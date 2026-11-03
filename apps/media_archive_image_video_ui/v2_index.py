"""Incremental timeline fusion, FTS5, explicit vector adapter and final join.

SQLiteExactVectors is a bounded exact fallback, NOT an ANN/scalable index.
Namespaces include the execution contract so incompatible embeddings never mix.
A deployment can replace this adapter without changing document/coverage keys.
"""
from __future__ import annotations
import json
from . import query_trace as trace
import math
import re
from .v2_producer import fingerprint
from .v2_runtime import ContractError

def fts_tokens(text):
    """Derived CJK bigrams; original document text is never rewritten."""
    chunks=[]
    for token in re.findall(r'[\u3400-\u9fff]+|[\w]+',text):
        chunks.append(token)
        if re.fullmatch(r'[\u3400-\u9fff]+',token):
            chunks+= [token[i:i+2] for i in range(len(token)-1)]
            chunks+=list(token)
    return ' '.join(chunks)

def fts_query(text):
    terms=[]
    for token in re.findall(r'[\u3400-\u9fff]+|[\w]+',text):
        if len(token)>2 and re.fullmatch(r'[\u3400-\u9fff]+',token):token=' '.join(token[i:i+2] for i in range(len(token)-1))
        terms.append('"'+token.replace('"','""')+'"')
    if not terms:raise ContractError('empty_search_query')
    return ' AND '.join(terms)


def _vector_json_loads(value):
    """Use the already bundled decoder; retain stdlib fallback and numeric format."""
    global _VECTOR_JSON_DECODER
    if _VECTOR_JSON_DECODER is None:
        import importlib.util, sysconfig
        from pathlib import Path
        extension = (Path(__file__).resolve().parents[3] / 'PipelineEnvs/qwen/site-packages/orjson' /
                     ('orjson' + sysconfig.get_config_var('EXT_SUFFIX')))
        try:
            spec = importlib.util.spec_from_file_location('_build151_vector_decoder.orjson', extension)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _VECTOR_JSON_DECODER = module.loads
        except (ImportError, OSError, AttributeError, TypeError):
            _VECTOR_JSON_DECODER = json.loads
    try:
        return _VECTOR_JSON_DECODER(value)
    except ValueError:
        return json.loads(value)

_VECTOR_JSON_DECODER = None

