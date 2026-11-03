"""Strict source-local lineage reads, with no project-wide graph or cache.

Columns and predicates are internal SQL fragments, never user input. All
proofs share the caller's transaction/snapshot and expire with this call.
"""
import json
import sqlite3


def current_source_rows(c,source,revision,where='1',args=(),columns='j.*'):
    return current_filtered_rows(c,'j.source_id=? AND j.source_revision=? AND ('+where+')',
        (source,revision)+tuple(args),columns)


def current_filtered_rows(c,where,args,columns='j.*'):
    """Internal filtered roots (source or owned status subset), never all jobs."""
    if not where or where.strip()=='1':raise ValueError('lineage_root_scope_required')
    from .v2_current_jobs_cache import correlated_predicate
    connection=c
    while not isinstance(connection,sqlite3.Connection):connection=connection.connection
    raw=lambda sql:sqlite3.Connection.execute(connection,sql)
    raw('SAVEPOINT source_local_lineage')
    try:
        roots=list(c.execute('SELECT '+columns+" FROM v2_jobs j WHERE j.status<>'SUPERSEDED' AND ("+where+')',tuple(args)))
        if not roots:return roots
        keys=[r['execution_key'] for r in roots]
        if all(r['status']=='DONE' for r in roots):
            invalid=c.execute('''WITH RECURSIVE needed(key) AS (
                SELECT value FROM json_each(?) UNION
                SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.key)
                SELECT 1 FROM needed n LEFT JOIN v2_jobs j ON j.execution_key=n.key
                LEFT JOIN v2_active_sources a ON a.source_id=j.source_id
                WHERE j.execution_key IS NULL OR j.status IS NULL OR j.status<>'DONE'
                OR (a.source_revision IS NOT NULL AND a.source_revision IS NOT j.source_revision) LIMIT 1''',(json.dumps(keys),)).fetchone()
            if invalid is None:return roots
        # Nonterminal/corrupt roots retain the exact original root-relative
        # predicate, including cycle behavior and unfinished root semantics.
        valid={r[0] for r in c.execute("SELECT j.execution_key FROM v2_jobs j WHERE j.execution_key IN (SELECT value FROM json_each(?)) AND j.status<>'SUPERSEDED' AND "+correlated_predicate(c),(json.dumps(keys),))}
        return [r for r in roots if r['execution_key'] in valid]
    finally:raw('RELEASE source_local_lineage')
