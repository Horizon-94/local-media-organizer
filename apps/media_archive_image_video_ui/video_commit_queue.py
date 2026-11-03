"""Bounded FIFO SQL-only commit queue; inference/serialization are caller work.

The ACK follows COMMIT. A failed fence rolls back the complete proposal.
Telemetry belongs to an external report, never to the media database.
"""
from concurrent.futures import Future
from dataclasses import dataclass
import collections,json,queue,sqlite3,threading,time,uuid
from pathlib import Path


@dataclass(frozen=True)
class SQLCommand:
    sql: str
    parameters: tuple = ()

    def __post_init__(self):
        if not isinstance(self.parameters,tuple) or any(not isinstance(v,(str,bytes,int,float,type(None))) for v in self.parameters):
            raise TypeError('immutable_SQL_parameters_required')


@dataclass(frozen=True)
class WriteProposal:
    operation: str
    source: str
    commands: tuple[SQLCommand,...]
    checks: tuple[tuple[SQLCommand,tuple],...] = ()
    proposal_id: str = ''
    telemetry_json: str = '{}'

    def __post_init__(self):
        if not self.proposal_id:object.__setattr__(self,'proposal_id',uuid.uuid4().hex)
        if not isinstance(self.commands,tuple) or any(not isinstance(c,SQLCommand) for c in self.commands):
            raise TypeError('immutable_command_tuple_required')


class StaleProposal(Exception):
    def __init__(self,message,details=None):
        super().__init__(message);self.details=details or {}


WRITER_DEADLINE_SECONDS = 2.0
MAX_WRITER_DEADLINE_RETRIES = 3
WRITER_DEADLINE_BACKOFF = (.25, .5, 1.0)


class RetryableWriterDeadline(RuntimeError):
    """Only a locally cancelled, completely rolled-back proposal may retry."""


