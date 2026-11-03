"""Read-only, source-scoped historical success verification.

This module never imports model handlers or claims work. It cannot declare
SOURCE_COMPLETE: that requires the new coordinator's independent local proof.
Historical identity/hash/attempt/owner are retained verbatim in an import audit.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from .v2_producer import fingerprint
from .v2_model_workers import sha_file
from .v2_runtime import ContractError
from .video_stream_ledger import encoded


@contextmanager
def readonly(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        yield c
    finally:c.close()


def verified_source(database,source,*,check_files=True):
    with readonly(database) as c:
        active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
        if not active or active[0].startswith('aborted:'):raise ContractError('reuse_active_revision_invalid')
        rev=active[0]
        producer=c.execute('SELECT * FROM v2_sources WHERE source_id=? AND revision=?',(source,rev)).fetchone()
        metadata=c.execute('SELECT * FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,rev)).fetchone()
        if not producer or producer['eos']!='EOS' or not metadata:raise ContractError('reuse_current_eos_missing')
        artifacts=[dict(r) for r in c.execute("SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot",(source,rev))]
        jobs=[dict(r) for r in c.execute("SELECT * FROM v2_jobs WHERE source_id=? AND source_revision=? AND status='DONE' ORDER BY execution_key",(source,rev))]
        keys={r['execution_key'] for r in jobs}
        edges=[tuple(r) for r in c.execute('SELECT d.child_key,d.parent_key FROM v2_job_dependencies d JOIN v2_jobs j ON j.execution_key=d.child_key WHERE j.source_id=? AND j.source_revision=? AND j.status=\'DONE\'',(source,rev))]
        if any(parent not in keys for _,parent in edges):raise ContractError('reuse_external_or_incomplete_dependency')
        for job in jobs:
            if not job['result_json'] or fingerprint(json.loads(job['result_json']))!=job['result_sha']:
                raise ContractError('reuse_result_hash_invalid:'+job['execution_key'])
            if not job['input_sha'] or not json.loads(job['provenance']):raise ContractError('reuse_provenance_missing')
        if len(artifacts)!=producer['final_sequence']+1:raise ContractError('reuse_artifact_manifest_incomplete')
        if check_files:
            stat=Path(metadata['path']).stat();m=json.loads(metadata['metadata'])
            if stat.st_size!=m['source_file_size_bytes'] or stat.st_mtime_ns!=m['source_file_mtime_ns']:
                raise ContractError('reuse_source_stat_changed')
            for artifact in artifacts:
                if sha_file(artifact['path'])!=artifact['sha']:raise ContractError('reuse_artifact_hash_invalid')
            # Check derived audio, never decode or rescan the original video.
            for job in jobs:
                if job['stage']=='audio_extract':
                    value=json.loads(job['result_json'])
                    if value.get('status')=='READY' and sha_file(value['path'])!=value['sha']:
                        raise ContractError('reuse_audio_hash_invalid')
        return dict(source_id=source,revision=rev,metadata=dict(metadata),producer=dict(producer),
            artifacts=artifacts,jobs=jobs,dependencies=edges,models_called=0,
            historical_success_digest=fingerprint([(j['execution_key'],j['result_sha'],j['attempts'],j['status']) for j in jobs]))


def eligible_unprocessed(database,limit=24):
    """No DONE of any stage, including alternate source IDs for the same file."""
    with readonly(database) as c:
        return [dict(r) for r in c.execute('''SELECT p.source_id,p.source_json FROM v2_product_sources p
            WHERE p.state='NEW' AND json_extract(p.source_json,'$.media_kind')='video'
            AND NOT EXISTS(SELECT 1 FROM v2_jobs j WHERE j.source_id=p.source_id AND j.status='DONE')
            AND NOT EXISTS(SELECT 1 FROM v2_source_metadata m JOIN v2_jobs j ON j.source_id=m.source_id
                WHERE j.status='DONE' AND m.path=json_extract(p.source_json,'$.source_path'))
            ORDER BY p.rowid LIMIT ?''',(limit,))]


def import_verified(runtime,database,source):
    """Import a current EOS snapshot into an EMPTY isolated source namespace.

    Historical DONE rows remain byte-for-byte identical. No epoch/owner is
    relabeled and no new inference attempt is fabricated. New queue entries
    record IMPORTED_SUCCESS, separate from native attempt history/service time.
    Closure is intentionally not imported: it must be reproved locally.
    """
    if Path(database).resolve()==runtime.db.resolve():raise ContractError('reuse_destination_is_production')
    pack=verified_source(database,source)
    rev=pack['revision']
    from .v2_people import PersonIdentity
    PersonIdentity(runtime.adapters.store)
    scoped={
        'v2_sources':'revision','v2_source_metadata':'source_revision',
        'v2_active_sources':'source_revision','v2_artifacts':'revision',
        'v2_sample_slots':'source_revision','v2_source_epochs':'source_revision',
        'v2_selection_contexts':'source_revision','v2_canonical':'source_revision',
        'v2_selection_snapshots':'source_revision','v2_selected_candidates':'source_revision',
        'v2_documents':'source_revision','v2_audio_branches':'source_revision',
        'v2_final_projection_heads':'source_revision',
        'v2_person_observations':'source_revision','v2_tracklets':'source_revision',
        'v2_source_people':'source_revision','v2_source_face_anchors':'source_revision'}
    related={
        'v2_artifact_mappings':('artifact_id','v2_artifacts','artifact_id','revision'),
        'v2_snapshot_evidence':('snapshot_id','v2_selection_snapshots','snapshot_id','source_revision'),
        'v2_observation_heads':('observation_id','v2_person_observations','observation_id','source_revision'),
        'v2_tracklet_membership':('observation_id','v2_person_observations','observation_id','source_revision'),
        'v2_person_review_observations':('observation_id','v2_person_observations','observation_id','source_revision'),
        'v2_tracklet_people':('tracklet_id','v2_tracklets','tracklet_id','source_revision')}
    from .video_stream_people import MANUAL
    batches={}
    with readonly(database) as c:
        # Re-read in ONE consistent snapshot, and reject a changed authority.
        current=[tuple(r) for r in c.execute("SELECT execution_key,result_sha,attempts,status FROM v2_jobs WHERE source_id=? AND source_revision=? AND status='DONE' ORDER BY execution_key",(source,rev))]
        if fingerprint(current)!=pack['historical_success_digest'] or c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()[0]!=rev:
            raise ContractError('reuse_authority_changed')
        names={r[0]:r[1] for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        for name,column in scoped.items():
            if name in names:batches[name]=(names[name],[dict(r) for r in c.execute(f'SELECT * FROM {name} WHERE source_id=? AND {column}=?',(source,rev))])
        for name,(field,parent,key,column) in related.items():
            if name in names:batches[name]=(names[name],[dict(r) for r in c.execute(f'SELECT * FROM {name} WHERE {field} IN (SELECT {key} FROM {parent} WHERE source_id=? AND {column}=?)',(source,rev))])
        for name in MANUAL:
            if name in names:batches[name]=(names[name],[dict(r) for r in c.execute('SELECT * FROM '+name)])
        name='v2_final_projection_documents'
        if name in names:batches[name]=(names[name],[dict(r) for r in c.execute('SELECT * FROM v2_final_projection_documents WHERE execution_key IN (SELECT execution_key FROM v2_jobs WHERE source_id=? AND source_revision=? AND status=\'DONE\')',(source,rev))])
    with runtime.ledger.connection() as c:
        c.execute('BEGIN IMMEDIATE');runtime.ledger.fence(c)
        if c.execute('SELECT 1 FROM video_stream_sources_v1 WHERE source_id=?',(source,)).fetchone():raise ContractError('reuse_requires_empty_source_namespace')
        c.execute('CREATE TABLE IF NOT EXISTS video_stream_imports_v1(execution_key TEXT PRIMARY KEY,source_database TEXT,historical_row TEXT,import_digest TEXT)')
        for name,(schema,rows) in batches.items():
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(name,)).fetchone():c.execute(schema)
            for row in rows:
                fields=list(row)
                c.execute(f'INSERT INTO {name} ({",".join(fields)}) VALUES({",".join("?" for _ in fields)})',list(row.values()))
        for row in pack['jobs']:
            fields=list(row)
            c.execute(f'INSERT INTO v2_jobs ({",".join(fields)}) VALUES({",".join("?" for _ in fields)})',list(row.values()))
            c.execute('INSERT INTO video_stream_imports_v1 VALUES(?,?,?,?)',(row['execution_key'],str(Path(database).resolve()),encoded(row),pack['historical_success_digest']))
        c.executemany('INSERT INTO v2_job_dependencies VALUES(?,?)',pack['dependencies'])
        c.execute('INSERT INTO video_stream_sources_v1 VALUES(?,?)',(source,rev))
        stat=Path(pack['metadata']['path']).stat()
        identity=dict(source_id=source,digest=pack['metadata']['source_sha'],path=pack['metadata']['path'],stat=[stat.st_size,stat.st_mtime_ns,stat.st_ino])
        audio=next((json.loads(j['result_json']) for j in pack['jobs'] if j['stage']=='audio_extract'),None)
        c.execute('INSERT INTO video_stream_inputs_v1 VALUES(?,?,?,?)',(source,rev,encoded(identity),encoded(audio)))
        # Import native-shaped evidence handles without claiming or invoking a
        # handler. Imported timing is UNKNOWN, not a zero-duration inference.
        for row in pack['jobs']:
            if row['stage'] in ('final_manifest','final_projection'):continue
            key='import:'+row['execution_key']
            c.execute('INSERT INTO video_stream_jobs_v1(job_id,source_id,revision,stage,input_sha,contract,payload,status,attempts,result_json,result_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (key,source,rev,row['stage'],row['input_sha'],row['provenance'],row['payload'],'DONE',row['attempts'],row['result_json'],row['result_sha']))
            c.execute('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)',(key,'IMPORTED_SUCCESS',encoded({'historical_execution_key':row['execution_key'],'historical_owner':row['owner']})))
            runtime.adapters.store.emit(c,'EVIDENCE_READY',source,rev,row['revision'],dict(stage=row['stage'],item_id=row['item_id'],execution_key=row['execution_key'],payload_sha=row['result_sha'],result=json.loads(row['result_json'])))
        snap=c.execute("SELECT snapshot_id,result_json FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? AND status='DONE'",(source,rev)).fetchone()
        if not snap:raise ContractError('reuse_frozen_selection_missing')
        for state,payload in [('EXTRACTION_DONE',{'imported':True}),('PRESELECTION_READY',{'imported':True}),('SELECTION_DONE',{'snapshot':snap[0],'imported':True})]:
            c.execute('INSERT INTO video_stream_source_states_v1 VALUES(?,?,?,?)',(source,rev,state,encoded(payload)))
        for stage in ('qwen','ocr'):
            count=c.execute('SELECT count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=?',(source,rev,stage)).fetchone()[0]
            c.execute('INSERT INTO video_stream_branches_v1 VALUES(?,?,?,?)',(source,rev,stage,count))
    runtime.sources.append((identity,rev))
    return pack
