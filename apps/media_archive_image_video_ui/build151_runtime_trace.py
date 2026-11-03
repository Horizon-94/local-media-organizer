"""Bounded, best-effort telemetry; never changes claim or dispatch decisions."""
import json,time
from pathlib import Path

def emit(ledger,row,event='QWEN_PENDING_DECISION'):
    signature=json.dumps(row,sort_keys=True,default=str)
    cache=getattr(ledger,'_build151_trace_cache',{})
    key=(event,row.get('scope'),row.get('job_id'),row.get('execution_key'),row.get('candidate_id'),row.get('claim_attempted'))
    if cache.get(key)==signature and not row.get('claim_attempted'):return
    cache[key]=signature;ledger._build151_trace_cache=cache
    try:
        with Path(ledger.path).with_name('qwen-pending-decisions.jsonl').open('a') as f:
            f.write(json.dumps(dict(event=event,timestamp=time.time(),**row),ensure_ascii=False,default=str)+'\n')
    except OSError:pass

def _native(ledger,c,job,resource_reason='UNKNOWN',claim_attempted=False,claim_result=None,claim_failure_reason=None,wave_eligible_override=None):
    if not job or job['stage']!='qwen':return
    p=job['payload'];p=json.loads(p) if isinstance(p,str) else p
    branch=c.execute("SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage='qwen'",(job['source_id'],job['revision'])).fetchone()
    key=p.get('_evidence_key');cid=p.get('candidate_id')
    canonical=c.execute('SELECT status FROM v2_jobs WHERE execution_key=?',(key,)).fetchone() if key else None
    selected=c.execute('SELECT status FROM v2_selected_candidates WHERE candidate_id=?',(cid,)).fetchone() if cid else None
    wave=ledger.wave_ready(c,job) if wave_eligible_override is None else wave_eligible_override
    emit(ledger,dict(execution_key=key,job_id=job['job_id'],source_id=job['source_id'],candidate_id=cid,
        canonical_state=canonical[0] if canonical else 'ABSENT',selected_candidate_state=selected[0] if selected else 'ABSENT',native_job_state=job['status'],
        manifest_sealed=bool(branch),manifest_expected=branch[0] if branch else None,wave_eligible=wave,
        wave_barrier_reason=None if wave else 'MANIFEST_NOT_SEALED' if not branch else 'FIRST_PASS_BARRIER_OR_NATIVE_STATE',
        resource_reason=resource_reason,claim_attempted=claim_attempted,claim_result=claim_result,claim_failure_reason=claim_failure_reason))

