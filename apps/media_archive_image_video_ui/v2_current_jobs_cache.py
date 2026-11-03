"""Versioned current-lineage acceleration; never a new evidence contract.

Every relevant durable mutation invalidates the cache in the same transaction.
Dirty connections fall back to the original correlated ancestry predicate.
"""
import collections
import json
import os
import sqlite3
import threading

_LOCK = threading.RLock()
_CACHE = collections.OrderedDict()
ORIGINAL = """NOT EXISTS (
 WITH RECURSIVE ancestry(parent_key) AS (
  SELECT j.execution_key UNION SELECT d.parent_key FROM ancestry a
  JOIN main.v2_job_dependencies d ON d.child_key=a.parent_key)
 SELECT 1 FROM ancestry a LEFT JOIN main.v2_jobs p ON p.execution_key=a.parent_key
 LEFT JOIN main.v2_active_sources active ON active.source_id=p.source_id
 WHERE p.execution_key IS NULL OR
 (a.parent_key<>j.execution_key AND p.status<>'DONE') OR
(active.source_revision IS NOT NULL AND active.source_revision<>p.source_revision))"""


def correlated_predicate(c):
    # SQLite prefers its unique key lookup even when the covering ancestry
    # header avoids reading large OCR result rows. Only choose a present index.
    present=c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='v2_jobs_lineage_header'").fetchone()
    return ORIGINAL.replace('main.v2_jobs p ON','main.v2_jobs p INDEXED BY v2_jobs_lineage_header ON') if present else ORIGINAL


def lineage_headers(c):
    # The covering index avoids paging through GBs of OCR/model result payload.
    hint=' INDEXED BY v2_jobs_lineage_header' if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='v2_jobs_lineage_header'").fetchone() else ''
    return c.execute('SELECT execution_key,status,source_id,source_revision FROM v2_jobs'+hint+" WHERE status<>'SUPERSEDED'")


def current_keys(rows, dependencies, active):
    """Linear DAG pass; exact root-relative traversal for cyclic components."""
    jobs = {r['execution_key']: r for r in rows}
    parents = collections.defaultdict(set)
    children = collections.defaultdict(set)
    for child, parent in dependencies:
        parents[child].add(parent)
        if child in jobs and parent in jobs:
            children[parent].add(child)
    indegree = {key: sum(p in jobs for p in parents[key]) for key in jobs}
    queue = collections.deque(key for key, n in indegree.items() if not n)
    valid = {}
    def locally_valid(key):
        row = jobs[key]
        return (row['status'] != 'SUPERSEDED' and
                (active.get(row['source_id']) is None or
                 active[row['source_id']] == row['source_revision']))
    while queue:
        key = queue.popleft()
        valid[key] = locally_valid(key) and all(
            p in jobs and jobs[p]['status'] == 'DONE' and valid.get(p, False)
            for p in parents[key])
        for child in children[key]:
            indegree[child] -= 1
            if not indegree[child]: queue.append(child)
    for root in jobs.keys() - valid.keys():
        seen = set(); pending = [root]; good = True
        while pending:
            key = pending.pop()
            if key in seen: continue
            seen.add(key)
            if (key not in jobs or not locally_valid(key) or
                    (key != root and jobs[key]['status'] != 'DONE')):
                good = False; break
            pending.extend(parents[key])
        valid[root] = good
    return {key for key, good in valid.items() if good}


def install_schema(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS v2_lineage_cache_generation(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL);
        INSERT OR IGNORE INTO v2_lineage_cache_generation VALUES(1,0);''')
    watched = {'v2_jobs': 'status,execution_key,source_id,source_revision',
               'v2_job_dependencies': 'child_key,parent_key',
               'v2_active_sources': 'source_id,source_revision'}
    for table, columns in watched.items():
        for event, clause in [('insert', 'INSERT'), ('delete', 'DELETE'),
                              ('update', 'UPDATE OF '+columns)]:
            # Pending -> running does not change ancestry validity. Done
            # transitions only matter if this job is an existing ancestor.
            # SQL status/provenance filters still read the live job row.
            condition = ''
            if table == 'v2_jobs' and event == 'update':
                condition = '''WHEN OLD.execution_key IS NOT NEW.execution_key
                    OR OLD.source_id IS NOT NEW.source_id
                    OR OLD.source_revision IS NOT NEW.source_revision
                    OR (OLD.status='SUPERSEDED')<>(NEW.status='SUPERSEDED')
                    OR ((OLD.status='DONE')<>(NEW.status='DONE') AND EXISTS(
                        SELECT 1 FROM v2_job_dependencies WHERE parent_key=OLD.execution_key))'''
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS cache_generation_{table}_{event}
                AFTER {clause} ON {table} {condition} BEGIN
                UPDATE v2_lineage_cache_generation SET generation=generation+1 WHERE singleton=1;
                END''')


def install_stub(c):
    """Install stable TEMP schema before any caller opens a dependent cursor."""
    execute=lambda sql:sqlite3.Connection.execute(c,sql)
    if not execute("SELECT 1 FROM sqlite_master WHERE name='v2_lineage_cache_generation'").fetchone():return False
    execute('CREATE TEMP TABLE IF NOT EXISTS current_lineage_cache(generation INTEGER, valid_json TEXT)')
    execute('CREATE TEMP TABLE IF NOT EXISTS current_lineage_keys(execution_key TEXT PRIMARY KEY) WITHOUT ROWID')
    execute('''CREATE TEMP VIEW IF NOT EXISTS v2_current_jobs AS SELECT j.* FROM main.v2_jobs j
        WHERE j.status<>'SUPERSEDED' AND CASE WHEN
        (SELECT generation FROM main.v2_lineage_cache_generation WHERE singleton=1)=
        (SELECT generation FROM current_lineage_cache LIMIT 1)
        THEN EXISTS(SELECT 1 FROM current_lineage_keys k WHERE k.execution_key=j.execution_key)
        ELSE '''+ORIGINAL+' END')
    return True


