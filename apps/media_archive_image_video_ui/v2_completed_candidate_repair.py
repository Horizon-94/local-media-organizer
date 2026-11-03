"""Audited ownership transition for candidate-only repair of completed tasks.

The completed run is immutable. A new run of the SAME task owns a narrowly
specified set of already-sealed image sources; no producer revision is created.
Existing job/result provenance is retained, not relabelled as new computation.
Execution/deployment must separately validate the repair driver before using it.
"""
import json
import copy
from pathlib import Path
from .central_database import connect
from .scheduler_identity import EVENT_DRIVEN_V2, start_scheduler_run, finish_scheduler_run
from .scheduler_identity import SchedulerOwnershipError
from .v2_product_lifecycle import ProductExecutionLease
from .v2_producer import fingerprint
from .v2_runtime import ContractError

REQUIRED=('yoloe','openclip','dhash','geometry','quality')

def plan_metadata_corrections(task):
    """Read original tags and existing contexts; no decode or model inference."""
    from .v2_task_materializer import _finder_metadata,_candidate_source_assets
    _,tags=_finder_metadata(task['v2_sources'])
    image_ids={r['source_content_id'] for r in task['v2_sources'] if r['media_kind']=='image'}
    tagged={r['source_content_id'] for r in tags}&image_ids
    with connect(Path(task['database']),readonly=True) as c:
        from .v2_shared_payload import load
        contexts={r['source_id']:(r['source_revision'],load(c,r['payload'])) for r in c.execute('''
            SELECT x.* FROM v2_selection_contexts x JOIN v2_active_sources a
            ON a.source_id=x.source_id AND a.source_revision=x.source_revision''')}
        groups={}
        for sid,(revision,context) in contexts.items():
            for group in {r['sequence_id'] for r in context['image_inputs']['timelapse_rows']}:
                groups.setdefault(group,[]).append(sid)
        scope=set(tagged)
        for members in groups.values():scope.update(members)
        corrections={}
        for sid in sorted(scope):
            if sid not in contexts:raise ContractError('tagged_image_own_preview_missing:'+sid)
            revision,context=contexts[sid]
            image=copy.deepcopy(context['image_inputs'])
            image['source_assets']=_candidate_source_assets(image['source_assets'])
            combined={json.dumps(r,sort_keys=True):r for r in [*image['finder_tags'],*tags]}
            image['finder_tags']=list(combined.values())
            sequence_ids={r['sequence_id'] for r in image['timelapse_rows']}
            deps=dict(context['dependencies'])
            if sequence_ids:
                if len(sequence_ids)!=1:raise ContractError('candidate_repair_mixed_sequence')
                members=groups[next(iter(sequence_ids))]
                deps={s:contexts[s][0] for s in members}
                for key in ('timelapse_rows','visual_mappings'):
                    rows={json.dumps(r,sort_keys=True):r for s in members for r in contexts[s][1]['image_inputs'][key]}
                    image[key]=list(rows.values())
            # The source inventory already exists durably in each immutable
            # context. Do not duplicate it 125 times into the task intent.
            corrections[sid]={'revision':revision,'before_sha':fingerprint(context),
                'dependencies':deps,'visual_mappings':image['visual_mappings'],
                'timelapse_rows':image['timelapse_rows'],'sequence':bool(sequence_ids)}
        return {'contract':'completed_candidate_metadata_repair_v1','base_run_id':task['scheduler_run_id'],
            'finder_tags':tags,'scope':{sid:r['revision'] for sid,r in corrections.items()},'corrections':corrections}

def install_metadata_corrections(selection,plan):
    """Restartable fenced amendments. Called before a repair scheduler pass."""
    for sid,entry in plan['corrections'].items():
        from .v2_task_materializer import _candidate_source_assets
        rev=entry['revision']
        with selection.store.connect() as c:current=selection.context(c,sid,rev)
        before=current['image_inputs']
        tags={json.dumps(r,sort_keys=True):r for r in [*before['finder_tags'],*plan['finder_tags']]}
        image={'finder_tags':list(tags.values()),'source_assets':_candidate_source_assets(before['source_assets']),
            'visual_mappings':entry['visual_mappings'],'timelapse_rows':entry['timelapse_rows']}
        desired={**current,'image_inputs':image}
        if entry['sequence']:desired['dependencies']=dict(sorted(entry['dependencies'].items()))
        if fingerprint(current)==fingerprint(desired):continue
        if fingerprint(current)!=entry['before_sha']:raise ContractError('candidate_repair_context_changed:'+sid)
        if entry['sequence']:
            selection.reconcile_image_context(sid,rev,entry['dependencies'],image)
        else:selection.reconcile_missing_finder_metadata(sid,rev,image)

