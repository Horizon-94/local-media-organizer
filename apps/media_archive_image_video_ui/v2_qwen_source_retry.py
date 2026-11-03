"""Single-source output-contract retry; no whole-project/audio completion gate."""
import json
from pathlib import Path

def reconcile_retry_source(store,key,request_id):
    """Retire an obsolete failure outcome, never invent a completion proof."""
    with store.connect() as c:
        job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(key,)).fetchone()
        if not job or job['stage']!='qwen' or job['status'] not in ('PENDING','DONE'):
            raise ValueError('retry_source_requires_current_qwen_revision')
        store.assert_source_epoch(c,job['source_id'],job['source_revision'])
        outcome=c.execute('SELECT status,payload FROM v2_source_outcomes WHERE source_id=? AND source_revision=?',(job['source_id'],job['source_revision'])).fetchone()
        if not outcome or outcome['status'] not in ('FAILED','FAILED_FINAL'):return False
        old=json.loads(outcome['payload'])
        failed_key=old.get('execution_key')
        failed=c.execute('SELECT stage,item_id,status FROM v2_jobs WHERE execution_key=?',(failed_key,)).fetchone()
        if not failed or failed['stage']!='qwen' or failed['item_id']!=job['item_id'] or failed['status']!='SUPERSEDED':return False
        store.emit(c,'SOURCE_QWEN_FAILURE_SUPERSEDED',job['source_id'],job['source_revision'],job['revision'],{'request_id':request_id,'previous_outcome':old,'new_execution_key':key})
        c.execute("UPDATE v2_source_outcomes SET status='RECOVERING',reason_code='qwen_retry_revision',execution_key=?,payload=? WHERE source_id=? AND source_revision=?",(key,json.dumps({'status':'RECOVERING','request_id':request_id,'execution_key':key}),job['source_id'],job['source_revision']))
        c.execute("UPDATE v2_product_sources SET state='INGESTED',error=NULL WHERE scheduler_run_id=? AND source_id=? AND source_revision=?",(store.scheduler_run_id,job['source_id'],job['source_revision']))
        return True

def retry_on_recoverable_run(task, plan, *, factory_builder=None, memory_provider=None):
    from .scheduler_identity import load_scheduler_run,resume_scheduler_run,mark_scheduler_run_recoverable,EVENT_DRIVEN_V2
    from .v2_product_lifecycle import ProductExecutionLease,ProductLifecycle
    from .scheduler_switching import FencedWorkerStore
    from .storage_compute_scheduler import StorageComputeScheduler
    from .v2_completed_candidate_repair import enqueue_failed_qwen_contract_retry
    from .v2_real_handlers import build_handler_factory
    from .v2_runtime import ContractError
    db=Path(task['database']);rid=task['scheduler_run_id']
    with ProductExecutionLease(db,rid) as execution_lease:
        run=load_scheduler_run(db,rid)
        if run['status']!='RECOVERABLE':raise ContractError('single_qwen_retry_requires_safe_handoff')
        run=resume_scheduler_run(db,rid,scheduler_mode=EVENT_DRIVEN_V2,execution_epoch=run['execution_epoch'])
        store=FencedWorkerStore(db,task_id=run['task_id'],scheduler_run_id=rid,execution_epoch=run['execution_epoch'])
        store.targeted_projection_invalidation_enabled=True
        lifecycle=ProductLifecycle(store,[],resume_handoff=True,execution_lease=execution_lease)
        factory=None;scheduler=None;admission=None;handler=None
        try:
            key=enqueue_failed_qwen_contract_retry(db,run,plan)
            reconcile_retry_source(store,key,plan['retry_request_id'])
            job=plan['retry_job']
            with store.connect() as c:
                current=c.execute('SELECT status,result_sha FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
                if current['status']=='DONE':
                    return {'status':'PASS','source_id':job['source_id'],'execution_key':key,'already_committed':True,'audio_completion_required':False}
                expected=json.dumps(plan['run_contracts']['qwen'],sort_keys=True)
                if c.execute("SELECT 1 FROM v2_current_jobs WHERE stage='qwen' AND provenance=? AND item_id<>? AND status IN ('PENDING','FAILED_RETRYABLE','RUNNING')",(expected,job['item_id'])).fetchone():
                    raise ContractError('single_qwen_retry_scope_not_unique')
                c.execute("UPDATE v2_product_sources SET state='INGESTED',error=NULL WHERE scheduler_run_id=? AND source_id=? AND source_revision=?",(rid,job['source_id'],job['source_revision']))
                store.emit(c,'SOURCE_QWEN_RETRY_CONTRACT',job['source_id'],job['source_revision'],plan['run_contracts']['qwen']['revision'],{'request_id':plan['retry_request_id'],'config':plan['run_handler_configs']['qwen'],'previous_execution_key':job['execution_key'],'execution_key':key,'scope':[job['source_id']]})
            from .v2_resource_budget import CachedSystemMemory
            scheduler=StorageComputeScheduler(db,pressure_provider=memory_provider or CachedSystemMemory())
            scheduler.switch_owner(task['task_id'],rid,run['execution_epoch'])
            admission=scheduler.acquire(task['task_id'],'qwen',scheduler_run_id=rid,execution_epoch=run['execution_epoch'],from_derivative=True,job_type='qwen',job_id=key)
            factory=(factory_builder or build_handler_factory)(task['workspace'],plan['run_handler_configs'])
            handler,validator=factory('qwen',None,task)
            if not store.run_one('qwen',handler,validator,owner='single-source-authorized-retry',execution_contract=plan['run_contracts']['qwen']):
                raise ContractError('single_qwen_retry_not_runnable')
            with store.connect() as c:
                result=dict(c.execute('SELECT status,result_sha,error,attempts FROM v2_jobs WHERE execution_key=?',(key,)).fetchone())
            return {'status':'PASS' if result['status']=='DONE' else 'FAIL','source_id':job['source_id'],'execution_key':key,'result':result,'downstream':'queued for normal source-local reconciliation','audio_completion_required':False}
        finally:
            try:
                if admission is not None:scheduler.release(admission,reason='single_source_retry_committed')
                if factory is not None:
                    if handler is not None:factory.release(handler)
                    factory.close()
            finally:
                mark_scheduler_run_recoverable(db,rid,scheduler_mode=EVENT_DRIVEN_V2,execution_epoch=run['execution_epoch'])
                lifecycle.close()
