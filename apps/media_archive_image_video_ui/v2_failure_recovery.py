"""Explicit Resume recovery for an audited, ownerless SQLite failure."""
import json
import os
import time
import hashlib
from pathlib import Path
from .central_database import connect
from .v2_product_lifecycle import ProductExecutionLease

INDEX_WRAPPER_ERROR = "descriptor 'execute' for 'sqlite3.Connection' objects doesn't apply to a 'TimedConnection' object"


def _index_wrapper_repair_verified():
    """Exercise the installed index path through the actual runtime wrapper."""
    import sqlite3
    from .v2_performance import Performance, TimedConnection
    from .v2_index_lineage import current_input_jobs
    with sqlite3.connect(':memory:') as raw:
        raw.row_factory = sqlite3.Row
        raw.executescript('''CREATE TABLE v2_jobs(execution_key,status,source_id,source_revision,result_sha,provenance,result_json);
            CREATE TABLE v2_job_dependencies(child_key,parent_key);
            CREATE TABLE v2_active_sources(source_id,source_revision);''')
        try:
            return current_input_jobs(TimedConnection(raw, Performance()), {'missing'}, set()) == ({}, {})
        except Exception:
            return False


def recover_failed_media_tools_run(task, run, *, request_id):
    """Explicit, audited retry after repairing a GUI media-tool environment.

    Only proven missing-ffmpeg producers and Whisper jobs are reopened. Valid
    evidence, unrelated failures, source files and pinned contracts are intact.
    This is never called by ordinary Resume without an explicit request.
    """
    import shutil
    if not request_id:
        raise RuntimeError('media_tool_recovery_request_missing')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('media_tool_recovery_environment_not_fixed')
    database=Path(task['database']);rid=run['scheduler_run_id'];epoch=run['execution_epoch']
    with ProductExecutionLease(database,rid),connect(database) as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(rid,)).fetchone()
        if current['task_id']!=task['task_id'] or current['execution_epoch']!=epoch:
            raise RuntimeError('media_tool_recovery_identity_mismatch')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_media_tool_recoveries'").fetchone():
            previous=c.execute('SELECT 1 FROM v2_media_tool_recoveries WHERE scheduler_run_id=? AND request_id=?',(rid,request_id)).fetchone()
            if previous:return dict(current)
        if current['status']!='COMPLETED':raise RuntimeError('media_tool_recovery_requires_terminal_run')
        life=c.execute('SELECT worker_pid FROM v2_product_lifecycle WHERE scheduler_run_id=?',(rid,)).fetchone()
        pid=int(life['worker_pid'] or 0) if life else 0
        if pid>1:
            try:os.kill(pid,0)
            except ProcessLookupError:pass
            else:raise RuntimeError('media_tool_recovery_owner_alive')
        has_admissions=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_admissions'").fetchone()
        if c.execute("SELECT 1 FROM v2_jobs WHERE scheduler_run_id=? AND status='RUNNING'",(rid,)).fetchone() or (has_admissions and c.execute("SELECT 1 FROM scheduler_admissions WHERE scheduler_run_id=? AND status='ACTIVE'",(rid,)).fetchone()):
            raise RuntimeError('media_tool_recovery_active_ownership')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('media_tool_recovery_integrity_failed')
        producers=[]
        for row in c.execute("SELECT * FROM v2_product_sources WHERE scheduler_run_id=? AND state='FAILED_FINAL' AND error LIKE 'ContractError: producer_ingest_without_current_eos:%'",(rid,)):
            reports=[json.loads(r[0]) for r in c.execute('SELECT report FROM v2_decode_attempts WHERE source_id=?',(row['source_id'],))]
            if reports and all("No such file or directory: 'ffmpeg'" in (r.get('stderr_tail') or r.get('ffmpeg_stderr_tail') or '')
                               and int(r.get('produced_count',r.get('produced_frame_count',0)) or 0)==0 for r in reports):
                producers.append(dict(row))
        jobs=[dict(r) for r in c.execute("SELECT * FROM v2_jobs WHERE scheduler_run_id=? AND execution_epoch=? AND stage='whisper' AND status='FAILED_FINAL' AND error LIKE ?",(rid,epoch,"FileNotFoundError:%No such file or directory: 'ffmpeg'%"))]
        affected={r['source_id'] for r in producers+jobs}
        if not affected:raise RuntimeError('media_tool_recovery_no_proven_failures')
        for sid in affected:
            source=c.execute('SELECT source_json FROM v2_product_sources WHERE scheduler_run_id=? AND source_id=?',(rid,sid)).fetchone()
            metadata=json.loads(source[0]) if source else {}
            if metadata.get('media_kind')!='video' or not Path(metadata.get('source_path','')).is_file():
                raise RuntimeError('media_tool_recovery_source_unavailable:'+sid)
        for job in jobs:
            if not Path(json.loads(job['payload'])['path']).is_file():
                raise RuntimeError('media_tool_recovery_audio_artifact_unavailable:'+job['source_id'])
        outcomes=[dict(r) for r in c.execute('SELECT * FROM v2_source_outcomes') if r['source_id'] in affected]
        audit={'contract':'explicit_missing_media_tool_recovery_v1','previous_run':dict(current),
               'producer_sources':producers,'whisper_jobs':jobs,'previous_outcomes':outcomes,
               'affected_product_sources':[dict(r) for r in c.execute('SELECT * FROM v2_product_sources WHERE scheduler_run_id=?',(rid,)) if r['source_id'] in affected],
               'locked_done_headers':[list(r) for r in c.execute("SELECT execution_key,result_sha,attempts FROM v2_jobs WHERE status='DONE' ORDER BY execution_key")]}
        c.execute('CREATE TABLE IF NOT EXISTS v2_media_tool_recoveries(scheduler_run_id TEXT NOT NULL,request_id TEXT NOT NULL,execution_epoch INTEGER NOT NULL,recovered_at REAL NOT NULL,audit_json TEXT NOT NULL,PRIMARY KEY(scheduler_run_id,request_id))')
        c.execute('INSERT INTO v2_media_tool_recoveries VALUES(?,?,?,?,?)',(rid,request_id,epoch,time.time(),json.dumps(audit,sort_keys=True)))
        for row in producers:
            # The normal producer will create a fresh attempt/revision. Old
            # aborted history and active tombstones are not deleted or edited.
            c.execute("UPDATE v2_product_sources SET state='NEW' WHERE scheduler_run_id=? AND source_id=?",(rid,row['source_id']))
        for job in jobs:
            # Reuse the formal retry-generation contract in this transaction;
            # never open a competing connection or discard attempt history.
            from .v2_runtime import WorkerStore
            from .v2_producer import SamplingPolicy
            store=object.__new__(WorkerStore)
            store.scheduler_owner={'task_id':task['task_id'],'scheduler_run_id':rid,'execution_epoch':epoch,
                'scheduler_mode':run['scheduler_mode'],'pipeline_contract_version':run['pipeline_contract_version']}
            store.policy=SamplingPolicy()
            store.clock=time.time
            store.retry_final(job['execution_key'],request_id=request_id+':'+job['execution_key'],
                actor='explicit_user_media_tool_recovery',reason='declared_media_tools_environment_fixed',connection=c)
            c.execute("UPDATE v2_product_sources SET state='INGESTED',error=NULL WHERE scheduler_run_id=? AND source_id=? AND source_revision=? AND state IN ('FAILED_FINAL','INGESTED')",(rid,job['source_id'],job['source_revision']))
        c.execute('UPDATE v2_product_lifecycle SET pass_index=0,worker_pid=NULL,stop_requested=0,updated_at=? WHERE scheduler_run_id=?',(time.time(),rid))
        c.execute("UPDATE scheduler_runs SET status='RECOVERABLE',recovered_at=?,finished_at=NULL WHERE scheduler_run_id=?",(time.time(),rid))
        return dict(c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(rid,)).fetchone())


