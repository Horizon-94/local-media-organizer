"""Stage03 artifact producer; independent of selection and inference."""
from __future__ import annotations
import hashlib
import json
import re
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

@dataclass(frozen=True)
class SamplingPolicy:
    offset_ms: int = 2000
    interval_ms: int = 3000
    long_edge: int = 1280
    jpeg_quality: int = 3
    decoder_policy: str = "videotoolbox_preferred_software_fallback"
    explicit_timestamps_ms: tuple[int,...] | None = None

    def __post_init__(self):
        if self.offset_ms < 0 or self.interval_ms <= 0 or self.long_edge <= 0 or not 1 <= self.jpeg_quality <= 31:
            raise ValueError("invalid_sampling_policy")
        if self.explicit_timestamps_ms is not None and (any(t<0 for t in self.explicit_timestamps_ms) or tuple(sorted(set(self.explicit_timestamps_ms)))!=self.explicit_timestamps_ms):raise ValueError('invalid_explicit_sampling_plan')

    @property
    def fingerprint(self):
        return fingerprint({"contract": "sampling-v2-legacy-fps-v1", **asdict(self)})

    def timestamp(self, slot):
        if slot < 0: raise ValueError("negative_slot")
        if self.explicit_timestamps_ms is not None:return self.explicit_timestamps_ms[slot]
        return self.offset_ms + slot * self.interval_ms

    def count(self, duration_ms):
        if self.explicit_timestamps_ms is not None:return len(self.explicit_timestamps_ms)
        return 0 if duration_ms < self.offset_ms else (duration_ms - self.offset_ms) // self.interval_ms + 1

    def revision(self, source_revision):
        return fingerprint((source_revision, self.fingerprint))

class ProducerConflict(RuntimeError):
    pass

def sampling_filter(policy):
    """Exact frozen extractor expression, including its square-image behavior.

    Timestamps label legacy sampled slots; they are not decoded PTS.
    Production uses the frozen extractor itself, not this fixture helper.
    """
    return (f"fps=1/{policy.interval_ms/1000}:start_time={policy.offset_ms/1000},"
            f"scale='if(gt(iw,ih),{policy.long_edge},-2)':'if(gt(ih,iw),{policy.long_edge},-2)'")

def sampling_command(ffmpeg,source,pattern,policy,decoder='software'):
    if decoder not in ('software','videotoolbox'):raise ProducerConflict('decoder_not_supported')
    hardware=['-hwaccel','videotoolbox'] if decoder=='videotoolbox' else []
    return [str(ffmpeg),'-hide_banner','-nostdin','-y',*hardware,'-i',str(source),
            '-map','0:v:0','-an','-sn','-dn','-vf',sampling_filter(policy),
            '-q:v',str(policy.jpeg_quality),str(pattern)]


def validate_jpeg(path):
    from PIL import Image
    data = Path(path).read_bytes()
    if not data or not data.endswith(b"\xff\xd9"): raise ValueError("jpeg_incomplete")
    with Image.open(path) as image:
        if image.format != "JPEG": raise ValueError("not_jpeg")
        image.verify()
    with Image.open(path) as image:
        image.load()
        return hashlib.sha256(data).hexdigest(), image.width, image.height

