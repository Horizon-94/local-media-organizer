"""Guarded ASR adapter; media admission is explicit, defaulting to AUDIO."""
from apps.media_archive_image_video_ui.stop_provenance import child_run,child_spawn
import hashlib,json,sqlite3,subprocess,time,wave
from pathlib import Path
from asr_quality import CONTRACT,RepeatHistory,guarded_inference,validate_packet

CURRENT_JOB_SQL="""WITH RECURSIVE ancestry(parent_key) AS (
 SELECT execution_key FROM v2_jobs WHERE execution_key=?
 UNION SELECT d.parent_key FROM ancestry a JOIN v2_job_dependencies d ON d.child_key=a.parent_key
 ) SELECT j.source_revision,j.provenance,j.execution_key FROM v2_jobs j
 WHERE j.execution_key=? AND j.status<>'SUPERSEDED' AND NOT EXISTS (
 SELECT 1 FROM ancestry a LEFT JOIN v2_jobs p ON p.execution_key=a.parent_key
 LEFT JOIN v2_active_sources active ON active.source_id=p.source_id
 WHERE p.execution_key IS NULL OR (a.parent_key<>j.execution_key AND p.status<>'DONE')
 OR (active.source_revision IS NOT NULL AND active.source_revision<>p.source_revision))"""

class GuardedWhisper:
    def __init__(self,resident,root,*,audio_only=False,history=None,guarded_kinds=('audio',)):
        self.resident=resident;self.root=Path(root);self.audio_only=audio_only
        self.guarded_kinds=frozenset(guarded_kinds)
        self.history=history or RepeatHistory()
    def __getattr__(self,name):return getattr(self.resident,name)
    def prepare(self):return self.resident.prepare()
    def close(self):return self.resident.close()
    def source(self,p):
        with sqlite3.connect((self.root/'video.sqlite').as_uri()+'?mode=ro',uri=True) as c:
            c.execute('PRAGMA query_only=ON')
            r=c.execute('SELECT j.source_id,json_extract(m.metadata,\'$.media_type\') FROM v2_jobs j JOIN v2_source_metadata m ON m.source_id=j.source_id AND m.source_revision=j.source_revision WHERE j.execution_key=?',(p.get('_evidence_key'),)).fetchone()
        if not r:raise ValueError('ASR_SOURCE_IDENTITY_MISSING')
        return r
    def local_input(self,p,source_id):
        left=max(0,p['start_ms']-350);right=p['end_ms']+350
        key=hashlib.sha256(json.dumps({k:p.get(k) for k in ('path','sha','start_ms','end_ms')},sort_keys=True).encode()).hexdigest()
        directory=self.root/'audio-core'/source_id;directory.mkdir(parents=True,exist_ok=True)
        target=directory/(key+'.wav');receipt=directory/(key+'.json')
        if not target.exists():
            child_run(self.root,['ffmpeg','-nostdin','-v','error','-n','-i',p['path'],'-ss',str(left/1000),'-t',str((right-left)/1000),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(target)],check=True,timeout=60)
        with wave.open(str(target),'rb') as f:
            if f.getframerate()!=16000 or f.getnchannels()!=1 or f.getsampwidth()!=2:raise ValueError('ASR_LOCAL_PCM_FORMAT_CHANGED')
            duration_ms=f.getnframes()*1000/16000
        sha=hashlib.sha256(target.read_bytes()).hexdigest()
        if receipt.exists() and json.loads(receipt.read_text())['sha']!=sha:raise ValueError('ASR_RETRY_SNIPPET_CHANGED')
        owned={'path':str(target),'sha':sha,'source_id':source_id,'kind':'ASR_RETRY_SNIPPET','evidence_receipt':str(receipt)}
        if not receipt.exists():receipt.write_text(json.dumps(owned,sort_keys=True))
        end=p['end_ms']-left
        if end>duration_ms+40:raise ValueError('ASR_RETRY_CLIP_TRUNCATED')
        return ({'path':str(target),'sha':sha,'start_ms':p['start_ms']-left,'end_ms':end,
                 'segment_id':p.get('segment_id'),'representation':'segment-local-pcm-v1'},left,[owned])
    def __call__(self,payload):
        sid,kind=self.source(payload)
        if kind not in self.guarded_kinds:return self.resident(payload)
        def local(p):
            if kind!='video':return self.local_input(p,sid)
            from video_asr_repair import prepare
            with sqlite3.connect((self.root/'video.sqlite').as_uri()+'?mode=ro',uri=True) as c:
                row=c.execute(CURRENT_JOB_SQL,(p['_evidence_key'],p['_evidence_key'])).fetchone()
                if not row:raise ValueError('VIDEO_REPAIR_JOB_NOT_CURRENT')
                branch=c.execute('SELECT segments_json FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(sid,row[0])).fetchone()
                if not branch:raise ValueError('VIDEO_REPAIR_VAD_NOT_DURABLE')
            return prepare(p,self.root,sid,row[0],json.loads(branch[0]),json.loads(row[1]),row[2])
        provider=self.resident
        if kind=='video':
            from video_asr_repair import PhysicalBudget
            provider=PhysicalBudget(provider,self.root,payload['_evidence_key'])
        result=guarded_inference(provider,payload,local,source_id=sid,history=self.history)
        validate_packet(result)
        with (self.root/'asr-quality-events.jsonl').open('a') as f:
            f.write(json.dumps({'at':time.time(),'source_id':sid,'segment_id':payload.get('segment_id'),'contract':CONTRACT,
                'quality_status':result['quality_status'],'quality_flags':result['quality_flags'],
                'selected_asr_attempt':result['selected_asr_attempt'],'attempts':[{k:v for k,v in a.items() if k not in ('raw_result','assessment')}|{'quality':a['assessment']['quality']} for a in result['asr_attempts']]})+'\n')
        return result
