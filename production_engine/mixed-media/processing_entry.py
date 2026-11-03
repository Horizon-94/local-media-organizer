import argparse,importlib.util,json,os,signal,sys,time,uuid
from pathlib import Path
from apps.media_archive_image_video_ui.video_telemetry import ClosureSpan
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from mixed_observer import read,save,protected,finalize
def validate_source_rows(rows,source_root,approved_roots=None):
    primary=Path(source_root).resolve(strict=True)
    roots={Path(x).resolve(strict=True) for x in (approved_roots or [source_root])}
    if primary not in roots:raise ValueError('PRIMARY_ROOT_NOT_AUTHORIZED')
    if len({r['source_id'] for r in rows})!=len(rows):raise ValueError('DUPLICATE_SOURCE_IDENTITY')
    if len({r['relative_path'] for r in rows})!=len(rows):raise ValueError('DUPLICATE_SOURCE_PATH')
    for row in rows:
        path=Path(row['path']);root=Path(row.get('source_root',source_root)).resolve(strict=True)
        if root not in roots:raise ValueError('MANIFEST_SOURCE_ROOT_NOT_AUTHORIZED')
        expected=root/row.get('physical_relative_path',row['relative_path'])
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root) or path.resolve()!=expected.resolve():raise ValueError('MANIFEST_SOURCE_ESCAPES_AUTHORIZED_ROOT')

def source_registration_order(rows,runtime=None):
    """Interleave admission inputs; retain each media kind's frozen order.

    Existing workers/queues still own execution and all resource admission.
    A blocked video frontend must not put every unregistered image behind it.
    """
    if runtime is not None:
        # Durable inputs consume preparation credit before their in-memory
        # producer inputs are restored. Restore those identities before a new
        # source can wait on their credit. Every row still passes the normal
        # content verification, add/revision fence and frontend below.
        with runtime.ledger.connection() as c:
            restored={r[0] for r in c.execute('SELECT DISTINCT source_id FROM video_stream_inputs_v1')}
            spool_occupants={r[0] for r in c.execute("""SELECT s.source_id
                FROM video_stream_source_states_v1 s
                JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision
                JOIN video_spool_reservations_v1 r ON r.token=json_extract(s.payload,'$.reservation_token')
                WHERE s.state='EXTRACTION_DONE'""")}

            occupants={r[0] for r in c.execute("""SELECT i.source_id FROM video_stream_inputs_v1 i
                JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision
                WHERE json_extract(i.source_json,'$.media_kind')='video' AND i.audio_json IS NULL
                AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 s
                  WHERE s.source_id=i.source_id AND s.revision=i.revision AND s.state='EXTRACTION_DONE')""")}
            closed={r[0] for r in c.execute('''SELECT i.source_id FROM video_stream_inputs_v1 i
                JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision
                WHERE EXISTS(SELECT 1 FROM video_stream_source_states_v1 s
                  WHERE s.source_id=i.source_id AND s.revision=i.revision
                    AND s.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL'))''')}
        # Restore actual occupied spool consumers before raw preparation credit.
        yield from source_registration_order([r for r in rows if r['source_id'] in spool_occupants])
        yield from source_registration_order([r for r in rows if r['source_id'] in occupants and r['source_id'] not in spool_occupants])
        occupants |= spool_occupants
        # Resume must restore live producers/closure before rereading every
        # terminal source. This changes only registration order: verification,
        # revision fences, normal add, frontend and model queue policy remain.
        remaining=[r for r in rows if r['source_id'] in restored and r['source_id'] not in occupants]
        yield from source_registration_order([r for r in remaining if r['source_id'] not in closed])
        # Supply new unfinished sources before revalidating terminal history.
        # Credit occupants and all restored unfinished identities remain first.
        yield from source_registration_order([r for r in rows if r['source_id'] not in restored])
        yield from source_registration_order([r for r in remaining if r['source_id'] in closed])
        return
    from collections import deque
    groups={}
    for row in rows:groups.setdefault(row['media_kind'],deque()).append(row)
    while groups:
        for kind in tuple(groups):
            yield groups[kind].popleft()
            if not groups[kind]:del groups[kind]

