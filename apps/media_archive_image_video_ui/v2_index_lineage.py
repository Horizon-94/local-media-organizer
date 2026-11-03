"""Exact current ancestry for index inputs, scoped to their dependency union."""
import json
import sqlite3
from .v2_current_jobs_cache import current_keys


def current_input_jobs(c, raw_refs, embedding_refs):
    raw_refs = {key for key in raw_refs if key is not None}
    embedding_refs = {key for key in embedding_refs if key is not None}
    refs = sorted(raw_refs | embedding_refs)
    if not refs:
        return {}, {}
    underlying = c
    while not isinstance(underlying, sqlite3.Connection):
        underlying = underlying.connection
    raw = lambda sql: sqlite3.Connection.execute(underlying, sql)
    # Retain one SQLite snapshot, including when the caller owns a write
    # transaction. No shared proof survives a mutation, rollback or revision.
    raw('SAVEPOINT index_input_lineage')
    try:
        # A complete, active dependency union proves every requested DONE
        # root current, including cycles. Keep this proof in this SQLite
        # snapshot only. A missing/pending/retired ancestor falls back to the
        # original root-relative algorithm (a pending root can be current).
        invalid = c.execute('''WITH RECURSIVE needed(key) AS (
            SELECT value FROM json_each(?) UNION
            SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.key)
            SELECT 1 FROM needed n LEFT JOIN v2_jobs j ON j.execution_key=n.key
            LEFT JOIN v2_active_sources a ON a.source_id=j.source_id
            WHERE j.execution_key IS NULL OR j.status IS NULL OR j.status<>'DONE'
               OR (a.source_revision IS NOT NULL AND a.source_revision IS NOT j.source_revision)
            LIMIT 1''', (json.dumps(refs),)).fetchone()
        if invalid is None:
            return (
                {r['execution_key']:dict(r) for r in c.execute(
                    'SELECT execution_key,status,result_sha FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',
                    (json.dumps(sorted(raw_refs)),))},
                {r['execution_key']:dict(r) for r in c.execute(
                    'SELECT execution_key,status,result_sha,provenance,result_json FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',
                    (json.dumps(sorted(embedding_refs)),))},
            )
        hint = ' INDEXED BY v2_jobs_lineage_header' if c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='v2_jobs_lineage_header'").fetchone() else ''
        rows = c.execute('''SELECT j.execution_key,j.status,j.source_id,j.source_revision,
            d.parent_key,a.source_revision AS active_revision FROM (
            WITH RECURSIVE needed(key) AS (
                SELECT value FROM json_each(?) UNION
                SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.key)
            SELECT key FROM needed) n JOIN v2_jobs j''' + hint + ''' ON j.execution_key=n.key
            LEFT JOIN v2_job_dependencies d ON d.child_key=j.execution_key
            LEFT JOIN v2_active_sources a ON a.source_id=j.source_id''', (json.dumps(refs),)).fetchall()
        jobs, dependencies, active = {}, [], {}
        for row in rows:
            jobs[row['execution_key']] = dict(row)
            if row['parent_key'] is not None:
                dependencies.append((row['execution_key'], row['parent_key']))
            if row['active_revision'] is not None:
                active[row['source_id']] = row['active_revision']
        valid = current_keys(list(jobs.values()), dependencies, active)
        # Large OCR raw payloads are deliberately not read for raw-result
        # checks; only embedding rows need their vector/provenance payload.
        raw_jobs = {r['execution_key']: dict(r) for r in c.execute(
            'SELECT execution_key,status,result_sha FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',
            (json.dumps(sorted(set(raw_refs))),)) if r['execution_key'] in valid}
        embeddings = {r['execution_key']: dict(r) for r in c.execute(
            'SELECT execution_key,status,result_sha,provenance,result_json FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',
            (json.dumps(sorted(set(embedding_refs))),)) if r['execution_key'] in valid}
        return raw_jobs, embeddings
    finally:
        raw('RELEASE index_input_lineage')
