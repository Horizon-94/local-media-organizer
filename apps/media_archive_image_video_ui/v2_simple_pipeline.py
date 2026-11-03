"""Opt-in experimental topology. No model/evidence/identity policy changes.

Each coordinator owns its mutable index/selection objects; one ordered actor
owns people reconciliation. FAST_DISPATCH
never invokes their long methods. Commands contain IDs, not connections or
cursors. All DB effects use the existing per-call fenced connections.
"""
from collections import deque
import inspect
import json
import sqlite3
import threading
import time
from .v2_runtime import ContractError
from .v2_people import ReconciliationChanged
from .v2_producer import fingerprint


class CoordinationStopped(Exception):pass
class QueueFull(Exception):pass


class CoordinationConsumer:
    def __init__(self,pipeline,*,capacity=128,refresh_source=None,stop_requested=lambda:False,performance=None,clone_factory=None):
        if type(capacity) is not int or capacity<2:raise ContractError('simple_coordination_capacity_invalid')
        self.pipeline=pipeline;self.capacity=capacity;self.refresh_source=refresh_source
        self.stop_requested=stop_requested;self.performance=performance
        self.cv=threading.Condition();self.queue=deque();self.pending=set()
        self.results={};self.error=None;self.stopping=False;self.active=set()
        self.maximum_depth=0;self.durations=deque(maxlen=2048);self.busy_count=0
        # FRAME_READY routing belongs exclusively to the fast thread.
        self.previous_fanout=getattr(pipeline,'external_frame_fanout',False)
        self.pipelines=[pipeline]+([clone_factory()] if clone_factory else [])
        self.previous_fanouts=[getattr(p,'external_frame_fanout',False) for p in self.pipelines]
        for p in self.pipelines:p.external_frame_fanout=True
        self.threads=[threading.Thread(target=self._run,args=(p,),name='simple-source-coordination-'+str(i),daemon=False) for i,p in enumerate(self.pipelines)]
        self.thread=self.threads[0]
        try:
            for thread in self.threads:thread.start()
        except BaseException:
            with self.cv:self.stopping=True;self.cv.notify_all()
            for thread in self.threads:
                if thread.ident is not None:thread.join(1)
            for p,previous in zip(self.pipelines,self.previous_fanouts):p.external_frame_fanout=previous
            raise

    def boundary(self):
        if self.stopping or self.stop_requested():raise CoordinationStopped()

    def offer(self,key):
        with self.cv:
            if self.error:raise self.error
            if self.stopping:return False
            if key in self.pending:return True
            if len(self.pending)>=self.capacity:return False
            self.pending.add(key);self.queue.append(key)
            self.maximum_depth=max(self.maximum_depth,len(self.pending));self.cv.notify()
            return True

    def take_result(self,key):
        with self.cv:
            if self.error:raise self.error
            return self.results.pop(key,None)

    def idle(self):
        with self.cv:
            if self.error:raise self.error
            return not self.pending

    def _invoke(self,method,*args,**kw):
        parameters=inspect.signature(method).parameters
        return method(*args,**{k:v for k,v in kw.items() if k in parameters})

    def _run(self,pipeline):
        try:
            while True:
                with self.cv:
                    while not self.queue and not self.stopping:self.cv.wait(.05)
                    if self.stopping:return
                    key=self.queue.popleft();self.active.add(key)
                started=time.monotonic()
                try:
                    self.boundary()
                    with pipeline.store.connect() as c:
                        input_marker=c.execute('SELECT max(rowid) FROM v2_outbox').fetchone()[0]
                    if key==('events',):
                        result=self._invoke(pipeline.advance_events,cooperative=True,yield_hook=self.boundary)
                    else:
                        result=self._invoke(pipeline.advance_source,*key,yield_hook=self.boundary)
                        if self.refresh_source:self.refresh_source(*key)
                    with self.cv:
                        # At most capacity unconsumed completions, each is a
                        # small outcome header. Repeated requests replace it.
                        if len(self.results)>=self.capacity and key not in self.results:
                            self.results.pop(next(iter(self.results)))
                        self.results[key]={'value':result,'input_marker':input_marker}
                except CoordinationStopped:
                    return
                except ReconciliationChanged as exc:
                    with self.cv:self.results[key]={'value':{'status':'WAITING','reason_code':str(exc)},'input_marker':input_marker}
                except QueueFull as exc:
                    # Existing event transaction rolled back atomically. It is
                    # left durable/unacked for a later low-water retry.
                    with self.cv:self.results[key]={'value':0 if key==('events',) else {'status':'WAITING','reason_code':str(exc)},'input_marker':input_marker,'retry':True}
                except sqlite3.OperationalError as exc:
                    code=getattr(exc,'sqlite_errorcode',0) or 0
                    if (code&255) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                    self.busy_count+=1
                    if self.performance:self.performance.count('SIMPLE_DB_BUSY_RETRY')
                    with self.cv:self.results[key]={'value':0 if key==('events',) else {'status':'WAITING','reason_code':'DB_BUSY'},'input_marker':input_marker,'retry':True}
                finally:
                    elapsed=time.monotonic()-started;self.durations.append(elapsed)
                    if self.performance:self.performance.record('SOURCE_COORDINATION',elapsed)
                    with self.cv:self.pending.discard(key);self.active.discard(key);self.cv.notify_all()
        except BaseException as exc:
            with self.cv:self.error=exc;self.cv.notify_all()

    def close(self,timeout=30):
        with self.cv:self.stopping=True;self.queue.clear();self.pending.intersection_update(self.active);self.cv.notify_all()
        deadline=time.monotonic()+timeout
        for thread in self.threads:thread.join(max(0,deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):raise ContractError('simple_coordination_shutdown_timeout')
        for p,previous in zip(self.pipelines,self.previous_fanouts):p.external_frame_fanout=previous
        self.pipeline.external_frame_fanout=self.previous_fanout
        if self.error:raise self.error


class PeopleLane:
    """One ordered identity actor, separate from index/closure consumers.

    The original ingest/build/reconcile algorithms are called unchanged. Local
    readiness is revision/evidence/manual-decision bound and rechecked after
    computation. Another source's slow identity calculation cannot hold up a
    source whose identity phase is already current.
    """
    def __init__(self,people,*,stop_requested=lambda:False,performance=None):
        self.people=people;self.store=people.store;self.ready={};self.lock=threading.RLock()
        self.consumer=CoordinationConsumer(self,stop_requested=stop_requested,performance=performance)

    def token(self,source,revision):
        from .v2_source_lineage import current_source_rows
        with self.store.connect() as c:
            c.execute('BEGIN')
            owner=self.store.scheduler_owner
            if owner:
                epoch=c.execute('SELECT scheduler_run_id,execution_epoch FROM v2_source_epochs WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
                if not epoch or tuple(epoch)!=(owner['scheduler_run_id'],owner['execution_epoch']):
                    from .scheduler_identity import SchedulerOwnershipError
                    raise SchedulerOwnershipError('stale_source_execution_epoch')
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
            if not active or active[0]!=revision:raise ReconciliationChanged('SIMPLE_PEOPLE_SOURCE_REV_CHANGED')
            rows=current_source_rows(c,source,revision,"j.stage IN ('yoloe','insightface')",columns='j.execution_key,j.stage,j.status,j.result_sha')
            artifacts=[tuple(r) for r in c.execute('SELECT artifact_id,sha,width,height FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision))]
            decisions=[tuple(r) for r in c.execute('SELECT decision_id FROM v2_person_decisions ORDER BY decision_id')]
        return fingerprint((source,revision,sorted(tuple(r) for r in rows),artifacts,decisions))

    def advance_source(self,source,revision,*,yield_hook=None):
        before=self.token(source,revision)
        if yield_hook:yield_hook()
        self.people.ingest_ready(source,revision)
        if yield_hook:yield_hook()
        self.people.build_source(source,revision)
        self.people.reconcile_source(source,revision,yield_hook=yield_hook)
        if self.token(source,revision)!=before:raise ReconciliationChanged('SIMPLE_PEOPLE_EVIDENCE_CHANGED')
        return {'status':'COMPLETE','token':before}

    def ensure(self,source,revision):
        key=(source,revision);token=self.token(*key)
        with self.lock:
            result=self.consumer.take_result(key)
            if result and result['value'].get('status')=='COMPLETE':self.ready[key]=result['value']['token']
            if self.ready.get(key)==token:return
            self.consumer.offer(key)
        raise ReconciliationChanged('SIMPLE_PEOPLE_PHASE_PENDING')

    def ingest_ready(self,source,revision):self.ensure(source,revision)
    def build_source(self,source,revision):self.ensure(source,revision)
    def reconcile_source(self,source,revision,*,yield_hook=None):
        self.ensure(source,revision)
        return []

    def close(self):self.consumer.close()


class DirectQueueBudget:
    """Durable direct-queue counts, not unfinished-source/closure counts.

    Single raw publisher checks before publishing, outside a write transaction.
    Unrouted frames are bounded independently of model jobs. Fanout reserves
    whole frame quanta. Terminal failures do not consume normal runnable slots.
    """
    defaults={'raw_frames':(32,64,128),'visual':(128,256,512),
              'audio':(64,128,256),'qwen':(16,32,64),'ocr':(16,32,64),'embedding':(128,256,512),
              'coordination_events':(1024,2048,4096)}
    def __init__(self,store,consumer,*,policy=None,stop_requested=lambda:False):
        self.store=store;self.consumer=consumer;self.stop_requested=stop_requested
        self.policy={**self.defaults,**(policy or {})};self.paused={};self.last={};self.cancel=threading.Event()
        if set(self.policy)!=set(self.defaults):raise ContractError('simple_queue_policy_invalid')
        for limits in self.policy.values():
            if len(limits)!=3 or any(type(n) is not int for n in limits) or not 0<=limits[0]<limits[1]<=limits[2]:
                raise ContractError('simple_queue_watermark_invalid')

    @staticmethod
    def lane(stage):
        return ('visual' if stage in ('yoloe','openclip','dhash','geometry','quality','insightface') else
              'audio' if stage in ('audio_extract','vad','whisper') else stage if stage in ('qwen','ocr','embedding') else None)

    def enqueue_guard(self,c,stage):
        lane=self.lane(stage)
        if lane is None:return
        stages={'visual':('yoloe','openclip','dhash','geometry','quality','insightface'),
                'audio':('audio_extract','vad','whisper')}.get(lane,(stage,))
        n=c.execute('''SELECT count(*) FROM v2_jobs j JOIN v2_active_sources a
            ON a.source_id=j.source_id AND a.source_revision=j.source_revision
            WHERE j.status IN ('PENDING','RUNNING','FAILED_RETRYABLE')
            AND j.stage IN (SELECT value FROM json_each(?))''',(json.dumps(stages),)).fetchone()[0]
        if n>=self.policy[lane][2]:raise QueueFull('SIMPLE_'+lane.upper()+'_HIGH_WATER')

    def publication_guard(self,c,event_type,payload):
        # Selection remains one immutable, atomic batch on disk. Its manifest
        # is not the executable queue; promotion alone reserves queue capacity.
        return None

    def counts(self):
        with self.store.connect() as c:
            owner=self.store.scheduler_owner or {};run=owner.get('scheduler_run_id');epoch=owner.get('execution_epoch')
            pending=c.execute('''SELECT count(*) FROM v2_outbox o WHERE o.event_type='FRAME_READY'
              AND (? IS NULL OR (o.scheduler_run_id=? AND o.execution_epoch=?))
              AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)''',(run,run,epoch,self.consumer)).fetchone()[0]
            rows=c.execute('''SELECT stage,count(*) FROM v2_jobs j JOIN v2_active_sources a
              ON a.source_id=j.source_id AND a.source_revision=j.source_revision
              WHERE j.status IN ('PENDING','RUNNING','FAILED_RETRYABLE')
              AND (? IS NULL OR (j.scheduler_run_id=? AND j.execution_epoch=?)) GROUP BY stage''',(run,run,epoch)).fetchall()
            document_consumer=getattr(self,'document_consumer',None)
            events=c.execute('''SELECT count(*) FROM v2_outbox o
                WHERE o.event_type IN ('SOURCE_READY','AUDIO_ARTIFACT_READY','EVIDENCE_READY','CANDIDATE_READY')
                AND (? IS NULL OR (o.scheduler_run_id=? AND o.execution_epoch=?))
                AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)''',
                (run,run,epoch,document_consumer)).fetchone()[0] if document_consumer else 0
        jobs=dict(rows)
        self.last={'raw_frames':pending,'visual':sum(jobs.get(s,0) for s in ('yoloe','openclip','dhash','geometry','quality','insightface')),
                   'audio':sum(jobs.get(s,0) for s in ('audio_extract','vad','whisper')),
                   'qwen':jobs.get('qwen',0),'ocr':jobs.get('ocr',0),'embedding':jobs.get('embedding',0),
                   'coordination_events':events}
        return dict(self.last)

    def can_refill(self,kind):
        counts=self.counts()
        # Audio publication has its own durable bounded promotion lane. A full
        # Whisper execution queue must not freeze independent visual supply.
        lanes=('raw_frames','visual','coordination_events')
        for lane in lanes:
            low,target,high=self.policy[lane]
            if counts[lane]>=high-(256 if lane=='coordination_events' else 0):self.paused[lane]=True
            if counts[lane]<=low:self.paused[lane]=False
        blocked=next((lane for lane in lanes if self.paused.get(lane)),None)
        return blocked is None,('SIMPLE_'+blocked.upper()+'_HIGH_WATER' if blocked else None)

    def fanout_limit(self,maximum,stages):
        counts=self.counts();room=self.policy['visual'][2]-counts['visual']
        return max(0,min(maximum,room//stages))

    def can_dispatch(self,inflight):
        counts=self.counts()
        # Existing bounded worker quanta are at most32 commits each. Reserve
        # enough result events for every in-flight future before the next claim;
        # never reject a valid model output after inference has already run.
        return counts['coordination_events']+32*(inflight+1)<self.policy['coordination_events'][2]

    def before_publish(self,*_):
        # No connection/cursor survives this sleep. Single publisher makes the
        # pending-frame check + next publication bounded without over-reserving.
        while self.counts()['raw_frames']>=self.policy['raw_frames'][2]:
            if self.cancel.is_set() or self.stop_requested():raise CoordinationStopped()
            time.sleep(.02)


class SimplePipelineFacade:
    def __init__(self,pipeline,consumer,budget):
        self.original=pipeline;self.coordination=consumer;self.budget=budget
        self.event_marker=None;self.more_events=True;self.outcomes={}
    def __getattr__(self,key):return getattr(self.original,key)
    def advance_events(self,*,cooperative=False,yield_hook=None):
        completed=self.coordination.take_result(('events',))
        if completed is not None:self.more_events=bool(completed['value']) or bool(completed.get('retry'))
        limit=self.budget.fanout_limit(8,len(self.original.selection.REQUIRED)+int('insightface' in self.original.contracts))
        count=0
        if limit:
            count=self.original.store.deliver_subscribed(self.original.fanout_consumer,self.original.route,('FRAME_READY',),limit=limit)
        with self.original.store.connect() as c:marker=c.execute('SELECT max(rowid) FROM v2_outbox').fetchone()[0]
        if self.more_events or marker!=self.event_marker:
            if self.coordination.offer(('events',)):self.event_marker=marker
        return count
    def advance_source(self,source,revision,*,yield_hook=None):
        key=(source,revision);result=self.coordination.take_result(key)
        if result is None:
            with self.original.store.connect() as c:marker=c.execute('SELECT max(rowid) FROM v2_outbox').fetchone()[0]
            cached=self.outcomes.get(key)
            if cached and cached[0]==marker:return cached[1]
            accepted=self.coordination.offer(key)
            return {'status':'WAITING','reason_code':'SOURCE_COORDINATION_PENDING' if accepted else 'SOURCE_COORDINATION_QUEUE_FULL'}
        if result['value'].get('status') in ('COMPLETE','DEGRADED','FAILED_FINAL'):
            self.outcomes[key]=(result['input_marker'],result['value'])
        return result['value']
