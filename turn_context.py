"""Per-turn context shared between ``GatewayRunner._run_agent_inner`` and the
``TurnRunner`` collaborator (gateway/run.py).

``_run_agent_inner`` historically defined its tool-progress plumbing as nested
closures (``progress_callback`` ~250 LOC, ``send_progress_messages`` ~353 LOC)
that closed over ~20 enclosing locals.  ``TurnContext`` is the extraction seam:
each closed-over local becomes a field on this dataclass, so the closure bodies
can move onto ``TurnRunner`` methods unchanged modulo ``name`` -> ``ctx.name``
rewrites.

Field notes:

- All fields are written once by ``_run_agent_inner`` while wiring up the turn
  (a few — ``_progress_metadata``, ``_progress_reply_to``, ``agent_holder`` —
  are computed slightly later than construction and assigned onto the ctx as
  soon as the original locals were bound).  None of the original closures
  *rebound* their captured names (no ``nonlocal``); mutable state uses the
  same single-element-list containers as before (``last_progress_msg``,
  ``repeat_count``, ...), so mutation stays visible to the outer body through
  the shared objects exactly as it did through the shared closure cells.
- ``_run_still_current`` stays a callable (it captures ``self``/
  ``session_key``/``run_generation``); carrying the callable keeps the
  extracted bodies byte-identical.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

@dataclass
class TurnContext:
    """Closed-over locals of ``_run_agent_inner`` needed by ``TurnRunner``."""
    source: Any = None
    _run_still_current: Callable[[], bool] = None
    _live_status_adapter: Any = None
    _live_status_mode: str = 'off'
    _thinking_enabled: bool = False
    progress_mode: str = 'off'
    progress_grouping: str = 'grouped'
    tool_progress_enabled: bool = False
    progress_queue: Any = None
    log_queue: Any = None
    last_progress_msg: list = field(default_factory=lambda: [None])
    last_tool: list = field(default_factory=lambda: [None])
    last_was_terminal_block: list = field(default_factory=lambda: [False])
    repeat_count: list = field(default_factory=lambda: [0])
    long_tool_hint_fired: list = field(default_factory=lambda: [False])
    agent_holder: list = field(default_factory=lambda: [None])
    _LONG_TOOL_THRESHOLD_S: float = 30.0
    _cleanup_progress: bool = False
    _cleanup_msg_ids: List[str] = field(default_factory=list)
    _progress_metadata: Optional[dict] = None
    _progress_reply_to: Optional[Any] = None
    message: Optional[str] = None
    history: Any = None
    context_prompt: Optional[str] = None
    channel_prompt: Optional[str] = None
    session_id: Optional[str] = None
    session_key: Optional[str] = None
    run_generation: Optional[int] = None
    process_task_id: str = ''
    process_baseline: frozenset[str] = field(default_factory=frozenset)
    _interrupt_depth: int = 0
    event_message_id: Optional[str] = None
    moa_config: Optional[dict] = None
    persist_user_message: Optional[Any] = None
    persist_user_timestamp: Optional[float] = None
    user_config: Any = None
    enabled_toolsets: Any = None
    disabled_toolsets: Any = None
    log_mode_enabled: bool = False
    interim_assistant_messages_enabled: bool = False
    needs_progress_queue: bool = False
    AIAgent: Any = None
    resolve_display_setting: Any = None
    result_holder: list = field(default_factory=lambda: [None])
    tools_holder: list = field(default_factory=lambda: [None])
    stream_consumer_holder: list = field(default_factory=lambda: [None])
    streaming_tts_consumer_holder: list = field(default_factory=lambda: [None])
    _voice_ack_fired: list = field(default_factory=lambda: [False])
    _voice_ack_guild: list = field(default_factory=lambda: [None])
    _voice_ack_loop: Any = None
    _loop_for_step: Any = None
    _hooks_ref: Any = None
    _status_adapter: Any = None
    _status_chat_id: Any = None
    _status_thread_metadata: Optional[dict] = None
    progress_callback: Optional[Callable] = None
    voice_ack_callback: Optional[Callable] = None
    _step_callback_sync: Optional[Callable] = None
    _event_callback_sync: Optional[Callable] = None
    _status_callback_sync: Optional[Callable] = None