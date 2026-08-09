"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.duck-agent/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""
import asyncio
import atexit
import concurrent.futures
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from typing import Any, List, Optional
sys.path.insert(0, str(Path(__file__).parent.parent))
from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags
from hermes_cli.config import _expand_env_vars, cron_model_drift_guard_enabled, load_config
from hermes_cli.fallback_config import get_fallback_chain
from hermes_time import now as _hermes_now
from agent.interrupt_compat import request_hard_interrupt
from agent.delegation_context import enter_non_dispatcher_owned_context, exit_non_dispatcher_owned_context
logger = logging.getLogger(__name__)

def _set_cron_session_title(session_db, session_id, base_title):
    """Robustly title a finished cron session before it is closed.

    Centralizes the title write so the cron finally block can guarantee a
    non-blank, unique title is persisted before end_session()/close() tear
    the connection down (issues #50535, #50536, #50537):

    - #50535: never leaves the session blank. base_title already carries a
      cron-id fallback for nameless jobs; this also guards a failed write.
    - #50537: a duplicate title makes set_session_title raise ValueError (the
      unique-title index). Recover by appending a #N suffix via
      get_next_title_in_lineage() when supported, instead of swallowing the
      error and ending up untitled. If lineage dedup is unavailable, raise.
    - #50536: this runs synchronously in the cron finally block ahead of the
      session close, so no in-flight title write can race the close.

    Returns the title actually persisted, or None if nothing could be set.
    """
    if not session_db or not session_id:
        return None
    title = (base_title or '').strip()
    if not title:
        return None
    try:
        session_db.set_session_title(session_id, title)
        return title
    except ValueError:
        next_title_fn = getattr(session_db, 'get_next_title_in_lineage', None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        session_db.set_session_title(session_id, deduped)
        return deduped

def _summarize_cron_failure_for_delivery(job: dict, error: str | None) -> str:
    """Return a compact one-line failure message for chat delivery.

    Full details stay in the cron output directory and the logs. Chat should
    show the operator what broke without dumping provider JSON, retry noise, or
    stack traces into the delivery channel.
    """
    job_name = job.get('name') or job.get('id') or 'cron job'
    text = (error or 'unknown error').strip()
    lower = text.lower()
    if '429' in text or 'rate limit' in lower or 'usage limit' in lower:
        reason = 'rate limit'
        if 'weekly usage limit' in lower:
            reason = 'weekly usage limit'
        elif 'quota' in lower:
            reason = 'quota limit'
        return f"⚠️ Cron '{job_name}' failed: provider {reason}. Fallback chain was exhausted or unavailable. Full details saved in cron output."
    if 'readtimeout' in lower or 'timed out' in lower or 'timeout' in lower:
        return f"⚠️ Cron '{job_name}' failed: provider timeout. Fallback chain was exhausted or unavailable. Full details saved in cron output."
    if re.search('authenticat|authoriz', lower) or re.search('\\b(401|403)\\b', text):
        return f"⚠️ Cron '{job_name}' failed: provider authentication error. Full details saved in cron output."
    cleaned = re.sub('^(RuntimeError|Exception|ValueError|HTTPStatusError):\\s*', '', text[:2000])
    cleaned = re.sub('\\s+', ' ', cleaned).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rstrip() + '...'
    return f"⚠️ Cron '{job_name}' failed: {cleaned}"

class CronPromptInjectionBlocked(Exception):
    """Raised by _build_job_prompt when the fully-assembled prompt trips the
    injection scanner. Caught in run_job so the operator sees a clean
    "job blocked" delivery instead of the scheduler crashing.

    Assembled-prompt scanning (including loaded skill content) plugs the
    gap from #3968: create-time scanning only covers the user-supplied
    prompt field; skill content loaded at runtime was never scanned, so a
    malicious skill could carry an injection payload that reached the
    non-interactive (auto-approve) cron agent.
    """

def _resolve_cron_disabled_toolsets(cfg: dict) -> list[str]:
    """Toolsets a cron-spawned agent must never receive.

    Four protected toolsets are always disabled in cron context:
      - ``cronjob`` — would let a cron-spawned agent schedule more cron jobs
      - ``messaging`` — interactive, needs a live gateway session
      - ``clarify`` — interactive, blocks waiting for user input
      - ``memory`` — cron agents are constructed with ``skip_memory=True``, so
        exposing this tool only gives the model an unbacked tool that fails

    User-level ``agent.disabled_toolsets`` from config.yaml is layered on top
    so per-job ``enabled_toolsets`` cannot bypass policy that applies to
    ordinary agent runs (#25752 — LLM-supplied enabled_toolsets was widening
    past config.yaml's denylist).
    """
    disabled = ['cronjob', 'messaging', 'clarify', 'memory']
    agent_cfg = (cfg or {}).get('agent') or {}
    user_disabled = agent_cfg.get('disabled_toolsets') or []
    for name in user_disabled:
        name = str(name).strip()
        if name and name not in disabled:
            disabled.append(name)
    return disabled

def _merge_mcp_into_per_job_toolsets(per_job: list[str], cfg: dict) -> list[str]:
    """Layer enabled MCP servers onto a per-job ``enabled_toolsets`` allowlist.

    A per-job list scopes the *native* toolsets, but on its own it silently
    drops every MCP server: ``discover_mcp_tools()`` registers the tools into
    the global registry, yet ``get_tool_definitions(enabled_toolsets=...)``
    only keeps toolsets named in the list. The agent then rejects every
    ``mcp_*`` call with "Unknown tool". This restores parity with
    ``_get_platform_tools`` MCP semantics:

      * ``no_mcp`` sentinel present  -> no MCP servers (sentinel stripped)
      * one or more MCP server names already listed -> treat as an allowlist,
        add nothing further (the user named exactly the servers they want)
      * otherwise -> union in every globally-enabled MCP server
    """
    result = [t for t in per_job if t != 'no_mcp']
    if 'no_mcp' in per_job:
        return result
    from hermes_cli.tools_config import enabled_mcp_server_names
    enabled_mcp = enabled_mcp_server_names(cfg)
    if set(result) & enabled_mcp:
        return result
    for name in sorted(enabled_mcp):
        if name not in result:
            result.append(name)
    return result

def _resolve_cron_enabled_toolsets(job: dict, cfg: dict) -> list[str] | None:
    """Resolve the toolset list for a cron job.

    Precedence:
    1. Per-job ``enabled_toolsets`` (set via ``cronjob`` tool on create/update).
       Keeps the agent's job-scoped toolset override intact — #6130. Enabled
       MCP servers are layered on per ``_merge_mcp_into_per_job_toolsets`` so a
       native-toolset allowlist does not silently strip MCP tools.
    2. Per-platform ``duck-agent tools`` config for the ``cron`` platform.
       Mirrors gateway behavior (``_get_platform_tools(cfg, platform_key)``)
       so users can gate cron toolsets globally without recreating every job.
    3. ``None`` on any lookup failure — AIAgent loads the full default set
       (legacy behavior before this change, preserved as the safety net).

    _DEFAULT_OFF_TOOLSETS ({moa, homeassistant, rl}) are removed by
    ``_get_platform_tools`` for unconfigured platforms, so fresh installs
    get cron WITHOUT ``moa`` by default (issue reported by Norbert —
    surprise $4.63 run).
    """
    per_job = job.get('enabled_toolsets')
    if per_job:
        return _merge_mcp_into_per_job_toolsets(list(per_job), cfg or {})
    try:
        from hermes_cli.tools_config import _get_platform_tools
        return sorted(_get_platform_tools(cfg or {}, 'cron'))
    except Exception as exc:
        logger.warning('Cron toolset resolution failed, falling back to full default toolset: %s', exc)
        return None
_KNOWN_DELIVERY_PLATFORMS = frozenset({'telegram', 'discord', 'slack', 'whatsapp', 'signal', 'matrix', 'mattermost', 'homeassistant', 'dingtalk', 'feishu', 'wecom', 'wecom_callback', 'weixin', 'sms', 'email', 'webhook', 'bluebubbles', 'qqbot', 'yuanbao'})
_HOME_TARGET_ENV_VARS = {'matrix': 'MATRIX_HOME_ROOM', 'telegram': 'TELEGRAM_HOME_CHANNEL', 'discord': 'DISCORD_HOME_CHANNEL', 'slack': 'SLACK_HOME_CHANNEL', 'signal': 'SIGNAL_HOME_CHANNEL', 'mattermost': 'MATTERMOST_HOME_CHANNEL', 'sms': 'SMS_HOME_CHANNEL', 'email': 'EMAIL_HOME_ADDRESS', 'dingtalk': 'DINGTALK_HOME_CHANNEL', 'feishu': 'FEISHU_HOME_CHANNEL', 'wecom': 'WECOM_HOME_CHANNEL', 'weixin': 'WEIXIN_HOME_CHANNEL', 'bluebubbles': 'BLUEBUBBLES_HOME_CHANNEL', 'qqbot': 'QQBOT_HOME_CHANNEL', 'whatsapp': 'WHATSAPP_HOME_CHANNEL', 'whatsapp_cloud': 'WHATSAPP_CLOUD_HOME_CHANNEL'}
_LEGACY_HOME_TARGET_ENV_VARS = {'QQBOT_HOME_CHANNEL': 'QQ_HOME_CHANNEL'}
from cron.jobs import get_due_jobs, mark_job_run, save_job_output, advance_next_runs, claim_dispatch, heartbeat_run_claim
from cron.executions import create_execution, finish_execution, mark_execution_running
SILENT_MARKER = '[SILENT]'

def _is_cron_silence_response(text: str) -> bool:
    """Return True when a cron final response should suppress delivery.

    Recognizes the bracketed ``[SILENT]`` sentinel (whole-response, first line,
    or last line) plus the bracketless ``SILENT`` / ``NO_REPLY`` / ``NO REPLY``
    variants the model emits when it drops the brackets (#51438, #46917).
    Whitespace-trimmed and case-insensitive.  A token buried mid-sentence is
    treated as real content and delivered.

    Delegates to the shared autonomous-lane matcher in
    :mod:`gateway.response_filters` (also used by the webhook adapter).
    """
    from gateway.response_filters import is_autonomous_silence_response
    return is_autonomous_silence_response(text)
_parallel_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_parallel_pool_max_workers: Optional[int] = None
_running_job_ids: set = set()
_running_lock = threading.Lock()
_interrupted_job_ids: set = set()

def get_running_job_ids() -> 'frozenset[str]':
    """Thread-safe snapshot of cron job IDs currently executing.

    A job ID is a member from the moment ``_submit_with_guard`` dispatches
    it onto the parallel/sequential pool until ``_process_job`` returns —
    i.e. for the job's *entire* run, tool calls included, not just the
    ticker's dispatch instant.

    The gateway shutdown path (``gateway/run.py::GatewayRunner.
    _drain_active_agents``) reads this to treat in-flight cron work as
    active the same way it already treats in-flight chat sessions via
    ``_running_agents`` — cron jobs run through their own thread pool here,
    entirely outside that dict, so without this the drain is structurally
    blind to them (#60432).
    """
    with _running_lock:
        return frozenset(_running_job_ids)

def try_register_running_job(job_id: str) -> bool:
    """Atomically add ``job_id`` to the in-flight running set.

    Returns False (without registering) when the job is already mid-run —
    the caller must skip the fire. This is the single dedupe owner shared by
    the ticker's ``_submit_with_guard`` and manual runs
    (``tools/cronjob_tools``): the fire claim alone cannot prevent a
    double-fire because its TTL (300s) is routinely outlived by real jobs,
    after which a manual ``cronjob(action='run')`` would claim successfully
    and run the same job concurrently (idea from #53395 by @izumi0uu).

    Registration also makes the run visible to ``get_running_job_ids`` (the
    gateway shutdown drain, #60432) and ``mark_running_jobs_interrupted``.
    Callers MUST pair a successful registration with
    ``release_running_job`` in a ``finally`` block.
    """
    with _running_lock:
        if job_id in _running_job_ids:
            return False
        _running_job_ids.add(job_id)
        return True

def release_running_job(job_id: str) -> None:
    """Remove ``job_id`` from the in-flight running set (idempotent)."""
    with _running_lock:
        _running_job_ids.discard(job_id)

def mark_running_jobs_interrupted(reason: str) -> list:
    """Best-effort: mark every currently in-flight cron job interrupted.

    Called by the gateway shutdown path immediately after it force-kills
    tool subprocesses (``process_registry.kill_all()``). A job whose tool
    subprocess was just killed out from under it must never be allowed to
    report success — even though its agent thread is still alive in this
    same process and may go on to produce a plausible-looking final
    response from the now-truncated tool output.

    Records the job IDs in ``_interrupted_job_ids`` BEFORE writing
    ``last_status`` so ``run_one_job``'s own eventual completion for the
    same job (racing in its own thread) sees the flag and skips its normal
    write instead of clobbering this one — see the check near the end of
    ``run_one_job``. This does not attempt to correlate the killed
    subprocess PID to a specific job ID (the process registry tracks PIDs,
    not cron job IDs); any job still dispatched at the moment of a forced
    kill is treated as interrupted, matching the coarser precedent already
    set by ``GatewayRunner._interrupt_running_agents``, which interrupts
    every entry in ``_running_agents`` on a drain timeout without
    per-agent correlation either.

    Returns the list of job IDs marked, for the caller to log.
    """
    with _running_lock:
        job_ids = list(_running_job_ids)
        _interrupted_job_ids.update(job_ids)
    marked = []
    for job_id in job_ids:
        try:
            mark_job_run(job_id, False, reason)
            marked.append(job_id)
        except Exception as e:
            logger.warning('Failed to mark job %s interrupted: %s', job_id, e)
    return marked

def _is_interrupted(job_id: str) -> bool:
    """Non-destructive peek at whether the shutdown path has marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` BEFORE it decides what to deliver — a job
    whose tool subprocess was killed mid-flight may still produce a
    plausible-looking ``final_response`` from the truncated output, and
    that must not go out to the user as if it were a normal result.
    Unlike ``_consume_interrupted_flag`` below, this does not clear the
    flag: the later, authoritative check (right before ``last_status`` is
    written) still needs to see it."""
    with _running_lock:
        return job_id in _interrupted_job_ids

def _consume_interrupted_flag(job_id: str) -> bool:
    """Return True and clear the flag if the shutdown path already marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` right before it would otherwise write its own
    ``last_status``. Consuming (discarding) rather than just checking keeps
    the flag from leaking across a later, unrelated run of the same job ID
    (recurring jobs reuse their ID every fire)."""
    with _running_lock:
        if job_id in _interrupted_job_ids:
            _interrupted_job_ids.discard(job_id)
            return True
        return False
_sequential_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

class _ReadWriteLock:
    """Writer-preferring readers-writer lock.

    Guards the process-global ``os.environ["TERMINAL_CWD"]`` override that a
    workdir cron job applies for the whole of its agent run.  Workdir jobs are
    writers: they mutate the shared env and need exclusive access.  Workdir-less
    jobs are readers: they only observe ``TERMINAL_CWD`` (indirectly, via the
    terminal / file / code-exec tools), so any number of them may run
    concurrently with each other, but none may run alongside a writer — that is
    exactly what stops a workdir-less job from picking up another job's workdir
    override and running its commands in the wrong directory.

    Writer preference bounds the wait for a workdir job (dispatched on the
    single-thread sequential pool) so a stream of workdir-less readers cannot
    starve it.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    def acquire_read(self, timeout: float | None=None) -> bool:
        """Acquire a read lock.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        A timed-out caller proceeds without the lock (degraded mode) —
        see the call-site in ``run_job`` for the logging / trade-off.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._cond.notify_all()
                        return False
                    self._cond.wait(timeout=remaining)
                else:
                    self._cond.wait()
            self._readers += 1
        return True

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self, timeout: float | None=None) -> bool:
        """Acquire a write lock.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        A timed-out caller proceeds without the lock (degraded mode).
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._cond.notify_all()
                            return False
                        self._cond.wait(timeout=remaining)
                    else:
                        self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer_active = True
        return True

    def release_write(self) -> None:
        with self._cond:
            self._writer_active = False
            self._cond.notify_all()
_terminal_cwd_lock = _ReadWriteLock()
_CWD_LOCK_TIMEOUT_FLOOR_SECONDS = 120.0
_CWD_LOCK_TIMEOUT_MARGIN_SECONDS = 60.0

def _cron_inactivity_seconds() -> float:
    """Parse HERMES_CRON_TIMEOUT (seconds). 0 = unlimited; bad input = 600.

    Shared by run_job's inactivity monitor (which maps 0 to "no limit") and
    the cwd-lock bound below (which keeps the wait bounded regardless) so
    the two sites cannot drift apart — the lock bound must stay at or above
    the inactivity limit or waiters would fail while a healthy holder runs.
    """
    raw = os.getenv('HERMES_CRON_TIMEOUT', '').strip()
    if not raw:
        return 600.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning('Invalid HERMES_CRON_TIMEOUT=%r; using default 600s', raw)
        return 600.0

def _cwd_lock_timeout_seconds() -> float:
    """Bound for the TERMINAL_CWD lock wait: inactivity limit + margin."""
    inactivity = _cron_inactivity_seconds()
    if inactivity <= 0:
        inactivity = 600.0
    return max(inactivity, _CWD_LOCK_TIMEOUT_FLOOR_SECONDS) + _CWD_LOCK_TIMEOUT_MARGIN_SECONDS

def _get_parallel_pool(max_workers: Optional[int]) -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent parallel pool."""
    global _parallel_pool, _parallel_pool_max_workers
    if _parallel_pool is None or _parallel_pool_max_workers != max_workers:
        if _parallel_pool is not None:
            _parallel_pool.shutdown(wait=False, cancel_futures=False)
        _parallel_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='cron-parallel')
        _parallel_pool_max_workers = max_workers
    return _parallel_pool

