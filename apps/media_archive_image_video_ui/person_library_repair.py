"""Bounded, resumable face-only maintenance of a completed V2 library.

The production DB is read-only. Evidence lives in a small companion DB;
production ownership, results, artifacts and index are never rewritten.
"""
import hashlib
import importlib.util
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .v2_current_jobs_cache import install_readonly_snapshot_view, correlated_predicate
from .v2_producer import fingerprint
from .v2_runtime import ContractError, validate_evidence

CONTRACT = 'completed_library_face_repair_v1'


def companion(database):
    return Path(database).parent / 'person-library-repair.sqlite'


def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def inventory(database, *, allow_active=False):
    c = readonly(database)
    try:
        c.execute('BEGIN')
        has_runs=c.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_runs'").fetchone()
        if not allow_active and has_runs and c.execute("SELECT 1 FROM scheduler_runs WHERE status IN ('STARTING','RUNNING','STOPPING') LIMIT 1").fetchone():
            raise ContractError('person_repair_active_product_worker')
        install_readonly_snapshot_view(c)
        rows = c.execute('''SELECT a.*,m.path AS source_path,m.metadata,j.execution_key,j.result_sha
            FROM v2_artifacts a JOIN v2_active_sources s
              ON s.source_id=a.source_id AND s.source_revision=a.revision
            JOIN v2_source_metadata m ON m.source_id=a.source_id AND m.source_revision=a.revision
            JOIN v2_current_jobs j ON j.item_id=a.artifact_id AND j.stage='yoloe' AND j.status='DONE'
              AND j.input_sha=a.sha
            WHERE a.status='READY' ORDER BY a.artifact_id''').fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def validate_current_input(database, expected):
    """Point proof equivalent to the snapshot, without rescanning every source."""
    c=readonly(database)
    try:
        c.execute('BEGIN')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_runs'").fetchone() and c.execute(
                "SELECT 1 FROM scheduler_runs WHERE status IN ('STARTING','RUNNING','STOPPING') LIMIT 1").fetchone():
            raise ContractError('person_repair_active_product_worker')
        row=c.execute('''SELECT a.*,m.path AS source_path,m.metadata,j.execution_key,j.result_sha
            FROM v2_jobs j JOIN v2_artifacts a ON a.artifact_id=j.item_id
            JOIN v2_active_sources s ON s.source_id=a.source_id AND s.source_revision=a.revision
            JOIN v2_source_metadata m ON m.source_id=a.source_id AND m.source_revision=a.revision
            WHERE j.execution_key=? AND j.stage='yoloe' AND j.status='DONE'
              AND j.input_sha=a.sha AND a.status='READY' AND '''+correlated_predicate(c),
              (expected['execution_key'],)).fetchone()
        if not row or dict(row)!=expected:
            raise ContractError('person_repair_inventory_changed')
    finally:
        c.close()


