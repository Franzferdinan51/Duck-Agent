"""Base class for all Duck Agent execution environment backends.

Unified spawn-per-call model: every command spawns a fresh ``bash -c`` process.
A session snapshot (env vars, functions, aliases) is captured once at init and
re-sourced before each command. CWD persists via in-band stdout markers (remote)
or a temp file (local).
"""
import codecs
import json
import logging
import os
import re
import select
import shlex
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import IO, Callable, Iterable, Protocol
from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags
from tools.interrupt import is_interrupted
logger = logging.getLogger(__name__)
_DEBUG_INTERRUPT = bool(os.getenv('HERMES_DEBUG_INTERRUPT'))
if _DEBUG_INTERRUPT:
    logger.setLevel(logging.INFO)
_activity_callback_local = threading.local()
_UNBOUNDED_CAPTURE_CHARS = 2 ** 63 - 1

class EnvironmentConnectionError(RuntimeError):
    """Infrastructure/connection-class failure of a terminal backend.

    Raised when the backend itself is unreachable (SSH host down, Docker
    daemon not running, remote file sync failing on a dead link) — never
    for a command that merely exited nonzero.  Subclassing RuntimeError
    keeps every existing ``except RuntimeError`` catcher working.

    ``terminal_tool`` turns this into a structured ``status: "degraded"``
    tool result (config gate ``terminal.degraded_mode: warn|fail``) so the
    model gets an actionable reason + retry hint instead of a traceback.
    The failed backend is never cached, so a later call retries from
    scratch and simply works once the backend is reachable again.
    """

    def __init__(self, reason: str, *, retry_hint: str=''):
        super().__init__(reason)
        self.reason = reason
        self.retry_hint = retry_hint or 'This is an infrastructure failure, not a command failure. Verify the backend is reachable (network, service running, credentials), then retry the same command — recovery is automatic once the backend is back.'

class _BoundedOutputCollector:
    """Retain a bounded 40/60 head-tail window of streamed text.

    When ``spill_path`` is set, the collector also tees the FULL stream to
    that file once eviction begins (up to ``_SPILL_CAP_CHARS``), so a
    truncated foreground result is recoverable without re-running the
    command. Memory stays bounded either way — the spill is disk-only.
    """
    _SPILL_CAP_CHARS = 5000000

    def __init__(self, max_chars: int, spill_path: 'Path | None'=None):
        self.max_chars = max(1, int(max_chars))
        self._head_limit = int(self.max_chars * 0.4)
        self._tail_limit = self.max_chars - self._head_limit
        self._head: list[str] = []
        self._tail: deque[str] = deque()
        self._head_chars = 0
        self._tail_chars = 0
        self._total_chars = 0
        self._lock = threading.Lock()
        self._spill_path = spill_path
        self._spill_fh: IO[str] | None = None
        self._spill_chars = 0
        self._spill_capped = False

    def _maybe_spill(self, text: str) -> None:
        """Tee ``text`` to the spill file (opened lazily on first overflow)."""
        if self._spill_path is None or self._spill_capped:
            return
        try:
            if self._spill_fh is None:
                self._spill_path.parent.mkdir(parents=True, exist_ok=True)
                self._spill_fh = open(self._spill_path, 'w', encoding='utf-8', errors='replace')
                backlog = ''.join(self._head) + ''.join(self._tail)
                self._spill_fh.write(backlog)
                self._spill_chars = len(backlog)
            budget = self._SPILL_CAP_CHARS - self._spill_chars
            if budget <= 0 or len(text) > budget:
                self._spill_fh.write(text[:max(0, budget)])
                self._spill_fh.write('\n... [spill capped at 5,000,000 chars] ...\n')
                self._spill_capped = True
            else:
                self._spill_fh.write(text)
            self._spill_chars += len(text)
        except OSError:
            self._spill_capped = True

    def close_spill(self) -> 'str | None':
        """Close the spill file and return its path if it was used."""
        with self._lock:
            if self._spill_fh is None:
                return None
            try:
                self._spill_fh.close()
            except OSError:
                pass
            self._spill_fh = None
            return str(self._spill_path)

    @property
    def buffered_chars(self) -> int:
        with self._lock:
            return self._head_chars + self._tail_chars

    @property
    def total_chars(self) -> int:
        with self._lock:
            return self._total_chars

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            text_len = len(text)
            if self._spill_path is not None and (self._spill_fh is not None or self._total_chars + text_len > self.max_chars):
                self._maybe_spill(text)
            self._total_chars += text_len
            start = 0
            if self._head_chars < self._head_limit:
                take = min(self._head_limit - self._head_chars, text_len)
                if take:
                    self._head.append(text[:take])
                    self._head_chars += take
                    start = take
            remaining = text_len - start
            if remaining <= 0 or self._tail_limit <= 0:
                return
            if remaining >= self._tail_limit:
                self._tail.clear()
                self._tail.append(text[-self._tail_limit:])
                self._tail_chars = self._tail_limit
                return
            chunk = text[start:]
            self._tail.append(chunk)
            self._tail_chars += len(chunk)
            while self._tail_chars > self._tail_limit:
                excess = self._tail_chars - self._tail_limit
                first = self._tail[0]
                if len(first) <= excess:
                    self._tail.popleft()
                    self._tail_chars -= len(first)
                else:
                    self._tail[0] = first[excess:]
                    self._tail_chars -= excess

    def render(self, *, suffix: str='') -> str:
        """Render within ``max_chars``, preserving a required status suffix."""
        with self._lock:
            if len(suffix) >= self.max_chars:
                return suffix[-self.max_chars:]
            head = ''.join(self._head)
            tail = ''.join(self._tail)
            available = self.max_chars - len(suffix)
            if self._total_chars <= available:
                return head + tail + suffix
            notice = ''
            for _ in range(4):
                content_budget = max(0, available - len(notice))
                head_chars = int(content_budget * 0.4)
                tail_chars = content_budget - head_chars
                omitted = max(0, self._total_chars - head_chars - tail_chars)
                updated = f'\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted out of {self._total_chars:,} total] ...\n\n'
                if updated == notice:
                    break
                notice = updated
            content_budget = max(0, available - len(notice))
            head_chars = int(content_budget * 0.4)
            tail_chars = content_budget - head_chars
            rendered_tail = tail[-tail_chars:] if tail_chars else ''
            return head[:head_chars] + notice[:available] + rendered_tail + suffix

