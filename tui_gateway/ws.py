"""WebSocket transport for the tui_gateway JSON-RPC server.

Reuses :func:`tui_gateway.server.dispatch` verbatim so every RPC method, every
slash command, every approval/clarify/sudo flow, and every agent event flows
through the same handlers whether the client is Ink over stdio or an iOS /
web client over WebSocket.

Wire protocol
-------------
Identical to stdio: newline-delimited JSON-RPC in both directions. The server
emits a ``gateway.ready`` event immediately after connection accept, then
echoes responses/events for inbound requests. No framing differences.

Mounting
--------
    from fastapi import WebSocket
    from tui_gateway.ws import handle_ws

    @app.websocket("/api/ws")
    async def ws(ws: WebSocket):
        await handle_ws(ws)
"""
from __future__ import annotations
import asyncio
import concurrent.futures
import json
import logging
import socket
import threading
from typing import Any
from tui_gateway import server
_log = logging.getLogger(__name__)
_WS_WRITE_TIMEOUT_S = 10.0
_WS_LOG_PAYLOAD_PREVIEW = 240
_STREAMING_EVENT_TYPES = frozenset({'message.delta', 'reasoning.delta', 'thinking.delta'})
_TOKEN_COALESCE_S = 0.033
try:
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect
except ImportError:
    _WebSocketDisconnect = Exception

class WSTransport:
    """Per-connection WS transport.

    ``write`` is safe to call from any thread *other than* the event loop
    thread that owns the socket. Pool workers (the only real caller) run in
    their own threads, so marshalling onto the loop via
    :func:`asyncio.run_coroutine_threadsafe` + ``future.result()`` is correct
    and deadlock-free there.

    When called from the loop thread itself (e.g. by ``handle_ws`` for an
    inline response) the same call would deadlock: we'd schedule work onto
    the loop we're currently blocking. We detect that case and fire-and-
    forget instead. Callers that need to know when the bytes are on the wire
    should use :meth:`write_async` from the loop thread.
    """

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop, *, peer: str='unknown') -> None:
        self._ws = ws
        self._loop = loop
        self._peer = peer
        self._closed = False
        self._token_lock = threading.Lock()
        self._pending_tokens: list[str] = []
        self._token_flush_handle: asyncio.TimerHandle | None = None
        self._token_flush_armed = False
        self._send_lock = asyncio.Lock()

    @staticmethod
    def _is_streaming_frame(obj: dict) -> bool:
        """True for high-frequency per-token frames eligible for coalescing."""
        params = obj.get('params') if isinstance(obj, dict) else None
        if not isinstance(params, dict):
            return False
        return params.get('type') in _STREAMING_EVENT_TYPES

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        line = json.dumps(obj, ensure_ascii=False)
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if self._is_streaming_frame(obj):
            with self._token_lock:
                self._pending_tokens.append(line)
                if not self._token_flush_armed:
                    self._token_flush_armed = True
                    self._loop.call_soon_threadsafe(self._arm_token_flush)
            return not self._closed
        from agent.async_utils import safe_schedule_threadsafe
        with self._token_lock:
            self._pending_tokens.append(line)
            batch = self._pending_tokens
            self._pending_tokens = []
            if on_loop:
                self._loop.create_task(self._safe_send_many(batch))
                return True
            fut = safe_schedule_threadsafe(self._safe_send_many(batch), self._loop)
            if fut is None:
                self._closed = True
                return False
        try:
            fut.result(timeout=_WS_WRITE_TIMEOUT_S)
            return not self._closed
        except concurrent.futures.TimeoutError:
            _log.warning('ws write slow (loop stalled >%ss) peer=%s — frame left in flight', _WS_WRITE_TIMEOUT_S, self._peer)
            return not self._closed
        except Exception as exc:
            self._closed = True
            _log.warning('ws write failed peer=%s error_type=%s error=%s', self._peer, type(exc).__name__, exc)
            return False

    def _arm_token_flush(self) -> None:
        """Arm the coalesce timer. Runs on the loop thread (call_soon_threadsafe)."""
        if self._closed:
            return
        self._token_flush_handle = self._loop.call_later(_TOKEN_COALESCE_S, self._flush_tokens)

    def _flush_tokens(self) -> None:
        """Send buffered tokens as one batch. Runs on the loop thread (timer).

        The send is scheduled under the lock so its wire order is fixed relative
        to a concurrent non-streaming flush in :meth:`write`.
        """
        with self._token_lock:
            self._token_flush_handle = None
            self._token_flush_armed = False
            if not self._pending_tokens or self._closed:
                self._pending_tokens = []
                return
            batch = self._pending_tokens
            self._pending_tokens = []
            self._loop.create_task(self._safe_send_many(batch))

    async def write_async(self, obj: dict) -> bool:
        """Send from the owning event loop. Awaits until the frame is on the wire."""
        if self._closed:
            return False
        with self._token_lock:
            batch = self._pending_tokens
            self._pending_tokens = []
            batch.append(json.dumps(obj, ensure_ascii=False))
        await self._safe_send_many(batch)
        return not self._closed

    async def _safe_send_many(self, lines: list[str]) -> None:
        """Send one indivisible batch of pre-serialized frames in wire order."""
        async with self._send_lock:
            if self._closed:
                return
            try:
                for line in lines:
                    if self._closed:
                        return
                    await self._ws.send_text(line)
            except Exception as exc:
                self._closed = True
                _log.warning('ws send failed peer=%s error_type=%s error=%s', self._peer, type(exc).__name__, exc)

    def close(self) -> None:
        self._closed = True
        handle = self._token_flush_handle
        if handle is not None:
            handle.cancel()
            self._token_flush_handle = None

