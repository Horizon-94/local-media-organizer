"""Scheduler ownership at the V2 SQLite transaction boundary.

Standalone V2 fixture databases have no scheduler_runs table. Product databases
require an explicit immutable owner. BEGIN IMMEDIATE serializes the ownership
check, every mutation (including closure/index SQL), and scheduler switching.
"""
from __future__ import annotations

import sqlite3
import re

_LINEAGE_READ = re.compile(r'\b(?:FROM|JOIN)\s+(?:(?:main|temp)\.)?["`\[]?v2_current_jobs\b', re.I)
_STAGE_FILTER = re.compile(r'\b(?:\w+\.)?stage\s*=\s*\?', re.I)
_KEY_FILTER = re.compile(r'\bWHERE\s+(?:\w+\.)?execution_key\s*=\s*\?', re.I)
_KEY_SET_FILTER = re.compile(r'\bWHERE\s+(?:\w+\.)?execution_key\s+IN\s*\(\s*SELECT\s+value\s+FROM\s+json_each\s*\(\s*\?\s*\)', re.I)
_NARROW_VISUAL_STAGES = frozenset(('dhash','geometry','quality','yoloe','openclip','insightface',
    'qwen','ocr','whisper','vad','audio_extract','embedding'))


class EpochCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        sql = self.connection._prepare(sql, parameters)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection._begin_owned()
        self.connection.lineage_owned_clean = False
        return super().executemany(sql, parameters)

    def executescript(self, sql):
        return self.connection.executescript(sql)


