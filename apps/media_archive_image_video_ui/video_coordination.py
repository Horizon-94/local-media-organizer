"""Small durable generation fences for detached VIDEO coordination work."""
import json
import time
from .video_commit_queue import SQLCommand
from .v2_producer import fingerprint


def current_evidence_check(execution_key):
    """Exact current-lineage proof rooted at one immutable input.

    The writer owns a plain SQLite connection, not the ledger's TEMP view.
    Do not use main.v2_current_jobs: that legacy view seeds every project job.
    ORIGINAL is the same frozen ancestry predicate used by ledger readers.
    """
    from .v2_current_jobs_cache import ORIGINAL
    return SQLCommand('SELECT j.status,j.result_sha,j.source_id,j.source_revision FROM main.v2_jobs j '
        "WHERE j.execution_key=? AND j.status<>'SUPERSEDED' AND "+ORIGINAL,(execution_key,))


def install_tokens(store,tables):
    with store.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS video_stream_change_tokens_v1(scope TEXT PRIMARY KEY,generation INTEGER NOT NULL)')
        for table in tables:
            definition=c.execute("SELECT sql FROM sqlite_master WHERE name=?",(table,)).fetchone()
            if definition and 'CREATE VIRTUAL TABLE' in definition[0].upper():continue
            columns={row[1] for row in c.execute('PRAGMA table_info('+table+')')}
            if not columns:continue
            for action in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if action=='DELETE' else 'NEW'
                scope="'source:'||"+ref+'.source_id' if 'source_id' in columns else "'manual'"
                if table=='v2_job_dependencies':
                    scope="'source:'||(SELECT source_id FROM v2_jobs WHERE execution_key="+ref+'.child_key)'
                elif table=='v2_snapshot_evidence':
                    scope="'source:'||(SELECT source_id FROM v2_selection_snapshots WHERE snapshot_id="+ref+'.snapshot_id)'
                previous=''
                if action=='UPDATE' and 'source_id' in columns:
                    previous="INSERT INTO video_stream_change_tokens_v1 VALUES('source:'||OLD.source_id,1) ON CONFLICT(scope) DO UPDATE SET generation=generation+1;"
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS video_token_v2_{table}_{action}
                    AFTER {action} ON {table} BEGIN
                    INSERT INTO video_stream_change_tokens_v1 VALUES({scope},1)
                    ON CONFLICT(scope) DO UPDATE SET generation=generation+1;
                    {previous}
                    END''')


def tokens(c,source):
    return tuple(c.execute('SELECT COALESCE((SELECT generation FROM video_stream_change_tokens_v1 WHERE scope=?),0)',
                           (scope,)).fetchone()[0] for scope in ('source:'+source,'manual'))


def guards(adapter,source,revision,token):
    checks=[
        (SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(adapter.ledger.owner,adapter.ledger.epoch)),
        (SQLCommand('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(source,)),(revision,)),
        (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)),(revision,))]
    checks.extend((SQLCommand('SELECT COALESCE((SELECT generation FROM video_stream_change_tokens_v1 WHERE scope=?),0)',(scope,)),(expected,))
                  for scope,expected in zip(('source:'+source,'manual'),token))
    return tuple(checks)


def event_command(adapter,event_type,source,revision,version,payload):
    with adapter.store.connect() as c:
        row=c.execute('SELECT policy_hash FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
    now=time.time()
    event={'schema_version':'pipeline_v2_streaming_semantic_gate_v1','event_type':event_type,
           'source_id':source,'source_revision':revision,'aggregate_version':version,
           'producer_id':'pipeline-v2','occurred_at':now,'committed_at':now,
           'policy_or_contract_hash':row[0] if row else adapter.store.policy.fingerprint,'payload':payload,
           'event_id':fingerprint((event_type,source,revision,payload))}
    return SQLCommand('INSERT OR IGNORE INTO v2_outbox(event_id,aggregate_id,version,event_type,payload,committed_at) VALUES(?,?,?,?,?,?)',
        (event['event_id'],source+':'+revision,version,event_type,json.dumps(event,sort_keys=True),now))