def pending(ledger,resource_reason):
    now=time.monotonic()
    if now-getattr(ledger,'_build151_pending_at',-100)<5:return
    ledger._build151_pending_at=now
    try:
        with ledger.connection() as c:
            native_counts={r[0]:r[1] for r in c.execute("SELECT status,count(*) FROM video_stream_jobs_v1 WHERE stage='qwen' AND status IN ('PENDING','RETRY_PENDING','RUNNING') GROUP BY status")}
            canonical_open=c.execute("SELECT count(*) FROM v2_jobs WHERE stage='qwen' AND status IN ('PENDING','RUNNING','FAILED_RETRYABLE')").fetchone()[0]
            selected_open=c.execute("SELECT count(*) FROM v2_selected_candidates x JOIN v2_active_sources a ON a.source_id=x.source_id AND a.source_revision=x.source_revision WHERE x.purpose='qwen' AND x.status IN ('QUEUED','READY')").fetchone()[0]
            supply=bool(native_counts or canonical_open or selected_open)
            if getattr(ledger,'_build151_pending_trace_failed',False):
                cache=getattr(ledger,'_build151_trace_cache',{})
                for key in list(cache):
                    if key[:2] in (('QWEN_PENDING_DECISION','SUPPLY_SUMMARY'),('QWEN_PENDING_TRACE_ERROR','observer')):cache.pop(key,None)
                ledger._build151_pending_trace_failed=False
            emit(ledger,dict(scope='SUPPLY_SUMMARY',execution_key=None,job_id=None,source_id=None,candidate_id=None,
                native_counts=native_counts,canonical_open_count=canonical_open,selected_open_count=selected_open,
                canonical_state='UNFINISHED' if canonical_open else 'EMPTY',selected_candidate_state='OPEN' if selected_open else 'EMPTY',
                native_job_state='RUNNING' if native_counts.get('RUNNING') else 'PENDING' if native_counts else 'EMPTY',
                manifest_sealed='UNKNOWN',manifest_expected='UNKNOWN',wave_eligible='UNKNOWN',
                wave_barrier_reason='SEE_JOB_GATES' if supply else 'NO_CANDIDATE_SUPPLY',resource_reason=resource_reason,
                claim_attempted=False,claim_result=None,claim_failure_reason=None))
            for job in c.execute("SELECT * FROM video_stream_jobs_v1 WHERE stage='qwen' AND status IN ('PENDING','RETRY_PENDING') LIMIT 64"):
                native(ledger,c,job,resource_reason)
            # Canonical -> native gaps remain visible without hydrating result bodies.
            for row in c.execute("SELECT execution_key,source_id,item_id,status FROM v2_jobs WHERE stage='qwen' AND status IN ('PENDING','FAILED_RETRYABLE') LIMIT 64"):
                mirror=c.execute("SELECT job_id FROM video_stream_jobs_v1 WHERE source_id=? AND stage='qwen' AND json_extract(payload,'$._evidence_key')=? LIMIT 1",(row['source_id'],row['execution_key'])).fetchone()
                if mirror:continue
                selected=c.execute('SELECT status FROM v2_selected_candidates WHERE candidate_id=?',(row['item_id'],)).fetchone()
                emit(ledger,dict(execution_key=row['execution_key'],job_id=None,source_id=row['source_id'],candidate_id=row['item_id'],canonical_state=row['status'],selected_candidate_state=selected[0] if selected else 'ABSENT',native_job_state='ABSENT',manifest_sealed='UNKNOWN',manifest_expected='UNKNOWN',wave_eligible='UNKNOWN',wave_barrier_reason='NATIVE_NOT_PROMOTED',resource_reason=resource_reason,claim_attempted=False,claim_result=None,claim_failure_reason=None))
            for row in c.execute("SELECT x.candidate_id,x.source_id,x.source_revision,x.status FROM v2_selected_candidates x JOIN v2_active_sources a ON a.source_id=x.source_id AND a.source_revision=x.source_revision WHERE x.purpose='qwen' AND x.status IN ('QUEUED','READY') AND NOT EXISTS(SELECT 1 FROM v2_jobs j WHERE j.stage='qwen' AND j.item_id=x.candidate_id) LIMIT 64"):
                branch=c.execute("SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage='qwen'",(row['source_id'],row['source_revision'])).fetchone()
                emit(ledger,dict(execution_key='UNKNOWN',job_id=None,source_id=row['source_id'],candidate_id=row['candidate_id'],canonical_state='ABSENT',selected_candidate_state=row['status'],native_job_state='ABSENT',manifest_sealed=bool(branch),manifest_expected=branch[0] if branch else None,wave_eligible='UNKNOWN',wave_barrier_reason='CANONICAL_NOT_PUBLISHED',resource_reason=resource_reason,claim_attempted=False,claim_result=None,claim_failure_reason=None))
    except Exception as error:
        ledger._build151_pending_trace_failed=True
        # Failed observation is UNKNOWN, never a fabricated empty queue.
        emit(ledger,dict(scope='observer',job_id=None,execution_key=None,claim_attempted=False,
             reason=type(error).__name__+':'+str(error)[:240]),event='QWEN_PENDING_TRACE_ERROR')

def native(*args,**kwargs):
    try:return _native(*args,**kwargs)
    except Exception:return None

# Claim truth is observation only; none of these facts changes a claim decision.
import threading,sqlite3,os
from .video_telemetry import CLOSURE_OUTPUT