class FaceRepairStore:
    def __init__(self, path, database, rows, policy):
        self.path = Path(path)
        self.database = str(Path(database).resolve())
        self.policy = policy
        self.rows = rows
        self.digest = fingerprint([(r['artifact_id'],r['source_id'],r['revision'],r['sha'],
                                    r['execution_key'],r['result_sha']) for r in rows])
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS jobs(artifact_id TEXT PRIMARY KEY,metadata TEXT,
                  status TEXT,attempts INTEGER DEFAULT 0,token TEXT,lease_until REAL,
                  result_json TEXT,result_sha TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS groups(face_id TEXT PRIMARY KEY,group_id TEXT,
                  artifact_id TEXT,face_json TEXT,similarity REAL,representative INTEGER);
                CREATE TABLE IF NOT EXISTS display_geometry(artifact_id TEXT PRIMARY KEY,
                  width INTEGER,height INTEGER,orientation INTEGER);
            ''')
            binding = {'contract':CONTRACT,'database':self.database,'inventory':self.digest,'policy':policy}
            raw = json.dumps(binding,sort_keys=True)
            old = c.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
            if old and old[0] != raw:
                raise ContractError('person_repair_binding_changed')
            c.execute("INSERT OR IGNORE INTO metadata VALUES('binding',?)",(raw,))
            c.executemany("INSERT OR IGNORE INTO jobs(artifact_id,metadata,status) VALUES(?,?,'PENDING')",
                [(r['artifact_id'],json.dumps(r,sort_keys=True)) for r in rows])

    def connect(self):
        c = sqlite3.connect(self.path,timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def claim(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM jobs WHERE (status IN ('PENDING','FAILED_RETRYABLE') OR (status='RUNNING' AND lease_until<=?)) AND attempts<3 ORDER BY artifact_id LIMIT 1",(time.time(),)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            c.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1,token=?,lease_until=? WHERE artifact_id=?",
                (token,time.time()+120,row['artifact_id']))
            return {**dict(row),'token':token,'attempts':row['attempts']+1}

    def heartbeat(self, job):
        with self.connect() as c:
            return c.execute("UPDATE jobs SET lease_until=? WHERE artifact_id=? AND token=? AND status='RUNNING' AND lease_until>?",
                (time.time()+120,job['artifact_id'],job['token'],time.time())).rowcount == 1

    def commit(self, job, result):
        validate_evidence('insightface',result)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            changed = c.execute("UPDATE jobs SET status='DONE',result_json=?,result_sha=?,lease_until=NULL,error=NULL WHERE artifact_id=? AND token=? AND status='RUNNING' AND lease_until>?",
                (json.dumps(result,sort_keys=True),fingerprint(result),job['artifact_id'],job['token'],time.time())).rowcount
            if changed != 1:
                raise ContractError('person_repair_late_result')

    def fail(self, job, error):
        with self.connect() as c:
            changed = c.execute("UPDATE jobs SET status=?,error=?,lease_until=NULL WHERE artifact_id=? AND token=? AND status='RUNNING' AND lease_until>?",
                ('FAILED_FINAL' if job['attempts']>=3 else 'FAILED_RETRYABLE',str(error),job['artifact_id'],job['token'],time.time())).rowcount
            if changed != 1:
                raise ContractError('person_repair_failure_owner_lost')

    def progress(self):
        with self.connect() as c:
            return {r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM jobs GROUP BY status')}

    def cluster(self, root):
        """Reuse frozen centroid-gated clustering, including same-frame cannot-link."""
        import numpy as np
        from .v2_people_coordinates import display_geometry
        module_path = Path(root)/'scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py'
        spec = importlib.util.spec_from_file_location('person_repair_cluster',module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        records=[];geometry={}
        with self.connect() as c:
            for job in c.execute("SELECT artifact_id,metadata,result_json,result_sha FROM jobs WHERE status='DONE' ORDER BY artifact_id"):
                result=json.loads(job['result_json'])
                if fingerprint(result)!=job['result_sha']:
                    raise ContractError('person_repair_result_corrupt')
                validate_evidence('insightface',result)
                if result['faces']:
                    geometry[job['artifact_id']]=display_geometry(json.loads(job['metadata']))
                for slot,face in enumerate(result['faces']):
                    if face['quality_score'] < self.policy['minimum_quality_score']:
                        continue
                    fid=fingerprint((CONTRACT,job['artifact_id'],job['result_sha'],slot))
                    records.append({'row':{'face_id':fid,'visual_unit_id':job['artifact_id'],
                                          'face':face},'vector':np.asarray(face['embedding'],dtype=np.float32)})
        clusters=module._cluster_face_records(records,self.policy['auto_merge_cosine_min'])
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM groups')
            c.executemany('INSERT OR REPLACE INTO display_geometry VALUES(?,?,?,?)',
                [(aid,*values) for aid,values in geometry.items()])
            for cluster in clusters:
                gid='face_group_'+fingerprint(sorted(r['face_id'] for r,_ in cluster['members']))[:28]
                for row,similarity in cluster['members']:
                    c.execute('INSERT INTO groups VALUES(?,?,?,?,?,?)',(row['face_id'],gid,
                        row['visual_unit_id'],json.dumps(row['face'],sort_keys=True),similarity,
                        int(row['face_id']==cluster['representative_face_id'])))
            c.execute("INSERT OR REPLACE INTO metadata VALUES('clustered_at',?)",(str(time.time()),))
        return len(records),len(clusters)


def read_frames(database):
    path=companion(database)
    if not path.is_file():
        return None
    # A completed companion cannot make stale or revoked production evidence current.
    current={r['artifact_id']:r for r in inventory(database)}
    c=readonly(path)
    try:
        binding=json.loads(c.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0])
        if binding['contract']!=CONTRACT or binding['database']!=str(Path(database).resolve()):
            raise ContractError('person_repair_catalog_wrong_library')
        items=[]
        rows=c.execute("SELECT g.*,j.metadata,j.result_sha,d.width AS display_width,d.height AS display_height FROM groups g JOIN jobs j USING(artifact_id) JOIN display_geometry d USING(artifact_id) WHERE j.status='DONE' ORDER BY g.group_id,g.face_id")
        for row in rows:
            original=json.loads(row['metadata']);active=current.get(row['artifact_id'])
            if not active or any(active[k]!=original[k] for k in ('source_id','revision','sha','execution_key','result_sha')):
                continue
            face=json.loads(row['face_json']);meta=json.loads(active['metadata'])
            source=active['source_path'];preview=active['path']
            box=face['bbox'];w=row['display_width'];h=row['display_height']
            normalized=[v/(w if i%2==0 else h) for i,v in enumerate(box)]
            items.append({'person_cluster_id':row['group_id'],'visual_unit_id':row['artifact_id'],
                'derived_id':row['artifact_id'],'source_content_id':active['source_id'],
                'relative_path':meta.get('source_relative_path',''),
                'media_type':meta.get('media_type',meta.get('media_kind','image')),
                'time_position_ms':active['time_ms'],'preview_path':preview,'derived_path':preview,
                'source_path':source,'absolute_path':source,'source_online':False,'source_availability':'unchecked',
                'can_open_original':bool(source),'cluster_confidence':'review',
                'human_review_status':'unconfirmed','anonymous_display_name':'',
                'similarity_to_representative':row['similarity'],
                'person_bbox':normalized if all(0<=v<=1 for v in normalized) else None,
                '_representative':bool(row['representative'])})
        for item in items:
            group=[r for r in items if r['person_cluster_id']==item['person_cluster_id']]
            item['member_count']=len(group)
            item['distinct_source_count']=len({r['source_content_id'] for r in group})
        return items
    finally:
        c.close()


def catalog(database, offset, limit):
    rows=read_frames(database)
    if rows is None:
        return None
    groups={}
    for row in rows:
        gid=row['person_cluster_id']
        if gid not in groups or row['_representative']:
            groups[gid]=row
    items=sorted(groups.values(),key=lambda r:(-r['member_count'],r['person_cluster_id']))
    return {'total':len(items),'offset':offset,'limit':limit,'items':items[offset:offset+limit]}


def health(database):
    path=companion(database)
    if not path.is_file():
        return None
    c=readonly(path)
    try:
        counts={r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM jobs GROUP BY status')}
        faces=c.execute("SELECT COALESCE(SUM(json_array_length(result_json,'$.faces')),0) FROM jobs WHERE status='DONE'").fetchone()[0]
        groups=c.execute('SELECT count(DISTINCT group_id) FROM groups').fetchone()[0]
        pending=sum(counts.get(k,0) for k in ('PENDING','RUNNING','FAILED_RETRYABLE'))
        return {'state':'running' if pending else 'failed' if counts.get('FAILED_FINAL') else 'ready' if groups else 'empty',
            'face_count':faces,'cluster_count':groups,'visual_unit_count':sum(counts.values()),
            'pending_count':pending,'authority':CONTRACT}
    finally:
        c.close()


def results(database, identifiers, media_type, offset, limit, source_id=None, extra_ids=()):
    rows=read_frames(database)
    if rows is None:
        return None
    selected=[r for r in rows if (r['person_cluster_id'] in identifiers or r['visual_unit_id'] in extra_ids)
              and (media_type=='all' or r['media_type']==media_type)
              and (not source_id or r['source_content_id']==source_id)]
    frame_total=len({r['visual_unit_id'] for r in selected})
    source_frames={sid:len({r['visual_unit_id'] for r in selected if r['source_content_id']==sid})
                   for sid in {r['source_content_id'] for r in selected}}
    grouped={}
    for row in selected:
        grouped.setdefault(row['visual_unit_id'] if source_id else row['source_content_id'],row)
    selected=list(grouped.values());counts={}
    for row in selected:
        counts[row['media_type']]=counts.get(row['media_type'],0)+1
        row['result_level']='frame' if source_id else 'source'
        row['source_frame_count']=source_frames[row['source_content_id']]
    return {'total':len(selected),'frame_total':frame_total,'offset':offset,'limit':limit,
            'items':selected[offset:offset+limit],'count_by_media':counts,
            'next_offset':offset+limit if offset+limit<len(selected) else None}


def visual_frames(database, identifiers, source_id, media_type, offset, limit):
    """Resolve manually selected reference frames through V2 IDs, not legacy tables."""
    from .library_path_alias import resolve
    rows=[]
    for artifact in inventory(database, allow_active=True):
        if identifiers and artifact['artifact_id'] not in identifiers:
            continue
        if source_id and artifact['source_id']!=source_id:
            continue
        meta=json.loads(artifact['metadata']);kind=meta.get('media_type',meta.get('media_kind','image'))
        if media_type!='all' and kind!=media_type:
            continue
        path=artifact['source_path']
        rows.append({'visual_unit_id':artifact['artifact_id'],'derived_id':artifact['artifact_id'],
            'source_content_id':artifact['source_id'],'media_type':kind,'absolute_path':path,
            'source_path':path,'source_online':False,'source_availability':'unchecked','can_open_original':bool(path),
            'relative_path':meta.get('source_relative_path',''),'preview_path':resolve(database,artifact['path']),
            'derived_path':resolve(database,artifact['path']),'time_position_ms':artifact['time_ms'],'result_level':'frame'})
    rows.sort(key=lambda row: (row['time_position_ms'], row['visual_unit_id']))
    counts={}
    for row in rows:
        counts[row['media_type']]=counts.get(row['media_type'],0)+1
        row['source_frame_count']=len(rows) if source_id else 1
    return {'total':len(rows),'items':rows[offset:offset+limit],'count_by_media':counts,
        'offset':offset,'limit':limit,'next_offset':offset+limit if offset+limit<len(rows) else None}