def set_activity_callback(cb: Callable[[str], None] | None) -> None:
    """Register a callback that _wait_for_process fires periodically."""
    _activity_callback_local.callback = cb

def get_activity_callback() -> Callable[[str], None] | None:
    """Return the thread-local activity callback (see ``set_activity_callback``).

    Public accessor for callers outside this module that need to capture the
    calling thread's callback before handing work to another thread (the
    callback is thread-local, so a freshly spawned thread cannot read it
    back) — e.g. the manual cron-run heartbeat (#76502).
    """
    return getattr(_activity_callback_local, 'callback', None)

def touch_activity_if_due(state: dict, label: str) -> None:
    """Fire the activity callback at most once every ``state['interval']`` seconds.

    *state* must contain ``last_touch`` (monotonic timestamp) and ``start``
    (monotonic timestamp of the operation start).  An optional ``interval``
    key overrides the default 10 s cadence.

    Swallows all exceptions so callers don't need their own try/except.
    """
    now = time.monotonic()
    interval = state.get('interval', 10.0)
    if now - state['last_touch'] < interval:
        return
    state['last_touch'] = now
    try:
        cb = get_activity_callback()
        if cb:
            elapsed = int(now - state['start'])
            cb(f'{label} ({elapsed}s elapsed)')
    except Exception:
        pass

def get_sandbox_dir() -> Path:
    """Return the host-side root for all sandbox storage (Docker workspaces,
    Singularity overlays/SIF cache, etc.).

    Configurable via TERMINAL_SANDBOX_DIR. Defaults to {DUCK_AGENT_HOME}/sandboxes/.
    """
    custom = os.getenv('TERMINAL_SANDBOX_DIR')
    if custom:
        p = Path(custom)
    else:
        p = get_hermes_home() / 'sandboxes'
    p.mkdir(parents=True, exist_ok=True)
    return p

