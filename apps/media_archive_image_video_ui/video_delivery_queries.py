"""Measured delivery scans: exact predicates, sparse indexes and commit wakeups."""
import re,threading

def delivery_predicate(partition,alias='o'):
    p=alias+'.' if alias else ''
    stage="json_extract("+p+"payload,'$.payload.stage')"
    audio="("+p+"event_type='AUDIO_ARTIFACT_READY' OR ("+p+"event_type='EVIDENCE_READY' AND "+stage+" IN ('audio_extract','vad')))"
    documents="("+p+"event_type='EVIDENCE_READY' AND "+stage+" IN ('qwen','ocr','whisper','embedding'))"
    return audio if partition=='audio' else documents if partition=='documents' else '('+audio+' OR '+documents+')'

def install(c):
    # TEXT affinity preserves the former comparison against execution_key TEXT.
    # The untyped JSON index was SCANNED once per candidate, not searched.
    c.execute("CREATE INDEX IF NOT EXISTS p0_policy_key_text ON video_stream_jobs_v1(CAST(json_extract(payload,'$._evidence_key') AS TEXT))")
    c.execute("CREATE INDEX IF NOT EXISTS p0_pending_promotion ON v2_jobs(stage,execution_key,source_id,source_revision) WHERE status IN ('PENDING','FAILED_RETRYABLE')")
    for partition in ('audio','documents'):
        c.execute('CREATE INDEX IF NOT EXISTS p0_outbox_'+partition+'_delivery ON v2_outbox(committed_at,event_id) WHERE '+delivery_predicate(partition,''))

_DML=re.compile(r'^(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE(?:\s+INTO)?|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+(?:main\.)?["`]?([A-Za-z_0-9]+)',re.I)
_RELEVANT={'v2_outbox','v2_inbox','v2_jobs','v2_active_sources','video_stream_jobs_v1'}
def affects_delivery(sql):
    match=_DML.match(sql.lstrip())
    return bool(match and match.group(1).lower() in _RELEVANT)

class DeliveryPulse:
    def __init__(self):self.condition=threading.Condition();self.generation=0
    def version(self):
        with self.condition:return self.generation
    def committed(self):
        with self.condition:
            self.generation+=1;self.condition.notify_all()
    def wait(self,previous,stop):
        # Commit-before-wait is not lost. Bounded fallback also observes writes
        # by external owners and Stop; no persistent candidate/result cache.
        with self.condition:
            return self.condition.wait_for(lambda:self.generation!=previous or stop.is_set(),timeout=.25)
