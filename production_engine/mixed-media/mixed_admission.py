"""Admission-only media aging; never interrupts an owned inference/source."""
from apps.media_archive_image_video_ui.video_source_admission import SourceAdmission
import json,time
from pathlib import Path

class MixedAdmission(SourceAdmission):
    def __init__(self,kinds,*args,kind_wait_seconds=60,**kwargs):
        super().__init__(*args,**kwargs)
        self.kinds=dict(kinds);self.kind_wait_seconds=kind_wait_seconds;self.kind_wait={}
        self.ended_stages=set()
        self.folders={};self.folder_active={};self.folder_wait={}
        self.trace_revisions={};self.last_decisions={};self._decision_signatures={}
    def end_stage(self,stage):
        with self.lock:
            self.release(stage,'stage_ended');self.ended_stages.add(stage);self.backlogs[stage]=0
            self.waiting={k:v for k,v in self.waiting.items() if k[0]!=stage}
            self.kind_wait={k:v for k,v in self.kind_wait.items() if k[0]!=stage}
    def order(self,stage,rows,admit=True):
        return self._regular_order(stage,rows,admit)

    def _regular_order(self,stage,rows,admit=True):
        with self.lock:
            now=self.clock()
            original=list(rows);layers={r['source_id']:[] for r in original}
            def record(layer,inputs,outputs,predicate,values):
                kept={r['source_id'] for r in outputs}
                for r in inputs:
                    sid=r['source_id'];layers[sid].append(dict(layer=layer,exact_predicate=predicate,input_value=values(sid),result=sid in kept))
            before=list(rows)
            rows=[r for r in rows if self.blocked.get((stage,r['source_id']),0)<=now]
            record('source_cooldown',before,rows,'blocked_until <= now',lambda sid:dict(blocked_until=self.blocked.get((stage,sid),0),now=now))
            present={self.kinds[r['source_id']] for r in rows}
            for key in [k for k in self.kind_wait if k[0]==stage and k[1] not in present]:self.kind_wait.pop(key)
            for kind in present:self.kind_wait.setdefault((stage,kind),now)
            old=self.active.get(stage)
            still_ready=old and any(r['source_id']==old['source'] for r in rows)
            quantum=old and old['giant'] and (old['count']>=self.quantum or now-old['since']>=self.seconds)
            # Existing sticky ownership survives priority changes. Only an
            # ordinary existing release/admission point can admit aged kind.
            if admit and (not still_ready or quantum):
                aged=[k for k in present if now-self.kind_wait[stage,k]>=self.kind_wait_seconds]
                if aged:
                    kind=min(aged,key=lambda k:(self.kind_wait[stage,k],k))
                    before=list(rows);rows=[r for r in rows if self.kinds[r['source_id']]==kind]
                    record('media',before,rows,'kind == oldest_aged_kind',lambda sid:dict(kind=self.kinds[sid],chosen_kind=kind,wait_seconds=now-self.kind_wait[stage,self.kinds[sid]],threshold=self.kind_wait_seconds,still_ready=bool(still_ready),quantum=bool(quantum)))
            if self.folders and admit:
                available={self.folders.get(r['source_id'],'.') for r in rows}
                for name in available:self.folder_wait.setdefault((stage,name),now)
                owner=self.active.get(stage)
                sticky=owner and any(r['source_id']==owner['source'] for r in rows) and not (owner['giant'] and (owner['count']>=self.quantum or now-owner['since']>=self.seconds))
                preferred=self.folder_active.get(stage)
                if not sticky and preferred and len(available)>1:
                    aged=[f for f in available if now-self.folder_wait[stage,f]>=self.seconds]
                    expired=preferred['count']>=self.quantum or now-preferred['since']>=self.seconds
                    chosen=min(aged,key=lambda f:(self.folder_wait[stage,f],f)) if aged else preferred['folder']
                    if expired:
                        alternatives=available-{preferred['folder']}
                        if alternatives:chosen=min(alternatives,key=lambda f:(self.folder_wait[stage,f],f))
                    if chosen in available:
                        before=list(rows);rows=[r for r in rows if self.folders.get(r['source_id'],'.')==chosen]
                        record('folder',before,rows,'folder == chosen_folder',lambda sid:dict(folder=self.folders.get(sid,'.'),chosen_folder=chosen,preferred=dict(preferred),expired=bool(expired),sticky=bool(sticky),wait_seconds=now-self.folder_wait[stage,self.folders.get(sid,'.')],seconds=self.seconds,quantum_limit=self.quantum))
            before=list(rows);ordered=super().order(stage,rows,admit)
            record('source',before,ordered,'source admission returns ordered eligible rows',lambda sid:dict(active=dict(self.active.get(stage) or {}),quantum_limit=self.quantum,seconds=self.seconds))
            self._trace_order(stage,original,ordered,layers)
            return ordered
    def _trace_order(self,stage,original,ordered,layers):
        if stage not in ('source_coordination','source_audio','source_visual'):return
        positions={r['source_id']:i+1 for i,r in enumerate(ordered)}
        for item in original:
            sid=item['source_id'];checks=layers[sid]
            failed=next((x for x in checks if not x['result']),None)
            row=dict(event='FIRST_SELECTION_ADMISSION_DECISION' if stage=='source_coordination' else 'SOURCE_ADMISSION_DECISION',stage=stage,source_id=sid,revision=self.trace_revisions.get(sid),input_count=len(original),output_count=len(ordered),excluded_count=len(original)-len(ordered),layers=checks,admission_subreason=(failed['layer']+':'+failed['exact_predicate']) if failed else 'ORDER_RETURNED',rank=positions.get(sid))
            row.update({name+'_admission':next((v['result'] for v in checks if v['layer']==name),'NOT_EVALUATED') for name in ('media','folder','source')})
            self.last_decisions[stage,sid]=row
            # Time inputs are captured at the actual predicate evaluation, but
            # do not force an unchanged decision to be logged on every poll.
            signature=json.dumps(dict(result=row['admission_subreason'],rank=row['rank'],revision=row['revision'],input_count=row['input_count'],output_count=row['output_count'],chosen=[(v['layer'],v['input_value'].get('chosen_folder'),v['input_value'].get('chosen_kind')) for v in checks]),sort_keys=True)
            if self._decision_signatures.get((stage,sid))==signature:continue
            self._decision_signatures[stage,sid]=signature
            if not self.path:continue
            try:
                with Path(self.path).with_name('admission-decisions.jsonl').open('a') as f:f.write(json.dumps(dict(timestamp=time.time(),**row),ensure_ascii=False)+'\n')
            except OSError:pass
    def claimed(self,stage,source):
        with self.lock:
            name=self.folders.get(source)
            if name is not None:
                old=self.folder_active.get(stage)
                if not old or old['folder']!=name:self.folder_active[stage]={'folder':name,'since':self.clock(),'count':0}
                self.folder_active[stage]['count']+=1;self.folder_wait[stage,name]=self.clock()
            self.kind_wait[stage,self.kinds[source]]=self.clock()
            return super().claimed(stage,source)
    def snapshot(self):
        with self.lock:
            result=super().snapshot()
            result['trace_scope']='ADMISSION_OWNER_ONLY'
            result['ended_stages']=sorted(self.ended_stages)
            result['folder_active']={s:dict(v) for s,v in self.folder_active.items()}
            result['media_kind_wait_seconds']={stage+':'+kind:max(0,self.clock()-when) for (stage,kind),when in self.kind_wait.items()}
            return result
