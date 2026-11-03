"""Durable product passes around the existing producer and worker graph.

This is bookkeeping, not another queue or worker loop. Decoder attempts and
WorkerStore claim counters retain their existing independent meanings.
"""
import json
import time
import uuid
import os
import fcntl

PASSES = ('PRIMARY', 'SWEEP_1', 'SWEEP_2')
SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_product_lifecycle(
 scheduler_run_id TEXT PRIMARY KEY, execution_epoch INTEGER NOT NULL,
 pass_index INTEGER NOT NULL DEFAULT 0 CHECK(pass_index BETWEEN 0 AND 2),
 stop_requested INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
 updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_product_sources(
 scheduler_run_id TEXT NOT NULL, source_id TEXT NOT NULL, source_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'NEW', source_revision TEXT, attempt_id TEXT,
 error TEXT, first_failed_at REAL, last_failed_at REAL,
 PRIMARY KEY(scheduler_run_id,source_id));
CREATE TABLE IF NOT EXISTS v2_product_source_attempts(
 attempt_id TEXT PRIMARY KEY, scheduler_run_id TEXT NOT NULL,
 execution_epoch INTEGER NOT NULL, source_id TEXT NOT NULL,
 pass_index INTEGER NOT NULL, status TEXT NOT NULL, source_revision TEXT,
 started_at REAL NOT NULL, finished_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS v2_product_job_passes(
 scheduler_run_id TEXT NOT NULL, execution_key TEXT NOT NULL,
 last_pass INTEGER NOT NULL,
 PRIMARY KEY(scheduler_run_id,execution_key));
"""


def migrate_lifecycle(database):
    from .central_database import connect
    with connect(database) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR REPLACE INTO app_schema_meta(meta_key,meta_value) VALUES('rc203_lifecycle_schema','1')")


def product_claim_predicate(c, owner):
    """SQL fragment shared by runnable detection and atomic claim."""
    if not owner or not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
        return '1'
    # Correlates to v2_current_jobs, not the attempts counter. Non-product
    # WorkerStore callers retain the documented low-level retry contract.
    return """NOT EXISTS (SELECT 1 FROM v2_product_lifecycle l
      WHERE l.scheduler_run_id=v2_current_jobs.scheduler_run_id AND
      (l.stop_requested=1 OR (v2_current_jobs.status<>'PENDING' AND EXISTS(
       SELECT 1 FROM v2_product_job_passes p WHERE p.scheduler_run_id=l.scheduler_run_id
       AND p.execution_key=v2_current_jobs.execution_key AND p.last_pass>=l.pass_index))))"""


class ProductExecutionLease:
    """OS lifetime lock acquired before constructing admission recovery."""
    def __init__(self, database, run_id):
        self.handle = open(str(database) + '.' + run_id + '.product.lock', 'a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('product_execution_already_owned')
    def close(self):
        self.handle.close()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class ProductLifecycle:
    def __init__(self, store, sources, *, resume_handoff=False, execution_lease=None):
        self.store = store
        self.owner = store.scheduler_owner
        self.run = self.owner['scheduler_run_id']
        self.epoch = self.owner['execution_epoch']
        self.owns_lock = execution_lease is None
        self.lock = execution_lease or ProductExecutionLease(store.db, self.run)
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO v2_product_lifecycle(scheduler_run_id,execution_epoch,updated_at) VALUES(?,?,?)',
                      (self.run, self.epoch, time.time()))
            row = c.execute('SELECT execution_epoch FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()
            if row[0] != self.epoch:
                from .scheduler_identity import SchedulerOwnershipError
                raise SchedulerOwnershipError('stale_product_lifecycle_epoch')
            for source in sources:
                sid = source['source_content_id']
                raw = json.dumps(source, sort_keys=True)
                old = c.execute('SELECT source_json FROM v2_product_sources WHERE scheduler_run_id=? AND source_id=?', (self.run, sid)).fetchone()
                if old and old[0] != raw:
                    raise ValueError('source_snapshot_changed_during_resume:' + sid)
                c.execute('INSERT OR IGNORE INTO v2_product_sources(scheduler_run_id,source_id,source_json) VALUES(?,?,?)', (self.run, sid, raw))
                if not old:
                    current = c.execute('SELECT a.source_revision FROM v2_active_sources a JOIN v2_source_epochs e ON e.source_id=a.source_id AND e.source_revision=a.source_revision WHERE a.source_id=? AND e.scheduler_run_id=? AND e.execution_epoch=?', (sid,self.run,self.epoch)).fetchone()
                    if current:
                        c.execute("UPDATE v2_product_sources SET state='INGESTED',source_revision=? WHERE scheduler_run_id=? AND source_id=?", (current[0],self.run,sid))
            # The exclusive OS lock proves no other product execution owns this
            # run. Revoke interrupted tokens before any resumed claims.
            c.execute("UPDATE v2_jobs SET status='PENDING',attempts=MAX(0,attempts-1),owner=NULL,token=NULL,lease_until=NULL WHERE scheduler_run_id=? AND execution_epoch=? AND status='RUNNING'", (self.run,self.epoch))
            c.execute("UPDATE v2_product_source_attempts SET status='HANDOFF',finished_at=? WHERE scheduler_run_id=? AND status='INGESTING'", (time.time(),self.run))
            c.execute("UPDATE v2_product_sources SET state='NEW' WHERE scheduler_run_id=? AND state='INGESTING'", (self.run,))
            c.execute('UPDATE v2_product_lifecycle SET stop_requested=CASE WHEN ? THEN 0 ELSE stop_requested END,worker_pid=?,updated_at=? WHERE scheduler_run_id=?', (resume_handoff,os.getpid(),time.time(),self.run))

    def close(self):
        if self.owns_lock:
            self.lock.close()

    def state(self):
        with self.store.connect() as c:
            return dict(c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone())

    def pending_ingest(self):
        with self.store.connect() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT source_json FROM v2_product_sources WHERE scheduler_run_id=? AND state='NEW' ORDER BY rowid", (self.run,))]

    def admitted(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute("SELECT source_id,source_revision FROM v2_product_sources WHERE scheduler_run_id=? AND state IN ('INGESTED','COMPLETE') AND source_revision IS NOT NULL", (self.run,))]

    def begin(self, source):
        sid = source['source_content_id']
        attempt = uuid.uuid4().hex
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            state = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()
            if state['stop_requested']:
                return None
            changed = c.execute("UPDATE v2_product_sources SET state='INGESTING',attempt_id=? WHERE scheduler_run_id=? AND source_id=? AND state='NEW'", (attempt, self.run, sid)).rowcount
            if not changed:
                return None
            c.execute("INSERT INTO v2_product_source_attempts VALUES(?,?,?,?,?,'INGESTING',NULL,?,NULL,NULL)", (attempt, self.run, self.epoch, sid, state['pass_index'], time.time()))
        return attempt

    def ingested(self, sid, attempt, items):
        revision = next(r['source_revision'] for r in items if r['source_id'] == sid)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE v2_product_source_attempts SET status='SUCCESS',source_revision=?,finished_at=? WHERE attempt_id=?", (revision, time.time(), attempt))
            c.execute("UPDATE v2_product_sources SET state='INGESTED',source_revision=?,error=NULL WHERE scheduler_run_id=? AND source_id=? AND attempt_id=?", (revision, self.run, sid, attempt))

    def failed_ingest(self, sid, attempt, error):
        # Revoke only this run's incomplete lineage. Old tombstones are never
        # activated. Errors before source_ready still have durable membership.
        with self.store.connect() as c:
            active = c.execute('SELECT a.source_revision FROM v2_active_sources a JOIN v2_source_epochs e ON e.source_id=a.source_id AND e.source_revision=a.source_revision WHERE a.source_id=? AND e.scheduler_run_id=? AND e.execution_epoch=?', (sid, self.run, self.epoch)).fetchone()
        if active:
            with self.store.connect() as c:
                c.execute('UPDATE v2_product_source_attempts SET source_revision=? WHERE attempt_id=?', (active[0],attempt))
            self.store.abort_decode_attempt(sid, active[0], str(error))
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()
            stopped = bool(life['stop_requested'])
            state = 'NEW' if stopped else ('FAILED_FINAL' if life['pass_index'] == 2 else 'RETRYABLE')
            now = time.time()
            c.execute('UPDATE v2_product_source_attempts SET status=?,error=?,finished_at=? WHERE attempt_id=?', ('HANDOFF' if stopped else state, str(error), now, attempt))
            c.execute('UPDATE v2_product_sources SET state=?,error=?,first_failed_at=CASE WHEN ? THEN first_failed_at ELSE COALESCE(first_failed_at,?) END,last_failed_at=CASE WHEN ? THEN last_failed_at ELSE ? END WHERE scheduler_run_id=? AND source_id=?', (state, str(error), stopped, now, stopped, now, self.run, sid))

    def refresh(self):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = list(c.execute("SELECT s.source_id,s.source_revision,o.status FROM v2_product_sources s LEFT JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.source_revision WHERE s.scheduler_run_id=? AND s.state IN ('INGESTED','COMPLETE')", (self.run,)))
            for row in rows:
                # Completion can be invalidated by a later projection. Never
                # let a cached COMPLETE hide new current work.
                state = ('COMPLETE' if row['status'] in ('COMPLETE','DEGRADED') else
                         'FAILED_FINAL' if row['status'] in ('FAILED_FINAL','FAILED','ABORTED') else 'INGESTED')
                c.execute('UPDATE v2_product_sources SET state=? WHERE scheduler_run_id=? AND source_id=?', (state, self.run, row['source_id']))

    def advance_pass(self):
        """Called ONLY by the existing pump at producer/worker quiescence."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()
            if life['stop_requested']:
                return False
            failures = c.execute("SELECT 1 FROM v2_product_sources WHERE scheduler_run_id=? AND state='RETRYABLE' UNION ALL SELECT 1 FROM v2_current_jobs WHERE scheduler_run_id=? AND status='FAILED_RETRYABLE' LIMIT 1", (self.run, self.run)).fetchone()
            if not failures or life['pass_index'] >= 2:
                return False
            c.execute('UPDATE v2_product_lifecycle SET pass_index=pass_index+1,updated_at=? WHERE scheduler_run_id=?', (time.time(), self.run))
            c.execute("UPDATE v2_product_sources SET state='NEW' WHERE scheduler_run_id=? AND state='RETRYABLE'", (self.run,))
            return True

    def summary(self):
        self.refresh()
        with self.store.connect() as c:
            life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()
            rows = [dict(r) for r in c.execute('SELECT * FROM v2_product_sources WHERE scheduler_run_id=?', (self.run,))]
            jobs = [dict(r) for r in c.execute("SELECT * FROM v2_current_jobs WHERE scheduler_run_id=? AND execution_epoch=?", (self.run,self.epoch))]
            admissions = 0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_admissions'").fetchone():
                admissions = c.execute("SELECT COUNT(*) FROM scheduler_admissions WHERE scheduler_run_id=? AND execution_epoch=? AND status='ACTIVE'", (self.run,self.epoch)).fetchone()[0]
            final_outcomes = {r[0] for r in c.execute("SELECT s.source_id FROM v2_product_sources s JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.source_revision WHERE s.scheduler_run_id=? AND o.status IN ('FAILED_FINAL','ABORTED','FAILED')", (self.run,))}
        failed_sources = {r['source_id'] for r in jobs if r['status']=='FAILED_FINAL'}
        failed_sources.update(final_outcomes)
        failed_sources.update(r['source_id'] for r in rows if r['state']=='FAILED_FINAL')
        retryable = {r['source_id'] for r in jobs if r['status']=='FAILED_RETRYABLE'}
        retryable.update(r['source_id'] for r in rows if r['state']=='RETRYABLE')
        terminal = all(r['state']=='COMPLETE' or r['source_id'] in failed_sources for r in rows)
        unfinished = any(r['status'] in ('PENDING','RUNNING','FAILED_RETRYABLE') for r in jobs)
        return {'current_pass': PASSES[life['pass_index']], 'retryable_count': len(retryable),
                'failed_final_count':len(failed_sources), 'failed_source_list': sorted(failed_sources),
                'error_summary': {r['source_id']:r['error'] for r in rows+jobs if r['error']},
                'complete': bool(rows) and terminal and not unfinished and not admissions,
                'active_admissions':admissions,
                'source_count':len(rows), 'successful_count':sum(r['state']=='COMPLETE' for r in rows)}

def request_handoff(database, run_id, epoch):
    """Supported Stop command: durable intent, never a signal to a PID guess."""
    from .central_database import connect
    from .scheduler_identity import SchedulerOwnershipError
    with connect(database) as c:
        c.execute('BEGIN IMMEDIATE')
        run = c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?', (run_id,)).fetchone()
        if not run or run['execution_epoch'] != epoch:
            raise SchedulerOwnershipError('stale_handoff_epoch')
        if run['status'] in ('COMPLETED','FAILED','CANCELLED','ABORTED','RECOVERABLE'):
            return {'status':'PASS','message':'任务已停止或结束','scheduler_run_status':run['status']}
        c.execute('INSERT INTO v2_product_lifecycle(scheduler_run_id,execution_epoch,stop_requested,updated_at) VALUES(?,?,1,?) ON CONFLICT(scheduler_run_id) DO UPDATE SET stop_requested=1,updated_at=excluded.updated_at', (run_id,epoch,time.time()))
    return {'status':'PASS','message':'已请求安全停止；正在保存可恢复状态','scheduler_run_status':'STOP_REQUESTED'}


def reconcile_orphaned_execution(database, run_id, epoch):
    """Recover a V2 run whose owned product worker is provably gone.

    This is deliberately a narrow ownership recovery boundary.  It never
    guesses a PID or touches another run; a live owned worker remains active.
    """
    from .central_database import connect
    from .scheduler_identity import mark_scheduler_run_recoverable, SchedulerOwnershipError
    with connect(database) as c:
        tables = {item[0] for item in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # The recovery hook is also called by lightweight lifecycle fixtures.
        # Without the durable admission/lifecycle tables there is no owned
        # product worker for this API to recover; leave those fixtures alone.
        if not {'scheduler_admissions', 'v2_product_lifecycle'} <= tables:
            return {'status': 'PASS', 'scheduler_run_status': 'RUNNING', 'recovered': False}
        # Keep owner/status verification and orphan reclamation in one snapshot.
        c.execute('BEGIN IMMEDIATE')
        row = c.execute(
            'SELECT scheduler_mode,status,execution_epoch FROM scheduler_runs WHERE scheduler_run_id=?',
            (run_id,),
        ).fetchone()
        if not row or row['scheduler_mode'] != 'event_driven_v2' or int(row['execution_epoch']) != int(epoch):
            raise SchedulerOwnershipError('stale_orphan_recovery_epoch')
        if row['status'] in ('COMPLETED', 'FAILED', 'CANCELLED', 'ABORTED'):
            return {'status': 'PASS', 'scheduler_run_status': row['status'], 'recovered': False}
        life = c.execute(
            'SELECT worker_pid,stop_requested FROM v2_product_lifecycle WHERE scheduler_run_id=?',
            (run_id,),
        ).fetchone()
        pid = int(life['worker_pid'] or 0) if life else 0
        alive = False
        if pid > 1:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
            if alive:
                return {'status': 'PASS', 'scheduler_run_status': row['status'], 'recovered': False, 'worker_pid': pid}
            job_columns = {item['name'] for item in c.execute('PRAGMA table_info(v2_jobs)')}
            # Lightweight lifecycle fixtures predate durable run/epoch columns.
            # They cannot contain an owned orphan from this API, so recovery is
            # a safe no-op rather than a schema error.
            if {'scheduler_run_id', 'execution_epoch'} <= job_columns:
                c.execute(
                    """UPDATE v2_jobs SET status='PENDING', attempts=MAX(0, attempts-1),
                            owner=NULL, token=NULL, lease_until=NULL
                        WHERE scheduler_run_id=? AND execution_epoch=? AND status='RUNNING'""",
                    (run_id, epoch),
                )
        c.execute(
            """UPDATE scheduler_admissions SET status='RELEASED', released_at=?,
                    release_reason='orphaned_product_worker'
                WHERE scheduler_run_id=? AND execution_epoch=? AND status='ACTIVE'""",
            (time.time(), run_id, epoch),
        )
        c.execute(
            'UPDATE v2_product_lifecycle SET worker_pid=NULL,stop_requested=0,updated_at=? WHERE scheduler_run_id=?',
            (time.time(), run_id),
        )
    recovered = mark_scheduler_run_recoverable(
        database, run_id, scheduler_mode='event_driven_v2', execution_epoch=int(epoch)
    )
    return {'status': 'PASS', 'scheduler_run_status': recovered['status'], 'recovered': True, 'worker_pid': pid}


def read_lifecycle_summary(database, run_id):
    """Read-only bridge projection; no lock acquisition or recovery side effects."""
    from .central_database import connect
    with connect(database, readonly=True) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
            return None
        life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (run_id,)).fetchone()
        if not life:
            return None
        sources = list(c.execute('SELECT source_id,state,error FROM v2_product_sources WHERE scheduler_run_id=?', (run_id,)))
        jobs = list(c.execute("SELECT source_id,status,error FROM v2_current_jobs WHERE scheduler_run_id=? AND status IN ('FAILED_RETRYABLE','FAILED_FINAL')", (run_id,)))
    retryable = {r['source_id'] for r in sources if r['state']=='RETRYABLE'}
    retryable.update(r['source_id'] for r in jobs if r['status']=='FAILED_RETRYABLE')
    final = {r['source_id'] for r in sources if r['state']=='FAILED_FINAL'}
    final.update(r['source_id'] for r in jobs if r['status']=='FAILED_FINAL')
    return {'current_pass':PASSES[life['pass_index']], 'retryable_count':len(retryable),
            'failed_final_count':len(final), 'failed_source_list':sorted(final),
            'error_summary':{r['source_id']:r['error'] for r in sources+jobs if r['error']}}
