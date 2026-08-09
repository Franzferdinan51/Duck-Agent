"""Full-text / trigram / CJK message search and FTS maintenance for SessionDB.

Mixin contract: this is a plain mixin class consumed by
``hermes_state.SessionDB``. It defines no ``__init__`` and no state of its
own; methods access the host's attributes (``self._conn``, ``self.db_path``,
``self._execute_write`` and other SessionDB methods) established by
``SessionDB.__init__``. It must never import hermes_state (cycle) — shared
module-level constants live in hermes_state_common.
"""
import logging
import json
import os
import re
import sqlite3
import time
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple
from agent.skill_commands import describe_skill_invocation
from hermes_state_common import FTS_CJK_STALE_KEY, FTS_SQL, FTS_STORAGE_VERSION, FTS_TRIGRAM_SQL, MAX_FTS5_QUERY_CHARS, SCHEMA_VERSION, _FTS_CJK_TRIGGERS, escape_like as _escape_like
logger = logging.getLogger('hermes_state')

class SessionSearchMixin:
    """See module docstring — mixin for SessionDB (Search cluster)."""
    _SEARCH_MESSAGE_RESULT_FIELDS = ('id', 'session_id', 'role', 'snippet', 'timestamp', 'tool_name', 'source', 'model', 'session_started', 'context')

    @classmethod
    def _search_message_fields(cls, fields: Optional[Collection[str]]) -> Optional[Tuple[str, ...]]:
        """Validate and canonically order an optional result projection."""
        if fields is None:
            return None
        if isinstance(fields, str):
            raise TypeError('search fields must be a collection of field names, not a string')
        requested = set(fields)
        unknown = requested.difference(cls._SEARCH_MESSAGE_RESULT_FIELDS)
        if unknown:
            raise ValueError(f"unknown search result field(s): {', '.join(sorted(unknown))}")
        return tuple((field for field in cls._SEARCH_MESSAGE_RESULT_FIELDS if field in requested))

    def _try_incremental_merge_fts(self) -> None:
        """Run one bounded FTS5 merge pass without failing the completed write."""
        if not self._fts_enabled:
            return
        try:
            self._merge_fts_incrementally(max_pages=self._FTS_MERGE_MAX_PAGES_PER_INDEX)
        except sqlite3.Error as exc:
            logger.warning('FTS incremental merge failed: %s', exc)

    def fts_rebuild_status(self) -> Optional[Dict[str, Any]]:
        """Return deferred-rebuild progress, or None when no rebuild pending.

        Shape: {"pending": True, "total": <rows at drop time>,
        "indexed": <rows backfilled>, "percent": <0-100 int>}.
        Consumed by search_messages() notes and by status surfaces
        (dashboard/desktop can poll this to render a progress indicator).

        Reads state_meta directly via _read_ctx instead of calling
        get_meta() (which takes self._lock) so search_messages doesn't
        block on the writer lock when checking rebuild status.
        """
        with self._read_ctx() as conn:
            row = conn.execute('SELECT key, value FROM state_meta WHERE key IN (?, ?)', ('fts_rebuild_high_water', 'fts_rebuild_progress')).fetchall()
        meta = {r['key']: r['value'] for r in row}
        high_water = meta.get('fts_rebuild_high_water')
        if high_water is None:
            return None
        progress = int(meta.get('fts_rebuild_progress') or 0)
        total = int(high_water)
        if total <= 0:
            return None
        pct = min(100, int(100 * progress / total))
        return {'pending': True, 'total': total, 'indexed': progress, 'percent': pct}

    def _fts_rebuild_finish(self) -> None:
        """Finalize the deferred rebuild: boundary sweep + clear markers.

        The sweep is cheap insurance against any write that slipped through
        the migration-boundary instant (between high_water capture and
        trigger activation): re-index any row near the boundary that the
        index is missing. docsize has one row per indexed doc, so the
        anti-join is exact and runs on a narrow id range.

        The trigram half of the sweep is gated on ``self._trigram_available``
        for the same reason ``fts_rebuild_step()`` gates its backfill INSERT:
        when the SQLite build has no trigram tokenizer (or the table was
        never created), an unconditional INSERT raises ``no such table``
        and aborts the whole rebuild — taking ``optimize_fts_storage()``
        down with it.
        """
        include_trigram = self._trigram_available

        def _do(conn):
            hw_row = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_rebuild_high_water'").fetchone()
            if hw_row is not None:
                hw = int(hw_row[0])
                lo, hi = (hw - 1000, hw + 1000)
                conn.execute('INSERT INTO messages_fts(rowid, content, tool_name, tool_calls) SELECT m.id, m.content, m.tool_name, m.tool_calls FROM messages m WHERE m.id > ? AND m.id <= ? AND NOT EXISTS (SELECT 1 FROM messages_fts_docsize d WHERE d.id = m.id)', (lo, hi))
                if include_trigram:
                    conn.execute("INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls) SELECT m.id, m.content, m.tool_name, m.tool_calls FROM messages m WHERE m.id > ? AND m.id <= ? AND m.role <> 'tool' AND NOT EXISTS (SELECT 1 FROM messages_fts_trigram_docsize d WHERE d.id = m.id)", (lo, hi))
            conn.execute("DELETE FROM state_meta WHERE key IN ('fts_rebuild_high_water', 'fts_rebuild_progress')")
        self._execute_write(_do)
        logger.info('Deferred FTS rebuild complete — all messages indexed.')

    def _fts_teardown_trash_step(self) -> bool:
        """Tear down one chunk of a demoted v22 FTS shadow table.

        The trash tables are PLAIN tables (their vtable parent was demoted
        away during the migration), so chunked DELETE + final DROP involve
        no FTS5 machinery at all. Returns True while teardown work remains.

        Single-column-key trash tables (the common shape — FTS shadow
        tables carry a rowid/integer PK) are drained with a high-water
        marker mirroring :meth:`fts_rebuild_step`: each chunk deletes only
        rows after the previously-drained key, so the per-chunk scan is
        bounded instead of re-scanning from the start of the table every
        chunk (O(n²) total on large trash tables, #79324). Compound-key
        trash tables (multi-column PK) cannot use a scalar high-water
        comparison, so they keep the legacy chunked ``LIMIT`` delete —
        those shadow tables are small by construction.
        """
        with self._lock:
            trash = [r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? ESCAPE '\\'", (self._FTS_TRASH_PREFIX.replace('_', '\\_') + '%',)).fetchall()]
        if not trash:
            return False
        tbl = trash[0]

        def _do(conn):
            pk_info = [(r[1], (r[2] or '').upper()) for r in conn.execute(f'PRAGMA table_info({tbl})') if r[5] > 0]
            pk_cols = [name for name, _typ in pk_info]
            key = ', '.join(pk_cols) if pk_cols else 'rowid'
            if len(pk_cols) == 1 and (not pk_info or pk_info[0][1] == 'INTEGER'):
                marker_key = f'fts_teardown_{tbl}_progress'
                row = conn.execute('SELECT value FROM state_meta WHERE key = ?', (marker_key,)).fetchone()
                high_water = int(row[0]) if row is not None else 0
                upper_rows = conn.execute(f'SELECT {key} FROM {tbl} WHERE {key} > ? ORDER BY {key} LIMIT {self._FTS_REBUILD_CHUNK_ROWS}', (high_water,)).fetchall()
                if not upper_rows:
                    conn.execute(f'DROP TABLE IF EXISTS {tbl}')
                    conn.execute('DELETE FROM state_meta WHERE key = ?', (marker_key,))
                    logger.info('Old FTS shadow table %s torn down.', tbl)
                    return True
                upper = upper_rows[-1][0]
                cur = conn.execute(f'DELETE FROM {tbl} WHERE {key} > ? AND {key} <= ?', (high_water, upper))
                if cur.rowcount > 0:
                    conn.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (marker_key, str(upper)))
                return True
            cur = conn.execute(f'DELETE FROM {tbl} WHERE ({key}) IN (SELECT {key} FROM {tbl} LIMIT {self._FTS_REBUILD_CHUNK_ROWS})')
            if cur.rowcount == 0:
                conn.execute(f'DROP TABLE IF EXISTS {tbl}')
                logger.info('Old FTS shadow table %s torn down.', tbl)
            return True
        try:
            return bool(self._execute_write(_do))
        except sqlite3.OperationalError as exc:
            logger.debug('FTS trash teardown chunk failed (will retry): %s', exc)
            return True

    def fts_rebuild_step(self) -> bool:
        """Backfill one chunk of the deferred FTS rebuild.

        Returns True when more work remains, False when the rebuild is
        complete (or none is pending). Safe to call from any process at any
        time; chunks are claimed atomically inside the write transaction, so
        concurrent callers interleave instead of duplicating rows.
        """
        if not self._fts_enabled:
            return False
        high_water_raw = self.get_meta('fts_rebuild_high_water')
        if high_water_raw is None:
            return False
        high_water = int(high_water_raw)
        include_trigram = self._trigram_available
        chunk = self._FTS_REBUILD_CHUNK_ROWS

        def _do(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_rebuild_progress'").fetchone()
            if row is None:
                return False
            progress = int(row[0])
            if progress >= high_water:
                return False
            upper = min(progress + chunk, high_water)
            conn.execute('INSERT INTO messages_fts(rowid, content, tool_name, tool_calls) SELECT id, content, tool_name, tool_calls FROM messages WHERE id > ? AND id <= ?', (progress, upper))
            if include_trigram:
                conn.execute("INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls) SELECT id, content, tool_name, tool_calls FROM messages WHERE id > ? AND id <= ? AND role <> 'tool'", (progress, upper))
            conn.execute("UPDATE state_meta SET value = ? WHERE key = 'fts_rebuild_progress'", (str(upper),))
            return upper < high_water
        try:
            more = self._execute_write(_do)
        except sqlite3.OperationalError as exc:
            logger.debug('FTS rebuild chunk failed (will retry): %s', exc)
            return True
        if more is False:
            status = self.fts_rebuild_status()
            if status is not None and status['indexed'] >= status['total']:
                self._fts_rebuild_finish()
            return False
        return bool(more)

    def fts_cjk_rebuild_status(self) -> Optional[Dict[str, Any]]:
        """CJK-index backfill progress, or None when none is pending."""
        with self._read_ctx() as conn:
            row = conn.execute('SELECT key, value FROM state_meta WHERE key IN (?, ?)', ('fts_cjk_rebuild_high_water', 'fts_cjk_rebuild_progress')).fetchall()
        meta = {r['key']: r['value'] for r in row}
        high_water = meta.get('fts_cjk_rebuild_high_water')
        if high_water is None:
            return None
        progress = int(meta.get('fts_cjk_rebuild_progress') or 0)
        total = int(high_water)
        if total <= 0:
            return None
        pct = min(100, int(100 * progress / total))
        return {'pending': True, 'total': total, 'indexed': progress, 'percent': pct}

    def fts_cjk_rebuild_step(self) -> bool:
        """Backfill one chunk of the CJK index. True while work remains."""
        if not self._fts_enabled or not self._fts_cjk_loaded:
            return False
        high_water_raw = self.get_meta('fts_cjk_rebuild_high_water')
        if high_water_raw is None:
            return False
        high_water = int(high_water_raw)
        chunk = self._FTS_REBUILD_CHUNK_ROWS

        def _do(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_cjk_rebuild_progress'").fetchone()
            if row is None:
                return False
            progress = int(row[0])
            if progress >= high_water:
                return False
            upper = min(progress + chunk, high_water)
            conn.execute("INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls) SELECT id, content, tool_name, tool_calls FROM messages WHERE id > ? AND id <= ? AND role <> 'tool'", (progress, upper))
            conn.execute("UPDATE state_meta SET value = ? WHERE key = 'fts_cjk_rebuild_progress'", (str(upper),))
            return upper < high_water
        try:
            more = self._execute_write(_do)
        except sqlite3.OperationalError as exc:
            logger.debug('CJK FTS rebuild chunk failed (will retry): %s', exc)
            return True
        if more is False:
            status = self.fts_cjk_rebuild_status()
            if status is not None and status['indexed'] >= status['total']:
                self._fts_cjk_rebuild_finish()
            return False
        return bool(more)

    def _fts_cjk_rebuild_finish(self) -> None:
        """Boundary sweep + clear the cjk markers; index becomes servable."""

        def _do(conn):
            hw_row = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_cjk_rebuild_high_water'").fetchone()
            if hw_row is not None:
                hw = int(hw_row[0])
                lo, hi = (hw - 1000, hw + 1000)
                conn.execute("INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls) SELECT m.id, m.content, m.tool_name, m.tool_calls FROM messages m WHERE m.id > ? AND m.id <= ? AND m.role <> 'tool' AND NOT EXISTS (SELECT 1 FROM messages_fts_cjk_docsize d WHERE d.id = m.id)", (lo, hi))
            conn.execute("DELETE FROM state_meta WHERE key IN ('fts_cjk_rebuild_high_water', 'fts_cjk_rebuild_progress')")
        self._execute_write(_do)
        self._fts_cjk_available = True
        logger.info('CJK FTS index backfill complete — serving CJK search.')

    def _fts_cjk_reset_if_stale(self) -> None:
        """Rebuild path for a stale cjk index (triggers were dropped).

        The gap's extent is unknown, so the only safe recovery is a from-
        scratch rebuild: drop the table + triggers, clear the breadcrumb,
        recreate via ``_ensure_fts_cjk_schema`` (which sets fresh backfill
        markers on a populated DB). Called from ``optimize_fts_storage`` on
        a tokenizer-capable host; no-op when not stale.
        """
        if not self._fts_cjk_loaded:
            return

        def _do(conn):
            stale = conn.execute('SELECT 1 FROM state_meta WHERE key = ?', (FTS_CJK_STALE_KEY,)).fetchone()
            if not stale:
                return False
            for trig in _FTS_CJK_TRIGGERS:
                conn.execute(f'DROP TRIGGER IF EXISTS {trig}')
            conn.execute('DROP TABLE IF EXISTS messages_fts_cjk')
            conn.execute('DROP VIEW IF EXISTS messages_fts_cjk_src')
            conn.execute(f"DELETE FROM state_meta WHERE key IN ('{FTS_CJK_STALE_KEY}', 'fts_cjk_rebuild_high_water', 'fts_cjk_rebuild_progress')")
            return True
        was_stale = self._execute_write(_do)
        if was_stale:
            with self._lock:
                self._ensure_fts_cjk_schema(self._conn)
                self._conn.commit()

    def _fts_external_index_empty_with_messages(self, conn) -> bool:
        """True when the base FTS table exists but indexes nothing while
        ``messages`` has rows. Caller must hold ``self._lock``.

        This is the post-demote empty-index shape: external-content FTS with
        zero ``messages_fts_docsize`` rows against a non-empty messages table.
        Healthy installs (and mid-backfill installs that still hold markers)
        never match.
        """
        try:
            has_msg = conn.execute('SELECT EXISTS(SELECT 1 FROM messages)').fetchone()[0]
            if not has_msg:
                return False
            has_fts = conn.execute('SELECT EXISTS(SELECT 1 FROM messages_fts_docsize)').fetchone()[0]
            return not has_fts
        except sqlite3.OperationalError:
            return False

    def _fts_index_known_empty(self, conn) -> bool:
        """True when the base external-content index holds no rows.

        A missing table counts as empty: the schema ensure that follows
        creates it fresh.
        """
        try:
            n = conn.execute('SELECT COUNT(*) FROM messages_fts_docsize').fetchone()[0]
            return int(n) == 0
        except sqlite3.OperationalError:
            return True

    def _reset_fts_index_to_empty(self, conn) -> None:
        """Delete every indexed row from the v23 external-content tables.

        Uses the FTS5 ``'delete-all'`` special command — the documented O(1)
        truncate for external-content tables. A plain no-WHERE ``DELETE`` is
        O(rows) on external-content FTS5 (each row's delete tokens are
        regenerated from the content table; measured ~12µs/row, minutes on a
        large index, while holding the write lock) and corrupts the index if
        indexed rows have diverged from ``messages`` — precisely the broken-
        bookkeeping shape this repair path handles. The backfill chunk worker
        replays its whole selected id range with no anti-join, so a replay
        from zero is only safe once the index is known empty — this is how a
        partially indexed DB gets there.
        """
        for tbl in ('messages_fts', 'messages_fts_trigram'):
            try:
                conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES('delete-all')")
            except sqlite3.OperationalError:
                pass

    def _seed_fts_rebuild_markers(self, conn, *, force: bool=False) -> int:
        """Write ``fts_rebuild_high_water`` / ``fts_rebuild_progress`` for a
        full backfill. Returns the high-water id.

        When ``force`` is False and high_water is already set, only repairs a
        missing progress key (stuck no-op when high_water exists alone), and
        only after the index is known empty: the chunk worker replays its
        whole selected id range without an anti-join, so a partially indexed
        DB is first reset to a known-empty surface rather than rebuilt from
        zero on top of surviving rows. Caller must hold the write
        transaction / lock as appropriate.
        """
        existing_hw = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_rebuild_high_water'").fetchone()
        if existing_hw is not None and (not force):
            hw = int(existing_hw[0])
            progress = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_rebuild_progress'").fetchone()
            if progress is None:
                if not self._fts_index_known_empty(conn):
                    self._reset_fts_index_to_empty(conn)
                conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_progress', '0') ON CONFLICT(key) DO UPDATE SET value = excluded.value")
            return hw
        hw = conn.execute('SELECT COALESCE(MAX(id), 0) FROM messages').fetchone()[0]
        for k, v in (('fts_rebuild_high_water', str(hw)), ('fts_rebuild_progress', '0')):
            conn.execute('INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (k, v))
        return int(hw)

    def _repair_optimize_bookkeeping(self) -> None:
        """Heal interrupted demote/backfill bookkeeping before optimize runs.

        Covers two post-#65798 failure classes:

        1. Empty external-content index with messages present and no rebuild
           markers (demote crash window after empty v23 tables landed but
           before markers, or settle that stamped without backfill). Seed a
           full backfill.
        2. ``fts_rebuild_high_water`` present without ``fts_rebuild_progress``
           (partial meta) — seed progress so the chunk loop is not a no-op,
           resetting a partially populated index to a known-empty surface
           first so the anti-join-free chunk replay cannot duplicate rows.

        Must not invent markers on a still-legacy inline DB: that would make
        ``optimize_fts_storage`` skip demote (``legacy and not pending``) and
        attempt v23-shaped INSERTs against the inline table forever.
        """

        def _do(conn):
            existing_hw = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_rebuild_high_water'").fetchone()
            if existing_hw is not None:
                progress = conn.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_progress'").fetchone()
                if progress is None:
                    if not self._fts_index_known_empty(conn):
                        self._reset_fts_index_to_empty(conn)
                    conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_progress', '0') ON CONFLICT(key) DO UPDATE SET value = '0'")
                return
            if self._db_has_legacy_inline_fts(conn):
                return
            if self._fts_external_index_empty_with_messages(conn):
                conn.execute("DELETE FROM state_meta WHERE key = 'fts_storage_version'")
                self._seed_fts_rebuild_markers(conn, force=True)
        self._execute_write(_do)

    def fts_optimize_available(self) -> bool:
        """True when `optimize_fts_storage()` has work to do: either this DB
        is a legacy inline-FTS install that can be optimized to the v23
        external-content schema, or a previous optimize run was interrupted
        (legacy vtables already demoted, but backfill markers and/or trash
        tables remain) and re-running would resume it, or the CJK-bigram
        index needs a backfill/rebuild on this tokenizer-capable host, or
        a prior demote left an empty external-content index without markers
        (healable on re-run).
        False for fresh and fully-optimized installs (and when FTS5 is
        unavailable)."""
        if not self._fts_enabled or self.read_only:
            return False
        with self._lock:
            if self._db_has_legacy_inline_fts(self._conn):
                return True
            if self._conn.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_high_water' LIMIT 1").fetchone():
                return True
            if self._fts_cjk_loaded and self._conn.execute(f"SELECT 1 FROM state_meta WHERE key IN ('fts_cjk_rebuild_high_water', '{FTS_CJK_STALE_KEY}') LIMIT 1").fetchone():
                return True
            if self._has_fts_trash(self._conn):
                return True
            return self._fts_external_index_empty_with_messages(self._conn)

    def _demote_legacy_fts_to_trash(self) -> int:
        """Demote the legacy inline FTS vtables and stage their shadow tables
        for chunked teardown. Returns MAX(messages.id) as the rebuild high
        water. O(1) schema surgery — the heavy delete is deferred to the
        chunked teardown, exactly as the validated auto path did.

        Markers are written in the same BEGIN IMMEDIATE as the demote, *before*
        the empty v23 schema is created. Schema creation uses
        ``executescript`` and therefore cannot run inside that transaction
        (it issues an implicit COMMIT — see the CJK recreate path). Creating
        the empty schema only after markers are durable closes the crash
        window where trash + empty v23 tables exist with no backfill claim.
        """

        def _stage(conn):
            self._drop_fts_triggers(conn)
            conn.execute('DROP VIEW IF EXISTS messages_fts_trigram_src')
            had = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name IN ('messages_fts', 'messages_fts_trigram') AND sql LIKE 'CREATE VIRTUAL TABLE%' LIMIT 1").fetchone())
            if had:
                conn.execute('PRAGMA writable_schema=ON')
                conn.execute("DELETE FROM sqlite_master WHERE type = 'table' AND name IN ('messages_fts', 'messages_fts_trigram') AND sql LIKE 'CREATE VIRTUAL TABLE%'")
                conn.execute('PRAGMA writable_schema=RESET')
                shadows = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND (name LIKE 'messages_fts_%' ESCAPE '\\' OR name LIKE 'messages_fts_trigram_%' ESCAPE '\\')").fetchall()]
                for sh in shadows:
                    conn.execute(f'ALTER TABLE {sh} RENAME TO fts_v22_trash_{sh}')
            hw = self._seed_fts_rebuild_markers(conn, force=True)
            conn.execute("DELETE FROM state_meta WHERE key = 'fts_optimize_available'")
            return hw
        hw = int(self._execute_write(_stage))
        with self._lock:
            base_ok = self._ensure_fts_schema(self._conn, 'messages_fts', FTS_SQL)
            trigram_ok = self._ensure_fts_schema(self._conn, 'messages_fts_trigram', FTS_TRIGRAM_SQL)
            self._trigram_available = bool(trigram_ok)
            if not base_ok:
                raise sqlite3.OperationalError('failed to create v23 messages_fts during optimize-storage demote')
            self._conn.commit()
        return hw

    def optimize_fts_storage(self, *, progress_cb: Optional[Callable[[Dict[str, Any]], None]]=None, vacuum: bool=True) -> Dict[str, Any]:
        """Migrate a legacy v22 inline-FTS DB to the v23 external-content
        schema, foreground and to completion. Safe to re-run: if a previous
        attempt was interrupted it resumes from the progress marker.

        ``progress_cb`` receives {"phase", "percent", "indexed", "total"}
        dicts for a CLI progress bar. Returns a summary dict.

        The trigram tokenizer being unavailable is not fatal — the base index
        is still rebuilt (CJK falls back to LIKE), mirroring normal startup.
        """
        if not self._fts_enabled:
            return {'ok': False, 'reason': 'fts5_unavailable'}
        if self.read_only:
            return {'ok': False, 'reason': 'read_only'}
        self._repair_optimize_bookkeeping()
        with self._lock:
            legacy = self._db_has_legacy_inline_fts(self._conn)
        pending = self.get_meta('fts_rebuild_high_water') is not None
        if legacy and (not pending):
            self._demote_legacy_fts_to_trash()
        elif pending and (not legacy):
            with self._lock:
                base_ok = self._ensure_fts_schema(self._conn, 'messages_fts', FTS_SQL)
                trigram_ok = self._ensure_fts_schema(self._conn, 'messages_fts_trigram', FTS_TRIGRAM_SQL)
                self._trigram_available = bool(trigram_ok)
                if not base_ok:
                    raise sqlite3.OperationalError('failed to re-create v23 messages_fts on optimize-storage resume')
                self._conn.commit()
        self._fts_cjk_reset_if_stale()
        if self._fts_cjk_loaded:
            with self._lock:
                self._ensure_fts_cjk_schema(self._conn)
                self._conn.commit()

        def _emit(phase: str) -> None:
            if progress_cb is None:
                return
            st = self.fts_rebuild_status()
            if st is None:
                st = self.fts_cjk_rebuild_status()
            progress_cb({'phase': phase, 'percent': st['percent'] if st else 100, 'indexed': st['indexed'] if st else 0, 'total': st['total'] if st else 0})

        def _pause(chunk_seconds: float) -> None:
            """Inter-chunk throttle (see the chunk-engine note above).

            The chunk methods themselves never sleep, so this loop is the
            single place the duty cycle is enforced: without it, back-to-back
            BEGIN IMMEDIATE chunks starve any live gateway/CLI process
            sharing the DB out of its lock retries (the measured ~85%
            write-lock ownership that froze concurrent sessions).
            """
            time.sleep(max(self._FTS_REBUILD_MIN_PAUSE, chunk_seconds * self._FTS_REBUILD_DUTY_FACTOR))
        _emit('backfill')
        while True:
            _t0 = time.monotonic()
            if not self.fts_rebuild_step():
                break
            _emit('backfill')
            _pause(time.monotonic() - _t0)
        _emit('backfill')
        while True:
            _t0 = time.monotonic()
            if not self.fts_cjk_rebuild_step():
                break
            _emit('backfill')
            _pause(time.monotonic() - _t0)
        _emit('teardown')
        while True:
            _t0 = time.monotonic()
            if not self._fts_teardown_trash_step():
                break
            _emit('teardown')
            _pause(time.monotonic() - _t0)
        with self._lock:
            still_pending = self._conn.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_high_water' LIMIT 1").fetchone() is not None
            still_trash = self._has_fts_trash(self._conn)
            empty_index = self._fts_external_index_empty_with_messages(self._conn)
        if still_pending or still_trash or empty_index:
            reason = 'backfill_incomplete' if still_pending or empty_index else 'teardown_incomplete'
            logger.warning('FTS storage optimization did not settle (%s): pending=%s trash=%s empty_index=%s', reason, still_pending, still_trash, empty_index)
            return {'ok': False, 'reason': reason, 'vacuumed': None}
        vacuum_ok = None
        if vacuum:
            _emit('vacuum')
            try:
                with self._lock:
                    self._conn.execute('VACUUM')
                vacuum_ok = True
            except sqlite3.OperationalError as exc:
                logger.warning('VACUUM after FTS optimize failed: %s', exc)
                vacuum_ok = False
            try:
                with self._lock:
                    self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except Exception as exc:
                logger.debug('WAL checkpoint (TRUNCATE) after optimize VACUUM failed: %s', exc)

        def _settle(conn):
            if conn.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_high_water' LIMIT 1").fetchone() is not None:
                return 'backfill_incomplete'
            if self._has_fts_trash(conn):
                return 'teardown_incomplete'
            if self._fts_external_index_empty_with_messages(conn):
                return 'backfill_incomplete'
            conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_storage_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(FTS_STORAGE_VERSION),))
            conn.execute("DELETE FROM state_meta WHERE key = 'fts_optimize_available'")
            conn.execute('UPDATE schema_version SET version = ? WHERE version < ?', (SCHEMA_VERSION, SCHEMA_VERSION))
            return None
        refusal = self._execute_write(_settle)
        if refusal is not None:
            logger.warning('FTS storage optimization settle refused (%s)', refusal)
            return {'ok': False, 'reason': refusal, 'vacuumed': vacuum_ok}
        _emit('done')
        logger.info('FTS storage optimization complete (layout v%d).', FTS_STORAGE_VERSION)
        return {'ok': True, 'vacuumed': vacuum_ok}

    def get_anchored_view(self, session_id: str, around_message_id: int, window: int=5, bookend: int=3, keep_roles: Optional[Tuple[str, ...]]=('user', 'assistant')) -> Dict[str, Any]:
        """Return an anchored window plus session bookends.

        Built on top of ``get_messages_around``. Three slices:

          - ``window``: messages immediately surrounding the anchor. Filtered
            to ``keep_roles`` (tool-response noise dropped by default), EXCEPT
            the anchor itself is always preserved regardless of role.
          - ``bookend_start``: first ``bookend`` user/assistant messages of the
            session — but only those whose id is strictly before the window's
            first message id. Empty when the window already overlaps the
            session head. Empty-content messages (tool-call-only assistant
            turns) are skipped so they don't crowd out actual prose openings.
          - ``bookend_end``: last ``bookend`` user/assistant messages of the
            session, same non-overlap rule at the tail.

        Bookends let an FTS5 hit anywhere in a long session yield the goal
        (opening) and the resolution (closing) on a single call — without
        loading the whole transcript.

        Returns ``{"window": [], "messages_before": 0, "messages_after": 0,
        "bookend_start": [], "bookend_end": []}`` when the anchor isn't in
        the session.

        ``keep_roles=None`` disables role filtering (raw window + raw
        bookends).
        """
        if bookend < 0:
            bookend = 0
        primitive = self.get_messages_around(session_id, around_message_id, window=window)
        window_rows = primitive['window']
        if not window_rows:
            return {'window': [], 'messages_before': 0, 'messages_after': 0, 'bookend_start': [], 'bookend_end': []}
        if keep_roles is not None:
            keep_set = set(keep_roles)
            filtered_window = [m for m in window_rows if m.get('id') == around_message_id or m.get('role') in keep_set]
        else:
            filtered_window = window_rows
        window_min_id = window_rows[0]['id']
        window_max_id = window_rows[-1]['id']
        bookend_start_rows: List[Any] = []
        bookend_end_rows: List[Any] = []
        if bookend > 0:
            with self._read_ctx() as conn:
                role_clause = ''
                role_params: list = []
                if keep_roles is not None:
                    role_placeholders = ','.join(('?' for _ in keep_roles))
                    role_clause = f' AND role IN ({role_placeholders})'
                    role_params = list(keep_roles)
                bookend_start_rows = conn.execute(f'SELECT * FROM messages WHERE session_id = ? AND id < ?{role_clause} AND length(content) > 0 ORDER BY id ASC LIMIT ?', (session_id, window_min_id, *role_params, bookend)).fetchall()
                bookend_end_rows = conn.execute(f'SELECT * FROM messages WHERE session_id = ? AND id > ?{role_clause} AND length(content) > 0 ORDER BY id DESC LIMIT ?', (session_id, window_max_id, *role_params, bookend)).fetchall()
                bookend_end_rows = list(reversed(bookend_end_rows))

        def _hydrate(row) -> Dict[str, Any]:
            msg = dict(row)
            if 'content' in msg:
                msg['content'] = self._decode_content(msg['content'])
            if msg.get('tool_calls'):
                try:
                    msg['tool_calls'] = json.loads(msg['tool_calls'])
                except (json.JSONDecodeError, TypeError):
                    logger.warning('Failed to deserialize tool_calls in get_anchored_view, falling back to []')
                    msg['tool_calls'] = []
            if msg.get('display_metadata') is not None:
                msg['display_metadata'] = self._decode_display_metadata(msg['display_metadata'])
            return msg
        return {'window': filtered_window, 'messages_before': primitive['messages_before'], 'messages_after': primitive['messages_after'], 'bookend_start': [_hydrate(r) for r in bookend_start_rows], 'bookend_end': [_hydrate(r) for r in bookend_end_rows]}

    def list_recent_user_messages(self, session_id: str, limit: int=20, include_inactive: bool=False) -> List[Dict[str, Any]]:
        """Return the *limit* most-recent user messages, newest first.

        Each entry is a dict with keys ``id``, ``timestamp``, ``preview``.
        ``preview`` is the first 80 characters of the message content
        (with line breaks collapsed to spaces). Used by the /rewind
        slash command picker, CLI/TUI/gateway ``/undo [N]``, and any other
        caller that needs real user-turn targets.

        Bookkeeping timeline rows (``display_kind`` set — e.g. model_switch,
        async_delegation_complete, auto_continue, hidden) are excluded. They
        are durable ``role='user'`` rows for the API transcript, but no client
        counts them as user turns (desktop demotes them to system / drops them;
        the CLI already uses ``not m.get("display_kind")``). Including them here
        made ``/undo`` soft-delete from a marker instead of the last real turn —
        same class of index skew as the prompt.submit ordinal bug.

        By default only active messages are returned.
        """
        active_clause = '' if include_inactive else ' AND active = 1'
        display_clause = " AND (display_kind IS NULL OR display_kind = '')"
        fetch_limit = int(limit) * 2 + 5
        with self._lock:
            cursor = self._conn.execute(f"SELECT id, timestamp, content FROM messages WHERE session_id = ? AND role = 'user'{active_clause}{display_clause} ORDER BY id DESC LIMIT ?", (session_id, fetch_limit))
            rows = cursor.fetchall()
        from agent.context_compressor import ContextCompressor
        result: List[Dict[str, Any]] = []
        for row in rows:
            if len(result) >= int(limit):
                break
            decoded = self._decode_content(row['content'])
            if ContextCompressor._is_context_summary_content(decoded):
                continue
            if isinstance(decoded, list):
                text_parts = [p.get('text', '') for p in decoded if isinstance(p, dict) and p.get('type') == 'text']
                preview = ' '.join((t for t in text_parts if t)).strip()
                if not preview:
                    preview = '[multimodal content]'
            elif isinstance(decoded, str):
                preview = describe_skill_invocation(decoded) or decoded
            else:
                preview = ''
            preview = ' '.join(preview.split())
            if len(preview) > 80:
                preview = preview[:77] + '...'
            result.append({'id': row['id'], 'timestamp': row['timestamp'], 'preview': preview})
        return result

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}``, the column-filter operator ``:`` and bare
        boolean operators (``AND``, ``OR``, ``NOT``) have special meaning.
        Passing raw user input directly to MATCH can cause
        ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        query = query[:MAX_FTS5_QUERY_CHARS]
        _quoted_parts: list = []
        pieces: list[str] = []
        i = 0
        while i < len(query):
            ch = query[i]
            if ch != '"':
                pieces.append(ch)
                i += 1
                continue
            end = query.find('"', i + 1)
            if end == -1:
                pieces.append(' ')
                i += 1
                continue
            _quoted_parts.append(query[i:end + 1])
            pieces.append(f'\x00Q{len(_quoted_parts) - 1}\x00')
            i = end + 1
        sanitized = ''.join(pieces)
        sanitized = re.sub('[+{}():\\"^]', ' ', sanitized)
        sanitized = re.sub('\\*+', '*', sanitized)
        sanitized = re.sub('(^|\\s)\\*', '\\1', sanitized)
        sanitized = re.sub('(?i)^(AND|OR|NOT)\\b\\s*', '', sanitized.strip())
        sanitized = re.sub('(?i)\\s+(AND|OR|NOT)\\s*$', '', sanitized.strip())
        sanitized = re.sub('\\b(\\w+(?:[._-]\\w+)+)\\b', '"\\1"', sanitized)
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f'\x00Q{i}\x00', quoted)
        return sanitized.strip()

    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return 19968 <= cp <= 40959 or 13312 <= cp <= 19903 or 131072 <= cp <= 173791 or (12288 <= cp <= 12351) or (12352 <= cp <= 12447) or (12448 <= cp <= 12543) or (44032 <= cp <= 55215)

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """Check if text contains CJK (Chinese, Japanese, Korean) characters."""
        for ch in text:
            cp = ord(ch)
            if 19968 <= cp <= 40959 or 13312 <= cp <= 19903 or 131072 <= cp <= 173791 or (12288 <= cp <= 12351) or (12352 <= cp <= 12447) or (12448 <= cp <= 12543) or (44032 <= cp <= 55215):
                return True
        return False

    @classmethod
    def _count_cjk(cls, text: str) -> int:
        """Count CJK characters in text."""
        return sum((1 for ch in text if cls._is_cjk_codepoint(ord(ch))))

    @classmethod
    def _has_lone_cjk_run(cls, query: str) -> bool:
        """True when any maximal CJK run in the query is a single char.

        The cjk-bigram index stores bigrams for runs >=2 chars and unigrams
        only for isolated chars, so a 1-char CJK term can't match inside
        longer runs there — those queries keep the LIKE substring route.
        """
        run = 0
        for ch in query:
            if cls._is_cjk_codepoint(ord(ch)):
                run += 1
            else:
                if run == 1:
                    return True
                run = 0
        return run == 1

    @staticmethod
    def _trigram_eligible_tokens(query: str) -> bool:
        """True when every non-operator token is long enough for the trigram
        tokenizer to match (>=3 chars).

        The trigram tokenizer indexes overlapping 3-character sequences, so a
        token shorter than 3 chars produces no trigrams and can never match.
        With FTS5's implicit-AND between tokens, a single short token makes the
        whole MATCH return nothing, so the trigram path is only worth taking
        when every searchable token qualifies.
        """
        tokens = [t for t in query.strip('"').strip().split() if t.upper() not in {'AND', 'OR', 'NOT'}]
        return bool(tokens) and all((len(t) >= 3 for t in tokens))

    def _run_trigram_search(self, raw_query: str, *, table: str='messages_fts_trigram', order_by_sql: str, include_inactive: bool, source_filter: List[str]=None, exclude_sources: List[str]=None, role_filter: List[str]=None, limit: int=20, offset: int=0) -> Optional[List[Dict[str, Any]]]:
        """Run a search against a substring-capable FTS index.

        ``table`` is ``messages_fts_trigram`` (default) or
        ``messages_fts_cjk``. The trigram tokenizer indexes overlapping
        3-byte sequences, so it matches substrings regardless of word
        boundaries — both CJK phrases the unicode61 tokenizer splits into
        single characters and Latin runs the unicode61 tokenizer fuses onto
        adjacent CJK (e.g. ``修改youer服务端``). The cjk-bigram tokenizer
        splits Latin runs off adjacent CJK, giving the same recovery as an
        exact ranked token match. Each non-operator token is quoted to
        neutralise FTS5 special characters while boolean operators
        (AND/OR/NOT) are preserved.

        Returns the matching rows, or ``None`` when the query cannot be
        executed (e.g. the tokenizer is unavailable at runtime) so the
        caller can fall back to another strategy.
        """
        tokens = raw_query.split()
        parts = []
        for tok in tokens:
            if tok.upper() in {'AND', 'OR', 'NOT'}:
                parts.append(tok)
            else:
                parts.append('"' + tok.replace('"', '""') + '"')
        trigram_query = ' '.join(parts)
        tri_where = [f'{table} MATCH ?']
        tri_params: list = [trigram_query]
        if not include_inactive:
            tri_where.append('(m.active = 1 OR m.compacted = 1)')
        if source_filter is not None:
            tri_where.append(f"s.source IN ({','.join(('?' for _ in source_filter))})")
            tri_params.extend(source_filter)
        if exclude_sources is not None:
            tri_where.append(f"s.source NOT IN ({','.join(('?' for _ in exclude_sources))})")
            tri_params.extend(exclude_sources)
        if role_filter:
            tri_where.append(f"m.role IN ({','.join(('?' for _ in role_filter))})")
            tri_params.extend(role_filter)
        tri_sql = f"\n            SELECT\n                m.id,\n                m.session_id,\n                m.role,\n                snippet({table}, -1, '>>>', '<<<', '...', 40) AS snippet,\n                m.content,\n                m.timestamp,\n                m.tool_name,\n                s.source,\n                s.model,\n                s.started_at AS session_started\n            FROM {table}\n            JOIN messages m ON m.id = {table}.rowid\n            JOIN sessions s ON s.id = m.session_id\n            WHERE {' AND '.join(tri_where)}\n            {order_by_sql}\n            LIMIT ? OFFSET ?\n        "
        tri_params.extend([limit, offset])
        with self._read_ctx() as conn:
            try:
                tri_cursor = conn.execute(tri_sql, tri_params)
            except sqlite3.OperationalError:
                return None
            return [dict(row) for row in tri_cursor.fetchall()]

    def search_messages(self, query: str, source_filter: List[str]=None, exclude_sources: List[str]=None, role_filter: List[str]=None, limit: int=20, offset: int=0, sort: str=None, include_inactive: bool=False, fields: Optional[Collection[str]]=None) -> List[Dict[str, Any]]:
        """Instrumented wrapper around :meth:`_search_messages_impl`.

        Logs one line per slow search with the routing path taken, so
        production latency stays attributable per query shape (the 2026-07
        session_search investigation needed trace archaeology to discover
        the LIKE full scans; this makes the next regression a grep).
        Threshold: HERMES_SEARCH_SLOW_MS (default 1000; 0 logs every call).
        """
        started = time.time()
        rows = None
        try:
            rows = self._search_messages_impl(query, source_filter=source_filter, exclude_sources=exclude_sources, role_filter=role_filter, limit=limit, offset=offset, sort=sort, include_inactive=include_inactive, fields=fields)
            return rows
        finally:
            try:
                threshold = float(os.getenv('HERMES_SEARCH_SLOW_MS', '1000'))
            except (TypeError, ValueError):
                threshold = 1000.0
            elapsed_ms = (time.time() - started) * 1000.0
            if elapsed_ms >= threshold:
                logger.info('slow session search: path=%s elapsed=%.0fms rows=%s query=%r', self._describe_search_path(query), elapsed_ms, len(rows) if rows is not None else 'err', query[:200])

    def _describe_search_path(self, query: str) -> str:
        """Best-effort name of the routing path a query takes (log-only)."""
        try:
            sanitized = self._sanitize_fts5_query(query or '')
            if not sanitized:
                return 'empty'
            if not self._contains_cjk(sanitized):
                return 'fts5'
            raw = sanitized.strip('"').strip()
            if self._fts_cjk_available and (not self._has_lone_cjk_run(raw)):
                return 'fts_cjk'
            tokens = [t for t in raw.split() if t.upper() not in {'AND', 'OR', 'NOT'} and self._contains_cjk(t)]
            short = any((self._count_cjk(t) < 3 for t in tokens))
            if self._count_cjk(raw) >= 3 and (not short) and self._trigram_available:
                return 'trigram'
            return 'like_scan'
        except Exception:
            return 'unknown'

    def _search_messages_impl(self, query: str, source_filter: List[str]=None, exclude_sources: List[str]=None, role_filter: List[str]=None, limit: int=20, offset: int=0, sort: str=None, include_inactive: bool=False, fields: Optional[Collection[str]]=None) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        ``fields`` selects a result projection; omitting it preserves the
        complete legacy result. Context is only loaded when that projection
        consumes it.

        ``sort`` controls temporal ordering:
          - ``None`` (default): FTS5 BM25 relevance only. Time-neutral.
          - ``"newest"``: order by message timestamp DESC, then by rank.
          - ``"oldest"``: order by message timestamp ASC, then by rank.

        The short-CJK LIKE fallback already orders by timestamp DESC and
        ignores ``sort``. The trigram CJK path honours ``sort`` like the main
        FTS5 path.

        Rewound (``active=0``, ``compacted=0``) rows are excluded by default —
        the user took those back. Compaction-archived rows (``active=0``,
        ``compacted=1``) ARE included by default: they were summarized away from
        the live context but remain part of the conversation's record, so the
        pre-compaction transcript stays discoverable after in-place compaction
        (#38763). Pass ``include_inactive=True`` to search every row regardless.
        """
        result_fields = self._search_message_fields(fields)
        if not self._fts_enabled:
            return []
        if not query or not query.strip():
            return []
        query = self._sanitize_fts5_query(query)
        if not query:
            return []
        if isinstance(sort, str):
            sort_norm = sort.strip().lower()
            if sort_norm not in ('newest', 'oldest'):
                sort_norm = None
        else:
            sort_norm = None
        if sort_norm == 'newest':
            order_by_sql = 'ORDER BY m.timestamp DESC, rank'
        elif sort_norm == 'oldest':
            order_by_sql = 'ORDER BY m.timestamp ASC, rank'
        else:
            order_by_sql = 'ORDER BY rank'
        where_clauses = ['messages_fts MATCH ?']
        params: list = [query]
        if not include_inactive:
            where_clauses.append('(m.active = 1 OR m.compacted = 1)')
        if source_filter is not None:
            source_placeholders = ','.join(('?' for _ in source_filter))
            where_clauses.append(f's.source IN ({source_placeholders})')
            params.extend(source_filter)
        if exclude_sources is not None:
            exclude_placeholders = ','.join(('?' for _ in exclude_sources))
            where_clauses.append(f's.source NOT IN ({exclude_placeholders})')
            params.extend(exclude_sources)
        if role_filter:
            role_placeholders = ','.join(('?' for _ in role_filter))
            where_clauses.append(f'm.role IN ({role_placeholders})')
            params.extend(role_filter)
        where_sql = ' AND '.join(where_clauses)
        params.extend([limit, offset])
        sql = f"\n            SELECT\n                m.id,\n                m.session_id,\n                m.role,\n                snippet(messages_fts, -1, '>>>', '<<<', '...', 40) AS snippet,\n                m.content,\n                m.timestamp,\n                m.tool_name,\n                s.source,\n                s.model,\n                s.started_at AS session_started\n            FROM messages_fts\n            JOIN messages m ON m.id = messages_fts.rowid\n            JOIN sessions s ON s.id = m.session_id\n            WHERE {where_sql}\n            {order_by_sql}\n            LIMIT ? OFFSET ?\n        "
        matches: List[Dict[str, Any]] = []
        is_cjk = self._contains_cjk(query)
        if is_cjk:
            raw_query = query.strip('"').strip()
            cjk_count = self._count_cjk(raw_query)
            _tokens_for_check = [t for t in raw_query.split() if t.upper() not in {'AND', 'OR', 'NOT'} and self._contains_cjk(t)]
            _any_short_cjk = any((self._count_cjk(t) < 3 for t in _tokens_for_check))
            _trigram_succeeded = False
            _wants_tool_rows = bool(role_filter) and 'tool' in role_filter
            if self._fts_cjk_available and (not _wants_tool_rows) and (not self._has_lone_cjk_run(raw_query)):
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in {'AND', 'OR', 'NOT'}:
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                cjk_query = ' '.join(parts)
                cjk_where = ['messages_fts_cjk MATCH ?']
                cjk_params: list = [cjk_query]
                if not include_inactive:
                    cjk_where.append('(m.active = 1 OR m.compacted = 1)')
                if source_filter is not None:
                    cjk_where.append(f"s.source IN ({','.join(('?' for _ in source_filter))})")
                    cjk_params.extend(source_filter)
                if exclude_sources is not None:
                    cjk_where.append(f"s.source NOT IN ({','.join(('?' for _ in exclude_sources))})")
                    cjk_params.extend(exclude_sources)
                if role_filter:
                    cjk_where.append(f"m.role IN ({','.join(('?' for _ in role_filter))})")
                    cjk_params.extend(role_filter)
                cjk_sql = f"\n                    SELECT\n                        m.id,\n                        m.session_id,\n                        m.role,\n                        snippet(messages_fts_cjk, -1, '>>>', '<<<', '...', 40) AS snippet,\n                        m.content,\n                        m.timestamp,\n                        m.tool_name,\n                        s.source,\n                        s.model,\n                        s.started_at AS session_started\n                    FROM messages_fts_cjk\n                    JOIN messages m ON m.id = messages_fts_cjk.rowid\n                    JOIN sessions s ON s.id = m.session_id\n                    WHERE {' AND '.join(cjk_where)}\n                    {order_by_sql}\n                    LIMIT ? OFFSET ?\n                "
                cjk_params.extend([limit, offset])
                try:
                    with self._read_ctx() as conn:
                        cjk_cursor = conn.execute(cjk_sql, cjk_params)
                        matches = [dict(row) for row in cjk_cursor.fetchall()]
                        _trigram_succeeded = True
                except sqlite3.OperationalError:
                    logger.debug('messages_fts_cjk query failed; falling back to trigram/LIKE', exc_info=True)
                except sqlite3.DatabaseError as exc:
                    if self._try_runtime_fts_rebuild(exc):
                        try:
                            with self._read_ctx() as conn:
                                cjk_cursor = conn.execute(cjk_sql, cjk_params)
                                matches = [dict(row) for row in cjk_cursor.fetchall()]
                                _trigram_succeeded = True
                        except sqlite3.DatabaseError:
                            logger.warning('CJK-bigram FTS search still failing after in-place rebuild; falling back to trigram/LIKE.')
                    else:
                        logger.warning('CJK-bigram FTS search hit a corruption error (%s) and no in-place rebuild was possible; falling back to trigram/LIKE.', exc)
            if not _trigram_succeeded and cjk_count >= 3 and (not _any_short_cjk) and self._trigram_available and (not _wants_tool_rows):
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in {'AND', 'OR', 'NOT'}:
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                trigram_query = ' '.join(parts)
                tri_where = ['messages_fts_trigram MATCH ?']
                tri_params: list = [trigram_query]
                if not include_inactive:
                    tri_where.append('(m.active = 1 OR m.compacted = 1)')
                if source_filter is not None:
                    tri_where.append(f"s.source IN ({','.join(('?' for _ in source_filter))})")
                    tri_params.extend(source_filter)
                if exclude_sources is not None:
                    tri_where.append(f"s.source NOT IN ({','.join(('?' for _ in exclude_sources))})")
                    tri_params.extend(exclude_sources)
                if role_filter:
                    tri_where.append(f"m.role IN ({','.join(('?' for _ in role_filter))})")
                    tri_params.extend(role_filter)
                tri_sql = f"\n                    SELECT\n                        m.id,\n                        m.session_id,\n                        m.role,\n                        snippet(messages_fts_trigram, -1, '>>>', '<<<', '...', 40) AS snippet,\n                        m.content,\n                        m.timestamp,\n                        m.tool_name,\n                        s.source,\n                        s.model,\n                        s.started_at AS session_started\n                    FROM messages_fts_trigram\n                    JOIN messages m ON m.id = messages_fts_trigram.rowid\n                    JOIN sessions s ON s.id = m.session_id\n                    WHERE {' AND '.join(tri_where)}\n                    {order_by_sql}\n                    LIMIT ? OFFSET ?\n                "
                tri_params.extend([limit, offset])
                try:
                    with self._read_ctx() as conn:
                        tri_cursor = conn.execute(tri_sql, tri_params)
                        matches = [dict(row) for row in tri_cursor.fetchall()]
                        _trigram_succeeded = True
                except sqlite3.OperationalError:
                    pass
                except sqlite3.DatabaseError as exc:
                    if self._try_runtime_fts_rebuild(exc):
                        try:
                            with self._read_ctx() as conn:
                                tri_cursor = conn.execute(tri_sql, tri_params)
                                matches = [dict(row) for row in tri_cursor.fetchall()]
                                _trigram_succeeded = True
                        except sqlite3.DatabaseError:
                            logger.warning('Trigram FTS search still failing after in-place rebuild; falling back to LIKE.')
                    else:
                        logger.warning('Trigram FTS search hit a corruption error (%s) and no in-place rebuild was possible; falling back to LIKE.', exc)
            if not _trigram_succeeded:
                non_op_tokens = [t for t in raw_query.split() if t.upper() not in {'AND', 'OR', 'NOT'}] or [raw_query]
                token_clauses = []
                like_params: list = []
                for tok in non_op_tokens:
                    esc = _escape_like(tok)
                    token_clauses.append("(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')")
                    like_params += [f'%{esc}%', f'%{esc}%', f'%{esc}%']
                like_where = [f"({' OR '.join(token_clauses)})"]
                if not include_inactive:
                    like_where.append('(m.active = 1 OR m.compacted = 1)')
                if source_filter is not None:
                    like_where.append(f"s.source IN ({','.join(('?' for _ in source_filter))})")
                    like_params.extend(source_filter)
                if exclude_sources is not None:
                    like_where.append(f"s.source NOT IN ({','.join(('?' for _ in exclude_sources))})")
                    like_params.extend(exclude_sources)
                if role_filter:
                    like_where.append(f"m.role IN ({','.join(('?' for _ in role_filter))})")
                    like_params.extend(role_filter)
                like_sql = f"\n                    SELECT m.id, m.session_id, m.role,\n                           substr(m.content,\n                                  max(1, instr(m.content, ?) - 40),\n                                  120) AS snippet,\n                           m.content, m.timestamp, m.tool_name,\n                           s.source, s.model, s.started_at AS session_started\n                    FROM messages m\n                    JOIN sessions s ON s.id = m.session_id\n                    WHERE {' AND '.join(like_where)}\n                    ORDER BY m.timestamp DESC\n                    LIMIT ? OFFSET ?\n                "
                like_params.extend([limit, offset])
                like_params = [non_op_tokens[0]] + like_params
                with self._read_ctx() as conn:
                    like_cursor = conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
        else:
            try:
                with self._read_ctx() as conn:
                    cursor = conn.execute(sql, params)
                    matches = [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []
            except sqlite3.DatabaseError as exc:
                if not self._try_runtime_fts_rebuild(exc):
                    raise
                with self._read_ctx() as conn:
                    cursor = conn.execute(sql, params)
                    matches = [dict(row) for row in cursor.fetchall()]
        rebuild_status = self.fts_rebuild_status()
        if rebuild_status is not None and len(matches) < limit:
            try:
                gap_matches = self._search_unindexed_gap(query, limit - len(matches), include_inactive=include_inactive, source_filter=source_filter, exclude_sources=exclude_sources, role_filter=role_filter)
                seen_ids = {m['id'] for m in matches}
                matches.extend((m for m in gap_matches if m['id'] not in seen_ids))
            except sqlite3.OperationalError as exc:
                logger.debug('Unindexed-gap supplement skipped: %s', exc)
        if not matches and (not is_cjk) and (not (bool(role_filter) and 'tool' in role_filter)):
            _fb_query = query.strip('"').strip()
            if self._fts_cjk_available:
                cjk_fb = self._run_trigram_search(_fb_query, table='messages_fts_cjk', order_by_sql=order_by_sql, include_inactive=include_inactive, source_filter=source_filter, exclude_sources=exclude_sources, role_filter=role_filter, limit=limit, offset=offset)
                if cjk_fb:
                    matches = cjk_fb
            if not matches and self._trigram_available and self._trigram_eligible_tokens(query):
                tri_matches = self._run_trigram_search(_fb_query, order_by_sql=order_by_sql, include_inactive=include_inactive, source_filter=source_filter, exclude_sources=exclude_sources, role_filter=role_filter, limit=limit, offset=offset)
                if tri_matches:
                    matches = tri_matches
        context_matches = matches if result_fields is None or 'context' in result_fields else ()
        for match in context_matches:
            try:
                with self._read_ctx() as conn:
                    ctx_cursor = conn.execute('WITH target AS (\n                               SELECT session_id, timestamp, id\n                               FROM messages\n                               WHERE id = ?\n                           )\n                           SELECT role, content\n                           FROM (\n                               SELECT m.id, m.timestamp, m.role, m.content\n                               FROM messages m\n                               JOIN target t ON t.session_id = m.session_id\n                               WHERE (m.timestamp < t.timestamp)\n                                  OR (m.timestamp = t.timestamp AND m.id < t.id)\n                               ORDER BY m.timestamp DESC, m.id DESC\n                               LIMIT 1\n                           )\n                           UNION ALL\n                           SELECT role, content\n                           FROM messages\n                           WHERE id = ?\n                           UNION ALL\n                           SELECT role, content\n                           FROM (\n                               SELECT m.id, m.timestamp, m.role, m.content\n                               FROM messages m\n                               JOIN target t ON t.session_id = m.session_id\n                               WHERE (m.timestamp > t.timestamp)\n                                  OR (m.timestamp = t.timestamp AND m.id > t.id)\n                               ORDER BY m.timestamp ASC, m.id ASC\n                               LIMIT 1\n                           )', (match['id'], match['id']))
                    context_msgs = []
                    for r in ctx_cursor.fetchall():
                        raw = r['content']
                        decoded = self._decode_content(raw)
                        if isinstance(decoded, list):
                            text_parts = [p.get('text', '') for p in decoded if isinstance(p, dict) and p.get('type') == 'text']
                            text = ' '.join((t for t in text_parts if t)).strip()
                            preview = text or '[multimodal content]'
                        elif isinstance(decoded, str):
                            preview = decoded
                        else:
                            preview = ''
                        context_msgs.append({'role': r['role'], 'content': preview[:200]})
                match['context'] = context_msgs
            except Exception:
                match['context'] = []
        for match in matches:
            match.pop('content', None)
        if result_fields is not None:
            matches = [{field: match[field] for field in result_fields if field in match} for match in matches]
        return matches

    def _search_unindexed_gap(self, fts_query: str, limit: int, *, include_inactive: bool=False, source_filter: Optional[List[str]]=None, exclude_sources: Optional[List[str]]=None, role_filter: Optional[List[str]]=None) -> List[Dict[str, Any]]:
        """LIKE-scan the rows the deferred rebuild hasn't indexed yet.

        Only touches ids in (fts_rebuild_progress, fts_rebuild_high_water] —
        a range that shrinks to nothing as the backfill advances. The FTS
        query is degraded to per-token substring terms (AND-joined; quoted
        phrases kept whole), which is deliberately recall-over-precision:
        temporary results beat silently missing ones mid-rebuild.
        """
        status = self.fts_rebuild_status()
        if status is None or limit <= 0:
            return []
        progress, high_water = (status['indexed'], status['total'])
        terms: List[str] = []
        for raw_tok in re.findall('"[^"]+"|\\S+', fts_query):
            tok = raw_tok.strip('"').strip('*').strip()
            if not tok or tok.upper() in {'AND', 'OR', 'NOT', 'NEAR'}:
                continue
            terms.append(tok)
        if not terms:
            return []
        where = ['m.id > ? AND m.id <= ?']
        params: list = [progress, high_water]
        for term in terms:
            esc = _escape_like(term)
            where.append("(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')")
            params += [f'%{esc}%'] * 3
        if not include_inactive:
            where.append('(m.active = 1 OR m.compacted = 1)')
        if source_filter is not None:
            where.append(f"s.source IN ({','.join(('?' for _ in source_filter))})")
            params.extend(source_filter)
        if exclude_sources is not None:
            where.append(f"s.source NOT IN ({','.join(('?' for _ in exclude_sources))})")
            params.extend(exclude_sources)
        if role_filter:
            where.append(f"m.role IN ({','.join(('?' for _ in role_filter))})")
            params.extend(role_filter)
        sql = f"\n            SELECT m.id, m.session_id, m.role,\n                   substr(m.content,\n                          max(1, instr(m.content, ?) - 40),\n                          120) AS snippet,\n                   m.content, m.timestamp, m.tool_name,\n                   s.source, s.model, s.started_at AS session_started\n            FROM messages m\n            JOIN sessions s ON s.id = m.session_id\n            WHERE {' AND '.join(where)}\n            ORDER BY m.timestamp DESC\n            LIMIT ?\n        "
        params = [terms[0]] + params + [limit]
        with self._read_ctx() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_sessions_by_id(self, query: str, limit: int=20, include_archived: bool=True, source: str=None, sources: List[str]=None, exclude_sources: List[str]=None) -> List[Dict[str, Any]]:
        """Search surfaced sessions by exact/prefix/substring session id.

        Desktop search uses this alongside FTS message search so users can paste
        a session id from logs, CLI output, or another Duck Agent surface and jump
        straight to that conversation.  Matching also checks ``_lineage_root_id``
        for projected compression-chain tips, so an old root id still resolves to
        the live continuation row.
        """
        needle = (query or '').strip().lower()
        if not needle or limit <= 0:
            return []
        candidates = self.list_sessions_rich(source=source, sources=sources, exclude_sources=exclude_sources, limit=max(limit * 4, limit), offset=0, include_archived=include_archived, order_by_last_active=True, id_query=needle)

        def score(row: Dict[str, Any]) -> int:
            ids = [str(row.get('id') or ''), str(row.get('_lineage_root_id') or '')]
            normalized = [value.lower() for value in ids if value]
            if any((value == needle for value in normalized)):
                return 0
            if any((value.startswith(needle) for value in normalized)):
                return 1
            return 2
        ranked = sorted(enumerate(candidates), key=lambda item: (score(item[1]), item[0]))
        return [row for _, row in ranked[:limit]]

    def _fts_table_exists(self, name: str) -> bool:
        """True if an FTS5 virtual table is queryable in this DB."""
        try:
            self._conn.execute(f'SELECT 1 FROM {name} LIMIT 0')
            return True
        except sqlite3.DatabaseError:
            return False

    def optimize_fts(self) -> int:
        """Merge fragmented FTS5 b-tree segments into one per index.

        FTS5 indexes grow as a series of incremental segments — one per
        ``INSERT`` batch driven by the message triggers. Over tens of
        thousands of messages these segments accumulate, which both bloats
        the ``*_data`` shadow tables and slows ``MATCH`` queries that must
        scan every segment. The special ``'optimize'`` command rewrites each
        index as a single merged segment.

        This is purely a maintenance operation — it changes neither search
        results nor ``snippet()`` output, only on-disk layout and query
        speed. It is complementary to VACUUM: ``optimize`` compacts the FTS
        index internally, then VACUUM returns the freed pages to the OS.

        Skips any FTS table that does not exist (e.g. the trigram index when
        disabled via ``HERMES_DISABLE_FTS_TRIGRAM`` or not yet created), so
        it is safe to call unconditionally.

        Returns the number of FTS indexes that were optimized.
        """
        optimized = 0
        with self._lock:
            for tbl in self._FTS_TABLES:
                if not self._fts_table_exists(tbl):
                    continue
                try:
                    self._conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES('optimize')")
                    optimized += 1
                except sqlite3.OperationalError as exc:
                    logger.warning('FTS optimize failed for %s: %s', tbl, exc)
        return optimized

    def rebuild_fts(self) -> int:
        """Rebuild FTS5 indexes from the canonical ``messages`` table.

        Uses the FTS5 ``'rebuild'`` command, which rewrites the internal
        b-tree segments from the content rows. This is the documented
        recovery for a corrupt FTS index that rejects message writes while
        reads still succeed (issue #50502). Unlike ``optimize_fts`` (which
        merges existing segments), ``rebuild`` discards and recreates the
        index data entirely.

        Safe to call when FTS tables don't exist (skips them).
        Returns the number of FTS indexes that were rebuilt.
        """
        rebuilt = 0
        with self._lock:
            for tbl in self._FTS_TABLES:
                if not self._fts_table_exists(tbl):
                    continue
                try:
                    self._conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES('rebuild')")
                    self._conn.commit()
                    rebuilt += 1
                except sqlite3.OperationalError as exc:
                    self._conn.rollback()
                    logger.warning('FTS rebuild failed for %s: %s', tbl, exc)
        return rebuilt

    def _merge_fts_incrementally(self, *, max_pages: int, max_commands: Optional[int]=None) -> int:
        """Run bounded FTS5 ``'merge'`` commands against each present index.

        A positive merge rank tells SQLite to stop after approximately that
        many output pages, so each command holds the write lock for
        milliseconds regardless of index size — unlike ``'optimize'``, which
        rewrites the whole index in one transaction (measured 9-18 s per
        index on a 10 GB production DB, long enough to exhaust a competing
        writer's entire lock-retry patience).

        Protocol (SQLite FTS5 §6.8-6.9):

        - ``usermerge`` is lowered to its minimum of 2 (persisted in the
          ``%_config`` shadow table, applied once per instance) so a
          positive merge acts on ANY level holding >= 2 segments. With the
          default of 4, levels below that threshold are never merged by a
          positive-rank command and a fragmented index cannot converge.
        - Up to *max_commands* merge commands run per index, stopping early
          on the documented no-progress signal: the delta in
          ``total_changes`` is < 2 (the command's own INSERT accounts
          for 1 change; >= 2 means real merge work happened).

        Each command is its own implicit transaction (the connection runs
        with ``isolation_level=None``), so the SQLite write lock is released
        between commands and competing processes can interleave writes
        mid-pass. Missing tables are valid schema variants (FTS variants are
        optional, and ``optimize_fts_storage`` legitimately drops + backfills
        these tables while writers keep running) and are skipped, mirroring
        ``optimize_fts``. Other SQLite errors propagate to the caller.

        Returns the number of merge commands executed.
        """
        if isinstance(max_pages, bool) or not isinstance(max_pages, int):
            raise TypeError('max_pages must be an integer')
        if max_pages <= 0:
            raise ValueError('max_pages must be greater than zero')
        if max_commands is None:
            max_commands = self._FTS_MERGE_COMMANDS_PER_PASS
        if isinstance(max_commands, bool) or not isinstance(max_commands, int):
            raise TypeError('max_commands must be an integer')
        if max_commands <= 0:
            raise ValueError('max_commands must be greater than zero')
        executed = 0
        with self._lock:
            for tbl in self._FTS_TABLES:
                if not self._fts_table_exists(tbl):
                    continue
                if not getattr(self, '_fts_usermerge_floor_applied', False):
                    self._conn.execute(f"INSERT INTO {tbl}({tbl}, rank) VALUES('usermerge', 2)")
                for _ in range(max_commands):
                    before = self._conn.total_changes
                    self._conn.execute(f"INSERT INTO {tbl}({tbl}, rank) VALUES('merge', ?)", (max_pages,))
                    executed += 1
                    if self._conn.total_changes - before < 2:
                        break
            self._fts_usermerge_floor_applied = True
        return executed