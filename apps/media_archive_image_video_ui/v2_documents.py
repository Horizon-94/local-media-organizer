"""Candidate/audio result -> versioned document -> embedding event chain."""
from __future__ import annotations
import json, os
from .v2_producer import fingerprint
from .v2_runtime import ContractError,validate_evidence

class AudioContractError(ContractError):
    failure_reason='NON_RETRYABLE_INPUT_INVALID'

def validate_result(stage,result):
    if not isinstance(result,dict):raise ContractError('result_must_be_object')
    if stage=='qwen':
        if result.get('contract')=='production-qwen-sections-v1':
            if not result.get('text') or not isinstance(result.get('sections'),dict):raise ContractError('qwen_sections_missing')
            return
        for key in ('people','objects','actions','scene','relationships','viewpoint','search_terms','description'):
            if key not in result or not isinstance(result[key],(str,list)):raise ContractError('qwen_schema:'+key)
    elif stage in ('ocr','whisper'):
        if not isinstance(result.get('text'),str):raise ContractError('text_result_missing')
        if stage=='whisper' and not result['text'].strip():raise ContractError('speech_segment_empty_transcript')
    elif stage=='embedding':validate_evidence('openclip',result)
    elif stage=='insightface':
        if not isinstance(result.get('faces'),list):raise ContractError('faces_missing_not_zero')
        for face in result['faces']:
            if not isinstance(face.get('quality_score'),(float,int)) or len(face.get('bbox',[]))!=4:raise ContractError('face_geometry_quality_missing')
            import math
            bbox=face['bbox']
            if not math.isfinite(face['quality_score']) or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in bbox) or bbox[2]<=bbox[0] or bbox[3]<=bbox[1]:raise ContractError('face_geometry_quality_invalid')
            validate_evidence('openclip',{'vector':face.get('embedding')})
    elif stage=='audio_extract':
        if result.get('status') not in ('READY','NO_AUDIO'):raise AudioContractError('audio_status_missing')
        if result['status']=='READY' and (not result.get('sha') or not result.get('path')):raise AudioContractError('audio_artifact_missing')
    elif stage=='vad':
        if not isinstance(result.get('segments'),list):raise ContractError('vad_segments_missing')
        for segment in result['segments']:
            if not 0<=segment['start_ms']<segment['end_ms']:raise ContractError('vad_range_invalid')

