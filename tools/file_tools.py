"""File Tools Module - LLM agent file manipulation tools."""
import errno
import json
import logging
import os
import posixpath
import sys
import threading
from pathlib import Path, PurePosixPath
from agent.file_safety import get_read_block_error
from tools.binary_extensions import has_binary_extension
from tools.file_operations import ShellFileOperations, normalize_read_pagination, normalize_search_pagination
from tools import file_state
from agent.redact import redact_sensitive_text
logger = logging.getLogger(__name__)
_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}

def _expand_tilde(path: str) -> str:
    """Expand ``~`` using the effective profile home when available.

    In-process file tools share the gateway process's HOME, which may differ
    from the profile-specific HOME that interactive CLI sessions use.  This
    mirrors ``hermes_constants.get_subprocess_home()`` so that ``~`` resolves
    consistently regardless of whether the tool runs interactively or inside a
    gateway-driven cron job (#48552).
    """
    if not path or '~' not in path:
        return path
    try:
        from hermes_constants import get_subprocess_home
        home = get_subprocess_home()
    except Exception:
        home = None
    if home and (path == '~' or path.startswith('~/')):
        return home if path == '~' else os.path.join(home, path[2:])
    return os.path.expanduser(path)
_DEFAULT_MAX_READ_CHARS = 100000
_max_read_chars_cached: int | None = None

def _get_max_read_chars() -> int:
    """Return the configured max characters per file read.

    Reads ``file_read_max_chars`` from config.yaml on first call, caches
    the result for the lifetime of the process.  Falls back to the
    built-in default if the config is missing or invalid.
    """
    global _max_read_chars_cached
    if _max_read_chars_cached is not None:
        return _max_read_chars_cached
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        val = cfg.get('file_read_max_chars')
        if isinstance(val, (int, float)) and val > 0:
            _max_read_chars_cached = int(val)
            return _max_read_chars_cached
    except Exception:
        pass
    _max_read_chars_cached = _DEFAULT_MAX_READ_CHARS
    return _max_read_chars_cached

def _truncate_to_char_budget(content: str, max_chars: int) -> tuple[str, int, bool]:
    """Trim line-numbered ``read_file`` content to fit a char budget.

    Ported in spirit from nearai/ironclaw#5029 (dual line/byte cap on
    ``read_file``). Where duck-agent previously hard-rejected an oversized read
    (forcing the model to guess a smaller ``limit`` and burn a round-trip
    returning nothing), this trims the content to the last *complete line*
    that fits within ``max_chars`` and reports how many lines were kept so
    the caller can offer a ``next_offset`` continuation.

    ``content`` is the gutter-rendered text (``LINE_NUM|CONTENT`` joined by
    ``\\n``). Individual lines are already clamped to ``get_max_line_length()``
    upstream, so a single line never blows the whole budget on its own; the
    overflow this handles is the *accumulation* of many lines under the
    line-count limit (logs, wide CSV rows, minified data).

    Returns ``(kept_text, lines_kept, truncated)``. When ``content`` already
    fits, returns it unchanged with ``truncated=False``. If not even the
    first line fits, that single line is clamped on a code-point boundary
    (Python ``str`` slicing never splits a code point) so the read never
    returns empty and the cursor can still advance.
    """
    if len(content) <= max_chars:
        return (content, content.count('\n') + 1 if content else 0, False)
    lines = content.split('\n')
    kept: list[str] = []
    running = 0
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if running + addition > max_chars:
            break
        kept.append(line)
        running += addition
    if not kept:
        kept.append(lines[0][:max_chars])
    return ('\n'.join(kept), len(kept), True)
_LARGE_FILE_HINT_BYTES = 512000
_BLOCKED_DEVICE_PATHS = frozenset({'/dev/zero', '/dev/random', '/dev/urandom', '/dev/full', '/dev/stdin', '/dev/tty', '/dev/console', '/dev/stdout', '/dev/stderr', '/dev/fd/0', '/dev/fd/1', '/dev/fd/2'})

def _resolve_path(filepath: str, task_id: str='default') -> Path | PurePosixPath:
    """Resolve a path relative to TERMINAL_CWD (the worktree base directory)
    instead of the main repository root.
    """
    return _resolve_path_for_task(filepath, task_id)
_TERMINAL_CWD_SENTINELS = frozenset({'', '.', './', 'auto', 'cwd'})
_CONTAINER_PATH_BACKENDS_FALLBACK = frozenset({'docker', 'singularity', 'modal', 'daytona', 'vercel_sandbox'})

def _terminal_env_type_for_task(task_id: str='default') -> str:
    """Best-effort terminal backend type for path-resolution decisions."""
    try:
        from tools.terminal_tool import _active_environments, _env_lock, _get_env_config, _resolve_container_task_id
        try:
            container_key = _resolve_container_task_id(task_id)
        except Exception:
            container_key = task_id
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
        if env is not None:
            name = env.__class__.__name__.lower()
            if 'local' in name:
                return 'local'
            if 'ssh' in name:
                return 'ssh'
            if 'docker' in name:
                return 'docker'
            if 'singularity' in name:
                return 'singularity'
            if 'modal' in name:
                return 'modal'
            if 'daytona' in name:
                return 'daytona'
        cfg = _get_env_config()
        return str(cfg.get('env_type') or os.getenv('TERMINAL_ENV') or 'local').lower()
    except Exception:
        return str(os.getenv('TERMINAL_ENV') or 'local').lower()

def _uses_container_paths(task_id: str='default') -> bool:
    try:
        from tools.terminal_tool import _CONTAINER_BACKENDS
        container_backends = _CONTAINER_BACKENDS
    except Exception:
        container_backends = _CONTAINER_PATH_BACKENDS_FALLBACK
    return _terminal_env_type_for_task(task_id) in container_backends

