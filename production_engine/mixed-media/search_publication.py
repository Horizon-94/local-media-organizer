"""Source-sized atomic publication and delta folder checkpoints in the existing DB."""
import json,time
from pathlib import PurePosixPath
from apps.media_archive_image_video_ui.v2_producer import fingerprint
POLICY='source-search-commit-v1'
DISCOVERY_IDENTITY='folder-discovery-inode-and-authority-v2'

def discovery_key(row):
    return (DISCOVERY_IDENTITY,row['source_id'],row['media_kind'],row.get('bytes'),row.get('mtime_ns'),row.get('inode'),row.get('publication_authority'))

def schema(c):
    statements=[
    'CREATE TABLE IF NOT EXISTS release_registry(relative_path TEXT PRIMARY KEY,source_id TEXT,revision TEXT,folder TEXT,signature TEXT,present INTEGER,terminal INTEGER,committed INTEGER,row_json TEXT)',
    'CREATE INDEX IF NOT EXISTS release_registry_source ON release_registry(source_id,revision,present)',
    'CREATE INDEX IF NOT EXISTS release_registry_pending ON release_registry(present,terminal)',
    'CREATE TABLE IF NOT EXISTS release_folders(folder TEXT PRIMARY KEY,snapshot TEXT,total INTEGER,terminal INTEGER,committed INTEGER,state TEXT,completed_at REAL,policy TEXT)',
    'CREATE TABLE IF NOT EXISTS release_folder_checkpoints(folder TEXT,snapshot TEXT,total INTEGER,terminal INTEGER,committed INTEGER,completed_at REAL,policy TEXT,PRIMARY KEY(folder,snapshot))',
    'CREATE TABLE IF NOT EXISTS release_search_heads(source_id TEXT PRIMARY KEY,revision TEXT,generation TEXT,visible INTEGER,committed_at REAL)',
    'CREATE TABLE IF NOT EXISTS release_search_segments(segment_id TEXT PRIMARY KEY,source_id TEXT,revision TEXT,generation TEXT,start_ms REAL,end_ms REAL,artifact_id TEXT,documents TEXT,speech_present INTEGER,evidence TEXT)',
    'CREATE INDEX IF NOT EXISTS release_search_segment_source ON release_search_segments(source_id,revision,generation)',
    'CREATE TABLE IF NOT EXISTS release_publication_queue(source_id TEXT,revision TEXT,state TEXT,PRIMARY KEY(source_id,revision))',
    'CREATE TABLE IF NOT EXISTS release_publication_dependencies(parent TEXT,parent_revision TEXT,source_id TEXT,revision TEXT,PRIMARY KEY(parent,parent_revision,source_id,revision))',
    '''CREATE TRIGGER IF NOT EXISTS release_head_wake AFTER INSERT ON release_search_heads BEGIN
       INSERT OR IGNORE INTO release_publication_queue SELECT source_id,revision,'SOURCE_COMPLETE'
       FROM release_publication_dependencies WHERE parent=NEW.source_id AND parent_revision=NEW.revision; END''',
    '''CREATE TRIGGER IF NOT EXISTS release_terminal_event AFTER INSERT ON video_stream_source_states_v1
       WHEN NEW.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL') BEGIN
       INSERT OR IGNORE INTO release_publication_queue VALUES(NEW.source_id,NEW.revision,NEW.state); END''',
    '''CREATE VIEW IF NOT EXISTS release_searchable_documents AS SELECT d.* FROM v2_searchable_documents d
       JOIN release_search_heads h ON h.source_id=d.source_id AND h.revision=d.source_revision AND h.visible=1
       JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision AND r.present=1 AND r.committed=1''']
    for s in statements:c.execute(s)

def folder(path):
    p=PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts:raise ValueError('RELATIVE_FOLDER_IDENTITY_REQUIRED')
    return str(p.parent)

