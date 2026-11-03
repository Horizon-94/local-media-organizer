import os,sys,json,time,sqlite3
from pathlib import Path
C=Path(__file__).resolve().parents[1];sys.path.insert(0,str(C/'Resources/Pipeline'));os.environ.setdefault('MEDIA_ARCHIVE_APP_STATE_ROOT',str(Path.home()/'Library/Application Support/素材大整理/1.3.2-build151'))
from apps.media_archive_image_video_ui import native_bridge as b
from apps.media_archive_image_video_ui import build151_library_lifecycle as lib
def shutdown_read(path,default=None):
 try:return lib.read(path) if Path(path).is_file() else (default or {})
 except (OSError,ValueError) as error:lib.log('shutdown_metadata_unavailable',path=str(path),reason=str(error));return default or {}
try:
 command=sys.argv[-1] if sys.argv[-1] in ['library-catalog','shutdown'] else sys.argv[1]
 if command=='library-catalog':result={'status':'PASS','libraries':lib.catalog()}
 elif command=='reconcile-registry':
  request=json.loads(Path(sys.argv[2]).read_text())
  result=lib.reconciliation.reconcile(lib,request['expected_registry_sha'],request['expected_active_sha'],request.get('approved_removals',[]))
 elif command=='finalize':
  value=lib.finalize(sys.argv[2]);lib.select(value['library_id']);result={'status':'PASS','library':value}
 elif command=='query':
  pointer=lib.read(lib.root()/'runtime/active_library.json');r,p,t=lib.resolve(pointer.get('library_id') or pointer['task_path']);c=sqlite3.connect(Path(t['database']).as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
  try:
   from apps.media_archive_image_video_ui.v2_release_search import search
   hits=search(c,sys.argv[2],limit=5);result={'status':'PASS','count':len(hits),'model_run':False,'database_write':False,'database':t['database'],'sample':hits[:1]}
  finally:c.close()
  lib.log('search_query',count=len(hits),database=t['database'])
 elif command=='shutdown':
  lib.log('shutdown_begin');cfg=shutdown_read(lib.root()/'runtime/active_library.json');path=cfg.get('task_path');task=shutdown_read(path) if path else {};state=shutdown_read(task['state_path']) if task.get('state_path') else {};pid=state.get('worker_pid');owned=False
  if pid:
   import subprocess
   cmd=subprocess.run([str(Path(__file__).resolve().parents[1]/'Helpers/ps-readonly'),'-p',str(pid),'-o','command='],capture_output=True,text=True).stdout;owned=bool(path and path in cmd and 'product-worker' in cmd)
  if owned:
   result=b.quit_handoff(cfg,timeout_seconds=30);deadline=time.monotonic()+5
   while time.monotonic()<deadline and b._build150.alive(pid):time.sleep(.2)
   if b._build150.alive(pid):raise RuntimeError('Scheduler still active: PID '+str(pid))
   lib.log('worker_stop',pid=pid)
  for f in [lib.registry(),lib.root()/'runtime/active_library.json']:
   if f.exists():fd=os.open(f,os.O_RDONLY);os.fsync(fd);os.close(fd)
  lib.log('registry_flush');lib.log('db_close',connections='all lifecycle readers use finally close');result={'status':'PASS','message':'已保存素材库并完成安全退出准备'}
 else:raise ValueError('Unknown lifecycle command')
 print(json.dumps(result,ensure_ascii=False))
except Exception as e:lib.log('lifecycle_error',reason=str(e));print(json.dumps({'status':'FAIL','error':str(e)},ensure_ascii=False));raise SystemExit(2)
