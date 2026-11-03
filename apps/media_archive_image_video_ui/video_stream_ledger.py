"""Experiment-only durable ledger and independent bounded hot consumers.

One SQLite connection per operation, inference outside transactions. Queue
objects carry stable IDs only. No people/index/source-advance on dispatch.
Schema is namespaced/versioned; this is not a production DB migration entry.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid
import traceback


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class StaleResult(Exception):
    pass



MAX_RETRIES = 3
MAX_TOTAL_ATTEMPTS = 4
CANDIDATE_FAILURE_CLASSES = frozenset((
    'RETRYABLE_MODEL_FAILURE','RETRYABLE_TRANSIENT_RESOURCE_FAILURE',
    'RETRYABLE_OUTPUT_CONTRACT_FAILURE','NON_RETRYABLE_INPUT_INVALID',
    'NON_RETRYABLE_REVISION_INVALID','NON_RETRYABLE_CANCELLED'))

from dataclasses import dataclass,asdict

@dataclass(frozen=True)
class CandidateFailureEvidence:
    source_id: str
    source_revision: str
    candidate_id: str
    visual_unit_id: str | None
    time_position_ms: int | None
    candidate_role: str | None
    candidate_score: float | None
    attempt_count: int
    failure_reason: str
    last_error: str
    first_failure_at: float
    final_failure_at: float
    yoloe_ref: object = None
    openclip_ref: object = None
    people_ref: object = None
    ocr_ref: object = None
    status: str = 'FAILED_FINAL'


def classify_candidate_failure(error,operation='model_inference'):
    declared=getattr(error,'failure_reason',None)
    if declared in CANDIDATE_FAILURE_CLASSES:return declared
    name=type(error).__name__.lower();message=str(error).lower()
    if 'cancelled' in name or 'canceled' in name:return 'NON_RETRYABLE_CANCELLED'
    if isinstance(error,StaleResult):
        return 'NON_RETRYABLE_INPUT_INVALID' if operation=='input_validation' and message in ('input_digest_changed','text_input_digest_changed') else 'NON_RETRYABLE_REVISION_INVALID'
    if operation=='input_validation':return 'NON_RETRYABLE_INPUT_INVALID'
    if operation=='result_validation' or any(s in message for s in ('qwen_schema','qwen_sections','qwen_generation_not_valid_complete','raw_media_schema')):
        return 'RETRYABLE_OUTPUT_CONTRACT_FAILURE'
    if isinstance(error,(MemoryError,TimeoutError,BlockingIOError)) or any(s in message for s in ('out of memory','resource temporarily unavailable','metal allocation')):
        return 'RETRYABLE_TRANSIENT_RESOURCE_FAILURE'
    return 'RETRYABLE_MODEL_FAILURE'


def candidate_input_failure(error,job):
    if getattr(error,'failure_reason',None)=='NON_RETRYABLE_INPUT_INVALID':return True
    if str(error) in ('input_digest_changed','text_input_digest_changed','IMAGE_QWEN_REQUIRES_EXACTLY_ONE_IMAGE'):return True
    return isinstance(error,FileNotFoundError) and getattr(error,'filename',None)==job['payload'].get('image_path')


class TrackedConnection(sqlite3.Connection):
    writer_acquired_at=None
    writer_wait_ms=None

    def _acquire_execute(self,sql,parameters=()):
        begin=sql.strip().upper().startswith('BEGIN IMMEDIATE')
        started=time.monotonic()
        retries=0
        deadline=started+12
        while True:
            try:
                result=super().execute(sql,parameters)
                break
            except sqlite3.OperationalError as error:
                # Retry only acquisition: no transaction or business SQL has run.
                # Never replay commit, DML, rollback, or a nested transaction.
                busy=getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_BUSY
                if not begin or self.in_transaction or not busy or time.monotonic()>=deadline:
                    raise
                retries+=1
                path=getattr(self,'acquisition_log',None)
                if path is None:
                    database=super().execute('PRAGMA database_list').fetchone()[2]
                    if database:path=Path(database).with_name('transaction-acquisition-retries.jsonl')
                if path:
                    with Path(path).open('a') as f:
                        f.write(encoded({'at':time.time(),'event':'BEGIN_BUSY_RETRY','thread':threading.current_thread().name,'retry':retries,'elapsed_ms':(time.monotonic()-started)*1000})+'\n')
                time.sleep(min(.05*2**min(retries-1,3),max(0,deadline-time.monotonic())))
        if begin:
            self.writer_wait_ms=(time.monotonic()-started)*1000
            self.writer_acquired_at=time.monotonic()
        return result


    def _write_acquisition(self):
        # Only lock acquisition is retried. The caller's first business SQL has
        # not run. Existing read snapshots must never be replayed or upgraded.
        if not self.in_transaction:
            self._acquire_execute('BEGIN IMMEDIATE')
            self._owns_writer = True
        elif not getattr(self, '_owns_writer', False):
            raise sqlite3.OperationalError('shared_main_write_in_read_snapshot_forbidden')

    @staticmethod
    def _kind(sql):
        text=sql.lstrip().upper()
        while text.startswith('--') or text.startswith('/*'):
            if text.startswith('--'):
                text=text.partition('\n')[2].lstrip()
            else:
                text=text.partition('*/')[2].lstrip()
        return text.split(None,1)[0].rstrip(';') if text else '', text

    def execute(self,sql,parameters=()):
        kind,text=self._kind(sql)
        if text.startswith('BEGIN IMMEDIATE'):
            result=self._acquire_execute(sql,parameters)
            self._owns_writer=True
            return result
        if kind in ('INSERT','UPDATE','DELETE','REPLACE'):
            self._write_acquisition()
            return super().execute(sql,parameters)
        # DDL originally auto-commits only when no transaction is open. Keep
        # that boundary, acquiring BEFORE the schema statement, never replaying
        # the DDL, COMMIT or rollback. TEMP schema belongs only to this connection.
        ddl=kind in ('CREATE','ALTER','DROP','REINDEX')
        private=text.startswith(('CREATE TEMP ', 'CREATE TEMPORARY ', 'DROP TABLE TEMP.', 'DROP VIEW TEMP.'))
        if ddl and not private and not self.in_transaction:
            self._write_acquisition()
            try:
                result=super().execute(sql,parameters)
                self.commit()
                return result
            except BaseException:
                self.rollback()
                raise
        result=super().execute(sql,parameters)
        if kind=='BEGIN':self._owns_writer=False
        return result

    def executemany(self,sql,parameters):
        kind,_=self._kind(sql)
        if kind in ('INSERT','UPDATE','DELETE','REPLACE'):
            self._write_acquisition()
        return super().executemany(sql,parameters)

    def executescript(self,sql):
        # Native legacy executescript commits pending work FIRST. Do that once,
        # outside acquisition retry. Then preserve per-statement autocommit and
        # explicit script transaction boundaries. Never invoke native
        # executescript after acquiring: it would silently release the lock.
        if self.in_transaction:
            self.commit()
        statement=''
        statements=[]
        for char in sql:
            statement+=char
            if char==';' and sqlite3.complete_statement(statement):
                statements.append(statement);statement=''
        if statement.strip():statements.append(statement)
        for statement in statements:
            kind,text=self._kind(statement)
            was_open=self.in_transaction
            try:
                if kind=='BEGIN' and text.rstrip(';').strip() in ('BEGIN','BEGIN TRANSACTION','BEGIN DEFERRED','BEGIN DEFERRED TRANSACTION'):
                    self.execute('BEGIN IMMEDIATE')
                else:
                    self.execute(statement)
                if not was_open and kind not in ('BEGIN','SAVEPOINT') and self.in_transaction:
                    self.commit()
            except BaseException:
                if not was_open and self.in_transaction:
                    self.rollback()
                raise
        return self.cursor()

    def commit(self):
        result=super().commit()
        self._owns_writer=False
        return result

    def rollback(self):
        result=super().rollback()
        self._owns_writer=False
        return result


class Ledger:
    def __init__(self, path, owner, epoch):
        self.path, self.owner, self.epoch = str(path), owner, epoch
        self.source_priorities={}
        from .video_delivery_queries import DeliveryPulse
        self.delivery_pulse=DeliveryPulse()
        self.delivery_notification_errors=0
        from .video_source_admission import SourceAdmission
        self.admission=SourceAdmission(Path(path).with_name('source-switches.jsonl'))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS video_stream_control_v1(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT, epoch INTEGER, stopped INTEGER);
              CREATE TABLE IF NOT EXISTS video_stream_sources_v1(
                source_id TEXT PRIMARY KEY, revision TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS video_stream_priority_v1(
                source_id TEXT PRIMARY KEY,revision TEXT,ready_at REAL,score REAL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS video_stream_jobs_v1(
                job_id TEXT PRIMARY KEY, source_id TEXT, revision TEXT, stage TEXT,
                input_sha TEXT, contract TEXT, payload TEXT, status TEXT,
                token TEXT, lease_until REAL, attempts INTEGER DEFAULT 0,
                result_json TEXT, result_sha TEXT, service_ms REAL);
              CREATE INDEX IF NOT EXISTS video_stream_pending_v1 ON video_stream_jobs_v1(stage,status);
              CREATE INDEX IF NOT EXISTS video_stream_policy_key_v1 ON video_stream_jobs_v1(json_extract(payload,'$._evidence_key'));
              CREATE INDEX IF NOT EXISTS video_stream_source_wave_v1 ON video_stream_jobs_v1(source_id,revision,stage,attempts,status);
              CREATE TABLE IF NOT EXISTS video_stream_events_v1(
                event_id INTEGER PRIMARY KEY, job_id TEXT, kind TEXT, payload TEXT);
              CREATE TABLE IF NOT EXISTS video_stream_branches_v1(
                source_id TEXT, revision TEXT, stage TEXT, expected INTEGER,
                PRIMARY KEY(source_id,revision,stage));
              CREATE TABLE IF NOT EXISTS video_stream_attempts_v1(
                job_id TEXT, candidate_id TEXT, source_id TEXT, attempt_number INTEGER,
                started_at REAL, finished_at REAL, status TEXT, error_type TEXT,
                error_message TEXT, input_sha TEXT, contract TEXT, result_sha TEXT,
                PRIMARY KEY(job_id,attempt_number));
              CREATE INDEX IF NOT EXISTS video_stream_attempt_finished_v1 ON video_stream_attempts_v1(finished_at,status,job_id);
              CREATE TABLE IF NOT EXISTS video_model_delivery_v1(
                event_id TEXT PRIMARY KEY,source_id TEXT,revision TEXT,candidate_id TEXT,snapshot_id TEXT,
                entitled_at REAL NOT NULL,job_id TEXT UNIQUE,sealed_at REAL,manifest TEXT,manifest_sha TEXT);
              CREATE INDEX IF NOT EXISTS video_model_delivery_pending_v1 ON video_model_delivery_v1(sealed_at,entitled_at);
              CREATE TABLE IF NOT EXISTS video_candidate_retry_v1(
                job_id TEXT PRIMARY KEY,not_before REAL NOT NULL,category TEXT NOT NULL,
                first_failure_at REAL NOT NULL,last_failure_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS video_candidate_failures_v1(
                job_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,source_id TEXT NOT NULL,
                source_revision TEXT NOT NULL,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS video_stream_retry_limits_v1(
                job_id TEXT PRIMARY KEY, max_attempts INTEGER NOT NULL);
            ''')
            from .video_stream_operator_once import ensure_schema
            ensure_schema(c)
            c.execute('INSERT OR IGNORE INTO video_stream_control_v1 VALUES(1,?,?,0)', (owner, epoch))
            self.fence(c)
            # Upgrade only the old default budget on unfinished candidates.
            # Historical DONE/final failures and explicit other caps are immutable.
            c.execute("UPDATE video_stream_retry_limits_v1 SET max_attempts=4 WHERE max_attempts=3 AND NOT EXISTS(SELECT 1 FROM video_stream_operator_once_v1 o WHERE o.job_id=video_stream_retry_limits_v1.job_id) AND job_id IN (SELECT job_id FROM video_stream_jobs_v1 WHERE stage IN ('qwen','ocr') AND (status IN ('PENDING','RETRY_PENDING') OR (status='RUNNING' AND lease_until<=(julianday('now')-2440587.5)*86400.0)))")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_jobs'").fetchone():
                c.execute("UPDATE v2_jobs SET max_attempts=4 WHERE stage IN ('qwen','ocr') AND NOT EXISTS(SELECT 1 FROM video_stream_operator_once_v1 o JOIN video_stream_jobs_v1 n ON n.job_id=o.job_id WHERE json_extract(n.payload,'$._evidence_key')=v2_jobs.execution_key) AND max_attempts=3 AND status IN ('PENDING','FAILED_RETRYABLE') AND execution_key IN (SELECT json_extract(n.payload,'$._evidence_key') FROM video_stream_jobs_v1 n JOIN video_stream_retry_limits_v1 r ON r.job_id=n.job_id WHERE r.max_attempts=4 AND (n.status IN ('PENDING','RETRY_PENDING') OR (n.status='RUNNING' AND n.lease_until<=(julianday('now')-2440587.5)*86400.0)))")

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=2,factory=TrackedConnection)
        from .video_result_reader import row_factory
        c.row_factory = row_factory
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_jobs'").fetchone():
            from .v2_current_jobs_cache import correlated_predicate
            c.execute("CREATE TEMP VIEW v2_current_jobs AS SELECT j.* FROM main.v2_jobs j WHERE j.status<>'SUPERSEDED' AND "+correlated_predicate(c))
        opened=time.monotonic();writer=[None];last_sql=[None];delivery_changed=[False]
        from .video_delivery_queries import affects_delivery
        def trace(sql):
            category=sql.lstrip().split(None,1)[0].upper() if sql.strip() else ''
            last_sql[0]=category
            if category in ('INSERT','UPDATE','DELETE','REPLACE') and affects_delivery(sql):delivery_changed[0]=True
            if writer[0] is None and (sql.upper().startswith('BEGIN IMMEDIATE') or category in ('INSERT','UPDATE','DELETE','REPLACE')):
                writer[0]=time.monotonic()
        c.set_trace_callback(trace)
        try:
            yield c
            c.commit()
            if delivery_changed[0] and c.total_changes:
                try:self.delivery_pulse.committed()
                except Exception as notification_error:
                    # A wakeup is advisory. Never turn a successful COMMIT into
                    # a reported rollback; bounded delivery polling recovers it.
                    self.delivery_notification_errors+=1
                    self.delivery_notification_error=type(notification_error).__name__
            receipt=getattr(c,'closure_claim_receipt',None)
            if receipt:
                try:receipt[0].committed_claim(receipt[1],time.time(),time.monotonic())
                except Exception as error:receipt[0].event('CLAIM_RECEIPT_ERROR',error=repr(error))
        except BaseException as error:
            failed_category=last_sql[0]
            c.rollback()
            if isinstance(error,sqlite3.Error):
                record={'timestamp':time.time(),'thread':threading.current_thread().name,
                    'operation':failed_category,'transaction_started_at':time.time()-(time.monotonic()-opened),
                    'transaction_elapsed_ms':(time.monotonic()-opened)*1000,
                    'writer_hold_ms':None if c.writer_acquired_at is None else (time.monotonic()-c.writer_acquired_at)*1000,
                    'writer_acquire_wait_ms':c.writer_wait_ms,
                    'owner':self.owner,'epoch':self.epoch,'exception_type':type(error).__name__,
                    'exception_message':str(error),'sqlite_errorcode':getattr(error,'sqlite_errorcode',None),
                    'sqlite_errorname':getattr(error,'sqlite_errorname',None),'traceback':traceback.format_exc()}
                with Path(self.path).with_name('database-failures.jsonl').open('a') as f:f.write(encoded(record)+'\n')
            raise
        finally:
            if c.writer_acquired_at is not None and time.monotonic()-c.writer_acquired_at>.1:
                with Path(self.path).with_name('writer-transactions.jsonl').open('a') as f:
                    f.write(encoded({'timestamp':time.time(),'thread':threading.current_thread().name,
                        'duration_ms':(time.monotonic()-c.writer_acquired_at)*1000,'writer_acquire_wait_ms':c.writer_wait_ms,'last_sql_category':last_sql[0],
                        'caller':traceback.format_stack(limit=7)})+'\n')
            c.close()

    @contextmanager
    def write_connection(self):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            yield c

    def fence(self, c, *, allow_stop=False):
        state = c.execute('SELECT * FROM video_stream_control_v1 WHERE singleton=1').fetchone()
        if state['owner'] != self.owner or state['epoch'] != self.epoch or (state['stopped'] and not allow_stop):
            raise StaleResult('owner_epoch_or_stop_fence')

    def source(self, source_id, revision):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c)
            c.execute('INSERT INTO video_stream_sources_v1 VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision', (source_id, revision))
            c.execute('INSERT INTO video_stream_priority_v1 VALUES(?,?,?,0) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,ready_at=CASE WHEN revision=excluded.revision THEN ready_at ELSE excluded.ready_at END',(source_id,revision,time.time()))
            c.execute("UPDATE video_stream_jobs_v1 SET status='SUPERSEDED' WHERE source_id=? AND revision<>? AND status IN ('PENDING','RUNNING')", (source_id, revision))

    def publish(self, source_id, revision, stage, input_sha, contract, payload, max_attempts=None):
        if max_attempts is None:
            max_attempts=MAX_TOTAL_ATTEMPTS if stage in ('qwen','ocr') else 3
        if max_attempts < 1:
            raise ValueError('bounded_attempts_required')
        identity_payload={k:v for k,v in payload.items() if not k.startswith('_')} if stage=='audio_extract' else payload
        key = hashlib.sha256(encoded([source_id, revision, stage, input_sha, contract, identity_payload]).encode()).hexdigest()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c)
            if c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?', (source_id,)).fetchone()[0] != revision:
                raise StaleResult('source_revision_changed')
            # Item manifests, not a source-wide transport counter, grant inference.
            created=c.execute('INSERT OR IGNORE INTO video_stream_jobs_v1(job_id,source_id,revision,stage,input_sha,contract,payload,status) VALUES(?,?,?,?,?,?,?,?)',
                (key, source_id, revision, stage, input_sha, encoded(contract), encoded(payload), 'PENDING')).rowcount==1
            c.execute('INSERT OR IGNORE INTO video_stream_retry_limits_v1 VALUES(?,?)', (key,max_attempts))
            if c.execute('SELECT max_attempts FROM video_stream_retry_limits_v1 WHERE job_id=?',(key,)).fetchone()[0] != max_attempts:
                raise ValueError('retry_contract_changed_for_existing_identity')
        from .build151_whisper_feed import call
        if created:call(self,'registered_job',stage,key,source_id,revision,payload)
        return key

    def seal_branch(self, source_id, revision, stage, expected):
        if stage not in ('qwen','ocr') or expected < 0:
            raise ValueError('invalid_candidate_branch_manifest')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c)
            count = c.execute('SELECT count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=?', (source_id,revision,stage)).fetchone()[0]
            if count != expected:
                raise ValueError('candidate_manifest_incomplete')
            old = c.execute('SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage=?', (source_id,revision,stage)).fetchone()
            if old and old[0] != expected:
                raise ValueError('candidate_manifest_changed')
            c.execute('INSERT OR IGNORE INTO video_stream_branches_v1 VALUES(?,?,?,?)', (source_id,revision,stage,expected))

    @staticmethod
    def wave_ready(c, job):
        if job['stage'] not in ('qwen','ocr'):
            return job['status'] in ('PENDING','RETRY_PENDING')
        local=c.execute('SELECT 1 FROM video_model_delivery_v1 WHERE job_id=? AND sealed_at IS NOT NULL',(job['job_id'],)).fetchone()
        if not local:return False
        if job['status'] == 'PENDING':
            return True
        if job['status'] != 'RETRY_PENDING':
            return False
        retry=c.execute('SELECT not_before FROM video_candidate_retry_v1 WHERE job_id=?',(job['job_id'],)).fetchone()
        return retry is None or retry[0]<=time.time()

    def pending(self, stage, limit, *, admit=False):
        with self.connection() as c:
            self.fence(c)
            # Eligibility is indexed/scoped to the source branch. Completion
            # boost for a finished first pass, without blocking another lane.
            rows = c.execute("""SELECT j.job_id,j.source_id,j.revision,j.status,j.rowid sequence,coalesce(priority.score,0) legacy_score FROM video_stream_jobs_v1 j
                JOIN video_stream_sources_v1 s ON s.source_id=j.source_id AND s.revision=j.revision
                LEFT JOIN video_stream_priority_v1 priority ON priority.source_id=j.source_id AND priority.revision=j.revision
                WHERE j.stage=? AND (j.status='PENDING' OR (j.status='RETRY_PENDING' AND (
                  (j.stage IN ('qwen','ocr') AND NOT EXISTS(SELECT 1 FROM video_candidate_retry_v1 cr WHERE cr.job_id=j.job_id AND cr.not_before>?))
                  OR (j.stage NOT IN ('qwen','ocr') AND NOT EXISTS(SELECT 1 FROM video_stream_jobs_v1 f WHERE f.source_id=j.source_id AND f.revision=j.revision AND f.stage=j.stage AND (f.attempts=0 OR (f.status='RUNNING' AND f.attempts=1)))))))
                AND (j.stage NOT IN ('qwen','ocr') OR EXISTS(SELECT 1 FROM video_model_delivery_v1 m WHERE m.job_id=j.job_id AND m.sealed_at IS NOT NULL))
                """, (stage,time.time())).fetchall()
            # These native jobs already have durable stage-local input and the
            # required candidate manifest. Do not re-enter source/media/folder
            # admission after readiness. Claim still revalidates every fence.
            with self.admission.lock:
                now=self.admission.clock()
                rows=[dict(row) for row in rows
                      if self.admission.blocked.get((stage,row['source_id']),0)<=now]
                rows.sort(key=lambda row:row['sequence'])
                self.admission.backlogs[stage]=len(rows)
            return [r['job_id'] for r in rows[:limit]]

    def claim(self, key, lease_seconds=600):
        cutoff=getattr(self,'new_work_stop',None)
        if cutoff is None:return self._claim_owned(key,lease_seconds)
        with cutoff.new_work('model_claim') as allowed:
            if not allowed:return None
            job=self._claim_owned(key,lease_seconds)
            if job:cutoff.work_started('model_claim',stage=job['stage'],source_id=job['source_id'],job_id=job['job_id'])
            return job

    def _claim_owned(self, key, lease_seconds=600):
        try:
            result=self._claim_once(key,lease_seconds)
        except Exception as error:
            # A commit exception can be ambiguous. Never log a committed claim
            # from an UPDATE rowcount before the transaction has returned.
            try:
                from .build151_runtime_trace import native
                with self.connection() as c:
                    current=c.execute('SELECT * FROM video_stream_jobs_v1 WHERE job_id=?',(key,)).fetchone()
                    native(self,c,current,claim_attempted=True,claim_result='UNKNOWN',
                           claim_failure_reason=type(error).__name__+':'+str(error)[:240])
            except Exception:pass
            raise
        if result:
            if result['stage']=='qwen':
                try:
                    from .build151_runtime_trace import native
                    with self.connection() as c:
                        native(self,c,result,resource_reason=None,claim_attempted=True,
                               claim_result=True,wave_eligible_override=True)
                except Exception:pass
            # Post-Ready ownership belongs to the atomic job lease only.
        return result

    def _claim_once(self, key, lease_seconds=600):
        token = uuid.uuid4().hex
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c)
            current = c.execute('SELECT * FROM video_stream_jobs_v1 WHERE job_id=?', (key,)).fetchone()
            if not current or not self.wave_ready(c,current):
                if current and current['stage']=='qwen':
                    from .build151_runtime_trace import native
                    native(self,c,current,claim_attempted=True,claim_result=False,claim_failure_reason='WAVE_NOT_ELIGIBLE')
                return None
            maximum=c.execute('SELECT max_attempts FROM video_stream_retry_limits_v1 WHERE job_id=?',(key,)).fetchone()[0]
            from .video_stream_operator_once import permission, consume
            operator_permission=permission(c,current)
            if current['attempts']>=maximum and operator_permission is None:
                if current['stage']=='qwen':
                    from .build151_runtime_trace import native
                    native(self,c,current,claim_attempted=True,claim_result=False,claim_failure_reason='MAX_ATTEMPTS')
                return None
            changed = c.execute("UPDATE video_stream_jobs_v1 SET status='RUNNING',token=?,lease_until=?,attempts=attempts+1 WHERE job_id=? AND status IN ('PENDING','RETRY_PENDING') AND EXISTS(SELECT 1 FROM video_stream_sources_v1 s WHERE s.source_id=video_stream_jobs_v1.source_id AND s.revision=video_stream_jobs_v1.revision)", (token, time.time()+lease_seconds, key)).rowcount
            if current['stage']=='qwen' and not changed:
                from .build151_runtime_trace import native
                native(self,c,current,resource_reason=None,claim_attempted=True,claim_result=False,claim_failure_reason='ATOMIC_CLAIM_NO_MATCH')
            if not changed:
                return None
            result = dict(c.execute('SELECT * FROM video_stream_jobs_v1 WHERE job_id=?', (key,)).fetchone())
            result['payload'] = json.loads(result['payload'])
            if operator_permission is not None:
                result['payload']['_operator_retry']=consume(c,operator_permission,token=token,owner=self.owner,epoch=self.epoch)
            if result['stage']=='qwen':
                result['payload']['_wave_attempt']=result['attempts']
                if result['attempts']>1:
                    previous=c.execute('SELECT error_type,error_message FROM video_stream_attempts_v1 WHERE job_id=? AND attempt_number=?', (key,result['attempts']-1)).fetchone()
                    if not previous:
                        raise ValueError('previous_attempt_audit_missing')
                    marker=previous['error_message'].find('{"candidate_id":')
                    outcome=None
                    if marker>=0:
                        try:
                            audit=json.loads(previous['error_message'][marker:])
                            if str(audit['candidate_id'])!=str(result['payload']['candidate_id']):
                                raise ValueError('previous_outcome_candidate_mismatch')
                            outcome=audit['outcome']
                        except (KeyError,json.JSONDecodeError):
                            raise ValueError('previous_qwen_audit_invalid')
                    result['payload']['_previous_outcome']=outcome or {
                        'result_status':'runtime_failed','truncation_status':'UNKNOWN',
                        'cleanup_warnings':'','error_type':previous['error_type'],
                        'error_message':previous['error_message']}
            c.execute('INSERT INTO video_stream_attempts_v1(job_id,candidate_id,source_id,attempt_number,started_at,status,input_sha,contract) VALUES(?,?,?,?,?,?,?,?)',
                (key,result['payload'].get('candidate_id',key),result['source_id'],result['attempts'],time.time(),'RUNNING',result['input_sha'],result['contract']))
            observer=getattr(self,'claim_truth',None)
            if observer and result['stage'] in ('qwen','whisper','audio_extract','vad'):
                c.closure_claim_receipt=(observer,{k:result[k] for k in ('job_id','stage','source_id','revision')})
            return result

    def commit(self, job, result, service_ms):
        # Freeze caller-owned model output before acquiring the writer. The
        # JSON round trip also prevents a mutable result being changed while
        # its digest and the durable evidence mirror are being prepared.
        projection=getattr(self,'result_projection',None)
        if projection is not None:
            with self.connection() as reader:
                reader.execute('PRAGMA query_only=ON')
                result=projection(reader,job,result)
        payload=encoded(result)
        result=json.loads(payload)
        digest=hashlib.sha256(payload.encode()).hexdigest()
        prepare=getattr(self,'prepare_completion',None)
        prepared=prepare(job,result) if prepare is not None else None
        sql_prepare=getattr(self,'prepare_sql_completion',None)
        if sql_prepare is not None:
            from .video_commit_queue import StaleProposal
            proposal=sql_prepare(job,payload,digest,service_ms,prepared)
            try:
                self.commit_queue.submit(proposal).result()
            except StaleProposal as error:
                raise StaleResult(str(error)) from error
            return
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            # Stop finishes owned inference then retires; epoch changes fence it.
            self.fence(c, allow_stop=True)
            changed = c.execute("UPDATE video_stream_jobs_v1 SET status='DONE',result_json=?,result_sha=?,service_ms=?,token=NULL,lease_until=NULL WHERE job_id=? AND status='RUNNING' AND token=? AND lease_until>? AND EXISTS(SELECT 1 FROM video_stream_sources_v1 s WHERE s.source_id=video_stream_jobs_v1.source_id AND s.revision=video_stream_jobs_v1.revision)", (payload, digest, service_ms, job['job_id'], job['token'], time.time())).rowcount
            if not changed:
                raise StaleResult('late_or_superseded_result')
            hook=getattr(self,'completion_hook',None)
            if prepared is not None:
                self.apply_prepared_completion(c,job,prepared)
            elif hook is not None:hook(c,job,result)
            if getattr(self,'reference_results',False):
                # Exactly one canonical model value lives in v2_jobs. The hot
                # dispatch ledger retains immutable SHA/lease/provenance and
                # reference bindings, not a second full float-vector payload.
                if not c.execute('SELECT 1 FROM video_stream_mirrors_v1 WHERE job_id=?',(job['job_id'],)).fetchone():raise ValueError('canonical_result_reference_missing')
                c.execute('UPDATE video_stream_jobs_v1 SET result_json=NULL WHERE job_id=?',(job['job_id'],))
            c.execute("UPDATE video_stream_attempts_v1 SET finished_at=?,status='SUCCESS',result_sha=? WHERE job_id=? AND attempt_number=?", (time.time(),digest,job['job_id'],job['attempts']))
            c.execute('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)', (job['job_id'], 'RESULT_COMMITTED', encoded({'result_sha':digest})))

    def failed(self, job, error, *, operation='model_inference'):
        message=str(error);stored_message=message;event_message=message;prepared=None
        if getattr(self,'reference_results',False):
            from .video_canonical_store import prepare_result
            from .video_result_reader import reference
            prepared=prepare_result(job['stage']+':failed-output','audit-v1',message,source=job['source_id'])
            stored_message=encoded(reference(prepared.result_id));event_message=reference(prepared.result_id)
        event_payload=encoded({'error':event_message})
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c, allow_stop=True)
            maximum = c.execute('SELECT max_attempts FROM video_stream_retry_limits_v1 WHERE job_id=?', (job['job_id'],)).fetchone()[0]
            category=classify_candidate_failure(error,operation) if job['stage'] in ('qwen','ocr') else None
            retryable=category is None or category.startswith('RETRYABLE_')
            if job['stage']=='audio_extract' and getattr(error,'failure_reason',None)=='NON_RETRYABLE_INPUT_INVALID':retryable=False
            status = 'RETRY_PENDING' if retryable and job['attempts'] < maximum else 'TERMINAL_FAILED'
            changed = c.execute("UPDATE video_stream_jobs_v1 SET status=?,token=NULL,lease_until=NULL WHERE job_id=? AND status='RUNNING' AND token=? AND EXISTS(SELECT 1 FROM video_stream_sources_v1 s WHERE s.source_id=video_stream_jobs_v1.source_id AND s.revision=video_stream_jobs_v1.revision)", (status,job['job_id'],job['token'])).rowcount
            if not changed:
                raise StaleResult('failed_attempt_no_longer_owned')
            hook=getattr(self,'attempt_failure_hook',None)
            if hook is not None:hook(c,job,status,error)
            if prepared is not None:
                for command in prepared.proposal.commands:c.execute(command.sql,command.parameters)
            c.execute('UPDATE video_stream_attempts_v1 SET finished_at=?,status=?,error_type=?,error_message=? WHERE job_id=? AND attempt_number=?', (time.time(),status,type(error).__name__,stored_message,job['job_id'],job['attempts']))
            c.execute('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)', (job['job_id'], 'ATTEMPT_FAILED', event_payload))
            if category:self._candidate_failure(c,job,status,category,message)


    def _candidate_failure(self,c,job,status,category,message):
        now=time.time();old=c.execute('SELECT first_failure_at FROM video_candidate_retry_v1 WHERE job_id=?',(job['job_id'],)).fetchone()
        first=old[0] if old else now
        delay=min(1.,.1*2**max(0,job['attempts']-1)) if status=='RETRY_PENDING' else 0.
        c.execute('INSERT INTO video_candidate_retry_v1 VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET not_before=excluded.not_before,category=excluded.category,last_failure_at=excluded.last_failure_at',
            (job['job_id'],now+delay,category,first,now))
        if status!='TERMINAL_FAILED':return
        payload=job['payload'];meta={};refs={s:None for s in ('yoloe','openclip','insightface','ocr')}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_selected_candidates'").fetchone():
            row=c.execute('SELECT payload FROM v2_selected_candidates WHERE candidate_id=? AND source_id=? AND source_revision=?',(payload.get('candidate_id'),job['source_id'],job['revision'])).fetchone()
            if row:meta=json.loads(row[0])
        visual=meta.get('visual_unit_id') or payload.get('artifact_id') or payload.get('visual_unit_id')
        position=meta.get('canonical_time_ms',meta.get('time_ms',payload.get('start_ms')))
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_jobs'").fetchone():
            for stage,key,digest in c.execute("SELECT stage,execution_key,result_sha FROM v2_jobs WHERE source_id=? AND source_revision=? AND stage IN ('yoloe','openclip','insightface','ocr') AND status='DONE' AND (item_id=? OR json_extract(payload,'$.artifact_id')=?)",(job['source_id'],job['revision'],visual,visual)):
                refs[stage]=dict(execution_key=key,result_sha=digest)
        evidence=CandidateFailureEvidence(job['source_id'],job['revision'],str(payload.get('candidate_id',job['job_id'])),visual,position,
            meta.get('candidate_role',payload.get('candidate_role')),meta.get('candidate_score',payload.get('candidate_score')),
            job['attempts'],category,message,first,now,refs['yoloe'],refs['openclip'],refs['insightface'],refs['ocr'])
        c.execute('INSERT OR IGNORE INTO video_candidate_failures_v1 VALUES(?,?,?,?,?)',(job['job_id'],evidence.candidate_id,job['source_id'],job['revision'],encoded(asdict(evidence))))
        c.execute('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)',(job['job_id'],'CANDIDATE_FAILED_FINAL',encoded({'failure_reason':category,'attempt_count':job['attempts'],'evidence_ref':job['job_id']})))

    def failure_evidence(self):
        with self.connection() as c:
            return [json.loads(row[0]) for row in c.execute('SELECT payload FROM video_candidate_failures_v1 ORDER BY rowid')]

    def result(self,key):
        """Resolve one current DONE, with canonical SHA/provenance proof."""
        from .v2_producer import fingerprint
        with self.connection() as c:
            self.fence(c,allow_stop=True)
            job=c.execute("SELECT j.* FROM video_stream_jobs_v1 j JOIN video_stream_sources_v1 s ON s.source_id=j.source_id AND s.revision=j.revision WHERE j.job_id=? AND j.status='DONE'",(key,)).fetchone()
            if not job:raise StaleResult('current_success_missing')
            if job['result_json'] is not None:
                value=json.loads(job['result_json'])
                digest=hashlib.sha256(encoded(value).encode()).hexdigest()
                if digest!=job['result_sha'] and not (key.startswith('import:') and fingerprint(value)==job['result_sha']):raise StaleResult('inline_result_digest_mismatch')
                return value
            parts={}
            for row in c.execute('SELECT m.stage,j.* FROM video_stream_mirrors_v1 m JOIN v2_jobs j ON j.execution_key=m.execution_key WHERE m.job_id=?',(key,)):
                value=json.loads(row['result_json'])
                expected=json.loads(job['contract'])
                if job['stage']=='frame_basics':expected=expected['parts'][row['stage']]
                if row['status']!='DONE' or row['source_id']!=job['source_id'] or row['source_revision']!=job['revision'] or json.loads(row['provenance'])!=expected or fingerprint(value)!=row['result_sha']:
                    raise StaleResult('canonical_result_changed')
                parts[row['stage']]=value
            if not parts:raise StaleResult('canonical_result_reference_missing')
            value=parts if job['stage']=='frame_basics' else parts[job['stage']]
            if hashlib.sha256(encoded(value).encode()).hexdigest()!=job['result_sha']:raise StaleResult('canonical_reference_digest_mismatch')
            return value

    def stop_claims(self):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c, allow_stop=True)
            c.execute('UPDATE video_stream_control_v1 SET stopped=1 WHERE singleton=1')

    def recover_expired(self):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.fence(c)
            expired = c.execute("SELECT j.* FROM video_stream_jobs_v1 j WHERE status='RUNNING' AND lease_until<=?", (time.time(),)).fetchall()
            for job in expired:
                maximum = c.execute('SELECT max_attempts FROM video_stream_retry_limits_v1 WHERE job_id=?',(job['job_id'],)).fetchone()[0]
                status = 'RETRY_PENDING' if job['attempts'] < maximum else 'TERMINAL_FAILED'
                c.execute('UPDATE video_stream_jobs_v1 SET status=?,token=NULL,lease_until=NULL WHERE job_id=?', (status,job['job_id']))
                hook=getattr(self,'attempt_failure_hook',None)
                if hook is not None:
                    recovered=dict(job);recovered['payload']=json.loads(recovered['payload'])
                    hook(c,recovered,status,RuntimeError('lease_expired_before_durable_commit'))
                c.execute("UPDATE video_stream_attempts_v1 SET finished_at=?,status=?,error_type='LEASE_EXPIRED',error_message='owner did not commit before lease expiry' WHERE job_id=? AND attempt_number=?", (time.time(),status,job['job_id'],job['attempts']))
                if job['stage'] in ('qwen','ocr'):
                    recovered=dict(job);recovered['payload']=json.loads(recovered['payload'])
                    self._candidate_failure(c,recovered,status,'RETRYABLE_TRANSIENT_RESOURCE_FAILURE','lease_expired_before_durable_commit')
            return len(expired)

    def branch_state(self, source_id, revision, stage):
        with self.connection() as c:
            branch = c.execute('SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage=?', (source_id,revision,stage)).fetchone()
            if not branch:
                return 'MANIFEST_PENDING'
            rows = c.execute('SELECT status,attempts FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=?',(source_id,revision,stage)).fetchall()
            if len(rows)!=branch['expected']:
                return 'MANIFEST_PENDING'
            if any(r['attempts']==0 and r['status']!='DONE' for r in rows):
                return 'FIRST_PASS_RUNNING'
            if all(r['status'] in ('DONE','TERMINAL_FAILED') for r in rows):
                return 'QWEN_COMPLETE' if stage=='qwen' else 'OCR_COMPLETE'
            if all(r['attempts'] and not(r['attempts']==1 and r['status']=='RUNNING') for r in rows):
                return 'QWEN_FIRST_PASS_DONE' if stage=='qwen' else 'OCR_FIRST_PASS_DONE'
            return 'FIRST_PASS_RUNNING'


class HotConsumers:
    def __init__(self, ledger, handlers, *, validators, capacities, batch_timeout=.03, input_verifier=None,admission=None,claim_admission=None):
        self.ledger, self.handlers = ledger, handlers
        if set(validators)!=set(handlers):
            raise ValueError('every_consumer_requires_frozen_validator')
        self.validators=validators
        self.input_verifier=input_verifier
        self.admission=admission
        self.claim_admission=claim_admission
        self.queues = {s: queue.Queue(capacities[s]) for s in handlers}
        self.enqueued = {s:set() for s in handlers}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.batch_timeout = batch_timeout
        self.errors = []
        self.failure_records=[]
        self.closure_phase={};self.closure_refill={}
        self.threads = []

    def start(self):
        for stage,value in self.handlers.items():
            for number,handler in enumerate(value if isinstance(value,list) else [value]):
                thread = threading.Thread(target=self.consume, args=(stage,handler), name=f'video-consumer-{stage}-{number}')
                thread.start()
                self.threads.append(thread)

    def closure_set_phase(self,stage,phase):
        import sys
        self.closure_phase[stage]=dict(phase=phase,at=time.time(),mono=time.monotonic(),function='HotConsumers.consume',callsite=__file__+':'+str(sys._getframe(1).f_lineno))
        from .build151_whisper_feed import call
        call(self.ledger,'phase',stage,phase)

    def refill(self):
        # Short indexed discovery; no source advance and no global source count.
        for stage, target in self.queues.items():
            if stage in ('qwen','whisper'):continue
            self.closure_refill=dict(stage=stage,operation='admission',mono=time.monotonic(),callsite=__file__+':refill')
            allowed=self.admission is None or self.admission(stage)
            if not allowed:
                continue
            self.closure_refill=dict(stage=stage,operation='ledger_pending',mono=time.monotonic(),callsite=__file__+':refill.pending')
            for key in self.ledger.pending(stage, target.maxsize):
                with self.lock:
                    if key in self.enqueued[stage]:
                        continue
                    try:
                        target.put_nowait(key)
                        self.enqueued[stage].add(key)
                    except queue.Full:
                        break

    def consume(self, stage, handler=None):
        target, handler = self.queues[stage], handler or self.handlers[stage]
        while not self.stop.is_set():
            self.closure_set_phase(stage,'WAITING_QUEUE')
            try:
                keys = [target.get(timeout=.1)]
            except queue.Empty:
                # A ready model must not wait behind another stage's synchronous
                # refill admission. Refill at most one stable ID on its existing
                # worker; the normal lease/resource/claim fences still decide.
                if stage in ('qwen','whisper') and not self.stop.is_set():
                    self.closure_set_phase(stage,'CLAIM_WAKE_DISCOVERY')
                    try:
                        discovered=self.ledger.pending(stage,1)
                        with self.lock:
                            for key in discovered:
                                if self.stop.is_set() or key in self.enqueued[stage]:continue
                                try:target.put_nowait(key);self.enqueued[stage].add(key)
                                except queue.Full:break
                        if not target.empty():continue
                    except StaleResult:
                        if self.stop.is_set():break
                        raise
                idle=getattr(handler,'idle',None)
                if idle:
                    try:idle()
                    except Exception as error:
                        self.errors.append('IDLE_MODEL_RETIRE_FAILED:'+str(error));self.stop.set()
                continue
            if stage == 'openclip':
                deadline = time.monotonic()+self.batch_timeout
                while len(keys)<8 and not self.stop.is_set():
                    try:
                        keys.append(target.get(timeout=max(0,deadline-time.monotonic())))
                    except queue.Empty:
                        break
            jobs = [];operation='admission';retry_count=0;operation_started=time.monotonic()
            lease=None
            self.closure_set_phase(stage,'MODEL_LEASE_ADMISSION')
            try:
                lease_factory=getattr(handler,'work_lease',None)
                if lease_factory:
                    lease=lease_factory();lease.__enter__()
                claim_admission=self.claim_admission or self.admission
                if claim_admission is not None and not claim_admission(stage):
                    self.stop.wait(.05)
                    continue
                for key in keys:
                    if self.stop.is_set():
                        break
                    chosen=key
                    # Admission occurs only when this worker can claim, not
                    # during fast-dispatch prefetch or a running inference.
                    self.closure_set_phase(stage,'LEDGER_PENDING')
                    ready=self.ledger.pending(stage,1,admit=True)
                    if ready:chosen=ready[0]
                    operation='claim';operation_started=time.monotonic()
                    self.closure_set_phase(stage,'CLAIM')
                    observer=getattr(self.ledger,'claim_truth',None)
                    if observer and stage in ('qwen','whisper'):observer.observe(stage)
                    job = self.ledger.claim(chosen)
                    if job:
                        jobs.append(job)
                        claimed=getattr(handler,'claimed',None)
                        if claimed:claimed(job)
                if jobs:
                    operation='input_validation'
                    self.closure_set_phase(stage,'VERIFY_INPUT')
                    if self.input_verifier:
                        for job in jobs:self.input_verifier(job)
                    prepare=getattr(handler,'prepare',None)
                    operation='model_prepare'
                    self.closure_set_phase(stage,'MODEL_PREPARE')
                    if prepare:prepare()  # Cold load is not inference service time.
                    operation='model_inference'
                    self.closure_set_phase(stage,'INFERENCE')
                    started = time.monotonic()
                    if stage == 'openclip':
                        results = handler.batch([j['payload'] for j in jobs])
                    else:
                        results = [handler.run_job(j) if hasattr(handler,'run_job') else handler(j['payload']) for j in jobs]
                    self.closure_set_phase(stage,'RESULTS_READY')
                    if len(results) != len(jobs):
                        raise ValueError('batch_result_cardinality_mismatch')
                    elapsed = (time.monotonic()-started)*1000/len(jobs)
                    prior=self.ledger.admission.service.get(stage,elapsed)
                    self.ledger.admission.service[stage]=.2*elapsed+.8*prior
                    if getattr(handler,'service_time_unknown',False):elapsed=None
                    for job, result in zip(jobs,results):
                        operation='input_validation'
                        if self.input_verifier:self.input_verifier(job)
                        operation='result_validation'
                        validation_start=time.perf_counter()
                        self.validators[stage](result)
                        validation_ms=(time.perf_counter()-validation_start)*1000
                        operation='result_commit';operation_started=time.monotonic()
                        self.closure_set_phase(stage,'DURABLE_COMMIT')
                        commit_start=time.perf_counter()
                        self.ledger.commit(job,result,elapsed)
                        self.closure_set_phase(stage,'POST_COMMIT')
                        observer=getattr(self.ledger,'claim_truth',None)
                        if observer and stage in ('qwen','whisper','audio_extract','vad'):observer.ack(job)
                        if stage=='qwen':
                            from .build151_runtime_trace import emit
                            emit(self.ledger,dict(scope='consumer_persistence',execution_key=job['payload'].get('_evidence_key'),candidate_id=job['payload'].get('candidate_id'),job_id=job['job_id'],input_prepare_ms='UNKNOWN',image_decode_ms='UNKNOWN',prefill_ms='UNKNOWN',generation_ms='UNKNOWN',decode_parse_ms='UNKNOWN',validation_ms=validation_ms,persist_ms=(time.perf_counter()-commit_start)*1000,total_ms=(time.monotonic()-started)*1000),event='QWEN_TIMING')
                        if stage=='whisper':
                            # Observation after durable ACK; never changes
                            # claim/result/retry state if logging fails.
                            try:
                                with Path(self.ledger.path).with_name('whisper-commit-acks.jsonl').open('a') as timing:
                                    timing.write(encoded({'job_id':job['job_id'],'source_id':job['source_id'],'commit_end_at':time.time(),'inference_started_monotonic':started,'consumer_service_ms':elapsed})+'\n')
                            except OSError:pass
            except StaleResult as error:
                if stage in ('qwen','ocr') and jobs and operation=='input_validation' and all(candidate_input_failure(error,j) for j in jobs):
                    try:
                        for job in jobs:self.ledger.failed(job,error,operation=operation)
                    except Exception as failure_ack_error:
                        self.errors.append('CANDIDATE_FAILURE_ACK_BLOCKED:'+str(failure_ack_error));self.stop.set()
                else:
                    self.errors.append(str(error))
                    for job in jobs:
                        try:self.ledger.failed(job,error,operation=operation)
                        except StaleResult:
                            # Changed epoch/revision/token belongs to recovery,
                            # never overwrite a new owner's state.
                            pass
            except sqlite3.Error as error:
                # A control-plane DB failure is not a model failure and must
                # never consume schema retry budget. Preserve owned RUNNING.
                self.errors.append('DB_COMMIT_BLOCKED:'+str(error))
                record={'timestamp':time.time(),'operation':operation,
                    'operation_duration_ms':(time.monotonic()-operation_started)*1000,'retry_count':retry_count,
                    'inference_started':bool(jobs),'commit_attempted':operation=='result_commit',
                    'exception_type':type(error).__name__,'exception_message':str(error),
                    'sqlite_errorcode':getattr(error,'sqlite_errorcode',None),
                    'sqlite_errorname':getattr(error,'sqlite_errorname',None),
                    'traceback':traceback.format_exc(),'thread':threading.current_thread().name,
                    'stage':stage,'owner':self.ledger.owner,'epoch':self.ledger.epoch,'queued_job_ids':keys,
                    'jobs':[{**{k:j.get(k) for k in ('job_id','source_id','revision','status','attempts','token','lease_until','input_sha')},
                        'execution_key':j['payload'].get('_evidence_key'),
                        'candidate_id':j['payload'].get('candidate_id'),
                        'frame_id':j['payload'].get('frame_id'),
                        'target_status':'DONE' if operation=='result_commit' else 'RUNNING'} for j in jobs]}
                self.failure_records.append(record)
                # Separate file survives an unavailable SQLite writer.
                with Path(self.ledger.path).with_name('consumer-failures.jsonl').open('a') as f:
                    f.write(encoded(record)+'\n')
                self.stop.set()
            except Exception as error:
                candidate_local=stage in ('qwen','ocr') and bool(jobs) and (operation in ('model_prepare','model_inference','result_validation') or (operation=='input_validation' and all(candidate_input_failure(error,j) for j in jobs)))
                if not candidate_local:self.errors.append(str(error))
                if operation in ('claim','result_commit') or (operation=='input_validation' and not candidate_local):
                    record={'timestamp':time.time(),'operation':operation,'stage':stage,
                        'exception_type':type(error).__name__,'exception_message':str(error),
                        'traceback':traceback.format_exc(),'thread':threading.current_thread().name,
                        'owner':self.ledger.owner,'epoch':self.ledger.epoch,
                        'jobs':[{k:j.get(k) for k in ('job_id','source_id','revision','attempts','token','status')} for j in jobs]}
                    self.failure_records.append(record)
                    with Path(self.ledger.path).with_name('consumer-failures.jsonl').open('a') as f:f.write(encoded(record)+'\n')
                    self.stop.set()
                    continue
                for job in jobs:
                    try:
                        self.ledger.failed(job,error,operation=operation)
                    except StaleResult:
                        pass
                    except Exception as failure_ack_error:
                        self.errors.append('CANDIDATE_FAILURE_ACK_BLOCKED:'+str(failure_ack_error));self.stop.set()
            finally:
                from .build151_whisper_feed import call
                call(self.ledger,'attempt_finished',stage,operation)
                if lease is not None:lease.__exit__(None,None,None)
                with self.lock:
                    for key in keys:
                        self.enqueued[stage].discard(key)
                        target.task_done()

    def stop_claims(self):
        # Synchronous in-memory cutoff at Runtime Stop acceptance. Ledger's
        # persisted stopped flag follows owned publication drain at close;
        # all new physical claims already share the Runtime owner fence.
        self.stop.set()

    def close(self, timeout=30):
        self.stop_claims()
        self.ledger.stop_claims()
        self.stop.set()
        deadline = time.monotonic()+timeout
        for thread in self.threads:
            thread.join(max(0,deadline-time.monotonic()))
        if any(t.is_alive() for t in self.threads):
            raise TimeoutError('inference_still_running_do_not_resume')