def _pipe_stdin(proc: subprocess.Popen, data: str) -> None:
    """Write *data* to proc.stdin on a daemon thread to avoid pipe-buffer deadlocks.

    On Windows, text-mode stdin (``text=True`` / ``encoding="utf-8"``)
    translates ``\\n`` → ``\\r\\n`` as the data flows through the pipe —
    which corrupts every write_file / patch call because the bytes that
    land on disk include injected carriage returns.  The file IS created,
    but every subsequent byte-count / content compare against the
    caller's ``\\n``-only string fails.

    Workaround: write through ``proc.stdin.buffer`` (the underlying byte
    buffer), encoding to UTF-8 ourselves.  That bypasses Python's
    newline translation entirely on every platform.  No behaviour change
    on POSIX — the byte sequence is identical to what text-mode would
    produce there.
    """

    def _write():
        try:
            raw = data.encode('utf-8') if isinstance(data, str) else data
            target = getattr(proc.stdin, 'buffer', proc.stdin)
            target.write(raw)
            target.close()
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=_write, daemon=True).start()

def _popen_bash(cmd: list[str], stdin_data: str | None=None, **kwargs) -> subprocess.Popen:
    """Spawn a subprocess with standard stdout/stderr/stdin setup.

    If *stdin_data* is provided, writes it asynchronously via :func:`_pipe_stdin`.
    Backends with special Popen needs (e.g. local's ``preexec_fn``) can bypass
    this and call :func:`_pipe_stdin` directly.
    """
    kwargs.setdefault('creationflags', windows_hide_flags())
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', **kwargs)
    if stdin_data is not None:
        _pipe_stdin(proc, stdin_data)
    return proc