def claim_facts(c,stage,context):
    from .v2_current_jobs_cache import ORIGINAL
    rows=list(c.execute("""SELECT j.job_id,j.source_id,j.revision,j.status,j.attempts,
        r.max_attempts,json_extract(j.payload,'$._evidence_key') evidence_key,
        n.revision native_revision,a.source_revision active_revision,
        EXISTS(SELECT 1 FROM video_model_delivery_v1 m WHERE m.job_id=j.job_id AND m.sealed_at IS NOT NULL) sealed,
        EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=j.source_id AND s.revision=j.revision AND s.state IN ('FIRST_SELECTION_PUBLISHED','SELECTION_DONE')) selection_published
        FROM video_stream_jobs_v1 j
        LEFT JOIN video_stream_retry_limits_v1 r ON r.job_id=j.job_id
        LEFT JOIN video_stream_sources_v1 n ON n.source_id=j.source_id
        LEFT JOIN v2_active_sources a ON a.source_id=j.source_id
        WHERE j.stage=? AND j.status IN ('PENDING','RETRY_PENDING')""",(stage,)))
    running=c.execute("SELECT count(*) FROM video_stream_jobs_v1 WHERE stage=? AND status='RUNNING'",(stage,)).fetchone()[0]
    context={**context,'worker_available':bool(context['worker_available'] and running==0)}
    blocked={k:0 for k in ('DEPENDENCY','RESOURCE','BACKOFF','REVISION','STOP','OTHER')};ready=[];details=[];structural=0
    for row in rows:
        r=dict(row);reason=None;category=None
        def reject(cat,why):return cat,why
        if r['native_revision']!=r['revision'] or r['active_revision']!=r['revision']:category,reason=reject('REVISION','ACTIVE_REVISION_MISMATCH')
        elif context['stop']:category,reason=reject('STOP','STOP_ACCEPTED')
        elif stage=='qwen' and (not r['sealed'] or not r['selection_published']):category,reason=reject('DEPENDENCY','SELECTION_MANIFEST_NOT_DURABLE')
        elif r['max_attempts'] is None or r['attempts']>=r['max_attempts']:category,reason=reject('OTHER','RETRY_BUDGET_EXHAUSTED')
        elif not r['evidence_key']:category,reason=reject('DEPENDENCY','CANONICAL_JOB_NOT_BOUND')
        else:
            root=c.execute("SELECT j.status FROM main.v2_jobs j WHERE j.execution_key=? AND j.status<>'SUPERSEDED' AND "+ORIGINAL,(r['evidence_key'],)).fetchone()
            if not root:category,reason=reject('DEPENDENCY','CANONICAL_ANCESTRY_NOT_CURRENT_OR_PARENT_NOT_DONE')
            elif root[0] not in ('PENDING','FAILED_RETRYABLE'):category,reason=reject('OTHER','CANONICAL_NOT_CLAIMABLE:'+str(root[0]))
        if reason is None:
            structural+=1
            barrier=stage=='qwen' and r['status']=='RETRY_PENDING' and c.execute('SELECT 1 FROM video_candidate_retry_v1 WHERE job_id=? AND not_before>?',(r['job_id'],time.time())).fetchone()
            if barrier or context.get('backoff',{}).get(r['source_id'],0)>context['mono']:category,reason=reject('BACKOFF','CANDIDATE_RETRY_BACKOFF' if barrier else 'SOURCE_RETRY_BACKOFF')
            elif context['resource_reason']:category,reason=reject('RESOURCE',context['resource_reason'])
            elif context.get('model_reason'):category,reason=reject('OTHER',context['model_reason'])
            elif not context['worker_available'] or running:category,reason=reject('OTHER','EXISTING_RUNNING_CLAIM' if running else 'WORKER_NOT_AVAILABLE')
        if reason:blocked[category]+=1;r['blocker_reason']=reason;r['blocked_by']=category
        else:ready.append(r);r['blocker_reason']=None
        details.append(r)
    return dict(registered_pending=len(rows),actionable_ready=len(ready),running=running,dependency_ready=structural,blocked=blocked,ready_jobs=ready,jobs=details,**context)


