"""One conditional local PCM repair. No new jobs, raw edits or semantic dedup."""
from apps.media_archive_image_video_ui.stop_provenance import child_run,child_spawn
import copy,hashlib,json,os,subprocess,wave
from pathlib import Path
from video_asr_chunks import build_chunks,CONFIG

POLICY='video-local-pcm-repair-v1'

def identity(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class PhysicalBudget:
    """Two durable reservations per logical unit, including interrupted Resume.

    A lost/uncertain result is infrastructure failure, never permission to spend
    another call. Completed raw receipts replay without re-running the model.
    """
    def __init__(self,provider,root,execution_key):
        self.provider=provider;self.root=Path(root)/'asr-attempts'/identity(execution_key);self.ordinal=0
        self.root.mkdir(parents=True,exist_ok=True)
    def __call__(self,p):
        ordinal=self.ordinal;self.ordinal+=1
        if ordinal>=2:raise RuntimeError('ASR_PHYSICAL_BUDGET_EXHAUSTED')
        key=identity((p.get('sha'),p['start_ms'],p['end_ms'],p.get('_asr_local_zero',False),p.get('_video_repair',{}).get('repair_id')))
        receipt=self.root/(str(ordinal)+'.json')
        if receipt.exists():
            old=json.loads(receipt.read_text())
            if old['input_identity']!=key:raise RuntimeError('ASR_ATTEMPT_INPUT_CHANGED')
            if old['state']!='COMPLETE':raise RuntimeError('ASR_PHYSICAL_ATTEMPT_UNCERTAIN_NO_RETRY')
            if old['result_sha']!=identity(old['raw_result']):raise RuntimeError('ASR_ATTEMPT_RAW_CHANGED')
            return copy.deepcopy(old['raw_result'])
        reserved={'state':'STARTED','input_identity':key,'ordinal':ordinal}
        with receipt.open('x') as f:json.dump(reserved,f);f.flush();os.fsync(f.fileno())
        raw=self.provider(p)
        complete={**reserved,'state':'COMPLETE','raw_result':raw,'result_sha':identity(raw)}
        temporary=receipt.with_suffix('.tmp')
        with temporary.open('w') as f:json.dump(complete,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        temporary.replace(receipt)
        return raw

def prepare(payload,root,source_id,source_revision,intervals,model_identity,owning_id):
    directory=Path(root)/'audio-core'/source_id;directory.mkdir(parents=True,exist_ok=True)
    # Source-owned normalized PCM; create only after the first hard reject.
    normal=directory/(identity((source_revision,payload['sha'],'normalized-16k-v1'))+'.wav')
    if not normal.exists():
        child_run(root,['ffmpeg','-nostdin','-v','error','-n','-i',payload['path'],'-ac','1','-ar','16000','-c:a','pcm_s16le',str(normal)],check=True,timeout=120)
    with wave.open(str(normal)) as w:
        if (w.getframerate(),w.getnchannels(),w.getsampwidth())!=(16000,1,2):raise ValueError('VIDEO_REPAIR_PCM_INVALID')
        duration=w.getnframes()*1000/16000
    matched=[n for n,m in enumerate(intervals) if (m['start_ms'],m['end_ms'])==(payload['start_ms'],payload['end_ms'])]
    if len(matched)!=1:raise ValueError('VIDEO_REPAIR_VAD_OWNER_INVALID')
    ordinal=matched[0]
    chunk=next(c for c in build_chunks(intervals,duration,source_revision=source_revision)
               if any(m['vad_ordinal']==ordinal for m in c['included_vad_intervals']))
    left,right=chunk['start_ms'],chunk['end_ms']
    proof={'policy':POLICY,'source_id':source_id,'source_revision':source_revision,'owning_id':owning_id,
           'owning_vad_ms':[payload['start_ms'],payload['end_ms']],'window_ms':[left,right],
           'model_identity':model_identity,'input_sha':payload['sha'],'intervals':intervals,'ordinal':ordinal,'config':CONFIG}
    proof['repair_id']=identity(proof);target=directory/(proof['repair_id']+'.wav')
    if not target.exists():
        child_run(root,['ffmpeg','-nostdin','-v','error','-n','-ss',str(left/1000),'-i',str(normal),'-t',str((right-left)/1000),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(target)],check=True,timeout=60)
    owned=[]
    for path,kind in [(normal,'ASR_NORMALIZED_PCM'),(target,'ASR_RETRY_SNIPPET')]:
        sha=hashlib.sha256(path.read_bytes()).hexdigest();receipt=path.with_suffix('.json')
        item={'path':str(path),'sha':sha,'source_id':source_id,'kind':kind,'evidence_receipt':str(receipt)}
        if receipt.exists() and json.loads(receipt.read_text())!=item:raise ValueError('VIDEO_REPAIR_INPUT_CHANGED')
        if not receipt.exists():receipt.write_text(json.dumps(item,sort_keys=True))
        owned.append(item)
    with wave.open(str(target)) as w:actual=w.getnframes()*1000/16000
    if abs(actual-(right-left))>1:raise ValueError('VIDEO_REPAIR_WINDOW_TRUNCATED')
    return ({'path':str(target),'sha':owned[-1]['sha'],'start_ms':0,'end_ms':actual,
             'segment_id':payload.get('segment_id'),'_asr_local_zero':True,'_video_repair':proof},left,owned)

def owned_assessment(raw,left,right,offset,proof):
    from asr_quality import evaluate,HARD
    if proof['policy']!=POLICY or proof['owning_vad_ms']!=[left,right] or offset!=proof['window_ms'][0]:
        raise ValueError('VIDEO_REPAIR_PROOF_MISMATCH')
    expected=copy.deepcopy(proof);repair_id=expected.pop('repair_id')
    if identity(expected)!=repair_id:raise ValueError('VIDEO_REPAIR_IDENTITY_CHANGED')
    a,b=proof['window_ms'];assessment=evaluate(raw,a,b,offset_ms=offset)
    if assessment['quality'] in HARD:return assessment
    kept=[];excluded=[]
    for row in assessment['usable_timeline']:
        # Whole raw subsegments only. Any competing VAD overlap is ambiguous;
        # never clip a sentence or steal a neighboring unit's speech ownership.
        owners=[n for n,m in enumerate(proof['intervals']) if min(row['source_end_ms'],m['end_ms'])>max(row['source_start_ms'],m['start_ms'])]
        if owners==[proof['ordinal']]:kept.append(row)
        else:excluded.append({'raw_ordinal':row['raw_ordinal'],'vad_owners':owners,'reason':'CONTEXT_NOT_EXCLUSIVELY_OWNED'})
    assessment['ownership_excluded']=excluded;assessment['usable_timeline']=kept
    assessment['usable_text']=' '.join(r['text'] for r in kept)
    if raw.get('text','').strip() and not kept:
        assessment['quality']='REPAIR_OWNERSHIP_UNRESOLVED';assessment['flags'].append('REPAIR_OWNERSHIP_UNRESOLVED')
    return assessment
