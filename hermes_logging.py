"""Centralized logging setup for Duck Agent.

Provides a single ``setup_logging()`` entry point that both the CLI and
gateway call early in their startup path.  All log files live under
``~/.duck-agent/logs/`` (profile-aware via ``get_hermes_home()``).

Log files produced:
    agent.log   — INFO+, all agent/tool/session activity (the main log)
    errors.log  — WARNING+, errors and warnings only (quick triage)
    gateway.log — INFO+, gateway-only events (created when mode="gateway")
    gui.log     — INFO+, dashboard/websocket/TUI-gateway events
                  (created when mode="gui")

All files use ``RotatingFileHandler`` with ``RedactingFormatter`` so
secrets are never written to disk.

Component separation:
    gateway.log only receives records from ``gateway.*`` loggers —
    platform adapters, session management, slash commands, delivery.
    gui.log receives dashboard-side records from ``hermes_cli.web_server``,
    ``hermes_cli.pty_bridge``, ``tui_gateway.*``, and ``uvicorn.*``.
    agent.log remains the catch-all (everything goes there).

Session context:
    Call ``set_session_context(session_id)`` at the start of a conversation
    and ``clear_session_context()`` when done.  All log lines emitted on
    that thread will include ``[session_id]`` for filtering/correlation.
"""
import atexit
import copy
import io
import logging
import os
import queue
import sys
import threading
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Optional, Sequence
if sys.platform == 'win32':
    from concurrent_log_handler import ConcurrentRotatingFileHandler as RotatingFileHandler
else:
    from logging.handlers import RotatingFileHandler
from hermes_constants import get_config_path, get_hermes_home
_logging_initialized = False
_session_context = threading.local()
_LOG_FORMAT = '%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s'
_LOG_FORMAT_VERBOSE = '%(asctime)s - %(name)s - %(levelname)s%(session_tag)s - %(message)s'

def _safe_stderr():
    """Return a stderr stream that tolerates Unicode on all platforms.

    On Windows the console encoding is often a legacy MBCS codec
    (cp949, cp1252, …) that raises ``UnicodeEncodeError`` for characters
    like the em-dash (U+2014).  We wrap ``sys.stderr`` in a
    ``TextIOWrapper`` with ``errors='replace'`` so log lines are never
    lost — un-encodable characters are replaced with ``?`` instead of
    crashing the process.
    """
    stream = sys.stderr
    encoding = getattr(stream, 'encoding', None) or 'utf-8'
    if encoding.lower().replace('-', '') in ('utf8', 'utf8surrogateescape'):
        return stream
    try:
        buf = getattr(stream, 'buffer', None)
        if buf is not None:
            wrapped = io.TextIOWrapper(buf, encoding='utf-8', errors='replace', line_buffering=True)
            wrapped.close = lambda: None
            return wrapped
    except Exception:
        pass
    return stream
_CONCURRENT_LOG_LOCK_TIMEOUT = 'Cannot acquire lock after 20 attempts'

def _is_windows_concurrent_log_lock_timeout(exc: BaseException | None) -> bool:
    """Return True for concurrent-log-handler's Windows lock timeout.

    On Windows Desktop, slash-command workers and the gateway can all write to
    the same rotating log files. ``concurrent-log-handler`` serializes rollover
    with a cross-process lock, but when another process holds that lock too
    long it raises this RuntimeError. Logging failures should not escape into
    Desktop chat output.
    """
    return sys.platform == 'win32' and isinstance(exc, RuntimeError) and (_CONCURRENT_LOG_LOCK_TIMEOUT in str(exc))
_NOISY_LOGGERS = ('openai', 'openai._base_client', 'httpx', 'httpcore', 'asyncio', 'hpack', 'hpack.hpack', 'grpc', 'modal', 'urllib3', 'urllib3.connectionpool', 'websockets', 'charset_normalizer', 'markdown_it')

def set_session_context(session_id: str) -> None:
    """Set the session ID for the current thread.

    All subsequent log records on this thread will include ``[session_id]``
    in the formatted output.  Call at the start of ``run_conversation()``.
    """
    _session_context.session_id = session_id

def clear_session_context() -> None:
    """Clear the session ID for the current thread."""
    _session_context.session_id = None

