"""Read native build150 model bindings; never require a legacy task owner."""
import json
from pathlib import Path
from .v2_producer import fingerprint

def _bindings(c):
    names={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    if not {'video_stream_control_v1','release_search_heads','v2_vectors'}<=names:return []
    main=next((r[2] for r in c.execute('PRAGMA database_list') if r[1]=='main'),None)
    if not main:return []
    db=Path(main).resolve();task_path=db.parent.parent.parent/'task.json'
    if not task_path.is_file():return []
    task=json.loads(task_path.read_text())
    if (task.get('processing_engine')!='BUILD150_ONLY' or
        task.get('task_contract')!='build150-production-task-v1' or task.get('database')!=str(db)):
        raise ValueError('BUILD150_SEARCH_DATABASE_BINDING_INVALID')
    model_data=json.loads((db.parent/'model-config.json').read_text())
    configs=model_data['specs']
    relocation=None
    result=[]
    for stage,prefix in [('embedding','text:'),('openclip','visual:')]:
        cfg=configs.get(stage)
        if not cfg:continue
        for row in c.execute('''SELECT DISTINCT j.provenance FROM release_search_heads h
            JOIN v2_active_sources a ON h.source_id=a.source_id AND h.revision=a.source_revision
            CROSS JOIN v2_current_jobs j ON a.source_id=j.source_id AND a.source_revision=j.source_revision
            WHERE j.stage=? AND j.status='DONE' AND h.visible=1''',(stage,)):
            proof=json.loads(row[0]);namespace=prefix+fingerprint(proof)
            if fingerprint(cfg)!=proof.get('config'):
                # Use the same exact path-only attestation as processing Resume.
                # Model, config, registry, prompt and runtime content changes still fail.
                if relocation is None:
                    from .build150_product import setup_engine,engine_path
                    setup_engine()
                    from resume_relocation import normalize
                    frozen=json.loads((db.parent.parent/'contracts.json').read_text())
                    # These are immutable persisted contracts, not a freshly built
                    # runtime config. Attest them in their recorded App namespace.
                    # The existing approval SHA, config and model checks remain mandatory.
                    approval_path=engine_path()/'approved_workspace_relocation.json'
                    approval=json.loads(approval_path.read_text())
                    recorded_app=Path(__file__).resolve().parents[5]
                    old_app=approval['old_app']
                    if all(not spec.get('python_path') or str(spec['python_path']).startswith(old_app+'/Contents/')
                           for spec in configs.values() if isinstance(spec,dict)):
                        recorded_app=Path(old_app)
                    relocation,receipt=normalize(frozen,model_data['contracts'],configs,task,
                        recorded_app,approval_path,
                        '1c0e3e9d806da014f39097e7ab99ac3454c659dd1eb744e204f6127827184700')
                    if receipt is None:raise ValueError('BUILD150_SEARCH_RELOCATION_UNPROVEN')
                expected={stages[stage]['config'] for stages in relocation.values() if stage in stages}
                if expected!={proof.get('config')}:raise ValueError('BUILD150_SEARCH_CONFIG_PROVENANCE_MISMATCH')
            if not Path(cfg.get('model_path','')).exists():continue
            if c.execute('SELECT 1 FROM v2_vectors WHERE namespace=? LIMIT 1',(namespace,)).fetchone():
                result.append((stage,namespace,cfg,proof))
    return result


# Only enabled by the App-owned query session. Never spans a transaction.
from contextvars import ContextVar
_query_bindings=ContextVar('query_bindings',default=None)
def begin_query_bindings():return _query_bindings.set({})
def end_query_bindings(token):_query_bindings.reset(token)
def bindings(c):
    cache=_query_bindings.get()
    if cache is None:return _bindings(c)
    if not c.in_transaction:raise ValueError('query_bindings_requires_snapshot')
    key=id(c)
    if key not in cache:cache[key]=_bindings(c)
    return cache[key]
