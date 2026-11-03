"""Read-only presentation of current V2 face-backed people, not body identity."""
import json
from collections import defaultdict
from pathlib import Path

from .v2_current_jobs_cache import install_readonly_snapshot_view, correlated_predicate
from .v2_people import PersonIdentity


def enabled(c):
    tables = {r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    required = {'v2_jobs', 'v2_global_people', 'v2_tracklet_membership', 'v2_tracklet_people',
                'v2_global_memberships', 'v2_person_observations', 'v2_observation_heads',
                'v2_artifacts', 'v2_source_metadata', 'v2_active_sources', 'v2_job_dependencies'}
    return required <= tables and bool(c.execute("SELECT 1 FROM v2_jobs WHERE stage='insightface' LIMIT 1").fetchone())


def frames(c, *, targeted_lineage=False, links_only=False):
    from .library_path_alias import resolve
    database = c.execute('PRAGMA database_list').fetchone()[2]
    if not c.in_transaction:
        c.execute('BEGIN')
    # Catalog/health callers also use the transaction-local view afterwards.
    # Only search-result decoration opts into bounded root-relative checking.
    if targeted_lineage:
        current_face_sql = ("SELECT 1 FROM main.v2_jobs j WHERE j.execution_key=? AND j.status='DONE' AND "
                            + correlated_predicate(c))
    else:
        install_readonly_snapshot_view(c)
        current_face_sql = "SELECT 1 FROM v2_current_jobs WHERE execution_key=? AND status='DONE'"
    identity = object.__new__(PersonIdentity)
    columns = ("o.artifact_id,o.source_id,o.face_evidence,o.provenance,g.global_person_id,p.display_name,o.time_ms,o.observation_id"
               if links_only else "o.*,g.global_person_id,g.authority,a.path AS derived_path,"
               "s.path AS absolute_path,s.metadata,p.display_name")
    rows = c.execute('SELECT ' + columns + '''
        FROM v2_person_observations o
        JOIN v2_observation_heads h ON h.observation_id=o.observation_id
        JOIN v2_active_sources active ON active.source_id=o.source_id AND active.source_revision=o.source_revision
        JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
          AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
        JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
          AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)
        JOIN v2_global_memberships g ON g.source_person_id=t.source_person_id
          AND g.version=(SELECT MAX(version) FROM v2_global_memberships WHERE source_person_id=t.source_person_id)
        JOIN v2_global_people p ON p.global_person_id=g.global_person_id
        JOIN v2_artifacts a ON a.artifact_id=o.artifact_id
        JOIN v2_source_metadata s ON s.source_id=o.source_id AND s.source_revision=o.source_revision
        ''' + ("WHERE o.face_evidence IS NOT '[]' " if links_only else '') +
        '')
    # Sort the already validated lightweight presentation rows, not wide JSON
    # evidence in SQLite TEMP. Preserve SQLite BINARY ASC type/null ordering.
    def sqlite_order(value):
        if value is None:return (0,0)
        if isinstance(value,(int,float)):return (1,value)
        if isinstance(value,str):return (2,value)
        return (3,bytes(value))
    result=[];order_keys={}
    for row in rows:
        order_key=(sqlite_order(row['time_ms']),sqlite_order(row['observation_id']))
        faces=json.loads(row['face_evidence'])
        if not faces:
            continue
        provenance=json.loads(row['provenance'])
        key=provenance.get('face_execution_key')
        if not key or not c.execute(current_face_sql,(key,)).fetchone():
            continue
        if links_only:
            # Search decorations require identity and global membership counts,
            # not preview I/O, bounding boxes or source presentation metadata.
            # Keep every current-lineage check and every member for counts below.
            result.append({'person_cluster_id':identity.canonical(c,row['global_person_id']),
                'visual_unit_id':row['artifact_id'], 'source_content_id':row['source_id'],
                'cluster_confidence':'review', 'human_review_status':'unconfirmed',
                'anonymous_display_name':row['display_name'] or ''})
            order_keys[id(result[-1])]=order_key
            continue
        meta=json.loads(row['metadata']);source=str(row['absolute_path'] or '')
        preview=resolve(database,str(row['derived_path'] or ''))
        result.append({'person_cluster_id':identity.canonical(c,row['global_person_id']),
            'visual_unit_id':row['artifact_id'],'derived_id':row['artifact_id'],
            'source_content_id':row['source_id'],'relative_path':meta.get('source_relative_path',''),
            'media_type':meta.get('media_type',meta.get('media_kind','image')),
            'time_position_ms':row['time_ms'],'preview_path':preview if Path(preview).is_file() else '',
            'derived_path':preview,'source_path':source,'absolute_path':source,
            'source_online':False,'source_availability':'unchecked',
            'can_open_original':bool(source),
            'cluster_confidence':'review','human_review_status':'unconfirmed',
            'anonymous_display_name':row['display_name'] or '',
            'similarity_to_representative':float(row['confidence']),
            'person_bbox':json.loads(row['bbox']), 'face_count':len(faces)})
        order_keys[id(result[-1])]=order_key
    result.sort(key=lambda item:order_keys[id(item)])
    groups=defaultdict(list)
    for row in result:groups[row['person_cluster_id']].append(row)
    for group in groups.values():
        count=len({r['visual_unit_id'] for r in group})
        sources=len({r['source_content_id'] for r in group})
        for row in group:row.update(member_count=count,distinct_source_count=sources)
    return result


def catalog(c,offset,limit):
    rows=frames(c);groups={}
    for row in rows:groups.setdefault(row['person_cluster_id'],row)
    items=sorted(groups.values(),key=lambda r:(-r['member_count'],r['person_cluster_id']))
    return {'total':len(items),'offset':offset,'limit':limit,'items':items[offset:offset+limit]}


def results(c,cluster_ids,media_type,offset,limit,source_id=None):
    selected=[r for r in frames(c) if r['person_cluster_id'] in cluster_ids and
              (media_type=='all' or r['media_type']==media_type) and
              (not source_id or r['source_content_id']==source_id)]
    frame_total=len(selected)
    if not source_id:
        grouped={}
        source_counts=defaultdict(set)
        for row in selected:
            source_counts[row['source_content_id']].add(row['visual_unit_id'])
            grouped.setdefault(row['source_content_id'],row)
        selected=list(grouped.values())
        for row in selected:
            row['source_frame_count']=len(source_counts[row['source_content_id']])
    counts=defaultdict(int)
    for row in selected:
        counts[row['media_type']]+=1
        row['result_level']='frame' if source_id else 'source'
    return {'total':len(selected),'frame_total':frame_total,'offset':offset,'limit':limit,
            'items':selected[offset:offset+limit], 'count_by_media':dict(counts),
            'next_offset':offset+limit if offset+limit<len(selected) else None}