def plan_failed_qwen_contract_retry(task,candidate_id,new_config,new_contract,request_id,*,repetition_only=False,same_parameters=False,schema_retry_only=False):
    """One failed candidate gets a new output-budget contract; good work stays."""
    old_config=task['real_handler_configs']['qwen'];old_contract=task['v2_contracts']['qwen']
    previous=task.get('candidate_repair') or {}
    anti_repeat=repetition_only or (previous.get('retry_candidate_id')==candidate_id and previous.get('run_handler_configs',{}).get('qwen',{}).get('max_tokens')==768)
    if schema_retry_only:
        from .v2_qwen_schema_retry import UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY
        if new_config not in ({**old_config,'schema_retry_policy':p} for p in (UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY)):
            raise ContractError('qwen_retry_only_schema_reask_policy_may_change')
    elif same_parameters:
        if new_config != old_config:
            raise ContractError('qwen_retry_frozen_parameters_changed')
    elif anti_repeat:
        if not repetition_only:
            old_config=previous['run_handler_configs']['qwen'];old_contract=previous['run_contracts']['qwen']
        if new_config!={**old_config,'repetition_penalty':1.1,'repetition_context_size':64}:
            raise ContractError('qwen_retry_only_authorized_repetition_control_may_change')
    elif ({k:v for k,v in new_config.items() if k!='max_tokens'}!={k:v for k,v in old_config.items() if k!='max_tokens'} or
        type(new_config.get('max_tokens')) is not int or not old_config['max_tokens']<new_config['max_tokens']<=768):
        raise ContractError('qwen_retry_only_output_budget_may_change')
    if (new_contract.get('stage')!='qwen' or new_contract['revision']!=old_contract['revision']+1 or
        new_contract['config']!=fingerprint(new_config) or new_contract['model_files']!=old_contract['model_files'] or
        new_contract['prompt']!=old_contract['prompt']):raise ContractError('qwen_retry_contract_identity_changed')
    with connect(Path(task['database']),readonly=True) as c:
        from .v2_current_jobs_cache import install_readonly_view
        install_readonly_view(c)
        job=c.execute("SELECT * FROM v2_current_jobs WHERE stage='qwen' AND item_id=?",(candidate_id,)).fetchone()
        if not job or job['status']!='FAILED_FINAL' or job['result_sha']:
            raise ContractError('qwen_retry_requires_uncommitted_final_failure')
        sid=job['source_id'];rev=job['source_revision']
        owner=c.execute('''SELECT r.* FROM v2_source_epochs e JOIN scheduler_runs r
            ON r.scheduler_run_id=e.scheduler_run_id AND r.execution_epoch=e.execution_epoch
            WHERE e.source_id=? AND e.source_revision=?''',(sid,rev)).fetchone()
        safe_recoverable=(owner and owner['status']=='RECOVERABLE' and owner['scheduler_run_id']==task['scheduler_run_id'])
        if safe_recoverable:
            if c.execute("SELECT 1 FROM v2_jobs WHERE status='RUNNING' LIMIT 1").fetchone() or c.execute("SELECT 1 FROM scheduler_admissions WHERE status='ACTIVE' LIMIT 1").fetchone():
                raise ContractError('qwen_retry_recoverable_run_not_quiescent')
        if not owner or owner['task_id']!=task['task_id'] or (owner['status']!='COMPLETED' and not safe_recoverable):
            raise ContractError('qwen_retry_source_owner_not_completed_same_task')
        dependencies=[r[0] for r in c.execute('SELECT parent_key FROM v2_job_dependencies WHERE child_key=?',(job['execution_key'],))]
    contracts=copy.deepcopy(task['v2_contracts']);contracts['qwen']=copy.deepcopy(new_contract)
    configs=copy.deepcopy(task['real_handler_configs']);configs['qwen']=copy.deepcopy(new_config)
    return {'contract':'completed_candidate_metadata_repair_v1','base_run_id':owner['scheduler_run_id'],
        'scope':{sid:rev},'finder_tags':copy.deepcopy(task['finder_tags']),'corrections':{},
        'retry_request_id':request_id,'retry_candidate_id':candidate_id,
        'retry_job':dict(job),'retry_dependencies':dependencies,
        'run_contracts':contracts,'run_handler_configs':configs,
        'reason':'user_requested_single_failed_qwen_uncertainty_schema_reask' if schema_retry_only else 'user_requested_single_failed_qwen_same_parameters' if same_parameters else 'user_authorized_single_failed_qwen_repetition_penalty_1_1_context_64' if anti_repeat else 'user_authorized_single_failed_qwen_output_budget_768'}

