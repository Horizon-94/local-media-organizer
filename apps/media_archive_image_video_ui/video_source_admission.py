"""Live source ownership: priority admits, never preempts running work."""
import collections,json,threading,time
from pathlib import Path

class SourceAdmission:
    def __init__(self,path=None,clock=time.monotonic,quantum=32,seconds=60):
        self.path=Path(path) if path else None;self.clock=clock;self.quantum=quantum;self.seconds=seconds
        self.active={};self.waiting={};self.lock=threading.RLock();self.service={};self.completion={};self.backlogs={}
        self.blocked={}
        self.workers={}
    def switch(self,stage,source,reason):
        previous=self.active.pop(stage,None)
        if source:self.active[stage]={'source':source,'since':self.clock(),'count':0,'giant':False}
        event={'timestamp':time.time(),'stage':stage,'previous_source':previous['source'] if previous else None,'next_source':source,'reason':reason,'same_source_consecutive_task_count':previous['count'] if previous else 0}
        if self.path:
            from .video_telemetry import append
            append(self.path,event)
    def release(self,stage,reason):
        with self.lock:
            if stage in self.active:self.switch(stage,None,reason)
    def order(self,stage,rows,admit=True):
        with self.lock:
            rows=[r for r in rows if self.blocked.get((stage,r['source_id']),0)<=self.clock()]
            grouped=collections.defaultdict(list)
            for row in rows:grouped[row['source_id']].append(row)
            self.backlogs[stage]=len(rows);now=self.clock();old=self.active.get(stage)
            previous_source=old['source'] if old else None
            for sid in grouped:self.waiting.setdefault((stage,sid),now)
            exhausted=bool(old and old['giant'] and (old['count']>=self.quantum or now-old['since']>=self.seconds))
            if old and (old['source'] not in grouped or exhausted):
                if admit:
                    self.switch(stage,None,'bounded_fairness_quantum' if exhausted else 'stage_complete_or_upstream_blocked');self.waiting[stage,previous_source]=now
                old=None
            if not old and grouped:
                service=self.service.get(stage,1.)
                def score(sid):
                    n=sum(r.get('work_items',1) for r in grouped[sid]);cost=n*service/max(1,self.workers.get(stage,1));age=(now-self.waiting[stage,sid])*100
                    hints=[r.get('legacy_score') for r in grouped[sid] if r.get('legacy_score')]
                    if stage not in self.service and hints:cost=min(hints)
                    retry=any(r.get('status')=='RETRY_PENDING' for r in grouped[sid])
                    return (cost-age-(service if retry else 0)+.1*self.completion.get(sid,0),self.completion.get(sid,0),grouped[sid][0].get('sequence',0))
                choices=list(grouped)
                if exhausted and len(choices)>1:choices=[s for s in choices if s!=previous_source]
                chosen=min(choices,key=score)
                if admit:
                    self.switch(stage,chosen,'eligible_completion_admission')
                    work=sum(r.get('work_items',1) for r in grouped[chosen])
                    self.active[stage]['giant']=work>self.quantum or work*service/max(1,self.workers.get(stage,1))>self.seconds*4000
                    self.waiting.pop((stage,chosen),None);old=self.active[stage]
                else:old={'source':chosen}
            if not old:return []
            source=old['source']
            return sorted(rows,key=lambda r:(r['source_id']!=source,r.get('sequence',0)))
    def backoff(self,stage,source,seconds=.2):
        with self.lock:
            self.blocked[stage,source]=self.clock()+seconds
            self.release(stage,'failure_backoff')
    def claimed(self,stage,source):
        with self.lock:
            current=self.active.get(stage)
            if current and current['source']==source:current['count']+=1
    def snapshot(self):
        with self.lock:return {'active':{k:dict(v) for k,v in self.active.items()},'backlogs':dict(self.backlogs)}
