"""Session-local cache of database aggregates only; live resources stay uncached."""
import hashlib,json,os,sqlite3
from contextlib import nullcontext
from pathlib import Path
class TerminalProjectionCache:
 def __init__(self):
  self.connection=None;self.path=None;self.key=None;self.payload=None;self.digest=None;self.context=None
  self.stats={'builds':0,'hits':0,'invalidations':0,'bypasses':0}
 def clear(self):
  self.key=self.payload=self.digest=None
 def close(self):
  self.clear()
  if self.connection is not None:self.connection.close();self.connection=None
  self.path=None
 def set_context(self,value):
  context=json.dumps(value,sort_keys=True,ensure_ascii=False,default=str)
  if context!=self.context:self.close();self.context=context
 @staticmethod
 def identity(path,required=False):
  path=Path(path)
  try:
   s=path.stat()
   # stat alone misses ACL changes and cannot establish current read permission.
   fd=os.open(path,os.O_RDONLY);os.close(fd)
   return (str(path.resolve()),s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_gid)
  except FileNotFoundError:
   if required:raise
   return None
 def token(self,root,c):
  db=root/'runtime/video.sqlite'
  paths=[(db,True),(Path(str(db)+'-wal'),False),(Path(str(db)+'-journal'),False),
         (root/'run-config.json',True),(root.parent/'task.json',False),(root.parent/'pipeline_state.json',False),
         (root/'image-plan.json',False),(root/'manifest.json',False)]
  files=tuple(self.identity(p,required) for p,required in paths)
  c.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
  return (self.context,files,c.execute('PRAGMA data_version').fetchone()[0],c.execute('PRAGMA schema_version').fetchone()[0],c.total_changes)
 def get(self,root,config,runner_alive,builder):
  root=Path(root).resolve();db=root/'runtime/video.sqlite'
  try:
   ident=self.identity(db,True)
   if self.path!=(str(db),ident[1],ident[2]):
    self.close();self.path=(str(db),ident[1],ident[2]);self.connection=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2);self.connection.row_factory=sqlite3.Row;self.connection.execute('PRAGMA query_only=ON')
   c=self.connection
   if c.in_transaction:raise RuntimeError('terminal_cache_transaction_overlap')
   c.execute('BEGIN')
   config_value=json.dumps(config,sort_keys=True,ensure_ascii=False)
   before=(self.token(root,c),config_value)
   state_path=root.parent/'pipeline_state.json'
   task_state=json.loads(state_path.read_text()) if state_path.exists() else {}
   eligible=task_state.get('status') not in {'running','queued','stopping'} and config.get('status') in {'PASS_BATCH','FAIL_BATCH','INTERRUPTED_RESUMABLE'} and runner_alive is False
   if eligible and before==self.key and self.payload is not None and hashlib.sha256(self.payload).hexdigest()==self.digest:
    value=json.loads(self.payload);self.stats['hits']+=1
   else:
    if self.key is not None:self.stats['invalidations']+=1
    self.clear()
    try:
     from . import query_trace
     span=query_trace.span('terminal_aggregates_build')
    except ImportError:span=nullcontext()
    with span:value=builder(c)
    self.stats['builds']+=1
    if not eligible:self.stats['bypasses']+=1
   c.execute('COMMIT')
   after=(self.token(root,c),config_value)
   if eligible and before==after:
    payload=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    if len(payload)<=8*1024*1024:self.payload=payload;self.digest=hashlib.sha256(payload).hexdigest();self.key=after
    else:self.clear()
   else:self.clear()
   return value
  except BaseException:
   if self.connection is not None and self.connection.in_transaction:self.connection.rollback()
   self.close();raise