def enqueue_failed_qwen_contract_retry(database,run,plan):
    from .scheduler_switching import FencedWorkerStore
    store=FencedWorkerStore(database,task_id=run['task_id'],scheduler_run_id=run['scheduler_run_id'],execution_epoch=run['execution_epoch'])
    job=plan['retry_job'];policy=plan['run_contracts']['qwen']
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        key=store.enqueue(c,'qwen',job['item_id'],policy['revision'],job['source_id'],job['source_revision'],
            job['input_sha'],policy,json.loads(job['payload']),dependencies=plan['retry_dependencies'])
        if not key:raise ContractError('qwen_retry_evidence_dependency_missing')
        store.candidate_status(c,'qwen',job['item_id'],'QUEUED')
        store.emit(c,'CANDIDATE_OUTPUT_CONTRACT_RETRY',job['source_id'],job['source_revision'],policy['revision'],
            {'old_execution_key':job['execution_key'],'new_execution_key':key,'request_id':plan['retry_request_id'],
             'reason':plan['reason'],'old_contract':json.loads(job['provenance']),'new_contract':policy})
    return key

def begin_completed_candidate_repair(database,base_run_id,source_revisions,contracts,*,request_id,allow_video=False):
    """Fence old ownership, record proof/history, then grant candidate repair.

    Requires the production execution lock. Checks are repeated in the grant
    transaction, so a stale plan cannot adopt changed or in-flight sources.
    Does not run inference, change the old terminal status, or rewrite results.
    """
    database=Path(database)
    if not request_id or not source_revisions:raise ContractError('candidate_repair_scope_missing')
    scope=dict(sorted(source_revisions.items()))
    with ProductExecutionLease(database,base_run_id):
        with connect(database,readonly=True) as c:
            base=c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(base_run_id,)).fetchone()
            if not base or base['status']!='COMPLETED' or base['scheduler_mode']!=EVENT_DRIVEN_V2:
                raise ContractError('candidate_repair_base_not_completed_v2')
            base=dict(base)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_candidate_repair_runs'").fetchone():
                previous=c.execute('SELECT * FROM v2_candidate_repair_runs WHERE base_run_id=? AND request_id=?',(base_run_id,request_id)).fetchone()
                if previous:
                    if json.loads(previous['scope_json'])!=scope:raise ContractError('candidate_repair_request_scope_changed')
                    return dict(c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(previous['repair_run_id'],)).fetchone())
            proof=_proof(c,base,scope,contracts,allow_video=allow_video)
        run=start_scheduler_run(database,base['task_id'],scheduler_mode=EVENT_DRIVEN_V2,
            request_id='candidate-repair:'+request_id,source_processing_revision='candidate_metadata_repair_v1')
        try:
            with connect(database) as c:
                c.execute('BEGIN IMMEDIATE')
                current=c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(run['scheduler_run_id'],)).fetchone()
                if not current or current['status']!='RUNNING' or current['execution_epoch']!=run['execution_epoch']:
                    raise SchedulerOwnershipError('candidate_repair_owner_lost')
                if _proof(c,base,scope,contracts,allow_video=allow_video)!=proof:raise ContractError('candidate_repair_evidence_changed')
                c.execute('''CREATE TABLE IF NOT EXISTS v2_candidate_repair_runs(
                    repair_run_id TEXT PRIMARY KEY,base_run_id TEXT,request_id TEXT,
                    scope_json TEXT,proof_json TEXT,UNIQUE(base_run_id,request_id))''')
                c.execute('''CREATE TABLE IF NOT EXISTS v2_candidate_repair_source_history(
                    repair_run_id TEXT,source_id TEXT,source_revision TEXT,
                    previous_run_id TEXT,previous_epoch INTEGER,
                    PRIMARY KEY(repair_run_id,source_id,source_revision))''')
                c.execute('INSERT INTO v2_candidate_repair_runs VALUES(?,?,?,?,?)',
                    (run['scheduler_run_id'],base_run_id,request_id,json.dumps(scope,sort_keys=True),json.dumps(proof,sort_keys=True)))
                for sid,revision in scope.items():
                    c.execute('INSERT INTO v2_candidate_repair_source_history VALUES(?,?,?,?,?)',
                        (run['scheduler_run_id'],sid,revision,base_run_id,base['execution_epoch']))
                    changed=c.execute('UPDATE v2_source_epochs SET scheduler_run_id=?,execution_epoch=? WHERE source_id=? AND source_revision=? AND scheduler_run_id=? AND execution_epoch=?',
                        (run['scheduler_run_id'],run['execution_epoch'],sid,revision,base_run_id,base['execution_epoch'])).rowcount
                    if changed!=1:raise SchedulerOwnershipError('candidate_repair_source_owner_changed')
                # No v2_jobs, artifacts, attempts, results, or old run updates.
        except BaseException:
            finish_scheduler_run(database,run['scheduler_run_id'],scheduler_mode=EVENT_DRIVEN_V2,
                execution_epoch=run['execution_epoch'],status='FAILED')
            raise
        return run

