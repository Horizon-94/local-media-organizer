"""Explicit RAW visual skip, with unchanged durable audio evidence chain."""
import json
from pathlib import Path
import subprocess
import time
from .v2_source_recovery import RecoveringProductLifecycle
from .v2_product_lifecycle import ProductLifecycle
from .v2_producer import fingerprint
from .v2_runtime import ContractError

TAGS = {'.crm': {'CRAW'}, '.braw': {'brxq'}}


def producer_raw_refill_state(c,run,epoch):
    """Visual window excludes sealed RAW skips; only proven skips bypass it."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_raw_visual_skips'").fetchone():return set(),set()
    sealed={r[0] for r in c.execute('''SELECT r.source_id FROM v2_raw_visual_skips r
        JOIN v2_active_sources a ON a.source_id=r.source_id AND a.source_revision=r.source_revision
        JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
        WHERE r.scheduler_run_id=? AND r.execution_epoch=? AND r.status IN ('ACTIVE','COMPLETE')
        AND s.eos='VISUAL_SKIPPED' ''',(run,epoch))}
    authorized={r[0] for r in c.execute('''SELECT r.source_id FROM v2_raw_visual_skips r
        JOIN v2_product_sources p ON p.source_id=r.source_id AND p.scheduler_run_id=r.scheduler_run_id
        WHERE r.scheduler_run_id=? AND r.execution_epoch=? AND r.status='CONFIRMED' AND p.state='NEW' ''',(run,epoch))}
    return sealed,authorized


def probe_raw(path):
    path = Path(path)
    if path.suffix.lower() not in TAGS: return None
    result = subprocess.run(['/opt/homebrew/bin/ffprobe','-v','error','-show_streams','-of','json',str(path)],
                            capture_output=True,text=True,timeout=30)
    if result.returncode: raise ContractError('raw_audio_probe_failed:'+result.stderr[-1000:])
    report = json.loads(result.stdout)
    video = [s for s in report.get('streams',[]) if s.get('codec_type')=='video']
    if not video or any(s.get('codec_tag_string') not in TAGS[path.suffix.lower()] for s in video):
        return None
    stat=path.stat()
    return {**report,'source_size':stat.st_size,'source_mtime_ns':stat.st_mtime_ns,
            'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO','audio_stream_count':
            sum(s.get('codec_type')=='audio' for s in report.get('streams',[]))}


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS v2_raw_visual_skips(
        source_id PRIMARY KEY,scheduler_run_id,execution_epoch,previous_revision,
        source_revision,status,reason,probe_json,updated_at)''')


class RawAwareProductLifecycle(RecoveringProductLifecycle):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        try:self.reconcile_raw()
        except BaseException:self.close();raise

    def refresh_source(self,sid,revision):
        """Publish only the actual current closure, not cached outcome alone."""
        with self.store.connect() as c:
            row=c.execute('''SELECT p.state,s.eos,o.status outcome,h.status closure
                FROM v2_product_sources p JOIN v2_active_sources a ON a.source_id=p.source_id
                AND a.source_revision=p.source_revision JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
                LEFT JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.revision
                LEFT JOIN v2_source_closure h ON h.source_id=s.source_id AND h.source_revision=s.revision
                WHERE p.scheduler_run_id=? AND p.source_id=? AND p.source_revision=?
                AND p.state IN ('INGESTED','COMPLETE')''',(self.run,sid,revision)).fetchone()
            if not row or row['eos']=='TIMELAPSE_COVERED':return False
            state='COMPLETE' if row['eos'] in ('EOS','VISUAL_SKIPPED') and row['outcome'] in ('COMPLETE','DEGRADED') and row['closure']=='COMPLETE' else 'INGESTED'
            if row['outcome'] in ('FAILED_FINAL','FAILED','ABORTED'):state='FAILED_FINAL'
            if state==row['state']:return False
            c.execute('BEGIN IMMEDIATE')
            self.store.assert_source_epoch(c,sid,revision)
            # Recheck closure under the owned write boundary before publication.
            proof=c.execute('''SELECT s.eos,o.status,h.status FROM v2_sources s
                JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision
                LEFT JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.revision
                LEFT JOIN v2_source_closure h ON h.source_id=s.source_id AND h.source_revision=s.revision
                WHERE s.source_id=? AND s.revision=?''',(sid,revision)).fetchone()
            if not proof:return False
            state='COMPLETE' if proof[0] in ('EOS','VISUAL_SKIPPED') and proof[1] in ('COMPLETE','DEGRADED') and proof[2]=='COMPLETE' else 'INGESTED'
            if proof[1] in ('FAILED_FINAL','FAILED','ABORTED'):state='FAILED_FINAL'
            current=c.execute('SELECT state FROM v2_product_sources WHERE scheduler_run_id=? AND source_id=? AND source_revision=?',
                              (self.run,sid,revision)).fetchone()
            if not current or current[0]==state:return False
            c.execute('UPDATE v2_product_sources SET state=? WHERE scheduler_run_id=? AND source_id=? AND source_revision=?',
                (state,self.run,sid,revision))
            self.store.emit(c,'SOURCE_PRODUCT_STATE_CHANGED',sid,revision,1,
                {'state':state,'closure':proof[2],'state_transition_at':time.time()})
            return True

    def refresh(self):
        # Cached outcomes are not a completion authority. Select only real
        # transitions; unchanged sources need neither a write nor an event.
        with self.store.connect() as c:
            rows=list(c.execute('''SELECT p.source_id,p.source_revision FROM v2_product_sources p
                JOIN v2_active_sources a ON a.source_id=p.source_id AND a.source_revision=p.source_revision
                JOIN v2_sources s ON s.source_id=p.source_id AND s.revision=p.source_revision
                LEFT JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.revision
                LEFT JOIN v2_source_closure h ON h.source_id=s.source_id AND h.source_revision=s.revision
                WHERE p.scheduler_run_id=? AND p.state IN ('INGESTED','COMPLETE') AND s.eos<>'TIMELAPSE_COVERED'
                AND p.state<>CASE WHEN o.status IN ('FAILED_FINAL','FAILED','ABORTED') THEN 'FAILED_FINAL'
                    WHEN s.eos IN ('EOS','VISUAL_SKIPPED') AND o.status IN ('COMPLETE','DEGRADED')
                         AND h.status='COMPLETE' THEN 'COMPLETE' ELSE 'INGESTED' END ''',(self.run,)))
        for row in rows:self.refresh_source(row[0],row[1])

    def reconcile_raw(self):
        with self.store.connect() as c:
            migrate(c)
            rows=[dict(r) for r in c.execute('''SELECT p.*,a.source_revision active_revision,s.eos
                FROM v2_product_sources p LEFT JOIN v2_active_sources a ON a.source_id=p.source_id
                LEFT JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
                WHERE p.scheduler_run_id=?''',(self.run,))]
        for row in rows:
            source=json.loads(row['source_json']);path=Path(source['source_path'])
            if source.get('media_kind')!='video' or path.suffix.lower() not in TAGS:continue
            # Successful visuals and already sealed skip revisions never rerun.
            if row['eos']=='EOS':continue
            if row['eos']=='VISUAL_SKIPPED':
                if row['state']=='NEW':
                    with self.store.connect() as c:
                        c.execute('BEGIN IMMEDIATE')
                        sealed=c.execute("SELECT 1 FROM v2_raw_visual_skips WHERE source_id=? AND source_revision=? AND status IN ('ACTIVE','COMPLETE')",
                                         (row['source_id'],row['active_revision'])).fetchone()
                        if not sealed:raise ContractError('raw_skip_handoff_seal_missing:'+row['source_id'])
                        self.store.assert_source_epoch(c,row['source_id'],row['active_revision'])
                        c.execute("UPDATE v2_product_sources SET state='INGESTED',source_revision=?,error=NULL WHERE scheduler_run_id=? AND source_id=?",
                                  (row['active_revision'],self.run,row['source_id']))
                        if row['attempt_id']:
                            c.execute("UPDATE v2_product_source_attempts SET status='SUCCESS',source_revision=?,finished_at=? WHERE attempt_id=?",
                                      (row['active_revision'],time.time(),row['attempt_id']))
                        self.store.emit(c,'RAW_SKIP_HANDOFF_RECONCILED',row['source_id'],row['active_revision'],1,
                                        {'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO','new_attempt_required':False})
                continue
            if row['state'] not in ('NEW','FAILED_FINAL'):continue
            if row['state']=='FAILED_FINAL' and row['error']!='decoder_unavailable':continue
            report=probe_raw(path)
            if report is None:continue
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self.store.assert_current_owner()
                old=c.execute('SELECT status FROM v2_raw_visual_skips WHERE source_id=?',(row['source_id'],)).fetchone()
                if old:continue
                previous_revision=(row['active_revision'] or '').removeprefix('aborted:')
                if previous_revision and c.execute('SELECT COUNT(*) FROM v2_artifacts WHERE source_id=? AND revision=?',
                    (row['source_id'],previous_revision)).fetchone()[0]:
                    raise ContractError('raw_skip_has_valid_artifacts:'+row['source_id'])
                c.execute('INSERT INTO v2_raw_visual_skips VALUES(?,?,?,?,NULL,?,?,?,?)',
                    (row['source_id'],self.run,self.epoch,row['active_revision'],'CONFIRMED',
                     'confirmed_raw_codec_visual_skip_audio_required',json.dumps(report,sort_keys=True),time.time()))
                c.execute("UPDATE v2_product_sources SET state='NEW',error='raw_visual_skip_audio_recovery' WHERE scheduler_run_id=? AND source_id=?",
                          (self.run,row['source_id']))
                self.store.emit(c,'RAW_VISUAL_SKIP_AUTHORIZED',row['source_id'],row['active_revision'] or '',1,
                    {'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO','audio_required':True,'probe':report})

    def ingested(self,sid,attempt,items):
        revision=next(r['source_revision'] for r in items if r['source_id']==sid)
        with self.store.connect() as c:
            skip=c.execute("SELECT 1 FROM v2_raw_visual_skips r JOIN v2_sources s ON s.source_id=r.source_id AND s.revision=r.source_revision JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE r.source_id=? AND r.source_revision=? AND r.status='ACTIVE' AND s.eos='VISUAL_SKIPPED'",
                           (sid,revision)).fetchone()
            if skip:self.store.assert_source_epoch(c,sid,revision)
        if skip:return ProductLifecycle.ingested(self,sid,attempt,items)
        return super().ingested(sid,attempt,items)


def produce_raw_video(driver,source):
    store=driver.store;sid=source['source_content_id'];path=Path(source['source_path'])
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_raw_visual_skips'").fetchone():return None
        skip=c.execute("SELECT * FROM v2_raw_visual_skips WHERE source_id=? AND status='CONFIRMED'",(sid,)).fetchone()
        if not skip:return None
        report=json.loads(skip['probe_json'])
    stat=path.stat()
    if (stat.st_size,stat.st_mtime_ns)!=(report['source_size'],report['source_mtime_ns']):
        raise ContractError('raw_source_changed_after_probe:'+sid)
    lease=None
    if driver.admission_scheduler:
        lease=driver.admission_scheduler.acquire(sid,'video_source_ingest',source_path=str(path),requires_original=True,
            scheduler_run_id=driver.owner['scheduler_run_id'],execution_epoch=driver.owner['execution_epoch'])
    try:
        from .v2_model_workers import sha_file
        sha=sha_file(path)
        if (path.stat().st_size,path.stat().st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):
            raise ContractError('raw_source_changed_during_fingerprint:'+sid)
        metadata={**source,'media_type':'video','visual_policy':'VISUAL_SKIPPED_RAW_VIDEO','raw_probe':report}
        revision=store.source_ready(sid,fingerprint((sha,'raw_visual_skip_v1')),sha,path,metadata,
                                    execution_attempt=source.get('_product_attempt_id'))
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE');store.assert_source_epoch(c,sid,revision)
            c.execute("UPDATE v2_sources SET eos='VISUAL_SKIPPED',final_sequence=-1,version=version+1,error='' WHERE source_id=? AND revision=?",
                      (sid,revision))
            c.execute("UPDATE v2_raw_visual_skips SET source_revision=?,status='ACTIVE',updated_at=? WHERE source_id=?",
                      (revision,time.time(),sid))
            store.emit(c,'SOURCE_VISUAL_SKIPPED',sid,revision,1,
                       {'reason_code':'VISUAL_SKIPPED_RAW_VIDEO','audio_required':True,'source_sha':sha})
        return {'source_id':sid,'source_revision':revision,'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO'}
    finally:
        if lease:driver.admission_scheduler.release(lease,reason='raw_visual_skip_published')


def close_audio_only(pipeline,source,revision,yield_hook=None):
    store=pipeline.store
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_raw_visual_skips'").fetchone():return None
        skip=c.execute("SELECT * FROM v2_raw_visual_skips WHERE source_id=? AND source_revision=? AND status IN ('ACTIVE','COMPLETE')",
                       (source,revision)).fetchone()
        if not skip:return None
        probe=json.loads(skip['probe_json'])
    pipeline.advance_events(cooperative=True,yield_hook=yield_hook)
    pipeline.index.advance(limit=8)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE');store.assert_source_epoch(c,source,revision)
        planner=getattr(store,'fanout_planner',None)
        if planner and planner.pending(c,source,revision):
            return {'status':'WAITING','reason':'required_fanout_not_fully_promoted'}
        rows=[dict(r) for r in c.execute('SELECT * FROM v2_current_jobs WHERE source_id=? AND source_revision=?',(source,revision))]
        jobs={stage:[j for j in rows if j['stage']==stage] for stage in ('audio_extract','vad','whisper','embedding')}
        for stage,js in jobs.items():
            if any(j['status']=='FAILED_FINAL' for j in js):
                return {'status':'FAILED_FINAL','reason_code':'raw_audio_required_worker_failed','stage':stage}
            expected=pipeline.contracts.get(stage)
            if expected and any(j['revision']!=expected['revision'] or j['provenance']!=json.dumps(expected,sort_keys=True) for j in js):
                raise ContractError('raw_audio_worker_contract_mismatch:'+stage)
        if len(jobs['audio_extract'])!=1 or jobs['audio_extract'][0]['status']!='DONE':
            return {'status':'WAITING','reason':'raw_audio_extract_incomplete'}
        audio=c.execute('SELECT * FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        if not audio or audio['status'] not in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY'):
            return {'status':'WAITING','reason':'raw_audio_branch_incomplete'}
        extracted=json.loads(jobs['audio_extract'][0]['result_json'])
        if (extracted['status']=='NO_AUDIO')!=(audio['status']=='NO_AUDIO'):
            return {'status':'WAITING','reason':'raw_audio_projection_stale'}
        if audio['status']=='NO_AUDIO' and probe['audio_stream_count']:
            return {'status':'FAILED_FINAL','reason_code':'raw_known_audio_stream_lost'}
        projected=json.loads(audio['artifact_json'])
        if projected!=extracted:
            return {'status':'WAITING','reason':'raw_audio_artifact_projection_stale'}
        if audio['status']=='NO_AUDIO' and (jobs['vad'] or jobs['whisper']):
            return {'status':'WAITING','reason':'raw_no_audio_has_unexpected_descendants'}
        if audio['status']!='NO_AUDIO':
            if len(jobs['vad'])!=1 or jobs['vad'][0]['status']!='DONE':
                return {'status':'WAITING','reason':'raw_vad_incomplete'}
            vad=jobs['vad'][0]
            if vad['input_sha']!=extracted.get('sha') or json.loads(vad['result_json'])['segments']!=json.loads(audio['segments_json']):
                return {'status':'WAITING','reason':'raw_vad_projection_stale'}
            segments=json.loads(audio['segments_json'])
            if (audio['status']=='SEGMENTS_READY')!=bool(segments):
                return {'status':'WAITING','reason':'raw_segment_status_projection_stale'}
            if len(jobs['whisper'])!=len(segments) or any(j['status']!='DONE' for j in jobs['whisper']):
                return {'status':'WAITING','reason':'raw_transcript_incomplete'}
        if any(j['status']!='DONE' for j in rows):return {'status':'WAITING','reason':'raw_audio_jobs_not_terminal'}
        indexed=[]
        for job in jobs['whisper']:
            doc=c.execute('SELECT * FROM v2_documents WHERE raw_result_ref=?',(job['execution_key'],)).fetchone()
            if not doc or doc['status']!='EMBEDDED':return {'status':'WAITING','reason':'raw_transcript_embedding_incomplete'}
            embedding=next((j for j in jobs['embedding'] if j['execution_key']==doc['embedding_ref']),None)
            if not embedding or embedding['status']!='DONE':return {'status':'WAITING','reason':'raw_embedding_revision_incomplete'}
            pipeline.index.index_document(c,doc['document_id'])
            coverage={r[0] for r in c.execute("SELECT index_kind FROM v2_index_coverage WHERE entity_id=? AND status='READY'",(doc['document_id'],))}
            if not {'fts','fusion','vector'}<=coverage:return {'status':'WAITING','reason':'raw_required_index_incomplete'}
            indexed.append((doc['document_id'],embedding['execution_key'],embedding['result_sha']))
        digest=fingerprint(('raw_audio_only_v1',probe,[(j['execution_key'],j['result_sha']) for j in rows],indexed))
        old=c.execute('SELECT join_digest,status FROM v2_source_closure WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        if not old or old['join_digest']!=digest or old['status']!='COMPLETE':
            c.execute("INSERT OR REPLACE INTO v2_source_closure VALUES(?,?,?,'COMPLETE')",(source,revision,digest))
            c.execute("UPDATE v2_raw_visual_skips SET status='COMPLETE',updated_at=? WHERE source_id=? AND source_revision=?",(time.time(),source,revision))
            for event in ('SOURCE_INDEXED','SOURCE_COMPLETE'):
                store.emit(c,event,source,revision,1,{'join_digest':digest,'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO','audio_status':audio['status']})
        return {'status':'COMPLETE','join_digest':digest,'visual_policy':'VISUAL_SKIPPED_RAW_VIDEO'}