class EpochConnection(sqlite3.Connection):
    scheduler_owner = None

    def rollback(self):
        # A lazy proof first created inside this transaction may have its TEMP
        # table/view rolled back too. Never keep a Python flag for absent DDL.
        super().rollback()
        self.lineage_proof_enabled = False

    def _assert_owned(self):
        if not super().execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_runs'").fetchone():
            return
        from .scheduler_identity import SchedulerOwnershipError
        owner = self.scheduler_owner
        if not owner:
            raise SchedulerOwnershipError('scheduler_identity_required')
        from .scheduler_identity import EVENT_DRIVEN_V2, PIPELINE_CONTRACT_VERSION
        if owner.get('scheduler_mode', EVENT_DRIVEN_V2) != EVENT_DRIVEN_V2 or owner.get('pipeline_contract_version', PIPELINE_CONTRACT_VERSION) != PIPELINE_CONTRACT_VERSION:
            raise SchedulerOwnershipError('unsupported_v2_product_contract')
        persisted = super().execute('SELECT pipeline_contract_version FROM scheduler_runs WHERE scheduler_run_id=?', (owner['scheduler_run_id'],)).fetchone()
        if persisted and persisted[0] != PIPELINE_CONTRACT_VERSION:
            raise SchedulerOwnershipError('unsupported_v2_product_contract')
        row = super().execute(
            "SELECT 1 FROM scheduler_runs WHERE task_id=? AND scheduler_run_id=? "
            "AND execution_epoch=? AND scheduler_mode='event_driven_v2' "
            "AND status IN ('STARTING','RUNNING','RECOVERABLE')",
            (owner['task_id'], owner['scheduler_run_id'], owner['execution_epoch']),
        ).fetchone()
        if not row:
            raise SchedulerOwnershipError('stale_scheduler_execution_epoch')

    def _begin_owned(self):
        if self.in_transaction:
            return
        super().execute('BEGIN IMMEDIATE')
        try:
            self._assert_owned()
            self.lineage_owned_clean = True
        except BaseException:
            super().rollback()
            self.lineage_proof_enabled = False
            raise

    def _narrow_lineage_read(self, sql, parameters):
        # These unit jobs have tiny ancestry in the measured product graph.
        # Keep the exact correlated predicate instead of rebuilding ALL roots.
        # This is a query-plan choice, never a dependency/evidence bypass.
        # Index payload reads have large shared ancestry. Only the parent
        # existence probe should use the exact root-local fallback here.
        if (_KEY_SET_FILTER.search(sql) and
                re.match(r'^\s*SELECT\s+execution_key\s+FROM\s+v2_current_jobs\b',sql,re.I)):
            return True
        for pattern,kind in ((_STAGE_FILTER,'stage'),(_KEY_FILTER,'key')):
            match=pattern.search(sql)
            if not match:continue
            try:value=parameters[sql[:match.end()].count('?')-1]
            except (IndexError,KeyError,TypeError):continue
            if kind=='key':
                # A point lookup needs only this root's exact ancestry, even
                # for projection/index jobs. Rebuilding every root here can
                # hold an event's write transaction for minutes.
                return True
            if value in _NARROW_VISUAL_STAGES:return True
        return False

    def _prepare(self, sql, parameters=()):
        command = sql.lstrip().upper()
        if command in ('BEGIN', 'BEGIN TRANSACTION'):
            # Evidence snapshots are readers. Converting BEGIN to IMMEDIATE
            # held the sole WAL writer throughout lineage/face calculations.
            # Mutations still use their explicit IMMEDIATE transaction; an
            # interleaved owner change prevents a stale snapshot upgrade.
            if not self.in_transaction:
                super().execute('BEGIN')
                try:self._assert_owned()
                except BaseException:
                    super().rollback()
                    raise
                self.lineage_owned_clean=False
            return 'SELECT 1'
        if (_LINEAGE_READ.search(sql) and (getattr(self,'lineage_cache_requested',False)
                or getattr(self,'lineage_proof_enabled',False)) and
                not self._narrow_lineage_read(sql,parameters)):
            stale = not getattr(self,'lineage_proof_enabled',False)
            if not stale:
                generation=super().execute('SELECT generation FROM v2_lineage_cache_generation WHERE singleton=1').fetchone()[0]
                proof=super().execute('SELECT generation FROM temp.current_lineage_cache LIMIT 1').fetchone()[0]
                stale = proof!=generation
            if stale:
                from .v2_current_jobs_cache import install_view
                self.lineage_proof_enabled = install_view(self, fresh_owned_snapshot=(
                    self.in_transaction and getattr(self,'lineage_owned_clean',False)))
        # Pure reads need no writer lock. Mutations, DDL, WITH and explicit
        # transactions take the lock before checking authoritative ownership.
        if command.startswith(('SELECT ', 'PRAGMA TABLE_INFO(', 'PRAGMA BUSY_TIMEOUT', 'PRAGMA FOREIGN_KEYS')):
            self._assert_owned()
        else:
            self._begin_owned()
        if command in ('BEGIN', 'BEGIN IMMEDIATE', 'BEGIN TRANSACTION'):
            return 'SELECT 1'
        if not command.startswith(('SELECT ', 'PRAGMA ')):
            self.lineage_owned_clean = False
        return sql

    def __enter__(self):
        self._assert_owned()
        return self

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

    def execute(self, sql, parameters=()):
        return super().execute(self._prepare(sql,parameters), parameters)

    def executemany(self, sql, parameters):
        self._begin_owned()
        self.lineage_owned_clean = False
        return super().executemany(sql, parameters)

    def cursor(self, factory=EpochCursor):
        return super().cursor(factory)

    def executescript(self, script):
        # sqlite3.executescript implicitly commits first, which would release
        # the owner lock. Execute complete statements inside our transaction.
        statement = ''
        result = None
        for char in script:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                result = self.execute(statement)
                statement = ''
        if statement.strip():
            result = self.execute(statement)
        return result


def connect_runtime(database, owner):
    connection = sqlite3.connect(database, timeout=30, factory=EpochConnection)
    connection.row_factory = sqlite3.Row
    connection.scheduler_owner = owner
    # Configure before opening the guarded transaction.
    sqlite3.Connection.execute(connection, 'PRAGMA busy_timeout=30000')
    sqlite3.Connection.execute(connection, 'PRAGMA foreign_keys=ON')
    return connection


def add_epoch_columns(connection, table):
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
    if 'scheduler_run_id' not in columns:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN scheduler_run_id TEXT')
    if 'execution_epoch' not in columns:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN execution_epoch INTEGER')
