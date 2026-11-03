"""Versioned final visual semantics using the unchanged legacy policy algorithms.

Early documents remain available until this source's final quality/propagation
snapshot is published. Models are never loaded here. Temporary SQLite is solely
a compatibility input for the read-only legacy propagation function.
"""
from __future__ import annotations
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from .v2_selection import ROOT
from .v2_producer import fingerprint
from .v2_runtime import ContractError

SCRIPTS=ROOT/'scripts/03_stop03_visual_analysis'
NAMES={'quality':'stop03_5a_joint_db_quality_audit_v1','propagation':'stop03_5c_qwenvl_yolo_propagation_v1','documents':'stop03_5d_text_embedding_db_contract_v1'}

def legacy(name):
    spec=importlib.util.spec_from_file_location('v2_final_'+name,SCRIPTS/(NAMES[name]+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def build_projection(artifacts,canonical,jobs,metadata,directory,quality_peers=None):
    """Transport V2 persisted results to legacy quality/propagation/document rules."""
    quality=legacy('quality');prop=legacy('propagation');docs=legacy('documents')
    qconfig=quality.load_config(ROOT/'configs/stop03_5a_joint_quality_audit_v1.json')
    direct=[];audits={};queue={};by_id={a['artifact_id']:a for a in artifacts}
    for stage in ('qwen','ocr'):
        rows=[]
        for job in jobs:
            if job['stage']!=stage:continue
            payload=json.loads(job['payload']);result=json.loads(job['result_json']);aid=payload['artifact_id'];a=by_id[aid]
            text=result['text'] if 'text' in result else json.dumps(result,ensure_ascii=False,sort_keys=True);raw=result.get('raw',{})
            # V2 stores raw outputs in the transaction, instead of legacy output
            # files. Materialize that exact envelope for legacy integrity checks.
            path=directory/(job['execution_key']+'.json');path.write_text(job['result_json'])
            metrics=directory/(job['execution_key']+'.metrics');metrics.write_text('{}')
            common=dict(candidate_id=job['item_id'],evidence_id=job['execution_key'],source_content_id=a['source_id'],visual_unit_id=aid,
                canonical_visual_unit_id=canonical[aid],derived_id=aid,candidate_role=payload.get('role',''),reason_codes='',policy_version='',runtime_visual_file_sha256=a['sha'],attempt_count=job['attempts'])
            queue[job['item_id']]=common
            row={**common,'item_status':'success','result_status':'success'}
            if stage == 'qwen':
                row['output_schema'] = result.get('qwen_schema_version', '')
            if stage=='qwen':row.update(clean_text=text,clean_text_sha256=quality.sha256_text(text),truncation_status=raw.get('truncation_status','MISSING'),cleanup_status=raw.get('cleanup_status','MISSING'),
                runtime_metrics_json='{}',raw_stdout_path=str(path),raw_stdout_sha256=quality.sha256_file(path),stderr_path=str(path),stderr_sha256=quality.sha256_file(path),metrics_path=str(metrics),metrics_sha256=quality.sha256_file(metrics),generation_tokens=raw.get('generation_tokens'))
            else:
                lines=result.get('lines',[]);scores=[l.get('score',l.get('confidence')) for l in lines];scores=[v for v in scores if v is not None]
                row.update(ocr_text=text,ocr_text_sha256=quality.sha256_text(text),ocr_lines_json=json.dumps(lines),ocr_line_count=len(lines),output_json_path=str(path),output_json_sha256=quality.sha256_file(path),
                    mean_confidence=sum(scores)/len(scores) if scores else None,min_confidence=min(scores) if scores else None,max_confidence=max(scores) if scores else None,reused_existing_result=False)
                if not text.strip():row.update(item_status='no_text',result_status='no_text')
            rows.append(row)
        context=None
        if quality_peers is not None:
            context=[]
            for peer in quality_peers:
                if peer['stage']!=stage:continue
                value=json.loads(peer['result_json'])
                context.append({'candidate_id':peer['item_id'],'clean_text' if stage=='qwen' else 'ocr_text':value['text'] if 'text' in value else json.dumps(value,ensure_ascii=False,sort_keys=True)})
        checked=(quality.audit_qwen if stage=='qwen' else quality.audit_ocr)(rows,queue,qconfig,duplicate_context=context)
        for row,check in zip(rows,checked):
            audits[row['evidence_id']]=check
            # A failed raw semantic contract cannot silently become a final
            # searchable document. REVIEW is the legacy exclusion contract.
            if check['quality_status']=='FAIL':raise ContractError('final_quality_failed:'+check['hard_fail_reasons'])
            a=by_id[row['derived_id']];meta=metadata[a['source_id']]
            direct.append({**row,'staging_run_id':'v2','modality':'qwenvl' if stage=='qwen' else stage,'quality_status':check['quality_status'],
                'evidence_text':row['clean_text'] if stage=='qwen' else row['ocr_text'],'evidence_text_sha256':row.get('clean_text_sha256',row.get('ocr_text_sha256')),
                'derived_type':'video_frame_jpg1280' if meta['media_type']=='video' else 'image_preview_jpg1280',
                'frame_index':a['slot'] if meta['media_type']=='video' else -1,'time_position_ms':a['time_ms'],'media_type':meta['media_type'],'relative_path':meta['relative_path']})
    db=directory/'compat.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript('''CREATE TABLE stop03_5_unified_evidence_runs(staging_run_id,status,created_at,qwen_run_id,ocr_run_id);
        INSERT INTO stop03_5_unified_evidence_runs VALUES('v2','success','0','v2','v2');
        CREATE TABLE derived_assets(derived_id,source_content_id,frame_index,time_position_ms,sha256,derived_type);
        CREATE TABLE visual_units(visual_unit_id,derived_id);
        CREATE TABLE visual_identity(visual_unit_id,canonical_visual_unit_id);
        CREATE TABLE visual_labels(visual_unit_id,label,confidence);
        CREATE TABLE visual_label_terms(label PRIMARY KEY,label_zh,search_terms_json);
        CREATE TABLE stop03_5_unified_evidence_items(staging_run_id,modality,quality_status,evidence_id,candidate_id,source_content_id,visual_unit_id,canonical_visual_unit_id,derived_id,evidence_text,evidence_text_sha256);''')
        for a in artifacts:
            aid=a['artifact_id'];kind=metadata[a['source_id']]['media_type']
            c.execute('INSERT INTO derived_assets VALUES(?,?,?,?,?,?)',(aid,a['source_id'],a['slot'],a['time_ms'],a['sha'],'video_frame_jpg1280' if kind=='video' else 'image_preview_jpg1280'))
            c.execute('INSERT INTO visual_units VALUES(?,?)',(aid,aid));c.execute('INSERT INTO visual_identity VALUES(?,?)',(aid,canonical[aid]))
        for j in jobs:
            if j['stage']!='yoloe':continue
            r=json.loads(j['result_json'])
            if r['detections'] and not r.get('label_terms'):raise ContractError('final_label_registry_missing')
            for term in r.get('label_terms',[]):
                value=(term['label'],term['label_zh'],term['search_terms_json'])
                old=c.execute('SELECT * FROM visual_label_terms WHERE label=?',(term['label'],)).fetchone()
                if old and old!=value:raise ContractError('final_label_registry_conflict')
                c.execute('INSERT OR IGNORE INTO visual_label_terms VALUES(?,?,?)',value)
            for d in r['detections']:c.execute('INSERT INTO visual_labels VALUES(?,?,?)',(j['item_id'],d['label'],d['confidence']))
        columns=[r[1] for r in c.execute('PRAGMA table_info(stop03_5_unified_evidence_items)')]
        c.executemany('INSERT INTO stop03_5_unified_evidence_items VALUES('+','.join('?' for _ in columns)+')',[[r[k] for k in columns] for r in direct])
    summary,propagation,_=prop.build_distribution(db,ROOT/'configs/stop03_5c_qwenvl_yolo_propagation_v1.json')
    if summary['status']!='PASS':raise ContractError('final_propagation_failed')
    for row in propagation:
        a=by_id[row['target_derived_id']];meta=metadata[a['source_id']]
        row.update(derived_type='video_frame_jpg1280',media_type=meta['media_type'],relative_path=meta['relative_path'])
    config=json.loads((ROOT/'configs/stop03_5d_text_embedding_db_contract_v1.json').read_text())
    documents,excluded=docs.aggregate_documents(direct,propagation,{'staging_run_id':'v2'},{'propagation_run_id':summary['propagation_run_id']},config)
    return {'documents':documents,'quality':audits,'excluded':excluded,'propagation':propagation}


class FinalProjection:
    def __init__(self,store,contracts):
        self.store,self.contracts=store,contracts
        with store.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS v2_final_projection_heads(source_id,source_revision,execution_key,snapshot_id,PRIMARY KEY(source_id,source_revision));
            CREATE TABLE IF NOT EXISTS v2_final_projection_documents(execution_key,document_key,PRIMARY KEY(execution_key,document_key));''')

    def advance(self,source,revision,snapshot):
        with self.store.connect() as c:
            candidates=list(c.execute('SELECT * FROM v2_selected_candidates WHERE snapshot_id=?',(snapshot,)))
            parents=[];jobs=[]
            for item in candidates:
                j=c.execute("SELECT * FROM v2_current_jobs WHERE stage=? AND item_id=? ORDER BY revision DESC LIMIT 1",(item['purpose'],item['candidate_id'])).fetchone()
                if not j or j['status']!='DONE':return False
                jobs.append(dict(j));parents.append(j['execution_key'])
            # Include every sampled slot: propagation addresses non-candidates.
            artifacts=[dict(a) for a in c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision))]
            canonical={r['frame_id']:r['canonical_id'] for r in c.execute('SELECT * FROM v2_canonical WHERE source_id=? AND source_revision=?',(source,revision))}
            evidence_keys=[r[0] for r in c.execute('SELECT execution_key FROM v2_snapshot_evidence WHERE snapshot_id=?',(snapshot,))]
            # One recursive current-lineage expansion, not one per parent.
            # Retain exactly the old DONE/current filter and missing-parent gate.
            evidence={j['execution_key']:dict(j) for j in c.execute("""SELECT * FROM v2_current_jobs
              WHERE execution_key IN (SELECT value FROM json_each(?)) AND status='DONE'""",(json.dumps(evidence_keys),))}
            if set(evidence_keys)!=set(evidence):return False
            provider=getattr(self.store,'selection_parent_provider',None)
            if provider is not None:
                parents.extend(provider(c,{'source_id':source,'source_revision':revision,'payload':{'snapshot_id':snapshot}}))
            else:parents.extend(evidence_keys)
            jobs.extend(evidence[key] for key in evidence_keys)
            meta=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            meta=json.loads(meta[0]) if meta else {};metadata={source:{'media_type':meta.get('media_type',meta.get('parent_media_kind','video')),'relative_path':meta.get('source_relative_path',meta.get('source_video_relative_path',meta.get('relative_path',source)))}}
            policy=fingerprint({str(p.relative_to(ROOT)):fingerprint(p.read_text()) for p in [Path(__file__),SCRIPTS/'qwenvl_raw_media_v3.py',*[SCRIPTS/(n+'.py') for n in NAMES.values()],*[ROOT/'configs'/name for name in ('stop03_5a_joint_quality_audit_v1.json','stop03_5c_qwenvl_yolo_propagation_v1.json','stop03_5d_text_embedding_db_contract_v1.json')]]})
            from .v2_projection_peers import compact_peers
            peers=compact_peers(c)
            peer_keys=[j['execution_key'] for j in peers]
            if getattr(self.store,'targeted_projection_invalidation_enabled',False):
                from .v2_projection_peers import relevant_peers
                parents += [j['execution_key'] for j in relevant_peers(peers,jobs)]
            else:parents+=peer_keys
            digest=fingerprint((snapshot,sorted(set(parents)),policy))
            old=c.execute('SELECT h.* FROM v2_final_projection_heads h JOIN v2_current_jobs j USING(execution_key) WHERE h.source_id=? AND h.source_revision=? AND j.input_sha=?',(source,revision,digest)).fetchone()
            if old:return True
        with tempfile.TemporaryDirectory(prefix='v2-final-') as directory:
            result=build_projection(artifacts,canonical,jobs,metadata,Path(directory),quality_peers=peers)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if peer_keys!=[j[0] for j in c.execute("SELECT execution_key FROM v2_current_jobs WHERE stage IN ('qwen','ocr') AND status='DONE' ORDER BY execution_key")]:return False
            if not c.execute("SELECT 1 FROM v2_selection_snapshots WHERE snapshot_id=? AND status='DONE'",(snapshot,)).fetchone():return False
            valid={r[0] for r in c.execute("""SELECT execution_key FROM v2_current_jobs
              WHERE execution_key IN (SELECT value FROM json_each(?)) AND status='DONE'""",(json.dumps(parents),))}
            if valid!=set(parents):return False
            if c.execute("SELECT 1 FROM v2_final_projection_heads h JOIN v2_current_jobs j USING(execution_key) WHERE h.source_id=? AND h.source_revision=? AND j.input_sha=?",(source,revision,digest)).fetchone():return True
            manifest_item=source+':'+revision
            generation=c.execute("SELECT COALESCE(MAX(revision),0)+1 FROM v2_jobs WHERE stage='final_manifest' AND item_id=?",(manifest_item,)).fetchone()[0]
            contract={'revision':1,'policy':policy,'kind':'legacy-final-semantic-projection'}
            def persist(stage,item,value):
                key=self.store.enqueue(c,stage,item,generation,source,revision,digest,contract,{},dependencies=parents)
                c.execute("UPDATE v2_jobs SET status='DONE',result_json=?,result_sha=? WHERE execution_key=?",(json.dumps(value,sort_keys=True),fingerprint(value),key));return key
            previous=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if previous:
                from .v2_lineage import invalidate
                invalidate(c,[previous[0]])
            head=persist('final_manifest',manifest_item,result)
            for doc in result['documents']:
                key=persist('final_projection',doc['derived_id'],doc)
                c.execute('INSERT OR IGNORE INTO v2_job_dependencies VALUES(?,?)',(key,head))
                did=fingerprint(('final',key));aid=doc['derived_id'];text=doc['embedding_text'];t=doc['time_position_ms']
                c.execute("INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'READY')",(did,source,revision,aid,t,t,'final_visual',key,text,json.dumps({'contract':contract,'legacy_document':doc},sort_keys=True),generation))
                c.execute('INSERT OR IGNORE INTO v2_final_projection_documents VALUES(?,?)',(head,key))
                self.store.enqueue(c,'embedding',did,self.contracts['embedding']['revision'],source,revision,fingerprint(text),self.contracts['embedding'],{'document_id':did,'text':text,'document_type':'final_visual'},dependencies=[key])
                self.store.emit(c,'DOCUMENT_READY',source,revision,generation,{'document_id':did,'document_type':'final_visual','text_sha':fingerprint(text)})
            c.execute('INSERT OR REPLACE INTO v2_final_projection_heads VALUES(?,?,?,?)',(source,revision,head,snapshot))
        return True

    def reconcile_heads(self, limit=100, yield_hook=None):
        """Late project evidence can change the legacy duplicate quality gate."""
        # Reconciliation is expensive and shares the scheduler thread. Never
        # restart at the first head: bounded quanta must eventually visit all
        # sources, including those added while processing is in flight.
        cursor = getattr(self, '_reconcile_cursor', ('', ''))
        scope=getattr(self,'repair_source_scope',None)
        if scope is not None and not scope:return 0
        scope_clause=(' AND h.source_id IN ('+','.join('?' for _ in scope)+')') if scope is not None else ''
        scope_args=tuple(sorted(scope)) if scope is not None else ()
        with self.store.connect() as c:
            query = """SELECT h.* FROM v2_final_projection_heads h
                JOIN v2_selection_snapshots s USING(snapshot_id)
                WHERE s.status='DONE' AND (h.source_id,h.source_revision)>(?,?)
                """+scope_clause+" ORDER BY h.source_id,h.source_revision LIMIT ?"
            heads=[dict(r) for r in c.execute(query, (*cursor, *scope_args,limit))]
            if not heads and cursor != ('', ''):
                heads=[dict(r) for r in c.execute(query, ('', '', *scope_args,limit))]
        if heads:
            self._reconcile_cursor = (heads[-1]['source_id'], heads[-1]['source_revision'])
        changed=0
        for h in heads:
            scope=getattr(self,'repair_source_scope',None)
            if scope is not None and h['source_id'] not in scope:continue
            try:self.advance(h['source_id'],h['source_revision'],h['snapshot_id'])
            except ContractError as exc:
                self.store.record_outcome(h['source_id'],h['source_revision'],{'status':'FAILED_FINAL','reason_code':'final_projection_contract_failed','reason':str(exc)})
                continue
            with self.store.connect() as c:
                current=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(h['source_id'],h['source_revision'])).fetchone()
            changed+=int(current[0]!=h['execution_key'])
            if yield_hook is not None:
                yield_hook()
        return changed
