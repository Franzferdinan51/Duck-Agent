"""
SQLite State Store for Duck Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""
import asyncio
import atexit
import errno
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from agent.memory_manager import sanitize_context
from agent.session_activity import ActivityProvenance
from agent.message_sanitization import _sanitize_surrogates
from agent.skill_commands import SKILL_EXCERPT_JOINT, SKILL_SCAFFOLD_SQL_LIKE, describe_skill_invocation
from hermes_constants import get_hermes_home
from hermes_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable as _is_sqlite_wal_reset_vulnerable
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from hermes_state_common import _BRANCH_CHILD_SQL, _COMPRESSION_CHILD_SQL, _FTS_CJK_TRIGGERS, _FTS_TRIGGERS, _LISTABLE_CHILD_SQL, _PREVIEW_RAW_SELECT, _ephemeral_child_sql, _shape_preview, _sql_session_last_active, _sql_session_last_active_by_id, escape_like as _escape_like, DEFERRED_INDEX_SQL, FTS_CJK_STALE_KEY, FTS_SQL, FTS_STORAGE_VERSION, FTS_TRIGRAM_SQL, LEGACY_FTS_SQL, LEGACY_FTS_TRIGRAM_SQL, MAX_FTS5_QUERY_CHARS, SCHEMA_SQL, SCHEMA_VERSION, _PREVIEW_CONTENT_SQL, _PREVIEW_HEAD_CHARS, _PREVIEW_MAX_CHARS, _PREVIEW_SCAFFOLD_WINDOW, _PREVIEW_SCAFFOLDED_SQL
from hermes_state_portability import SessionPortabilityMixin
from hermes_state_schema import SessionSchemaMixin
from hermes_state_search import SessionSearchMixin
try:
    import psutil
except ImportError:
    psutil = None
logger = logging.getLogger(__name__)
_COMPRESSION_LOCK_HOLDER_PID_RE = re.compile('(?:^|:)pid=(\\d+)(?::|$)')

def _system_prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()

def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """Return True only when a structured lock holder's local PID is gone.

    Compression locks are stored in a host-local SQLite database and holder
    IDs created by ``conversation_compression`` start with ``pid=<n>``. A
    process killed during gateway shutdown cannot release its lease, so waiting
    for the full TTL makes every new turn repeatedly attempt compaction. Reclaim
    only when the kernel proves that PID no longer exists; legacy/unstructured
    holders, same-process holders, permission errors, and any probe doubt
    remain protected until normal TTL expiry (conservative: PID reuse must
    never steal a live lease, and a wrongly-kept lease self-heals via TTL).
    """
    match = _COMPRESSION_LOCK_HOLDER_PID_RE.search(holder or '')
    if match is None:
        return False
    try:
        pid = int(match.group(1))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return False
    if psutil is not None:
        try:
            return not psutil.pid_exists(pid)
        except Exception:
            return False
    if os.name == 'nt':
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError):
        return False
    return False

def _scrub_surrogates(value: Any) -> Any:
    """Replace lone surrogates when *value* is text; pass anything else through.

    sqlite3 encodes bound ``str`` parameters as UTF-8 and raises
    ``UnicodeEncodeError`` on lone surrogates (U+D800..U+DFFF), so a single
    such code point anywhere in a message aborts the whole write. No-op for
    well-formed text.
    """
    return _sanitize_surrogates(value) if isinstance(value, str) else value

def workspace_key(row: Dict[str, Any]) -> Optional[str]:
    """A session's workspace grouping key: its git repo root when known, else
    its cwd.

    Branch is deliberately excluded so checking out a new branch doesn't
    fragment a workspace's session history. Returns None for cwd-less (unbound)
    sessions. Both fields are already recorded on ``sessions`` — this just picks
    the coarser identity for grouping/filtering.
    """
    root = (row.get('git_repo_root') or '').strip()
    if root:
        return root
    cwd = (row.get('cwd') or '').strip()
    return cwd or None

def _delegate_from_json(col: str='model_config') -> str:
    return f"json_extract(COALESCE({col}, '{{}}'), '$._delegate_from')"
_MODEL_CONFIG_ROW_MISSING = object()

def _cwd_prefix_clause(cwd_prefix: str) -> Tuple[str, List[str]]:
    prefix = cwd_prefix.rstrip('/\\') or cwd_prefix
    esc = _escape_like(prefix)
    return ("(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\' OR s.cwd LIKE ? ESCAPE '\\')", [prefix, f'{esc}/%', f'{esc}\\\\%'])

def _workspace_key_clause(key: str) -> Tuple[str, List[str]]:
    """Match sessions whose ``workspace_key(row)`` equals ``key``.

    Mirrors :func:`workspace_key`: a session belongs to workspace ``key``
    when its recorded ``git_repo_root`` equals ``key``, or — for rows that
    predate per-session git metadata — when its ``cwd`` is at or under
    ``key`` (so a session started in ``repo/src`` still groups with ``repo``).
    Used by ``duck-agent -c``/``--resume`` to continue the most recent session in
    the *current* workspace rather than the global MRU.
    """
    prefix = key.rstrip('/\\') or key
    cwd_clause, cwd_params = _cwd_prefix_clause(prefix)
    return (f"(s.git_repo_root = ? OR (COALESCE(s.git_repo_root, '') = '' AND {cwd_clause}))", [prefix, *cwd_params])

def _collect_delegate_child_ids(conn, parent_ids: List[str]) -> List[str]:
    """Delegate-subagent ids to cascade-delete with *parent_ids*.

    Only rows carrying the ``_delegate_from`` marker (set at creation, and
    backfilled by the v16 migration) — generic untagged children keep the
    orphan-don't-delete contract. Walks marker chains recursively so an
    orchestrator subagent's own delegate children go too (FK safety).
    """
    df = _delegate_from_json()
    seeds = {sid for sid in parent_ids if sid}
    found: set[str] = set(seeds)
    frontier = list(seeds)
    while frontier:
        ph = ','.join('?' * len(frontier))
        cursor = conn.execute(f'SELECT id FROM sessions WHERE {df} IN ({ph}) OR (parent_session_id IN ({ph}) AND {df} IS NOT NULL)', frontier + frontier)
        frontier = [row['id'] for row in cursor.fetchall() if row['id'] not in found]
        found.update(frontier)
    return [sid for sid in found if sid not in seeds]

def _delete_delegate_children(conn, parent_ids: List[str]) -> List[str]:
    ids = _collect_delegate_child_ids(conn, parent_ids)
    if ids:
        ph = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM messages WHERE session_id IN ({ph})', ids)
        conn.execute(f'UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})', ids)
        conn.execute(f'DELETE FROM sessions WHERE id IN ({ph})', ids)
    return ids
T = TypeVar('T')
DEFAULT_DB_PATH = get_hermes_home() / 'state.db'
_IMPORT_DEFAULT_DB_PATH = DEFAULT_DB_PATH

def _default_db_path() -> Path:
    """Resolve the default state DB path at call time.

    ``DEFAULT_DB_PATH`` is computed when this module is first imported, which
    freezes the developer's real ``~/.duck-agent`` even when a test fixture later
    redirects ``DUCK_AGENT_HOME`` — importing this module during collection was
    enough to point every default ``SessionDB()`` at the real state.db.

    Precedence:

    1. A deliberately re-pointed ``DEFAULT_DB_PATH`` (differs from the
       import-time snapshot — the established test escape hatch) wins.
    2. Otherwise resolve ``get_hermes_home()`` fresh so a runtime
       ``DUCK_AGENT_HOME`` redirect takes effect regardless of import order.
    """
    if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH:
        return DEFAULT_DB_PATH
    return get_hermes_home() / 'state.db'
_STATE_DB_GUARD_BYPASS = False
_STATE_DB_GUARD_EXTRA_DENY_ROOTS: Tuple[Path, ...] = ()

def _real_platform_state_root() -> Optional[Path]:
    """Resolve the REAL platform-default Duck Agent root for the guard.

    Deliberately avoids ``Path.home()`` / ``hermes_constants``: tests
    routinely monkeypatch ``Path.home`` to a tempdir, and ``hermes_state``
    is often imported lazily *while* such a patch is active — resolving
    through the patched callable would misidentify the test's own hermetic
    home as "production" (false positive) or, worse, miss the real one
    (false negative).  ``os.path.expanduser`` reads the HOME environment
    variable / passwd entry, which the hermetic conftest never rewrites.
    """
    try:
        if sys.platform == 'win32':
            base = os.environ.get('LOCALAPPDATA', '').strip()
            root = Path(base) / 'duck-agent' if base else Path(os.path.expanduser('~')) / 'AppData' / 'Local' / 'duck-agent'
        else:
            root = Path(os.path.expanduser('~')) / '.duck-agent'
        return root.resolve()
    except Exception:
        return None

def _running_under_pytest() -> bool:
    """True when this process (or a parent test process) is a pytest run."""
    return bool(os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('PYTEST_VERSION'))

def _production_state_roots() -> List[Path]:
    roots: List[Path] = []
    real_root = _real_platform_state_root()
    if real_root is not None:
        roots.append(real_root)
    for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            continue
    return roots

def _is_production_state_db(resolved: Path, root: Path) -> bool:
    """True when *resolved* is a DB file of the real Duck Agent home *root*.

    Matches files directly in the root (``<root>/state.db``) and profile
    homes (``<root>/profiles/<name>/state.db``).  Deliberately does NOT
    match deeper scratch paths (e.g. repo worktrees that happen to live
    under ``~/.duck-agent/duck-agent/...``) so hermetic tests using unusual
    tempdirs cannot false-positive.
    """
    if resolved.parent == root:
        return True
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) == 3 and parts[0] == 'profiles'

def _ensure_test_isolation(db_path: Path) -> None:
    """Fail hard when a pytest-context process resolves a production DB.

    Raises ``RuntimeError`` before any connection, mkdir, journal-mode
    pragma, or byte probe can touch the live database.  No-op outside
    pytest and for hermetic (tmp ``DUCK_AGENT_HOME``) paths.
    """
    if _STATE_DB_GUARD_BYPASS or not _running_under_pytest():
        return
    try:
        resolved = Path(db_path).expanduser().resolve()
    except Exception:
        return
    for root in _production_state_roots():
        if _is_production_state_db(resolved, root):
            raise RuntimeError(f'live-system guard: test attempted to open production state.db at {resolved} (under real Duck Agent root {root}). Tests must run against a temporary DUCK_AGENT_HOME — pass an explicit tmp db_path or let the hermetic conftest redirect DUCK_AGENT_HOME. If this test genuinely needs the live database, mark it with @pytest.mark.live_system_guard_bypass.')
_WAL_INCOMPAT_MARKERS = ('locking protocol', 'not authorized', 'disk i/o error')
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()
_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()
_wal_reset_bug_warned_paths: set[str] = set()
_wal_reset_bug_warned_lock = threading.Lock()

def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear) the most recent state.db init failure.

    Thread-safe via _last_init_error_lock.  Callers pass a message to
    record a failure or None to clear.  SessionDB.__init__ only calls
    this to SET on failure — it deliberately does NOT clear on success,
    because in a multi-threaded caller (e.g. gateway / web_server per-
    request SessionDB() instantiation), a concurrent successful open
    racing past a different thread's failure would erase the cause
    string that thread's /resume handler is about to format.  Explicit
    clears (e.g. test fixtures) are still supported by passing None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg

def get_last_init_error() -> Optional[str]:
    """Return the most recent state.db init failure, if any.

    Slash-command handlers (``/resume``, ``/title``, ``/history``, ``/branch``)
    call this to surface the underlying cause in their error messages when
    ``_session_db is None``.  Returns ``None`` if SessionDB initialized
    successfully (or hasn't been attempted).
    """
    return _last_init_error
_REVIEW_HARNESS_PREFIXES = ('Review the conversation above and update the skill library', 'Review the conversation above and consider saving to memory')

def _is_background_review_harness_message(msg: Dict[str, Any]) -> bool:
    """True when ``msg`` is a persisted background-review harness prompt.

    These are user/system turns the forked skill/memory review agent wrote into
    a real session in older builds (before the ``_persist_disabled`` isolation
    fix). They instruct the agent to act as the curator under a hard tool
    restriction, so replaying them as live history hijacks the session.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get('role') not in {'user', 'system'}:
        return False
    content = msg.get('content')
    if not isinstance(content, str):
        return False
    head = content.lstrip()
    return any((head.startswith(p) for p in _REVIEW_HARNESS_PREFIXES))

def _strip_background_review_harness(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop background-review harness messages and the curator-mode assistant
    reply that immediately followed each one.

    Walk the list once; when a harness user/system message is found, skip it and
    also skip the next message if it is the assistant turn that answered it.
    Everything else passes through untouched and in order.
    """
    if not messages:
        return messages
    out: List[Dict[str, Any]] = []
    skip_next_assistant = False
    for msg in messages:
        if _is_background_review_harness_message(msg):
            skip_next_assistant = True
            continue
        if skip_next_assistant:
            skip_next_assistant = False
            if isinstance(msg, dict) and msg.get('role') == 'assistant':
                continue
        out.append(msg)
    return out
_STALE_TOOL_CALL_MARKER_RE = re.compile('^\\[[A-Za-z_][A-Za-z0-9_.-]*\\]$')

def _is_stale_tool_call_marker_message(msg: Dict[str, Any]) -> bool:
    """True when ``msg`` is a persisted assistant turn whose content is a bare
    bracketed marker (e.g. ``[memory]``) left over from a tool-call turn.

    Before the #78148 fix in ``agent.conversation_loop``, a local tool-call
    template could emit a bare marker as assistant content alongside a real
    tool call. The loop cached that marker as a fallback and later replayed
    it as the "final response", persisting it into the session. Sessions
    written before the fix can still carry these rows.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get('role') != 'assistant':
        return False
    if not msg.get('tool_calls'):
        return False
    content = msg.get('content')
    if not isinstance(content, str):
        return False
    return bool(_STALE_TOOL_CALL_MARKER_RE.fullmatch(content.strip()))

def _strip_stale_tool_call_markers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clear bare protocol-marker content persisted before the #78148 fix.

    Replaying "[memory]" as if the model had actually answered teaches the
    model, by example, to keep emitting the same marker in later turns — the
    exact symptom the issue reported. Only the stray ``content`` field is
    blanked; the tool call and its result are left untouched so provider
    tool_call/tool_result pairing stays intact. Sessions with no affected
    rows pass through unchanged.
    """
    repaired = 0
    for msg in messages:
        if _is_stale_tool_call_marker_message(msg):
            msg['content'] = ''
            repaired += 1
    if repaired:
        logger.info('Cleared %d stale tool-call marker message(s) while restoring session (#78148)', repaired)
    return messages

def format_session_db_unavailable(prefix: str='Session database not available') -> str:
    """Format a user-facing 'session DB unavailable' message with cause.

    When ``SessionDB()`` init fails, callers set ``_session_db = None`` and
    several slash commands (/resume, /title, /history, /branch) previously
    responded with a bare ``"Session database not available."`` — no
    indication of WHY.  This helper includes the captured cause (typically
    ``"locking protocol"`` from NFS/SMB) and points users at the known
    culprit so they can fix it themselves.

    Example output:
        Session database not available: locking protocol (state.db may be
        on NFS/SMB — see https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f'{prefix}.'
    hint = ''
    if any((marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS)):
        hint = ' (state.db may be on NFS/SMB/FUSE/ZFS — see https://www.sqlite.org/wal.html)'
    return f'{prefix}: {cause}{hint}.'

def _on_disk_journal_mode(conn: sqlite3.Connection) -> Optional[str]:
    """Read the journal mode from the SQLite DB header on disk.

    Returns the mode string (e.g. ``"wal"``, ``"delete"``), or ``None``
    if the value cannot be determined (new DB, or PRAGMA read failed).
    """
    try:
        row = conn.execute('PRAGMA journal_mode').fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    mode = row[0]
    if isinstance(mode, bytes):
        try:
            mode = mode.decode('ascii')
        except UnicodeDecodeError:
            return None
    return str(mode).strip().lower() if mode is not None else None

def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> None:
    """Enable ``PRAGMA checkpoint_fullfsync`` on macOS (no-op elsewhere).

    On Darwin, ``synchronous=FULL`` (the WAL default) issues a plain
    ``fsync()``, which Apple documents does *not* guarantee that data
    has reached stable storage or that writes are not reordered — see
    the ``fsync(2)`` man page.  SQLite's WAL corruption-safety guarantee
    assumes the OS honors the fsync write barrier; macOS does not unless
    the app uses ``F_FULLFSYNC``.

    During a launchd *system* shutdown/reboot the OS page cache is
    dropped (effectively a power-loss event for in-flight pages), so a
    WAL checkpoint whose ``fsync()`` "reported" durable may never have
    hit the platter — corrupting ``state.db`` with a malformed image.
    This is the trigger in issue #30636 ("SIGTERM during launchd
    shutdown under high load"), distinct from a plain in-session kill
    (which the page cache survives and SQLite recovers from).

    ``checkpoint_fullfsync=1`` forces an ``F_FULLFSYNC`` barrier only at
    checkpoint boundaries — where WAL frames land in the main DB — so the
    cost amortizes to roughly +0.1 ms/commit (vs ~+4 ms for the broader
    ``fullfsync=1`` that flushes on every commit's WAL sync).  Guarded by
    ``sys.platform == "darwin"`` because ``F_FULLFSYNC`` is macOS-only;
    on other platforms the PRAGMA is a no-op, so we skip it entirely.

    Best-effort: never raises.
    """
    if sys.platform != 'darwin':
        return
    try:
        conn.execute('PRAGMA checkpoint_fullfsync=1')
    except sqlite3.OperationalError:
        pass

def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    """Enforce ``PRAGMA synchronous=FULL`` on macOS to prevent btree corruption.

    On Darwin, the default ``synchronous=NORMAL`` only calls ``fsync()``,
    which Apple's fsync(2) man page explicitly states does *not* guarantee
    data-on-platter or write-ordering. During a WAL checkpoint race with
    process termination (e.g., launchd shutdown), this can leave the main
    DB with half-written btree pages → ``btreeInitPage error 11``.

    WAL mode's durability guarantee assumes the OS honors fsync barriers;
    macOS does not unless we explicitly set ``synchronous=FULL``, which issues
    a real ``fsync()`` on every transaction commit.  The ``F_FULLFSYNC``
    barrier at checkpoint boundaries is handled separately by
    :func:`_apply_macos_checkpoint_barrier`.

    This function is called after any successful WAL activation (either
    from ``apply_wal_with_fallback()`` setting a fresh WAL or when probing
    an existing WAL mode). It ensures macOS connections always use FULL
    synchronous mode, even if a prior connection set ``synchronous=NORMAL``.

    Best-effort: never raises.
    """
    if sys.platform != 'darwin':
        return
    try:
        conn.execute('PRAGMA synchronous=FULL')
    except sqlite3.OperationalError:
        pass

def is_sqlite_wal_reset_vulnerable(version_info: Optional[tuple]=None) -> bool:
    """Return True when the linked SQLite library has the WAL-reset bug.

    Upstream documents the bug in versions 3.7.0 through 3.51.2, fixed in
    3.51.3+, with backports 3.50.7 and 3.44.6:
    https://sqlite.org/wal.html#walresetbug

    Pre-WAL libraries (< 3.7.0) cannot hit the race and are treated as safe.
    """
    info = version_info if version_info is not None else sqlite3.sqlite_version_info
    return _is_sqlite_wal_reset_vulnerable(info)

def sqlite_source_id() -> str:
    """Return ``sqlite_source_id()``, or an empty string when unavailable."""
    try:
        conn = sqlite3.connect(':memory:')
        try:
            row = conn.execute('SELECT sqlite_source_id()').fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ''
    if not row or row[0] is None:
        return ''
    return str(row[0])

def resolve_journal_mode() -> str:
    """Return the configured journal mode (``wal`` or ``delete``).

    ``database.journal_mode`` in config.yaml is the canonical operator
    setting. ``wal`` remains the default; use ``delete`` when the backing
    filesystem does not provide WAL-safe durability (for example macOS
    virtiofs, NFS, or SMB). Invalid or malformed values fail safely to the
    existing default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        database = config.get('database', {})
        if not isinstance(database, dict):
            return 'wal'
        raw = database.get('journal_mode', 'wal')
    except Exception:
        return 'wal'
    if not isinstance(raw, str):
        return 'wal'
    mode = raw.strip().lower()
    return mode if mode in ('wal', 'delete') else 'wal'

class WalUnsupportedError(sqlite3.OperationalError):
    """Raised by :func:`apply_wal_with_fallback` when ``require_wal=True`` and
    the filesystem cannot provide WAL journal mode.

    Covers both shapes of WAL refusal on network filesystems (NFS / SMB / FUSE
    / the AgentFS NFS overlay): SQLite *raising* ``SQLITE_PROTOCOL`` ("locking
    protocol"), and the quieter macOS-NFS case where ``PRAGMA journal_mode=WAL``
    silently returns the still-effective mode without raising.  Subclasses
    ``sqlite3.OperationalError`` so existing ``except sqlite3.OperationalError``
    DB-init handling still catches it, while callers that specifically mandate
    WAL can catch this narrower type.
    """

def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str='state.db', require_wal: bool=False) -> str:
    """Set ``journal_mode=WAL`` on ``conn``, falling back to DELETE on failure.

    Returns the journal mode actually set (``"wal"`` or ``"delete"``).

    On WAL-incompatible filesystems (NFS, SMB, some FUSE, ZFS), SQLite either
    raises ``OperationalError("locking protocol")`` /
    ``OperationalError("disk I/O error")`` or — on macOS NFS / SMB /
    the AgentFS NFS overlay — silently refuses the switch and leaves the DB in
    DELETE.  Either way the degradation is logged at ERROR level (it is a real
    loss of concurrency — a write blocks concurrent readers — not a cosmetic
    warning) and, by default, the function falls back to DELETE (the pre-WAL
    default, which works on NFS and ZFS) so the feature keeps working.

    On SQLite builds that still contain the WAL-reset corruption bug
    (issue #69784), refuse to enable WAL on fresh / non-WAL databases
    (prefer DELETE).  If the on-disk DB is already WAL, keep WAL and warn
    — never live-downgrade under possible concurrent openers.

    This gate (#70055) is deliberately RETAINED. An earlier revision of the
    lock-cancellation fix (#71724) reverted it on the theory that DELETE was
    "the mode that corrupts", but that comparison was confounded: the clean
    WAL result came from SQLite 3.53.1, which carries BOTH the WAL-reset fix
    AND 3.51.0's defenses against close()-broken POSIX locks, so it says
    nothing about 3.50.4.  Re-measured on the actually-bundled 3.50.4 with
    the lock fix in place, WAL and DELETE are both clean (0/3 each) — i.e.
    there is no evidence that WAL is safer here, and upstream still documents
    the WAL-reset bug as real through 3.51.2 with serious consequences.  Until
    a fixed runtime is delivered, keep new databases out of WAL.

    Callers that genuinely require WAL concurrency (and would rather fail loudly
    than run silently degraded) pass ``require_wal=True``; the function then
    raises :class:`WalUnsupportedError` instead of returning ``"delete"``.  All
    current callers deliberately keep the default ``require_wal=False`` so
    NFS-homed installs keep working.

    The ERROR is deduplicated per ``db_label``: repeated connections to the
    same underlying DB (e.g. kanban_db.connect() which is called on every
    kanban operation) log once per process, not once per call.  Different
    db_labels log independently, so state.db and kanban.db each get one error
    on the same NFS mount.

    Shared by :class:`SessionDB` and ``hermes_cli.kanban_db.connect`` so
    both databases get identical fallback behavior.

    Never downgrades to DELETE if the on-disk DB header reports WAL — see
    _on_disk_journal_mode.  That holds for both the NFS path and the
    WAL-reset vulnerability path.
    """
    configured = resolve_journal_mode()
    if is_sqlite_wal_reset_vulnerable():
        return _apply_delete_for_wal_reset_bug(conn, db_label=db_label, require_delete=configured == 'delete')
    current_mode = _on_disk_journal_mode(conn)
    if current_mode == 'wal':
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return 'wal'
    if configured == 'delete':
        if current_mode is None:
            raise sqlite3.OperationalError('could not verify journal mode before applying configured journal_mode=delete (database is locked — possible concurrent openers); refusing to downgrade a database this process does not exclusively own')
        actual = _set_journal_mode_no_wait(conn, 'DELETE')
        if actual != 'delete':
            raise sqlite3.OperationalError(f"could not set configured journal_mode=delete (got {actual or 'no result'})")
        return actual
    try:
        row = conn.execute('PRAGMA journal_mode=WAL').fetchone()
        mode = str(row[0]).strip().lower() if row and row[0] is not None else ''
        if mode == 'wal':
            _apply_macos_checkpoint_barrier(conn)
            _enforce_macos_synchronous_full(conn)
            return 'wal'
        silent_exc = WalUnsupportedError(f'journal_mode=WAL refused without raising (still {mode!r})')
        if require_wal:
            raise silent_exc
        _log_wal_fallback_once(db_label, silent_exc)
        return mode or 'delete'
    except sqlite3.OperationalError as exc:
        if isinstance(exc, WalUnsupportedError):
            raise
        msg = str(exc).lower()
        if not any((marker in msg for marker in _WAL_INCOMPAT_MARKERS)):
            raise
        if 'disk i/o error' in msg:
            for _ in range(2):
                time.sleep(0.05)
                try:
                    row = conn.execute('PRAGMA journal_mode=WAL').fetchone()
                except sqlite3.OperationalError as retry_exc:
                    if 'disk i/o error' not in str(retry_exc).lower():
                        raise
                    exc = retry_exc
                    continue
                mode = str(row[0]).strip().lower() if row and row[0] is not None else ''
                if mode == 'wal':
                    _apply_macos_checkpoint_barrier(conn)
                    _enforce_macos_synchronous_full(conn)
                    return 'wal'
                break
        existing = _on_disk_journal_mode(conn)
        if existing == 'wal' or existing is None:
            raise
        if require_wal:
            raise WalUnsupportedError(str(exc)) from exc
        _log_wal_fallback_once(db_label, exc)
        _set_journal_mode_no_wait(conn, 'DELETE')
        return 'delete'

def _set_journal_mode_no_wait(conn: sqlite3.Connection, mode: str) -> str:
    """Execute ``PRAGMA journal_mode=<mode>`` without waiting on other openers.

    This is the ONLY place a journal-mode switch pragma may be issued for a
    non-WAL target.  It temporarily forces ``busy_timeout=0`` so SQLite's own
    exclusivity requirement becomes a concurrent-opener detector: leaving WAL
    mode requires exclusive access to the database, so if ANY other connection
    (this process or another) holds the DB open, the pragma fails immediately
    with ``database is locked`` instead of waiting out a busy timeout and
    sneaking the flip in between a concurrent writer's transactions — which is
    exactly how committed-but-uncheckpointed WAL transactions get destroyed.

    Callers must treat a raised ``OperationalError`` as "not exclusively
    owned: leave the journal mode alone", never as a retryable condition.

    Returns the resulting journal mode as reported by SQLite (lowercase), or
    ``""`` when SQLite returned no row.
    """
    previous_timeout = 0
    try:
        row = conn.execute('PRAGMA busy_timeout').fetchone()
        if row and row[0] is not None:
            previous_timeout = int(row[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        previous_timeout = 0
    conn.execute('PRAGMA busy_timeout=0')
    try:
        row = conn.execute(f'PRAGMA journal_mode={mode}').fetchone()
        return str(row[0]).strip().lower() if row and row[0] is not None else ''
    finally:
        try:
            conn.execute(f'PRAGMA busy_timeout={previous_timeout}')
        except sqlite3.OperationalError:
            pass

def _apply_delete_for_wal_reset_bug(conn: sqlite3.Connection, *, db_label: str, require_delete: bool=False) -> str:
    """Avoid enabling WAL when the linked SQLite has the WAL-reset bug.

    - Already-WAL on disk: leave WAL alone (no live downgrade) and warn.
    - Mode unreadable (probe blocked by a concurrent opener's locks):
      ownership is not provably exclusive — leave the journal mode alone
      and warn.  Never treat "could not read the mode" as "not WAL": that
      exact confusion let a vulnerable-SQLite process flip a live WAL
      state.db to DELETE under a concurrent WAL writer, destroying its
      committed-but-uncheckpointed transactions.
    - Otherwise: set DELETE (refusing to wait out concurrent openers) and
      warn.
    - For an explicit operator request, verify SQLite accepted DELETE.
    """
    current = _on_disk_journal_mode(conn)
    if current == 'wal':
        _log_wal_reset_bug_once(db_label, kept_wal=True)
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return 'wal'
    if current is None:
        if require_delete:
            raise sqlite3.OperationalError('could not verify journal mode before applying configured journal_mode=delete (database is locked — possible concurrent openers); refusing to downgrade a database this process does not exclusively own')
        _log_wal_reset_bug_once(db_label, kept_wal=True, indeterminate=True)
        return 'wal'
    actual = ''
    try:
        actual = _set_journal_mode_no_wait(conn, 'DELETE')
    except sqlite3.OperationalError as exc:
        if require_delete:
            raise
        lowered = str(exc).lower()
        if 'locked' in lowered or 'busy' in lowered:
            _log_wal_reset_bug_once(db_label, kept_wal=True, indeterminate=True)
            return current or 'delete'
    if require_delete and actual != 'delete':
        raise sqlite3.OperationalError(f"could not set configured journal_mode=delete (got {actual or 'no result'})")
    _log_wal_reset_bug_once(db_label, kept_wal=False)
    return 'delete'

def _wal_reset_repair_hint() -> str:
    """Return a context-appropriate hint for repairing the SQLite runtime.

    Uses the codebase's install-type detection so the hint matches what
    ``duck-agent update`` can actually do for this install (#75153).
    """
    try:
        from hermes_cli.config import detect_install_method, recommended_update_command_for_method, get_project_root
        method = detect_install_method(get_project_root())
        cmd = recommended_update_command_for_method(method)
        if method in {'git', 'unknown'}:
            return f'Duck Agent-managed installs can repair the embedded runtime with `{cmd}`'
        if method == 'docker':
            return f'update the container image with `{cmd}`'
        return cmd
    except Exception:
        pass
    return 'install a Python build bundled with SQLite 3.51.3+ (or backports 3.50.7 / 3.44.6) and restart Duck Agent'

def _log_wal_reset_bug_once(db_label: str, *, kept_wal: bool, indeterminate: bool=False) -> None:
    """Log once per (process, db_label) about the WAL-reset vulnerability path."""
    with _wal_reset_bug_warned_lock:
        if db_label in _wal_reset_bug_warned_paths:
            return
        _wal_reset_bug_warned_paths.add(db_label)
    if indeterminate:
        action = 'journal mode could not be verified or exclusively switched (database is locked — possible concurrent openers); leaving the journal mode untouched (no live downgrade under concurrent openers)'
    elif kept_wal:
        action = 'is already in WAL mode — leaving WAL in place (no live downgrade under concurrent openers)'
    else:
        action = 'using journal_mode=DELETE instead of enabling WAL'
    repair_hint = _wal_reset_repair_hint()
    logger.warning('%s: linked SQLite %s is vulnerable to the WAL-reset corruption bug (https://sqlite.org/wal.html#walresetbug) — %s. Upgrade to SQLite 3.51.3+ (or backports 3.50.7 / 3.44.6); %s. See `duck-agent doctor`. This warning fires once per process per database.', db_label, sqlite3.sqlite_version, action, repair_hint)

def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Log a single ERROR per (process, db_label) about WAL fallback.

    ERROR (not WARNING): a DB silently dropped to DELETE means a real loss of
    concurrency — under the kanban dispatcher + workers a write blocks readers,
    surfacing as SQLITE_BUSY/lock contention — so it must be loud, not cosmetic.

    Without this dedup, NFS users running kanban (which opens a fresh
    connection on every operation — see hermes_cli/kanban_db.py) would
    fill errors.log with hundreds of identical errors per hour.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    logger.error('%s: WAL journal_mode unsupported on this filesystem (%s) — falling back to journal_mode=DELETE (slower rollback-journal mode; reduces concurrency but works on NFS/SMB/FUSE/ZFS). See https://www.sqlite.org/wal.html for details. This message fires once per process per database.', db_label, exc)

def apply_database_pragmas(conn: sqlite3.Connection, *, db_label: str='state.db') -> None:
    """Apply optional performance and WAL-sizing PRAGMAs from ``config.yaml``.

    Reads the ``database:`` section and applies configurable PRAGMAs when set
    to integer values.  The journal mode itself is NOT handled here —
    ``database.journal_mode`` is owned by :func:`resolve_journal_mode` inside
    :func:`apply_wal_with_fallback`, which layers the operator setting under
    all the safety guards (never live-downgrading an on-disk WAL DB,
    filesystem fallback, WAL-reset-bug gating).

    Supported keys under ``database:`` in config.yaml:

    * ``cache_size`` — negative value = KiB, positive = pages
      (e.g. ``-262144`` = 256 MB page cache)
    * ``mmap_size`` — max bytes for memory-mapped I/O (0 = disabled)
    * ``temp_store`` — 0=DEFAULT(file), 1=FILE, 2=MEMORY, 3=ALWAYS
    * ``wal_autocheckpoint`` — WAL auto-checkpoint threshold in pages
    * ``journal_size_limit`` — max journal/WAL size in bytes

    Best-effort: config load or pragma failures are ignored so DB init
    never breaks on a malformed ``database:`` section.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        cfg = load_config_readonly()
    except Exception:
        return
    for pragma_name in ('cache_size', 'mmap_size', 'temp_store', 'wal_autocheckpoint', 'journal_size_limit'):
        raw_value = cfg_get(cfg, 'database', pragma_name, default=None)
        if raw_value is None:
            continue
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            logger.warning('%s: ignoring non-integer database.%s=%r', db_label, pragma_name, raw_value)
            continue
        try:
            conn.execute(f'PRAGMA {pragma_name}={value}')
        except sqlite3.OperationalError:
            pass
_MALFORMED_SCHEMA_MARKERS = ('malformed database schema', 'database disk image is malformed')
_repair_attempted_paths: set[str] = set()
_repair_attempt_lock = threading.Lock()

def is_malformed_db_error(exc: BaseException) -> bool:
    """True if *exc* is a SQLite 'malformed schema / disk image' error.

    These are the corruption classes where the schema fails to parse, so
    targeted ``sqlite_master`` surgery (not an ordinary FTS rebuild) is the
    only recovery path.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any((marker in str(exc).lower() for marker in _MALFORMED_SCHEMA_MARKERS))
_DISK_FULL_MARKERS = ('no space left on device', 'not enough space', 'database or disk is full', 'disk full', 'full disk', 'enospc')

def is_disk_full_error(exc: BaseException | str | None) -> bool:
    """True when *exc* (or a stringified error) is a disk-full / ENOSPC failure.

    Covers:
      * ``OSError`` with ``errno.ENOSPC``
      * SQLite ``OperationalError: database or disk is full`` (SQLITE_FULL)
      * Plain English / errno strings that survive RPC wrapping
    """
    if exc is None:
        return False
    if isinstance(exc, OSError) and getattr(exc, 'errno', None) == errno.ENOSPC:
        return True
    text = exc if isinstance(exc, str) else str(exc)
    lowered = text.lower()
    return any((marker in lowered for marker in _DISK_FULL_MARKERS))

def _claim_repair_attempt(db_path: Path) -> bool:
    """Claim the one-shot repair attempt for *db_path* in this process.

    Returns True for the first caller, False afterwards. Keeps a malformed
    DB from triggering an unbounded repair/reopen loop and stops concurrent
    callers from racing surgery on the same file.
    """
    key = str(db_path)
    with _repair_attempt_lock:
        if key in _repair_attempted_paths:
            return False
        _repair_attempted_paths.add(key)
        return True

def _backup_db_file(db_path: Path) -> Optional[Path]:
    """Copy a (possibly malformed) DB file to a timestamped backup beside it.

    Raw file copy on purpose: the DB won't open cleanly, so we preserve the
    bytes exactly for forensics / manual restore. WAL and SHM sidecars are
    copied too when present. Returns the backup path, or None on failure.

    Refuses when a connection to this database is still live in the process:
    reading the file would ``close()`` a descriptor for it and cancel that
    connection's POSIX advisory locks (see ``hermes_cli.sqlite_safe_read``).
    The repair path can be entered by one SessionDB while the gateway holds
    others, so this is a real possibility rather than a theoretical one.
    """
    import datetime
    import shutil
    try:
        from hermes_cli.sqlite_safe_read import has_live_connection
    except ImportError:
        has_live_connection = None
    if has_live_connection is not None and has_live_connection(db_path):
        logger.error("Refusing to raw-copy %s for backup: a connection to it is still open in this process and the copy would cancel that connection's POSIX locks. Close all SessionDB handles first.", db_path)
        return None
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = db_path.with_name(f'{db_path.name}.malformed-backup-{stamp}')
    try:
        shutil.copy2(db_path, backup_path)
        for suffix in ('-wal', '-shm'):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup_path.with_name(backup_path.name + suffix))
        return backup_path
    except Exception as exc:
        logger.warning('Could not back up malformed DB %s: %s', db_path, exc)
        return None

def preflight_db_writability(db_path: Path, *, db_label: str='state.db') -> None:
    """Refuse-or-repair read-only DB files BEFORE the first connection opens.

    Port of Kilo-Org/kilocode#12508's startup preflight. A stray read-only
    ``state.db`` / ``-wal`` / ``-shm`` (sudo run, restored backup, copied
    dotfiles) previously surfaced as an opaque
    ``sqlite3.OperationalError: attempt to write a readonly database`` raised
    from deep inside ``_init_schema`` — naming no file and no fix — and the
    obvious wrong "fix" (deleting the ``-wal``) silently loses committed
    transactions. This preflight:

    - **Repairs** permissions with ``chmod u+rw`` when the file lives inside
      the Duck Agent home tree (``get_hermes_home()``) — the safe repair scope:
      Duck Agent owns those files, and the OS makes ``chmod`` fail on files the
      user doesn't own, which bounds the repair exactly.
    - **Fails fast with an actionable error** naming the exact file and the
      exact ``chmod`` command for anything else (root-owned files, read-only
      mounts, custom paths outside the home tree).
    - Never deletes or truncates a WAL sidecar — once writable, the normal
      open path checkpoints its committed frames into the DB as intended.

    ``:memory:`` and ``file:`` URI paths are skipped (no plain on-disk files
    to check). Shared by :class:`SessionDB` and ``hermes_cli.kanban_db``.
    """
    raw = str(db_path)
    if raw == ':memory:' or raw.startswith('file:'):
        return
    try:
        home: Optional[Path] = Path(get_hermes_home()).resolve()
    except Exception:
        home = None

    def _in_repair_scope(p: Path) -> bool:
        if home is None:
            return False
        try:
            return p.resolve().is_relative_to(home)
        except (OSError, ValueError):
            return False

    def _ensure_writable(p: Path, *, is_dir: bool=False) -> None:
        import stat as _stat
        if os.access(p, os.R_OK | os.W_OK):
            return
        if _in_repair_scope(p):
            try:
                add = _stat.S_IRUSR | _stat.S_IWUSR | (_stat.S_IXUSR if is_dir else 0)
                os.chmod(p, p.stat().st_mode | add)
            except OSError:
                pass
            if os.access(p, os.R_OK | os.W_OK):
                logger.info('%s preflight: repaired read-only %s (chmod u+rw%s)', db_label, p, 'x' if is_dir else '')
                return
        kind = 'directory' if is_dir else 'file'
        wal_note = ' Do NOT delete the -wal file — it contains committed data that will be merged into the database once it is writable.' if p.name.endswith('-wal') else ''
        raise sqlite3.OperationalError(f"{db_label} is not writable: {kind} {p} is read-only for this user. Duck Agent needs read-write access to open the database. Fix with: chmod u+rw{('x' if is_dir else '')} '{p}' (files owned by another user may need sudo/chown).{wal_note}")
    parent = db_path.parent
    if parent.is_dir():
        _ensure_writable(parent, is_dir=True)
    for suffix in ('', '-wal', '-shm'):
        p = db_path.with_name(db_path.name + suffix) if suffix else db_path
        if p.is_file():
            _ensure_writable(p)

def _db_opens_cleanly(db_path: Path) -> Optional[str]:
    """Probe a DB on a fresh connection. Returns None if healthy, else a reason.

    Runs the same first-statement (``PRAGMA journal_mode``) that trips the
    malformed-schema parse, then ``PRAGMA integrity_check`` and a canonical
    ``sessions`` read, and finally a rolled-back ``messages`` write so that
    FTS5 index corruption — which leaves base-table reads and
    ``integrity_check`` passing while every ``INSERT INTO messages`` fails
    through the FTS triggers — is reported as unhealthy rather than slipping
    past as a false "ok" (#50502).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        load_fts5_cjk_extension(conn)
        conn.execute('PRAGMA journal_mode').fetchone()
        rows = conn.execute('PRAGMA integrity_check').fetchall()
        problems = [str(r[0]) for r in rows if r and str(r[0]).lower() != 'ok']
        if problems:
            return '; '.join(problems[:3])
        conn.execute('SELECT COUNT(*) FROM sessions').fetchone()
        for fts_table in ('messages_fts', 'messages_fts_trigram', 'messages_fts_cjk'):
            try:
                conn.execute(f"""SELECT 1 FROM {fts_table} WHERE {fts_table} MATCH '""' LIMIT 1""").fetchone()
            except sqlite3.OperationalError as exc:
                if SessionDB._is_fts5_unavailable_error(exc):
                    continue
                msg = str(exc).lower()
                if 'no such table' in msg or 'no such column' in msg:
                    continue
                return f'fts5 read probe failed on {fts_table}: {exc}'
            except sqlite3.DatabaseError as exc:
                return f'fts5 read probe failed on {fts_table}: {exc}'
        probe_session_id = f'_hermes_fts_health_probe_{time.time_ns()}'
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)', (probe_session_id, '_health_probe', time.time()))
            conn.execute('INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)', (probe_session_id, 'user', '_fts_health_probe', time.time()))
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError as exc:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            if 'no such table' in msg or 'no such column' in msg:
                return None
            if 'no such tokenizer: cjk_unicode61' in msg:
                return None
            return str(exc)
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()

def repair_state_db_schema(db_path: Path, *, backup: bool=True) -> Dict[str, Any]:
    """Repair a state.db whose ``sqlite_master`` schema is malformed or whose
    FTS indexes reject writes.

    Handles two corruption classes: the "duplicate object definition" /
    malformed-schema class where even ``PRAGMA`` statements fail, and the FTS
    write-corruption class (#50502) where base tables read fine and
    ``integrity_check`` passes but writes fail through the ``messages_fts*``
    triggers. Tries least-destructive recovery first and escalates:

      1. **Rebuild FTS indexes in place** via the FTS5 ``'rebuild'`` command,
         which rewrites the internal b-tree segments from the canonical
         ``messages`` rows without dropping or recreating anything. Fixes the
         FTS write-corruption class while preserving the schema intact.
      2. **De-duplicate** ``sqlite_master`` (keep the lowest rowid per
         ``type``/``name``). Fixes the canonical "table X already exists"
         case and PRESERVES the existing FTS index intact.
      3. **Drop the FTS schema** (every ``messages_fts*`` object) + ``VACUUM``.
         The next ``SessionDB()`` open rebuilds the FTS indexes from the
         canonical ``messages`` table.

    Canonical ``sessions`` / ``messages`` rows are never modified. A
    timestamped raw backup is taken first unless ``backup=False``.

    Returns a report dict: ``{repaired: bool, strategy: str|None,
    backup_path: str|None, error: str|None}``.
    """
    report: Dict[str, Any] = {'repaired': False, 'strategy': None, 'backup_path': None, 'error': None}
    db_path = Path(db_path)
    if not db_path.exists():
        report['error'] = f'{db_path} does not exist'
        return report
    if _db_opens_cleanly(db_path) is None:
        report['repaired'] = True
        report['strategy'] = 'already_healthy'
        return report
    if backup:
        bpath = _backup_db_file(db_path)
        report['backup_path'] = str(bpath) if bpath else None
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            load_fts5_cjk_extension(conn)
            for table_name in ('messages_fts', 'messages_fts_trigram', 'messages_fts_cjk'):
                try:
                    conn.execute(f"INSERT INTO {table_name}({table_name}) VALUES('rebuild')")
                except sqlite3.OperationalError:
                    continue
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report['repaired'] = True
            report['strategy'] = 'rebuild_fts'
            logger.warning('state.db FTS indexes rebuilt in place (schema preserved): %s', db_path)
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning('state.db FTS in-place rebuild pass failed: %s', exc)
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute('REINDEX')
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report['repaired'] = True
            report['strategy'] = 'reindex_btree'
            logger.warning('state.db B-tree indexes rebuilt via REINDEX: %s', db_path)
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning('state.db REINDEX pass failed: %s', exc)
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute('PRAGMA writable_schema=ON')
            dupes = conn.execute('SELECT type, name, COUNT(*) AS c, MIN(rowid) AS keep FROM sqlite_master GROUP BY type, name HAVING c > 1').fetchall()
            for type_, name, _count, keep in dupes:
                conn.execute('DELETE FROM sqlite_master WHERE type IS ? AND name IS ? AND rowid <> ?', (type_, name, keep))
            conn.execute('PRAGMA writable_schema=OFF')
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report['repaired'] = True
            report['strategy'] = 'dedup_schema'
            logger.warning('state.db schema repaired by de-duplicating sqlite_master (FTS index preserved): %s', db_path)
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning('state.db dedup repair pass failed: %s', exc)
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute('PRAGMA writable_schema=ON')
            conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'")
            conn.execute('PRAGMA writable_schema=OFF')
            conn.commit()
            conn.execute('VACUUM')
        finally:
            conn.close()
        reason = _db_opens_cleanly(db_path)
        if reason is None:
            report['repaired'] = True
            report['strategy'] = 'drop_fts_rebuild'
            logger.warning('state.db schema repaired by dropping FTS schema; indexes will rebuild from messages on next open: %s', db_path)
            return report
        report['error'] = reason
    except sqlite3.DatabaseError as exc:
        report['error'] = str(exc)
    if not report['repaired']:
        logger.error('state.db schema repair could not recover %s automatically (backup: %s); manual restore from backup may be required.', db_path, report['backup_path'])
    return report
FTS_CJK_TABLE_SQL = "\nCREATE VIEW IF NOT EXISTS messages_fts_cjk_src AS\n    SELECT id, role, content, tool_name, tool_calls\n    FROM messages\n    WHERE role <> 'tool';\n\nCREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(\n    content,\n    tool_name,\n    tool_calls,\n    content='messages_fts_cjk_src',\n    content_rowid='id',\n    tokenize='cjk_unicode61'\n);\n"
FTS_CJK_TRIGGER_SQL = "\nCREATE TRIGGER IF NOT EXISTS messages_fts_cjk_insert AFTER INSERT ON messages\nWHEN new.role <> 'tool'\n   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)\n     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                            WHERE key = 'fts_cjk_rebuild_progress'), -1))\nBEGIN\n    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)\n    VALUES (new.id, new.content, new.tool_name, new.tool_calls);\nEND;\n\nCREATE TRIGGER IF NOT EXISTS messages_fts_cjk_delete AFTER DELETE ON messages\nWHEN old.role <> 'tool'\n   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)\n     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                            WHERE key = 'fts_cjk_rebuild_progress'), -1))\nBEGIN\n    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)\n    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);\nEND;\n\nCREATE TRIGGER IF NOT EXISTS messages_fts_cjk_update\nAFTER UPDATE OF content, tool_name, tool_calls, role ON messages\nWHEN (old.content IS NOT new.content\n    OR old.tool_name IS NOT new.tool_name\n    OR old.tool_calls IS NOT new.tool_calls\n    OR old.role IS NOT new.role)\n   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)\n     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta\n                            WHERE key = 'fts_cjk_rebuild_progress'), -1))\nBEGIN\n    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)\n    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls\n    WHERE old.role <> 'tool';\n    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)\n    SELECT new.id, new.content, new.tool_name, new.tool_calls\n    WHERE new.role <> 'tool';\nEND;\n"

def fts5_cjk_so_path() -> Path:
    """Location of the cjk_unicode61 loadable extension."""
    env = os.getenv('HERMES_FTS5_CJK_SO')
    if env:
        return Path(env).expanduser()
    return get_hermes_home() / 'lib' / 'libfts5_cjk.so'

def _cjk_fts_config_enabled() -> bool:
    """config.yaml ``sessions.cjk_fts`` (default on), via its env bridge."""
    return os.getenv('HERMES_CJK_FTS', '1').strip().lower() not in ('0', 'false', 'off', 'no')

def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the cjk_unicode61 tokenizer into ``conn``.

    Returns False (never raises) when the .so is absent, the feature is
    disabled via ``sessions.cjk_fts``, or this Python build has extension
    loading compiled out — every caller treats False as "behave exactly as
    before the cjk index existed".
    """
    if not _cjk_fts_config_enabled():
        return False
    path = fts5_cjk_so_path()
    if not path.exists():
        return False
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(path))
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception:
        logger.warning('fts5_cjk extension load failed (%s)', path, exc_info=True)
        return False

class CompressionSessionClosedError(RuntimeError):
    """A durable write targeted a parent already closed by compression."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f'Session {session_id!r} is closed by compression; adopt its live continuation before appending messages')

class CompressionSessionBusyError(RuntimeError):
    """A non-owner tried to write while compression owns the session."""

class SessionCompressionInProgressError(CompressionSessionBusyError):
    """A concurrent writer collided with a *live* compression lock.

    Split out from :class:`CompressionSessionBusyError` because the two
    conditions that class covers need opposite handling. This one is
    transient: a healthy compressor holds the session for a few seconds and
    the lock row carries its own ``expires_at``, so the write can simply wait
    (see ``_execute_write``'s patience loop). The other case, a compressor
    discovering its own lease is gone, is permanent and must fail fast rather
    than spin out the whole patience budget.

    Subclassing keeps every existing ``except CompressionSessionBusyError``
    handler working unchanged.
    """

def _connect_tracked_db(path, tracking_path=None, **kwargs):
    """``sqlite3.connect`` that registers the open fd for lock-safety.

    While a connection is live, byte-level probes of the same file are
    refused: an ``open()``/``close()`` cancels every POSIX advisory lock this
    process holds on it -- including a running VACUUM's EXCLUSIVE lock.
    Released automatically on ``close()``.

    The ONLY tolerated fallback is the helper being absent entirely
    (scaffold/embed installs that ship hermes_state without hermes_cli). A
    real connection failure must propagate: silently retrying an *untracked*
    connect would disable the guard for the lifetime of that connection,
    which is precisely the failure mode this module exists to prevent.
    """
    try:
        from hermes_cli.sqlite_safe_read import connect_tracked
    except ImportError:
        logger.debug('hermes_cli.sqlite_safe_read unavailable; opening %s untracked (byte-probe guard inactive in this install)', path)
        return sqlite3.connect(str(path), **kwargs)
    return connect_tracked(path, tracking_path=tracking_path, connect_fn=sqlite3.connect, **kwargs)

def is_zeroed_state_db(path: Path, *, probe_bytes: int=100, force: bool=False) -> bool:
    """Detect the #68474 zeroed state.db signature (size>0, NUL header).

    Byte-level probe, so it is only safe BEFORE any connection to *path*
    exists in this process: ``close()`` cancels every POSIX advisory lock the
    process holds on the file, which can pull the EXCLUSIVE lock out from
    under a running VACUUM and corrupt the database. The read is routed
    through ``read_header_bytes_preopen``, which refuses (returning False
    here) once a connection is live. Pass ``force=True`` only for offline
    files -- quarantined copies, snapshots, archives.

    Prefer ``hermes_cli.backup.is_zeroed_sqlite_file`` when available; this
    local copy keeps SessionDB openable without importing the CLI package
    in constrained embed paths.
    """
    try:
        from hermes_cli.backup import is_zeroed_sqlite_file
        return is_zeroed_sqlite_file(path, probe_bytes=probe_bytes, force=force)
    except Exception:
        pass
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    from hermes_cli.sqlite_safe_read import read_header_bytes_preopen
    head = read_header_bytes_preopen(path, length=max(16, probe_bytes), force=force)
    if not head or head.startswith(b'SQLite format 3'):
        return False
    return all((byte == 0 for byte in head))

def quarantine_zeroed_state_db(path: Path) -> Optional[Path]:
    """Move a zeroed state.db aside (preserve bytes) and return quarantine path.

    Uses a cross-process lock (``#68805``) so two concurrent startups cannot
    race: the first process moves the zeroed file and the second re-checks
    under the lock, finding the file already gone (or a fresh DB in its place)
    instead of clobbering the quarantine.
    """
    import platform
    lock_path = path.with_name(path.name + '.quarantine.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+b')
    acquired = False
    try:
        deadline = time.monotonic() + 5.0
        if platform.system() == 'Windows':
            import msvcrt
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        if not acquired:
            logger.error('quarantine lock for %s not acquired within 5s — refusing to quarantine without the cross-process lock. The zeroed file is left in place. If sessions fail to load, restore from state-snapshots via `duck-agent snapshot list` / `duck-agent snapshot restore <id>`.', path)
            return None
        if not path.exists():
            logger.info('quarantine_zeroed_state_db: %s already moved by another process', path)
            return None
        if not is_zeroed_state_db(path):
            logger.info('quarantine_zeroed_state_db: %s is no longer zeroed (another process quarantined it and a fresh DB was created)', path)
            return None
        try:
            ts = time.strftime('%Y%m%d-%H%M%S')
        except Exception:
            ts = 'unknown'
        dest = path.with_name(f'{path.name}.zeroed-{ts}-{os.getpid()}.bak')
        n = 0
        while dest.exists():
            n += 1
            dest = path.with_name(f'{path.name}.zeroed-{ts}-{os.getpid()}-{n}.bak')
        try:
            path.rename(dest)
        except OSError as exc:
            logger.error('Failed to quarantine zeroed %s: %s', path, exc)
            return None
        for suffix in ('-wal', '-shm'):
            side = Path(str(path) + suffix)
            if side.exists():
                try:
                    side.rename(Path(str(dest) + suffix))
                except OSError:
                    pass
        return dest
    finally:
        try:
            if acquired:
                if platform.system() == 'Windows':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, AttributeError):
            pass
        finally:
            handle.close()

class SessionDB(SessionSearchMixin, SessionSchemaMixin, SessionPortabilityMixin):
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """
    _WRITE_PATIENCE_S = 20.0
    _TRANSCRIPT_WRITE_PATIENCE_S = 60.0
    _ACTIVITY_WRITE_PATIENCE_S = 0.5
    _COMPRESSION_BUSY_WAIT_S = 5.0
    _WRITE_RETRY_MIN_S = 0.02
    _WRITE_RETRY_MAX_S = 0.15
    _WRITE_RETRY_SLOW_AFTER_S = 2.0
    _WRITE_RETRY_SLOW_MIN_S = 0.25
    _WRITE_RETRY_SLOW_MAX_S = 1.0
    _CHECKPOINT_EVERY_N_WRITES = 50
    _FTS_MERGE_EVERY_N_WRITES = 1000
    _FTS_MERGE_MAX_PAGES_PER_INDEX = 500
    _FTS_MERGE_COMMANDS_PER_PASS = 4
    _IMPORT_MAX_SESSIONS = 500
    _IMPORT_MAX_MESSAGES_PER_SESSION = 10000
    _IMPORT_MAX_TOTAL_MESSAGES = 50000
    _IMPORT_MAX_SESSION_BYTES = 5 * 1024 * 1024
    _IMPORT_MAX_TOTAL_BYTES = 25 * 1024 * 1024

    @staticmethod
    def _store_system_prompt(conn, system_prompt: Optional[str]) -> Optional[str]:
        if system_prompt is None:
            return None
        prompt_hash = _system_prompt_hash(system_prompt)
        conn.execute('INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)', (prompt_hash, system_prompt))
        return prompt_hash

    @staticmethod
    def _delete_unreferenced_system_prompts(conn) -> None:
        conn.execute('DELETE FROM system_prompts WHERE NOT EXISTS (SELECT 1 FROM sessions WHERE sessions.system_prompt_hash = system_prompts.hash)')

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        if '_system_prompt_resolved' in data:
            resolved = data.pop('_system_prompt_resolved')
            if 'system_prompt' in data:
                data['system_prompt'] = resolved
        return data

    def __init__(self, db_path: Path=None, read_only: bool=False):
        self.db_path = db_path or _default_db_path()
        _ensure_test_isolation(self.db_path)
        self.read_only = read_only
        self._lock = threading.Lock()
        self._read_local = threading.local()
        self._read_conns: 'set[sqlite3.Connection]' = set()
        self._read_conns_lock = threading.Lock()
        self._read_conns_closed = False
        self._wal_active = False
        self._write_count = 0
        self._fts_runtime_rebuild_attempted = False
        self._fts_usermerge_floor_applied = False
        self._fts_enabled = False
        self._trigram_available = False
        self._fts_cjk_loaded = False
        self._fts_cjk_available = False
        self._fts_unavailable_warned = False
        self._conn = None
        self._token_queue: deque = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread: Optional[threading.Thread] = None
        self._token_writer_stop = False
        self._token_writer_busy = False
        try:
            if read_only:
                self._conn = _connect_tracked_db(f'file:{self.db_path}?mode=ro', tracking_path=self.db_path, uri=True, check_same_thread=False, timeout=1.0, isolation_level=None)
                self._conn.row_factory = sqlite3.Row
                try:
                    apply_database_pragmas(self._conn, db_label='state.db')
                    cursor = self._conn.cursor()
                    self._fts_enabled = self._fts_table_probe(cursor, 'messages_fts') is True
                    if self._fts_enabled:
                        self._trigram_available = self._fts_table_probe(cursor, 'messages_fts_trigram') is True
                except BaseException:
                    conn, self._conn = (self._conn, None)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if not read_only:
                preflight_db_writability(self.db_path, db_label='state.db')
            if not read_only and self.db_path.exists() and is_zeroed_state_db(self.db_path):
                try:
                    zsize = self.db_path.stat().st_size
                except OSError:
                    zsize = -1
                qpath = quarantine_zeroed_state_db(self.db_path)
                snaps = self.db_path.parent / 'state-snapshots'
                msg = f"state.db looks ZEROED ({zsize} bytes, no SQLite header). Preserved at {qpath or '(quarantine failed — file left in place)'}. Restore from {snaps} via `duck-agent snapshot list` / `duck-agent snapshot restore <id>` if available. Opening a fresh empty database so the agent can start."
                logger.error(msg)
                _set_last_init_error(msg)
                if qpath is None and self.db_path.exists() and is_zeroed_state_db(self.db_path):
                    raise sqlite3.DatabaseError(msg)

            def _connect_and_init():
                self._conn = _connect_tracked_db(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
                self._conn.row_factory = sqlite3.Row
                self._wal_active = apply_wal_with_fallback(self._conn, db_label='state.db') == 'wal'
                apply_database_pragmas(self._conn, db_label='state.db')
                self._conn.execute('PRAGMA foreign_keys=ON')
                self._fts_cjk_loaded = load_fts5_cjk_extension(self._conn)
                self._init_schema()

            def _connect_and_init_with_lock_patience():
                deadline = time.monotonic() + self._WRITE_PATIENCE_S
                while True:
                    try:
                        _connect_and_init()
                        return
                    except sqlite3.OperationalError as exc:
                        err = str(exc).lower()
                        if 'locked' not in err and 'busy' not in err:
                            raise
                        try:
                            if self._conn is not None:
                                self._conn.close()
                        except Exception:
                            pass
                        now = time.monotonic()
                        if now >= deadline:
                            raise
                        time.sleep(min(random.uniform(self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S), max(deadline - now, 0.001)))
            try:
                _connect_and_init_with_lock_patience()
            except sqlite3.DatabaseError as exc:
                if not is_malformed_db_error(exc) or not _claim_repair_attempt(self.db_path):
                    raise
                logger.error('state.db schema is malformed (%s) — attempting automatic repair (a backup copy is made first).', exc)
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                report = repair_state_db_schema(self.db_path)
                if not report.get('repaired'):
                    raise
                _connect_and_init_with_lock_patience()
        except Exception as exc:
            _set_last_init_error(f'{type(exc).__name__}: {exc}')
            raise

    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """Per-thread read-only connection, or None when unavailable.

        Only used under WAL: WAL readers see a consistent snapshot and never
        block on (or get blocked by) the writer, so recall/browse queries can
        skip self._lock entirely. Under DELETE journal mode (NFS fallback) a
        reader can hit SQLITE_BUSY storms during writes, so we keep the
        legacy locked single-connection path there.

        Fresh read transactions begin per statement (autocommit), so each
        query observes everything committed so far — read-your-writes holds
        for the flush-then-search patterns in a turn.
        """
        if not self._wal_active or self.read_only:
            return None
        conn = getattr(self._read_local, 'conn', None)
        if conn is not None:
            return conn
        if getattr(self._read_local, 'failed', False):
            return None
        try:
            conn = _connect_tracked_db(f'file:{self.db_path}?mode=ro', tracking_path=self.db_path, uri=True, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            apply_database_pragmas(conn, db_label='state.db')
            if self._fts_cjk_loaded:
                load_fts5_cjk_extension(conn)
            with self._read_conns_lock:
                if self._read_conns_closed:
                    conn.close()
                    self._read_local.failed = True
                    return None
                self._read_conns.add(conn)
        except sqlite3.Error:
            self._read_local.failed = True
            logger.debug('read-only connection open failed for %s', self.db_path, exc_info=True)
            return None
        self._read_local.conn = conn
        return conn

    @contextmanager
    def _read_ctx(self):
        """Yield a connection for read-only statements.

        WAL: a per-thread read-only connection with NO lock — recall queries
        never convoy behind writer flushes (the gateway shares one SessionDB
        across every agent, so this lock was a global choke point).
        Non-WAL or read-conn failure: the shared writer connection under
        self._lock, byte-for-byte the legacy behavior.
        """
        conn = self._get_read_conn()
        if conn is not None:
            yield conn
            return
        with self._lock:
            yield self._conn

    @staticmethod
    def _is_fts5_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        err = str(exc).lower()
        if 'no such module' in err and 'fts5' in err:
            return True
        if 'no such tokenizer: trigram' in err:
            return True
        if 'no such tokenizer: cjk_unicode61' in err:
            return True
        return False

    @staticmethod
    def _is_trigram_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        """True when only an optional tokenizer is missing (FTS5 itself works).

        Covers the built-in trigram tokenizer (needs SQLite >= 3.34) and the
        loadable cjk_unicode61 tokenizer — both mean "this one index can't be
        served here", never "disable FTS".
        """
        err = str(exc).lower()
        return 'no such tokenizer: trigram' in err or 'no such tokenizer: cjk_unicode61' in err

    @staticmethod
    def _db_has_legacy_inline_fts(cursor: sqlite3.Cursor) -> bool:
        """True when messages_fts exists in ANY pre-v23 shape.

        v23's messages_fts is external-content over THREE real columns
        (content, tool_name, tool_calls). Every pre-v23 shape lacks the
        tool_name/tool_calls columns — whether the old inline single-column
        form (v11..v22) or the even older external-content single-column form
        (v10-era, pre-#16751). We therefore detect "needs optimize" as "the
        stored CREATE lacks the tool_name column", which is the precise v23
        marker and correctly catches BOTH legacy variants.

        Returns False when messages_fts doesn't exist yet (fresh DB mid-init):
        the post-migration FTS setup block will create it in the v23 shape.
        """
        row = cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'").fetchone()
        if row is None:
            return False
        sql = (row[0] if not isinstance(row, sqlite3.Row) else row['sql']) or ''
        return 'tool_name' not in sql

    def _warn_trigram_unavailable(self, exc: sqlite3.OperationalError) -> None:
        """Log once that the trigram tokenizer is missing; base FTS5 stays enabled."""
        if getattr(self, '_trigram_unavailable_warned', False):
            return
        self._trigram_unavailable_warned = True
        logger.info('SQLite trigram tokenizer unavailable for %s (requires SQLite >= 3.34, this build is %s); CJK/substring search will fall back to LIKE: %s', self.db_path, sqlite3.sqlite_version, exc)

    def _warn_fts5_unavailable(self, exc: sqlite3.OperationalError) -> None:
        self._fts_enabled = False
        if self._fts_unavailable_warned:
            return
        self._fts_unavailable_warned = True
        logger.warning('SQLite FTS5 unavailable for %s; full-text session search disabled. Run `duck-agent update` to rebuild the venv with a current Python (managed uv guarantees FTS5). (underlying error: %s)', self.db_path, exc)

    def _ensure_fts_cjk_schema(self, cursor) -> None:
        """Create / repair / self-heal the CJK-bigram index surface.

        ``cursor`` may be a Cursor or a Connection (both expose execute /
        executescript). Called only for v23-shape DBs with the base FTS
        surface healthy. Sets ``self._fts_cjk_available``. Never raises;
        every failure mode degrades to "no cjk index" (trigram/LIKE routing
        keeps working).

        Cases:
          tokenizer loaded, table absent  → create. Empty DB: index is
              complete by construction (triggers cover everything). Populated
              DB: set the cjk backfill markers so the id-gated triggers stay
              correct and `optimize-storage` can backfill; the index is NOT
              served until the backfill completes.
          tokenizer loaded, table present → ensure triggers (recreates any
              dropped by a tokenizer-less process), honour the stale
              breadcrumb (serve only when absent and no backfill pending).
          tokenizer NOT loaded, table present with live triggers → drop the
              cjk triggers so message INSERTs don't fail at trigger time,
              and leave the stale breadcrumb (#self-heal). The table itself
              stays for a later capable open to rebuild.
        """
        cjk_present = bool(cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_cjk'").fetchone())
        if not self._fts_cjk_loaded:
            if cjk_present:
                live = [r[0] for r in cursor.execute(f"SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN ({','.join(('?' for _ in _FTS_CJK_TRIGGERS))})", _FTS_CJK_TRIGGERS).fetchall()]
                if live:
                    logger.warning('messages_fts_cjk triggers present but the cjk_unicode61 tokenizer is unavailable (%s) — dropping the cjk triggers so message writes keep working. CJK search falls back to trigram/LIKE; run `duck-agent sessions optimize-storage` on a host with the extension to rebuild.', fts5_cjk_so_path())
                    cursor.execute("INSERT INTO state_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = '1'", (FTS_CJK_STALE_KEY,))
                    for trig in live:
                        cursor.execute(f'DROP TRIGGER IF EXISTS {trig}')
            self._fts_cjk_available = False
            return
        try:
            cursor.executescript(FTS_CJK_TABLE_SQL)
            if not cjk_present:
                cursor.execute('DELETE FROM state_meta WHERE key = ?', (FTS_CJK_STALE_KEY,))
                n_msgs = cursor.execute("SELECT COUNT(*) FROM messages WHERE role <> 'tool'").fetchone()[0]
                if n_msgs > 0:
                    hw = cursor.execute('SELECT COALESCE(MAX(id), 0) FROM messages').fetchone()[0]
                    for k, v in (('fts_cjk_rebuild_high_water', str(hw)), ('fts_cjk_rebuild_progress', '0')):
                        cursor.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (k, v))
            stale = cursor.execute('SELECT 1 FROM state_meta WHERE key = ?', (FTS_CJK_STALE_KEY,)).fetchone()
            if stale:
                self._fts_cjk_available = False
                return
            cursor.executescript(FTS_CJK_TRIGGER_SQL)
            backfill_pending = cursor.execute("SELECT 1 FROM state_meta WHERE key = 'fts_cjk_rebuild_high_water' LIMIT 1").fetchone()
            self._fts_cjk_available = not backfill_pending
        except sqlite3.OperationalError:
            logger.warning('messages_fts_cjk ensure failed; CJK search stays on trigram/LIKE', exc_info=True)
            self._fts_cjk_available = False

    @staticmethod
    def _drop_fts_triggers(cursor: sqlite3.Cursor) -> None:
        for trigger in _FTS_TRIGGERS:
            try:
                cursor.execute(f'DROP TRIGGER IF EXISTS {trigger}')
            except sqlite3.OperationalError:
                pass

    def _ensure_fts_schema(self, cursor: sqlite3.Cursor, table_name: str, ddl: str) -> bool:
        status = self._fts_table_probe(cursor, table_name)
        if status is None:
            return False
        try:
            cursor.executescript(ddl)
            return True
        except sqlite3.OperationalError as exc:
            if not self._is_fts5_unavailable_error(exc):
                raise
            if self._is_trigram_unavailable_error(exc):
                self._warn_trigram_unavailable(exc)
            else:
                self._warn_fts5_unavailable(exc)
            return False

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float]=None) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random jitter, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        *patience_s* is the total time budget for lock retries (default
        ``_WRITE_PATIENCE_S``).  Transcript-critical writes pass
        ``_TRANSCRIPT_WRITE_PATIENCE_S`` so a sibling process holding the
        lock for a legitimate long operation (VACUUM, TRUNCATE checkpoint,
        pre-bounded-merge FTS optimize from an older still-running
        install) exhausts routine writers' patience without destroying a
        user turn.  Jitter starts small (20-150ms) for fast reclaim on
        millisecond contention and backs off to 250ms-1s once the lock has
        been held longer than ``_WRITE_RETRY_SLOW_AFTER_S``.

        Returns whatever *fn* returns.
        """
        if patience_s is None:
            patience_s = self._WRITE_PATIENCE_S
        deadline = time.monotonic() + patience_s
        compression_deadline: Optional[float] = None

        def _is_no_more_rows(exc: sqlite3.Error) -> bool:
            return 'no more rows available' in str(exc).lower()
        while True:
            try:
                with self._lock:
                    self._conn.execute('BEGIN IMMEDIATE')
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                if self._write_count % self._FTS_MERGE_EVERY_N_WRITES == 0:
                    self._try_incremental_merge_fts()
                return result
            except SessionCompressionInProgressError:
                if compression_deadline is None:
                    compression_deadline = min(time.monotonic() + self._COMPRESSION_BUSY_WAIT_S, deadline)
                if self._sleep_before_write_retry(compression_deadline, self._COMPRESSION_BUSY_WAIT_S):
                    continue
                raise
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if 'locked' in err_msg or 'busy' in err_msg:
                    if self._sleep_before_write_retry(deadline, patience_s):
                        continue
                    raise sqlite3.OperationalError(f'database is locked (another Duck Agent process held the state.db write lock for over {patience_s:.0f}s — likely a long maintenance operation such as VACUUM, a large WAL checkpoint, or an older pre-update process; the database itself is healthy)') from exc
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                raise
            except sqlite3.DatabaseError as exc:
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                if not self._try_runtime_fts_rebuild(exc):
                    raise
                continue
            except sqlite3.Error as exc:
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                raise

    def _sleep_before_write_retry(self, deadline: float, patience_s: float) -> bool:
        """Sleep one jitter interval if the patience budget still allows it.

        Returns True when the caller should retry, False when *deadline* has
        passed and the error should propagate. Jitter stays small for the
        first ``_WRITE_RETRY_SLOW_AFTER_S`` (fast reclaim on millisecond
        contention) and backs off after that, and never overshoots the
        deadline by a full slow-jitter.
        """
        now = time.monotonic()
        if now >= deadline:
            return False
        elapsed = now - (deadline - patience_s)
        if elapsed >= self._WRITE_RETRY_SLOW_AFTER_S:
            jitter = random.uniform(self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S)
        else:
            jitter = random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S)
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    @staticmethod
    def _is_fts_write_corruption_error(exc: sqlite3.DatabaseError) -> bool:
        """True for the error class a corrupt FTS index raises on writes.

        The message varies by SQLite version: older builds raise the generic
        ``database disk image is malformed`` (covered by
        ``is_malformed_db_error``); newer builds (e.g. ubuntu-latest CI)
        raise the FTS5-specific ``fts5: corrupt structure record for table
        "messages_fts"``. Both mean the same thing for the write path: the
        canonical rows are fine, the FTS shadow tables are not.
        """
        if is_malformed_db_error(exc):
            return True
        msg = str(exc).lower()
        return 'fts5' in msg and 'corrupt' in msg

    def _try_runtime_fts_rebuild(self, exc: sqlite3.DatabaseError) -> bool:
        """One-shot in-place FTS rebuild after a corrupt-index write failure.

        Returns True when a rebuild was performed and the failed write should
        be retried; False when the error isn't the FTS-corruption class, FTS
        is disabled, or a rebuild was already attempted for this instance.

        Delegates to :meth:`rebuild_fts` (the FTS5 ``'rebuild'`` command —
        index rewritten from the canonical messages table, zero message-row
        mutation). Safe to call from ``_execute_write``'s except path: the
        failed transaction was rolled back and ``self._lock`` released before
        the exception propagated, and ``rebuild_fts`` re-acquires it.
        E2E-verified: a corrupted ``messages_fts_data`` shadow table rejects
        every append; after the in-place rebuild the same append succeeds and
        search works again.
        """
        if self._fts_runtime_rebuild_attempted:
            return False
        if not self._fts_enabled:
            return False
        if not self._is_fts_write_corruption_error(exc):
            return False
        self._fts_runtime_rebuild_attempted = True
        logger.warning('state.db write failed with an FTS-corruption error (%s) — attempting one-shot in-place FTS rebuild; canonical message rows are preserved.', exc)
        try:
            rebuilt = self.rebuild_fts()
        except Exception as rebuild_exc:
            logger.error('In-place FTS rebuild failed (%s); the database needs the full offline repair path (repair_state_db_schema).', rebuild_exc)
            return False
        if not rebuilt:
            logger.error('In-place FTS rebuild made no progress; the database needs the full offline repair path (repair_state_db_schema).')
            return False
        logger.warning('state.db FTS indexes rebuilt in place (%d); retrying the failed write.', rebuilt)
        return True

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never raises.

        Flushes committed WAL frames back into the main DB file without
        requiring an exclusive lock.  PASSIVE is safe for frequent
        periodic use because it does not block concurrent writers and
        cannot corrupt B-tree pages under I/O pressure.

        PASSIVE does not truncate the WAL file — it stays at its
        high-water mark.  WAL truncation happens in :meth:`close`
        (TRUNCATE) and pre-VACUUM checkpoints, which run infrequently
        under controlled conditions.

        Previous TRUNCATE strategy caused B-tree corruption on large
        databases (65K+ pages) due to the exclusive-lock I/O pressure
        from checkpointing thousands of frames at once (issue #45383).
        """
        try:
            with self._lock:
                result = self._conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                if result and result[1] > 0:
                    logger.debug('WAL checkpoint: %d/%d pages checkpointed', result[2], result[1])
        except Exception as exc:
            logger.warning('WAL checkpoint (PASSIVE) failed: %s', exc)

    def close(self):
        """Close the database connection.

        Drains queued token deltas first (the background writer needs the
        connection). Writable connections then attempt a TRUNCATE WAL
        checkpoint so exiting writer processes help shrink the WAL file.
        Read-only connections never request a checkpoint.
        """
        self._stop_token_writer()
        atexit.unregister(self._drain_token_queue_at_exit)
        with self._read_conns_lock:
            self._read_conns_closed = True
            read_conns = list(self._read_conns)
            self._read_conns.clear()
        for conn in read_conns:
            try:
                conn.close()
            except Exception:
                pass
        self._read_local.conn = None
        with self._lock:
            if self._conn:
                if not self.read_only:
                    try:
                        self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    except Exception as exc:
                        logger.debug('WAL checkpoint (TRUNCATE) at close failed: %s', exc)
                self._conn.close()
                self._conn = None
    _FTS_REBUILD_CHUNK_ROWS = 500
    _FTS_REBUILD_DUTY_FACTOR = 4.0
    _FTS_REBUILD_MIN_PAUSE = 0.2
    _FTS_TRASH_PREFIX = 'fts_v22_trash_'

    def _has_fts_trash(self, conn) -> bool:
        """True when demoted v22 shadow tables are still awaiting teardown.
        Caller must hold ``self._lock`` (or pass a migration-time cursor)."""
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name LIKE ? ESCAPE '\\' LIMIT 1", (self._FTS_TRASH_PREFIX.replace('_', '\\_') + '%',)).fetchone())

    def _insert_session_row(self, session_id: str, source: str, model: str=None, model_config: Dict[str, Any]=None, system_prompt: str=None, user_id: str=None, session_key: Optional[str]=None, chat_id: str=None, chat_type: str=None, thread_id: str=None, parent_session_id: str=None, cwd: str=None, profile_name: str=None, git_repo_root: str=None) -> None:
        """Insert a session row, enriching NULL metadata on conflict.

        The gateway's ``get_or_create_session`` creates a bare row (source +
        user_id) *before* the agent exists; the agent's later
        ``create_session`` then carries the real ``model`` / ``model_config`` /
        ``system_prompt``. A plain ``INSERT OR IGNORE`` silently dropped that
        enrichment, leaving gateway sessions with NULL model/billing metadata.
        The ``ON CONFLICT`` upsert backfills those fields via ``COALESCE`` —
        only filling columns that are still NULL, never overwriting values an
        earlier writer already set (so a later bare call with source="unknown"
        can't clobber a real source/model).

        ``chat_id``/``thread_id`` record the messaging origin (the chat/room and
        thread the session was started in) so that gateway ``/resume`` can prove
        a persisted, now-inactive row belongs to the caller's chat/thread before
        switching to it (IDOR scoping — without them the ``sessions`` table has
        no chat/thread to compare).

        When ``parent_session_id`` is set (compression fork, delegate/subagent
        spawn, branch continuation) and this row's own ``cwd``/``git_repo_root``/
        ``git_branch``/``profile_name`` are still NULL after the insert, they are
        backfilled from the parent row. Callers of ``create_session`` for a child
        session historically didn't propagate these fields themselves (e.g. the
        compression-fork path), so a lineage could silently lose its working
        directory and drop out of the project sidebar every time it forked
        (#64709), or lose its owning profile and be aggregated as "default" every
        time it rotated or branched (the cross-profile session-jump bug). This
        only fills NULLs — an explicit value on the child is never overwritten.
        For compression forks specifically
        (parent ended with ``end_reason='compression'``), the gateway origin
        columns (``user_id``/``session_key``/``chat_id``/``chat_type``/
        ``thread_id``/``display_name``/``origin_json``) are inherited too, so a
        crash before the gateway re-records the peer can't strand the child
        without a recoverable routing mapping (#59527).
        """

        def _do(conn):
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute('INSERT INTO sessions (\n                   id, source, user_id, session_key, chat_id, chat_type, thread_id,\n                   model, model_config, system_prompt, system_prompt_hash,\n                   parent_session_id, cwd, profile_name, git_repo_root, started_at\n                )\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       model = COALESCE(sessions.model, excluded.model),\n                       model_config = COALESCE(sessions.model_config, excluded.model_config),\n                       system_prompt_hash = COALESCE(\n                           sessions.system_prompt_hash,\n                           excluded.system_prompt_hash\n                       ),\n                       system_prompt = CASE\n                           WHEN sessions.system_prompt_hash IS NULL\n                                AND excluded.system_prompt_hash IS NOT NULL\n                           THEN NULL\n                           ELSE sessions.system_prompt\n                       END,\n                       session_key = COALESCE(sessions.session_key, excluded.session_key),\n                       chat_id = COALESCE(sessions.chat_id, excluded.chat_id),\n                       chat_type = COALESCE(sessions.chat_type, excluded.chat_type),\n                       thread_id = COALESCE(sessions.thread_id, excluded.thread_id),\n                       parent_session_id = COALESCE(sessions.parent_session_id, excluded.parent_session_id),\n                       cwd = COALESCE(sessions.cwd, excluded.cwd),\n                       profile_name = COALESCE(sessions.profile_name, excluded.profile_name),\n                       git_repo_root = COALESCE(sessions.git_repo_root, excluded.git_repo_root)', (session_id, source, user_id, session_key, chat_id, chat_type, thread_id, model, json.dumps(model_config) if model_config else None, system_prompt_hash, parent_session_id, cwd, profile_name, git_repo_root, time.time()))
            if system_prompt_hash is not None:
                self._delete_unreferenced_system_prompts(conn)
            if parent_session_id:
                conn.execute('UPDATE sessions\n                       SET cwd = COALESCE(sessions.cwd,\n                                 (SELECT p.cwd FROM sessions p\n                                   WHERE p.id = sessions.parent_session_id)),\n                           git_repo_root = COALESCE(sessions.git_repo_root,\n                                           (SELECT p.git_repo_root FROM sessions p\n                                             WHERE p.id = sessions.parent_session_id)),\n                           git_branch = COALESCE(sessions.git_branch,\n                                        (SELECT p.git_branch FROM sessions p\n                                          WHERE p.id = sessions.parent_session_id)),\n                           profile_name = COALESCE(sessions.profile_name,\n                                          (SELECT p.profile_name FROM sessions p\n                                            WHERE p.id = sessions.parent_session_id))\n                     WHERE id = ? AND parent_session_id IS NOT NULL', (session_id,))
                conn.execute("UPDATE sessions\n                       SET user_id = COALESCE(sessions.user_id,\n                                     (SELECT p.user_id FROM sessions p\n                                       WHERE p.id = sessions.parent_session_id)),\n                           session_key = COALESCE(sessions.session_key,\n                                         (SELECT p.session_key FROM sessions p\n                                           WHERE p.id = sessions.parent_session_id)),\n                           chat_id = COALESCE(sessions.chat_id,\n                                     (SELECT p.chat_id FROM sessions p\n                                       WHERE p.id = sessions.parent_session_id)),\n                           chat_type = COALESCE(sessions.chat_type,\n                                       (SELECT p.chat_type FROM sessions p\n                                         WHERE p.id = sessions.parent_session_id)),\n                           thread_id = COALESCE(sessions.thread_id,\n                                       (SELECT p.thread_id FROM sessions p\n                                         WHERE p.id = sessions.parent_session_id)),\n                           display_name = COALESCE(sessions.display_name,\n                                          (SELECT p.display_name FROM sessions p\n                                            WHERE p.id = sessions.parent_session_id)),\n                           origin_json = COALESCE(sessions.origin_json,\n                                         (SELECT p.origin_json FROM sessions p\n                                           WHERE p.id = sessions.parent_session_id))\n                     WHERE id = ? AND parent_session_id IS NOT NULL\n                       AND EXISTS (\n                           SELECT 1 FROM sessions p\n                           WHERE p.id = sessions.parent_session_id\n                             AND p.end_reason = 'compression'\n                       )", (session_id,))
        self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Create a new session record. Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id

    def record_gateway_session_peer(self, session_id: str, *, source: str, user_id: str=None, session_key: str=None, chat_id: str=None, chat_type: str=None, thread_id: str=None, display_name: str=None, origin_json: str=None, include_compression_ancestors: bool=False) -> None:
        """Persist the gateway routing peer for an existing session row.

        ``display_name`` / ``origin_json`` carry the gateway's presentation
        and full origin metadata (#9006) so consumers (mcp_serve, mirror,
        channel directory) can read routing data from state.db instead of
        sessions.json.  They are COALESCE'd only in the sense that ``None``
        leaves the existing value untouched.

        ``include_compression_ancestors`` keeps a logical compression lineage
        on one routing peer when an explicit gateway resume moves its tip to a
        different lane. Normal per-turn metadata refreshes update only the
        supplied row.
        """
        if not session_id or not session_key:
            return

        def _do(conn):
            lineage_cte = ''
            target_clause = 'WHERE id = ?'
            query_params = []
            if include_compression_ancestors:
                lineage_cte = "\n                    WITH RECURSIVE compression_lineage(id) AS (\n                        SELECT ?\n                        UNION\n                        SELECT parent.id\n                        FROM compression_lineage lineage\n                        JOIN sessions child ON child.id = lineage.id\n                        JOIN sessions parent ON parent.id = child.parent_session_id\n                        WHERE parent.end_reason = 'compression'\n                          AND json_extract(\n                              COALESCE(child.model_config, '{}'),\n                              '$._branched_from'\n                          ) IS NULL\n                          AND json_extract(\n                              COALESCE(child.model_config, '{}'),\n                              '$._delegate_from'\n                          ) IS NULL\n                          AND COALESCE(child.source, '') != 'tool'\n                    )\n                "
                target_clause = 'WHERE id IN (SELECT id FROM compression_lineage)'
                query_params.append(session_id)
            query_params.extend((session_key, source, user_id, chat_id, chat_type, thread_id, display_name, origin_json))
            if not include_compression_ancestors:
                query_params.append(session_id)
            conn.execute(f'{lineage_cte}\n                   UPDATE sessions\n                   SET session_key = ?, source = ?, user_id = ?, chat_id = ?,\n                       chat_type = ?, thread_id = ?,\n                       display_name = COALESCE(?, display_name),\n                       origin_json = COALESCE(?, origin_json)\n                   {target_clause}', query_params)
        self._execute_write(_do)

    def set_expiry_finalized(self, session_id: str, finalized: bool=True) -> None:
        """Mark a gateway session's expiry-finalization flag in state.db.

        Mirrors ``SessionEntry.expiry_finalized`` (sessions.json) so the flag
        survives even if the JSON index is pruned or lost (#9006).
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute('UPDATE sessions SET expiry_finalized = ? WHERE id = ?', (1 if finalized else 0, session_id))
        self._execute_write(_do)

    def save_gateway_routing_entry(self, session_key: str, entry_json: str, *, scope: str='') -> None:
        """Upsert one gateway routing entry (session_key -> SessionEntry JSON).

        The gateway_routing table is the durable replacement for
        sessions.json: one row per routing key, holding the full serialized
        ``SessionEntry`` so the gateway can rehydrate exactly what it wrote.

        ``scope`` namespaces the index the way separate sessions.json files
        did (one per sessions_dir) — callers pass their sessions_dir path so
        two stores with different directories never share routing state.
        """
        if not session_key or not entry_json:
            return

        def _do(conn):
            conn.execute('INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(scope, session_key) DO UPDATE SET\n                       entry_json = excluded.entry_json,\n                       updated_at = excluded.updated_at', (scope, session_key, entry_json, time.time()))
        self._execute_write(_do)

    def replace_gateway_routing_entries(self, entries: Dict[str, str], *, scope: str='') -> None:
        """Atomically replace the routing index for *scope* with *entries*.

        Mirrors the sessions.json full-rewrite semantics: keys absent from
        *entries* are removed (pruned/reset sessions disappear from the
        index).  Runs as a single write transaction.  Other scopes are
        untouched.
        """
        now = time.time()

        def _do(conn):
            conn.execute('DELETE FROM gateway_routing WHERE scope = ?', (scope,))
            if entries:
                conn.executemany('INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)', [(scope, k, v, now) for k, v in entries.items() if k and v])
        self._execute_write(_do)

    def load_gateway_routing_entries(self, *, scope: str='') -> Dict[str, str]:
        """Load routing entries for *scope* as {session_key: entry_json}."""
        with self._lock:
            rows = self._conn.execute('SELECT session_key, entry_json FROM gateway_routing WHERE scope = ?', (scope,)).fetchall()
        return {r['session_key']: r['entry_json'] for r in rows}

    def delete_gateway_routing_entries(self, session_keys: List[str], *, scope: str='') -> None:
        """Remove routing entries for the given session keys in *scope*."""
        if not session_keys:
            return

        def _do(conn):
            conn.executemany('DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?', [(scope, k) for k in session_keys])
        self._execute_write(_do)

    def list_gateway_sessions(self, *, platform: Optional[str]=None, active_only: bool=True) -> List[Dict[str, Any]]:
        """List gateway sessions (rows with a session_key) from state.db.

        Returns the newest row per session_key — the same shape consumers got
        from sessions.json: one live mapping per routing key.  ``platform``
        filters on ``source``; ``active_only`` restricts to sessions that
        have not ended.
        """
        self.flush_token_counts()
        query = f"\n            SELECT sessions.*,\n                   COALESCE(sp.prompt, sessions.system_prompt)\n                       AS _system_prompt_resolved,\n                   {_sql_session_last_active('sessions')} AS last_active\n            FROM sessions\n            LEFT JOIN system_prompts sp\n              ON sp.hash = sessions.system_prompt_hash\n            WHERE session_key IS NOT NULL\n              AND started_at = (\n                  SELECT MAX(s2.started_at) FROM sessions s2\n                  WHERE s2.session_key = sessions.session_key\n              )\n        "
        params: list = []
        if platform:
            query += ' AND LOWER(source) = LOWER(?)'
            params.append(platform)
        if active_only:
            query += ' AND ended_at IS NULL'
        query += ' ORDER BY last_active DESC'
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._session_row_dict(r) for r in rows]

    def find_session_by_origin(self, *, platform: str, chat_id: str, thread_id: Optional[str]=None, user_id: Optional[str]=None) -> Optional[str]:
        """Find the most recent live session_id for a platform + chat origin.

        Equivalent of gateway/mirror's sessions.json scan: matches on
        source + chat_id (+ thread_id when provided).  When ``user_id`` is
        provided, exact sender matches are preferred; if multiple distinct
        users share the chat and none matches, returns None rather than
        contaminating another participant's session.
        """
        if not platform or chat_id in (None, ''):
            return None
        query = '\n            SELECT id, user_id, started_at FROM sessions\n            WHERE LOWER(source) = LOWER(?)\n              AND session_key IS NOT NULL\n              AND chat_id = ?\n              AND ended_at IS NULL\n        '
        params: list = [platform, str(chat_id)]
        if thread_id is not None:
            query += " AND COALESCE(thread_id, '') = ?"
            params.append(str(thread_id))
        query += ' ORDER BY started_at DESC'
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(query, params).fetchall()]
        if not rows:
            return None
        if user_id:
            exact = [r for r in rows if str(r.get('user_id') or '') == str(user_id)]
            if exact:
                return str(exact[0]['id'])
            if len(rows) > 1:
                return None
        elif len(rows) > 1:
            distinct_users = {str(r.get('user_id') or '').strip() for r in rows if str(r.get('user_id') or '').strip()}
            if len(distinct_users) > 1:
                return None
        return str(rows[0]['id'])

    def find_latest_gateway_session_for_peer(self, *, source: str, user_id: Optional[str]=None, session_key: Optional[str]=None, chat_id: Optional[str]=None, chat_type: Optional[str]=None, thread_id: Optional[str]=None) -> Optional[Dict[str, Any]]:
        """Find the latest recoverable gateway session for a routing peer.

        ``sessions.json`` is the fast routing index, but it can be missing or
        pruned after process-level restart bugs.  New gateway sessions persist
        the deterministic ``session_key`` on the durable session row so the
        mapping can be rebuilt exactly.  Rows ended only by older gateway
        cleanup's ``agent_close`` bug or a mistaken TUI ``ws_orphan_reap``
        (dashboard viewer disconnect before #60609) are treated as recoverable;
        explicit conversation boundaries such as /new, /resume switches, and
        compression splits are not.
        """
        if not session_key:
            return None
        with self._lock:
            row = self._conn.execute("\n                SELECT s.*,\n                       COALESCE(sp.prompt, s.system_prompt)\n                           AS _system_prompt_resolved\n                FROM sessions s\n                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash\n                WHERE s.session_key = ?\n                  AND s.source = ?\n                  AND (s.ended_at IS NULL OR s.end_reason IN ('agent_close', 'ws_orphan_reap'))\n                  AND (COALESCE(s.message_count, 0) > 0 OR EXISTS (\n                      SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1\n                  ))\n                ORDER BY s.started_at DESC\n                LIMIT 1\n                ", (session_key, source)).fetchone()
            if row is not None:
                return self._session_row_dict(row)
            if chat_id is None or chat_type is None:
                return None
            row = self._conn.execute("\n                SELECT s.*,\n                       COALESCE(sp.prompt, s.system_prompt)\n                           AS _system_prompt_resolved\n                FROM sessions s\n                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash\n                WHERE s.source = ?\n                  AND COALESCE(s.user_id, '') = COALESCE(?, '')\n                  AND COALESCE(s.chat_id, '') = COALESCE(?, '')\n                  AND COALESCE(s.chat_type, '') = COALESCE(?, '')\n                  AND COALESCE(s.thread_id, '') = COALESCE(?, '')\n                  AND (s.ended_at IS NULL OR s.end_reason IN ('agent_close', 'ws_orphan_reap'))\n                  AND (COALESCE(s.message_count, 0) > 0 OR EXISTS (\n                      SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1\n                  ))\n                ORDER BY s.started_at DESC\n                LIMIT 1\n                ", (source, user_id, chat_id, chat_type, thread_id)).fetchone()
        return self._session_row_dict(row) if row else None
    _NON_CONTINUATION_CHILD_FILTER_SQL = "  AND COALESCE(json_extract(COALESCE({alias}model_config, '{{}}'), '$._branched_from'), '') != ?\n  AND COALESCE(json_extract(COALESCE({alias}model_config, '{{}}'), '$._delegate_from'), '') != ?\n  AND COALESCE({alias}source, '') != 'tool'\n"

    def find_live_compression_child(self, parent_session_id: str) -> Optional[Dict[str, Any]]:
        """Return the unique live direct child of a compression-ended session.

        A stale agent may observe that another compression path already rotated
        its parent. Recovery is safe only when the durable lineage identifies
        exactly one live direct continuation. Multiple children are treated as
        ambiguous and fail closed rather than guessing which transcript owns
        subsequent messages.
        """
        if not parent_session_id:
            return None
        with self._lock:
            parent = self._conn.execute('SELECT ended_at, end_reason FROM sessions WHERE id = ?', (parent_session_id,)).fetchone()
            if parent is None or parent['ended_at'] is None or parent['end_reason'] != 'compression':
                return None
            rows = self._conn.execute('\n                SELECT s.*,\n                       COALESCE(sp.prompt, s.system_prompt)\n                           AS _system_prompt_resolved\n                FROM sessions s\n                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash\n                WHERE s.parent_session_id = ?\n                  AND s.ended_at IS NULL\n                ' + self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias='s.') + '\n                ORDER BY s.started_at ASC\n                LIMIT 2\n                ', (parent_session_id, parent_session_id, parent_session_id)).fetchall()
        return self._session_row_dict(rows[0]) if len(rows) == 1 else None

    def reopen_orphaned_compression_session(self, session_id: str) -> bool:
        """Reopen a compression parent only when no continuation was published.

        Compression publication is atomic in current builds, but older builds
        could leave a closed parent behind after an interrupted handoff.  This
        recovery is deliberately conservative: an active compression lease or
        any canonical child means the lineage is still owned by another path,
        so the caller must fail closed instead of reopening the parent.
        """
        if not session_id:
            return False

        def _do(conn):
            parent = conn.execute('SELECT ended_at, end_reason FROM sessions WHERE id = ?', (session_id,)).fetchone()
            if parent is None or parent['ended_at'] is None or parent['end_reason'] != 'compression':
                return False
            child = conn.execute('\n                SELECT 1\n                FROM sessions\n                WHERE parent_session_id = ?\n                ' + self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias='') + '\n                LIMIT 1\n                ', (session_id, session_id, session_id)).fetchone()
            if child is not None:
                return False
            now = time.time()
            lock_row = conn.execute('SELECT holder, expires_at FROM compression_locks WHERE session_id = ?', (session_id,)).fetchone()
            if lock_row is not None:
                expires_at = lock_row['expires_at']
                if expires_at is None or float(expires_at) >= now:
                    return False
                deleted = conn.execute('DELETE FROM compression_locks WHERE session_id = ? AND holder = ? AND expires_at = ?', (session_id, lock_row['holder'], expires_at))
                if deleted.rowcount != 1:
                    return False
            updated = conn.execute("UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ? AND ended_at IS NOT NULL AND end_reason = 'compression'", (session_id,))
            return updated.rowcount == 1
        return bool(self._execute_write(_do))

    def publish_compression_child(self, *, parent_session_id: str, child_session_id: str, source: str, messages: List[Dict[str, Any]], model: str=None, model_config: Dict[str, Any]=None, system_prompt: str=None, cwd: str=None, profile_name: str=None, compression_lock_holder: str=None, require_compression_lease: bool=True) -> None:
        """Atomically close a parent and publish its durable compression child.

        The parent closure, child row, and compacted handoff become visible in
        one transaction. Readers can therefore observe either the live parent or
        a complete child, never an ended parent with a missing/empty child.
        """

        def _do(conn):
            lock_row = conn.execute('SELECT holder, expires_at FROM compression_locks WHERE session_id = ?', (parent_session_id,)).fetchone()
            if require_compression_lease and (lock_row is None or not compression_lock_holder or lock_row['holder'] != compression_lock_holder or (float(lock_row['expires_at']) <= time.time())):
                raise CompressionSessionBusyError(f'Compression lease lost before publication: {parent_session_id}')
            parent = conn.execute('SELECT ended_at, cwd, git_branch, git_repo_root,\n                          user_id, session_key, chat_id, chat_type,\n                          thread_id, display_name, origin_json, profile_name\n                   FROM sessions WHERE id = ?', (parent_session_id,)).fetchone()
            if parent is None:
                raise RuntimeError(f'Compression parent not found: {parent_session_id}')
            if parent['ended_at'] is not None:
                raise RuntimeError(f'Compression parent already ended: {parent_session_id}')
            if not messages:
                raise RuntimeError('Compression child handoff must not be empty')
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute('INSERT INTO sessions (\n                   id, source, model, model_config, system_prompt,\n                   system_prompt_hash,\n                   parent_session_id, cwd, git_branch, git_repo_root,\n                   profile_name, user_id, session_key, chat_id, chat_type,\n                   thread_id, display_name, origin_json, started_at\n                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (child_session_id, source, model, json.dumps(model_config) if model_config else None, system_prompt_hash, parent_session_id, cwd or parent['cwd'], parent['git_branch'], parent['git_repo_root'], profile_name or parent['profile_name'], parent['user_id'], parent['session_key'], parent['chat_id'], parent['chat_type'], parent['thread_id'], parent['display_name'], parent['origin_json'], time.time()))
            total_messages, total_tool_calls = self._insert_message_rows(conn, child_session_id, messages)
            conn.execute('UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?', (total_messages, total_tool_calls, child_session_id))
            updated = conn.execute("UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ? AND ended_at IS NULL", (time.time(), parent_session_id))
            if updated.rowcount != 1:
                raise RuntimeError(f'Compression parent changed during publication: {parent_session_id}')
        self._execute_write(_do)

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended. The first end_reason wins:
        compression-split sessions must keep their ``end_reason = 'compression'``
        record even if a later stale ``end_session()`` call (e.g. from a
        desynced CLI session_id after ``/resume`` or ``/branch``) targets them
        with a different reason. Use ``reopen_session()`` first if you
        intentionally need to re-end a closed session with a new reason.
        """

        def _do(conn):
            conn.execute('UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL', (time.time(), end_reason, session_id))
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""

        def _do(conn):
            conn.execute('UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?', (session_id,))
        self._execute_write(_do)

    def promote_to_session_reset(self, session_id: str, reason: str='session_reset') -> bool:
        """Durably mark a session as ended by an intentional reset boundary.

        Promotes *only* live rows (``ended_at IS NULL``) or rows carrying an
        accidental end_reason that the recovery query
        (``find_latest_gateway_session_for_peer``) treats as recoverable:
        ``agent_close`` (older gateway cleanup bug) and ``ws_orphan_reap``
        (mistaken TUI reaper).  Explicit conversation boundaries such as
        ``compression``, ``session_reset``, ``session_switch``, etc. are
        preserved — the first writer wins for those, and a later expiry
        finalization must not silently overwrite them.

        Plain ``end_session()`` is NOT sufficient for reset boundaries: it
        no-ops on an already-ended row, so a row that agent cleanup already
        closed as ``agent_close`` would stay recoverable and stale-route
        recovery would resurrect the reset session with its full history
        (#61220, #61993, #63539).

        Keep this promotion set in sync with the recoverable set in
        ``find_latest_gateway_session_for_peer`` — any reason recovery would
        reopen must be promotable here.

        ``reason`` lets reset paths keep their auditable specific reasons
        (``idle``, ``daily``, ``suspended``, ``resume_pending_expired``).

        Returns ``True`` when the row was promoted, ``False`` when skipped
        (already has a different explicit end_reason, or row not found).
        """
        if not session_id:
            return False
        now = time.time()

        def _do(conn):
            cursor = conn.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND (ended_at IS NULL OR end_reason IN ('agent_close', 'ws_orphan_reap'))", (now, reason, session_id))
            return cursor.rowcount
        try:
            rows = self._execute_write(_do)
            return bool(rows)
        except Exception:
            return False

    def update_session_cwd(self, session_id: str, cwd: str, git_branch: str=None, git_repo_root: str=None, replace_git_meta: bool=False) -> None:
        """Persist the session working directory when a frontend knows it.

        ``git_branch`` records the git branch checked out in ``cwd`` at the time
        the session started/resumed. The sidebar groups main-checkout sessions
        by this so feature-branch work doesn't pile under a single "main" row
        (the main checkout's *current* branch is transient and would
        misattribute past sessions).

        ``git_repo_root`` records the git repo this cwd belongs to — the
        authoritative project key. Resolving it here, at the lowest level, means
        every surface reads the same membership instead of re-probing git in the
        GUI over a partial page. Each field is only written when non-empty so a
        probe failure never clobbers a previously-captured value.

        ``replace_git_meta`` inverts that non-empty rule: a deliberate workspace
        MOVE (re-homing a session into another project) must overwrite the old
        repo identity even when the new cwd resolves to none — keeping the stale
        root would leave the session grouped under the project it just left.
        """
        if not session_id or not cwd:
            return
        branch = (git_branch or '').strip()
        repo_root = (git_repo_root or '').strip()
        sets = ['cwd = ?']
        params: List[Any] = [cwd]
        if branch or replace_git_meta:
            sets.append('git_branch = ?')
            params.append(branch or None)
        if repo_root or replace_git_meta:
            sets.append('git_repo_root = ?')
            params.append(repo_root or None)
        params.append(session_id)

        def _do(conn):
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
        self._execute_write(_do)

    def backfill_repo_roots(self, cwd_to_root: Dict[str, str]) -> None:
        """Persist resolved git repo roots for cwds that don't have one yet.

        Backfills history so projects light up for sessions created before the
        column existed, without clobbering an already-recorded root. Only
        non-empty roots are written (a non-git cwd stays NULL).
        """
        pairs = [(root, cwd) for cwd, root in cwd_to_root.items() if root and cwd]
        if not pairs:
            return

        def _do(conn):
            for root, cwd in pairs:
                conn.execute("UPDATE sessions SET git_repo_root = ? WHERE cwd = ? AND COALESCE(git_repo_root, '') = ''", (root, cwd))
        self._execute_write(_do)

    def record_compression_failure_cooldown(self, session_id: str, cooldown_until: float, error: Optional[str]=None) -> None:
        """Persist the active compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute('UPDATE sessions SET compression_failure_cooldown_until = ?, compression_failure_error = ? WHERE id = ?', (cooldown_until, error, session_id))
        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning('record_compression_failure_cooldown(%s) failed: %s', session_id, exc)

    def get_compression_failure_cooldown(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the active compression-failure cooldown for ``session_id``."""
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute('SELECT compression_failure_cooldown_until, compression_failure_error FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            return None
        cooldown_until = row['compression_failure_cooldown_until'] if isinstance(row, sqlite3.Row) else row[0]
        if cooldown_until is None:
            return None
        cooldown_until = float(cooldown_until)
        if cooldown_until <= now:
            return None
        error = row['compression_failure_error'] if isinstance(row, sqlite3.Row) else row[1]
        return {'cooldown_until': cooldown_until, 'remaining_seconds': cooldown_until - now, 'error': error}

    def get_compression_failure_cooldown_row(self, session_id: str) -> Dict[str, Any]:
        """Return the exact stored cooldown columns without expiry filtering.

        Compression cancellation uses this under its session lease so rollback
        can preserve an expired row, a partially-null row, or an absent session
        exactly instead of converting those states through the active-cooldown
        API.
        """
        if not session_id:
            return {'session_exists': False, 'cooldown_until': None, 'error': None}
        with self._lock:
            row = self._conn.execute('SELECT compression_failure_cooldown_until, compression_failure_error FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            return {'session_exists': False, 'cooldown_until': None, 'error': None}
        cooldown_until = row['compression_failure_cooldown_until'] if isinstance(row, sqlite3.Row) else row[0]
        error = row['compression_failure_error'] if isinstance(row, sqlite3.Row) else row[1]
        return {'session_exists': True, 'cooldown_until': float(cooldown_until) if cooldown_until is not None else None, 'error': error}

    def restore_compression_failure_cooldown_row(self, session_id: str, snapshot: Dict[str, Any]) -> None:
        """Restore and verify an exact cooldown-row snapshot.

        Unlike the ordinary record/clear helpers, this transactional rollback
        API deliberately propagates write and verification failures. A caller
        must not report cancellation as mutation-free when compensation failed.
        """
        expected_exists = bool(snapshot.get('session_exists', False))
        if not expected_exists:
            actual = self.get_compression_failure_cooldown_row(session_id)
            if actual.get('session_exists', False):
                raise RuntimeError('cannot restore absent compression cooldown row: session now exists')
            return
        deadline = snapshot.get('cooldown_until')
        error = snapshot.get('error')

        def _do(conn):
            cursor = conn.execute('UPDATE sessions SET compression_failure_cooldown_until = ?, compression_failure_error = ? WHERE id = ?', (deadline, error, session_id))
            if cursor.rowcount != 1:
                raise RuntimeError(f'compression cooldown rollback session missing: {session_id}')
        self._execute_write(_do)
        actual = self.get_compression_failure_cooldown_row(session_id)
        expected = {'session_exists': True, 'cooldown_until': float(deadline) if deadline is not None else None, 'error': error}
        if actual != expected:
            raise RuntimeError(f'compression cooldown rollback verification failed: expected={expected!r}, actual={actual!r}')

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        """Clear any persisted compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute('UPDATE sessions SET compression_failure_cooldown_until = NULL, compression_failure_error = NULL WHERE id = ?', (session_id,))
        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning('clear_compression_failure_cooldown(%s) failed: %s', session_id, exc)

    def get_compression_fallback_streak(self, session_id: str) -> int:
        """Return the persisted deterministic-fallback streak."""
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute('SELECT compression_fallback_streak FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            return 0
        value = row['compression_fallback_streak'] if isinstance(row, sqlite3.Row) else row[0]
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_fallback_streak(self, session_id: str, streak: int) -> None:
        """Persist the deterministic-fallback streak for one session."""
        if not session_id:
            return
        normalized = max(0, int(streak))

        def _do(conn):
            conn.execute('UPDATE sessions SET compression_fallback_streak = ? WHERE id = ?', (normalized, session_id))
        self._execute_write(_do)

    def get_compression_ineffective_count(self, session_id: str) -> int:
        """Return the persisted ineffective-compaction strike count.

        Mirrors ``get_compression_fallback_streak``: this is the durable half
        of the anti-thrash guard (``_ineffective_compression_count`` on the
        built-in compressor), persisted so that a fresh compressor bound to a
        resumed session inherits an armed/tripped guard instead of starting
        from zero across process restarts (#54923).
        """
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute('SELECT compression_ineffective_count FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            return 0
        value = row['compression_ineffective_count'] if isinstance(row, sqlite3.Row) else row[0]
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_ineffective_count(self, session_id: str, count: int) -> None:
        """Persist the ineffective-compaction strike count for one session."""
        if not session_id:
            return
        normalized = max(0, int(count))

        def _do(conn):
            conn.execute('UPDATE sessions SET compression_ineffective_count = ? WHERE id = ?', (normalized, session_id))
        self._execute_write(_do)

    def refresh_compression_lock(self, session_id: str, holder: str, ttl_seconds: float=300.0) -> bool:
        """Extend the compression lock lease if ``holder`` still owns it.

        Ownership is decided by the ``holder`` column alone, deliberately NOT
        by ``expires_at``: a live owner whose refresher thread was starved
        (GC pause, loaded CI runner, a slow write escaping ``_execute_write``'s
        retry budget) past its own TTL must be able to revive its still-unclaimed
        row on the next tick. Requiring ``expires_at >= now`` here made such a
        stall permanent — every later refresh matched 0 rows, so the owner kept
        compressing and rotating with no lease at all, which is exactly the
        unprotected window a competing path can fork the session lineage in.

        This does not resurrect a lock somebody else already took: SQLite
        serialises writes, so a reclaim (DELETE-expired + INSERT-or-IGNORE in
        :meth:`try_acquire_compression_lock`) and this UPDATE never interleave.
        Reclaim-first replaces ``holder``, so this UPDATE matches nothing and
        returns False; refresh-first pushes ``expires_at`` into the future, so
        the reclaimer's DELETE-expired matches nothing and its acquire fails.
        """
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            cur = conn.execute('UPDATE compression_locks SET expires_at = ? WHERE session_id = ? AND holder = ?', (expires_at, session_id, holder))
            return cur.rowcount > 0
        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning('refresh_compression_lock(%s) failed: %s', session_id, exc)
            return False

    def try_acquire_compression_lock(self, session_id: str, holder: str, ttl_seconds: float=300.0) -> bool:
        """Try to atomically acquire the compression lock for ``session_id``.

        Returns ``True`` on success (caller now owns the lock and must
        release via :meth:`release_compression_lock`).  Returns ``False``
        if another holder already owns a non-expired lock — the caller
        MUST NOT proceed with compression in that case (its rotation would
        race against the holder's, splitting the session lineage).

        Expired locks (``expires_at < now``) are reclaimed transparently.
        Structured holders whose local ``pid=`` no longer exists are reclaimed
        immediately, so a gateway killed during compression does not stall the
        replacement process for the full lease TTL.

        Implementation: single-transaction DELETE-expired + INSERT-or-IGNORE,
        followed by a SELECT to confirm we got the row. SQLite serialises
        writes, so the whole sequence is atomic against other writers.
        """
        if not session_id:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            reclaimed_holder = None
            row = conn.execute('SELECT holder, expires_at FROM compression_locks WHERE session_id = ?', (session_id,)).fetchone()
            if row is not None:
                current_holder = row['holder'] if isinstance(row, sqlite3.Row) else row[0]
                current_expires_at = row['expires_at'] if isinstance(row, sqlite3.Row) else row[1]
                if current_expires_at < now or _compression_lock_holder_process_is_dead(current_holder):
                    conn.execute('DELETE FROM compression_locks WHERE session_id = ? AND holder = ?', (session_id, current_holder))
                    reclaimed_holder = current_holder
            conn.execute('INSERT OR IGNORE INTO compression_locks (session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)', (session_id, holder, now, expires_at))
            row = conn.execute('SELECT holder FROM compression_locks WHERE session_id = ?', (session_id,)).fetchone()
            acquired = row is not None and (row['holder'] if isinstance(row, sqlite3.Row) else row[0]) == holder
            return (acquired, reclaimed_holder)
        try:
            acquired, reclaimed_holder = self._execute_write(_do)
            if reclaimed_holder:
                logger.warning('Reclaimed stale compression lock for session=%s (holder=%s)', session_id, reclaimed_holder)
            return bool(acquired)
        except sqlite3.Error as exc:
            logger.warning('try_acquire_compression_lock(%s) failed: %s', session_id, exc)
            return False

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        """Release the compression lock for ``session_id`` iff we own it.

        Idempotent: no-op when the lock has already expired and been
        reclaimed by a different holder, or when no lock exists. The
        ``holder`` check prevents a late-returning compressor from
        clobbering a fresh lock held by someone else.
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute('DELETE FROM compression_locks WHERE session_id = ? AND holder = ?', (session_id, holder))
        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning('release_compression_lock(%s) failed: %s', session_id, exc)

    def get_compression_lock_holder(self, session_id: str) -> Optional[str]:
        """Return the current (non-expired) holder for ``session_id``, or None.

        Diagnostic helper — not used by the locking protocol itself.
        """
        if not session_id:
            return None
        now = time.time()
        row = self._conn.execute('SELECT holder FROM compression_locks WHERE session_id = ? AND expires_at >= ?', (session_id, now)).fetchone()
        if row is None:
            return None
        return row['holder'] if isinstance(row, sqlite3.Row) else row[0]

    def touch_session_activity(self, session_id: str, ts: Optional[float]=None, *, description: Optional[str]=None, provenance: Optional[ActivityProvenance]=None) -> None:
        """Stamp durable mid-turn session activity (observation-only).

        Called (rate-limited) from ``AIAgent._touch_activity`` so gateway/CLI
        surfaces and stall consumers observe API/tool/compaction activity
        even when no new message row has been written yet (#72016 / #72039).

        Never moves ``last_activity_at`` backwards. When the timestamp
        advances, bounded ``last_activity_description`` /
        ``last_activity_provenance`` are written with it. No-ops when
        ``session_id`` is empty or the row does not exist.
        """
        if not session_id:
            return
        from agent.session_activity import bound_activity_description, normalize_activity_provenance
        when = float(ts if ts is not None else time.time())
        desc = bound_activity_description(description)
        prov = normalize_activity_provenance(provenance).value

        def _do(conn):
            conn.execute('UPDATE sessions SET last_activity_at = ?, last_activity_description = ?, last_activity_provenance = ? WHERE id = ? AND (last_activity_at IS NULL OR last_activity_at < ?)', (when, desc, prov, session_id, when))
        self._execute_write(_do, patience_s=self._ACTIVITY_WRITE_PATIENCE_S)

    def clear_session_activity_labels(self, session_id: str) -> None:
        """Clear mid-turn activity labels after a turn ends.

        Keeps ``last_activity_at`` intact so idle / watchdog clocks stay
        continuous. Description and provenance are observation labels for
        *what was happening at* that timestamp during an active turn; once
        the turn is idle they must not keep advertising "compressing" /
        "executing tool" (#72039).

        Response-critical-path contract (#76354 review S1): runs in the
        turn's ``finally``; a no-op clear (labels already empty) skips the
        write transaction entirely, and a real clear uses the same short
        sub-second busy budget as :meth:`touch_session_activity` instead of
        the full routine write patience.
        """
        if not session_id:
            return
        from agent.session_activity import ActivityProvenance
        try:
            row = self._conn.execute('SELECT last_activity_description, last_activity_provenance FROM sessions WHERE id = ?', (session_id,)).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            desc = row[0] if not isinstance(row, sqlite3.Row) else row['last_activity_description']
            prov = row[1] if not isinstance(row, sqlite3.Row) else row['last_activity_provenance']
            if not desc and (not prov or prov == ActivityProvenance.UNKNOWN.value):
                return

        def _do(conn):
            conn.execute('UPDATE sessions SET last_activity_description = ?, last_activity_provenance = ? WHERE id = ?', ('', ActivityProvenance.UNKNOWN.value, session_id))
        self._execute_write(_do, patience_s=self._ACTIVITY_WRITE_PATIENCE_S)

    def get_session_activity(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the durable activity snapshot for *session_id*, or None."""
        if not session_id:
            return None
        row = self.get_session(session_id)
        if not row:
            return None
        from agent.session_activity import build_activity_snapshot
        return build_activity_snapshot(last_activity_at=row.get('last_activity_at'), last_activity_description=row.get('last_activity_description'), last_activity_provenance=row.get('last_activity_provenance'))

    def update_session_meta(self, session_id: str, model_config_json: str, model: Optional[str]=None) -> None:
        """Update model_config and optionally model for an existing session.

        Uses COALESCE so that passing model=None leaves the stored model
        column unchanged.  Routes through _execute_write for the standard
        BEGIN IMMEDIATE + jitter-retry + lock guarantee.
        """
        self.flush_token_counts()

        def _do(conn):
            conn.execute('UPDATE sessions SET model_config = ?, model = COALESCE(?, model) WHERE id = ?', (model_config_json, model, session_id))
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: Optional[str]) -> None:
        """Store the full assembled system prompt snapshot."""

        def _do(conn):
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute('UPDATE sessions SET system_prompt_hash = ?, system_prompt = NULL WHERE id = ?', (system_prompt_hash, session_id))
            self._delete_unreferenced_system_prompts(conn)
        self._execute_write(_do)

    def update_session_model(self, session_id: str, model: str) -> None:
        """Update the model for a session after a mid-session switch.

        Unlike ``update_token_counts`` which uses ``COALESCE(model, ?)``
        (only filling in NULL), this unconditionally sets the model column
        so that the dashboard reflects the user's latest /model choice.
        Also nulls ``system_prompt`` so stale ``Model:`` / ``Provider:``
        footer metadata is rebuilt on the next turn. A successful /model
        switch explicitly replaces any confirmed Browser runtime lock while
        preserving unrelated lineage markers in ``model_config``.
        """
        self.flush_token_counts()

        def _do(conn):
            conn.execute("UPDATE sessions SET\n                   model = ?,\n                   model_config = CASE\n                       WHEN model_config IS NULL THEN NULL\n                       WHEN json_valid(model_config)\n                           THEN json_remove(model_config, '$.browser_model_lock')\n                       ELSE model_config\n                   END,\n                   system_prompt = NULL,\n                   system_prompt_hash = NULL\n                   WHERE id = ?", (model, session_id))
            self._delete_unreferenced_system_prompts(conn)
        self._execute_write(_do)

    def _merge_model_config_json(self, conn, session_id: str, patch: Dict[str, Any], *, on_missing: str='skip'):
        """SELECT + tolerant-parse + merge ``patch`` into a session's model_config.

        Shared by every model_config writer (``update_session_runtime_lock``,
        ``set_session_yolo``, ``archive_and_compact``,
        ``patch_session_model_config``) so the merge discipline that keeps
        lineage markers like ``_branched_from`` / ``_delegate_from`` alive
        lives in exactly one place. A ``None`` patch value deletes that key.
        Must run inside an open write transaction (callers own the UPDATE).

        Returns the serialized merged JSON — ``None`` when the merged dict is
        empty (matching ``create_session``'s NULL convention) — or the
        ``_MODEL_CONFIG_ROW_MISSING`` sentinel when the row doesn't exist and
        ``on_missing == "skip"``; ``on_missing == "raise"`` raises ValueError.
        """
        row = conn.execute('SELECT model_config FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            if on_missing == 'raise':
                raise ValueError(f'Session not found: {session_id}')
            return _MODEL_CONFIG_ROW_MISSING
        raw = row['model_config'] if isinstance(row, sqlite3.Row) else row[0]
        config: Dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    config = parsed
            except (json.JSONDecodeError, TypeError):
                config = {}
        elif isinstance(raw, dict):
            config = dict(raw)
        for key, value in patch.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        return json.dumps(config) if config else None

    def patch_session_model_config(self, session_id: str, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into a session's model_config JSON atomically.

        A ``None`` patch value removes that key. No-op when the session row
        doesn't exist or the patch is empty. This is the standalone setter for
        callers that need to update model_config *without* rewriting the
        transcript (the transcript-coupled path is ``archive_and_compact``'s
        ``model_config_patch``, which shares the same merge helper).
        """
        if not session_id or not patch:
            return

        def _do(conn):
            merged = self._merge_model_config_json(conn, session_id, patch)
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute('UPDATE sessions SET model_config = ? WHERE id = ?', (merged, session_id))
        self._execute_write(_do)

    def get_session_model_config_value(self, session_id: str, key: str, default: Any=None) -> Any:
        """Read one key out of a session's model_config JSON (tolerant parse)."""
        session = self.get_session(session_id) or {}
        raw = session.get('model_config')
        config: Dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    config = parsed
            except (json.JSONDecodeError, TypeError):
                config = {}
        elif isinstance(raw, dict):
            config = raw
        return config.get(key, default)

    def update_session_runtime_lock(self, session_id: str, *, model: Optional[str]=None, provider: Optional[str]=None, model_options: Optional[Dict[str, Any]]=None, route_source: Optional[str]=None, confirmed: bool=False) -> None:
        """Persist a Browser / API client runtime lock without clobbering lineage markers.

        Merges ``browser_model_lock`` into the existing ``model_config`` JSON so
        ``_branched_from`` / ``_delegate_from`` survive. Nulls ``system_prompt``
        so cached ``Model:`` / ``Provider:`` footers cannot lie after a switch.
        """
        lock = {'provider': provider or '', 'model': model or '', 'model_options': model_options or {}, 'route_source': route_source or '', 'confirmed': bool(confirmed), 'updated_at': time.time()}

        def _do(conn):
            merged = self._merge_model_config_json(conn, session_id, {'browser_model_lock': lock})
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute('UPDATE sessions SET\n                   model_config = ?,\n                   model = COALESCE(?, model),\n                   system_prompt = NULL,\n                   system_prompt_hash = NULL\n                   WHERE id = ?', (merged, model, session_id))
            self._delete_unreferenced_system_prompts(conn)
        self._execute_write(_do)

    def set_session_yolo(self, session_id: str, enabled: bool) -> None:
        """Persist the per-session YOLO bypass flag into ``model_config``.

        Merges ``yolo_mode`` into the existing ``model_config`` JSON (same
        merge discipline as ``update_session_runtime_lock`` so lineage
        markers like ``_branched_from`` / ``_delegate_from`` survive). The
        CLI resume paths read this flag back so a ``/yolo ON`` toggle — or a
        ``--yolo`` launch — survives ``duck-agent --resume`` into a fresh
        process. No-op when the session row doesn't exist yet; the
        creation-time ``model_config`` carries the flag for ``--yolo``
        launches.
        """
        if not session_id:
            return

        def _do(conn):
            merged = self._merge_model_config_json(conn, session_id, {'yolo_mode': bool(enabled)})
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute('UPDATE sessions SET model_config = ? WHERE id = ?', (merged, session_id))
        self._execute_write(_do)

    @staticmethod
    def session_yolo_enabled(session_meta: Optional[Dict[str, Any]]) -> bool:
        """Read the persisted YOLO flag off a session row dict.

        Accepts the dict returned by ``get_session`` (``model_config`` is a
        JSON string) or an already-parsed dict. Returns False on any parse
        failure — resume must never enable the bypass by accident.
        """
        raw = (session_meta or {}).get('model_config')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return False
        if not isinstance(raw, dict):
            return False
        return bool(raw.get('yolo_mode'))

    def update_session_billing_route(self, session_id: str, *, provider: str, base_url: str, billing_mode: Optional[str]=None) -> None:
        """Unconditionally update the billing provider/base_url for a session.

        Unlike ``update_token_counts`` which uses ``COALESCE(billing_provider, ?)``
        (only filling in NULL), this unconditionally sets the billing fields so
        that the dashboard reflects the user's latest /model switch.

        Also nulls ``system_prompt`` so the cached snapshot (which embeds a
        stale ``Model:`` / ``Provider:`` header) is rebuilt — matching the
        behavior of ``update_session_model`` (see #48173, #48248).
        """
        self.flush_token_counts()

        def _do(conn):
            conn.execute('UPDATE sessions SET\n                   billing_provider = ?,\n                   billing_base_url = ?,\n                   billing_mode = COALESCE(?, billing_mode),\n                   system_prompt = NULL,\n                   system_prompt_hash = NULL\n                   WHERE id = ?', (provider, base_url, billing_mode, session_id))
            self._delete_unreferenced_system_prompts(conn)
        self._execute_write(_do)
    _TOKEN_DELTA_SUM_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'api_call_count')
    _TOKEN_DELTA_COST_FIELDS = ('estimated_cost_usd', 'actual_cost_usd')
    _TOKEN_DELTA_ROUTE_FIELDS = ('model', 'cost_status', 'cost_source', 'pricing_version', 'billing_provider', 'billing_base_url', 'billing_mode')

    def queue_token_counts(self, session_id: str, **kwargs) -> None:
        """Enqueue a token/cost delta for the background writer.

        Accepts the same keyword arguments as :meth:`update_token_counts`
        and applies them asynchronously with identical semantics.  Cheap
        (append + notify) — safe to call on the turn thread after every
        API call.  After close() has stopped the writer, falls back to the
        synchronous path and may raise like :meth:`update_token_counts`.
        """
        with self._token_queue_cond:
            thread = self._token_writer_thread
            writer_stopped = self._token_writer_stop and (thread is None or not thread.is_alive())
            if not writer_stopped:
                self._token_queue.append((session_id, kwargs))
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=self._token_writer_loop, name='session-db-token-writer', daemon=True)
                    self._token_writer_thread = thread
                    thread.start()
                    atexit.register(self._drain_token_queue_at_exit)
                self._token_queue_cond.notify_all()
        if writer_stopped:
            self.update_token_counts(session_id, **kwargs)

    def flush_token_counts(self, timeout: float=5.0) -> bool:
        """Block until every queued token delta has been applied.

        Returns True when the queue is fully drained, False on timeout
        (callers then read totals that are stale by the still-queued
        deltas — no worse than reading before the flush existed).
        Never raises: apply failures are logged by the writer.
        """
        if not self._token_queue and (not self._token_writer_busy):
            return True
        batch = None
        with self._token_queue_cond:
            deadline = time.monotonic() + timeout
            while self._token_queue or self._token_writer_busy:
                thread = self._token_writer_thread
                if (thread is None or not thread.is_alive()) and (not self._token_writer_busy):
                    self._token_writer_busy = True
                    batch = list(self._token_queue)
                    self._token_queue.clear()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._token_queue_cond.wait(remaining)
        if batch:
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()
        return True

    def _token_writer_loop(self) -> None:
        while True:
            with self._token_queue_cond:
                while not self._token_queue and (not self._token_writer_stop):
                    self._token_queue_cond.wait()
                if not self._token_queue:
                    return
                self._token_writer_busy = True
                batch = list(self._token_queue)
                self._token_queue.clear()
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()

    def _apply_token_batch(self, batch: List[Tuple[str, Dict[str, Any]]]) -> None:
        """Apply queued deltas in order, coalescing where safe. Never raises."""
        try:
            coalesced = self._coalesce_token_deltas(batch)
        except Exception as exc:
            logger.warning('async token accounting: coalesce failed, applying raw batch: %s', exc)
            coalesced = batch
        for session_id, kwargs in coalesced:
            try:
                self.update_token_counts(session_id, **kwargs)
            except Exception as exc:
                logger.warning('async token accounting: apply failed (session=%s): %s', session_id, exc)

    def _coalesce_token_deltas(self, batch: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, Dict[str, Any]]]:
        """Merge consecutive incremental deltas with an identical route.

        Only adjacent deltas merge, so ordering across sessions and across
        a mid-session /model switch is preserved exactly.  absolute=True
        deltas (cumulative overwrites) never merge.
        """
        groups: List[Tuple[Optional[tuple], str, Dict[str, Any]]] = []
        for session_id, kwargs in batch:
            key = None
            if not kwargs.get('absolute'):
                key = (session_id,) + tuple((kwargs.get(f) for f in self._TOKEN_DELTA_ROUTE_FIELDS))
            if groups and key is not None and (groups[-1][0] == key):
                merged = groups[-1][2]
                for f in self._TOKEN_DELTA_SUM_FIELDS:
                    merged[f] = merged.get(f, 0) + kwargs.get(f, 0)
                for f in self._TOKEN_DELTA_COST_FIELDS:
                    value = kwargs.get(f)
                    if value is not None:
                        merged[f] = (merged.get(f) or 0.0) + value
            else:
                groups.append((key, session_id, dict(kwargs)))
        return [(sid, kw) for _, sid, kw in groups]

    def _stop_token_writer(self, join_timeout: float=10.0) -> None:
        """Stop the writer thread and drain remaining deltas. Never raises."""
        with self._token_queue_cond:
            self._token_writer_stop = True
            self._token_queue_cond.notify_all()
            thread = self._token_writer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                logger.warning('async token accounting: writer did not stop within %.0fs; %d queued delta(s) not persisted', join_timeout, len(self._token_queue))
                return
        with self._token_queue_cond:
            deadline = time.monotonic() + join_timeout
            while self._token_writer_busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning('async token accounting: concurrent drain did not finish within %.0fs; %d queued delta(s) not persisted', join_timeout, len(self._token_queue))
                    return
                self._token_queue_cond.wait(remaining)
            batch = list(self._token_queue)
            if batch:
                self._token_writer_busy = True
                self._token_queue.clear()
        if batch:
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()

    def _drain_token_queue_at_exit(self) -> None:
        try:
            self._stop_token_writer()
        except Exception:
            pass

    def update_token_counts(self, session_id: str, input_tokens: int=0, output_tokens: int=0, model: str=None, cache_read_tokens: int=0, cache_write_tokens: int=0, reasoning_tokens: int=0, estimated_cost_usd: Optional[float]=None, actual_cost_usd: Optional[float]=None, cost_status: Optional[str]=None, cost_source: Optional[str]=None, pricing_version: Optional[str]=None, billing_provider: Optional[str]=None, billing_base_url: Optional[str]=None, billing_mode: Optional[str]=None, api_call_count: int=0, absolute: bool=False) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        self._insert_session_row(session_id, 'unknown', model=model)
        if absolute:
            sql = 'UPDATE sessions SET\n                   input_tokens = ?,\n                   output_tokens = ?,\n                   cache_read_tokens = ?,\n                   cache_write_tokens = ?,\n                   reasoning_tokens = ?,\n                   estimated_cost_usd = COALESCE(?, 0),\n                   actual_cost_usd = CASE\n                       WHEN ? IS NULL THEN actual_cost_usd\n                       ELSE ?\n                   END,\n                   cost_status = COALESCE(?, cost_status),\n                   cost_source = COALESCE(?, cost_source),\n                   pricing_version = COALESCE(?, pricing_version),\n                   billing_provider = COALESCE(billing_provider, ?),\n                   billing_base_url = COALESCE(billing_base_url, ?),\n                   billing_mode = COALESCE(billing_mode, ?),\n                   model = COALESCE(model, ?),\n                   api_call_count = ?\n                   WHERE id = ?'
        else:
            sql = 'UPDATE sessions SET\n                   input_tokens = input_tokens + ?,\n                   output_tokens = output_tokens + ?,\n                   cache_read_tokens = cache_read_tokens + ?,\n                   cache_write_tokens = cache_write_tokens + ?,\n                   reasoning_tokens = reasoning_tokens + ?,\n                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),\n                   actual_cost_usd = CASE\n                       WHEN ? IS NULL THEN actual_cost_usd\n                       ELSE COALESCE(actual_cost_usd, 0) + ?\n                   END,\n                   cost_status = COALESCE(?, cost_status),\n                   cost_source = COALESCE(?, cost_source),\n                   pricing_version = COALESCE(?, pricing_version),\n                   billing_provider = COALESCE(billing_provider, ?),\n                   billing_base_url = COALESCE(billing_base_url, ?),\n                   billing_mode = COALESCE(billing_mode, ?),\n                   model = COALESCE(model, ?),\n                   api_call_count = COALESCE(api_call_count, 0) + ?\n                   WHERE id = ?'
        has_accounted_usage = bool(input_tokens or output_tokens or cache_read_tokens or cache_write_tokens or reasoning_tokens or api_call_count or estimated_cost_usd or actual_cost_usd)
        params = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, actual_cost_usd, cost_status, cost_source, pricing_version, billing_provider if has_accounted_usage else None, billing_base_url if has_accounted_usage else None, billing_mode if has_accounted_usage else None, model if has_accounted_usage else None, api_call_count, session_id)
        record_model_usage = not absolute and (input_tokens or output_tokens or cache_read_tokens or cache_write_tokens or reasoning_tokens or api_call_count or estimated_cost_usd)

        def _do(conn):
            row = conn.execute('SELECT model, billing_provider, api_call_count FROM sessions WHERE id = ?', (session_id,)).fetchone()
            existing_model = row['model'] if row is not None else None
            existing_provider = row['billing_provider'] if row is not None else None
            existing_api_calls = int((row['api_call_count'] if row is not None else 0) or 0)
            first_accounted_route = existing_api_calls == 0 and has_accounted_usage and bool(model) and bool(billing_provider) and (existing_model != model or existing_provider != billing_provider)
            if first_accounted_route:
                conn.execute('UPDATE sessions\n                       SET model = ?, billing_provider = ?,\n                       billing_base_url = ?, billing_mode = ?\n                       WHERE id = ?', (model, billing_provider, billing_base_url, billing_mode, session_id))
            conn.execute(sql, params)
            if record_model_usage:
                self._record_model_usage(conn, session_id, model=model, billing_provider=billing_provider, billing_base_url=billing_base_url, billing_mode=billing_mode, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, reasoning_tokens=reasoning_tokens, estimated_cost_usd=estimated_cost_usd, actual_cost_usd=actual_cost_usd, cost_status=cost_status, cost_source=cost_source, api_call_count=api_call_count)
        self._execute_write(_do)

    def _record_model_usage(self, conn, session_id: str, *, model: Optional[str], billing_provider: Optional[str], billing_base_url: Optional[str], billing_mode: Optional[str], input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_write_tokens: int, reasoning_tokens: int, estimated_cost_usd: Optional[float], actual_cost_usd: Optional[float], cost_status: Optional[str], cost_source: Optional[str], api_call_count: int, task: str='') -> None:
        """Accumulate a per-API-call usage delta into session_model_usage.

        Runs inside the caller's write transaction (after the ``sessions``
        UPDATE) so the per-model rows stay consistent with the summary row.
        When the caller omits the model/provider (some paths only pass token
        deltas), fall back to the values already recorded on the session row —
        the same COALESCE-from-session behaviour the summary update uses.

        ``task`` distinguishes what kind of work consumed the tokens:
        ``''`` (empty) is the main agent loop; auxiliary calls record their
        task name (``vision``, ``compression``, ``title_generation``, ...)
        via :meth:`record_auxiliary_usage` (issue #23270).
        """
        row = conn.execute('SELECT model, billing_provider, billing_base_url, billing_mode FROM sessions WHERE id = ?', (session_id,)).fetchone()
        sess_model = row['model'] if row is not None else None
        sess_provider = row['billing_provider'] if row is not None else None
        sess_base_url = row['billing_base_url'] if row is not None else None
        sess_billing_mode = row['billing_mode'] if row is not None else None
        if task:
            eff_model = model or 'unknown'
            eff_provider = billing_provider or ''
            eff_base_url = billing_base_url or ''
            eff_billing_mode = billing_mode or ''
        else:
            eff_model = model or sess_model or 'unknown'
            eff_provider = billing_provider or sess_provider or ''
            eff_base_url = billing_base_url or sess_base_url or ''
            eff_billing_mode = billing_mode or sess_billing_mode or ''
        now = time.time()
        conn.execute('INSERT INTO session_model_usage (\n                   session_id, model, billing_provider, billing_base_url, billing_mode,\n                   task, api_call_count, input_tokens, output_tokens,\n                   cache_read_tokens, cache_write_tokens, reasoning_tokens,\n                   estimated_cost_usd, actual_cost_usd, cost_status, cost_source,\n                   first_seen, last_seen\n               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)\n               DO UPDATE SET\n                   api_call_count = api_call_count + excluded.api_call_count,\n                   input_tokens = input_tokens + excluded.input_tokens,\n                   output_tokens = output_tokens + excluded.output_tokens,\n                   cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,\n                   cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,\n                   reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,\n                   estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,\n                   actual_cost_usd = actual_cost_usd + excluded.actual_cost_usd,\n                   cost_status = COALESCE(excluded.cost_status, cost_status),\n                   cost_source = COALESCE(excluded.cost_source, cost_source),\n                   last_seen = excluded.last_seen', (session_id, eff_model, eff_provider, eff_base_url, eff_billing_mode, task or '', api_call_count or 0, input_tokens or 0, output_tokens or 0, cache_read_tokens or 0, cache_write_tokens or 0, reasoning_tokens or 0, float(estimated_cost_usd or 0.0), float(actual_cost_usd or 0.0), cost_status, cost_source, now, now))

    def ensure_session(self, session_id: str, source: str='unknown', model: str=None, **kwargs) -> str:
        """Ensure a session row exists (INSERT OR IGNORE). Accepts optional kwargs."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def record_auxiliary_usage(self, session_id: str, task: str, *, model: Optional[str]=None, billing_provider: Optional[str]=None, billing_base_url: Optional[str]=None, input_tokens: int=0, output_tokens: int=0, cache_read_tokens: int=0, cache_write_tokens: int=0, reasoning_tokens: int=0, estimated_cost_usd: Optional[float]=None) -> None:
        """Record an auxiliary LLM call's usage against *session_id* (issue #23270).

        Auxiliary calls (vision, compression, title_generation, web_extract,
        session_search, ...) historically discarded their usage, leaving the
        dashboard's per-model analytics blind to aux model spend. This writes
        a per-(model, provider, task) delta into ``session_model_usage`` —
        the same table the main loop's ``update_token_counts`` feeds — WITHOUT
        touching the ``sessions`` summary row. That separation is deliberate:
        the gateway overwrites session counters with absolute main-loop totals,
        so folding aux tokens into the summary row would either be clobbered
        or double-counted. Insights/analytics read the union of both.

        Best-effort by contract: callers must never fail an aux call because
        accounting failed.
        """
        if not session_id or not task:
            return
        self._insert_session_row(session_id, 'unknown')

        def _do(conn):
            self._record_model_usage(conn, session_id, model=model, billing_provider=billing_provider, billing_base_url=billing_base_url, billing_mode=None, input_tokens=input_tokens or 0, output_tokens=output_tokens or 0, cache_read_tokens=cache_read_tokens or 0, cache_write_tokens=cache_write_tokens or 0, reasoning_tokens=reasoning_tokens or 0, estimated_cost_usd=estimated_cost_usd, actual_cost_usd=None, cost_status=None, cost_source=None, api_call_count=1, task=task)
        self._execute_write(_do)

    def prune_empty_ghost_sessions(self, sessions_dir: 'Optional[Path]'=None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400

        def _do(conn):
            rows = conn.execute("\n                SELECT id FROM sessions\n                WHERE source = 'tui'\n                  AND title IS NULL\n                  AND ended_at IS NOT NULL\n                  AND started_at < ?\n                  AND NOT EXISTS (\n                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id\n                  )\n            ", (cutoff,)).fetchall()
            ids = [r[0] if isinstance(r, (tuple, list)) else r['id'] for r in rows]
            if ids:
                placeholders = ','.join('?' * len(ids))
                conn.execute(f'DELETE FROM sessions WHERE id IN ({placeholders})', ids)
                self._delete_unreferenced_system_prompts(conn)
            return ids
        removed_ids = self._execute_write(_do) or []
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended
        with reason='compression', child has messages but no end_reason/ended_at
        and api_call_count=0.  Non-destructive: preserves all messages and sets
        end_reason='orphaned_compression'.  Fix for #20001.
        """
        cutoff = time.time() - 604800

        def _do(conn):
            now = time.time()
            result = conn.execute("\n                UPDATE sessions\n                SET ended_at = ?,\n                    end_reason = 'orphaned_compression'\n                WHERE api_call_count = 0\n                  AND end_reason IS NULL\n                  AND ended_at IS NULL\n                  AND started_at < ?\n                  AND parent_session_id IS NOT NULL\n                  AND EXISTS (\n                      SELECT 1 FROM sessions p\n                      WHERE p.id = sessions.parent_session_id\n                        AND p.end_reason = 'compression'\n                        AND p.ended_at IS NOT NULL\n                  )\n                  AND EXISTS (\n                      SELECT 1 FROM messages m\n                      WHERE m.session_id = sessions.id\n                  )\n                ", (now, cutoff))
            return result.rowcount
        return self._execute_write(_do) or 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        self.flush_token_counts()
        with self._read_ctx() as conn:
            cursor = conn.execute('SELECT s.*, COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved FROM sessions s LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash WHERE s.id = ?', (session_id,))
            row = cursor.fetchone()
        return self._session_row_dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact['id']
        escaped = _escape_like(session_id_or_prefix)
        with self._lock:
            cursor = self._conn.execute("SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2", (f'{escaped}%',))
            matches = [row['id'] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None
        title = _sanitize_surrogates(title)
        cleaned = re.sub('[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]', '', title)
        cleaned = re.sub('[\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u2069\\ufeff\\ufffc\\ufff9-\\ufffb]', '', cleaned)
        cleaned = re.sub('\\s+', ' ', cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(f'Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})')
        return cleaned

    def _is_compression_ancestor(self, conn, *, ancestor_id: str, descendant_id: str) -> bool:
        """Return True if *ancestor_id* is a compression predecessor of
        *descendant_id* (walking parent links up the continuation chain).

        The continuation edge is the canonical one shared with
        :func:`_ephemeral_child_sql` / :meth:`set_session_archived`
        (``_COMPRESSION_CHILD_SQL``): a parent → child edge counts only when the
        parent ended with ``end_reason = 'compression'`` and the child started
        at or after the parent's ``ended_at``, which distinguishes continuations
        from delegate subagents / branch children that also carry a
        ``parent_session_id``. Expressed as a single recursive CTE rather than a
        per-hop Python walk so the edge definition lives in exactly one place.
        """
        if not ancestor_id or not descendant_id or ancestor_id == descendant_id:
            return False
        edge = _COMPRESSION_CHILD_SQL.format(a='child')
        row = conn.execute(f'\n            WITH RECURSIVE ancestors(id) AS (\n                SELECT ?\n                UNION\n                SELECT parent.id\n                FROM ancestors a\n                JOIN sessions child ON child.id = a.id\n                JOIN sessions parent ON parent.id = child.parent_session_id\n                WHERE {edge}\n            )\n            SELECT 1 FROM ancestors WHERE id = ? AND id != ? LIMIT 1\n            ', (descendant_id, ancestor_id, descendant_id)).fetchone()
        return row is not None

    def _set_session_title(self, session_id: str, title: str, *, only_if_empty: bool) -> bool:
        title = self.sanitize_title(title)

        def _do(conn):
            if only_if_empty:
                current = conn.execute('SELECT title FROM sessions WHERE id = ?', (session_id,)).fetchone()
                if current is None or current['title'] is not None:
                    return 0
            if title:
                cursor = conn.execute('SELECT id FROM sessions WHERE title = ? AND id != ?', (title, session_id))
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict['id']
                    if self._is_compression_ancestor(conn, ancestor_id=conflict_id, descendant_id=session_id):
                        conn.execute('UPDATE sessions SET title = NULL WHERE id = ?', (conflict_id,))
                    else:
                        raise ValueError(f"Title '{title}' is already in use by session {conflict_id}")
            predicate = ' AND title IS NULL' if only_if_empty else ''
            cursor = conn.execute(f'UPDATE sessions SET title = ? WHERE id = ?{predicate}', (title, session_id))
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        return self._set_session_title(session_id, title, only_if_empty=False)

    def set_auto_title_if_empty(self, session_id: str, title: str) -> bool:
        """Set an auto-generated title only when the current title is NULL.

        The predicate and write run in one transaction so a concurrent manual
        rename cannot be overwritten. Validation and uniqueness behavior match
        :meth:`set_session_title`.
        """
        return self._set_session_title(session_id, title, only_if_empty=True)

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute('SELECT title FROM sessions WHERE id = ?', (session_id,))
            row = cursor.fetchone()
        return row['title'] if row else None

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        """Archive or unarchive a session.

        Archived sessions are hidden from the default session list but keep all
        their messages — this is a soft hide, not a delete. For compression
        chains, archive the whole logical conversation. Desktop lists compression
        roots projected forward to their latest continuation; updating only the
        displayed tip lets the still-unarchived root resurrect it on refresh.
        Returns True when at least one row was updated.
        """

        def _do(conn):
            cursor = conn.execute("\n                WITH RECURSIVE\n                  ancestors(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT parent.id\n                    FROM ancestors a\n                    JOIN sessions child ON child.id = a.id\n                    JOIN sessions parent ON parent.id = child.parent_session_id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  descendants(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT child.id\n                    FROM descendants d\n                    JOIN sessions parent ON parent.id = d.id\n                    JOIN sessions child ON child.parent_session_id = parent.id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  lineage(id) AS (\n                    SELECT id FROM ancestors\n                    UNION\n                    SELECT id FROM descendants\n                  )\n                UPDATE sessions\n                SET archived = ?\n                WHERE id IN (SELECT id FROM lineage)\n                ", (session_id, session_id, 1 if archived else 0))
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute('SELECT changes()').fetchone()[0]
            return rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        """Pin or unpin a session (and its whole compression lineage).

        ``pinned`` is a durable "keep" flag: pinned sessions are exempt from
        the ``sessions.auto_archive`` stale sweep (see
        :meth:`archive_stale_sessions`). Desktop is the current writer — its
        sidebar pins mirror here so a backend/other-surface sweep honours
        them. Like :meth:`set_session_archived` the whole compression chain is
        flipped as a unit, so pinning the surfaced tip protects the root (and
        vice-versa) no matter which id the caller holds. Returns True when at
        least one row changed.
        """

        def _do(conn):
            cursor = conn.execute("\n                WITH RECURSIVE\n                  ancestors(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT parent.id\n                    FROM ancestors a\n                    JOIN sessions child ON child.id = a.id\n                    JOIN sessions parent ON parent.id = child.parent_session_id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  descendants(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT child.id\n                    FROM descendants d\n                    JOIN sessions parent ON parent.id = d.id\n                    JOIN sessions child ON child.parent_session_id = parent.id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  lineage(id) AS (\n                    SELECT id FROM ancestors\n                    UNION\n                    SELECT id FROM descendants\n                  )\n                UPDATE sessions\n                SET pinned = ?\n                WHERE id IN (SELECT id FROM lineage)\n                ", (session_id, session_id, 1 if pinned else 0))
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute('SELECT changes()').fetchone()[0]
            return rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_read(self, session_id: str, read: bool=True) -> bool:
        """Mark a session read or unread (and its whole compression lineage).

        Read state is a watermark, not a flag: ``last_read_at`` records when
        the conversation was last read, and it counts as unread when activity
        postdates that watermark (the derived ``unread`` key on
        :meth:`list_sessions_rich` rows). New messages therefore flip a read
        conversation back to unread without any write on the message path.
        Three states:

        * NULL — never tracked (every pre-feature row): treated as read, so
          shipping the column doesn't badge a user's entire history at once.
        * 0 — explicitly marked unread: any activity postdates it.
        * timestamp — read up to that moment.

        Like :meth:`set_session_archived` / :meth:`set_session_pinned`, the
        whole compression chain is stamped as a unit, so reading the surfaced
        tip clears the root (and vice-versa) no matter which id the caller
        holds. Returns True when at least one row changed.
        """

        def _do(conn):
            cursor = conn.execute("\n                WITH RECURSIVE\n                  ancestors(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT parent.id\n                    FROM ancestors a\n                    JOIN sessions child ON child.id = a.id\n                    JOIN sessions parent ON parent.id = child.parent_session_id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  descendants(id) AS (\n                    SELECT ?\n                    UNION\n                    SELECT child.id\n                    FROM descendants d\n                    JOIN sessions parent ON parent.id = d.id\n                    JOIN sessions child ON child.parent_session_id = parent.id\n                    WHERE parent.end_reason = 'compression'\n                  ),\n                  lineage(id) AS (\n                    SELECT id FROM ancestors\n                    UNION\n                    SELECT id FROM descendants\n                  )\n                UPDATE sessions\n                SET last_read_at = ?\n                WHERE id IN (SELECT id FROM lineage)\n                ", (session_id, session_id, time.time() if read else 0.0))
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute('SELECT changes()').fetchone()[0]
            return rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    @staticmethod
    def session_unread(session_row: Dict[str, Any]) -> bool:
        """Derive unread from a session row's watermark and activity.

        Shared by ``list_sessions_rich`` and any future surface that holds a
        row (or projected row) with ``last_read_at`` and ``last_active``.
        NULL watermark = never tracked = read.
        """
        last_read = session_row.get('last_read_at')
        if last_read is None:
            return False
        last_active = session_row.get('last_active') or session_row.get('started_at')
        return float(last_active or 0) > float(last_read)

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._read_ctx() as conn:
            cursor = conn.execute('SELECT s.*, COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved FROM sessions s LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash WHERE s.title = ?', (title,))
            row = cursor.fetchone()
        return self._session_row_dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        exact = self.get_session_by_title(title)
        escaped = _escape_like(title)
        with self._read_ctx() as conn:
            cursor = conn.execute("SELECT id, title, started_at FROM sessions WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC", (f'{escaped} #%',))
            numbered = cursor.fetchall()
        if numbered:
            return numbered[0]['id']
        elif exact:
            return exact['id']
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        match = re.match('^(.*?) #(\\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title
        escaped = _escape_like(base)
        with self._lock:
            cursor = self._conn.execute("SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'", (base, f'{escaped} #%'))
            existing = [row['title'] for row in cursor.fetchall()]
        if not existing:
            return base
        max_num = 1
        for t in existing:
            m = re.match('^.* #(\\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))
        return f'{base} #{max_num + 1}'

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child of a session whose
        ``end_reason = 'compression'``.  Older builds tried to distinguish
        continuations from branches/subagents by requiring
        ``child.started_at >= parent.ended_at``.  That ordering is too brittle:
        gateway + compression races can insert the real continuation row before
        the parent row's ``ended_at`` is written, while a stale websocket later
        creates/reuses a sibling that *does* satisfy the timestamp test.  The
        visible symptom is brutal: desktop resume follows the stale sibling and
        the user's latest messages look "lost" even though they are persisted in
        the real continuation chain.

        Instead, only follow children of compression-ended parents, exclude
        explicit branch/delegate/tool children, and prefer children that are
        themselves continuing the compression chain (``end_reason='compression'``)
        or still live over stale closed siblings such as ``ws_orphan_reap``.
        Returns the latest continuation tip, or the input id when no
        continuation exists.
        """
        current = session_id
        seen = {current} if current else set()
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(f"\n                    SELECT child.id\n                    FROM sessions parent\n                    JOIN sessions child ON child.parent_session_id = parent.id\n                    WHERE parent.id = ?\n                      AND parent.end_reason = 'compression'\n                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL\n                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL\n                      AND COALESCE(child.source, '') != 'tool'\n                    ORDER BY\n                      CASE\n                        WHEN child.end_reason = 'compression' THEN 0\n                        WHEN child.ended_at IS NULL THEN 1\n                        ELSE 2\n                      END,\n                      {_sql_session_last_active('child')} DESC,\n                      child.started_at DESC,\n                      child.id DESC\n                    LIMIT 1\n                    ", (current,))
                row = cursor.fetchone()
            if row is None:
                return current
            child_id = row['id']
            if not child_id or child_id in seen:
                return current
            seen.add(child_id)
            current = child_id
        return current
    _SESSION_COMPACT_EXCLUDED = frozenset({'system_prompt', 'system_prompt_hash'})
    _session_compact_cols_sql: Optional[str] = None

    def list_sessions_rich(self, source: str=None, sources: List[str]=None, exclude_sources: List[str]=None, cwd_prefix: str=None, limit: int=20, offset: int=0, include_children: bool=False, min_message_count: int=0, project_compression_tips: bool=True, order_by_last_active: bool=False, include_archived: bool=False, archived_only: bool=False, id_query: str=None, search_query: str=None, compact_rows: bool=False, include_pinned: bool=False, session_key: str=None) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (freshest of last_activity_at heartbeat and latest
        message timestamp, else started_at).

        Uses a single query with correlated subqueries instead of N+2 queries.

        By default, child sessions (subagent runs, compression continuations)
        are excluded.  Pass ``include_children=True`` to include them.

        With ``project_compression_tips=True`` (default), sessions that are
        roots of compression chains are projected forward to their latest
        continuation — one logical conversation = one list entry, showing the
        live continuation's id/message_count/title/last_active. This prevents
        compressed continuations from being invisible to users while keeping
        delegate subagents and branches hidden. Pass ``False`` to return the
        raw root rows (useful for admin/debug UIs).

        Pass ``order_by_last_active=True`` to sort by most-recent activity
        instead of original conversation start time. For compression chains,
        the "most-recent activity" is taken from the live tip (not the root),
        so an old conversation that was compressed and continued recently
        surfaces in the correct slot. Ordering is computed at SQL level via
        a recursive CTE that walks compression-continuation edges, so LIMIT
        and OFFSET still apply efficiently.

        ``search_query`` matches case-insensitive substrings against each
        surfaced row's title and id (and, like ``id_query``, every title/id in
        its forward compression chain). A punctuation-stripped variant is also
        matched so e.g. ``an94`` finds ``AN-94``. Only honored in the
        ``order_by_last_active`` path.

        Pass ``compact_rows=True`` for dashboard and picker callers that only
        need lightweight metadata. This omits the ``system_prompt`` blob from
        the SELECT so SQLite never copies it out of the B-tree page — a
        significant I/O saving on large databases where the blob routinely
        runs to tens of kilobytes per row.

        Pass ``include_pinned=True`` to back-fill any conversation carrying the
        durable ``pinned`` flag that the LIMIT/OFFSET window left out. A pin is
        a "this must always be reachable" statement, so a pinned conversation
        aging past the requested page is a bug, not a paging outcome — the
        desktop sidebar would render an empty Pinned section. Back-filled rows
        obey the same filters (source, archived, min_message_count) as the
        page: an archived or filtered-out conversation stays out.

        Pass ``session_key`` to restrict results to one stable gateway
        conversation scope (DM, group, channel, or thread, including the
        configured per-user isolation policy).
        """
        self.flush_token_counts()
        where_clauses = []
        params = []
        if not include_children:
            where_clauses.append(_LISTABLE_CHILD_SQL)
            where_clauses.append(f"{_delegate_from_json('s.model_config')} IS NULL")
        include_sources = [source] if source else list(sources or [])
        if include_sources:
            placeholders = ','.join(('?' for _ in include_sources))
            where_clauses.append(f's.source IN ({placeholders})')
            params.extend(include_sources)
        if session_key:
            where_clauses.append('s.session_key = ?')
            params.append(session_key)
        if exclude_sources:
            placeholders = ','.join(('?' for _ in exclude_sources))
            where_clauses.append(f's.source NOT IN ({placeholders})')
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append('s.message_count >= ?')
            params.append(min_message_count)
        if archived_only:
            where_clauses.append('s.archived = 1')
        elif not include_archived:
            where_clauses.append('s.archived = 0')
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        base_where_params = list(params)
        prompt_select = '' if compact_rows else ', COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved'
        prompt_join = '' if compact_rows else 'LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash'
        id_needle = (id_query or '').strip().lower()
        search_needle = (search_query or '').strip().lower()
        if order_by_last_active:
            outer_where = where_sql
            id_params: List[Any] = []
            filter_clauses: List[str] = []

            def _like_pattern(needle: str) -> str:
                return f'%{_escape_like(needle)}%'
            if id_needle:
                filter_clauses.append("EXISTS (SELECT 1 FROM chain cq        WHERE cq.root_id = s.id          AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')")
                id_params.append(_like_pattern(id_needle))
            if search_needle:
                compact_needle = re.sub('[\\W_]+', '', search_needle)
                compact_sql = "REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE({0}, '')), '-', ''), '_', ''), '.', ''), ' ', '')"
                search_clause = "EXISTS (SELECT 1 FROM chain cq JOIN sessions cs ON cs.id = cq.cur_id WHERE cq.root_id = s.id AND (LOWER(COALESCE(cs.title, '')) LIKE ? ESCAPE '\\' OR LOWER(cq.cur_id) LIKE ? ESCAPE '\\'"
                id_params.extend([_like_pattern(search_needle)] * 2)
                if compact_needle:
                    search_clause += f" OR {compact_sql.format('cs.title')} LIKE ? ESCAPE '\\'"
                    id_params.append(_like_pattern(compact_needle))
                filter_clauses.append(search_clause + '))')
            if filter_clauses:
                combined = ' AND '.join(filter_clauses)
                outer_where = f'{where_sql} AND {combined}' if where_sql else f'WHERE {combined}'
            _sel = self._compact_session_cols() if compact_rows else 's.*'
            query = f"\n                WITH RECURSIVE chain(root_id, cur_id) AS (\n                    SELECT s.id, s.id FROM sessions s {where_sql}\n                    UNION ALL\n                    SELECT c.root_id, child.id\n                    FROM chain c\n                    JOIN sessions parent ON parent.id = c.cur_id\n                    JOIN sessions child ON child.parent_session_id = c.cur_id\n                    WHERE parent.end_reason = 'compression'\n                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL\n                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL\n                      AND COALESCE(child.source, '') != 'tool'\n                ),\n                chain_max AS (\n                    SELECT\n                        root_id,\n                        MAX({_sql_session_last_active_by_id('cur_id')}) AS effective_last_active\n                    FROM chain\n                    GROUP BY root_id\n                )\n                SELECT {_sel}{prompt_select},\n                    COALESCE(\n                        (SELECT {_PREVIEW_RAW_SELECT}\n                         FROM messages m\n                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL\n                         ORDER BY m.timestamp, m.id LIMIT 1),\n                        ''\n                    ) AS _preview_raw,\n                    {_sql_session_last_active('s')} AS last_active,\n                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active\n                FROM sessions s\n                LEFT JOIN chain_max cm ON cm.root_id = s.id\n                {prompt_join}\n                {outer_where}\n                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC\n                LIMIT ? OFFSET ?\n            "
            params = params + params + id_params + [limit, offset]
        else:
            _sel = self._compact_session_cols() if compact_rows else 's.*'
            query = f"\n                SELECT {_sel}{prompt_select},\n                    COALESCE(\n                        (SELECT {_PREVIEW_RAW_SELECT}\n                         FROM messages m\n                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL\n                         ORDER BY m.timestamp, m.id LIMIT 1),\n                        ''\n                    ) AS _preview_raw,\n                    {_sql_session_last_active('s')} AS last_active\n                FROM sessions s\n                {prompt_join}\n                {where_sql}\n                ORDER BY s.started_at DESC\n                LIMIT ? OFFSET ?\n            "
            params.extend([limit, offset])
        with self._read_ctx() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = self._session_row_dict(row)
            s['preview'] = _shape_preview(s.pop('_preview_raw', ''))
            s.pop('_effective_last_active', None)
            sessions.append(s)
        if include_pinned:
            seen_ids = {s['id'] for s in sessions}
            pinned_where = f'{where_sql} AND s.pinned = 1' if where_sql else 'WHERE s.pinned = 1'
            _sel = self._compact_session_cols() if compact_rows else 's.*'
            pinned_query = f"\n                SELECT {_sel}{prompt_select},\n                    COALESCE(\n                        (SELECT {_PREVIEW_RAW_SELECT}\n                         FROM messages m\n                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL\n                         ORDER BY m.timestamp, m.id LIMIT 1),\n                        ''\n                    ) AS _preview_raw,\n                    COALESCE(\n                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),\n                        s.started_at\n                    ) AS last_active\n                FROM sessions s\n                {prompt_join}\n                {pinned_where}\n                ORDER BY s.started_at DESC\n            "
            with self._read_ctx() as conn:
                pinned_cursor = conn.execute(pinned_query, base_where_params)
                pinned_rows = pinned_cursor.fetchall()
            for row in pinned_rows:
                s = self._session_row_dict(row)
                if s['id'] in seen_ids:
                    continue
                s['preview'] = _shape_preview(s.pop('_preview_raw', ''))
                seen_ids.add(s['id'])
                sessions.append(s)
        if project_compression_tips and (not include_children):
            tip_ids_by_root: Dict[str, str] = {}
            for s in sessions:
                if s.get('end_reason') != 'compression':
                    continue
                tip_id = self.get_compression_tip(s['id'])
                if tip_id != s['id']:
                    tip_ids_by_root[s['id']] = tip_id
            tip_rows = self._get_session_rich_rows_batch(set(tip_ids_by_root.values()), compact_rows=compact_rows) if tip_ids_by_root else {}
            projected = []
            for s in sessions:
                tip_id = tip_ids_by_root.get(s['id'])
                tip_row = tip_rows.get(tip_id) if tip_id else None
                if not tip_row:
                    projected.append(s)
                    continue
                merged = dict(s)
                for key in ('id', 'ended_at', 'end_reason', 'message_count', 'tool_call_count', 'title', 'last_active', 'preview', 'model', 'system_prompt', 'cwd', 'git_branch', 'git_repo_root'):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged['_lineage_root_id'] = s['id']
                projected.append(merged)
            sessions = projected
        for s in sessions:
            s['unread'] = self.session_unread(s)
        return sessions
    _CONTENT_JSON_PREFIX = '\x00json:'

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize structured (list/dict) message content for sqlite.

        sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``, and ``None``
        to query parameters. Multimodal messages have ``content`` as a list of
        parts (``[{"type": "text", ...}, {"type": "image_url", ...}]``), which
        raises ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` when bound directly.

        Returns the value unchanged when it's already a safe scalar, or a
        sentinel-prefixed JSON string for lists/dicts. Paired with
        :meth:`_decode_content` on read.
        """
        if isinstance(content, str):
            return _sanitize_surrogates(content)
        if content is None or isinstance(content, (bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            return _sanitize_surrogates(str(content))

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning('Failed to decode JSON-encoded message content; returning raw string')
                return content
        return content

    @staticmethod
    def _encode_display_metadata(display_metadata: Any) -> Optional[str]:
        """Serialize ``display_metadata`` for its TEXT column without double-encoding.

        Import/replace paths can hand us an already-serialized JSON string (the
        same hazard ``tool_calls`` guards against above). ``json.dumps`` on that
        string would store a quoted JSON string, and the single ``json.loads``
        on read then yields a ``str`` instead of a dict.
        """
        if not display_metadata:
            return None
        if isinstance(display_metadata, str):
            try:
                parsed = json.loads(display_metadata)
            except (json.JSONDecodeError, TypeError):
                logger.warning('Ignoring non-JSON display metadata on write')
                return None
            if not isinstance(parsed, dict):
                logger.warning('Ignoring non-object display metadata on write')
                return None
            return json.dumps(parsed)
        if isinstance(display_metadata, dict):
            return json.dumps(display_metadata)
        logger.warning('Ignoring unexpected display metadata type on write: %s', type(display_metadata).__name__)
        return None

    def _check_transcript_write_guards(self, conn, session_id: str, compression_lock_holder: Optional[str]) -> None:
        """Transcript-append admission checks, run INSIDE the write txn.

        Shared by :meth:`append_message` and :meth:`append_messages_batch` so
        the two writers can never diverge on these correctness invariants
        (this guard has already needed targeted fixes — see the #74478
        patience note below).
        """
        active_lock = conn.execute('SELECT holder FROM compression_locks WHERE session_id = ? AND expires_at > ?', (session_id, time.time())).fetchone()
        if active_lock is not None and active_lock['holder'] != compression_lock_holder:
            raise SessionCompressionInProgressError(f'Session {session_id!r} is being compressed by another writer')
        session = conn.execute('SELECT ended_at, end_reason FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session is not None and session['ended_at'] is not None and (session['end_reason'] == 'compression'):
            raise CompressionSessionClosedError(session_id)

    @staticmethod
    def _decode_display_metadata(raw: Any) -> Optional[Dict[str, Any]]:
        """Decode a ``display_metadata`` column into the dict every reader expects.

        Every message read path must go through this. Returning the raw TEXT
        instead reaches the desktop as a string, where ``'task_count' in meta``
        throws and fails the whole resume. Rows written before the encode guard
        landed are double-encoded, so unwrap a second layer when we find one.
        """
        if raw is None:
            return None
        try:
            meta = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(meta, str):
                meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            logger.warning('Ignoring invalid display metadata on message row')
            return None
        if not isinstance(meta, dict):
            logger.warning('Ignoring non-object display metadata on message row')
            return None
        return meta

    def append_message(self, session_id: str, role: str, content: str=None, tool_name: str=None, tool_calls: Any=None, tool_call_id: str=None, token_count: int=None, finish_reason: str=None, reasoning: str=None, reasoning_content: str=None, reasoning_details: Any=None, codex_reasoning_items: Any=None, codex_message_items: Any=None, platform_message_id: str=None, observed: bool=False, effect_disposition: Optional[str]=None, timestamp: Any=None, api_content: Optional[str]=None, display_kind: Optional[str]=None, display_metadata: Optional[Dict[str, Any]]=None, compression_lock_holder: Optional[str]=None) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).

        ``platform_message_id`` is the external messaging platform's own
        message ID (e.g. Telegram update_id, Yuanbao msg_id).  It is
        independent of the SQLite autoincrement primary key and is used by
        platform-specific flows like yuanbao's recall guard to redact a
        message by its platform-side identifier.

        ``api_content`` is the exact content string sent to the API for this
        message when it differs from ``content`` (ephemeral memory/plugin
        injections, persist overrides).  It is a byte-fidelity sidecar for
        prompt-cache-stable replay — stored as sent, except lone surrogates
        (which sqlite3 cannot bind and which the conversation loop scrubs
        from every outgoing payload anyway, so the scrubbed form IS the
        wire bytes).
        """
        display_metadata_json = self._encode_display_metadata(display_metadata)
        reasoning_details_json = json.dumps(reasoning_details) if reasoning_details else None
        codex_items_json = json.dumps(codex_reasoning_items) if codex_reasoning_items else None
        codex_message_items_json = json.dumps(codex_message_items) if codex_message_items else None
        if isinstance(tool_calls, str):
            try:
                tool_calls = json.loads(tool_calls)
            except (json.JSONDecodeError, TypeError):
                tool_calls = []
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        stored_content = self._encode_content(content)
        message_timestamp = time.time()
        if timestamp is not None:
            try:
                if hasattr(timestamp, 'timestamp'):
                    message_timestamp = float(timestamp.timestamp())
                else:
                    message_timestamp = float(timestamp)
            except (TypeError, ValueError):
                logger.debug('Ignoring invalid explicit message timestamp: %r', timestamp)
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            self._check_transcript_write_guards(conn, session_id, compression_lock_holder)
            cursor = conn.execute('INSERT INTO messages (session_id, role, content, tool_call_id,\n                   tool_calls, tool_name, effect_disposition, timestamp, token_count, finish_reason,\n                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,\n                   codex_message_items, platform_message_id, observed, active, api_content, display_kind, display_metadata)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (session_id, role, stored_content, tool_call_id, tool_calls_json, _scrub_surrogates(tool_name), effect_disposition, message_timestamp, token_count, finish_reason, _scrub_surrogates(reasoning), _scrub_surrogates(reasoning_content), reasoning_details_json, codex_items_json, codex_message_items_json, platform_message_id, 1 if observed else 0, 1, _scrub_surrogates(api_content) if isinstance(api_content, str) else None, _scrub_surrogates(display_kind) if isinstance(display_kind, str) else None, display_metadata_json))
            msg_id = cursor.lastrowid
            if num_tool_calls > 0:
                conn.execute('UPDATE sessions SET message_count = message_count + 1,\n                       tool_call_count = tool_call_count + ? WHERE id = ?', (num_tool_calls, session_id))
            else:
                conn.execute('UPDATE sessions SET message_count = message_count + 1 WHERE id = ?', (session_id,))
            return msg_id
        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def append_messages_batch(self, session_id: str, messages: List[Dict[str, Any]], compression_lock_holder: Optional[str]=None, chunk_rows: Optional[int]=None) -> int:
        """Append multiple messages atomically in ONE write transaction.

        ``messages`` is a list of dicts in the same shape
        :meth:`_insert_message_rows` already consumes for replace/compact/
        import (role, content, tool_name, tool_calls, tool_call_id,
        finish_reason, reasoning*, codex_*, timestamp, api_content,
        display_kind, display_metadata, ...). Reusing that helper keeps ONE
        row-serialization path for every multi-row writer.

        A turn-boundary flush writes the whole turn (user + assistant + tool
        rows, typically 3-8 messages) as one BEGIN IMMEDIATE / commit pair
        instead of one transaction (and, off WAL, one fsync) per row.

        Atomicity contract: all rows land or none do (the caller re-flushes
        unstamped messages on the next attempt). The same admission guards
        as :meth:`append_message` run once for the batch — same session,
        same instant.

        ``chunk_rows`` bounds the transaction size for LARGE copies (branch
        seeds can be thousands of rows; measured: 10k rows ≈ 2.4s inside one
        BEGIN IMMEDIATE because the FTS triggers run per row, which would
        monopolize the write lock and starve concurrent writers). When set,
        the batch commits in chunks of at most that many rows — same
        recovery semantics as the old per-row loops (a mid-copy failure
        leaves a partial seed), just with bounded lock holds. A turn flush
        never needs it. Returns the inserted row count.
        """
        if not messages:
            return 0
        if chunk_rows is not None and len(messages) > chunk_rows:
            inserted_total = 0
            for start in range(0, len(messages), chunk_rows):
                inserted_total += self.append_messages_batch(session_id, messages[start:start + chunk_rows], compression_lock_holder=compression_lock_holder)
            return inserted_total

        def _do(conn):
            self._check_transcript_write_guards(conn, session_id, compression_lock_holder)
            inserted, tool_calls_total = self._insert_message_rows(conn, session_id, messages)
            if tool_calls_total > 0:
                conn.execute('UPDATE sessions SET message_count = message_count + ?,\n                       tool_call_count = tool_call_count + ? WHERE id = ?', (inserted, tool_calls_total, session_id))
            else:
                conn.execute('UPDATE sessions SET message_count = message_count + ? WHERE id = ?', (inserted, session_id))
            return inserted
        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def set_latest_matching_message_display_kind(self, session_id: str, *, role: str, content: str, display_kind: str, display_metadata: Optional[Dict[str, Any]]=None) -> bool:
        """Stamp presentation metadata on this turn's freshly persisted row.

        The model still receives ``role`` and ``content`` unchanged. Gateway and
        CLI synthetic inputs call this immediately after their serial turn has
        flushed, preserving producer provenance without classifying by content
        during transcript rendering.
        """
        if not session_id or not content or (not display_kind):
            return False

        def _do(conn):
            row = conn.execute('SELECT id FROM messages WHERE session_id = ? AND role = ? AND content = ? AND active = 1 ORDER BY id DESC LIMIT 1', (session_id, role, self._encode_content(content))).fetchone()
            if row is None:
                return False
            conn.execute('UPDATE messages SET display_kind = ?, display_metadata = ? WHERE id = ?', (_scrub_surrogates(display_kind), self._encode_display_metadata(display_metadata), row[0]))
            return True
        return bool(self._execute_write(_do))
    REACTIONS_METADATA_KEY = 'reactions'

    def set_message_reaction(self, session_id: str, message_row_id: int, emoji: Optional[str], *, author: str='user') -> Optional[List[Dict[str, Any]]]:
        """Set (or with ``emoji=None`` clear) *author*'s reaction on one message.

        iOS Tapback semantics: one reaction per author per message. Re-sending
        the same emoji clears it, a different emoji replaces it. Returns the
        message's full reaction list after the write, or ``None`` when the row
        doesn't exist or isn't part of *session_id*.
        """
        if not session_id or message_row_id is None:
            return None

        def _do(conn):
            row = conn.execute('SELECT display_metadata FROM messages WHERE id = ? AND session_id = ?', (message_row_id, session_id)).fetchone()
            if row is None:
                return None
            meta = self._decode_display_metadata(row[0]) or {}
            existing = meta.get(self.REACTIONS_METADATA_KEY)
            reactions = [r for r in (existing if isinstance(existing, list) else []) if isinstance(r, dict) and r.get('author') != author]
            previous = next((r for r in (existing if isinstance(existing, list) else []) if isinstance(r, dict) and r.get('author') == author), None)
            toggling_off = emoji is not None and previous is not None and (previous.get('emoji') == emoji)
            if emoji and (not toggling_off):
                reactions.append({'emoji': _scrub_surrogates(emoji), 'author': author, 'at': time.time()})
            if reactions:
                meta[self.REACTIONS_METADATA_KEY] = reactions
            else:
                meta.pop(self.REACTIONS_METADATA_KEY, None)
            conn.execute('UPDATE messages SET display_metadata = ? WHERE id = ?', (self._encode_display_metadata(meta) if meta else None, message_row_id))
            return reactions
        return self._execute_write(_do)

    def get_message_reactions(self, session_id: str, message_row_id: int) -> List[Dict[str, Any]]:
        """Return the reaction list persisted on one message row (never ``None``)."""
        if not session_id or message_row_id is None:
            return []
        with self._lock:
            row = self._conn.execute('SELECT display_metadata FROM messages WHERE id = ? AND session_id = ?', (message_row_id, session_id)).fetchone()
        if row is None:
            return []
        meta = self._decode_display_metadata(row[0]) or {}
        reactions = meta.get(self.REACTIONS_METADATA_KEY)
        return [r for r in reactions if isinstance(r, dict)] if isinstance(reactions, list) else []

    def take_unseen_reactions(self, session_id: str, *, author: str='user') -> List[Dict[str, Any]]:
        """Return *author*'s not-yet-surfaced reactions and mark them seen.

        Powers the cache-safe model-context path: reactions are announced on the
        NEXT user turn (never by rewriting the message that was reacted to), and
        the ``seen`` stamp guarantees each one is announced exactly once.
        """
        if not session_id:
            return []

        def _do(conn):
            rows = conn.execute('SELECT id, role, content, display_metadata FROM messages WHERE session_id = ? AND active = 1 AND display_metadata IS NOT NULL ORDER BY id', (session_id,)).fetchall()
            pending = []
            for row in rows:
                meta = self._decode_display_metadata(row['display_metadata'])
                if not meta:
                    continue
                reactions = meta.get(self.REACTIONS_METADATA_KEY)
                if not isinstance(reactions, list):
                    continue
                changed = False
                for reaction in reactions:
                    if not isinstance(reaction, dict) or reaction.get('author') != author or reaction.get('seen'):
                        continue
                    reaction['seen'] = True
                    changed = True
                    content = self._decode_content(row['content'])
                    pending.append({'row_id': row['id'], 'role': row['role'], 'emoji': reaction.get('emoji') or '', 'text': content if isinstance(content, str) else ''})
                if changed:
                    conn.execute('UPDATE messages SET display_metadata = ? WHERE id = ?', (self._encode_display_metadata(meta), row['id']))
            return pending
        return self._execute_write(_do) or []

    def latest_message_row_id(self, session_id: str, *, role: str='user', offset: int=0, require_text: bool=True) -> Optional[int]:
        """Row id of the most recent active message with *role*, or ``None``.

        Two callers, same need — "the message I mean, without an id": the agent
        defaulting to the turn that triggered it, and the desktop reacting to a
        live message that hasn't round-tripped through a resume yet.
        ``offset`` steps to earlier turns (1 = the one before the latest) so a
        reaction can land retroactively — "two messages ago" is how the caller
        thinks about it.

        ``require_text`` (default) skips rows with no plain-text content —
        tool-call-only assistant turns and attachment stubs don't render as
        bubbles, so "the latest message" as a HUMAN means it must never
        resolve to one (a reaction landing on an invisible row looks dropped,
        and its annotation quotes an empty string).
        """
        if not session_id or role not in {'user', 'assistant'} or offset < 0:
            return None
        text_filter = "AND content IS NOT NULL AND TRIM(content) != '' " if require_text else ''
        with self._lock:
            row = self._conn.execute(f'SELECT id FROM messages WHERE session_id = ? AND role = ? AND active = 1 {text_filter}ORDER BY id DESC LIMIT 1 OFFSET ?', (session_id, role, int(offset))).fetchone()
        return row[0] if row else None

    def latest_user_message_row_id(self, session_id: str) -> Optional[int]:
        """Row id of the most recent active user message, or ``None``.

        The agent's default reaction target: "the message that triggered me",
        so the model never has to thread row ids through a tool call (mirrors
        the photon adapter's ``_record_last_inbound``).
        """
        return self.latest_message_row_id(session_id, role='user')

    def get_message_role(self, session_id: str, row_id: int) -> Optional[str]:
        """Role of the active message at *row_id* in *session_id*, or ``None``.

        Lets a reaction event carry the target's role so a renderer can match
        a live message that doesn't know its durable row id yet.
        """
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute('SELECT role FROM messages WHERE id = ? AND session_id = ? AND active = 1', (int(row_id), session_id)).fetchone()
        return row[0] if row else None

    def _insert_message_rows(self, conn, session_id: str, messages: List[Dict[str, Any]]) -> tuple[int, int]:
        """Insert *messages* as fresh active rows for *session_id*.

        Shared by :meth:`replace_messages` (delete-then-insert) and
        :meth:`archive_and_compact` (soft-archive-then-insert). Runs inside the
        caller's write transaction (takes the live ``conn``). Returns
        ``(inserted_count, tool_call_count)``. Does NOT touch sessions.* counters
        — the caller owns that, since the two flows reconcile counts differently.
        """
        now_ts = time.time()
        inserted = 0
        tool_calls_total = 0
        for msg in messages:
            role = msg.get('role', 'unknown')
            tool_calls = msg.get('tool_calls')
            message_timestamp = now_ts
            if msg.get('timestamp') is not None:
                try:
                    ts_value = msg.get('timestamp')
                    if hasattr(ts_value, 'timestamp'):
                        message_timestamp = float(ts_value.timestamp())
                    else:
                        message_timestamp = float(ts_value)
                except (TypeError, ValueError):
                    logger.debug('Ignoring invalid explicit message timestamp: %r', msg.get('timestamp'))
            reasoning_details = msg.get('reasoning_details') if role == 'assistant' else None
            codex_reasoning_items = msg.get('codex_reasoning_items') if role == 'assistant' else None
            codex_message_items = msg.get('codex_message_items') if role == 'assistant' else None
            reasoning_details_json = json.dumps(reasoning_details) if reasoning_details else None
            codex_items_json = json.dumps(codex_reasoning_items) if codex_reasoning_items else None
            codex_message_items_json = json.dumps(codex_message_items) if codex_message_items else None
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except (json.JSONDecodeError, TypeError):
                    tool_calls = []
            tool_calls_json = json.dumps(tool_calls) if tool_calls else None
            platform_msg_id = msg.get('platform_message_id') or msg.get('message_id')
            api_content = msg.get('api_content')
            conn.execute('INSERT INTO messages (session_id, role, content, tool_call_id,\n                   tool_calls, tool_name, effect_disposition, timestamp, token_count, finish_reason,\n                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,\n                   codex_message_items, platform_message_id, observed, active, api_content, display_kind, display_metadata)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (session_id, role, self._encode_content(msg.get('content')), msg.get('tool_call_id'), tool_calls_json, _scrub_surrogates(msg.get('tool_name')), msg.get('effect_disposition'), message_timestamp, msg.get('token_count'), msg.get('finish_reason'), _scrub_surrogates(msg.get('reasoning')) if role == 'assistant' else None, _scrub_surrogates(msg.get('reasoning_content')) if role == 'assistant' else None, reasoning_details_json, codex_items_json, codex_message_items_json, platform_msg_id, 1 if msg.get('observed') else 0, 1, _scrub_surrogates(api_content) if isinstance(api_content, str) else None, _scrub_surrogates(msg.get('display_kind')) if isinstance(msg.get('display_kind'), str) else None, self._encode_display_metadata(msg.get('display_metadata'))))
            inserted += 1
            if tool_calls is not None:
                tool_calls_total += len(tool_calls) if isinstance(tool_calls, list) else 1
            now_ts = max(now_ts + 1e-06, message_timestamp + 1e-06)
        return (inserted, tool_calls_total)

    def replace_messages(self, session_id: str, messages: List[Dict[str, Any]], active_only: bool=False) -> None:
        """Atomically replace the stored messages for a session.

        Used by transcript-rewrite flows such as /retry, /undo, and /compress.
        The delete + reinsert sequence must commit as one transaction so a
        mid-rewrite failure does not leave SQLite with a partial transcript.

        DESTRUCTIVE by default: every row for the session is DELETEd (and drops
        out of the FTS index). For compaction that must preserve the
        pre-compaction transcript under the same id, use
        :meth:`archive_and_compact` instead.

        Pass ``active_only=True`` to replace ONLY the live (``active = 1``) rows,
        leaving soft-archived rows (``active = 0`` — e.g. the ``compacted = 1``
        turns that :meth:`archive_and_compact` keeps on disk for #38763
        durability, or rewind/undo rows) untouched. Callers that share a session
        id with an agent already running in-place compaction must use this so a
        full-history rewrite doesn't wipe the rows the agent deliberately
        archived. ``message_count``/``tool_call_count`` then track the live set,
        matching :meth:`archive_and_compact`.
        """
        active_clause = ' AND active = 1' if active_only else ''

        def _do(conn):
            session = conn.execute('SELECT ended_at, end_reason FROM sessions WHERE id = ?', (session_id,)).fetchone()
            if session is not None and session['ended_at'] is not None and (session['end_reason'] == 'compression'):
                raise CompressionSessionClosedError(session_id)
            conn.execute(f'DELETE FROM messages WHERE session_id = ?{active_clause}', (session_id,))
            conn.execute('UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?', (session_id,))
            total_messages, total_tool_calls = self._insert_message_rows(conn, session_id, messages)
            conn.execute('UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?', (total_messages, total_tool_calls, session_id))
        self._execute_write(_do)

    def has_archived_messages(self, session_id: str) -> bool:
        """Return True if the session has any soft-archived (``active = 0``) rows.

        Cheap existence probe — does not load rows. NOTE: production rewrite
        paths no longer branch on this (they pass ``active_only=True``
        unconditionally — a probe can fail open or race a concurrent
        ``archive_and_compact``, #80216); kept for tests and diagnostics.
        """
        with self._lock:
            cursor = self._conn.execute('SELECT 1 FROM messages WHERE session_id = ? AND active = 0 LIMIT 1', (session_id,))
            return cursor.fetchone() is not None

    def archive_and_compact(self, session_id: str, compacted_messages: List[Dict[str, Any]], model_config_patch: Optional[Dict[str, Any]]=None) -> int:
        """Non-destructive in-place compaction for a single durable session id.

        Soft-archives every currently-active message (``active = 0``) and
        inserts *compacted_messages* as fresh active rows — atomically, in one
        write transaction. The conversation keeps ONE session id for life
        (#38763) WITHOUT destroying history:

        - The live-context load (:meth:`get_messages_as_conversation`,
          :meth:`get_messages`) filters ``active = 1`` by default, so the model
          reloads ONLY the compacted set.
        - The archived pre-compaction turns stay on disk (active=0) and stay
          DISCOVERABLE: they are marked compacted=1, and search_messages()
          includes compacted=1 rows by default — so session_search still finds
          them, unlike rewind/undo rows (active=0, compacted=0) which stay
          hidden. They remain in the FTS index (the messages_fts* triggers
          index on INSERT / drop on DELETE and don't key on active/compacted;
          flipping to active=0 is a content-preserving UPDATE) and are
          recoverable via get_messages(..., include_inactive=True).

        This is the durability-preserving alternative to :meth:`replace_messages`
        for compaction. ``message_count`` is set to the ACTIVE (compacted) count,
        matching what the live load returns. ``model_config_patch`` is merged
        into the session's JSON config in the same transaction; a ``None``
        value removes that key. Returns the new active count.
        """

        def _do(conn):
            patched_model_config = None
            if model_config_patch is not None:
                patched_model_config = self._merge_model_config_json(conn, session_id, model_config_patch, on_missing='raise')
            conn.execute('UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1', (session_id,))
            inserted, tool_calls_total = self._insert_message_rows(conn, session_id, compacted_messages)
            if model_config_patch is None:
                conn.execute('UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?', (inserted, tool_calls_total, session_id))
            else:
                conn.execute('UPDATE sessions SET message_count = ?, tool_call_count = ?, model_config = ? WHERE id = ?', (inserted, tool_calls_total, patched_model_config, session_id))
            return inserted
        return self._execute_write(_do)

    def set_latest_user_api_content(self, session_id: str, content: Any, api_content: str) -> int:
        """Backfill the ``api_content`` sidecar onto the newest ACTIVE user row.

        In-place preflight compaction (:meth:`archive_and_compact`) inserts the
        current turn's user row BEFORE the turn prologue composes the
        prefetch/plugin sidecar, and the subsequent crash persist identity-skips
        every compacted dict — without this backfill the stamped sidecar would
        never land in the DB and any reload would replay clean content,
        re-introducing the prompt-cache divergence the sidecar exists to close.

        The ``content`` match is a defensive guard: if the newest active user
        row is not the message the caller stamped (racing rewrite, unexpected
        tail shape), nothing is written. Returns the number of rows updated
        (0 or 1).
        """
        encoded = self._encode_content(content)

        def _do(conn):
            cursor = conn.execute("UPDATE messages SET api_content = ? WHERE id = (SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND active = 1 ORDER BY id DESC LIMIT 1) AND content IS ?", (_scrub_surrogates(api_content), session_id, encoded))
            return cursor.rowcount
        return self._execute_write(_do)

    def get_messages(self, session_id: str, include_inactive: bool=False, limit: Optional[int]=None, offset: int=0) -> List[Dict[str, Any]]:
        """Load messages for a session in insertion order.

        By default only active messages are returned. Pass
        ``include_inactive=True`` to load soft-deleted rows (e.g. for
        audit / debug views of rewound history). See
        :meth:`rewind_to_message` for the soft-delete mechanic.

        Ordered by AUTOINCREMENT id (true insertion order) rather than
        timestamp — see c03acca50 for the WSL2 clock-regression rationale.

        When ``limit`` is provided, returns at most ``limit`` messages
        starting from ``offset`` (0-based, in insertion order). Enables
        pagination for the API endpoint to avoid loading entire transcripts.
        ``offset`` alone (without ``limit``) also pages — SQLite requires a
        LIMIT clause for OFFSET, so it's emitted as ``LIMIT -1`` (unbounded).
        """
        active_clause = '' if include_inactive else ' AND active = 1'
        sql = f'SELECT * FROM messages WHERE session_id = ?{active_clause} ORDER BY id'
        params: list = [session_id]
        if limit is not None or offset:
            sql += ' LIMIT ? OFFSET ?'
            params.extend([-1 if limit is None else limit, offset])
        with self._read_ctx() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if 'content' in msg:
                msg['content'] = self._decode_content(msg['content'])
            if msg.get('tool_calls'):
                try:
                    msg['tool_calls'] = json.loads(msg['tool_calls'])
                except (json.JSONDecodeError, TypeError):
                    logger.warning('Failed to deserialize tool_calls in get_messages, falling back to []')
                    msg['tool_calls'] = []
            if msg.get('display_metadata') is not None:
                msg['display_metadata'] = self._decode_display_metadata(msg['display_metadata'])
            result.append(msg)
        return result

    def get_messages_around(self, session_id: str, around_message_id: int, window: int=5) -> Dict[str, Any]:
        """Load a window of messages anchored on a specific message id.

        Returns a dict with:
          - ``window``: up to ``window`` messages before the anchor, the anchor
            itself, and up to ``window`` messages after, ordered by id ascending.
          - ``messages_before``: count of messages strictly before the anchor
            still in the session (== window unless we hit the start).
          - ``messages_after``: count of messages strictly after the anchor
            still in the session (== window unless we hit the end).

        Used by ``session_search`` for both the discovery shape (anchored on the
        FTS5 match) and the scroll shape (anchored on any message id). The
        ``messages_before`` / ``messages_after`` counts let the caller detect
        session boundaries: when either is less than ``window``, the agent has
        reached one end of the session.

        Returns an empty window when ``around_message_id`` is not a real id in
        ``session_id`` — callers decide how to surface that.
        """
        if window < 0:
            window = 0
        with self._read_ctx() as conn:
            anchor_exists = conn.execute('SELECT 1 FROM messages WHERE id = ? AND session_id = ? LIMIT 1', (around_message_id, session_id)).fetchone()
            if not anchor_exists:
                return {'window': [], 'messages_before': 0, 'messages_after': 0}
            before_rows = conn.execute('SELECT * FROM messages WHERE session_id = ? AND id <= ? ORDER BY id DESC LIMIT ?', (session_id, around_message_id, window + 1)).fetchall()
            after_rows = conn.execute('SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?', (session_id, around_message_id, window)).fetchall()
        rows = list(reversed(before_rows)) + list(after_rows)
        result = []
        for row in rows:
            msg = dict(row)
            if 'content' in msg:
                msg['content'] = self._decode_content(msg['content'])
            if msg.get('tool_calls'):
                try:
                    msg['tool_calls'] = json.loads(msg['tool_calls'])
                except (json.JSONDecodeError, TypeError):
                    logger.warning('Failed to deserialize tool_calls in get_messages_around, falling back to []')
                    msg['tool_calls'] = []
            if msg.get('display_metadata') is not None:
                msg['display_metadata'] = self._decode_display_metadata(msg['display_metadata'])
            result.append(msg)
        messages_before = max(0, len(before_rows) - 1)
        messages_after = len(after_rows)
        return {'window': result, 'messages_before': messages_before, 'messages_after': messages_after}

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new child session
        (linked via ``parent_session_id``). The flush cursor is reset, so the
        child is where new messages actually land — the parent ends up with
        ``message_count = 0`` rows unless messages had already been flushed to
        it before compression. See #15000.

        This helper walks ``parent_session_id`` forward from ``session_id`` and
        returns the descendant in the chain that has the **most recent** messages.
        Unlike the original logic, it does NOT short-circuit when the starting
        session already has messages — a descendant that was created by
        compression may hold the continuation content and should be preferred
        by the WebUI and gateway for ``--resume`` and session loading.

        If no descendant (including the starting session) has any messages,
        the original ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at`` is
        latest; that matches the single-chain shape that compression creates.
        A depth cap (32) guards against accidental loops in malformed data.
        """
        if not session_id:
            return session_id
        try:
            tip = self.get_compression_tip(session_id)
        except Exception:
            tip = session_id
        if tip and tip != session_id:
            session_id = tip
        with self._lock:
            current = session_id
            seen = {current}
            best = None
            for _ in range(32):
                try:
                    row = self._conn.execute('SELECT 1 FROM messages WHERE session_id = ? LIMIT 1', (current,)).fetchone()
                except Exception:
                    return session_id
                if row is not None:
                    best = current
                try:
                    child_row = self._conn.execute("SELECT id FROM sessions WHERE parent_session_id = ?   AND json_extract(COALESCE(model_config, '{}'), '$._branched_from') IS NULL   AND json_extract(COALESCE(model_config, '{}'), '$._delegate_from') IS NULL   AND COALESCE(source, '') != 'tool' ORDER BY started_at DESC, id DESC LIMIT 1", (current,)).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    break
                child_id = child_row['id'] if hasattr(child_row, 'keys') else child_row[0]
                if not child_id or child_id in seen:
                    break
                seen.add(child_id)
                current = child_id
            return best if best is not None else session_id

    def get_messages_as_conversation(self, session_id: str, include_ancestors: bool=False, include_inactive: bool=False, repair_alternation: bool=False, include_row_ids: bool=False) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.

        By default only active messages are returned. Pass
        ``include_inactive=True`` to load soft-deleted (rewound) rows
        as well. See :meth:`rewind_to_message`.

        ``repair_alternation=True`` runs ``repair_message_sequence`` over the
        loaded list before returning it. Callers that restore a session for
        LIVE REPLAY should pass it: a durable alternation violation (e.g. a
        ``user;user`` pair left by a turn that persisted no assistant row)
        otherwise re-triggers the pre-request defensive repair on every
        single request for the rest of the session's life — the repair
        mutates only the per-request list, never the stored transcript.
        Inspection/export consumers keep the default and see the transcript
        verbatim.
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)
        active_clause = '' if include_inactive else ' AND active = 1'
        with self._read_ctx() as conn:
            placeholders = ','.join(('?' for _ in session_ids))
            rows = conn.execute(f'SELECT {self._CONVERSATION_ROW_COLUMNS} FROM messages WHERE session_id IN ({placeholders}){active_clause} ORDER BY id', tuple(session_ids)).fetchall()
        return self._rows_to_conversation(rows, session_id=session_id, include_ancestors=include_ancestors, repair_alternation=repair_alternation, include_row_ids=include_row_ids)
    _CONVERSATION_ROW_COLUMNS = 'id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition, finish_reason, reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items, platform_message_id, observed, timestamp, api_content, display_kind, display_metadata'

    def _rows_to_conversation(self, rows, *, session_id: str, include_ancestors: bool, repair_alternation: bool, include_row_ids: bool=False) -> List[Dict[str, Any]]:
        """Decode fetched message rows into the OpenAI conversation format.

        Extracted from get_messages_as_conversation so get_resume_conversations
        can build the model-fed and display views from one SELECT. ``rows`` must
        already be ordered by ``id`` (insertion order) and filtered to the
        desired session set / active state by the caller.
        """
        messages = []
        for row in rows:
            content = self._decode_content(row['content'])
            if row['role'] in {'user', 'assistant'} and isinstance(content, str):
                content = sanitize_context(content).strip()
            msg = {'role': row['role'], 'content': content}
            if include_row_ids and row['id'] is not None:
                msg['_row_id'] = row['id']
            if row['api_content']:
                msg['api_content'] = row['api_content']
            if row['display_kind']:
                msg['display_kind'] = row['display_kind']
            if row['display_metadata']:
                decoded = self._decode_display_metadata(row['display_metadata'])
                if decoded is not None:
                    msg['display_metadata'] = decoded
            if row['timestamp']:
                msg['timestamp'] = row['timestamp']
            if row['tool_call_id']:
                msg['tool_call_id'] = row['tool_call_id']
            if row['tool_name']:
                msg['tool_name'] = row['tool_name']
            if row['effect_disposition']:
                msg['effect_disposition'] = row['effect_disposition']
            if row['tool_calls']:
                try:
                    msg['tool_calls'] = json.loads(row['tool_calls'])
                except (json.JSONDecodeError, TypeError):
                    logger.warning('Failed to deserialize tool_calls in conversation replay, falling back to []')
                    msg['tool_calls'] = []
            if row['platform_message_id']:
                msg['message_id'] = row['platform_message_id']
            if row['observed']:
                msg['observed'] = True
            if row['role'] == 'assistant':
                if row['finish_reason']:
                    msg['finish_reason'] = row['finish_reason']
                if row['reasoning']:
                    msg['reasoning'] = row['reasoning']
                if row['reasoning_content'] is not None:
                    msg['reasoning_content'] = row['reasoning_content']
                if row['reasoning_details']:
                    try:
                        msg['reasoning_details'] = json.loads(row['reasoning_details'])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning('Failed to deserialize reasoning_details, falling back to None')
                        msg['reasoning_details'] = None
                if row['codex_reasoning_items']:
                    try:
                        msg['codex_reasoning_items'] = json.loads(row['codex_reasoning_items'])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning('Failed to deserialize codex_reasoning_items, falling back to None')
                        msg['codex_reasoning_items'] = None
                if row['codex_message_items']:
                    try:
                        msg['codex_message_items'] = json.loads(row['codex_message_items'])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning('Failed to deserialize codex_message_items, falling back to None')
                        msg['codex_message_items'] = None
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)
        messages = _strip_background_review_harness(messages)
        messages = _strip_stale_tool_call_markers(messages)
        if repair_alternation and messages:
            from agent.agent_runtime_helpers import repair_message_sequence
            repaired = repair_message_sequence(None, messages)
            if repaired:
                logger.info('Repaired %d message-alternation violation(s) while restoring session %s — durable transcript kept them, see repair_message_sequence', repaired, session_id)
        return messages

    def get_resume_conversations(self, session_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return ``(model_history, display_history)`` for a session resume in ONE SELECT.

        ``session.resume`` needs two projections of the same lineage:

        - ``model_history`` — the tip session's active rows, alternation-repaired
          (the live-replay working conversation). Equivalent to
          ``get_messages_as_conversation(session_id, repair_alternation=True)``.
        - ``display_history`` — the full lineage (ancestors → tip), verbatim, with
          replayed-user dedup. Equivalent to
          ``get_messages_as_conversation(session_id, include_ancestors=True)``.

        The display fetch already reads a superset of the model fetch (the tip
        rows are part of the lineage), so serving both from one lineage SELECT
        halves the resume's DB work versus two separate calls, with byte-identical
        output (see test_get_resume_conversations_matches_separate_reads).
        """
        session_ids = self._session_lineage_root_to_tip(session_id)
        with self._read_ctx() as conn:
            placeholders = ','.join(('?' for _ in session_ids))
            rows = conn.execute(f'SELECT session_id, {self._CONVERSATION_ROW_COLUMNS} FROM messages WHERE session_id IN ({placeholders}) AND active = 1 ORDER BY id', tuple(session_ids)).fetchall()
        tip_rows = [r for r in rows if r['session_id'] == session_id]
        model_history = self._rows_to_conversation(tip_rows, session_id=session_id, include_ancestors=False, repair_alternation=True, include_row_ids=True)
        display_history = self._rows_to_conversation(rows, session_id=session_id, include_ancestors=True, repair_alternation=False, include_row_ids=True)
        return (model_history, display_history)

    def get_ancestor_display_prefix(self, session_id: str) -> List[Dict[str, Any]]:
        """Return the ancestor-only display messages for a session lineage.

        These are messages from parent/grandparent sessions (compression
        ancestors) that appear in the display transcript but NOT in the
        tip session's model-fed history. Used by ``session.resume`` to
        build the ``display_history_prefix`` that ``_live_session_payload``
        prepends to the live model history.

        Previously the prefix was calculated as
        ``display_history[:len(display) - len(raw)]``, but that overcounts
        when ``repair_message_sequence`` removes messages from the MIDDLE
        of the tip history (e.g. verification candidates collapsed by the
        consecutive-assistant merge) — the length difference includes both
        ancestor messages AND repair-removed tip messages, but the slice
        only captures the first N display messages (which are tip messages
        when there are no ancestors), causing duplication. This method
        returns ONLY the genuine ancestor messages, identified by
        ``session_id != tip_session_id``. (#65919)
        """
        session_ids = self._session_lineage_root_to_tip(session_id)
        if len(session_ids) <= 1:
            return []
        with self._read_ctx() as conn:
            placeholders = ','.join(('?' for _ in session_ids))
            rows = conn.execute(f'SELECT session_id, {self._CONVERSATION_ROW_COLUMNS} FROM messages WHERE session_id IN ({placeholders}) AND active = 1 ORDER BY id', tuple(session_ids)).fetchall()
        ancestor_rows = [r for r in rows if r['session_id'] != session_id]
        if not ancestor_rows:
            return []
        return self._rows_to_conversation(ancestor_rows, session_id=session_id, include_ancestors=True, repair_alternation=False)

    def get_conversation_root(self, session_id: str) -> str:
        """Return the ROOT id of *session_id*'s lineage chain.

        The root is the stable "conversation id": context compression
        rotates ``session_id`` to a new segment linked via
        ``parent_session_id``, and delegate subagents hang off their
        parent the same way. Walking to the root gives every segment of
        one user-facing conversation (and its delegation tree) a single
        identifier — used for Nous Portal ``conversation=`` usage tagging.
        Returns *session_id* unchanged when it has no recorded parent.
        """
        chain = self._session_lineage_root_to_tip(session_id)
        return chain[0] if chain and chain[0] else session_id

    def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        if not session_id:
            return [session_id]
        chain = []
        current = session_id
        seen = set()
        with self._read_ctx() as conn:
            for _ in range(100):
                if not current or current in seen:
                    break
                seen.add(current)
                chain.append(current)
                row = conn.execute('SELECT parent_session_id FROM sessions WHERE id = ?', (current,)).fetchone()
                if row is None:
                    break
                current = row['parent_session_id'] if hasattr(row, 'keys') else row[0]
        return list(reversed(chain)) or [session_id]

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get('role') != 'user':
            return False
        content = msg.get('content')
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get('role') == 'user' and prev.get('content') == content:
                return True
            if prev.get('role') == 'assistant' and (prev.get('content') or prev.get('tool_calls')):
                return False
        return False

    def rewind_to_message(self, session_id: str, target_message_id: int) -> Dict[str, Any]:
        """Soft-delete all messages with id >= ``target_message_id`` in *session_id*.

        The target message itself becomes inactive as well so the caller
        can pre-fill it as the next user prompt without it appearing
        twice in the replayed transcript.  Rewound rows are kept on
        disk with ``active=0`` for audit / forensic inspection — use
        :meth:`get_messages` with ``include_inactive=True`` to see them.

        Returns a dict::

            {
                "rewound_count": int,    # number of rows newly flipped to active=0
                "target_message": dict,  # full row dict of the target
                "new_head_id":   int|None  # id of the last still-active row, or None
            }

        Raises ``ValueError`` if the target message does not exist in
        *session_id* or if its role is not ``"user"``.

        Always increments ``sessions.rewind_count`` — even when the
        target is already inactive — so the counter accurately reflects
        the number of rewind operations performed against the session.
        Idempotent on the ``active`` flag: re-rewinding past the same
        target is a no-op on row state but still bumps the counter.
        """
        with self._lock:
            row = self._conn.execute('SELECT * FROM messages WHERE id = ? AND session_id = ?', (target_message_id, session_id)).fetchone()
        if row is None:
            raise ValueError(f'message {target_message_id} not found in session {session_id}')
        target_row = dict(row)
        if target_row.get('role') != 'user':
            raise ValueError(f"rewind target must be a 'user' message (got role={target_row.get('role')!r}, id={target_message_id})")
        target_row['content'] = self._decode_content(target_row.get('content'))
        rewound: List[int] = []

        def _do(conn):
            cursor = conn.execute('SELECT id FROM messages WHERE session_id = ? AND id >= ? AND active = 1', (session_id, target_message_id))
            ids = [r[0] for r in cursor.fetchall()]
            if ids:
                placeholders = ','.join(('?' for _ in ids))
                conn.execute(f'UPDATE messages SET active = 0 WHERE id IN ({placeholders})', ids)
            conn.execute('UPDATE sessions SET rewind_count = COALESCE(rewind_count, 0) + 1 WHERE id = ?', (session_id,))
            return ids
        rewound = self._execute_write(_do)
        with self._lock:
            head_row = self._conn.execute('SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1', (session_id,)).fetchone()
        new_head_id = head_row[0] if head_row and head_row[0] is not None else None
        return {'rewound_count': len(rewound), 'target_message': target_row, 'new_head_id': new_head_id}

    def restore_rewound(self, session_id: str, since_message_id: int) -> int:
        """Mark inactive messages with id >= *since_message_id* active again.

        Returns the number of rows flipped back to ``active=1``.
        Intended for undo-of-rewind and test cleanup; not wired to a
        slash command in v1.
        """

        def _do(conn):
            cursor = conn.execute('SELECT id FROM messages WHERE session_id = ? AND id >= ? AND active = 0', (session_id, since_message_id))
            ids = [r[0] for r in cursor.fetchall()]
            if ids:
                placeholders = ','.join(('?' for _ in ids))
                conn.execute(f'UPDATE messages SET active = 1 WHERE id IN ({placeholders})', ids)
            return len(ids)
        return self._execute_write(_do)

    def search_sessions(self, source: str=None, limit: int=20, offset: int=0, workspace_key: str=None) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows enriched with a computed ``last_active`` column
        (freshest of ``last_activity_at`` and latest message timestamp,
        else ``started_at``), ordered by most-recently-used first.

        Pass ``workspace_key`` to scope rows to one workspace - matching
        :func:`workspace_key` semantics (git repo root, else cwd). Used by
        ``duck-agent -c``/``--resume`` so the "last" session is the last one in
        the *current* workspace, not the global MRU.
        """
        select_with_last_active = f"SELECT s.*, COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved, {_sql_session_last_active('s')} AS last_active FROM sessions s LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
        where_clauses = []
        params: list = []
        if source:
            where_clauses.append('s.source = ?')
            params.append(source)
        if workspace_key:
            ws_clause, ws_params = _workspace_key_clause(workspace_key)
            where_clauses.append(ws_clause)
            params.extend(ws_params)
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(f'{select_with_last_active}{where_sql} ORDER BY last_active DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?', params)
            return [self._session_row_dict(row) for row in cursor.fetchall()]

    def session_count(self, source: str=None, sources: List[str]=None, cwd_prefix: str=None, min_message_count: int=0, include_archived: bool=False, archived_only: bool=False, exclude_children: bool=False, exclude_sources: List[str]=None) -> int:
        """Count sessions, optionally filtered by source.

        Pass ``exclude_children=True`` to count only the conversations that
        ``list_sessions_rich`` surfaces (root + branch sessions), hiding
        sub-agent runs and compression continuations. Use it whenever the count
        is paired with a ``list_sessions_rich`` page (e.g. sidebar "load more"
        totals) so the total matches the number of listable rows — otherwise the
        raw row count is inflated by children and "load more" never settles.

        Pass ``exclude_sources`` to drop whole source classes from the count
        (e.g. ``["cron"]`` so the recents "load more" total matches a
        cron-excluded ``list_sessions_rich`` page and doesn't keep "load more"
        stuck on for buried scheduler sessions).
        """
        where_clauses = []
        params = []
        if exclude_children:
            where_clauses.append(_LISTABLE_CHILD_SQL)
            where_clauses.append(f"{_delegate_from_json('s.model_config')} IS NULL")
        include_sources = [source] if source else list(sources or [])
        if include_sources:
            placeholders = ','.join(('?' for _ in include_sources))
            where_clauses.append(f's.source IN ({placeholders})')
            params.extend(include_sources)
        if exclude_sources:
            placeholders = ','.join(('?' for _ in exclude_sources))
            where_clauses.append(f's.source NOT IN ({placeholders})')
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append('s.message_count >= ?')
            params.append(min_message_count)
        if archived_only:
            where_clauses.append('s.archived = 1')
        elif not include_archived:
            where_clauses.append('s.archived = 0')
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        with self._lock:
            cursor = self._conn.execute(f'SELECT COUNT(*) FROM sessions s{where_sql}', params)
            return cursor.fetchone()[0]

    def session_count_ge(self, n: int=1) -> bool:
        """Check if at least N sessions exist (archived included).

        Short-circuits via LIMIT — much cheaper than ``session_count()``,
        which pays a full index scan for its default ``archived = 0``
        filter (measured 543us vs 4us on a 20k-session DB). Archived
        sessions count: every caller so far asks "has this install ever
        had sessions", and an archived session is still a created one.
        Use this instead of ``session_count() >= n`` when the exact count
        is irrelevant.
        """
        with self._lock:
            cursor = self._conn.execute('SELECT 1 FROM sessions LIMIT ?', (n,))
            rows = cursor.fetchall()
        return len(rows) >= n

    def session_count_by_source(self, *, include_archived: bool=False, archived_only: bool=False, exclude_children: bool=False) -> Dict[str, int]:
        """Return a ``{source: count}`` dict via a single ``GROUP BY`` query.

        Replaces the O(N) ``list_sessions_rich`` histogram loop with an
        aggregate query. When ``exclude_children`` is False the query uses
        ``idx_sessions_source``; when True, the child-exclusion predicates
        require a full table scan (same as ``session_count`` and
        ``list_sessions_rich``).

        ``exclude_children=True`` mirrors ``list_sessions_rich`` visibility
        (roots + branch sessions, excluding sub-agent runs, delegates, and
        compression continuations) so the source counts match what the
        Sessions page actually lists.
        """
        where_clauses = []
        params: list = []
        if exclude_children:
            where_clauses.append(_LISTABLE_CHILD_SQL)
            where_clauses.append(f"{_delegate_from_json('s.model_config')} IS NULL")
        if archived_only:
            where_clauses.append('s.archived = 1')
        elif not include_archived:
            where_clauses.append('s.archived = 0')
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ''
        with self._lock:
            if self._conn is None:
                raise RuntimeError('SessionDB connection is closed')
            rows = self._conn.execute(f"SELECT COALESCE(NULLIF(s.source, ''), 'cli') AS source, COUNT(*) AS count FROM sessions s{where_sql} GROUP BY COALESCE(NULLIF(s.source, ''), 'cli') ORDER BY count DESC", params).fetchall()
        return {str(row['source']): int(row['count'] or 0) for row in rows}

    def message_count(self, session_id: str=None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute('SELECT COUNT(*) FROM messages WHERE session_id = ?', (session_id,))
            else:
                cursor = self._conn.execute('SELECT COUNT(*) FROM messages')
            return cursor.fetchone()[0]

    def has_platform_message_id(self, session_id: str, platform_message_id: str) -> bool:
        """Check if a message with the given platform_message_id exists.

        Uses the idx_messages_platform_msg_id partial index for efficient
        lookup. Used by the gateway's transient-failure dedupe guard (#47237)
        to skip re-persisting a user message that was already saved on a
        prior retry of the same inbound platform message.
        """
        with self._lock:
            cursor = self._conn.execute('SELECT 1 FROM messages WHERE session_id = ? AND platform_message_id = ? LIMIT 1', (session_id, platform_message_id))
            return cursor.fetchone() is not None

    def _is_explicit_fork_child_row(self, session: Dict[str, Any]) -> bool:
        if session.get('source') == 'tool':
            return True
        raw = session.get('model_config')
        if not raw:
            return False
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(cfg, dict) and (cfg.get('_branched_from') is not None or cfg.get('_delegate_from') is not None)

    def _is_compression_child_row(self, child: Dict[str, Any]) -> bool:
        parent_id = child.get('parent_session_id')
        if not parent_id or self._is_explicit_fork_child_row(child):
            return False
        parent = self.get_session(parent_id)
        return bool(parent and parent.get('end_reason') == 'compression')

    def get_compression_lineage(self, session_id: str) -> List[str]:
        """Return compression ancestors through tip in chronological order."""
        session = self.get_session(session_id)
        if not session or self._is_explicit_fork_child_row(session):
            return [session_id] if session else []
        root = session
        ancestors = {root['id']}
        while self._is_compression_child_row(root):
            parent = self.get_session(root['parent_session_id'])
            if not parent or parent['id'] in ancestors:
                break
            root = parent
            ancestors.add(root['id'])
        lineage = [root['id']]
        seen = {root['id']}
        current = root
        while current.get('end_reason') == 'compression':
            with self._lock:
                rows = self._conn.execute('\n                    SELECT * FROM sessions\n                    WHERE parent_session_id = ?\n                    ORDER BY started_at ASC\n                    ', (current['id'],)).fetchall()
            next_child = None
            for row in rows:
                candidate = dict(row)
                if self._is_compression_child_row(candidate):
                    next_child = candidate
                    break
            if not next_child or next_child['id'] in seen:
                break
            lineage.append(next_child['id'])
            seen.add(next_child['id'])
            current = next_child
            if current['id'] == session_id:
                continue
        return lineage if session_id in lineage else [session_id]

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""

        def _do(conn):
            conn.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
            conn.execute('UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?', (session_id,))
        self._execute_write(_do)

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in ('.json', '.jsonl'):
            p = sessions_dir / f'{session_id}{suffix}'
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            for p in sessions_dir.glob(f'request_dump_{session_id}_*.json'):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def get_session_delete_targets(self, session_id: str) -> List[str]:
        """Return every session row that :meth:`delete_session` would remove.

        The requested session is first, followed by its recursively discovered
        delegate/subagent children. Branch and compression children are not
        included because deletion preserves them by orphaning their parent
        reference.
        """
        with self._lock:
            exists = self._conn.execute('SELECT 1 FROM sessions WHERE id = ? LIMIT 1', (session_id,)).fetchone()
            if not exists:
                return []
            delegate_ids = _collect_delegate_child_ids(self._conn, [session_id])
        return [session_id, *sorted(delegate_ids)]

    def delete_session(self, session_id: str, sessions_dir: Optional[Path]=None, expected_delete_ids: Optional[List[str]]=None) -> bool:
        """Delete a session and all its messages.

        Delegate subagent children (``model_config._delegate_from``) are
        cascade-deleted with the parent so they never resurface in session
        pickers as orphaned rows. Branch / compression children are orphaned
        (``parent_session_id → NULL``) so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for every deleted
        session. When *expected_delete_ids* is provided, deletion proceeds only
        if the parent plus delegate cascade still matches that exact set. This
        lets export-before-delete callers fail closed if a new delegate appears
        after they materialize their archive. The delegate tree is re-walked
        inside the write transaction on purpose (TOCTOU guard); the cost is
        accepted for correctness. Returns True if the session was found and
        deleted.
        """
        removed_delegate_ids: List[str] = []
        expected_ids = set(expected_delete_ids) if expected_delete_ids is not None else None

        def _do(conn):
            cursor = conn.execute('SELECT 1 FROM sessions WHERE id = ? LIMIT 1', (session_id,))
            if cursor.fetchone() is None:
                return False
            if expected_ids is not None:
                actual_ids = {session_id, *_collect_delegate_child_ids(conn, [session_id])}
                if actual_ids != expected_ids:
                    return False
            removed_delegate_ids.extend(_delete_delegate_children(conn, [session_id]))
            conn.execute('UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?', (session_id,))
            conn.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
            conn.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
            self._delete_unreferenced_system_prompts(conn)
            return True
        deleted = self._execute_write(_do)
        if deleted:
            for delegate_id in removed_delegate_ids:
                self._remove_session_files(sessions_dir, delegate_id)
            self._remove_session_files(sessions_dir, session_id)
        return bool(deleted)

    def delete_session_if_empty(self, session_id: str, sessions_dir: Optional[Path]=None) -> bool:
        """Delete *session_id* only when it never gained resumable content.

        A session is considered empty when it has no messages and no
        user-assigned title. Used by CLI exit / session-rotation paths so
        immediately-started-and-quit sessions don't pile up in ``/resume``
        and ``duck-agent sessions list`` output. (Pattern ported from
        google-gemini/gemini-cli#27770.)

        The emptiness check and delete run in one transaction, so a message
        flushed concurrently by another writer can't be lost. Sessions with
        children (delegate subagent runs) are preserved — a parent that
        spawned work is not "empty" even if its own transcript never
        flushed. Returns True if the session was deleted.
        """

        def _do(conn):
            cursor = conn.execute('\n                DELETE FROM sessions\n                WHERE id = ?\n                  AND title IS NULL\n                  AND NOT EXISTS (\n                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM sessions child\n                      WHERE child.parent_session_id = sessions.id\n                  )\n                ', (session_id,))
            if cursor.rowcount > 0:
                self._delete_unreferenced_system_prompts(conn)
            return cursor.rowcount > 0
        deleted = self._execute_write(_do)
        if deleted:
            self._remove_session_files(sessions_dir, session_id)
        return bool(deleted)

    def delete_sessions(self, session_ids: List[str], sessions_dir: Optional[Path]=None) -> int:
        """Delete every session in *session_ids* in a single transaction.

        Backs the dashboard's bulk-select-then-delete flow on the
        sessions page (``POST /api/sessions/bulk-delete``). Mirrors the
        single-session :meth:`delete_session` contract per row:

        * Unknown IDs are silently skipped (no 404) — selection state
          in the UI can race against another tab's delete, and we'd
          rather succeed-on-the-rest than fail-the-whole-batch.
        * Delegate subagent children (``model_config._delegate_from``) are
          cascade-deleted with their parent; branch children are orphaned
          (``parent_session_id → NULL``) so they stay accessible.
        * Messages and the session row both go in one
          ``_execute_write`` call so a partial failure can't leave the
          DB in a "messages gone but session row still there" state.
        * On-disk transcript / ``request_dump_*`` files are cleaned up
          outside the DB transaction when *sessions_dir* is provided,
          matching :meth:`prune_sessions` and
          :meth:`delete_empty_sessions`.

        Returns the count of sessions that actually existed and were
        deleted (may be less than ``len(session_ids)`` if some IDs were
        already gone).
        """
        if not session_ids:
            return 0
        unique_ids = list({sid for sid in session_ids if isinstance(sid, str) and sid})
        if not unique_ids:
            return 0
        removed_ids: list[str] = []
        removed_delegate_ids: list[str] = []

        def _do(conn):
            placeholders = ','.join('?' * len(unique_ids))
            cursor = conn.execute(f'SELECT id FROM sessions WHERE id IN ({placeholders})', unique_ids)
            existing = [row['id'] for row in cursor.fetchall()]
            if not existing:
                return 0
            existing_placeholders = ','.join('?' * len(existing))
            removed_delegate_ids.extend(_delete_delegate_children(conn, existing))
            conn.execute(f'UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({existing_placeholders})', existing)
            conn.execute(f'DELETE FROM messages WHERE session_id IN ({existing_placeholders})', existing)
            conn.execute(f'DELETE FROM sessions WHERE id IN ({existing_placeholders})', existing)
            self._delete_unreferenced_system_prompts(conn)
            removed_ids.extend(existing)
            return len(existing)
        count = self._execute_write(_do)
        for sid in removed_delegate_ids:
            self._remove_session_files(sessions_dir, sid)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    def count_empty_sessions(self) -> int:
        """Return the count of empty, non-active, non-archived sessions.

        "Empty" = ``message_count = 0`` AND the session has ended
        (``ended_at IS NOT NULL``) AND is not archived. The ``ended_at``
        guard matches the safety contract used by :meth:`prune_sessions`:
        only ended sessions are candidates for bulk deletion, so a freshly
        spawned session whose first message hasn't landed yet — or one
        held open by the live agent — is never sniped out from under
        the runtime.

        Backs the ``GET /api/sessions/empty/count`` endpoint that lets the
        web dashboard hide its "Delete empty" button when there's nothing
        to clean up, and pre-populate the confirm dialog with the actual
        count.
        """
        with self._lock:
            cursor = self._conn.execute('SELECT COUNT(*) FROM sessions WHERE message_count = 0 AND ended_at IS NOT NULL AND archived = 0')
            return cursor.fetchone()[0]

    def delete_empty_sessions(self, sessions_dir: Optional[Path]=None) -> int:
        """Delete every empty, ended, non-archived session.

        Mirrors :meth:`prune_sessions`' transactional shape:

        * Selects candidate IDs first (``message_count = 0`` AND
          ``ended_at IS NOT NULL`` AND ``archived = 0``) so we never
          touch a live session or one the user deliberately archived.
        * Orphans any child whose parent is in the kill list — children
          of an empty parent are kept and re-parented to ``NULL`` rather
          than cascade-deleted, matching ``delete_session`` /
          ``prune_sessions`` semantics so branch/subagent transcripts
          survive an inadvertent parent cleanup.
        * Deletes the rows in a single ``_execute_write`` callback so
          the operation is atomic — a partial failure (e.g. SIGKILL
          mid-loop) doesn't leave the DB in a "messages-deleted but
          session-row-still-there" half-state.
        * Cleans up on-disk transcript files (``.json`` / ``.jsonl`` /
          ``request_dump_*``) outside the DB transaction when
          ``sessions_dir`` is provided. Empty sessions don't typically
          have transcript files, but the gateway can leave a stub
          ``request_dump_*`` if it crashed before the first reply —
          so we still sweep, matching ``prune_sessions``.

        Returns the number of sessions deleted.
        """
        removed_ids: list[str] = []

        def _do(conn):
            cursor = conn.execute('SELECT id FROM sessions WHERE message_count = 0 AND ended_at IS NOT NULL AND archived = 0')
            session_ids = {row['id'] for row in cursor.fetchall()}
            if not session_ids:
                return 0
            placeholders = ','.join('?' * len(session_ids))
            conn.execute(f'UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({placeholders})', list(session_ids))
            for sid in session_ids:
                conn.execute('DELETE FROM messages WHERE session_id = ?', (sid,))
                conn.execute('DELETE FROM sessions WHERE id = ?', (sid,))
                removed_ids.append(sid)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)
        count = self._execute_write(_do)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    @staticmethod
    def _prune_filter_where(*, last_active_before: Optional[float]=None, last_active_after: Optional[float]=None, started_before: Optional[float]=None, started_after: Optional[float]=None, source: Optional[str]=None, title_like: Optional[str]=None, end_reason: Optional[str]=None, cwd_prefix: Optional[str]=None, min_messages: Optional[int]=None, max_messages: Optional[int]=None, archived: Optional[bool]=None, model_like: Optional[str]=None, provider: Optional[str]=None, user_id: Optional[str]=None, chat_id: Optional[str]=None, chat_type: Optional[str]=None, branch_like: Optional[str]=None, min_tokens: Optional[int]=None, max_tokens: Optional[int]=None, min_cost: Optional[float]=None, max_cost: Optional[float]=None, min_tool_calls: Optional[int]=None, max_tool_calls: Optional[int]=None) -> Tuple[str, list]:
        """Build the shared WHERE clause for bulk prune/archive selection.

        All filters AND together. Only ended sessions are ever candidates
        (``ended_at IS NOT NULL``) so a live session is never selected.
        ``archived`` is a tri-state: ``None`` = both, ``True`` = only
        archived rows, ``False`` = only unarchived rows.

        String matching conventions: ``model_like`` / ``branch_like`` /
        ``title_like`` are case-insensitive substring matches (model slugs
        and branch names vary in prefix format); ``provider`` / ``user_id``
        / ``chat_id`` / ``chat_type`` / ``source`` / ``end_reason`` are
        exact (case-insensitive for provider). Token bounds apply to
        ``input_tokens + output_tokens``; cost bounds apply to
        ``COALESCE(actual_cost_usd, estimated_cost_usd)``.

        The clause references the ``s`` table alias — callers must select
        ``FROM sessions s``.
        """
        clauses = ['s.ended_at IS NOT NULL']
        params: list = []
        if last_active_before is not None:
            clauses.append('COALESCE(\n                       (SELECT MAX(m.timestamp) FROM messages m\n                        WHERE m.session_id = s.id),\n                       s.started_at\n                   ) < ?')
            params.append(last_active_before)
        if last_active_after is not None:
            clauses.append('COALESCE(\n                       (SELECT MAX(m.timestamp) FROM messages m\n                        WHERE m.session_id = s.id),\n                       s.started_at\n                   ) >= ?')
            params.append(last_active_after)
        if started_before is not None:
            clauses.append('s.started_at < ?')
            params.append(started_before)
        if started_after is not None:
            clauses.append('s.started_at >= ?')
            params.append(started_after)
        if source:
            clauses.append('s.source = ?')
            params.append(source)
        if title_like:
            clauses.append("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'")
            params.append(f'%{_escape_like(title_like.lower())}%')
        if end_reason:
            clauses.append('s.end_reason = ?')
            params.append(end_reason)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            clauses.append(clause)
            params.extend(clause_params)
        if min_messages is not None:
            clauses.append('s.message_count >= ?')
            params.append(min_messages)
        if max_messages is not None:
            clauses.append('s.message_count <= ?')
            params.append(max_messages)
        if model_like:
            clauses.append("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'")
            params.append(f'%{_escape_like(model_like.lower())}%')
        if provider:
            clauses.append("LOWER(COALESCE(s.billing_provider, '')) = ?")
            params.append(provider.lower())
        if user_id:
            clauses.append('s.user_id = ?')
            params.append(user_id)
        if chat_id:
            clauses.append('s.chat_id = ?')
            params.append(chat_id)
        if chat_type:
            clauses.append('s.chat_type = ?')
            params.append(chat_type)
        if branch_like:
            clauses.append("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'")
            params.append(f'%{_escape_like(branch_like.lower())}%')
        if min_tokens is not None:
            clauses.append('(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) >= ?')
            params.append(min_tokens)
        if max_tokens is not None:
            clauses.append('(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) <= ?')
            params.append(max_tokens)
        if min_cost is not None:
            clauses.append('COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) >= ?')
            params.append(min_cost)
        if max_cost is not None:
            clauses.append('COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) <= ?')
            params.append(max_cost)
        if min_tool_calls is not None:
            clauses.append('COALESCE(s.tool_call_count, 0) >= ?')
            params.append(min_tool_calls)
        if max_tool_calls is not None:
            clauses.append('COALESCE(s.tool_call_count, 0) <= ?')
            params.append(max_tool_calls)
        if archived is True:
            clauses.append('s.archived = 1')
        elif archived is False:
            clauses.append('s.archived = 0')
        return (' AND '.join(clauses), params)

    def list_prune_candidates(self, older_than_days: Optional[float]=None, source: str=None, **filters) -> List[Dict[str, Any]]:
        """Return the sessions a matching :meth:`prune_sessions` /
        :meth:`archive_sessions` call would touch, without modifying anything.

        Backs ``--dry-run`` and pre-confirmation counts. Accepts the same
        keyword filters as :meth:`_prune_filter_where` (unknown names raise
        ``TypeError`` there). Rows are ordered oldest-first and carry
        ``id, source, title, model, started_at, last_active, ended_at,
        message_count, archived``. ``older_than_days`` is an inactivity
        threshold: it uses the latest message timestamp, falling back to
        ``started_at`` for sessions without messages.
        """
        if filters.get('last_active_before') is None and filters.get('started_before') is None and (older_than_days is not None):
            filters['last_active_before'] = time.time() - older_than_days * 86400
        where, params = self._prune_filter_where(source=source, **filters)
        with self._lock:
            cursor = self._conn.execute(f'SELECT s.id, s.source, s.title, s.model, s.started_at,\n                           COALESCE(\n                               (SELECT MAX(m.timestamp) FROM messages m\n                                WHERE m.session_id = s.id),\n                               s.started_at\n                           ) AS last_active,\n                           s.ended_at, s.message_count, s.archived\n                    FROM sessions s WHERE {where}\n                    ORDER BY last_active ASC, s.started_at ASC', params)
            return [dict(row) for row in cursor.fetchall()]

    def archive_sessions(self, older_than_days: Optional[float]=None, source: str=None, **filters) -> int:
        """Bulk-archive (soft-hide) every session matching the filters.

        Same filter surface as :meth:`prune_sessions`, but instead of deleting
        rows it flips ``archived = 1`` via :meth:`set_session_archived` so
        each match's compression lineage is archived as a unit (an unarchived
        compression root would otherwise resurrect the conversation in
        Desktop's projected list). Nothing is deleted; messages and transcript
        files are untouched. Returns the number of sessions matched.

        ``archived`` defaults to ``False`` here (only select rows not yet
        archived) so repeat runs are idempotent no-ops.
        """
        filters.setdefault('archived', False)
        rows = self.list_prune_candidates(older_than_days=older_than_days, source=source, **filters)
        for row in rows:
            self.set_session_archived(row['id'], True)
        return len(rows)

    def archive_stale_sessions(self, idle_days: float, *, exclude_pinned: bool=True) -> int:
        """Archive every session untouched for at least ``idle_days`` days.

        "Touched" is the freshest of ``last_activity_at`` and the latest
        message timestamp (else ``started_at``) — i.e. real recency, not
        creation time — so a session
        created long ago but active yesterday is spared, while an old
        abandoned one (even a still-open one) is swept. Unlike
        :meth:`archive_sessions`, this method can also archive unended
        sessions.

        Guards:
          * ``pinned = 0`` when ``exclude_pinned`` (the Desktop "keep" flag).
          * ``archived = 0`` so repeat runs are idempotent no-ops.
          * only lineage *tips* / standalone rows are candidates
            (``end_reason <> 'compression'``); a stale tip archives its whole
            chain via :meth:`set_session_archived`, so we never resurrect an
            active conversation by matching an old compressed-away root whose
            live continuation is recent.

        Returns the number of sessions archived. Never raises for an empty or
        non-positive ``idle_days`` — it simply archives nothing.
        """
        if idle_days is None or idle_days < 0:
            return 0
        cutoff = time.time() - float(idle_days) * 86400.0
        pin_clause = 'AND s.pinned = 0' if exclude_pinned else ''
        with self._lock:
            rows = self._conn.execute(f"\n                SELECT s.id FROM sessions s\n                WHERE s.archived = 0\n                  AND COALESCE(s.end_reason, '') <> 'compression'\n                  {pin_clause}\n                  AND {_sql_session_last_active('s')} < ?\n                ORDER BY s.started_at ASC\n                ", (cutoff,)).fetchall()
        ids = [r['id'] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
        for sid in ids:
            self.set_session_archived(sid, True)
        return len(ids)

    def prune_sessions(self, older_than_days: Optional[float]=90, source: str=None, sessions_dir: Optional[Path]=None, **filters) -> int:
        """Delete sessions matching the filters. Returns count deleted.

        By default, delete ended sessions inactive for
        ``older_than_days`` days, optionally restricted to ``source``.
        Activity is the latest message timestamp, falling back to
        ``started_at`` for sessions without messages. Additional keyword
        filters AND together — the full set is defined by
        :meth:`_prune_filter_where`:

        * ``last_active_before`` / ``last_active_after`` — epoch bounds on
          the latest message timestamp (falling back to ``started_at``).
        * ``started_before`` / ``started_after`` — epoch bounds on
          ``started_at``. An explicit ``started_before`` overrides the
          default ``older_than_days`` inactivity cutoff; pass
          ``older_than_days=None`` for no implicit upper age bound.
        * ``title_like`` / ``model_like`` / ``branch_like`` —
          case-insensitive substring matches.
        * ``end_reason`` / ``provider`` / ``user_id`` / ``chat_id`` /
          ``chat_type`` — exact matches (provider case-insensitive, against
          ``billing_provider``).
        * ``cwd_prefix`` — session cwd equals or is under this path.
        * ``min_messages`` / ``max_messages`` — bounds on message_count.
        * ``min_tokens`` / ``max_tokens`` — bounds on input+output tokens.
        * ``min_cost`` / ``max_cost`` — bounds on USD cost
          (actual, falling back to estimated).
        * ``min_tool_calls`` / ``max_tool_calls`` — bounds on tool_call_count.
        * ``archived`` — tri-state: None = both (default), True = only
          archived, False = only unarchived.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        if filters.get('last_active_before') is None and filters.get('started_before') is None and (older_than_days is not None):
            filters['last_active_before'] = time.time() - older_than_days * 86400
        where, where_params = self._prune_filter_where(source=source, **filters)
        removed_ids: list[str] = []

        def _do(conn):
            cursor = conn.execute(f'SELECT s.id FROM sessions s WHERE {where}', where_params)
            session_ids = {row['id'] for row in cursor.fetchall()}
            if not session_ids:
                return 0
            placeholders = ','.join('?' * len(session_ids))
            conn.execute(f'UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({placeholders})', list(session_ids))
            for sid in session_ids:
                conn.execute('DELETE FROM messages WHERE session_id = ?', (sid,))
                conn.execute('DELETE FROM sessions WHERE id = ?', (sid,))
                removed_ids.append(sid)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)
        count = self._execute_write(_do)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    def purge_stale_tool_call_markers(self, *, dry_run: bool=False, backup: bool=True) -> Dict[str, Any]:
        """Permanently clear bare tool-call marker content (e.g. "[memory]")
        left in the ``messages`` table by sessions persisted before the
        #78148 fix in ``agent.conversation_loop``.

        ``_strip_stale_tool_call_markers`` already repairs this in memory on
        every session load (see ``_rows_to_conversation``), so running this
        is optional — but for long-lived sessions the same rows get
        re-scanned and re-repaired on every resume, which is wasted work
        and keeps the contaminated bytes sitting in the DB (and in any
        downstream cache/backup snapshot of it) indefinitely. This rewrites
        the affected rows once, in place.

        Only the ``content`` column is touched — ``role``, ``tool_calls``,
        and every other column on the row are left exactly as they are, so
        provider tool_call/tool_result pairing is unaffected.

        Unlike the in-memory repair, this UPDATE is permanent and can't be
        undone from within the DB. Since ``backup`` defaults to True, a
        timestamped full snapshot is taken via ``VACUUM INTO`` (safe against
        a live connection, unlike the raw-copy ``_backup_db_file`` used for
        malformed-schema repair) before any row is touched — mirroring
        ``repair_state_db_schema``'s backup-by-default convention for
        destructive state.db operations. No snapshot is taken when there is
        nothing to change.

        With ``dry_run=True``, reports the affected row count/ids without
        writing or backing up (read-only, no write lock taken).

        Returns ``{"dry_run": bool, "rows_affected": int, "row_ids": [...],
        "backup_path": str|None}``.
        """

        def _find_affected(conn) -> List[int]:
            cursor = conn.execute("SELECT id, content FROM messages WHERE role = 'assistant' AND tool_calls IS NOT NULL AND tool_calls != ''")
            affected: List[int] = []
            for row in cursor.fetchall():
                content = row['content']
                if isinstance(content, str) and _STALE_TOOL_CALL_MARKER_RE.fullmatch(content.strip()):
                    affected.append(row['id'])
            return affected
        with self._read_ctx() as conn:
            affected_ids = _find_affected(conn)
        if dry_run:
            return {'dry_run': True, 'rows_affected': len(affected_ids), 'row_ids': affected_ids, 'backup_path': None}
        if not affected_ids:
            return {'dry_run': False, 'rows_affected': 0, 'row_ids': [], 'backup_path': None}
        backup_path: Optional[str] = None
        if backup:
            import datetime
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            dest = self.db_path.with_name(f'{self.db_path.name}.pre-clean-markers-backup-{stamp}')
            with self._lock:
                self._conn.execute('VACUUM INTO ?', (str(dest),))
            backup_path = str(dest)
            logger.info('Backed up state.db to %s before clean-markers write', backup_path)

        def _do(conn):
            ids = _find_affected(conn)
            if ids:
                placeholders = ','.join('?' * len(ids))
                conn.execute(f"UPDATE messages SET content = '' WHERE id IN ({placeholders})", ids)
            return ids
        affected_ids = self._execute_write(_do)
        if affected_ids:
            logger.info('Permanently cleared %d stale tool-call marker row(s) in state.db (#78148)', len(affected_ids))
        return {'dry_run': False, 'rows_affected': len(affected_ids), 'row_ids': affected_ids, 'backup_path': backup_path}

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the state_meta key/value store."""
        with self._lock:
            row = self._conn.execute('SELECT value FROM state_meta WHERE key = ?', (key,)).fetchone()
        if row is None:
            return None
        return row['value'] if isinstance(row, sqlite3.Row) else row[0]

    def set_meta(self, key: str, value: str, *, cursor: Optional[sqlite3.Cursor]=None) -> None:
        """Write a value to the state_meta key/value store.

        When ``cursor`` is provided the write is issued on that cursor
        inline (used during ``_init_schema``, which already holds an open
        transaction — routing through ``_execute_write`` there would nest
        BEGIN IMMEDIATE and deadlock). Otherwise a normal write transaction
        is used.
        """
        if cursor is not None:
            cursor.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
            return

        def _do(conn):
            conn.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
        self._execute_write(_do)

    def retag_kanban_worker_sessions(self, workspaces_root: str) -> int:
        """Retag legacy kanban worker rows from ``cli`` to ``kanban``.

        Workers used to spawn without ``HERMES_SESSION_SOURCE``, so their runs
        landed as untitled ``cli`` rows and the sidebar rendered one per attempt
        labeled with the worker's own prompt. New workers tag themselves; this
        reclaims the rows already on disk so they drop out of the session lists
        too. Identified by cwd under the board's workspaces root — a path only
        the dispatcher ever runs a session in.

        Gated per workspaces root (``state_meta``) so each board reclaims its
        own rows exactly once. Returns the number of rows retagged.
        """
        prefix = str(workspaces_root).rstrip('/\\')
        if not prefix:
            return 0
        gate = f'kanban_worker_source_retagged:{prefix}'
        if self.get_meta(gate) == '1':
            return 0

        def _do(conn):
            cursor = conn.execute("UPDATE sessions SET source = 'kanban' WHERE source = 'cli' AND (cwd = ? OR cwd LIKE ? ESCAPE '\\')", (prefix, _escape_like(prefix) + '/%'))
            retagged = cursor.rowcount or 0
            self.set_meta(gate, '1', cursor=cursor)
            return retagged
        return self._execute_write(_do)

    def apply_telegram_topic_migration(self) -> None:
        """Create Telegram DM topic-mode tables on explicit /topic opt-in.

        This migration is deliberately not part of automatic SessionDB startup
        reconciliation. Operators must be able to upgrade Duck Agent, keep the old
        Telegram bot behavior running, and only mutate topic-mode state when the
        user executes /topic to opt into the feature.

        Schema versions:
          v1 — initial shape (no ON DELETE CASCADE on session_id FK)
          v2 — session_id FK gets ON DELETE CASCADE so session pruning
               automatically clears bindings.
        """

        def _do(conn):
            conn.executescript("\n                CREATE TABLE IF NOT EXISTS telegram_dm_topic_mode (\n                    chat_id TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    enabled INTEGER NOT NULL DEFAULT 1,\n                    activated_at REAL NOT NULL,\n                    updated_at REAL NOT NULL,\n                    has_topics_enabled INTEGER,\n                    allows_users_to_create_topics INTEGER,\n                    capability_checked_at REAL,\n                    intro_message_id TEXT,\n                    pinned_message_id TEXT\n                );\n\n                CREATE TABLE IF NOT EXISTS telegram_dm_topic_bindings (\n                    chat_id TEXT NOT NULL,\n                    thread_id TEXT NOT NULL,\n                    user_id TEXT NOT NULL,\n                    session_key TEXT NOT NULL,\n                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n                    managed_mode TEXT NOT NULL DEFAULT 'auto',\n                    linked_at REAL NOT NULL,\n                    updated_at REAL NOT NULL,\n                    PRIMARY KEY (chat_id, thread_id)\n                );\n\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_session\n                ON telegram_dm_topic_bindings(session_id);\n\n                CREATE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_user\n                ON telegram_dm_topic_bindings(user_id, chat_id);\n                ")
            current = conn.execute('SELECT value FROM state_meta WHERE key = ?', ('telegram_dm_topic_schema_version',)).fetchone()
            current_version = int(current[0]) if current and str(current[0]).isdigit() else 0
            if current_version < 2:
                fk_rows = conn.execute("PRAGMA foreign_key_list('telegram_dm_topic_bindings')").fetchall()
                needs_rebuild = any((row[2] == 'sessions' and (row[6] or '') != 'CASCADE' for row in fk_rows))
                if needs_rebuild:
                    conn.executescript("\n                        CREATE TABLE telegram_dm_topic_bindings_new (\n                            chat_id TEXT NOT NULL,\n                            thread_id TEXT NOT NULL,\n                            user_id TEXT NOT NULL,\n                            session_key TEXT NOT NULL,\n                            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n                            managed_mode TEXT NOT NULL DEFAULT 'auto',\n                            linked_at REAL NOT NULL,\n                            updated_at REAL NOT NULL,\n                            PRIMARY KEY (chat_id, thread_id)\n                        );\n                        INSERT INTO telegram_dm_topic_bindings_new\n                            SELECT chat_id, thread_id, user_id, session_key,\n                                   session_id, managed_mode, linked_at, updated_at\n                            FROM telegram_dm_topic_bindings;\n                        DROP TABLE telegram_dm_topic_bindings;\n                        ALTER TABLE telegram_dm_topic_bindings_new\n                            RENAME TO telegram_dm_topic_bindings;\n                        CREATE UNIQUE INDEX idx_telegram_dm_topic_bindings_session\n                            ON telegram_dm_topic_bindings(session_id);\n                        CREATE INDEX idx_telegram_dm_topic_bindings_user\n                            ON telegram_dm_topic_bindings(user_id, chat_id);\n                        ")
            conn.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', ('telegram_dm_topic_schema_version', '2'))
        self._execute_write(_do)

    def enable_telegram_topic_mode(self, *, chat_id: str, user_id: str, has_topics_enabled: Optional[bool]=None, allows_users_to_create_topics: Optional[bool]=None) -> None:
        """Enable Telegram DM topic mode for one private chat/user.

        This method intentionally owns the explicit topic migration. Ordinary
        SessionDB startup must not create these side tables.
        """
        self.apply_telegram_topic_migration()
        now = time.time()

        def _to_int(value: Optional[bool]) -> Optional[int]:
            if value is None:
                return None
            return 1 if value else 0

        def _do(conn):
            conn.execute('\n                INSERT INTO telegram_dm_topic_mode (\n                    chat_id, user_id, enabled, activated_at, updated_at,\n                    has_topics_enabled, allows_users_to_create_topics,\n                    capability_checked_at\n                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)\n                ON CONFLICT(chat_id) DO UPDATE SET\n                    user_id = excluded.user_id,\n                    enabled = 1,\n                    updated_at = excluded.updated_at,\n                    has_topics_enabled = excluded.has_topics_enabled,\n                    allows_users_to_create_topics = excluded.allows_users_to_create_topics,\n                    capability_checked_at = excluded.capability_checked_at\n                ', (str(chat_id), str(user_id), now, now, _to_int(has_topics_enabled), _to_int(allows_users_to_create_topics), now))
        self._execute_write(_do)

    def disable_telegram_topic_mode(self, *, chat_id: str, clear_bindings: bool=True) -> None:
        """Disable Telegram DM topic mode for one private chat.

        When ``clear_bindings`` is True (default) the (chat_id, thread_id)
        bindings for this chat are also cleared so re-enabling later
        starts from a clean slate. Set to False if the operator wants to
        preserve bindings for a later re-enable.

        Never creates the topic-mode tables from scratch; if they don't
        exist there is nothing to disable and the call is a no-op.
        """

        def _do(conn):
            try:
                conn.execute('UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = ? WHERE chat_id = ?', (time.time(), str(chat_id)))
                if clear_bindings:
                    conn.execute('DELETE FROM telegram_dm_topic_bindings WHERE chat_id = ?', (str(chat_id),))
            except sqlite3.OperationalError:
                return
        self._execute_write(_do)

    def is_telegram_topic_mode_enabled(self, *, chat_id: str, user_id: str) -> bool:
        """Return whether Telegram DM topic mode is enabled for this chat/user."""
        with self._lock:
            try:
                row = self._conn.execute('\n                    SELECT enabled FROM telegram_dm_topic_mode\n                    WHERE chat_id = ? AND user_id = ?\n                    ', (str(chat_id), str(user_id))).fetchone()
            except sqlite3.OperationalError:
                return False
        if row is None:
            return False
        enabled = row['enabled'] if isinstance(row, sqlite3.Row) else row[0]
        return bool(enabled)

    def get_telegram_topic_binding(self, *, chat_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
        """Return the session binding for a Telegram DM topic, if present."""
        with self._lock:
            try:
                row = self._conn.execute('\n                    SELECT * FROM telegram_dm_topic_bindings\n                    WHERE chat_id = ? AND thread_id = ?\n                    ', (str(chat_id), str(thread_id))).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def list_telegram_topic_bindings_for_chat(self, *, chat_id: str) -> List[Dict[str, Any]]:
        """All Telegram DM topic bindings for one chat, newest first.

        Read-only; returns [] if the bindings table doesn't exist yet
        (does not trigger the topic-mode migration).
        """
        with self._lock:
            try:
                rows = self._conn.execute('SELECT * FROM telegram_dm_topic_bindings WHERE chat_id = ? ORDER BY updated_at DESC', (str(chat_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def get_telegram_topic_binding_by_session(self, *, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the Telegram DM topic binding for a given session_id, if present.

        Uses the UNIQUE INDEX on telegram_dm_topic_bindings(session_id) for an
        efficient reverse lookup. Returns None when the session has no binding or
        the table does not exist yet.
        """
        with self._lock:
            try:
                row = self._conn.execute('\n                    SELECT * FROM telegram_dm_topic_bindings\n                    WHERE session_id = ?\n                    ', (str(session_id),)).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def delete_telegram_topic_binding(self, *, chat_id: str, thread_id: str) -> int:
        """Remove the binding row for a single (chat, thread) pair.

        Called when the Telegram Bot API confirms a topic was deleted
        externally (``Thread not found`` after the same-thread retry
        already failed).  Without this prune, the stale row keeps
        living in ``telegram_dm_topic_bindings`` and the
        recovery logic in ``gateway.run._recover_telegram_topic_thread_id``
        cheerfully redirects future inbound messages to the deleted
        topic, causing tool progress, approvals, and replies to land
        in the wrong place.  Issue #31501.

        When this prune removes the chat's *last* remaining binding,
        the chat's row in ``telegram_dm_topic_mode`` is also flipped to
        ``enabled = 0`` in the same transaction.  Otherwise the chat
        would be left in topic mode with zero lanes — and
        ``gateway.run._recover_telegram_topic_thread_id`` keeps treating
        the chat as topic-enabled, lobby messages keep hunting for a
        binding that no longer exists, and a user who disabled topics in
        the Telegram client (rather than via ``/topic off``) stays stuck
        until the next send happens to fail. Clearing the flag makes
        recovery fully stand down once the dead topics are gone.

        Returns the number of binding rows deleted (0 when the binding
        was already absent or the topic-mode tables haven't been
        migrated yet — both are silent no-ops; we never raise from
        a cleanup hot path).
        """
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        deleted = {'count': 0}

        def _do(conn):
            try:
                cursor = conn.execute('\n                    DELETE FROM telegram_dm_topic_bindings\n                    WHERE chat_id = ? AND thread_id = ?\n                    ', (chat_id, thread_id))
                deleted['count'] = cursor.rowcount or 0
            except sqlite3.OperationalError:
                deleted['count'] = 0
                return
            if not deleted['count']:
                return
            try:
                remaining = conn.execute('\n                    SELECT 1 FROM telegram_dm_topic_bindings\n                    WHERE chat_id = ? LIMIT 1\n                    ', (chat_id,)).fetchone()
                if remaining is None:
                    conn.execute('UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = ? WHERE chat_id = ?', (time.time(), chat_id))
            except sqlite3.OperationalError:
                pass
        self._execute_write(_do)
        return deleted['count']

    def bind_telegram_topic(self, *, chat_id: str, thread_id: str, user_id: str, session_key: str, session_id: str, managed_mode: str='auto') -> None:
        """Bind one Telegram DM topic thread to one Duck Agent session.

        A Duck Agent session may only be linked to one Telegram topic in MVP.
        Rebinding the same topic to the same session is idempotent; trying to
        link the same session to a different topic raises ValueError.
        """
        self.apply_telegram_topic_migration()
        now = time.time()
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        user_id = str(user_id)
        session_key = str(session_key)
        session_id = str(session_id)

        def _do(conn):
            existing_session = conn.execute('\n                SELECT chat_id, thread_id FROM telegram_dm_topic_bindings\n                WHERE session_id = ?\n                ', (session_id,)).fetchone()
            if existing_session is not None:
                linked_chat = existing_session['chat_id'] if isinstance(existing_session, sqlite3.Row) else existing_session[0]
                linked_thread = existing_session['thread_id'] if isinstance(existing_session, sqlite3.Row) else existing_session[1]
                if str(linked_chat) != chat_id or str(linked_thread) != thread_id:
                    raise ValueError('session is already linked to another Telegram topic')
            conn.execute('\n                INSERT INTO telegram_dm_topic_bindings (\n                    chat_id, thread_id, user_id, session_key, session_id,\n                    managed_mode, linked_at, updated_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(chat_id, thread_id) DO UPDATE SET\n                    user_id = excluded.user_id,\n                    session_key = excluded.session_key,\n                    session_id = excluded.session_id,\n                    managed_mode = excluded.managed_mode,\n                    updated_at = excluded.updated_at\n                ', (chat_id, thread_id, user_id, session_key, session_id, managed_mode, now, now))
        self._execute_write(_do)

    def is_telegram_session_linked_to_topic(self, *, session_id: str) -> bool:
        """Return True if a Duck Agent session is already bound to any Telegram DM topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables have not been created yet (i.e. nobody has run
        ``/topic`` in this profile), the session is by definition unbound
        and we return False.
        """
        with self._lock:
            try:
                row = self._conn.execute('\n                    SELECT 1 FROM telegram_dm_topic_bindings\n                    WHERE session_id = ?\n                    LIMIT 1\n                    ', (str(session_id),)).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def list_unlinked_telegram_sessions_for_user(self, *, chat_id: str, user_id: str, limit: int=10) -> List[Dict[str, Any]]:
        """List previous Telegram sessions for this user that are not bound to a topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables are absent, fall back to a simpler query that
        just returns this user's Telegram sessions — there can't be any
        bindings yet.
        """
        with self._lock:
            try:
                rows = self._conn.execute(f"\n                    SELECT s.*,\n                        COALESCE(sp.prompt, s.system_prompt)\n                            AS _system_prompt_resolved,\n                        COALESCE(\n                            (SELECT {_PREVIEW_RAW_SELECT}\n                             FROM messages m\n                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL\n                             ORDER BY m.timestamp, m.id LIMIT 1),\n                            ''\n                        ) AS _preview_raw,\n                        {_sql_session_last_active('s')} AS last_active\n                    FROM sessions s\n                    LEFT JOIN system_prompts sp\n                      ON sp.hash = s.system_prompt_hash\n                    WHERE s.source = 'telegram'\n                      AND s.user_id = ?\n                      AND NOT EXISTS (\n                          SELECT 1 FROM telegram_dm_topic_bindings b\n                          WHERE b.session_id = s.id\n                      )\n                    ORDER BY last_active DESC, s.started_at DESC\n                    LIMIT ?\n                    ", (str(user_id), int(limit))).fetchall()
            except sqlite3.OperationalError:
                rows = self._conn.execute(f"\n                    SELECT s.*,\n                        COALESCE(sp.prompt, s.system_prompt)\n                            AS _system_prompt_resolved,\n                        COALESCE(\n                            (SELECT {_PREVIEW_RAW_SELECT}\n                             FROM messages m\n                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL\n                             ORDER BY m.timestamp, m.id LIMIT 1),\n                            ''\n                        ) AS _preview_raw,\n                        {_sql_session_last_active('s')} AS last_active\n                    FROM sessions s\n                    LEFT JOIN system_prompts sp\n                      ON sp.hash = s.system_prompt_hash\n                    WHERE s.source = 'telegram'\n                      AND s.user_id = ?\n                    ORDER BY last_active DESC, s.started_at DESC\n                    LIMIT ?\n                    ", (str(user_id), int(limit))).fetchall()
        sessions: List[Dict[str, Any]] = []
        for row in rows:
            session = self._session_row_dict(row)
            session['preview'] = _shape_preview(session.pop('_preview_raw', ''))
            sessions.append(session)
        return sessions
    _FTS_TABLES = ('messages_fts', 'messages_fts_trigram', 'messages_fts_cjk')

    def logical_size_bytes(self) -> Optional[int]:
        """Database size in bytes as SQLite itself accounts for it.

        ``page_count * page_size`` — the size the main DB file will have once
        the WAL is checkpointed back into it.

        Prefer this over ``os.path.getsize(db_path)`` when reporting the effect
        of a VACUUM. In WAL mode a VACUUM's rewrite lands in the ``-wal`` file,
        and the checkpoint that folds it back is refused while any other
        connection (a live gateway) holds a read-mark. Until that happens the
        main file on disk still carries its pre-VACUUM size and keeps growing,
        so a stat()-based before/after delta understates the win and can go
        negative — the "reclaimed -3820.1 MB" report on a database that had
        actually shrunk 60%.

        Returns None if the pragmas cannot be read.
        """
        try:
            with self._lock:
                if self._conn is None:
                    return None
                page_count = self._conn.execute('PRAGMA page_count').fetchone()[0]
                page_size = self._conn.execute('PRAGMA page_size').fetchone()[0]
            return int(page_count) * int(page_size)
        except Exception as exc:
            logger.debug('Could not read logical DB size: %s', exc)
            return None

    def vacuum(self) -> int:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.

        FTS5 segments are merged first via :meth:`optimize_fts` so the
        subsequent VACUUM reclaims the pages freed by the merge. This is a
        layout-only optimization — search results are unchanged.

        Returns the number of FTS indexes that were optimized (0 if the
        merge step failed or no FTS tables exist).
        """
        optimized = 0
        try:
            optimized = self.optimize_fts()
        except Exception as exc:
            logger.warning('FTS optimize before VACUUM failed: %s', exc)
        with self._lock:
            try:
                self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except Exception as exc:
                logger.debug('WAL checkpoint (TRUNCATE) before VACUUM failed: %s', exc)
            self._conn.execute('VACUUM')
        return optimized

    def maybe_auto_prune_and_vacuum(self, retention_days: int=90, min_interval_hours: int=24, vacuum: bool=True, sessions_dir: Optional[Path]=None, min_vacuum_interval_days: int=30) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune inactive sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. VACUUM has its own, typically
        longer, throttle controlled by ``min_vacuum_interval_days`` so routine
        pruning does not repeatedly rewrite the database. Designed to be
        called once at startup from long-lived entrypoints (CLI, gateway, cron
        scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {'skipped': False, 'pruned': 0, 'vacuumed': False}
        try:
            last_raw = self.get_meta('last_auto_prune')
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result['skipped'] = True
                        return result
                except (TypeError, ValueError):
                    pass
            pruned = self.prune_sessions(older_than_days=retention_days, sessions_dir=sessions_dir)
            result['pruned'] = pruned
            last_vacuum_raw = self.get_meta('last_vacuum')
            vacuum_due = True
            if last_vacuum_raw:
                try:
                    vacuum_due = now - float(last_vacuum_raw) >= min_vacuum_interval_days * 86400
                except (TypeError, ValueError):
                    vacuum_due = True
            if vacuum and pruned > 0 and vacuum_due:
                try:
                    self.vacuum()
                    result['vacuumed'] = True
                    self.set_meta('last_vacuum', str(now))
                except Exception as exc:
                    logger.warning('state.db VACUUM failed: %s', exc)
            self.set_meta('last_auto_prune', str(now))
            if pruned > 0:
                logger.info('state.db auto-maintenance: pruned %d session(s) inactive for %d days%s', pruned, retention_days, ' + VACUUM' if result['vacuumed'] else '')
        except Exception as exc:
            logger.warning('state.db auto-maintenance failed: %s', exc)
            result['error'] = str(exc)
        return result

    def maybe_auto_archive(self, idle_days: float=3, min_interval_hours: int=24, exclude_pinned: bool=True) -> Dict[str, Any]:
        """Idempotent auto-archive: soft-hide sessions idle for ``idle_days``.

        Sibling of :meth:`maybe_auto_prune_and_vacuum` but non-destructive —
        it archives (hides) rather than deletes, and ages on last activity
        (see :meth:`archive_stale_sessions`) rather than creation. Records the
        last run in ``state_meta['last_auto_archive']`` so calls within
        ``min_interval_hours`` no-op; safe to call opportunistically (startup
        hooks, or when the Desktop backend lists sessions).

        Never raises. Returns a dict with:
          - ``"skipped"`` (bool) — within min_interval_hours of last run
          - ``"archived"`` (int) — sessions archived this run
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {'skipped': False, 'archived': 0}
        try:
            last_raw = self.get_meta('last_auto_archive')
            now = time.time()
            if last_raw:
                try:
                    if now - float(last_raw) < min_interval_hours * 3600:
                        result['skipped'] = True
                        return result
                except (TypeError, ValueError):
                    pass
            archived = self.archive_stale_sessions(idle_days, exclude_pinned=exclude_pinned)
            result['archived'] = archived
            self.set_meta('last_auto_archive', str(now))
            if archived > 0:
                logger.info('state.db auto-archive: archived %d session(s) idle >= %s days', archived, idle_days)
        except Exception as exc:
            logger.warning('state.db auto-archive failed: %s', exc)
            result['error'] = str(exc)
        return result

    def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state.
        """

        def _do(conn):
            cur = conn.execute("UPDATE sessions SET handoff_state = 'pending',     handoff_platform = ?,     handoff_error = NULL WHERE id = ? AND (handoff_state IS NULL                   OR handoff_state IN ('completed', 'failed'))", (platform, session_id))
            return cur.rowcount > 0
        return self._execute_write(_do)

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session has
        no handoff record.
        """
        try:
            cur = self._conn.execute('SELECT handoff_state, handoff_platform, handoff_error FROM sessions WHERE id = ?', (session_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {'state': row['handoff_state'], 'platform': row['handoff_platform'], 'error': row['handoff_error']}
        except Exception:
            return None

    def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            cur = self._conn.execute("SELECT s.*, COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved FROM sessions s LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash WHERE s.handoff_state = 'pending' ORDER BY s.started_at ASC")
            return [self._session_row_dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""

        def _do(conn):
            cur = conn.execute("UPDATE sessions SET handoff_state = 'running' WHERE id = ? AND handoff_state = 'pending'", (session_id,))
            return cur.rowcount > 0
        return self._execute_write(_do)

    def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""

        def _do(conn):
            conn.execute("UPDATE sessions SET handoff_state = 'completed', handoff_error = NULL WHERE id = ?", (session_id,))
        self._execute_write(_do)

    def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""

        def _do(conn):
            conn.execute("UPDATE sessions SET handoff_state = 'failed', handoff_error = ? WHERE id = ?", (error[:500], session_id))
        self._execute_write(_do)

class AsyncSessionDB:
    """Async door onto SessionDB: offloads each call via asyncio.to_thread so a blocking SQLite call never freezes the event loop. Generic forwarder — the audit confirms no method returns a live cursor/generator."""

    def __init__(self, db: 'SessionDB') -> None:
        self._db = db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)
        return _offloaded