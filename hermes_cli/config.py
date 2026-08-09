"""
Configuration management for Duck Agent.

Config files are stored in ~/.duck-agent/ for easy access:
- ~/.duck-agent/config.yaml  - All settings (model, toolsets, terminal, etc.)
- ~/.duck-agent/.env         - API keys and secrets

This module provides:
- duck-agent config          - Show current configuration
- duck-agent config edit     - Open config in editor
- duck-agent config get      - Print a resolved configuration value
- duck-agent config set      - Set a specific value
- duck-agent config unset    - Remove a user configuration value
- duck-agent config wizard   - Re-run setup wizard
"""
import copy
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set
from hermes_cli.route_identity import normalize_route_base_url
from hermes_cli.secret_prompt import masked_secret_prompt
logger = logging.getLogger(__name__)
_CONFIG_PARSE_WARNED: set = set()

def _backup_corrupt_config(config_path: Path) -> Optional[Path]:
    """Preserve a corrupted ``config.yaml`` by copying it to a timestamped ``.bak``.

    When the YAML can't be parsed, ``load_config()`` silently falls back to
    ``DEFAULT_CONFIG`` and the user's broken file stays on disk untouched.
    That file is still the user's only copy of their intended overrides — if
    they re-run the setup wizard or ``duck-agent config set`` (which rewrites
    ``config.yaml``), the broken-but-recoverable content is gone for good.

    This snapshots the corrupted file to ``config.yaml.corrupt.<ts>.bak`` so
    the user can diff/repair it. Unlike Gemini CLI's policy-file recovery
    (which resets the live file to a clean state), we deliberately leave
    ``config.yaml`` in place: duck-agent never silently mutates the user's config,
    and leaving it means a hand-fixed file is re-read on the next load. The
    backup is best-effort — any failure (permissions, symlink, disk full) is
    swallowed so config loading is never blocked by backup problems.

    Returns the backup path on success, else ``None``. Symlinks are not
    followed/copied (mirrors the Gemini #21541 lstat guard) to avoid
    clobbering whatever a malicious/misconfigured symlink points at.
    """
    try:
        if config_path.is_symlink():
            return None
        st = config_path.stat()
        if st.st_size == 0:
            return None
        ts = time.strftime('%Y%m%d-%H%M%S')
        backup_path = config_path.with_name(f'{config_path.name}.corrupt.{ts}.bak')
        sibling_baks = list(config_path.parent.glob(f'{config_path.name}.corrupt.*.bak'))
        for existing in sibling_baks:
            try:
                if existing.stat().st_size == st.st_size:
                    return None
            except OSError:
                continue
        if backup_path.exists():
            return None
        shutil.copy2(config_path, backup_path)
        return backup_path
    except Exception:
        return None