def _normalize_without_host_deref(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Normalize path syntax without following host symlinks.

    Container backends use paths that are meaningful inside the sandbox. Calling
    ``Path.resolve()`` on the host can dereference a host-side symlink such as
    ``/workspace`` and rewrite the path before Docker sees it.
    """
    return PurePosixPath(posixpath.normpath(str(path)))

def _sentinel_free_abs_cwd(raw: str | None) -> str | None:
    """Normalize a cwd candidate to an absolute, sentinel-free anchor.

    Returns the expanded path only when *raw* is non-empty, not a sentinel (see
    ``_TERMINAL_CWD_SENTINELS``), and absolute. A relative anchor is meaningless
    without knowing which cwd it is relative to — exactly the ambiguity that
    misroutes worktree edits — so relative/sentinel/empty values yield ``None``.
    """
    raw = str(raw or '').strip()
    if raw.lower() in _TERMINAL_CWD_SENTINELS:
        return None
    expanded = _expand_tilde(raw)
    if not os.path.isabs(expanded):
        return None
    return expanded

def _configured_terminal_cwd() -> str | None:
    """Return ``$TERMINAL_CWD`` only when it names a real directory anchor.

    Sentinel values (see ``_TERMINAL_CWD_SENTINELS``) and relative paths are
    rejected — a relative anchor is meaningless without knowing which cwd it is
    relative to, which is exactly the ambiguity that misroutes worktree edits.
    Only an absolute, sentinel-free value is honored.
    """
    return _sentinel_free_abs_cwd(os.environ.get('TERMINAL_CWD'))

def _registered_task_cwd_override(task_id: str='default') -> str | None:
    """Return a registered cwd override for the raw task id, when available.

    ``terminal_tool`` intentionally collapses CWD-only task overrides to the
    shared ``"default"`` environment so TUI/dashboard/ACP sessions do not spin
    up isolated sandboxes just because they have different workspaces. The cwd
    value itself is still keyed by the raw session/task id, so file tools must
    read that raw override before falling back to the collapsed container key.
    """
    try:
        from tools.terminal_tool import resolve_task_overrides
        overrides = resolve_task_overrides(task_id)
    except Exception:
        return None
    return _sentinel_free_abs_cwd(overrides.get('cwd'))

def _authoritative_workspace_root(task_id: str='default') -> str | None:
    """Best-effort absolute workspace root for divergence checks.

    Resolution:

      1. The session's own cwd RECORD (``terminal_tool.get_session_cwd``) —
         written on every completed terminal command and seeded by workspace
         registration, keyed by the raw session id. Because the record is
         per-session, one session's ``cd`` can never leak into another
         session's resolution.
      2. A registered task/session cwd override (TUI/Desktop/ACP sessions
         register a raw-keyed cwd before any tool runs). Normally already
         mirrored into the record at registration; kept as a direct fallback
         so a cleared/never-written record still resolves the workspace.
      3. A sentinel-free absolute ``$TERMINAL_CWD`` (the worktree path set by
         ``cli.py``/``main.py`` for ``-w`` sessions).

    Returns ``None`` only when there is genuinely no reliable anchor, in which
    case callers fall back to the process cwd.
    """
    try:
        from tools.terminal_tool import get_session_cwd
        recorded = get_session_cwd(task_id)
    except Exception:
        recorded = None
    if recorded:
        return recorded
    registered = _registered_task_cwd_override(task_id)
    if registered:
        return registered
    return _configured_terminal_cwd()

def _resolve_base_dir(task_id: str='default', *, container_paths: bool | None=None) -> Path | PurePosixPath:
    """Return the ABSOLUTE base directory for resolving relative paths.

    Resolution order:
      1. The task's live terminal cwd (the directory the agent is actually
         working in — e.g. a git worktree). Authoritative when known.
      2. A registered task/session cwd override (TUI/Desktop/ACP sessions
         register a raw-keyed workspace cwd before any terminal command runs).
      3. A sentinel-free, absolute ``$TERMINAL_CWD`` (the worktree path set by
         ``cli.py``/``main.py`` for ``-w`` sessions). Used even before any
         terminal command has populated the live cwd registry.
      4. The process cwd.

    The returned base is ALWAYS absolute. This is the core invariant that
    prevents the worktree-cwd divergence bug: a relative or sentinel
    ``TERMINAL_CWD`` (commonly the literal ``"."`` from a stale config) is
    meaningless as a resolution anchor — left to ``Path.resolve()`` it silently
    resolves against whatever the agent PROCESS cwd happens to be (e.g. the main
    repo while the terminal is in a worktree), routing edits to the wrong
    checkout. We therefore reject sentinel/relative ``TERMINAL_CWD`` values
    outright (rather than anchoring them to the process cwd) and fall through to
    the process cwd only as a last resort, deterministically.
    """
    root = _authoritative_workspace_root(task_id)
    if container_paths is None:
        container_paths = _uses_container_paths(task_id)
    if root:
        base_text = _expand_tilde(root)
    else:
        base_text = os.getcwd()
    if container_paths:
        if not posixpath.isabs(base_text):
            base_text = posixpath.join(os.getcwd(), base_text)
        return _normalize_without_host_deref(base_text)
    from tools.environments.local import _msys_to_windows_path
    base_text = _msys_to_windows_path(base_text)
    if sys.platform == 'win32':
        import ntpath
        if not ntpath.isabs(base_text):
            base_text = ntpath.join(os.getcwd(), base_text)
        return Path(ntpath.normpath(base_text))
    base = Path(base_text)
    if not base.is_absolute():
        base = Path(os.getcwd()) / base
    return base.resolve()

def _resolve_path_for_task(filepath: str, task_id: str='default') -> Path | PurePosixPath:
    """Resolve *filepath* against the task's absolute base directory.

    See :func:`_resolve_base_dir` for how the base is chosen. Absolute input
    paths are returned resolved-but-unanchored.

    On native Windows, Git Bash / MSYS drive paths (``/c/Users/...``) are
    translated to ``C:\\Users\\...`` before resolution so file tools don't
    treat them as relative ``\\c\\Users\\...`` under the process cwd.
    """
    container_paths = _uses_container_paths(task_id)
    if container_paths:
        expanded = _expand_tilde(filepath)
        if posixpath.isabs(expanded):
            return _normalize_without_host_deref(expanded)
        resolved = _resolve_base_dir(task_id, container_paths=True) / expanded
        return _normalize_without_host_deref(resolved)
    from tools.environments.local import _msys_to_windows_path
    expanded = _expand_tilde(_msys_to_windows_path(filepath))
    if sys.platform == 'win32':
        import ntpath
        if ntpath.isabs(expanded):
            return Path(ntpath.normpath(expanded))
        joined = ntpath.join(str(_resolve_base_dir(task_id, container_paths=False)), expanded)
        return Path(ntpath.normpath(joined))
    p = Path(expanded)
    if p.is_absolute():
        return p.resolve()
    resolved = _resolve_base_dir(task_id, container_paths=False) / p
    return resolved.resolve()

def _path_resolution_warning(filepath: str, resolved: Path, task_id: str='default') -> str | None:
    """Warn when a relative path resolved OUTSIDE the task's workspace root.

    Surfaces the worktree-cwd divergence the moment it would matter: if the
    agent passes a relative path but it resolves under a directory that is not
    the workspace root (i.e. the edit is about to land in a different checkout
    than the one the agent is working in), return a message naming the absolute
    target. ``None`` when the path is absolute, the base is unknown, or the
    resolved path is correctly under the workspace root.

    The workspace root is the live terminal cwd when known, else a registered
    task/session cwd override, else a sentinel-free absolute ``$TERMINAL_CWD``
    — so a worktree or Desktop session whose terminal registry is still empty
    (no ``cd`` run yet) is warned on the very first write.
    """
    try:
        if Path(_expand_tilde(filepath)).is_absolute():
            return None
        workspace_root = _authoritative_workspace_root(task_id)
        if not workspace_root:
            return None
        if _uses_container_paths(task_id):
            root = _normalize_without_host_deref(Path(_expand_tilde(workspace_root)))
        else:
            root = Path(_expand_tilde(workspace_root)).resolve()
        try:
            resolved.relative_to(root)
            return None
        except ValueError:
            return f"Relative path {filepath!r} resolved to {str(resolved)!r}, which is OUTSIDE the active workspace ({str(root)!r}). The edit will land in a different directory than the terminal's cwd. If this is not intended (e.g. a git-worktree session writing into the main checkout), pass an absolute path under the workspace instead."
    except Exception:
        return None

def _file_ops_uses_host_paths(file_ops) -> bool:
    """Return True when *file_ops* targets the same host filesystem as Duck Agent.

    Only then may we rewrite V4A header paths to resolved host-absolute
    paths: a container/remote backend has its own filesystem namespace where
    a host-absolute path would be meaningless.
    """
    env = getattr(file_ops, 'env', None)
    if env is None:
        return True
    try:
        from tools.environments.local import LocalEnvironment
    except ImportError:
        return True
    return isinstance(env, LocalEnvironment)

def _rewrite_v4a_patch_paths_for_host(patch: str, path_to_resolved: dict, file_ops) -> str:
    """Rewrite V4A file headers to the exact host paths the tool layer resolved.

    ``patch_tool`` resolves every header path against the task's workspace for
    locking, staleness, and reporting, but historically handed the *original*
    patch text to ``file_ops.patch_v4a`` — so the shell layer re-resolved the
    (often relative) header against its own cwd, which can differ from the
    tool layer's workspace (the git-worktree cwd bug). That made a relative
    header land in a different directory than everything else the tool
    reported. This rewrites ``*** Update/Add/Delete/Move File:`` headers to the
    resolved absolute paths so both layers agree on the target.

    Header patterns mirror ``patch_parser`` (``\\s*`` after ``***`` accepts the
    no-space ``***Update File:`` form) and cover ``Move File: src -> dst``.
    Only applied when *file_ops* targets the host filesystem.
    """
    if not _file_ops_uses_host_paths(file_ops):
        return patch
    import re as _re

    def _resolved_or_original(raw: str) -> str:
        raw = raw.strip()
        return path_to_resolved.get(raw) or raw

    def _replace_single(match):
        prefix = match.group(1)
        resolved = _resolved_or_original(match.group(2))
        return f'{prefix}{resolved}'
    patch = _re.sub('^(\\*\\*\\*\\s*(?:Update|Add|Delete)\\s+File:\\s*)(.+)$', _replace_single, patch, flags=_re.MULTILINE)

    def _replace_move(match):
        prefix = match.group(1)
        src = _resolved_or_original(match.group(2))
        dst = _resolved_or_original(match.group(3))
        return f'{prefix}{src} -> {dst}'
    patch = _re.sub('^(\\*\\*\\*\\s*Move\\s+File:\\s*)(.+?)\\s*->\\s*(.+)$', _replace_move, patch, flags=_re.MULTILINE)
    return patch

def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.normpath(_expand_tilde(path))
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith('/proc/') and normalized.endswith(('/fd/0', '/fd/1', '/fd/2')):
        return True
    if normalized.startswith('/proc/') and normalized.endswith(('/environ', '/cmdline', '/maps', '/smaps', '/smaps_rollup', '/numa_maps', '/mem', '/auxv', '/pagemap')):
        return True
    return False

def _is_blocked_device(filepath: str, base_dir: str | Path | None=None) -> bool:
    """Return True if the path would hang the process (infinite output or blocking input).

    Check the literal path first so aliases like /dev/stdin are caught before
    they resolve to terminal-specific paths. Then check each symlink hop before
    the final resolved path so aliases to devices cannot bypass the guard.
    """
    expanded = _expand_tilde(filepath)
    if base_dir is not None and (not os.path.isabs(expanded)):
        expanded = os.path.join(os.fspath(base_dir), expanded)
    normalized = os.path.normpath(expanded)
    if _is_blocked_device_path(normalized):
        return True
    seen: set[str] = set()
    current = normalized
    for _ in range(20):
        try:
            target = os.readlink(current)
        except OSError:
            break
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        target = os.path.normpath(target)
        if _is_blocked_device_path(target):
            return True
        if target in seen:
            break
        seen.add(target)
        current = target
    try:
        resolved = os.path.normpath(os.path.realpath(normalized))
    except (OSError, ValueError):
        return False
    if _is_blocked_device_path(resolved):
        return True
    return False

def _search_result_read_block_error(path: str, task_id: str='default') -> str | None:
    """Return the read-safety error for a search result path.

    Search backends may return paths relative to the task cwd, while
    ``get_read_block_error`` expects an already-resolved path when the task cwd
    can differ from the Python process cwd. Mirror ``read_file_tool``'s path
    resolution before applying the shared read guard.
    """
    try:
        resolved = _resolve_path_for_task(path, task_id)
    except (OSError, ValueError, RuntimeError):
        return get_read_block_error(path)
    return get_read_block_error(str(resolved))

def _filter_read_blocked_search_results(result, task_id: str='default') -> int:
    """Remove credential/cache/env paths from a SearchResult in-place."""
    omitted = 0
    if hasattr(result, 'matches') and result.matches:
        allowed_matches = []
        for match in result.matches:
            if _search_result_read_block_error(match.path, task_id):
                omitted += 1
                continue
            allowed_matches.append(match)
        result.matches = allowed_matches
    if hasattr(result, 'files') and result.files:
        allowed_files = []
        for file_path in result.files:
            if _search_result_read_block_error(file_path, task_id):
                omitted += 1
                continue
            allowed_files.append(file_path)
        result.files = allowed_files
    if hasattr(result, 'counts') and result.counts:
        allowed_counts = {}
        for file_path, count in result.counts.items():
            if _search_result_read_block_error(file_path, task_id):
                omitted += 1
                continue
            allowed_counts[file_path] = count
        result.counts = allowed_counts
    return omitted
_SENSITIVE_PATH_PREFIXES = ('/etc/', '/boot/', '/usr/lib/systemd/', '/private/etc/', '/private/var/db/', '/private/var/root/')
_SENSITIVE_EXACT_PATHS = {'/var/run/docker.sock', '/run/docker.sock'}
_hermes_config_resolved: str | None = None
_hermes_config_resolved_loaded = False

def _get_hermes_config_resolved() -> str | None:
    """Return the resolved absolute path of the Duck Agent config file (cached)."""
    global _hermes_config_resolved, _hermes_config_resolved_loaded
    if _hermes_config_resolved_loaded:
        return _hermes_config_resolved
    _hermes_config_resolved_loaded = True
    try:
        from hermes_cli.config import get_config_path
        _hermes_config_resolved = str(get_config_path().resolve())
    except Exception:
        try:
            _hermes_config_resolved = str(Path(_expand_tilde('~/.duck-agent/config.yaml')).resolve())
        except Exception:
            _hermes_config_resolved = None
    return _hermes_config_resolved

def _check_sensitive_path(filepath: str, task_id: str='default') -> str | None:
    """Return an error message if the path targets a sensitive system location."""
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(_expand_tilde(filepath))
    _err = f'Refusing to write to sensitive system path: {filepath}\nUse the terminal tool with sudo if you need to modify system files.'
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _err
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _err
    hermes_config = _get_hermes_config_resolved()
    if hermes_config and (resolved == hermes_config or normalized == hermes_config):
        return f"Refusing to write to Duck Agent config file: {filepath}\nAgent cannot modify security-sensitive configuration. Edit ~/.duck-agent/config.yaml directly or use 'duck-agent config' instead."
    return None
_PROTECTED_INSTRUCTION_BASENAMES = frozenset({'agents.md', 'claude.md', 'soul.md', '.cursorrules'})
_real_hermes_home_cached: str | None = None
_real_hermes_home_loaded = False

def _get_real_hermes_home() -> str | None:
    """Return the realpath of the authoritative Duck Agent home (cached)."""
    global _real_hermes_home_cached, _real_hermes_home_loaded
    if _real_hermes_home_loaded:
        return _real_hermes_home_cached
    _real_hermes_home_loaded = True
    try:
        from hermes_constants import get_hermes_home
        _real_hermes_home_cached = os.path.realpath(str(get_hermes_home()))
    except Exception:
        try:
            _real_hermes_home_cached = os.path.realpath(_expand_tilde('~/.duck-agent'))
        except Exception:
            _real_hermes_home_cached = None
    return _real_hermes_home_cached

def _protected_instruction_config() -> tuple[bool, list[str]]:
    """Read the protected-instruction-files gate config.

    Returns ``(enabled, extra_patterns)``. Defaults to enabled with no extra
    patterns; config read failures keep the gate ON (fail-safe for a
    security boundary).

    Config keys (config.yaml)::

        security:
          protected_instruction_files: true       # default
          protected_instruction_extra_patterns: []  # fnmatch on basename
    """
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        enabled = cfg_get(cfg, 'security', 'protected_instruction_files', default=True)
        extra = cfg_get(cfg, 'security', 'protected_instruction_extra_patterns', default=[])
    except Exception:
        return (True, [])
    if not isinstance(enabled, bool):
        enabled = True
    if not isinstance(extra, list):
        extra = []
    return (enabled, [str(p) for p in extra if p])

def _protected_instruction_reason(filepath: str, task_id: str='default', *, enabled: bool | None=None, extra_patterns: list[str] | None=None) -> str | None:
    """Return a short label when ``filepath`` targets a protected
    agent-instruction file, else ``None``.

    Matching runs on BOTH the normalized input path and its realpath so
    neither a symlink pointing AT a protected file (#41351) nor a protected
    name that is itself a symlink escapes the gate. ``..`` traversal is
    neutralized by normpath/realpath before the basename compare.
    """
    if enabled is None or extra_patterns is None:
        enabled, extra_patterns = _protected_instruction_config()
    if not enabled:
        return None
    normalized = os.path.normpath(_expand_tilde(filepath))
    try:
        resolved = os.path.realpath(str(_resolve_path_for_task(filepath, task_id)))
    except (OSError, ValueError, RuntimeError):
        resolved = os.path.realpath(normalized)
    real_home = _get_real_hermes_home()
    if real_home and (resolved == real_home or resolved.startswith(real_home + os.sep)):
        return None
    import fnmatch
    for candidate in (normalized, resolved):
        base = os.path.basename(candidate)
        base_lower = base.lower()
        if base_lower in _PROTECTED_INSTRUCTION_BASENAMES:
            return base
        for pattern in extra_patterns:
            if fnmatch.fnmatch(base_lower, pattern.lower()):
                return base
        parts = candidate.replace('\\', '/').rstrip('/').split('/')
        if len(parts) >= 2 and parts[-2] == '.duck-agent':
            return candidate
    return None

def _request_protected_instruction_approval(reasons: list[str], task_id: str='default') -> str | None:
    """Ask the human to approve a write to protected instruction file(s).

    Returns ``None`` when approved, or a BLOCKED error string. This gate
    intentionally does NOT route through ``_run_approval_gate``: that gate
    honors --yolo and session/permanent allowlists, and the entire point
    here is one-operation approval EVERY time, with no persistent scope
    and no yolo bypass. Fail-closed when no human channel exists.
    """
    targets = ', '.join(dict.fromkeys(reasons))
    description = f'Write to protected agent-instruction file(s): {targets}. These files steer future agent behavior; approval is always required (not bypassed by auto-approve).'
    display = f'<write to {targets}>'
    blocked = f'BLOCKED: write to protected agent-instruction file(s) ({targets}) {{why}} The user has NOT consented to this write. Do NOT retry it or attempt the same edit via another path (terminal, execute_code, etc.).'
    try:
        import tools.approval as _approval
    except Exception:
        return blocked.format(why='requires approval but the approval subsystem is unavailable.')
    session_key = _approval.get_current_session_key()
    notify_cb = None
    try:
        with _approval._lock:
            notify_cb = _approval._gateway_notify_cbs.get(session_key)
    except Exception:
        notify_cb = None
    if notify_cb is not None:
        approval_data = {'command': display, 'pattern_key': 'protected_instruction_file', 'pattern_keys': ['protected_instruction_file'], 'description': description, 'allow_permanent': False, 'allow_session': False}
        decision = _approval._await_gateway_decision(session_key, notify_cb, approval_data, surface='gateway')
        if decision.get('notify_failed'):
            return blocked.format(why='requires approval but the approval request could not be delivered.')
        choice = decision.get('choice')
        if decision.get('resolved') and choice in {'once', 'session', 'always'}:
            return None
        if not decision.get('resolved'):
            return blocked.format(why='approval prompt timed out without a user response. Silence is not consent.')
        return blocked.format(why='was denied by the user.')
    callback = None
    try:
        from tools.terminal_tool import _get_approval_callback
        callback = _get_approval_callback()
    except Exception:
        callback = None
    if callback is not None:
        choice = _approval.prompt_dangerous_approval(display, description, allow_permanent=False, approval_callback=callback)
        if choice in {'once', 'session', 'always'}:
            return None
        if choice == 'timeout':
            return blocked.format(why='approval prompt timed out without a user response. Silence is not consent.')
        return blocked.format(why='was denied by the user.')
    return blocked.format(why='requires approval but no interactive user or gateway is present to approve it.')

def _check_protected_instruction_write(paths: list[str], task_id: str='default') -> str | None:
    """Gate a write/patch touching protected instruction files.

    Returns ``None`` when no target is protected or the human approved;
    otherwise a BLOCKED error string. For multi-file V4A patches, ONE
    protected file gates the ENTIRE patch: a single prompt lists every
    protected target, and a deny applies nothing (including innocent
    files) — partial application of an approved-in-part patch would be
    more surprising than an atomic all-or-nothing outcome.
    """
    enabled, extra = _protected_instruction_config()
    if not enabled:
        return None
    reasons: list[str] = []
    for p in paths:
        reason = _protected_instruction_reason(p, task_id, enabled=enabled, extra_patterns=extra)
        if reason:
            reasons.append(reason)
    if not reasons:
        return None
    return _request_protected_instruction_approval(reasons, task_id)

def _get_container_mirror_prefix_for_task(task_id: str='default') -> str | None:
    """Return the container-side Duck Agent mirror prefix for Docker file tools."""
    try:
        from tools.terminal_tool import _active_environments, _env_lock, _get_env_config, _resolve_container_task_id
        container_key = _resolve_container_task_id(task_id)
    except Exception:
        return None
    try:
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
        if env is not None:
            if env.__class__.__name__ == 'DockerEnvironment' and bool(getattr(env, '_persistent', False)):
                return '/root/.duck-agent'
            return None
        config = _get_env_config()
    except Exception:
        return None
    if config.get('env_type') == 'docker' and config.get('container_persistent', True):
        return '/root/.duck-agent'
    return None

def _check_cross_profile_path(filepath: str, task_id: str='default') -> str | None:
    """Return a soft-guard warning when ``filepath`` lands in another Duck Agent
    profile's scoped area, a host-side sandbox-mirror of authoritative profile
    state, or the Docker container's sandbox mirror of Duck Agent state.

    Three detectors run in order:

    * cross-profile — writes that hit another profile's
      ``skills/plugins/cron/memories`` directory.
    * sandbox-mirror (#32049) — writes that hit the
      ``…/sandboxes/<backend>/<task>/home/.duck-agent/…`` mirror created by a
      non-local terminal backend (Docker, Daytona, etc.), where the host
      Duck Agent process never reads the mirror and the authoritative file is
      left untouched.
    * container-mirror (#32049 follow-up) — writes from inside a Docker
      container whose bind-mounted home strips the ``sandboxes/`` prefix, so
      the agent sees a plain ``/root/.duck-agent/…`` path.

    Returns ``None`` when the write is in-scope or outside Duck Agent scope.
    All detectors are soft guards — the agent can override any by
    passing ``cross_profile=True`` to its write tool after explicit user
    direction. Defense-in-depth, NOT a security boundary — the terminal
    tool runs as the same OS user and can write any of these paths
    directly. See ``agent/file_safety.classify_cross_profile_target``,
    ``classify_sandbox_mirror_target`` and ``classify_container_mirror_target``
    for the detection rules.
    """
    try:
        from agent.file_safety import get_container_mirror_warning, get_cross_profile_warning, get_sandbox_mirror_warning
    except Exception:
        return None
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    warning = get_cross_profile_warning(resolved)
    if warning is not None:
        return warning
    warning = get_sandbox_mirror_warning(resolved)
    if warning is not None:
        return warning
    return get_container_mirror_warning(resolved, mirror_prefix=_get_container_mirror_prefix_for_task(task_id))

def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False
_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}
_patch_failure_lock = threading.Lock()
_patch_failure_tracker: dict = {}

def _record_patch_failure(task_id: str, resolved_path: str) -> int:
    """Increment and return the consecutive-failure count for this path."""
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.setdefault(task_id, {})
        if len(task_failures) >= 64 and resolved_path not in task_failures:
            try:
                first_key = next(iter(task_failures))
                del task_failures[first_key]
            except StopIteration:
                pass
        task_failures[resolved_path] = task_failures.get(resolved_path, 0) + 1
        return task_failures[resolved_path]

def _reset_patch_failures(task_id: str, resolved_paths: list) -> None:
    """Clear consecutive-failure counts for the given paths."""
    if not resolved_paths:
        return
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.get(task_id)
        if not task_failures:
            return
        for rp in resolved_paths:
            task_failures.pop(rp, None)
_READ_HISTORY_CAP = 500
_DEDUP_CAP = 1000
_READ_TIMESTAMPS_CAP = 1000
_NOT_FOUND_CAP = 500
_NOT_FOUND_TTL_SECONDS = 60.0
_READ_DEDUP_STATUS_MESSAGE = 'File unchanged since last read. The content from the earlier read_file result in this conversation is still current — refer to that instead of re-reading.'

def _cap_read_tracker_data(task_data: dict) -> None:
    """Enforce size caps on the per-task read-tracker sub-containers.

    Must be called with ``_read_tracker_lock`` held.  Eviction policy:

      * ``read_history`` (set): pop arbitrary entries on overflow.  This
        is fine because the set only feeds diagnostic summaries; losing
        old entries just trims the summary's tail.
      * ``dedup`` / ``read_timestamps`` (dict): pop oldest by insertion
        order (Python 3.7+ dicts).  Evicted entries lose their dedup
        skip on a future re-read (the file gets re-sent once) and
        external-edit mtime comparison (the write/patch falls back to
        a non-mtime check).  Both are graceful degradations, not bugs.
    """
    rh = task_data.get('read_history')
    if rh is not None and len(rh) > _READ_HISTORY_CAP:
        excess = len(rh) - _READ_HISTORY_CAP
        for _ in range(excess):
            try:
                rh.pop()
            except KeyError:
                break
    dedup = task_data.get('dedup')
    if dedup is not None and len(dedup) > _DEDUP_CAP:
        excess = len(dedup) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup.pop(next(iter(dedup)))
            except (StopIteration, KeyError):
                break
    dedup_hits = task_data.get('dedup_hits')
    if dedup_hits is not None and len(dedup_hits) > _DEDUP_CAP:
        excess = len(dedup_hits) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup_hits.pop(next(iter(dedup_hits)))
            except (StopIteration, KeyError):
                break
    ts = task_data.get('read_timestamps')
    if ts is not None and len(ts) > _READ_TIMESTAMPS_CAP:
        excess = len(ts) - _READ_TIMESTAMPS_CAP
        for _ in range(excess):
            try:
                ts.pop(next(iter(ts)))
            except (StopIteration, KeyError):
                break
    nf = task_data.get('not_found')
    if nf is not None and len(nf) > _NOT_FOUND_CAP:
        excess = len(nf) - _NOT_FOUND_CAP
        for _ in range(excess):
            try:
                nf.pop(next(iter(nf)))
            except (StopIteration, KeyError):
                break

def _check_not_found_cache(op: str, resolved_str: str, task_id: str) -> str | None:
    """Return cached not-found JSON for *(op, resolved_str)* if still fresh.

    Skips the expensive subprocess + suggestion walk when the model retries
    the same missing path. Observed in agent.log: a single typo'd path was
    retried 13 times — each retry forked a shell to walk the parent directory
    and score similar names.

    *op* is "read" or "search" — kept separate because the two callers return
    different error JSON shapes ("File not found:" vs "Path not found:").

    Eviction: TTL or write_file/patch on the path (see invalidate_for_path).
    """
    import os as _os
    import time
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        nf = task_data.get('not_found')
        if not nf:
            return None
        entry = nf.get((op, resolved_str))
        if entry is None:
            return None
        ts, cached_json = entry
        if time.monotonic() - ts > _NOT_FOUND_TTL_SECONDS:
            nf.pop((op, resolved_str), None)
            return None
    if _os.path.exists(resolved_str):
        with _read_tracker_lock:
            task_data = _read_tracker.get(task_id)
            nf = task_data.get('not_found') if task_data else None
            if nf:
                nf.pop((op, resolved_str), None)
        return None
    return cached_json

def _record_not_found(op: str, resolved_str: str, task_id: str, error_json: str) -> None:
    """Cache a not-found error so the next *op* call for *resolved_str* skips I/O."""
    import time
    with _read_tracker_lock:
        task_data = _read_tracker.setdefault(task_id, {'last_key': None, 'consecutive': 0, 'read_history': set(), 'dedup': {}, 'dedup_hits': {}, 'read_timestamps': {}})
        nf = task_data.setdefault('not_found', {})
        nf[op, resolved_str] = (time.monotonic(), error_json)
        _cap_read_tracker_data(task_data)

def _is_internal_file_status_text(content: str) -> bool:
    """Return True when content looks like an internal file-tool status, not real file bytes.

    The read_file dedup status message must never be persisted as file
    content.  The obvious shape is the model echoing the message verbatim,
    but in practice it also wraps it with small framing text (a leading
    "Note:", a trailing newline + short comment, etc.) before calling
    write_file.  We treat any short-ish write whose body is dominated by
    the status message as the same class of corruption.

    Heuristic:
      * Strict equality (after strip) — the verbatim shape.
      * OR the stripped content contains the full status message AND is
        short enough that the status dominates it (<=2x the message length).
        Short, status-dominated writes can't plausibly be real files —
        legitimate docs/notes that happen to quote this internal message
        are always dramatically longer.
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if stripped == _READ_DEDUP_STATUS_MESSAGE:
        return True
    if _READ_DEDUP_STATUS_MESSAGE in stripped and len(stripped) <= 2 * len(_READ_DEDUP_STATUS_MESSAGE):
        return True
    return False

def _looks_like_read_file_line_numbered_content(content: str) -> bool:
    """Return True for content dominated by read_file's ``LINE_NUM|CONTENT`` display.

    ``read_file`` intentionally returns line-numbered text to the model. If
    that display format is echoed into ``write_file``, config/source files are
    silently corrupted with prefixes like `` 1|``.  We reject writes where the
    non-empty lines are mostly consecutive read_file-style numbered lines, while
    allowing sparse literal pipe content such as a single ``1|value`` line.
    """
    if not isinstance(content, str):
        return False
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    numbered: list[int] = []
    for line in lines:
        stripped = line.lstrip()
        prefix, sep, _rest = stripped.partition('|')
        if sep and prefix.isdigit():
            numbered.append(int(prefix))
    if len(numbered) < 2:
        return False
    if len(numbered) / len(lines) < 0.6:
        return False
    consecutive_pairs = sum((1 for prev, current in zip(numbered, numbered[1:]) if current == prev + 1))
    return consecutive_pairs >= len(numbered) - 1

def _is_internal_file_tool_content(content: str) -> bool:
    """Return True when content is file-tool display text, not intended file bytes."""
    return _is_internal_file_status_text(content) or _looks_like_read_file_line_numbered_content(content)

def _get_file_ops(task_id: str='default') -> ShellFileOperations:
    """Get or create ShellFileOperations for a terminal environment.

    Respects the TERMINAL_ENV setting -- if the task_id doesn't have an
    environment yet, creates one using the configured backend (local, docker,
    modal, etc.) rather than always defaulting to local.

    Thread-safe: uses the same per-task creation locks as terminal_tool to
    prevent duplicate sandbox creation from concurrent tool calls.

    Note: subagent task_ids are collapsed to "default" via
    ``_resolve_container_task_id`` so delegate_task children share the
    parent's container and its cached file_ops. RL/benchmark task_ids with
    a registered env override keep their isolation.
    """
    from tools.terminal_tool import _active_environments, _env_lock, _create_environment, _get_env_config, _last_activity, _start_cleanup_thread, _creation_locks, _creation_locks_lock, _resolve_container_task_id, _is_unusable_container_cwd, _CONTAINER_BACKENDS
    import time
    raw_task_id = task_id or 'default'
    task_id = _resolve_container_task_id(raw_task_id)
    with _file_ops_lock:
        cached = _file_ops_cache.get(task_id)
    if cached is not None:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                return cached
            else:
                old_cwd = getattr(cached, 'cwd', None)
                if old_cwd:
                    try:
                        from tools.terminal_tool import record_session_cwd
                        record_session_cwd(raw_task_id, old_cwd)
                    except Exception:
                        pass
                with _file_ops_lock:
                    _file_ops_cache.pop(task_id, None)
    with _creation_locks_lock:
        if task_id not in _creation_locks:
            _creation_locks[task_id] = threading.Lock()
        task_lock = _creation_locks[task_id]
    with task_lock:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                terminal_env = _active_environments[task_id]
            else:
                terminal_env = None
        if terminal_env is None:
            from tools.terminal_tool import resolve_task_overrides
            config = _get_env_config()
            env_type = config['env_type']
            overrides = resolve_task_overrides(raw_task_id)
            if env_type == 'docker':
                image = overrides.get('docker_image') or config['docker_image']
            elif env_type == 'singularity':
                image = overrides.get('singularity_image') or config['singularity_image']
            elif env_type == 'modal':
                image = overrides.get('modal_image') or config['modal_image']
            elif env_type == 'daytona':
                image = overrides.get('daytona_image') or config['daytona_image']
            else:
                image = ''
            try:
                from tools.terminal_tool import get_session_cwd
                recorded_cwd = get_session_cwd(raw_task_id)
            except Exception:
                recorded_cwd = None
            cwd = overrides.get('cwd') or recorded_cwd or config['cwd']
            if env_type in _CONTAINER_BACKENDS and _is_unusable_container_cwd(cwd):
                if cwd != config['cwd']:
                    logger.info("Ignoring host/relative cwd override %r for %s backend (won't exist in sandbox). Using %r instead.", cwd, env_type, config['cwd'])
                cwd = config['cwd']
            logger.info('Creating new %s environment for task %s...', env_type, task_id[:8])
            container_config = None
            if env_type in {'docker', 'singularity', 'modal', 'daytona', 'vercel_sandbox'}:
                container_config = {'container_cpu': config.get('container_cpu', 1), 'container_memory': config.get('container_memory', 5120), 'container_disk': config.get('container_disk', 51200), 'container_persistent': config.get('container_persistent', True), 'vercel_runtime': config.get('vercel_runtime', ''), 'docker_volumes': config.get('docker_volumes', []), 'docker_mount_cwd_to_workspace': config.get('docker_mount_cwd_to_workspace', False), 'docker_forward_env': config.get('docker_forward_env', []), 'docker_run_as_host_user': config.get('docker_run_as_host_user', False), 'docker_network': config.get('docker_network', True)}
            ssh_config = None
            if env_type == 'ssh':
                ssh_config = {'host': config.get('ssh_host', ''), 'user': config.get('ssh_user', ''), 'port': config.get('ssh_port', 22), 'key': config.get('ssh_key', ''), 'persistent': config.get('ssh_persistent', False)}
            local_config = None
            if env_type == 'local':
                local_config = {'persistent': config.get('local_persistent', False)}
            terminal_env = _create_environment(env_type=env_type, image=image, cwd=cwd, timeout=config['timeout'], ssh_config=ssh_config, container_config=container_config, local_config=local_config, task_id=task_id, host_cwd=config.get('host_cwd'))
            with _env_lock:
                _active_environments[task_id] = terminal_env
                _last_activity[task_id] = time.time()
            _start_cleanup_thread()
            logger.info('%s environment ready for task %s', env_type, task_id[:8])
    file_ops = ShellFileOperations(terminal_env)
    with _file_ops_lock:
        _file_ops_cache[task_id] = file_ops
    return file_ops

def clear_file_ops_cache(task_id: str=None):
    """Clear the file operations cache."""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()

def read_file_tool(path: str, offset: int=1, limit: int=2000, task_id: str='default') -> str:
    """Read a file with pagination and line numbers."""
    try:
        offset, limit = normalize_read_pagination(offset, limit)
        device_base = None if Path(path).expanduser().is_absolute() else _resolve_base_dir(task_id)
        if _is_blocked_device(path, base_dir=device_base):
            return tool_error(f"Cannot read '{path}': this is a device file that would block or produce infinite output.")
        _resolved = _resolve_path_for_task(path, task_id)
        from tools.read_extract import ExtractionError, extract_document_text, is_extractable_document
        if is_extractable_document(str(_resolved)):
            try:
                extracted_text = extract_document_text(str(_resolved))
            except ExtractionError:
                logger.debug('document extraction failed for %s', path, exc_info=True)
            else:
                file_ops = _get_file_ops(task_id)
                lines = extracted_text.splitlines()
                total_lines = len(lines)
                end_line = offset + limit - 1
                page_text = '\n'.join(lines[offset - 1:end_line])
                result_dict = {'content': file_ops._add_line_numbers(page_text, offset) if page_text else '', 'total_lines': total_lines, 'file_size': os.path.getsize(_resolved), 'truncated': total_lines > end_line, 'extracted_document': True}
                if result_dict['truncated']:
                    result_dict['hint'] = f'Use offset={end_line + 1} to continue reading (showing {offset}-{min(end_line, total_lines)} of {total_lines} lines)'
                content_len = len(result_dict['content'])
                max_chars = _get_max_read_chars()
                if content_len > max_chars:
                    trimmed, lines_kept, _ = _truncate_to_char_budget(result_dict['content'], max_chars)
                    next_offset = offset + lines_kept
                    shown_end = offset + lines_kept - 1
                    result_dict['content'] = trimmed
                    result_dict['truncated'] = True
                    result_dict['truncated_by'] = 'bytes'
                    result_dict['next_offset'] = next_offset
                    result_dict['hint'] = f'Output truncated at the {max_chars:,}-char read budget after {lines_kept} line(s) (showing lines {offset}-{shown_end} of {total_lines}). Use offset={next_offset} to continue.'
                    if len(trimmed.split('\n', 1)[0]) >= max_chars:
                        result_dict['hint'] += ' Note: the first line alone exceeded the budget and was clamped mid-line; its remainder is not retrievable via offset.'
                if result_dict['content']:
                    result_dict['content'] = redact_sensitive_text(result_dict['content'], file_read=True)
                return json.dumps(result_dict, ensure_ascii=False)
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            return tool_error(f"Cannot read binary file '{path}' ({_ext}). Use vision_analyze for images, or terminal to inspect binary files.")
        block_error = get_read_block_error(str(_resolved))
        if block_error:
            return tool_error(block_error)
        resolved_str_for_neg = str(_resolved)
        cached_not_found = _check_not_found_cache('read', resolved_str_for_neg, task_id)
        if cached_not_found is not None:
            return cached_not_found
        resolved_str = str(_resolved)
        dedup_key = (resolved_str, offset, limit)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {'last_key': None, 'consecutive': 0, 'read_history': set(), 'dedup': {}, 'dedup_hits': {}, 'read_timestamps': {}})
            if 'dedup_hits' not in task_data:
                task_data['dedup_hits'] = {}
            if 'read_timestamps' not in task_data:
                task_data['read_timestamps'] = {}
            cached_mtime = task_data.get('dedup', {}).get(dedup_key)
        if cached_mtime is not None:
            try:
                current_mtime = os.path.getmtime(resolved_str)
                if current_mtime == cached_mtime:
                    with _read_tracker_lock:
                        hits = task_data['dedup_hits'].get(dedup_key, 0) + 1
                        task_data['dedup_hits'][dedup_key] = hits
                        _cap_read_tracker_data(task_data)
                    if hits >= 2:
                        return tool_error(f'BLOCKED: You have called read_file on this exact region {hits + 1} times and the file has NOT changed. STOP calling read_file for this path — the content from your earlier read_file result in this conversation is still current. Proceed with your task using the information you already have.', path=path, already_read=hits + 1)
                    return json.dumps({'status': 'unchanged', 'message': _READ_DEDUP_STATUS_MESSAGE, 'path': path, 'dedup': True, 'content_returned': False}, ensure_ascii=False)
            except OSError:
                pass
        file_ops = _get_file_ops(task_id)
        result = file_ops.read_file(path, offset, limit)
        result_dict = result.to_dict()
        _err = result_dict.get('error') or ''
        if isinstance(_err, str) and _err.startswith('File not found:'):
            _not_found_json = json.dumps(result_dict, ensure_ascii=False)
            _record_not_found('read', resolved_str_for_neg, task_id, _not_found_json)
        content_len = len(result.content or '')
        file_size = result_dict.get('file_size', 0)
        max_chars = _get_max_read_chars()
        if content_len > max_chars:
            total_lines = result_dict.get('total_lines', 'unknown')
            trimmed, lines_kept, _ = _truncate_to_char_budget(result.content or '', max_chars)
            next_offset = offset + lines_kept
            shown_end = offset + lines_kept - 1
            result.content = trimmed
            result_dict['content'] = trimmed
            result_dict['truncated'] = True
            result_dict['truncated_by'] = 'bytes'
            result_dict['next_offset'] = next_offset
            result_dict['hint'] = f'Output truncated at the {max_chars:,}-char read budget after {lines_kept} line(s) (showing lines {offset}-{shown_end} of {total_lines}). Use offset={next_offset} to continue.'
            if len(trimmed.split('\n', 1)[0]) >= max_chars:
                result_dict['hint'] += ' Note: the first line alone exceeded the budget and was clamped mid-line; its remainder is not retrievable via offset.'
            content_len = len(trimmed)
        if result.content:
            result.content = redact_sensitive_text(result.content, file_read=True)
            result_dict['content'] = result.content
        if file_size and file_size > _LARGE_FILE_HINT_BYTES and (limit > 200) and result_dict.get('truncated'):
            result_dict.setdefault('_hint', f'This file is large ({file_size:,} bytes). Consider reading only the section you need with offset and limit to keep context usage efficient.')
        read_key = ('read', path, offset, limit)
        with _read_tracker_lock:
            if 'dedup' not in task_data:
                task_data['dedup'] = {}
            if 'dedup_hits' not in task_data:
                task_data['dedup_hits'] = {}
            task_data['dedup_hits'].pop(dedup_key, None)
            task_data['read_history'].add((path, offset, limit))
            if task_data['last_key'] == read_key:
                task_data['consecutive'] += 1
            else:
                task_data['last_key'] = read_key
                task_data['consecutive'] = 1
            count = task_data['consecutive']
            try:
                _mtime_now = os.path.getmtime(resolved_str)
                task_data['dedup'][dedup_key] = _mtime_now
                task_data.setdefault('read_timestamps', {})[resolved_str] = _mtime_now
            except OSError:
                pass
            _cap_read_tracker_data(task_data)
        try:
            _partial = offset > 1 or bool(result_dict.get('truncated'))
            file_state.record_read(task_id, resolved_str, partial=_partial)
        except Exception:
            logger.debug('file_state.record_read failed', exc_info=True)
        if count >= 4:
            return tool_error(f'BLOCKED: You have read this exact file region {count} times in a row. The content has NOT changed. You already have this information. STOP re-reading and proceed with your task.', path=path, already_read=count)
        elif count >= 3:
            result_dict['_warning'] = f'You have read this exact file region {count} times consecutively. The content has not changed since your last read. Use the information you already have. If you are stuck in a loop, stop reading and proceed with writing or responding.'
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))

