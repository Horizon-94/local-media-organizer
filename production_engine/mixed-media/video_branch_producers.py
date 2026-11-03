"""Branch-local producers using existing admission ordering and runtime Stop.

No model pools, scheduler, ledger schema, source revisions or visual sampling
changes. One Visual producer and one native Audio producer, matching variant C.
"""
from apps.media_archive_image_video_ui.stop_provenance import child_run,child_spawn
import json,threading,time,uuid,subprocess,shutil,os
from pathlib import Path
from apps.media_archive_image_video_ui.video_stream_runtime import Runtime as Engine
from apps.media_archive_image_video_ui.v2_model_workers import sha_file

AUDIO_HIGH=1024**3
AUDIO_LOW=768*1024**2

def audio_branch_terminal(c,sid,rev):
    if c.execute("SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('AUDIO_COMPLETE','SOURCE_COMPLETE','SOURCE_FAILED_FINAL')",(sid,rev)).fetchone():return True
    branch=c.execute('SELECT status,segments_json FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(sid,rev)).fetchone()
    if branch and branch[0]=='READY' and not json.loads(branch[1] or '[]'):
        # A failed VAD has no successful speech projection. It can drain as a
        # FAILURE only when both ledgers agree on the current immutable input.
        # This never creates NO_SPEECH, AUDIO_COMPLETE or a searchable source.
        artifact=c.execute('SELECT artifact_json FROM v2_audio_branches WHERE source_id=? AND source_revision=?',(sid,rev)).fetchone()
        sha=json.loads(artifact[0]).get('sha') if artifact and artifact[0] else None
        failed=bool(sha and c.execute("""SELECT 1 FROM video_stream_jobs_v1 n
            JOIN v2_current_jobs j ON j.execution_key=json_extract(n.payload,'$._evidence_key')
            WHERE n.source_id=? AND n.revision=? AND n.stage='vad' AND n.status='TERMINAL_FAILED'
              AND j.source_id=n.source_id AND j.source_revision=n.revision AND j.stage=n.stage
              AND j.status='FAILED_FINAL' AND j.input_sha=n.input_sha AND j.input_sha=?
              AND NOT EXISTS(SELECT 1 FROM v2_jobs newer WHERE newer.stage=j.stage
                  AND newer.item_id=j.item_id AND newer.revision>j.revision)
            LIMIT 1""",(sid,rev,sha)).fetchone())
        if not failed:return False
        extraction=c.execute("SELECT status,result_json FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND stage='audio_extract' ORDER BY revision DESC LIMIT 1",(sid,rev)).fetchone()
        if not extraction or extraction[0]!='DONE':return False
        result=json.loads(extraction[1])
        if result.get('status')!='READY' or result.get('sha')!=sha:return False
        if not c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage='audio_extract' AND status='DONE' LIMIT 1",(sid,rev)).fetchone():return False
        if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND (stage='whisper' OR (stage IN ('audio_extract','vad') AND status NOT IN ('DONE','TERMINAL_FAILED','SUPERSEDED'))) LIMIT 1",(sid,rev)).fetchone():return False
        if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND source_revision=? AND (stage='whisper' OR (stage IN ('audio_extract','vad') AND status NOT IN ('DONE','FAILED_FINAL','SUPERSEDED','SKIPPED'))) LIMIT 1",(sid,rev)).fetchone():return False
        return True
    if not branch or branch[0] not in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY'):return False
    expected=len(json.loads(branch[1] or '[]'))
    if c.execute("SELECT count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage='whisper'",(sid,rev)).fetchone()[0]!=expected:return False
    if c.execute("SELECT count(*) FROM v2_jobs WHERE source_id=? AND source_revision=? AND stage='whisper'",(sid,rev)).fetchone()[0]!=expected:return False
    if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('audio_extract','vad','whisper') AND status NOT IN ('DONE','TERMINAL_FAILED','SUPERSEDED')",(sid,rev)).fetchone():return False
    if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND source_revision=? AND stage IN ('audio_extract','vad','whisper') AND status NOT IN ('DONE','FAILED_FINAL','SUPERSEDED','SKIPPED')",(sid,rev)).fetchone():return False
    return True

def audio_usage(runtime):
    # Pending source audio stays owned until its branch is terminal. Completed
    # retained files still participate in disk-free protection.
    total=0;owned=set()
    with runtime.ledger.connection() as c:
        for sid,rev,raw in c.execute("SELECT source_id,revision,payload FROM video_stream_source_states_v1 WHERE state='AUDIO_ADMISSION_RESERVED'"):
            terminal=audio_branch_terminal(c,sid,rev)
            if not terminal:
                owned.add((sid,rev))
                p=json.loads(raw);directory=Path(p['directory'])
                actual=sum(x.stat().st_size for x in directory.iterdir() if x.is_file()) if directory.is_dir() else 0
                total+=max(p['reserved_bytes'],actual)
        for sid,rev,raw in c.execute('SELECT source_id,revision,audio_json FROM video_stream_inputs_v1 WHERE audio_json IS NOT NULL'):
            if (sid,rev) in owned:continue
            terminal=audio_branch_terminal(c,sid,rev)
            if not terminal:
                path=json.loads(raw).get('path')
                if path and Path(path).is_file():total+=Path(path).stat().st_size
    return total

def audio_gate(runtime):
    used=audio_usage(runtime)
    paused=getattr(runtime,'audio_watermark_paused',False)
    if used>=AUDIO_HIGH:paused=True
    elif used<=AUDIO_LOW:paused=False
    runtime.audio_watermark_paused=paused
    runtime.audio_gate_snapshot=dict(used_bytes=used,high=AUDIO_HIGH,low=AUDIO_LOW,paused=paused)
    return not paused and used<AUDIO_HIGH

def emit(runtime,**values):
    with (runtime.root/'branch-producers.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),**values})+'\n')

def audio_decision(runtime,source,rev,**fields):
    sid=source['source_id'];row=dict(event='AUDIO_SUPPLY_DECISION',source_id=sid,revision=rev,registered=True,**fields)
    if fields.get('producer_called'):runtime.audio_supply_decision={'at':time.time(),**row}
    cache=getattr(runtime,'_audio_supply_trace',{});signature=json.dumps(row,sort_keys=True,default=str)
    if cache.get(sid)==signature:return
    cache[sid]=signature;runtime._audio_supply_trace=cache
    try:emit(runtime,**row)
    except OSError:pass

def audio_prepare(runtime,source,rev):
    sid=source['source_id'];view=runtime.views['video']
    with runtime.ledger.connection() as c:
        row=c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(sid,rev)).fetchone()
    if row and row[0]:
        audio_decision(runtime,source,rev,eligible=True,already_done=True,gate_pass=True,gate_reason='REUSE_PUBLISHED_AUDIO',capacity_pass=True,capacity_reason='NO_NEW_RESERVATION',producer_called=True)
        Engine.publish_audio(view,source,rev,None)
        return True
    if not runtime.can_admit('raw_producer'):
        audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=False,gate_reason=runtime.blocked.get('raw_producer'),capacity_pass=None,capacity_reason='NOT_EVALUATED',producer_called=True);return False
    if not audio_gate(runtime):
        audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=True,gate_reason=None,capacity_pass=False,capacity_reason='AUDIO_HIGH_WATERMARK',capacity=getattr(runtime,'audio_gate_snapshot',{}),producer_called=True);return False
    try:
        probe=child_run(runtime.stop,['ffprobe','-v','error','-show_streams','-show_format','-of','json',source['path']],capture_output=True,text=True,check=True,timeout=30)
    except subprocess.TimeoutExpired as error:
        # Only the original-media preflight is the proven recoverable path.
        error.build151_source_preflight_timeout=True
        raise
    info=json.loads(probe.stdout);streams=[s for s in info['streams'] if s.get('codec_type')=='audio']
    # The accepted short-video policy chooses its canonical revision in feed.
    # Do not publish audio against a provisional revision while that edge waits.
    if float(info['format']['duration'])<=2 and view.publishers[rev].policy.explicit_timestamps_ms is None:
        audio_decision(runtime,source,rev,eligible=False,already_done=False,gate_pass=False,gate_reason='WAIT_VISUAL_SHORT_VIDEO_POLICY',capacity_pass=True,capacity_reason=None,producer_called=True)
        runtime.audio_wait_visual_policy.add(sid)
        return False
    if not streams:
        Engine.publish_audio(view,source,rev,None);return True
    stream=streams[0];duration=float(info['format']['duration']);rate=int(stream.get('bit_rate') or 0)
    reserve=min(AUDIO_HIGH,int(rate/8*duration*1.2)+4*1024**2) if rate else AUDIO_HIGH
    if audio_usage(runtime)+reserve>AUDIO_HIGH:
        runtime.audio_watermark_paused=True
        audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=True,gate_reason=None,capacity_pass=False,capacity_reason='AUDIO_RESERVATION_HIGH_WATERMARK',reserve=reserve,producer_called=True)
        return False
    if shutil.disk_usage(runtime.root).free-reserve<runtime.budget.disk_free_floor:
        audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=True,gate_reason=None,capacity_pass=False,capacity_reason='DISK_FREE_FLOOR',reserve=reserve,threshold=runtime.budget.disk_free_floor,producer_called=True);return False
    directory=runtime.root/'branch-audio'/rev
    if not directory.resolve().is_relative_to(runtime.root.resolve()):
        raise ValueError('AUDIO_SPOOL_DIRECTORY_NOT_OWNED')
    directory.mkdir(parents=True,exist_ok=True)
    # Retain interruption outputs. Charge these before allocating another run.
    previous=sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
    if previous+reserve>AUDIO_HIGH:
        audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=True,gate_reason=None,capacity_pass=False,capacity_reason='RETAINED_INTERRUPTION_CEILING',previous=previous,reserve=reserve,threshold=AUDIO_HIGH,producer_called=True);return False
    audio_decision(runtime,source,rev,eligible=True,already_done=False,gate_pass=True,gate_reason=None,capacity_pass=True,capacity_reason=None,reserve=reserve,producer_called=True)
    runtime.adapters.state(sid,rev,'AUDIO_ADMISSION_RESERVED',{'reserved_bytes':previous+reserve,'directory':str(directory)})
    target=directory/(uuid.uuid4().hex+'.mka');partial=target.with_suffix('.partial')
    args=['ffmpeg','-nostdin','-v','error','-i',source['path'],'-map','0:a:0','-vn','-sn','-dn','-c:a','copy','-f','matroska','pipe:1']
    before=Path(source['path']).stat();written=0
    with (directory/'ffmpeg.log').open('ab') as log:
        process=child_spawn(runtime.stop,args,stdout=subprocess.PIPE,stderr=log)
        (directory/'ffmpeg.pid').write_text(str(process.pid));emit(runtime,branch='audio',source_id=sid,event='FFMPEG_START',pid=process.pid,command=args)
        # A blocking pipe read is interruptible by the watcher, owned and joined.
        done=threading.Event()
        def watch():
            while not done.wait(.1):
                if runtime.stop.is_set():
                    if process.poll() is None:process.terminate()
                    return
        watcher=threading.Thread(target=watch,name='audio-stop-watch');watcher.start()
        try:
            with partial.open('xb') as out:
                while chunk:=process.stdout.read(65536):
                    if written+len(chunk)>reserve:raise RuntimeError('AUDIO_RESERVATION_CEILING')
                    if shutil.disk_usage(runtime.root).free-len(chunk)<runtime.budget.disk_free_floor:raise RuntimeError('INTERNAL_DISK_SPACE_LOW')
                    out.write(chunk);written+=len(chunk)
            code=process.wait(timeout=15)
            if runtime.stop.is_set():return False
            if code:raise RuntimeError('AUDIO_DEMUX_FAILED:'+str(code))
        finally:
            done.set();watcher.join(timeout=2)
            if process.poll() is None:process.terminate();process.wait(timeout=15)
    after=Path(source['path']).stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise RuntimeError('source_changed_during_reader')
    checked=child_run(runtime.stop,['ffprobe','-v','error','-show_streams','-of','json',str(partial)],capture_output=True,text=True,check=True,timeout=30)
    metadata=json.loads(checked.stdout)['streams']
    if len(metadata)!=1 or metadata[0].get('codec_type')!='audio':raise ValueError('AUDIO_STREAM_INVALID')
    partial.rename(target)
    Engine.publish_audio(view,source,rev,target,native_metadata=metadata[0])
    runtime.adapters.state(sid,rev,'AUDIO_BRANCH_READY',{'at':time.time(),'metadata':metadata[0]})
    emit(runtime,branch='audio',source_id=sid,event='AUDIO_PUBLISHED',bytes=written)
    return True