class SQLiteExactVectors:
    backend='sqlite-exact-v1'
    def __init__(self,store):self.store=store
    def upsert(self,c,entity,namespace,vector,input_digest):
        if not vector or not all(math.isfinite(x) for x in vector):raise ContractError('invalid_index_vector')
        norm=math.sqrt(sum(x*x for x in vector))
        if not norm:raise ContractError('zero_index_vector')
        old=c.execute('SELECT dimension FROM v2_vectors WHERE namespace=? LIMIT 1',(namespace,)).fetchone()
        if old and old[0]!=len(vector):raise ContractError('namespace_dimension_conflict')
        c.execute('INSERT OR REPLACE INTO v2_vectors VALUES(?,?,?,?,?)',(entity,namespace,len(vector),json.dumps([x/norm for x in vector]),input_digest))
    def delete(self,c,entity):c.execute('DELETE FROM v2_vectors WHERE entity_id=?',(entity,))
    def search(self,namespace,vector,limit=20,*,candidate_groups=None,group_limits=None):
        norm=math.sqrt(sum(x*x for x in vector))
        if norm==0:raise ContractError('zero_query')
        with self.store.connect() as c:
          from .v2_release_search import enabled,predicate
          fence=(' AND (EXISTS(SELECT 1 FROM v2_documents pd WHERE pd.document_id=v.entity_id'+predicate(c,'pd.source_id','pd.source_revision')+') OR EXISTS(SELECT 1 FROM v2_artifacts pa WHERE pa.artifact_id=v.entity_id'+predicate(c,'pa.source_id','pa.revision')+'))') if enabled(c) else ''
          with trace.span('vector_sql_read'):
            rows=list(c.execute('''SELECT v.* FROM v2_vectors v WHERE namespace=?
            AND NOT EXISTS(SELECT 1 FROM v2_documents d WHERE d.document_id=v.entity_id AND (
              NOT EXISTS(SELECT 1 FROM v2_searchable_documents visible WHERE visible.document_id=d.document_id) OR
              NOT EXISTS(SELECT 1 FROM v2_current_jobs j WHERE j.execution_key=d.raw_result_ref AND j.status='DONE') OR
              NOT EXISTS(SELECT 1 FROM v2_current_jobs j WHERE j.execution_key=d.embedding_ref AND j.status='DONE' AND j.result_sha=v.input_digest)))
            AND NOT EXISTS(SELECT 1 FROM v2_artifacts f WHERE f.artifact_id=v.entity_id AND
              NOT EXISTS(SELECT 1 FROM v2_current_jobs j WHERE j.item_id=f.artifact_id AND j.stage='openclip' AND j.status='DONE' AND j.result_sha=v.input_digest))
            AND NOT EXISTS(SELECT 1 FROM v2_documents d JOIN v2_active_sources a ON a.source_id=d.source_id
              WHERE d.document_id=v.entity_id AND d.source_revision<>a.source_revision)
            AND NOT EXISTS(SELECT 1 FROM v2_artifacts f JOIN v2_active_sources a ON a.source_id=f.source_id
              WHERE f.artifact_id=v.entity_id AND f.revision<>a.source_revision)'''+fence,(namespace,)))
        if rows and len(vector)!=rows[0]['dimension']:raise ContractError('query_dimension_conflict')
        with trace.span('vector_parse_dot_sort'):
            scored=[(r['entity_id'],sum(x*y for x,y in zip(_vector_json_loads(r['vector_json']),vector))/norm) for r in rows]
            ordered=sorted(scored,key=lambda r:(-r[1],r[0]))
            if candidate_groups is None:return ordered[:limit]
            ordered=[hit for hit in ordered if hit[0] in candidate_groups]
            if not group_limits:return ordered[:limit]
            selected=[];used=set();counts={}
            for hit in ordered:
                group=candidate_groups[hit[0]]
                if counts.get(group,0)<group_limits.get(group,0):
                    selected.append(hit);used.add(hit[0]);counts[group]=counts.get(group,0)+1
            for hit in ordered:
                if len(selected)>=limit:break
                if hit[0] not in used:selected.append(hit);used.add(hit[0])
            return sorted(selected,key=lambda r:(-r[1],r[0]))[:limit]