def reset_file_dedup(task_id: str=None):
    """Clear the deduplication cache for file reads.

    Called after context compression — the original read content has been
    summarised away, so the model needs the full content if it reads the
    same file again.  Without this, reads after compression would return
    a "file unchanged" stub pointing at content that no longer exists in
    context.

    Call with a task_id to clear just that task, or without to clear all.
    """
    with _read_tracker_lock:
        if task_id:
            task_data = _read_tracker.get(task_id)
            if task_data:
                if 'dedup' in task_data:
                    task_data['dedup'].clear()
                if 'dedup_hits' in task_data:
                    task_data['dedup_hits'].clear()
        else:
            for task_data in _read_tracker.values():
                if 'dedup' in task_data:
                    task_data['dedup'].clear()
                if 'dedup_hits' in task_data:
                    task_data['dedup_hits'].clear()

def notify_other_tool_call(task_id: str='default'):
    """Reset consecutive read/search counter for a task.

    Called by the tool dispatcher (model_tools.py) whenever a tool OTHER
    than read_file / search_files is executed.  This ensures we only warn
    or block on *truly consecutive* repeated reads — if the agent does
    anything else in between (write, patch, terminal, etc.) the counter
    resets and the next read is treated as fresh.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data['last_key'] = None
            task_data['consecutive'] = 0
            if 'dedup_hits' in task_data:
                task_data['dedup_hits'].clear()
            nf = task_data.get('not_found')
            if nf:
                nf.clear()

def _invalidate_dedup_for_path(filepath: str, task_id: str) -> None:
    """Remove all dedup cache entries whose resolved path matches *filepath*.

    Called after write_file and patch so that a subsequent read_file on
    the same path always returns fresh content instead of a stale
    "File unchanged" stub.  The dedup cache keys are tuples of
    ``(resolved_path, offset, limit)``; we must evict **all** offset/limit
    combinations for the written path because any cached range could now
    be stale.

    Must be called with ``_read_tracker_lock`` **not** held — acquires it
    internally.
    """
    try:
        resolved = str(_resolve_path(filepath, task_id))
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is None:
            return
        dedup = task_data.get('dedup')
        if dedup:
            stale_keys = [k for k in dedup if k[0] == resolved]
            for k in stale_keys:
                del dedup[k]
        nf = task_data.get('not_found')
        if nf:
            nf.pop(('read', resolved), None)
            nf.pop(('search', resolved), None)

def _update_read_timestamp(filepath: str, task_id: str) -> None:
    """Record the file's current modification time after a successful write.

    Called after write_file and patch so that consecutive edits by the
    same task don't trigger false staleness warnings — each write
    refreshes the stored timestamp to match the file's new state.

    Also invalidates the dedup cache for the written path so that
    subsequent reads return fresh content (fixes #13144).
    """
    _invalidate_dedup_for_path(filepath, task_id)
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
        current_mtime = os.path.getmtime(resolved)
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is not None:
            task_data.setdefault('read_timestamps', {})[resolved] = current_mtime
            _cap_read_tracker_data(task_data)

def _check_file_staleness(filepath: str, task_id: str) -> str | None:
    """Check whether a file was modified since the agent last read it.

    Returns a warning string if the file is stale (mtime changed since
    the last read_file call for this task), or None if the file is fresh
    or was never read.  Does not block — the write still proceeds.
    """
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        read_mtime = task_data.get('read_timestamps', {}).get(resolved)
    if read_mtime is None:
        return None
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None
    if current_mtime != read_mtime:
        return f'Warning: {filepath} was modified since you last read it (external edit or concurrent agent). The content you read may be stale. Consider re-reading the file to verify before writing.'
    return None

def _mark_verification_stale(task_id: str, resolved_paths: list[str], session_id: str | None=None) -> None:
    """Best-effort note that successful edits made prior verification stale."""
    paths = [p for p in resolved_paths if p]
    if not paths:
        return
    try:
        from agent.coding_context import project_facts_for
        from agent.verification_evidence import mark_workspace_edited
        cwd = None
        for path in paths:
            try:
                candidate = str(Path(path).parent)
            except Exception:
                continue
            if project_facts_for(candidate):
                cwd = candidate
                break
        if cwd is None:
            cwd = _authoritative_workspace_root(task_id)
        if cwd is None:
            try:
                cwd = str(Path(paths[0]).parent)
            except Exception:
                cwd = None
        mark_workspace_edited(session_id=session_id or task_id, cwd=cwd, paths=paths)
    except Exception:
        logger.debug('verification stale marker failed', exc_info=True)

def write_file_tool(path: str, content: str, task_id: str='default', cross_profile: bool=False, session_id: str | None=None) -> str:
    """Write content to a file.

    ``cross_profile`` opts out of the soft cross-Duck Agent-profile guard. The
    guard fires only on writes that land in another profile's
    skills/plugins/cron/memories directory; everything else is unaffected.
    Pass ``True`` after explicit user direction — same shape as ``force``
    on the terminal tool.
    """
    sensitive_err = _check_sensitive_path(path, task_id)
    if sensitive_err:
        return tool_error(sensitive_err)
    protected_err = _check_protected_instruction_write([path], task_id)
    if protected_err:
        return tool_error(protected_err)
    if not cross_profile:
        cross_warning = _check_cross_profile_path(path, task_id)
        if cross_warning:
            return tool_error(cross_warning)
    if _is_internal_file_tool_content(content):
        return tool_error('Refusing to write internal read_file display text as file content. Strip read_file line-number prefixes or reconstruct the intended file contents before writing.')
    try:
        try:
            _resolved = str(_resolve_path_for_task(path, task_id))
        except Exception:
            _resolved = None
        if _resolved is None:
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(path, content)
            result_dict = result.to_dict()
            if stale_warning:
                result_dict['_warning'] = stale_warning
            if not result_dict.get('error'):
                _mark_verification_stale(task_id, [path], session_id=session_id)
            _update_read_timestamp(path, task_id)
            return json.dumps(result_dict, ensure_ascii=False)
        with file_state.lock_path(_resolved):
            cross_warning = file_state.check_stale(task_id, _resolved)
            stale_warning = _check_file_staleness(path, task_id)
            cwd_warning = _path_resolution_warning(path, Path(_resolved), task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(_resolved, content)
            result_dict = result.to_dict()
            effective_warning = cross_warning or stale_warning or cwd_warning
            if effective_warning:
                result_dict['_warning'] = effective_warning
            result_dict['resolved_path'] = _resolved
            if not result_dict.get('error'):
                result_dict['files_modified'] = [_resolved]
                _mark_verification_stale(task_id, [_resolved], session_id=session_id)
            _update_read_timestamp(path, task_id)
            if not result_dict.get('error'):
                file_state.note_write(task_id, _resolved)
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug('write_file expected denial: %s: %s', type(e).__name__, e)
        else:
            logger.error('write_file error: %s: %s', type(e).__name__, e, exc_info=True)
        return tool_error(str(e))

def patch_tool(mode: str='replace', path: str=None, old_string: str=None, new_string: str=None, replace_all: bool=False, patch: str=None, task_id: str='default', cross_profile: bool=False, session_id: str | None=None) -> str:
    """Patch a file using replace mode or V4A patch format.

    ``cross_profile`` opts out of the soft cross-Duck Agent-profile guard for
    targets under another profile's skills/plugins/cron/memories
    directory. Same shape as ``write_file``'s flag.
    """
    _paths_to_check = []
    if path:
        _paths_to_check.append(path)
    if mode == 'patch' and patch:
        import re as _re
        from tools.path_security import has_traversal_component

        def _reject_v4a_traversal(v4a_path: str) -> str | None:
            if has_traversal_component(v4a_path):
                return tool_error(f"V4A patch header contains '..' traversal: {v4a_path!r}. Use the agent's cwd-relative path (no '..') or an absolute path in '*** Update File:' / '*** Add File:' / '*** Delete File:' / '*** Move File:' headers.")
            return None
        for _m in _re.finditer('^\\*\\*\\*\\s*(?:Update|Add|Delete)\\s+File:\\s*(.+)$', patch, _re.MULTILINE):
            v4a_path = _m.group(1).strip()
            _err = _reject_v4a_traversal(v4a_path)
            if _err:
                return _err
            _paths_to_check.append(v4a_path)
        for _m in _re.finditer('^\\*\\*\\*\\s*Move\\s+File:\\s*(.+?)\\s*->\\s*(.+)$', patch, _re.MULTILINE):
            for v4a_path in (_m.group(1).strip(), _m.group(2).strip()):
                _err = _reject_v4a_traversal(v4a_path)
                if _err:
                    return _err
                _paths_to_check.append(v4a_path)
    for _p in _paths_to_check:
        sensitive_err = _check_sensitive_path(_p, task_id)
        if sensitive_err:
            return tool_error(sensitive_err)
        if not cross_profile:
            cross_warning = _check_cross_profile_path(_p, task_id)
            if cross_warning:
                return tool_error(cross_warning)
    protected_err = _check_protected_instruction_write(_paths_to_check, task_id)
    if protected_err:
        return tool_error(protected_err)
    try:
        _resolved_paths: list[str] = []
        _seen: set[str] = set()
        for _p in _paths_to_check:
            try:
                _r = str(_resolve_path_for_task(_p, task_id))
            except Exception:
                _r = None
            if _r and _r not in _seen:
                _resolved_paths.append(_r)
                _seen.add(_r)
        _resolved_paths.sort()
        from contextlib import ExitStack
        with ExitStack() as _locks:
            for _r in _resolved_paths:
                _locks.enter_context(file_state.lock_path(_r))
            stale_warnings: list[str] = []
            _path_to_resolved: dict[str, str] = {}
            for _p in _paths_to_check:
                try:
                    _r = str(_resolve_path_for_task(_p, task_id))
                except Exception:
                    _r = None
                _path_to_resolved[_p] = _r
                _cross = file_state.check_stale(task_id, _r) if _r else None
                _sw = _cross or _check_file_staleness(_p, task_id)
                if not _sw and _r:
                    _sw = _path_resolution_warning(_p, Path(_r), task_id)
                if _sw:
                    stale_warnings.append(_sw)
            file_ops = _get_file_ops(task_id)
            if mode == 'replace':
                if not path:
                    return tool_error('path required')
                if old_string is None or new_string is None:
                    return tool_error('old_string and new_string required')
                _replace_target = _path_to_resolved.get(path) or path
                result = file_ops.patch_replace(_replace_target, old_string, new_string, replace_all)
            elif mode == 'patch':
                if not patch:
                    return tool_error('patch content required')
                patch_for_ops = _rewrite_v4a_patch_paths_for_host(patch, _path_to_resolved, file_ops)
                result = file_ops.patch_v4a(patch_for_ops)
            else:
                return tool_error(f'Unknown mode: {mode}')
            result_dict = result.to_dict()
            if stale_warnings:
                result_dict['_warning'] = stale_warnings[0] if len(stale_warnings) == 1 else ' | '.join(stale_warnings)
            _resolved_modified = [_path_to_resolved.get(_p) or _p for _p in _paths_to_check]
            if not result_dict.get('error'):
                result_dict['files_modified'] = _resolved_modified
                if len(_resolved_modified) == 1:
                    result_dict['resolved_path'] = _resolved_modified[0]
                _mark_verification_stale(task_id, _resolved_modified, session_id=session_id)
                for _p in _paths_to_check:
                    _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r)
                _reset_patch_failures(task_id, [_r for _r in (_path_to_resolved.get(_p) for _p in _paths_to_check) if _r])
        if result_dict.get('error') and 'Could not find' in str(result_dict['error']):
            failure_count = 0
            if mode == 'replace' and path:
                resolved = _path_to_resolved.get(path) or path
                failure_count = _record_patch_failure(task_id, resolved)
            if failure_count >= 3:
                result_dict['_hint'] = f'This is failure #{failure_count} patching {path!r}. Stop retrying with variations of the same old_string. Either: (1) re-read the file fresh to verify current content, (2) use a longer / more unique old_string with surrounding context lines, or (3) use write_file to replace the entire file if the targeted region is hard to anchor.'
            elif 'Did you mean one of these sections?' not in str(result_dict['error']):
                result_dict['_hint'] = 'old_string not found. Use read_file to verify the current content, or search_files to locate the text.'
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))

def search_tool(pattern: str, target: str='content', path: str='.', file_glob: str=None, limit: int=50, offset: int=0, output_mode: str='content', context: int=0, task_id: str='default') -> str:
    """Search for content or files."""
    try:
        offset, limit = normalize_search_pagination(offset, limit)
        search_key = ('search', pattern, target, str(path), file_glob or '', limit, offset)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {'last_key': None, 'consecutive': 0, 'read_history': set()})
            if task_data['last_key'] == search_key:
                task_data['consecutive'] += 1
            else:
                task_data['last_key'] = search_key
                task_data['consecutive'] = 1
            count = task_data['consecutive']
        if count >= 4:
            return tool_error(f'BLOCKED: You have run this exact search {count} times in a row. The results have NOT changed. You already have this information. STOP re-searching and proceed with your task.', pattern=pattern, already_searched=count)
        try:
            resolved_path = _resolve_path_for_task(path, task_id)
        except (OSError, ValueError, RuntimeError):
            resolved_path = None
        block_error = get_read_block_error(str(resolved_path) if resolved_path else path)
        if block_error:
            return tool_error(block_error)
        try:
            resolved_search_path = str(_resolve_path_for_task(path, task_id))
        except (OSError, ValueError):
            resolved_search_path = path
        cached_search_nf = _check_not_found_cache('search', resolved_search_path, task_id)
        if cached_search_nf is not None:
            return cached_search_nf
        file_ops = _get_file_ops(task_id)
        result = file_ops.search(pattern=pattern, path=path, target=target, file_glob=file_glob, limit=limit, offset=offset, output_mode=output_mode, context=context)
        omitted = _filter_read_blocked_search_results(result, task_id)
        if hasattr(result, 'matches'):
            for m in result.matches:
                if hasattr(m, 'content') and m.content:
                    m.content = redact_sensitive_text(m.content, file_read=True)
        result_dict = result.to_dict(densify=True)
        if omitted:
            result_dict['_omitted'] = f'{omitted} result(s) omitted because they target credential, token, cache, or secret-bearing environment files.'
        _search_err = result_dict.get('error') or ''
        if isinstance(_search_err, str) and _search_err.startswith('Path not found:'):
            _search_nf_json = json.dumps(result_dict, ensure_ascii=False)
            _record_not_found('search', resolved_search_path, task_id, _search_nf_json)
        if count >= 3:
            result_dict['_warning'] = f'You have run this exact search {count} times consecutively. The results have not changed. Use the information you already have.'
        result_json = json.dumps(result_dict, ensure_ascii=False)
        if result_dict.get('truncated'):
            next_offset = offset + limit
            result_json += f'\n\n[Hint: Results truncated. Use offset={next_offset} to see more, or narrow with a more specific pattern or file_glob.]'
        return result_json
    except Exception as e:
        return tool_error(str(e))
from tools.registry import registry, tool_error

def _check_file_reqs():
    """Lazy wrapper to avoid circular import with tools/__init__.py."""
    from tools import check_file_requirements
    return check_file_requirements()
READ_FILE_SCHEMA = {'name': 'read_file', 'description': "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are truncated on a line boundary and return a next_offset; continue with offset to read the rest. Jupyter notebooks (.ipynb), Word documents (.docx), and Excel workbooks (.xlsx) are auto-extracted to readable text; PDF, legacy Office (.doc/.ppt/.xls), OpenDocument, RTF, and EPUB convert too when the optional anydoc converter is available (auto-installed on first use where installs are permitted). NOTE: Cannot read images or other binary files — use vision_analyze for images.", 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': 'Path to the file to read (absolute, relative, or ~/path)'}, 'offset': {'type': 'integer', 'description': 'Line number to start reading from (1-indexed, default: 1)', 'default': 1, 'minimum': 1}, 'limit': {'type': 'integer', 'description': 'Maximum number of lines to read (default: 2000, max: 2000). Reads are additionally capped at a ~100K-character budget with a next_offset continuation.', 'default': 2000, 'maximum': 2000}}, 'required': ['path']}}
WRITE_FILE_SCHEMA = {'name': 'write_file', 'description': "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out). The result's verified:true means the on-disk content hash was confirmed — do NOT re-read the file to check the write landed.", 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"}, 'content': {'type': 'string', 'description': 'Complete content to write to the file'}, 'cross_profile': {'type': 'boolean', 'description': "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Duck Agent profile's skills/plugins/cron/memories — by default these writes are blocked with a warning because they affect a different profile than the one this session is running under.", 'default': False}}, 'required': ['path', 'content']}}
PATCH_SCHEMA = {'name': 'patch', 'description': "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. Returns a unified diff. Auto-runs syntax checks after editing.\n\nREPLACE MODE (mode='replace', default): find a unique string and replace it. REQUIRED PARAMETERS: mode, path, old_string, new_string.\nPATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. REQUIRED PARAMETERS: mode, patch.", 'parameters': {'type': 'object', 'properties': {'mode': {'type': 'string', 'enum': ['replace', 'patch'], 'description': "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.", 'default': 'replace'}, 'path': {'type': 'string', 'description': "REQUIRED when mode='replace'. File path to edit."}, 'old_string': {'type': 'string', 'description': "REQUIRED when mode='replace'. Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness."}, 'new_string': {'type': 'string', 'description': "REQUIRED when mode='replace'. Replacement text. Pass empty string '' to delete the matched text."}, 'replace_all': {'type': 'boolean', 'description': 'Replace all occurrences instead of requiring a unique match (default: false)', 'default': False}, 'patch': {'type': 'string', 'description': "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch"}, 'cross_profile': {'type': 'boolean', 'description': "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Duck Agent profile's skills/plugins/cron/memories.", 'default': False}}, 'required': ['mode']}}
SEARCH_FILES_SCHEMA = {'name': 'search_files', 'description': "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time.", 'parameters': {'type': 'object', 'properties': {'pattern': {'type': 'string', 'description': "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"}, 'target': {'type': 'string', 'enum': ['content', 'files'], 'description': "'content' searches inside file contents, 'files' searches for files by name", 'default': 'content'}, 'path': {'type': 'string', 'description': 'Directory or file to search in (default: current working directory)', 'default': '.'}, 'file_glob': {'type': 'string', 'description': "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"}, 'limit': {'type': 'integer', 'description': 'Maximum number of results to return (default: 50)', 'default': 50}, 'offset': {'type': 'integer', 'description': 'Skip first N results for pagination (default: 0)', 'default': 0}, 'output_mode': {'type': 'string', 'enum': ['content', 'files_only', 'count'], 'description': "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", 'default': 'content'}, 'context': {'type': 'integer', 'description': 'Number of context lines before and after each match (grep mode only)', 'default': 0}}, 'required': ['pattern']}}

def _handle_read_file(args, **kw):
    tid = kw.get('task_id') or 'default'
    return read_file_tool(path=args.get('path', ''), offset=args.get('offset', 1), limit=args.get('limit', 500), task_id=tid)

def _handle_write_file(args, **kw):
    tid = kw.get('task_id') or 'default'
    if not args.get('path') or not isinstance(args.get('path'), str):
        return tool_error("write_file: missing required field 'path'. Re-emit the tool call with both 'path' and 'content' set.")
    if 'content' not in args:
        return tool_error("write_file: missing required field 'content'. The tool call included a path but no content argument — this is almost always a dropped-arg bug under context pressure. Re-emit the tool call with the full content payload, or use execute_code with hermes_tools.write_file() for very large files.")
    if not isinstance(args['content'], str):
        return tool_error(f"write_file: 'content' must be a string, got {type(args['content']).__name__}.")
    return write_file_tool(path=args['path'], content=args['content'], task_id=tid, cross_profile=bool(args.get('cross_profile', False)), session_id=kw.get('session_id'))

def _handle_patch(args, **kw):
    tid = kw.get('task_id') or 'default'
    return patch_tool(mode=args.get('mode', 'replace'), path=args.get('path'), old_string=args.get('old_string'), new_string=args.get('new_string'), replace_all=args.get('replace_all', False), patch=args.get('patch'), task_id=tid, cross_profile=bool(args.get('cross_profile', False)), session_id=kw.get('session_id'))

def _handle_search_files(args, **kw):
    tid = kw.get('task_id') or 'default'
    target_map = {'grep': 'content', 'find': 'files'}
    raw_target = args.get('target', 'content')
    target = target_map.get(raw_target, raw_target)
    return search_tool(pattern=args.get('pattern', ''), target=target, path=args.get('path', '.'), file_glob=args.get('file_glob'), limit=args.get('limit', 50), offset=args.get('offset', 0), output_mode=args.get('output_mode', 'content'), context=args.get('context', 0), task_id=tid)
registry.register(name='read_file', toolset='file', schema=READ_FILE_SCHEMA, handler=_handle_read_file, check_fn=_check_file_reqs, emoji='📖', max_result_size_chars=100000)
registry.register(name='write_file', toolset='file', schema=WRITE_FILE_SCHEMA, handler=_handle_write_file, check_fn=_check_file_reqs, emoji='✍️', max_result_size_chars=100000)
registry.register(name='patch', toolset='file', schema=PATCH_SCHEMA, handler=_handle_patch, check_fn=_check_file_reqs, emoji='🔧', max_result_size_chars=100000)
registry.register(name='search_files', toolset='file', schema=SEARCH_FILES_SCHEMA, handler=_handle_search_files, check_fn=_check_file_reqs, emoji='🔎', max_result_size_chars=100000)