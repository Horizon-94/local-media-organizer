"""Recoverable task.json projection of the authoritative DB scheduler owner."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .central_database import connect
from .scheduler_identity import SchedulerOwnershipError, ensure_scheduler_identity_schema

IDENTITY_FIELDS = (
    'scheduler_mode', 'scheduler_run_id', 'execution_epoch',
    'pipeline_contract_version', 'product_schema_version',
)


def projected_run_status(con, run):
    """Completion is terminal, not a promise that every required job succeeded."""
    if run['status'] == 'COMPLETED':
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'v2_current_jobs' in tables:
            from .v2_current_jobs_cache import install_readonly_view
            install_readonly_view(con)
            if current_task_failure_count(con,run):
                return 'failed'
        if 'v2_product_sources' in tables and con.execute("SELECT 1 FROM v2_product_sources WHERE scheduler_run_id=? AND state='FAILED_FINAL' LIMIT 1", (run['scheduler_run_id'],)).fetchone():
            return 'failed'
        return 'success'
    return {'FAILED':'failed','CANCELLED':'cancelled','ABORTED':'aborted','RECOVERABLE':'recoverable'}.get(run['status'],'running')


def current_task_failure_count(con,run):
    """Current evidence can belong to earlier completed repair owners."""
    if run.get('task_id'):
        return con.execute('''SELECT COUNT(*) FROM v2_current_jobs j
            JOIN scheduler_runs r ON r.scheduler_run_id=j.scheduler_run_id
            WHERE r.task_id=? AND j.status='FAILED_FINAL' ''',(run['task_id'],)).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM v2_current_jobs WHERE scheduler_run_id=? AND status='FAILED_FINAL'",(run['scheduler_run_id'],)).fetchone()[0]


def remember_task(database, task_path, task):
    """Persist only task configuration for file recovery, never owner authority."""
    ensure_scheduler_identity_schema(database)
    configuration = {key: value for key, value in task.items()
                     if key not in (*IDENTITY_FIELDS, 'scheduler_run_status')}
    path = str(Path(task_path).resolve())
    configuration['task_path'] = path
    with connect(database) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute(
            'INSERT INTO scheduler_task_files(task_id,task_path,configuration_json) VALUES(?,?,?) '
            'ON CONFLICT(task_id) DO UPDATE SET task_path=excluded.task_path,configuration_json=excluded.configuration_json',
            (task['task_id'], path, json.dumps(configuration, ensure_ascii=False)),
        )


def project_current_task(database, task_id):
    """Serialize DB-to-file publication against switching, including replays.

    A crash before replace leaves an old/missing projection, repaired on next
    recovery. After replace, it names the locked DB owner. Low-level DB-only
    callers without registered task metadata need no file projection.
    """
    with connect(database) as con:
        con.execute('BEGIN IMMEDIATE')
        metadata = con.execute('SELECT * FROM scheduler_task_files WHERE task_id=?', (task_id,)).fetchone()
        if metadata is None:
            return None
        current = con.execute(
            "SELECT * FROM scheduler_runs WHERE task_id=? ORDER BY execution_epoch DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if current is None:
            raise SchedulerOwnershipError('current_scheduler_run_missing')
        run = dict(current)
        path = Path(metadata['task_path'])
        task = json.loads(metadata['configuration_json'])
        if path.is_file():
            existing = json.loads(path.read_text(encoding='utf-8'))
            if existing.get('task_id') != task_id or Path(existing.get('database', '')).resolve() != Path(database).resolve():
                raise SchedulerOwnershipError('task_database_identity_mismatch')
            task.update(existing)
        task.update({key: run[key] for key in IDENTITY_FIELDS})
        task['scheduler_run_status'] = run['status']
        task['status'] = projected_run_status(con, run)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(task, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return run


def reconcile_task_scheduler(database, task_id):
    """Product restart completes pending DB transitions, then projects current."""
    from .scheduler_switching import recover_scheduler_switch
    ensure_scheduler_identity_schema(database)
    with connect(database, readonly=True) as con:
        pending = con.execute(
            "SELECT switch_id FROM scheduler_switches WHERE task_id=? AND state IN ('PREPARING','FENCED','TARGET_CREATED') ORDER BY created_at",
            (task_id,),
        ).fetchall()
    for row in pending:
        recover_scheduler_switch(database, row['switch_id'])
    current = project_current_task(database, task_id)
    if current is None:
        raise SchedulerOwnershipError('task_scheduler_metadata_missing')
    return current
