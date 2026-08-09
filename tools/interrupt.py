"""Per-thread interrupt signaling for all tools.

Provides thread-scoped interrupt tracking so that interrupting one agent
session does not kill tools running in other sessions.  This is critical
in the gateway where multiple agents run concurrently in the same process.

The agent stores its execution thread ID at the start of run_conversation()
and passes it to set_interrupt()/clear_interrupt().  Tools call
is_interrupted() which checks the CURRENT thread — no argument needed.

Usage in tools:
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"output": "[interrupted]", "returncode": 130}
"""
import logging
import os
import threading
logger = logging.getLogger(__name__)
_DEBUG_INTERRUPT = bool(os.getenv('HERMES_DEBUG_INTERRUPT'))
if _DEBUG_INTERRUPT:
    logger.setLevel(logging.INFO)
_interrupted_threads: set[int] = set()
_lock = threading.Lock()

def set_interrupt(active: bool, thread_id: int | None=None) -> None:
    """Set or clear interrupt for a specific thread.

    Args:
        active: True to signal interrupt, False to clear it.
        thread_id: Target thread ident.  When None, targets the
                   current thread (backward compat for CLI/tests).
    """
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        if active:
            _interrupted_threads.add(tid)
        else:
            _interrupted_threads.discard(tid)
        _snapshot = set(_interrupted_threads) if _DEBUG_INTERRUPT else None
    if _DEBUG_INTERRUPT:
        logger.info('[interrupt-debug] set_interrupt(active=%s, target_tid=%s) called_from_tid=%s current_set=%s', active, tid, threading.current_thread().ident, _snapshot)

def is_interrupted() -> bool:
    """Check if an interrupt has been requested for the current thread.

    Safe to call from any thread — each thread only sees its own
    interrupt state.
    """
    tid = threading.current_thread().ident
    with _lock:
        return tid in _interrupted_threads

def clear_current_thread_interrupt() -> None:
    """Clear any interrupt bit on the CURRENT thread.

    Gives a user-approved command a clean interrupt slate immediately before
    it spawns its child process, so a stale bit that landed on this thread
    during the blocking approval-wait cannot SIGINT the just-approved run
    (exit 130 + "[Command interrupted]").  Single-thread ordering on this tid
    keeps the DO-NOT-BREAK invariant intact: a *genuine* interrupt arriving
    after this call re-sets the bit on the same thread and is still observed by
    the executor's poll loop.  Call this directly, never via the
    _interrupt_event proxy (its .clear() binds to whatever thread runs it).
    """
    set_interrupt(False)

class _ThreadAwareEventProxy:
    """Drop-in proxy that maps threading.Event methods to per-thread state."""

    def is_set(self) -> bool:
        return is_interrupted()

    def set(self) -> None:
        set_interrupt(True)

    def clear(self) -> None:
        set_interrupt(False)

    def wait(self, timeout: float | None=None) -> bool:
        """Not truly supported — returns current state immediately."""
        return self.is_set()
_interrupt_event = _ThreadAwareEventProxy()