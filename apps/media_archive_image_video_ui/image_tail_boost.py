"""IMAGE-only capacity trial: a second existing HotConsumer, one shared writer.

No multi-image Qwen and no second scheduler. Worker2 owns a separate instance
of the SAME source admission policy; atomic ledger claim and wave fences remain
authoritative. VIDEO profile's hard single-worker setting is untouched.
"""
from dataclasses import dataclass
import copy,math,re,threading,time
from .video_stream_ledger import HotConsumers
from .video_source_admission import SourceAdmission

GIB=1024**3
def worker_faults(errors):
    """A validated content review is a candidate retry, not a dead worker.

    Keep this allowlist narrow: unknown exceptions/control failures still
    retire capacity. Never suppress the durable failed attempt itself.
    """
    return [error for error in errors if not str(error).startswith('ContractError:qwen_requires_review:')]
def available_bytes(vm_stat):
    if not isinstance(vm_stat,str):return None
    page=re.search(r'page size of (\d+) bytes',vm_stat)
    if not page:return None
    values=[]
    for name in ('Pages free','Pages inactive','Pages speculative'):
        match=re.search(re.escape(name)+r':\s*(\d+)',vm_stat)
        if not match:return None
        values.append(int(match[1]))
    return sum(values)*int(page[1])

@dataclass(frozen=True)
class TailPolicy:
    minimum_backlog:int=8
    minimum_single_completions:int=6
    evaluation_seconds:float=120
    minimum_dual_completions:int=8
    minimum_speedup:float=1.10
    second_worker_floor_bytes:int=11*GIB
    system_reserve_bytes:int=2*GIB
    future_embedding_reserve_bytes:int=int(3.5*GIB)
    def eligible(self,*,pending,service_ms,cold_start_ms,single_completions,primary_footprint,
                 available,system,governor,other_drained,dominant,embedding_resident):
        minimum=max(self.minimum_backlog,math.ceil(2*(cold_start_ms or 0)/max(1,service_ms or 1)))
        budget=max(self.second_worker_floor_bytes,int((primary_footprint or 0)*1.25))+self.system_reserve_bytes
        if not embedding_resident:budget+=self.future_embedding_reserve_bytes
        reason=('SMALL_BACKLOG' if pending<minimum else 'NO_SINGLE_BASELINE' if single_completions<self.minimum_single_completions or not service_ms
            else 'NOT_DOMINANT' if not dominant else 'OTHER_MODELS_NOT_DRAINED' if not other_drained
            else 'TELEMETRY_UNKNOWN' if primary_footprint is None or available is None or not system
            else 'TELEMETRY_STALE' if time.time()-system.get('timestamp',0)>15
            else 'PRESSURE_NOT_NORMAL' if system.get('memory_pressure_level')!=1
            else 'GOVERNOR' if governor else 'SWAP_GROWTH' if system.get('sustained_swap_growth') or (system.get('swap_growth_rate_bytes_per_second') or 0)>0
            else 'INSUFFICIENT_UNIFIED_HEADROOM' if available<budget else None)
        return {'eligible':reason is None,'reason':reason or 'QWEN_DOMINANT_HEALTHY_HEADROOM','minimum_backlog':minimum,
                'available_reclaimable_estimate_bytes':available,'reserved_budget_bytes':budget,
                'budget_is_not_a_measured_model_memory':True}