def _ws_peer_label(ws: Any) -> str:
    """Return ``host:port`` when available, else a stable placeholder."""
    client = getattr(ws, 'client', None)
    if client is None:
        return 'unknown'
    host = getattr(client, 'host', None) or 'unknown'
    port = getattr(client, 'port', None)
    return f'{host}:{port}' if port is not None else host

def _disable_nagle(ws: Any) -> None:
    """Disable Nagle so streamed JSON-RPC frames go out individually.

    Without it the kernel coalesces the small per-token frames, so a burst after
    the model's think-pause lands on the client in one tick and no client-side
    smoothing can recover the cadence. GUI/WS only; chat platforms don't hit
    this path. Best-effort — skip silently if the socket isn't reachable.
    """
    try:
        scope = getattr(ws, 'scope', None) or {}
        transport = (scope.get('extensions') or {}).get('transport') or getattr(ws, 'transport', None)
        sock = transport.get_extra_info('socket') if transport is not None else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as exc:
        _log.debug('ws TCP_NODELAY skip: %s', exc)

async def handle_ws(ws: Any) -> None:
    """Run one WebSocket session. Wire-compatible with ``tui_gateway.entry``."""
    peer = _ws_peer_label(ws)
    transport: WSTransport | None = None
    messages = 0
    parse_errors = 0
    dispatch_crashes = 0
    send_failures = 0
    disconnect_reason = 'not_connected'
    try:
        await ws.accept()
        disconnect_reason = 'connected'
        _disable_nagle(ws)
        _log.info('ws accepted peer=%s', peer)
        transport = WSTransport(ws, asyncio.get_running_loop(), peer=peer)
        skin_payload = await asyncio.to_thread(server.resolve_skin)
        ready_ok = await transport.write_async({'jsonrpc': '2.0', 'method': 'event', 'params': {'type': 'gateway.ready', 'payload': {'skin': skin_payload, 'change_events': True}}})
        if ready_ok:
            server._ensure_skin_watcher()
            server.register_live_transport(transport)
        if not ready_ok:
            disconnect_reason = 'ready_send_failed'
            send_failures += 1
            _log.error('ws ready frame send failed peer=%s', peer)
            return
        while True:
            try:
                raw = await ws.receive_text()
            except _WebSocketDisconnect as exc:
                disconnect_reason = f"client_disconnect(code={getattr(exc, 'code', None)},reason={getattr(exc, 'reason', None)})"
                break
            except Exception:
                disconnect_reason = 'receive_failed'
                _log.exception('ws receive failed peer=%s', peer)
                break
            line = raw.strip()
            if not line:
                continue
            messages += 1
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors += 1
                _log.warning('ws parse error peer=%s index=%d error=%s payload=%r', peer, messages, exc, line[:_WS_LOG_PAYLOAD_PREVIEW])
                ok = await transport.write_async({'jsonrpc': '2.0', 'error': {'code': -32700, 'message': 'parse error'}, 'id': None})
                if not ok:
                    disconnect_reason = 'send_failed_after_parse_error'
                    send_failures += 1
                    _log.warning('ws parse-error reply send failed peer=%s', peer)
                    break
                continue
            req_id = req.get('id') if isinstance(req, dict) else None
            req_method = req.get('method') if isinstance(req, dict) else None
            try:
                resp = await asyncio.to_thread(server.dispatch, req, transport)
            except Exception:
                dispatch_crashes += 1
                _log.exception('ws dispatch crash peer=%s id=%s method=%s', peer, req_id, req_method)
                ok = await transport.write_async({'jsonrpc': '2.0', 'error': {'code': -32603, 'message': 'internal error'}, 'id': req_id if req_id is not None else None})
                if not ok:
                    disconnect_reason = 'send_failed_after_dispatch_crash'
                    send_failures += 1
                    _log.warning('ws dispatch-crash reply send failed peer=%s id=%s method=%s', peer, req_id, req_method)
                    break
                continue
            if resp is not None and (not await transport.write_async(resp)):
                disconnect_reason = 'send_failed_after_response'
                send_failures += 1
                _log.warning('ws response send failed peer=%s id=%s method=%s', peer, req_id, req_method)
                break
    finally:
        reaped_sessions = 0
        detached_sessions = 0
        if transport is not None:
            server.unregister_live_transport(transport)
            transport.close()
            try:
                await asyncio.to_thread(server._release_wake_for_transport, transport)
            except Exception:
                _log.exception('ws wake-word teardown failed peer=%s', peer)
            try:
                reaped_sessions, detached_sessions = await asyncio.to_thread(server._close_sessions_for_transport, transport, end_reason='ws_disconnect')
            except Exception:
                _log.exception('ws transport teardown failed peer=%s', peer)
        try:
            await ws.close()
        except Exception as exc:
            _log.debug('ws close failed peer=%s error=%s', peer, exc)
        _log.info('ws closed peer=%s reason=%s messages=%d parse_errors=%d dispatch_crashes=%d send_failures=%d reaped_sessions=%d detached_sessions=%d', peer, disconnect_reason, messages, parse_errors, dispatch_crashes, send_failures, reaped_sessions, detached_sessions)