"""Opt-in guarded AUDIO document path. Never indexes raw rejected ASR text."""
import json
from asr_quality import CONTRACT,HARD,validate_packet
from apps.media_archive_image_video_ui.v2_documents import DocumentChain
from apps.media_archive_image_video_ui.v2_producer import fingerprint
from apps.media_archive_image_video_ui.v2_runtime import ContractError

class UsableAudioDocuments(DocumentChain):
    def callback(self,c,event):
        p=event['payload']
        from speech_evidence import enabled as speech_enabled,record_vad,record
        speech=speech_enabled(self.contracts)
        if event['event_type']!='EVIDENCE_READY' or p.get('stage')!='whisper':
            result=super().callback(c,event)
            if speech and event['event_type']=='EVIDENCE_READY' and p.get('stage')=='vad':record_vad(c,event)
            return result
        r=p['result']
        if self.contracts['whisper'].get('usable_asr_contract') and r.get('asr_contract')!=CONTRACT:
            raise ContractError('ASR_USABLE_CONTRACT_REQUIRED')
        if r.get('asr_contract')!=CONTRACT:return super().callback(c,event)
        validate_packet(r);sid=event['source_id'];rev=event['source_revision']
        job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(p['execution_key'],)).fetchone()
        if not job or job['status']!='DONE' or job['result_sha']!=p['payload_sha']:return
        owning=json.loads(job['payload'])
        if (r['source_start_ms'],r['source_end_ms'])!=(owning['start_ms'],owning['end_ms']):raise ContractError('ASR_OWNING_CLIP_MISMATCH')
        if speech:
            tier=record(c,event,job,r)
            if tier!='SPEECH_CONFIDENT' and r['quality_status'] not in HARD:
                outcome='SKIP_NO_TEXT' if not r['usable_text'].strip() else 'WHISPER_UNCERTAIN_FINAL'
                self.store.emit(c,outcome,sid,rev,job['revision'],{'execution_key':job['execution_key'],'raw_result_sha':job['result_sha']})
                return
        if r['quality_status'] in HARD:
            from video_transcript_quality import enabled,record_rejection
            if enabled(self.contracts):
                record_rejection(self,c,job,r)
                return
            reason={'stage':'whisper','reason':'ASR_REQUIRED_QUALITY_REVIEW','quality_status':r['quality_status'],
                    'quality_flags':r['quality_flags'],'execution_key':p['execution_key'],'raw_result_sha':p['payload_sha']}
            c.execute('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)',(sid,rev,'ASR_QUALITY_REVIEW',json.dumps(reason,sort_keys=True)))
            self.store.emit(c,'ASR_QUALITY_REVIEW',sid,rev,job['revision'],reason)
            return
        timeline=r['usable_timeline'];text=r['usable_text'];did=fingerprint(('whisper',p['execution_key'],p['payload_sha']))
        provenance={'execution_key':p['execution_key'],'input_sha':job['input_sha'],'contract':json.loads(job['provenance']),
            'result_sha':p['payload_sha'],'asr_contract':CONTRACT,'usable_timeline':timeline,'quality_status':r['quality_status'],
            'quality_flags':r['quality_flags'],'raw_evidence_only':True}
        start,end=(timeline[0]['source_start_ms'],timeline[-1]['source_end_ms']) if timeline else (None,None)
        status='READY' if timeline else 'SKIP_NO_TEXT'
        c.execute("INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?)",(did,sid,rev,None,
            start,end,'whisper',p['execution_key'],text,json.dumps(provenance,sort_keys=True),job['revision'],status))
        old=c.execute("SELECT * FROM v2_documents WHERE document_type='whisper' AND raw_result_ref=?",(p['execution_key'],)).fetchone()
        if old['text']!=text or old['start_ms']!=start or old['end_ms']!=end:
            raise ContractError('ASR_DOCUMENT_REPLAY_CONFLICT')
        did=old['document_id']
        if not timeline:
            self.store.adapter.pipeline.index.covered(c,did,'no-text-skip',job['result_sha'],CONTRACT)
            self.store.emit(c,'DOCUMENT_SKIPPED_NO_TEXT',sid,rev,job['revision'],{'document_id':did,'result_sha':job['result_sha']})
            return
        reusable=c.execute("SELECT execution_key FROM v2_current_jobs WHERE stage='embedding' AND item_id=? AND status='DONE' AND input_sha=? AND provenance=? LIMIT 1",(did,fingerprint(text),json.dumps(self.contracts['embedding'],sort_keys=True))).fetchone()
        if reusable:
            c.execute("UPDATE v2_documents SET embedding_ref=?,status='EMBEDDED' WHERE document_id=?",(reusable[0],did))
            self.store.emit(c,'DOCUMENT_EMBEDDED',sid,rev,job['revision'],{'document_id':did,'embedding_ref':reusable[0]});return
        if not (old['status']=='EMBEDDED' and old['embedding_ref']):
            self.enqueue(c,'embedding',did,event,fingerprint(text),{'document_id':did,'text':text,'document_type':'whisper'})
        self.store.emit(c,'DOCUMENT_READY',sid,rev,job['revision'],{'document_id':did,'document_type':'whisper','text_sha':fingerprint(text)})

def install(adapter):adapter.pipeline.documents.__class__=UsableAudioDocuments

def cleanup_paths(runtime,source,revision):
    """Only new source-owned retry snippets whose evidence is durable may be removed."""
    if not runtime.adapters.has_state(source,revision,'SOURCE_COMPLETE'):return []
    from apps.media_archive_image_video_ui.video_audio_recovery import owned_audio_path
    paths=[]
    with runtime.adapters.store.connect() as c:
        routed=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='AUDIO_ROUTE_READY'",(source,revision)).fetchone()
        if routed:
            from pathlib import Path
            from apps.media_archive_image_video_ui.v2_model_workers import sha_file
            for item in json.loads(routed[0]).get('transient_audio',[]):
                p=owned_audio_path(runtime.ledger.path,item['path'])
                if item.get('source_id')!=source or not p.resolve().is_relative_to((runtime.root/'audio-core'/source).resolve()):raise ContractError('CASS_CLEANUP_OWNER_MISMATCH')
                if p.exists() and sha_file(p)!=item['sha']:raise ContractError('CASS_CLEANUP_SHA_CHANGED')
                paths.append(p)
        for row in c.execute("SELECT * FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND stage='whisper' AND status='DONE'",(source,revision)):
            result=json.loads(row['result_json'])
            if result.get('asr_contract')!=CONTRACT:continue
            validate_packet(result)
            if result['quality_status'] in HARD:
                from video_transcript_quality import reject_proof
                if not reject_proof(c,row):raise ContractError('ASR_CLEANUP_UNRESOLVED_REVIEW')
            for item in result['derived_inputs']:
                from pathlib import Path
                from apps.media_archive_image_video_ui.v2_model_workers import sha_file
                p=owned_audio_path(runtime.ledger.path,item['path']);root=(runtime.root/'audio-core'/source).resolve()
                if not p.resolve().is_relative_to(root) or item['source_id']!=source:raise ContractError('ASR_CLEANUP_OWNER_MISMATCH')
                if p.exists() and sha_file(p)!=item['sha']:raise ContractError('ASR_CLEANUP_SHA_CHANGED')
                paths.append(p)
                receipt=owned_audio_path(runtime.ledger.path,item['evidence_receipt'])
                if not receipt.resolve().is_relative_to(root):raise ContractError('ASR_RECEIPT_OWNER_MISMATCH')
                paths.append(receipt)
    return paths