def _get_sequential_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent single-thread sequential pool.

    A single worker guarantees env-mutating jobs never overlap, even
    across ticks: a job queued by a newer tick waits for the previous tick's
    sequential jobs to finish rather than corrupting their os.environ
    state.
    """
    global _sequential_pool
    if _sequential_pool is None:
        _sequential_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='cron-seq')
    return _sequential_pool

def _shutdown_parallel_pool() -> None:
    """Shut down the persistent pools on process exit."""
    global _parallel_pool, _parallel_pool_max_workers, _sequential_pool
    if _parallel_pool is not None:
        _parallel_pool.shutdown(wait=True, cancel_futures=False)
        _parallel_pool = None
        _parallel_pool_max_workers = None
    if _sequential_pool is not None:
        _sequential_pool.shutdown(wait=True, cancel_futures=False)
        _sequential_pool = None
atexit.register(_shutdown_parallel_pool)

def _usage_audit_path() -> Path:
    return _get_hermes_home() / 'cron' / 'usage_audit.jsonl'

def _utcnow_iso_ms() -> str:
    """RFC3339 UTC timestamp with millisecond precision and 'Z' suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'

def _write_usage_audit(record: dict) -> None:
    """Append a single JSONL line to ~/.duck-agent/cron/usage_audit.jsonl.

    NEVER raises — a logger bug must not break cron jobs. Wraps the entire
    write (path resolve, mkdir, json.dumps, file append) in a single try.
    """
    try:
        path = _usage_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception as e:
        logger.warning('usage_audit write failed: %s', e)

def _interpreter_shutting_down(exc: Optional[BaseException]=None) -> bool:
    """True when the Python interpreter is finalizing.

    A cron tick can fire while the gateway is tearing down — SIGTERM from
    ``duck-agent update`` / ``duck-agent gateway stop`` / systemd restart, or an
    OOM-kill. Once finalization starts, ``concurrent.futures`` refuses new
    work with ``RuntimeError: cannot schedule new futures after interpreter
    shutdown`` and asyncio's default executor is gone, so *any* attempt to
    schedule delivery (live-adapter, ``asyncio.run``, or a fresh pool) is
    doomed and only pollutes ``errors.log`` with a traceback. Callers use
    this to skip gracefully with a warning instead of crashing (#58720,
    #55924).

    ``exc`` lets a caller also treat an already-raised scheduling error as a
    shutdown signal: the ``concurrent.futures`` module-global flag can be set
    a hair before ``sys.is_finalizing()`` flips, so matching the error text is
    a safe fallback for that race.
    """
    if sys.is_finalizing():
        return True
    if exc is not None:
        return 'cannot schedule new futures' in str(exc).lower()
    return False
_hermes_home: Path | None = None

def _get_hermes_home() -> Path:
    """Resolve Duck Agent home dynamically while preserving test monkeypatch hooks.

    Cron is per-profile by design (#4707): the in-process ticker runs inside a
    profile-scoped gateway, so resolving the active DUCK_AGENT_HOME at call time
    means a profile's jobs are stored AND executed under that profile's home
    (its .env, config.yaml, scripts, skills). Do not freeze this at import or
    anchor it at the shared default root — either re-breaks profile isolation.
    """
    return _hermes_home or get_hermes_home()

def _get_lock_paths() -> tuple[Path, Path]:
    """Resolve cron lock paths at call time so profile/env changes are honored."""
    hermes_home = _get_hermes_home()
    lock_dir = hermes_home / 'cron'
    return (lock_dir, lock_dir / '.tick.lock')

def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job, preserving any extra routing metadata.

    Treats non-dict origins (free-form provenance strings, ints, lists from
    migration scripts or hand-edited jobs.json) as missing instead of
    crashing with ``AttributeError`` on ``origin.get(...)``. Without this
    guard, a job tagged with e.g. ``"combined-digest-replaces-x-and-y"``
    crashed every fire attempt with
    ``'str' object has no attribute 'get'`` — ``mark_job_run`` recorded the
    failure, but the next tick re-loaded the same poisoned origin and
    crashed identically until the field was patched manually (#18722).
    """
    origin = job.get('origin')
    if not isinstance(origin, dict):
        return None
    platform = origin.get('platform')
    chat_id = origin.get('chat_id')
    if platform and chat_id:
        return origin
    return None

def _cron_mirror_delivery_enabled(job: dict, cfg: Optional[dict]=None) -> bool:
    """Whether a cron delivery should also be mirrored into the target chat's
    gateway session transcript.

    Default OFF — preserves the historical isolation guarantee (cron deliveries
    live only in the cron job's own session, never the target chat's history)
    byte-for-byte for everyone who does not opt in.

    Precedence (first decisive value wins):
      1. Per-job ``attach_to_session`` (bool) — set via the ``cronjob`` tool,
         lets one briefing job opt in without flipping global behaviour.
      2. Global ``cron.mirror_delivery`` (bool) in config.yaml.
      3. False.

    When enabled, the cron's final output is appended to the target session as
    an assistant turn via the existing ``gateway.mirror.mirror_to_session`` —
    the same primitive ``send_message`` uses — so the next user reply in that
    chat sees the brief in context (no "what is Task #2?" amnesia). This is
    alternation- and cache-safe: the append lands at a turn boundary between
    user turns, never mid-loop, and never mutates the cached system prompt.
    """
    per_job = job.get('attach_to_session')
    if isinstance(per_job, bool):
        return per_job
    try:
        if cfg is None:
            cfg = load_config() or {}
        return bool((cfg.get('cron', {}) or {}).get('mirror_delivery', False))
    except Exception:
        return False

def _target_matches_origin(origin: dict, platform_name: str, chat_id: str, thread_id: Optional[str]) -> bool:
    """True when a delivery target is the job's own origin conversation.

    Mirroring is scoped to the origin session by design (see
    ``_maybe_mirror_cron_delivery``). A job created from a live gateway chat
    stamps that chat as ``origin`` (``cronjob_tools._origin_from_env``), and
    that session is guaranteed to exist — it is the very conversation the user
    was in when they scheduled the job. Fan-out targets (``deliver=all``,
    explicit ``platform:chat_id`` to some *other* chat, or a home-channel
    fallback for an origin-less API/script job) are deliberately NOT mirrored:
    they are broadcasts, not a continuation of a conversation, and may point at
    a chat the user never opened an agent session in.

    This makes the historical "cold-start" worry a non-case: when the mirror
    semantically applies (target == origin) the session always exists; when no
    session exists, the target was never the origin conversation, so we simply
    do not mirror.
    """
    if not origin:
        return False
    if str(origin.get('platform', '')).lower() != str(platform_name).lower():
        return False
    if str(origin.get('chat_id', '')) != str(chat_id):
        return False
    origin_thread = origin.get('thread_id')
    if origin_thread is not None and str(origin_thread) != str(thread_id or ''):
        return False
    return True

def _maybe_mirror_cron_delivery(job: dict, platform_name: str, chat_id: str, mirror_text: str, thread_id: Optional[str]=None, user_id: Optional[str]=None, *, enabled: bool=False) -> None:
    """Best-effort mirror of a cron delivery into the origin chat's session.

    No-op unless ``enabled`` (resolved once by the caller, and already scoped to
    the origin target — see ``_target_matches_origin``). Reuses the shipped
    ``mirror_to_session`` so cron rides exactly the same path that interactive
    ``send_message`` mirroring already uses, including passing ``user_id`` so a
    per-user-isolated group chat resolves to the exact member who scheduled the
    job (parity with ``send_message``). All failures are swallowed — a delivery
    that succeeded must never be reported as failed because the transcript
    mirror hit a problem.

    Because the caller only enables this for the target that equals the job's
    origin conversation, the session is expected to exist (the job was born in
    that session). A missing session therefore indicates an origin-less /
    fan-out delivery that should not have been mirrored anyway, and is treated
    as a silent no-op — never a synthetic session is created.
    """
    if not enabled:
        return
    text = (mirror_text or '').strip()
    if not text:
        return
    try:
        from gateway.mirror import mirror_to_session
        ok = mirror_to_session(platform_name, str(chat_id), f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}", source_label='cron', thread_id=thread_id, user_id=user_id, role='user')
        if ok:
            logger.info("Job '%s': mirrored delivery into %s:%s session transcript", job.get('id', '?'), platform_name, chat_id)
        else:
            logger.debug("Job '%s': delivery mirror skipped for %s:%s (no matching gateway session — cold start)", job.get('id', '?'), platform_name, chat_id)
    except Exception as e:
        logger.debug("Job '%s': delivery mirror failed for %s:%s: %s", job.get('id', '?'), platform_name, chat_id, e)

def _open_continuable_cron_thread(job: dict, adapter, chat_id: str, loop) -> Optional[str]:
    """Open a dedicated thread for a continuable cron job (thread-preferred).

    Returns the new ``thread_id`` on success, or ``None`` when the platform has
    no thread primitive (WhatsApp/Signal/SMS) or creation failed — the ``None``
    return is the caller's signal to fall back to the origin-DM mirror, the same
    open-thread-or-fallback shape as ``GatewayRunner._process_handoff``. Reuses
    the shipped ``adapter.create_handoff_thread``; no new adapter surface.
    """
    create_thread = getattr(adapter, 'create_handoff_thread', None)
    if not callable(create_thread) or loop is None:
        return None
    task_name = job.get('name') or job.get('id', 'cron')
    thread_name = f'Duck Agent — {task_name}'
    try:
        from agent.async_utils import safe_schedule_threadsafe
        coro = create_thread(str(chat_id), thread_name)
        future = safe_schedule_threadsafe(coro, loop)
        if future is None:
            return None
        new_thread_id = future.result(timeout=30)
        return str(new_thread_id) if new_thread_id else None
    except Exception as e:
        logger.debug("Job '%s': create_handoff_thread failed on %s — falling back to DM-session mirror: %s", job.get('id', '?'), getattr(adapter, 'name', '?'), e)
        return None

def _seed_cron_thread_session(job: dict, adapter, platform_name: str, chat_id: str, thread_id: str, mirror_text: str, chat_name: Optional[str]=None) -> None:
    """Seed the freshly-opened cron thread's session with the brief.

    Without this the brief is *visible* in the new thread but absent from any
    transcript, so the user's first reply in-thread would hit a session with no
    record of it ("what is Task #2?"). We create the thread-keyed session (the
    same key the user's reply will resolve to — ``build_session_key`` keys
    threads as participant-shared, so no ``user_id`` is needed) and append the
    brief as an assistant turn via the shipped ``mirror_to_session``.

    Mirrors ``GatewayRunner._process_handoff``'s seed step, but standalone:
    cron reaches the live ``SessionStore`` through the adapter's
    ``_session_store`` handle rather than the gateway object. Best-effort — a
    delivery that already succeeded is never failed by a seeding problem.
    """
    text = (mirror_text or '').strip()
    if not text:
        return
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource
        session_store = getattr(adapter, '_session_store', None)
        if session_store is not None:
            try:
                platform_enum = Platform(platform_name.lower())
            except (ValueError, KeyError):
                platform_enum = None
            if platform_enum is not None:
                if platform_enum == Platform.DISCORD:
                    seed_chat_id = str(thread_id)
                else:
                    seed_chat_id = str(chat_id)
                dest_source = SessionSource(platform=platform_enum, chat_id=seed_chat_id, chat_name=chat_name, chat_type='thread', user_id='system:cron', user_name='Cron', thread_id=str(thread_id))
                session_store.get_or_create_session(dest_source)
        from gateway.mirror import mirror_to_session
        mirror_to_session(platform_name, str(chat_id), f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}", source_label='cron', thread_id=str(thread_id), user_id='system:cron', role='user')
        logger.info("Job '%s': opened continuable thread %s on %s:%s and seeded the brief", job.get('id', '?'), thread_id, platform_name, chat_id)
    except Exception as e:
        logger.debug("Job '%s': seeding cron thread session failed for %s:%s:%s: %s", job.get('id', '?'), platform_name, chat_id, thread_id, e)

def _seed_cron_channel_session(job: dict, adapter, platform_name: str, chat_id: str, mirror_text: str, *, is_dm: bool, user_id: Optional[str], chat_name: Optional[str]=None) -> bool:
    """Seed the FLAT (thread_id=None) session for an ``in_channel`` cron delivery.

    The ``in_channel`` surface (D1/D2) delivers the brief flat into the channel
    with no thread, so the continuation surface is the whole-channel /
    whole-DM session keyed ``thread_id=None`` — the same bucket
    ``reply_in_thread: false`` routes an inbound plain reply to.

    Unlike the thread path, the shipped delivery-mirror alone is NOT sufficient
    here: ``mirror_to_session`` only APPENDS to a session that already EXISTS
    (``_find_session_id`` → no-op when none matches), and a flat channel
    ``(…, None)`` row is only created when a human posts a top-level message the
    bot processes — a ``chat_postMessage`` cron delivery never goes through the
    inbound handler, so the row is usually absent and the mirror silently drops
    the brief (verified live: the brief never landed, the reply had no context).
    So we CREATE the flat session row first, exactly like
    ``_seed_cron_thread_session`` does for threads, then mirror into it.

    The session KEY must match what the user's later inbound reply resolves to
    (``build_session_key``):
    - **Channel** (``chat_type="group"``): key is
      ``…:group:<chat_id>:<user_id>`` — user-isolated — so the seed MUST carry
      the **origin's real ``user_id``** (the member who scheduled the job), NOT
      a synthetic ``system:cron`` id, or the reply keys to a different session.
    - **1:1 DM** (``chat_type="dm"``): the key is ``…:dm:<chat_id>`` and does
      NOT embed ``user_id``, so any ``user_id`` resolves to the same session.
    ``chat_type`` mirrors the inbound handler's own choice
    (``"dm" if is_dm else "group"``, ``adapter.py``), so the seeded key is
    byte-identical to the reply's key.

    Returns True if a seed row was created and the brief mirrored, else False
    (caller falls back to the plain mirror). Best-effort — a delivery that
    already succeeded is never failed by a seeding problem.
    """
    text = (mirror_text or '').strip()
    if not text:
        return False
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource
        chat_type = 'dm' if is_dm else 'group'
        session_store = getattr(adapter, '_session_store', None)
        if session_store is not None:
            try:
                platform_enum = Platform(platform_name.lower())
            except (ValueError, KeyError):
                platform_enum = None
            if platform_enum is not None:
                dest_source = SessionSource(platform=platform_enum, chat_id=str(chat_id), chat_name=chat_name, chat_type=chat_type, user_id=str(user_id) if user_id else None, thread_id=None)
                session_store.get_or_create_session(dest_source)
        from gateway.mirror import mirror_to_session
        ok = mirror_to_session(platform_name, str(chat_id), f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}", source_label='cron', thread_id=None, user_id=str(user_id) if user_id else None, role='user')
        if ok:
            logger.info("Job '%s': seeded flat in_channel session on %s:%s (chat_type=%s)", job.get('id', '?'), platform_name, chat_id, chat_type)
        return bool(ok)
    except Exception as e:
        logger.debug("Job '%s': seeding in_channel session failed for %s:%s: %s", job.get('id', '?'), platform_name, chat_id, e)
        return False

def _cron_job_origin_log_suffix(job: dict) -> str:
    """Return safe provenance details for security warnings about a cron job.

    The scheduler normally has no live HTTP request object when it detects a
    bad stored ``context_from`` reference. Including the job's saved origin
    makes future probe logs actionable without exposing secrets: platform/chat
    metadata for gateway-created jobs, and optional source-IP fields for API
    surfaces that persist them in origin metadata.
    """
    origin = job.get('origin')
    if not isinstance(origin, dict):
        return ''
    fields = []
    for key in ('platform', 'chat_id', 'thread_id', 'source_ip', 'remote', 'forwarded_for'):
        value = origin.get(key)
        if value is None:
            continue
        text = str(value).replace('\r', ' ').replace('\n', ' ').strip()
        if text:
            fields.append(f'origin_{key}={text[:200]!r}')
    return ' ' + ' '.join(fields) if fields else ''

def _plugin_cron_env_var(platform_name: str) -> str:
    """Return the cron home-channel env var registered by a plugin platform.

    Falls through the platform registry so plugins that set
    ``cron_deliver_env_var`` on their ``PlatformEntry`` get cron delivery
    support without editing this module.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name.lower())
        if entry and entry.cron_deliver_env_var:
            return entry.cron_deliver_env_var
    except Exception:
        pass
    return ''

