"""Read-only, peer-root-scoped ancestry traversal; no shared DAG cache."""
import json
import sqlite3
from .v2_current_jobs_cache import current_keys


def peer_headers(c, stage=None):
    # All three reads share one snapshot, including when called inside an
    # existing evidence transaction. TEMP savepoints need no main writer lock.
    raw=lambda sql,args=():sqlite3.Connection.execute(c,sql,args)
    raw('SAVEPOINT peer_header_snapshot')
    try:
        condition='stage=?' if stage else "stage IN ('qwen','ocr')"
        roots=[dict(r) for r in c.execute('SELECT execution_key,stage,item_id,source_id,source_revision,result_sha '
            "FROM v2_jobs WHERE status='DONE' AND "+condition+' ORDER BY execution_key',(stage,) if stage else ())]
        if not roots:return []
        rows=c.execute('''WITH RECURSIVE needed(key) AS (
            SELECT value FROM json_each(?) UNION
            SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.key)
            SELECT j.execution_key,j.status,j.source_id,j.source_revision,d.parent_key,
                   a.source_revision AS active_revision
            FROM needed n JOIN v2_jobs j ON j.execution_key=n.key
            LEFT JOIN v2_job_dependencies d ON d.child_key=j.execution_key
            LEFT JOIN v2_active_sources a ON a.source_id=j.source_id
            ''',(json.dumps([r['execution_key'] for r in roots]),)).fetchall()
        jobs={};dependencies=[];active={}
        for row in rows:
            key=row['execution_key'];jobs[key]=dict(row)
            if row['parent_key'] is not None:dependencies.append((key,row['parent_key']))
            if row['active_revision'] is not None:active[row['source_id']]=row['active_revision']
        valid=current_keys(list(jobs.values()),dependencies,active)
        return [r for r in roots if r['execution_key'] in valid]
    finally:
        raw('RELEASE peer_header_snapshot')