def _install_session_record_factory() -> None:
    """Replace the global LogRecord factory with one that adds ``session_tag``.

    Unlike a ``logging.Filter`` on a handler or logger, the record factory
    runs for EVERY record in the process — including records that propagate
    from child loggers and records handled by third-party handlers.  This
    guarantees ``%(session_tag)s`` is always available in format strings,
    eliminating the KeyError that would occur if a handler used our format
    without having a ``_SessionFilter`` attached.

    Idempotent — checks for a marker attribute to avoid double-wrapping if
    the module is reloaded.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, '_hermes_session_injector', False):
        return

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, 'session_id', None)
        record.session_tag = f' [{sid}]' if sid else ''
        return record
    _session_record_factory._hermes_session_injector = True
    logging.setLogRecordFactory(_session_record_factory)
_install_session_record_factory()

class _ComponentFilter(logging.Filter):
    """Only pass records whose logger name starts with one of *prefixes*.

    Used to route gateway-specific records to ``gateway.log`` while
    keeping ``agent.log`` as the catch-all.
    """

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)
COMPONENT_PREFIXES = {'gateway': ('gateway', 'hermes_plugins', 'plugins.platforms'), 'agent': ('agent', 'run_agent', 'model_tools', 'batch_runner'), 'tools': ('tools',), 'cli': ('hermes_cli', 'cli'), 'cron': ('cron',), 'gui': ('hermes_cli.web_server', 'hermes_cli.pty_bridge', 'tui_gateway', 'uvicorn')}

def setup_logging(*, hermes_home: Optional[Path]=None, log_level: Optional[str]=None, max_size_mb: Optional[int]=None, backup_count: Optional[int]=None, mode: Optional[str]=None, force: bool=False) -> Path:
    """Configure the Duck Agent logging subsystem.

    Safe to call multiple times — the second call is a no-op unless
    *force* is ``True``.

    Parameters
    ----------
    duck_agent_home
        Override for the Duck Agent home directory.  Falls back to
        ``get_hermes_home()`` (profile-aware).
    log_level
        Minimum level for the ``agent.log`` file handler.  Accepts any
        standard Python level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        Defaults to ``"INFO"`` or the value from config.yaml ``logging.level``.
    max_size_mb
        Maximum size of each log file in megabytes before rotation.
        Defaults to 5 or the value from config.yaml ``logging.max_size_mb``.
    backup_count
        Number of rotated backup files to keep.
        Defaults to 3 or the value from config.yaml ``logging.backup_count``.
    mode
        Caller context: ``"cli"``, ``"gateway"``, ``"gui"``, ``"cron"``.
        When ``"gateway"``, an additional ``gateway.log`` file is created
        that receives only gateway-component records.
        When ``"gui"``, an additional ``gui.log`` file is created that
        receives dashboard and TUI-gateway component records.
    force
        Re-run setup even if it has already been called.

    Returns
    -------
    Path
        The ``logs/`` directory where files are written.
    """
    global _logging_initialized
    home = hermes_home or get_hermes_home()
    log_dir = home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    cfg_level, cfg_max_size, cfg_backup = _read_logging_config()
    level_name = (log_level or cfg_level or 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
    backups = backup_count or cfg_backup or 3
    from agent.redact import RedactingFormatter
    root = logging.getLogger()
    _add_rotating_handler(root, log_dir / 'agent.log', level=level, max_bytes=max_bytes, backup_count=backups, formatter=RedactingFormatter(_LOG_FORMAT))
    _add_rotating_handler(root, log_dir / 'errors.log', level=logging.WARNING, max_bytes=2 * 1024 * 1024, backup_count=2, formatter=RedactingFormatter(_LOG_FORMAT))
    if mode == 'gateway':
        _add_rotating_handler(root, log_dir / 'gateway.log', level=logging.INFO, max_bytes=5 * 1024 * 1024, backup_count=3, formatter=RedactingFormatter(_LOG_FORMAT), log_filter=_ComponentFilter(COMPONENT_PREFIXES['gateway']))
    if mode == 'gui':
        _add_rotating_handler(root, log_dir / 'gui.log', level=logging.INFO, max_bytes=10 * 1024 * 1024, backup_count=5, formatter=RedactingFormatter(_LOG_FORMAT), log_filter=_ComponentFilter(COMPONENT_PREFIXES['gui']))
    if _logging_initialized and (not force):
        return log_dir
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _logging_initialized = True
    return log_dir

def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode.

    Called by ``AIAgent.__init__()`` when ``verbose_logging=True``.
    """
    from agent.redact import RedactingFormatter
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and (not isinstance(h, RotatingFileHandler)):
            if getattr(h, '_hermes_verbose', False):
                return
    handler = logging.StreamHandler(_safe_stderr())
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt='%H:%M:%S'))
    handler._hermes_verbose = True
    root.addHandler(handler)
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger('rex-deploy').setLevel(logging.INFO)

