"""Schema creation, column reconciliation, and FTS DDL management for SessionDB.

Mixin contract: this is a plain mixin class consumed by
``hermes_state.SessionDB``. It defines no ``__init__`` and no state of its
own; methods access the host's attributes (``self._conn``, ``self.db_path``,
``self._execute_write`` and other SessionDB methods) established by
``SessionDB.__init__``. It must never import hermes_state (cycle) — shared
module-level constants live in hermes_state_common.
"""
import logging
import json
import sqlite3
from typing import Dict, Optional
from hermes_constants import get_hermes_home
from hermes_state_common import DEFERRED_INDEX_SQL, FTS_CJK_STALE_KEY, FTS_SQL, FTS_STORAGE_VERSION, FTS_TRIGRAM_SQL, LEGACY_FTS_SQL, LEGACY_FTS_TRIGRAM_SQL, SCHEMA_SQL, SCHEMA_VERSION, _FTS_TRIGGERS, _ephemeral_child_sql
logger = logging.getLogger('hermes_state')
_READ_PROBE_STATEMENTS: Optional[tuple] = None

def schema_read_probe_statements() -> tuple:
    """SELECT statements that fail iff a live store is behind SCHEMA_SQL.

    Read-only opens skip ``_reconcile_columns()`` by design (no DDL against
    another profile's live DB), so a store created before a schema addition
    keeps 500ing on read paths until something opens it writable. Callers
    that heal on staleness (see ``_open_session_db_at_path`` in
    ``hermes_cli/web_server.py``) run these probes right after a read-only
    open: any missing table raises "no such table" and any missing column
    raises "no such column", both at prepare time.

    Derived from SCHEMA_SQL — the same source of truth the writable
    reconciler diffs against — so a column added there is covered here
    automatically. A hand-maintained probe list went stale within days of
    shipping (it never learned ``sessions.last_activity_at``, so the sidebar
    served an empty session list after `duck-agent update` until the user's
    first message forced a writable open).

    Each statement is ``LIMIT 0``: column resolution happens at prepare
    time, so the probe reads zero rows. Column references are qualified
    with the table name — an unqualified double-quoted identifier that
    fails to resolve silently degrades to a string literal (SQLite's
    double-quoted-string misfeature), which would make the probe pass on
    exactly the stale store it exists to catch.
    """
    global _READ_PROBE_STATEMENTS
    if _READ_PROBE_STATEMENTS is None:
        tables = SessionSchemaMixin._parse_schema_columns(SCHEMA_SQL)
        _READ_PROBE_STATEMENTS = tuple(('SELECT {} FROM "{}" LIMIT 0'.format(', '.join(('"{}"."{}"'.format(table.replace('"', '""'), col.replace('"', '""')) for col in cols)), table.replace('"', '""')) for table, cols in sorted(tables.items())))
    return _READ_PROBE_STATEMENTS

