"""Conservative person graph and append-only manual identity decisions.

Bbox association is track continuity, never person identity. Cross-source auto
merge requires repeated high-quality compatible face evidence. Body/clothes do
not trigger auto merge. Existing buffalo_l results can be imported unchanged.
"""
from __future__ import annotations
import json
import math
import time
import uuid
from dataclasses import asdict,dataclass
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .person_track_suggestions import PersonDetection,LinkerConfig,build_tracklets

@dataclass(frozen=True)
class TrackingViewPolicy:
    enabled:bool=False
    fps:float|None=None
    long_edge:int|None=None
    buffer_root:str|None=None
    ttl_seconds:int|None=None

    def validate(self):
        if self.enabled and (self.fps is None or not 1<=self.fps<=3 or not self.long_edge or self.long_edge<=0 or not self.buffer_root or not self.ttl_seconds or self.ttl_seconds<=0):
            raise ContractError('tracking_view_requires_benchmark_config')
        return {**asdict(self),'artifact_type':'EPHEMERAL_TRACKING_VIEW','persist_media':False,
                'allowed_consumers':['yoloe_person','tracker','insightface'],'qwen':False,'openclip':False}

def cosine(a,b):
    if len(a)!=len(b) or not a:return None
    denom=math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/denom if denom else None


class ReconciliationChanged(ContractError):
    """A concurrent edit invalidated a read-only reconciliation proposal."""

