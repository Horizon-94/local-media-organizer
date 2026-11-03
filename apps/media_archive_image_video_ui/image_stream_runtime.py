"""Explicit IMAGE frontend over the accepted parallel durable engine.

No video producer, video sampling, video EOS, audio recovery or audio model
is called. The inherited methods below are resource/queue/index infrastructure,
not a second scheduler. Internal historical table names are not media kinds.
"""
import json,threading,time
from pathlib import Path
from .video_stream_runtime import Runtime as Engine
from .video_stream_adapter import FrozenAdapters
from .v2_producer import fingerprint
from .v2_model_workers import sha_file
from .v2_runtime import ContractError
from .image_frontend import ImagePolicy,decode_preview

class ImageAdapters(FrozenAdapters):
    def closure(self,source,revision):
        if self.has_state(source,revision,'SOURCE_COMPLETE'):return
        if not self.has_state(source,revision,'LOCAL_PEOPLE_DONE'):return
        with self.store.connect() as c:
            row=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SELECTION_DONE'",(source,revision)).fetchone()
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status<>'DONE' LIMIT 1",(source,revision)).fetchone():return
        if not row:return
        for stage,complete in (('qwen','QWEN_COMPLETE'),('ocr','OCR_COMPLETE')):
            if self.ledger.branch_state(source,revision,stage)!=complete:return
        from .video_detached_coordination import pipeline_operation
        snapshot=json.loads(row[0])['snapshot']
        if not pipeline_operation(self,source,revision,'final_projection',lambda p:p.final_projection.advance(source,revision,snapshot)):return
        required={stage:self.contracts[stage]['revision'] for stage in self.pipeline.selection.REQUIRED}
        # Shared visual closure validates documents, embeddings, all indexes,
        # projection and people; its visual_only explicitly excludes AUDIO.
        pipeline_operation(self,source,revision,'visual_closure',lambda p:p.index.close_source(
            source,revision,p.selection,required,self.contracts['insightface'],
            require_final_projection=True,visual_only=True,allow_empty_text=True))
        self.state(source,revision,'SOURCE_COMPLETE')

