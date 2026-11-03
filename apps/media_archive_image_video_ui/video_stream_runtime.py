"""Independent VIDEO entry: feeder + fast queues + one local coordinator.

No ProductExecutionEntrypoint/run_stage_pass/advance_source invocation. All
connections are operation-local; inference and source policy are outside writer
transactions. A process-level flock prevents concurrent takeover of this DB.
"""
from .stop_provenance import child_run,child_spawn
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from .video_stream_ledger import Ledger,HotConsumers,StaleResult,TrackedConnection
from .video_commit_queue import RetryableWriterDeadline,WriterDeadlineRetry
from .video_stream_adapter import FrozenAdapters
from .video_stream_people import build_local
from .video_stream_profile import VideoProfile,enabled
from .video_spool_budget import ByteSpool
from .video_pipeline_eta import ServiceStats,ProjectCalibration,remaining_ms,critical_path_eta,source_priority
from .video_system_resources import SystemResources
from .video_resource_governor import ResourceGovernor
from .v2_model_workers import sha_file
from .v2_producer import fingerprint
from .v2_producer import SamplingPolicy
from .video_stream_adapter import EvidenceStore
from dataclasses import replace
from .v2_runtime import validate_evidence,ContractError
from .v2_documents import validate_result
from .video_qwen_contract import validate as qwen_validator


