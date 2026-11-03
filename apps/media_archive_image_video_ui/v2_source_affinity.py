"""Bounded scheduling hints; never evidence/progress authority."""
import threading

VISUAL=('yoloe','openclip','dhash','geometry','quality')


class SourceAffinity:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock();self.remaining={};self.turns={};self.completed_keys=set()
        with store.connect() as c:
            c.execute('CREATE INDEX IF NOT EXISTS v2_jobs_stage_source_claim ON v2_jobs(stage,source_id,status,item_id,revision)')

    def admit(self,source,revision):
        # Once per sealed producer revision, not a whole-project scan per job.
        with self.store.connect() as c:
            n=c.execute("SELECT count(*) FROM v2_artifacts WHERE source_id=? AND revision=? AND status='READY'",(source,revision)).fetchone()[0]
            from .v2_source_lineage import current_source_rows
            rows=current_source_rows(c,source,revision,"j.status='DONE' AND j.stage IN ('yoloe','openclip','dhash','geometry','quality')",columns='j.execution_key,j.status,j.stage')
            done={stage:sum(r['stage']==stage for r in rows) for stage in VISUAL}
        with self.lock:
            counts={stage:max(0,n-done.get(stage,0)) for stage in VISUAL}
            if any(counts.values()):self.remaining[source]=(revision,counts)
            else:self.remaining.pop(source,None)

    def preferred(self,stage):
        if stage not in VISUAL:return None
        with self.lock:
            # One original-order grant every ninth claim bounds interference
            # with the unchanged queue. Stage fairness remains independent.
            if self.turns.get(stage,0)%9==8:return None
            choices=[(sum(counts.values()),sid) for sid,(_,counts) in self.remaining.items() if counts[stage]>0]
            return min(choices)[1] if choices else None

    def claimed(self,job,preferred=None):
        if job['stage'] in VISUAL:
            with self.lock:
                turn=self.turns.get(job['stage'],0)+1;self.turns[job['stage']]=turn
            perf=getattr(self.store,'performance',None)
            if perf:perf.count('SOURCE_AFFINITY_FOCUSED_CLAIMS' if preferred==job['source_id'] else 'SOURCE_AFFINITY_ORIGINAL_ORDER_CLAIMS')
            return turn

    def completed(self,job):
        if job['stage'] not in VISUAL:return
        with self.lock:
            if job['execution_key'] in self.completed_keys:return
            self.completed_keys.add(job['execution_key'])
            entry=self.remaining.get(job['source_id'])
            if entry and entry[0]==job['source_revision']:
                counts=entry[1];counts[job['stage']]=max(0,counts[job['stage']]-1)
                if not any(counts.values()):self.remaining.pop(job['source_id'],None)


def source_advancement_order(admitted,cursor,hint,prefer):
    """One hinted source before the unchanged rotating fair order.

    A hint never advances the global cursor. Every fourth scheduler quantum
    disables preference, so global work still receives bounded opportunities.
    Revision identity and unfinished outcome are required; no evidence bypass.
    """
    count=len(admitted)
    positions=[(cursor+n)%count for n in range(count)]
    preferred=next((p for p in positions if prefer and hint
        and (admitted[p]['source_id'],admitted[p]['source_revision'])==hint
        and (admitted[p].get('outcome') or {}).get('status') not in ('COMPLETE','DEGRADED')),None)
    if preferred is None:return [(p,True) for p in positions]
    return [(preferred,False)]+[(p,True) for p in positions if p!=preferred]
