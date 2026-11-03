"""Atomic transitive revocation, batched by the same complete key set.

History/result bodies stay intact. Source generation remains monotonic; one
source closure is invalidated once per transaction, rather than once per child.
"""
import json


def _delete_present(c,table,predicate,parameters):
    # SQLite has row triggers, not statement triggers: an empty DELETE has no
    # durable effect. Only omit it inside the caller's existing transaction;
    # detached preparation retains the same source/revision/epoch fences.
    if getattr(c,'in_transaction',False) and not c.execute(
            'SELECT 1 FROM '+table+' WHERE '+predicate+' LIMIT 1',parameters).fetchone():
        return
    c.execute('DELETE FROM '+table+' WHERE '+predicate,parameters)

def invalidate(c,keys):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pending=set(keys);revoked=set()
    while pending-revoked:
        batch=pending-revoked;revoked.update(batch);encoded=json.dumps(sorted(batch))
        pending.update(r[0] for r in c.execute('SELECT child_key FROM v2_job_dependencies WHERE parent_key IN (SELECT value FROM json_each(?))',(encoded,)))
        if 'v2_snapshot_evidence' in tables:
            snapshots=sorted({r[0] for r in c.execute('SELECT snapshot_id FROM v2_snapshot_evidence WHERE execution_key IN (SELECT value FROM json_each(?))',(encoded,))})
            if snapshots:
                encoded_snapshots=json.dumps(snapshots)
                c.execute("UPDATE v2_selection_snapshots SET status='SUPERSEDED' WHERE snapshot_id IN (SELECT value FROM json_each(?)) AND status IS NOT 'SUPERSEDED'",(encoded_snapshots,))
                c.execute("UPDATE v2_selected_candidates SET status='SUPERSEDED' WHERE snapshot_id IN (SELECT value FROM json_each(?)) AND status IS NOT 'SUPERSEDED'",(encoded_snapshots,))
                pending.update(r[0] for r in c.execute('SELECT execution_key FROM v2_jobs WHERE item_id IN (SELECT candidate_id FROM v2_selected_candidates WHERE snapshot_id IN (SELECT value FROM json_each(?)))',(encoded_snapshots,)))
    jobs=list(c.execute('SELECT * FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',(json.dumps(sorted(revoked)),)))
    if not jobs:return revoked
    actual_keys={j['execution_key'] for j in jobs}
    keys_json=json.dumps(sorted(actual_keys))
    doc_rows=list(c.execute('SELECT document_id,status FROM v2_documents WHERE raw_result_ref IN (SELECT value FROM json_each(?))',(keys_json,))) if 'v2_documents' in tables else []
    docs=[r[0] for r in doc_rows]
    changed_keys=[j["execution_key"] for j in jobs if j["status"]!="SUPERSEDED" or j["token"] is not None]
    if changed_keys:
        c.execute("UPDATE v2_jobs SET status='SUPERSEDED',token=NULL WHERE execution_key IN (SELECT value FROM json_each(?)) AND (status IS NOT 'SUPERSEDED' OR token IS NOT NULL)",(json.dumps(sorted(changed_keys)),))
    sources={(j['source_id'],j['source_revision']) for j in jobs}
    if 'v2_source_closure' in tables:
        for source,revision in sorted(sources):
            row=c.execute("SELECT status FROM v2_source_closure WHERE source_id=? AND source_revision=?",(source,revision)).fetchone()
            if row and row[0]!="STALE":c.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=? AND status IS NOT 'STALE'",(source,revision))
    if 'v2_visual_source_closure' in tables:
        visual=set()
        for j in jobs:
            if j['stage'] in ('audio_extract','vad','whisper'):continue
            if j['stage']=='embedding':
                payload=json.loads(j['payload']) if isinstance(j['payload'],str) else j['payload'] or {}
                if payload.get('document_type')=='whisper':continue
            visual.add((j['source_id'],j['source_revision']))
        for source,revision in sorted(visual):
            row=c.execute("SELECT status FROM v2_visual_source_closure WHERE source_id=? AND source_revision=?",(source,revision)).fetchone()
            if row and row[0]!="STALE":c.execute("UPDATE v2_visual_source_closure SET status='STALE' WHERE source_id=? AND source_revision=? AND status IS NOT 'STALE'",(source,revision))
    vectors=set(docs)|{j['item_id'] for j in jobs if j['stage'] in ('openclip','embedding')}
    if 'v2_vectors' in tables and vectors:_delete_present(c,'v2_vectors','entity_id IN (SELECT value FROM json_each(?))',(json.dumps(sorted(vectors)),))
    if 'v2_documents' in tables:
        changed_docs=[row[0] for row in doc_rows if row[1]!="SUPERSEDED"]
        if changed_docs:c.execute("UPDATE v2_documents SET status='SUPERSEDED' WHERE document_id IN (SELECT value FROM json_each(?)) AND status IS NOT 'SUPERSEDED'",(json.dumps(changed_docs),))
        if docs:
            encoded_docs=json.dumps(sorted(docs))
            if 'v2_text_fts' in tables:_delete_present(c,'v2_text_fts','document_id IN (SELECT value FROM json_each(?))',(encoded_docs,))
            if 'v2_index_coverage' in tables:_delete_present(c,'v2_index_coverage','entity_id IN (SELECT value FROM json_each(?))',(encoded_docs,))
        embeddings={j['item_id'] for j in jobs if j['stage']=='embedding'}
        if embeddings and 'v2_index_coverage' in tables:_delete_present(c,'v2_index_coverage',"index_kind='vector' AND entity_id IN (SELECT value FROM json_each(?))",(json.dumps(sorted(embeddings)),))
    if 'v2_fusion' in tables:_delete_present(c,'v2_fusion','result_ref IN (SELECT value FROM json_each(?))',(keys_json,))
    if 'v2_observation_heads' in tables:
        observations=set()
        for source,revision in sources:
            for row in c.execute('SELECT observation_id,provenance FROM v2_person_observations WHERE source_id=? AND source_revision=?',(source,revision)):
                p=json.loads(row['provenance'])
                if p.get('yolo_execution_key') in actual_keys or p.get('face_execution_key') in actual_keys:observations.add(row['observation_id'])
        if observations:_delete_present(c,'v2_observation_heads','observation_id IN (SELECT value FROM json_each(?))',(json.dumps(sorted(observations)),))
    return revoked