def refresh_folder(c,name,dirty=False):
    # Arithmetic counters and state derive from the writer's current folder
    # row, not a detached copy: two source commits cannot lose an increment.
    c.execute('''UPDATE release_folders SET state=CASE WHEN ? AND terminal<total THEN 'FOLDER_DIRTY'
       WHEN terminal=total THEN 'FOLDER_COMPLETE' WHEN committed>0 THEN 'FOLDER_SEARCHABLE_PARTIAL' ELSE 'FOLDER_PENDING' END,
       completed_at=CASE WHEN terminal=total THEN COALESCE((SELECT p.completed_at FROM release_folder_checkpoints p
       WHERE p.folder=release_folders.folder AND p.snapshot=release_folders.snapshot),?) ELSE NULL END WHERE folder=?''',(int(dirty),time.time(),name))
    c.execute('''INSERT OR IGNORE INTO release_folder_checkpoints SELECT folder,snapshot,total,terminal,committed,completed_at,policy
       FROM release_folders WHERE folder=? AND state='FOLDER_COMPLETE' ''',(name,))

def reconcile(c,rows):
    """One complete discovery snapshot; no models, source revisions or index rebuild."""
    selected={r['relative_path']:r for r in rows if r['media_kind'] in ('image','video')}
    groups={}
    for path,r in selected.items():groups.setdefault(folder(path),[]).append((path,discovery_key(r)))
    changed=set()
    for name,items in groups.items():
        snapshot=fingerprint(sorted(items));old=c.execute('SELECT snapshot FROM release_folders WHERE folder=?',(name,)).fetchone()
        if old and old[0]!=snapshot:changed.add(name)
        c.execute('INSERT OR IGNORE INTO release_folders VALUES(?,?,0,0,0,\'FOLDER_PENDING\',NULL,?)',(name,snapshot,POLICY))
        c.execute('UPDATE release_folders SET snapshot=? WHERE folder=?',(snapshot,name))
    old={r['relative_path']:r for r in c.execute('SELECT * FROM release_registry WHERE present=1')}
    for path,r in old.items():
        new=selected.get(path);sig=fingerprint(discovery_key(new)) if new else None
        if new and sig==r['signature']:continue
        changed.add(r['folder'])
        c.execute('UPDATE release_search_heads SET visible=0 WHERE source_id=?',(r['source_id'],))
        c.execute('UPDATE release_registry SET present=0,terminal=0,committed=0 WHERE relative_path=?',(path,))
        c.execute('UPDATE release_folders SET total=total-1,terminal=terminal-?,committed=committed-? WHERE folder=?',(r['terminal'],r['committed'],r['folder']))
    for path,r in selected.items():
        sig=fingerprint(discovery_key(r))
        if path in old and old[path]['signature']==sig:continue
        name=folder(path)
        c.execute('INSERT OR REPLACE INTO release_registry VALUES(?,?,NULL,?,?,1,0,0,?)',(path,r['source_id'],name,sig,json.dumps(r,sort_keys=True)))
        c.execute('UPDATE release_folders SET total=total+1 WHERE folder=?',(name,))
    for name in changed-set(groups):c.execute('UPDATE release_folders SET snapshot=? WHERE folder=?',(fingerprint([]),name))
    for name in set(groups)|changed:refresh_folder(c,name,name in changed)

def bind_revision(c,sid,revision):
    r=c.execute('SELECT * FROM release_registry WHERE source_id=? AND present=1',(sid,)).fetchone()
    if not r:return
    if r['revision'] and r['revision']!=revision:
        c.execute('UPDATE release_search_heads SET visible=0 WHERE source_id=?',(sid,))
        c.execute('UPDATE release_folders SET terminal=terminal-?,committed=committed-? WHERE folder=?',(r['terminal'],r['committed'],r['folder']))
        c.execute('UPDATE release_registry SET terminal=0,committed=0 WHERE relative_path=?',(r['relative_path'],))
    c.execute('UPDATE release_registry SET revision=? WHERE relative_path=?',(revision,r['relative_path']))
    # Same-revision published sources need no replay of a terminal no-op.
    for row in c.execute("SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL') AND EXISTS(SELECT 1 FROM release_registry WHERE source_id=? AND revision=? AND present=1 AND committed=0)",(sid,revision,sid,revision)):
        c.execute('INSERT OR IGNORE INTO release_publication_queue VALUES(?,?,?)',(sid,revision,row[0]))

