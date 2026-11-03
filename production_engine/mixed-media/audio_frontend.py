"""Opt-in standalone AUDIO frontend for the accepted durable streaming engine.

No visual artifacts, visual selection, video EOS or model-specific scheduler.
Only NEW explicitly marked isolated workspaces may instantiate this frontend.
"""
from apps.media_archive_image_video_ui.stop_provenance import child_run,child_spawn
from dataclasses import dataclass, asdict
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from apps.media_archive_image_video_ui.video_stream_runtime import Runtime as Engine
from apps.media_archive_image_video_ui.video_stream_adapter import FrozenAdapters, EvidenceStore
from apps.media_archive_image_video_ui.v2_producer import fingerprint
from apps.media_archive_image_video_ui.v2_model_workers import sha_file
from apps.media_archive_image_video_ui.v2_runtime import ContractError


@dataclass(frozen=True)
class AudioPolicy:
    contract: str = 'standalone-audio-source-native-v1'
    explicit_timestamps_ms: None = None
    @property
    def fingerprint(self): return fingerprint(asdict(self))
    def revision(self, content): return fingerprint((content, self.fingerprint))
    def timestamp(self, slot): raise ContractError('AUDIO_HAS_NO_VISUAL_SLOTS')


def workspace_guard(workspace, fixture=False):
    if fixture:raise ValueError("FIXTURE_MODE_FORBIDDEN_IN_PRODUCTION")
    from production_workspace import validate_runtime
    validate_runtime(workspace)


