"""Recover redundant remux publication headers using existing durable truth.

No model/FFmpeg calls, attempt resets, fabricated SUCCESS or result copies.
Only a proven duplicate of the same source/revision/input/contract is retired.
"""
import json,time,hashlib
from pathlib import Path
from .video_commit_queue import SQLCommand,WriteProposal
from .v2_model_workers import sha_file
from .v2_producer import fingerprint
from .v2_runtime import ContractError


def owned_audio_path(ledger_path,value_path):
    """Resolve an attested project-copy path without changing result identity."""
    path=Path(value_path).resolve();root=Path(ledger_path).resolve().parent
    if not path.is_relative_to(root):
        task_file=root.parent.parent/'task.json'
        task=json.loads(task_file.read_text()) if task_file.is_file() else {}
        if Path(task.get('database','/__missing__')).resolve()!=Path(ledger_path).resolve():raise ContractError('audio_adoption_workspace_unproven')
        matches=[]
        for old,new in task.get('path_relocations',{}).items():
            old_root=Path(old).resolve()/'workspace/runtime'
            if Path(new).resolve()==task_file.parent.resolve() and path.is_relative_to(old_root):matches.append(root/path.relative_to(old_root))
        if len(matches)!=1:raise ContractError('audio_adoption_workspace_unproven')
        path=matches[0].resolve()
    if not path.is_relative_to(root):raise ContractError('audio_adoption_workspace_unproven')
    return path


def recover(adapter, source_id=None, revision=None):
    if (source_id is None)!=(revision is None):raise ValueError("audio_recovery_scope_incomplete")
    ledger=adapter.ledger
    scope=" AND n.source_id=? AND n.revision=?" if source_id is not None else ""
    args=(source_id,revision) if source_id is not None else ()
    with adapter.store.connect() as c:
        duplicates=[dict(r) for r in c.execute("SELECT n.* FROM video_stream_jobs_v1 n WHERE n.stage='audio_extract' AND (json_extract(n.payload,'$.status')='READY' OR (json_extract(n.payload,'$.status') IS NULL AND json_extract(n.payload,'$.source_sha')=n.input_sha)) AND n.status IN ('PENDING','RETRY_PENDING','TERMINAL_FAILED') AND EXISTS(SELECT 1 FROM video_stream_jobs_v1 d WHERE d.source_id=n.source_id AND d.revision=n.revision AND d.stage=n.stage AND d.input_sha=n.input_sha AND d.contract=n.contract AND d.status='DONE' AND json_extract(d.payload,'$.status')='READY')"+scope,args)]
    for duplicate in duplicates:
        source,revision=duplicate['source_id'],duplicate['revision']
        with adapter.store.connect() as c:
            c.execute('BEGIN')
            row=c.execute("SELECT d.*,m.execution_key FROM video_stream_jobs_v1 d JOIN video_stream_mirrors_v1 m ON m.job_id=d.job_id AND m.stage=d.stage JOIN v2_jobs j ON j.execution_key=m.execution_key AND j.status='DONE' WHERE d.source_id=? AND d.revision=? AND d.stage='audio_extract' AND d.input_sha=? AND d.contract=? AND d.status='DONE' AND json_extract(d.payload,'$.status')='READY' ORDER BY d.rowid LIMIT 1",(source,revision,duplicate['input_sha'],duplicate['contract'])).fetchone()
            if not row:raise ContractError('audio_adoption_requires_current_success')
            success=dict(row);authority=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(success['execution_key'],)).fetchone()
            value={k:v for k,v in json.loads(success['payload']).items() if not k.startswith('_')}
            other=json.loads(duplicate['payload'])
            original=c.execute('SELECT source_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(source,revision)).fetchone()
            if not original or json.loads(original[0])['digest']!=duplicate['input_sha']:raise ContractError('audio_adoption_source_digest_changed')
            legacy_header=(other.get('status') is None and other.get('source_sha')==duplicate['input_sha']
                and other.get('path')==json.loads(original[0])['path']
                and other.get('_evidence_key')==authority['execution_key'])
            if value.get('status')!='READY' or (not legacy_header and any(value.get(k)!=other.get(k) for k in ('status','codec','sample_rate','channels','representation'))):raise ContractError('audio_adoption_semantics_changed')
            native_sha=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            if duplicate['result_sha'] is not None or fingerprint(value)!=authority['result_sha'] or native_sha!=success['result_sha']:raise ContractError('audio_adoption_result_proof_changed')
            if authority['source_id']!=source or authority['source_revision']!=revision or authority['input_sha']!=duplicate['input_sha'] or json.loads(authority['provenance'])!=json.loads(duplicate['contract']):raise ContractError('audio_adoption_lineage_changed')
            path=owned_audio_path(ledger.path,value['path'])
            if not path.is_file() or sha_file(path)!=value['sha']:raise ContractError('audio_adoption_artifact_changed')
        checks=(
            (SQLCommand('SELECT owner,epoch,stopped FROM video_stream_control_v1 WHERE singleton=1'),(ledger.owner,ledger.epoch,0)),
            (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)),(revision,)),
            (SQLCommand('SELECT status,attempts,result_sha FROM video_stream_jobs_v1 WHERE job_id=?',(duplicate['job_id'],)),(duplicate['status'],duplicate['attempts'],None)),
            (SQLCommand('SELECT status,result_sha FROM video_stream_jobs_v1 WHERE job_id=?',(success['job_id'],)),('DONE',success['result_sha'])),
            (SQLCommand('SELECT status,result_sha,input_sha FROM v2_jobs WHERE execution_key=?',(success['execution_key'],)),('DONE',authority['result_sha'],duplicate['input_sha'])))
        commands=(
            SQLCommand("UPDATE video_stream_jobs_v1 SET status='SUPERSEDED',token=NULL,lease_until=NULL WHERE job_id=?",(duplicate['job_id'],)),
            SQLCommand('INSERT OR IGNORE INTO video_stream_mirrors_v1 VALUES(?,?,?)',(duplicate['job_id'],'audio_extract',success['execution_key'])),
            SQLCommand('UPDATE video_stream_inputs_v1 SET audio_json=? WHERE source_id=? AND revision=?',(json.dumps(value),source,revision)),
            SQLCommand("DELETE FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SOURCE_FAILED_FINAL','SOURCE_BRANCH_FAILED') AND json_extract(payload,'$.reason') IN ('required_branch_terminal_failure','required_mixed_stage_failed') AND NOT EXISTS(SELECT 1 FROM video_stream_jobs_v1 j WHERE j.source_id=? AND j.revision=? AND j.status='TERMINAL_FAILED')",(source,revision,source,revision)))
        ledger.commit_queue.submit(WriteProposal('adopt_existing_audio_publication',source,commands,checks)).result()
        with Path(ledger.path).with_name('audio-publication-recovery.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'source_id':source,'revision':revision,'redundant_header':duplicate['job_id'],'adopted_success':success['job_id'],'canonical_execution_key':success['execution_key'],'result_sha':success['result_sha'],'reason':'legacy_source_header_covered_by_verified_ready_result' if legacy_header else 'same_source_digest_contract_native_audio_remux','attempts_preserved':duplicate['attempts']})+'\n')
