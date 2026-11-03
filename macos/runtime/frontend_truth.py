"""P3 read-only projection. No processing imports, writes, claims or completion decisions."""
import json,time
from pathlib import Path

def read(path, default):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default


def audio_worker_state(registry,audio,resources,state,running,processes,now):
    owner=state.get('worker_pid')
    candidates=[p for p in processes if owner is not None and p.get('ppid')==owner and p.get('type')=='FFMPEG' and '-c:a copy' in p.get('command','') and 'matroska' in p.get('command','')]
    paths=[json.loads(r['row_json']).get('path') for r in registry.values()] if candidates else []
    live=[p for p in candidates if any(path and ('-i '+path+' -map 0:a:0 ') in p.get('command','') for path in paths)]
    supply=resources.get('audio_supply',{});producer=supply.get('producer',{});decision=supply.get('decision',{})
    resource_fresh=resources.get('at',0)>=state.get('started_at_epoch',float('inf')) and 0<=now-resources.get('at',0)<=5
    producer_fresh=resource_fresh and 0<=now-producer.get('at',0)<=5
    decision_fresh=resource_fresh and 0<=now-decision.get('at',0)<=5
    counts=audio.get('audio_extract',{});failed=counts.get('failed_final',0)
    remaining=sum(counts.get(k,0) for k in ('waiting','running','retrying'))
    active=bool(running and live);fresh=bool(not running or active or producer_fresh or decision_fresh)
    lane='ACTIVE' if active else 'UNKNOWN' if not fresh else 'WAITING' if running else 'STOPPED'
    if active:reason='正在抽取音轨；已核对本任务 FFmpeg 子进程与素材路径'
    elif not running:reason='处理已停止；未将历史 RUNNING 当成实际抽取'
    elif not fresh:reason='音频供给状态过期/未知；未观察到本任务抽取进程'
    elif decision_fresh and decision.get('capacity_pass') is False:
        reason='等待音频暂存容量释放：'+str(decision.get('capacity_reason'));lane='RESOURCE_WAIT'
    elif decision_fresh and decision.get('gate_pass') is False:
        reason='等待上游/资源许可：'+str(decision.get('gate_reason'))
    elif producer_fresh and producer.get('state')=='NO_WORK':reason='当前已登记来源没有合格音频候选；等待上游登记';lane='NO_CANDIDATE'
    elif counts.get('running',0):reason='已登记产物等待处理/提交；没有实际 FFmpeg 抽取进程';lane='WAITING_COMMIT'
    else:reason='已有音频来源，等待抽取前检查或领取'
    if failed:reason+='；保留 '+str(failed)+' 条最终失败'
    return dict(stage='audio_extract',state=lane,lane_state=lane,worker_process_state='ALIVE' if live else 'NOT_RUNNING',model_residency='NOT_REQUIRED',inference_state='ACTIVE' if active else 'IDLE' if fresh else 'UNKNOWN',remaining=remaining,observed_at=now if fresh else producer.get('at'),fresh=fresh,stale=not fresh,completed_last_60s=0,wait_reason=reason,failed_final=failed,worker_pids=[p['pid'] for p in live],source='owned FFmpeg + exact source path + current producer decision')