class _ManagedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that ensures group-writable perms in managed mode
    AND survives external rotation.

    Two responsibilities:

    1.  In managed mode (NixOS), the stateDir uses setgid (2770) so new files
        inherit the duck-agent group. However, both ``_open()`` (initial creation)
        and ``doRollover()`` create files via ``open()``, which uses the
        process umask — typically 0022, producing 0644. This subclass applies
        ``chmod 0660`` after both operations so the gateway and interactive
        users can share log files.

    2.  ``RotatingFileHandler`` keeps an open file descriptor.  If anything
        rotates the file *externally* (``logrotate``, manual ``mv``,
        another process rotating under us, a transient unlink), our fd
        keeps pointing at the renamed/unlinked inode and every subsequent
        write goes to ``gateway.log.1`` instead of ``gateway.log`` — silent
        log loss for the file every operator expects to read.  Before each
        emit we ``stat`` ``baseFilename`` and compare it against the open
        stream's inode; on mismatch we reopen.  This is the same pattern
        as stdlib ``WatchedFileHandler.reopenIfNeeded()``, adapted for
        rotating handlers.
    """

    def __init__(self, *args, **kwargs):
        from hermes_cli.config import is_managed
        self._managed = is_managed()
        super().__init__(*args, **kwargs)
        self._stat_dev: Optional[int] = None
        self._stat_ino: Optional[int] = None
        self._record_stream_stat()

    def _chmod_if_managed(self):
        if self._managed:
            try:
                os.chmod(self.baseFilename, 432)
            except OSError:
                pass

    def _record_stream_stat(self) -> None:
        """Snapshot dev/ino of ``baseFilename`` so we can detect external rotation."""
        try:
            st = os.stat(self.baseFilename)
            self._stat_dev, self._stat_ino = (st.st_dev, st.st_ino)
        except OSError:
            self._stat_dev, self._stat_ino = (None, None)

    def _reopen_if_externally_rotated(self) -> None:
        """Reopen the stream when ``baseFilename`` no longer matches our fd.

        Triggered when ``baseFilename`` was renamed (logrotate), unlinked,
        or replaced by a different inode.  Silent + best-effort: any error
        falls back to the existing (possibly stale) stream so logging keeps
        working instead of dying on a stat failure.
        """
        try:
            st = os.stat(self.baseFilename)
        except FileNotFoundError:
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            try:
                self.stream = self._open()
                self._record_stream_stat()
            except Exception:
                pass
            return
        except OSError:
            return
        if self._stat_dev is None or self._stat_ino is None:
            self._stat_dev, self._stat_ino = (st.st_dev, st.st_ino)
            return
        if (st.st_dev, st.st_ino) != (self._stat_dev, self._stat_ino):
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            try:
                self.stream = self._open()
                self._stat_dev, self._stat_ino = (st.st_dev, st.st_ino)
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is not None or os.path.exists(self.baseFilename):
            self._reopen_if_externally_rotated()
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """Suppress the known Windows ``concurrent-log-handler`` lock timeout
        instead of printing a traceback.

        CLH's own ``emit()`` wraps its body in ``try/except Exception:
        self.handleError(record)``, so the ``"Cannot acquire lock after N
        attempts"`` RuntimeError raised in ``_do_lock()`` is caught inside CLH
        and routed here — it never propagates out of ``super().emit()``.  This
        override is the single point where that timeout can be silenced before
        the stdlib handler prints it to stderr (which, under the Desktop
        slash-worker, is captured and surfaced into chat output)."""
        exc = sys.exc_info()[1]
        if _is_windows_concurrent_log_lock_timeout(exc):
            return
        super().handleError(record)

    def _open(self):
        stream = super()._open()
        self._chmod_if_managed()
        return stream

    def doRollover(self):
        super().doRollover()
        self._chmod_if_managed()
        self._record_stream_stat()
_log_queue: 'Optional[queue.SimpleQueue]' = None
_queue_listener: Optional[QueueListener] = None
_queued_file_handlers: list = []
_queue_atexit_registered = False
_queue_state_lock = threading.Lock()

class _NonFormattingQueueHandler(QueueHandler):
    """``QueueHandler`` for an in-process queue.

    Stdlib ``prepare()`` formats the record and drops ``args``/``exc_info`` so it
    can be pickled to another process.  Our queue is in-process, so we skip that
    and hand the target file handlers an unformatted record — they apply their
    own ``RedactingFormatter`` and component filters on the listener thread.

    We return a **shallow copy** rather than the original record: the same
    record is still owned by the emitting thread (and any synchronous handler
    on it, e.g. a ``StreamHandler``), which may format/mutate ``record.message``
    while our listener thread reads it. Copying preserves ``msg``/``args``/
    ``exc_info`` for the deferred format while removing the cross-thread
    mutation race on a shared object.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)

def _stop_queue_listener_locked() -> None:
    """Stop the listener assuming ``_queue_state_lock`` is already held."""
    global _queue_listener
    listener, _queue_listener = (_queue_listener, None)
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass

def _stop_queue_listener() -> None:
    """Flush and stop the background log listener (idempotent, thread-safe).

    This is the atexit hook, so it must acquire the state lock itself.
    """
    with _queue_state_lock:
        _stop_queue_listener_locked()

