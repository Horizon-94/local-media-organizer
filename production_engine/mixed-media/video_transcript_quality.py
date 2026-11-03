"""VIDEO-only enrichment terminal proofs. Never weakens the ASR guard."""
import json
from asr_quality import HARD,validate_packet
from apps.media_archive_image_video_ui.v2_runtime import ContractError
from apps.media_archive_image_video_ui.v2_producer import fingerprint

POLICY='video-enrichment-quality-terminal-v1'

def enabled(contracts):return contracts.get('whisper',{}).get('video_transcript_policy')==POLICY

def reject_proof(c,job):
    row=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone()
    if not row or row['stage']!='whisper':return None
    contract=json.loads(row['provenance'])
    if contract.get('video_transcript_policy')!=POLICY:return None
    meta=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(row['source_id'],row['source_revision'])).fetchone()
    if not meta or json.loads(meta[0]).get('media_type')!='video':raise ContractError('VIDEO_REJECTION_SOURCE_KIND_INVALID')
    if row['status']!='DONE' or row['result_sha']!=job['result_sha']:raise ContractError('VIDEO_REJECTION_CURRENT_RESULT_CHANGED')
    result=json.loads(row['result_json']);validate_packet(result)
    if result['quality_status'] not in HARD:return None
    if result.get('fallback_error') or len(result['asr_attempts'])!=2:
        raise ContractError('VIDEO_ASR_FALLBACK_INFRASTRUCTURE_FAILURE')
    owning=json.loads(row['payload'])
    if (result['source_start_ms'],result['source_end_ms'])!=(owning['start_ms'],owning['end_ms']):raise ContractError('VIDEO_REJECTION_OWNING_INTERVAL_CHANGED')
    marker=c.execute('SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state=?',
        (row['source_id'],row['source_revision'],'WHISPER_REJECTED_FINAL:'+row['execution_key'])).fetchone()
    if not marker:raise ContractError('video_rejection_proof_pending')
    value=json.loads(marker[0])
    if value!={'policy':POLICY,'execution_key':row['execution_key'],'raw_result_sha':row['result_sha'],
               'quality_status':result['quality_status'],'quality_flags':result['quality_flags']}:
        raise ContractError('VIDEO_REJECTION_PROOF_CHANGED')
    if c.execute('SELECT 1 FROM v2_documents WHERE raw_result_ref=?',(row['execution_key'],)).fetchone():raise ContractError('VIDEO_REJECTED_DOCUMENT_LEAK')
    if c.execute("SELECT 1 FROM v2_current_jobs WHERE stage='embedding' AND item_id=?",(fingerprint(('whisper',row['execution_key'],row['result_sha'])),)).fetchone():raise ContractError('VIDEO_REJECTED_EMBEDDING_LEAK')
    return fingerprint(value)

def record_rejection(documents,c,job,result):
    meta=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(job['source_id'],job['source_revision'])).fetchone()
    if not meta or json.loads(meta[0]).get('media_type')!='video':raise ContractError('VIDEO_REJECTION_SOURCE_KIND_INVALID')
    value={'policy':POLICY,'execution_key':job['execution_key'],'raw_result_sha':job['result_sha'],
           'quality_status':result['quality_status'],'quality_flags':result['quality_flags']}
    c.execute('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)',
              (job['source_id'],job['source_revision'],'WHISPER_REJECTED_FINAL:'+job['execution_key'],json.dumps(value,sort_keys=True)))
    reject_proof(c,job)
    documents.store.emit(c,'WHISPER_REJECTED_FINAL',job['source_id'],job['source_revision'],job['revision'],value)

def classify(usable,rejected,jobs):
    if not jobs:return 'VIDEO_NO_SPEECH'
    if not usable:return 'VIDEO_TRANSCRIPT_UNAVAILABLE'
    return 'VIDEO_TRANSCRIPT_PARTIAL' if rejected else 'VIDEO_TRANSCRIPT_COMPLETE'

def publish_status(adapter,source,revision):
    if not enabled(adapter.contracts):return True
    with adapter.store.connect() as c:
        branch=c.execute('SELECT * FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        if not branch or branch['status'] not in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY'):return False
        jobs=list(c.execute("SELECT * FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND stage='whisper'",(source,revision)))
        if len(jobs)!=len(json.loads(branch['segments_json'])) or any(j['status']!='DONE' for j in jobs):return False
        usable=rejected=empty=0
        for job in jobs:
            result=json.loads(job['result_json']);validate_packet(result)
            from speech_evidence import enabled as speech_enabled,decide,terminal_proof
            if speech_enabled(adapter.contracts) and decide(result)[0]!='SPEECH_CONFIDENT' and result['quality_status'] not in HARD:
                terminal_proof(c,job);rejected+=1;continue
            if result['quality_status'] in HARD:
                reject_proof(c,job);rejected+=1
            else:
                doc=c.execute('SELECT text,status FROM v2_documents WHERE raw_result_ref=?',(job['execution_key'],)).fetchone()
                if not doc or doc['status'] not in ('EMBEDDED','SKIP_NO_TEXT'):return False
                if doc['text']!=result['usable_text']:raise ContractError('VIDEO_USABLE_DOCUMENT_CHANGED')
                usable+=bool(result['usable_text']);empty+=not bool(result['usable_text'])
        status=classify(usable,rejected,len(jobs))
    adapter.state(source,revision,status,{'policy':POLICY,'usable':usable,'rejected':rejected,'empty':empty,'speech_jobs':len(jobs),'audio_branch':branch['status']})
    return True
