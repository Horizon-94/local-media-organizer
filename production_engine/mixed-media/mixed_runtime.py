"""Three explicit frontends over ONE accepted Engine and serialized writer.

MediaView is a policy/context view, never another Runtime or model pool.
"""
import copy,json,threading,time,types
from pathlib import Path
from audio_frontend import AudioFrontendMixin,AudioAdapters,audio_schema,workspace_guard
from apps.media_archive_image_video_ui.video_stream_runtime import Runtime as Engine
from apps.media_archive_image_video_ui.video_stream_adapter import FrozenAdapters,EvidenceStore
from apps.media_archive_image_video_ui.image_stream_runtime import ImageRuntime,ImageAdapters
from apps.media_archive_image_video_ui.image_frontend import ImagePolicy
from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy,fingerprint
from apps.media_archive_image_video_ui.v2_model_workers import sha_file
from apps.media_archive_image_video_ui.v2_runtime import ContractError
from apps.media_archive_image_video_ui.model_lifecycle import ModelLifecycleManager


class MediaView:
    """Explicit frontend bindings, shared engine resources; no __init__ side effects."""
    def __init__(self,runtime,kind,adapter):
        self.runtime=runtime;self.kind=kind;self.adapters=adapter;self.contracts=adapter.contracts
        self.sources=[];self.publishers={}
    def __getattr__(self,name):return getattr(self.runtime,name)
    frame=Engine.frame
    publish_audio=Engine.publish_audio
    model_audio=Engine.model_audio
    trace=ImageRuntime.trace


