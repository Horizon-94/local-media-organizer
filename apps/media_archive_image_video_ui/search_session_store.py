"""One App query session; short snapshots, reusable exact lineage proof.

PRAGMA data_version is compared only on the same read-only connection. Any
committed database change, file replacement, or failed transaction invalidates
reuse. TEMP rows are accelerators, never authority or durable job writes.
"""
import sqlite3,time,os,copy,json,hashlib
from pathlib import Path
from contextlib import contextmanager
from .search_jobs import _install_search_lineage_view

class SessionReadOnlyStore:
    def __init__(self,path):
        self.path=Path(path);self.connection=None;self.deadlines=[];self.token=None;self.active=False
        self.bindings_token=None;self.reuse_preparation=False;self.prepared_digest=None;self.prepared_bindings=None;self.prepared_readiness=None;self.context_token=lambda:None
        self.stats={'build_count':0,'reuse_count':0,'build_seconds':0.0}
    def _identity(self):
        s=self.path.resolve().stat();return (str(self.path.resolve()),s.st_dev,s.st_ino)
    def preparation_identity(self):
        files=[]
        for suffix in ('','-wal','-journal'):
            p=Path(str(self.path.resolve())+suffix)
            try:
                st=p.stat()
                if not suffix:
                    fd=os.open(p,os.O_RDONLY);os.close(fd)
                files.append((str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_mode,st.st_uid,st.st_gid))
            except FileNotFoundError:
                if not suffix:raise
                files.append((str(p),None))
        return (tuple(files),self.context_token())
    def preparation_current(self):
        if not self.reuse_preparation or self.connection is None or self.token is None:return False
        if self._identity()!=self.identity:return False
        c=self.connection
        current=(self.preparation_identity(),c.execute('PRAGMA data_version').fetchone()[0],c.execute('PRAGMA schema_version').fetchone()[0],c.total_changes)
        return current==self.token
    def clear_preparation(self):
        self.prepared_bindings=None;self.prepared_readiness=None;self.prepared_digest=None
    def preparation_digest(self):
        return hashlib.sha256(json.dumps([self.prepared_bindings,self.prepared_readiness],sort_keys=True).encode()).hexdigest()
    def begin(self):
        if self.active:raise RuntimeError('search_session_query_overlap')
        identity=self._identity()
        if self.connection is not None and identity!=self.identity:self.close()
        if self.connection is None:
            self.connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
            self.connection.row_factory=sqlite3.Row;self.identity=identity
        c=self.connection;c.execute('BEGIN');self.active=True
        try:
            c.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            files=self.preparation_identity() if self.reuse_preparation else None
            token=((files if self.reuse_preparation else identity),c.execute('PRAGMA data_version').fetchone()[0],c.execute('PRAGMA schema_version').fetchone()[0])
            if self.reuse_preparation:token=token+(c.total_changes,)
            if token!=self.token:
                self.clear_preparation()
                start=time.monotonic();c.set_progress_handler(lambda:int(time.monotonic()-start>30),10000)
                try:_install_search_lineage_view(c)
                finally:c.set_progress_handler(None,0)
                self.stats['build_count']+=1;self.stats['build_seconds']+=time.monotonic()-start
            else:self.stats['reuse_count']+=1
            c.execute('PRAGMA query_only=ON')
            self.token=token[:3]+(c.total_changes,) if self.reuse_preparation else token
            from .build150_search_bindings import begin_query_bindings
            self.bindings_token=begin_query_bindings()
            if self.reuse_preparation and self.prepared_digest is not None and self.preparation_digest()!=self.prepared_digest:self.clear_preparation()
            if self.reuse_preparation and self.prepared_bindings is not None:
                from .build150_search_bindings import _query_bindings
                _query_bindings.get()[id(c)]=copy.deepcopy(self.prepared_bindings)
        except BaseException:self.finish(False);raise
    @contextmanager
    def connect(self):
        if not self.active:self.begin()
        self.deadlines.append(time.monotonic()+30)
        c=self.connection;c.set_progress_handler(lambda:int(time.monotonic()>self.deadlines[-1]),10000)
        try:yield c
        finally:
            self.deadlines.pop()
            if not self.deadlines:c.set_progress_handler(None,0)
    def finish(self,success=True):
        if self.active:
            try:
                if success and self.reuse_preparation:
                    from .build150_search_bindings import _query_bindings
                    self.prepared_bindings=copy.deepcopy((_query_bindings.get() or {}).get(id(self.connection)))
                    self.prepared_digest=self.preparation_digest()
                self.connection.execute('COMMIT' if success else 'ROLLBACK')
                if self.reuse_preparation and (not success or not self.preparation_current()):
                    self.token=None;self.clear_preparation()
            finally:
                self.active=False
                if self.bindings_token is not None:
                    from .build150_search_bindings import end_query_bindings
                    end_query_bindings(self.bindings_token);self.bindings_token=None
                if not success:self.token=None
    def close(self):
        self.finish(False)
        if self.connection is not None:self.connection.close();self.connection=None
        self.token=None;self.clear_preparation()
