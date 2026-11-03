"""Fresh production-task workspace binding; no fixture/test directory bypass."""
import json
import os
from pathlib import Path

def task():
    filename=os.environ.get('MEDIA_ARCHIVE_BUILD150_TASK')
    if not filename:raise ValueError('BUILD150_PRODUCTION_TASK_REQUIRED')
    raw_path=Path(filename).expanduser().absolute()
    for node in [raw_path,*raw_path.parents]:
        if node.is_symlink():raise ValueError('BUILD150_WORKSPACE_SYMLINK_FORBIDDEN')
    path=raw_path.resolve(strict=True)
    value=json.loads(path.read_text())
    if value.get('task_contract')!='build150-production-task-v1' or value.get('processing_engine')!='BUILD150_ONLY':
        raise ValueError('BUILD150_PRODUCTION_TASK_IDENTITY_REQUIRED')
    if not value.get('task_id') or value.get('source_scope')!='IMAGE_VIDEO_ONLY':raise ValueError('BUILD150_TASK_SCOPE_INVALID')
    root=path.parent/'workspace';source=Path(value['source_root']).resolve(strict=True)
    if value['workspace']!=str(root) or value['database']!=str(root/'runtime/video.sqlite'):
        raise ValueError('BUILD150_PRODUCTION_PATH_BINDING_INVALID')
    forbidden=[Path('/'),Path.home(),source,Path(os.environ['MEDIA_ARCHIVE_MODEL_ROOT']).resolve()] if os.environ.get('MEDIA_ARCHIVE_MODEL_ROOT') else [Path('/'),Path.home(),source]
    if root in forbidden or any(p==root or p in root.parents for p in forbidden if p not in (Path('/'),Path.home())):
        raise ValueError('BUILD150_UNSAFE_WORKSPACE_ROOT')
    if source==root or source in root.parents or root in source.parents:raise ValueError('BUILD150_SOURCE_OUTPUT_OVERLAP')
    for node in [path,*path.parents]:
        if node.is_symlink():raise ValueError('BUILD150_WORKSPACE_SYMLINK_FORBIDDEN')
    return path,value

def validate_runtime(workspace):
    path,value=task()
    if Path(workspace).resolve()!=path.parent/'workspace/runtime':raise ValueError('BUILD150_RUNTIME_WORKSPACE_MISMATCH')

def validate_entry(output_root,source,run_id,resume=None):
    path,value=task()
    if Path(output_root).resolve()!=path.parent or Path(source).resolve()!=Path(value['source_root']).resolve():
        raise ValueError('BUILD150_ENTRY_BINDING_MISMATCH')
    if run_id!='workspace' or (resume is not None and Path(resume).resolve()!=path.parent/'workspace'):
        raise ValueError('BUILD150_ENTRY_RUN_ID_MISMATCH')
