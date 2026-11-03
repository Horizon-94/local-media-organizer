"""Audited restoration of unchanged proofs after erroneous global invalidation.

Never reruns inference, invents results or revives a changed/retired revision.
The captured pre-retry immutable hashes and current dependency graph are gates.
"""
import json
from .v2_producer import fingerprint
from .v2_runtime import ContractError


def restore_unchanged_search_proofs(store,locked,target,request_id):
    from .v2_incremental_index import IncrementalFusionIndex
    index=IncrementalFusionIndex(store)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM v2_jobs WHERE status='RUNNING'").fetchone():raise ContractError('scope_restore_worker_active')
        if c.execute("SELECT 1 FROM scheduler_admissions WHERE status='ACTIVE'").fetchone():raise ContractError('scope_restore_admission_active')
        from .v2_projection_peers import text_key
        own=c.execute("SELECT result_json FROM v2_current_jobs WHERE stage='qwen' AND source_id=? AND status='DONE'",(target,)).fetchall()
        target_texts={text_key('qwen',json.loads(r[0])) for r in own}
        for peer in c.execute("SELECT result_json FROM v2_current_jobs WHERE stage='qwen' AND source_id<>? AND status='DONE'",(target,)):
            if text_key('qwen',json.loads(peer[0])) in target_texts:raise ContractError('scope_restore_real_duplicate_dependency')
        changed={}
        for key,sha,attempts in locked:
            row=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
            if not row or row['result_sha']!=sha or row['attempts']!=attempts:raise ContractError('scope_restore_immutable_result_changed')
            if row['status']=='DONE':continue
            if row['source_id']==target:continue
            if row['status']!='SUPERSEDED' or row['stage'] not in ('final_manifest','final_projection','embedding'):
                raise ContractError('scope_restore_unexpected_change:'+key)
            if fingerprint(json.loads(row['result_json']))!=sha:raise ContractError('scope_restore_corrupt_result')
            changed[key]=dict(row)
        # Only descendants of captured, wrongly revoked DONE roots qualify.
        pending=set(changed)
        while pending:
            key=pending.pop()
            for child in c.execute('SELECT j.* FROM v2_job_dependencies d JOIN v2_jobs j ON j.execution_key=d.child_key WHERE d.parent_key=?',(key,)).fetchall():
                if child['execution_key'] in changed or child['source_id']==target:continue
                if child['status']=='SUPERSEDED' and not child['result_sha'] and not child['attempts'] and child['stage']=='embedding':
                    changed[child['execution_key']]=dict(child);pending.add(child['execution_key'])
        for key,row in changed.items():
            store.assert_source_epoch(c,row['source_id'],row['source_revision'])
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(row['source_id'],)).fetchone()
            latest=c.execute('SELECT MAX(revision) FROM v2_jobs WHERE stage=? AND item_id=?',(row['stage'],row['item_id'])).fetchone()[0]
            if not active or active[0]!=row['source_revision'] or latest!=row['revision']:raise ContractError('scope_restore_revision_changed')
            for parent in c.execute('SELECT j.execution_key,j.status FROM v2_job_dependencies d JOIN v2_jobs j ON j.execution_key=d.parent_key WHERE d.child_key=?',(key,)):
                if parent['status']!='DONE' and parent['execution_key'] not in changed:raise ContractError('scope_restore_dependency_changed')
                if parent['execution_key'] not in changed and not c.execute('SELECT 1 FROM v2_current_jobs WHERE execution_key=?',(parent['execution_key'],)).fetchone():raise ContractError('scope_restore_dependency_retired')
        c.execute('CREATE TABLE IF NOT EXISTS v2_search_scope_restorations(request_id TEXT PRIMARY KEY,target_source TEXT,proof_digest TEXT,audit_json TEXT)')
        audit={'reason':'legacy_global_projection_invalidation_on_single_qwen_retry','target_source':target,'prior_headers':[(k,r['status'],r['result_sha'],r['attempts']) for k,r in sorted(changed.items())]}
        digest=fingerprint(audit)
        existing=c.execute('SELECT proof_digest FROM v2_search_scope_restorations WHERE request_id=?',(request_id,)).fetchone()
        if existing and changed and existing[0]!=digest:raise ContractError('scope_restore_request_conflict')
        c.execute('INSERT OR IGNORE INTO v2_search_scope_restorations VALUES(?,?,?,?)',(request_id,target,digest,json.dumps(audit,sort_keys=True)))
        for key,row in changed.items():
            c.execute('UPDATE v2_jobs SET status=? WHERE execution_key=?',('DONE' if row['result_sha'] else 'PENDING',key))
        docs=c.execute("SELECT d.* FROM v2_documents d JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref WHERE d.source_id<>? AND d.status='SUPERSEDED' AND j.status='DONE'",(target,)).fetchall()
        repaired=[]
        for doc in docs:
            if doc['raw_result_ref'] not in changed and doc['embedding_ref'] not in changed:continue
            embedding=c.execute('SELECT status FROM v2_current_jobs WHERE execution_key=?',(doc['embedding_ref'],)).fetchone() if doc['embedding_ref'] else None
            status='EMBEDDED' if embedding and embedding[0]=='DONE' else 'READY'
            c.execute('UPDATE v2_documents SET status=? WHERE document_id=?',(status,doc['document_id']))
            index.index_document(c,doc['document_id'])
            repaired.append(doc['document_id'])
        for sid,revision in {(r['source_id'],r['source_revision']) for r in changed.values()}:
            store.emit(c,'UNCHANGED_SEARCH_PROOF_RESTORED',sid,revision,1,{'request_id':request_id,'proof_digest':digest,'no_model_inference':True})
        return {'restored_done':sum(bool(r['result_sha']) for r in changed.values()),'restored_pending':sum(not r['result_sha'] for r in changed.values()),'documents_repaired':len(repaired),'model_results_regenerated':0}