def bind_live(runtime,sid,revision):
    """Frontend revision handoff through the existing writer, not another writer."""
    from apps.media_archive_image_video_ui.video_commit_queue import SQLCommand,WriteProposal
    with runtime.ledger.connection() as c:
        row=c.execute('SELECT * FROM release_registry WHERE source_id=? AND present=1',(sid,)).fetchone()
    if not row:raise ValueError('PUBLICATION_SOURCE_NOT_REGISTERED')
    commands=[]
    if row['revision'] and row['revision']!=revision:
        commands += [SQLCommand('UPDATE release_search_heads SET visible=0 WHERE source_id=?',(sid,)),
          SQLCommand('UPDATE release_folders SET terminal=terminal-?,committed=committed-? WHERE folder=?',(row['terminal'],row['committed'],row['folder'])),
          SQLCommand('UPDATE release_registry SET terminal=0,committed=0 WHERE relative_path=?',(row['relative_path'],))]
    commands += [SQLCommand('UPDATE release_registry SET revision=? WHERE relative_path=?',(revision,row['relative_path'])),
       SQLCommand("INSERT OR IGNORE INTO release_publication_queue SELECT source_id,revision,state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL') AND EXISTS(SELECT 1 FROM release_registry WHERE source_id=? AND revision=? AND present=1 AND committed=0)",(sid,revision,sid,revision))]
    checks=((SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(runtime.ledger.owner,runtime.ledger.epoch)),
       (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(sid,)),(revision,)),
       (SQLCommand('SELECT revision,terminal,committed FROM release_registry WHERE relative_path=?',(row['relative_path'],)),(row['revision'],row['terminal'],row['committed'])))
    runtime.ledger.commit_queue.submit(WriteProposal('search_revision_handoff',sid,tuple(commands),checks)).result()

