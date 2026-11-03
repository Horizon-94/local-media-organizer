"""Bounded first-selection admission and changed-input coordination, no models."""
import collections,json,time
from contextlib import contextmanager

class ChangedInputs:
    def __init__(self):self.processed={}
    def changed(self,source,key):return self.processed.get(source)!=key
    def observed(self,source,key):
        # Store the pre-compute key, never a post-compute key that can swallow a wake.
        self.processed[source]=key

class CoordinationTurns:
    """Fair visits under continuously changing global/source generations.

    This only selects bookkeeping visits, never claims model jobs or changes
    their priority. An attempted visit consumes its turn even on a stale read;
    all dirty keys, revision fences and retry wakes stay with the caller.
    """
    def __init__(self):self.sequence=0;self.last={}
    def select(self,ready,limit):
        return sorted(ready,key=lambda row:(self.last.get(row['source_id'],-1),row['sequence']))[:limit]
    def served(self,source):
        self.sequence+=1;self.last[source]=self.sequence


def install_coverage_tokens(c):
    """Durable coverage revision facts, using existing source generation fence."""
    for action in ('INSERT','UPDATE','DELETE'):
        ref='OLD' if action=='DELETE' else 'NEW'
        owner=f"COALESCE((SELECT source_id FROM v2_documents WHERE document_id={ref}.entity_id),(SELECT source_id FROM v2_artifacts WHERE artifact_id={ref}.entity_id),(SELECT source_id FROM v2_fusion WHERE link_id={ref}.entity_id))"
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS mixed_coverage_generation_{action}
          AFTER {action} ON v2_index_coverage WHEN {owner} IS NOT NULL BEGIN
          INSERT INTO video_stream_change_tokens_v1 VALUES('source:'||{owner},1)
          ON CONFLICT(scope) DO UPDATE SET generation=generation+1; END''')

def trace(runtime,**value):
    from apps.media_archive_image_video_ui.video_telemetry import append
    append(runtime.root/'coordination-events.jsonl',{'timestamp':time.time(),'scope':'ACTUAL_SOURCE_OPERATION',**value})

# Dedicated service capacity; never another model pool.
READY_SELECTION_CONCURRENCY = 1


class ReadyLaneStopped(Exception):pass


def visual_prerequisite_proof(runtime,c,sid,rev,*,cancel=None,related=False):
    """Exact durable prerequisite generation, read in the caller's transaction."""
    import hashlib
    from apps.media_archive_image_video_ui.v2_current_jobs_cache import ORIGINAL
    active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(sid,)).fetchone()
    if not active or active[0]!=rev:return None
    states={r[0] for r in c.execute('SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(sid,rev))}
    kind=runtime.expected_sources.get(sid)
    front='IMAGE_PREVIEW_READY' if kind=='image' else 'EXTRACTION_DONE'
    blocked={'VISUAL_FAILED_FINAL'} if related else {'FIRST_SELECTION_PUBLISHED','SELECTION_DONE','SOURCE_COMPLETE','SOURCE_FAILED_FINAL','VISUAL_FAILED_FINAL'}
    if front not in states or states&blocked:return None
    if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('frame_basics','yoloe','openclip') AND status='TERMINAL_FAILED' LIMIT 1",(sid,rev)).fetchone():return None
    eos=c.execute('SELECT eos,final_sequence FROM v2_sources WHERE source_id=? AND revision=?',(sid,rev)).fetchone()
    if not eos or eos[0]!='EOS' or eos[1] is None:return None
    slots=[tuple(r) for r in c.execute('SELECT slot,status,artifact_id FROM v2_sample_slots WHERE source_id=? AND source_revision=? ORDER BY slot',(sid,rev))]
    if [r[0] for r in slots]!=list(range(eos[1]+1)) or any(r[1]!='READY' for r in slots):return None
    native=[tuple(r) for r in c.execute("SELECT job_id,stage,status,result_sha,json_extract(payload,'$.frame_id') FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('frame_basics','yoloe','openclip') ORDER BY job_id",(sid,rev))]
    if any(r[2]!='DONE' for r in native):return None
    contracts={stage:runtime.media_adapters[kind].contracts[stage]['revision'] for stage in runtime.media_adapters[kind].pipeline.selection.REQUIRED}
    evidence=[]
    for slot,status,artifact in slots:
        if cancel is not None and cancel.is_set():raise ReadyLaneStopped()
        if not artifact:return None
        for stage,contract in contracts.items():
            job=c.execute("SELECT j.execution_key,j.status,j.result_sha,j.source_id,j.source_revision,j.provenance FROM main.v2_jobs j WHERE j.stage=? AND j.item_id=? AND j.revision=? AND j.status<>'SUPERSEDED' AND "+ORIGINAL,(stage,artifact,contract)).fetchone()
            if not job or job[1]!='DONE' or job[3]!=sid or job[4]!=rev or not job[2]:return None
            evidence.append(tuple(job))
    # Only the actual required ancestry defines this dependency generation.
    # Audio/document/peer mutations must not revoke a ready visual entitlement.
    roots=json.dumps(sorted(r[0] for r in evidence))
    closure="""WITH RECURSIVE needed(k) AS (SELECT value FROM json_each(?) UNION
        SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.k) """
    ancestry=[tuple(r) for r in c.execute(closure+"SELECT n.k,j.status,j.result_sha,j.source_id,j.source_revision FROM needed n LEFT JOIN v2_jobs j ON j.execution_key=n.k ORDER BY n.k",(roots,))]
    edges=[tuple(r) for r in c.execute(closure+"SELECT d.child_key,d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.k ORDER BY d.child_key,d.parent_key",(roots,))]
    generation=hashlib.sha256(json.dumps([ancestry,edges],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    body=dict(source_id=sid,source_revision=rev,eos=eos[0],final_sequence=eos[1],slots=slots,contracts=contracts,native=native,evidence=evidence,dependency_generation=generation)
    body['identity']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return body


def readiness_proof(runtime,c,sid,rev,*,cancel=None):
    from apps.media_archive_image_video_ui.v2_producer import fingerprint
    kind=runtime.expected_sources.get(sid)
    if kind=='video':return visual_prerequisite_proof(runtime,c,sid,rev,cancel=cancel)
    if kind!='image' or not runtime.decisions[sid]['should_generate_preview']:return None
    from apps.media_archive_image_video_ui.v2_timelapse_selection_context import sequence_members
    members=sequence_members(runtime.image_views[sid].plan,sid) or [runtime.decisions[sid]]
    group=[];own=None
    for member in members:
        other=member['parent_source_content_id']
        row=c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(other,)).fetchone()
        if not row:return None
        proof=visual_prerequisite_proof(runtime,c,other,row[0],cancel=cancel,related=other!=sid)
        if not proof:return None
        group.append((other,row[0],proof['identity']))
        if other==sid:own=proof
    if own is None:return None
    own={**own,'image_context_inputs':group,'plan_fingerprint':runtime.image_views[sid].plan['fingerprint']}
    own['identity']=fingerprint(own)
    return own


class _ReadyProof:
    """Immutable proof handle. Header reads never hydrate historical evidence."""
    def __init__(self,store,reference):
        self.store=store;self.reference=reference;self.signature=self._stat()
    def _stat(self):
        try:
            s=(self.store.proof_root/(self.reference['$proof_sha256']+'.json')).stat()
            return (s.st_ino,s.st_size,s.st_mtime_ns)
        except FileNotFoundError as error:raise ValueError('READY_PROOF_MISSING') from error
    def validate(self):
        signature=self._stat()
        if signature!=self.signature:
            self.store._read_proof(self.reference);self.signature=signature
    def __getitem__(self,key):
        if key in ('identity','source_id','source_revision'):return self.reference[key]
        return self.materialize()[key]
    def materialize(self):return self.store._read_proof(self.reference)


class ReadyEntitlements:
    """Atomic state journal referencing fsynced, immutable complete evidence.

    One runtime owns the project; flock + generation fingerprint additionally
    reject stale store instances. Commit fences remain in the existing caller.
    """
    FORMAT='ready-entitlements-proof-refs-v1'
    HEADER=('identity','source_id','source_revision')
    def __init__(self,path):
        import threading
        from pathlib import Path
        self.path=Path(path);self.lock=threading.RLock()
        self.proof_root=self.path.with_name(self.path.name+'.proofs')
        self.lock_path=self.path.with_name(self.path.name+'.lock')
        self.poisoned=False
        self.metrics=dict(proof_write_calls=0,proof_write_bytes=0,state_write_calls=0,state_write_bytes=0,proof_read_bytes=0,legacy_read_bytes=0,save_ms=0.,migration_write_bytes=0)
        with self._file_lock():
            self.signature=self._signature()
            raw=self.path.read_bytes() if self.path.exists() else b'{"next":1,"entries":{}}'
            self.data=json.loads(raw)
            self.legacy='format' not in self.data
            if self.legacy:self.metrics['legacy_read_bytes']=len(raw)
            elif self.data['format']!=self.FORMAT:raise ValueError('READY_FORMAT_UNSUPPORTED')
            for key,row in self.data['entries'].items():
                if not self.legacy:
                    ref=row['proof'];self._read_proof(ref)
                    row['proof']=_ReadyProof(self,ref)
                proof=row['proof']
                if key!=proof['source_id']+':'+proof['source_revision']:raise ValueError('READY_SOURCE_REVISION_MISMATCH')
            if self.signature!=self._signature():raise ValueError('READY_STATE_CHANGED_DURING_OPEN')
    @contextmanager
    def _file_lock(self):
        import fcntl
        with self.lock_path.open('a+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(f,fcntl.LOCK_UN)
    def _signature(self):
        try:
            s=self.path.stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        except FileNotFoundError:return None
    def _check_owner(self):
        if self.poisoned:raise ValueError('READY_STORE_REOPEN_REQUIRED')
        if self.signature!=self._signature():raise ValueError('READY_STALE_STORE_OWNER')
    @staticmethod
    def _sync_dir(path):
        import os
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    def _point(self,name):
        hook=getattr(self,'fault_hook',None)
        if hook:hook(name)
    def _atomic(self,path,chunks,kind):
        import os,uuid
        tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        # Incomplete crash remnants are retained; never garbage collect here.
        with tmp.open('xb') as f:
            for block in chunks:
                written=f.write(block)
                if written!=len(block):raise OSError('READY_SHORT_WRITE')
                self.metrics[kind+'_write_bytes']+=written
            f.flush();os.fsync(f.fileno());self._point(kind+'_file_synced')
        os.replace(tmp,path);self._point(kind+'_replaced')
        self._sync_dir(path.parent);self._point(kind+'_directory_synced')
        self.metrics[kind+'_write_calls']+=1
    def _read_proof(self,ref):
        import hashlib,re
        digest=ref.get('$proof_sha256','')
        if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('READY_PROOF_REFERENCE_INVALID')
        try:raw=(self.proof_root/(digest+'.json')).read_bytes()
        except FileNotFoundError as error:raise ValueError('READY_PROOF_MISSING:'+digest) from error
        self.metrics['proof_read_bytes']+=len(raw)
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('READY_PROOF_HASH_MISMATCH:'+digest)
        proof=json.loads(raw)
        if any(proof.get(k)!=ref.get(k) for k in self.HEADER):raise ValueError('READY_PROOF_HEADER_MISMATCH')
        return proof
    def _persist_proof(self,proof):
        import hashlib
        if isinstance(proof,_ReadyProof):
            proof.validate();return proof
        raw=json.dumps(proof,sort_keys=True,separators=(',',':')).encode()
        digest=hashlib.sha256(raw).hexdigest()
        ref={'$proof_sha256':digest,**{k:proof[k] for k in self.HEADER}}
        if not self.proof_root.exists():
            self.proof_root.mkdir();self._sync_dir(self.proof_root.parent)
        target=self.proof_root/(digest+'.json')
        if target.exists():
            self._read_proof(ref)
            import os
            with target.open("rb") as f:os.fsync(f.fileno())
            self._sync_dir(self.proof_root)
        else:self._atomic(target,(raw,),'proof')
        self._point('proof_durable_before_state')
        return _ReadyProof(self,ref)
    def save(self):
        with self.lock,self._file_lock():
            self._check_owner();began=time.perf_counter();before=self.metrics['proof_write_bytes']+self.metrics['state_write_bytes']
            was_legacy=self.legacy
            try:
                rows={}
                for key,row in self.data['entries'].items():
                    row['proof']=self._persist_proof(row['proof'])
                    rows[key]={**row,'proof':row['proof'].reference}
                body={'format':self.FORMAT,'next':self.data['next'],'entries':rows}
                self._atomic(self.path,(json.dumps(body,sort_keys=True,separators=(',',':')).encode(),),'state')
                self.signature=self._signature();self.legacy=False;self.data['format']=self.FORMAT
                if was_legacy:self.metrics['migration_write_bytes']+=self.metrics['proof_write_bytes']+self.metrics['state_write_bytes']-before
            except BaseException:
                self.poisoned=True;raise
            finally:self.metrics['save_ms']+=(time.perf_counter()-began)*1000
    def migrate(self):
        if self.legacy:self.save()
    def offer(self,proof):
        with self.lock:
            key=proof['source_id']+':'+proof['source_revision'];old=self.data['entries'].get(key)
            if old and old['proof']['identity']==proof['identity'] and old['state'] in ('READY','OWNED','RETIRED'):return old
            if old and old['state']=='OWNED':return None
            seq=self.data['next'];self.data['next']+=1
            row=dict(seq=seq,proof=proof,ready_at=time.time(),state='READY',retry_at=0)
            self.data['entries'][key]=row;self.save();return row
    def next(self):
        with self.lock:
            rows=[r for r in self.data['entries'].values() if r['state']=='READY' and r['retry_at']<=time.monotonic()]
            return min(rows,key=lambda r:r['seq']) if rows else None
    def transition(self,row,state):
        with self.lock:
            proof=row['proof'];key=proof['source_id']+':'+proof['source_revision']
            if self.data['entries'].get(key) is not row:raise ValueError('READY_STALE_ROW_OWNER')
            row['state']=state
            if state=='READY':row['retry_at']=time.monotonic()+.1
            self.save()
    def pending_ids(self):
        with self.lock:return {r['proof']['source_id'] for r in self.data['entries'].values() if r['state'] in ('READY','OWNED')}
    def recover(self):
        with self.lock:
            for row in self.data['entries'].values():
                if row['state']=='OWNED':row['state']='READY'
                row['retry_at']=0
            self.save()
    def export_legacy(self,destination):
        """Stopped-runtime rollback: export latest progress, never an old backup."""
        from pathlib import Path
        destination=Path(destination)
        with self.lock,self._file_lock():
            self._check_owner()
            def chunks():
                yield ('{"next":'+str(self.data['next'])+',"entries":{').encode()
                for n,(key,row) in enumerate(self.data['entries'].items()):
                    proof=row['proof'].materialize() if isinstance(row['proof'],_ReadyProof) else row['proof']
                    yield ((',' if n else '')+json.dumps(key)+':'+json.dumps({**row,'proof':proof},sort_keys=True,separators=(',',':'))).encode()
                yield b'}}'
            try:self._atomic(destination,chunks(),'state')
            except BaseException:self.poisoned=True;raise
            if destination==self.path:self.poisoned=True
    def io_snapshot(self):
        with self.lock:return dict(self.metrics)


def run_ready_selection(runtime):
    """Single independent image/video Selection owner. No Search/peer/closure."""
    from apps.media_archive_image_video_ui.v2_runtime import ContractError
    from apps.media_archive_image_video_ui.video_commit_queue import RetryableWriterDeadline
    lane=runtime.ready_entitlements
    try:
        lane.recover();scan_cursor=0
        while not runtime.stop.is_set():
            # Admission discovery depends only on durable visual prerequisites.
            # All sources in this scan share the same transaction snapshot.
            with runtime.ledger.connection() as c:
                c.execute('BEGIN')
                candidates=list(c.execute("""SELECT s.source_id,s.revision FROM video_stream_sources_v1 s
                    JOIN v2_source_metadata m ON m.source_id=s.source_id AND m.source_revision=s.revision
                    WHERE json_extract(m.metadata,'$.media_type') IN ('video','image')
                    AND EXISTS(SELECT 1 FROM video_stream_source_states_v1 e WHERE e.source_id=s.source_id AND e.revision=s.revision AND e.state IN ('EXTRACTION_DONE','IMAGE_PREVIEW_READY'))
                    AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 d WHERE d.source_id=s.source_id AND d.revision=s.revision AND d.state IN ('FIRST_SELECTION_PUBLISHED','SELECTION_DONE','SOURCE_COMPLETE','SOURCE_FAILED_FINAL','VISUAL_FAILED_FINAL'))
                    ORDER BY s.rowid"""))
                proofs=[]
                if candidates:
                    count=min(64,len(candidates))
                    window=[candidates[(scan_cursor+i)%len(candidates)] for i in range(count)]
                    scan_cursor=(scan_cursor+count)%len(candidates)
                    for sid,rev in window:
                        if runtime.stop.is_set():raise ReadyLaneStopped()
                        previous=lane.data['entries'].get(sid+':'+rev)
                        if previous and previous['state'] in ('READY','OWNED'):continue
                        proof=readiness_proof(runtime,c,sid,rev,cancel=runtime.stop)
                        if proof:proofs.append(proof)
            for proof in proofs:
                with runtime.stop.new_work('first_selection_entitlement') as allowed:
                    if not allowed:break
                    old=lane.data['next'];row=lane.offer(proof)
                    if row and lane.data['next']!=old:trace(runtime,event='READY_ENTITLEMENT',source_id=proof['source_id'],revision=proof['source_revision'],ready_seq=row['seq'],ready_entitlement_at=row['ready_at'],proof_identity=proof['identity'])
            row=lane.next()
            if row is None:runtime.stop.wait(.1);continue
            proof=row['proof'];sid=proof['source_id'];rev=proof['source_revision']
            # Revalidate exact proof immediately before the atomic owner handoff.
            with runtime.ledger.connection() as c:
                c.execute('BEGIN');current=readiness_proof(runtime,c,sid,rev,cancel=runtime.stop)
            if current is None:
                done=runtime.for_source(sid).has_state(sid,rev,'FIRST_SELECTION_PUBLISHED') or runtime.for_source(sid).has_state(sid,rev,'SELECTION_DONE')
                lane.transition(row,'RETIRED' if done else 'CANCELLED');continue
            if current['identity']!=proof['identity']:
                lane.transition(row,'CANCELLED');continue
            with runtime.stop.new_work('first_selection') as allowed:
                if not allowed:break
                lane.transition(row,'OWNED')
                trace(runtime,event='FIRST_SELECTION_SERVICE_BEGIN',source_id=sid,revision=rev,ready_seq=row['seq'],ready_entitlement_at=row['ready_at'],proof_identity=proof['identity'])
            # Existing policy, fenced writer and ACK remain the only publication.
            # Owned publication is drained even when Stop is accepted here.
            try:
                if runtime.expected_sources.get(sid)=='image':
                    from apps.media_archive_image_video_ui.image_stream_runtime import ImageRuntime
                    if not ImageRuntime.bind_image_context(runtime.image_views[sid],sid,rev):
                        lane.transition(row,'READY');continue
                runtime.for_source(sid).selection_prepare(sid,rev)
            except RetryableWriterDeadline:
                # A fully rolled-back writer deadline is source-local and
                # explicitly retryable. Do not convert it into global Stop.
                runtime.errors.append('READY_SELECTION_RETRYABLE_WRITER_DEADLINE')
                lane.transition(row,'READY')
                if runtime.stop.wait(.25):break
                continue
            except ContractError as error:
                if not any(t in str(error) for t in ('pending','incomplete','changed_retry','snapshot_changed_retry','stale_source_state')):raise
                lane.transition(row,'READY');continue
            if runtime.for_source(sid).has_state(sid,rev,'FIRST_SELECTION_PUBLISHED'):
                trace(runtime,event='SELECTION_PUBLICATION',source_id=sid,revision=rev,ready_seq=row['seq'],ready_entitlement_at=row['ready_at'])
                lane.transition(row,'RETIRED')
            else:lane.transition(row,'READY')
    except ReadyLaneStopped:
        return  # Preserve unstarted entitlements for native Resume.
    except Exception as error:
        runtime.errors.append('READY_SELECTION:'+repr(error))
        runtime.stop.request('PROCESSING_FATAL','READY_SELECTION_EXCEPTION',error=str(error),traceback=__import__('traceback').format_exc())
