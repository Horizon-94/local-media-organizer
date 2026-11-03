"""Source barrier + Original V25 adapter. Frozen scoring is imported, never copied."""
from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
from .v2_producer import fingerprint
from .v2_runtime import ContractError

ROOT=Path(__file__).resolve().parents[2]
V25_PATH=ROOT/'scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py'
CONFIG_PATH=ROOT/'configs/stop03_2_high_value_policy_v25.json'

def selection_code_hash():
    """Include the adapter veto in immutable selection snapshot identity."""
    import hashlib
    return fingerprint({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (V25_PATH, Path(__file__),
                                     Path(__file__).with_name('v2_candidate_quality.py'))})

def artifact_reference_matches(artifact,visual_file):
    """Historical preview aliases may name identical bytes, never new evidence."""
    if not artifact:return False
    if artifact['path']==visual_file:return True
    import hashlib
    try:
        return all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==artifact['sha']
                   for p in (artifact['path'],visual_file))
    except OSError:return False

def revoke_candidate_completion(c,source,revision):
    """Revoke BOTH completion authorities in the amendment transaction."""
    from .v2_visual_closure import invalidate_visual
    invalidate_visual(c,source,revision)
    previous=c.execute('SELECT * FROM v2_source_outcomes WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_source_closure'").fetchone():
        c.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?",(source,revision))
    if previous:
        payload={'status':'WAITING','reason_code':'candidate_metadata_reconciled','reason':'candidate selection and index proof require reconciliation'}
        c.execute("UPDATE v2_source_outcomes SET status='WAITING',reason_code=?,execution_key='',payload=? WHERE source_id=? AND source_revision=?",(payload['reason_code'],json.dumps(payload,sort_keys=True),source,revision))
    return dict(previous) if previous else None

def original_v25():
    spec=importlib.util.spec_from_file_location('closure_original_v25',V25_PATH)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def select_original(canonical,raw_by_source,image_inputs,config=None,lineage=None):
    v25=original_v25(); config=config or json.loads(CONFIG_PATH.read_text())
    from .v2_candidate_quality import low_detail_ids, POLICY
    canonical = copy.deepcopy(canonical)
    rejected = low_detail_ids(canonical)
    for row in canonical:
        if row['visual_unit_id'] in rejected:
            row['candidate_quality_rejected'] = POLICY
    hashes={'script_sha256':selection_code_hash(),
            'config_sha256':fingerprint(config),'rule_document_sha256':__import__('hashlib').sha256((ROOT/'docs/pipeline_rules/STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md').read_bytes()).hexdigest()}
    # Adapter snapshot IDs are not production run IDs. Pin their actual
    # evidence content rather than inventing a constant run lineage.
    lineage=lineage or {
        'central_dedup_run_id':'adapter-snapshot:'+fingerprint([(r['visual_unit_id'],r.get('canonical_visual_unit_id'),r.get('dhash')) for r in canonical]),
        'yoloe_run_id':'adapter-snapshot:'+fingerprint([(r['visual_unit_id'],r.get('labels')) for r in canonical]),
        'openclip_run_id':'adapter-snapshot:'+fingerprint([(r['visual_unit_id'],r.get('vector')) for r in canonical])}
    return v25.select_candidates(copy.deepcopy(canonical),copy.deepcopy(raw_by_source),config,
                                 'source-selection-'+fingerprint((canonical,raw_by_source))[:20],
                                 'source_scoped',hashes,lineage,copy.deepcopy(image_inputs))

def existing_source_inputs(db,source):
    """Read old V25 evidence without modifying the database or loading models.

    TEMP views scope the old SQL loaders; main remains SQLite mode=ro.
    Payload rows belonging to other sources are skipped, not fed to selection.
    This validates selection equivalence, not historical zero-detection lineage.
    """
    import sqlite3
    v25=original_v25();cfg=json.loads(CONFIG_PATH.read_text())
    c=sqlite3.connect('file:'+str(Path(db).resolve())+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    quoted="'"+source.replace("'","''")+"'"
    try:
        for table in ['source_assets','canonical_source_assets_for_heavy','canonical_visual_units_for_heavy']:
            c.execute('CREATE TEMP VIEW '+table+' AS SELECT * FROM main.'+table+' WHERE source_content_id='+quoted)
        c.execute('CREATE TEMP VIEW visual_labels AS SELECT l.* FROM main.visual_labels l JOIN main.visual_units v ON v.visual_unit_id=l.visual_unit_id WHERE v.source_content_id='+quoted)
        raw,by_source=v25.load_raw_visual_metadata(c)
        rows=v25.load_canonical_visuals(c,by_source)
        stats=v25.attach_signatures(rows,cfg)
        if stats['grid_signature_failed_count']:raise ContractError('legacy_derived_signature_missing')
        by_id={r['visual_unit_id']:r for r in rows}
        refs=[dict(r) for r in c.execute('SELECT e.* FROM embeddings e JOIN main.visual_units v ON v.visual_unit_id=e.visual_unit_id WHERE v.source_content_id=?',(source,))]
        expected={r['embedding_id']:r for r in refs}
        for path in {r['vector_key'].removeprefix('jsonl:').split('#',1)[0] for r in refs}:
            with Path(path).open() as f:
                for line in f:
                    if not line.strip():continue
                    value=json.loads(line)
                    if value.get('embedding_id') not in expected:continue
                    eid=value['embedding_id'];visual=value['visual_unit_id']
                    if expected[eid]['visual_unit_id']!=visual:raise ContractError('legacy_vector_identity_mismatch')
                    if visual in by_id:by_id[visual]['vector']=v25.normalize_vector(value['vector'])
        if any(not r.get('vector') for r in rows):raise ContractError('legacy_vectors_missing')
        v25.load_labels(c,by_id,cfg)
        image={'finder_tags':[],'source_assets':[],'visual_mappings':[],'timelapse_rows':[]}
        return rows,by_source,image
    finally:c.close()

class SourceSelection:
    REQUIRED=('yoloe','openclip','dhash','quality','geometry')
    def __init__(self,store):
        self.store=store
        with store.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS v2_selection_contexts(source_id TEXT,source_revision TEXT,payload TEXT,PRIMARY KEY(source_id,source_revision));
            CREATE TABLE IF NOT EXISTS v2_snapshot_evidence(snapshot_id TEXT,execution_key TEXT,PRIMARY KEY(snapshot_id,execution_key));
            CREATE TABLE IF NOT EXISTS v2_selection_metadata_revisions(
              metadata_revision TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
              previous_context_sha TEXT,payload TEXT,reason TEXT);
            CREATE TABLE IF NOT EXISTS v2_candidate_binding_history(
              candidate_id TEXT,snapshot_id TEXT,payload TEXT,status TEXT,
              PRIMARY KEY(candidate_id,snapshot_id));
            CREATE TABLE IF NOT EXISTS v2_canonical(source_id TEXT,source_revision TEXT,frame_id TEXT,
              canonical_id TEXT,canonical_revision TEXT,row_json TEXT,PRIMARY KEY(source_id,source_revision,frame_id));
            CREATE TABLE IF NOT EXISTS v2_selection_snapshots(snapshot_id TEXT PRIMARY KEY,source_id TEXT,
              source_revision TEXT,evidence_digest TEXT,canonical_revision TEXT,policy_hash TEXT,code_hash TEXT,
              input_json TEXT,result_json TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS v2_selected_candidates(candidate_id TEXT PRIMARY KEY,snapshot_id TEXT,
              source_id TEXT,source_revision TEXT,frame_id TEXT,purpose TEXT,payload TEXT,status TEXT);
            """)

    def canonical(self,source,revision,frame_id,canonical_id,canonical_revision,row_metadata=None):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT * FROM v2_canonical WHERE source_id=? AND source_revision=? AND frame_id=?',(source,revision,frame_id)).fetchone()
            values=(canonical_id,canonical_revision,json.dumps(row_metadata or {},sort_keys=True))
            if existing and tuple(existing[k] for k in ['canonical_id','canonical_revision','row_json'])!=values:
                raise ContractError('canonical_revision_requires_new_processing_revision')
            c.execute('INSERT OR IGNORE INTO v2_canonical VALUES(?,?,?,?,?,?)',(source,revision,frame_id,*values))

    def _local_inputs(self,source,revision,required_revisions,config=None):
        if set(required_revisions)!=set(self.REQUIRED):raise ContractError('selection_required_contract_missing')
        v25=original_v25();cfg=config or json.loads(CONFIG_PATH.read_text())
        with self.store.connect() as c:
            c.execute('BEGIN')
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
            if active and active[0]!=revision:raise ContractError('selection_dependency_revision_inactive')
            progress=c.execute('SELECT * FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
            if not progress or progress['eos']!='EOS':raise ContractError('source_eos_required')
            slots=list(c.execute('SELECT slot,status FROM v2_sample_slots WHERE source_id=? AND source_revision=? ORDER BY slot',(source,revision)))
            if [s['slot'] for s in slots]!=list(range(progress['final_sequence']+1)) or any(s['status']!='READY' for s in slots):raise ContractError('sample_slots_incomplete')
            artifacts=[dict(r) for r in c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision))]
            for a in artifacts:
                mapping=c.execute('SELECT derived_id FROM v2_artifact_mappings WHERE visual_unit_id=?',(a['artifact_id'],)).fetchone()
                if not mapping:raise ContractError('derived_visual_mapping_missing')
                a['derived_id']=mapping[0]
            if [r['slot'] for r in artifacts]!=list(range(progress['final_sequence']+1)) or any(r['status']!='READY' for r in artifacts):raise ContractError('sample_slots_incomplete')
            canonical={r['frame_id']:dict(r) for r in c.execute('SELECT * FROM v2_canonical WHERE source_id=? AND source_revision=?',(source,revision))}
            if set(canonical)!={a['artifact_id'] for a in artifacts}:raise ContractError('canonical_evidence_missing')
            canonical_revisions={r['canonical_revision'] for r in canonical.values()}
            if len(canonical_revisions)>1:raise ContractError('mixed_canonical_revision')
            evidence={};keys=[];stage_keys={stage:[] for stage in self.REQUIRED}
            for a in artifacts:
                evidence[a['artifact_id']]={}
                for stage,rev in required_revisions.items():
                    job=c.execute('SELECT * FROM v2_jobs WHERE stage=? AND item_id=? AND revision=?',(stage,a['artifact_id'],rev)).fetchone()
                    latest=c.execute('SELECT MAX(revision) FROM v2_jobs WHERE stage=? AND item_id=?',(stage,a['artifact_id'])).fetchone()[0]
                    if not job or job['status']!='DONE' or latest!=rev or job['input_sha']!=a['sha']:
                        raise ContractError('required_evidence_incomplete:'+stage)
                    evidence[a['artifact_id']][stage]=json.loads(job['result_json'])
                    keys.append((job['execution_key'],job['result_sha']))
                    stage_keys[stage].append((job['execution_key'],job['result_sha']))
        rows=[];raw=[]
        for a in artifacts:
            cid=canonical[a['artifact_id']];metadata=json.loads(cid['row_json'])
            base={**metadata,'visual_unit_id':a['artifact_id'],'source_content_id':source,'derived_id':a['derived_id'],
                  'sampled_sequence_index':a['slot'] if metadata.get('media_type','video')=='video' else -1,
                  'frame_index':metadata.get('frame_index',a['slot']),'time_position_ms':a['time_ms'] if metadata.get('media_type','video')=='video' else -1,
                  'media_type':metadata.get('media_type','video'),'source_relative_path':metadata.get('source_relative_path',source),
                  'visual_file':a['path'],'derived_sha256':a['sha'],'canonical_visual_unit_id':cid['canonical_id'],
                  'canonical_time_ms':metadata.get('canonical_time_ms',a['time_ms']),
                  'group_start_ms':metadata.get('group_start_ms',a['time_ms']),'group_end_ms':metadata.get('group_end_ms',a['time_ms']),
                  'identity_status':metadata.get('identity_status','unique' if cid['canonical_id']==a['artifact_id'] else 'duplicate'),
                  'eligible_for_heavy_models':int(cid['canonical_id']==a['artifact_id'] and metadata.get('eligible_for_heavy_models',True)),
                  'blocked_reason':metadata.get('blocked_reason',''),
                  'duplicate_group_id':metadata.get('duplicate_group_id',''),'labels':[], 'width':a['width'],'height':a['height']}
            raw.append(base)
            if cid['canonical_id']!=a['artifact_id'] or not base['eligible_for_heavy_models']:continue
            e=evidence[a['artifact_id']]
            quality=e['quality']
            if quality.get('signature_status')!='PASS' or 'grid' not in quality:raise ContractError('v25_quality_signature_missing')
            base.update(quality);base['dhash']=e['dhash']['dhash'];base['vector']=v25.normalize_vector(e['openclip']['vector'])
            base['sharpness_score'] = e['dhash'].get('sharpness_score')
            base['sharpness_score_contract'] = e['dhash'].get('contract')
            # Reuse V25's own label normalization and category mapping.
            import sqlite3
            with sqlite3.connect(':memory:') as labels:
                labels.row_factory=sqlite3.Row
                labels.execute('CREATE TABLE visual_labels(label_id INTEGER,visual_unit_id TEXT,label TEXT,confidence REAL,bbox TEXT,run_id TEXT)')
                for i,d in enumerate(e['yoloe']['detections']):
                    labels.execute('INSERT INTO visual_labels VALUES(?,?,?,?,?,?)',(i,a['artifact_id'],d['label'],d['confidence'],json.dumps(d['bbox']),'adapter-snapshot:'+fingerprint(stage_keys['yoloe'])))
                if e['yoloe']['detections']:v25.load_labels(labels,{a['artifact_id']:base},cfg)
            base['generic_label_categories']=sorted({d['category'] for d in base['labels'] if d['category'] not in {'other','weak_background',''}})
            rows.append(base)
        image_inputs={'finder_tags':[],'source_assets':[],'visual_mappings':[],'timelapse_rows':[]}
        return {'canonical':rows,'raw_by_source':{source:raw} if any(r['media_type']=='video' for r in raw) else {},'image_inputs':image_inputs,
                'evidence_keys':keys,'canonical_revision':next(iter(canonical_revisions),'empty'),
                'lineage':{'central_dedup_run_id':'adapter-snapshot:'+fingerprint(sorted((k,v['canonical_revision'],v['row_json']) for k,v in canonical.items())),
                           'yoloe_run_id':'adapter-snapshot:'+fingerprint(stage_keys['yoloe']),
                           'openclip_run_id':'adapter-snapshot:'+fingerprint(stage_keys['openclip'])},
                'required_revisions':required_revisions,'config':cfg}

    def context(self,c,source,revision):
        from .v2_shared_payload import load
        amended=c.execute('SELECT payload FROM v2_selection_metadata_revisions WHERE source_id=? AND source_revision=? ORDER BY rowid DESC LIMIT 1',(source,revision)).fetchone()
        if amended:return load(c,amended[0])
        row=c.execute('SELECT payload FROM v2_selection_contexts WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        return load(c,row[0]) if row else None

    def reconcile_missing_finder_metadata(self,source,revision,image_inputs):
        """Append a metadata-only revision for a previously unselected image.

        Original producer/visual evidence and immutable contexts stay intact.
        This deliberately refuses sources with existing heavy candidates:
        their valid work must not be invalidated by a metadata-only repair.
        """
        from .v2_task_materializer import _candidate_source_assets
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.store.assert_source_epoch(c,source,revision)
            old=self.context(c,source,revision)
            if not old:raise ContractError('finder_reconcile_context_missing')
            before=old['image_inputs']
            if set(image_inputs)!=set(before):raise ContractError('finder_reconcile_context_schema_changed')
            for key in ('visual_mappings','timelapse_rows'):
                if before[key]!=image_inputs[key]:raise ContractError('finder_reconcile_visual_identity_changed')
            if _candidate_source_assets(before['source_assets'])!=image_inputs['source_assets']:
                raise ContractError('finder_reconcile_source_inventory_changed')
            previous_tags={json.dumps(t,sort_keys=True) for t in before['finder_tags']}
            if not previous_tags<={json.dumps(t,sort_keys=True) for t in image_inputs['finder_tags']}:
                raise ContractError('finder_reconcile_existing_tag_removed')
            if not any(t.get('source_content_id')==source for t in image_inputs['finder_tags']):
                raise ContractError('finder_reconcile_source_not_tagged')
            if before==image_inputs:return fingerprint((source,revision,old))
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
            eos=c.execute('SELECT eos FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
            if not active or active[0]!=revision or not eos or eos[0]!='EOS':raise ContractError('finder_reconcile_current_eos_missing')
            if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND status='RUNNING'",(source,)).fetchone():raise ContractError('finder_reconcile_worker_active')
            if c.execute("SELECT 1 FROM v2_selected_candidates WHERE source_id=? AND source_revision=? AND status<>'SUPERSEDED'",(source,revision)).fetchone():raise ContractError('finder_reconcile_existing_candidate_preservation_required')
            amended={**old,'image_inputs':copy.deepcopy(image_inputs)}
            metadata_revision=fingerprint((source,revision,amended))
            c.execute('INSERT OR IGNORE INTO v2_selection_metadata_revisions VALUES(?,?,?,?,?,?)',
                      (metadata_revision,source,revision,fingerprint(old),json.dumps(amended,sort_keys=True),'missing_finder_metadata_v1'))
            previous_outcome=revoke_candidate_completion(c,source,revision)
            self.store.emit(c,'SELECTION_METADATA_RECONCILED',source,revision,1,
                            {'metadata_revision':metadata_revision,'previous_context_sha':fingerprint(old),'previous_outcome':previous_outcome,'producer_rerun':False,'visual_evidence_rerun':False})
            return metadata_revision

    def bind_context(self,dependencies,image_inputs,*,canonical_mappings=None,canonical_revision=None):
        """Immutable dependency-closed snapshot from existing A9T/V25 contracts.

        Include all sequence members, sidecars and canonical owners/aliases.
        Existing cross-source canonical identity may be imported, never inferred
        or changed here. Every visual reference must resolve to a pinned artifact.
        """
        if set(image_inputs)!={'finder_tags','source_assets','visual_mappings','timelapse_rows'}:
            raise ContractError('image_context_incomplete')
        if canonical_mappings and not canonical_revision:raise ContractError('canonical_context_lineage_missing')
        payload={'dependencies':dict(sorted(dependencies.items())),'image_inputs':image_inputs,
                 'canonical_mappings':canonical_mappings or {},'canonical_revision':canonical_revision}
        serialized=json.dumps(payload,sort_keys=True)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ids={r[0] for source,revision in dependencies.items() for r in c.execute('SELECT artifact_id FROM v2_artifacts WHERE source_id=? AND revision=?',(source,revision))}
            refs={r['visual_unit_id'] for r in image_inputs['timelapse_rows']}
            refs.update(x for row in image_inputs['visual_mappings'] for x in (row['visual_unit_id'],row['canonical_visual_unit_id']))
            refs.update(canonical_mappings or {})
            refs.update(v['canonical_visual_unit_id'] if isinstance(v,dict) else v for v in (canonical_mappings or {}).values())
            if not refs<=ids:raise ContractError('canonical_or_sequence_dependency_missing')
            known={r['source_content_id'] for r in image_inputs['source_assets']}
            if not set(dependencies)<=known:raise ContractError('source_assets_context_missing')
            for source,revision in dependencies.items():
                old=self.context(c,source,revision)
                if old and json.dumps(old,sort_keys=True)!=serialized:raise ContractError('context_requires_new_processing_revision')
                from .v2_shared_payload import dump
                c.execute('INSERT OR IGNORE INTO v2_selection_contexts VALUES(?,?,?)',(source,revision,dump(c,payload)))

    def reconcile_image_context(self,source,revision,dependencies,image_inputs):
        """Append a closed-group metadata revision, without changing evidence.

        This is a correction boundary, not normal Resume. Old contexts remain
        immutable. Every added representative must be a current sealed image.
        Candidate/result preservation is performed by select(preserve_results=True).
        """
        from .v2_task_materializer import _candidate_source_assets
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.store.assert_source_epoch(c,source,revision)
            old=self.context(c,source,revision)
            if not old:raise ContractError('image_reconcile_context_missing')
            before=old['image_inputs']
            if set(image_inputs)!=set(before):raise ContractError('image_reconcile_schema_changed')
            if _candidate_source_assets(before['source_assets'])!=image_inputs['source_assets']:
                raise ContractError('image_reconcile_inventory_changed')
            if any(dependencies.get(s)!=r for s,r in old['dependencies'].items()):
                raise ContractError('image_reconcile_existing_dependency_changed')
            for key in ('finder_tags','timelapse_rows','visual_mappings'):
                normalize=lambda rows:{json.dumps(r,sort_keys=True) for r in rows}
                if not normalize(before[key])<=normalize(image_inputs[key]):
                    raise ContractError('image_reconcile_existing_metadata_removed:'+key)
            groups={r['sequence_id'] for r in before['timelapse_rows']}
            if not groups or len(groups)!=1 or {r['sequence_id'] for r in image_inputs['timelapse_rows']}!=groups:
                raise ContractError('image_reconcile_unrelated_sequence')
            representative_sources={r['parent_source_content_id'] for r in image_inputs['timelapse_rows']}
            if set(dependencies)!=representative_sources:
                raise ContractError('image_reconcile_dependency_not_representative')
            for sid,rev in dependencies.items():
                active=c.execute('SELECT a.source_revision,s.eos FROM v2_active_sources a JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision WHERE a.source_id=?',(sid,)).fetchone()
                if not active or tuple(active)!=(rev,'EOS'):raise ContractError('image_reconcile_current_eos_missing')
                if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND status='RUNNING'",(sid,)).fetchone():
                    raise ContractError('image_reconcile_worker_active')
                for row in image_inputs['timelapse_rows']:
                    if row['parent_source_content_id']==sid:
                        artifact=c.execute("SELECT path,sha FROM v2_artifacts WHERE source_id=? AND revision=? AND artifact_id=? AND status='READY'",(sid,rev,row['visual_unit_id'])).fetchone()
                        if not artifact_reference_matches(artifact,row['visual_file']):
                            raise ContractError('image_reconcile_artifact_identity_changed')
            amended={**old,'dependencies':dict(sorted(dependencies.items())),'image_inputs':copy.deepcopy(image_inputs)}
            digest=fingerprint((source,revision,amended))
            if amended==old:return digest
            c.execute('INSERT OR IGNORE INTO v2_selection_metadata_revisions VALUES(?,?,?,?,?,?)',
                (digest,source,revision,fingerprint(old),json.dumps(amended,sort_keys=True),'closed_timelapse_context_v1'))
            previous_outcome=revoke_candidate_completion(c,source,revision)
            self.store.emit(c,'SELECTION_METADATA_RECONCILED',source,revision,1,
                {'metadata_revision':digest,'previous_context_sha':fingerprint(old),'previous_outcome':previous_outcome,'reason':'closed_timelapse_context_v1'})
            return digest

    def frozen_inputs(self,source,revision,required_revisions,config=None):
        with self.store.connect() as c:context=self.context(c,source,revision)
        dependencies=context['dependencies'] if context else {source:revision}
        parts=[self._local_inputs(s,r,required_revisions,config) for s,r in sorted(dependencies.items())]
        result=parts[0]
        if context:
            result={**result,'canonical':[row for part in parts for row in part['canonical']],
                    'raw_by_source':{s:rows for part in parts for s,rows in part['raw_by_source'].items()},
                    'image_inputs':context['image_inputs'],
                    'evidence_keys':[key for part in parts for key in part['evidence_keys']],
                    'canonical_revision':fingerprint([part['canonical_revision'] for part in parts]),
                    'lineage':{key:'adapter-snapshot:'+fingerprint([part['lineage'][key] for part in parts]) for key in result['lineage']},
                    'context':context}
        return result

    def select(self,source,revision,required_revisions,config=None,*,preserve_results=False,reuse_contracts=None):
        repair_contracts=getattr(self,'repair_reuse_contracts',None)
        if repair_contracts is not None:
            preserve_results=True
            reuse_contracts=repair_contracts
        inputs=self.frozen_inputs(source,revision,required_revisions,config)
        result=select_original(inputs['canonical'],inputs['raw_by_source'],inputs['image_inputs'],inputs['config'],inputs['lineage'])
        # A shared image sequence is evaluated as a whole; each source owns
        # only its canonical candidates, preventing duplicate cross-source work.
        for key in ('q_rows','o_rows'):result[key]=[row for row in result[key] if row['source_content_id']==source]
        evidence_digest=fingerprint(inputs);code_hash=selection_code_hash()
        snapshot_id=fingerprint((source,revision,evidence_digest,code_hash))
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # Recheck all pinned evidence after selection; a revision arriving
            # during CPU selection must not publish a stale candidate set.
            for key,sha in inputs['evidence_keys']:
                job=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
                if job['status']!='DONE' or job['result_sha']!=sha:raise ContractError('evidence_changed_during_selection')
            repaired=c.execute("SELECT n.result_json FROM v2_selection_snapshots n WHERE n.snapshot_id=? AND n.status='DONE' AND EXISTS (SELECT 1 FROM v2_selected_candidates a JOIN v2_candidate_binding_history h USING(candidate_id) WHERE a.snapshot_id=n.snapshot_id AND a.status='DONE')",(snapshot_id,)).fetchone()
            if repaired:
                if fingerprint(json.loads(repaired[0]))!=fingerprint(result):raise ContractError('repaired_snapshot_result_changed')
                return snapshot_id,result
            self.store.emit(c,'SOURCE_READY_FOR_SELECTION',source,revision,1,{'evidence_digest':evidence_digest,'canonical_revision':inputs['canonical_revision'],'selection_policy_hash':fingerprint(inputs['config'])})
            obsolete=[r[0] for r in c.execute("SELECT snapshot_id FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? AND snapshot_id<>? AND status='DONE'",(source,revision,snapshot_id))]
            retained={}
            if preserve_results:
                for candidate in c.execute("SELECT * FROM v2_selected_candidates WHERE source_id=? AND source_revision=? AND status='DONE'",(source,revision)):
                    matching=[r for r in result['q_rows' if candidate['purpose']=='qwen' else 'o_rows'] if r['visual_unit_id']==candidate['frame_id']]
                    if not matching:continue
                    old_payload=json.loads(candidate['payload']);new_payload=matching[0]
                    if any(old_payload.get(k)!=new_payload.get(k) for k in ('visual_unit_id','visual_file','derived_sha256','media_type','source_content_id')):
                        raise ContractError('candidate_reuse_inference_input_changed')
                    job=c.execute("SELECT * FROM v2_current_jobs WHERE stage=? AND item_id=? AND status='DONE' ORDER BY revision DESC LIMIT 1",(candidate['purpose'],candidate['candidate_id'])).fetchone()
                    if not job or not job['result_sha'] or not job['result_json']:
                        raise ContractError('candidate_reuse_durable_result_missing')
                    if not reuse_contracts or json.loads(job['provenance'])!=reuse_contracts.get(candidate['purpose']):
                        raise ContractError('candidate_reuse_model_contract_changed')
                    if fingerprint(json.loads(job['result_json']))!=job['result_sha']:
                        raise ContractError('candidate_reuse_durable_result_corrupt')
                    retained[(candidate['purpose'],candidate['frame_id'])]=dict(candidate)
                    c.execute('INSERT OR IGNORE INTO v2_candidate_binding_history VALUES(?,?,?,?)',(candidate['candidate_id'],candidate['snapshot_id'],candidate['payload'],candidate['status']))
            for old in obsolete:
                from .v2_visual_closure import invalidate_visual
                invalidate_visual(c,source,revision)
                c.execute("UPDATE v2_selection_snapshots SET status='SUPERSEDED' WHERE snapshot_id=?",(old,))
                for candidate in c.execute('SELECT * FROM v2_selected_candidates WHERE snapshot_id=?',(old,)).fetchall():
                    if (candidate['purpose'],candidate['frame_id']) not in retained:
                        c.execute("UPDATE v2_selected_candidates SET status='SUPERSEDED' WHERE candidate_id=?",(candidate['candidate_id'],))
                from .v2_lineage import invalidate
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_final_projection_heads'").fetchone():
                    invalidate(c,[r[0] for r in c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE snapshot_id=?',(old,))])
                invalidate(c,[r[0] for r in c.execute("SELECT execution_key FROM v2_jobs WHERE item_id IN (SELECT candidate_id FROM v2_selected_candidates WHERE snapshot_id=? AND status='SUPERSEDED')",(old,))])
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_source_closure'").fetchone():
                    c.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?",(source,revision))
            from .v2_shared_payload import dump
            c.execute('INSERT OR IGNORE INTO v2_selection_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',(snapshot_id,source,revision,evidence_digest,inputs['canonical_revision'],fingerprint(inputs['config']),code_hash,dump(c,inputs),json.dumps(result,sort_keys=True),'DONE'))
            c.executemany('INSERT OR IGNORE INTO v2_snapshot_evidence VALUES(?,?)',[(snapshot_id,key) for key,sha in inputs['evidence_keys']])
            for purpose,key in [('qwen','q_rows'),('ocr','o_rows')]:
                for row in result[key]:
                    saved=retained.get((purpose,row['visual_unit_id']))
                    if saved:
                        payload={**row,'candidate_id':saved['candidate_id'],'snapshot_id':snapshot_id,'purpose':purpose}
                        c.execute('UPDATE v2_selected_candidates SET snapshot_id=?,payload=? WHERE candidate_id=?',(snapshot_id,json.dumps(payload,sort_keys=True),saved['candidate_id']))
                        self.store.emit(c,'CANDIDATE_RESULT_REUSED',source,revision,1,{'candidate_id':saved['candidate_id'],'old_snapshot_id':saved['snapshot_id'],'new_snapshot_id':snapshot_id,'model_rerun':False})
                        continue
                    cid=fingerprint((snapshot_id,row['candidate_id']))
                    payload={**row,'candidate_id':cid,'snapshot_id':snapshot_id,'purpose':purpose}
                    c.execute("INSERT OR IGNORE INTO v2_selected_candidates VALUES(?,?,?,?,?,?,?,'READY')",(cid,snapshot_id,source,revision,row['visual_unit_id'],purpose,json.dumps(payload,sort_keys=True)))
                    self.store.emit(c,'CANDIDATE_READY',source,revision,1,payload)
        return snapshot_id,result