def recover_failed_candidate_repair(task,run):
    """Audited retry of a pre-inference historical-preview validation failure."""
    import time,os
    if run['status']!='FAILED':return run
    state=json.loads(Path(task['state_path']).read_text())
    if (state.get('task_id')!=task['task_id'] or state.get('status')!='failed' or
        state.get('error') not in {'image_reconcile_artifact_identity_changed', "No module named 'PIL'"} or
        state.get('reason_code')!='PRODUCT_EXECUTION_FAILED'):
        raise ContractError('candidate_repair_recovery_reason_unproven')
    database=Path(task['database']);rid=run['scheduler_run_id']
    with ProductExecutionLease(database,rid),connect(database) as c:
        c.execute('BEGIN IMMEDIATE')
        current=dict(c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?',(rid,)).fetchone())
        if current['status']!='FAILED' or current['execution_epoch']!=task['execution_epoch'] or current['task_id']!=task['task_id']:
            raise ContractError('candidate_repair_recovery_owner_changed')
        if abs(float(state.get('finished_at_epoch') or 0)-float(current['finished_at'] or 0))>5:
            raise ContractError('candidate_repair_recovery_failure_timestamp_changed')
        record=c.execute('SELECT * FROM v2_candidate_repair_runs WHERE repair_run_id=?',(rid,)).fetchone()
        if not record or json.loads(record['scope_json'])!=task['candidate_repair']['scope']:
            raise ContractError('candidate_repair_recovery_scope_changed')
        if c.execute('SELECT status FROM scheduler_runs WHERE scheduler_run_id=?',(record['base_run_id'],)).fetchone()[0]!='COMPLETED':
            raise ContractError('candidate_repair_recovery_base_changed')
        life=c.execute('SELECT worker_pid FROM v2_product_lifecycle WHERE scheduler_run_id=?',(rid,)).fetchone()
        pid=int(life[0] or 0) if life else 0
        if pid>1:
            try:os.kill(pid,0)
            except ProcessLookupError:pass
            else:raise ContractError('candidate_repair_recovery_worker_alive')
        proof=_proof(c,current,json.loads(record['scope_json']),task['v2_contracts'],required_status='FAILED')
        if proof!=json.loads(record['proof_json']):raise ContractError('candidate_repair_recovery_evidence_changed')
        if c.execute("SELECT 1 FROM v2_jobs WHERE scheduler_run_id=? AND stage='qwen' AND attempts>0",(rid,)).fetchone():
            raise ContractError('candidate_repair_recovery_not_pre_inference')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():
            raise ContractError('candidate_repair_recovery_database_invalid')
        c.execute('CREATE TABLE IF NOT EXISTS v2_candidate_repair_recoveries(recovery_id TEXT PRIMARY KEY,repair_run_id TEXT,evidence_json TEXT,recovered_at REAL)')
        evidence={'failure_state':state,'previous_run':current,'proof':proof}
        c.execute('INSERT INTO v2_candidate_repair_recoveries VALUES(?,?,?,?)',
            (fingerprint((rid,current['finished_at'])),rid,json.dumps(evidence,sort_keys=True),time.time()))
        c.execute("UPDATE scheduler_runs SET status='RECOVERABLE',recovered_at=?,finished_at=NULL WHERE scheduler_run_id=? AND status='FAILED'",(time.time(),rid))
    return {**run,'status':'RECOVERABLE'}

def _proof(c,base,scope,contracts,*,required_status='COMPLETED',allow_video=False):
    # Use the existing production-equivalent lineage adapter, not the legacy
    # full-DAG view for each individual evidence proof.
    from .v2_current_jobs_cache import install_readonly_view
    install_readonly_view(c)
    if c.execute('SELECT status FROM scheduler_runs WHERE scheduler_run_id=?',(base['scheduler_run_id'],)).fetchone()[0]!=required_status:
        raise ContractError('candidate_repair_base_changed')
    if c.execute("SELECT 1 FROM v2_jobs WHERE status='RUNNING'").fetchone():raise ContractError('candidate_repair_worker_active')
    if c.execute("SELECT 1 FROM scheduler_admissions WHERE status='ACTIVE'").fetchone():raise ContractError('candidate_repair_admission_active')
    proof={}
    for sid,revision in scope.items():
        row=c.execute('''SELECT s.eos,s.final_sequence,e.scheduler_run_id,e.execution_epoch,m.metadata
            FROM v2_active_sources a JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
            JOIN v2_source_epochs e ON e.source_id=s.source_id AND e.source_revision=s.revision
            JOIN v2_source_metadata m ON m.source_id=s.source_id AND m.source_revision=s.revision
            WHERE a.source_id=? AND a.source_revision=?''',(sid,revision)).fetchone()
        if not row or row['eos']!='EOS' or row['scheduler_run_id']!=base['scheduler_run_id'] or row['execution_epoch']!=base['execution_epoch']:
            raise ContractError('candidate_repair_source_not_current_sealed')
        metadata=json.loads(row['metadata'])
        media=metadata.get('media_type',metadata.get('parent_media_kind'))
        if media not in ({'image','video'} if allow_video else {'image'}):raise ContractError('candidate_repair_not_image')
        artifacts=list(c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(sid,revision)))
        if not artifacts or [a['slot'] for a in artifacts]!=list(range(row['final_sequence']+1)) or any(a['status']!='READY' for a in artifacts):
            raise ContractError('candidate_repair_artifacts_incomplete')
        entries=[]
        for artifact in artifacts:
            for stage in REQUIRED:
                job=c.execute('SELECT * FROM v2_current_jobs WHERE stage=? AND item_id=? ORDER BY revision DESC LIMIT 1',(stage,artifact['artifact_id'])).fetchone()
                if not job or job['status']!='DONE' or job['revision']!=contracts[stage]['revision'] or job['input_sha']!=artifact['sha']:
                    raise ContractError('candidate_repair_required_evidence_missing:'+stage)
                if fingerprint(json.loads(job['result_json']))!=job['result_sha']:raise ContractError('candidate_repair_evidence_corrupt')
                entries.append((job['execution_key'],job['result_sha'],artifact['sha']))
        proof[sid]={'source_revision':revision,'evidence_digest':fingerprint(entries)}
    return proof
