"""Publication adapter around the unchanged 1.2.8 decoder and fallback policy."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import threading
import uuid
from .v2_producer import ProducerStore, SamplingPolicy, ProducerConflict, fingerprint


def install(module, db, *, scheduler_owner=None,before_publish=None):
    # Snapshot identity; never adopt later mutations to a caller-owned run dict.
    scheduler_owner = dict(scheduler_owner) if scheduler_owner is not None else None
    if scheduler_owner is not None:
        from .v2_product_driver import validate_owner
        scheduler_owner = validate_owner(db, scheduler_owner)
    policy=SamplingPolicy(offset_ms=module.SAMPLING_OFFSET_MS,
                          interval_ms=module.SAMPLING_INTERVAL_MS,long_edge=module.MAX_EDGE_PX)
    # Retain the exact legacy sampling contract/ID: publication is not sampling.
    local=threading.local()
    original_process=module.process_one
    original_decode=module.run_single_decode_attempt
    original_single=module.run_single_frame_attempt

    def begin(ctx,effective,decoder):
        attempt=uuid.uuid4().hex
        store=ProducerStore(db,effective,artifact_root=Path(db).parent/'v2-artifacts',scheduler_owner=scheduler_owner)
        store.before_publish=before_publish
        with store.connect() as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_decode_attempts'").fetchone()
            interrupted=list(c.execute("SELECT source_revision FROM v2_decode_attempts WHERE source_id=? AND status='RUNNING'",(ctx['source'],))) if exists else []
        for old in interrupted:
            if scheduler_owner:
                with store.connect() as c:
                    owned=c.execute('SELECT 1 FROM v2_source_epochs WHERE source_id=? AND source_revision=? AND scheduler_run_id=? AND execution_epoch=?',(ctx['source'],old[0],scheduler_owner['scheduler_run_id'],scheduler_owner['execution_epoch'])).fetchone()
                if not owned:continue
            store.abort_decode_attempt(ctx['source'],old[0],'interrupted_decode_restarted')
        revision=store.source_ready(ctx['source'],ctx['sha'],ctx['sha'],ctx['path'],ctx['metadata'],execution_attempt=attempt)
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS v2_decode_attempts(attempt_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,decoder TEXT,status TEXT,report TEXT)')
            c.execute("INSERT INTO v2_decode_attempts VALUES(?,?,?,?,'RUNNING','{}')",(attempt,ctx['source'],revision,decoder))
        ctx.update(store=store,revision=revision,attempt=attempt)
        return store,revision

    def status(ctx,value,report):
        with ctx['store'].connect() as c:
            c.execute('UPDATE v2_decode_attempts SET status=?,report=? WHERE attempt_id=?',(value,json.dumps(report,sort_keys=True),ctx['attempt']))

    @contextmanager
    def observe(ctx,effective,decoder,out):
        store,revision=begin(ctx,effective,decoder)
        stopped=threading.Event();errors=[]
        def scan():
            while not stopped.is_set():
                try:store.observe(out,ctx['source'],revision)
                except Exception as exc:errors.append(exc);return
                stopped.wait(.02)
        with store.source_lease(ctx['source'],revision):
            thread=threading.Thread(target=scan);thread.start()
            try:yield
            finally:stopped.set();thread.join()
            store.observe(out,ctx['source'],revision)
            if errors:raise errors[0]

    def attempt(call,source,out,*args,single=False):
        ctx=local.context
        effective=replace(policy,explicit_timestamps_ms=(args[0],)) if single else policy
        try:
            with observe(ctx,effective,args[-1],out):
                result=call(source,out,*args)
            if not result['success']:
                ctx['store'].abort_decode_attempt(ctx['source'],ctx['revision'],'legacy_decode_attempt_failed')
                status(ctx,'ABORTED',result)
            return result
        except Exception:
            if ctx.get('revision'):
                ctx['store'].abort_decode_attempt(ctx['source'],ctx['revision'],'publication_or_decode_exception')
                status(ctx,'ABORTED',{})
            raise

    module.run_single_decode_attempt=lambda source,out,pattern,vf,decoder:attempt(original_decode,source,out,pattern,vf,decoder)
    module.run_single_frame_attempt=lambda source,out,time_ms,decoder:attempt(original_single,source,out,time_ms,decoder,single=True)

    def process(job,total,source,base,out,action):
        from .v2_model_workers import sha_file
        source_id=str(base.get('parent_source_content_id') or base.get('source_content_id') or base.get('source_video_id') or '')
        if not source_id:raise ProducerConflict('stream_source_identity_missing')
        sha=sha_file(source);lock=ProducerStore(db,policy,scheduler_owner=scheduler_owner)
        ctx={'source':source_id,'sha':sha,'path':source,'metadata':base}
        local.context=ctx
        # Isolate work from legacy outputs; a crash restart starts a fresh attempt.
        lease_revision = fingerprint((policy.revision(sha), scheduler_owner)) if scheduler_owner else policy.revision(sha)
        isolated=out/'v2-processing'/lease_revision
        with lock.source_lease(source_id,lease_revision):
            try:
                result=original_process(job,total,source,base,isolated,action)
                report,frames,_=result
                if 'store' not in ctx:begin(ctx,policy,'legacy-no-decode')
                store,revision=ctx['store'],ctx['revision']
                store.observe(isolated,source_id,revision)
                # Only actual legacy slots are required. Expected count/tail
                # tolerance remain in the durable original report, not a new gate.
                store.declare_slots(source_id,revision,len(frames))
                valid=all(int(row.get('time_position_ms',row.get('estimated_frame_time_ms',-1)))==store.policy.timestamp(i) for i,row in enumerate(frames))
                with store.connect() as c:
                    ready=c.execute("SELECT count(*) FROM v2_sample_slots WHERE source_id=? AND source_revision=? AND status='READY'",(source_id,revision)).fetchone()[0]
                success=report.get('checkpoint_status')=='completed' and valid and ready==len(frames)
                if success:store.finish(source_id,revision,len(frames)-1,success=True)
                else:store.abort_decode_attempt(source_id,revision,'legacy_or_publication_failed')
                status(ctx,'EOS' if success else 'ABORTED',report)
                report['v2_source_revision']=revision
                if not success and report.get('checkpoint_status')=='completed':raise ProducerConflict('legacy_publication_incomplete')
                return result
            except Exception:
                if ctx.get('store'):ctx['store'].abort_decode_attempt(source_id,ctx['revision'],'producer_interrupted')
                raise
            finally:del local.context
    module.process_one=process
