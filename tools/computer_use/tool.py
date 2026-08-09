"""Entry point for the `computer_use` tool.

Universal (any-model) desktop control across macOS, Windows, and Linux via
cua-driver's background computer-use primitive. Replaces #4562's
Anthropic-native `computer_20251124` approach — the schema here is standard
OpenAI function-calling so every tool-capable model can drive it.

Linux is the most recent runtime (X11 + Wayland, via cua-driver-rs's
AT-SPI tree path); it is enabled here alongside macOS and Windows. When a
host's display server or accessibility stack isn't reachable, cua-driver's
`health_report` (surfaced by `duck-agent computer-use doctor`) reports the
exact blocked check rather than the toolset silently failing.

Return contract
---------------
For text-only results (wait, key, list_apps, focus_app, failures, etc.):
  JSON string.

For captures / actions with `capture_after=True`:
  A dict wrapped as the OpenAI-style multi-part tool-message content:

      {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "<human-readable summary + SOM index>"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<b64>"}},
        ],
        "text_summary": "<text used for fallback string content>",
      }

  run_agent.py's tool-message builder inspects `_multimodal` and emits a
  list-shaped `content` for OpenAI-compatible providers. The Anthropic
  adapter splices the base64 image into a `tool_result` block (see
  `agent/anthropic_adapter.py`). Every provider that supports multi-part
  tool content gets the image; text-only providers see the summary only.
"""
from __future__ import annotations
import atexit
import base64
import json
import logging
import os
import re
import struct
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple
from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend, UIElement
logger = logging.getLogger(__name__)
_approval_callback = None

def set_approval_callback(cb) -> None:
    """Register a callback for computer_use approval prompts (used by CLI).

    Matches the terminal_tool._approval_callback pattern. The callback
    receives (action, args, summary) and returns one of:
      "approve_once" | "approve_session" | "always_approve" | "deny".
    """
    global _approval_callback
    _approval_callback = cb
_SAFE_ACTIONS = frozenset({'capture', 'wait', 'list_apps', 'list_windows', 'cua_browser_state'})
_DESTRUCTIVE_ACTIONS = frozenset({'click', 'double_click', 'right_click', 'middle_click', 'drag', 'scroll', 'type', 'key', 'set_value', 'focus_app', 'cua_browser_prepare', 'cua_browser_navigate', 'cua_browser_click', 'cua_browser_type', 'cua_browser_pointer', 'cua_browser_dialog', 'cua_browser_set_input_files', 'cua_browser_download'})
_BLOCKED_KEY_COMBOS = {frozenset({'cmd', 'shift', 'backspace'}), frozenset({'cmd', 'option', 'backspace'}), frozenset({'cmd', 'ctrl', 'q'}), frozenset({'cmd', 'shift', 'q'}), frozenset({'cmd', 'option', 'shift', 'q'}), frozenset({'win', 'l'}), frozenset({'ctrl', 'option', 'delete'}), frozenset({'ctrl', 'option', 'del'}), frozenset({'option', 'f4'})}
_KEY_ALIASES = {'command': 'cmd', 'control': 'ctrl', 'alt': 'option', '⌘': 'cmd', '⌥': 'option', 'windows': 'win', 'super': 'win', 'meta': 'win'}

