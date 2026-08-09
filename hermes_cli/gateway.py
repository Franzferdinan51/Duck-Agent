"""
Gateway subcommand for duck-agent CLI.

Handles: duck-agent gateway [run|start|stop|restart|status|install|uninstall|setup]
"""
import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
if os.name == 'posix':
    _sys_dirs = {'/bin', '/usr/bin', '/usr/sbin', '/sbin'}
    _path_dirs = set(os.environ.get('PATH', '').split(os.pathsep))
    _missing = _sys_dirs - _path_dirs
    if _missing:
        os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + os.pathsep.join(sorted(_missing))
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
from gateway.config import coerce_systemd_watchdog_seconds, load_gateway_config
from gateway.status import terminate_pid
from gateway.restart import DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT, EXTERNAL_GATEWAY_SUPERVISOR_ENV, GATEWAY_FATAL_CONFIG_EXIT_CODE, GATEWAY_SERVICE_RESTART_EXIT_CODE, is_gateway_supervisor_process, parse_restart_after_turn_timeout, parse_restart_drain_timeout, resolve_restart_exit_wait_budget
from hermes_cli.config import get_env_value, get_hermes_home, is_managed, managed_error, read_raw_config, save_env_value, write_platform_config_field
from hermes_cli.setup import print_header, print_info, print_success, print_warning, print_error, prompt, prompt_choice, prompt_yes_no
from hermes_cli.colors import Colors, color
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and (not self.service_running)

@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    path: Path
    pid: int

