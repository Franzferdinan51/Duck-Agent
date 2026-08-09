"""Docker execution environment for sandboxed command execution.

Security hardened (cap-drop ALL, no-new-privileges, PID limits),
configurable resource limits (CPU, memory, disk), and optional filesystem
persistence via bind mounts.
"""
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError, _popen_bash
from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST, _is_hermes_internal_secret
logger = logging.getLogger(__name__)
_DOCKER_SEARCH_PATHS = ['/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/Applications/Docker.app/Contents/Resources/bin/docker']
_docker_executable: Optional[str] = None
_ENV_VAR_NAME_RE = re.compile('^[A-Za-z_][A-Za-z0-9_]*$')
_EGRESS_LABEL_KEY = 'duck-agent-egress'

def _normalize_forward_env_names(forward_env: list[str] | None) -> list[str]:
    """Return a deduplicated list of valid environment variable names."""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in forward_env or []:
        if not isinstance(item, str):
            logger.warning('Ignoring non-string docker_forward_env entry: %r', item)
            continue
        key = item.strip()
        if not key:
            continue
        if not _ENV_VAR_NAME_RE.match(key):
            logger.warning('Ignoring invalid docker_forward_env entry: %r', item)
            continue
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized

def _normalize_env_dict(env: dict | None) -> dict[str, str]:
    """Validate and normalize a docker_env dict to {str: str}.

    Filters out entries with invalid variable names or non-string values.
    """
    if not env:
        return {}
    if not isinstance(env, dict):
        logger.warning('docker_env is not a dict: %r', env)
        return {}
    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_VAR_NAME_RE.match(key.strip()):
            logger.warning('Ignoring invalid docker_env key: %r', key)
            continue
        key = key.strip()
        if not isinstance(value, str):
            if isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                logger.warning('Ignoring non-string docker_env value for %r: %r', key, value)
                continue
        normalized[key] = value
    return normalized

def _load_hermes_env_vars() -> dict[str, str]:
    """Load ~/.duck-agent/.env values without failing Docker command execution."""
    try:
        from hermes_cli.config import load_env
        return load_env() or {}
    except Exception:
        return {}
_LABEL_VALUE_OK_RE = re.compile('[^A-Za-z0-9_.-]')

def _sanitize_label_value(value: str) -> str:
    """Coerce *value* into a Docker label-safe form (alnum + ``_.-``, ≤63 chars).

    Empty or all-invalid inputs collapse to ``"unknown"`` so the resulting
    label is always queryable. Used at container-create time; never round-trip
    a sanitized value back into application logic.
    """
    if not isinstance(value, str) or not value:
        return 'unknown'
    cleaned = _LABEL_VALUE_OK_RE.sub('_', value)
    cleaned = cleaned[:63] or 'unknown'
    return cleaned