def producer(runtime,branch):
    completed=set();retry_after={};stage='source_'+branch
    try:
        while not runtime.stop.is_set():
            source_snapshot=list(runtime.views['video'].sources)
            pairs=[(s,r) for s,r in source_snapshot if (s['source_id'],r) not in completed and time.monotonic()>=retry_after.get((s['source_id'],r),0)]
            if branch=='audio':
                pairs=[(s,r) for s,r in pairs if s['source_id'] not in runtime.audio_wait_visual_policy or runtime.views['video'].publishers[r].policy.explicit_timestamps_ms is not None]
            if branch=='audio':
                retired=[]
                # Audio completion survives a visual peer refresh reopening the
                # source. Its cleaned transient audio must not be re-consumed.
                # Retire the completed branch/source from
                # this producer's in-memory candidate set BEFORE quota scans.
                # Progressive Resume may append restored sources after startup.
                if pairs:
                    with runtime.ledger.connection() as c:
                        terminal={tuple(r) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL','AUDIO_COMPLETE')")}
                    retired=[(source,rev) for source,rev in pairs if (source['source_id'],rev) in terminal]
                    for source,rev in retired:
                        completed.add((source['source_id'],rev))
                        audio_decision(runtime,source,rev,eligible=False,already_done=True,gate_pass=None,gate_reason='AUDIO_OR_SOURCE_TERMINAL',capacity_pass=None,capacity_reason='NOT_EVALUATED_NO_WORK',producer_called=False)
                    pairs=[(source,rev) for source,rev in pairs if (source['source_id'],rev) not in completed]
                runtime.audio_producer_snapshot=dict(at=time.time(),registered=len(source_snapshot),candidates=len(pairs),terminal_skipped_this_round=len(retired),completed_in_producer=len(completed),state='ELIGIBLE' if pairs else 'NO_WORK')
                if not pairs:
                    runtime.ledger.admission.order(stage,[])
                    runtime.stop.wait(.1)
                    continue
            if branch=='audio' and not audio_gate(runtime):
                runtime.audio_producer_snapshot.update(state='CAPACITY_WAIT',reason='AUDIO_HIGH_WATERMARK')
                for source,rev in pairs:audio_decision(runtime,source,rev,eligible=True,already_done=None,gate_pass=None,gate_reason='NOT_EVALUATED',capacity_pass=False,capacity_reason='AUDIO_HIGH_WATERMARK',capacity=getattr(runtime,'audio_gate_snapshot',{}),producer_called=False)
                runtime.stop.wait(.1);continue
            rows=[{'source_id':s['source_id'],'sequence':n} for n,(s,r) in enumerate(pairs)]
            getattr(runtime.ledger.admission,'trace_revisions',{}).update({s['source_id']:r for s,r in pairs})
            ordered=runtime.ledger.admission.order(stage,rows)
            if branch=='audio':
                chosen=ordered[0]['source_id'] if ordered else None
                for candidate,revision in pairs:
                    candidate_id=candidate['source_id']
                    if candidate_id==chosen:continue
                    decision=runtime.ledger.admission.last_decisions.get((stage,candidate_id),{})
                    audio_decision(runtime,candidate,revision,eligible=True,already_done=None,gate_pass=decision.get('rank') is not None,gate_reason=decision.get('admission_subreason') if decision.get('rank') is None else 'ORDERED_NOT_THIS_QUANTUM',capacity_pass=True,capacity_reason=None,producer_called=False)
            if not ordered:runtime.stop.wait(.1);continue
            sid=ordered[0]['source_id'];source,rev=next((s,r) for s,r in pairs if s['source_id']==sid)
            runtime.ledger.admission.claimed(stage,sid)
            try:
                if runtime.adapters.has_state(sid,rev,'SOURCE_COMPLETE') or runtime.adapters.has_state(sid,rev,'SOURCE_FAILED_FINAL'):ok=True
                elif branch=='audio':ok=audio_prepare(runtime,source,rev)
                else:
                    Engine.feed(runtime.views['video'],source,rev)
                    runtime.sources=[pair for view in runtime.views.values() for pair in view.sources]
                    current=next(r for s,r in runtime.views['video'].sources if s['source_id']==sid)
                    if runtime.search_publication:
                        from search_publication import bind_revision
                        with runtime.ledger.connection() as c:
                            c.execute('BEGIN IMMEDIATE');bind_revision(c,sid,current)
                    ok=runtime.adapters.has_state(sid,current,'EXTRACTION_DONE') or runtime.adapters.has_state(sid,current,'VISUAL_FAILED_FINAL')
                if ok:completed.add((sid,rev))
            except subprocess.TimeoutExpired as error:
                # A read-only ffprobe timeout is source-local and recoverable.
                # Do not disguise FFmpeg failures, DB faults or integrity errors.
                command=error.cmd
                if not getattr(error,'build151_source_preflight_timeout',False) or not command or Path(command[0]).name!='ffprobe':raise
                ok=False;retry_after[(sid,rev)]=time.monotonic()+30
                emit(runtime,branch=branch,event='RECOVERABLE_WAIT',source_id=sid,revision=rev,
                     reason_code='FFPROBE_TIMEOUT',command=command,timeout_seconds=error.timeout,
                     retry_after_seconds=30)
            finally:runtime.ledger.admission.release(stage,'branch_complete_or_capacity_wait')
            runtime.stop.wait(.02 if ok else .1)
    except Exception as error:
        if runtime.stop.is_set() and str(error)=='STOP_BEFORE_CHILD_SPAWN':return
        runtime.errors.append(branch+'_producer:'+str(error));emit(runtime,branch=branch,event='ERROR',reason=str(error));runtime.stop.request('PROCESSING_FATAL','BRANCH_PRODUCER_EXCEPTION',branch=branch,error=str(error),traceback=__import__('traceback').format_exc(),source_id=locals().get('sid'))

def start(runtime):
    if runtime.branch_producers_started:return
    runtime.branch_producers_started=True
    runtime.audio_wait_visual_policy=set()
    for branch in ('visual','audio'):
        thread=threading.Thread(target=producer,args=(runtime,branch),name=branch+'-producer')
        thread.start();runtime.threads.append(thread)