def audio_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS mixed_audio_sources_v1(source_id TEXT PRIMARY KEY,revision TEXT NOT NULL,metadata TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS mixed_audio_projection_heads_v1(source_id TEXT,source_revision TEXT,manifest_sha TEXT NOT NULL,manifest TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(source_id,source_revision))')


def current_audio_manifest(store,c,source,revision,contracts):
    """Pure source-local validation. No index repair, model calls or I/O."""
    meta=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
    if not meta or json.loads(meta[0]).get('media_type')!='audio':raise ContractError('AUDIO_IDENTITY_REQUIRED')
    rows=[dict(r) for r in c.execute('SELECT * FROM v2_current_jobs WHERE source_id=? AND source_revision=?',(source,revision))]
    allowed={'audio_extract','vad','whisper','embedding'}
    if any(r['stage'] not in allowed for r in rows):raise ContractError('UNEXPECTED_AUDIO_VISUAL_WORK')
    if any(r['status']!='DONE' for r in rows):return None
    for r in rows:
        if json.loads(r['provenance'])!=contracts[r['stage']]:raise ContractError('AUDIO_CURRENT_CONTRACT_MISMATCH')
    jobs={s:[r for r in rows if r['stage']==s] for s in allowed}
    if len(jobs['audio_extract'])!=1 or len(jobs['vad'])!=1:return None
    extracted=json.loads(jobs['audio_extract'][0]['result_json'])
    if extracted.get('status')!='READY':raise ContractError('KNOWN_AUDIO_STREAM_LOST')
    vad=jobs['vad'][0];segments=json.loads(vad['result_json'])['segments']
    if vad['input_sha']!=extracted['sha']:raise ContractError('AUDIO_VAD_LINEAGE_MISMATCH')
    branch=c.execute('SELECT * FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
    if not branch or json.loads(branch['artifact_json'])!=extracted or json.loads(branch['segments_json'])!=segments:return None
    if branch['status']!=('SEGMENTS_READY' if segments else 'NO_SPEECH'):return None
    expected={fingerprint((source,revision,vad['execution_key'],segment)):segment for segment in segments}
    if len(expected)!=len(segments):raise ContractError('AUDIO_DUPLICATE_SEGMENT_IDENTITY')
    actual={r['item_id']:r for r in jobs['whisper']}
    if set(actual)!=set(expected):return None
    documents=[]
    for segment_id,segment in expected.items():
        job=actual[segment_id];payload=json.loads(job['payload']);result=json.loads(job['result_json'])
        if payload.get('vad_execution_key')!=vad['execution_key'] or any(payload.get(k)!=v for k,v in segment.items()):
            raise ContractError('AUDIO_TRANSCRIPT_LINEAGE_MISMATCH')
        doc=c.execute('SELECT * FROM v2_documents WHERE raw_result_ref=?',(job['execution_key'],)).fetchone()
        if not doc:return None
        if doc['source_id']!=source or doc['source_revision']!=revision:raise ContractError('AUDIO_DOCUMENT_SOURCE_MISMATCH')
        if result.get('asr_contract'):
            from asr_quality import CONTRACT,HARD,validate_packet
            validate_packet(result)
            if (result['source_start_ms'],result['source_end_ms'])!=(segment['start_ms'],segment['end_ms']):raise ContractError('AUDIO_USABLE_OWNING_CLIP_MISMATCH')
            if result['quality_status'] in HARD:return None
            timeline=result['usable_timeline']
            expected_range=(timeline[0]['source_start_ms'],timeline[-1]['source_end_ms']) if timeline else (None,None)
            if doc['text']!=result['usable_text'] or (doc['start_ms'],doc['end_ms'])!=expected_range:raise ContractError('AUDIO_USABLE_DOCUMENT_MISMATCH')
            provenance=json.loads(doc['provenance'])
            if provenance.get('asr_contract')!=CONTRACT or provenance.get('usable_timeline')!=timeline:raise ContractError('AUDIO_USABLE_TIMELINE_NOT_DURABLE')
        elif (doc['start_ms'],doc['end_ms'])!=(segment['start_ms'],segment['end_ms']):raise ContractError('AUDIO_DOCUMENT_TIMESTAMP_MISMATCH')
        if doc['status']=='SKIP_NO_TEXT':
            skip=c.execute("SELECT input_digest FROM v2_index_coverage WHERE entity_id=? AND index_kind='no-text-skip' AND status='READY'",(doc['document_id'],)).fetchone()
            if result.get('usable_text',result.get('text','not-empty')).strip() or doc['embedding_ref'] or not skip or skip[0]!=job['result_sha']:
                raise ContractError('INVALID_AUDIO_NO_TEXT_PROOF')
        elif doc['status']=='EMBEDDED':
            embedding=next((r for r in jobs['embedding'] if r['execution_key']==doc['embedding_ref']),None)
            if not embedding or embedding['item_id']!=doc['document_id'] or embedding['input_sha']!=fingerprint(doc['text']):return None
            coverage={r[0] for r in c.execute("SELECT index_kind FROM v2_index_coverage WHERE entity_id=? AND status='READY'",(doc['document_id'],))}
            if not {'fts','vector','fusion'}<=coverage:return None
        else:return None
        documents.append({'document_id':doc['document_id'],'raw_result_ref':job['execution_key'],
                          'embedding_ref':doc['embedding_ref'],'status':doc['status'],
                          'start_ms':doc['start_ms'],'end_ms':doc['end_ms']})
    all_documents={r[0] for r in c.execute('SELECT document_id FROM v2_documents WHERE source_id=? AND source_revision=?',(source,revision))}
    if all_documents!={d['document_id'] for d in documents}:raise ContractError('UNEXPECTED_AUDIO_DOCUMENT_LINEAGE')
    if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status IN ('PENDING','RUNNING','RETRY_PENDING','TERMINAL_FAILED') LIMIT 1",(source,revision)).fetchone():return None
    if c.execute("SELECT 1 FROM v2_outbox o WHERE aggregate_id=? AND event_type IN ('AUDIO_ARTIFACT_READY','EVIDENCE_READY') AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=o.event_id) LIMIT 1",(source+':'+revision,)).fetchone():return None
    return {'contract':'standalone-audio-projection-v1','source_id':source,'source_revision':revision,
            'audio_status':branch['status'],'segments':segments,'documents':documents,
            'results':sorted((r['execution_key'],r['result_sha']) for r in rows)}


class AudioAdapters(FrozenAdapters):
    def document_events(self,limit=16):return self.document_events_fair(limit=limit)
    def _document_route(self,c,event):
        p=event['payload']
        if event['event_type']=='EVIDENCE_READY' and p.get('stage')=='whisper' and (self.contracts['whisper'].get('usable_asr_contract') or p.get('result',{}).get('asr_contract')):
            return self.pipeline.documents.callback(c,event)
        return super()._document_route(c,event)
    def closure(self,source,revision):
        # Cheap WAITING is not completion authority. Do not hydrate/copy the
        # source on every tick while hundreds of known speech jobs are live.
        # The full fenced snapshot below revalidates every necessary fact.
        with self.store.connect() as c:
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status IN ('PENDING','RUNNING','RETRY_PENDING','TERMINAL_FAILED') LIMIT 1",(source,revision)).fetchone():return False
            if c.execute("SELECT 1 FROM v2_documents WHERE source_id=? AND source_revision=? AND status NOT IN ('EMBEDDED','SKIP_NO_TEXT') LIMIT 1",(source,revision)).fetchone():return False
            if c.execute("SELECT 1 FROM v2_documents d WHERE d.source_id=? AND d.source_revision=? AND d.status='EMBEDDED' AND (NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id AND x.index_kind='fts' AND x.status='READY') OR NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id AND x.index_kind='vector' AND x.status='READY') OR NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id AND x.index_kind='fusion' AND x.status='READY')) LIMIT 1",(source,revision)).fetchone():return False
        from apps.media_archive_image_video_ui.video_detached_coordination import run
        def prepare(store,c):
            manifest=current_audio_manifest(store,c,source,revision,self.contracts)
            if manifest is None:return False
            digest=fingerprint(manifest)
            previous=c.execute('SELECT manifest_sha FROM mixed_audio_projection_heads_v1 WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if previous:
                if previous[0]!=digest:raise ContractError('AUDIO_CLOSED_LINEAGE_CHANGED')
                return True
            c.execute("INSERT INTO mixed_audio_projection_heads_v1 VALUES(?,?,?,?,'COMPLETE')",(source,revision,digest,json.dumps(manifest,sort_keys=True)))
            c.execute("INSERT OR REPLACE INTO v2_source_closure VALUES(?,?,?,'COMPLETE')",(source,revision,digest))
            for state in ('AUDIO_COMPLETE','SOURCE_COMPLETE'):
                c.execute('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)',(source,revision,state,json.dumps({'manifest_sha':digest})))
                store.emit(c,state,source,revision,1,{'manifest_sha':digest,'media_kind':'audio'})
            return True
        return run(self.store,source,revision,'audio_source_closure',prepare)


class AudioFrontendMixin:
    def add_audio(self,row):
        if row['media_kind']!='audio':raise ValueError('AUDIO_SOURCE_REQUIRED')
        path=Path(row['path']);st=path.stat();sid=row['source_id']
        if [st.st_size,st.st_mtime_ns,st.st_ino]!=[row['bytes'],row['mtime_ns'],row['inode']]:raise ValueError('AUDIO_INVENTORY_CHANGED')
        with self.ledger.connection() as c:
            old=c.execute('SELECT i.revision,i.source_json FROM video_stream_inputs_v1 i JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision WHERE i.source_id=?',(sid,)).fetchone()
        codec=next((s.get('codec_name') for s in row['probe']['metadata']['streams'] if s.get('codec_type')=='audio'),None)
        if old:
            source=json.loads(old['source_json'])
            if source.get('usable_asr_contract')!=self.contracts['whisper'].get('usable_asr_contract'):raise ValueError('AUDIO_RESUME_CORE_CONTRACT_CHANGED')
            if source.get('audio_route_request')!=row.get('audio_route_request'):raise ValueError('AUDIO_RESUME_ROUTE_AUTHORITY_CHANGED')
            if source['stat']!=[st.st_size,st.st_mtime_ns,st.st_ino] or source.get('media_kind')!='audio':raise ValueError('AUDIO_RESUME_IDENTITY_CHANGED')
            if codec!='alac' or source.get('audio_container_policy')=='native-alac-mp4-v2':
                self.sources.append((source,old['revision']));return old['revision']
        digest=sha_file(path)
        after=path.stat()
        if (after.st_size,after.st_mtime_ns,after.st_ino)!=(st.st_size,st.st_mtime_ns,st.st_ino):raise ValueError('AUDIO_CHANGED_DURING_HASH')
        source={'source_id':sid,'path':str(path),'digest':digest,'relative_path':row['relative_path'],
                'stat':[st.st_size,st.st_mtime_ns,st.st_ino],'media_kind':'audio','probe':row['probe'],
                'audio_container_policy':'native-alac-mp4-v2' if codec=='alac' else 'native-matroska-v1'}
        if self.contracts['whisper'].get('usable_asr_contract'):
            source['usable_asr_contract']=self.contracts['whisper']['usable_asr_contract']
            if row.get('audio_route_request'):source['audio_route_request']=row['audio_route_request']
        publisher=EvidenceStore(self.ledger,AudioPolicy(contract='standalone-audio-guarded-asr-v2') if source.get('usable_asr_contract') else AudioPolicy())
        content=fingerprint((digest,source['audio_container_policy'])) if codec=='alac' else digest
        if source.get('usable_asr_contract'):content=fingerprint((content,self.contracts['whisper'],source.get('audio_route_request')))
        revision=publisher.source_ready(sid,content,digest,path,{'media_type':'audio','media_kind':'audio',
            'source_relative_path':row['relative_path'],'source_stat':source['stat'],'audio_probe':row['probe']})
        self.ledger.source(sid,revision)
        with self.ledger.connection() as c:
            c.execute('INSERT INTO video_stream_inputs_v1 VALUES(?,?,?,NULL)',(sid,revision,json.dumps(source,sort_keys=True)))
            c.execute('INSERT INTO mixed_audio_sources_v1 VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,metadata=excluded.metadata',(sid,revision,json.dumps(row['probe'],sort_keys=True)))
        self.sources.append((source,revision));return revision

    def prepare_audio(self,source,revision):
        sid=source['source_id']
        if self.adapters.has_state(sid,revision,'SOURCE_COMPLETE') or self.adapters.has_state(sid,revision,'AUDIO_INPUT_SEALED'):return
        with self.ledger.connection() as c:
            retained=c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(sid,revision)).fetchone()
        previous=json.loads(retained[0]) if retained and retained[0] else None
        directory=self.root/'audio';directory.mkdir(exist_ok=True)
        original_audio=next(s for s in source['probe']['metadata']['streams'] if s.get('codec_type')=='audio')
        # Prefer the native ALAC container; full decode validation below,
        # not the container suffix or a short probe, decides acceptability.
        suffix='.m4a' if original_audio.get('codec_name')=='alac' else '.mka'
        target=directory/(revision+suffix)
        if previous:
            target=Path(previous['path'])
            if not target.is_relative_to(self.root) or not target.is_file() or sha_file(target)!=previous['sha']:raise ValueError('AUDIO_RETAINED_ARTIFACT_CHANGED')
            metadata={'sample_rate':previous['sample_rate'],'channels':previous['channels'],'codec_name':previous['codec']}
        else:
            partial=directory/(revision+'.'+uuid.uuid4().hex+'.partial'+suffix)
            command=['ffmpeg','-nostdin','-v','error','-n','-i',source['path'],'-map','0:a:0','-vn','-sn','-dn','-c:a','copy',str(partial)]
            with (directory/(revision+'.stderr.log')).open('ab') as stderr:
                process=child_spawn(self.stop,command,stdout=subprocess.DEVNULL,stderr=stderr)
                (directory/(revision+'.pid')).write_text(str(process.pid))
                while process.poll() is None:
                    if self.stop.wait(.2):
                        process.terminate();process.wait(timeout=15);raise InterruptedError('AUDIO_PREPARATION_INTERRUPTED')
                if process.returncode:raise RuntimeError('AUDIO_DEMUX_FAILED:'+str(process.returncode))
            result=child_run(self.stop,['ffprobe','-v','error','-show_streams','-of','json',str(partial)],capture_output=True,text=True,timeout=30,check=True)
            streams=json.loads(result.stdout)['streams']
            if len(streams)!=1 or streams[0].get('codec_type')!='audio':raise ValueError('AUDIO_DEMUX_STREAM_INVALID')
            # Header-only ffprobe cannot establish decodability. These two
            # real ALAC originals also have a zero-sample packet rejected by
            # ffmpeg: container choice alone does NOT fix this backend issue.
            with (directory/(revision+'.decode-check.log')).open('ab') as stderr:
                check=child_spawn(self.stop,['ffmpeg','-nostdin','-v','error','-xerror','-i',str(partial),'-map','0:a:0','-f','null','-'],stdout=subprocess.DEVNULL,stderr=stderr)
                (directory/(revision+'.decode-check.pid')).write_text(str(check.pid))
                while check.poll() is None:
                    if self.stop.wait(.2):check.terminate();check.wait(timeout=15);raise InterruptedError('AUDIO_DECODE_CHECK_INTERRUPTED')
            if check.returncode:
                if original_audio.get('codec_name')!='alac' or not Path('/usr/bin/afconvert').is_file():raise ValueError('AUDIO_FULL_DECODE_FAILED:'+str(check.returncode))
                # Explicit platform decoder fallback, never ignore corruption.
                # Float32 preserves native PCM precision/rate/channels before
                # the unchanged shared model 16kHz/mono preprocessing.
                fallback=directory/(revision+'.'+uuid.uuid4().hex+'.coreaudio.wav')
                command=['/usr/bin/afconvert','-f','WAVE','-d','LEF32',source['path'],str(fallback)]
                with (directory/(revision+'.coreaudio.log')).open('ab') as stderr:
                    process=child_spawn(self.stop,command,stdout=subprocess.DEVNULL,stderr=stderr)
                    (directory/(revision+'.coreaudio.pid')).write_text(str(process.pid))
                    while process.poll() is None:
                        if self.stop.wait(.2):process.terminate();process.wait(timeout=15);raise InterruptedError('AUDIO_COREAUDIO_INTERRUPTED')
                    if process.returncode:raise ValueError('AUDIO_COREAUDIO_FAILED:'+str(process.returncode))
                result=child_run(self.stop,['ffprobe','-v','error','-show_streams','-of','json',str(fallback)],capture_output=True,text=True,timeout=30,check=True)
                native=json.loads(result.stdout)['streams']
                if len(native)!=1 or native[0]['codec_name']!='pcm_f32le' or int(native[0]['sample_rate'])!=int(original_audio['sample_rate']) or native[0]['channels']!=original_audio['channels']:raise ValueError('AUDIO_COREAUDIO_NATIVE_FORMAT_CHANGED')
                child_run(self.stop,['ffmpeg','-nostdin','-v','error','-xerror','-i',str(fallback),'-map','0:a:0','-f','null','-'],stdout=subprocess.DEVNULL,check=True,timeout=120)
                partial=fallback;target=directory/(revision+'.wav');streams=native
                self.adapters.state(sid,revision,'AUDIO_DECODER_FALLBACK',{'backend':'CoreAudio','primary_error':check.returncode,'representation':'native-rate-channels-float32'})
            metadata=streams[0];partial.replace(target)
        if source.get('usable_asr_contract'):
            from audio_routes import RouteRequest,AudioRouteUnavailable,execute,dsp_preflight
            directory=self.root/'audio-core'/sid;directory.mkdir(parents=True,exist_ok=True)
            probe=directory/'risk-preflight.wav'
            if not probe.exists():child_run(self.stop,['ffmpeg','-nostdin','-v','error','-n','-i',str(target),'-t','20','-ac','1','-ar','16000','-c:a','pcm_s16le',str(probe)],check=True,timeout=30)
            risk=dsp_preflight(probe)
            self.adapters.state(sid,revision,'AUDIO_RISK_PREFLIGHT',risk)
            request=source.get('audio_route_request')
            if not request:raise AudioRouteUnavailable('AUDIO_ROUTE_REVIEW_REQUIRED:inaSpeechSegmenter unavailable; DSP_POC UNKNOWN')
            result=execute(RouteRequest(**request),target,directory,getattr(self,'audio_route_providers',{}))
            result['transient_audio'].append({'path':str(probe),'kind':'RISK_PREFLIGHT_PCM'})
            for item in result['transient_audio']:item.update(source_id=sid,sha=sha_file(item['path']))
            self.adapters.state(sid,revision,'AUDIO_ROUTE_READY',result)
            if 'MUSIC_EVIDENCE_ONLY' in result['route']:raise AudioRouteUnavailable('MUSIC_ONLY_TERMINAL_ROUTE_REQUIRES_VERIFIED_CLASSIFIER')
            routed=Path(result['dialogue_path'])
            if routed!=target:
                data=child_run(self.stop,['ffprobe','-v','error','-show_streams','-of','json',str(routed)],capture_output=True,text=True,check=True,timeout=30)
                stream=json.loads(data.stdout)['streams']
                if len(stream)!=1 or stream[0].get('codec_type')!='audio':raise ValueError('CASS_DIALOGUE_STREAM_INVALID')
                metadata=stream[0]
            target=routed
        Engine.publish_audio(self,source,revision,target,native_metadata=metadata)
        self.adapters.state(sid,revision,'AUDIO_INPUT_SEALED',{'artifact_sha':sha_file(target),'media_kind':'audio'})


class AudioRuntime(AudioFrontendMixin,Engine):
    def __init__(self,workspace,contracts,handlers,*,resume=False,fixture_mode=False,resource_monitor=None,experimental_whisper_workers_2=False):
        workspace_guard(workspace,fixture_mode)
        if set(handlers)-{'audio_extract','vad','whisper','embedding'}:raise ValueError('AUDIO_ONLY_FORBIDS_VISUAL_MODELS')
        profile=None
        if experimental_whisper_workers_2:
            from audio_test_profile import ExperimentalAudioProfile
            profile=ExperimentalAudioProfile()
        super().__init__(workspace,contracts,handlers,profile=profile,resume=resume,fixture_mode=fixture_mode,resource_monitor=resource_monitor,environment={'VIDEO_PIPELINE_V2_STREAMING':'1'})
        self.adapters.__class__=AudioAdapters
        self.adapters.store.document_root_snapshots=True
        from audio_usable_documents import install,cleanup_paths
        install(self.adapters)
        self.extra_transient_audio_paths=lambda sid,rev:cleanup_paths(self,sid,rev)
        with self.ledger.connection() as c:
            audio_schema(c)
            c.execute('CREATE INDEX IF NOT EXISTS mixed_document_result_ref ON v2_documents(raw_result_ref)')
        self.snapshot_lock=threading.Lock()
        from apps.media_archive_image_video_ui.model_lifecycle import ModelLifecycleManager
        self.lifecycle=ModelLifecycleManager(self.root/'model-lifecycle.jsonl',self.describe_input,lambda message:(self.errors.append(message),self.stop.set()))
        for stage in ('vad','whisper','embedding'):
            if stage in handlers:
                value=handlers[stage]
                handlers[stage]=[self.lifecycle.register(stage,stage+'#'+str(n+1),h) for n,h in enumerate(value)] if isinstance(value,list) else self.lifecycle.register(stage,stage+'#1',value)
        self.lifecycle_thread=None
    def describe_input(self,stage,payload):
        with self.ledger.connection() as c:
            row=c.execute('SELECT source_id FROM v2_jobs WHERE execution_key=?',(payload.get('_evidence_key'),)).fetchone()
        source=next((s for s,r in self.sources if row and s['source_id']==row[0]),{})
        return {'stage':stage,'source_id':source.get('source_id'),'source_path':source.get('relative_path'),'media_kind':'audio','segment_id':payload.get('segment_id'),'document_id':payload.get('document_id')}
    def drain_proofs(self):
        from apps.media_archive_image_video_ui.model_lifecycle import DrainProof
        with self.ledger.connection() as c:
            c.execute('BEGIN')
            states={sid:{r[0] for r in c.execute('SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(sid,rev))} for source,rev in self.sources for sid in (source['source_id'],)}
            sealed=bool(self.sources) and all('AUDIO_INPUT_SEALED' in v for v in states.values())
            counts={s:dict(pending=0,running=0,retry_pending=0,unpublished=0) for s in ('audio_extract','vad','whisper','embedding')}
            for stage,status,n in c.execute("SELECT stage,status,count(*) FROM video_stream_jobs_v1 WHERE status IN ('PENDING','RUNNING','RETRY_PENDING') GROUP BY stage,status"):
                if stage in counts:counts[stage][status.lower()]=n
            for stage,n in c.execute("SELECT stage,count(*) FROM v2_jobs WHERE status IN ('PENDING','RUNNING','FAILED_RETRYABLE') GROUP BY stage"):
                if stage in counts:counts[stage]['unpublished']=n
            event_stages=[(r[0],r[1]) for r in c.execute("SELECT event_type,json_extract(payload,'$.payload.stage') FROM v2_outbox o WHERE event_type IN ('EVIDENCE_READY','AUDIO_ARTIFACT_READY') AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=o.event_id)")]
            events=len(event_stages)
            extract_events=sum(event=='AUDIO_ARTIFACT_READY' or stage=='audio_extract' for event,stage in event_stages)
            vad_events=sum(stage=='vad' for event,stage in event_stages)
            ready=c.execute("SELECT count(*) FROM v2_documents WHERE status='READY'").fetchone()[0]
            closed=all('SOURCE_COMPLETE' in s or 'SOURCE_FAILED_FINAL' in s for s in states.values())
        upstream=sealed and extract_events==0 and not any(counts['audio_extract'].values())
        gates={'vad':upstream,'whisper':upstream and vad_events==0 and not any(counts['vad'].values()),
               'embedding':sealed and closed and events==0 and ready==0 and not any(counts['whisper'].values())}
        return {s:DrainProof(gates[s],not gates[s],**counts[s],downstream_safe=gates[s],reason='global_audio_upstream_sealed' if gates[s] else 'audio_future_work_possible') for s in gates}
    def start(self,people_threshold=.58):
        super().start(people_threshold)
        def control():
            try:
                while not self.stop.is_set():
                    self.lifecycle.update(self.drain_proofs())
                    temp=self.root/'model-lifecycle.json.tmp';temp.write_text(json.dumps({'at':time.time(),'lifecycle':self.lifecycle.snapshot()}));temp.replace(self.root/'model-lifecycle.json')
                    self.stop.wait(1)
            except Exception as error:self.errors.append('AUDIO_LIFECYCLE:'+str(error));self.stop.set()
        self.lifecycle_thread=threading.Thread(target=control,name='audio-lifecycle');self.lifecycle_thread.start()
    def close(self):
        self.stop.set()
        if self.lifecycle_thread:self.lifecycle_thread.join(timeout=30)
        if not self.snapshot()['closure_pending'] and not self.snapshot()['model_remaining']:self.lifecycle.update(self.drain_proofs())
        super().close()
        # Audio-only runs do not use the video producer variants whose close
        # path performs this cleanup. Workers are joined before removing PCM.
        from apps.media_archive_image_video_ui.video_transient_audio import cleanup
        for source,revision in self.sources:
            cleanup(self,source['source_id'],revision)
        (self.root/'model-lifecycle.json').write_text(json.dumps({'at':time.time(),'lifecycle':self.lifecycle.snapshot(),'historical':True,'final_exit_state':True}))
    def coordinate(self,people_threshold):
        raise RuntimeError('BUILD151_REQUIRES_MIXED_STAGE_LOCAL_SERVICES')
    def snapshot(self):
        with self.snapshot_lock:return self._snapshot()
    def _snapshot(self):
        with self.ledger.connection() as c:
            counts=[tuple(r) for r in c.execute('SELECT stage,status,count(*) FROM video_stream_jobs_v1 GROUP BY stage,status')]
            complete={tuple(r) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state='SOURCE_COMPLETE'")}
            for row in c.execute("SELECT job_id,stage,service_ms FROM video_stream_jobs_v1 WHERE status='DONE'"):
                if row['job_id'] in self.observed_job_ids:continue
                self.observed_job_ids.add(row['job_id'])
                if row['stage'] in self.stats and row['service_ms'] is not None:self.stats[row['stage']].observe(row['service_ms'])
        value={'at':time.time(),'media_kind':'audio','counts':counts,'model_remaining':{},
               'closure_pending':{s['source_id']:{'audio_manifest':1} for s,r in self.sources if (s['source_id'],r) not in complete},'errors':self.errors,
               'service_stats':{s:v.summary() for s,v in self.stats.items()}}
        for stage,status,n in counts:
            if status in ('PENDING','RUNNING','RETRY_PENDING'):value['model_remaining'][stage]=value['model_remaining'].get(stage,0)+n
        temp=self.root/('runtime.json.'+str(threading.get_ident())+'.tmp');temp.write_text(json.dumps(value));temp.replace(self.root/'runtime.json');self.last_snapshot=time.monotonic();return value
