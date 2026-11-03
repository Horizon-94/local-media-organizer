"""Completed-snapshot delta planning. Frozen cohorts never get silently replanned.

Resume of an interrupted snapshot is unchanged. A new scan is an explicit operation
after the previous snapshot is terminal; its additions form a new frozen cohort.
"""
import json,sqlite3
from pathlib import Path
from apps.media_archive_image_video_ui.v2_producer import fingerprint

def signature(r):return (r['source_id'],r['bytes'],r['mtime_ns'],r.get('inode'))

def combine(cohorts,rows):
    active={r['source_id'] for r in rows if r['media_kind']=='image'}
    latest={r['source_content_id']:p for p in cohorts for r in p['rows']}
    result={k:v for k,v in cohorts[-1].items() if k not in ('fingerprint','rows','decisions','finder_tags','covered_by','sidecar_assets')}
    result.update(rows=[],decisions=[],finder_tags=[],covered_by={},sidecar_assets=[],cohorts=cohorts)
    for p in cohorts:
        own={s for s in active if latest[s] is p}
        result['rows'] += [r for r in p['rows'] if r['source_content_id'] in own]
        result['decisions'] += [r for r in p['decisions'] if r['parent_source_content_id'] in own]
        result['finder_tags'] += [r for r in p['finder_tags'] if r['source_content_id'] in own]
        result['covered_by'].update({s:refs for s,refs in p['covered_by'].items() if s in own})
        result['sidecar_assets'] += p.get('sidecar_assets',[])
    result['fingerprint']=fingerprint(result);return result

def cohort(plan,sid):
    return next((p for p in reversed(plan.get('cohorts',[])) if any(r['source_content_id']==sid for r in p['rows'])),plan)

def prepare(run,config):
    """Only reads business history. Caller saves one crash-safe descriptor."""
    if config.get('release_scope')!='BUILD150_IMAGE_VIDEO_ONLY':raise ValueError('INCREMENTAL_REQUIRES_RELEASE_SCOPE')
    with sqlite3.connect((run/'runtime/video.sqlite').as_uri()+'?mode=ro',uri=True) as c:
        if c.execute('SELECT 1 FROM release_registry WHERE present=1 AND terminal=0 LIMIT 1').fetchone():raise ValueError('RESUME_CURRENT_SNAPSHOT_BEFORE_INCREMENTAL_SCAN')
    from mixed_inventory import inventory
    from release_image_video import release_rows
    from apps.media_archive_image_video_ui.image_frontend import build_plan,metadata,verify_resume_authority
    from apps.media_archive_image_video_ui.v2_task_materializer import _read_finder_xattr
    old=json.loads((run/'manifest.json').read_text());oldplan=json.loads((run/'image-plan.json').read_text())
    inv=inventory(Path(config['source_root']),include_audio=False,defer_video_probe=True);rows,ignored=release_rows(inv)
    prior={r['relative_path']:r for r in old['files']};changed=[]
    diff={'UNCHANGED':[],'NEW':[],'CHANGED':[],'MISSING':[],'INCOMPLETE':[]}
    with sqlite3.connect((run/'runtime/video.sqlite').as_uri()+'?mode=ro',uri=True) as c:
        incomplete={r[0] for r in c.execute('SELECT source_id FROM release_registry WHERE present=1 AND terminal=0')}
    current_paths={r['relative_path'] for r in rows}
    diff['MISSING']=[{'source_id':r['source_id'],'relative_path':r['relative_path'],'state':'SOURCE_MISSING'} for r in old['files'] if r['relative_path'] not in current_paths]
    for r in rows:
        p=prior.get(r['relative_path']);different=p is None or signature(r)!=signature(p)
        if not different and r['media_kind']=='video' and p.get('probe',{}).get('ok'):
            r.update(probe=p['probe'],probe_pending=False)
        if not different and r['media_kind']=='image':
            plan=cohort(oldplan,r['source_id']);tags=sorted(t['tag_raw'] for t in plan['finder_tags'] if t['source_content_id']==r['source_id'])
            raw,_=_read_finder_xattr(Path(r['path']));different=tags!=sorted(raw)
        category='NEW' if p is None else 'CHANGED' if different else 'INCOMPLETE' if r['source_id'] in incomplete else 'UNCHANGED'
        diff[category].append({'source_id':r['source_id'],'relative_path':r['relative_path']})
        if different:changed.append(r)
    fields=metadata([Path(r['path']) for r in changed if r['media_kind']=='image'],required=True)
    delta={**inv,'files':[{**r,'metadata':fields.get(r['path'],{})} if r['media_kind']=='image' else r for r in changed]+[r for r in inv['files'] if r['media_kind']=='sidecar']}
    newplan=build_plan(delta)
    plans=oldplan.get('cohorts',[oldplan])+[newplan]
    plan=combine(plans,rows)
    # Verify only current, unchanged authorities: deleted/changed historical
    # files stay evidence, not a prerequisite for resuming the new snapshot.
    verify_current(plan,rows)
    return {'manifest':{**inv,'files':rows},'plan':plan,'ignored':ignored,'diff':diff,'diff_counts':{k:len(v) for k,v in diff.items()},
            'changed_sources':[r['source_id'] for r in changed],
            'snapshot':fingerprint([(r['relative_path'],signature(r)) for r in rows])}