class DocumentChain:
    def __init__(self,store,contracts):
        self.store,self.contracts=store,contracts
        self.consumer='document-chain:'+fingerprint(contracts)
        with store.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS v2_documents(document_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
              artifact_id TEXT,start_ms INTEGER,end_ms INTEGER,document_type TEXT,raw_result_ref TEXT,text TEXT,
              provenance TEXT,revision INTEGER,embedding_ref TEXT,status TEXT,UNIQUE(document_type,raw_result_ref));
            CREATE TABLE IF NOT EXISTS v2_audio_branches(source_id TEXT,source_revision TEXT,status TEXT,
              artifact_json TEXT,segments_json TEXT,PRIMARY KEY(source_id,source_revision));
            """)

    @staticmethod
    def _audio_stream_present(row):
        try:
            probe=json.loads(row.get('row_json') or '{}').get('probe') or {}
            streams=((probe.get('metadata') or {}).get('streams') or [])
            return any(s.get('codec_type')=='audio' for s in streams)
        except (TypeError,ValueError):
            return False

    def ensure_audio_registration(self,c,source,revision,source_sha,payload):
        """Use the same idempotent contract for admission and reconciliation."""
        if not source or not revision or not source_sha: return {'status':'SKIP','reason':'missing_identity'}
        active=c.execute("SELECT source_revision FROM v2_active_sources WHERE source_id=?",(source,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_active_sources'").fetchone() else None
        if active and active[0] != revision: return {'status':'SKIP','reason':'stale_revision'}
        existing=c.execute("SELECT status FROM v2_jobs WHERE stage='audio_extract' AND source_id=? AND source_revision=? LIMIT 1",(source,revision)).fetchone()
        if existing: return {'status':'EXISTS','reason':'audio_job_exists'}
        branch=c.execute("SELECT status FROM v2_audio_branches WHERE source_id=? AND source_revision=?",(source,revision)).fetchone()
        if branch and branch[0] in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY','READY'): return {'status':'SKIP','reason':'terminal_audio_branch'}
        # Extraction is owned by the independent audio producer. Resume may
        # register only its durable artifact, never a raw-source passthrough.
        retained = c.execute("SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?",(source,revision)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='video_stream_inputs_v1'").fetchone() else None
        if not retained or not retained[0]:
            return {'status':'DEFERRED','reason':'audio_artifact_not_ready'}
        artifact=json.loads(retained[0])
        validate_result('audio_extract',artifact)
        self.enqueue(c,'audio_extract',source+':'+revision,
                     {'event_type':'SOURCE_READY','source_id':source,'source_revision':revision,'payload':artifact},
                     source_sha,artifact)
        return {'status':'CREATED','reason':'audio_registration'}

    def reconcile_audio_registration(self,limit=128):
        """Bounded, read-from-durable-eligibility reconciliation for Resume."""
        stats={'scanned':0,'eligible':0,'created':0,'existing':0,'pre_admission':0,
               'stale_revision':0,'terminal':0,'skipped_stop':0}
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            stopped=c.execute("SELECT stopped FROM video_stream_control_v1 WHERE singleton=1").fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='video_stream_control_v1'").fetchone() else None
            if stopped and stopped[0]: stats['skipped_stop']=1; return stats
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_source_metadata'").fetchone(): return stats
            rows=list(c.execute('SELECT * FROM v2_source_metadata ORDER BY rowid LIMIT ?', (int(limit),)))
            for row in rows:
                stats['scanned']+=1
                source=row['source_id']; revision=row['source_revision']; metadata=json.loads(row['metadata'] or '{}')
                if not source or not revision or metadata.get('media_type')=='image': stats['pre_admission']+=1; continue
                registry=c.execute("SELECT row_json FROM release_registry WHERE source_id=? ORDER BY rowid DESC LIMIT 1",(source,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='release_registry'").fetchone() else None
                if not registry or not self._audio_stream_present(dict(registry)):
                    stats['pre_admission']+=1; continue
                result=self.ensure_audio_registration(c,source,revision,row['source_sha'],{'source_sha':row['source_sha'],'path':row['path'],'content_revision':metadata.get('content_revision'),'metadata':metadata})
                if result['status']=='CREATED': stats['eligible']+=1; stats['created']+=1
                elif result['status']=='EXISTS': stats['eligible']+=1; stats['existing']+=1
                elif result['reason']=='stale_revision': stats['stale_revision']+=1
                elif result['reason']=='terminal_audio_branch': stats['terminal']+=1
            # Publish canonical audio jobs through the existing native queue
            # contract after the reconciliation transaction closes. This is
            # idempotent and also repairs jobs left PENDING by an interrupted
            # Resume.
            c.commit()
            adapter=getattr(self.store,'adapter',None)
            if adapter is not None:
                adapter.promote_policy_jobs(stages=('audio_extract',),limit=int(limit))
            return stats

    def enqueue(self,c,stage,item,event,input_sha,payload):
        if stage=='embedding' and os.environ.get('VIDEO_CLOSURE_ONLY')=='1':
            raise ContractError('FAIL_UNEXPECTED_EMBEDDING_ENQUEUE')
        contract=self.contracts.get(stage)
        if not contract:raise ContractError('stage_contract_not_configured:'+stage)
        parents=[]
        if event['event_type']=='EVIDENCE_READY':parents=[event['payload']['execution_key']]
        if event['event_type']=='CANDIDATE_READY' and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_snapshot_evidence'").fetchone():
            provider=getattr(self.store,'selection_parent_provider',None)
            parents=provider(c,event) if provider is not None else [r[0] for r in c.execute('SELECT execution_key FROM v2_snapshot_evidence WHERE snapshot_id=?',(event['payload']['snapshot_id'],))]
        # New audio execution can legitimately retain the same VAD model config.
        # Dependency identity must distinguish these jobs without forged revisions.
        if stage=='vad' and parents:item += ':'+parents[0]
        return self.store.enqueue(c,stage,item,contract['revision'],event['source_id'],event['source_revision'],input_sha,contract,payload,dependencies=parents)

    def callback(self,c,event):
        p=event['payload'];source,revision=event['source_id'],event['source_revision']
        if event['event_type']=='CANDIDATE_READY':
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_selected_candidates'").fetchone():
                current=c.execute('SELECT status FROM v2_selected_candidates WHERE candidate_id=?',(p['candidate_id'],)).fetchone()
                if current and current[0]=='SUPERSEDED':return
            aid=p['visual_unit_id']
            artifact=c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(aid,)).fetchone()
            if not artifact:raise ContractError('candidate_artifact_missing')
            self.enqueue(c,p['purpose'],p['candidate_id'],event,artifact['sha'],
                {'candidate_id':p['candidate_id'],'artifact_id':aid,'image_path':artifact['path'],
                 'derived_sha':artifact['sha'],
                 'start_ms':p['segment_start_ms'],'end_ms':p['segment_end_ms'],'snapshot_id':p['snapshot_id']})
            planner=getattr(self.store,'fanout_planner',None)
            if not (planner and planner.is_capturing()) and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_selected_candidates'").fetchone():
                c.execute("UPDATE v2_selected_candidates SET status='QUEUED' WHERE candidate_id=? AND status='READY'",(p['candidate_id'],))
        elif event['event_type']=='SOURCE_READY':
            if p.get('metadata',{}).get('media_type')=='image':
                c.execute('INSERT OR REPLACE INTO v2_audio_branches VALUES(?,?,?,?,?)',(source,revision,'NO_AUDIO',json.dumps({'status':'NO_AUDIO','reason':'still_image'}),'[]'))
            else:self.ensure_audio_registration(c,source,revision,p['source_sha'],p)
        elif event['event_type']=='AUDIO_ARTIFACT_READY':
            validate_result('audio_extract',p)
            c.execute('INSERT OR REPLACE INTO v2_audio_branches VALUES(?,?,?,?,?)',(source,revision,p['status'],json.dumps(p,sort_keys=True),'[]'))
            if p['status']=='READY':self.enqueue(c,'vad',source+':'+revision,event,p['sha'],p)
        elif event['event_type']=='EVIDENCE_READY':
            stage=p['stage'];r=p['result']
            job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(p['execution_key'],)).fetchone()
            # A superseded result event remains an audit record, not new work.
            if not job or job['status']!='DONE' or job['result_sha']!=p['payload_sha']:return
            payload=json.loads(job['payload'])
            if stage=='audio_extract':
                validate_result(stage,r)
                c.execute('INSERT OR REPLACE INTO v2_audio_branches VALUES(?,?,?,?,?)',(source,revision,r['status'],json.dumps(r,sort_keys=True),'[]'))
                if r['status']=='READY':self.enqueue(c,'vad',source+':'+revision,event,r['sha'],r)
            elif stage=='vad':
                validate_result(stage,r)
                status='SEGMENTS_READY' if r['segments'] else 'NO_SPEECH'
                c.execute('UPDATE v2_audio_branches SET status=?,segments_json=? WHERE source_id=? AND source_revision=?',(status,json.dumps(r['segments'],sort_keys=True),source,revision))
                for segment in r['segments']:
                    sid=fingerprint((source,revision,p['execution_key'],segment))
                    self.enqueue(c,'whisper',sid,event,fingerprint((payload['sha'],segment)),{**payload,**segment,'segment_id':sid,'vad_execution_key':p['execution_key']})
            elif stage in ('qwen','ocr','whisper'):
                validate_result(stage,r)
                did=fingerprint((stage,p['execution_key'],p['payload_sha']))
                text=r['text'] if 'text' in r else json.dumps(r,ensure_ascii=False,sort_keys=True)
                provenance={'execution_key':p['execution_key'],'input_sha':job['input_sha'],
                            'contract':json.loads(job['provenance']),'result_sha':p['payload_sha']}
                c.execute("INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'READY')",(did,source,revision,payload.get('artifact_id'),payload.get('start_ms'),payload.get('end_ms'),stage,p['execution_key'],text,json.dumps(provenance,sort_keys=True),job['revision']))
                existing=c.execute('SELECT * FROM v2_documents WHERE document_type=? AND raw_result_ref=?',(stage,p['execution_key'])).fetchone()
                did=existing['document_id']
                # Identity belongs to the authoritative execution/result,
                # never to a differently serialized replay envelope.
                if existing['text']!=text:raise ContractError('document_replay_text_conflict')
                reused=c.execute("SELECT execution_key FROM v2_current_jobs WHERE stage='embedding' AND item_id=? AND status='DONE' AND input_sha=? AND provenance=? LIMIT 1",(did,fingerprint(text),json.dumps(self.contracts['embedding'],sort_keys=True))).fetchone()
                if reused:
                    c.execute("UPDATE v2_documents SET embedding_ref=?,status='EMBEDDED' WHERE document_id=?",(reused[0],did))
                    self.store.emit(c,'DOCUMENT_EMBEDDED',source,revision,job['revision'],{'document_id':did,'embedding_ref':reused[0]})
                    return
                if not existing or not (existing['status']=='EMBEDDED' and existing['embedding_ref']):
                    self.enqueue(c,'embedding',did,event,fingerprint(text),{'document_id':did,'text':text,'document_type':stage})
                self.store.emit(c,'DOCUMENT_READY',source,revision,job['revision'],{'document_id':did,'document_type':stage,'text_sha':fingerprint(text)})
            elif stage=='embedding':
                validate_result(stage,r)
                did=payload['document_id']
                cur=c.execute("UPDATE v2_documents SET embedding_ref=?,status='EMBEDDED' WHERE document_id=?",(p['execution_key'],did))
                if cur.rowcount!=1:raise ContractError('embedding_document_missing')
                self.store.emit(c,'DOCUMENT_EMBEDDED',source,revision,job['revision'],{'document_id':did,'embedding_ref':p['execution_key'],'vector_sha':p['payload_sha']})

    def advance(self, limit=100):
        return self.store.deliver(self.consumer,self.callback,limit=limit)
