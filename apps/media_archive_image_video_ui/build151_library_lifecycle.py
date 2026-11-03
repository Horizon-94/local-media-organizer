"""Durable library catalog and lifecycle only. Never claims or computes processing work."""
import json,os,time,uuid,fcntl,sqlite3,shutil
from pathlib import Path
from contextlib import contextmanager

try:
 from .build151_state_context import current,ContextError
 from . import build151_registry_reconciliation as reconciliation
except ImportError:
 from build151_state_context import current,ContextError
 import build151_registry_reconciliation as reconciliation

def guard(p):return current().guard(p)
def read(p):
 value=json.loads(guard(p).read_text())
 if current().kind=='TEST':
  def check(v):
   if isinstance(v,dict):
    for k,x in v.items():
     if k in ('task_path','state_path','workspace','database','source_root','durable_library_path','original_task_path','library_task_path','output_root','task_state_path') and x:guard(x)
     check(x)
   elif isinstance(v,list):
    for x in v:check(x)
  check(value)
 return value
def atomic(p,value):
 p=guard(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name('.'+p.name+'.'+uuid.uuid4().hex+'.tmp')
 try:
  with tmp.open('w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,p);fd=os.open(p.parent,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 finally:
  if tmp.exists():tmp.unlink()

def root():return guard(current().state_root)
def registry():return root()/'runtime/library_registry_v1.json'
def log(event,**fields):
 try:
  p=guard(root()/'logs/lifecycle.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
  with p.open('a') as f:f.write(json.dumps({'at':time.time(),'pid':os.getpid(),'event':event,**fields},ensure_ascii=False)+'\n')
 except OSError:pass
@contextmanager
def locked():
 # Validate every shared write destination before even creating a lock.
 for target in (registry(),registry().with_suffix('.lock'),root()/'runtime/active_library.json',root()/'logs/lifecycle.jsonl'):guard(target)
 p=guard(registry().with_suffix('.lock'));p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('a') as f:fcntl.flock(f,fcntl.LOCK_EX);yield

def rows():
 p=registry();return read(p).get('libraries',[]) if p.exists() else []
def failure_details(task,state):
 report_path=Path(task['workspace'])/'reports/FINAL_BATCH_REPORT.json'
 report=read(report_path) if report_path.is_file() else {}
 failures=report.get('failures') or []
 detail=failures[0] if failures else {'reason':state.get('error') or '初始化未完成，数据库不存在','stage':state.get('failure_stage') or 'RUNTIME_DB_INITIALIZE'}
 reason=detail.get('reason') or state.get('error') or '初始化失败';stage=detail.get('stage') or state.get('failure_stage') or 'RUNTIME_DB_INITIALIZE'
 message=reason
 if reason.startswith('DISK_SPACE_BLOCKER:'):
  stage='STORAGE_PREFLIGHT'
  try:
   budget=json.loads(reason.split(':',1)[1]);message='索引盘空间不足：可用 %.2f GiB，该次预算要求 %.2f GiB'%(budget['available']/1024**3,budget['required']/1024**3)
  except (ValueError,KeyError):pass
 elif 'NO_SUPPORTED_SOURCES' in reason:stage='SOURCE_DISCOVERY'
 return {'stage':stage,'message':message,'root_cause':detail,'report_path':str(report_path)}

def record(task_path):
 p=guard(task_path);t=read(p);
 if current().kind=='TEST':
  for key in ('state_path','workspace','database','source_root'):guard(t[key])
 s=read(t['state_path']) if Path(t['state_path']).is_file() else {};counts=t.get('inventory_counts') or {}
 snap=s.get('build150_snapshot',{});counts=snap.get('files') or counts
 if not counts:
  manifest=Path(t['workspace'])/'manifest.json'
  if manifest.is_file():counts=read(manifest).get('counts',{})
 return {**(current().identity() if current().kind=='TEST' else {}),'library_id':t['task_id'],'task_id':t['task_id'],'task_name':t['name'],'task_path':str(p.resolve()),'database':t['database'],'source_root':t['source_root'],'created_at':t.get('created_at',''),'status':s.get('status',t.get('status','unknown')),'image_count':counts.get('image',0),'video_count':counts.get('video',0),'elapsed_seconds':max(0,(snap.get('finished_at') or time.time())-(s.get('started_at_epoch') or time.time())),'elapsed_human':'','registered_at':time.time()}

def create_project_directory(index_root,name):
 # User chooses index_root. Human project name is the only folder label.
 import re
 label=re.sub(r'[/\\:\x00-\x1f]','-',name).strip(' .') or '未命名项目'
 while len(label.encode('utf-8'))>180:label=label[:-1]
 root_path=guard(index_root);root_path.mkdir(parents=True,exist_ok=True)
 n=1
 while True:
  candidate=root_path/(label if n==1 else label+' ('+str(n)+')')
  try:guard(candidate).mkdir(exist_ok=False);return candidate
  except FileExistsError:n+=1

def register(task_path):
 r=record(task_path)
 with locked():
  old=rows();match=next((x for x in old if x.get('library_id')==r['library_id']),{})
  if match.get('durable_library_path'):r={**r,**match}
  old=[x for x in old if x.get('library_id')!=r['library_id'] and x.get('task_path')!=str(Path(task_path).resolve())];atomic(registry(),{'contract':'media_archive_library_registry_v1','libraries':old+[r]})
 log('library_register',library_id=r['library_id']);return r

def catalog(config=None):
 active=read(root()/'runtime/active_library.json') if (root()/'runtime/active_library.json').exists() else {};result=[]
 for entry in reconciliation.project(__import__(__name__,fromlist=[''])):
  if not entry['eligible']:continue
  raw=entry['row'];r=dict(raw)
  r.update(library_valid=True,source_online=entry['evidence']['source_online'],source_offline=entry['evidence']['source_online'] is False,source_availability=entry['evidence'].get('source_availability','unknown'),classification=entry['classification'])
  if not r.get('library_id'):
   try:r={**record(r['task_path']),**r}
   except (OSError,ValueError,KeyError):continue
  # Durable location is an identity/persistence fact, not a frozen task status.
  # A later incremental run may be running or stopped while the library remains durable.
  current_task=Path(r['durable_library_path'])/'task.json' if r.get('durable_library_path') else Path(r['task_path'])
  try:r.update(record(current_task))
  except (OSError,ValueError,KeyError):pass
  task_path=Path(r['task_path'])
  try:
   task=read(task_path);state=read(task['state_path']) if Path(task['state_path']).is_file() else {}
   r.update(project_id=r['library_id'],project_name=task['name'],workspace_path=task['workspace'],database_identity={'project_id':r['library_id'],'database_path':str(Path(task['database']).resolve())},last_opened_at=raw.get('last_opened_at'),last_processed_at=state.get('updated_at_epoch') or state.get('finished_at_epoch'),source_count=r.get('image_count',0)+r.get('video_count',0),current_state=r['status'],resumable=r['status'] not in ('success','CREATE_FAILED','initializing') and bool(task.get('source_root')))
  except (OSError,ValueError,KeyError):pass
  r['is_active']=r['library_id']==active.get('library_id',active.get('task_id'))
  if r.get('database') and not Path(r['database']).is_file():
   try:
    task=read(r['task_path']);state=read(task['state_path']) if Path(task['state_path']).is_file() else {};pid=state.get('worker_pid')
    if current().kind=='TEST' and pid:raise ContextError('WORKER_NOT_ENABLED')
    import subprocess
    command=subprocess.run([str(Path(__file__).resolve().parents[4]/'Helpers/ps-readonly'),'-p',str(pid),'-o','command='],capture_output=True,text=True).stdout if pid else ''
    initializing=str(r['task_path']) in command and 'product-worker' in command
    r.update(status='initializing' if initializing else 'CREATE_FAILED',failure_stage=None if initializing else 'RUNTIME_DB_INITIALIZATION',failure_reason=None if initializing else '初始化未完成，数据库不存在')
   except (OSError,ValueError,KeyError):r.update(status='CREATE_FAILED',failure_stage='TASK_MANIFEST')
  try:
   task=read(r['task_path']);state=read(task['state_path']) if Path(task['state_path']).is_file() else {}
   telemetry=Path(task['workspace']).parent/'ingestion_state.json'
   if telemetry.is_file():
    pending=read(telemetry)
    if pending.get('pid')==state.get('worker_pid') and state.get('status') in ('running','queued'):r['lifecycle_phase']=pending.get('phase')
   if r.get('status') in ('CREATE_FAILED','failed'):
    cause=failure_details(task,state);r.update(failure_stage=cause['stage'],failure_reason=cause['message'])
  except (OSError,ValueError,KeyError):pass
  if r['source_offline']:r['task_name']=str(r.get('task_name') or r['library_id'])+'（原素材离线）'
  result.append(r)
 log('history_load',count=len(result));return result

def resolve(value):
 for entry in reconciliation.project(__import__(__name__,fromlist=[''])):
  if not entry['eligible']:continue
  r=entry['row']
  if value in [r.get('library_id'),r.get('task_id'),r.get('task_path'),r.get('original_task_path')]:
   p=Path(r.get('durable_library_path') or Path(r['task_path']).parent)/'task.json' if r.get('durable_library_path') else Path(r['task_path'])
   if not p.is_file():raise FileNotFoundError('Library manifest missing: '+str(p))
   t=read(p)
   if current().kind=='TEST':
    for key in ('state_path','workspace','database','source_root'):guard(t[key])
   if not Path(t['database']).is_file():raise FileNotFoundError('DB missing: '+t['database'])
   return r,p,t
 raise ValueError('Registry entry invalid: '+str(value))

def select(value):
 r,p,t=resolve(value)
 with locked():
  active_path=root()/'runtime/active_library.json'
  active=read(active_path) if active_path.is_file() else {}
  if active.get('task_path') and active.get('library_id')!=r['library_id']:
   old_task=read(active['task_path']);old_state=read(old_task['state_path']) if Path(old_task['state_path']).is_file() else {}
   if old_state.get('status') in ('queued','running','stopping') and old_state.get('worker_pid'):
    try:os.kill(int(old_state['worker_pid']),0)
    except ProcessLookupError:pass
    else:raise RuntimeError('PROJECT_SWITCH_REQUIRES_STOP')
  opened={**r,'last_opened_at':time.time()}
  entries=rows();atomic(registry(),{'contract':'media_archive_library_registry_v1','libraries':[opened if x.get('library_id')==r['library_id'] or x.get('task_path')==r['task_path'] else x for x in entries]})
  atomic(root()/'runtime/active_library.json',{'library_id':r['library_id'],'task_id':r['library_id'],'task_path':str(p),'library_task_path':str(p),'task_name':r['task_name'],'database':t['database'],'output_root':t['workspace'],'task_state_path':t['state_path'],'processing_engine':'BUILD150_ONLY','durable_library_path':r.get('durable_library_path')})
 log('current_selection',library_id=r['library_id'],path=str(p));return {'status':'PASS','message':'已打开原项目；可在运行状态继续处理','database_write':False,'model_run':False}

def select_initializing(identity):
 if current().kind=='TEST':raise ContextError('WORKER_NOT_ENABLED')
 r=next(x for x in rows() if x.get('library_id')==identity);p=Path(r['task_path']);t=read(p);state=read(t['state_path'])
 import subprocess
 pid=state.get('worker_pid');command=subprocess.run([str(Path(__file__).resolve().parents[4]/'Helpers/ps-readonly'),'-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
 if str(p) not in command or 'product-worker' not in command:raise RuntimeError('Initializer ownership unproven')
 with locked():
  r.update(status='initializing',lifecycle_state='INITIALIZING')
  atomic(registry(),{'contract':'media_archive_library_registry_v1','libraries':[r if x.get('library_id')==identity else x for x in rows()]})
  atomic(root()/'runtime/active_library.json',{'library_id':identity,'task_id':identity,'task_path':str(p),'library_task_path':str(p),'task_name':t['name'],'database':t['database'],'output_root':t['workspace'],'task_state_path':t['state_path'],'processing_engine':'BUILD150_ONLY','lifecycle_state':'INITIALIZING'})
 log('current_selection_initializing',library_id=identity,path=str(p))


def finalize(task_path):
 # The user's chosen task directory IS the library. Never copy to an app-defined root.
 task_path=guard(task_path).resolve();t=read(task_path);state=read(t['state_path']);report=read(Path(t['workspace'])/'reports/FINAL_BATCH_REPORT.json')
 if state.get('status')!='success' or report.get('status')!='PASS_BATCH':raise ValueError('Library not completed: '+str(task_path))
 if not Path(t['database']).is_file():raise FileNotFoundError('DB missing: '+t['database'])
 guard(task_path.parent/'library_manifest.json')
 guard(t['database']+'-wal')
 r=record(task_path);r.update(durable_library_path=str(task_path.parent),lifecycle_state='COMPLETED_SEARCHABLE',status='success')
 if t.get('original_task_path'):r['original_task_path']=t['original_task_path']
 if t.get('path_relocations'):r['path_relocations']=t['path_relocations']
 with locked():
  # No processing transaction or result is modified. Persist index files before publication.
  for f in [Path(t['database']),Path(t['database']+'-wal')]:
   if guard(f).is_file():fd=os.open(f,os.O_RDONLY);os.fsync(fd);os.close(fd)
  atomic(task_path.parent/'library_manifest.json',r)
  atomic(registry(),{'contract':'media_archive_library_registry_v1','libraries':[x for x in rows() if x.get('library_id')!=r['library_id'] and x.get('task_path')!=str(task_path)]+[r]})
 log('library_finalize',library_id=r['library_id'],path=str(task_path.parent),copy_created=False);return r


def relocate_payload(value):
 mappings={old:new for r in rows() for old,new in r.get('path_relocations',{}).items()}
 def convert(v):
  if isinstance(v,dict):return {k:convert(x) for k,x in v.items()}
  if isinstance(v,list):return [convert(x) for x in v]
  if isinstance(v,str):
   for old,new in mappings.items():
    if v==old or v.startswith(old+'/'):return new+v[len(old):]
  return v
 return convert(value)

def install(g):
 if current().kind=='TEST':raise ContextError('PRODUCT_INSTALL_NOT_ENABLED')
 g['register_library']=register;g['existing_libraries']=catalog;g['_relocate_library_payload']=relocate_payload
 oldload=g['load_active_library']
 def load(config):
  cfg=oldload(config);entry=reconciliation.current_valid(__import__(__name__,fromlist=['']),cfg)
  if not entry:
   # Read-only fallback B: no usable current selection, no guessed library.
   cfg=dict(cfg)
   for key in ('library_id','task_id','task_path','library_task_path','database','output_root','task_state_path','durable_library_path'):cfg.pop(key,None)
   return cfg
  r=entry['row'];t=entry['task'];p=Path(r['task_path'])
  cfg.update(task_path=str(p),library_task_path=str(p),database=t['database'],output_root=t['workspace'],task_state_path=t['state_path'],library_id=r['library_id'],source_online=entry['evidence']['source_online'])
  return cfg
 g['load_active_library']=load
 g['activate_library']=lambda config,task_path:select(str(task_path))
 original=g['SearchJobManager']._database_preflight
 def preflight(self):
  c,r=original(self)
  if c:
   try:
    from .search_jobs import _install_search_lineage_view
    c.execute('BEGIN');_install_search_lineage_view(c)
   except BaseException:c.close();raise
  return c,r
 g['SearchJobManager']._database_preflight=preflight
