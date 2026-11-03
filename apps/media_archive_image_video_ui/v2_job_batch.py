"""Bounded inference batching; existing per-job claims/results/events stay atomic."""
import json
import threading
from .v2_runtime import ContractError,LostLease,SupersededCompletion


def run_batch(store,stage,handler,validator,*,batch_size,owner='worker',lease_seconds=120,
              execution_contract=None,on_claim=None,should_stop=None,on_start=None,on_finish=None):
    if type(batch_size) is not int or not 1<=batch_size<=8:raise ContractError('invalid_true_batch_size')
    if lease_seconds<=0:raise ContractError('invalid_lease')
    jobs=[];stop=threading.Event()
    def renew():
        while not stop.wait(lease_seconds/3):
            for job in list(jobs):
                if not store.heartbeat(job['execution_key'],job['token'],lease_seconds):return
    thread=threading.Thread(target=renew,daemon=True);thread.start()
    try:
        for _ in range(batch_size):
            if should_stop and should_stop():break
            job=store.claim(stage,owner,lease_seconds,execution_contract=execution_contract)
            if not job:break
            jobs.append(job)
            if on_claim:
                try:on_claim(job)
                except Exception as exc:
                    for claimed in jobs:store.fail(claimed,exc)
                    raise
        if not jobs:return 0
        try:
            if on_start:on_start(jobs)
            try:results=handler([json.loads(j['payload']) for j in jobs])
            finally:
                if on_finish:on_finish(jobs)
            if not isinstance(results,list) or len(results)!=len(jobs):raise ContractError('batch_result_cardinality_mismatch')
        except LostLease:raise
        except Exception as exc:
            for job in jobs:store.fail(job,exc)
            return len(jobs)
        completed=0
        for job,result in zip(jobs,results):
            try:store.complete(job,result,validator);completed+=1
            except SupersededCompletion:continue
            except LostLease:raise
            except Exception as exc:store.fail(job,exc);completed+=1
        return completed
    finally:stop.set();thread.join()
