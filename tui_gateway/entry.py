import os
import sys
import hermes_bootstrap
hermes_bootstrap.harden_import_path()
import json
import logging
import signal
import threading
import time
import traceback
from tui_gateway._stdin_recovery import handle_spurious_eof
from tui_gateway import server
from tui_gateway.server import _CRASH_LOG, dispatch, resolve_skin, write_json
from tui_gateway.transport import TeeTransport
logger = logging.getLogger(__name__)
_mcp_discovery_thread = None
_mcp_discovery_enabled = False

def _install_sidecar_publisher() -> None:
    """Mirror every dispatcher emit to the dashboard sidebar via WS.

    Activated by `HERMES_TUI_SIDECAR_URL`, set by the dashboard's
    ``/api/pty`` endpoint when a chat tab passes a ``channel`` query param.
    Best-effort: connect failure or runtime drop falls back to stdio-only.
    """
    url = os.environ.get('HERMES_TUI_SIDECAR_URL')
    if not url:
        return
    from tui_gateway.event_publisher import WsPublisherTransport
    server._stdio_transport = TeeTransport(server._stdio_transport, WsPublisherTransport(url))
_DEFAULT_SHUTDOWN_GRACE_S = 1.0

def _shutdown_grace_seconds() -> float:
    raw = (os.environ.get('HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S') or '').strip()
    if not raw:
        return _DEFAULT_SHUTDOWN_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SHUTDOWN_GRACE_S
    return value if value > 0 else _DEFAULT_SHUTDOWN_GRACE_S