class WriterDeadlineRetry:
    def __init__(self):self.failures=0;self.batch_limit=32

    def failed(self):
        self.failures+=1
        if self.failures>MAX_WRITER_DEADLINE_RETRIES:
            raise RuntimeError('writer_deadline_retry_exhausted')
        self.batch_limit=max(1,self.batch_limit//2)
        return WRITER_DEADLINE_BACKOFF[self.failures-1]

    def committed(self):self.failures=0;self.batch_limit=32


def apply(con,proposal,metrics):
    start=time.monotonic();acquired=None;committed=False;rows=con.total_changes;failure=None
    fence_ms=0.;statement_ms=0.;fts_ms=0.;vector_ms=0.;slow_fences=[]
    deadline_fired=False;phase='begin';command_index=None;retryable=False
    def progress():
        nonlocal deadline_fired
        if acquired is not None and time.monotonic()-acquired>=WRITER_DEADLINE_SECONDS:
            deadline_fired=True;return 1
        return 0
    con.set_progress_handler(progress,1000)
    try:
        con.execute('BEGIN IMMEDIATE');acquired=time.monotonic()
        phase='fence'
        for check_index,(command,expected) in enumerate(proposal.checks):
            checking=time.perf_counter()
            row=con.execute(command.sql,command.parameters).fetchone()
            elapsed=(time.perf_counter()-checking)*1000;fence_ms+=elapsed
            if elapsed>=100:
                slow_fences.append({'check_index':check_index,'elapsed_ms':elapsed,'sql':command.sql,'parameters':command.parameters})
            if row is None or tuple(row)!=expected:
                parameter=command.parameters[0] if command.parameters else None
                generation='change_tokens' in command.sql
                changed=str(parameter).removeprefix('source:') if generation and str(parameter).startswith('source:') else None
                dependency=parameter if 'WHERE execution_key=?' in command.sql or 'WHERE j.execution_key=?' in command.sql else None
                reason='generation_changed' if generation else 'proof_or_owner_changed'
                semantic='video_peer_text_v1' in command.sql
                if semantic:
                    reason='semantic_peer_set_changed'
                    old_keys=set(json.loads(expected[0]));new_keys=set(json.loads(row[0])) if row else set()
                    dependency=sorted({v.split(':',1)[0] for v in old_keys^new_keys})
                    owners=[r[0] for r in con.execute('SELECT DISTINCT source_id FROM video_peer_text_v1 WHERE execution_key IN (SELECT value FROM json_each(?))',(json.dumps(dependency[:64]),))]
                    changed=owners[0] if len(owners)==1 else None
                elif not generation and ('FROM v2_active_sources WHERE source_id=?' in command.sql or 'FROM video_stream_sources_v1 WHERE source_id=?' in command.sql):
                    changed=parameter;reason='source_revision_changed'
                elif 'video_stream_control_v1' in command.sql:reason='owner_epoch_changed'
                raise StaleProposal('revision_or_owner_fence_changed',{
                    'stale_reason':reason,
                    'which_generation_changed':parameter if generation else None,
                    'which_source_changed':changed,
                    'which_dependency_changed':dependency,
                    'authority_scope_changed':parameter if generation and not changed else None,
                    'whether_unrelated_source_caused_stale':None if generation and not changed else False if semantic else bool(changed and changed!=proposal.source and changed not in json.loads(proposal.telemetry_json).get('real_dependency_sources',[])),
                    'check_sql':command.sql,'parameters':command.parameters,'expected':expected,
                    'actual':None if row is None else tuple(row)})
        phase='execution'
        # FTS5 document_id is UNINDEXED. Resolve targeted rowids once inside
        # this same IMMEDIATE transaction, instead of scanning all FTS rows for
        # every document. No persistent cache, schema change or weaker fence.
        fts_delete='DELETE FROM v2_text_fts WHERE document_id=?'
        fts_bulk='DELETE FROM v2_text_fts WHERE document_id IN (SELECT value FROM json_each(?))'
        fts_insert='INSERT INTO v2_text_fts VALUES(?,?,?)'
        delete_targets={}
        for index,cmd in enumerate(proposal.commands):
            if cmd.sql==fts_delete:delete_targets[index]=[cmd.parameters[0]]
            elif cmd.sql==fts_bulk:
                try:
                    values=json.loads(cmd.parameters[0])
                    if isinstance(values,list) and all(isinstance(v,str) for v in values):delete_targets[index]=values
                except (ValueError,TypeError):pass  # Original SQLite error/semantics below.
        targets=sorted({key for values in delete_targets.values() for key in values})
        fts_rows=None
        if len(targets)>1:
            command=proposal.commands[next(iter(delete_targets))]
            fts_rows={key:[] for key in targets}
            for rowid,key in con.execute('SELECT rowid,document_id FROM v2_text_fts WHERE document_id IN (SELECT value FROM json_each(?))',(json.dumps(targets),)):
                fts_rows[key].append(rowid)
        for command_index,command in enumerate(proposal.commands):
            executing=time.perf_counter()
            if fts_rows is not None and command_index in delete_targets:
                ids={rowid for key in delete_targets[command_index] for rowid in fts_rows[key]}
                if ids:con.execute('DELETE FROM v2_text_fts WHERE rowid IN (SELECT value FROM json_each(?))',(json.dumps(sorted(ids)),))
                for key in delete_targets[command_index]:fts_rows[key]=[]
            else:
                cursor=con.execute(command.sql,command.parameters)
                if fts_rows is not None and 'v2_text_fts' in command.sql:
                    if command.sql==fts_insert:
                        key=command.parameters[0]
                        if key in fts_rows:fts_rows[key].append(cursor.lastrowid)
                    else:fts_rows=None  # Unknown mutation: retain original SQL semantics.
            elapsed=(time.perf_counter()-executing)*1000
            statement_ms+=elapsed
            if 'v2_text_fts' in command.sql:fts_ms+=elapsed
            if 'v2_vectors' in command.sql:vector_ms+=elapsed
        phase='commit';before_commit=time.monotonic();con.commit();committed=True
        return con.total_changes-rows
    except BaseException as error:
        failure={'exception_type':type(error).__name__,'message':str(error),
            'stale_detail':getattr(error,'details',None),
            'failed_sql':command.sql if phase in ('fence','execution') else None,
            'sqlite_errorcode':getattr(error,'sqlite_errorcode',None),
            'sqlite_errorname':getattr(error,'sqlite_errorname',None)}
        # A spent callback must not interrupt rollback or the next request.
        con.set_progress_handler(None,0)
        transaction_open=con.in_transaction
        con.rollback()
        if con.in_transaction:raise RuntimeError('writer_rollback_incomplete') from error
        retryable=deadline_fired and not committed and (phase!='commit' or transaction_open) and getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT
        if retryable:raise RetryableWriterDeadline('retryable_writer_deadline') from error
        raise
    finally:
        end=time.monotonic();con.set_progress_handler(None,0)
        metrics.append({'timestamp':time.time(),'proposal_id':proposal.proposal_id,
            'deadline_seconds':WRITER_DEADLINE_SECONDS,'deadline_fired':deadline_fired,
            'deadline_phase':phase,'command_index':command_index,'command_count':len(proposal.commands),
            'retryable_deadline':retryable,
            'fence_check_ms':fence_ms,'writer_statement_ms':statement_ms,'fts_write_ms':fts_ms,'vector_write_ms':vector_ms,
            'fence_check_count':len(proposal.checks),'slow_fence_checks':slow_fences,
            'context':json.loads(proposal.telemetry_json),'durable_rows_changed':con.total_changes-rows if committed else 0,
            'connection_id':id(con),'thread':threading.current_thread().name,
            'operation':proposal.operation,'source':proposal.source,'rows':con.total_changes-rows,
            'begin_wait_ms':((acquired or end)-start)*1000,
            'writer_hold_ms':None if acquired is None else (end-acquired)*1000,
            'commit_ms':(end-before_commit)*1000 if committed else None,'rollback':not committed,
            'failure':failure,'busy_locked':bool(failure and failure['sqlite_errorcode'] is not None and (failure['sqlite_errorcode'] & 255) in (5,6))})


class CommitQueue:
    def __init__(self,path,capacity=64,trace_path=None,history_limit=None):
        self.path=str(path);self.queue=queue.Queue(capacity)
        self.metrics=collections.deque(maxlen=history_limit) if history_limit is not None else []
        self.trace_path=Path(trace_path) if trace_path is not None else None
        self.trace_errors=[]
        self.started=threading.Event();self.startup_error=None
        self.lock=threading.Lock();self.closed=False
        self.thread=threading.Thread(target=self._run,name='video-db-writer');self.thread.start()
        if not self.started.wait(5):raise TimeoutError('writer_startup_not_acknowledged')
        if self.startup_error is not None:raise self.startup_error

    def submit(self,proposal):
        if not isinstance(proposal,WriteProposal):raise TypeError('immutable_sql_proposal_required')
        future=Future()
        future.queued_at=time.time();future.queued_monotonic=time.monotonic()
        # Once accepted, ownership lasts through durable ACK. Caller cancel
        # cannot race set_result and kill the writer/lose following proposals.
        future.set_running_or_notify_cancel()
        with self.lock:
            if self.closed:raise RuntimeError('writer_closed')
            self.queue.put((proposal,future))
        return future

    def _run(self):
        con=None
        try:
            from .video_stream_ledger import TrackedConnection
            con=sqlite3.connect(self.path,timeout=2,factory=TrackedConnection)
            con.execute('PRAGMA foreign_keys=ON')
        except BaseException as error:
            self.startup_error=error
            if con is not None:con.close()
            self.started.set()
            return
        self.started.set()
        try:
            while True:
                value=self.queue.get()
                try:
                    if value is None:return
                    proposal,future=value
                    future.writer_started_at=time.time();future.writer_started_monotonic=time.monotonic()
                    try:
                        result=apply(con,proposal,self.metrics)
                        self.metrics[-1]['queue_wait_ms']=(future.writer_started_monotonic-future.queued_monotonic)*1000
                        future.commit_metrics=dict(self.metrics[-1])
                        if not future.cancelled():future.set_result(result)
                    except BaseException as error:
                        self.metrics[-1]['queue_wait_ms']=(future.writer_started_monotonic-future.queued_monotonic)*1000
                        future.commit_metrics=dict(self.metrics[-1])
                        if not future.cancelled():future.set_exception(error)
                finally:self.queue.task_done()
                # Outside the transaction. Diagnostic failure must never
                # convert a successful durable ACK into a model retry.
                if self.trace_path is not None and value is not None:
                    try:
                        with self.trace_path.open('a') as stream:
                            stream.write(json.dumps(self.metrics[-1],sort_keys=True)+'\n')
                    except OSError as error:self.trace_errors.append(str(error))
        finally:con.close()

    def close(self):
        with self.lock:
            if not self.closed:self.closed=True;self.queue.put(None)
        self.thread.join(timeout=5)
        if self.thread.is_alive():raise TimeoutError('writer_did_not_finish_owned_commit')