class PersonIdentity:
    def __init__(self,store):
        self.store=store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS v2_person_observations(observation_id TEXT PRIMARY KEY,
              source_id TEXT,source_revision TEXT,artifact_id TEXT,time_ms INTEGER,bbox TEXT,
              confidence REAL,face_evidence TEXT,provenance TEXT,revision INTEGER,evidence_key TEXT);
            CREATE INDEX IF NOT EXISTS v2_people_source ON v2_person_observations(source_id,source_revision,time_ms);
            CREATE TABLE IF NOT EXISTS v2_observation_heads(artifact_id TEXT,detection_slot TEXT,
              observation_id TEXT,PRIMARY KEY(artifact_id,detection_slot));
            CREATE TABLE IF NOT EXISTS v2_tracklets(tracklet_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
              tracker_hash TEXT,version INTEGER,status TEXT);
            CREATE TABLE IF NOT EXISTS v2_tracklet_membership(observation_id TEXT,version INTEGER,tracklet_id TEXT,
              authority TEXT,evidence_digest TEXT,PRIMARY KEY(observation_id,version));
            CREATE TABLE IF NOT EXISTS v2_source_people(source_person_id TEXT PRIMARY KEY,source_id TEXT,
              source_revision TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS v2_source_face_anchors(source_id TEXT,source_revision TEXT,cluster_id TEXT,
              source_person_id TEXT,PRIMARY KEY(source_id,source_revision,cluster_id));
            CREATE TABLE IF NOT EXISTS v2_tracklet_people(tracklet_id TEXT,version INTEGER,source_person_id TEXT,
              authority TEXT,PRIMARY KEY(tracklet_id,version));
            CREATE TABLE IF NOT EXISTS v2_global_people(global_person_id TEXT PRIMARY KEY,display_name TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS v2_global_memberships(source_person_id TEXT,version INTEGER,global_person_id TEXT,
              authority TEXT,PRIMARY KEY(source_person_id,version));
            CREATE TABLE IF NOT EXISTS v2_person_aliases(global_person_id TEXT,version INTEGER,canonical_global_person_id TEXT,
              authority TEXT,decision_id TEXT,PRIMARY KEY(global_person_id,version));
            CREATE TABLE IF NOT EXISTS v2_person_decisions(decision_id TEXT PRIMARY KEY,action TEXT,left_id TEXT,
              right_id TEXT,payload TEXT,actor TEXT,created_at REAL);
            CREATE TABLE IF NOT EXISTS v2_person_constraints(left_source_person TEXT,right_source_person TEXT,
              decision_id TEXT,active INTEGER,PRIMARY KEY(left_source_person,right_source_person,decision_id));
            CREATE TABLE IF NOT EXISTS v2_person_suggestions(left_id TEXT,right_id TEXT,status TEXT,evidence TEXT,
              PRIMARY KEY(left_id,right_id));
            CREATE TABLE IF NOT EXISTS v2_manual_observation_authority(logical_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,artifact_id TEXT,bbox TEXT,tracklet_id TEXT,source_person_id TEXT,decision_id TEXT);
            CREATE TABLE IF NOT EXISTS v2_person_review_observations(observation_id TEXT PRIMARY KEY,reason TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS v2_person_decision_results(decision_id TEXT PRIMARY KEY,result_id TEXT);
            ''')

    def observation(self,c,source,revision,artifact,time_ms,bbox,confidence,face_evidence,provenance,evidence_revision,evidence_key,detection_slot=None):
        if len(bbox)!=4 or not all(math.isfinite(v) and 0<=v<=1 for v in bbox) or bbox[2]<=bbox[0] or bbox[3]<=bbox[1]:raise ContractError('person_normalized_bbox_invalid')
        if not provenance or not evidence_key:raise ContractError('person_lineage_required')
        oid=fingerprint((source,revision,artifact,time_ms,bbox,evidence_key,evidence_revision))
        values=(source,revision,artifact,time_ms,json.dumps(bbox),confidence,json.dumps(face_evidence,sort_keys=True),json.dumps(provenance,sort_keys=True),evidence_revision,evidence_key)
        old=c.execute('SELECT * FROM v2_person_observations WHERE observation_id=?',(oid,)).fetchone()
        if old and tuple(old[k] for k in ('source_id','source_revision','artifact_id','time_ms','bbox','confidence','face_evidence','provenance','revision','evidence_key'))!=values:raise ContractError('observation_digest_conflict')
        c.execute('INSERT OR IGNORE INTO v2_person_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',(oid,*values))
        logical=fingerprint((source,revision,artifact,bbox))
        authority=c.execute('SELECT * FROM v2_manual_observation_authority WHERE logical_id=?',(logical,)).fetchone()
        c.execute('INSERT OR REPLACE INTO v2_observation_heads VALUES(?,?,?)',(artifact,fingerprint(bbox),oid))
        ambiguous=provenance.get('logical_mapping_ambiguous',False)
        if authority and not ambiguous:
            member=c.execute('SELECT * FROM v2_tracklet_membership WHERE observation_id=? ORDER BY version DESC LIMIT 1',(oid,)).fetchone()
            if not member or member['authority']!='MANUAL' or member['tracklet_id']!=authority['tracklet_id']:
                self.append(c,'v2_tracklet_membership','observation_id',oid,(authority['tracklet_id'],'MANUAL',fingerprint(authority['decision_id'])))
            self.publish_links(c,{authority['source_person_id']})
        elif c.execute('SELECT 1 FROM v2_manual_observation_authority WHERE artifact_id=?',(artifact,)).fetchone():
            c.execute("INSERT OR IGNORE INTO v2_person_review_observations VALUES(?,'manual_lineage_mapping_uncertain','REVIEW')",(oid,))
        return oid

    def observe(self,**args):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');return self.observation(c,**args)

    def canonical(self,c,gid):
        seen=set()
        while True:
            if gid in seen:raise ContractError('person_alias_cycle')
            seen.add(gid)
            row=c.execute('SELECT canonical_global_person_id FROM v2_person_aliases WHERE global_person_id=? ORDER BY version DESC LIMIT 1',(gid,)).fetchone()
            if not row or row[0]==gid:return gid
            gid=row[0]

    def append(self,c,table,id_column,identifier,fields):
        version=c.execute(f'SELECT COALESCE(MAX(version),0)+1 FROM {table} WHERE {id_column}=?',(identifier,)).fetchone()[0]
        c.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in range(len(fields)+2))})',(identifier,version,*fields))
        return version

    def ensure_person(self,c,person,source,revision):
        c.execute("INSERT OR IGNORE INTO v2_source_people VALUES(?,?,?,'UNKNOWN')",(person,source,revision))
        row=c.execute('SELECT * FROM v2_global_memberships WHERE source_person_id=? ORDER BY version DESC LIMIT 1',(person,)).fetchone()
        if row:return self.canonical(c,row['global_person_id'])
        gid='person_'+uuid.uuid4().hex
        c.execute("INSERT INTO v2_global_people VALUES(?,'','SEPARATE')",(gid,))
        self.append(c,'v2_global_memberships','source_person_id',person,(gid,'AUTO'))
        return gid

    def build_source(self,source,revision,config=LinkerConfig()):
        tracker_hash=fingerprint(('existing-face-anchored-bbox-tracker-v1',asdict(config)))
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            observations=list(c.execute('''SELECT o.*,h.detection_slot FROM v2_person_observations o JOIN v2_observation_heads h ON h.observation_id=o.observation_id
              WHERE o.source_id=? AND o.source_revision=? AND NOT EXISTS(SELECT 1 FROM v2_person_review_observations review WHERE review.observation_id=o.observation_id AND review.status='REVIEW') ORDER BY o.time_ms,o.bbox,o.observation_id''',(source,revision)))
            detections=[PersonDetection(row['observation_id'],row['artifact_id'],source,row['time_ms'],tuple(json.loads(row['bbox'])),row['confidence'],
                tuple(sorted({f['cluster_id'] for f in json.loads(row['face_evidence']) if f.get('cluster_id') and f.get('cluster_confidence')=='high'}))) for row in observations]
            result=build_tracklets(detections,config);by_id={r['observation_id']:r for r in observations}
            for track in result.tracklets:
                first=by_id[track.detections[0].detection_id]
                tid=fingerprint((source,revision,tracker_hash,first['artifact_id'],first['detection_slot']))
                c.execute("INSERT OR IGNORE INTO v2_tracklets VALUES(?,?,?,?,1,?)",(tid,source,revision,tracker_hash,track.identity_status))
                current=c.execute('SELECT * FROM v2_tracklet_people WHERE tracklet_id=? ORDER BY version DESC LIMIT 1',(tid,)).fetchone()
                person=current['source_person_id'] if current else fingerprint((source,revision,tid))
                self.ensure_person(c,person,source,revision)
                if track.identity_status=='FACE_ANCHORED_TRACK' and (not current or current['authority']!='MANUAL'):
                    cluster=next(iter(track.face_cluster_ids))
                    anchor=c.execute('SELECT source_person_id FROM v2_source_face_anchors WHERE source_id=? AND source_revision=? AND cluster_id=?',(source,revision,cluster)).fetchone()
                    if anchor and not self.blocked(c,self.person_global(c,person),self.person_global(c,anchor[0])):person=anchor[0]
                    elif not anchor:c.execute('INSERT INTO v2_source_face_anchors VALUES(?,?,?,?)',(source,revision,cluster,person))
                self.ensure_person(c,person,source,revision)
                c.execute('UPDATE v2_source_people SET status=? WHERE source_person_id=?',(track.identity_status,person))
                c.execute('UPDATE v2_tracklets SET status=?,version=version+CASE WHEN status<>? THEN 1 ELSE 0 END WHERE tracklet_id=?',(track.identity_status,track.identity_status,tid))
                if not current or (current['authority']!='MANUAL' and current['source_person_id']!=person):self.append(c,'v2_tracklet_people','tracklet_id',tid,(person,'AUTO'))
                if current and current['authority']=='MANUAL':person=current['source_person_id']
                for detection in track.detections:
                    member=c.execute('SELECT * FROM v2_tracklet_membership WHERE observation_id=? ORDER BY version DESC LIMIT 1',(detection.detection_id,)).fetchone()
                    if member and member['authority']=='MANUAL':continue
                    if not member or member['tracklet_id']!=tid:self.append(c,'v2_tracklet_membership','observation_id',detection.detection_id,(tid,'AUTO',fingerprint(asdict(detection))))
                    row=by_id[detection.detection_id]
                    self.store.emit(c,'PERSON_LINK_UPDATED',source,revision,row['revision'],{'observation_id':detection.detection_id,'artifact_id':row['artifact_id'],'time_ms':row['time_ms'],'source_person_id':person,'global_person_id':self.person_global(c,person),'tracklet_id':tid,'identity_status':track.identity_status})
        return result

    def person_global(self,c,person):
        row=c.execute('SELECT global_person_id FROM v2_global_memberships WHERE source_person_id=? ORDER BY version DESC LIMIT 1',(person,)).fetchone()
        if not row:raise ContractError('source_person_missing')
        return self.canonical(c,row[0])

    def relationship_snapshot(self,c):
        """Transaction-local latest relationships; never cache across writes.

        Bulk reads replace one membership + alias SELECT per person/group.
        The caller owns the transaction, including visibility after a merge.
        """
        aliases=dict(c.execute('''SELECT a.global_person_id,a.canonical_global_person_id
          FROM v2_person_aliases a WHERE a.version=(SELECT MAX(version)
          FROM v2_person_aliases WHERE global_person_id=a.global_person_id)'''))
        latest=dict(c.execute('''SELECT m.source_person_id,m.global_person_id
          FROM v2_global_memberships m WHERE m.version=(SELECT MAX(version)
          FROM v2_global_memberships WHERE source_person_id=m.source_person_id)'''))
        resolved={}
        def canonical(gid):
            chain=[];seen=set();current=gid
            while current not in resolved:
                if current in seen:raise ContractError('person_alias_cycle')
                seen.add(current);chain.append(current)
                target=aliases.get(current)
                if target is None or target==current:break
                current=target
            result=resolved.get(current,current)
            for item in chain:resolved[item]=result
            return result
        groups={};people={}
        for row in c.execute('SELECT source_person_id FROM v2_source_people'):
            person=row[0]
            if person not in latest:raise ContractError('source_person_missing')
            gid=canonical(latest[person]);people[person]=gid
            groups.setdefault(gid,set()).add(person)
        return canonical,people,groups

    def members(self,c,gid):
        canonical,_,groups=self.relationship_snapshot(c)
        return groups.get(canonical(gid),set())

    def blocked(self,c,left,right):
        a,b=self.members(c,left),self.members(c,right)
        return any((row[0] in a and row[1] in b) or (row[1] in a and row[0] in b) for row in c.execute('SELECT left_source_person,right_source_person FROM v2_person_constraints WHERE active=1'))

    def log(self,c,action,left,right,payload,actor,decision_id):
        serialized=json.dumps(payload,sort_keys=True)
        old=c.execute('SELECT * FROM v2_person_decisions WHERE decision_id=?',(decision_id,)).fetchone()
        if old:
            if tuple(old[k] for k in ('action','left_id','right_id','payload','actor'))!=(action,left,right or '',serialized,actor):raise ContractError('manual_decision_id_conflict')
            return False
        # Observation-level SPLIT decisions have no right-hand person.  The
        # migrated product schema records that absent relationship as empty
        # provenance rather than violating its non-null compatibility column.
        c.execute('INSERT INTO v2_person_decisions VALUES(?,?,?,?,?,?,?)',(decision_id,action,left,right or '',serialized,actor,time.time()))
        return True

    def constrain(self,c,a,b,decision):
        for left in sorted(a):
            for right in sorted(b):
                if left==right:raise ContractError('cannot_link_self_requires_split')
                pair=sorted((left,right));c.execute('INSERT OR IGNORE INTO v2_person_constraints VALUES(?,?,?,1)',(*pair,decision))

    def freeze_manual_authority(self,c,people,decision_id):
        """Pin logical detections, not ephemeral model execution/array positions."""
        rows=list(c.execute("""SELECT o.*,m.tracklet_id,t.source_person_id FROM v2_person_observations o
          JOIN v2_observation_heads h ON h.observation_id=o.observation_id
          JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
          JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
          WHERE m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
          AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)"""))
        for row in rows:
            if row['source_person_id'] not in people:continue
            logical=fingerprint((row['source_id'],row['source_revision'],row['artifact_id'],json.loads(row['bbox'])))
            c.execute('INSERT OR REPLACE INTO v2_manual_observation_authority VALUES(?,?,?,?,?,?,?,?)',
                (logical,row['source_id'],row['source_revision'],row['artifact_id'],row['bbox'],row['tracklet_id'],row['source_person_id'],decision_id))
            member=c.execute('SELECT authority FROM v2_tracklet_membership WHERE observation_id=? ORDER BY version DESC LIMIT 1',(row['observation_id'],)).fetchone()
            if member[0]!='MANUAL':self.append(c,'v2_tracklet_membership','observation_id',row['observation_id'],(row['tracklet_id'],'MANUAL',fingerprint(decision_id)))
            track=c.execute('SELECT authority FROM v2_tracklet_people WHERE tracklet_id=? ORDER BY version DESC LIMIT 1',(row['tracklet_id'],)).fetchone()
            if track[0]!='MANUAL':self.append(c,'v2_tracklet_people','tracklet_id',row['tracklet_id'],(row['source_person_id'],'MANUAL'))

    def decide(self,action,left,right=None,*,actor,decision_id,selected_people=None):
        if action not in ('MERGE','NOT_SAME','SPLIT','DEFER') or not actor:raise ContractError('manual_action_invalid')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            payload={'selected_people':sorted(selected_people or [])}
            if not self.log(c,action,left,right,payload,actor,decision_id):
                return c.execute('SELECT result_id FROM v2_person_decision_results WHERE decision_id=?',(decision_id,)).fetchone()[0]
            a=self.canonical(c,left)
            if not c.execute('SELECT 1 FROM v2_global_people WHERE global_person_id=?',(a,)).fetchone():raise ContractError('global_person_missing')
            if action=='SPLIT':
                selected=set(selected_people or []);whole=self.members(c,a)
                if not selected or not selected<whole:raise ContractError('split_requires_proper_partition')
                result='person_'+fingerprint(('manual-split',decision_id))[:32]
                c.execute("INSERT INTO v2_global_people VALUES(?,'','MANUAL')",(result,))
                for person in sorted(selected):self.append(c,'v2_global_memberships','source_person_id',person,(result,'MANUAL'))
                self.constrain(c,selected,whole-selected,decision_id)
            else:
                b=self.canonical(c,right)
                if not c.execute('SELECT 1 FROM v2_global_people WHERE global_person_id=?',(b,)).fetchone():raise ContractError('global_person_missing')
                result=a
                if action=='MERGE':
                    if self.blocked(c,a,b):raise ContractError('manual_cannot_link_conflict_requires_explicit_revocation')
                    if a!=b:self.append(c,'v2_person_aliases','global_person_id',b,(a,'MANUAL',decision_id))
                elif action=='NOT_SAME':self.constrain(c,self.members(c,a),self.members(c,b),decision_id)
            c.execute('INSERT OR REPLACE INTO v2_person_suggestions VALUES(?,?,?,?)',(left,right or left,action,json.dumps({'decision_id':decision_id,'result':result},sort_keys=True)))
            c.execute('INSERT INTO v2_person_decision_results VALUES(?,?)',(decision_id,result))
            # Identity membership/alias changes are re-materialized incrementally;
            # no observation or raw model result is rewritten.
            affected=self.members(c,result)|self.members(c,a)
            if right is not None:affected|=self.members(c,right)
            self.freeze_manual_authority(c,affected,decision_id)
            self.publish_links(c,affected)
            return result

    def publish_links(self,c,people):
        for person in sorted(people):
            rows=c.execute('''SELECT o.*,m.tracklet_id FROM v2_person_observations o
              JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
              JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
              WHERE EXISTS(SELECT 1 FROM v2_observation_heads h WHERE h.observation_id=o.observation_id)
              AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
              AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)
              AND t.source_person_id=?''',(person,))
            for row in rows:
                self.store.emit(c,'PERSON_LINK_UPDATED',row['source_id'],row['source_revision'],row['revision'],{'observation_id':row['observation_id'],'artifact_id':row['artifact_id'],'time_ms':row['time_ms'],'source_person_id':person,'global_person_id':self.person_global(c,person),'tracklet_id':row['tracklet_id'],'identity_authority':'MANUAL_OR_RECONCILED'})

    def face_records(self,c,people):
        records=[]
        for person in sorted(people):
            rows=c.execute('''SELECT o.* FROM v2_person_observations o
              JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
              JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
              WHERE EXISTS(SELECT 1 FROM v2_observation_heads h WHERE h.observation_id=o.observation_id)
              AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
              AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)
              AND t.source_person_id=?''',(person,))
            for row in rows:
                active = c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?', (row['source_id'],)).fetchone()
                if active and active[0] != row['source_revision']:
                    continue
                provenance = json.loads(row['provenance'])
                face_key = provenance.get('face_execution_key')
                if face_key and not c.execute("SELECT 1 FROM v2_current_jobs WHERE execution_key=? AND status='DONE'", (face_key,)).fetchone():
                    continue
                for face in json.loads(row['face_evidence']):records.append({**face,'artifact_id':row['artifact_id'],'source_id':row['source_id']})
        return records

    def face_contracts_by_person(self,c,*,yield_hook=None):
        """One transaction-local evidence read for reconciliation prefilter.

        Only model-contract membership is needed here. Final reconciliation
        still reads its original ordered evidence and applies all policies.
        No person decisions or evidence are cached across transactions.
        """
        contracts={};active=dict(c.execute('SELECT source_id,source_revision FROM v2_active_sources'))
        valid_faces={};last_yield=time.monotonic()
        for row in c.execute('''SELECT o.source_id,o.source_revision,o.provenance,o.face_evidence,t.source_person_id
          FROM v2_person_observations o
          JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
          JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
          WHERE EXISTS(SELECT 1 FROM v2_observation_heads h WHERE h.observation_id=o.observation_id)
          AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
          AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)'''):
            # This is a deferred read snapshot, not a write transaction.
            # Interrupting its plan is safe; no partial identity is published.
            if yield_hook is not None and time.monotonic()-last_yield>=0.1:
                yield_hook();last_yield=time.monotonic()
            if row['source_id'] in active and active[row['source_id']]!=row['source_revision']:continue
            key=json.loads(row['provenance']).get('face_execution_key')
            if key:
                if key not in valid_faces:
                    valid_faces[key]=bool(c.execute("SELECT 1 FROM v2_current_jobs WHERE execution_key=? AND status='DONE'",(key,)).fetchone())
                if not valid_faces[key]:continue
            contracts.setdefault(row['source_person_id'],set()).update(face.get('model_contract') for face in json.loads(row['face_evidence']))
        return contracts

    def observation_snapshot(self,c,people,*,yield_hook=None):
        """Reconcile-local evidence, captured in the caller's read transaction.

        The legacy query scans membership row order for each person. Preserve
        that order explicitly while reading all required people in one join.
        No snapshot
        survives this reconcile_source invocation.
        """
        people=sorted(people);records={p:[] for p in people}
        active=dict(c.execute('SELECT source_id,source_revision FROM v2_active_sources'))
        # Match face_records even for imported evidence whose key is not an
        # InsightFace-stage job. Validation is keyed, not stage-assumed.
        keys=set();valid=set()
        rows=[]
        last_yield=time.monotonic()
        for row in c.execute('''SELECT t.source_person_id AS snapshot_person,o.* FROM v2_person_observations o
              JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
              JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
              WHERE EXISTS(SELECT 1 FROM v2_observation_heads h WHERE h.observation_id=o.observation_id)
              AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
              AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)
              AND t.source_person_id IN (SELECT value FROM json_each(?))
              ORDER BY t.source_person_id,m.rowid''',(json.dumps(people),)):
            if yield_hook is not None and time.monotonic()-last_yield>=.1:
                yield_hook();last_yield=time.monotonic()
            data=dict(row);person=row['snapshot_person']
            if data['source_id'] in active and active[data['source_id']]!=data['source_revision']:continue
            key=json.loads(data['provenance']).get('face_execution_key')
            if key:keys.add(key)
            rows.append((person,data,key))
        if keys:
            valid={r[0] for r in c.execute("SELECT execution_key FROM v2_current_jobs WHERE status='DONE' AND execution_key IN (SELECT value FROM json_each(?))",(json.dumps(sorted(keys)),))}
        for person,row,key in rows:
            if key and key not in valid:continue
            records[person].extend({**face,'artifact_id':row['artifact_id'],'source_id':row['source_id']} for face in json.loads(row['face_evidence']))
        proof_rows,valid_faces=self.reconciliation_evidence(c,people)
        by_person={p:[] for p in people}
        for row in proof_rows:by_person[row['source_person_id']].append(tuple(row))
        return {'records':records,'rows':by_person,'valid_faces':dict(valid_faces)}

    def reconciliation_evidence(self,c,members):
        """Small revision/root proof shared by capture and final validation."""
        import hashlib
        c.create_function('_person_evidence_sha',1,lambda text:hashlib.sha256((text or '').encode()).hexdigest(),deterministic=True)
        rows=list(c.execute('''SELECT o.observation_id,o.evidence_key,o.source_id,o.source_revision,
            active.source_revision,m.tracklet_id,m.version,t.source_person_id,t.version,
            o.provenance,o.bbox,o.confidence,o.revision,_person_evidence_sha(o.face_evidence),
            json_extract(o.provenance,'$.face_execution_key') AS face_key
            FROM v2_person_observations o
            JOIN v2_tracklet_membership m ON m.observation_id=o.observation_id
            JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
            LEFT JOIN v2_active_sources active ON active.source_id=o.source_id
            WHERE EXISTS(SELECT 1 FROM v2_observation_heads h WHERE h.observation_id=o.observation_id)
            AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=o.observation_id)
            AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)
            AND t.source_person_id IN (SELECT value FROM json_each(?)) ORDER BY o.observation_id,t.source_person_id''',(json.dumps(sorted(members)),)))
        keys=sorted({r['face_key'] for r in rows if r['face_key']})
        from .v2_source_lineage import current_filtered_rows
        valid=current_filtered_rows(c,"j.execution_key IN (SELECT value FROM json_each(?)) AND j.status='DONE'",(json.dumps(keys),),columns='j.execution_key,j.status,j.result_sha') if keys else []
        strict={r['execution_key']:r['result_sha'] for r in valid}
        raw={r['execution_key']:(r['status'],r['result_sha'],strict.get(r['execution_key'])) for r in c.execute(
            'SELECT execution_key,status,result_sha FROM v2_current_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',
            (json.dumps(keys),))} if keys else {}
        return rows,sorted((key,raw.get(key)) for key in keys)

    def reconciliation_proof(self,c,left,right,*,observation_snapshot=None):
        """Narrow immutable-evidence proof, not another face comparison.

        Hash vector bytes in SQLite without materializing/parsing them in the
        writer. Actual face filtering and score computation remain unchanged.
        All manual relationships and root validity are rechecked at commit.
        """
        canonical,_,groups=self.relationship_snapshot(c)
        a,b=canonical(left),canonical(right)
        members=sorted(groups.get(a,set())|groups.get(b,set()))
        rows,valid=self.reconciliation_evidence(c,members)
        if observation_snapshot is not None:
            if any(p not in observation_snapshot['rows'] for p in members):
                raise ReconciliationChanged('person_reconciliation_snapshot_changed')
            expected=sorted((r for p in members for r in observation_snapshot['rows'][p]),key=lambda r:(r[0],r[7]))
            keys={r['face_key'] for r in rows if r['face_key']}
            expected_valid=sorted((key,value) for key,value in observation_snapshot['valid_faces'].items() if key in keys)
            if [tuple(r) for r in rows]!=expected or valid!=expected_valid:
                raise ReconciliationChanged('person_reconciliation_snapshot_changed')
        return fingerprint({'pair':(a,b),'observations':[tuple(r) for r in rows],
            'valid_faces':valid,
            'aliases':[tuple(r) for r in c.execute('SELECT * FROM v2_person_aliases ORDER BY global_person_id,version')],
            'memberships':[tuple(r) for r in c.execute('SELECT * FROM v2_global_memberships ORDER BY source_person_id,version')],
            'decisions':[tuple(r) for r in c.execute('SELECT * FROM v2_person_decisions ORDER BY decision_id')],
            'constraints':[tuple(r) for r in c.execute('SELECT * FROM v2_person_constraints ORDER BY left_source_person,right_source_person,decision_id')]})

    def reconcile(self,left,right,policy,*,observation_snapshot=None):
        required={'quality_min','cosine_min','review_cosine_min','minimum_observations','calibrated','model_contract'}
        if not required<=set(policy) or policy['minimum_observations']<2:raise ContractError('identity_policy_incomplete')
        with self.store.connect() as c:
            c.execute('BEGIN');a,b=self.canonical(c,left),self.canonical(c,right)
            if a==b:
                manual=c.execute("SELECT 1 FROM v2_person_aliases WHERE authority='MANUAL' AND canonical_global_person_id=?",(a,)).fetchone()
                return 'MANUAL_CONFIRMED' if manual else 'AUTO_CONFIRMED'
            if self.blocked(c,a,b):return 'SEPARATE_MANUAL'
            for manual in c.execute("SELECT * FROM v2_person_decisions WHERE action='DEFER'"):
                if {self.canonical(c,manual['left_id']),self.canonical(c,manual['right_id'])}=={a,b}:return 'DEFER_MANUAL'
            if observation_snapshot is None:
                ar=self.face_records(c,self.members(c,a));br=self.face_records(c,self.members(c,b))
            else:
                _,_,groups=self.relationship_snapshot(c)
                def records(gid):
                    return [r for p in sorted(groups.get(gid,set())) for r in observation_snapshot['records'].get(p,())]
                ar,br=records(a),records(b)
            proof=self.reconciliation_proof(c,left,right,observation_snapshot=observation_snapshot)
        # No SQLite transaction or writer lock during N x M comparisons.
        def eligible(rows):return [r for r in rows if r.get('quality',0)>=policy['quality_min'] and r.get('model_contract')==policy['model_contract'] and r.get('vector')]
        ar,br=eligible(ar),eligible(br)
        shared_frames={r['artifact_id'] for r in ar}&{r['artifact_id'] for r in br}
        pairs=[cosine(x['vector'],y['vector']) for x in ar for y in br]
        valid=[s for s in pairs if s is not None]
        repeated=min(len({r['artifact_id'] for r in ar}),len({r['artifact_id'] for r in br}))>=policy['minimum_observations']
        same_source=bool(ar and br and {r['source_id'] for r in ar}=={r['source_id'] for r in br} and len({r['source_id'] for r in ar})==1)
        if policy.get('allow_source_face_continuity') and same_source:
            # Preserve the original sparse-frame continuity decision.
            repeated=len({r['artifact_id'] for r in ar+br})>=policy['minimum_observations']
        strong=valid and min(valid)>=policy['cosine_min']
        auto=bool(policy['calibrated'] and repeated and strong and not shared_frames)
        status='SEPARATE_SAME_FRAME' if shared_frames else 'AUTO_CONFIRMED' if auto else 'POSSIBLE_MATCH' if valid and max(valid)>=policy['review_cosine_min'] else 'UNKNOWN'
        evidence={'scores':valid,'repeated':repeated,'same_frame_conflict':bool(shared_frames),'policy':policy}
        serialized=json.dumps(evidence,sort_keys=True)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.reconciliation_proof(c,left,right)!=proof:
                raise ReconciliationChanged('person_reconciliation_snapshot_changed')
            c.execute('INSERT OR REPLACE INTO v2_person_suggestions VALUES(?,?,?,?)',(a,b,status,serialized))
            if auto:
                winner,alias=sorted((a,b))
                decision=fingerprint(('auto-face',winner,alias,evidence))
                self.append(c,'v2_person_aliases','global_person_id',alias,(winner,'AUTO',decision))
                self.publish_links(c,self.members(c,winner))
            return status

    def split_observations(self,person,observation_ids,*,actor,decision_id):
        """Manual split inside a tracklet: version memberships, don't rewrite raw."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM v2_person_decisions WHERE decision_id=?',(decision_id,)).fetchone()
            if old:
                self.log(c,'SPLIT_OBSERVATIONS',person,None,{'observations':sorted(set(observation_ids))},actor,decision_id)
                return fingerprint(('manual-source-person',decision_id))
            whole={r['observation_id'] for r in c.execute('''SELECT m.observation_id FROM v2_tracklet_membership m JOIN v2_tracklet_people t ON t.tracklet_id=m.tracklet_id
              WHERE t.source_person_id=? AND m.version=(SELECT MAX(version) FROM v2_tracklet_membership WHERE observation_id=m.observation_id)
              AND t.version=(SELECT MAX(version) FROM v2_tracklet_people WHERE tracklet_id=m.tracklet_id)''',(person,))}
            selected=set(observation_ids)
            if not selected or not selected<whole:raise ContractError('observation_split_requires_proper_partition')
            if not self.log(c,'SPLIT_OBSERVATIONS',person,None,{'observations':sorted(selected)},actor,decision_id):return fingerprint(('manual-source-person',decision_id))
            row=c.execute('SELECT * FROM v2_source_people WHERE source_person_id=?',(person,)).fetchone()
            new=fingerprint(('manual-source-person',decision_id));tid=fingerprint(('manual-tracklet',decision_id))
            self.ensure_person(c,new,row['source_id'],row['source_revision'])
            c.execute("INSERT INTO v2_tracklets VALUES(?,?,?,'manual',1,'MANUAL')",(tid,row['source_id'],row['source_revision']))
            self.append(c,'v2_tracklet_people','tracklet_id',tid,(new,'MANUAL'))
            for oid in sorted(selected):self.append(c,'v2_tracklet_membership','observation_id',oid,(tid,'MANUAL',fingerprint(decision_id)))
            self.constrain(c,{new},{person},decision_id)
            self.freeze_manual_authority(c,{new,person},decision_id);self.publish_links(c,{new,person})
            return new

    def ingest_ready(self,source,revision):
        """Pair raw YOLO and optional buffalo_l artifacts, preserving lineage.

        Missing faces never mean no person. Face-center containment is a bbox
        association proxy; ambiguous assignment is left unattached.
        """
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            artifacts=list(c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision)))
            for artifact in artifacts:
                y=c.execute("SELECT * FROM v2_jobs WHERE stage='yoloe' AND item_id=? ORDER BY revision DESC LIMIT 1",(artifact['artifact_id'],)).fetchone()
                face=c.execute("SELECT * FROM v2_jobs WHERE stage='insightface' AND item_id=? ORDER BY revision DESC LIMIT 1",(artifact['artifact_id'],)).fetchone()
                if not y or y['status']!='DONE' or y['input_sha']!=artifact['sha']:continue
                available=face and face['status']=='DONE' and face['input_sha']==artifact['sha']
                faces=json.loads(face['result_json'])['faces'] if available else []
                detections=[(i,d) for i,d in enumerate(json.loads(y['result_json'])['detections']) if d['label'].lower()=='person']
                boxes=[]
                for i,d in detections:
                    bbox=d.get('bbox',d.get('bbox_xyxy'))
                    if not bbox or len(bbox)!=4:raise ContractError('yolo_person_bbox_missing')
                    normalized=[max(0.,min(1.,v/(artifact['width'] if j%2==0 else artifact['height']))) for j,v in enumerate(bbox)]
                    boxes.append((i,d,normalized))
                associated={i:[] for i,_,_ in boxes}
                body_boxes=list(boxes)
                face_owners=[]
                for f in faces:
                    x=(f['bbox'][0]+f['bbox'][2])/2/artifact['width']; ypos=(f['bbox'][1]+f['bbox'][3])/2/artifact['height']
                    face_owners.append([idx for idx,_,b in body_boxes if b[0]<=x<=b[2] and b[1]<=ypos<=b[3]])
                for i,f in enumerate(faces):
                    x=(f['bbox'][0]+f['bbox'][2])/2/artifact['width'];ypos=(f['bbox'][1]+f['bbox'][3])/2/artifact['height']
                    possible=face_owners[i]
                    evidence={**f,'vector':f.get('embedding'),'quality':f['quality_score'],
                      'face_ref':face['execution_key']+':'+str(i),'model_contract':fingerprint(json.loads(face['provenance']))}
                    if len(possible)==1 and sum(possible[0] in owners for owners in face_owners)==1:
                        associated[possible[0]].append(evidence)
                    else:
                        # A valid face is independent evidence, not gated by a
                        # YOLO body detection. Ambiguous bodies are never guessed.
                        normalized=[float(v)/(artifact['width'] if j%2==0 else artifact['height']) for j,v in enumerate(f['bbox'])]
                        boxes.append(('face_'+str(i),{'confidence':f.get('detection_score',f.get('det_score',f.get('confidence',f['quality_score'])))},normalized))
                        associated['face_'+str(i)]=[evidence]
                c.execute('DELETE FROM v2_observation_heads WHERE artifact_id=?',(artifact['artifact_id'],))
                for i,d,b in boxes:
                    key=fingerprint((y['execution_key'],face['execution_key'] if available else 'FACE_MISSING',i))
                    provenance={'yolo_execution_key':y['execution_key'],'input_sha':artifact['sha'],
                        'face_execution_key':face['execution_key'] if available else None,'face_status':'READY' if available else 'MISSING',
                        'yolo_contract':json.loads(y['provenance']),'association':'FACE_CENTER_CONTAINMENT_PROXY',
                        'logical_mapping_ambiguous':sum(1 for _,_,box in boxes if box==b)>1}
                    self.observation(c,source,revision,artifact['artifact_id'],artifact['time_ms'],b,d['confidence'],associated[i],provenance,y['revision'],key,detection_slot=i)

    def reconcile_source(self, source, revision,*,yield_hook=None):
        """Wire the existing conservative face policy into source advancement.

        Cross-source auto merges require two observations on each side; sparse
        frames in one source require two distinct compatible face-backed frames.
        A single image can only become a review suggestion. Manual
        cannot-link/defer decisions and same-frame conflicts remain authoritative.
        """
        if yield_hook is not None:yield_hook()
        with self.store.connect() as c:
            c.execute('BEGIN')
            local_people={r[0] for r in c.execute('SELECT source_person_id FROM v2_source_people WHERE source_id=? AND source_revision=?',(source,revision))}
            if not local_people:return []
            # Cache immutable evidence + manual state. Repeated downstream
            # events must not rebuild the identity graph without new evidence.
            token=fingerprint((
                [tuple(r) for r in c.execute('SELECT artifact_id,detection_slot,observation_id FROM v2_observation_heads ORDER BY artifact_id,detection_slot')],
                [tuple(r) for r in c.execute('SELECT source_id,source_revision FROM v2_active_sources ORDER BY source_id')],
                [tuple(r) for r in c.execute('SELECT decision_id FROM v2_person_decisions ORDER BY decision_id')],
                [tuple(r) for r in c.execute("SELECT execution_key,status FROM v2_jobs WHERE stage='insightface' ORDER BY execution_key")]))
            cache=getattr(self,'_reconcile_tokens',{})
            if cache.get((source,revision))==token:return []
            _,person_groups,groups=self.relationship_snapshot(c)
            local={person_groups[person] for person in local_people}
            candidates=set(groups)
            snapshot=self.observation_snapshot(c,person_groups,yield_hook=yield_hook)
            by_person={p:{r.get('model_contract') for r in rows} for p,rows in snapshot['records'].items()}
            group_contracts={gid:set().union(*(by_person.get(person,set()) for person in members)) for gid,members in groups.items()}
        if yield_hook is not None:yield_hook()
        done=set(); results=[]
        for left in sorted(local):
            for right in sorted(candidates-local) + sorted(local):
                pair=tuple(sorted((left,right)))
                if left==right or pair in done:continue
                done.add(pair)
                contracts=group_contracts.get(left,set()) & group_contracts.get(right,set())
                for contract in sorted(c for c in contracts if c):
                    policy={'quality_min':0.2,'cosine_min':0.58,'review_cosine_min':0.48,
                            'minimum_observations':2,'calibrated':True,'model_contract':contract,
                            'allow_source_face_continuity':True}
                    results.append(self.reconcile(left,right,policy,observation_snapshot=snapshot))
                    # Each pair has committed completely. A producer refill or
                    # Stop may now interrupt; the full-source token stays unset.
                    if yield_hook is not None:yield_hook()
        cache[(source,revision)]=token
        self._reconcile_tokens=cache
        return results

    def import_manual_annotations(self,annotations,cluster_to_global,*,actor,import_id):
        """Explicit bridge from the existing local-person annotation format.

        Never infer NOT_SAME from separate display names or unrelated clusters.
        Imported user memberships are stronger than automatic reconciliation.
        """
        from .local_person_annotations import CONTRACT
        if annotations.get('contract')!=CONTRACT:raise ContractError('legacy_person_annotation_contract')
        results=[]
        for identity,value in sorted(annotations.get('identities',{}).items()):
            clusters=sorted(value.get('cluster_ids',[]))
            if any(cluster not in cluster_to_global for cluster in clusters):raise ContractError('manual_cluster_mapping_missing')
            gids=[cluster_to_global[cluster] for cluster in clusters]
            for gid in gids[1:]:results.append(self.decide('MERGE',gids[0],gid,actor=actor,decision_id=fingerprint((import_id,identity,gid))))
        return results