def _warn_config_parse_failure(config_path: Path, exc: Exception, *, fallback: str='defaults') -> None:
    """Surface a config.yaml parse failure to user, log, and stderr.

    A YAML parse error in ``~/.duck-agent/config.yaml`` causes ``load_config()``
    to silently fall back to ``DEFAULT_CONFIG``, which means every user
    override (auxiliary providers, fallback chain, model overrides, etc.)
    is dropped. Before this helper that was a one-line ``print(...)`` that
    scrolled off-screen on the first invocation and was never seen again.

    Now: warn once per (path, mtime_ns, size) on stderr **and** in
    ``agent.log`` / ``errors.log`` at WARNING level so ``duck-agent logs``
    surfaces it. Re-warns automatically if the file changes (different
    mtime/size), so users editing the config see the next failure. On the
    first warning for a given broken file we also snapshot it to a
    timestamped ``.bak`` (best-effort) so the user's recoverable content
    survives any later rewrite of ``config.yaml`` by the setup wizard or
    ``duck-agent config set``.

    ``fallback`` selects the message wording: ``"defaults"`` (fresh process,
    nothing else to serve) or ``"last-known-good"`` (in-process retention of
    the previously loaded config — see the codex#31188 port in
    ``_load_config_impl``).
    """
    try:
        st = config_path.stat()
        key = (str(config_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(config_path), 0, 0)
    if key in _CONFIG_PARSE_WARNED:
        return
    _CONFIG_PARSE_WARNED.add(key)
    backup_path = _backup_corrupt_config(config_path)
    if fallback == 'last-known-good':
        msg = f'Failed to parse {config_path}: {exc}. Keeping the previously loaded config for this process — edits to config.yaml are being IGNORED until the YAML is fixed.'
    else:
        msg = f'Failed to parse {config_path}: {exc}. Falling back to default config — every user override (auxiliary providers, fallback chain, model settings) is being IGNORED. Fix the YAML and restart.'
    if backup_path is not None:
        msg += f' A copy of the corrupted file was saved to {backup_path}.'
    logger.warning(msg)
    try:
        sys.stderr.write(f'⚠️  duck-agent config: {msg}\n')
        sys.stderr.flush()
    except Exception:
        pass
_IS_WINDOWS = platform.system() == 'Windows'
_ENV_VAR_NAME_RE = re.compile('^[A-Za-z_][A-Za-z0-9_]*$')
_ENV_VAR_NAME_DENYLIST: frozenset[str] = frozenset({'LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT', 'LD_DEBUG', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'PYTHONEXECUTABLE', 'PYTHONNOUSERSITE', 'NODE_OPTIONS', 'NODE_PATH', 'PATH', 'SHELL', 'BROWSER', 'EDITOR', 'VISUAL', 'PAGER', 'GIT_SSH_COMMAND', 'GIT_EXEC_PATH', 'GIT_SHELL', 'DUCK_AGENT_HOME', 'HERMES_PROFILE', 'HERMES_CONFIG', 'HERMES_ENV'})

def _reject_denylisted_env_var(key: str) -> None:
    """Raise if ``key`` is in :data:`_ENV_VAR_NAME_DENYLIST`.

    Centralised so both the regular and "secure" env writers share the
    same gate, and so the message is consistent for callers.
    """
    if key in _ENV_VAR_NAME_DENYLIST:
        raise ValueError(f'Environment variable {key!r} is on the writer denylist. Names that influence subprocess execution (LD_PRELOAD, PYTHONPATH, PATH, EDITOR, ...) or Duck Agent runtime location (DUCK_AGENT_HOME, HERMES_PROFILE, ...) cannot be persisted via the env writer. If you really need this, edit ~/.duck-agent/.env directly.')
_LAST_EXPANDED_CONFIG_BY_PATH: Dict[str, Any] = {}
_LOAD_CONFIG_CACHE: Dict[str, Tuple[int, int, int, int, Dict[str, Any], Dict[str, Optional[str]]]] = {}
_RAW_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
_CONFIG_LOCK = threading.RLock()
_EXTRA_ENV_KEYS = frozenset({'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_TOKEN', 'DISCORD_HOME_CHANNEL', 'DISCORD_HOME_CHANNEL_NAME', 'TELEGRAM_HOME_CHANNEL', 'TELEGRAM_HOME_CHANNEL_NAME', 'SLACK_HOME_CHANNEL', 'SLACK_HOME_CHANNEL_NAME', 'SIGNAL_ACCOUNT', 'SIGNAL_HTTP_URL', 'SIGNAL_ALLOWED_USERS', 'SIGNAL_GROUP_ALLOWED_USERS', 'SIGNAL_HOME_CHANNEL', 'SIGNAL_HOME_CHANNEL_NAME', 'SMS_HOME_CHANNEL', 'SMS_HOME_CHANNEL_NAME', 'DINGTALK_CLIENT_ID', 'DINGTALK_CLIENT_SECRET', 'DINGTALK_HOME_CHANNEL', 'DINGTALK_HOME_CHANNEL_NAME', 'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_ENCRYPT_KEY', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_HOME_CHANNEL', 'FEISHU_HOME_CHANNEL_NAME', 'YUANBAO_HOME_CHANNEL', 'YUANBAO_HOME_CHANNEL_NAME', 'WECOM_BOT_ID', 'WECOM_SECRET', 'WECOM_CALLBACK_CORP_ID', 'WECOM_CALLBACK_CORP_SECRET', 'WECOM_CALLBACK_AGENT_ID', 'WECOM_CALLBACK_TOKEN', 'WECOM_CALLBACK_ENCODING_AES_KEY', 'WECOM_CALLBACK_HOST', 'WECOM_CALLBACK_PORT', 'WECOM_HOME_CHANNEL', 'WECOM_HOME_CHANNEL_NAME', 'WEIXIN_ACCOUNT_ID', 'WEIXIN_TOKEN', 'WEIXIN_BASE_URL', 'WEIXIN_CDN_BASE_URL', 'WEIXIN_HOME_CHANNEL', 'WEIXIN_HOME_CHANNEL_NAME', 'WEIXIN_DM_POLICY', 'WEIXIN_GROUP_POLICY', 'WEIXIN_ALLOWED_USERS', 'WEIXIN_GROUP_ALLOWED_USERS', 'WEIXIN_ALLOW_ALL_USERS', 'BLUEBUBBLES_SERVER_URL', 'BLUEBUBBLES_PASSWORD', 'BLUEBUBBLES_HOME_CHANNEL', 'BLUEBUBBLES_HOME_CHANNEL_NAME', 'QQ_APP_ID', 'QQ_CLIENT_SECRET', 'QQBOT_HOME_CHANNEL', 'QQBOT_HOME_CHANNEL_NAME', 'QQ_HOME_CHANNEL', 'QQ_HOME_CHANNEL_NAME', 'QQ_ALLOWED_USERS', 'QQ_GROUP_ALLOWED_USERS', 'QQ_ALLOW_ALL_USERS', 'QQ_MARKDOWN_SUPPORT', 'QQ_STT_API_KEY', 'QQ_STT_BASE_URL', 'QQ_STT_MODEL', 'IRC_SERVER', 'IRC_PORT', 'IRC_NICKNAME', 'IRC_CHANNEL', 'IRC_USE_TLS', 'IRC_SERVER_PASSWORD', 'IRC_NICKSERV_PASSWORD', 'TERMINAL_ENV', 'TERMINAL_SSH_KEY', 'TERMINAL_SSH_PORT', 'HERMES_TOOL_PROGRESS_MODE', 'WHATSAPP_MODE', 'WHATSAPP_ENABLED', 'MATTERMOST_HOME_CHANNEL', 'MATTERMOST_HOME_CHANNEL_NAME', 'MATTERMOST_REPLY_MODE', 'MATRIX_PASSWORD', 'MATRIX_ENCRYPTION', 'MATRIX_DEVICE_ID', 'MATRIX_HOME_ROOM', 'MATRIX_REQUIRE_MENTION', 'MATRIX_FREE_RESPONSE_ROOMS', 'MATRIX_AUTO_THREAD', 'MATRIX_DM_AUTO_THREAD', 'MATRIX_RECOVERY_KEY', 'HERMES_LANGFUSE_ENV', 'HERMES_LANGFUSE_RELEASE', 'HERMES_LANGFUSE_SAMPLE_RATE', 'HERMES_LANGFUSE_MAX_CHARS', 'HERMES_LANGFUSE_DEBUG', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL', 'HERMES_ACP_AUTH_METHOD', 'HERMES_ACP_AUTO_APPROVE', 'HERMES_COPILOT_ACP_COMMAND', 'HERMES_COPILOT_ACP_ARGS', 'COPILOT_CLI_PATH', 'COPILOT_ACP_BASE_URL'})
import yaml
from hermes_cli.colors import Colors, color
from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
_MANAGED_TRUE_VALUES = ('true', '1', 'yes')
_MANAGED_SYSTEM_NAMES = {'nix': 'NixOS', 'nixos': 'NixOS'}
_NIX_STORE = Path('/nix/store')
_IGNORED_MANAGED_VALUES = frozenset({'brew', 'homebrew'})

def get_managed_system() -> Optional[str]:
    """Return the package manager owning this install, if any."""
    raw = os.getenv('HERMES_MANAGED', '').strip()
    if raw:
        normalized = raw.lower()
        if normalized in _IGNORED_MANAGED_VALUES:
            return None
        if normalized in _MANAGED_TRUE_VALUES:
            return 'NixOS'
        return _MANAGED_SYSTEM_NAMES.get(normalized, raw)
    managed_marker = get_hermes_home() / '.managed'
    if managed_marker.exists():
        return 'NixOS'
    return None

def is_managed() -> bool:
    """Check if Duck Agent is running in package-manager-managed mode.

    Two signals: the HERMES_MANAGED env var (set by the systemd service),
    or a .managed marker file in DUCK_AGENT_HOME (set by the NixOS activation
    script, so interactive shells also see it).
    """
    return get_managed_system() is not None
_NIX_UPDATE_MSG = 'Update Duck Agent through the Nix source that installed it (e.g. nix profile upgrade, or update your flake input and rebuild with nixos-rebuild or home-manager switch)'

def get_managed_update_command() -> Optional[str]:
    """Return the preferred upgrade command for a managed install."""
    managed_system = get_managed_system()
    if managed_system == 'NixOS':
        return _NIX_UPDATE_MSG
    return None

def _install_method_project_root(project_root: Optional[Path]=None) -> Path:
    """Resolve the directory that holds the *running code* (the install tree).

    This is the parent of ``hermes_cli/`` — i.e. the git checkout for source
    installs, ``/opt/duck-agent`` inside the published image. It is a property of
    the running interpreter, NOT of ``$DUCK_AGENT_HOME``, which is why a
    code-scoped stamp here is immune to two installs sharing one data
    directory.
    """
    if project_root is not None:
        return project_root
    return Path(__file__).parent.parent.resolve()

def detect_install_method(project_root: Optional[Path]=None) -> str:
    """Detect how Duck Agent was installed: 'docker', 'nix', 'nixos', 'git', or 'unknown'.

    Resolution order:
    1. Code-scoped stamp ``<install tree>/.install_method`` (next to the
       running code) — the authoritative marker.
    2. Legacy home-scoped stamp ``$DUCK_AGENT_HOME/.install_method`` — read for
       backward compatibility, but a ``docker`` value is IGNORED when we are
       not actually running inside a container (see below).
    3. HERMES_MANAGED env / .managed marker (NixOS managed mode)
    4. /nix/store/ path detection -> 'nix' (nix run / nix profile install)
    5. .git directory presence -> 'git'
    6. Fallback -> 'unknown'

    Why the stamp is code-scoped, not home-scoped (issue: shared ``~/.duck-agent``)
    --------------------------------------------------------------------------
    The install method describes *the binary that is running*, but
    ``$DUCK_AGENT_HOME`` is a shared DATA directory — the Docker docs deliberately
    bind-mount it (``~/.duck-agent:/opt/data``) so config/sessions/memory persist
    and can be shared with a host-side Desktop/CLI install. When a
    containerised gateway and a host install share one ``$DUCK_AGENT_HOME``, a
    home-scoped stamp is a single slot describing two different installs:
    the container stamps ``docker`` on every boot, the host install then reads
    ``docker`` and ``duck-agent update`` refuses to run ("doesn't apply inside the
    Docker container") even though the host binary is a perfectly updatable
    git/pip install. Scoping the stamp to the install tree gives each install
    its own truthful marker.

    Self-healing for already-poisoned homes: a legacy ``docker`` value in the
    home-scoped stamp is only honoured when we are genuinely in a container.
    On a host install that read a contaminating ``docker`` stamp, we fall
    through to managed/.git detection instead — so existing shared-home
    setups recover without the user touching anything.

    Note: running inside a container is NOT treated as "docker" on its own.
    The supported installs self-identify via the code-scoped stamp:
      - the curl installer (scripts/install.sh, the README/website install
        command) git-clones the repo and stamps ``git`` next to the code;
      - the published ``nousresearch/duck-agent`` image bakes a ``docker``
        stamp into ``/opt/duck-agent`` at build time.
    An unsupported manual install dropped into a container (no stamp) falls
    through to the ``.git`` checks and behaves like any off-path install.
    See issue #34397.
    """
    root = _install_method_project_root(project_root)
    supported_methods = {'docker', 'nix', 'nixos', 'git', 'unknown'}
    try:
        method = (root / '.install_method').read_text(encoding='utf-8').strip().lower()
        if method in supported_methods:
            return method
    except OSError:
        pass
    try:
        method = (get_hermes_home() / '.install_method').read_text(encoding='utf-8').strip().lower()
        if method in supported_methods and (not (method == 'docker' and (not _running_in_container()))):
            return method
    except OSError:
        pass
    managed = get_managed_system()
    if managed:
        return managed.lower().replace(' ', '-')
    try:
        resolved = root.resolve()
        if resolved != _NIX_STORE and _NIX_STORE in resolved.parents:
            return 'nix'
    except OSError:
        pass
    git_path = root / '.git'
    if git_path.is_dir():
        return 'git'
    if git_path.is_file():
        try:
            content = git_path.read_text(encoding='utf-8').strip()
            if content.startswith('gitdir:'):
                return 'git'
        except OSError:
            pass
    return 'unknown'

def _running_in_container() -> bool:
    """Thin wrapper around ``hermes_constants.is_container`` (import-safe)."""
    try:
        from hermes_constants import is_container
        return is_container()
    except Exception:
        return False

def stamp_install_method(method: str, project_root: Optional[Path]=None) -> None:
    """Write the install method next to the running code (code-scoped stamp).

    The stamp lives in the install tree (``<install tree>/.install_method``),
    not in ``$DUCK_AGENT_HOME``, so that two installs sharing one data directory
    do not overwrite each other's marker. See ``detect_install_method`` for
    the full rationale.

    Best-effort: if the install tree is read-only (e.g. the immutable
    ``/opt/duck-agent`` in the published image, which instead bakes the stamp at
    build time) the write silently no-ops and detection falls back to its
    other signals.
    """
    root = _install_method_project_root(project_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / '.install_method').write_text(method + '\n', encoding='utf-8')
    except OSError:
        pass

def recommended_update_command_for_method(method: str) -> str:
    """Return the update command or guidance for a given install method."""
    if method in {'nix', 'nixos'}:
        return _NIX_UPDATE_MSG
    if method == 'docker':
        return 'docker pull nousresearch/duck-agent:latest'
    return 'duck-agent update'

def recommended_update_command() -> str:
    """Return the best update command for the current installation."""
    managed_cmd = get_managed_update_command()
    if managed_cmd:
        return managed_cmd
    method = detect_install_method(get_project_root())
    return recommended_update_command_for_method(method)
_DOCKER_UPDATE_MESSAGE = "✗ ``duck-agent update`` doesn't apply inside the Docker container.\n\nDuck Agent runs as a published image (nousresearch/duck-agent), not a\ngit checkout — the container has no working tree to pull into.  Update by\npulling a fresh image and restarting your container instead:\n\n  docker pull nousresearch/duck-agent:latest\n  # then restart whatever started the container, e.g.:\n  docker compose up -d --force-recreate duck-agent\n  # or, for ad-hoc runs, exit the current container and `docker run` again\n\nVerify the new version after restart:\n  docker run --rm nousresearch/duck-agent:latest --version\n\nNotes:\n  • If you pinned a specific tag (e.g. ``:v0.14.0``) the ``:latest`` tag\n    won't move your container — pull the newer tag you actually want, or\n    switch to ``:latest`` / ``:main`` for rolling updates.  See available\n    tags at https://hub.docker.com/r/nousresearch/duck-agent/tags\n  • Your config and session history live under ``$DUCK_AGENT_HOME`` (``/opt/data``\n    in the container, typically bind-mounted from the host) and persist\n    across image upgrades — re-pulling doesn't lose any state.\n  • Running a fork?  Build your own image with this repo's ``Dockerfile``\n    and replace the ``docker pull`` step with your build/push pipeline."

def format_docker_update_message() -> str:
    """Return the user-facing message for ``duck-agent update`` inside Docker.

    Centralised so ``cmd_update`` (the apply path) and ``_cmd_update_check``
    (the dry-run path) share the same wording.  See ``_DOCKER_UPDATE_MESSAGE``
    above for the full rationale.
    """
    return _DOCKER_UPDATE_MESSAGE

def format_managed_message(action: str='modify this Duck Agent installation') -> str:
    """Build a user-facing error for managed installs."""
    managed_system = get_managed_system() or 'a package manager'
    raw = os.getenv('HERMES_MANAGED', '').strip().lower()
    if managed_system == 'NixOS':
        env_hint = 'true' if raw in _MANAGED_TRUE_VALUES else raw or 'true'
        return f'Cannot {action}: this Duck Agent installation is managed by NixOS (HERMES_MANAGED={env_hint}).\nEdit services.duck-agent.settings in your configuration.nix and run:\n  sudo nixos-rebuild switch'
    return f'Cannot {action}: this Duck Agent installation is managed by {managed_system}.\nUse your package manager to upgrade or reinstall Duck Agent.'

def managed_error(action: str='modify configuration'):
    """Print user-friendly error for managed mode."""
    print(format_managed_message(action), file=sys.stderr)

def get_container_exec_info() -> Optional[dict]:
    """Read container mode metadata from DUCK_AGENT_HOME/.container-mode.

    Returns a dict with keys: backend, container_name, exec_user, hermes_bin
    or None if container mode is not active, we're already inside the
    container, or HERMES_DEV=1 is set.

    The .container-mode file is written by the NixOS activation script when
    container.enable = true. It tells the host CLI to exec into the container
    instead of running locally.
    """
    if os.environ.get('HERMES_DEV') == '1':
        return None
    from hermes_constants import is_container
    if is_container():
        return None
    container_mode_file = get_hermes_home() / '.container-mode'
    try:
        info = {}
        with open(container_mode_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if '=' in line and (not line.startswith('#')):
                    key, _, value = line.partition('=')
                    info[key.strip()] = value.strip()
    except FileNotFoundError:
        return None
    backend = info.get('backend', 'docker')
    container_name = info.get('container_name', 'duck-agent')
    exec_user = info.get('exec_user', 'duck-agent')
    hermes_bin = info.get('hermes_bin', '/data/current-package/bin/duck-agent')
    return {'backend': backend, 'container_name': container_name, 'exec_user': exec_user, 'hermes_bin': hermes_bin}
from hermes_constants import get_hermes_home, get_process_hermes_home
from utils import atomic_replace, fast_safe_load

def get_config_path() -> Path:
    """Get the main config file path."""
    return get_hermes_home() / 'config.yaml'

def get_env_path() -> Path:
    """Get the .env file path (for API keys)."""
    return get_hermes_home() / '.env'

def get_project_root() -> Path:
    """Get the project installation directory."""
    return Path(__file__).parent.parent.resolve()

def _resolve_hermes_uid_gid() -> tuple[Optional[int], Optional[int]]:
    """Read the HERMES_UID / HERMES_GID env vars set by Docker deployments.

    Docker containers running Duck Agent commonly set these to map the in-container
    user to a host user so volume-mounted state files end up with the right
    ownership. The entrypoint chowns the top-level DUCK_AGENT_HOME once, but
    subdirectories created at runtime by ``ensure_hermes_home()`` (especially
    for profile namespaces under ``profiles/<name>/``) need the same chown
    or they land as ``root:root`` and block subsequent uid-mapped workers
    with ``PermissionError [Errno 13]``. See #34107.

    Returns ``(uid, gid)`` parsed from the env vars, or ``(None, None)``
    when either is missing/invalid. Returns ``(None, None)`` on Windows
    too (where chown is a no-op anyway).
    """
    if sys.platform == 'win32':
        return (None, None)
    uid_str = os.environ.get('HERMES_UID', '').strip()
    gid_str = os.environ.get('HERMES_GID', '').strip()
    try:
        uid = int(uid_str) if uid_str else None
    except ValueError:
        uid = None
    try:
        gid = int(gid_str) if gid_str else None
    except ValueError:
        gid = None
    return (uid, gid)

def _chown_to_hermes_uid(path) -> None:
    """Chown ``path`` to ``HERMES_UID:HERMES_GID`` if those env vars are set.

    No-op when:
      - Either env var is unset/invalid
      - The current process isn't root (chown will EPERM — silently ignored)
      - On Windows (chown semantics don't apply)

    Used by :func:`_secure_dir` to keep ownership consistent across all
    directories created by :func:`ensure_hermes_home` on Docker deployments.
    See #34107.
    """
    uid, gid = _resolve_hermes_uid_gid()
    if uid is None and gid is None:
        return
    try:
        os.chown(path, uid if uid is not None else -1, gid if gid is not None else -1)
    except (OSError, AttributeError, NotImplementedError):
        pass

def _secure_dir(path):
    """Set directory to owner-only access (0700 by default). No-op on Windows.

    Skipped in managed mode — the NixOS module sets group-readable
    permissions (0750) so interactive users in the duck-agent group can
    share state with the gateway service.

    The mode can be overridden via the HERMES_HOME_MODE environment variable
    (e.g. HERMES_HOME_MODE=0701) for deployments where a web server (nginx,
    caddy, etc.) needs to traverse DUCK_AGENT_HOME to reach a served subdirectory.
    The execute-only bit on a directory permits cd-through without exposing
    directory listings.

    Also applies ``HERMES_UID``/``HERMES_GID``-based ownership when those env
    vars are set (#34107 — Docker deployments need this so profile subdirs
    created at runtime by kanban workers don't land as root:root and block
    subsequent uid-mapped workers).
    """
    if is_managed():
        return
    try:
        mode_str = os.environ.get('HERMES_HOME_MODE', '').strip()
        mode = int(mode_str, 8) if mode_str else 448
    except ValueError:
        mode = 448
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass
    _chown_to_hermes_uid(path)

def _is_container() -> bool:
    """Detect if we're running inside a Docker/Podman/LXC container.

    When Duck Agent runs in a container with volume-mounted config files, forcing
    0o600 permissions breaks multi-process setups where the gateway and
    dashboard run as different UIDs or the volume mount requires broader
    permissions.
    """
    if os.environ.get('HERMES_CONTAINER') or os.environ.get('HERMES_SKIP_CHMOD'):
        return True
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r', encoding='utf-8') as f:
            cgroup_content = f.read()
        if 'docker' in cgroup_content or 'lxc' in cgroup_content or 'kubepods' in cgroup_content:
            return True
    except (OSError, IOError):
        pass
    return False

def _secure_file(path):
    """Set file to owner-only read/write (0600). No-op on Windows.

    Skipped in managed mode — the NixOS activation script sets
    group-readable permissions (0640) on config files.

    Skipped in containers — Docker/Podman volume mounts often need broader
    permissions.  Set HERMES_SKIP_CHMOD=1 to force-skip on other systems.
    """
    if is_managed() or _is_container():
        return
    try:
        if os.path.exists(str(path)):
            os.chmod(path, 384)
    except (OSError, NotImplementedError):
        pass

def _ensure_default_soul_md(home: Path) -> None:
    """Seed a default SOUL.md into DUCK_AGENT_HOME, upgrading legacy empty templates.

    First run: write DEFAULT_SOUL_MD. Existing installs whose SOUL.md is still
    the old comment-only scaffold (seeded by older install.sh / install.ps1 /
    docker images, which shadowed the runtime default) get upgraded in place to
    DEFAULT_SOUL_MD. A SOUL.md the user actually customized is never touched.
    """
    soul_path = home / 'SOUL.md'
    if soul_path.exists():
        try:
            existing = soul_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return
        if not is_legacy_template_soul(existing):
            return
    soul_path.write_text(DEFAULT_SOUL_MD, encoding='utf-8')
    _secure_file(soul_path)
_HERMES_HOME_ENSURED: set = set()

def ensure_hermes_home():
    """Ensure ~/.duck-agent directory structure exists with secure permissions.

    In managed mode (NixOS), dirs are created by the activation script with
    setgid + group-writable (2770). We skip mkdir and set umask(0o007) so
    any files created (e.g. SOUL.md) are group-writable (0660).

    Memoized per home path: this runs on EVERY ``load_config()`` (inside the
    config lock), and the ~14 mkdir/chmod syscalls per call made repeated
    config loads the dominant cost of hot read paths like ``model.options``.
    After the first successful pass for a given ``DUCK_AGENT_HOME`` we only re-run
    the full walk if the home directory itself has vanished (a deleted home is
    recreated on the next load, as before). Profile switches change
    ``get_hermes_home()`` and therefore re-run for the new path.
    """
    home = get_hermes_home()
    key = str(home)
    if key in _HERMES_HOME_ENSURED and home.is_dir():
        return
    if home.parent.name == 'profiles' and (not home.exists()):
        raise FileNotFoundError(f'Named profile home does not exist: {home}. Create the profile explicitly before using it.')
    if is_managed():
        old_umask = os.umask(7)
        try:
            _ensure_hermes_home_managed(home)
        finally:
            os.umask(old_umask)
    else:
        home.mkdir(parents=True, exist_ok=True)
        _secure_dir(home)
        for subdir in ('cron', 'sessions', 'logs', 'logs/curator', 'memories', 'pairing', 'hooks', 'image_cache', 'audio_cache', 'skills'):
            d = home / subdir
            d.mkdir(parents=True, exist_ok=True)
            _secure_dir(d)
        _ensure_default_soul_md(home)
    _HERMES_HOME_ENSURED.add(key)

def _ensure_hermes_home_managed(home: Path):
    """Managed-mode variant: verify dirs exist (activation creates them), seed SOUL.md."""
    if not home.is_dir():
        raise RuntimeError(f"DUCK_AGENT_HOME {home} does not exist. Run 'sudo nixos-rebuild switch' first.")
    for subdir in ('cron', 'sessions', 'logs', 'memories'):
        d = home / subdir
        if not d.is_dir():
            raise RuntimeError(f"{d} does not exist. Run 'sudo nixos-rebuild switch' first.")
    (home / 'logs' / 'curator').mkdir(parents=True, exist_ok=True)
    _ensure_default_soul_md(home)
from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS
ENV_VARS_BY_VERSION: Dict[int, List[str]] = {3: ['FIRECRAWL_API_KEY', 'BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID', 'FAL_KEY'], 4: ['VOICE_TOOLS_OPENAI_KEY', 'ELEVENLABS_API_KEY'], 5: ['WHATSAPP_ENABLED', 'WHATSAPP_MODE', 'WHATSAPP_ALLOWED_USERS', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_ALLOWED_USERS'], 10: ['TAVILY_API_KEY'], 11: ['TERMINAL_MODAL_MODE']}
REQUIRED_ENV_VARS = {}

def get_missing_env_vars(required_only: bool=False) -> List[Dict[str, Any]]:
    """
    Check which environment variables are missing.
    
    Returns list of dicts with var info for missing variables.
    """
    missing = []
    for var_name, info in REQUIRED_ENV_VARS.items():
        if not get_env_value(var_name):
            missing.append({'name': var_name, **info, 'is_required': True})
    if not required_only:
        for var_name, info in OPTIONAL_ENV_VARS.items():
            if not get_env_value(var_name):
                missing.append({'name': var_name, **info, 'is_required': False})
    return missing

def _set_nested(config, dotted_key: str, value):
    """Set a value at an arbitrarily nested dotted key path.

    Supports both dict and list navigation:
      _set_nested(c, "a.b.c", 1)     → c["a"]["b"]["c"] = 1
      _set_nested(c, "a.0.b", 1)     → c["a"][0]["b"] = 1
      _set_nested(c, "providers.1", "x") → c["providers"][1] = "x"

    Intermediate dicts are created on demand.  List indices are parsed
    from numeric path segments; the referenced index must already exist
    (we do not grow lists — the user is navigating into structure they
    wrote themselves).  If a segment targets a non-container leaf
    (scalar), the leaf is replaced with a fresh dict so the write can
    proceed — this preserves the pre-existing behavior for bare scalar
    overrides (e.g. setting ``a.b.c`` where ``a.b`` was previously a
    string).

    Guards against #17876: before this fix the code unconditionally
    replaced any non-dict value (including lists) with ``{}``, silently
    destroying list-typed config like ``custom_providers`` whenever a
    caller used an indexed path.
    """
    parts = dotted_key.split('.')
    current = config
    for part in parts[:-1]:
        if isinstance(current, list):
            try:
                idx = int(part)
            except (TypeError, ValueError):
                raise TypeError(f'Cannot navigate into list at key {dotted_key!r}: segment {part!r} is not a numeric index')
            current = current[idx]
        elif isinstance(current, dict):
            existing = current.get(part)
            if part not in current or not isinstance(existing, (dict, list)):
                current[part] = {}
            current = current[part]
        else:
            raise TypeError(f'Cannot navigate into {type(current).__name__} at key {dotted_key!r}')
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value

def clear_model_endpoint_credentials(model_cfg: Dict[str, Any], *, clear_api_key: bool=True, clear_api_mode: bool=True, clear_base_url: bool=False) -> Dict[str, Any]:
    """Remove stale inline endpoint credentials from a model config.

    ``model.api_key`` is valid only for explicit custom endpoint assignments.
    Built-in providers resolve credentials from env vars, auth.json, or the
    credential pool. When switching away from a custom endpoint, leaving these
    fields behind keeps secrets in config.yaml and can contaminate later custom
    resolution paths.
    """
    if not isinstance(model_cfg, dict):
        return model_cfg
    if clear_api_key:
        model_cfg.pop('api_key', None)
        model_cfg.pop('api', None)
    if clear_api_mode:
        model_cfg.pop('api_mode', None)
    if clear_base_url:
        model_cfg.pop('base_url', None)
    return model_cfg
_MISSING = object()

def _get_nested(config, dotted_key: str):
    """Return a dotted-path value from nested dict/list config data."""
    current = config
    for part in dotted_key.split('.'):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return _MISSING
        elif isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            return _MISSING
    return current

def _unset_nested(config, dotted_key: str) -> bool:
    """Remove a dotted-path value from nested dict/list config data."""
    parts = dotted_key.split('.')
    if not parts:
        return False
    parents = []
    current = config
    for part in parts[:-1]:
        parents.append((current, part))
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return False
        elif isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
        else:
            return False
    last = parts[-1]
    removed = False
    if isinstance(current, list):
        try:
            current.pop(int(last))
            removed = True
        except (TypeError, ValueError, IndexError):
            return False
    elif isinstance(current, dict):
        if last not in current:
            return False
        del current[last]
        removed = True
    else:
        return False
    for parent, part in reversed(parents):
        if current != {}:
            break
        if isinstance(parent, list):
            try:
                idx = int(part)
            except (TypeError, ValueError):
                break
            if 0 <= idx < len(parent) and parent[idx] == {}:
                parent.pop(idx)
                current = parent
                continue
        elif isinstance(parent, dict) and parent.get(part) == {}:
            del parent[part]
            current = parent
            continue
        break
    return removed

def _is_env_config_key(key: str) -> bool:
    """Return whether `duck-agent config set` routes this key to .env."""
    if '.' in key:
        return False
    key_upper = key.upper()
    api_keys = ['OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VOICE_TOOLS_OPENAI_KEY', 'EXA_API_KEY', 'PARALLEL_API_KEY', 'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL', 'FIRECRAWL_GATEWAY_URL', 'TOOL_GATEWAY_DOMAIN', 'TOOL_GATEWAY_SCHEME', 'TOOL_GATEWAY_USER_TOKEN', 'TAVILY_API_KEY', 'BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID', 'BROWSER_USE_API_KEY', 'FAL_KEY', 'TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN', 'TERMINAL_SSH_HOST', 'TERMINAL_SSH_USER', 'TERMINAL_SSH_KEY', 'SUDO_PASSWORD', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'GITHUB_TOKEN', 'HONCHO_API_KEY']
    return key_upper in api_keys or key_upper.endswith(('_API_KEY', '_TOKEN', '_SECRET')) or key_upper.startswith('TERMINAL_SSH')

def _format_config_get_value(value, *, as_json: bool) -> str:
    """Format a config value for command-line output."""
    if as_json:
        import json
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return 'null'
    if isinstance(value, (dict, list)):
        return yaml.safe_dump(value, sort_keys=False).rstrip()
    return str(value)

def get_missing_config_fields() -> List[Dict[str, Any]]:
    """
    Check which config fields are missing or outdated (recursive).
    
    Walks the DEFAULT_CONFIG tree at arbitrary depth and reports any keys
    present in defaults but absent from the user's loaded config.
    """
    config = load_config()
    missing = []

    def _check(defaults: dict, current: dict, prefix: str=''):
        for key, default_value in defaults.items():
            if key.startswith('_'):
                continue
            full_key = key if not prefix else f'{prefix}.{key}'
            if key not in current:
                missing.append({'key': full_key, 'default': default_value, 'description': f'New config option: {full_key}'})
            elif isinstance(default_value, dict) and isinstance(current.get(key), dict):
                _check(default_value, current[key], full_key)
    _check(DEFAULT_CONFIG, config)
    return missing

def get_missing_skill_config_vars() -> List[Dict[str, Any]]:
    """Return skill-declared config vars that are missing or empty in config.yaml.

    Scans all enabled skills for ``metadata.duck-agent.config`` entries, then checks
    which ones are absent or empty under ``skills.config.<key>`` in the user's
    config.yaml.  Returns a list of dicts suitable for prompting.
    """
    try:
        from agent.skill_utils import discover_all_skill_config_vars, SKILL_CONFIG_PREFIX
    except Exception:
        return []
    try:
        all_vars = discover_all_skill_config_vars()
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug('discover_all_skill_config_vars failed: %s', e)
        return []
    if not all_vars:
        return []
    config = load_config()
    missing: List[Dict[str, Any]] = []
    for var in all_vars:
        storage_key = f"{SKILL_CONFIG_PREFIX}.{var['key']}"
        parts = storage_key.split('.')
        current = config
        value = None
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
                value = current
            else:
                value = None
                break
        if value is None or (isinstance(value, str) and (not value.strip())):
            missing.append(var)
    return missing
_PROVIDER_NORMALIZE_WARNED: set = set()

def _warn_once_per_provider(provider_key: str, signature: str, msg: str, *args: Any) -> None:
    """Emit ``logger.warning(msg, *args)`` at most once per (provider, signature)."""
    dedup_key = (provider_key or '?', signature)
    if dedup_key in _PROVIDER_NORMALIZE_WARNED:
        return
    _PROVIDER_NORMALIZE_WARNED.add(dedup_key)
    logger.warning(msg, *args)

def _normalize_custom_provider_entry(entry: Any, *, provider_key: str='') -> Optional[Dict[str, Any]]:
    """Return a runtime-compatible custom provider entry or ``None``."""
    if not isinstance(entry, dict):
        return None
    entry = dict(entry)
    _CAMEL_ALIASES: Dict[str, str] = {'apiKey': 'api_key', 'baseUrl': 'base_url', 'apiMode': 'api_mode', 'keyEnv': 'key_env', 'apiKeyEnv': 'key_env', 'defaultModel': 'default_model', 'contextLength': 'context_length', 'rateLimitDelay': 'rate_limit_delay'}
    if 'api_key_env' in entry and 'key_env' not in entry:
        entry['key_env'] = entry['api_key_env']
    _KNOWN_KEYS = {'provider', 'name', 'api', 'url', 'base_url', 'api_key', 'key_env', 'api_key_env', 'api_mode', 'transport', 'model', 'default_model', 'models', 'context_length', 'rate_limit_delay', 'request_timeout_seconds', 'stale_timeout_seconds', 'discover_models', 'extra_body', 'extra_headers', 'ssl_ca_cert', 'ssl_verify'}
    for camel, snake in _CAMEL_ALIASES.items():
        if camel in entry and snake not in entry:
            _warn_once_per_provider(provider_key, f'camel:{camel}', "providers.%s: camelCase key '%s' auto-mapped to '%s' (use snake_case to avoid this warning)", provider_key or '?', camel, snake)
            entry[snake] = entry[camel]
    unknown = set(entry.keys()) - _KNOWN_KEYS - set(_CAMEL_ALIASES.keys())
    if unknown:
        _warn_once_per_provider(provider_key, 'unknown:' + ','.join(sorted(unknown)), 'providers.%s: unknown config keys ignored: %s', provider_key or '?', ', '.join(sorted(unknown)))
    from urllib.parse import urlparse
    base_url = ''
    for url_key in ('base_url', 'url', 'api'):
        raw_url = entry.get(url_key)
        if isinstance(raw_url, str) and raw_url.strip():
            candidate = raw_url.strip()
            if re.search('\\{[^}]+\\}', candidate):
                base_url = candidate
                break
            parsed = urlparse(candidate)
            if parsed.scheme and parsed.netloc:
                base_url = candidate
                break
            else:
                logger.warning("providers.%s: '%s' value '%s' is not a valid URL (no scheme or host) — skipped", provider_key or '?', url_key, candidate)
    if not base_url:
        return None
    name = ''
    raw_name = entry.get('name')
    if isinstance(raw_name, str) and raw_name.strip():
        name = raw_name.strip()
    elif provider_key.strip():
        name = provider_key.strip()
    if not name:
        return None
    normalized: Dict[str, Any] = {'name': name, 'base_url': base_url}
    provider_key = provider_key.strip()
    if provider_key:
        normalized['provider_key'] = provider_key
    api_key = entry.get('api_key')
    if isinstance(api_key, str) and api_key.strip():
        normalized['api_key'] = api_key.strip()
    key_env = entry.get('key_env')
    if isinstance(key_env, str) and key_env.strip():
        normalized['key_env'] = key_env.strip()
    api_mode = entry.get('api_mode') or entry.get('transport')
    if isinstance(api_mode, str) and api_mode.strip():
        normalized['api_mode'] = api_mode.strip()
    model_name = entry.get('model') or entry.get('default_model')
    if isinstance(model_name, str) and model_name.strip():
        normalized['model'] = model_name.strip()
    models = entry.get('models')
    if isinstance(models, dict) and models:
        normalized['models'] = dict(models)
    elif isinstance(models, list) and models:
        normalized_models: Dict[str, Any] = {}
        for item in models:
            if isinstance(item, str) and item.strip():
                normalized_models[item.strip()] = {}
                continue
            if not isinstance(item, dict):
                continue
            model_id = item.get('id')
            if not isinstance(model_id, str) or not model_id.strip():
                model_id = item.get('name')
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_meta = {k: v for k, v in item.items() if k not in {'id', 'name'}}
            normalized_models[model_id.strip()] = model_meta
        if normalized_models:
            normalized['models'] = normalized_models
    context_length = entry.get('context_length')
    if isinstance(context_length, int) and context_length > 0:
        normalized['context_length'] = context_length
    rate_limit_delay = entry.get('rate_limit_delay')
    if isinstance(rate_limit_delay, (int, float)) and rate_limit_delay >= 0:
        normalized['rate_limit_delay'] = rate_limit_delay
    discover_models = entry.get('discover_models')
    if isinstance(discover_models, bool):
        normalized['discover_models'] = discover_models
    extra_body = entry.get('extra_body')
    if isinstance(extra_body, dict):
        normalized['extra_body'] = dict(extra_body)
    normalized_headers = normalize_extra_headers(entry.get('extra_headers'))
    if normalized_headers:
        normalized['extra_headers'] = normalized_headers
    ssl_ca_cert = entry.get('ssl_ca_cert')
    if isinstance(ssl_ca_cert, str) and ssl_ca_cert.strip():
        normalized['ssl_ca_cert'] = ssl_ca_cert.strip()
    ssl_verify = entry.get('ssl_verify')
    if isinstance(ssl_verify, bool):
        normalized['ssl_verify'] = ssl_verify
    elif isinstance(ssl_verify, str) and ssl_verify.strip():
        normalized['ssl_verify'] = ssl_verify.strip()
    return normalized

def _custom_provider_entry_to_provider_config(entry: Any, *, provider_key: str='') -> Optional[Dict[str, Any]]:
    """Translate a legacy custom provider entry to the v12 providers shape."""
    normalized = _normalize_custom_provider_entry(dict(entry) if isinstance(entry, dict) else entry, provider_key=provider_key)
    if normalized is None:
        return None
    provider_entry: Dict[str, Any] = {'api': normalized['base_url']}
    for field in ('name', 'api_key', 'key_env', 'models', 'context_length', 'rate_limit_delay', 'discover_models', 'extra_body', 'extra_headers', 'ssl_ca_cert', 'ssl_verify'):
        if field in normalized:
            provider_entry[field] = normalized[field]
    if 'model' in normalized:
        provider_entry['default_model'] = normalized['model']
    if 'api_mode' in normalized:
        provider_entry['transport'] = normalized['api_mode']
    return provider_entry

def providers_dict_to_custom_providers(providers_dict: Any) -> List[Dict[str, Any]]:
    """Normalize ``providers`` config entries into the legacy custom-provider shape."""
    if not isinstance(providers_dict, dict):
        return []
    custom_providers: List[Dict[str, Any]] = []
    for key, entry in providers_dict.items():
        if isinstance(entry, dict) and (not is_provider_enabled(entry)):
            continue
        normalized = _normalize_custom_provider_entry(entry, provider_key=str(key))
        if normalized is not None:
            custom_providers.append(normalized)
    return custom_providers

def get_compatible_custom_providers(config: Optional[Dict[str, Any]]=None) -> List[Dict[str, Any]]:
    """Return a deduplicated custom-provider view across legacy and v12+ config.

    ``custom_providers`` remains the on-disk legacy format, while ``providers``
    is the newer keyed schema.  Runtime and picker flows still need a single
    list-shaped view, but we should not materialise that compatibility layer
    back into config.yaml because it duplicates entries in UIs.
    """
    if config is None:
        config = load_config()
    compatible: List[Dict[str, Any]] = []
    seen_provider_keys: set = set()
    seen_name_url_pairs: set = set()

    def _append_if_new(entry: Optional[Dict[str, Any]]) -> None:
        if entry is None:
            return
        provider_key = str(entry.get('provider_key', '') or '').strip().lower()
        name = str(entry.get('name', '') or '').strip().lower()
        base_url = str(entry.get('base_url', '') or '').strip().rstrip('/').lower()
        model = str(entry.get('model', '') or '').strip().lower()
        pair = (name, base_url, model)
        if provider_key and provider_key in seen_provider_keys:
            return
        if name and base_url and (pair in seen_name_url_pairs):
            return
        compatible.append(entry)
        if provider_key:
            seen_provider_keys.add(provider_key)
        if name and base_url:
            seen_name_url_pairs.add(pair)
    custom_providers = config.get('custom_providers')
    if custom_providers is not None:
        if not isinstance(custom_providers, list):
            return []
        for entry in custom_providers:
            _append_if_new(_normalize_custom_provider_entry(entry))
    for entry in providers_dict_to_custom_providers(config.get('providers')):
        _append_if_new(entry)
    return compatible

def _coerce_ssl_verify(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'false', '0', 'no', 'off'}:
            return False
        if lowered in {'true', '1', 'yes', 'on'}:
            return True
    return None

def get_custom_provider_tls_settings(base_url: str, custom_providers: Optional[List[Dict[str, Any]]]=None, config: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Return TLS settings from a matching ``custom_providers`` / ``providers`` entry."""
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            custom_providers = []
    if not base_url or not isinstance(custom_providers, list):
        return {}
    target_url = normalize_route_base_url(base_url)
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get('base_url'))
        if not entry_url or entry_url != target_url:
            continue
        out: Dict[str, Any] = {}
        ca = entry.get('ssl_ca_cert')
        if isinstance(ca, str) and ca.strip():
            out['ssl_ca_cert'] = ca.strip()
        verify = _coerce_ssl_verify(entry.get('ssl_verify'))
        if verify is not None:
            out['ssl_verify'] = verify
        return out
    return {}

def apply_custom_provider_tls_to_client_kwargs(client_kwargs: Dict[str, Any], base_url: str, custom_providers: Optional[List[Dict[str, Any]]]=None, config: Optional[Dict[str, Any]]=None) -> None:
    """Attach per-provider TLS knobs to OpenAI client kwargs when matched."""
    tls = get_custom_provider_tls_settings(base_url, custom_providers, config)
    if tls.get('ssl_ca_cert'):
        client_kwargs['ssl_ca_cert'] = tls['ssl_ca_cert']
    if 'ssl_verify' in tls:
        client_kwargs['ssl_verify'] = tls['ssl_verify']

def normalize_extra_headers(extra_headers: Any) -> Dict[str, str]:
    """Normalize a raw ``extra_headers`` value into a ``dict[str, str]``.

    Stringifies keys and values and drops entries whose value is ``None``.
    Returns ``{}`` for non-dict or empty inputs. This is the single shared
    normalizer for per-provider ``extra_headers`` across config normalization,
    runtime resolution, client construction, and live ``/models`` discovery.

    SECURITY: header values routinely carry credentials (Cloudflare Access
    service tokens, proxy auth, custom bearer schemes). Callers must never
    log the returned values.
    """
    if not isinstance(extra_headers, dict) or not extra_headers:
        return {}
    return {str(k): str(v) for k, v in extra_headers.items() if v is not None}

def get_custom_provider_extra_headers(base_url: str, custom_providers: Optional[List[Dict[str, Any]]]=None, config: Optional[Dict[str, Any]]=None) -> Dict[str, str]:
    """Return ``extra_headers`` from a matching ``providers`` / ``custom_providers`` entry.

    Matches the entry whose normalized route identity equals *base_url*,
    mirroring :func:`get_custom_provider_tls_settings`, and returns its
    ``extra_headers`` dict, or ``{}`` when no entry matches or declares none.

    SECURITY: header values routinely carry credentials (Cloudflare Access
    service tokens, proxy auth, custom bearer schemes). Callers must never
    log the returned values.
    """
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            custom_providers = []
    if not base_url or not isinstance(custom_providers, list):
        return {}
    target_url = normalize_route_base_url(base_url)
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get('base_url'))
        if not entry_url or entry_url != target_url:
            continue
        headers = normalize_extra_headers(entry.get('extra_headers'))
        if headers:
            return headers
    return {}

def apply_custom_provider_extra_headers_to_client_kwargs(client_kwargs: Dict[str, Any], base_url: str, custom_providers: Optional[List[Dict[str, Any]]]=None, config: Optional[Dict[str, Any]]=None) -> None:
    """Merge per-provider ``extra_headers`` onto OpenAI client ``default_headers``.

    Provider-specific headers win over provider/SDK defaults already present in
    ``client_kwargs`` — they are the most specific configuration level. No-op
    when the base_url matches no ``providers`` / ``custom_providers`` entry or
    the entry declares no headers.

    SECURITY: values may carry credentials — never log them.
    """
    extra_headers = get_custom_provider_extra_headers(base_url, custom_providers, config)
    if not extra_headers:
        return
    merged = dict(client_kwargs.get('default_headers') or {})
    merged.update(extra_headers)
    client_kwargs['default_headers'] = merged

def get_custom_provider_context_length(model: str, base_url: str, custom_providers: Optional[List[Dict[str, Any]]]=None, config: Optional[Dict[str, Any]]=None) -> Optional[int]:
    """Look up a per-model ``context_length`` override from ``custom_providers``.

    Matches any entry whose normalized route identity equals ``base_url`` and
    returns ``custom_providers[i].models.<model>.context_length`` if present and
    valid.  Returns ``None`` when no override applies.

    This is the single source of truth for custom-provider context overrides,
    used by:
      * ``AIAgent.__init__`` (startup resolution)
      * ``AIAgent.switch_model`` (mid-session ``/model`` switch)
      * ``hermes_cli.model_switch.resolve_display_context_length`` (``/model`` confirmation display)
      * ``gateway.run._format_session_info`` (``/info`` display)
      * ``agent.model_metadata.get_model_context_length`` (when custom_providers is threaded through)

    Before this helper existed, the lookup was duplicated in ``run_agent.py``'s
    startup path only; every other path (notably ``/model`` switch) fell back
    to the 128K default.  See #15779.
    """
    if not model or not base_url:
        return None
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            if config is None:
                return None
            raw = config.get('custom_providers')
            custom_providers = raw if isinstance(raw, list) else []
    if not isinstance(custom_providers, list):
        return None
    target_url = normalize_route_base_url(base_url)
    if not target_url:
        return None
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get('base_url'))
        if not entry_url or entry_url != target_url:
            continue
        models = entry.get('models')
        if not isinstance(models, dict):
            continue
        model_cfg = models.get(model)
        if not isinstance(model_cfg, dict):
            continue
        raw_ctx = model_cfg.get('context_length')
        if raw_ctx is None:
            continue
        try:
            ctx = int(raw_ctx)
        except (TypeError, ValueError):
            continue
        if ctx > 0:
            return ctx
    return None

def _coerce_config_version(value: Any) -> int:
    """Return a safe integer config version, treating invalid values as legacy."""
    if isinstance(value, bool):
        return 0
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)

def _raw_config_has_explicit_version() -> bool:
    """True when config.yaml exists, parses, and carries a ``_config_version`` key.

    Distinguishes an ANCIENT config (explicit old version → refused by the
    v12 support floor) from a fresh minimal/hand-written/cloned config with
    no version key at all (→ migrated + stamped normally). Missing or
    unparseable files return False so they never trip the floor gate.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return False
    try:
        with open(config_path, encoding='utf-8') as f:
            raw = fast_safe_load(f) or {}
    except Exception:
        return False
    return isinstance(raw, dict) and '_config_version' in raw

def check_config_version() -> Tuple[int, int]:
    """
    Check the raw on-disk config schema version.

    ``load_config()`` deliberately starts from ``DEFAULT_CONFIG`` and deep-merges
    the user's file, which is correct for runtime reads but wrong for deciding
    whether the user's persisted schema has been migrated. A config file with no
    raw ``_config_version`` must remain visible as legacy instead of inheriting
    the latest default version in memory.

    Returns (current_version, latest_version).
    """
    latest = _coerce_config_version(DEFAULT_CONFIG.get('_config_version', 1)) or 1
    config_path = get_config_path()
    if not config_path.exists():
        return (latest, latest)
    try:
        with open(config_path, encoding='utf-8') as f:
            config = fast_safe_load(f) or {}
    except Exception as e:
        _warn_config_parse_failure(config_path, e)
        return (latest, latest)
    if not isinstance(config, dict):
        config = {}
    current = _coerce_config_version(config.get('_config_version'))
    return (current, latest)
_EXTRA_KNOWN_ROOT_KEYS = {'custom_providers', 'fallback_model', 'mcp_servers', 'image_gen', 'video_gen', 'plugins', 'smart_model_routing', 'platform_toolsets', 'known_plugin_toolsets', 'known_builtin_toolsets', 'session_reset', 'group_sessions_per_user', 'thread_sessions_per_user', 'stt_echo_transcripts', 'reset_triggers', 'always_log_local', 'filter_silence_narration', 'multiplex_profiles', 'profile_routes', 'platforms', 'require_mention', 'unauthorized_dm_behavior', 'signal'}
_KNOWN_ROOT_KEYS = frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS
_VALID_CUSTOM_PROVIDER_FIELDS = {'name', 'base_url', 'api_key', 'api_mode', 'model', 'models', 'context_length', 'rate_limit_delay', 'extra_body', 'ssl_ca_cert', 'ssl_verify', 'key_env'}
_CUSTOM_PROVIDER_LIKE_FIELDS = {'base_url', 'api_key', 'rate_limit_delay', 'api_mode'}

@dataclass
class ConfigIssue:
    """A detected config structure problem."""
    severity: str
    message: str
    hint: str

def validate_config_structure(config: Optional[Dict[str, Any]]=None) -> List['ConfigIssue']:
    """Validate config.yaml structure and return a list of detected issues.

    Catches common YAML formatting mistakes that produce confusing runtime
    errors (like "Unknown provider") instead of clear diagnostics.

    Can be called with a pre-loaded config dict, or will load from disk.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return [ConfigIssue('error', 'Could not load config.yaml', "Run 'duck-agent setup' to create a valid config")]
    issues: List[ConfigIssue] = []
    cp = config.get('custom_providers')
    if cp is not None:
        if isinstance(cp, dict):
            issues.append(ConfigIssue('error', "custom_providers is a dict — it must be a YAML list (items prefixed with '-')", 'Change to:\n  custom_providers:\n    - name: my-provider\n      base_url: https://...\n      api_key: ...'))
            cp_keys = set(cp.keys()) if isinstance(cp, dict) else set()
            suspicious = cp_keys & _CUSTOM_PROVIDER_LIKE_FIELDS
            if suspicious:
                issues.append(ConfigIssue('warning', f'Root-level keys {sorted(suspicious)} look like custom_providers entry fields', "These should be indented under a '- name: ...' list entry, not at root level"))
        elif isinstance(cp, list):
            for i, entry in enumerate(cp):
                if not isinstance(entry, dict):
                    issues.append(ConfigIssue('warning', f'custom_providers[{i}] is not a dict (got {type(entry).__name__})', 'Each entry should have at minimum: name, base_url'))
                    continue
                if not entry.get('name'):
                    issues.append(ConfigIssue('warning', f"custom_providers[{i}] is missing 'name' field", 'Add a name, e.g.: name: my-provider'))
                if not entry.get('base_url'):
                    issues.append(ConfigIssue('warning', f"custom_providers[{i}] is missing 'base_url' field", 'Add the API endpoint URL, e.g.: base_url: https://api.example.com/v1'))
    fb = config.get('fallback_model')
    if fb is not None:
        if isinstance(fb, list):
            for i, entry in enumerate(fb):
                if not isinstance(entry, dict):
                    issues.append(ConfigIssue('error', f'fallback_model[{i}] should be a dict, got {type(entry).__name__}', 'Each entry needs provider + model'))
                else:
                    if not entry.get('provider'):
                        issues.append(ConfigIssue('warning', f"fallback_model[{i}] is missing 'provider' field", 'Add: provider: openrouter (or another provider)'))
                    if not entry.get('model'):
                        issues.append(ConfigIssue('warning', f"fallback_model[{i}] is missing 'model' field", 'Add: model: <model-name>'))
        elif not isinstance(fb, dict):
            issues.append(ConfigIssue('error', f"fallback_model should be a dict with 'provider' and 'model', got {type(fb).__name__}", 'Change to:\n  fallback_model:\n    provider: openrouter\n    model: anthropic/claude-sonnet-4'))
        elif fb:
            if not fb.get('provider'):
                issues.append(ConfigIssue('warning', "fallback_model is missing 'provider' field — fallback will be disabled", 'Add: provider: openrouter (or another provider)'))
            if not fb.get('model'):
                issues.append(ConfigIssue('warning', "fallback_model is missing 'model' field — fallback will be disabled", 'Add: model: anthropic/claude-sonnet-4 (or another model)'))
    if isinstance(cp, dict) and 'fallback_model' not in config and ('fallback_model' in (cp or {})):
        issues.append(ConfigIssue('error', 'fallback_model appears inside custom_providers instead of at root level', 'Move fallback_model to the top level of config.yaml (no indentation)'))
    model_cfg = config.get('model')
    if cp and (not model_cfg):
        issues.append(ConfigIssue('warning', "custom_providers defined but no 'model' section — Duck Agent won't know which provider to use", 'Add a model section:\n  model:\n    provider: custom\n    default: your-model-name\n    base_url: https://...'))
    for key in config:
        if key.startswith('_'):
            continue
        if key not in _KNOWN_ROOT_KEYS and key in _CUSTOM_PROVIDER_LIKE_FIELDS:
            issues.append(ConfigIssue('warning', f"Root-level key '{key}' looks misplaced — should it be under 'model:' or inside a 'custom_providers' entry?", f"Move '{key}' under the appropriate section"))
    return issues

def print_config_warnings(config: Optional[Dict[str, Any]]=None) -> None:
    """Print config structure warnings to stderr at startup.

    Called early in CLI and gateway init so users see problems before
    they hit cryptic "Unknown provider" errors.  Prints nothing if
    config is healthy.
    """
    try:
        issues = validate_config_structure(config)
    except Exception:
        return
    if not issues:
        return
    lines = ['\x1b[33m⚠ Config issues detected in config.yaml:\x1b[0m']
    for ci in issues:
        marker = '\x1b[31m✗\x1b[0m' if ci.severity == 'error' else '\x1b[33m⚠\x1b[0m'
        lines.append(f'  {marker} {ci.message}')
    lines.append("  \x1b[2mRun 'duck-agent doctor' for fix suggestions.\x1b[0m")
    sys.stderr.write('\n'.join(lines) + '\n\n')

def warn_deprecated_cwd_env_vars(config: Optional[Dict[str, Any]]=None) -> None:
    """Warn if MESSAGING_CWD or TERMINAL_CWD is set in .env instead of config.yaml.

    These env vars are deprecated — the canonical setting is terminal.cwd
    in config.yaml.  Prints a migration hint to stderr.
    """
    messaging_cwd = os.environ.get('MESSAGING_CWD')
    terminal_cwd_env = os.environ.get('TERMINAL_CWD')
    if config is None:
        try:
            config = load_config()
        except Exception:
            return
    terminal_cfg = config.get('terminal', {})
    config_cwd = terminal_cfg.get('cwd', '.') if isinstance(terminal_cfg, dict) else '.'
    config_has_explicit_cwd = config_cwd not in {'.', 'auto', 'cwd', ''}
    lines: list[str] = []
    if messaging_cwd:
        lines.append(f'  \x1b[33m⚠\x1b[0m MESSAGING_CWD={messaging_cwd} found in .env — this is deprecated.')
    if terminal_cwd_env and (not config_has_explicit_cwd):
        lines.append(f'  \x1b[33m⚠\x1b[0m TERMINAL_CWD={terminal_cwd_env} found in .env — this is deprecated.')
    if lines:
        from hermes_constants import display_hermes_home
        hint_path = display_hermes_home()
        lines.insert(0, '\x1b[33m⚠ Deprecated .env settings detected:\x1b[0m')
        lines.append('  \x1b[2mMove to config.yaml instead:  terminal:\\n    cwd: /your/project/path\x1b[0m')
        lines.append(f'  \x1b[2mThen remove the old entries from {hint_path}/.env\x1b[0m')
        sys.stderr.write('\n'.join(lines) + '\n\n')

def _persist_migration(config: Dict[str, Any]) -> None:
    """Persist a migrated config under the migration write invariant.

    THE INVARIANT (single source of truth for the whole migration pipeline):
    a migration may only persist values that DIFFER from the current schema
    default, plus explicit removals/renames of user data. Pure schema defaults
    are never materialised to disk — ``load_config()``'s deep-merge supplies
    them at read time, so writing them adds nothing and actively shadows future
    default changes (see ``save_config``'s docstring). Materialising defaults on
    every version bump is what rewrote hand-curated configs into full
    DEFAULT_CONFIG dumps (the "duck-agent update / duck-agent -p blows up my config"
    reports).

    Every migration step MUST route its write through this helper instead of
    calling ``save_config`` directly. It is a thin wrapper over
    ``save_config(config)`` (default-stripping ON, no ``merge_existing``);
    centralising the call makes the invariant impossible to regress one
    migration at a time. Callers must pass the full raw config returned by
    ``read_raw_config()`` after in-place mutations (including key removals);
    deep-merging the on-disk file back in would resurrect keys the migration
    just deleted. Partial-save preservation for unrelated top-level sections
    belongs on ``save_config(..., merge_existing=True)``, not here.
    """
    save_config(config)

def migrate_config(interactive: bool=True, quiet: bool=False) -> Dict[str, Any]:
    """
    Migrate config to latest version, prompting for new required fields.
    
    Args:
        interactive: If True, prompt user for missing values
        quiet: If True, suppress output
        
    Returns:
        Dict with migration results: {"env_added": [...], "config_added": [...], "warnings": [...]}
    """
    results = {'env_added': [], 'config_added': [], 'warnings': []}
    try:
        fixes = sanitize_env_file()
        if fixes and (not quiet):
            print(f'  ✓ Normalized .env line formatting ({fixes} line(s) changed)')
    except Exception:
        pass
    current_ver, latest_ver = check_config_version()
    from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION, run_migrations, support_floor_message
    _explicit_version = _raw_config_has_explicit_version()
    floor_refused = _explicit_version and current_ver < SUPPORT_FLOOR_VERSION and (current_ver < latest_ver)
    if floor_refused:
        msg = support_floor_message()
        results['warnings'].append(msg)
        sys.stderr.write(f'⚠ duck-agent config: {msg}\n')
        if not quiet:
            print(f'  ⚠ {msg}')
    else:
        run_migrations(current_ver, results, quiet)
    config = read_raw_config()
    raw_mcp_servers = config.get('mcp_servers')
    if isinstance(raw_mcp_servers, dict):
        try:
            from hermes_cli.mcp_security import validate_mcp_server_entry as _validate_mcp_server_entry
        except Exception:
            _validate_mcp_server_entry = None
        if _validate_mcp_server_entry:
            mcp_touched = False
            for server_name, entry in raw_mcp_servers.items():
                if not isinstance(entry, dict):
                    continue
                issues = _validate_mcp_server_entry(server_name, entry)
                if not issues:
                    continue
                entry['enabled'] = False
                mcp_touched = True
                results['warnings'].append(f"Disabled suspicious MCP server '{server_name}'")
                if not quiet:
                    for issue in issues:
                        print(f'  ⚠ {issue}')
                    print(f"  ⚠ Disabled MCP server '{server_name}' pending review")
            if mcp_touched:
                config['mcp_servers'] = raw_mcp_servers
                _persist_migration(config)
    try:
        from toolsets import validate_toolset
        from hermes_cli.toolset_validation import validate_platform_toolsets
        ts_warnings = validate_platform_toolsets(read_raw_config().get('platform_toolsets'), validate_toolset)
        for w in ts_warnings:
            results['warnings'].append(w)
            if not quiet:
                print(f'  ⚠ {w}')
    except Exception as _ts_val_err:
        logger.debug('platform_toolsets validation skipped: %s', _ts_val_err)
    if current_ver < latest_ver and (not quiet) and (not floor_refused):
        print(f'Config version: {current_ver} → {latest_ver}')
    missing_env = get_missing_env_vars(required_only=True)
    if missing_env and (not quiet):
        print('\n⚠️  Missing required environment variables:')
        for var in missing_env:
            print(f"   • {var['name']}: {var['description']}")
    if interactive and missing_env:
        print("\nLet's configure them now:\n")
        for var in missing_env:
            if var.get('url'):
                print(f"  Get your key at: {var['url']}")
            if var.get('password'):
                value = masked_secret_prompt(f"  {var['prompt']}: ")
            else:
                value = input(f"  {var['prompt']}: ").strip()
            if value:
                save_env_value(var['name'], value)
                results['env_added'].append(var['name'])
                print(f"  ✓ Saved {var['name']}")
            else:
                results['warnings'].append(f"Skipped {var['name']} - some features may not work")
            print()
    missing_optional = get_missing_env_vars(required_only=False)
    required_names = {v['name'] for v in missing_env} if missing_env else set()
    missing_optional = [v for v in missing_optional if v['name'] not in required_names and (not v.get('advanced'))]
    new_var_names = set()
    for ver in range(current_ver + 1, latest_ver + 1):
        new_var_names.update(ENV_VARS_BY_VERSION.get(ver, []))
    if new_var_names and interactive and (not quiet):
        new_and_unset = [(name, OPTIONAL_ENV_VARS[name]) for name in sorted(new_var_names) if not get_env_value(name) and name in OPTIONAL_ENV_VARS]
        if new_and_unset:
            print(f'\n  {len(new_and_unset)} new optional key(s) in this update:')
            for name, info in new_and_unset:
                print(f"    • {name} — {info.get('description', '')}")
            print()
            try:
                answer = input('  Configure new keys? [y/N]: ').strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = 'n'
            if answer in {'y', 'yes'}:
                print()
                for name, info in new_and_unset:
                    if info.get('url'):
                        print(f"  {info.get('description', name)}")
                        print(f"  Get your key at: {info['url']}")
                    else:
                        print(f"  {info.get('description', name)}")
                    if info.get('password'):
                        value = masked_secret_prompt(f"  {info.get('prompt', name)} (Enter to skip): ")
                    else:
                        value = input(f"  {info.get('prompt', name)} (Enter to skip): ").strip()
                    if value:
                        save_env_value(name, value)
                        results['env_added'].append(name)
                        print(f'  ✓ Saved {name}')
                    print()
            else:
                print('  Set later with: duck-agent config set <key> <value>')
    missing_config = get_missing_config_fields()
    if missing_config:
        results['config_added'].extend((field['key'] for field in missing_config))
    if current_ver < latest_ver and (not floor_refused):
        config = read_raw_config()
        config['_config_version'] = latest_ver
        _persist_migration(config)
    missing_skill_config = get_missing_skill_config_vars()
    if missing_skill_config and interactive and (not quiet):
        print(f'\n  {len(missing_skill_config)} skill setting(s) not configured:')
        for var in missing_skill_config:
            skill_name = var.get('skill', 'unknown')
            print(f"    • {var['key']} — {var['description']} (from skill: {skill_name})")
        print()
        try:
            answer = input('  Configure skill settings? [y/N]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = 'n'
        if answer in {'y', 'yes'}:
            print()
            config = read_raw_config()
            try:
                from agent.skill_utils import SKILL_CONFIG_PREFIX
            except Exception:
                SKILL_CONFIG_PREFIX = 'skills.config'
            for var in missing_skill_config:
                default = var.get('default', '')
                default_hint = f' (default: {default})' if default else ''
                value = input(f"  {var['prompt']}{default_hint}: ").strip()
                if not value and default:
                    value = str(default)
                if value:
                    storage_key = f"{SKILL_CONFIG_PREFIX}.{var['key']}"
                    _set_nested(config, storage_key, value)
                    results['config_added'].append(var['key'])
                    print(f"  ✓ Saved {var['key']} = {value}")
                else:
                    results['warnings'].append(f"Skipped {var['key']} — skill '{var.get('skill', '?')}' may ask for it later")
                print()
            _persist_migration(config)
        else:
            print('  Set later with: duck-agent config set <key> <value>')
    return results

def _merge_partial_save(raw: dict, override: dict) -> dict:
    """Merge *override* over *raw* for partial ``save_config`` writes.

    Top-level sections omitted from *override* are preserved from *raw*.
    Shared top-level dict sections are deep-merged so a caller can update one
    nested key without dropping sibling keys from disk. Intentional key
    removals within a section are not supported here — migration writes must
    route through ``_persist_migration`` with a full ``read_raw_config()`` dict
    instead.
    """
    result = copy.deepcopy(override)
    for key, value in raw.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(value, result[key])
    return result

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, preserving nested defaults.

    Keys in *override* take precedence. If both values are dicts the merge
    recurses, so a user who overrides only ``tts.elevenlabs.voice_id`` will
    keep the default ``tts.elevenlabs.model_id`` intact.

    An empty section key in config.yaml (``terminal:`` with no value) parses
    as YAML ``None``; treating that as an override would replace the entire
    default dict with ``None`` and crash every downstream consumer that
    expects a mapping (#58277). A ``None`` override of a dict default is
    ignored — same as the key being absent.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], dict) and (value is None):
            continue
        else:
            result[key] = value
    return result

def _strip_dotted_keys(cfg: dict, dotted_keys: set) -> Tuple[dict, set]:
    """Remove the given dotted leaf keys from a nested config dict.

    Returns ``(pruned_cfg, set_of_stripped_keys_that_were_present)``. Used by
    ``save_config`` to drop managed-scope leaves before persisting, so a bulk
    write never writes a user value that would lose to the managed layer on the
    next load. Only keys actually present in ``cfg`` are reported as stripped.
    """
    stripped: set = set()
    for dotted in dotted_keys:
        parts = dotted.split('.')
        node = cfg
        for p in parts[:-1]:
            if not isinstance(node, dict) or p not in node:
                node = None
                break
            node = node[p]
        if isinstance(node, dict) and parts[-1] in node:
            del node[parts[-1]]
            stripped.add(dotted)
    return (cfg, stripped)

def _env_expand_match(m: re.Match) -> str:
    """Expand one ``${...}`` config reference.

    Two accepted shapes, matching what MCP server config already resolves
    (``tools/mcp_tool.py::_env_ref_name``):

    * ``${VAR}`` — legacy bare name, resolved via ``os.environ``.
    * ``${env:VAR}`` — Cursor-style SecretRef, same resolution after the
      ``env:`` prefix is stripped.  Before this, the prefixed form worked in
      MCP config but stayed a literal string in config.yaml — a confusing
      half-support.

    Other SecretRef sources (``file:``, ``bitwarden:``, ``vault:``, ...)
    are NOT resolved here — external secret backends inject their values
    into the environment at startup (the ``secrets:`` block), so a config
    ref only ever needs the env shape.  Unknown prefixes warn once and stay
    verbatim so callers can detect them.
    """
    raw = m.group(0)
    inner = m.group(1).strip()
    if inner.startswith('env:'):
        name = inner[len('env:'):].strip()
        if not name:
            return raw
        val = os.environ.get(name)
        if val is not None:
            return val
        logger.warning('Config ref %r: %s is not set (check ~/.duck-agent/.env); keeping the literal placeholder', raw, name)
        return raw
    if ':' in inner and re.match('^[a-z][a-z0-9_-]*:', inner):
        logger.warning('Config ref %r uses source %r which is not resolvable in config.yaml — external secret sources inject env vars at startup, so reference the variable as ${env:NAME} instead', raw, inner.split(':', 1)[0])
        return raw
    return os.environ.get(inner, raw)

def _env_ref_var_name(ref: str) -> Optional[str]:
    """Normalize a ``${...}`` body to the env-var name it reads, or None
    when the ref uses a non-env source and never touches the environment."""
    ref = ref.strip()
    if ref.startswith('env:'):
        name = ref[len('env:'):].strip()
        return name or None
    if ':' in ref and re.match('^[a-z][a-z0-9_-]*:', ref):
        return None
    return ref

def _expand_env_vars(obj):
    """Recursively expand ``${VAR}`` / ``${env:VAR}`` references in config
    values.

    Only string values are processed; dict keys, numbers, booleans, and
    None are left untouched.  Unresolved references (variable not in
    ``os.environ``) are kept verbatim so callers can detect them.
    """
    if isinstance(obj, str):
        return re.sub('\\${([^}]+)}', _env_expand_match, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj

def _env_ref_snapshot(obj, snapshot=None):
    """Map every ``${VAR}`` / ``${env:VAR}`` name referenced in config values
    to its current ``os.environ`` value (``None`` when unset).

    Stored alongside cached ``load_config()`` results so a cache hit can
    detect that the cached expansion was made against a *different*
    environment — e.g. a ``load_config()`` that ran before
    ``load_hermes_dotenv()`` populated the process env, or an env var
    rotated in-process after the first load. File mtime/size alone cannot
    see either case (#58514).

    ``${env:VAR}`` refs are tracked under the real variable name; refs
    with a non-env source prefix never read the environment, so they are
    excluded from the snapshot.
    """
    if snapshot is None:
        snapshot = {}
    if isinstance(obj, str):
        for raw in re.findall('\\${([^}]+)}', obj):
            name = _env_ref_var_name(raw)
            if name is not None:
                snapshot[name] = os.environ.get(name)
    elif isinstance(obj, dict):
        for value in obj.values():
            _env_ref_snapshot(value, snapshot)
    elif isinstance(obj, list):
        for item in obj:
            _env_ref_snapshot(item, snapshot)
    return snapshot

def _items_by_unique_name(items):
    """Return a name-indexed dict only when all items have unique string names."""
    if not isinstance(items, list):
        return None
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            return None
        name = item['name']
        if name in indexed:
            return None
        indexed[name] = item
    return indexed

def _preserve_env_ref_templates(current, raw, loaded_expanded=None):
    """Restore raw ``${VAR}`` templates when a value is otherwise unchanged.

    ``load_config()`` expands env refs for runtime use. When a caller later
    persists that config after modifying some unrelated setting, keep the
    original on-disk template instead of writing the expanded plaintext
    secret back to ``config.yaml``.

    Prefer preserving the raw template when ``current`` still matches either
    the value previously returned by ``load_config()`` for this config path or
    the current environment expansion of ``raw``. This handles env-var
    rotation between load and save while still treating mixed literal/template
    string edits as caller-owned once their rendered value diverges.
    """
    if isinstance(current, str) and isinstance(raw, str) and re.search('\\${[^}]+}', raw):
        if current == raw:
            return raw
        if isinstance(loaded_expanded, str) and current == loaded_expanded:
            return raw
        if _expand_env_vars(raw) == current:
            return raw
        return current
    if isinstance(current, dict) and isinstance(raw, dict):
        return {key: _preserve_env_ref_templates(value, raw.get(key), loaded_expanded.get(key) if isinstance(loaded_expanded, dict) else None) for key, value in current.items()}
    if isinstance(current, list) and isinstance(raw, list):
        current_by_name = _items_by_unique_name(current)
        raw_by_name = _items_by_unique_name(raw)
        loaded_by_name = _items_by_unique_name(loaded_expanded)
        if current_by_name is not None and raw_by_name is not None:
            return [_preserve_env_ref_templates(item, raw_by_name.get(item.get('name')), loaded_by_name.get(item.get('name')) if loaded_by_name is not None else None) for item in current]
        return [_preserve_env_ref_templates(item, raw[index] if index < len(raw) else None, loaded_expanded[index] if isinstance(loaded_expanded, list) and index < len(loaded_expanded) else None) for index, item in enumerate(current)]
    return current

def _explicit_config_paths(config: Dict[str, Any]) -> Set[Tuple[str, ...]]:
    """Return leaf paths explicitly present in a raw config dict.

    Computed on the **raw** (un-normalized, un-expanded) config so that
    values injected by normalisation (e.g. ``agent.max_turns`` from
    ``DEFAULT_CONFIG``) are not mistakenly treated as user-set.

    Used by ``save_config`` to build the *preserve* set passed to
    ``_strip_default_values`` so only user-authored keys survive the
    defaults-strip pass.
    """
    paths: Set[Tuple[str, ...]] = set()

    def _walk(value: Any, path: Tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                _walk(child, path + (key,))
            return
        if path:
            paths.add(path)
    _walk(config, ())
    return paths

def _strip_default_values(config: Dict[str, Any], defaults: Dict[str, Any]=DEFAULT_CONFIG, preserve_keys: Optional[Set[Tuple[str, ...]]]=None) -> Dict[str, Any]:
    """Return *config* without keys whose values match *defaults*.

    Keys in *preserve_keys* (explicitly present in the user's raw config,
    before any normalisation) are always kept even when they equal the
    default, so user-set values such as ``memory.user_char_limit: 2200``
    survive a ``save_config`` round-trip.

    Nested dicts whose every child is stripped are removed entirely so
    default-only subtrees (e.g. ``gateway``) never bloat ``config.yaml``
    when the user has nothing to say about them.
    """
    preserve_keys = {('_config_version',)} | set(preserve_keys or ())

    def _strip(value: Any, default: Any, path: Tuple[str, ...]) -> Any:
        if path in preserve_keys:
            return copy.deepcopy(value)
        if isinstance(value, dict) and value:
            default_dict = default if isinstance(default, dict) else {}
            stripped: Dict[str, Any] = {}
            for key, child in value.items():
                child_default = default_dict.get(key)
                stripped_child = _strip(child, child_default, path + (key,))
                if stripped_child is not None:
                    stripped[key] = stripped_child
            if stripped:
                return stripped
            return None
        if value == default:
            return None
        return copy.deepcopy(value)
    result: Dict[str, Any] = {}
    for key, value in config.items():
        stripped = _strip(value, defaults.get(key), (key,))
        if stripped is not None:
            result[key] = stripped
    return result

def _normalize_root_model_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move stale root-level provider/base_url/context_length into model section.

    Some users (or older code) placed ``provider:``, ``base_url:``, or
    ``context_length:`` at the config root instead of inside ``model:``.
    These root-level keys are only used as a fallback when the corresponding
    ``model.*`` key is empty — they never override an existing value.
    After migration the root-level keys are removed so they can't cause
    confusion on subsequent loads.

    Also aliases ``api_base`` → ``base_url`` (issue #8919). ``api_base`` is the
    intuitive name OpenAI-SDK / LiteLLM users reach for, and ``duck-agent config set``
    blindly accepts any dotted key — so ``model.api_base`` got written, confirmed,
    and then silently ignored by the runtime resolver (which reads only
    ``model.base_url``), causing requests to fall back to OpenRouter. We migrate
    the alias to the canonical key (fallback-only — never override an explicit
    ``base_url``) and drop the alias so it can't confuse later loads.

    Finally, canonicalizes the model-id key to ``model.default`` (issue #34500).
    The runtime resolver and ~14 other readers select the chat model via
    ``model.default``; ``model.model`` was already aliased inline at some sites
    but ``model.name`` was not, so a custom-provider config like
    ``model: {name: <id>, provider: <custom>}`` resolved to an empty model and
    the API request went out with ``model=`` (HTTP 400 from OpenAI-compatible
    backends) — while display paths (``duck-agent status``/``dump``) read ``name``
    and *showed* the model, making the failure silent. Normalizing here (the
    single load/save chokepoint) means every reader, present and future, sees a
    populated ``default`` and the stale alias is migrated out of config.yaml on
    the next save. Precedence: ``default`` > ``model`` > ``name`` (never
    overrides an explicit ``default``, so existing configs are unaffected).
    """
    model_in = config.get('model')
    model_has_alias = isinstance(model_in, dict) and model_in.get('api_base')
    model_needs_canon = isinstance(model_in, dict) and (model_in.get('model') or model_in.get('name'))
    has_root = any((config.get(k) for k in ('provider', 'base_url', 'context_length', 'api_base')))
    if not has_root and (not model_has_alias) and (not model_needs_canon):
        return config
    config = dict(config)
    model = config.get('model')
    if not isinstance(model, dict):
        model = {'default': model} if model else {}
    else:
        model = dict(model)
    config['model'] = model
    for key in ('provider', 'base_url', 'context_length'):
        root_val = config.get(key)
        if root_val and (not model.get(key)):
            model[key] = root_val
        config.pop(key, None)
    for alias_val in (config.get('api_base'), model.get('api_base')):
        if alias_val and (not model.get('base_url')):
            model['base_url'] = alias_val
    config.pop('api_base', None)
    model.pop('api_base', None)
    if not (model.get('default') or ''):
        alias = model.get('model') or model.get('name')
        if alias:
            model['default'] = alias
    if model.get('default'):
        model.pop('model', None)
        model.pop('name', None)
    return config

def _normalize_max_turns_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize legacy root-level max_turns into agent.max_turns.

    Only injects the schema default when the user actually set max_turns
    somewhere (root level or under ``agent``).  A bare ``load_config()``
    call that passes the result straight to ``save_config()`` should not
    materialise ``agent.max_turns`` in config.yaml when the user never set
    it — that makes the default sticky and blocks future schema changes.
    """
    config = dict(config)
    agent_config = dict(config.get('agent') or {})
    had_root = 'max_turns' in config
    had_agent = 'max_turns' in agent_config
    if had_root and (not had_agent):
        agent_config['max_turns'] = config['max_turns']
    if not had_root and (not had_agent):
        pass
    elif 'max_turns' not in agent_config:
        agent_config['max_turns'] = DEFAULT_CONFIG['agent']['max_turns']
    config['agent'] = agent_config
    config.pop('max_turns', None)
    return config

def is_provider_enabled(provider_cfg: Optional[Dict[str, Any]]) -> bool:
    """Return whether a ``providers.<name>`` config block is enabled.

    A provider is enabled by default. Only an explicit ``enabled: false`` in
    the block hides it from the model picker, ``/models`` listings, the
    runtime resolver and the doctor / status output.

    Backward-compat: configs without the ``enabled`` key keep working as
    before — the default is ``True``.

    Pass any non-dict (None, list, string) and you get ``True`` too, so
    malformed entries don't disappear silently; they'll still be flagged
    by the existing validation paths.
    """
    if not isinstance(provider_cfg, dict):
        return True
    flag = provider_cfg.get('enabled', True)
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return flag.strip().lower() not in {'false', '0', 'no', 'off'}
    return bool(flag)

def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any=None) -> Any:
    """Traverse nested dict keys safely, returning ``default`` on any miss.

    Canonical helper for the ``cfg.get("X", {}).get("Y", default)`` pattern
    that appears 50+ times across the codebase. Handles three common gotchas
    in one place:

      1. Missing intermediate keys (returns ``default``, no KeyError).
      2. An intermediate value that's not a dict (e.g. a user wrote a string
         where a section was expected). Returns ``default`` instead of
         AttributeError on ``.get()``.
      3. ``cfg is None`` (callers sometimes pass ``load_config() or None``).

    Named ``cfg_get`` rather than ``cfg_path`` to avoid shadowing the
    ubiquitous ``cfg_path = _hermes_home / "config.yaml"`` local variable
    that appears in gateway/run.py, cron/scheduler.py, main.py, etc.

    Explicit ``None`` values are returned as-is (matches ``dict.get(key,
    default)`` semantics — ``default`` is only returned when the key is
    *absent*, not when it's present but set to ``None``).

    Examples:
        >>> cfg_get({"agent": {"reasoning_effort": "high"}}, "agent", "reasoning_effort")
        'high'
        >>> cfg_get({}, "agent", "reasoning_effort", default="medium")
        'medium'
        >>> cfg_get({"agent": "oops_a_string"}, "agent", "reasoning_effort", default="low")
        'low'
        >>> cfg_get(None, "anything", default=42)
        42
        >>> cfg_get({"a": {"b": None}}, "a", "b", default="def")  # explicit None preserved
        >>> cfg_get({"a": {"b": False}}, "a", "b", default=True)  # falsy values preserved
        False
    """
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        if key not in node:
            return default
        node = node[key]
    return node

def read_raw_config() -> Dict[str, Any]:
    """Read ~/.duck-agent/config.yaml as-is, without merging defaults or migrating.

    Returns the raw YAML dict, or ``{}`` if the file doesn't exist or can't
    be parsed.  Use this for lightweight config reads where you just need a
    single value and don't want the overhead of ``load_config()``'s deep-merge
    + migration pipeline.

    Cached on the config file's (mtime_ns, size) — same strategy as
    ``load_config()``. Returns a deepcopy on every call since some callers
    mutate the result before passing to ``save_config()``.
    """
    with _CONFIG_LOCK:
        try:
            config_path = get_config_path()
            st = config_path.stat()
            cache_key = (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return {}
        path_key = str(config_path)
        cached = _RAW_CONFIG_CACHE.get(path_key)
        if cached is not None and cached[:2] == cache_key:
            return copy.deepcopy(cached[2])
        try:
            with open(config_path, encoding='utf-8') as f:
                data = fast_safe_load(f) or {}
        except Exception as e:
            _warn_config_parse_failure(config_path, e)
            return {}
        if not isinstance(data, dict):
            data = {}
        _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], copy.deepcopy(data))
        return data

def read_user_config_raw(config_path: Optional[Path]=None) -> Dict[str, Any]:
    """Read a user ``config.yaml`` EXACTLY as written on disk.

    No DEFAULT_CONFIG merge, no managed-scope overlay, no ``${ENV_VAR}``
    expansion, no migration, no root-model normalization, no caching.

    ONLY legal for write-back round-trips and raw-file diagnostics —
    behavioral reads must use load_config()/load_config_readonly().

    Legal call sites, exhaustively:

      * WRITE-BACK ROUND-TRIPS (read → mutate one key → save): merging
        defaults or the managed overlay here would persist hundreds of
        default keys (or administrator-pinned values) into the user's file
        on the next save. Raw is *correct*, not an optimization.
      * RAW-FILE DIAGNOSTICS (doctor, deprecation sweeps): these inspect
        what the user actually wrote — stale root keys, drift against .env —
        and merged defaults would produce false positives.
      * PRESENCE-SENSITIVE ENV BRIDGES (gateway/send bridges that only
        export a key when the user explicitly set it): a defaults merge
        would make every key "present" and bridge the entire DEFAULT_CONFIG
        into the environment. These sites must still apply
        ``managed_scope.apply_managed_overlay`` + ``_expand_env_vars``
        inline, which they do.

    Semantics (deliberately mirrors the bare ``open()+yaml.safe_load()``
    pattern this replaces, so migrated sites keep their exact failure
    behavior):

      * missing file → ``{}``
      * unparseable YAML / other I/O errors → raises (callers that want
        fail-open already wrap in try/except; callers with last-known-good
        or warn semantics rely on the exception)
      * non-dict YAML root → ``{}``

    ``config_path`` defaults to :func:`get_config_path` (profile-aware).
    Pass an explicit path when the caller resolves its own home (gateway
    ``_hermes_home``, tui profile override, multi-profile probes).
    """
    if config_path is None:
        config_path = get_config_path()
    try:
        with open(config_path, encoding='utf-8') as f:
            data = fast_safe_load(f) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}

def read_raw_config_readonly() -> Dict[str, Any]:
    """Fast-path variant of ``read_raw_config()`` for callers that ONLY READ.

    Returns the cached raw-config dict directly, skipping the per-call
    ``copy.deepcopy`` that ``read_raw_config()`` performs (needed there
    because some callers mutate the result before ``save_config``).

    **Mutating the returned dict corrupts the in-process cache for every
    subsequent caller.** Only use on read-only paths — e.g. per-turn policy
    checks like the shared-metrics gate, which runs 2-3x per agent turn and
    was paying a full config deepcopy each time.

    Same (mtime_ns, size) freshness key as ``read_raw_config()`` — an edited
    config.yaml is picked up on the next call.
    """
    with _CONFIG_LOCK:
        try:
            config_path = get_config_path()
            st = config_path.stat()
            cache_key = (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return {}
        path_key = str(config_path)
        cached = _RAW_CONFIG_CACHE.get(path_key)
        if cached is not None and cached[:2] == cache_key:
            return cached[2]
        try:
            with open(config_path, encoding='utf-8') as f:
                data = fast_safe_load(f) or {}
        except Exception as e:
            _warn_config_parse_failure(config_path, e)
            return {}
        if not isinstance(data, dict):
            data = {}
        cached_copy = copy.deepcopy(data)
        _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], cached_copy)
        return cached_copy

def require_readable_config_before_write(config_path: Optional[Path]=None) -> None:
    """Refuse to replace an existing config.yaml that cannot be read."""
    if config_path is None:
        config_path = get_config_path()
    try:
        config_path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f'Refusing to overwrite {config_path}: existing config.yaml cannot be accessed ({exc}). Fix the file permissions or move it aside first.') from exc
    try:
        with open(config_path, 'rb') as f:
            f.read(1)
    except OSError as exc:
        raise RuntimeError(f'Refusing to overwrite {config_path}: existing config.yaml cannot be read ({exc}). Fix the file permissions or move it aside first.') from exc

def atomic_config_write(config_path: Path, data: Any, **kwargs: Any) -> None:
    """Fail-closed atomic write for ``config.yaml``.

    The single chokepoint every config-update path should use instead of
    calling :func:`utils.atomic_yaml_write` directly. It runs
    :func:`require_readable_config_before_write` first, so a full-file
    replacement can never silently clobber an existing ``config.yaml`` that
    degraded to an empty dict on read (permission error, broken mount,
    transient I/O). New-file creation still works when the path is absent.

    Root cause this guards: ``read_raw_config()`` returns ``{}`` for BOTH an
    absent file and an unreadable-but-present file. Callers that read then
    overwrite can't tell the two apart, so an unreadable config would be
    replaced with only defaults or the single edited section. Routing every
    write through this helper enforces the invariant in one place rather than
    relying on each of ~15 independent write sites to remember the guard.

    ``kwargs`` are forwarded verbatim to ``atomic_yaml_write``
    (``sort_keys``, ``default_flow_style``, ``extra_content``, ...).
    """
    from utils import atomic_yaml_write
    require_readable_config_before_write(config_path)
    atomic_yaml_write(config_path, data, **kwargs)

def load_config() -> Dict[str, Any]:
    """Load configuration from ~/.duck-agent/config.yaml.

    Cached on the config file's (mtime_ns, size). Returns a deepcopy of
    the cached value when unchanged, since most call sites mutate the
    result (e.g. ``cfg["model"]["default"] = ...`` before ``save_config``).
    The cache is keyed on ``str(config_path)`` so profile switches
    (which change ``DUCK_AGENT_HOME`` and therefore ``get_config_path()``)
    don't collide.

    Read-only callers should use ``load_config_readonly()`` to skip the
    defensive deepcopy — that path matters in agent-loop hot spots like
    ``get_provider_request_timeout`` which is called once per API turn.
    """
    return _load_config_impl(want_deepcopy=True)

def load_config_readonly() -> Dict[str, Any]:
    """Fast-path variant of ``load_config()`` for callers that ONLY READ.

    Returns the cached config dict directly without the defensive deepcopy
    that ``load_config()`` applies. **Mutating the returned dict (or any
    nested structure) corrupts the in-process cache for every subsequent
    caller** — only use this when you are absolutely sure your code path
    will not write to the result. If you need to mutate or pass to
    ``save_config``, call ``load_config()`` instead.

    Why this exists: ``load_config()`` cache-hit cost is ~265us per call,
    half of which (~135us) is the defensive deepcopy. The agent loop calls
    into config reads (timeouts, thresholds, feature flags) ~20-50x per
    conversation; skipping deepcopy here removes a measurable allocation
    source and the GC pressure that comes with it.

    Note: this returns a plain ``dict`` (not ``MappingProxyType``) so
    existing ``isinstance(x, dict)`` guards downstream keep working. The
    safety guarantee is purely documented, not enforced — be careful.
    """
    return _load_config_impl(want_deepcopy=False)

def write_platform_config_field(platform_key: str, field_key: str, value: Any, *, raw: bool=False) -> None:
    """Persist one scalar field under ``platforms.<platform_key>``.

    ``raw=True`` preserves CLI setup flows that intentionally edit only the
    user's raw config file. Dashboard routes use the default loaded-config path
    so they retain their existing profile-scoped ``load_config`` behavior.
    """
    config = read_raw_config() if raw else load_config()
    platforms = config.setdefault('platforms', {})
    if not isinstance(platforms, dict):
        platforms = {}
        config['platforms'] = platforms
    platform_config = platforms.setdefault(platform_key, {})
    if not isinstance(platform_config, dict):
        platform_config = {}
        platforms[platform_key] = platform_config
    platform_config[field_key] = value
    save_config(config)
TERMINAL_CONFIG_ENV_MAP = {'backend': 'TERMINAL_ENV', 'modal_mode': 'TERMINAL_MODAL_MODE', 'degraded_mode': 'TERMINAL_DEGRADED_MODE', 'cwd': 'TERMINAL_CWD', 'timeout': 'TERMINAL_TIMEOUT', 'lifetime_seconds': 'TERMINAL_LIFETIME_SECONDS', 'docker_image': 'TERMINAL_DOCKER_IMAGE', 'docker_forward_env': 'TERMINAL_DOCKER_FORWARD_ENV', 'singularity_image': 'TERMINAL_SINGULARITY_IMAGE', 'modal_image': 'TERMINAL_MODAL_IMAGE', 'daytona_image': 'TERMINAL_DAYTONA_IMAGE', 'vercel_runtime': 'TERMINAL_VERCEL_RUNTIME', 'ssh_host': 'TERMINAL_SSH_HOST', 'ssh_user': 'TERMINAL_SSH_USER', 'ssh_port': 'TERMINAL_SSH_PORT', 'ssh_key': 'TERMINAL_SSH_KEY', 'container_cpu': 'TERMINAL_CONTAINER_CPU', 'container_memory': 'TERMINAL_CONTAINER_MEMORY', 'container_disk': 'TERMINAL_CONTAINER_DISK', 'container_persistent': 'TERMINAL_CONTAINER_PERSISTENT', 'docker_volumes': 'TERMINAL_DOCKER_VOLUMES', 'docker_env': 'TERMINAL_DOCKER_ENV', 'docker_mount_cwd_to_workspace': 'TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE', 'docker_network': 'TERMINAL_DOCKER_NETWORK', 'docker_extra_args': 'TERMINAL_DOCKER_EXTRA_ARGS', 'docker_shm_size': 'TERMINAL_DOCKER_SHM_SIZE', 'docker_run_as_host_user': 'TERMINAL_DOCKER_RUN_AS_HOST_USER', 'docker_persist_across_processes': 'TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES', 'docker_orphan_reaper': 'TERMINAL_DOCKER_ORPHAN_REAPER', 'sandbox_dir': 'TERMINAL_SANDBOX_DIR', 'persistent_shell': 'TERMINAL_PERSISTENT_SHELL'}

def _terminal_env_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)

def terminal_config_env_var_for_key(key: str) -> Optional[str]:
    """Return the env var mirrored by a ``terminal.*`` config key."""
    prefix = 'terminal.'
    if not key.startswith(prefix):
        return None
    return TERMINAL_CONFIG_ENV_MAP.get(key[len(prefix):])

def apply_terminal_config_to_env(*, env: Optional[Dict[str, str]]=None, config: Optional[Dict[str, Any]]=None, override: Optional[bool]=None) -> Dict[str, str]:
    """Bridge ``terminal.*`` config into the env vars terminal tools read.

    ``tools.terminal_tool`` is intentionally environment-driven because it also
    runs in child processes (TUI, dashboard PTY, gateway workers).  This helper
    gives those child-process launch paths the same config bridge as classic
    CLI without importing ``cli.py`` and paying for its startup side effects.

    Explicit keys in the user config's ``terminal`` section are authoritative
    and override their matching env values.  Merged defaults only backfill
    missing env vars; they never replace unrelated exported/.env values.
    """
    target = os.environ if env is None else env
    raw_config = read_raw_config()
    raw_terminal_cfg = raw_config.get('terminal')
    file_has_terminal_config = isinstance(raw_terminal_cfg, dict)
    if not file_has_terminal_config:
        raw_terminal_cfg = {}
    should_override = file_has_terminal_config if override is None else override
    cfg = config if config is not None else load_config_readonly()
    terminal_cfg = cfg.get('terminal', {}) if isinstance(cfg, dict) else {}
    if not isinstance(terminal_cfg, dict):
        return target
    explicit_keys = terminal_cfg.keys() if config is not None else raw_terminal_cfg.keys()
    for cfg_key, env_var in TERMINAL_CONFIG_ENV_MAP.items():
        if cfg_key not in terminal_cfg:
            continue
        value = terminal_cfg[cfg_key]
        if cfg_key == 'cwd':
            raw_cwd = str(value or '').strip()
            if raw_cwd in {'.', 'auto', 'cwd'}:
                continue
            if isinstance(value, str):
                value = os.path.expanduser(value)
        if should_override and cfg_key in explicit_keys or env_var not in target:
            target[env_var] = _terminal_env_value(value)
    return target

def _load_config_impl(*, want_deepcopy: bool) -> Dict[str, Any]:
    with _CONFIG_LOCK:
        ensure_hermes_home()
        config_path = get_config_path()
        path_key = str(config_path)
        try:
            st = config_path.stat()
            user_sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            user_sig = None
        from hermes_cli import managed_scope
        managed_dir = managed_scope.get_managed_dir()
        managed_cfg_path = managed_dir / 'config.yaml' if managed_dir else None
        try:
            mst = managed_cfg_path.stat() if managed_cfg_path else None
            managed_sig = (mst.st_mtime_ns, mst.st_size) if mst else (0, 0)
        except OSError:
            managed_sig = (0, 0)
        if user_sig is not None:
            cache_sig: Optional[Tuple[int, int, int, int]] = (user_sig[0], user_sig[1], managed_sig[0], managed_sig[1])
        elif managed_sig != (0, 0):
            cache_sig = (0, 0, managed_sig[0], managed_sig[1])
        else:
            cache_sig = None
        cached = _LOAD_CONFIG_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and (cached[:4] == cache_sig):
            env_snapshot = cached[5] if len(cached) > 5 else {}
            if all((os.environ.get(k) == v for k, v in env_snapshot.items())):
                return copy.deepcopy(cached[4]) if want_deepcopy else cached[4]
        config = copy.deepcopy(DEFAULT_CONFIG)
        if user_sig is not None:
            try:
                with open(config_path, encoding='utf-8') as f:
                    user_config = fast_safe_load(f) or {}
                if 'max_turns' in user_config:
                    agent_user_config = dict(user_config.get('agent') or {})
                    if agent_user_config.get('max_turns') is None:
                        agent_user_config['max_turns'] = user_config['max_turns']
                    user_config['agent'] = agent_user_config
                    user_config.pop('max_turns', None)
                config = _deep_merge(config, user_config)
            except Exception as e:
                lkg = _LAST_EXPANDED_CONFIG_BY_PATH.get(path_key)
                _warn_config_parse_failure(config_path, e, fallback='last-known-good' if lkg is not None else 'defaults')
                if lkg is not None:
                    from typing import cast as _cast
                    lkg_copy: Dict[str, Any] = _cast(Dict[str, Any], _expand_env_vars(copy.deepcopy(lkg)))
                    if cache_sig is not None:
                        _empty_env: Dict[str, Optional[str]] = {}
                        _LOAD_CONFIG_CACHE[path_key] = (cache_sig[0], cache_sig[1], cache_sig[2], cache_sig[3], lkg_copy, _empty_env)
                    return copy.deepcopy(lkg_copy) if want_deepcopy else lkg_copy
        normalized = _normalize_root_model_keys(_normalize_max_turns_config(config))
        expanded = _expand_env_vars(normalized)
        managed_config = managed_scope.load_managed_config()
        if managed_config:
            managed_expanded = _expand_env_vars(managed_config)
            expanded = _deep_merge(expanded, managed_expanded)
        _LAST_EXPANDED_CONFIG_BY_PATH[path_key] = copy.deepcopy(expanded)
        if cache_sig is not None:
            cached_copy = copy.deepcopy(expanded)
            env_snapshot = _env_ref_snapshot(normalized)
            if managed_config:
                _env_ref_snapshot(managed_config, env_snapshot)
            _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, cached_copy, env_snapshot)
            if not want_deepcopy:
                return cached_copy
        else:
            _LOAD_CONFIG_CACHE.pop(path_key, None)
        return expanded
_SECURITY_COMMENT = '\n# ── Security ──────────────────────────────────────────────────────────\n# Secret redaction is ON by default — strings that look like API keys,\n# tokens, and passwords are masked in tool output, logs, and chat\n# responses before the model or user ever sees them. Set redact_secrets\n# to false to disable (e.g. when developing the redactor itself).\n# tirith pre-exec scanning is enabled by default when the tirith binary\n# is available. Configure via security.tirith_* keys or env vars\n# (TIRITH_ENABLED, TIRITH_BIN, TIRITH_TIMEOUT, TIRITH_FAIL_OPEN).\n#\n# security:\n#   redact_secrets: true\n#   tirith_enabled: true\n#   tirith_path: "tirith"\n#   tirith_timeout: 5\n#   tirith_fail_open: true\n'
_FALLBACK_COMMENT = '\n# ── Fallback Model ────────────────────────────────────────────────────\n# Automatic provider failover when primary is unavailable.\n# Uncomment and configure to enable. Triggers on rate limits (429),\n# overload (529), service errors (503), or connection failures.\n#\n# Supported providers:\n#   openrouter   (OPENROUTER_API_KEY)  — routes to any model\n#   openai-codex (OAuth — duck-agent auth) — OpenAI Codex\n#   nous         (OAuth — duck-agent auth) — Nous Portal\n#   zai          (ZAI_API_KEY)         — Z.AI / GLM\n#   kimi-coding  (KIMI_API_KEY)        — Kimi / Moonshot\n#   kimi-coding-cn (KIMI_CN_API_KEY)   — Kimi / Moonshot (China)\n#   minimax      (MINIMAX_API_KEY)     — MiniMax\n#   minimax-cn   (MINIMAX_CN_API_KEY)  — MiniMax (China)\n#   bedrock      (AWS IAM / boto3)     — AWS Bedrock (Converse API)\n#\n# For custom OpenAI-compatible endpoints, add base_url and key_env.\n#\n# fallback_model:\n#   provider: openrouter\n#   model: anthropic/claude-sonnet-4\n'
_COMMENTED_SECTIONS = '\n# ── Security ──────────────────────────────────────────────────────────\n# Secret redaction is ON by default. Set to false to pass tool output,\n# logs, and chat responses through unmodified (e.g. for redactor dev).\n#\n# security:\n#   redact_secrets: true\n\n# ── Fallback Model ────────────────────────────────────────────────────\n# Automatic provider failover when primary is unavailable.\n# Uncomment and configure to enable. Triggers on rate limits (429),\n# overload (529), service errors (503), or connection failures.\n#\n# Supported providers:\n#   openrouter   (OPENROUTER_API_KEY)  — routes to any model\n#   openai-codex (OAuth — duck-agent auth) — OpenAI Codex\n#   nous         (OAuth — duck-agent auth) — Nous Portal\n#   zai          (ZAI_API_KEY)         — Z.AI / GLM\n#   kimi-coding  (KIMI_API_KEY)        — Kimi / Moonshot\n#   kimi-coding-cn (KIMI_CN_API_KEY)   — Kimi / Moonshot (China)\n#   minimax      (MINIMAX_API_KEY)     — MiniMax\n#   minimax-cn   (MINIMAX_CN_API_KEY)  — MiniMax (China)\n#   bedrock      (AWS IAM / boto3)     — AWS Bedrock (Converse API)\n#\n# For custom OpenAI-compatible endpoints, add base_url and key_env.\n#\n# fallback_model:\n#   provider: openrouter\n#   model: anthropic/claude-sonnet-4\n'

def save_config(config: Dict[str, Any], *, strip_defaults: bool=True, preserve_keys: Optional[Set[Tuple[str, ...]]]=None, merge_existing: bool=False):
    """Save configuration to ~/.duck-agent/config.yaml.


    Default values from ``DEFAULT_CONFIG`` are not written to disk unless
    the user explicitly set them (i.e. the path exists in the raw config
    before any normalisation).  This prevents config.yaml from being
    contaminated with schema defaults on every save, which makes future
    default changes invisible to users.

    When ``merge_existing`` is True, the on-disk raw config is deep-merged
    under *config* before writing so partial callers (migration steps via
    ``_persist_migration``) cannot drop unrelated sections the caller omitted.
    Full-document replacement callers (dashboard raw YAML editor, callers that
    already deep-merge) must leave this False so intentional deletions survive.
    """
    with _CONFIG_LOCK:
        if is_managed():
            managed_error('save configuration')
            return
        from hermes_cli import managed_scope
        managed_keys = managed_scope.managed_config_keys()
        if managed_keys:
            config, _stripped = _strip_dotted_keys(copy.deepcopy(config), managed_keys)
            if _stripped:
                print(f"Note: {len(_stripped)} managed setting(s) were not saved (managed by your administrator): {', '.join(sorted(_stripped))}", file=sys.stderr)
        from utils import atomic_yaml_write
        ensure_hermes_home()
        config_path = get_config_path()
        require_readable_config_before_write(config_path)
        _raw_for_paths = read_raw_config()
        explicit_raw_paths: Optional[Set[Tuple[str, ...]]] = _explicit_config_paths(_raw_for_paths) if _raw_for_paths else None
        if merge_existing and _raw_for_paths:
            config = _merge_partial_save(_raw_for_paths, config)
        current_normalized = _normalize_root_model_keys(_normalize_max_turns_config(config))
        normalized = current_normalized
        raw_existing = _normalize_root_model_keys(_normalize_max_turns_config(_raw_for_paths)) if _raw_for_paths else {}
        if raw_existing:
            normalized = _preserve_env_ref_templates(normalized, raw_existing, _LAST_EXPANDED_CONFIG_BY_PATH.get(str(config_path)))
        effective_preserve_keys: Set[Tuple[str, ...]] = {('_config_version',)}
        if explicit_raw_paths:
            effective_preserve_keys.update(explicit_raw_paths)
        if preserve_keys:
            effective_preserve_keys.update(preserve_keys)
        if strip_defaults and effective_preserve_keys:
            from typing import cast as _cast
            normalized = _cast(Dict[str, Any], normalized)
            normalized = _strip_default_values(normalized, DEFAULT_CONFIG, preserve_keys=effective_preserve_keys)
        parts = []
        sec = normalized.get('security', {})
        if not sec or sec.get('redact_secrets') is None:
            parts.append(_SECURITY_COMMENT)
        fb = normalized.get('fallback_model', {})
        fb_is_valid = False
        if isinstance(fb, list):
            fb_is_valid = any((isinstance(e, dict) and e.get('provider') and e.get('model') for e in fb))
        elif isinstance(fb, dict):
            fb_is_valid = bool(fb.get('provider') and fb.get('model'))
        if not fb_is_valid:
            parts.append(_FALLBACK_COMMENT)
        atomic_yaml_write(config_path, normalized, extra_content=''.join(parts) if parts else None)
        _secure_file(config_path)
        _RAW_CONFIG_CACHE.pop(str(config_path), None)
        _LAST_EXPANDED_CONFIG_BY_PATH[str(config_path)] = copy.deepcopy(current_normalized)

def _parse_env_value(raw_value: str) -> str:
    """Parse the small .env value subset Duck Agent writes itself."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        quoted = value[1:-1]
        parsed: list[str] = []
        i = 0
        while i < len(quoted):
            ch = quoted[i]
            if ch == '\\' and i + 1 < len(quoted):
                next_ch = quoted[i + 1]
                if next_ch in {'"', '\\'}:
                    parsed.append(next_ch)
                    i += 2
                    continue
            parsed.append(ch)
            i += 1
        return ''.join(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value

def load_env() -> Dict[str, str]:
    """Load environment variables from ~/.duck-agent/.env.

    Normalizes line endings before parsing while treating each assignment's
    value as opaque data for boundary discovery.

    The parsed dict is memoised keyed on the .env file mtime, because
    ``get_env_value()`` is called dozens-to-hundreds of times per
    interactive menu render (`duck-agent tools`, `duck-agent setup`, status
    panels). Sanitisation is O(lines), so re-parsing the
    same file on every call was burning ~300ms of CPU per `duck-agent tools`
    menu paint on top of the OAuth-refresh slowness. The mtime check
    invalidates the cache when the user edits .env mid-process.
    """
    global _env_cache
    env_path = get_env_path()
    try:
        mtime = env_path.stat().st_mtime
        size = env_path.stat().st_size
        cache_key = (str(env_path), mtime, size)
    except FileNotFoundError:
        cache_key = (str(env_path), None, None)
    except Exception:
        cache_key = None
    if cache_key is not None and _env_cache is not None:
        cached_key, cached_vars = _env_cache
        if cached_key == cache_key:
            return dict(cached_vars)
    env_vars: Dict[str, str] = {}
    if env_path.exists():
        open_kw = {'encoding': 'utf-8-sig', 'errors': 'replace'}
        with open(env_path, **open_kw) as f:
            raw_lines = f.readlines()
        lines = _sanitize_env_lines(raw_lines)
        for line in lines:
            line = line.strip()
            if line and (not line.startswith('#')) and ('=' in line):
                if line.startswith('export '):
                    line = line[7:]
                key, _, value = line.partition('=')
                env_vars[key.strip()] = _parse_env_value(value)
    if cache_key is not None:
        _env_cache = (cache_key, dict(env_vars))
    return env_vars
_env_cache: Optional[Tuple[Tuple[str, Optional[float], Optional[int]], Dict[str, str]]] = None

def invalidate_env_cache() -> None:
    """Clear the load_env() process-level memo.

    Writers that mutate .env (set_env_value, save_env, etc.) call this
    to guarantee the next load_env() sees their change even on
    filesystems with coarse mtime resolution. Reads invalidate naturally
    via the mtime/size check.
    """
    global _env_cache
    _env_cache = None

def _sanitize_env_lines(lines: list) -> list:
    """Normalize .env line endings without changing assignment semantics.

    Content after the first ``=`` is opaque value data. A known variable name
    embedded in that value must never be reinterpreted as another assignment;
    concatenated assignments are ambiguous and therefore remain on one line.
    """
    sanitized: list[str] = []
    for line in lines:
        raw = line.rstrip('\r\n')
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            sanitized.append(raw + '\n')
            continue
        sanitized.append(stripped + '\n')
    return sanitized

def sanitize_env_file() -> int:
    """Read, sanitize, and rewrite ~/.duck-agent/.env in place.

    Returns the number of lines whose safe formatting was normalized. Returns
    0 when no changes are needed.
    """
    env_path = get_env_path()
    if not env_path.exists():
        return 0
    read_kw = {'encoding': 'utf-8-sig', 'errors': 'replace'}
    write_kw = {'encoding': 'utf-8'}
    with open(env_path, **read_kw) as f:
        original_lines = f.readlines()
    sanitized = _sanitize_env_lines(original_lines)
    if sanitized == original_lines:
        return 0
    fixes = abs(len(sanitized) - len(original_lines))
    if fixes == 0:
        fixes = sum((1 for a, b in zip(original_lines, sanitized) if a != b))
        fixes += abs(len(sanitized) - len(original_lines))
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
    try:
        with os.fdopen(fd, 'w', **write_kw) as f:
            f.writelines(sanitized)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _secure_file(env_path)
    invalidate_env_cache()
    return fixes

def _check_non_ascii_credential(key: str, value: str) -> str:
    """Warn and strip non-ASCII characters from credential values.

    API keys and tokens must be pure ASCII — they are sent as HTTP header
    values which httpx/httpcore encode as ASCII.  Non-ASCII characters
    (commonly introduced by copy-pasting from rich-text editors or PDFs
    that substitute lookalike Unicode glyphs for ASCII letters) cause
    ``UnicodeEncodeError: 'ascii' codec can't encode character`` at
    request time.

    Returns the sanitized (ASCII-only) value.  Prints a warning if any
    non-ASCII characters were found and removed.
    """
    try:
        value.encode('ascii')
        return value
    except UnicodeEncodeError:
        pass
    bad_chars: list[str] = []
    for i, ch in enumerate(value):
        if ord(ch) > 127:
            bad_chars.append(f'  position {i}: {ch!r} (U+{ord(ch):04X})')
    sanitized = value.encode('ascii', errors='ignore').decode('ascii')
    print(f'\n  Warning: {key} contains non-ASCII characters that will break API requests.\n  This usually happens when copy-pasting from a PDF, rich-text editor,\n  or web page that substitutes lookalike Unicode glyphs for ASCII letters.\n\n' + '\n'.join((f'  {line}' for line in bad_chars[:5])) + ('\n  ... and more' if len(bad_chars) > 5 else '') + "\n\n  The non-ASCII characters have been stripped automatically.\n  If authentication fails, re-copy the key from the provider's dashboard.\n", file=sys.stderr)
    return sanitized

def _quote_env_value(value: str) -> str:
    """Quote .env values containing characters with special dotenv meaning."""
    if value == '':
        return value
    needs_quoting = '#' in value or '"' in value or "'" in value or (value != value.strip()) or any((c.isspace() for c in value))
    if not needs_quoting:
        return value
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'

def _env_line_defines_key(line: str, key: str) -> bool:
    """True when a .env line assigns ``key`` — plain or ``export``-prefixed.

    ``load_env()`` accepts the bash-compatible ``export KEY=value`` form
    (#6659), so the writers must recognise the same shape. Otherwise a
    hand-added ``export`` line is invisible to save (duplicate appended) and
    remove (line survives → the value resurrects on the next load, #40041).
    """
    stripped = line.strip()
    if stripped.startswith('export '):
        stripped = stripped[7:].lstrip()
    return stripped.startswith(f'{key}=')

def save_env_value(key: str, value: str):
    """Save or update a value in ~/.duck-agent/.env."""
    if is_managed():
        managed_error(f'set {key}')
        return
    from hermes_cli import managed_scope
    if managed_scope.is_env_managed(key):
        managed_dir = managed_scope.get_managed_dir()
        src = managed_dir / '.env' if managed_dir else 'the managed scope'
        print(f'Cannot set {key}: it is managed by your administrator ({src}) and cannot be changed.', file=sys.stderr)
        return
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f'Invalid environment variable name: {key!r}')
    _reject_denylisted_env_var(key)
    value = value.replace('\n', '').replace('\r', '')
    value = _check_non_ascii_credential(key, value)
    ensure_hermes_home()
    env_path = get_env_path()
    read_kw = {'encoding': 'utf-8-sig', 'errors': 'replace'}
    write_kw = {'encoding': 'utf-8'}
    lines = []
    if env_path.exists():
        with open(env_path, **read_kw) as f:
            lines = f.readlines()
        lines = _sanitize_env_lines(lines)
    serialized_value = _quote_env_value(value)
    found = False
    for i, line in enumerate(lines):
        if _env_line_defines_key(line, key):
            lines[i] = f'{key}={serialized_value}\n'
            found = True
            break
    if not found:
        if lines and (not lines[-1].endswith('\n')):
            lines[-1] += '\n'
        lines.append(f'{key}={serialized_value}\n')
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
    original_mode = None
    if env_path.exists():
        try:
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            pass
    try:
        with os.fdopen(fd, 'w', **write_kw) as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, env_path)
        if original_mode is not None:
            try:
                os.chmod(env_path, original_mode)
            except OSError:
                pass
        else:
            _secure_file(env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.environ[key] = value
    invalidate_env_cache()

def custom_endpoint_key_env(identity: str) -> str:
    """Env var name holding a custom endpoint's API key.

    ``identity`` is whatever names the endpoint on the calling path — the
    Desktop panel's endpoint id, or ``host:port`` for the CLI setup flow.
    Two properties matter:

    - It keys off the endpoint's own identity, not just its hostname, so two
      endpoints on one host (``127.0.0.1:8000`` and ``:8001``) get separate
      slots instead of the second save clobbering the first's credential.
    - The fixed ``HERMES_CUSTOM_`` prefix keeps the result a valid POSIX name
      even when the slug starts with a digit, which every IP-based local
      endpoint does (``127.0.0.1`` → ``127_0_0_1``). ``save_env_value``
      rejects digit-leading names outright.
    """
    slug = re.sub('[^A-Z0-9]+', '_', str(identity or '').upper()).strip('_')
    return f'HERMES_CUSTOM_{slug}_API_KEY' if slug else 'HERMES_CUSTOM_API_KEY'

def remove_env_value(key: str) -> bool:
    """Remove a key from ~/.duck-agent/.env and os.environ.

    Returns True if the key was found and removed, False otherwise.
    """
    if is_managed():
        managed_error(f'remove {key}')
        return False
    from hermes_cli import managed_scope
    if managed_scope.is_env_managed(key):
        managed_dir = managed_scope.get_managed_dir()
        src = managed_dir / '.env' if managed_dir else 'the managed scope'
        print(f'Cannot remove {key}: it is managed by your administrator ({src}) and cannot be changed.', file=sys.stderr)
        return False
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f'Invalid environment variable name: {key!r}')
    env_path = get_env_path()
    if not env_path.exists():
        os.environ.pop(key, None)
        return False
    read_kw = {'encoding': 'utf-8-sig', 'errors': 'replace'}
    write_kw = {'encoding': 'utf-8'}
    with open(env_path, **read_kw) as f:
        lines = f.readlines()
    lines = _sanitize_env_lines(lines)
    new_lines = [line for line in lines if not _env_line_defines_key(line, key)]
    found = len(new_lines) < len(lines)
    if found:
        fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
        original_mode = None
        try:
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            pass
        try:
            with os.fdopen(fd, 'w', **write_kw) as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, env_path)
            if original_mode is not None:
                try:
                    os.chmod(env_path, original_mode)
                except OSError:
                    pass
            else:
                _secure_file(env_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    os.environ.pop(key, None)
    invalidate_env_cache()
    return found

def save_anthropic_oauth_token(value: str, save_fn=None):
    """Persist an Anthropic OAuth/setup token and clear the API-key slot."""
    writer = save_fn or save_env_value
    writer('ANTHROPIC_TOKEN', value)
    writer('ANTHROPIC_API_KEY', '')

def use_anthropic_claude_code_credentials(save_fn=None):
    """Use Claude Code's own credential files instead of persisting env tokens."""
    writer = save_fn or save_env_value
    writer('ANTHROPIC_TOKEN', '')
    writer('ANTHROPIC_API_KEY', '')

def save_anthropic_api_key(value: str, save_fn=None):
    """Persist an Anthropic API key and clear the OAuth/setup-token slot."""
    writer = save_fn or save_env_value
    writer('ANTHROPIC_API_KEY', value)
    writer('ANTHROPIC_TOKEN', '')

def save_env_value_secure(key: str, value: str) -> Dict[str, Any]:
    from hermes_cli.credential_lifecycle import save_provider_env_credential
    save_provider_env_credential(key, value)
    return {'success': True, 'stored_as': key, 'validated': False}

def reload_env() -> int:
    """Re-read ~/.duck-agent/.env into os.environ. Returns count of vars updated.

    Adds/updates vars that changed and removes vars that were deleted from
    the .env file (but only vars known to Duck Agent — OPTIONAL_ENV_VARS and
    _EXTRA_ENV_KEYS — to avoid clobbering unrelated environment).
    """
    env_vars = load_env()
    known_keys = set(OPTIONAL_ENV_VARS.keys()) | _EXTRA_ENV_KEYS
    count = 0
    for key, value in env_vars.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            count += 1
    for key in known_keys:
        if key not in env_vars and key in os.environ:
            del os.environ[key]
            count += 1
    return count

def get_env_value(key: str) -> Optional[str]:
    """Get a value from ``os.environ`` or ``~/.duck-agent/.env``, scope-aware.

    The ``os.environ`` read routes through ``agent.secret_scope.get_secret``
    so that, under an active profile scope (multiplexed gateway turn), this
    is scope-checked rather than leaking another profile's raw ``os.environ``
    value. ``get_secret`` encodes the whole policy: global vars pass through;
    scope is authoritative under multiplexing (miss -> None, no environ
    fallthrough); when multiplexing is off it behaves exactly like the
    legacy ``os.environ`` read. Its siblings ``get_env_value_prefer_dotenv``
    and ``gateway.config._getenv`` already work this way — this was the last
    scope-blind reader of the trio (#67027).
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret as _get_secret
    except Exception:
        if key in os.environ:
            return os.environ[key]
        return load_env().get(key)
    try:
        val = _get_secret(key)
    except UnscopedSecretError:
        raise
    except Exception:
        val = os.environ.get(key)
    if val is not None:
        return val
    env_vars = load_env()
    return env_vars.get(key)

def get_env_value_prefer_dotenv(key: str) -> Optional[str]:
    """Resolve a credential env value, preferring ``~/.duck-agent/.env`` over ``os.environ``.

    Used for Duck Agent-managed credentials where a deliberate edit to ``.env``
    must take precedence over a stale value inherited from the parent shell
    (Codex CLI, test scripts, login profile exports). Without this, rotating
    a key in ``.env`` mid-session leaves callers serving the stale shell
    value and produces persistent 401s.

    The ``os.environ`` fallback routes through ``secret_scope.get_secret`` so
    that, under an active profile scope (multiplexed gateway turn), this read
    is scope-checked rather than leaking another profile's raw ``os.environ``
    value — matching the credential-pool seeding path's behaviour.
    """
    env_vars = load_env()
    val = env_vars.get(key)
    if val:
        return val
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret as _get_secret
    except Exception:
        return os.environ.get(key)
    try:
        return _get_secret(key)
    except UnscopedSecretError:
        raise
    except Exception:
        return os.environ.get(key)

def redact_key(key: str) -> str:
    """Redact an API key for display.

    Thin wrapper over :func:`agent.redact.mask_secret` — preserves the
    "(not set)" placeholder in dim color for the empty case.
    """
    from agent.redact import mask_secret
    return mask_secret(key, empty=color('(not set)', Colors.DIM))
_SECRET_CONFIG_KEYS = frozenset({'api_key', 'apikey', 'key', 'token', 'access_token', 'refresh_token', 'id_token', 'secret', 'client_secret', 'password', 'passwd', 'auth', 'authorization', 'private_key', 'bearer', 'jwt'})

def redact_config_value(value: Any, _depth: int=0) -> Any:
    """Return a copy of ``value`` with credential-shaped keys masked for display.

    Recursively walks dicts/lists and replaces the value of any key in
    ``_SECRET_CONFIG_KEYS`` (case-insensitive) with a masked form via
    :func:`agent.redact.mask_secret`. Non-secret keys and scalar values pass
    through unchanged. Use this before ``print``-ing any config sub-tree that
    might carry a custom-provider ``api_key`` — ``print`` bypasses the logging
    redactor, and opaque tokens (e.g. Cloudflare ``cfut_...``) don't match the
    vendor-prefix regexes either, so structural key-name masking is required.
    """
    from agent.redact import mask_secret
    if _depth > 20:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_CONFIG_KEYS and isinstance(v, str) and v:
                out[k] = mask_secret(v)
            else:
                out[k] = redact_config_value(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact_config_value(v, _depth + 1) for v in value]
    return value

def show_config():
    """Display current configuration."""
    config = load_config()
    print()
    print(color('┌─────────────────────────────────────────────────────────┐', Colors.CYAN))
    print(color('│              ⚕ Duck Agent Configuration                    │', Colors.CYAN))
    print(color('└─────────────────────────────────────────────────────────┘', Colors.CYAN))
    from hermes_cli import managed_scope
    _managed_keys = managed_scope.managed_config_keys()
    _managed_env = managed_scope.load_managed_env()
    if _managed_keys or _managed_env:
        _managed_dir = managed_scope.get_managed_dir()
        print()
        print(color(f'  ⚷ Some settings are managed by your administrator ({_managed_dir}) and cannot be changed', Colors.YELLOW, Colors.BOLD))
        if _managed_keys:
            print(color(f"    Managed config keys: {', '.join(sorted(_managed_keys))}", Colors.YELLOW))
        if _managed_env:
            print(color(f"    Managed env keys: {', '.join(sorted(_managed_env))}", Colors.YELLOW))
    print()
    print(color('◆ Paths', Colors.CYAN, Colors.BOLD))
    print(f'  Config:       {get_config_path()}')
    print(f'  Secrets:      {get_env_path()}')
    print(f'  Install:      {get_project_root()}')
    print()
    print(color('◆ API Keys', Colors.CYAN, Colors.BOLD))
    keys = [('OPENROUTER_API_KEY', 'OpenRouter'), ('VOICE_TOOLS_OPENAI_KEY', 'OpenAI (STT/TTS)'), ('EXA_API_KEY', 'Exa'), ('PARALLEL_API_KEY', 'Parallel'), ('FIRECRAWL_API_KEY', 'Firecrawl'), ('TAVILY_API_KEY', 'Tavily'), ('BROWSERBASE_API_KEY', 'Browserbase'), ('BROWSER_USE_API_KEY', 'Browser Use'), ('FAL_KEY', 'FAL')]
    for env_key, name in keys:
        value = get_env_value(env_key)
        print(f'  {name:<14} {redact_key(value)}')
    from hermes_cli.auth import get_anthropic_key
    anthropic_value = get_anthropic_key()
    print(f"  {'Anthropic':<14} {redact_key(anthropic_value)}")
    print()
    print(color('◆ Model', Colors.CYAN, Colors.BOLD))
    print(f"  Model:        {redact_config_value(config.get('model', 'not set'))}")
    _cfg_max_turns = config.get('agent', {}).get('max_turns', DEFAULT_CONFIG['agent']['max_turns'])
    print(f'  Max turns:    {_cfg_max_turns}')
    try:
        _env_ghost = load_env().get('HERMES_MAX_ITERATIONS')
        if _env_ghost is not None and str(_env_ghost).strip() != str(_cfg_max_turns).strip():
            print(color(f"                ⚠ .env has stale HERMES_MAX_ITERATIONS={_env_ghost} (run 'duck-agent doctor --fix' to remove)", Colors.YELLOW))
    except Exception:
        pass
    print()
    print(color('◆ Display', Colors.CYAN, Colors.BOLD))
    display = config.get('display', {})
    print(f"  Personality:  {display.get('personality') or 'none'}")
    print(f"  Reasoning:    {('on' if display.get('show_reasoning', True) else 'off')}")
    print(f"  Bell:         {('on' if display.get('bell_on_complete', False) else 'off')}")
    ump = display.get('user_message_preview', {}) if isinstance(display.get('user_message_preview', {}), dict) else {}
    ump_first = ump.get('first_lines', 2)
    ump_last = ump.get('last_lines', 2)
    print(f'  User preview: first {ump_first} line(s), last {ump_last} line(s)')
    print()
    print(color('◆ Terminal', Colors.CYAN, Colors.BOLD))
    terminal = config.get('terminal', {})
    print(f"  Backend:      {terminal.get('backend', 'local')}")
    print(f"  Working dir:  {terminal.get('cwd', '.')}")
    print(f"  Timeout:      {terminal.get('timeout', 60)}s")
    if terminal.get('backend') == 'docker':
        print(f"  Docker image: {terminal.get('docker_image', 'nikolaik/python-nodejs:python3.11-nodejs20')}")
    elif terminal.get('backend') == 'singularity':
        print(f"  Image:        {terminal.get('singularity_image', 'docker://nikolaik/python-nodejs:python3.11-nodejs20')}")
    elif terminal.get('backend') == 'modal':
        print(f"  Modal image:  {terminal.get('modal_image', 'nikolaik/python-nodejs:python3.11-nodejs20')}")
        modal_token = get_env_value('MODAL_TOKEN_ID')
        print(f"  Modal token:  {('configured' if modal_token else '(not set)')}")
    elif terminal.get('backend') == 'daytona':
        print(f"  Daytona image: {terminal.get('daytona_image', 'nikolaik/python-nodejs:python3.11-nodejs20')}")
        daytona_key = get_env_value('DAYTONA_API_KEY')
        print(f"  API key:      {('configured' if daytona_key else '(not set)')}")
    elif terminal.get('backend') == 'vercel_sandbox':
        print(f"  Vercel runtime: {terminal.get('vercel_runtime', 'node24')}")
        print(f"  Vercel auth:    {('configured' if get_env_value('VERCEL_OIDC_TOKEN') or (get_env_value('VERCEL_TOKEN') and get_env_value('VERCEL_PROJECT_ID') and get_env_value('VERCEL_TEAM_ID')) else '(not set)')}")
    elif terminal.get('backend') == 'ssh':
        ssh_host = get_env_value('TERMINAL_SSH_HOST')
        ssh_user = get_env_value('TERMINAL_SSH_USER')
        print(f"  SSH host:     {ssh_host or '(not set)'}")
        print(f"  SSH user:     {ssh_user or '(not set)'}")
    print()
    print(color('◆ Timezone', Colors.CYAN, Colors.BOLD))
    tz = config.get('timezone', '')
    if tz:
        print(f'  Timezone:     {tz}')
    else:
        print(f"  Timezone:     {color('(server-local)', Colors.DIM)}")
    print()
    print(color('◆ Context Compression', Colors.CYAN, Colors.BOLD))
    compression = config.get('compression', {})
    enabled = compression.get('enabled', True)
    print(f"  Enabled:      {('yes' if enabled else 'no')}")
    if enabled:
        print(f"  Threshold:    {compression.get('threshold', 0.5) * 100:.0f}%")
        _tt = compression.get('threshold_tokens')
        if _tt is not None:
            try:
                _tt = int(_tt)
                if _tt > 0:
                    print(f'  Token cap:    {_tt:,} tokens (takes lower of ratio vs absolute)')
            except (TypeError, ValueError):
                pass
        print(f"  Target ratio: {compression.get('target_ratio', 0.2) * 100:.0f}% of threshold preserved")
        print(f"  Protect last: {compression.get('protect_last_n', 20)} messages")
        print(f"  Protect first: {compression.get('protect_first_n', 3)} non-system head messages")
        _aux_comp = config.get('auxiliary', {}).get('compression', {})
        _sm = _aux_comp.get('model', '') or '(auto)'
        print(f'  Model:        {_sm}')
        comp_provider = _aux_comp.get('provider', 'auto')
        if comp_provider and comp_provider != 'auto':
            print(f'  Provider:     {comp_provider}')
    auxiliary = config.get('auxiliary', {})
    aux_tasks = {'Vision': auxiliary.get('vision', {}), 'Web extract': auxiliary.get('web_extract', {})}
    has_overrides = any((t.get('provider', 'auto') != 'auto' or t.get('model', '') for t in aux_tasks.values()))
    if has_overrides:
        print()
        print(color('◆ Auxiliary Models (overrides)', Colors.CYAN, Colors.BOLD))
        for label, task_cfg in aux_tasks.items():
            prov = task_cfg.get('provider', 'auto')
            mdl = task_cfg.get('model', '')
            if prov != 'auto' or mdl:
                parts = [f'provider={prov}']
                if mdl:
                    parts.append(f'model={mdl}')
                print(f"  {label:12s}  {', '.join(parts)}")
    print()
    print(color('◆ Messaging Platforms', Colors.CYAN, Colors.BOLD))
    telegram_token = get_env_value('TELEGRAM_BOT_TOKEN')
    discord_token = get_env_value('DISCORD_BOT_TOKEN')
    print(f"  Telegram:     {('configured' if telegram_token else color('not configured', Colors.DIM))}")
    print(f"  Discord:      {('configured' if discord_token else color('not configured', Colors.DIM))}")
    try:
        from agent.skill_utils import discover_all_skill_config_vars, resolve_skill_config_values
        skill_vars = discover_all_skill_config_vars()
        if skill_vars:
            resolved = resolve_skill_config_values(skill_vars)
            print()
            print(color('◆ Skill Settings', Colors.CYAN, Colors.BOLD))
            for var in skill_vars:
                key = var['key']
                value = resolved.get(key, '')
                skill_name = var.get('skill', '')
                display_val = str(value) if value else color('(not set)', Colors.DIM)
                print(f"  {key:<20s} {display_val}  {color(f'[{skill_name}]', Colors.DIM)}")
    except Exception:
        pass
    print()
    print(color('─' * 60, Colors.DIM))
    print(color('  duck-agent config edit     # Edit config file', Colors.DIM))
    print(color('  duck-agent config set <key> <value>', Colors.DIM))
    print(color('  duck-agent setup           # Run setup wizard', Colors.DIM))
    print()

def edit_config():
    """Open config file in user's editor."""
    if is_managed():
        managed_error('edit configuration')
        return
    config_path = get_config_path()
    if not config_path.exists():
        save_config(DEFAULT_CONFIG, strip_defaults=False)
        print(f'Created {config_path}')
    editor = os.getenv('EDITOR') or os.getenv('VISUAL')
    if not editor:
        import shutil
        import sys as _sys
        if _sys.platform == 'win32':
            candidates = ['notepad', 'code', 'vim', 'vi', 'nano']
        else:
            candidates = ['nano', 'vim', 'vi', 'code', 'notepad']
        for cmd in candidates:
            if shutil.which(cmd):
                editor = cmd
                break
    if not editor:
        print('No editor found. Config file is at:')
        print(f'  {config_path}')
        return
    print(f'Opening {config_path} in {editor}...')
    subprocess.run([editor, str(config_path)])

def _cron_model_drift_axis_for_config_key(key: str) -> Optional[str]:
    """Return the cron drift guard axis affected by a config key, if any."""
    normalized = str(key or '').strip().lower()
    if normalized in {'model', 'model.default', 'model.model', 'model.name'}:
        return 'model'
    if normalized in {'model.provider', 'provider'}:
        return 'provider'
    return None

def cron_model_drift_guard_enabled(config: Optional[Dict[str, Any]]=None) -> bool:
    """Return whether cron must fail closed on unpinned inference drift.

    Only the literal YAML boolean ``false`` disables this spend-safety guard.
    Missing, malformed, or non-boolean values stay fail-closed. When *config*
    is omitted, load the active merged configuration so CLI warnings honor the
    same user/managed setting as the scheduler.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return True
    if not isinstance(config, dict):
        return True
    cron_config = config.get('cron')
    if not isinstance(cron_config, dict):
        return True
    return cron_config.get('model_drift_guard', True) is not False

def _cron_fleet_default_covers_axis(axis: str, config: Optional[Dict[str, Any]]=None) -> bool:
    """True when cron.model / cron.model_provider covers *axis*.

    An axis covered by the explicit cron-fleet default no longer follows the
    global model/provider at fire time, so the drift guard never engages for
    it and switch-time warnings would be false alarms.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return False
    if not isinstance(config, dict):
        return False
    cron_config = config.get('cron')
    if not isinstance(cron_config, dict):
        return False
    key = 'model' if axis == 'model' else 'model_provider'
    value = cron_config.get(key)
    return isinstance(value, str) and bool(value.strip())

def _load_cron_jobs_for_config_warning() -> List[Dict[str, Any]]:
    """Best-effort read of the active profile's cron jobs database.

    Delegates to ``cron.jobs.load_jobs`` to reuse its BOM handling, corruption
    repair, and context-local store resolution (tests, embedders). Falls back
    to an empty list on any failure so config writes never break.
    """
    try:
        from cron.jobs import load_jobs
        return load_jobs()
    except Exception:
        return []

def warn_unpinned_cron_jobs_after_model_config_change(key: str, value: Any, config: Optional[Dict[str, Any]]=None) -> None:
    """Warn when a global model/provider change will trip cron's drift guard.

    Cron intentionally fails closed when an unpinned agent job's current global
    model/provider differs from its creation-time snapshot. Surface that outcome
    when the operator changes the global axis instead of letting the next tick
    be the first visible signal.
    """
    axis = _cron_model_drift_axis_for_config_key(key)
    if axis is None:
        return
    if not cron_model_drift_guard_enabled(config):
        return
    if _cron_fleet_default_covers_axis(axis, config):
        return
    new_value = str(value or '').strip().lower()
    if not new_value:
        return
    pinned_field = axis
    snapshot_field = f'{axis}_snapshot'
    affected = 0
    for job in _load_cron_jobs_for_config_warning():
        if not job.get('enabled', True):
            continue
        if job.get('no_agent'):
            continue
        if str(job.get(pinned_field) or '').strip():
            continue
        snapshot = str(job.get(snapshot_field) or '').strip().lower()
        if snapshot and snapshot != new_value:
            affected += 1
    if affected <= 0:
        return
    noun = 'job' if affected == 1 else 'jobs'
    verb = 'has' if affected == 1 else 'have'
    print(f'⚠️  {affected} enabled unpinned cron {noun} {verb} stored {snapshot_field} values that differ from the new global {axis}. They will fail closed on their next run instead of silently using the changed model/provider. Inspect with `duck-agent cron list`, then pin the intended values with `cronjob action=update job_id=<job_id> provider=<provider> model=<model>`.')

def _default_value_for_key(dotted_key: str):
    """Return the leaf value declared for *dotted_key* in ``DEFAULT_CONFIG``.

    Unknown keys and non-leaf paths return ``None`` so they retain the legacy
    best-effort coercion used by ``config set``.
    """
    node = DEFAULT_CONFIG
    for part in dotted_key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if not isinstance(node, dict) else None
_OPEN_DICT_TOP_LEVEL_KEYS = frozenset({'providers', 'credential_pool_strategies', 'mcp_servers', 'hooks', 'quick_commands', 'personalities', 'command_allowlist', 'model_catalog', 'channel_prompts', 'server_actions', 'secrets', 'goals'})
_SCHEMA_DEFINED_DICT_KEYS = frozenset({'discord', 'telegram', 'slack', 'whatsapp', 'signal', 'mattermost', 'matrix', 'feishu', 'wecom', 'weixin', 'bluebubbles', 'qqbot', 'yuanbao', 'email', 'sms', 'dingtalk', 'sessions', 'checkpoints'})
_DYNAMIC_TOP_LEVEL_KEYS = frozenset({'custom_providers'})
_PLATFORM_CONTAINER_KEYS = frozenset({'platforms'})

def _known_top_level_keys() -> set[str]:
    """Return the union of known top-level config keys for validation.

    Combines :data:`DEFAULT_CONFIG` with the dynamic categories that
    accept user-supplied child keys.  Used by :func:`_validate_config_key`
    to decide whether a ``duck-agent config set`` invocation is targeting a
    known shape.
    """
    keys = set(DEFAULT_CONFIG.keys())
    keys.update(_OPEN_DICT_TOP_LEVEL_KEYS)
    keys.update(_DYNAMIC_TOP_LEVEL_KEYS)
    keys.update(_SCHEMA_DEFINED_DICT_KEYS)
    return keys

def _suggest_closest_key(key: str, candidates: set[str], cutoff: float=0.6) -> Optional[str]:
    """Return the closest valid key name from ``candidates`` if any are
    similar enough to ``key``, else None.  Used by ``duck-agent config set``
    to point users at the right path when they've typo'd a top-level key.

    Uses :func:`difflib.get_close_matches` with a conservative cutoff so
    we only suggest when there's a strong match — we'd rather say nothing
    than mislead a user toward a wrong-but-similar key.
    """
    import difflib
    matches = difflib.get_close_matches(key, sorted(candidates), n=1, cutoff=cutoff)
    return matches[0] if matches else None

def _validate_config_key(key: str) -> tuple[bool, Optional[str]]:
    """Validate a dotted config-key path against the known schema.

    Returns ``(is_known, suggested_alternative_or_None)``.  Known keys
    return ``(True, None)``.  Unknown keys return ``(False, <suggestion>)``
    where ``<suggestion>`` may be ``None`` if no close match was found.

    Validates as deep as DEFAULT_CONFIG can be safely walked, then stops
    at any segment that hits an open-dict container (mcp_servers,
    providers, hooks, etc.) where users define the inner keys themselves.

    Headline case from #34067: ``gateway.discord.gateway_restart_notification``
    was silently written, even though ``gateway`` only has 4 known sub-keys
    (``strict``, ``media_delivery_allow_dirs``, ``trust_recent_files``,
    ``trust_recent_files_seconds``). The correct path is
    ``discord.gateway_restart_notification`` (platform configs live at the
    top level, not under a ``platforms`` namespace).
    """
    if not key:
        return (False, None)
    segments = key.split('.')
    top = segments[0]
    if top.startswith('_'):
        return (True, None)
    known = _known_top_level_keys()
    if top in _PLATFORM_CONTAINER_KEYS:
        return (True, None)
    if top not in known:
        suggestion = _suggest_closest_key(top, known)
        if suggestion is not None:
            rest = '.'.join(segments[1:])
            suggested_full = f'{suggestion}.{rest}' if rest else suggestion
            return (False, suggested_full)
        return (False, None)
    if top in _OPEN_DICT_TOP_LEVEL_KEYS or top in _DYNAMIC_TOP_LEVEL_KEYS or top in _SCHEMA_DEFINED_DICT_KEYS:
        return (True, None)
    node: Any = DEFAULT_CONFIG.get(top)
    consumed = [top]
    for seg in segments[1:]:
        if seg in _PLATFORM_CONTAINER_KEYS:
            return (True, None)
        if not isinstance(node, dict):
            return (True, None)
        if seg not in node:
            sibling_suggestion = _suggest_closest_key(seg, set(node.keys()))
            if sibling_suggestion is not None:
                fixed_path = '.'.join(consumed + [sibling_suggestion])
                return (False, fixed_path)
            return (False, None)
        consumed.append(seg)
        node = node[seg]
    return (True, None)

def set_config_value(key: str, value: str, force: bool=False):
    """Set a configuration value.

    Args:
        key: Dotted config path (e.g. ``terminal.backend``).
        value: String value (auto-coerced to bool/int/float when matching).
        force: When True, skip the unknown-key warning — useful for scripted
            writes of keys the running version doesn't recognize yet — AND
            authorize destructive replacement of a mapping section by a
            scalar (e.g. ``--force model gpt-x`` replaces the whole ``model:``
            mapping). Without --force, scalar writes over mapping sections are
            refused (bare ``model`` is redirected to ``model.default``). The
            CLI exposes this via ``duck-agent config set --force``.
    """
    if is_managed():
        managed_error('set configuration values')
        return
    from hermes_cli import managed_scope
    if managed_scope.is_key_managed(key):
        managed_dir = managed_scope.get_managed_dir()
        src = managed_dir / 'config.yaml' if managed_dir else 'the managed scope'
        print(f"Cannot set '{key}': it is managed by your administrator ({src}) and cannot be changed. Contact your administrator to modify it.", file=sys.stderr)
        sys.exit(1)
    if _is_env_config_key(key):
        from hermes_cli.credential_lifecycle import save_provider_env_credential
        save_provider_env_credential(key.upper(), value)
        print(f'✓ Set {key} in {get_env_path()}')
        return
    is_known, suggestion = _validate_config_key(key)
    config_path = get_config_path()
    require_readable_config_before_write(config_path)
    user_config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding='utf-8') as f:
                user_config = fast_safe_load(f) or {}
        except Exception as exc:
            print(f'✗ Cannot parse {config_path}: {exc}\n  The file contains a YAML syntax error. Fix the error\n  in your config file first, then retry.\n  (duck-agent config edit will open it in your editor.)', file=sys.stderr)
            sys.exit(1)
    coerced_value: Any = value
    if not isinstance(_default_value_for_key(key), str):
        if value.lower() in {'true', 'yes', 'on'}:
            coerced_value = True
        elif value.lower() in {'false', 'no', 'off'}:
            coerced_value = False
        elif value.isdigit():
            coerced_value = int(value)
        elif value.replace('.', '', 1).isdigit():
            coerced_value = float(value)
    value = coerced_value
    _model_key = key.strip().lower()
    if _model_key.startswith('model.'):
        _model_val = user_config.get('model')
        if isinstance(_model_val, str) and _model_val:
            user_config['model'] = {'default': _model_val}
    if '.' not in key:
        _existing = user_config.get(key)
        if isinstance(_existing, dict):
            if key == 'model':
                if force:
                    print(f"⚠ Replacing entire 'model' section with a scalar (discarding {len(_existing)} existing sub-key(s))")
                else:
                    key = 'model.default'
                    print(f"✓ Redirecting bare 'model' to 'model.default' (preserving {len(_existing)} existing model sub-key(s))")
            elif not force:
                _sub = [k for k in _existing if isinstance(k, str)]
                print(f"✗ Cannot set '{key}' to a scalar — '{key}' is a configuration section with {len(_sub)} sub-key(s).", file=sys.stderr)
                if _sub:
                    _sub_list = ', '.join(_sub[:8])
                    print(f'  Sub-keys: {_sub_list}', file=sys.stderr)
                    if len(_sub) > 8:
                        print(f'  ... and {len(_sub) - 8} more', file=sys.stderr)
                print('  Use a dotted path to set a specific leaf key:', file=sys.stderr)
                print(f'    duck-agent config set {key}.<sub-key> <value>', file=sys.stderr)
                print('  Or use --force to replace the entire section:', file=sys.stderr)
                print(f'    duck-agent config set --force {key} {value!r}', file=sys.stderr)
                sys.exit(1)
    _set_nested(user_config, key, value)
    _alias_norm = key.strip().lower()
    if _alias_norm in ('model.api_base', 'api_base'):
        user_config = _normalize_root_model_keys(user_config)
        key = 'model.base_url'
        print("  (note: 'api_base' is an alias — saved as model.base_url)")
    ensure_hermes_home()
    from utils import atomic_yaml_write
    atomic_yaml_write(config_path, user_config, sort_keys=False)
    env_var = terminal_config_env_var_for_key(key)
    if env_var and key != 'terminal.cwd':
        save_env_value(env_var, _terminal_env_value(value))
    if key == 'display.skin' and isinstance(value, str) and value:
        try:
            skin_file = get_hermes_home() / 'skins' / f'{value}.yaml'
            if skin_file.exists():
                skin_file.touch()
        except Exception:
            pass
    _leaf_key = key.rsplit('.', 1)[-1].lower()
    if _leaf_key in _SECRET_CONFIG_KEYS and isinstance(value, str) and value:
        from agent.redact import mask_secret
        _display_value = mask_secret(value)
    else:
        _display_value = value
    print(f'✓ Set {key} = {_display_value} in {config_path}')
    warn_unpinned_cron_jobs_after_model_config_change(key, value, user_config)
    if not is_known and (not force):
        print(color(f"⚠ '{key}' is not a recognized config key — it was saved anyway, but Duck Agent may not read it.", Colors.YELLOW))
        if suggestion:
            print(color(f'  Did you mean: {suggestion}', Colors.YELLOW))
        print(color('  (Custom top-level keys are supported and bridged to the environment for skills/external tools. Use --force to skip this notice.)', Colors.DIM))

def get_config_value(key: str, *, as_json: bool=False):
    """Print a resolved configuration value."""
    if _is_env_config_key(key):
        env_value = get_env_value(key.upper())
        value = _MISSING if env_value is None else env_value
    else:
        value = _get_nested(load_config(), key)
    if value is _MISSING:
        print(f'Config key not set: {key}', file=sys.stderr)
        sys.exit(1)
    print(_format_config_get_value(value, as_json=as_json))

def unset_config_value(key: str):
    """Remove a user-set configuration or .env value."""
    if is_managed():
        managed_error('unset configuration values')
        return
    from hermes_cli import managed_scope
    if managed_scope.is_key_managed(key):
        managed_dir = managed_scope.get_managed_dir()
        src = managed_dir / 'config.yaml' if managed_dir else 'the managed scope'
        print(f"Cannot unset '{key}': it is managed by your administrator ({src}) and cannot be changed. Contact your administrator to modify it.", file=sys.stderr)
        sys.exit(1)
    if _is_env_config_key(key):
        from hermes_cli.credential_lifecycle import remove_provider_env_credential
        if not remove_provider_env_credential(key.upper()).get('found'):
            print(f'Config key not set: {key}', file=sys.stderr)
            sys.exit(1)
        print(f'✓ Unset {key} from {get_env_path()}')
        return
    config_path = get_config_path()
    require_readable_config_before_write(config_path)
    user_config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding='utf-8') as f:
                user_config = fast_safe_load(f) or {}
        except Exception as exc:
            print(f'✗ Cannot parse {config_path}: {exc}\n  The file contains a YAML syntax error. Fix the error\n  in your config file first, then retry.\n  (duck-agent config edit will open it in your editor.)', file=sys.stderr)
            sys.exit(1)
    removed = _unset_nested(user_config, key)
    env_var = terminal_config_env_var_for_key(key)
    if env_var and key != 'terminal.cwd':
        removed = remove_env_value(env_var) or removed
    if not removed:
        print(f'Config key not set: {key}', file=sys.stderr)
        sys.exit(1)
    ensure_hermes_home()
    from utils import atomic_yaml_write
    atomic_yaml_write(config_path, user_config, sort_keys=False)
    print(f'✓ Unset {key} from {config_path}')

def config_command(args):
    """Handle config subcommands."""
    subcmd = getattr(args, 'config_command', None)
    if subcmd is None or subcmd == 'show':
        show_config()
    elif subcmd == 'edit':
        edit_config()
    elif subcmd == 'get':
        key = getattr(args, 'key', None)
        if not key:
            print('Usage: duck-agent config get <key> [--json]')
            print()
            print('Examples:')
            print('  duck-agent config get model')
            print('  duck-agent config get terminal.backend')
            print('  duck-agent config get skills.config --json')
            sys.exit(1)
        get_config_value(key, as_json=getattr(args, 'json', False))
    elif subcmd == 'set':
        key = getattr(args, 'key', None)
        value = getattr(args, 'value', None)
        force = bool(getattr(args, 'force', False))
        if not key or value is None:
            print('Usage: duck-agent config set [--force] <key> <value>')
            print()
            print('Examples:')
            print('  duck-agent config set model anthropic/claude-sonnet-4')
            print('  duck-agent config set terminal.backend docker')
            print('  duck-agent config set OPENROUTER_API_KEY sk-or-...')
            print()
            print('  --force: skip the unknown-key notice for unrecognized keys,')
            print('           and allow a scalar to replace a whole mapping section')
            sys.exit(1)
        set_config_value(key, value, force=force)
    elif subcmd == 'unset':
        key = getattr(args, 'key', None)
        if not key:
            print('Usage: duck-agent config unset <key>')
            print()
            print('Examples:')
            print('  duck-agent config unset model')
            print('  duck-agent config unset terminal.backend')
            print('  duck-agent config unset OPENROUTER_API_KEY')
            sys.exit(1)
        unset_config_value(key)
    elif subcmd == 'path':
        print(get_config_path())
    elif subcmd == 'env-path':
        print(get_env_path())
    elif subcmd == 'migrate':
        print()
        print(color('🔄 Checking configuration for updates...', Colors.CYAN, Colors.BOLD))
        print()
        missing_env = get_missing_env_vars(required_only=False)
        missing_config = get_missing_config_fields()
        current_ver, latest_ver = check_config_version()
        if not missing_env and (not missing_config) and (current_ver >= latest_ver):
            print(color('✓ Configuration is up to date!', Colors.GREEN))
            print()
            return
        if current_ver < latest_ver:
            print(f'  Config version: {current_ver} → {latest_ver}')
        if missing_config:
            print(f'\n  {len(missing_config)} new config option(s) will be added with defaults')
        required_missing = [v for v in missing_env if v.get('is_required')]
        optional_missing = [v for v in missing_env if not v.get('is_required') and (not v.get('advanced'))]
        if required_missing:
            print(f'\n  ⚠️  {len(required_missing)} required API key(s) missing:')
            for var in required_missing:
                print(f"     • {var['name']}")
        if optional_missing:
            print(f'\n  ℹ️  {len(optional_missing)} optional API key(s) not configured:')
            for var in optional_missing:
                tools = var.get('tools', [])
                tools_str = f" (enables: {', '.join(tools[:2])})" if tools else ''
                print(f"     • {var['name']}{tools_str}")
        print()
        results = migrate_config(interactive=True, quiet=False)
        print()
        if results['env_added'] or results['config_added']:
            print(color('✓ Configuration updated!', Colors.GREEN))
        if results['warnings']:
            print()
            for warning in results['warnings']:
                print(color(f'  ⚠️  {warning}', Colors.YELLOW))
        print()
    elif subcmd == 'check':
        print()
        print(color('📋 Configuration Status', Colors.CYAN, Colors.BOLD))
        print()
        current_ver, latest_ver = check_config_version()
        if current_ver >= latest_ver:
            print(f'  Config version: {current_ver} ✓')
        else:
            print(color(f'  Config version: {current_ver} → {latest_ver} (update available)', Colors.YELLOW))
        print()
        print(color('  Required:', Colors.BOLD))
        for var_name in REQUIRED_ENV_VARS:
            if get_env_value(var_name):
                print(f'    ✓ {var_name}')
            else:
                print(color(f'    ✗ {var_name} (missing)', Colors.RED))
        print()
        print(color('  Optional:', Colors.BOLD))
        for var_name, info in OPTIONAL_ENV_VARS.items():
            if get_env_value(var_name):
                print(f'    ✓ {var_name}')
            else:
                tools = info.get('tools', [])
                tools_str = f" → {', '.join(tools[:2])}" if tools else ''
                print(color(f'    ○ {var_name}{tools_str}', Colors.DIM))
        missing_config = get_missing_config_fields()
        if missing_config:
            print()
            print(color(f'  {len(missing_config)} new config option(s) available', Colors.YELLOW))
            print("    Run 'duck-agent config migrate' to add them")
        print()
    else:
        print(f'Unknown config command: {subcmd}')
        print()
        print('Available commands:')
        print('  duck-agent config           Show current configuration')
        print('  duck-agent config edit      Open config in editor')
        print('  duck-agent config get <key>          Print a resolved config value')
        print('  duck-agent config set <key> <value>   Set a config value')
        print('  duck-agent config unset <key>        Remove a config value')
        print('  duck-agent config check     Check for missing/outdated config')
        print('  duck-agent config migrate   Update config with new options')
        print('  duck-agent config path      Show config file path')
        print('  duck-agent config env-path  Show .env file path')
        sys.exit(1)
_profile_env_vars_injected = False

def _inject_profile_env_vars() -> None:
    """Populate OPTIONAL_ENV_VARS from provider profiles not already listed.

    Called once at module load time. Idempotent — repeated calls are no-ops.
    """
    global _profile_env_vars_injected
    if _profile_env_vars_injected:
        return
    _profile_env_vars_injected = True
    try:
        from providers import list_providers
        for _pp in list_providers():
            if _pp.auth_type not in {'api_key'}:
                continue
            for _var in _pp.env_vars:
                if _var in OPTIONAL_ENV_VARS:
                    continue
                _is_key = not _var.endswith('_BASE_URL') and (not _var.endswith('_URL'))
                OPTIONAL_ENV_VARS[_var] = {'description': f"{_pp.display_name or _pp.name} {('API key' if _is_key else 'base URL override')}", 'prompt': f"{_pp.display_name or _pp.name} {('API key' if _is_key else 'base URL (leave empty for default)')}", 'url': _pp.signup_url or None, 'password': _is_key, 'category': 'provider', 'advanced': True}
    except Exception:
        pass
_inject_profile_env_vars()
_platform_plugin_env_vars_injected = False

def _inject_platform_plugin_env_vars() -> None:
    """Populate OPTIONAL_ENV_VARS from bundled platform plugin manifests.

    Called once at module load time. Idempotent — repeated calls are no-ops.
    Failures are swallowed so a malformed plugin.yaml can't break CLI import.
    """
    global _platform_plugin_env_vars_injected
    if _platform_plugin_env_vars_injected:
        return
    _platform_plugin_env_vars_injected = True
    try:
        import yaml
        repo_root = Path(__file__).resolve().parents[1]
        platforms_dir = repo_root / 'plugins' / 'platforms'
        if not platforms_dir.is_dir():
            return
        for child in platforms_dir.iterdir():
            if not child.is_dir():
                continue
            manifest_path = child / 'plugin.yaml'
            if not manifest_path.exists():
                manifest_path = child / 'plugin.yml'
            if not manifest_path.exists():
                continue
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = fast_safe_load(f) or {}
            except Exception:
                continue
            label = manifest.get('label') or manifest.get('name') or child.name
            entries = list(manifest.get('requires_env') or [])
            entries.extend(manifest.get('optional_env') or [])
            for entry in entries:
                if isinstance(entry, str):
                    name = entry
                    meta: dict = {}
                elif isinstance(entry, dict) and entry.get('name'):
                    name = entry['name']
                    meta = entry
                else:
                    continue
                if name in OPTIONAL_ENV_VARS:
                    continue
                name_upper = name.upper()
                is_secret = bool(meta.get('password') or meta.get('secret'))
                if not is_secret and (not meta.get('password') is False):
                    is_secret = any((name_upper.endswith(suf) for suf in ('_TOKEN', '_SECRET', '_KEY', '_PASSWORD', '_JSON')))
                OPTIONAL_ENV_VARS[name] = {'description': meta.get('description') or f'{label} configuration', 'prompt': meta.get('prompt') or name, 'url': meta.get('url') or None, 'password': is_secret, 'category': meta.get('category') or 'messaging'}
    except Exception:
        pass
_inject_platform_plugin_env_vars()