def _is_known_delivery_platform(platform_name: str) -> bool:
    """Whether ``platform_name`` is a valid cron delivery target.

    Hardcoded built-ins in ``_KNOWN_DELIVERY_PLATFORMS`` are checked first;
    plugin platforms registered via ``PlatformEntry`` are accepted if they
    provide a ``cron_deliver_env_var``.
    """
    name = platform_name.lower()
    if name in _KNOWN_DELIVERY_PLATFORMS:
        return True
    return bool(_plugin_cron_env_var(name))

def _resolve_home_env_var(platform_name: str) -> str:
    """Return the env var name for a platform's cron home channel.

    Built-in platforms are in ``_HOME_TARGET_ENV_VARS``; plugin platforms are
    resolved from the platform registry.
    """
    name = platform_name.lower()
    env_var = _HOME_TARGET_ENV_VARS.get(name)
    if env_var:
        return env_var
    return _plugin_cron_env_var(name)

def _get_home_target_chat_id(platform_name: str) -> str:
    """Return the configured home target chat/room ID for a delivery platform."""
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return ''
    value = os.getenv(env_var, '')
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(legacy, '')
    return value

def _get_home_target_thread_id(platform_name: str) -> Optional[str]:
    """Return the optional thread/topic ID for a platform home target.

    Telegram-only override: ``TELEGRAM_CRON_THREAD_ID`` takes precedence over
    ``TELEGRAM_HOME_CHANNEL_THREAD_ID`` for cron delivery. When topic mode is
    enabled, deliveries that land in the root DM (thread_id unset) end up in
    the system-only lobby where the user cannot reply — the gateway returns
    the lobby reminder and drops ``reply_to_message_id`` (#24409). Pointing
    cron at a dedicated topic via this env var lets replies work as expected
    without changing the lobby invariant.
    """
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return None
    if platform_name.lower() == 'telegram':
        cron_thread = os.getenv('TELEGRAM_CRON_THREAD_ID', '').strip()
        if cron_thread:
            return cron_thread
    value = os.getenv(f'{env_var}_THREAD_ID', '').strip()
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(f'{legacy}_THREAD_ID', '').strip()
    return value or None

def _iter_home_target_platforms():
    """Iterate built-in + plugin platform names that expose a home channel.

    Used by the ``deliver=origin`` fallback when the job has no origin.
    """
    for name in _HOME_TARGET_ENV_VARS:
        yield name
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.cron_deliver_env_var and entry.name not in _HOME_TARGET_ENV_VARS:
                yield entry.name
    except Exception:
        pass

def cron_delivery_targets() -> list[dict]:
    """Return the platforms a cron job can auto-deliver to.

    Single source of truth for any UI (dashboard dropdown, etc.) that lets a
    user pick a cron delivery target. A platform is included when it is a valid
    cron delivery platform AND its gateway is configured (enabled + credentials
    present). Each entry reports whether the platform's home target (the
    room/channel cron posts to) is set — a platform can be configured for
    interactive use but still lack the home target an unattended cron job needs.

    Returns a list of dicts: ``{"id", "name", "home_target_set", "home_env_var"}``
    ordered by the gateway's canonical platform order. Callers should always
    prepend the implicit ``local`` option themselves — it needs no config.
    """
    targets: list[dict] = []
    try:
        from gateway.config import load_gateway_config
        gateway_config = load_gateway_config()
        connected = {p.value for p in gateway_config.get_connected_platforms()}
    except Exception:
        logger.debug('cron_delivery_targets: gateway config unavailable', exc_info=True)
        connected = set()
    for name in _iter_home_target_platforms():
        if name not in connected:
            continue
        if not _is_known_delivery_platform(name):
            continue
        env_var = _resolve_home_env_var(name)
        targets.append({'id': name, 'name': name.replace('_', ' ').title(), 'home_target_set': bool(_get_home_target_chat_id(name)), 'home_env_var': env_var or None})
    return targets

def _resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
    """Resolve one concrete auto-delivery target for a cron job."""
    origin = _resolve_origin(job)
    if deliver_value == 'local':
        return None
    if deliver_value == 'origin':
        if origin:
            return {'platform': origin['platform'], 'chat_id': str(origin['chat_id']), 'thread_id': origin.get('thread_id')}
        for platform_name in _iter_home_target_platforms():
            chat_id = _get_home_target_chat_id(platform_name)
            if chat_id:
                logger.info("Job '%s' has deliver=origin but no origin; falling back to %s home channel", job.get('name', job.get('id', '?')), platform_name)
                return {'platform': platform_name, 'chat_id': chat_id, 'thread_id': _get_home_target_thread_id(platform_name)}
        return None
    if ':' in deliver_value:
        platform_name, rest = deliver_value.split(':', 1)
        platform_key = platform_name.lower()
        from tools.send_message_tool import _parse_target_ref
        parsed_chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_key, rest)
        if is_explicit:
            chat_id, thread_id = (parsed_chat_id, parsed_thread_id)
        else:
            chat_id, thread_id = (rest, None)
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_key, chat_id)
            if resolved:
                parsed_chat_id, parsed_thread_id, resolved_is_explicit = _parse_target_ref(platform_key, resolved)
                if resolved_is_explicit:
                    chat_id = parsed_chat_id
                    if parsed_thread_id is not None:
                        thread_id = parsed_thread_id
                else:
                    chat_id = resolved
        except Exception:
            pass
        if thread_id is None and platform_key == 'slack' and origin and (str(origin.get('platform') or '').lower() == platform_key) and (str(origin.get('chat_id')) == str(chat_id)) and origin.get('thread_id'):
            thread_id = origin.get('thread_id')
        return {'platform': platform_name, 'chat_id': chat_id, 'thread_id': thread_id}
    platform_name = deliver_value
    if origin and origin.get('platform') == platform_name:
        chat_id = _get_home_target_chat_id(platform_name)
        if chat_id:
            return {'platform': platform_name, 'chat_id': chat_id, 'thread_id': _get_home_target_thread_id(platform_name)}
        return {'platform': platform_name, 'chat_id': str(origin['chat_id']), 'thread_id': origin.get('thread_id')}
    if not _is_known_delivery_platform(platform_name):
        return None
    chat_id = _get_home_target_chat_id(platform_name)
    if not chat_id:
        return None
    return {'platform': platform_name, 'chat_id': chat_id, 'thread_id': _get_home_target_thread_id(platform_name)}

def _normalize_deliver_value(deliver) -> str:
    """Normalize a stored/submitted ``deliver`` value to its canonical string form.

    The contract is that ``deliver`` is a string (``"local"``, ``"origin"``,
    ``"telegram"``, ``"telegram:-1001:17"``, or comma-separated combinations).
    Historically some callers — MCP clients passing an array, direct edits of
    ``jobs.json``, or stale code paths — have stored a list/tuple like
    ``["telegram"]``.  ``str(["telegram"])`` would serialize to the literal
    string ``"['telegram']"``, which is not a known platform and fails
    resolution silently.  Flatten lists/tuples into a comma-separated string
    so both forms work.  Returns ``"local"`` for anything falsy.
    """
    if deliver is None or deliver == '':
        return 'local'
    if isinstance(deliver, (list, tuple)):
        parts = [str(p).strip() for p in deliver if str(p).strip()]
        return ','.join(parts) if parts else 'local'
    return str(deliver)
_ROUTING_TOKENS = frozenset({'all'})

def _expand_routing_tokens(part: str) -> List[str]:
    """Expand a routing-intent token to concrete platform names.

    ``all`` expands to every platform in ``_iter_home_target_platforms()``
    that has a configured home chat_id right now.  Unknown / non-token
    values pass through unchanged as a single-element list, so the caller
    can treat every token uniformly.
    """
    token = part.lower()
    if token not in _ROUTING_TOKENS:
        return [part]
    expanded: List[str] = []
    for platform_name in _iter_home_target_platforms():
        if _get_home_target_chat_id(platform_name):
            expanded.append(platform_name)
    return expanded

def _resolve_delivery_targets(job: dict) -> List[dict]:
    """Resolve all concrete auto-delivery targets for a cron job.

    Accepts the legacy comma-separated ``deliver`` string plus the
    ``all`` routing-intent token, which expands to every platform with
    a configured home channel.  Tokens may be combined with explicit
    targets: ``origin,all`` and ``all,telegram:-100:17`` both work.
    Duplicate (platform, chat_id, thread_id) tuples are collapsed by the
    existing dedup pass.
    """
    deliver = _normalize_deliver_value(job.get('deliver', 'local'))
    if deliver == 'local':
        return []
    raw_parts = [p.strip() for p in deliver.split(',') if p.strip()]
    parts: List[str] = []
    for raw in raw_parts:
        parts.extend(_expand_routing_tokens(raw))
    seen = set()
    targets = []
    for part in parts:
        target = _resolve_single_delivery_target(job, part)
        if target:
            key = (target['platform'].lower(), str(target['chat_id']), target.get('thread_id'))
            if key not in seen:
                seen.add(key)
                targets.append(target)
    return targets

def _resolve_delivery_target(job: dict) -> Optional[dict]:
    """Resolve the concrete auto-delivery target for a cron job, if any."""
    targets = _resolve_delivery_targets(job)
    return targets[0] if targets else None
_VIDEO_EXTS = frozenset({'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'})
_IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif'})

def _send_media_via_adapter(adapter, chat_id: str, media_files: list, metadata: dict | None, loop, job: dict, platform=None) -> None:
    """Send extracted MEDIA files as native platform attachments via a live adapter.

    Routes each file to the appropriate adapter method (send_voice, send_image_file,
    send_video, send_document) based on file extension — mirroring the routing logic
    in ``BasePlatformAdapter._process_message_background``.
    """
    from pathlib import Path
    from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    for media_path, _is_voice in media_files:
        try:
            ext = Path(media_path).suffix.lower()
            route_platform = platform if platform is not None else getattr(adapter, 'platform', None)
            if should_send_media_as_audio(route_platform, ext, is_voice=_is_voice):
                coro = adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                coro = adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
            else:
                coro = adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=metadata)
            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(coro, loop)
            if future is None:
                logger.warning("Job '%s': cannot send media %s, gateway loop unavailable", job.get('id', '?'), media_path)
                return
            try:
                result = future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                raise
            if result and (not getattr(result, 'success', True)):
                logger.warning("Job '%s': media send failed for %s: %s", job.get('id', '?'), media_path, getattr(result, 'error', 'unknown'))
        except Exception as e:
            logger.warning("Job '%s': failed to send media %s: %s", job.get('id', '?'), media_path, e)

def _confirm_adapter_delivery(send_result) -> bool:
    """Return True only if ``send_result`` unambiguously confirms delivery.

    A live adapter that returns ``None`` (e.g. a swallowed exception, a busy
    platform, or a code path that returns early without producing a
    ``SendResult``) must NOT be treated as success — doing so causes the
    scheduler to log ``"delivered to <chat> via live adapter"`` while the
    gateway never actually sees the message (#47056).

    Likewise, an object missing a ``success`` attribute (e.g. a bare ``dict``
    or a partial mock) is a contract violation: it does not actually tell us
    whether the send succeeded.  Require an explicit, truthy ``success``
    attribute to count as confirmed.
    """
    if send_result is None:
        return False
    if not hasattr(send_result, 'success'):
        return False
    return bool(getattr(send_result, 'success'))

def _is_channel_dm_topic(runtime_adapter: Any, chat_id: Any, loop: Any, job_id: str) -> bool:
    """Decide whether an (already-ambiguous) Telegram topic target is a genuine
    Bot API *channel* Direct-Messages topic (route via
    ``direct_messages_topic_id``) rather than a forum-style topic in a private
    chat (route via ``message_thread_id``).

    Callers gate this on the ambiguous shape first
    (``telegram:<positive_chat_id>:<numeric_thread_id>``) — that shape is
    identical for both cases, so shape alone cannot decide (this was the #52060
    regression).  The real signal is the chat *type*: a genuine channel DM topic
    lives on a ``channel`` chat.  Probe the live adapter's ``get_chat_info`` once
    and only return True when the chat is a channel.

    Fails SAFE to ``message_thread_id`` (returns False) for adapters without a
    probe, or any probe error/timeout — that is the pre-#22773 behaviour and the
    correct default for the common forum-topic case.
    """
    get_chat_info = getattr(type(runtime_adapter), 'get_chat_info', None)
    if not callable(get_chat_info):
        return False
    try:
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(get_chat_info(runtime_adapter, str(chat_id)), loop)
        if future is None:
            return False
        info = future.result(timeout=10)
    except Exception:
        logger.debug("Job '%s': get_chat_info probe failed for chat=%s — defaulting to message_thread_id routing", job_id, chat_id, exc_info=True)
        return False
    is_channel = isinstance(info, dict) and str(info.get('type') or '').lower() == 'channel'
    if is_channel:
        logger.info("Job '%s': chat=%s is a channel — routing via direct_messages_topic_id", job_id, chat_id)
    return is_channel

