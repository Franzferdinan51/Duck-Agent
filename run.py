"""
Gateway runner - entry point for messaging platform integrations.

This module provides:
- start_gateway(): Start all configured platform adapters
- GatewayRunner: Main class managing the gateway lifecycle

Usage:
    # Start the gateway
    python -m gateway.run
    
    # Or from CLI
    python cli.py --gateway
"""
try:
    import hermes_bootstrap
except ModuleNotFoundError:
    pass
import asyncio
import concurrent.futures
import dataclasses
import faulthandler
import inspect
import json
import logging
import os
import queue
import re
import shlex
import site
import sys
import signal
import threading
import time
from collections import OrderedDict
from contextvars import copy_context
from pathlib import Path
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Any, List, Tuple, Union, cast
from agent.async_utils import consume_detached_task_result, safe_schedule_threadsafe
from agent.conversation_compression import COMPACTION_STATUS, COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE, COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE, COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE, COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE, IDLE_COMPACTION_STATUS_TEMPLATE, PRE_API_COMPRESSION_STATUS_TEMPLATE, PREFLIGHT_COMPRESSION_STATUS_TEMPLATE
from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
from agent.i18n import t
from agent.interrupt_compat import request_hard_interrupt
from agent.turn_context import compression_made_progress
from hermes_cli.config import cfg_get
from hermes_cli.fallback_config import get_fallback_chain
_AGENT_CACHE_MAX_SIZE = 128
_AGENT_CACHE_IDLE_TTL_SECS = 3600.0
_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0
_TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT = 180.0
_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0
_STALL_NOTIFY_SEND_TIMEOUT_SECONDS = 15.0
_GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS = 16 * 1024 * 1024
_TELEGRAM_COMMAND_MENTION_RE = re.compile('(?<![\\w:/])/([A-Za-z0-9][A-Za-z0-9_-]*)')
_GATEWAY_HYGIENE_PLATFORM = 'gateway_hygiene'
_TELEGRAM_NOISY_STATUS_RE = re.compile("(auxiliary\\s+.+\\s+failed|compression\\s+summary\\s+failed|fallback\\s+context\\s+marker|configured\\s+compression\\s+model\\s+.+\\s+failed|no\\s+auxiliary\\s+llm\\s+provider\\s+configured|auto-lowered\\s+compression\\s+threshold|auto-lowered\\s+(?:this\\s+)?session'?s?\\s+threshold|configured\\s+auxiliary\\s+compression\\s+provider\\s+.+\\s+unavailable|skipping\\s+concurrent\\s+compression|compacting\\s+context\\s+[—-]\\s+summarizing\\s+earlier\\s+conversation|resumed\\s+after\\s+\\d+s\\s+idle\\s+[—-]\\s+compacting|preflight\\s+compression|pre[- ]api\\s+compression|context\\s+too\\s+large\\s+\\(~[\\d,]+\\s+tokens\\)\\s+[—-]+\\s+compressing|compressed\\s+\\d[\\d,]*\\s+(?:→|->)\\s+\\d[\\d,]*\\s+messages,\\s+retrying|compressed\\s+~[\\d,]+\\s+(?:→|->)\\s+~[\\d,]+\\s+tokens,\\s+retrying|context\\s+reduced\\s+to\\s+[\\d,]+\\s+tokens\\s+\\(was\\s+[\\d,]+\\),\\s+retrying|session\\s+compressed\\s+\\d+\\s+times|rate\\s+limited\\.\\s+waiting\\s+\\d|retrying\\s+in\\s+\\d|max\\s+retries\\s+\\(\\d+\\).*(?:trying\\s+fallback|exhausted|invalid\\s+responses)|stream\\s+(?:drop|drop\\s+mid\\s+tool-call).+retry\\s+\\d|stale\\s+connections\\s+from\\s+a\\s+previous\\s+provider\\s+issue)", re.IGNORECASE | re.DOTALL)
_HYGIENE_COOLDOWN_LADDER_MULTIPLIERS = (1, 3, 9)
_HYGIENE_COOLDOWN_MAX_SECONDS = 3600.0

def _hygiene_cooldown_for_failure(gateway, session_key: str, base_cooldown_seconds: float) -> float:
    """Bump the hygiene failure streak and return the escalated cooldown.

    This is a MULTIPLIER ladder (x1, x3, x9) over the operator's configured
    ``hygiene_failure_cooldown_seconds``, clamped to
    ``_HYGIENE_COOLDOWN_MAX_SECONDS``, so a tuned base is preserved as rung 1.

    It exists because the in-agent equivalent is unreachable from here:
    ``ContextCompressor.record_timeout_failure`` escalates on an absolute
    60 -> 300 -> 900s ladder driven by the in-memory
    ``_consecutive_timeout_failures`` counter, which ``bind_session_state``
    zeroes.  Session hygiene constructs a FRESH ``AIAgent`` per run and re-binds
    state every time, so from the gateway that streak is structurally always 0
    and only the flat ``hygiene_failure_cooldown_seconds`` could ever be
    recorded — a session whose summary model always times out retried on that
    same fixed interval forever (#79624).  Keeping the streak on
    ``PersistentState`` outlives the per-run agent, so failures climb.
    """
    streak = 1
    try:
        state = gateway._session_state(session_key).persistent
        state.hygiene_failure_streak += 1
        streak = state.hygiene_failure_streak
    except Exception as exc:
        logger.debug('hygiene failure streak update failed: %s', exc)
    multiplier = _HYGIENE_COOLDOWN_LADDER_MULTIPLIERS[min(streak, len(_HYGIENE_COOLDOWN_LADDER_MULTIPLIERS)) - 1]
    return min(base_cooldown_seconds * multiplier, _HYGIENE_COOLDOWN_MAX_SECONDS)

def _reset_hygiene_failure_streak(gateway, session_key: str) -> None:
    """Clear the hygiene failure streak after a compression that reduced context.

    Peeks rather than get-or-creates: writing a 0 that is already 0 must not
    materialise a ``_sessions`` entry (those are never evicted).
    """
    try:
        state = gateway._peek_session_state(session_key)
        if state is not None:
            state.persistent.hygiene_failure_streak = 0
    except Exception as exc:
        logger.debug('hygiene failure streak reset failed: %s', exc)

def hygiene_compaction_recovered(*, aborted: bool, rotated: bool, in_place: bool, msg_count: int, new_count: int, approx_tokens: int, new_tokens: int) -> bool:
    """True when a hygiene run actually recovered the session.

    Extracted from ``_handle_message_with_agent`` so the decision is unit
    testable: it previously lived inline in a ~2000-line async method, and the
    only way to pin it was a source-reading test — which AGENTS.md bans
    outright, naming this file.

    "Recovered" requires all three:

    * the compressor did not abort (no summary produced at all);
    * the transcript was actually rewritten — either rotated into a new session
      or compacted in place.  The degenerate "did not rotate or compact in
      place" path (#21301) reuses the pre-compression counts, so relying on the
      numbers alone would read a no-op as success;
    * the request materially shrank, per the canonical
      :func:`compression_made_progress` (#39548) — a row-count drop counts even
      when the summary keeps the token estimate flat, and a sub-5% token wobble
      does not count at all.

    The token arguments are deliberately compared through that shared predicate
    rather than with a bare ``<``: ``approx_tokens`` can be provider-reported
    while ``new_tokens`` is always a rough estimate (documented to run 30-50%
    high on code-heavy sessions), so a bare comparison both misses real wins and
    counts noise as one.
    """
    if aborted:
        return False
    if not (rotated or in_place):
        return False
    return compression_made_progress(msg_count, new_count, approx_tokens, new_tokens)

def _record_hygiene_cooldown(gateway, session_id: str, cooldown_seconds: float, error: Optional[str]=None) -> None:
    """Persist a session-hygiene compression-failure cooldown to the state DB.

    Uses the same ``compression_failure_cooldown_until`` column and
    ``record_compression_failure_cooldown`` method that the in-conversation
    compression path (``agent/context_compressor.py``) already uses, so the
    cooldown survives gateway restarts (#74136).

    ``error`` is forwarded because the recorder writes
    ``compression_failure_error`` UNCONDITIONALLY — omitting it clobbers to NULL
    any reason the in-conversation path recorded, and readers surface that
    reason to the user (falling back to "unknown error"). That matters more now
    that an escalated cooldown can last up to an hour.
    """
    import time as _time
    session_db = getattr(gateway, '_session_db', None)
    if session_db is None:
        return
    session_db = getattr(session_db, '_db', session_db)
    recorder = getattr(session_db, 'record_compression_failure_cooldown', None)
    if recorder is None:
        return
    try:
        recorder(session_id, _time.time() + cooldown_seconds, error)
    except Exception as exc:
        logger.debug('session hygiene cooldown persist failed: %s', exc)

def _status_template_to_regex(template: str) -> str:
    """Compile a compression status template constant into a regex source.

    Literal text is escaped verbatim (so wording drift in
    agent/conversation_compression.py cannot silently diverge from this
    matcher — the constants ARE the wording) and each ``{field}`` format
    placeholder is replaced with a numeric-ish pattern covering every value
    the emit sites format in (ints, ``{:,}`` thousands separators).
    """
    parts = re.split('\\{[^{}]*\\}', template)
    return '[\\d,]+'.join((re.escape(part) for part in parts))
_COMPRESSION_PROGRESS_STATUS_RE = re.compile('|'.join((_status_template_to_regex(_template) for _template in (COMPACTION_STATUS, PRE_API_COMPRESSION_STATUS_TEMPLATE, PREFLIGHT_COMPRESSION_STATUS_TEMPLATE, IDLE_COMPACTION_STATUS_TEMPLATE, COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE, COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE, COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE, COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE))), re.IGNORECASE)

def _gateway_compression_progress_notices_enabled() -> bool:
    """True when the user opted into routine compression progress notices.

    Reads ``compression.progress_notices`` from the gateway's raw YAML config
    (#52995). Default False — routine compression stays silent-by-design on
    chat platforms unless explicitly enabled. Read live (mtime-cached) so a
    config edit on a running gateway takes effect on the next status.
    Fail-closed: any config read error keeps the silent default.
    """
    try:
        config = _load_gateway_config()
        compression_cfg = config.get('compression') if isinstance(config, dict) else None
        if isinstance(compression_cfg, dict):
            return str(compression_cfg.get('progress_notices', False)).strip().lower() in {'true', '1', 'yes', 'on'}
    except Exception:
        pass
    return False
_GATEWAY_RAW_TEXT_PLATFORMS = frozenset({'local', 'api_server', 'webhook', 'msgraph_webhook'})

def _gateway_surface_passes_raw_text(platform: Any) -> bool:
    """True only for programmatic/local surfaces that must keep raw text."""
    return _gateway_platform_value(platform) in _GATEWAY_RAW_TEXT_PLATFORMS
_GATEWAY_PROVIDER_ERROR_RE = re.compile('(api\\s+(?:call\\s+)?failed|provider\\s+authentication\\s+failed|non-retryable\\s+error|rate\\s+limited\\s+after\\s+\\d+\\s+retries|error\\s+code\\s*:|\\bhttp\\s*\\d{3}\\b|incorrect\\s+api\\s+key|invalid\\s+api\\s+key)', re.IGNORECASE)
_GATEWAY_PROVIDER_POLICY_RE = re.compile('(cybersecurity\\s+risk|security\\s+policy|safety\\s+policy|policy\\s+violation|violat(?:e|es|ed|ion)|blocked\\s+(?:because|by|under)|request\\s+(?:was\\s+)?(?:blocked|rejected)|disallowed|moderation)', re.IGNORECASE)
_GATEWAY_AUTH_ERROR_RE = re.compile('(provider\\s+authentication\\s+failed|incorrect\\s+api\\s+key|invalid\\s+api\\s+key|\\b401\\b)', re.IGNORECASE)
_GATEWAY_RATE_LIMIT_RE = re.compile('(rate\\s+limit|rate-limited|\\b429\\b|quota|usage\\s+limit)', re.IGNORECASE)
_GATEWAY_SECRET_PATTERNS = (re.compile('\\bsk-[A-Za-z0-9][A-Za-z0-9_\\-]{12,}\\b'), re.compile('\\bgh[pousr]_[A-Za-z0-9_]{20,}\\b'), re.compile('\\bxapp-\\d+-[A-Za-z0-9\\-]{20,}\\b'), re.compile('\\bxox[baprs]-[A-Za-z0-9\\-]{20,}\\b'), re.compile('\\bhf_[A-Za-z0-9]{20,}\\b'), re.compile('\\bglpat-[A-Za-z0-9_\\-]{20,}\\b'), re.compile('(?i)\\b(Bearer\\s+)[A-Za-z0-9._\\-]{20,}\\b'))

def _ensure_windows_gateway_venv_imports() -> None:
    """Make detached Windows gateway runs see the Duck Agent venv packages.

    Some Windows restart paths run the gateway under uv's base ``pythonw.exe``
    to avoid the venv launcher respawning a visible console interpreter.  That
    mode can import the source tree via cwd/PYTHONPATH but still miss optional
    packages installed only in ``venv/Lib/site-packages`` (notably the MCP SDK).
    Patch the live process before MCP discovery so tool injection does not
    depend on every launcher preserving PYTHONPATH perfectly.
    """
    if sys.platform != 'win32':
        return
    project_root = Path(__file__).resolve().parent.parent
    candidates: list[Path] = []
    if os.environ.get('VIRTUAL_ENV'):
        candidates.append(Path(os.environ['VIRTUAL_ENV']))
    candidates.append(project_root / 'venv')
    seen: set[str] = set()
    for venv_dir in candidates:
        try:
            resolved_venv = venv_dir.resolve()
        except OSError:
            resolved_venv = venv_dir
        venv_key = str(resolved_venv).lower()
        if venv_key in seen:
            continue
        seen.add(venv_key)
        site_packages = resolved_venv / 'Lib' / 'site-packages'
        if not site_packages.exists():
            continue
        project_entry = str(project_root)
        site_entry = str(site_packages)
        if project_entry not in sys.path:
            sys.path.insert(0, project_entry)
        site.addsitedir(site_entry)
        if site_entry in sys.path:
            sys.path.remove(site_entry)
        insert_at = 1 if sys.path and sys.path[0] == project_entry else 0
        sys.path.insert(insert_at, site_entry)
        os.environ['VIRTUAL_ENV'] = str(resolved_venv)
        pythonpath = [project_entry, site_entry]
        if os.environ.get('PYTHONPATH'):
            pythonpath.append(os.environ['PYTHONPATH'])
        os.environ['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(pythonpath))
        return

def _gateway_platform_value(platform: Any) -> str:
    """Return a normalized gateway platform value for enums or raw strings."""
    return str(getattr(platform, 'value', platform) or '').strip().lower()

def _non_conversational_metadata(metadata: Optional[Dict[str, Any]]=None, *, platform: Any=None) -> Optional[Dict[str, Any]]:
    """Mark Discord lifecycle/status sends without changing other platforms."""
    if _gateway_platform_value(platform) != 'discord':
        return metadata
    merged = dict(metadata or {})
    merged['non_conversational'] = True
    return merged

def _seed_hygiene_system_prompt(agent: Any, session_row: Optional[Dict[str, Any]]) -> bool:
    """Keep gateway hygiene from rebuilding a live session's system prompt.

    The hygiene helper intentionally skips memory-provider initialization.
    Compression is allowed to persist a system prompt, so letting that helper
    rebuild one would strip external provider blocks from the live session.
    Seed the exact persisted prompt instead.  When no usable prompt can be
    restored, seed an empty cache entry.  Compression either preserves that
    unusable value or rebuilds with the hygiene-only platform marker; the real
    turn will rebuild either form with its fully initialized providers.
    """
    stored_prompt = ''
    if isinstance(session_row, dict):
        raw_prompt = session_row.get('system_prompt')
        if isinstance(raw_prompt, str) and raw_prompt.strip():
            stored_prompt = raw_prompt
    agent._cached_system_prompt = stored_prompt
    return bool(stored_prompt)

def _is_transient_network_error(exc: BaseException) -> bool:
    """Return True for transient network errors safe to log + swallow.

    The crash class targeted by #31066 / #31110: an unhandled Telegram
    ``TimedOut`` (or peer ``NetworkError`` / ``httpx`` connection error)
    propagating to the event loop and killing the entire gateway
    process. These are by definition transient — the next poll cycle or
    user action recovers — so they must never crash the process.

    Walk the exception cause chain so wrapped errors (e.g. PTB's
    ``NetworkError`` wrapping ``httpx.ConnectError``) are still
    classified. The chain is bounded to avoid pathological cycles.
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    depth = 0
    transient_class_names = {'TimedOut', 'NetworkError', 'ReadError', 'WriteError', 'ConnectError', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'RemoteProtocolError', 'ServerDisconnectedError', 'ClientConnectorError', 'ClientOSError'}
    while cur is not None and depth < 12:
        ident = id(cur)
        if ident in seen:
            break
        seen.add(ident)
        depth += 1
        name = type(cur).__name__
        if name in transient_class_names:
            return True
        cur = cur.__cause__ or cur.__context__
    return False

def _gateway_loop_exception_handler(loop: 'asyncio.AbstractEventLoop', context: Dict[str, Any]) -> None:
    """Loop-level safety net for transient network errors.

    Installed once during :func:`start_gateway`. Catches the
    ``telegram.error.TimedOut`` crash class (issues #31066 / #31110)
    and any peer transient network error before it can kill the
    gateway process. Logs at WARNING with full traceback so the
    originating call site stays diagnosable; non-transient errors
    are forwarded to the default loop handler so real bugs still
    surface.
    """
    exc = context.get('exception')
    if exc is not None and _is_transient_network_error(exc):
        task = context.get('future') or context.get('task')
        task_name = ''
        if task is not None:
            try:
                task_name = task.get_name() if hasattr(task, 'get_name') else repr(task)
            except Exception:
                task_name = repr(task)
        logger.warning('Gateway swallowed transient network error from %s: %s: %s', task_name or '<unknown task>', type(exc).__name__, exc, exc_info=(type(exc), exc, exc.__traceback__))
        return
    loop.default_exception_handler(context)

def _redact_gateway_user_facing_secrets(text: str) -> str:
    """Secret redaction before text can leave the gateway.

    Delegates to the authoritative ``agent.redact.redact_sensitive_text`` — the
    same Tirith-grade redactor already applied to logs, tool output, and
    approval-command prompts — so the outbound chat path masks the full
    credential set the startup banner promises ("chat responses are scrubbed
    before delivery"), not a divergent subset. ``force=True`` honors redaction
    even when ``security.redact_secrets`` is off, matching the
    ``_redact_approval_command`` reasoning (#23810).

    The narrow ``_GATEWAY_SECRET_PATTERNS`` set runs as a belt-and-suspenders
    second pass so nothing the gateway historically caught can regress, and so
    redaction still degrades gracefully if the import ever fails.
    """
    redacted = str(text or '')
    try:
        from agent.redact import redact_sensitive_text
        redacted = redact_sensitive_text(redacted, force=True)
    except Exception:
        pass
    for pattern in _GATEWAY_SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: (m.group(1) if m.lastindex else '') + '[REDACTED]', redacted)
    return redacted

def _redact_approval_command(cmd: 'str | None') -> str:
    """Redact credentials from a command before it goes into an approval prompt.

    Tirith's *findings* are already redacted, but the gateway approval prompt
    is built from the raw command string, so a credential-shaped value Tirith
    flagged would otherwise be echoed verbatim to the chat platform (#48456).
    Uses ``redact_sensitive_text(force=True)`` — the same Tirith-grade redactor
    — so the prompt honors redaction even when ``security.redact_secrets`` is
    off. Module-level so the wiring is unit-testable (the call site is a deeply
    nested gateway closure that cannot be driven directly).
    """
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(str(cmd or ''), force=True)

def _format_exec_approval_fallback(command: str, description: str, command_prefix: str, *, allow_permanent: bool=True, allow_session: bool=True, smart_denied: bool=False) -> str:
    """Render the text fallback from approval capabilities, not platform names."""
    cmd_preview = command[:200] + '...' if len(command) > 200 else command
    heading = '⚠️ **Dangerous command requires approval:**'
    if smart_denied:
        heading = '⚠️ **Smart DENY — owner override for one operation:**'
    choices = [f'Reply `{command_prefix}approve` to execute this one operation']
    if not smart_denied and allow_session:
        choices.append(f'`{command_prefix}approve session` to approve this pattern for the session')
        if allow_permanent:
            choices.append(f'`{command_prefix}approve always` to approve permanently')
    choices.append(f'`{command_prefix}deny` to cancel')
    return f'{heading}\n```\n{cmd_preview}\n```\nReason: {description}\n\n' + ', '.join(choices[:-1]) + f', or {choices[-1]}.'

def _gateway_provider_error_reply(text: str) -> str:
    """Map raw provider/API errors to a short user-safe Telegram reply."""
    if _GATEWAY_AUTH_ERROR_RE.search(text):
        return '⚠️ Provider authentication failed. Check the configured credentials; raw provider details are in the gateway logs.'
    if _GATEWAY_PROVIDER_POLICY_RE.search(text):
        return '⚠️ The model provider rejected the request. I kept the raw provider error out of chat; check gateway logs for details or try rephrasing.'
    if _GATEWAY_RATE_LIMIT_RE.search(text):
        return '⏱️ The model provider is rate-limiting requests. Please wait a moment and try again.'
    return '⚠️ The model provider failed after retries. I kept raw provider details out of chat; check gateway logs for diagnostics.'
_GATEWAY_PROVIDER_ERROR_SHAPE_RE = re.compile('^\\s*(\\W*\\s*)?(api\\s+(?:call\\s+)?failed|provider\\s+authentication\\s+failed|non-retryable\\s+error|rate\\s+limited\\s+after\\s+\\d+\\s+retries|error\\s+code\\s*:|http\\s*\\d{3}\\b|incorrect\\s+api\\s+key|invalid\\s+api\\s+key)', re.IGNORECASE)

def _looks_like_gateway_provider_error(text: str) -> bool:
    """True when text is infrastructure/provider failure, not normal content.

    Two heuristics combined so the rewrite only fires on actual provider
    error envelopes, not on assistant prose that happens to mention an
    HTTP status code:

    1. The text is short — real provider errors are 1–3 lines of envelope
       text; assistant answers are usually longer.
    2. AND the error marker appears at the start of the message (optionally
       behind a punctuation/symbol prefix), not buried mid-paragraph in an
       explanation like "HTTP 404 means 'not found' — ...".
    """
    if not text:
        return False
    body = str(text).strip()
    if len(body) > 400 or body.count('\n') > 4:
        return False
    return bool(_GATEWAY_PROVIDER_ERROR_SHAPE_RE.search(body))

def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """Sanitize final gateway replies before sending them to chat surfaces.

    Every human-facing chat surface (Telegram, WhatsApp, Discord, Slack,
    Signal, Matrix, plugin platforms, etc.) should receive concise, safe
    provider failure categories with secrets redacted instead of raw HTTP
    bodies, request IDs, leaked credentials, or policy text. Only programmatic
    surfaces in ``_GATEWAY_RAW_TEXT_PLATFORMS`` (CLI/TUI ``local`` diagnostics,
    API JSON, webhook payloads) keep the raw text unchanged.
    """
    if not text:
        return text
    if _gateway_surface_passes_raw_text(platform):
        return text
    if str(text).strip().startswith(INTERRUPT_WAITING_FOR_MODEL_PREFIX):
        return ''
    redacted = _redact_gateway_user_facing_secrets(str(text))
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted

def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery.

    Local/CLI sessions keep the raw diagnostic stream. Messaging gateway
    surfaces should not receive transient auxiliary/compression chatter.
    """
    text = str(message or '').strip()
    if not text:
        return None
    if _gateway_surface_passes_raw_text(platform):
        return text
    text = _redact_gateway_user_facing_secrets(text)
    if _TELEGRAM_NOISY_STATUS_RE.search(text):
        if not (_gateway_compression_progress_notices_enabled() and _COMPRESSION_PROGRESS_STATUS_RE.search(text)):
            return None
    if _looks_like_gateway_provider_error(text):
        return _gateway_provider_error_reply(text)
    return text

def render_notice_line(notice) -> str:
    """Render an AgentNotice to a single plaintext line for messaging platforms.

    Messaging has no persistent status bar (unlike the TUI), so a notice is a
    one-shot standalone push. The notice policy already bakes the level glyph
    (⚠ / • / ✕ / ✓) into the text, and the TUI + CLI REPL render that text
    verbatim — so we emit it as-is here too. Prepending a per-level glyph would
    DOUBLE it ("⚠ ⚠ Credits 90% used", "⛔ ✕ Credit access paused"). Plaintext
    only — no markdown — so it renders uniformly across Telegram/Discord/Slack/
    SMS without per-platform escaping. Fail-soft: a malformed/empty notice
    degrades to "" rather than raising on the agent's callback path.
    """
    return str(getattr(notice, 'text', '') or '').strip()

async def _send_or_update_status_coro(adapter, chat_id, status_key, content, metadata):
    """Route a status message through adapter.send_or_update_status when supported.

    Issue #30045: adapters that implement send_or_update_status (currently
    Telegram) edit the previous bubble for the same status_key instead of
    appending a new one. Adapters without the method fall back to plain send.
    """
    sender = getattr(adapter, 'send_or_update_status', None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await adapter.send(chat_id, content, metadata=metadata)

def _resolve_progress_thread_id(platform: Any, source_thread_id: Any, event_message_id: Any, *, reply_in_thread: bool=True) -> Optional[str]:
    """Return thread/root ID that progress/status bubbles should target.

    ``reply_in_thread=False`` (Slack ``platforms.slack.extra.reply_in_thread``)
    disables the synthetic-thread fallback: progress messages must not create
    a thread the final flat reply would then inherit. A source.thread_id equal
    to the event's own message id is the adapter's synthetic session-keying
    thread, not a real thread — treat it as "no thread" too (#18859).
    """
    platform_value = getattr(platform, 'value', platform)
    platform_key = str(platform_value or '').lower()
    if not reply_in_thread:
        if source_thread_id and event_message_id and (str(source_thread_id) == str(event_message_id)):
            return None
        return str(source_thread_id) if source_thread_id else None
    if source_thread_id:
        return str(source_thread_id)
    if platform_key in {'slack', 'mattermost'} and event_message_id:
        return str(event_message_id)
    return None

def _has_platform_display_override(user_config: dict, platform_key: str, setting: str) -> bool:
    """Return True when display.platforms.<platform> explicitly sets setting."""
    display = user_config.get('display') if isinstance(user_config, dict) else None
    if not isinstance(display, dict):
        return False
    platforms = display.get('platforms')
    if not isinstance(platforms, dict):
        return False
    platform_cfg = platforms.get(platform_key)
    return isinstance(platform_cfg, dict) and setting in platform_cfg

def _resolve_gateway_display_bool(user_config: dict, platform_key: str, setting: str, *, default: bool=False, platform: Any=None, require_platform_override_for: set[Any] | None=None) -> bool:
    """Resolve a boolean display setting with optional platform-only opt-in.

    Some display features expose assistant scratch text rather than deliberate
    user-facing output.  For high-noise threaded chat surfaces such as
    Mattermost, a global opt-in is too broad: they must be enabled with an
    explicit display.platforms.<platform>.<setting> override.
    """
    current_platform = _gateway_platform_value(platform or platform_key)
    platform_only = {_gateway_platform_value(candidate) for candidate in require_platform_override_for or set()}
    if current_platform in platform_only and (not _has_platform_display_override(user_config, platform_key, setting)):
        return False
    from gateway.display_config import resolve_display_setting
    value = resolve_display_setting(user_config, platform_key, setting, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'true', 'yes', '1', 'on'}
    if value is None:
        return bool(default)
    return bool(value)

def _telegramize_command_mentions(text: str, platform: Any) -> str:
    """Rewrite slash-command mentions to Telegram-valid command names.

    Telegram Bot API command names allow only lowercase letters, digits, and
    underscores.  Keep other platform renderings unchanged, but normalize
    Telegram help text so command mentions remain clickable/valid there.
    """
    platform_value = getattr(platform, 'value', platform)
    if platform_value != 'telegram':
        return text
    from hermes_cli.commands import _sanitize_telegram_name

    def _replace(match: re.Match[str]) -> str:
        sanitized = _sanitize_telegram_name(match.group(1))
        return f'/{sanitized}' if sanitized else match.group(0)
    return _TELEGRAM_COMMAND_MENTION_RE.sub(_replace, text)
_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60
_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT = 30.0

def _coerce_gateway_timestamp(value: Any) -> Optional[float]:
    """Best-effort conversion of stored gateway timestamps to epoch seconds.

    Missing/unparseable timestamps return None so legacy transcripts keep the
    historical auto-continue behaviour instead of being silently dropped.
    Accepts: datetime, epoch seconds (int/float), epoch milliseconds (when
    the magnitude exceeds year-2286), ISO-8601 strings (with or without a
    trailing ``Z``), and numeric strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 10000000000 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
            return numeric / 1000.0 if numeric > 10000000000 else numeric
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None

def _auto_continue_freshness_window() -> float:
    """Return the configured auto-continue freshness window in seconds.

    Thin wrapper that delegates to the canonical implementation in
    ``gateway.session`` (the single source of truth shared with the
    routing-time zombie gate in ``get_or_create_session``).  Reads
    ``HERMES_AUTO_CONTINUE_FRESHNESS`` (bridged from ``config.yaml``
    ``agent.gateway_auto_continue_freshness`` at gateway startup, same
    pattern as ``HERMES_AGENT_TIMEOUT``).  Falls back to the module default
    when unset or malformed.  Non-positive values disable the freshness gate
    (restores the pre-fix "always fresh" behaviour for users who want to opt
    out).  Kept here so existing call sites and test patches importing it
    from ``gateway.run`` continue to work.
    """
    from gateway.session import auto_continue_freshness_window
    return auto_continue_freshness_window()

def _startup_restore_drain_timeout_secs() -> float:
    """Max seconds ``_finish_startup_restore`` waits on boot auto-resume turns
    before releasing the inbound gate and draining the queue.

    While startup restore is in progress the gateway QUEUES every inbound
    message (``_queue_startup_restore_event``) instead of processing it, so no
    channel gets a reply until the gate opens.  The gate is opened by
    ``_finish_startup_restore``, which waits for the synthetic boot
    auto-resume turns to finish.  A single long resumed turn therefore held
    the gate shut for every channel — inbound piled up unanswered for as long
    as that one turn ran.

    This bounds that wait.  Duplicate-agent safety does NOT depend on the
    wait: ``_schedule_resume_pending_sessions`` claims each session's
    ``_running_agents`` slot SYNCHRONOUSLY (before the gate ever runs), so a
    message drained while a resume turn is still running queues behind that
    slot rather than spawning a second agent.  So on timeout we release the
    gate and let the slow turn finish in the background.

    Reads ``HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT`` (bridged from
    ``config.yaml`` ``agent.gateway_startup_restore_drain_timeout`` at gateway
    startup, same pattern as the other ``agent.*`` knobs).  Non-positive
    disables the bound (restores the historical "wait forever" behaviour).
    """
    raw = os.environ.get('HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT')
    if raw is None or raw == '':
        return float(_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT)

def _float_env(name: str, default: float) -> float:
    """Read an env var as float, falling back to ``default`` on typos/empty.

    A misconfigured env var (e.g. ``HERMES_AGENT_TIMEOUT=abc``) must not
    crash the gateway or an agent turn.  Unset/empty also falls back.
    """
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)

def _stamp_hygiene_compression_provenance(agent: Any, desc: str, provenance: 'ActivityProvenance', debug_label: str) -> None:
    """Best-effort activity provenance stamp for hygiene compression transitions."""
    try:
        agent._touch_activity(desc, provenance=provenance)
    except Exception:
        logger.debug(debug_label, exc_info=True)

def _is_fresh_gateway_interruption(value: Any, *, now: Optional[float]=None, window_secs: Optional[float]=None) -> bool:
    """Return True when an interruption marker is fresh enough to auto-continue.

    Unknown timestamps are treated as fresh for backward compatibility with
    legacy transcripts (pre-dating timestamp persistence) and with in-memory
    test scaffolding that constructs history entries without timestamps.

    A non-positive ``window_secs`` disables the gate (always fresh), which
    restores the pre-fix behaviour for users who opt out via config.
    """
    window = float(window_secs) if window_secs is not None else float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    if window <= 0:
        return True
    timestamp = _coerce_gateway_timestamp(value)
    if timestamp is None:
        return True
    current = time.time() if now is None else now
    return current - timestamp <= window

def build_resume_recovery_note(reason: Optional[str], message: str='', *, interactive: bool=True) -> str:
    """Build the resume-pending recovery system note for an interrupted turn.

    ``reason`` is the session's ``resume_reason`` (``restart_timeout``,
    ``shutdown_timeout``, or anything else → generic interruption phrasing).
    ``message`` is the user's NEW message text; empty means this is the
    startup auto-resume turn synthesized by
    ``_schedule_resume_pending_sessions`` with no human message attached.

    ``interactive`` selects the empty-message guidance: on interactive
    platforms a human is present, so "report the restore and ask what next"
    is right.  On non-interactive event platforms (webhook, API server —
    adapters with ``interactive_resume = False``) nobody can answer; the
    resumed turn must instead complete the interrupted work, or the task is
    silently abandoned behind a "restored" acknowledgement that goes
    nowhere (#57056).
    """
    reason_phrase = 'a gateway restart' if reason == 'restart_timeout' else 'a gateway shutdown' if reason == 'shutdown_timeout' else 'a gateway interruption'
    if message:
        resume_guidance = "Address the user's NEW message below FIRST and focus on what the user is asking now."
        tail_guidance = 'Do NOT re-execute old tool calls — skip any unfinished work from the conversation history.'
    elif interactive:
        resume_guidance = 'Report to the user that the session was restored successfully and ask what they would like to do next.'
        tail_guidance = 'Do NOT re-execute old tool calls — skip any unfinished work from the conversation history.'
    else:
        resume_guidance = "No user is present on this non-interactive platform, so do NOT emit a 'session restored' acknowledgement or ask questions. Review the conversation history and CONTINUE the interrupted task to completion."
        tail_guidance = 'Do NOT re-run tool calls whose results already appear in the history — resume from the first step that has no recorded result.'
    return f'[System note: The previous turn was interrupted by {reason_phrase}; the gateway is now back online. Any restart/shutdown command in the history has already run — do NOT re-execute or verify it. {resume_guidance} {tail_guidance}]' + (f'\n\n{message}' if message else '')
_ASSISTANT_REPLAY_FIELDS: tuple[str, ...] = ('reasoning', 'reasoning_content', 'reasoning_details', 'codex_reasoning_items', 'codex_message_items', 'finish_reason')

def _build_replay_entry(role: str, content: Any, msg: Dict[str, Any], preserve_timestamp: bool=False) -> Dict[str, Any]:
    """Build a replay entry for a non-tool-calling message, preserving the
    assistant fields the agent's API builders rely on for multi-turn fidelity.

    Lifted out of the inline ``run_sync`` closure so the field whitelist can
    be unit-tested in isolation.  Mirrors the ``_ASSISTANT_REPLAY_FIELDS``
    contract above.

    ``preserve_timestamp``: when True, copy the source row's ``timestamp``
    onto the replay entry. Currently only user messages need this — the
    stale-dangerous-confirmation stripper in ``agent/replay_cleanup.py``
    reads the timestamp to decide whether a confirmation is too old to
    replay safely.  Assistant/tool messages are not timestamp-stripped in
    the same way, so we keep the existing default of dropping it.

    Empty values: most fields are dropped when falsy (matching the original
    PR #2974 behaviour) since an empty list/string for those carries no
    information.  The exception is ``reasoning_content``: DeepSeek/Kimi
    thinking-mode replay treats an empty string as a meaningful sentinel
    that ``_copy_reasoning_content_for_api`` upgrades to a single space.
    Dropping it here would make the gateway send no ``reasoning_content``
    at all on the next turn, which can cause HTTP 400 from strict thinking
    providers.
    """
    entry: Dict[str, Any] = {'role': role, 'content': content}
    _sidecar = msg.get('api_content')
    if role in ('user', 'assistant') and isinstance(_sidecar, str) and _sidecar and (content == msg.get('content')):
        entry['api_content'] = _sidecar
    if role == 'assistant':
        for _rkey in _ASSISTANT_REPLAY_FIELDS:
            if _rkey not in msg:
                continue
            _rval = msg.get(_rkey)
            if _rkey == 'reasoning_content':
                if _rval is None:
                    continue
            elif not _rval:
                continue
            entry[_rkey] = _rval
    if preserve_timestamp:
        ts = msg.get('timestamp')
        if ts:
            entry['timestamp'] = ts
    return entry
_TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER = 'observed Telegram group context'
_OBSERVED_GROUP_CONTEXT_HEADER = '[Observed Telegram group context - context only, not requests]'
_CURRENT_ADDRESSED_MESSAGE_HEADER = '[Current addressed message - answer only this unless it explicitly asks you to use the observed context]'

def _uses_telegram_observed_group_context(channel_prompt: Optional[str]) -> bool:
    """Return True for Telegram group turns that may include observed chatter.

    Telegram's observe-unmentioned mode persists skipped group chatter so a
    later @mention can see it. Those rows must not replay as ordinary user
    turns: a weak wake word like ``@bot cambio`` should not make the model treat
    old unmentioned chatter as pending work. The Telegram adapter marks these
    turns with a channel prompt; this helper keeps the run-path check explicit
    and unit-testable.
    """
    return bool(channel_prompt and _TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER in channel_prompt)

def _csv_or_list_to_set(raw: Any) -> set[str]:
    """Normalize a config list or comma-separated scalar into a string set."""
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    s = str(raw).strip()
    if not s:
        return set()
    return {part.strip() for part in s.split(',') if part.strip()}

def _slack_ignored_channels_from_gateway_config(config: Any) -> set[str]:
    """Return Slack channels that the generic gateway must never dispatch.

    The Slack adapter has the first-line drop, but this runner-level guard is
    intentionally duplicated as a fail-safe. If a future Slack code path, test
    hook, malformed event, or stale adapter instance bypasses the Slack plugin
    adapter, ignored channels still cannot reach auth, pairing, sessions, or
    the agent/home-channel prompt pipeline.
    """
    platform_cfg = getattr(config, 'platforms', {}).get(Platform.SLACK)
    raw = None
    if platform_cfg is not None:
        raw = getattr(platform_cfg, 'extra', {}).get('ignored_channels')
    if raw is None:
        raw = os.getenv('SLACK_IGNORED_CHANNELS') or None
    return _csv_or_list_to_set(raw)

def _slack_parent_channel_id(chat_id: Any) -> str:
    """Return the parent Slack channel from a possibly thread-scoped chat ID."""
    if not chat_id:
        return ''
    return str(chat_id).split(':', 1)[0]

def _is_slack_ignored_channel(config: Any, chat_id: Any) -> bool:
    """Check the generic Slack gateway blacklist for channel or thread IDs."""
    channel_id = _slack_parent_channel_id(chat_id)
    ignored = _slack_ignored_channels_from_gateway_config(config)
    return bool(channel_id and ('*' in ignored or channel_id in ignored))

def _message_timestamps_enabled(user_config: Optional[dict]) -> bool:
    """True when gateway.message_timestamps.enabled is opted in.

    Default OFF: injecting a ``[Tue 2026-04-28 13:40:53 CEST]`` prefix onto
    every user message changes what the model sees for all gateway users, so
    it must be explicitly enabled in config.yaml under
    ``gateway.message_timestamps.enabled``.
    """
    if not isinstance(user_config, dict):
        return False
    gw = user_config.get('gateway')
    if not isinstance(gw, dict):
        return False
    mt = gw.get('message_timestamps')
    if isinstance(mt, dict):
        return bool(mt.get('enabled', False))
    return bool(mt)

def _build_gateway_agent_history(history: List[Dict[str, Any]], *, channel_prompt: Optional[str]=None, inject_timestamps: bool=False) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Convert stored gateway transcript rows into agent replay messages.

    Observed Telegram group rows are returned as API-only context for the
    current addressed message instead of being replayed as normal prior user
    turns.  Keeping that context out of ``conversation_history`` avoids
    consecutive-user repair merging it with the live user turn and then hiding
    the current message behind ``history_offset`` during persistence.

    When ``inject_timestamps`` is True (gateway.message_timestamps.enabled),
    each replayed user message is rendered with a single human-readable
    timestamp prefix from its stored metadata.
    """
    from hermes_time import get_timezone as _get_msg_tz
    from gateway.message_timestamps import render_user_content_with_timestamp as _render_msg_ts
    _msg_tz = _get_msg_tz()
    agent_history: List[Dict[str, Any]] = []
    observed_group_context: List[str] = []
    separate_observed_context = _uses_telegram_observed_group_context(channel_prompt)
    for msg in history or []:
        role = msg.get('role')
        if not role:
            continue
        if role in {'session_meta'}:
            continue
        if role == 'system':
            continue
        content = msg.get('content')
        if inject_timestamps and role == 'user' and isinstance(content, str):
            content = _render_msg_ts(content, msg.get('timestamp'), tz=_msg_tz)
        if separate_observed_context and msg.get('observed') and (role == 'user') and content:
            observed_group_context.append(str(content).strip())
            continue
        has_tool_calls = 'tool_calls' in msg
        has_tool_call_id = 'tool_call_id' in msg
        is_tool_message = role == 'tool'
        if has_tool_calls or has_tool_call_id or is_tool_message:
            clean_msg = {k: v for k, v in msg.items() if k not in {'timestamp', 'observed'}}
            agent_history.append(clean_msg)
        elif content:
            if role == 'user':
                content = _strip_auto_continue_noise(content)
                if not content:
                    continue
            if msg.get('mirror'):
                mirror_src = msg.get('mirror_source', 'another session')
                content = f'[Delivered from {mirror_src}] {content}'
            entry = _build_replay_entry(role, content, msg, preserve_timestamp=role == 'user')
            agent_history.append(entry)
    agent_history = strip_interrupted_tool_tails(agent_history)
    agent_history = strip_dangling_tool_call_tail(agent_history)
    agent_history = strip_stale_dangerous_confirmations(agent_history, now=time.time())
    observed_context = '\n'.join(observed_group_context).strip() or None
    return (agent_history, observed_context)

def _select_cached_agent_history(persisted_history: List[Dict[str, Any]], live_history: Any) -> List[Dict[str, Any]]:
    """Prefer a cached agent's live in-memory transcript over a shorter
    persisted one.

    Guards the FTS write-corruption case (#50502): when message writes fail
    silently through corrupt FTS triggers, the next turn reloads a stale/empty
    ``conversation_history`` from disk even though the same cached ``AIAgent``
    still holds the full live ``_session_messages``. Replacing the live
    transcript with that shorter persisted copy causes immediate same-session
    amnesia. When the live transcript is strictly longer, keep it.

    Returns ``persisted_history`` unchanged unless the live copy is a longer
    list, in which case a copy of the live transcript is returned.
    """
    if isinstance(live_history, list) and len(live_history) > len(persisted_history):
        return list(live_history)
    return persisted_history

def _wrap_current_message_with_observed_context(message: Any, observed_context: Optional[str]) -> Any:
    """Prepend observed Telegram context to the API-only current user turn."""
    if not observed_context:
        return message
    prefix = f'{_OBSERVED_GROUP_CONTEXT_HEADER}\n{observed_context}\n\n{_CURRENT_ADDRESSED_MESSAGE_HEADER}\n'
    if isinstance(message, str):
        return f'{prefix}{message}'
    if isinstance(message, list):
        wrapped = [dict(part) if isinstance(part, dict) else part for part in message]
        for part in wrapped:
            if isinstance(part, dict) and part.get('type') == 'text':
                part['text'] = f"{prefix}{part.get('text', '')}"
                return wrapped
        return [{'type': 'text', 'text': prefix.rstrip()}] + wrapped
    return message

def _last_transcript_timestamp(history: Optional[List[Dict[str, Any]]]) -> Any:
    """Return the ``timestamp`` of the last usable transcript row, if any.

    Skips metadata-only rows (``session_meta``, system injections) that are
    dropped before being handed to the agent.  Returns ``None`` when no
    usable row carries a timestamp — callers should treat that as "fresh"
    for backward compatibility.
    """
    if not history:
        return None
    for msg in reversed(history):
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if not role or role in {'session_meta', 'system'}:
            continue
        ts = msg.get('timestamp')
        if ts is not None:
            return ts
        return None
    return None
_AUTO_APPEND_MEDIA_TOOL_NAMES = {'text_to_speech', 'text_to_speech_tool', 'image_generate', 'bfl_flux3_get_result'}
from agent.replay_cleanup import strip_interrupted_tool_tails, strip_dangling_tool_call_tail, strip_stale_dangerous_confirmations
_AUTO_CONTINUE_NOTE_PREFIX = '[System note: Your previous turn'
_AUTO_CONTINUE_FALLBACK_PREFIX = '[System note: A new message'

def _is_auto_continue_noise(content: Any) -> bool:
    """Return True if this user-message content is a gateway-injected
    auto-continue note that should NOT be replayed as a real user turn."""
    if not isinstance(content, str):
        return False
    return content.startswith(_AUTO_CONTINUE_NOTE_PREFIX) or content.startswith(_AUTO_CONTINUE_FALLBACK_PREFIX)

def _strip_auto_continue_noise(content: Any) -> Any:
    """Remove persisted gateway auto-continue note prefix from user text.

    Older gateway builds prepended the recovery note directly to the user
    message, so the transcript row can contain both the synthetic note and
    the user's real question.  Strip one or more leading synthetic notes while
    preserving any real text that follows.
    """
    if not _is_auto_continue_noise(content):
        return content
    text = str(content)
    while _is_auto_continue_noise(text):
        end = text.find(']')
        if end < 0:
            return ''
        text = text[end + 1:].lstrip()
    return text
_JSON_MEDIA_TOOL_PATH_FIELDS = ('host_image', 'image', 'agent_visible_image')
_TOOL_MEDIA_RE = re.compile('MEDIA:((?:[A-Za-z]:[/\\\\]|/|~\\/)\\S+\\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|apk|ipa))', re.IGNORECASE)

def _collect_auto_append_media_tags(messages: List[Dict[str, Any]], history_offset: int=0, history_media_paths: Optional[set]=None) -> tuple[List[str], bool]:
    """Collect real media tags from current-turn producer-tool results only.

    Two layered guards keep stale/example MEDIA: strings out of the reply:

    1. Producer-tool allowlist: only tools that intentionally emit deliverable
       artifacts (TTS) are eligible. Documentation, logs, and search results can
       contain example strings such as MEDIA:/absolute/path/to/file, which must
       never be delivered as attachments. (Fixes the original report behind #16721.)
    2. Current-turn isolation: only messages produced this turn are scanned, so a
       tool result from an earlier turn (still present in the full message list)
       cannot leak onto a later text-only reply (#34608).

    Mid-run context compression can rewrite/shrink the message list below the
    original history length. When that happens the slice boundary is no longer
    trustworthy, so fall back to scanning every message and rely on
    ``history_media_paths`` for dedup, preserving the compression-safe behaviour
    of #160. The producer-tool allowlist still applies on the fallback path.
    """
    history_media_paths = history_media_paths or set()
    if history_offset and len(messages) >= history_offset:
        new_messages = messages[history_offset:]
    else:
        new_messages = messages
    tool_name_by_call_id: Dict[str, str] = {}
    for msg in new_messages:
        if msg.get('role') != 'assistant':
            continue
        for call in msg.get('tool_calls') or []:
            call_id = call.get('id') or call.get('call_id')
            fn = call.get('function') or {}
            name = str(fn.get('name') or call.get('name') or '')
            if call_id and name:
                tool_name_by_call_id[str(call_id)] = name
    media_tags: List[str] = []
    has_voice_directive = False
    for msg in new_messages:
        if msg.get('role') not in ('tool', 'function'):
            continue
        call_id = str(msg.get('tool_call_id') or msg.get('call_id') or '')
        if tool_name_by_call_id.get(call_id) not in _AUTO_APPEND_MEDIA_TOOL_NAMES:
            continue
        content = str(msg.get('content') or '')
        tool_name = tool_name_by_call_id.get(call_id)
        if tool_name == 'image_generate' and 'MEDIA:' not in content:
            try:
                payload = json.loads(content)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get('success'):
                for field in _JSON_MEDIA_TOOL_PATH_FIELDS:
                    path = payload.get(field)
                    if isinstance(path, str) and _TOOL_MEDIA_RE.fullmatch(f'MEDIA:{path}') and (path not in history_media_paths):
                        media_tags.append(f'MEDIA:{path}')
                        break
            continue
        if 'MEDIA:' not in content:
            continue
        for match in _TOOL_MEDIA_RE.finditer(content):
            path = match.group(1).strip().rstrip('",}')
            if path and path not in history_media_paths:
                media_tags.append(f'MEDIA:{path}')
        if '[[audio_as_voice]]' in content:
            has_voice_directive = True
    return (media_tags, has_voice_directive)

def _collect_history_media_paths(agent_history: List[Dict[str, Any]]) -> set:
    """Collect every media path already delivered in prior assistant/tool output.

    Used to dedup auto-appended and model-emitted MEDIA tags so the same file
    is not re-sent on later turns. Covers three delivery shapes:
      * ``MEDIA:<path>`` text tags in tool results,
      * ``MEDIA:<path>`` text tags in assistant messages (model-generated tags),
      * ``image_generate`` JSON-payload paths (``host_image`` / ``image`` /
        ``agent_visible_image``), which carry no MEDIA: tag.

    Missing the JSON-payload shape caused #46627; missing the assistant-message
    shape caused repeated delivery when the model echoed a previous MEDIA tag.
    """
    paths: set = set()
    tool_name_by_call_id: Dict[str, str] = {}

    def _add_text_media_paths(content: str) -> None:
        for match in _TOOL_MEDIA_RE.finditer(content):
            path = match.group(1).strip().rstrip('",}')
            if path:
                paths.add(path)
        media_files, _ = BasePlatformAdapter.extract_media(content)
        paths.update((path for path, _is_voice in media_files))
    for msg in agent_history:
        if msg.get('role') == 'assistant':
            for call in msg.get('tool_calls') or []:
                cid = call.get('id') or call.get('call_id')
                fn = call.get('function') or {}
                name = str(fn.get('name') or call.get('name') or '')
                if cid and name:
                    tool_name_by_call_id[str(cid)] = name
    for msg in agent_history:
        role = msg.get('role')
        if role == 'assistant':
            content = str(msg.get('content', '') or '')
            if 'MEDIA:' in content:
                _add_text_media_paths(content)
            continue
        if role not in {'tool', 'function'}:
            continue
        content = str(msg.get('content', '') or '')
        if 'MEDIA:' in content:
            _add_text_media_paths(content)
            continue
        cid = str(msg.get('tool_call_id') or msg.get('call_id') or '')
        if tool_name_by_call_id.get(cid) == 'image_generate':
            try:
                payload = json.loads(content)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get('success'):
                for field in _JSON_MEDIA_TOOL_PATH_FIELDS:
                    jp = payload.get(field)
                    if isinstance(jp, str) and jp:
                        paths.add(jp)
                        break
    return paths

def _ensure_ssl_certs() -> None:
    """Set SSL_CERT_FILE if the system doesn't expose CA certs to Python.

    Windows startup paths (Desktop, Scheduled Tasks, installer children) can
    occasionally inherit a stale SSL_CERT_FILE. Returning just because the
    variable is present makes every later httpx/OpenAI client construction fail
    with FileNotFoundError from ssl.load_verify_locations(). Treat a missing
    path as unset and fall back to certifi instead.
    """
    configured_cert = os.environ.get('SSL_CERT_FILE')
    if configured_cert:
        if os.path.exists(configured_cert):
            return
        logging.getLogger(__name__).warning('Ignoring stale SSL_CERT_FILE=%r because the path does not exist', configured_cert)
        os.environ.pop('SSL_CERT_FILE', None)
    import ssl
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and os.path.exists(candidate):
            os.environ['SSL_CERT_FILE'] = candidate
            return
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
        return
    except ImportError:
        pass
    for candidate in ('/etc/ssl/certs/ca-certificates.crt', '/etc/pki/tls/certs/ca-bundle.crt', '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem', '/etc/ssl/ca-bundle.pem', '/etc/ssl/cert.pem', '/etc/pki/tls/cert.pem', '/usr/local/etc/openssl@1.1/cert.pem', '/opt/homebrew/etc/openssl@1.1/cert.pem'):
        if os.path.exists(candidate):
            os.environ['SSL_CERT_FILE'] = candidate
            return

def _home_target_env_var(platform_name: str) -> str:
    """Return the configured home-target env var for a platform.

    Consults built-in ``_HOME_TARGET_ENV_VARS`` first, then the plugin
    registry via ``cron.scheduler._resolve_home_env_var``, then falls back
    to ``<PLATFORM>_HOME_CHANNEL`` for unknown names.
    """
    from cron.scheduler import _resolve_home_env_var
    resolved = _resolve_home_env_var(platform_name)
    if resolved:
        return resolved
    return f'{platform_name.upper()}_HOME_CHANNEL'

def _home_thread_env_var(platform_name: str) -> str:
    """Return the optional thread/topic env var for a platform home target."""
    return f'{_home_target_env_var(platform_name)}_THREAD_ID'

def _restart_notification_pending() -> bool:
    """Return True when a /restart completion marker is waiting to be delivered."""
    return (_hermes_home / '.restart_notify.json').exists()

def _planned_restart_notification_path() -> Path:
    return _hermes_home / '.restart_pending.json'

def _planned_restart_notification_pending() -> bool:
    """Return True when a non-chat planned restart should notify home channels."""
    return _planned_restart_notification_path().exists()

def _clear_planned_restart_notification() -> None:
    _planned_restart_notification_path().unlink(missing_ok=True)
os.environ['_HERMES_GATEWAY'] = '1'
_ensure_ssl_certs()
sys.path.insert(0, str(Path(__file__).parent.parent))
from hermes_constants import get_hermes_home, get_hermes_home_override
from utils import atomic_json_write, is_truthy_value
_hermes_home = get_hermes_home()
from dotenv import load_dotenv
from hermes_cli.env_loader import load_hermes_dotenv
_env_path = _hermes_home / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / '.env')

def _reload_runtime_env_preserving_config_authority() -> None:
    """Reload .env for fresh credentials without letting stale .env override config.

    Gateway processes are long-lived, so per-turn code reloads ~/.duck-agent/.env to
    pick up rotated API keys. config.yaml remains authoritative for agent budget
    settings such as agent.max_turns; otherwise a stale HERMES_MAX_ITERATIONS in
    .env can replace the startup bridge on later turns.

    In multiplex mode this is a NO-OP for the credential reload: secrets come
    from the per-turn ``set_secret_scope`` (installed by ``_profile_runtime_scope``)
    which loads the routed profile's ``.env`` into an isolated mapping. Mutating
    the process-global ``os.environ`` here would defeat that isolation and leak
    the default profile's keys to every profile's turns and subprocesses.
    """
    from agent.secret_scope import is_multiplex_active
    if is_multiplex_active():
        _bridge_max_turns_from_config(_hermes_home)
        return
    load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / '.env')
    _bridge_max_turns_from_config(_hermes_home)

def _bridge_max_turns_from_config(home: 'Path') -> None:
    """Bridge config.yaml agent.max_turns into HERMES_MAX_ITERATIONS (a global)."""
    config_path = home / 'config.yaml'
    if not config_path.exists():
        return
    try:
        from hermes_cli.config import _expand_env_vars, read_user_config_raw
        cfg = read_user_config_raw(config_path)
        cfg = _expand_env_vars(cfg)
        if not isinstance(cfg, dict):
            cfg = {}
        try:
            from hermes_cli import managed_scope
            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            pass
    except Exception:
        return
    agent_cfg = cfg.get('agent', {})
    if isinstance(agent_cfg, dict) and 'max_turns' in agent_cfg:
        os.environ['HERMES_MAX_ITERATIONS'] = str(agent_cfg['max_turns'])
    sessions_cfg = cfg.get('sessions', {})
    if isinstance(sessions_cfg, dict):
        if 'cjk_fts' in sessions_cfg:
            os.environ['HERMES_CJK_FTS'] = str(sessions_cfg['cjk_fts'])
        if 'search_slow_ms' in sessions_cfg:
            os.environ['HERMES_SEARCH_SLOW_MS'] = str(sessions_cfg['search_slow_ms'])

def _current_max_iterations() -> int:
    """Return the current per-turn iteration budget after runtime env refresh."""
    _reload_runtime_env_preserving_config_authority()
    try:
        return int(os.getenv('HERMES_MAX_ITERATIONS', '500'))
    except (TypeError, ValueError):
        return 500
from contextlib import contextmanager as _contextmanager
from gateway.config import PORT_BINDING_PLATFORM_VALUES as _PORT_BINDING_PLATFORM_VALUES, platform_binds_port as _platform_binds_port

class MultiplexConfigError(RuntimeError):
    """A profile multiplexer config is invalid.

    Distinct from a transient adapter-connect failure: a config error means the
    operator must fix config.yaml. Fatal configuration errors propagate to the
    startup guard instead of being treated as retryable adapter noise.
    """

class SecondaryPortBindingConfigError(MultiplexConfigError):
    """A secondary profile conflicts with the multiplexer's shared listener."""

@_contextmanager
def _profile_runtime_scope(profile_home: 'Path'):
    """Scope config/skills/memory AND credentials to a profile for one turn.

    Combines the two seams the multiplexer needs:
      1. ``set_hermes_home_override`` — redirects ``get_hermes_home()`` (config,
         skills, memory, SOUL, sessions) to the profile's home. Contextvar, so
         it propagates into the agent worker thread via ``copy_context()``.
      2. ``set_secret_scope`` — installs the profile's ``.env`` secrets as the
         authoritative credential source, so ``get_secret`` reads this profile's
         keys and never the process-global ``os.environ`` (which in a
         multiplexer may hold another profile's values).

    Only used on the multiplexed inbound path. Single-profile gateways never
    enter this scope, so their behavior is unchanged. Loading the profile's
    ``.env`` here does NOT mutate ``os.environ`` — ``build_profile_secret_scope``
    returns an isolated dict — which is what keeps subprocesses (MCP, kanban)
    from inheriting cross-profile secrets.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    home_token = set_hermes_home_override(str(profile_home))
    hydrate_profile_secret_sources(Path(profile_home))
    secret_token = set_secret_scope(build_profile_secret_scope(Path(profile_home)))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)

def load_gateway_config_for_runner() -> 'GatewayConfig':
    """Load gateway config for the process-level GatewayRunner.

    When ``gateway.multiplex_profiles`` is off, this is identical to
    ``load_gateway_config()`` (legacy single-profile path).

    When multiplexing is on, reload under the default/active profile's
    ``_profile_runtime_scope`` so platform tokens in that profile's ``.env``
    resolve through the secret scope — the same path secondary profiles use
    in ``_start_one_profile_adapters``. Without this, primary startup calls
    ``load_gateway_config()`` unscoped: ``_getenv`` falls through to
    ``os.environ``, which often has no ``TELEGRAM_BOT_TOKEN`` once the token
    lives only under ``profiles/<name>/.env`` (#64674).

    Single-profile gateways never set ``multiplex_profiles``, so they keep the
    unscoped load and are unaffected.
    """
    cfg = load_gateway_config()
    if not getattr(cfg, 'multiplex_profiles', False):
        return cfg
    try:
        home = get_hermes_home()
    except Exception:
        return cfg
    try:
        with _profile_runtime_scope(Path(home)):
            return load_gateway_config()
    except Exception:
        logger.debug('multiplex default-scope config reload failed; using unscoped load', exc_info=True)
        return cfg

def _platform_has_bot_credential(platform: 'Platform', platform_config: 'PlatformConfig') -> bool:
    """Return True when a token-authenticated platform has a usable bot credential.

    Platforms that do not use ``PlatformConfig.token`` always return True so we
    never skip them here (Signal session paths, port-binding HTTP adapters, etc.).
    """
    from gateway.config import PLATFORM_TOKEN_ENV_NAMES
    if platform not in PLATFORM_TOKEN_ENV_NAMES:
        return True
    token = getattr(platform_config, 'token', None) or ''
    if isinstance(token, str) and token.strip():
        return True
    api_key = getattr(platform_config, 'api_key', None) or ''
    if isinstance(api_key, str) and api_key.strip():
        return True
    return False
_DOCKER_VOLUME_SPEC_RE = re.compile('^(?P<host>.+):(?P<container>/[^:]+?)(?::(?P<options>[^:]+))?$')
_DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS = {'/output', '/outputs'}
from hermes_cli.config_defaults import DEFAULT_CONFIG as _DEFAULT_CONFIG
os.environ['HERMES_TURN_LEASE_TIMEOUT'] = str(_DEFAULT_CONFIG['agent']['gateway_turn_lease_timeout'])
_config_path = _hermes_home / 'config.yaml'
if _config_path.exists():
    try:
        from hermes_cli.config import _expand_env_vars, read_user_config_raw
        _cfg = read_user_config_raw(_config_path)
        _cfg = _expand_env_vars(_cfg)
        if not isinstance(_cfg, dict):
            _cfg = {}
        try:
            from hermes_cli import managed_scope
            _cfg = managed_scope.apply_managed_overlay(_cfg)
        except Exception:
            pass
        for _key, _val in _cfg.items():
            if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
                os.environ[_key] = str(_val)
        _terminal_cfg = _cfg.get('terminal', {})
        if _terminal_cfg and isinstance(_terminal_cfg, dict):
            _terminal_backend = str(_terminal_cfg.get('backend') or os.environ.get('TERMINAL_ENV') or '').strip().lower()
            _terminal_env_map = {'backend': 'TERMINAL_ENV', 'degraded_mode': 'TERMINAL_DEGRADED_MODE', 'cwd': 'TERMINAL_CWD', 'timeout': 'TERMINAL_TIMEOUT', 'home_mode': 'TERMINAL_HOME_MODE', 'lifetime_seconds': 'TERMINAL_LIFETIME_SECONDS', 'docker_image': 'TERMINAL_DOCKER_IMAGE', 'docker_forward_env': 'TERMINAL_DOCKER_FORWARD_ENV', 'singularity_image': 'TERMINAL_SINGULARITY_IMAGE', 'modal_image': 'TERMINAL_MODAL_IMAGE', 'daytona_image': 'TERMINAL_DAYTONA_IMAGE', 'vercel_runtime': 'TERMINAL_VERCEL_RUNTIME', 'ssh_host': 'TERMINAL_SSH_HOST', 'ssh_user': 'TERMINAL_SSH_USER', 'ssh_port': 'TERMINAL_SSH_PORT', 'ssh_key': 'TERMINAL_SSH_KEY', 'container_cpu': 'TERMINAL_CONTAINER_CPU', 'container_memory': 'TERMINAL_CONTAINER_MEMORY', 'container_disk': 'TERMINAL_CONTAINER_DISK', 'container_persistent': 'TERMINAL_CONTAINER_PERSISTENT', 'docker_volumes': 'TERMINAL_DOCKER_VOLUMES', 'docker_env': 'TERMINAL_DOCKER_ENV', 'docker_extra_args': 'TERMINAL_DOCKER_EXTRA_ARGS', 'docker_shm_size': 'TERMINAL_DOCKER_SHM_SIZE', 'docker_mount_cwd_to_workspace': 'TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE', 'docker_network': 'TERMINAL_DOCKER_NETWORK', 'docker_run_as_host_user': 'TERMINAL_DOCKER_RUN_AS_HOST_USER', 'docker_persist_across_processes': 'TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES', 'docker_orphan_reaper': 'TERMINAL_DOCKER_ORPHAN_REAPER', 'sandbox_dir': 'TERMINAL_SANDBOX_DIR', 'persistent_shell': 'TERMINAL_PERSISTENT_SHELL'}
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    _val = _terminal_cfg[_cfg_key]
                    if _cfg_key == 'cwd' and str(_val) in {'.', 'auto', 'cwd'}:
                        continue
                    if _cfg_key == 'cwd' and isinstance(_val, str):
                        from tools.terminal_tool import _is_ssh_remote_tilde_cwd
                        if not _is_ssh_remote_tilde_cwd(_terminal_backend, _val.strip()):
                            _val = os.path.expanduser(_val)
                    if isinstance(_val, (list, dict)):
                        os.environ[_env_var] = json.dumps(_val)
                    else:
                        os.environ[_env_var] = str(_val)
        _auxiliary_cfg = _cfg.get('auxiliary', {})
        if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
            _aux_bridged_keys = {'vision', 'web_extract', 'approval'}
            try:
                from hermes_cli.plugins import get_plugin_auxiliary_tasks
                for _entry in get_plugin_auxiliary_tasks():
                    _aux_bridged_keys.add(_entry['key'])
            except Exception:
                pass
            for _task_key in _aux_bridged_keys:
                _task_cfg = _auxiliary_cfg.get(_task_key, {})
                if not isinstance(_task_cfg, dict):
                    continue
                _prov = str(_task_cfg.get('provider', '')).strip()
                _model = str(_task_cfg.get('model', '')).strip()
                _base_url = str(_task_cfg.get('base_url', '')).strip()
                _api_key = str(_task_cfg.get('api_key', '')).strip()
                _upper = _task_key.upper()
                if _prov and _prov != 'auto':
                    os.environ[f'AUXILIARY_{_upper}_PROVIDER'] = _prov
                if _model:
                    os.environ[f'AUXILIARY_{_upper}_MODEL'] = _model
                if _base_url:
                    os.environ[f'AUXILIARY_{_upper}_BASE_URL'] = _base_url
                if _api_key:
                    os.environ[f'AUXILIARY_{_upper}_API_KEY'] = _api_key
        _agent_cfg = _cfg.get('agent', {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if 'max_turns' in _agent_cfg:
                os.environ['HERMES_MAX_ITERATIONS'] = str(_agent_cfg['max_turns'])
            if 'gateway_timeout' in _agent_cfg:
                os.environ['HERMES_AGENT_TIMEOUT'] = str(_agent_cfg['gateway_timeout'])
            if 'gateway_turn_lease_timeout' in _agent_cfg:
                os.environ['HERMES_TURN_LEASE_TIMEOUT'] = str(_agent_cfg['gateway_turn_lease_timeout'])
            if 'gateway_timeout_warning' in _agent_cfg:
                os.environ['HERMES_AGENT_TIMEOUT_WARNING'] = str(_agent_cfg['gateway_timeout_warning'])
            if 'gateway_notify_interval' in _agent_cfg:
                os.environ['HERMES_AGENT_NOTIFY_INTERVAL'] = str(_agent_cfg['gateway_notify_interval'])
            if 'session_stall_timeout' in _agent_cfg:
                os.environ['HERMES_SESSION_STALL_TIMEOUT'] = str(_agent_cfg['session_stall_timeout'])
            if 'restart_drain_timeout' in _agent_cfg:
                os.environ['HERMES_RESTART_DRAIN_TIMEOUT'] = str(_agent_cfg['restart_drain_timeout'])
            if 'gateway_auto_continue_freshness' in _agent_cfg:
                os.environ['HERMES_AUTO_CONTINUE_FRESHNESS'] = str(_agent_cfg['gateway_auto_continue_freshness'])
            if 'gateway_startup_restore_drain_timeout' in _agent_cfg:
                os.environ['HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT'] = str(_agent_cfg['gateway_startup_restore_drain_timeout'])
        _sessions_cfg = _cfg.get('sessions', {})
        if _sessions_cfg and isinstance(_sessions_cfg, dict):
            if 'cjk_fts' in _sessions_cfg:
                os.environ['HERMES_CJK_FTS'] = str(_sessions_cfg['cjk_fts'])
            if 'search_slow_ms' in _sessions_cfg:
                os.environ['HERMES_SEARCH_SLOW_MS'] = str(_sessions_cfg['search_slow_ms'])
        _display_cfg = _cfg.get('display', {})
        if _display_cfg and isinstance(_display_cfg, dict):
            if 'busy_input_mode' in _display_cfg:
                os.environ['HERMES_GATEWAY_BUSY_INPUT_MODE'] = str(_display_cfg['busy_input_mode'])
            if 'busy_text_mode' in _display_cfg:
                os.environ['HERMES_GATEWAY_BUSY_TEXT_MODE'] = str(_display_cfg['busy_text_mode'])
            if 'busy_ack_enabled' in _display_cfg:
                os.environ['HERMES_GATEWAY_BUSY_ACK_ENABLED'] = str(_display_cfg['busy_ack_enabled'])
            if 'busy_steer_ack_enabled' in _display_cfg and 'HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED' not in os.environ:
                os.environ['HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED'] = str(_display_cfg['busy_steer_ack_enabled'])
        _tz_cfg = _cfg.get('timezone', '')
        if _tz_cfg and isinstance(_tz_cfg, str):
            os.environ['HERMES_TIMEZONE'] = _tz_cfg.strip()
        _security_cfg = _cfg.get('security', {})
        if isinstance(_security_cfg, dict):
            _redact = _security_cfg.get('redact_secrets')
            if _redact is not None:
                os.environ['HERMES_REDACT_SECRETS'] = str(_redact).lower()
        _gateway_cfg = _cfg.get('gateway', {})
        if isinstance(_gateway_cfg, dict):
            _strict = _gateway_cfg.get('strict')
            if _strict is not None:
                os.environ['HERMES_MEDIA_DELIVERY_STRICT'] = '1' if _strict else '0'
            _allow_dirs = _gateway_cfg.get('media_delivery_allow_dirs')
            if _allow_dirs:
                if isinstance(_allow_dirs, str):
                    _allow_dirs_str = _allow_dirs
                elif isinstance(_allow_dirs, (list, tuple)):
                    _allow_dirs_str = os.pathsep.join((str(p) for p in _allow_dirs if p))
                else:
                    _allow_dirs_str = ''
                if _allow_dirs_str:
                    os.environ['HERMES_MEDIA_ALLOW_DIRS'] = _allow_dirs_str
            _trust_recent = _gateway_cfg.get('trust_recent_files')
            if _trust_recent is not None:
                os.environ['HERMES_MEDIA_TRUST_RECENT_FILES'] = '1' if _trust_recent else '0'
            _trust_recent_seconds = _gateway_cfg.get('trust_recent_files_seconds')
            if _trust_recent_seconds is not None:
                os.environ['HERMES_MEDIA_TRUST_RECENT_SECONDS'] = str(_trust_recent_seconds)
            if 'platform_connect_timeout' in _gateway_cfg and (not os.environ.get('HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT', '').strip()):
                os.environ['HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT'] = str(_gateway_cfg['platform_connect_timeout'])
    except Exception as _bridge_err:
        print(f'  Warning: config.yaml → env bridge failed: {type(_bridge_err).__name__}: {_bridge_err}', file=sys.stderr)
        print('  Gateway will fall back to .env values, which may not match your current config.yaml. Run `duck-agent doctor` to investigate.', file=sys.stderr)
try:
    from hermes_constants import apply_ipv4_preference
    _network_cfg = (_cfg if '_cfg' in dir() else {}).get('network', {})
    if isinstance(_network_cfg, dict) and _network_cfg.get('force_ipv4'):
        apply_ipv4_preference(force=True)
except Exception as _bootstrap_exc:
    print(f'  Warning: IPv4 preference application failed: {_bootstrap_exc}', file=sys.stderr)
try:
    from hermes_cli.config import print_config_warnings
    print_config_warnings()
except Exception as _bootstrap_exc:
    print(f'  Warning: config validation failed: {_bootstrap_exc}', file=sys.stderr)
try:
    from hermes_cli.config import warn_deprecated_cwd_env_vars
    warn_deprecated_cwd_env_vars()
except Exception as _bootstrap_exc:
    print(f'  Warning: deprecation check failed: {_bootstrap_exc}', file=sys.stderr)
os.environ['HERMES_QUIET'] = '1'
os.environ['HERMES_EXEC_ASK'] = '1'
from gateway.cwd_placeholder import CWD_PLACEHOLDERS, resolve_placeholder_terminal_cwd
_configured_cwd = os.environ.get('TERMINAL_CWD', '')
if not _configured_cwd or _configured_cwd in CWD_PLACEHOLDERS:
    _resolved_cwd = resolve_placeholder_terminal_cwd(configured_cwd=_configured_cwd, terminal_backend=os.environ.get('TERMINAL_ENV', ''), messaging_cwd=os.getenv('MESSAGING_CWD'), docker_mount_cwd_to_workspace=os.getenv('TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE', 'false').lower() in {'true', '1', 'yes'}, home_fallback=str(Path.home()))
    if _resolved_cwd is None:
        os.environ.pop('TERMINAL_CWD', None)
    else:
        os.environ['TERMINAL_CWD'] = _resolved_cwd
from gateway.config import ChannelOverride, Platform, _BUILTIN_PLATFORM_VALUES, GatewayConfig, PlatformConfig, _getenv, load_gateway_config
from gateway.session import AsyncSessionStore, SessionEntry, SessionStore, SessionSource, SessionContext, build_session_context, build_session_context_prompt, build_channel_continuity_note, build_session_key, is_shared_multi_user_session, neutralize_untrusted_inline_text
from gateway.delivery import DeliveryRouter, looks_like_telegram_private_chat_id, resolve_delivery_transport
from gateway.turn_lease import DEFAULT_LEASE_WAIT, SessionTurnLeaseRegistry, TurnLeaseTimeoutError
from gateway.session_state import SERVICE_TIER_UNSET as _SERVICE_TIER_UNSET, SessionState, legacy_dict_property, legacy_lease_token_property
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.slash_commands import GatewaySlashCommandsMixin
from gateway.turn_context import TurnContext
from gateway.platforms.base import BasePlatformAdapter, EphemeralReply, MessageEvent, MessageType, _prefix_within_utf16_limit, _reply_anchor_for_event, build_auto_tts_output_path, merge_pending_message_event, utf16_len
from gateway.shutdown_watchdog import DEFAULT_HEARTBEAT_INTERVAL_S, _arm_loop_floor_timer, arm_shutdown_watchdog, loop_heartbeat_forever, resolve_shutdown_watchdog_delay, start_loop_liveness_watchdog
from gateway.restart import DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT, GATEWAY_FATAL_CONFIG_EXIT_CODE, GATEWAY_SERVICE_RESTART_EXIT_CODE, parse_restart_after_turn_timeout, parse_restart_drain_timeout
from gateway.whatsapp_identity import canonical_whatsapp_identifier as _canonical_whatsapp_identifier, expand_whatsapp_aliases as _expand_whatsapp_auth_aliases, normalize_whatsapp_identifier as _normalize_whatsapp_identifier
logger = logging.getLogger(__name__)
_OWN_POLICY_OPEN_ENV = {Platform.WECOM: ('WECOM_DM_POLICY', 'WECOM_GROUP_POLICY', 'WECOM_ALLOW_ALL_USERS'), Platform.WEIXIN: ('WEIXIN_DM_POLICY', 'WEIXIN_GROUP_POLICY', 'WEIXIN_ALLOW_ALL_USERS'), Platform.YUANBAO: ('YUANBAO_DM_POLICY', 'YUANBAO_GROUP_POLICY', 'YUANBAO_ALLOW_ALL_USERS'), Platform.QQBOT: (None, None, 'QQ_ALLOW_ALL_USERS'), Platform.WHATSAPP: ('WHATSAPP_DM_POLICY', 'WHATSAPP_GROUP_POLICY', 'WHATSAPP_ALLOW_ALL_USERS')}

def _own_policy_open_startup_violation(config) -> Optional[str]:
    """Return a startup-abort reason when open policy lacks allow-all opt-in."""
    for platform, platform_config in getattr(config, 'platforms', {}).items():
        if not getattr(platform_config, 'enabled', False):
            continue
        open_env = _OWN_POLICY_OPEN_ENV.get(platform)
        if not open_env:
            continue
        dm_env, group_env, allow_all_env = open_env
        extra = getattr(platform_config, 'extra', None) or {}
        dm_policy = str(extra.get('dm_policy') or (_getenv(dm_env, 'pairing') if dm_env else 'pairing')).strip().lower()
        group_policy = str(extra.get('group_policy') or (_getenv(group_env, 'pairing') if group_env else 'pairing')).strip().lower()
        if dm_policy != 'open' and group_policy != 'open':
            continue
        gateway_allow_all = os.getenv('GATEWAY_ALLOW_ALL_USERS', '').lower() in {'true', '1', 'yes'}
        platform_opted_in = gateway_allow_all or (allow_all_env and _getenv(allow_all_env, '').lower() in {'true', '1', 'yes'})
        if platform_opted_in:
            continue
        return f'{platform.value}: open policy without allow-all opt-in'
    return None
_AGENT_PENDING_SENTINEL = object()
_CONVERSATION_SCOPED_STATE: tuple = ('_session_model_overrides', '_pending_one_turn_model_restores', '_session_reasoning_overrides', '_session_service_tier_overrides', '_pending_model_notes', '_last_resolved_model', '_queued_events', '_session_stall_notified', '_pending_turn_sidecar_notes')
_UNSET = object()

def _resolve_runtime_agent_kwargs() -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances.

    Provider is read from ``config.yaml`` ``model.provider`` (the single
    source of truth). ``resolve_runtime_provider()`` falls through to env
    var lookups internally for legacy compatibility, but the gateway does
    not consult environment variables for behavioral config — config.yaml
    is authoritative.

    If the primary provider fails with an authentication error, attempt to
    resolve credentials using the fallback provider chain from config.yaml
    before giving up.
    """
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error, _get_model_config
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    try:
        runtime = resolve_runtime_provider()
    except AuthError as auth_exc:
        if is_rate_limited_auth_error(auth_exc):
            logger.warning('Primary provider rate-limited (429): %s — trying fallback', auth_exc)
        else:
            logger.warning('Primary provider auth failed: %s — trying fallback', auth_exc)
        fb_config = _try_resolve_fallback_provider()
        if fb_config is not None:
            return fb_config
        raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc
    model_cfg = _get_model_config()
    max_tokens = None
    _env_mt = os.environ.get('HERMES_MAX_TOKENS')
    if _env_mt:
        try:
            max_tokens = int(_env_mt)
        except (ValueError, TypeError):
            max_tokens = None
    elif isinstance(model_cfg, dict):
        mt = model_cfg.get('max_tokens')
        if isinstance(mt, int):
            max_tokens = mt
    if max_tokens is None:
        _runtime_mot = runtime.get('max_output_tokens')
        if isinstance(_runtime_mot, int) and _runtime_mot > 0:
            max_tokens = _runtime_mot
    return {'api_key': runtime.get('api_key'), 'base_url': runtime.get('base_url'), 'provider': runtime.get('provider'), 'requested_provider': runtime.get('requested_provider'), 'api_mode': runtime.get('api_mode'), 'command': runtime.get('command'), 'args': list(runtime.get('args') or []), 'credential_pool': runtime.get('credential_pool'), 'max_tokens': max_tokens}

def _resolve_runtime_agent_kwargs_for_provider(provider: str) -> dict:
    """Resolve runtime credentials for a specific provider (e.g. from channel override)."""
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error
    try:
        runtime = resolve_runtime_provider(requested=provider)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc
    return {'api_key': runtime.get('api_key'), 'base_url': runtime.get('base_url'), 'provider': runtime.get('provider'), 'requested_provider': runtime.get('requested_provider'), 'api_mode': runtime.get('api_mode'), 'command': runtime.get('command'), 'args': list(runtime.get('args') or []), 'credential_pool': runtime.get('credential_pool')}

def _credential_pool_for_provider(provider: Optional[str]):
    """Return the live credential pool for a provider id (e.g. ``custom:hyper``)."""
    if not provider or not str(provider).strip():
        return None
    try:
        return _resolve_runtime_agent_kwargs_for_provider(str(provider).strip()).get('credential_pool')
    except Exception:
        logger.debug('Failed to resolve credential pool for provider=%s', provider, exc_info=True)
        return None

def _try_resolve_fallback_provider() -> dict | None:
    """Attempt to resolve credentials from the fallback_model/fallback_providers config."""
    from hermes_cli.runtime_provider import resolve_runtime_provider
    try:
        cfg = _load_gateway_runtime_config()
        fb_list = get_fallback_chain(cfg)
        if not fb_list:
            return None
        for entry in fb_list:
            try:
                from hermes_cli.fallback_config import resolve_entry_api_key
                runtime = resolve_runtime_provider(requested=entry.get('provider'), explicit_base_url=entry.get('base_url'), explicit_api_key=resolve_entry_api_key(entry))
                logger.info('Fallback provider resolved: %s model=%s', entry.get('provider') or runtime.get('provider'), entry.get('model'))
                return {'api_key': runtime.get('api_key'), 'base_url': runtime.get('base_url'), 'provider': runtime.get('provider'), 'requested_provider': runtime.get('requested_provider'), 'api_mode': runtime.get('api_mode'), 'command': runtime.get('command'), 'args': list(runtime.get('args') or []), 'credential_pool': runtime.get('credential_pool'), 'model': entry.get('model')}
            except Exception as fb_exc:
                logger.debug('Fallback entry %s failed: %s', entry.get('provider'), fb_exc)
                continue
    except Exception:
        pass
    return None

def _event_media_type_at(event, index: int) -> str:
    """Return the per-attachment MIME for the attachment at *index*.

    Empty string when the platform didn't populate a per-file MIME for
    that slot (some adapters only set a message-level type).
    """
    media_types = getattr(event, 'media_types', None) or []
    return media_types[index] if index < len(media_types) else ''

def _event_media_is_image(event, index: int) -> bool:
    """True if the attachment at *index* is an image.

    Trust the per-attachment MIME when present. Only fall back to the
    message-level ``PHOTO`` type when this attachment's MIME is unknown --
    otherwise a document (or any non-image) uploaded alongside an image in
    the same message gets mis-routed as an image, base64'd into a vision
    content part, and the provider 400s ("Could not process image").
    """
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith('image/')
    return getattr(event, 'message_type', None) == MessageType.PHOTO

def _event_media_is_audio(event, index: int) -> bool:
    """True if the attachment at *index* is audio (per-attachment MIME first)."""
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith('audio/')
    return getattr(event, 'message_type', None) in {MessageType.VOICE, MessageType.AUDIO}

def _event_media_is_stt_input(event, index: int) -> bool:
    """True when an audio attachment should enter the automatic STT pipeline."""
    message_type = getattr(event, 'message_type', None)
    if message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
        return False
    return message_type == MessageType.VOICE or _event_media_type_at(event, index).startswith('audio/')

def _event_media_is_video(event, index: int) -> bool:
    """True if the attachment at *index* is video (per-attachment MIME first)."""
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith('video/')
    return getattr(event, 'message_type', None) == MessageType.VIDEO

def _build_media_placeholder(event) -> str:
    """Build a text placeholder for media-only events so they aren't dropped.

    When a photo/document is queued during active processing and later
    dequeued, only .text is extracted.  If the event has no caption,
    the media would be silently lost.  This builds a placeholder that
    the vision enrichment pipeline will replace with a real description.
    """
    parts = []
    media_urls = getattr(event, 'media_urls', None) or []
    for i, url in enumerate(media_urls):
        if _event_media_is_image(event, i):
            parts.append(f'[User sent an image: {url}]')
        elif _event_media_is_audio(event, i):
            parts.append(f'[User sent audio: {url}]')
        elif _event_media_is_video(event, i):
            parts.append(f'[User sent a video: {url}]')
        else:
            parts.append(f'[User sent a file: {url}]')
    return '\n'.join(parts)

def _build_document_context_note(display_name: str, agent_path: str, mtype: str) -> str:
    """Context note prepended to a user turn when they attach a document.

    Text documents (``text/*``) have their content inlined upstream by the
    platform adapter, so the note just confirms that and records the path.

    Binary documents (PDF, DOCX, XLSX, …) cannot be inlined as text. The note
    must tell the agent to *extract* the text itself before answering — earlier
    wording ("Ask the user what they'd like you to do with it") steered the
    model into punting back to the user, which is why attached PDFs/DOCX looked
    "unreadable" to the agent even though it has the tools to read them.
    """
    if mtype.startswith('text/'):
        return f"[The user sent a text document: '{display_name}'. Its content has been included below. The file is also saved at: {agent_path}]"
    return f"[The user sent a document: '{display_name}'. It is saved at: {agent_path}. Its text is not inlined here (it's a binary format such as PDF or DOCX). To read it, extract the document's text yourself — for example with the terminal tool or the ocr-and-documents skill — before answering, instead of asking the user to paste the contents.]"

def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'

async def _probe_audio_duration(path: str) -> Optional[str]:
    """Best-effort duration probe. Returns formatted MM:SS / HH:MM:SS, or None on failure."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.wav':
        try:

            def _wav_duration() -> float:
                import wave
                with wave.open(path, 'rb') as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    return frames / float(rate)
            secs = await asyncio.to_thread(_wav_duration)
            return _format_duration(secs)
        except Exception:
            pass
    if ext in ('.ogg', '.opus', '.oga'):
        try:

            def _ogg_duration() -> float:
                from mutagen.oggopus import OggOpus
                return float(OggOpus(path).info.length)
            secs = await asyncio.to_thread(_ogg_duration)
            return _format_duration(secs)
        except Exception:
            pass
    try:
        proc = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            return _format_duration(float(stdout.decode().strip()))
    except Exception:
        pass
    return None

def _dequeue_pending_event(adapter, session_key: str) -> MessageEvent | None:
    """Consume and return the full pending event for a session.

    Queued follow-ups must preserve their media metadata so they can re-enter
    the normal image/STT/document preprocessing path instead of being reduced
    to a placeholder string.
    """
    return adapter.get_pending_message(session_key)
_INTERRUPT_REASON_STOP = 'Stop requested'
_INTERRUPT_REASON_RESET = 'Session reset requested'
_INTERRUPT_REASON_TIMEOUT = 'Execution timed out (inactivity)'
_INTERRUPT_REASON_SSE_DISCONNECT = 'SSE client disconnected'
_INTERRUPT_REASON_GATEWAY_SHUTDOWN = 'Gateway shutting down'
_INTERRUPT_REASON_GATEWAY_RESTART = 'Gateway restarting'

def _reap_gateway_turn_processes(task_id: str, process_baseline, *, source: str, is_still_current: Optional[Callable[[], bool]]=None) -> int:
    """Reap only background processes created by one abandoned turn.

    ``task_id`` is session-scoped (task_id == session_id), not turn-scoped,
    so a *replacement* turn on the same session can start and spawn its own
    legitimate process while this reap is still in flight. ``is_still_current``
    — a closure over the run_generation captured when the reaping turn began
    or was interrupted — lets the caller detect that a newer turn has since
    claimed the session and bail out instead of killing that newer turn's
    process. The newer turn snapshots its own baseline independently, so
    skipping here does not leave anything permanently unreaped.
    """
    if not task_id:
        return 0
    if is_still_current is not None:
        try:
            if not is_still_current():
                logger.debug('Skipping reap for turn %s (%s): a newer turn already claimed this session; it owns its own baseline.', task_id, source)
                return 0
        except Exception:
            logger.debug('is_still_current check failed for turn %s (%s); reaping anyway', task_id, source, exc_info=True)
    from tools.process_registry import process_registry
    try:
        killed = process_registry.kill_started_since(task_id, process_baseline, source=source)
    except Exception:
        logger.warning('Failed to reap background processes for turn %s (%s)', task_id, source, exc_info=True)
        return 0
    if killed:
        logger.warning('Reaped %d background process(es) created by abandoned turn %s (%s)', killed, task_id, source)
    return killed

def _abandon_timed_out_gateway_turn(*, agent_holder, task_id: str, process_baseline, worker_done: threading.Event, timeout_fired: threading.Event, cleanup_lock: threading.Lock, is_still_current: Optional[Callable[[], bool]]=None) -> bool:
    """Interrupt one timed-out turn and reap only processes it created."""
    with cleanup_lock:
        if worker_done.is_set() or timeout_fired.is_set():
            return False
        timeout_fired.set()
    agent = agent_holder[0] if agent_holder else None
    if agent is not None:
        try:
            request_hard_interrupt(agent, _INTERRUPT_REASON_TIMEOUT)
        except Exception:
            logger.debug('Timed-out agent interrupt failed', exc_info=True)
    try:
        _reap_gateway_turn_processes(task_id, process_baseline, source='gateway_turn_timeout', is_still_current=is_still_current)
    except Exception:
        logger.warning('Failed to reap background processes for timed-out turn %s', task_id, exc_info=True)
    return True

def _watch_gateway_turn_inactivity(*, agent_holder, task_id: str, process_baseline, timeout: float, worker_done: threading.Event, timeout_fired: threading.Event, cleanup_lock: threading.Lock, poll_interval: float=5.0, is_still_current: Optional[Callable[[], bool]]=None) -> None:
    """Thread watchdog that remains runnable when gateway asyncio is starved."""
    while not worker_done.wait(max(0.01, poll_interval)):
        agent = agent_holder[0] if agent_holder else None
        if agent is None or not hasattr(agent, 'get_activity_summary'):
            continue
        try:
            idle_seconds = float(agent.get_activity_summary().get('seconds_since_activity', 0.0))
        except Exception:
            continue
        if idle_seconds < timeout:
            continue
        _abandon_timed_out_gateway_turn(agent_holder=agent_holder, task_id=task_id, process_baseline=process_baseline, worker_done=worker_done, timeout_fired=timeout_fired, cleanup_lock=cleanup_lock, is_still_current=is_still_current)
        return
_CONTROL_INTERRUPT_MESSAGES = frozenset({_INTERRUPT_REASON_STOP.lower(), _INTERRUPT_REASON_RESET.lower(), _INTERRUPT_REASON_TIMEOUT.lower(), _INTERRUPT_REASON_SSE_DISCONNECT.lower(), _INTERRUPT_REASON_GATEWAY_SHUTDOWN.lower(), _INTERRUPT_REASON_GATEWAY_RESTART.lower()})

def _is_control_interrupt_message(message: Optional[str]) -> bool:
    """Return True when an interrupt message is internal control flow."""
    if not message:
        return False
    normalized = ' '.join(str(message).strip().split()).lower()
    return normalized in _CONTROL_INTERRUPT_MESSAGES

def _skill_slug_from_frontmatter(skill_md: Path) -> tuple[str | None, str | None]:
    """Derive the /command slug and declared frontmatter name from a SKILL.md.

    Matches the exact normalization used by
    :func:`agent.skill_commands.scan_skill_commands` so the slug here is the
    same string a user types after the leading ``/`` (e.g. a skill with
    frontmatter ``name: Stable Diffusion Image Generation`` resolves to
    ``stable-diffusion-image-generation`` — NOT the parent directory name,
    which is commonly shorter/different, e.g. ``stable-diffusion``).

    Using the directory name silently broke :func:`_check_unavailable_skill`
    for every skill whose directory name drifted from its frontmatter name
    (19 such skills on a standard install as of 2026-05), causing a generic
    "unknown command" response where a "disabled — enable with …" or
    "not installed — install with …" hint was expected.

    Returns ``(slug, declared_name)`` or ``(None, None)`` when the file
    can't be read or lacks a ``name:`` in its frontmatter.
    """
    try:
        content = skill_md.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return (None, None)
    content = content.lstrip('\ufeff')
    if not content.startswith('---'):
        return (None, None)
    end = content.find('\n---', 3)
    if end < 0:
        return (None, None)
    declared_name: str | None = None
    for line in content[3:end].splitlines():
        line = line.strip()
        if line.startswith('name:'):
            raw = line.split(':', 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and (raw[0] in {'"', "'"}):
                raw = raw[1:-1]
            declared_name = raw.strip()
            break
    if not declared_name:
        return (None, None)
    slug = declared_name.lower().replace(' ', '-').replace('_', '-')
    import re as _re
    slug = _re.sub('[^a-z0-9-]', '', slug)
    slug = _re.sub('-{2,}', '-', slug).strip('-')
    if not slug:
        return (None, declared_name)
    return (slug, declared_name)

def _check_unavailable_skill(command_name: str) -> str | None:
    """Check if a command matches a known-but-inactive skill.

    Returns a helpful message if the skill exists but is disabled or only
    available as an optional install. Returns None if no match found.

    The slug for each on-disk skill is derived from its frontmatter ``name:``
    (via :func:`_skill_slug_from_frontmatter`), NOT from its containing
    directory name — because the two can differ (e.g. directory
    ``stable-diffusion`` + frontmatter ``Stable Diffusion Image Generation``
    yields slug ``stable-diffusion-image-generation``). Matching on
    directory name would miss that slug entirely and fall through to the
    generic "unknown command" path.
    """
    normalized = command_name.lower().replace('_', '-')
    try:
        from tools.skills_tool import _get_disabled_skill_names
        from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
        disabled = _get_disabled_skill_names()
        for skills_dir in get_all_skills_dirs():
            if not skills_dir.exists():
                continue
            for skill_md in skills_dir.rglob('SKILL.md'):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, declared_name = _skill_slug_from_frontmatter(skill_md)
                if not slug or not declared_name:
                    continue
                if slug == normalized and declared_name in disabled:
                    return f'The **{command_name}** skill is installed but disabled.\nEnable it with: `duck-agent skills config`'
        from hermes_constants import get_optional_skills_dir
        repo_root = Path(__file__).resolve().parent.parent
        optional_dir = get_optional_skills_dir(repo_root / 'optional-skills')
        if optional_dir.exists():
            for skill_md in optional_dir.rglob('SKILL.md'):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, _declared = _skill_slug_from_frontmatter(skill_md)
                if not slug:
                    continue
                if slug == normalized:
                    rel = skill_md.parent.relative_to(optional_dir)
                    parts = list(rel.parts)
                    install_path = f"official/{'/'.join(parts)}"
                    return f'The **{command_name}** skill is available but not installed.\nInstall it with: `duck-agent skills install {install_path}`'
    except Exception:
        pass
    return None

def _platform_config_key(platform: 'Platform') -> str:
    """Map a Platform enum to its config.yaml key (LOCAL→"cli", rest→enum value)."""
    return 'cli' if platform == Platform.LOCAL else platform.value

def _teams_pipeline_plugin_enabled() -> bool:
    """Return True when the standalone Teams pipeline plugin is enabled."""
    config = _load_gateway_config()
    enabled = cfg_get(config, 'plugins', 'enabled', default=[])
    if not isinstance(enabled, list):
        return False
    return 'teams_pipeline' in enabled or 'teams-pipeline' in enabled

def _gateway_config_home() -> Path:
    """Return the Duck Agent home that gateway config reads should use."""
    override = get_hermes_home_override()
    if override:
        return Path(override)
    return _hermes_home

def _load_gateway_config() -> dict:
    """Load and parse ~/.duck-agent/config.yaml, returning {} on any error.

    Uses the module-level ``_hermes_home`` (so tests that monkeypatch it
    still see their fixture) and shares the mtime-keyed raw-yaml cache
    from ``hermes_cli.config.read_raw_config`` when the paths match.

    Managed scope is overlaid on the result (via the shared helper) so the
    gateway honors administrator-pinned values — neither read_raw_config nor a
    direct yaml.safe_load carries the managed merge on its own. Fail-open.
    """
    config_home = _gateway_config_home()
    config_path = config_home / 'config.yaml'
    raw: dict = {}
    used_canonical = False
    try:
        from hermes_cli.config import get_config_path, read_raw_config
        if config_path == get_config_path():
            raw = read_raw_config()
            used_canonical = True
    except Exception:
        pass
    if not used_canonical:
        try:
            if config_path.exists():
                import yaml
                with open(config_path, 'r', encoding='utf-8') as f:
                    raw = yaml.safe_load(f) or {}
        except Exception:
            logger.debug('Could not load gateway config from %s', config_path)
            raw = {}
    try:
        from hermes_cli import managed_scope
        raw = managed_scope.apply_managed_overlay(raw if isinstance(raw, dict) else {})
    except Exception:
        pass
    if not isinstance(raw, dict):
        return {}
    try:
        from hermes_cli.config import _normalize_root_model_keys
        raw = _normalize_root_model_keys(raw)
    except Exception:
        pass
    return raw

def _checkpoint_agent_kwargs(config: dict | None) -> dict:
    """Translate gateway checkpoint config into ``AIAgent`` constructor args.

    The gateway reads raw YAML instead of ``load_config()``, so checkpoint
    defaults must be supplied here.  Keep legacy ``checkpoints: true`` configs
    working while giving every gateway-created agent the same limits.
    """
    cp_cfg = config.get('checkpoints', {}) if isinstance(config, dict) else {}
    if isinstance(cp_cfg, bool):
        cp_cfg = {'enabled': cp_cfg}
    elif not isinstance(cp_cfg, dict):
        cp_cfg = {}
    from hermes_cli.config import DEFAULT_CONFIG
    defaults = DEFAULT_CONFIG['checkpoints']
    return {'checkpoints_enabled': cp_cfg.get('enabled', defaults['enabled']), 'checkpoint_max_snapshots': cp_cfg.get('max_snapshots', defaults['max_snapshots']), 'checkpoint_max_total_size_mb': cp_cfg.get('max_total_size_mb', defaults['max_total_size_mb']), 'checkpoint_max_file_size_mb': cp_cfg.get('max_file_size_mb', defaults['max_file_size_mb'])}

def _load_gateway_runtime_config() -> dict:
    """Load gateway config for runtime reads, expanding supported ``${VAR}`` refs.

    Runtime helpers should honor the same env-template expansion documented for
    ``config.yaml`` while still respecting tests that monkeypatch
    ``gateway.run._hermes_home``. Build on ``_load_gateway_config()`` rather
    than calling the canonical loader directly so both behaviors stay aligned.

    Expansion failures are intentionally NOT swallowed — silently returning
    the unexpanded dict would mask the very bug this helper exists to fix.
    """
    cfg = _load_gateway_config()
    if not isinstance(cfg, dict) or not cfg:
        return {}
    from hermes_cli.config import _expand_env_vars
    expanded = _expand_env_vars(cfg)
    return expanded if isinstance(expanded, dict) else {}

def _resolve_gateway_model(config: dict | None=None) -> str:
    """Read model from config.yaml — single source of truth.

    Without this, temporary AIAgent instances (e.g. /compress) fall
    back to the hardcoded default which fails when the active provider is
    openai-codex.
    """
    cfg = config if config is not None else _load_gateway_config()
    model_cfg = cfg.get('model', {})
    if isinstance(model_cfg, str):
        return model_cfg
    elif isinstance(model_cfg, dict):
        return model_cfg.get('default') or model_cfg.get('model') or ''
    return ''

def _channel_override_lookup_keys(chat_id: str, *, thread_id: Optional[str]=None, parent_id: Optional[str]=None) -> list[str]:
    """Ordered, de-duplicated keys for ``channel_overrides`` lookup.

    Matches ``resolve_channel_prompt`` semantics: exact thread/channel id first,
    then parent channel/forum id (Discord threads inherit parent overrides).
    """
    keys: list[str] = []
    seen: set[str] = set()
    for key in (chat_id, thread_id, parent_id):
        if not key:
            continue
        sk = str(key)
        if sk in seen:
            continue
        seen.add(sk)
        keys.append(sk)
    return keys

def _get_channel_override(config: GatewayConfig, platform: Platform, chat_id: str, *, thread_id: Optional[str]=None, parent_id: Optional[str]=None) -> Optional[ChannelOverride]:
    """Return per-channel override for this platform/chat_id, or None.

    Looks up ``channel_overrides`` by ``chat_id``, then ``thread_id``, then
    ``parent_id`` (forum threads / child channels inherit the parent entry).
    """
    platforms = getattr(config, 'platforms', None)
    if not platforms:
        return None
    platform_config = platforms.get(platform)
    if not platform_config or not platform_config.channel_overrides:
        return None
    overrides = platform_config.channel_overrides
    for key in _channel_override_lookup_keys(chat_id, thread_id=thread_id, parent_id=parent_id):
        ov = overrides.get(key)
        if ov is not None:
            return ov
    return None

def _resolve_hermes_bin() -> Optional[list[str]]:
    """Resolve the Duck Agent update command as argv parts.

    Tries in order:
    1. ``shutil.which("duck-agent")`` — standard PATH lookup
    2. ``sys.executable -m hermes_cli.main`` — fallback when Duck Agent is running
       from a venv/module invocation and the ``duck-agent`` shim is not on PATH

    Returns argv parts ready for quoting/joining, or ``None`` if neither works.
    """
    import shutil
    hermes_bin = shutil.which('duck-agent')
    if hermes_bin:
        return [hermes_bin]
    try:
        import importlib.util
        if importlib.util.find_spec('hermes_cli') is not None:
            return [sys.executable, '-m', 'hermes_cli.main']
    except Exception:
        pass
    return None

def _parse_session_key(session_key: str) -> 'dict | None':
    """Parse a session key into its component parts.

    Session keys follow the format
    ``agent:main:{platform}:{chat_type}:{chat_id}[:{extra}...]``.
    Returns a dict with ``platform``, ``chat_type``, ``chat_id``, and
    optionally ``thread_id`` keys, or None if the key doesn't match.

    The 6th element is only returned as ``thread_id`` for chat types where
    it is unambiguous (``dm`` and ``thread``).  For group/channel sessions
    the suffix may be a user_id (per-user isolation) rather than a
    thread_id, so we leave ``thread_id`` out to avoid mis-routing.
    """
    parts = session_key.split(':')
    if len(parts) >= 5 and parts[0] == 'agent' and (parts[1] == 'main'):
        result = {'platform': parts[2], 'chat_type': parts[3], 'chat_id': parts[4]}
        if len(parts) > 5 and parts[3] in {'dm', 'thread'}:
            result['thread_id'] = parts[5]
        return result
    return None

def _format_gateway_process_notification(evt: dict) -> 'str | None':
    """Format a watch pattern event from completion_queue into a [IMPORTANT:] message."""
    evt_type = evt.get('type', 'completion')
    _sid = evt.get('session_id', 'unknown')
    _cmd = evt.get('command', 'unknown')
    if evt_type == 'watch_disabled':
        return f"[IMPORTANT: {evt.get('message', '')}]"
    if evt_type == 'watch_match':
        _pat = evt.get('pattern', '?')
        _out = evt.get('output', '')
        _sup = evt.get('suppressed', 0)
        text = f'[IMPORTANT: Background process {_sid} matched watch pattern "{_pat}".\nCommand: {_cmd}\nMatched output:\n{_out}'
        if _sup:
            text += f'\n({_sup} earlier matches were suppressed by rate limit)'
        text += ']'
        return text
    if evt_type == 'async_delegation':
        from tools.process_registry import format_process_notification
        return format_process_notification(evt)
    return None

def _drain_gateway_watch_events(completion_queue) -> 'list[dict]':
    """Drain gateway-owned watch events without spinning on requeued events.

    Watch events are handled by the post-turn gateway drain. Process
    completions are owned by their per-process watcher task, and async
    delegation completions are owned by ``_async_delegation_watcher``.
    Requeueing async events inside ``while not queue.empty()`` would make the
    loop non-terminating, so detach the current batch first, then requeue any
    events this drain does not own after the queue is empty.
    """
    watch_events: list[dict] = []
    requeue: list[dict] = []
    while not completion_queue.empty():
        try:
            evt = completion_queue.get_nowait()
        except Exception:
            break
        evt_type = evt.get('type', 'completion')
        if evt_type in {'watch_match', 'watch_disabled'}:
            watch_events.append(evt)
        elif evt_type == 'async_delegation':
            requeue.append(evt)
    for evt in requeue:
        completion_queue.put(evt)
    return watch_events
import weakref as _weakref
_gateway_runner_ref: _weakref.ref = lambda: None

def _normalize_empty_agent_response(agent_result: dict, response: str, *, history_len: int=0) -> str:
    """Normalize empty/None agent responses into user-facing messages.

    Consolidates the existing ``failed`` handler and adds a catch-all for
    the case where the agent did work (api_calls > 0) but returned no text.
    Fix for #18765.

    Also surfaces a retry hint when the agent never ran at all
    (api_calls == 0) for a non-interrupted, non-failed turn -- this is the
    silent-drop pattern observed after ``/stop`` where the next user
    message hits a stale generation token and returns an empty result,
    leaving the platform with nothing to send. (#31884)
    """
    if response:
        return response
    if agent_result.get('failed'):
        error_detail = agent_result.get('error', 'unknown error')
        error_str = str(error_detail).lower()
        is_context_failure = any((p in error_str for p in ('context', 'token', 'too large', 'too long', 'exceed', 'payload'))) or ('400' in error_str and history_len > 50)
        if is_context_failure:
            return "⚠️ Session too large for the model's context window.\nUse /compact to compress the conversation, or /reset to start fresh."
        return f'The request failed: {str(error_detail)[:300]}\nTry again or use /reset to start a fresh session.'
    api_calls = int(agent_result.get('api_calls', 0) or 0)
    if agent_result.get('interrupted'):
        if api_calls == 0:
            return '⚠️ Your message was interrupted before processing started (likely by a recent /stop). Please send it again.'
        return response
    if api_calls > 0:
        if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
            return ''
        if agent_result.get('partial'):
            err = agent_result.get('error', 'processing incomplete')
            return f'⚠️ Processing stopped: {str(err)[:200]}. Try again.'
        return '⚠️ Processing completed but no response was generated. This may be a transient error — try sending your message again.'
    if api_calls == 0 and (not agent_result.get('interrupted')) and (not agent_result.get('failed')) and (not agent_result.get('partial')):
        return "⚠️ Your message wasn't processed (the previous turn was still being cleaned up). Please send it again."
    return response

def _is_gateway_hidden_reasoning_incomplete_turn(agent_result: dict) -> bool:
    """Detect retry-exhausted turns with hidden reasoning but no visible answer.

    The conversation loop returns the retry-exhaustion sentinel as BOTH
    ``final_response`` and ``error`` ("Codex response remained incomplete
    after 3 continuation attempts"), so ``final_response`` being non-empty
    does not mean the model produced a visible answer. Treat the turn as
    hidden when the error sentinel is present and ``final_response`` is
    either empty or merely echoes that sentinel — any genuinely different
    final text means the model DID answer and must be delivered.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get('failed') or agent_result.get('interrupted'):
        return False
    if not agent_result.get('partial'):
        return False
    error_text = str(agent_result.get('error', '') or '').strip()
    if 'remained incomplete after' not in error_text.lower():
        return False
    final_response = str(agent_result.get('final_response') or '').strip()
    return not final_response or final_response == error_text

def _should_clear_resume_pending_after_turn(agent_result: dict) -> bool:
    """Return True only when a gateway turn really completed successfully.

    Restart recovery uses ``resume_pending`` as a durable marker for sessions
    interrupted during gateway drain.  A soft interrupt can still bubble out as
    a syntactically normal agent result with an empty final response; clearing
    the marker in that case loses the recovery signal and startup auto-resume
    has nothing to schedule.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get('interrupted'):
        return False
    if agent_result.get('failed') or agent_result.get('partial') or agent_result.get('error'):
        return False
    if agent_result.get('completed') is False:
        return False
    return True

def _preserve_queued_followup_history_offset(current_result: dict, followup_result: dict) -> dict:
    """Carry the outer history offset through queued follow-up drains.

    ``_process_message_background()`` persists transcript rows only once, after the
    entire in-band queued-follow-up chain returns.  Each recursive ``_run_agent()``
    call advances ``history_offset`` to the history it received, so without
    correction the outermost persistence step sees only the *last* queued turn as
    "new" and silently drops earlier turns from the same drain chain.

    Preserve the earliest (outermost) history offset so the final transcript slice
    still includes every queued turn that ran during the chain.
    """
    if not isinstance(followup_result, dict):
        return followup_result
    if not isinstance(current_result, dict):
        return followup_result
    current_offset = current_result.get('history_offset')
    followup_offset = followup_result.get('history_offset')
    if not isinstance(current_offset, int):
        return followup_result
    if isinstance(followup_offset, int) and followup_offset <= current_offset:
        return followup_result
    merged = dict(followup_result)
    merged['history_offset'] = current_offset
    return merged

async def _dispose_unused_adapter(adapter: 'BasePlatformAdapter | None') -> None:
    """Best-effort dispose for an adapter that never made it onto ``self.adapters``.

    The reconnect watcher in ``GatewayRunner._platform_reconnect_watcher``
    constructs a fresh adapter on every retry attempt. When the connect
    call fails — for any of the three reasons (non-retryable error,
    retryable error, exception during connect) — the adapter is dropped
    without ever being installed, so nothing else will call its
    ``disconnect()``. Any resources the adapter opened in ``__init__``
    (e.g. ``APIServerAdapter`` opens a SQLite ``ResponseStore`` that
    holds 2 fds — the db file and its WAL sidecar) stay open until
    garbage collection sweeps the unreachable object, which Python's
    cyclic GC does not do promptly for asyncio-bound objects with
    native handles. The cumulative leak is 2 fds × every retry at the
    300s backoff cap ≈ 12 fds/hour, and the default 2560-fd ulimit
    is exhausted in ~12h of continuous failure, after which every
    open() call on the gateway raises ``OSError: [Errno 24] Too many
    open files`` and the gateway becomes a zombie (#37011).

    This helper centralises the dispose-with-suppression so the three
    failure paths in the reconnect watcher can all call it without
    each one having to know that ``disconnect()`` may itself raise
    on a half-constructed adapter.

    ``adapter`` may be ``None``: the reconnect watcher initialises
    ``adapter = None`` before the ``try`` so the ``except Exception``
    arm can dispose a half-constructed object, and also early-returns
    here when ``_create_adapter()`` returned ``None``.
    """
    if adapter is None:
        return
    try:
        await adapter.disconnect()
    except Exception:
        logger.debug('Adapter dispose raised on unowned adapter %r', getattr(adapter, 'name', type(adapter).__name__), exc_info=True)
_RECONNECT_BACKOFF_CAP = 300

def _reconnect_backoff(attempt: int) -> int:
    """Exponential reconnect backoff: 30s, 60s, 120s, ... capped at 5 min."""
    return min(30 * 2 ** (attempt - 1), _RECONNECT_BACKOFF_CAP)

class TurnRunner:
    """Per-turn collaborator carrying the tool-progress callbacks that used to
    be nested closures inside ``GatewayRunner._run_agent_inner``.

    The bodies are byte-identical to the original closures modulo
    ``local_name`` -> ``ctx.field`` rewrites (closed-over locals now travel on
    the shared :class:`gateway.turn_context.TurnContext`) and ``self`` ->
    ``self._runner`` (the owning :class:`GatewayRunner`). Module-global
    references (logger, cfg_get, BasePlatformAdapter, ...) resolve in this
    same module exactly as before.
    """

    def __init__(self, runner: 'GatewayRunner', ctx: TurnContext) -> None:
        self._runner = runner
        self._ctx = ctx

    def progress_callback(self, event_type: str, tool_name: str=None, preview: str=None, args: dict=None, **kwargs):
        """Callback invoked by agent on tool lifecycle events."""
        ctx = self._ctx
        if ctx._live_status_adapter is not None and ctx._live_status_mode != 'off' and (tool_name != '_thinking'):
            try:
                if event_type == 'tool.started' and tool_name and ctx._run_still_current():
                    from agent.display import build_status_phrase
                    _phrase = build_status_phrase(tool_name, args if ctx._live_status_mode == 'full' else None)
                    ctx._live_status_adapter.set_status_text(ctx.source.chat_id, _phrase)
                elif event_type == 'tool.completed':
                    ctx._live_status_adapter.set_status_text(ctx.source.chat_id, None)
            except Exception as _ls_err:
                logger.debug('live status update failed: %s', _ls_err)
        if ctx.log_queue is not None:
            if event_type == 'tool.started' and tool_name and (tool_name != '_thinking'):
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                preview_str = f' "{preview}"' if preview else ''
                ctx.log_queue.put(f'{ts}  {tool_name}:{preview_str}'.rstrip())
            if not ctx.progress_queue:
                return
        if not ctx.progress_queue or not ctx._run_still_current():
            return
        if event_type == 'tool.completed' and (not ctx.long_tool_hint_fired[0]):
            try:
                duration = kwargs.get('duration') or 0
                if duration >= ctx._LONG_TOOL_THRESHOLD_S and ctx.progress_mode == 'all':
                    from agent.onboarding import TOOL_PROGRESS_FLAG, is_seen, mark_seen, tool_progress_hint_gateway
                    _cfg = _load_gateway_config()
                    gate_on = is_truthy_value(cfg_get(_cfg, 'display', 'tool_progress_command'), default=False)
                    if gate_on and (not is_seen(_cfg, TOOL_PROGRESS_FLAG)):
                        ctx.long_tool_hint_fired[0] = True
                        ctx.progress_queue.put(tool_progress_hint_gateway())
                        mark_seen(_hermes_home / 'config.yaml', TOOL_PROGRESS_FLAG)
            except Exception as _hint_err:
                logger.debug('tool-progress onboarding hint failed: %s', _hint_err)
            return
        if event_type == '_thinking' or tool_name == '_thinking':
            if not ctx._thinking_enabled:
                return
            thinking_text = preview if tool_name == '_thinking' else tool_name
            msg = f'💬 {thinking_text}' if thinking_text else None
            if msg:
                ctx.progress_queue.put(msg)
            return
        if not ctx.tool_progress_enabled:
            return
        if event_type not in {'tool.started'}:
            return
        if tool_name == 'clarify':
            return
        try:
            _agent_for_interrupt = ctx.agent_holder[0] if ctx.agent_holder else None
            if _agent_for_interrupt is not None and getattr(_agent_for_interrupt, 'is_interrupted', False):
                return
        except Exception:
            pass
        if ctx.progress_mode == 'new' and tool_name == ctx.last_tool[0]:
            return
        ctx.last_tool[0] = tool_name
        from agent.display import get_tool_emoji
        emoji = get_tool_emoji(tool_name, default='⚙️')
        _code_block_full = None
        _code_block_short = None
        try:
            _progress_adapter = self._runner._adapter_for_source(ctx.source)
        except Exception:
            _progress_adapter = None
        if getattr(_progress_adapter, 'supports_code_blocks', False) and tool_name == 'terminal' and isinstance(args, dict) and isinstance(args.get('command'), str) and args['command'].strip():
            from agent.display import get_tool_preview_max_len
            _cmd_full = args['command'].rstrip()
            _block_header = '' if ctx.last_was_terminal_block[0] else f'{emoji} {tool_name}\n'
            _code_block_full = f'{_block_header}```\n{_cmd_full}\n```'
            _pl = get_tool_preview_max_len()
            _cap = _pl if _pl > 0 else 40
            _lines = _cmd_full.splitlines()
            _cmd_short = _lines[0] if _lines else _cmd_full
            _multiline = len(_lines) > 1
            if len(_cmd_short) > _cap:
                _cmd_short = _cmd_short[:_cap - 3] + '...'
            elif _multiline:
                _cmd_short = _cmd_short + ' ...'
            _code_block_short = f'{_block_header}```\n{_cmd_short}\n```'
        if ctx.progress_mode == 'verbose':
            if _code_block_full is not None:
                ctx.last_was_terminal_block[0] = True
                ctx.progress_queue.put(_code_block_full)
                return
            ctx.last_was_terminal_block[0] = False
            if args:
                from agent.display import get_tool_preview_max_len
                _pl = get_tool_preview_max_len()
                args_str = json.dumps(args, ensure_ascii=False, default=str)
                if _pl > 0 and len(args_str) > _pl:
                    args_str = args_str[:_pl - 3] + '...'
                msg = f'{emoji} {tool_name}({list(args.keys())})\n{args_str}'
            elif preview:
                msg = f'{emoji} {tool_name}: "{preview}"'
            else:
                msg = f'{emoji} {tool_name}...'
            ctx.progress_queue.put(msg)
            return
        if _code_block_short is not None:
            msg = _code_block_short
            ctx.last_was_terminal_block[0] = True
        elif preview:
            from agent.display import get_tool_preview_max_len, get_tool_verb, prepare_tool_preview, tool_verb_connector, verb_drops_preview
            _pl = get_tool_preview_max_len()
            _cap = _pl if _pl > 0 else 40
            _prepared_preview = prepare_tool_preview(tool_name, args, fallback=preview, max_len=_cap)
            if _progress_adapter is not None:
                preview = _progress_adapter.format_tool_preview(_prepared_preview)
            else:
                preview = _prepared_preview.text
            _verb = get_tool_verb(tool_name)
            if _verb:
                if verb_drops_preview(tool_name):
                    msg = f'{emoji} {_verb}'
                else:
                    msg = f'{emoji} {_verb}{tool_verb_connector(tool_name)}{preview}'
            else:
                msg = f'{emoji} {tool_name}: "{preview}"'
            ctx.last_was_terminal_block[0] = False
        else:
            msg = f'{emoji} {tool_name}...'
            ctx.last_was_terminal_block[0] = False
        if msg == ctx.last_progress_msg[0]:
            ctx.repeat_count[0] += 1
            ctx.progress_queue.put(('__dedup__', msg, ctx.repeat_count[0]))
            return
        ctx.last_progress_msg[0] = msg
        ctx.repeat_count[0] = 0
        ctx.progress_queue.put(msg)

    async def send_progress_messages(self):
        ctx = self._ctx
        if not ctx.progress_queue:
            return
        adapter = self._runner._adapter_for_source(ctx.source)
        if not adapter:
            return
        _adapter_edit = getattr(type(adapter), 'edit_message', None)
        if _adapter_edit is None or _adapter_edit is BasePlatformAdapter.edit_message:
            while not ctx.progress_queue.empty():
                try:
                    ctx.progress_queue.get_nowait()
                except Exception:
                    break
            return
        progress_lines = []
        progress_msg_id = None
        can_edit = ctx.progress_grouping != 'separate'
        _last_edit_ts = 0.0
        _PROGRESS_EDIT_INTERVAL = 1.5
        _progress_len_fn = adapter.message_len_fn if isinstance(adapter, BasePlatformAdapter) else len
        try:
            _raw_progress_limit = int(getattr(adapter, 'MAX_MESSAGE_LENGTH', 4000) or 4000)
        except Exception:
            _raw_progress_limit = 4000
        if isinstance(adapter, BasePlatformAdapter):
            try:
                _raw_progress_limit = int(adapter.max_message_length_for_chat(ctx.source.chat_id) or 4000)
                _progress_len_fn = adapter.message_len_fn_for_chat(ctx.source.chat_id)
            except Exception:
                pass
        _PROGRESS_TEXT_LIMIT = max(1, _raw_progress_limit - (64 if _raw_progress_limit > 128 else 0))
        _edit_accepts_metadata = False
        if ctx._progress_metadata:
            try:
                _edit_params = inspect.signature(adapter.edit_message).parameters
                _edit_accepts_metadata = 'metadata' in _edit_params or any((param.kind is inspect.Parameter.VAR_KEYWORD for param in _edit_params.values()))
            except (TypeError, ValueError):
                _edit_accepts_metadata = False

        async def _edit_progress_message(message_id: str, content: str):
            kwargs = {'chat_id': ctx.source.chat_id, 'message_id': message_id, 'content': content}
            if getattr(adapter, 'REQUIRES_EDIT_FINALIZE', False):
                kwargs['finalize'] = True
            if _edit_accepts_metadata:
                kwargs['metadata'] = ctx._progress_metadata
            return await adapter.edit_message(**kwargs)

        def _progress_text(lines: list) -> str:
            return '\n'.join((str(line) for line in lines))

        def _split_progress_groups(lines: list) -> list[list]:
            """Partition progress lines into platform-sized editable bubbles."""
            groups: list[list] = []
            current: list = []
            for line in lines:
                candidate = current + [line]
                if current and _progress_len_fn(_progress_text(candidate)) > _PROGRESS_TEXT_LIMIT:
                    groups.append(current)
                    current = [line]
                else:
                    current = candidate
            if current:
                groups.append(current)
            return groups

        def _track_progress_result(result) -> None:
            if ctx._cleanup_progress and getattr(result, 'success', False) and getattr(result, 'message_id', None):
                ctx._cleanup_msg_ids.append(str(result.message_id))

        async def _send_progress_text(text: str):
            result = await adapter.send(chat_id=ctx.source.chat_id, content=text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata)
            _track_progress_result(result)
            return result

        async def _roll_progress_overflow_if_needed() -> bool:
            """Start fresh editable progress bubbles before a bubble exceeds limit.

                Returns True when it delivered/split the current buffer, or when
                a transient edit failure left the buffer and message identity
                intact for a later retry.  In either case the caller should skip
                the normal send/edit path for this tick.
                """
            nonlocal progress_msg_id, progress_lines, can_edit
            if not progress_lines or not can_edit:
                return False
            groups = _split_progress_groups(progress_lines)
            if len(groups) <= 1:
                return False
            first_text = _progress_text(groups[0])
            if progress_msg_id is not None:
                result = await _edit_progress_message(progress_msg_id, first_text)
                if not result.success:
                    if getattr(result, 'retryable', False):
                        logger.debug('[%s] Transient overflow edit failure — keeping can_edit=True', adapter.name)
                        return True
                    can_edit = False
                    return False
            else:
                result = await _send_progress_text(first_text)
                if result.success and result.message_id:
                    progress_msg_id = result.message_id
            for group in groups[1:]:
                result = await _send_progress_text(_progress_text(group))
                if result.success and result.message_id:
                    progress_msg_id = result.message_id
            progress_lines = groups[-1]
            return True
        while True:
            try:
                if not ctx._run_still_current():
                    while not ctx.progress_queue.empty():
                        try:
                            ctx.progress_queue.get_nowait()
                        except Exception:
                            break
                    return
                raw = ctx.progress_queue.get_nowait()
                try:
                    _agent_for_interrupt = ctx.agent_holder[0] if ctx.agent_holder else None
                    if _agent_for_interrupt is not None and getattr(_agent_for_interrupt, 'is_interrupted', False):
                        await asyncio.sleep(0)
                        continue
                except Exception:
                    pass
                if isinstance(raw, tuple) and len(raw) == 3 and (raw[0] == '__dedup__'):
                    _, base_msg, count = raw
                    if progress_lines:
                        progress_lines[-1] = f'{base_msg} (×{count + 1})'
                    msg = progress_lines[-1] if progress_lines else base_msg
                elif isinstance(raw, tuple) and len(raw) >= 1 and (raw[0] == '__reset__'):
                    progress_msg_id = None
                    progress_lines = []
                    ctx.last_progress_msg[0] = None
                    ctx.repeat_count[0] = 0
                    continue
                else:
                    msg = raw
                    progress_lines.append(msg)
                if await _roll_progress_overflow_if_needed():
                    _last_edit_ts = time.monotonic()
                    await asyncio.sleep(0.3)
                    if ctx._run_still_current():
                        await adapter.send_typing(ctx.source.chat_id, metadata=ctx._progress_metadata)
                    continue
                _now = time.monotonic()
                _remaining = _PROGRESS_EDIT_INTERVAL - (_now - _last_edit_ts)
                if _remaining > 0:
                    await asyncio.sleep(_remaining)
                    continue
                if not ctx._run_still_current():
                    return
                if can_edit and progress_msg_id is not None:
                    full_text = '\n'.join(progress_lines)
                    result = await _edit_progress_message(progress_msg_id, full_text)
                    if not result.success:
                        _err = (getattr(result, 'error', '') or '').lower()
                        if getattr(result, 'retryable', False):
                            logger.debug('[%s] Transient edit failure — keeping can_edit=True', adapter.name)
                            continue
                        if 'flood' in _err or 'retry after' in _err:
                            logger.info('[%s] Progress edit flood control, backing off', adapter.name)
                            _last_edit_ts = time.monotonic()
                        else:
                            can_edit = False
                        _flood_result = await adapter.send(chat_id=ctx.source.chat_id, content=msg, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata)
                        if ctx._cleanup_progress and getattr(_flood_result, 'success', False) and getattr(_flood_result, 'message_id', None):
                            ctx._cleanup_msg_ids.append(str(_flood_result.message_id))
                else:
                    if can_edit:
                        full_text = '\n'.join(progress_lines)
                        result = await adapter.send(chat_id=ctx.source.chat_id, content=full_text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata)
                    else:
                        result = await adapter.send(chat_id=ctx.source.chat_id, content=msg, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata)
                    if result.success and result.message_id:
                        progress_msg_id = result.message_id
                        if ctx._cleanup_progress:
                            ctx._cleanup_msg_ids.append(str(result.message_id))
                _last_edit_ts = time.monotonic()
                await asyncio.sleep(0.3)
                if ctx._run_still_current():
                    await adapter.send_typing(ctx.source.chat_id, metadata=ctx._progress_metadata)
            except queue.Empty:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                while not ctx.progress_queue.empty():
                    try:
                        raw = ctx.progress_queue.get_nowait()
                        if isinstance(raw, tuple) and len(raw) == 3 and (raw[0] == '__dedup__'):
                            _, base_msg, count = raw
                            if progress_lines:
                                progress_lines[-1] = f'{base_msg} (×{count + 1})'
                                await _roll_progress_overflow_if_needed()
                        elif isinstance(raw, tuple) and len(raw) >= 1 and (raw[0] == '__reset__'):
                            await _roll_progress_overflow_if_needed()
                            if can_edit and progress_lines and progress_msg_id:
                                _pending_text = _progress_text(progress_lines)
                                try:
                                    await _edit_progress_message(progress_msg_id, _pending_text)
                                except Exception:
                                    pass
                            progress_msg_id = None
                            progress_lines = []
                            ctx.last_progress_msg[0] = None
                            ctx.repeat_count[0] = 0
                        else:
                            progress_lines.append(raw)
                            await _roll_progress_overflow_if_needed()
                    except Exception:
                        break
                if can_edit and progress_lines and progress_msg_id:
                    await _roll_progress_overflow_if_needed()
                if can_edit and progress_lines and progress_msg_id:
                    full_text = _progress_text(progress_lines)
                    try:
                        await _edit_progress_message(progress_msg_id, full_text)
                    except Exception:
                        pass
                return
            except Exception as e:
                logger.error('Progress message error: %s', e)
                await asyncio.sleep(1)

    def voice_ack_callback(self, call_id, tool_name, args):
        """tool_start_callback: speak a one-time ack in the voice channel."""
        ctx = self._ctx
        if ctx._voice_ack_fired[0] or ctx._voice_ack_guild[0] is None:
            return
        if not ctx._run_still_current():
            return
        ctx._voice_ack_fired[0] = True
        _adapter = self._runner.adapters.get(Platform.DISCORD)
        if _adapter is None or not hasattr(_adapter, 'play_ack_in_voice'):
            return
        try:
            safe_schedule_threadsafe(_adapter.play_ack_in_voice(ctx._voice_ack_guild[0]), ctx._voice_ack_loop, logger=logger, log_message='voice ack scheduling error')
        except Exception as _ack_err:
            logger.debug('voice ack schedule failed: %s', _ack_err)

    def _step_callback_sync(self, iteration: int, prev_tools: list) -> None:
        ctx = self._ctx
        if not ctx._run_still_current():
            return
        _names: list[str] = []
        for _t in prev_tools or []:
            if isinstance(_t, dict):
                _names.append(_t.get('name') or '')
            else:
                _names.append(str(_t))
        safe_schedule_threadsafe(ctx._hooks_ref.emit('agent:step', {'platform': ctx.source.platform.value if ctx.source.platform else '', 'user_id': ctx.source.user_id, 'session_id': ctx.session_id, 'iteration': iteration, 'tool_names': _names, 'tools': prev_tools}), ctx._loop_for_step, logger=logger, log_message='agent:step hook scheduling error')

    def _event_callback_sync(self, event_type: str, context: dict) -> None:
        ctx = self._ctx
        try:
            asyncio.run_coroutine_threadsafe(ctx._hooks_ref.emit(event_type, context), ctx._loop_for_step)
        except Exception as _e:
            logger.debug('event_callback hook error: %s', _e)

    def _status_callback_sync(self, event_type: str, message: str) -> None:
        ctx = self._ctx
        if not ctx._status_adapter or not ctx._run_still_current():
            return
        prepared_message = _prepare_gateway_status_message(ctx.source.platform, event_type, message)
        if prepared_message is None:
            logger.debug('status_callback suppressed for %s/%s: %s', ctx.source.platform.value if ctx.source.platform else 'unknown', event_type, _redact_gateway_user_facing_secrets(str(message or ''))[:160])
            return
        _fut = safe_schedule_threadsafe(_send_or_update_status_coro(ctx._status_adapter, ctx._status_chat_id, event_type, prepared_message, ctx._status_thread_metadata), ctx._loop_for_step, logger=logger, log_message=f'status_callback ({event_type}) scheduling error')
        if _fut is None:
            return
        if ctx._cleanup_progress:

            def _track_status_id(fut) -> None:
                try:
                    res = fut.result()
                except Exception:
                    return
                mid = getattr(res, 'message_id', None)
                if getattr(res, 'success', False) and mid:
                    ctx._cleanup_msg_ids.append(str(mid))
            _fut.add_done_callback(_track_status_id)

    def run_sync(self):
        ctx = self._ctx
        platform_key = 'cli' if ctx.source.platform == Platform.LOCAL else ctx.source.platform.value
        combined_ephemeral = ctx.context_prompt or ''
        event_channel_prompt = (ctx.channel_prompt or '').strip()
        if event_channel_prompt:
            combined_ephemeral = (combined_ephemeral + '\n\n' + event_channel_prompt).strip()
        cfg_channel_prompt = self._runner._get_system_prompt_for_channel(ctx.source.platform, ctx.source.chat_id or '', thread_id=getattr(ctx.source, 'thread_id', None), parent_id=getattr(ctx.source, 'parent_chat_id', None))
        if cfg_channel_prompt:
            combined_ephemeral = (combined_ephemeral + '\n\n' + cfg_channel_prompt).strip()
        max_iterations = _current_max_iterations()
        try:
            model, runtime_kwargs = self._runner._resolve_session_agent_runtime(source=ctx.source, session_key=ctx.session_key, user_config=ctx.user_config)
            logger.debug('run_agent resolved: model=%s provider=%s session=%s', model, runtime_kwargs.get('provider'), ctx.session_key or '')
        except Exception as exc:
            return {'final_response': f'⚠️ Provider authentication failed: {exc}', 'messages': [], 'api_calls': 0, 'tools': []}
        pr = self._runner._provider_routing
        reasoning_config = self._runner._resolve_session_reasoning_config(source=ctx.source, session_key=ctx.session_key, model=model)
        self._runner._reasoning_config = reasoning_config
        self._runner._service_tier = self._runner._resolve_session_service_tier(source=ctx.source, session_key=ctx.session_key)
        _stream_consumer = None
        _stream_delta_cb = None
        _stts_consumer_ref = ctx.streaming_tts_consumer_holder[0]
        _scfg = getattr(getattr(self._runner, 'config', None), 'streaming', None)
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()
        _plat_streaming = ctx.resolve_display_setting(ctx.user_config, platform_key, 'streaming')
        _streaming_enabled = _scfg.enabled and _scfg.transport != 'off' if _plat_streaming is None else bool(_plat_streaming)
        _want_stream_deltas = _streaming_enabled
        _want_interim_messages = ctx.interim_assistant_messages_enabled
        _want_interim_consumer = _want_interim_messages
        if _want_stream_deltas or _want_interim_consumer:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                _adapter = self._runner._adapter_for_source(ctx.source)
                if _adapter:
                    _consumer_cfg, _pause_typing_before_finalize = self._runner._build_stream_consumer_config(ctx.source, _scfg, _adapter, on_missing_cursor='raise')
                    _stream_consumer = GatewayStreamConsumer(adapter=_adapter, chat_id=ctx.source.chat_id, config=_consumer_cfg, metadata=ctx._status_thread_metadata, on_new_message=(lambda: ctx.progress_queue.put(('__reset__',))) if ctx.progress_queue is not None else None, on_before_finalize=_pause_typing_before_finalize, initial_reply_to_id=ctx.event_message_id, run_still_current=ctx._run_still_current)
                    if _want_stream_deltas:

                        def _stream_delta_cb(text: str) -> None:
                            if ctx._run_still_current():
                                _stream_consumer.on_delta(text)
                                if _stts_consumer_ref is not None:
                                    _stts_consumer_ref.on_delta(text)
                    ctx.stream_consumer_holder[0] = _stream_consumer
            except Exception as _sc_err:
                logger.debug('Could not set up stream consumer: %s', _sc_err)
        if _stream_delta_cb is None and _stts_consumer_ref is not None:

            def _stream_delta_cb(text: str) -> None:
                if ctx._run_still_current():
                    _stts_consumer_ref.on_delta(text)

        def _interim_assistant_cb(text: str, *, already_streamed: bool=False) -> None:
            if not ctx._run_still_current():
                return
            display_text = text
            if _stream_consumer is not None:
                if already_streamed:
                    _stream_consumer.on_segment_break()
                else:
                    _stream_consumer.on_commentary(display_text)
                return
            if already_streamed or not ctx._status_adapter or (not str(display_text or '').strip()):
                return
            safe_schedule_threadsafe(ctx._status_adapter.send(ctx._status_chat_id, display_text, metadata=ctx._status_thread_metadata), ctx._loop_for_step, logger=logger, log_message='interim_assistant_callback scheduling error')
        turn_route = self._runner._resolve_turn_agent_config(ctx.message, model, runtime_kwargs)
        _platforms_gw_cfg = (ctx.user_config.get('gateway') or {}).get('platforms') or {}
        _plat_gw_cfg = _platforms_gw_cfg.get(platform_key) or {}
        _skip_context = _plat_gw_cfg.get('skip_context_files')
        skip_context_files = bool(_skip_context) if _skip_context is not None else False
        _sig = self._runner._agent_config_signature(turn_route['model'], turn_route['runtime'], ctx.enabled_toolsets, combined_ephemeral, cache_keys=self._runner._extract_cache_busting_config(ctx.user_config), user_id=getattr(ctx.source, 'user_id', None), user_id_alt=getattr(ctx.source, 'user_id_alt', None), skip_context_files=skip_context_files)
        agent = None
        reused_cached_agent = False
        _cache_lock = getattr(self._runner, '_agent_cache_lock', None)
        _cache = getattr(self._runner, '_agent_cache', None)
        _peek_cached_sid = None
        if _cache_lock and _cache is not None:
            with _cache_lock:
                _peek_entry = _cache.get(ctx.session_key)
            if _peek_entry and len(_peek_entry) > 3:
                _peek_cached_sid = _peek_entry[3]
        _cached_sid_is_dead = False
        if _peek_cached_sid is not None and ctx.session_id is not None and (_peek_cached_sid != ctx.session_id):
            try:
                _cached_sid_is_dead = self._runner.session_store._is_session_ended_in_db(_peek_cached_sid)
            except Exception:
                _cached_sid_is_dead = False
        _current_msg_count = None
        if self._runner._session_db is not None and ctx.session_id:
            try:
                _sess_row = self._runner._session_db._db.get_session(ctx.session_id)
                if _sess_row:
                    _current_msg_count = _sess_row.get('message_count', 0)
            except Exception:
                pass
        _xproc_evicted_agent = None
        if _cache_lock and _cache is not None:
            with _cache_lock:
                cached = _cache.get(ctx.session_key)
                if cached and cached[1] == _sig:
                    _cached_mc = cached[2] if len(cached) > 2 else None
                    _cached_sid = cached[3] if len(cached) > 3 else None
                    _session_id_mismatch = _cached_sid is not None and ctx.session_id is not None and (_cached_sid != ctx.session_id)
                    _stale_dead_sid_reuse = _session_id_mismatch and _cached_sid_is_dead and (_cached_sid == _peek_cached_sid)
                    if _stale_dead_sid_reuse:
                        logger.info("Agent cache invalidated for session %s: cached agent's session_id %s is ended in state.db (stale self-heal artifact, #54878 x #54947) — discarding instead of reusing across the routing recovery", ctx.session_key, _cached_sid)
                        evicted = self._runner._agent_cache.pop(ctx.session_key, None)
                        _ev_agent = evicted[0] if isinstance(evicted, tuple) and evicted else None
                        if _ev_agent and _ev_agent is not _AGENT_PENDING_SENTINEL:
                            _xproc_evicted_agent = _ev_agent
                    elif not _session_id_mismatch and _cached_mc is not None and (_current_msg_count is not None) and (_current_msg_count != _cached_mc):
                        logger.info('Agent cache invalidated for session %s: message_count changed (%s -> %s), possible cross-process write', ctx.session_key, _cached_mc, _current_msg_count)
                        evicted = self._runner._agent_cache.pop(ctx.session_key, None)
                        _ev_agent = evicted[0] if isinstance(evicted, tuple) and evicted else None
                        if _ev_agent and _ev_agent is not _AGENT_PENDING_SENTINEL:
                            _xproc_evicted_agent = _ev_agent
                    else:
                        agent = cached[0]
                        if hasattr(_cache, 'move_to_end'):
                            try:
                                _cache.move_to_end(ctx.session_key)
                            except KeyError:
                                pass
                        self._runner._init_cached_agent_for_turn(agent, ctx._interrupt_depth)
                        agent.max_iterations = max_iterations
                        logger.debug('Reusing cached agent for session %s', ctx.session_key)
                        reused_cached_agent = True
        if reused_cached_agent and agent is not None:
            self._runner._apply_fallback_chain_to_agent(agent, self._runner._refresh_fallback_model())
        if _xproc_evicted_agent is not None:
            try:
                threading.Thread(target=self._runner._release_evicted_agent_soft, args=(_xproc_evicted_agent,), daemon=True, name=f'agent-xproc-evict-{str(ctx.session_key)[:24]}').start()
            except Exception:
                try:
                    self._runner._release_evicted_agent_soft(_xproc_evicted_agent)
                except Exception:
                    pass
        if agent is None:
            agent = ctx.AIAgent(model=turn_route['model'], **turn_route['runtime'], **_checkpoint_agent_kwargs(ctx.user_config), max_iterations=max_iterations, quiet_mode=True, verbose_logging=False, enabled_toolsets=ctx.enabled_toolsets, disabled_toolsets=ctx.disabled_toolsets, ephemeral_system_prompt=combined_ephemeral or None, prefill_messages=self._runner._prefill_messages or None, reasoning_config=reasoning_config, service_tier=self._runner._service_tier, request_overrides=turn_route.get('request_overrides'), providers_allowed=pr.get('only'), providers_ignored=pr.get('ignore'), providers_order=pr.get('order'), provider_sort=pr.get('sort'), provider_require_parameters=pr.get('require_parameters', False), provider_data_collection=pr.get('data_collection'), session_id=ctx.session_id, platform=platform_key, user_id=ctx.source.user_id, user_id_alt=ctx.source.user_id_alt, user_name=ctx.source.user_name, chat_id=ctx.source.chat_id, chat_name=ctx.source.chat_name, chat_type=ctx.source.chat_type, thread_id=ctx.source.thread_id, gateway_session_key=ctx.session_key, session_db=getattr(self._runner._session_db, '_db', self._runner._session_db), fallback_model=self._runner._refresh_fallback_model(), skip_context_files=skip_context_files, load_soul_identity=True)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    _cache[ctx.session_key] = (agent, _sig, _current_msg_count, ctx.session_id)
                    self._runner._enforce_agent_cache_cap()
            logger.debug('Created new agent for session %s (sig=%s)', ctx.session_key, _sig)
        agent.tool_progress_callback = ctx.progress_callback if ctx.needs_progress_queue or ctx.log_mode_enabled or ctx._live_status_adapter is not None else None
        agent.tool_start_callback = ctx.voice_ack_callback if ctx._voice_ack_guild[0] is not None else None
        agent.step_callback = ctx._step_callback_sync if ctx._hooks_ref.loaded_hooks else None
        agent.stream_delta_callback = _stream_delta_cb
        agent.interim_assistant_callback = _interim_assistant_cb if _want_interim_messages else None
        agent.status_callback = ctx._status_callback_sync

        def _notice_callback_sync(notice) -> None:
            if not ctx._status_adapter or not ctx._run_still_current():
                return
            try:
                line = render_notice_line(notice)
            except Exception:
                logger.debug('render_notice_line failed', exc_info=True)
                return
            if not line:
                return
            safe_schedule_threadsafe(self._runner._deliver_platform_notice(ctx.source, line), ctx._loop_for_step, logger=logger, log_message='notice_callback delivery scheduling error')
        agent.notice_callback = _notice_callback_sync
        agent.notice_clear_callback = None
        agent.event_callback = ctx._event_callback_sync
        agent.reasoning_config = reasoning_config
        agent.service_tier = self._runner._service_tier
        agent.request_overrides = turn_route.get('request_overrides') or {}
        agent._gateway_turn_context_notes = '\n\n'.join(self._runner._consume_pending_turn_sidecar_notes(ctx.session_key))
        _bg_review_release = threading.Event()
        _bg_review_pending: list[str] = []
        _bg_review_pending_lock = threading.Lock()

        def _deliver_bg_review_message(message: str) -> None:
            if not ctx._status_adapter or not ctx._run_still_current():
                return
            safe_schedule_threadsafe(ctx._status_adapter.send(ctx._status_chat_id, message, metadata=_non_conversational_metadata(ctx._status_thread_metadata, platform=ctx.source.platform)), ctx._loop_for_step, logger=logger, log_message='background_review_callback scheduling error')

        def _release_bg_review_messages() -> None:
            _bg_review_release.set()
            with _bg_review_pending_lock:
                pending = list(_bg_review_pending)
                _bg_review_pending.clear()
            for queued in pending:
                _deliver_bg_review_message(queued)

        def _bg_review_send(message: str) -> None:
            if not ctx._status_adapter or not ctx._run_still_current():
                return
            if not _bg_review_release.is_set():
                with _bg_review_pending_lock:
                    if not _bg_review_release.is_set():
                        _bg_review_pending.append(message)
                        return
            _deliver_bg_review_message(message)
        agent.background_review_callback = _bg_review_send
        if ctx._status_adapter and ctx.session_key:
            if getattr(type(ctx._status_adapter), 'register_post_delivery_callback', None) is not None:
                ctx._status_adapter.register_post_delivery_callback(ctx.session_key, _release_bg_review_messages, generation=ctx.run_generation)
            else:
                _pdc = getattr(ctx._status_adapter, '_post_delivery_callbacks', None)
                if _pdc is not None:
                    _pdc[ctx.session_key] = _release_bg_review_messages
        _mem_notif = ctx.user_config.get('display', {}).get('memory_notifications')
        if isinstance(_mem_notif, bool):
            _mem_notif = 'on' if _mem_notif else 'off'
        agent.memory_notifications = str(_mem_notif).lower() if _mem_notif else 'on'

        def _clarify_callback_sync(question: str, choices, multi_select: bool=False) -> str:
            from tools import clarify_gateway as _clarify_mod
            import uuid as _uuid
            if not ctx._status_adapter:
                return ''
            clarify_id = _uuid.uuid4().hex[:10]
            _clarify_mod.register(clarify_id=clarify_id, session_key=ctx.session_key or '', question=question, choices=list(choices) if choices else None, multi_select=bool(multi_select))
            try:
                ctx._status_adapter.pause_typing_for_chat(ctx._status_chat_id)
            except Exception:
                pass
            try:
                _sc = ctx.stream_consumer_holder[0] if ctx.stream_consumer_holder else None
                _flush = getattr(_sc, 'flush_pending_sync', None)
                if callable(_flush):
                    _flush(timeout=3.0)
            except Exception:
                logger.debug('Stream-consumer flush before clarify prompt failed', exc_info=True)
            send_ok = False
            fut = safe_schedule_threadsafe(ctx._status_adapter.send_clarify(chat_id=ctx._status_chat_id, question=question, choices=list(choices) if choices else None, clarify_id=clarify_id, session_key=ctx.session_key or '', metadata=ctx._status_thread_metadata), ctx._loop_for_step, logger=logger, log_message='Clarify send failed to schedule')
            if fut is None:
                send_ok = False
            else:
                try:
                    result = fut.result(timeout=15)
                    send_ok = bool(getattr(result, 'success', False))
                except Exception as exc:
                    logger.warning('Clarify send failed: %s', exc)
                    send_ok = False
            if not send_ok:
                _clarify_mod.clear_session(ctx.session_key or '')
                return '[clarify prompt could not be delivered]'
            timeout = _clarify_mod.get_clarify_timeout()
            response = _clarify_mod.wait_for_response(clarify_id, timeout=float(timeout))
            if response is None or response == '':
                return f'[user did not respond within {int(timeout / 60)}m]'
            return response
        agent.clarify_callback = _clarify_callback_sync
        agent.thinking_progress = ctx._thinking_enabled
        ctx.agent_holder[0] = agent
        agent._gateway_turn_process_task_id = ctx.process_task_id
        agent._gateway_turn_process_baseline = ctx.process_baseline
        ctx.tools_holder[0] = agent.tools if hasattr(agent, 'tools') else None
        agent_history, observed_group_context = _build_gateway_agent_history(ctx.history, channel_prompt=ctx.channel_prompt, inject_timestamps=_message_timestamps_enabled(ctx.user_config))
        if reused_cached_agent and getattr(agent, 'session_id', None) == ctx.session_id:
            _selected = _select_cached_agent_history(agent_history, getattr(agent, '_session_messages', None))
            if _selected is not agent_history:
                logger.warning('Persisted transcript lagged live cached history for session %s (disk=%d, memory=%d); preserving live conversation context (possible FTS write corruption)', ctx.session_key, len(agent_history), len(_selected))
                agent_history = strip_stale_dangerous_confirmations(_selected, now=time.time())
        _history_media_paths: set = _collect_history_media_paths(agent_history)
        from tools.approval import register_gateway_notify, reset_current_session_key, set_current_session_key, unregister_gateway_notify

        def _approval_notify_sync(approval_data: dict) -> None:
            """Send the approval request to the user from the agent thread.

                If the adapter supports interactive button-based approvals
                (e.g. Discord's ``send_exec_approval``), use that for a richer
                UX.  Otherwise fall back to a plain text message with
                ``/approve`` instructions.
                """
            ctx._status_adapter.pause_typing_for_chat(ctx._status_chat_id)
            cmd = approval_data.get('command', '')
            desc = approval_data.get('description', 'dangerous command')
            cmd = _redact_approval_command(cmd)
            if getattr(type(ctx._status_adapter), 'send_exec_approval', None) is not None:
                try:
                    _approval_fut = safe_schedule_threadsafe(ctx._status_adapter.send_exec_approval(chat_id=ctx._status_chat_id, command=cmd, session_key=_approval_session_key, description=desc, metadata=ctx._status_thread_metadata, allow_permanent=approval_data.get('allow_permanent', True), allow_session=approval_data.get('allow_session', True), smart_denied=approval_data.get('smart_denied', False)), ctx._loop_for_step, logger=logger, log_message='send_exec_approval scheduling error')
                    if _approval_fut is None:
                        raise RuntimeError('send_exec_approval: loop unavailable')
                    _approval_result = _approval_fut.result(timeout=15)
                    if _approval_result.success:
                        return
                    logger.warning('Button-based approval failed (send returned error), falling back to text: %s', _approval_result.error)
                except Exception as _e:
                    logger.warning('Button-based approval failed, falling back to text: %s', _e)
            _p = getattr(ctx._status_adapter, 'typed_command_prefix', '/')
            msg = _format_exec_approval_fallback(cmd, desc, _p, allow_permanent=approval_data.get('allow_permanent', True), allow_session=approval_data.get('allow_session', True), smart_denied=approval_data.get('smart_denied', False))
            try:
                _approval_send_fut = safe_schedule_threadsafe(ctx._status_adapter.send(ctx._status_chat_id, msg, metadata=ctx._status_thread_metadata), ctx._loop_for_step, logger=logger, log_message='Approval text-send scheduling error')
                if _approval_send_fut is not None:
                    _approval_send_fut.result(timeout=15)
            except Exception as _e:
                logger.error('Failed to send approval request: %s', _e)
        _persist_user_message_override: Optional[Any] = ctx.persist_user_message
        _persist_user_timestamp_override: Optional[float] = ctx.persist_user_timestamp
        _pending_notes = getattr(self._runner, '_pending_model_notes', {})
        _msn = _pending_notes.pop(ctx.session_key, None) if ctx.session_key else None
        if _msn:
            ctx.message = _msn + '\n\n' + ctx.message
        _freshness_window = _auto_continue_freshness_window()
        _interruption_is_fresh = _is_fresh_gateway_interruption(_last_transcript_timestamp(ctx.history), window_secs=_freshness_window)
        _resume_entry = None
        if ctx.session_key:
            try:
                _resume_entry = self._runner.session_store._entries.get(ctx.session_key)
            except Exception:
                _resume_entry = None
        _resume_mark_is_fresh = False
        if _resume_entry is not None and getattr(_resume_entry, 'resume_pending', False):
            _resume_mark_is_fresh = _is_fresh_gateway_interruption(getattr(_resume_entry, 'last_resume_marked_at', None), window_secs=_freshness_window)
        _is_resume_pending = bool(_resume_entry is not None and getattr(_resume_entry, 'resume_pending', False) and (_interruption_is_fresh or _resume_mark_is_fresh))
        _has_fresh_tool_tail = bool(agent_history and agent_history[-1].get('role') == 'tool' and _interruption_is_fresh)
        if _is_resume_pending:
            _reason = getattr(_resume_entry, 'resume_reason', None) or 'restart_timeout'
            _persist_user_message_override = ctx.message
            _resume_adapter = self._runner._adapter_for_source(ctx.source)
            _interactive_resume = bool(getattr(_resume_adapter, 'interactive_resume', True))
            ctx.message = build_resume_recovery_note(_reason, ctx.message, interactive=_interactive_resume)
        elif _has_fresh_tool_tail:
            _persist_user_message_override = ctx.message
            ctx.message = "[System note: A new message has arrived. The conversation history contains pending tool outputs from an interrupted turn. IGNORE those pending results. Address the user's NEW message below FIRST. Do NOT re-execute old tool calls from the history.]\n\n" + ctx.message
        _pending_notes = getattr(self._runner, '_pending_skills_reload_notes', None)
        if _pending_notes and ctx.session_key and (ctx.session_key in _pending_notes):
            _srn = _pending_notes.pop(ctx.session_key, None)
            if _srn:
                ctx.message = _srn + '\n\n' + ctx.message
        if isinstance(ctx.message, str) and (not ctx.message.strip()) and (_resume_entry is not None) and getattr(_resume_entry, 'resume_pending', False):
            _sn_reason = getattr(_resume_entry, 'resume_reason', None) or 'restart_timeout'
            _sn_adapter = self._runner._adapter_for_source(ctx.source)
            ctx.message = build_resume_recovery_note(_sn_reason, '', interactive=bool(getattr(_sn_adapter, 'interactive_resume', True)))
        _approval_session_key = ctx.session_key or ''
        _approval_session_token = set_current_session_key(_approval_session_key)
        register_gateway_notify(_approval_session_key, _approval_notify_sync)
        try:
            _native_imgs = self._runner._consume_pending_native_image_paths(ctx.session_key)
            if _native_imgs:
                try:
                    from agent.image_routing import build_native_content_parts
                    _parts, _skipped = build_native_content_parts(ctx.message, _native_imgs)
                    if _skipped:
                        logger.warning('Native image attachment: skipped %d unreadable path(s): %s', len(_skipped), _skipped)
                    if any((p.get('type') == 'image_url' for p in _parts)):
                        _run_message: Any = _parts
                    else:
                        _run_message = ctx.message
                except Exception as _img_exc:
                    logger.warning('Native image attachment failed, falling back to text: %s', _img_exc)
                    _run_message = ctx.message
            else:
                _run_message = ctx.message
            _api_run_message = _wrap_current_message_with_observed_context(_run_message, observed_group_context)
            _conversation_kwargs = {'conversation_history': agent_history, 'task_id': ctx.session_id}
            if _persist_user_message_override is not None:
                _conversation_kwargs['persist_user_message'] = _persist_user_message_override
            elif observed_group_context:
                _conversation_kwargs['persist_user_message'] = ctx.message
            if ctx.moa_config is not None:
                _conversation_kwargs['moa_config'] = ctx.moa_config
            if _persist_user_timestamp_override is not None:
                _conversation_kwargs['persist_user_timestamp'] = _persist_user_timestamp_override
            result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
        finally:
            unregister_gateway_notify(_approval_session_key)
            try:
                from tools.clarify_gateway import clear_session as _clear_clarify_session
                _clear_clarify_session(_approval_session_key)
            except Exception:
                pass
            reset_current_session_key(_approval_session_token)
        ctx.result_holder[0] = result
        if _stream_consumer is not None:
            _stream_consumer.finish()
        final_response = result.get('final_response')
        _last_prompt_toks = 0
        _input_toks = 0
        _output_toks = 0
        _context_length = 0
        _agent = ctx.agent_holder[0]
        if _agent and hasattr(_agent, 'context_compressor'):
            _last_prompt_toks = getattr(_agent.context_compressor, 'last_prompt_tokens', 0)
            _input_toks = getattr(_agent, 'session_prompt_tokens', 0)
            _output_toks = getattr(_agent, 'session_completion_tokens', 0)
            _context_length = getattr(_agent.context_compressor, 'context_length', 0) or 0
        _resolved_model = getattr(_agent, 'model', None) if _agent else None
        agent = ctx.agent_holder[0]
        _session_was_split = False
        _compacted_in_place = bool(getattr(agent, '_last_compaction_in_place', False)) if agent else False
        agent_session_id = getattr(agent, 'session_id', ctx.session_id) if agent else ctx.session_id
        if agent and ctx.session_key and (agent_session_id != ctx.session_id):
            _session_was_split = True
            logger.info('Session split detected: %s → %s (compression)', ctx.session_id, agent_session_id)
            entry = self._runner.session_store._entries.get(ctx.session_key)
            _session_split_entry_persisted = False
            if entry:
                entry_session_id = getattr(entry, 'session_id', None)
                if not ctx._run_still_current():
                    logger.info('Skipping session split sync for stale run %s — generation %s is no longer current', ctx.session_key or '?', ctx.run_generation)
                elif entry_session_id == agent_session_id:
                    _session_split_entry_persisted = True
                elif entry_session_id != ctx.session_id:
                    logger.info('Skipping session split sync for %s because the session binding moved from %s to %s before compression finished', ctx.session_key or '?', ctx.session_id, entry_session_id)
                else:
                    entry.session_id = agent_session_id
                    self._runner.session_store._save()
                    self._runner.session_store._record_gateway_session_peer(agent_session_id, ctx.session_key, ctx.source)
                    _session_split_entry_persisted = True
            if _session_split_entry_persisted and (getattr(ctx.source, 'platform', None) == Platform.TELEGRAM and getattr(ctx.source, 'chat_type', None) == 'dm' and (getattr(ctx.source, 'thread_id', None) is None) and (self._runner._session_db is not None)):
                try:
                    _binding = self._runner._session_db._db.get_telegram_topic_binding_by_session(session_id=agent_session_id)
                    if _binding and _binding.get('thread_id'):
                        ctx.source.thread_id = str(_binding['thread_id'])
                        logger.debug('Restored source.thread_id=%s from binding after session split %s → %s', ctx.source.thread_id, ctx.session_id, agent_session_id)
                except Exception:
                    logger.debug('Failed to restore thread_id from binding after session split', exc_info=True)
            if _session_split_entry_persisted:
                self._runner._sync_telegram_topic_binding(ctx.source, entry, reason='agent-run-compression')
        effective_session_id = agent_session_id
        self._runner._sync_session_model_from_agent(effective_session_id, agent)
        _effective_history_offset = 0 if _session_was_split or _compacted_in_place else len(agent_history)
        if not final_response:
            final_response = _normalize_empty_agent_response(result, final_response or '', history_len=len(agent_history))
            final_response = _sanitize_gateway_final_response(ctx.source.platform, final_response)
            if not final_response:
                final_response = f"⚠️ {result['error']}" if result.get('error') else ''
            return {'final_response': final_response, 'messages': result.get('messages', []), 'api_calls': result.get('api_calls', 0), 'failed': result.get('failed', False), 'failure_reason': result.get('failure_reason'), 'partial': result.get('partial', False), 'completed': result.get('completed'), 'interrupted': result.get('interrupted', False), 'interrupt_message': result.get('interrupt_message'), 'error': result.get('error'), 'compression_exhausted': result.get('compression_exhausted', False), 'compression_deferred': result.get('compression_deferred', False), 'tools': ctx.tools_holder[0] or [], 'history_offset': _effective_history_offset, 'compacted_in_place': _compacted_in_place, 'session_id': effective_session_id, 'last_prompt_tokens': _last_prompt_toks, 'input_tokens': _input_toks, 'output_tokens': _output_toks, 'model': _resolved_model, 'context_length': _context_length}
        if 'MEDIA:' not in final_response:
            media_tags, has_voice_directive = _collect_auto_append_media_tags(result.get('messages', []), history_offset=len(agent_history), history_media_paths=_history_media_paths)
            if media_tags:
                seen = set()
                unique_tags = []
                for tag in media_tags:
                    if tag not in seen:
                        seen.add(tag)
                        unique_tags.append(tag)
                if has_voice_directive:
                    unique_tags.insert(0, '[[audio_as_voice]]')
                final_response = final_response + '\n' + '\n'.join(unique_tags)
        if final_response and self._runner._session_db:
            try:
                from agent.title_generator import maybe_auto_title
                all_msgs = ctx.result_holder[0].get('messages', []) if ctx.result_holder[0] else []

                def _title_failure_cb(task: str, exc: BaseException) -> None:
                    logger.debug('Gateway auto-title failure suppressed (not user-visible): %s: %s', task, exc)
                _title_model = getattr(agent, 'model', None) if agent else None
                _title_provider = getattr(agent, 'provider', None) if agent else None
                maybe_auto_title_kwargs = {'failure_callback': _title_failure_cb, 'main_runtime': {'model': getattr(agent, 'model', None), 'provider': getattr(agent, 'provider', None), 'base_url': getattr(agent, 'base_url', None), 'api_key': getattr(agent, 'api_key', None), 'api_mode': getattr(agent, 'api_mode', None)} if agent else None, 'runtime_validator': (lambda: getattr(agent, 'model', None) == _title_model and getattr(agent, 'provider', None) == _title_provider) if agent else None}
                if self._runner._is_telegram_topic_lane(ctx.source):
                    maybe_auto_title_kwargs['title_callback'] = lambda title: self._runner._schedule_telegram_topic_title_rename(ctx.source, effective_session_id, title)
                elif self._runner._is_discord_auto_thread_lane(ctx.source) or self._runner._is_relay_discord_channel_lane(ctx.source):
                    maybe_auto_title_kwargs['title_callback'] = lambda title: self._runner._schedule_discord_semantic_thread_rename(ctx.source, effective_session_id, title)
                maybe_auto_title(getattr(self._runner._session_db, '_db', self._runner._session_db), effective_session_id, ctx.message, final_response, all_msgs, **maybe_auto_title_kwargs)
            except Exception:
                pass
        return {'final_response': final_response, 'last_reasoning': result.get('last_reasoning'), 'messages': ctx.result_holder[0].get('messages', []) if ctx.result_holder[0] else [], 'api_calls': ctx.result_holder[0].get('api_calls', 0) if ctx.result_holder[0] else 0, 'failed': ctx.result_holder[0].get('failed', False) if ctx.result_holder[0] else False, 'failure_reason': ctx.result_holder[0].get('failure_reason') if ctx.result_holder[0] else None, 'completed': ctx.result_holder[0].get('completed') if ctx.result_holder[0] else None, 'interrupted': ctx.result_holder[0].get('interrupted', False) if ctx.result_holder[0] else False, 'partial': ctx.result_holder[0].get('partial', False) if ctx.result_holder[0] else False, 'error': ctx.result_holder[0].get('error') if ctx.result_holder[0] else None, 'interrupt_message': ctx.result_holder[0].get('interrupt_message') if ctx.result_holder[0] else None, 'compression_deferred': ctx.result_holder[0].get('compression_deferred', False) if ctx.result_holder[0] else False, 'tools': ctx.tools_holder[0] or [], 'history_offset': _effective_history_offset, 'compacted_in_place': _compacted_in_place, 'last_prompt_tokens': _last_prompt_toks, 'input_tokens': _input_toks, 'output_tokens': _output_toks, 'model': _resolved_model, 'context_length': _context_length, 'session_id': effective_session_id, 'response_previewed': result.get('response_previewed', False), 'response_transformed': result.get('response_transformed', False), 'agent_persisted': ctx.result_holder[0].get('agent_persisted', True) if ctx.result_holder[0] else True}

class GatewayRunner(GatewayAuthorizationMixin, GatewayKanbanWatchersMixin, GatewaySlashCommandsMixin):
    """
    Main gateway controller.

    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """
    _busy_input_mode: str = 'interrupt'
    _busy_text_mode: str = 'interrupt'
    _restart_drain_timeout: float = DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    _restart_after_turn_timeout: float = DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    _exit_code: Optional[int] = None
    _draining: bool = False
    _external_drain_active: bool = False
    _restart_requested: bool = False
    _restart_task_started: bool = False
    _restart_detached: bool = False
    _restart_via_service: bool = False
    _detached_restart_helper_started: bool = False
    _restart_command_source: Optional[SessionSource] = None
    _stop_task: Optional[asyncio.Task] = None
    _restart_task: Optional[asyncio.Task] = None
    _profile_failed_platforms: Optional[Dict[str, Dict[Platform, asyncio.Task]]] = None
    _systemd_watchdog: Optional[Any] = None
    _startup_restore_in_progress: bool = False
    _running_agents = legacy_dict_property('_running_agents')
    _running_agents_ts = legacy_dict_property('_running_agents_ts')
    _active_session_leases = legacy_dict_property('_active_session_leases')
    _busy_ack_ts = legacy_dict_property('_busy_ack_ts')
    _turn_lease_tokens = legacy_lease_token_property()
    _session_run_generation = legacy_dict_property('_session_run_generation')
    _session_model_overrides = legacy_dict_property('_session_model_overrides')
    _pending_one_turn_model_restores = legacy_dict_property('_pending_one_turn_model_restores')
    _session_reasoning_overrides = legacy_dict_property('_session_reasoning_overrides')
    _session_service_tier_overrides = legacy_dict_property('_session_service_tier_overrides')
    _last_resolved_model = legacy_dict_property('_last_resolved_model')
    _queued_events = legacy_dict_property('_queued_events')
    _pending_turn_sidecar_notes = legacy_dict_property('_pending_turn_sidecar_notes')
    _pending_messages = legacy_dict_property('_pending_messages')
    _pending_native_image_paths_by_session = legacy_dict_property('_pending_native_image_paths_by_session')
    _session_ephemeral_pin = legacy_dict_property('_session_ephemeral_pin')
    _session_vc_last = legacy_dict_property('_session_vc_last')
    _pending_approvals = legacy_dict_property('_pending_approvals')
    _update_prompt_pending = legacy_dict_property('_update_prompt_pending')

    def _sessions_map(self) -> Dict[str, 'SessionState']:
        """The per-session state map; lazily created so bare test runners
        built via ``object.__new__`` work without ``__init__``."""
        sessions = self.__dict__.get('_sessions')
        if sessions is None:
            sessions = {}
            self.__dict__['_sessions'] = sessions
        return sessions

    def _session_state(self, session_key: str) -> 'SessionState':
        """Get-or-create the :class:`SessionState` for ``session_key``."""
        sessions = self._sessions_map()
        state = sessions.get(session_key)
        if state is None:
            state = SessionState()
            sessions[session_key] = state
        return state

    def _peek_session_state(self, session_key: str) -> Optional['SessionState']:
        """Return the SessionState for ``session_key`` without creating one."""
        sessions = self.__dict__.get('_sessions')
        if not sessions:
            return None
        return sessions.get(session_key)

    def _is_session_running(self, session_key: str) -> bool:
        """True when the session holds a running-turn slot (agent or sentinel)."""
        state = self._peek_session_state(session_key)
        return state is not None and state.turn.agent is not None

    def _running_agent_items(self) -> List[tuple]:
        """(session_key, agent) pairs for sessions with a running turn
        (including pending sentinels), matching the old ``_running_agents``
        dict contents."""
        return [(key, state.turn.agent) for key, state in self._sessions_map().items() if state.turn.agent is not None]
    _loop_heartbeat_task: Optional['asyncio.Task'] = None
    _loop_floor_timer_handle: Optional[Any] = None
    _loop_liveness_watchdog: Optional[Any] = None
    _gateway_started_at: float = 0.0
    _shutdown_watchdog_done: Optional['threading.Event'] = None
    _platform_lock_takeover_on_start: bool = False
    _reconnect_watcher_task: Optional['asyncio.Task'] = None

    def __init__(self, config: Optional[GatewayConfig]=None):
        global _gateway_runner_ref
        self.config = config if config is not None else load_gateway_config_for_runner()
        try:
            from agent.secret_scope import set_multiplex_active
            set_multiplex_active(bool(getattr(self.config, 'multiplex_profiles', False)))
        except Exception:
            logger.debug('could not set multiplex-active flag', exc_info=True)
        self.adapters: Dict[Platform, BasePlatformAdapter] = {}
        self._profile_adapters: Dict[str, Dict[Platform, BasePlatformAdapter]] = {}
        self._warn_if_docker_media_delivery_is_risky()
        _gateway_runner_ref = _weakref.ref(self)
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._service_tier = self._load_service_tier()
        self._show_reasoning = self._load_show_reasoning()
        self._busy_input_mode = self._load_busy_input_mode()
        self._busy_text_mode = self._load_busy_text_mode()
        self._restart_drain_timeout = self._load_restart_drain_timeout()
        self._restart_after_turn_timeout = self._load_restart_after_turn_timeout()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()
        from tools.process_registry import process_registry
        _bg_max_age_hours = getattr(self.config.default_reset_policy, 'bg_process_max_age_hours', 24)
        _bg_max_age_seconds = _bg_max_age_hours * 3600 if _bg_max_age_hours and _bg_max_age_hours > 0 else None
        self.session_store = SessionStore(self.config.sessions_dir, self.config, has_active_processes_fn=lambda key: process_registry.has_active_for_session(key, max_active_age=_bg_max_age_seconds))
        self._async_session_store = AsyncSessionStore(self.session_store)
        self.delivery_router = DeliveryRouter(self.config)
        self._running = False
        self._gateway_loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event = asyncio.Event()
        self._exit_cleanly = False
        self._exit_with_failure = False
        self._exit_reason: Optional[str] = None
        self._exit_code: Optional[int] = None
        self._draining = False
        self._profile_failed_platforms: Dict[str, Dict[Platform, asyncio.Task]] = {}
        self._systemd_watchdog = None
        self._external_drain_active = False
        self._restart_requested = False
        self._signal_initiated_shutdown = False
        self._restart_task_started = False
        self._restart_detached = False
        self._restart_via_service = False
        self._detached_restart_helper_started = False
        self._restart_command_source: Optional[SessionSource] = None
        self._startup_time: float = time.time()
        self._booted_from_restart: bool = False
        self._stop_task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._executor_lock = threading.Lock()
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._executor_closing = False
        self._sessions: Dict[str, SessionState] = {}
        self._turn_leases = SessionTurnLeaseRegistry()
        self._session_stall_notified: Dict[str, bool] = {}
        self._startup_restore_in_progress = False
        self._platform_lock_takeover_on_start = False
        self._startup_restore_queue: List[MessageEvent] = []
        self._startup_restore_tasks: List[asyncio.Task] = []
        self._session_sources: 'OrderedDict[str, SessionSource]' = OrderedDict()
        self._session_sources_max = 512
        self._completion_delivery_lock = threading.Lock()
        self._completion_deliveries_inflight: set[tuple[str, str, object]] = set()
        self._completion_deliveries_delivered: 'OrderedDict[tuple[str, str, object], None]' = OrderedDict()
        self._completion_delivery_retention = 2048
        import threading as _threading
        self._agent_cache: 'OrderedDict[str, tuple]' = OrderedDict()
        self._agent_cache_lock = _threading.Lock()
        self._kanban_notifier_profile = self._active_profile_name()
        self._teams_pipeline_runtime = None
        self._teams_pipeline_runtime_error: Optional[str] = None
        self._failed_platforms: Dict[Platform, Dict[str, Any]] = {}
        self._fatal_handler_tasks: set = set()
        import itertools as _itertools
        self._slash_confirm_counter = _itertools.count(1)
        try:
            from tools.tirith_security import ensure_installed
            ensure_installed(log_failures=False)
        except Exception:
            pass
        try:
            from hermes_cli.config import load_config as _load_full_config
            _appr_cfg = _load_full_config()
            _appr_mode = str(cfg_get(_appr_cfg, 'approvals', 'mode', default='manual') or 'manual').strip().lower()
            _tirith_on = bool(cfg_get(_appr_cfg, 'security', 'tirith_enabled', default=True))
            _aux_approval = cfg_get(_appr_cfg, 'auxiliary', 'approval', default=None)
            if _appr_mode == 'manual' and (not _tirith_on) and (not _aux_approval):
                logger.warning('Gateway approvals.mode=manual with no automated risk assessor (security.tirith_enabled is false and auxiliary.approval is unset): dangerous commands and execute_code scripts will BLOCK until a human approves them in chat. Enable security.tirith_enabled or configure auxiliary.approval for unattended operation.')
        except Exception:
            logger.debug('approvals.mode startup check skipped', exc_info=True)
        self._session_db = None
        try:
            from hermes_state import AsyncSessionDB, SessionDB
            self._session_db = AsyncSessionDB(SessionDB())
        except Exception as e:
            logger.warning('SQLite session store not available: %s', e)
        if self._session_db is not None:
            try:
                from hermes_cli.config import load_config as _load_full_config
                _sess_cfg = _load_full_config().get('sessions') or {}
                if _sess_cfg.get('auto_archive', False):
                    self._session_db._db.maybe_auto_archive(idle_days=float(_sess_cfg.get('auto_archive_days', 3)), min_interval_hours=int(_sess_cfg.get('min_interval_hours', 24)))
                if _sess_cfg.get('auto_prune', False):
                    self._session_db._db.maybe_auto_prune_and_vacuum(retention_days=int(_sess_cfg.get('retention_days', 90)), min_interval_hours=int(_sess_cfg.get('min_interval_hours', 24)), min_vacuum_interval_days=int(_sess_cfg.get('min_vacuum_interval_days', 30)), vacuum=bool(_sess_cfg.get('vacuum_after_prune', True)), sessions_dir=self.config.sessions_dir)
            except Exception as exc:
                logger.debug('state.db auto-maintenance skipped: %s', exc)
        try:
            from hermes_cli.config import load_config as _load_full_config
            _ckpt_cfg = _load_full_config().get('checkpoints') or {}
            if _ckpt_cfg.get('auto_prune', False):
                from tools.checkpoint_manager import maybe_auto_prune_checkpoints
                maybe_auto_prune_checkpoints(retention_days=int(_ckpt_cfg.get('retention_days', 7)), min_interval_hours=int(_ckpt_cfg.get('min_interval_hours', 24)), delete_orphans=False, max_total_size_mb=int(_ckpt_cfg.get('max_total_size_mb', 500)))
        except Exception as exc:
            logger.debug('checkpoint auto-maintenance skipped: %s', exc)
        from gateway.pairing import PairingStore
        self.pairing_store = PairingStore()
        self.pairing_stores: Dict[str, 'PairingStore'] = {}
        from gateway.hooks import HookRegistry
        self.hooks = HookRegistry()
        self._voice_mode: Dict[str, str] = self._load_voice_modes()
        self._recent_voice_transcripts: Dict[tuple[int, int], List[tuple[float, str]]] = {}
        self._background_tasks: set = set()
        self._gateway_started_at: float = time.time()
        self._loop_heartbeat_task: Optional[asyncio.Task] = None
        self._loop_floor_timer_handle = None
        self._loop_liveness_watchdog = None
        self._last_inbound_at: float = time.time()
        self._scale_to_zero_cooldown_until: float = 0.0

    def _wire_teams_pipeline_runtime(self) -> None:
        """Bind the Teams meeting pipeline runtime to Graph webhook ingress.

        No-op when the msgraph_webhook adapter isn't running or the
        teams_pipeline plugin isn't enabled — lets the gateway start cleanly
        whether or not the user has opted into the pipeline.
        """
        if Platform.MSGRAPH_WEBHOOK not in self.adapters:
            return
        if not _teams_pipeline_plugin_enabled():
            logger.debug('Teams pipeline plugin is disabled; skipping runtime wiring')
            return
        try:
            from plugins.teams_pipeline.runtime import bind_gateway_runtime
        except Exception as exc:
            logger.warning('Teams pipeline runtime import failed: %s', exc)
            return
        try:
            bound = bind_gateway_runtime(self)
        except Exception as exc:
            logger.warning('Teams pipeline runtime wiring failed: %s', exc)
            return
        if bound:
            logger.info('Teams pipeline runtime bound to msgraph webhook ingress')
        elif self._teams_pipeline_runtime_error:
            logger.warning('Teams pipeline runtime unavailable: %s', self._teams_pipeline_runtime_error)

    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        """Warn when Docker-backed gateways lack an explicit export mount.

        MEDIA delivery happens in the gateway process, so paths emitted by the model
        must be readable from the host. A plain container-local path like
        `/workspace/report.txt` or `/output/report.txt` often exists only inside
        Docker, so users commonly need a dedicated export mount such as
        `host-dir:/output`.
        """
        if os.getenv('TERMINAL_ENV', '').strip().lower() != 'docker':
            return
        connected = self.config.get_connected_platforms()
        messaging_platforms = [p for p in connected if p not in {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}]
        if not messaging_platforms:
            return
        raw_volumes = os.getenv('TERMINAL_DOCKER_VOLUMES', '').strip()
        volumes: List[str] = []
        if raw_volumes:
            try:
                parsed = json.loads(raw_volumes)
                if isinstance(parsed, list):
                    volumes = [str(v) for v in parsed if isinstance(v, str)]
            except Exception:
                logger.debug('Could not parse TERMINAL_DOCKER_VOLUMES for gateway media warning', exc_info=True)
        has_explicit_output_mount = False
        for spec in volumes:
            match = _DOCKER_VOLUME_SPEC_RE.match(spec)
            if not match:
                continue
            container_path = match.group('container')
            if container_path in _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS:
                has_explicit_output_mount = True
                break
        if has_explicit_output_mount:
            return
        logger.warning("Docker backend is enabled for the messaging gateway but no explicit host-visible output mount (for example '/home/user/.duck-agent/cache/documents:/output') is configured. This is fine if the model already emits host-visible paths, but MEDIA file delivery can fail for container-local paths like '/workspace/...' or '/output/...'.")

    def _has_setup_skill(self) -> bool:
        """Check if the duck-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill('duck-agent-setup') is not None
        except Exception:
            return False
    _VOICE_MODE_PATH = _hermes_home / 'gateway_voice_mode.json'

    def _voice_key(self, platform: Platform, chat_id: str) -> str:
        """Return a platform-namespaced key for voice mode state."""
        return f'{platform.value}:{chat_id}'

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        valid_modes = {'off', 'voice_only', 'all'}
        result = {}
        for chat_id, mode in data.items():
            if mode not in valid_modes:
                continue
            key = str(chat_id)
            if ':' not in key:
                logger.warning('Skipping legacy unprefixed voice mode key %r during migration. Re-enable voice mode on that chat to rebuild the prefixed key.', key)
                continue
            result[key] = mode
        return result

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(json.dumps(self._voice_mode, indent=2), encoding='utf-8')
        except OSError as e:
            logger.warning('Failed to save voice modes: %s', e)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, '_auto_tts_disabled_chats', None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
            enabled_chats = getattr(adapter, '_auto_tts_enabled_chats', None)
            if isinstance(enabled_chats, set):
                enabled_chats.discard(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _set_adapter_auto_tts_enabled(self, adapter, chat_id: str, enabled: bool) -> None:
        """Update an adapter's per-chat auto-TTS opt-in set if present.

        Used for ``/voice on``/``/voice tts`` where the user explicitly wants
        auto-TTS even when ``voice.auto_tts`` is False globally.
        """
        enabled_chats = getattr(adapter, '_auto_tts_enabled_chats', None)
        if not isinstance(enabled_chats, set):
            return
        if enabled:
            enabled_chats.add(chat_id)
            disabled_chats = getattr(adapter, '_auto_tts_disabled_chats', None)
            if isinstance(disabled_chats, set):
                disabled_chats.discard(chat_id)
        else:
            enabled_chats.discard(chat_id)

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live platform adapter.

        Populates three fields from config + ``self._voice_mode``:
          - ``_auto_tts_default``: global default from ``voice.auto_tts``
          - ``_auto_tts_enabled_chats``: chats with mode ``voice_only``/``all``
          - ``_auto_tts_disabled_chats``: chats with mode ``off``
        """
        platform = getattr(adapter, 'platform', None)
        if not isinstance(platform, Platform):
            return
        disabled_chats = getattr(adapter, '_auto_tts_disabled_chats', None)
        enabled_chats = getattr(adapter, '_auto_tts_enabled_chats', None)
        if not isinstance(disabled_chats, set) and (not isinstance(enabled_chats, set)):
            return
        try:
            from hermes_cli.config import load_config as _load_full_config
            _full_cfg = _load_full_config()
            _auto_tts_default = bool((_full_cfg.get('voice') or {}).get('auto_tts', False))
        except Exception:
            _auto_tts_default = False
        if hasattr(adapter, '_auto_tts_default'):
            adapter._auto_tts_default = _auto_tts_default
        prefix = f'{platform.value}:'
        if isinstance(disabled_chats, set):
            disabled_chats.clear()
            disabled_chats.update((key[len(prefix):] for key, mode in self._voice_mode.items() if mode == 'off' and key.startswith(prefix)))
        if isinstance(enabled_chats, set):
            enabled_chats.clear()
            enabled_chats.update((key[len(prefix):] for key, mode in self._voice_mode.items() if mode in {'voice_only', 'all'} and key.startswith(prefix)))

    async def _await_adapter_cleanup_with_timeout(self, awaitable: Awaitable[Any], timeout: float) -> bool:
        """Wait for adapter cleanup without letting cancellation swallowing hang us.

        ``asyncio.wait_for`` cancels an overdue child but then waits for it to
        exit. An adapter close path that catches ``CancelledError`` can therefore
        block recovery forever. Keep ownership of the old task through its done
        callback, but release the runner at the deadline.
        """
        if timeout <= 0:
            await awaitable
            return True
        task = asyncio.ensure_future(awaitable)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            await task
            return True
        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        return False

    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        """Call adapter.disconnect() defensively, swallowing any error.

        Used when adapter.connect() failed or raised — the adapter may
        have allocated partial resources (aiohttp.ClientSession, poll
        tasks, child subprocesses) that would otherwise leak and surface
        as "Unclosed client session" warnings at process exit.

        Must tolerate partial-init state and never raise, since callers
        use it inside error-handling blocks.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        try:
            completed = await self._await_adapter_cleanup_with_timeout(adapter.disconnect(), timeout)
            if not completed:
                logger.warning('Timed out after %.1fs while disconnecting %s adapter; continuing shutdown', timeout, platform.value if platform is not None else 'adapter')
        except Exception as e:
            logger.debug('Defensive %s disconnect after failed connect raised: %s', platform.value if platform is not None else 'adapter', e)

    async def _bounded_adapter_teardown(self, adapter, platform, *, profile: Optional[str]=None) -> None:
        """Tear down one adapter on the shutdown path with bounded awaits.

        Both ``cancel_background_tasks()`` and ``disconnect()`` can block
        indefinitely when a platform's network state is half-dead (e.g. a
        wedged Feishu/Lark WebSocket thread waiting on I/O). An unbounded
        await here stalls the entire shutdown sequence past systemd's
        ``TimeoutStopSec``; the resulting SIGKILL skips ``atexit`` PID-file
        cleanup, so the next start dies with "PID file race lost" (#14128).

        Each await uses the existing per-adapter timeout budget
        (``HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT``). On timeout the old
        task is cancelled and detached, then teardown forces forward progress;
        the loop never hangs even if an adapter swallows cancellation. Never
        raises.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        suffix = f' (profile: {profile})' if profile else ''
        started_at = time.monotonic()
        try:
            cancelled = await self._await_adapter_cleanup_with_timeout(adapter.cancel_background_tasks(), timeout)
            if not cancelled:
                logger.warning('✗ %s background-task cancel timed out after %.1fs - forcing continue%s', platform.value, timeout, suffix)
        except Exception as e:
            logger.debug('✗ %s background-task cancel error%s: %s', platform.value, suffix, e)
        try:
            disconnected = await self._await_adapter_cleanup_with_timeout(adapter.disconnect(), timeout)
            if disconnected:
                logger.info('✓ %s disconnected (%.2fs)%s', platform.value, time.monotonic() - started_at, suffix)
            else:
                logger.warning('✗ %s disconnect timed out after %.1fs - forcing continue%s', platform.value, timeout, suffix)
        except Exception as e:
            logger.error('✗ %s disconnect error after %.2fs%s: %s', platform.value, time.monotonic() - started_at, suffix, e)

    def _adapter_disconnect_timeout_secs(self) -> float:
        """Return the per-adapter disconnect timeout used during shutdown."""
        raw = os.getenv('HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT', '').strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning('Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r', raw)
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    def _platform_connect_timeout_secs(self, platform=None) -> float:
        """Return the per-platform connect timeout used during startup/retry."""
        raw = os.getenv('HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT', '').strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning('Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r', raw)
            else:
                return max(0.0, timeout)
        if platform == Platform.TELEGRAM:
            return _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def _connect_adapter_with_timeout(self, adapter, platform, *, is_reconnect: bool=False) -> bool:
        """Connect an adapter without allowing one platform to block others.

        ``is_reconnect`` is forwarded to ``adapter.connect()`` so platform
        adapters can distinguish a cold first boot (drop any stale
        server-side queue) from a watcher reconnect after a prolonged outage
        (preserve the queue so messages sent during the outage are delivered
        rather than silently dropped — #46621).
        """
        timeout = self._platform_connect_timeout_secs(platform)
        if timeout <= 0:
            return await adapter.connect(is_reconnect=is_reconnect)
        task = asyncio.ensure_future(adapter.connect(is_reconnect=is_reconnect))
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            result = await task
            return bool(result)
        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        raise TimeoutError(f'{platform.value} connect timed out after {timeout:g}s')

    async def _connect_initial_adapter_with_timeout(self, adapter, platform) -> bool:
        """Connect one cold-start adapter with tightly scoped replace intent.

        The capability is visible only while this initial connect is awaited.
        Reconnects call ``_connect_adapter_with_timeout`` directly and adapters
        also default to deny, so a later network recovery can never evict a
        healthy token holder.
        """
        adapter._platform_lock_takeover_allowed = bool(self._platform_lock_takeover_on_start)
        try:
            return await self._connect_adapter_with_timeout(adapter, platform)
        finally:
            adapter._platform_lock_takeover_allowed = False

    @property
    def should_exit_cleanly(self) -> bool:
        return self._exit_cleanly

    @property
    def should_exit_with_failure(self) -> bool:
        return self._exit_with_failure

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def _session_key_for_source(self, source: SessionSource) -> str:
        """Resolve the current session key for a source, honoring gateway config when available."""
        if hasattr(self, 'session_store') and self.session_store is not None:
            try:
                session_key = self.session_store._generate_session_key(source)
                if isinstance(session_key, str) and session_key:
                    return session_key
            except Exception:
                pass
        config = getattr(self, 'config', None)
        _profile = None
        if getattr(config, 'multiplex_profiles', False):
            if source.profile:
                _profile = source.profile
            else:
                try:
                    from hermes_cli.profiles import get_active_profile_name
                    _profile = get_active_profile_name() or 'default'
                except Exception:
                    _profile = None
        return build_session_key(source, group_sessions_per_user=getattr(config, 'group_sessions_per_user', True), thread_sessions_per_user=getattr(config, 'thread_sessions_per_user', False), profile=_profile)

    def _telegram_topic_mode_enabled(self, source: SessionSource) -> bool:
        """Return whether Telegram DM topic mode is active for this chat."""
        if source.platform != Platform.TELEGRAM or source.chat_type != 'dm':
            return False
        session_db = getattr(self, '_session_db', None)
        if session_db is None:
            return False
        session_db = getattr(session_db, '_db', session_db)
        try:
            raw = session_db.is_telegram_topic_mode_enabled(chat_id=str(source.chat_id), user_id=str(source.user_id))
        except Exception:
            logger.debug('Failed to read Telegram topic mode state', exc_info=True)
            return False
        return raw is True
    _TELEGRAM_GENERAL_TOPIC_IDS = frozenset({'', '1'})

    def _is_telegram_topic_root_lobby(self, source: SessionSource) -> bool:
        """True for the main Telegram DM (or General topic) when topic mode has made it a lobby."""
        if source.platform != Platform.TELEGRAM or source.chat_type != 'dm':
            return False
        if not self._telegram_topic_mode_enabled(source):
            return False
        tid = str(source.thread_id or '')
        return tid in self._TELEGRAM_GENERAL_TOPIC_IDS

    def _is_telegram_topic_lane(self, source: SessionSource) -> bool:
        """True for a user-created Telegram private-chat topic lane."""
        if source.platform != Platform.TELEGRAM or source.chat_type != 'dm':
            return False
        if not self._telegram_topic_mode_enabled(source):
            return False
        tid = str(source.thread_id or '')
        if not tid or tid in self._TELEGRAM_GENERAL_TOPIC_IDS:
            return False
        return True
    _TELEGRAM_LOBBY_REMINDER_COOLDOWN_S = 30.0

    def _should_send_telegram_lobby_reminder(self, source: SessionSource) -> bool:
        """Rate-limit root-DM lobby reminders to one message per cooldown window.

        A user who forgets multi-session mode is enabled and types several
        prompts in the root DM would otherwise get a reminder for every
        message. Cap it so the first one lands and the rest stay quiet.
        """
        if not hasattr(self, '_telegram_lobby_reminder_ts'):
            self._telegram_lobby_reminder_ts = {}
        chat_id = str(source.chat_id or '')
        if not chat_id:
            return True
        import time as _time
        now = _time.monotonic()
        last = self._telegram_lobby_reminder_ts.get(chat_id, 0.0)
        if now - last < self._TELEGRAM_LOBBY_REMINDER_COOLDOWN_S:
            return False
        self._telegram_lobby_reminder_ts[chat_id] = now
        return True

    def _telegram_topic_root_lobby_message(self) -> str:
        return 'This main chat is reserved for system commands.\n\nTo start a new Duck Agent chat, open the All Messages topic at the top of this bot interface and send any message there. Telegram will create a new topic for that message; each topic works as an independent Duck Agent session.'

    def _telegram_topic_root_new_message(self) -> str:
        return "To start a new parallel Duck Agent chat, open the All Messages topic at the top of this bot interface and send any message there. Telegram will create a new topic for it.\n\nEach topic is an independent Duck Agent session. Use /new inside an existing topic only if you want to replace that topic's current session."

    def _telegram_topic_new_header(self, source: SessionSource) -> Optional[str]:
        if not self._is_telegram_topic_lane(source):
            return None
        return 'Started a new Duck Agent session in this topic.\n\nTip: for parallel work, open All Messages and send a message there to create a separate topic instead of using /new here. /new replaces the session attached to the current topic.'

    def _record_telegram_topic_binding(self, source: SessionSource, session_entry) -> None:
        """Persist the Telegram topic -> Duck Agent session binding for topic lanes."""
        session_db = getattr(self, '_session_db', None)
        if session_db is None or not source.chat_id or (not source.thread_id):
            return
        session_db = getattr(session_db, '_db', session_db)
        session_db.bind_telegram_topic(chat_id=str(source.chat_id), thread_id=str(source.thread_id), user_id=str(source.user_id or ''), session_key=session_entry.session_key, session_id=session_entry.session_id)

    def _sync_telegram_topic_binding(self, source: SessionSource, session_entry, *, reason: str) -> None:
        """Update the topic binding to point at ``session_entry.session_id``.

        Telegram topic lanes persist a (chat_id, thread_id) -> session_id row
        so reopening a topic in a fresh process resumes the right Duck Agent
        session. When compression rotates ``session_entry.session_id`` mid-turn,
        the binding goes stale and the next inbound message in that topic
        reloads the oversized parent transcript instead of the compressed
        child, retriggering preflight compression — sometimes in a loop
        (#20470, #29712, #33414).
        """
        if not self._is_telegram_topic_lane(source):
            return
        try:
            self._record_telegram_topic_binding(source, session_entry)
        except Exception:
            logger.debug('telegram topic binding refresh failed (%s)', reason, exc_info=True)

    def _recover_telegram_topic_thread_id(self, source: SessionSource) -> Optional[str]:
        """Pin DM-topic routing to the user's last-active topic.

        Telegram can omit ``message_thread_id`` or surface General (``1``)
        for some topic-mode DM replies. In those lobby-shaped cases, keep the
        conversation attached to the user's most-recent bound topic.

        Do not rewrite a non-lobby, previously-unbound thread id: a newly
        created Telegram DM topic is also "unknown" until the first inbound
        message is recorded, and rewriting it would send that brand-new topic's
        answer into an older lane. Returns None to leave the source alone.
        """
        if source.platform != Platform.TELEGRAM or source.chat_type != 'dm' or (not source.chat_id) or (not source.user_id) or (not self._telegram_topic_mode_enabled(source)):
            return None
        inbound = str(source.thread_id or '')
        is_lobby = not inbound or inbound in self._TELEGRAM_GENERAL_TOPIC_IDS
        if not is_lobby:
            return None
        session_db = getattr(self, '_session_db', None)
        if session_db is None:
            return None
        session_db = getattr(session_db, '_db', session_db)
        try:
            bindings = session_db.list_telegram_topic_bindings_for_chat(chat_id=str(source.chat_id))
        except Exception:
            logger.debug('topic-recover: read failed', exc_info=True)
            return None
        if not bindings:
            return None
        user_id = str(source.user_id)
        for b in bindings:
            if str(b.get('user_id') or '') == user_id:
                recovered = str(b.get('thread_id') or '')
                if recovered and recovered != inbound:
                    return recovered
                return None
        return None

    def _normalize_source_for_session_key(self, source: SessionSource) -> SessionSource:
        """Apply Telegram DM topic recovery to a source for session-key purposes.

        ``_handle_message_with_agent`` rewrites ``source.thread_id`` via
        ``_recover_telegram_topic_thread_id`` *before* deriving the session
        key for a normal message turn (a lobby/stripped reply gets pinned to
        the user's last-active topic).  Session-scoped command handlers like
        ``/model`` and ``/reasoning`` derive their override key from the raw
        inbound ``event.source``, which skips that recovery — so the override
        is stored under a different key than the next message turn reads,
        and the override is silently dropped on Telegram forum topics and
        after compression session splits (#30479).

        Returns a recovery-normalized copy when a rewrite applies, otherwise
        the original source unchanged.  Always derive the override storage key
        from the result so storage and read use an identical key.
        """
        try:
            recovered = self._recover_telegram_topic_thread_id(source)
        except Exception:
            return source
        if recovered is None:
            return source
        return dataclasses.replace(source, thread_id=recovered)

    def _resolve_session_agent_runtime(self, *, source: Optional[SessionSource]=None, session_key: Optional[str]=None, user_config: Optional[dict]=None) -> tuple[str, dict]:
        """Resolve model/runtime for a session.

        Priority (highest first): session ``/model`` → ``channel_overrides`` →
        global config/env (``_resolve_gateway_model(user_config)`` and default
        provider resolution).
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None
        model = _resolve_gateway_model(user_config)
        if resolved_session_key:
            self._rehydrate_session_model_override(resolved_session_key)
        _override_state = self._peek_session_state(resolved_session_key) if resolved_session_key else None
        override = _override_state.conversation.model_override if _override_state else None
        if override:
            override_model = override.get('model', model)
            override_runtime = {'provider': override.get('provider'), 'api_key': override.get('api_key'), 'base_url': override.get('base_url'), 'api_mode': override.get('api_mode'), 'max_tokens': override.get('max_tokens'), 'credential_pool': override.get('credential_pool')}
            if override_runtime.get('api_key'):
                if override_runtime.get('credential_pool') is None:
                    override_runtime['credential_pool'] = _credential_pool_for_provider(override.get('provider'))
                logger.debug('Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s', resolved_session_key or '', model, override_model, override_runtime.get('provider'))
                return (override_model, override_runtime)
            logger.debug('Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s', resolved_session_key or '', model, override_model)
        else:
            logger.debug('No session model override: session=%s config_model=%s override_keys=%s', resolved_session_key or '', model, [_key for _key, _st in list(self._sessions_map().items()) if _st.conversation.model_override is not None][:5] or '[]')
        runtime_kwargs = _resolve_runtime_agent_kwargs()
        runtime_model = runtime_kwargs.pop('model', None)
        if runtime_model:
            logger.info('Runtime provider supplied explicit model override: %s -> %s', model, runtime_model)
            model = runtime_model
        cfg = getattr(self, 'config', None)
        if cfg and source is not None:
            chat_id = str(source.chat_id) if source.chat_id else ''
            thread_id = str(source.thread_id) if getattr(source, 'thread_id', None) else None
            parent_id = str(source.parent_chat_id) if getattr(source, 'parent_chat_id', None) else None
            ch = _get_channel_override(cfg, source.platform, chat_id, thread_id=thread_id, parent_id=parent_id)
            if ch:
                if ch.model:
                    model = ch.model
                if ch.provider:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(ch.provider)
                    ch_runtime_model = runtime_kwargs.pop('model', None)
                    if ch_runtime_model and (not ch.model):
                        model = ch_runtime_model
        if override and resolved_session_key:
            model, runtime_kwargs = self._apply_session_model_override(resolved_session_key, model, runtime_kwargs)
        if not model and runtime_kwargs.get('provider'):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs['provider'])
                if model:
                    logger.info('No model configured — defaulting to %s for provider %s', model, runtime_kwargs['provider'])
            except Exception:
                pass
        if not model:
            _lr_state = self._peek_session_state(resolved_session_key) if resolved_session_key else None
            _lr_star = self._peek_session_state('*')
            _recovered = (_lr_state.conversation.last_resolved_model if _lr_state else '') or (_lr_star.conversation.last_resolved_model if _lr_star else '')
            if _recovered:
                logger.warning('Empty model resolved for session=%s — recovering last-known-good model %s (config read likely returned empty; see #35314)', resolved_session_key or '', _recovered)
                model = _recovered
        elif model:
            if resolved_session_key:
                self._session_state(resolved_session_key).conversation.last_resolved_model = model
            self._session_state('*').conversation.last_resolved_model = model
        return (model, runtime_kwargs)

    def _resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        """Build the effective model/runtime config for a single turn.

        Always uses the session's primary model/provider.  If `/fast` is
        enabled and the model supports Priority Processing / Anthropic fast
        mode, attach `request_overrides` so the API call is marked
        accordingly.
        """
        from hermes_cli.models import resolve_fast_mode_overrides
        runtime = {'api_key': runtime_kwargs.get('api_key'), 'base_url': runtime_kwargs.get('base_url'), 'provider': runtime_kwargs.get('provider'), 'requested_provider': runtime_kwargs.get('requested_provider'), 'api_mode': runtime_kwargs.get('api_mode'), 'command': runtime_kwargs.get('command'), 'args': list(runtime_kwargs.get('args') or []), 'credential_pool': runtime_kwargs.get('credential_pool'), 'max_tokens': runtime_kwargs.get('max_tokens')}
        route = {'model': model, 'runtime': runtime, 'signature': (model, runtime['provider'], runtime['requested_provider'], runtime['base_url'], runtime['api_mode'], runtime['command'], tuple(runtime['args']))}
        service_tier = getattr(self, '_service_tier', None)
        if not service_tier:
            route['request_overrides'] = {}
            return route
        try:
            overrides = resolve_fast_mode_overrides(route['model'])
        except Exception:
            overrides = None
        route['request_overrides'] = overrides or {}
        return route

    def _sync_session_model_from_agent(self, session_id: str, agent: Any) -> None:
        """Persist the runtime model/provider actually used by a gateway turn.

        Provider fallback can switch ``agent.model``/``agent.provider`` after the
        session row was created. Keep the session DB metadata in sync so session
        lists, desktop/dashboard details, and follow-up session tooling report the
        backend that actually answered the latest turn.

        Called from the ``run_sync`` closure, which executes off the event loop
        in the executor thread — so the synchronous ``SessionDB`` (``_db``) is
        used directly rather than awaiting the AsyncSessionDB forwarder.
        """
        if not session_id or agent is None or self._session_db is None:
            return
        model = getattr(agent, 'model', None)
        if not model:
            return
        runtime = {'provider': getattr(agent, 'provider', None), 'base_url': getattr(agent, 'base_url', None), 'api_mode': getattr(agent, 'api_mode', None), 'fallback_active': bool(getattr(agent, '_fallback_activated', False))}
        runtime = {k: v for k, v in runtime.items() if v not in (None, '')}
        try:
            db = self._session_db._db
            row = db.get_session(session_id)
            if not row:
                return
            current_model = row.get('model')
            raw_config = row.get('model_config')
            try:
                config = json.loads(raw_config) if raw_config else {}
            except Exception:
                config = {}
            if not isinstance(config, dict):
                config = {}
            gateway_runtime = dict(config.get('gateway_runtime') or {})
            if current_model == model and all((gateway_runtime.get(k) == v for k, v in runtime.items())):
                return
            config['gateway_runtime'] = runtime
            db.update_session_meta(session_id, json.dumps(config), model=model)
        except Exception:
            logger.debug('Failed to sync gateway session model metadata', exc_info=True)

    async def _handle_reaction_event(self, ctx: Dict[str, Any]) -> None:
        """Fan a normalised platform reaction event out to the HookRegistry.

        Adapters call this via ``set_reaction_handler`` for every
        platform-native reaction event they surface. The adapter-supplied
        ``event_name`` ("reaction:added" / "reaction:removed") becomes the
        hook event so user hooks subscribe with the same name scheme as the
        existing ``agent:*`` family. Errors never block the adapter's event
        loop — the hook contract is non-blocking.
        """
        event_name = str(ctx.get('event_name') or 'reaction:added')
        try:
            await self.hooks.emit(event_name, ctx)
        except Exception:
            logger.debug('[Gateway] reaction hook emit failed', exc_info=True)

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.

        The notification arrives on the failing adapter's own polling task,
        and the disconnect inside the handler can cancel that task mid-flight:
        disconnect()'s current-task guard misses it because
        _safe_adapter_disconnect runs the close in a wrapper task. A cancelled
        handler dies between the fatal log and the reconnect queue, silently
        stranding the platform (observed 2026-07-21: telegram popped from
        adapters but never queued after a travel network outage). Run the real
        work in a detached task that adapter teardown cannot cancel.
        """
        tasks = getattr(self, '_fatal_handler_tasks', None)
        if tasks is None:
            tasks = self._fatal_handler_tasks = set()
        task = asyncio.create_task(self._handle_adapter_fatal_error_detached(adapter))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        await asyncio.shield(task)

    def _queue_retryable_fatal_platform(self, adapter: BasePlatformAdapter) -> bool:
        """Queue a retryable fatal adapter for background reconnection.

        Returns True when the platform was newly queued. Idempotent if already
        queued. Must not await: callers invoke this *before* any disconnect
        await so a wedged close cannot strand the platform (#80598).
        """
        if not adapter.fatal_error_retryable:
            return False
        platform_config = self.config.platforms.get(adapter.platform)
        if not platform_config or adapter.platform in self._failed_platforms:
            return False
        self._failed_platforms[adapter.platform] = {'config': platform_config, 'attempts': 0, 'next_retry': time.monotonic(), 'credential_claim': self._adapter_credential_claim(adapter.platform, adapter), 'listener_claim': self._adapter_listener_claim(adapter.platform, adapter)}
        logger.info('%s queued for background reconnection', adapter.platform.value)
        self._ensure_reconnect_watcher_running()
        return True

    async def _handle_adapter_fatal_error_detached(self, adapter: BasePlatformAdapter) -> None:
        """Run the fatal handler; if the platform still ends up stranded
        (not reconnected, not queued, not intentionally disabled), exit the
        gateway with failure so the service manager restarts it instead of
        leaving a silent partial outage."""
        try:
            timeout = self._adapter_disconnect_timeout_secs()
            if timeout <= 0:
                await self._handle_adapter_fatal_error_impl(adapter)
            else:
                outer = timeout + min(2.0, max(0.05, timeout))
                completed = await self._await_adapter_cleanup_with_timeout(self._handle_adapter_fatal_error_impl(adapter), outer)
                if not completed:
                    logger.error('Fatal-error handling for %s timed out after %.1fs; ensuring reconnect queue is populated', adapter.platform.value, outer)
                    self._queue_retryable_fatal_platform(adapter)
        except asyncio.CancelledError:
            try:
                self._queue_retryable_fatal_platform(adapter)
            except Exception:
                logger.debug('Failed to queue %s after fatal-handler cancellation', adapter.platform.value, exc_info=True)
            raise
        except Exception:
            logger.exception('Fatal-error handling for %s raised unexpectedly', adapter.platform.value)
            try:
                self._queue_retryable_fatal_platform(adapter)
            except Exception:
                logger.debug('Failed to queue %s after fatal-handler exception', adapter.platform.value, exc_info=True)
        finally:
            platform = adapter.platform
            shutdown_event = getattr(self, '_shutdown_event', None)
            stranded = adapter.fatal_error_retryable and platform not in self.adapters and (platform not in getattr(self, '_failed_platforms', {})) and (not (shutdown_event is not None and shutdown_event.is_set()))
            if stranded:
                logger.error('%s adapter was lost without entering the reconnection queue; exiting gateway so the service manager restarts it.', platform.value)
                self._exit_reason = f'{platform.value} adapter lost without reconnection queue'
                self._exit_with_failure = True
                await self.stop()

    async def _handle_adapter_fatal_error_impl(self, adapter: BasePlatformAdapter) -> None:
        existing = self.adapters.get(adapter.platform)
        if existing is not None and existing is not adapter:
            logger.debug('Ignoring stale fatal error from a superseded %s adapter instance: %s', adapter.platform.value, adapter.fatal_error_code or 'unknown')
            return
        logger.error('Fatal %s adapter error (%s): %s', adapter.platform.value, adapter.fatal_error_code or 'unknown', adapter.fatal_error_message or 'unknown error')
        if adapter.fatal_error_code == 'relay_disabled':
            platform_state = 'disabled'
        elif adapter.fatal_error_retryable:
            platform_state = 'retrying'
        else:
            platform_state = 'fatal'
        self._update_platform_runtime_status(adapter.platform.value, platform_state=platform_state, error_code=adapter.fatal_error_code, error_message=adapter.fatal_error_message)
        if existing is adapter:
            self.adapters.pop(adapter.platform, None)
            self.delivery_router.adapters = self.adapters
        self._queue_retryable_fatal_platform(adapter)
        if existing is adapter:
            await self._safe_adapter_disconnect(adapter, adapter.platform)
        if not self.adapters and (not self._failed_platforms):
            self._exit_reason = adapter.fatal_error_message or 'All messaging adapters disconnected'
            if adapter.fatal_error_retryable:
                self._exit_with_failure = True
                logger.error('No connected messaging platforms remain. Shutting down gateway for service restart.')
            else:
                logger.error('No connected messaging platforms remain. Shutting down gateway cleanly.')
            await self.stop()
        elif not self.adapters and self._failed_platforms:
            logger.warning('No connected messaging platforms remain, but %d platform(s) queued for reconnection — gateway staying alive, watcher will retry in background.', len(self._failed_platforms))

    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def _active_work_count(self) -> int:
        """All agent work the gateway must expose and drain as one total."""
        return self._running_agent_count() + self._active_cron_job_count() + self._active_api_run_count()

    def _active_cron_job_count(self) -> int:
        """Count of cron jobs currently executing, from the cron scheduler's
        own in-flight tracking (``cron.scheduler._running_job_ids``).

        Cron jobs run through a standalone ``AIAgent`` on the scheduler's own
        thread pool (``cron/scheduler.py::run_job``), entirely outside
        ``self._running_agents`` — the dict every OTHER active-work check on
        this class (``_running_agent_count``, ``_drain_active_agents``) reads.
        Without this, the shutdown drain is structurally blind to in-flight
        cron work: it can report ``active_at_start=0`` and proceed straight
        to killing tool subprocesses while a cron job's terminal command is
        still running (#60432). Best-effort: returns 0 if the cron module
        can't be imported (e.g. a minimal test double for this class).
        """
        try:
            from cron.scheduler import get_running_job_ids
            return len(get_running_job_ids())
        except Exception:
            return 0

    def _active_api_run_count(self) -> int:
        """Count API-server work that is outside ``_running_agents``.

        The primary API server owns the sole HTTP listener. Secondary multiplex
        profiles cannot create an ``api_server`` adapter because it binds a port,
        so only the primary registry is a supported source of this work.
        """
        try:
            adapter = getattr(self, 'adapters', {}).get(Platform.API_SERVER)
            helper = getattr(adapter, 'active_agent_work_count', None)
            return max(0, int(helper())) if callable(helper) else 0
        except Exception:
            return 0

    def _interrupt_api_server_runs(self, reason: str) -> int:
        """Interrupt API-server agents that are not in ``_running_agents``.

        Counterpart of ``_active_api_run_count()``: that method folds
        adapter-owned API work into the shutdown drain, so this one must reach
        the same agents when the drain times out. Duck-typed on the adapter so
        an older adapter (or a minimal test double for this class) without the
        hook is simply skipped rather than raising mid-shutdown.
        """
        try:
            adapter = getattr(self, 'adapters', {}).get(Platform.API_SERVER)
            helper = getattr(adapter, 'interrupt_active_runs', None)
            return max(0, int(helper(reason))) if callable(helper) else 0
        except Exception as exc:
            logger.debug('Failed interrupting api_server runs during shutdown: %s', exc)
            return 0

    def _scale_to_zero_has_live_background_work(self) -> bool:
        """Live background work that must block a suspend (D3/F7).

        Backgrounded delegate_task / kanban / terminal(background=true) are NOT
        counted by _running_agent_count(), but suspending mid-flight loses them.
        Checks the runner's own tracked tasks + the process registry's running
        processes + any pending process-completion watchers.
        """
        if any((not t.done() for t in self._background_tasks)):
            return True
        try:
            from tools.async_delegation import active_count
            if active_count() > 0:
                return True
        except Exception:
            logger.debug('scale-to-zero async-delegation check failed', exc_info=True)
        try:
            from tools.process_registry import process_registry
            if process_registry.has_any_active():
                return True
            if process_registry.pending_watchers:
                return True
        except Exception:
            logger.debug('scale-to-zero bg-work check failed', exc_info=True)
        return False

    def _scale_to_zero_idle_timeout_seconds(self) -> float:
        from gateway.scale_to_zero import parse_idle_timeout_seconds
        raw = None
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get('gateway') if isinstance(user_cfg, dict) else None
            stz = gw.get('scale_to_zero') if isinstance(gw, dict) else None
            if isinstance(stz, dict):
                raw = stz.get('idle_timeout_minutes')
        except Exception:
            raw = None
        return parse_idle_timeout_seconds(raw)

    def _restart_loop_guard_config(self) -> tuple:
        """Return ``(max_restarts, window_seconds)`` for the auto-resume
        restart-loop breaker (#30719, defense-3), read from
        ``gateway.restart_loop_guard`` in config.yaml with the module defaults
        as fallback. ``max_restarts <= 0`` disables the breaker.
        """
        from gateway import restart_loop_guard as _rlg
        max_restarts = _rlg.DEFAULT_MAX_RESTARTS
        window_seconds = _rlg.DEFAULT_WINDOW_SECONDS
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get('gateway') if isinstance(user_cfg, dict) else None
            rlg = gw.get('restart_loop_guard') if isinstance(gw, dict) else None
            if isinstance(rlg, dict):
                if isinstance(rlg.get('max_restarts'), int):
                    max_restarts = rlg['max_restarts']
                if isinstance(rlg.get('window_seconds'), int) and rlg['window_seconds'] > 0:
                    window_seconds = rlg['window_seconds']
        except Exception:
            pass
        return (max_restarts, window_seconds)

    def _scale_to_zero_should_arm(self) -> bool:
        """Whether to start the idle watcher (D1/D11/§3.4(1))."""
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import messaging_is_relay_only_or_absent, scale_to_zero_enabled, should_arm
        try:
            platforms = [p for p, pc in self.config.platforms.items() if getattr(pc, 'enabled', False)] if self.config else []
        except Exception:
            platforms = []
        try:
            wake_url = relay_wake_url()
        except Exception:
            wake_url = None
        return should_arm(enabled=scale_to_zero_enabled(), relay_only_or_absent=messaging_is_relay_only_or_absent(platforms), wake_url=wake_url)

    def _log_scale_to_zero_not_armed_reason(self) -> None:
        """Log why the idle watcher did NOT arm — but only for an OPTED-IN instance.

        A non-opted instance (no HERMES_SCALE_TO_ZERO stamp) not arming is the normal
        case and must stay silent. When the Labs stamp IS set but the watcher still
        didn't arm, that's the surprising case worth one INFO line so "why won't it
        suspend/wake?" is a log grep, not a box-dive.
        """
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import messaging_is_relay_only_or_absent, scale_to_zero_enabled
        try:
            enabled = scale_to_zero_enabled()
            if not enabled:
                return
            try:
                active = [getattr(p, 'value', p) for p, pc in self.config.platforms.items() if getattr(pc, 'enabled', False)] if self.config else []
            except Exception:
                active = []
            relay_only = messaging_is_relay_only_or_absent(active)
            try:
                wake_url = relay_wake_url()
            except Exception:
                wake_url = None
            logger.info('scale-to-zero: NOT armed despite opt-in — relay_only_or_absent=%s (enabled platforms=%s), wake_url=%s. Need relay-only messaging + a registered wake URL.', relay_only, active or 'none', 'set' if wake_url else 'MISSING')
        except Exception:
            logger.debug('scale-to-zero: not-armed reason logging failed', exc_info=True)

    def _scale_to_zero_is_idle(self) -> bool:
        from gateway.scale_to_zero import is_idle
        return is_idle(running_agent_count=self._running_agent_count(), seconds_since_last_inbound=time.time() - self._last_inbound_at, idle_timeout_seconds=self._scale_to_zero_idle_timeout_seconds(), has_live_background_work=self._scale_to_zero_has_live_background_work())

    def _scale_to_zero_note_real_inbound(self) -> None:
        """Stamp real inbound and restore lifecycle after a dormant wake.

        The watcher marks runtime status `draining` as it quiesces the relay, but
        dormancy is not the stop/restart drain path: the process remains alive and
        should present as running once real traffic wakes it and re-enters the
        gateway. Internal completion/replay events intentionally do not call this
        helper, so they do not keep an otherwise idle gateway awake.
        """
        self._last_inbound_at = time.time()
        if getattr(self, '_scale_to_zero_cooldown_until', 0.0) > 0:
            try:
                self._update_runtime_status('running')
            except Exception:
                logger.debug('scale-to-zero: status restore failed', exc_info=True)
            self._scale_to_zero_cooldown_until = 0.0

    def _relay_adapter_for_dormancy(self):
        """Return the connected RELAY adapter, if any (the one go_dormant targets)."""
        try:
            from gateway.platforms.base import Platform
        except Exception:
            return None
        return self.adapters.get(Platform.RELAY)

    async def _scale_to_zero_watcher(self, interval: float=30.0) -> None:
        """Watch for idle and drive the relay dormant so the platform can suspend.

        Started ONLY when _scale_to_zero_should_arm() (opted in via the Labs
        HERMES_SCALE_TO_ZERO stamp + relay-only/absent messaging + a wakeUrl).
        On a sustained idle window it runs the DORMANT sequence (D12/F12/F14):
          - mark runtime status `draining` (composes with the existing state
            machine, §3.4(6); does NOT set _running=False),
          - relay adapter.go_dormant() — going_idle->ack + supervisor-preserving
            socket close (NOT disconnect(), NOT the run.py stop path),
          - deliberately NO mark_resume_pending (D13 — suspend preserves RAM).
        The process stays alive; the platform (Fly autostop:"suspend") suspends
        the now-traffic-idle machine and autostart wakes it on the wakeUrl poke,
        at which point the preserved reconnect supervisor re-dials and the
        connector drains the buffered backlog. After driving dormant we set a
        re-arm cooldown so a wake's drained backlog isn't immediately re-quiesced.
        """
        await asyncio.sleep(min(interval, 30.0))
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    return
                if time.time() < self._scale_to_zero_cooldown_until:
                    continue
                if not self._scale_to_zero_is_idle():
                    continue
                adapter = self._relay_adapter_for_dormancy()
                if adapter is None:
                    continue
                go_dormant = getattr(adapter, 'go_dormant', None)
                if not callable(go_dormant):
                    continue
                logger.info('scale-to-zero: gateway idle for >= %.0fs — going dormant (relay buffered, socket closed, awaiting platform suspend)', self._scale_to_zero_idle_timeout_seconds())
                try:
                    self._update_runtime_status('draining')
                except Exception:
                    logger.debug('scale-to-zero: status mark failed', exc_info=True)
                try:
                    result = go_dormant()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.debug('scale-to-zero: go_dormant failed', exc_info=True)
                self._scale_to_zero_cooldown_until = time.time() + max(interval, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug('scale-to-zero watcher iteration error', exc_info=True)

    def _status_action_label(self) -> str:
        return 'restart' if self._restart_requested else 'shutdown'

    def _status_action_gerund(self) -> str:
        return 'restarting' if self._restart_requested else 'shutting down'

    def _queue_during_drain_enabled(self) -> bool:
        return self._restart_requested and self._busy_input_mode in {'queue', 'steer'}

    def _enqueue_fifo(self, session_key: str, queued_event: 'MessageEvent', adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        if adapter is None:
            return
        pending_slot = getattr(adapter, '_pending_messages', None)
        if pending_slot is None:
            return
        if session_key in pending_slot:
            self._session_state(session_key).conversation.queued_events.append(queued_event)
        else:
            pending_slot[session_key] = queued_event

    def _promote_queued_event(self, session_key: str, adapter: Any, pending_event: Optional['MessageEvent']) -> Optional['MessageEvent']:
        """Promote the next overflow item after the slot was drained.

        Called at the drain site after _dequeue_pending_event consumed
        (or failed to consume) the slot.  If there's an overflow item:
          - When pending_event is None (slot was empty), return the
            overflow head as the new pending_event.
          - When pending_event already exists (slot was populated by an
            interrupt follow-up or similar), stage the overflow head in
            the slot so the NEXT recursion picks it up.
        Returns the (possibly updated) pending_event for drain to use.
        """
        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else None
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, '_pending_messages'):
            adapter._pending_messages[session_key] = next_queued
        else:
            overflow.insert(0, next_queued)
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any=None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        _q_state = self._peek_session_state(session_key)
        depth = len(_q_state.conversation.queued_events) if _q_state else 0
        if adapter is not None and session_key in getattr(adapter, '_pending_messages', {}):
            depth += 1
        return depth

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """Return True for synthetic /goal continuation turns.

        Goal continuations are normal queued user-role events, so pause/clear
        must distinguish them from real user /queue messages before removing or
        suppressing them.
        """
        text = getattr(event_or_text, 'text', event_or_text) or ''
        return str(text).startswith('[Continuing toward your standing goal]\nGoal:')

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session.

        User-issued /goal pause/clear can race with a continuation already
        queued by the judge.  Remove only synthetic goal continuations while
        preserving normal /queue and user follow-up events.
        """
        removed = 0
        pending_slot = getattr(adapter, '_pending_messages', None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1
        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else []
        if overflow:
            kept = []
            for queued_event in overflow:
                if self._is_goal_continuation_event(queued_event):
                    removed += 1
                else:
                    kept.append(queued_event)
            _q_state.conversation.queued_events = kept
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug('goal continuation: active-state recheck failed: %s', exc)
            return False

    def _update_runtime_status(self, gateway_state: Optional[str]=None, exit_reason: Optional[str]=None) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(gateway_state=gateway_state, exit_reason=exit_reason, restart_requested=self._restart_requested, active_agents=self._active_work_count())
        except Exception:
            pass

    def _persist_active_agents(self) -> None:
        """Persist the live in-flight agent count to ``gateway_state.json``.

        Called at every turn boundary (a running-agent slot is claimed or
        released) so the dashboard ``/api/status`` readout reflects in-flight
        gateway turns in near-real-time.  Without this the file is only
        rewritten on lifecycle transitions, so any ``active_agents`` read
        between transitions is stale (a turn could start and finish without the
        file ever moving).

        Deliberately passes ONLY ``active_agents`` — ``gateway_state`` and the
        other fields stay ``_UNSET`` so ``write_runtime_status``'s
        read-merge-write preserves the current lifecycle state (``running`` /
        ``draining`` / …).  Passing ``gateway_state=None`` here would clobber it.
        Best-effort: a failed status write must never disrupt a turn.
        """
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(active_agents=self._active_work_count())
        except Exception:
            pass

    def _enter_external_drain(self) -> None:
        """Begin external drain: stop accepting new turns, flip state.

        Idempotent — re-entering while already draining is a no-op beyond a
        best-effort status re-write. In-flight turns are NOT interrupted (the
        whole point is to let them finish); only NEW turns are refused.
        """
        if self._external_drain_active:
            return
        self._external_drain_active = True
        logger.info('External drain ENGAGED (.drain_request.json present) — refusing new turns; %d in-flight turn(s) will finish. Process stays up.', self._active_work_count())
        self._update_runtime_status('draining')

    def _exit_external_drain(self) -> None:
        """Cancel external drain: revert state, re-accept new turns.

        Idempotent. Only reverts to ``running`` when we are actually mid-drain
        AND not also shutting down (a real shutdown ``_draining`` must win —
        never resurrect a stopping gateway to ``running``).
        """
        if not self._external_drain_active:
            return
        self._external_drain_active = False
        if self._draining or not self._running:
            logger.info('External drain marker cleared during shutdown — not reverting to running (shutdown takes precedence).')
            return
        logger.info('External drain RELEASED (.drain_request.json removed) — re-accepting new turns; gateway_state -> running.')
        self._update_runtime_status('running')

    async def _drain_control_watcher(self, interval: float=1.0) -> None:
        """Background task: reconcile gateway accept-state with the drain marker.

        Polls ``.drain_request.json`` (presence-based contract,
        gateway/drain_control.py). Marker present -> ``_enter_external_drain``;
        marker absent -> ``_exit_external_drain``. The 1s cadence bounds the
        observe-the-marker latency the live-validation gate checks (point a).
        Reconciles once at startup. A marker stamped with a PRIOR
        instantiation epoch (one that survived a machine restart on the durable
        DUCK_AGENT_HOME volume — NS-570) is treated as absent by ``drain_requested``
        and is NOT honoured; only a marker from the current instantiation flips
        the gateway into drain. Best-effort: any tick error is logged and the
        loop continues (a transient stat() failure must not wedge the gateway).
        """
        from gateway.drain_control import drain_requested
        while self._running:
            try:
                if drain_requested():
                    self._enter_external_drain()
                    self._persist_active_agents()
                else:
                    self._exit_external_drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug('Drain-control watcher tick error: %s', exc, exc_info=True)
            await asyncio.sleep(interval)

    def _update_platform_runtime_status(self, platform: str, *, platform_state: Optional[str]=None, error_code: Optional[str]=None, error_message: Optional[str]=None) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(platform=platform, platform_state=platform_state, error_code=error_code, error_message=error_message)
        except Exception:
            pass

    def _pause_failed_platform(self, platform, *, reason: str='') -> None:
        """Mark a queued platform as paused — keep it in ``_failed_platforms``
        but stop the reconnect watcher from hammering it.

        Used by ``/platform pause <name>`` for manual operator intervention.
        Paused platforms are surfaced in ``/platform list`` and resumed with
        ``/platform resume <name>``.  Note: the reconnect watcher does NOT
        auto-pause — retryable (network/DNS) failures keep retrying at the
        backoff cap indefinitely so a transient outage self-heals without
        manual intervention.
        """
        info = getattr(self, '_failed_platforms', {}).get(platform)
        if info is None:
            return
        if info.get('paused'):
            return
        info['paused'] = True
        info['pause_reason'] = reason or 'auto-paused after repeated failures'
        info['next_retry'] = float('inf')
        try:
            self._update_platform_runtime_status(platform.value, platform_state='paused', error_code=None, error_message=info['pause_reason'])
        except Exception:
            pass
        logger.warning('%s paused after %d consecutive failures (%s) — fix the underlying issue then run `/platform resume %s` to retry, or `duck-agent gateway restart` to restart the gateway.', platform.value, info.get('attempts', 0), info['pause_reason'], platform.value)

    def _resume_paused_platform(self, platform) -> bool:
        """Unpause a platform — reset its attempt counter and schedule an
        immediate retry.  Returns True if the platform was paused and is
        now queued; False if it wasn't paused (or wasn't in the queue).
        """
        info = getattr(self, '_failed_platforms', {}).get(platform)
        if info is None:
            return False
        if not info.get('paused'):
            return False
        info['paused'] = False
        info.pop('pause_reason', None)
        info['attempts'] = 0
        info['next_retry'] = time.monotonic()
        try:
            self._update_platform_runtime_status(platform.value, platform_state='retrying', error_code=None, error_message=None)
        except Exception:
            pass
        logger.info('%s resumed — retrying on next watcher tick', platform.value)
        return True

    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.
        
        Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
        the top-level prefill_messages_file key in ~/.duck-agent/config.yaml.
        agent.prefill_messages_file is accepted as a legacy fallback.
        Relative paths are resolved from ~/.duck-agent/.
        """
        file_path = os.getenv('HERMES_PREFILL_MESSAGES_FILE', '')
        if not file_path:
            cfg = _load_gateway_runtime_config()
            file_path = str(cfg.get('prefill_messages_file', '') or '')
            if not file_path:
                file_path = str(cfg_get(cfg, 'agent', 'prefill_messages_file', default='') or '')
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning('Prefill messages file not found: %s', path)
            return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning('Prefill messages file must contain a JSON array: %s', path)
                return []
            return data
        except Exception as e:
            logger.warning('Failed to load prefill messages from %s: %s', path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var.
        
        Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then falls back to
        agent.system_prompt in ~/.duck-agent/config.yaml.
        """
        prompt = os.getenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', '')
        if prompt:
            return prompt
        cfg = _load_gateway_runtime_config()
        return str(cfg_get(cfg, 'agent', 'system_prompt', default='') or '').strip()

    def _resolve_model_for_channel(self, platform: Platform, chat_id: str, *, user_config: Optional[dict]=None, thread_id: Optional[str]=None, parent_id: Optional[str]=None) -> str:
        """Resolve model for this channel: channel_overrides else global default.

        Delegates the precedence rule to
        :func:`hermes_cli.model_switch.resolve_effective_model` (session
        override > channel override > global default) — the single owner
        shared with the API server, so the two surfaces cannot diverge
        again (see 7dd00bb47d).  This call site has no session tier: session
        /model overrides are applied later by
        ``_apply_session_model_override`` on the resolved runtime.
        """
        from hermes_cli.model_switch import resolve_effective_model
        override = None
        config = getattr(self, 'config', None)
        if config:
            override = _get_channel_override(config, platform, chat_id, thread_id=thread_id, parent_id=parent_id)
        return resolve_effective_model(None, override, _resolve_gateway_model(user_config))

    def _get_system_prompt_for_channel(self, platform: Platform, chat_id: str, *, thread_id: Optional[str]=None, parent_id: Optional[str]=None) -> str:
        """Ephemeral system prompt for this channel/thread.

        Uses ``channel_overrides`` when set, else the global gateway prompt.
        Legacy ``channel_prompts`` are applied separately via ``event.channel_prompt``
        in ``run_sync`` (adapter ``resolve_channel_prompt``), so they are not
        duplicated here.
        """
        config = getattr(self, 'config', None)
        if config:
            override = _get_channel_override(config, platform, chat_id, thread_id=thread_id, parent_id=parent_id)
            if override and override.system_prompt:
                return (override.system_prompt or '').strip()
        return getattr(self, '_ephemeral_system_prompt', None) or ''

    @staticmethod
    def _load_reasoning_config(model: str='') -> dict | None:
        """Load reasoning effort from config.yaml, respecting per-model overrides.

        Thin wrapper over the shared chokepoint
        :func:`hermes_constants.resolve_reasoning_config` (per-model override >
        global ``agent.reasoning_effort``; YAML boolean False = disabled).
        Closes #21256.

        Args:
            model: The effective model for the calling session. When empty,
                   the config's ``model.default`` is used.
        """
        from hermes_constants import resolve_reasoning_config
        cfg = _load_gateway_runtime_config()
        return resolve_reasoning_config(cfg, model)

    @staticmethod
    def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`.

        `/reasoning <level>` is session-scoped by default. `--global` may be
        supplied in any position to persist the change to config.yaml.
        """
        import shlex
        text = str(raw_args or '').strip().replace('—', '--')
        if not text:
            return ('', False)
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        persist_global = False
        value_tokens = []
        for token in tokens:
            if token == '--global':
                persist_global = True
            else:
                value_tokens.append(token)
        return (' '.join(value_tokens).strip().lower(), persist_global)

    def _resolve_session_reasoning_config(self, *, source: Optional[SessionSource]=None, session_key: Optional[str]=None, model: str='') -> dict | None:
        """Resolve reasoning effort for a session, honoring session overrides.

        Priority: session-scoped ``/reasoning --session`` override >
        per-model override (``agent.reasoning_overrides``) > global
        ``agent.reasoning_effort``. ``model`` should be the session's
        *effective* model (session ``/model`` override included) so
        per-model overrides track what the session actually runs — when
        empty, the config's ``model.default`` is used.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None
        if resolved_session_key:
            _r_state = self._peek_session_state(resolved_session_key)
            if _r_state is not None and _r_state.conversation.reasoning_override is not None:
                return _r_state.conversation.reasoning_override
        return self._load_reasoning_config(model)

    def _set_session_reasoning_override(self, session_key: str, reasoning_config: Optional[dict]) -> None:
        """Set or clear the session-scoped reasoning override."""
        if not session_key:
            return
        self._session_state(session_key).conversation.reasoning_override = None if reasoning_config is None else dict(reasoning_config)

    def _resolve_session_service_tier(self, source=None, session_key: Optional[str]=None) -> Optional[str]:
        """Resolve the effective service tier for a session.

        A session-scoped /fast override wins over the config default. The
        override dict stores "priority" or None (explicit normal), so key
        presence — not value truthiness — decides whether it applies.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None
        if resolved_session_key:
            _t_state = self._peek_session_state(resolved_session_key)
            if _t_state is not None and _t_state.conversation.service_tier_override is not _SERVICE_TIER_UNSET:
                return _t_state.conversation.service_tier_override
        return self._load_service_tier()

    def _set_session_service_tier_override(self, session_key: str, service_tier, clear: bool=False) -> None:
        """Set or clear the session-scoped /fast override.

        ``service_tier`` is "priority" or None (explicit normal). Pass
        ``clear=True`` to remove the override entirely (fall back to config).
        """
        if not session_key:
            return
        self._session_state(session_key).conversation.service_tier_override = _SERVICE_TIER_UNSET if clear else service_tier

    @staticmethod
    def _load_service_tier() -> str | None:
        """Load Priority Processing setting from config.yaml.

        Reads agent.service_tier from config.yaml. Accepted values mirror the CLI:
        "fast"/"priority"/"on" => "priority", while "normal"/"off" disables it.
        Returns None when unset or unsupported.
        """
        cfg = _load_gateway_runtime_config()
        raw = str(cfg_get(cfg, 'agent', 'service_tier', default='') or '').strip()
        value = raw.lower()
        if not value or value in {'normal', 'default', 'standard', 'off', 'none'}:
            return None
        if value in {'fast', 'priority', 'on'}:
            return 'priority'
        logger.warning("Unknown service_tier '%s', ignoring", raw)
        return None

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        cfg = _load_gateway_runtime_config()
        return is_truthy_value(cfg_get(cfg, 'display', 'show_reasoning'), default=False)

    @staticmethod
    def _load_busy_input_mode() -> str:
        """Load gateway drain-time busy-input behavior from config/env."""
        mode = os.getenv('HERMES_GATEWAY_BUSY_INPUT_MODE', '').strip().lower()
        if not mode:
            cfg = _load_gateway_runtime_config()
            mode = str(cfg_get(cfg, 'display', 'busy_input_mode', default='') or '').strip().lower()
        if mode == 'queue':
            return 'queue'
        if mode == 'steer':
            return 'steer'
        return 'interrupt'

    @staticmethod
    def _load_busy_text_mode() -> str:
        """Resolve normal busy TEXT follow-up behavior.

        ``busy_input_mode`` is the single source of truth (default
        ``interrupt``). The legacy ``busy_text_mode`` knob is honored only
        when a user explicitly set it, so existing queue setups keep
        working; new installs follow ``busy_input_mode``. Returns one of
        ``interrupt`` | ``queue`` (``steer`` is handled upstream by
        ``busy_input_mode`` and maps to non-queue text handling here).
        """
        legacy = os.getenv('HERMES_GATEWAY_BUSY_TEXT_MODE', '').strip().lower()
        if not legacy:
            cfg = _load_gateway_runtime_config()
            legacy = str(cfg_get(cfg, 'display', 'busy_text_mode', default='') or '').strip().lower()
        if legacy == 'interrupt':
            return 'interrupt'
        if legacy == 'queue':
            return 'queue'
        input_mode = GatewayRunner._load_busy_input_mode()
        return 'queue' if input_mode == 'queue' else 'interrupt'

    @staticmethod
    def _load_restart_drain_timeout() -> float:
        """Load graceful gateway restart/stop drain timeout in seconds."""
        raw = os.getenv('HERMES_RESTART_DRAIN_TIMEOUT', '').strip()
        if not raw:
            cfg = _load_gateway_runtime_config()
            raw = str(cfg_get(cfg, 'agent', 'restart_drain_timeout', default='') or '').strip()
        value = parse_restart_drain_timeout(raw)
        if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning("Invalid restart_drain_timeout '%s', using default %.0fs", raw, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)
        return value

    @staticmethod
    def _load_restart_after_turn_timeout() -> float:
        """Load in-band restart wait-for-idle timeout in seconds (#77184)."""
        env_raw = os.getenv('HERMES_RESTART_AFTER_TURN_TIMEOUT')
        if env_raw is not None and str(env_raw).strip() != '':
            raw: object = env_raw
        else:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, 'agent', 'restart_after_turn_timeout', default=None)
        value = parse_restart_after_turn_timeout(raw)
        if raw is not None and str(raw).strip() != '':
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning("Invalid restart_after_turn_timeout '%s', using default %.0fs", raw, DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT)
        return value

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var.

        Modes:
          - ``all``    — push running-output updates *and* the final message (default)
          - ``result`` — only the final completion message (regardless of exit code)
          - ``error``  — only the final message when exit code is non-zero
          - ``off``    — no watcher messages at all
        """
        mode = os.getenv('HERMES_BACKGROUND_NOTIFICATIONS', '')
        if not mode:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, 'display', 'background_process_notifications')
            if raw is False:
                mode = 'off'
            elif raw not in {None, ''}:
                mode = str(raw)
        mode = (mode or 'all').strip().lower()
        valid = {'all', 'result', 'error', 'off'}
        if mode not in valid:
            logger.warning("Unknown background_process_notifications '%s', defaulting to 'all'", mode)
            return 'all'
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        try:
            cfg = _load_gateway_runtime_config()
            return cfg.get('provider_routing', {}) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _load_fallback_model() -> list | None:
        """Load fallback provider chain from config.yaml.

        Returns the merged effective chain from ``fallback_providers`` plus any
        legacy ``fallback_model`` entries. ``fallback_providers`` stays first
        when both keys are present.
        """
        try:
            cfg = _load_gateway_runtime_config()
            fb = get_fallback_chain(cfg)
            if fb:
                return fb
        except Exception:
            pass
        return None

    def _refresh_fallback_model(self) -> list | None:
        """Re-read fallback_providers from disk for the next agent create/reuse.

        Cron already does this per job via ``get_fallback_chain``; the gateway
        previously froze ``self._fallback_model`` at process start, so a chain
        configured (or changed) after ``duck-agent gateway`` was running never
        reached messaging sessions even though the same process's cron jobs
        fell back correctly. Fixes #60955.

        A TRANSIENT read/parse failure (user mid-edit of config.yaml with a
        non-atomic write) keeps the last known-good chain instead of wiping a
        cached agent's working fallback for that turn.  Only a successful read
        that genuinely lacks the key clears the chain.
        """
        try:
            from hermes_cli.config import read_user_config_raw
            cfg_path = _hermes_home / 'config.yaml'
            if not cfg_path.exists():
                self._fallback_model = None
                return self._fallback_model
            cfg = read_user_config_raw(cfg_path)
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            try:
                from hermes_cli.config import _expand_env_vars
                expanded = _expand_env_vars(cfg)
                if isinstance(expanded, dict):
                    cfg = expanded
            except Exception:
                pass
        except Exception:
            logger.debug('fallback_providers refresh: config.yaml read failed; keeping last known-good chain', exc_info=True)
            return self._fallback_model
        self._fallback_model = get_fallback_chain(cfg) or None
        return self._fallback_model

    @staticmethod
    def _apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
        """Keep a cached agent's fallback chain aligned with current config.

        Skips rewrite while a cooldown is holding the agent on an already-
        activated fallback provider — ``restore_primary_runtime`` owns that
        turn-scoped lifecycle. When primary is active (or cooldown expired),
        replace the chain so mid-uptime ``fallback_providers`` edits take
        effect without requiring a gateway restart (#60955).
        """
        if agent is None:
            return
        new_chain = list(chain or [])
        rate_limited_until = getattr(agent, '_rate_limited_until', 0) or 0
        if getattr(agent, '_fallback_activated', False) and rate_limited_until > time.monotonic():
            return
        old_chain = list(getattr(agent, '_fallback_chain', []) or [])
        agent._fallback_chain = new_chain
        agent._fallback_model = new_chain[0] if new_chain else None
        if not getattr(agent, '_fallback_activated', False):
            agent._fallback_index = 0
        if new_chain != old_chain:
            unavailable = getattr(agent, '_unavailable_fallback_keys', None)
            if unavailable:
                unavailable.clear()

    def _snapshot_running_agents(self) -> Dict[str, Any]:
        return {session_key: agent for session_key, agent in self._running_agent_items() if agent is not _AGENT_PENDING_SENTINEL}

    def _get_max_concurrent_sessions(self) -> Optional[int]:
        """Return the configured active chat session cap, if enabled."""
        try:
            from hermes_cli.active_sessions import resolve_max_concurrent_sessions
            return resolve_max_concurrent_sessions(getattr(self, 'config', None))
        except Exception:
            return None

    def _active_session_limit_message(self, session_key: str) -> Optional[str]:
        """Return a user-facing rejection when starting a new session exceeds the cap."""
        max_sessions = self._get_max_concurrent_sessions()
        if max_sessions is None:
            return None
        if self._is_session_running(session_key):
            return None
        active_count = self._running_agent_count()
        if active_count < max_sessions:
            return None
        from hermes_cli.active_sessions import active_session_limit_message
        return active_session_limit_message(active_count, max_sessions)

    def _claim_active_session_slot(self, session_key: str, source: SessionSource) -> tuple[Any, Optional[str]]:
        """Claim a cross-process active-session slot for a new gateway turn."""
        if self._is_session_running(session_key):
            return (None, None)
        local_limit_message = self._active_session_limit_message(session_key)
        if local_limit_message is not None:
            return (None, local_limit_message)
        try:
            from hermes_cli.active_sessions import try_acquire_active_session
            platform = source.platform.value if source and source.platform else 'gateway'
            return try_acquire_active_session(session_id=session_key, surface=f'gateway:{platform}', config=getattr(self, 'config', None), metadata={'platform': platform, 'chat_id': getattr(source, 'chat_id', '') or '', 'user_id': getattr(source, 'user_id', '') or ''})
        except Exception as exc:
            logger.warning('Failed to claim active session slot: %s', exc)
            return (None, None)

    @staticmethod
    def _agent_has_active_subagents(running_agent: Any) -> bool:
        """Return True when *running_agent* is currently driving subagents
        via the ``delegate_task`` tool.

        Background (#30170): ``AIAgent.interrupt()`` cascades through the
        parent's ``_active_children`` list and calls ``interrupt()`` on
        every child synchronously, which aborts in-flight subagent work
        and produces a fallback cascade with no actionable signal.
        Demoting ``busy_input_mode='interrupt'`` to ``queue`` semantics
        whenever this helper returns True protects subagent work from
        conversational follow-ups while leaving the explicit ``/stop``
        path (which goes through ``_interrupt_and_clear_session``)
        untouched. Safe-by-default: returns False on any attribute or
        lock error so a missing/broken parent never blocks the existing
        interrupt path.
        """
        if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
            return False
        children = getattr(running_agent, '_active_children', None)
        if not isinstance(children, (list, tuple, set)):
            return False
        if not children:
            return False
        lock = getattr(running_agent, '_active_children_lock', None)
        try:
            if lock is not None:
                with lock:
                    return bool(children)
            return bool(children)
        except Exception:
            return False

    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """Return True when a compression lock is held for this session's id.

        Context compression is interrupt-protected (#23975) but gateway
        ``interrupt`` busy-input mode can still start a follow-up turn against
        the pre-rotation parent while compression is mid-flight, producing
        orphaned compression siblings (#56391). Callers demote interrupt to
        queue when this returns True.

        Both blocking sources — the ``session_store`` lock + JSON load, and the
        SQLite ``get_compression_lock_holder`` SELECT — are offloaded to a
        worker thread so a large state.db never freezes the event loop (#5).
        """
        session_store = getattr(self, 'session_store', None)
        if not session_key or session_store is None:
            return False
        try:
            session_id = await asyncio.to_thread(self._lookup_session_id_under_store_lock, session_store, session_key)
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning('Compression in-flight check failed while reading session %s; treating compression as active to avoid interrupting a possible parent-session rotation', session_key, exc_info=True)
            return True
        if not session_id:
            return False
        session_db = getattr(self, '_session_db', None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, '_db', session_db)
        try:
            holder = await asyncio.to_thread(raw_db.get_compression_lock_holder, str(session_id))
            return bool(holder)
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning('Compression in-flight check failed while reading lock holder for session %s; treating compression as active to avoid interrupting a possible parent-session rotation', session_id, exc_info=True)
            return True

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock."""
        with session_store._lock:
            session_store._ensure_loaded_locked()
            entry = session_store._entries.get(session_key)
        return getattr(entry, 'session_id', None) if entry is not None else None
    _BUSY_QUEUE_MAX_PENDING = 32

    def _queue_or_replace_pending_event(self, session_key: str, event: MessageEvent) -> None:
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return
        pending_slot = getattr(adapter, '_pending_messages', None)
        existing = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
        if existing is not None and (getattr(existing, 'message_type', None) == MessageType.PHOTO or event.message_type == MessageType.PHOTO or bool(getattr(existing, 'media_urls', None)) or bool(getattr(event, 'media_urls', None))):
            merge_pending_message_event(adapter._pending_messages, session_key, event, merge_text=event.message_type == MessageType.TEXT)
            return
        if self._queue_depth(session_key, adapter=adapter) >= self._BUSY_QUEUE_MAX_PENDING:
            logger.warning('Dropping busy-mode follow-up for session %s — pending queue at cap (%d).', session_key, self._BUSY_QUEUE_MAX_PENDING)
            return
        self._enqueue_fifo(session_key, event, adapter)

    async def _prepare_busy_steer_text(self, event: MessageEvent) -> str:
        """Return steerable text for a busy follow-up, transcribing voice first.

        Fresh and queued voice messages reach the normal inbound STT pipeline,
        but successful steer messages intentionally bypass that queue. Without
        preprocessing here, a media-only voice follow-up has an empty text
        payload and steer mode silently degrades to queue mode.

        Audio file attachments remain files; only voice-message media follows
        the automatic STT contract used by ``_prepare_inbound_message_text``.
        If transcription fails, preserve any caption and let the existing
        steer fallback handle an otherwise empty event without losing it.

        Routes through ``_transcribe_and_echo_pending_voice`` — the single
        out-of-band transcription choke point shared with the interrupt
        monitor and the pending-drain path — so the STT call is made at most
        once per platform message (cached on the event) and the transcript
        echo respects the count-based ledger.  If steering later falls back
        to queue mode, the drain path reuses the cached transcript instead of
        paying for a second STT call or re-echoing the same line.
        """
        text = (event.text or '').strip()
        if not self._pending_event_audio_paths(event):
            return text
        adapter = self._adapter_for_source(event.source)
        enriched_text, successful_transcripts = await self._transcribe_and_echo_pending_voice(event, adapter, event.source, text, log_context='Busy-steer')
        if not successful_transcripts:
            return text
        return (enriched_text or text).strip()

    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        if not self._is_user_authorized(event.source):
            logger.warning('Dropping message from unauthorized user in active session: user=%s (%s), platform=%s, session=%s', event.source.user_id, event.source.user_name, event.source.platform.value if event.source.platform else 'unknown', session_key)
            return True
        if self._draining:
            adapter = self._adapter_for_source(event.source)
            if not adapter:
                return True
            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            if self._queue_during_drain_enabled():
                self._queue_or_replace_pending_event(session_key, event)
                message = f'⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back.'
            else:
                message = f'⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now.'
            await adapter._send_with_retry(chat_id=event.source.chat_id, content=message, reply_to=reply_anchor if event.source.platform == Platform.TELEGRAM and event.source.chat_type == 'dm' and event.source.thread_id else None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id, metadata=thread_meta)
            return True
        try:
            from tools.approval import has_blocking_approval
            if has_blocking_approval(session_key):
                _raw_text = (event.text or '').strip().lower()
                _approve_words = {'approve', 'yes', 'ok', 'okay', 'confirm', 'y', '👍'}
                _deny_words = {'deny', 'no', 'reject', 'cancel', 'n', '👎'}
                _approval_handler = None
                _normalized_args = ''
                if _raw_text in _approve_words:
                    _approval_handler = self._handle_approve_command
                elif _raw_text in _deny_words:
                    _approval_handler = self._handle_deny_command
                elif _raw_text in {'always', 'approve always', 'always approve'}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = 'always'
                elif _raw_text in {'session', 'approve session', 'session approve'}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = 'session'
                if _approval_handler is not None:
                    _verb = 'approve' if _approval_handler is self._handle_approve_command else 'deny'
                    _synth = f'/{_verb}'
                    if _normalized_args:
                        _synth = f'{_synth} {_normalized_args}'
                    event.text = _synth
                    _reply = await _approval_handler(event)
                    logger.info('Approval response via plain text: session=%s verb=%s args=%r', session_key, _verb, _normalized_args)
                    _adapter = self._adapter_for_source(event.source)
                    if _adapter and _reply:
                        _text, _eph_ttl = _adapter._unwrap_ephemeral(_reply)
                        if _text:
                            _anchor = self._reply_anchor_for_event(event)
                            await _adapter._send_with_retry(chat_id=event.source.chat_id, content=_text, reply_to=_anchor, metadata=self._thread_metadata_for_source(event.source, _anchor))
                    return True
        except Exception:
            logger.warning('Plain-text approval routing failed for session %s; falling through to busy handling', session_key, exc_info=True)
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return False
        if getattr(event, 'internal', False):
            return False
        _busy_state = self._peek_session_state(session_key)
        running_agent = _busy_state.turn.agent if _busy_state else None
        effective_mode = self._busy_input_mode
        busy_text_mode = getattr(self, '_busy_text_mode', 'interrupt')
        if event.message_type == MessageType.TEXT and busy_text_mode == 'queue' and (effective_mode != 'steer'):
            return False
        demoted_for_subagents = effective_mode == 'interrupt' and self._agent_has_active_subagents(running_agent)
        if demoted_for_subagents:
            logger.info("Demoting busy_input_mode 'interrupt' to 'queue' for session %s because the running agent has active subagents (#30170)", session_key)
            effective_mode = 'queue'
        demoted_for_compression = effective_mode == 'interrupt' and await self._session_has_compression_in_flight(session_key)
        if demoted_for_compression:
            logger.info("Demoting busy_input_mode 'interrupt' to 'queue' for session %s because context compression is in flight (#56391)", session_key)
            effective_mode = 'queue'
        steered = False
        redirected = False
        if effective_mode == 'steer':
            steer_text = await self._prepare_busy_steer_text(event)
            _steer_media_urls = getattr(event, 'media_urls', None) or []
            _steer_all_voice = bool(_steer_media_urls) and len(self._pending_event_audio_paths(event)) == len(_steer_media_urls)
            can_steer = steer_text and (event.message_type == MessageType.TEXT and (not event.media_urls) and (not event.media_types) or _steer_all_voice) and (running_agent is not None) and (running_agent is not _AGENT_PENDING_SENTINEL) and hasattr(running_agent, 'steer')
            if can_steer:
                try:
                    steered = bool(running_agent.steer(steer_text))
                except Exception as exc:
                    logger.warning('Gateway steer failed for session %s: %s', session_key, exc)
                    steered = False
            if not steered:
                effective_mode = 'queue'
        elif effective_mode == 'interrupt' and event.message_type == MessageType.TEXT and (not event.media_urls) and (not event.media_types) and (running_agent is not None) and (running_agent is not _AGENT_PENDING_SENTINEL) and (getattr(running_agent, '_supports_active_turn_redirect', False) is True) and hasattr(running_agent, 'redirect'):
            try:
                redirected = bool(running_agent.redirect((event.text or '').strip()))
            except Exception as exc:
                logger.warning('Gateway redirect failed for session %s: %s', session_key, exc)
                redirected = False
        if not steered and (not redirected):
            self._queue_or_replace_pending_event(session_key, event)
        is_queue_mode = effective_mode == 'queue'
        is_steer_mode = effective_mode == 'steer'
        is_redirect_mode = effective_mode == 'interrupt' and redirected
        if effective_mode == 'interrupt' and (not redirected) and running_agent and (running_agent is not _AGENT_PENDING_SENTINEL):
            try:
                _interrupt_text = event.text
                _media_urls = getattr(event, 'media_urls', None) or []
                if self._pending_event_audio_paths(event):
                    _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(event, adapter, event.source, event.text or '', log_context='Voice-busy-interrupt')
                elif not _interrupt_text and _media_urls:
                    _interrupt_text = _build_media_placeholder(event)
                running_agent.interrupt(_interrupt_text)
            except Exception:
                pass
        busy_ack_enabled = os.environ.get('HERMES_GATEWAY_BUSY_ACK_ENABLED', 'true').lower() == 'true'
        if not busy_ack_enabled:
            logger.debug('Busy ack suppressed for session %s', session_key)
            return True
        _BUSY_ACK_COOLDOWN = 30
        now = time.time()
        last_ack = _busy_state.turn.busy_ack_ts if _busy_state else 0
        if now - last_ack < _BUSY_ACK_COOLDOWN:
            return True
        from gateway.display_config import resolve_display_setting
        platform_key = _platform_config_key(event.source.platform)
        if is_steer_mode:
            steer_ack_env = os.environ.get('HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED')
            if steer_ack_env is not None:
                steer_ack_enabled = steer_ack_env.strip().lower() in {'1', 'true', 'yes', 'on'}
            else:
                steer_ack_enabled = bool(resolve_display_setting(_load_gateway_config(), platform_key, 'busy_steer_ack_enabled', True))
            if not steer_ack_enabled:
                logger.debug('Busy steer ack suppressed for session %s', session_key)
                return True
        self._session_state(session_key).turn.busy_ack_ts = now
        status_parts = []
        busy_ack_detail_enabled = bool(resolve_display_setting(_load_gateway_config(), _platform_config_key(event.source.platform), 'busy_ack_detail', True))
        if busy_ack_detail_enabled and running_agent and (running_agent is not _AGENT_PENDING_SENTINEL):
            try:
                summary = running_agent.get_activity_summary()
                iteration = summary.get('api_call_count', 0)
                max_iter = summary.get('max_iterations', 0)
                current_tool = summary.get('current_tool')
                start_ts = _busy_state.turn.started_ts if _busy_state else 0
                if start_ts:
                    elapsed_min = int((now - start_ts) / 60)
                    if elapsed_min > 0:
                        status_parts.append(f'{elapsed_min} min elapsed')
                if max_iter:
                    status_parts.append(f'iteration {iteration}/{max_iter}')
                if current_tool:
                    status_parts.append(f'running: {current_tool}')
            except Exception:
                pass
        status_detail = f" ({', '.join(status_parts)})" if status_parts else ''
        if is_steer_mode:
            message = f'⏩ Steered into current run{status_detail}. Your message arrives after the next tool call.'
        elif is_redirect_mode:
            message = f"↪ Redirected current run{status_detail}. I'll adjust using your correction."
        elif is_queue_mode and demoted_for_subagents:
            message = f'⏳ Subagent working{status_detail} — your message is queued for when it finishes (use /stop to cancel everything).'
        elif is_queue_mode and demoted_for_compression:
            message = f'⏳ Compressing context{status_detail} — your message is queued for when it finishes (use /stop to cancel everything).'
        elif is_queue_mode:
            message = f"⏳ Queued for the next turn{status_detail}. I'll respond once the current task finishes."
        else:
            message = f"⚡ Interrupting current task{status_detail}. I'll respond to your message shortly."
        try:
            from agent.onboarding import BUSY_INPUT_FLAG, busy_input_hint_gateway, is_seen, mark_seen
            _user_cfg = _load_gateway_config()
            if not is_seen(_user_cfg, BUSY_INPUT_FLAG):
                if is_steer_mode:
                    _hint_mode = 'steer'
                elif is_queue_mode:
                    _hint_mode = 'queue'
                elif is_redirect_mode:
                    _hint_mode = 'redirect'
                else:
                    _hint_mode = 'interrupt'
                message = f'{message}\n\n{busy_input_hint_gateway(_hint_mode)}'
                mark_seen(_hermes_home / 'config.yaml', BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug('Failed to apply busy-input onboarding hint: %s', _onb_err)
        reply_anchor = self._reply_anchor_for_event(event)
        thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
        try:
            await adapter._send_with_retry(chat_id=event.source.chat_id, content=message, reply_to=reply_anchor if event.source.platform == Platform.TELEGRAM and event.source.chat_type == 'dm' and event.source.thread_id else None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id, metadata=thread_meta)
        except Exception as e:
            logger.debug('Failed to send busy-ack: %s', e)
        return True

    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:
        snapshot = self._snapshot_running_agents()
        last_active_count = self._running_agent_count()
        last_cron_count = self._active_cron_job_count()
        last_api_count = self._active_api_run_count()
        last_status_at = 0.0

        def _maybe_update_status(force: bool=False) -> None:
            nonlocal last_active_count, last_cron_count, last_api_count, last_status_at
            now = asyncio.get_running_loop().time()
            active_count = self._running_agent_count()
            cron_count = self._active_cron_job_count()
            api_count = self._active_api_run_count()
            if force or active_count != last_active_count or cron_count != last_cron_count or (api_count != last_api_count) or (now - last_status_at >= 1.0):
                self._update_runtime_status('draining')
                last_active_count = active_count
                last_cron_count = cron_count
                last_api_count = api_count
                last_status_at = now
        if not self._running_agents and last_cron_count == 0 and (last_api_count == 0):
            _maybe_update_status(force=True)
            return (snapshot, False)
        _maybe_update_status(force=True)
        if timeout <= 0:
            return (snapshot, True)
        deadline = asyncio.get_running_loop().time() + timeout
        while (len(self._running_agents) or self._active_cron_job_count() or self._active_api_run_count()) and asyncio.get_running_loop().time() < deadline:
            _maybe_update_status()
            await asyncio.sleep(0.1)
        timed_out = bool(len(self._running_agents)) or bool(self._active_cron_job_count()) or bool(self._active_api_run_count())
        _maybe_update_status(force=True)
        return (snapshot, timed_out)

    def _interrupt_running_agents(self, reason: str) -> None:
        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                request_hard_interrupt(agent, reason)
                logger.debug('Interrupted running agent for session %s during shutdown', session_key)
            except Exception as e:
                logger.debug('Failed interrupting agent during shutdown: %s', e)
        interrupted_api = self._interrupt_api_server_runs(reason)
        if interrupted_api:
            logger.debug('Interrupted %d api_server run(s) during shutdown', interrupted_api)

    async def _notify_active_sessions_of_shutdown(self) -> None:
        """Send shutdown/restart notifications to active chats and home channels.

        Called at the very start of stop() — adapters are still connected so
        messages can be delivered. Best-effort: individual send failures are
        logged and swallowed so they never block the shutdown sequence.
        """
        active = self._snapshot_running_agents()
        restart_source = self._restart_command_source if self._restart_requested else None
        action = 'restarting' if self._restart_requested else 'shutting down'
        hint = "Your current task will be interrupted. Send any message after restart and I'll try to resume where you left off." if self._restart_requested else 'Your current task will be interrupted.'
        msg = f'⚠️ Gateway {action} — {hint}'
        notified: set[tuple[str, str, Optional[str]]] = set()
        for session_key in active:
            source = None
            try:
                if getattr(self, 'session_store', None) is not None:
                    await self.async_session_store._ensure_loaded()
                    entry = self.session_store._entries.get(session_key)
                    source = getattr(entry, 'origin', None) if entry else None
            except Exception as e:
                logger.debug('Failed to load session origin for shutdown notification %s: %s', session_key, e)
            if source is None:
                source = self._get_cached_session_source(session_key)
            if source is not None:
                platform_str = source.platform.value
                chat_id = str(source.chat_id)
                thread_id = source.thread_id
            else:
                _parsed = _parse_session_key(session_key)
                if not _parsed:
                    continue
                platform_str = _parsed['platform']
                chat_id = _parsed['chat_id']
                thread_id = _parsed.get('thread_id')
            dedup_key = (platform_str, chat_id, str(thread_id) if thread_id else None)
            if dedup_key in notified:
                continue
            try:
                platform = Platform(platform_str)
                adapter = self.adapters.get(platform)
                if not adapter:
                    continue
                platform_cfg = self.config.platforms.get(platform)
                if platform_cfg is not None and (not platform_cfg.gateway_restart_notification):
                    logger.info('Shutdown notification suppressed for active session: %s has gateway_restart_notification=false', platform_str)
                    continue
                reply_to_message_id = getattr(source, 'message_id', None) if source is not None else None
                if reply_to_message_id is None and restart_source is not None:
                    try:
                        restart_platform = restart_source.platform.value
                        restart_chat_id = str(restart_source.chat_id)
                        restart_thread_id = str(restart_source.thread_id) if restart_source.thread_id else None
                        if (restart_platform, restart_chat_id, restart_thread_id) == dedup_key:
                            reply_to_message_id = getattr(restart_source, 'message_id', None)
                    except Exception:
                        pass
                metadata = self._thread_metadata_for_target(platform, chat_id, thread_id, chat_type=getattr(source, 'chat_type', None) if source is not None else None, reply_to_message_id=reply_to_message_id, adapter=adapter)
                result = await adapter.send(chat_id, msg, metadata=metadata)
                if result is not None and getattr(result, 'success', True) is False:
                    logger.debug('Failed to send shutdown notification to %s:%s: %s', platform_str, chat_id, getattr(result, 'error', 'send returned success=False'))
                    continue
                notified.add(dedup_key)
                logger.info('Sent shutdown notification to active chat %s:%s', platform_str, chat_id)
            except Exception as e:
                logger.debug('Failed to send shutdown notification to %s:%s: %s', platform_str, chat_id, e)
        if self._restart_requested and restart_source is not None:
            logger.debug('Skipping home-channel shutdown notifications for in-chat restart')
            return
        try:
            from gateway.drain_control import drain_notification_suppressed
            if drain_notification_suppressed():
                logger.info('Home-channel shutdown broadcast suppressed by drain marker (suppress_notification=true)')
                return
        except Exception as e:
            logger.debug('drain_notification_suppressed check failed: %s', e)
        for platform, adapter in list(self.adapters.items()):
            home = self.config.get_home_channel(platform)
            if not home or not home.chat_id:
                continue
            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and (not platform_cfg.gateway_restart_notification):
                logger.info('Shutdown notification suppressed for home channel: %s has gateway_restart_notification=false', platform.value)
                continue
            dedup_key = (platform.value, str(home.chat_id), str(home.thread_id) if home.thread_id else None)
            if dedup_key in notified:
                continue
            try:
                metadata = self._thread_metadata_for_target(platform, home.chat_id, home.thread_id, adapter=adapter)
                if metadata:
                    result = await adapter.send(str(home.chat_id), msg, metadata=metadata)
                else:
                    result = await adapter.send(str(home.chat_id), msg)
                if result is not None and getattr(result, 'success', True) is False:
                    logger.debug('Failed to send shutdown notification to home channel %s:%s: %s', platform.value, home.chat_id, getattr(result, 'error', 'send returned success=False'))
                    continue
                notified.add(dedup_key)
                logger.info('Sent shutdown notification to home channel %s:%s', platform.value, home.chat_id)
            except Exception as e:
                logger.debug('Failed to send shutdown notification to home channel %s:%s: %s', platform.value, home.chat_id, e)

    async def _finalize_shutdown_agents(self, active_agents: Dict[str, Any]) -> None:
        for agent in active_agents.values():
            try:
                _flush = getattr(agent, '_flush_messages_to_session_db', None)
                _session_messages = getattr(agent, '_session_messages', None)
                if callable(_flush) and isinstance(_session_messages, list) and _session_messages:
                    _strip = getattr(agent, '_drop_trailing_empty_response_scaffolding', None)
                    if callable(_strip):
                        try:
                            _strip(_session_messages)
                        except Exception:
                            pass
                    try:
                        _flush(_session_messages)
                    except Exception as _flush_err:
                        logger.warning('Shutdown transcript flush failed (%s); preserving %d in-memory message(s) to recovery snapshot', _flush_err, len(_session_messages))
                        from gateway.shutdown_flush import flush_agent_history_to_file
                        flush_agent_history_to_file(getattr(agent, 'session_id', None), _session_messages)
            except Exception as _e:
                logger.debug('Shutdown transcript flush failed: %s', _e)
            try:
                from hermes_cli.lifecycle import finalize_session
                finalize_session(session_id=getattr(agent, 'session_id', None), platform='gateway', reason='shutdown')
            except Exception:
                pass
            await self._cleanup_agent_resources_off_loop(agent, context='shutdown finalize')

    def _should_emit_long_running_notification(self, session_key: Optional[str], agent: Any, executor_task: Optional[Any]) -> bool:
        """Only emit the heartbeat while this task still owns the live run.

        Guards against a stale ``running: delegate_task`` heartbeat outliving the
        run that started it: stop once the executor finishes, the agent is gone,
        or the session key has been rebound to a different live agent (e.g. the
        user sent ``/new`` and a fresh agent took the slot mid-run, #12029).
        """
        if agent is None:
            return False
        if executor_task is not None and executor_task.done():
            return False
        if session_key:
            _hb_state = self._peek_session_state(session_key)
            if (_hb_state.turn.agent if _hb_state else None) is not agent:
                return False
        return True
    _CLEANUP_TIMEOUT_S = 30.0

    def _defer_agent_cleanup_until_future_done(self, future: asyncio.Future, agent: Any, *, context: str) -> None:
        """Clean up ``agent`` only after its executor future has finished.

        A timed-out executor call keeps running in its worker thread. Closing
        the agent before that thread exits can tear down clients or providers
        it is still using. Keep a strong task reference and wait for the real
        future before invoking the normal bounded, off-loop cleanup path.
        """

        async def _cleanup_when_done() -> None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug('Deferred agent worker%s finished with an error: %s', f' ({context})' if context else '', exc)
            await self._cleanup_agent_resources_off_loop(agent, context=context)
        task = asyncio.create_task(_cleanup_when_done())
        tasks = getattr(self, '_deferred_agent_cleanup_tasks', None)
        if tasks is None:
            tasks = set()
            self._deferred_agent_cleanup_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _cleanup_agent_resources_off_loop(self, agent: Any, *, context: str='') -> None:
        """Run _cleanup_agent_resources in a worker thread with a bounded wait.

        Safe to await from coroutines on the gateway event loop: a slow or
        wedged teardown (memory provider IO, subprocess close) can no longer
        block message processing. On timeout the await is cancelled and the
        worker thread is left to finish (or leak) on its own — the caller
        proceeds regardless, exactly as the /new reset path does (#35994).
        """
        if agent is None:
            return
        if context.startswith('shutdown') or context == 'session expiry':
            try:
                agent._end_session_on_close = False
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._run_in_executor_with_context(self._cleanup_agent_resources, agent), timeout=self._CLEANUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning('Agent resource cleanup%s exceeded %ss; proceeding without blocking the event loop (the worker thread is left to finish on its own). (#53175)', f' ({context})' if context else '', self._CLEANUP_TIMEOUT_S)
        except Exception as cleanup_exc:
            logger.warning('Agent resource cleanup%s failed: %s (#53175)', f' ({context})' if context else '', cleanup_exc)

    def _cleanup_agent_resources(self, agent: Any) -> None:
        """Best-effort cleanup for temporary or cached agent instances."""
        if agent is None:
            return
        try:
            if hasattr(agent, 'shutdown_memory_provider'):
                _mm = getattr(agent, '_memory_manager', None)
                if _mm is not None and hasattr(_mm, 'flush_pending'):
                    try:
                        _mm.flush_pending(timeout=10)
                    except Exception:
                        pass
                session_messages = getattr(agent, '_session_messages', None)
                if isinstance(session_messages, list):
                    agent.shutdown_memory_provider(session_messages)
                else:
                    agent.shutdown_memory_provider()
        except Exception:
            pass
        try:
            if hasattr(agent, 'close'):
                agent.close()
        except Exception:
            pass
        try:
            from agent.auxiliary_client import cleanup_stale_async_clients
            cleanup_stale_async_clients()
        except Exception:
            pass
    _STUCK_LOOP_THRESHOLD = 3
    _STUCK_LOOP_FILE = '.restart_failure_counts'

    def _increment_restart_failure_counts(self, active_session_keys: set) -> None:
        """Increment restart-failure counters for sessions active at shutdown.

        Persists to a JSON file so counters survive across restarts.
        Sessions NOT in active_session_keys are removed (they completed
        successfully, so the loop is broken).
        """
        import json
        path = _hermes_home / self._STUCK_LOOP_FILE
        try:
            counts = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except Exception:
            counts = {}
        new_counts = {}
        for key in active_session_keys:
            new_counts[key] = counts.get(key, 0) + 1
        try:
            atomic_json_write(path, new_counts, indent=None)
        except Exception:
            pass

    def _suspend_stuck_loop_sessions(self) -> int:
        """Suspend sessions that have been active across too many restarts.

        Returns the number of sessions suspended.  Called on gateway startup
        AFTER suspend_recently_active() to catch the stuck-loop pattern:
        session loads → agent gets stuck → gateway restarts → repeat.
        """
        import json
        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return 0
        try:
            counts = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return 0
        suspended = 0
        stuck_keys = [k for k, v in counts.items() if v >= self._STUCK_LOOP_THRESHOLD]
        for session_key in stuck_keys:
            try:
                entry = self.session_store._entries.get(session_key)
                if entry and (not entry.suspended):
                    entry.suspended = True
                    suspended += 1
                    logger.warning('Auto-suspended stuck session %s (active across %d consecutive restarts — likely a stuck loop)', session_key, counts[session_key])
            except Exception:
                pass
        if suspended:
            try:
                self.session_store._save()
            except Exception:
                pass
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return suspended

    def _clear_restart_failure_count(self, session_key: str) -> None:
        """Clear the restart-failure counter for a session that completed OK.

        Called after a successful agent turn to signal the loop is broken.
        """
        import json
        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return
        try:
            counts = json.loads(path.read_text(encoding='utf-8'))
            if session_key in counts:
                del counts[session_key]
                if counts:
                    atomic_json_write(path, counts, indent=None)
                else:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _launch_detached_restart_command(self) -> None:
        import shutil
        import subprocess
        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            logger.error('Could not locate duck-agent binary for detached /restart')
            return
        if self._detached_restart_helper_started:
            return
        self._detached_restart_helper_started = True
        current_pid = os.getpid()
        restart_after_s = max(float(getattr(self, '_restart_drain_timeout', 0.0) or 0.0) + 5.0, 5.0)
        if sys.platform == 'win32':
            import textwrap
            from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway, windows_detach_popen_kwargs
            cmd_argv = [*hermes_cmd, 'gateway', 'restart']
            watcher = textwrap.dedent("\n                import os, subprocess, sys, time\n                from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway\n                pid = int(sys.argv[1])\n                restart_after_s = float(sys.argv[2])\n                cmd = sys.argv[3:]\n                deadline = time.monotonic() + restart_after_s\n\n                def _alive(p):\n                    # On Windows, os.kill(pid, 0) is NOT a no-op — it maps to\n                    # GenerateConsoleCtrlEvent(0, pid) (bpo-14484). Use the\n                    # Win32 handle-based existence check instead.\n                    if os.name == 'nt':\n                        import ctypes\n                        k32 = ctypes.windll.kernel32\n                        k32.OpenProcess.restype = ctypes.c_void_p\n                        k32.WaitForSingleObject.restype = ctypes.c_uint\n                        k32.GetLastError.restype = ctypes.c_uint\n                        h = k32.OpenProcess(0x1000 | 0x100000, False, int(p))\n                        if not h:\n                            return k32.GetLastError() != 87\n                        try:\n                            return k32.WaitForSingleObject(h, 0) == 0x102\n                        finally:\n                            k32.CloseHandle(h)\n                    try:\n                        os.kill(int(p), 0)\n                        return True\n                    except ProcessLookupError:\n                        return False\n                    except PermissionError:\n                        return True\n                    except OSError:\n                        return False\n\n                while time.monotonic() < deadline:\n                    if not _alive(pid):\n                        break\n                    time.sleep(0.2)\n                subprocess.Popen(\n                    cmd,\n                    stdout=subprocess.DEVNULL,\n                    stderr=subprocess.DEVNULL,\n                    creationflags=windows_detach_flags_without_breakaway(),\n                )\n                ").strip()
            from tools.environments.local import build_subprocess_env
            watcher_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
            watcher_env.pop('_HERMES_GATEWAY', None)
            project_root = Path(__file__).resolve().parent.parent
            watcher_python = sys.executable
            venv_dir = Path(watcher_env.get('VIRTUAL_ENV') or project_root / 'venv')
            site_packages = venv_dir / 'Lib' / 'site-packages'
            if site_packages.exists():
                watcher_env['VIRTUAL_ENV'] = str(venv_dir)
                pythonpath = [str(project_root), str(site_packages)]
                if watcher_env.get('PYTHONPATH'):
                    pythonpath.append(watcher_env['PYTHONPATH'])
                watcher_env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(pythonpath))
            watcher_argv = [watcher_python, '-c', watcher, str(current_pid), str(restart_after_s), *cmd_argv]
            try:
                subprocess.Popen(watcher_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=watcher_env, **windows_detach_popen_kwargs())
            except OSError:
                try:
                    subprocess.Popen(watcher_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=watcher_env, creationflags=windows_detach_flags_without_breakaway())
                except OSError as exc:
                    winerror = getattr(exc, 'winerror', None)
                    error_code = winerror if winerror is not None else exc.errno
                    error_field = 'winerror' if winerror is not None else 'errno'
                    logger.warning('Detached restart watcher was not started after the no-breakaway retry (%s; %s=%r). The gateway will not be respawned by this restart attempt.', os.path.basename(watcher_python), error_field, error_code)
            return
        cmd = ' '.join((shlex.quote(part) for part in hermes_cmd))
        shell_cmd = f'deadline=$(( $(date +%s) + {int(restart_after_s)} )); while kill -0 {current_pid} 2>/dev/null && [ $(date +%s) -lt $deadline ]; do sleep 0.2; done; {cmd} gateway restart'
        from tools.environments.local import build_subprocess_env
        watcher_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
        watcher_env.pop('_HERMES_GATEWAY', None)
        setsid_bin = shutil.which('setsid')
        if setsid_bin:
            subprocess.Popen([setsid_bin, 'bash', '-lc', shell_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=watcher_env, start_new_session=True)
        else:
            subprocess.Popen(['bash', '-lc', shell_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=watcher_env, start_new_session=True)

    def _launch_systemd_restart_shortcut(self) -> None:
        """Best-effort helper to bypass systemd's automatic restart delay.

        For planned in-chat restarts, the gateway exits cleanly so systemd does
        not record a failure.  However, units with RestartSteps still count
        automatic restarts and can delay repeated /restart tests.  A transient
        user service survives our cgroup teardown and explicitly starts the
        gateway as soon as this PID exits, while the unit keeps its normal
        backoff for real crash loops.
        """
        if sys.platform != 'linux' or not os.environ.get('INVOCATION_ID'):
            return
        try:
            import shutil
            import subprocess
            systemd_run = shutil.which('systemd-run')
            systemctl = shutil.which('systemctl')
            if not systemd_run or not systemctl:
                return
            try:
                from hermes_cli.gateway import get_service_name
                service_name = get_service_name()
            except Exception:
                service_name = 'duck-agent-gateway'
            current_pid = os.getpid()

            def _query_pid(scope_flags):
                try:
                    out = subprocess.run([systemctl, *scope_flags, 'show', service_name, '--property=MainPID', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=2)
                    return (out.stdout or '').strip()
                except Exception:
                    return ''
            system_pid = _query_pid([])
            user_pid = _query_pid(['--user'])
            if str(current_pid) == system_pid:
                scope_flags = []
                systemctl_scope = 'systemctl'
            elif str(current_pid) == user_pid:
                scope_flags = ['--user']
                systemctl_scope = 'systemctl --user'
            else:
                return
            service_arg = shlex.quote(service_name)
            shell_cmd = f'while kill -0 {current_pid} 2>/dev/null; do sleep 0.2; done; {systemctl_scope} reset-failed {service_arg}; {systemctl_scope} restart {service_arg}'
            unit_name = f'{service_name}-planned-restart-{current_pid}'.replace('.', '-')
            subprocess.Popen([systemd_run, *scope_flags, '--collect', '--unit', unit_name, '/bin/sh', '-lc', shell_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            logger.info('Launched systemd planned-restart helper for %s (pid=%s, scope=%s)', service_name, current_pid, 'user' if scope_flags else 'system')
        except Exception as e:
            logger.debug('Failed to launch systemd planned-restart helper: %s', e)

    async def _await_active_work_before_restart(self) -> bool:
        """Wait for in-flight work to finish before entering ``stop()``.

        In-band restart used to call ``stop()`` immediately, which folded the
        requesting turn into the drain wait set and force-interrupted it at
        ``restart_drain_timeout`` (#77184). Instead we refuse new turns and
        wait here for active agents/cron/api work to reach zero, then let
        ``stop()`` run against an idle gateway (drain is instant).

        Returns True when work drained to zero, False when the safety cap
        elapsed with work still active (caller proceeds to ``stop()``, which
        may then interrupt remaining runs under ``restart_drain_timeout``).
        """
        active = self._active_work_count()
        if active <= 0:
            return True
        timeout = float(getattr(self, '_restart_after_turn_timeout', 0.0) or 0.0)
        if timeout <= 0:
            logger.info('Restart requested with %d active work unit(s); restart_after_turn_timeout=0 — entering stop()/drain immediately', active)
            return False
        logger.info('Restart requested with %d active work unit(s); deferring stop() until they finish (cap=%.0fs) so in-flight turns are not amputated (#77184)', active, timeout)
        try:
            self._update_runtime_status('draining')
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_status_at = 0.0
        while self._active_work_count() > 0:
            now = loop.time()
            if now >= deadline:
                logger.warning('Restart after-turn wait timed out after %.0fs with %d still active; proceeding to stop()/drain which may interrupt remaining work (#77184)', timeout, self._active_work_count())
                return False
            if now - last_status_at >= 30.0:
                logger.info('Restart deferred: waiting on %d active work unit(s) (%.0fs remaining before force drain)', self._active_work_count(), deadline - now)
                try:
                    self._update_runtime_status('draining')
                except Exception:
                    pass
                last_status_at = now
            await asyncio.sleep(0.1)
        logger.info('Restart deferred wait complete — active work drained; proceeding to stop()')
        return True

    def request_restart(self, *, detached: bool=False, via_service: bool=False) -> bool:
        if self._restart_task_started:
            return False
        self._restart_requested = True
        self._restart_detached = detached
        self._restart_via_service = via_service
        self._restart_task_started = True
        self._draining = True

        async def _run_restart() -> None:
            await self._await_active_work_before_restart()
            if detached:
                try:
                    await self._launch_detached_restart_command()
                except Exception as e:
                    logger.error('Failed to launch detached gateway restart helper: %s', e)
            await asyncio.sleep(0.05)
            await self.stop(restart=True, detached_restart=detached, service_restart=via_service)
        self._restart_task = asyncio.create_task(_run_restart())
        return True
    _AUTO_RESUME_REASONS = frozenset({'restart_timeout', 'shutdown_timeout', 'restart_interrupted'})

    async def _run_startup_resume_event(self, adapter: BasePlatformAdapter, event: MessageEvent, session_key: str) -> None:
        """Dispatch one synthetic startup resume and wait for its agent turn.

        ``BasePlatformAdapter.handle_message()`` returns after it installs the
        adapter-level guard and spawns the background processing task.  Startup
        restore needs a stronger boundary: inbound messages must stay queued
        until the resumed agent turn itself has finished, otherwise a user
        message can race the restore turn immediately after ``handle_message``
        returns.
        """
        try:
            await adapter.handle_message(event)
            session_tasks = getattr(adapter, '_session_tasks', {})
            task = session_tasks.get(session_key) if isinstance(session_tasks, dict) else None
            if task is not None:
                await asyncio.shield(task)
        finally:
            _pre_state = self._peek_session_state(session_key)
            if (_pre_state.turn.agent if _pre_state else None) is _AGENT_PENDING_SENTINEL:
                self._release_running_agent_state(session_key)

    def _queue_startup_restore_event(self, event: MessageEvent) -> None:
        queue = getattr(self, '_startup_restore_queue', None)
        if queue is None:
            queue = []
            self._startup_restore_queue = queue
        queue.append(event)
        try:
            source = event.source
            logger.info('Queued inbound message during gateway startup restore: platform=%s chat=%s', source.platform.value if source and source.platform else 'unknown', source.chat_id if source else 'unknown')
        except Exception:
            pass

    async def _drain_startup_restore_queue(self) -> int:
        """Replay inbound messages queued while startup auto-resume ran."""
        drained = 0
        queue = getattr(self, '_startup_restore_queue', None)
        if queue is None:
            return 0
        while queue:
            event = queue.pop(0)
            source = getattr(event, 'source', None)
            adapter = self._adapter_for_source(source)
            if adapter is None:
                logger.debug('Dropping startup-restore queued message: adapter unavailable for %s', getattr(getattr(source, 'platform', None), 'value', None))
                continue
            try:
                setattr(event, '_hermes_startup_restore_replay', True)
            except Exception:
                pass
            await adapter.handle_message(event)
            drained += 1
        return drained

    async def _finish_startup_restore(self) -> None:
        """Wait (BOUNDED) for startup auto-resume, then release + drain inbound.

        The wait is bounded by ``_startup_restore_drain_timeout_secs`` so that
        a single pathologically long boot-resume turn cannot hold the inbound
        gate shut for every channel.  On timeout we release the gate and let
        the still-running resume turn(s) finish in the background — they are
        NOT cancelled.  This is safe because duplicate-agent protection does
        not depend on the wait: ``_schedule_resume_pending_sessions`` claims
        each session's ``_running_agents`` slot SYNCHRONOUSLY before this gate
        runs, so any inbound message drained while a resume turn is still in
        flight queues behind that slot instead of spawning a second agent.
        """
        tasks = list(getattr(self, '_startup_restore_tasks', []) or [])
        if tasks:
            timeout = _startup_restore_drain_timeout_secs()
            if timeout > 0:
                done, pending = await asyncio.wait(tasks, timeout=timeout)
                if pending:
                    logger.warning('Startup-restore gate released after %.0fs with %d boot auto-resume turn(s) still running; draining inbound queue now (resume slots already claimed, so no duplicate agents). Slow turn(s) continue in the background.', timeout, len(pending))
                    for task in pending:
                        task.add_done_callback(self._log_background_resume_result)
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
                done = set(tasks)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.debug('startup auto-resume task failed', exc_info=(type(exc), exc, exc.__traceback__))
        self._startup_restore_tasks = []
        drained = await self._drain_startup_restore_queue()
        self._startup_restore_in_progress = False
        if drained:
            logger.info('Drained %d inbound message(s) queued during startup restore', drained)

    @staticmethod
    def _log_background_resume_result(task: 'asyncio.Task') -> None:
        """Done-callback for a boot-resume turn that outlived the
        startup-restore gate.  Logs a late failure that would otherwise be
        swallowed once the task is discarded from ``_background_tasks``.
        Cancellation is expected (shutdown) and is not an error."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug('background startup auto-resume task failed after gate release', exc_info=(type(exc), exc, exc.__traceback__))

    async def _redeliver_pending_obligations(self) -> int:
        """Redeliver final responses recorded in the delivery ledger by a
        previous (now dead) gateway process.

        Runs at startup BEFORE ``_schedule_resume_pending_sessions``. A
        session with a recoverable obligation already produced its answer —
        the turn completed and only delivery is owed — so this method sends
        the stored text and clears ``resume_pending`` for that session,
        preventing the resume path from re-running (and re-paying for) a
        turn whose output we hold.

        Crash-ambiguity contract (see gateway/delivery_ledger.py):
        rows that were mid-send or previously rejected carry a visible
        recovered-reply marker so a possible duplicate is labeled, never
        silent. Returns the number of redeliveries attempted.
        """
        try:
            from gateway.delivery_ledger import RECOVERED_MARKER, ledger_enabled, mark_delivered, mark_failed, sweep_recoverable
            if not await asyncio.to_thread(ledger_enabled):
                return 0
            _deliverable = {getattr(p, 'value', str(p)) for p in self.adapters}
            claimed = await asyncio.to_thread(sweep_recoverable, None, deliverable_platforms=_deliverable)
        except Exception:
            logger.debug('delivery ledger sweep failed', exc_info=True)
            return 0
        if not claimed:
            return 0
        redelivered = 0
        for row in claimed:
            try:
                platform = Platform(row['platform'])
            except Exception:
                logger.debug('obligation %s: unknown platform %r', row['obligation_id'], row.get('platform'))
                continue
            adapter = self.adapters.get(platform)
            if adapter is None:
                continue
            content = row['content']
            if row.get('needs_marker'):
                content = RECOVERED_MARKER + content
            metadata = {'thread_id': row['thread_id']} if row.get('thread_id') else None
            try:
                result = await adapter.send(chat_id=row['chat_id'], content=content, metadata=metadata)
            except Exception as send_err:
                logger.warning('obligation %s: redelivery send raised: %s', row['obligation_id'], send_err)
                result = None
            try:
                if result is not None and getattr(result, 'success', False):
                    await asyncio.to_thread(mark_delivered, row['obligation_id'])
                    redelivered += 1
                    logger.info('Redelivered recovered final response to %s:%s (obligation %s, attempt %d)', row['platform'], row['chat_id'], row['obligation_id'], row['attempts'])
                else:
                    await asyncio.to_thread(mark_failed, row['obligation_id'], str(getattr(result, 'error', '') or 'send failed'))
            except Exception:
                logger.debug('delivery ledger update failed', exc_info=True)
            session_key = row.get('session_key') or ''
            if session_key:
                try:
                    await self.async_session_store.clear_resume_pending(session_key)
                except Exception:
                    logger.debug('clear_resume_pending failed for %s', session_key, exc_info=True)
        return redelivered

    def _schedule_resume_pending_sessions(self, platform=None) -> int:
        """Auto-continue fresh restart-interrupted sessions after startup.

        ``resume_pending`` already preserves the transcript AND the existing
        ``_is_resume_pending`` branch in ``_handle_message_with_agent``
        injects a reason-aware recovery system note on the next turn.  This
        method closes the UX gap by synthesizing that next turn once
        adapters are back online — the event text is empty so the existing
        injection path owns the wording and we never double up.

        Adapters that are not yet ready (adapter missing from
        ``self.adapters``) are skipped silently; their sessions stay
        ``resume_pending`` and will auto-resume on the next real user
        message, or when the platform reconnects — the reconnect watcher
        calls this again scoped to that ``platform``.

        ``platform`` (a ``Platform``) restricts the pass to sessions that
        originated on that platform.  The reconnect path passes it so a
        platform coming back online retries only its own sessions and never
        re-touches another platform's in-flight recoveries.  Sessions whose
        agent is already running are skipped regardless, so a session
        scheduled at startup is never resumed a second time.
        """
        window = _auto_continue_freshness_window()
        try:
            with self.session_store._lock:
                self.session_store._ensure_loaded_locked()
                candidates = [entry for entry in self.session_store._entries.values() if entry.resume_pending and (not entry.suspended) and (entry.origin is not None) and (entry.resume_reason in self._AUTO_RESUME_REASONS) and (platform is None or entry.origin.platform == platform)]
        except Exception as exc:
            logger.warning('Failed to enumerate resume-pending sessions: %s', exc)
            return 0
        if candidates:
            try:
                from gateway import restart_loop_guard as _rlg
                _max_restarts, _window = self._restart_loop_guard_config()
                if _rlg.check_and_record(_max_restarts, _window):
                    return 0
            except Exception as exc:
                logger.debug('Restart-loop guard check skipped: %s', exc)
        now = datetime.now()
        scheduled = 0
        for entry in candidates:
            marker = entry.last_resume_marked_at or entry.updated_at
            if marker is not None and (now - marker).total_seconds() > window:
                continue
            if self._is_session_running(entry.session_key):
                continue
            source = entry.origin
            adapter = self._adapter_for_source(source)
            if adapter is None:
                logger.debug('Skipping auto-resume for %s: adapter not ready for %s', entry.session_key, getattr(source.platform, 'value', source.platform))
                continue
            try:
                if not self._is_user_authorized(source):
                    logger.warning('Skipping auto-resume for %s: session owner is no longer authorized under the current allowlist', entry.session_key)
                    continue
            except Exception as exc:
                logger.warning('Skipping auto-resume for %s: authorization check failed: %s', entry.session_key, exc)
                continue
            _resume_state = self._session_state(entry.session_key)
            _resume_state.turn.agent = _AGENT_PENDING_SENTINEL
            _resume_state.turn.started_ts = time.time()
            self._persist_active_agents()
            event = MessageEvent(text='', message_type=MessageType.TEXT, source=source, internal=True)
            task = asyncio.create_task(self._run_startup_resume_event(adapter, event, entry.session_key))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            if getattr(self, '_startup_restore_in_progress', False):
                tasks = getattr(self, '_startup_restore_tasks', None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
        if scheduled:
            logger.info('Scheduled auto-resume for %d restart-interrupted session(s)', scheduled)
        return scheduled

    def _startup_should_abort(self) -> bool:
        return self._restart_requested or self._draining or self._shutdown_event.is_set()

    async def _abort_startup_if_shutdown_requested(self, adapter: Optional[BasePlatformAdapter]=None, platform: Optional[Platform]=None) -> bool:
        """Clean up and exit startup when restart/shutdown begins mid-startup."""
        if not self._startup_should_abort():
            return False
        if adapter is not None and platform is not None:
            try:
                await adapter.cancel_background_tasks()
            except Exception as e:
                logger.debug('✗ %s background-task cancel error: %s', platform.value, e)
            await self._safe_adapter_disconnect(adapter, platform)
        stop_task = self._stop_task
        current_task = asyncio.current_task()
        if stop_task is not None and stop_task is not current_task:
            await stop_task
        elif not self._shutdown_event.is_set():
            await self.stop(restart=self._restart_requested, detached_restart=self._restart_detached, service_restart=self._restart_via_service)
        return True

    def _start_loop_liveness_guards(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arm the selector floor and out-of-loop watchdog before adapters.

        Disabled entirely with ``gateway.loop_watchdog: false`` in config.yaml
        (no env override — config-only knob, #69089).
        """
        config = getattr(self, 'config', None)
        if config is not None and (not getattr(config, 'loop_watchdog', True)):
            return
        if getattr(self, '_loop_floor_timer_handle', None) is None:
            try:
                self._loop_floor_timer_handle = _arm_loop_floor_timer(loop)
            except Exception:
                logger.debug('Failed to arm gateway loop floor timer', exc_info=True)
        watchdog = getattr(self, '_loop_liveness_watchdog', None)
        if watchdog is None or not watchdog.is_alive():
            try:
                self._loop_liveness_watchdog = start_loop_liveness_watchdog(loop)
            except Exception:
                logger.debug('Failed to start gateway loop liveness watchdog', exc_info=True)

    def _stop_loop_liveness_guards(self) -> None:
        """Disarm lifetime liveness guards before shutdown can load the loop."""
        watchdog = getattr(self, '_loop_liveness_watchdog', None)
        self._loop_liveness_watchdog = None
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:
                logger.debug('Failed to stop gateway loop liveness watchdog', exc_info=True)
        floor_timer = getattr(self, '_loop_floor_timer_handle', None)
        self._loop_floor_timer_handle = None
        if floor_timer is not None:
            try:
                floor_timer.cancel()
            except Exception:
                logger.debug('Failed to cancel gateway loop floor timer', exc_info=True)

    async def _consume_clean_shutdown_marker(self, marker_path) -> int:
        """Discard orphan turn markers before consuming a clean-exit receipt.

        If either persistence or marker removal fails, startup must fail closed.
        Continuing with the old receipt would let a later unclean exit masquerade
        as clean and discard genuinely interrupted turns.
        """
        discarded = await self.async_session_store.discard_active_turn_markers()
        marker_path.unlink()
        return discarded

    async def _recover_unclean_sessions(self) -> tuple[int, int]:
        """Recover exact active turns, then run the legacy recency fallback."""
        exact = 0
        fallback = 0
        try:
            agent_timeout = max(1.0, _float_env('HERMES_AGENT_TIMEOUT', 1800))
            marker_max_age = max(60 * 60, int(agent_timeout * 2))
            exact = await self.async_session_store.recover_interrupted_turns(max_age_seconds=marker_max_age)
        except Exception as exc:
            logger.warning('Exact active-turn recovery on startup failed: %s', exc)
        try:
            fallback = await self.async_session_store.suspend_recently_active(max_age_seconds=120)
        except Exception as exc:
            logger.warning('Legacy session recovery on startup failed: %s', exc)
        return (exact, fallback)

    async def start(self) -> bool:
        """
        Start the gateway and all configured platform adapters.
        
        Returns True if at least one adapter connected successfully.
        """
        logger.info('Starting Duck Agent Gateway...')
        try:
            faulthandler.enable()
        except (RuntimeError, ValueError, OSError):
            try:
                _fh_log_dir = getattr(self.config, 'log_dir', None) or os.path.join(str(get_hermes_home()), 'logs')
                os.makedirs(_fh_log_dir, exist_ok=True)
                _fh_enable_path = os.path.join(_fh_log_dir, 'gateway_faulthandler.log')
                _fh_enable_file = open(_fh_enable_path, 'a', encoding='utf-8')
                faulthandler.enable(file=_fh_enable_file, all_threads=True)
            except Exception:
                logger.debug('faulthandler.enable() unavailable', exc_info=True)
        _sigusr2 = getattr(signal, 'SIGUSR2', None)
        if _sigusr2 is not None and hasattr(faulthandler, 'register'):
            try:
                _log_dir = getattr(self.config, 'log_dir', None) or os.path.join(str(get_hermes_home()), 'logs')
                _faulthandler_path = os.path.join(_log_dir, 'gateway_faulthandler.log')
                os.makedirs(_log_dir, exist_ok=True)
                _fh = open(_faulthandler_path, 'a', encoding='utf-8')
                faulthandler.register(_sigusr2, file=_fh, all_threads=True, chain=True)
            except Exception:
                logger.debug('Could not set up faulthandler file logging', exc_info=True)
        try:
            self._gateway_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._gateway_loop = None
        if self._gateway_loop is not None:
            self._start_loop_liveness_guards(self._gateway_loop)
        logger.info('Session storage: %s', self.config.sessions_dir)
        try:
            from gateway.shutdown_forensics import check_systemd_timing_alignment
            _alignment = check_systemd_timing_alignment(self._restart_drain_timeout)
            if _alignment is not None and _alignment.get('mismatch'):
                logger.warning('Stale systemd unit detected: %s has TimeoutStopSec=%.0fs but drain_timeout=%.0fs (expected >=%.0fs). systemd may SIGKILL the gateway mid-drain. Run `duck-agent gateway install --force` to regenerate the unit, or shorten agent.restart_drain_timeout.', _alignment.get('unit', '(unknown)'), _alignment['timeout_stop_sec'], _alignment['drain_timeout'], _alignment['expected_min'])
        except Exception as _e:
            logger.debug('check_systemd_timing_alignment failed: %s', _e)
        try:
            _effective_max_iter = int(os.getenv('HERMES_MAX_ITERATIONS', '500'))
            logger.info('Agent budget: max_iterations=%d (agent.max_turns from config.yaml, or HERMES_MAX_ITERATIONS from .env, or default 500)', _effective_max_iter)
        except Exception:
            pass
        try:
            _redact_raw = os.getenv('HERMES_REDACT_SECRETS', 'true')
            _redact_on = _redact_raw.lower() in {'1', 'true', 'yes', 'on'}
            if _redact_on:
                logger.info('Secret redaction: ENABLED (tool output, logs, and chat responses are scrubbed before delivery)')
            else:
                logger.warning('Secret redaction: DISABLED (HERMES_REDACT_SECRETS=%s). API keys and tokens may appear verbatim in chat output, session JSONs, and logs. Set security.redact_secrets: true in config.yaml to re-enable.', _redact_raw)
        except Exception:
            pass
        try:
            from hermes_cli.profiles import get_active_profile_name
            _profile = get_active_profile_name()
            if _profile and _profile != 'default':
                logger.info('Active profile: %s', _profile)
        except Exception:
            pass
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(gateway_state='starting', exit_reason=None)
        except Exception:
            pass
        try:
            from hermes_cli.config import load_config
            from agent.monitoring.gateway_health_export import start_gateway_health_export
            self._gateway_health_export_runtime = start_gateway_health_export(load_config())
            if getattr(self._gateway_health_export_runtime, 'enabled', False):
                logger.info('Gateway health OTLP export: enabled')
        except Exception:
            logger.debug('gateway health OTLP export startup failed', exc_info=True)
        try:
            from hermes_cli.security_advisories import detect_compromised, gateway_log_message
            _adv_hits = detect_compromised()
            _adv_msg = gateway_log_message(_adv_hits)
            if _adv_msg:
                logger.warning('%s', _adv_msg)
                logger.warning('Run `duck-agent doctor` on the gateway host for full remediation steps.')
        except Exception:
            logger.debug('security advisory check failed at gateway startup', exc_info=True)
        if await self._abort_startup_if_shutdown_requested():
            return True
        _builtin_allowed_vars = ('TELEGRAM_ALLOWED_USERS', 'DISCORD_ALLOWED_USERS', 'WHATSAPP_ALLOWED_USERS', 'WHATSAPP_CLOUD_ALLOWED_USERS', 'SLACK_ALLOWED_USERS', 'SIGNAL_ALLOWED_USERS', 'SIGNAL_GROUP_ALLOWED_USERS', 'TELEGRAM_GROUP_ALLOWED_USERS', 'TELEGRAM_GROUP_ALLOWED_CHATS', 'EMAIL_ALLOWED_USERS', 'SMS_ALLOWED_USERS', 'MATTERMOST_ALLOWED_USERS', 'MATRIX_ALLOWED_USERS', 'DINGTALK_ALLOWED_USERS', 'FEISHU_ALLOWED_USERS', 'WECOM_ALLOWED_USERS', 'WECOM_CALLBACK_ALLOWED_USERS', 'WEIXIN_ALLOWED_USERS', 'BLUEBUBBLES_ALLOWED_USERS', 'QQ_ALLOWED_USERS', 'YUANBAO_ALLOWED_USERS', 'GATEWAY_ALLOWED_USERS')
        _builtin_allow_all_vars = ('TELEGRAM_ALLOW_ALL_USERS', 'DISCORD_ALLOW_ALL_USERS', 'WHATSAPP_ALLOW_ALL_USERS', 'WHATSAPP_CLOUD_ALLOW_ALL_USERS', 'SLACK_ALLOW_ALL_USERS', 'SIGNAL_ALLOW_ALL_USERS', 'EMAIL_ALLOW_ALL_USERS', 'SMS_ALLOW_ALL_USERS', 'MATTERMOST_ALLOW_ALL_USERS', 'MATRIX_ALLOW_ALL_USERS', 'DINGTALK_ALLOW_ALL_USERS', 'FEISHU_ALLOW_ALL_USERS', 'WECOM_ALLOW_ALL_USERS', 'WECOM_CALLBACK_ALLOW_ALL_USERS', 'WEIXIN_ALLOW_ALL_USERS', 'BLUEBUBBLES_ALLOW_ALL_USERS', 'QQ_ALLOW_ALL_USERS', 'YUANBAO_ALLOW_ALL_USERS')
        _plugin_allowed_vars: tuple = ()
        _plugin_allow_all_vars: tuple = ()
        try:
            from gateway.platform_registry import platform_registry
            _plugin_allowed_vars = tuple((e.allowed_users_env for e in platform_registry.plugin_entries() if e.allowed_users_env))
            _plugin_allow_all_vars = tuple((e.allow_all_env for e in platform_registry.plugin_entries() if e.allow_all_env))
        except Exception:
            pass
        _any_allowlist = any((os.getenv(v) for v in _builtin_allowed_vars + _plugin_allowed_vars))
        _allow_all = os.getenv('GATEWAY_ALLOW_ALL_USERS', '').lower() in {'true', '1', 'yes'} or any((os.getenv(v, '').lower() in {'true', '1', 'yes'} for v in _builtin_allow_all_vars + _plugin_allow_all_vars))
        if not _any_allowlist and (not _allow_all):
            logger.warning('No env user allowlists configured. Messaging platforms default to pairing/allowlist policies and will deny unknown senders unless you configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id) or explicitly opt in with GATEWAY_ALLOW_ALL_USERS=true plus dm_policy/group_policy: open on the platform.')
        reason = _own_policy_open_startup_violation(self.config)
        if reason:
            platform_value = reason.split(':', 1)[0]
            allow_all_env = None
            for platform, open_env in _OWN_POLICY_OPEN_ENV.items():
                if platform.value == platform_value:
                    allow_all_env = open_env[2]
                    break
            logger.error("Refusing to start: %s has dm_policy/group_policy set to 'open' but neither GATEWAY_ALLOW_ALL_USERS nor %s is enabled.", platform_value, allow_all_env or 'a platform allow-all flag')
            try:
                from gateway.status import write_runtime_status
                write_runtime_status(gateway_state='startup_failed', exit_reason=reason)
            except Exception:
                pass
            self._request_clean_exit(reason)
            return True
        try:
            from hermes_cli.plugins import discover_plugins
            discover_plugins()
        except Exception:
            logger.warning('plugin discovery failed at gateway startup', exc_info=True)
        try:
            from gateway.relay import register_relay_adapter, relay_url, self_provision_relay, send_relay_policy
            self_provision_relay()
            if register_relay_adapter():
                logger.info('relay adapter registered (connector at %s)', relay_url())
                send_relay_policy()
        except Exception:
            logger.warning('relay adapter registration failed at gateway startup', exc_info=True)
        try:
            from hermes_cli.config import load_config
            from agent.shell_hooks import register_from_config
            _hooks_cfg = load_config()
            register_from_config(_hooks_cfg, accept_hooks=False)
            from agent.outbound_webhooks import register_from_config as register_outbound_webhooks
            register_outbound_webhooks(_hooks_cfg)
        except Exception:
            logger.debug('shell-hook registration failed at gateway startup', exc_info=True)
        self.hooks.discover_and_load()
        try:
            from tools.process_registry import process_registry
            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                logger.info('Recovered %s background process(es) from previous run', recovered)
        except Exception as e:
            logger.warning('Process checkpoint recovery: %s', e)
        _clean_marker = _hermes_home / '.clean_shutdown'
        if _clean_marker.exists():
            logger.info('Previous gateway exited cleanly — skipping session suspension')
            try:
                discarded = await self._consume_clean_shutdown_marker(_clean_marker)
            except Exception as exc:
                logger.error('Clean-start marker cleanup failed; refusing startup so the clean-exit receipt cannot mask a later unclean exit: %s', exc)
                raise RuntimeError('clean-start recovery cleanup failed') from exc
            if discarded:
                logger.info('Discarded %d orphan active-turn marker(s) after clean shutdown', discarded)
        else:
            exact, fallback = await self._recover_unclean_sessions()
            recovered = exact + fallback
            if recovered:
                logger.info('Marked %d in-flight session(s) as resumable from previous run (%d exact, %d legacy)', recovered, exact, fallback)
        try:
            stuck = self._suspend_stuck_loop_sessions()
            if stuck:
                logger.warning('Auto-suspended %d stuck-loop session(s)', stuck)
        except Exception as e:
            logger.debug('Stuck-loop detection failed: %s', e)
        self._startup_restore_in_progress = True
        self._startup_restore_queue = []
        self._startup_restore_tasks = []
        connected_count = 0
        enabled_platform_count = 0
        startup_nonretryable_errors: list[str] = []
        startup_retryable_errors: list[str] = []
        _multiplex_on = bool(getattr(self.config, 'multiplex_profiles', False))
        _multiplex_skipped_platforms: list[Platform] = []
        for platform, platform_config in self.config.platforms.items():
            if await self._abort_startup_if_shutdown_requested():
                return True
            if not platform_config.enabled:
                continue
            if _multiplex_on and (not _platform_has_bot_credential(platform, platform_config)):
                logger.info("Skipping %s on default profile: no bot credential in this profile's secrets. Secondary multiplexed profiles that provide the token will still connect.", platform.value)
                _multiplex_skipped_platforms.append(platform)
                continue
            enabled_platform_count += 1
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                _pval = platform.value
                _builtin_names = {m.value for m in Platform.__members__.values()}
                if _pval not in _builtin_names:
                    logger.warning("No adapter for '%s' — is the plugin installed? (platform is enabled in config.yaml but no plugin registered it)", _pval)
                else:
                    logger.warning('No adapter available for %s', _pval)
                continue
            adapter.set_message_handler(self._primary_message_handler())
            adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
            adapter.set_session_store(self.session_store)
            adapter.set_busy_session_handler(self._handle_active_session_busy_message)
            _set_reaction = getattr(adapter, 'set_reaction_handler', None)
            if callable(_set_reaction):
                _set_reaction(self._handle_reaction_event)
            adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
            adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
            adapter._busy_text_mode = self._busy_text_mode
            logger.info('Connecting to %s...', platform.value)
            self._update_platform_runtime_status(platform.value, platform_state='connecting', error_code=None, error_message=None)
            try:
                success = await self._connect_initial_adapter_with_timeout(adapter, platform)
                if await self._abort_startup_if_shutdown_requested(adapter, platform):
                    return True
                if success:
                    self.adapters[platform] = adapter
                    self._sync_voice_mode_state_to_adapter(adapter)
                    if hasattr(adapter, '_voice_input_callback'):
                        adapter._voice_input_callback = self._handle_voice_channel_input
                    connected_count += 1
                    self._update_platform_runtime_status(platform.value, platform_state='connected', error_code=None, error_message=None)
                    logger.info('✓ %s connected', platform.value)
                else:
                    logger.warning('✗ %s failed to connect', platform.value)
                    await self._safe_adapter_disconnect(adapter, platform)
                    if adapter.has_fatal_error:
                        self._update_platform_runtime_status(platform.value, platform_state='retrying' if adapter.fatal_error_retryable else 'fatal', error_code=adapter.fatal_error_code, error_message=adapter.fatal_error_message)
                        target = startup_retryable_errors if adapter.fatal_error_retryable else startup_nonretryable_errors
                        target.append(f'{platform.value}: {adapter.fatal_error_message}')
                        if adapter.fatal_error_retryable:
                            self._failed_platforms[platform] = {'config': platform_config, 'attempts': 1, 'next_retry': time.monotonic() + 30, 'credential_claim': self._adapter_credential_claim(platform, adapter), 'listener_claim': self._adapter_listener_claim(platform, adapter)}
                    else:
                        self._update_platform_runtime_status(platform.value, platform_state='retrying', error_code=None, error_message='failed to connect')
                        startup_retryable_errors.append(f'{platform.value}: failed to connect')
                        self._failed_platforms[platform] = {'config': platform_config, 'attempts': 1, 'next_retry': time.monotonic() + 30, 'credential_claim': self._adapter_credential_claim(platform, adapter), 'listener_claim': self._adapter_listener_claim(platform, adapter)}
            except Exception as e:
                logger.error('✗ %s error: %s', platform.value, e)
                await self._safe_adapter_disconnect(adapter, platform)
                self._update_platform_runtime_status(platform.value, platform_state='retrying', error_code=None, error_message=str(e))
                startup_retryable_errors.append(f'{platform.value}: {e}')
                self._failed_platforms[platform] = {'config': platform_config, 'attempts': 1, 'next_retry': time.monotonic() + 30, 'credential_claim': self._adapter_credential_claim(platform, adapter), 'listener_claim': self._adapter_listener_claim(platform, adapter)}
            if await self._abort_startup_if_shutdown_requested():
                return True
        try:
            _secondary_connected = await self._start_secondary_profile_adapters()
            connected_count += _secondary_connected
        except MultiplexConfigError as e:
            reason = str(e)
            logger.error('Gateway multiplexer config error: %s', reason)
            try:
                from gateway.status import write_runtime_status
                write_runtime_status(gateway_state='startup_failed', exit_reason=reason)
            except Exception:
                pass
            self._exit_code = GATEWAY_FATAL_CONFIG_EXIT_CODE
            self._request_clean_exit(reason)
            self._startup_restore_in_progress = False
            return True
        except Exception as e:
            logger.error('Secondary-profile adapter startup failed: %s', e, exc_info=True)
        finally:
            self._platform_lock_takeover_on_start = False
        for _skipped in _multiplex_skipped_platforms:
            _served_by_secondary = any((_skipped in _profile_map for _profile_map in self._profile_adapters.values()))
            if not _served_by_secondary:
                logger.warning('%s is enabled but no profile (default or secondary) provided a bot credential for it — the platform is not being served. Add its token to the profile that should own it, or disable the platform.', _skipped.value)
        if connected_count == 0:
            if startup_nonretryable_errors and (not startup_retryable_errors):
                reason = '; '.join(startup_nonretryable_errors)
                logger.error('Gateway hit a non-retryable startup conflict: %s', reason)
                try:
                    from gateway.status import write_runtime_status
                    write_runtime_status(gateway_state='startup_failed', exit_reason=reason)
                except Exception:
                    pass
                self._exit_code = GATEWAY_FATAL_CONFIG_EXIT_CODE
                self._request_clean_exit(reason)
                self._startup_restore_in_progress = False
                return True
            if startup_nonretryable_errors:
                logger.error('%d platform(s) fatally misconfigured and parked: %s. Staying alive so retryable platforms can recover.', len(startup_nonretryable_errors), '; '.join(startup_nonretryable_errors))
            if enabled_platform_count > 0:
                if startup_retryable_errors:
                    reason = '; '.join(startup_retryable_errors)
                    logger.warning('Gateway started with no connected platforms — %d platform(s) queued for retry: %s', len(self._failed_platforms), reason)
                    try:
                        from gateway.status import write_runtime_status
                        write_runtime_status(gateway_state='degraded', exit_reason=None)
                    except Exception:
                        pass
                logger.warning('No adapter could be created for any of the %d configured platform(s). Check that required dependencies are installed and credentials are set. Gateway will continue for cron job execution.', enabled_platform_count)
            else:
                logger.warning('No messaging platforms enabled.')
                logger.info('Gateway will continue running for cron job execution.')
        if await self._abort_startup_if_shutdown_requested():
            return True
        self.delivery_router.adapters = self.adapters
        self._wire_teams_pipeline_runtime()
        self._running = True
        self._update_runtime_status('running')
        try:
            _existing_hb = getattr(self, '_loop_heartbeat_task', None)
            if _existing_hb is None or _existing_hb.done():
                self._loop_heartbeat_task = asyncio.create_task(loop_heartbeat_forever(interval_s=DEFAULT_HEARTBEAT_INTERVAL_S, start_time=getattr(self, '_gateway_started_at', 0.0)))
                _bg = getattr(self, '_background_tasks', None)
                if _bg is not None:
                    _bg.add(self._loop_heartbeat_task)
                    self._loop_heartbeat_task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug('Failed to start gateway loop heartbeat', exc_info=True)
        hook_count = len(self.hooks.loaded_hooks)
        if hook_count:
            logger.info('%s hook(s) loaded', hook_count)
        await self.hooks.emit('gateway:startup', {'platforms': [p.value for p in self.adapters.keys()]})
        if connected_count > 0:
            logger.info('Gateway running with %s platform(s)', connected_count)
        try:
            from gateway.channel_directory import build_channel_directory
            directory = await build_channel_directory(self.adapters)
            ch_count = sum((len(chs) for chs in directory.get('platforms', {}).values()))
            logger.info('Channel directory built: %d target(s)', ch_count)
        except Exception as e:
            logger.warning('Channel directory build failed: %s', e)
        notified = await self._send_update_notification()
        if not notified and any((path.exists() for path in (_hermes_home / '.update_pending.json', _hermes_home / '.update_pending.claimed.json'))):
            self._schedule_update_notification_watch()
        if connected_count > 0:
            await asyncio.sleep(1.0)
        chat_restart_notification_pending = _restart_notification_pending()
        planned_restart_notification_pending = _planned_restart_notification_pending()
        if chat_restart_notification_pending:
            self._booted_from_restart = True
        await self._send_restart_notification()
        if planned_restart_notification_pending:
            try:
                await self._send_home_channel_startup_notifications(skip_targets=None)
            finally:
                _clear_planned_restart_notification()
        await self._redeliver_pending_obligations()
        self._schedule_resume_pending_sessions()
        await self._finish_startup_restore()
        try:
            from tools.process_registry import process_registry
            watchers = process_registry.pending_watchers
            process_registry.pending_watchers = []
            for i, watcher in enumerate(watchers):
                self._spawn_supervised(lambda w=watcher: self._run_process_watcher(w), f"process_watcher:{watcher.get('session_id')}", restart=False)
                logger.info('Resumed watcher for recovered process %s', watcher.get('session_id'))
                if i % 100 == 99:
                    await asyncio.sleep(0)
        except Exception as e:
            logger.error('Recovered watcher setup error: %s', e)
        self._spawn_supervised(self._session_expiry_watcher, 'session_expiry_watcher')
        self._spawn_supervised(self._session_stall_watcher, 'session_stall_watcher')
        self._spawn_supervised(self._kanban_notifier_watcher, 'kanban_notifier_watcher')
        self._spawn_supervised(self._kanban_dispatcher_watcher, 'kanban_dispatcher_watcher')
        if self._failed_platforms:
            logger.info('Starting reconnection watcher for %d failed platform(s): %s', len(self._failed_platforms), ', '.join((p.value for p in self._failed_platforms)))
        self._reconnect_watcher_task = self._spawn_supervised(self._platform_reconnect_watcher, 'platform_reconnect_watcher', on_spawn=lambda t: setattr(self, '_reconnect_watcher_task', t))
        self._spawn_supervised(self._handoff_watcher, 'handoff_watcher')
        self._spawn_supervised(self._async_delegation_watcher, 'async_delegation_watcher')
        try:
            if self._scale_to_zero_should_arm():
                logger.info('scale-to-zero: armed (idle timeout %.0fs) — watching for idle', self._scale_to_zero_idle_timeout_seconds())
                self._spawn_supervised(self._scale_to_zero_watcher, 'scale_to_zero_watcher')
            else:
                self._log_scale_to_zero_not_armed_reason()
        except Exception:
            logger.debug('scale-to-zero: arm check failed at startup', exc_info=True)
        self._spawn_supervised(self._drain_control_watcher, 'drain_control_watcher')
        logger.info('Press Ctrl+C to stop')
        return True
    _MAX_SUPERVISED_RESTARTS = 5
    _SUPERVISED_HEALTHY_SECS = 300

    def _spawn_supervised(self, coro_factory, name, *, restart=True, _attempt=0, on_spawn=None):
        """Launch a long-lived background task with task-level supervision.

        Complements upstream's per-iteration inner-loop try/except (which only
        guards a single loop-body) by covering what that CANNOT: an exception
        raised in the watcher's OUTER ``while self._running:`` loop or its
        pre-try setup region, plus task-level death generally. A bare
        ``asyncio.create_task`` drops such an exception on the floor — no log,
        no restart, the watcher silently gone. This retains the handle in
        ``self._background_tasks``, logs any crash, and restarts with capped
        exponential backoff up to ``_MAX_SUPERVISED_RESTARTS`` failures in rapid
        succession (each within ``_SUPERVISED_HEALTHY_SECS`` of its restart).
        The counter resets after any run that stayed healthy for at least
        ``_SUPERVISED_HEALTHY_SECS`` — so a long-lived daemon that crashes
        occasionally over days is never permanently abandoned.

        ``on_spawn`` (optional) is invoked with the freshly-created task on
        every spawn, INCLUDING internal backoff respawns. Callers that also
        track the live handle elsewhere (e.g. ``self._reconnect_watcher_task``
        for ``_ensure_reconnect_watcher_running``) MUST pass it — otherwise the
        supervisor's own respawn creates a new task without updating that
        external handle, so ``_ensure_...`` later sees the stale/done handle
        and spawns a SECOND concurrent watcher (double reconnect attempts).
        """
        if getattr(self, '_background_tasks', None) is None:
            self._background_tasks = set()
        _started = time.monotonic()
        task = asyncio.create_task(coro_factory())
        self._background_tasks.add(task)
        if on_spawn is not None:
            try:
                on_spawn(task)
            except Exception:
                logger.debug('on_spawn callback for %s raised', name, exc_info=True)

        def _done(t):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            logger.error('Supervised task %s died: %r', name, exc, exc_info=exc)
            if restart and self._running:
                ran_for = time.monotonic() - _started
                if ran_for >= self._SUPERVISED_HEALTHY_SECS:
                    effective_attempt = 0
                else:
                    effective_attempt = _attempt
                if effective_attempt >= self._MAX_SUPERVISED_RESTARTS:
                    logger.error('Supervised task %s died %d times in rapid succession (each within %ds of restart) — giving up restarts', name, effective_attempt, self._SUPERVISED_HEALTHY_SECS)
                    return
                backoff = min(60, 2 ** min(effective_attempt, 6))

                async def _respawn():
                    await asyncio.sleep(backoff)
                    if self._running:
                        self._spawn_supervised(coro_factory, name, restart=restart, _attempt=effective_attempt + 1, on_spawn=on_spawn)
                respawn_task = asyncio.create_task(_respawn())
                self._background_tasks.add(respawn_task)
                respawn_task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_done)
        return task

    async def _handoff_watcher(self, interval: float=2.0) -> None:
        """Background task that processes pending CLI→gateway session handoffs.

        Polls ``state.db`` for sessions in ``handoff_state='pending'`` and,
        for each one:

        1. Atomically claims it (pending → running).
        2. Resolves the destination platform's configured home channel.
        3. Re-binds the gateway's session_key for that home channel to the
           CLI's existing session_id via ``session_store.switch_session`` so
           the full role-aware transcript replays on the next agent turn.
        4. Forges a synthetic ``MessageEvent`` (``internal=True``) with a
           handoff-notice text and dispatches through the normal gateway
           message pipeline so the agent runs and replies on the platform.
        5. Marks the row ``completed`` (or ``failed`` with ``handoff_error``).

        The CLI process is poll-blocked on the row's terminal state and
        prints the result to the user.
        """
        await asyncio.sleep(5)
        while self._running:
            try:
                if self._session_db is None:
                    await asyncio.sleep(interval)
                    continue
                pending = await self._session_db.list_pending_handoffs()
                for row in pending:
                    session_id = row.get('id')
                    if not session_id:
                        continue
                    if not await self._session_db.claim_handoff(session_id):
                        continue
                    try:
                        await self._process_handoff(row)
                        await self._session_db.complete_handoff(session_id)
                    except Exception as exc:
                        logger.warning('Handoff for session %s failed: %s', session_id, exc, exc_info=True)
                        await self._session_db.fail_handoff(session_id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug('Handoff watcher tick error: %s', exc, exc_info=True)
            await asyncio.sleep(interval)

    async def _process_handoff(self, row: Dict[str, Any]) -> None:
        """Execute one handoff row. Raises on failure (caller marks failed)."""
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent
        cli_session_id = row['id']
        platform_name = (row.get('handoff_platform') or '').strip().lower()
        if not platform_name:
            raise RuntimeError('handoff_platform is empty')
        try:
            platform = Platform(platform_name)
        except (ValueError, KeyError):
            raise RuntimeError(f"unknown platform '{platform_name}'")
        transport = resolve_delivery_transport(platform, self.config, self.adapters)
        if not transport:
            raise RuntimeError(f"platform '{platform_name}' is not active in this gateway")
        adapter = transport.adapter
        home = self.config.get_home_channel(platform)
        if not home or not home.chat_id:
            raise RuntimeError(f'no home channel configured for {platform_name}; run /sethome on the desired chat first')
        cli_title = row.get('title') or cli_session_id[:8]
        thread_name = f'Duck Agent — {cli_title}'
        try:
            new_thread_id = await adapter.create_handoff_thread(str(home.chat_id), thread_name)
        except Exception as exc:
            logger.debug('Handoff: create_handoff_thread raised on %s: %s', platform_name, exc, exc_info=True)
            new_thread_id = None
        effective_thread_id = new_thread_id or (str(home.thread_id) if home.thread_id else None)
        home_chat_id = str(home.chat_id)
        is_telegram_private_chat = platform == Platform.TELEGRAM and looks_like_telegram_private_chat_id(home_chat_id)
        if new_thread_id and (not is_telegram_private_chat):
            dest_chat_type = 'thread'
            dest_user_id = 'system:handoff'
        else:
            dest_chat_type = 'dm'
            dest_user_id = home_chat_id if is_telegram_private_chat else 'system:handoff'
        if platform == Platform.DISCORD and dest_chat_type == 'thread' and effective_thread_id:
            dest_chat_id = str(effective_thread_id)
        else:
            dest_chat_id = home_chat_id
        dest_source = SessionSource(platform=platform, chat_id=dest_chat_id, chat_name=home.name, chat_type=dest_chat_type, user_id=dest_user_id, user_name='Handoff', thread_id=effective_thread_id)
        platform_cfg = self.config.platforms.get(platform)
        extra = platform_cfg.extra if platform_cfg else {}
        session_key = build_session_key(dest_source, group_sessions_per_user=extra.get('group_sessions_per_user', True), thread_sessions_per_user=extra.get('thread_sessions_per_user', False))
        await self.async_session_store.get_or_create_session(dest_source)
        switched = await self.async_session_store.switch_session(session_key, cli_session_id)
        if switched is None:
            raise RuntimeError(f'could not switch session key {session_key} → {cli_session_id}')
        self._evict_cached_agent(session_key)
        self._release_running_agent_state(session_key)
        synthetic_text = f'''[Session was just handed off from CLI ("{cli_title}") to this channel. The full prior conversation history is loaded above. Briefly confirm you're working here and summarize what we were working on, so the user can continue from this device.]'''
        synthetic_event = MessageEvent(text=synthetic_text, source=dest_source, internal=True)
        logger.info('Handoff: dispatching synthetic turn for CLI session %s → %s (home=%s, thread=%s, session_key=%s)', cli_session_id, platform_name, home.chat_id, effective_thread_id, session_key)
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            return
        send_metadata: Dict[str, Any] = {}
        if effective_thread_id:
            send_metadata['thread_id'] = effective_thread_id
        try:
            result = await transport.send(platform, str(home.chat_id), response_text, send_metadata or None)
        except Exception as exc:
            raise RuntimeError(f'adapter.send failed: {exc}') from exc
        if not getattr(result, 'success', True):
            err = getattr(result, 'error', 'send returned success=False')
            raise RuntimeError(f'adapter.send failed: {err}')

    async def _session_expiry_watcher(self, interval: int=300):
        """Background task that finalizes expired sessions.

        Runs every ``interval`` seconds (default 5 min).  For each session
        whose reset policy has expired, invokes ``on_session_finalize``
        hooks, cleans up the cached AIAgent's tool resources, evicts the
        cache entry so it can be garbage-collected, and marks the session
        so it won't be finalized again.
        """
        await asyncio.sleep(60)
        _finalize_failures: dict[str, int] = {}
        _MAX_FINALIZE_RETRIES = 3
        while self._running:
            try:
                await self.async_session_store._ensure_loaded()
                _expired_entries = []
                for key, entry in list(self.session_store._entries.items()):
                    if entry.expiry_finalized:
                        continue
                    if not await self.async_session_store._is_session_expired(entry):
                        continue
                    _expired_entries.append((key, entry))
                if _expired_entries:
                    _platforms: dict[str, int] = {}
                    for _k, _e in _expired_entries:
                        _parts = _k.split(':')
                        _plat = _parts[2] if len(_parts) > 2 else 'unknown'
                        _platforms[_plat] = _platforms.get(_plat, 0) + 1
                    _plat_summary = ', '.join((f'{p}:{c}' for p, c in sorted(_platforms.items())))
                    logger.info('Session expiry: %d sessions to finalize (%s)', len(_expired_entries), _plat_summary)
                for key, entry in _expired_entries:
                    try:
                        try:
                            from hermes_cli.lifecycle import finalize_session
                            _parts = key.split(':')
                            _platform = _parts[2] if len(_parts) > 2 else ''
                            finalize_session(session_id=entry.session_id, platform=_platform, reason='session_expired')
                        except Exception:
                            pass
                        _cached_agent = None
                        _cache_lock = getattr(self, '_agent_cache_lock', None)
                        if _cache_lock is not None:
                            with _cache_lock:
                                _cached = self._agent_cache.get(key)
                                _cached_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
                        if _cached_agent is None:
                            _exp_state = self._peek_session_state(key)
                            _cached_agent = _exp_state.turn.agent if _exp_state else None
                        if _cached_agent and _cached_agent is not _AGENT_PENDING_SENTINEL:
                            await self._cleanup_agent_resources_off_loop(_cached_agent, context='session expiry')
                        self._evict_cached_agent(key)
                        self._clear_conversation_scope(key, reason='expiry_finalized')
                        await self.async_session_store.set_expiry_finalized(entry)
                        logger.debug('Session expiry finalized for %s', entry.session_id)
                        _finalize_failures.pop(entry.session_id, None)
                    except Exception as e:
                        failures = _finalize_failures.get(entry.session_id, 0) + 1
                        _finalize_failures[entry.session_id] = failures
                        if failures >= _MAX_FINALIZE_RETRIES:
                            logger.warning('Session finalize gave up after %d attempts for %s: %s. Marking as finalized to prevent infinite retry loop.', failures, entry.session_id, e)
                            await self.async_session_store.set_expiry_finalized(entry, clear_model_override=False)
                            _finalize_failures.pop(entry.session_id, None)
                        else:
                            logger.debug('Session finalize failed (%d/%d) for %s: %s', failures, _MAX_FINALIZE_RETRIES, entry.session_id, e)
                if _expired_entries:
                    _done = sum((1 for _, e in _expired_entries if e.expiry_finalized))
                    _failed = len(_expired_entries) - _done
                    if _failed:
                        logger.info('Session expiry done: %d finalized, %d pending retry', _done, _failed)
                    else:
                        logger.info('Session expiry done: %d finalized', _done)
                try:
                    _idle_evicted = self._sweep_idle_cached_agents()
                    if _idle_evicted:
                        logger.info('Agent cache idle sweep: evicted %d agent(s)', _idle_evicted)
                except Exception as _e:
                    logger.debug('Idle agent sweep failed: %s', _e)
                try:
                    self._sweep_agent_cache_under_pressure()
                except Exception as _e:
                    logger.debug('Agent cache pressure sweep failed: %s', _e)
                _last_prune_ts = getattr(self, '_last_session_store_prune_ts', 0.0)
                _prune_interval = 3600.0
                if time.time() - _last_prune_ts > _prune_interval:
                    try:
                        _max_age = int(getattr(self.config, 'session_store_max_age_days', 0) or 0)
                        if _max_age > 0:
                            _pruned = await self.async_session_store.prune_old_entries(_max_age)
                            if _pruned:
                                logger.info('SessionStore prune: dropped %d stale entries', _pruned)
                    except Exception as _e:
                        logger.debug('SessionStore prune failed: %s', _e)
                    self._last_session_store_prune_ts = time.time()
            except Exception as e:
                logger.debug('Session expiry watcher error: %s', e)
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _session_stall_timeout_seconds(self) -> float:
        """Return configured stall timeout (seconds); 0 disables the watchdog."""
        return _float_env('HERMES_SESSION_STALL_TIMEOUT', 300)

    def _iter_gateway_adapters(self):
        """Yield every live platform adapter (default + multiplex profiles)."""
        seen: set[int] = set()
        for adapter in list(getattr(self, 'adapters', {}).values()):
            if adapter is None:
                continue
            aid = id(adapter)
            if aid in seen:
                continue
            seen.add(aid)
            yield adapter
        for amap in list(getattr(self, '_profile_adapters', {}).values()):
            for adapter in list(amap.values()):
                if adapter is None:
                    continue
                aid = id(adapter)
                if aid in seen:
                    continue
                seen.add(aid)
                yield adapter

    def _session_activity_for_stall(self, session_key: str) -> Optional[dict]:
        """Return the shared activity snapshot for stall progress (#72039).

        Single progress source: ``AIAgent.get_activity_summary()`` /
        ``agent.session_activity``. No turn-start or pending-inbound clocks.
        """
        agent = (getattr(self, '_running_agents', None) or {}).get(session_key)
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return None
        if not hasattr(agent, 'get_activity_summary'):
            return None
        try:
            summary = agent.get_activity_summary()
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None

    async def _check_session_stalls(self, timeout_seconds: float) -> int:
        """Scan pending inbound sessions and notify once per stall episode.

        Returns the number of notifications sent this pass (for tests).
        """
        from gateway.session_stall import format_session_stall_notification, resolve_session_idle_seconds_from_activity, should_clear_session_stall_notification, should_emit_session_stall_notification
        notified_map = getattr(self, '_session_stall_notified', None)
        if notified_map is None:
            notified_map = {}
            self._session_stall_notified = notified_map
        sent = 0
        now = time.time()
        candidates: Dict[str, tuple[Any, Any]] = {}
        for adapter in self._iter_gateway_adapters():
            pending_slot = getattr(adapter, '_pending_messages', None) or {}
            for session_key, event in list(pending_slot.items()):
                if session_key and session_key not in candidates and (event is not None):
                    candidates[session_key] = (adapter, event)
        for session_key, overflow in list((getattr(self, '_queued_events', None) or {}).items()):
            if not session_key or session_key in candidates or (not overflow):
                continue
            event = overflow[0]
            source = getattr(event, 'source', None)
            adapter = self._adapter_for_source(source) if source is not None else None
            if adapter is None:
                continue
            candidates[session_key] = (adapter, event)
        for session_key, (adapter, pending_event) in list(candidates.items()):
            has_pending = pending_event is not None
            activity = self._session_activity_for_stall(session_key) if has_pending else None
            idle_seconds = resolve_session_idle_seconds_from_activity(activity, now=now) if has_pending else None
            already = bool(notified_map.get(session_key))
            if should_clear_session_stall_notification(timeout_seconds=timeout_seconds, idle_seconds=idle_seconds, has_pending_inbound=has_pending):
                notified_map.pop(session_key, None)
                already = False
            if not should_emit_session_stall_notification(timeout_seconds=timeout_seconds, idle_seconds=idle_seconds, has_pending_inbound=has_pending, already_notified=already):
                continue
            if idle_seconds is None:
                continue
            mins = max(1, int(idle_seconds // 60))
            activity = activity or {}
            logger.warning('Session stall detected: session=%s idle=%.0fs (timeout=%.0fs, ~%d min); pending inbound present | last_activity=%s | provenance=%s (agent.session_stall_timeout)', session_key, idle_seconds, timeout_seconds, mins, activity.get('last_activity_desc') or activity.get('last_activity_description') or 'unknown', activity.get('provenance') or activity.get('last_activity_provenance') or 'unknown')
            source = getattr(pending_event, 'source', None)
            chat_id = getattr(source, 'chat_id', None) if source is not None else None
            if not chat_id:
                logger.warning('Session stall notify skipped (no chat_id): session=%s', session_key)
                notified_map[session_key] = True
                continue
            still_pending = (getattr(adapter, '_pending_messages', None) or {}).get(session_key) is not None or bool((getattr(self, '_queued_events', None) or {}).get(session_key))
            fresh_idle = resolve_session_idle_seconds_from_activity(self._session_activity_for_stall(session_key), now=time.time())
            if not still_pending or (fresh_idle is not None and fresh_idle < timeout_seconds):
                logger.info('Session stall notify aborted (no longer stale): session=%s pending=%s fresh_idle=%s', session_key, still_pending, fresh_idle)
                notified_map.pop(session_key, None)
                continue
            try:
                metadata = self._thread_metadata_for_source(source) if source is not None and hasattr(self, '_thread_metadata_for_source') else None
                try:
                    result = await asyncio.wait_for(adapter.send(str(chat_id), format_session_stall_notification(idle_seconds), metadata=metadata), timeout=_STALL_NOTIFY_SEND_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning('Session stall notify send timed out after %.0fs for %s; will retry next tick', _STALL_NOTIFY_SEND_TIMEOUT_SECONDS, session_key)
                    continue
                if result is not None and getattr(result, 'success', True) is False:
                    logger.warning('Session stall notify failed for %s: %s', session_key, getattr(result, 'error', 'send returned success=False'))
                    continue
                sent += 1
                notified_map[session_key] = True
            except Exception as exc:
                logger.warning('Session stall notify failed for %s: %s', session_key, exc)
        for key in list(notified_map.keys()):
            if key not in candidates:
                notified_map.pop(key, None)
        return sent

    async def _session_stall_watcher(self, interval: float=30.0):
        """Periodic pending-inbound + stale-activity stall watchdog (#72016).

        Progress comes only from ``get_activity_summary()`` (#72039).
        Pending inbound is a notify policy gate, not a progress clock.
        Notify-only: does not kill the turn (contrast ``gateway_timeout`` /
        ``shutdown_watchdog``).
        """
        await asyncio.sleep(min(30.0, max(1.0, float(interval))))
        while self._running:
            try:
                timeout = self._session_stall_timeout_seconds()
                if timeout > 0:
                    await self._check_session_stalls(timeout)
            except Exception as exc:
                logger.debug('Session stall watcher error: %s', exc)
            steps = max(1, int(float(interval)))
            for _ in range(steps):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or 'default'
        except Exception:
            return 'default'

    def _ensure_reconnect_watcher_running(self) -> None:
        """Ensure the platform reconnect watcher background task is alive.

        If the tracked reconnect watcher task has died (e.g. from exhausting
        its restart budget, or a terminal exception that _spawn_supervised
        could not recover), respawns it so platforms queued for reconnection
        are not permanently stranded. Called after queueing a retryable fatal
        error in _handle_adapter_fatal_error (#70344).
        """
        if not getattr(self, '_running', False):
            return
        task = getattr(self, '_reconnect_watcher_task', None)
        if task is not None and (not task.done()):
            return
        logger.warning('Reconnect watcher task is dead (done=%s) — respawning', task.done() if task is not None else 'N/A')
        self._reconnect_watcher_task = self._spawn_supervised(self._platform_reconnect_watcher, 'platform_reconnect_watcher', on_spawn=lambda t: setattr(self, '_reconnect_watcher_task', t))

    async def _platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms.

        Uses exponential backoff: 30s → 60s → 120s → 240s → 300s (cap).
        Retryable failures (network/DNS blips) keep retrying at the backoff
        cap indefinitely — they self-heal once connectivity returns, so a
        transient outage never requires manual intervention. Non-retryable
        failures (bad auth, etc.) drop out of the queue immediately. The
        circuit breaker (``_pause_failed_platform`` / ``/platform pause``)
        remains available for manual operator control via ``/platform list``
        and ``/platform resume <name>``, but is no longer triggered
        automatically — auto-pausing a recovered platform was the cause of
        bots silently staying dead after a transient DNS failure.
        """
        await asyncio.sleep(10)
        while self._running:
            if not self._failed_platforms:
                for _ in range(30):
                    if not self._running:
                        return
                    if self._failed_platforms:
                        break
                    await asyncio.sleep(1)
                continue
            now = time.monotonic()
            for platform in list(self._failed_platforms.keys()):
                if not self._running:
                    return
                info = self._failed_platforms.get(platform)
                if info is None:
                    continue
                if info.get('paused'):
                    continue
                if now < info['next_retry']:
                    continue
                platform_config = info['config']
                attempt = info['attempts'] + 1
                if not _platform_has_bot_credential(platform, platform_config):
                    logger.warning('Reconnect %s: no bot credential on queued config, removing from retry queue', platform.value)
                    del self._failed_platforms[platform]
                    continue
                logger.info('Reconnecting %s (attempt %d)...', platform.value, attempt)
                adapter = None
                try:
                    adapter = self._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning('Reconnect %s: adapter creation returned None, removing from retry queue', platform.value)
                        del self._failed_platforms[platform]
                        continue
                    adapter.set_message_handler(self._primary_message_handler())
                    adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
                    adapter.set_session_store(self.session_store)
                    adapter.set_busy_session_handler(self._handle_active_session_busy_message)
                    _set_reaction = getattr(adapter, 'set_reaction_handler', None)
                    if callable(_set_reaction):
                        _set_reaction(self._handle_reaction_event)
                    adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
                    adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
                    adapter._busy_text_mode = self._busy_text_mode
                    success = await self._connect_adapter_with_timeout(adapter, platform, is_reconnect=True)
                    if success:
                        self.adapters[platform] = adapter
                        self._sync_voice_mode_state_to_adapter(adapter)
                        if hasattr(adapter, '_voice_input_callback'):
                            adapter._voice_input_callback = self._handle_voice_channel_input
                        self.delivery_router.adapters = self.adapters
                        del self._failed_platforms[platform]
                        self._update_platform_runtime_status(platform.value, platform_state='connected', error_code=None, error_message=None)
                        logger.info('✓ %s reconnected successfully', platform.value)
                        try:
                            from gateway.channel_directory import build_channel_directory
                            await build_channel_directory(self.adapters)
                        except Exception:
                            pass
                        try:
                            self._schedule_resume_pending_sessions(platform=platform)
                        except Exception:
                            logger.debug('resume-pending reschedule after %s reconnect failed', platform.value, exc_info=True)
                    elif adapter.has_fatal_error and (not adapter.fatal_error_retryable):
                        self._update_platform_runtime_status(platform.value, platform_state='fatal', error_code=adapter.fatal_error_code, error_message=adapter.fatal_error_message)
                        logger.warning('Reconnect %s: non-retryable error (%s), removing from retry queue', platform.value, adapter.fatal_error_message)
                        await _dispose_unused_adapter(adapter)
                        del self._failed_platforms[platform]
                    else:
                        self._update_platform_runtime_status(platform.value, platform_state='retrying', error_code=adapter.fatal_error_code, error_message=adapter.fatal_error_message or 'failed to reconnect')
                        backoff = _reconnect_backoff(attempt)
                        info['attempts'] = attempt
                        info['next_retry'] = time.monotonic() + backoff
                        logger.info('Reconnect %s failed, next retry in %ds', platform.value, backoff)
                        await _dispose_unused_adapter(adapter)
                except Exception as e:
                    if adapter is not None:
                        await _dispose_unused_adapter(adapter)
                    self._update_platform_runtime_status(platform.value, platform_state='retrying', error_code=None, error_message=str(e))
                    backoff = _reconnect_backoff(attempt)
                    info['attempts'] = attempt
                    info['next_retry'] = time.monotonic() + backoff
                    logger.warning('Reconnect %s error: %s, next retry in %ds', platform.value, e, backoff)
            for _ in range(10):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _cancel_secondary_profile_reconnect_tasks(self) -> None:
        """Cancel profile-scoped reconnects before tearing down their registry.

        A reconnect can be waiting in adapter setup while shutdown begins. It
        must not republish an adapter after the secondary registry is drained.
        Waiting is bounded by the same adapter-cleanup budget; if a task does
        not finish in time, the stopped runner state still prevents it from
        installing an adapter when it eventually resumes.
        """
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            return
        current = asyncio.current_task()
        tasks: list[asyncio.Task] = []
        for profile_pending in pending.values():
            if not isinstance(profile_pending, dict):
                continue
            for task in profile_pending.values():
                if isinstance(task, asyncio.Task) and task is not current and (not task.done()):
                    tasks.append(task)
        for task in tasks:
            task.cancel()
        timeout = self._adapter_disconnect_timeout_secs()
        if tasks and timeout > 0:
            _done, unfinished = await asyncio.wait(tasks, timeout=timeout)
            if unfinished:
                logger.warning('Timed out waiting for %d secondary profile reconnect task(s) during shutdown', len(unfinished))
        pending.clear()

    def _start_systemd_watchdog(self) -> bool:
        """Start sd_notify only after a configured gateway is truly running."""
        if not self._running or self.config.systemd_watchdog_seconds <= 0:
            return False
        if self._systemd_watchdog is not None:
            return True
        from gateway.systemd_notify import SystemdWatchdog
        watchdog = SystemdWatchdog(config_enabled=True)
        if not watchdog.start():
            return False
        self._systemd_watchdog = watchdog
        watchdog.ready('Duck Agent Gateway running')
        return True

    async def _stop_systemd_watchdog(self) -> None:
        """Stop heartbeats before any potentially long shutdown drain."""
        watchdog = self._systemd_watchdog
        if watchdog is None:
            return
        self._systemd_watchdog = None
        await watchdog.stop()

    async def stop(self, *, restart: bool=False, detached_restart: bool=False, service_restart: bool=False) -> None:
        """Stop the gateway and disconnect all adapters."""
        _stop_guards = getattr(self, '_stop_loop_liveness_guards', None)
        if callable(_stop_guards):
            _stop_guards()
        if restart:
            self._restart_requested = True
            self._restart_detached = detached_restart
            self._restart_via_service = service_restart
        if self._stop_task is not None:
            await self._stop_task
            return

        async def _stop_impl() -> None:

            def _kill_tool_subprocesses(phase: str) -> None:
                """Kill tool subprocesses + tear down terminal envs + browsers.

                Called twice in the shutdown path: once eagerly after a
                drain timeout forces agent interrupt (so we reclaim bash/
                sleep children before systemd TimeoutStopSec escalates to
                SIGKILL on the cgroup — #8202), and once as a final
                catch-all at the end of _stop_impl() for the graceful
                path or anything respawned mid-teardown.

                All steps are best-effort; exceptions are swallowed so
                one subsystem's failure doesn't block the rest.
                """
                try:
                    from tools.process_registry import process_registry
                    _killed = process_registry.kill_all()
                    if _killed:
                        logger.info('Shutdown (%s): killed %d tool subprocess(es)', phase, _killed)
                except Exception as _e:
                    logger.debug('process_registry.kill_all (%s) error: %s', phase, _e)
                try:
                    from cron.scheduler import mark_running_jobs_interrupted
                    _interrupted = mark_running_jobs_interrupted(f"Gateway shutdown ({phase}) killed the job's tool subprocess before the run finished.")
                    if _interrupted:
                        logger.warning('Shutdown (%s): marked %d in-flight cron job(s) interrupted: %s', phase, len(_interrupted), ', '.join(_interrupted))
                except Exception as _e:
                    logger.debug('mark_running_jobs_interrupted (%s) error: %s', phase, _e)
                try:
                    from tools.async_delegation import interrupt_all as _interrupt_async
                    _async_n = _interrupt_async(reason=f'gateway shutdown ({phase})')
                    if _async_n:
                        logger.info('Shutdown (%s): interrupted %d background delegation(s)', phase, _async_n)
                except Exception as _e:
                    logger.debug('async interrupt_all (%s) error: %s', phase, _e)
                try:
                    from tools.terminal_tool import cleanup_all_environments
                    cleanup_all_environments()
                except Exception as _e:
                    logger.debug('cleanup_all_environments (%s) error: %s', phase, _e)
                try:
                    from tools.browser_tool import cleanup_all_browsers
                    cleanup_all_browsers()
                except Exception as _e:
                    logger.debug('cleanup_all_browsers (%s) error: %s', phase, _e)
            _watchdog_done = threading.Event()
            self._shutdown_watchdog_done = _watchdog_done
            _stop_started_at_box: dict[str, float] = {}

            def _shutdown_watchdog_snapshot() -> dict:
                started = _stop_started_at_box.get('t')
                return {'restart_requested': bool(self._restart_requested), 'draining': bool(self._draining), 'running': bool(self._running), 'active_agents': self._running_agent_count(), 'active_cron_jobs': self._active_cron_job_count(), 'active_api_runs': self._active_api_run_count(), 'restart_drain_timeout': self._restart_drain_timeout, 'watchdog_delay_s': resolve_shutdown_watchdog_delay(self._restart_drain_timeout), 'phase_elapsed_s': time.monotonic() - started if started is not None else None}
            if not os.environ.get('PYTEST_CURRENT_TEST'):
                arm_shutdown_watchdog(resolve_shutdown_watchdog_delay(self._restart_drain_timeout), done_event=_watchdog_done, snapshot_fn=_shutdown_watchdog_snapshot, exit_code=1)
            try:
                await _stop_impl_body(_kill_tool_subprocesses, _stop_started_at_box)
            finally:
                _watchdog_done.set()

        async def _stop_impl_body(_kill_tool_subprocesses, _stop_started_at_box) -> None:
            logger.info('Stopping gateway%s...', ' for restart' if self._restart_requested else '')
            _stop_started_at = time.monotonic()
            _stop_started_at_box['t'] = _stop_started_at

            def _phase_elapsed() -> float:
                return time.monotonic() - _stop_started_at
            self._running = False
            self._draining = True
            stop_watchdog = getattr(self, '_stop_systemd_watchdog', None)
            if callable(stop_watchdog):
                await stop_watchdog()
            await self._cancel_secondary_profile_reconnect_tasks()
            await self._notify_active_sessions_of_shutdown()
            logger.info('Shutdown phase: notify_active_sessions done at +%.2fs', _phase_elapsed())
            timeout = self._restart_drain_timeout
            _pre_drain_keys: list[str] = []
            for _sk, _agent in list(self._running_agents.items()):
                if _agent is _AGENT_PENDING_SENTINEL:
                    continue
                try:
                    await self.async_session_store.mark_resume_pending(_sk, 'restart_timeout' if self._restart_requested else 'shutdown_timeout')
                    _pre_drain_keys.append(_sk)
                except Exception as _e:
                    logger.debug('pre-drain mark_resume_pending failed for %s: %s', _sk, _e)
            _cron_at_start = self._active_cron_job_count()
            _api_at_start = self._active_api_run_count()
            _drain_started_at = time.monotonic()
            active_agents, timed_out = await self._drain_active_agents(timeout)
            logger.info('Shutdown phase: drain done at +%.2fs (drain took %.2fs, timed_out=%s, active_at_start=%d, active_now=%d, cron_at_start=%d, cron_now=%d, api_at_start=%d, api_now=%d)', _phase_elapsed(), time.monotonic() - _drain_started_at, timed_out, len(active_agents), self._running_agent_count(), _cron_at_start, self._active_cron_job_count(), _api_at_start, self._active_api_run_count())
            if not timed_out:
                for _sk in _pre_drain_keys:
                    if _sk not in self._running_agents:
                        try:
                            await self.async_session_store.clear_resume_pending(_sk)
                        except Exception as _e:
                            logger.debug('clear_resume_pending after drain failed for %s: %s', _sk, _e)
            if timed_out:
                logger.warning('Gateway drain timed out after %.1fs with %d active agent(s), %d in-flight cron job(s), and %d api_server run(s); interrupting remaining work.', timeout, self._running_agent_count(), self._active_cron_job_count(), self._active_api_run_count())
                _resume_reason = 'restart_timeout' if self._restart_requested else 'shutdown_timeout'
                for _sk, _agent in list(self._running_agents.items()):
                    if _agent is _AGENT_PENDING_SENTINEL:
                        continue
                    try:
                        await self.async_session_store.mark_resume_pending(_sk, _resume_reason)
                    except Exception as _e:
                        logger.debug('mark_resume_pending failed for %s: %s', _sk, _e)
                self._interrupt_running_agents(_INTERRUPT_REASON_GATEWAY_RESTART if self._restart_requested else _INTERRUPT_REASON_GATEWAY_SHUTDOWN)
                interrupt_deadline = asyncio.get_running_loop().time() + 5.0
                while (self._running_agents or self._active_api_run_count()) and asyncio.get_running_loop().time() < interrupt_deadline:
                    self._update_runtime_status('draining')
                    await asyncio.sleep(0.1)
                if self._running_agents or self._active_api_run_count():
                    self._interrupt_running_agents(_INTERRUPT_REASON_GATEWAY_RESTART if self._restart_requested else _INTERRUPT_REASON_GATEWAY_SHUTDOWN)
                    logger.debug('Re-signaled interrupt for work still live at settle-window exit')
                _kill_tool_subprocesses('post-interrupt')
                logger.info('Shutdown phase: post-interrupt tool kill done at +%.2fs', _phase_elapsed())
            if self._restart_requested and self._restart_detached:
                try:
                    await self._launch_detached_restart_command()
                except Exception as e:
                    logger.error('Failed to launch detached gateway restart: %s', e)
            await self._finalize_shutdown_agents(active_agents)
            _cache_lock = getattr(self, '_agent_cache_lock', None)
            _cache = getattr(self, '_agent_cache', None)
            if _cache_lock is not None and _cache is not None:
                with _cache_lock:
                    _idle_agents = list(_cache.values())
                    _cache.clear()
                for _entry in _idle_agents:
                    _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                    await self._cleanup_agent_resources_off_loop(_agent, context='shutdown idle-cache')
            for platform, adapter in list(self.adapters.items()):
                await self._bounded_adapter_teardown(adapter, platform)
            for _prof, _amap in list(getattr(self, '_profile_adapters', {}).items()):
                for platform, adapter in list(_amap.items()):
                    await self._bounded_adapter_teardown(adapter, platform, profile=_prof)
                _amap.clear()
            if hasattr(self, '_profile_adapters'):
                self._profile_adapters.clear()
            logger.info('Shutdown phase: all adapters disconnected at +%.2fs', _phase_elapsed())
            for _task in list(self._background_tasks):
                if _task is self._stop_task:
                    continue
                if _task is self._restart_task:
                    continue
                _task.cancel()
            self._background_tasks.clear()
            self.adapters.clear()
            for _session_key in list(self._running_agents):
                self._release_running_agent_state(_session_key)
            try:
                from gateway.shutdown_flush import flush_pending_to_file
                flush_pending_to_file(dict(self._pending_messages), reason='shutdown')
            except Exception:
                pass
            self._running_agents.clear()
            self._running_agents_ts.clear()
            if hasattr(self, '_active_session_leases'):
                self._active_session_leases.clear()
            self._pending_messages.clear()
            self._pending_approvals.clear()
            if hasattr(self, '_busy_ack_ts'):
                self._busy_ack_ts.clear()
            self._shutdown_event.set()
            _kill_tool_subprocesses('final-cleanup')
            logger.info('Shutdown phase: final-cleanup tool kill done at +%.2fs', _phase_elapsed())
            try:
                from agent.auxiliary_client import shutdown_cached_clients
                shutdown_cached_clients()
            except Exception as _e:
                logger.debug('shutdown_cached_clients error: %s', _e)
            _self_db = getattr(self, '_session_db', None)
            _self_db = getattr(_self_db, '_db', _self_db)
            for _db in (_self_db, getattr(getattr(self, 'session_store', None), '_db', None)):
                if _db is None or not hasattr(_db, 'close'):
                    continue
                try:
                    _db.close()
                except Exception as _e:
                    logger.debug('SessionDB close error: %s', _e)
            GatewayRunner._shutdown_executor(self)
            logger.info('Shutdown phase: SessionDB close done at +%.2fs', _phase_elapsed())
            from gateway.status import remove_pid_file, release_gateway_runtime_lock
            remove_pid_file()
            release_gateway_runtime_lock()
            if not timed_out:
                try:
                    (_hermes_home / '.clean_shutdown').touch()
                except Exception:
                    pass
            else:
                logger.info('Skipping .clean_shutdown marker — drain timed out with interrupted agents; next startup will suspend recently active sessions.')
            if active_agents:
                self._increment_restart_failure_counts(set(active_agents.keys()))
            if self._restart_requested and self._restart_command_source is None:
                try:
                    atomic_json_write(_planned_restart_notification_path(), {'requested_at': time.time(), 'via_service': bool(self._restart_via_service), 'detached': bool(self._restart_detached)}, indent=None)
                except Exception as e:
                    logger.debug('Failed to write planned restart notification marker: %s', e)
            if self._restart_requested and self._restart_via_service:
                self._launch_systemd_restart_shortcut()
                self._exit_code = GATEWAY_SERVICE_RESTART_EXIT_CODE
                self._exit_reason = self._exit_reason or 'Gateway restart requested'
            self._draining = False
            if getattr(self, '_signal_initiated_shutdown', False) and (not self._restart_requested):
                logger.info('Gateway stopped by an unexpected signal — persisting gateway_state=running so container_boot auto-starts on the next boot (issue #42675)')
                self._update_runtime_status('running', self._exit_reason)
            else:
                self._update_runtime_status('stopped', self._exit_reason)
            _shutdown_gateway_health_export(self)
            logger.info('Gateway stopped (total teardown %.2fs)', _phase_elapsed())
        self._stop_task = asyncio.create_task(_stop_impl())
        await self._stop_task

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    async def _start_secondary_profile_adapters(self) -> int:
        """Bring up adapters for every non-active profile this gateway serves.

        Returns the number of secondary adapters that connected. No-op (returns
        0) unless ``gateway.multiplex_profiles`` is on.

        Each profile's adapters are created and connected under that profile's
        DUCK_AGENT_HOME + secret scope (``_profile_runtime_scope``), stored in
        ``self._profile_adapters[profile]``, and given a message handler that
        stamps ``source.profile`` before delegating to the shared
        ``_handle_message`` — so the agent turn resolves that profile's config,
        skills, and credentials. Same-platform credential collisions (two
        profiles polling the same bot token) are detected and refused here, the
        only point that sees every profile's resolved credentials together.
        """
        if not getattr(self.config, 'multiplex_profiles', False):
            return 0
        try:
            from hermes_cli.profiles import profiles_to_serve, get_active_profile_name
        except Exception:
            return 0
        active = get_active_profile_name() or 'default'
        connected = 0
        claimed: Dict[tuple, str] = {}
        for _plat, _ad in self.adapters.items():
            fp = self._adapter_credential_fingerprint(_ad)
            if fp is not None:
                claimed[_plat, fp] = active
            listener_claim = self._adapter_listener_claim(_plat, _ad)
            if listener_claim is not None:
                claimed[listener_claim] = active
        for retry_info in getattr(self, '_failed_platforms', {}).values():
            for claim_name in ('credential_claim', 'listener_claim'):
                retry_claim = retry_info.get(claim_name)
                if isinstance(retry_claim, tuple):
                    claimed[retry_claim] = active
        for profile_name, profile_home in profiles_to_serve(multiplex=True):
            if profile_name == active:
                continue
            try:
                connected += await self._start_one_profile_adapters(profile_name, profile_home, claimed)
            except SecondaryPortBindingConfigError as e:
                logger.warning("Skipping secondary profile '%s' due to port-binding config error: %s", profile_name, e)
            except MultiplexConfigError:
                raise
            except Exception as e:
                logger.error("Failed to start adapters for profile '%s': %s", profile_name, e, exc_info=True)
        try:
            from gateway.status import write_runtime_status
            from gateway.pairing import PairingStore
            served = [active] + sorted(self._profile_adapters.keys())
            for name in served:
                if name and name not in self.pairing_stores:
                    self.pairing_stores[name] = self.pairing_store if name == active else PairingStore(profile=name)
            write_runtime_status(served_profiles=served)
        except Exception:
            logger.debug('could not record served_profiles', exc_info=True)
        return connected

    async def _start_one_profile_adapters(self, profile_name: str, profile_home: 'Path', claimed: Dict[tuple, str]) -> int:
        """Create+connect one profile's adapters under its runtime scope."""
        from gateway.config import load_gateway_config
        with _profile_runtime_scope(profile_home):
            profile_cfg = load_gateway_config()
            violation = _own_policy_open_startup_violation(profile_cfg)
        if violation:
            raise MultiplexConfigError(f"Profile '{profile_name}' enables {violation}. Enable GATEWAY_ALLOW_ALL_USERS or the platform allow-all flag for that profile, or change dm_policy/group_policy away from 'open'.")
        port_binding_platforms = sorted((platform.value for platform, platform_config in profile_cfg.platforms.items() if platform_config.enabled and _platform_binds_port(platform.value, platform_config.extra)))
        if port_binding_platforms:
            joined = ', '.join(port_binding_platforms)
            raise SecondaryPortBindingConfigError(f"Profile '{profile_name}' enables port-binding platform(s) {joined}, but gateway.multiplex_profiles is on. The default profile owns the single shared HTTP listener and serves every profile through the /p/{profile_name}/ URL prefix. Remove these platform entries from profile '{profile_name}'s config.yaml or configure them only on the default profile.")
        profile_map = self._profile_adapters.setdefault(profile_name, {})
        connected = 0
        for platform, platform_config in profile_cfg.platforms.items():
            if not platform_config.enabled:
                continue
            if getattr(self.config, 'multiplex_profiles', False) and platform is Platform.RELAY:
                continue
            try:
                with _profile_runtime_scope(profile_home):
                    adapter = self._create_adapter(platform, platform_config)
            except Exception as e:
                logger.error("[MULTIPLEX] Profile '%s': _create_adapter('%s') raised %s", profile_name, platform.value, e, exc_info=True)
                continue
            if not adapter:
                logger.warning("[MULTIPLEX] Profile '%s': skipping platform '%s' - adapter creation returned None", profile_name, platform.value)
                continue
            credential_claim = self._adapter_credential_claim(platform, adapter)
            if credential_claim is not None:
                owner = claimed.get(credential_claim)
                if owner is not None:
                    logger.error("Profile '%s' and '%s' both configure %s with the same credential — refusing to start the duplicate (one credential cannot be consumed twice). Give each profile its own %s credential.", owner, profile_name, platform.value, platform.value)
                    continue
            listener_claim = self._adapter_listener_claim(platform, adapter)
            if listener_claim is not None:
                owner = claimed.get(listener_claim)
                if owner is not None:
                    bind, port = listener_claim[-2:]
                    logger.error("Profile '%s' and '%s' both configure %s sidecars on %s:%s — refusing to start the duplicate listener. Set platforms.%s.extra.sidecar_port to a distinct port for profile '%s'.", owner, profile_name, platform.value, bind, port, platform.value, profile_name)
                    continue
            self._configure_profile_adapter(adapter, profile_name, platform)
            try:
                with _profile_runtime_scope(profile_home):
                    success = await self._connect_initial_adapter_with_timeout(adapter, platform)
                if success:
                    profile_map[platform] = adapter
                    if credential_claim is not None:
                        claimed[credential_claim] = profile_name
                    if listener_claim is not None:
                        claimed[listener_claim] = profile_name
                    connected += 1
                    logger.info('✓ %s connected (profile: %s)', platform.value, profile_name)
                else:
                    logger.warning('✗ %s failed to connect (profile: %s)', platform.value, profile_name)
                    await self._safe_adapter_disconnect(adapter, platform)
            except Exception as e:
                logger.error('✗ %s error (profile: %s): %s', platform.value, profile_name, e)
                await self._safe_adapter_disconnect(adapter, platform)
        return connected

    def _configure_profile_adapter(self, adapter: BasePlatformAdapter, profile_name: str, platform: Platform) -> None:
        """Install the profile-scoped handlers shared by startup and reconnect."""
        adapter.set_message_handler(self._make_profile_message_handler(profile_name))
        adapter.set_fatal_error_handler(self._make_profile_fatal_error_handler(profile_name, platform))
        adapter.set_session_store(self.session_store)
        adapter.set_busy_session_handler(self._handle_active_session_busy_message)
        _set_reaction = getattr(adapter, 'set_reaction_handler', None)
        if callable(_set_reaction):
            _set_reaction(self._handle_reaction_event)
        adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
        adapter.set_authorization_check(self._make_adapter_auth_check(platform, profile_name=profile_name))
        adapter._busy_text_mode = self._busy_text_mode

    async def _run_secondary_profile_reconnect(self, profile_name: str, platform: Platform) -> None:
        """Reconnect a retryable secondary adapter under its own profile scope."""
        attempts = 0
        current_task = asyncio.current_task()
        try:
            while self._running:
                adapter = None
                try:
                    from hermes_cli.profiles import get_profile_dir
                    from gateway.config import load_gateway_config
                    profile_home = get_profile_dir(profile_name)
                    with _profile_runtime_scope(profile_home):
                        profile_config = load_gateway_config().platforms.get(platform)
                        if profile_config is None or not profile_config.enabled:
                            return
                        adapter = self._create_adapter(platform, profile_config)
                        if adapter is None:
                            logger.warning('Secondary %s reconnect skipped: adapter unavailable (profile: %s)', platform.value, profile_name)
                            return
                        self._configure_profile_adapter(adapter, profile_name, platform)
                        success = await self._connect_adapter_with_timeout(adapter, platform, is_reconnect=True)
                    if success and self._running:
                        profile_map = self._profile_adapters.setdefault(profile_name, {})
                        if platform not in profile_map:
                            profile_map[platform] = adapter
                            self._sync_voice_mode_state_to_adapter(adapter)
                            logger.info('✓ %s reconnected (profile: %s)', platform.value, profile_name)
                            return
                        await self._safe_adapter_disconnect(adapter, platform)
                        return
                    if success:
                        await self._safe_adapter_disconnect(adapter, platform)
                        return
                    await self._safe_adapter_disconnect(adapter, platform)
                    if getattr(adapter, 'has_fatal_error', False) and (not getattr(adapter, 'fatal_error_retryable', True)):
                        return
                except asyncio.CancelledError:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    raise
                except Exception:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    logger.debug('Secondary %s reconnect attempt failed (profile: %s)', platform.value, profile_name, exc_info=True)
                if not self._running:
                    return
                attempts += 1
                backoff = _reconnect_backoff(attempts)
                logger.info('Secondary %s reconnect retry in %ds (profile: %s)', platform.value, backoff, profile_name)
                await asyncio.sleep(backoff)
        finally:
            pending = self._profile_failed_platforms
            if isinstance(pending, dict):
                profile_pending = pending.get(profile_name)
                task = profile_pending.get(platform) if isinstance(profile_pending, dict) else None
                if not isinstance(task, asyncio.Task) or task is current_task:
                    if isinstance(profile_pending, dict):
                        profile_pending.pop(platform, None)
                        if not profile_pending:
                            pending.pop(profile_name, None)

    def _schedule_secondary_profile_reconnect(self, profile_name: str, platform: Platform, adapter: BasePlatformAdapter) -> None:
        """Schedule one runner-owned reconnect without sharing primary secrets."""
        if not self._running or not adapter.fatal_error_retryable:
            return
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            pending = {}
            self._profile_failed_platforms = pending
        profile_pending = pending.setdefault(profile_name, {})
        if platform in profile_pending:
            return
        task = asyncio.create_task(self._run_secondary_profile_reconnect(profile_name, platform), name=f'secondary-reconnect:{profile_name}:{platform.value}')
        profile_pending[platform] = task
        background_tasks = getattr(self, '_background_tasks', None)
        if not isinstance(background_tasks, set):
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    def _make_profile_fatal_error_handler(self, profile_name: str, platform: Platform) -> Callable[[BasePlatformAdapter], Awaitable[None]]:
        """Route a secondary-profile fatal error to that profile's reconnect slot."""

        async def _handler(adapter: BasePlatformAdapter) -> None:
            await self._handle_profile_adapter_fatal_error(profile_name, platform, adapter)
        return _handler

    async def _handle_profile_adapter_fatal_error(self, profile_name: str, platform: Platform, adapter: BasePlatformAdapter) -> None:
        """Remove a failed multiplexed adapter without touching the primary slot.

        Secondary adapters are owned by ``_profile_adapters`` rather than
        ``self.adapters``. The primary-only fatal handler intentionally ignores
        them; without this route, a fatal secondary Discord client stayed live
        forever after its liveness sampler stopped.
        """
        profile_map = getattr(self, '_profile_adapters', {}).get(profile_name)
        if not isinstance(profile_map, dict) or profile_map.get(platform) is not adapter:
            logger.debug('Ignoring stale fatal error from secondary %s adapter (profile: %s)', platform.value, profile_name)
            return
        profile_map.pop(platform, None)
        await self._safe_adapter_disconnect(adapter, platform)
        if not self._running:
            return
        self._schedule_secondary_profile_reconnect(profile_name, platform, adapter)
        logger.error('Fatal %s adapter error for multiplexed profile %s (%s)', platform.value, profile_name, adapter.fatal_error_code or 'unknown')

    def _make_profile_message_handler(self, profile_name: str):
        """Return a message handler that stamps source.profile then delegates.

        Auth runs inside ``_handle_message`` *before* the agent-turn scope is
        installed. For secondary profiles under multiplex, wrap the whole
        handler in ``_profile_runtime_scope`` so allowlists/tokens from that
        profile's ``.env`` are visible to ``get_secret`` / authz.
        """
        from hermes_cli.profiles import get_profile_dir
        try:
            profile_home = get_profile_dir(profile_name)
        except Exception:
            profile_home = None

        async def _handler(event):
            try:
                if getattr(event, 'source', None) is not None and (not event.source.profile):
                    event.source.profile = profile_name
            except Exception:
                pass
            if profile_home is not None:
                with _profile_runtime_scope(profile_home):
                    return await self._handle_message(event)
            return await self._handle_message(event)
        return _handler

    def _make_default_profile_message_handler(self):
        """Scope a multiplexed default-profile message from ingress onward."""
        profile_home = Path(get_hermes_home())

        async def _handler(event):
            with _profile_runtime_scope(profile_home):
                return await self._handle_message(event)
        return _handler

    def _primary_message_handler(self):
        """Return the correctly scoped handler for a primary adapter."""
        if getattr(self.config, 'multiplex_profiles', False):
            return self._make_default_profile_message_handler()
        return self._handle_message

    @staticmethod
    def _adapter_credential_claim(platform: Platform, adapter: Any) -> Optional[tuple]:
        """Return the exclusive credential resource claimed by an adapter."""
        fingerprint = GatewayRunner._adapter_credential_fingerprint(adapter)
        if fingerprint is None:
            return None
        return (platform, fingerprint)

    @staticmethod
    def _adapter_listener_claim(platform: Platform, adapter: Any) -> Optional[tuple]:
        """Return the exclusive listener resource claimed by an adapter.

        Photon sidecars are per-profile processes. Even when two profiles use
        different project credentials, their sidecars cannot share a bind and
        port. Represent that endpoint as a claim so multiplex startup rejects
        the later adapter before either ``connect()`` or ``disconnect()`` can
        disturb the first profile.
        """
        if getattr(platform, 'value', None) != 'photon':
            return None
        bind = getattr(adapter, '_sidecar_bind', None)
        port = getattr(adapter, '_sidecar_port', None)
        if not isinstance(bind, str) or not bind.strip():
            return None
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        return ('listener', 'photon', bind.strip().lower(), port)

    @staticmethod
    def _adapter_credential_fingerprint(adapter: Any) -> Optional[str]:
        """Return a stable, log-safe fingerprint of an adapter's credential.

        Used only to detect two profiles claiming the same platform credential.
        Returns a salted hash (never the credential itself) of the adapter's
        primary credential, or None when no credential is discoverable (in
        which case we don't attempt conflict detection for it).
        """
        token = None
        for attr in ('token', 'bot_token', '_token', 'api_token', '_bot_token', '_project_secret'):
            val = getattr(adapter, attr, None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
                break
        if not token:
            cfg = getattr(adapter, 'config', None)
            if cfg is not None:
                for attr in ('token', 'bot_token'):
                    val = getattr(cfg, attr, None)
                    if isinstance(val, str) and val.strip():
                        token = val.strip()
                        break
        if not token:
            config = getattr(adapter, 'config', None)
            val = getattr(config, 'token', None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
        if not token:
            return None
        import hashlib
        return hashlib.sha256(('duck-agent-mux:' + token).encode('utf-8')).hexdigest()[:16]

    def _create_adapter(self, platform: Platform, config: Any) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform.

        Checks the platform_registry first (plugin adapters), then falls
        through to the built-in if/elif chain for core platforms.
        """
        if hasattr(config, 'extra') and isinstance(config.extra, dict):
            config.extra.setdefault('group_sessions_per_user', self.config.group_sessions_per_user)
            config.extra.setdefault('thread_sessions_per_user', getattr(self.config, 'thread_sessions_per_user', False))
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(platform.value):
                adapter = platform_registry.create_adapter(platform.value, config)
                if adapter is not None:
                    adapter.gateway_runner = self
                    return adapter
                logger.error("Platform '%s' is registered but adapter creation failed (check dependencies and config)", platform.value)
                return None
        except Exception as e:
            logger.debug("Platform registry lookup for '%s' failed: %s", platform.value, e)
        if platform == Platform.WHATSAPP_CLOUD:
            from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter, check_whatsapp_cloud_requirements
            if not check_whatsapp_cloud_requirements():
                logger.warning('WhatsApp Cloud: aiohttp/httpx missing — reinstall duck-agent')
                return None
            return WhatsAppCloudAdapter(config)
        elif platform == Platform.SIGNAL:
            from gateway.platforms.signal import SignalAdapter, check_signal_requirements, validate_signal_config
            if not check_signal_requirements():
                logger.warning('Signal: runtime requirements not met')
                return None
            if not validate_signal_config(config):
                logger.warning('Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured')
                return None
            return SignalAdapter(config)
        elif platform == Platform.WEIXIN:
            from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
            if not check_weixin_requirements():
                logger.warning('Weixin: aiohttp/cryptography not installed')
                return None
            return WeixinAdapter(config)
        elif platform == Platform.API_SERVER:
            from gateway.platforms.api_server import APIServerAdapter, check_api_server_requirements
            if not check_api_server_requirements():
                logger.warning('API Server: aiohttp not installed')
                return None
            adapter = APIServerAdapter(config)
            adapter.gateway_runner = self
            return adapter
        elif platform == Platform.WEBHOOK:
            from gateway.platforms.webhook import WebhookAdapter, check_webhook_requirements
            if not check_webhook_requirements():
                logger.warning('Webhook: aiohttp not installed')
                return None
            adapter = WebhookAdapter(config)
            adapter.gateway_runner = self
            return adapter
        elif platform == Platform.MSGRAPH_WEBHOOK:
            from gateway.platforms.msgraph_webhook import MSGraphWebhookAdapter, check_msgraph_webhook_requirements
            if not check_msgraph_webhook_requirements():
                logger.warning('MSGraph webhook: aiohttp not installed')
                return None
            return MSGraphWebhookAdapter(config)
        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning('BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured')
                return None
            return BlueBubblesAdapter(config)
        elif platform == Platform.QQBOT:
            from gateway.platforms.qqbot import QQAdapter, check_qq_requirements
            if not check_qq_requirements():
                logger.warning('QQBot: aiohttp/httpx missing or QQ_APP_ID/QQ_CLIENT_SECRET not configured')
                return None
            return QQAdapter(config)
        elif platform == Platform.YUANBAO:
            from gateway.platforms.yuanbao import YuanbaoAdapter, WEBSOCKETS_AVAILABLE
            if not WEBSOCKETS_AVAILABLE:
                logger.warning('Yuanbao: websockets not installed. Run: pip install websockets')
                return None
            return YuanbaoAdapter(config)
        return None

    def _make_adapter_auth_check(self, platform: Platform, profile_name: Optional[str]=None) -> Callable[[str, Optional[str], Optional[str]], bool]:
        """Build a platform-bound auth callback for adapter use.

        Adapters that fetch external context (e.g. Slack
        ``conversations.replies``) call this through
        ``BasePlatformAdapter._is_sender_authorized`` to mark non-allowlisted
        senders as unverified in LLM context, mitigating indirect prompt
        injection from third parties in shared threads/channels.

        The returned callback delegates to :meth:`_is_user_authorized` so the
        full auth chain — platform allowlists, group allowlists, pairing
        store, allow-all flags — stays the single source of truth.

        ``profile_name`` binds the callback to the secondary adapter's own
        multiplex profile, so its ``SessionSource`` resolves that profile's
        secret scope instead of falling back to the active profile.
        """

        def check(user_id: str, chat_type: Optional[str]=None, chat_id: Optional[str]=None) -> bool:
            if not user_id:
                return False
            source = SessionSource(platform=platform, chat_id=chat_id or '', chat_type=chat_type or 'group', user_id=user_id, profile=profile_name)
            return self._is_user_authorized(source)
        return check

    async def _deliver_platform_notice(self, source, content: str) -> None:
        """Deliver a setup/operational notice using platform-specific privacy rules."""
        adapter = self._adapter_for_source(source)
        if not adapter:
            return
        config = getattr(self, 'config', None)
        if config and getattr(source, 'platform', None) == Platform.SLACK and _is_slack_ignored_channel(config, getattr(source, 'chat_id', None)):
            logger.info('Skipping Slack platform notice for configured ignored channel %s', getattr(source, 'chat_id', None))
            return
        notice_delivery = 'public'
        if config and hasattr(config, 'get_notice_delivery'):
            notice_delivery = config.get_notice_delivery(source.platform)
        metadata = self._thread_metadata_for_source(source)
        if notice_delivery == 'private' and getattr(source, 'user_id', None):
            try:
                result = await adapter.send_private_notice(source.chat_id, source.user_id, content, metadata=metadata)
                if getattr(result, 'success', False):
                    return
            except Exception:
                logger.debug('[%s] send_private_notice failed, falling back to public', getattr(source, 'platform', '?'), exc_info=True)
        await adapter.send(source.chat_id, content, metadata=metadata)

    async def _resolve_async_delegation_session(self, session_entry: SessionEntry, pinned_session_id: str) -> Optional[SessionEntry]:
        """Resolve an async completion to its verified owning gateway session.

        A compression rotation ends the physical parent row while continuing
        the same logical conversation in a child.  Follow that lineage, but
        never let a late completion override an unrelated /new or restored
        route.  Unknown ownership remains fail-closed; the result is still
        available in the delegation records.
        """
        session_db = cast(Any, self._session_db)
        if session_db is None:
            logger.warning('Async-delegation completion has no session database; dropping injection (#55578 fail-closed).')
            return None
        pinned_row = None
        try:
            pinned_row = await session_db.get_session(pinned_session_id)
        except Exception:
            logger.debug('Async-delegation parent lookup failed for %s', pinned_session_id, exc_info=True)
        if pinned_row is None:
            logger.warning('Async-delegation completion has unknown spawning session %s; dropping injection (#55578 fail-closed).', pinned_session_id)
            return None
        target_session_id = pinned_session_id
        follows_compression = False
        if pinned_row.get('ended_at'):
            if pinned_row.get('end_reason') != 'compression':
                logger.warning('Async-delegation completion pinned to ended session %s (end_reason=%r); dropping injection instead of resurrecting it (#55578 fail-closed).', pinned_session_id, pinned_row.get('end_reason'))
                return None
            follows_compression = True
            try:
                target_session_id = await session_db.get_compression_tip(pinned_session_id)
            except Exception:
                logger.debug('Async-delegation compression-tip lookup failed for %s', pinned_session_id, exc_info=True)
                target_session_id = None
            if not target_session_id or target_session_id == pinned_session_id:
                logger.warning('Async-delegation completion pinned to compressed session %s without a continuation; dropping injection.', pinned_session_id)
                return None
            try:
                tip_row = await session_db.get_session(target_session_id)
            except Exception:
                tip_row = None
            if tip_row is None or tip_row.get('ended_at'):
                logger.warning('Async-delegation compression continuation %s is %s; dropping injection.', target_session_id, 'unknown' if tip_row is None else 'ended')
                return None
            route_owns_lineage = session_entry.session_id in {pinned_session_id, target_session_id}
            if not route_owns_lineage:
                try:
                    route_row = await session_db.get_session(session_entry.session_id)
                    route_tip = await session_db.get_compression_tip(session_entry.session_id) if route_row is not None and route_row.get('ended_at') and (route_row.get('end_reason') == 'compression') else None
                except Exception:
                    route_tip = None
                route_owns_lineage = route_tip == target_session_id
            if not route_owns_lineage:
                logger.warning('Async-delegation completion for compression lineage %s -> %s does not own current route %s; dropping injection.', pinned_session_id, target_session_id, session_entry.session_id)
                return None
        if target_session_id == session_entry.session_id:
            return session_entry
        prior_session_id = session_entry.session_id
        if follows_compression:
            switched = await self.async_session_store.advance_compression_session(session_entry.session_key, prior_session_id, target_session_id)
        else:
            switched = await self.async_session_store.switch_session(session_entry.session_key, target_session_id)
        if switched is None:
            logger.warning('Async-delegation completion could not bind routing key %s to owning session %s; dropping injection.', session_entry.session_key, target_session_id)
            return None
        logger.info('Pinned async-delegation completion to owning session %s (was %s) for routing key %s (#57498)', target_session_id, prior_session_id, session_entry.session_key)
        return switched
    _BUSY_REJECT_TEXT: Dict[str, str] = {'model': 'Agent is running — wait or /stop first, then switch models.', 'codex-runtime': 'Agent is running — wait or /stop first, then change runtime.', 'moa': 'Agent is running — wait or /stop first, then run /moa.'}

    async def _dispatch_busy_slash_command(self, event: MessageEvent, cmd_def, quick_key: str, source):
        """Dispatch a recognized slash command while an agent is running.

        Resolution order:
          1. ``busy_handler`` — special mid-run variant (e.g. /goal's
             control-verb whitelist, /queue's FIFO enqueue, /model's
             custom reject text).
          2. ``busy_policy == "dispatch"`` — the command's normal handler.
          3. Catch-all busy-reject text. Rejecting is required rather than
             falling through to interrupt + discard: commands like /model,
             /reasoning, /voice, /insights, /title, /resume, /retry,
             /undo, /compress, /usage, /reload-mcp, /sethome, /reset (all
             registered as Discord slash commands) would interrupt the
             agent AND get silently discarded by the slash-command safety
             net, producing a zero-char response. See #5057, #6252, #10370.
        """
        name = cmd_def.name
        policy = getattr(cmd_def, 'busy_policy', 'reject')
        handler_key = getattr(cmd_def, 'busy_handler', None)
        if handler_key:
            special = {'start': self._busy_start_command, 'stop': self._busy_stop_command, 'new': self._busy_new_command, 'queue': self._busy_queue_command, 'steer': self._busy_steer_command, 'egress': self._busy_egress_command, 'goal': self._busy_goal_command}.get(handler_key)
            if special is not None:
                return await special(event, quick_key, source)
            reject_text = self._BUSY_REJECT_TEXT.get(handler_key)
            if reject_text is not None:
                return reject_text
        if policy in ('dispatch', 'interrupt_then_dispatch'):
            plain = {'status': self._handle_status_command, 'context': self._handle_context_command, 'restart': self._handle_restart_command, 'approve': self._handle_approve_command, 'deny': self._handle_deny_command, 'agents': self._handle_agents_command, 'background': self._handle_background_command, 'kanban': self._handle_kanban_command, 'subgoal': self._handle_subgoal_command, 'heartbeat': self._handle_heartbeat_command, 'yolo': self._handle_yolo_command, 'verbose': self._handle_verbose_command, 'footer': self._handle_footer_command, 'help': self._handle_help_command, 'commands': self._handle_commands_command, 'profile': self._handle_profile_command, 'update': self._handle_update_command, 'version': self._handle_version_command}.get(name)
            if plain is not None:
                return await plain(event)
            logger.warning('busy_policy=%s for /%s has no mid-run handler — falling back to busy-reject', policy, name)
        return f"⏳ Agent is running — `/{name}` can't run mid-turn. Wait for the current response or `/stop` first."

    async def _busy_start_command(self, event: MessageEvent, quick_key: str, source):
        logger.info('Ignoring /start platform ping for active session %s', quick_key)
        return ''

    async def _busy_egress_command(self, event: MessageEvent, quick_key: str, source):
        from hermes_cli.proxy_cli import format_status_text
        return format_status_text()

    async def _busy_stop_command(self, event: MessageEvent, quick_key: str, source):
        await self._interrupt_and_clear_session(quick_key, source, interrupt_reason=_INTERRUPT_REASON_STOP, invalidation_reason='stop_command')
        logger.info('STOP for session %s — agent interrupted, session lock released', quick_key)
        return EphemeralReply(t('gateway.stop.stopped'))

    async def _busy_new_command(self, event: MessageEvent, quick_key: str, source):
        await self._interrupt_and_clear_session(quick_key, source, interrupt_reason=_INTERRUPT_REASON_RESET, invalidation_reason='new_command')
        return await self._handle_reset_command(event)

    async def _busy_queue_command(self, event: MessageEvent, quick_key: str, source):
        queued_text = event.get_command_args().strip()
        has_media = bool(getattr(event, 'media_urls', None))
        if not queued_text and (not has_media):
            return 'Usage: /queue <prompt>'
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(text=queued_text, message_type=event.message_type if has_media else MessageType.TEXT, source=event.source, raw_message=event.raw_message, message_id=event.message_id, media_urls=list(getattr(event, 'media_urls', []) or []), media_types=list(getattr(event, 'media_types', []) or []), reply_to_message_id=event.reply_to_message_id, reply_to_text=event.reply_to_text, reply_to_author_id=event.reply_to_author_id, reply_to_author_name=event.reply_to_author_name, reply_to_is_own_message=event.reply_to_is_own_message, auto_skill=event.auto_skill, channel_prompt=event.channel_prompt, channel_context=event.channel_context, internal=event.internal, timestamp=event.timestamp)
            self._enqueue_fifo(quick_key, queued_event, adapter)
        depth = self._queue_depth(quick_key, adapter=self._adapter_for_source(source))
        if depth <= 1:
            return 'Queued for the next turn.'
        return f'Queued for the next turn. ({depth} queued)'

    async def _busy_steer_command(self, event: MessageEvent, quick_key: str, source):
        steer_text = event.get_command_args().strip()
        if not steer_text:
            return 'Usage: /steer <prompt>'
        _steer_state = self._peek_session_state(quick_key)
        running_agent = _steer_state.turn.agent if _steer_state else None
        if running_agent is _AGENT_PENDING_SENTINEL:
            adapter = self._adapter_for_source(source)
            if adapter:
                queued_event = MessageEvent(text=steer_text, message_type=MessageType.TEXT, source=event.source, message_id=event.message_id, channel_prompt=event.channel_prompt, channel_context=event.channel_context)
                self._enqueue_fifo(quick_key, queued_event, adapter)
            return 'Agent still starting — /steer queued for the next turn.'
        if running_agent and hasattr(running_agent, 'steer'):
            try:
                accepted = running_agent.steer(steer_text)
            except Exception as exc:
                logger.warning('Steer failed for session %s: %s', quick_key, exc)
                return f'⚠️ Steer failed: {exc}'
            if accepted:
                preview = steer_text[:60] + ('...' if len(steer_text) > 60 else '')
                return f"⏩ Steer queued — arrives after the next tool call: '{preview}'"
            return 'Steer rejected (empty payload).'
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(text=steer_text, message_type=MessageType.TEXT, source=event.source, message_id=event.message_id, channel_prompt=event.channel_prompt, channel_context=event.channel_context)
            self._enqueue_fifo(quick_key, queued_event, adapter)
        return 'No active agent — /steer queued for the next turn.'

    async def _busy_goal_command(self, event: MessageEvent, quick_key: str, source):
        _goal_arg = (event.get_command_args() or '').strip().lower()
        _goal_verb = _goal_arg.split(None, 1)[0] if _goal_arg else ''
        _is_control = not _goal_arg or _goal_arg in {'status', 'pause', 'resume', 'clear', 'stop', 'done', 'unwait'} or _goal_verb in {'wait', 'gate'}
        if _is_control:
            return await self._handle_goal_command(event)
        return 'Agent is running — use /goal status / pause / clear / wait mid-run, or /stop before setting a new goal.'

    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        Handle an incoming message from any platform.
        
        This is the core message processing pipeline:
        1. Check user authorization
        2. Check for commands (/new, /reset, etc.)
        3. Check for running agent and interrupt if needed
        4. Get or create session
        5. Build context for agent
        6. Run agent conversation
        7. Return response
        """
        source = event.source
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug('reset_session_vars failed at handler entry', exc_info=True)
        is_internal = bool(getattr(event, 'internal', False))
        if not is_internal and getattr(source, 'platform', None) == Platform.SLACK and _is_slack_ignored_channel(getattr(self, 'config', None), getattr(source, 'chat_id', None)):
            logger.info('Dropping Slack message from configured ignored channel %s', getattr(source, 'chat_id', None))
            return None
        if getattr(self, '_startup_restore_in_progress', False) and (not is_internal) and (not getattr(event, '_hermes_startup_restore_replay', False)):
            self._queue_startup_restore_event(event)
            return None
        if not is_internal:
            self._scale_to_zero_note_real_inbound()
        if not is_internal:
            try:
                from hermes_cli.lifecycle import invoke_hook as _invoke_hook
                _hook_results = _invoke_hook('pre_gateway_dispatch', event=event, gateway=self, session_store=getattr(self, 'session_store', None))
            except Exception as _hook_exc:
                logger.warning('pre_gateway_dispatch invocation failed: %s', _hook_exc)
                _hook_results = []
            for _result in _hook_results:
                if not isinstance(_result, dict):
                    continue
                _action = _result.get('action')
                if _action == 'skip':
                    logger.info('pre_gateway_dispatch skip: reason=%s platform=%s chat=%s', _result.get('reason'), source.platform.value if source.platform else 'unknown', source.chat_id or 'unknown')
                    return None
                if _action == 'rewrite':
                    _new_text = _result.get('text')
                    if isinstance(_new_text, str):
                        event = dataclasses.replace(event, text=_new_text)
                        source = event.source
                    break
                if _action == 'allow':
                    break
        if is_internal:
            pass
        elif source.user_id is None:
            if not self._is_user_authorized(source):
                logger.debug('Ignoring message with no user_id from %s', source.platform.value)
                return None
        elif not self._is_user_authorized(source):
            logger.warning('Unauthorized user: %s (%s) on %s', source.user_id, source.user_name, source.platform.value)
            if source.chat_type == 'dm' and self._get_unauthorized_dm_behavior(source.platform, profile=source.profile) == 'pair':
                platform_name = source.platform.value if source.platform else 'unknown'
                pairing_store = self._pairing_store_for(source)
                if pairing_store is None:
                    logger.error('Cannot offer pairing code on %s: no pairing store', platform_name)
                    return None
                if pairing_store._is_rate_limited(platform_name, source.user_id):
                    return None
                code = pairing_store.generate_code(platform_name, source.user_id, source.user_name or '')
                if code:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        store_profile = getattr(pairing_store, 'profile', None)
                        profile_arg = f'-p {store_profile} ' if isinstance(store_profile, str) and store_profile and (store_profile != 'default') else ''
                        await adapter.send(source.chat_id, f"Hi~ I don't recognize you yet!\n\nHere's your pairing code: `{code}`\n\nAsk the bot owner to run:\n`duck-agent {profile_arg}pairing approve {platform_name} {code}`")
                else:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        await adapter.send(source.chat_id, 'Too many pairing requests right now~ Please try again later!')
                    pairing_store._record_rate_limit(platform_name, source.user_id)
            return None
        if not is_internal:
            try:
                from agent.estop import paused_reply as _estop_paused_reply
                _paused_notice = _estop_paused_reply()
            except ImportError:
                _paused_notice = None
            if _paused_notice is not None:
                logger.info('Gateway turn paused by global emergency stop (platform=%s chat=%s)', getattr(getattr(source, 'platform', None), 'value', 'unknown'), getattr(source, 'chat_id', None) or 'unknown')
                return _paused_notice
        _quick_key = self._session_key_for_source(source)
        _up_state = self._peek_session_state(_quick_key)
        if _up_state is not None and _up_state.persistent.update_prompt_pending:
            raw = (event.text or '').strip()
            cmd = event.get_command()
            if cmd in {'approve', 'yes'}:
                response_text = 'y'
            elif cmd in {'deny', 'no'}:
                response_text = 'n'
            else:
                _recognized_cmd = None
                if cmd:
                    try:
                        from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    except Exception:
                        _resolve_update_cmd = None
                    if _resolve_update_cmd is not None:
                        try:
                            _cmd_def = _resolve_update_cmd(cmd)
                            _recognized_cmd = _cmd_def.name if _cmd_def else None
                        except Exception:
                            _recognized_cmd = None
                if _recognized_cmd:
                    response_text = ''
                else:
                    response_text = raw
            if response_text:
                response_path = _hermes_home / '.update_response'
                prompt_path = _hermes_home / '.update_prompt.json'
                try:
                    tmp = response_path.with_suffix('.tmp')
                    tmp.write_text(response_text, encoding='utf-8')
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning('Failed to write update response: %s', e)
                    return f'✗ Failed to send response to update process: {e}'
                _up_state.persistent.update_prompt_pending = False
                label = response_text if len(response_text) <= 20 else response_text[:20] + '…'
                return f'✓ Sent `{label}` to the update process.'
            if _recognized_cmd:
                response_path = _hermes_home / '.update_response'
                prompt_path = _hermes_home / '.update_prompt.json'
                try:
                    tmp = response_path.with_suffix('.tmp')
                    tmp.write_text('', encoding='utf-8')
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                    logger.info('Recognized /%s during pending update prompt for %s; cancelled prompt with default and dispatching command', _recognized_cmd, _quick_key)
                except OSError as e:
                    logger.warning('Failed to write cancel response for pending update prompt: %s', e)
                _up_state.persistent.update_prompt_pending = False
        _clarify_mod = None
        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(_quick_key, include_choice_prompts=True)
        except Exception:
            _pending_clarify = None
        if _pending_clarify is not None and _clarify_mod is not None:
            _clarify_has_audio = bool(self._pending_event_audio_paths(event))
            _raw_clarify_reply = await self._prepare_clarify_reply_text(event)
            if _clarify_has_audio and (not _raw_clarify_reply):
                logger.info('Gateway retained pending clarify after voice transcription produced no usable text (session=%s, id=%s)', _quick_key, _pending_clarify.clarify_id)
                return ''
            if _raw_clarify_reply and (not _raw_clarify_reply.startswith('/')):
                _resolved = _clarify_mod.resolve_text_response_for_session(_quick_key, _raw_clarify_reply)
                if _resolved:
                    logger.info('Gateway intercepted clarify text response (session=%s, id=%s)', _quick_key, _pending_clarify.clarify_id)
                    _clarify_adapter = self._adapter_for_source(source)
                    if _clarify_adapter:
                        try:
                            _clarify_adapter.resume_typing_for_chat(source.chat_id)
                        except Exception:
                            logger.debug('Failed to resume typing after clarify response', exc_info=True)
                    return ''
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        _tool_approval_live = False
        try:
            from tools.approval import has_blocking_approval
            _tool_approval_live = has_blocking_approval(_quick_key)
        except Exception:
            _tool_approval_live = False
        if _pending_confirm and (not _tool_approval_live):
            _raw_reply = (event.text or '').strip()
            _norm_reply = _raw_reply.lstrip('!/').lower()
            _cmd_reply = event.get_command()
            _confirm_choice = None
            if _cmd_reply in {'approve', 'yes', 'ok', 'confirm'}:
                _confirm_choice = 'once'
            elif _cmd_reply in {'always', 'remember'}:
                _confirm_choice = 'always'
            elif _cmd_reply in {'cancel', 'no', 'deny', 'nevermind'}:
                _confirm_choice = 'cancel'
            elif _norm_reply in {'approve', 'approve once', 'once'}:
                _confirm_choice = 'once'
            elif _norm_reply in {'always', 'always approve'}:
                _confirm_choice = 'always'
            elif _norm_reply in {'cancel', 'nevermind', 'no'}:
                _confirm_choice = 'cancel'
            if _confirm_choice is not None:
                _resolved = await _slash_confirm_mod.resolve(_quick_key, _pending_confirm.get('confirm_id'), _confirm_choice)
                return _resolved or ''
            _slash_confirm_mod.clear_if_stale(_quick_key)
        _raw_stale_timeout = _float_env('HERMES_AGENT_TIMEOUT', 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is not None and _quick_state.turn.agent is not None and _stale_ts:
            _stale_age = time.time() - _stale_ts
            _stale_agent = _quick_state.turn.agent
            _stale_idle = float('inf')
            _stale_detail = ''
            if _stale_agent and hasattr(_stale_agent, 'get_activity_summary'):
                try:
                    _sa = _stale_agent.get_activity_summary()
                    _stale_idle = _sa.get('seconds_since_activity', float('inf'))
                    _stale_detail = f" | last_activity={_sa.get('last_activity_desc', 'unknown')} ({_stale_idle:.0f}s ago) | iteration={_sa.get('api_call_count', 0)}/{_sa.get('max_iterations', 0)}"
                except Exception:
                    pass
            _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float('inf')
            _should_evict = _stale_agent is not _AGENT_PENDING_SENTINEL and (_raw_stale_timeout > 0 and _stale_idle >= _raw_stale_timeout or _stale_age > _wall_ttl)
            if _should_evict:
                logger.warning('Evicting stale _running_agents entry for %s (age: %.0fs, idle: %.0fs, timeout: %.0fs)%s', _quick_key, _stale_age, _stale_idle, _raw_stale_timeout, _stale_detail)
                self._invalidate_session_run_generation(_quick_key, reason='stale_running_agent_eviction')
                self._release_running_agent_state(_quick_key)
        if self._is_session_running(_quick_key):
            from hermes_cli.commands import resolve_command as _resolve_cmd_inner
            _evt_cmd = event.get_command()
            _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None
            if _cmd_def_inner and _cmd_def_inner.name == 'status':
                return await self._handle_status_command(event)
            if _cmd_def_inner and _cmd_def_inner.name == 'context':
                return await self._handle_context_command(event)
            if _evt_cmd and _cmd_def_inner is not None:
                _denied = self._check_slash_access(source, _cmd_def_inner.name)
                if _denied is not None:
                    return _denied
            if _cmd_def_inner:
                return await self._dispatch_busy_slash_command(event, _cmd_def_inner, _quick_key, source)
            if event.message_type == MessageType.PHOTO:
                logger.debug('PRIORITY photo follow-up for session %s — queueing without interrupt', _quick_key)
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(adapter._pending_messages, _quick_key, event)
                return None
            _telegram_followup_grace = float(os.getenv('HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS', '3.0'))
            _grace_state = self._peek_session_state(_quick_key)
            _started_at = _grace_state.turn.started_ts if _grace_state else 0
            if source.platform == Platform.TELEGRAM and event.message_type == MessageType.TEXT and (_telegram_followup_grace > 0) and _started_at and (time.time() - _started_at <= _telegram_followup_grace):
                logger.debug('Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt', time.time() - _started_at, _quick_key)
                adapter = self._adapter_for_source(source)
                if adapter:
                    if self._busy_input_mode == 'queue':
                        self._enqueue_fifo(_quick_key, event, adapter)
                    else:
                        merge_pending_message_event(adapter._pending_messages, _quick_key, event, merge_text=True)
                return None
            _ra_state = self._peek_session_state(_quick_key)
            running_agent = _ra_state.turn.agent if _ra_state else None
            if running_agent is _AGENT_PENDING_SENTINEL:
                if event.get_command() == 'stop':
                    self._release_running_agent_state(_quick_key)
                    logger.info('HARD STOP (pending) for session %s — sentinel cleared', _quick_key)
                    return EphemeralReply('⚡ Force-stopped. The agent was still starting — session unlocked.')
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(adapter._pending_messages, _quick_key, event, merge_text=True)
                return None
            if self._draining:
                if self._queue_during_drain_enabled():
                    self._queue_or_replace_pending_event(_quick_key, event)
                return f'⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back.' if self._queue_during_drain_enabled() else f'⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now.'
            if self._busy_input_mode == 'queue':
                logger.debug('PRIORITY queue follow-up for session %s', _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if self._busy_input_mode == 'steer':
                steer_text = (event.text or '').strip()
                steered = False
                if event.message_type == MessageType.TEXT and (not event.media_urls) and (not event.media_types) and steer_text and hasattr(running_agent, 'steer'):
                    try:
                        steered = bool(running_agent.steer(steer_text))
                    except Exception as exc:
                        logger.warning('PRIORITY steer failed for session %s: %s', _quick_key, exc)
                        steered = False
                if steered:
                    logger.debug('PRIORITY steer for session %s', _quick_key)
                    return None
                logger.debug('PRIORITY steer-fallback-to-queue for session %s', _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if self._agent_has_active_subagents(running_agent):
                logger.info('PRIORITY interrupt demoted to queue for session %s because the running agent has active subagents (#30170)', _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if await self._session_has_compression_in_flight(_quick_key):
                logger.info('PRIORITY interrupt demoted to queue for session %s because context compression is in flight (#56391)', _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if event.message_type == MessageType.TEXT and (not event.media_urls) and (not event.media_types) and (getattr(running_agent, '_supports_active_turn_redirect', False) is True) and hasattr(running_agent, 'redirect'):
                try:
                    if running_agent.redirect((event.text or '').strip()):
                        logger.debug('PRIORITY redirect for session %s', _quick_key)
                        return None
                except Exception as exc:
                    logger.warning('PRIORITY redirect failed for session %s: %s', _quick_key, exc)
            logger.debug('PRIORITY interrupt for session %s', _quick_key)
            _interrupt_text = event.text
            _media_urls = getattr(event, 'media_urls', None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(event, self._adapter_for_source(source), source, event.text or '', log_context='Voice-priority-interrupt')
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
            return None
        command = event.get_command()
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, is_gateway_known_command, resolve_command as _resolve_cmd
        _cmd_def = _resolve_cmd(command) if command else None
        canonical = _cmd_def.name if _cmd_def else command
        if command and _cmd_def is None:
            if isinstance(self.config, dict):
                quick_commands = self.config.get('quick_commands', {}) or {}
            else:
                quick_commands = getattr(self.config, 'quick_commands', {}) or {}
            if isinstance(quick_commands, dict) and command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get('type') == 'alias':
                    target = (qcmd.get('target') or '').strip()
                    if target:
                        target = target if target.startswith('/') else f'/{target}'
                        target_command = target.lstrip('/')
                        user_args = event.get_command_args().strip()
                        event.text = f'{target} {user_args}'.strip()
                        command = target_command.split()[0] if target_command else target_command
                        _cmd_def = _resolve_cmd(command) if command else None
                        canonical = _cmd_def.name if _cmd_def else command
        if command and canonical and is_gateway_known_command(canonical):
            _denied = self._check_slash_access(source, canonical)
            if _denied is not None:
                return _denied
        if command and is_gateway_known_command(canonical):
            raw_args = event.get_command_args().strip()
            hook_ctx = {'platform': source.platform.value if source.platform else '', 'user_id': source.user_id, 'command': canonical, 'raw_command': command, 'args': raw_args, 'raw_args': raw_args}
            try:
                hook_results = await self.hooks.emit_collect(f'command:{canonical}', hook_ctx)
            except Exception as _hook_err:
                logger.debug('command:%s hook dispatch failed (non-fatal): %s', canonical, _hook_err)
                hook_results = []
            for hook_result in hook_results:
                if not isinstance(hook_result, dict):
                    continue
                decision = str(hook_result.get('decision', '')).strip().lower()
                if not decision or decision == 'allow':
                    continue
                if decision == 'deny':
                    message = hook_result.get('message')
                    if isinstance(message, str) and message:
                        return message
                    return f'Command `/{command}` was blocked by a hook.'
                if decision == 'handled':
                    message = hook_result.get('message')
                    return message if isinstance(message, str) and message else None
                if decision == 'rewrite':
                    new_command = str(hook_result.get('command_name', '')).strip().lstrip('/')
                    if not new_command:
                        continue
                    new_args = str(hook_result.get('raw_args', '')).strip()
                    event.text = f'/{new_command} {new_args}'.strip()
                    command = event.get_command()
                    _cmd_def = _resolve_cmd(command) if command else None
                    canonical = _cmd_def.name if _cmd_def else command
                    break
        if canonical == 'new':
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                return self._telegram_topic_root_new_message()

            async def _do_reset():
                return await self._handle_reset_command(event)
            return await self._maybe_confirm_destructive_slash(event=event, command='new', title='/new', detail='This starts a fresh session and discards the current conversation history.', execute=_do_reset)
        if canonical == 'topic':
            return await self._handle_topic_command(event)
        if canonical == 'help':
            return await self._handle_help_command(event)
        if canonical == 'start':
            logger.info('Ignoring /start platform ping for session %s', _quick_key)
            return ''
        if canonical == 'commands':
            return await self._handle_commands_command(event)
        if canonical == 'profile':
            return await self._handle_profile_command(event)
        if canonical == 'whoami':
            return await self._handle_whoami_command(event)
        if canonical == 'status':
            return await self._handle_status_command(event)
        if canonical == 'egress':
            from hermes_cli.proxy_cli import format_status_text
            return format_status_text()
        if canonical == 'context':
            return await self._handle_context_command(event)
        if canonical == 'agents':
            return await self._handle_agents_command(event)
        if canonical == 'platform':
            return await self._handle_platform_command(event)
        if canonical == 'restart':
            return await self._handle_restart_command(event)
        if canonical == 'stop':
            return await self._handle_stop_command(event)
        if canonical == 'reasoning':
            return await self._handle_reasoning_command(event)
        if canonical == 'memory':
            return await self._handle_memory_command(event)
        if canonical == 'skills':
            return await self._handle_skills_command(event)
        if canonical == 'learn':
            from agent.learn_prompt import build_learn_prompt
            _learn_req = event.get_command_args().strip()
            _ack = 'Learning a skill from what you described…' if _learn_req else 'Learning a skill from this conversation…'
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug('learn ack send failed', exc_info=True)
            try:
                event.text = build_learn_prompt(_learn_req)
            except Exception:
                return 'Could not start /learn — please try again.'
        if canonical == 'init':
            from hermes_cli.init_command import build_init_prompt_for_cwd
            _init_notes = event.get_command_args().strip()
            try:
                _init_prompt = build_init_prompt_for_cwd(extra=_init_notes)
            except Exception:
                return 'Could not start /init — please try again.'
            _ack = 'Updating AGENTS.md from a project scan…' if 'UPDATE the existing AGENTS.md' in _init_prompt else 'Generating AGENTS.md from a project scan…'
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug('init ack send failed', exc_info=True)
            event.text = _init_prompt
        if canonical == 'fast':
            return await self._handle_fast_command(event)
        if canonical == 'verbose':
            return await self._handle_verbose_command(event)
        if canonical == 'footer':
            return await self._handle_footer_command(event)
        if canonical == 'yolo':
            return await self._handle_yolo_command(event)
        if canonical == 'approvals':
            return await self._handle_approvals_command(event)
        if canonical == 'model':
            return await self._handle_model_command(event)
        if canonical == 'codex-runtime':
            return await self._handle_codex_runtime_command(event)
        if canonical == 'personality':
            return await self._handle_personality_command(event)
        if canonical == 'kanban':
            return await self._handle_kanban_command(event)
        if canonical == 'suggestions':
            return await self._handle_suggestions_command(event)
        if canonical == 'blueprint':
            _blueprint_result = await self._handle_blueprint_command(event)
            _blueprint_seed = getattr(_blueprint_result, 'agent_seed', None)
            if _blueprint_seed:
                _ack = getattr(_blueprint_result, 'text', '') or ''
                if _ack:
                    try:
                        adapter = self._adapter_for_source(source)
                        if adapter:
                            _ack_meta = self._thread_metadata_for_source(source)
                            await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
                    except Exception:
                        logger.debug('blueprint ack send failed', exc_info=True)
                try:
                    event.text = _blueprint_seed
                except Exception:
                    return getattr(_blueprint_result, 'text', '') or None
            else:
                return getattr(_blueprint_result, 'text', '') or None
        if canonical == 'retry':
            return await self._handle_retry_command(event)
        if canonical == 'undo':

            async def _do_undo():
                return await self._handle_undo_command(event)
            _undo_n = 1
            _undo_raw = event.get_command_args().strip()
            if _undo_raw:
                try:
                    _undo_n = max(1, int(_undo_raw.split()[0]))
                except (ValueError, IndexError):
                    _undo_n = 1
            _undo_detail = 'This removes the last user/assistant exchange from history.' if _undo_n == 1 else f'This removes the last {_undo_n} user turns from history.'
            return await self._maybe_confirm_destructive_slash(event=event, command='undo', title='/undo', detail=_undo_detail, execute=_do_undo)
        if canonical == 'sethome':
            return await self._handle_set_home_command(event)
        if canonical == 'compress':
            return await self._handle_compress_command(event)
        if canonical == 'usage':
            return await self._handle_usage_command(event)
        if canonical == 'topup':
            return await self._handle_topup_command(event)
        if canonical == 'insights':
            return await self._handle_insights_command(event)
        if canonical == 'reload-mcp':
            return await self._handle_reload_mcp_command(event)
        if canonical == 'reload-skills':
            return await self._handle_reload_skills_command(event)
        if canonical == 'bundles':
            return await self._handle_bundles_command(event)
        if canonical == 'approve':
            return await self._handle_approve_command(event)
        if canonical == 'deny':
            return await self._handle_deny_command(event)
        if canonical == 'update':
            return await self._handle_update_command(event)
        if canonical == 'version':
            return await self._handle_version_command(event)
        if canonical == 'debug':
            return await self._handle_debug_command(event)
        if canonical == 'title':
            return await self._handle_title_command(event)
        if canonical == 'resume':
            return await self._handle_resume_command(event)
        if canonical == 'sessions':
            return await self._handle_sessions_command(event)
        if canonical == 'branch':
            return await self._handle_branch_command(event)
        if canonical == 'rollback':
            return await self._handle_rollback_command(event)
        if canonical == 'diff':
            return await self._handle_diff_command(event)
        if canonical == 'background':
            return await self._handle_background_command(event)
        if canonical == 'queue':
            queue_payload = event.get_command_args().strip()
            if not queue_payload:
                return 'Usage: /queue <prompt>'
            try:
                event.text = queue_payload
            except Exception:
                pass
        if canonical == 'steer':
            steer_payload = event.get_command_args().strip()
            if not steer_payload:
                return 'Usage: /steer <prompt>  (no agent is running; sending as a normal message)'
            try:
                event.text = steer_payload
            except Exception:
                pass
        if canonical == 'goal':
            return await self._handle_goal_command(event)
        if canonical == 'heartbeat':
            return await self._handle_heartbeat_command(event)
        if canonical == 'refine':
            return await self._handle_refine_command(event)
        if canonical == 'moa':
            from hermes_cli.moa_config import moa_usage, normalize_moa_config
            from hermes_cli.config import load_config
            moa_payload = event.get_command_args().strip()
            if not moa_payload:
                return moa_usage()
            try:
                cfg = load_config()
                moa_cfg = normalize_moa_config(cfg.get('moa') if isinstance(cfg, dict) else {})
            except Exception:
                moa_cfg = normalize_moa_config({})
            preset = moa_cfg['default_preset']
            try:
                event.text = moa_payload
                _moa_state = self._session_state(_quick_key)
                event._moa_restore_override = _moa_state.conversation.model_override
                _moa_state.conversation.model_override = {'provider': 'moa', 'model': preset, 'base_url': 'moa://local', 'api_key': 'moa-virtual-provider', 'api_mode': 'chat_completions'}
                self._evict_cached_agent(_quick_key)
                event._moa_disable_after_turn = True
            except Exception:
                return 'Failed to prepare MoA turn.'
        if canonical == 'subgoal':
            return await self._handle_subgoal_command(event)
        if canonical == 'voice':
            return await self._handle_voice_command(event)
        if self._draining:
            return f'⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now.'
        if command:
            if isinstance(self.config, dict):
                quick_commands = self.config.get('quick_commands', {}) or {}
            else:
                quick_commands = getattr(self.config, 'quick_commands', {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                _denied = self._check_slash_access(source, command)
                if _denied is not None:
                    return _denied
                qcmd = quick_commands[command]
                if qcmd.get('type') == 'exec':
                    exec_cmd = qcmd.get('command', '')
                    if exec_cmd:
                        try:
                            from tools.environments.local import build_subprocess_env
                            sanitized_env = build_subprocess_env()
                            proc = await asyncio.create_subprocess_shell(exec_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=sanitized_env)
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                            output = (stdout or stderr).decode().strip()
                            if output:
                                from agent.redact import redact_sensitive_text
                                output = redact_sensitive_text(output)
                            return output if output else 'Command returned no output.'
                        except asyncio.TimeoutError:
                            return 'Quick command timed out (30s).'
                        except Exception as e:
                            return f'Quick command error: {e}'
                    else:
                        return f"Quick command '/{command}' has no command defined."
                elif qcmd.get('type') == 'alias':
                    target = (qcmd.get('target') or '').strip()
                    if target:
                        target = target if target.startswith('/') else f'/{target}'
                        target_command = target.lstrip('/')
                        user_args = event.get_command_args().strip()
                        event.text = f'{target} {user_args}'.strip()
                        command = target_command.split()[0] if target_command else target_command
                    else:
                        return f"Quick command '/{command}' has no target defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias')."
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                plugin_handler = get_plugin_command_handler(command.replace('_', '-'))
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    result = plugin_handler(user_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.warning('Plugin command dispatch failed: %s', e)
        if command:
            _bundle_handled = False
            try:
                from agent.skill_bundles import build_bundle_invocation_message, resolve_bundle_command_key
                bundle_key = resolve_bundle_command_key(command)
                if bundle_key is not None:
                    user_instruction = event.get_command_args().strip()
                    _bundle_plat = source.platform.value if source.platform else None
                    bundle_result = build_bundle_invocation_message(bundle_key, user_instruction, task_id=_quick_key, platform=_bundle_plat)
                    if bundle_result:
                        msg, _loaded, missing = bundle_result
                        event.text = msg
                        _bundle_handled = True
                        if missing:
                            logger.info('Bundle %s skipped missing skills: %s', bundle_key, ', '.join(missing))
            except Exception as exc:
                logger.warning('Bundle dispatch failed: %s', exc)
        if command and (not locals().get('_bundle_handled', False)):
            try:
                from agent.skill_commands import get_skill_commands, build_skill_invocation_message, resolve_skill_command_key
                skill_cmds = get_skill_commands()
                cmd_key = resolve_skill_command_key(command)
                if cmd_key is not None:
                    _skill_name = skill_cmds[cmd_key].get('name', '')
                    _plat = source.platform.value if source.platform else None
                    if _plat and _skill_name:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        if _skill_name in _get_plat_disabled(platform=_plat):
                            return f'The **{_skill_name}** skill is disabled for {_plat}.\nEnable it with: `duck-agent skills config`'
                    user_instruction = event.get_command_args().strip()
                    try:
                        from agent.skill_commands import build_stacked_skill_invocation_message as _build_stacked, split_stacked_skill_commands
                        extra_keys, stacked_instruction = split_stacked_skill_commands(user_instruction)
                    except Exception:
                        _build_stacked = None
                        extra_keys, stacked_instruction = ([], user_instruction)
                    if extra_keys and _plat:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        _plat_disabled = _get_plat_disabled(platform=_plat)
                        _disabled_extra = [skill_cmds.get(k, {}).get('name', '') for k in extra_keys if skill_cmds.get(k, {}).get('name', '') in _plat_disabled]
                        if _disabled_extra:
                            return f"The **{', '.join(_disabled_extra)}** skill(s) in this stacked invocation are disabled for {_plat}.\nEnable them with: `duck-agent skills config`"
                    if extra_keys and _build_stacked is not None:
                        stacked_result = _build_stacked([cmd_key, *extra_keys], stacked_instruction, task_id=_quick_key)
                        if stacked_result:
                            msg, _loaded, _missing = stacked_result
                            event.text = msg
                        else:
                            return f'Failed to load stacked skills for /{command}.'
                    else:
                        msg = build_skill_invocation_message(cmd_key, user_instruction, task_id=_quick_key)
                        if msg:
                            event.text = msg
                else:
                    _unavail_msg = _check_unavailable_skill(command)
                    if _unavail_msg:
                        return _unavail_msg
                    if command.replace('_', '-') not in GATEWAY_KNOWN_COMMANDS:
                        logger.warning('Unrecognized slash command /%s from %s — replying with unknown-command notice', command, source.platform.value if source.platform else '?')
                        return f"Unknown command `/{command}`. Type /commands to see what's available, or resend without the leading slash to send as a regular message."
            except Exception as e:
                logger.debug('Skill command check failed (non-fatal): %s', e)
        if not is_internal and await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
            if self._should_send_telegram_lobby_reminder(source):
                return self._telegram_topic_root_lobby_message()
            return None
        if self._external_drain_active and (not is_internal):
            logger.info('Refusing new turn for session %s — external drain active.', _quick_key)
            return "⏳ This agent is draining for a maintenance action and isn't accepting new turns right now. It'll be back in a moment — please resend shortly."
        _active_session_lease, _limit_message = self._claim_active_session_slot(_quick_key, source)
        if _limit_message is not None:
            logger.info('Rejecting new active session %s: max_concurrent_sessions reached', _quick_key)
            return _limit_message
        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.started_ts = time.time()
        self._persist_active_agents()
        _run_generation = self._begin_session_run_generation(_quick_key)
        try:
            try:
                _agent_result = await self._handle_message_with_agent(event, source, _quick_key, _run_generation)
            except TurnLeaseTimeoutError as exc:
                logger.error('Rejecting turn for routing key %s on session %s after turn-lease timeout; transcript load was not started and the user must resend', _quick_key, exc.session_id)
                return '⏳ Another turn is still running on this session. To protect the transcript, this message was not processed. Wait for the active turn to finish, then resend it.'
            try:
                _final_text = ''
                if isinstance(_agent_result, dict):
                    _final_text = str(_agent_result.get('final_response') or '')
                elif isinstance(_agent_result, str):
                    _final_text = _agent_result
                if _final_text.strip():
                    try:
                        session_entry = await self.async_session_store.get_or_create_session(source)
                    except Exception:
                        session_entry = None
                    if session_entry is not None:
                        await self._post_turn_goal_continuation(session_entry=session_entry, source=source, final_response=_final_text)
            except Exception as _goal_exc:
                logger.debug('goal continuation hook failed: %s', _goal_exc)
            return _agent_result
        finally:
            self._restore_moa_one_shot(event, _quick_key)
            self._restore_pending_one_turn_model_override(_quick_key)
            await self._clear_durable_active_turn(event)
            self._release_running_agent_state(_quick_key)
            self._release_turn_lease(_quick_key, _run_generation)

    def _restore_moa_one_shot(self, event: 'MessageEvent', quick_key: str) -> None:
        """Revert a ``/moa <prompt>`` one-shot model override after its turn.

        Called from the ``finally`` of the message-handling path so the revert
        fires whether the turn succeeded, raised, or was interrupted. A no-op
        unless ``event._moa_disable_after_turn`` is set. ``_moa_restore_override``
        carries the prior per-session override (``None`` means the user had no
        override, so the MoA override is cleared outright).
        """
        if not getattr(event, '_moa_disable_after_turn', False):
            return
        try:
            _restore = getattr(event, '_moa_restore_override', None)
            self._session_state(quick_key).conversation.model_override = _restore
            self._evict_cached_agent(quick_key)
        except Exception:
            pass

    def _restore_pending_one_turn_model_override(self, session_key: str) -> None:
        """Restore a per-session model override after ``/model --once`` runs."""
        if not session_key:
            return
        try:
            _otr_state = self._peek_session_state(session_key)
            snapshot = _otr_state.conversation.one_turn_restore if _otr_state else None
            if _otr_state is not None:
                _otr_state.conversation.one_turn_restore = None
            if not snapshot:
                return
            self._restore_session_model_override(session_key, snapshot)
        except Exception:
            logger.debug('Failed to restore one-turn model override', exc_info=True)

    async def _prepare_inbound_message_text(self, *, event: MessageEvent, source: SessionSource, history: List[Dict[str, Any]], session_key: Optional[str]=None) -> Optional[str]:
        """Prepare inbound event text for the agent.

        Keep the normal inbound path and the queued follow-up path on the same
        preprocessing pipeline so sender attribution, image enrichment, STT,
        document notes, reply context, and @ references all behave the same.

        Side effect: buffers per-session native image paths when the active
        model supports native vision AND the user has images attached. The
        caller consumes and clears that session-scoped buffer at the
        ``run_conversation`` site to build a multimodal user turn. When the
        list is empty, the ``_enrich_message_with_vision`` text path has
        already run and images are represented in-text.
        """
        history = history or []
        _pending_stt_prepared = hasattr(event, '_gateway_pending_stt_text')
        message_text = (getattr(event, '_gateway_pending_stt_text', None) if _pending_stt_prepared else event.text) or ''
        _group_sessions_per_user = getattr(self.config, 'group_sessions_per_user', True)
        _thread_sessions_per_user = getattr(self.config, 'thread_sessions_per_user', False)
        session_key = session_key or self._session_key_for_source(source)
        self._consume_pending_native_image_paths(session_key)
        _is_shared_multi_user = is_shared_multi_user_session(source, group_sessions_per_user=_group_sessions_per_user, thread_sessions_per_user=_thread_sessions_per_user)
        if _is_shared_multi_user and source.user_name:
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = f'{_safe_user_name} | Slack user <@{source.user_id}>'
            message_text = f'[{_safe_user_name}] {message_text}'
        if getattr(event, 'channel_context', None):
            message_text = f'{event.channel_context}\n\n[New message]\n{message_text}'
        audio_file_paths: list[str] = []
        video_paths: list[str] = []
        if event.media_urls:
            image_paths = []
            audio_paths = []
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ''
                if _event_media_is_image(event, i):
                    image_paths.append(path)
                if event.message_type == MessageType.AUDIO:
                    audio_file_paths.append(path)
                elif not _pending_stt_prepared and _event_media_is_stt_input(event, i):
                    audio_paths.append(path)
                if mtype.startswith('video/') or (not mtype and event.message_type == MessageType.VIDEO):
                    video_paths.append(path)
            if image_paths:
                _img_mode = await asyncio.to_thread(self._decide_image_input_mode, source=source, session_key=session_key)
                if _img_mode == 'native':
                    self._session_state(session_key).persistent.native_image_paths = list(image_paths)
                    logger.info('Image routing: native (model supports vision). %d image(s) will be attached inline.', len(image_paths))
                else:
                    logger.info('Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.', _img_mode, len(image_paths))
                    vision_runtime = None
                    try:
                        turn_model, runtime_kwargs = self._resolve_session_agent_runtime(source=source, session_key=session_key)
                        vision_runtime = dict(runtime_kwargs or {})
                        vision_runtime['model'] = turn_model
                    except Exception:
                        logger.debug('vision enrichment: session runtime resolution failed', exc_info=True)
                    from agent.auxiliary_client import scoped_runtime_main
                    with scoped_runtime_main(vision_runtime):
                        message_text = await self._enrich_message_with_vision(message_text, image_paths)
            if audio_paths:
                message_text, _successful_transcripts = await self._enrich_message_with_transcription(message_text, audio_paths)
                if _successful_transcripts and self._should_echo_stt_transcripts():
                    _echo_adapter = self._adapter_for_source(source)
                    _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    if _echo_adapter:
                        for _tx in _successful_transcripts:
                            try:
                                await _echo_adapter.send(source.chat_id, f'🎙️ "{_tx}"', metadata=_echo_meta)
                            except Exception as _echo_exc:
                                logger.debug('Transcript echo failed (non-fatal): %s', _echo_exc)
        if audio_file_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _apath in audio_file_paths:
                _basename = os.path.basename(_apath)
                _parts = _basename.split('_', 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub('[^\\w.\\- ]', '_', _display)
                _agent_path = _to_agent_path(_apath)
                _note = f"[The user sent an audio file attachment: '{_display}'. It is saved at: {_agent_path}. Its content is not inlined here. If the user's request involves what the audio contains, transcribe or process it yourself — for example by passing the path to a transcription or media tool — instead of asking the user to describe it. Only ask what to do with it if their intent is genuinely unclear.]"
                message_text = f'{_note}\n\n{message_text}'
        if video_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _vpath in video_paths:
                _basename = os.path.basename(_vpath)
                _parts = _basename.split('_', 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub('[^\\w.\\- ]', '_', _display)
                _agent_path = _to_agent_path(_vpath)
                _note = f"[The user sent a video attachment: '{_display}'. It is saved at: {_agent_path}. Its content is not inlined here. If the user's request involves what the video contains, inspect or process it yourself — for example by passing the path to a video analysis or media tool — instead of asking the user to describe it. Only ask what to do with it if their intent is genuinely unclear.]"
                message_text = f'{_note}\n\n{message_text}'
        if event.media_urls:
            import mimetypes as _mimetypes
            from tools.credential_files import to_agent_visible_cache_path
            _TEXT_EXTENSIONS = {'.txt', '.md', '.csv', '.log', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg'}
            for i, path in enumerate(event.media_urls):
                if _event_media_is_image(event, i) or _event_media_is_audio(event, i) or _event_media_is_video(event, i):
                    continue
                mtype = event.media_types[i] if i < len(event.media_types) else ''
                if mtype in {'', 'application/octet-stream'}:
                    _ext = os.path.splitext(path)[1].lower()
                    if _ext in _TEXT_EXTENSIONS:
                        mtype = 'text/plain'
                    else:
                        guessed, _ = _mimetypes.guess_type(path)
                        if guessed:
                            mtype = guessed
                        else:
                            mtype = 'application/octet-stream'
                basename = os.path.basename(path)
                parts = basename.split('_', 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                display_name = re.sub('[^\\w.\\- ]', '_', display_name)
                agent_path = to_agent_visible_cache_path(path)
                context_note = _build_document_context_note(display_name, agent_path, mtype)
                message_text = f'{context_note}\n\n{message_text}'
        if source is not None and getattr(source, 'platform', None) == Platform.DISCORD and getattr(event, 'message_id', None):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = f'[Triggering message id: `{event.message_id}` — use as `message_id` for reply/react/pin via the discord tools.]\n\n{message_text}'
        if getattr(event, 'reply_to_text', None) and event.reply_to_message_id:
            reply_snippet = event.reply_to_text[:500]
            if getattr(event, 'reply_to_is_own_message', False):
                message_text = f'[Replying to your previous message: "{reply_snippet}"]\n\n{message_text}'
            else:
                message_text = f'[Replying to: "{reply_snippet}"]\n\n{message_text}'
        if '@' in message_text:
            try:
                from agent.context_references import preprocess_context_references_async
                from agent.model_metadata import get_model_context_length_async
                _msg_cwd = os.environ.get('TERMINAL_CWD', os.path.expanduser('~'))
                _msg_config_ctx = None
                _msg_cfg = None
                _msg_model_cfg = {}
                _msg_custom_providers = []
                try:
                    _msg_cfg = _load_gateway_config()
                    _msg_model_cfg = _msg_cfg.get('model', {})
                    if isinstance(_msg_model_cfg, dict):
                        _msg_raw_ctx = _msg_model_cfg.get('context_length')
                        if _msg_raw_ctx is not None:
                            _msg_config_ctx = int(_msg_raw_ctx)
                    try:
                        from hermes_cli.config import get_compatible_custom_providers
                        _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
                    except Exception:
                        _msg_custom_providers = _msg_cfg.get('custom_providers') or []
                except Exception:
                    pass
                _msg_model, _msg_runtime = self._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=_msg_cfg)
                _msg_base_url = _msg_runtime.get('base_url') or ''
                _msg_configured_model = _msg_model_cfg.get('default') or _msg_model_cfg.get('model') if isinstance(_msg_model_cfg, dict) else _msg_model_cfg
                if _msg_model != _msg_configured_model:
                    _msg_config_ctx = None
                if _msg_config_ctx is not None and isinstance(_msg_model_cfg, dict):
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async
                        if await should_clear_context_pin_async(None, None, _msg_model_cfg.get('base_url'), _msg_base_url, _msg_model_cfg.get('provider'), _msg_runtime.get('provider')):
                            _msg_config_ctx = None
                    except Exception:
                        _msg_config_ctx = None
                if _msg_custom_providers and _msg_base_url:
                    try:
                        from hermes_cli.config import get_custom_provider_context_length
                        _msg_custom_ctx = get_custom_provider_context_length(model=_msg_model, base_url=_msg_base_url, custom_providers=_msg_custom_providers)
                        if _msg_custom_ctx:
                            _msg_config_ctx = _msg_custom_ctx
                    except Exception:
                        pass
                _msg_ctx_len = await get_model_context_length_async(_msg_model, base_url=_msg_base_url, api_key=_msg_runtime.get('api_key') or '', config_context_length=_msg_config_ctx, provider=_msg_runtime.get('provider') or '', custom_providers=_msg_custom_providers)
                _ctx_result = await preprocess_context_references_async(message_text, cwd=_msg_cwd, context_length=_msg_ctx_len, allowed_root=_msg_cwd)
                if _ctx_result.blocked:
                    _adapter = self._adapter_for_source(source)
                    if _adapter:
                        await _adapter.send(source.chat_id, '\n'.join(_ctx_result.warnings) or 'Context injection refused.')
                    return None
                if _ctx_result.expanded:
                    message_text = _ctx_result.message
            except Exception as exc:
                logger.warning('@ context reference expansion failed: %s', exc)
                logger.debug('@ context reference expansion failure detail', exc_info=True)
        return message_text

    async def _prepare_profile_scoped_inbound_message_text(self, *, event: MessageEvent, source: SessionSource, history: List[Dict[str, Any]], session_key: Optional[str]=None) -> Optional[str]:
        """Run inbound preprocessing under the routed profile when multiplexed."""
        if getattr(getattr(self, 'config', None), 'multiplex_profiles', False):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return await self._prepare_inbound_message_text(event=event, source=source, history=history, session_key=session_key)
        return await self._prepare_inbound_message_text(event=event, source=source, history=history, session_key=session_key)

    async def _prepare_clarify_reply_text(self, event) -> str:
        """Return raw text or successful voice transcripts for a clarify reply."""
        if not self._pending_event_audio_paths(event):
            return (event.text or '').strip()
        _, successful_transcripts = await self._transcribe_pending_audio_event_once(event, '')
        return '\n\n'.join((transcript.strip() for transcript in successful_transcripts if transcript.strip()))

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        state = self._peek_session_state(session_key)
        if state is None or not state.persistent.native_image_paths:
            return []
        paths = list(state.persistent.native_image_paths)
        state.persistent.native_image_paths = []
        return paths

    def _cache_session_source(self, session_key: str, source) -> None:
        if not session_key or source is None:
            return
        cached_sources = getattr(self, '_session_sources', None)
        if cached_sources is None:
            cached_sources = OrderedDict()
            self._session_sources = cached_sources
        try:
            cached_sources[session_key] = dataclasses.replace(source)
        except Exception:
            logger.debug('Failed to cache live session source for %s', session_key, exc_info=True)
            return
        try:
            cached_sources.move_to_end(session_key)
            max_size = getattr(self, '_session_sources_max', 512)
            while len(cached_sources) > max_size:
                cached_sources.popitem(last=False)
        except Exception:
            pass

    @property
    def async_session_store(self) -> AsyncSessionStore:
        """Return the single async facade for this runner's SessionStore."""
        facade = getattr(self, '_async_session_store', None)
        if facade is None or facade._store is not self.session_store:
            facade = AsyncSessionStore(self.session_store)
            self._async_session_store = facade
        return facade

    async def _mark_durable_active_turn(self, event: 'MessageEvent', session_key: str) -> bool:
        """Persist the exact resolved routing key for this running turn."""
        try:
            token = await self.async_session_store.mark_turn_active(session_key)
        except Exception as exc:
            logger.warning('Could not persist active-turn marker for %s: %s', session_key, exc)
            return False
        if not token:
            return False
        setattr(event, '_gateway_active_turn_session_key', session_key)
        setattr(event, '_gateway_active_turn_token', token)
        return True

    async def _clear_durable_active_turn(self, event: 'MessageEvent') -> bool:
        """Best-effort CAS clear of the marker owned by *event*."""
        session_key = getattr(event, '_gateway_active_turn_session_key', None)
        token = getattr(event, '_gateway_active_turn_token', None)
        try:
            if not session_key or not token:
                return False
            last_error: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    return bool(await self.async_session_store.clear_turn_active(session_key, token))
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        logger.debug('Retrying active-turn marker cleanup for %s (%d/3): %s', session_key, attempt, exc)
            logger.warning('Could not clear active-turn marker for %s after 3 attempts: %s', session_key, last_error)
            return False
        finally:
            for attr in ('_gateway_active_turn_session_key', '_gateway_active_turn_token'):
                try:
                    delattr(event, attr)
                except AttributeError:
                    pass

    def _get_cached_session_source(self, session_key: str):
        if not session_key:
            return None
        cached_sources = getattr(self, '_session_sources', None)
        if not cached_sources:
            return None
        source = cached_sources.get(session_key)
        if source is not None:
            try:
                cached_sources.move_to_end(session_key)
            except Exception:
                pass
        return source

    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        _msg_start_time = time.time()
        _platform_name = source.platform.value if hasattr(source.platform, 'value') else str(source.platform)
        _msg_preview = (event.text or '')[:80].replace('\n', ' ')
        _reply_id = getattr(event, 'reply_to_message_id', None)
        _reply_txt = (getattr(event, 'reply_to_text', None) or '')[:80].replace('\n', ' ')
        logger.info('inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r', _platform_name, source.user_name or source.user_id or 'unknown', source.chat_id or 'unknown', _msg_preview, _reply_id, _reply_txt)
        recovered = await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
        if recovered is not None:
            logger.info('telegram topic recovery: chat=%s user=%s %r -> %s', source.chat_id, source.user_id, source.thread_id, recovered)
            source = dataclasses.replace(source, thread_id=recovered)
            try:
                event.source = source
            except Exception:
                pass
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        pinned_session_id = str((getattr(event, 'metadata', None) or {}).get('gateway_session_id') or '').strip()
        if pinned_session_id:
            resolved_entry = await self._resolve_async_delegation_session(session_entry, pinned_session_id)
            if resolved_entry is None:
                return
            session_entry = resolved_entry
        self._cache_session_source(session_key, source)
        if await asyncio.to_thread(self._is_telegram_topic_lane, source):
            try:
                binding = await self._session_db.get_telegram_topic_binding(chat_id=str(source.chat_id), thread_id=str(source.thread_id)) if self._session_db else None
            except Exception:
                logger.debug('Failed to read Telegram topic binding', exc_info=True)
                binding = None
            if binding:
                bound_session_id = str(binding.get('session_id') or '')
                if bound_session_id and self._session_db is not None:
                    try:
                        canonical_session_id = await self._session_db.get_compression_tip(bound_session_id)
                    except Exception:
                        logger.debug('compression-tip lookup failed for %s', bound_session_id, exc_info=True)
                        canonical_session_id = bound_session_id
                    if canonical_session_id and canonical_session_id != bound_session_id:
                        bound_session_id = canonical_session_id
                if bound_session_id and bound_session_id != session_entry.session_id:
                    switched = await self.async_session_store.switch_session(session_key, bound_session_id)
                    if switched is not None:
                        session_entry = switched
                if bound_session_id and bound_session_id != str(binding.get('session_id') or ''):
                    await asyncio.to_thread(self._sync_telegram_topic_binding, source, session_entry, reason='compression-tip-walk')
            else:
                try:
                    await asyncio.to_thread(self._record_telegram_topic_binding, source, session_entry)
                except Exception:
                    logger.debug('Failed to record Telegram topic binding', exc_info=True)
        _was_auto_reset = getattr(session_entry, 'was_auto_reset', False)
        if _was_auto_reset:
            self._clear_conversation_scope(session_key, reason='auto_reset')
            self._evict_cached_agent(session_key)
            session_entry.was_auto_reset = False
        _is_new_session = session_entry.created_at == session_entry.updated_at or _was_auto_reset or getattr(session_entry, 'is_fresh_reset', False)
        if getattr(session_entry, 'is_fresh_reset', False):
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await self.hooks.emit('session:start', {'platform': source.platform.value if source.platform else '', 'user_id': source.user_id, 'session_id': session_entry.session_id, 'session_key': session_key})
        context = build_session_context(source, self.config, session_entry)
        _session_env_tokens = self._set_session_env(context)
        _redact_pii = False
        persist_user_message = None
        persist_user_timestamp = None
        try:
            _pcfg = _load_gateway_config()
            _redact_pii = bool((_pcfg.get('privacy') or {}).get('redact_pii', False))
        except Exception:
            pass
        context_prompt = self._pinned_session_context_prompt(context, _redact_pii, session_key)
        turn_sidecar_notes: List[str] = []
        if _was_auto_reset:
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == 'suspended':
                context_note = "[System note: The user's previous session was stopped and suspended. This is a fresh conversation with no prior context.]"
            elif reset_reason == 'daily':
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            elif reset_reason == 'resume_pending_expired':
                context_note = '[System note: The previous gateway session could not be recovered after a restart (API recovery timed out). This is a fresh conversation — use /resume to restore history if needed.]'
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            try:
                continuity_note = build_channel_continuity_note(session_entry, source)
            except Exception:
                continuity_note = None
            if continuity_note:
                context_note = context_note + '\n\n' + continuity_note
            turn_sidecar_notes.append(context_note)
            try:
                policy = self.session_store.config.get_reset_policy(platform=source.platform, session_type=getattr(source, 'chat_type', 'dm'))
                platform_name = source.platform.value if source.platform else ''
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                should_notify = reset_reason in {'suspended', 'resume_pending_expired'} or (policy.notify and had_activity and (platform_name not in policy.notify_exclude_platforms))
                if should_notify:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        if reset_reason == 'suspended':
                            reason_text = 'previous session was stopped or interrupted'
                        elif reset_reason == 'resume_pending_expired':
                            reason_text = 'gateway restart recovery timed out'
                        elif reset_reason == 'daily':
                            reason_text = f'daily schedule at {policy.at_hour}:00'
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f'{hours}h' if not mins else f'{hours}h {mins}m' if hours else f'{mins}m'
                            reason_text = f'inactive for {duration}'
                        notice = f'◐ Session automatically reset ({reason_text}). Conversation history cleared.\nUse /resume to browse and restore a previous session.\nAdjust reset timing in config.yaml under session_reset.'
                        try:
                            session_info = await asyncio.to_thread(self._reset_notice_session_info, source)
                            if session_info:
                                notice = f'{notice}\n\n{session_info}'
                        except Exception:
                            pass
                        await adapter.send(source.chat_id, notice, metadata=self._thread_metadata_for_source(source))
            except Exception as e:
                logger.debug('Auto-reset notification failed (non-fatal): %s', e)
            session_entry.auto_reset_reason = None
        _auto = getattr(event, 'auto_skill', None)
        if _is_new_session and _auto:
            _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _combined_parts: list[str] = []
                _loaded_names: list[str] = []
                for _sname in _skill_names:
                    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                    if _loaded:
                        _loaded_skill, _skill_dir, _display_name = _loaded
                        _note = f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. Follow its instructions for this session.]'
                        _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
                        if _part:
                            _combined_parts.append(_part)
                            _loaded_names.append(_sname)
                    else:
                        logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                if _combined_parts:
                    _combined_parts.append(event.text)
                    event.text = '\n\n'.join(_combined_parts)
                    logger.info('[Gateway] Auto-loaded skill(s) %s for session %s', _loaded_names, session_key)
            except Exception as e:
                logger.warning('[Gateway] Failed to auto-load skill(s) %s: %s', _skill_names, e)
        _lease_registry = getattr(self, '_turn_leases', None)
        if _lease_registry is not None:
            try:
                _lease_token = await _lease_registry.acquire(session_entry.session_id, owner_key=_quick_key, generation=run_generation, timeout=_float_env('HERMES_TURN_LEASE_TIMEOUT', DEFAULT_LEASE_WAIT))
            except TurnLeaseTimeoutError:
                self._clear_session_env(_session_env_tokens)
                raise
            if _lease_token is not None:
                _lease_state = self._session_state(_quick_key).turn
                _lease_state.lease_token = _lease_token
                _lease_state.lease_generation = run_generation
        await self._mark_durable_active_turn(event, session_entry.session_key)
        history = await self.async_session_store.load_transcript(session_entry.session_id)
        if history and len(history) >= 4:
            from agent.model_metadata import estimate_messages_tokens_rough, get_model_context_length_async
            _hyg_model = 'anthropic/claude-sonnet-4.6'
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_hard_msg_limit = 5000
            _hyg_timeout_seconds = 30.0
            _hyg_total_ceiling_seconds = 600.0
            _hyg_failure_cooldown_seconds = 300.0
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            _hyg_configured_model = None
            _hyg_configured_provider = None
            _hyg_configured_base_url = None
            _hyg_data = {}
            try:
                _hyg_data = _load_gateway_config()
                if _hyg_data:
                    _model_cfg = _hyg_data.get('model', {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get('default') or _model_cfg.get('model') or _hyg_model
                        _raw_ctx = _model_cfg.get('context_length')
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        _hyg_provider = _model_cfg.get('provider') or None
                        _hyg_base_url = _model_cfg.get('base_url') or None
                    _comp_cfg = _hyg_data.get('compression', {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(_comp_cfg.get('enabled', True)).lower() in {'true', '1', 'yes'}
                        _raw_hard_limit = _comp_cfg.get('hygiene_hard_message_limit')
                        if _raw_hard_limit is not None:
                            try:
                                _parsed = int(_raw_hard_limit)
                                if _parsed > 0:
                                    _hyg_hard_msg_limit = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_timeout = _comp_cfg.get('hygiene_timeout_seconds')
                        if _raw_timeout is not None:
                            try:
                                _parsed = float(_raw_timeout)
                                if _parsed > 0:
                                    _hyg_timeout_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_ceiling = _comp_cfg.get('hygiene_total_ceiling_seconds')
                        if _raw_ceiling is not None:
                            try:
                                _parsed = float(_raw_ceiling)
                                if _parsed > 0:
                                    _hyg_total_ceiling_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _hyg_total_ceiling_seconds = max(_hyg_total_ceiling_seconds, _hyg_timeout_seconds)
                        _raw_cooldown = _comp_cfg.get('hygiene_failure_cooldown_seconds')
                        if _raw_cooldown is not None:
                            try:
                                _parsed = float(_raw_cooldown)
                                if _parsed >= 0:
                                    _hyg_failure_cooldown_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                _hyg_configured_model = _hyg_model
                _hyg_configured_provider = _hyg_provider
                _hyg_configured_base_url = _hyg_base_url
                try:
                    _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=_hyg_data if isinstance(_hyg_data, dict) else None)
                    _hyg_provider = _hyg_runtime.get('provider') or _hyg_provider
                    _hyg_base_url = _hyg_runtime.get('base_url') or _hyg_base_url
                    _hyg_api_key = _hyg_runtime.get('api_key') or _hyg_api_key
                except Exception:
                    pass
                if _hyg_config_context_length is not None:
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async
                        if await should_clear_context_pin_async(_hyg_configured_model, _hyg_model, _hyg_configured_base_url, _hyg_base_url, _hyg_configured_provider, _hyg_provider):
                            _hyg_config_context_length = None
                    except Exception:
                        _hyg_config_context_length = None
                if _hyg_config_context_length is None and _hyg_base_url:
                    try:
                        try:
                            from hermes_cli.config import get_compatible_custom_providers as _gw_gcp, get_custom_provider_context_length as _gw_gccl
                            _hyg_custom_providers = _gw_gcp(_hyg_data)
                        except Exception:
                            _hyg_custom_providers = _hyg_data.get('custom_providers')
                            if not isinstance(_hyg_custom_providers, list):
                                _hyg_custom_providers = []
                        _hyg_custom_ctx = _gw_gccl(model=_hyg_model, base_url=_hyg_base_url, custom_providers=_hyg_custom_providers)
                        if _hyg_custom_ctx:
                            _hyg_config_context_length = int(_hyg_custom_ctx)
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass
            if _hyg_compression_enabled:
                _hyg_context_length = await get_model_context_length_async(_hyg_model, base_url=_hyg_base_url or '', api_key=_hyg_api_key or '', config_context_length=_hyg_config_context_length, provider=_hyg_provider or '')
                _compress_token_threshold = int(_hyg_context_length * _hyg_threshold_pct)
                _warn_token_threshold = int(_hyg_context_length * 0.95)
                _msg_count = len(history)
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = 'actual'
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = 'estimated'
                _HARD_MSG_LIMIT = _hyg_hard_msg_limit
                _needs_compress = _approx_tokens >= _compress_token_threshold or _msg_count >= _HARD_MSG_LIMIT
                if _needs_compress:
                    _session_db = getattr(self, '_session_db', None)
                    if _session_db is not None:
                        _session_db = getattr(_session_db, '_db', _session_db)
                        _getter = getattr(_session_db, 'get_compression_failure_cooldown', None)
                        if _getter is not None:
                            try:
                                _cooldown_state = _getter(session_entry.session_id)
                            except Exception:
                                _cooldown_state = None
                            if _cooldown_state and _cooldown_state.get('remaining_seconds', 0) > 0:
                                logger.info('Session hygiene: skipping compression for %s; previous failure cooldown active for %.1fs', session_entry.session_id, _cooldown_state['remaining_seconds'])
                                _needs_compress = False
                if _needs_compress:
                    logger.info('Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing (threshold: %s%% of %s = %s tokens)', _msg_count, f'{_approx_tokens:,}', _token_source, int(_hyg_threshold_pct * 100), f'{_hyg_context_length:,}', f'{_compress_token_threshold:,}')
                    _hyg_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    try:
                        from agent.conversation_compression import CompressionCommitFence
                        from run_agent import AIAgent
                        _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=_hyg_data if isinstance(_hyg_data, dict) else None)
                        if _hyg_runtime.get('api_key'):
                            _hyg_msgs = [m for m in history if m.get('role') in {'user', 'assistant', 'tool'}]
                            if len(_hyg_msgs) >= 4:
                                try:
                                    _hyg_session_row = await self._session_db.get_session(session_entry.session_id)
                                except Exception as exc:
                                    _hyg_session_row = None
                                    logger.warning('Session hygiene could not restore the system prompt for session %s: %s. Preserving an empty prompt so the live turn rebuilds it with its configured providers.', session_entry.session_id, exc, exc_info=True)
                                _hyg_session_db = getattr(self._session_db, '_db', self._session_db)
                                _hyg_agent = AIAgent(**_hyg_runtime, model=_hyg_model, max_iterations=4, quiet_mode=True, skip_memory=True, enabled_toolsets=['memory'], session_id=session_entry.session_id, session_db=_hyg_session_db)
                                _seed_hygiene_system_prompt(_hyg_agent, _hyg_session_row)
                                _hyg_agent.platform = _GATEWAY_HYGIENE_PLATFORM
                                _hyg_cleanup_deferred = False
                                try:
                                    _hyg_agent.compression_in_place = True
                                    _bind_hyg_state = getattr(getattr(_hyg_agent, 'context_compressor', None), 'bind_session_state', None)
                                    if callable(_bind_hyg_state):
                                        _bind_hyg_state(_hyg_session_db, session_entry.session_id)
                                    _hyg_agent._end_session_on_close = False
                                    _hyg_agent._print_fn = lambda *a, **kw: None
                                    loop = asyncio.get_running_loop()
                                    _hyg_commit_fence = CompressionCommitFence()
                                    _hyg_future = loop.run_in_executor(None, lambda: _hyg_agent._compress_context(_hyg_msgs, '', approx_tokens=_approx_tokens, commit_fence=_hyg_commit_fence))
                                    try:
                                        _hyg_wait_started = time.monotonic()
                                        while True:
                                            _slice = max(_hyg_timeout_seconds - _hyg_commit_fence.seconds_since_progress(), 0.005)
                                            try:
                                                _compressed, _ = await asyncio.wait_for(asyncio.shield(_hyg_future), timeout=_slice)
                                                break
                                            except asyncio.TimeoutError:
                                                _hyg_waited = time.monotonic() - _hyg_wait_started
                                                _idle = _hyg_commit_fence.seconds_since_progress()
                                                if _idle < _hyg_timeout_seconds and _hyg_waited < _hyg_total_ceiling_seconds:
                                                    logger.info('Session hygiene compression for session %s still streaming after %.0fs (last progress %.1fs ago) — extending wait (ceiling %.0fs)', session_entry.session_id, _hyg_waited, _idle, _hyg_total_ceiling_seconds)
                                                    continue
                                                raise
                                    except asyncio.TimeoutError:
                                        _cancelled = None
                                        while _cancelled is None:
                                            if _hyg_commit_fence.commit_in_flight:
                                                _cancelled = False
                                                break
                                            _cancelled = _hyg_commit_fence.try_cancel_before_commit()
                                            if _cancelled is None:
                                                await asyncio.sleep(0.025)
                                        if not _cancelled:
                                            _compressed, _ = await _hyg_future
                                        else:
                                            _hyg_commit_fence.release_cancelled_compression_lock()
                                            self._defer_agent_cleanup_until_future_done(_hyg_future, _hyg_agent, context='session hygiene timeout')
                                            _hyg_cleanup_deferred = True
                                            if _hyg_failure_cooldown_seconds >= 0:
                                                _record_hygiene_cooldown(self, session_entry.session_id, _hygiene_cooldown_for_failure(self, session_key, _hyg_failure_cooldown_seconds), 'session hygiene compression timed out with no output from the summary model')
                                            from agent.session_activity import ActivityProvenance
                                            _stamp_hygiene_compression_provenance(_hyg_agent, 'session hygiene compression timed out', ActivityProvenance.AGENT_COMPRESSION_TIMEOUT, 'hygiene compression timeout activity stamp failed')
                                            logger.warning('Session hygiene compression for session %s made no progress for %.1fs (total wait %.1fs, ceiling %.1fs); continuing without compression', session_entry.session_id, _hyg_commit_fence.seconds_since_progress(), time.monotonic() - _hyg_wait_started, _hyg_total_ceiling_seconds)
                                            _timeout_msg = f'⚠️ Context compression timed out after {_hyg_timeout_seconds:.1f}s with no output from the summary model. No messages were dropped — continuing without compression. Run /compress to retry, /reset for a clean session, or check your auxiliary.compression model configuration.'
                                            try:
                                                _adapter = self._adapter_for_source(source)
                                                if _adapter and source.chat_id:
                                                    await _adapter.send(source.chat_id, _timeout_msg, metadata=_hyg_meta)
                                            except Exception as _werr:
                                                logger.warning('Failed to deliver compression-timeout warning to user: %s', _werr)
                                            raise
                                    except BaseException:
                                        _hyg_commit_fence.revoke_commit_admission()
                                        if not _hyg_cleanup_deferred:
                                            self._defer_agent_cleanup_until_future_done(_hyg_future, _hyg_agent, context='session hygiene unwind')
                                            _hyg_cleanup_deferred = True
                                        raise
                                    _hyg_new_sid = _hyg_agent.session_id
                                    _hyg_rotated = _hyg_new_sid != session_entry.session_id
                                    _hyg_in_place = bool(getattr(_hyg_agent, '_last_compaction_in_place', False))
                                    if _hyg_rotated:
                                        if not await self.async_session_store.rewrite_transcript(_hyg_new_sid, _compressed):
                                            logger.error('Session hygiene: failed to persist compressed transcript for rotated session %s → %s; keeping the live entry on the original session so the conversation is not dropped', session_entry.session_id, _hyg_new_sid)
                                            _hyg_rotated = False
                                            _hyg_in_place = False
                                        else:
                                            session_entry.session_id = _hyg_new_sid
                                            self._rebind_turn_lease(_quick_key, run_generation, _hyg_new_sid)
                                            await self.async_session_store._save()
                                            await asyncio.to_thread(self._sync_telegram_topic_binding, source, session_entry, reason='hygiene-compression')
                                    if _hyg_rotated:
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(_compressed)
                                    elif _hyg_in_place:
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(_compressed)
                                    else:
                                        _new_count = _msg_count
                                        _new_tokens = _approx_tokens
                                        logger.warning('Gateway hygiene compression for session %s did not rotate or compact in place (no session_db on the hygiene agent) — preserving the original transcript instead of overwriting it with the summary (#21301).', session_entry.session_id)
                                    logger.info('Session hygiene: compressed %s → %s msgs, ~%s → ~%s tokens', _msg_count, _new_count, f'{_approx_tokens:,}', f'{_new_tokens:,}')
                                    if _new_tokens >= _warn_token_threshold:
                                        logger.warning('Session hygiene: still ~%s tokens after compression', f'{_new_tokens:,}')
                                    _comp = getattr(_hyg_agent, 'context_compressor', None)
                                    _hyg_aborted = _comp is not None and getattr(_comp, '_last_compress_aborted', False)
                                    if not _hyg_aborted:
                                        if hygiene_compaction_recovered(aborted=_hyg_aborted, rotated=_hyg_rotated, in_place=_hyg_in_place, msg_count=_msg_count, new_count=_new_count, approx_tokens=_approx_tokens, new_tokens=_new_tokens):
                                            _reset_hygiene_failure_streak(self, session_key)
                                    if _hyg_aborted:
                                        if _hyg_failure_cooldown_seconds >= 0:
                                            _record_hygiene_cooldown(self, session_entry.session_id, _hygiene_cooldown_for_failure(self, session_key, _hyg_failure_cooldown_seconds), getattr(_comp, '_last_summary_error', None))
                                        from agent.session_activity import ActivityProvenance
                                        _stamp_hygiene_compression_provenance(_hyg_agent, 'session hygiene compression aborted', ActivityProvenance.AGENT_COMPRESSION_COOLDOWN, 'hygiene compression abort activity stamp failed')
                                        _err = getattr(_comp, '_last_summary_error', None) or 'unknown error'
                                        from agent.redact import redact_sensitive_text
                                        _err = redact_sensitive_text(_err, force=True)
                                        _warn_msg = f'⚠️ Context compression aborted ({_err}). No messages were dropped — conversation is unchanged. Run /compress to retry, /reset for a clean session, or check your auxiliary.compression model configuration.'
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _warn_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning('Failed to deliver compression-failure warning to user: %s', _werr)
                                    elif _comp is not None and getattr(_comp, '_last_aux_model_failure_model', None):
                                        _aux_model = getattr(_comp, '_last_aux_model_failure_model', '')
                                        _aux_err = getattr(_comp, '_last_aux_model_failure_error', None) or 'unknown error'
                                        _aux_msg = f'ℹ️ Configured compression model `{_aux_model}` failed ({_aux_err}). Recovered using your main model — context is intact — but you may want to check `auxiliary.compression.model` in config.yaml.'
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _aux_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning('Failed to deliver aux-model-fallback notice to user: %s', _werr)
                                finally:
                                    self._evict_cached_agent(session_key)
                                    if not _hyg_cleanup_deferred:
                                        await self._cleanup_agent_resources_off_loop(_hyg_agent, context='session hygiene')
                    except Exception as e:
                        logger.warning('Session hygiene auto-compress failed: %s', e)
        if not history and (not await self.async_session_store.has_any_sessions()):
            _intro_note = "[System note: This is the user's very first message ever. Briefly introduce yourself and mention that /help shows available commands. Keep the introduction concise -- one or two sentences max.]"
            try:
                from agent.onboarding import PROFILE_BUILD_FLAG, is_seen, mark_seen, profile_build_directive, profile_build_mode
                _onb_cfg = _load_gateway_config()
                if profile_build_mode(_onb_cfg) == 'ask' and (not is_seen(_onb_cfg, PROFILE_BUILD_FLAG)):
                    turn_sidecar_notes.append(profile_build_directive().strip())
                    mark_seen(_hermes_home / 'config.yaml', PROFILE_BUILD_FLAG)
                else:
                    turn_sidecar_notes.append(_intro_note)
            except Exception as _pb_err:
                logger.debug('Profile-build onboarding directive failed, using plain intro: %s', _pb_err)
                turn_sidecar_notes.append(_intro_note)
        if not history and source.platform and (source.platform != Platform.LOCAL) and (source.platform != Platform.WEBHOOK):
            platform_name = source.platform.value
            env_key = _home_target_env_var(platform_name)
            home_env = ''
            try:
                from agent.secret_scope import get_secret
                home_env = (get_secret(env_key) or '').strip() if env_key else ''
            except Exception:
                home_env = ''
            if not home_env:
                home_env = (os.getenv(env_key) or '').strip() if env_key else ''
            try:
                if not home_env and self.config.get_home_channel(source.platform):
                    home_env = 'set'
            except Exception:
                pass
            if not home_env:
                try:
                    from gateway.config import load_gateway_config as _lgc
                    prof = (getattr(source, 'profile', None) or '').strip()
                    if prof and prof != 'default':
                        _pcfg = _lgc()
                        if _pcfg.get_home_channel(source.platform):
                            home_env = 'set'
                except Exception:
                    pass
            if not home_env:
                sethome_cmd = '/duck-agent sethome' if source.platform == Platform.SLACK else '/sethome'
                notice = f'📬 No home channel is set for {platform_name.title()}. A home channel is where Duck Agent delivers cron job results and cross-platform messages.\n\nType {sethome_cmd} to make this chat your home channel, or ignore to skip.'
                await self._deliver_platform_notice(source, notice)
        _vc_note = self._voice_channel_sidecar_note(event, source, session_key)
        if _vc_note:
            turn_sidecar_notes.append(_vc_note)
        message_text = await self._prepare_profile_scoped_inbound_message_text(event=event, source=source, history=history, session_key=session_key)
        if message_text is None:
            return
        try:
            from hermes_time import get_timezone as _get_evt_tz
            from gateway.message_timestamps import coerce_message_timestamp as _coerce_msg_ts, render_user_content_with_timestamp as _render_msg_ts, strip_leading_message_timestamps as _strip_msg_ts
            _evt_tz = _get_evt_tz()
            _evt_ts = getattr(event, 'timestamp', None)
            if message_text and isinstance(message_text, str):
                _clean_message_text, _embedded_ts = _strip_msg_ts(message_text, tz=_evt_tz)
                persist_user_message = _clean_message_text
                _event_epoch = _coerce_msg_ts(_evt_ts, tz=_evt_tz)
                persist_user_timestamp = _event_epoch if _event_epoch is not None else _embedded_ts
                if _message_timestamps_enabled(_load_gateway_config()):
                    message_text = _render_msg_ts(_clean_message_text, persist_user_timestamp, tz=_evt_tz)
                else:
                    message_text = _clean_message_text
        except Exception as _ts_err:
            logger.debug('Message timestamp injection failed (non-fatal): %s', _ts_err)
        if turn_sidecar_notes and session_key:
            self._set_pending_turn_sidecar_notes(session_key, turn_sidecar_notes)
        self._bind_adapter_run_generation(self._adapter_for_source(source), session_key, run_generation)
        try:
            hook_ctx = {'platform': source.platform.value if source.platform else '', 'user_id': source.user_id, 'chat_id': source.chat_id or '', 'thread_id': str(getattr(source, 'thread_id', None)) if getattr(source, 'thread_id', None) else '', 'chat_type': getattr(source, 'chat_type', '') or '', 'session_id': session_entry.session_id, 'message': message_text[:500]}
            await self.hooks.emit('agent:start', hook_ctx)
            _run_start_session_id = session_entry.session_id
            _turn_started_monotonic = time.monotonic()
            agent_result = await self._run_agent(message=message_text, context_prompt=context_prompt, history=history, source=source, session_id=_run_start_session_id, session_key=session_key, run_generation=run_generation, event_message_id=self._reply_anchor_for_event(event), channel_prompt=event.channel_prompt, moa_config=getattr(event, '_moa_config', None), persist_user_message=persist_user_message, persist_user_timestamp=persist_user_timestamp, message_type=event.message_type)
            _turn_seconds = time.monotonic() - _turn_started_monotonic
            try:
                _typing_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(type(_typing_adapter), '_stop_typing_with_metadata', None)
                _stop_typing = getattr(type(_typing_adapter), 'stop_typing', None)
                if _typing_adapter and callable(_stop_with_metadata):
                    await _typing_adapter._stop_typing_with_metadata(source.chat_id, self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)))
                elif _typing_adapter and callable(_stop_typing):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            if not self._is_session_run_current(_quick_key, run_generation):
                logger.info('Discarding stale agent result for %s — generation %d is no longer current', _quick_key or '?', run_generation)
                _stale_adapter = self._adapter_for_source(source)
                if getattr(type(_stale_adapter), 'pop_post_delivery_callback', None) is not None:
                    _stale_adapter.pop_post_delivery_callback(_quick_key, generation=run_generation)
                elif _stale_adapter and hasattr(_stale_adapter, '_post_delivery_callbacks'):
                    _stale_adapter._post_delivery_callbacks.pop(_quick_key, None)
                return None
            response = agent_result.get('final_response') or ''
            if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
                response = ''
            try:
                from gateway.response_filters import is_intentional_silence_agent_result
                _intentional_silence = is_intentional_silence_agent_result(agent_result, response)
            except Exception:
                _intentional_silence = False
            if response == '(empty)' and (not _intentional_silence):
                response = '⚠️ The model returned no response after processing tool results. This can happen with some models — try again or rephrase your question.'
            agent_messages = agent_result.get('messages', [])
            _response_time = time.time() - _msg_start_time
            _api_calls = agent_result.get('api_calls', 0)
            _resp_len = len(response)
            logger.info('response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars', _platform_name, source.chat_id or 'unknown', _response_time, _api_calls, _resp_len)
            if session_key and _should_clear_resume_pending_after_turn(agent_result):
                self._clear_restart_failure_count(session_key)
                try:
                    await self.async_session_store.clear_resume_pending(session_key)
                except Exception as _e:
                    logger.debug('clear_resume_pending failed for %s: %s', session_key, _e)
            if not _intentional_silence:
                response = _normalize_empty_agent_response(agent_result, response, history_len=len(history))
                response = _sanitize_gateway_final_response(source.platform, response)
            if agent_result.get('session_id') and agent_result['session_id'] != session_entry.session_id:
                if session_entry.session_id == _run_start_session_id:
                    session_entry.session_id = agent_result['session_id']
                    self._rebind_turn_lease(_quick_key, run_generation, session_entry.session_id)
                    await self.async_session_store._save()
                    await self.async_session_store._record_gateway_session_peer(session_entry.session_id, session_key, source)
                    await asyncio.to_thread(self._sync_telegram_topic_binding, source, session_entry, reason='agent-result-compression')
                else:
                    logger.info('Skipping agent-result session split sync for %s because the session binding moved from %s to %s before compression finished', session_key or '?', _run_start_session_id, session_entry.session_id)
            try:
                _show_reasoning_effective = _resolve_gateway_display_bool(_load_gateway_config(), _platform_config_key(source.platform), 'show_reasoning', default=bool(getattr(self, '_show_reasoning', False)), platform=source.platform, require_platform_override_for={Platform.MATTERMOST})
            except Exception:
                _show_reasoning_effective = False if source.platform == Platform.MATTERMOST else getattr(self, '_show_reasoning', False)
            if _show_reasoning_effective and response and (not _intentional_silence):
                last_reasoning = agent_result.get('last_reasoning')
                if last_reasoning:
                    from gateway.stream_consumer import escape_code_fences_for_display
                    lines = last_reasoning.strip().splitlines()
                    if len(lines) > 15:
                        display_reasoning = '\n'.join(lines[:15])
                        display_reasoning += f'\n_... ({len(lines) - 15} more lines)_'
                    else:
                        display_reasoning = last_reasoning.strip()
                    try:
                        from gateway.display_config import resolve_display_setting
                        _reasoning_style = resolve_display_setting(_load_gateway_config(), _platform_config_key(source.platform), 'reasoning_style', 'code')
                    except Exception:
                        _reasoning_style = 'code'
                    if _reasoning_style == 'subtext':
                        _quoted = '\n'.join((f'-# {ln}' if ln else '-#' for ln in display_reasoning.splitlines()))
                        response = f'-# 💭 Reasoning\n{_quoted}\n\n{response}'
                    elif _reasoning_style == 'blockquote':
                        _quoted = '\n'.join((f'> {ln}' if ln else '>' for ln in display_reasoning.splitlines()))
                        response = f'> 💭 **Reasoning:**\n{_quoted}\n\n{response}'
                    else:
                        display_reasoning = escape_code_fences_for_display(display_reasoning)
                        response = f'💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}'
            _footer_line = ''
            try:
                from gateway.runtime_footer import build_footer_line as _bfl
                _footer_line = _bfl(user_config=_load_gateway_config(), platform_key=_platform_config_key(source.platform), model=agent_result.get('model'), context_tokens=agent_result.get('last_prompt_tokens', 0) or 0, context_length=agent_result.get('context_length') or None, cwd=os.environ.get('TERMINAL_CWD', ''), turn_seconds=_turn_seconds)
            except Exception as _footer_err:
                logger.debug('runtime_footer build failed: %s', _footer_err)
                _footer_line = ''
            if _footer_line and response and (not agent_result.get('already_sent')) and (not _intentional_silence):
                response = f'{response}\n\n{_footer_line}'
            await self.hooks.emit('agent:end', {**hook_ctx, 'response': (response or '')[:500]})
            try:
                from tools.process_registry import process_registry
                watchers = process_registry.pending_watchers
                process_registry.pending_watchers = []
                for i, watcher in enumerate(watchers):
                    asyncio.create_task(self._run_process_watcher(watcher))
                    if i % 100 == 99:
                        await asyncio.sleep(0)
            except Exception as e:
                logger.error('Process watcher setup error: %s', e)
            try:
                from tools.process_registry import process_registry as _pr
                _watch_events = _drain_gateway_watch_events(_pr.completion_queue)
                for evt in _watch_events:
                    synth_text = _format_gateway_process_notification(evt)
                    if synth_text:
                        try:
                            await self._inject_watch_notification(synth_text, evt)
                        except Exception as e2:
                            logger.error('Watch notification injection error: %s', e2)
            except Exception as e:
                logger.debug('Watch queue drain error: %s', e)
            agent_failed_early = bool(agent_result.get('failed'))
            hidden_reasoning_incomplete = _is_gateway_hidden_reasoning_incomplete_turn(agent_result)
            _err_str_for_classify = str(agent_result.get('error', '')).lower()
            is_context_overflow_failure = agent_failed_early and (bool(agent_result.get('compression_exhausted')) or any((p in _err_str_for_classify for p in ('context length', 'context size', 'context window', 'maximum context', 'token limit', 'too many tokens', 'reduce the length', 'exceeds the limit', 'request entity too large', 'prompt is too long', 'payload too large', 'input is too long'))) or ('400' in _err_str_for_classify and len(history) > 50))
            if is_context_overflow_failure:
                logger.info('Skipping transcript persistence for context-overflow failure in session %s to prevent session growth loop.', session_entry.session_id)
            elif agent_failed_early:
                logger.info('Transient agent failure in session %s — persisting user message so conversation context is preserved on retry.', session_entry.session_id)
            elif hidden_reasoning_incomplete:
                logger.warning('Suppressing hidden-reasoning-only incomplete gateway turn for session %s: %s', session_entry.session_id, agent_result.get('error', 'processing incomplete'))
            if agent_result.get('compression_deferred'):
                logger.info('Compression deferred for session %s — the compression lock is held by a concurrent compressor. Keeping the session intact; the next message retries normally.', session_entry.session_id if session_entry else '?')
            elif agent_result.get('compression_exhausted') and session_entry and session_key:
                logger.info('Auto-resetting session %s after compression exhaustion.', session_entry.session_id)
                new_entry = await self.async_session_store.reset_session(session_key)
                self._evict_cached_agent(session_key)
                self._clear_conversation_scope(session_key, reason='compression_exhausted_reset')
                if new_entry is not None:
                    session_entry = new_entry
                    await asyncio.to_thread(self._sync_telegram_topic_binding, source, session_entry, reason='compression-exhausted-reset')
                response = (response or '') + '\n\n🔄 Session auto-reset — the conversation exceeded the maximum context size and could not be compressed further. Your next message will start a fresh session.'
            ts = time.time()
            if is_context_overflow_failure:
                pass
            elif not history:
                tool_defs = agent_result.get('tools', [])
                await self.async_session_store.append_to_transcript(session_entry.session_id, {'role': 'session_meta', 'tools': tool_defs or [], 'model': _resolve_gateway_model(), 'platform': source.platform.value if source.platform else '', 'timestamp': ts})
            agent_persisted = agent_result.get('agent_persisted', self._session_db is not None)
            if is_context_overflow_failure:
                pass
            elif agent_failed_early or hidden_reasoning_incomplete:
                _user_entry = {'role': 'user', 'content': persist_user_message if persist_user_message is not None else message_text, 'timestamp': persist_user_timestamp if persist_user_timestamp is not None else ts}
                if event.message_id:
                    _user_entry['message_id'] = str(event.message_id)
                _skip_persist = event.message_id and await self.async_session_store.has_platform_message_id(session_entry.session_id, str(event.message_id))
                if _skip_persist:
                    logger.info('Skipping duplicate user turn (message_id=%s) in session %s', event.message_id, session_entry.session_id)
                else:
                    await self.async_session_store.append_to_transcript(session_entry.session_id, _user_entry, skip_db=agent_persisted)
            else:
                history_len = agent_result.get('history_offset', len(history))
                new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []
                if not new_messages:
                    _user_entry = {'role': 'user', 'content': persist_user_message if persist_user_message is not None else message_text, 'timestamp': persist_user_timestamp if persist_user_timestamp is not None else ts}
                    if event.message_id:
                        _user_entry['message_id'] = str(event.message_id)
                    await self.async_session_store.append_to_transcript(session_entry.session_id, _user_entry, skip_db=agent_persisted)
                    if response:
                        await self.async_session_store.append_to_transcript(session_entry.session_id, {'role': 'assistant', 'content': response, 'timestamp': ts}, skip_db=agent_persisted)
                else:
                    _user_msg_id_attached = False
                    for msg in new_messages:
                        if msg.get('role') == 'system':
                            continue
                        entry = {**msg, 'timestamp': ts}
                        if not _user_msg_id_attached and msg.get('role') == 'user' and event.message_id and ('message_id' not in entry):
                            entry['message_id'] = str(event.message_id)
                            _user_msg_id_attached = True
                        await self.async_session_store.append_to_transcript(session_entry.session_id, entry, skip_db=agent_persisted)
            await self.async_session_store.update_session(session_entry.session_key, last_prompt_tokens=agent_result.get('last_prompt_tokens', 0))
            await self._refresh_agent_cache_message_count(session_key, session_entry.session_id)
            if _intentional_silence:
                logger.info('Suppressing intentional silence marker for session %s', session_entry.session_id)
                response = ''
            _already_sent = bool(agent_result.get('already_sent'))
            _stts_adapter = self._adapter_for_source(source)
            _streaming_tts_done = _stts_adapter is not None and bool(getattr(_stts_adapter, '_streaming_tts_turn_completed', lambda *_a, **_k: False)(session_key, run_generation))
            if not _streaming_tts_done and self._should_send_voice_reply(event, response, agent_messages, already_sent=_already_sent):
                await self._send_voice_reply(event, response)
            if agent_result.get('already_sent') and (not agent_result.get('failed')):
                if response:
                    _media_adapter = self._adapter_for_source(source)
                    if _media_adapter:
                        await self._deliver_media_from_response(response, event, _media_adapter)
                if _footer_line:
                    try:
                        _foot_adapter = self._adapter_for_source(source)
                        if _foot_adapter:
                            await _foot_adapter.send(source.chat_id, _footer_line, metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)))
                    except Exception as _e:
                        logger.debug('trailing footer send failed: %s', _e)
                return None
            return response
        except Exception as e:
            try:
                _err_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(type(_err_adapter), '_stop_typing_with_metadata', None)
                _stop_typing = getattr(type(_err_adapter), 'stop_typing', None)
                if _err_adapter and callable(_stop_with_metadata):
                    await _err_adapter._stop_typing_with_metadata(source.chat_id, self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)))
                elif _err_adapter and callable(_stop_typing):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            logger.exception('Agent error in session %s', session_key)
            try:
                if 'message_text' in locals() and message_text is not None and (session_entry is not None):
                    _already_persisted = False
                    try:
                        _recent_transcript = await self.async_session_store.load_transcript(session_entry.session_id)
                    except Exception:
                        _recent_transcript = []
                    for _msg in reversed(_recent_transcript[-10:]):
                        if _msg.get('role') == 'user':
                            _expected_user_content = persist_user_message if persist_user_message is not None else message_text
                            _already_persisted = _msg.get('content') == _expected_user_content
                            break
                    if not _already_persisted:
                        _user_entry = {'role': 'user', 'content': persist_user_message if persist_user_message is not None else message_text, 'timestamp': persist_user_timestamp if persist_user_timestamp is not None else time.time()}
                        if getattr(event, 'message_id', None):
                            _user_entry['message_id'] = str(event.message_id)
                        await self.async_session_store.append_to_transcript(session_entry.session_id, _user_entry)
            except Exception:
                logger.debug('Failed to persist inbound user message after agent exception', exc_info=True)
            status_hint = ''
            status_code = getattr(e, 'status_code', None)
            _hist_len = len(history) if 'history' in locals() else 0
            if status_code == 401:
                status_hint = ' Check your API key or run `claude /login` to refresh OAuth credentials.'
            elif status_code == 402:
                status_hint = ' Your API balance or quota is exhausted. Check your provider dashboard.'
            elif status_code == 429:
                _err_body = getattr(e, 'response', None)
                _err_json = {}
                try:
                    if _err_body is not None:
                        _err_json = _err_body.json().get('error', {})
                        if not isinstance(_err_json, dict):
                            _err_json = {}
                except Exception:
                    pass
                if _err_json.get('type') == 'usage_limit_reached':
                    _resets_in = _err_json.get('resets_in_seconds')
                    if _resets_in and _resets_in > 0:
                        import math
                        _hours = math.ceil(_resets_in / 3600)
                        status_hint = f" Your plan's usage limit has been reached. It resets in ~{_hours}h."
                    else:
                        status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
                else:
                    status_hint = ' You are being rate-limited. Please wait a moment and try again.'
            elif status_code == 529:
                status_hint = ' The API is temporarily overloaded. Please try again shortly.'
            elif status_code in {400, 500}:
                if _hist_len > 50:
                    return "⚠️ Session too large for the model's context window.\nUse /compact to compress the conversation, or /reset to start fresh."
                elif status_code == 400:
                    status_hint = ' The request was rejected by the API.'
            return f'Sorry, I encountered an unexpected error.{status_hint}\nTry again or use /reset to start a fresh session.'
        finally:
            self._clear_session_env(_session_env_tokens)

    def _reset_notice_session_info(self, source: SessionSource) -> str:
        """Session-info block for the auto-reset notice, profile-scoped.

        When multiplexing, resolve model/provider/context inside the profile
        serving ``source`` — otherwise the banner advertises the base config's
        model while the session actually runs on the profile's (#59003).
        Mirrors ``_run_agent``'s gating so single-profile gateways never
        enter the scope.

        Call via ``asyncio.to_thread`` from async handlers: under the scope,
        resolution can do blocking work (credential refresh, context-length
        HTTP probes) that must not run on the event loop. The scope is entered
        inside this method, so contextvars behave correctly in the worker
        thread.
        """
        if getattr(getattr(self, 'config', None), 'multiplex_profiles', False):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return self._format_session_info()
        return self._format_session_info()

    def _format_session_info(self) -> str:
        """Resolve current model config and return a formatted info block.

        Surfaces model, provider, context length, and endpoint so gateway
        users can immediately see if context detection went wrong (e.g.
        local models falling to the 128K default).
        """
        from agent.model_metadata import get_model_context_length, DEFAULT_FALLBACK_CONTEXT
        model = _resolve_gateway_model()
        config_context_length = None
        provider = None
        base_url = None
        api_key = None
        custom_provs = None
        data = None
        configured_model = None
        configured_provider = None
        configured_base_url = None
        try:
            data = _load_gateway_config()
            if data:
                model_cfg = data.get('model', {})
                if isinstance(model_cfg, dict):
                    configured_model = model_cfg.get('default') or model_cfg.get('model')
                    raw_ctx = model_cfg.get('context_length')
                    if raw_ctx is not None:
                        try:
                            config_context_length = int(raw_ctx)
                        except (TypeError, ValueError):
                            pass
                    provider = model_cfg.get('provider') or None
                    base_url = model_cfg.get('base_url') or None
                    configured_provider = provider
                    configured_base_url = base_url
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(data)
                except Exception:
                    custom_provs = data.get('custom_providers')
        except Exception:
            pass
        try:
            runtime = _resolve_runtime_agent_kwargs()
            provider = runtime.get('provider') or provider
            base_url = runtime.get('base_url') or base_url
            api_key = runtime.get('api_key')
        except Exception:
            pass
        if config_context_length is not None:
            try:
                from hermes_cli.route_identity import should_clear_context_pin
                if should_clear_context_pin(configured_model, model, configured_base_url, base_url, configured_provider, provider):
                    config_context_length = None
            except Exception:
                config_context_length = None
        if config_context_length is None and custom_provs and base_url:
            try:
                from hermes_cli.config import get_custom_provider_context_length
                custom_ctx = get_custom_provider_context_length(model=model, base_url=base_url, custom_providers=custom_provs)
                if custom_ctx:
                    config_context_length = custom_ctx
            except Exception:
                pass
        context_length = get_model_context_length(model, base_url=base_url or '', api_key=api_key or '', config_context_length=config_context_length, provider=provider or '', custom_providers=custom_provs)
        if config_context_length is not None:
            ctx_source = 'config'
        elif context_length == DEFAULT_FALLBACK_CONTEXT:
            ctx_source = 'default — set model.context_length in config to override'
        else:
            ctx_source = 'detected'
        if context_length >= 1000000:
            ctx_display = f'{context_length / 1000000:.1f}M'
        elif context_length >= 1000:
            ctx_display = f'{context_length // 1000}K'
        else:
            ctx_display = str(context_length)
        lines = [f'◆ Model: `{model}`', f"◆ Provider: {provider or 'openrouter'}", f'◆ Context: {ctx_display} tokens ({ctx_source})']
        if base_url and ('localhost' in base_url or '127.0.0.1' in base_url or '0.0.0.0' in base_url):
            lines.append(f'◆ Endpoint: {base_url}')
        return '\n'.join(lines)

    def _check_slash_access(self, source: SessionSource, canonical_cmd: str) -> Optional[str]:
        """Return a denial message if ``source`` cannot run ``canonical_cmd``,
        else None. Used by both the cold and running-agent dispatch paths
        in ``_handle_message`` so admin/user gating can't be bypassed by
        an in-flight agent.

        Backward-compat semantics live in
        :func:`gateway.slash_access.policy_for_source` — when the operator
        hasn't set ``allow_admin_from`` for the scope, the policy returns
        ``enabled=False`` and this method always returns None.
        """
        from gateway.slash_access import policy_for_source as _policy_for_source
        if not canonical_cmd:
            return None
        policy = _policy_for_source(self.config, source)
        if not policy.enabled or policy.can_run(source.user_id, canonical_cmd):
            return None
        logger.info('Slash command /%s denied for %s:%s (not admin, not in user_allowed_commands)', canonical_cmd, source.platform.value if source.platform else '?', source.user_id)
        allowed_preview = sorted(policy.user_allowed_commands)
        if allowed_preview:
            suffix = 'You can run: ' + ', '.join((f'/{c}' for c in allowed_preview[:12])) + ('…' if len(allowed_preview) > 12 else '') + '. Use /whoami for the full list.'
        else:
            suffix = 'No slash commands are enabled for non-admins on this platform. Ask an admin to add you to allow_admin_from or to set user_allowed_commands.'
        return f'⛔ /{canonical_cmd} is admin-only here. {suffix}'

    def _sibling_thread_run_keys(self, source: SessionSource, own_key: str) -> list:
        """Find running-agent keys for OTHER participants in the same thread.

        Only applies when the message originates in a thread.  In per-user
        thread mode (``thread_sessions_per_user=True``) each participant gets
        an isolated session key of the form
        ``agent:main:{platform}:{chat_type}:{chat_id}:{thread_id}:{user_id}``,
        so a run started by another user is invisible to the caller's own
        ``/stop``.  This returns the keys of any *actually running* agents
        (not the pending sentinel, not the caller's own key) whose key shares
        the caller's ``{chat_id}:{thread_id}`` prefix.

        Returns an empty list when the source is not in a thread, or when no
        sibling runs exist — callers must still gate on authorization.
        """
        thread_id = getattr(source, 'thread_id', None)
        chat_id = getattr(source, 'chat_id', None)
        if not thread_id or not chat_id:
            return []
        platform = source.platform.value
        chat_type = getattr(source, 'chat_type', None) or ''
        prefix = ':'.join(['agent:main', platform, chat_type, str(chat_id), str(thread_id)])
        matches = []
        for key, agent in self._running_agent_items():
            if key == own_key:
                continue
            if agent is _AGENT_PENDING_SENTINEL or not agent:
                continue
            if key == prefix or key.startswith(prefix + ':'):
                matches.append(key)
        return matches

    def _is_stale_restart_redelivery(self, event: MessageEvent) -> bool:
        """Return True if this /restart is a Telegram re-delivery we already handled.

        The previous gateway wrote ``.restart_last_processed.json`` with the
        triggering platform + update_id when it processed the /restart.  If
        we now see a /restart on the same platform with an update_id <= that
        recorded value, it is a redelivery when this process booted from that
        restart. Otherwise the marker must still be recent (< 5 minutes).

        Only applies to Telegram today (the only platform that exposes a
        numeric cross-session update ordering); other platforms return False.
        """
        if event is None or event.source is None:
            return False
        if event.platform_update_id is None:
            return False
        if event.source.platform is None:
            return False
        try:
            platform_value = event.source.platform.value
        except Exception:
            return False
        if platform_value != 'telegram':
            return False
        try:
            marker_path = _hermes_home / '.restart_last_processed.json'
            if not marker_path.exists():
                if getattr(self, '_booted_from_restart', False) and time.time() - getattr(self, '_startup_time', 0.0) < 60:
                    self._booted_from_restart = False
                    return True
                return False
            data = json.loads(marker_path.read_text(encoding='utf-8'))
        except Exception:
            return False
        if data.get('platform') != platform_value:
            return False
        recorded_uid = data.get('update_id')
        if not isinstance(recorded_uid, int):
            return False
        if event.platform_update_id > recorded_uid:
            return False
        if getattr(self, '_booted_from_restart', False):
            self._booted_from_restart = False
            return True
        requested_at = data.get('requested_at')
        if isinstance(requested_at, (int, float)):
            if time.time() - requested_at > 300:
                return False
        return True

    async def _handle_suggestions_command(self, event: MessageEvent) -> str:
        """Handle /suggestions in the gateway.

        Delegates to the shared handler so CLI and gateway never drift. The
        origin is built from the event source so an accepted suggestion's job
        delivers back to this chat/thread.
        """
        args = (event.get_command_args() or '').strip()
        source = event.source
        origin = None
        try:
            platform = getattr(source.platform, 'value', None) or str(getattr(source, 'platform', '') or '')
            chat_id = getattr(source, 'chat_id', None)
            if platform and chat_id:
                origin = {'platform': platform, 'chat_id': str(chat_id), 'chat_name': getattr(source, 'chat_name', None), 'thread_id': getattr(source, 'thread_id', None)}
        except Exception:
            origin = None
        try:
            from hermes_cli.suggestions_cmd import handle_suggestions_command
            return handle_suggestions_command(args, origin=origin, surface='gateway')
        except Exception as e:
            logger.debug('suggestions command failed: %s', e)
            return f'Suggestions command failed: {e}'

    async def _handle_blueprint_command(self, event: MessageEvent):
        """Handle /blueprint in the gateway.

        Delegates to the shared handler so CLI, TUI, and gateway never drift.
        Returns a BlueprintCommandResult: ``text`` is shown to the user, and if
        ``agent_seed`` is set the dispatch site rewrites ``event.text`` to the
        seed and falls through to the agent (the ``/steer`` pattern) so the
        agent gathers the slot values conversationally. Origin is built from the
        event source so a directly created blueprint job delivers back to this chat.
        """
        args = (event.get_command_args() or '').strip()
        source = event.source
        origin = None
        try:
            platform = getattr(source.platform, 'value', None) or str(getattr(source, 'platform', '') or '')
            chat_id = getattr(source, 'chat_id', None)
            if platform and chat_id:
                origin = {'platform': platform, 'chat_id': str(chat_id), 'chat_name': getattr(source, 'chat_name', None), 'thread_id': getattr(source, 'thread_id', None)}
        except Exception:
            origin = None
        try:
            from hermes_cli.blueprint_cmd import handle_blueprint_command
            return handle_blueprint_command(args, origin=origin, surface='gateway')
        except Exception as e:
            logger.debug('blueprint command failed: %s', e)
            from hermes_cli.blueprint_cmd import BlueprintCommandResult
            return BlueprintCommandResult(f'Cron blueprint command failed: {e}')

    def _goal_max_turns_from_config(self) -> int:
        """Resolve the configured /goal turn budget for gateway sessions.

        GatewayRunner.config is a GatewayConfig dataclass, not the full
        user config mapping. Top-level config blocks such as ``goals`` are
        therefore only available through hermes_cli.config.load_config().
        """
        try:
            goals_cfg = (self.config or {}).get('goals', {}) if isinstance(self.config, dict) else getattr(self.config, 'goals', {}) or {}
            if not goals_cfg:
                from hermes_cli.config import load_config
                goals_cfg = (load_config() or {}).get('goals') or {}
            return int(goals_cfg.get('max_turns', 20) or 20)
        except Exception:
            return 20

    async def _get_goal_manager_for_event(self, event: 'MessageEvent'):
        """Return a GoalManager bound to the session for this gateway event.

        Returns ``(manager, session_entry)`` or ``(None, None)`` if the
        goals module can't be loaded.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug('goal manager unavailable: %s', exc)
            return (None, None)
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception as exc:
            logger.debug('goal manager: session lookup failed: %s', exc)
            return (None, None)
        sid = getattr(session_entry, 'session_id', None) or ''
        if not sid:
            return (None, None)
        max_turns = self._goal_max_turns_from_config()
        return (GoalManager(session_id=sid, default_max_turns=max_turns), session_entry)

    async def _get_heartbeat_manager_for_event(self, event: 'MessageEvent'):
        """Return a HeartbeatManager bound to the session for this event.

        Returns ``(manager, session_entry)`` or ``(None, None)``.
        """
        try:
            from hermes_cli.heartbeat import HeartbeatManager
        except Exception as exc:
            logger.debug('heartbeat manager unavailable: %s', exc)
            return (None, None)
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception as exc:
            logger.debug('heartbeat manager: session lookup failed: %s', exc)
            return (None, None)
        sid = getattr(session_entry, 'session_id', None) or ''
        if not sid:
            return (None, None)
        return (HeartbeatManager(session_id=sid), session_entry)

    def _register_heartbeat_watch(self, quick_key: str, source: Any, session_id: str) -> None:
        """Track a session with an active heartbeat and start the poller.

        The registry maps ``quick_key`` → ``(source, session_id)`` so the
        poller can rebuild a MessageEvent and enqueue via the adapter FIFO.
        In-memory by design: heartbeat STATE survives restarts in SessionDB,
        but firing resumes when the user touches /heartbeat again in the new
        gateway process (documented; durable schedules belong to cron).
        """
        watch = getattr(self, '_heartbeat_watch', None)
        if watch is None:
            watch = {}
            self._heartbeat_watch = watch
        watch[quick_key] = (source, session_id)
        self._start_heartbeat_poller()

    def _unregister_heartbeat_watch(self, quick_key: str) -> None:
        watch = getattr(self, '_heartbeat_watch', None)
        if watch:
            watch.pop(quick_key, None)

    def _start_heartbeat_poller(self) -> None:
        """Start the single gateway-wide heartbeat poll task (idempotent)."""
        existing = getattr(self, '_heartbeat_poll_task', None)
        if existing is not None and (not existing.done()):
            return
        from hermes_cli.heartbeat import POLL_SECONDS

        async def _poll_loop():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                watch = getattr(self, '_heartbeat_watch', None)
                if not watch:
                    continue
                for quick_key, (source, session_id) in list(watch.items()):
                    try:
                        if quick_key in self._running_agents:
                            continue
                        from hermes_cli.heartbeat import HeartbeatManager
                        mgr = HeartbeatManager(session_id=session_id)
                        if not mgr.has_heartbeat():
                            watch.pop(quick_key, None)
                            continue
                        prompt = mgr.due_prompt()
                        if not prompt:
                            continue
                        adapter = self._adapter_for_source(source)
                        if adapter is None:
                            continue
                        hb_event = MessageEvent(text=prompt, message_type=MessageType.TEXT, source=source, message_id=None, channel_prompt=None)
                        self._enqueue_fifo(quick_key, hb_event, adapter)
                    except Exception as exc:
                        logger.debug('heartbeat poll for %s failed: %s', quick_key, exc)
        try:
            task = asyncio.create_task(_poll_loop())
            self._heartbeat_poll_task = task
            _bg = getattr(self, '_background_tasks', None)
            if _bg is not None:
                _bg.add(task)
                task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug('Failed to start heartbeat poller', exc_info=True)

    async def _send_goal_status_notice(self, source: Any, message: str) -> None:
        """Send a /goal judge status line back to the originating chat/thread."""
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug('goal continuation: no adapter for %s', getattr(source, 'platform', None))
            return
        try:
            metadata = self._thread_metadata_for_source(source)
        except Exception:
            metadata = None
        result = await adapter.send(source.chat_id, message, metadata=metadata)
        if result is not None and (not getattr(result, 'success', True)):
            logger.warning('goal continuation: status send failed: %s', getattr(result, 'error', 'unknown error'))

    async def _defer_goal_status_notice_after_delivery(self, source: Any, message: str) -> None:
        """Send a /goal status line after the main response is delivered.

        The gateway message handler returns the agent response to the platform
        adapter, which sends it after this method's caller has returned.  For a
        natural Discord/Telegram reading order, goal status belongs after that
        send.  Platform adapters provide a one-shot post-delivery callback for
        exactly this boundary; when unavailable, fall back to direct awaited
        delivery rather than silently dropping the notice.
        """
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug('goal continuation: no adapter for %s', getattr(source, 'platform', None))
            return

        async def _deliver() -> None:
            try:
                await self._send_goal_status_notice(source, message)
            except Exception as exc:
                logger.warning('goal continuation: status send failed: %s', exc, exc_info=True)
        try:
            session_key = self._session_key_for_source(source)
        except Exception:
            session_key = None
        if session_key and hasattr(adapter, 'register_post_delivery_callback'):
            try:
                generation = None
                active = getattr(adapter, '_active_sessions', {}).get(session_key)
                if active is not None:
                    generation = getattr(active, '_hermes_run_generation', None)
                adapter.register_post_delivery_callback(session_key, _deliver, generation=generation)
                return
            except Exception as exc:
                logger.debug('goal continuation: post-delivery callback registration failed: %s', exc)
        await _deliver()

    async def _post_turn_goal_continuation(self, *, session_entry: Any, source: Any, final_response: str) -> None:
        """Run the goal judge after a gateway turn and, if still active,
        enqueue a continuation prompt for the same session.

        Called from ``_handle_message_with_agent`` at turn boundary, AFTER
        the response has been delivered. Safe when no goal is set.

        We use the adapter's pending-message / FIFO machinery so any real
        user message that arrives simultaneously is handled by the same
        queue and takes priority naturally.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug('goal continuation: goals module unavailable: %s', exc)
            return
        sid = getattr(session_entry, 'session_id', None) or ''
        if not sid:
            return
        max_turns = self._goal_max_turns_from_config()
        mgr = GoalManager(session_id=sid, default_max_turns=max_turns)
        if not mgr.is_active():
            return
        try:
            from hermes_cli.goals import gather_background_processes as _gather_bg
            _bg_procs = _gather_bg()
        except Exception:
            _bg_procs = None
        decision = mgr.evaluate_after_turn(final_response or '', user_initiated=True, background_processes=_bg_procs)
        msg = decision.get('message') or ''
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)
        if not decision.get('should_continue'):
            return
        prompt = decision.get('continuation_prompt') or ''
        if not prompt or source is None:
            return
        try:
            adapter = self._adapter_for_source(source)
            _quick_key = self._session_key_for_source(source)
            if adapter and _quick_key:
                cont_event = MessageEvent(text=prompt, message_type=MessageType.TEXT, source=source, message_id=None, channel_prompt=None)
                self._enqueue_fifo(_quick_key, cont_event, adapter)
        except Exception as exc:
            logger.debug('goal continuation: enqueue failed: %s', exc)

    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        """Extract Discord guild_id from the raw message object."""
        raw = getattr(event, 'raw_message', None)
        if raw is None:
            return None
        if hasattr(raw, 'guild_id') and raw.guild_id:
            return int(raw.guild_id)
        if hasattr(raw, 'guild') and raw.guild:
            return raw.guild.id
        return None

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        if not hasattr(adapter, 'join_voice_channel'):
            return 'Voice channels are not supported on this platform.'
        guild_id = self._get_guild_id(event)
        if not guild_id:
            return 'This command only works in a Discord server.'
        voice_channel = await adapter.get_user_voice_channel(guild_id, event.source.user_id)
        if not voice_channel:
            return 'You need to be in a voice channel first.'
        if hasattr(adapter, '_voice_input_callback'):
            adapter._voice_input_callback = self._handle_voice_channel_input
        if hasattr(adapter, '_on_voice_disconnect'):
            adapter._on_voice_disconnect = self._handle_voice_timeout_cleanup
        if hasattr(adapter, '_voice_mode_getter'):
            adapter._voice_mode_getter = lambda chat_id: self._voice_mode.get(self._voice_key(Platform.DISCORD, str(chat_id)), 'off')
        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning('Failed to join voice channel: %s', e)
            adapter._voice_input_callback = None
            err_lower = str(e).lower()
            if 'pynacl' in err_lower or 'nacl' in err_lower or 'davey' in err_lower:
                return f'Voice dependencies are missing (PyNaCl / davey). Install with: `{sys.executable} -m pip install PyNaCl`'
            return f'Failed to join voice channel: {e}'
        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            if hasattr(adapter, '_voice_sources'):
                adapter._voice_sources[guild_id] = event.source.to_dict()
            self._voice_mode[self._voice_key(event.source.platform, event.source.chat_id)] = 'all'
            self._save_voice_modes()
            self._set_adapter_auto_tts_enabled(adapter, event.source.chat_id, enabled=True)
            return f"Joined voice channel **{voice_channel.name}**.\nI'll speak my replies and listen to you. Use /voice leave to disconnect."
        adapter._voice_input_callback = None
        return 'Failed to join voice channel. Check bot permissions (Connect + Speak).'

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        guild_id = self._get_guild_id(event)
        if not guild_id or not hasattr(adapter, 'leave_voice_channel'):
            return 'Not in a voice channel.'
        if not hasattr(adapter, 'is_in_voice_channel') or not adapter.is_in_voice_channel(guild_id):
            return 'Not in a voice channel.'
        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning('Error leaving voice channel: %s', e)
        self._voice_mode[self._voice_key(event.source.platform, event.source.chat_id)] = 'off'
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=True)
        if hasattr(adapter, '_voice_input_callback'):
            adapter._voice_input_callback = None
        return 'Left voice channel.'

    def _handle_voice_timeout_cleanup(self, chat_id: str) -> None:
        """Called by the adapter when a voice channel times out.

        Cleans up runner-side voice_mode state that the adapter cannot reach.
        """
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id)] = 'off'
        self._save_voice_modes()
        adapter = self.adapters.get(Platform.DISCORD)
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    def _is_duplicate_voice_transcript(self, guild_id: int, user_id: int, transcript: str) -> bool:
        """Suppress repeated STT outputs for the same recent utterance.

        Voice capture can occasionally emit the same utterance twice a few
        seconds apart, which creates a second queued agent run and overlapping
        spoken replies. Dedup exact and near-exact repeats per guild/user over a
        short window while allowing genuinely new turns through.
        """
        from difflib import SequenceMatcher
        normalized = re.sub('\\s+', ' ', transcript).strip().lower()
        normalized = re.sub('[^\\w\\s]', '', normalized)
        if not normalized:
            return False
        now = time.monotonic()
        window_seconds = 12.0
        key = (guild_id, user_id)
        recent_store = getattr(self, '_recent_voice_transcripts', None)
        if not isinstance(recent_store, dict):
            recent_store = {}
            self._recent_voice_transcripts = recent_store
        recent = [(ts, txt) for ts, txt in recent_store.get(key, []) if now - ts <= window_seconds]
        for _, prior in recent:
            if prior == normalized:
                recent_store[key] = recent
                return True
            if len(prior) >= 16 and len(normalized) >= 16:
                if SequenceMatcher(None, prior, normalized).ratio() >= 0.95:
                    recent_store[key] = recent
                    return True
        recent.append((now, normalized))
        recent_store[key] = recent[-5:]
        return False

    async def _handle_voice_channel_input(self, guild_id: int, user_id: int, transcript: str):
        """Handle transcribed voice from a user in a voice channel.

        Creates a synthetic MessageEvent and processes it through the
        adapter's full message pipeline (session, typing, agent, TTS reply).
        """
        adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return
        text_ch_id = adapter._voice_text_channels.get(guild_id)
        if not text_ch_id:
            return
        source_data = getattr(adapter, '_voice_sources', {}).get(guild_id)
        if source_data:
            source = SessionSource.from_dict(source_data)
            source.user_id = str(user_id)
            source.user_name = str(user_id)
        else:
            source = SessionSource(platform=Platform.DISCORD, chat_id=str(text_ch_id), user_id=str(user_id), user_name=str(user_id), chat_type='channel')
        if not self._is_user_authorized(source):
            logger.debug('Unauthorized voice input from user %d, ignoring', user_id)
            return
        if self._is_duplicate_voice_transcript(guild_id, user_id, transcript):
            logger.info('Suppressing duplicate voice transcript for guild=%s user=%s: %s', guild_id, user_id, transcript[:100])
            return
        try:
            channel = adapter._client.get_channel(text_ch_id)
            if channel:
                safe_text = transcript[:2000].replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
                await channel.send(f'**[Voice]** <@{user_id}>: {safe_text}')
        except Exception:
            pass
        from types import SimpleNamespace
        channel_prompt: Optional[str] = None
        resolver = getattr(adapter, '_resolve_channel_prompt', None)
        if callable(resolver):
            try:
                resolved = resolver(str(text_ch_id))
                channel_prompt = resolved if isinstance(resolved, str) else None
            except Exception:
                channel_prompt = None
        event = MessageEvent(source=source, text=transcript, message_type=MessageType.VOICE, raw_message=SimpleNamespace(guild_id=guild_id, guild=None), channel_prompt=channel_prompt)
        await adapter.handle_message(event)

    def _should_send_voice_reply(self, event: MessageEvent, response: str, agent_messages: list, already_sent: bool=False) -> bool:
        """Decide whether the runner should send a TTS voice reply.

        Returns False when:
        - voice_mode is off for this chat
        - response is empty or an error
        - agent already called text_to_speech tool (dedup)
        - voice input and base adapter auto-TTS already handled it (skip_double)
          UNLESS streaming already consumed the response (already_sent=True),
          in which case the base adapter won't have text for auto-TTS so the
          runner must handle it.
        """
        if not response or response.startswith('Error:'):
            return False
        chat_id = event.source.chat_id
        voice_key = self._voice_key(event.source.platform, chat_id)
        voice_mode = self._voice_mode.get(voice_key)
        is_voice_input = event.message_type == MessageType.VOICE
        adapter = self.adapters.get(event.source.platform)
        adapter_auto_tts = False
        if adapter and hasattr(adapter, '_should_auto_tts_for_chat'):
            try:
                adapter_auto_tts = bool(adapter._should_auto_tts_for_chat(chat_id))
            except Exception:
                adapter_auto_tts = False
        should = voice_mode == 'all' or (voice_mode == 'voice_only' and is_voice_input) or (voice_mode is None and adapter_auto_tts)
        if not should:
            logger.debug('Auto voice reply skipped: mode=%s adapter_auto_tts=%s chat=%s platform=%s', voice_mode, adapter_auto_tts, chat_id, event.source.platform.value)
            return False
        last_user_idx = None
        for i, msg in enumerate(reversed(agent_messages)):
            if msg.get('role') == 'user':
                last_user_idx = len(agent_messages) - 1 - i
                break
        turn_messages = agent_messages[last_user_idx:] if last_user_idx is not None else agent_messages
        has_agent_tts = any((msg.get('role') == 'assistant' and any(((tc.get('function') or {}).get('name') == 'text_to_speech' for tc in msg.get('tool_calls') or [])) for msg in turn_messages))
        if has_agent_tts:
            return False
        if is_voice_input and (not already_sent):
            return False
        return True

    def _should_echo_stt_transcripts(self) -> bool:
        """Return whether inbound voice/STT transcripts should be echoed to chat."""
        return bool(getattr(self.config, 'stt_echo_transcripts', True))

    async def _send_voice_reply(self, event: MessageEvent, text: str) -> None:
        """Generate TTS audio and send as a voice message before the text reply."""
        audio_path = None
        actual_path = None
        try:
            from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts
            tts_text = _strip_markdown_for_tts(text[:4000])
            if not tts_text:
                return
            audio_path = build_auto_tts_output_path(event.source.platform)
            result_json = await asyncio.to_thread(text_to_speech_tool, text=tts_text, output_path=audio_path)
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning('Auto voice reply TTS returned invalid JSON: %s', result_json[:200] if result_json else result_json)
                return
            actual_path = result.get('file_path', audio_path)
            if not result.get('success') or not os.path.isfile(actual_path):
                logger.warning('Auto voice reply TTS failed: %s', result.get('error'))
                return
            adapter = self._adapter_for_source(event.source)
            guild_id = self._get_guild_id(event)
            if guild_id and hasattr(adapter, 'play_in_voice_channel') and hasattr(adapter, 'is_in_voice_channel') and adapter.is_in_voice_channel(guild_id):
                await adapter.play_in_voice_channel(guild_id, actual_path)
            elif adapter and hasattr(adapter, 'send_voice'):
                reply_anchor = self._reply_anchor_for_event(event)
                thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
                if thread_meta is not None:
                    thread_meta = dict(thread_meta)
                    thread_meta['notify'] = True
                else:
                    thread_meta = {'notify': True}
                send_kwargs: Dict[str, Any] = {'chat_id': event.source.chat_id, 'audio_path': actual_path, 'reply_to': reply_anchor, 'metadata': thread_meta}
                await adapter.send_voice(**send_kwargs)
        except Exception as e:
            logger.warning('Auto voice reply failed: %s', e, exc_info=True)
        finally:
            for p in {audio_path, actual_path} - {None}:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def _deliver_media_from_response(self, response: str, event: MessageEvent, adapter) -> None:
        """Extract explicit MEDIA: tags from a response and deliver them.

        Called after streaming has already sent the text to the user, so the
        text itself is already delivered — this only handles file attachments
        that the normal _process_message_background path would have caught.

        Unlike the non-streaming path in ``gateway/platforms/base.py`` (which
        also auto-detects bare local paths via ``extract_local_files``), this
        post-stream rescan is EXPLICIT-ONLY. The visible reply has already
        been streamed verbatim, so a bare path string here was either (a)
        already shown to the user as text, or (b) stale tool/inspected
        content that was never part of the intended visible reply. Promoting
        such paths into uploads after the fact sent files the model never
        asked to deliver (#20834). Only ``MEDIA:`` directives — the explicit
        attachment contract — trigger post-stream uploads.
        """
        from pathlib import Path
        from urllib.parse import quote as _quote
        try:
            force_document_attachments = '[[as_document]]' in response
            from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio
            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            adapter.extract_images(cleaned)
            _thread_meta = self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event))
            _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
            _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
            image_paths: list = []
            non_image_media: list = []
            for media_path, is_voice in media_files:
                ext = Path(media_path).suffix.lower()
                if ext in _IMAGE_EXTS and (not is_voice) and (not force_document_attachments):
                    image_paths.append(media_path)
                else:
                    non_image_media.append((media_path, is_voice))
            if image_paths:
                try:
                    images = [(f'file://{_quote(p)}', '') for p in image_paths]
                    await adapter.send_multiple_images(chat_id=event.source.chat_id, images=images, metadata=_thread_meta)
                except Exception as e:
                    logger.warning('[%s] Post-stream image batch delivery failed: %s', adapter.name, e)
            for media_path, is_voice in non_image_media:
                try:
                    ext = Path(media_path).suffix.lower()
                    if should_send_media_as_audio(event.source.platform, ext, is_voice=is_voice):
                        await adapter.send_voice(chat_id=event.source.chat_id, audio_path=media_path, metadata=_thread_meta)
                    elif ext in _VIDEO_EXTS:
                        await adapter.send_video(chat_id=event.source.chat_id, video_path=media_path, metadata=_thread_meta)
                    else:
                        await adapter.send_document(chat_id=event.source.chat_id, file_path=media_path, metadata=_thread_meta)
                except Exception as e:
                    logger.warning('[%s] Post-stream media delivery failed: %s', adapter.name, e)
        except Exception as e:
            logger.warning('Post-stream media extraction failed: %s', e)

    async def _run_background_task(self, prompt: str, source: 'SessionSource', task_id: str, event_message_id: Optional[str]=None, media_urls: Optional[List[str]]=None, media_types: Optional[List[str]]=None) -> None:
        """Profile-scoping wrapper around the background agent task.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole task inside ``_profile_runtime_scope`` so credentials
        resolve from that profile's secret scope. Mirrors the pattern in
        ``_run_agent``.
        """
        if not getattr(getattr(self, 'config', None), 'multiplex_profiles', False):
            return await self._run_background_task_inner(prompt, source, task_id, event_message_id, media_urls, media_types)
        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_background_task_inner(prompt, source, task_id, event_message_id, media_urls, media_types)

    async def _run_background_task_inner(self, prompt: str, source: 'SessionSource', task_id: str, event_message_id: Optional[str]=None, media_urls: Optional[List[str]]=None, media_types: Optional[List[str]]=None) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from run_agent import AIAgent
        media_urls = media_urls or []
        media_types = media_types or []
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.warning('No adapter for platform %s in background task %s', source.platform, task_id)
            return
        _thread_metadata = self._thread_metadata_for_source(source, event_message_id)
        try:
            user_config = _load_gateway_config()
            model, runtime_kwargs = self._resolve_session_agent_runtime(source=source, user_config=user_config)
            if not runtime_kwargs.get('api_key'):
                await adapter.send(source.chat_id, f'❌ Background task {task_id} failed: no provider credentials configured.', metadata=_thread_metadata)
                return
            platform_key = _platform_config_key(source.platform)
            from hermes_cli.tools_config import _get_platform_tools
            enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
            agent_cfg = user_config.get('agent') or {}
            disabled_toolsets = agent_cfg.get('disabled_toolsets') or None
            pr = self._provider_routing
            max_iterations = _current_max_iterations()
            reasoning_config = self._resolve_session_reasoning_config(source=source, model=model)
            self._reasoning_config = reasoning_config
            self._service_tier = self._resolve_session_service_tier(source=source)
            turn_route = self._resolve_turn_agent_config(prompt, model, runtime_kwargs)
            enriched_prompt = prompt
            if media_urls:
                image_paths = []
                for i, path in enumerate(media_urls):
                    mtype = media_types[i] if i < len(media_types) else ''
                    if mtype.startswith('image/'):
                        image_paths.append(path)
                if image_paths:
                    try:
                        enriched_prompt = await self._enrich_message_with_vision(prompt, image_paths)
                    except Exception as e:
                        logger.warning('Background task vision enrichment failed: %s', e)

            def run_sync():
                agent = AIAgent(model=turn_route['model'], **turn_route['runtime'], **_checkpoint_agent_kwargs(user_config), max_iterations=max_iterations, quiet_mode=True, verbose_logging=False, enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets, reasoning_config=reasoning_config, service_tier=self._service_tier, request_overrides=turn_route.get('request_overrides'), providers_allowed=pr.get('only'), providers_ignored=pr.get('ignore'), providers_order=pr.get('order'), provider_sort=pr.get('sort'), provider_require_parameters=pr.get('require_parameters', False), provider_data_collection=pr.get('data_collection'), session_id=task_id, platform=platform_key, user_id=source.user_id, user_id_alt=source.user_id_alt, user_name=source.user_name, chat_id=source.chat_id, chat_name=source.chat_name, chat_type=source.chat_type, thread_id=source.thread_id, session_db=getattr(self._session_db, '_db', self._session_db), fallback_model=self._refresh_fallback_model())
                try:
                    return agent.run_conversation(user_message=enriched_prompt, task_id=task_id)
                finally:
                    self._cleanup_agent_resources(agent)
            result = await self._run_in_executor_with_context(run_sync)
            response = result.get('final_response', '') if result else ''
            if not response and result and result.get('error'):
                response = f"Error: {result['error']}"
            if response:
                media_files, response = adapter.extract_media(response)
                from gateway.platforms.base import BasePlatformAdapter
                media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
                images, text_content = adapter.extract_images(response)
                preview = prompt[:60] + ('...' if len(prompt) > 60 else '')
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'
                if text_content:
                    await adapter.send(chat_id=source.chat_id, content=header + text_content, metadata=_thread_metadata)
                elif not images and (not media_files):
                    await adapter.send(chat_id=source.chat_id, content=header + '(No response generated)', metadata=_thread_metadata)
                for image_url, alt_text in images or []:
                    try:
                        await adapter.send_image(chat_id=source.chat_id, image_url=image_url, caption=alt_text, metadata=_thread_metadata)
                    except Exception:
                        pass
                from gateway.platforms.base import should_send_media_as_audio as _should_send_media_as_audio
                _IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
                _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
                for media_path, _is_voice in media_files or []:
                    _ext = os.path.splitext(media_path)[1].lower()
                    try:
                        if _should_send_media_as_audio(source.platform, _ext, _is_voice):
                            await adapter.send_voice(chat_id=source.chat_id, audio_path=media_path, metadata=_thread_metadata)
                        elif _ext in _VIDEO_EXTS:
                            await adapter.send_video(chat_id=source.chat_id, video_path=media_path, metadata=_thread_metadata)
                        elif _ext in _IMAGE_EXTS:
                            await adapter.send_image_file(chat_id=source.chat_id, image_path=media_path, metadata=_thread_metadata)
                        else:
                            await adapter.send_document(chat_id=source.chat_id, file_path=media_path, metadata=_thread_metadata)
                    except Exception:
                        pass
            else:
                preview = prompt[:60] + ('...' if len(prompt) > 60 else '')
                await adapter.send(chat_id=source.chat_id, content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)', metadata=_thread_metadata)
        except Exception as e:
            logger.exception('Background task %s failed', task_id)
            try:
                await adapter.send(chat_id=source.chat_id, content=f'❌ Background task {task_id} failed: {e}', metadata=_thread_metadata)
            except Exception:
                pass

    async def _get_telegram_topic_capabilities(self, source: SessionSource) -> dict:
        """Read Telegram private-topic capability flags via Bot API getMe."""
        adapter = self._adapter_for_source(source)
        bot = getattr(adapter, '_bot', None)
        if bot is None or not hasattr(bot, 'get_me'):
            return {'checked': False}
        try:
            me = await bot.get_me()
        except Exception:
            logger.debug('Failed to fetch Telegram getMe topic capabilities', exc_info=True)
            return {'checked': False}

        def _field(name: str):
            if hasattr(me, name):
                return getattr(me, name)
            api_kwargs = getattr(me, 'api_kwargs', None)
            if isinstance(api_kwargs, dict) and name in api_kwargs:
                return api_kwargs.get(name)
            if isinstance(me, dict):
                return me.get(name)
            return None
        return {'checked': True, 'has_topics_enabled': _field('has_topics_enabled'), 'allows_users_to_create_topics': _field('allows_users_to_create_topics')}

    async def _ensure_telegram_system_topic(self, source: SessionSource) -> None:
        """Create/pin the managed System topic after /topic activation when possible."""
        adapter = self._adapter_for_source(source)
        if adapter is None or not source.chat_id:
            return
        thread_id = None
        create_topic = getattr(adapter, '_create_dm_topic', None)
        if callable(create_topic):
            try:
                thread_id = await create_topic(int(source.chat_id), 'System')
            except Exception:
                logger.debug('Failed to create Telegram System topic', exc_info=True)
        if not thread_id:
            return
        message_id = None
        try:
            send_result = await adapter.send(source.chat_id, 'System topic for Duck Agent commands and status.', metadata={'thread_id': str(thread_id)})
            message_id = getattr(send_result, 'message_id', None)
        except Exception:
            logger.debug('Failed to send Telegram System topic intro', exc_info=True)
        if not message_id:
            return
        bot = getattr(adapter, '_bot', None)
        if bot is None or not hasattr(bot, 'pin_chat_message'):
            return
        try:
            await bot.pin_chat_message(chat_id=int(source.chat_id), message_id=int(message_id), disable_notification=True)
        except Exception:
            logger.debug('Failed to pin Telegram System topic intro', exc_info=True)

    async def _send_telegram_topic_setup_image(self, source: SessionSource) -> None:
        """Send the bundled BotFather Threads Settings screenshot when available."""
        adapter = self._adapter_for_source(source)
        if adapter is None or not source.chat_id or (not hasattr(adapter, 'send_image_file')):
            return
        image_path = Path(__file__).resolve().parent / 'assets' / 'telegram-botfather-threads-settings.jpg'
        if not image_path.exists():
            return
        try:
            await adapter.send_image_file(chat_id=source.chat_id, image_path=str(image_path), caption='BotFather → Bot Settings → Threads Settings', metadata={'thread_id': str(source.thread_id)} if source.thread_id else None)
        except Exception:
            logger.debug('Failed to send Telegram topic setup image', exc_info=True)

    def _sanitize_telegram_topic_title(self, title: str) -> str:
        """Return a Bot API-safe forum topic name from a generated session title."""
        cleaned = re.sub('\\s+', ' ', str(title or '')).strip()
        if not cleaned:
            return 'Duck Agent Chat'
        if len(cleaned) > 120:
            cleaned = cleaned[:117].rstrip() + '...'
        return cleaned

    def _is_discord_auto_thread_lane(self, source: SessionSource) -> bool:
        """Return True only for Discord threads Duck Agent just auto-created."""
        return source.platform == Platform.DISCORD and source.chat_type == 'thread' and bool(getattr(source, 'auto_thread_created', False)) and bool(source.thread_id) and bool(getattr(source, 'auto_thread_initial_name', None))

    def _is_relay_discord_channel_lane(self, source: SessionSource) -> bool:
        """Shape-only check: a relay-delivered Discord CHANNEL event whose
        reply the connector MAY auto-thread (title-turn registration gate).

        Deliberately does NOT consult the send-result cache: at registration
        time (before delivery) the feedback can't exist yet. The rename lane
        polls the cache at fire time instead."""
        return source.platform == Platform.DISCORD and bool(source.chat_id) and (not source.thread_id) and (source.chat_type in ('group', 'channel')) and (getattr(source, 'delivered_via_upstream_relay', False) is True)

    def _relay_auto_thread_info(self, source: SessionSource) -> Optional[Tuple[str, str]]:
        """(thread_id, initial_name) when the RELAY connector auto-threaded our
        reply to this source's chat — the title-turn sibling of
        _is_discord_auto_thread_lane.

        The marker-based check above only lights up for events ARRIVING IN an
        auto-created thread (turn 2+). The auto-title fires on the FIRST
        exchange, whose source is the PARENT channel event — the thread did
        not exist at ingest, so no markers can be present and the native lane
        check never matches on the relay title turn (staging repro
        2026-07-29: initial titles fine, semantic renames never happened).

        Preferred path: the connector stamps ``prospective_thread_id`` on the
        inbound (the anchor message id, which IS the id of the thread it will
        auto-create). It's deterministic and per-message, so it identifies the
        EXACT thread even when several auto-threads spawn from one channel —
        unlike the send-result cache below, which held a single slot per parent
        chat and so only the FIRST thread in a channel ever renamed (staging
        repro 2026-08-02: thread A renamed, sibling thread B stuck at raw
        text). The connector's own created-name guard (prefer_connector_created)
        enforces no-clobber, so no initial name is needed here.

        Fallback: the connector reports where the reply actually landed on the
        send result (contract §SendResult thread_id/auto_thread_name); the
        relay adapter caches it per chat and this reads it back — kept for
        older connectors that don't stamp prospective_thread_id.
        """
        if source.platform != Platform.DISCORD or not source.chat_id:
            return None
        if not getattr(source, 'delivered_via_upstream_relay', False):
            return None
        prospective = getattr(source, 'prospective_thread_id', None)
        if prospective:
            return (str(prospective), '')
        adapter = self._adapter_for_source(source)
        info_fn = getattr(adapter, 'auto_thread_info_for_chat', None)
        if not callable(info_fn):
            return None
        try:
            info = info_fn(str(source.chat_id))
            if isinstance(info, tuple) and len(info) == 2 and all((isinstance(x, str) for x in info)):
                return cast(Tuple[str, str], info)
            return None
        except Exception:
            return None

    def _sanitize_discord_thread_title(self, title: str) -> str:
        """Return a Discord-safe semantic thread title from a session title.

        Discord thread names are capped at 100 characters measured in UTF-16
        code units (emoji count double), so truncate with the UTF-16 helpers
        rather than Python code-point slices.
        """
        cleaned = re.sub('\\s+', ' ', str(title or '')).strip()
        if not cleaned:
            return 'Duck Agent Chat'
        if utf16_len(cleaned) > 80:
            cleaned = _prefix_within_utf16_limit(cleaned, 77).rstrip() + '...'
        return cleaned

    async def _rename_discord_auto_thread_for_session_title(self, source: SessionSource, session_id: str, title: str, relay_info: Optional[Tuple[str, str]]=None) -> None:
        """Best-effort semantic rename of a newly auto-created Discord thread.

        ``relay_info`` is the (thread_id, initial_name) pair from the relay
        connector's send-result feedback — supplied on the title turn, where
        the source is the parent-channel event and carries no auto-thread
        markers (see _relay_auto_thread_info). When absent, the native
        marker-based lane supplies thread identity from the source itself.
        """
        if relay_info is None and (not await asyncio.to_thread(self._is_discord_auto_thread_lane, source)):
            if not self._is_relay_discord_channel_lane(source):
                return
            for _ in range(20):
                relay_info = self._relay_auto_thread_info(source)
                if relay_info is not None:
                    break
                await asyncio.sleep(0.5)
            if relay_info is None:
                return
        adapter = self._adapter_for_source(source) if getattr(self, 'adapters', None) else None
        if adapter is None:
            return
        rename_thread = getattr(adapter, 'rename_thread', None)
        if rename_thread is None:
            return
        target_thread_id = relay_info[0] if relay_info else str(source.thread_id)
        use_connector_guard = relay_info is not None
        guard_name = None if use_connector_guard else getattr(source, 'auto_thread_initial_name', None)
        thread_name = self._sanitize_discord_thread_title(title)
        parent_chat_id = str(source.chat_id) if use_connector_guard and source.chat_id else None
        logger.info('discord auto-thread rename: thread=%s lane=%s new_title=%r', target_thread_id, 'relay' if use_connector_guard else 'native', thread_name)
        try:
            renamed = await rename_thread(target_thread_id, thread_name, prefer_connector_created=use_connector_guard, only_if_current_name=guard_name, parent_chat_id=parent_chat_id)
            logger.info('discord auto-thread rename result: thread=%s applied=%s', target_thread_id, bool(renamed))
        except Exception:
            logger.debug('Failed to rename Discord auto-thread for generated session title', exc_info=True)

    def _schedule_discord_semantic_thread_rename(self, source: SessionSource, session_id: str, title: str) -> None:
        """Schedule Discord auto-thread rename from the auto-title background thread."""
        relay_info = None
        if not title:
            return
        if not self._is_discord_auto_thread_lane(source):
            relay_info = self._relay_auto_thread_info(source)
            if relay_info is None and (not self._is_relay_discord_channel_lane(source)):
                return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, '_gateway_loop', None)
        if loop is None or loop.is_closed():
            return
        try:
            copied_source = dataclasses.replace(source)
        except Exception:
            copied_source = source
        future = safe_schedule_threadsafe(self._rename_discord_auto_thread_for_session_title(copied_source, session_id, title, relay_info=relay_info), loop, logger=logger, log_message='Discord semantic thread rename failed to schedule')
        if future is None:
            return

        def _log_rename_failure(fut) -> None:
            try:
                fut.result()
            except Exception:
                logger.debug('Discord semantic thread rename failed', exc_info=True)
        future.add_done_callback(_log_rename_failure)

    async def _rename_telegram_topic_for_session_title(self, source: SessionSource, session_id: str, title: str) -> None:
        """Best-effort rename of a Telegram DM topic when Duck Agent auto-titles a session."""
        if not await asyncio.to_thread(self._is_telegram_topic_lane, source) or not source.chat_id or (not source.thread_id):
            return
        if self._telegram_topic_auto_rename_disabled(source):
            return
        adapter = self._adapter_for_source(source)
        if adapter is not None:
            get_info = getattr(type(adapter), '_get_dm_topic_info', None)
            if callable(get_info):
                try:
                    operator_topic = get_info(adapter, str(source.chat_id), str(source.thread_id))
                except Exception:
                    operator_topic = None
                if isinstance(operator_topic, dict):
                    return
        session_db = getattr(self, '_session_db', None)
        if session_db is not None:
            try:
                binding = await session_db.get_telegram_topic_binding(chat_id=str(source.chat_id), thread_id=str(source.thread_id))
                if binding and str(binding.get('session_id') or '') != str(session_id):
                    return
            except Exception:
                logger.debug('Failed to verify Telegram topic binding before rename', exc_info=True)
                return
        if adapter is None:
            return
        topic_name = self._sanitize_telegram_topic_title(title)
        try:
            rename_topic = getattr(adapter, 'rename_dm_topic', None)
            if rename_topic is not None:
                await rename_topic(chat_id=str(source.chat_id), thread_id=str(source.thread_id), name=topic_name)
                return
            bot = getattr(adapter, '_bot', None)
            edit_forum_topic = getattr(bot, 'edit_forum_topic', None) if bot is not None else None
            if edit_forum_topic is None:
                edit_forum_topic = getattr(bot, 'editForumTopic', None) if bot is not None else None
            if edit_forum_topic is None:
                return
            try:
                await edit_forum_topic(chat_id=int(source.chat_id), message_thread_id=int(source.thread_id), name=topic_name)
            except (TypeError, ValueError):
                await edit_forum_topic(chat_id=source.chat_id, message_thread_id=source.thread_id, name=topic_name)
        except Exception:
            logger.debug('Failed to rename Telegram topic for auto-generated title', exc_info=True)

    def _telegram_topic_auto_rename_disabled(self, source: SessionSource) -> bool:
        """Return True when operator disabled per-topic auto-rename for this Telegram chat.

        Controlled via ``gateway.platforms.telegram.extra.disable_topic_auto_rename``.
        Default is False (auto-rename enabled, preserves prior behaviour).
        """
        platform_cfg = self.config.platforms.get(source.platform) if getattr(self, 'config', None) and getattr(self.config, 'platforms', None) else None
        if platform_cfg is None:
            return False
        extra = getattr(platform_cfg, 'extra', None) or {}
        value = extra.get('disable_topic_auto_rename')
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return bool(value)

    def _schedule_telegram_topic_title_rename(self, source: SessionSource, session_id: str, title: str) -> None:
        """Schedule a topic rename from the auto-title background thread."""
        if not title or not self._is_telegram_topic_lane(source):
            return
        if self._telegram_topic_auto_rename_disabled(source):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, '_gateway_loop', None)
        if loop is None or loop.is_closed():
            return
        try:
            copied_source = dataclasses.replace(source)
        except Exception:
            copied_source = source
        future = safe_schedule_threadsafe(self._rename_telegram_topic_for_session_title(copied_source, session_id, title), loop, logger=logger, log_message='Telegram topic title rename failed to schedule')
        if future is None:
            return

        def _log_rename_failure(fut) -> None:
            try:
                fut.result()
            except Exception:
                logger.debug('Telegram topic title rename failed', exc_info=True)
        future.add_done_callback(_log_rename_failure)
    _TELEGRAM_CAPABILITY_HINT_COOLDOWN_S = 300.0

    def _should_send_telegram_capability_hint(self, source: SessionSource) -> bool:
        """Rate-limit the BotFather Threads Settings screenshot.

        If a user sends /topic repeatedly while Threads Settings are still
        off, we shouldn't keep re-uploading the screenshot every time.
        """
        if not hasattr(self, '_telegram_capability_hint_ts'):
            self._telegram_capability_hint_ts = {}
        chat_id = str(source.chat_id or '')
        if not chat_id:
            return True
        import time as _time
        now = _time.monotonic()
        last = self._telegram_capability_hint_ts.get(chat_id, 0.0)
        if now - last < self._TELEGRAM_CAPABILITY_HINT_COOLDOWN_S:
            return False
        self._telegram_capability_hint_ts[chat_id] = now
        return True

    def _telegram_topic_help_text(self) -> str:
        return "/topic — enable multi-session DM mode (one bot, many parallel chats)\n\nUsage:\n  /topic             Enable topic mode, or show status if already on\n  /topic help        Show this message\n  /topic off         Disable topic mode and clear topic bindings\n  /topic <id>        Inside a topic: restore a previous session by ID\n\nHow it works:\n1. Run /topic once in this DM — Duck Agent checks BotFather Threads\n   Settings are enabled and flips on multi-session mode.\n2. Tap All Messages at the top of the bot and send any message.\n   Telegram creates a new topic for that message; each topic is\n   an independent Duck Agent session (fresh history, fresh context).\n3. The root DM becomes a system lobby — send /topic, /status,\n   /help, /usage there. Normal prompts go in a topic.\n4. /new inside a topic resets just that topic's session.\n5. /topic <id> inside a topic restores an old session into it."

    async def _disable_telegram_topic_mode_for_chat(self, source: SessionSource) -> str:
        """Cleanly disable topic mode for a chat via /topic off."""
        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t('gateway.shared.session_db_unavailable_prefix'))
        chat_id = str(source.chat_id or '')
        if not chat_id:
            return 'Could not determine chat ID.'
        try:
            currently_enabled = await self._session_db.is_telegram_topic_mode_enabled(chat_id=chat_id, user_id=str(source.user_id or ''))
        except Exception:
            currently_enabled = False
        if not currently_enabled:
            return 'Multi-session topic mode is not currently enabled for this chat.'
        try:
            await self._session_db.disable_telegram_topic_mode(chat_id=chat_id)
        except Exception as exc:
            logger.exception('Failed to disable Telegram topic mode')
            return f'Failed to disable topic mode: {exc}'
        for attr in ('_telegram_lobby_reminder_ts', '_telegram_capability_hint_ts'):
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(chat_id, None)
        return "Multi-session topic mode is now OFF for this chat.\n\nExisting topics in Telegram aren't removed — they'll just stop being gated as independent sessions. The root DM works as a normal Duck Agent chat again. Run /topic to re-enable later."

    async def _telegram_topic_root_status_message(self, source: SessionSource) -> str:
        lines = ['Telegram multi-session topics are enabled.', '', 'To create a new Duck Agent chat, open All Messages at the top of this bot interface and send any message there. Telegram will create a new topic for it.', '']
        try:
            sessions = await self._session_db.list_unlinked_telegram_sessions_for_user(chat_id=str(source.chat_id), user_id=str(source.user_id), limit=10)
        except Exception:
            logger.debug('Failed to list unlinked Telegram sessions', exc_info=True)
            sessions = []
        if sessions:
            lines.append('Previous unlinked sessions:')
            for session in sessions:
                session_id = str(session.get('id') or '')
                title = str(session.get('title') or 'Untitled session')
                preview = str(session.get('preview') or '').strip()
                line = f'- {title} — `{session_id}`'
                if preview:
                    line += f' — {preview}'
                lines.append(line)
            lines.extend(['', 'To restore one:', '1. Create or open a topic. To create a new one, open All Messages and send any message there.', '2. Send /topic <session-id> inside that topic.', f"Example: Send /topic {sessions[0].get('id')} inside a topic."])
        else:
            lines.extend(['No previous unlinked Telegram sessions found.', '', 'To restore a previous session later:', '1. Create or open a topic. To create a new one, open All Messages and send any message there.', '2. Send /topic <session-id> inside that topic.'])
        return '\n'.join(lines)

    async def _restore_telegram_topic_session(self, event: MessageEvent, raw_session_id: str) -> str:
        """Restore an existing Telegram-owned Duck Agent session into this topic."""
        source = event.source
        session_id = await self._session_db.resolve_session_id(raw_session_id.strip())
        if not session_id:
            return f'Session not found: {raw_session_id.strip()}'
        session = await self._session_db.get_session(session_id)
        if not session:
            return f'Session not found: {raw_session_id.strip()}'
        if str(session.get('source') or '') != 'telegram':
            return 'That session is not a Telegram session and cannot be restored into this topic.'
        if str(session.get('user_id') or '') != str(source.user_id):
            return 'That session does not belong to this Telegram user.'
        linked = await self._session_db.is_telegram_session_linked_to_topic(session_id=session_id)
        current_binding = await self._session_db.get_telegram_topic_binding(chat_id=str(source.chat_id), thread_id=str(source.thread_id))
        if linked:
            if not current_binding or current_binding.get('session_id') != session_id:
                return 'That session is already linked to another Telegram topic.'
        session_key = self._session_key_for_source(source)
        try:
            await self._session_db.bind_telegram_topic(chat_id=str(source.chat_id), thread_id=str(source.thread_id), user_id=str(source.user_id), session_key=session_key, session_id=session_id, managed_mode='restored')
        except ValueError as exc:
            if 'already linked' in str(exc):
                return 'That session is already linked to another Telegram topic.'
            raise
        title = await self._session_db.get_session_title(session_id) or session_id
        last_assistant = None
        try:
            for message in reversed(await self._session_db.get_messages(session_id)):
                if message.get('role') == 'assistant' and message.get('content'):
                    last_assistant = str(message.get('content'))
                    break
        except Exception:
            last_assistant = None
        response = f'Session restored: {title}'
        if last_assistant:
            response += f'\n\nLast Duck Agent message:\n{last_assistant}'
        return response

    async def _execute_mcp_reload(self, event: MessageEvent) -> str:
        """Actually disconnect, reconnect, and notify MCP tool changes.

        Split out from ``_handle_reload_mcp_command`` so the confirmation
        wrapper can invoke the same path whether the user confirmed via
        button, text reply, or has the confirm gate disabled.
        """
        loop = asyncio.get_running_loop()
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock
            with _lock:
                old_servers = set(_servers.keys())
            await loop.run_in_executor(None, shutdown_mcp_servers)
            new_tools = await loop.run_in_executor(None, discover_mcp_tools)
            with _lock:
                connected_servers = set(_servers.keys())
            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers
            lines = [t('gateway.reload_mcp.header')]
            if reconnected:
                lines.append(t('gateway.reload_mcp.reconnected', names=', '.join(sorted(reconnected))))
            if added:
                lines.append(t('gateway.reload_mcp.added', names=', '.join(sorted(added))))
            if removed:
                lines.append(t('gateway.reload_mcp.removed', names=', '.join(sorted(removed))))
            if not connected_servers:
                lines.append(t('gateway.reload_mcp.none_connected'))
            else:
                lines.append(t('gateway.reload_mcp.tools_available', tools=len(new_tools), servers=len(connected_servers)))
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools
                _cache = getattr(self, '_agent_cache', None)
                _cache_lock = getattr(self, '_agent_cache_lock', None)
                if _cache_lock is not None and _cache:
                    with _cache_lock:
                        for _sess_key, _entry in list(_cache.items()):
                            try:
                                _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                            except Exception:
                                continue
                            if _agent is None:
                                continue
                            refresh_agent_mcp_tools(_agent, quiet_mode=True)
            except Exception as _exc:
                logger.debug('Failed to update cached agent tools after MCP reload: %s', _exc)
            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f'{len(new_tools)} MCP tool(s) now available' if new_tools else 'No MCP tools available'
            change_detail = '. '.join(change_parts) + '. ' if change_parts else ''
            reload_msg = {'role': 'user', 'content': f'[IMPORTANT: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]'}
            try:
                session_entry = await self.async_session_store.get_or_create_session(event.source)
                await self.async_session_store.append_to_transcript(session_entry.session_id, reload_msg)
            except Exception:
                pass
            return '\n'.join(lines)
        except Exception as e:
            logger.warning('MCP reload failed: %s', e)
            return t('gateway.reload_mcp.failed', error=e)

    async def _maybe_confirm_destructive_slash(self, *, event: MessageEvent, command: str, title: str, detail: str, execute) -> Union[str, 'EphemeralReply', None]:
        """Gate a destructive session slash command (/new, /reset, /undo).

        ``execute`` is an async callable ``execute() -> str | EphemeralReply``
        that performs the destructive action.  If the
        ``approvals.destructive_slash_confirm`` config gate is off, ``execute``
        runs immediately (returning its result).  Otherwise this routes
        through ``_request_slash_confirm`` — native yes/no buttons on
        Telegram/Discord/Slack, text fallback elsewhere.

        Three-option resolution:

          - ``once``  — run ``execute`` and return its result
          - ``always`` — persist ``approvals.destructive_slash_confirm: false``,
                        then run ``execute``
          - ``cancel`` — return a "cancelled" message; do not run ``execute``
        """
        confirm_required = True
        try:
            cfg = self._read_user_config()
            approvals = cfg.get('approvals') if isinstance(cfg, dict) else None
            if isinstance(approvals, dict):
                confirm_required = bool(approvals.get('destructive_slash_confirm', True))
        except Exception:
            pass
        if not confirm_required:
            return await execute()
        session_key = self._session_key_for_source(event.source)

        async def _on_confirm(choice: str):
            if choice == 'cancel':
                return f'🟡 /{command} cancelled. Conversation unchanged.'
            persisted = False
            if choice == 'always':
                try:
                    from cli import save_config_value
                    persisted = bool(save_config_value('approvals.destructive_slash_confirm', False))
                    if persisted:
                        logger.info('User opted out of destructive slash confirm (session=%s)', session_key)
                    else:
                        logger.warning('Could not persist destructive_slash_confirm=false (session=%s); config.yaml is not writable', session_key)
                except Exception as exc:
                    logger.warning('Failed to persist destructive_slash_confirm=false: %s', exc)
            result = await execute()
            if choice == 'always':
                if persisted:
                    note = '\n\nℹ️ Future /clear, /new, /reset, and /undo will run without confirmation. Re-enable via `approvals.destructive_slash_confirm: true` in config.yaml.'
                else:
                    note = '\n\n⚠️ Could not save that preference (config.yaml is not writable), so /clear, /new, /reset, and /undo will ask again next time. To silence it permanently, set `approvals.destructive_slash_confirm: false` in config.yaml.'
                if isinstance(result, str):
                    return result + note
                return result
            return result
        _p = self._typed_command_prefix_for(event.source.platform)
        prompt_message = f'⚠️ **Confirm /{command}**\n\n{detail}\n\nChoose:\n• **Approve Once** — proceed this time only\n• **Always Approve** — proceed and silence this prompt permanently\n• **Cancel** — keep current conversation\n\n_Text fallback: reply `{_p}approve`, `{_p}always`, or `{_p}cancel`._'
        return await self._request_slash_confirm(event=event, command=command, title=title, message=prompt_message, handler=_on_confirm)

    async def _request_slash_confirm(self, *, event: MessageEvent, command: str, title: str, message: str, handler) -> Optional[str]:
        """Ask the user to confirm an expensive slash command.

        ``handler`` is an async callable ``handler(choice: str) -> str``
        where ``choice`` is ``"once"``, ``"always"``, or ``"cancel"``.
        The handler runs on the event loop when the user responds; its
        return value is sent back as a gateway message.

        Returns a short acknowledgment string to send immediately (before
        the user's response).  If buttons rendered successfully the ack
        is ``None`` (buttons are self-explanatory); if we fell back to
        text the message itself IS the ack.
        """
        from tools import slash_confirm as _slash_confirm_mod
        source = event.source
        session_key = self._session_key_for_source(source)
        counter = getattr(self, '_slash_confirm_counter', None)
        if counter is None:
            import itertools as _itertools
            counter = _itertools.count(1)
            self._slash_confirm_counter = counter
        confirm_id = f'{next(counter)}'
        _slash_confirm_mod.register(session_key, confirm_id, command, handler)
        adapter = self._adapter_for_source(source)
        metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
        used_buttons = False
        if adapter is not None:
            try:
                button_result = await adapter.send_slash_confirm(chat_id=source.chat_id, title=title, message=message, session_key=session_key, confirm_id=confirm_id, metadata=metadata)
                if button_result and getattr(button_result, 'success', False):
                    used_buttons = True
            except Exception as exc:
                logger.debug('send_slash_confirm failed for %s on %s: %s', command, source.platform, exc)
        if used_buttons:
            return None
        return message

    def _read_user_config(self) -> Dict[str, Any]:
        """Read the user's raw config.yaml (cached) for gate lookups.

        Used by slash-confirm gates that must reflect on-disk state changes
        (e.g. a prior "Always Approve" click) without a gateway restart.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _thread_metadata_for_source(self, source, reply_to_message_id: Optional[str]=None) -> Optional[Dict[str, Any]]:
        """Build the metadata dict platforms need for thread-aware replies."""
        metadata = self._thread_metadata_for_target(getattr(source, 'platform', None), getattr(source, 'chat_id', None), getattr(source, 'thread_id', None), chat_type=getattr(source, 'chat_type', None), reply_to_message_id=reply_to_message_id or getattr(source, 'message_id', None))
        if getattr(source, 'platform', None) == Platform.SLACK:
            team_id = getattr(source, 'scope_id', None)
            if team_id:
                metadata = dict(metadata or {})
                metadata['slack_team_id'] = str(team_id)
        return metadata

    def _thread_metadata_for_target(self, platform: Optional[Platform], chat_id: Optional[str], thread_id: Optional[str], *, chat_type: Optional[str]=None, reply_to_message_id: Optional[str]=None, adapter: Optional[Any]=None) -> Optional[Dict[str, Any]]:
        """Build thread metadata for synthetic sends that only have routing state."""
        if thread_id is None:
            return None
        metadata: Dict[str, Any] = {'thread_id': thread_id}
        if self._is_telegram_dm_topic_target(platform, chat_id, thread_id, chat_type=chat_type, adapter=adapter):
            metadata['telegram_dm_topic_reply_fallback'] = True
            tid = str(thread_id)
            if tid and tid not in {'', '1'}:
                metadata['direct_messages_topic_id'] = tid
            if reply_to_message_id is not None:
                metadata['telegram_reply_to_message_id'] = str(reply_to_message_id)
        if platform == Platform.SLACK and reply_to_message_id is not None:
            metadata['message_id'] = str(reply_to_message_id)
        return metadata

    @staticmethod
    def _is_telegram_dm_topic_target(platform: Optional[Platform], chat_id: Optional[str], thread_id: Optional[str], *, chat_type: Optional[str]=None, adapter: Optional[Any]=None) -> bool:
        """Return True when a target is a Telegram private DM topic lane."""
        if platform != Platform.TELEGRAM or thread_id is None:
            return False
        if chat_type == 'dm':
            return True
        if adapter is not None and chat_id:
            get_dm_topic_info = getattr(type(adapter), '_get_dm_topic_info', None)
            if callable(get_dm_topic_info):
                try:
                    topic_info = get_dm_topic_info(adapter, str(chat_id), str(thread_id))
                except Exception:
                    logger.debug('Failed to inspect Telegram DM topic metadata', exc_info=True)
                else:
                    return isinstance(topic_info, dict)
        return False

    @staticmethod
    def _reply_anchor_for_event(event: MessageEvent) -> Optional[str]:
        """Return the platform-specific reply anchor for GatewayRunner sends."""
        return _reply_anchor_for_event(event)
    _APPROVAL_TIMEOUT_SECONDS = 300
    _UPDATE_ALLOWED_PLATFORMS = frozenset({Platform.TELEGRAM, Platform.SLACK, Platform.WHATSAPP, Platform.SIGNAL, Platform.MATRIX, Platform.EMAIL, Platform.SMS, Platform.DINGTALK, Platform.FEISHU, Platform.WECOM, Platform.WECOM_CALLBACK, Platform.WEIXIN, Platform.BLUEBUBBLES, Platform.QQBOT, Platform.LOCAL})

    def _schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, '_update_notification_task', None)
        if existing_task and (not existing_task.done()):
            return
        try:
            self._update_notification_task = asyncio.create_task(self._watch_update_progress())
        except RuntimeError:
            logger.debug('Skipping update notification watcher: no running event loop')

    async def _watch_update_progress(self, poll_interval: float=2.0, stream_interval: float=4.0, timeout: float=1800.0) -> None:
        """Watch ``duck-agent update --gateway``, streaming output + forwarding prompts.

        Polls ``.update_output.txt`` for new content and sends chunks to the
        user periodically.  Detects ``.update_prompt.json`` (written by the
        update process when it needs user input) and forwards the prompt to
        the messenger.  The user's next message is intercepted by
        ``_handle_message`` and written to ``.update_response``.
        """
        pending_path = _hermes_home / '.update_pending.json'
        claimed_path = _hermes_home / '.update_pending.claimed.json'
        output_path = _hermes_home / '.update_output.txt'
        exit_code_path = _hermes_home / '.update_exit_code'
        prompt_path = _hermes_home / '.update_prompt.json'
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        adapter = None
        chat_id = None
        session_key = None
        metadata = None
        for path in (claimed_path, pending_path):
            if path.exists():
                try:
                    pending = json.loads(path.read_text(encoding='utf-8'))
                    platform_str = pending.get('platform')
                    chat_id = pending.get('chat_id')
                    chat_type = pending.get('chat_type')
                    session_key = pending.get('session_key')
                    thread_id = pending.get('thread_id')
                    message_id = pending.get('message_id')
                    if platform_str and chat_id:
                        platform = Platform(platform_str)
                        adapter = self.adapters.get(platform)
                        metadata = self._thread_metadata_for_target(platform, chat_id, thread_id, chat_type=chat_type, reply_to_message_id=message_id, adapter=adapter)
                        if not session_key:
                            session_key = f'{platform_str}:{chat_id}'
                    break
                except Exception:
                    pass
        if not adapter or not chat_id:
            logger.warning('Update watcher: cannot resolve adapter/chat_id, falling back to completion-only')
            while (pending_path.exists() or claimed_path.exists()) and loop.time() < deadline:
                if exit_code_path.exists() and await self._send_update_notification():
                    return
                await asyncio.sleep(poll_interval)
            if (pending_path.exists() or claimed_path.exists()) and (not exit_code_path.exists()):
                exit_code_path.write_text('124', encoding='utf-8')
                await self._send_update_notification()
            return

        def _strip_ansi(text: str) -> str:
            from tools.ansi_strip import strip_ansi
            return strip_ansi(text)
        bytes_sent = 0
        last_stream_time = loop.time()
        buffer = ''

        async def _flush_buffer() -> None:
            """Send buffered output to the user."""
            nonlocal buffer, last_stream_time
            if not buffer.strip():
                buffer = ''
                return
            clean = _strip_ansi(buffer).strip()
            buffer = ''
            last_stream_time = loop.time()
            if not clean:
                return
            max_chunk = 3500
            chunks = [clean[i:i + max_chunk] for i in range(0, len(clean), max_chunk)]
            for chunk in chunks:
                try:
                    await adapter.send(chat_id, f'```\n{chunk}\n```', metadata=_non_conversational_metadata(metadata, platform=platform))
                except Exception as e:
                    logger.debug('Update stream send failed: %s', e)
        while loop.time() < deadline:
            if exit_code_path.exists():
                if output_path.exists():
                    try:
                        content = output_path.read_text(encoding='utf-8')
                        if len(content) > bytes_sent:
                            buffer += content[bytes_sent:]
                            bytes_sent = len(content)
                    except OSError:
                        pass
                await _flush_buffer()
                try:
                    exit_code_raw = exit_code_path.read_text(encoding='utf-8').strip() or '1'
                    exit_code = int(exit_code_raw)
                    if exit_code == 0:
                        await adapter.send(chat_id, '✅ Duck Agent update finished.', metadata=_non_conversational_metadata(metadata, platform=platform))
                    else:
                        await adapter.send(chat_id, '❌ Duck Agent update failed (exit code {}).'.format(exit_code), metadata=_non_conversational_metadata(metadata, platform=platform))
                    logger.info('Update finished (exit=%s), notified %s', exit_code, session_key)
                except Exception as e:
                    logger.warning('Update final notification failed: %s', e)
                for p in (pending_path, claimed_path, output_path, exit_code_path, prompt_path):
                    p.unlink(missing_ok=True)
                (_hermes_home / '.update_response').unlink(missing_ok=True)
                _up_done = self._peek_session_state(session_key)
                if _up_done is not None:
                    _up_done.persistent.update_prompt_pending = False
                return
            if output_path.exists():
                try:
                    content = output_path.read_text(encoding='utf-8')
                    if len(content) > bytes_sent:
                        buffer += content[bytes_sent:]
                        bytes_sent = len(content)
                except OSError:
                    pass
            if buffer.strip() and loop.time() - last_stream_time >= stream_interval:
                await _flush_buffer()
            _up_pending_state = self._peek_session_state(session_key) if session_key else None
            if prompt_path.exists() and session_key and (not (_up_pending_state is not None and _up_pending_state.persistent.update_prompt_pending)):
                try:
                    prompt_data = json.loads(prompt_path.read_text(encoding='utf-8'))
                    prompt_text = prompt_data.get('prompt', '')
                    default = prompt_data.get('default', '')
                    if prompt_text:
                        await _flush_buffer()
                        sent_buttons = False
                        if getattr(type(adapter), 'send_update_prompt', None) is not None:
                            try:
                                await adapter.send_update_prompt(chat_id=chat_id, prompt=prompt_text, default=default, session_key=session_key, metadata=_non_conversational_metadata(metadata, platform=platform))
                                sent_buttons = True
                            except Exception as btn_err:
                                logger.debug('Button-based update prompt failed: %s', btn_err)
                        if not sent_buttons:
                            default_hint = f' (default: {default})' if default else ''
                            _p = getattr(adapter, 'typed_command_prefix', '/')
                            await adapter.send(chat_id, f'⚕ **Update needs your input:**\n\n{prompt_text}{default_hint}\n\nReply `{_p}approve` (yes) or `{_p}deny` (no), or type your answer directly.', metadata=_non_conversational_metadata(metadata, platform=platform))
                        self._session_state(session_key).persistent.update_prompt_pending = True
                        logger.info('Forwarded update prompt to %s: %s', session_key, prompt_text[:80])
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug('Failed to read update prompt: %s', e)
            await asyncio.sleep(poll_interval)
        if not exit_code_path.exists():
            logger.warning('Update watcher timed out after %.0fs', timeout)
            exit_code_path.write_text('124', encoding='utf-8')
            await _flush_buffer()
            try:
                await adapter.send(chat_id, '❌ Duck Agent update timed out after 30 minutes.', metadata=_non_conversational_metadata(metadata, platform=platform))
            except Exception:
                pass
            for p in (pending_path, claimed_path, output_path, exit_code_path, prompt_path):
                p.unlink(missing_ok=True)
            (_hermes_home / '.update_response').unlink(missing_ok=True)
            _up_timeout_state = self._peek_session_state(session_key)
            if _up_timeout_state is not None:
                _up_timeout_state.persistent.update_prompt_pending = False

    async def _send_update_notification(self) -> bool:
        """If an update finished, notify the user.

        Returns False when the update is still running so a caller can retry
        later. Returns True after a definitive send/skip decision.

        This is the legacy notification path used when the streaming watcher
        cannot resolve the adapter (e.g. after a gateway restart where the
        platform hasn't reconnected yet).
        """
        pending_path = _hermes_home / '.update_pending.json'
        claimed_path = _hermes_home / '.update_pending.claimed.json'
        output_path = _hermes_home / '.update_output.txt'
        exit_code_path = _hermes_home / '.update_exit_code'
        if not pending_path.exists() and (not claimed_path.exists()):
            return False
        cleanup = True
        active_pending_path = claimed_path
        try:
            if pending_path.exists():
                try:
                    pending_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return True
            elif not claimed_path.exists():
                return True
            pending = json.loads(claimed_path.read_text(encoding='utf-8'))
            platform_str = pending.get('platform')
            chat_id = pending.get('chat_id')
            chat_type = pending.get('chat_type')
            thread_id = pending.get('thread_id')
            message_id = pending.get('message_id')
            if not exit_code_path.exists():
                logger.info('Update notification deferred: update still running')
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False
            exit_code_raw = exit_code_path.read_text(encoding='utf-8').strip() or '1'
            exit_code = int(exit_code_raw)
            output = ''
            if output_path.exists():
                output = output_path.read_text(encoding='utf-8')
            platform = Platform(platform_str)
            adapter = self.adapters.get(platform)
            if not adapter and chat_id:
                logger.info('Update notification deferred: %s adapter not connected yet', platform_str)
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False
            if adapter and chat_id:
                metadata = self._thread_metadata_for_target(platform, chat_id, thread_id, chat_type=chat_type, reply_to_message_id=message_id, adapter=adapter)
                from tools.ansi_strip import strip_ansi
                output = strip_ansi(output).strip()
                if output:
                    if len(output) > 3500:
                        output = '…' + output[-3500:]
                    if exit_code == 0:
                        msg = f'✅ Duck Agent update finished.\n\n```\n{output}\n```'
                    else:
                        msg = f'❌ Duck Agent update failed.\n\n```\n{output}\n```'
                elif exit_code == 0:
                    msg = '✅ Duck Agent update finished successfully.'
                else:
                    msg = '❌ Duck Agent update failed. Check the gateway logs or run `duck-agent update` manually for details.'
                await adapter.send(chat_id, msg, metadata=_non_conversational_metadata(metadata, platform=platform))
                logger.info('Sent post-update notification to %s:%s (exit=%s)', platform_str, chat_id, exit_code)
        except Exception as e:
            logger.warning('Post-update notification failed: %s', e)
        finally:
            if cleanup:
                active_pending_path.unlink(missing_ok=True)
                claimed_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                exit_code_path.unlink(missing_ok=True)
        return True

    async def _send_restart_notification(self) -> Optional[tuple[str, str, Optional[str]]]:
        """Notify the chat that initiated /restart that the gateway is back."""
        notify_path = _hermes_home / '.restart_notify.json'
        if not notify_path.exists():
            return None
        try:
            data = json.loads(notify_path.read_text(encoding='utf-8'))
            platform_str = data.get('platform')
            chat_id = data.get('chat_id')
            chat_type = data.get('chat_type')
            thread_id = data.get('thread_id')
            message_id = data.get('message_id')
            if not platform_str or not chat_id:
                return None
            platform = Platform(platform_str)
            transport = resolve_delivery_transport(platform, self.config, self.adapters)
            if transport is None:
                logger.debug('Restart notification skipped: no live transport for %s', platform_str)
                return None
            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and (not platform_cfg.gateway_restart_notification):
                logger.info('Restart notification suppressed: %s has gateway_restart_notification=false', platform_str)
                return None
            metadata = self._thread_metadata_for_target(platform, chat_id, thread_id, chat_type=chat_type, reply_to_message_id=message_id, adapter=transport.adapter)
            if data.get('delivered_via_upstream_relay') is True:
                metadata = dict(metadata or {})
                if data.get('user_id'):
                    metadata['user_id'] = str(data['user_id'])
                if data.get('scope_id'):
                    metadata['scope_id'] = str(data['scope_id'])
            result = await transport.send(platform, str(chat_id), '♻ Gateway restarted successfully. Your session continues.', metadata=_non_conversational_metadata(metadata, platform=platform))
            if result is not None and getattr(result, 'success', True) is False:
                logger.warning('Restart notification to %s:%s was not delivered: %s', platform_str, chat_id, getattr(result, 'error', 'send returned success=False'))
                return None
            logger.info('Sent restart notification to %s:%s', platform_str, chat_id)
            return (str(platform_str), str(chat_id), str(thread_id) if thread_id else None)
        except Exception as e:
            logger.warning('Restart notification failed: %s', e)
            return None
        finally:
            notify_path.unlink(missing_ok=True)

    async def _send_home_channel_startup_notifications(self, *, skip_targets: Optional[set[tuple[str, str, Optional[str]]]]=None) -> set[tuple[str, str, Optional[str]]]:
        """Notify configured home channels that the gateway is back online.

        The notification is best-effort and sent once per connected platform
        home channel. ``skip_targets`` lets startup avoid duplicate messages
        when a more specific restart notification is queued for the same chat.
        """
        delivered: set[tuple[str, str, Optional[str]]] = set()
        skipped = skip_targets or set()
        message = '♻️ Gateway online — Duck Agent is back and ready.'
        for platform, platform_cfg in self.config.platforms.items():
            home = platform_cfg.home_channel
            if not home or not home.chat_id:
                continue
            transport = resolve_delivery_transport(platform, self.config, self.adapters)
            if transport is None:
                continue
            if not platform_cfg.gateway_restart_notification:
                logger.info('Home-channel startup notification suppressed: %s has gateway_restart_notification=false', platform.value)
                continue
            target = (platform.value, str(home.chat_id), str(home.thread_id) if home.thread_id else None)
            if target in skipped or target in delivered:
                continue
            try:
                metadata = self._thread_metadata_for_target(platform, home.chat_id, home.thread_id, adapter=transport.adapter)
                if transport.is_relay:
                    metadata = dict(metadata or {})
                    if home.user_id:
                        metadata['user_id'] = home.user_id
                    if home.scope_id:
                        metadata['scope_id'] = home.scope_id
                send_metadata = _non_conversational_metadata(metadata, platform=platform)
                if send_metadata is not None or transport.is_relay:
                    result = await transport.send(platform, str(home.chat_id), message, metadata=send_metadata)
                else:
                    result = await transport.adapter.send(str(home.chat_id), message)
                if result is not None and getattr(result, 'success', True) is False:
                    logger.warning('Home-channel startup notification failed for %s:%s: %s', platform.value, home.chat_id, getattr(result, 'error', 'send returned success=False'))
                    continue
                delivered.add(target)
                logger.info('Sent home-channel startup notification to %s:%s', platform.value, home.chat_id)
            except Exception as exc:
                logger.warning('Home-channel startup notification failed for %s:%s: %s', platform.value, home.chat_id, exc)
        return delivered

    def _set_session_env(self, context: SessionContext) -> list:
        """Set session context variables for the current async task.

        Uses ``contextvars`` instead of ``os.environ`` so that concurrent
        gateway messages cannot overwrite each other's session state.

        Returns a list of reset tokens; pass them to ``_clear_session_env``
        in a ``finally`` block.
        """
        from gateway.session_context import set_session_vars
        _adapters = getattr(self, 'adapters', None) or {}
        _adapter = _adapters.get(context.source.platform)
        _async_delivery = getattr(_adapter, 'supports_async_delivery', True)
        return set_session_vars(platform=context.source.platform.value, chat_id=context.source.chat_id, chat_type=str(context.source.chat_type) if context.source.chat_type else '', chat_name=context.source.chat_name or '', thread_id=str(context.source.thread_id) if context.source.thread_id else '', user_id=str(context.source.user_id) if context.source.user_id else '', user_name=str(context.source.user_name) if context.source.user_name else '', session_key=context.session_key, message_id=str(context.source.message_id) if context.source.message_id else '', profile=getattr(context.source, 'profile', '') or '', async_delivery=_async_delivery, cron_session='')

    def _clear_session_env(self, tokens: list) -> None:
        """Restore session context variables to their pre-handler values."""
        from gateway.session_context import clear_session_vars
        clear_session_vars(tokens)

    async def _run_in_executor_with_context(self, func, *args):
        """Run blocking work in the thread pool while preserving session contextvars."""
        loop = asyncio.get_running_loop()
        ctx = copy_context()
        return await loop.run_in_executor(self._get_executor(), ctx.run, func, *args)

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Return the gateway-owned executor for blocking agent work."""
        lock = getattr(self, '_executor_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._executor_lock = lock
        with lock:
            if getattr(self, '_executor_closing', False):
                raise RuntimeError('Gateway is shutting down; executor unavailable')
            executor = getattr(self, '_executor', None)
            if executor is None or getattr(executor, '_shutdown', False):
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix='duck-agent-gateway')
                self._executor = executor
            return executor

    def _shutdown_executor(self) -> None:
        """Stop the gateway-owned executor without touching the loop default."""
        lock = getattr(self, '_executor_lock', None)
        if lock is None:
            return
        with lock:
            self._executor_closing = True
            executor = getattr(self, '_executor', None)
            self._executor = None
        if executor is None:
            return
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    def _decide_image_input_mode(self, *, source: Optional[SessionSource]=None, session_key: Optional[str]=None, user_config: Optional[dict]=None, provider: Optional[str]=None, model: Optional[str]=None) -> str:
        """Resolve image-input routing for the effective model this turn.

        Returns ``"native"`` (attach pixels on the user turn) or ``"text"``
        (pre-analyze with vision_analyze and prepend the description). See
        agent/image_routing.py for the full decision table.

        Gateway sessions can have /model overrides that live outside
        config.yaml. Image preprocessing runs before AIAgent sets the
        auxiliary_client runtime globals, so resolve the same per-session
        runtime bundle the upcoming agent turn will use instead of consulting
        only the persisted default model.
        """
        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config
            cfg = user_config if isinstance(user_config, dict) else load_config()
            resolved_provider = (provider or '').strip()
            resolved_model = (model or '').strip()
            resolved_requested_provider = ''
            needs_session_runtime = not resolved_provider or not resolved_model
            has_session_identity = source is not None or session_key
            if needs_session_runtime and has_session_identity:
                try:
                    turn_model, runtime_kwargs = self._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=cfg)
                    if not resolved_model and isinstance(turn_model, str):
                        resolved_model = turn_model.strip()
                    runtime_provider = runtime_kwargs.get('provider') if isinstance(runtime_kwargs, dict) else None
                    runtime_requested_provider = runtime_kwargs.get('requested_provider') if isinstance(runtime_kwargs, dict) else None
                    if not resolved_provider and isinstance(runtime_provider, str):
                        resolved_provider = runtime_provider.strip()
                    if isinstance(runtime_requested_provider, str):
                        resolved_requested_provider = runtime_requested_provider.strip()
                except Exception as exc:
                    logger.debug('image_routing: session runtime resolution failed, falling back to config — %s', exc)
            if not resolved_provider:
                resolved_provider = _read_main_provider()
            if not resolved_model:
                resolved_model = _read_main_model()
            return decide_image_input_mode(resolved_provider, resolved_model, cfg, requested_provider=resolved_requested_provider)
        except Exception as exc:
            logger.debug('image_routing: decision failed, falling back to text — %s', exc)
            return 'text'

    async def _enrich_message_with_vision(self, user_text: str, image_paths: List[str]) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from tools.vision_tools import vision_analyze_tool
        from agent.memory_manager import sanitize_context
        analysis_prompt = 'Describe everything visible in this image in thorough detail. Include any text, code, data, objects, people, layout, colors, and any other notable visual information.'
        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug('Auto-analyzing user image: %s', path)
                result_json = await vision_analyze_tool(image_url=path, user_prompt=analysis_prompt)
                result = json.loads(result_json)
                if result.get('success'):
                    description = result.get('analysis', '')
                    description = sanitize_context(description)
                    enriched_parts.append(f"[The user sent an image~ Here's what I can see:\n{description}]\n[If you need a closer look, use vision_analyze with image_url: {path} ~]")
                else:
                    enriched_parts.append(f"[The user sent an image but I couldn't quite see it this time (>_<) You can try looking at it yourself with vision_analyze using image_url: {path}]")
            except Exception as e:
                logger.error('Vision auto-analysis error: %s', e)
                enriched_parts.append(f'[The user sent an image but something went wrong when I tried to look at it~ You can try examining it yourself with vision_analyze using image_url: {path}]')
        if enriched_parts:
            prefix = '\n\n'.join(enriched_parts)
            if user_text:
                return f'{prefix}\n\n{user_text}'
            return prefix
        return user_text

    async def _enrich_message_with_transcription(self, user_text: str, audio_paths: List[str]) -> tuple[str, List[str]]:
        """
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            A tuple of ``(enriched_text, successful_transcripts)``:
              - ``enriched_text``: the message string with transcription wrappers
                prepended (same as before).
              - ``successful_transcripts``: the raw transcript strings for audio
                clips that were successfully transcribed, in input order. Empty
                list if every clip failed or STT is disabled. Callers can use
                this to echo transcripts back to the user before the agent loop.
        """
        seen = set()
        audio_paths = [p for p in audio_paths if p not in seen and (not seen.add(p))]
        if not getattr(self.config, 'stt_enabled', True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                if duration_str:
                    notes.append(f'[The user sent a voice message: {abs_path} (duration: {duration_str})]')
                else:
                    notes.append(f'[The user sent a voice message: {abs_path}]')
            if not notes:
                return (user_text, [])
            prefix = '\n\n'.join(notes)
            _placeholder = '(The user sent a message with no text content)'
            if user_text and user_text.strip() == _placeholder:
                return (prefix, [])
            if user_text:
                return (f'{prefix}\n\n{user_text}', [])
            return (prefix, [])
        try:
            from tools.transcription_tools import transcribe_audio, transcribe_audio_local_fallback
        except ModuleNotFoundError as e:
            logger.error('Transcription module unavailable: %s', e)
            unavailable_note = '[voice message could not be transcribed]'
            _placeholder = '(The user sent a message with no text content)'
            if user_text and user_text.strip() == _placeholder:
                return (unavailable_note, [])
            if user_text:
                return (f'{unavailable_note}\n\n{user_text}', [])
            return (unavailable_note, [])
        enriched_parts = []
        successful_transcripts: List[str] = []
        for path in audio_paths:
            try:
                logger.debug('Transcribing user voice: %s', path)
                result = await asyncio.to_thread(transcribe_audio, path)
                if not result.get('success'):
                    fallback = await asyncio.to_thread(transcribe_audio_local_fallback, path)
                    if fallback.get('success'):
                        logger.info('Configured STT failed for %s; recovered with local STT', path)
                        result = fallback
                if result['success']:
                    transcript = result['transcript']
                    if not (transcript or '').strip():
                        enriched_parts.append('[The user sent a voice message but it came through empty or inaudible — speech-to-text returned no words. Do not guess at the content; ask the user to resend or type it out.]')
                        continue
                    successful_transcripts.append(transcript)
                    enriched_parts.append(f'"{transcript}"')
                else:
                    error = result.get('error', 'unknown error')
                    logger.info('Voice transcription failed for %s: %s', path, error)
                    from tools.credential_files import to_agent_visible_cache_path
                    agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                    enriched_parts.append(f'[voice message could not be transcribed automatically; the audio is available at: {agent_path}]')
            except Exception as e:
                logger.error('Transcription error: %s', e)
                from tools.credential_files import to_agent_visible_cache_path
                agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                enriched_parts.append(f'[voice message could not be transcribed automatically; the audio is available at: {agent_path}]')
        if enriched_parts:
            prefix = '\n\n'.join(enriched_parts)
            _placeholder = '(The user sent a message with no text content)'
            if user_text and user_text.strip() == _placeholder:
                return (prefix, successful_transcripts)
            if user_text:
                return (f'{prefix}\n\n{user_text}', successful_transcripts)
            return (prefix, successful_transcripts)
        return (user_text, successful_transcripts)

    def _pending_event_audio_paths(self, event) -> List[str]:
        """Return STT-eligible paths from a pending voice message."""
        audio_paths: List[str] = []
        media_urls = getattr(event, 'media_urls', None) or []
        for i, path in enumerate(media_urls):
            if _event_media_is_stt_input(event, i):
                audio_paths.append(path)
        return audio_paths

    async def _transcribe_pending_audio_event_once(self, event, user_text: Optional[str]=None) -> tuple[str | None, List[str]]:
        """Transcribe a pending audio event once and cache the result on the event.

        Voice follow-ups can be inspected first by the interrupt monitor and
        later consumed by the pending-drain path.  Both need the same transcript,
        but only one STT call and one transcript echo should happen for the
        platform message.
        """
        if hasattr(event, '_gateway_pending_stt_text'):
            cached_text = getattr(event, '_gateway_pending_stt_text')
            cached_transcripts = getattr(event, '_gateway_pending_stt_transcripts', []) or []
            return (cached_text, list(cached_transcripts))
        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            return (user_text if user_text is not None else getattr(event, 'text', None) or None, [])
        text = user_text if user_text is not None else getattr(event, 'text', '') or ''
        enriched_text, successful_transcripts = await self._enrich_message_with_transcription(text, audio_paths)
        setattr(event, '_gateway_pending_stt_text', enriched_text)
        setattr(event, '_gateway_pending_stt_transcripts', list(successful_transcripts))
        return (enriched_text, successful_transcripts)

    async def _echo_pending_stt_transcripts_once(self, event, adapter, source, transcripts: List[str], *, metadata=None, log_context: str='Transcript') -> None:
        """Echo pending-event STT transcripts to the chat at most once.

        The already-echoed transcripts are tracked as a COUNT rather than a
        single boolean.  ``merge_pending_message_event`` can append a second
        voice note to an event whose first transcript was already echoed and
        invalidates the transcription cache; the re-run transcription then
        returns the earlier transcripts as a prefix of the new list, so
        echoing only the unsent tail suppresses the repeat while still
        surfacing the newly merged note.  A count rather than a set of seen
        values because two separate notes that transcribe identically are two
        distinct deliveries and both must be echoed.
        """
        if not transcripts or not self._should_echo_stt_transcripts() or adapter is None:
            return
        already_echoed = int(getattr(event, '_gateway_pending_stt_echoed', 0) or 0)
        unsent = transcripts[already_echoed:]
        setattr(event, '_gateway_pending_stt_echoed', already_echoed + len(unsent))
        for tx in unsent:
            try:
                await adapter.send(source.chat_id, f'🎙️ "{tx}"', metadata=metadata)
            except Exception as echo_exc:
                logger.debug('%s echo failed (non-fatal): %s', log_context, echo_exc)

    async def _transcribe_and_echo_pending_voice(self, event, adapter, source, text: str, *, log_context: str, metadata=_UNSET) -> tuple[str, List[str]]:
        """Transcribe a pending voice event and echo transcripts once.

        Unified helper for all interrupt/monitor/backup/drain paths that need
        to transcribe a pending voice event and echo the transcript to chat.
        Returns ``(enriched_text, transcripts)`` so the caller can feed the
        enriched text into ``agent.interrupt()`` or the pending-drain flow.

        If the event has no STT-eligible media, returns ``(text, [])`` unchanged.
        The caller is responsible for the ``_build_media_placeholder`` fallback
        when ``text`` is empty and the event has non-audio media.
        """
        if not self._pending_event_audio_paths(event):
            return (text, [])
        try:
            enriched_text, transcripts = await self._transcribe_pending_audio_event_once(event, text)
            echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)) if metadata is _UNSET else metadata
            await self._echo_pending_stt_transcripts_once(event, adapter, source, transcripts, metadata=echo_meta, log_context=log_context)
            return (enriched_text or text, transcripts)
        except Exception as trans_exc:
            logger.warning('%s transcription failed: %s', log_context, trans_exc)
            return (text, [])

    def _build_process_event_source(self, evt: dict):
        """Resolve the canonical source for a synthetic background-process event.

        Prefer the persisted session-store origin for the event's session key.
        Falling back to the currently active foreground event is what causes
        cross-topic bleed, so don't do that.
        """
        from gateway.session import SessionSource
        session_key = str(evt.get('session_key') or '').strip()
        derived_platform = ''
        derived_chat_type = ''
        derived_chat_id = ''
        if session_key:
            try:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(session_key)
                if entry and getattr(entry, 'origin', None):
                    return entry.origin
            except Exception as exc:
                logger.debug('Synthetic process-event session-store lookup failed for %s: %s', session_key, exc)
            cached_source = self._get_cached_session_source(session_key)
            if cached_source is not None:
                return cached_source
            _parsed = _parse_session_key(session_key)
            if _parsed:
                derived_platform = _parsed['platform']
                derived_chat_type = _parsed['chat_type']
                derived_chat_id = _parsed['chat_id']
        platform_name = str(evt.get('platform') or derived_platform or '').strip().lower()
        chat_type = str(evt.get('chat_type') or derived_chat_type or '').strip().lower()
        chat_id = str(evt.get('chat_id') or derived_chat_id or '').strip()
        if not platform_name or not chat_type or (not chat_id):
            logger.warning('Synthetic event source unresolvable: session_key=%r platform=%r chat_type=%r chat_id=%r evt_type=%s', session_key, platform_name, chat_type, chat_id, evt.get('type', '?'))
            return None
        try:
            platform = Platform(platform_name)
            if platform.value not in _BUILTIN_PLATFORM_VALUES:
                try:
                    from gateway.platform_registry import platform_registry
                    if not platform_registry.is_registered(platform.value):
                        raise ValueError(platform_name)
                except Exception:
                    raise ValueError(platform_name)
        except Exception:
            logger.warning('Synthetic process event has invalid platform metadata: %r', platform_name)
            return None
        return SessionSource(platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=str(evt.get('thread_id') or '').strip() or None, user_id=str(evt.get('user_id') or '').strip() or None, user_name=str(evt.get('user_name') or '').strip() or None)

    async def _inject_watch_notification(self, synth_text: str, evt: dict) -> Optional[bool]:
        """Inject a watch/completion notification as a synthetic message event.

        Routing must come from the queued event itself, not from whatever
        foreground message happened to be active when the queue was drained.
        Returns ``True`` after adapter acceptance, ``False`` after a retryable
        adapter failure, and ``None`` when the event has no gateway route. This
        is not a transactional boundary: a process crash after adapter
        acceptance can still cause durable at-least-once replay.
        """
        source = self._build_process_event_source(evt)
        if not source:
            raw_sid = str(evt.get('origin_session_id') or '').strip()
            if not raw_sid:
                _sk = str(evt.get('session_key') or '').strip()
                if _sk and _parse_session_key(_sk) is None:
                    raw_sid = _sk
            if raw_sid:
                adapter = self.adapters.get(Platform.API_SERVER)
                from gateway.wake import adapter_supports_push, deliver_wake
                if adapter is not None and (not adapter_supports_push(adapter)):
                    try:
                        logger.info('Watch pattern notification — waking api_server session %s via self-post', raw_sid)
                        await deliver_wake(adapter, text=synth_text, session_id=raw_sid)
                        return True
                    except Exception as e:
                        logger.warning('Watch notification self-post wake failed for session %s: %s', raw_sid, e)
                        return False
                logger.warning('Dropping watch notification for raw session %s: no api_server adapter to self-post through', raw_sid)
                return None
            logger.warning('Dropping watch notification with no routing metadata for process %s', evt.get('session_id', 'unknown'))
            return None
        platform_name = source.platform.value if hasattr(source.platform, 'value') else str(source.platform)
        adapter = None
        for p, a in self.adapters.items():
            if p.value == platform_name:
                adapter = a
                break
        if not adapter:
            return None
        from gateway.wake import adapter_supports_push as _wake_push_ok
        if not _wake_push_ok(adapter):
            from gateway.wake import deliver_wake
            raw_sid = str(evt.get('origin_session_id') or '').strip() or str(source.chat_id or '')
            try:
                logger.info('Watch pattern notification — waking api_server session %s via self-post', raw_sid)
                await deliver_wake(adapter, text=synth_text, session_id=raw_sid)
                return True
            except Exception as e:
                logger.warning('Watch notification self-post wake failed for session %s: %s', raw_sid, e)
                return False
        try:
            metadata = {}
            parent_session_id = str(evt.get('parent_session_id') or '').strip()
            if parent_session_id:
                metadata['gateway_session_id'] = parent_session_id
            synth_event = MessageEvent(text=synth_text, message_type=MessageType.TEXT, source=source, internal=True, message_id=str(evt.get('message_id') or '').strip() or None, metadata=metadata)
            logger.info('Watch pattern notification — injecting for %s chat=%s thread=%s', platform_name, source.chat_id, source.thread_id)
            await adapter.handle_message(synth_event)
            return True
        except Exception as e:
            logger.error('Watch notification injection error: %s', e)
            return False

    @staticmethod
    def _completion_delivery_identity(evt: dict) -> Optional[tuple[str, str, object]]:
        """Return a producer-stable identity when one is available.

        Delegation UUIDs identify one producer completion. Process session IDs
        are normally unique too, but include the persisted spawn epoch so an
        explicitly reused ID represents a distinct process incarnation. Legacy
        process events without ``started_at`` are delivered without deduplication
        rather than risking suppression of a real completion.
        """
        evt_type = str(evt.get('type') or '')
        if evt_type == 'async_delegation':
            producer_id = str(evt.get('delegation_id') or '')
            return (evt_type, producer_id, '') if producer_id else None
        if evt_type == 'completion':
            producer_id = str(evt.get('session_id') or '')
            started_at = evt.get('started_at')
            if producer_id and started_at is not None:
                return (evt_type, producer_id, started_at)
        return None

    async def _classify_completion_target(self, parent_session_id: str) -> str:
        """Classify an async-completion delivery target before adapter acceptance.

        Returns one of:

        - ``"deliver"`` — the spawning session is live, or ended by a
          compression rotation with a verified live continuation. The inner
          #55578 resolver (:meth:`_resolve_async_delegation_session`) still
          owns the actual route retarget; this pre-flight only proves the
          completion is deliverable so the durable ack stays honest.
        - ``"terminal"`` — the spawning session is gone for good (unknown, or
          ended at an explicit user boundary such as /new). Delivery can never
          succeed; the durable row should be terminally dropped rather than
          falsely acknowledged as delivered or replayed forever as pending.
        - ``"retry"`` — transient uncertainty (session DB unavailable, lookup
          error, or a compression rotation caught mid-flight before its
          continuation exists). The claim should be released so a later
          consumer can retry; the attempt cap bounds the churn.
        """
        session_db = getattr(self, '_session_db', None)
        if session_db is None:
            return 'retry'
        try:
            parent = await session_db.get_session(parent_session_id)
        except Exception:
            logger.debug('Async-completion pre-flight parent lookup failed for %s', parent_session_id, exc_info=True)
            return 'retry'
        if parent is None:
            return 'terminal'
        if not parent.get('ended_at'):
            return 'deliver'
        if parent.get('end_reason') != 'compression':
            return 'terminal'
        try:
            tip_session_id = await session_db.get_compression_tip(parent_session_id)
            if not tip_session_id or tip_session_id == parent_session_id:
                return 'retry'
            tip = await session_db.get_session(tip_session_id)
        except Exception:
            logger.debug('Async-completion pre-flight tip lookup failed for %s', parent_session_id, exc_info=True)
            return 'retry'
        if tip is None or tip.get('ended_at'):
            return 'retry'
        return 'deliver'

    async def _deliver_completion_notification(self, synth_text: str, evt: dict) -> Optional[bool]:
        """Deliver once per live gateway, or return False for a retry.

        ``True`` means this caller reached adapter acceptance, ``False`` means
        injection failed and the claim was released for retry, and ``None``
        means either another same-lifecycle caller owns/delivered the producer
        event or the event has no gateway route. No cross-process exactly-once
        guarantee is claimed.
        """
        identity = self._completion_delivery_identity(evt)
        durable_claim_id = ''
        durable_delegation_id = ''
        if evt.get('type') == 'async_delegation':
            durable_delegation_id = str(evt.get('delegation_id') or '')
            if durable_delegation_id:
                try:
                    from tools.async_delegation import claim_completion_delivery
                    durable_claim_id = f"gateway:{id(self)}:{__import__('uuid').uuid4().hex}"
                    if not claim_completion_delivery(durable_delegation_id, durable_claim_id):
                        return None
                except Exception as exc:
                    logger.warning('Could not claim durable async completion %s: %s', durable_delegation_id, exc)
                    return False
            parent_session_id = str(evt.get('parent_session_id') or '').strip()
            if parent_session_id:
                verdict = await self._classify_completion_target(parent_session_id)
                if verdict == 'terminal':
                    logger.warning('Async delegation %s targets permanently-gone session %s; terminally dropping delivery (result remains in the delegation records).', durable_delegation_id or '<legacy>', parent_session_id)
                    if durable_claim_id:
                        try:
                            from tools.async_delegation import drop_completion_delivery
                            drop_completion_delivery(durable_delegation_id, durable_claim_id)
                        except Exception:
                            logger.debug('Could not drop durable completion claim', exc_info=True)
                    return None
                if verdict == 'retry':
                    if durable_claim_id:
                        try:
                            from tools.async_delegation import release_completion_delivery
                            release_completion_delivery(durable_delegation_id, durable_claim_id)
                        except Exception:
                            logger.debug('Could not release durable completion claim', exc_info=True)
                    return False
        if identity is not None:
            with self._completion_delivery_lock:
                if identity in self._completion_deliveries_inflight or identity in self._completion_deliveries_delivered:
                    return None
                self._completion_deliveries_inflight.add(identity)
        accepted = False
        try:
            injection_result = await self._inject_watch_notification(synth_text, evt)
            if injection_result is not True:
                return injection_result
            accepted = True
            if identity is not None:
                with self._completion_delivery_lock:
                    self._completion_deliveries_inflight.discard(identity)
                    self._completion_deliveries_delivered[identity] = None
                    while len(self._completion_deliveries_delivered) > self._completion_delivery_retention:
                        self._completion_deliveries_delivered.popitem(last=False)
            if durable_claim_id:
                try:
                    from tools.async_delegation import complete_completion_delivery
                    complete_completion_delivery(durable_delegation_id, durable_claim_id)
                except Exception as exc:
                    logger.warning('Could not acknowledge durable async completion %s: %s', durable_delegation_id, exc)
            return True
        finally:
            if identity is not None and (not accepted):
                with self._completion_delivery_lock:
                    self._completion_deliveries_inflight.discard(identity)
            if durable_claim_id and (not accepted):
                try:
                    from tools.async_delegation import release_completion_delivery
                    release_completion_delivery(durable_delegation_id, durable_claim_id)
                except Exception:
                    logger.debug('Could not release durable completion claim', exc_info=True)

    def _enrich_async_delegation_routing(self, evt: dict) -> None:
        """Fill platform/chat_id/thread_id/chat_type on an async-delegation event.

        Async-delegation completion events only carry ``session_key`` (the
        daemon worker has no access to the per-message routing metadata the
        terminal background watcher captures at spawn time). Parse the
        session_key into the routing fields ``_build_process_event_source``
        expects. Best-effort: a CLI-origin event (empty session_key) is left
        as-is and simply won't route on the gateway.
        """
        if evt.get('platform'):
            return
        parsed = _parse_session_key(evt.get('session_key', '') or '')
        if not parsed:
            return
        evt['platform'] = parsed.get('platform', '')
        evt['chat_type'] = parsed.get('chat_type', '')
        evt['chat_id'] = parsed.get('chat_id', '')
        if parsed.get('thread_id'):
            evt['thread_id'] = parsed['thread_id']

    async def _async_delegation_watcher(self, interval: float=2.0) -> None:
        """Drain async-delegation completions and inject them as new turns.

        Background subagents (``delegate_task(background=true)``) run on the
        async-delegation daemon executor — they have no per-process watcher
        task, so their completion events would only be seen by the post-turn
        queue drain. This watcher covers the IDLE case: when a background
        subagent finishes while no agent turn is running, its result still
        re-enters the originating session promptly.

        Mirrors the CLI's idle ``process_loop`` drain. Stays silent when the
        queue has nothing for us; ignores non-async event types (those are
        handled by ``_run_process_watcher`` / the post-turn drain).
        """
        await asyncio.sleep(3)
        from tools.process_registry import process_registry as _pr
        while self._running:
            try:
                requeue = []
                async_events = []
                while not _pr.completion_queue.empty():
                    try:
                        evt = _pr.completion_queue.get_nowait()
                    except Exception:
                        break
                    if evt.get('type') == 'async_delegation':
                        async_events.append(evt)
                    else:
                        requeue.append(evt)
                for evt in requeue:
                    _pr.completion_queue.put(evt)
                for evt in async_events:
                    self._enrich_async_delegation_routing(evt)
                    synth_text = _format_gateway_process_notification(evt)
                    if not synth_text:
                        continue
                    try:
                        delivered = await self._deliver_completion_notification(synth_text, evt)
                        if delivered is False:
                            _pr.completion_queue.put(evt)
                    except Exception as e:
                        _pr.completion_queue.put(evt)
                        logger.error('Async delegation injection error: %s', e)
            except Exception as e:
                logger.debug('Async delegation watcher error: %s', e)
            await asyncio.sleep(interval)

    async def _run_process_watcher(self, watcher: dict) -> None:
        """
        Periodically check a background process and push updates to the user.

        Runs as an asyncio task. Stays silent when nothing changed.
        Auto-removes when the process exits or is killed.

        Notification mode (from ``display.background_process_notifications``):
          - ``all``    — running-output updates + final message
          - ``result`` — final completion message only
          - ``error``  — final message only when exit code != 0
          - ``off``    — no messages at all
        """
        from tools.process_registry import process_registry
        session_id = watcher['session_id']
        interval = watcher['check_interval']
        session_key = watcher.get('session_key', '')
        platform_name = watcher.get('platform', '')
        chat_id = watcher.get('chat_id', '')
        thread_id = watcher.get('thread_id', '')
        user_id = watcher.get('user_id', '')
        user_name = watcher.get('user_name', '')
        message_id = str(watcher.get('message_id') or '').strip() or None
        agent_notify = watcher.get('notify_on_complete', False)
        notify_mode = self._load_background_notifications_mode()
        logger.debug('Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)', session_id, interval, notify_mode, agent_notify)
        if notify_mode == 'off' and (not agent_notify):
            while True:
                await asyncio.sleep(interval)
                session = process_registry.get(session_id)
                if session is None or session.exited:
                    break
            logger.debug('Process watcher ended (silent): %s', session_id)
            return
        last_output_len = 0
        while True:
            await asyncio.sleep(interval)
            session = process_registry.get(session_id)
            if session is None:
                break
            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len
            if session.exited:
                from tools.process_registry import format_process_notification, process_registry as _pr_check
                if agent_notify and (not _pr_check.is_completion_consumed(session_id)):
                    from agent.redact import redact_terminal_output
                    from tools.ansi_strip import strip_ansi
                    _command = getattr(session, 'command', '') or ''
                    _raw = strip_ansi(session.output_buffer) if session.output_buffer else ''
                    _raw = redact_terminal_output(_raw, _command)
                    _command = _redact_gateway_user_facing_secrets(_command)
                    _LIMIT = 2000
                    if len(_raw) > _LIMIT:
                        _tail = _raw[-_LIMIT:]
                        _nl = _tail.find('\n')
                        _tail = _tail[_nl + 1:] if _nl != -1 else _tail
                        _out = f'[… output truncated — showing last {len(_tail)} chars]\n{_tail}'
                    else:
                        _out = _raw
                    completion_evt = {'type': 'completion', 'session_id': session_id, 'session_key': session_key, 'platform': platform_name, 'chat_type': watcher.get('chat_type', ''), 'chat_id': chat_id, 'thread_id': thread_id, 'user_id': user_id, 'user_name': user_name, 'message_id': message_id, 'started_at': getattr(session, 'started_at', None), 'command': _command, 'exit_code': session.exit_code, 'completion_reason': getattr(session, 'completion_reason', 'exited'), 'termination_source': getattr(session, 'termination_source', ''), 'output': _out}
                    synth_text = format_process_notification(completion_evt)
                    if not synth_text:
                        break
                    delivered = await self._deliver_completion_notification(synth_text, completion_evt)
                    if delivered is False:
                        continue
                    break
                if _pr_check.is_completion_consumed(session_id):
                    logger.debug('Process watcher: completion for %s already consumed via wait/log — skipping raw notification (#65379)', session_id)
                    break
                should_notify = notify_mode in {'all', 'result'} or (notify_mode == 'error' and session.exit_code not in {0, None})
                if should_notify:
                    new_output = session.output_buffer[-1000:] if session.output_buffer else ''
                    if new_output:
                        from agent.redact import redact_terminal_output
                        new_output = redact_terminal_output(new_output, getattr(session, 'command', '') or '')
                    message_text = f"[Background process {session_id} finished with exit code {session.exit_code}~ Here's the final output:\n{new_output}]"
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter and chat_id:
                        try:
                            send_meta = {'thread_id': thread_id} if thread_id else None
                            await adapter.send(chat_id, message_text, metadata=_non_conversational_metadata(send_meta, platform=platform_name))
                        except Exception as e:
                            logger.error('Watcher delivery error: %s', e)
                break
            elif has_new_output and notify_mode == 'all' and (not agent_notify):
                new_output = session.output_buffer[-500:] if session.output_buffer else ''
                if new_output:
                    from agent.redact import redact_terminal_output
                    new_output = redact_terminal_output(new_output, getattr(session, 'command', '') or '')
                message_text = f'[Background process {session_id} is still running~ New output:\n{new_output}]'
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        send_meta = {'thread_id': thread_id} if thread_id else None
                        await adapter.send(chat_id, message_text, metadata=_non_conversational_metadata(send_meta, platform=platform_name))
                    except Exception as e:
                        logger.error('Watcher delivery error: %s', e)
        logger.debug('Process watcher ended: %s', session_id)
    _MAX_INTERRUPT_DEPTH = 3
    _CACHE_BUSTING_CONFIG_KEYS: tuple = (('model', 'context_length'), ('model', 'max_tokens'), ('compression', 'enabled'), ('compression', 'progress_notices'), ('compression', 'threshold'), ('compression', 'model_thresholds'), ('compression', 'threshold_tokens'), ('compression', 'codex_gpt55_autoraise'), ('compression', 'codex_app_server_auto'), ('compression', 'target_ratio'), ('compression', 'protect_last_n'), ('compression', 'proactive_prune_tokens'), ('compression', 'proactive_prune_min_result_chars'), ('compression', 'proactive_prune_min_reclaim_tokens'), ('compression', 'min_tail_user_messages'), ('agent', 'disabled_toolsets'), ('memory', 'provider'), ('checkpoints', 'enabled'), ('checkpoints', 'max_snapshots'), ('checkpoints', 'max_total_size_mb'), ('checkpoints', 'max_file_size_mb'))
    _HONCHO_CACHE_BUSTING_KEYS = ('honcho.peer_name', 'honcho.ai_peer', 'honcho.pin_peer_name', 'honcho.runtime_peer_prefix', 'honcho.user_peer_aliases')
    _HONCHO_CACHE_BUSTING_MEMO: dict[tuple[str, int | None], dict[str, Any]] = {}

    @classmethod
    def _empty_honcho_cache_busting_config(cls) -> dict[str, Any]:
        return {key: None for key in cls._HONCHO_CACHE_BUSTING_KEYS}

    @classmethod
    def _extract_honcho_cache_busting_config(cls) -> dict[str, Any]:
        """Extract Honcho identity keys, memoized by honcho.json mtime."""
        try:
            from plugins.memory.honcho.client import HonchoClientConfig, resolve_config_path
            path = resolve_config_path()
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            memo_key = (str(path), mtime_ns)
            cached = cls._HONCHO_CACHE_BUSTING_MEMO.get(memo_key)
            if cached is not None:
                return dict(cached)
            hcfg = HonchoClientConfig.from_global_config(config_path=path)
            aliases = hcfg.user_peer_aliases or {}
            values = {'honcho.peer_name': hcfg.peer_name, 'honcho.ai_peer': hcfg.ai_peer, 'honcho.pin_peer_name': bool(hcfg.pin_peer_name), 'honcho.runtime_peer_prefix': hcfg.runtime_peer_prefix or '', 'honcho.user_peer_aliases': sorted(aliases.items()) if isinstance(aliases, dict) else []}
            cls._HONCHO_CACHE_BUSTING_MEMO = {memo_key: values}
            return dict(values)
        except Exception:
            return cls._empty_honcho_cache_busting_config()

    @classmethod
    def _extract_cache_busting_config(cls, user_config: dict | None) -> dict:
        """Pull values that must bust the cached agent.

        Returns a flat dict keyed by 'section.key'.  Missing config keys and
        non-dict sections yield None values, which still contribute to the
        signature (so 'absent' vs 'present-and-null' differ).

        The live tool registry generation is included too.  MCP reloads and
        dynamic MCP tool-list changes mutate the registry without necessarily
        changing config.yaml.  Cached AIAgent instances freeze their tool
        schemas at construction time, so a registry generation change must
        rebuild the agent before the next turn.
        """
        out: Dict[str, Any] = {}
        cfg = user_config if isinstance(user_config, dict) else {}
        for section, key in cls._CACHE_BUSTING_CONFIG_KEYS:
            section_val = cfg.get(section)
            if section == 'checkpoints' and isinstance(section_val, bool):
                out[f'{section}.{key}'] = section_val if key == 'enabled' else None
            elif isinstance(section_val, dict):
                out[f'{section}.{key}'] = section_val.get(key)
            else:
                out[f'{section}.{key}'] = None
        try:
            from tools.registry import registry
            out['tools.registry_generation'] = getattr(registry, '_generation', None)
        except Exception:
            out['tools.registry_generation'] = None
        provider = cfg_get(cfg, 'memory', 'provider')
        if isinstance(provider, str) and provider.lower() == 'honcho':
            out.update(cls._extract_honcho_cache_busting_config())
        else:
            out.update(cls._empty_honcho_cache_busting_config())
        return out

    @staticmethod
    def _agent_config_signature(model: str, runtime: dict, enabled_toolsets: list, ephemeral_prompt: str, cache_keys: dict | None=None, user_id: str | None=None, user_id_alt: str | None=None, skip_context_files: bool=False) -> str:
        """Compute a stable string key from agent config values.

        When this signature changes between messages, the cached AIAgent is
        discarded and rebuilt.  When it stays the same, the cached agent is
        reused — preserving the frozen system prompt and tool schemas for
        prompt cache hits.

        ``cache_keys`` is an optional flat dict of additional config values
        that should invalidate the cache when they change.  Callers pass
        the output of ``_extract_cache_busting_config(user_config)`` so
        edits to model.context_length / compression.* in config.yaml are
        picked up on the next gateway message without a manual restart.

        ``user_id`` and ``user_id_alt`` are the runtime user identities
        carried by the current message's gateway source.  They participate
        in the cache key because the Honcho memory provider freezes them
        into ``HonchoSessionManager`` at first-message init (see
        ``plugins/memory/honcho/__init__.py::_do_session_init``).  Without
        them in the signature, a shared-thread session_key (one in which
        ``build_session_key`` intentionally omits the participant ID,
        e.g. ``thread_sessions_per_user=False``) would reuse the cached
        AIAgent across distinct users, causing the second user's messages
        to be attributed to the first user's resolved Honcho peer.  This
        broke #27371's per-user-peer contract in multi-user gateways.
        Per-user agent rebuilds in shared threads trade prompt-cache
        warmth for correct memory attribution.
        """
        import hashlib, json as _j
        _api_key = str(runtime.get('api_key', '') or '')
        _api_key_fingerprint = hashlib.sha256(_api_key.encode()).hexdigest() if _api_key else ''
        _cache_keys_sorted = sorted((cache_keys or {}).items())
        blob = _j.dumps([model, _api_key_fingerprint, runtime.get('base_url', ''), runtime.get('provider', ''), runtime.get('requested_provider', ''), runtime.get('api_mode', ''), sorted(enabled_toolsets) if enabled_toolsets else [], ephemeral_prompt or '', _cache_keys_sorted, str(user_id or ''), str(user_id_alt or ''), bool(skip_context_files)], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _rehydrate_session_model_override(self, session_key: str) -> None:
        """Lazily restore a persisted /model override after a gateway restart.

        ``_session_model_overrides`` is in-memory only, so before persistence
        a restart silently reverted every session to the global default model.
        The non-secret parts (model/provider/base_url) are written through to
        the session store when /model runs (and cleared on /new); here we read
        them back on first use and re-resolve credentials via the normal
        runtime provider resolution — api_key is never persisted to disk.

        No-op when an in-memory override already exists (live state wins) or
        when the store has nothing persisted (e.g. the user ran /new, which
        clears both the in-memory dict and the persisted field).
        """
        _rehydrate_state = self._peek_session_state(session_key)
        if _rehydrate_state is not None and _rehydrate_state.conversation.model_override is not None:
            return
        store = getattr(self, 'session_store', None)
        if store is None:
            return
        try:
            persisted = store.get_model_override(session_key)
        except Exception:
            logger.debug('Failed to read persisted session model override', exc_info=True)
            return
        if not persisted:
            return
        override: Dict[str, Any] = {'model': persisted.get('model'), 'provider': persisted.get('provider'), 'base_url': persisted.get('base_url')}
        provider = persisted.get('provider')
        if provider:
            try:
                runtime = _resolve_runtime_agent_kwargs_for_provider(provider)
                override['api_key'] = runtime.get('api_key')
                override['api_mode'] = runtime.get('api_mode')
                override['credential_pool'] = runtime.get('credential_pool')
                if not override.get('base_url'):
                    override['base_url'] = runtime.get('base_url')
            except Exception:
                logger.debug('Credential re-resolution failed for persisted override (provider=%s); using credential-less override', provider, exc_info=True)
        self._session_state(session_key).conversation.model_override = override
        logger.info('Rehydrated persisted /model override for session=%s: model=%s provider=%s', session_key, override.get('model'), provider or '')

    def _apply_session_model_override(self, session_key: str, model: str, runtime_kwargs: dict) -> tuple:
        """Apply /model session overrides if present, returning (model, runtime_kwargs).

        The gateway /model command stores per-session overrides in
        ``_session_model_overrides``.  These must take precedence over
        config.yaml defaults so the switched model is actually used for
        subsequent messages.  Fields with ``None`` values are skipped so
        partial overrides don't clobber valid config defaults.
        """
        _apply_state = self._peek_session_state(session_key)
        override = _apply_state.conversation.model_override if _apply_state else None
        if not override:
            return (model, runtime_kwargs)
        model = override.get('model', model)
        for key in ('provider', 'api_key', 'base_url', 'api_mode', 'credential_pool'):
            val = override.get(key)
            if val is not None:
                runtime_kwargs[key] = val
        if runtime_kwargs.get('api_key') and runtime_kwargs.get('credential_pool') is None and override.get('provider'):
            runtime_kwargs['credential_pool'] = _credential_pool_for_provider(override.get('provider'))
        return (model, runtime_kwargs)

    def _snapshot_session_model_override(self, session_key: str) -> dict:
        """Capture a gateway session override before a one-turn switch."""
        _snap_state = self._peek_session_state(session_key)
        override = _snap_state.conversation.model_override if _snap_state else None
        return {'had_override': override is not None, 'override': dict(override) if override is not None else None}

    def _restore_session_model_override(self, session_key: str, snapshot: dict) -> None:
        """Restore the session override captured before a one-turn switch."""
        if not session_key:
            return
        if snapshot.get('had_override'):
            self._session_state(session_key).conversation.model_override = dict(snapshot.get('override') or {})
        else:
            _rst_state = self._peek_session_state(session_key)
            if _rst_state is not None:
                _rst_state.conversation.model_override = None
        self._evict_cached_agent(session_key)

    def _is_intentional_model_switch(self, session_key: str, agent_model: str) -> bool:
        """Return True if *agent_model* matches an active /model session override."""
        _ims_state = self._peek_session_state(session_key)
        override = _ims_state.conversation.model_override if _ims_state else None
        return override is not None and override.get('model') == agent_model

    def _release_running_agent_state(self, session_key: str, *, run_generation: Optional[int]=None) -> bool:
        """Pop ALL per-running-agent state entries for ``session_key``.

        Replaces ad-hoc ``del self._running_agents[key]`` calls scattered
        across the gateway.  Those sites had drifted: some popped only
        ``_running_agents``; some also ``_running_agents_ts``; only one
        path also cleared ``_busy_ack_ts``.  Each missed entry was a
        small, persistent leak — a (str_key → float) tuple per session
        per gateway lifetime.

        Use this at every site that ends a running turn, regardless of
        cause (normal completion, /stop, /reset, /resume, sentinel
        cleanup, stale-eviction).  Per-session state that PERSISTS
        across turns (``_session_model_overrides``, ``_voice_mode``,
        ``_pending_approvals``, ``_update_prompt_pending``) is NOT
        touched here — those have their own lifecycles.

        When ``run_generation`` is provided, only clear the slot if that
        generation is still current for the session.  This prevents an
        older async run whose generation was bumped by /stop or /new from
        clobbering a newer run's state during its own unwind.  Returns
        True when the slot was cleared, False when an ownership guard
        blocked it.
        """
        if not session_key:
            return False
        if run_generation is not None and (not self._is_session_run_current(session_key, run_generation)):
            return False
        state = self._peek_session_state(session_key)
        if state is not None:
            lease = state.turn.lease
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    logger.debug('Failed to release active session slot', exc_info=True)
            state.turn.clear()
        self._persist_active_agents()
        return True

    def _release_turn_lease(self, session_key: str, run_generation: int) -> bool:
        """Release the turn lease acquired by (``session_key``, ``run_generation``).

        Companion to the acquisition in ``_handle_message_with_agent``
        (#64934). The token map is keyed by (routing key, run generation), so
        this can only ever free the lease its own turn acquired — a stale
        unwind whose generation was bumped by /stop or /new pops ITS token,
        and the registry's identity check refuses it if a newer turn already
        holds the lease. Idempotent and safe for bare test runners built via
        ``object.__new__`` (getattr defaults).
        """
        if not session_key:
            return False
        registry = getattr(self, '_turn_leases', None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        token = turn.lease_token
        turn.lease_token = None
        turn.lease_generation = None
        try:
            return registry.release(token)
        except Exception:
            logger.debug('Failed to release turn lease', exc_info=True)
            return False

    def _rebind_turn_lease(self, session_key: str, run_generation: int, new_session_id: str) -> bool:
        """Follow a mid-turn session_id rotation with the held turn lease.

        Compression (session-hygiene pre-compression or the agent's own
        compressor) can rotate ``session_entry.session_id`` while this turn
        is in flight. The turn's flush targets the NEW id, so the
        serialization boundary must follow it — otherwise an alias routing
        key resolving the new id (topic tip-walk onto the fresh child) could
        start a concurrent turn the lease never sees (#64934 rotation-alias
        window). Call at every site that reassigns session_entry.session_id
        mid-turn. Fail-open no-op when there is no held token.
        """
        if not session_key or not new_session_id:
            return False
        registry = getattr(self, '_turn_leases', None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        try:
            return registry.rebind(turn.lease_token, new_session_id)
        except Exception:
            logger.debug('Failed to rebind turn lease', exc_info=True)
            return False

    def _clear_conversation_scope(self, session_key: str, *, reason: str) -> None:
        """Clear ALL conversation-scoped per-session state for ``session_key``.

        THE single conversation-boundary funnel. Call this — and nothing
        else — whenever a session_key crosses a conversation boundary:
        /new, /resume, auto-reset (idle/daily/suspended), expiry
        finalization, and the compression-exhausted auto-reset.

        Why a funnel: these boundaries used to each carry a hand-copied
        pop-list of the per-session dicts, and the lists drifted every time
        a new dict was added (#48031, #58403, #10702, #35809 were all
        "boundary X forgot dict Y" bugs — e.g. /new cleared the /model
        override but not the /model --once restore snapshot). Adding a new
        conversation-scoped dict now means adding its attribute name to
        _CONVERSATION_SCOPED_STATE below; every boundary picks it up
        automatically.

        Scope rules:
        - Conversation-scoped (cleared here): model/reasoning overrides,
          one-turn restore snapshots, pending model notes, last-resolved
          model cache, queued follow-up events, and the boundary security
          state (approvals, /yolo, slash-confirm, update prompts).
        - Turn-scoped (NOT cleared here): _running_agents/_ts, slot leases,
          turn-lease tokens — owned by _release_running_agent_state and the
          dispatch finally.
        - Idle agent-cache eviction is NOT a conversation boundary: the
          session is still alive and a resumed turn rebuilds from these
          overrides. Only true boundaries call this.

        Safe on bare test runners built via ``object.__new__`` (every
        access is getattr-guarded).
        """
        if not session_key:
            return
        state = self._peek_session_state(session_key)
        if state is not None:
            state.conversation.clear()
        for attr in _CONVERSATION_SCOPED_STATE:
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(session_key, None)
        self._clear_session_boundary_security_state(session_key)
        logger.debug('Cleared conversation scope for %s (%s)', session_key, reason)

    def _clear_session_boundary_security_state(self, session_key: str) -> None:
        """Clear per-session control state that must not survive a boundary switch."""
        if not session_key:
            return
        pending_skills_reload_notes = getattr(self, '_pending_skills_reload_notes', None)
        if isinstance(pending_skills_reload_notes, dict):
            pending_skills_reload_notes.pop(session_key, None)
        _sec_state = self._peek_session_state(session_key)
        if _sec_state is not None:
            _sec_state.persistent.approvals = None
            _sec_state.persistent.update_prompt_pending = False
        try:
            from tools import slash_confirm as _slash_confirm_mod
        except Exception:
            _slash_confirm_mod = None
        if _slash_confirm_mod is not None:
            try:
                _slash_confirm_mod.clear(session_key)
            except Exception as e:
                logger.debug('Failed to clear slash-confirm state for session boundary %s: %s', session_key, e)
        try:
            from tools.approval import clear_session as _clear_approval_session
        except Exception:
            return
        try:
            _clear_approval_session(session_key)
        except Exception as e:
            logger.debug('Failed to clear approval state for session boundary %s: %s', session_key, e)

    def _begin_session_run_generation(self, session_key: str) -> int:
        """Claim a fresh run generation token for ``session_key``.

        Every top-level gateway turn gets a monotonically increasing token.
        If a later command like /stop or /new invalidates that token while the
        old worker is still unwinding, the late result can be recognized and
        dropped instead of bleeding into the fresh session.
        """
        if not session_key:
            return 0
        persistent = self._session_state(session_key).persistent
        persistent.run_generation = int(persistent.run_generation) + 1
        return persistent.run_generation

    def _invalidate_session_run_generation(self, session_key: str, *, reason: str='') -> int:
        """Invalidate any in-flight run token for ``session_key``."""
        generation = self._begin_session_run_generation(session_key)
        if reason:
            logger.info('Invalidated run generation for %s → %d (%s)', session_key, generation, reason)
        return generation

    def _is_session_run_current(self, session_key: str, generation: int) -> bool:
        """Return True when ``generation`` is still current for ``session_key``."""
        if not session_key:
            return True
        state = self._peek_session_state(session_key)
        current = state.persistent.run_generation if state is not None else 0
        return int(current) == int(generation)

    def _bind_adapter_run_generation(self, adapter: Any, session_key: str, generation: int | None) -> None:
        """Bind a gateway run generation to the adapter's active-session event."""
        if not adapter or not session_key or generation is None:
            return
        try:
            interrupt_event = getattr(adapter, '_active_sessions', {}).get(session_key)
            if interrupt_event is not None:
                setattr(interrupt_event, '_hermes_run_generation', int(generation))
        except Exception:
            pass

    async def _interrupt_and_clear_session(self, session_key: str, source: SessionSource, *, interrupt_reason: str, invalidation_reason: str, release_running_state: bool=True) -> None:
        """Interrupt the current run and clear queued session state consistently."""
        if not session_key:
            return
        _iac_state = self._peek_session_state(session_key)
        running_agent = _iac_state.turn.agent if _iac_state else None
        _process_task_id = ''
        _process_baseline = None
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            request_hard_interrupt(running_agent, interrupt_reason)
            _process_task_id = getattr(running_agent, '_gateway_turn_process_task_id', '')
            _process_baseline = getattr(running_agent, '_gateway_turn_process_baseline', None)
        _generation_at_interrupt = self._invalidate_session_run_generation(session_key, reason=invalidation_reason)
        if _process_task_id and _process_baseline is not None:
            threading.Thread(target=_reap_gateway_turn_processes, args=(_process_task_id, _process_baseline), kwargs={'source': 'gateway_turn_interrupt', 'is_still_current': lambda: self._is_session_run_current(session_key, _generation_at_interrupt)}, name=f'gateway-turn-reaper-{_process_task_id[:12]}', daemon=True).start()
        adapter = self._adapter_for_source(source)
        interrupt_session_activity = getattr(type(adapter), 'interrupt_session_activity', None)
        if adapter and callable(interrupt_session_activity):
            metadata = self._thread_metadata_for_source(source)
            try:
                params = inspect.signature(interrupt_session_activity).parameters
                accepts_metadata = 'metadata' in params or any((param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()))
            except (TypeError, ValueError):
                accepts_metadata = False
            if accepts_metadata:
                await adapter.interrupt_session_activity(session_key, source.chat_id, metadata=metadata)
            else:
                await adapter.interrupt_session_activity(session_key, source.chat_id)
        if adapter and hasattr(adapter, 'get_pending_message'):
            adapter.get_pending_message(session_key)
        if _iac_state is not None:
            _iac_state.persistent.pending_command_text = None
        if release_running_state:
            self._release_running_agent_state(session_key)
            self._evict_cached_agent(session_key)

    async def _refresh_agent_cache_message_count(self, session_key: str, session_id: Optional[str]) -> None:
        """Re-baseline a cached agent's stored message_count after THIS turn.

        The cross-process coherence guard (#45966) compares the session's
        on-disk ``message_count`` against the count snapshotted next to the
        cached agent, and rebuilds the agent on a mismatch.  But the snapshot
        is taken at agent-BUILD time — before this turn writes its own user +
        assistant (+ tool) rows — and the cache entry is never rewritten on a
        reuse.  So without this re-baseline, THIS process's own turn would
        grow ``message_count`` and the very next turn would see a mismatch
        and rebuild the agent — every turn, for every conversation — silently
        destroying the per-conversation prompt caching the cache exists to
        protect.

        Call this once a turn has completed and the agent has flushed its
        rows to the SessionDB.  It snapshots the now-current count (which
        includes this process's own writes) so the guard only fires when a
        DIFFERENT process changes the transcript out from under us.  The
        ``_sig`` is left untouched; only the count element is refreshed, and
        only when the same agent is still cached (no rebuild/eviction raced
        in between).  Fail-safe: any DB error leaves the snapshot as-is, which
        at worst costs one unnecessary rebuild on the next turn.

        When the cache entry records a ``session_id`` (4-tuple form, #54947)
        that differs from the current ``session_id`` — meaning the cache
        was built for a DIFFERENT conversation under the same ``session_key``
        — the snapshot is intentionally left untouched.  Overwriting it with
        the current session's count would corrupt the original conversation's
        baseline and cause the next switch back to fire the cross-process
        guard spuriously.  Fail-safe: the legacy 3-tuple shape (no
        ``session_id``) is still re-baselined as before.
        """
        if self._session_db is None or not session_id:
            return
        _cache_lock = getattr(self, '_agent_cache_lock', None)
        _cache = getattr(self, '_agent_cache', None)
        if not _cache_lock or _cache is None:
            return
        try:
            _sess_row = await self._session_db.get_session(session_id)
            _live = _sess_row.get('message_count', 0) if _sess_row else None
        except Exception:
            return
        if _live is None:
            return
        with _cache_lock:
            cached = _cache.get(session_key)
            if isinstance(cached, tuple) and len(cached) > 2 and (cached[0] is not _AGENT_PENDING_SENTINEL):
                _snapshot_sid = cached[3] if len(cached) > 3 else None
                if _snapshot_sid is not None and _snapshot_sid != session_id:
                    return
                if cached[2] != _live:
                    if _snapshot_sid is None:
                        _cache[session_key] = (cached[0], cached[1], _live)
                    else:
                        _cache[session_key] = (cached[0], cached[1], _live, _snapshot_sid)

    def _set_pending_turn_sidecar_notes(self, session_key: str, notes: List[str]) -> None:
        """Stage per-turn must-deliver notes for the next agent run (one-shot)."""
        if not session_key or not notes:
            return
        self._session_state(session_key).conversation.sidecar_notes = list(notes)

    def _consume_pending_turn_sidecar_notes(self, session_key: str) -> List[str]:
        if not session_key:
            return []
        state = self._peek_session_state(session_key)
        if state is None:
            return []
        staged = state.conversation.sidecar_notes
        state.conversation.sidecar_notes = []
        return list(staged) if isinstance(staged, list) else []

    def _voice_channel_sidecar_note(self, event, source: SessionSource, session_key: str) -> Optional[str]:
        """Return a ``[Voice channel now: ...]`` note when VC state changed.

        Compares the live Discord voice-channel context against the last
        value delivered for this session and returns a note only on change
        (including leaving the channel).  Unchanged state returns ``None`` so
        the per-turn member/speaking serialization cannot churn the prompt.
        """
        if source.platform != Platform.DISCORD:
            return None
        adapter = self.adapters.get(Platform.DISCORD)
        guild_id = self._get_guild_id(event)
        if not (guild_id and adapter and hasattr(adapter, 'get_voice_channel_context')):
            return None
        try:
            vc_now = adapter.get_voice_channel_context(guild_id) or ''
        except Exception:
            logger.debug('voice-channel context read failed', exc_info=True)
            return None
        vc_prev = None
        if session_key:
            _vc_state = self._session_state(session_key)
            vc_prev = _vc_state.conversation.vc_last
            _vc_state.conversation.vc_last = vc_now
        if vc_now == (vc_prev if vc_prev is not None else ''):
            return None
        if not vc_now:
            return '[Voice channel now: not connected to a voice channel]'
        return f'[Voice channel now: {vc_now}]'

    def _pinned_session_context_prompt(self, context, redact_pii: bool, session_key: Optional[str]) -> str:
        """Return the session-context prompt, pinned per session.

        Key hit → the pinned bytes are reused VERBATIM (immunizes the
        composed system prompt against renderer nondeterminism); key miss →
        re-render ``build_session_context_prompt`` and re-pin (a legitimate
        cache bust: rename, topic edit, /sethome, redact_pii flip, ...).
        """
        _eph_key = self._ephemeral_change_key(context, redact_pii)
        _eph_pin = None
        if session_key:
            _pin_state = self._peek_session_state(session_key)
            _eph_pin = _pin_state.conversation.ephemeral_pin if _pin_state else None
        if _eph_pin is not None and _eph_pin[0] == _eph_key:
            return _eph_pin[1]
        text = build_session_context_prompt(context, redact_pii=redact_pii)
        if session_key:
            self._session_state(session_key).conversation.ephemeral_pin = (_eph_key, text)
        return text

    @staticmethod
    def _ephemeral_change_key(context, redact_pii: bool) -> str:
        """Hash the exact inputs ``build_session_context_prompt`` renders.

        This key decides when the pinned per-session context-prompt bytes are
        reused verbatim vs re-rendered.  The maintained invariant (guarded by
        the parity test in tests/gateway/test_prompt_tail_freeze.py): any
        input whose change alters the rendered bytes MUST appear here —
        omission means a stale pinned prompt (cosmetic staleness); inclusion
        of an extra field only costs a spurious re-render.
        """
        import hashlib
        src = context.source
        platform = src.platform.value if src.platform else ''
        discord_ids: tuple = ()
        discord_tools = ''
        if src.platform == Platform.DISCORD:
            from gateway.session import _discord_tools_loaded
            discord_tools = '1' if _discord_tools_loaded() else '0'
            discord_ids = (str(src.guild_id or ''), str(src.parent_chat_id or ''), str(src.thread_id or ''), str(src.chat_id or ''), '1' if src.message_id else '0')
        slack_tools = ''
        if src.platform == Platform.SLACK:
            from gateway.session import _slack_tools_loaded
            slack_tools = '1' if _slack_tools_loaded() else '0'
        try:
            from hermes_constants import display_hermes_home
            home_display = str(display_hermes_home())
        except Exception:
            home_display = ''
        key_tuple = (platform, str(src.chat_id or ''), str(src.thread_id or ''), str(src.chat_type or ''), str(src.chat_name or ''), str(src.chat_topic or ''), str(src.user_name or ''), str(src.user_id or ''), str(getattr(src, 'profile', None) or ''), bool(context.shared_multi_user_session), discord_ids, discord_tools, slack_tools, tuple((p.value for p in context.connected_platforms)), tuple(((p.value, str(getattr(hc, 'name', '') or ''), str(getattr(hc, 'chat_id', '') or '')) for p, hc in context.home_channels.items())), bool(redact_pii), home_display)
        return hashlib.sha256(repr(key_tuple).encode('utf-8')).hexdigest()

    def _evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session (called on /new, /model, etc).

        Pops the entry AND soft-releases the evicted agent's LLM client
        pool so the httpx connection (sockets + held buffers) is freed
        promptly rather than waiting on CPython GC — AIAgent holds
        reference cycles (callbacks, tool state) that delay refcount
        collection, so a manual release is required to keep gateway RSS
        flat across many /new, /model, undo and reset operations (#29298,
        same leak class as #25315).

        The release is soft (``release_clients()``): it frees the client
        pool and per-turn child subagents but PRESERVES the session's
        terminal sandbox, browser daemon, and tracked bg processes (keyed
        on task_id), because the session may resume with a freshly-built
        agent.  Call sites that want a hard teardown (true conversation
        boundaries like /new) already call ``_cleanup_agent_resources``
        before evicting; ``release_clients`` is idempotent and safe to
        run again after that (the client is already None).

        Cleanup runs on a daemon thread so we never block holding
        ``_agent_cache_lock`` on slow socket teardown — mirrors the
        cap-enforcer and idle-sweeper paths.
        """
        _evict_state = self._peek_session_state(session_key)
        if _evict_state is not None:
            _evict_state.conversation.ephemeral_pin = None
            _evict_state.conversation.vc_last = None
        _lock = getattr(self, '_agent_cache_lock', None)
        evicted = None
        if _lock:
            with _lock:
                evicted = self._agent_cache.pop(session_key, None)
        else:
            _cache = getattr(self, '_agent_cache', None)
            if _cache is not None:
                evicted = _cache.pop(session_key, None)
        agent = evicted[0] if isinstance(evicted, tuple) and evicted else evicted
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return
        running_ids = {id(a) for _, a in self._running_agent_items() if a is not None and a is not _AGENT_PENDING_SENTINEL}
        if id(agent) in running_ids:
            return
        try:
            threading.Thread(target=self._release_evicted_agent_soft, args=(agent,), daemon=True, name=f'agent-evict-{str(session_key)[:24]}').start()
        except Exception:
            try:
                self._release_evicted_agent_soft(agent)
            except Exception:
                pass

    @staticmethod
    def _init_cached_agent_for_turn(agent: Any, interrupt_depth: int) -> None:
        """Reset per-turn state on a cached agent before a new turn starts.

        ``_last_activity_ts``, ``_last_activity_desc``, and
        ``_last_activity_provenance`` are only reset for fresh external
        turns (depth 0); they are a semantic triple - description and
        provenance describe the activity *at* ts, so updating one without
        the others would make get_activity_summary() misleading.
        For interrupt-recursive turns all three are preserved so the
        inactivity watchdog can accumulate stuck-turn idle time and fire
        the 30-min timeout (#15654).  The depth-0 reset is still needed:
        a session idle for 29 min would otherwise trip the watchdog before
        the new turn makes its first API call (#9051).
        """
        if interrupt_depth == 0:
            from agent.session_activity import ActivityProvenance
            agent._last_activity_ts = time.time()
            agent._last_activity_desc = 'starting new turn (cached)'
            agent._last_activity_provenance = ActivityProvenance.UNKNOWN
            if hasattr(agent, '_last_flushed_db_idx'):
                agent._last_flushed_db_idx = 0
        agent._api_call_count = 0

    def _commit_memory_before_soft_evict(self, agent: Any, key: str) -> None:
        """Fire on_session_end extraction before soft-evicting a live agent.

        Soft eviction (``_release_evicted_agent_soft``) deliberately keeps the
        session resumable and does NOT fire ``on_session_end`` — that hook is
        reserved for the true session boundary, tear-down done by
        ``_session_expiry_watcher`` when the session finally expires.

        But the watcher tears down whatever agent it finds in ``_agent_cache``
        at expiry time.  If cache pressure (the LRU cap) soft-evicts a
        finalizable session's agent BEFORE it expires, the watcher later finds
        no cached agent and ``on_session_end`` is silently skipped — memory
        providers never see the transcript (#11205, LRU-cap variant).

        We hold the live, fully-scoped agent right now, so commit its
        end-of-session memory extraction here using the agent's own memory
        manager (correct per-user/chat scoping, no reconstruction).  This uses
        ``commit_memory_session`` — extraction WITHOUT provider teardown — so
        the eviction stays soft and a resumed turn keeps working.

        Only fires for sessions the expiry watcher will eventually finalize
        (finite reset policy).  For ``mode == "none"`` sessions the watcher
        never runs, so there is no missed-boundary to compensate for and we
        skip the commit (the agent is simply released).  Best-effort: any
        failure is swallowed so eviction still proceeds.
        """
        if agent is None or not hasattr(agent, 'commit_memory_session'):
            return
        if getattr(agent, '_memory_manager', None) is None:
            return
        try:
            _store = getattr(self, 'session_store', None)
            if _store is None:
                return
            _store._ensure_loaded()
            entry = _store._entries.get(key)
            if entry is None:
                return
            if not _store.is_session_finalizable(entry):
                return
            if _store._is_session_expired(entry):
                return
            messages = getattr(agent, '_session_messages', None)
            agent.commit_memory_session(messages if isinstance(messages, list) else None)
            logger.debug('Committed on_session_end extraction before soft-evicting finalizable session=%s (cache pressure, pre-expiry)', key)
        except Exception as _e:
            logger.debug('Pre-evict memory commit failed for %s: %s', key, _e)

    def _commit_then_release_soft(self, agent: Any, key: str) -> None:
        """Commit end-of-session memory (if warranted), then soft-release.

        Runs on the daemon eviction thread so the memory-provider call and the
        client teardown never block the caller's held cache lock. Order matters:
        commit uses the live agent's memory manager before ``release_clients``
        drops the message buffer.
        """
        self._commit_memory_before_soft_evict(agent, key)
        self._release_evicted_agent_soft(agent)

    def _release_evicted_agent_soft(self, agent: Any) -> None:
        """Soft cleanup for cache-evicted agents — preserves session tool state.

        Called from _enforce_agent_cache_cap and _sweep_idle_cached_agents.
        Distinct from _cleanup_agent_resources (full teardown) because a
        cache-evicted session may resume at any time — its terminal
        sandbox, browser daemon, and tracked bg processes must outlive
        the Python AIAgent instance so the next agent built for the
        same task_id inherits them.
        """
        if agent is None:
            return
        try:
            if hasattr(agent, 'release_clients'):
                agent.release_clients()
            else:
                self._cleanup_agent_resources(agent)
        except Exception:
            pass
        if hasattr(agent, '_session_messages'):
            agent._session_messages = []
        if hasattr(agent, '_db_flush_scan_prefix'):
            agent._db_flush_scan_prefix = None

    def _agent_cache_bounds(self):
        """Operator-configured agent-cache bounds, resolved once per process.

        Resolved lazily rather than in ``__init__`` so it also works for the
        ``__new__``-constructed runners used by tests and by the slash-command
        mixin.
        """
        bounds = getattr(self, '_agent_cache_bounds_cache', None)
        if bounds is None:
            from gateway.agent_cache_pressure import resolve_agent_cache_bounds
            try:
                bounds = resolve_agent_cache_bounds(_load_gateway_config())
            except Exception as _e:
                logger.debug('Agent cache bounds config read failed: %s', _e)
                bounds = resolve_agent_cache_bounds({})
            self._agent_cache_bounds_cache = bounds
        return bounds

    def _agent_cache_cap(self) -> int:
        """Effective LRU cap — the configured override, else the default."""
        configured = self._agent_cache_bounds().max_size
        return configured if configured else _AGENT_CACHE_MAX_SIZE

    def _agent_cache_idle_ttl(self) -> float:
        """Effective idle TTL in seconds — configured override, else default."""
        configured = self._agent_cache_bounds().idle_ttl_secs
        return configured if configured else _AGENT_CACHE_IDLE_TTL_SECS

    def _sweep_agent_cache_under_pressure(self) -> int:
        """Shed cached transcripts once the gateway's own heap nears its budget.

        The LRU cap counts entries and the idle sweep counts seconds; neither
        knows that one cached agent pins a full ``_session_messages``
        transcript — tens of MB on a session with 100+ tool calls.  A gateway
        serving many chats therefore holds every warm transcript indefinitely:
        agents that took a turn within the TTL are never idle-swept, and the
        sweep additionally defers finalizable sessions until they expire.  RSS
        climbs until the cgroup throttles and SIGTERM can no longer flush
        inside systemd's stop timeout (#80764).

        This is the missing valve.  Above the configured anonymous-RSS budget
        it evicts LRU agents through the same soft path the cap enforcer uses,
        so the transcript is dropped and rebuilt from the persisted session on
        the next turn.  Three things are never touched: agents mid-turn (their
        clients and sandboxes are in use), the most recently used sessions
        (whose prompt cache is worth the most), and any session whose live
        transcript has not finished reaching disk.

        Returns the number of entries evicted (0 when memory is fine).
        """
        from gateway.agent_cache_pressure import plan_pressure_evictions, read_anon_rss_mb, transcript_persistence_caught_up
        bounds = self._agent_cache_bounds()
        if not bounds.memory_high_mb:
            return 0
        _cache = getattr(self, '_agent_cache', None)
        _lock = getattr(self, '_agent_cache_lock', None)
        if not _cache or _lock is None:
            return 0
        rss_mb = read_anon_rss_mb()
        if rss_mb is None or rss_mb < bounds.memory_high_mb:
            return 0
        running_ids = {id(a) for _, a in self._running_agent_items() if a is not None and a is not _AGENT_PENDING_SENTINEL}

        def _is_evictable(key: str, agent: Any) -> bool:
            if agent is None or agent is _AGENT_PENDING_SENTINEL:
                return False
            if id(agent) in running_ids:
                return False
            return transcript_persistence_caught_up(agent)
        with _lock:
            ordered = [(key, entry[0] if isinstance(entry, tuple) and entry else entry) for key, entry in _cache.items()]
            plan = plan_pressure_evictions(ordered, is_evictable=_is_evictable, max_evictions=bounds.max_evictions_per_pass, protect_recent=bounds.protect_recent)
            for key, _ in plan:
                _cache.pop(key, None)
        if not plan:
            _mid_turn = sum((1 for _, a in ordered if a is not None and id(a) in running_ids))
            _unflushed = sum((1 for _, a in ordered if a is not None and a is not _AGENT_PENDING_SENTINEL and (id(a) not in running_ids) and (not transcript_persistence_caught_up(a))))
            logger.warning('Agent cache pressure: anon RSS %dMB over budget %dMB but no evictable session (%d cached, %d mid-turn, %d blocked on un-flushed persistence)%s', rss_mb, bounds.memory_high_mb, len(ordered), _mid_turn, _unflushed, ' — transcripts are not reaching the session DB (session persistence disabled or failing?); the memory valve cannot shed sessions until they persist.' if _unflushed and (not _mid_turn) else ' — memory will keep climbing until those turns finish.')
            return 0
        evicted_count = len(plan)
        logger.warning('Agent cache pressure: anon RSS %dMB over budget %dMB — evicting %d LRU session(s): %s', rss_mb, bounds.memory_high_mb, evicted_count, ', '.join((key for key, _ in plan)))
        try:
            threading.Thread(target=self._release_pressure_batch, args=(plan,), daemon=True, name='agent-cache-pressure').start()
        except Exception:
            self._release_pressure_batch(plan)
        return evicted_count

    def _release_pressure_batch(self, plan: List[tuple]) -> None:
        """Release a pressure-evicted batch, then return the heap to the OS.

        Sequential on one daemon thread rather than a thread per agent: the
        batch is already capped, and the point of the pass is to reclaim
        memory, not to race N teardowns. The trailing ``malloc_trim`` is what
        turns "Python dropped the transcript" into "RSS actually fell" —
        without it glibc keeps the freed arenas and the cgroup never notices.

        The plan is drained (``pop`` + ``del``) rather than iterated so that
        no local reference pins the evicted agents when ``gc.collect`` +
        ``malloc_trim`` run — otherwise the trim frees almost nothing in this
        pass, the next tick re-reads a still-high RSS, and the valve
        over-evicts an extra batch of warm prompt caches every cycle.
        """
        while plan:
            key, agent = plan.pop(0)
            try:
                self._commit_then_release_soft(agent, key)
            except Exception as _e:
                logger.debug('Pressure release failed for %s: %s', key, _e)
            del agent
        try:
            from hermes_cli.mem_trim import trim_memory
            trim_memory(force=True, reason='agent_cache_pressure')
        except Exception:
            pass

    def _enforce_agent_cache_cap(self) -> None:
        """Evict oldest cached agents when cache exceeds the LRU cap.

        Must be called with _agent_cache_lock held.  Resource cleanup
        (memory provider shutdown, tool resource close) is scheduled
        on a daemon thread so the caller doesn't block on slow teardown
        while holding the cache lock.

        Agents currently in _running_agents are SKIPPED — their clients,
        terminal sandboxes, background processes, and child subagents
        are all in active use by the running turn.  Evicting them would
        tear down those resources mid-turn and crash the request.  If
        every candidate in the LRU order is active, we simply leave the
        cache over the cap; it will be re-checked on the next insert.
        """
        _cache = getattr(self, '_agent_cache', None)
        if _cache is None:
            return
        if not hasattr(_cache, 'move_to_end'):
            return
        running_ids = {id(a) for _, a in self._running_agent_items() if a is not None and a is not _AGENT_PENDING_SENTINEL}
        cap = self._agent_cache_cap()
        excess = max(0, len(_cache) - cap)
        evict_plan: List[tuple] = []
        if excess > 0:
            ordered_keys = list(_cache.keys())
            for key in ordered_keys[:excess]:
                entry = _cache.get(key)
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is not None and id(agent) in running_ids:
                    continue
                evict_plan.append((key, agent))
        for key, _ in evict_plan:
            _cache.pop(key, None)
        remaining_over_cap = len(_cache) - cap
        if remaining_over_cap > 0:
            logger.warning('Agent cache over cap (%d > %d); %d excess slot(s) held by mid-turn agents — will re-check on next insert.', len(_cache), cap, remaining_over_cap)
        for key, agent in evict_plan:
            logger.info('Agent cache at cap; evicting LRU session=%s (cache_size=%d)', key, len(_cache))
            if agent is not None:
                threading.Thread(target=self._commit_then_release_soft, args=(agent, key), daemon=True, name=f'agent-cache-evict-{key[:24]}').start()

    def _sweep_idle_cached_agents(self) -> int:
        """Evict cached agents whose AIAgent has been idle past the idle TTL.

        Safe to call from the session expiry watcher without holding the
        cache lock — acquires it internally.  Returns the number of entries
        evicted.  Resource cleanup is scheduled on daemon threads.

        Agents currently in _running_agents are SKIPPED for the same reason
        as _enforce_agent_cache_cap: tearing down an active turn's clients
        mid-flight would crash the request.
        """
        _cache = getattr(self, '_agent_cache', None)
        _lock = getattr(self, '_agent_cache_lock', None)
        if _cache is None or _lock is None:
            return 0
        now = time.time()
        idle_ttl = self._agent_cache_idle_ttl()
        to_evict: List[tuple] = []
        running_ids = {id(a) for _, a in self._running_agent_items() if a is not None and a is not _AGENT_PENDING_SENTINEL}
        with _lock:
            for key, entry in list(_cache.items()):
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is None:
                    continue
                if id(agent) in running_ids:
                    continue
                last_activity = getattr(agent, '_last_activity_ts', None)
                if last_activity is None:
                    continue
                if now - last_activity > idle_ttl:
                    session_entry = None
                    _store = getattr(self, 'session_store', None)
                    try:
                        if _store is not None:
                            _store._ensure_loaded()
                            session_entry = _store._entries.get(key)
                    except Exception:
                        session_entry = None
                    if session_entry is not None and _store is not None and _store.is_session_finalizable(session_entry) and (not _store._is_session_expired(session_entry)):
                        continue
                    to_evict.append((key, agent))
            for key, _ in to_evict:
                _cache.pop(key, None)
        for key, agent in to_evict:
            logger.info('Agent cache idle-TTL evict: session=%s (idle=%.0fs)', key, now - getattr(agent, '_last_activity_ts', now))
            threading.Thread(target=self._release_evicted_agent_soft, args=(agent,), daemon=True, name=f'agent-cache-idle-{key[:24]}').start()
        return len(to_evict)

    def _get_proxy_url(self) -> Optional[str]:
        """Return the proxy URL if proxy mode is configured, else None.

        Checks GATEWAY_PROXY_URL env var first (convenient for Docker),
        then ``gateway.proxy_url`` in config.yaml.
        """
        url = os.getenv('GATEWAY_PROXY_URL', '').strip()
        if url:
            return url.rstrip('/')
        cfg = _load_gateway_config()
        url = (cfg.get('gateway') or {}).get('proxy_url')
        url = (url or '').strip()
        if url:
            return url.rstrip('/')
        return None

    def _build_stream_consumer_config(self, source: 'SessionSource', scfg: Any, adapter: Any, *, on_missing_cursor: str) -> 'tuple[Any, Optional[Callable[[], None]]]':
        """Build the shared ``StreamConsumerConfig`` and the optional
        Telegram pause-typing closure used by both agent-run paths.

        ``on_missing_cursor`` controls how platforms whose adapter sets
        ``SUPPORTS_MESSAGE_EDITING = False`` are handled — both semantics
        are preserved verbatim from the pre-refactor call sites:

        - ``"fallback"`` (proxy path): stream anyway with an empty cursor.
        - ``"raise"`` (in-process agent path): raise ``RuntimeError`` so
          the caller's ``except`` skips streaming entirely.

        Returns ``(consumer_cfg, pause_typing_before_finalize)``.
        """
        from gateway.stream_consumer import StreamConsumerConfig
        _pause_typing_before_finalize = None
        if source.platform == Platform.TELEGRAM and hasattr(adapter, 'pause_typing_for_chat'):

            def _pause_typing_before_finalize(_adapter=adapter, _chat_id=source.chat_id) -> None:
                _adapter.pause_typing_for_chat(_chat_id)
        _adapter_supports_edit = getattr(adapter, 'SUPPORTS_MESSAGE_EDITING', True)
        if not _adapter_supports_edit and on_missing_cursor == 'raise':
            raise RuntimeError('skip streaming for non-editable platform')
        _effective_cursor = scfg.cursor if _adapter_supports_edit else ''
        _buffer_only = False
        if source.platform == Platform.MATRIX:
            _effective_cursor = ''
            _buffer_only = True
        _fresh_final_secs = float(getattr(scfg, 'fresh_final_after_seconds', 0.0) or 0.0) if source.platform == Platform.TELEGRAM else 0.0
        _consumer_cfg = StreamConsumerConfig(edit_interval=scfg.edit_interval, buffer_threshold=scfg.buffer_threshold, cursor=_effective_cursor, buffer_only=_buffer_only, fresh_final_after_seconds=_fresh_final_secs, transport=scfg.transport or 'edit', chat_type=getattr(source, 'chat_type', '') or '')
        return (_consumer_cfg, _pause_typing_before_finalize)

    async def _run_agent_via_proxy(self, message: str, context_prompt: str, history: List[Dict[str, Any]], source: 'SessionSource', session_id: str, session_key: str=None, run_generation: Optional[int]=None, event_message_id: Optional[str]=None) -> Dict[str, Any]:
        """Forward the message to a remote Duck Agent API server instead of
        running a local AIAgent.

        When ``GATEWAY_PROXY_URL`` (or ``gateway.proxy_url`` in config.yaml)
        is set, the gateway becomes a thin relay: it handles platform I/O
        (encryption, threading, media) and delegates all agent work to the
        remote server via ``POST /v1/chat/completions`` with SSE streaming.

        This lets a Docker container handle Matrix E2EE while the actual
        agent runs on the host with full access to local files, memory,
        skills, and a unified session store.
        """
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return {'final_response': '⚠️ Proxy mode requires aiohttp. Install with: pip install aiohttp', 'messages': [], 'api_calls': 0, 'tools': []}
        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return {'final_response': '⚠️ Proxy URL not configured (GATEWAY_PROXY_URL or gateway.proxy_url)', 'messages': [], 'api_calls': 0, 'tools': []}
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret
            try:
                proxy_key = (get_secret('GATEWAY_PROXY_KEY') or '').strip()
            except UnscopedSecretError:
                proxy_key = os.getenv('GATEWAY_PROXY_KEY', '').strip()
        except Exception:
            proxy_key = os.getenv('GATEWAY_PROXY_KEY', '').strip()

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)
        api_messages: List[Dict[str, str]] = []
        if context_prompt:
            api_messages.append({'role': 'system', 'content': context_prompt})
        for msg in history:
            role = msg.get('role')
            content = msg.get('content')
            if role in {'user', 'assistant'} and content:
                api_messages.append({'role': role, 'content': content})
        api_messages.append({'role': 'user', 'content': message})
        headers: Dict[str, str] = {'Content-Type': 'application/json'}
        if proxy_key:
            headers['Authorization'] = f'Bearer {proxy_key}'
        if session_id:
            headers['X-Duck Agent-Session-Id'] = session_id
        body = {'model': 'duck-agent', 'messages': api_messages, 'stream': True}
        _stream_consumer = None
        _scfg = getattr(getattr(self, 'config', None), 'streaming', None)
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()
        platform_key = _platform_config_key(source.platform)
        user_config = _load_gateway_config()
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(user_config, platform_key, 'streaming')
        _streaming_enabled = _scfg.enabled and _scfg.transport != 'off' if _plat_streaming is None else bool(_plat_streaming)
        _thread_metadata: Optional[Dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)
        if _streaming_enabled:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                _adapter = self._adapter_for_source(source)
                if _adapter:
                    _consumer_cfg, _pause_typing_before_finalize = self._build_stream_consumer_config(source, _scfg, _adapter, on_missing_cursor='fallback')
                    _stream_consumer = GatewayStreamConsumer(adapter=_adapter, chat_id=source.chat_id, config=_consumer_cfg, metadata=_thread_metadata, on_before_finalize=_pause_typing_before_finalize, initial_reply_to_id=event_message_id, run_still_current=_run_still_current)
            except Exception as _sc_err:
                logger.debug('Proxy: could not set up stream consumer: %s', _sc_err)
        stream_task = None
        if _stream_consumer:
            stream_task = asyncio.create_task(_stream_consumer.run())
        _adapter = self._adapter_for_source(source)
        if _adapter:
            try:
                await _adapter.send_typing(source.chat_id, metadata=_thread_metadata)
            except Exception:
                pass
        full_response = ''
        _start = time.time()
        try:
            _timeout = ClientTimeout(total=0, sock_read=1800)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(f'{proxy_url}/v1/chat/completions', json=body, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning('Proxy error (%d) from %s: %s', resp.status, proxy_url, error_text[:500])
                        return {'final_response': f'⚠️ Proxy error ({resp.status}): {error_text[:300]}', 'messages': [], 'api_calls': 0, 'tools': []}
                    buffer = ''
                    async for chunk in resp.content.iter_any():
                        if not _run_still_current():
                            logger.info('Discarding stale proxy stream for %s — generation %d is no longer current', session_key or '?', run_generation or 0)
                            return {'final_response': '', 'messages': [], 'api_calls': 0, 'tools': [], 'history_offset': len(history), 'session_id': session_id, 'response_previewed': False}
                        text = chunk.decode('utf-8', errors='replace')
                        buffer += text
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith('data: '):
                                data = line[6:]
                                if data.strip() == '[DONE]':
                                    break
                                try:
                                    obj = json.loads(data)
                                    choices = obj.get('choices', [])
                                    if choices:
                                        delta = choices[0].get('delta', {})
                                        content = delta.get('content', '')
                                        if content:
                                            full_response += content
                                            if _stream_consumer:
                                                _stream_consumer.on_delta(content)
                                except json.JSONDecodeError:
                                    pass
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError('Proxy SSE stream exceeded max buffer size without a line boundary')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error('Proxy connection error to %s: %s', proxy_url, e)
            if not full_response:
                return {'final_response': f'⚠️ Proxy connection error: {e}', 'messages': [], 'api_calls': 0, 'tools': []}
        finally:
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()
        _elapsed = time.time() - _start
        if not _run_still_current():
            logger.info('Discarding stale proxy result for %s — generation %d is no longer current', session_key or '?', run_generation or 0)
            return {'final_response': '', 'messages': [], 'api_calls': 0, 'tools': [], 'history_offset': len(history), 'session_id': session_id, 'response_previewed': False}
        logger.info('proxy response: url=%s session=%s time=%.1fs response=%d chars', proxy_url, (session_id or '')[:20], _elapsed, len(full_response))
        return {'final_response': full_response or '(No response from remote agent)', 'messages': [{'role': 'user', 'content': message}, {'role': 'assistant', 'content': full_response}], 'api_calls': 1, 'tools': [], 'history_offset': len(history), 'session_id': session_id, 'response_previewed': _stream_consumer is not None and bool(full_response)}

    async def _run_agent(self, message: str, context_prompt: str, history: List[Dict[str, Any]], source: SessionSource, session_id: str, session_key: str=None, run_generation: Optional[int]=None, _interrupt_depth: int=0, event_message_id: Optional[str]=None, channel_prompt: Optional[str]=None, moa_config: Optional[dict]=None, persist_user_message: Optional[Any]=None, persist_user_timestamp: Optional[float]=None, message_type: Optional[str]=None) -> Dict[str, Any]:
        """Profile-scoping wrapper around the agent run.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole turn inside ``_profile_runtime_scope`` so config/skills/
        memory resolve to that profile's home AND credentials resolve from that
        profile's secret scope (never the process-global ``os.environ``). When
        multiplexing is off this is a transparent pass-through — zero behavior
        change for single-profile gateways.
        """
        if not getattr(getattr(self, 'config', None), 'multiplex_profiles', False):
            return await self._run_agent_inner(message, context_prompt, history, source, session_id, session_key=session_key, run_generation=run_generation, _interrupt_depth=_interrupt_depth, event_message_id=event_message_id, channel_prompt=channel_prompt, moa_config=moa_config, persist_user_message=persist_user_message, persist_user_timestamp=persist_user_timestamp, message_type=message_type)
        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_agent_inner(message, context_prompt, history, source, session_id, session_key=session_key, run_generation=run_generation, _interrupt_depth=_interrupt_depth, event_message_id=event_message_id, channel_prompt=channel_prompt, moa_config=moa_config, persist_user_message=persist_user_message, persist_user_timestamp=persist_user_timestamp, message_type=message_type)

    def _profile_name_for_source(self, source: SessionSource) -> Optional[str]:
        """Resolve the profile name for an inbound source via configured routes.

        Returns ``None`` when multiplexing is off, no routes are configured, or
        no route matches. Callers (``build_source``,
        ``_resolve_profile_home_for_source``) treat ``None`` as "use the
        default/active profile". When ``gateway.profile_routes`` is configured,
        the most specific matching route wins (guild < channel < thread). See
        :mod:`gateway.profile_routing` for matching rules.

        Gated on ``gateway.multiplex_profiles``: routing stamps
        ``source.profile``, which selects the session-key namespace and batch
        keys — but the profile-scoped agent run only activates under
        multiplexing. Without this gate, a configured route with multiplexing
        off would namespace batch/session keys by profile while the agent
        still runs in ``agent:main``, splitting the two out of agreement.
        """
        config = getattr(self, 'config', None)
        if not getattr(config, 'multiplex_profiles', False):
            return None
        routes = getattr(config, 'profile_routes', None)
        if not routes:
            return None
        from gateway.profile_routing import match_profile_route
        try:
            matched = match_profile_route(routes, platform=source.platform.value, guild_id=getattr(source, 'guild_id', None), chat_id=source.chat_id, thread_id=getattr(source, 'thread_id', None), parent_chat_id=getattr(source, 'parent_chat_id', None))
        except Exception:
            logger.warning('Profile route matching failed for %s/%s, falling back to default', source.platform, source.chat_id, exc_info=True)
            return None
        if matched:
            return matched.profile
        logger.debug('No profile route matched: platform=%s chat_id=%s thread_id=%s parent_chat_id=%s', source.platform.value, source.chat_id, getattr(source, 'thread_id', None), getattr(source, 'parent_chat_id', None))
        return None

    def _resolve_profile_home_for_source(self, source: SessionSource) -> 'Path':
        """Resolve which profile's DUCK_AGENT_HOME should serve this inbound source.

        Resolution order:
          1. ``source.profile`` — set by /p/<profile>/ URL prefix, per-credential
             adapter ownership, OR profile_routes matching at ``build_source`` time.
          2. ``_profile_name_for_source`` — re-run routing here as a defensive
             fallback for sources that bypass ``build_source``.
          3. The active profile (the multiplexer's own home).
        """
        from hermes_cli.profiles import get_active_profile_name, get_profile_dir, profile_exists
        from hermes_constants import get_hermes_home
        explicit_profile = None
        try:
            name = (source.profile or '').strip()
            if name:
                explicit_profile = name
            if not name:
                name = self._profile_name_for_source(source)
                if name:
                    explicit_profile = name
            if not name:
                name = get_active_profile_name() or 'default'
            profile_dir = get_profile_dir(name)
            if explicit_profile and (not profile_exists(name)):
                logger.warning('Profile %r does not exist for source %s/%s (guild_id=%s), falling back to global DUCK_AGENT_HOME', explicit_profile, source.platform.value, source.chat_id, getattr(source, 'guild_id', None))
                return get_hermes_home()
            return profile_dir
        except Exception:
            logger.warning('Failed to resolve profile directory for source %s/%s (guild_id=%s), falling back to global DUCK_AGENT_HOME: %s', source.platform.value, source.chat_id, getattr(source, 'guild_id', None), explicit_profile or '(no profile)', exc_info=True)
            return get_hermes_home()

    async def _run_agent_inner(self, message: str, context_prompt: str, history: List[Dict[str, Any]], source: SessionSource, session_id: str, session_key: str=None, run_generation: Optional[int]=None, _interrupt_depth: int=0, event_message_id: Optional[str]=None, channel_prompt: Optional[str]=None, moa_config: Optional[dict]=None, persist_user_message: Optional[Any]=None, persist_user_timestamp: Optional[float]=None, message_type: Optional[str]=None) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.
        
        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool
        
        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        if self._get_proxy_url():
            return await self._run_agent_via_proxy(message=message, context_prompt=context_prompt, history=history, source=source, session_id=session_id, session_key=session_key, run_generation=run_generation, event_message_id=event_message_id)
        from run_agent import AIAgent
        import queue

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)
        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)
        from hermes_cli.tools_config import _get_platform_tools
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        agent_cfg_local = user_config.get('agent') or {}
        disabled_toolsets = agent_cfg_local.get('disabled_toolsets') or None
        display_config = user_config.get('display', {})
        if not isinstance(display_config, dict):
            display_config = {}
        from gateway.display_config import resolve_display_setting
        try:
            from agent.display import set_tool_preview_max_len
            _tpl = resolve_display_setting(user_config, platform_key, 'tool_preview_length', 0)
            set_tool_preview_max_len(int(_tpl) if _tpl else 0)
        except Exception:
            pass
        try:
            from agent.display import set_friendly_tool_labels
            _ftl = resolve_display_setting(user_config, platform_key, 'friendly_tool_labels', True)
            set_friendly_tool_labels(bool(_ftl))
        except Exception:
            pass
        _resolved_tp = resolve_display_setting(user_config, platform_key, 'tool_progress')
        _env_tp = os.getenv('HERMES_TOOL_PROGRESS_MODE')
        _display_cfg = display_config if isinstance(display_config, dict) else {}
        _platforms_cfg = _display_cfg.get('platforms') or {}
        _platform_cfg = _platforms_cfg.get(platform_key) or {}
        _legacy_tp_overrides = _display_cfg.get('tool_progress_overrides') or {}
        _tool_progress_configured = 'tool_progress' in _display_cfg or (isinstance(_platform_cfg, dict) and 'tool_progress' in _platform_cfg) or (isinstance(_legacy_tp_overrides, dict) and platform_key in _legacy_tp_overrides)
        progress_mode = _env_tp if _env_tp and (not _tool_progress_configured) else _resolved_tp or _env_tp or 'all'
        progress_grouping = resolve_display_setting(user_config, platform_key, 'tool_progress_grouping') or 'accumulate'
        from gateway.status_phrases import choose_status_phrase, resolve_status_phrase_catalog
        _generic_status_recent: List[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(setting: str, *, default: bool=False, require_platform_override_for: set[Any] | None=None, allow_generic: bool=False) -> str:
            """Return off|raw|generic for a gateway visibility surface."""
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {_gateway_platform_value(item) for item in require_platform_override_for}
                if current_platform in platform_only and (not _has_platform_display_override(user_config, platform_key, setting)):
                    return 'off'
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str) and value.strip().lower() == 'generic':
                return 'generic' if allow_generic else 'off'
            return 'raw' if bool(value) else 'off'

        def _generic_status_phrase(kind: str, *, tool_name: str | None=None, preview: str | None=None, args: Any=None) -> str:
            try:
                return choose_status_phrase(kind, tool_name=tool_name, preview=preview, args=args, recent=_generic_status_recent, catalog=_generic_status_catalog)
            except Exception as _phrase_err:
                logger.debug('generic status phrase selection failed: %s', _phrase_err)
                return 'still on it' if kind in {'heartbeat', 'waiting', 'long_running', 'status'} else 'one sec'
        from gateway.config import Platform
        tool_progress_enabled = progress_mode not in {'off', 'log'} and source.platform != Platform.WEBHOOK
        _live_status_mode = resolve_display_setting(user_config, platform_key, 'live_status', 'full')
        _live_status_adapter = self._adapter_for_source(source)
        if not getattr(_live_status_adapter, 'supports_status_text', False):
            _live_status_adapter = None
        if _live_status_mode == 'off':
            _live_status_adapter = None
        log_mode_enabled = progress_mode == 'log' and source.platform != Platform.WEBHOOK
        log_queue: 'queue.Queue | None' = queue.Queue() if log_mode_enabled else None
        interim_assistant_messages_mode = _display_surface_mode('interim_assistant_messages', default=True, require_platform_override_for={Platform.MATTERMOST})
        interim_assistant_messages_enabled = source.platform != Platform.WEBHOOK and interim_assistant_messages_mode != 'off'
        _thinking_mode = _display_surface_mode('thinking_progress', default=False, require_platform_override_for={Platform.MATTERMOST})
        _thinking_enabled = _thinking_mode != 'off'
        needs_progress_queue = tool_progress_enabled or _thinking_enabled
        progress_queue = queue.Queue() if needs_progress_queue else None
        last_tool = [None]
        last_progress_msg = [None]
        repeat_count = [0]
        last_was_terminal_block = [False]
        _voice_ack_fired = [False]
        _voice_ack_guild: List[Optional[int]] = [None]
        if source.platform == Platform.DISCORD:
            _va = self.adapters.get(Platform.DISCORD)
            _vtc = getattr(_va, '_voice_text_channels', None)
            if isinstance(_vtc, dict) and hasattr(_va, 'voice_mixer_active'):
                for _gid, _tc in _vtc.items():
                    if str(_tc) == str(source.chat_id) and _va.voice_mixer_active(_gid):
                        _voice_ack_guild[0] = _gid
                        break
        _voice_ack_loop = asyncio.get_running_loop()
        _cleanup_progress = bool(resolve_display_setting(user_config, platform_key, 'cleanup_progress'))
        _cleanup_adapter = self._adapter_for_source(source) if _cleanup_progress else None
        _cleanup_delete = getattr(type(_cleanup_adapter), 'delete_message', None) if _cleanup_adapter is not None else None
        if _cleanup_adapter is not None and (_cleanup_delete is None or _cleanup_delete is BasePlatformAdapter.delete_message):
            _cleanup_progress = False
            _cleanup_adapter = None
        _cleanup_msg_ids: List[str] = []
        long_tool_hint_fired = [False]
        _LONG_TOOL_THRESHOLD_S = 30.0
        turn_ctx = TurnContext(source=source, _run_still_current=_run_still_current, _live_status_adapter=_live_status_adapter, _live_status_mode=_live_status_mode, _thinking_enabled=_thinking_enabled, progress_mode=progress_mode, progress_grouping=progress_grouping, tool_progress_enabled=tool_progress_enabled, progress_queue=progress_queue, log_queue=log_queue, last_progress_msg=last_progress_msg, last_tool=last_tool, last_was_terminal_block=last_was_terminal_block, repeat_count=repeat_count, long_tool_hint_fired=long_tool_hint_fired, _LONG_TOOL_THRESHOLD_S=_LONG_TOOL_THRESHOLD_S, _cleanup_progress=_cleanup_progress, _cleanup_msg_ids=_cleanup_msg_ids, message=message, AIAgent=AIAgent, resolve_display_setting=resolve_display_setting, user_config=user_config, enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets, log_mode_enabled=log_mode_enabled, interim_assistant_messages_enabled=interim_assistant_messages_enabled, needs_progress_queue=needs_progress_queue, _voice_ack_fired=_voice_ack_fired, _voice_ack_guild=_voice_ack_guild, _voice_ack_loop=_voice_ack_loop, history=history, context_prompt=context_prompt, channel_prompt=channel_prompt, session_id=session_id, session_key=session_key, run_generation=run_generation, _interrupt_depth=_interrupt_depth, event_message_id=event_message_id, moa_config=moa_config, persist_user_message=persist_user_message, persist_user_timestamp=persist_user_timestamp)
        turn_runner = TurnRunner(self, turn_ctx)
        turn_ctx.progress_callback = turn_runner.progress_callback
        turn_ctx.voice_ack_callback = turn_runner.voice_ack_callback
        _progress_reply_in_thread = True
        if source.platform == Platform.SLACK:
            _slack_adapter_for_progress = self._adapter_for_source(source)
            if _slack_adapter_for_progress is not None:
                try:
                    _mode_fn = getattr(_slack_adapter_for_progress, '_effective_reply_in_thread', None)
                    if callable(_mode_fn):
                        _progress_reply_in_thread = bool(_mode_fn())
                    else:
                        _progress_reply_in_thread = bool(_slack_adapter_for_progress.config.extra.get('reply_in_thread', True))
                except Exception:
                    _progress_reply_in_thread = True
        _progress_thread_id = _resolve_progress_thread_id(source.platform, source.thread_id, event_message_id, reply_in_thread=_progress_reply_in_thread)
        _relay_prospective_thread_id = str(getattr(source, 'prospective_thread_id', None)) if source.platform == Platform.DISCORD and getattr(source, 'delivered_via_upstream_relay', False) and getattr(source, 'prospective_thread_id', None) and (not source.thread_id) else None
        _progress_metadata = (self._thread_metadata_for_source(source, event_message_id) if _progress_thread_id == source.thread_id else self._thread_metadata_for_target(source.platform, source.chat_id, _progress_thread_id, chat_type=getattr(source, 'chat_type', None), reply_to_message_id=event_message_id)) if _progress_thread_id else None
        if _progress_metadata is None and _relay_prospective_thread_id:
            _progress_metadata = {'reply_to_message_id': event_message_id}
        _progress_metadata = _non_conversational_metadata(_progress_metadata, platform=source.platform)
        _progress_reply_to = event_message_id if source.platform in (Platform.FEISHU, Platform.MATTERMOST) and source.thread_id and event_message_id or _relay_prospective_thread_id else None

        async def write_tool_log():
            """Drain log_queue and append tool-call lines to tool_calls.log.

            Only active when ``display.tool_progress`` is ``log``. Uses a
            RotatingFileHandler (5MB × 3 backups) so the audit log can't grow
            unbounded, and the shared RedactingFormatter so secrets never land
            on disk.
            """
            if log_queue is None:
                return
            from logging.handlers import RotatingFileHandler
            from agent.redact import RedactingFormatter
            log_dir = _hermes_home / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_dir / 'tool_calls.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            file_handler.setFormatter(RedactingFormatter('%(message)s'))
            tool_logger = logging.getLogger(f'duck-agent.tool_calls.{id(log_queue)}')
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(file_handler)
            try:
                while True:
                    try:
                        tool_logger.info('%s', log_queue.get_nowait())
                    except queue.Empty:
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error('write_tool_log error: %s', e)
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                while True:
                    try:
                        tool_logger.info('%s', log_queue.get_nowait())
                    except queue.Empty:
                        break
                    except Exception:
                        break
                tool_logger.removeHandler(file_handler)
                try:
                    file_handler.flush()
                    file_handler.close()
                except Exception:
                    pass
        turn_ctx._progress_metadata = _progress_metadata
        turn_ctx._progress_reply_to = _progress_reply_to
        send_progress_messages = turn_runner.send_progress_messages
        agent_holder = [None]
        turn_ctx.agent_holder = agent_holder
        result_holder = [None]
        tools_holder = [None]
        stream_consumer_holder = [None]
        streaming_tts_consumer_holder: list = [None]
        turn_ctx.result_holder = result_holder
        turn_ctx.tools_holder = tools_holder
        turn_ctx.stream_consumer_holder = stream_consumer_holder
        turn_ctx.streaming_tts_consumer_holder = streaming_tts_consumer_holder
        _loop_for_step = asyncio.get_running_loop()
        _hooks_ref = self.hooks
        turn_ctx._loop_for_step = _loop_for_step
        turn_ctx._hooks_ref = _hooks_ref
        turn_ctx._step_callback_sync = turn_runner._step_callback_sync
        turn_ctx._event_callback_sync = turn_runner._event_callback_sync
        _status_adapter = self._adapter_for_source(source)
        _status_chat_id = source.chat_id
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            _status_thread_metadata: Optional[Dict[str, Any]] = {'thread_id': _progress_thread_id, 'reply_to_message_id': event_message_id}
        else:
            _status_thread_metadata = (self._thread_metadata_for_source(source, event_message_id) if _progress_thread_id == source.thread_id else self._thread_metadata_for_target(source.platform, source.chat_id, _progress_thread_id, chat_type=getattr(source, 'chat_type', None), reply_to_message_id=event_message_id)) if _progress_thread_id else None
            if _status_thread_metadata is None and _relay_prospective_thread_id:
                _status_thread_metadata = {'reply_to_message_id': event_message_id}
        turn_ctx._status_adapter = _status_adapter
        turn_ctx._status_chat_id = _status_chat_id
        turn_ctx._status_thread_metadata = _status_thread_metadata
        turn_ctx._status_callback_sync = turn_runner._status_callback_sync
        _stts_adapter = self._adapter_for_source(source)
        _is_voice_input = message_type is not None and str(getattr(message_type, 'value', message_type)).lower() == 'voice'
        if _stts_adapter is not None and _is_voice_input and _stts_adapter._should_auto_tts_for_chat(source.chat_id):
            try:
                from gateway.streaming_tts_consumer import StreamingTTSConsumer
                from tools.tts_tool import _load_tts_config
                _tts_cfg = _load_tts_config()
                _gateway_loop = self._gateway_loop or asyncio.get_event_loop()
                _stts_consumer = StreamingTTSConsumer(adapter=_stts_adapter, chat_id=source.chat_id, tts_config=_tts_cfg, loop=_gateway_loop, metadata=_status_thread_metadata)
                if _stts_consumer.active:
                    streaming_tts_consumer_holder[0] = _stts_consumer
                    _stts_consumer.start()
            except Exception as _stts_err:
                logger.debug('Could not set up streaming TTS consumer: %s', _stts_err)
        run_sync = turn_runner.run_sync
        progress_task = None
        if needs_progress_queue:
            progress_task = asyncio.create_task(send_progress_messages())
        log_task = None
        if log_mode_enabled:
            log_task = asyncio.create_task(write_tool_log())
        stream_task = None

        async def _start_stream_consumer():
            """Wait for the stream consumer to be created, then run it."""
            for _ in range(200):
                if stream_consumer_holder[0] is not None:
                    await stream_consumer_holder[0].run()
                    return
                await asyncio.sleep(0.05)
        stream_task = asyncio.create_task(_start_stream_consumer())

        async def track_agent():
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if not session_key:
                return
            if run_generation is not None and (not self._is_session_run_current(session_key, run_generation)):
                logger.info('Skipping stale agent promotion for %s — generation %s is no longer current', session_key or '', run_generation)
                return
            self._session_state(session_key).turn.agent = agent_holder[0]
            if self._draining:
                self._update_runtime_status('draining')
        tracking_task = asyncio.create_task(track_agent())
        _interrupt_detected = asyncio.Event()

        async def monitor_for_interrupt():
            if not session_key:
                return
            while True:
                await asyncio.sleep(0.2)
                try:
                    _adapter = self._adapter_for_source(source)
                    if not _adapter:
                        continue
                    if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                        agent = agent_holder[0]
                        if agent:
                            _peek_event = _adapter._pending_messages.get(session_key)
                            pending_text = None
                            if _peek_event is not None:
                                pending_text = _peek_event.text or ''
                                _media_urls = getattr(_peek_event, 'media_urls', None) or []
                                if self._pending_event_audio_paths(_peek_event):
                                    pending_text, _ = await self._transcribe_and_echo_pending_voice(_peek_event, _adapter, source, pending_text, log_context='Voice-interrupt', metadata={'thread_id': source.thread_id} if source.thread_id else None)
                                elif not pending_text and _media_urls:
                                    pending_text = _build_media_placeholder(_peek_event)
                            logger.debug('Interrupt detected from adapter, signaling agent...')
                            agent.interrupt(pending_text)
                            _interrupt_detected.set()
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort('barge-in')
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as _mon_err:
                    logger.debug('monitor_for_interrupt error (will retry): %s', _mon_err)
        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())
        _NOTIFY_INTERVAL_RAW = _float_env('HERMES_AGENT_NOTIFY_INTERVAL', 180)
        _NOTIFY_INTERVAL = _NOTIFY_INTERVAL_RAW if _NOTIFY_INTERVAL_RAW > 0 else None
        _long_running_mode = _display_surface_mode('long_running_notifications', default=True, allow_generic=True)
        if _long_running_mode == 'off':
            _NOTIFY_INTERVAL = None
        _notify_start = time.time()

        async def _notify_long_running():
            if _NOTIFY_INTERVAL is None:
                return
            _notify_adapter = self._adapter_for_source(source)
            if not _notify_adapter:
                return
            _heartbeat_msg_id: Optional[str] = None
            while True:
                await asyncio.sleep(_NOTIFY_INTERVAL)
                try:
                    _exec_ref = _executor_task
                except NameError:
                    _exec_ref = None
                if not self._should_emit_long_running_notification(session_key, agent_holder[0], _exec_ref):
                    break
                _elapsed_mins = int((time.time() - _notify_start) // 60)
                _agent_ref = agent_holder[0]
                _status_detail = ''
                _want_iteration_detail = bool(resolve_display_setting(user_config, platform_key, 'busy_ack_detail', True))
                if _agent_ref and hasattr(_agent_ref, 'get_activity_summary'):
                    try:
                        _a = _agent_ref.get_activity_summary()
                        _parts = []
                        if _want_iteration_detail:
                            _parts.append(f"iteration {_a['api_call_count']}/{_a['max_iterations']}")
                        _action = _a.get('current_tool') or _a.get('last_activity_desc')
                        if _action:
                            _parts.append(str(_action))
                        if _parts:
                            _status_detail = ' — ' + ', '.join(_parts)
                    except Exception:
                        pass
                _heartbeat_text = _generic_status_phrase('status') if _long_running_mode == 'generic' else f'⏳ Working — {_elapsed_mins} min{_status_detail}'
                try:
                    _notify_res = None
                    if _heartbeat_msg_id:
                        try:
                            _notify_res = await _notify_adapter.edit_message(source.chat_id, _heartbeat_msg_id, _heartbeat_text)
                        except Exception as _ee:
                            logger.debug('Heartbeat edit failed: %s', _ee)
                            _notify_res = None
                    if not (_notify_res and getattr(_notify_res, 'success', False)):
                        _notify_res = await _notify_adapter.send(source.chat_id, _heartbeat_text, metadata=_non_conversational_metadata(_status_thread_metadata, platform=source.platform))
                        if getattr(_notify_res, 'success', False) and getattr(_notify_res, 'message_id', None):
                            _heartbeat_msg_id = str(_notify_res.message_id)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
                except Exception as _ne:
                    logger.debug('Long-running notification error: %s', _ne)
        _notify_task = asyncio.create_task(_notify_long_running())

        def _stream_confirmed_final_delivery(consumer, final_text: str, *, previewed: bool=False) -> bool:
            """Return True only when the actual final reply reached the user."""
            if consumer is None:
                return False
            if getattr(consumer, 'final_response_sent', False):
                matcher = getattr(consumer, 'delivered_final_matches', None)
                if callable(matcher):
                    try:
                        if matcher(final_text) is False:
                            return False
                    except Exception:
                        pass
                return True
            if previewed:
                has_delivered_text = getattr(consumer, 'has_delivered_text', None)
                if callable(has_delivered_text):
                    try:
                        return bool(has_delivered_text(final_text))
                    except Exception:
                        return False
            return False
        try:
            _agent_timeout_raw = _float_env('HERMES_AGENT_TIMEOUT', 1800)
            _agent_timeout = _agent_timeout_raw if _agent_timeout_raw > 0 else None
            _agent_warning_raw = _float_env('HERMES_AGENT_TIMEOUT_WARNING', 900)
            _agent_warning = _agent_warning_raw if _agent_warning_raw > 0 else None
            _warning_fired = False
            from tools.process_registry import process_registry
            _turn_task_id = session_id or ''
            _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
            turn_ctx.process_task_id = _turn_task_id
            turn_ctx.process_baseline = _turn_process_baseline
            _turn_worker_done = threading.Event()
            _turn_timeout_fired = threading.Event()
            _turn_cleanup_lock = threading.Lock()
            _turn_run_generation = run_generation
            _turn_is_current = (lambda: self._is_session_run_current(session_key, _turn_run_generation)) if _turn_run_generation is not None else lambda: True

            def _run_sync_with_timeout_lifecycle():
                try:
                    return run_sync()
                finally:
                    _turn_worker_done.set()
                    _finished_agent = agent_holder[0] if agent_holder else None
                    if _finished_agent is not None:
                        _finished_agent._gateway_turn_process_task_id = ''
                        _finished_agent._gateway_turn_process_baseline = frozenset()
            if _agent_timeout is not None:
                threading.Thread(target=_watch_gateway_turn_inactivity, kwargs={'agent_holder': agent_holder, 'task_id': _turn_task_id, 'process_baseline': _turn_process_baseline, 'timeout': _agent_timeout, 'worker_done': _turn_worker_done, 'timeout_fired': _turn_timeout_fired, 'cleanup_lock': _turn_cleanup_lock, 'poll_interval': 5.0, 'is_still_current': _turn_is_current}, name=f'gateway-turn-watchdog-{_turn_task_id[:12]}', daemon=True).start()
            _executor_task = asyncio.ensure_future(self._run_in_executor_with_context(_run_sync_with_timeout_lifecycle))
            _inactivity_timeout = False
            _POLL_INTERVAL = 5.0
            if _agent_timeout is None:
                response = None
                while True:
                    done, _ = await asyncio.wait({_executor_task}, timeout=_POLL_INTERVAL)
                    if done:
                        response = _executor_task.result()
                        break
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if _backup_adapter and _backup_agent and hasattr(_backup_adapter, 'has_pending_interrupt') and _backup_adapter.has_pending_interrupt(session_key):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, 'media_urls', None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(_bp_event, _backup_adapter, source, _bp_text or '', log_context='Voice-backup-interrupt', metadata={'thread_id': source.thread_id} if source.thread_id else None)
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info('Backup interrupt detected for session %s (monitor task state: %s)', session_key, 'done' if interrupt_monitor.done() else 'running')
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort('barge-in')
            else:
                response = None
                while True:
                    done, _ = await asyncio.wait({_executor_task}, timeout=_POLL_INTERVAL)
                    if done:
                        response = _executor_task.result()
                        break
                    if _turn_timeout_fired.is_set():
                        _inactivity_timeout = True
                        break
                    _agent_ref = agent_holder[0]
                    _idle_secs = 0.0
                    if _agent_ref and hasattr(_agent_ref, 'get_activity_summary'):
                        try:
                            _act = _agent_ref.get_activity_summary()
                            _idle_secs = _act.get('seconds_since_activity', 0.0)
                        except Exception:
                            pass
                    if not _warning_fired and _agent_warning is not None and (_idle_secs >= _agent_warning):
                        _warning_fired = True
                        _warn_adapter = self._adapter_for_source(source)
                        if _warn_adapter:
                            _elapsed_warn = int(_agent_warning // 60) or 1
                            _remaining_mins = int((_agent_timeout - _agent_warning) // 60) or 1
                            try:
                                await _warn_adapter.send(source.chat_id, f'⚠️ No activity for {_elapsed_warn} min. If the agent does not respond soon, it will be timed out in {_remaining_mins} min. You can continue waiting or use /reset.', metadata=_status_thread_metadata)
                            except Exception as _warn_err:
                                logger.debug('Inactivity warning send error: %s', _warn_err)
                    if _idle_secs >= _agent_timeout:
                        _inactivity_timeout = True
                        threading.Thread(target=_abandon_timed_out_gateway_turn, kwargs={'agent_holder': agent_holder, 'task_id': _turn_task_id, 'process_baseline': _turn_process_baseline, 'worker_done': _turn_worker_done, 'timeout_fired': _turn_timeout_fired, 'cleanup_lock': _turn_cleanup_lock, 'is_still_current': _turn_is_current}, name=f'gateway-turn-reaper-{_turn_task_id[:12]}', daemon=True).start()
                        break
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if _backup_adapter and _backup_agent and hasattr(_backup_adapter, 'has_pending_interrupt') and _backup_adapter.has_pending_interrupt(session_key):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, 'media_urls', None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(_bp_event, _backup_adapter, source, _bp_text or '', log_context='Voice-backup-interrupt', metadata={'thread_id': source.thread_id} if source.thread_id else None)
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info('Backup interrupt detected for session %s (monitor task state: %s)', session_key, 'done' if interrupt_monitor.done() else 'running')
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort('barge-in')
            if _inactivity_timeout:
                _timed_out_agent = agent_holder[0]
                _activity = {}
                if _timed_out_agent and hasattr(_timed_out_agent, 'get_activity_summary'):
                    try:
                        _activity = _timed_out_agent.get_activity_summary()
                    except Exception:
                        pass
                _last_desc = _activity.get('last_activity_desc', 'unknown')
                _secs_ago = _activity.get('seconds_since_activity', 0)
                _cur_tool = _activity.get('current_tool')
                _iter_n = _activity.get('api_call_count', 0)
                _iter_max = _activity.get('max_iterations', 0)
                logger.error('Agent idle for %.0fs (timeout %.0fs) in session %s | last_activity=%s | iteration=%s/%s | tool=%s', _secs_ago, _agent_timeout, session_key, _last_desc, _iter_n, _iter_max, _cur_tool or 'none')
                if _timed_out_agent:
                    request_hard_interrupt(_timed_out_agent, _INTERRUPT_REASON_TIMEOUT)
                _timeout_mins = int(_agent_timeout // 60) or 1
                _diag_lines = [f'⏱️ Agent inactive for {_timeout_mins} min — no tool calls or API responses.']
                if _cur_tool:
                    _diag_lines.append(f'The agent appears stuck on tool `{_cur_tool}` ({_secs_ago:.0f}s since last activity, iteration {_iter_n}/{_iter_max}).')
                else:
                    _diag_lines.append(f'Last activity: {_last_desc} ({_secs_ago:.0f}s ago, iteration {_iter_n}/{_iter_max}). The agent may have been waiting on an API response.')
                _diag_lines.append('To increase the limit, set agent.gateway_timeout in config.yaml (value in seconds, 0 = no limit) and restart the gateway.\nTry again, or use /reset to start fresh.')
                response = {'final_response': '\n'.join(_diag_lines), 'messages': result_holder[0].get('messages', []) if result_holder[0] else [], 'api_calls': _iter_n, 'tools': tools_holder[0] or [], 'history_offset': 0, 'failed': True}
            _agent = agent_holder[0]
            _result_for_fb = result_holder[0]
            _run_failed = _result_for_fb.get('failed') if _result_for_fb else False
            if _agent is not None and hasattr(_agent, 'model') and (not _run_failed):
                _cfg_model = _resolve_gateway_model()
                try:
                    from hermes_cli.model_normalize import _AGGREGATOR_PROVIDERS, normalize_model_for_provider
                    _agent_provider = getattr(_agent, 'provider', '') or ''
                    if _agent_provider and _agent_provider not in _AGGREGATOR_PROVIDERS:
                        _cfg_model = normalize_model_for_provider(_cfg_model, _agent_provider)
                except Exception:
                    pass
                if _agent.model != _cfg_model and (not self._is_intentional_model_switch(session_key, _agent.model)):
                    self._evict_cached_agent(session_key)
            result = result_holder[0]
            adapter = self._adapter_for_source(source)
            _stts = streaming_tts_consumer_holder[0]
            if _stts is not None:
                _stts.finish()
                try:
                    await _stts.wait_complete(timeout=10.0)
                except Exception as _stts_done_err:
                    logger.debug('streaming TTS wait_complete error: %s', _stts_done_err)
                if not _stts.done:
                    _stts.abort('streaming TTS finalisation timeout')
                    await _stts.wait_complete(timeout=2.0)
                if _stts.suppress_whole_file and adapter is not None:
                    _mark_turn = getattr(adapter, '_mark_streaming_tts_completed_turn', None)
                    if callable(_mark_turn):
                        _mark_turn(session_key, run_generation)
            pending_event = None
            pending = None
            if result and adapter and session_key:
                pending_event = _dequeue_pending_event(adapter, session_key)
                pending_event = self._promote_queued_event(session_key, adapter, pending_event)
                if result.get('interrupted') and (not pending_event) and result.get('interrupt_message'):
                    interrupt_message = result.get('interrupt_message')
                    if _is_control_interrupt_message(interrupt_message):
                        logger.info('Ignoring control interrupt message for session %s: %s', session_key or '?', interrupt_message)
                    else:
                        pending = interrupt_message
                elif pending_event:
                    _pending_text = pending_event.text or ''
                    _media_urls = getattr(pending_event, 'media_urls', None) or []
                    if self._pending_event_audio_paths(pending_event):
                        pending, _ = await self._transcribe_and_echo_pending_voice(pending_event, adapter, source, _pending_text, log_context='Voice-drain', metadata={'thread_id': source.thread_id} if source.thread_id else None)
                        if not pending:
                            pending = _build_media_placeholder(pending_event)
                    else:
                        pending = _pending_text or _build_media_placeholder(pending_event)
                    if pending:
                        logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])
            if result and (not pending) and (not pending_event):
                _leftover_steer = result.get('pending_steer')
                if _leftover_steer:
                    pending = _leftover_steer
                    logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])
            if pending and pending.strip().startswith('/'):
                _pending_parts = pending.strip().split(None, 1)
                _pending_cmd_word = _pending_parts[0][1:].lower() if _pending_parts else ''
                if _pending_cmd_word:
                    try:
                        from hermes_cli.commands import resolve_command as _rc_pending
                        if _rc_pending(_pending_cmd_word):
                            logger.info("Discarding command '/%s' from pending queue — commands must not be passed as agent input", _pending_cmd_word)
                            pending_event = None
                            pending = None
                    except Exception:
                        pass
            if self._draining and (pending_event or pending):
                logger.info('Discarding pending follow-up for session %s during gateway %s', session_key or '?', self._status_action_label())
                pending_event = None
                pending = None
            if pending_event or pending:
                logger.debug("Processing pending message: '%s...'", pending[:40])
                if adapter and hasattr(adapter, '_active_sessions') and session_key and (session_key in adapter._active_sessions):
                    adapter._active_sessions[session_key].clear()
                if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
                    logger.warning('Interrupt recursion depth %d reached for session %s — queueing message instead of recursing.', _interrupt_depth, session_key)
                    adapter = self._adapter_for_source(source)
                    if adapter and pending_event:
                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)
                    elif adapter and hasattr(adapter, 'queue_message'):
                        adapter.queue_message(session_key, pending)
                    return result_holder[0] or {'final_response': response, 'messages': history}
                was_interrupted = result.get('interrupted')
                if not was_interrupted:
                    _sc = stream_consumer_holder[0]
                    if _sc and stream_task:
                        try:
                            await asyncio.wait_for(stream_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            stream_task.cancel()
                            try:
                                await stream_task
                            except asyncio.CancelledError:
                                pass
                        except Exception as e:
                            logger.debug('Stream consumer wait before queued message failed: %s', e)
                    _delivery_result = response if isinstance(response, dict) else result or {}
                    _previewed = bool(_delivery_result.get('response_previewed'))
                    first_response = _delivery_result.get('final_response', '')
                    _already_streamed = _stream_confirmed_final_delivery(_sc, first_response, previewed=_previewed)
                    try:
                        from gateway.response_filters import is_intentional_silence_agent_result
                        _intentional_silence = is_intentional_silence_agent_result(_delivery_result, first_response)
                    except Exception:
                        _intentional_silence = False
                    if _intentional_silence:
                        logger.info('Queued follow-up for session %s: suppressing intentional silence marker before continuing.', session_key or '?')
                    elif first_response and (not _already_streamed):
                        try:
                            logger.info('Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.', session_key or '?')
                            await adapter.send(source.chat_id, first_response, metadata=_status_thread_metadata)
                        except Exception as e:
                            logger.warning('Failed to send first response before queued message: %s', e)
                    elif first_response:
                        logger.info('Queued follow-up for session %s: skipping resend because final streamed delivery was confirmed.', session_key or '?')
                    if getattr(type(adapter), 'pop_post_delivery_callback', None) is not None:
                        _bg_cb = adapter.pop_post_delivery_callback(session_key, generation=run_generation)
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                    elif adapter and hasattr(adapter, '_post_delivery_callbacks'):
                        _bg_cb = adapter._post_delivery_callbacks.pop(session_key, None)
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                updated_history = result.get('messages', history)
                next_source = source
                next_message = pending
                next_message_id = None
                next_channel_prompt = None
                next_session_key = session_key
                next_message_type = None
                if pending_event is not None:
                    next_source = getattr(pending_event, 'source', None) or source
                    if self._is_goal_continuation_event(pending_event) and (not self._goal_still_active_for_session(session_id)):
                        logger.info('Discarding stale goal continuation for session %s — goal is no longer active', session_key or '?')
                        return result
                    try:
                        next_session_key = self._session_key_for_source(next_source)
                    except Exception:
                        logger.debug('Queued follow-up session-key resolution failed; reusing %s', session_key or '?', exc_info=True)
                    next_message = await self._prepare_profile_scoped_inbound_message_text(event=pending_event, source=next_source, history=updated_history, session_key=next_session_key)
                    if next_message is None:
                        return result
                    next_message_id = self._reply_anchor_for_event(pending_event)
                    next_channel_prompt = getattr(pending_event, 'channel_prompt', None)
                    next_message_type = getattr(pending_event, 'message_type', None)
                _clear_adapter = self._adapter_for_source(source)
                if _clear_adapter is not None and session_key and (run_generation is not None):
                    _completed_turns = getattr(_clear_adapter, '_streaming_tts_completed_turns', None)
                    if _completed_turns is not None:
                        _prior_key = getattr(_clear_adapter, '_streaming_tts_turn_key', None)
                        if callable(_prior_key):
                            _pk = _prior_key(session_key, run_generation)
                            if _pk:
                                _completed_turns.discard(_pk)
                _followup_adapter = self._adapter_for_source(source)
                if _followup_adapter:
                    try:
                        await _followup_adapter.send_typing(source.chat_id, metadata=_status_thread_metadata)
                    except Exception:
                        pass
                await self._refresh_agent_cache_message_count(session_key, session_id)
                followup_result = await self._run_agent(message=next_message, context_prompt=context_prompt, history=updated_history, source=next_source, session_id=session_id, session_key=next_session_key, run_generation=run_generation, _interrupt_depth=_interrupt_depth + 1, event_message_id=next_message_id, channel_prompt=next_channel_prompt, message_type=next_message_type)
                return _preserve_queued_followup_history_offset(result, followup_result)
        finally:
            if progress_task:
                progress_task.cancel()
            if log_task:
                log_task.cancel()
            interrupt_monitor.cancel()
            _notify_task.cancel()
            if stream_task:
                _has_stream_consumer = stream_consumer_holder and stream_consumer_holder[0] is not None
                if not _has_stream_consumer:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(stream_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stream_task.cancel()
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass
            _stts_finally = streaming_tts_consumer_holder[0]
            if _stts_finally is not None and (not _stts_finally.done):
                _stts_finally.abort('cleanup')
                try:
                    await _stts_finally.wait_complete(timeout=2.0)
                except Exception:
                    pass
            tracking_task.cancel()
            if session_key:
                self._release_running_agent_state(session_key, run_generation=run_generation)
            if self._draining:
                self._update_runtime_status('draining')
            for task in [progress_task, log_task, interrupt_monitor, tracking_task, _notify_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        _sc = stream_consumer_holder[0]
        if isinstance(response, dict) and (not response.get('failed')):
            _final = response.get('final_response') or ''
            _is_empty_sentinel = not _final or _final == '(empty)'
            _previewed = bool(response.get('response_previewed'))
            _content_delivered = bool(_sc and getattr(_sc, 'final_content_delivered', False))
            _stale_finalized = False
            if _content_delivered and (not _is_empty_sentinel):
                _matcher = getattr(_sc, 'delivered_final_matches', None)
                if callable(_matcher):
                    try:
                        _stale_finalized = _matcher(_final) is False
                    except Exception:
                        _stale_finalized = False
                if _stale_finalized:
                    _content_delivered = False
            _transformed = bool(response.get('response_transformed'))
            _streamed = _stream_confirmed_final_delivery(_sc, _final, previewed=_previewed)
            if not _is_empty_sentinel and (not _transformed) and (_streamed or _content_delivered):
                logger.info('Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).', session_key or '?', _streamed, _previewed, _content_delivered)
                response['already_sent'] = True
            elif not _is_empty_sentinel and (not _transformed) and _stale_finalized and (_sc is not None):
                _sc_msg_id = _sc.message_id
                _sc_adapter = getattr(_sc, 'adapter', None)
                if getattr(_sc, '_turn_split_delivery', False):
                    logger.info('Stale streamed finalize detected for session %s on a multi-message split; skipping the in-place reconciliation edit and delivering the complete response via normal final send (#78541).', session_key or '?')
                elif _sc_msg_id and _sc_msg_id != '__no_edit__' and (_sc_adapter is not None):
                    try:
                        _reconcile_res = await _sc_adapter.edit_message(chat_id=source.chat_id, message_id=_sc_msg_id, content=_final, finalize=True)
                        if getattr(_reconcile_res, 'success', True):
                            response['already_sent'] = True
                            logger.info('Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).', session_key or '?', _sc_msg_id)
                        else:
                            logger.warning('Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.', session_key or '?', getattr(_reconcile_res, 'error', None))
                    except Exception as _edit_err:
                        logger.warning('Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.', session_key or '?', _edit_err)
                else:
                    logger.info('Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).', session_key or '?')
            elif not _is_empty_sentinel and _transformed and (_sc is not None):
                _sc_msg_id = _sc.message_id
                if _sc_msg_id:
                    try:
                        await _sc.adapter.edit_message(chat_id=source.chat_id, message_id=_sc_msg_id, content=response['final_response'], finalize=True)
                        response['already_sent'] = True
                        logger.info('Edited streamed message %s for session %s to include plugin-transformed content.', _sc_msg_id, session_key or '?')
                    except Exception as _edit_err:
                        logger.warning('Failed to edit streamed message for session %s: %s', session_key or '?', _edit_err)
        if _cleanup_progress and _cleanup_adapter is not None and _cleanup_msg_ids and session_key and isinstance(response, dict) and (not response.get('failed')) and hasattr(_cleanup_adapter, 'register_post_delivery_callback'):
            _ids_snapshot = list(_cleanup_msg_ids)
            _chat_id_snapshot = source.chat_id
            _adapter_snapshot = _cleanup_adapter
            _loop_snapshot = asyncio.get_running_loop()

            def _cleanup_temp_bubbles() -> None:

                async def _delete_all() -> None:
                    for _mid in _ids_snapshot:
                        try:
                            await _adapter_snapshot.delete_message(_chat_id_snapshot, _mid)
                        except Exception:
                            pass
                try:
                    safe_schedule_threadsafe(_delete_all(), _loop_snapshot, logger=logger, log_message='Temp bubble cleanup scheduling error')
                except Exception:
                    pass
            try:
                _cleanup_adapter.register_post_delivery_callback(session_key, _cleanup_temp_bubbles, generation=run_generation)
            except Exception as _rpe:
                logger.debug('Post-delivery cleanup registration failed: %s', _rpe)
        return response

def _run_planned_stop_watcher(stop_event: threading.Event, runner, loop: asyncio.AbstractEventLoop, shutdown_handler, *, poll_interval: float=0.5) -> None:
    """Poll for the planned-stop marker and trigger graceful shutdown.

    On Windows, ``asyncio.add_signal_handler`` raises NotImplementedError
    for SIGTERM/SIGINT, so the standard signal-driven shutdown path
    never runs when ``duck-agent gateway stop`` signals the gateway. The
    consequence is that the drain loop is skipped — in-flight agent
    sessions are killed mid-turn and ``resume_pending`` is never set,
    so the next gateway boot has no idea those sessions need to be
    auto-resumed (issue #33778, v0.13.0 session-resume feature broken
    on native Windows).

    This watcher runs on every platform (cheap, defensive) and bridges
    the gap on Windows by translating a filesystem marker into the
    same shutdown-handler invocation a real SIGTERM would have produced
    on POSIX. The CLI's ``hermes_cli.gateway_windows.stop()`` writes
    the marker via ``write_planned_stop_marker(pid)`` and then waits
    for the gateway PID to exit; this watcher is what makes that
    exit happen cleanly.

    On POSIX this is a no-op safety net — the signal handler always
    races us to consuming the marker file because it fires synchronously
    from the kernel's signal delivery.

    Args:
        stop_event: cleared by start_gateway() during normal shutdown
            to tell the watcher to exit.
        runner: the GatewayRunner instance; we check ``_running`` and
            ``_draining`` to avoid triggering shutdown if the gateway
            is already in one of those states.
        loop: the asyncio event loop the shutdown handler must run on.
        shutdown_handler: same callable that's wired to SIGTERM —
            tolerates a ``None`` signal argument (planned stop case)
            and consumes the marker via
            ``consume_planned_stop_marker_for_self()``.
        poll_interval: seconds between marker checks. 0.5s gives a
            responsive shutdown without burning CPU.
    """
    from gateway.status import _get_planned_stop_marker_path, planned_stop_marker_targets_self
    marker_path = _get_planned_stop_marker_path()
    while not stop_event.is_set():
        try:
            if marker_path.exists() and (not getattr(runner, '_draining', False)) and getattr(runner, '_running', False):
                if not planned_stop_marker_targets_self():
                    stop_event.wait(poll_interval)
                    continue
                loop.call_soon_threadsafe(shutdown_handler, None)
                break
        except Exception as _e:
            logger.debug('Planned-stop watcher tick error: %s', _e)
        stop_event.wait(poll_interval)

def _start_gateway_housekeeping(stop_event: threading.Event, adapters=None, loop=None, interval: int=60):
    """Background thread for gateway-only periodic chores (NOT cron).

    Split out of the historical ``_start_cron_ticker`` so the cron *trigger*
    can live behind the ``CronScheduler`` provider (built-in or external) while
    these gateway-specific chores keep running independently of which provider
    fires cron. An external scale-to-zero provider has no 60s loop at all, but
    this housekeeping still wants its hourly cadence — so it owns its own loop.

    Refreshes the channel directory every 5 minutes and prunes the
    image/audio/video/document/screenshot caches + expired ``duck-agent debug
    share`` pastes once per hour, and polls the curator hourly (its inner
    gate enforces the real weekly cadence).
    """
    from gateway.platforms.base import cleanup_audio_cache, cleanup_document_cache, cleanup_image_cache, cleanup_screenshot_cache, cleanup_video_cache
    from hermes_cli.debug import _sweep_expired_pastes
    IMAGE_CACHE_EVERY = 60
    CHANNEL_DIR_EVERY = 5
    PASTE_SWEEP_EVERY = 60
    CURATOR_EVERY = 60
    AUTO_ARCHIVE_EVERY = 60
    MEMORY_TRIM_EVERY = 1
    MEDIA_CACHE_CLEANUPS = (('Image', cleanup_image_cache), ('Document', cleanup_document_cache), ('Audio', cleanup_audio_cache), ('Video', cleanup_video_cache), ('Screenshot', cleanup_screenshot_cache))
    logger.info('Gateway housekeeping started (interval=%ds)', interval)
    tick_count = 0
    while not stop_event.is_set():
        tick_count += 1
        if tick_count % CHANNEL_DIR_EVERY == 0 and adapters:
            try:
                from gateway.channel_directory import build_channel_directory
                if loop is not None:
                    fut = safe_schedule_threadsafe(build_channel_directory(adapters), loop, logger=logger, log_message='Channel directory refresh scheduling error')
                    if fut is not None:
                        fut.result(timeout=30)
            except Exception as e:
                logger.debug('Channel directory refresh error: %s', e)
        if tick_count % IMAGE_CACHE_EVERY == 0:
            for cache_name, cleanup_fn in MEDIA_CACHE_CLEANUPS:
                try:
                    removed = cleanup_fn(max_age_hours=24)
                    if removed:
                        logger.info('%s cache cleanup: removed %d stale file(s)', cache_name, removed)
                except Exception as e:
                    logger.debug('%s cache cleanup error: %s', cache_name, e)
        if tick_count % PASTE_SWEEP_EVERY == 0:
            try:
                deleted, remaining = _sweep_expired_pastes()
                if deleted:
                    logger.info('Paste sweep: deleted %d expired paste(s), %d pending', deleted, remaining)
            except Exception as e:
                logger.debug('Paste sweep error: %s', e)
        if tick_count % CURATOR_EVERY == 0:
            try:
                from agent.curator import maybe_run_curator
                maybe_run_curator(idle_for_seconds=float('inf'), on_summary=lambda msg: logger.info('curator: %s', msg))
            except Exception as e:
                logger.debug('Curator tick error: %s', e)
            try:
                from tools.skills_sync_client import maybe_pull_skills
                maybe_pull_skills()
            except Exception as e:
                logger.debug('Sync pull tick error: %s', e)
            try:
                from tools.skills_sync_client import maybe_pull_org_skills
                maybe_pull_org_skills()
            except Exception as e:
                logger.debug('Org sync pull tick error: %s', e)
        if tick_count % AUTO_ARCHIVE_EVERY == 0:
            try:
                from hermes_cli.config import load_config as _load_full_config
                from hermes_state import SessionDB
                _sess_cfg = _load_full_config().get('sessions') or {}
                if _sess_cfg.get('auto_archive', False):
                    _adb = SessionDB()
                    try:
                        _adb.maybe_auto_archive(idle_days=float(_sess_cfg.get('auto_archive_days', 3)), min_interval_hours=int(_sess_cfg.get('min_interval_hours', 24)))
                    finally:
                        _adb.close()
            except Exception as e:
                logger.debug('Auto-archive tick error: %s', e)
        if tick_count % MEMORY_TRIM_EVERY == 0:
            try:
                from hermes_cli.mem_trim import trim_memory
                trim_memory(reason='messaging gateway housekeeping')
            except Exception as exc:
                logger.debug('gateway housekeeping memory trim failed: %s: %s', type(exc).__name__, exc)
        stop_event.wait(timeout=interval)
    logger.info('Gateway housekeeping stopped')

def _start_cron_ticker(stop_event: threading.Event, adapters=None, loop=None, interval: int=60):
    """DEPRECATED shim — preserved for backward compatibility.

    The cron trigger now lives behind the ``CronScheduler`` provider
    (``cron.scheduler_provider``); the gateway resolves a provider and runs its
    ``start()`` directly (see ``start_gateway``). This shim runs ONLY the
    built-in in-process tick loop, exactly as before, for any external caller
    or test that still references this symbol (e.g. hermes_cli/debug.py). It no
    longer runs gateway housekeeping — that moved to
    ``_start_gateway_housekeeping``.
    """
    from cron.scheduler_provider import InProcessCronScheduler
    InProcessCronScheduler().start(stop_event, adapters=adapters, loop=loop, interval=interval)
_CRON_SHUTDOWN_DRAIN_TIMEOUT = 65.0
_HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT = 35.0

async def _await_thread_exit(thread: Optional[threading.Thread], timeout: float, poll: float=0.1) -> bool:
    """Wait for a daemon thread to exit WITHOUT blocking the event loop.

    A synchronous ``thread.join()`` here would freeze the event loop — fatal
    for the cron ticker, whose in-flight delivery is a coroutine scheduled onto
    *this* loop via ``safe_schedule_threadsafe``. Blocking the loop deadlocks
    that delivery (the loop can never run it), so ``join(timeout=5)`` always
    times out and the message is silently dropped on restart (#58818).

    Polling ``is_alive()`` with ``await asyncio.sleep`` keeps the loop running
    so the pending delivery completes, then the ticker sees ``stop_event`` and
    exits. Returns True if the thread exited within ``timeout``.
    """
    if thread is None:
        return True
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while thread.is_alive() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll)
    return not thread.is_alive()

def _shutdown_gateway_health_export(runner: Any) -> None:
    """Idempotently drain and detach Gateway Health OTLP export."""
    runtime = getattr(runner, '_gateway_health_export_runtime', None)
    if runtime is None:
        return
    runner._gateway_health_export_runtime = None
    try:
        runtime.shutdown()
    except Exception:
        logger.debug('gateway health OTLP export shutdown failed', exc_info=True)

async def start_gateway(config: Optional[GatewayConfig]=None, replace: bool=False, verbosity: Optional[int]=0) -> bool:
    """
    Start the gateway and run until interrupted.
    
    This is the main entry point for running the gateway.
    Returns True if the gateway ran successfully, False if it failed to start.
    A False return causes a non-zero exit code so systemd can auto-restart.
    
    Args:
        config: Optional gateway configuration override.
        replace: If True, kill any existing gateway instance before starting.
                 Useful for systemd services to avoid restart-loop deadlocks
                 when the previous process hasn't fully exited yet.
    """
    from gateway.code_skew import record_boot_fingerprint
    record_boot_fingerprint()
    from gateway.status import acquire_gateway_runtime_lock, get_running_pid, get_process_start_time, release_gateway_runtime_lock, remove_pid_file, terminate_pid
    existing_pid = get_running_pid()
    if existing_pid is not None and existing_pid != os.getpid():
        if replace:
            existing_start_time = get_process_start_time(existing_pid)
            logger.info('Replacing existing gateway instance (PID %d) with --replace.', existing_pid)
            try:
                from gateway.status import write_takeover_marker
                write_takeover_marker(existing_pid)
            except Exception as e:
                logger.debug('Could not write takeover marker: %s', e)
            try:
                from gateway.status import _snapshot_gateway_children
                _old_gateway_children = _snapshot_gateway_children(existing_pid)
            except Exception:
                _old_gateway_children = []
            try:
                terminate_pid(existing_pid, force=False)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                logger.error('Permission denied killing PID %d. Cannot replace.', existing_pid)
                try:
                    from gateway.status import clear_takeover_marker
                    clear_takeover_marker()
                except Exception:
                    pass
                return False
            from gateway.status import _pid_exists
            old_gateway_exited = False
            for _ in range(20):
                if not _pid_exists(existing_pid):
                    old_gateway_exited = True
                    break
                time.sleep(0.5)
            else:
                logger.warning('Old gateway (PID %d) did not exit after SIGTERM, sending SIGKILL.', existing_pid)
                try:
                    terminate_pid(existing_pid, force=True)
                except ProcessLookupError:
                    old_gateway_exited = True
                except (PermissionError, OSError):
                    pass
                if not old_gateway_exited:
                    for _ in range(20):
                        if not _pid_exists(existing_pid):
                            old_gateway_exited = True
                            break
                        time.sleep(0.25)
                if not old_gateway_exited:
                    logger.error('Old gateway (PID %d) still appears alive after SIGKILL; aborting replacement to avoid a duplicate gateway.', existing_pid)
                    try:
                        from gateway.status import clear_takeover_marker
                        clear_takeover_marker()
                    except Exception:
                        pass
                    return False
            try:
                from gateway.status import reap_gateway_children
                reap_gateway_children(_old_gateway_children, parent_pid=existing_pid)
            except Exception:
                logger.debug('Child reap for replaced gateway PID %d failed', existing_pid, exc_info=True)
            remove_pid_file()
            try:
                (get_hermes_home() / 'gateway.pid').unlink(missing_ok=True)
            except Exception:
                pass
            try:
                from gateway.status import clear_takeover_marker
                clear_takeover_marker()
            except Exception:
                pass
            try:
                from gateway.status import release_all_scoped_locks
                _released = release_all_scoped_locks(owner_pid=existing_pid, owner_start_time=existing_start_time)
                if _released:
                    logger.info('Released %d stale scoped lock(s) from old gateway.', _released)
            except Exception:
                pass
        else:
            hermes_home = str(get_hermes_home())
            logger.error("Another gateway instance is already running (PID %d, DUCK_AGENT_HOME=%s). Use 'duck-agent gateway restart' to replace it, or 'duck-agent gateway stop' first.", existing_pid, hermes_home)
            print(f"\n❌ Gateway already running (PID {existing_pid}).\n   Use 'duck-agent gateway restart' to replace it,\n   or 'duck-agent gateway stop' to kill it first.\n   Or use 'duck-agent gateway run --replace' to auto-replace.\n")
            return False
    try:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)
    except Exception:
        pass
    from hermes_logging import setup_logging, _safe_stderr
    setup_logging(hermes_home=_hermes_home, mode='gateway')
    try:
        from hermes_cli.security_audit_startup import log_startup_security_warnings
        _audit_cfg = None
        try:
            from hermes_cli.config import read_raw_config
            _audit_cfg = read_raw_config()
        except Exception:
            _audit_cfg = None
        log_startup_security_warnings(hermes_home=_hermes_home, config=_audit_cfg)
    except Exception as _audit_exc:
        logger.debug('Startup security audit failed (non-fatal): %s', _audit_exc)
    if verbosity is not None:
        from agent.redact import RedactingFormatter
        _stderr_level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
        _stderr_handler = logging.StreamHandler(_safe_stderr())
        _stderr_handler.setLevel(_stderr_level)
        _stderr_handler.setFormatter(RedactingFormatter('%(levelname)s %(name)s: %(message)s'))
        logging.getLogger().addHandler(_stderr_handler)
        if _stderr_level < logging.getLogger().level:
            logging.getLogger().setLevel(_stderr_level)
    runner = GatewayRunner(config)
    runner._platform_lock_takeover_on_start = bool(replace)
    _signal_initiated_shutdown = False

    def shutdown_signal_handler(received_signal=None):
        nonlocal _signal_initiated_shutdown
        planned_takeover = False
        try:
            from gateway.status import consume_takeover_marker_for_self
            planned_takeover = consume_takeover_marker_for_self()
        except Exception as e:
            logger.debug('Takeover marker check failed: %s', e)
        planned_stop = False
        if received_signal == signal.SIGINT:
            planned_stop = True
        elif not planned_takeover:
            try:
                from gateway.status import consume_planned_stop_marker_for_self
                planned_stop = consume_planned_stop_marker_for_self()
            except Exception as e:
                logger.debug('Planned stop marker check failed: %s', e)
        try:
            from gateway.shutdown_forensics import format_context_for_log, snapshot_shutdown_context, spawn_async_diagnostic
            _shutdown_ctx = snapshot_shutdown_context(received_signal)
        except Exception as _e:
            _shutdown_ctx = None
            logger.debug('snapshot_shutdown_context failed: %s', _e)
        if planned_takeover:
            logger.info('Received %s as a planned --replace takeover — exiting cleanly', _shutdown_ctx['signal'] if _shutdown_ctx else 'SIGTERM')
        elif planned_stop:
            logger.info('Received %s as a planned gateway stop — exiting cleanly', _shutdown_ctx['signal'] if _shutdown_ctx else 'SIGTERM/SIGINT')
        else:
            _signal_initiated_shutdown = True
            runner._signal_initiated_shutdown = True
            logger.info('Received %s — initiating shutdown', _shutdown_ctx['signal'] if _shutdown_ctx else 'SIGTERM/SIGINT')
        if _shutdown_ctx is not None:
            try:
                logger.warning('Shutdown context: %s', format_context_for_log(_shutdown_ctx))
            except Exception as _e:
                logger.debug('format_context_for_log failed: %s', _e)
            try:
                _diag_log = _hermes_home / 'logs' / 'gateway-shutdown-diag.log'
                spawn_async_diagnostic(_diag_log, _shutdown_ctx['signal'], timeout_seconds=5.0)
            except Exception as _e:
                logger.debug('spawn_async_diagnostic failed: %s', _e)
        asyncio.create_task(runner.stop())

    def restart_signal_handler():
        runner.request_restart(detached=False, via_service=True)
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_gateway_loop_exception_handler)
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_signal_handler, sig)
            except NotImplementedError:
                pass
        if hasattr(signal, 'SIGUSR1'):
            try:
                loop.add_signal_handler(signal.SIGUSR1, restart_signal_handler)
            except NotImplementedError:
                pass
    else:
        logger.info('Skipping signal handlers (not running in main thread).')
    _planned_stop_watcher_stop = threading.Event()
    _planned_stop_watcher_thread = threading.Thread(target=_run_planned_stop_watcher, args=(_planned_stop_watcher_stop, runner, loop, shutdown_signal_handler), daemon=True, name='planned-stop-watcher')
    _planned_stop_watcher_thread.start()
    import atexit
    from gateway.status import write_pid_file, remove_pid_file, get_running_pid
    _current_pid = get_running_pid()
    if _current_pid is not None and _current_pid != os.getpid():
        logger.error('Another gateway instance (PID %d) started during our startup. Exiting to avoid double-running.', _current_pid)
        return False
    if not acquire_gateway_runtime_lock():
        logger.error('Gateway runtime lock is already held by another instance. Exiting.')
        return False
    try:
        write_pid_file()
    except FileExistsError:
        release_gateway_runtime_lock()
        logger.error('PID file race lost to another gateway instance. Exiting.')
        return False
    atexit.register(remove_pid_file)
    atexit.register(release_gateway_runtime_lock)
    try:
        from gateway.lifecycle_ledger import record_startup as _lifecycle_record_startup
        _lifecycle_record_startup()
    except Exception as _lc_exc:
        logger.debug('Lifecycle ledger startup record failed: %s', _lc_exc)
    try:
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive
        start_nous_auth_keepalive()
    except Exception as exc:
        logger.debug('Nous auth keepalive did not start: %s', exc)
    _ensure_windows_gateway_venv_imports()
    try:
        from tools.mcp_tool import discover_mcp_tools
        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(None, discover_mcp_tools)
    except Exception as e:
        logger.debug('MCP tool discovery failed: %s', e)
    try:
        success = await runner.start()
    except BaseException:
        _shutdown_gateway_health_export(runner)
        raise
    if not success:
        _shutdown_gateway_health_export(runner)
        return False
    try:
        from gateway.shutdown_flush import recover_pending_to_db
        recovered = recover_pending_to_db()
        if recovered:
            logger.info('Recovered %d pending message(s) from shutdown flush', recovered)
    except Exception:
        pass
    if runner.should_exit_cleanly:
        _shutdown_gateway_health_export(runner)
        if runner.exit_reason:
            logger.error('Gateway exiting cleanly: %s', runner.exit_reason)
        if runner.exit_code is not None:
            raise SystemExit(runner.exit_code)
        return True
    if not runner._running:
        try:
            await runner.wait_for_shutdown()
            if runner.should_exit_with_failure:
                if runner.exit_reason:
                    logger.error('Gateway exiting with failure: %s', runner.exit_reason)
                return False
            try:
                from tools.mcp_tool import shutdown_mcp_servers
                shutdown_mcp_servers()
            except Exception:
                pass
            if runner.exit_code is not None:
                raise SystemExit(runner.exit_code)
            return True
        finally:
            _shutdown_gateway_health_export(runner)
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler
    cron_stop = threading.Event()
    cron_provider = resolve_cron_scheduler()
    cron_start_kwargs: Dict[str, Any] = {'adapters': runner.adapters, 'loop': asyncio.get_running_loop()}
    if isinstance(cron_provider, InProcessCronScheduler) and getattr(runner.config, 'multiplex_profiles', False):
        try:
            from hermes_cli.profiles import profiles_to_serve
            profile_homes = list(profiles_to_serve(multiplex=True))
            if profile_homes:
                cron_start_kwargs['profile_homes'] = profile_homes
                logger.info('Cron scheduler will tick %d profile(s) under multiplex: %s', len(profile_homes), [p[0] if isinstance(p, tuple) else p for p in profile_homes])
        except Exception as exc:
            logger.warning('Could not resolve profile homes for multiplex cron: %s', exc)
    if isinstance(cron_provider, InProcessCronScheduler):
        cron_start_kwargs['can_dispatch'] = lambda: not (runner._draining or runner._external_drain_active)
    cron_thread = threading.Thread(target=cron_provider.start, args=(cron_stop,), kwargs=cron_start_kwargs, daemon=True, name='cron-scheduler')
    cron_thread.start()
    housekeeping_thread = threading.Thread(target=_start_gateway_housekeeping, args=(cron_stop,), kwargs={'adapters': runner.adapters, 'loop': asyncio.get_running_loop()}, daemon=True, name='gateway-housekeeping')
    housekeeping_thread.start()
    start_watchdog = getattr(runner, '_start_systemd_watchdog', None)
    if callable(start_watchdog):
        start_watchdog()
    await runner.wait_for_shutdown()
    try:
        from hermes_cli.nous_auth_keepalive import stop_nous_auth_keepalive
        stop_nous_auth_keepalive()
    except Exception:
        pass
    if runner.should_exit_with_failure:
        if runner.exit_reason:
            logger.error('Gateway exiting with failure: %s', runner.exit_reason)
        return False
    cron_stop.set()
    try:
        cron_provider.stop()
    except Exception as e:
        logger.debug('Cron provider stop() error: %s', e)
    if not await _await_thread_exit(cron_thread, timeout=_CRON_SHUTDOWN_DRAIN_TIMEOUT):
        logger.warning('Cron ticker did not exit within %.0fs of shutdown — an in-flight delivery may have been dropped.', _CRON_SHUTDOWN_DRAIN_TIMEOUT)
    await _await_thread_exit(housekeeping_thread, timeout=_HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT)
    _planned_stop_watcher_stop.set()
    _planned_stop_watcher_thread.join(timeout=2)
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except Exception:
        pass
    if runner.exit_code is not None:
        raise SystemExit(runner.exit_code)
    if _signal_initiated_shutdown and (not runner._restart_requested):
        logger.info('Exiting with code 1 (signal-initiated shutdown without restart request) so systemd Restart=on-failure can revive the gateway.')
        return False
    if runner._restart_via_service:
        logger.info('Exiting with code 75 (service-restart requested) so the service manager relaunches the gateway.')
        raise SystemExit(75)
    return True

def main():
    """CLI entry point for the gateway."""
    try:
        from hermes_cli.stdio import configure_windows_stdio
        configure_windows_stdio()
    except Exception:
        pass
    import argparse
    parser = argparse.ArgumentParser(description='Duck Agent Gateway - Multi-platform messaging')
    parser.add_argument('--config', '-c', help='Path to gateway config file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    args = parser.parse_args()
    config = None
    if args.config:
        import yaml
        with open(args.config, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
            config = GatewayConfig.from_dict(data)
    try:
        success = asyncio.run(start_gateway(config))
        exit_code = 0 if success else 1
    except SystemExit as e:
        if e.code is None:
            exit_code = 0
        elif isinstance(e.code, int):
            exit_code = e.code
        else:
            exit_code = 1
    _exit_after_graceful_shutdown(exit_code)

def _exit_after_graceful_shutdown(exit_code: int) -> None:
    """Flush stdio, release the PID file + runtime lock, then hard-exit.

    Graceful teardown is already complete by the time this runs, so there is
    nothing left that needs a clean interpreter shutdown. We deliberately use
    ``os._exit`` (not ``sys.exit``): ``sys.exit`` raises ``SystemExit``, which
    triggers ``Py_FinalizeEx`` → ``wait_for_thread_shutdown`` and joins every
    non-daemon thread — exactly the hang (#53107) a wedged tool-worker causes.

    ``os._exit`` bypasses ``atexit`` handlers, so we cannot rely on the
    ``atexit``-registered ``remove_pid_file`` / ``release_gateway_runtime_lock``
    (registered in ``start_gateway``) to run. The full-shutdown path releases
    both explicitly in ``_stop_impl``, but the EARLY exit paths —
    clean-fatal-config (#51228) and startup-aborted-before-running — raise
    ``SystemExit`` right after ``runner.start()`` without going through
    ``_stop_impl``, so on those paths ``atexit`` was the only thing releasing
    them. Now that those paths are routed through this backstop (#53107),
    release both here explicitly. Both calls are idempotent —
    ``remove_pid_file`` only unlinks a PID file that belongs to this process,
    and ``release_gateway_runtime_lock`` no-ops when the lock is already
    released — so this is a no-op on the normal shutdown path and the actual
    cleanup on the early-exit paths.

    Logging IS drained here: the rotating file handlers are driven by an
    async ``QueueListener`` on a dedicated thread (see
    ``hermes_logging._register_queued_handler``), so records emitted right
    before shutdown may still be sitting in the in-memory queue. ``os._exit``
    below bypasses ``atexit``, so the ``atexit``-registered listener drain
    never runs on this path — we drain explicitly (bounded, via
    ``drain_log_queue``) or lose the last log lines (including the shutdown
    reason on the early-exit paths). Stdio is flushed too.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        from gateway.status import remove_pid_file, release_gateway_runtime_lock
        remove_pid_file()
        release_gateway_runtime_lock()
    except Exception:
        pass
    try:
        from gateway.lifecycle_ledger import mark_exited
        mark_exited(exit_code, reason='graceful_shutdown')
    except Exception:
        pass
    try:
        from hermes_logging import drain_log_queue
        drain_log_queue(timeout=1.0)
    except Exception:
        pass
    os._exit(exit_code)
if __name__ == '__main__':
    main()