def _register_queued_handler(handler: logging.Handler) -> None:
    """Route *handler* through the shared async queue instead of attaching it to
    *root* directly, so emitting threads never block on file I/O or the
    cross-process rotation lock.  The ``QueueListener`` applies each handler's
    own level and filters on its worker thread."""
    global _log_queue, _queue_listener, _queue_atexit_registered
    with _queue_state_lock:
        if _log_queue is None:
            _log_queue = queue.SimpleQueue()
            qh = _NonFormattingQueueHandler(_log_queue)
            qh._hermes_queue = True
            logging.getLogger().addHandler(qh)
        _queued_file_handlers.append(handler)
        if _queue_listener is not None:
            _queue_listener.stop()
        _queue_listener = QueueListener(_log_queue, *_queued_file_handlers, respect_handler_level=True)
        _queue_listener.start()
        if not _queue_atexit_registered:
            atexit.register(_stop_queue_listener)
            _queue_atexit_registered = True

def flush_log_queue() -> None:
    """Block until all queued records have been written, then resume.

    Draining is done by stopping the listener (which processes every pending
    record before joining) and restarting it.  Used by tests that read a log
    file right after emitting to it.

    NOTE: ``stop()`` joins the worker thread, so this blocks until the queue
    is empty. Do NOT call this on a hard-exit path where the listener may be
    wedged on the rotation lock — use ``drain_log_queue()`` there instead,
    which bounds the wait.
    """
    with _queue_state_lock:
        listener = _queue_listener
        if listener is not None:
            listener.stop()
            listener.start()

def drain_log_queue(timeout: float=1.0) -> None:
    """Best-effort, time-bounded drain for hard-exit paths (no restart).

    Unlike ``flush_log_queue()``, this stops the listener WITHOUT restarting it
    (the process is about to exit) and bounds the drain: if the listener's
    worker thread is wedged on the cross-process rotation lock — the very
    failure this async-logging change exists to survive — an unbounded
    ``stop()``/join would re-freeze the shutdown path. We run ``stop()`` on a
    throwaway thread and only wait ``timeout`` seconds for it; if it hasn't
    drained by then we abandon the last few records and let ``os._exit``
    proceed. Availability beats the last log line when the disk is already
    wedged.
    """
    listener = _queue_listener
    if listener is None:
        return

    def _drain() -> None:
        try:
            listener.stop()
        except Exception:
            pass
    t = threading.Thread(target=_drain, name='duck-agent-log-drain', daemon=True)
    t.start()
    t.join(timeout)

def rotating_file_handlers() -> list:
    """Return the live rotating file handlers.

    They are attached to the async ``QueueListener`` rather than the root
    logger, so callers/tests must use this instead of scanning
    ``logging.getLogger().handlers``."""
    return list(_queued_file_handlers)

def _reset_queued_handlers() -> None:
    """Tear down the async logging queue + listener (test-isolation helper)."""
    global _log_queue
    with _queue_state_lock:
        _stop_queue_listener_locked()
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, '_hermes_queue', False):
                root.removeHandler(h)
        for h in list(_queued_file_handlers):
            try:
                h.close()
            except Exception:
                pass
        _queued_file_handlers.clear()
        _log_queue = None

def _add_rotating_handler(logger: logging.Logger, path: Path, *, level: int, max_bytes: int, backup_count: int, formatter: logging.Formatter, log_filter: Optional[logging.Filter]=None) -> None:
    """Add a ``RotatingFileHandler`` to *logger*, skipping if one already
    exists for the same resolved file path (idempotent).

    Parameters
    ----------
    log_filter
        Optional filter to attach to the handler (e.g. ``_ComponentFilter``
        for gateway.log).
    """
    resolved = path.resolve()
    for existing in _queued_file_handlers:
        if isinstance(existing, RotatingFileHandler) and Path(getattr(existing, 'baseFilename', '')).resolve() == resolved:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _ManagedRotatingFileHandler(str(path), maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if log_filter is not None:
        handler.addFilter(log_filter)
    _register_queued_handler(handler)

def _read_logging_config():
    """Best-effort read of ``logging.*`` from config.yaml.

    Returns ``(level, max_size_mb, backup_count)`` — any may be ``None``.
    """
    try:
        try:
            from hermes_cli.config import read_raw_config as _rrc
            cfg = _rrc() or {}
        except Exception:
            from utils import fast_safe_load
            config_path = get_config_path()
            if not config_path.exists():
                return (None, None, None)
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = fast_safe_load(f) or {}
        if cfg:
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            log_cfg = cfg.get('logging', {})
            if isinstance(log_cfg, dict):
                return (log_cfg.get('level'), log_cfg.get('max_size_mb'), log_cfg.get('backup_count'))
    except Exception:
        pass
    return (None, None, None)