def _deliver_result(job: dict, content: str, adapters=None, loop=None) -> Optional[str]:
    """
    Deliver job output to the configured target(s) (origin chat, specific platform, etc.).

    When ``adapters`` and ``loop`` are provided (gateway is running), tries to
    use the live adapter first — this supports E2EE rooms (e.g. Matrix) where
    the standalone HTTP path cannot encrypt.  Falls back to standalone send if
    the adapter path fails or is unavailable.

    Returns None on success, or an error string on failure.
    """
    targets = _resolve_delivery_targets(job)
    if not targets:
        deliver_value = _normalize_deliver_value(job.get('deliver', 'local'))
        if deliver_value == 'local':
            return None
        if deliver_value == 'origin':
            logger.info("Job '%s': deliver=origin but no origin or home channels — skipping delivery (output saved in last_output)", job.get('name', job.get('id', '?')))
            return None
        msg = f'no delivery target resolved for deliver={deliver_value}'
        logger.warning("Job '%s': %s", job['id'], msg)
        return msg
    from tools.send_message_tool import _send_to_platform
    from gateway.config import load_gateway_config, Platform
    wrap_response = True
    user_cfg = None
    try:
        user_cfg = load_config()
        wrap_response = user_cfg.get('cron', {}).get('wrap_response', True)
    except Exception:
        pass
    if wrap_response:
        task_name = job.get('name', job['id'])
        job_id = job.get('id', '')
        delivery_content = f'Cronjob Response: {task_name}\n(job_id: {job_id})\n-------------\n\n{content}\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder {task_name}").'
    else:
        delivery_content = content
    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    try:
        mirror_enabled = _cron_mirror_delivery_enabled(job, user_cfg)
    except Exception:
        mirror_enabled = False
    mirror_text = ''
    if mirror_enabled:
        _, mirror_text = BasePlatformAdapter.extract_media(content)
        mirror_text = (mirror_text or '').strip()
    try:
        config = load_gateway_config()
    except Exception as e:
        msg = f'failed to load gateway config: {e}'
        logger.error("Job '%s': %s", job['id'], msg)
        return msg
    delivery_errors = []
    for target in targets:
        platform_name = target['platform']
        chat_id = target['chat_id']
        thread_id = target.get('thread_id')
        origin = _resolve_origin(job) or {}
        origin_thread = origin.get('thread_id')
        if origin_thread and (not thread_id):
            logger.warning("Job '%s': origin has thread_id=%s but delivery target lost it (deliver=%s, target=%s)", job['id'], origin_thread, job.get('deliver', 'local'), target)
        elif thread_id:
            logger.debug("Job '%s': delivering to %s:%s thread_id=%s", job['id'], platform_name, chat_id, thread_id)
        mirror_this_target = mirror_enabled and _target_matches_origin(origin, platform_name, chat_id, thread_id)
        origin_user_id = origin.get('user_id') if mirror_this_target else None
        try:
            platform = Platform(platform_name.lower())
        except (ValueError, KeyError):
            msg = f"unknown platform '{platform_name}'"
            logger.warning("Job '%s': %s", job['id'], msg)
            delivery_errors.append(msg)
            continue
        from gateway.delivery import resolve_delivery_transport
        transport = resolve_delivery_transport(platform, config, adapters)
        if transport is not None:
            pconfig = transport.config
            runtime_adapter = transport.adapter
        else:
            pconfig = config.platforms.get(platform)
            runtime_adapter = None
        if not pconfig or not pconfig.enabled:
            msg = f"platform '{platform_name}' not configured/enabled"
            logger.warning("Job '%s': %s", job['id'], msg)
            delivery_errors.append(msg)
            continue
        live_adapter_ready = runtime_adapter is not None and loop is not None and getattr(loop, 'is_running', lambda: False)()
        delivered = False
        target_errors = []
        surface_mode = 'thread'
        try:
            surface_raw = (pconfig.extra or {}).get('cron_continuable_surface')
            if surface_raw is not None and str(surface_raw).strip().lower() == 'in_channel':
                surface_mode = 'in_channel'
        except Exception:
            surface_mode = 'thread'
        in_channel_surface = surface_mode == 'in_channel'
        if in_channel_surface and runtime_adapter is not None and (not getattr(runtime_adapter, 'supports_inchannel_continuable', False)):
            logger.debug("Job '%s': cron_continuable_surface=in_channel not supported on %s, using thread", job.get('id', '?'), platform_name)
            in_channel_surface = False
        if in_channel_surface and mirror_this_target and live_adapter_ready:
            thread_id = None
        origin_chat_type = str(origin.get('chat_type') or '').lower()
        is_dm_target = origin_chat_type == 'dm' or (not origin_chat_type and str(chat_id).startswith('D'))
        inchannel_seeded = False
        thread_seeded = False
        opened_thread_id: Optional[str] = None
        if mirror_this_target and (not in_channel_surface) and (runtime_adapter is not None) and (loop is not None) and (not thread_id):
            new_thread_id = _open_continuable_cron_thread(job, runtime_adapter, chat_id, loop)
            if new_thread_id:
                thread_id = new_thread_id
                opened_thread_id = new_thread_id
        if live_adapter_ready:
            from gateway.delivery import DeliveryRouter, DeliveryTarget, _looks_like_int, looks_like_telegram_private_chat_id
            is_ambiguous_telegram_topic = platform == Platform.TELEGRAM and thread_id is not None and looks_like_telegram_private_chat_id(str(chat_id)) and _looks_like_int(str(thread_id))
            route_via_dm_topic = is_ambiguous_telegram_topic and _is_channel_dm_topic(runtime_adapter, chat_id, loop, job['id'])
            if route_via_dm_topic:
                route_thread_id = None
                route_metadata = {'direct_messages_topic_id': str(thread_id), 'job_id': job['id']}
                media_metadata = {'direct_messages_topic_id': str(thread_id)}
            else:
                route_thread_id = str(thread_id) if thread_id is not None else None
                route_metadata = {'job_id': job['id']}
                if route_thread_id:
                    route_metadata['thread_id'] = route_thread_id
                media_metadata = {'thread_id': thread_id} if thread_id else None
            try:
                text_to_send = cleaned_delivery_content.strip()
                adapter_ok = True
                timed_out = False
                if text_to_send:
                    from agent.async_utils import safe_schedule_threadsafe
                    router = DeliveryRouter(config, adapters)
                    route_target = DeliveryTarget(platform=platform, chat_id=str(chat_id), thread_id=route_thread_id, is_explicit=True)
                    future = safe_schedule_threadsafe(router._deliver_to_platform(route_target, text_to_send, route_metadata), loop)
                    if future is None:
                        adapter_ok = False
                        target_errors.append('live adapter event loop scheduling failed')
                    else:
                        send_result = None
                        timeout_handled = False
                        try:
                            send_result = future.result(timeout=60)
                        except TimeoutError:
                            cancelled = future.cancel()
                            if cancelled:
                                msg = f'live adapter send to {platform_name}:{chat_id} timed out before the coroutine was dispatched'
                                logger.warning("Job '%s': %s, falling back to standalone", job['id'], msg)
                                target_errors.append(msg)
                                adapter_ok = False
                                timeout_handled = True
                            else:
                                timed_out = True
                                timeout_handled = True
                                logger.warning("Job '%s': live adapter send to %s:%s timed out after 60s; already dispatched (in flight), assuming delivered (skipping standalone fallback to avoid duplicate)", job['id'], platform_name, chat_id)
                        except Exception as ex:
                            target_errors.append(f'live adapter send failed: {ex}')
                            raise
                        if timeout_handled:
                            pass
                        else:
                            if isinstance(send_result, dict):
                                send_success = bool(send_result.get('success', False))
                                send_raw_response = send_result.get('raw_response')
                            else:
                                send_success = _confirm_adapter_delivery(send_result)
                                send_raw_response = getattr(send_result, 'raw_response', None)
                            if not send_success:
                                if isinstance(send_result, dict):
                                    err = send_result.get('error', 'unknown')
                                    shape = 'dict'
                                elif send_result is not None:
                                    err = getattr(send_result, 'error', None)
                                    shape = type(send_result).__name__
                                else:
                                    err = 'no response from adapter'
                                    shape = 'None'
                                msg = f'live adapter send to {platform_name}:{chat_id} returned unconfirmed result ({shape}, error={err})'
                                if transport is not None and transport.is_relay:
                                    logger.warning("Job '%s': %s", job['id'], msg)
                                else:
                                    logger.warning("Job '%s': %s, falling back to standalone", job['id'], msg)
                                target_errors.append(msg)
                                adapter_ok = False
                            elif send_raw_response and thread_id and send_raw_response.get('thread_fallback'):
                                requested_thread_id = send_raw_response.get('requested_thread_id') or thread_id
                                msg = f'configured thread_id {requested_thread_id} for {platform_name}:{chat_id} was not found; delivered without thread_id'
                                logger.warning("Job '%s': %s", job['id'], msg)
                                delivery_errors.append(msg)
                if adapter_ok and (not timed_out) and media_files:
                    routed_media_metadata = dict(media_metadata or {})
                    if transport is not None and transport.is_relay:
                        routed_media_metadata['_relay_logical_platform'] = platform.value
                        logical_home = config.get_home_channel(platform)
                        if logical_home is not None and logical_home.chat_id == chat_id:
                            if logical_home.user_id:
                                routed_media_metadata['user_id'] = logical_home.user_id
                            if logical_home.scope_id:
                                routed_media_metadata['scope_id'] = logical_home.scope_id
                    _send_media_via_adapter(runtime_adapter, chat_id, media_files, routed_media_metadata or None, loop, job, platform=platform)
                elif timed_out and media_files:
                    msg = f'{len(media_files)} media attachment(s) not delivered to {platform_name}:{chat_id} (live adapter confirmation timed out)'
                    logger.warning("Job '%s': %s", job['id'], msg)
                    delivery_errors.append(msg)
                if adapter_ok:
                    logger.info("Job '%s': delivered to %s:%s via live adapter", job['id'], platform_name, chat_id)
                    delivered = True
                    if opened_thread_id and (not thread_seeded):
                        _seed_cron_thread_session(job, runtime_adapter, platform_name, chat_id, opened_thread_id, mirror_text, chat_name=origin.get('chat_name'))
                        thread_seeded = True
                    if in_channel_surface and mirror_this_target and (not thread_seeded):
                        inchannel_seeded = _seed_cron_channel_session(job, runtime_adapter, platform_name, chat_id, mirror_text, is_dm=is_dm_target, user_id=origin_user_id, chat_name=origin.get('chat_name'))
                    _maybe_mirror_cron_delivery(job, platform_name, chat_id, mirror_text, thread_id=thread_id, user_id=origin_user_id, enabled=mirror_this_target and (not thread_seeded) and (not inchannel_seeded))
            except Exception as e:
                err_msg = f'live adapter delivery to {platform_name}:{chat_id} failed: {e}'
                if not any((err_msg in err for err in target_errors)):
                    target_errors.append(err_msg)
                if transport is not None and transport.is_relay:
                    logger.warning("Job '%s': %s", job['id'], err_msg)
                else:
                    logger.warning("Job '%s': %s, falling back to standalone", job['id'], err_msg)
        if not delivered:
            if transport is not None and transport.is_relay:
                if not target_errors:
                    target_errors.append(f'relay delivery to {platform_name}:{chat_id} failed')
                delivery_errors.extend(target_errors)
                continue
            if _interpreter_shutting_down():
                msg = f'delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down'
                logger.warning("Job '%s': %s", job['id'], msg)
                target_errors.append(msg)
                delivery_errors.extend(target_errors)
                continue
            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files)
            try:
                result = asyncio.run(coro)
            except RuntimeError as run_err:
                coro.close()
                if _interpreter_shutting_down(run_err):
                    msg = f'delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down'
                    logger.warning("Job '%s': %s", job['id'], msg)
                    target_errors.append(msg)
                    delivery_errors.extend(target_errors)
                    continue
                try:
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files))
                        result = future.result(timeout=30)
                    finally:
                        pool.shutdown(wait=False)
                except Exception as e:
                    if _interpreter_shutting_down(e):
                        msg = f'delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down'
                        logger.warning("Job '%s': %s", job['id'], msg)
                        target_errors.append(msg)
                        delivery_errors.extend(target_errors)
                        continue
                    msg = f'delivery to {platform_name}:{chat_id} failed: {e}'
                    logger.error("Job '%s': %s", job['id'], msg, exc_info=True)
                    target_errors.extend([msg])
                    delivery_errors.extend(target_errors)
                    continue
            except Exception as e:
                msg = f'delivery to {platform_name}:{chat_id} failed: {e}'
                logger.error("Job '%s': %s", job['id'], msg, exc_info=True)
                target_errors.extend([msg])
                delivery_errors.extend(target_errors)
                continue
            if result and result.get('error'):
                msg = f"delivery error: {result['error']}"
                logger.error("Job '%s': %s", job['id'], msg)
                target_errors.extend([msg])
                delivery_errors.extend(target_errors)
                continue
            logger.info("Job '%s': delivered to %s:%s", job['id'], platform_name, chat_id)
            _maybe_mirror_cron_delivery(job, platform_name, chat_id, mirror_text, thread_id=thread_id, user_id=origin_user_id, enabled=mirror_this_target and (not thread_seeded))
    if delivery_errors:
        return '; '.join(delivery_errors)
    return None
_DEFAULT_SCRIPT_TIMEOUT = 3600
_SCRIPT_TIMEOUT = _DEFAULT_SCRIPT_TIMEOUT
_RUN_CLAIM_HEARTBEAT_SECONDS = 60.0