class FusionIndex:
    def __init__(self,store,vector_factory=SQLiteExactVectors):
        self.store=store
        from .v2_documents import DocumentChain
        DocumentChain(store,{})  # schema initialization is independent of consumer startup order
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS v2_fusion(link_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
              artifact_id TEXT,start_ms INTEGER,end_ms INTEGER,kind TEXT,result_ref TEXT,payload TEXT);
            CREATE INDEX IF NOT EXISTS v2_fusion_timeline ON v2_fusion(source_id,source_revision,start_ms,end_ms);
            CREATE VIRTUAL TABLE IF NOT EXISTS v2_text_fts USING fts5(document_id UNINDEXED,source_id UNINDEXED,text);
            CREATE TABLE IF NOT EXISTS v2_vectors(entity_id TEXT,namespace TEXT,dimension INTEGER,vector_json TEXT,
              input_digest TEXT,PRIMARY KEY(entity_id,namespace));
            CREATE INDEX IF NOT EXISTS v2_vectors_namespace ON v2_vectors(namespace);
            CREATE TABLE IF NOT EXISTS v2_index_coverage(entity_id TEXT,index_kind TEXT,input_digest TEXT,
              backend TEXT,status TEXT,PRIMARY KEY(entity_id,index_kind));
            CREATE TABLE IF NOT EXISTS v2_source_closure(source_id TEXT,source_revision TEXT,join_digest TEXT,
              status TEXT,PRIMARY KEY(source_id,source_revision));
            ''')
        from .v2_projection import FinalProjection
        FinalProjection(store,{})
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DROP VIEW IF EXISTS v2_searchable_documents')
            c.execute('''CREATE VIEW v2_searchable_documents AS SELECT d.* FROM v2_documents d
              WHERE d.document_type NOT IN ('qwen','ocr') OR NOT EXISTS(
                SELECT 1 FROM v2_final_projection_heads h JOIN v2_job_dependencies dep ON dep.child_key=h.execution_key
                WHERE h.source_id=d.source_id AND h.source_revision=d.source_revision AND dep.parent_key=d.raw_result_ref)''')
        self.vectors=vector_factory(store)

    def covered(self,c,entity,kind,digest,backend):
        c.execute("INSERT OR REPLACE INTO v2_index_coverage VALUES(?,?,?,?,'READY')",(entity,kind,digest,backend))

    def fusion(self,c,source,revision,entity,artifact,start,end,kind,result_ref,payload):
        link=fingerprint((source,revision,entity,kind))
        c.execute('INSERT OR REPLACE INTO v2_fusion VALUES(?,?,?,?,?,?,?,?,?)',
                  (link,source,revision,artifact,start,end,kind,result_ref,json.dumps(payload,sort_keys=True)))
        self.covered(c,entity,'fusion',fingerprint(payload),'sqlite-timeline-v1')

    def index_document(self,c,did):
        doc=c.execute('SELECT * FROM v2_documents WHERE document_id=?',(did,)).fetchone()
        if not doc:raise ContractError('document_missing')
        job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(doc['raw_result_ref'],)).fetchone()
        if not job or job['status']!='DONE':
            c.execute('DELETE FROM v2_text_fts WHERE document_id=?',(did,));self.vectors.delete(c,did)
            c.execute('DELETE FROM v2_index_coverage WHERE entity_id=?',(did,))
            c.execute('DELETE FROM v2_fusion WHERE result_ref=?',(doc['raw_result_ref'],))
            return False
        digest=fingerprint((doc['revision'],doc['text'],job['result_sha']))
        c.execute('DELETE FROM v2_text_fts WHERE document_id=?',(did,))
        c.execute('INSERT INTO v2_text_fts VALUES(?,?,?)',(did,doc['source_id'],fts_tokens(doc['text'])))
        self.covered(c,did,'fts',digest,'sqlite-fts5-cjk-bigram-v1')
        self.fusion(c,doc['source_id'],doc['source_revision'],did,doc['artifact_id'],doc['start_ms'],doc['end_ms'],doc['document_type'],doc['raw_result_ref'],dict(doc))
        if doc['embedding_ref']:
            embedding=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(doc['embedding_ref'],)).fetchone()
            self.vectors.delete(c,did)
            c.execute("DELETE FROM v2_index_coverage WHERE entity_id=? AND index_kind='vector'",(did,))
            if embedding and embedding['status']=='DONE':
                namespace='text:'+fingerprint(json.loads(embedding['provenance']))
                self.vectors.upsert(c,did,namespace,json.loads(embedding['result_json'])['vector'],embedding['result_sha'])
                self.covered(c,did,'vector',embedding['result_sha'],self.vectors.backend)
        return True

    def callback(self,c,event):
        p=event['payload'];source,revision=event['source_id'],event['source_revision']
        # Sweep only this source's stale materializations when a new event arrives.
        for doc in list(c.execute('SELECT document_id FROM v2_documents WHERE source_id=? AND source_revision=?',(source,revision))):self.index_document(c,doc[0])
        c.execute("DELETE FROM v2_fusion WHERE source_id=? AND source_revision=? AND EXISTS (SELECT 1 FROM v2_jobs j WHERE j.execution_key=v2_fusion.result_ref AND j.status='SUPERSEDED')",(source,revision))
        if event['event_type']=='SOURCE_READY':
            self.fusion(c,source,revision,'source:'+source+':'+revision,None,None,None,'source_metadata',source,p)
        elif event['event_type']=='EVIDENCE_READY' and p['stage'] in ('yoloe','openclip','dhash','quality','geometry','insightface'):
            job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(p['execution_key'],)).fetchone()
            if not job or job['status']!='DONE':return
            artifact=c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(p['item_id'],)).fetchone()
            if not artifact:raise ContractError('fusion_artifact_missing')
            self.fusion(c,source,revision,job['execution_key'],artifact['artifact_id'],artifact['time_ms'],artifact['time_ms'],p['stage'],job['execution_key'],p)
            if p['stage']=='openclip':
                namespace='visual:'+fingerprint(json.loads(job['provenance']))
                self.vectors.upsert(c,artifact['artifact_id'],namespace,p['result']['vector'],p['payload_sha'])
                self.covered(c,artifact['artifact_id'],'visual-vector',p['payload_sha'],self.vectors.backend)
        elif event['event_type']=='PERSON_LINK_UPDATED':
            if not c.execute('SELECT 1 FROM v2_observation_heads WHERE observation_id=?',(p['observation_id'],)).fetchone():return
            # Current membership wins even if a late stale event is redelivered.
            from .v2_people import PersonIdentity
            people=PersonIdentity.__new__(PersonIdentity);people.store=self.store
            membership=c.execute('SELECT tracklet_id FROM v2_tracklet_membership WHERE observation_id=? ORDER BY version DESC LIMIT 1',(p['observation_id'],)).fetchone()
            current=c.execute('SELECT source_person_id FROM v2_tracklet_people WHERE tracklet_id=? ORDER BY version DESC LIMIT 1',(membership[0],)).fetchone() if membership else None
            if not current or current[0]!=p['source_person_id'] or people.person_global(c,current[0])!=p['global_person_id']:return
            self.fusion(c,source,revision,p['observation_id'],p.get('artifact_id'),p.get('time_ms'),p.get('time_ms'),'person',p['source_person_id'],p)

    def advance(self, limit=100):return self.store.deliver('fusion-index',self.callback,limit=limit)

    def text_search(self,query,source=None,limit=20):
        with self.store.connect() as c:
            from .v2_release_search import predicate
            fence=predicate(c,'d.source_id','d.source_revision')
            return [dict(r) for r in c.execute('''SELECT f.document_id,f.source_id,d.text,bm25(v2_text_fts) rank
              FROM v2_text_fts f JOIN v2_searchable_documents d ON d.document_id=f.document_id
              JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref
              WHERE v2_text_fts MATCH ? AND j.status='DONE' AND (? IS NULL OR f.source_id=?)
              AND NOT EXISTS(SELECT 1 FROM v2_active_sources a WHERE a.source_id=d.source_id AND a.source_revision<>d.source_revision)
              '''+fence+''' ORDER BY rank,f.document_id LIMIT ?''',(fts_query(query),source,source,limit))]

    def rebuild(self,source,revision):
        """Atomic reconstruction from persisted results; no model execution."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in list(c.execute('SELECT document_id FROM v2_documents WHERE source_id=? AND source_revision=?',(source,revision))):self.index_document(c,row[0])
            for row in list(c.execute("SELECT payload FROM v2_outbox WHERE aggregate_id=? AND event_type IN ('EVIDENCE_READY','PERSON_LINK_UPDATED') ORDER BY committed_at,event_id",(source+':'+revision,))):self.callback(c,json.loads(row[0]))

    def person_search(self,global_person_id,source=None):
        from .v2_people import PersonIdentity
        people=PersonIdentity.__new__(PersonIdentity);people.store=self.store
        with self.store.connect() as c:
            target=people.canonical(c,global_person_id);result=[]
            rows=c.execute("SELECT * FROM v2_fusion WHERE kind='person' AND (? IS NULL OR source_id=?) ORDER BY source_id,start_ms,link_id",(source,source))
            for row in rows:
                p=json.loads(row['payload'])
                active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(row['source_id'],)).fetchone()
                if active and active[0]!=row['source_revision']:continue
                if not c.execute('SELECT 1 FROM v2_observation_heads WHERE observation_id=?',(p['observation_id'],)).fetchone():continue
                if people.person_global(c,p['source_person_id'])==target:result.append(dict(row))
            return result

    def timeline_search(self,source,revision):
        with self.store.connect() as c:
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
            if active and active[0]!=revision:return []
            rows=list(c.execute('SELECT * FROM v2_fusion WHERE source_id=? AND source_revision=? ORDER BY start_ms,link_id',(source,revision)))
            result=[]
            for row in rows:
                if row['kind']=='source_metadata':result.append(dict(row));continue
                if row['kind']=='person':
                    p=json.loads(row['payload'])
                    if c.execute('SELECT 1 FROM v2_observation_heads WHERE observation_id=?',(p['observation_id'],)).fetchone():result.append(dict(row))
                elif row['kind'] in ('qwen','ocr') and not c.execute('SELECT 1 FROM v2_searchable_documents WHERE raw_result_ref=?',(row['result_ref'],)).fetchone():continue
                elif c.execute("SELECT 1 FROM v2_current_jobs WHERE execution_key=? AND status='DONE'",(row['result_ref'],)).fetchone():result.append(dict(row))
            return result

    def close_source(self,source,revision,selection,required_revisions,local_person_required=None,selection_config=None,require_final_projection=False,*,visual_only=False,allow_empty_text=False):
        # A missing required fusion row cannot pass the original barrier.
        # Reject that necessary condition before rebuilding selection inputs;
        # this is only a WAITING shortcut, never a completion proof. Once it
        # passes, the original full revision/evidence barrier below still runs.
        revisions=dict(required_revisions)
        if local_person_required:revisions['insightface']=local_person_required['revision']
        with self.store.connect() as c:
            missing=c.execute('''SELECT j.stage FROM v2_jobs j JOIN json_each(?) required
                ON required.key=j.stage AND required.value=j.revision
                JOIN v2_artifacts a ON a.artifact_id=j.item_id AND a.source_id=j.source_id AND a.revision=j.source_revision
                WHERE j.source_id=? AND j.source_revision=? AND j.status='DONE' AND a.status='READY'
                AND NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=j.execution_key AND x.index_kind='fusion')
                LIMIT 1''',(json.dumps(revisions),source,revision)).fetchone()
            if missing:raise ContractError('local_person_index_incomplete' if missing[0]=='insightface' else 'required_visual_fusion_incomplete')
        # Barrier A validation is re-used, not a weaker completion heuristic.
        inputs=selection.frozen_inputs(source,revision,required_revisions,selection_config)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            planner=getattr(self.store,'fanout_planner',None)
            if planner and planner.pending(c,source,revision,visual_only=visual_only):
                raise ContractError('required_fanout_not_fully_promoted')
            for key,digest in inputs['evidence_keys']:
                job=c.execute('SELECT status,result_sha FROM v2_current_jobs WHERE execution_key=?',(key,)).fetchone()
                coverage=c.execute("SELECT 1 FROM v2_index_coverage WHERE entity_id=? AND index_kind='fusion'",(key,)).fetchone()
                if job['status']!='DONE' or job['result_sha']!=digest or not coverage:raise ContractError('required_visual_fusion_incomplete')
            local_faces=[]
            if local_person_required:
                artifacts=list(c.execute('SELECT artifact_id FROM v2_artifacts WHERE source_id=? AND revision=?',(source,revision)))
                for artifact in artifacts:
                    face=c.execute("SELECT execution_key,result_sha,status,revision FROM v2_jobs WHERE stage='insightface' AND item_id=? ORDER BY revision DESC LIMIT 1",(artifact[0],)).fetchone()
                    if not face or face['revision']!=local_person_required['revision'] or face['status']!='DONE':raise ContractError('local_person_branch_incomplete')
                    if not c.execute("SELECT 1 FROM v2_index_coverage WHERE entity_id=? AND index_kind='fusion' AND status='READY'",(face['execution_key'],)).fetchone():raise ContractError('local_person_index_incomplete')
                    local_faces.append((face['execution_key'],face['result_sha']))
            snapshot=c.execute("SELECT * FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? AND evidence_digest=? AND status='DONE'",(source,revision,fingerprint(inputs))).fetchone()
            if not snapshot:raise ContractError('current_candidate_snapshot_missing')
            candidates=list(c.execute('SELECT * FROM v2_selected_candidates WHERE snapshot_id=?',(snapshot['snapshot_id'],)))
            required_jobs=[]
            for item in candidates:
                job=c.execute('SELECT execution_key,result_sha,status FROM v2_jobs WHERE stage=? AND item_id=? ORDER BY revision DESC LIMIT 1',(item['purpose'],item['candidate_id'])).fetchone()
                if not job or job['status']!='DONE':raise ContractError('candidate_branch_incomplete')
                required_jobs.append(job)
            audio=audio_job=vad_job=None
            if not visual_only:
                audio=c.execute('SELECT * FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
                if not audio or audio['status'] not in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY'):raise ContractError('audio_branch_incomplete')
                # An old NO_AUDIO/NO_SPEECH projection is not proof that a newly
                # requested extraction/VAD revision has completed.
                audio_job=c.execute("SELECT execution_key,result_sha,status,result_json FROM v2_jobs WHERE stage='audio_extract' AND item_id=? ORDER BY revision DESC LIMIT 1",(source+':'+revision,)).fetchone()
                if audio_job and audio_job['status']!='DONE':raise ContractError('audio_revision_incomplete')
                if audio_job:
                    actual_audio=json.loads(audio_job['result_json'])
                    if (actual_audio['status']=='NO_AUDIO')!=(audio['status']=='NO_AUDIO'):raise ContractError('audio_projection_stale')
                    if actual_audio['status']=='READY' and actual_audio['sha']!=json.loads(audio['artifact_json']).get('sha'):raise ContractError('audio_projection_stale')
                vad_job=None
                if audio['status']!='NO_AUDIO':
                    vad_job=c.execute("SELECT execution_key,result_sha,status,result_json,input_sha FROM v2_current_jobs WHERE stage='vad' AND source_id=? AND source_revision=? ORDER BY revision DESC LIMIT 1",(source,revision)).fetchone()
                    if not vad_job or vad_job['status']!='DONE':raise ContractError('vad_revision_incomplete')
                    audio_result=json.loads(audio_job['result_json']) if audio_job else json.loads(audio['artifact_json'])
                    if vad_job['input_sha']!=audio_result.get('sha'):raise ContractError('vad_audio_revision_mismatch')
                    if json.loads(vad_job['result_json'])['segments']!=json.loads(audio['segments_json']):raise ContractError('vad_projection_stale')
                if audio['status']=='SEGMENTS_READY':
                    whisper=[j for j in c.execute("SELECT execution_key,result_sha,status,payload FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND stage='whisper' AND status<>'SUPERSEDED'",(source,revision)) if json.loads(j['payload']).get('vad_execution_key')==vad_job['execution_key']]
                    if len(whisper)!=len(json.loads(audio['segments_json'])) or any(j['status']!='DONE' for j in whisper):raise ContractError('transcript_incomplete')
                    required_jobs+=whisper
            head=c.execute('SELECT h.execution_key FROM v2_final_projection_heads h JOIN v2_current_jobs j USING(execution_key) WHERE h.source_id=? AND h.source_revision=? AND h.snapshot_id=?',(source,revision,snapshot['snapshot_id'])).fetchone()
            if require_final_projection and not head:raise ContractError('final_projection_missing')
            if head:
                final_jobs=list(c.execute('SELECT j.execution_key,j.result_sha FROM v2_final_projection_documents p JOIN v2_current_jobs j ON j.execution_key=p.document_key WHERE p.execution_key=?',(head[0],)))
                if len(final_jobs)!=c.execute('SELECT count(*) FROM v2_final_projection_documents WHERE execution_key=?',(head[0],)).fetchone()[0]:raise ContractError('final_projection_coverage_incomplete')
                required_jobs += final_jobs
            # No person/global reconciliation dependency is present here.
            embedding_keys=[]
            for job in required_jobs:
                doc=c.execute('SELECT document_id,status,embedding_ref FROM v2_documents WHERE raw_result_ref=?',(job['execution_key'],)).fetchone()
                terminal=getattr(self.store,'terminal_document_outcome',None)
                if doc is None and terminal:
                    proof=terminal(c,job)
                    if proof is not None:
                        embedding_keys.append((job['execution_key'],'WHISPER_REJECTED_FINAL',job['result_sha'],proof))
                        continue
                if allow_empty_text and doc and doc['status']=='SKIP_NO_TEXT':
                    proof=c.execute('SELECT stage,status,result_json,result_sha FROM v2_current_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone()
                    skip=c.execute("SELECT input_digest FROM v2_index_coverage WHERE entity_id=? AND index_kind='no-text-skip' AND status='READY'",(doc['document_id'],)).fetchone()
                    result=json.loads(proof['result_json']) if proof else {}
                    no_text=result.get('text','NOT_EMPTY')
                    if terminal and result.get('asr_contract')=='source-global-usable-asr-v1':
                        terminal(c,job)  # validates owning interval/raw lineage even for usable-empty
                        no_text=result['usable_text']
                    if (not proof or proof['stage'] not in ('ocr','whisper') or proof['status']!='DONE'
                            or no_text.strip() or doc['embedding_ref']
                            or not skip or skip[0]!=proof['result_sha']):
                        raise ContractError('invalid_no_text_skip_proof')
                    embedding_keys.append((doc['document_id'],'SKIP_NO_TEXT',proof['result_sha']))
                    continue
                if not doc or doc['status']!='EMBEDDED':raise ContractError('required_document_incomplete')
                embedding=c.execute("SELECT execution_key,result_sha,status FROM v2_jobs WHERE stage='embedding' AND item_id=? ORDER BY revision DESC LIMIT 1",(doc['document_id'],)).fetchone()
                if not embedding or embedding['status']!='DONE' or embedding['execution_key']!=doc['embedding_ref']:raise ContractError('document_embedding_revision_incomplete')
                embedding_keys.append((embedding['execution_key'],embedding['result_sha']))
                self.index_document(c,doc['document_id'])
                coverage={r['index_kind'] for r in c.execute("SELECT index_kind FROM v2_index_coverage WHERE entity_id=? AND status='READY'",(doc['document_id'],))}
                if not {'fts','fusion','vector'}<=coverage:raise ContractError('required_index_coverage_incomplete')
            audio_keys=[(j['execution_key'],j['result_sha']) for j in (audio_job,vad_job) if j]
            digest=fingerprint((inputs,snapshot['snapshot_id'],[(j['execution_key'],j['result_sha']) for j in required_jobs],embedding_keys,dict(audio) if audio is not None else {},audio_keys,local_faces,head[0] if head else None))
            if visual_only:
                c.execute('CREATE TABLE IF NOT EXISTS v2_visual_source_closure(source_id TEXT,source_revision TEXT,join_digest TEXT,status TEXT,PRIMARY KEY(source_id,source_revision))')
                previous=c.execute('SELECT * FROM v2_visual_source_closure WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
                if not previous or previous['join_digest']!=digest or previous['status']!='COMPLETE':
                    c.execute("INSERT OR REPLACE INTO v2_visual_source_closure VALUES(?,?,?,'COMPLETE')",(source,revision,digest))
                    self.store.emit(c,'SOURCE_VISUAL_COMPLETE',source,revision,1,{'join_digest':digest,'snapshot_id':snapshot['snapshot_id']})
                return digest
            previous=c.execute('SELECT * FROM v2_source_closure WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if previous and previous['join_digest']==digest and previous['status']=='COMPLETE':return digest
            c.execute("INSERT OR REPLACE INTO v2_source_closure VALUES(?,?,?,'COMPLETE')",(source,revision,digest))
            for event_type in ('SOURCE_INDEXED','SOURCE_COMPLETE'):self.store.emit(c,event_type,source,revision,1,{'join_digest':digest,'snapshot_id':snapshot['snapshot_id']})
        return digest
