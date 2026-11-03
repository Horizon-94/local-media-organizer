"""Durable verification and ingestion telemetry. No model/schema/search changes."""
import hashlib,json,os,stat,time,uuid
from pathlib import Path

class IngestionInterrupted(InterruptedError):pass

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('w') as f:
            json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp.exists():tmp.unlink()

def read(path,default):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default

def identity(s):
    return {'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns,'device':s.st_dev,'inode':s.st_ino}

class Ingestion:
    def __init__(self,root,cancel=lambda:False):
        self.root=Path(root);self.cancel=cancel;self.path=self.root/'ingestion_state.json';self.cache_path=self.root/'source_hash_cache.json'
        self.cache=read(self.cache_path,{'contract':'build151-source-hash-cache-v1','sources':{}})
        if self.cache.get('contract')!='build151-source-hash-cache-v1' or not isinstance(self.cache.get('sources'),dict):self.cache={'contract':'build151-source-hash-cache-v1','sources':{}}
        expected=self.cache.get('cache_checksum')
        actual=hashlib.sha256(json.dumps(self.cache['sources'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if self.cache['sources'] and expected!=actual:self.cache={'contract':'build151-source-hash-cache-v1','sources':{}}
        self.start=time.time();self.last_emit=0.;self.total_read=0;self.verified=0;self.reused=0;self.previous_position=0;self.hash_started=None
        self.state={'contract':'build151-ingestion-v1','pid':os.getpid(),'session_started':self.start,'phase':'DISCOVERING_SOURCES','last_progress_time':self.start,'bytes_read':0,'verified_count':0,'hash_reused_count':0}
        self.emit(force=True)
    def emit(self,force=False,**values):
        self.state.update(values);now=time.time()
        if not force and now-self.last_emit<1:return
        self.state.update(observed_at=now,bytes_read=self.total_read,verified_count=self.verified,hash_reused_count=self.reused,read_bytes_per_second=self.state.get("current_file_bytes_read",0)/max(.001,now-self.hash_started) if self.hash_started and self.state.get("phase")=="VERIFYING_CONTENT" else None)
        atomic(self.path,self.state);self.last_emit=now
    def phase(self,phase,**values):self.emit(force=True,phase=phase,last_progress_time=time.time(),**values)
    def inventory_progress(self,phase,**values):
        if self.cancel():raise IngestionInterrupted('INGESTION_INTERRUPTED')
        self.emit(force=phase!=self.state.get('phase'),phase=phase,last_progress_time=time.time(),**values)
    def verify(self,row):
        if self.cancel():raise IngestionInterrupted('CONTENT_VERIFY_INTERRUPTED')
        path=Path(row['path']);before=path.stat();meta=identity(before)
        if not stat.S_ISREG(before.st_mode):raise ValueError('SOURCE_NOT_REGULAR_FILE: '+str(path))
        key=row['source_id']+':'+row['relative_path'];entry=self.cache['sources'].get(key,{})
        digest=entry.get('full_sha256');trusted=isinstance(digest,str) and len(digest)==64 and all(c in '0123456789abcdef' for c in digest)
        if trusted and entry.get('metadata')==meta and entry.get('relative_path')==row['relative_path']:
            self.verified+=1;self.reused+=1;self.hash_started=None
            self.phase('REUSING_CONTENT_IDENTITY',current_source=row['relative_path'],current_source_id=row['source_id'],current_file_size=before.st_size,current_file_bytes_read=0,hash_policy='HASH_REUSE_ALLOWED',source_identity=meta,last_full_sha256=digest)
            return digest
        self.hash_started=time.time()
        self.phase('VERIFYING_CONTENT',current_source=row['relative_path'],current_source_id=row['source_id'],current_file_size=before.st_size,current_file_bytes_read=0,hash_policy='FULL_SHA_REQUIRED',source_identity=meta)
        h=hashlib.sha256();position=0
        from source_read_policy import verification_input
        with verification_input(path) as (f,io_policy):
            self.emit(force=True,content_read_policy=io_policy)
            if identity(os.fstat(f.fileno()))!=meta:raise ValueError('SOURCE_CHANGED_BEFORE_HASH: '+str(path))
            while True:
                if self.cancel():raise IngestionInterrupted('CONTENT_VERIFY_INTERRUPTED: '+str(path))
                chunk=f.read(1024*1024)
                if not chunk:break
                h.update(chunk);position+=len(chunk);self.total_read+=len(chunk)
                self.emit(current_file_bytes_read=position,last_progress_time=time.time())
            if identity(os.fstat(f.fileno()))!=meta or identity(path.stat())!=meta:raise ValueError('SOURCE_CHANGED_DURING_HASH: '+str(path))
        digest=h.hexdigest();self.cache['sources'][key]={'relative_path':row['relative_path'],'source_id':row['source_id'],'metadata':meta,'full_sha256':digest,'verified_at':time.time()}
        self.cache['cache_checksum']=hashlib.sha256(json.dumps(self.cache['sources'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        atomic(self.cache_path,self.cache);self.verified+=1
        self.phase('VERIFYING_CONTENT',current_file_bytes_read=position,last_full_sha256=digest)
        return digest
    def failure(self,error,row=None,stage=None):
        self.phase('ERROR',failed_stage=stage or self.state['phase'],error_type=type(error).__name__,error=str(error),errno=getattr(error,'errno',None),failed_path=(row or {}).get('path'),last_source=self.state.get('current_source'))