def _canon_key_combo(keys: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split('\\s*[+\\-]\\s*', keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)
_BLOCKED_TYPE_PATTERNS = [re.compile('curl\\s+[^|]*\\|\\s*bash', re.IGNORECASE), re.compile('curl\\s+[^|]*\\|\\s*sh', re.IGNORECASE), re.compile('wget\\s+[^|]*\\|\\s*bash', re.IGNORECASE), re.compile('\\bsudo\\s+rm\\s+-[rf]', re.IGNORECASE), re.compile('\\brm\\s+-rf\\s+/\\s*$', re.IGNORECASE), re.compile(':\\s*\\(\\)\\s*\\{\\s*:\\|:\\s*&\\s*\\}', re.IGNORECASE)]

def _is_blocked_type(text: str) -> Optional[str]:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None
_backend_lock = threading.Lock()
_AUX_VISION_ROUTE_CACHE: Dict[Tuple[str, str], bool] = {}
_backend: Optional[ComputerUseBackend] = None
_backends: Dict[str, ComputerUseBackend] = {}
_backend_call_locks: Dict[str, threading.RLock] = {}
_backend_permission_modes: Dict[str, str] = {}
_approval_lock = threading.Lock()
_session_auto_approve: Dict[str, bool] = {}
_always_allow: Dict[str, set] = {}

def _cua_permission_mode(session_id: str) -> str:
    """Map Duck Agent'ss explicit approval bypass onto Cua's immutable mode.

    Duck Agent has TWO session-identity namespaces: the tool-dispatch path passes
    the DB ``session_id`` (``agent.session_id``), while gateway ``/yolo``
    keys approval state off the gateway ``session_key`` (set per turn via the
    ``set_current_session_key`` contextvar in tools/approval.py). CLI and TUI
    use the DB id for both. Checking ONLY ``session_id`` here would make a
    gateway ``/yolo`` toggle silently invisible to computer_use (works in
    CLI, dead on messaging platforms), so we consult both namespaces —
    bypass in either means the user explicitly opted out of approvals for
    this run. Fails closed on any resolution error.
    """
    try:
        from tools.approval import get_current_session_key, is_approval_bypass_active_for_session
        if is_approval_bypass_active_for_session(session_id):
            return 'unrestricted'
        current_key = get_current_session_key(default='')
        if current_key and is_approval_bypass_active_for_session(current_key):
            return 'unrestricted'
    except Exception:
        pass
    return 'standard'

def _get_backend(session_id: str='') -> ComputerUseBackend:
    global _backend
    sid = str(session_id or '')
    while True:
        stale_backend: Optional[ComputerUseBackend] = None
        stale_lock: Optional[threading.RLock] = None
        with _backend_lock:
            permission_mode = _cua_permission_mode(sid)
            if sid == '' and _backend is not None and (sid not in _backends):
                _backends[sid] = _backend
                _backend_call_locks[sid] = threading.RLock()
                _backend_permission_modes[sid] = permission_mode
            cached = _backends.get(sid)
            if cached is not None:
                if _backend_permission_modes.get(sid, 'standard') == permission_mode:
                    return cached
                stale_backend = _backends.pop(sid)
                stale_lock = _backend_call_locks.pop(sid, None)
                _backend_permission_modes.pop(sid, None)
                if sid == '':
                    _backend = None
            else:
                backend_name = os.environ.get('HERMES_COMPUTER_USE_BACKEND', 'cua').lower()
                if backend_name in {'cua', 'cua-driver', ''}:
                    from tools.computer_use.cua_backend import CuaDriverBackend
                    backend = CuaDriverBackend(permission_mode=permission_mode)
                elif backend_name == 'noop':
                    backend = _NoopBackend()
                else:
                    raise RuntimeError(f'Unknown HERMES_COMPUTER_USE_BACKEND={backend_name!r}')
                backend.start()
                _backends[sid] = backend
                _backend_call_locks[sid] = threading.RLock()
                _backend_permission_modes[sid] = permission_mode
                if sid == '':
                    _backend = backend
                return backend
        try:
            if stale_lock is not None:
                with stale_lock:
                    stale_backend.stop()
            elif stale_backend is not None:
                stale_backend.stop()
        except Exception:
            pass

def release_computer_use_session(session_id: str) -> bool:
    """Release one session-owned computer-use backend.

    This is the production lifecycle seam for hosts and policy plugins. It
    removes the exact session backend, its call lock, and its recorded
    permission mode before stopping the backend, so new lookups cannot retain
    the stale target/ref namespace — and stops a private embedded daemon when
    Duck Agent YOLO selected unrestricted mode. Approval state is cleared even
    when no backend was started.

    Returns ``True`` when a backend was found and released, ``False`` when the
    session was already absent. Safe to call repeatedly.
    """
    global _backend
    sid = str(session_id or '')
    with _backend_lock:
        backend = _backends.pop(sid, None)
        call_lock = _backend_call_locks.pop(sid, None)
        _backend_permission_modes.pop(sid, None)
        if sid == '' and backend is None:
            backend = _backend
        if sid == '' and _backend is backend:
            _backend = None
    with _approval_lock:
        _session_auto_approve.pop(sid, None)
        _always_allow.pop(sid, None)
    if backend is None:
        return False
    try:
        if call_lock is not None:
            with call_lock:
                backend.stop()
        else:
            backend.stop()
    except Exception:
        logger.debug('computer_use backend release failed for session %s', sid, exc_info=True)
    return True

def _shutdown_backend_atexit() -> None:
    """Stop all cached backends so cua-driver children don't outlive us.

    Each session backend holds a long-lived ``cua-driver`` subprocess, so
    without this a driver can survive the Duck Agent process that spawned it
    (#28152 item 3). #69903 kept the orphan from burning a core by disabling
    the cursor overlay; the process itself still lingered.

    Mirrors ``browser_tool``'s ``atexit.register(_emergency_cleanup_all_sessions)``
    — same spawn-and-drive-a-subprocess shape. atexit only, no signal handlers:
    a ``SystemExit`` raised from a prompt_toolkit key binding corrupts its
    coroutine state and makes the process unkillable. Never raises, since an
    exception escaping atexit prints a traceback on every exit.
    """
    global _backend
    with _backend_lock:
        unique = {id(backend): (backend, _backend_call_locks.get(sid)) for sid, backend in _backends.items()}
        if _backend is not None:
            unique.setdefault(id(_backend), (_backend, _backend_call_locks.get('')))
        _backend = None
        _backends.clear()
        _backend_call_locks.clear()
        _backend_permission_modes.clear()
    with _approval_lock:
        _session_auto_approve.clear()
        _always_allow.clear()
    for backend, call_lock in unique.values():
        try:
            if call_lock is not None:
                with call_lock:
                    backend.stop()
            else:
                backend.stop()
        except Exception as e:
            logger.debug('cua-driver atexit teardown failed: %s', e)
atexit.register(_shutdown_backend_atexit)

def reset_backend_for_tests() -> None:
    """Test helper — tear down the cached backend and per-session state."""
    _shutdown_backend_atexit()
    _AUX_VISION_ROUTE_CACHE.clear()

class _NoopBackend(ComputerUseBackend):
    """Test/CI stub. Records calls; returns trivial results."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._started = False

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def is_available(self) -> bool:
        return True

    def capture(self, mode: str='som', app: Optional[str]=None, pid: Optional[int]=None, window_id: Optional[int]=None) -> CaptureResult:
        self.calls.append(('capture', {'mode': mode, 'app': app, 'pid': pid, 'window_id': window_id}))
        return CaptureResult(mode=mode, width=1024, height=768, png_b64=None, elements=[], app=app or '', window_title='')

    def click(self, **kw) -> ActionResult:
        self.calls.append(('click', kw))
        return ActionResult(ok=True, action='click')

    def drag(self, **kw) -> ActionResult:
        self.calls.append(('drag', kw))
        return ActionResult(ok=True, action='drag')

    def scroll(self, **kw) -> ActionResult:
        self.calls.append(('scroll', kw))
        return ActionResult(ok=True, action='scroll')

    def type_text(self, text: str, **kw) -> ActionResult:
        self.calls.append(('type', {'text': text, **kw}))
        return ActionResult(ok=True, action='type')

    def key(self, keys: str, **kw) -> ActionResult:
        self.calls.append(('key', {'keys': keys, **kw}))
        return ActionResult(ok=True, action='key')

    def list_apps(self) -> List[Dict[str, Any]]:
        self.calls.append(('list_apps', {}))
        return []

    def list_windows(self) -> List[Dict[str, Any]]:
        self.calls.append(('list_windows', {}))
        return []

    def focus_app(self, app: str, raise_window: bool=False) -> ActionResult:
        self.calls.append(('focus_app', {'app': app, 'raise': raise_window}))
        return ActionResult(ok=True, action='focus_app')

    def set_value(self, value: str, element: Optional[int]=None) -> ActionResult:
        self.calls.append(('set_value', {'value': value, 'element': element}))
        return ActionResult(ok=True, action='set_value')

def handle_computer_use(args: Dict[str, Any], **kwargs) -> Any:
    """Main entry point — dispatched by tools.registry.

    Returns either a JSON string (text-only) or a dict marked `_multimodal`
    (image + summary) which run_agent.py wraps into the tool message.
    """
    action = (args.get('action') or '').strip().lower()
    if not action:
        return json.dumps({'error': 'missing `action`'})
    session_id = str(kwargs.get('session_id') or '')
    if action in {'type', 'cua_browser_type'}:
        text = args.get('text', '')
        pat = _is_blocked_type(text)
        if pat:
            return json.dumps({'error': f'blocked pattern in type text: {pat!r}', 'hint': 'Dangerous shell patterns cannot be typed via computer_use.'})
    if action == 'key':
        keys = args.get('keys', '')
        combo = _canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo) and len(blocked) <= len(combo):
                return json.dumps({'error': f'blocked key combo: {sorted(blocked)}', 'hint': 'Destructive system shortcuts are hard-blocked.'})
    if args.get('bring_to_front') and args.get('delivery_mode') != 'foreground':
        return json.dumps({'error': "bring_to_front requires delivery_mode='foreground'", 'code': 'bring_to_front_requires_foreground'})
    if action in _DESTRUCTIVE_ACTIONS:
        err = _request_approval(action, args, session_id)
        if err is not None:
            return err
    if args.get('bring_to_front') or (action == 'focus_app' and args.get('raise_window')):
        err = _request_approval('bring_to_front', args, session_id)
        if err is not None:
            return err
    try:
        backend = _get_backend(session_id=session_id)
    except Exception as e:
        return json.dumps({'error': f'computer_use backend unavailable: {e}', 'hint': 'If the cua-driver binary is missing, run `duck-agent computer-use install`. If a Python dependency is missing, the error above shows the exact install command.'})
    try:
        with _backend_lock:
            call_lock = _backend_call_locks.setdefault(session_id, threading.RLock())
        with call_lock:
            return _dispatch(backend, action, args)
    except Exception as e:
        logger.exception('computer_use %s failed', action)
        return json.dumps({'error': f'{action} failed: {e}'})

def _request_approval(action: str, args: Dict[str, Any], session_id: str='') -> Optional[str]:
    """Return None if approved, or a JSON error string if denied.

    Approval is scoped by (action, delivery_mode) AND by session_id.
    Foreground delivery is a visible focus change, so a prior background
    approval — even ``approve_session`` on the same action — must NOT
    silently authorize it (NousResearch/duck-agent#67052).
    ``always_approve`` (the blanket "auto-approve everything" unlock) still
    covers foreground, since the user explicitly opted into unattended
    operation. State is keyed on session_id so concurrent runs don't leak
    unlocks into one another.
    """
    is_foreground = args.get('delivery_mode') == 'foreground'
    scope_key = (action, 'foreground' if is_foreground else 'background')
    with _approval_lock:
        if _session_auto_approve.get(session_id):
            return None
        if scope_key in _always_allow.get(session_id, set()):
            return None
    cb = _approval_callback
    if cb is None:
        return None
    summary = _summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as e:
        logger.warning('approval callback failed: %s', e)
        verdict = 'deny'
    if verdict == 'approve_once':
        return None
    if verdict == 'approve_session' or verdict == 'always_approve':
        with _approval_lock:
            _always_allow.setdefault(session_id, set()).add(scope_key)
            if verdict == 'always_approve':
                _session_auto_approve[session_id] = True
        return None
    if verdict == 'timeout':
        return json.dumps({'error': 'approval prompt timed out — the user did not respond. Silence is not consent; do not retry without the user.', 'action': action})
    return json.dumps({'error': 'denied by user', 'action': action})

def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    fg = ' [FOREGROUND — briefly raises the window / changes focus]' if args.get('delivery_mode') == 'foreground' else ''
    if action in {'click', 'double_click', 'right_click', 'middle_click'}:
        if args.get('element') is not None:
            return f"{action} element #{args['element']}{fg}"
        coord = args.get('coordinate')
        if coord:
            return f'{action} at {tuple(coord)}{fg}'
        return action + fg
    if action == 'drag':
        src = args.get('from_element') or args.get('from_coordinate')
        dst = args.get('to_element') or args.get('to_coordinate')
        return f'drag {src} → {dst}{fg}'
    if action == 'scroll':
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}{fg}"
    if action == 'type':
        text = args.get('text', '')
        return f'type {text[:60]!r}' + ('...' if len(text) > 60 else '') + fg
    if action == 'key':
        return f"key {args.get('keys', '')!r}{fg}"
    if action == 'focus_app':
        return f"focus {args.get('app', '')!r}" + (' (raise)' if args.get('raise_window') else '')
    return action + fg

def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get('capture_after'))
    if action == 'capture':
        mode = str(args.get('mode', 'som'))
        if mode not in {'som', 'vision', 'ax'}:
            return json.dumps({'error': f'bad mode {mode!r}; use som|vision|ax'})
        capture_kwargs: Dict[str, Any] = {'mode': mode, 'app': args.get('app')}
        if args.get('pid') is not None or args.get('window_id') is not None:
            capture_kwargs.update({'pid': args.get('pid'), 'window_id': args.get('window_id')})
        cap = backend.capture(**capture_kwargs)
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get('max_elements')))
    if action == 'wait':
        seconds = float(args.get('seconds', 1.0))
        res = backend.wait(seconds)
        return _text_response(res)
    if action == 'list_apps':
        apps = backend.list_apps()
        return json.dumps({'apps': apps, 'count': len(apps)})
    if action == 'list_windows':
        windows = backend.list_windows()
        return json.dumps({'windows': windows, 'count': len(windows)})
    if action == 'focus_app':
        app = args.get('app')
        if not app:
            return json.dumps({'error': 'focus_app requires `app`'})
        res = backend.focus_app(app, raise_window=bool(args.get('raise_window')))
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'cua_browser_state':
        state_args: Dict[str, Any] = {}
        for public, internal in (('pid', 'pid'), ('window_id', 'window_id'), ('tab_id', 'tab_id'), ('snapshot_format', 'snapshot_format'), ('query', 'query'), ('scope_ref', 'scope_ref'), ('continuation', 'continuation')):
            if args.get(public) is not None:
                state_args[internal] = args[public]
        return json.dumps(backend.typed_browser_state(**state_args))
    if action == 'cua_browser_prepare':
        return json.dumps(backend.typed_browser_prepare(pid=args.get('pid'), window_id=args.get('window_id'), profile_mode=args.get('profile_mode', 'isolated_new'), profile_name=args.get('profile_name'), allow_launch=bool(args.get('allow_launch'))))
    browser_tools = {'cua_browser_navigate': 'browser_navigate', 'cua_browser_click': 'browser_click', 'cua_browser_type': 'browser_type', 'cua_browser_pointer': 'browser_pointer', 'cua_browser_dialog': 'browser_dialog', 'cua_browser_set_input_files': 'browser_set_input_files', 'cua_browser_download': 'browser_download'}
    driver_tool = browser_tools.get(action)
    if driver_tool is not None:
        call_args: Dict[str, Any] = {}
        allowed_fields = {'browser_navigate': ('url',), 'browser_click': ('ref', 'input_route', 'x', 'y'), 'browser_type': ('ref', 'text'), 'browser_pointer': ('ref', 'destination_ref', 'input_route', 'x', 'y', 'to_x', 'to_y', 'delta_x', 'delta_y'), 'browser_dialog': ('dialog_id', 'prompt_text', 'delivery_mode'), 'browser_set_input_files': ('ref', 'files'), 'browser_download': ('ref', 'destination_root')}
        for field in allowed_fields[driver_tool]:
            if args.get(field) is not None:
                call_args[field] = args[field]
        if driver_tool in {'browser_click', 'browser_pointer'} and args.get('coordinate') is not None:
            coordinate = args['coordinate']
            if isinstance(coordinate, (list, tuple)) and len(coordinate) == 2:
                call_args['x'], call_args['y'] = coordinate
        pointer_action = args.get('browser_pointer_action')
        dialog_action = args.get('browser_dialog_action')
        nested_action = args.get('action')
        if nested_action not in browser_tools:
            if driver_tool == 'browser_pointer' and pointer_action is None:
                pointer_action = nested_action
            if driver_tool == 'browser_dialog' and dialog_action is None:
                dialog_action = nested_action
        if pointer_action is not None:
            call_args['action'] = pointer_action
        if dialog_action is not None:
            call_args['action'] = dialog_action
        if args.get('browser_type_mode') is not None:
            call_args['mode'] = args['browser_type_mode']
        return json.dumps(backend.typed_browser_action(driver_tool, tab_id=args.get('tab_id'), args=call_args))
    delivery_mode = args.get('delivery_mode')
    bring_to_front = bool(args.get('bring_to_front'))
    if action in {'click', 'double_click', 'right_click', 'middle_click'}:
        button = args.get('button')
        click_count = 1
        if action == 'double_click':
            click_count = 2
        elif action == 'right_click':
            button = 'right'
        elif action == 'middle_click':
            button = 'middle'
        else:
            button = button or 'left'
        element = args.get('element')
        coord = args.get('coordinate') or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(element=element if element is not None else None, x=x, y=y, button=button or 'left', click_count=click_count, modifiers=args.get('modifiers'), delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'drag':
        has_elements = args.get('from_element') is not None and args.get('to_element') is not None
        has_coords = args.get('from_coordinate') and args.get('to_coordinate')
        if not has_elements and (not has_coords):
            return json.dumps({'error': 'drag requires from_coordinate/to_coordinate or from_element/to_element'})
        res = backend.drag(from_element=args.get('from_element'), to_element=args.get('to_element'), from_xy=tuple(args['from_coordinate']) if args.get('from_coordinate') else None, to_xy=tuple(args['to_coordinate']) if args.get('to_coordinate') else None, button=args.get('button', 'left'), modifiers=args.get('modifiers'), delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'scroll':
        coord = args.get('coordinate') or (None, None)
        res = backend.scroll(direction=args.get('direction', 'down'), amount=int(args.get('amount', 3)), element=args.get('element'), x=coord[0] if coord and coord[0] is not None else None, y=coord[1] if coord and coord[1] is not None else None, modifiers=args.get('modifiers'), delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'type':
        res = backend.type_text(args.get('text', ''), delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'key':
        res = backend.key(args.get('keys', ''), delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)
    if action == 'set_value':
        value = args.get('value')
        if value is None:
            return json.dumps({'error': 'set_value requires `value`'})
        res = backend.set_value(value=str(value), element=args.get('element'))
        return _maybe_follow_capture(backend, res, capture_after)
    return json.dumps({'error': f'unknown action {action!r}'})

def _classify_action_result(res: ActionResult) -> Dict[str, Any]:
    """Choose the next ladder step from semantic evidence, in precedence order.

    An escalation recommendation is advisory. It never overrides a confirmed
    effect and it never turns an unverifiable action into permission to repeat
    input. The model must first obtain fresh evidence.
    """
    if res.effect == 'confirmed' or res.verified is True:
        return {'decision': 'done'}
    if res.effect == 'unverifiable':
        return {'decision': 'verify_fresh_state'}
    if res.effect == 'suspected_noop' or not res.ok or res.code is not None:
        decision: Dict[str, Any] = {'decision': 'escalate'}
        if isinstance(res.escalation, dict):
            decision['recommended'] = res.escalation.get('recommended')
        return decision
    return {'decision': 'verify_fresh_state'}

def _action_payload(res: ActionResult) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'ok': res.ok, 'action': res.action}
    if res.message:
        payload['message'] = res.message
    if res.verified is not None:
        payload['verified'] = res.verified
    if res.effect is not None:
        payload['effect'] = res.effect
    if res.escalation is not None:
        payload['escalation'] = res.escalation
    if res.path is not None:
        payload['path'] = res.path
    if res.degraded is not None:
        payload['degraded'] = res.degraded
    if res.delivery_mode is not None:
        payload['delivery_mode'] = res.delivery_mode
    if res.code is not None:
        payload['code'] = res.code
    if res.meta:
        payload['meta'] = res.meta
    payload['verdict'] = _classify_action_result(res)
    return payload

def _text_response(res: ActionResult) -> str:
    return json.dumps(_action_payload(res))
_DEFAULT_MAX_ELEMENTS = 100
_MAX_ALLOWED_MAX_ELEMENTS = 1000
_MIN_PROVIDER_IMAGE_DIMENSION = 8

def _image_dimensions_from_b64(image_b64: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) for common inline screenshot formats.

    Some providers reject images below 8x8 before the model sees the tool
    result. Inspecting the encoded bytes here lets computer_use fall back to
    its AX/SOM text payload instead of sending an unusable placeholder.
    """
    if not image_b64:
        return None
    try:
        raw = base64.b64decode(image_b64, validate=False)
    except Exception:
        return None
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) >= 24:
        try:
            width, height = struct.unpack('>II', raw[16:24])
            return (int(width), int(height))
        except Exception:
            return None
    if raw.startswith(b'\xff\xd8') and len(raw) > 4:
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 255:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            while marker == 255 and i < len(raw):
                marker = raw[i]
                i += 1
            if marker in {216, 217}:
                continue
            if marker == 218:
                break
            if i + 2 > len(raw):
                break
            segment_len = int.from_bytes(raw[i:i + 2], 'big')
            if segment_len < 2 or i + segment_len > len(raw):
                break
            if marker in {192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207} and segment_len >= 7:
                height = int.from_bytes(raw[i + 3:i + 5], 'big')
                width = int.from_bytes(raw[i + 5:i + 7], 'big')
                return (int(width), int(height))
            i += segment_len
    return None

def _coerce_max_elements(value: Any) -> int:
    """Validate the caller-supplied ``max_elements``.

    Falls back to :data:`_DEFAULT_MAX_ELEMENTS` for missing / non-integer /
    sub-1 inputs so the cap can never be silently disabled by a malformed
    tool-call argument. Clamps oversized values to
    :data:`_MAX_ALLOWED_MAX_ELEMENTS` so a caller cannot bypass the
    safeguard by passing a very large integer.
    """
    if value is None:
        return _DEFAULT_MAX_ELEMENTS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return _DEFAULT_MAX_ELEMENTS
    if n > _MAX_ALLOWED_MAX_ELEMENTS:
        return _MAX_ALLOWED_MAX_ELEMENTS
    return n

def _capture_response(cap: CaptureResult, max_elements: int=_DEFAULT_MAX_ELEMENTS) -> Any:
    total_elements = len(cap.elements)
    visible_elements = cap.elements[:max_elements]
    truncated_elements = max(0, total_elements - len(visible_elements))
    image_dimensions = _image_dimensions_from_b64(cap.png_b64 or '') if cap.png_b64 else None
    response_width = image_dimensions[0] if image_dimensions else cap.width
    response_height = image_dimensions[1] if image_dimensions else cap.height
    image_too_small = bool(image_dimensions and (image_dimensions[0] < _MIN_PROVIDER_IMAGE_DIMENSION or image_dimensions[1] < _MIN_PROVIDER_IMAGE_DIMENSION))
    element_index = _format_elements(visible_elements)
    summary_lines = [f'capture mode={cap.mode} {response_width}x{response_height}' + (f' app={cap.app}' if cap.app else '') + (f' window={cap.window_title!r}' if cap.window_title else ''), f'{total_elements} interactable element(s):']
    if element_index:
        summary_lines.extend(element_index)
    if image_too_small:
        summary_lines.append(f'  (screenshot omitted: {image_dimensions[0]}x{image_dimensions[1]} is below the {_MIN_PROVIDER_IMAGE_DIMENSION}x{_MIN_PROVIDER_IMAGE_DIMENSION} provider minimum)')
    summary = '\n'.join(summary_lines)
    if cap.png_b64 and cap.mode != 'ax' and (not image_too_small):
        if _should_route_through_aux_vision():
            routed = _route_capture_through_aux_vision(cap, summary)
            if routed is not None:
                return routed
            summary_lines.append('  (vision unavailable: the auxiliary vision model could not be reached; screenshot omitted. Element-index actions still work — drive via the element list above.)')
            if truncated_elements:
                summary_lines.append(f'  (response truncated to {len(visible_elements)} of {total_elements} elements; raise max_elements or pass app= to narrow)')
            payload = {'mode': cap.mode, 'width': response_width, 'height': response_height, 'app': cap.app, 'window_title': cap.window_title, 'elements': [_element_to_dict(e) for e in visible_elements], 'total_elements': total_elements, 'summary': '\n'.join(summary_lines), 'vision_unavailable': True}
            if truncated_elements:
                payload['truncated_elements'] = truncated_elements
            return json.dumps(payload)
        _mime = cap.image_mime_type
        if not _mime:
            _b64_prefix = cap.png_b64[:8]
            _mime = 'image/jpeg' if _b64_prefix.startswith('/9j/') else 'image/png'
        return {'_multimodal': True, 'content': [{'type': 'text', 'text': summary}, {'type': 'image_url', 'image_url': {'url': f'data:{_mime};base64,{cap.png_b64}'}}], 'text_summary': summary, 'meta': {'mode': cap.mode, 'width': response_width, 'height': response_height, 'elements': total_elements, 'png_bytes': cap.png_bytes_len}}
    if truncated_elements:
        summary_lines.append(f'  (response truncated to {len(visible_elements)} of {total_elements} elements; raise max_elements or pass app= to narrow)')
    summary = '\n'.join(summary_lines)
    payload: Dict[str, Any] = {'mode': cap.mode, 'width': response_width, 'height': response_height, 'app': cap.app, 'window_title': cap.window_title, 'elements': [_element_to_dict(e) for e in visible_elements], 'total_elements': total_elements, 'summary': summary}
    if truncated_elements:
        payload['truncated_elements'] = truncated_elements
    return json.dumps(payload)
_MAX_VISION_DIM = 1456

def _shrink_capture_for_vision(raw: bytes, ext: str, max_dim: int=_MAX_VISION_DIM) -> bytes:
    """Downscale encoded image bytes so the longest side is <= max_dim.

    Returns the original bytes unchanged when the image already fits or when
    Pillow is unavailable/fails — no worse than the pre-shrink behavior.
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if max(img.size) <= max_dim:
            return raw
        img.thumbnail((max_dim, max_dim))
        out = BytesIO()
        img.save(out, format='JPEG' if ext == '.jpg' else 'PNG')
        return out.getvalue()
    except Exception as exc:
        logger.debug('computer_use: vision downscale skipped: %s', exc)
        return raw

def _should_route_through_aux_vision() -> bool:
    """Return True when ``_capture_response`` should hand the PNG to aux vision.

    Reads the active main provider/model and the loaded config and asks the
    routing helper. Any failure (config import, runtime override missing,
    etc.) returns False so the existing multimodal envelope continues to be
    returned — fail open on the routing decision so a broken config can
    never silently drop the screenshot for vision-capable main models.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config
        from tools.computer_use.vision_routing import should_route_capture_to_aux_vision
    except Exception as exc:
        logger.debug('computer_use: aux-vision routing import failed: %s', exc)
        return False
    try:
        provider = _read_main_provider() or ''
        model = _read_main_model() or ''
    except Exception as exc:
        logger.debug('computer_use: aux-vision routing config read failed: %s', exc)
        return False
    cache_key = (str(provider), str(model))
    cached = _AUX_VISION_ROUTE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        cfg = load_config()
        decision = bool(should_route_capture_to_aux_vision(provider, model, cfg))
    except Exception as exc:
        logger.debug('computer_use: aux-vision routing decision failed: %s', exc)
        return False
    _AUX_VISION_ROUTE_CACHE[cache_key] = decision
    return decision

def _capture_after_mode() -> str:
    """Mode for ``capture_after`` follow-ups. Default ``som`` (screenshot)."""
    try:
        from hermes_cli.config import load_config
        raw = ((load_config() or {}).get('computer_use') or {}).get('capture_after_mode', 'som')
    except Exception:
        return 'som'
    mode = str(raw or 'som').strip().lower()
    return mode if mode in {'som', 'vision', 'ax'} else 'som'

def _route_capture_through_aux_vision(cap: CaptureResult, summary: str) -> Optional[str]:
    """Pre-analyse the captured PNG via ``vision_analyze`` and return a text result.

    The captured base64 PNG is materialised to ``$DUCK_AGENT_HOME/cache/vision/``
    and handed to ``vision_analyze_tool`` with a generic describe prompt.
    The resulting text description is merged into the existing AX/SOM
    summary so the main model receives a single text payload that mentions
    every interactable element AND a description of what the screenshot
    looked like.

    Returns:
      A JSON-encoded text response on success.
      ``None`` on failure (caller falls back to the multimodal envelope).
    """
    if not cap.png_b64:
        return None
    try:
        import base64 as _base64
        import os as _os
        import uuid as _uuid
        from hermes_constants import get_hermes_dir
        from model_tools import _run_async
        from tools.vision_tools import vision_analyze_tool
    except Exception as exc:
        logger.debug('computer_use: aux-vision import failed: %s', exc)
        return None
    temp_image_path = None
    try:
        try:
            raw = _base64.b64decode(cap.png_b64, validate=False)
        except Exception as exc:
            logger.debug('computer_use: failed to decode capture base64: %s', exc)
            return None
        _mime_for_ext = cap.image_mime_type or ''
        if _mime_for_ext == 'image/jpeg' or (not _mime_for_ext and cap.png_b64[:8].startswith('/9j/')):
            ext = '.jpg'
        else:
            ext = '.png'
        cache_dir = get_hermes_dir('cache/vision', 'temp_vision_images')
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = cache_dir / f'computer_use_{_uuid.uuid4().hex}{ext}'
        raw = _shrink_capture_for_vision(raw, ext)
        temp_image_path.write_bytes(raw)
        prompt = f'Describe what is visible in this desktop application screenshot in concise but specific terms. Mention the app name and window title if visible, the overall layout, any labelled buttons, menus or text fields, and any prominent text content the user would need to know about. Do not invent details that are not actually visible.\n\nAX/SOM index for cross-reference:\n{summary}'
        result_json = _run_async(vision_analyze_tool(str(temp_image_path), prompt))
    except Exception as exc:
        logger.warning('computer_use: auxiliary.vision pre-analysis failed (%s); returning to caller without aux analysis', exc)
        return None
    finally:
        if temp_image_path is not None:
            try:
                _os.unlink(str(temp_image_path))
            except Exception:
                pass
    analysis_text = ''
    if isinstance(result_json, str):
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                analysis_text = str(parsed.get('analysis') or '').strip()
        except (TypeError, json.JSONDecodeError):
            analysis_text = result_json.strip()
    if not analysis_text:
        return None
    return json.dumps({'mode': cap.mode, 'width': cap.width, 'height': cap.height, 'app': cap.app, 'window_title': cap.window_title, 'elements': [_element_to_dict(e) for e in cap.elements], 'summary': summary, 'vision_analysis': analysis_text, 'vision_analysis_routed_via': 'auxiliary.vision'})

def _maybe_follow_capture(backend: ComputerUseBackend, res: ActionResult, do_capture: bool) -> Any:
    if not do_capture:
        return _text_response(res)
    if not res.ok:
        return _text_response(res)
    try:
        target = getattr(backend, '_last_target', None) or {}
        pid = target.get('pid')
        window_id = target.get('window_id')
        mode = _capture_after_mode()
        if pid is not None and window_id is not None:
            cap = backend.capture(mode=mode, pid=pid, window_id=window_id)
        else:
            cap = backend.capture(mode=mode, app=getattr(backend, '_last_app', None))
    except Exception as e:
        logger.warning('follow-up capture failed: %s', e)
        return _text_response(res)
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get('_multimodal'):
        prefix = json.dumps(_action_payload(res))
        resp['content'][0]['text'] = prefix + '\n\n' + resp['content'][0]['text']
        resp['text_summary'] = prefix + '\n\n' + resp['text_summary']
        resp['action_result'] = _action_payload(res)
        return resp
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {'capture': resp}
    data.update(_action_payload(res))
    return json.dumps(data)

def _format_elements(elements: List[UIElement], max_lines: int=40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace('\n', ' ')[:60]
        out.append(f'  #{e.index} {e.role} {label!r} @ {e.bounds}' + (f' [{e.app}]' if e.app else ''))
    if len(elements) > max_lines:
        out.append(f'  ... +{len(elements) - max_lines} more (call capture with app= to narrow)')
    return out

def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    return {'index': e.index, 'role': e.role, 'label': e.label, 'bounds': list(e.bounds), 'app': e.app}

def check_computer_use_requirements() -> bool:
    """Return True iff computer_use can run on this host.

    Conditions: macOS, Windows, or Linux + cua-driver binary installed (or
    override via env). cua-driver runs on all three; the Linux path is
    headed/X11 today (Wayland via XWayland), pure-Wayland progress tracked
    upstream. Linux users see specific blocked checks via
    `duck-agent computer-use doctor` if their session is incomplete (e.g. no
    DISPLAY set).
    """
    if sys.platform not in ('darwin', 'win32', 'linux'):
        return False
    from tools.computer_use.cua_backend import cua_driver_binary_available
    return cua_driver_binary_available()

def get_computer_use_schema() -> Dict[str, Any]:
    from tools.computer_use.schema import COMPUTER_USE_SCHEMA
    return COMPUTER_USE_SCHEMA