"""Abstract backend interface for computer use.

Any implementation (cua-driver over MCP, pyautogui, noop, future Linux/Windows)
must return the shape described below. All methods synchronous; async is
handled inside the backend implementation if needed.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class UIElement:
    """One interactable element on the current screen."""
    index: int
    role: str
    label: str = ''
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    app: str = ''
    pid: int = 0
    window_id: int = 0
    attributes: Dict[str, Any] = field(default_factory=dict)
    element_token: Optional[str] = None

    def center(self) -> Tuple[int, int]:
        x, y, w, h = self.bounds
        return (x + w // 2, y + h // 2)

@dataclass
class CaptureResult:
    """Result of a screen capture call.

    At least one of png_b64 / elements is populated depending on capture mode:
      * mode="vision" → png_b64 only
      * mode="ax"     → elements only
      * mode="som"    → both (default): PNG already has numbered overlays
                         drawn by the backend, and `elements` holds the
                         matching index → element mapping.
    """
    mode: str
    width: int
    height: int
    png_b64: Optional[str] = None
    elements: List[UIElement] = field(default_factory=list)
    app: str = ''
    window_title: str = ''
    png_bytes_len: int = 0
    image_mime_type: Optional[str] = None

@dataclass
class ActionResult:
    """Result of any action (click / type / scroll / drag / key / wait).

    Beyond the transport-level ``ok`` flag, this carries cua-driver's
    structured action verdict so the model can follow the documented
    verify → escalate ladder (NousResearch/duck-agent#67052). ``ok`` stays
    tool/transport success only — it is NOT the semantic verdict. Read
    ``effect`` / ``escalation`` to decide the next rung. All structured
    fields are optional and additive: an older driver that omits
    ``structuredContent`` leaves them ``None`` and behavior is unchanged.
    """
    ok: bool
    action: str
    message: str = ''
    capture: Optional[CaptureResult] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    verified: Optional[bool] = None
    effect: Optional[str] = None
    escalation: Optional[Dict[str, Any]] = None
    path: Optional[str] = None
    degraded: Optional[bool] = None
    delivery_mode: Optional[str] = None
    code: Optional[str] = None

class ComputerUseBackend(ABC):
    """Lifecycle: `start()` before first use, `stop()` at shutdown."""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend can be used on this host right now.

        Used by check_fn gating and by the post-setup wizard.
        """

    @abstractmethod
    def capture(self, mode: str='som', app: Optional[str]=None, pid: Optional[int]=None, window_id: Optional[int]=None) -> CaptureResult:
        ...

    @abstractmethod
    def click(self, *, element: Optional[int]=None, x: Optional[int]=None, y: Optional[int]=None, button: str='left', click_count: int=1, modifiers: Optional[List[str]]=None, delivery_mode: Optional[str]=None, bring_to_front: bool=False) -> ActionResult:
        ...

    @abstractmethod
    def drag(self, *, from_element: Optional[int]=None, to_element: Optional[int]=None, from_xy: Optional[Tuple[int, int]]=None, to_xy: Optional[Tuple[int, int]]=None, button: str='left', modifiers: Optional[List[str]]=None, delivery_mode: Optional[str]=None, bring_to_front: bool=False) -> ActionResult:
        ...

    @abstractmethod
    def scroll(self, *, direction: str, amount: int=3, element: Optional[int]=None, x: Optional[int]=None, y: Optional[int]=None, modifiers: Optional[List[str]]=None, delivery_mode: Optional[str]=None, bring_to_front: bool=False) -> ActionResult:
        ...

    @abstractmethod
    def type_text(self, text: str, *, delivery_mode: Optional[str]=None, bring_to_front: bool=False) -> ActionResult:
        ...

    @abstractmethod
    def key(self, keys: str, *, delivery_mode: Optional[str]=None, bring_to_front: bool=False) -> ActionResult:
        """Send a key combo, e.g. 'cmd+s', 'ctrl+alt+t', 'return'."""

    @abstractmethod
    def list_apps(self) -> List[Dict[str, Any]]:
        """Return running apps with bundle IDs, PIDs, window counts."""

    def list_windows(self) -> List[Dict[str, Any]]:
        """Return visible native windows with PID and window identifiers.

        Optional compatibility hook: backends that predate window discovery
        remain instantiable and simply report no windows.
        """
        return []

    @abstractmethod
    def focus_app(self, app: str, raise_window: bool=False) -> ActionResult:
        """Route input to `app` (by name or bundle ID). Default: focus without raise."""

    @abstractmethod
    def set_value(self, value: str, element: Optional[int]=None) -> ActionResult:
        """Set a native value on an element (e.g. AXPopUpButton selection).

        `element` is the 1-based SOM index returned by a prior capture call.
        """

    @staticmethod
    def _typed_browser_unavailable() -> Dict[str, Any]:
        return {'ok': False, 'status': 'refused', 'code': 'typed_browser_unavailable', 'message': 'This computer-use backend has no typed browser route; use native capture/input.', 'native_fallback_required': True}

    def typed_browser_state(self, **kwargs: Any) -> Dict[str, Any]:
        """Optional exact-bind/read hook; native-only backends fail closed."""
        return self._typed_browser_unavailable()

    def typed_browser_prepare(self, **kwargs: Any) -> Dict[str, Any]:
        """Optional setup hook; native-only backends fail closed."""
        return self._typed_browser_unavailable()

    def typed_browser_action(self, driver_tool: str, *, tab_id: Optional[str]=None, args: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
        """Optional mutation hook; native-only backends fail closed."""
        return self._typed_browser_unavailable()

    def wait(self, seconds: float) -> ActionResult:
        """Default implementation: time.sleep."""
        import time
        time.sleep(max(0.0, min(seconds, 30.0)))
        return ActionResult(ok=True, action='wait', message=f'waited {seconds:.2f}s')