"""Stopped-project source-set expansion. No scheduler, DB or model mutation."""
import json,os,time,hashlib,fcntl
from pathlib import Path
from contextlib import ExitStack
from . import build150_product as product
from . import build151_library_lifecycle as life

CONTRACT='build151-project-source-set-v1'
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def source_roots(manifest):return manifest.get('source_roots') or [manifest['source_root']]
def apply_descriptor(run,task):
 p=Path(run)/'project-source-set.json'
 if not p.is_file():return None
 v=life.read(p)
 if v.get('contract')!=CONTRACT or v.get('project_id')!=task['task_id'] or v.get('database')!=task['database'] or v.get('workspace')!=task['workspace']:raise ValueError('PROJECT_SOURCE_SET_BINDING_MISMATCH')
 inv=v['manifest'];roots=source_roots(inv)
 if not roots or roots[0]!=task['source_root'] or inv['source_root']!=task['source_root']:raise ValueError('PROJECT_PRIMARY_ROOT_CHANGED')
 if len(roots)!=len(set(roots)):raise ValueError('DUPLICATE_SOURCE_ROOT')
 return v

def plan_expansion(task,folder,*,inventory=None,ingestion=None,make_plan=None,combine=None,metadata=None):
 product.setup_engine()
 from progressive_ingestion import Ingestion
 from mixed_inventory import inventory as scan
 from release_incremental import combine as merge
 from apps.media_archive_image_video_ui.image_frontend import build_plan,metadata as image_metadata
 run=Path(task['workspace']);folder=Path(folder).expanduser().resolve(strict=True)
 if not folder.is_dir() or folder.is_symlink():raise ValueError('SOURCE_FOLDER_NOT_DIRECTORY')
 workspace=run.resolve()
 if folder==workspace or folder in workspace.parents or workspace in folder.parents:raise ValueError('SOURCE_WORKSPACE_OVERLAP')
 old=life.read(run/'manifest.json');oldplan=life.read(run/'image-plan.json')
 current=apply_descriptor(run,task)
 if current:
  # Prefer the runtime's refreshed media probe metadata, but require identical membership.
  if {x['source_id'] for x in old['files']}!={x['source_id'] for x in current['manifest']['files']}:old=current['manifest']
  oldplan=current['plan']
 roots=source_roots(old);source=next((x for x in roots if Path(x).exists() and folder.samefile(Path(x))),str(folder));folder=Path(source)
 inv=(inventory or scan)(folder,include_audio=False,defer_video_probe=True)
 candidates=[x for x in inv['files'] if x['media_kind'] in ('image','video')]
 oldrows=old['files'];byid={x['source_id']:x for x in oldrows};bypath={str(Path(x['path']).resolve()):x for x in oldrows};new=[];duplicates=[];existing=[]
 sizes={x['bytes'] for x in candidates};ing=ingestion or Ingestion(Path(task['workspace']).parent)
 hashes={}
 for row in oldrows:
  if row['bytes'] not in sizes:continue
  path=Path(row['path'])
  if not path.is_file():raise ValueError('EXISTING_SOURCE_OFFLINE_RELINK_REQUIRED')
  st=path.stat()
  if [st.st_size,st.st_mtime_ns,st.st_ino]!=[row['bytes'],row['mtime_ns'],row['inode']]:raise ValueError('EXISTING_SOURCE_REVISION_CHANGED_RESUME_FIRST')
  sha=ing.verify(row);hashes.setdefault((row['media_kind'],sha),row)
 for original in candidates:
  row=dict(original);path=str(Path(row['path']).resolve())
  if row['source_id'] in byid or path in bypath:existing.append(row['source_id']);continue
  physical=row['relative_path'];prefix=hashlib.sha256(source.encode()).hexdigest()
  if source!=old['source_root']:row.update(source_root=source,physical_relative_path=physical,relative_path='__source_roots/'+prefix+'/'+physical)
  sha=ing.verify(row);prior=hashes.get((row['media_kind'],sha))
  if prior:
   if row['media_kind']=='image':
    from apps.media_archive_image_video_ui.v2_task_materializer import _read_finder_xattr
    if sorted(_read_finder_xattr(Path(row['path']))[0])!=sorted(_read_finder_xattr(Path(prior['path']))[0]):raise ValueError('DUPLICATE_MEDIA_METADATA_CONFLICT')
   duplicates.append({'path':row['path'],'existing_source_id':prior['source_id'],'media_sha256':sha,'reason':'EXACT_MEDIA_DUPLICATE'});continue
  hashes[(row['media_kind'],sha)]=row;new.append(row)
 rows=oldrows+new
 if len({x['source_id'] for x in rows})!=len(rows) or len({x['relative_path'] for x in rows})!=len(rows):raise ValueError('EXPANSION_SOURCE_IDENTITY_COLLISION')
 fields=(metadata or image_metadata)([Path(x['path']) for x in new if x['media_kind']=='image'],required=True)
 delta={**inv,'files':[{**x,'metadata':fields.get(x['path'],{})} if x['media_kind']=='image' else x for x in new]+[x for x in inv['files'] if x['media_kind']=='sidecar']}
 if new:
  cohort=(make_plan or build_plan)(delta);plan=(combine or merge)(oldplan.get('cohorts',[oldplan])+[cohort],rows)
 else:plan=oldplan
 newroots=roots+([source] if source not in roots else [])
 from collections import Counter
 manifest={**old,'files':rows,'source_roots':newroots,'counts':dict(Counter(x['media_kind'] for x in rows))}
 return dict(contract=CONTRACT,classification='PROJECT_SOURCE_SET_EXPANSION',project_id=task['task_id'],workspace=task['workspace'],database=task['database'],manifest=manifest,plan=plan,counts={'existing_sources':len(oldrows),'new_sources':len(new),'total_sources':len(rows),'duplicates_ignored':len(duplicates),'already_registered':len(existing)},duplicates=duplicates,expanded_at=time.time(),source_set_revision=(current or {}).get('source_set_revision',0)+(1 if new or source not in roots else 0))

