"""Bound model supply separately from sources awaiting index completion.

These are scheduling hints, never evidence or source completion authority.
"""
MODEL_STAGES=('yoloe','openclip','dhash','geometry','quality','insightface','qwen','ocr','audio_extract','vad','whisper')


def pending_model_sources(c,run_id,epoch=None):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'v2_jobs','v2_active_sources','v2_product_sources'}<=tables:return None
    owner_clause=''
    owner_args=()
    if epoch is not None:
        owner_clause=' AND j.scheduler_run_id=? AND j.execution_epoch=?'
        owner_args=(run_id,epoch)
    return {r[0] for r in c.execute('''SELECT DISTINCT p.source_id FROM v2_product_sources p
        JOIN v2_active_sources a ON a.source_id=p.source_id AND a.source_revision=p.source_revision
        JOIN v2_jobs j ON j.source_id=p.source_id AND j.source_revision=p.source_revision
        WHERE p.scheduler_run_id=? AND p.state='INGESTED'
        AND j.status IN ('PENDING','RUNNING','FAILED_RETRYABLE','FAILED_FINAL')
        AND j.stage IN (SELECT value FROM json_each(?))'''+owner_clause,
        (run_id,__import__('json').dumps(MODEL_STAGES))+owner_args)}


def update_pressure(paused,model_backlog,closure_backlog,*,low=2,target=6,high=12,closure_high=150):
    if not 0<=low<target<=high or closure_high<high:raise ValueError('invalid_resource_producer_watermarks')
    if closure_backlog>=closure_high:return True,'INDEX_BACKPRESSURE'
    if model_backlog>=high:return True,'MODEL_BACKPRESSURE'
    if paused and model_backlog<=low:paused=False
    if not paused and model_backlog>=target:paused=True
    return paused,'MODEL_BACKPRESSURE' if paused else None
