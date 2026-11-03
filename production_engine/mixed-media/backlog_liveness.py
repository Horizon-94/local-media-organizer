"""Completion-counter liveness watchdog. Never Stop/restart or bypass admission."""
import time,json

def observe(runtime,value,threshold=60,clock=time.monotonic):
    done=tuple(sorted((stage,n) for stage,status,n in value['counts'] if status=='DONE'))
    signature=(done,len(value['closure_pending']))
    now=clock();old=getattr(runtime,'_backlog_progress',None)
    if old is None or old['signature']!=signature:
        runtime._backlog_progress=dict(signature=signature,since=now,emitted=False);return
    pending=sum(value['model_remaining'].values())+len(value['closure_pending'])
    if not pending or now-old['since']<threshold or old['emitted']:return
    active=[stage for stage,status,n in value['counts'] if status=='RUNNING' and n]
    reasons=dict(getattr(runtime,'blocked',{}))
    if getattr(runtime.budget,'blocked_reason',None):reasons['visual_producer']=runtime.budget.blocked_reason
    audio=getattr(runtime,'audio_gate_snapshot',{})
    producer=getattr(runtime,'audio_producer_snapshot',{})
    if audio.get('paused') and producer.get('state')!='NO_WORK':reasons['audio_producer']='AUDIO_HIGH_WATERMARK'
    first=getattr(runtime,'_first_selection_ready_ids',set())
    supply=next((json.loads(v) for k,v in getattr(runtime.ledger,'_build151_trace_cache',{}).items() if k[:2]==('QWEN_PENDING_DECISION','SUPPLY_SUMMARY')),None)
    row=dict(event='BACKLOG_STALL_WATCHDOG',timestamp=time.time(),no_progress_seconds=now-old['since'],durable_backlog=pending,active_stages=active,blocked_stages=reasons,spool_blocked_reason=getattr(runtime.budget,'blocked_reason',None),audio_supply=dict(last_capacity_evaluation=audio,producer=producer),raw_preparation=getattr(runtime,'raw_preparation_snapshot',None),qwen_supply_last_changed=supply,first_selection_backlog=len(first),counts=value['counts'],closure_pending=len(value['closure_pending']),classification='NO_COMPLETION_PROGRESS_REEVALUATING',action='EXISTING_LANES_CONTINUE_REEVALUATION',global_stall_proven=False)
    try:
        with (runtime.root/'backlog-liveness.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    except OSError:return
    old['emitted']=True
    # Existing dispatch/coordination loops continue re-evaluating their gates.
    # No input key, lease, resource policy or source identity is modified here.
