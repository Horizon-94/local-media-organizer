"""Pinned proof is an actual V2 lineage node, not an alternate truth graph."""
import json
from .video_commit_queue import SQLCommand,WriteProposal,StaleProposal
from .video_canonical_store import prepare_result
from .video_result_reader import reference
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .v2_index_lineage import current_input_jobs


def ensure(adapter,snapshot,source,revision):
    with adapter.store.connect() as c:
        c.execute('BEGIN')
        row=c.execute('SELECT evidence_digest,policy_hash,status,input_json FROM v2_selection_snapshots WHERE snapshot_id=?',(snapshot,)).fetchone()
        if not row or row[2]!='DONE':raise ContractError('selection_proof_snapshot_not_current')
        parents=sorted(r[0] for r in c.execute('SELECT execution_key FROM v2_snapshot_evidence WHERE snapshot_id=?',(snapshot,)))
        current,_=current_input_jobs(c,parents,[])
        if set(current)!=set(parents):raise ContractError('selection_proof_evidence_not_current')
        evidence=[(key,current[key]['result_sha']) for key in parents]
        pinned=sorted(tuple(item) for item in json.loads(row[3]).get('evidence_keys',[]))
        if pinned!=evidence:raise ContractError('selection_proof_snapshot_evidence_changed')
        body={'snapshot_id':snapshot,'manifest_sha':row[0],'policy_version':row[1],
              'evidence_set_sha':fingerprint(evidence),'evidence_count':len(evidence),'evidence_refs':evidence}
        contract={'revision':1,'kind':'video-pinned-selection-proof-v1','policy':row[1]}
        key=fingerprint(('selection_proof',snapshot,1,fingerprint(body),contract,{},parents))
        old=c.execute('SELECT status,result_sha FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
        if old:
            if old[0]!='DONE' or old[1]!=fingerprint(body):raise ContractError('selection_proof_changed')
            return key
    canonical=prepare_result('selection_proof','v1',body,source=source)
    checks=(
        (SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(adapter.ledger.owner,adapter.ledger.epoch)),
        (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)),(revision,)),
        (SQLCommand('SELECT evidence_digest,policy_hash,status FROM v2_selection_snapshots WHERE snapshot_id=?',(snapshot,)),tuple(row[:3])),
        (SQLCommand("SELECT count(*) FROM json_each(?) refs JOIN v2_jobs j ON j.execution_key=json_extract(refs.value,'$[0]') JOIN v2_active_sources a ON a.source_id=j.source_id AND a.source_revision=j.source_revision WHERE j.status='DONE' AND j.result_sha=json_extract(refs.value,'$[1]')",(json.dumps(evidence),)),(len(evidence),)))
    commands=list(canonical.proposal.commands)
    commands.append(SQLCommand("INSERT OR IGNORE INTO v2_jobs(execution_key,stage,item_id,revision,source_id,source_revision,input_sha,provenance,payload,status,attempts,max_attempts,result_json,result_sha) VALUES(?,'selection_proof',?,1,?,?,?,?,?,'DONE',0,1,?,?)",
        (key,snapshot,source,revision,fingerprint(body),json.dumps(contract,sort_keys=True),'{}',json.dumps(reference(canonical.result_id)),fingerprint(body))))
    commands.extend(SQLCommand('INSERT OR IGNORE INTO v2_job_dependencies VALUES(?,?)',(key,parent)) for parent in parents)
    commands.append(SQLCommand('INSERT OR IGNORE INTO video_stream_selection_proofs_v1 VALUES(?,?,?,?)',(snapshot,key,source,revision)))
    try:adapter.ledger.commit_queue.submit(WriteProposal('pinned_selection_proof',source,tuple(commands),checks)).result()
    except StaleProposal as error:raise ContractError('selection_proof_changed_retry') from error
    return key


def parent(store,c,event):
    row=c.execute('SELECT proof_key FROM video_stream_selection_proofs_v1 WHERE snapshot_id=? AND source_id=? AND source_revision=?',
        (event['payload']['snapshot_id'],event['source_id'],event['source_revision'])).fetchone()
    if not row:raise ContractError('pinned_selection_proof_missing')
    proof=c.execute('SELECT status,result_json,result_sha FROM v2_jobs WHERE execution_key=?',(row[0],)).fetchone()
    if not proof or proof[0]!='DONE':raise ContractError('selection_proof_evidence_not_current')
    body=json.loads(proof[1])
    if fingerprint(body)!=proof[2]:raise ContractError('selection_proof_digest_mismatch')
    evidence=body['evidence_refs']
    current,_=current_input_jobs(c,[item[0] for item in evidence],[])
    if any(item[0] not in current or current[item[0]]['result_sha']!=item[1] for item in evidence):
        raise ContractError('selection_proof_evidence_not_current')
    return [row[0]]