class ImageRuntime(Engine):
    def __init__(self,workspace,contracts,handlers,*,plan,resume=False,fixture_mode=False,resource_monitor=None,qwen_factory=None,tail_boost_enabled=False):
        if set(handlers)-{'frame_basics','yoloe','openclip','insightface','qwen','ocr','embedding'}:
            raise ValueError('IMAGE_ONLY_FORBIDS_VIDEO_AUDIO_WORKERS')
        if fingerprint({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:
            raise ValueError('IMAGE_PLAN_DIGEST_MISMATCH')
        self.plan=plan;self.image_rows={r['source_content_id']:r for r in plan['rows']}
        self.decisions={r['parent_source_content_id']:r for r in plan['decisions']}
        # Only bootstrap the accepted ledger/consumers/writer/index engine.
        # Never invoke Engine.add_source/feed/frame/start/coordinate/snapshot.
        super().__init__(workspace,contracts,handlers,resume=resume,fixture_mode=fixture_mode,
                         resource_monitor=resource_monitor,environment={'VIDEO_PIPELINE_V2_STREAMING':'1'})
        adapter=object.__new__(ImageAdapters);adapter.__dict__.update(self.adapters.__dict__)
        self.adapters=adapter;adapter.store.adapter=adapter
        adapter.store.policy=ImagePolicy()
        adapter.store.snapshot_source_scope=self.snapshot_source_scope
        self.pipeline_revision=fingerprint((self.pipeline_revision,sha_file(__file__),plan['fingerprint']))
        with self.ledger.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS image_sources_v1(source_id TEXT PRIMARY KEY,revision TEXT NOT NULL,plan_sha TEXT NOT NULL,decision TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS image_group_coverage_v1(source_id TEXT PRIMARY KEY,revision TEXT NOT NULL,group_id TEXT NOT NULL,proof TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS image_candidate_source ON v2_selected_candidates(source_id,purpose,candidate_id)')
        self.preview_decoder=decode_preview
        from .model_lifecycle import ModelLifecycleManager
        from .image_tail_boost import TailBoost
        def invariant(message):self.errors.append(message);self.stop.set()
        self.lifecycle=ModelLifecycleManager(self.root/'model-lifecycle.jsonl',self.describe_model_input,invariant)
        primary=handlers.get('qwen')
        if tail_boost_enabled and qwen_factory is None and not fixture_mode and hasattr(primary,'config'):
            from .video_stream_native import Resident
            import copy
            qwen_factory=lambda:Resident('qwen',copy.deepcopy(primary.config),primary.monitor)
        for stage in ('yoloe','openclip','insightface','qwen','ocr','embedding'):
            if stage in handlers:handlers[stage]=self.lifecycle.register(stage,stage+'#1',handlers[stage])
        self.tail_boost=TailBoost(self,qwen_factory if tail_boost_enabled else None)
        if not tail_boost_enabled:self.tail_boost.reason='DISABLED_BENCHMARK_REJECTED'
        self.lifecycle_stop=threading.Event();self.lifecycle_thread=None

    def describe_model_input(self,stage,payload):
        candidate=payload.get('candidate_id');artifact=payload.get('artifact_id') or payload.get('frame_id')
        sid=None
        with self.ledger.connection() as c:
            if candidate:row=c.execute('SELECT source_id FROM v2_selected_candidates WHERE candidate_id=?',(candidate,)).fetchone()
            elif artifact:row=c.execute('SELECT source_id FROM v2_artifacts WHERE artifact_id=?',(artifact,)).fetchone()
            elif payload.get('document_id'):row=c.execute('SELECT source_id FROM v2_documents WHERE document_id=?',(payload['document_id'],)).fetchone()
            else:row=None
            if row:sid=row[0]
            candidates=[r[0] for r in c.execute("SELECT candidate_id FROM v2_selected_candidates WHERE source_id=? AND purpose='qwen' ORDER BY candidate_id",(sid,))] if candidate else []
        source=self.image_rows.get(sid,{})
        return {'candidate_id':candidate,'source_id':sid,'source_path':source.get('source_relative_path'),
                'group_id':self.decisions.get(sid,{}).get('sequence_id'),
                'candidate_index':candidates.index(candidate)+1 if candidate in candidates else None,
                'candidate_total':len(candidates) if candidate else None,'attempt':payload.get('_wave_attempt')}

    @staticmethod
    def snapshot_source_scope(c,source,revision,operation):
        if operation not in ('canonical_preparation','selection','visual_closure','final_projection'):return []
        row=c.execute('SELECT payload FROM v2_selection_contexts WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        if not row:return []
        context=json.loads(row[0]);dependencies=context['dependencies']
        if dependencies.get(source)!=revision:raise ContractError('image_context_revision_mismatch')
        return sorted(s for s in dependencies if s!=source)

    def verify_input(self,job):
        if job['stage']=='qwen':
            payload=job['payload']
            if not isinstance(payload.get('image_path'),str) or any(payload.get(k) for k in ('images','image_paths','context_images')):
                raise ContractError('IMAGE_QWEN_REQUIRES_EXACTLY_ONE_IMAGE')
        return super().verify_input(job)

    def add_image(self,row,verified_digest=None):
        if row['media_kind']!='image':raise ValueError('IMAGE_SOURCE_REQUIRED')
        sid=row['source_content_id'];path=Path(row['source_path']);st=path.stat()
        if (st.st_size,st.st_mtime_ns)!=(row['source_size'],row['source_mtime_ns']):raise ValueError('IMAGE_INVENTORY_CHANGED')
        with self.ledger.connection() as c:
            old=c.execute('SELECT * FROM image_sources_v1 WHERE source_id=?',(sid,)).fetchone()
            if old:
                if old['plan_sha']!=self.plan['fingerprint']:raise ValueError('IMAGE_PLAN_CHANGED_ON_RESUME')
                raw=c.execute('SELECT source_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(sid,old['revision'])).fetchone()
                source=json.loads(raw[0]);rev=old['revision']
                if source['stat']!=[st.st_size,st.st_mtime_ns,st.st_ino]:raise ValueError('IMAGE_CHANGED_ON_RESUME')
                self.sources.append((source,rev));return rev
        digest=verified_digest or sha_file(path)
        if (path.stat().st_size,path.stat().st_mtime_ns)!=(st.st_size,st.st_mtime_ns):raise ValueError('IMAGE_CHANGED_DURING_HASH')
        source={'source_id':sid,'digest':digest,'path':str(path),'stat':[st.st_size,st.st_mtime_ns,st.st_ino],
                'relative_path':row['source_relative_path'],'media_kind':'image'}
        decision=self.decisions[sid]
        meta={**row,'media_type':'image','media_kind':'image','relative_path':row['source_relative_path'],
              'a9t_plan':decision,'planning_revision':self.plan['fingerprint']}
        rev=self.adapters.store.source_ready(sid,fingerprint((digest,self.plan['fingerprint'])),digest,path,meta)
        self.ledger.source(sid,rev)
        with self.ledger.write_connection() as c:
            c.execute('INSERT INTO image_sources_v1 VALUES(?,?,?,?)',(sid,rev,self.plan['fingerprint'],json.dumps(decision,sort_keys=True)))
            c.execute('INSERT INTO video_stream_inputs_v1 VALUES(?,?,?,NULL)',(sid,rev,json.dumps(source,sort_keys=True)))
        self.sources.append((source,rev));return rev

    def prepare_image(self,source,revision):
        sid=source['source_id'];decision=self.decisions[sid]
        if not decision['should_generate_preview'] or self.adapters.has_state(sid,revision,'IMAGE_PREVIEW_READY'):return
        with self.ledger.connection() as c:
            existing=c.execute("SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? AND slot=0",(sid,revision)).fetchone()
            sealed=c.execute('SELECT eos FROM v2_sources WHERE source_id=? AND revision=?',(sid,revision)).fetchone()[0]=='EOS'
        self.frontend_work={'source_id':sid,'source_path':source.get('relative_path'),'group_id':decision.get('sequence_id'),'stage':'image_preview','started_at':time.time()}
        if existing:
            if sha_file(existing['path'])!=existing['sha']:raise ValueError('IMAGE_PREVIEW_CHANGED')
            preview={'output_path':existing['path'],'resume_reused':True}
        else:preview=self.preview_decoder(decision,self.root)
        publisher=self.adapters.store
        with publisher.source_lease(sid,revision):
            aid=publisher.publish(sid,revision,0,preview['output_path'])
            with publisher.connect() as c:artifact=dict(c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(aid,)).fetchone())
            payload={'artifact_id':aid,'visual_unit_id':aid,'frame_id':aid,'derived_path':artifact['path'],
                     'derived_sha':artifact['sha'],'time_position_ms':-1,'media_kind':'image',
                     'width':artifact['width'],'height':artifact['height']}
            for stage in ('frame_basics','yoloe','openclip','insightface'):
                contract=self.contracts[stage] if stage!='frame_basics' else {'revision':1,'parts':{s:self.contracts[s] for s in ('dhash','geometry','quality')}}
                self.ledger.publish(sid,revision,stage,artifact['sha'],contract,payload)
            if not sealed:publisher.finish(sid,revision,0,success=True)
        self.adapters.state(sid,revision,'IMAGE_PREVIEW_READY',preview)
        self.trace('preview',source_id=sid,revision=revision,decision=decision,preview=preview)
        self.frontend_work={'stage':'IDLE'}

    def bind_image_context(self,sid,revision):
        selection=self.adapters.pipeline.selection
        with selection.store.connect() as c:
            if selection.context(c,sid,revision):return True
        from .v2_timelapse_selection_context import sequence_members
        members=sequence_members(self.plan,sid) or [self.decisions[sid]]
        deps={};mappings=[];timelapse=[];assets=[]
        with selection.store.connect() as c:
            for member in members:
                key=member['parent_source_content_id']
                row=c.execute("SELECT i.revision,a.* FROM image_sources_v1 i JOIN v2_sources s ON s.source_id=i.source_id AND s.revision=i.revision AND s.eos='EOS' JOIN v2_artifacts a ON a.source_id=i.source_id AND a.revision=i.revision WHERE i.source_id=?",(key,)).fetchone()
                if not row:return False
                if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('frame_basics','yoloe','openclip') AND status<>'DONE' LIMIT 1",(key,row['revision'])).fetchone():return False
                aid=row['artifact_id'];deps[key]=row['revision']
                mappings.append({'source_content_id':key,'visual_unit_id':aid,'canonical_visual_unit_id':aid,
                                 'visual_duplicate_group_id':fingerprint(aid),'identity_status':'canonical'})
                image=self.image_rows[key];assets.append({**image,'relative_path':image['source_relative_path']})
                if member['preview_role']=='timelapse_keyframe':
                    timelapse.append({'visual_unit_id':aid,'sequence_id':member['sequence_id'],
                        'representative_position':member['representative_position'],'source_relative_path':image['source_relative_path'],
                        'visual_file':row['path'],'parent_source_content_id':key})
        stems={(str(Path(a['source_relative_path']).parent).casefold(),Path(a['source_relative_path']).stem.casefold()) for a in assets}
        assets.extend(a for a in self.plan.get('sidecar_assets',[]) if (str(Path(a['relative_path']).parent).casefold(),Path(a['relative_path']).stem.casefold()) in stems)
        tags=[tag for tag in self.plan['finder_tags'] if tag['source_content_id'] in deps]
        selection.bind_context(deps,{'source_assets':assets,'finder_tags':tags,'visual_mappings':mappings,'timelapse_rows':timelapse})
        return True

    def complete_covered(self,sid,revision):
        """Frozen A9T covered members: identity retained, never synthetic frames.

        Coverage depends on sealed representative artifacts, not completion of
        unrelated/group heavy Qwen. The proof remains inspectable on restart.
        """
        refs=[]
        with self.ledger.connection() as c:
            for representative in self.plan['covered_by'][sid]:
                if self.decisions[representative]['preview_role']!='timelapse_keyframe':continue
                row=c.execute("SELECT a.source_id,a.revision,a.artifact_id,a.sha FROM v2_artifacts a JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.revision WHERE a.source_id=? AND s.eos='EOS'",(representative,)).fetchone()
                if not row:return
                refs.append(dict(row))
        if not refs:raise ContractError('image_coverage_without_representatives')
        proof={'contract':'image-a9t-preview-coverage-v1','plan_sha':self.plan['fingerprint'],'representatives':refs}
        from .video_commit_queue import SQLCommand,WriteProposal
        from .video_coordination import guards,tokens
        with self.ledger.connection() as c:checks=guards(self.adapters,sid,revision,tokens(c,sid))
        self.ledger.commit_queue.submit(WriteProposal('image_coverage',sid,(
            SQLCommand('INSERT OR IGNORE INTO image_group_coverage_v1 VALUES(?,?,?,?)',(sid,revision,str(self.decisions[sid]['sequence_id']),json.dumps(proof,sort_keys=True))),),checks)).result()
        self.adapters.state(sid,revision,'SOURCE_COMPLETE',{'visual_policy':'TIMELAPSE_COVERED','proof_sha':fingerprint(proof)})

    def coordination_sources(self):
        # Image eligibility has no video extraction/EOS dependency. Only ready
        # durable facts are read here; the existing in-memory admission policy
        # owns priority, aging and lane ownership.
        with self.ledger.connection() as c:
            closed={r[0] for r in c.execute("SELECT source_id FROM video_stream_source_states_v1 WHERE state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')")}
            ready={r[0] for r in c.execute("SELECT source_id FROM video_stream_source_states_v1 WHERE state='IMAGE_PREVIEW_READY'")}
        remaining=[(s,rev) for s,rev in list(self.sources) if s['source_id'] not in closed and
                   (s['source_id'] in ready or not self.decisions[s['source_id']]['should_generate_preview'])]
        while remaining and not self.stop.is_set():
            rows=[{'source_id':s['source_id'],'sequence':i} for i,(s,rev) in enumerate(remaining)]
            order=self.ledger.admission.order('source_coordination',rows)
            if not order:return
            chosen=order[0]['source_id'];position=next(i for i,(s,rev) in enumerate(remaining) if s['source_id']==chosen)
            item=remaining.pop(position);self.ledger.admission.claimed('source_coordination',chosen)
            started=time.monotonic()
            try:yield item
            finally:
                self.ledger.admission.service['source_coordination']=(time.monotonic()-started)*1000
                complete=self.adapters.has_state(chosen,item[1],'SOURCE_COMPLETE')
                self.ledger.admission.release('source_coordination','stage_complete' if complete else 'upstream_or_dependency_blocked')

    def coordinate(self,people_threshold):
        raise RuntimeError('BUILD151_REQUIRES_MIXED_STAGE_LOCAL_SERVICES')

    def trace(self,event,**values):
        with (self.root/'image-events.jsonl').open('a') as stream:stream.write(json.dumps({'at':time.time(),'event':event,**values},ensure_ascii=False)+'\n')

    def dispatch(self):
        # Image close first sets runtime.stop, then stops consumers. A refill
        # already in flight must not reinterpret that normal stop as DB fatal.
        try:
            while not self.stop.is_set():
                self.consumers.refill()
                if self.consumers.stop.is_set():
                    if self.stop.is_set():break
                    raise RuntimeError('consumer_database_commit_blocked')
                self.stop.wait(.02)
        except Exception as error:self.errors.append(str(error));self.stop.set()

    def snapshot(self):
        with self.ledger.connection() as c:
            counts=[tuple(r) for r in c.execute('SELECT stage,status,count(*) FROM video_stream_jobs_v1 GROUP BY stage,status')]
            complete={r[0] for r in c.execute("SELECT source_id FROM video_stream_source_states_v1 WHERE state='SOURCE_COMPLETE'")}
            pending={s['source_id']:{'image_source_closure':1} for s,r in self.sources if s['source_id'] not in complete}
            for row in c.execute("SELECT job_id,stage,service_ms FROM video_stream_jobs_v1 WHERE status='DONE'"):
                if row['job_id'] in self.observed_job_ids:continue
                self.observed_job_ids.add(row['job_id'])
                if row['stage'] in self.stats and row['service_ms'] is not None:self.stats[row['stage']].observe(row['service_ms'])
            costs={}
            for row in c.execute("SELECT source_id,stage,count(*) n FROM video_stream_jobs_v1 WHERE status IN ('PENDING','RUNNING','RETRY_PENDING') GROUP BY source_id,stage"):
                service=self.ledger.admission.service.get(row['stage'])
                if service is not None:costs[row['source_id']]=max(costs.get(row['source_id'],0),row['n']*service/max(1,self.ledger.admission.workers.get(row['stage'],1)))
            self.ledger.admission.completion=costs
        remaining={stage:sum(n for s,status,n in counts if s==stage and status in ('PENDING','RUNNING','RETRY_PENDING')) for stage in self.handlers}
        value={'at':time.time(),'media_kind':'image','counts':counts,'model_remaining':{k:v for k,v in remaining.items() if v},
               'closure_pending':pending,'errors':self.errors,'system':self.resources.cached,
               'resource_governor':self.governor.snapshot(),'eta_ms':None,'eta_reason':'insufficient_data',
               'service_stats':{s:stat.summary() for s,stat in self.stats.items()}}
        temporary=self.root/'runtime.json.tmp';temporary.write_text(json.dumps(value));temporary.replace(self.root/'runtime.json')
        self.last_snapshot=time.monotonic();return value

    def start(self,people_threshold=.58):
        if people_threshold!=.58:raise ValueError('FROZEN_PEOPLE_THRESHOLD')
        def observe():
            while not self.stop.is_set():
                try:
                    system=self.resources.sample(self.root)
                    workers=self.resource_monitor.sample_all() if self.resource_monitor else []
                    value={'at':time.time(),'system':system,'model_workers':workers,
                           'governor':self.governor.snapshot(),'index':dict(getattr(self,'index_lane',{})),
                           'frontend':dict(getattr(self,'frontend_work',{})),
                           'scheduler':self.ledger.admission.snapshot(),
                           'queue_depth':{stage:q.qsize() for stage,q in self.consumers.queues.items()}}
                    index_source=value['index'].get('active_source')
                    if index_source:
                        value['index'].update(source_path=self.image_rows.get(index_source,{}).get('source_relative_path'),
                                              group_id=self.decisions.get(index_source,{}).get('sequence_id'))
                    temporary=self.root/'resources.json.tmp';temporary.write_text(json.dumps(value));temporary.replace(self.root/'resources.json')
                    with (self.root/'resources.jsonl').open('a') as stream:stream.write(json.dumps(value)+'\n')
                except Exception as error:self.resource_errors.append(str(error))
                self.stop.wait(5)
        self.resource_thread=threading.Thread(target=observe,name='image-resource-observer',daemon=True);self.resource_thread.start()
        self.consumers.start()
        def lifecycle_control():
            from .model_lifecycle import image_drain_proofs
            while not self.lifecycle_stop.is_set():
                try:
                    proofs=image_drain_proofs(self)
                    system=self.resources.cached
                    if system:self.governor.update(system)
                    self.lifecycle.resource_context={'memory_pressure_level':(system or {}).get('memory_pressure_level'),
                        'swap_used_bytes':(system or {}).get('swap_used_bytes'),
                        'swap_growth_rate_bytes_per_second':(system or {}).get('swap_growth_rate_bytes_per_second'),
                        'governor_tier':self.governor.tier,'qwen_worker_count':self.tail_boost.snapshot()['effective_concurrency']}
                    self.lifecycle.update(proofs)
                    self.tail_boost.tick(proofs,system)
                    model_state=self.lifecycle.snapshot()
                    self.governor.model_context={'qwen_worker_count':self.tail_boost.snapshot()['effective_concurrency'],
                        'resident_models':{k:v for k,v in model_state['models'].items() if v['resident']}}
                    value={'at':time.time(),'lifecycle':self.lifecycle.snapshot(),'tail_boost':self.tail_boost.snapshot(),
                           'remaining':{stage:proof.__dict__ for stage,proof in proofs.items()},
                           'service_ewma_ms':dict(self.ledger.admission.service),'governor':self.governor.snapshot(),
                           'governor_model_context':self.governor.model_context}
                    temporary=self.root/'model-lifecycle.json.tmp';temporary.write_text(json.dumps(value));temporary.replace(self.root/'model-lifecycle.json')
                except Exception as error:
                    self.errors.append('MODEL_LIFECYCLE:'+type(error).__name__+':'+str(error));self.stop.set();break
                self.lifecycle_stop.wait(1)
        self.lifecycle_thread=threading.Thread(target=lifecycle_control,name='image-model-lifecycle');self.lifecycle_thread.start()
        for name,call in (('image-dispatch',self.dispatch),('image-index',self.index_coordinate),('image-coordination',lambda:self.coordinate(people_threshold))):
            thread=threading.Thread(target=call,name=name);thread.start();self.threads.append(thread)

    def close(self):
        self.stop.set();self.lifecycle_stop.set()
        if self.lifecycle_thread:self.lifecycle_thread.join(timeout=15)
        if self.lifecycle_thread and self.lifecycle_thread.is_alive():raise RuntimeError('LIFECYCLE_CONTROLLER_NOT_STOPPED')
        self.consumers.stop.set()
        self.tail_boost.shutdown()
        # Graceful shutdown includes cold model loading, not just a typical
        # 16s inference. Never kill a model to satisfy a 30s UI deadline.
        for thread in self.consumers.threads:thread.join(timeout=360)
        if any(t.is_alive() for t in self.consumers.threads):raise RuntimeError('IMAGE_WORKER_ACTIVE_DO_NOT_RESUME')
        super().close()
        value={'at':time.time(),'lifecycle':self.lifecycle.snapshot(),'tail_boost':self.tail_boost.snapshot(),
               'errors':self.errors,'historical':True}
        (self.root/'model-lifecycle.json').write_text(json.dumps(value))

    def feed(self,*args,**kwargs):raise RuntimeError('IMAGE_ONLY_FORBIDS_VIDEO_PRODUCER')
    def add_source(self,*args,**kwargs):raise RuntimeError('USE_ADD_IMAGE')
    def frame(self,*args,**kwargs):raise RuntimeError('IMAGE_IS_NOT_VIDEO_FRAME')
    def feed_existing(self,*args,**kwargs):raise RuntimeError('IMAGE_ONLY_FORBIDS_VIDEO_REPLAY')