class TailBoost:
    def __init__(self,runtime,factory,policy=None):
        self.runtime=runtime;self.factory=factory;self.policy=policy or TailPolicy()
        self.consumer=None;self.model=None;self.mode='NORMAL';self.trial_done=False
        self.started_at=None;self.ended_at=None;self.baseline_rate=None;self.single=[];self.dual=[]
        self.capacity_retired_at=None
        with runtime.ledger.connection() as c:
            self.seen_done={r[0] for r in c.execute("SELECT job_id FROM video_stream_jobs_v1 WHERE stage='qwen' AND status='DONE'")}
        self.reason='NOT_ELIGIBLE';self.last_decision={};self.speedup=None
        self.result='NOT_ENOUGH_EVIDENCE';self.start_pending=None
    def activate(self,decision,pending,now):
        if not self.factory:return False
        r=self.runtime
        model=r.lifecycle.register('qwen','qwen#2',self.factory())
        ledger=copy.copy(r.ledger)
        ledger.admission=SourceAdmission(r.root/'source-switches-qwen2.jsonl')
        ledger.admission.service.update(r.ledger.admission.service)
        ledger.admission.workers={'qwen':1}
        consumer=HotConsumers(ledger,{'qwen':model},validators={'qwen':r.validator('qwen')},
            capacities={'qwen':4},input_verifier=r.verify_input,
            admission=lambda stage:(getattr(r,'fixture_mode',False) or r.can_admit(stage)) and self.mode=='TAIL_BOOST')
        self.consumer=consumer;self.model=model;self.mode='TAIL_BOOST';self.started_at=now
        r.ledger.admission.workers['qwen']=2
        self.start_pending=pending
        window=[t for t in self.single if t>=now-180]
        self.baseline_rate=(len(window)-1)/(window[-1]-window[0]) if len(window)>1 and window[-1]>window[0] else None
        r.lifecycle.event('tail_boost_started',tail_boost_eligible_at=now,tail_boost_started_at=now,
            activation_reason=decision,pending=pending,worker_count_before=1,worker_count_after=2,
            models_unloaded_before_boost=list(r.lifecycle.unloaded),single_observed_per_min=None if self.baseline_rate is None else self.baseline_rate*60)
        consumer.start();return True
    def deactivate(self,reason,now,pending):
        if self.mode!='TAIL_BOOST':return
        self.mode='DRAINING_WORKER2';self.reason=reason;self.ended_at=now;self.trial_done=True
        # No kill. A claimed inference finishes and durably ACKs as usual.
        self.consumer.stop.set()
        self.runtime.ledger.admission.workers['qwen']=1
        if reason=='WORKER2_ERROR':self.result='TRIAL_INTERRUPTED_WORKER_ERROR'
        elif reason=='RESOURCE_GOVERNOR_OR_SWAP':self.result='REJECTED_RESOURCE_OR_WORKER'
        self.runtime.lifecycle.event('tail_boost_ended',tail_boost_ended_at=now,deactivation_reason=reason,
            qwen_pending_at_start=self.start_pending,qwen_pending_at_end=pending,
            worker_count_before=2,worker_count_after=1,effective_speedup=self.speedup)
    def reap(self):
        if self.mode!='DRAINING_WORKER2' or any(t.is_alive() for t in self.consumer.threads):return
        if self.model.unload('TAIL_CAPACITY_RETIRED'):
            self.mode='NORMAL';self.capacity_retired_at=time.time()
    def tick(self,proofs,system,now=None):
        now=now or time.time();r=self.runtime
        with r.ledger.connection() as c:
            rows=list(c.execute("SELECT j.job_id,j.status,max(a.finished_at) FROM video_stream_jobs_v1 j LEFT JOIN video_stream_attempts_v1 a ON a.job_id=j.job_id WHERE j.stage='qwen' GROUP BY j.job_id"))
        for job,status,finished in rows:
            if status=='DONE' and job not in self.seen_done:
                self.seen_done.add(job)
                (self.dual if self.mode=='TAIL_BOOST' else self.single).append(finished or now)
        pending=proofs['qwen'].pending+proofs['qwen'].retry_pending
        if self.mode=='TAIL_BOOST':
            failure=bool(worker_faults(self.consumer.errors))
            if self.consumer.failure_records:
                r.errors.append('QWEN2_DURABLE_CONTROL_FAILURE');r.stop.set()
            unhealthy=(not system or now-system.get('timestamp',0)>15 or system.get('memory_pressure_level')!=1
                       or r.governor.tier>0 or system.get('sustained_swap_growth') or (system.get('swap_growth_rate_bytes_per_second') or 0)>0)
            required_load=any(m.loading for m in r.lifecycle.models.values() if m.stage!='qwen')
            if failure or unhealthy or required_load or pending<4:
                self.deactivate('WORKER2_ERROR' if failure else 'RESOURCE_GOVERNOR_OR_SWAP' if unhealthy else 'REQUIRED_MODEL_LOADING' if required_load else 'BACKLOG_LOW',now,pending)
            elif now-self.started_at>=self.policy.evaluation_seconds and len(self.dual)>=self.policy.minimum_dual_completions:
                rate=len(self.dual)/(now-self.started_at)
                self.speedup=rate/self.baseline_rate if self.baseline_rate else None
                if self.speedup is not None and self.speedup<self.policy.minimum_speedup:
                    self.result='TAIL_BOOST_NOT_BENEFICIAL';self.deactivate(self.result,now,pending)
                elif self.speedup is not None:self.result='POSITIVE_MEASURED_THROUGHPUT'
            if self.mode=='TAIL_BOOST':self.consumer.refill()
        self.reap()
        if self.mode!='NORMAL' or self.trial_done or not self.factory:return
        primary=r.lifecycle.models['qwen#1'];other_drained=all(stage in r.lifecycle.drained and all(not m.resident for m in r.lifecycle.models.values() if m.stage==stage) for stage in ('yoloe','openclip','insightface','ocr'))
        service=r.ledger.admission.service.get('qwen');other_cost=max((
            (proof.pending+proof.retry_pending)*r.ledger.admission.service.get(stage,0)
            for stage,proof in proofs.items() if stage!='qwen'),default=0)
        embedding=next(m for m in r.lifecycle.models.values() if m.stage=='embedding')
        decision=self.policy.eligible(pending=pending,service_ms=service,cold_start_ms=primary.cold_start_ms,
            single_completions=len(self.single),primary_footprint=primary.footprint(),
            available=available_bytes((system or {}).get('vm_stat')),system=system,governor=r.governor.tier,
            other_drained=other_drained,dominant=bool(service and pending*service>=2*other_cost),
            embedding_resident=embedding.resident)
        if system and system.get('timestamp',0)<r.lifecycle.last_unload_at:
            decision.update(eligible=False,reason='WAIT_FOR_POST_UNLOAD_RESOURCE_SAMPLE')
        if any(m.loading for m in r.lifecycle.models.values() if m.stage!='qwen'):
            decision.update(eligible=False,reason='REQUIRED_MODEL_LOADING')
        self.last_decision=decision;self.reason=decision['reason']
        if decision['eligible']:self.activate(decision,pending,now)
    def snapshot(self):
        duration=(self.ended_at or time.time())-self.started_at if self.started_at else 0
        observed_rate=len(self.dual)/duration if duration else None
        observed_speedup=observed_rate/self.baseline_rate if self.baseline_rate and observed_rate is not None else None
        return {'mode':self.mode,'effective_concurrency':2 if self.mode in ('TAIL_BOOST','DRAINING_WORKER2') else 1,
            'admitted_concurrency':2 if self.mode=='TAIL_BOOST' else 1,
            'started_at':self.started_at,'ended_at':self.ended_at,'reason':self.reason,'decision':self.last_decision,
            'capacity_retired_at':self.capacity_retired_at,
            'single_observed_per_min':self.baseline_rate*60 if self.baseline_rate else None,
            'dual_observed_per_min':observed_rate*60 if observed_rate is not None else None,
            'effective_speedup':self.speedup if self.speedup is not None else observed_speedup,
            'observed_speedup':observed_speedup,'dual_completed_samples':len(self.dual),
            'speedup_evaluation_qualified':self.speedup is not None,
            'result':self.result,'policy':self.policy.__dict__}
    def shutdown(self):
        if self.mode=='TAIL_BOOST':self.deactivate('SHUTDOWN',time.time(),0)
        if self.consumer:
            self.consumer.stop.set()
            for thread in self.consumer.threads:thread.join(timeout=360)
            if any(t.is_alive() for t in self.consumer.threads):raise RuntimeError('QWEN2_STILL_ACTIVE_DO_NOT_RESUME')
            self.model.unload('runtime_shutdown')
        self.mode='NORMAL'
