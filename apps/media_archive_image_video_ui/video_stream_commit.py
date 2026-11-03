"""Native model completion: read/encode first, SQL-only fenced durable ACK.

Policy coordination is deliberately not smuggled into this writer. Its
separate snapshot/proposal conversion is required before release acceptance.
"""
import json
import time
from .video_commit_queue import SQLCommand, WriteProposal
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .video_canonical_store import prepare_result
from .video_result_reader import reference


def prepare(adapter,job,payload,digest,service_ms,parts):
    ledger=adapter.ledger
    source,revision=job['source_id'],job['revision']
    commands=[]
    checks=[
        (SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(ledger.owner,ledger.epoch)),
        (SQLCommand('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(source,)),(revision,)),
        (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)),(revision,)),
        (SQLCommand('SELECT source_id,revision,stage,input_sha,contract FROM video_stream_jobs_v1 WHERE job_id=?',(job['job_id'],)),
         (source,revision,job['stage'],job['input_sha'],job['contract'])),
        (SQLCommand("SELECT count(*) FROM video_stream_jobs_v1 WHERE job_id=? AND status='RUNNING' AND token=? AND attempts=? AND lease_until>(julianday('now')-2440587.5)*86400.0",
                    (job['job_id'],job['token'],job['attempts'])),(1,))]
    item_payload={k:v for k,v in job['payload'].items() if not k.startswith('_')}
    with ledger.connection() as reader:
        reader.execute('BEGIN')
        ledger.fence(reader,allow_stop=True)
        active=reader.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
        if active is None or active[0]!=revision:
            from .video_stream_ledger import StaleResult
            raise StaleResult('canonical_evidence_no_longer_current')
        policy=reader.execute('SELECT policy_hash FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
        if not policy:raise ContractError('missing_current_source')
        checks.append((SQLCommand('SELECT policy_hash FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)),(policy[0],)))
        for stage,value_json,value_sha in parts:
            contract=adapter.contracts[stage]
            canonical=prepare_result(stage,str(contract['revision']),json.loads(value_json),
                source=source,model_version=str(contract.get('model','')))
            commands.extend(canonical.proposal.commands)
            stored_json=json.dumps(reference(canonical.result_id),sort_keys=True)
            contract_json=json.dumps(contract,sort_keys=True)
            if stage in ('dhash','geometry','quality','yoloe','openclip','insightface'):
                item=item_payload.get('frame_id')
            elif stage in ('qwen','ocr'):item=item_payload.get('candidate_id')
            elif stage=='embedding':item=item_payload.get('document_id')
            elif stage=='whisper':item=item_payload.get('segment_id')
            else:item=source+':'+revision
            key=job['payload'].get('_evidence_key') or fingerprint(
                (stage,item,contract['revision'],job['input_sha'],contract,item_payload,[]))
            old=reader.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
            if old is None and stage=='audio_extract' and not job['payload'].get('_evidence_key'):
                # Source extraction identity is source/revision + input digest
                # + contract. A retained READY artifact is its result, not a
                # second logical extraction. Adopt only an exact logical peer.
                peers=reader.execute('SELECT * FROM v2_jobs WHERE stage=? AND item_id=?',(stage,item)).fetchall()
                if len(peers)==1:
                    peer=peers[0]
                    if (peer['source_id']==source and peer['source_revision']==revision
                        and peer['input_sha']==job['input_sha'] and json.loads(peer['provenance'])==contract
                        and peer['status'] not in ('SUPERSEDED','RUNNING')):
                        key=peer['execution_key'];old=peer
                        checks.append((SQLCommand('SELECT execution_key FROM v2_jobs WHERE stage=? AND item_id=?',(stage,item)),(key,)))

            if old:
                if (old['stage']!=stage or old['input_sha']!=job['input_sha'] or
                    json.loads(old['provenance'])!=contract or old['source_id']!=source or
                    old['source_revision']!=revision or old['status']=='SUPERSEDED'):
                    raise ContractError('mirror_contract_mismatch')
                if old['status']=='DONE' and old['result_sha']!=value_sha:
                    raise ContractError('existing_success_would_change')
                checks.append((SQLCommand('SELECT status,attempts,result_sha,provenance,input_sha FROM v2_jobs WHERE execution_key=?',(key,)),
                               tuple(old[k] for k in ('status','attempts','result_sha','provenance','input_sha'))))
            else:
                # This completion may create its own immutable evidence, but
                # must never supersede another revision as a side effect.
                if job['payload'].get('_evidence_key'):raise ContractError('canonical_evidence_missing')
                checks.append((SQLCommand('SELECT count(*) FROM v2_jobs WHERE stage=? AND item_id=?',(stage,item)),(0,)))
                commands.append(SQLCommand("INSERT INTO v2_jobs(execution_key,stage,item_id,revision,source_id,source_revision,input_sha,provenance,payload,status,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,'PENDING',3)",
                    (key,stage,item,contract['revision'],source,revision,job['input_sha'],contract_json,json.dumps(item_payload,sort_keys=True))))
            if not old or old['status']!='DONE':
                commands.append(SQLCommand("UPDATE v2_jobs SET status='DONE',attempts=?,result_json=?,result_sha=? WHERE execution_key=?",
                    (job['attempts'],stored_json,value_sha,key)))
                if stage in ('qwen','ocr'):
                    from .video_peer_catalog import command
                    commands.append(command(key,source,revision,stage,json.loads(value_json),value_sha))
                    commands.append(SQLCommand("UPDATE v2_selected_candidates SET status='DONE' WHERE candidate_id=? AND status<>'SUPERSEDED'",(item,)))
                event_payload={'stage':stage,'item_id':item,'execution_key':key,'payload_sha':value_sha,'result':reference(canonical.result_id)}
                now=time.time()
                event={'schema_version':'pipeline_v2_streaming_semantic_gate_v1','event_type':'EVIDENCE_READY',
                       'source_id':source,'source_revision':revision,'aggregate_version':contract['revision'],
                       'producer_id':'pipeline-v2','occurred_at':now,'committed_at':now,
                       'policy_or_contract_hash':policy[0],'payload':event_payload,
                       'event_id':fingerprint(('EVIDENCE_READY',source,revision,event_payload))}
                commands.append(SQLCommand('INSERT OR IGNORE INTO v2_outbox(event_id,aggregate_id,version,event_type,payload,committed_at) VALUES(?,?,?,?,?,?)',
                    (event['event_id'],source+':'+revision,contract['revision'],'EVIDENCE_READY',json.dumps(event,sort_keys=True),now)))
            commands.append(SQLCommand('INSERT OR IGNORE INTO video_stream_mirrors_v1 VALUES(?,?,?)',(job['job_id'],stage,key)))
    commands.extend((
        SQLCommand("UPDATE video_stream_jobs_v1 SET status='DONE',result_json=NULL,result_sha=?,service_ms=?,token=NULL,lease_until=NULL WHERE job_id=?",(digest,service_ms,job['job_id'])),
        SQLCommand("UPDATE video_stream_attempts_v1 SET finished_at=?,status='SUCCESS',result_sha=? WHERE job_id=? AND attempt_number=?",(time.time(),digest,job['job_id'],job['attempts'])),
        SQLCommand('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)',(job['job_id'],'RESULT_COMMITTED',json.dumps({'result_sha':digest},sort_keys=True)))))
    return WriteProposal('native_model_result',source,tuple(commands),tuple(checks))