def _load_json_store(path: Path) -> dict:
    """Load a JSON file as a dict, returning ``{}`` on any error."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def _save_json_store(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')

def _file_mtime_key(host_path: str) -> tuple[float, int] | None:
    """Return ``(mtime, size)`` for cache comparison, or ``None`` if unreadable."""
    try:
        st = Path(host_path).stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None

class ProcessHandle(Protocol):
    """Duck type that every backend's _run_bash() must return.

    subprocess.Popen satisfies this natively.  SDK backends (Modal, Daytona)
    return _ThreadedProcessHandle which adapts their blocking calls.
    """

    def poll(self) -> int | None:
        ...

    def kill(self) -> None:
        ...

    def wait(self, timeout: float | None=None) -> int:
        ...

    @property
    def stdout(self) -> IO[str] | None:
        ...

    @property
    def returncode(self) -> int | None:
        ...

class _ThreadedProcessHandle:
    """Adapter for SDK backends (Modal, Daytona) that have no real subprocess.

    Wraps a blocking ``exec_fn() -> (output_str, exit_code)`` in a background
    thread and exposes a ProcessHandle-compatible interface.  An optional
    ``cancel_fn`` is invoked on ``kill()`` for backend-specific cancellation
    (e.g. Modal sandbox.terminate, Daytona sandbox.stop).
    """

    def __init__(self, exec_fn: Callable[[], tuple[str, int]], cancel_fn: Callable[[], None] | None=None):
        self._cancel_fn = cancel_fn
        self._done = threading.Event()
        self._returncode: int | None = None
        self._error: Exception | None = None
        read_fd, write_fd = os.pipe()
        self._stdout = os.fdopen(read_fd, 'r', encoding='utf-8', errors='replace')
        self._write_fd = write_fd

        def _worker():
            try:
                output, exit_code = exec_fn()
                self._returncode = exit_code
                try:
                    os.write(self._write_fd, output.encode('utf-8', errors='replace'))
                except OSError:
                    pass
            except Exception as exc:
                self._error = exc
                self._returncode = 1
            finally:
                try:
                    os.close(self._write_fd)
                except OSError:
                    pass
                self._done.set()
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def kill(self):
        if self._cancel_fn:
            try:
                self._cancel_fn()
            except Exception:
                pass

    def wait(self, timeout: float | None=None) -> int:
        self._done.wait(timeout=timeout)
        return self._returncode

def _cwd_marker(session_id: str) -> str:
    return f'__HERMES_CWD_{session_id}__'
_SNAPSHOT_EXCLUDED_ENV_REGEX = '^declare -x (HERMES_SESSION_|HERMES_UI_SESSION_ID|HERMES_CRON_AUTO_DELIVER_|HERMES_CRON_SESSION)'
_SHELL_ENV_NAME_RE = re.compile('^[A-Za-z_][A-Za-z0-9_]*$')

def _export_dump_excluding_session_vars(tmp_path: str, excluded_names: Iterable[str]=()) -> str:
    """Return a shell snippet that dumps ``export -p`` to *tmp_path* minus the
    per-session bridged vars (see ``_SNAPSHOT_EXCLUDED_ENV_REGEX``) and any
    additional names supplied by the caller.

    Unset the bridged vars in a subshell *before* ``export -p``. A line-based
    ``grep -vE`` filter is unsafe: bash 3.2 prints a value containing a newline
    as a multi-line ``declare -x NAME="…`` block, so only the opener matches the
    regex and continuation lines (e.g. ``curl … | bash #`` smuggled into a
    Matrix room/display name via ``HERMES_SESSION_CHAT_NAME``) land in the
    snapshot and execute on the next ``source`` (issue #71296). Unsetting first
    means ``export -p`` never emits those vars — including any continuation
    lines. ``|| true`` keeps the success contract for callers that chain on it.

    The dump MUST be wrapped in a brace group with the redirection applied to
    the group. *tmp_path* is typically a shell-variable expansion (a
    mktemp-allocated per-writer temp name); a redirection attached to a
    pipeline segment would expand it inside that segment's subshell,
    potentially inconsistently with the parent that expands the follow-up
    ``mv``. The brace-group redirect is expanded in the current shell,
    keeping both expansions consistent.
    """
    safe_names = {name for name in excluded_names if isinstance(name, str) and name}
    extra_unset = ' '.join((shlex.quote(name) for name in sorted(safe_names)))
    if extra_unset:
        extra_unset = f' {extra_unset}'
    return f'{{ ( unset ${{!HERMES_SESSION_*}} ${{!HERMES_CRON_AUTO_DELIVER_*}} HERMES_UI_SESSION_ID{extra_unset} 2>/dev/null; export -p; ) || true; }} > {tmp_path}'

class BaseEnvironment(ABC):
    """Common interface and unified execution flow for all Duck Agent backends.

    Subclasses implement ``_run_bash()`` and ``cleanup()``.  The base class
    provides ``execute()`` with session snapshot sourcing, CWD tracking,
    interrupt handling, and timeout enforcement.
    """
    _stdin_mode: str = 'pipe'
    _snapshot_timeout: int = 30
    _profile_scoped_passthrough: bool = False

    def get_temp_dir(self) -> str:
        """Return the backend temp directory used for session artifacts.

        Most sandboxed backends use ``/tmp`` inside the target environment.
        LocalEnvironment overrides this on platforms like Termux where ``/tmp``
        may be missing and ``TMPDIR`` is the portable writable location.
        """
        return '/tmp'

    def __init__(self, cwd: str, timeout: int, env: dict=None):
        self.cwd = cwd
        self.timeout = timeout
        self.env = env or {}
        self._session_id = uuid.uuid4().hex[:12]
        temp_dir = self.get_temp_dir().rstrip('/') or '/'
        self._snapshot_path = f'{temp_dir}/duck-agent-snap-{self._session_id}.sh'
        self._cwd_file = f'{temp_dir}/duck-agent-cwd-{self._session_id}.txt'
        self._cwd_marker = _cwd_marker(self._session_id)
        self._snapshot_ready = False
        self._snapshot_passthrough_names: set[str] = set()
        self._prefer_nonlogin = False

    def _run_bash(self, cmd_string: str, *, login: bool=False, timeout: int=120, stdin_data: str | None=None) -> ProcessHandle:
        """Spawn a bash process to run *cmd_string*.

        Returns a ProcessHandle (subprocess.Popen or _ThreadedProcessHandle).
        Must be overridden by every backend.
        """
        raise NotImplementedError(f'{type(self).__name__} must implement _run_bash()')

    @abstractmethod
    def cleanup(self):
        """Release backend resources (container, instance, connection)."""
        ...

    def _additional_profile_scoped_passthrough_names(self) -> Iterable[str]:
        """Return backend-specific names that must not persist in snapshots."""
        return ()

    def _snapshot_excluded_passthrough_names(self) -> tuple[str, ...]:
        """Return profile-scoped names that must not persist in the snapshot.

        The set is monotonic for the environment lifetime. A skill/config
        allowlist can be cleared after a value was captured; retaining the
        exclusion prevents that old value from becoming visible to a later
        profile through the shared snapshot.
        """
        if not self._profile_scoped_passthrough:
            return ()
        try:
            from agent.secret_scope import is_multiplex_active
            if is_multiplex_active():
                from tools.env_passthrough import get_all_passthrough
                names = (*get_all_passthrough(), *self._additional_profile_scoped_passthrough_names())
                self._snapshot_passthrough_names.update((name for name in names if isinstance(name, str) and _SHELL_ENV_NAME_RE.fullmatch(name)))
        except Exception:
            logger.debug('Could not refresh profile-scoped snapshot exclusions', exc_info=True)
        return tuple(sorted(self._snapshot_passthrough_names))

    def init_session(self):
        """Capture login shell environment into a snapshot file.

        Called once after backend construction.  On success, sets
        ``_snapshot_ready = True`` so subsequent commands source the snapshot
        instead of running with ``bash -l``.
        """
        _quoted_cwd = self._quote_cwd_for_cd(self.cwd)
        _quoted_snap = self._quote_shell_path(self._snapshot_path)
        _snap_tmp_template = self._quote_shell_path(self._snapshot_path + '.tmp.XXXXXXXXXX')
        _snap_tmp = '"$__hermes_snap_tmp"'
        snapshot_excluded = self._snapshot_excluded_passthrough_names()
        bootstrap = f"""umask 077\n__hermes_snap_tmp=$(mktemp {_snap_tmp_template}) || exit 1\n{_export_dump_excluding_session_vars(_snap_tmp, snapshot_excluded)}\n__hermes_fns=$(declare -F | awk '{{print $3}}' | grep -vE '^_[^_]') || true\n[ -n "$__hermes_fns" ] && declare -f $__hermes_fns >> {_snap_tmp} 2>/dev/null || true\nalias -p >> {_snap_tmp}\necho 'shopt -s expand_aliases' >> {_snap_tmp}\necho 'set +e' >> {_snap_tmp}\necho 'set +u' >> {_snap_tmp}\nmv -f {_snap_tmp} {_quoted_snap} || rm -f {_snap_tmp}\nbuiltin cd -- {_quoted_cwd} 2>/dev/null || true\nprintf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' "$(pwd -P)"\n"""
        try:
            proc = self._run_bash(bootstrap, login=True, timeout=self._snapshot_timeout)
            result = self._wait_for_process(proc, timeout=self._snapshot_timeout)
            if int(result.get('returncode') or 0) != 0:
                raise RuntimeError(f"snapshot bootstrap failed with exit code {result.get('returncode')}")
            self._snapshot_ready = True
            self._update_cwd(result)
            logger.info('Session snapshot created (session=%s, cwd=%s)', self._session_id, self.cwd)
        except Exception as exc:
            self._snapshot_ready = False
            detail = str(exc)
            prefer_nonlogin = False
            try:
                probe = self._run_bash('true', login=False, timeout=min(15, self._snapshot_timeout))
                probe_result = self._wait_for_process(probe, timeout=min(15, self._snapshot_timeout))
                prefer_nonlogin = int(probe_result.get('returncode') or 0) == 0
                if not prefer_nonlogin:
                    detail = (probe_result.get('stdout') or detail).strip() or detail
            except Exception as probe_exc:
                detail = f'{detail}; non-login probe: {probe_exc}'
            self._prefer_nonlogin = prefer_nonlogin
            if prefer_nonlogin:
                logger.warning('init_session failed (session=%s): %s — login bash unusable; falling back to non-login bash -c', self._session_id, exc)
            else:
                logger.warning('init_session failed (session=%s): %s — falling back to bash -l per command', self._session_id, detail)

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        """Quote a ``cd`` target while preserving ``~`` expansion."""
        if cwd == '~':
            return cwd
        if cwd == '~/':
            return '$HOME'
        if cwd.startswith('~/'):
            return f'$HOME/{shlex.quote(cwd[2:])}'
        return shlex.quote(cwd)

    def _quote_shell_path(self, path: str) -> str:
        """Quote *path* for interpolation into a bash script.

        LocalEnvironment overrides this to rewrite native/mixed Windows
        paths to ``/c/...`` before quoting. Remote backends leave paths
        as-is (they already speak POSIX).
        """
        return shlex.quote(path)

    def _wrap_command(self, command: str, cwd: str) -> str:
        """Build the full bash script that sources snapshot, cd's, runs command,
        re-dumps env vars, and emits CWD markers."""
        escaped = command.replace("'", "'\\''")
        _quoted_snap = self._quote_shell_path(self._snapshot_path)
        _snap_tmp_template = self._quote_shell_path(self._snapshot_path + '.tmp.XXXXXXXXXX')
        _snap_tmp = '"$__hermes_snap_tmp"'
        parts = []
        passthrough_names = self._snapshot_excluded_passthrough_names()
        saved_names: list[tuple[str, str, str]] = []
        for name in passthrough_names:
            marker = f'_HERMES_RUNTIME_PASSTHROUGH_{name}'
            present = f'{marker}_PRESENT'
            value = f'{marker}_VALUE'
            saved_names.append((name, present, value))
            parts.append(f'{present}=${{{name}+x}}')
            parts.append(f'{value}=${{{name}-}}')
        if self._snapshot_ready:
            parts.append(f'source {_quoted_snap} >/dev/null 2>&1 || true')
        for name, present, value in saved_names:
            parts.append(f'if [ "${present}" = x ]; then export {name}="${value}"; else unset {name}; fi')
            parts.append(f'unset {present} {value}')
        quoted_cwd = self._quote_cwd_for_cd(cwd)
        parts.append(f'builtin cd -- {quoted_cwd} || exit 126')
        parts.append(f"eval '{escaped}'")
        parts.append('__hermes_ec=$?')
        parts.append('umask 077')
        if self._snapshot_ready:
            parts.append(f'__hermes_snap_tmp=$(mktemp {_snap_tmp_template}) && {{ {_export_dump_excluding_session_vars(_snap_tmp, passthrough_names)} && mv -f {_snap_tmp} {_quoted_snap}; }} 2>/dev/null || rm -f {_snap_tmp} 2>/dev/null || true')
        parts.append(f'''printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' "$(pwd -P)"''')
        parts.append('exit $__hermes_ec')
        return '\n'.join(parts)

    @staticmethod
    def _embed_stdin_heredoc(command: str, stdin_data: str) -> str:
        """Append stdin_data as a shell heredoc to the command string."""
        delimiter = f'HERMES_STDIN_{uuid.uuid4().hex[:12]}'
        return f"{command} << '{delimiter}'\n{stdin_data}\n{delimiter}"

    def _wait_for_process(self, proc: ProcessHandle, timeout: int=120, *, bounded_capture: bool=False) -> dict:
        """Poll-based wait with interrupt checking and stdout draining.

        Shared across all backends — not overridden.

        ``bounded_capture=True`` (foreground terminal-tool path only) retains
        at most ``tool_output.max_bytes`` of output in a head/tail window
        while draining, so a verbose subprocess cannot OOM the process
        (#64435). The default (False) preserves full-fidelity capture for
        internal consumers — file-operation ``cat`` reads feeding the patch
        engine, code-execution RPC reads, log reads — where truncation would
        corrupt data.

        Fires the ``activity_callback`` (if set on this instance) every 10s
        while the process is running so the gateway's inactivity timeout
        doesn't kill long-running commands.

        Also wraps the poll loop in a ``try/finally`` that guarantees we
        call ``self._kill_process(proc)`` if we exit via ``KeyboardInterrupt``
        or ``SystemExit``.  Without this, the local backend (which spawns
        subprocesses with ``os.setsid`` into their own process group) leaves
        an orphan with ``PPID=1`` when python is shut down mid-tool — the
        ``sleep 300``-survives-30-min bug Physikal and I both hit.
        """
        if bounded_capture:
            try:
                from tools.tool_output_limits import get_max_bytes
                capture_limit = get_max_bytes()
            except Exception:
                capture_limit = 50000
        else:
            capture_limit = _UNBOUNDED_CAPTURE_CHARS
        spill_path = None
        if bounded_capture:
            try:
                spill_dir = get_hermes_home() / 'cache' / 'terminal-output'
                spill_path = spill_dir / f'out-{int(time.time())}-{os.getpid()}-{id(proc) & 65535:x}.log'
                if spill_dir.is_dir():
                    cutoff = time.time() - 7 * 86400
                    for old in spill_dir.glob('out-*.log'):
                        try:
                            if old.stat().st_mtime < cutoff:
                                old.unlink()
                        except OSError:
                            pass
            except Exception:
                spill_path = None
        output = _BoundedOutputCollector(capture_limit, spill_path=spill_path)
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

        def _drain_iterable(stream):
            try:
                for piece in stream:
                    if piece is None:
                        continue
                    if isinstance(piece, bytes):
                        output.append(decoder.decode(piece))
                    else:
                        output.append(str(piece))
            except Exception:
                pass
            finally:
                try:
                    tail = decoder.decode(b'', final=True)
                    if tail:
                        output.append(tail)
                except Exception:
                    pass

        def _drain():
            stream = proc.stdout
            if stream is None:
                return
            fileno = getattr(stream, 'fileno', None)
            try:
                fd = fileno() if callable(fileno) else None
            except Exception:
                fd = None
            if not isinstance(fd, int) or fd < 0:
                _drain_iterable(stream)
                return
            if os.name == 'nt':
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        output.append(decoder.decode(chunk))
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        tail = decoder.decode(b'', final=True)
                        if tail:
                            output.append(tail)
                    except Exception:
                        pass
                return
            idle_after_exit = 0
            try:
                while True:
                    try:
                        ready, _, _ = select.select([fd], [], [], 0.1)
                    except (ValueError, OSError):
                        break
                    if ready:
                        try:
                            chunk = os.read(fd, 4096)
                        except (ValueError, OSError):
                            break
                        if not chunk:
                            break
                        output.append(decoder.decode(chunk))
                        idle_after_exit = 0
                    elif proc.poll() is not None:
                        idle_after_exit += 1
                        if idle_after_exit >= 3:
                            break
            finally:
                try:
                    tail = decoder.decode(b'', final=True)
                    if tail:
                        output.append(tail)
                except Exception:
                    pass
        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        deadline = time.monotonic() + timeout
        _now = time.monotonic()
        _activity_state = {'last_touch': _now, 'start': _now}
        _tid = threading.current_thread().ident
        _pid = getattr(proc, 'pid', None)
        _iter_count = 0
        _last_heartbeat = _now
        _last_interrupt_state = False
        _cb_was_none = get_activity_callback() is None
        if _DEBUG_INTERRUPT:
            logger.info('[interrupt-debug] _wait_for_process ENTER tid=%s pid=%s timeout=%ss activity_cb=%s initial_interrupt=%s', _tid, _pid, timeout, 'set' if not _cb_was_none else 'MISSING', is_interrupted())
        try:
            _poll_sleep = 0.005
            while proc.poll() is None:
                _iter_count += 1
                if is_interrupted():
                    if _DEBUG_INTERRUPT:
                        logger.info('[interrupt-debug] _wait_for_process INTERRUPT DETECTED tid=%s pid=%s iter=%d elapsed=%.1fs — killing process group', _tid, _pid, _iter_count, time.monotonic() - _activity_state['start'])
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    return self._finalize_wait_result(output, output.render(suffix='\n[Command interrupted]'), 130)
                if time.monotonic() > deadline:
                    if _DEBUG_INTERRUPT:
                        logger.info('[interrupt-debug] _wait_for_process TIMEOUT tid=%s pid=%s iter=%d timeout=%ss', _tid, _pid, _iter_count, timeout)
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    timeout_msg = f'\n[Command timed out after {timeout}s]'
                    return self._finalize_wait_result(output, output.render(suffix=timeout_msg).lstrip() if output.total_chars == 0 else output.render(suffix=timeout_msg), 124)
                touch_activity_if_due(_activity_state, 'terminal command running')
                if _DEBUG_INTERRUPT and time.monotonic() - _last_heartbeat >= 30.0:
                    _cb_now_none = get_activity_callback() is None
                    logger.info('[interrupt-debug] _wait_for_process HEARTBEAT tid=%s pid=%s iter=%d elapsed=%.0fs interrupt=%s activity_cb=%s%s', _tid, _pid, _iter_count, time.monotonic() - _activity_state['start'], is_interrupted(), 'set' if not _cb_now_none else 'MISSING', ' (LOST during run)' if _cb_now_none and (not _cb_was_none) else '')
                    _last_heartbeat = time.monotonic()
                    _cb_was_none = _cb_now_none
                time.sleep(_poll_sleep)
                if _poll_sleep < 0.2:
                    _poll_sleep = min(_poll_sleep * 1.5, 0.2)
        except (KeyboardInterrupt, SystemExit):
            if _DEBUG_INTERRUPT:
                logger.info('[interrupt-debug] _wait_for_process EXCEPTION_EXIT tid=%s pid=%s iter=%d elapsed=%.1fs — killing subprocess group before re-raise', _tid, _pid, _iter_count, time.monotonic() - _activity_state['start'])
            try:
                self._kill_process(proc)
                drain_thread.join(timeout=2)
            except Exception:
                pass
            raise
        drain_thread.join(timeout=2)
        try:
            proc.stdout.close()
        except Exception:
            pass
        if _DEBUG_INTERRUPT:
            logger.info('[interrupt-debug] _wait_for_process EXIT (natural) tid=%s pid=%s iter=%d elapsed=%.1fs returncode=%s', _tid, _pid, _iter_count, time.monotonic() - _activity_state['start'], proc.returncode)
        return self._finalize_wait_result(output, output.render(), proc.returncode)

    @staticmethod
    def _finalize_wait_result(collector: '_BoundedOutputCollector', rendered: str, returncode: int | None) -> dict:
        """Assemble a wait result, attaching spill metadata when overflow occurred."""
        result = {'output': rendered, 'returncode': returncode}
        spill = collector.close_spill()
        if spill:
            result['output_total_chars'] = collector.total_chars
            result['full_output_path'] = spill
        return result

    def _kill_process(self, proc: ProcessHandle):
        """Terminate a process. Subclasses may override for process-group kill."""
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _update_cwd(self, result: dict):
        """Extract CWD from command output. Override for local file-based read."""
        self._extract_cwd_from_output(result)

    def _extract_cwd_from_output(self, result: dict):
        """Parse the __HERMES_CWD_{session}__ marker from stdout output.

        Updates self.cwd and strips the marker from result["output"].
        Used by remote backends (Docker, SSH, Modal, Daytona, Singularity).
        """
        output = result.get('output', '')
        marker = self._cwd_marker
        last = output.rfind(marker)
        if last == -1:
            return
        search_start = max(0, last - 4096)
        first = output.rfind(marker, search_start, last)
        if first == -1 or first == last:
            return
        cwd_path = output[first + len(marker):last].strip()
        if cwd_path:
            self.cwd = cwd_path
        line_start = output.rfind('\n', 0, first)
        if line_start == -1:
            line_start = first
        line_end = output.find('\n', last + len(marker))
        line_end = line_end + 1 if line_end != -1 else len(output)
        result['output'] = output[:line_start] + output[line_end:]

    def _before_execute(self) -> None:
        """Hook called before each command execution.

        Remote backends (SSH, Modal, Daytona) override this to trigger
        their FileSyncManager.  Bind-mount backends (Docker, Singularity)
        and Local don't need file sync — the host filesystem is directly
        visible inside the container/process.
        """
        pass

    def execute(self, command: str, cwd: str='', *, timeout: int | None=None, stdin_data: str | None=None, rewrite_compound_background: bool=True, bounded_capture: bool=False) -> dict:
        """Execute a command, return {"output": str, "returncode": int}.

        ``bounded_capture=True`` caps stdout/stderr retention at
        ``tool_output.max_bytes`` WHILE the stream is drained (head/tail
        window) instead of holding the full output in memory (#64435).
        It must only be set by callers whose output is destined for the
        model/tool payload (the foreground terminal tool). Internal
        full-fidelity consumers — file operations ``cat`` reads that feed
        the patch engine, code-execution RPC reads, log reads — MUST leave
        it False: truncating those corrupts data, not just display.
        """
        self._before_execute()
        exec_command, sudo_stdin = self._prepare_command(command)
        if rewrite_compound_background:
            from tools.terminal_tool import _rewrite_compound_background
            exec_command = _rewrite_compound_background(exec_command)
        effective_timeout = timeout or self.timeout
        effective_cwd = cwd or self.cwd
        if sudo_stdin is not None and stdin_data is not None:
            effective_stdin = sudo_stdin + stdin_data
        elif sudo_stdin is not None:
            effective_stdin = sudo_stdin
        else:
            effective_stdin = stdin_data
        if effective_stdin and self._stdin_mode == 'heredoc':
            exec_command = self._embed_stdin_heredoc(exec_command, effective_stdin)
            effective_stdin = None
        wrapped = self._wrap_command(exec_command, effective_cwd)
        login = not self._snapshot_ready and (not self._prefer_nonlogin)
        proc = self._run_bash(wrapped, login=login, timeout=effective_timeout, stdin_data=effective_stdin)
        result = self._wait_for_process(proc, timeout=effective_timeout, bounded_capture=bounded_capture)
        self._update_cwd(result)
        return result

    def stop(self):
        """Alias for cleanup (compat with older callers)."""
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    def _prepare_command(self, command: str) -> tuple[str, str | None]:
        """Transform sudo commands if SUDO_PASSWORD is available."""
        from tools.terminal_tool import _transform_sudo_command
        return _transform_sudo_command(command)