def _get_script_timeout() -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if _SCRIPT_TIMEOUT != _DEFAULT_SCRIPT_TIMEOUT:
        try:
            timeout = int(float(_SCRIPT_TIMEOUT))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning('Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default', _SCRIPT_TIMEOUT)
    env_value = os.getenv('HERMES_CRON_SCRIPT_TIMEOUT', '').strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning('Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default', env_value)
    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get('cron', {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get('script_timeout_seconds')
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception as exc:
        logger.debug('Failed to load cron script timeout from config: %s', exc)
    return _DEFAULT_SCRIPT_TIMEOUT

def _read_windows_pyvenv_cfg(venv_dir: Path) -> dict[str, str]:
    cfg_path = venv_dir / 'pyvenv.cfg'
    try:
        lines = cfg_path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return {}
    parsed: dict[str, str] = {}
    for raw in lines:
        if '=' not in raw:
            continue
        key, value = raw.split('=', 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed

def _windows_cron_python_invocation(python_exe: str) -> tuple[str, dict[str, str]]:
    """Return an output-capable hidden Python invocation for Windows scripts.

    Cron scripts capture stdout/stderr, so using ``pythonw.exe`` directly can
    lose script output.  uv-created venv ``python.exe`` launchers are also a
    problem: even with CREATE_NO_WINDOW, the launcher can re-exec the base
    console interpreter and flash a visible window.  For uv venvs, bypass the
    launcher and run the base ``python.exe`` directly with the venv paths
    overlaid in the environment.
    """
    if sys.platform != 'win32':
        return (python_exe, {})
    interpreter = Path(python_exe)
    venv_dir = interpreter.parent.parent
    env_overlay: dict[str, str] = {}
    if interpreter.name.lower() == 'pythonw.exe':
        sibling = interpreter.with_name('python.exe')
        if sibling.exists():
            interpreter = sibling
    cfg = _read_windows_pyvenv_cfg(venv_dir)
    home = cfg.get('home', '')
    site_packages = venv_dir / 'Lib' / 'site-packages'
    if 'uv' in cfg and home:
        base_python = Path(home) / 'python.exe'
        if base_python.exists() and site_packages.exists():
            interpreter = base_python
            env_overlay['VIRTUAL_ENV'] = str(venv_dir)
            pythonpath_entries = [str(Path(__file__).resolve().parents[1]), str(site_packages)]
            existing_pythonpath = os.environ.get('PYTHONPATH', '')
            if existing_pythonpath:
                pythonpath_entries.append(existing_pythonpath)
            env_overlay['PYTHONPATH'] = os.pathsep.join(pythonpath_entries)
    return (str(interpreter), env_overlay)

def _run_job_script(script_path: str, workdir: Optional[str]=None) -> tuple[bool, str]:
    """Execute a cron job's data-collection script and capture its output.

    Scripts must reside within DUCK_AGENT_HOME/scripts/.  Both relative and
    absolute paths are resolved and validated against this directory to
    prevent arbitrary script execution via path traversal or absolute
    path injection.

    Supported interpreters (chosen by file extension):

    * ``.sh`` / ``.bash`` — run with ``/bin/bash``
    * anything else — run with the current Python interpreter
      (``sys.executable``), preserving the original behaviour for
      Python-based pre-check and data-collection scripts.

    Shell support lets ``no_agent=True`` jobs ship classic bash watchdogs
    (the `memory-watchdog.sh` pattern) without wrapping them in Python.

    Subprocess environment is passed through ``_sanitize_subprocess_env`` so
    provider credentials and other Duck Agent-managed secrets are not inherited
    (SECURITY.md §2.3), matching terminal and MCP child processes.

    Args:
        script_path: Path to the script.  Relative paths are resolved
            against DUCK_AGENT_HOME/scripts/.  Absolute and ~-prefixed paths
            are also validated to ensure they stay within the scripts dir.
        workdir: Optional absolute path to use as the script's cwd.
            When set, the subprocess runs in this directory instead of
            the scripts-dir parent.  The Python process cwd is NEVER
            mutated, avoiding the global-side-effect bug where a cron
            job's ``os.chdir()`` leaks into concurrent gateway sessions
            (#69396).

    Returns:
        (success, output) — on failure *output* contains the error message so the
        LLM can report the problem to the user.
    """
    scripts_dir = _get_hermes_home() / 'scripts'
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir_resolved = scripts_dir.resolve()
    try:
        raw = Path(script_path).expanduser()
    except (ValueError, RuntimeError, OSError):
        return (False, f'Blocked: script path is not a valid filesystem path: {script_path!r}')
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()
    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return (False, f'Blocked: script path resolves outside the scripts directory ({scripts_dir_resolved}): {script_path!r}')
    if not path.exists():
        return (False, f'Script not found: {path}')
    if not path.is_file():
        return (False, f'Script path is not a file: {path}')
    script_timeout = _get_script_timeout()
    suffix = path.suffix.lower()
    if suffix in {'.sh', '.bash'}:
        _bash = shutil.which('bash') or ('/bin/bash' if os.path.isfile('/bin/bash') else None)
        if _bash is None:
            return (False, f'Cannot run .sh/.bash script {path.name!r}: bash not found on PATH. On Windows, install Git for Windows (which ships Git Bash) or rewrite the script as Python (.py).')
        argv = [_bash, str(path)]
        env_overlay: dict[str, str] = {}
    else:
        python_exe, env_overlay = _windows_cron_python_invocation(sys.executable)
        argv = [python_exe, str(path)]
    try:
        from tools.environments.local import build_subprocess_env
        popen_kwargs = {}
        if sys.platform == 'win32':
            popen_kwargs = {'creationflags': windows_hide_flags(), 'encoding': 'utf-8', 'errors': 'replace'}
        env = build_subprocess_env()
        env.update(env_overlay)
        _script_cwd = workdir or str(path.parent)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=script_timeout, cwd=_script_cwd, env=env, **popen_kwargs)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        try:
            from agent.redact import redact_sensitive_text
            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
        except Exception as e:
            logger.warning('Failed to redact sensitive text from output: %s', e)
            stdout = '[REDACTED - redaction failed]'
            stderr = '[REDACTED - redaction failed]'
        if result.returncode != 0:
            parts = [f'Script exited with code {result.returncode}']
            if stderr:
                parts.append(f'stderr:\n{stderr}')
            if stdout:
                parts.append(f'stdout:\n{stdout}')
            return (False, '\n'.join(parts))
        return (True, stdout)
    except subprocess.TimeoutExpired:
        return (False, f'Script timed out after {script_timeout}s: {path}')
    except Exception as exc:
        return (False, f'Script execution failed: {exc}')

def _run_job_script_with_claim_heartbeat(job: dict, script_path: str, workdir: Optional[str]=None) -> tuple[bool, str]:
    """Run a cron script while keeping its owned one-shot claim fresh.

    Script execution is synchronous and may legitimately outlive the stale
    claim TTL.  Without a concurrent heartbeat, another scheduler process can
    mistake the live run for a dead owner and dispatch the same one-shot again.
    Recurring jobs and unclaimed/manual runs have no durable one-shot claim and
    therefore use the ordinary script path without starting a thread.

    The claim owner is captured from the dispatched job and never re-read from
    storage.  ``heartbeat_run_claim`` compares that stable owner before every
    refresh, so a stale runner cannot extend a replacement owner's claim.
    """
    schedule = job.get('schedule')
    claim = job.get('run_claim')
    owner = str(claim.get('by') or '') if isinstance(claim, dict) else ''
    if not (isinstance(schedule, dict) and schedule.get('kind') == 'once' and owner):
        return _run_job_script(script_path, workdir=workdir)
    job_id = str(job.get('id') or '')
    stop = threading.Event()
    heartbeat_context = contextvars.copy_context()

    def _heartbeat_loop() -> None:
        while not stop.wait(_RUN_CLAIM_HEARTBEAT_SECONDS):
            try:
                heartbeat_run_claim(job_id, expected_owner=owner)
            except Exception:
                logger.debug("Job '%s': script run_claim heartbeat failed", job_id, exc_info=True)
    heartbeat_thread = threading.Thread(target=heartbeat_context.run, args=(_heartbeat_loop,), name='cron-script-claim-heartbeat', daemon=True)
    try:
        heartbeat_thread.start()
    except Exception:
        logger.debug("Job '%s': could not start script run_claim heartbeat", job_id, exc_info=True)
        return _run_job_script(script_path, workdir=workdir)
    try:
        return _run_job_script(script_path, workdir=workdir)
    finally:
        stop.set()
        heartbeat_thread.join(timeout=1.0)

def _parse_wake_gate(script_output: str) -> bool:
    """Parse the last non-empty stdout line of a cron job's pre-check script
    as a wake gate.

    The convention (ported from nanoclaw #1232): if the last stdout line is
    JSON like ``{"wakeAgent": false}``, the agent is skipped entirely — no
    LLM run, no delivery. Any other output (non-JSON, missing flag, gate
    absent, or ``wakeAgent: true``) means wake the agent normally.

    Returns True if the agent should wake, False to skip.
    """
    if not script_output:
        return True
    stripped_lines = [line for line in script_output.splitlines() if line.strip()]
    if not stripped_lines:
        return True
    last_line = stripped_lines[-1].strip()
    try:
        gate = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(gate, dict):
        return True
    return gate.get('wakeAgent', True) is not False

def _build_job_prompt(job: dict, prerun_script: Optional[tuple]=None, extra_prompt: Optional[str]=None) -> str:
    """Build the effective prompt for a cron job, optionally loading one or more skills first.

    Args:
        job: The cron job dict.
        prerun_script: Optional ``(success, stdout)`` from a script that has
            already been executed by the caller (e.g. for a wake-gate check).
            When provided, the script is not re-executed and the cached
            result is used for prompt injection. When omitted, the script
            (if any) runs inline as before.
        extra_prompt: Optional per-run context (from ``cronjob(action='run')``,
            #57331 — salvaged from #57342 by @liuhao1024). Appended to the
            stored prompt under a ``## Run Context`` header for this single
            fire only — never persisted to the job definition.
    """
    user_prompt = str(job.get('prompt') or '')
    if extra_prompt:
        user_prompt = f'{user_prompt}\n\n## Run Context\n{extra_prompt}'
    prompt = user_prompt
    skills = job.get('skills')
    has_injected_data = False
    script_path = job.get('script')
    if script_path:
        if prerun_script is not None:
            success, script_output = prerun_script
        else:
            success, script_output = _run_job_script(script_path)
        if success:
            if script_output:
                prompt = f'## Script Output\nThe following data was collected by a pre-run script. Use it as context for your analysis.\n\n```\n{script_output}\n```\n\n{prompt}'
                has_injected_data = True
            else:
                return None
        else:
            prompt = f'## Script Error\nThe data-collection script failed. Report this to the user.\n\n```\n{script_output}\n```\n\n{prompt}'
            has_injected_data = True
    context_from = job.get('context_from')
    if context_from:
        from cron.jobs import get_cron_output_dir
        output_dir = get_cron_output_dir()
        if isinstance(context_from, str):
            context_from = [context_from]
        for source_job_id in context_from:
            if not source_job_id or not all((c in '0123456789abcdef' for c in source_job_id)):
                logger.warning('context_from: skipping invalid job_id %r for job_id=%r name=%r%s', source_job_id, job.get('id'), job.get('name'), _cron_job_origin_log_suffix(job))
                continue
            try:
                job_output_dir = output_dir / source_job_id
                if not job_output_dir.exists():
                    continue
                output_files = sorted(job_output_dir.glob('*.md'), key=lambda f: f.stat().st_mtime, reverse=True)
                if not output_files:
                    continue
                latest_output = output_files[0].read_text(encoding='utf-8').strip()
                _MAX_CONTEXT_CHARS = 8000
                if len(latest_output) > _MAX_CONTEXT_CHARS:
                    latest_output = latest_output[:_MAX_CONTEXT_CHARS] + '\n\n[... output truncated ...]'
                if latest_output:
                    prompt = f"## Output from job '{source_job_id}'\nThe following is the most recent output from a preceding cron job. Use it as context for your analysis.\n\n```\n{latest_output}\n```\n\n{prompt}"
                    has_injected_data = True
                else:
                    continue
            except (OSError, PermissionError) as e:
                logger.warning('context_from: failed to read output for job %r: %s', source_job_id, e)
    from cron import notepad as cron_notepad
    notepad_section = cron_notepad.render_notepad_section(str(job.get('id') or ''))
    if notepad_section:
        prompt = f'{notepad_section}{prompt}'
        has_injected_data = True
    cron_hint = '[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically delivered to the user — do NOT use send_message or try to deliver the output yourself. Just produce your report/output as your final response and the system handles the rest. SILENT: If there is genuinely nothing new to report, respond with exactly "[SILENT]" (nothing else) to suppress delivery. Never combine [SILENT] with content — either report your findings normally, or say [SILENT] and nothing more.]\n\n'
    prompt = cron_hint + prompt
    if skills is None:
        legacy = job.get('skill')
        skills = [legacy] if legacy else []
    elif isinstance(skills, str):
        skills = [skills]
    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return _scan_assembled_cron_prompt(prompt, job, has_skills=False, has_injected_data=has_injected_data, user_prompt=user_prompt)
    from tools.skills_tool import skill_view
    from tools.skill_usage import bump_use
    from agent.skill_bundles import build_bundle_invocation_message, resolve_bundle_command_key
    from agent.skill_utils import normalize_skill_lookup_name
    parts = []
    skipped: list[str] = []
    for skill_name in skill_names:
        bundle_key = resolve_bundle_command_key(skill_name.lstrip('/'))
        if bundle_key:
            bundle_payload = build_bundle_invocation_message(bundle_key, user_instruction='', task_id=str(job.get('id') or '') or None)
            if bundle_payload:
                bundle_message, _loaded_bundle_skills, _missing_bundle_skills = bundle_payload
                if parts:
                    parts.append('')
                parts.append(bundle_message)
                continue
            logger.warning("Cron job '%s': bundle '%s' could not load any skills, skipping", job.get('name', job.get('id')), skill_name)
            skipped.append(skill_name)
            continue
        try:
            loaded = json.loads(skill_view(normalize_skill_lookup_name(skill_name)))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Cron job '%s': skill '%s' returned invalid JSON, skipping", job.get('name', job.get('id')), skill_name)
            skipped.append(skill_name)
            continue
        if not loaded.get('success'):
            error = loaded.get('error') or f"Failed to load skill '{skill_name}'"
            logger.warning("Cron job '%s': skill not found, skipping — %s", job.get('name', job.get('id')), error)
            skipped.append(skill_name)
            continue
        try:
            bump_use(skill_name, task_id=str(job.get('id') or '') or None)
        except Exception:
            logger.debug("Cron job: failed to bump skill usage for '%s'", skill_name, exc_info=True)
        content = str(loaded.get('content') or '').strip()
        if parts:
            parts.append('')
        parts.extend([f'[IMPORTANT: The user has invoked the "{skill_name}" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]', '', content])
    if skipped:
        notice = f"[IMPORTANT: The following skill(s) were listed for this job but could not be found and were skipped: {', '.join(skipped)}. Start your response with a brief notice so the user is aware, e.g.: '⚠️ Skill(s) not found and skipped: {', '.join(skipped)}']"
        parts.insert(0, notice)
    if prompt:
        parts.extend(['', f'The user has provided the following instruction alongside the skill invocation: {prompt}'])
    return _scan_assembled_cron_prompt('\n'.join(parts), job, has_skills=True)

def _scan_assembled_cron_prompt(assembled: str, job: dict, *, has_skills: bool=False, has_injected_data: bool=False, user_prompt: Optional[str]=None) -> str:
    """Scan the fully-assembled cron prompt for injection patterns. Raises
    ``CronPromptInjectionBlocked`` when a match fires so ``run_job`` can
    surface a clear refusal to the operator.

    Plugs the #3968 gap: ``_scan_cron_prompt`` runs on the user-supplied
    prompt at create/update, but skill content is loaded from disk at
    runtime and was never scanned. Since cron runs non-interactively
    (auto-approves tool calls), a malicious skill carrying an injection
    payload bypassed every gate.

    Two pattern tiers, selected by what the assembled prompt CONTAINS,
    not just whether skills are attached:

    - When the assembled prompt is essentially the user prompt + the cron
      hint (no skills, no injected data), the STRICT ``_scan_cron_prompt``
      patterns apply: a bare ``rm -rf /`` in a small directive prompt is a
      smoking gun, not prose.
    - When the assembled prompt includes runtime-loaded content — skill
      markdown (``has_skills=True``) or DATA injected from a job script's
      stdout / an upstream job's output (``has_injected_data=True``) — the
      LOOSER ``_scan_cron_skill_assembled`` pattern set is used: only
      unambiguous prompt-injection directives block; command-shape
      patterns are dropped and invisible unicode is sanitized (stripped +
      logged) rather than blocked, to avoid false-positives that
      permanently kill a job. Skill bodies are vetted at install time by
      ``skills_guard.py``; script output is produced by operator-authored
      code, the same trust class — and data feeds (e.g. a triage bot
      ingesting bug reports) legitimately quote dangerous commands.

    When the looser tier is selected because of injected data only,
    ``user_prompt`` (the raw, pre-assembly prompt) is additionally scanned
    with the STRICT set so the user-authored surface keeps the full
    create/update-time guarantee at runtime (defense-in-depth for legacy
    jobs that predate the create-time scanner).
    """
    from tools.cronjob_tools import _scan_cron_prompt, _scan_cron_skill_assembled
    if has_skills or has_injected_data:
        cleaned, scan_error = _scan_cron_skill_assembled(assembled)
        assembled = cleaned
        if not scan_error and (not has_skills) and user_prompt:
            scan_error = _scan_cron_prompt(user_prompt)
    else:
        scan_error = _scan_cron_prompt(assembled)
    if scan_error:
        job_label = job.get('name') or job.get('id') or '<unknown>'
        logger.warning("Cron job '%s': assembled prompt blocked by injection scanner — %s", job_label, scan_error)
        raise CronPromptInjectionBlocked(scan_error)
    return assembled

def _guard_job_credential_exfil(job: dict) -> None:
    """Fail closed if a job's stored provider/base_url pair would exfiltrate a
    credential (F8 runtime backstop; CWE-200/CWE-522).

    The model-callable cron tool validates this on create/update, but a job
    persisted before that guard — or written directly to the jobs store —
    reaches the scheduler's provider-resolution sink unchecked. Re-validate the
    EFFECTIVE stored pair with the same guard the tool uses, so a named
    provider's stored key is never paired with an off-host base_url at fire
    time. Raises ``RuntimeError`` (caught by the run_job failure path → the run
    is aborted and reported) when the pair is unsafe; returns ``None`` otherwise.

    Fallback providers come from operator config, not the model-callable job, so
    they are trusted and validated by the caller, not here.
    """
    try:
        from tools.cronjob_tools import _validate_cron_base_url
        err = _validate_cron_base_url(job.get('provider'), job.get('base_url'))
    except Exception as exc:
        if job.get('base_url'):
            err = f'could not validate provider/base_url pair ({exc.__class__.__name__}: {exc}); refusing to run a job with an unverified base_url override'
        else:
            err = None
    if err:
        job_id = job.get('id')
        logger.error("Job '%s': refusing to run — unsafe provider/base_url pair could exfiltrate a stored credential: %s", job_id, err)
        raise RuntimeError(f"Cron job '{job_id}' blocked for safety: {err}")
BLOCKED_CONFIG_MARKER = '[blocked_config]'
BLOCKED_CONFIG_SILENT_MARKER = '[blocked_config:silent]'

def _cron_preflight_enabled(cfg: dict) -> bool:
    """Whether cron pre-dispatch configuration validation is enabled.

    Default ON; only the literal boolean ``false`` under ``cron.preflight``
    opts out (mirrors ``cron_model_drift_guard_enabled`` semantics).
    """
    cron_cfg = (cfg or {}).get('cron')
    if not isinstance(cron_cfg, dict):
        return True
    return cron_cfg.get('preflight', True) is not False

def _preflight_check_provider_key(job: dict, cfg: dict) -> Optional[str]:
    """READ-ONLY probe: would provider resolution fail for lack of a key?

    Mirrors the effective requested-provider computation from run_job's
    resolution block without any side effects on the run. When a fallback
    chain is configured the check is skipped entirely — the existing
    auth-fallback path may legitimately rescue a missing primary key, so
    blocking here would break that contract (and burning zero LLM calls is
    already guaranteed by the fallback resolution being config-local).
    """
    try:
        if get_fallback_chain(cfg):
            return None
    except Exception:
        return None
    _cron_cfg = cfg.get('cron') if isinstance(cfg.get('cron'), dict) else {}
    requested = job.get('provider') or str((_cron_cfg or {}).get('model_provider') or '').strip() or None
    model = job.get('model') or os.getenv('HERMES_MODEL') or ''
    from hermes_cli.auth import AuthError
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        kwargs = {'requested': requested, 'target_model': model}
        if job.get('base_url'):
            kwargs['explicit_base_url'] = job.get('base_url')
        resolve_runtime_provider(**kwargs)
    except AuthError as exc:
        return f"provider credential missing: {exc}. Set the provider API key in .env (or `duck-agent setup`), or pin a working provider via `cronjob action=update job_id={job.get('id')} provider=<p>`."
    except Exception:
        return None
    return None

def _preflight_check_delivery(job: dict) -> Optional[str]:
    """Check the job's delivery target(s) resolve to configured platforms.

    ``local``/``origin`` (and the ``all`` routing token) need no gateway
    credentials and are never checked — a deliver=local job must not pay a
    gateway-config load. For concrete platform targets, an unknown platform
    always blocks; a known platform additionally blocks when the gateway
    config is loadable and reports it unconnected (enabled + credentials —
    the same source `cron_delivery_targets` uses). Gateway-config load
    failures fail OPEN so a transient config hiccup never wedges delivery
    that would have worked.
    """
    deliver_value = _normalize_deliver_value(job.get('deliver', 'local'))
    platform_parts: list[str] = []
    for part in deliver_value.split(','):
        part = part.strip()
        if not part or part.lower() in {'local', 'origin', 'all'}:
            continue
        platform_parts.append(part.split(':', 1)[0].strip())
    if not platform_parts:
        return None
    connected: Optional[set] = None
    for platform_name in platform_parts:
        if not _is_known_delivery_platform(platform_name):
            return f"delivery platform '{platform_name}' is not a known cron delivery target. Fix the job's `deliver` value or configure the platform's gateway credentials."
        if connected is None:
            try:
                from gateway.config import load_gateway_config
                gateway_config = load_gateway_config()
                connected = {p.value for p in gateway_config.get_connected_platforms()}
            except Exception:
                logger.debug('preflight: gateway config unavailable — skipping delivery credential check', exc_info=True)
                return None
        if platform_name.lower() not in connected:
            return f"delivery platform '{platform_name}' has no gateway credentials configured (not connected). Configure it via `duck-agent setup` or change the job's `deliver` target."
    return None

def _preflight_check_skills(job: dict) -> Optional[str]:
    """Check attached skills report ready (no missing required env/commands).

    Consults the same ``readiness_status`` payload ``skill_view`` computes
    for interactive use. Skills that fail to load at all are left to the
    existing skipped-skill handling in ``_build_job_prompt`` (fail-open):
    this check only blocks on an affirmative "setup needed" verdict, i.e.
    the skill exists but its required environment is missing — a run that
    is guaranteed to misfire.
    """
    skills = job.get('skills')
    if skills is None:
        legacy = job.get('skill')
        skills = [legacy] if legacy else []
    elif isinstance(skills, str):
        skills = [skills]
    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return None
    from tools.skills_tool import skill_view
    for skill_name in skill_names:
        try:
            payload = json.loads(skill_view(skill_name))
        except Exception:
            continue
        if not isinstance(payload, dict) or not payload.get('success'):
            continue
        if payload.get('setup_needed') or payload.get('readiness_status') == 'setup_needed':
            missing = [f'env ${name}' for name in payload.get('missing_required_environment_variables') or []]
            missing += [f"command '{name}'" for name in payload.get('missing_required_commands') or []]
            missing += [f'credential file {name}' for name in payload.get('missing_credential_files') or []]
            detail = ', '.join(missing) or 'required setup incomplete'
            return f"attached skill '{skill_name}' is not ready: missing {detail}. Provide the missing prerequisites or detach the skill from this job."
    return None

def _preflight_job_config(job: dict, cfg: dict) -> Optional[str]:
    """Pre-dispatch configuration validation (T1-26).

    Returns a human-readable reason when the job's configuration cannot
    produce a successful run — missing provider API key, unconfigured
    delivery platform, or an attached skill with missing required env —
    so the caller can refuse the run BEFORE any agent machinery is
    constructed and no LLM call is burned. Returns ``None`` when the
    configuration validates (or when a check cannot be evaluated: every
    check fails open, so preflight can only ever block on an affirmative
    misconfiguration verdict).

    Same fail-before-spend spirit as the #44585 drift guard and the
    fail-loud-on-hidden-tools direction in #27948; alert dedup follows the
    alert-once pattern from the dead-pin auto-pause (#73506).
    """
    for name, check in (('provider_key', lambda: _preflight_check_provider_key(job, cfg)), ('skills', lambda: _preflight_check_skills(job)), ('delivery', lambda: _preflight_check_delivery(job))):
        try:
            reason = check()
        except Exception:
            logger.debug('preflight check %s raised — failing open', name, exc_info=True)
            continue
        if reason:
            return reason
    return None

def run_job(job: dict, *, defer_agent_teardown: Optional[list]=None, extra_prompt: Optional[str]=None) -> tuple[bool, str, str, Optional[str]]:
    """
    Execute a single cron job.

    ``defer_agent_teardown``: when a caller passes a list, ``run_job`` skips
    the agent's async-resource teardown (``agent.close()`` +
    ``cleanup_stale_async_clients()``) in its ``finally`` block and instead
    appends the live agent to that list. The caller is then responsible for
    calling ``_teardown_cron_agent(agent)`` AFTER it has delivered the result.
    This closes the ordering window in #58720 where delivery ran against a
    torn-down async client (defense-in-depth alongside the interpreter-shutdown
    guard). When ``None`` (the default) teardown happens inline as before, so
    every existing caller is unchanged.

    ``extra_prompt``: optional per-run context from ``cronjob(action='run',
    prompt=...)`` (#57331). Appended to the stored prompt for this fire only —
    never persisted to the job definition.

    Returns:
        Tuple of (success, full_output_doc, final_response, error_message)
    """
    job_id = job['id']
    job_name = str(job.get('name') or job.get('prompt') or job_id or 'cron job')
    if job.get('no_agent'):
        script_path = job.get('script')
        if not script_path:
            err = 'no_agent=True but no script is set for this job'
            logger.error("Job '%s': %s", job_id, err)
            return (False, '', '', err)
        _job_workdir = (job.get('workdir') or '').strip() or None
        if _job_workdir and (not Path(_job_workdir).is_dir()):
            logger.warning("Job '%s': configured workdir %r no longer exists — running without it", job_id, _job_workdir)
            _job_workdir = None
        try:
            ok, output = _run_job_script_with_claim_heartbeat(job, script_path, workdir=_job_workdir)
        except Exception as exc:
            logger.exception("Job '%s': script execution raised unexpectedly", job_id)
            ok, output = (False, f'Script execution failed: {exc}')
        now_iso = _hermes_now().strftime('%Y-%m-%d %H:%M:%S')
        if not ok:
            alert = f"⚠ Cron watchdog '{job_name}' script failed\n\n{output}\n\nTime: {now_iso}"
            doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {now_iso}\n**Mode:** no_agent (script)\n**Status:** script failed\n\n{output}\n'
            return (False, doc, alert, output)
        if not _parse_wake_gate(output):
            logger.info("Job '%s' (no_agent): wakeAgent=false gate — silent run", job_id)
            silent_doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {now_iso}\n**Mode:** no_agent (script)\n**Status:** silent (wakeAgent=false)\n'
            return (True, silent_doc, SILENT_MARKER, None)
        if not output.strip():
            logger.info("Job '%s' (no_agent): empty stdout — silent run", job_id)
            silent_doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {now_iso}\n**Mode:** no_agent (script)\n**Status:** silent (empty output)\n'
            return (True, silent_doc, SILENT_MARKER, None)
        doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {now_iso}\n**Mode:** no_agent (script)\n\n---\n\n{output}\n'
        return (True, doc, output, None)
    from cron.monitor import check_monitor, job_has_monitor
    _monitor_context: Optional[str] = None
    if job_has_monitor(job):
        _mon = check_monitor(job)
        _mon_now = _hermes_now().strftime('%Y-%m-%d %H:%M:%S')
        if not _mon.ok:
            logger.error("Job '%s': monitor source failed: %s", job_id, _mon.error)
            _mon_doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_mon_now}\n**Mode:** monitor\n**Status:** monitor source failed\n\n{_mon.error}\n'
            _mon_alert = f"⚠ Cron monitor '{job_name}' source failed\n\n{_mon.error}\n\nTime: {_mon_now}"
            return (False, _mon_doc, _mon_alert, _mon.error)
        if not _mon.changed:
            logger.info("Job '%s': monitor output unchanged — suppressing agent run", job_id)
            _mon_doc = f'# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_mon_now}\n**Mode:** monitor\n**Status:** no_change (agent run suppressed)\n'
            return (True, _mon_doc, SILENT_MARKER, None)
        _monitor_context = _mon.context_block
        if _monitor_context:
            extra_prompt = f'{_monitor_context}\n\n{extra_prompt}' if extra_prompt else _monitor_context
    from run_agent import AIAgent
    _session_db = None
    try:
        from hermes_state import SessionDB
        _session_db_timeout: float | None = None
        _raw_env_timeout = os.getenv('HERMES_CRON_SESSION_DB_TIMEOUT', '').strip()
        if _raw_env_timeout:
            try:
                _session_db_timeout = float(_raw_env_timeout)
            except (ValueError, TypeError):
                logger.warning('Invalid HERMES_CRON_SESSION_DB_TIMEOUT=%r; using config/default', _raw_env_timeout)
        if _session_db_timeout is None:
            try:
                from hermes_cli.config import load_config
                _cfg = load_config() or {}
                _cron_cfg = _cfg.get('cron', {}) if isinstance(_cfg, dict) else {}
                _configured = _cron_cfg.get('session_db_timeout_seconds')
                if _configured is not None:
                    _session_db_timeout = float(_configured)
            except Exception as exc:
                logger.debug('Failed to load cron.session_db_timeout_seconds from config: %s', exc)
        if _session_db_timeout is None:
            _session_db_timeout = 10.0
        if _session_db_timeout > 0:
            _session_db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _session_db = _session_db_pool.submit(SessionDB).result(timeout=_session_db_timeout)
            finally:
                _session_db_pool.shutdown(wait=False)
        else:
            _session_db = SessionDB()
    except concurrent.futures.TimeoutError:
        logger.error("Job '%s': SessionDB init did not return within %.0fs — proceeding without a session store for this run instead of blocking it forever", job.get('id', '?'), _session_db_timeout)
    except Exception as e:
        logger.debug("Job '%s': SQLite session store not available: %s", job.get('id', '?'), e)
    prerun_script = None
    script_path = job.get('script')
    if script_path:
        prerun_script = _run_job_script_with_claim_heartbeat(job, script_path)
        _ran_ok, _script_output = prerun_script
        if _ran_ok and (not _parse_wake_gate(_script_output)):
            logger.info("Job '%s' (ID: %s): wakeAgent=false, skipping agent run", job_name, job_id)
            silent_doc = f"# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n\nScript gate returned `wakeAgent=false` — agent skipped.\n"
            return (True, silent_doc, SILENT_MARKER, None)
    try:
        prompt = _build_job_prompt(job, prerun_script=prerun_script, extra_prompt=extra_prompt)
    except CronPromptInjectionBlocked as block_exc:
        logger.warning("Job '%s' (ID: %s): blocked by prompt-injection scanner — %s", job_name, job_id, block_exc)
        blocked_doc = f"# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n**Status:** BLOCKED\n\nThe assembled prompt (user prompt + loaded skill content) tripped the cron injection scanner and the agent was NOT run.\n\n**Scanner result:** {block_exc}\n\nAudit the skill(s) attached to this job for prompt-injection payloads or invisible-unicode markers. If the skill is legitimate and the match is a false positive, rephrase the content to avoid the threat pattern (`tools/cronjob_tools.py::_CRON_THREAT_PATTERNS`)."
        return (False, blocked_doc, '', str(block_exc))
    if prompt is None:
        logger.info("Job '%s': script produced no output, skipping AI call.", job_name)
        return (True, '', SILENT_MARKER, None)
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"
    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info('Prompt: %s', prompt[:100])
    agent = None
    from gateway.session_context import set_session_vars, clear_session_vars, _VAR_MAP
    _job_workdir = (job.get('workdir') or '').strip() or None
    if _job_workdir and (not Path(_job_workdir).is_dir()):
        logger.warning("Job '%s': configured workdir %r no longer exists — running without it", job_id, _job_workdir)
        _job_workdir = None
    _ctx_tokens = set_session_vars(platform='', chat_id='', chat_name='', async_delivery=False, cwd=_job_workdir or '')
    _cron_delivery_vars = ('HERMES_CRON_AUTO_DELIVER_PLATFORM', 'HERMES_CRON_AUTO_DELIVER_CHAT_ID', 'HERMES_CRON_AUTO_DELIVER_THREAD_ID')
    for _var_name in _cron_delivery_vars:
        _VAR_MAP[_var_name].set('')
    _prior_terminal_cwd = os.environ.get('TERMINAL_CWD', '_UNSET_')
    _holds_cwd_write = _job_workdir is not None
    _cwd_lock_timeout = _cwd_lock_timeout_seconds()
    _cwd_lock_acquired = True
    if _holds_cwd_write:
        if not _terminal_cwd_lock.acquire_write(timeout=_cwd_lock_timeout):
            _cwd_lock_acquired = False
    elif not _terminal_cwd_lock.acquire_read(timeout=_cwd_lock_timeout):
        _cwd_lock_acquired = False
    _cron_session_var = _VAR_MAP['HERMES_CRON_SESSION']
    _cron_session_token = None
    _non_dispatcher_token = None
    try:
        if not _cwd_lock_acquired:
            raise TimeoutError(f"Timed out waiting for the TERMINAL_CWD {('write' if _holds_cwd_write else 'read')} lock after {_cwd_lock_timeout:.0f}s — another cron job (a workdir writer, or long-running readers) has held it for longer than the cron inactivity limit. If a workdir job is the holder, stagger its schedule or remove its workdir to unblock this job (#79768).")
        _cron_session_token = _cron_session_var.set('1')
        _non_dispatcher_token = enter_non_dispatcher_owned_context()
        if _job_workdir:
            os.environ['TERMINAL_CWD'] = _job_workdir
            logger.info("Job '%s': using workdir %s", job_id, _job_workdir)
        from hermes_cli.env_loader import load_hermes_dotenv, reset_secret_source_cache
        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=_get_hermes_home())
        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            _VAR_MAP['HERMES_CRON_AUTO_DELIVER_PLATFORM'].set(delivery_target['platform'])
            _VAR_MAP['HERMES_CRON_AUTO_DELIVER_CHAT_ID'].set(str(delivery_target['chat_id']))
            _VAR_MAP['HERMES_CRON_AUTO_DELIVER_THREAD_ID'].set('' if delivery_target.get('thread_id') is None else str(delivery_target['thread_id']))
        model = job.get('model') or os.getenv('HERMES_MODEL') or ''
        _cron_default_model = ''
        _cron_default_provider = ''
        _cfg = {}
        _model_cfg = {}
        try:
            from hermes_cli.config import read_user_config_raw
            _cfg_path = str(_get_hermes_home() / 'config.yaml')
            if os.path.exists(_cfg_path):
                _cfg = read_user_config_raw(Path(_cfg_path))
                try:
                    from hermes_cli import managed_scope
                    _cfg = managed_scope.apply_managed_overlay(_cfg)
                except Exception:
                    pass
                _cfg = _expand_env_vars(_cfg)
                _model_cfg = _cfg.get('model') or {}
                _cron_cfg_for_model = _cfg.get('cron') or {}
                if isinstance(_cron_cfg_for_model, dict):
                    _cron_default_model = str(_cron_cfg_for_model.get('model') or '').strip()
                    _cron_default_provider = str(_cron_cfg_for_model.get('model_provider') or '').strip()
                if not job.get('model'):
                    if _cron_default_model:
                        model = _cron_default_model
                    elif isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _default = _model_cfg.get('default') or _model_cfg.get('model')
                        if _default:
                            model = _default
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)
        if not (isinstance(model, str) and model.strip()):
            raise RuntimeError(f"Cron job '{job_name}' has no model configured (job.model={job.get('model')!r}, HERMES_MODEL={os.getenv('HERMES_MODEL', '')!r}, config.yaml model.default missing or empty). Set a per-job model via `cronjob action=update job_id={job_id} model=<name>` or set a default with `duck-agent model <name>`.")
        try:
            from hermes_constants import apply_ipv4_preference
            _net_cfg = _cfg.get('network', {})
            if isinstance(_net_cfg, dict) and _net_cfg.get('force_ipv4'):
                apply_ipv4_preference(force=True)
        except Exception:
            pass
        from hermes_constants import resolve_reasoning_config
        prefill_messages = None
        agent_cfg = _cfg.get('agent', {}) if isinstance(_cfg.get('agent', {}), dict) else {}
        prefill_file = os.getenv('HERMES_PREFILL_MESSAGES_FILE', '') or _cfg.get('prefill_messages_file', '') or agent_cfg.get('prefill_messages_file', '')
        if prefill_file:
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = _get_hermes_home() / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, 'r', encoding='utf-8') as _pf:
                        prefill_messages = json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None
        max_iterations = _cfg.get('agent', {}).get('max_turns') or _cfg.get('max_turns') or 500
        pr = _cfg.get('provider_routing') or {}
        from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error
        from hermes_cli.auth import AuthError
        _guard_job_credential_exfil(job)
        _pf_reason = None
        try:
            if _cron_preflight_enabled(_cfg):
                _pf_reason = _preflight_job_config(job, _cfg)
                if not _pf_reason and job.get('preflight_alerted'):
                    try:
                        from cron.jobs import clear_preflight_alerted
                        clear_preflight_alerted(job_id)
                    except Exception:
                        pass
        except Exception:
            logger.debug("Job '%s': preflight validation errored — failing open", job_id, exc_info=True)
            _pf_reason = None
        if _pf_reason:
            logger.warning("Job '%s' (ID: %s): BLOCKED by pre-dispatch config validation — %s (no LLM call was made)", job_name, job_id, _pf_reason)
            already_alerted = False
            try:
                from cron.jobs import mark_preflight_alerted
                already_alerted = mark_preflight_alerted(job_id)
            except Exception:
                logger.debug("Job '%s': could not persist preflight alert marker", job_id, exc_info=True)
            marker = BLOCKED_CONFIG_SILENT_MARKER if already_alerted else BLOCKED_CONFIG_MARKER
            blocked_doc = f"# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n**Status:** BLOCKED (configuration)\n\nPre-dispatch validation found a configuration problem and the agent was NOT run (no tokens spent).\n\n**Reason:** {_pf_reason}\n\nThe job will stay blocked (without re-alerting) until the configuration is fixed; the next healthy run clears this state. Set `cron.preflight: false` in config.yaml to disable this validation."
            return (False, blocked_doc, '', f'{marker} {_pf_reason}')
        primary_model_for_drift = model
        configured_provider_for_drift = str(_model_cfg.get('provider') or '').strip().lower() if isinstance(_model_cfg, dict) else ''
        primary_provider_for_drift = str(job.get('provider') or '').strip().lower() or configured_provider_for_drift or None
        try:
            runtime_kwargs = {'requested': job.get('provider') or _cron_default_provider or None, 'target_model': model}
            if job.get('base_url'):
                runtime_kwargs['explicit_base_url'] = job.get('base_url')
            runtime = resolve_runtime_provider(**runtime_kwargs)
            primary_provider_for_drift = str(runtime.get('provider') or '').strip().lower() or primary_provider_for_drift
        except AuthError as auth_exc:
            primary_provider_for_drift = str(getattr(auth_exc, 'provider', '') or '').strip().lower() or primary_provider_for_drift
            logger.warning("Job '%s': primary auth failed (%s), trying fallback", job_id, auth_exc)
            fb_list = get_fallback_chain(_cfg)
            runtime = None
            for entry in fb_list:
                if not isinstance(entry, dict):
                    continue
                fb_provider = str(entry.get('provider') or '').strip()
                fb_model = str(entry.get('model') or '').strip()
                if not fb_provider or not fb_model:
                    continue
                try:
                    from hermes_cli.fallback_config import resolve_entry_api_key
                    fb_kwargs = {'requested': fb_provider, 'target_model': fb_model}
                    if entry.get('base_url'):
                        fb_kwargs['explicit_base_url'] = entry['base_url']
                    fb_api_key = resolve_entry_api_key(entry)
                    if fb_api_key:
                        fb_kwargs['explicit_api_key'] = fb_api_key
                    runtime = resolve_runtime_provider(**fb_kwargs)
                    model = fb_model
                    logger.info("Job '%s': fallback resolved to %s model %s", job_id, runtime.get('provider'), fb_model)
                    break
                except Exception as fb_exc:
                    logger.debug("Job '%s': fallback %s failed: %s", job_id, fb_provider, fb_exc)
            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc
        reasoning_config = resolve_reasoning_config(_cfg if isinstance(_cfg, dict) else {}, str(model))
        if cron_model_drift_guard_enabled(_cfg):
            _drift: list[str] = []
            _provider_snapshot = (job.get('provider_snapshot') or '').strip().lower()
            if _provider_snapshot and (not (job.get('provider') or '').strip()) and (not _cron_default_provider):
                _current_provider = str(primary_provider_for_drift or runtime.get('provider') or '').strip().lower()
                if _current_provider and _current_provider != _provider_snapshot:
                    _drift.append(f"provider '{_provider_snapshot}' -> '{_current_provider}'")
            _model_snapshot = (job.get('model_snapshot') or '').strip().lower()
            if _model_snapshot and (not (job.get('model') or '').strip()) and (not _cron_default_model):
                _current_model = str(primary_model_for_drift or '').strip().lower()
                if _current_model and _current_model != _model_snapshot:
                    _drift.append(f"model '{_model_snapshot}' -> '{_current_model}'")
            if _drift:
                _changes = '; '.join(_drift)
                logger.warning("Job '%s': SKIPPED — global inference config drifted since creation (%s) and this job is unpinned. Skipped to prevent unintended spend. Pin explicitly to proceed: `cronjob action=update job_id=%s provider=<p> model=<m>`.", job_id, _changes, job_id)
                raise RuntimeError(f'Skipped to prevent unintended spend: global inference config drifted since this job was created ({_changes}), and this job is unpinned. No inference call was made. To run on the new config, pin it explicitly: `cronjob action=update job_id={job_id} provider=<provider> model=<model>` (or pin the original values to keep them). See #44585.')
        fallback_model = get_fallback_chain(_cfg) or None
        credential_pool = None
        runtime_provider = str(runtime.get('provider') or '').strip().lower()
        if runtime_provider:
            try:
                from agent.credential_pool import load_pool
                pool = load_pool(runtime_provider)
                if pool.has_credentials():
                    credential_pool = pool
                    logger.info("Job '%s': loaded credential pool for provider %s with %d entries", job_id, runtime_provider, len(pool.entries()))
            except Exception as e:
                logger.debug("Job '%s': failed to load credential pool for %s: %s", job_id, runtime_provider, e)
        try:
            from tools.mcp_tool import discover_mcp_tools
            _mcp_tools = discover_mcp_tools()
            if _mcp_tools:
                logger.info("Job '%s': %d MCP tool(s) available", job_id, len(_mcp_tools))
        except Exception as _mcp_exc:
            logger.warning("Job '%s': MCP initialization failed (non-fatal): %s", job_id, _mcp_exc)
        agent = AIAgent(model=model, api_key=runtime.get('api_key'), base_url=runtime.get('base_url'), provider=runtime.get('provider'), requested_provider=runtime.get('requested_provider'), api_mode=runtime.get('api_mode'), acp_command=runtime.get('command'), acp_args=runtime.get('args'), max_iterations=max_iterations, reasoning_config=reasoning_config, prefill_messages=prefill_messages, fallback_model=fallback_model, credential_pool=credential_pool, providers_allowed=pr.get('only'), providers_ignored=pr.get('ignore'), providers_order=pr.get('order'), provider_sort=pr.get('sort'), openrouter_min_coding_score=(_cfg.get('openrouter') or {}).get('min_coding_score'), enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg), disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg), quiet_mode=True, skip_context_files=not bool(_job_workdir), load_soul_identity=True, skip_memory=True, skip_background_review=True, platform='cron', session_id=_cron_session_id, session_db=_session_db)
        _cron_timeout = _cron_inactivity_seconds()
        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        _POLL_INTERVAL = 5.0
        _job_schedule = job.get('schedule')
        _is_oneshot = isinstance(_job_schedule, dict) and _job_schedule.get('kind') == 'once'
        _run_claim = job.get('run_claim')
        _run_claim_owner = str(_run_claim.get('by') or '') if isinstance(_run_claim, dict) else ''
        _last_claim_heartbeat = time.monotonic()

        def _heartbeat_run_claim_if_due():
            nonlocal _last_claim_heartbeat
            if not _is_oneshot or not _run_claim_owner:
                return
            _mono = time.monotonic()
            if _mono - _last_claim_heartbeat < _RUN_CLAIM_HEARTBEAT_SECONDS:
                return
            _last_claim_heartbeat = _mono
            try:
                heartbeat_run_claim(job_id, expected_owner=_run_claim_owner)
            except Exception:
                logger.debug("Job '%s': run_claim heartbeat failed", job_name, exc_info=True)
        _cron_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _cron_context = contextvars.copy_context()
        _audit_fire_id = uuid.uuid4().hex
        _audit_t_start = time.monotonic()
        _cron_future = _cron_pool.submit(_cron_context.run, agent.run_conversation, prompt)
        _inactivity_timeout = False
        try:
            if _cron_inactivity_limit is None:
                if _is_oneshot:
                    result = None
                    while True:
                        done, _ = concurrent.futures.wait({_cron_future}, timeout=_POLL_INTERVAL)
                        if done:
                            result = _cron_future.result()
                            break
                        _heartbeat_run_claim_if_due()
                else:
                    result = _cron_future.result()
            else:
                result = None
                while True:
                    done, _ = concurrent.futures.wait({_cron_future}, timeout=_POLL_INTERVAL)
                    if done:
                        result = _cron_future.result()
                        break
                    _heartbeat_run_claim_if_due()
                    _idle_secs = 0.0
                    if hasattr(agent, 'get_activity_summary'):
                        try:
                            _act = agent.get_activity_summary()
                            _idle_secs = _act.get('seconds_since_activity', 0.0)
                        except Exception:
                            pass
                    if _idle_secs >= _cron_inactivity_limit:
                        _inactivity_timeout = True
                        break
        except Exception:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
        if _inactivity_timeout:
            _activity = {}
            if hasattr(agent, 'get_activity_summary'):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get('last_activity_desc', 'unknown')
            _secs_ago = _activity.get('seconds_since_activity', 0)
            _cur_tool = _activity.get('current_tool')
            _iter_n = _activity.get('api_call_count', 0)
            _iter_max = _activity.get('max_iterations', 0)
            logger.error("Job '%s' idle for %.0fs (inactivity limit %.0fs) | last_activity=%s | iteration=%s/%s | tool=%s", job_name, _secs_ago, _cron_inactivity_limit, _last_desc, _iter_n, _iter_max, _cur_tool or 'none')
            request_hard_interrupt(agent, 'Cron job timed out (inactivity)')
            raise TimeoutError(f"Cron job '{job_name}' idle for {int(_secs_ago)}s (limit {int(_cron_inactivity_limit)}s) — last activity: {_last_desc}")
        if not isinstance(result, dict):
            raise RuntimeError(f'agent.run_conversation returned {type(result).__name__} instead of dict: {result!r}')
        turn_exit_reason = str(result.get('turn_exit_reason') or '')
        final_response_text = (result.get('final_response') or '').strip()
        max_iteration_summary = result.get('failed') is not True and result.get('completed') is False and turn_exit_reason.startswith('max_iterations_reached(') and bool(final_response_text)
        if result.get('failed') is True or (result.get('completed') is False and (not max_iteration_summary)):
            _err_text = result.get('error') or final_response_text or 'agent reported failure'
            raise RuntimeError(_err_text)
        if max_iteration_summary:
            logger.warning("Job '%s' reached the iteration limit but produced a final fallback response; delivering the response instead of failing the cron run", job_name)
        final_response = result.get('final_response', '') or ''
        if final_response.strip() == '(No response generated)':
            final_response = ''
        if final_response.strip() and turn_exit_reason:
            try:
                _explainer_text = AIAgent._format_turn_completion_explanation(turn_exit_reason)
            except Exception:
                _explainer_text = ''
            if _explainer_text and final_response.strip() == _explainer_text.strip():
                logger.info("Job '%s': abnormal empty turn (%s) — suppressing explainer for cron delivery", job_id, turn_exit_reason)
                final_response = ''
        logged_response = final_response if final_response else '(No response generated)'
        output = f"# Cron Job: {job_name}\n\n**Job ID:** {job_id}\n**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n**Schedule:** {job.get('schedule_display', 'N/A')}\n\n## Prompt\n\n{prompt}\n\n## Response\n\n{logged_response}\n"
        logger.info("Job '%s' completed successfully", job_name)
        _audit_duration_ms = int((time.monotonic() - _audit_t_start) * 1000)
        _audit_response_silent = _is_cron_silence_response(final_response or '')
        _write_usage_audit({'ts': _utcnow_iso_ms(), 'job_id': job_id, 'fire_id': _audit_fire_id, 'prompt_tokens': result.get('prompt_tokens'), 'completion_tokens': result.get('completion_tokens'), 'total_tokens': result.get('total_tokens'), 'response_silent': _audit_response_silent, 'deliver_target': job.get('deliver'), 'model': model or None, 'duration_ms': _audit_duration_ms, 'error': None})
        return (True, output, final_response, None)
    except Exception as e:
        error_msg = f'{type(e).__name__}: {str(e)}'
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        if '_audit_fire_id' in locals():
            _audit_duration_ms = int((time.monotonic() - _audit_t_start) * 1000)
            _write_usage_audit({'ts': _utcnow_iso_ms(), 'job_id': job_id, 'fire_id': _audit_fire_id, 'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None, 'response_silent': False, 'deliver_target': job.get('deliver'), 'model': model or None, 'duration_ms': _audit_duration_ms, 'error': error_msg})
        output = f"# Cron Job: {job_name} (FAILED)\n\n**Job ID:** {job_id}\n**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n**Schedule:** {job.get('schedule_display', 'N/A')}\n\n## Prompt\n\n{prompt}\n\n## Error\n\n```\n{error_msg}\n```\n"
        return (False, output, '', error_msg)
    finally:
        if _job_workdir and _cwd_lock_acquired:
            if _prior_terminal_cwd == '_UNSET_':
                os.environ.pop('TERMINAL_CWD', None)
            else:
                os.environ['TERMINAL_CWD'] = _prior_terminal_cwd
        if _cwd_lock_acquired:
            if _holds_cwd_write:
                _terminal_cwd_lock.release_write()
            else:
                _terminal_cwd_lock.release_read()
        clear_session_vars(_ctx_tokens)
        if _cron_session_token is not None:
            _cron_session_var.reset(_cron_session_token)
        if _non_dispatcher_token is not None:
            exit_non_dispatcher_owned_context(_non_dispatcher_token)
        for _var_name in _cron_delivery_vars:
            _VAR_MAP[_var_name].set('')
        if _session_db:
            _final_cron_session_id = _cron_session_id
            try:
                _compression_tip = _session_db.get_compression_tip(_cron_session_id)
                if _compression_tip:
                    _final_cron_session_id = _compression_tip
            except (Exception, KeyboardInterrupt) as e:
                try:
                    _agent_session_id = getattr(agent, 'session_id', None)
                    if _agent_session_id:
                        _final_cron_session_id = _agent_session_id
                except (Exception, KeyboardInterrupt):
                    pass
                logger.debug("Job '%s': failed to resolve cron compression tip: %s", job_id, e)
            try:
                _title_base = ' '.join(job_name.split())[:60].strip() or f'cron {job_id}'
                _cron_title = f"{_title_base} · {_hermes_now().strftime('%b %d %H:%M')}"
                if not _set_cron_session_title(_session_db, _final_cron_session_id, _cron_title):
                    _set_cron_session_title(_session_db, _final_cron_session_id, f'cron {job_id}')
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to set cron session title: %s", job_id, e)
                for _fallback in (getattr(_session_db, 'get_next_title_in_lineage', lambda b: b)(f'cron {job_id}'), f'cron {job_id} {_final_cron_session_id[-6:]}'):
                    try:
                        if _set_cron_session_title(_session_db, _final_cron_session_id, _fallback):
                            break
                    except (Exception, KeyboardInterrupt):
                        continue
            try:
                _session_db.end_session(_final_cron_session_id, 'cron_complete')
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)
        if defer_agent_teardown is not None:
            if agent is not None:
                defer_agent_teardown.append(agent)
        else:
            _teardown_cron_agent(agent, job_id)

def _teardown_cron_agent(agent, job_id: str) -> None:
    """Release an ephemeral cron agent's async resources.

    Split out of ``run_job``'s ``finally`` so a caller that defers teardown
    (to deliver first — #58720) can invoke the identical cleanup AFTER delivery.
    Closes the agent (subprocesses, sandboxes, browser daemons, OpenAI/httpx
    client) and reaps stale async clients whose loop has since closed. Idempotent
    and independently guarded, matching the original inline behavior.
    """
    try:
        if agent is not None:
            agent.close()
    except (Exception, KeyboardInterrupt) as e:
        logger.debug("Job '%s': failed to close agent resources: %s", job_id, e)
    try:
        from agent.auxiliary_client import cleanup_stale_async_clients
        cleanup_stale_async_clients()
    except Exception as e:
        logger.debug("Job '%s': failed to reap stale auxiliary clients: %s", job_id, e)

def run_one_job(job: dict, *, adapters=None, loop=None, verbose: bool=False, extra_prompt: Optional[str]=None) -> bool:
    """Run ONE due job end-to-end: execute → save output → deliver → mark.

    This is the shared firing body extracted from ``tick``'s per-job closure so
    that BOTH the built-in ticker and an external provider's ``fire_due`` (e.g.
    Chronos) run the identical sequence — no duplicated correctness.

    It does NOT decide whether the job is due, claim it, or compute the next
    run — those are the caller's concern (``tick`` advances ``next_run_at``
    under the file lock before dispatch; an external provider claims via the
    store CAS). This function only fires the given job once.

    Returns True if the job was processed (even if the job itself failed —
    failure is recorded via ``mark_job_run``), False only if processing raised.
    """
    execution_id = job.get('execution_id')
    if not execution_id:
        execution_id = create_execution(job['id'], source='direct')['id']
    try:
        if not claim_dispatch(job['id']):
            logger.info("Job '%s': one-shot dispatch limit reached — skipping", job.get('name', job['id']))
            finish_execution(execution_id, success=False, error='Dispatch claim rejected; execution was not started.')
            return True
        mark_execution_running(execution_id)
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
        _scope_token = set_secret_scope(build_profile_secret_scope(_get_hermes_home()))
        _deferred_agents: list = []
        try:
            success, output, final_response, error = run_job(job, defer_agent_teardown=_deferred_agents, extra_prompt=extra_prompt)
        except BaseException:
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job['id'])
            raise
        finally:
            reset_secret_scope(_scope_token)
        delivery_error = None
        blocked_config = False
        try:
            output_file = save_job_output(job['id'], output)
            if verbose:
                logger.info('Output saved to: %s', output_file)
            if success and _is_interrupted(job['id']):
                success = False
                error = 'Interrupted by gateway shutdown before the run finished (tool subprocess was killed mid-flight).'
            blocked_config_silent = bool(error) and BLOCKED_CONFIG_SILENT_MARKER in str(error)
            blocked_config = blocked_config_silent or (bool(error) and BLOCKED_CONFIG_MARKER in str(error))
            if blocked_config and (not success):
                _pf_text = re.sub('\\[blocked_config[^\\]]*\\]\\s*', '', str(error)).strip()
                deliver_content = f"⛔ Cron '{job.get('name') or job['id']}' blocked by configuration validation (no LLM call was made): {_pf_text} This alert is sent once; the job stays blocked until the configuration is fixed."
            else:
                deliver_content = final_response if success else _summarize_cron_failure_for_delivery(job, error)
            should_deliver = bool(deliver_content.strip())
            if blocked_config_silent:
                should_deliver = False
            unresolved_origin = False
            if should_deliver and success and _is_cron_silence_response(deliver_content):
                logger.info("Job '%s': agent returned %s — skipping delivery", job['id'], SILENT_MARKER)
                should_deliver = False
            if should_deliver:
                unresolved_origin = _normalize_deliver_value(job.get('deliver', 'local')) == 'origin' and (not _resolve_delivery_targets(job))
                try:
                    delivery_error = _deliver_result(job, deliver_content, adapters=adapters, loop=loop)
                except Exception as de:
                    delivery_error = str(de)
                    logger.error('Delivery failed for job %s: %s', job['id'], de)
        finally:
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job['id'])
        if success and (not final_response.strip()):
            success = False
            error = 'Agent completed but produced empty response (model error, timeout, or misconfiguration)'
        if not _consume_interrupted_flag(job['id']):
            if blocked_config:
                mark_job_run(job['id'], success, error, delivery_error=delivery_error, status='blocked_config')
            else:
                mark_job_run(job['id'], success, error, delivery_error=delivery_error)
        normalized_deliver = _normalize_deliver_value(job.get('deliver', 'local'))
        if delivery_error:
            delivery_outcome = 'failed'
        elif should_deliver and unresolved_origin:
            delivery_outcome = 'not_configured'
        elif should_deliver and normalized_deliver != 'local':
            delivery_outcome = 'delivered'
        else:
            delivery_outcome = 'suppressed'
        finish_execution(execution_id, success=success, error=error, delivery_outcome=delivery_outcome)
        return True
    except BaseException as e:
        _err_text = str(e) or type(e).__name__
        logger.error('Error processing job %s: %s', job['id'], _err_text)
        try:
            if not _consume_interrupted_flag(job['id']):
                mark_job_run(job['id'], False, _err_text)
        except Exception as record_err:
            logger.error('Failed to record interrupted run for job %s: %s', job['id'], record_err)
        try:
            finish_execution(execution_id, success=False, error=_err_text)
        except Exception as record_err:
            logger.error('Failed to finish execution record for job %s: %s', job['id'], record_err)
        if not isinstance(e, Exception):
            raise
        return False

