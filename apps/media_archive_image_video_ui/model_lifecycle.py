"""Dependency-aware residency, external telemetry only (no workflow tables).

The caller supplies authoritative sealed dependency facts. Empty queues are
never a drain proof. Resume reconstructs a NEW manager/instances from ledger.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json,threading,time
from pathlib import Path
from .video_telemetry import append

class LateModelWork(RuntimeError):pass

@dataclass(frozen=True)
class DrainProof:
    upstream_sealed:bool
    future_work_possible:bool
    pending:int=0
    running:int=0
    retry_pending:int=0
    unpublished:int=0
    downstream_safe:bool=False
    reason:str='upstream_not_sealed'
    @property
    def eligible(self):
        return (self.upstream_sealed and not self.future_work_possible and self.downstream_safe
                and self.pending==self.running==self.retry_pending==self.unpublished==0)

class ManagedModel:
    """One physical instance. A consumer, never a timer, owns inference.

    No inherited Resident.idle timeout: dependency proof decides residency.
    Closing a retired worker is distinct from declaring the stage drained.
    """
    def __init__(self,manager,stage,worker_id,instance):
        self.manager=manager;self.stage=stage;self.worker_id=worker_id;self.instance=instance
        self.lock=threading.RLock();self.lease_lock=threading.RLock();self.state='NOT_RESIDENT';self.resident=False
        self.active=0;self.loading=False;self.current=[];self.cold_start_ms=None
        self.owned=0;self.resident_since=None;self.pause_since=None
        self.unloaded_at=None;self.reloaded_at=None;self.cycles=[];self.cooldown=False;self.first_post_reload=False
    @property
    def service_time_unknown(self):return getattr(self.instance,'service_time_unknown',False)
    def prepare(self):
        self.manager.allow_work(self.stage)
        with self.lock:
            if self.resident:return
            if self.state=='UNLOADING':raise LateModelWork('MODEL_UNLOAD_RACE')
            self.manager.begin_reload(self)
            self.state='LOADING';self.loading=True
        start=time.monotonic()
        self.manager.event('model_load_started',stage=self.stage,worker_id=self.worker_id)
        try:
            prepare=getattr(self.instance,'prepare',None)
            if prepare:prepare()
            self.cold_start_ms=(time.monotonic()-start)*1000
            self.resident=True;self.state='IDLE'
            self.resident_since=self.manager.clock()
            if self.cooldown:
                self.reloaded_at=self.resident_since;self.cooldown=False;self.first_post_reload=True
                self.manager.event('RELOAD_READY',stage=self.stage,worker_id=self.worker_id)
                if self.owned:
                    self.first_post_reload=False
                    self.manager.event('FIRST_POST_RELOAD_CLAIM',stage=self.stage,worker_id=self.worker_id,claim_already_owned=True)
            self.manager.event('model_loaded',stage=self.stage,worker_id=self.worker_id,pid=self.pid,load_ms=self.cold_start_ms)
        except BaseException as error:
            self.state='NOT_RESIDENT';self.resident=False
            self.manager.event('model_load_failed',stage=self.stage,worker_id=self.worker_id,error_type=type(error).__name__,reason=str(error))
            raise
        finally:self.loading=False
    @contextmanager
    def work_lease(self):
        # Consumer holds this across admission, claim, prepare and durable commit.
        with self.lease_lock:
            self.owned+=1
            try:yield
            finally:
                self.owned-=1
                governor=getattr(self.manager,'governor',None)
                if governor:governor.completed(self.stage)
    def claimed(self,job):
        governor=getattr(self.manager,'governor',None)
        if governor:governor.claimed(self.stage)
        if self.first_post_reload:
            self.first_post_reload=False
            self.manager.event('FIRST_POST_RELOAD_CLAIM',stage=self.stage,worker_id=self.worker_id,job_id=job['job_id'])
        self.manager.event('model_claim_owned',stage=self.stage,worker_id=self.worker_id,
                           job_id=job['job_id'],source_id=job['source_id'],attempt=job['attempts'])
    def _call(self,payloads,batch):
        self.prepare();start=time.time()
        details=[self.manager.describe(self.stage,p) for p in payloads]
        with self.lock:self.active+=1;self.state='ACTIVE';self.current=details
        self.manager.inference(self,True,start,details)
        result='RETURNED_NOT_YET_VALIDATED';error=None
        try:
            return self.instance.batch(payloads) if batch else self.instance(payloads[0])
        except BaseException as failure:
            result='EXCEPTION';error={'type':type(failure).__name__,'message':str(failure)};raise
        finally:
            end=time.time()
            with self.lock:self.active-=1;self.state='IDLE';self.current=[]
            self.manager.inference(self,False,end,details,service_ms=(end-start)*1000,
                                   result=result,error=error)
    def __call__(self,payload):return self._call([payload],False)
    def batch(self,payloads):return self._call(payloads,True)
    def idle(self):pass
    def unload(self,reason):
        with self.lock:
            if self.active or self.loading or self.owned:return False
            if not self.resident:return True
            self.state='UNLOADING'
        before=self.footprint();pid=self.pid;start=time.time()
        close=getattr(self.instance,'close',None)
        if close:close()
        self.resident=False;self.state='NOT_RESIDENT'
        self.manager.event('model_unloaded',stage=self.stage,worker_id=self.worker_id,pid=pid,reason=reason,
            timestamp=start,unload_ms=(time.time()-start)*1000,footprint_before=before,
            footprint_after=None,measurement_note='process exit does not prove immediate system reclaim')
        return True
    def footprint(self):
        resident=getattr(self.instance,'handler',None)
        monitor=getattr(self.instance,'monitor',None)
        identity=getattr(resident,'resource_worker_id',None)
        record=monitor.workers.get(identity,{}) if monitor and identity else {}
        if record.get('state')=='CLOSED' or time.time()-(record.get('sampled_at') or 0)>15:return None
        return record.get('phys_footprint')
    @property
    def pid(self):return getattr(getattr(getattr(self.instance,'handler',None),'process',None),'pid',None)
    def snapshot(self):
        with self.lock:return {'worker_id':self.worker_id,'pid':self.pid,'stage':self.stage,'state':self.state,
            'resident':self.resident,'active':self.active,'current':list(self.current),
            'cold_start_ms':self.cold_start_ms,'phys_footprint':self.footprint(),
            'owned':self.owned,'pause_since':self.pause_since,'resident_since':self.resident_since,
            'resource_cooldown':self.cooldown,'unloaded_at':self.unloaded_at,
            'reloaded_at':self.reloaded_at,'cycles_10min':len(self.cycles)}
    def close(self):
        if not self.unload('runtime_shutdown'):raise RuntimeError('MODEL_STILL_ACTIVE')

class ModelLifecycleManager:
    def __init__(self,path,describe=None,on_violation=None):
        self.path=Path(path);self.describe=describe or (lambda stage,p:dict(candidate_id=p.get('candidate_id'),attempt=p.get('_wave_attempt')))
        self.on_violation=on_violation;self.models={};self.proofs={};self.drained=set();self.lock=threading.RLock()
        self.history=[];self.overlap_started=None;self.overlap_seconds=0
        self.active_qwen=set();self.unloaded=[]
        self.resource_context={}
        self.last_unload_at=0
        self.clock=time.monotonic;self.governor=None
        self.resource_lock=threading.Lock();self.last_resource_unload=None;self.last_resource_reload=None
        self.recovery_reason=None
    def event(self,event,**values):
        if event=='model_unloaded':self.last_unload_at=time.time()
        record={'timestamp':time.time(),'event':event,'resources':dict(self.resource_context),**values}
        append(self.path,record)
        with self.lock:self.history.append(record);self.history=self.history[-1000:]
    def register(self,stage,worker_id,instance):
        with self.lock:
            if worker_id in self.models:raise ValueError('DUPLICATE_MODEL_WORKER')
            model=ManagedModel(self,stage,worker_id,instance);self.models[worker_id]=model;return model
    def allow_work(self,stage):
        with self.lock:invalid=stage in self.drained
        if invalid:
            message='UNEXPECTED_LATE_MODEL_WORK:'+stage
            self.event('invariant_violation',stage=stage,reason=message)
            if self.on_violation:self.on_violation(message)
            raise LateModelWork(message)
    def can_model_drain(self,stage):return self.proofs.get(stage,DrainProof(False,True)).eligible
    def can_model_unload(self,stage):
        with self.lock:models=list(self.models.values())
        return self.can_model_drain(stage) and all(not m.active and not m.loading for m in models if m.stage==stage)
    def mark_stage_drained(self,stage):
        if not self.can_model_drain(stage):return False
        if stage not in self.drained:
            self.drained.add(stage);self.event('stage_drained',stage=stage,proof=self.proofs[stage].__dict__)
        return True
    def update(self,proofs):
        self.proofs=dict(proofs)
        for stage in proofs:
            proof=proofs[stage]
            if stage in self.drained and (proof.future_work_possible or proof.pending or proof.running or proof.retry_pending or proof.unpublished):
                message='UNEXPECTED_LATE_MODEL_WORK:'+stage
                self.event('invariant_violation',stage=stage,reason=message,proof=proof.__dict__)
                if self.on_violation:self.on_violation(message)
                raise LateModelWork(message)
            if not self.can_model_unload(stage):continue
            self.mark_stage_drained(stage)
            with self.lock:models=list(self.models.values())
            for model in models:
                if model.stage==stage and model.resident and model.unload('DEPENDENCY_DRAINED'):
                    if stage not in self.unloaded:self.unloaded.append(stage)
    def resource_tick(self,governor):
        """Nonblocking lease arbitration; no queue mutations or dependency seals."""
        self.governor=governor
        if not self.resource_lock.acquire(False):return
        try:
            now=self.clock();eligible=[];critical=False
            for model in list(self.models.values()):
                # Resource retirement is temporary, not a dependency drain.
                # An open upstream must remain allowed to publish future work.
                idle_check=getattr(self,'idle_resource_check',None)
                idle_pressure=(getattr(governor,'valid',False) and governor.tier>=2
                               and model.stage in ('qwen','yoloe','openclip','insightface','ocr','embedding')
                               and idle_check is not None and idle_check(model.stage))
                # A resource-paused model may have pending work: retire only its
                # idle instance, never drain its lane or discard durable tasks.
                # T1 still pauses embedding: do not reset its continuous pause
                # clock on T2 -> T1 -> T2. Retirement remains restricted to T2+.
                resource_paused=(getattr(governor,'valid',False)
                                 and model.stage in governor.PAUSED[governor.tier])
                if model.stage not in ('qwen','whisper') and not idle_pressure and not resource_paused:
                    model.pause_since=None
                    continue
                paused=(model.stage in ('qwen','whisper') and model.stage in governor.PAUSED[governor.tier]) or idle_pressure or resource_paused
                # A stale tier may outlive the pressure while hysteresis recovers.
                # Keep claims paused, but do not retire Whisper during recovery.
                recovering=(model.stage=='whisper' and getattr(governor,'valid',False)
                            and 'reload' in governor.windows)
                critical=(model.stage=='whisper' and getattr(governor,'valid',False) and governor.sample
                          and governor.sample['memory_pressure_level']==4)
                if recovering:paused=False
                # A permitted warm Whisper probe with durable work is an
                # execution opportunity, not a continuously paused lane.
                governor_reason=getattr(governor,'reason_for',None)
                if (model.stage=='whisper' and paused and model.resident and idle_check is not None
                        and governor_reason is not None
                        and governor_reason('whisper',resident=True) is None
                        and not idle_check('whisper')):
                    paused=False
                if not paused:model.pause_since=None
                elif model.pause_since is None:
                    model.pause_since=now
                    self.event('PAUSE_START',stage=model.stage,worker_id=model.worker_id)
                model.cycles=[t for t in model.cycles if now-t<600]
                if (paused and governor.tier>=2 and model.pause_since is not None and (critical or now-model.pause_since>=120)
                        and model.resident and model.resident_since is not None
                        and (critical or now-model.resident_since>=120)
                        and (critical or model.unloaded_at is None or now-model.unloaded_at>=120)):
                    eligible.append(model)
            urgent=any(m.stage=='whisper' for m in eligible) and getattr(governor,'valid',False) and governor.sample and governor.sample['memory_pressure_level']==4
            if not urgent and self.last_resource_unload is not None and now-self.last_resource_unload<30:return
            eligible.sort(key=lambda m:(0 if urgent and m.stage=='whisper' else 1,m.footprint() is None,-(m.footprint() or 0),0 if m.stage=='qwen' else 1))
            for model in eligible:
                if not (urgent and model.stage=='whisper') and len(model.cycles)>=2:
                    self.recovery_reason='COOLDOWN_EXHAUSTED';continue
                if not model.lease_lock.acquire(False):continue
                try:
                    if model.owned or model.active or model.loading or model.state!='IDLE':continue
                    check=getattr(self,'quiescent_check',None)
                    if check is None or not check(model.stage):continue
                    # close() is terminal for Resident. Recreate the same provider
                    # class/config; preserve the GuardedWhisper wrapper and history.
                    wrapper=model.instance
                    native=vars(wrapper).get('resident',wrapper)
                    if not all(hasattr(native,k) for k in ('stage','config','monitor')):
                        self.recovery_reason='RESOURCE_RELOAD_PROVIDER_UNSUPPORTED';continue
                    replacement=type(native)(native.stage,native.config,native.monitor)
                    if not model.unload('RESOURCE_COOLDOWN'):continue
                    if native is wrapper:model.instance=replacement
                    else:wrapper.resident=replacement
                    model.cooldown=True;model.unloaded_at=now;model.cycles.append(now)
                    self.last_resource_unload=now
                    self.event('MODEL_UNLOAD',stage=model.stage,worker_id=model.worker_id,reason='RESOURCE_COOLDOWN')
                    break
                finally:model.lease_lock.release()
            if governor.critical_since is not None and governor.last_timestamp-governor.critical_since>=60:
                self.recovery_reason='RESOURCE_RECOVERY_EXTERNAL_PRESSURE'
        finally:self.resource_lock.release()

    def resource_reason(self,stage):
        now=self.clock()
        for model in list(self.models.values()):
            if model.stage!=stage or not model.cooldown:continue
            if model.stage in self.drained:return 'STAGE_DRAINED'
            if now-model.unloaded_at<60:return 'MINIMUM_UNLOAD_DURATION'
            if model.reloaded_at is not None and now-model.reloaded_at<120:return 'RELOAD_COOLDOWN'
            if any(m.loading for m in self.models.values()):return 'RESOURCE_RELOAD_IN_PROGRESS'
            if not self.governor.reload_ready():return 'RESOURCE_RELOAD_WAIT_STABLE_NORMAL'
            if self.last_resource_reload is not None and now-self.last_resource_reload<30:return 'RESOURCE_RELOAD_SERIAL_COOLDOWN'
        return None

    def begin_reload(self,model):
        if not model.cooldown:return
        with self.resource_lock:
            reason=self.resource_reason(model.stage)
            if reason:raise LateModelWork(reason)
            self.last_resource_reload=self.clock()
            with self.governor.lock:self.governor.windows.pop('reload',None)
            self.event('MODEL_RELOAD',stage=model.stage,worker_id=model.worker_id)

    def inference(self,model,starting,timestamp,details,**values):
        if model.stage=='qwen':
            with self.lock:
                if starting:self.active_qwen.add(model.worker_id)
                else:self.active_qwen.discard(model.worker_id)
                if len(self.active_qwen)>1 and self.overlap_started is None:self.overlap_started=timestamp
                if len(self.active_qwen)<2 and self.overlap_started is not None:
                    self.overlap_seconds+=timestamp-self.overlap_started;self.overlap_started=None
        self.event('inference_start' if starting else 'inference_end',stage=model.stage,
                   worker_id=model.worker_id,pid=model.pid,items=details,**values)
    def snapshot(self):
        with self.lock:models=list(self.models.items())
        return {'recovery_reason':self.recovery_reason,'models':{key:model.snapshot() for key,model in models},
            'stages':{stage:{'drained':stage in self.drained,'state':'DRAINED' if stage in self.drained else 'DRAINING' if proof.upstream_sealed else 'OPEN',
                              'future_work_possible':proof.future_work_possible,
                              'proof':proof.__dict__} for stage,proof in self.proofs.items()},
            'models_unloaded':list(self.unloaded),'dual_overlap_seconds':self.overlap_seconds+
                (time.time()-self.overlap_started if self.overlap_started is not None else 0)}

def image_drain_proofs(runtime):
    """Proof from immutable discovery + actual published/canonical work.

    Qwen/OCR events AND FinalProjection create embedding inputs. Projection's
    peer/catalog-dependent tail cannot be inferred from empty model queues.
    Conservatively require local source closure to seal this final producer.
    """
    stages=('yoloe','openclip','insightface','qwen','ocr','embedding')
    with runtime.ledger.connection() as c:
        c.execute('BEGIN')  # coherent short READ snapshot, never a writer lock
        source_states={}
        for source,state in c.execute('SELECT source_id,state FROM video_stream_source_states_v1'):
            source_states.setdefault(source,set()).add(state)
        registered={r[0] for r in c.execute('SELECT source_id FROM image_sources_v1')}
        front_sealed=(registered==set(runtime.image_rows) and all(
            not decision['should_generate_preview'] or bool(source_states.get(sid,set())&{'IMAGE_PREVIEW_READY','SOURCE_FAILED_FINAL'})
            for sid,decision in runtime.decisions.items()))
        selections_sealed=front_sealed and all(not d['should_generate_preview'] or bool(source_states.get(sid,set())&{'SELECTION_DONE','SOURCE_FAILED_FINAL'}) for sid,d in runtime.decisions.items())
        counts={stage:{'pending':0,'running':0,'retry_pending':0} for stage in stages}
        for stage,status,n in c.execute("SELECT stage,status,count(*) FROM video_stream_jobs_v1 GROUP BY stage,status"):
            if stage in counts and status in ('PENDING','RUNNING','RETRY_PENDING'):counts[stage][status.lower()]=n
        # Canonical unfinished facts also block drain even if a native mirror
        # exists. This is conservative during inference and fails closed for
        # an inconsistent/recoverable mirror; queue emptiness cannot hide it.
        unpublished={s:c.execute("SELECT count(*) FROM v2_jobs WHERE stage=? AND status IN ('PENDING','RUNNING','FAILED_RETRYABLE')",(s,)).fetchone()[0] for s in stages}
        document_events=c.execute("SELECT count(*) FROM v2_outbox o WHERE o.event_type='EVIDENCE_READY' AND json_extract(o.payload,'$.payload.stage') IN ('qwen','ocr','embedding') AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=o.event_id)").fetchone()[0]
        ready_documents=c.execute("SELECT count(*) FROM v2_documents WHERE status='READY'").fetchone()[0]
        branch_terminal=selections_sealed and all(not any(counts[s].values()) and unpublished[s]==0 for s in ('qwen','ocr'))
        projection_producers_sealed=front_sealed and all(bool(source_states.get(sid,set())&{'SOURCE_COMPLETE','SOURCE_FAILED_FINAL'}) for sid in runtime.image_rows)
    result={}
    for stage in stages:
        sealed=front_sealed if stage in ('yoloe','openclip','insightface') else selections_sealed
        if stage=='embedding':sealed=branch_terminal and projection_producers_sealed and document_events==0 and ready_documents==0
        result[stage]=DrainProof(sealed,not sealed,**counts[stage],unpublished=unpublished[stage],downstream_safe=sealed,
                                reason='all_legal_input_paths_sealed' if sealed else 'legal_upstream_work_possible')
    return result