def add_folder(config,config_path,folder,project_id):
 path,task=product.active(config)
 if task['task_id']!=project_id:raise ValueError('PROJECT_SELECTION_CHANGED')
 with ExitStack() as stack:
  for name in ['launch.lock','production.lock']:
   handle=stack.enter_context((path.parent/name).open('a'));fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
  state=product.read(task['state_path'],{})
  if product.alive(state.get('worker_pid')):raise ValueError('STOP_PROJECT_BEFORE_ADD_FOLDER')
  log=path.parent/'logs/source-expansion.jsonl';log.parent.mkdir(exist_ok=True);(path.parent/'source-expansion.pid').write_text(str(os.getpid()))
  def event(stage,**values):
   with log.open('a') as f:f.write(json.dumps({'at':time.time(),'pid':os.getpid(),'project_id':project_id,'stage':stage,**values},ensure_ascii=False)+'\n')
  event('ADD_FOLDER_START',folder=str(folder))
  launched=None
  try:
   value=plan_expansion(task,folder);counts=value['counts']
   with life.locked():
    active=life.read(life.root()/'runtime/active_library.json')
    if (active.get('library_id') or active.get('task_id'))!=project_id:raise ValueError('PROJECT_SELECTION_CHANGED')
    if counts['new_sources']:
     # Publish resumability first. A crash before descriptor commit only resumes existing work;
     # a crash after commit can always consume the durable expansion with normal Resume.
     product.save(task['state_path'],{**state,'status':'recoverable','source_set_pending':True})
    destination=Path(task['workspace'])/'project-source-set.json';life.atomic(destination,value);event('SOURCE_SET_COMMITTED',counts=counts)
    if counts['new_sources']:
     # Both launch and production locks are held; the child acquires production after return.
     launched=product._launch_unlocked(config_path,path,resume=True)
  except BaseException as error:event('ADD_FOLDER_FAILED',reason=str(error));raise
  message='现有 %d · 新增 %d · 总计 %d · 重复忽略 %d · 已登记 %d'%(counts['existing_sources'],counts['new_sources'],counts['total_sources'],counts['duplicates_ignored'],counts['already_registered'])
  return {'status':'PASS','message':message,'counts':counts,'project_id':project_id,'database':task['database'],'classification':'PROJECT_SOURCE_SET_EXPANSION','launched':launched,'database_write':False}
