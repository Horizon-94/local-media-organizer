"""Source-scoped detached SQL computation, then fenced prepared mutations.

SQLite in-memory transactions below are a private calculation workspace, not
the durable writer. Never back up/copy the complete project database.
"""
import copy
from contextlib import contextmanager
import json
import re
import sqlite3
import types
import zlib
import time,uuid
from .video_telemetry import append
from .video_commit_queue import SQLCommand,WriteProposal,StaleProposal
from .video_coordination import tokens,guards
from .video_result_reader import row_factory,JSON_COLUMNS
from .v2_runtime import ContractError,WorkerStore
from .video_canonical_store import encode,sha,prepare_result
from .video_result_reader import reference


class PlanningConnection(sqlite3.Connection):
    recording=False
    def execute(self,sql,parameters=()):
        result=super().execute(sql,parameters)
        if self.recording and re.match(r'^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE)\b',sql,re.I):
            self.commands.append(SQLCommand(sql,tuple(parameters)))
        return result
    def executemany(self,sql,parameters):
        for values in parameters:self.execute(sql,values)
        return self.cursor()


def reference_commands(memory,commands,source):
    """Pre-encode coordination bodies/refs outside the durable writer."""
    identities={row[0]:row[1] for row in memory.execute('SELECT result_sha,result_id FROM canonical_result')}
    bodies=[];replacements={}
    for number,command in enumerate(commands):
        match=re.search(r'UPDATE\s+v2_jobs\s+SET\b.*?result_json\s*=\s*\?',command.sql,re.I|re.S)
        indices=[]
        if match:indices=[command.sql[:match.end()].count('?')-1]
        elif re.match(r'\s*INSERT.*?INTO\s+v2_selection_snapshots\s+VALUES',command.sql,re.I):indices=[7,8]
        for index in indices:
            text=command.parameters[index]
            if not isinstance(text,str):continue
            value=json.loads(text)
            if isinstance(value,dict) and set(value)=={'$canonical_result'}:continue
            digest=sha(encode(value));identities[digest]=digest
            bodies.append((digest,value))
            replacements[number,index]=json.dumps(reference(digest),sort_keys=True)
    def pack(node):
        if isinstance(node,(dict,list)):
            if isinstance(node,dict) and set(node)=={'$canonical_result'}:return node
            digest=sha(encode(node))
            if digest in identities:return reference(identities[digest])
            if isinstance(node,dict):return {key:pack(value) for key,value in node.items()}
            return [pack(value) for value in node]
        return node
    output=[]
    for digest,value in bodies:
        stored={key:pack(item) for key,item in value.items()} if isinstance(value,dict) else [pack(item) for item in value] if isinstance(value,list) else value
        output.extend(prepare_result('coordination','v1',value,source=source,storage_value=stored).proposal.commands)
    for number,command in enumerate(commands):
        json_indices=set()
        insert=re.search(r'INTO\s+(\w+)\s*(\([^)]*\))?\s+VALUES\s*\((.*)\)',command.sql,re.I|re.S)
        if insert:
            columns=[name.strip().strip('"') for name in insert[2][1:-1].split(',')] if insert[2] else [row[1] for row in memory.execute('PRAGMA table_info('+insert[1]+')')]
            position=0
            for column,expression in zip(columns,insert[3].split(',')):
                if expression.strip()=='?' and column in ('payload','result_json','input_json','provenance','artifact_json','segments_json','row_json'):
                    json_indices.add(position)
                position+=expression.count('?')
        else:
            prefix=command.sql.split('WHERE',1)[0]
            for match in re.finditer(r'\b(payload|result_json|input_json|provenance|artifact_json|segments_json|row_json)\s*=\s*\?',prefix,re.I):
                json_indices.add(prefix[:match.end()].count('?')-1)
        parameters=[]
        for index,value in enumerate(command.parameters):
            if (number,index) in replacements:value=replacements[number,index]
            elif index in json_indices and isinstance(value,str) and value.startswith(('{','[')):
                try:decoded=json.loads(value)
                except json.JSONDecodeError:decoded=None
                if decoded is not None:
                    value=json.dumps(pack(decoded),sort_keys=True)
            parameters.append(value)
        output.append(SQLCommand(command.sql,tuple(parameters)))
    return tuple(output)