def _log_signal(signum: int, frame) -> None:
    """Capture WHICH thread and WHERE a termination signal hit us.

    SIG_DFL for SIGPIPE kills the process silently the instant any
    background thread (TTS playback, beep, voice status emitter, etc.)
    writes to a stdout the TUI has stopped reading.  Without this
    handler the gateway-exited banner in the TUI has no trace — the
    crash log never sees a Python exception because the kernel reaps
    the process before the interpreter runs anything.

    Termination semantics: ``sys.exit(0)`` here used to race the worker
    pool — a thread holding ``_stdout_lock`` mid-flush would block the
    interpreter shutdown indefinitely.  We now log the stack, give the
    process the configured shutdown grace
    (``HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S``, default
    ``_DEFAULT_SHUTDOWN_GRACE_S``) to drain naturally on a background
    thread, and fall back to ``os._exit(0)`` so a wedged write/flush
    can never strand the process.
    """
    _signal_names: dict[int, str] = {}
    for _attr in ('SIGPIPE', 'SIGTERM', 'SIGHUP', 'SIGINT', 'SIGBREAK'):
        _sig = getattr(signal, _attr, None)
        if _sig is not None:
            _signal_names[int(_sig)] = _attr
    name = _signal_names.get(signum, f'signal {signum}')
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
            f.write(f"\n=== {name} received · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            if frame is not None:
                f.write('main-thread stack at signal delivery:\n')
                traceback.print_stack(frame, file=f)
            import threading as _threading
            for tid, th in _threading._active.items():
                f.write(f'\n--- thread {th.name} (id={tid}) ---\n')
                f.write(''.join(traceback.format_stack(sys._current_frames().get(tid))))
    except Exception:
        pass
    print(f'[gateway-signal] {name}', file=sys.stderr, flush=True)
    import threading as _threading

    def _hard_exit() -> None:
        os._exit(0)
    timer = _threading.Timer(_shutdown_grace_seconds(), _hard_exit)
    timer.daemon = True
    timer.start()
    try:
        from tui_gateway.server import _shutdown_sessions
        _shutdown_sessions()
    except Exception:
        pass
    try:
        sys.exit(0)
    except SystemExit:
        raise

def _install_signal(signame, handler):
    """Install a signal handler if legal in this thread.

    signal.signal() raises ValueError outside the main thread; skip silently
    there so a worker-thread import of this module (Desktop build path) does
    not abort.  On any main-thread import the handler is installed as before.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    sig = getattr(signal, signame, None)
    if sig is None:
        return
    try:
        signal.signal(sig, handler)
    except (ValueError, OSError, RuntimeError):
        pass
_install_signal('SIGPIPE', signal.SIG_IGN)
_install_signal('SIGTERM', _log_signal)
if hasattr(signal, 'SIGHUP'):
    _install_signal('SIGHUP', _log_signal)
elif hasattr(signal, 'SIGBREAK'):
    _install_signal('SIGBREAK', _log_signal)
_install_signal('SIGINT', signal.SIG_IGN)

def _log_exit(reason: str) -> None:
    """Record why the gateway subprocess is shutting down.

    Three exit paths (startup write fail, parse-error-response write fail,
    dispatch-response write fail, stdin EOF) all collapse into a silent
    sys.exit(0) here.  Without this trail the TUI shows "gateway exited"
    with no actionable clue about WHICH broken pipe or WHICH message
    triggered it — the main reason voice-mode turns look like phantom
    crashes when the real story is "TUI read pipe closed on this event".
    """
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
            f.write(f"\n=== gateway exit · {time.strftime('%Y-%m-%d %H:%M:%S')} · reason={reason} ===\n")
    except Exception:
        pass
    print(f'[gateway-exit] {reason}', file=sys.stderr, flush=True)

def wait_for_mcp_discovery(timeout: 'float | None'=None) -> None:
    """Block until background MCP discovery finishes, up to the resolved bound.

    MCP discovery runs in a daemon thread spawned at startup (see main()) so a
    slow/dead server can't freeze ``gateway.ready``.  But the agent snapshots
    its tool list ONCE at build time and never re-reads it, so a reachable-but-
    slow server that finishes connecting *after* the first prompt would be
    invisible for the whole session.  Joining with a bounded timeout before the
    first agent build lets already-spawning servers land without re-introducing
    the startup hang: ``thread.join(timeout)`` returns the instant discovery
    completes (so fast/no-MCP startups pay ~0s), and a dead server is simply not
    waited on beyond the bound.  No-op when no discovery thread was started.

    The bound comes from ``mcp_discovery_timeout`` in config (shared with the
    CLI path via ``hermes_cli.mcp_startup``); ``timeout`` overrides it.
    """
    thread = _mcp_discovery_thread
    if thread is not None and thread.is_alive():
        try:
            from hermes_cli.mcp_startup import _resolve_discovery_timeout
            bound = _resolve_discovery_timeout(timeout)
        except Exception:
            bound = timeout if timeout is not None else 0.75
        thread.join(timeout=bound)
        return
    if not _mcp_discovery_enabled:
        return
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery
        start_background_mcp_discovery(logger=logger, thread_name='tui-mcp-discovery')
    except Exception:
        logger.debug('TUI MCP discovery retry-spawn failed', exc_info=True)
    try:
        from hermes_cli.mcp_startup import wait_for_mcp_discovery as _startup_wait
        _startup_wait(timeout)
    except Exception:
        pass

def mcp_discovery_in_flight() -> bool:
    """Return True if ANY background MCP discovery thread is still running.

    Used by the agent-build path to decide whether to schedule a late tool
    snapshot refresh: if discovery didn't land within the bounded
    ``wait_for_mcp_discovery`` join, the agent was built without those tools
    and the banner/tool count will be stale until they arrive.

    There are two independent discovery-thread owners by surface: the stdio
    ``duck-agent --tui`` path spawns ITS thread here (``_mcp_discovery_thread``),
    while the desktop app + dashboard WebSocket sidecar (``tui_gateway/ws.py``)
    and ``duck-agent dashboard`` spawn theirs via
    ``hermes_cli.mcp_startup.start_background_mcp_discovery``. The late-refresh
    scheduler imports this function regardless of surface, so it MUST consult
    both — checking only the entry thread left the desktop/dashboard surfaces
    with no late refresh, so a slow MCP server's tools never surfaced for the
    whole session (#51587).
    """
    thread = _mcp_discovery_thread
    if thread is not None and thread.is_alive():
        return True
    try:
        from hermes_cli.mcp_startup import mcp_discovery_in_flight as _startup_in_flight
        return _startup_in_flight()
    except Exception:
        return False

def join_mcp_discovery(timeout: float | None=None) -> bool:
    """Block until background MCP discovery finishes, up to ``timeout`` seconds.

    Returns True if discovery has completed (both thread owners absent or no
    longer alive), False if either is still running after the timeout. Unlike
    ``wait_for_mcp_discovery`` this accepts an unbounded/long wait and reports
    the outcome, for the off-critical-path late-refresh waiter.

    Joins both discovery-thread owners (see ``mcp_discovery_in_flight``): the
    entry thread first, then the ``hermes_cli.mcp_startup`` thread used by the
    desktop/dashboard surfaces. ``timeout`` bounds EACH join, mirroring the
    pre-#51587 single-owner behavior for the entry thread.
    """
    entry_done = True
    thread = _mcp_discovery_thread
    if thread is not None:
        thread.join(timeout=timeout)
        entry_done = not thread.is_alive()
    try:
        from hermes_cli.mcp_startup import join_mcp_discovery as _startup_join
        startup_done = _startup_join(timeout=timeout)
    except Exception:
        startup_done = True
    return entry_done and startup_done
_recovery_times: list[float] = []

def _has_configured_mcp_servers() -> bool:
    """Delegate to the shared native and portable MCP startup gate."""
    from hermes_cli.mcp_startup import _has_configured_mcp_servers as configured
    return configured()

def ensure_mcp_discovery_started() -> None:
    """Start background MCP discovery for the current profile context, once.

    ``main()`` calls this for the stdio/TUI path. WebSocket/Desktop
    entrypoints can accept sessions without running ``main()``, so the
    agent-build path (``server._start_agent_build``) also calls it AFTER
    binding the session profile's DUCK_AGENT_HOME override — the shared owner in
    ``hermes_cli.mcp_startup`` captures the caller's context-local override
    and propagates it into the discovery thread, so discovery reads the
    SELECTED profile's ``mcp_servers``, not the launch profile's (#67605).

    Delegating to the shared owner (instead of a hand-rolled thread) keeps
    the process-wide start lock, the retry-after-zero-connected allowance,
    and interactive-OAuth suppression.

    Known limitation: MCP tool registration is process-global, so in a
    multi-profile process the FIRST profile that builds an agent wins the
    discovery slot. Full per-profile MCP registries are tracked in #67605.
    """
    global _mcp_discovery_enabled
    if not _has_configured_mcp_servers():
        return
    _mcp_discovery_enabled = True
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery
        start_background_mcp_discovery(logger=logger, thread_name='tui-mcp-discovery')
    except Exception:
        logger.warning('Background MCP tool discovery failed to start', exc_info=True)

def main():
    _install_sidecar_publisher()
    ensure_mcp_discovery_started()
    if not write_json({'jsonrpc': '2.0', 'method': 'event', 'params': {'type': 'gateway.ready', 'payload': {'skin': resolve_skin(), 'change_events': True}}}):
        _log_exit('startup write failed (broken stdout pipe before first event)')
        sys.exit(0)
    server._ensure_skin_watcher()
    try:
        from hermes_cli.model_switch import prewarm_picker_cache_async
        prewarm_picker_cache_async()
    except Exception:
        logger.debug('picker cache prewarm (tui) failed to start', exc_info=True)
    while True:
        raw = sys.stdin.readline()
        if not raw:
            if not handle_spurious_eof(_recovery_times, _log_exit):
                break
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            if not write_json({'jsonrpc': '2.0', 'error': {'code': -32700, 'message': 'parse error'}, 'id': None}):
                _log_exit('parse-error-response write failed (broken stdout pipe)')
                sys.exit(0)
            continue
        method = req.get('method') if isinstance(req, dict) else None
        resp = dispatch(req)
        if resp is not None:
            if not write_json(resp):
                _log_exit(f'response write failed for method={method!r} (broken stdout pipe)')
                sys.exit(0)
if __name__ == '__main__':
    main()