class RoutedAdapters(FrozenAdapters):
    def _document_route(self,c,event):
        meta=c.execute('SELECT metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(event['source_id'],event['source_revision'])).fetchone()
        if not meta:raise ContractError('MIXED_EVENT_SOURCE_UNKNOWN')
        kind=json.loads(meta[0]).get('media_type')
        adapter=self.media_adapters.get(kind)
        if adapter is None:raise ContractError('MIXED_EVENT_KIND_UNKNOWN')
        p=event['payload']
        if (event['event_type']=='EVIDENCE_READY' and p.get('stage')=='whisper'
                and adapter.contracts['whisper'].get('usable_asr_contract')):
            return adapter.pipeline.documents.callback(c,event)
        return adapter._document_route(c,event)
    def document_events(self,limit=16):return self.document_events_fair(limit=limit)


def clone_adapter(base,cls,contracts,policy):
    a=object.__new__(cls);a.__dict__.update(base.__dict__)
    a.contracts=contracts;a.store=copy.copy(base.store);a.store.adapter=a;a.store.policy=policy
    a.pipeline=copy.copy(base.pipeline);a.pipeline.store=a.store;a.pipeline.contracts=contracts
    for name in ('documents','index','selection','people','final_projection'):
        part=copy.copy(getattr(base.pipeline,name));part.store=a.store
        if hasattr(part,'contracts'):part.contracts=contracts
        setattr(a.pipeline,name,part)
    from apps.media_archive_image_video_ui.video_selection_proof import parent
    a.store.selection_parent_provider=lambda c,event:parent(a.store,c,event)
    if cls is ImageAdapters:a.store.snapshot_source_scope=ImageRuntime.snapshot_source_scope
    return a


class MixedRuntime(Engine):
    def __init__(self,workspace,contracts_by_kind,handlers,*,plan,resume=False,fixture_mode=False,resource_monitor=None,expected_sources=(),incremental_changed=()):
        workspace_guard(workspace,fixture_mode)
        super().__init__(workspace,contracts_by_kind['video'],handlers,resume=resume,fixture_mode=fixture_mode,
                         resource_monitor=resource_monitor,environment={'VIDEO_PIPELINE_V2_STREAMING':'1'})
        # The mixed runtime is the actual build151 Resume construction path.
        # Reconcile once here, after the shared ledger/adapter is live.
        self.audio_reconciliation=self.adapters.pipeline.documents.reconcile_audio_registration(limit=4096)
        (self.root/'audio-reconciliation-result.json').write_text(json.dumps(self.audio_reconciliation,ensure_ascii=False,sort_keys=True)+'\n')
        self.plan=plan;self.image_rows={r['source_content_id']:r for r in plan['rows']}
        self.incremental_changed=set(incremental_changed)
        self.decisions={r['parent_source_content_id']:r for r in plan['decisions']}
        self.expected_sources={r['source_id']:r['media_kind'] for r in expected_sources}
        from mixed_admission import MixedAdmission
        admission=MixedAdmission(self.expected_sources,self.root/'source-switches.jsonl')
        admission.service.update(self.ledger.admission.service);admission.workers.update(self.ledger.admission.workers)
        self.ledger.admission=admission
        self.discovery_sealed=False
        self.snapshot_lock=threading.Lock()
        self.adapters.__class__=RoutedAdapters
        self.adapters.store.document_root_snapshots=True
        self.media_adapters={
            'video':clone_adapter(self.adapters,FrozenAdapters,contracts_by_kind['video'],SamplingPolicy()),
            'image':clone_adapter(self.adapters,ImageAdapters,contracts_by_kind['image'],ImagePolicy()),
            'audio':clone_adapter(self.adapters,AudioAdapters,contracts_by_kind['audio'],SamplingPolicy())}
        self.adapters.media_adapters=self.media_adapters
        # The bounded global document inbox routes each event using its durable
        # media identity; no mutable adapter swap is shared between threads.
        for adapter in self.media_adapters.values():
            adapter.document_events_fair=self.adapters.document_events_fair
            adapter.document_events=self.adapters.document_events
        self.views={k:MediaView(self,k,a) for k,a in self.media_adapters.items()}
        self.image_views={}
        from release_incremental import cohort
        for sid in self.image_rows:
            view=MediaView(self,'image',self.media_adapters['image'])
            view.plan=cohort(plan,sid);view.sources=self.views['image'].sources
            self.image_views[sid]=view
        self.views['video'].producer_variant='C'
        self.views['video'].branch_visual_only=True
        self.branch_producers_started=False
        with self.ledger.connection() as c:
            audio_schema(c)
            c.execute('CREATE INDEX IF NOT EXISTS mixed_document_result_ref ON v2_documents(raw_result_ref)')
            c.execute('CREATE TABLE IF NOT EXISTS image_sources_v1(source_id TEXT PRIMARY KEY,revision TEXT NOT NULL,plan_sha TEXT NOT NULL,decision TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS image_group_coverage_v1(source_id TEXT PRIMARY KEY,revision TEXT NOT NULL,group_id TEXT NOT NULL,proof TEXT NOT NULL)')
        from apps.media_archive_image_video_ui.image_frontend import decode_preview
        self.preview_decoder=decode_preview
        from apps.media_archive_image_video_ui.video_stream_commit import prepare
        self.ledger.prepare_sql_completion=lambda job,payload,digest,ms,parts:prepare(self.for_source(job['source_id']),job,payload,digest,ms,parts)
        self.ledger.apply_prepared_completion=lambda c,job,parts:self.for_source(job['source_id']).apply_prepared_mirror_result(c,job,parts)
        self.lifecycle=ModelLifecycleManager(self.root/'model-lifecycle.jsonl',self.describe_input,lambda e:(self.errors.append(e),self.stop.request('PROCESSING_FATAL','MODEL_LIFECYCLE_FATAL',error=str(e))))
        for stage in ('yoloe','openclip','insightface','ocr','qwen','vad','whisper','embedding'):
            if stage in handlers:handlers[stage]=self.lifecycle.register(stage,stage+'#1',handlers[stage])
        self.lifecycle_thread=None
        from mixed_coordination import ChangedInputs,install_coverage_tokens
        self.changed_inputs=ChangedInputs()
        from mixed_coordination import ReadyEntitlements
        self.ready_entitlements=ReadyEntitlements(self.root/'ready-selection-entitlements.json')
        with self.ledger.connection() as c:install_coverage_tokens(c)
        from audio_usable_documents import install,cleanup_paths
        install(self.media_adapters['audio'])
        if contracts_by_kind['video']['whisper'].get('usable_asr_contract'):
            install(self.media_adapters['video'])
        from video_transcript_quality import enabled,reject_proof
        if enabled(contracts_by_kind['video']):self.media_adapters['video'].store.terminal_document_outcome=reject_proof
        from speech_evidence import enabled as speech_enabled,schema as speech_schema,terminal_proof
        self.search_publication=speech_enabled(contracts_by_kind['video'])
        if self.search_publication:
            from search_publication import schema,reconcile
            publication_sources=[{**r,'publication_authority':cohort(plan,r['source_id'])['fingerprint']} if r['media_kind']=='image' else r for r in expected_sources]
            with self.ledger.connection() as c:
                c.execute('BEGIN IMMEDIATE');speech_schema(c);schema(c);reconcile(c,publication_sources)
                # A completed previous discovery snapshot can lose a source.
                # Preserve all history while revoking its current search head.
                for old in list(c.execute('SELECT source_id,revision FROM release_registry WHERE present=0')):
                    c.execute("UPDATE v2_active_sources SET source_revision=?,generation=generation+1 WHERE source_id=? AND source_revision=?",('missing:'+str(old['revision']),old['source_id'],old['revision']))
            self.media_adapters['video'].store.terminal_document_outcome=terminal_proof
            self.ledger.admission.folders={r['source_id']:str(Path(r['relative_path']).parent) for r in expected_sources if r['media_kind'] in ('image','video')}
        self.extra_transient_audio_paths=lambda sid,rev:cleanup_paths(self,sid,rev)

    def for_source(self,sid):
        kind=self.expected_sources.get(sid)
        if kind not in self.media_adapters:raise ContractError('MIXED_SOURCE_NOT_ADMITTED')
        return self.media_adapters[kind]
    def verify_input(self,job):
        if job['stage']=='qwen':
            payload=job['payload']
            if not isinstance(payload.get('image_path'),str) or any(payload.get(k) for k in ('images','image_paths','context_images')):raise ContractError('IMAGE_QWEN_REQUIRES_EXACTLY_ONE_IMAGE')
        return Engine.verify_input(self,job)

    def add(self,row,verified_digest=None):
        with self.stop.new_work('source_admission') as allowed:
            if not allowed:return None
            self.stop.work_started('source_admission',source_id=row['source_id'])
        return self._add_owned(row,verified_digest)

    def _add_owned(self,row,verified_digest=None):
        kind=row['media_kind'];view=self.views[kind]
        if self.expected_sources.get(row['source_id'])!=kind:raise ValueError('MIXED_MANIFEST_IDENTITY_MISMATCH')
        revision=None
        if row['source_id'] in self.incremental_changed:
            from release_incremental import register_changed
            revision=register_changed(self,self.image_views[row['source_id']] if kind=='image' else view,row,verified_digest=verified_digest)
        if revision is not None:pass
        elif kind=='image':
            view=self.image_views[row['source_id']]
            image=self.image_rows[row['source_id']];revision=ImageRuntime.add_image(view,image,verified_digest=verified_digest)
        elif kind=='audio':revision=AudioFrontendMixin.add_audio(view,row)
        else:
            source={'source_id':row['source_id'],'path':row['path'],'relative_path':row['relative_path'],
                    'media_kind':'video','digest':verified_digest or sha_file(row['path'])}
            revision=Engine.add_source(view,source)
        self.sources=[pair for v in self.views.values() for pair in v.sources]
        if self.search_publication and kind in ('image','video'):
            from search_publication import bind_revision
            with self.ledger.connection() as c:
                c.execute('BEGIN IMMEDIATE');bind_revision(c,row['source_id'],revision)
        return revision

    def prepare_source(self,source,revision):
        kind=source.get('media_kind','video');view=self.views[kind]
        # Existing registration/admission thread publishes a source identity;
        # each branch uses its existing stage admission owner independently.
        if self.adapters.has_state(source['source_id'],revision,'SOURCE_COMPLETE') or self.adapters.has_state(source['source_id'],revision,'SOURCE_FAILED_FINAL'):return
        if self.search_publication:
            from apps.media_archive_image_video_ui.video_commit_queue import SQLCommand,WriteProposal
            sql="UPDATE release_folders SET state='FOLDER_ACTIVE' WHERE committed=0 AND terminal<total AND folder=(SELECT folder FROM release_registry WHERE source_id=? AND present=1)"
            checks=((SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(self.ledger.owner,self.ledger.epoch)),)
            self.ledger.commit_queue.submit(WriteProposal('folder_admission',source['source_id'],(SQLCommand(sql,(source['source_id'],)),),checks)).result()
        if kind=='video':return
        if kind=='image':ImageRuntime.prepare_image(self.image_views[source['source_id']],source,revision)
        elif kind=='audio':AudioFrontendMixin.prepare_audio(view,source,revision)
        else:Engine.feed(view,source,revision)
        self.sources=[pair for v in self.views.values() for pair in v.sources]
        if self.search_publication:
            # The frozen short-video sampling policy can activate another
            # revision inside Engine.feed. Publication follows that real owner.
            actual=next(r for s,r in self.sources if s['source_id']==source['source_id'])
            from search_publication import bind_live
            bind_live(self,source['source_id'],actual)

    def describe_input(self,stage,payload):
        with self.ledger.connection() as c:
            if payload.get('_evidence_key'):r=c.execute('SELECT source_id FROM v2_jobs WHERE execution_key=?',(payload['_evidence_key'],)).fetchone()
            elif payload.get('candidate_id'):r=c.execute('SELECT source_id FROM v2_selected_candidates WHERE candidate_id=?',(payload['candidate_id'],)).fetchone()
            elif payload.get('document_id'):r=c.execute('SELECT source_id FROM v2_documents WHERE document_id=?',(payload['document_id'],)).fetchone()
            elif payload.get('artifact_id'):r=c.execute('SELECT source_id FROM v2_artifacts WHERE artifact_id=?',(payload['artifact_id'],)).fetchone()
            else:r=None
        sid=r[0] if r else payload.get('_source_id')
        source=next((s for s,rev in self.sources if s['source_id']==sid),{})
        return {'source_id':sid,'media_kind':self.expected_sources.get(sid),'source_path':source.get('relative_path'),
                'candidate_id':payload.get('candidate_id'),'segment_id':payload.get('segment_id'),'group_id':self.decisions.get(sid,{}).get('sequence_id')}

    def failed_source_outcome(self,sid,revision,kind,payload):
        with self.ledger.connection() as c:
            c.execute('BEGIN IMMEDIATE');self.ledger.fence(c)
            active=c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(sid,)).fetchone()
            if not active or active[0]!=revision:return
            evidence_revision=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(sid,)).fetchone()
            if not evidence_revision or evidence_revision[0]!=revision:return
            delivery_pending=c.execute("SELECT 1 FROM video_model_delivery_v1 WHERE source_id=? AND revision=? AND sealed_at IS NULL LIMIT 1",(sid,revision)).fetchone()
            publication_open=c.execute("SELECT 1 FROM video_stream_source_states_v1 p WHERE p.source_id=? AND p.revision=? AND p.state='FIRST_SELECTION_PUBLISHED' AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 d WHERE d.source_id=p.source_id AND d.revision=p.revision AND d.state='SELECTION_DONE')",(sid,revision)).fetchone()
            native_pending=c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status IN ('PENDING','RUNNING','RETRY_PENDING') LIMIT 1",(sid,revision)).fetchone()
            audio_done=True
            if kind in ('video','audio'):
                from video_branch_producers import audio_branch_terminal
                audio_done=audio_branch_terminal(c,sid,revision)
            visual_supply_open=False
            if kind in ('image','video'):
                selection_done=c.execute("SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SELECTION_DONE','VISUAL_FAILED_FINAL')",(sid,revision)).fetchone()
                visual_failed=c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('frame_basics','yoloe','openclip') AND status='TERMINAL_FAILED' LIMIT 1",(sid,revision)).fetchone()
                visual_supply_open=not selection_done and not visual_failed
            if not visual_supply_open and not delivery_pending and not publication_open and not native_pending and audio_done:
                c.execute("INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,'SOURCE_FAILED_FINAL',?)",(sid,revision,json.dumps(payload,sort_keys=True)))

    def coordinate_source(self,source,revision):
        from apps.media_archive_image_video_ui.video_stream_people import build_local
        sid=source['source_id'];kind=source.get('media_kind','video');a=self.media_adapters[kind]
        if a.has_state(sid,revision,'SOURCE_COMPLETE') or a.has_state(sid,revision,'SOURCE_FAILED_FINAL'):return
        with self.ledger.connection() as c:
            statuses=[tuple(r) for r in c.execute('SELECT stage,status FROM video_stream_jobs_v1 WHERE source_id=? AND revision=?',(sid,revision))]
        # A newly committed READY may now prove an older failed raw header redundant.
        # Reconcile only this source before using native failures to close it.
        if ('audio_extract','TERMINAL_FAILED') in statuses and ('audio_extract','DONE') in statuses:
            from apps.media_archive_image_video_ui.video_audio_recovery import recover
            recover(a,sid,revision)
            with self.ledger.connection() as c:
                statuses=[tuple(r) for r in c.execute('SELECT stage,status FROM video_stream_jobs_v1 WHERE source_id=? AND revision=?',(sid,revision))]
        if kind=='video' and a.has_state(sid,revision,'VISUAL_FAILED_FINAL'):
            # Partial source outcome: never stop or retire independent Audio
            # before it seals/drains. Existing jobs/artifacts remain intact.
            from video_branch_producers import audio_branch_terminal
            with self.ledger.connection() as c:
                audio_done=audio_branch_terminal(c,sid,revision)
            if audio_done and not any(status in ('PENDING','RUNNING','RETRY_PENDING') for stage,status in statuses):
                a.state(sid,revision,'SOURCE_FAILED_FINAL',{'reason':'visual_decode_failed_final','audio_preserved':True})
            return
        if any(status=='TERMINAL_FAILED' for stage,status in statuses):
            self.failed_source_outcome(sid,revision,kind,{'reason':'required_mixed_stage_failed'})
            return
        if kind in ('audio','video') and a.has_state(sid,revision,'ASR_QUALITY_REVIEW'):
            self.failed_source_outcome(sid,revision,kind,{'stage':'whisper','reason':'ASR_REQUIRED_QUALITY_REVIEW'})
            return
        if kind=='audio':
            if a.has_state(sid,revision,'AUDIO_INPUT_SEALED'):
                a.closure(sid,revision)
                if a.has_state(sid,revision,'SOURCE_COMPLETE'):
                    from apps.media_archive_image_video_ui.video_transient_audio import cleanup
                    cleanup(self.views[kind],sid,revision)
            return
        if kind=='image' and not self.decisions[sid]['should_generate_preview']:
            ImageRuntime.complete_covered(self.image_views[sid],sid,revision);return
        gate='IMAGE_PREVIEW_READY' if kind=='image' else 'EXTRACTION_DONE'
        if not a.has_state(sid,revision,gate):return
        if any(status!='DONE' for stage,status in statuses if stage in ('frame_basics','yoloe','openclip')):return
        # IMAGE first Selection is admitted only by the bounded publication lane.
        if kind=='image' and not a.has_state(sid,revision,'SELECTION_DONE'):return
        # Video first Selection belongs exclusively to its independent lane.
        if kind=='video' and not a.has_state(sid,revision,'FIRST_SELECTION_PUBLISHED') and not a.has_state(sid,revision,'SELECTION_DONE'):return
        if not a.has_state(sid,revision,'SELECTION_DONE'):return
        if not any(status!='DONE' for stage,status in statuses if stage=='insightface') and not a.has_state(sid,revision,'LOCAL_PEOPLE_DONE'):build_local(a,sid,revision,.58)
        for stage in ('qwen','ocr'):
            state=self.ledger.branch_state(sid,revision,stage)
            if state=='QWEN_COMPLETE':a.state(sid,revision,'QWEN_FIRST_PASS_DONE')
            if state in ('QWEN_FIRST_PASS_DONE','QWEN_COMPLETE','OCR_COMPLETE'):a.state(sid,revision,state)
        if kind=='video':
            from video_transcript_quality import publish_status
            if not publish_status(a,sid,revision):return
        a.closure(sid,revision)
        if kind=='video' and a.has_state(sid,revision,'SOURCE_COMPLETE'):
            from apps.media_archive_image_video_ui.video_transient_audio import cleanup
            cleanup(self.views[kind],sid,revision)

    def reclaim_closed_reservations(self,source=None,revision=None):
        # Also covers Resume's closed sources, which never enter ready/dirty.
        with self.ledger.connection() as c:
            scope=' AND s.source_id=? AND s.revision=?' if source is not None else ''
            rows=list(c.execute("""SELECT DISTINCT s.source_id,s.revision,e.payload
                FROM video_stream_source_states_v1 s
                JOIN video_stream_source_states_v1 e ON e.source_id=s.source_id AND e.revision=s.revision AND e.state='EXTRACTION_DONE'
                JOIN video_spool_reservations_v1 r ON r.token=json_extract(e.payload,'$.reservation_token')
                WHERE s.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL','SELECTION_DONE')"""+scope, (source,revision) if source is not None else ()))
        decisions=getattr(self,'_spool_decisions',{})
        for sid,rev,raw in rows:
            token=json.loads(raw)['reservation_token']
            outcome=self.budget.release_visual(token,sid,rev,self.ledger.owner,self.ledger.epoch)
            if outcome not in ('VISUAL_RELEASED_PAYLOAD_RETAINED','ALREADY_RELEASED'):
                outcome=self.budget.release_closed(token,sid,rev,self.ledger.owner,self.ledger.epoch)
            if decisions.get(token)!=outcome:
                with (self.root/'spool-reclamation.jsonl').open('a') as f:
                    f.write(json.dumps({'at':time.time(),'source_id':sid,'revision':rev,'token':token,'outcome':outcome})+'\n')
                decisions[token]=outcome
        self._spool_decisions=decisions

    def coordinate(self,people_threshold):
        from mixed_peers import refresh
        from mixed_coordination import trace
        try:
            while not self.stop.is_set():
                self.publish_search()
                self.reclaim_closed_reservations()
                # A fresh local snapshot of eligibility; only a bounded number
                # of image header lookups. No canonical/model body hydration.
                pairs={s['source_id']:(s,r) for s,r in self.sources}
                with self.ledger.connection() as c:
                    states={}
                    for sid,rev,state in c.execute("SELECT source_id,revision,state FROM video_stream_source_states_v1 WHERE state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL','SELECTION_DONE','PRESELECTION_READY','AUDIO_INPUT_SEALED','IMAGE_PREVIEW_READY','EXTRACTION_DONE')"):
                        states.setdefault((sid,rev),set()).add(state)
                selected={sid for (sid,rev),ss in states.items() if sid in pairs and pairs[sid][1]==rev and ('SELECTION_DONE' in ss or 'SOURCE_FAILED_FINAL' in ss)}
                try:refresh(self,limit=4)
                except ContractError as e:
                    if str(e)!='coordination_snapshot_changed_retry':raise
                with self.ledger.connection() as c:
                    closed={tuple(r) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')")}
                    front={tuple(r) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state IN ('AUDIO_INPUT_SEALED','IMAGE_PREVIEW_READY','EXTRACTION_DONE')")}
                    generations={r[0]:r[1] for r in c.execute('SELECT scope,generation FROM video_stream_change_tokens_v1')}
                def dirty_key(sid,rev):
                    related=self.plan.get('covered_by',{}).get(sid,[])
                    return (rev,generations.get('source:'+sid,0),generations.get('manual',0),(sid,rev) in front,sid in selected,
                            tuple((s,generations.get('source:'+s,0)) for s in related))
                source_snapshot=list(self.sources)
                pairs={s['source_id']:(s,r) for s,r in source_snapshot}
                ready=[{'source_id':s['source_id'],'sequence':n} for n,(s,r) in enumerate(source_snapshot)
                       if (s['source_id'],r) not in closed and ((s['source_id'],r) in front or
                          (s.get('media_kind')=='image' and not self.decisions[s['source_id']]['should_generate_preview']))
                       and self.changed_inputs.changed(s['source_id'],dirty_key(s['source_id'],r))]
                # A bounded scheduler quantum, not 2k source connection opens
                # before candidate/document events can receive their next turn.
                # Fair closure visits do not re-enter model/source
                # affinity, Completion Bias, folder fairness or protected rounds.
                from mixed_coordination import CoordinationTurns
                if not hasattr(self,'coordination_turns'):self.coordination_turns=CoordinationTurns()
                batch=self.coordination_turns.select(ready,16)
                for item in batch:
                    if self.stop.is_set():break
                    sid=item['source_id'];source,rev=pairs[sid]
                    current=next((r for s,r in self.sources if s['source_id']==sid),None)
                    if current!=rev:continue  # Progressive policy revision changed: re-evaluate next round, never observe this key.
                    with self.stop.new_work('source_coordination') as allowed:
                        if not allowed:break
                    self.coordination_turns.served(sid)
                    before=dirty_key(sid,rev);retry=False
                    trace(self,event='coordination_begin',source_id=sid,media_kind=source.get('media_kind','video'))
                    try:self.coordinate_source(source,rev)
                    except ContractError as e:
                        retry='changed_retry' in str(e) or 'snapshot_changed_retry' in str(e)
                        if not any(token in str(e) for token in ('incomplete','pending','snapshot_changed_retry','required_fanout_not_fully_promoted')):raise
                    finally:
                        if not retry:self.changed_inputs.observed(sid,before)
                        trace(self,event='coordination_end',source_id=sid,retry=retry)
                    # Publish at each source boundary, not after the other
                    # fifteen potentially expensive coordination turns.
                    if not self.stop.is_set():self.publish_search()
                if time.monotonic()-self.last_snapshot>=1:self.snapshot()
                self.stop.wait(.1)
        except Exception as e:self.errors.append('MIXED_COORDINATION:'+type(e).__name__+':'+str(e));self.stop.request('PROCESSING_FATAL','MIXED_COORDINATION_EXCEPTION',error=str(e),traceback=__import__('traceback').format_exc())

    def publish_search(self):
        if self.search_publication:
            from search_publication import advance
            advance(self)

    def drain_proofs(self):
        from apps.media_archive_image_video_ui.mixed_model_lifecycle import SourceIdentity,SourceFacts,Work,MixedFacts,WORK_STAGES,mixed_drain_proofs
        with self.ledger.connection() as c:
            c.execute('BEGIN');facts=[];identities=[]
            from apps.media_archive_image_video_ui.video_peer_catalog import dirty_heads
            peer_dirty=bool(dirty_heads(c,1))
            for source,revision in self.sources:
                sid=source['source_id'];kind=source.get('media_kind','video');identity=SourceIdentity(sid,revision,kind);identities.append(identity)
                states={r[0] for r in c.execute('SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(sid,revision))}
                native={r[0]:{} for r in c.execute('SELECT DISTINCT stage FROM video_stream_jobs_v1 WHERE source_id=? AND revision=?',(sid,revision))}
                for stage,status,n in c.execute("SELECT stage,status,count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status IN ('PENDING','RUNNING','RETRY_PENDING') GROUP BY stage,status",(sid,revision)):native.setdefault(stage,{})[status.lower()]=n
                canonical={r[0]:r[1] for r in c.execute("SELECT stage,count(*) FROM v2_jobs WHERE source_id=? AND source_revision=? AND status IN ('PENDING','RUNNING','FAILED_RETRYABLE') GROUP BY stage",(sid,revision))}
                events=c.execute("SELECT count(*) FROM v2_outbox o WHERE aggregate_id=? AND (event_type IN ('CANDIDATE_READY','AUDIO_ARTIFACT_READY') OR (event_type='EVIDENCE_READY' AND json_extract(payload,'$.payload.stage') IN ('qwen','ocr','audio_extract','vad','whisper','embedding'))) AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=o.event_id)",(sid+':'+revision,)).fetchone()[0]
                documents=c.execute("SELECT count(*) FROM v2_documents WHERE source_id=? AND source_revision=? AND status='READY'",(sid,revision)).fetchone()[0]
                closed=bool(states&{'SOURCE_COMPLETE','SOURCE_FAILED_FINAL'})
                frontend=closed or 'VISUAL_FAILED_FINAL' in states or ('AUDIO_INPUT_SEALED' in states if kind=='audio' else 'EXTRACTION_DONE' in states if kind=='video' else not self.decisions[sid]['should_generate_preview'] or 'IMAGE_PREVIEW_READY' in states)
                selection=closed or 'VISUAL_FAILED_FINAL' in states or kind=='audio' or 'SELECTION_DONE' in states or (kind=='image' and not self.decisions[sid]['should_generate_preview'])
                branch=c.execute('SELECT status FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(sid,revision)).fetchone()
                audio_events=c.execute("SELECT 1 FROM v2_outbox o WHERE aggregate_id=? AND (event_type='AUDIO_ARTIFACT_READY' OR (event_type='EVIDENCE_READY' AND json_extract(payload,'$.payload.stage') IN ('audio_extract','vad'))) AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='video-stream-documents-v1' AND i.event_id=o.event_id) LIMIT 1",(sid+':'+revision,)).fetchone()
                audio_seal=closed or kind=='image' or bool(frontend and branch and branch[0] in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY') and not audio_events)
                facts.append(SourceFacts(identity,frontend,selection,audio_seal,closed and not peer_dirty,'SOURCE_COMPLETE' if 'SOURCE_COMPLETE' in states else 'SOURCE_FAILED_FINAL' if closed else 'OPEN',
                    tuple(Work(s,native.get(s,{}).get('pending',0),native.get(s,{}).get('running',0),native.get(s,{}).get('retry_pending',0),0) for s in WORK_STAGES),
                    tuple(Work(s,0,0,0,canonical.get(s,0)) for s in WORK_STAGES),events,documents))
        valid=self.discovery_sealed and {s.source_id for s in identities}==set(self.expected_sources)
        result=mixed_drain_proofs(MixedFacts(tuple(identities),valid,tuple(facts)))
        from apps.media_archive_image_video_ui.model_lifecycle import DrainProof
        vad={k:sum(getattr(w,k) for fact in facts for w in fact.native+fact.canonical if w.stage=='vad') for k in ('pending','running','retry_pending','unpublished')}
        sealed=result['whisper'].upstream_sealed
        result['vad']=DrainProof(sealed,not sealed,**vad,downstream_safe=sealed,reason='all_audio_vad_producers_sealed' if sealed else 'global_audio_upstream_open')
        return result

    def snapshot(self):
        with self.snapshot_lock:return self._snapshot()
    def _snapshot(self):
        # Shared model work and per-source closure remain separate concepts.
        with self.ledger.connection() as c:
            counts=[tuple(r) for r in c.execute('SELECT stage,status,count(*) FROM video_stream_jobs_v1 GROUP BY stage,status')]
            closed={tuple(r) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')")}
            from apps.media_archive_image_video_ui.video_peer_catalog import dirty_heads
            peer_pending=dirty_heads(c,1)
            for row in c.execute("SELECT job_id,stage,service_ms FROM video_stream_jobs_v1 WHERE status='DONE'"):
                if row['job_id'] in self.observed_job_ids:continue
                self.observed_job_ids.add(row['job_id'])
                if row['stage'] in self.stats and row['service_ms'] is not None:
                    self.stats[row['stage']].observe(row['service_ms']);self.ledger.admission.service[row['stage']]=self.stats[row['stage']].ewma
        value={'at':time.time(),'media_kind':'mixed','counts':counts,'model_remaining':{},'closure_pending':{s['source_id']:{'media_kind':s.get('media_kind','video')} for s,r in self.sources if (s['source_id'],r) not in closed},'errors':self.errors,
               'resource_governor':self.governor.snapshot(),'service_stats':{s:v.summary() for s,v in self.stats.items()}}
        if self.search_publication:
            from search_publication import status
            with self.ledger.connection() as c:
                value['search_publication']=status(c)
                for q in c.execute('SELECT source_id FROM release_publication_queue'):
                    value['closure_pending'].setdefault(q[0],{})['search_publication_pending']=True
                for q in c.execute("SELECT r.source_id FROM release_registry r WHERE r.present=1 AND r.committed=0 AND r.terminal=0 AND EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=r.source_id AND s.revision=r.revision AND s.state='SOURCE_COMPLETE')"):
                    value['closure_pending'].setdefault(q[0],{})['search_publication_pending']=True
            value['search_publication']['current_folder']=next(iter(getattr(self.ledger.admission,'folder_active',{}).values()),{}).get('folder')
        for sid,rev,snapshot in peer_pending:value['closure_pending'].setdefault(sid,{})['peer_projection']=snapshot
        for stage,status,n in counts:
            if status in ('PENDING','RUNNING','RETRY_PENDING'):value['model_remaining'][stage]=value['model_remaining'].get(stage,0)+n
        if getattr(self,'claim_truth',None):value['claim_truth']=self.claim_truth.current()
        value['prerequisites_done_without_selection']=len(self.ready_entitlements.pending_ids())
        value['ready_entitlement_io']=self.ready_entitlements.io_snapshot()
        value['first_selection_metric_scope']='revision-scoped pending ready entitlements; exact prerequisites revalidated at ownership'
        from backlog_liveness import observe
        observe(self,value)
        tmp=self.root/('runtime.json.'+str(threading.get_ident())+'.tmp');tmp.write_text(json.dumps(value));tmp.replace(self.root/'runtime.json');self.last_snapshot=time.monotonic();return value
    def dispatch(self):return ImageRuntime.dispatch(self)
    def start(self,people_threshold=.58):
        if getattr(self,'_mixed_start_attempted',False):
            raise RuntimeError('mixed_runtime_start_already_attempted')
        self._mixed_start_attempted=True
        try:
            return self._start_once(people_threshold)
        except Exception as error:
            self.stop.request('RUNTIME_START','RUNTIME_START_FAILED',error=str(error))
            raise

    def _start_once(self,people_threshold):
        # Recovery must finish before spawning any coordination/model threads.
        from apps.media_archive_image_video_ui.video_audio_recovery import recover
        recover(self.adapters)
        self._audio_recovered_before_start=True
        from apps.media_archive_image_video_ui.video_transient_audio import cleanup
        for source,rev in self.sources:
            kind=source.get('media_kind','video')
            if kind in ('video','audio') and self.adapters.has_state(source['source_id'],rev,'SOURCE_COMPLETE'):cleanup(self.views[kind],source['source_id'],rev)
        from mixed_coordination import run_ready_selection
        ready_thread=threading.Thread(target=lambda:run_ready_selection(self),name='ready-first-selection')
        self.threads.append(ready_thread);ready_thread.start()
        from apps.media_archive_image_video_ui.build151_runtime_trace import ClaimTruth
        self.claim_truth=ClaimTruth(self);self.ledger.claim_truth=self.claim_truth
        Engine.start(self,people_threshold)
        from apps.media_archive_image_video_ui.video_stream_adapter import ModelDeliveryLane
        self.model_delivery=ModelDeliveryLane(self)
        delivery_thread=threading.Thread(target=self.model_delivery.run,name='model-delivery')
        self.threads.append(delivery_thread);delivery_thread.start()
        from apps.media_archive_image_video_ui.video_stream_adapter import NativeDeliveryService
        self.native_delivery_services=[]
        for partition in ('audio','documents'):
            service=NativeDeliveryService(self,partition);self.native_delivery_services.append(service)
            thread=threading.Thread(target=service.run,name='native-delivery-'+partition)
            self.threads.append(thread);thread.start()

        truth_thread=threading.Thread(target=self.claim_truth.run,name='claim-truth-observer');self.threads.append(truth_thread);truth_thread.start()
        warmer=getattr(self,'decoder_evidence_warmer',None)
        if warmer:
            def warm():
                from apps.media_archive_image_video_ui.video_telemetry import ClosureSpan
                while not self.stop.is_set():
                    with ClosureSpan(self.root,'BACKGROUND_DECODER_CACHE','model_setup.decode_evidence'):
                        warmer()
                    self.stop.wait(30)
            warm_thread=threading.Thread(target=warm,name='decoder-evidence-cache');self.threads.append(warm_thread);warm_thread.start()
        from video_branch_producers import start
        start(self)
        def control():
            try:
                while not self.stop.is_set():
                    proofs=self.drain_proofs();self.lifecycle.update(proofs)
                    system=self.resources.cached or {}
                    if system:
                        old_reason=self.governor.reason_for('whisper')
                        self.governor.update(system)
                        self.lifecycle.resource_tick(self.governor)
                        if old_reason and self.governor.reason_for('whisper') is None:
                            from build151_decision_trace import changed
                            changed(self,'WHISPER_RESOURCE_RECOVERY','whisper',dict(previous_reason=old_reason,resource_reason=self.lifecycle.resource_reason('whisper')),decision=True)
                    models=self.lifecycle.snapshot()['models']
                    self.governor.model_context={'qwen_worker_count':1,'resident_models':{k:v for k,v in models.items() if v['resident']},
                        'future_reload_stages':[k for k,v in proofs.items() if v.future_work_possible and not any(m['resident'] for m in models.values() if m['stage']==k)]}
                    self.lifecycle.resource_context={'memory_pressure_level':system.get('memory_pressure_level'),'swap_used_bytes':system.get('swap_used_bytes'),'governor_tier':self.governor.tier,'qwen_worker_count':1}
                    value={'at':time.time(),'lifecycle':self.lifecycle.snapshot(),'proofs':{k:v.__dict__ for k,v in proofs.items()},'qwen_workers':1,'tail_boost':'OFF'}
                    value['governor']=self.governor.snapshot();value['model_context']=self.governor.model_context
                    tmp=self.root/'model-lifecycle.json.tmp';tmp.write_text(json.dumps(value));tmp.replace(self.root/'model-lifecycle.json');self.stop.wait(1)
            except Exception as e:self.errors.append('MIXED_LIFECYCLE:'+str(e));self.stop.request('PROCESSING_FATAL','MIXED_LIFECYCLE_EXCEPTION',error=str(e),traceback=__import__('traceback').format_exc())
        self.lifecycle_thread=threading.Thread(target=control,name='mixed-lifecycle');self.lifecycle_thread.start()
    def close(self):
        if getattr(self,'_mixed_close_complete',False):return
        self.stop.set()
        if self.lifecycle_thread:
            self.lifecycle_thread.join(timeout=30)
            if self.lifecycle_thread.is_alive():raise TimeoutError('lifecycle_shutdown_not_safe')
        failures=[]
        try:
            snapshot=self.snapshot()
            if not snapshot['closure_pending'] and not snapshot['model_remaining']:
                self.lifecycle.update(self.drain_proofs())
        except Exception as error:failures.append(error)
        # Status serialization must never prevent owned resource cleanup.
        try:Engine.close(self)
        except Exception as error:failures.append(error)
        if getattr(self,'_engine_close_complete',False):
            try:
                (self.root/'model-lifecycle.json').write_text(json.dumps({'at':time.time(),'lifecycle':self.lifecycle.snapshot(),'historical':True,'final_exit_state':True}))
            except Exception as error:failures.append(error)
            self._mixed_close_complete=True
        self._mixed_close_errors=[{'type':type(error).__name__,'message':str(error)} for error in failures]
        if failures:raise failures[0]