def verify_current(plan,rows):
    from apps.media_archive_image_video_ui.image_frontend import verify_resume_authority
    active={r['source_id'] for r in rows if r['media_kind']=='image'}
    seen=set()
    for p in reversed(plan.get('cohorts',[plan])):
        keep={r['source_content_id'] for r in p['rows']} & active-seen;seen |= keep
        # verify_resume_authority validates code/Finder authority. Do not mutate
        # the original frozen cohort or its digest used by source revisions.
        subset={**p,'rows':[r for r in p['rows'] if r['source_content_id'] in keep]}
        verify_resume_authority(subset)

def register_changed(runtime,view,row,verified_digest=None):
    """Revision activation, retaining historical evidence/jobs; no model calls."""
    sid=row['source_id'];path=Path(row['path']);st=path.stat()
    from apps.media_archive_image_video_ui.v2_model_workers import sha_file
    from apps.media_archive_image_video_ui.video_stream_adapter import EvidenceStore
    with runtime.ledger.connection() as c:
        old=c.execute('SELECT i.source_json,i.revision FROM video_stream_inputs_v1 i JOIN video_stream_sources_v1 a ON a.source_id=i.source_id AND a.revision=i.revision WHERE i.source_id=?',(sid,)).fetchone()
        active=c.execute('SELECT source_revision,generation FROM v2_active_sources WHERE source_id=?',(sid,)).fetchone()
    if not old:return None
    prior=json.loads(old['source_json']);digest=verified_digest or sha_file(path)
    if row['media_kind']=='image':
        with runtime.ledger.connection() as c:
            image=c.execute('SELECT * FROM image_sources_v1 WHERE source_id=?',(sid,)).fetchone()
        if image and image['plan_sha']==view.plan['fingerprint'] and prior['stat']==[st.st_size,st.st_mtime_ns,st.st_ino] and active and active[0]==old['revision']:return None
        asset=runtime.image_rows[sid];decision=runtime.decisions[sid]
        metadata={**asset,'media_type':'image','media_kind':'image','relative_path':row['relative_path'],'a9t_plan':decision,'planning_revision':view.plan['fingerprint']}
        content=fingerprint((digest,view.plan['fingerprint']))
    else:
        if prior.get('digest')==digest and prior['stat']==[st.st_size,st.st_mtime_ns,st.st_ino] and active and active[0]==old['revision']:return None
        metadata={'media_type':'video','source_relative_path':row['relative_path'],'source_stat':[st.st_size,st.st_mtime_ns,st.st_ino],'pipeline_revision':view.pipeline_revision}
        content=fingerprint((digest,st.st_size,st.st_mtime_ns,st.st_ino))
    if (path.stat().st_size,path.stat().st_mtime_ns,path.stat().st_ino)!=(st.st_size,st.st_mtime_ns,st.st_ino):raise ValueError('DELTA_SOURCE_CHANGED_DURING_HASH')
    source=dict(source_id=sid,digest=digest,path=str(path),stat=[st.st_size,st.st_mtime_ns,st.st_ino],relative_path=row['relative_path'],media_kind=row['media_kind'])
    if row['media_kind']=='video':source['content_revision']=content
    revision=view.adapters.store.source_ready(sid,content,digest,path,metadata)
    with runtime.ledger.connection() as c:current=c.execute('SELECT source_revision,generation FROM v2_active_sources WHERE source_id=?',(sid,)).fetchone()
    if current and current[0]!=revision:
        view.adapters.store.activate_revision(sid,revision,current[1])
    runtime.ledger.source(sid,revision)
    with runtime.ledger.write_connection() as c:
        if row['media_kind']=='image':
            c.execute('CREATE TABLE IF NOT EXISTS release_image_source_history(source_id,revision,plan_sha,decision,PRIMARY KEY(source_id,revision))')
            c.execute('INSERT OR IGNORE INTO release_image_source_history SELECT * FROM image_sources_v1 WHERE source_id=?',(sid,))
            c.execute('INSERT OR REPLACE INTO image_sources_v1 VALUES(?,?,?,?)',(sid,revision,view.plan['fingerprint'],json.dumps(decision,sort_keys=True)))
        c.execute('INSERT OR IGNORE INTO video_stream_inputs_v1 VALUES(?,?,?,NULL)',(sid,revision,json.dumps(source,sort_keys=True)))
    view.sources.append((source,revision));view.publishers[revision]=view.adapters.store
    return revision