class ProducerStore:
    def __init__(self, db, policy=None, artifact_root=None, *, scheduler_owner=None):
        self.scheduler_owner = scheduler_owner
        self.db, self.policy = Path(db), policy or SamplingPolicy()
        self.artifact_root=Path(artifact_root) if artifact_root else None
        self._producer_tokens={}
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS v2_sources(source_id TEXT,revision TEXT,policy_hash TEXT,policy_json TEXT,
              produced_frontier INTEGER DEFAULT -1,eos TEXT DEFAULT 'OPEN',final_sequence INTEGER,
              version INTEGER DEFAULT 0,error TEXT,PRIMARY KEY(source_id,revision));
            CREATE TABLE IF NOT EXISTS v2_artifacts(artifact_id TEXT PRIMARY KEY,source_id TEXT,revision TEXT,
              slot INTEGER,time_ms INTEGER,sha TEXT,path TEXT,width INTEGER,height INTEGER,status TEXT,
              UNIQUE(source_id,revision,slot));
            CREATE TABLE IF NOT EXISTS v2_artifact_mappings(visual_unit_id TEXT PRIMARY KEY,
              derived_id TEXT UNIQUE,artifact_id TEXT REFERENCES v2_artifacts(artifact_id));
            CREATE TABLE IF NOT EXISTS v2_sample_slots(source_id TEXT,source_revision TEXT,slot INTEGER,
              timestamp_ms INTEGER,status TEXT,artifact_id TEXT,error TEXT,PRIMARY KEY(source_id,source_revision,slot));
            CREATE TABLE IF NOT EXISTS v2_outbox(event_id TEXT PRIMARY KEY,aggregate_id TEXT,version INTEGER,
              event_type TEXT,payload TEXT,committed_at REAL,delivered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS v2_source_metadata(source_id TEXT,source_revision TEXT,
              content_revision TEXT,source_sha TEXT,path TEXT,metadata TEXT,PRIMARY KEY(source_id,source_revision));
            CREATE TABLE IF NOT EXISTS v2_source_leases(source_id TEXT,source_revision TEXT,
              token TEXT,expires_at REAL,PRIMARY KEY(source_id,source_revision));
            CREATE TABLE IF NOT EXISTS v2_active_sources(source_id TEXT PRIMARY KEY,source_revision TEXT,generation INTEGER);
            """)
            from .v2_epoch_fence import add_epoch_columns
            add_epoch_columns(c,'v2_outbox')
            c.execute('CREATE TABLE IF NOT EXISTS v2_source_epochs(source_id TEXT,source_revision TEXT,scheduler_run_id TEXT NOT NULL,execution_epoch INTEGER NOT NULL,PRIMARY KEY(source_id,source_revision))')

    def connect(self):
        from .v2_epoch_fence import connect_runtime
        return connect_runtime(self.db, self.scheduler_owner)

    @contextmanager
    def write_connect(self):
        with self.connect() as c:
            if not c.in_transaction:
                c.execute('BEGIN IMMEDIATE')
            yield c

    def assert_source_epoch(self, c, source, revision):
        if not self.scheduler_owner:return
        old=c.execute('SELECT scheduler_run_id,execution_epoch FROM v2_source_epochs WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        owner=self.scheduler_owner
        if old and tuple(old)!=(owner['scheduler_run_id'],owner['execution_epoch']):
            from .scheduler_identity import SchedulerOwnershipError
            raise SchedulerOwnershipError('stale_source_execution_epoch')
        c.execute('INSERT OR IGNORE INTO v2_source_epochs VALUES(?,?,?,?)',(source,revision,owner['scheduler_run_id'],owner['execution_epoch']))

    def register(self, c, source, revision):
        self.assert_source_epoch(c,source,revision)
        old = c.execute("SELECT policy_hash FROM v2_sources WHERE source_id=? AND revision=?", (source, revision)).fetchone()
        if old and old[0] != self.policy.fingerprint: raise ProducerConflict("sampling_revision_conflict")
        c.execute("INSERT OR IGNORE INTO v2_sources(source_id,revision,policy_hash,policy_json) VALUES(?,?,?,?)", (source, revision, self.policy.fingerprint, json.dumps(asdict(self.policy), sort_keys=True)))

    def declare_slots(self,source,revision,count):
        if count<0:raise ProducerConflict('negative_slot_count')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.register(c,source,revision);self.check_source_lease(c,source,revision)
            old=c.execute('SELECT MAX(slot) FROM v2_sample_slots WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()[0]
            if old is not None and old>=count:raise ProducerConflict('sampling_slot_plan_conflict')
            for slot in range(count):c.execute("INSERT OR IGNORE INTO v2_sample_slots VALUES(?,?,?,?,'PENDING',NULL,'')",(source,revision,slot,self.policy.timestamp(slot)))

    def update_frontier(self,c,source,revision):
        slots={r[0] for r in c.execute("SELECT slot FROM v2_sample_slots WHERE source_id=? AND source_revision=? AND status IN ('READY','FAILED_FINAL')",(source,revision))}
        frontier=-1
        while frontier+1 in slots:frontier+=1
        c.execute('UPDATE v2_sources SET produced_frontier=?,version=version+1 WHERE source_id=? AND revision=?',(frontier,source,revision))

    def slot_failed(self,source,revision,slot,error):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.register(c,source,revision);self.check_source_lease(c,source,revision)
            old=c.execute('SELECT status FROM v2_sample_slots WHERE source_id=? AND source_revision=? AND slot=?',(source,revision,slot)).fetchone()
            if old and old[0]=='READY':raise ProducerConflict('cannot_fail_committed_frame')
            c.execute("INSERT OR REPLACE INTO v2_sample_slots VALUES(?,?,?,?,'FAILED_FINAL',NULL,?)",(source,revision,slot,self.policy.timestamp(slot),str(error)))
            self.update_frontier(c,source,revision)
            self.emit(c,'FRAME_FAILED',source,revision,slot+1,{'sampled_sequence_index':slot,'time_position_ms':self.policy.timestamp(slot),'status':'FAILED_FINAL','error':str(error)})

    def emit(self, c, event_type, source, revision, version, payload,producer_id='pipeline-v2',contract_hash=None):
        self.assert_source_epoch(c,source,revision)
        row=c.execute('SELECT policy_hash FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
        event = {"schema_version": "pipeline_v2_streaming_semantic_gate_v1", "event_type": event_type,
                 "source_id": source,"source_revision": revision,"aggregate_version": version,
                 "producer_id": producer_id, "occurred_at": time.time(),"committed_at": time.time(),
                 "policy_or_contract_hash": contract_hash or (row[0] if row else self.policy.fingerprint),"payload": payload}
        if self.scheduler_owner:
            event.update(self.scheduler_owner)
        event["event_id"] = fingerprint((event_type, source, revision, payload, self.scheduler_owner)) if self.scheduler_owner else fingerprint((event_type, source, revision, payload))
        guard=getattr(self,'publication_guard',None)
        if guard is not None and not c.execute('SELECT 1 FROM v2_outbox WHERE event_id=?',(event['event_id'],)).fetchone():
            guard(c,event_type,payload)
        owner=self.scheduler_owner or {}
        c.execute("INSERT OR IGNORE INTO v2_outbox(event_id,aggregate_id,version,event_type,payload,committed_at,scheduler_run_id,execution_epoch) VALUES(?,?,?,?,?,?,?,?)", (event["event_id"], source+":"+revision, version, event_type, json.dumps(event,sort_keys=True),time.time(),owner.get('scheduler_run_id'),owner.get('execution_epoch')))
        return event

    def source_ready(self,source,content_revision,source_sha,path,metadata=None,*,execution_attempt=None):
        if not re.fullmatch('[0-9a-f]{64}',source_sha) or not Path(path).is_file():raise ProducerConflict('source_fingerprint_or_path_invalid')
        if self.scheduler_owner:
            execution_attempt=(self.scheduler_owner['scheduler_run_id'],self.scheduler_owner['execution_epoch'],execution_attempt)
        revision=self.policy.revision(content_revision)
        if execution_attempt is not None:revision=fingerprint((revision,'decode-attempt',execution_attempt))
        values=(content_revision,source_sha,str(Path(path).resolve()),json.dumps(metadata or {},sort_keys=True))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.register(c,source,revision)
            old=c.execute('SELECT * FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if old and tuple(old[k] for k in ('content_revision','source_sha','path','metadata'))!=values:raise ProducerConflict('source_metadata_revision_conflict')
            c.execute('INSERT OR IGNORE INTO v2_source_metadata VALUES(?,?,?,?,?,?)',(source,revision,*values))
            if not old:
                active=c.execute('SELECT generation FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
                c.execute('INSERT OR REPLACE INTO v2_active_sources VALUES(?,?,?)',(source,revision,1 if not active else active[0]+1))
            self.emit(c,'SOURCE_READY',source,revision,1,{'source_sha':source_sha,'path':values[2],'content_revision':content_revision,'metadata':metadata or {}})
        return revision

    def abort_decode_attempt(self,source,revision,error):
        """Revoke published work before legacy clears/replaces decoder outputs.

        Historical rows are retained. Active source lookup points to a tombstone
        until the next attempt registers; every existing search lane excludes it.
        """
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.assert_source_epoch(c,source,revision)
            c.execute("UPDATE v2_sources SET eos='ABORTED',error=? WHERE source_id=? AND revision=?",(error,source,revision))
            from .v2_visual_closure import invalidate_visual
            invalidate_visual(c,source,revision)
            c.execute("UPDATE v2_active_sources SET source_revision=?,generation=generation+1 WHERE source_id=? AND source_revision=?",('aborted:'+revision,source,revision))
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'v2_decode_attempts' in tables:
                c.execute("UPDATE v2_decode_attempts SET status='ABORTED' WHERE source_id=? AND source_revision=?",(source,revision))
            if 'v2_jobs' in tables:
                c.execute("UPDATE v2_jobs SET status='SUPERSEDED',token=NULL WHERE source_id=? AND source_revision=?",(source,revision))
            if 'v2_source_closure' in tables:
                c.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?",(source,revision))
            self.emit(c,'SOURCE_ABORTED',source,revision,1,{'error':error,'decode_attempt_revoked':True})

    def activate_revision(self,source,revision,expected_generation):
        """Explicit UI reuse of an older processing policy; resume cannot flip it."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.assert_source_epoch(c,source,revision)
            if not c.execute('SELECT 1 FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone():raise ProducerConflict('processing_revision_missing')
            cur=c.execute('UPDATE v2_active_sources SET source_revision=?,generation=generation+1 WHERE source_id=? AND generation=?',(revision,source,expected_generation))
            if cur.rowcount!=1:raise ProducerConflict('activation_generation_conflict')
            self.emit(c,'SOURCE_ACTIVE_REVISION_CHANGED',source,revision,expected_generation+1,{'source_revision':revision,'generation':expected_generation+1})

    @contextmanager
    def source_lease(self,source,revision,lease_seconds=120):
        if lease_seconds<=0:raise ProducerConflict('invalid_source_lease')
        token=uuid.uuid4().hex
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.register(c,source,revision)
            old=c.execute('SELECT * FROM v2_source_leases WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if old and old['expires_at']>time.time():raise ProducerConflict('source_producer_already_leased')
            c.execute('INSERT OR REPLACE INTO v2_source_leases VALUES(?,?,?,?)',(source,revision,token,time.time()+lease_seconds))
        self._producer_tokens[(source,revision)]=token
        stopped=threading.Event()
        def renew():
            while not stopped.wait(lease_seconds/3):
                with self.write_connect() as c:
                    cur=c.execute('UPDATE v2_source_leases SET expires_at=? WHERE source_id=? AND source_revision=? AND token=? AND expires_at>?',(time.time()+lease_seconds,source,revision,token,time.time()))
                    if cur.rowcount!=1:return
        thread=threading.Thread(target=renew,daemon=True);thread.start()
        try:yield token
        finally:
            stopped.set();thread.join();self._producer_tokens.pop((source,revision),None)
            with self.write_connect() as c:c.execute('DELETE FROM v2_source_leases WHERE source_id=? AND source_revision=? AND token=?',(source,revision,token))

    def check_source_lease(self,c,source,revision):
        row=c.execute('SELECT * FROM v2_source_leases WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        token=self._producer_tokens.get((source,revision))
        if (token and not row) or (row and (row['expires_at']<=time.time() or row['token']!=token)):raise ProducerConflict('source_producer_fence_lost')

    def publish(self, source, revision, slot, path):
        hook=getattr(self,'before_publish',None)
        if hook:hook(source,revision,slot)
        sha,w,h = validate_jpeg(path)
        with self.connect() as c:
            metadata=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        timestamp=-1 if metadata and json.loads(metadata[0]).get('media_type')=='image' else self.policy.timestamp(slot)
        if self.artifact_root:
            # Legacy Stage03 renames/deletes temporary files after extraction.
            # Published artifacts therefore live at immutable, SHA-bound paths.
            target=self.artifact_root/source/revision/(str(slot)+'-'+sha+'.jpg')
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():
                import tempfile
                data=Path(path).read_bytes()
                if hashlib.sha256(data).hexdigest()!=sha:raise ProducerConflict('file_changed_during_validation')
                fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.publishing-')
                try:
                    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                    os.replace(tmp,target)
                finally:
                    if Path(tmp).exists():Path(tmp).unlink()
            if validate_jpeg(target)[0]!=sha:raise ProducerConflict('published_artifact_corrupt')
            path=target
        artifact = "vu_"+fingerprint((source,revision,slot,sha))[:28]
        payload={"frame_id":artifact,"visual_unit_id":artifact,"derived_id":"der_"+artifact[3:],
                 "sampled_sequence_index":slot,"time_position_ms":timestamp,
                 "timecode_basis":"explicit_legacy_short_video_sampling" if self.policy.explicit_timestamps_ms is not None else "source_offset_plus_sampling_interval","derived_path":str(Path(path).resolve()),
                 "derived_sha":sha,"width":w,"height":h,"sampling_contract_hash":self.policy.fingerprint,
                 "geometry":{"orientation":"portrait" if h>w else "landscape"}}
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.register(c,source,revision)
            self.check_source_lease(c,source,revision)
            old=c.execute("SELECT sha FROM v2_artifacts WHERE source_id=? AND revision=? AND slot=?",(source,revision,slot)).fetchone()
            if old and old[0] != sha: raise ProducerConflict("slot_sha_conflict")
            if old:
                c.execute('INSERT OR IGNORE INTO v2_artifact_mappings VALUES(?,?,?)',(artifact,payload['derived_id'],artifact))
                c.execute("INSERT OR REPLACE INTO v2_sample_slots VALUES(?,?,?,?,'READY',?,'')",(source,revision,slot,timestamp,artifact))
                self.update_frontier(c,source,revision)
                return artifact
            declared=c.execute('SELECT status FROM v2_sample_slots WHERE source_id=? AND source_revision=? AND slot=?',(source,revision,slot)).fetchone()
            if c.execute("SELECT eos FROM v2_sources WHERE source_id=? AND revision=?",(source,revision)).fetchone()[0]=='EOS' and (not declared or declared[0]!='FAILED_FINAL'):raise ProducerConflict('publish_after_eos')
            c.execute("INSERT INTO v2_artifacts VALUES(?,?,?,?,?,?,?,?,?,'READY')",(artifact,source,revision,slot,timestamp,sha,str(Path(path).resolve()),w,h))
            c.execute('INSERT INTO v2_artifact_mappings VALUES(?,?,?)',(artifact,payload['derived_id'],artifact))
            c.execute("INSERT OR REPLACE INTO v2_sample_slots VALUES(?,?,?,?,'READY',?,'')",(source,revision,slot,timestamp,artifact))
            self.update_frontier(c,source,revision)
            self.emit(c,'FRAME_READY',source,revision,slot+1,payload)
        return artifact

    def finish(self, source, revision, final_sequence, *, success, error=''):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE'); self.register(c,source,revision)
            self.check_source_lease(c,source,revision)
            row=c.execute('SELECT * FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
            maximum=c.execute('SELECT COALESCE(MAX(slot),-1) FROM v2_sample_slots WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()[0]
            if success and (row['produced_frontier']!=final_sequence or maximum!=final_sequence): raise ProducerConflict('eos_slot_gap')
            if row['eos']=='EOS' and (not success or row['final_sequence']!=final_sequence): raise ProducerConflict('eos_conflict')
            c.execute('UPDATE v2_sources SET eos=?,final_sequence=?,error=?,version=version+1 WHERE source_id=? AND revision=?',('EOS' if success else 'ABORTED',final_sequence if success else None,error,source,revision))
            return self.emit(c,'SOURCE_EOS' if success else 'SOURCE_ABORTED',source,revision,final_sequence+2,
                {'final_sequence':final_sequence if success else None,'last_committed_sampled_sequence_index':row['produced_frontier'],
                 'sampling_contract_hash':self.policy.fingerprint,'error':error,'producer_completion_fingerprint':fingerprint((source,revision,final_sequence,success))})

    def observe(self, directory, source, revision, *, filename_start=1):
        for path in sorted(Path(directory).glob('*.jpg')):
            named=re.search(r'_idx(\d+)_t(\d+)ms\.jpg$',path.name)
            match=named or re.search(r'(\d+)\.jpg$',path.name)
            if not match: raise ProducerConflict('filename_sequence_missing')
            if named and int(named.group(2))!=self.policy.timestamp(int(named.group(1))-filename_start):raise ProducerConflict('filename_timestamp_contract_conflict')
            try: self.publish(source,revision,int(match.group(1))-filename_start,path)
            except (OSError,ValueError): continue

    def run(self, command, directory, source, revision, expected_count, *, filename_start=1, timeout=None):
        """Safe replay of committed files on restart; never overwrite input media."""
        with self.source_lease(source,revision):
            self.declare_slots(source,revision,expected_count)
            return self._run(command,directory,source,revision,expected_count,filename_start=filename_start,timeout=timeout)

    def _run(self, command, directory, source, revision, expected_count, *, filename_start=1, timeout=None):
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        stopped=threading.Event(); failure=[]
        def watch():
            while not stopped.is_set():
                try: self.observe(directory,source,revision,filename_start=filename_start)
                except Exception as exc: failure.append(str(exc)); process.terminate(); return
                stopped.wait(.02)
        thread=threading.Thread(target=watch); thread.start()
        try:
            stdout,stderr=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill(); stdout,stderr=process.communicate(); failure.append('producer_timeout')
        finally: stopped.set(); thread.join()
        try:self.observe(directory,source,revision,filename_start=filename_start)
        except Exception as exc:failure.append(str(exc))
        success=process.returncode==0 and not failure
        invalid=False
        if success:
            with self.connect() as c:pending=[r[0] for r in c.execute("SELECT slot FROM v2_sample_slots WHERE source_id=? AND source_revision=? AND status<>'READY'",(source,revision))]
            for slot in pending:self.slot_failed(source,revision,slot,'FRAME_NOT_PRODUCED_OR_INVALID');invalid=True
        try: self.finish(source,revision,expected_count-1,success=success,error=';'.join(failure) or stderr[-1000:])
        except ProducerConflict as exc:
            self.finish(source,revision,expected_count-1,success=False,error=str(exc)); success=False
        return success and not invalid,stdout,stderr