def _get_active_profile_name() -> str:
    """Return the active Duck Agent profile name, or ``"default"`` on any error.

    Resolved at container-create time so a single container is permanently
    tagged with the profile that created it. Profile switches inside the
    same process don't retroactively relabel running containers.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or 'default'
    except Exception:
        return 'default'

def reap_orphan_containers(*, max_age_seconds: int=600, profile_filter: str | None=None, docker_exe: str | None=None) -> int:
    """Remove stale duck-agent-tagged containers left behind by prior processes.

    Targets containers that match all of:

    * ``label=duck-agent=1`` (created by this codebase)
    * ``status=exited`` (running containers are NEVER reaped — they may
      belong to a sibling Duck Agent process whose reuse path will pick them
      up; killing them would crash the sibling mid-command)
    * (optional) ``label=duck-agent-profile=<profile_filter>`` (sweep only the
      caller's profile by default; a duck-agent process in profile A must not
      tear down profile B's containers)
    * ``State.FinishedAt`` older than *max_age_seconds* ago (so a sibling
      process that just exited and is about to be replaced doesn't get
      its container yanked out from under it)

    Returns the number of containers removed. Best-effort: any failure
    (docker daemon unreachable, slow inspect, parse error) is logged at
    debug level and the function returns whatever it managed before the
    failure. Safe to call repeatedly; idempotent.

    Issue #20561 — this is the safety net for SIGKILL / OOM / crashed
    terminal exits that bypass the ``atexit`` cleanup hook. Without it,
    even with the cleanup-fix in the prior commit, a hard-killed Duck Agent
    process leaves its container behind permanently because there's no
    subsequent Duck Agent process scheduled to reuse that exact (task, profile)
    pair.
    """
    docker = docker_exe or find_docker() or 'docker'
    filters = ['--filter', 'label=duck-agent=1', '--filter', 'status=exited']
    if profile_filter:
        filters.extend(['--filter', f'label=duck-agent-profile={_sanitize_label_value(profile_filter)}'])
    try:
        listing = subprocess.run([docker, 'ps', '-a', *filters, '--format', '{{.ID}}'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, check=False, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug('orphan reaper docker ps failed: %s', e)
        return 0
    if listing.returncode != 0:
        logger.debug('orphan reaper docker ps returned %d: %s', listing.returncode, listing.stderr.strip())
        return 0
    candidate_ids = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
    if not candidate_ids:
        return 0
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    removed = 0
    for cid in candidate_ids:
        finished_at = _container_finished_at(docker, cid)
        if finished_at is None:
            continue
        age = (now - finished_at).total_seconds()
        if age < max_age_seconds:
            continue
        try:
            result = subprocess.run([docker, 'rm', '-f', cid], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, stdin=subprocess.DEVNULL)
            if result.returncode == 0:
                removed += 1
                logger.info('Reaped orphan container %s (exited %d seconds ago)', cid[:12], int(age))
            else:
                logger.debug('docker rm -f %s failed: %s', cid[:12], result.stderr.strip())
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug('orphan reaper docker rm %s failed: %s', cid[:12], e)
    return removed

def _container_finished_at(docker_exe: str, container_id: str):
    """Parse ``docker inspect`` FinishedAt for *container_id*.

    Returns a timezone-aware datetime, or ``None`` if the field is missing,
    unparseable, or the zero-value ``0001-01-01T00:00:00Z`` Docker emits
    for never-finished containers. ``None`` means "don't reap" — the caller
    leaves the container alone.
    """
    try:
        result = subprocess.run([docker_exe, 'inspect', '--format', '{{.State.FinishedAt}}', container_id], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug('orphan reaper docker inspect %s failed: %s', container_id[:12], e)
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw or raw.startswith('0001-01-01'):
        return None
    import re as _re
    raw = _re.sub('(\\.\\d{6})\\d+', '\\1', raw)
    raw = raw.replace('Z', '+00:00')
    try:
        import datetime
        return datetime.datetime.fromisoformat(raw)
    except ValueError as e:
        logger.debug('could not parse FinishedAt %r for %s: %s', raw, container_id[:12], e)
        return None

def find_docker() -> Optional[str]:
    """Locate the docker (or podman) CLI binary.

    Resolution order:
    1. ``HERMES_DOCKER_BINARY`` env var — explicit override (e.g. ``/usr/bin/podman``)
    2. ``docker`` on PATH via ``shutil.which``
    3. ``podman`` on PATH via ``shutil.which``
    4. Well-known macOS Docker Desktop install locations

    Returns the absolute path, or ``None`` if neither runtime can be found.
    """
    global _docker_executable
    if _docker_executable is not None:
        return _docker_executable
    override = os.getenv('HERMES_DOCKER_BINARY')
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        _docker_executable = override
        logger.info('Using HERMES_DOCKER_BINARY override: %s', override)
        return override
    found = shutil.which('docker')
    if found:
        _docker_executable = found
        return found
    found = shutil.which('podman')
    if found:
        _docker_executable = found
        logger.info('Using podman as container runtime: %s', found)
        return found
    for path in _DOCKER_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            _docker_executable = path
            logger.info('Found docker at non-PATH location: %s', path)
            return path
    return None
_BASE_SECURITY_ARGS = ['--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,size=512m', '--tmpfs', '/var/tmp:rw,noexec,nosuid,size=256m']
_DEFAULT_PIDS_LIMIT = '256'
_DEFAULT_SHM_SIZE = '1g'

def _extra_args_set_shm_size(extra_args: list) -> bool:
    """True when user-supplied docker_extra_args already set ``--shm-size``.

    In that case we skip our default so the user's value is unambiguous
    (rather than relying on flag-ordering / last-wins behavior).
    """
    return any((isinstance(a, str) and (a == '--shm-size' or a.startswith('--shm-size=')) for a in extra_args or []))
_RUN_TMPFS_NOEXEC = ('--tmpfs', '/run:rw,noexec,nosuid,size=64m')
_RUN_TMPFS_EXEC = ('--tmpfs', '/run:rw,exec,nosuid,size=64m')
_PRIVDROP_CAP_ARGS = ['--cap-add', 'SETUID', '--cap-add', 'SETGID']

def _egress_proxy_args_for_docker() -> tuple[list[str], dict[str, str], list[str]]:
    """Build the docker mount/env/host args needed to route a sandbox through
    the iron-proxy egress firewall.

    Returns ``(volume_args, env_overrides, host_args)``:

    * ``volume_args`` — read-only bind mount of the CA cert into the container
      (extends docker's ``-v`` argv list)
    * ``env_overrides`` — env vars to set on container creation: ``HTTPS_PROXY``,
      ``HTTP_PROXY``, ``NO_PROXY`` (loopback only), Python/Node/curl CA-bundle
      paths, and one ``HERMES_PROXY_TOKEN_<NAME>`` per minted mapping
    * ``host_args`` — extra ``--add-host`` flags so the container can reach the
      host-side proxy (Linux needs ``host.docker.internal:host-gateway``;
      Docker Desktop populates this automatically on macOS/Windows)

    Returns three empty containers when the proxy is disabled, not yet set up,
    or not currently running.  If ``proxy.enforce_on_docker`` is true and the
    proxy is enabled-but-not-running, raises ``RuntimeError`` so the docker
    backend refuses to start the sandbox.
    """
    try:
        from hermes_cli.config import load_config
        from agent.proxy_sources import iron_proxy as ip
    except ImportError as exc:
        logger.debug('Egress proxy plumbing unavailable: %s', exc)
        return ([], {}, [])
    cfg = load_config()
    proxy_cfg = cfg.get('proxy') or {}
    if not proxy_cfg.get('enabled'):
        return ([], {}, [])
    status = ip.get_status()
    enforce = bool(proxy_cfg.get('enforce_on_docker', True))
    if not status.configured:
        msg = 'proxy.enabled is true but iron-proxy is not configured. Run `duck-agent egress setup` to mint tokens and write proxy.yaml.'
        if enforce:
            raise RuntimeError(msg)
        logger.warning('%s — continuing without proxy (enforce_on_docker=false).', msg)
        return ([], {}, [])
    if not (status.pid and status.listening):
        msg = f'iron-proxy is enabled but not running on port {status.tunnel_port}. Start it with `duck-agent egress start`.'
        if enforce:
            raise RuntimeError(msg)
        logger.warning('%s — continuing without proxy (enforce_on_docker=false).', msg)
        return ([], {}, [])
    if status.ca_cert_path is None or not status.ca_cert_path.exists():
        msg = f'iron-proxy CA cert vanished from {status.ca_cert_path}. Re-run `duck-agent egress setup` to regenerate it.'
        if enforce:
            raise RuntimeError(msg)
        logger.warning('%s — continuing without proxy (enforce_on_docker=false).', msg)
        return ([], {}, [])
    mappings = ip.load_mappings()
    if not mappings:
        msg = 'iron-proxy is configured but mappings.json is empty or corrupt.  Re-run `duck-agent egress setup` to mint provider tokens before starting a sandbox.'
        if enforce:
            raise RuntimeError(msg)
        logger.warning('%s — continuing without proxy (enforce_on_docker=false).', msg)
        return ([], {}, [])
    container_ca = '/etc/ssl/certs/duck-agent-egress-ca.crt'
    volume_args = ['-v', f'{status.ca_cert_path}:{container_ca}:ro']
    proxy_url = f'http://host.docker.internal:{status.tunnel_port}'
    plain_http_url = f'http://host.docker.internal:{status.tunnel_port + 1}'
    env_overrides: dict[str, str] = {'HTTPS_PROXY': proxy_url, 'https_proxy': proxy_url, 'HTTP_PROXY': plain_http_url, 'http_proxy': plain_http_url, 'NO_PROXY': '127.0.0.1,localhost,::1', 'no_proxy': '127.0.0.1,localhost,::1', 'REQUESTS_CA_BUNDLE': container_ca, 'SSL_CERT_FILE': container_ca, 'CURL_CA_BUNDLE': container_ca, 'NODE_EXTRA_CA_CERTS': container_ca, 'HERMES_EGRESS_PROXY': '1', '_HERMES_EGRESS_NODE_OPTIONS_APPEND': '--use-openssl-ca'}
    for m in mappings:
        env_overrides[m.real_env_name] = m.proxy_token
        env_overrides[f'HERMES_PROXY_TOKEN_{m.real_env_name}'] = m.proxy_token
        for alias in getattr(m, 'alias_env_names', ()) or ():
            env_overrides[alias] = m.proxy_token
    host_args: list[str] = ['--add-host', 'host.docker.internal:host-gateway']
    return (volume_args, env_overrides, host_args)

def _egress_reuse_fingerprint(volume_args: list[str], env_overrides: dict[str, str], host_args: list[str]) -> str:
    """Stable Docker-label value for the egress posture of a container."""
    if not (volume_args or env_overrides or host_args):
        return 'off'
    payload = json.dumps({'volume_args': volume_args, 'env_overrides': env_overrides, 'host_args': host_args}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]

def _egress_enforce_on_docker(default: bool=True) -> bool:
    """Read proxy.enforce_on_docker with fail-safe defaulting."""
    try:
        from hermes_cli.config import load_config as _load_cfg
        return bool((_load_cfg().get('proxy') or {}).get('enforce_on_docker', default))
    except (ImportError, OSError):
        return default
    except Exception:
        return default

def _critical_egress_env_names(env_overrides: dict[str, str]) -> set[str]:
    """Env names that would weaken or bypass enforced egress if overridden."""
    critical = {'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS', 'NODE_OPTIONS'}
    critical.update((key for key in env_overrides if key.endswith('_API_KEY') or key.endswith('_TOKEN')))
    return critical

def _extra_args_egress_collisions(extra_args: list[str], critical_names: set[str]) -> list[str]:
    """Return docker_extra_args entries that can override egress controls."""
    collisions: list[str] = []
    env_flags = {'-e', '--env', '--env-file'}
    network_flags = {'--network', '--net'}
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        nxt = extra_args[i + 1] if i + 1 < len(extra_args) else ''
        if arg in env_flags:
            if arg == '--env-file':
                collisions.append(arg)
            else:
                name = nxt.split('=', 1)[0]
                if name in critical_names:
                    collisions.append(name)
            i += 2
            continue
        if any((arg.startswith(f'{flag}=') for flag in env_flags)):
            if arg.startswith('--env-file='):
                collisions.append('--env-file')
            else:
                name = arg.split('=', 1)[1].split('=', 1)[0]
                if name in critical_names:
                    collisions.append(name)
        elif arg in network_flags or any((arg.startswith(f'{flag}=') for flag in network_flags)):
            collisions.append(arg)
        i += 1
    return sorted(set(collisions))

def _build_security_args(run_as_host_user: bool, run_exec: bool=False) -> list[str]:
    """Return the security/cap/tmpfs args tailored to the privilege mode.

    ``run_exec`` mounts ``/run`` with ``exec`` instead of the hardened
    ``noexec`` default. This is required for s6-overlay images whose ``/init``
    entrypoint execs ``/run/s6/basedir/bin/init`` during startup; see
    ``_image_uses_init_entrypoint``.
    """
    run_tmpfs = list(_RUN_TMPFS_EXEC if run_exec else _RUN_TMPFS_NOEXEC)
    args = list(_BASE_SECURITY_ARGS) + run_tmpfs
    if run_as_host_user:
        return args
    return args + list(_PRIVDROP_CAP_ARGS)

def _image_uses_init_entrypoint(docker_exe: str, image: str) -> bool:
    """Return True if ``image``'s entrypoint is the s6-overlay ``/init``.

    Such images (e.g. anything built on ``s6-overlay``, including
    ``duck-agent:latest``) already provide their own PID-1 init and execute
    ``/run/s6/basedir/bin/init`` during stage0 startup. They are incompatible
    with Docker's ``--init`` (two competing PID-1 inits) and with a ``noexec``
    ``/run`` mount. Detection is best-effort: on any inspection failure we
    return False and keep the hardened defaults.
    """
    try:
        result = subprocess.run([docker_exe, 'image', 'inspect', image, '--format', '{{json .Config.Entrypoint}}'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug('Docker: could not inspect entrypoint for %s: %s', image, e)
        return False
    if result.returncode != 0:
        logger.debug('Docker: image inspect for %s returned %d (stderr=%s)', image, result.returncode, result.stderr.strip())
        return False
    raw = (result.stdout or '').strip()
    if not raw or raw == 'null':
        return False
    try:
        entrypoint = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    if not isinstance(entrypoint, list) or not entrypoint:
        return False
    first = str(entrypoint[0]).strip()
    return first in ('/init', '/package/admin/s6-overlay/command/init')

def _resolve_host_user_spec() -> Optional[str]:
    """Return ``<uid>:<gid>`` for the current host user, or ``None`` on platforms
    where this is not meaningful (e.g. Windows without posix ids).

    We intentionally read ``os.getuid()``/``os.getgid()`` directly rather than
    going through ``getpass``/``pwd`` so this stays cheap and never raises on
    nameless UIDs (nss lookups can fail inside sandboxed launchers).
    """
    get_uid = getattr(os, 'getuid', None)
    get_gid = getattr(os, 'getgid', None)
    if get_uid is None or get_gid is None:
        return None
    try:
        return f'{get_uid()}:{get_gid()}'
    except Exception:
        return None
_storage_opt_ok: Optional[bool] = None
_cgroup_limits_ok: Optional[bool] = None

def _cgroup_limits_available(image: str) -> bool:
    """Probe whether cgroup resource limits work in this environment.

    Tests ``--cpus``, ``--memory`` and ``--pids-limit`` together by spawning
    a throwaway container from *image* (the same sandbox image we are about
    to use for real, so no extra pull and no dependency on a public
    registry). The container runs ``sleep 0`` — sleep is guaranteed to be
    present because the sandbox itself uses ``sleep 2h`` as its long-lived
    entrypoint.

    On hosts where the corresponding cgroup controllers are not delegated
    to this process (typical inside unprivileged LXCs and some rootless
    setups) these flags cause every container start to fail with ``OCI
    runtime error`` / exit 126. The probe runs once per process and the
    result — which is host-wide, not image-specific — is cached.
    """
    global _cgroup_limits_ok
    if _cgroup_limits_ok is not None:
        return _cgroup_limits_ok
    docker_exe = find_docker()
    if not docker_exe or not image:
        _cgroup_limits_ok = False
        return False
    try:
        result = subprocess.run([docker_exe, 'run', '--rm', '--cpus', '0.5', '--memory', '64m', '--pids-limit', '32', image, 'sleep', '0'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, stdin=subprocess.DEVNULL)
        _cgroup_limits_ok = result.returncode == 0
        if not _cgroup_limits_ok:
            logger.warning('Cgroup resource limits (--cpus/--memory/--pids-limit) not available in this environment. Containers will run without CPU, memory or PID limits. To enable, delegate the cpu, memory and pids cgroup controllers to this container. Probe stderr: %s', (result.stderr or '').strip()[:500])
    except Exception as e:
        _cgroup_limits_ok = False
        logger.warning('Cgroup limit probe failed; disabling resource limits: %s', e)
    return _cgroup_limits_ok

def _ensure_docker_available() -> None:
    """Best-effort check that the docker CLI is available before use.

    Reuses ``find_docker()`` so this preflight stays consistent with the rest of
    the Docker backend, including known non-PATH Docker Desktop locations.
    """
    docker_exe = find_docker()
    if not docker_exe:
        logger.error('Docker backend selected but no docker executable was found in PATH or known install locations. Install Docker Desktop and ensure the CLI is available.')
        raise EnvironmentConnectionError("Docker executable not found in PATH or known install locations. Install Docker and ensure the 'docker' command is available.", retry_hint="Install Docker (or fix PATH) and retry, or switch terminal.backend to 'local'.")
    try:
        result = subprocess.run([docker_exe, 'version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        logger.error("Docker backend selected but the resolved docker executable '%s' could not be executed.", docker_exe, exc_info=True)
        raise EnvironmentConnectionError('Docker executable could not be executed. Check your Docker installation.', retry_hint='Repair the Docker installation and retry.')
    except subprocess.TimeoutExpired:
        logger.error("Docker backend selected but '%s version' timed out. The Docker daemon may not be running.", docker_exe, exc_info=True)
        raise EnvironmentConnectionError('Docker daemon is not responding. Ensure Docker is running and try again.', retry_hint='Start the Docker daemon (e.g. `systemctl start docker` or launch Docker Desktop), then retry the same command.')
    except Exception:
        logger.error('Unexpected error while checking Docker availability.', exc_info=True)
        raise
    else:
        if result.returncode != 0:
            logger.error("Docker backend selected but '%s version' failed (exit code %d, stderr=%s)", docker_exe, result.returncode, result.stderr.strip())
            raise EnvironmentConnectionError("Docker command is available but 'docker version' failed. Check your Docker installation.", retry_hint='The Docker daemon may be down or the current user lacks permission (docker group). Fix and retry.')

class DockerEnvironment(BaseEnvironment):
    """Hardened Docker container execution with resource limits and persistence.

    Security: all capabilities dropped, no privilege escalation, PID limits,
    size-limited tmpfs for scratch dirs. The container itself is the security
    boundary — the filesystem inside is writable so agents can install packages
    (pip, npm, apt) as needed. Writable workspace via tmpfs or bind mounts.

    Persistence: when enabled, bind mounts preserve /workspace and /root
    across container restarts.
    """
    _profile_scoped_passthrough = True

    def _additional_profile_scoped_passthrough_names(self) -> tuple[str, ...]:
        """Keep explicit docker_forward_env values out of shared snapshots."""
        return tuple(self._forward_env)

    def __init__(self, image: str, cwd: str='/root', timeout: int=60, cpu: float=0, memory: int=0, disk: int=0, persistent_filesystem: bool=False, task_id: str='default', volumes: list=None, forward_env: list[str] | None=None, env: dict | None=None, network: bool=True, host_cwd: str=None, auto_mount_cwd: bool=False, run_as_host_user: bool=False, extra_args: list=None, persist_across_processes: bool=True, shm_size: str=_DEFAULT_SHM_SIZE):
        if cwd == '~':
            cwd = '/root'
        super().__init__(cwd=cwd, timeout=timeout)
        self._persistent = persistent_filesystem
        self._persist_across_processes = persist_across_processes
        self._task_id = task_id
        self._forward_env = _normalize_forward_env_names(forward_env)
        self._env = _normalize_env_dict(env)
        self._init_unset_passthrough_names: tuple[str, ...] = ()
        self._container_id: Optional[str] = None
        self._labels: dict[str, str] = {}
        self._image: str = ''
        self._container_name: str = ''
        self._image_uses_s6_init: bool = False
        self._all_run_args: list[str] = []
        logger.info('DockerEnvironment volumes: %s', volumes)
        if volumes is not None and (not isinstance(volumes, list)):
            logger.warning('docker_volumes config is not a list: %r', volumes)
            volumes = []
        _ensure_docker_available()
        resource_args = []
        if cpu > 0 and _cgroup_limits_available(image):
            resource_args.extend(['--cpus', str(cpu)])
        if memory > 0 and _cgroup_limits_available(image):
            resource_args.extend(['--memory', f'{memory}m'])
        if _cgroup_limits_available(image):
            resource_args.extend(['--pids-limit', _DEFAULT_PIDS_LIMIT])
        shm = str(shm_size or '').strip()
        if shm and shm != '0' and (not _extra_args_set_shm_size(extra_args)):
            resource_args.extend(['--shm-size', shm])
        if disk > 0 and sys.platform != 'darwin':
            if self._storage_opt_supported():
                resource_args.extend(['--storage-opt', f'size={disk}m'])
            else:
                logger.warning('Docker storage driver does not support per-container disk limits (requires overlay2 on XFS with pquota). Container will run without disk quota.')
        if not network:
            resource_args.append('--network=none')
        from tools.environments.base import get_sandbox_dir
        volume_args = []
        workspace_explicitly_mounted = False
        for vol in volumes or []:
            if not isinstance(vol, str):
                logger.warning('Docker volume entry is not a string: %r', vol)
                continue
            vol = vol.strip()
            if not vol:
                continue
            if ':' in vol:
                volume_args.extend(['-v', vol])
                if ':/workspace' in vol:
                    workspace_explicitly_mounted = True
            else:
                logger.warning("Docker volume '%s' missing colon, skipping", vol)
        host_cwd_abs = os.path.abspath(os.path.expanduser(host_cwd)) if host_cwd else ''
        bind_host_cwd = auto_mount_cwd and bool(host_cwd_abs) and os.path.isdir(host_cwd_abs) and (not workspace_explicitly_mounted)
        if auto_mount_cwd and host_cwd and (not os.path.isdir(host_cwd_abs)):
            logger.debug('Skipping docker cwd mount: host_cwd is not a valid directory: %s', host_cwd)
        self._workspace_dir: Optional[str] = None
        self._home_dir: Optional[str] = None
        writable_args = []
        if self._persistent:
            sandbox = get_sandbox_dir() / 'docker' / task_id
            self._home_dir = str(sandbox / 'home')
            os.makedirs(self._home_dir, exist_ok=True)
            writable_args.extend(['-v', f'{self._home_dir}:/root'])
            if not bind_host_cwd and (not workspace_explicitly_mounted):
                self._workspace_dir = str(sandbox / 'workspace')
                os.makedirs(self._workspace_dir, exist_ok=True)
                writable_args.extend(['-v', f'{self._workspace_dir}:/workspace'])
        else:
            if not bind_host_cwd and (not workspace_explicitly_mounted):
                writable_args.extend(['--tmpfs', '/workspace:rw,exec,size=10g'])
            writable_args.extend(['--tmpfs', '/home:rw,exec,size=1g', '--tmpfs', '/root:rw,exec,size=1g'])
        if bind_host_cwd:
            logger.info('Mounting configured host cwd to /workspace: %s', host_cwd_abs)
            volume_args = ['-v', f'{host_cwd_abs}:/workspace', *volume_args]
        elif workspace_explicitly_mounted:
            logger.debug('Skipping docker cwd mount: /workspace already mounted by user config')
        try:
            from tools.credential_files import get_credential_file_mounts, get_skills_directory_mount, get_cache_directory_mounts
            for mount_entry in get_credential_file_mounts():
                src = Path(mount_entry['host_path'])
                if src.is_dir():
                    logger.warning('Docker: skipping credential mount — source is a directory (likely Docker-in-Docker auto-creation): %s', src)
                    continue
                if not src.is_file():
                    logger.warning('Docker: skipping credential mount — source not found: %s', src)
                    continue
                volume_args.extend(['-v', f"{mount_entry['host_path']}:{mount_entry['container_path']}:ro"])
                logger.info('Docker: mounting credential %s -> %s', mount_entry['host_path'], mount_entry['container_path'])
            for skills_mount in get_skills_directory_mount():
                src = Path(skills_mount['host_path'])
                if not src.is_dir():
                    logger.warning('Docker: skipping skills mount — source is not a directory: %s', src)
                    continue
                volume_args.extend(['-v', f"{skills_mount['host_path']}:{skills_mount['container_path']}:ro"])
                logger.info('Docker: mounting skills dir %s -> %s', skills_mount['host_path'], skills_mount['container_path'])
            for cache_mount in get_cache_directory_mounts():
                src = Path(cache_mount['host_path'])
                if not src.is_dir():
                    logger.warning('Docker: skipping cache mount — source is not a directory: %s', src)
                    continue
                volume_args.extend(['-v', f"{cache_mount['host_path']}:{cache_mount['container_path']}:ro"])
                logger.info('Docker: mounting cache dir %s -> %s', cache_mount['host_path'], cache_mount['container_path'])
        except Exception as e:
            logger.debug('Docker: could not load credential file mounts: %s', e)
        egress_volume_args, egress_env_overrides, egress_host_args = _egress_proxy_args_for_docker()
        egress_label = _egress_reuse_fingerprint(egress_volume_args, egress_env_overrides, egress_host_args)
        _enforce_egress = _egress_enforce_on_docker()
        _critical_egress_names = _critical_egress_env_names(egress_env_overrides)
        if egress_env_overrides:
            _forward_collisions = sorted((key for key in self._forward_env if key in _critical_egress_names))
            if _forward_collisions:
                _msg = f"docker_forward_env would inject real egress-protected variables {_forward_collisions}; enforce_on_docker is {('enabled' if _enforce_egress else 'disabled')}."
                if _enforce_egress:
                    raise RuntimeError(f'{_msg}  Remove these names from docker_forward_env or disable enforce_on_docker to opt out of egress isolation.')
                logger.warning('%s  Explicit docker_forward_env values will override egress tokens.', _msg)
        volume_args.extend(egress_volume_args)
        if egress_env_overrides:
            try:
                from hermes_cli.config import load_config as _load_cfg_for_collision
                _proxy_cfg = _load_cfg_for_collision().get('proxy') or {}
            except (ImportError, OSError):
                _proxy_cfg = {}
            except Exception as _e:
                try:
                    import yaml
                except ImportError:
                    raise
                logger.warning('Could not read proxy config for egress collision check: %s', _e)
                _proxy_cfg = {}
            _enforce_egress = bool(_proxy_cfg.get('enforce_on_docker', True))
            _critical_proxy_control = {'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS'}
            _critical_provider_keys: set[str] = set()
            try:
                from agent.proxy_sources import iron_proxy as _ip_for_mappings
                _critical_provider_keys = {m.real_env_name for m in _ip_for_mappings.load_mappings()}
            except Exception:
                pass
            _critical = _critical_proxy_control | _critical_provider_keys
            _collisions = sorted((k for k in _critical if k in self._env and (k not in egress_env_overrides or self._env[k] != egress_env_overrides[k]) and (k in _critical_provider_keys or (k in egress_env_overrides and self._env[k] != egress_env_overrides[k]))))
            if _collisions:
                _msg = f"docker_env in config.yaml overrides egress-proxy variables {_collisions}; enforce_on_docker is {('enabled' if _enforce_egress else 'disabled')}."
                if _enforce_egress:
                    raise RuntimeError(f'{_msg}  Remove these keys from docker_env or disable enforce_on_docker to opt out of egress isolation.')
                logger.warning('%s  Falling back to docker_env values; sandbox traffic will NOT route through the proxy.', _msg)
        try:
            from hermes_cli.config import load_config as _load_cfg_for_precedence
            _enforce_egress_merge = bool((_load_cfg_for_precedence().get('proxy') or {}).get('enforce_on_docker', True))
        except (ImportError, OSError):
            _enforce_egress_merge = True
        except Exception:
            _enforce_egress_merge = True
        if _enforce_egress_merge and egress_env_overrides:
            merged_env = dict(self._env)
            merged_env.update(egress_env_overrides)
        else:
            merged_env = dict(egress_env_overrides)
            merged_env.update(self._env)
        _egress_node_append = merged_env.pop('_HERMES_EGRESS_NODE_OPTIONS_APPEND', None)
        if _egress_node_append:
            existing_node = merged_env.get('NODE_OPTIONS', '')
            existing_tokens = existing_node.split()
            _CA_MODE_FLAGS = {'--use-openssl-ca', '--use-bundled-ca'}
            append_token = _egress_node_append.strip()
            if append_token in _CA_MODE_FLAGS:
                dropped = [t for t in existing_tokens if t in _CA_MODE_FLAGS and t != append_token]
                if dropped:
                    logger.warning('Overriding conflicting NODE_OPTIONS CA-mode flag(s) %s with egress-required %s to keep Node routed through the egress CA store.', dropped, append_token)
                existing_tokens = [t for t in existing_tokens if t not in _CA_MODE_FLAGS or t == append_token]
            if append_token not in existing_tokens:
                existing_tokens.append(append_token)
            merged_env['NODE_OPTIONS'] = ' '.join(existing_tokens).strip()
            if not merged_env['NODE_OPTIONS']:
                merged_env.pop('NODE_OPTIONS', None)
        env_args = []
        for key in sorted(merged_env):
            env_args.extend(['-e', f'{key}={merged_env[key]}'])
        user_args: list[str] = []
        if run_as_host_user:
            user_spec = _resolve_host_user_spec()
            if user_spec is not None:
                user_args = ['--user', user_spec]
                logger.info('Docker: running container as host user %s', user_spec)
            else:
                logger.warning('docker_run_as_host_user is enabled but this platform does not expose POSIX uid/gid; container will start as its image default user.')
        self._docker_exe = find_docker() or 'docker'
        image_uses_s6_init = _image_uses_init_entrypoint(self._docker_exe, image)
        if image_uses_s6_init:
            logger.info('Docker: image %s uses /init (s6-overlay) as entrypoint — skipping --init and mounting /run with exec.', image)
        security_args = _build_security_args(run_as_host_user and bool(user_args), run_exec=image_uses_s6_init)
        logger.info('Docker volume_args: %s', volume_args)
        validated_extra = []
        for arg in extra_args or []:
            if not isinstance(arg, str):
                logger.warning('Ignoring non-string docker_extra_args entry: %r', arg)
                continue
            validated_extra.append(arg)
        if egress_env_overrides:
            _extra_collisions = _extra_args_egress_collisions(validated_extra, _critical_egress_names)
            if _extra_collisions:
                _msg = f"docker_extra_args would override egress-proxy controls {_extra_collisions}; enforce_on_docker is {('enabled' if _enforce_egress else 'disabled')}."
                if _enforce_egress:
                    raise RuntimeError(f'{_msg}  Remove these args or disable enforce_on_docker to opt out of egress isolation.')
                logger.warning('%s  Extra Docker args may bypass egress isolation.', _msg)
        all_run_args = security_args + user_args + writable_args + resource_args + egress_host_args + volume_args + env_args + validated_extra
        logger.info('Docker run_args: %s', all_run_args)
        container_name = f'duck-agent-{uuid.uuid4().hex[:8]}'
        profile_name = _sanitize_label_value(_get_active_profile_name())
        task_label = _sanitize_label_value(task_id)
        label_args = ['--label', 'duck-agent=1', '--label', f'duck-agent-task-id={task_label}', '--label', f'duck-agent-profile={profile_name}', '--label', f'{_EGRESS_LABEL_KEY}={egress_label}']
        self._image = image
        self._container_name = container_name
        self._image_uses_s6_init = image_uses_s6_init
        self._all_run_args = all_run_args
        self._labels = {'duck-agent': '1', 'duck-agent-task-id': task_label, 'duck-agent-profile': profile_name, _EGRESS_LABEL_KEY: egress_label}
        reused = False
        if persist_across_processes:
            existing = self._find_reusable_container(task_label, profile_name, egress_label)
            if existing is not None:
                container_id, state = existing
                mode_mismatch = False
                actual_mode = None
                if not network:
                    actual_mode = self._container_network_mode(container_id)
                    mode_mismatch = actual_mode != 'none'
                if mode_mismatch:
                    logger.warning('Existing container %s has NetworkMode=%s but docker_network=false requests an air-gapped container — removing it and starting fresh (task=%s, profile=%s).', container_id[:12], actual_mode or 'unknown', task_label, profile_name)
                    try:
                        subprocess.run([self._docker_exe, 'rm', '-f', container_id], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=False, stdin=subprocess.DEVNULL)
                    except (subprocess.TimeoutExpired, OSError) as e:
                        logger.warning('Failed to remove mismatched container %s: %s', container_id[:12], e)
                    existing = None
            if existing is not None:
                container_id, state = existing
                self._container_id = container_id
                if state != 'running':
                    try:
                        subprocess.run([self._docker_exe, 'start', container_id], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=True, stdin=subprocess.DEVNULL)
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                        logger.warning('Failed to start existing container %s (state=%s): %s — falling back to a fresh container.', container_id[:12], state, e)
                        self._container_id = None
                if self._container_id:
                    logger.info('Reusing container %s (task=%s, profile=%s, prior state=%s)', container_id[:12], task_label, profile_name, state)
                    reused = True
        if not reused:
            init_args = [] if image_uses_s6_init else ['--init']
            run_cmd = [self._docker_exe, 'run', '-d', *init_args, '--name', container_name, *label_args, '-w', cwd, *all_run_args, image, 'sleep', 'infinity']
            logger.debug('Starting container: %s', ' '.join(run_cmd))
            try:
                result = subprocess.run(run_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, check=True, stdin=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logger.warning('docker run failed for %s, cleaning up orphaned container: %s', container_name, e)
                subprocess.run([self._docker_exe, 'rm', '-f', container_name], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
                raise
            self._container_id = result.stdout.strip()
            logger.info('Started container %s (%s)', container_name, self._container_id[:12])
        self._init_env_args = self._build_init_env_args()
        self.init_session()

    def _build_init_env_args(self) -> list[str]:
        """Build -e KEY=VALUE args for injecting host env vars into init_session.

        These are used during init_session() so that export -p captures the
        configured environment and the current profile's forwarded values.
        """
        passthrough_env, unset_names = self._resolve_passthrough_env()
        exec_env: dict[str, str] = dict(self._env)
        exec_env.update(passthrough_env)
        for name in unset_names:
            exec_env.pop(name, None)
        self._init_unset_passthrough_names = tuple(sorted(unset_names))
        args = []
        for key in sorted(exec_env):
            args.extend(['-e', f'{key}={exec_env[key]}'])
        return args

    def _build_passthrough_env(self) -> dict[str, str]:
        """Resolve forwarded host variables through the active profile scope."""
        return self._resolve_passthrough_env()[0]

    def _resolve_passthrough_env(self) -> tuple[dict[str, str], set[str]]:
        """Return forwarded values and scoped names that must be unset."""
        exec_env: dict[str, str] = {}
        explicit_forward_keys = set(self._forward_env)
        passthrough_keys: set[str] = set()
        resolve_passthrough_value = None
        multiplex_active = False
        is_global_env = lambda _name: False
        try:
            from tools.env_passthrough import get_all_passthrough, resolve_passthrough_value
            from agent.secret_scope import _is_global_env, is_multiplex_active as _is_multiplex_active
            is_global_env = _is_global_env
            multiplex_active = _is_multiplex_active()
            passthrough_keys = set(get_all_passthrough())
        except Exception:
            pass
        _implicit_forward = {k for k in passthrough_keys if not _is_hermes_internal_secret(k)}
        forward_keys = explicit_forward_keys | _implicit_forward - _HERMES_PROVIDER_ENV_BLOCKLIST
        hermes_env = _load_hermes_env_vars() if forward_keys else {}
        unset_names: set[str] = set()
        for key in sorted(forward_keys):
            value = os.getenv(key) or hermes_env.get(key)
            if resolve_passthrough_value is not None:
                value = resolve_passthrough_value(key, value)
            if value is not None:
                exec_env[key] = value
            elif multiplex_active and (not is_global_env(key)) and _ENV_VAR_NAME_RE.fullmatch(key):
                unset_names.add(key)
        return (exec_env, unset_names)

    def _build_runtime_env_args_with_unsets(self) -> tuple[list[str], tuple[str, ...]]:
        """Build runtime forwarding args plus names absent from the active scope."""
        passthrough_env, unset_names = self._resolve_passthrough_env()
        args = []
        for key in sorted(passthrough_env):
            args.extend(['-e', f'{key}={passthrough_env[key]}'])
        return (args, tuple(sorted(unset_names)))

    def _build_runtime_env_args(self) -> list[str]:
        """Build only dynamic forwarded values for a non-login command."""
        return self._build_runtime_env_args_with_unsets()[0]

    def _run_bash(self, cmd_string: str, *, login: bool=False, timeout: int=120, stdin_data: str | None=None) -> subprocess.Popen:
        """Spawn a bash process inside the Docker container."""
        assert self._container_id, 'Container not started'
        cmd = [self._docker_exe, 'exec']
        if stdin_data is not None:
            cmd.append('-i')
        unset_names: tuple[str, ...] = ()
        if login:
            cmd.extend(self._init_env_args)
        elif self._profile_scoped_passthrough:
            runtime_args, unset_names = self._build_runtime_env_args_with_unsets()
            cmd.extend(runtime_args)
        if login:
            unset_names = getattr(self, '_init_unset_passthrough_names', ())
        if unset_names:
            quoted_names = ' '.join((shlex.quote(name) for name in unset_names))
            cmd_string = f'unset {quoted_names} 2>/dev/null || true\n{cmd_string}'
        cmd.extend([self._container_id])
        if login:
            cmd.extend(['bash', '-l', '-c', cmd_string])
        else:
            cmd.extend(['bash', '-c', cmd_string])
        return _popen_bash(cmd, stdin_data)
    _NO_CONTAINER_PATTERNS = ('No such container', 'is not running', 'no such container')

    def _is_container_gone(self, output: str) -> bool:
        """Return True if the output indicates the container no longer exists."""
        return any((p in output for p in self._NO_CONTAINER_PATTERNS))

    def _recreate_container(self) -> bool:
        """Recreate the container after it was removed out-of-band.

        Tries label-based reuse first; if no existing container is found,
        starts a fresh one with the same image and run-args.  Returns True
        on success, False if recreation fails (caller should surface the
        original error).
        """
        old_id = (self._container_id or '')[:12]
        logger.warning('Container %s appears to be gone — attempting recovery', old_id)
        self._container_id = None
        task_label = self._labels.get('duck-agent-task-id', '')
        profile_label = self._labels.get('duck-agent-profile', '')
        existing = self._find_reusable_container(task_label, profile_label, self._labels.get(_EGRESS_LABEL_KEY, 'off'))
        if existing is not None:
            cid, state = existing
            if state == 'running':
                self._container_id = cid
                logger.info('Recovery: reusing running container %s', cid[:12])
            else:
                try:
                    subprocess.run([self._docker_exe, 'start', cid], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=True, stdin=subprocess.DEVNULL)
                    self._container_id = cid
                    logger.info('Recovery: restarted container %s', cid[:12])
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    logger.warning('Recovery: failed to start container %s: %s', cid[:12], e)
        if not self._container_id:
            if not self._image:
                logger.error('Recovery: no saved image name, cannot recreate container')
                return False
            try:
                import uuid as _uuid
                new_name = f'duck-agent-{_uuid.uuid4().hex[:8]}'
                init_args = [] if self._image_uses_s6_init else ['--init']
                label_args = []
                for k, v in self._labels.items():
                    label_args.extend(['--label', f'{k}={v}'])
                run_cmd = [self._docker_exe, 'run', '-d', *init_args, '--name', new_name, *label_args, '-w', self.cwd, *self._all_run_args, self._image, 'sleep', 'infinity']
                result = subprocess.run(run_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, check=True, stdin=subprocess.DEVNULL)
                self._container_id = result.stdout.strip()
                self._container_name = new_name
                logger.info('Recovery: created fresh container %s (%s)', new_name, self._container_id[:12])
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.error('Recovery: failed to create new container: %s', e)
                return False
        try:
            self._snapshot_ready = False
            self.init_session()
        except Exception as e:
            logger.error('Recovery: init_session failed in new container: %s', e)
            return False
        logger.info('Recovery successful — new container %s', (self._container_id or '')[:12])
        return True

    def execute(self, command: str, cwd: str='', **kwargs) -> dict:
        """Execute a command, auto-recovering from dead containers.

        If the container was removed out-of-band (idle reaper, docker prune,
        OOM kill, daemon restart), detect the error and recreate the container
        transparently before retrying once.
        """
        result = super().execute(command, cwd, **kwargs)
        if result.get('returncode', 0) != 0 and self._is_container_gone(result.get('output', '')) and self._persist_across_processes:
            if self._recreate_container():
                result = super().execute(command, cwd, **kwargs)
        return result

    @staticmethod
    def _storage_opt_supported() -> bool:
        """Check if Docker's storage driver supports --storage-opt size=.
        
        Only overlay2 on XFS with pquota supports per-container disk quotas.
        Ubuntu (and most distros) default to ext4, where this flag errors out.
        """
        global _storage_opt_ok
        if _storage_opt_ok is not None:
            return _storage_opt_ok
        try:
            docker = find_docker() or 'docker'
            result = subprocess.run([docker, 'info', '--format', '{{.Driver}}'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, stdin=subprocess.DEVNULL)
            driver = result.stdout.strip().lower()
            if driver != 'overlay2':
                _storage_opt_ok = False
                return False
            probe = subprocess.run([docker, 'create', '--storage-opt', 'size=1m', 'hello-world'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, stdin=subprocess.DEVNULL)
            if probe.returncode == 0:
                container_id = probe.stdout.strip()
                if container_id:
                    subprocess.run([docker, 'rm', container_id], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
                _storage_opt_ok = True
            else:
                _storage_opt_ok = False
        except Exception:
            _storage_opt_ok = False
        logger.debug('Docker --storage-opt support: %s', _storage_opt_ok)
        return _storage_opt_ok

    def _container_network_mode(self, container_id: str) -> Optional[str]:
        """Return the container's ``HostConfig.NetworkMode`` (e.g. ``bridge``,
        ``none``, ``host``), or ``None`` when inspection fails.

        Used by the reuse path to make sure a persisted container's network
        mode still matches the operator's ``docker_network`` setting; callers
        treat ``None`` (unknown) as a mismatch when lockdown was requested,
        so a failed inspect fails closed rather than open.
        """
        try:
            result = subprocess.run([self._docker_exe, 'inspect', '--format', '{{.HostConfig.NetworkMode}}', container_id], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug('docker inspect NetworkMode failed: %s', e)
            return None
        if result.returncode != 0:
            logger.debug('docker inspect NetworkMode returned %d: %s', result.returncode, result.stderr.strip())
            return None
        mode = result.stdout.strip()
        return mode or None

    def _find_reusable_container(self, task_label: str, profile_label: str, egress_label: str) -> Optional[tuple[str, str]]:
        """Look for an existing container labeled for this (task, profile).

        Returns ``(container_id, state)`` on hit, ``None`` on miss / on any
        failure (including ``docker ps`` itself failing). State is one of the
        values Docker reports via ``{{.State}}`` — e.g. ``running``, ``exited``,
        ``created``, ``paused``, ``restarting``, ``dead``. The caller decides
        whether the state warrants ``docker start`` before reuse.

        Restricted to the docker-stored label set this class creates; never
        matches containers that happened to be named ``duck-agent-*`` but were
        started by some other tool.
        """
        try:
            filters = ['--filter', 'label=duck-agent=1', '--filter', f'label=duck-agent-task-id={task_label}', '--filter', f'label=duck-agent-profile={profile_label}']
            if egress_label != 'off':
                filters.extend(['--filter', f'label={_EGRESS_LABEL_KEY}={egress_label}'])
                fmt = '{{.ID}}\t{{.State}}'
            else:
                fmt = '{{.ID}}\t{{.State}}\t{{.Label "' + _EGRESS_LABEL_KEY + '"}}'
            result = subprocess.run([self._docker_exe, 'ps', '-a', *filters, '--format', fmt], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug('docker ps probe failed: %s — will start a fresh container', e)
            return None
        if result.returncode != 0:
            logger.debug('docker ps probe returned %d: %s — will start a fresh container', result.returncode, result.stderr.strip())
            return None
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        running = None
        first = None
        for ln in lines:
            if egress_label == 'off':
                parts = ln.split('\t', 2)
                if len(parts) < 3:
                    continue
                cid, state, egress_val = (parts[0], parts[1].lower(), parts[2])
                if egress_val not in ('', '<no value>', 'off'):
                    logger.debug('skipping container %s for egress=off reuse: label %s=%r', cid, _EGRESS_LABEL_KEY, egress_val)
                    continue
            else:
                parts = ln.split('\t', 1)
                if len(parts) != 2:
                    continue
                cid, state = (parts[0], parts[1].lower())
            if first is None:
                first = (cid, state)
            if state == 'running' and running is None:
                running = (cid, state)
        return running or first

    def cleanup(self, *, force_remove: bool=False):
        """Tear down the container according to persist mode and *force_remove*.

        Persist-mode (``persist_across_processes=True``, the default) leaves the
        container **running** untouched. The docs promise "ONE long-lived
        container shared across sessions" and stopping it on every Duck Agent exit
        breaks that promise:

        * Background processes inside the container (``npm run dev``, watchers,
          long-running pytest) get killed every time the user runs ``/quit``.
        * Every reuse requires ``docker start`` + waiting for the container to
          come back up, adding 1–2s to the first tool call of the new session.
        * The user-visible difference between "ONE long-lived container" and
          "a new container that happens to share state" is exactly this:
          processes survive in the former, die in the latter.

        Resource reclamation for the persist-mode case lives in the
        ``reap_orphan_containers()`` path (see issue #20561 commit 3): if no
        Duck Agent process touches a labeled container for ``2 × lifetime_seconds``
        it gets ``docker rm -f``'d at the next Duck Agent startup. That covers the
        SIGKILL / OOM / abandoned-laptop cases without us needing to stop the
        container on every graceful exit.

        Opt-out mode (``persist_across_processes=False``) still does
        ``docker stop`` + ``docker rm -f`` on every cleanup, matching the
        pre-PR behavior for users who explicitly want per-process isolation.

        ``force_remove=True`` overrides persist mode and always tears the
        container down (``docker stop`` + ``docker rm -f``). This is the
        explicit-teardown path for ``/reset``, ``cleanup_vm(task_id)``-driven
        resets, or any caller that wants a guaranteed fresh container on next
        ``DockerEnvironment(task_id=...)``. No current caller passes
        ``force_remove=True``; the parameter is here so the explicit-teardown
        semantics can be wired up later without changing this method's
        signature.

        Cleanup runs on a daemon thread with bounded ``subprocess.run`` calls
        (not the racy ``Popen(... &)`` pattern from before PR #33645). The
        atexit hook in ``tools/terminal_tool.py`` waits up to 15s for the
        thread to finish before the interpreter exits, so ``docker stop`` /
        ``docker rm`` actually completes when we do trigger it.
        """
        container_id = self._container_id
        if not container_id:
            if not self._persistent:
                for d in (self._workspace_dir, self._home_dir):
                    if d:
                        shutil.rmtree(d, ignore_errors=True)
            return
        if force_remove:
            should_stop = True
            should_remove = True
        elif self._persist_across_processes:
            self._container_id = None
            return
        else:
            should_stop = True
            should_remove = True
        docker_exe = self._docker_exe
        log_id = container_id[:12]

        def _do_cleanup() -> None:
            if should_stop:
                try:
                    subprocess.run([docker_exe, 'stop', '-t', '10', container_id], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning('docker stop %s timed out / failed: %s', log_id, e)
            if should_remove:
                try:
                    subprocess.run([docker_exe, 'rm', '-f', container_id], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning('docker rm -f %s failed: %s', log_id, e)
        import threading
        t = threading.Thread(target=_do_cleanup, daemon=True, name=f'duck-agent-cleanup-{log_id}')
        t.start()
        self._cleanup_thread = t
        self._container_id = None
        if should_remove and (not self._persistent):
            for d in (self._workspace_dir, self._home_dir):
                if d:
                    shutil.rmtree(d, ignore_errors=True)

    def wait_for_cleanup(self, timeout: float=30.0) -> bool:
        """Block up to *timeout* seconds for the cleanup worker thread.

        Returns ``True`` if the thread finished (or no thread was started),
        ``False`` on timeout. The atexit hook in terminal_tool.py calls this
        on every active environment so docker stop/rm actually completes
        before the Python process exits — without this, ``duck-agent /quit``
        races the interpreter shutdown and leaves stopped containers behind.
        """
        thread = getattr(self, '_cleanup_thread', None)
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()