def wait_preparation_capacity(runtime,row):
    """Bound raw preparation ahead of either branch, never global closure.

    Restored identities bypass this buffer so Resume cannot deadlock before
    restoring its producer inputs. Image admission keeps its accepted order.
    """
    if row.get('media_kind')!='video':return True
    with runtime.ledger.connection() as c:
        if c.execute('SELECT 1 FROM video_stream_inputs_v1 WHERE source_id=? LIMIT 1',(row['source_id'],)).fetchone():return True
    admitted={s['source_id'] for s,r in runtime.sources}
    if any(kind=='image' and sid not in admitted for sid,kind in runtime.expected_sources.items()):return True
    last=0
    while not runtime.stop.is_set():
        with runtime.ledger.connection() as c:
            pending=c.execute("""SELECT count(*) FROM video_stream_inputs_v1 i
                JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision
                WHERE json_extract(i.source_json,'$.media_kind')='video' AND i.audio_json IS NULL
                AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 s
                  WHERE s.source_id=i.source_id AND s.revision=i.revision AND s.state='EXTRACTION_DONE')""").fetchone()[0]
        runtime.raw_preparation_snapshot=dict(at=time.time(),usage=pending,capacity=2,blocked=pending>=2,reason='RAW_PREPARATION_BUFFER_FULL' if pending>=2 else None)
        if pending<2:return True
        if time.monotonic()-last>=30:
            with (runtime.root/'source-preparation-admission.jsonl').open('a') as f:
                f.write(json.dumps({'at':time.time(),'reason_code':'RAW_PREPARATION_BUFFER_FULL','raw_sources_neither_branch_prepared':pending,'capacity':2,'scope':'raw preparation only; not Whisper or source closure'})+'\n')
            last=time.monotonic()
        runtime.stop.wait(.2)
    return False

def project_terminal(runtime, snapshot, publication_wait, expected_sources):
    """Completion requires durable closure AND sealed future work, never idleness."""
    if publication_wait or snapshot.get('closure_pending') or snapshot.get('model_remaining'):
        return False
    if not getattr(runtime, 'discovery_sealed', False):
        return False
    if {s['source_id'] for s, _ in runtime.sources} != set(expected_sources):
        return False
    proofs = runtime.drain_proofs()
    return bool(proofs) and all(proof.eligible for proof in proofs.values())

