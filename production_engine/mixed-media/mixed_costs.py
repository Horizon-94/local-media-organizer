"""Incremental external telemetry summary; never a media-ledger write."""
import collections,json,time
from pathlib import Path

class PreparationCosts:
    def __init__(self,kinds=None):
        self.offsets={};self.samples=collections.defaultdict(lambda:collections.deque(maxlen=1000))
        self.counts=collections.Counter();self.stale=collections.Counter()
        self.kinds=kinds or {}
    def update(self,root):
        from mixed_observer import save
        root=Path(root)
        for filename in ('index-proposals.jsonl','image-events.jsonl'):
            path=root/filename
            if not path.exists():continue
            offset=self.offsets.get(filename,0)
            if path.stat().st_size<offset:offset=0
            with path.open('rb') as f:
                f.seek(offset);data=f.read(2*1024**2)
            end=data.rfind(b'\n')
            if end<0:continue
            self.offsets[filename]=offset+end+1
            for line in data[:end].splitlines():
                try:v=json.loads(line)
                except ValueError:continue
                if filename=='image-events.jsonl':
                    ms=v.get('preview',{}).get('total_task_elapsed_seconds')
                    if ms is not None:self.samples['preview'].append(ms*1000);self.counts['preview']+=1
                    continue
                stage=v.get('stage','')
                group='index' if stage=='consume_batch:fusion-index' else 'documents' if stage=='consume_batch:video-stream-documents-v1' else 'closure' if 'closure' in stage or stage=='final_projection' else None
                if not group:continue
                self.counts[group]+=1;self.stale[group]+=v.get('result')=='stale'
                if v.get('total_ms') is not None:self.samples[group].append(v['total_ms']/max(1,v.get('batch_size') or 1))
                if group=='closure' and v.get('total_ms') is not None:
                    key='closure:'+self.kinds.get(v.get('source_id'),'unknown')+':'+stage
                    self.counts[key]+=1;self.stale[key]+=v.get('result')=='stale';self.samples[key].append(v['total_ms'])
        values={}
        for group,samples in self.samples.items():
            s=sorted(samples);n=len(s)
            values[group]={'samples':self.counts[group],'mean_ms_per_item':sum(s)/n,'P95_ms_per_item':s[int((n-1)*.95)],'stale_rate':self.stale[group]/max(1,self.counts[group])}
        result={'at':time.time(),'scope':'OBSERVED_EXTERNAL_SERVICE','stages':values,'trace_offsets':self.offsets}
        save(root/'preparation-costs.json',result);return result