class Runtime:
    def __init__(self,workspace,contracts,handlers,*,profile=None,resume=False,environment=None,fixture_mode=False,resource_monitor=None,producer_variant='C'):
        if not enabled(environment):raise ValueError('VIDEO_PIPELINE_V2_STREAMING_is_off')
        self.profile=(profile or VideoProfile()).validate()
        if producer_variant!='C':raise ValueError('unknown_producer_variant')
        self.producer_variant=producer_variant
        for stage,value in handlers.items():
            count=len(value) if isinstance(value,list) else 1
            expected=getattr(self.profile,stage,1)
            if (not fixture_mode and count!=expected) or (stage=='qwen' and count!=1):
                raise ValueError('physical_worker_profile_mismatch:'+stage)
        self.root=Path(workspace).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.lock_file=(self.root/'runtime.lock').open('a')
        fcntl.flock(self.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.db=self.root/'video.sqlite'
        owner=uuid.uuid4().hex;epoch=1
        if self.db.exists():
            if not resume:raise ValueError('existing_workspace_requires_resume')
            with sqlite3.connect(self.db,timeout=2,factory=TrackedConnection) as c:
                c.execute('BEGIN IMMEDIATE')
                old=c.execute('SELECT owner,epoch FROM video_stream_control_v1').fetchone()
                if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE status='RUNNING' AND lease_until>? LIMIT 1",(time.time(),)).fetchone():
                    raise ValueError('old_live_lease_wait_for_expiry')
                epoch=old[1]+1
                c.execute('UPDATE video_stream_control_v1 SET owner=?,epoch=?,stopped=0',(owner,epoch))
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_source_leases'").fetchone():
                    # Exclusive process flock proves the old publisher cannot
                    # still own this experiment; clear ONLY its source leases.
                    c.execute('DELETE FROM v2_source_leases WHERE (source_id,source_revision) IN (SELECT source_id,revision FROM video_stream_inputs_v1)')
        self.ledger=Ledger(self.db,owner,epoch)
        self.handlers=handlers
        self.resource_monitor=resource_monitor
        self.adapters=FrozenAdapters(self.ledger,contracts);self.contracts=contracts
        self.ledger.recover_expired()
        from .stop_provenance import StopEvent
        self.stop=StopEvent(self.root);self.errors=[];self.threads=[]
        self.stats={stage:ServiceStats() for stage in handlers};self.stats_cursor=0
        self.ledger.admission.service.update({'yoloe':170.8,'openclip':41.5,'insightface':171.,'qwen':10740.,'ocr':5000.,'whisper':690.,'embedding':270.})
        self.ledger.admission.workers={stage:len(h) if isinstance(h,list) else 1 for stage,h in handlers.items()}
        self.coord_stats={stage:ServiceStats() for stage in ('selection','person_local','index')}
        self.resources=SystemResources();self.governor=ResourceGovernor();self.last_snapshot=0
        self.resource_thread=None;self.resource_errors=[]
        self.fixture_mode=fixture_mode;self.blocked={}
        # P1-RG changes admission/residency only, preserving evidence identity.
        operational_baseline_pins={'video_detached_coordination.py':'05d79e32b016a3195d98b7997679aa22da927d3d87ab6a62f072b1a2ee5dfdae','video_telemetry.py':'11dc8e0430d8095faa4741a4c2d5f0167f80166fc83acf9039001b7adc547a86','video_stream_adapter.py':'790f4fd025df6e30a62085c04fc4a0f02555ec0a4cc7377e08bbe7d2031c8650','video_pipe_reader.py': 'a5d4705e881220c7333ee72d872feb1acd0121683764979a7d831d55d49cc78a', 'video_stream_runtime.py': '5ce3f0e7bf4d18cae1a2f923baf8e415052192348df58abad45b7ebf419176c3', 'video_stream_ledger.py': '07e11bbb68614ae44925deafa23603bd9f7659f65589fd31151ee86da778031d', 'video_commit_queue.py': '4923003eb95fab1e17b80902826fc0394876e8e774c60ecb57613ba3c004451b'}
        self.pipeline_revision=fingerprint({name:operational_baseline_pins.get(name) or sha_file(Path(__file__).with_name(name)) for name in
            ('video_stream_runtime.py','video_stream_adapter.py','video_stream_people.py','video_pipe_reader.py','video_stream_ledger.py',
             'video_stream_commit.py','video_commit_queue.py','video_canonical_store.py','video_result_reader.py',
             'video_selection_proof.py','video_coordination.py','video_detached_coordination.py','video_stream_index.py',
             'video_peer_catalog.py','video_source_admission.py','video_telemetry.py')})
        self.budget=ByteSpool(self.db,self.root,self.profile.spool_hard_bytes)
        validators={stage:self.validator(stage) for stage in handlers}
        self.consumers=HotConsumers(self.ledger,handlers,validators=validators,
            capacities={stage:8 if stage=='openclip' else 4 for stage in handlers},input_verifier=self.verify_input,
            admission=None if fixture_mode else self.can_admit,claim_admission=None if fixture_mode else self.can_claim)
        from .stop_provenance import register_runtime_stop
        register_runtime_stop(self.stop)
        self.ledger.new_work_stop=self.stop
        self.stop.on_cutoff(self.consumers.stop_claims)
        with self.ledger.connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS video_stream_inputs_v1(source_id TEXT,revision TEXT,source_json TEXT,audio_json TEXT,PRIMARY KEY(source_id,revision));
              CREATE TABLE IF NOT EXISTS video_stream_stats_v1(job_id TEXT PRIMARY KEY,stage TEXT,service_ms REAL);
              CREATE TABLE IF NOT EXISTS video_stream_read_attempts_v1(attempt_id TEXT PRIMARY KEY,source_id TEXT,revision TEXT,path TEXT,status TEXT,result TEXT);
              CREATE TABLE IF NOT EXISTS video_stream_eta_calibration_v1(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS video_stream_eta_source_v1(source_id TEXT, revision TEXT, predicted_ms REAL,
                observed_at REAL NOT NULL, completed_at REAL, calibrated INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source_id,revision));''')
            c.execute('CREATE TABLE IF NOT EXISTS video_stream_coord_stats_v1(seq INTEGER PRIMARY KEY,stage TEXT,service_ms REAL)')
            # Restart preserves measured service history, not merely its
            # deduplication cursor. Imported successes have no new service time.
            self.observed_job_ids=set()
            for row in c.execute("SELECT job_id,stage,service_ms FROM video_stream_jobs_v1 WHERE status='DONE' ORDER BY rowid"):
                self.observed_job_ids.add(row['job_id'])
                if row['stage'] in self.stats and row['service_ms'] is not None:
                    self.stats[row['stage']].observe(row['service_ms'])
            for row in c.execute('SELECT stage,service_ms FROM video_stream_coord_stats_v1 ORDER BY seq'):
                self.coord_stats[row['stage']].observe(row['service_ms'])
            previous=c.execute('SELECT payload FROM video_stream_eta_calibration_v1 ORDER BY seq DESC LIMIT 1').fetchone()
            self.eta_sources={row['source_id']+':'+row['revision']:dict(row) for row in c.execute('SELECT * FROM video_stream_eta_source_v1')}
        self.calibration=ProjectCalibration(**json.loads(previous[0])) if previous else ProjectCalibration()
        performance_state=self.root/'performance-state.json'
        if performance_state.exists():
            historical=json.loads(performance_state.read_text())
            self.eta_sources=historical.get('eta_sources',self.eta_sources)
            self.calibration=ProjectCalibration(**historical.get('calibration',self.calibration.state()))
            for stage,state in historical.get('coordination',{}).items():
                if stage not in self.coord_stats:continue
                stat=self.coord_stats[stage]
                stat.values.extend(state['values']);stat.ewma=state['ewma'];stat.completed_count=state['completed_count']
        self.sources=[]
        self.publishers={}

    def can_admit(self,stage,*,maintenance=True):
        if stage in ('frame_basics','audio_extract'):return True
        system=self.resources.cached
        self.governor.update(system)
        lifecycle=getattr(self,'lifecycle',None)
        resident=False
        if lifecycle is not None and any(m.stage==stage for m in lifecycle.models.values()):
            lifecycle.quiescent_check=self.resource_quiescent
            lifecycle.idle_resource_check=self.resource_idle
            if maintenance:lifecycle.resource_tick(self.governor)
            resident=any(m.stage==stage and m.resident and not m.loading and m.state not in ('LOADING','UNLOADING')
                         for m in lifecycle.models.values())
        reason=self.governor.reason_for(stage,resident=resident)
        if reason is None and lifecycle is not None:
            reason=lifecycle.resource_reason(stage)
        previous=self.blocked.get(stage)
        self.blocked[stage]=reason
        if stage=='whisper' and previous and reason is None:
            # Record the actual successful admission decision, separately from
            # the resource sample's older timestamp. Never changes admission.
            try:
                from .video_telemetry import append
                append(self.root/'whisper-admission-events.jsonl',dict(event='WHISPER_RESOURCE_ALLOWED',
                    timestamp=time.time(),previous_reason=previous,resident=resident))
            except OSError:pass
        return reason is None

    def can_claim(self,stage):
        if stage!='qwen':return self.can_admit(stage)
        # Existing controller and shared refill retain residency maintenance.
        # The owned Qwen worker evaluates the SAME resource/model gates without
        # synchronously scanning/retiring unrelated model stages before claim.
        from .video_telemetry import ClosureSpan
        with ClosureSpan(self.root,'QWEN_CLAIM_ADMISSION','Runtime.can_claim'):
            return self.can_admit(stage,maintenance=False)

    def resource_quiescent(self,stage):
        # A failed durable commit can leave RUNNING after a consumer exits.
        # Never treat the released Python lease alone as completion evidence.
        with self.ledger.connection() as c:
            return not c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE stage=? AND status='RUNNING' LIMIT 1",(stage,)).fetchone()

    def resource_idle(self,stage):
        # Temporary retirement never seals the upstream or changes a job.
        with self.ledger.connection() as c:
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE stage=? AND status IN ('PENDING','RUNNING','RETRY_PENDING') LIMIT 1",(stage,)).fetchone():return False
            if c.execute("SELECT 1 FROM v2_jobs WHERE stage=? AND status NOT IN ('DONE','FAILED_FINAL','SUPERSEDED','SKIPPED') LIMIT 1",(stage,)).fetchone():return False
        proof=self.lifecycle.proofs.get(stage)
        return proof is not None and not (proof.pending or proof.running or proof.retry_pending or proof.unpublished)

    def validator(self,stage):
        if stage=='qwen':return qwen_validator
        if stage=='frame_basics':
            def basics(value):
                for sub in ('dhash','geometry','quality'):validate_evidence(sub,value[sub])
            return basics
        if stage in ('yoloe','openclip'):return lambda value:validate_evidence(stage,value)
        if stage=='whisper':
            def transcript(value):
                if not isinstance(value.get('text'),str):raise ContractError('text_result_missing')
            return transcript
        return lambda value:validate_result(stage,value)

    def verify_input(self,job):
        with self.ledger.connection() as c:
            self.ledger.fence(c,allow_stop=True)
            row=c.execute('SELECT source_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(job['source_id'],job['revision'])).fetchone()
        source=json.loads(row[0]);stat=Path(source['path']).stat()
        if [stat.st_size,stat.st_mtime_ns,stat.st_ino]!=source['stat']:
            raise StaleResult('source_stat_identity_changed')
        payload=job['payload']
        path=payload.get('image_path') or payload.get('derived_path') or payload.get('path')
        if path:
            expected=payload.get('_file_sha') or payload.get('derived_sha') or payload.get('sha') or job['input_sha']
            if sha_file(path)!=expected:raise StaleResult('input_digest_changed')
        elif 'text' in payload and fingerprint(payload['text'])!=job['input_sha']:
            raise StaleResult('text_input_digest_changed')

    def add_source(self,source):
        """Caller supplies frozen Step01 content SHA, never scan original twice."""
        stat=Path(source['path']).stat();source={**source,'stat':[stat.st_size,stat.st_mtime_ns,stat.st_ino]}
        with self.ledger.connection() as c:
            previous=c.execute('SELECT i.revision,i.source_json,s.policy_json FROM video_stream_inputs_v1 i JOIN video_stream_sources_v1 a ON a.source_id=i.source_id AND a.revision=i.revision JOIN v2_sources s ON s.source_id=i.source_id AND s.revision=i.revision WHERE i.source_id=?',(source['source_id'],)).fetchone()
        if previous:
            frozen=json.loads(previous['source_json'])
            if any(frozen.get(k)!=source.get(k) for k in ('path','digest','stat')):raise StaleResult('source_identity_changed_requires_new_revision')
            revision=previous['revision']
            policy=json.loads(previous['policy_json'])
            if policy.get('explicit_timestamps_ms') is not None:policy['explicit_timestamps_ms']=tuple(policy['explicit_timestamps_ms'])
            self.publishers[revision]=EvidenceStore(self.ledger,SamplingPolicy(**policy))
            if not any(r==revision for _,r in self.sources):self.sources.append((frozen,revision))
            return revision
        meta={'media_type':'video','source_relative_path':source.get('relative_path',Path(source['path']).name),'source_stat':source['stat'],'pipeline_revision':self.pipeline_revision}
        revision=self.adapters.store.source_ready(source['source_id'],source['digest'],source['digest'],source['path'],meta)
        self.ledger.source(source['source_id'],revision)
        with self.ledger.write_connection() as c:
            c.execute('INSERT OR IGNORE INTO video_stream_inputs_v1 VALUES(?,?,?,NULL)',(source['source_id'],revision,json.dumps(source,sort_keys=True)))
        self.sources.append((source,revision))
        self.publishers[revision]=self.adapters.store
        return revision

    def frame(self,source,revision,path,slot):
        aid=self.publishers.get(revision,self.adapters.store).publish(source,revision,slot,path)
        with self.adapters.store.connect() as c:
            artifact=dict(c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(aid,)).fetchone())
        payload={'frame_id':aid,'visual_unit_id':aid,'artifact_id':aid,'derived_path':artifact['path'],
            'derived_sha':artifact['sha'],'sampled_sequence_index':slot,'time_position_ms':artifact['time_ms'],
            'width':artifact['width'],'height':artifact['height']}
        for stage in ('frame_basics','yoloe','openclip','insightface'):
            contract=self.contracts[stage] if stage!='frame_basics' else {'revision':1,'parts':{s:self.contracts[s] for s in ('dhash','geometry','quality')}}
            self.ledger.publish(source,revision,stage,artifact['sha'],contract,payload)
        return aid

    def feed_existing(self,source,revision,manifest):
        # Reuse owned valid derivatives; no original-media read or new FFmpeg.
        publisher=self.publishers.get(revision,self.adapters.store)
        with publisher.source_lease(source['source_id'],revision,lease_seconds=600):
            for slot,row in enumerate(manifest['frames_manifest']):
                if sha_file(row['path'])!=row['sha256']:raise ValueError('reused_frame_corrupt')
                self.frame(source['source_id'],revision,row['path'],slot)
            publisher.finish(source['source_id'],revision,len(manifest['frames_manifest'])-1,success=True)
        self.publish_audio(source,revision,manifest.get('audio_path'))
        self.adapters.state(source['source_id'],revision,'EXTRACTION_DONE')

    def feed(self,source,revision):
        if self.adapters.has_state(source['source_id'],revision,'EXTRACTION_DONE') or self.adapters.has_state(source['source_id'],revision,'VISUAL_FAILED_FINAL'):return revision
        while not self.fixture_mode and not self.stop.is_set() and not self.can_admit('raw_producer'):
            self.stop.wait(.1)  # True pressure/stale telemetry, never source count.
        if self.stop.is_set():return revision
        from .video_pipe_reader import run,DecodeFailure
        from .video_stream_reader import probe
        try:info=probe(source['path'],stop=self.stop)
        except subprocess.TimeoutExpired as error:
            # No reservation, extraction or result mutation has begun here.
            error.build151_source_preflight_timeout=True
            raise
        duration=info['duration_seconds']
        visual_only=getattr(self,'branch_visual_only',False)
        if visual_only:info={**info,'has_audio':False}
        publisher=self.publishers.get(revision,self.adapters.store)
        if duration<=2 and publisher.policy.explicit_timestamps_ms is None:
            if duration<.5:raise ContractError('below_frozen_short_video_minimum')
            timestamp=round((1.5 if duration>=1.5 else duration/2)*1000)
            publisher=EvidenceStore(self.ledger,replace(SamplingPolicy(),explicit_timestamps_ms=(timestamp,)))
            stat=Path(source['path']).stat();source={**source,'duration_seconds':duration,'stat':[stat.st_size,stat.st_mtime_ns,stat.st_ino]}
            metadata={'media_type':'video','source_relative_path':source.get('relative_path',Path(source['path']).name),'source_stat':source['stat'],'pipeline_revision':self.pipeline_revision,'short_sampling_contract':'explicit_legacy_short_video_sampling'}
            old=revision
            revision=publisher.source_ready(source['source_id'],source.get('content_revision',source['digest']),source['digest'],source['path'],metadata)
            self.ledger.source(source['source_id'],revision)
            with self.ledger.write_connection() as c:c.execute('INSERT OR IGNORE INTO video_stream_inputs_v1 VALUES(?,?,?,NULL)',(source['source_id'],revision,json.dumps(source,sort_keys=True)))
            self.sources=[(source,revision) if rev==old else (s,rev) for s,rev in self.sources]
            self.publishers[revision]=publisher
        # Recover the narrow crash window after reader commit and before EOS
        # publication without another decoder launch or duplicate model work.
        if visual_only:
            with self.ledger.connection() as c:
                completed_read=c.execute("SELECT result FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND status='EXTRACTION_DONE' ORDER BY rowid DESC LIMIT 1",(source['source_id'],revision)).fetchone()
            if completed_read:
                recovered=json.loads(completed_read[0])
                with publisher.source_lease(source['source_id'],revision,lease_seconds=3600):
                    publisher.finish(source['source_id'],revision,recovered['frames']-1,success=True)
                self.adapters.state(source['source_id'],revision,'VIDEO_BRANCH_EOS',{'at':recovered.get('video_eos_at',time.time())})
                self.adapters.state(source['source_id'],revision,'EXTRACTION_DONE',recovered)
                return revision
        # Replay only an interrupted producer, never any successful model.
        # Preserve its owned partial media and stable published frame paths.
        with self.ledger.connection() as c:
            prior=[dict(r) for r in c.execute("SELECT * FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND status<>'EXTRACTION_DONE'",(source['source_id'],revision))]
        for old in prior:
            directory=Path(old['path'])
            if old['status']=='RUNNING' and old['result'] and json.loads(old['result']).get('decode_mode'):
                raise ContractError('decoder_attempt_outcome_unknown_requires_review')
            pidfile=directory/'ffmpeg.pid'
            if pidfile.exists():
                pid=int(pidfile.read_text())
                try:os.kill(pid,0)
                except ProcessLookupError:pass
                else:raise ContractError('reader_recovery_child_still_alive')
            with self.ledger.connection() as c:
                owner=c.execute('SELECT token FROM video_spool_owners_v1 WHERE path=?',(str(directory.resolve()),)).fetchone()
            if owner:self.budget.archive(owner[0])
        attempt=uuid.uuid4().hex
        directory=self.root/'spool'/(revision+'-'+attempt)
        with self.ledger.write_connection() as c:c.execute('INSERT INTO video_stream_read_attempts_v1 VALUES(?,?,?,?,?,NULL)',(attempt,source['source_id'],revision,str(directory),'RUNNING'))
        with publisher.source_lease(source['source_id'],revision,lease_seconds=3600):
            with self.ledger.connection() as c:
                slots=[r[0] for r in c.execute('SELECT slot FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source['source_id'],revision))]
            if slots!=list(range(len(slots))):raise ContractError('decoder_replay_existing_slot_gap')
            count=[len(slots)]
            replay=[0]
            def ready(path,time_ms):
                if self.stop.is_set():return
                if replay[0]<count[0]:
                    if time_ms!=publisher.policy.timestamp(replay[0]):raise ContractError('decoder_replay_timestamp_conflict')
                    replay[0]+=1;return
                if time_ms!=publisher.policy.timestamp(count[0]):
                    raise ContractError('explicit_short_source_policy_adapter_required')
                self.frame(source['source_id'],revision,path,count[0]);count[0]+=1;replay[0]+=1
            def audio_ready(path,metadata):
                self.publish_audio(source,revision,path,native_metadata=metadata)
                self.adapters.state(source['source_id'],revision,'AUDIO_BRANCH_READY',{'at':time.time(),'metadata':metadata})
            # Completed failures are durable and revision scoped. A normal Stop
            # remains INTERRUPTED and resumable; it is never a decode failure.
            with self.ledger.connection() as c:
                failures=[json.loads(r[0]) for r in c.execute("SELECT result FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND status='DECODE_FAILED' ORDER BY rowid",(source['source_id'],revision))]
            mode='software' if failures else 'videotoolbox'
            while True:
                replay[0]=0
                try:
                    if any(f['decode_mode']=='software' for f in failures):
                        raise DecodeFailure(failures[-1])
                    with self.ledger.write_connection() as c:c.execute('UPDATE video_stream_read_attempts_v1 SET result=? WHERE attempt_id=?',(json.dumps({'decode_mode':mode,'status':'STARTING','at':time.time()}),attempt))
                    result=run(source['path'],directory,on_frame=ready,stop=self.stop,reservation=self.budget,probe_info=info,
                        on_audio_ready=audio_ready,variant='C',decode_mode=mode)
                except DecodeFailure as error:
                    evidence={**error.evidence,'source_id':source['source_id'],'revision':revision,'original_path':source['path']}
                    # A legacy combined reader is outside source-local isolation.
                    if not visual_only:raise
                    if evidence not in failures:failures.append(evidence)
                    with self.ledger.write_connection() as c:c.execute("UPDATE video_stream_read_attempts_v1 SET status='DECODE_FAILED',result=? WHERE attempt_id=?",(json.dumps(evidence),attempt))
                    with (self.root/'decoder-recovery.jsonl').open('a') as log:log.write(json.dumps(dict(event='DECODE_FAILED',**evidence))+'\n')
                    if mode=='videotoolbox' and not self.stop.is_set():
                        # Retain partial frames/artifacts, release only active
                        # reservation via existing inode-pinned archival path.
                        self.budget.archive(evidence['reservation_token'])
                        mode='software';attempt=uuid.uuid4().hex
                        directory=self.root/'spool'/(revision+'-'+attempt)
                        with self.ledger.write_connection() as c:c.execute('INSERT INTO video_stream_read_attempts_v1 VALUES(?,?,?,?,?,NULL)',(attempt,source['source_id'],revision,str(directory),'RUNNING'))
                        continue
                    if self.stop.is_set():return revision
                    final=dict(source_id=source['source_id'],revision=revision,original_path=source['path'],
                        classification='SOURCE_LOCAL_PERMANENT_DECODE_FAILURE',first_failure=failures[0],
                        attempts=failures,at=time.time(),audio_policy='CONTINUE_INDEPENDENT_BRANCH')
                    publisher.finish(source['source_id'],revision,count[0]-1,success=False,error='visual_decode_failed_final')
                    self.adapters.state(source['source_id'],revision,'VISUAL_FAILED_FINAL',final)
                    return revision
                except BaseException as error:
                    with self.ledger.write_connection() as c:c.execute("UPDATE video_stream_read_attempts_v1 SET status='INTERRUPTED',result=? WHERE attempt_id=?",(json.dumps({'reason':str(error)}),attempt))
                    raise
                result['decode_failures']=failures
                if result['status']=='EXTRACTION_DONE' and replay[0]<count[0]:raise ContractError('decoder_replay_prefix_incomplete')
                with self.ledger.write_connection() as c:c.execute('UPDATE video_stream_read_attempts_v1 SET status=?,result=? WHERE attempt_id=?',(result['status'],json.dumps(result),attempt))
                if result['status']!='EXTRACTION_DONE':return revision
                break
            publisher.finish(source['source_id'],revision,count[0]-1,success=True)
        if not visual_only and not result.get('audio_path'):
            self.publish_audio(source,revision,result.get('audio_path'))
        self.adapters.state(source['source_id'],revision,'VIDEO_BRANCH_EOS',{'at':result.get('video_eos_at',time.time())})
        self.adapters.state(source['source_id'],revision,'EXTRACTION_DONE',result)
        return revision

    def publish_audio(self,source,revision,path,native_metadata=None):
        # A failed video reader may already have durably completed its
        # independent audio branch. Reuse that closed artifact and identity;
        # remuxing the same audio creates a different container SHA/path.
        with self.ledger.connection() as c:
            row=c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(source['source_id'],revision)).fetchone()
            previous=json.loads(row[0]) if row and row[0] else None
            retained=c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage='audio_extract' AND input_sha=? AND contract=? LIMIT 1",(source['source_id'],revision,source['digest'],json.dumps(self.contracts['audio_extract'],sort_keys=True,ensure_ascii=False))).fetchone()
        if previous and retained:
            if previous.get('status')=='READY':
                from .video_audio_recovery import owned_audio_path
                artifact=owned_audio_path(self.ledger.path,previous['path'])
                if not artifact.is_file() or sha_file(artifact)!=previous['sha']:raise ContractError('retained_audio_artifact_changed')
            return
        value={'status':'NO_AUDIO'}
        if path:
            if native_metadata is not None:
                value={'status':'READY','path':str(path),'sha':sha_file(path),
                    'sample_rate':int(native_metadata['sample_rate']),'channels':native_metadata.get('channels'),
                    'codec':native_metadata.get('codec_name'),'representation':'source-native-transient-v1'}
            else:
                value=self.model_audio(source,revision,path)
        with self.ledger.write_connection() as c:
            c.execute('UPDATE video_stream_inputs_v1 SET audio_json=? WHERE source_id=? AND revision=?',(json.dumps(value),source['source_id'],revision))
        self.ledger.publish(source['source_id'],revision,'audio_extract',source['digest'],self.contracts['audio_extract'],{**value,'_file_sha':value.get('sha')})

    def model_audio(self,source,revision,path):
            out=self.root/'audio'/f'{revision}.wav';out.parent.mkdir(parents=True,exist_ok=True)
            if not out.exists():
                child_run(self.stop,['ffmpeg','-nostdin','-v','error','-n','-i',str(path),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(out)],check=True,timeout=300)
            return {'status':'READY','path':str(out),'sha':sha_file(out),'sample_rate':16000}

    def dispatch(self):
        try:
            while not self.stop.is_set():
                self.consumers.refill()
                if self.consumers.stop.is_set() and not self.stop.is_set():raise RuntimeError('consumer_database_commit_blocked')
                self.stop.wait(.02)
        except Exception as error:self.errors.append(str(error));self.stop.request('PROCESSING_FATAL','RUNTIME_THREAD_EXCEPTION',error=str(error),traceback=__import__('traceback').format_exc())

    def index_coordinate(self):
        # Own adapter/index objects: no mutable delivery snapshot or cursor
        # shared with the closure coordinator. All durable writes use one ACK
        # queue; only this bounded preparation consumer is additional.
        import copy
        from .video_source_admission import SourceAdmission
        store=copy.copy(self.adapters.store);store.index_affinity=SourceAdmission(self.root/'source-switches.jsonl')
        index=copy.copy(self.adapters.pipeline.index);index.store=store
        index._delivery_snapshot=None;index._join_snapshot_enabled=False;index._retired_in_delivery=frozenset()
        self.index_store=store;self.index_lane={'concurrency':1,'batch_limit':32,'state':'READY','active_source':None}
        deadline_retry=WriterDeadlineRetry()
        store.lane_report=lambda source,revision,n,remaining:self.index_lane.update(active_source=source,revision=revision,batch_size=n,source_remaining=remaining)
        store.index_phase=lambda phase:self.index_lane.update(state=phase,phase_at=time.time())
        try:
            while not self.stop.is_set():
                if not self.fixture_mode and (self.governor.tier>=3 or (self.resources.cached or {}).get('memory_pressure_level',0) in (4,)):
                    self.index_lane['state']='RESOURCE_PAUSED';store.index_affinity.release('index','memory_pressure');self.stop.wait(.2);continue
                self.index_lane['state']='PREPARING'
                store.index_affinity.completion={sid+':'+rev:self.ledger.admission.completion.get(sid,0) for item,rev in self.sources for sid in (item['source_id'],)}
                try:
                    started=time.monotonic()
                    count=index.advance(limit=deadline_retry.batch_limit)
                    if count:deadline_retry.committed()
                    self.index_lane.update(batch_limit=deadline_retry.batch_limit,deadline_retries=deadline_retry.failures)
                    if count:
                        previous=store.index_affinity.service.get('index',(time.monotonic()-started)*1000/count)
                        store.index_affinity.service['index']=.8*previous+.2*(time.monotonic()-started)*1000/count
                    self.index_lane.update(state='READY' if count else 'IDLE')
                except RetryableWriterDeadline:
                    delay=deadline_retry.failed()
                    self.index_lane.update(state='DEADLINE_BACKOFF',batch_limit=deadline_retry.batch_limit,deadline_retries=deadline_retry.failures)
                    with (self.root/'writer-deadline-retries.jsonl').open('a') as stream:
                        stream.write(json.dumps({'at':time.time(),'retry':deadline_retry.failures,'backoff_seconds':delay,'batch_limit':deadline_retry.batch_limit,'reason':'RETRYABLE_WRITER_DEADLINE'})+'\n')
                    if self.stop.wait(delay):break
                    continue
                except ContractError as error:
                    if str(error)!='coordination_snapshot_changed_retry':raise
                    self.index_lane['state']='STALE_RETRY';self.stop.wait(.05)
                self.stop.wait(.02)
        except Exception as error:self.errors.append('index_lane:'+str(error));self.stop.request('PROCESSING_FATAL','INDEX_LANE_EXCEPTION',error=str(error),traceback=__import__('traceback').format_exc())

    def coordination_sources(self):
        # Only sources whose local preselection gate is met compete for this
        # preparation lane. Identity/status facts come from SQLite; ownership
        # and completion-biased admission stay in runtime memory.
        with self.ledger.connection() as c:
            eligible={r[0] for r in c.execute("SELECT s.source_id FROM video_stream_sources_v1 s WHERE EXISTS(SELECT 1 FROM video_stream_source_states_v1 x WHERE x.source_id=s.source_id AND x.revision=s.revision AND x.state='EXTRACTION_DONE') AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 x WHERE x.source_id=s.source_id AND x.revision=s.revision AND x.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')) AND (EXISTS(SELECT 1 FROM video_stream_jobs_v1 j WHERE j.source_id=s.source_id AND j.revision=s.revision AND j.status='TERMINAL_FAILED') OR NOT EXISTS(SELECT 1 FROM video_stream_jobs_v1 j WHERE j.source_id=s.source_id AND j.revision=s.revision AND j.stage IN ('frame_basics','yoloe','openclip') AND j.status<>'DONE'))")}
        remaining=[(s,rev) for s,rev in list(self.sources) if s['source_id'] in eligible]
        while remaining and not self.stop.is_set():
            rows=[{'source_id':s['source_id'],'sequence':i} for i,(s,rev) in enumerate(remaining)]
            chosen=self.ledger.admission.order('source_coordination',rows)[0]['source_id']
            position=next(i for i,(s,rev) in enumerate(remaining) if s['source_id']==chosen)
            item=remaining.pop(position);self.ledger.admission.claimed('source_coordination',chosen)
            started=time.monotonic()
            try:yield item
            finally:
                self.ledger.admission.service['source_coordination']=(time.monotonic()-started)*1000
                done=self.adapters.has_state(chosen,item[1],'SOURCE_COMPLETE') or self.adapters.has_state(chosen,item[1],'SOURCE_FAILED_FINAL')
                self.ledger.admission.release('source_coordination','stage_complete' if done else 'upstream_or_dependency_blocked')

    def coordinate(self,people_threshold):
        raise RuntimeError('BUILD151_REQUIRES_MIXED_STAGE_LOCAL_SERVICES')

    def measure_coord(self,stage,call,only_when_result=False):
        started=time.monotonic();result=call();elapsed=(time.monotonic()-started)*1000
        if not only_when_result or result:
            self.coord_stats[stage].observe(elapsed)
            with (self.root/'performance.jsonl').open('a') as stream:
                stream.write(json.dumps({'timestamp':time.time(),'stage':stage,'service_ms':elapsed})+'\n')
        return result

    def snapshot(self):
        with self.ledger.connection() as c:
            rows=[dict(r) for r in c.execute("SELECT job_id,stage,service_ms FROM video_stream_jobs_v1 WHERE status='DONE'") if r['job_id'] not in self.observed_job_ids]
            counts=[tuple(r) for r in c.execute('SELECT stage,status,count(*) FROM video_stream_jobs_v1 GROUP BY stage,status')]
            states=[tuple(r) for r in c.execute('SELECT source_id,state FROM video_stream_source_states_v1')]
            for row in rows:
                if row['stage'] in self.stats and row['service_ms'] is not None:self.stats[row['stage']].observe(row['service_ms'])
                self.observed_job_ids.add(row['job_id'])
            # A source obtains a calibration prediction only after its actual
            # selected work exists.  Unknown early source work is never made
            # up merely to display a countdown.
            selected_now={row['source_id'] for row in c.execute("SELECT source_id FROM video_stream_source_states_v1 WHERE state='SELECTION_DONE'")}
            for source,revision in self.sources:
                sid=source['source_id']
                if sid not in selected_now:
                    continue
                known=self.eta_sources.get(sid+':'+revision)
                if known:
                    continue
                by_stage={row['stage']:row['n'] for row in c.execute("SELECT stage,count(*) n FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status IN ('PENDING','RUNNING','RETRY_PENDING') GROUP BY stage",(sid,revision))}
                costs=[]
                for stage,count in by_stage.items():
                    stat=self.stats.get(stage)
                    workers=len(self.handlers[stage]) if isinstance(self.handlers.get(stage),list) else 1
                    estimate=remaining_ms(stat,count,workers) if stat else None
                    if estimate is None:
                        costs=[];break
                    costs.append(estimate)
                # A source's independent model branches overlap; this is an
                # intentionally conservative critical-path approximation.
                prediction=max(costs) if costs else None
                self.eta_sources[sid+':'+revision]={'source_id':sid,'revision':revision,'predicted_ms':prediction,'observed_at':time.time(),'calibrated':0}
            complete_now={(row['source_id'],row['revision']) for row in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state='SOURCE_COMPLETE'")}
            for sid,revision in complete_now:
                row=self.eta_sources.get(sid+':'+revision)
                if not row or row['calibrated'] or row['predicted_ms'] is None:
                    continue
                completed_at=time.time();actual=(completed_at-row['observed_at'])*1000
                completed_count=c.execute("SELECT count(DISTINCT source_id) FROM video_stream_source_states_v1 WHERE state='SOURCE_COMPLETE'").fetchone()[0]
                self.calibration.observe(row['predicted_ms'],actual,completed_count)
                row.update(completed_at=completed_at,calibrated=1)
            # Recompute a SMALL source/work aggregate once per snapshot, not a
            # lineage scan or a per-candidate SQL loop. Frozen measured priors
            # calibrate startup ranking; live service EWMA takes over.
            priors={'frame_basics':70.,'yoloe':170.8,'openclip':41.5,'insightface':171.}
            completion={}
            for item in c.execute("SELECT source_id,stage,count(*) n FROM video_stream_jobs_v1 WHERE status IN ('PENDING','RUNNING','RETRY_PENDING') GROUP BY source_id,stage"):
                stage=item['stage'];stat=self.stats.get(stage);service=stat.ewma if stat is not None and stat.ewma is not None else self.ledger.admission.service.get(stage,1.)
                completion[item['source_id']]=max(completion.get(item['source_id'],0),item['n']*service/max(1,self.ledger.admission.workers.get(stage,1)))
            self.ledger.admission.completion=completion
            estimates={}
            for item in c.execute("SELECT source_id,revision,stage,count(*) n FROM video_stream_jobs_v1 WHERE status IN ('PENDING','RUNNING','RETRY_PENDING') AND stage IN ('frame_basics','yoloe','openclip','insightface') GROUP BY source_id,revision,stage"):
                stage=item['stage'];stat=self.stats.get(stage)
                service=stat.ewma if stat is not None and stat.ewma is not None else priors[stage]
                workers=len(self.handlers[stage]) if isinstance(self.handlers.get(stage),list) else 1
                key=(item['source_id'],item['revision'])
                estimates[key]=max(estimates.get(key,0),item['n']*service/workers)
            for row in c.execute('SELECT * FROM video_stream_priority_v1').fetchall():
                score=source_priority(estimates.get((row['source_id'],row['revision']),0),time.time()-row['ready_at'])
                self.ledger.source_priorities[row['source_id'],row['revision']]=score
        remaining={s:sum(n for stage,status,n in counts if stage==s and status in ('PENDING','RUNNING','RETRY_PENDING')) for s in self.stats}
        future_frames=0;future_frames_known=True
        for source,rev in self.sources:
            if self.adapters.has_state(source['source_id'],rev,'EXTRACTION_DONE'):continue
            duration=source.get('duration_seconds')
            if duration is None:future_frames_known=False;continue
            expected=1 if .5<=duration<2 else SamplingPolicy().count(round(duration*1000))
            with self.ledger.connection() as c:emitted=c.execute('SELECT count(*) FROM v2_artifacts WHERE source_id=? AND revision=?',(source['source_id'],rev)).fetchone()[0]
            future_frames+=max(0,expected-emitted)
        for stage in ('frame_basics','yoloe','openclip','insightface'):
            if stage in remaining:remaining[stage]+=future_frames
        costs={s:remaining_ms(stat,remaining[s],len(self.handlers[s]) if isinstance(self.handlers[s],list) else 1) for s,stat in self.stats.items()}
        costs.update(selection=0,person_local=0,index=0)
        unselected_sources=[(s,rev) for s,rev in self.sources if not self.adapters.has_state(s['source_id'],rev,'SELECTION_DONE')]
        unselected=bool(unselected_sources)
        with self.ledger.connection() as c:
            selected=[tuple(r) for r in c.execute("SELECT s.source_id,j.stage,count(*) FROM video_stream_source_states_v1 s JOIN video_stream_jobs_v1 j ON j.source_id=s.source_id AND j.revision=s.revision WHERE s.state='SELECTION_DONE' AND j.stage IN ('qwen','ocr') GROUP BY s.source_id,j.stage")]
        # Empirical candidates-per-video, replaced by exact manifests on selection.
        selected_sources={sid for sid,state in states if state=='SELECTION_DONE'}
        calibrated=bool(selected_sources)
        future={stage:(sum(n for sid,s,n in selected if s==stage)/len(selected_sources))*len(unselected_sources) if calibrated else None for stage in ('qwen','ocr')}
        for stage in ('qwen','ocr'):
            if stage in self.stats and future[stage] is not None:costs[stage]=remaining_ms(self.stats[stage],remaining[stage]+future[stage],1)
        for stage,state in [('selection','SELECTION_DONE'),('person_local','LOCAL_PEOPLE_DONE')]:
            n=sum(not self.adapters.has_state(s['source_id'],rev,state) for s,rev in self.sources)
            costs[stage]=remaining_ms(self.coord_stats[stage],n,1)
        # A measured coordination allowance; never silently call it zero.
        unfinished=sum(not self.adapters.has_state(s['source_id'],rev,'SOURCE_COMPLETE') for s,rev in self.sources)
        costs['index']=remaining_ms(self.coord_stats['index'],unfinished,1)
        raw_eta=None if not future_frames_known or unselected and not calibrated else critical_path_eta(costs)
        complete_sources=sum(1 for source,rev in self.sources if self.adapters.has_state(source['source_id'],rev,'SOURCE_COMPLETE'))
        service_samples=sum(stats.completed_count for stats in self.stats.values())
        value={'at':time.time(),'counts':counts,'states':states,'remaining':remaining,'stats':{s:v.summary() for s,v in self.stats.items()},
            'eta_ms':self.calibration.display(raw_eta),'eta_raw_ms':raw_eta,'eta_reason':'UNPRODUCED_FRAME_COUNT_UNKNOWN' if not future_frames_known else 'UNSELECTED_WORK_NOT_YET_CALIBRATED' if unselected and not calibrated else 'EMPIRICAL_FUTURE_CANDIDATES' if unselected else 'MEASURED_REMAINING_WORK',
            'eta_confidence':self.calibration.confidence(complete_sources,service_samples),'eta_calibration':self.calibration.state(),
            'system':self.resources.cached or {'timestamp':time.time(),'memory_pressure_level':None,'swap_delta_bytes':None,'blocked_reason':'RESOURCE_TELEMETRY_NOT_READY'},'resource_governor':self.governor.snapshot(),'blocked_reason':dict(self.blocked),'fixture_mode':self.fixture_mode,'errors':self.errors}
        if not unselected:
            bounds=[]
            for percentile in ('P50','P95'):
                candidate={s:0 if remaining[s]==0 else None if self.stats[s].summary()[percentile] is None else remaining[s]*self.stats[s].summary()[percentile]/(len(self.handlers[s]) if isinstance(self.handlers[s],list) else 1) for s in self.stats}
                candidate.update(selection=0,person_local=0,index=0)
                bounds.append(critical_path_eta(candidate))
            value['eta_service_range_ms']=bounds
        else:value['eta_service_range_ms']=[None,None]
        value['eta_limitations']=['coordination_tail_estimated_per_unfinished_source','future_candidates_empirical_not_exact_until_selection']
        value['coordination_stats']={s:v.summary() for s,v in self.coord_stats.items()}
        value['future_candidate_estimates']=future
        if self.resource_monitor is not None:
            value['model_workers']=self.resource_monitor.sample_all()
            value['stage_footprints']=self.resource_monitor.stage_footprints
        value['model_cold_start_ms']={s:[getattr(h,'cold_start_ms',None) for h in (v if isinstance(v,list) else [v])] for s,v in self.handlers.items()}
        from .video_closure_progress import pending
        with self.ledger.connection() as c:
            value['model_remaining']={k:v for k,v in remaining.items() if v}
            value['closure_pending']=pending(c)
        (self.root/'runtime.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
        performance_state={'eta_sources':self.eta_sources,'calibration':self.calibration.state(),
            'coordination':{stage:{'values':list(stat.values),'ewma':stat.ewma,'completed_count':stat.completed_count} for stage,stat in self.coord_stats.items()}}
        temporary=self.root/'performance-state.json.tmp'
        temporary.write_text(json.dumps(performance_state,sort_keys=True));temporary.replace(self.root/'performance-state.json')
        with (self.root/'trajectory.jsonl').open('a') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
        self.last_snapshot=time.monotonic()

    def start(self,people_threshold):
        # Before any model/coordination thread can replay pending policy work.
        from .video_audio_recovery import recover
        if not getattr(self,'_audio_recovered_before_start',False):
            recover(self.adapters)
            self._audio_recovered_before_start=True
        def observe():
            while not self.stop.is_set():
                try:
                    system=self.resources.sample(self.root)
                    if self.stop.is_set():break
                    models=self.resource_monitor.sample_all() if self.resource_monitor else []
                    value={'at':time.time(),'system':system,'model_workers':models,'errors':self.resource_errors[-32:]}
                    value['audio_supply']={'producer':dict(getattr(self,'audio_producer_snapshot',{})),'capacity':dict(getattr(self,'audio_gate_snapshot',{})),'decision':dict(getattr(self,'audio_supply_decision',{}))}
                    # Independent live resource page data: no SQLite, no
                    # coordination dependency and no expensive vmmap helper.
                    tmp=self.root/'resources.json.tmp';tmp.write_text(json.dumps(value,ensure_ascii=False));tmp.replace(self.root/'resources.json')
                    lane={'at':time.time(),'models':self.ledger.admission.snapshot(),'index':dict(getattr(self,'index_lane',{})),
                        'ready_queue_depth':{stage:q.qsize() for stage,q in self.consumers.queues.items()},
                        'worker_count':{stage:len(h) if isinstance(h,list) else 1 for stage,h in self.handlers.items()},
                        'governor':self.governor.snapshot(),'system':system}
                    from .video_telemetry import errors
                    lane['telemetry_errors']=errors(self.root)+list(self.ledger.commit_queue.trace_errors)
                    if getattr(self,'index_store',None):lane['index_scheduler']=self.index_store.index_affinity.snapshot()
                    temporary=self.root/'lane-state.json.tmp';temporary.write_text(json.dumps(lane));temporary.replace(self.root/'lane-state.json')
                except Exception as error:
                    self.resource_errors.append(str(error));self.resource_errors=self.resource_errors[-32:]
                # SystemResources internally caches heavy system reads5s;
                # this observer is independent of slow source coordination.
                self.stop.wait(1)
        self.resource_thread=threading.Thread(target=observe,name='bounded-system-resource-observer',daemon=True)
        self.resource_thread.start()
        self.consumers.start()
        for name,call in (('fast-dispatch',self.dispatch),('index-preparation',self.index_coordinate),('source-coordination',lambda:self.coordinate(people_threshold))):
            thread=threading.Thread(target=call,name=name);thread.start();self.threads.append(thread)

    def close(self):
        if getattr(self,'_engine_close_complete',False):return
        self.stop.set()
        deadline=time.monotonic()+30
        for thread in self.threads:thread.join(timeout=max(0,deadline-time.monotonic()))
        if any(t.is_alive() for t in self.threads):raise TimeoutError('coordination_shutdown_not_safe')
        # Keep the writer and process lock while any owner may still publish.
        self.consumers.close()
        if self.resource_thread:
            self.resource_thread.join(timeout=2)
            if self.resource_thread.is_alive():raise TimeoutError('resource_observer_shutdown_not_safe')
        self.ledger.commit_queue.close()
        failures=[]
        def attempt(operation):
            try:operation()
            except Exception as error:failures.append(error)
        try:
            try:
                from .video_transient_audio import cleanup
                for source,revision in self.sources:
                    attempt(lambda source=source,revision=revision:cleanup(self,source['source_id'],revision))
            except Exception as error:failures.append(error)
            seen=set()
            for value in self.handlers.values():
                for handler in value if isinstance(value,list) else [value]:
                    if id(handler) in seen:continue
                    seen.add(id(handler))
                    close=getattr(handler,'close',None)
                    if close:attempt(close)
            # Owned work has drained; never reset or touch DONE.
            def release_claims():
                with self.ledger.write_connection() as c:
                    self.ledger.fence(c,allow_stop=True)
                    c.execute("UPDATE video_stream_jobs_v1 SET lease_until=? WHERE status='RUNNING'",(time.time(),))
            attempt(release_claims)
            attempt(self.snapshot)
        finally:
            try:self.lock_file.close()
            except Exception as error:failures.append(error)
            else:self._engine_close_complete=True
            self._engine_close_errors=[{'type':type(error).__name__,'message':str(error)} for error in failures]
        if failures:raise failures[0]

