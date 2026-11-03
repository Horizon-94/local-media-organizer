"""App boundary for NEW build150 tasks; no legacy scheduler or migration.

Installed by the candidate assembler as build150_product.py. The existing
verified engine owns ALL processing, SQLite facts and publication.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace
import uuid

ENGINE = 'BUILD150_ONLY'
TASK_CONTRACT = 'build150-production-task-v1'

def bridge():
    from . import native_bridge
    return native_bridge

def engine_path():
    return Path(__file__).resolve().parents[2]/'production_engine/mixed-media'

def setup_engine():
    path=engine_path()
    if not path.is_dir():raise RuntimeError('BUILD150_PRODUCTION_CLOSURE_MISSING')
    sys.path.insert(0,str(path))

def read(path, default=None):
    path=Path(path)
    return json.loads(path.read_text()) if path.is_file() else default

def save(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)

def require_task(path):
    path=Path(path).expanduser().resolve(strict=True);task=read(path)
    if task.get('task_contract')!=TASK_CONTRACT or task.get('processing_engine')!=ENGINE:
        raise RuntimeError('OLD_PROJECT_OUT_OF_SCOPE_CREATE_A_NEW_BUILD150_TASK')
    run=path.parent/'workspace'
    if task['workspace']!=str(run) or task['database']!=str(run/'runtime/video.sqlite'):
        raise RuntimeError('PRODUCTION_TASK_PATH_BINDING_MISMATCH')
    return task

def active(config):
    value=config.get('task_path') or read(bridge().active_library_path(),{}).get('task_path')
    if not value:raise RuntimeError('没有可以继续的build150任务')
    return Path(value),require_task(value)

def alive(pid):
    if not pid:return False
    try:os.kill(int(pid),0);return True
    except ProcessLookupError:return False
    except PermissionError:return True

def activate_library(config, task_path):
    b=bridge();task=require_task(task_path);path=Path(task_path).resolve()
    record={'task_path':str(path),'library_task_path':str(path),'task_id':task['task_id'],
            'task_name':task['name'],'task_directory':str(path.parent),
            'database':task['database'],'output_root':task['workspace'],
            'task_state_path':task['state_path'],'processing_engine':ENGINE}
    save(b.active_library_path(),record);b.register_library(path)
    return {'status':'PASS','message':'已选择build150素材库','database':task['database'],
            'database_write':False,'model_run':False}

def launch(config_path, task_path, resume=False, incremental=False):
    with (Path(task_path).parent/'launch.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _launch_unlocked(config_path,task_path,resume,incremental)

def _launch_unlocked(config_path, task_path, resume=False, incremental=False):
    task=require_task(task_path);b=bridge()
    state=read(task['state_path'],{})
    if alive(state.get('worker_pid')) and state.get('status') in ('queued','running','stopping'):
        raise RuntimeError('build150任务仍在运行，不会重复启动')
    command=b._helper_command(Path(config_path),'product-worker','--task',str(task_path),*(['--resume'] if resume else []))
    launch_config=read(config_path)
    declared=read(Path(config_path).resolve().parent/'runtime_contract.json',{})
    environment=b._task_worker_environment({**launch_config,'tools':declared.get('tools',{})},os.environ.copy())
    environment.update(PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                       MEDIA_ARCHIVE_MODEL_ROOT=str(b.selected_model_root()))
    if launch_config.get('whisper_diagnostics_root'):environment['MEDIA_ARCHIVE_WHISPER_DIAGNOSTICS_ROOT']=launch_config['whisper_diagnostics_root']
    if incremental:environment['BUILD150_INCREMENTAL']='1'
    log=Path(task['log_path']);log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('ab') as stream:
        process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,env=environment,start_new_session=True)
    save(task['state_path'],{'task_id':task['task_id'],'status':'queued','worker_pid':process.pid,
        'processing_engine':ENGINE,'scheduler_mode':'build150_streaming','stages':[],'started_at_epoch':time.time()})
    (Path(task_path).parent/'pipeline.pid').write_text(str(process.pid)+'\n')
    return {'status':'PASS','message':'build150动态处理已启动','path':str(task_path),
            'identifier':task['task_id'],'pid':process.pid,'processing_engine':ENGINE}

def start_task(config, config_path, args):
    b=bridge();lock=b.application_state_root()/'runtime/start_task.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pointer=read(b.active_library_path(),{})
        if pointer.get('task_path'):
            previous=require_task(pointer['task_path']);state=read(previous['state_path'],{})
            if state.get('status') in ('queued','running','stopping') and alive(state.get('worker_pid')):
                raise RuntimeError('已有build150任务正在运行')
        source=Path(args.source).expanduser().resolve(strict=True)
        root=Path(args.workspace_root).expanduser().resolve()
        name=' '.join(args.name.split())
        if not name or not source.is_dir():raise ValueError('任务名称与素材目录必填')
        if root in (Path('/'),Path.home()):raise ValueError('不能将系统根目录或用户主目录直接用作索引目录')
        if source==root or source in root.parents or root in source.parents:raise ValueError('索引与素材目录必须分离')
        if getattr(args,'task_mode','full')!='full':raise ValueError('新任务必须使用完整整理')
        from .build151_library_lifecycle import create_project_directory
        task_dir=create_project_directory(root,name)
        path=task_dir/'task.json';run=task_dir/'workspace'
        profile=b._load_or_default_profile(config)
        profile['scheduler']={'mode':'build150_streaming','event_driven_database_handoff':True,
            'qwen_workers':1,'whisper_workers':1,'tail_boost':False,
            'runtime_may_silently_increase_workers':False}
        task={'task_contract':TASK_CONTRACT,'processing_engine':ENGINE,'task_id':'build150_'+uuid.uuid4().hex,
              'name':name,'source_root':str(source),'source_access':'read_only','workspace':str(run),
              'database':str(run/'runtime/video.sqlite'),'state_path':str(task_dir/'pipeline_state.json'),
              'log_path':str(task_dir/'logs/pipeline.log'),'created_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'software_version':'1.3.2','scheduler_mode':'build150_streaming','mode':'full',
              'profile':profile,'qwen_workers':1,'whisper_workers':1,'tail_boost':False,
              'source_scope':'IMAGE_VIDEO_ONLY','status':'queued'}
        save(path,task);activate_library(config,path)
        return launch(config_path,path)

def resume_active_task(config, config_path, **options):
    if options.get('repair_media_tools_request_id'):raise RuntimeError('BUILD150_SCOPED_REPAIR_NOT_REQUESTED')
    path,task=active(config);state=task_state({**config,'task_path':str(path)})
    if state and state.get('status')=='success':
        return {'status':'PASS','message':'已完成，DONE与已发布搜索结果不会重跑','processing_engine':ENGINE}
    return launch(config_path,path,resume=True)

def start_existing_task(config,config_path,args):
    require_task(args.task)
    if args.task_mode!='incremental':raise RuntimeError('BUILD150维护仅使用现有增量入口；不会回退旧引擎')
    activate_library(config,args.task)
    return launch(config_path,args.task,resume=True,incremental=True)

def execute_product_task(task_path, resume=False, **unused):
    with (Path(task_path).parent/'production.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _execute_locked(task_path,resume=resume)

def _execute_locked(task_path, resume=False):
    task=require_task(task_path);os.environ['MEDIA_ARCHIVE_BUILD150_TASK']=str(Path(task_path).resolve())
    setup_engine()
    from processing_entry import execute
    args=SimpleNamespace(incremental=os.environ.get('BUILD150_INCREMENTAL')=='1',audio_acceptance=False,
        mixed_smoke=False,image_video_release=True,prepared=None,source=Path(task['source_root']),
        workspace_root=Path(task_path).resolve().parent,run_id='workspace',inventory_json=None,
        resume=Path(task['workspace']) if resume else None,watchdog_seconds=1800)
    save(task['state_path'],{'task_id':task['task_id'],'status':'running','worker_pid':os.getpid(),
        'processing_engine':ENGINE,'scheduler_mode':'build150_streaming','stages':[],'started_at_epoch':time.time()})
    try:
        code=execute(args)
    except BaseException as error:
        save(task['state_path'],{'task_id':task['task_id'],'status':'failed','worker_pid':os.getpid(),
            'error':str(error),'traceback':traceback.format_exc(),'processing_engine':ENGINE,'stages':[]})
        raise
    finally:
        forbidden={'product_execution','v2_product_driver','scheduler_switching'}
        loaded=[name for name in sys.modules if name.rsplit('.',1)[-1] in forbidden]
        save(Path(task['workspace'])/'runtime/production-entry-audit.json',{
            'processing_engine':ENGINE,'pid':os.getpid(),'python':sys.executable,
            'entry':str(engine_path()/'processing_entry.py'),
            'task':str(Path(task_path).resolve()),'database':task['database'],
            'forbidden_legacy_modules':loaded,'legacy_import_fence':True,
            'build149_processing_runtime_calls':0 if not loaded else None,
            'qwen_workers':1,'whisper_workers':1,'tail_boost':False})
    final=task_state({'task_path':str(task_path)})
    save(task['state_path'],final)
    return {'status':'PASS' if code==0 else 'FAIL','exit_code':code,'processing_engine':ENGINE,
            'message':'build150已完成' if code==0 else final.get('error','build150已停止，可继续')}

def task_state(config):
    if not config.get('task_path') and not read(bridge().active_library_path(),{}).get('task_path'):
        return None
    path,task=active(config)
    state=read(task['state_path'],{})
    run=Path(task['workspace']);cfg=read(run/'run-config.json',{})
    status=cfg.get('status','RUNNING' if alive(state.get('worker_pid')) else 'PENDING')
    mapped={'PASS_BATCH':'success','FAIL_BATCH':'failed','INTERRUPTED_RESUMABLE':'recoverable','RUNNING':'running'}
    result={**state,'task_id':task['task_id'],'processing_engine':ENGINE,'scheduler_mode':'build150_streaming',
            'status':mapped.get(status,'queued'),'stages':[]}
    if (run/'runtime/video.sqlite').exists():
        setup_engine()
        from mixed_observer import snapshot
        try:snap=snapshot(run)
        except sqlite3.OperationalError:
            # The bootstrap schema transaction may still be in flight; do not
            # fall back to a different engine or claim completion.
            return {**result,'status':'running','reason_code':'BUILD150_SCHEMA_INITIALIZING'}
        result['build150_snapshot']=snap
        stage_keys={'qwen':'qwen_optional_v2','embedding':'embedding_optional_v2',
                    'ocr':'ocr_optional_v2','insightface':'person_reid_optional_v1'}
        names={'frame_basics':'基础画面信息','audio_extract':'视频音频抽取','vad':'人声检测（VAD）',
               'whisper':'语音转写（Whisper）','embedding':'文本搜索向量','qwen':'高价值画面描述（Qwen）',
               'yoloe':'物体识别（YOLOE）','openclip':'画面语义（OpenCLIP）','insightface':'人脸分析（InsightFace）','ocr':'画面文字（OCR）'}
        for stage,rows in snap['stages'].items():
            total=sum(x['count'] for x in rows.values());done=rows.get('DONE',{}).get('count',0)
            result['stages'].append({'key':stage_keys.get(stage,stage),'title':stage,'name':names.get(stage,stage),'status':'success' if done==total else 'running',
                'live_total':total,'live_success':done,'live_completed':done,'progress_percent':100*done/max(1,total),
                'configured_workers':1 if stage in ('qwen','whisper') else None})
        result['source_progress']=snap.get('sources',{});result['eta']=snap.get('eta',{})
        done_counts={s:r.get('DONE',{}).get('count',0) for s,r in snap['stages'].items()}
        result['v2_overview']={'source':{k:{'count':snap['sources'].get(k,{}).get('total',0),'bytes':0} for k in ('image','video')},
            'source_total_count':sum(v['total'] for v in snap['sources'].values()),
            'visual_unit_total_count':snap.get('coverage',{}).get('visual-vector',0),
            'recognition':{'openclip_visual_units':done_counts.get('openclip',0),
                'yoloe_detected_visual_units':done_counts.get('yoloe',0),
                'qwen_success':done_counts.get('qwen',0),'ocr_completed':done_counts.get('ocr',0),
                'text_vectors':snap.get('coverage',{}).get('vector',0)}}
        result['stage_count']=len(result['stages'])
        result['completed_stage_count']=sum(r['status']=='success' for r in result['stages'])
        result['current_stage_name']='、'.join(r['name'] for r in result['stages'] if r['status']=='running') or '处理完成'
        total=sum(v['total'] for v in snap['sources'].values())
        result['v2_live_progress']={'source_total':sum(cfg.get('inventory_counts',{}).values()) or total,
            'source_complete':sum(v['complete'] for v in snap['sources'].values()),
            'source_failed':sum(v['failed'] for v in snap['sources'].values()),
            'current_jobs_total':sum(x['count'] for rows in snap['stages'].values() for x in rows.values()),
            'current_jobs_done':sum(rows.get('DONE',{}).get('count',0) for rows in snap['stages'].values()),
            'current_jobs_pending':sum(rows.get('PENDING',{}).get('count',0) for rows in snap['stages'].values()),
            'current_jobs_running':sum(rows.get('RUNNING',{}).get('count',0) for rows in snap['stages'].values()),
            'current_jobs_retryable':sum(rows.get('RETRY_PENDING',{}).get('count',0) for rows in snap['stages'].values()),
            'current_jobs_failed_final':sum(rows.get('TERMINAL_FAILED',{}).get('count',0) for rows in snap['stages'].values())}
    if result['status']=='running' and not alive(result.get('worker_pid')):
        result.update(status='recoverable',reason_code='BUILD150_WORKER_EXITED_RESUME_REQUIRED')
    return result

def stop_active_task(config, *, origin='USER_UI', reason_code='EXPLICIT_USER_STOP'):
    path,task=active(config);state=read(task['state_path'],{});pid=state.get('worker_pid')
    if pid and alive(pid):
        # PID alone is not enough: only signal the recorded task process whose
        # current argv still explicitly owns this task.json.
        observed=subprocess.run([str(Path(__file__).resolve().parents[4]/'Helpers/ps-readonly'),'-p',str(pid),'-o','command='],capture_output=True,text=True,check=True).stdout
        if str(path.resolve()) not in observed:raise RuntimeError('BUILD150_STOP_PID_OWNERSHIP_UNPROVEN')
        from .stop_provenance import record
        record(Path(task['workspace'])/'runtime','STOP_INTENT',origin,reason_code,target_pid=int(pid),task_path=str(path))
        os.kill(int(pid),signal.SIGINT)
    return {'status':'PASS','message':'build150正在保存已完成结果并停止','processing_engine':ENGINE}

def quit_handoff(config,timeout_seconds=30):
    try:path,task=active(config)
    except RuntimeError:return {'status':'PASS','message':'没有运行任务'}
    stop_active_task(config,origin='APP_LIFECYCLE',reason_code='EXPLICIT_APP_QUIT');deadline=time.monotonic()+timeout_seconds
    while time.monotonic()<deadline:
        state=read(task['state_path'],{})
        if not alive(state.get('worker_pid')) or state.get('status') in ('success','failed','recoverable'):
            return {'status':'PASS','message':'build150已安全停止'}
        time.sleep(.25)
    return {'status':'FAIL','message':'build150仍在收尾，未强杀worker'}

def task_output_acceptance(config,state):
    if not state or state.get('status')!='success':return {}
    _,task=active(config);report=read(Path(task['workspace'])/'reports/FINAL_BATCH_REPORT.json',{})
    return {} if report.get('status')=='PASS_BATCH' else {'build150':'最终durable acceptance未通过'}

def blocked_legacy(*args,**kwargs):
    raise RuntimeError('BUILD149_PROCESSING_DISABLED_IN_BUILD150')