def execute(args):
    if args.incremental and not args.resume:raise ValueError('INCREMENTAL_REQUIRES_RESUME_WORKSPACE')
    if not (args.audio_acceptance or args.mixed_smoke or args.prepared or args.image_video_release):raise ValueError('FULL_MIXED_REQUIRES_QUALIFIED_PREPARED_TICKET')
    if args.image_video_release and (args.audio_acceptance or args.mixed_smoke or args.prepared):raise ValueError('RELEASE_SCOPE_MODE_CONFLICT')
    from audio_frontend import AudioRuntime
    from mixed_inventory import inventory
    root=args.workspace_root.resolve()
    from production_workspace import validate_entry
    validate_entry(root,args.source,args.run_id,args.resume)
    from apps.media_archive_image_video_ui.stop_provenance import record,signal_origin
    interrupted=False;runtime=None;failures=[];status='FAIL_BATCH'
    def stop(signum,frame):
        nonlocal interrupted
        interrupted=True
        evidence_root=(args.resume or root/args.run_id)/'runtime'
        origin,reason=signal_origin(evidence_root,signum)
        if runtime:runtime.stop.request(origin,reason,signal=signum)
        else:record(evidence_root,'STOP_REQUESTED',origin,reason,signal=signum)
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    from progressive_ingestion import Ingestion,IngestionInterrupted
    ingestion=Ingestion(root,cancel=lambda:interrupted or bool(runtime and runtime.stop.is_set()))
    if args.resume or args.prepared:
        run=(args.resume or args.prepared).resolve(strict=True);config=read(run/'run-config.json',{})
        if config.get('contract')!=('standalone-audio-acceptance-v1' if args.audio_acceptance else 'mixed-terminal-v1'):raise ValueError('RESUME_MEDIA_CONTRACT_MISMATCH')
        if args.prepared and not args.resume and (config.get('status')!='PREPARED_NOT_STARTED' or (run/'runtime/video.sqlite').exists()):raise ValueError('EXISTING_RUN_REQUIRES_RESUME')
        for name in ('runtime','logs','metrics','reports','spool'):(run/name).mkdir(exist_ok=True)
        inv=read(run/'manifest.json');rows=inv['files']
        if args.incremental and (run/'project-source-set.json').is_file():raise ValueError('EXPANDED_PROJECT_USE_ADD_FOLDER')
        if args.incremental:
            if not args.resume:raise ValueError('INCREMENTAL_REQUIRES_RESUME_WORKSPACE')
            from release_incremental import prepare
            descriptor=prepare(run,config)
            save(run/'incremental-current.json',descriptor)
            save(run/'INCREMENTAL_DIFF_SUMMARY.json',{'counts':descriptor['diff_counts'],'sources':descriptor['diff'],'source_root':str(args.source),'missing_policy':'historical results retained; relocation is new plus missing unless proven'})
        descriptor=read(run/'incremental-current.json')
        if descriptor:
            inv=descriptor['manifest'];rows=inv['files']
            save(run/'manifest.json',inv);save(run/'image-plan.json',descriptor['plan'])
            config['ignored_sources']=descriptor['ignored']
            config['inventory_counts']={kind:sum(r['media_kind']==kind for r in rows) for kind in ('image','video')}
            config['discovery_snapshot']=descriptor['snapshot']
        from apps.media_archive_image_video_ui.build151_project_expansion import apply_descriptor
        task_path=os.environ.get('MEDIA_ARCHIVE_BUILD150_TASK')
        expanded=apply_descriptor(run,read(Path(task_path))) if task_path else None
        if expanded:
            inv=expanded['manifest'];rows=inv['files']
            save(run/'manifest.json',inv);save(run/'image-plan.json',expanded['plan'])
            config['inventory_counts']=inv['counts'];config['source_roots']=inv['source_roots']
    else:
        inv=read(args.inventory_json) if args.inventory_json else inventory(args.source,include_audio=not args.image_video_release,progress=ingestion.inventory_progress,defer_video_probe=args.image_video_release)
        if inv['source_root']!=str(args.source.resolve()):raise ValueError('INVENTORY_SOURCE_MISMATCH')
        validate_source_rows(inv['files'],inv['source_root'],inv.get('source_roots'))
        if args.image_video_release:
            from release_image_video import release_rows
            rows,ignored=release_rows(inv)
        else:rows=[r for r in inv['files'] if r['media_kind']=='audio'] if args.audio_acceptance else smoke_sample(inv)
        if not rows:raise ValueError('NO_SUPPORTED_SOURCES')
        import re
        if args.run_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',args.run_id):raise ValueError('UNSAFE_RUN_ID')
        run=root/(args.run_id or (time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]));run.mkdir(parents=True,exist_ok=False)
        for name in ('runtime','logs','metrics','reports','spool'):(run/name).mkdir()
        save(run/'manifest.json',{**inv,'files':rows})
        from collections import Counter
        config={'contract':'standalone-audio-acceptance-v1' if args.audio_acceptance else 'mixed-terminal-v1','run_id':run.name,'source_root':inv['source_root'],
                'inventory_counts':dict(Counter(r['media_kind'] for r in rows)),'started_at':time.time(),'qwen_workers':0 if args.audio_acceptance else 1,'tail_boost':False}
        if args.image_video_release:config.update(release_scope='BUILD150_IMAGE_VIDEO_ONLY',whisper_workers=1,ignored_sources=ignored)
    if config.get('release_scope')=='BUILD150_IMAGE_VIDEO_ONLY':
        args.image_video_release=True
        if any(r['media_kind'] not in ('image','video') for r in rows):raise ValueError('RELEASE_FORBIDS_STANDALONE_AUDIO_REGISTRATION')
    validate_source_rows(rows,inv['source_root'],inv.get('source_roots'))
    if 'source_volume' not in config:
        from mixed_inventory import volume_evidence
        config.update(volume_evidence(Path(inv['source_root']),run,inv))
    config.update(status='RUNNING',pid=os.getpid(),started_at=config.get('started_at') or time.time());save(run/'run-config.json',config)
    if args.image_video_release:
        from release_image_video import print_discovery
        print_discovery(rows,config.get('ignored_sources',{}))
    (run/'logs/runner.pid').write_text(str(os.getpid()))
    for k,v in {'RUN_ID':run.name,'WORKSPACE':run,'DATABASE':run/'runtime/video.sqlite','SOURCE_ROOT':inv['source_root'],
                'SOURCE INVENTORY':len(rows),'MONITOR':'APP_BUILD150_STATUS'}.items():print(k,'=',v,flush=True)
    before=protected(run/'runtime/video.sqlite');save(run/'reports/PROTECTED_DONE_BEFORE.json',before)
    save(run/'reports'/('PROTECTED_DONE_BEFORE_'+str(time.time_ns())+'.json'),before)
    try:
        observer_spec=importlib.util.spec_from_file_location('_mixed_writer_observer',Path(__file__).parents[1]/'image-batch/image_observer.py')
        observer_module=importlib.util.module_from_spec(observer_spec);observer_spec.loader.exec_module(observer_module)
        producer_spec=importlib.util.spec_from_file_location('_mixed_producer_observer',Path(__file__).parents[1]/'video-batch/model_setup.py')
        producer_observer=importlib.util.module_from_spec(producer_spec);producer_spec.loader.exec_module(producer_observer)
        decoder_generation=producer_observer.diagnostic_ffmpeg_launcher(run,rows) if any(r['media_kind']=='video' for r in rows) else None
        if decoder_generation is None:producer_observer.prepare_evidence_index(run/'runtime',{row['source_id']:row['path'] for row in rows})
        writer=observer_module.WriterSummary();last_metrics=0
        from mixed_costs import PreparationCosts
        prep_costs=PreparationCosts({r['source_id']:r['media_kind'] for r in rows})
        if args.audio_acceptance:contracts,handlers,monitor=native(run/'runtime',audio_only=True)
        else:
            from mixed_native import native as mixed_native
            contracts,handlers,monitor=mixed_native(run/'runtime')
            ticket=read(run/'ticket.json')
            if ticket:
                from mixed_preparation import model_identity
                actual=model_identity(read(run/'runtime/model-config.json'),run/'runtime')
                if actual!=ticket['pinned_profile']['model_identity']:raise ValueError('PREPARED_MODEL_IDENTITY_CHANGED')
        frozen=read(run/'contracts.json')
        from resume_relocation import normalize
        relocation_task=read(Path(os.environ.get('MEDIA_ARCHIVE_BUILD150_TASK','')), {}) if os.environ.get('MEDIA_ARCHIVE_BUILD150_TASK') else {}
        if frozen and relocation_task.get('path_relocations'):
            contracts,relocation_receipt=normalize(frozen,contracts,read(run/'runtime/model-config.json')['specs'],relocation_task,REPO.parents[2],Path(__file__).with_name('approved_workspace_relocation.json'),'1c0e3e9d806da014f39097e7ab99ac3454c659dd1eb744e204f6127827184700')
            if relocation_receipt:save(run/'runtime/verified-path-relocation.json',relocation_receipt)
        from resume_compatibility import resolve_contracts
        contracts,migration=resolve_contracts(frozen,contracts,
            Path(__file__).with_name('approved_resume_migration.json'),
            '7cd858ace1e74aa5fd6225dc14e251b99ab057e31eefc8c1ed2af853bf3c60b0',REPO,
            {'APP_VERSION':'1.3.2','BUILD':'151','PROCESSING_ENGINE':'BUILD150_ONLY'})
        if migration:
            lineage=run/('runtime/resume-contract-lineage-build151-closed-issues-'+migration['MIGRATION_MANIFEST_SHA'][:16]+'.json')
            previous=read(lineage)
            if previous is not None and previous!=migration:
                raise ValueError('RESUME_CONTRACT_CHANGED:LINEAGE_CONFLICT')
            if previous is None:save(lineage,migration)
        # Existing jobs retain their historical identity and results. Fresh
        # implementations are explicitly attributed by the lineage receipt.
        if frozen is None:save(run/'contracts.json',contracts)
        if args.audio_acceptance:runtime=AudioRuntime(run/'runtime',contracts,handlers,resume=bool(args.resume),resource_monitor=monitor)
        else:
            from apps.media_archive_image_video_ui.image_frontend import build_plan,metadata,verify_resume_authority
            from mixed_runtime import MixedRuntime
            selected={**inv,'files':rows}
            if args.resume or args.prepared:
                plan=read(run/'image-plan.json')
                from release_incremental import verify_current
                verify_current(plan,rows)
            else:
                fields=metadata([Path(r['path']) for r in rows if r['media_kind']=='image'],required=True)
                selected['files']=[{**r,'metadata':fields.get(r['path'],{})} if r['media_kind']=='image' else r for r in rows]
                selected['files'] += [r for r in inv['files'] if r['media_kind']=='sidecar']
                plan=build_plan(selected);save(run/'image-plan.json',plan);save(run/'metadata.json',fields)
            from prepared_run import storage_budget
            from dataclasses import asdict
            from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy
            # A full prepared run must retain its independently calibrated
            # capacity evidence, not silently revert to the generic draft.
            budget_inventory=read(run/'inventory.json',selected)
            profile=config.get('pinned_profile') or {'preview_required':sum(d['should_generate_preview'] for d in plan['decisions']),'sampling_policy':asdict(SamplingPolicy())}
            if args.image_video_release:
                from release_image_video import release_storage_budget
                budget=release_storage_budget(rows,plan,run)
            else:budget=storage_budget(budget_inventory,run,profile=profile)
            save(run/'metrics/storage-budget.json',budget)
            if not budget['enough_space']:raise ValueError('DISK_SPACE_BLOCKER:'+json.dumps(budget))
            runtime=MixedRuntime(run/'runtime',contracts,handlers,plan=plan,resume=bool(args.resume),resource_monitor=monitor,expected_sources=rows,
                incremental_changed=read(run/'incremental-current.json',{}).get('changed_sources',[]))
        # Candidate-only, opt-in safe-point stacks. No tracing loop or models.
        if (Path(__file__).resolve().parents[3]/'build151_local_validation.json').is_file():
            from types import SimpleNamespace
            from apps.media_archive_image_video_ui.v2_runtime_observer import register_live_stack_signal
            runtime._stack_diagnostic=SimpleNamespace(owner={'scheduler_run_id':runtime.ledger.owner,'execution_epoch':runtime.ledger.epoch})
            register_live_stack_signal(runtime._stack_diagnostic,{'scheduler_trace_path':str(runtime.root/'candidate-control')})
        runtime.decoder_catalog={row['source_id']:row['path'] for row in rows}
        runtime.decoder_generation=decoder_generation
        import threading
        summary_lock=threading.RLock()
        def _warm_summaries_owned():
            with ClosureSpan(run,'BACKGROUND_WRITER_SUMMARY','WriterSummary.update'):writer.update(run/'runtime')
            with ClosureSpan(run,'BACKGROUND_PREPARATION_SUMMARY','PreparationCosts.update'):prep_costs.update(run/'runtime')
            producer_observer.decode_evidence(runtime)
        def warm_summaries():
            with summary_lock:_warm_summaries_owned()
        runtime.decoder_evidence_warmer=warm_summaries
        if args.audio_acceptance:
            for row in rows:
                if interrupted:break
                if args.audio_acceptance:runtime.add_audio(row)
                else:runtime.add(row)
            if not args.audio_acceptance:runtime.discovery_sealed=not interrupted
            runtime.start(.58)
            if not config.get('processing_started_at'):config['processing_started_at']=time.time()
            config.update(last_runtime_started_at=time.time(),full_mixed_started=bool(args.prepared))
            if args.resume:config['full_mixed_started']=bool(config.get('full_mixed_started') or read(run/'ticket.json'))
            save(run/'run-config.json',config)
            for source,rev in (list(runtime.sources) if args.audio_acceptance else frontend_order(runtime)):
                if interrupted or runtime.stop.is_set():break
                try:
                    if args.audio_acceptance:runtime.prepare_audio(source,rev)
                    else:runtime.prepare_source(source,rev)
                except InterruptedError:
                    interrupted=True;break
                except Exception as error:
                    failures.append({'source_id':source['source_id'],'path':source['relative_path'],'stage':source.get('media_kind','video')+'_frontend','reason':str(error),'resumable':True})
                    runtime.adapters.state(source['source_id'],rev,'SOURCE_FRONTEND_FAILED',failures[-1])
        else:
            # Register one verified source, start existing workers, then admit the
            # next source while downstream work runs. Existing writer/admission owns jobs.
            started_runtime=False
            ingestion.phase('VERIFYING_CONTENT',total_sources=len(rows),counts=dict(__import__('collections').Counter(r['media_kind'] for r in rows)))
            for row in source_registration_order(rows,runtime if args.resume else None):
                if interrupted or runtime.stop.is_set():break
                if not wait_preparation_capacity(runtime,row):
                    interrupted=True;break
                try:
                    from mixed_inventory import prepare_video
                    if row.get('probe_pending'):
                        ingestion.phase('INVENTORYING',current_source=row['relative_path'],total_sources=len(rows))
                        prepared_row=prepare_video(row,inv['decoder_support'],root,cancel=lambda:interrupted or runtime.stop.is_set())
                        row.clear();row.update(prepared_row)
                    from release_image_video import require_source_space
                    capacity=require_source_space(row,run)
                    ingestion.emit(capacity=capacity)
                    digest=ingestion.verify(row) if row['media_kind'] in ('video','image') else None
                    ingestion.phase('ENQUEUING',current_source=row['relative_path'])
                    if args.audio_acceptance:runtime.add_audio(row)
                    else:runtime.add(row,verified_digest=digest)
                    if row['media_kind']=='video':
                        from mixed_inventory import commit_video_metadata
                        pair=next(pair for pair in runtime.sources if pair[0]['source_id']==row['source_id'])
                        commit_video_metadata(runtime,row,pair[1])
                    if not started_runtime:
                        runtime.start(.58);started_runtime=True
                        if not config.get('processing_started_at'):config['processing_started_at']=time.time()
                        config.update(last_runtime_started_at=time.time(),full_mixed_started=bool(args.prepared))
                        if args.resume:config['full_mixed_started']=bool(config.get('full_mixed_started') or read(run/'ticket.json'))
                        save(run/'run-config.json',config)
                        ingestion.emit(force=True,first_runtime_started_at=time.time())
                    pair=next((pair for pair in runtime.sources if pair[0]['source_id']==row['source_id']),None)
                    if pair:
                        source,rev=pair
                        for ready_source,ready_rev in frontend_order_subset(runtime,[pair]):
                            if args.audio_acceptance:runtime.prepare_audio(ready_source,ready_rev)
                            else:runtime.prepare_source(ready_source,ready_rev)
                    ingestion.emit(force=True,enqueued_count=ingestion.state.get('enqueued_count',0)+1,last_progress_time=time.time())
                except InterruptedError:
                    interrupted=True;break
                except Exception as error:
                    ingestion.failure(error,row)
                    failures.append({'source_id':row['source_id'],'path':row['relative_path'],'stage':'CONTENT_VERIFY_OR_ENQUEUE','reason':str(error),'exception_type':type(error).__name__,'errno':getattr(error,'errno',None),'resumable':True})
            # One atomic manifest publication after admission (including safe interruption);
            # per-source probe caches already survive an abrupt exit during the loop.
            save(run/'manifest.json',{**inv,'files':rows})
            if not args.audio_acceptance:runtime.discovery_sealed=not interrupted
            if not started_runtime:
                runtime.start(.58);started_runtime=True
            ingestion.phase('STOPPED_RESUMABLE' if interrupted else 'PROCESSING',current_source=None)
        progress=None;last=time.monotonic()
        while not interrupted and not runtime.stop.is_set():
            if getattr(runtime,'search_publication',False):runtime.publish_search()
            publication_wait=False
            if getattr(runtime,'search_publication',False):
                with runtime.ledger.connection() as c:
                    publication_wait=bool(c.execute('SELECT 1 FROM release_registry WHERE present=1 AND terminal=0 LIMIT 1').fetchone())
            snap=read(run/'runtime/runtime.json') or runtime.snapshot();key=json.dumps((snap['counts'],snap['closure_pending']),sort_keys=True)
            if key!=progress:progress=key;last=time.monotonic()
            if time.monotonic()-last_metrics>=5:
                with summary_lock:
                    writer.update(run/'runtime')
                    prep_costs.update(run/'runtime')
                    save(run/'metrics/producer.json',producer_observer.decode_evidence(runtime))
                from mixed_observer import snapshot as observe
                observed=observe(run)
                sample={k:observed[k] for k in ('at','sources','stages','documents','coverage','model_remaining','eta')}
                sample['closure_pending_sources']=len(observed['closure_pending'])
                with (run/'metrics/samples.jsonl').open('a') as f:f.write(json.dumps(sample,ensure_ascii=False)+'\n')
                last_metrics=time.monotonic()
            if project_terminal(runtime,snap,publication_wait,[r['source_id'] for r in rows]):break
            with runtime.ledger.connection() as c:
                terminal=c.execute("SELECT count(DISTINCT s.source_id) FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE s.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')").fetchone()[0]
            failed_sources={f['source_id'] for f in failures if f.get('source_id')}
            with runtime.ledger.connection() as c:
                terminal_sources={r[0] for r in c.execute("SELECT s.source_id FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE s.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL')")}
            # Recoverable pending/dependency/resource waits are not terminal.
            # No-progress telemetry is retained without killing the owner.
            if time.monotonic()-last>args.watchdog_seconds:
                record(run/'runtime','RUNNING_WAITING','WATCHDOG','DURABLE_WORK_NOT_TERMINAL',
                       timeout_seconds=args.watchdog_seconds,pending_gates=snap['closure_pending'])
                last=time.monotonic()
            time.sleep(.5)
        ingestion.phase('FINALIZING')
        status='INTERRUPTED_RESUMABLE' if interrupted else 'PASS_BATCH' if not runtime.errors and not failures else 'FAIL_BATCH'
    except Exception as error:
        import traceback
        record(run/'runtime','STOP_REQUESTED','PROCESSING_FATAL','RUNTIME_EXCEPTION',error=str(error),traceback=traceback.format_exc())
        failures.append({'stage':'runtime','reason':str(error),'traceback':traceback.format_exc()})
    finally:
        if runtime:
            record(run/'runtime','IN_FLIGHT_DRAIN_BEGIN','RUNTIME_FINALIZER','DRAIN')
            closed_for_audit=False
            try:runtime.close();closed_for_audit=True
            except Exception as error:failures.append({'stage':'shutdown','reason':str(error)});status='FAIL_BATCH'
            drain_end_mono=time.monotonic();drain_end_wall=time.time()
            record(run/'runtime','IN_FLIGHT_DRAIN_END','RUNTIME_FINALIZER','DRAINED' if closed_for_audit else 'DRAIN_FAILED',DRAIN_SUCCESS=closed_for_audit,DRAIN_END_MONOTONIC=drain_end_mono,DRAIN_END_WALL=drain_end_wall)
            record(run/'runtime','FINALIZER_BEGIN','RUNTIME_FINALIZER','POST_DRAIN')
            if monitor:save(run/'runtime/worker-exit-audit.json',monitor.workers)
            with ClosureSpan(run,'C_WRITER_SUMMARY','WriterSummary.update'):
                writer.update(run/'runtime')
            with ClosureSpan(run,'B_PREPARATION_SUMMARY','PreparationCosts.update'):
                prep_costs.update(run/'runtime')
            with ClosureSpan(run,'A_PRODUCER_EVIDENCE','model_setup.decode_evidence'):
                save(run/'metrics/producer.json',producer_observer.decode_evidence(runtime,final=closed_for_audit))
            with ClosureSpan(run,'OBSERVER_TOTAL','mixed_observer.finalize'):
                status=finalize(run,status,before,failures)
            if args.image_video_release and status=='PASS_BATCH':
                from release_image_video import acceptance
                if not acceptance(run):
                    failures.append({'stage':'release_acceptance','reason':'SEARCH_OR_SPEECH_PUBLICATION_GATE_FAILED'})
                    status=finalize(run,'FAIL_BATCH',before,failures)
        else:
            config.update(status='FAIL_BATCH',finished_at=time.time());save(run/'run-config.json',config)
            save(run/'reports/FINAL_BATCH_REPORT.json',{'status':'FAIL_BATCH','failures':failures})
        record(run/'runtime','FINALIZER_END','RUNTIME_FINALIZER',status)
        with ClosureSpan(run,'R_STOPPED_RESUMABLE_PUBLICATION','Ingestion.phase'):
            ingestion.phase('COMPLETED' if status=='PASS_BATCH' else 'STOPPED_RESUMABLE' if status=='INTERRUPTED_RESUMABLE' else 'ERROR',final_status=status)
        with ClosureSpan(run,'S_STOP_FINALIZED_PUBLICATION','stop_provenance.record'):
            record(run/'runtime','STOP_FINALIZED','RUNTIME_FINALIZER',status,checkpoint_written=(run/'reports/FINAL_BATCH_CHECKPOINT.md').is_file(),errors=getattr(runtime,'errors',[]),failures=failures)
        (run/'logs/runner.exit').write_text(str(0 if status=='PASS_BATCH' else 130 if status=='INTERRUPTED_RESUMABLE' else 1))
        for k,v in {'STATUS':status,'WORKSPACE':run,'REPORT_MD':run/'reports/FINAL_BATCH_REPORT.md',
                    'REPORT_JSON':run/'reports/FINAL_BATCH_REPORT.json','CHECKPOINT':run/'reports/FINAL_BATCH_CHECKPOINT.md'}.items():print(k,'=',v,flush=True)
    return 0 if status=='PASS_BATCH' else 130 if status=='INTERRUPTED_RESUMABLE' else 1

def frontend_order(runtime):
    yield from frontend_order_subset(runtime,list(runtime.sources))

def frontend_order_subset(runtime,pairs):
    remaining=list(pairs)
    try:
        while remaining and not runtime.stop.is_set():
            rows=[{'source_id':s['source_id'],'sequence':n} for n,(s,rev) in enumerate(remaining)]
            ordered=runtime.ledger.admission.order('source_frontend',rows)
            if not ordered:
                runtime.stop.wait(.1);continue
            sid=ordered[0]['source_id'];position=next(n for n,(s,rev) in enumerate(remaining) if s['source_id']==sid)
            pair=remaining.pop(position);runtime.ledger.admission.claimed('source_frontend',sid)
            try:yield pair
            finally:runtime.ledger.admission.release('source_frontend','frontend_complete_or_blocked')
    finally:
        end=getattr(runtime.ledger.admission,'end_stage',None)
        if end:end('source_frontend')