def install_view(c, *, fresh_owned_snapshot=False):
    execute = lambda sql, args=(): sqlite3.Connection.execute(c, sql, args)
    if not install_stub(c):
        return False
    database = execute('PRAGMA database_list').fetchone()[2]
    # An outer transaction may contain uncommitted durable mutations. Its
    # generation can be reused after rollback and must not enter the shared
    # committed-state cache. TEMP changes still roll back with that transaction.
    # Only EpochConnection's freshly acquired write transaction may opt in:
    # ownership was checked using reads and no main-table write has occurred.
    # General/dirty transactions retain the rollback-safe private proof path.
    share_cache = not c.in_transaction or fresh_owned_snapshot
    # TEMP writes must not accidentally acquire a main-database writer lease.
    execute('SAVEPOINT current_lineage_cache')
    try:
        generation = execute('SELECT generation FROM v2_lineage_cache_generation WHERE singleton=1').fetchone()[0]
        cache_key = (database, generation)
        # Never hold a process mutex across SQLite reads. Another thread can
        # own a writer transaction while waiting for this same mutex.
        with _LOCK:
            # Committed proofs may be read in any matching snapshot. Only
            # publishing an uncommitted proof is forbidden: a dirty generation
            # cannot already have a committed cached proof from the future.
            serialized = _CACHE.get(cache_key)
            if serialized is not None:_CACHE.move_to_end(cache_key)
        if serialized is None:
            rows = [dict(r) for r in lineage_headers(c)]
            dependencies = [tuple(r) for r in execute("""SELECT d.child_key,d.parent_key
                FROM v2_job_dependencies d JOIN v2_jobs j ON j.execution_key=d.child_key
                WHERE j.status<>'SUPERSEDED'""")]
            active = dict(execute('SELECT source_id,source_revision FROM v2_active_sources'))
            serialized = json.dumps(sorted(current_keys(rows, dependencies, active)))
            if share_cache:
                with _LOCK:
                    _CACHE[cache_key] = serialized
                    _CACHE.move_to_end(cache_key)
                    while len(_CACHE) > 4: _CACHE.popitem(last=False)
        execute('DELETE FROM current_lineage_keys')
        sqlite3.Connection.executemany(c,'INSERT INTO current_lineage_keys VALUES(?)',
                                      ((key,) for key in json.loads(serialized)))
        execute('INSERT INTO current_lineage_cache SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM current_lineage_cache)', (generation, serialized))
        execute('UPDATE current_lineage_cache SET generation=?,valid_json=?', (generation, serialized))
        execute('RELEASE current_lineage_cache')
    except BaseException:
        execute('ROLLBACK TO current_lineage_cache'); execute('RELEASE current_lineage_cache'); raise
    return True


def install_readonly_view(c):
    query_only=sqlite3.Connection.execute(c,'PRAGMA query_only').fetchone()[0]
    try:
        sqlite3.Connection.execute(c,'PRAGMA query_only=OFF')
        if os.environ.get('MEDIA_ARCHIVE_CURRENT_LINEAGE_CACHE_OPT_IN')=='1' and install_view(c):return True
        sqlite3.Connection.execute(c,'''CREATE TEMP VIEW IF NOT EXISTS v2_current_jobs AS
            SELECT j.* FROM main.v2_jobs j WHERE j.status<>'SUPERSEDED' AND '''+correlated_predicate(c))
        return False
    finally:
        sqlite3.Connection.execute(c,'PRAGMA query_only='+str(int(query_only)))


def install_readonly_snapshot_view(c):
    """Exact linear proof for a UI reader's fixed transaction snapshot.

    Unlike a runtime cache, this cannot outlive its read transaction. No task
    tables, generations or triggers are created or changed.
    """
    if not c.in_transaction:
        raise ValueError('readonly_lineage_snapshot_requires_transaction')
    execute = lambda sql, args=(): sqlite3.Connection.execute(c, sql, args)
    rows = [dict(zip(('execution_key', 'status', 'source_id', 'source_revision'), row))
            for row in lineage_headers(c)]
    valid = current_keys(rows, execute('SELECT child_key,parent_key FROM main.v2_job_dependencies'),
                         dict(execute('SELECT source_id,source_revision FROM main.v2_active_sources')))
    query_only = execute('PRAGMA query_only').fetchone()[0]
    try:
        execute('PRAGMA query_only=OFF')
        execute('CREATE TEMP TABLE IF NOT EXISTS readonly_current_keys(execution_key TEXT PRIMARY KEY)')
        execute('DELETE FROM readonly_current_keys')
        sqlite3.Connection.executemany(c, 'INSERT INTO readonly_current_keys VALUES(?)',
                                      ((key,) for key in sorted(valid)))
        execute('DROP VIEW IF EXISTS temp.v2_current_jobs')
        execute('''CREATE TEMP VIEW v2_current_jobs AS SELECT j.* FROM main.v2_jobs j
                   JOIN readonly_current_keys k ON k.execution_key=j.execution_key''')
    finally:
        execute('PRAGMA query_only='+str(int(query_only)))
