"""Persistent byte reservations; no unfinished-source-count backpressure."""
from pathlib import Path
from contextlib import contextmanager
import shutil
import sqlite3
import threading
import uuid
import os
import hashlib,json
import time


class ByteSpool:
    @contextmanager
    def connection(self, *, write=False):
        from .video_stream_ledger import TrackedConnection
        with sqlite3.connect(self.database, timeout=2, factory=TrackedConnection) as c:
            if write:
                c.execute('BEGIN IMMEDIATE')
            yield c

    def __init__(self, database, directory, hard_bytes, disk_free_floor=3*1024**3):
        self.database, self.directory = str(database), Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.hard_bytes, self.disk_free_floor = hard_bytes,disk_free_floor
        self.lock=threading.Lock()
        self.blocked_reason=None
        with self.connection(write=True) as c:
            c.execute('CREATE TABLE IF NOT EXISTS video_spool_reservations_v1(token TEXT PRIMARY KEY,bytes INTEGER NOT NULL CHECK(bytes>=0))')
            c.execute('CREATE TABLE IF NOT EXISTS video_spool_owners_v1(token TEXT PRIMARY KEY,path TEXT,device INTEGER,inode INTEGER,state TEXT)')

    def bind_directory(self,token,path):
        path=Path(path).resolve(strict=True)
        if not path.is_relative_to(self.directory.resolve()) or path.parent.name!='spool':raise ValueError('spool_directory_not_owned')
        stat=path.stat()
        with self.connection(write=True) as c:
            if not c.execute('SELECT 1 FROM video_spool_reservations_v1 WHERE token=?',(token,)).fetchone():raise ValueError('reservation_missing')
            c.execute("INSERT OR IGNORE INTO video_spool_owners_v1 VALUES(?,?,?,?,'ACTIVE')",(token,str(path),stat.st_dev,stat.st_ino))

    def archive(self,token):
        """Move own completed spool to retained archive, preserve old paths.

        No media deletion. Directory inode pins ownership; restart can finish
        rename→symlink→ledger commit. Disk free floor continues covering archive.
        """
        with self.connection(write=True) as c:
            row=c.execute('SELECT path,device,inode,state FROM video_spool_owners_v1 WHERE token=?',(token,)).fetchone()
            if not row:raise ValueError('artifact_owner_missing')
            if row[3]=='ARCHIVED':return
            if Path(row[0]).parent.resolve()!=self.directory.resolve()/'spool':raise ValueError('spool_directory_not_owned')
            c.execute("UPDATE video_spool_owners_v1 SET state='MOVING' WHERE token=?",(token,))
        original=Path(row[0]);target=self.directory/'archive'/(original.name+'-'+token)
        target.parent.mkdir(parents=True,exist_ok=True)
        owned=target if target.exists() else original
        if owned.exists() and owned.stat().st_dev!=row[1]:
            # st_dev is a mount-session number on macOS, not a volume UUID.
            # An OS restart may change it. Never waive inode/path ownership:
            # prove the same pinned reader directory AND all durable artifacts
            # before rebinding only this live reservation's device number.
            stat=owned.stat()
            if stat.st_ino!=row[2] or stat.st_dev!=self.directory.stat().st_dev or not owned.resolve().is_relative_to(self.directory.resolve()):raise ValueError('spool_owner_identity_changed')
            with self.connection() as c:
                attempt=c.execute('SELECT attempt_id,revision,source_id FROM video_stream_read_attempts_v1 WHERE path=?',(str(original),)).fetchone()
                if not attempt or original.name!=attempt[1]+'-'+attempt[0]:raise ValueError('spool_reader_identity_changed')
                removed=set()
                complete=c.execute("SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SOURCE_COMPLETE'",(attempt[2],attempt[1])).fetchone()
                if complete:
                    for state,payload in c.execute("SELECT state,payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('TRANSIENT_AUDIO_CLEANUP_INTENT','TRANSIENT_AUDIO_CLEANED')",(attempt[2],attempt[1])):
                        proof=json.loads(payload);removed.update(proof.get('removed_paths',proof.get('paths',[])))
                evidence=list(c.execute('SELECT path,sha FROM v2_artifacts WHERE path LIKE ?',(str(original)+'/%',)))
                for (raw,) in c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE audio_json IS NOT NULL'):
                    audio=json.loads(raw)
                    if audio.get('path','').startswith(str(original)+'/'):evidence.append((audio['path'],audio['sha']))
            for path,digest in evidence:
                artifact=owned/Path(path).relative_to(original)
                if not artifact.is_file() and path in removed and artifact.suffix.lower() in ('.mka','.wav','.flac'):continue
                if not artifact.resolve().is_relative_to(owned.resolve()) or not artifact.is_file():raise ValueError('spool_retained_artifact_missing')
                h=hashlib.sha256()
                with artifact.open('rb') as f:
                    for block in iter(lambda:f.read(1048576),b''):h.update(block)
                if h.hexdigest()!=digest:raise ValueError('spool_retained_artifact_changed')
            with self.connection(write=True) as c:
                c.execute('UPDATE video_spool_owners_v1 SET device=? WHERE token=? AND device=? AND inode=?',(stat.st_dev,token,row[1],row[2]))
            row=(row[0],stat.st_dev,row[2],row[3])
        if not target.exists():
            stat=original.stat()
            if (stat.st_dev,stat.st_ino)!=(row[1],row[2]) or original.is_symlink():raise ValueError('spool_owner_identity_changed')
            original.rename(target)
        stat=target.stat()
        if (stat.st_dev,stat.st_ino)!=(row[1],row[2]):raise ValueError('archive_owner_identity_changed')
        if not os.path.lexists(original):original.symlink_to(target,target_is_directory=True)
        if not original.is_symlink() or original.resolve()!=target.resolve():raise ValueError('archive_alias_conflict')
        with self.connection(write=True) as c:
            c.execute("UPDATE video_spool_owners_v1 SET state='ARCHIVED' WHERE token=?",(token,))
            c.execute('DELETE FROM video_spool_reservations_v1 WHERE token=?',(token,))

    def release_closed(self, token, source_id, revision, owner, epoch):
        from .video_spool_copy_ownership import release
        return release(self,self._release_closed,token,source_id,revision,owner,epoch)

    def release_visual(self, token, source_id, revision, owner, epoch):
        from .video_spool_copy_ownership import release
        return release(self,self._release_visual,token,source_id,revision,owner,epoch)

    def _release_closed(self, token, source_id, revision, owner, epoch, copy_proof=None):
        """Retain derived paths in place, retire only durable mixed-source quota.

        One fenced transaction makes validation/release atomic and restart-safe.
        No rename, unlink, result rewrite, or change to disk-free accounting.
        """
        with self.lock, self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            control=c.execute('SELECT owner,epoch,stopped FROM video_stream_control_v1 WHERE singleton=1').fetchone()
            if control != (owner,epoch,0):raise ValueError('spool_runtime_owner_fence')
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source_id,)).fetchone()
            if active != (revision,):raise ValueError('spool_source_revision_fence')
            states={r[0] for r in c.execute('SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(source_id,revision))}
            if not states.intersection(('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')):return 'ACTIVE'
            if c.execute('SELECT 1 FROM v2_source_leases WHERE source_id=? AND source_revision=? AND expires_at>?',(source_id,revision,time.time())).fetchone():return 'ACTIVE_LEASE'
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status NOT IN ('DONE','TERMINAL_FAILED','SUPERSEDED')",(source_id,revision)).fetchone():return 'STREAM_CONSUMER_PENDING'
            if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND source_revision=? AND status NOT IN ('DONE','FAILED_FINAL','SUPERSEDED','SKIPPED')",(source_id,revision)).fetchone():return 'V2_CONSUMER_PENDING'
            if c.execute("SELECT 1 FROM v2_documents WHERE source_id=? AND source_revision=? AND status NOT IN ('EMBEDDED','SKIP_NO_TEXT')",(source_id,revision)).fetchone():return 'DOCUMENT_CONSUMER_PENDING'
            if c.execute("SELECT 1 FROM v2_deliveries d JOIN v2_outbox o ON o.event_id=d.event_id WHERE json_extract(o.payload,'$.source_id')=? AND json_extract(o.payload,'$.source_revision')=? AND d.status<>'DONE'",(source_id,revision)).fetchone():return 'EVENT_CONSUMER_PENDING'
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'release_publication_queue' in tables:
                if c.execute('SELECT 1 FROM release_publication_queue WHERE source_id=? AND revision=?',(source_id,revision)).fetchone():return 'SEARCH_CONSUMER_PENDING'
                if not c.execute('SELECT 1 FROM release_registry WHERE source_id=? AND revision=? AND present=1 AND terminal=1',(source_id,revision)).fetchone():return 'SEARCH_TERMINAL_PENDING'
            if 'SOURCE_COMPLETE' in states:
                if not c.execute("SELECT 1 FROM v2_source_closure WHERE source_id=? AND source_revision=? AND status='COMPLETE'",(source_id,revision)).fetchone():return 'DURABLE_CLOSURE_PENDING'
                if 'release_search_heads' in tables and not c.execute('SELECT 1 FROM release_search_heads WHERE source_id=? AND revision=? AND visible=1',(source_id,revision)).fetchone():return 'SEARCH_COMMIT_PENDING'
            extraction=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='EXTRACTION_DONE'",(source_id,revision)).fetchone()
            if not extraction or json.loads(extraction[0]).get('reservation_token')!=token:raise ValueError('spool_extraction_token_fence')
            row=c.execute('SELECT path,device,inode,state FROM video_spool_owners_v1 WHERE token=?',(token,)).fetchone()
            if not row:raise ValueError('artifact_owner_missing')
            if row[3] not in ('ACTIVE','RETAINED'):raise ValueError('spool_owner_state_fence')
            from .video_spool_copy_ownership import owned_directory
            path,expected_owner=owned_directory(self,c,row,source_id,revision,copy_proof)
            attempt=c.execute("SELECT attempt_id FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND path=? AND status='EXTRACTION_DONE'",(source_id,revision,row[0])).fetchone()
            if not attempt or path.name!=revision+'-'+attempt[0]:raise ValueError('spool_reader_identity_changed')
            stat=path.stat()
            if (stat.st_dev,stat.st_ino)!=expected_owner:raise ValueError('spool_owner_identity_changed')
            if row[3]=='RETAINED':
                if c.execute('SELECT 1 FROM video_spool_reservations_v1 WHERE token=?',(token,)).fetchone():raise ValueError('spool_retained_reservation_conflict')
                return 'ALREADY_RELEASED'
            if not c.execute('SELECT 1 FROM video_spool_reservations_v1 WHERE token=?',(token,)).fetchone():raise ValueError('reservation_missing')
            c.execute("UPDATE video_spool_owners_v1 SET state='RETAINED' WHERE token=?",(token,))
            c.execute('DELETE FROM video_spool_reservations_v1 WHERE token=?',(token,))
            return 'RELEASED'

    def _release_visual(self, token, source_id, revision, owner, epoch, copy_proof=None):
        """Retire visual quota after its branch closes; retain every payload path.

        Audio/Search may still reference retained artifacts. No file is deleted,
        moved or renamed. RC4 owner, revision, inode and transaction fences apply.
        """
        with self.lock, self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT owner,epoch,stopped FROM video_stream_control_v1 WHERE singleton=1').fetchone() != (owner,epoch,0):
                raise ValueError('spool_runtime_owner_fence')
            if c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source_id,)).fetchone() != (revision,):
                raise ValueError('spool_source_revision_fence')
            states={r[0]:r[1] for r in c.execute('SELECT state,payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(source_id,revision))}
            required={'EXTRACTION_DONE','SELECTION_DONE','LOCAL_PEOPLE_DONE','OCR_COMPLETE','QWEN_FIRST_PASS_DONE'}
            if not required.issubset(states):return 'VISUAL_BRANCH_PENDING'
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND (stage IS NULL OR stage NOT IN ('audio_extract','vad','whisper','embedding')) AND status NOT IN ('DONE','TERMINAL_FAILED','SUPERSEDED')",(source_id,revision)).fetchone():return 'VISUAL_CONSUMER_PENDING'
            if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND source_revision=? AND (stage IS NULL OR stage NOT IN ('audio_extract','vad','whisper','embedding')) AND status NOT IN ('DONE','FAILED_FINAL','SUPERSEDED','SKIPPED')",(source_id,revision)).fetchone():return 'VISUAL_V2_CONSUMER_PENDING'
            if c.execute("SELECT 1 FROM v2_documents WHERE source_id=? AND source_revision=? AND document_type<>'whisper' AND document_type NOT LIKE '%audio%' AND document_type NOT LIKE '%transcript%' AND status NOT IN ('EMBEDDED','SKIP_NO_TEXT')",(source_id,revision)).fetchone():return 'VISUAL_DOCUMENT_PENDING'
            if c.execute('SELECT 1 FROM v2_source_leases WHERE source_id=? AND source_revision=? AND expires_at>?',(source_id,revision,time.time())).fetchone():return 'ACTIVE_LEASE'
            # Selection/promotion must be fully delivered, not just currently empty.
            if c.execute("SELECT 1 FROM v2_deliveries d JOIN v2_outbox o ON o.event_id=d.event_id WHERE json_extract(o.payload,'$.source_id')=? AND json_extract(o.payload,'$.source_revision')=? AND (o.event_type='CANDIDATE_READY' OR (o.event_type='EVIDENCE_READY' AND json_extract(o.payload,'$.payload.stage') IN ('qwen','ocr'))) AND d.status<>'DONE'",(source_id,revision)).fetchone():return 'VISUAL_EVENT_PENDING'
            if json.loads(states['EXTRACTION_DONE']).get('reservation_token')!=token:raise ValueError('spool_extraction_token_fence')
            row=c.execute('SELECT path,device,inode,state FROM video_spool_owners_v1 WHERE token=?',(token,)).fetchone()
            if not row:raise ValueError('artifact_owner_missing')
            if row[3] not in ('ACTIVE','RETAINED'):raise ValueError('spool_owner_state_fence')
            from .video_spool_copy_ownership import owned_directory
            path,expected_owner=owned_directory(self,c,row,source_id,revision,copy_proof)
            attempt=c.execute("SELECT attempt_id FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND path=? AND status='EXTRACTION_DONE'",(source_id,revision,row[0])).fetchone()
            if not attempt or path.name!=revision+'-'+attempt[0]:raise ValueError('spool_reader_identity_changed')
            stat=path.stat()
            if (stat.st_dev,stat.st_ino)!=expected_owner:raise ValueError('spool_owner_identity_changed')
            exists=c.execute('SELECT 1 FROM video_spool_reservations_v1 WHERE token=?',(token,)).fetchone()
            if row[3]=='RETAINED':
                if exists:raise ValueError('spool_retained_reservation_conflict')
                return 'ALREADY_RELEASED'
            if not exists:raise ValueError('reservation_missing')
            c.execute("UPDATE video_spool_owners_v1 SET state='RETAINED' WHERE token=?",(token,))
            c.execute('DELETE FROM video_spool_reservations_v1 WHERE token=?',(token,))
            return 'VISUAL_RELEASED_PAYLOAD_RETAINED'

    def acquire(self, size, stop):
        if not 0 < size <= self.hard_bytes:
            raise ValueError('source_reservation_exceeds_spool_hard_capacity')
        while not stop.is_set():
            with self.lock, self.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                occupied=c.execute('SELECT coalesce(sum(bytes),0) FROM video_spool_reservations_v1').fetchone()[0]
                free=shutil.disk_usage(self.directory).free
                if occupied+size > self.hard_bytes:
                    self.blocked_reason='SPOOL_BYTE_HIGH_WATERMARK'
                elif free-size < self.disk_free_floor:
                    self.blocked_reason='INTERNAL_DISK_SPACE_LOW'
                else:
                    token=uuid.uuid4().hex
                    c.execute('INSERT INTO video_spool_reservations_v1 VALUES(?,?)',(token,size))
                    self.blocked_reason=None
                    return token
            # Wait outside the writer transaction; consumption remains runnable.
            now=time.monotonic()
            if getattr(self,'_last_wait_reason',None)!=self.blocked_reason or now-getattr(self,'_last_wait_at',0)>=30:
                with (self.directory/'spool-admission.jsonl').open('a') as f:
                    f.write(json.dumps({'at':time.time(),'reason_code':self.blocked_reason,'occupied_bytes':occupied,'requested_bytes':size,'hard_bytes':self.hard_bytes,'free_bytes':free})+'\n')
                self._last_wait_reason=self.blocked_reason;self._last_wait_at=now
            stop.wait(.1)
        self.blocked_reason='STOP_REQUESTED'
        return None

    def retire(self, token):
        """Only after all durable consumers no longer require spool ownership.

        Does not delete media. Archived derived files still count toward disk
        free-space protection; caller must distinguish archive from live spool.
        """
        with self.lock,self.connection(write=True) as c:
            c.execute('DELETE FROM video_spool_reservations_v1 WHERE token=?',(token,))
