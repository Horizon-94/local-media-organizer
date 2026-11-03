"""Product lifecycle adapter. The unchanged production engine owns DB/schema/jobs."""
import json, os, time, uuid, fcntl, sqlite3, subprocess
from pathlib import Path
from . import build150_product as product
from . import build151_library_lifecycle as lib

def event(stage, **fields):
    lib.log('new_task_event', stage=stage, **fields)

def control_config(path):
    path=Path(path).resolve(); content=path.parent.parent
    return {k:v.replace('$APP_CONTENTS',str(content)).replace('$APP_RESOURCES',str(content/'Resources')) if isinstance(v,str) else v for k,v in json.loads(path.read_text()).items()}

def validate(source, index_root, name):
    source=Path(source).expanduser().resolve(strict=True)
    root=Path(index_root).expanduser().resolve()
    name=' '.join(name.split())
    if not name or not source.is_dir():raise ValueError('任务名称与素材目录必填')
    if root in (Path('/'),Path.home()):raise ValueError('请选择专用索引文件夹')
    if source==root or source in root.parents or root in source.parents:raise ValueError('索引与素材目录必须分离')
    if not os.access(source,os.R_OK|os.X_OK):raise PermissionError('SOURCE_PERMISSION: '+str(source))
    # Only a bounded, representative read; never recursively scan the source here.
    for n,p in enumerate(source.iterdir()):
        if n>=128:break
        if p.is_file():
            with p.open('rb') as f:f.read(1)
            break
    parent=root
    while not parent.exists():parent=parent.parent
    if not parent.is_dir() or not os.access(parent,os.W_OK|os.X_OK):raise PermissionError('INDEX_PERMISSION: '+str(parent))
    return source,root,name

def resolveTaskDestination(index_root,task_name):
    return lib.create_project_directory(Path(index_root),task_name)

def start_task(config, config_path, args):
    event('CREATE_CLICK', source=str(args.source), index_root=str(args.workspace_root), name=args.name)
    try:source,root,name=validate(args.source,args.workspace_root,args.name)
    except Exception as error:
        event('DRAFT_VALIDATION_FAILED',error=str(error));raise
    event('DRAFT_VALIDATION',source=str(source),index_root=str(root),name=name)
    if getattr(args,'task_mode','full')!='full':raise ValueError('新任务必须使用完整整理')
    b=product.bridge();lock=b.application_state_root()/'runtime/start_task.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Only an owned live scheduler blocks creation. No previous DB is opened.
        commands=subprocess.check_output([str(Path(__file__).resolve().parents[4]/'Helpers/ps-readonly'),'-axo','command'],text=True).splitlines()
        owner_root=str(Path(__file__).resolve().parents[3])
        if any('product-worker' in cmd and owner_root in cmd for cmd in commands):
            raise RuntimeError('已有任务正在运行，请先安全停止')
        task_id=getattr(args,'task_id',None) or 'build150_'+uuid.uuid4().hex
        if not task_id.startswith('build150_') or len(task_id)!=41:raise ValueError('Invalid task identity')
        uuid.UUID(hex=task_id[9:])
        if any(r.get('library_id')==task_id for r in lib.rows()):raise ValueError('Task identity already registered')
        event('TASK_ID_ALLOCATE',task_id=task_id)
        profile=b._load_or_default_profile(config)
        task_dir=resolveTaskDestination(root,name);path=task_dir/'task.json';run=task_dir/'workspace';stage='TASK_ROOT_CREATE'
        event('TASK_DIRECTORY_RESOLVE',task_id=task_id,task_directory=str(task_dir))
        profile['scheduler']={'mode':'build150_streaming','event_driven_database_handoff':True,'qwen_workers':1,'whisper_workers':1,'tail_boost':False,'runtime_may_silently_increase_workers':False}
        task={'task_contract':product.TASK_CONTRACT,'processing_engine':product.ENGINE,'task_id':task_id,'name':name,'source_root':str(source),'source_access':'read_only','workspace':str(run),'database':str(run/'runtime/video.sqlite'),'state_path':str(task_dir/'pipeline_state.json'),'log_path':str(task_dir/'logs/pipeline.log'),'created_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'software_version':'1.3.2','scheduler_mode':'build150_streaming','mode':'full','profile':profile,'qwen_workers':1,'whisper_workers':1,'tail_boost':False,'source_scope':'IMAGE_VIDEO_ONLY','status':'initializing'}
        try:
            event(stage,task_id=task_id)
            event('WORKSPACE_PLANNED',task_id=task_id,path=str(run))
            event('RUNTIME_ROOT_PLANNED',task_id=task_id,path=str(run/'runtime'))
            lib.atomic(path,task);lib.atomic(task['state_path'],{'task_id':task_id,'status':'initializing','stages':[]})
            event('TASK_JSON_WRITE',task_id=task_id,task_path=str(path))
            stage='RUNTIME_DB_INITIALIZE'
            # Existing worker performs the real bootstrap. Do not fabricate SQLite.
            result=product.launch(config_path,path)
            event('BACKEND_INITIALIZER_START',task_id=task_id,pid=result['pid'])
            deadline=time.monotonic()+28
            while time.monotonic()<deadline:
                db=Path(task['database'])
                if db.is_file():
                    try:
                        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
                            ready=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='release_registry'").fetchone())
                        if ready:break
                    except sqlite3.Error:pass
                state=product.read(task['state_path'],{}) or {}
                if state.get('status')=='failed':
                    cause=lib.failure_details(task,state);stage=cause['stage'];raise RuntimeError(cause['message'])
                if not product.alive(result['pid']):raise RuntimeError('Backend initializer exited before database publication')
                time.sleep(.15)
            else:
                # Large inventories may still be bootstrapping; expose real initialization,
                # not a fabricated DB or a failure based only on elapsed time.
                if not product.alive(result['pid']):raise RuntimeError('Initializer no longer running: '+task['database'])
                lib.register(path);lib.select_initializing(task_id)
                event('INITIALIZING',task_id=task_id,task_path=str(path),pid=result['pid'])
                return {**result,'message':'任务正在初始化；数据库由处理后端创建','identifier':task_id,'path':str(path)}
            event('WORKSPACE_CREATE',task_id=task_id,path=str(run),owner='existing engine')
            event('RUNTIME_ROOT_CREATE',task_id=task_id,path=str(run/'runtime'),owner='existing engine')
            event(stage,task_id=task_id,database=task['database'],owner='existing Processing Engine bootstrap')
            stage='REGISTRY_REGISTER';lib.register(path);event(stage,task_id=task_id)
            stage='CURRENT_TASK_ACTIVATE';lib.select(task_id);event(stage,task_id=task_id)
            event('RUN_STATUS_BIND',task_id=task_id,task_path=str(path),name=name)
            return {**result,'message':'任务已初始化并开始处理','identifier':task_id,'path':str(path)}
        except BaseException as error:
            try:product.stop_active_task({'task_path':str(path)})
            except Exception:pass
            state=product.read(task['state_path'],{}) or {}
            cause=lib.failure_details(task,state)
            if cause.get('root_cause',{}).get('reason'):stage=cause['stage']
            lib.atomic(task['state_path'],{**state,'task_id':task_id,'status':'CREATE_FAILED','failure_stage':stage,'error':str(error),'failure_details':cause,'stages':[]})
            lib.register(path)
            event('CREATE_FAILED',task_id=task_id,failure_stage=stage,error=str(error),root_cause=cause,task_path=str(path))
            raise RuntimeError('创建失败 ['+stage+']: '+str(error)) from error