def recover_failed_sqlite_run(task, run):
    if run['status'] not in ('FAILED','CANCELLED'): return run
    path=Path(task['log_path'])
    with path.open('rb') as h:
        h.seek(0,2); size=h.tell();h.seek(max(0,size-65536));lines=h.read().splitlines()
    evidence=None
    index_wrapper_failure=False
    for line in reversed(lines):
        try:r=json.loads(line)
        except (ValueError,UnicodeDecodeError):continue
        if (r.get('scheduler_run_id')==run['scheduler_run_id'] and
            r.get('execution_epoch')==run['execution_epoch'] and
            r.get('reason_code') in ('PRODUCT_EXECUTION_FAILED','TASK_CANCELLED')):
            # Only the latest terminal response is authoritative. An older
            # lock error must never justify recovery of a newer model failure.
            index_wrapper_failure = (run['status']=='FAILED' and r.get('reason_code')=='PRODUCT_EXECUTION_FAILED'
                and r.get('error')==INDEX_WRAPPER_ERROR and r.get('result',{}).get('failed_stage_key') is None
                and any(b'v2_index_lineage.py' in entry for entry in lines)
                and _index_wrapper_repair_verified())
            if (index_wrapper_failure or (run['status']=='FAILED' and r.get('reason_code')=='PRODUCT_EXECUTION_FAILED' and
                 r.get('error') in ('database is locked','database table is locked','abort due to ROLLBACK')) or
                (run['status']=='CANCELLED' and r.get('reason_code')=='TASK_CANCELLED')):
                evidence=r
            break
    if evidence is None:raise RuntimeError('failed_run_recovery_reason_unproven')
    database=Path(task['database']);rid=run['scheduler_run_id'];epoch=run['execution_epoch']
    with ProductExecutionLease(database,rid),connect(database) as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT status,execution_epoch,task_id,finished_at FROM scheduler_runs WHERE scheduler_run_id=?',(rid,)).fetchone()
        if current['task_id']!=task['task_id'] or current['execution_epoch']!=epoch:raise RuntimeError('failed_run_recovery_identity_mismatch')
        if current['status']=='RECOVERABLE':return {**run,'status':'RECOVERABLE'}
        if current['status']!=run['status']:raise RuntimeError('failed_run_recovery_state_changed')
        life=c.execute('SELECT worker_pid FROM v2_product_lifecycle WHERE scheduler_run_id=? AND execution_epoch=?',(rid,epoch)).fetchone()
        if life is None:raise RuntimeError('failed_run_recovery_owner_missing')
        pid=int(life['worker_pid'] or 0)
        if pid>1:
            try:os.kill(pid,0)
            except ProcessLookupError:pass
            else:raise RuntimeError('failed_run_recovery_owner_alive')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('failed_run_recovery_integrity_failed')
        if index_wrapper_failure and (c.execute("SELECT 1 FROM v2_jobs WHERE scheduler_run_id=? AND status IN ('RUNNING','FAILED_FINAL')",(rid,)).fetchone()
            or c.execute("SELECT 1 FROM scheduler_admissions WHERE scheduler_run_id=? AND status='ACTIVE'",(rid,)).fetchone()):
            raise RuntimeError('index_wrapper_recovery_requires_quiescent_successful_jobs')
        c.execute('CREATE TABLE IF NOT EXISTS v2_product_run_recoveries(recovery_id TEXT PRIMARY KEY,scheduler_run_id TEXT,execution_epoch INTEGER,failed_at REAL,recovered_at REAL,old_worker_pid INTEGER,evidence_json TEXT)')
        key=hashlib.sha256(json.dumps((rid,epoch,current['finished_at'])).encode()).hexdigest()
        audit={'failure':evidence,'verified_repair':'index_wrapper_connection' if index_wrapper_failure else 'sqlite_lock_or_cancel',
               'orphan_jobs':[dict(r) for r in c.execute("SELECT * FROM v2_jobs WHERE scheduler_run_id=? AND execution_epoch=? AND status='RUNNING'",(rid,epoch))],
               'orphan_admissions':[dict(r) for r in c.execute("SELECT * FROM scheduler_admissions WHERE scheduler_run_id=? AND execution_epoch=? AND status='ACTIVE'",(rid,epoch))]}
        # Infrastructure lock failures are not permanent decode/model evidence
        # failures. Reopen only those exact uncommitted failures via the formal
        # audited retry-generation API, inside this already-owned recovery tx.
        from .v2_current_jobs_cache import install_readonly_snapshot_view
        install_readonly_snapshot_view(c)
        locked=[dict(r) for r in c.execute("SELECT * FROM v2_current_jobs WHERE scheduler_run_id=? AND execution_epoch=? AND status='FAILED_FINAL' AND result_sha IS NULL AND error IN ('database is locked','database table is locked')",(rid,epoch))]
        audit['final_lock_jobs']=locked
        c.execute('INSERT OR IGNORE INTO v2_product_run_recoveries VALUES(?,?,?,?,?,?,?)',(key,rid,epoch,current['finished_at'],time.time(),pid,json.dumps(audit,sort_keys=True)))
        from .v2_runtime import WorkerStore
        from .v2_producer import SamplingPolicy
        store=object.__new__(WorkerStore);store.scheduler_owner={'task_id':task['task_id'],'scheduler_run_id':rid,'execution_epoch':epoch}
        store.policy=SamplingPolicy();store.clock=time.time
        for job in locked:
            store.retry_final(job['execution_key'],request_id=key+':'+job['execution_key'],actor='audited_sqlite_failure_recovery',reason='sqlite_lock_runtime_fixed',connection=c)
        c.execute("UPDATE v2_jobs SET status='PENDING',attempts=MAX(0,attempts-1),owner=NULL,token=NULL,lease_until=NULL WHERE scheduler_run_id=? AND execution_epoch=? AND status='RUNNING'",(rid,epoch))
        c.execute("UPDATE scheduler_admissions SET status='RELEASED',released_at=?,release_reason='audited_sqlite_failure_recovery' WHERE scheduler_run_id=? AND execution_epoch=? AND status='ACTIVE'",(time.time(),rid,epoch))
        c.execute('UPDATE v2_product_lifecycle SET worker_pid=NULL,stop_requested=0,updated_at=? WHERE scheduler_run_id=? AND execution_epoch=?',(time.time(),rid,epoch))
        c.execute("UPDATE scheduler_runs SET status='RECOVERABLE',recovered_at=?,finished_at=NULL WHERE scheduler_run_id=? AND execution_epoch=? AND status=?",(time.time(),rid,epoch,run['status']))
    return {**run,'status':'RECOVERABLE'}