def _get_service_pids() -> set:
    """Return PIDs currently managed by systemd or launchd gateway services.

    Used to avoid killing freshly-restarted service processes when sweeping
    for stale manual gateway processes after a service restart.  Relies on the
    service manager having committed the new PID before the restart command
    returns (true for both systemd and launchd in practice).
    """
    pids: set = set()
    if supports_systemd_services():
        for scope_args in [['systemctl', '--user'], ['systemctl']]:
            try:
                result = subprocess.run(scope_args + ['list-units', 'duck-agent-gateway*', '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if not parts or not parts[0].endswith('.service'):
                        continue
                    svc = parts[0]
                    try:
                        show = subprocess.run(scope_args + ['show', svc, '--property=MainPID', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                        pid = int(show.stdout.strip())
                        if pid > 0:
                            pids.add(pid)
                    except (ValueError, subprocess.TimeoutExpired):
                        pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    if is_macos():
        try:
            label = get_launchd_label()
            result = subprocess.run(['launchctl', 'list', label], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            if result.returncode == 0:
                pid = _parse_launchd_pid_from_list_output(result.stdout)
                if pid is not None and pid > 0:
                    pids.add(pid)
                else:
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and parts[2] == label:
                            try:
                                pid = int(parts[0])
                                if pid > 0:
                                    pids.add(pid)
                            except ValueError:
                                pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return pids

def _get_parent_pid(pid: int) -> int | None:
    """Return the parent PID for ``pid``, or ``None`` when unavailable.

    Uses psutil (core dependency) which works on every platform.  The
    older implementation shelled out to ``ps -o ppid= -p <pid>``, which
    silently fails on Windows (no ``ps``) so the ancestor walk terminated
    at self — the caller's dedup / exclude logic then couldn't distinguish
    "duck-agent CLI that invoked this scan" from "real gateway process".
    """
    if pid <= 1:
        return None
    try:
        import psutil
        return psutil.Process(pid).ppid() or None
    except ImportError:
        pass
    except Exception:
        return None
    if is_windows():
        return None
    if not shutil.which('ps'):
        return None
    try:
        result = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw.splitlines()[-1].strip())
    except ValueError:
        return None
    return parent_pid if parent_pid > 0 else None

def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
    """Return True when ``target_pid`` is this process or one of its ancestors."""
    if target_pid <= 0:
        return False
    pid = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == target_pid:
            return True
        seen.add(pid)
        pid = _get_parent_pid(pid) or 0
    return False

def _request_gateway_self_restart(pid: int) -> bool:
    """Ask a running gateway ancestor to restart itself asynchronously."""
    if not hasattr(signal, 'SIGUSR1'):
        return False
    if not _is_pid_ancestor_of_current_process(pid):
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True

def _graceful_restart_via_sigusr1(pid: int, drain_timeout: float) -> bool:
    """Send SIGUSR1 to a gateway PID and wait for it to exit gracefully.

    SIGUSR1 is wired in gateway/run.py to ``request_restart(via_service=True)``,
    which refuses new turns, waits for in-flight work up to
    ``agent.restart_after_turn_timeout``, then runs ``stop()`` (force-interrupt
    budget ``agent.restart_drain_timeout``) and exits.  Both systemd
    (``Restart=always``) and launchd (unconditional KeepAlive) restart on
    any exit.

    This is the drain-aware alternative to ``systemctl restart`` / ``SIGTERM``,
    which SIGKILL in-flight agents after a short timeout.

    Args:
        pid: Gateway process PID (systemd MainPID, launchd PID, or bare
            process PID).
        drain_timeout: Seconds to wait for the process to exit after sending
            SIGUSR1.  Must cover the after-turn wait plus the stop()/drain
            phase (#77184); callers should pass
            ``resolve_restart_exit_wait_budget(...)``.

    Returns:
        True if the PID was signalled and exited within the timeout.
        False if SIGUSR1 couldn't be sent or the process didn't exit in
        time (caller should fall back to a harder restart path).
    """
    if not hasattr(signal, 'SIGUSR1'):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return _wait_for_pid_exit(pid, max(drain_timeout, 1.0))

def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to leave the process table.

    ``launchctl bootstrap`` of a label whose previous instance is still draining
    fails with EIO ("already loaded"), so callers that tear the gateway down
    must wait for the old process to actually exit before re-bootstrapping.

    Returns True once the PID is gone (or was never alive), False on timeout.
    """
    if pid <= 0:
        return True
    import time as _time
    from gateway.status import _pid_exists
    deadline = _time.monotonic() + max(timeout, 0.0)
    while True:
        if not _pid_exists(pid):
            return True
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.5)

def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the current process's ancestor chain.

    Walks from the current PID up to PID 1 (init) so that process-table scans
    never match the calling CLI process or any of its parents.  This prevents
    ``duck-agent gateway status`` from falsely counting the ``duck-agent`` CLI that
    invoked it as a running gateway instance (see #13242).
    """
    ancestors: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        ancestors.add(pid)
        parent = _get_parent_pid(pid)
        if parent is None or parent <= 0 or parent in ancestors:
            break
        pid = parent
    return ancestors

def _append_unique_pid(pids: list[int], pid: int | None, exclude_pids: set[int]) -> None:
    if pid is None or pid <= 0:
        return
    if pid == os.getpid() or pid in exclude_pids or pid in pids:
        return
    pids.append(pid)

def _scan_gateway_pids(exclude_pids: set[int], all_profiles: bool=False, include_restart_managers: bool=False) -> list[int]:
    """Best-effort process-table scan for gateway PIDs.

    This supplements the profile-scoped PID file so status views can still spot
    a live gateway when the PID file is stale/missing, and ``--all`` sweeps can
    discover gateways outside the current profile.
    """
    exclude_pids = exclude_pids | _get_ancestor_pids()
    pids: list[int] = []
    from gateway.status import looks_like_gateway_command_line, looks_like_gateway_runtime_command_line
    current_home = str(get_hermes_home().resolve())
    current_home_lc = current_home.lower().replace('\\', '/')
    current_profile_arg = _profile_arg(current_home)
    current_profile_name = current_profile_arg.split()[-1] if current_profile_arg else ''
    current_profile_name_lc = current_profile_name.lower()

    def _matches_current_profile(command: str) -> bool:
        command_lc = command.lower().replace('\\', '/')
        if current_profile_name:
            return f'--profile {current_profile_name_lc}' in command_lc or f'-p {current_profile_name_lc}' in command_lc or f'duck_agent_home={current_home_lc}' in command_lc
        if '--profile ' in command_lc or ' -p ' in command_lc:
            return False
        if 'duck_agent_home=' in command_lc and f'duck_agent_home={current_home_lc}' not in command_lc:
            return False
        return True

    def _matches_gateway_runtime(command: str) -> bool:
        if looks_like_gateway_command_line(command):
            return True
        return include_restart_managers and looks_like_gateway_runtime_command_line(command)
    try:
        if is_windows():
            from hermes_cli._subprocess_compat import windows_hide_flags
            _no_window = {'creationflags': windows_hide_flags()}
            wmic_path = shutil.which('wmic')
            result = None
            if wmic_path is not None:
                try:
                    result = subprocess.run([wmic_path, 'process', 'get', 'ProcessId,CommandLine', '/FORMAT:LIST'], capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=10, **_no_window)
                except (OSError, subprocess.TimeoutExpired):
                    result = None
            if result is None or result.returncode != 0 or (not (result.stdout or '')):
                powershell = shutil.which('powershell') or shutil.which('pwsh')
                if powershell is None:
                    return []
                ps_cmd = 'Get-CimInstance Win32_Process | ForEach-Object {   \'CommandLine=\' + ($_.CommandLine -replace "`r`n",\' \' -replace "`n",\' \');   \'ProcessId=\' + $_.ProcessId;   \'\' }'
                try:
                    result = subprocess.run([powershell, '-NoProfile', '-Command', ps_cmd], capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=15, **_no_window)
                except (OSError, subprocess.TimeoutExpired):
                    return []
            if result.returncode != 0 or result.stdout is None:
                return []
            current_cmd = ''
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith('CommandLine='):
                    current_cmd = line[len('CommandLine='):]
                elif line.startswith('ProcessId='):
                    pid_str = line[len('ProcessId='):]
                    if _matches_gateway_runtime(current_cmd) and (all_profiles or _matches_current_profile(current_cmd)):
                        try:
                            _append_unique_pid(pids, int(pid_str), exclude_pids)
                        except ValueError:
                            pass
                    current_cmd = ''
        else:
            _found_via_proc = False
            if os.path.isdir('/proc'):
                try:
                    my_pid = os.getpid()
                    for entry in os.listdir('/proc'):
                        if not entry.isdigit():
                            continue
                        pid = int(entry)
                        if pid == my_pid or pid in exclude_pids:
                            continue
                        try:
                            with open(f'/proc/{pid}/cmdline', 'rb') as _f:
                                cmdline = _f.read().decode('utf-8', errors='replace')
                            cmdline = cmdline.replace('\x00', ' ')
                            if _matches_gateway_runtime(cmdline) and (all_profiles or _matches_current_profile(cmdline)):
                                _append_unique_pid(pids, pid, exclude_pids)
                        except (OSError, PermissionError):
                            continue
                    _found_via_proc = True
                except Exception:
                    pass
            if not _found_via_proc:
                result = subprocess.run(['ps', '-A', 'eww', '-o', 'pid=,command='], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                if result.returncode != 0:
                    return []
                for line in result.stdout.split('\n'):
                    stripped = line.strip()
                    if not stripped or 'grep' in stripped:
                        continue
                    pid = None
                    command = ''
                    parts = stripped.split(None, 1)
                    if len(parts) == 2:
                        try:
                            pid = int(parts[0])
                            command = parts[1]
                        except ValueError:
                            pid = None
                    if pid is None:
                        aux_parts = stripped.split()
                        if len(aux_parts) > 10 and aux_parts[1].isdigit():
                            pid = int(aux_parts[1])
                            command = ' '.join(aux_parts[10:])
                    if pid is None:
                        continue
                    if _matches_gateway_runtime(command) and (all_profiles or _matches_current_profile(command)):
                        _append_unique_pid(pids, pid, exclude_pids)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if is_windows() and len(pids) > 1:
        pids = _filter_venv_launcher_stubs(pids)
    return pids

def _filter_venv_launcher_stubs(pids: list[int]) -> list[int]:
    """Drop venv-launcher ``pythonw.exe`` stubs that are parents of the real
    interpreter process.  See comment at the tail of ``_scan_gateway_pids``.

    Uses ``psutil`` (core dependency).  Safe on any platform; only invoked
    on Windows by the caller because the stub pattern is Windows-specific.
    """
    try:
        import psutil
    except ImportError:
        return pids
    pid_set = set(pids)
    parent_of: dict[int, int | None] = {}
    for pid in pids:
        try:
            parent_of[pid] = psutil.Process(pid).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent_of[pid] = None
    drop: set[int] = set()
    for pid, ppid in parent_of.items():
        if ppid is not None and ppid in pid_set:
            drop.add(ppid)
    return [p for p in pids if p not in drop]

def find_gateway_pids(exclude_pids: set | None=None, all_profiles: bool=False) -> list:
    """Find PIDs of running gateway processes.

    Args:
        exclude_pids: PIDs to exclude from the result (e.g. service-managed
            PIDs that should not be killed during a stale-process sweep).
        all_profiles: When ``True``, return gateway PIDs across **all**
            profiles (the pre-7923 global behaviour).  ``duck-agent update``
            needs this because a code update affects every profile.
            When ``False`` (default), only PIDs belonging to the current
            Duck Agent profile are returned.
    """
    _exclude = set(exclude_pids or set())
    pids: list[int] = []
    if not all_profiles:
        try:
            from gateway.status import get_running_pid
            _append_unique_pid(pids, get_running_pid(), _exclude)
        except Exception:
            pass
    for pid in _get_service_pids():
        _append_unique_pid(pids, pid, _exclude)
    try:
        include_restart_managers = not supports_systemd_services()
    except Exception:
        include_restart_managers = False
    for pid in _scan_gateway_pids(_exclude, all_profiles=all_profiles, include_restart_managers=include_restart_managers):
        _append_unique_pid(pids, pid, _exclude)
    return pids

def find_profile_gateway_processes(exclude_pids: set | None=None) -> list[ProfileGatewayProcess]:
    """Return running gateway PIDs mapped to Duck Agent profiles via PID files."""
    _exclude = set(exclude_pids or set())
    processes: list[ProfileGatewayProcess] = []
    try:
        from gateway.status import get_running_pid
        from hermes_cli.profiles import list_profiles
    except Exception:
        return processes
    seen: set[int] = set()
    for profile in list_profiles():
        try:
            pid = get_running_pid(profile.path / 'gateway.pid', cleanup_stale=False)
        except Exception:
            continue
        if pid is None or pid <= 0 or pid in _exclude or (pid in seen):
            continue
        seen.add(pid)
        processes.append(ProfileGatewayProcess(profile=profile.name, path=profile.path, pid=pid))
    return processes

def _gateway_run_args_for_profile(profile: str) -> list[str]:
    args = [get_python_path(), '-m', 'hermes_cli.main']
    if profile != 'default':
        args.extend(['--profile', profile])
    args.extend(['gateway', 'run', '--replace'])
    return args

def _capture_gateway_argv(pid: int) -> list[str] | None:
    """Return the live argv of a running gateway process, or ``None``.

    Used to respawn gateways that have no profile→PID-file mapping (e.g. a
    Windows Scheduled Task running ``pythonw.exe -m hermes_cli.main gateway
    run``). ``_pause_windows_gateways_for_update`` force-kills such gateways
    before mutating the venv; without their original command line we cannot
    bring them back, so we snapshot it here before the kill.

    Best-effort: returns ``None`` if psutil is unavailable, the process is
    gone, access is denied, or the argv doesn't look like a gateway command.
    """
    if pid <= 1:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        argv = list(psutil.Process(pid).cmdline() or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    except Exception:
        return None
    if not argv:
        return None
    try:
        from gateway.status import looks_like_gateway_command_line
        if not looks_like_gateway_command_line(' '.join(argv)):
            return None
    except Exception:
        pass
    return argv

def _prepare_profile_gateway_update_restart(profile: str, pid: int) -> str | None:
    """Choose who relaunches a profile gateway after ``duck-agent update``.

    A gateway started with ``--external-supervisor`` must exit back to that
    manager. Starting Duck Agent'ss detached watcher as well would escape the
    manager and race its replacement process. Ordinary foreground gateways
    retain the existing detached-watcher behavior.
    """
    argv = _capture_gateway_argv(pid)
    if argv and '--external-supervisor' in argv:
        return 'external-supervisor'
    if launch_detached_profile_gateway_restart(profile, pid):
        return 'detached'
    return None

def launch_detached_gateway_restart_by_cmdline(old_pid: int, run_argv: list[str]) -> bool:
    """Relaunch a gateway by replaying its captured command line after exit.

    Companion to ``launch_detached_profile_gateway_restart`` for gateways that
    have no profile→PID-file mapping (Scheduled-Task / manually-launched
    ``gateway run`` whose DUCK_AGENT_HOME or argv doesn't match a known profile).
    Uses the identical detached-watcher mechanism; only the respawn argv
    differs (the process's own argv instead of a profile-derived one).
    """
    if old_pid <= 0 or not run_argv:
        return False
    return _spawn_gateway_restart_watcher(old_pid, list(run_argv))

def launch_detached_profile_gateway_restart(profile: str, old_pid: int) -> bool:
    """Relaunch a manually-run profile gateway after its current PID exits."""
    if old_pid <= 0:
        return False
    return _spawn_gateway_restart_watcher(old_pid, _gateway_run_args_for_profile(profile))

def _spawn_gateway_restart_watcher(old_pid: int, run_argv: list[str]) -> bool:
    """Spawn the detached watcher that respawns ``run_argv`` once ``old_pid`` exits."""
    if old_pid <= 0 or not run_argv:
        return False
    from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway, windows_detach_popen_kwargs
    respawn_cwd = ''
    respawn_env_overlay: dict[str, str] = {}
    if sys.platform == 'win32':
        try:
            from hermes_cli.gateway_windows import windowless_gateway_restart_spec
            run_argv, respawn_cwd, respawn_env_overlay = windowless_gateway_restart_spec(list(run_argv))
        except Exception:
            respawn_cwd = ''
            respawn_env_overlay = {}
    respawn_cwd_literal = json.dumps(respawn_cwd)
    respawn_env_literal = json.dumps(respawn_env_overlay)
    watcher = textwrap.dedent('\n        import os\n        import subprocess\n        import sys\n        import time\n        from hermes_cli._subprocess_compat import (\n            windows_detach_flags,\n            windows_detach_flags_without_breakaway,\n        )\n\n        pid = int(sys.argv[1])\n        cmd = sys.argv[2:]\n        _respawn_cwd = {respawn_cwd_literal}\n        _respawn_env_overlay = {respawn_env_literal}\n        deadline = time.monotonic() + 120\n        while time.monotonic() < deadline:\n            # ``os.kill(pid, 0)`` is not a no-op on Windows — use the\n            # cross-platform existence check.\n            from gateway.status import _pid_exists\n            if not _pid_exists(pid):\n                break\n            time.sleep(0.2)\n\n        # Platform-appropriate detach for the respawned gateway.  On POSIX\n        # start_new_session=True maps to os.setsid; on Windows we need\n        # explicit creationflags because start_new_session is a no-op there.\n        # CREATE_BREAKAWAY_FROM_JOB is critical: the watcher itself may have\n        # been spawned inside a job object (Electron/Tauri parent), and\n        # without breakaway the respawned gateway would die when that job\n        # tears down. See _subprocess_compat.windows_detach_flags().\n        _popen_kwargs = {{\n            "stdout": subprocess.DEVNULL,\n            "stderr": subprocess.DEVNULL,\n        }}\n        # Anchor the respawned gateway at the stable working dir and overlay\n        # the env (VIRTUAL_ENV / PYTHONPATH / DUCK_AGENT_HOME) the windowless\n        # base interpreter needs to import hermes_cli.  Empty on POSIX, where\n        # the venv python resolves imports without help.\n        if _respawn_cwd:\n            _popen_kwargs["cwd"] = _respawn_cwd\n        if _respawn_env_overlay:\n            _popen_kwargs["env"] = {{**os.environ, **_respawn_env_overlay}}\n        if sys.platform == "win32":\n            try:\n                _popen_kwargs["creationflags"] = windows_detach_flags()\n                subprocess.Popen(cmd, **_popen_kwargs)\n            except OSError:\n                # CREATE_BREAKAWAY_FROM_JOB can be rejected with\n                # ERROR_ACCESS_DENIED when the parent\'s job object refuses\n                # breakaway. Retry without it — DETACHED_PROCESS et al.\n                # alone are enough in most setups. Mirrors the canonical\n                # fallback in gateway_windows._spawn_detached.\n                _popen_kwargs["creationflags"] = windows_detach_flags_without_breakaway()\n                subprocess.Popen(cmd, **_popen_kwargs)\n        else:\n            _popen_kwargs["start_new_session"] = True\n            subprocess.Popen(cmd, **_popen_kwargs)\n        ').strip().format(respawn_cwd_literal=respawn_cwd_literal, respawn_env_literal=respawn_env_literal)
    watcher_argv = [sys.executable, '-c', watcher, str(old_pid), *run_argv]
    try:
        subprocess.Popen(watcher_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **windows_detach_popen_kwargs())
    except OSError:
        try:
            fallback_kwargs: dict = {'creationflags': windows_detach_flags_without_breakaway()} if sys.platform == 'win32' else {'start_new_session': True}
            subprocess.Popen(watcher_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **fallback_kwargs)
        except OSError:
            return False
    return True

def _probe_systemd_service_running(system: bool=False) -> tuple[bool, bool]:
    selected_system = _select_systemd_scope(system)
    unit_exists = get_systemd_unit_path(system=selected_system).exists()
    if not unit_exists:
        return (selected_system, False)
    try:
        result = _run_systemctl(['is-active', get_service_name()], system=selected_system, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except (RuntimeError, subprocess.TimeoutExpired):
        return (selected_system, False)
    return (selected_system, result.stdout.strip() == 'active')

def _read_systemd_unit_environment(system: bool=False) -> dict[str, str]:
    """Parse the gateway unit's ``Environment=`` directives.

    ``systemctl show -p Environment`` returns a single line of
    space-separated ``KEY=VALUE`` pairs; values are not quoted in the output
    even when the unit file quoted them. We split on whitespace and ``=``.
    """
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(['show', get_service_name(), '--no-pager', '--property', 'Environment'], system=selected_system, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith('Environment='):
            continue
        body = line[len('Environment='):].strip()
        for token in body.split():
            if '=' not in token:
                continue
            key, value = token.split('=', 1)
            parsed[key] = value
    return parsed

def _hermes_home_from_systemd_unit_file(system: bool=False) -> str | None:
    """Read ``DUCK_AGENT_HOME`` from the on-disk unit file (not ``systemctl show``).

    Prefer the file when refreshing/comparing: under ``sudo``, ``systemctl``
    may be slow/unavailable in tests, and the on-disk unit is what
    ``systemd_unit_is_current`` / ``refresh_systemd_unit_if_needed`` already
    compare against.
    """
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return None
    try:
        text = unit_path.read_text(encoding='utf-8')
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('Environment='):
            continue
        body = stripped[len('Environment='):].strip().strip('"')
        if body.startswith('DUCK_AGENT_HOME='):
            value = body.split('=', 1)[1].strip().strip('"')
            return value or None
    return None

def _sync_hermes_home_from_systemd_unit(system: bool) -> None:
    """When acting on a system-scope unit, adopt its ``DUCK_AGENT_HOME``.

    Under ``sudo``, ``DUCK_AGENT_HOME`` is stripped and ``HOME=/root``, so
    :func:`get_hermes_home` falls back to ``/root/.duck-agent`` — the wrong
    profile. The unit file pins ``DUCK_AGENT_HOME`` for the actual gateway
    process, so we mirror that into our own environment to make
    ``read_runtime_status`` / ``get_running_pid`` read the correct files.
    """
    if not system:
        return
    unit_home = (_hermes_home_from_systemd_unit_file(system=True) or '').strip()
    if not unit_home:
        unit_home = _read_systemd_unit_environment(system=True).get('DUCK_AGENT_HOME', '').strip()
    if not unit_home:
        return
    current = os.environ.get('DUCK_AGENT_HOME', '').strip()
    if current == unit_home:
        return
    os.environ['DUCK_AGENT_HOME'] = unit_home

def _read_systemd_unit_properties(system: bool=False, properties: tuple[str, ...]=('ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'MainPID')) -> dict[str, str]:
    """Return selected ``systemctl show`` properties for the gateway unit."""
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(['show', get_service_name(), '--no-pager', '--property', ','.join(properties)], system=selected_system, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        parsed[key] = value.strip()
    return parsed

def _systemd_main_pid_from_props(props: dict[str, str]) -> int | None:
    try:
        pid = int(props.get('MainPID', '0') or '0')
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None

def _systemd_main_pid(system: bool=False) -> int | None:
    return _systemd_main_pid_from_props(_read_systemd_unit_properties(system=system))

def _read_gateway_runtime_status() -> dict | None:
    try:
        from gateway.status import read_runtime_status
        state = read_runtime_status()
    except Exception:
        return None
    return state if isinstance(state, dict) else None

def _gateway_runtime_status_for_pid(pid: int | None) -> dict | None:
    if not pid:
        return None
    state = _read_gateway_runtime_status()
    if not state:
        return None
    try:
        state_pid = int(state.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return None
    return state if state_pid == pid else None

def _wait_for_systemd_service_restart(*, system: bool=False, previous_pid: int | None=None, timeout: float=60.0) -> bool:
    """Wait for the gateway service to become active after a restart handoff."""
    import time
    svc = get_service_name()
    scope_label = _service_scope_label(system).capitalize()
    deadline = time.monotonic() + timeout
    printed_runtime_wait = False
    while time.monotonic() < deadline:
        props = _read_systemd_unit_properties(system=system)
        active_state = props.get('ActiveState', '')
        sub_state = props.get('SubState', '')
        new_pid = None
        try:
            from gateway.status import get_running_pid
            new_pid = get_running_pid()
        except Exception:
            new_pid = None
        if not new_pid:
            new_pid = _systemd_main_pid_from_props(props)
        if active_state == 'active':
            if new_pid and (previous_pid is None or new_pid != previous_pid):
                runtime_state = _gateway_runtime_status_for_pid(new_pid)
                gateway_state = (runtime_state or {}).get('gateway_state')
                if gateway_state == 'running':
                    print(f'✓ {scope_label} service restarted (PID {new_pid})')
                    return True
                if gateway_state == 'startup_failed':
                    reason = (runtime_state or {}).get('exit_reason') or 'startup failed'
                    print(f'⚠ {scope_label} service process restarted (PID {new_pid}), but gateway startup failed: {reason}')
                    return False
                if not printed_runtime_wait:
                    print(f'⏳ {scope_label} service process started (PID {new_pid}); waiting for gateway runtime...')
                    printed_runtime_wait = True
        if active_state == 'activating' and sub_state == 'auto-restart':
            time.sleep(1)
            continue
        if _systemd_unit_is_start_limited(props):
            _print_systemd_start_limit_wait(system=system)
            return False
        time.sleep(2)
    print(f"⚠ {scope_label} service did not become active within {int(timeout)}s.\n  Check status: {('sudo ' if system else '')}duck-agent gateway status\n  Check logs:   journalctl {('--user ' if not system else '')}-u {svc} -l --since '2 min ago'")
    return False

def _systemd_unit_is_start_limited(props: dict[str, str]) -> bool:
    result = props.get('Result', '').lower()
    sub_state = props.get('SubState', '').lower()
    return result == 'start-limit-hit' or sub_state == 'start-limit-hit'

def _systemd_error_indicates_start_limit(exc: subprocess.CalledProcessError) -> bool:
    parts: list[str] = []
    for attr in ('stderr', 'stdout', 'output'):
        value = getattr(exc, attr, None)
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode(errors='replace')
        parts.append(str(value))
    text = '\n'.join(parts).lower()
    return 'start-limit-hit' in text or 'start request repeated too quickly' in text or 'start-limit' in text

def _systemd_service_is_start_limited(system: bool=False) -> bool:
    return _systemd_unit_is_start_limited(_read_systemd_unit_properties(system=system))

def _print_systemd_start_limit_wait(system: bool=False) -> None:
    svc = get_service_name()
    scope_label = _service_scope_label(system).capitalize()
    scope_flag = ' --system' if system else ''
    systemctl_prefix = 'systemctl ' if system else 'systemctl --user '
    journal_prefix = 'journalctl ' if system else 'journalctl --user '
    print(f'⏳ {scope_label} service is temporarily rate-limited by systemd.')
    print('  systemd is refusing another immediate start after repeated exits.')
    print(f"  Wait for the start-limit window to expire, then run: {('sudo ' if system else '')}duck-agent gateway restart{scope_flag}")
    print(f'  Or clear the failed state manually: {systemctl_prefix}reset-failed {svc}')
    print(f"  Check logs: {journal_prefix}-u {svc} -l --since '5 min ago'")

def _recover_pending_systemd_restart(system: bool=False, previous_pid: int | None=None) -> bool:
    """Recover a planned service restart that is stuck in systemd state."""
    props = _read_systemd_unit_properties(system=system)
    if not props:
        return False
    try:
        from gateway.status import read_runtime_status
    except Exception:
        return False
    runtime_state = read_runtime_status() or {}
    if not runtime_state.get('restart_requested'):
        return False
    active_state = props.get('ActiveState', '')
    sub_state = props.get('SubState', '')
    exec_main_status = props.get('ExecMainStatus', '')
    result = props.get('Result', '')
    if active_state == 'activating' and sub_state == 'auto-restart':
        print('⏳ Service restart already pending — waiting for systemd relaunch...')
        return _wait_for_systemd_service_restart(system=system, previous_pid=previous_pid)
    if active_state == 'failed' and (exec_main_status == str(GATEWAY_SERVICE_RESTART_EXIT_CODE) or result == 'exit-code'):
        svc = get_service_name()
        scope_label = _service_scope_label(system).capitalize()
        print(f'↻ Clearing failed state for pending {scope_label.lower()} service restart...')
        _run_systemctl(['reset-failed', svc], system=system, check=False, timeout=30)
        _run_systemctl(['start', svc], system=system, check=False, timeout=90)
        return _wait_for_systemd_service_restart(system=system, previous_pid=previous_pid)
    return False

def _parse_launchd_pid_from_list_output(output: str) -> int | None:
    """Extract the PID from ``launchctl list <label>`` output.

    When launchd is actively supervising a process, the output includes a
    ``"PID" = <number>;`` line.  When the service definition is only *registered*
    but not running (macOS 26+ with an unmanageable domain, fallback active),
    the output lacks a PID field entirely.  Returns ``None`` when no PID is
    found or the PID is non-positive (e.g. ``-1`` for a recently-crashed service).
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"') or stripped.startswith('PID'):
            parts = stripped.split('=', 1)
            if len(parts) == 2:
                val = parts[1].strip().rstrip(';').strip('"')
                try:
                    pid = int(val)
                    return pid if pid > 0 else None
                except ValueError:
                    return None
    return None

def _probe_launchd_service_running() -> bool:
    """Return True when launchd is actively supervising the gateway process.

    ``launchctl list <label>`` returns exit 0 whenever the service definition is
    registered with launchd — even when ``state = not running`` (macOS 26+).
    We additionally require a PID in the output to confirm launchd is actually
    managing a live process, not just holding a static definition.
    """
    if not get_launchd_plist_path().exists():
        return False
    try:
        result = subprocess.run(['launchctl', 'list', get_launchd_label()], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    return _parse_launchd_pid_from_list_output(result.stdout) is not None

def get_gateway_runtime_snapshot(system: bool=False) -> GatewayRuntimeSnapshot:
    """Return a unified view of gateway liveness for the current profile."""
    gateway_pids = tuple(find_gateway_pids())
    if is_termux():
        return GatewayRuntimeSnapshot(manager='Termux / manual process', gateway_pids=gateway_pids)
    from hermes_constants import is_container
    if is_linux() and is_container():
        try:
            from hermes_cli.service_manager import detect_service_manager, get_service_manager
            if detect_service_manager() == 's6':
                profile = _profile_suffix() or 'default'
                service_name = f'gateway-{profile}'
                mgr = get_service_manager()
                service_installed = False
                service_running = False
                try:
                    service_dir = getattr(mgr, 'scandir', None)
                    if service_dir is not None:
                        service_installed = (service_dir / service_name).is_dir()
                except Exception:
                    service_installed = False
                if service_installed:
                    try:
                        service_running = bool(mgr.is_running(service_name))
                    except Exception:
                        service_running = False
                return GatewayRuntimeSnapshot(manager='s6 (container supervisor)', service_installed=service_installed, service_running=service_running, gateway_pids=gateway_pids, service_scope='s6')
        except Exception:
            pass
        return GatewayRuntimeSnapshot(manager='docker (foreground)', gateway_pids=gateway_pids)
    if supports_systemd_services():
        selected_system, service_running = _probe_systemd_service_running(system=system)
        scope_label = _service_scope_label(selected_system)
        return GatewayRuntimeSnapshot(manager=f'systemd ({scope_label})', service_installed=get_systemd_unit_path(system=selected_system).exists(), service_running=service_running, gateway_pids=gateway_pids, service_scope=scope_label)
    if is_macos():
        return GatewayRuntimeSnapshot(manager='launchd', service_installed=get_launchd_plist_path().exists(), service_running=_probe_launchd_service_running(), gateway_pids=gateway_pids, service_scope='launchd')
    return GatewayRuntimeSnapshot(manager='manual process', gateway_pids=gateway_pids)

def _format_gateway_pids(pids: tuple[int, ...] | list[int], *, limit: int | None=3) -> str:
    rendered = [str(pid) for pid in pids[:limit] if pid > 0] if limit is not None else [str(pid) for pid in pids if pid > 0]
    if limit is not None and len(pids) > limit:
        rendered.append('...')
    return ', '.join(rendered)

def _print_gateway_process_mismatch(snapshot: GatewayRuntimeSnapshot) -> None:
    if not snapshot.has_process_service_mismatch:
        return
    print()
    if _launchd_unsupported_marker_exists():
        print('⚠ Gateway is running as a detached fallback process — launchd cannot supervise it')
        print(f'  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}')
        print('  Auto-start at login and auto-restart on crash are NOT available.')
        print('  Stop it with: duck-agent gateway stop')
    else:
        print('⚠ Gateway process is running for this profile, but the service is not active')
        print(f'  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}')
        print('  This is usually a manual foreground/tmux/nohup run, so `duck-agent gateway`')
        print('  can refuse to start another copy until this process stops.')

def _print_other_profiles_gateway_status() -> None:
    """Print a summary of gateway status across all profiles.

    Shown at the bottom of ``duck-agent gateway status`` output so users with
    multiple profiles can tell at a glance which gateways are running and
    avoid confusing another profile's process with the current one.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name
        current = get_active_profile_name()
        other_processes = [p for p in find_profile_gateway_processes() if p.profile != current]
        if not other_processes:
            return
        print()
        print('Other profiles:')
        for proc in other_processes:
            print(f'  ✓ {proc.profile:<16s} — PID {proc.pid}')
    except Exception:
        pass

def _gateway_list() -> None:
    """List all profiles and their gateway running status.

    Provides a single-command overview of every known profile and whether
    its gateway is currently running, so multi-profile users don't have to
    check each profile individually.
    """
    try:
        from hermes_cli.profiles import list_profiles, get_active_profile_name
    except Exception:
        print('Unable to list profiles.')
        return
    profiles = list_profiles()
    if not profiles:
        print('No profiles found.')
        return
    current = get_active_profile_name()
    print('Gateways:')
    for prof in profiles:
        marker = '✓' if prof.gateway_running else '✗'
        label = prof.name
        if prof.name == current:
            label += ' (current)'
        parts = [f'  {marker} {label:<24s}']
        if prof.gateway_running:
            try:
                from gateway.status import get_running_pid
                pid = get_running_pid(prof.path / 'gateway.pid', cleanup_stale=False)
                if pid:
                    parts.append(f'PID {pid}')
            except Exception:
                pass
        else:
            parts.append('not running')
        print(' — '.join(parts))

def kill_gateway_processes(force: bool=False, exclude_pids: set | None=None, all_profiles: bool=False) -> int:
    """Kill any running gateway processes. Returns count killed.

    Args:
        force: Use the platform's force-kill mechanism instead of graceful terminate.
        exclude_pids: PIDs to skip (e.g. service-managed PIDs that were just
            restarted and should not be killed).
        all_profiles: When ``True``, kill across all profiles.  Passed
            through to :func:`find_gateway_pids`.
    """
    pids = find_gateway_pids(exclude_pids=exclude_pids, all_profiles=all_profiles)
    killed = 0
    for pid in pids:
        try:
            terminate_pid(pid, force=force)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f'⚠ Permission denied to kill PID {pid}')
        except OSError as exc:
            print(f'Failed to kill PID {pid}: {exc}')
    return killed

def _reap_unsupervised_gateway_orphans(extra_exclude: set | None=None) -> bool:
    """Kill no-supervisor gateway orphans the pidfile/runtime record can't see.

    On WSL/no-systemd hosts the manual restart fallback runs the gateway
    in-process under a ``gateway restart`` argv (hermes_cli/gateway.py restart
    branch → ``run_gateway()``). If its pidfile or runtime record goes missing
    or stale, ``get_running_pid()`` returns ``None`` even though a live orphan
    still holds the webhook port, so a follow-up restart stacks a duplicate on
    the same port (#51325). This is a no-op on hosts WITH a service supervisor,
    where a ``gateway restart`` argv is a transient management command, not the
    running gateway — gating on ``supports_systemd_services()`` keeps the
    orphan-aware scan from killing live management processes there.

    Args:
        extra_exclude: Additional PIDs to skip (e.g. a PID already killed by
            the caller so the sweep doesn't send a redundant SIGTERM/SIGKILL).

    Returns True if at least one orphan was reaped.
    """
    try:
        if supports_systemd_services():
            return False
    except Exception:
        return False
    from gateway.status import _pid_exists, write_planned_stop_marker
    own = {os.getpid()}
    if extra_exclude:
        own |= extra_exclude
    try:
        orphans = [p for p in find_gateway_pids(exclude_pids=own) if p and p > 0]
    except Exception:
        return False
    if not orphans:
        return False
    reaped = False
    for pid in orphans:
        try:
            write_planned_stop_marker(pid)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f'⚠ Permission denied to kill orphaned gateway PID {pid}')
            continue
        reaped = True
    deadline = time.monotonic() + 5.0
    survivors = list(orphans)
    while survivors and time.monotonic() < deadline:
        survivors = [p for p in survivors if _pid_exists(p)]
        if survivors:
            time.sleep(0.2)
    for pid in survivors:
        try:
            os.kill(pid, getattr(signal, 'SIGKILL', signal.SIGTERM))
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return reaped

def stop_profile_gateway() -> bool:
    """Stop only the gateway for the current profile (DUCK_AGENT_HOME-scoped).

    Uses the PID file written by start_gateway(), so it only kills the
    gateway belonging to this profile — not gateways from other profiles.
    Returns True if a process was stopped, False if none was found.

    On hosts without a service supervisor (e.g. WSL/no-systemd, where the
    manual restart fallback runs the gateway in-process under a ``gateway
    restart`` argv), the pidfile/runtime record can be missing or stale while
    a live orphan still holds the webhook port. In that case fall back to the
    orphan-aware process scan so the replacement reaps the prior instance
    instead of stacking a duplicate on the same port (#51325).

    Even when the pid file is valid and points to the current gateway, older
    orphans may linger from prior restarts that overwrote the pid file before
    the old process exited. After killing the recorded PID, also sweep for
    any remaining orphans so each restart produces at most one live gateway
    (#75936).
    """
    try:
        from gateway.status import get_running_pid, remove_pid_file
    except ImportError:
        return False
    pid = get_running_pid()
    if pid is None:
        return _reap_unsupervised_gateway_orphans()
    try:
        from gateway.status import write_planned_stop_marker
        write_planned_stop_marker(pid)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        print(f'⚠ Permission denied to kill PID {pid}')
        return False
    import time as _time
    from gateway.status import _pid_exists
    for _ in range(20):
        if not _pid_exists(pid):
            break
        _time.sleep(0.5)
    if get_running_pid() is None:
        remove_pid_file()
    try:
        _reap_unsupervised_gateway_orphans(extra_exclude={pid} if pid else None)
    except Exception as exc:
        logger.debug('orphan reap after stop_profile_gateway failed: %s', exc)
    return True

def is_linux() -> bool:
    return sys.platform.startswith('linux')
from hermes_constants import is_container, is_termux, is_wsl

def _wsl_systemd_operational() -> bool:
    """Check if systemd is actually running as PID 1 on WSL.

    WSL2 with ``systemd=true`` in wsl.conf has working systemd.
    WSL2 without it (or WSL1) does not — systemctl commands fail.
    """
    return _systemd_operational(system=True)

def _systemd_operational(system: bool=False) -> bool:
    """Return True when the requested systemd scope is usable."""
    try:
        result = _run_systemctl(['is-system-running'], system=system, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        status = result.stdout.strip().lower()
        return status in {'running', 'degraded', 'starting', 'initializing'}
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return False

def _container_systemd_operational() -> bool:
    """Return True when a container exposes working user or system systemd.

    This is NOT our Duck Agent Docker image — that one runs s6-overlay as
    PID 1 (since Phase 2 of the s6-overlay supervision plan) and is
    detected via ``service_manager.detect_service_manager() == "s6"``.
    This function handles the "container managed by something else"
    case: systemd-nspawn, certain k8s pods, containers built FROM
    systemd-bearing distros where the user has wired systemd as their
    init. In those environments systemctl behaves identically to the
    host case, so we fall through to the normal systemd code paths.
    """
    if _systemd_operational(system=False):
        return True
    if _systemd_operational(system=True):
        return True
    return False

def supports_systemd_services() -> bool:
    if not is_linux() or is_termux():
        return False
    if shutil.which('systemctl') is None:
        return False
    if is_wsl():
        return _wsl_systemd_operational()
    if is_container():
        return _container_systemd_operational()
    return True

def is_macos() -> bool:
    return sys.platform == 'darwin'

def is_windows() -> bool:
    return sys.platform == 'win32'

def _windows_gateway_should_absorb_console_controls() -> bool:
    """Return True for detached Windows gateway runs that should ignore Ctrl+C.

    Foreground ``duck-agent gateway run`` must remain interruptible from
    PowerShell/CMD. Detached service-style launches opt in via
    ``HERMES_GATEWAY_DETACHED=1``; older wrappers without the env marker are
    treated as detached when no interactive stdin is attached.
    """
    if not is_windows():
        return False
    detached = os.getenv('HERMES_GATEWAY_DETACHED', '').strip().lower()
    if detached in {'1', 'true', 'yes', 'on'}:
        return True
    try:
        return not bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return True
_SERVICE_BASE = 'duck-agent-gateway'
SERVICE_DESCRIPTION = 'Duck Agent Gateway - Messaging Platform Integration'

def _profile_suffix() -> str:
    """Derive a service-name suffix from the current DUCK_AGENT_HOME.

    Returns ``""`` for the default root, the profile name for
    ``<root>/profiles/<name>``, or a short hash for any other path.
    Works correctly in Docker (DUCK_AGENT_HOME=/opt/data) and standard deployments.
    """
    import hashlib
    import re
    from hermes_constants import get_default_hermes_root
    home = get_hermes_home().resolve()
    default = get_default_hermes_root().resolve()
    if home == default:
        return ''
    profiles_root = (default / 'profiles').resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match('^[a-z0-9][a-z0-9_-]{0,63}$', parts[0]):
            return parts[0]
    except ValueError:
        pass
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]

def _profile_arg(hermes_home: str | None=None, default_root: str | Path | None=None) -> str:
    """Return ``--profile <name>`` only when DUCK_AGENT_HOME is a named profile.

    For ``~/.duck-agent/profiles/<name>``, returns ``"--profile <name>"``.
    For the default profile or hash-based custom paths, returns the empty string.

    Args:
        duck_agent_home: Optional explicit DUCK_AGENT_HOME path. Defaults to the current
            ``get_hermes_home()`` value. Should be passed when generating a
            service definition for a different user (e.g. system service).
        default_root: Optional Duck Agent root to compare against. Used when
            generating a system service for another user from a sudo/root
            process, where ``Path.home()`` and ``get_default_hermes_root()``
            refer to root but the target profile lives under the service user.
    """
    import re
    from hermes_constants import get_default_hermes_root
    home = Path(hermes_home or str(get_hermes_home())).resolve()
    default = Path(default_root).resolve() if default_root else get_default_hermes_root().resolve()
    if home == default:
        return ''
    profiles_root = (default / 'profiles').resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match('^[a-z0-9][a-z0-9_-]{0,63}$', parts[0]):
            return f'--profile {parts[0]}'
    except ValueError:
        pass
    return ''

def _profile_arg_for_target_user(hermes_home: str, target_home_dir: str) -> str:
    """Return the profile arg for a system service running as another user."""
    target_root = Path(target_home_dir) / '.duck-agent'
    try:
        Path(hermes_home).resolve().relative_to(target_root.resolve())
        return _profile_arg(hermes_home, default_root=target_root)
    except ValueError:
        return _profile_arg(hermes_home)

def get_service_name() -> str:
    """Derive a systemd service name scoped to this DUCK_AGENT_HOME.

    Default ``~/.duck-agent`` returns ``duck-agent-gateway`` (backward compatible).
    Profile ``~/.duck-agent/profiles/coder`` returns ``duck-agent-gateway-coder``.
    Any other DUCK_AGENT_HOME appends a short hash for uniqueness.
    """
    suffix = _profile_suffix()
    if not suffix:
        return _SERVICE_BASE
    return f'{_SERVICE_BASE}-{suffix}'

def get_systemd_unit_path(system: bool=False) -> Path:
    name = get_service_name()
    if system:
        return Path('/etc/systemd/system') / f'{name}.service'
    return Path.home() / '.config' / 'systemd' / 'user' / f'{name}.service'

class UserSystemdUnavailableError(RuntimeError):
    """Raised when ``systemctl --user`` cannot reach the user D-Bus session.

    Typically hit on fresh RHEL/Debian SSH sessions where linger is disabled
    and no user@.service is running, so ``/run/user/$UID/bus`` never exists.
    Carries a user-facing remediation message in ``args[0]``.
    """

class SystemScopeRequiresRootError(RuntimeError):
    """Raised when a system-scope gateway operation is attempted as non-root.

    System-scope units live in ``/etc/systemd/system/`` and require root for
    install / uninstall / start / stop / restart via ``systemctl``. The
    previous behavior was ``sys.exit(1)`` which blew past the wizard's
    ``except Exception`` guards and dumped the user at a bare shell prompt
    with no guidance. Raising a typed exception lets callers that can
    recover (the setup wizard) print actionable remediation instead, while
    ``gateway_command`` still exits 1 with the same message for the direct
    CLI path.

    ``args[0]`` carries the user-facing message, ``args[1]`` the action name.
    ``str(e)`` returns only the message (not the tuple repr) so format
    strings like ``f"Failed: {e}"`` render cleanly.
    """

    def __str__(self) -> str:
        return self.args[0] if self.args else ''

def _user_dbus_socket_path() -> Path:
    """Return the expected per-user D-Bus socket path (regardless of existence)."""
    xdg = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return Path(xdg) / 'bus'

def _user_systemd_private_socket_path() -> Path:
    """Return the per-user systemd private socket path (regardless of existence)."""
    xdg = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return Path(xdg) / 'systemd' / 'private'

def _user_systemd_socket_ready() -> bool:
    """Return True when user-scope systemd has a reachable control socket.

    Some distros expose only the per-user systemd private socket even when the
    D-Bus session bus socket is absent. ``systemctl --user`` can still work in
    that configuration, so preflight checks must treat either socket as valid.
    """
    return _user_dbus_socket_path().exists() or _user_systemd_private_socket_path().exists()

def _ensure_user_systemd_env() -> None:
    """Ensure DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR are set for systemctl --user.

    On headless servers (SSH sessions), these env vars may be missing even when
    the user's systemd instance is running (via linger).  Without them,
    ``systemctl --user`` fails with "Failed to connect to bus: No medium found".
    We detect the standard socket path and set the vars so all subsequent
    subprocess calls inherit them.
    """
    uid = os.getuid()
    if 'XDG_RUNTIME_DIR' not in os.environ:
        runtime_dir = f'/run/user/{uid}'
        if Path(runtime_dir).exists():
            os.environ['XDG_RUNTIME_DIR'] = runtime_dir
    if 'DBUS_SESSION_BUS_ADDRESS' not in os.environ:
        xdg_runtime = os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{uid}')
        bus_path = Path(xdg_runtime) / 'bus'
        if bus_path.exists():
            os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={bus_path}'

def _wait_for_user_dbus_socket(timeout: float=3.0) -> bool:
    """Poll for the user systemd runtime socket(s), up to ``timeout`` seconds.

    Linger-enabled user@.service can take a second or two to spawn its control
    socket(s) after ``loginctl enable-linger`` runs. Returns True once either
    the user D-Bus socket or the per-user systemd private socket exists.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _user_systemd_socket_ready():
            _ensure_user_systemd_env()
            return True
        time.sleep(0.2)
    return _user_systemd_socket_ready()

def _preflight_user_systemd(*, auto_enable_linger: bool=True) -> None:
    """Ensure ``systemctl --user`` will reach the user-scope systemd instance.

    No-op when the user D-Bus socket or per-user systemd private socket is
    already there (the common case on desktops and linger-enabled servers). On
    fresh SSH sessions where both are missing:

    * If linger is already enabled, wait briefly for user@.service to spawn
      the socket.
    * If linger is disabled and ``auto_enable_linger`` is True, try
      ``loginctl enable-linger $USER`` (works as non-root when polkit permits
      it, otherwise needs sudo).
    * If the socket is still missing afterwards, raise
      :class:`UserSystemdUnavailableError` with a precise remediation message.

    Callers should treat the exception as a terminal condition for user-scope
    systemd operations and surface the message to the user.
    """
    _ensure_user_systemd_env()
    if _user_systemd_socket_ready():
        return
    import getpass
    username = getpass.getuser()
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        if _wait_for_user_dbus_socket(timeout=3.0):
            return
        _raise_user_systemd_unavailable(username, reason='User systemd control sockets are missing even though linger is enabled.', fix_hint=f'  systemctl start user@{os.getuid()}.service\n  (may require sudo; try again after the command succeeds)')
    if auto_enable_linger and shutil.which('loginctl'):
        try:
            result = subprocess.run(['loginctl', 'enable-linger', username], capture_output=True, text=True, encoding='utf-8', errors='replace', check=False, timeout=30)
        except Exception as exc:
            _raise_user_systemd_unavailable(username, reason=f'loginctl enable-linger failed ({exc}).', fix_hint=f'  sudo loginctl enable-linger {username}')
        else:
            if result.returncode == 0:
                if _wait_for_user_dbus_socket(timeout=5.0):
                    print(f'✓ Enabled linger for {username} — user D-Bus now available')
                    return
                _raise_user_systemd_unavailable(username, reason='Linger was enabled, but the user D-Bus socket did not appear.', fix_hint=f'  Log out and log back in, then re-run the command.\n  Or reboot and run: systemctl --user start {get_service_name()}')
            detail = (result.stderr or result.stdout or f'exit {result.returncode}').strip()
            _raise_user_systemd_unavailable(username, reason=f'loginctl enable-linger was denied: {detail}', fix_hint=f'  sudo loginctl enable-linger {username}')
    _raise_user_systemd_unavailable(username, reason=f"User D-Bus session is not available ({linger_detail or 'linger disabled'}).", fix_hint=f'  sudo loginctl enable-linger {username}')

def _raise_user_systemd_unavailable(username: str, *, reason: str, fix_hint: str) -> None:
    """Build a user-facing error message and raise UserSystemdUnavailableError."""
    msg = f'{reason}\n  systemctl --user cannot reach the user D-Bus session in this shell.\n\n  To fix:\n{fix_hint}\n\n  Alternative: run the gateway in the foreground (stays up until\n  you exit / close the terminal):\n    duck-agent gateway run'
    raise UserSystemdUnavailableError(msg)

def _systemctl_cmd(system: bool=False) -> list[str]:
    if not system:
        _ensure_user_systemd_env()
    return ['systemctl'] if system else ['systemctl', '--user']

def _journalctl_cmd(system: bool=False) -> list[str]:
    return ['journalctl'] if system else ['journalctl', '--user']

def _run_systemctl(args: list[str], *, system: bool=False, **kwargs) -> subprocess.CompletedProcess:
    """Run a systemctl command, raising RuntimeError if systemctl is missing.

    Defense-in-depth: callers are gated by ``supports_systemd_services()``,
    but this ensures any future caller that bypasses the gate still gets a
    clear error instead of a raw ``FileNotFoundError`` traceback.
    """
    try:
        return subprocess.run(_systemctl_cmd(system) + args, **kwargs)
    except FileNotFoundError:
        raise RuntimeError('systemctl is not available on this system') from None

def _service_scope_label(system: bool=False) -> str:
    return 'system' if system else 'user'

def get_installed_systemd_scopes() -> list[str]:
    scopes = []
    seen_paths: set[Path] = set()
    for system, label in ((False, 'user'), (True, 'system')):
        unit_path = get_systemd_unit_path(system=system)
        if unit_path in seen_paths:
            continue
        if unit_path.exists():
            scopes.append(label)
            seen_paths.add(unit_path)
    return scopes

def has_conflicting_systemd_units() -> bool:
    return len(get_installed_systemd_scopes()) > 1
_LEGACY_SERVICE_NAMES: tuple[str, ...] = ('duck-agent.service',)
_LEGACY_UNIT_EXECSTART_MARKERS: tuple[str, ...] = ('hermes_cli.main gateway', 'hermes_cli/main.py gateway', 'gateway/run.py', ' duck-agent gateway ', '/duck-agent gateway ')

def _legacy_unit_search_paths() -> list[tuple[bool, Path]]:
    """Return ``[(is_system, base_dir), ...]`` — directories to scan for legacy units.

    Factored out so tests can monkeypatch the search roots without touching
    real filesystem paths.
    """
    return [(False, Path.home() / '.config' / 'systemd' / 'user'), (True, Path('/etc/systemd/system'))]

def _find_legacy_hermes_units() -> list[tuple[str, Path, bool]]:
    """Return ``[(unit_name, unit_path, is_system)]`` for legacy Duck Agent gateway units.

    Detects unit files installed by older Duck Agent versions that used a
    different service name (e.g. ``duck-agent.service`` before the rename to
    ``duck-agent-gateway.service``). When both a legacy unit and the current
    ``duck-agent-gateway.service`` are active, they fight over the same bot
    token — the PR #5646 signal-recovery change turns this into a 30-second
    SIGTERM flap loop.

    Safety guards:

    * Explicit allowlist of legacy names (no globbing). Profile units such
      as ``duck-agent-gateway-coder.service`` and unrelated third-party
      ``duck-agent-*`` services are never matched.
    * ExecStart content check — only flag units that invoke our gateway
      entrypoint. A user-created ``duck-agent.service`` running an unrelated
      binary is left untouched.
    * Results are returned purely for caller inspection; this function
      never mutates or removes anything.
    """
    results: list[tuple[str, Path, bool]] = []
    for is_system, base in _legacy_unit_search_paths():
        for name in _LEGACY_SERVICE_NAMES:
            unit_path = base / name
            try:
                if not unit_path.exists():
                    continue
                text = unit_path.read_text(encoding='utf-8', errors='ignore')
            except (OSError, PermissionError):
                continue
            if not any((marker in text for marker in _LEGACY_UNIT_EXECSTART_MARKERS)):
                continue
            results.append((name, unit_path, is_system))
    return results

def has_legacy_hermes_units() -> bool:
    """Return True when any legacy Duck Agent gateway unit files exist."""
    return bool(_find_legacy_hermes_units())

def print_legacy_unit_warning() -> None:
    """Warn about legacy Duck Agent gateway unit files if any are installed.

    Idempotent: prints nothing when no legacy units are detected. Safe to
    call from any status/install/setup path.
    """
    legacy = _find_legacy_hermes_units()
    if not legacy:
        return
    print_warning('Legacy Duck Agent gateway unit(s) detected from an older install:')
    for name, path, is_system in legacy:
        scope = 'system' if is_system else 'user'
        print_info(f'    {path}  ({scope} scope)')
    print_info('  These run alongside the current duck-agent-gateway service and')
    print_info('  cause SIGTERM flap loops — both try to use the same bot token.')
    print_info('  Remove them with:')
    print_info('    duck-agent gateway migrate-legacy')

def remove_legacy_hermes_units(interactive: bool=True, dry_run: bool=False) -> tuple[int, list[Path]]:
    """Stop, disable, and remove legacy Duck Agent gateway unit files.

    Iterates over whatever ``_find_legacy_hermes_units()`` returns — which is
    an explicit allowlist of legacy names (not a glob). Profile units and
    unrelated third-party services are never touched.

    Args:
        interactive: When True, prompt before removing. When False, remove
            without asking (used when another prompt has already confirmed,
            e.g. from the install flow).
        dry_run: When True, list what would be removed and return.

    Returns:
        ``(removed_count, remaining_paths)`` — remaining includes units we
        couldn't remove (typically system-scope when not running as root).
    """
    legacy = _find_legacy_hermes_units()
    if not legacy:
        print('No legacy Duck Agent gateway units found.')
        return (0, [])
    user_units = [(n, p) for n, p, is_sys in legacy if not is_sys]
    system_units = [(n, p) for n, p, is_sys in legacy if is_sys]
    print()
    print('Legacy Duck Agent gateway unit(s) found:')
    for name, path, is_system in legacy:
        scope = 'system' if is_system else 'user'
        print(f'  {path}  ({scope} scope)')
    print()
    if dry_run:
        print('(dry-run — nothing removed)')
        return (0, [p for _, p, _ in legacy])
    if interactive and (not prompt_yes_no('Remove these legacy units?', True)):
        print('Skipped. Run again with: duck-agent gateway migrate-legacy')
        return (0, [p for _, p, _ in legacy])
    removed = 0
    remaining: list[Path] = []
    for name, path in user_units:
        try:
            _run_systemctl(['stop', name], system=False, check=False, timeout=90)
            _run_systemctl(['disable', name], system=False, check=False, timeout=30)
            path.unlink(missing_ok=True)
            print(f'  ✓ Removed {path}')
            removed += 1
        except (OSError, RuntimeError) as e:
            print(f'  ⚠ Could not remove {path}: {e}')
            remaining.append(path)
    if user_units:
        try:
            _run_systemctl(['daemon-reload'], system=False, check=False, timeout=30)
        except RuntimeError:
            pass
    if system_units:
        if os.geteuid() != 0:
            print()
            print_warning('System-scope legacy units require root to remove.')
            print_info('  Re-run with: sudo duck-agent gateway migrate-legacy')
            for _, path in system_units:
                remaining.append(path)
        else:
            for name, path in system_units:
                try:
                    _run_systemctl(['stop', name], system=True, check=False, timeout=90)
                    _run_systemctl(['disable', name], system=True, check=False, timeout=30)
                    path.unlink(missing_ok=True)
                    print(f'  ✓ Removed {path}')
                    removed += 1
                except (OSError, RuntimeError) as e:
                    print(f'  ⚠ Could not remove {path}: {e}')
                    remaining.append(path)
            try:
                _run_systemctl(['daemon-reload'], system=True, check=False, timeout=30)
            except RuntimeError:
                pass
    print()
    if remaining:
        print_warning(f'{len(remaining)} legacy unit(s) still present — see messages above.')
    else:
        print_success(f'Removed {removed} legacy unit(s).')
    return (removed, remaining)

def print_systemd_scope_conflict_warning() -> None:
    scopes = get_installed_systemd_scopes()
    if len(scopes) < 2:
        return
    rendered_scopes = ' + '.join(scopes)
    print_warning(f'Both user and system gateway services are installed ({rendered_scopes}).')
    print_info('  This is confusing and can make start/stop/status behavior ambiguous.')
    print_info('  Default gateway commands target the user service unless you pass --system.')
    print_info('  Keep one of these:')
    print_info('    duck-agent gateway uninstall')
    print_info('    sudo duck-agent gateway uninstall --system')

def _require_root_for_system_service(action: str) -> None:
    if os.geteuid() != 0:
        raise SystemScopeRequiresRootError(f'System gateway {action} requires root. Re-run with sudo.', action)

def _system_service_identity(run_as_user: str | None=None) -> tuple[str, str, str]:
    import getpass
    import grp
    import pwd
    username = (run_as_user or os.getenv('SUDO_USER') or os.getenv('USER') or os.getenv('LOGNAME') or getpass.getuser()).strip()
    if not username:
        raise ValueError('Could not determine which user the gateway service should run as')
    if username == 'root' and (not run_as_user):
        raise ValueError('Refusing to install the gateway system service as root; pass --run-as-user root to override (e.g. in LXC containers)')
    if username == 'root':
        print_warning('Installing gateway service to run as root.')
        print_info('  This is fine for LXC/container environments but not recommended on bare-metal hosts.')
    try:
        user_info = pwd.getpwnam(username)
    except KeyError as e:
        raise ValueError(f'Unknown user: {username}') from e
    group_name = grp.getgrgid(user_info.pw_gid).gr_name
    return (username, group_name, user_info.pw_dir)

def _read_systemd_user_from_unit(unit_path: Path) -> str | None:
    if not unit_path.exists():
        return None
    for line in unit_path.read_text(encoding='utf-8').splitlines():
        if line.startswith('User='):
            value = line.split('=', 1)[1].strip()
            return value or None
    return None

def _default_system_service_user() -> str | None:
    for candidate in (os.getenv('SUDO_USER'), os.getenv('USER'), os.getenv('LOGNAME')):
        if candidate and candidate.strip() and (candidate.strip() != 'root'):
            return candidate.strip()
    return None

def prompt_linux_gateway_install_scope() -> str | None:
    is_root = os.geteuid() == 0
    if not is_root:
        choice = prompt_choice('  Choose how the gateway should run in the background:', ['User service (no sudo; best for laptops/dev boxes; may need linger after logout)', 'Skip service install for now'], default=0)
        if choice == 0:
            print_info('  Tip: for a boot-time system service, re-run setup as root (e.g. from a root shell or `sudo -i`).')
        return {0: 'user', 1: None}[choice]
    choice = prompt_choice('  Choose how the gateway should run in the background:', ['User service (no sudo; best for laptops/dev boxes; may need linger after logout)', 'System service (starts on boot; runs as your chosen user)', 'Skip service install for now'], default=0)
    return {0: 'user', 1: 'system', 2: None}[choice]

def install_linux_gateway_from_setup(force: bool=False, enable_on_startup: bool=True) -> tuple[str | None, bool]:
    scope = prompt_linux_gateway_install_scope()
    if scope is None:
        return (None, False)
    if scope == 'system':
        run_as_user = _default_system_service_user()
        if os.geteuid() != 0:
            print_warning('  System service install requires root. Re-run setup from a root shell, or install a user service instead: duck-agent gateway install')
            return (scope, False)
        if not run_as_user:
            while True:
                run_as_user = prompt('  Run the system gateway service as which user?', default='')
                run_as_user = (run_as_user or '').strip()
                if run_as_user:
                    break
                print_error('  Enter a username.')
        systemd_install(force=force, system=True, run_as_user=run_as_user, enable_on_startup=enable_on_startup)
        return (scope, True)
    systemd_install(force=force, system=False, enable_on_startup=enable_on_startup)
    return (scope, True)

def get_systemd_linger_status() -> tuple[bool | None, str]:
    """Return systemd linger status for the current user.

    Returns:
        (True, "") when linger is enabled.
        (False, "") when linger is disabled.
        (None, detail) when the status could not be determined.
    """
    if is_termux():
        return (None, 'not supported in Termux')
    if not is_linux():
        return (None, 'not supported on this platform')
    if not shutil.which('loginctl'):
        return (None, 'loginctl not found')
    username = os.getenv('USER') or os.getenv('LOGNAME')
    if not username:
        try:
            import pwd
            username = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            return (None, 'could not determine current user')
    try:
        result = subprocess.run(['loginctl', 'show-user', username, '--property=Linger', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', check=False, timeout=10)
    except Exception as e:
        return (None, str(e))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f'exit {result.returncode}').strip()
        return (None, detail or 'loginctl query failed')
    value = (result.stdout or '').strip().lower()
    if value in {'yes', 'true', '1'}:
        return (True, '')
    if value in {'no', 'false', '0'}:
        return (False, '')
    rendered = value or '<empty>'
    return (None, f'unexpected loginctl output: {rendered}')

def print_systemd_linger_guidance() -> None:
    """Print the current linger status and the fix when it is disabled."""
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print('✓ Systemd linger is enabled (service survives logout)')
    elif linger_enabled is False:
        print('⚠ Systemd linger is disabled (gateway may stop when you log out)')
        print('  Run: sudo loginctl enable-linger $USER')
    else:
        print(f'⚠ Could not verify systemd linger ({linger_detail})')
        print('  If you want the gateway user service to survive logout, run:')
        print('  sudo loginctl enable-linger $USER')

def _launchd_user_home() -> Path:
    """Return the real macOS user home for launchd artifacts.

    Profile-mode Duck Agent often sets ``HOME`` to a profile-scoped directory, but
    launchd user agents still live under the actual account home.
    """
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)

def get_launchd_plist_path() -> Path:
    """Return the launchd plist path, scoped per profile.

    Default ``~/.duck-agent`` → ``ai.duck-agent.gateway.plist`` (backward compatible).
    Profile ``~/.duck-agent/profiles/coder`` → ``ai.duck-agent.gateway-coder.plist``.
    """
    suffix = _profile_suffix()
    name = f'ai.duck-agent.gateway-{suffix}' if suffix else 'ai.duck-agent.gateway'
    return _launchd_user_home() / 'Library' / 'LaunchAgents' / f'{name}.plist'

def _detect_venv_dir() -> Path | None:
    """Detect the active virtualenv directory.

    Checks ``sys.prefix`` first (works regardless of the directory name),
    then ``VIRTUAL_ENV`` env var (covers uv-managed environments where
    sys.prefix == sys.base_prefix), then falls back to probing common
    directory names under PROJECT_ROOT.
    Returns ``None`` when no virtualenv can be found.
    """
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv
    _virtual_env = os.environ.get('VIRTUAL_ENV')
    if _virtual_env:
        venv = Path(_virtual_env)
        if venv.is_dir():
            return venv
    for candidate in ('.venv', 'venv'):
        venv = PROJECT_ROOT / candidate
        if venv.is_dir():
            return venv
    return None

def get_python_path() -> str:
    venv = _detect_venv_dir()
    if venv is not None:
        try:
            from hermes_constants import venv_python_path
        except ImportError:
            from hermes_cli.managed_uv import _reload_hermes_constants
            venv_python_path = _reload_hermes_constants().venv_python_path
        venv_python = venv_python_path(venv, windows=is_windows())
        if venv_python.exists():
            return str(venv_python)
    return sys.executable

def _build_user_local_paths(home: Path, path_entries: list[str]) -> list[str]:
    """Return user-local bin dirs that exist and aren't already in *path_entries*."""
    candidates = [str(home / '.local' / 'bin'), str(home / '.cargo' / 'bin'), str(home / 'go' / 'bin'), str(home / '.npm-global' / 'bin')]
    return [p for p in candidates if p not in path_entries and Path(p).exists()]

def _build_wsl_interop_paths(path_entries: list[str]) -> list[str]:
    """Return WSL Windows interop PATH entries for generated systemd units.

    WSL shells normally inherit Windows PATH entries such as
    ``/mnt/c/WINDOWS/System32``. systemd user services do not, so gateway tools
    that call ``powershell.exe``/``cmd.exe`` work in a terminal but fail in the
    background service unless we persist the relevant entries at install time.
    """
    if not is_wsl():
        return []
    candidates: list[str] = []
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry.startswith('/mnt/'):
            candidates.append(entry)
    for executable in ('powershell.exe', 'cmd.exe', 'explorer.exe', 'wsl.exe'):
        resolved = shutil.which(executable)
        if resolved:
            candidates.append(str(Path(resolved).parent))
    for entry in ('/mnt/c/WINDOWS/system32', '/mnt/c/WINDOWS', '/mnt/c/WINDOWS/System32/Wbem', '/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/', '/mnt/c/WINDOWS/System32/OpenSSH/'):
        if Path(entry).exists():
            candidates.append(entry)
    result: list[str] = []
    seen = set(path_entries)
    for entry in candidates:
        if entry and entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result

def _remap_path_for_user(path: str, target_home_dir: str) -> str:
    """Remap *path* from the current user's home to *target_home_dir*.

    If *path* lives under ``Path.home()`` the corresponding prefix is swapped
    to *target_home_dir*; otherwise the path is returned unchanged.

      /root/.duck-agent/duck-agent  -> /home/alice/.duck-agent/duck-agent
      /opt/duck-agent                 -> /opt/duck-agent  (kept as-is)

    Note: this function intentionally does NOT resolve symlinks. A venv's
    ``bin/python`` is typically a symlink to the base interpreter (e.g. a
    uv-managed CPython at ``~/.local/share/uv/python/.../python3.11``);
    resolving that symlink swaps the unit's ``ExecStart`` to a bare Python
    that has none of the venv's site-packages, so the service crashes on
    the first ``import``. Keep the symlinked path so the venv activates
    its own environment. Lexical expansion only via ``expanduser``.
    """
    current_home = Path.home()
    p = Path(path).expanduser()
    try:
        relative = p.relative_to(current_home)
        return str(Path(target_home_dir) / relative)
    except ValueError:
        return str(p)

def _hermes_home_for_target_user(target_home_dir: str) -> str:
    """Remap the current DUCK_AGENT_HOME to the equivalent under a target user's home.

    When installing a system service via sudo, get_hermes_home() resolves to
    root's home.  This translates it to the target user's equivalent path:
      /root/.duck-agent                    → /home/alice/.duck-agent
      /root/.duck-agent/profiles/coder     → /home/alice/.duck-agent/profiles/coder
      /opt/custom-duck-agent               → /opt/custom-duck-agent  (kept as-is)
    """
    current_hermes_raw = os.environ.get('DUCK_AGENT_HOME', '').strip()
    current_hermes = Path(current_hermes_raw).expanduser() if current_hermes_raw else get_hermes_home()
    current_default = Path.home() / '.duck-agent'
    target_default = Path(target_home_dir) / '.duck-agent'
    if current_hermes == current_default:
        return str(target_default)
    try:
        relative = current_hermes.relative_to(current_default)
        return str(target_default / relative)
    except ValueError:
        return str(current_hermes)

def _build_service_path_dirs(project_root: Path | None=None) -> list[str]:
    """Build PATH directory list for service units, excluding non-existent dirs."""
    if project_root is None:
        project_root = PROJECT_ROOT

    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False
    candidates = []
    venv_bin = project_root / 'venv' / 'bin'
    if _is_dir(venv_bin):
        candidates.append(str(venv_bin))
    elif sys.prefix != sys.base_prefix:
        candidates.append(str(Path(sys.prefix) / 'bin'))
    node_bin = project_root / 'node_modules' / '.bin'
    if _is_dir(node_bin):
        candidates.append(str(node_bin))
    hermes_home = get_hermes_home()
    hermes_node = hermes_home / 'node' / 'bin'
    if _is_dir(hermes_node):
        candidates.append(str(hermes_node))
    hermes_nm = hermes_home / 'node_modules' / '.bin'
    if _is_dir(hermes_nm):
        candidates.append(str(hermes_nm))
    return candidates

def _stable_service_working_dir() -> str:
    """Return a WorkingDirectory that will not disappear out from under systemd.

    The gateway does NOT need its cwd to be the source checkout — ``ExecStart``
    uses an absolute python interpreter and ``-m hermes_cli.main``, so module
    resolution does not depend on cwd. Pinning ``WorkingDirectory`` to
    ``PROJECT_ROOT`` (``Path(__file__).parent.parent``) is actively harmful:
    when the unit is generated from a transient checkout — a ``.worktrees/``
    dir, or a clone that ``duck-agent update`` later relocates/removes — the path
    rots. systemd then fails the start at the CHDIR step (``status=200/CHDIR``,
    "Changing to the requested working directory failed") *before* Python
    loads, so the on-boot ``refresh_systemd_unit_if_needed()`` self-heal never
    runs and ``Restart=always`` crash-loops forever on a dead directory.

    ``DUCK_AGENT_HOME`` is the stable anchor: it is where config/state/logs live,
    it never moves, and it is guaranteed to exist whenever the gateway is
    meaningfully installed. Fall back to ``PROJECT_ROOT`` only if DUCK_AGENT_HOME
    cannot be resolved (it always can in practice).
    """
    try:
        home = get_hermes_home()
        if home and Path(home).is_dir():
            return str(Path(home).resolve())
    except Exception:
        pass
    return str(PROJECT_ROOT)

def _systemd_watchdog_seconds(hermes_home: str | Path | None=None) -> int:
    """Resolve the managed-overlay-aware watchdog setting for a service home."""
    override_token = None
    reset_home_override = None
    if hermes_home is not None:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        override_token = set_hermes_home_override(hermes_home)
        reset_home_override = reset_hermes_home_override
    try:
        config = load_gateway_config()
        return coerce_systemd_watchdog_seconds(getattr(config, 'systemd_watchdog_seconds', 0))
    except Exception:
        logger.debug('Could not resolve effective systemd watchdog configuration', exc_info=True)
        return 0
    finally:
        if override_token is not None and reset_home_override is not None:
            reset_home_override(override_token)

def _systemd_watchdog_service_fields(hermes_home: str | Path | None=None) -> tuple[str, str]:
    """Return systemd service fields for the effective gateway config."""
    seconds = _systemd_watchdog_seconds(hermes_home)
    if seconds <= 0:
        return ('simple', '')
    return ('notify', f'NotifyAccess=main\nWatchdogSec={seconds}s\n')

def _append_node_dir_for_service(path_entries: list[str], hermes_root: Path | None=None) -> None:
    """Add the Node directory a generated service unit should use to *path_entries*.

    The Duck Agent-managed Node under ``$DUCK_AGENT_HOME/node`` goes first when it
    exists. A bare ``shutil.which("node")`` cannot be trusted on its own here:
    a service unit is written once and then survives reboots, so resolving a
    system Node that happens to be ahead on the installing shell's PATH bakes
    the wrong interpreter in permanently — the exact failure the desktop
    backend spawn was fixed for. Managed dirs are profile-scoped, so each
    profile's unit still names its own Node.

    *hermes_root* is the Duck Agent home the unit will run against. System units
    installed via sudo MUST pass the **target user's** home: probing the
    default (the calling user's — root's — tree) would bake root's Node into
    the target user's unit. The probe swallows OSError: an unreadable
    candidate dir (hardened home) means "skip the rung", not "crash the
    generator".

    PATH lookup remains the fallback rung for installs with no managed Node.
    """
    from hermes_constants import iter_hermes_node_dirs
    for directory in iter_hermes_node_dirs(hermes_root):
        entry = str(directory)
        try:
            present = directory.is_dir()
        except OSError:
            present = False
        if present and entry not in path_entries:
            path_entries.append(entry)
    resolved_node = shutil.which('node')
    if not resolved_node:
        return
    resolved_node_dir = str(Path(resolved_node).parent)
    if resolved_node_dir not in path_entries:
        path_entries.append(resolved_node_dir)

def generate_systemd_unit(system: bool=False, run_as_user: str | None=None) -> str:
    python_path = get_python_path()
    working_dir = _stable_service_working_dir()
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / 'venv')
    path_entries = _build_service_path_dirs()
    if not system:
        _append_node_dir_for_service(path_entries)
    common_bin_paths = ['/usr/local/sbin', '/usr/local/bin', '/usr/sbin', '/usr/bin', '/sbin', '/bin']
    _drain_timeout = int(_get_restart_drain_timeout() or 0)
    restart_timeout = max(60, _drain_timeout + 30)
    if system:
        username, group_name, home_dir = _system_service_identity(run_as_user)
        hermes_home = _hermes_home_for_target_user(home_dir)
        systemd_type, systemd_watchdog_directives = _systemd_watchdog_service_fields(hermes_home)
        profile_arg = _profile_arg_for_target_user(hermes_home, home_dir)
        python_path = _remap_path_for_user(python_path, home_dir)
        working_dir = str(hermes_home) if hermes_home else _remap_path_for_user(working_dir, home_dir)
        venv_dir = _remap_path_for_user(venv_dir, home_dir)
        path_entries = [_remap_path_for_user(p, home_dir) for p in path_entries]
        _target_node_entries: list[str] = []
        _append_node_dir_for_service(_target_node_entries, Path(hermes_home) if hermes_home else None)
        path_entries = [e for e in _target_node_entries if e not in path_entries] + path_entries
        path_entries.extend(_build_user_local_paths(Path(home_dir), path_entries))
        path_entries.extend(_build_wsl_interop_paths(path_entries))
        path_entries.extend(common_bin_paths)
        sane_path = ':'.join(path_entries)
        return f"""[Unit]\nDescription={SERVICE_DESCRIPTION}\nAfter=network-online.target\nWants=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType={systemd_type}\n{systemd_watchdog_directives}User={username}\nGroup={group_name}\nExecStart={python_path} -m hermes_cli.main{(f' {profile_arg}' if profile_arg else '')} gateway run\nWorkingDirectory={working_dir}\nEnvironment="HOME={home_dir}"\nEnvironment="USER={username}"\nEnvironment="LOGNAME={username}"\nEnvironment="PATH={sane_path}"\nEnvironment="VIRTUAL_ENV={venv_dir}"\nEnvironment="DUCK_AGENT_HOME={duck_agent_home}"\nRestart=always\nRestartSec=5\nRestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}\nRestartPreventExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}\nKillMode=mixed\nKillSignal=SIGTERM\nExecReload=/bin/kill -USR1 $MAINPID\nExecStopPost=-{python_path} -m gateway.cgroup_cleanup\nTimeoutStopSec={restart_timeout}\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n"""
    hermes_home = str(get_hermes_home().resolve())
    systemd_type, systemd_watchdog_directives = _systemd_watchdog_service_fields(hermes_home)
    profile_arg = _profile_arg(hermes_home)
    path_entries.extend(_build_user_local_paths(Path.home(), path_entries))
    path_entries.extend(_build_wsl_interop_paths(path_entries))
    path_entries.extend(common_bin_paths)
    sane_path = ':'.join(path_entries)
    return f"""[Unit]\nDescription={SERVICE_DESCRIPTION}\nAfter=network-online.target\nWants=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType={systemd_type}\n{systemd_watchdog_directives}ExecStart={python_path} -m hermes_cli.main{(f' {profile_arg}' if profile_arg else '')} gateway run\nWorkingDirectory={working_dir}\nEnvironment="PATH={sane_path}"\nEnvironment="VIRTUAL_ENV={venv_dir}"\nEnvironment="DUCK_AGENT_HOME={duck_agent_home}"\nRestart=always\nRestartSec=5\nRestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}\nRestartPreventExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}\nKillMode=mixed\nKillSignal=SIGTERM\nExecReload=/bin/kill -USR1 $MAINPID\nExecStopPost=-{python_path} -m gateway.cgroup_cleanup\nTimeoutStopSec={restart_timeout}\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=default.target\n"""

def _normalize_service_definition(text: str) -> str:
    return '\n'.join((line.rstrip() for line in text.strip().splitlines()))
_SYSTEMD_OPTIONAL_DIRECTIVES = ('RestartMaxDelaySec', 'RestartSteps')

def _strip_optional_systemd_directives(text: str) -> str:
    """Remove systemd directives that older hosts silently drop."""
    lines = text.splitlines()
    filtered = []
    for line in lines:
        stripped = line.strip()
        if stripped and (not stripped.startswith('#')):
            key = stripped.split('=', 1)[0].strip()
            if key in _SYSTEMD_OPTIONAL_DIRECTIVES:
                continue
        filtered.append(line)
    return '\n'.join(filtered)

def _normalize_launchd_plist_for_comparison(text: str) -> str:
    """Normalize launchd plist text for staleness checks.

    The generated plist intentionally captures a broad PATH assembled from the
    invoking shell so user-installed tools remain reachable under launchd.
    That makes raw text comparison unstable across shells, so ignore the PATH
    payload when deciding whether the installed plist is stale.
    """
    import re
    normalized = _normalize_service_definition(text)
    return re.sub('(<key>PATH</key>\\s*<string>)(.*?)(</string>)', '\\1__HERMES_PATH__\\3', normalized, flags=re.S)

def systemd_unit_is_current(system: bool=False) -> bool:
    _sync_hermes_home_from_systemd_unit(system=system)
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False
    installed = unit_path.read_text(encoding='utf-8')
    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    expected = generate_systemd_unit(system=system, run_as_user=expected_user)
    norm_installed = _normalize_service_definition(_strip_optional_systemd_directives(installed))
    norm_expected = _normalize_service_definition(_strip_optional_systemd_directives(expected))
    return norm_installed == norm_expected

def _temp_home_in_service_definition(definition: str) -> str | None:
    """Return the temp-dir DUCK_AGENT_HOME baked into a service definition, or None.

    A generated systemd unit / launchd plist carries the resolved DUCK_AGENT_HOME
    in its environment block. If that path lives under the system temp dir,
    the definition was almost certainly generated by a test/E2E harness that
    exported a throwaway ``DUCK_AGENT_HOME=/tmp/...`` — writing it to the real
    service file silently breaks the user's gateway on the next (re)start:
    the gateway comes back "active (running)" but pointed at an empty temp
    home ("No messaging platforms enabled"), deaf to every platform.
    Seen live 2026-06-11: an E2E guard probe ran ``duck-agent gateway restart``
    with ``DUCK_AGENT_HOME=/tmp/duck-agent-e2e-<pr>`` exported; the restart path's
    unit refresh baked the temp path into the production unit and the
    post-update restart produced a zombie gateway for 7+ hours.

    Matches both systemd ``Environment="DUCK_AGENT_HOME=..."`` lines and launchd
    ``<key>DUCK_AGENT_HOME</key><string>...</string>`` pairs.
    """
    import re
    import tempfile
    candidates = re.findall('DUCK_AGENT_HOME=([^"\\n]+)', definition)
    candidates += re.findall('<key>DUCK_AGENT_HOME</key>\\s*<string>(.*?)</string>', definition, flags=re.S)
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp'), Path('/var/tmp'), Path('/private/tmp'), Path('/private/var/tmp')}
    for raw in candidates:
        try:
            resolved = Path(raw.strip().strip('"')).resolve()
        except (OSError, ValueError):
            continue
        for root in temp_roots:
            if resolved == root or root in resolved.parents:
                return raw.strip()
    return None

def _refuse_temp_home_service_write(definition: str, kind: str) -> bool:
    """Refuse (with guidance) when a service definition carries a temp DUCK_AGENT_HOME."""
    temp_home = _temp_home_in_service_definition(definition)
    if temp_home is None:
        return False
    print(f'✗ Refusing to write the gateway {kind}: DUCK_AGENT_HOME resolves to a temporary directory ({temp_home}).')
    print('  This usually means a test/E2E environment exported DUCK_AGENT_HOME. Unset it (or run from a clean shell) and retry.')
    return True

def refresh_systemd_unit_if_needed(system: bool=False) -> bool:
    """Rewrite the installed systemd unit when the generated definition has changed."""
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False
    if systemd_unit_is_current(system=system):
        return False
    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    new_unit = generate_systemd_unit(system=system, run_as_user=expected_user)
    if not system and ('/pytest-of-' in new_unit or '/hermes_test"' in new_unit or '/hermes_test/' in new_unit):
        return False
    if _refuse_temp_home_service_write(new_unit, 'systemd unit'):
        return False
    unit_path.write_text(new_unit, encoding='utf-8')
    _run_systemctl(['daemon-reload'], system=system, check=True, timeout=30)
    print(f'↻ Updated gateway {_service_scope_label(system)} service definition to match the current Duck Agent install')
    return True

def _print_linger_enable_warning(username: str, detail: str | None=None) -> None:
    print()
    print('⚠ Linger not enabled — gateway may stop when you close this terminal.')
    if detail:
        print(f'  Auto-enable failed: {detail}')
    print()
    print('  On headless servers (VPS, cloud instances) run:')
    print(f'    sudo loginctl enable-linger {username}')
    print()
    print('  Then restart the gateway:')
    print(f'    systemctl --user restart {get_service_name()}.service')
    print()

def _ensure_linger_enabled() -> None:
    """Enable linger when possible so the user gateway survives logout."""
    if is_termux() or not is_linux():
        return
    import getpass
    username = getpass.getuser()
    linger_file = Path(f'/var/lib/systemd/linger/{username}')
    if linger_file.exists():
        print('✓ Systemd linger is enabled (service survives logout)')
        return
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print('✓ Systemd linger is enabled (service survives logout)')
        return
    if not shutil.which('loginctl'):
        _print_linger_enable_warning(username, linger_detail or 'loginctl not found')
        return
    print('Enabling linger so the gateway survives SSH logout...')
    try:
        result = subprocess.run(['loginctl', 'enable-linger', username], capture_output=True, text=True, encoding='utf-8', errors='replace', check=False, timeout=30)
    except Exception as e:
        _print_linger_enable_warning(username, str(e))
        return
    if result.returncode == 0:
        print('✓ Linger enabled — gateway will persist after logout')
        return
    detail = (result.stderr or result.stdout or f'exit {result.returncode}').strip()
    _print_linger_enable_warning(username, detail or linger_detail)

def _select_systemd_scope(system: bool=False) -> bool:
    if system:
        return True
    return get_systemd_unit_path(system=True).exists() and (not get_systemd_unit_path(system=False).exists())

def _system_scope_wizard_would_need_root(system: bool=False) -> bool:
    """True when the setup wizard is about to trigger a system-scope operation
    as a non-root user.

    Replicates the decision ``_select_systemd_scope`` makes inside
    ``systemd_start`` / ``systemd_restart`` / ``systemd_stop`` so the wizard
    can detect the dead-end BEFORE prompting, rather than letting
    ``SystemScopeRequiresRootError`` propagate out and leave the user
    staring at a bare shell.
    """
    if os.geteuid() == 0:
        return False
    return _select_systemd_scope(system=system)

def _print_system_scope_remediation(action: str) -> None:
    """Print actionable remediation when the wizard skips a system-scope
    prompt because the user isn't root. Keeps the wizard flowing instead of
    aborting.
    """
    svc = get_service_name()
    print_warning(f'Gateway is installed as a system-wide service — {action} requires root.')
    print_info('  Options:')
    print_info(f'    1. {action.capitalize()} it this time:')
    if action == 'start':
        print_info(f'         sudo systemctl start {svc}')
    elif action == 'stop':
        print_info(f'         sudo systemctl stop {svc}')
    elif action == 'restart':
        print_info(f'         sudo systemctl restart {svc}')
    else:
        print_info(f'         sudo systemctl {action} {svc}')
    print_info('    2. Switch to a per-user service (recommended for personal use):')
    print_info('         sudo duck-agent gateway uninstall --system')
    print_info('         duck-agent gateway install')
    print_info('         duck-agent gateway start')

def _get_restart_drain_timeout() -> float:
    """Return the configured gateway restart drain timeout in seconds."""
    raw = os.getenv('HERMES_RESTART_DRAIN_TIMEOUT', '').strip()
    if not raw:
        cfg = read_raw_config()
        agent_cfg = cfg.get('agent', {}) if isinstance(cfg, dict) else {}
        raw = str(agent_cfg.get('restart_drain_timeout', DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT))
    return parse_restart_drain_timeout(raw)

def _get_restart_after_turn_timeout() -> float:
    """Return the in-band restart wait-for-idle timeout in seconds (#77184)."""
    env_raw = os.getenv('HERMES_RESTART_AFTER_TURN_TIMEOUT')
    if env_raw is not None and str(env_raw).strip() != '':
        return parse_restart_after_turn_timeout(env_raw)
    cfg = read_raw_config()
    agent_cfg = cfg.get('agent', {}) if isinstance(cfg, dict) else {}
    if isinstance(agent_cfg, dict) and 'restart_after_turn_timeout' in agent_cfg:
        return parse_restart_after_turn_timeout(agent_cfg.get('restart_after_turn_timeout'))
    return parse_restart_after_turn_timeout(None)

def _get_restart_exit_wait_budget() -> float:
    """CLI wait for gateway exit after SIGUSR1 / self-restart (#77184)."""
    return resolve_restart_exit_wait_budget(_get_restart_drain_timeout(), _get_restart_after_turn_timeout())

def systemd_install(force: bool=False, system: bool=False, run_as_user: str | None=None, enable_on_startup: bool=True, non_interactive: bool=False):
    if system:
        _require_root_for_system_service('install')
    if has_legacy_hermes_units():
        print()
        print_legacy_unit_warning()
        print()
        if non_interactive or prompt_yes_no('Remove the legacy unit(s) before installing?', True):
            remove_legacy_hermes_units(interactive=False)
            print()
    unit_path = get_systemd_unit_path(system=system)
    scope_flag = ' --system' if system else ''
    if unit_path.exists():
        _sync_hermes_home_from_systemd_unit(system=system)
    if unit_path.exists() and (not force):
        if not systemd_unit_is_current(system=system):
            print(f'↻ Repairing outdated {_service_scope_label(system)} systemd service at: {unit_path}')
            refresh_systemd_unit_if_needed(system=system)
            if enable_on_startup:
                _run_systemctl(['enable', get_service_name()], system=system, check=True, timeout=30)
            print(f'✓ {_service_scope_label(system).capitalize()} service definition updated')
            return
        print(f'Service already installed at: {unit_path}')
        print('Use --force to reinstall')
        return
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    new_unit = generate_systemd_unit(system=system, run_as_user=run_as_user)
    if _refuse_temp_home_service_write(new_unit, 'systemd unit'):
        return
    print(f'Installing {_service_scope_label(system)} systemd service to: {unit_path}')
    unit_path.write_text(new_unit, encoding='utf-8')
    _run_systemctl(['daemon-reload'], system=system, check=True, timeout=30)
    if enable_on_startup:
        _run_systemctl(['enable', get_service_name()], system=system, check=True, timeout=30)
    print()
    enable_label = 'installed and enabled' if enable_on_startup else 'installed'
    print(f'✓ {_service_scope_label(system).capitalize()} service {enable_label}!')
    print()
    print('Next steps:')
    print(f"  {('sudo ' if system else '')}duck-agent gateway start{scope_flag}              # Start the service")
    print(f"  {('sudo ' if system else '')}duck-agent gateway status{scope_flag}             # Check status")
    print(f"  {('journalctl' if system else 'journalctl --user')} -u {get_service_name()} -f  # View logs")
    print()
    if system:
        configured_user = _read_systemd_user_from_unit(unit_path)
        if configured_user:
            print(f'Configured to run as: {configured_user}')
    else:
        _ensure_linger_enabled()
    print_systemd_scope_conflict_warning()
    print_legacy_unit_warning()

def systemd_uninstall(system: bool=False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service('uninstall')
    _run_systemctl(['stop', get_service_name()], system=system, check=False, timeout=90)
    _run_systemctl(['disable', get_service_name()], system=system, check=False, timeout=30)
    unit_path = get_systemd_unit_path(system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f'✓ Removed {unit_path}')
    _run_systemctl(['daemon-reload'], system=system, check=True, timeout=30)
    print(f'✓ {_service_scope_label(system).capitalize()} service uninstalled')

def _require_service_installed(action: str, system: bool=False) -> None:
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        scope_flag = ' --system' if system else ''
        print('✗ Gateway service is not installed')
        print(f"  Run: {('sudo ' if system else '')}duck-agent gateway install{scope_flag}")
        sys.exit(1)

def systemd_start(system: bool=False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service('start')
    else:
        _preflight_user_systemd()
    _require_service_installed('start', system=system)
    refresh_systemd_unit_if_needed(system=system)
    _run_systemctl(['start', get_service_name()], system=system, check=True, timeout=30)
    print(f'✓ {_service_scope_label(system).capitalize()} service started')

def systemd_stop(system: bool=False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service('stop')
    _require_service_installed('stop', system=system)
    _sync_hermes_home_from_systemd_unit(system=system)
    try:
        from gateway.status import get_running_pid, write_planned_stop_marker
        pid = get_running_pid(cleanup_stale=False)
        if pid is not None:
            write_planned_stop_marker(pid)
    except Exception:
        pass
    try:
        _run_systemctl(['stop', get_service_name()], system=system, check=True, timeout=90)
    except subprocess.TimeoutExpired:
        label = _service_scope_label(system)
        print(f'Gateway {label} service is still stopping after 90s; check `duck-agent gateway status` or logs for final shutdown state.')
        return
    print(f'✓ {_service_scope_label(system).capitalize()} service stopped')

def systemd_restart(system: bool=False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service('restart')
    else:
        _preflight_user_systemd()
    _require_service_installed('restart', system=system)
    refresh_systemd_unit_if_needed(system=system)
    from gateway.status import get_running_pid
    pid = get_running_pid() or _systemd_main_pid(system=system)
    if pid is not None:
        scope_label = _service_scope_label(system).capitalize()
        svc = get_service_name()
        wait_budget = _get_restart_exit_wait_budget()
        print(f'⏳ {scope_label} service restarting gracefully (PID {pid}) — waiting up to {wait_budget:.0f}s for in-flight turns + drain...')
        if _graceful_restart_via_sigusr1(pid, wait_budget):
            _run_systemctl(['reset-failed', svc], system=system, check=False, timeout=30)
            _run_systemctl(['restart', svc], system=system, check=False, timeout=90)
            if _wait_for_systemd_service_restart(system=system, previous_pid=pid):
                return
            if _systemd_service_is_start_limited(system=system):
                return
        print(f'⚠ Graceful restart did not complete within {int(wait_budget)}s; forcing a service restart...')
        _run_systemctl(['reset-failed', svc], system=system, check=False, timeout=30)
        try:
            _run_systemctl(['restart', svc], system=system, check=True, timeout=90)
        except subprocess.CalledProcessError as exc:
            if _systemd_error_indicates_start_limit(exc) or _systemd_service_is_start_limited(system=system):
                _print_systemd_start_limit_wait(system=system)
                return
            raise
        except subprocess.TimeoutExpired:
            label = _service_scope_label(system)
            print(f'Gateway {label} service is still restarting after 90s; check `duck-agent gateway status` or logs for final state.')
            return
        _wait_for_systemd_service_restart(system=system, previous_pid=pid)
        return
    if _recover_pending_systemd_restart(system=system, previous_pid=pid):
        return
    _run_systemctl(['reset-failed', get_service_name()], system=system, check=False, timeout=30)
    try:
        _run_systemctl(['restart', get_service_name()], system=system, check=True, timeout=90)
    except subprocess.CalledProcessError as exc:
        if _systemd_error_indicates_start_limit(exc) or _systemd_service_is_start_limited(system=system):
            _print_systemd_start_limit_wait(system=system)
            return
        raise
    except subprocess.TimeoutExpired:
        label = _service_scope_label(system)
        print(f'Gateway {label} service is still restarting after 90s; check `duck-agent gateway status` or logs for final state.')
        return
    _wait_for_systemd_service_restart(system=system, previous_pid=pid)

def systemd_status(deep: bool=False, system: bool=False, full: bool=False):
    system = _select_systemd_scope(system)
    unit_path = get_systemd_unit_path(system=system)
    scope_flag = ' --system' if system else ''
    if not unit_path.exists():
        print('✗ Gateway service is not installed')
        print(f"  Run: {('sudo ' if system else '')}duck-agent gateway install{scope_flag}")
        return
    if has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()
    if has_legacy_hermes_units():
        print_legacy_unit_warning()
        print()
    if not systemd_unit_is_current(system=system):
        print('⚠ Installed gateway service definition is outdated')
        print(f"  Run: {('sudo ' if system else '')}duck-agent gateway restart{scope_flag}  # auto-refreshes the unit")
        print()
    status_cmd = ['status', get_service_name(), '--no-pager']
    if full:
        status_cmd.append('-l')
    _run_systemctl(status_cmd, system=system, capture_output=False, timeout=10)
    result = _run_systemctl(['is-active', get_service_name()], system=system, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    status = result.stdout.strip()
    if status == 'active':
        print(f'✓ {_service_scope_label(system).capitalize()} gateway service is running')
    else:
        print(f'✗ {_service_scope_label(system).capitalize()} gateway service is stopped')
        print(f"  Run: {('sudo ' if system else '')}duck-agent gateway start{scope_flag}")
    configured_user = _read_systemd_user_from_unit(unit_path) if system else None
    if configured_user:
        print(f'Configured to run as: {configured_user}')
    runtime_lines = _runtime_health_lines()
    if runtime_lines:
        print()
        print('Recent gateway health:')
        for line in runtime_lines:
            print(f'  {line}')
    unit_props = _read_systemd_unit_properties(system=system)
    active_state = unit_props.get('ActiveState', '')
    sub_state = unit_props.get('SubState', '')
    exec_main_status = unit_props.get('ExecMainStatus', '')
    result_code = unit_props.get('Result', '')
    if active_state == 'activating' and sub_state == 'auto-restart':
        print('  ⏳ Restart pending: systemd is waiting to relaunch the gateway')
    elif _systemd_unit_is_start_limited(unit_props):
        print('  ⏳ Restart pending: systemd is temporarily rate-limiting starts')
        print(f"  Run after the start-limit window expires: {('sudo ' if system else '')}duck-agent gateway restart{scope_flag}")
        print(f"  Or clear it manually: systemctl {('--user ' if not system else '')}reset-failed {get_service_name()}")
    elif active_state == 'failed' and exec_main_status == str(GATEWAY_SERVICE_RESTART_EXIT_CODE):
        print('  ⚠ Planned restart is stuck in systemd failed state (exit 75)')
        print(f"  Run: systemctl {('--user ' if not system else '')}reset-failed {get_service_name()} && {('sudo ' if system else '')}duck-agent gateway start{scope_flag}")
    elif active_state == 'failed' and result_code:
        print(f'  ⚠ Systemd unit result: {result_code}')
    if system:
        print('✓ System service starts at boot without requiring systemd linger')
    elif deep:
        print_systemd_linger_guidance()
    else:
        linger_enabled, _ = get_systemd_linger_status()
        if linger_enabled is True:
            print('✓ Systemd linger is enabled (service survives logout)')
        elif linger_enabled is False:
            print('⚠ Systemd linger is disabled (gateway may stop when you log out)')
            print('  Run: sudo loginctl enable-linger $USER')
    if deep:
        print()
        print('Recent logs:')
        log_cmd = _journalctl_cmd(system) + ['-u', get_service_name(), '-n', '20', '--no-pager']
        if full:
            log_cmd.append('-l')
        subprocess.run(log_cmd, timeout=10)

def get_launchd_label() -> str:
    """Return the launchd service label, scoped per profile."""
    suffix = _profile_suffix()
    return f'ai.duck-agent.gateway-{suffix}' if suffix else 'ai.duck-agent.gateway'
_resolved_launchd_domain: str | None = None

def _launchd_domain() -> str:
    """Return the launchd domain that actually manages the gateway service.

    Probes ``gui/<uid>`` first (Aqua sessions), then ``user/<uid>``
    (Background/SSH sessions).  When neither domain contains a loaded
    service, falls back to ``launchctl managername`` as a heuristic.

    The result is cached for the lifetime of the process so that repeated
    calls (``start``, ``stop``, ``restart``) use a consistent domain.

    See #40831, #23387.
    """
    global _resolved_launchd_domain
    if _resolved_launchd_domain is not None:
        return _resolved_launchd_domain
    uid = os.getuid()
    label = get_launchd_label()
    gui_domain = f'gui/{uid}'
    user_domain = f'user/{uid}'
    try:
        subprocess.run(['launchctl', 'print', f'{gui_domain}/{label}'], check=True, timeout=5, capture_output=True)
        _resolved_launchd_domain = gui_domain
        return gui_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        subprocess.run(['launchctl', 'print', f'{user_domain}/{label}'], check=True, timeout=5, capture_output=True)
        _resolved_launchd_domain = user_domain
        return user_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        result = subprocess.run(['launchctl', 'managername'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        if 'Aqua' in (result.stdout or ''):
            _resolved_launchd_domain = gui_domain
            return gui_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    _resolved_launchd_domain = user_domain
    return user_domain
_LAUNCHD_JOB_UNLOADED_EXIT_CODES = frozenset({3, 113, 125})
_LAUNCHCTL_DOMAIN_UNSUPPORTED_CODES = frozenset({5, 125})

def _launchd_error_indicates_unloaded(exc: subprocess.CalledProcessError) -> bool:
    """True when launchctl failed because the job isn't loaded (retry bootstrap)."""
    return exc.returncode in _LAUNCHD_JOB_UNLOADED_EXIT_CODES

def _launchctl_domain_unsupported(returncode: int) -> bool:
    """True when launchctl can't manage the domain even after a fresh bootstrap.

    Codes 5 and 125 persist on macOS hosts where neither `gui/<uid>` nor
    `user/<uid>` supports service management; treat these as "launchd
    unavailable" and degrade gracefully to a detached process.
    """
    return returncode in _LAUNCHCTL_DOMAIN_UNSUPPORTED_CODES
_LAUNCHCTL_BOOTSTRAP_EIO = 5

def _launchctl_bootstrap(domain: str, plist_path, label: str, *, timeout: int=30) -> None:
    """Bootstrap a launchd job, recovering from a stale already-loaded label.

    On modern macOS, ``launchctl bootstrap`` of a label that is still
    registered in ``domain`` fails with ``5: Input/output error`` (EIO). That
    is the *already loaded* case — distinct from the domain being unmanageable,
    which callers handle via :func:`_launchctl_domain_unsupported`. A leftover
    registration from an interrupted restart leaves the job
    loaded-but-not-running, so the next bootstrap hits EIO; without this retry
    we misclassify it as "launchd cannot manage this macOS version" and degrade
    to a detached process, silently losing auto-start and crash-restart.

    Recover by booting the stale label out and bootstrapping once more. If the
    retry still fails, the ``CalledProcessError`` propagates so callers apply
    their domain-unsupported fallback for a genuinely broken domain.
    """
    try:
        subprocess.run(['launchctl', 'bootstrap', domain, str(plist_path)], check=True, timeout=timeout)
        return
    except subprocess.CalledProcessError as exc:
        if exc.returncode != _LAUNCHCTL_BOOTSTRAP_EIO:
            raise
        subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], check=False, timeout=timeout)
        subprocess.run(['launchctl', 'bootstrap', domain, str(plist_path)], check=True, timeout=timeout)

def _launchd_reload_log_path() -> Path:
    """Path the launchd reload watchdog tails for persistent-orphan detection."""
    return get_hermes_home() / 'logs' / 'launchd-reload.log'

def _append_launchd_reload_log(message: str) -> None:
    """Append a timestamped line to the launchd reload log (best-effort)."""
    path = _launchd_reload_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        stamp = _dt.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
        with path.open('a', encoding='utf-8') as fh:
            fh.write(f'[{stamp}] {message}\n')
    except OSError:
        pass

def _launchctl_label_supervising_process(label: str) -> bool:
    """True when launchd both knows ``label`` AND is running a process for it.

    A bare ``launchctl list <label>`` exit-0 only proves a *definition* is
    registered — it also returns 0 for ``state = not running`` (macOS 26+),
    which is why :func:`_probe_launchd_service_running` already insists on a
    PID. The reload's success check needs the same standard: ending the retry
    loop on "a definition exists" can report success for a job launchd is not
    actually running.

    Measured against live launchd (2026-08-05): immediately after ``bootout``
    the label deregisters within ~1s (rc=113) while the old process keeps
    draining, so this is NOT what distinguishes a draining instance from a
    fresh one — waiting for the old PID to exit before bootstrapping is what
    does that. This check is the narrower guarantee: success means launchd is
    supervising a live process.
    """
    try:
        result = subprocess.run(['launchctl', 'list', label], check=False, timeout=10, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            return False
        return _parse_launchd_pid_from_list_output(result.stdout) is not None
    except (subprocess.TimeoutExpired, OSError):
        return False

def _retry_launchctl_bootstrap_until_registered(domain: str, plist_path, label: str, *, deadline: float) -> bool:
    """Bootstrap with retry until the label is registered or ``deadline`` passes.

    Wraps :func:`_launchctl_bootstrap` (which already recovers the EIO
    "already loaded" case) in a wall-clock retry loop for the *transient*
    failure mode: under high load or a launchd race the bootstrap can fail
    even after ``bootout`` already tore down the prior registration, leaving
    the service orphaned from ``KeepAlive`` supervision. The reported incident
    happened during a graceful drain (default ``agent.restart_drain_timeout``
    = 180s), so a fixed ~10s window is too short — retry until ``deadline``.

    Both ``CalledProcessError`` and ``TimeoutExpired`` are treated as
    retryable: a ``bootstrap`` that times out after ``bootout`` still leaves
    the service unloaded, so it must be retried, not allowed to escape. On
    each failure a timestamped line is appended to the reload log; success is
    confirmed with ``launchctl list`` (not merely a zero bootstrap exit).
    Returns True once the label is registered, False if the deadline is hit.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            _launchctl_bootstrap(domain, plist_path, label, timeout=30)
            if _launchctl_label_supervising_process(label):
                return True
            _append_launchd_reload_log(f'bootstrap attempt {attempt} exited 0 but {domain}/{label} has no supervised process (launchctl list) — retrying')
        except subprocess.CalledProcessError as exc:
            _append_launchd_reload_log(f'bootstrap attempt {attempt} failed (rc={exc.returncode}) for {domain}/{label} — retrying')
        except subprocess.TimeoutExpired:
            _append_launchd_reload_log(f'bootstrap attempt {attempt} timed out for {domain}/{label} — retrying')
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)

def _launchd_unsupported_marker_path() -> Path:
    return get_hermes_home() / '.gateway-launchd-unsupported'

def _write_launchd_unsupported_marker() -> None:
    """Persist that launchd cannot supervise the gateway on this host."""
    import json
    from datetime import datetime, timezone
    try:
        _launchd_unsupported_marker_path().write_text(json.dumps({'written_at': datetime.now(timezone.utc).isoformat(), 'reason': 'launchd domain unsupported (exit 5/125)'}), encoding='utf-8')
    except OSError:
        pass

def _clear_launchd_unsupported_marker() -> None:
    """Clear the unsupported marker when launchd bootstrap succeeds."""
    try:
        _launchd_unsupported_marker_path().unlink(missing_ok=True)
    except OSError:
        pass

def _launchd_unsupported_marker_exists() -> bool:
    return _launchd_unsupported_marker_path().exists()

def _gateway_run_command() -> list[str]:
    """Build the `python -m hermes_cli.main [--profile X] gateway run --replace` argv.

    Profile-aware: honors the active DUCK_AGENT_HOME via `_profile_arg()` so the
    detached fallback launches into the same profile as the CLI invocation.
    """
    cmd = [get_python_path(), '-m', 'hermes_cli.main']
    profile_arg = _profile_arg()
    if profile_arg:
        cmd.extend(profile_arg.split())
    cmd.extend(['gateway', 'run', '--replace'])
    return cmd

def _spawn_detached_gateway() -> bool:
    """Launch the gateway as a detached background process (launchd fallback).

    Used when launchctl can no longer bootstrap/kickstart the gateway on
    macOS 26+ (issue #23387). Mirrors the `nohup duck-agent gateway run --replace`
    workaround but keeps it CLI-managed: stdout/stderr go to the profile's
    gateway logs and the PID is tracked via the gateway.pid file that
    `run_gateway` writes, so stop/status/restart keep working.
    """
    from hermes_cli._subprocess_compat import windows_detach_popen_kwargs
    log_dir = get_hermes_home() / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / 'gateway.log'
    err_path = log_dir / 'gateway.error.log'
    try:
        out = open(out_path, 'ab')
        err = open(err_path, 'ab')
    except OSError:
        return False
    try:
        with out, err:
            subprocess.Popen(_gateway_run_command(), stdin=subprocess.DEVNULL, stdout=out, stderr=err, **windows_detach_popen_kwargs())
    except OSError:
        return False
    return True

def _launchd_fallback_to_detached(reason: str, *, exit_on_failure: bool=True) -> bool:
    """Start the gateway detached when launchd can't manage it, with guidance.

    Returns True if the detached gateway was launched. When it can't be
    launched, prints the manual workaround and (by default) exits non-zero so
    the failure surfaces instead of silently doing nothing.
    """
    from hermes_constants import display_hermes_home as _dhh
    _write_launchd_unsupported_marker()
    print(f'⚠ launchd cannot manage the gateway on this macOS version ({reason}).')
    if _spawn_detached_gateway():
        print('✓ Started gateway as a background process instead')
        print('  It will NOT auto-start at login or auto-restart on crash.')
        print(f'  Logs: {_dhh()}/logs/gateway.log')
        print('  Stop it with: duck-agent gateway stop')
        return True
    print_error('Failed to start the gateway as a background process.')
    print(f'  Try manually: nohup duck-agent gateway run --replace > {_dhh()}/logs/gateway.log 2>&1 &')
    if exit_on_failure:
        sys.exit(1)
    return False

def generate_launchd_plist() -> str:
    python_path = get_python_path()
    working_dir = _stable_service_working_dir()
    hermes_home = str(get_hermes_home().resolve())
    log_dir = get_hermes_home() / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    label = get_launchd_label()
    profile_arg = _profile_arg(hermes_home)
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / 'venv')
    priority_dirs = _build_service_path_dirs()
    _append_node_dir_for_service(priority_dirs)
    sane_path = ':'.join(dict.fromkeys(priority_dirs + [p for p in os.environ.get('PATH', '').split(':') if p]))
    prog_args = [f'<string>{python_path}</string>', '<string>-m</string>', '<string>hermes_cli.main</string>']
    if profile_arg:
        for part in profile_arg.split():
            prog_args.append(f'<string>{part}</string>')
    prog_args.extend(['<string>gateway</string>', '<string>run</string>', '<string>--replace</string>'])
    prog_args_xml = '\n        '.join(prog_args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n    <key>Label</key>\n    <string>{label}</string>\n\n    <key>ProgramArguments</key>\n    <array>\n        {prog_args_xml}\n    </array>\n    \n    <key>WorkingDirectory</key>\n    <string>{working_dir}</string>\n    \n    <key>EnvironmentVariables</key>\n    <dict>\n        <key>PATH</key>\n        <string>{sane_path}</string>\n        <key>VIRTUAL_ENV</key>\n        <string>{venv_dir}</string>\n        <key>DUCK_AGENT_HOME</key>\n        <string>{duck_agent_home}</string>\n    </dict>\n\n    <key>LimitLoadToSessionType</key>\n    <array>\n        <string>Aqua</string>\n        <string>Background</string>\n    </array>\n    \n    <key>RunAtLoad</key>\n    <true/>\n    \n    <key>KeepAlive</key>\n    <true/>\n\n    <!-- ThrottleInterval raises launchd's default 10s minimum respawn interval\n         to 30s so a crash-looping gateway can't hammer launchd into a rapid\n         respawn storm; ExitTimeOut gives the gateway 25s of graceful-drain\n         headroom before launchd escalates from SIGTERM to SIGKILL on stop. -->\n    <key>ThrottleInterval</key>\n    <integer>30</integer>\n\n    <key>ExitTimeOut</key>\n    <integer>25</integer>\n\n    <key>StandardOutPath</key>\n    <string>{log_dir}/gateway.log</string>\n    \n    <key>StandardErrorPath</key>\n    <string>{log_dir}/gateway.error.log</string>\n</dict>\n</plist>\n"""

def launchd_plist_is_current() -> bool:
    """Check if the installed launchd plist matches the currently generated one."""
    plist_path = get_launchd_plist_path()
    if not plist_path.exists():
        return False
    installed = plist_path.read_text(encoding='utf-8')
    expected = generate_launchd_plist()
    return _normalize_launchd_plist_for_comparison(installed) == _normalize_launchd_plist_for_comparison(expected)

def refresh_launchd_plist_if_needed() -> bool:
    """Rewrite the installed launchd plist when the generated definition has changed.

    Unlike systemd, launchd picks up plist changes on the next ``launchctl kill``/
    ``launchctl kickstart`` cycle — no daemon-reload is needed. We still bootout/
    bootstrap to make launchd re-read the updated plist immediately.
    """
    plist_path = get_launchd_plist_path()
    if not plist_path.exists() or launchd_plist_is_current():
        return False
    new_plist = generate_launchd_plist()
    if _refuse_temp_home_service_write(new_plist, 'launchd plist'):
        return False
    plist_path.write_text(new_plist, encoding='utf-8')
    label = get_launchd_label()
    domain = _launchd_domain()
    target = f'{domain}/{label}'
    gateway_pid = None
    try:
        from gateway.status import get_running_pid
        gateway_pid = get_running_pid()
    except Exception:
        gateway_pid = None
    if gateway_pid is not None and hasattr(os, 'setsid'):
        reload_log_path = get_hermes_home() / 'logs' / 'launchd-reload.log'
        try:
            reload_log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _append_launchd_reload_log(f'Launchd reload helper started for {target}')
        _reload_budget = int(max(30.0, _get_restart_drain_timeout()))
        submit_label = f'{label}.reload.{os.getpid()}.{int(time.time())}'
        reload_script = f"""sleep 2; launchctl bootout {shlex.quote(target)} 2>/dev/null; _wait_deadline=$(($(date +%s) + {_reload_budget})); while kill -0 {gateway_pid} 2>/dev/null; do   if [ $(date +%s) -ge $_wait_deadline ]; then     echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] old gateway pid {gateway_pid} still alive after {_reload_budget}s drain wait — bootstrapping anyway" >> {shlex.quote(str(reload_log_path))};     break;   fi;   sleep 1; done; sleep 1; _deadline=$(($(date +%s) + {_reload_budget})); while :; do   launchctl bootstrap {shlex.quote(domain)} {shlex.quote(str(plist_path))} 2>/dev/null;   if launchctl list {shlex.quote(label)} 2>/dev/null | grep -qE '\\"PID\\" = [0-9]+;'; then break; fi;   echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] bootstrap not yet registered for {shlex.quote(target)} — retrying" >> {shlex.quote(str(reload_log_path))};   if [ $(date +%s) -ge $_deadline ]; then break; fi;   sleep 2; done; if ! launchctl list {shlex.quote(label)} 2>/dev/null | grep -qE '\\"PID\\" = [0-9]+;'; then   echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] FAILED launchd reload for {shlex.quote(target)} — service NOT registered after {_reload_budget}s of retries" >> {shlex.quote(str(reload_log_path))}; fi; launchctl remove {shlex.quote(submit_label)} 2>/dev/null"""
        try:
            subprocess.Popen(['launchctl', 'submit', '-l', submit_label, '-o', str(reload_log_path), '-e', str(reload_log_path), '--', '/bin/bash', '-c', reload_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning('Deferred launchd reload could not be spawned: %s', e)
            _append_launchd_reload_log(f'FAILED to spawn launchd reload helper for {target}: {e} — falling back to in-process bootout/bootstrap')
        else:
            print('↻ Updated gateway launchd service definition; reload deferred to a transient launchd job (survives the bootout of this process)')
            return True
    subprocess.run(['launchctl', 'bootout', target], check=False, timeout=90)
    _reload_budget = max(30.0, _get_restart_drain_timeout())
    if gateway_pid is not None and (not _wait_for_pid_exit(gateway_pid, _reload_budget)):
        _append_launchd_reload_log(f'old gateway pid {gateway_pid} still alive after {int(_reload_budget)}s drain wait — bootstrapping {target} anyway')
    _deadline = time.monotonic() + _reload_budget
    if not _retry_launchctl_bootstrap_until_registered(domain, plist_path, label, deadline=_deadline):
        _append_launchd_reload_log(f'FAILED launchd reload of {target} — service NOT registered after retrying for {int(_reload_budget)}s (in-process fallback path)')
        logger.error('launchd reload of %s failed — service not registered after %ds of retries; see %s', target, int(_reload_budget), _launchd_reload_log_path())
    print('↻ Updated gateway launchd service definition to match the current Duck Agent install')
    return True

def launchd_install(force: bool=False):
    plist_path = get_launchd_plist_path()
    if plist_path.exists() and (not force):
        if not launchd_plist_is_current():
            print(f'↻ Repairing outdated launchd service at: {plist_path}')
            refresh_launchd_plist_if_needed()
            print('✓ Service definition updated')
            return
        print(f'Service already installed at: {plist_path}')
        print('Use --force to reinstall')
        return
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    new_plist = generate_launchd_plist()
    if _refuse_temp_home_service_write(new_plist, 'launchd plist'):
        return
    print(f'Installing launchd service to: {plist_path}')
    plist_path.write_text(new_plist, encoding='utf-8')
    try:
        _launchctl_bootstrap(_launchd_domain(), plist_path, get_launchd_label(), timeout=30)
    except subprocess.CalledProcessError as e:
        if not _launchctl_domain_unsupported(e.returncode):
            raise
        _launchd_fallback_to_detached(f'launchctl bootstrap exit {e.returncode}')
        return
    print()
    print('✓ Service installed and loaded!')
    _clear_launchd_unsupported_marker()
    print()
    print('Next steps:')
    print('  duck-agent gateway status             # Check status')
    from hermes_constants import display_hermes_home as _dhh
    print(f'  tail -f {_dhh()}/logs/gateway.log  # View logs')

def launchd_uninstall():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    subprocess.run(['launchctl', 'bootout', f'{_launchd_domain()}/{label}'], check=False, timeout=90)
    if plist_path.exists():
        plist_path.unlink()
        print(f'✓ Removed {plist_path}')
    print('✓ Service uninstalled')

def launchd_start():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    if not plist_path.exists():
        new_plist = generate_launchd_plist()
        if _refuse_temp_home_service_write(new_plist, 'launchd plist'):
            sys.exit(1)
        print('↻ launchd plist missing; regenerating service definition')
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(new_plist, encoding='utf-8')
        try:
            _launchctl_bootstrap(_launchd_domain(), plist_path, label, timeout=30)
            subprocess.run(['launchctl', 'kickstart', f'{_launchd_domain()}/{label}'], check=True, timeout=30)
        except subprocess.CalledProcessError as e:
            if not _launchctl_domain_unsupported(e.returncode):
                raise
            _launchd_fallback_to_detached(f'launchctl exit {e.returncode}')
            return
        print('✓ Service started')
        _clear_launchd_unsupported_marker()
        return
    refresh_launchd_plist_if_needed()
    try:
        subprocess.run(['launchctl', 'kickstart', f'{_launchd_domain()}/{label}'], check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        if not _launchd_error_indicates_unloaded(e):
            raise
        print('↻ launchd job was unloaded; reloading service definition')
        try:
            _launchctl_bootstrap(_launchd_domain(), plist_path, label, timeout=30)
            subprocess.run(['launchctl', 'kickstart', f'{_launchd_domain()}/{label}'], check=True, timeout=30)
        except subprocess.CalledProcessError as e2:
            if not _launchctl_domain_unsupported(e2.returncode):
                raise
            _launchd_fallback_to_detached(f'launchctl exit {e2.returncode}')
            return
    print('✓ Service started')
    _clear_launchd_unsupported_marker()

def launchd_stop():
    label = get_launchd_label()
    target = f'{_launchd_domain()}/{label}'
    try:
        from gateway.status import get_running_pid, write_planned_stop_marker
        pid = get_running_pid(cleanup_stale=False)
        if pid is not None:
            write_planned_stop_marker(pid)
    except Exception:
        pass
    try:
        subprocess.run(['launchctl', 'bootout', target], check=True, timeout=90)
    except subprocess.CalledProcessError as e:
        if _launchd_error_indicates_unloaded(e) or _launchctl_domain_unsupported(e.returncode):
            pass
        else:
            raise
    _wait_for_gateway_exit(timeout=10.0, force_after=5.0)
    print('✓ Service stopped')

def _wait_for_gateway_exit(timeout: float=10.0, force_after: float | None=5.0) -> bool:
    """Wait for the gateway process (by saved PID) to exit.

    Uses the PID from the gateway.pid file — not launchd labels — so this
    works correctly when multiple gateway instances run under separate
    DUCK_AGENT_HOME directories.

    Args:
        timeout: Total seconds to wait before giving up.
        force_after: Seconds of graceful waiting before escalating to force-kill.
    """
    import time
    from gateway.status import get_running_pid
    deadline = time.monotonic() + timeout
    force_deadline = time.monotonic() + force_after if force_after is not None else None
    force_sent = False
    while time.monotonic() < deadline:
        pid = get_running_pid()
        if pid is None:
            return True
        if force_after is not None and (not force_sent) and (time.monotonic() >= force_deadline):
            try:
                terminate_pid(pid, force=True)
                print(f'⚠ Gateway PID {pid} did not exit gracefully; sent SIGKILL')
            except (ProcessLookupError, PermissionError, OSError):
                return True
            force_sent = True
        time.sleep(0.3)
    remaining_pid = get_running_pid()
    if remaining_pid is not None:
        print(f'⚠ Gateway PID {remaining_pid} still running after {timeout}s — restart may fail')
        return False
    return True

def launchd_restart():
    label = get_launchd_label()
    target = f'{_launchd_domain()}/{label}'
    drain_timeout = _get_restart_drain_timeout()
    from gateway.status import get_running_pid
    try:
        pid = get_running_pid()
        if pid is not None and _request_gateway_self_restart(pid):
            print('✓ Service restart requested')
            _clear_launchd_unsupported_marker()
            return
        if pid is not None:
            print(f'→ Stopping gateway (PID {pid}) — draining in-flight runs (up to {drain_timeout:.0f}s)...')
            try:
                terminate_pid(pid, force=False)
            except (ProcessLookupError, PermissionError, OSError):
                pid = None
            if pid is not None:
                exited = _wait_for_gateway_exit(timeout=drain_timeout, force_after=None)
                if not exited:
                    print(f'⚠ Gateway drain timed out after {drain_timeout:.0f}s — forcing launchd restart')
        subprocess.run(['launchctl', 'kickstart', '-k', target], check=True, timeout=90)
        print('✓ Service restarted')
        _clear_launchd_unsupported_marker()
    except subprocess.CalledProcessError as e:
        if not _launchd_error_indicates_unloaded(e):
            if _launchctl_domain_unsupported(e.returncode):
                _launchd_fallback_to_detached(f'launchctl kickstart exit {e.returncode}')
                return
            raise
        print('↻ launchd job was unloaded; reloading')
        plist_path = get_launchd_plist_path()
        try:
            subprocess.run(['launchctl', 'bootout', target], check=False, timeout=90)
            subprocess.run(['launchctl', 'bootstrap', _launchd_domain(), str(plist_path)], check=True, timeout=30)
            subprocess.run(['launchctl', 'kickstart', target], check=True, timeout=30)
        except subprocess.CalledProcessError as e2:
            if not _launchctl_domain_unsupported(e2.returncode):
                raise
            _launchd_fallback_to_detached(f'launchctl exit {e2.returncode}')
            return
        print('✓ Service restarted')
        _clear_launchd_unsupported_marker()

def launchd_status(deep: bool=False):
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    try:
        result = subprocess.run(['launchctl', 'list', label], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        service_listed = result.returncode == 0
        list_output = result.stdout
    except subprocess.TimeoutExpired:
        service_listed = False
        list_output = ''
    launchd_pid = _parse_launchd_pid_from_list_output(list_output) if service_listed else None
    from gateway.status import get_running_pid
    fallback_pid = get_running_pid(cleanup_stale=False)
    if launchd_pid is not None and fallback_pid == launchd_pid:
        fallback_pid = None
    launchd_unsupported = _launchd_unsupported_marker_exists()
    print(f'Launchd plist: {plist_path}')
    if launchd_plist_is_current():
        print('✓ Service definition matches the current Duck Agent install')
    else:
        print('⚠ Service definition is stale relative to the current Duck Agent install')
        print('  Run: duck-agent gateway start')
    if service_listed:
        if launchd_pid is not None:
            print(f'✓ Gateway is supervised by launchd (PID {launchd_pid})')
            print('  Auto-start at login and auto-restart on crash are available.')
            if launchd_unsupported:
                print('  (launchd domain was previously unavailable but is now working)')
        elif launchd_unsupported:
            print('⚠ Gateway service is registered but launchd is not supervising it')
            print('  launchd cannot manage the gateway on this macOS version.')
            if fallback_pid:
                print(f'✓ Detached fallback process is running (PID {fallback_pid})')
                print('  Cron jobs will fire. Stop with: duck-agent gateway stop')
            else:
                print('✗ No fallback process is running')
                print('  Run: duck-agent gateway start')
            print('  ⚠ Auto-start at login and auto-restart on crash are NOT available.')
        else:
            print('✓ Gateway service is registered with launchd')
            print(list_output)
            if fallback_pid:
                print(f'  Detached gateway process is running (PID {fallback_pid})')
    else:
        print('✗ Gateway service is not loaded')
        print('  Service definition exists locally but launchd has not loaded it.')
        print('  Run: duck-agent gateway start')
        if fallback_pid:
            print(f'  Note: a detached gateway process is running (PID {fallback_pid})')
    if deep:
        log_file = get_hermes_home() / 'logs' / 'gateway.log'
        if log_file.exists():
            print()
            print('Recent logs:')
            subprocess.run(['tail', '-20', str(log_file)], timeout=10)

def _truthy_env(value: str | None) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}

def _is_official_docker_checkout() -> bool:
    return str(PROJECT_ROOT) == '/opt/duck-agent' and (PROJECT_ROOT / 'docker' / 'entrypoint.sh').is_file()

def _running_under_gateway_supervisor() -> bool:
    """Return True when this process IS the gateway a service manager launched.

    The conflict guard below must never fire on the service's own startup, or
    it would wedge the unit into a respawn/refuse loop. Each supervisor exports
    a reliable marker into the child's environment:

      - systemd sets ``INVOCATION_ID`` for every unit it launches (the same
        marker ``gateway/run.py`` already uses to pick the restart path).
      - launchd sets ``XPC_SERVICE_NAME`` to the job label for jobs it spawns;
        interactive shells inherit the sentinel ``"0"`` instead.
      - the s6-overlay container longrun exports ``HERMES_S6_SUPERVISED_CHILD``.
      - wrapped services can opt in with ``--external-supervisor`` when their
        launcher strips the native systemd/launchd marker.
    """
    return is_gateway_supervisor_process()

def _guard_named_profile_under_multiplexer(force: bool=False) -> None:
    """Refuse a named-profile gateway when a multiplexer is already serving it.

    When the default profile's gateway runs with gateway.multiplex_profiles=on,
    it is the sole inbound process for EVERY profile on the host. Starting a
    separate gateway for a named profile would double-bind that profile's
    platforms (two pollers on one bot token, port fights). In that mode a
    named-profile ``duck-agent gateway run`` is always a misconfiguration, so we
    hard-error with a pointer to the multiplexer. ``--force`` overrides.

    Inert unless ALL of: (a) this invocation is a named profile, (b) a default-
    profile gateway is running, (c) that gateway's config has multiplexing on.
    """
    if force:
        return
    try:
        suffix = _profile_suffix()
    except Exception:
        return
    if not suffix:
        return
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        from gateway.status import get_running_pid as _default_running_pid
    except Exception:
        return
    try:
        import yaml as _yaml
        from gateway.status import _read_pid_record
        default_pid_path = default_root / 'gateway.pid'
        rec = _read_pid_record(default_pid_path)
        if not rec:
            return
        from gateway.status import _pid_exists, _pid_from_record
        pid = _pid_from_record(rec)
        if not pid or not _pid_exists(pid):
            return
        from gateway.config import _env_multiplex_profiles_override
        env_multiplex = _env_multiplex_profiles_override()
        if env_multiplex is False:
            return
        if env_multiplex is True:
            multiplex = True
        else:
            cfg_path = default_root / 'config.yaml'
            if not cfg_path.exists():
                return
            from hermes_cli.config import read_user_config_raw
            cfg = read_user_config_raw(cfg_path)
            multiplex = bool(cfg.get('multiplex_profiles') or (cfg.get('gateway', {}) or {}).get('multiplex_profiles'))
        if not multiplex:
            return
    except Exception:
        logger.debug('Multiplexer-conflict probe failed', exc_info=True)
        return
    print_error(f"The default gateway is running as a profile multiplexer and already serves profile '{suffix}'.")
    print('  When gateway.multiplex_profiles is on, the default gateway is the\n  single inbound process for every profile. Starting a separate\n  gateway for this profile would double-bind its platforms (two\n  pollers on one bot token, port conflicts).\n')
    print('  Manage the multiplexer instead (from the default profile):')
    print()
    print('    duck-agent gateway restart')
    print()
    print('  Pass --force to start a separate profile gateway anyway (not')
    print('  recommended while the multiplexer is running).')
    sys.exit(1)

def _guard_supervised_gateway_conflict(force: bool=False) -> None:
    """Refuse a foreground gateway when a service manager already supervises one.

    Running ``duck-agent gateway run [--replace]`` (or the manual-restart fallback)
    from a shell on a systemd/launchd host spawns a second, long-lived
    dispatcher that escapes the service cgroup, survives
    ``systemctl restart``, and becomes a silent concurrent writer on the shared
    kanban DB — the documented root cause of multi-writer SQLite WAL corruption
    (issue #35240). Pass ``--force`` to start anyway.
    """
    if force or _running_under_gateway_supervisor():
        return
    try:
        snapshot = get_gateway_runtime_snapshot()
    except Exception:
        logger.debug('Supervised-gateway conflict probe failed', exc_info=True)
        return
    if not (snapshot.service_installed and snapshot.service_running):
        return
    print_error(f'A gateway is already running under {snapshot.manager} for this profile.')
    print('  Starting another one from a shell leaves an orphan dispatcher that\n  escapes the service, survives restarts, and writes to the same kanban\n  DB concurrently — which can corrupt it. Restart the supervised gateway\n  instead:')
    print()
    print('    duck-agent gateway restart')
    print()
    print('  Pass --force to start a foreground gateway anyway (not recommended\n  while the service is running).')
    sys.exit(1)

def _guard_existing_gateway_process_conflict(replace: bool=False) -> None:
    """Refuse duplicate foreground startup before importing gateway.run.

    ``gateway.run`` performs the authoritative PID/lock check, but importing it
    is expensive: it pulls in model_tools/plugin discovery first. On small
    instances, a supervisor or dashboard loop repeatedly running bare
    ``duck-agent gateway run`` can burn memory/CPU just to fail with "already
    running" after plugin discovery. This cheap PID-file preflight preserves the
    same user-facing contract while avoiding that startup work without scanning
    unrelated gateway processes from other DUCK_AGENT_HOME roots.
    """
    if replace or _running_under_gateway_supervisor():
        return
    try:
        from gateway.status import get_running_pid
        pid = get_running_pid()
    except Exception:
        logger.debug('Existing-gateway process probe failed', exc_info=True)
        return
    if pid is None:
        return
    print_error(f'Another gateway instance is already running (PID {pid}).')
    print("  Use 'duck-agent gateway restart' to replace it,")
    print("  or 'duck-agent gateway stop' first.")
    print("  Or use 'duck-agent gateway run --replace' to auto-replace.")
    sys.exit(1)

def _guard_official_docker_root_gateway() -> None:
    """Refuse gateway startup when the official Docker privilege drop was bypassed."""
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return
    if _truthy_env(os.getenv('HERMES_ALLOW_ROOT_GATEWAY')):
        return
    if not _is_official_docker_checkout():
        return
    print_error('Refusing to run the Duck Agent gateway as root inside the official Docker image.')
    print("  The image entrypoint normally drops privileges to the 'duck-agent' user. If you override entrypoint in Docker Compose, include /opt/duck-agent/docker/entrypoint.sh before the Duck Agent command.")
    print('  Running the gateway as root can leave root-owned files in $DUCK_AGENT_HOME and break later non-root dashboard/gateway runs.')
    print('  Set HERMES_ALLOW_ROOT_GATEWAY=1 only if you intentionally accept this risk.')
    sys.exit(1)

def run_gateway(verbose: int=0, quiet: bool=False, replace: bool=False, force: bool=False):
    """Run the gateway in foreground.

    Args:
        verbose: Stderr log verbosity count added on top of default WARNING (0=WARNING, 1=INFO, 2+=DEBUG).
        quiet: Suppress all stderr log output.
        replace: If True, kill any existing gateway instance before starting.
                 This prevents systemd restart loops when the old process
                 hasn't fully exited yet.
        force: Skip the supervised-gateway conflict guard and start even when a
               systemd/launchd service is already supervising this profile.
    """
    _guard_official_docker_root_gateway()
    _guard_named_profile_under_multiplexer(force=force)
    _guard_supervised_gateway_conflict(force=force)
    _guard_existing_gateway_process_conflict(replace=replace)
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        _stdin_is_tty = bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        _stdin_is_tty = False
    _absorb_windows_console_controls = _windows_gateway_should_absorb_console_controls()
    if _absorb_windows_console_controls:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if hasattr(signal, 'SIGBREAK'):
                signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler(None, 1)
        except (OSError, AttributeError):
            pass
    if supports_systemd_services():
        try:
            refresh_systemd_unit_if_needed(system=False)
        except Exception:
            pass
    from gateway.run import start_gateway
    print('┌─────────────────────────────────────────────────────────┐')
    print('│           ⚕ Duck Agent Gateway Starting...                 │')
    print('├─────────────────────────────────────────────────────────┤')
    print('│  Messaging platforms + cron scheduler                    │')
    print('│  Press Ctrl+C to stop                                   │')
    print('└─────────────────────────────────────────────────────────┘')
    print()
    verbosity = None if quiet else verbose
    import atexit as _atexit
    import traceback as _traceback
    from datetime import datetime as _dt, timezone as _tz

    def _exit_diag(tag: str, **extra: object) -> None:
        if os.environ.get('HERMES_GATEWAY_EXIT_DIAG', '1') != '1':
            return
        try:
            from hermes_constants import get_hermes_home as _ghh
            log_dir = _ghh() / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.now(_tz.utc).isoformat()
            line = {'ts': ts, 'tag': tag, 'pid': os.getpid(), 'python': sys.version.split()[0], 'platform': sys.platform, **extra}
            import json as _json
            with open(log_dir / 'gateway-exit-diag.log', 'a', encoding='utf-8') as f:
                f.write(_json.dumps(line, default=str) + '\n')
        except Exception:
            pass
    _exit_diag('gateway.start', replace=replace, argv=sys.argv, stdin_is_tty=_stdin_is_tty, absorb_windows_console_controls=_absorb_windows_console_controls)

    def _atexit_hook() -> None:
        _exit_diag('atexit.hook', sys_exc=repr(sys.exc_info()))
    _atexit.register(_atexit_hook)
    try:
        import time as _time
        from gateway.status import record_start_and_check_storm
        _max_starts = 5
        _win = 120.0
        try:
            from hermes_cli.config import load_config
            _cfg = load_config()
            _gw = _cfg.get('gateway') if isinstance(_cfg, dict) else None
            _rs = _gw.get('respawn_storm') if isinstance(_gw, dict) else None
            if isinstance(_rs, dict):
                if isinstance(_rs.get('max_starts'), int):
                    _max_starts = _rs['max_starts']
                if isinstance(_rs.get('window_seconds'), (int, float)):
                    _win = float(_rs['window_seconds'])
        except Exception:
            pass
        try:
            _env_starts = os.getenv('HERMES_GATEWAY_MAX_STARTS')
            if _env_starts is not None:
                _max_starts = int(_env_starts)
        except ValueError:
            pass
        try:
            _env_win = os.getenv('HERMES_GATEWAY_START_WINDOW_S')
            if _env_win is not None:
                _win = float(_env_win)
        except ValueError:
            pass
        _storm = record_start_and_check_storm(max_starts=_max_starts, window_s=_win) if _max_starts > 0 else None
        if _storm is not None:
            logger.warning('Gateway (re)started %d times in %.0fs — backing off %.0fs to break a respawn storm.', _storm.count, _storm.window_s, _storm.backoff_s)
            _time.sleep(_storm.backoff_s)
    except Exception as _be:
        logger.debug('respawn-storm breaker check failed (non-fatal): %s', _be)

    def _hard_exit_after_gateway_teardown(code: int) -> None:
        from gateway.run import _exit_after_graceful_shutdown
        _exit_after_graceful_shutdown(code)
    success = False
    try:
        success = asyncio.run(start_gateway(replace=replace, verbosity=verbosity))
        _exit_diag('asyncio.run.returned', success=success)
    except KeyboardInterrupt:
        _exit_diag('asyncio.run.KeyboardInterrupt', traceback=_traceback.format_exc())
        print('\nGateway stopped.')
        _hard_exit_after_gateway_teardown(0)
        return
    except SystemExit as e:
        _exit_diag('asyncio.run.SystemExit', code=getattr(e, 'code', None), traceback=_traceback.format_exc())
        if e.code is None:
            _code = 0
        elif isinstance(e.code, int):
            _code = e.code
        else:
            _code = 1
        _hard_exit_after_gateway_teardown(_code)
    except BaseException as e:
        _exit_diag('asyncio.run.exception', exc_type=type(e).__name__, exc_repr=repr(e), traceback=_traceback.format_exc())
        raise
    if not success:
        _exit_diag('gateway.exit_nonzero')
        _hard_exit_after_gateway_teardown(1)
    _exit_diag('gateway.exit_clean')
    _hard_exit_after_gateway_teardown(0)
_PLATFORMS = [{'key': 'mattermost', 'label': 'Mattermost', 'emoji': '💬', 'token_var': 'MATTERMOST_TOKEN', 'setup_instructions': ['1. In Mattermost: Integrations → Bot Accounts → Add Bot Account', '   (System Console → Integrations → Bot Accounts must be enabled)', '2. Give it a username (e.g. duck-agent) and copy the bot token', '3. Works with any self-hosted Mattermost instance — enter your server URL', '4. To find your user ID: click your avatar (top-left) → Profile', '   Your user ID is displayed there — click it to copy.', "   ⚠ This is NOT your username — it's a 26-character alphanumeric ID.", '5. To get a channel ID: click the channel name → View Info → copy the ID'], 'vars': [{'name': 'MATTERMOST_URL', 'prompt': 'Server URL (e.g. https://mm.example.com)', 'password': False, 'help': 'Your Mattermost server URL. Works with any self-hosted instance.'}, {'name': 'MATTERMOST_TOKEN', 'prompt': 'Bot token', 'password': True, 'help': 'Paste the bot token from step 2 above.'}, {'name': 'MATTERMOST_ALLOWED_USERS', 'prompt': 'Allowed user IDs (comma-separated)', 'password': False, 'is_allowlist': True, 'help': 'Your Mattermost user ID from step 4 above.'}, {'name': 'MATTERMOST_HOME_CHANNEL', 'prompt': 'Home channel ID (for cron/notification delivery, or empty to set later with /set-home)', 'password': False, 'help': 'Channel ID where Duck Agent delivers cron results and notifications.'}, {'name': 'MATTERMOST_REPLY_MODE', 'prompt': "Reply mode — 'off' for flat messages, 'thread' for threaded replies (default: off)", 'password': False, 'help': 'off = flat channel messages, thread = replies nest under your message.'}]}, {'key': 'signal', 'label': 'Signal', 'emoji': '📡', 'token_var': 'SIGNAL_HTTP_URL'}, {'key': 'weixin', 'label': 'Weixin / WeChat', 'emoji': '💬', 'token_var': 'WEIXIN_ACCOUNT_ID'}, {'key': 'bluebubbles', 'label': 'BlueBubbles (iMessage)', 'emoji': '💬', 'token_var': 'BLUEBUBBLES_SERVER_URL', 'setup_instructions': ['1. Install BlueBubbles on a Mac that will act as your iMessage server:', '   https://bluebubbles.app/', '2. Complete the BlueBubbles setup wizard — sign in with your Apple ID', '3. In BlueBubbles Settings → API, note the Server URL and password', '4. The server URL is typically http://<your-mac-ip>:1234', '5. Duck Agent connects via the BlueBubbles REST API and receives', '   incoming messages via a local webhook', '6. To authorize users, use DM pairing: duck-agent pairing generate bluebubbles', '   Share the code — the user sends it via iMessage to get approved'], 'vars': [{'name': 'BLUEBUBBLES_SERVER_URL', 'prompt': 'BlueBubbles server URL (e.g. http://192.168.1.10:1234)', 'password': False, 'help': 'The URL shown in BlueBubbles Settings → API.'}, {'name': 'BLUEBUBBLES_PASSWORD', 'prompt': 'BlueBubbles server password', 'password': True, 'help': 'The password shown in BlueBubbles Settings → API.'}, {'name': 'BLUEBUBBLES_ALLOWED_USERS', 'prompt': 'Pre-authorized phone numbers or iMessage IDs (comma-separated, or leave empty for DM pairing)', 'password': False, 'is_allowlist': True, 'help': 'Optional — pre-authorize specific users. Leave empty to use DM pairing instead (recommended).'}, {'name': 'BLUEBUBBLES_HOME_CHANNEL', 'prompt': 'Home channel (phone number or iMessage ID for cron/notifications, or empty)', 'password': False, 'help': 'Phone number or Apple ID to deliver cron results and notifications to.'}]}, {'key': 'qqbot', 'label': 'QQ Bot', 'emoji': '🐧', 'token_var': 'QQ_APP_ID', 'setup_instructions': ['1. Register a QQ Bot application at q.qq.com', '2. Note your App ID and App Secret from the application page', '3. Enable the required intents (C2C, Group, Guild messages)', '4. Configure sandbox or publish the bot'], 'vars': [{'name': 'QQ_APP_ID', 'prompt': 'QQ Bot App ID', 'password': False, 'help': 'Your QQ Bot App ID from q.qq.com.'}, {'name': 'QQ_CLIENT_SECRET', 'prompt': 'QQ Bot App Secret', 'password': True, 'help': 'Your QQ Bot App Secret from q.qq.com.'}, {'name': 'QQ_ALLOWED_USERS', 'prompt': 'Allowed user OpenIDs (comma-separated, leave empty for open access)', 'password': False, 'is_allowlist': True, 'help': 'Optional — restrict DM access to specific user OpenIDs.'}, {'name': 'QQBOT_HOME_CHANNEL', 'prompt': 'Home channel (user/group OpenID for cron delivery, or empty)', 'password': False, 'help': 'OpenID to deliver cron results and notifications to.'}]}, {'key': 'yuanbao', 'label': 'Yuanbao', 'emoji': '💎', 'token_var': 'YUANBAO_APP_ID', 'setup_instructions': ['1. Download the Yuanbao app from https://yuanbao.tencent.com/', '2. In the app, go to PAI → My Bot and create a new bot', '3. After the bot is created, copy the App ID and App Secret', '4. Enter them below and Duck Agent will connect automatically over WebSocket'], 'vars': [{'name': 'YUANBAO_APP_ID', 'prompt': 'App ID', 'password': False, 'help': 'The App ID from your Yuanbao IM Bot credentials.'}, {'name': 'YUANBAO_APP_SECRET', 'prompt': 'App Secret', 'password': True, 'help': 'The App Secret (used for HMAC signing) from your Yuanbao IM Bot.'}]}]

def _all_platforms() -> list[dict]:
    """Return the full list of platforms for setup menus.

    Combines the built-in ``_PLATFORMS`` with plugin platforms registered via
    ``platform_registry``. Plugins are discovered on first call so bundled
    platforms (like IRC, which auto-load via ``kind: platform``) appear in
    ``duck-agent setup gateway`` without needing the gateway to be running.
    Built-ins keep their dict shape; plugin entries are adapted to the same
    shape with ``_registry_entry`` holding the source.

    Platform-specific gating: some platforms can't be configured on
    every host. Currently:
      - Matrix is hidden on Windows. The [matrix] extra pulls
        ``mautrix[encryption]`` -> ``python-olm``, which has no Windows
        wheel and needs ``make`` + libolm to build from sdist. There's
        no native Windows path that works, so we don't offer it in the
        picker. Users who want Matrix on Windows can run duck-agent under
        WSL.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
    except Exception as e:
        logger.debug('plugin discovery failed during platform enumeration: %s', e)
    platforms = [dict(p) for p in _PLATFORMS]
    if sys.platform == 'win32':
        platforms = [p for p in platforms if p.get('key') != 'matrix']
    by_key = {p['key']: p for p in platforms}
    try:
        from gateway.platform_registry import platform_registry
    except Exception:
        return platforms
    for entry in platform_registry.all_entries():
        if entry.name in by_key:
            continue
        if sys.platform == 'win32' and entry.name == 'matrix':
            continue
        platforms.append({'key': entry.name, 'label': entry.label, 'emoji': entry.emoji, 'token_var': entry.required_env[0] if entry.required_env else '', 'install_hint': entry.install_hint, '_registry_entry': entry})
    return platforms

def _platform_status(platform: dict) -> str:
    """Return a plain-text status string for a platform.

    Returns uncolored text so it can safely be embedded in
    curses menu items (ANSI codes break width calculation).
    """
    entry = platform.get('_registry_entry')
    if entry is not None:
        configured = False
        if entry.is_connected is not None:
            try:
                from gateway.config import PlatformConfig
                synthetic = PlatformConfig(enabled=True)
                configured = bool(entry.is_connected(synthetic))
            except Exception:
                configured = False
        else:
            try:
                configured = bool(entry.check_fn())
            except Exception:
                configured = False
        return 'configured' if configured else 'not configured'
    token_var = platform.get('token_var', '')
    if not token_var:
        return 'not configured'
    val = get_env_value(token_var)
    if token_var == 'WHATSAPP_ENABLED':
        if val and val.lower() == 'true':
            session_file = get_hermes_home() / 'whatsapp' / 'session' / 'creds.json'
            if session_file.exists():
                return 'configured + paired'
            return 'enabled, not paired'
        return 'not configured'
    if platform.get('key') == 'signal':
        account = get_env_value('SIGNAL_ACCOUNT')
        if val and account:
            return 'configured'
        if val or account:
            return 'partially configured'
        return 'not configured'
    if platform.get('key') == 'email':
        pwd = get_env_value('EMAIL_PASSWORD')
        imap = get_env_value('EMAIL_IMAP_HOST')
        smtp = get_env_value('EMAIL_SMTP_HOST')
        if all([val, pwd, imap, smtp]):
            return 'configured'
        if any([val, pwd, imap, smtp]):
            return 'partially configured'
        return 'not configured'
    if platform.get('key') == 'matrix':
        homeserver = get_env_value('MATRIX_HOMESERVER')
        password = get_env_value('MATRIX_PASSWORD')
        if (val or password) and homeserver:
            e2ee = get_env_value('MATRIX_ENCRYPTION')
            suffix = ' + E2EE' if e2ee and e2ee.lower() in {'true', '1', 'yes'} else ''
            return f'configured{suffix}'
        if val or password or homeserver:
            return 'partially configured'
        return 'not configured'
    if platform.get('key') == 'weixin':
        token = get_env_value('WEIXIN_TOKEN')
        if val and token:
            return 'configured'
        if val or token:
            return 'partially configured'
        return 'not configured'
    if val:
        return 'configured'
    return 'not configured'

def _runtime_health_lines() -> list[str]:
    """Summarize the latest persisted gateway runtime health state."""
    try:
        from gateway.status import read_runtime_status, runtime_status_is_stale, runtime_status_pid_is_live
    except Exception:
        return []
    state = read_runtime_status()
    if not state:
        return []
    lines: list[str] = []
    gateway_state = state.get('gateway_state')
    exit_reason = state.get('exit_reason')
    active_agents = state.get('active_agents')
    restart_requested = state.get('restart_requested')
    platforms = state.get('platforms', {}) or {}
    for platform, pdata in platforms.items():
        if pdata.get('state') == 'fatal':
            message = pdata.get('error_message') or 'unknown error'
            lines.append(f'⚠ {platform}: {message}')
    if gateway_state in ('running', 'starting', 'draining') and runtime_status_is_stale(state) and (not runtime_status_pid_is_live(state)):
        lines.append(f"⚠ Stale gateway_state.json: recorded state '{gateway_state}' but the recorded process is gone (likely an ungraceful shutdown)")
        return lines
    if gateway_state == 'startup_failed' and exit_reason:
        lines.append(f'⚠ Last startup issue: {exit_reason}')
    elif gateway_state == 'draining':
        action = 'restart' if restart_requested else 'shutdown'
        from gateway.status import parse_active_agents
        count = parse_active_agents(active_agents)
        lines.append(f'⏳ Gateway draining for {action} ({count} active agent(s))')
    elif gateway_state == 'stopped' and exit_reason:
        lines.append(f'⚠ Last shutdown reason: {exit_reason}')
    return lines

def _set_platform_unauthorized_dm_behavior(platform_key: str, behavior: str) -> None:
    """Persist a platform-specific unauthorized-DM policy in config.yaml."""
    write_platform_config_field(platform_key, 'unauthorized_dm_behavior', behavior, raw=True)

def _setup_standard_platform(platform: dict):
    """Interactive setup for Telegram, Discord, or Slack."""
    from hermes_cli.setup_hidden_env import is_setup_hidden_env as _is_setup_hidden_env
    emoji = platform['emoji']
    label = platform['label']
    token_var = platform['token_var']
    print()
    print(color(f'  ─── {emoji} {label} Setup ───', Colors.CYAN))
    instructions = platform.get('setup_instructions')
    if instructions:
        print()
        for line in instructions:
            print_info(f'  {line}')
    existing_token = get_env_value(token_var)
    if existing_token:
        print()
        print_success(f'{label} is already configured.')
        if not prompt_yes_no(f'  Reconfigure {label}?', False):
            return
    auto_token_saved = False
    auto_owner_user_id = None
    if platform.get('key') == 'telegram':
        print()
        print_info('  Telegram can be configured automatically with a managed bot:')
        print_info('  [1] Automatic (scan QR → confirm in Telegram → done)')
        print_info('  [2] Manual BotFather token')
        choice = prompt('  Choice [1/2]', default='1')
        if choice.strip() == '1':
            try:
                from hermes_cli.telegram_managed_bot import auto_setup_telegram_bot_result, is_valid_telegram_bot_token
            except ImportError:
                print_warning('  Automatic setup is unavailable in this install.')
            else:
                result = auto_setup_telegram_bot_result()
                if result and is_valid_telegram_bot_token(result.token):
                    save_env_value(token_var, result.token)
                    print_success('  Saved TELEGRAM_BOT_TOKEN')
                    auto_token_saved = True
                    auto_owner_user_id = result.owner_user_id
                else:
                    if result:
                        print_warning('  Automatic setup returned an invalid Telegram token.')
                    print()
                    print_info('  Falling back to manual setup...')
    allowed_val_set = None
    required_names = {token_var}
    setup_vars = [v for v in platform['vars'] if v['name'] in required_names or v.get('is_allowlist') or (not _is_setup_hidden_env(v['name']))]
    for var in setup_vars:
        print()
        print_info(f"  {var['help']}")
        existing = get_env_value(var['name'])
        if existing and var['name'] != token_var:
            print_info(f'  Current: {existing}')
        if auto_token_saved and var['name'] == token_var:
            print_info('  Token saved by automatic setup.')
            continue
        if var.get('is_allowlist'):
            if 'TELEGRAM' in var['name'] and auto_owner_user_id:
                detected_id = str(auto_owner_user_id)
                print_success(f'  Detected your Telegram user ID: {detected_id}')
                if prompt_yes_no('  Allow this Telegram account to use the bot?', True):
                    extra = prompt('  Additional allowed user IDs (comma-separated, optional)', password=False)
                    ids = [detected_id]
                    for uid in extra.replace(' ', '').split(','):
                        if uid and uid not in ids:
                            ids.append(uid)
                    cleaned = ','.join(ids)
                    save_env_value(var['name'], cleaned)
                    print_success('  Saved — only these users can interact with the bot.')
                    allowed_val_set = cleaned
                    continue
            print_info('  The gateway DENIES all users by default for security.')
            print_info('  Enter user IDs to create an allowlist, or leave empty')
            print_info("  and you'll be asked about open access next.")
            value = prompt(f"  {var['prompt']}", password=False)
            if value:
                cleaned = value.replace(' ', '')
                if 'DISCORD' in var['name']:
                    parts = []
                    for uid in cleaned.split(','):
                        uid = uid.strip()
                        if uid.startswith('<@') and uid.endswith('>'):
                            uid = uid.lstrip('<@!').rstrip('>')
                        if uid.lower().startswith('user:'):
                            uid = uid[5:]
                        if uid:
                            parts.append(uid)
                    cleaned = ','.join(parts)
                save_env_value(var['name'], cleaned)
                print_success('  Saved — only these users can interact with the bot.')
                allowed_val_set = cleaned
            else:
                print()
                is_email = platform.get('key') == 'email'
                if is_email:
                    access_choices = ['Enable open access (any email sender can message the bot)', 'Use DM pairing (unknown email senders receive a pairing code)', 'Keep unknown senders silent']
                    default_access_idx = 2
                else:
                    access_choices = ['Enable open access (anyone can message the bot)', "Use DM pairing (unknown users request access, you approve with 'duck-agent pairing approve')", 'Skip for now (bot will deny all users until configured)']
                    default_access_idx = 1
                access_idx = prompt_choice('  How should unauthorized users be handled?', access_choices, default_access_idx)
                if access_idx == 0:
                    if is_email:
                        save_env_value('EMAIL_ALLOW_ALL_USERS', 'true')
                    else:
                        save_env_value('GATEWAY_ALLOW_ALL_USERS', 'true')
                    print_warning('  Open access enabled — anyone can use your bot!')
                elif access_idx == 1:
                    if is_email:
                        _set_platform_unauthorized_dm_behavior('email', 'pair')
                    print_success('  DM pairing mode — users will receive a code to request access.')
                    print_info('  Approve with: duck-agent pairing approve <platform> <code>')
                elif is_email:
                    print_success('  Unknown email senders will be ignored.')
                else:
                    print_info("  Skipped — configure later with 'duck-agent gateway setup'")
            continue
        value = prompt(f"  {var['prompt']}", password=var.get('password', False))
        if value:
            save_env_value(var['name'], value)
            print_success(f"  Saved {var['name']}")
        elif var['name'] == token_var:
            print_warning(f"  Skipped — {label} won't work without this.")
            return
        else:
            print_info('  Skipped (can configure later)')
    home_var = f'{label.upper()}_HOME_CHANNEL'
    home_val = get_env_value(home_var)
    if allowed_val_set and (not home_val) and (label == 'Telegram'):
        first_id = allowed_val_set.split(',')[0].strip()
        if first_id and prompt_yes_no(f'  Use your user ID ({first_id}) as the home channel?', True):
            save_env_value(home_var, first_id)
            print_success(f'  Home channel set to {first_id}')
    print()
    print_success(f'{emoji} {label} configured!')

def _is_service_installed() -> bool:
    """Check if the gateway is installed as a system service."""
    if supports_systemd_services():
        return get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()
    elif is_macos():
        return get_launchd_plist_path().exists()
    elif is_windows():
        from hermes_cli import gateway_windows
        return gateway_windows.is_installed()
    return False

def _is_service_running() -> bool:
    """Check if the gateway service is currently running."""
    if supports_systemd_services():
        user_unit_exists = get_systemd_unit_path(system=False).exists()
        system_unit_exists = get_systemd_unit_path(system=True).exists()
        if user_unit_exists:
            try:
                result = _run_systemctl(['is-active', get_service_name()], system=False, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                if result.stdout.strip() == 'active':
                    return True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        if system_unit_exists:
            try:
                result = _run_systemctl(['is-active', get_service_name()], system=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                if result.stdout.strip() == 'active':
                    return True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        return False
    elif is_macos() and get_launchd_plist_path().exists():
        try:
            result = subprocess.run(['launchctl', 'list', get_launchd_label()], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
    elif is_windows():
        from hermes_cli import gateway_windows
        if gateway_windows.is_installed():
            return len(find_gateway_pids()) > 0
    return len(find_gateway_pids()) > 0

def _setup_weixin():
    """Interactive setup for Weixin / WeChat personal accounts."""
    print()
    print(color('  ─── 💬 Weixin / WeChat Setup ───', Colors.CYAN))
    print()
    print_info('  1. Duck Agent will open Tencent iLink QR login in this terminal.')
    print_info('  2. Use WeChat to scan and confirm the QR code.')
    print_info('  3. Duck Agent will store the returned account_id/token in ~/.duck-agent/.env.')
    print_info('  4. This adapter supports native text, image, video, and document delivery.')
    existing_account = get_env_value('WEIXIN_ACCOUNT_ID')
    existing_token = get_env_value('WEIXIN_TOKEN')
    if existing_account and existing_token:
        print()
        print_success('Weixin is already configured.')
        if not prompt_yes_no('  Reconfigure Weixin?', False):
            return
    try:
        from gateway.platforms.weixin import check_weixin_requirements, qr_login
    except Exception as exc:
        print_error(f'  Weixin adapter import failed: {exc}')
        print_info('  Install gateway dependencies first, then retry.')
        return
    if not check_weixin_requirements():
        print_error('  Missing dependencies: Weixin needs aiohttp and cryptography.')
        print_info('  Install them, then rerun `duck-agent gateway setup`.')
        return
    print()
    if not prompt_yes_no('  Start QR login now?', True):
        print_info('  Cancelled.')
        return
    import asyncio
    try:
        credentials = asyncio.run(qr_login(str(get_hermes_home())))
    except KeyboardInterrupt:
        print()
        print_warning('  Weixin setup cancelled.')
        return
    except Exception as exc:
        print_error(f'  QR login failed: {exc}')
        return
    if not credentials:
        print_warning('  QR login did not complete.')
        return
    account_id = credentials.get('account_id', '')
    token = credentials.get('token', '')
    base_url = credentials.get('base_url', '')
    user_id = credentials.get('user_id', '')
    save_env_value('WEIXIN_ACCOUNT_ID', account_id)
    save_env_value('WEIXIN_TOKEN', token)
    if base_url:
        save_env_value('WEIXIN_BASE_URL', base_url)
    save_env_value('WEIXIN_CDN_BASE_URL', get_env_value('WEIXIN_CDN_BASE_URL') or 'https://novac2c.cdn.weixin.qq.com/c2c')
    print()
    access_choices = ['Use DM pairing approval (recommended)', 'Allow all direct messages', 'Only allow listed user IDs', 'Disable direct messages']
    access_idx = prompt_choice('  How should direct messages be authorized?', access_choices, 0)
    if access_idx == 0:
        save_env_value('WEIXIN_DM_POLICY', 'pairing')
        save_env_value('WEIXIN_ALLOW_ALL_USERS', 'false')
        save_env_value('WEIXIN_ALLOWED_USERS', '')
        print_success('  DM pairing enabled.')
        print_info('  Unknown DM users can request access and you approve them with `duck-agent pairing approve`.')
    elif access_idx == 1:
        save_env_value('WEIXIN_DM_POLICY', 'open')
        save_env_value('WEIXIN_ALLOW_ALL_USERS', 'true')
        save_env_value('WEIXIN_ALLOWED_USERS', '')
        print_warning('  Open DM access enabled for Weixin.')
    elif access_idx == 2:
        default_allow = user_id or ''
        allowlist = prompt('  Allowed Weixin user IDs (comma-separated)', default_allow, password=False).replace(' ', '')
        save_env_value('WEIXIN_DM_POLICY', 'allowlist')
        save_env_value('WEIXIN_ALLOW_ALL_USERS', 'false')
        save_env_value('WEIXIN_ALLOWED_USERS', allowlist)
        print_success('  Weixin allowlist saved.')
    else:
        save_env_value('WEIXIN_DM_POLICY', 'disabled')
        save_env_value('WEIXIN_ALLOW_ALL_USERS', 'false')
        save_env_value('WEIXIN_ALLOWED_USERS', '')
        print_warning('  Direct messages disabled.')
    print()
    print_info('  Note: QR login connects an iLink bot identity (e.g. ...@im.bot), not a')
    print_info('  scriptable personal WeChat account. Ordinary WeChat groups typically cannot')
    print_info('  invite an @im.bot identity, and iLink does not deliver ordinary-group events')
    print_info('  to most bot accounts. The settings below only apply when iLink actually')
    print_info('  delivers group events for your account type — otherwise DM remains the only')
    print_info('  working channel regardless of this choice.')
    group_choices = ['Disable group chats (recommended)', 'Allow all group chats', 'Only allow listed group chat IDs']
    group_idx = prompt_choice('  How should group chats be handled?', group_choices, 0)
    if group_idx == 0:
        save_env_value('WEIXIN_GROUP_POLICY', 'disabled')
        save_env_value('WEIXIN_GROUP_ALLOWED_USERS', '')
        print_info('  Group chats disabled.')
    elif group_idx == 1:
        save_env_value('WEIXIN_GROUP_POLICY', 'open')
        save_env_value('WEIXIN_GROUP_ALLOWED_USERS', '')
        print_warning('  All group chats enabled (only takes effect if iLink delivers group events).')
    else:
        allow_groups = prompt('  Allowed group chat IDs (comma-separated, not member user IDs)', '', password=False).replace(' ', '')
        save_env_value('WEIXIN_GROUP_POLICY', 'allowlist')
        save_env_value('WEIXIN_GROUP_ALLOWED_USERS', allow_groups)
        print_success('  Group allowlist saved (only takes effect if iLink delivers group events).')
    if user_id:
        print()
        if prompt_yes_no(f'  Use your Weixin user ID ({user_id}) as the home channel?', True):
            save_env_value('WEIXIN_HOME_CHANNEL', user_id)
            print_success(f'  Home channel set to {user_id}')
    print()
    print_success('Weixin configured!')
    print_info(f'  Account ID: {account_id}')
    if user_id:
        print_info(f'  User ID: {user_id}')

def _setup_qqbot():
    """Interactive setup for QQ Bot — scan-to-configure or manual credentials."""
    print()
    print(color('  ─── 🐧 QQ Bot Setup ───', Colors.CYAN))
    existing_app_id = get_env_value('QQ_APP_ID')
    existing_secret = get_env_value('QQ_CLIENT_SECRET')
    if existing_app_id and existing_secret:
        print()
        print_success('QQ Bot is already configured.')
        if not prompt_yes_no('  Reconfigure QQ Bot?', False):
            return
    print()
    method_choices = ['Scan QR code to add bot automatically (recommended)', 'Enter existing App ID and App Secret manually']
    method_idx = prompt_choice('  How would you like to set up QQ Bot?', method_choices, 0)
    credentials = None
    if method_idx == 0:
        try:
            from gateway.platforms.qqbot import qr_register
            credentials = qr_register()
        except KeyboardInterrupt:
            print()
            print_warning('  QQ Bot setup cancelled.')
            return
        if not credentials:
            print_info('  QR setup did not complete. Continuing with manual input.')
    if not credentials:
        print()
        print_info('  Go to https://q.qq.com to register a QQ Bot application.')
        print_info('  Note your App ID and App Secret from the application page.')
        print()
        app_id = prompt('  App ID', password=False)
        if not app_id:
            print_warning("  Skipped — QQ Bot won't work without an App ID.")
            return
        app_secret = prompt('  App Secret', password=True)
        if not app_secret:
            print_warning("  Skipped — QQ Bot won't work without an App Secret.")
            return
        credentials = {'app_id': app_id.strip(), 'client_secret': app_secret.strip(), 'user_openid': ''}
    save_env_value('QQ_APP_ID', credentials['app_id'])
    save_env_value('QQ_CLIENT_SECRET', credentials['client_secret'])
    user_openid = credentials.get('user_openid', '')
    print()
    access_choices = ['Use DM pairing approval (recommended)', 'Allow all direct messages', 'Only allow listed user OpenIDs']
    access_idx = prompt_choice('  How should direct messages be authorized?', access_choices, 0)
    if access_idx == 0:
        save_env_value('QQ_ALLOW_ALL_USERS', 'false')
        if user_openid:
            print()
            if prompt_yes_no(f'  Add yourself ({user_openid}) to the allow list?', True):
                save_env_value('QQ_ALLOWED_USERS', user_openid)
                print_success(f'  Allow list set to {user_openid}')
            else:
                save_env_value('QQ_ALLOWED_USERS', '')
        else:
            save_env_value('QQ_ALLOWED_USERS', '')
        print_success('  DM pairing enabled.')
        print_info('  Unknown users can request access; approve with `duck-agent pairing approve`.')
    elif access_idx == 1:
        save_env_value('QQ_ALLOW_ALL_USERS', 'true')
        save_env_value('QQ_ALLOWED_USERS', '')
        print_warning('  Open DM access enabled for QQ Bot.')
    else:
        default_allow = user_openid or ''
        allowlist = prompt('  Allowed user OpenIDs (comma-separated)', default_allow, password=False).replace(' ', '')
        save_env_value('QQ_ALLOW_ALL_USERS', 'false')
        save_env_value('QQ_ALLOWED_USERS', allowlist)
        print_success('  Allowlist saved.')
    if user_openid:
        print()
        if prompt_yes_no(f'  Use your QQ user ID ({user_openid}) as the home channel?', True):
            save_env_value('QQBOT_HOME_CHANNEL', user_openid)
            print_success(f'  Home channel set to {user_openid}')
    else:
        print()
        home_channel = prompt('  Home channel OpenID (for cron/notifications, or empty)', password=False)
        if home_channel:
            save_env_value('QQBOT_HOME_CHANNEL', home_channel.strip())
            print_success(f'  Home channel set to {home_channel.strip()}')
    print()
    print_success('🐧 QQ Bot configured!')
    print_info(f"  App ID: {credentials['app_id']}")

def _setup_signal():
    """Interactive setup for Signal messenger."""
    import shutil
    print()
    print(color('  ─── 📡 Signal Setup ───', Colors.CYAN))
    existing_url = get_env_value('SIGNAL_HTTP_URL')
    existing_account = get_env_value('SIGNAL_ACCOUNT')
    if existing_url and existing_account:
        print()
        print_success('Signal is already configured.')
        if not prompt_yes_no('  Reconfigure Signal?', False):
            return
    print()
    if shutil.which('signal-cli'):
        print_success('signal-cli found on PATH.')
    else:
        print_warning('signal-cli not found on PATH.')
        print_info('  Signal requires signal-cli running as an HTTP daemon.')
        print_info('  Install options:')
        print_info('    Linux:  download from https://github.com/AsamK/signal-cli/releases')
        print_info('    macOS:  brew install signal-cli')
        print_info('    Docker: bbernhard/signal-cli-rest-api')
        print()
        print_info('  After installing, link your account and start the daemon:')
        print_info('    signal-cli link -n "HermesAgent"')
        print_info('    signal-cli --account +YOURNUMBER daemon --http 127.0.0.1:8080')
        print()
    print()
    print_info('  Enter the URL where signal-cli HTTP daemon is running.')
    default_url = existing_url or 'http://127.0.0.1:8080'
    try:
        url = input(f'  HTTP URL [{default_url}]: ').strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print('\n  Setup cancelled.')
        return
    print_info('  Testing connection...')
    try:
        import httpx
        resp = httpx.get(f"{url.rstrip('/')}/api/v1/check", timeout=10.0)
        if resp.status_code == 200:
            print_success('  signal-cli daemon is reachable!')
        else:
            print_warning(f'  signal-cli responded with status {resp.status_code}.')
            if not prompt_yes_no('  Continue anyway?', False):
                return
    except Exception as e:
        print_warning(f'  Could not reach signal-cli at {url}: {e}')
        if not prompt_yes_no('  Save this URL anyway? (you can start signal-cli later)', True):
            return
    save_env_value('SIGNAL_HTTP_URL', url)
    print()
    print_info('  Enter your Signal account phone number in E.164 format.')
    print_info('  Example: +15551234567')
    default_account = existing_account or ''
    try:
        account = input(f"  Account number{(f' [{default_account}]' if default_account else '')}: ").strip()
        if not account:
            account = default_account
    except (EOFError, KeyboardInterrupt):
        print('\n  Setup cancelled.')
        return
    if not account:
        print_error('  Account number is required.')
        return
    save_env_value('SIGNAL_ACCOUNT', account)
    print()
    print_info('  The gateway DENIES all users by default for security.')
    print_info('  Enter phone numbers or UUIDs of allowed users (comma-separated).')
    existing_allowed = get_env_value('SIGNAL_ALLOWED_USERS') or ''
    default_allowed = existing_allowed or account
    try:
        allowed = input(f'  Allowed users [{default_allowed}]: ').strip() or default_allowed
    except (EOFError, KeyboardInterrupt):
        print('\n  Setup cancelled.')
        return
    save_env_value('SIGNAL_ALLOWED_USERS', allowed)
    print()
    if prompt_yes_no('  Enable group messaging? (disabled by default for security)', False):
        print()
        print_info('  Enter group IDs to allow, or * for all groups.')
        existing_groups = get_env_value('SIGNAL_GROUP_ALLOWED_USERS') or ''
        try:
            groups = input(f"  Group IDs [{existing_groups or '*'}]: ").strip() or existing_groups or '*'
        except (EOFError, KeyboardInterrupt):
            print('\n  Setup cancelled.')
            return
        save_env_value('SIGNAL_GROUP_ALLOWED_USERS', groups)
    print()
    print_success('Signal configured!')
    print_info(f'  URL: {url}')
    print_info(f'  Account: {account}')
    print_info('  DM auth: via SIGNAL_ALLOWED_USERS + DM pairing')
    print_info(f"  Groups: {('enabled' if get_env_value('SIGNAL_GROUP_ALLOWED_USERS') else 'disabled')}")

def _builtin_setup_fn(key: str):
    """Resolve the interactive setup function for a built-in platform key.

    Late-bound to avoid a circular import with ``hermes_cli.setup`` (which
    imports from this module for the remaining bespoke flows).
    """
    from hermes_cli import setup as _s
    return {'bluebubbles': _s._setup_bluebubbles, 'webhooks': _s._setup_webhooks, 'signal': _setup_signal, 'weixin': _setup_weixin, 'qqbot': _setup_qqbot}.get(key)

def _configure_platform(platform: dict) -> None:
    """Run the interactive setup flow for a single platform.

    Dispatch order:
      1. Plugin-provided ``setup_fn`` on the registry entry.
      2. Built-in setup function matched by platform key.
      3. ``_setup_standard_platform`` when the entry has a ``vars`` schema.
      4. Env-var hint fallback for plugins that offer no setup helper.

    Bundled platform plugins (e.g. IRC) auto-load, so no plugin enable step
    is needed here. User-installed platform plugins under ~/.duck-agent/plugins/
    must already be in ``plugins.enabled`` before they appear in this menu.
    """
    entry = platform.get('_registry_entry')
    if entry is not None and entry.setup_fn is not None:
        entry.setup_fn()
        return
    fn = _builtin_setup_fn(platform['key'])
    if fn is not None:
        fn()
        return
    if platform.get('vars'):
        _setup_standard_platform(platform)
        return
    label = platform.get('label', platform['key'])
    emoji = platform.get('emoji', '🔌')
    print()
    print(color(f'  ─── {emoji} {label} Setup ───', Colors.CYAN))
    required = entry.required_env if entry else []
    if required:
        print_info(f"  Set these env vars in ~/.duck-agent/.env: {', '.join(required)}")
    else:
        print_info(f"  Configure {label} in config.yaml under gateway.platforms.{platform['key']}")
    if platform.get('install_hint'):
        print_info(f"  {platform['install_hint']}")

def gateway_setup():
    """Interactive setup for messaging platforms + gateway service."""
    if is_managed():
        managed_error('run gateway setup')
        return
    print()
    print(color('┌─────────────────────────────────────────────────────────┐', Colors.MAGENTA))
    print(color('│             ⚕ Gateway Setup                            │', Colors.MAGENTA))
    print(color('├─────────────────────────────────────────────────────────┤', Colors.MAGENTA))
    print(color('│  Configure messaging platforms and the gateway service. │', Colors.MAGENTA))
    print(color('│  Press Ctrl+C at any time to exit.                     │', Colors.MAGENTA))
    print(color('└─────────────────────────────────────────────────────────┘', Colors.MAGENTA))
    print()
    service_installed = _is_service_installed()
    service_running = _is_service_running()
    if supports_systemd_services() and has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()
    if supports_systemd_services() and has_legacy_hermes_units():
        print_legacy_unit_warning()
        print()
    if service_installed and service_running:
        print_success('Gateway service is installed and running.')
    elif service_installed:
        print_warning('Gateway service is installed but not running.')
        if supports_systemd_services() and _system_scope_wizard_would_need_root():
            _print_system_scope_remediation('start')
        elif prompt_yes_no('  Start it now?', True):
            try:
                if supports_systemd_services():
                    systemd_start()
                elif is_macos():
                    launchd_start()
            except UserSystemdUnavailableError as e:
                print_error('  Failed to start — user systemd not reachable:')
                for line in str(e).splitlines():
                    print(f'  {line}')
            except SystemScopeRequiresRootError as e:
                print_error(f'  Failed to start: {e}')
                _print_system_scope_remediation('start')
            except subprocess.CalledProcessError as e:
                print_error(f'  Failed to start: {e}')
    else:
        print_info('Gateway service is not installed yet.')
        print_info("You'll be offered to install it after configuring platforms.")
    while True:
        print()
        print_header('Messaging Platforms')
        platforms = _all_platforms()
        menu_items = [f"{p['emoji']} {p['label']}  ({_platform_status(p)})" for p in platforms]
        menu_items.append('Done')
        choice = prompt_choice('Select a platform to configure:', menu_items, len(menu_items) - 1)
        if choice == len(platforms):
            break
        _configure_platform(platforms[choice])

    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (s == 'not configured' or s.startswith('partially') or s.startswith('plugin disabled'))
    any_configured = any((_is_progress(_platform_status(p)) for p in _all_platforms()))
    if any_configured:
        print()
        print(color('─' * 58, Colors.DIM))
        service_installed = _is_service_installed()
        service_running = _is_service_running()
        if service_running:
            if supports_systemd_services() and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation('restart')
            elif prompt_yes_no('  Restart the gateway to pick up changes?', True):
                try:
                    if supports_systemd_services():
                        systemd_restart()
                    elif is_macos():
                        launchd_restart()
                    elif is_windows():
                        from hermes_cli import gateway_windows
                        gateway_windows.restart()
                    else:
                        stop_profile_gateway()
                        print_info('Start manually: duck-agent gateway')
                except UserSystemdUnavailableError as e:
                    print_error('  Restart failed — user systemd not reachable:')
                    for line in str(e).splitlines():
                        print(f'  {line}')
                except SystemScopeRequiresRootError as e:
                    print_error(f'  Restart failed: {e}')
                    _print_system_scope_remediation('restart')
                except subprocess.CalledProcessError as e:
                    print_error(f'  Restart failed: {e}')
        elif service_installed:
            if supports_systemd_services() and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation('start')
            elif prompt_yes_no('  Start the gateway service?', True):
                try:
                    if supports_systemd_services():
                        systemd_start()
                    elif is_macos():
                        launchd_start()
                    elif is_windows():
                        from hermes_cli import gateway_windows
                        gateway_windows.start()
                except UserSystemdUnavailableError as e:
                    print_error('  Start failed — user systemd not reachable:')
                    for line in str(e).splitlines():
                        print(f'  {line}')
                except SystemScopeRequiresRootError as e:
                    print_error(f'  Start failed: {e}')
                    _print_system_scope_remediation('start')
                except subprocess.CalledProcessError as e:
                    print_error(f'  Start failed: {e}')
        else:
            print()
            if supports_systemd_services() or is_macos() or is_windows():
                if supports_systemd_services():
                    platform_name = 'systemd'
                elif is_macos():
                    platform_name = 'launchd'
                else:
                    platform_name = 'Scheduled Task'
                wsl_note = ' (note: services may not survive WSL restarts)' if is_wsl() else ''
                start_now = prompt_yes_no('  Start the gateway now?', True)
                start_on_login = prompt_yes_no(f'  Start the gateway automatically on login/boot as a {platform_name} service?{wsl_note}', True)
                if start_now or start_on_login:
                    try:
                        installed_scope = None
                        did_install = False
                        if supports_systemd_services():
                            installed_scope, did_install = install_linux_gateway_from_setup(force=False, enable_on_startup=start_on_login)
                        elif is_macos():
                            launchd_install(force=False)
                            did_install = True
                        else:
                            from hermes_cli import gateway_windows
                            gateway_windows.install(force=False)
                            did_install = True
                        print()
                        if did_install and start_now:
                            try:
                                if supports_systemd_services():
                                    systemd_start(system=installed_scope == 'system')
                                elif is_macos():
                                    launchd_start()
                                elif is_windows():
                                    from hermes_cli import gateway_windows
                                    gateway_windows.start()
                            except UserSystemdUnavailableError as e:
                                print_error('  Start failed — user systemd not reachable:')
                                for line in str(e).splitlines():
                                    print(f'  {line}')
                            except subprocess.CalledProcessError as e:
                                print_error(f'  Start failed: {e}')
                    except subprocess.CalledProcessError as e:
                        print_error(f'  Install failed: {e}')
                        print_info('  You can try manually: duck-agent gateway install')
                else:
                    print_info('  Skipped start and auto-start setup.')
                    print_info('  You can install later: duck-agent gateway install')
                    if supports_systemd_services():
                        print_info('  Or as a boot-time service: sudo duck-agent gateway install --system')
                    print_info('  Or run in foreground:  duck-agent gateway run')
            elif is_wsl():
                print_info('  WSL detected but systemd is not running.')
                print_info('  Run in foreground: duck-agent gateway run')
                print_info("  For persistence:   tmux new -s duck-agent 'duck-agent gateway run'")
                print_info("  To enable systemd: add systemd=true to /etc/wsl.conf, then 'wsl --shutdown'")
            elif is_termux():
                from hermes_constants import display_hermes_home as _dhh
                print_info('  Termux does not use systemd/launchd services.')
                print_info('  Run in foreground: duck-agent gateway run')
                print_info(f'  Or start it manually in the background (best effort): nohup duck-agent gateway run >{_dhh()}/logs/gateway.log 2>&1 &')
            else:
                print_info('  Service install not supported on this platform.')
                print_info('  Run in foreground: duck-agent gateway run')
    else:
        print()
        print_info("No platforms configured. Run 'duck-agent gateway setup' when ready.")
    print()

def _dispatch_via_service_manager_if_s6(action: str, profile: str | None=None) -> bool:
    """If we're in a container with s6, dispatch gateway lifecycle via s6.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    ``action`` is one of ``start`` / ``stop`` / ``restart``. The
    profile defaults to the current one (resolved via ``_profile_arg``).
    The s6 service slot was created either by the Phase 4 profile-create
    hook or by the container-boot reconciler (cont-init.d/02-…). If it
    doesn't exist or s6 returns an error, the named errors from
    :mod:`hermes_cli.service_manager` are caught and surfaced as
    actionable CLI messages (no raw ``CalledProcessError`` traceback).
    """
    from hermes_cli.service_manager import GatewayNotRegisteredError, S6CommandError, detect_service_manager, get_service_manager
    if detect_service_manager() != 's6':
        return False
    if profile is None:
        profile = _profile_suffix() or 'default'
    mgr = get_service_manager()
    service_name = f'gateway-{profile}'
    try:
        if action == 'start':
            mgr.start(service_name)
        elif action == 'stop':
            mgr.stop(service_name)
        elif action == 'restart':
            mgr.restart(service_name)
        else:
            return False
    except GatewayNotRegisteredError as exc:
        print(f'✗ {exc}')
        sys.exit(1)
    except S6CommandError as exc:
        print(f'✗ {exc}')
        sys.exit(1)
    return True

def _dispatch_all_via_service_manager_if_s6(action: str) -> bool:
    """Inside a container with s6, dispatch ``--all`` lifecycle to every
    registered profile gateway.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    Without this, ``duck-agent gateway stop --all`` and ``... restart --all``
    fall through to ``kill_gateway_processes(all_profiles=True)``, which
    just ``pkill``s every gateway process. s6-supervise observes the
    crash and restarts each one ~1s later — so ``--all`` ends up
    *kicking* every gateway instead of *stopping* it. By iterating
    ``list_profile_gateways()`` and sending the lifecycle command
    through the service manager we get the intended semantics (s6's
    ``want up``/``want down`` flips correctly so supervise stays down
    after a stop).

    ``action`` is one of ``stop`` / ``restart`` (``start --all`` isn't
    a supported CLI surface).
    """
    from hermes_cli.service_manager import detect_service_manager, get_service_manager
    if detect_service_manager() != 's6':
        return False
    if action not in ('stop', 'restart'):
        return False
    mgr = get_service_manager()
    profiles = mgr.list_profile_gateways()
    if not profiles:
        print('✗ No profile gateways registered under s6')
        return True
    fn = mgr.stop if action == 'stop' else mgr.restart
    errors: list[tuple[str, Exception]] = []
    for profile in profiles:
        service_name = f'gateway-{profile}'
        try:
            fn(service_name)
        except Exception as exc:
            errors.append((profile, exc))
    succeeded = len(profiles) - len(errors)
    verb = 'stopped' if action == 'stop' else 'restarted'
    if succeeded:
        print(f'✓ {verb.capitalize()} {succeeded} profile gateway(s) under s6')
    for profile, exc in errors:
        print(f'✗ Could not {action} gateway-{profile}: {exc}')
    return True

def gateway_command(args):
    """Handle gateway subcommands."""
    try:
        return _gateway_command_inner(args)
    except UserSystemdUnavailableError as e:
        print_error('User systemd not reachable:')
        for line in str(e).splitlines():
            print(f'  {line}')
        sys.exit(1)
    except SystemScopeRequiresRootError as e:
        print(str(e))
        sys.exit(1)

def _maybe_redirect_run_to_s6_supervision(args) -> bool:
    """Inside an s6 container, redirect bare ``gateway run`` to the
    supervised path.

    Background. Before the s6 image landed, ``docker run <image> gateway
    run`` was the standard way to start a containerized gateway: the
    gateway was the container's main process, tini reaped zombies, and
    container exit code == gateway exit code. With s6-overlay as PID 1,
    we'd much rather have the gateway run as a supervised s6 longrun
    (auto-restart on crash, dashboard supervised alongside, multiple
    profile gateways under the same /init). This redirect upgrades the
    old invocation transparently — the user gets the new behavior
    without changing their docker run command.

    Three gates make this a no-op outside the intended scope:

      1. ``_dispatch_via_service_manager_if_s6`` returns False unless
         we're in a container with s6 as PID 1. Host runs of
         ``duck-agent gateway run`` are unaffected.
      2. ``HERMES_S6_SUPERVISED_CHILD`` is exported by
         ``S6ServiceManager._render_run_script`` for the supervised
         process itself — i.e. when s6-supervise execs ``duck-agent gateway
         run --replace`` as a longrun, this guard short-circuits the
         redirect so the supervised gateway actually runs in
         foreground (otherwise we'd recurse: run → start → run → start
         → ...).
      3. ``--no-supervise`` (or ``HERMES_GATEWAY_NO_SUPERVISE=1``) opts
         out for users who genuinely want pre-s6 semantics — CI smoke
         tests, debugging the foreground startup path, etc.

    Returns True iff dispatched (caller should ``return``).
    """
    no_supervise = getattr(args, 'no_supervise', False) or os.environ.get('HERMES_GATEWAY_NO_SUPERVISE', '').lower() in ('1', 'true', 'yes')
    if no_supervise:
        return False
    if os.environ.get('HERMES_S6_SUPERVISED_CHILD'):
        return False
    if not _dispatch_via_service_manager_if_s6('start'):
        return False
    print('→ gateway is now running under s6 supervision (auto-restart on crash,\n  dashboard supervised alongside if HERMES_DASHBOARD is set).\n  This is the recommended setup for the s6 container image — the\n  gateway will keep running even if it crashes.\n  Use `--no-supervise` (or HERMES_GATEWAY_NO_SUPERVISE=1) to opt out\n  and get the pre-s6 foreground behavior instead.', file=sys.stderr, flush=True)
    try:
        os.execvp('sleep', ['sleep', 'infinity'])
    except OSError:
        print('→ `sleep` is unavailable; keeping the s6 CMD process alive in-process until the container is stopped.', file=sys.stderr, flush=True)
        _block_until_terminated()
    return True

def _block_until_terminated() -> None:
    """Keep the s6 CMD process alive until the container is stopped.

    Fallback heartbeat for when ``os.execvp("sleep", ...)`` can't run
    (``sleep`` missing from PATH — issue #36208). Installs a SIGTERM
    handler that exits with the conventional 128+signum code so
    ``docker stop`` produces a clean, expected exit, then blocks on
    ``signal.pause()``. Falls back to ``threading.Event().wait()`` on
    platforms without ``signal.pause()`` (e.g. Windows) — although this
    path only runs inside the s6 Linux container image, the fallback
    keeps the helper safe to import and unit-test anywhere.
    """
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    pause = getattr(signal, 'pause', None)
    if pause is not None:
        while True:
            pause()
    else:
        import threading
        threading.Event().wait()

def _gateway_command_inner(args):
    subcmd = getattr(args, 'gateway_command', None)
    if subcmd is None or subcmd == 'run':
        if _maybe_redirect_run_to_s6_supervision(args):
            return
        if getattr(args, 'external_supervisor', False):
            os.environ[EXTERNAL_GATEWAY_SUPERVISOR_ENV] = '1'
        verbose = getattr(args, 'verbose', 0)
        quiet = getattr(args, 'quiet', False)
        replace = getattr(args, 'replace', False)
        force = getattr(args, 'force', False)
        run_gateway(verbose, quiet=quiet, replace=replace, force=force)
        return
    if subcmd == 'setup':
        gateway_setup()
        return
    if subcmd == 'install':
        if is_managed():
            managed_error('install gateway service (managed by NixOS)')
            return
        force = getattr(args, 'force', False)
        system = getattr(args, 'system', False)
        run_as_user = getattr(args, 'run_as_user', None)
        if is_termux():
            print('Gateway service installation is not supported on Termux.')
            print('Run manually: duck-agent gateway')
            sys.exit(1)
        if supports_systemd_services():
            if is_wsl():
                print_warning('WSL detected — systemd services may not survive WSL restarts.')
                print_info('  Consider running in foreground instead: duck-agent gateway run')
                print_info("  Or use tmux/screen for persistence: tmux new -s duck-agent 'duck-agent gateway run'")
                print()
            non_interactive = not (hasattr(sys.stdin, 'isatty') and sys.stdin.isatty())
            _sn = getattr(args, 'start_now', None)
            if _sn is not None:
                start_now = _sn
            elif not non_interactive:
                start_now = prompt_yes_no('Start the gateway now after installing the service?', True)
            else:
                start_now = True
            _sol = getattr(args, 'start_on_login', None)
            if _sol is not None:
                start_on_login = _sol
            elif not non_interactive:
                start_on_login = prompt_yes_no('Start the gateway automatically on login/boot with systemd?', True)
            else:
                start_on_login = True
            systemd_install(force=force, system=system, run_as_user=run_as_user, enable_on_startup=start_on_login, non_interactive=non_interactive)
            if start_now:
                systemd_start(system=system)
        elif is_macos():
            launchd_install(force)
        elif is_windows():
            from hermes_cli import gateway_windows
            gateway_windows.install(force=force, start_now=getattr(args, 'start_now', None), start_on_login=getattr(args, 'start_on_login', None), elevated_handoff=getattr(args, 'elevated_handoff', False))
        elif is_wsl():
            print('WSL detected but systemd is not running.')
            print('Either enable systemd (add systemd=true to /etc/wsl.conf and restart WSL)')
            print('or run the gateway in foreground mode:')
            print()
            print('  duck-agent gateway run                              # direct foreground')
            print("  tmux new -s duck-agent 'duck-agent gateway run'         # persistent via tmux")
            print('  nohup duck-agent gateway run > ~/.duck-agent/logs/gateway.log 2>&1 &  # background')
            sys.exit(1)
        elif is_container():
            from hermes_cli.service_manager import detect_service_manager
            if detect_service_manager() == 's6':
                print('Per-profile gateways are auto-registered when you create a profile.')
                print()
                print('  duck-agent profile create <name>     # creates the s6 service slot')
                print('  duck-agent -p <name> gateway start   # bring it up via s6')
                print('  duck-agent status                    # see currently-supervised gateways')
                return
            print('Service installation is not needed inside a Docker container.')
            print('The container runtime is your service manager — use Docker restart policies instead:')
            print()
            print('  docker run --restart unless-stopped ...   # auto-restart on crash/reboot')
            print('  docker restart <container>                # manual restart')
            print()
            print('To run the gateway: duck-agent gateway run')
            sys.exit(0)
        else:
            print('Service installation not supported on this platform.')
            print('Run manually: duck-agent gateway run')
            sys.exit(1)
    elif subcmd == 'uninstall':
        if is_managed():
            managed_error('uninstall gateway service (managed by NixOS)')
            return
        system = getattr(args, 'system', False)
        if is_termux():
            print('Gateway service uninstall is not supported on Termux because there is no managed service to remove.')
            print('Stop manual runs with: duck-agent gateway stop')
            sys.exit(1)
        if supports_systemd_services():
            systemd_uninstall(system=system)
        elif is_macos():
            launchd_uninstall()
        elif is_windows():
            from hermes_cli import gateway_windows
            gateway_windows.uninstall()
        elif is_container():
            from hermes_cli.service_manager import detect_service_manager
            if detect_service_manager() == 's6':
                print('Per-profile gateways are auto-unregistered when you delete the profile.')
                print()
                print('  duck-agent profile delete <name>     # tears down the s6 service slot')
                print('  duck-agent -p <name> gateway stop    # stop without deleting the profile')
                return
            print('Service uninstall is not applicable inside a Docker container.')
            print('To stop the gateway, stop or remove the container:')
            print()
            print('  docker stop <container>')
            print('  docker rm <container>')
            sys.exit(0)
        else:
            print('Not supported on this platform.')
            sys.exit(1)
    elif subcmd == 'start':
        system = getattr(args, 'system', False)
        start_all = getattr(args, 'all', False)
        if not start_all and _dispatch_via_service_manager_if_s6('start'):
            return
        if start_all:
            killed = kill_gateway_processes(all_profiles=True)
            if killed:
                print(f'✓ Killed {killed} stale gateway process(es) across all profiles')
                _wait_for_gateway_exit(timeout=10.0, force_after=5.0)
        if is_termux():
            print('Gateway service start is not supported on Termux because there is no system service manager.')
            print('Run manually: duck-agent gateway')
            sys.exit(1)
        if supports_systemd_services():
            systemd_start(system=system)
        elif is_macos():
            launchd_start()
        elif is_windows():
            from hermes_cli import gateway_windows
            gateway_windows.start()
        elif is_wsl():
            print('WSL detected but systemd is not available.')
            print('Run the gateway in foreground mode instead:')
            print()
            print('  duck-agent gateway run                              # direct foreground')
            print("  tmux new -s duck-agent 'duck-agent gateway run'         # persistent via tmux")
            print('  nohup duck-agent gateway run > ~/.duck-agent/logs/gateway.log 2>&1 &  # background')
            print()
            print("To enable systemd: add systemd=true to /etc/wsl.conf and run 'wsl --shutdown' from PowerShell.")
            sys.exit(1)
        elif is_container():
            print('Service start is not applicable inside a Docker container.')
            print("The gateway runs as the container's main process.")
            print()
            print('  docker start <container>     # start a stopped container')
            print('  docker restart <container>   # restart a running container')
            print()
            print('Or run the gateway directly: duck-agent gateway run')
            sys.exit(0)
        else:
            print('Not supported on this platform.')
            sys.exit(1)
    elif subcmd == 'stop':
        if os.getenv('_HERMES_GATEWAY') == '1':
            print_error('Refusing to stop the gateway from inside the gateway process.\nThis command was blocked to prevent restart loops.\nUse `duck-agent gateway stop` from a shell outside the running gateway.')
            sys.exit(1)
        stop_all = getattr(args, 'all', False)
        system = getattr(args, 'system', False)
        if stop_all and _dispatch_all_via_service_manager_if_s6('stop'):
            return
        if not stop_all and _dispatch_via_service_manager_if_s6('stop'):
            return
        if stop_all:
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from hermes_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_available = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_available else 0)
            if total:
                print(f'✓ Stopped {total} gateway process(es) across all profiles')
            else:
                print('✗ No gateway processes found')
        else:
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from hermes_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_available = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            if not service_available:
                if stop_profile_gateway():
                    print('✓ Stopped gateway for this profile')
                else:
                    print('✗ No gateway running for this profile')
            else:
                print(f'✓ Stopped {get_service_name()} service')
    elif subcmd == 'restart':
        if os.getenv('_HERMES_GATEWAY') == '1':
            print_error('Refusing to restart the gateway from inside the gateway process.\nThis command was blocked to prevent restart loops.\nUse `duck-agent gateway restart` from a shell outside the running gateway.')
            sys.exit(1)
        service_available = False
        system = getattr(args, 'system', False)
        restart_all = getattr(args, 'all', False)
        service_configured = False
        if restart_all and _dispatch_all_via_service_manager_if_s6('restart'):
            return
        if not restart_all and _dispatch_via_service_manager_if_s6('restart'):
            return
        if restart_all:
            service_stopped = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from hermes_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_stopped = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_stopped else 0)
            if total:
                print(f'✓ Stopped {total} gateway process(es) across all profiles')
            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)
            print('Starting gateway...')
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                systemd_start(system=system)
            elif is_macos() and get_launchd_plist_path().exists():
                launchd_start()
            elif is_windows():
                from hermes_cli import gateway_windows
                gateway_windows.start()
            else:
                run_gateway(verbose=0)
            return
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            service_configured = True
            try:
                systemd_restart(system=system)
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_macos() and get_launchd_plist_path().exists():
            service_configured = True
            try:
                launchd_restart()
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_windows():
            from hermes_cli import gateway_windows
            service_configured = gateway_windows.is_installed()
            try:
                gateway_windows.restart()
                return
            except (subprocess.CalledProcessError, RuntimeError, OSError):
                pass
        if not service_available:
            if supports_systemd_services():
                linger_ok, _detail = get_systemd_linger_status()
                if linger_ok is not True:
                    import getpass
                    _username = getpass.getuser()
                    print()
                    print('⚠ Cannot restart gateway as a service — linger is not enabled.')
                    print('  The gateway user service requires linger to function on headless servers.')
                    print()
                    print(f'  Run:  sudo loginctl enable-linger {_username}')
                    print()
                    print('  Then restart the gateway:')
                    print('    duck-agent gateway restart')
                    return
            if service_configured:
                print()
                print('✗ Gateway service restart failed.')
                print('  The service definition exists, but the service manager did not recover it.')
                print('  Fix the service, then retry: duck-agent gateway start')
                sys.exit(1)
            if stop_profile_gateway():
                print('✓ Stopped gateway for this profile')
            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)
            print('Starting gateway...')
            run_gateway(verbose=0)
    elif subcmd == 'status':
        deep = getattr(args, 'deep', False)
        full = getattr(args, 'full', False)
        system = getattr(args, 'system', False)
        snapshot = get_gateway_runtime_snapshot(system=system)
        _windows_service_installed = False
        if is_windows():
            from hermes_cli import gateway_windows
            _windows_service_installed = gateway_windows.is_installed()
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            systemd_status(deep, system=system, full=full)
            _print_gateway_process_mismatch(snapshot)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
            _print_gateway_process_mismatch(snapshot)
        elif _windows_service_installed:
            from hermes_cli import gateway_windows
            gateway_windows.status(deep=deep)
            _print_gateway_process_mismatch(snapshot)
        else:
            pids = list(snapshot.gateway_pids)
            if pids:
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                print('  (Running manually, not as a system service)')
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print('Recent gateway health:')
                    for line in runtime_lines:
                        print(f'  {line}')
                print()
                if is_termux():
                    print('Termux note:')
                    print('  Android may stop background jobs when Termux is suspended')
                elif is_wsl():
                    print('WSL note:')
                    print('  The gateway is running in foreground/manual mode (recommended for WSL).')
                    print('  Use tmux or screen for persistence across terminal closes.')
                elif is_windows():
                    print('To install as a Windows Scheduled Task (auto-start on login):')
                    print('  duck-agent gateway install')
                else:
                    print('To install as a service:')
                    print('  duck-agent gateway install')
                    print('  sudo duck-agent gateway install --system')
            else:
                print('✗ Gateway is not running')
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print('Recent gateway health:')
                    for line in runtime_lines:
                        print(f'  {line}')
                print()
                print('To start:')
                print('  duck-agent gateway run      # Run in foreground')
                if is_termux():
                    print('  nohup duck-agent gateway run > ~/.duck-agent/logs/gateway.log 2>&1 &  # Best-effort background start')
                elif is_wsl():
                    print("  tmux new -s duck-agent 'duck-agent gateway run'         # persistent via tmux")
                    print('  nohup duck-agent gateway run > ~/.duck-agent/logs/gateway.log 2>&1 &  # background')
                elif is_windows():
                    print('  duck-agent gateway install  # Install as Windows Scheduled Task (auto-start on login)')
                else:
                    print('  duck-agent gateway install  # Install as user service')
                    print('  sudo duck-agent gateway install --system  # Install as boot-time system service')
        _print_other_profiles_gateway_status()
    elif subcmd == 'list':
        _gateway_list()
    elif subcmd == 'migrate-legacy':
        dry_run = getattr(args, 'dry_run', False)
        yes = getattr(args, 'yes', False)
        if not supports_systemd_services() and (not is_macos()):
            print('Legacy unit migration only applies to systemd-based Linux hosts.')
            return
        remove_legacy_hermes_units(interactive=not yes, dry_run=dry_run)