class SessionSchemaMixin:
    """See module docstring — mixin for SessionDB (Schema cluster)."""

    def _dedupe_legacy_system_prompts(self, cursor: sqlite3.Cursor) -> None:
        """Move inline prompt snapshots into the shared content-addressed table."""
        try:
            rows = cursor.execute('SELECT id, system_prompt FROM sessions WHERE system_prompt IS NOT NULL').fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            session_id = row['id'] if isinstance(row, sqlite3.Row) else row[0]
            prompt = row['system_prompt'] if isinstance(row, sqlite3.Row) else row[1]
            prompt_hash = self._store_system_prompt(cursor, prompt)
            cursor.execute('UPDATE sessions SET system_prompt_hash = ?, system_prompt = NULL WHERE id = ?', (prompt_hash, session_id))

    def _sqlite_supports_fts5(self, cursor: sqlite3.Cursor) -> bool:
        try:
            cursor.execute('CREATE VIRTUAL TABLE temp._hermes_fts5_probe USING fts5(x)')
            cursor.execute('DROP TABLE temp._hermes_fts5_probe')
            return True
        except sqlite3.OperationalError as exc:
            if not self._is_fts5_unavailable_error(exc):
                raise
            self._warn_fts5_unavailable(exc)
            return False

    @staticmethod
    def _fts_trigger_count(cursor: sqlite3.Cursor) -> int:
        placeholders = ','.join(('?' for _ in _FTS_TRIGGERS))
        row = cursor.execute(f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN ({placeholders})", _FTS_TRIGGERS).fetchone()
        return int(row[0] if not isinstance(row, sqlite3.Row) else row[0])

    @staticmethod
    def _fts_update_trigger_needs_narrowing(sql: Optional[str]) -> bool:
        """True when trigger SQL is missing AFTER UPDATE OF (still broad)."""
        if not sql:
            return False
        compact = ' '.join(sql.split()).upper()
        if 'AFTER UPDATE OF ' in compact:
            return False
        return 'AFTER UPDATE ON ' in compact

    def _migrate_broad_fts_update_triggers(self, cursor: sqlite3.Cursor) -> int:
        """Replace broad AFTER UPDATE FTS triggers with AFTER UPDATE OF variants.

        ``CREATE TRIGGER IF NOT EXISTS`` will not replace an existing broad
        trigger, so installs that already created ``AFTER UPDATE ON messages``
        would keep firing on every messages row touch (status/compaction
        writes included). Inspect ``sqlite_master``, drop any still-broad
        UPDATE triggers, and re-apply the current DDL constants.

        No FTS rebuild: content correctness was already gated by WHEN clauses
        on modern installs; OF only skips unnecessary trigger evaluation.

        Returns the number of triggers dropped (0 when already converged).
        """
        legacy_layout = self._db_has_legacy_inline_fts(cursor)
        update_names = ('messages_fts_update', 'messages_fts_trigram_update')
        if not legacy_layout and hasattr(self, '_ensure_fts_cjk_schema'):
            update_names += ('messages_fts_cjk_update',)
        placeholders = ', '.join(('?' for _ in update_names))
        rows = cursor.execute(f"SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN ({placeholders})", update_names).fetchall()
        to_drop = []
        for row in rows:
            name = row[0] if not isinstance(row, sqlite3.Row) else row['name']
            sql = row[1] if not isinstance(row, sqlite3.Row) else row['sql']
            if self._fts_update_trigger_needs_narrowing(sql):
                to_drop.append(name)
        if not to_drop:
            return 0
        for name in to_drop:
            cursor.execute(f'DROP TRIGGER IF EXISTS {name}')
        if legacy_layout:
            self._ensure_fts_schema(cursor, 'messages_fts', LEGACY_FTS_SQL)
            self._ensure_fts_schema(cursor, 'messages_fts_trigram', LEGACY_FTS_TRIGRAM_SQL)
        else:
            self._ensure_fts_schema(cursor, 'messages_fts', FTS_SQL)
            self._ensure_fts_schema(cursor, 'messages_fts_trigram', FTS_TRIGRAM_SQL)
            if 'messages_fts_cjk_update' in to_drop:
                try:
                    self._ensure_fts_cjk_schema(cursor)
                except Exception:
                    self._quarantine_cjk_after_update_of_migration(cursor)
                    logger.exception('CJK FTS re-ensure after UPDATE OF migration failed')
                    raise
                if not self._cjk_update_trigger_is_narrowed(cursor):
                    self._quarantine_cjk_after_update_of_migration(cursor)
                    logger.warning('CJK FTS UPDATE trigger missing or still broad after UPDATE OF migration; marked stale and unavailable')
        logger.info('Migrated %d broad FTS UPDATE trigger(s) to AFTER UPDATE OF (no rebuild required)', len(to_drop))
        return len(to_drop)

    def _cjk_update_trigger_is_narrowed(self, cursor: sqlite3.Cursor) -> bool:
        """True when messages_fts_cjk_update exists with AFTER UPDATE OF."""
        row = cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", ('messages_fts_cjk_update',)).fetchone()
        if not row:
            return False
        sql = row[0] if not isinstance(row, sqlite3.Row) else row['sql']
        return not self._fts_update_trigger_needs_narrowing(sql)

    def _quarantine_cjk_after_update_of_migration(self, cursor: sqlite3.Cursor) -> None:
        """Fail-closed after dropping CJK UPDATE during OF migration.

        Clears availability, persists ``fts_cjk_stale``, and drops any
        residual broad/partial CJK UPDATE trigger so a later open cannot
        ``CREATE TRIGGER IF NOT EXISTS`` a gap without rebuild.
        """
        self._fts_cjk_available = False
        try:
            self.set_meta(FTS_CJK_STALE_KEY, '1', cursor=cursor)
        except Exception:
            logger.debug('Could not persist CJK FTS stale breadcrumb', exc_info=True)
        try:
            cursor.execute('DROP TRIGGER IF EXISTS messages_fts_cjk_update')
        except Exception:
            logger.debug('Could not drop residual CJK UPDATE trigger after quarantine', exc_info=True)

    @staticmethod
    def _rebuild_fts_indexes(cursor: sqlite3.Cursor, *, include_trigram: bool=True) -> None:
        cursor.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        if include_trigram:
            cursor.execute("INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')")
        cursor.execute("DELETE FROM state_meta WHERE key IN ('fts_rebuild_high_water', 'fts_rebuild_progress')")

    @staticmethod
    def _rebuild_legacy_fts_indexes(cursor: sqlite3.Cursor, *, include_trigram: bool=True) -> None:
        """Rebuild the LEGACY inline FTS indexes (pre-v23) from messages.

        Used only to repair a legacy DB whose triggers degraded under an
        earlier no-FTS5 runtime. Inline tables have no external-content
        'rebuild' source, so we DELETE + reinsert the concatenated content
        the legacy triggers produced. Never touches the v23 shape.
        """
        cursor.execute('DELETE FROM messages_fts')
        cursor.execute("INSERT INTO messages_fts(rowid, content) SELECT id, COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') FROM messages")
        if not include_trigram:
            return
        cursor.execute('DELETE FROM messages_fts_trigram')
        cursor.execute("INSERT INTO messages_fts_trigram(rowid, content) SELECT id, COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') FROM messages")

    def _fts_table_probe(self, cursor: sqlite3.Cursor, table_name: str) -> Optional[bool]:
        try:
            cursor.execute(f'SELECT * FROM {table_name} LIMIT 0')
            return True
        except sqlite3.OperationalError as exc:
            if self._is_fts5_unavailable_error(exc):
                if self._is_trigram_unavailable_error(exc):
                    self._warn_trigram_unavailable(exc)
                else:
                    self._warn_fts5_unavailable(exc)
                return None
            if 'no such table' in str(exc).lower():
                return False
            raise

    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Extract expected columns per table from SCHEMA_SQL.

        Uses an in-memory SQLite database to parse the SQL — SQLite itself
        handles all syntax (DEFAULT expressions with commas, inline
        REFERENCES, CHECK constraints, etc.) so there are zero regex
        edge cases.  The in-memory DB is opened, the schema DDL is
        executed, and PRAGMA table_info extracts the column metadata.

        Adding a column to SCHEMA_SQL is all that's needed; the
        reconciliation loop picks it up automatically.
        """
        ref = sqlite3.connect(':memory:')
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for tbl, in ref.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(f'PRAGMA table_info("{tbl}")').fetchall():
                    col_name = row[1]
                    col_type = row[2] or ''
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    parts = [col_type] if col_type else []
                    if notnull and (not pk):
                        parts.append('NOT NULL')
                    if default is not None:
                        parts.append(f'DEFAULT {default}')
                    cols[col_name] = ' '.join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> None:
        """Ensure live tables have every column declared in SCHEMA_SQL.

        Follows the Beets/sqlite-utils pattern: the CREATE TABLE definition
        in SCHEMA_SQL is the single source of truth for the desired schema.
        On every startup this method diffs the live columns (via PRAGMA
        table_info) against the declared columns, and ADDs any that are
        missing.

        This makes column additions a declarative operation — just add
        the column to SCHEMA_SQL and it appears on the next startup.
        Version-gated migration blocks are no longer needed for ADD COLUMN.
        """
        expected = self._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            try:
                rows = cursor.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            except sqlite3.OperationalError:
                continue
            live_cols = set()
            for row in rows:
                name = row[1] if isinstance(row, (tuple, list)) else row['name']
                live_cols.add(name)
            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {col_type}')
                    except sqlite3.OperationalError as exc:
                        logger.debug('reconcile %s.%s: %s', table_name, col_name, exc)

    def _heal_gateway_routing_pk(self, cursor: sqlite3.Cursor) -> None:
        """Rebuild ``gateway_routing`` when its PRIMARY KEY predates scoping.

        Early builds of the routing-index migration (#59203) created the
        table with ``session_key TEXT PRIMARY KEY`` and no ``scope`` column.
        ``_reconcile_columns()`` ADDs the missing ``scope`` column on those
        databases, but SQLite cannot ALTER a primary key, so the shipped
        composite ``PRIMARY KEY (scope, session_key)`` never lands.  On such
        tables every write path is broken:

        * ``save_gateway_routing_entry`` fails with "ON CONFLICT clause does
          not match any PRIMARY KEY or UNIQUE constraint" (its upsert targets
          the composite key), and
        * ``replace_gateway_routing_entries`` fails with "UNIQUE constraint
          failed: gateway_routing.session_key" whenever the same session_key
          exists under a different scope — the exact isolation the composite
          key exists to provide.

        Each failed save logs a warning and falls back to sessions.json,
        so a legacy-shaped table produces endless per-save warning spam.
        Rebuild it once, preserving rows.  On a session_key collision across
        scopes (possible while the PK was wrong) the newest row wins.
        """
        try:
            rows = cursor.execute('PRAGMA table_info("gateway_routing")').fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return

        def _col(row, idx, name):
            return row[idx] if isinstance(row, (tuple, list)) else row[name]
        pk_cols = [_col(r, 1, 'name') for r in sorted((r for r in rows if _col(r, 5, 'pk')), key=lambda r: _col(r, 5, 'pk'))]
        if pk_cols == ['scope', 'session_key']:
            return
        logger.info('gateway_routing has legacy primary key %r; rebuilding with composite (scope, session_key) key', pk_cols)
        cursor.execute('ALTER TABLE gateway_routing RENAME TO gateway_routing_legacy_pk')
        cursor.execute("CREATE TABLE gateway_routing (\n    scope TEXT NOT NULL DEFAULT '',\n    session_key TEXT NOT NULL,\n    entry_json TEXT NOT NULL,\n    updated_at REAL NOT NULL,\n    PRIMARY KEY (scope, session_key)\n)")
        cursor.execute("INSERT OR REPLACE INTO gateway_routing (scope, session_key, entry_json, updated_at) SELECT COALESCE(scope, ''), session_key, entry_json, updated_at FROM gateway_routing_legacy_pk ORDER BY updated_at ASC")
        cursor.execute('DROP TABLE gateway_routing_legacy_pk')

    def _heal_session_model_usage_pk(self, cursor: sqlite3.Cursor) -> None:
        """Rebuild ``session_model_usage`` when its PRIMARY KEY lacks ``task``.

        Installs whose ``state.db`` reached ``schema_version >= 22`` before
        the ``task`` dimension was added carry a 5-column PRIMARY KEY
        ``(session_id, model, billing_provider, billing_base_url,
        billing_mode)``.  ``_reconcile_columns()`` ADDs the ``task`` column
        as a bare nullable, but SQLite cannot ALTER a primary key, so the
        shipped composite 6-column key never lands.  The version-gated v22
        rebuild is unreachable on those installs (``current_version < 22``
        is already false), so every upsert in ``_record_model_usage()``
        fails with "ON CONFLICT clause does not match any PRIMARY KEY or
        UNIQUE constraint" — aborting the enclosing write transaction and
        silently zeroing all token *and* cost accounting (#73823).

        Idempotent; runs unconditionally on every open, same pattern as
        :meth:`_heal_gateway_routing_pk` above.  On healthy databases the
        PRAGMA check short-circuits and this is a no-op.
        """
        try:
            rows = cursor.execute('PRAGMA table_info("session_model_usage")').fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return

        def _col(row, idx, name):
            return row[idx] if isinstance(row, (tuple, list)) else row[name]
        pk_cols = {_col(r, 1, 'name') for r in rows if _col(r, 5, 'pk')}
        if 'task' in pk_cols:
            return
        logger.info('session_model_usage has legacy primary key %r (missing task); rebuilding with composite 6-column key', sorted(pk_cols))
        cursor.execute('PRAGMA foreign_keys=OFF')
        try:
            cursor.execute('ALTER TABLE session_model_usage RENAME TO session_model_usage_legacy_pk')
            cursor.execute("CREATE TABLE session_model_usage (\n    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n    model TEXT NOT NULL,\n    billing_provider TEXT NOT NULL DEFAULT '',\n    billing_base_url TEXT NOT NULL DEFAULT '',\n    billing_mode TEXT NOT NULL DEFAULT '',\n    task TEXT NOT NULL DEFAULT '',\n    api_call_count INTEGER NOT NULL DEFAULT 0,\n    input_tokens INTEGER NOT NULL DEFAULT 0,\n    output_tokens INTEGER NOT NULL DEFAULT 0,\n    cache_read_tokens INTEGER NOT NULL DEFAULT 0,\n    cache_write_tokens INTEGER NOT NULL DEFAULT 0,\n    reasoning_tokens INTEGER NOT NULL DEFAULT 0,\n    estimated_cost_usd REAL NOT NULL DEFAULT 0,\n    actual_cost_usd REAL NOT NULL DEFAULT 0,\n    cost_status TEXT,\n    cost_source TEXT,\n    first_seen REAL,\n    last_seen REAL,\n    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)\n)")
            cursor.execute("INSERT OR IGNORE INTO session_model_usage (\n                       session_id, model, billing_provider, billing_base_url,\n                       billing_mode, task, api_call_count, input_tokens,\n                       output_tokens, cache_read_tokens, cache_write_tokens,\n                       reasoning_tokens, estimated_cost_usd, actual_cost_usd,\n                       cost_status, cost_source, first_seen, last_seen\n                   )\n                   SELECT session_id, model,\n                          COALESCE(billing_provider, ''),\n                          COALESCE(billing_base_url, ''),\n                          COALESCE(billing_mode, ''),\n                          COALESCE(task, ''),\n                          api_call_count, input_tokens,\n                          output_tokens, cache_read_tokens, cache_write_tokens,\n                          reasoning_tokens, estimated_cost_usd, actual_cost_usd,\n                          cost_status, cost_source, first_seen, last_seen\n                   FROM session_model_usage_legacy_pk")
            cursor.execute('DROP TABLE session_model_usage_legacy_pk')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_model_usage_session ON session_model_usage(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_model_usage_model ON session_model_usage(model)')
        except sqlite3.OperationalError as exc:
            logger.debug('session_model_usage PK heal skipped: %s', exc)
        finally:
            cursor.execute('PRAGMA foreign_keys=ON')

    def _init_schema(self):
        """Create tables and FTS if they don't exist, reconcile columns.

        Schema management follows the declarative reconciliation pattern
        (Beets, sqlite-utils): SCHEMA_SQL is the single source of truth.
        On existing databases, _reconcile_columns() diffs live columns
        against SCHEMA_SQL and ADDs any missing ones.  This eliminates
        the version-gated migration chain for column additions, making
        it impossible for reordered or inserted migrations to skip columns.

        The schema_version table is retained for future data migrations
        (transforming existing rows) which cannot be handled declaratively.
        """
        cursor = self._conn.cursor()
        cursor.executescript(SCHEMA_SQL)
        self._reconcile_columns(cursor)
        self._heal_gateway_routing_pk(cursor)
        self._heal_session_model_usage_pk(cursor)
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id ON messages(session_id, platform_message_id) WHERE platform_message_id IS NOT NULL')
        except sqlite3.OperationalError as exc:
            logger.debug('idx_messages_platform_msg_id create skipped: %s', exc)
        cursor.executescript(DEFERRED_INDEX_SQL)
        try:
            cursor.execute('UPDATE messages SET active = 1 WHERE active IS NULL')
        except sqlite3.OperationalError:
            pass
        fts5_available = self._sqlite_supports_fts5(cursor)
        fts_migrations_complete = True
        if not fts5_available:
            self._drop_fts_triggers(cursor)
        cursor.execute('SELECT version FROM schema_version LIMIT 1')
        row = cursor.fetchone()
        if row is None:
            cursor.execute('INSERT INTO schema_version (version) VALUES (?)', (SCHEMA_VERSION,))
        else:
            current_version = row['version'] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 10 and SCHEMA_VERSION == 10:
                if fts5_available:
                    _fts_trigram_exists = self._fts_table_probe(cursor, 'messages_fts_trigram')
                    if _fts_trigram_exists is False:
                        if self._ensure_fts_schema(cursor, 'messages_fts_trigram', FTS_TRIGRAM_SQL):
                            cursor.execute('INSERT INTO messages_fts_trigram(rowid, content) SELECT id, content FROM messages WHERE content IS NOT NULL')
                        else:
                            fts_migrations_complete = False
                    elif _fts_trigram_exists is None:
                        fts_migrations_complete = False
                else:
                    fts_migrations_complete = False
            if current_version < 11 and SCHEMA_VERSION < 23:
                pass
            if current_version < 16:
                try:
                    cursor.execute(f"UPDATE sessions SET model_config = json_set(COALESCE(model_config, '{{}}'), '$._delegate_from', parent_session_id) WHERE parent_session_id IS NOT NULL AND json_extract(COALESCE(model_config, '{{}}'), '$._delegate_from') IS NULL AND {_ephemeral_child_sql('sessions')}")
                    cursor.execute("UPDATE sessions SET model_config = json_set(COALESCE(model_config, '{}'), '$._delegate_from', '__orphaned__') WHERE parent_session_id IS NULL AND json_extract(COALESCE(model_config, '{}'), '$._delegate_from') IS NULL AND json_extract(COALESCE(model_config, '{}'), '$._branched_from') IS NULL AND title IS NULL AND message_count <= 25 AND EXISTS (SELECT 1 FROM messages m             WHERE m.session_id = sessions.id AND m.role = 'tool') AND NOT EXISTS (SELECT 1 FROM sessions ch                 WHERE ch.parent_session_id = sessions.id)")
                except sqlite3.OperationalError:
                    pass
            if current_version < 18:
                try:
                    self._backfill_gateway_metadata_from_sessions_json(cursor)
                except Exception as exc:
                    logger.debug('v18 gateway metadata backfill skipped: %s', exc)
            if current_version < 20:
                try:
                    cursor.execute("INSERT OR IGNORE INTO session_model_usage (\n                               session_id, model, billing_provider,\n                               billing_base_url, billing_mode,\n                               api_call_count, input_tokens,\n                               output_tokens, cache_read_tokens,\n                               cache_write_tokens, reasoning_tokens,\n                               estimated_cost_usd, actual_cost_usd,\n                               cost_status, cost_source, first_seen, last_seen\n                           )\n                           SELECT id, COALESCE(model, 'unknown'),\n                                  COALESCE(billing_provider, ''),\n                                  COALESCE(billing_base_url, ''),\n                                  COALESCE(billing_mode, ''),\n                                  COALESCE(api_call_count, 0),\n                                  COALESCE(input_tokens, 0),\n                                  COALESCE(output_tokens, 0),\n                                  COALESCE(cache_read_tokens, 0),\n                                  COALESCE(cache_write_tokens, 0),\n                                  COALESCE(reasoning_tokens, 0),\n                                  COALESCE(estimated_cost_usd, 0),\n                                  COALESCE(actual_cost_usd, 0),\n                                  cost_status, cost_source,\n                                  started_at, COALESCE(ended_at, started_at)\n                           FROM sessions\n                           WHERE COALESCE(input_tokens, 0)\n                                 + COALESCE(output_tokens, 0)\n                                 + COALESCE(cache_read_tokens, 0)\n                                 + COALESCE(cache_write_tokens, 0)\n                                 + COALESCE(reasoning_tokens, 0) > 0")
                except sqlite3.OperationalError:
                    pass
            if current_version < 22:
                try:
                    legacy_pk = cursor.execute("SELECT COUNT(*) FROM pragma_table_info('session_model_usage') WHERE name = 'task' AND pk > 0").fetchone()[0]
                    if not legacy_pk:
                        cursor.execute('ALTER TABLE session_model_usage RENAME TO session_model_usage_v21')
                        cursor.execute("CREATE TABLE session_model_usage (\n                                   session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n                                   model TEXT NOT NULL,\n                                   billing_provider TEXT NOT NULL DEFAULT '',\n                                   billing_base_url TEXT NOT NULL DEFAULT '',\n                                   billing_mode TEXT NOT NULL DEFAULT '',\n                                   task TEXT NOT NULL DEFAULT '',\n                                   api_call_count INTEGER NOT NULL DEFAULT 0,\n                                   input_tokens INTEGER NOT NULL DEFAULT 0,\n                                   output_tokens INTEGER NOT NULL DEFAULT 0,\n                                   cache_read_tokens INTEGER NOT NULL DEFAULT 0,\n                                   cache_write_tokens INTEGER NOT NULL DEFAULT 0,\n                                   reasoning_tokens INTEGER NOT NULL DEFAULT 0,\n                                   estimated_cost_usd REAL NOT NULL DEFAULT 0,\n                                   actual_cost_usd REAL NOT NULL DEFAULT 0,\n                                   cost_status TEXT,\n                                   cost_source TEXT,\n                                   first_seen REAL,\n                                   last_seen REAL,\n                                   PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)\n                               )")
                        cursor.execute("INSERT INTO session_model_usage (\n                                   session_id, model, billing_provider, billing_base_url,\n                                   billing_mode, task, api_call_count, input_tokens,\n                                   output_tokens, cache_read_tokens, cache_write_tokens,\n                                   reasoning_tokens, estimated_cost_usd, actual_cost_usd,\n                                   cost_status, cost_source, first_seen, last_seen\n                               )\n                               SELECT session_id, model, billing_provider, billing_base_url,\n                                      billing_mode, '', api_call_count, input_tokens,\n                                      output_tokens, cache_read_tokens, cache_write_tokens,\n                                      reasoning_tokens, estimated_cost_usd, actual_cost_usd,\n                                      cost_status, cost_source, first_seen, last_seen\n                               FROM session_model_usage_v21")
                        cursor.execute('DROP TABLE session_model_usage_v21')
                        cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_model_usage_session ON session_model_usage(session_id)')
                        cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_model_usage_model ON session_model_usage(model)')
                except sqlite3.OperationalError as exc:
                    logger.debug('v22 session_model_usage rebuild skipped: %s', exc)
            if current_version < 23:
                if fts5_available and self._db_has_legacy_inline_fts(cursor):
                    self.set_meta('fts_optimize_available', '1', cursor=cursor)
            if current_version < 25:
                self._dedupe_legacy_system_prompts(cursor)
            if fts5_available and (not self._db_has_legacy_inline_fts(cursor)) and (cursor.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_high_water' LIMIT 1").fetchone() is None) and (not self._has_fts_trash(cursor)) and (not self._fts_external_index_empty_with_messages(cursor)):
                self.set_meta('fts_storage_version', str(FTS_STORAGE_VERSION), cursor=cursor)
            if current_version < SCHEMA_VERSION and fts_migrations_complete and fts5_available:
                cursor.execute('UPDATE schema_version SET version = ?', (SCHEMA_VERSION,))
        title_index_sql = 'CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL'
        try:
            cursor.execute(title_index_sql)
        except sqlite3.IntegrityError:
            try:
                cursor.execute('UPDATE sessions AS older\n                       SET title = NULL\n                       WHERE title IS NOT NULL\n                         AND EXISTS (\n                             SELECT 1 FROM sessions AS newer\n                             WHERE newer.title = older.title\n                               AND newer.rowid > older.rowid\n                         )')
                logger.warning('Cleared %d duplicate session title(s) while restoring the unique index', cursor.rowcount)
                cursor.execute(title_index_sql)
            except sqlite3.Error:
                logger.exception('Could not repair duplicate session titles; unique title index not created')
        except sqlite3.OperationalError:
            pass
        if fts5_available:
            if self._db_has_legacy_inline_fts(cursor):
                triggers_need_repair = self._fts_trigger_count(cursor) < len(_FTS_TRIGGERS)
                self._fts_enabled = self._ensure_fts_schema(cursor, 'messages_fts', LEGACY_FTS_SQL)
                if self._fts_enabled:
                    trigram_enabled = self._ensure_fts_schema(cursor, 'messages_fts_trigram', LEGACY_FTS_TRIGRAM_SQL)
                    self._trigram_available = trigram_enabled
                    if triggers_need_repair:
                        self._rebuild_legacy_fts_indexes(cursor, include_trigram=trigram_enabled)
            else:
                triggers_need_repair = self._fts_trigger_count(cursor) < len(_FTS_TRIGGERS)
                self._fts_enabled = self._ensure_fts_schema(cursor, 'messages_fts', FTS_SQL)
                if self._fts_enabled:
                    trigram_enabled = self._ensure_fts_schema(cursor, 'messages_fts_trigram', FTS_TRIGRAM_SQL)
                    self._trigram_available = trigram_enabled
                    if triggers_need_repair:
                        self._rebuild_fts_indexes(cursor, include_trigram=trigram_enabled)
                    self._ensure_fts_cjk_schema(cursor)
            if getattr(self, '_fts_enabled', False):
                self._migrate_broad_fts_update_triggers(cursor)
        self._conn.commit()

    def _backfill_gateway_metadata_from_sessions_json(self, cursor: sqlite3.Cursor) -> None:
        """One-time v18 backfill of gateway metadata from sessions.json.

        Existing gateway sessions predate the display_name / origin_json /
        expiry_finalized columns; copy what sessions.json knows so consumers
        can switch to state.db without losing pre-migration sessions.
        Only fills NULL columns — never overwrites data written by newer code.
        """
        sessions_file = get_hermes_home() / 'sessions' / 'sessions.json'
        if not sessions_file.exists():
            return
        with open(sessions_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        for key, entry in data.items():
            if str(key).startswith('_') or not isinstance(entry, dict):
                continue
            session_id = entry.get('session_id')
            if not session_id:
                continue
            origin = entry.get('origin')
            cursor.execute('UPDATE sessions\n                   SET session_key = COALESCE(session_key, ?),\n                       chat_id = COALESCE(chat_id, ?),\n                       chat_type = COALESCE(chat_type, ?),\n                       thread_id = COALESCE(thread_id, ?),\n                       display_name = COALESCE(display_name, ?),\n                       origin_json = COALESCE(origin_json, ?),\n                       expiry_finalized = CASE\n                           WHEN COALESCE(expiry_finalized, 0) = 0 AND ? = 1 THEN 1\n                           ELSE expiry_finalized\n                       END\n                   WHERE id = ?', (entry.get('session_key') or key, (origin or {}).get('chat_id') if isinstance(origin, dict) else None, entry.get('chat_type'), (origin or {}).get('thread_id') if isinstance(origin, dict) else None, entry.get('display_name'), json.dumps(origin) if isinstance(origin, dict) else None, 1 if entry.get('expiry_finalized') or entry.get('memory_flushed') else 0, str(session_id)))