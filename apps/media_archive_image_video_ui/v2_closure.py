"""Event router and source-local joins. No inference/resource scheduler."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .v2_selection import SourceSelection,ROOT
from .v2_documents import DocumentChain
from .v2_incremental_index import IncrementalFusionIndex as FusionIndex
from .v2_people_coordinates import DisplayAwarePersonIdentity as PersonIdentity

# Scheduling consumes this contract; V25 implementation/snapshot code is unchanged.
ACTIVE_SELECTION_POLICY = 'V25'
_SELECTION_POLICY_FACTORIES = {'V25': SourceSelection}

def create_active_selection_policy(store, policy=None):
    name = ACTIVE_SELECTION_POLICY if policy is None else policy
    factory = _SELECTION_POLICY_FACTORIES.get(name)
    if factory is None:
        raise ContractError('unsupported_active_selection_policy:'+str(name))
    selection = factory(store)
    if not selection.REQUIRED or not callable(selection.context) or not callable(selection.select):
        raise ContractError('invalid_selection_policy_interface')
    return selection

class ClosurePipeline:
    def __init__(self,store,contracts,selection_config=None, scheduler=None):
        self.store,self.contracts=store,contracts
        self.fanout_consumer='closure-fanout:'+fingerprint(contracts)
        self.selection_config=selection_config
        self.active_selection_policy=ACTIVE_SELECTION_POLICY
        self.selection=create_active_selection_policy(store);self.documents=DocumentChain(store,contracts);self.index=FusionIndex(store)
        # A lineage retirement addresses exact result refs. Without this
        # index every already-empty retirement rescanned the full fusion table.
        with store.connect() as c:
            c.execute('CREATE INDEX IF NOT EXISTS v2_fusion_result_ref ON v2_fusion(result_ref)')
            c.execute('CREATE INDEX IF NOT EXISTS v2_jobs_index_headers ON v2_jobs(execution_key,status,result_sha)')
        self.people=PersonIdentity(store)
        from .v2_projection_current_head import CurrentHeadFinalProjection as FinalProjection
        self.final_projection=FinalProjection(store,contracts)
        # Optional Phase-3 admission observer.  Existing callers retain the
        # audited behavior when no scheduler is supplied; the observer only
        # receives durable artifact-ready notifications.
        self.scheduler = scheduler

    def route(self,c,event):
        if event['event_type']=='FRAME_READY':
            p=event['payload']
            if self.scheduler is not None:
                self.scheduler.artifact_event(event['source_id'], 'FRAME_READY', p)
            for stage in self.selection.REQUIRED:
                policy=self.contracts[stage]
                self.store.enqueue(c,stage,p['frame_id'],policy['revision'],event['source_id'],event['source_revision'],p['derived_sha'],policy,p)
            if 'insightface' in self.contracts:
                policy=self.contracts['insightface']
                self.store.enqueue(c,'insightface',p['frame_id'],policy['revision'],event['source_id'],event['source_revision'],p['derived_sha'],policy,p)

    def _canonicalize_local(self,source,revision):
        """Original central dedup algorithm, source-scoped, pinned cheap evidence.

        This source join is part of Barrier A, not a global-stage wait. Cross-source
        identity reconciliation can run later without blocking this source.
        """
        path=ROOT/'scripts/source_frame_dedup_central_db.py'
        spec=importlib.util.spec_from_file_location('v2_original_dedup',path)
        dedup=importlib.util.module_from_spec(spec);spec.loader.exec_module(dedup)
        with self.store.connect() as c:
            progress=c.execute('SELECT * FROM v2_sources WHERE source_id=? AND revision=?',(source,revision)).fetchone()
            if not progress or progress['eos']!='EOS':raise ContractError('source_eos_required')
            artifacts=[dict(r) for r in c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision))]
            if [a['slot'] for a in artifacts]!=list(range(progress['final_sequence']+1)):raise ContractError('slots_incomplete')
            metadata=c.execute('SELECT * FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            meta=json.loads(metadata['metadata']) if metadata else {}
            relative=meta.get('source_relative_path') or meta.get('source_video_relative_path') or meta.get('relative_path') or (Path(metadata['path']).name if metadata else source)
            media=meta.get('media_type') or meta.get('parent_media_kind') or 'video'
            context=self.selection.context(c,source,revision)
            if media=='image' and not context:raise ContractError('image_selection_context_required')
            rows=[];vectors={};pinned=[]
            for a in artifacts:
                jobs={}
                for stage in self.selection.REQUIRED:
                    job=c.execute('SELECT * FROM v2_jobs WHERE stage=? AND item_id=? ORDER BY revision DESC LIMIT 1',(stage,a['artifact_id'])).fetchone()
                    if not job or job['status']!='DONE' or job['revision']!=self.contracts[stage]['revision']:raise ContractError('required_evidence_incomplete:'+stage)
                    if job['input_sha']!=a['sha']:raise ContractError('artifact_evidence_mismatch')
                    jobs[stage]=json.loads(job['result_json']);pinned.append((job['execution_key'],job['result_sha']))
                rows.append({'visual_unit_id':a['artifact_id'],'derived_id':a['artifact_id'],'source_content_id':source,
                  'visual_file':a['path'],'derived_sha256':a['sha'],'time_position_ms':a['time_ms'],'frame_index':a['slot'] if media=='video' else -1,
                  'source_relative_path':relative,'source_absolute_path':metadata['path'] if metadata else '',
                  'media_type':media,'near_black':bool(jobs['quality'].get('black_rejected'))})
                vectors[a['artifact_id']]=jobs['openclip']['vector']
        static_policy=dedup.load_static_shot_policy(dedup.DEFAULT_STATIC_POLICY_PATH)
        canonical_revision=fingerprint((__import__('hashlib').sha256(path.read_bytes()).hexdigest(),static_policy,pinned))
        identities,groups,summary=dedup.compute_visual_identity(rows,run_id=canonical_revision,max_workers=1,
          decode_backend='pillow',dhash_threshold=dedup.DEFAULT_VISUAL_HASH_THRESHOLD,
          video_time_window_sec=dedup.DEFAULT_VIDEO_TIME_WINDOW_SEC,openclip_vectors=vectors,
          openclip_cosine_threshold=dedup.DEFAULT_OPENCLIP_COSINE_THRESHOLD,static_policy=static_policy)
        if summary['failed_count'] or summary['blocked_decoder_count']:raise ContractError('canonical_signature_failed')
        if context and context['canonical_mappings']:
            for row in identities:
                imported=context['canonical_mappings'].get(row['visual_unit_id'],row['canonical_visual_unit_id'])
                if isinstance(imported,dict):
                    cid=imported['canonical_visual_unit_id'];row.update(imported)
                else:cid=imported
                row.update(canonical_visual_unit_id=cid,eligible_for_heavy_models=int(cid==row['visual_unit_id']),identity_status='canonical' if cid==row['visual_unit_id'] else 'duplicate')
            canonical_revision=fingerprint((canonical_revision,context['canonical_revision'],context['canonical_mappings']))
        by_canonical={}
        for row in identities:by_canonical.setdefault(row['canonical_visual_unit_id'],[]).append(row)
        # Publish the entire mapping under one DB transaction. Never partially
        # freeze a source while another process is still building its mapping.
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for key,sha in pinned:
                job=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
                if job['status']!='DONE' or job['result_sha']!=sha:raise ContractError('canonical_input_changed')
            for row in identities:
                members=by_canonical[row['canonical_visual_unit_id']]
                data={**row,'duplicate_group_id':row['visual_duplicate_group_id'],
                  'duplicate_reverse_member_count':len(members),
                  'duplicate_reverse_visual_unit_ids':'|'.join(sorted(m['visual_unit_id'] for m in members)),
                  'group_start_ms':min(m['time_position_ms'] for m in members),'group_end_ms':max(m['time_position_ms'] for m in members)}
                old=c.execute('SELECT * FROM v2_canonical WHERE source_id=? AND source_revision=? AND frame_id=?',(source,revision,row['visual_unit_id'])).fetchone()
                serialized=json.dumps(data,sort_keys=True)
                if old and old['canonical_revision']!=canonical_revision:
                    # Versioned replacement is auditable; the prior candidate
                    # snapshot retains the full old mapping in its input_json.
                    self.store.emit(c,'CANONICAL_REVISION_SUPERSEDED',source,revision,1,{'frame_id':row['visual_unit_id'],'old_revision':old['canonical_revision'],'new_revision':canonical_revision})
                c.execute('INSERT OR REPLACE INTO v2_canonical VALUES(?,?,?,?,?,?)',(source,revision,row['visual_unit_id'],row['canonical_visual_unit_id'],canonical_revision,serialized))

    def canonicalize(self,source,revision):
        with self.store.connect() as c:context=self.selection.context(c,source,revision)
        for sid,rev in sorted((context['dependencies'] if context else {source:revision}).items()):
            self._canonicalize_local(sid,rev)
        if context:
            # Reverse membership is part of V25 input, including alias sources.
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                mappings=[dict(r) for sid,rev in context['dependencies'].items() for r in c.execute('SELECT * FROM v2_canonical WHERE source_id=? AND source_revision=?',(sid,rev))]
                for row in mappings:
                    data=json.loads(row['row_json']);members=[json.loads(m['row_json']) for m in mappings if m['canonical_id']==row['canonical_id']]
                    data.update(duplicate_reverse_member_count=len(members),duplicate_reverse_visual_unit_ids='|'.join(sorted(m['visual_unit_id'] for m in members)),
                                group_start_ms=min(m['time_position_ms'] for m in members),group_end_ms=max(m['time_position_ms'] for m in members))
                    c.execute('UPDATE v2_canonical SET row_json=? WHERE source_id=? AND source_revision=? AND frame_id=?',(json.dumps(data,sort_keys=True),row['source_id'],row['source_revision'],row['frame_id']))

    def advance_events(self, *, cooperative=False, yield_hook=None):
        # A single event includes no-op notifications for this consumer. At
        # real-task scale, one-event quanta stranded candidate/frame events
        # behind thousands of audit markers while model workers were idle.
        # Drain a bounded quantum, retaining the existing yield boundaries,
        # transaction-per-event fencing and inbox idempotency.
        limit = 8 if cooperative else 100
        subscribed=getattr(self.store,'deliver_subscribed',None)
        if getattr(self,'external_frame_fanout',False):progress=0
        elif subscribed:
            progress=subscribed(self.fanout_consumer,self.route,('FRAME_READY',),limit=limit)
        else:progress = self.store.deliver(self.fanout_consumer,self.route,limit=limit)
        if yield_hook is not None: yield_hook()
        if subscribed:
            planner=getattr(self.store,'fanout_planner',None)
            if planner:progress+=planner.promote()
            callback=(lambda c,e:planner.documents(c,e,self.documents)) if planner else self.documents.callback
            progress+=subscribed(self.documents.consumer,callback,
                ('CANDIDATE_READY','SOURCE_READY','AUDIO_ARTIFACT_READY','EVIDENCE_READY'),limit=limit)
            if planner:progress+=planner.promote()
        else:progress += self.documents.advance(limit=limit)
        if yield_hook is not None: yield_hook()
        progress += self.final_projection.reconcile_heads(limit=1 if cooperative else limit, yield_hook=yield_hook)
        progress += self.index.advance(limit=limit)
        if yield_hook is not None: yield_hook()
        return progress

    def _advance_source(self,source,revision,*,yield_hook=None):
        with self.store.connect() as c:self.store.assert_source_epoch(c,source,revision)
        # Returns WAITING for unmet dependencies; never treats failures as empty
        # detections, silent defaults, normal EOS, or source completion.
        for _ in range(1 if yield_hook is not None else 100):
            if not self.advance_events(cooperative=yield_hook is not None, yield_hook=yield_hook):break
        self.people.ingest_ready(source,revision)
        if yield_hook is not None:yield_hook()
        self.people.build_source(source,revision)
        if yield_hook is not None:yield_hook()
        from .v2_people import ReconciliationChanged
        try:self.people.reconcile_source(source,revision,yield_hook=yield_hook)
        except ReconciliationChanged as exc:
            return {'status':'WAITING','reason':str(exc),'reason_code':'PERSON_EVIDENCE_CHANGED'}
        self.index.advance(limit=8 if yield_hook is not None else 100)
        if yield_hook is not None: yield_hook()
        required={stage:self.contracts[stage]['revision'] for stage in self.selection.REQUIRED}
        try:
            self.canonicalize(source,revision)
            snapshot,result=self.selection.select(source,revision,required,self.selection_config)
        except ContractError as exc:return {'status':'WAITING','reason':str(exc)}
        for _ in range(1 if yield_hook is not None else 100):
            if not self.advance_events(cooperative=yield_hook is not None, yield_hook=yield_hook):break
        try:
            try:projection_ready=self.final_projection.advance(source,revision,snapshot)
            except ContractError as exc:return {'status':'FAILED_FINAL','reason_code':'final_projection_contract_failed','reason':str(exc),'snapshot_id':snapshot}
            if not projection_ready:raise ContractError('final_projection_dependencies_incomplete')
            for _ in range(1 if yield_hook is not None else 100):
                if not self.advance_events(cooperative=yield_hook is not None, yield_hook=yield_hook):break
            face=self.contracts.get('insightface')
            person=face if face and face.get('required_for_completion',True) else None
            visual_digest=self.index.close_source(source,revision,self.selection,required,person,self.selection_config,require_final_projection=True,visual_only=True)
            digest=self.index.close_source(source,revision,self.selection,required,person,self.selection_config,require_final_projection=True)
            return {'status':'COMPLETE','join_digest':digest,'snapshot_id':snapshot}
        except ContractError as exc:return {'status':'WAITING','reason':str(exc),'snapshot_id':snapshot,**({'visual_status':'COMPLETE','visual_digest':visual_digest} if 'visual_digest' in locals() else {})}

    def advance_source(self,source,revision,*,yield_hook=None):
        from .v2_timelapse_selection_context import prepare_context
        try:prepare_context(self.selection,source,revision)
        except ContractError as exc:
            return {'status':'WAITING','reason':str(exc)}
        from .v2_source_current_closure import current_closed_outcome
        closed=current_closed_outcome(self,source,revision)
        if closed is not None:
            perf=getattr(self.final_projection,'performance',None)
            if perf:perf.count('CURRENT_SOURCE_CLOSURE_SKIP')
            if yield_hook is not None:yield_hook()
            return closed
        from .v2_raw_audio_lane import close_audio_only
        audio_only=close_audio_only(self,source,revision,yield_hook)
        if audio_only is not None:return self.store.record_outcome(source,revision,audio_only)
        with self.store.connect() as c:
            context=self.selection.context(c,source,revision)
        dependencies=context['dependencies'] if context else {source:revision}
        required=set(self.selection.REQUIRED)|{'qwen','ocr','audio_extract','vad','whisper','embedding'}
        if self.contracts.get('insightface',{}).get('required_for_completion',bool(self.contracts.get('insightface'))):required.add('insightface')
        retrying=None
        visual_outcome=None
        for sid,rev in dependencies.items():
            self.store.reap_expired(sid,rev)
            with self.store.connect() as c:
                progress=c.execute('SELECT eos,error FROM v2_sources WHERE source_id=? AND revision=?',(sid,rev)).fetchone()
                if progress and progress['eos']=='ABORTED':
                    return self.store.record_outcome(source,revision,{'status':'ABORTED','reason_code':'producer_aborted','reason':progress['error'],'dependency_source':sid})
                from .v2_source_lineage import current_source_rows
                jobs=sorted(current_source_rows(c,sid,rev,"j.status IN ('FAILED_FINAL','FAILED_RETRYABLE')"),key=lambda j:(j['stage'] or '',j['item_id'] or ''))
            for job in jobs:
                if job['stage'] not in required:continue
                if job['stage'] in ('audio_extract','vad','whisper') or (job['stage']=='embedding' and json.loads(job['payload']).get('document_type')=='whisper'):
                    if visual_outcome is None:
                        visual_outcome=self._advance_source(source,revision,yield_hook=yield_hook)
                if job['status']=='FAILED_FINAL':
                    return self.store.record_outcome(source,revision,{'status':'FAILED_FINAL','reason_code':'required_worker_failed_final','stage':job['stage'],'execution_key':job['execution_key'],'reason':job['error'],'attempts':job['attempts'],'dependency_source':sid})
                retrying=job
        if retrying:
            return self.store.record_outcome(source,revision,{'status':'RETRYING','reason_code':'required_worker_retryable','stage':retrying['stage'],'execution_key':retrying['execution_key'],'reason':retrying['error']})
        return self.store.record_outcome(source,revision,self._advance_source(source,revision,yield_hook=yield_hook))
