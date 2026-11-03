"""VAD facts and non-destructive transcript publication tiers, never ASR hard guards."""
import json,re
from asr_quality import HARD,validate_packet
from apps.media_archive_image_video_ui.v2_producer import fingerprint
from apps.media_archive_image_video_ui.v2_runtime import ContractError

POLICY='video-speech-search-publication-v1'
TAGS=['有人声','说话','讲话','speech','speaking']
def enabled(contracts):return contracts.get('whisper',{}).get('speech_evidence_policy')==POLICY
def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS release_vad_facts(source_id TEXT,revision TEXT,vad_ref TEXT,speech_present INTEGER,status TEXT,evidence TEXT,PRIMARY KEY(source_id,revision))')
    c.execute('''CREATE TABLE IF NOT EXISTS release_speech_intervals(source_id TEXT,revision TEXT,ordinal INTEGER,start_ms REAL,end_ms REAL,vad_ref TEXT,PRIMARY KEY(source_id,revision,ordinal))''')
    c.execute('''CREATE TABLE IF NOT EXISTS release_transcript_evidence(execution_key TEXT PRIMARY KEY,source_id TEXT,revision TEXT,result_sha TEXT,tier TEXT,evidence TEXT)''')
    c.execute('CREATE INDEX IF NOT EXISTS release_speech_source ON release_transcript_evidence(source_id,revision)')
    # A terminal transcript may intentionally create no Document/Embedding.
    # Such durable facts must wake the same source coordinator themselves.
    for table in ('release_vad_facts','release_transcript_evidence'):
        for action in ('INSERT','UPDATE','DELETE'):
            ref='OLD' if action=='DELETE' else 'NEW'
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_wake_{action} AFTER {action} ON {table} BEGIN
              INSERT INTO video_stream_change_tokens_v1 VALUES('source:'||{ref}.source_id,1)
              ON CONFLICT(scope) DO UPDATE SET generation=generation+1; END''')

def decide(packet):
    validate_packet(packet)
    if packet['quality_status'] in HARD:return 'SPEECH_REJECTED',list(packet['quality_flags'])
    text=packet['usable_text'];compact=re.sub(r'[^\w]+','',text)
    if not text.strip() or not packet['usable_timeline']:return 'SPEECH_REJECTED',['SKIP_NO_TEXT']
    risks=[]
    if packet['quality_status'] in ('LOW_CONFIDENCE','REVIEW'):risks.append(packet['quality_status'])
    if len(compact)<=2:risks.append('SEMANTIC_RISK_LOW_INFORMATION')
    if re.search(r'(.)\1{2,}',compact) or re.search(r'(.{2,20})\1+',compact) or (len(compact)>=10 and len(set(compact))/len(compact)<.4):
        risks.append('SEMANTIC_RISK_REPETITIVE_LOW_INFORMATION')
    return ('SPEECH_UNCERTAIN',risks) if risks else ('SPEECH_CONFIDENT',[])

def record_vad(c,event):
    p=event['payload'];sid=event['source_id'];rev=event['source_revision']
    body=json.dumps(p['result'],sort_keys=True);present=bool(p['result']['segments'])
    values=(sid,rev,p['execution_key'],int(present),'SPEECH_PRESENT' if present else 'NO_SPEECH',body)
    old=c.execute('SELECT * FROM release_vad_facts WHERE source_id=? AND revision=?',(sid,rev)).fetchone()
    if old and tuple(old)!=values:raise ContractError('VAD_EVIDENCE_REPLAY_CONFLICT')
    c.execute('INSERT OR IGNORE INTO release_vad_facts VALUES(?,?,?,?,?,?)',values)
    for n,s in enumerate(p['result']['segments']):
        c.execute('INSERT OR IGNORE INTO release_speech_intervals VALUES(?,?,?,?,?,?)',(sid,rev,n,s['start_ms'],s['end_ms'],p['execution_key']))

def record(c,event,job,packet):
    tier,risks=decide(packet);chosen=packet['asr_attempts'][packet['selected_asr_attempt']-1]
    evidence={'policy':POLICY,'speech_present':True,'authority':'VAD','audio_tags':TAGS,
        'owning_interval':[packet['source_start_ms'],packet['source_end_ms']],
        'transcript_raw':chosen['raw_result']['text'],'initial_transcript_raw':packet['text'],
        'transcript_quality':tier,'transcript_searchable':tier=='SPEECH_CONFIDENT',
        'quality_status':packet['quality_status'],'risk_flags':risks,'raw_result_ref':job['execution_key'],
        'terminal_outcome':'SKIP_NO_TEXT' if risks==['SKIP_NO_TEXT'] else tier,
        'raw_result_sha':job['result_sha'],'repair_provenance':chosen.get('input',{}).get('_video_repair')}
    body=json.dumps(evidence,sort_keys=True)
    old=c.execute('SELECT result_sha,tier,evidence FROM release_transcript_evidence WHERE execution_key=?',(job['execution_key'],)).fetchone()
    if old and tuple(old)!=(job['result_sha'],tier,body):raise ContractError('SPEECH_EVIDENCE_REPLAY_CONFLICT')
    c.execute('INSERT OR IGNORE INTO release_transcript_evidence VALUES(?,?,?,?,?,?)',(job['execution_key'],job['source_id'],job['source_revision'],job['result_sha'],tier,body))
    return tier

def terminal_proof(c,job):
    from video_transcript_quality import reject_proof
    row=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone()
    if not row:return None
    if not enabled({'whisper':json.loads(row['provenance'])}):return reject_proof(c,job)
    packet=json.loads(row['result_json']);tier,flags=decide(packet)
    if tier=='SPEECH_CONFIDENT' or packet['quality_status'] in HARD:return reject_proof(c,job)
    evidence=c.execute('SELECT result_sha,tier,evidence FROM release_transcript_evidence WHERE execution_key=?',(row['execution_key'],)).fetchone()
    if not evidence or tuple(evidence)[:2]!=(row['result_sha'],tier):raise ContractError('speech_publication_pending')
    if row['status']!='DONE' or row['result_sha']!=job['result_sha']:raise ContractError('SPEECH_CURRENT_RESULT_CHANGED')
    if c.execute('SELECT 1 FROM v2_documents WHERE raw_result_ref=?',(row['execution_key'],)).fetchone():raise ContractError('UNCERTAIN_TRANSCRIPT_DOCUMENT_LEAK')
    did=fingerprint(('whisper',row['execution_key'],row['result_sha']))
    if c.execute("SELECT 1 FROM v2_current_jobs WHERE stage='embedding' AND item_id=?",(did,)).fetchone():raise ContractError('UNCERTAIN_TRANSCRIPT_EMBEDDING_LEAK')
    return fingerprint(tuple(evidence))
