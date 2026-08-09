"""Shared spurious stdin-EOF recovery for the TUI gateway entry point and slash worker.

When a child process inherits fd 0 (stdin) and sets ``O_NONBLOCK``, the flag
lands on the **shared open file description** — not just the child's descriptor.
The gateway's next ``read()`` returns ``EAGAIN``, which CPython's buffered
``TextIOWrapper`` converts to ``b''`` (apparent EOF), killing the gateway.

This module provides:
- :func:`diagnose_stdin_state` — forensic diagnostic (``O_NONBLOCK`` / ``SO_RCVTIMEO``)
- :func:`handle_spurious_eof` — check whether an empty ``readline()`` is a genuine
  peer-close or a spurious EOF, and recover if spurious.

The recovery is **POSIX-only** (``fcntl``).  On Windows, ``O_NONBLOCK`` on a
shared file description is not a concern, so the guard simply reports a
genuine EOF and lets the caller exit.
"""
from __future__ import annotations
import os
import time
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:
    _fcntl = None
    _HAS_FCNTL = False
try:
    import socket as _socket
    _HAS_SOCKET = True
except ImportError:
    _socket = None
    _HAS_SOCKET = False
import struct
MAX_RECOVERIES_PER_MINUTE = 10

def diagnose_stdin_state() -> str:
    """Return a diagnostic string about stdin's current state.

    Used for crash-log forensics when stdin iteration falls through.
    Distinguishes genuine peer-close (flag clear) from spurious EOF
    caused by a child setting ``O_NONBLOCK`` on the shared file description.
    """
    parts: list[str] = []
    if _HAS_FCNTL and _fcntl is not None:
        try:
            flags = _fcntl.fcntl(0, _fcntl.F_GETFL)
            parts.append(f"O_NONBLOCK={('1' if flags & os.O_NONBLOCK else '0')}")
        except Exception as e:
            parts.append(f'F_GETFL error: {e}')
    else:
        parts.append('O_NONBLOCK=n/a (no fcntl)')
    if _HAS_SOCKET and _socket is not None:
        try:
            s = _socket.fromfd(0, _socket.AF_UNIX, _socket.SOCK_STREAM)
            try:
                tv = s.getsockopt(_socket.SOL_SOCKET, _socket.SO_RCVTIMEO)
                parts.append(f'SO_RCVTIMEO={tv!r}')
            finally:
                s.close()
        except Exception:
            pass
    return ', '.join(parts) if parts else 'unknown'

def handle_spurious_eof(recovery_times: list[float], log_fn: object) -> bool:
    """Check whether an empty ``readline()`` is spurious; recover if so.

    Returns ``True`` if the caller should ``continue`` the read loop
    (spurious EOF was recovered), ``False`` if it should ``break`` (genuine
    peer-close or rate limit exceeded).

    ``log_fn`` is called with a diagnostic string — ``_log_exit`` in
    ``entry.py``, ``print(file=sys.stderr)`` in ``slash_worker.py``.
    """
    if not (_HAS_FCNTL and _fcntl is not None):
        log_fn('stdin EOF (peer closed)')
        return False
    try:
        flags = _fcntl.fcntl(0, _fcntl.F_GETFL)
        is_nonblock = bool(flags & os.O_NONBLOCK)
    except Exception:
        is_nonblock = False
    if not is_nonblock:
        log_fn('stdin EOF (peer closed)')
        return False
    now = time.time()
    recovery_times.append(now)
    recovery_times[:] = [t for t in recovery_times if t > now - 60]
    if len(recovery_times) > MAX_RECOVERIES_PER_MINUTE:
        log_fn(f'stdin spurious-EOF recovery rate exceeded ({len(recovery_times)}/min, cap {MAX_RECOVERIES_PER_MINUTE})')
        return False
    diag = diagnose_stdin_state()
    log_fn(f'stdin spurious EOF (subprocess O_NONBLOCK flip), recovering: {diag}')
    os.set_blocking(0, True)
    if _HAS_SOCKET and _socket is not None:
        try:
            s = _socket.fromfd(0, _socket.AF_UNIX, _socket.SOCK_STREAM)
            try:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVTIMEO, struct.pack('ll', 0, 0))
            finally:
                s.close()
        except Exception:
            pass
    return True