def source_snapshot(store,source,revision,operation=None,index_events=None):
    started=time.perf_counter();timings={'identity_lookup_ms':0.,'lineage_lookup_ms':0.,'sql_fetch_ms':0.,'json_reference_tree_ms':0.}
    memory=sqlite3.connect(':memory:',factory=PlanningConnection)
    memory.commands=[]
    try:
        with store.connect() as reader:
            reader.execute('BEGIN')
            token=tokens(reader,source)
            scoped=index_events is not None
            # Optional frontend-declared, bounded semantic siblings. VIDEO
            # stores have no provider and retain the exact source-local path.
            # IMAGE timelapse comparison needs the 1..3 real representatives,
            # never every member or a project snapshot.
            provider=getattr(store,'snapshot_source_scope',None)
            siblings=provider(reader,source,revision,operation) if provider and not scoped else []
            if len(siblings)>2 or source in siblings:raise ContractError('snapshot_sibling_scope_not_bounded')
            local_scope=json.dumps([source,*siblings])
            roots=set();doc_ids=set();frame_ids=set()
            if scoped:
                for event in index_events:
                    p=event['payload']
                    if p.get('execution_key'):roots.add(p['execution_key'])
                    if p.get('document_id'):doc_ids.add(p['document_id'])
                    if p.get('item_id'):frame_ids.add(p['item_id'])
                    if p.get('artifact_id'):frame_ids.add(p['artifact_id'])
                if operation=='consume_batch:video-stream-documents-v1':
                    # Opt-in document preparation roots, never all source bodies.
                    for row in reader.execute('SELECT payload FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?))',(json.dumps(sorted(roots)),)):
                        payload=json.loads(row[0])
                        if payload.get('document_id'):doc_ids.add(payload['document_id'])
                        if payload.get('artifact_id'):frame_ids.add(payload['artifact_id'])
                    for row in reader.execute('SELECT document_id,raw_result_ref,embedding_ref,artifact_id FROM v2_documents WHERE document_id IN (SELECT value FROM json_each(?)) OR raw_result_ref IN (SELECT value FROM json_each(?))',(json.dumps(sorted(doc_ids)),json.dumps(sorted(roots)))):
                        doc_ids.add(row[0]);roots.update(k for k in (row[1],row[2]) if k)
                        if row[3]:frame_ids.add(row[3])
                    roots.update(row[0] for row in reader.execute("SELECT execution_key FROM v2_jobs WHERE stage='embedding' AND item_id IN (SELECT value FROM json_each(?))",(json.dumps(sorted(doc_ids)),)))
                for row in reader.execute('SELECT raw_result_ref,embedding_ref,artifact_id FROM v2_documents WHERE document_id IN (SELECT value FROM json_each(?))',(json.dumps(sorted(doc_ids)),)):
                    roots.update(key for key in (row[0],row[1]) if key)
                    if row[2]:frame_ids.add(row[2])
            timings['identity_lookup_ms']=(time.perf_counter()-started)*1000
            master=list(reader.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('table','index','view')"))
            virtual={row[1] for row in master if row[0]=='table' and 'CREATE VIRTUAL TABLE' in row[2].upper()}
            shadow={name+suffix for name in virtual for suffix in ('_data','_idx','_content','_docsize','_config')}
            for kind,name,sql in master:
                if kind=='table' and name not in shadow:memory.execute(sql)
            # Only actual ancestors; projection additionally needs exact-text
            # peers, selected through an indexed immutable fact catalog.
            peers=[]
            if operation in ('final_projection','visual_closure','source_closure'):
                from .video_peer_catalog import matching
                peers=matching(reader,source,revision)
            memory.peer_membership=peers
            lineage_started=time.perf_counter()
            seed="execution_key IN (SELECT value FROM json_each(?))" if scoped else "(source_id=? AND source_revision=?) OR execution_key IN (SELECT value FROM json_each(?))"
            seed_params=(json.dumps(sorted(roots)),) if scoped else (source,revision,json.dumps([r[0] for r in peers]))
            if siblings:
                seed+=" OR (source_id IN (SELECT value FROM json_each(?)) AND stage IN ('yoloe','openclip','dhash','quality','geometry'))"
                seed_params=(*seed_params,json.dumps(siblings))
            keys=[row[0] for row in reader.execute("""WITH RECURSIVE required(key) AS (
                SELECT execution_key FROM v2_jobs WHERE """+seed+"""
                UNION SELECT d.parent_key FROM required r JOIN v2_job_dependencies d ON d.child_key=r.key)
                SELECT key FROM required""",seed_params)]
            key_json=json.dumps(keys)
            external={row[0]:tokens(reader,row[0])[0] for row in reader.execute(
                'SELECT DISTINCT source_id FROM v2_jobs WHERE execution_key IN (SELECT value FROM json_each(?)) AND source_id<>?',(key_json,source))}
            timings['lineage_lookup_ms']=(time.perf_counter()-lineage_started)*1000
            artifacts=json.dumps(sorted(frame_ids)) if scoped else json.dumps([row[0] for row in reader.execute(
                'SELECT artifact_id FROM v2_artifacts WHERE source_id IN (SELECT value FROM json_each(?))' if siblings else 'SELECT artifact_id FROM v2_artifacts WHERE source_id=?',
                (local_scope,) if siblings else (source,))])
            docs=json.dumps(sorted(doc_ids)) if scoped else json.dumps([row[0] for row in reader.execute('SELECT document_id FROM v2_documents WHERE source_id=?',(source,))])
            canonical=json.dumps([row[0] for row in reader.execute('''SELECT json_extract(result_json,'$."$canonical_result"') FROM v2_jobs
                WHERE execution_key IN (SELECT value FROM json_each(?)) AND result_json IS NOT NULL''',(json.dumps(sorted(roots)) if scoped else key_json,)) if row[0]])
            for kind,name,sql in master:
                if kind!='table' or name in shadow:continue
                columns={row[1] for row in reader.execute('PRAGMA table_info("'+name+'")')}
                predicate='';params=()
                if name=='v2_jobs':predicate='execution_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                elif name=='canonical_result':predicate='result_id IN (SELECT value FROM json_each(?))';params=(canonical,)
                elif name=='v2_job_dependencies':predicate='child_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                elif name=='v2_active_sources':predicate='source_id=? OR source_id IN (SELECT value FROM json_each(?))';params=(source,json.dumps(sorted(external)))
                elif name=='video_peer_text_v1':predicate='execution_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                elif name=='v2_artifact_mappings':predicate='artifact_id IN (SELECT value FROM json_each(?))';params=(artifacts,)
                elif name=='v2_observation_heads':predicate='artifact_id IN (SELECT value FROM json_each(?))';params=(artifacts,)
                elif name in ('v2_tracklet_membership','v2_person_review_observations'):
                    predicate='observation_id IN (SELECT observation_id FROM v2_person_observations WHERE source_id=?)';params=(source,)
                elif name=='v2_tracklet_people':predicate='tracklet_id IN (SELECT tracklet_id FROM v2_tracklets WHERE source_id=?)';params=(source,)
                elif name=='v2_global_memberships':predicate='source_person_id IN (SELECT source_person_id FROM v2_source_people WHERE source_id=?)';params=(source,)
                elif name=='v2_final_projection_documents':predicate='execution_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                elif name=='v2_candidate_binding_history':predicate='candidate_id IN (SELECT candidate_id FROM v2_selected_candidates WHERE source_id=?)';params=(source,)
                elif name=='v2_retry_requests':predicate='execution_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                elif name=='video_stream_change_tokens_v1':predicate="scope='manual' OR scope=? OR scope IN (SELECT 'source:'||value FROM json_each(?))";params=('source:'+source,json.dumps(sorted(external)))
                elif 'source_id' in columns:predicate='source_id=?';params=(source,)
                elif name=='v2_outbox':predicate='aggregate_id=?';params=(source+':'+revision,)
                elif name in ('v2_vectors','v2_index_coverage'):
                    predicate='entity_id IN (SELECT value FROM json_each(?)) OR entity_id IN (SELECT value FROM json_each(?)) OR entity_id IN (SELECT value FROM json_each(?))';params=(artifacts,docs,key_json)
                elif name in ('v2_inbox','v2_deliveries'):
                    predicate='event_id IN (SELECT event_id FROM v2_outbox WHERE aggregate_id=?)';params=(source+':'+revision,)
                elif name=='v2_snapshot_evidence':
                    predicate='snapshot_id IN (SELECT snapshot_id FROM v2_selection_snapshots WHERE source_id=?)';params=(source,)
                elif 'job_id' in columns:
                    predicate='job_id IN (SELECT job_id FROM video_stream_jobs_v1 WHERE source_id=?)';params=(source,)
                if siblings and name in ('v2_sources','v2_source_metadata','v2_artifacts','v2_sample_slots','v2_canonical','v2_selection_contexts','v2_source_epochs'):
                    predicate='source_id IN (SELECT value FROM json_each(?))';params=(local_scope,)
                if scoped:
                    if getattr(store,'document_root_snapshots',False):
                        if name=='evidence_header':predicate='execution_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                        elif name=='immutable_payload':
                            predicate='payload_id IN (SELECT provenance_ref FROM evidence_header WHERE execution_key IN (SELECT value FROM json_each(?))) OR payload_id IN (SELECT payload_ref FROM evidence_header WHERE execution_key IN (SELECT value FROM json_each(?)))';params=(key_json,key_json)
                        elif name=='proof_evidence':predicate='proof_id IN (SELECT proof_id FROM selection_proof WHERE source_id=?)';params=(source,)
                        elif name=='candidate_proof':predicate='candidate_key IN (SELECT value FROM json_each(?))';params=(key_json,)
                        elif name=='result_reference':predicate='result_id IN (SELECT value FROM json_each(?))';params=(canonical,)
                    if name=='v2_documents':predicate='document_id IN (SELECT value FROM json_each(?))';params=(docs,)
                    elif name=='v2_artifacts':predicate='artifact_id IN (SELECT value FROM json_each(?))';params=(artifacts,)
                    elif name=='v2_fusion':predicate='result_ref IN (SELECT value FROM json_each(?))';params=(key_json,)
                    elif name in ('v2_outbox','v2_inbox','v2_deliveries'):
                        predicate='event_id IN (SELECT value FROM json_each(?))';params=(json.dumps([e['event_id'] for e in index_events]),)
                    elif name=='v2_text_fts':predicate='document_id IN (SELECT value FROM json_each(?))';params=(docs,)
                    elif name.startswith('video_stream_') and name not in ('video_stream_control_v1','video_stream_sources_v1','video_stream_change_tokens_v1'):
                        predicate='0';params=()
                    elif name in ('v2_selection_snapshots','v2_snapshot_evidence','v2_selected_candidates','v2_candidate_binding_history','v2_final_projection_documents','v2_retry_requests'):
                        predicate='0';params=()
                if scoped and operation=='consume_batch:fusion-index':
                    # Index callbacks use source memberships/aliases, not the
                    # global display registry or native spool ownership table.
                    # Keep schema; avoid copying unrelated project-wide rows.
                    if name in ('v2_global_people','video_spool_owners_v1'):
                        predicate='0';params=()
                    # FTS UNINDEXED document_id otherwise scans all text even
                    # when the event batch has no document inputs.
                    elif name=='v2_text_fts' and not doc_ids:
                        predicate='0';params=()
                query='SELECT * FROM "'+name+'"'+(' WHERE '+predicate if predicate else '')
                # Physical values are copied. The detached business reader
                # resolves refs on demand; never duplicate hydrated bodies.
                fetched=time.perf_counter()
                rows=[tuple(row.values) for row in reader.execute(query,params)]
                timings['sql_fetch_ms']+=(time.perf_counter()-fetched)*1000
                if rows:memory.executemany('INSERT INTO "'+name+'" VALUES('+','.join('?' for _ in rows[0])+')',rows)
            # Snapshot/planning/failure refs are not necessarily job results.
            # Copy their reachable bodies too, never the whole body store.
            json_started=time.perf_counter();pending=set()
            def references(node):
                if isinstance(node,dict):
                    if set(node)=={'$canonical_result'}:pending.add(node['$canonical_result'])
                    else:
                        for value in node.values():references(value)
                elif isinstance(node,list):
                    for value in node:references(value)
            for kind,name,sql in master:
                if kind!='table' or name in shadow:continue
                columns=[row[1] for row in memory.execute('PRAGMA table_info("'+name+'")') if row[1] in JSON_COLUMNS]
                for column in columns:
                    condition=' WHERE execution_key IN (SELECT value FROM json_each(?))' if scoped and name=='v2_jobs' else ''
                    values=(json.dumps(sorted(roots)),) if condition else ()
                    for value, in memory.execute('SELECT "'+column+'" FROM "'+name+'"'+condition,values):
                        if isinstance(value,str) and '$canonical_result' in value:
                            try:references(json.loads(value))
                            except json.JSONDecodeError:pass  # legacy error text
            copied=set()
            while pending-copied:
                batch=pending-copied;copied.update(batch)
                rows=[tuple(row.values) for row in reader.execute('SELECT * FROM canonical_result WHERE result_id IN (SELECT value FROM json_each(?))',(json.dumps(sorted(batch)),))]
                if {row[0] for row in rows}!=batch:raise ValueError('coordination_snapshot_canonical_body_missing')
                for row in rows:
                    memory.execute('INSERT OR IGNORE INTO canonical_result VALUES('+','.join('?' for _ in row)+')',row)
                    references(json.loads(zlib.decompress(row[4])))
            timings['json_reference_tree_ms']=(time.perf_counter()-json_started)*1000
            for kind,name,sql in master:
                if kind in ('index','view'):memory.execute(sql)
        memory.commit();memory.row_factory=row_factory
        from .v2_current_jobs_cache import correlated_predicate
        memory.execute("CREATE TEMP VIEW v2_current_jobs AS SELECT j.* FROM main.v2_jobs j WHERE j.status<>'SUPERSEDED' AND "+correlated_predicate(memory))
        memory.phase_timings={}
        memory.item_timings=[]
        memory.snapshot_stats={
            'jobs_count':memory.execute('SELECT count(*) FROM v2_jobs').fetchone()[0],
            'dependency_edges_count':memory.execute('SELECT count(*) FROM v2_job_dependencies').fetchone()[0],
            'canonical_body_count':memory.execute('SELECT count(*) FROM canonical_result').fetchone()[0],
            'proof_count':memory.execute('SELECT count(*) FROM video_stream_selection_proofs_v1').fetchone()[0],
            'fence_count':len(external)+2,'foreign_source_count':len(external)}
        memory.snapshot_timings={**timings,'snapshot_build_ms':(time.perf_counter()-started)*1000}
        return memory,(token,external)
    except BaseException:
        memory.close();raise



def exact_visual_input_read_set(store, memory, source, revision, operation, *, published=False):
    """First active visual decision: fence its data, not branch activity.

    Historical snapshots and cross-source policy contexts retain their original
    compatibility guards. The current source-local Ready path uses exact reads.
    """
    if operation not in ('canonical_preparation','selection'):return None
    metadata=memory.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
    if not metadata or json.loads(metadata[0]).get('media_type') not in ('image','video'):return None
    if not published and memory.execute('SELECT 1 FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? LIMIT 1',(source,revision)).fetchone():return None
    context=store.adapter.pipeline.selection.context(memory,source,revision)
    if context and set(context.get('dependencies',{}))!={source}:return None
    stages=tuple(store.adapter.pipeline.selection.REQUIRED)
    checks=[]
    def exact(table,where,parameters):
        columns=[r[1] for r in memory.execute('PRAGMA table_info("'+table+'")')]
        if not columns:return
        names=','.join('"'+n+'"' for n in columns)
        sql='SELECT json_group_array(json_array('+names+')) FROM (SELECT '+names+' FROM main."'+table+'" WHERE '+where+' ORDER BY '+names+')'
        checks.append((SQLCommand(sql,parameters),tuple(memory.execute(sql,parameters).fetchone())))
    # Covers source policy/EOS, complete slot/artifact identity, metadata/context
    # revisions, and the pre-existing canonical mappings before calculation.
    for table in ('v2_sources','v2_artifacts','v2_sample_slots','v2_selection_contexts','v2_selection_metadata_revisions','v2_canonical'):
        exact(table,'source_id=?',(source,))
    # Metadata is a mixed-branch envelope: fence visual/identity fields only.
    fields=('media_type','parent_media_kind','source_relative_path','source_video_relative_path','relative_path','pipeline_revision','source_stat','policy_revision','context_revision')
    projection='source_id,source_revision,content_revision,source_sha,path,'+','.join("json_extract(metadata,'$."+field+"')" for field in fields)
    sql='SELECT json_group_array(json_array('+projection+')) FROM (SELECT * FROM main.v2_source_metadata WHERE source_id=? ORDER BY source_id,source_revision)'
    checks.append((SQLCommand(sql,(source,)),tuple(memory.execute(sql,(source,)).fetchone())))
    exact('v2_artifact_mappings','artifact_id IN (SELECT artifact_id FROM v2_artifacts WHERE source_id=?)',(source,))
    roots="WITH RECURSIVE required(k) AS (SELECT execution_key FROM v2_jobs WHERE source_id=? AND stage IN (SELECT value FROM json_each(?)) UNION SELECT d.parent_key FROM required r JOIN v2_job_dependencies d ON d.child_key=r.k) SELECT k FROM required"
    parameters=(source,json.dumps(stages))
    # Full immutable input/result identity plus every transitive edge, including
    # any real external ancestor. An unrelated audio/document row is not a root.
    exact('v2_jobs','execution_key IN ('+roots+')',parameters)
    exact('v2_job_dependencies','child_key IN ('+roots+')',parameters)
    exact('v2_active_sources','source_id IN (SELECT DISTINCT source_id FROM v2_jobs WHERE execution_key IN ('+roots+')) OR source_id=?',(*parameters,source))
    exact('video_stream_source_states_v1',"source_id=? AND state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL','VISUAL_FAILED_FINAL','FIRST_SELECTION_PUBLISHED')",(source,))
    return checks



def visual_candidate_input_read_sets(store,memory,source,revision,operation):
    """Current Candidate callback proof only; no delivery/service policy change.

    Prepare potential checks before calculation, then select the exact consumed
    event from its existing durable inbox proposal. Other event types retain
    their original guards. Other Candidates' model activity is not an input.
    """
    if operation!='consume:video-stream-documents-v1':return {}
    receipt=memory.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='FIRST_SELECTION_PUBLISHED'",(source,revision)).fetchone()
    if not receipt:return {}
    snapshot=json.loads(receipt[0])['snapshot']
    base=exact_visual_input_read_set(store,memory,source,revision,'selection',published=True)
    if base is None:return {}
    def exact(table,where,parameters):
        columns=[r[1] for r in memory.execute('PRAGMA table_info("'+table+'")')]
        names=','.join('"'+n+'"' for n in columns)
        sql='SELECT json_group_array(json_array('+names+')) FROM (SELECT '+names+' FROM main."'+table+'" WHERE '+where+' ORDER BY '+names+')'
        return SQLCommand(sql,parameters),tuple(memory.execute(sql,parameters).fetchone())
    proof=memory.execute('SELECT proof_key FROM video_stream_selection_proofs_v1 WHERE snapshot_id=? AND source_id=? AND source_revision=?',(snapshot,source,revision)).fetchone()
    if not proof:return {}
    shared=base+[exact('v2_selection_snapshots','snapshot_id=?',(snapshot,)),exact('v2_snapshot_evidence','snapshot_id=?',(snapshot,)),exact('video_stream_selection_proofs_v1','snapshot_id=?',(snapshot,)),exact('v2_jobs','execution_key=?',(proof[0],)),exact('v2_job_dependencies','child_key=?',(proof[0],))]
    result={}
    for event_id,payload in memory.execute("SELECT event_id,payload FROM v2_outbox WHERE aggregate_id=? AND event_type='CANDIDATE_READY' AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=v2_outbox.event_id)",(source+':'+revision,)):
        event=json.loads(payload);item=event['payload']
        if item['snapshot_id']!=snapshot:continue
        event_sql="SELECT event_id,aggregate_id,version,event_type,payload AS raw_event_payload,committed_at FROM v2_outbox WHERE event_id=?"
        result[event_id]=shared+[exact('v2_selected_candidates','candidate_id=?',(item['candidate_id'],)),exact('v2_jobs',"item_id=? AND stage IN ('qwen','ocr')",(item['candidate_id'],)),exact('v2_inbox','consumer=? AND event_id=?',('video-stream-documents-v1',event_id)),(SQLCommand(event_sql,(event_id,)),tuple(memory.execute(event_sql,(event_id,)).fetchone()))]
    return result


def run(store,source,revision,operation,compute,event_evidence=None):
    began=time.time();clock=time.perf_counter();proposal_id=uuid.uuid4().hex
    scoped=(operation=='consume_batch:fusion-index' or
            operation=='consume_batch:video-stream-documents-v1' and getattr(store,'document_root_snapshots',False))
    index_phase=getattr(store,'index_phase',None) if operation=='consume_batch:fusion-index' else None
    if index_phase:index_phase('PREPARING')
    memory,(token,external)=source_snapshot(store,source,revision,operation,getattr(store,'proposal_events',None) if scoped else None)
    visual_checks=exact_visual_input_read_set(store,memory,source,revision,operation)
    candidate_checks=visual_candidate_input_read_sets(store,memory,source,revision,operation)
    meta={'proposal_id':proposal_id,'timestamp':began,'source_id':source,'revision':revision,
        'stage':operation,'prep_started_at':began,'snapshot':memory.snapshot_stats,
        'timings':dict(memory.snapshot_timings),**getattr(store,'proposal_context',{})}
    meta.setdefault('trigger_event',None);meta.setdefault('claimed_count',None);meta.setdefault('batch_size',None);meta.setdefault('queue_depth',None)
    shadow=copy.copy(store)
    @contextmanager
    def connect(self):
        try:yield memory;memory.commit()
        except BaseException:memory.rollback();raise
    shadow.connect=types.MethodType(connect,shadow)
    shadow.consume=types.MethodType(WorkerStore.consume,shadow)
    shadow.db=':memory:'
    try:
        memory.recording=True
        compute_started=time.perf_counter();result=compute(shadow,memory)
        meta['timings']['compute_ms']=(time.perf_counter()-compute_started)*1000
        meta['timings']['coverage_check_ms']=memory.phase_timings.get('coverage_check_ms',0.)
        meta['document_service_samples']=list(memory.item_timings)
        meta['timings']['closure_check_ms']=meta['timings']['compute_ms'] if operation in ('source_closure','visual_closure') else 0.
        memory.commit()
        if candidate_checks:
            # The original WorkerStore.consume owns this inbox insert. Match its
            # actual event, never infer an event from queue order or source state.
            consumed=[q.parameters[1] for q in memory.commands
                      if re.match(r'^\s*INSERT(?: OR \w+)? INTO v2_inbox\b',q.sql,re.I)
                      and len(q.parameters)==3 and q.parameters[0]=='video-stream-documents-v1']
            if len(consumed)==1 and consumed[0] in candidate_checks:
                visual_checks=candidate_checks[consumed[0]]
                meta['candidate_visual_proof_fence']=True
        checks=list(guards(store.adapter,source,revision,token))
        if visual_checks is not None:
            checks=checks[:3]+visual_checks
            meta['visual_stage_local_fence']=True
        if operation in ('final_projection','visual_closure','source_closure'):
            from .video_peer_catalog import membership_check
            checks.append(membership_check(source,revision,memory.peer_membership))
        if event_evidence:
            # Independent audio events depend on their immutable current
            # result, not unrelated frame writes to the same source.
            checks=checks[:3]+checks[4:]
            from .video_coordination import current_evidence_check
            checks.extend((current_evidence_check(key),('DONE',digest,source,revision)) for key,digest in event_evidence)
        if visual_checks is None:
            checks.extend((SQLCommand('SELECT COALESCE((SELECT generation FROM video_stream_change_tokens_v1 WHERE scope=?),0)',('source:'+sid,)),(generation,)) for sid,generation in sorted(external.items()))
        meta['snapshot']['fence_count']=len(checks)
        commands=reference_commands(memory,memory.commands,source)
        meta['prep_ended_at']=time.time();meta['prep_ms']=(time.perf_counter()-clock)*1000
        meta['real_dependency_sources']=sorted(external)
        proposal=WriteProposal(operation,source,commands,tuple(checks),proposal_id,json.dumps(meta))
    except BaseException as error:
        meta.update(result='waiting' if isinstance(error,ContractError) else 'prepare_rejected',error_type=type(error).__name__,error_message=str(error),total_ms=(time.perf_counter()-clock)*1000,durable_rows_changed=0)
        from pathlib import Path
        append(Path(store.ledger.path).with_name('index-proposals.jsonl'),meta)
        raise
    finally:memory.close()
    meta['result']='no_mutation';meta['durable_rows_changed']=0
    if not proposal.commands:
        from pathlib import Path
        meta['total_ms']=(time.perf_counter()-clock)*1000
        append(Path(store.ledger.path).with_name('index-proposals.jsonl'),meta)
    if proposal.commands:
        if index_phase:index_phase('COMMITTING')
        waiting=time.perf_counter();future=store.ledger.commit_queue.submit(proposal)
        try:
            meta['durable_rows_changed']=future.result();meta['result']='success'
        except StaleProposal as error:
            meta['result']='stale';meta.update(error.details)
            raise ContractError('coordination_snapshot_changed_retry') from error
        except BaseException:
            meta['result']='rejected';raise
        finally:
            metric=getattr(future,'commit_metrics',{})
            meta['timings']['commit_wait_ms']=(time.perf_counter()-waiting)*1000
            meta['timings']['writer_hold_ms']=metric.get('writer_hold_ms')
            for name in ('queue_wait_ms','fence_check_ms','writer_statement_ms','fts_write_ms','vector_write_ms'):meta['timings'][name]=metric.get(name)
            meta['total_ms']=(time.perf_counter()-clock)*1000
            meta['durable_rows_changed']=metric.get('durable_rows_changed',0)
            from pathlib import Path
            append(Path(store.ledger.path).with_name('index-proposals.jsonl'),meta)
    return result


def pipeline_operation(adapter,source,revision,operation,compute):
    def calculate(store,memory):
        from .v2_closure import ClosurePipeline
        from .video_stream_index import VideoFusionIndex
        memory.recording=False
        pipeline=ClosurePipeline(store,adapter.contracts)
        pipeline.index=VideoFusionIndex(store)
        memory.recording=True
        return compute(pipeline)
    return run(adapter.store,source,revision,operation,calculate)
