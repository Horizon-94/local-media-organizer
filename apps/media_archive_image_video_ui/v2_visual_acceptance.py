"""Read-only V2 artifact coverage, independent of legacy projection tables."""
import collections
import json
import sqlite3

from .v2_current_jobs_cache import install_readonly_view
from .v2_producer import fingerprint
from .v2_runtime import validate_evidence


def openclip_coverage(c, task):
    required_tables={'scheduler_runs','v2_product_sources','v2_artifacts','v2_active_sources','v2_jobs','v2_job_dependencies'}
    tables={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    if not required_tables <= tables:
        return {'reason':'OPENCLIP_V2_AUTHORITY_MISSING','required':None,'valid':None}
    run_id,epoch=task.get('scheduler_run_id'),task.get('execution_epoch')
    owner=c.execute('SELECT task_id,scheduler_mode,execution_epoch FROM scheduler_runs WHERE scheduler_run_id=?',(run_id,)).fetchone()
    if not owner or tuple(owner)!=(task.get('task_id'),'event_driven_v2',epoch):
        return {'reason':'OPENCLIP_V2_OWNER_MISMATCH','required':None,'valid':None}
    contract=(task.get('v2_contracts') or {}).get('openclip')
    if not isinstance(contract,dict) or type(contract.get('revision')) is not int:
        return {'reason':'OPENCLIP_V2_CONTRACT_MISSING','required':None,'valid':None}
    previous_factory=c.row_factory
    c.row_factory=sqlite3.Row
    try:
        install_readonly_view(c)
        # Both the required universe and the results must describe one snapshot.
        if not c.in_transaction:c.execute('BEGIN')
        inconsistent=c.execute('''SELECT 1 FROM v2_product_sources p LEFT JOIN v2_active_sources a ON a.source_id=p.source_id
            WHERE p.scheduler_run_id=? AND p.source_revision IS NOT NULL
            AND (a.source_revision IS NULL OR a.source_revision<>p.source_revision) LIMIT 1''',(run_id,)).fetchone()
        if inconsistent:return {'reason':'OPENCLIP_V2_SOURCE_REVISION_MISMATCH','required':None,'valid':None}
        repair=bool(task.get('candidate_repair'))
        source_owner='p.scheduler_run_id IN (SELECT scheduler_run_id FROM scheduler_runs WHERE task_id=?)' if repair else 'p.scheduler_run_id=?'
        source_args=(task['task_id'] if repair else run_id,)
        artifacts=list(c.execute('''SELECT DISTINCT ar.* FROM v2_artifacts ar
            JOIN v2_active_sources a ON a.source_id=ar.source_id AND a.source_revision=ar.revision
            JOIN v2_product_sources p ON p.source_id=ar.source_id AND p.source_revision=ar.revision
            WHERE '''+source_owner+" AND ar.status='READY'",source_args))
        jobs=collections.defaultdict(list)
        job_owner=('scheduler_run_id IN (SELECT scheduler_run_id FROM scheduler_runs WHERE task_id=?) AND EXISTS (SELECT 1 FROM scheduler_runs owner WHERE owner.scheduler_run_id=j.scheduler_run_id AND owner.execution_epoch=j.execution_epoch)') if repair else 'scheduler_run_id=? AND execution_epoch=?'
        job_args=(task['task_id'],) if repair else (run_id,epoch)
        for j in c.execute("SELECT j.* FROM v2_current_jobs j WHERE stage='openclip' AND "+job_owner,job_args):
            jobs[(j['source_id'],j['source_revision'],j['item_id'],j['input_sha'])].append(j)
        provenance=json.dumps(contract,sort_keys=True)
        missing=[];duplicates=[];valid=0
        for ar in artifacts:
            accepted=[]
            for j in jobs.get((ar['source_id'],ar['revision'],ar['artifact_id'],ar['sha']),[]):
                if j['status']!='DONE' or j['revision']!=contract['revision'] or j['provenance']!=provenance:continue
                try:
                    result=json.loads(j['result_json']);validate_evidence('openclip',result)
                    if fingerprint(result)!=j['result_sha']:continue
                except (ValueError,TypeError,RuntimeError):continue
                accepted.append(j['execution_key'])
            if not accepted:missing.append(ar['artifact_id'])
            if len(accepted)>1:duplicates.append(ar['artifact_id'])
            valid+=bool(accepted)
        reason=f'OPENCLIP_COVERAGE_MISMATCH_{valid}_OF_{len(artifacts)}' if missing else ''
        if duplicates:reason=f'OPENCLIP_DUPLICATE_ACTIVE_RESULT_{len(duplicates)}'
        return {'required':len(artifacts),'valid':valid,'missing':len(missing),'duplicate_active':len(duplicates),
                'missing_artifacts':missing,'duplicate_artifacts':duplicates,'reason':reason,
                'authority':'v2_active_ready_artifact_current_result_frozen_contract'}
    finally:c.row_factory=previous_factory