def _notify_provider_jobs_changed() -> None:
    """Best-effort: tell the active scheduler provider the job set changed.

    Called by the consumer surfaces (model tool / CLI / REST) AFTER a
    successful store mutation (create/update/remove/pause/resume) so an external
    provider (Chronos) can re-provision/cancel the affected one-shot via NAS.
    No-op for the built-in (it re-reads jobs.json each tick), so the default
    path is unchanged. Lives here (not in cron/jobs.py) to keep the store free
    of provider imports — avoids an import cycle and keeps jobs.py low-coupling.
    Never raises into the caller.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        resolve_cron_scheduler().on_jobs_changed()
    except Exception as e:
        logger.debug('on_jobs_changed notify failed: %s', e)

class CronSchedulerRegistrationError(RuntimeError):
    """A job was persisted but its first external trigger was not registered."""

    def __init__(self, job: dict, cause: Exception) -> None:
        self.job = job
        self.cause = cause
        super().__init__(f"Cron job '{job['id']}' was saved, but its first scheduler registration failed ({type(cause).__name__}). Do not create a duplicate. Pause/resume or update the job to retry registration.")

    def user_message(self) -> str:
        """Human-facing variant for chat/CLI surfaces (no exception class name)."""
        label = self.job.get('name') or self.job['id']
        return f"Saved cron job '{label}', but couldn't register it with the external scheduler yet. The job is kept — don't re-create it; pause/resume or edit it (e.g. via /cron) to retry registration."

    def to_dict(self) -> dict:
        """Return the public partial-failure contract without provider details."""
        return {'error': str(self), 'job_id': self.job['id'], 'job_saved': True, 'scheduler_registered': False, 'retry_create': False}

def create_job_with_scheduler_registration(**kwargs) -> dict:
    """Persist one job and register its first trigger with the active provider."""
    from cron.jobs import create_job
    from cron.scheduler_provider import resolve_cron_scheduler
    job = create_job(**kwargs)
    try:
        resolve_cron_scheduler().register_job(job)
    except Exception as exc:
        raise CronSchedulerRegistrationError(job, exc) from exc
    return job

def tick(verbose: bool=True, adapters=None, loop=None, sync: bool=True, *, can_dispatch=None):
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
        adapters: Optional dict mapping Platform → live adapter (from gateway)
        loop: Optional asyncio event loop (from gateway) for live adapter sends
        can_dispatch: Optional synchronous gate; false leaves due jobs untouched
            for the next allowed tick

    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    lock_dir, lock_file = _get_lock_paths()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(lock_file, 'w', encoding='utf-8')
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug('Tick skipped — another instance holds the lock')
        if lock_fd is not None:
            lock_fd.close()
        return 0
    try:
        try:
            from agent.estop import check_paused as _estop_check_paused
            if _estop_check_paused('cron', logger):
                return 0
        except ImportError:
            pass
        if can_dispatch is not None and (not can_dispatch()):
            logger.debug('Cron dispatch paused while gateway drains existing work')
            return 0
        due_jobs = get_due_jobs()
        if not due_jobs:
            if verbose:
                logger.info('%s - No jobs due', _hermes_now().strftime('%H:%M:%S'))
            try:
                from tools.mcp_tool import _kill_orphaned_mcp_children
                _kill_orphaned_mcp_children()
            except Exception as _e:
                logger.debug('Post-tick MCP orphan cleanup failed: %s', _e)
            return 0
        if verbose:
            logger.info('%s - %s job(s) due', _hermes_now().strftime('%H:%M:%S'), len(due_jobs))
        advance_next_runs([job['id'] for job in due_jobs])
        _max_workers: Optional[int] = None
        try:
            _env_par = os.getenv('HERMES_CRON_MAX_PARALLEL', '').strip()
            if _env_par:
                _max_workers = int(_env_par) or None
        except (ValueError, TypeError):
            logger.warning('Invalid HERMES_CRON_MAX_PARALLEL value; defaulting to unbounded')
        if _max_workers is None:
            try:
                _ucfg = load_config() or {}
                _cfg_par = (_ucfg.get('cron', {}) if isinstance(_ucfg, dict) else {}).get('max_parallel_jobs')
                if _cfg_par is not None:
                    _max_workers = int(_cfg_par) or None
            except Exception:
                pass
        if verbose:
            logger.info('Running %d job(s) in parallel (max_workers=%s)', len(due_jobs), _max_workers if _max_workers else 'unbounded')

        def _process_job(job: dict) -> bool:
            """Run one due job end-to-end. Thin wrapper around the shared
            module-level ``run_one_job`` so ``tick`` and external providers
            (Chronos ``fire_due``) use the identical execute→save→deliver→mark
            body."""
            return run_one_job(job, adapters=adapters, loop=loop, verbose=verbose)
        sequential_jobs = [j for j in due_jobs if (j.get('workdir') or '').strip()]
        parallel_jobs = [j for j in due_jobs if not (j.get('workdir') or '').strip()]
        _results: list = []
        _all_futures: list = []

        def _submit_with_guard(job: dict, pool: concurrent.futures.ThreadPoolExecutor):
            """Submit a job fire-and-forget with the in-flight dedup guard.

            Returns the future, or None if the job was skipped because a prior
            tick's run of the same job is still in flight.  The running-set
            membership is released in the worker's finally block.
            """
            job_id = job['id']
            if _interpreter_shutting_down():
                logger.warning("Job '%s' not dispatched — interpreter is shutting down", job.get('name', job_id))
                return None
            if not try_register_running_job(job_id):
                logger.info("Job '%s' already running — skipping", job.get('name', job_id))
                return None
            execution = create_execution(job_id, source='builtin')
            dispatched_job = dict(job, execution_id=execution['id'])
            _ctx = contextvars.copy_context()

            def _run_and_release(j=dispatched_job, ctx=_ctx):
                try:
                    return ctx.run(_process_job, j)
                finally:
                    release_running_job(j['id'])
            try:
                return pool.submit(_run_and_release)
            except Exception as submit_err:
                release_running_job(job_id)
                finish_execution(execution['id'], success=False, error=f'Executor dispatch failed: {submit_err}')
                if isinstance(submit_err, RuntimeError) and _interpreter_shutting_down(submit_err):
                    logger.warning("Job '%s' not dispatched — interpreter is shutting down", job.get('name', job_id))
                    return None
                logger.error("Job '%s' not dispatched: %s", job.get('name', job_id), submit_err)
                return None
        if sequential_jobs:
            seq_pool = _get_sequential_pool()
            for job in sequential_jobs:
                fut = _submit_with_guard(job, seq_pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)
        if parallel_jobs:
            pool = _get_parallel_pool(_max_workers)
            for job in parallel_jobs:
                fut = _submit_with_guard(job, pool)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)

        def _sweep_mcp_orphans() -> None:
            try:
                from tools.mcp_tool import _kill_orphaned_mcp_children
                _kill_orphaned_mcp_children()
            except Exception as _e:
                logger.debug('Post-tick MCP orphan cleanup failed: %s', _e)
        if sync:
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    logger.error('Cron job future failed: %s', exc)
                    _results.append(False)
            _sweep_mcp_orphans()
            return sum(_results)
        if _all_futures:
            _remaining = [len(_all_futures)]

            def _on_done(_f: concurrent.futures.Future) -> None:
                _remaining[0] -= 1
                try:
                    _exc = _f.exception()
                    if _exc is not None:
                        logger.error('Cron job future failed in async mode: %s', _exc, exc_info=(type(_exc), _exc, _exc.__traceback__))
                except Exception:
                    pass
                if _remaining[0] <= 0:
                    _sweep_mcp_orphans()
            for _f in _all_futures:
                _f.add_done_callback(_on_done)
        else:
            _sweep_mcp_orphans()
        return sum(_results)
    finally:
        if fcntl:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()
if __name__ == '__main__':
    tick(verbose=True)