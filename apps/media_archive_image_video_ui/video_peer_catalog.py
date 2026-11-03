"""Indexed immutable semantic peer facts; only equal text is a dependency.

No global payload scan in source proposals. Legacy duplicate-quality semantics
remain exact. Absence/presence of an equal-text peer is rechecked at commit.
"""
import json
from .video_commit_queue import SQLCommand,WriteProposal
from .v2_projection_peers import text_key

def schema(adapter):
    with adapter.store.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS video_peer_text_v1(execution_key TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,stage TEXT,text_key TEXT,result_sha TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS video_peer_text_key_v1 ON video_peer_text_v1(stage,text_key)')
        c.execute('CREATE INDEX IF NOT EXISTS video_peer_text_source_v1 ON video_peer_text_v1(source_id,source_revision)')

def command(key,source,revision,stage,result,digest):
    _,group=text_key(stage,result)
    return SQLCommand('INSERT OR IGNORE INTO video_peer_text_v1 VALUES(?,?,?,?,?,?)',(key,source,revision,stage,group,digest))

def bootstrap(adapter):
    # Startup-only recovery of existing immutable facts. No inference and no
    # change to successes. Computation precedes bounded writer transactions.
    with adapter.store.connect() as c:
        rows=[dict(r) for r in c.execute("SELECT j.execution_key,j.source_id,j.source_revision,j.stage,j.result_sha,j.result_json FROM v2_current_jobs j LEFT JOIN video_peer_text_v1 p USING(execution_key) WHERE j.stage IN ('qwen','ocr') AND j.status='DONE' AND p.execution_key IS NULL")]
    for start in range(0,len(rows),32):
        batch=rows[start:start+32]
        cmds=tuple(command(r['execution_key'],r['source_id'],r['source_revision'],r['stage'],json.loads(r['result_json']),r['result_sha']) for r in batch)
        checks=tuple((SQLCommand('SELECT status,result_sha FROM v2_jobs WHERE execution_key=?',(r['execution_key'],)),('DONE',r['result_sha'])) for r in batch)
        adapter.ledger.commit_queue.submit(WriteProposal('peer_catalog_recovery','',cmds,checks)).result()

PEERS="""SELECT p.execution_key,p.result_sha,p.stage,p.text_key FROM video_peer_text_v1 p
 JOIN v2_jobs j ON j.execution_key=p.execution_key AND j.result_sha=p.result_sha AND j.status='DONE'
 JOIN v2_active_sources a ON a.source_id=p.source_id AND a.source_revision=p.source_revision
 WHERE (p.stage,p.text_key) IN (SELECT stage,text_key FROM video_peer_text_v1 WHERE source_id=? AND source_revision=?)
 ORDER BY p.execution_key"""

def matching(c,source,revision):
    return [tuple(r) for r in c.execute(PEERS,(source,revision))]

def membership_check(source,revision,rows):
    # Exact indexed set proof, including the previous absence of peers. Small
    # header facts only, no ancestry/payload construction under the writer.
    sql='SELECT COALESCE(json_group_array(value),\'[]\') FROM (SELECT execution_key||\':\'||result_sha value FROM ('+PEERS+'))'
    expected=json.dumps([r[0]+':'+r[1] for r in rows],separators=(',',':'))
    return SQLCommand(sql,(source,revision)),(expected,)

DIRTY="""SELECT h.source_id,h.source_revision,h.snapshot_id FROM v2_final_projection_heads h
 JOIN v2_jobs head ON head.execution_key=h.execution_key
 JOIN v2_active_sources active ON active.source_id=h.source_id AND active.source_revision=h.source_revision
 WHERE head.status<>'DONE' OR EXISTS(
 SELECT 1 FROM video_peer_text_v1 own JOIN video_peer_text_v1 peer ON peer.stage=own.stage AND peer.text_key=own.text_key
 JOIN v2_jobs j ON j.execution_key=peer.execution_key AND j.status='DONE' AND j.result_sha=peer.result_sha
 JOIN v2_active_sources a ON a.source_id=peer.source_id AND a.source_revision=peer.source_revision
 WHERE own.source_id=h.source_id AND own.source_revision=h.source_revision
 AND NOT EXISTS(SELECT 1 FROM v2_job_dependencies d WHERE d.child_key=h.execution_key AND d.parent_key=peer.execution_key))
 ORDER BY h.source_id"""

def dirty_heads(c,limit=4,after=''):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='video_peer_text_v1'").fetchone():return []
    return [tuple(r) for r in c.execute('SELECT * FROM ('+DIRTY+') WHERE source_id>? LIMIT ?',(after,limit))]

def refresh(adapter,limit=4):
    # A later exact duplicate is a REAL semantic dependency. Never bring
    # unrelated sources back into source snapshots just to catch this case.
    with adapter.store.connect() as c:
        heads=dirty_heads(c,limit,getattr(adapter,'peer_refresh_cursor',''))
        if not heads:heads=dirty_heads(c,limit)
    if heads:adapter.peer_refresh_cursor=heads[-1][0]
    from .video_detached_coordination import pipeline_operation
    for source,revision,snapshot in heads:
        def calculate(p):
            with p.store.connect() as c:old=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if not p.final_projection.advance(source,revision,snapshot):return False
            with p.store.connect() as c:
                new=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
                if old and new and old[0]!=new[0]:
                    c.execute("DELETE FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SOURCE_COMPLETE','VISUAL_COMPLETE')",(source,revision))
            return True
        pipeline_operation(adapter,source,revision,'final_projection',calculate)
    return len(heads)