def commit_source(c,sid,revision,state):
    r=c.execute('SELECT * FROM release_registry WHERE source_id=? AND revision=? AND present=1',(sid,revision)).fetchone()
    if not r:return
    # An explicitly authorized native recovery may finish this same revision
    # after publication recorded its earlier final failure. Keep that history;
    # only authoritative new SOURCE_COMPLETE can release the recovered source.
    recovered=False
    if r['terminal'] and not r['committed'] and state=='SOURCE_COMPLETE':
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_stream_operator_source_history_v1'").fetchone():
            recovered=bool(c.execute("SELECT 1 FROM video_stream_operator_source_history_v1 h WHERE h.source_id=? AND h.revision=? AND json_extract(h.previous_state,'$.state')='SOURCE_FAILED_FINAL' AND json_extract(h.previous_state,'$.source_id')=h.source_id AND json_extract(h.previous_state,'$.revision')=h.revision AND EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=h.source_id AND s.revision=h.revision AND s.state='SOURCE_COMPLETE') AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=h.source_id AND s.revision=h.revision AND s.state='SOURCE_FAILED_FINAL') LIMIT 1",(sid,revision)).fetchone())
    if r['terminal'] and not recovered:return
    if state=='SOURCE_FAILED_FINAL':
        c.execute('UPDATE release_registry SET terminal=1 WHERE relative_path=?',(r['relative_path'],))
        c.execute('UPDATE release_folders SET terminal=terminal+1 WHERE folder=?',(r['folder'],));refresh_folder(c,r['folder']);return
    if c.execute("SELECT 1 FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND status IN ('PENDING','RUNNING','FAILED_RETRYABLE') LIMIT 1",(sid,revision)).fetchone():raise ValueError('SEARCH_COMMIT_MODEL_NOT_TERMINAL')
    if c.execute("SELECT 1 FROM v2_documents WHERE source_id=? AND source_revision=? AND status='READY' LIMIT 1",(sid,revision)).fetchone():raise ValueError('SEARCH_COMMIT_EMBEDDING_NOT_DURABLE')
    docs=[dict(d) for d in c.execute("SELECT * FROM v2_documents WHERE source_id=? AND source_revision=? AND status IN ('EMBEDDED','SKIP_NO_TEXT')",(sid,revision))]
    speech=[dict(s) for s in c.execute('SELECT * FROM release_speech_intervals WHERE source_id=? AND revision=?',(sid,revision))]
    transcripts=[json.loads(s[0]) for s in c.execute('SELECT evidence FROM release_transcript_evidence WHERE source_id=? AND revision=?',(sid,revision))]
    artifacts=[dict(a) for a in c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY time_ms,artifact_id',(sid,revision))]
    coverage=c.execute('SELECT proof FROM image_group_coverage_v1 WHERE source_id=? AND revision=?',(sid,revision)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='image_group_coverage_v1'").fetchone() else None
    if coverage and not artifacts:
        representatives=json.loads(coverage[0])['representatives'];pending=False
        for ref in representatives:
            c.execute('INSERT OR IGNORE INTO release_publication_dependencies VALUES(?,?,?,?)',(ref['source_id'],ref['revision'],sid,revision))
            if not c.execute('SELECT 1 FROM release_search_heads WHERE source_id=? AND revision=? AND visible=1',(ref['source_id'],ref['revision'])).fetchone():pending=True
        if pending:return # Parent SEARCH_COMMITTED events wake this source; no polling.
        for ref in representatives:
            proxy=c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=? AND sha=?',(ref['artifact_id'],ref['sha'])).fetchone()
            if not proxy:raise ValueError('SEARCH_COVERAGE_ARTIFACT_CHANGED')
            artifacts.append(dict(proxy))
            docs += [dict(d) for d in c.execute("SELECT * FROM release_searchable_documents WHERE source_id=? AND source_revision=? AND document_type<>'whisper'",(ref['source_id'],ref['revision']))]
    generation=fingerprint((sid,revision,[(d['document_id'],d['embedding_ref']) for d in docs],speech,transcripts,POLICY))
    source=json.loads(r['row_json']);duration=float(source.get('probe',{}).get('metadata',{}).get('format',{}).get('duration',0) or 0)*1000
    if not artifacts:artifacts=[{'artifact_id':None,'time_ms':0,'path':None}]
    for a in artifacts:
        anchor=max(0,a['time_ms'] or 0);start=max(0,anchor-2500);end=anchor+5000
        if duration:end=min(end,duration)
        if end<=start:end=start+1
        visual=[d for d in docs if d['artifact_id']==a['artifact_id'] and d['document_type']!='whisper']
        if not visual:visual=[d for d in docs if d['document_type']!='whisper' and d['artifact_id'] is None]
        primary=[d for d in docs if d['document_type']=='whisper' and d['start_ms'] is not None and min(end,d['end_ms'])>max(start,d['start_ms'])]
        intervals=[s for s in speech if min(end,s['end_ms'])>max(start,s['start_ms'])]
        text_evidence=[t for t in transcripts if min(end,t['owning_interval'][1])>max(start,t['owning_interval'][0])]
        tier=next((t for t in ('SPEECH_CONFIDENT','SPEECH_UNCERTAIN','SPEECH_REJECTED') if any(e['transcript_quality']==t for e in text_evidence)), 'SPEECH_REJECTED' if intervals else 'NO_SPEECH')
        evidence={'source_id':sid,'relative_path':r['relative_path'],'resolved_path':source['path'],'start_ms':start,'end_ms':end,
                  'preview_anchor_ms':anchor,'preview_path':a.get('path'),'visual_evidence_refs':[d['raw_result_ref'] for d in visual],
                  'visual_coverage_proxy':({'source_id':a['source_id'],'revision':a['revision']} if coverage else None),
                  'coverage_visual_text':('\n'.join(d['text'] for d in visual) if coverage else None),
                  'vad_evidence':intervals,'transcripts':text_evidence,'audio_tags':['有人声','说话','讲话','speech','speaking'] if intervals else [],
                  'speech_present':bool(intervals),'transcript_quality':tier,'publication_state':'SEARCH_COMMITTED','generation':generation}
        segment=fingerprint((sid,revision,a['artifact_id'],start,end,generation))
        c.execute('INSERT OR IGNORE INTO release_search_segments VALUES(?,?,?,?,?,?,?,?,?,?)',
            (segment,sid,revision,generation,start,end,a['artifact_id'],json.dumps([d['document_id'] for d in visual+primary]),bool(intervals),json.dumps(evidence,sort_keys=True)))
    c.execute('INSERT OR REPLACE INTO release_search_heads VALUES(?,?,?,1,?)',(sid,revision,generation,time.time()))
    c.execute('UPDATE release_registry SET terminal=1,committed=1 WHERE relative_path=?',(r['relative_path'],))
    c.execute('UPDATE release_folders SET terminal=terminal+?,committed=committed+1 WHERE folder=?',(int(not r['terminal']),r['folder']))
    c.execute('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)',(sid,revision,'SEARCH_COMMITTED',json.dumps({'generation':generation,'policy':POLICY},sort_keys=True)))
    refresh_folder(c,r['folder'])

def advance(runtime,limit=16):
    from apps.media_archive_image_video_ui.video_commit_queue import SQLCommand,WriteProposal,StaleProposal
    from apps.media_archive_image_video_ui.video_coordination import guards,tokens
    class Preparation:
        def __init__(self,c):self.c=c;self.commands=[];self.checks=[]
        def execute(self,sql,params=()):
            if sql.lstrip().upper().startswith('SELECT'):
                result=self.c.execute(sql,params)
                if sql.startswith('SELECT 1 FROM release_search_heads WHERE'):
                    row=result.fetchone()
                    self.checks.append((SQLCommand('SELECT EXISTS('+sql+')',tuple(params)),(int(row is not None),)))
                    class One:
                        def fetchone(self):return row
                    return One()
                return result
            if not sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')):raise ValueError('PUBLICATION_SQL_ONLY_REQUIRED')
            self.commands.append(SQLCommand(sql,tuple(params)))
    with runtime.ledger.connection() as c:
        queue=list(c.execute('SELECT * FROM release_publication_queue LIMIT ?',(limit,)))
    for row in queue:
        sid,rev=row['source_id'],row['revision']
        with runtime.ledger.connection() as c:
            c.execute('BEGIN')
            registry=c.execute('SELECT revision,present,terminal,committed FROM release_registry WHERE source_id=?',(sid,)).fetchone()
            p=Preparation(c);commit_source(p,sid,rev,row['state'])
            p.commands.append(SQLCommand('DELETE FROM release_publication_queue WHERE source_id=? AND revision=?',(sid,rev)))
            checks=[(SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(runtime.ledger.owner,runtime.ledger.epoch))]
            if registry:
                checks.append((SQLCommand('SELECT revision,present,terminal,committed FROM release_registry WHERE source_id=?',(sid,)),tuple(registry)))
                if registry['present'] and registry['revision']==rev:
                    checks.extend(guards(runtime.for_source(sid),sid,rev,tokens(c,sid)))
            checks.extend(p.checks)
        try:runtime.ledger.commit_queue.submit(WriteProposal('search_publication',sid,tuple(p.commands),tuple(checks))).result()
        except StaleProposal:continue # The durable queue remains, next quantum re-prepares.

def status(c):
    folders=dict(c.execute('SELECT state,count(*) FROM release_folders GROUP BY state'))
    return {'folders_total':sum(folders.values()),'folders_complete':folders.get('FOLDER_COMPLETE',0),
            'folders_partial':folders.get('FOLDER_SEARCHABLE_PARTIAL',0),'folders_pending':sum(n for s,n in folders.items() if s not in ('FOLDER_COMPLETE','FOLDER_SEARCHABLE_PARTIAL')),
            'sources_total':c.execute('SELECT COALESCE(sum(total),0) FROM release_folders').fetchone()[0],
            'sources_search_committed':c.execute('SELECT COALESCE(sum(committed),0) FROM release_folders').fetchone()[0]}