class ClaimTruth:
    def __init__(self,runtime,output_root=None):
        from .build151_whisper_feed import WhisperFeed
        try:self.whisper_feed=WhisperFeed(runtime)
        except Exception as error:self.whisper_feed=None;runtime._whisper_feed_error=repr(error)
        runtime.ledger.whisper_feed=self.whisper_feed
        self.runtime=runtime;self.lock=threading.RLock();self.generation=0;self.latest={};self.open={};self.last_claim_mono={};self.gaps={'qwen':[],'whisper':[]};self.incidents={'qwen':0,'whisper':0}
        try:self.task_id=json.loads((runtime.root.parent.parent/'task.json').read_text()).get('task_id',str(runtime.root.parent))
        except (OSError,ValueError):self.task_id=str(runtime.root.parent)
        default_output=self.whisper_feed.output/'claim-truth' if self.whisper_feed is not None else CLOSURE_OUTPUT/'truth-projection'
        self.output=Path(output_root) if output_root is not None else default_output;self.output.mkdir(parents=True,exist_ok=True)
        self.maxima={s:{k:0 for k in ('registered_pending','actionable_ready','running')} for s in ('qwen','whisper')}
        self.events=self.output.parent/'claim-liveness/events.jsonl';self.events.parent.mkdir(parents=True,exist_ok=True)
    def event(self,kind,**row):
        try:
            with self.events.open('a') as f:f.write(json.dumps(dict(event=kind,at=time.time(),mono=time.monotonic(),pid=os.getpid(),**row),default=str)+'\n')
        except OSError:pass
    def context(self,stage):
        r=self.runtime;models=[m for m in getattr(getattr(r,'lifecycle',None),'models',{}).values() if m.stage==stage]
        resident=any(m.resident and not m.loading and m.state not in ('LOADING','UNLOADING') for m in models)
        resource=r.governor.reason_for(stage,resident=resident) if not r.fixture_mode else None
        lifecycle=getattr(r,'lifecycle',None)
        model_reason=lifecycle.resource_reason(stage) if lifecycle else None
        if lifecycle and stage in lifecycle.drained:model_reason='MODEL_STAGE_DRAINED'
        if r.ledger.commit_queue.closed or not r.ledger.commit_queue.thread.is_alive():model_reason='WRITER_UNAVAILABLE'
        phase=getattr(r.consumers,'closure_phase',{}).get(stage,{})
        available=any(t.is_alive() and t.name.startswith('video-consumer-'+stage+'-') for t in r.consumers.threads)
        available=available and not any(m.loading or m.state=='UNLOADING' for m in models)
        return dict(stop=r.stop.is_set() or r.consumers.stop.is_set(),worker_available=available,worker_state=phase,model_state=[dict(state=m.state,resident=m.resident,loading=m.loading,owned=m.owned) for m in models],resource_reason=resource,model_reason=model_reason,governor_state=r.governor.snapshot(),backoff={sid:at for (s,sid),at in r.ledger.admission.blocked.items() if s==stage},mono=time.monotonic(),writer_state=dict(closed=r.ledger.commit_queue.closed,queue_depth=r.ledger.commit_queue.queue.qsize(),writer_alive=r.ledger.commit_queue.thread.is_alive()),db_state='READ_ONLY_SNAPSHOT',dispatch_state=getattr(r.consumers,'closure_refill',{}))
    def observe(self,stage):
        now=time.time();mono=time.monotonic()
        try:
            c=sqlite3.connect(Path(self.runtime.ledger.path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            try:
                state=c.execute('SELECT owner,epoch,stopped FROM video_stream_control_v1 WHERE singleton=1').fetchone()
                ctx=self.context(stage)
                if state['owner']!=self.runtime.ledger.owner or state['epoch']!=self.runtime.ledger.epoch:ctx['model_reason']='DB_OWNER_EPOCH_MISMATCH'
                if state['stopped']:ctx['stop']=True
                facts=claim_facts(c,stage,ctx)
                if stage=='whisper':
                    from .build151_whisper_feed import call
                    call(self.runtime.ledger,'observe',{**facts,'snapshot_started_mono':mono},c)
            finally:c.rollback();c.close()
            facts.update(observed_at=now,observed_mono=mono,freshness='FRESH',read_seconds=time.monotonic()-mono)
            with self.lock:
                if self.last_claim_mono.get(stage,-1)>mono:
                    facts.update(freshness='STALE',read_error='CONCURRENT_COMMITTED_CLAIM_SNAPSHOT_BOUNDARY')
                    self.latest[stage]=facts;self.generation+=1;self.publish();return
                if self.runtime.stop.is_set() or self.runtime.consumers.stop.is_set():
                    facts.update(stop=True,actionable_ready=0,ready_jobs=[])
                    facts['blocked']={k:(facts['registered_pending'] if k=='STOP' else 0) for k in facts['blocked']}
                self.latest[stage]=facts;self.generation+=1
                for key in self.maxima[stage]:self.maxima[stage][key]=max(self.maxima[stage][key],facts[key])
                if facts['actionable_ready']>0 and facts['running']==0:
                    if stage not in self.open:self.open[stage]=dict(at=now,mono=mono,job=facts['ready_jobs'][0],incident=False)
                    window=self.open[stage];age=time.monotonic()-window['mono']
                    if age>2 and not window['incident']:
                        window['incident']=True;self.incidents[stage]+=1
                        self.event(stage.upper()+'_SCHEDULER_LIVENESS_INCIDENT',stage=stage,gap_seconds=age,**self.blocker(stage,window,facts))
                elif stage in self.open and facts['running']==0:
                    window=self.open.pop(stage)
                    age=time.monotonic()-window['mono']
                    self.event('CLAIM_GAP_LEGAL_WINDOW_CLOSED',stage=stage,CLAIM_AT=None,gap_seconds=age,blocker=facts['jobs'][0].get('blocker_reason') if facts['jobs'] else 'NO_REGISTERED_PENDING',**self.blocker(stage,window,facts))
                self.publish()
        except Exception as error:
            with self.lock:
                if stage in self.open:
                    self.open.pop(stage,None);self.event('CLAIM_GAP_TELEMETRY_INTERRUPTED',stage=stage,reason=repr(error))
                previous=self.latest.get(stage,{})
                self.latest[stage]={**previous,'freshness':'STALE','error_at':time.time(),'read_error':repr(error)};self.generation+=1;self.publish()
            self.event('TRUTH_READ_FAILED',stage=stage,error=repr(error))
    def blocker(self,stage,window,facts):
        worker=facts.get('worker_state',{});dispatch=facts.get('dispatch_state',{});phase=worker.get('phase','THREAD_NOT_YET_OBSERVED')
        reason=('DISPATCH_REFILL:'+str(dispatch.get('stage'))+':'+str(dispatch.get('operation'))) if phase=='WAITING_QUEUE' else 'CONSUMER:'+phase
        return dict(TASK_ID=self.task_id,SOURCE_ID=window['job']['source_id'],REVISION=window['job']['revision'],READY_AT=window['at'],WORKER_STATE=worker,MODEL_STATE=facts.get('model_state'),GOVERNOR_STATE=facts.get('governor_state'),DEPENDENCY_STATE='DURABLE_CURRENT_READY',DB_STATE=facts.get('db_state'),WRITER_STATE=facts.get('writer_state'),RETRY_STATE='NO_LEGAL_BACKOFF',BLOCKER_REASON=reason)
    def committed_claim(self,job,wall,mono):
        stage=job['stage']
        from .build151_whisper_feed import call
        call(self.runtime.ledger,'claimed',job,wall,mono)
        if stage not in self.gaps:return
        with self.lock:
            self.last_claim_mono[stage]=mono
            window=self.open.pop(stage,None)
            if window:
                gap=mono-window['mono'];self.gaps[stage].append(gap)
                facts=self.latest.get(stage,{})
                info=self.blocker(stage,window,facts)
                info.update(READY_TRIGGER_JOB_ID=window['job']['job_id'],READY_TRIGGER_SOURCE_ID=window['job']['source_id'],READY_TRIGGER_REVISION=window['job']['revision'],SOURCE_ID=job['source_id'],REVISION=job['revision'])
                self.event('COMMITTED_CLAIM_GAP',stage=stage,job_id=job['job_id'],CLAIM_AT=wall,gap_seconds=gap,**info)
            else:self.event('CLAIM_WITHOUT_OBSERVED_READY_WINDOW',stage=stage,job_id=job['job_id'],CLAIM_AT=wall)
    def ack(self,job):
        self.event('DURABLE_RESULT_ACK',stage=job['stage'],job_id=job['job_id'])
        from .build151_whisper_feed import call
        call(self.runtime.ledger,'ack',job)
    def current(self):
        with self.lock:
            data=dict(task_id=self.task_id,workspace=str(self.runtime.root),owner=self.runtime.ledger.owner,epoch=self.runtime.ledger.epoch,maxima=self.maxima,incidents=self.incidents,observed_at=max((f.get('observed_at',0) for f in self.latest.values()),default=0),generation=self.generation,freshness='FRESH' if len(self.latest)==2 else 'STALE',freshness_ttl_seconds=1.5,stages={})
            for stage in ('qwen','whisper'):
                for key in ('REGISTERED_PENDING','ACTIONABLE_READY','RUNNING'):data[stage.upper()+'_'+key]=None
                for category in ('DEPENDENCY','RESOURCE','BACKOFF','REVISION','STOP','OTHER'):
                    data.setdefault('BLOCKED_BY_'+category if category!='OTHER' else 'OTHER_BLOCKED',{})[stage]=None
            for stage,f in self.latest.items():
                value={k:v for k,v in f.items() if k not in ('jobs','ready_jobs','backoff')}
                if f.get('freshness')!='FRESH' or time.time()-f.get('observed_at',0)>1.5:value['freshness']='STALE';data['freshness']='STALE'
                value['scope']='LIVE' if value.get('freshness')=='FRESH' else 'LAST_KNOWN'
                data['stages'][stage]=value
                for category,count in f.get('blocked',{}).items():data.setdefault('BLOCKED_BY_'+category if category!='OTHER' else 'OTHER_BLOCKED',{})[stage]=count
                for key in ('registered_pending','actionable_ready','running'):data[stage.upper()+'_'+key.upper()]=f.get(key) if value.get('freshness')=='FRESH' else None
            return data
    def publish(self):
        try:
            data=self.current();p=self.output/'backend-truth.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,default=str));tmp.replace(p)
        except OSError as error:
            self.event('TRUTH_PROJECTION_WRITE_FAILED',error=repr(error))
    def run(self):
        while not self.runtime.stop.is_set():
            for stage in ('qwen','whisper'):self.observe(stage)
            self.runtime.stop.wait(.2)
        for stage in ('qwen','whisper'):self.observe(stage)
        from .build151_whisper_feed import call
        call(self.runtime.ledger,'finish')


def read_claim_truth(path=None,now=None):
    """Backend reader contract: a dead publisher can never remain FRESH forever."""
    now=time.time() if now is None else now
    path=Path(path) if path is not None else CLOSURE_OUTPUT/'truth-projection/backend-truth.json'
    try:data=json.loads(path.read_text())
    except (OSError,ValueError) as error:
        return dict(observed_at=None,generation=None,freshness='STALE',read_error=repr(error),published_at=now)
    for stage,value in data.get('stages',{}).items():
        if value.get('freshness')!='FRESH' or now-value.get('observed_at',0)>data.get('freshness_ttl_seconds',1.5):
            value.update(freshness='STALE',scope='LAST_KNOWN');data['freshness']='STALE'
            for key in ('REGISTERED_PENDING','ACTIONABLE_READY','RUNNING'):data[stage.upper()+'_'+key]=None
    return data