def project(c, task, running, processes, now=None):
    now=now or time.time()
    observation_origin=now;observation_started=time.monotonic()
    registry={(r['source_id'],r['revision']):dict(r) for r in c.execute("SELECT source_id,revision,relative_path,row_json FROM release_registry WHERE present=1 AND json_extract(row_json,'$.media_kind')='video'")}
    completed={(r[0],r[1]) for r in c.execute("SELECT source_id,revision FROM video_stream_source_states_v1 WHERE state='EXTRACTION_DONE'")} & registry.keys()
    started=set(completed)
    started.update((r[0],r[1]) for r in c.execute("SELECT DISTINCT source_id,revision FROM video_stream_jobs_v1 INDEXED BY video_stream_source_wave_v1 WHERE stage='frame_basics' AND status<>'SUPERSEDED'") if (r[0],r[1]) in registry)
    current=[];preparing=[]
    bypid={r['pid']:r for r in processes}
    for row in c.execute('SELECT * FROM video_stream_read_attempts_v1'):
        key=(row['source_id'],row['revision'])
        if key not in registry:continue
        p=Path(row['path'])/'ffmpeg.pid'
        try:pid=int(p.read_text())
        except (OSError,ValueError):pid=None
        reg=registry[key];source=json.loads(reg['row_json'])
        process=bypid.get(pid,{})
        confirmed=process.get('type')=='FFMPEG' and source.get('path','__missing__') in process.get('command','')
        if confirmed:started.add(key)
        if running and row['status']=='RUNNING' and key not in completed:
            item={'source_id':key[0],'filename':Path(reg['relative_path']).name,'path':source.get('path'),'status':'ACTIVE' if confirmed else 'WAITING','worker':'FFmpeg' if confirmed else 'reader admission','offline':not Path(source.get('path','/__missing__')).is_file()}
            (current if confirmed else preparing).append(item)
    stages={}
    for r in c.execute("SELECT j.stage,j.status,COUNT(*) n FROM video_stream_jobs_v1 j JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision WHERE r.present=1 AND j.status<>'SUPERSEDED' AND json_extract(r.row_json,'$.media_kind')='video' GROUP BY j.stage,j.status"):
        stages.setdefault(r['stage'],{})[r['status']]=r['n']
    audio={}
    for stage in ('audio_extract','vad','whisper'):
        states=stages.get(stage,{})
        audio[stage]={'registered':sum(states.values()),'done':states.get('DONE',0),'waiting':states.get('PENDING',0),'running':states.get('RUNNING',0) if running else 0,'retrying':states.get('RETRY_PENDING',0),'failed_final':states.get('TERMINAL_FAILED',0),'scope':'registered jobs only; not full-source closure'}
    global_stage_counts={}
    for row in c.execute("SELECT stage,status,count(*) FROM video_stream_jobs_v1 WHERE status<>'SUPERSEDED' GROUP BY stage,status"):
        global_stage_counts.setdefault(row[0],{})[row[1]]=row[2]
    # Telemetry files may advance while the preceding SQL is running.
    # Compare each read to its actual observation time, never request-start time.
    now=observation_origin+time.monotonic()-observation_started
    life=read(Path(task['workspace'])/'runtime/model-lifecycle.json',{})
    # Each model observation has its own expiry. A fresh unrelated runtime
    # heartbeat cannot refresh an old residency/inference observation.
    state=read(task.get('state_path','/__missing__'),{})
    same_run=life.get('at',0)>=state.get('started_at_epoch',float('inf'))
    fresh=bool(same_run and 0<=now-life.get('at',0)<=15 and not life.get('historical'))
    latest_inference={};observed_pids={}
    log=Path(task['workspace'])/'runtime/model-lifecycle.jsonl'
    try:
        with log.open('rb') as f:
            offset=max(0,log.stat().st_size-512*1024);f.seek(offset)
            if offset:f.readline()
            for line in f.read(512*1024).splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('timestamp',0)<state.get('started_at_epoch',float('inf')):continue
                stage=event.get('stage')
                if event.get('pid'):observed_pids[stage]=event['pid']
                if event.get('event') in ('inference_start','inference_end'):latest_inference[stage]=event
    except OSError:pass
    resources=read(Path(task['workspace'])/'runtime/resources.json',{})
    now=observation_origin+time.monotonic()-observation_started
    resource_fresh=bool(resources.get('at',0)>=state.get('started_at_epoch',float('inf')) and 0<=now-resources.get('at',0)<=5)
    live_workers=resources.get('model_workers',[]) if resource_fresh else []
    recent={r[0]:r[1] for r in c.execute("SELECT j.stage,count(*) FROM video_stream_attempts_v1 a JOIN video_stream_jobs_v1 j ON j.job_id=a.job_id WHERE a.status IN ('DONE','SUCCESS') AND a.finished_at>=? AND a.finished_at<=? GROUP BY j.stage",(max(now-60,state.get('started_at_epoch',now)),now))}
    lane_snapshot=read(Path(task['workspace'])/'runtime/lane-state.json',{})
    governor=lane_snapshot.get('governor',{}) if resource_fresh else {}
    stop_errors=state.get('build150_snapshot',{}).get('errors',[])
    # A newer startup failure must not inherit a previous run's stop reason.
    report_path=Path(task['workspace'])/'reports/FINAL_BATCH_REPORT.json'
    try:
        if report_path.stat().st_mtime>=state.get('started_at_epoch',float('inf')):
            failures=read(report_path,{}).get('failures',[])
            reasons=[item['reason'] for item in failures if isinstance(item,dict) and item.get('stage')=='runtime' and item.get('reason')]
            if reasons:stop_errors=reasons
    except OSError:pass
    models=[]
    for stage in ('qwen','whisper','yoloe','openclip','ocr','embedding','insightface','vad'):
        entries=[r for r in life.get('lifecycle',{}).get('models',{}).values() if r.get('stage')==stage]
        worker_alive=(any(r.get('pid') in bypid for r in entries if r.get('pid')) or observed_pids.get(stage) in bypid
                      or any(p.get('ppid')==state.get('worker_pid') and state.get('worker_pid') is not None and p.get('type','').casefold()==stage for p in processes))
        active=bool(running and fresh and worker_alive and any(r.get('active',0)>0 for r in entries))
        resident='UNKNOWN' if not fresh or not entries else 'RESIDENT' if any(r.get('resident') for r in entries) else 'NOT_RESIDENT'
        if resident=='RESIDENT' and not worker_alive:resident='UNKNOWN'
        counts=global_stage_counts.get(stage,{})
        remaining=sum(counts.get(k,0) for k in ('PENDING','RUNNING','RETRY_PENDING'))
        edge=latest_inference.get(stage,{})
        # A paired start/end stream identifies the current handler interval.
        # Live PID + durable owned RUNNING are required; residency stays UNKNOWN
        # when its separate snapshot has expired.
        if edge:
            active=bool(running and edge.get('event')=='inference_start' and edge.get('pid') in bypid and counts.get('RUNNING',0)>0)
        # Independent 1-second observer survives slow source coordination.
        # Require this run, a live child PID and durable RUNNING ownership.
        live=[r for r in live_workers if r.get('stage')==stage and r.get('pid') in bypid and bypid[r['pid']].get('ppid')==state.get('worker_pid')]
        stage_fresh=fresh
        observed_at=life.get('at')
        if live:
            worker_alive=True;stage_fresh=True;observed_at=resources['at']
            active=bool(running and counts.get('RUNNING',0)>0 and any(r.get('state')=='INFERENCE' for r in live))
            # The observer reports worker activity, not independent residency proof.
            if active:resident='RESIDENT'
        loading=bool(running and any(r.get('state')=='LOADING' for r in live))
        if not stage_fresh:active=False
        if loading:resident='LOADING'
        lane='LOADING' if loading else 'ACTIVE' if active else 'WAITING' if remaining else 'NO_CANDIDATE'
        inference='LOADING' if loading else 'ACTIVE' if active else 'UNKNOWN' if running and worker_alive and not stage_fresh else 'IDLE'
        failed=stages.get(stage,{}).get('TERMINAL_FAILED',0)
        if loading:wait_reason='模型正在加载，尚未进入推理'
        elif active:wait_reason=''
        elif not running:
            cause='数据库提交超时，处理已停止' if any('WriterDeadline' in str(e) or 'writer_deadline' in str(e) for e in stop_errors) else '处理已停止'
            if any('RESUME_CONTRACT_CHANGED' in str(e) for e in stop_errors):cause='恢复身份校验未通过，处理已停止'
            wait_reason=cause+'；待继续 '+str(remaining)+' 项'
        elif stage in governor.get('paused_new_claims',[]):wait_reason='资源保护暂停新领取；tier '+str(governor.get('tier'))
        elif remaining:wait_reason='已登记 '+str(remaining)+' 项，等待调度领取'
        elif stage=='whisper' and sum(stages.get('vad',{}).get(k,0) for k in ('PENDING','RETRY_PENDING','RUNNING')):wait_reason='等待 VAD 产出语音片段'
        else:wait_reason='暂无可领取候选'
        if failed:wait_reason+=('；' if wait_reason else '')+'保留 '+str(failed)+' 条最终失败'
        models.append(dict(stage=stage,state='ACTIVE' if active else 'IDLE' if worker_alive else 'UNKNOWN' if not fresh else 'NOT_RUNNING',
            lane_state=lane,worker_process_state='ALIVE' if worker_alive else 'NOT_RUNNING',model_residency=resident,
            completed_count=counts.get('DONE',0),registered_count=sum(counts.values()),inference_state=inference,inference_observed_at=edge.get('timestamp'),remaining=remaining,observed_at=observed_at,fresh=stage_fresh,stale=not stage_fresh,completed_last_60s=recent.get(stage,0),wait_reason=wait_reason,failed_final=failed,worker_pids=[r['pid'] for r in live],
            source='lifecycle + 1-second resources + live child PID + durable jobs'))
    audio_model=audio_worker_state(registry,audio,resources,state,running,processes,now)
    audio_model['completed_last_60s']=recent.get('audio_extract',0)
    models.append(audio_model)
    retries=c.execute("SELECT COALESCE(SUM(MAX(attempts-1,0)),0) FROM video_stream_jobs_v1").fetchone()[0]
    current_counts={r[0]:r[1] for r in c.execute("SELECT j.status,count(*) FROM video_stream_jobs_v1 j INDEXED BY video_stream_pending_v1 JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision AND r.present=1 WHERE j.status IN ('PENDING','RETRY_PENDING','RUNNING') GROUP BY j.status")}
    video_live_stages=[dict(kind='video',stage=stage,status=status,count=count) for stage,states in stages.items() for status,count in states.items()]
    return {'video_live_stages':video_live_stages,'current_job_state_totals':current_counts,'historical_retry_attempts':retries,'visual_progress':{'total':len(registry),'started':len(started),'completed':len(completed),'waiting':len(registry)-len(started),'current':current,'admitted_not_started':preparing,'basis':'EXTRACTION_DONE / published frame_basics / owned live ffmpeg.pid; reader admission alone is not started'},'audio_registered':audio,'frame_progress':{'unit':'帧任务','stages':{k:v for k,v in stages.items() if k in ('frame_basics','yoloe','openclip','insightface','ocr')}},'model_states':models,'projection_observed_at':now}
