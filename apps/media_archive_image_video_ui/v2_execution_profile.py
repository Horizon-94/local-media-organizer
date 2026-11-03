"""Frozen per-task measured capacities; no global concurrency reset."""
import copy
from .v2_producer import fingerprint
from .storage_compute_scheduler import workload_class

DEFAULT_WORKERS={'yoloe':1,'openclip':1,'qwen':1,'whisper':1,'ocr':2,'embedding':1,'insightface':1}
MEASURED_NEW_TASK_PROFILE={'workers':{'qwen':2,'whisper':1},
    'openclip_batch':8,'cheap_visual_batch':8,'source_affinity':True}


def freeze_profile(value):
    if value is None:return None
    if not isinstance(value,dict) or set(value)-{'workers','openclip_batch','cheap_visual_batch','source_affinity','contract','digest'}:
        raise ValueError('v2_execution_profile_invalid')
    workers=value.get('workers',{})
    if not isinstance(workers,dict) or set(workers)-set(DEFAULT_WORKERS):raise ValueError('v2_worker_profile_invalid')
    workers={**DEFAULT_WORKERS,**workers}
    if any(type(n) is not int or not 1<=n<=4 for n in workers.values()):raise ValueError('v2_worker_profile_invalid')
    if workers['qwen']>3 or workers['whisper']>2:raise ValueError('v2_heavy_worker_profile_invalid')
    clip=value.get('openclip_batch',1);cheap=value.get('cheap_visual_batch',8);affinity=value.get('source_affinity',False)
    if type(clip) is not int or clip not in (1,2,4,8):raise ValueError('v2_openclip_batch_invalid')
    if type(cheap) is not int or cheap not in (1,2,4,8,16,32):raise ValueError('v2_cheap_batch_invalid')
    if type(affinity) is not bool:raise ValueError('v2_source_affinity_invalid')
    profile={'contract':'v2_execution_profile_v1','workers':workers,'openclip_batch':clip,'cheap_visual_batch':cheap,'source_affinity':affinity}
    digest=fingerprint(profile)
    if 'contract' in value and value['contract']!=profile['contract']:raise ValueError('v2_execution_profile_contract_invalid')
    if 'digest' in value and value['digest']!=digest:raise ValueError('v2_execution_profile_digest_mismatch')
    return {**profile,'digest':digest}


def apply_profile(driver,task,*,runtime_workers=None):
    heavy_controller=None
    if task.get('heavy_admission_policy') and driver.admission_scheduler is not None:
        from .v2_heavy_admission import HeavyAdmissionController
        heavy_controller=HeavyAdmissionController(task['heavy_admission_policy'])
    profile=freeze_profile(task.get('v2_execution_profile'))
    if profile is None:
        if heavy_controller is not None:driver.admission_scheduler.heavy_controller=heavy_controller
        apply_runtime_workers(driver,runtime_workers)
        return
    configs=task.get('real_handler_configs') or {}
    for stage,pin in (task.get('v2_contracts') or {}).items():
        if fingerprint(configs.get(stage,{}))!=pin.get('config'):
            raise ValueError('v2_profile_handler_fingerprint_mismatch:'+stage)
    for stage,n in profile['workers'].items():
        if configs.get(stage,{}).get('resident_worker_limit')!=n:raise ValueError('v2_profile_worker_contract_mismatch:'+stage)
    if (task.get('openclip_batch_size')!=profile['openclip_batch'] or
        task.get('cheap_visual_batch_size')!=profile['cheap_visual_batch'] or
        task.get('visual_source_affinity')!=profile['source_affinity']):
        raise ValueError('v2_profile_task_fields_mismatch')
    scheduler=driver.admission_scheduler
    if scheduler is not None:
        policy=copy.deepcopy(scheduler.policy)
        for stage,n in profile['workers'].items():policy['compute'][workload_class(stage)]=n
        scheduler.policy=policy
        if heavy_controller is not None:scheduler.heavy_controller=heavy_controller
        controller=getattr(scheduler,'heavy_controller',None)
        if controller is not None:
            for stage in controller.selected:
                controller.policy['validated_limits'][stage]=min(controller.policy['validated_limits'][stage],profile['workers'][stage])
    apply_runtime_workers(driver,runtime_workers)


def apply_runtime_workers(driver,workers):
    """Execution-only override; frozen model/prompt/input fingerprints unchanged."""
    if not workers:return
    scheduler=driver.admission_scheduler
    if scheduler is None:return
    n=workers.get('qwen')
    if type(n) is not int or n not in (1,2,3):raise ValueError('qwen_runtime_workers_invalid')
    scheduler.policy=copy.deepcopy(scheduler.policy)
    scheduler.policy['compute'][workload_class('qwen')]=n
    controller=scheduler.heavy_controller
    controller.policy['validated_limits']['qwen']=min(n,controller.policy['ceilings']['qwen'])
    controller.selected['qwen']=min(n,controller.policy['validated_limits']['qwen'])
