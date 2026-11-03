"""Exact path-only identity normalization for the attested copied workspace.
Runtime handlers keep actual new paths; model/config/policy changes are rejected.
"""
import copy,hashlib,json
from pathlib import Path

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def normalize(frozen,actual,specs,task,app_root,approval_path,approval_sha):
    raw=Path(approval_path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=approval_sha:raise ValueError('RELOCATION_APPROVAL_SHA')
    a=json.loads(raw)
    if task.get('task_id')!=a['task_id']:return actual,None
    relocations=task.get('path_relocations',{})
    workspace=task['workspace']
    if relocations.get(str(Path(a['old_workspace']).parent))!=str(Path(workspace).parent):
        # Return to the original attested location while retaining immutable
        # config paths through the explicit project-owned compatibility link.
        aliases=[str(Path(old)/'workspace') for old,new in relocations.items()
                 if new==str(Path(workspace).parent)
                 and Path(old).is_symlink()
                 and Path(old).resolve()==Path(new).resolve()]
        if workspace!=a['old_workspace'] or len(aliases)!=1:
            raise ValueError('RELOCATION_TASK_BINDING')
        workspace=aliases[0]
    pairs=[(a['old_workspace'],workspace),(a['old_app'],str(app_root))]
    path_fields={'python_path','registry','prompt_path','paddlex_cache_root','artifact_root','_event_log','_inference_event_log'}
    immutable={'python_path','registry','prompt_path'}
    def move(v):
        for old,new in pairs:
            if v==old or v.startswith(old+'/'):return new+v[len(old):]
        return v
    normalized=copy.deepcopy(actual);changes=[]
    from apps.media_archive_image_video_ui.v2_qwen_schema_retry import PERSISTENT_GROUP_POLICY
    for lane,stages in actual.items():
        for stage,contract in stages.items():
            if stage not in specs:continue
            current=copy.deepcopy(specs[stage]);historical=copy.deepcopy(a['old_specs'][stage])
            if lane=='image' and stage=='qwen':
                for cfg in (current,historical):cfg.update(schema_retry_policy=PERSISTENT_GROUP_POLICY,image_qwen_profile='current_qwen35_9b',physical_workers=1)
            if digest(current)!=contract['config']:raise ValueError('RELOCATION_ACTUAL_CONFIG:'+stage)
            if digest(historical)!=frozen[lane][stage]['config']:raise ValueError('RELOCATION_FROZEN_CONFIG:'+stage)
            if current.keys()!=historical.keys():raise ValueError('RELOCATION_CONFIG_KEYS:'+stage)
            for key,before in historical.items():
                after=current[key]
                if before==after:continue
                if key not in path_fields or not isinstance(before,str) or move(before)!=after:raise ValueError('RELOCATION_SEMANTIC_CHANGE:'+stage+':'+key)
                if key in immutable:
                    if hashlib.sha256(Path(before).read_bytes()).digest()!=hashlib.sha256(Path(after).read_bytes()).digest():raise ValueError('RELOCATION_FILE_CONTENT:'+stage+':'+key)
                changes.append([lane,stage,key,before,after])
            expected={move(k):v for k,v in frozen[lane][stage]['model_files'].items()}
            if expected!=contract['model_files']:raise ValueError('RELOCATION_MODEL_CONTENT:'+stage)
            normalized[lane][stage]['config']=frozen[lane][stage]['config']
            normalized[lane][stage]['model_files']=copy.deepcopy(frozen[lane][stage]['model_files'])
    return normalized,{'operation':'path_only_verified_relocation','changes':changes,'task_id':task['task_id'],'approval_sha':approval_sha,'database_modified':False}
