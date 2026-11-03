"""App-owned serial snapshot reader. Fresh report, reusable same-connection lineage."""
import os,sys,time,json,sqlite3,signal,select,threading,contextlib
from pathlib import Path
# Tests may explicitly point to the installed code without copying the App.
contents=Path(os.environ.get('MEDIA_ARCHIVE_APP_CONTENTS',str(Path(__file__).resolve().parents[1])))
pipeline=contents/'Resources/Pipeline';sys.path.insert(0,str(pipeline))
os.environ.setdefault('MEDIA_ARCHIVE_APP_STATE_ROOT',str(Path.home()/'Library/Application Support/素材大整理/1.3.2-build151'))
os.environ.setdefault('MEDIA_ARCHIVE_PROJECT_ROOT',str(pipeline));os.environ.setdefault('MEDIA_ARCHIVE_ENV_ROOT',str(contents/'Resources/RuntimeEnvs'))
import importlib.abc
class NoLegacy(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.rsplit('.',1)[-1] in {'product_execution','v2_product_driver','scheduler_switching'}:raise RuntimeError('BUILD149_PROCESSING_DISABLED:'+fullname)
sys.meta_path.insert(0,NoLegacy())
from apps.media_archive_image_video_ui.search_session_store import SessionReadOnlyStore
class SnapshotStore(SessionReadOnlyStore):
 def _identity(self):
  p=self.path.resolve();parts=[str(p)]
  for suffix in ('','-wal','-journal'):
   try:
    s=Path(str(p)+suffix).stat();parts.append((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
   except FileNotFoundError:
    if not suffix:raise
    parts.append(None)
  return tuple(parts)

def main():
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--owner-pid',type=int,required=True);parser.add_argument('--initial-query-id');args=parser.parse_args()
 if os.getppid()!=args.owner_pid:raise RuntimeError('snapshot_owner_mismatch')
 from apps.media_archive_image_video_ui import native_bridge as bridge,query_trace as trace
 from apps.media_archive_image_video_ui.terminal_projection_cache import TerminalProjectionCache
 from apps.media_archive_image_video_ui import build150_product
 build150_product.setup_engine()
 import mixed_observer
 aggregate_cache=TerminalProjectionCache();observer_snapshot=mixed_observer.snapshot
 def cached_observer(root,connection=None):return observer_snapshot(root,connection,projection_cache=aggregate_cache)
 mixed_observer.snapshot=cached_observer
 original=sqlite3.connect;store=None;cancelled=False;cleaning=False;deadline=0.;finished=threading.Event()
 def stop(*_):
  nonlocal cancelled
  cancelled=True
 signal.signal(signal.SIGTERM,stop)
 def invalid():return False if cleaning else cancelled or os.getppid()!=args.owner_pid or bool(deadline and time.monotonic()>=deadline)
 class Bounded(sqlite3.Connection):
  def execute(self,*a,**k):
   if invalid():raise sqlite3.OperationalError('SNAPSHOT_INTERRUPTED')
   return super().execute(*a,**k)
  def set_progress_handler(self,callback,n):
   return super().set_progress_handler(lambda:int(invalid() or bool(callback and callback())),min(n or 10000,10000))
 def connect(*a,**k):
  k.setdefault('factory',Bounded);c=original(*a,**k);c.set_progress_handler(None,10000);return c
 sqlite3.connect=connect
 def owner_watch():
  while not finished.wait(.5):
   if os.getppid()!=args.owner_pid:stop();return
 threading.Thread(target=owner_watch,daemon=True).start()
 def send(kind,qid,**fields):print(json.dumps({'kind':kind,'query_id':qid,**fields},ensure_ascii=False),flush=True)
 last=time.monotonic();send('ready',None,pid=os.getpid())
 try:
  while not cancelled and time.monotonic()-last<60:
   if not select.select([sys.stdin],[],[],.5)[0]:continue
   line=sys.stdin.readline(65537)
   if not line:break
   if len(line)>65536:raise ValueError('snapshot_request_too_large')
   request=json.loads(line);qid=request.get('query_id')
   if not isinstance(qid,str) or not qid or len(qid)>128 or request.get('arguments')!=['snapshot']:raise ValueError('snapshot_request_invalid')
   deadline=time.monotonic()+15;trace.begin(qid);sample=trace.checkpoint();success=False
   try:
    cfg,repo,manager=bridge.load_runtime(Path(args.config))
    if manager is None:
     if store is not None:store.close();store=None
    else:
     if store is None or store.path.resolve()!=manager.db_path.resolve():
      if store is not None:store.close()
      store=SnapshotStore(manager.db_path)
     manager.session_store=store
     readiness=manager.readiness
     def compatible_readiness():
      value=readiness()
      preflight=value.get('database_preflight',{})
      if preflight.get('database_open_mode')=='readonly_session_snapshot':
       preflight.update(database_path_absolute=manager.db_path.is_absolute(),database_exists=True,database_is_file=True,database_readable=True,database_open_mode='readonly_uri')
      return value
     manager.readiness=compatible_readiness
    aggregate_cache.set_context({'config':cfg,'config_identity':aggregate_cache.identity(Path(args.config),True),'state_identities':{k:aggregate_cache.identity(Path(cfg[k]),True) for k in ('task_path','task_state_path','runtime_contract_path') if cfg.get(k)}})
    result=bridge.snapshot(cfg,repo,manager)
    if invalid():raise sqlite3.OperationalError('SNAPSHOT_INTERRUPTED')
    success=not result.get('database_read_error')
   except Exception as e:
    aggregate_cache.close()
    result={'status':'CANCELLED' if cancelled or os.getppid()!=args.owner_pid else 'INCOMPLETE' if time.monotonic()>=deadline else 'FAIL','error':type(e).__name__,'error_reason':str(e)}
   finally:
    cleaning=True
    try:
     if store is not None:
      store.finish(success and not cancelled and time.monotonic()<deadline)
      if not success or cancelled or time.monotonic()>=deadline:store.close();store=None
    finally:cleaning=False
   trace.complete('snapshot_total',sample)
   result['snapshot_session_metrics']={'pid':os.getpid(),'store':dict(store.stats) if store else None,'aggregates':dict(aggregate_cache.stats),'trace':trace.take()}
   send('result',qid,result=result);deadline=0.;last=time.monotonic()
 finally:
  finished.set();deadline=0.;cancelled=False
  if store is not None:store.close()
  aggregate_cache.close();mixed_observer.snapshot=observer_snapshot
  sqlite3.connect=original
if __name__=='__main__':main()
