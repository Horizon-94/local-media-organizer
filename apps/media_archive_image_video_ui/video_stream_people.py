"""Source-local frozen people computation on a detached SQLite snapshot.

No observation from another video is compared. Manual relationship authority
is copied in bulk and revalidated before a short write-back. Global matching
is deliberately absent; new global IDs are pending source-scoped mappings.
"""
from contextlib import contextmanager,nullcontext
import json
import sqlite3
from .v2_people_coordinates import DisplayAwarePersonIdentity
from .v2_people import PersonIdentity
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .v2_model_workers import module
from .video_coordination import install_tokens,tokens,guards,event_command
from .video_commit_queue import SQLCommand,WriteProposal,StaleProposal

LOCAL=('v2_person_observations','v2_observation_heads','v2_tracklets',
       'v2_tracklet_membership','v2_source_people','v2_source_face_anchors','v2_tracklet_people',
       'v2_person_review_observations')
MANUAL=('v2_person_decisions','v2_person_constraints','v2_person_aliases',
        'v2_manual_observation_authority','v2_global_memberships','v2_global_people','v2_person_decision_results')


class MemoryStore:
    def __init__(self,c):self.c=c;self.events=[]
    @contextmanager
    def connect(self):
        try:
            yield self.c
            self.c.commit()
        except BaseException:
            self.c.rollback();raise
    def emit(self,c,event,source,revision,version,payload):
        self.events.append((event,source,revision,version,payload))


class LocalIdentity(DisplayAwarePersonIdentity):
    def ensure_person(self,c,person,source,revision):
        c.execute("INSERT OR IGNORE INTO v2_source_people VALUES(?,?,?,'UNKNOWN')",(person,source,revision))
        old=c.execute('SELECT global_person_id FROM v2_global_memberships WHERE source_person_id=? ORDER BY version DESC LIMIT 1',(person,)).fetchone()
        if old:return self.canonical(c,old[0])
        pending='pending_global_'+fingerprint((source,revision,person))
        c.execute("INSERT OR IGNORE INTO v2_global_people VALUES(?,'','PENDING_GLOBAL_MERGE')",(pending,))
        self.append(c,'v2_global_memberships','source_person_id',person,(pending,'AUTO'))
        return pending


def snapshot(store,source,revision,connection=None):
    """Person evidence scoped; manual maps are bulk small authority tables."""
    with (nullcontext(connection) if connection is not None else store.connect()) as c:
        if not c.in_transaction:c.execute('BEGIN')
        sources=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
        evidence=[tuple(r) for r in c.execute("SELECT execution_key,result_sha,status FROM v2_jobs WHERE source_id=? AND source_revision=? AND stage IN ('yoloe','insightface') ORDER BY execution_key",(source,revision))]
        tables={r[0]:r[1] for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        rows={}
        for name in (*LOCAL,*MANUAL,'v2_artifacts','v2_jobs'):
            if name in ('v2_artifacts','v2_jobs'):
                column='revision' if name=='v2_artifacts' else 'source_revision'
                query=f'SELECT * FROM {name} WHERE source_id=? AND {column}=?'
                values=(source,revision)
            elif name in ('v2_person_observations','v2_tracklets','v2_source_people','v2_source_face_anchors'):
                query=f'SELECT * FROM {name} WHERE source_id=? AND source_revision=?';values=(source,revision)
            elif name in ('v2_observation_heads','v2_tracklet_membership','v2_person_review_observations'):
                query=f'SELECT * FROM {name} WHERE observation_id IN (SELECT observation_id FROM v2_person_observations WHERE source_id=? AND source_revision=?)';values=(source,revision)
            elif name=='v2_tracklet_people':
                query=f'SELECT * FROM {name} WHERE tracklet_id IN (SELECT tracklet_id FROM v2_tracklets WHERE source_id=? AND source_revision=?)';values=(source,revision)
            else:query=f'SELECT * FROM {name}';values=()
            rows[name]=[tuple(r) for r in c.execute(query,values)]
        token=tokens(c,source)
    return tables,rows,token


def build_local(adapter,source,revision,threshold):
    store=adapter.store
    # Initialize only schema, not global reconciliation.
    PersonIdentity(store)
    install_tokens(store,(*LOCAL,*MANUAL,'v2_jobs','v2_active_sources'))
    schemas,rows,token=snapshot(store,source,revision)
    memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
    try:
        for name,values in rows.items():
            memory.execute(schemas[name])
            if values:memory.executemany(f'INSERT INTO {name} VALUES({",".join("?" for _ in values[0])})',values)
        memory.commit()
        temporary=MemoryStore(memory);people=LocalIdentity(temporary)
        people.ingest_ready(source,revision)
        people.build_source(source,revision)
        # Frozen face clustering algorithm, restricted to this video.
        import numpy as np
        records=[]
        for job in memory.execute("SELECT * FROM v2_jobs WHERE stage='insightface' AND status='DONE' ORDER BY item_id"):
            for slot,face in enumerate(json.loads(job['result_json'])['faces']):
                face_id=fingerprint((source,revision,job['execution_key'],slot))
                records.append({'row':{'face_id':face_id,'visual_unit_id':job['item_id'],'source_content_id':source},'vector':np.asarray(face['embedding'],dtype=np.float32)})
        clusters=module('insightface')._cluster_face_records(records,threshold)
        manifest=[dict(local_person_id='local_'+fingerprint((source,revision,sorted(row['face_id'] for row,_ in group['members']))),
            source_id=source,source_revision=revision,representative=group['representative_face_id'],members=group['members']) for group in clusters]
        output={name:[tuple(r) for r in memory.execute('SELECT * FROM '+name)] for name in (*LOCAL,'v2_global_people','v2_global_memberships')}
        # All diff/JSON work occurs before the writer. Monotonic generation
        # fences replace the old full snapshot/hash under BEGIN IMMEDIATE.
        commands=[]
        for name,values in output.items():
            old=set(rows[name]);changes=[row for row in values if row not in old]
            commands.extend(SQLCommand(f'INSERT OR REPLACE INTO {name} VALUES({",".join("?" for _ in row)})',tuple(row)) for row in changes)
        commands.extend(event_command(adapter,*event) for event in temporary.events)
        commands.append(SQLCommand('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)',
            (source,revision,'LOCAL_PEOPLE_DONE',json.dumps({'clusters':manifest,'global_merge':'PENDING','auto_merge_cosine_min':threshold},sort_keys=True))))
        try:
            adapter.ledger.commit_queue.submit(WriteProposal('people_local',source,tuple(commands),guards(adapter,source,revision,token))).result()
        except StaleProposal as error:raise ContractError('local_people_snapshot_changed_retry') from error
        return manifest
    finally:memory.close()
