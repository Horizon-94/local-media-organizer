"""Read-only source-oriented presentation. Never grants work or alters evidence."""
import collections
import datetime
import json
import time
from pathlib import Path

LABELS={'yoloe':'识别画面中的物体','openclip':'分析画面内容特征','dhash':'检查相似画面','geometry':'检查画面尺寸','quality':'检查画面清晰度与质量','audio_extract':'音频抽取','vad':'人声检测','whisper':'语音转写','qwen':'生成画面描述','ocr':'识别画面文字','final_projection':'整理搜索内容','embedding':'建立搜索向量','final_manifest':'来源收口','insightface':'人物归并'}

def _date(value):
    return datetime.datetime.fromtimestamp(float(value)).astimezone().isoformat() if value is not None else None

def project_source_rows(c,task):
    """A repair run owns a subset; presentation still covers the SAME task."""
    if task.get('candidate_repair'):
        return list(c.execute('''SELECT p.* FROM v2_product_sources p
            JOIN scheduler_runs r ON r.scheduler_run_id=p.scheduler_run_id
            JOIN v2_active_sources a ON a.source_id=p.source_id AND a.source_revision=p.source_revision
            WHERE r.task_id=? AND NOT EXISTS (
                SELECT 1 FROM v2_product_sources newer
                JOIN scheduler_runs nr ON nr.scheduler_run_id=newer.scheduler_run_id
                WHERE nr.task_id=r.task_id AND newer.source_id=p.source_id
                AND newer.source_revision=p.source_revision
                AND nr.execution_epoch>r.execution_epoch
            )''',(task['task_id'],)))
    return list(c.execute('SELECT * FROM v2_product_sources WHERE scheduler_run_id=?',(task['scheduler_run_id'],)))

def build_dashboard(c,task,run,source_rows,authority,covered,raw_skipped):
    """Reuse existing snapshot headers, avoiding another per-source DB scan."""
    tables={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    sources=[];by_kind=collections.defaultdict(list)
    for row in source_rows:
        payload=json.loads(row['source_json'] or '{}');kind=payload.get('media_kind',payload.get('media_type','unknown'))
        path=payload.get('source_path',payload.get('path',''))
        item={'source_id':row['source_id'],'filename':Path(path).name or row['source_id'],'media_kind':kind,'state':row['state'],'reason':(row['error'] if 'error' in row.keys() else '') or ''}
        sources.append(item);by_kind[kind].append(item)
    jobs=authority['jobs'];by_stage=collections.defaultdict(list)
    for j in jobs:
        if j['status']!='SUPERSEDED':by_stage[j['stage']].append(j)
    summaries=[]
    for stage in LABELS:
        js=by_stage[stage];counts=collections.Counter(j['status'] for j in js)
        block=authority['blocks'].get(stage,{})
        summaries.append({'stage':stage,'name':LABELS[stage],'done':counts['DONE'],'running':counts['RUNNING'],'pending':counts['PENDING']+counts['FAILED_RETRYABLE'],'failed':counts['FAILED_FINAL']+counts['FAILED'],'runnable':authority['stages'].get(stage,{}).get('runnable',0),'blocker':block.get('blocking_reason',''),'source_count':len({j.get('source_id') for j in js if j.get('source_id')}),'current_sources':[j.get('source_id','') for j in js if j['status']=='RUNNING']})
    eos={};frames=collections.Counter()
    if {'v2_sources','v2_active_sources'}<=tables:
        eos=dict(c.execute('SELECT s.source_id,s.eos FROM v2_sources s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision'))
    if {'v2_artifacts','v2_active_sources'}<=tables:
        frames.update(dict(c.execute("SELECT ar.source_id,COUNT(*) FROM v2_artifacts ar JOIN v2_active_sources a ON a.source_id=ar.source_id AND a.source_revision=ar.revision WHERE ar.status='READY' GROUP BY ar.source_id")))
    ready=set()
    if 'v2_selection_snapshots' in tables:
        ready={r[0] for r in c.execute("SELECT DISTINCT s.source_id FROM v2_selection_snapshots s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.source_revision WHERE s.status='DONE'")}
    lanes=[]
    visual_closed=None
    if 'v2_visual_source_closure' in tables:
        visual_closed={r[0] for r in c.execute("SELECT v.source_id FROM v2_visual_source_closure v JOIN v2_active_sources a ON a.source_id=v.source_id AND a.source_revision=v.source_revision WHERE v.status='COMPLETE'")}
    audio_branches={}
    documents=collections.defaultdict(list)
    if {'v2_audio_branches','v2_active_sources'}<=tables:
        audio_branches={r['source_id']:dict(r) for r in c.execute('SELECT b.* FROM v2_audio_branches b JOIN v2_active_sources a ON a.source_id=b.source_id AND a.source_revision=b.source_revision')}
    if {'v2_documents','v2_active_sources'}<=tables:
        current_keys={j.get('execution_key') for j in jobs}
        for r in c.execute('SELECT d.source_id,d.document_id,d.document_type,d.raw_result_ref,d.status FROM v2_documents d JOIN v2_active_sources a ON a.source_id=d.source_id AND a.source_revision=d.source_revision'):
            if r['raw_result_ref'] not in current_keys:continue
            documents[r['source_id']].append(dict(r))
    for kind,name in (('image','图片'),('video','视频'),('audio','音频')):
        members=by_kind['video'] if kind=='audio' else by_kind[kind]
        ids={s['source_id'] for s in members}
        required=ids-covered if kind=='image' else ids-raw_skipped if kind=='video' else ids
        audio_done={j.get('source_id') for j in by_stage['audio_extract'] if j['status']=='DONE'}
        active_stages=('audio_extract','vad','whisper') if kind=='audio' else ('yoloe','openclip','dhash','geometry','quality')
        active={j.get('source_id') for stage in active_stages for j in by_stage[stage] if j['status']=='RUNNING'}
        current=[s['filename'] for s in members if s['source_id'] in active or (kind!='audio' and s['state']=='INGESTING')]
        lane_stages=[]
        runnable_keys=set(authority.get('runnable_execution_keys',[]))
        for summary in summaries:
            if summary['stage'] not in active_stages:continue
            local=[j for j in by_stage[summary['stage']] if j.get('source_id') in ids]
            counts=collections.Counter(j['status'] for j in local)
            lane_stages.append({**summary,'done':counts['DONE'],'running':counts['RUNNING'],'pending':counts['PENDING']+counts['FAILED_RETRYABLE'],'failed':counts['FAILED_FINAL']+counts['FAILED'],'runnable':sum(j.get('execution_key') in runnable_keys for j in local),'source_count':len({j.get('source_id') for j in local}),'current_sources':[j.get('source_id') for j in local if j['status']=='RUNNING']})
        # Presentation follows each branch, not the shared final source join.
        # Missing fanout/documents must never look complete just because queues
        # are empty. Only current revisions and the original evidence count.
        audio_document_ids={d['document_id'] for sid in ids for d in documents[sid] if d['document_type']=='whisper'}
        branch_jobs=[j for js in by_stage.values() for j in js if j.get('source_id') in ids and
                     (j['stage'] in ('audio_extract','vad','whisper') or
                      (j['stage']=='embedding' and j.get('item_id') in audio_document_ids))] if kind=='audio' else [j for js in by_stage.values() for j in js if j.get('source_id') in ids and j['stage'] in ('yoloe','openclip','dhash','geometry','quality','insightface','qwen','ocr','final_projection')]
        failed_ids={j['source_id'] for j in branch_jobs if j['status'] in ('FAILED_FINAL','FAILED')}
        source_complete_ids={s['source_id'] for s in members if s['state']=='COMPLETE'}
        done_ids=set()
        source_jobs=collections.defaultdict(list)
        for job in branch_jobs:source_jobs[job['source_id']].append(job)
        for sid in ids:
            local=source_jobs[sid]
            if any(j['status']!='DONE' for j in local):continue
            ds=[d for d in documents[sid] if (d['document_type']=='whisper')==(kind=='audio')]
            if any(d['status']!='EMBEDDED' for d in ds):continue
            if sid in source_complete_ids:
                done_ids.add(sid);continue
            if kind=='audio':
                branch=audio_branches.get(sid,{})
                status=branch.get('status')
                if status in ('NO_AUDIO','NO_SPEECH'):done_ids.add(sid)
                elif status=='SEGMENTS_READY':
                    expected=len(json.loads(branch.get('segments_json') or '[]'))
                    speech=[j for j in local if j['stage']=='whisper']
                    if expected and len(speech)==expected and len(ds)==expected:done_ids.add(sid)
            elif sid in covered or sid in raw_skipped:done_ids.add(sid)
            elif visual_closed is not None:
                if sid in visual_closed:done_ids.add(sid)
            elif sid in ready:
                expected=sum(j['stage'] in ('qwen','ocr') for j in local)
                if sum(d['document_type'] in ('qwen','ocr') for d in ds)==expected and (expected==0 or any(j['stage']=='final_projection' for j in local)):done_ids.add(sid)
        lanes.append({'kind':kind,'name':name,'source_total':len(ids),'source_done':len(done_ids),'failed':len(failed_ids),'eligible':len(required),'covered':len(ids&covered) if kind=='image' else 0,'skipped':len(ids&raw_skipped) if kind=='video' else 0,'producer_done':len(ids&audio_done) if kind=='audio' else sum(eos.get(sid)=='EOS' for sid in required),'ready':len(required&ready) if kind!='audio' else 0,'frames':sum(frames[sid] for sid in required) if kind=='video' else 0,'current_item':'、'.join(current[:3]) or '当前没有正在处理的文件','waiting':len(ids-done_ids-failed_ids),'blocker':next((s['reason'] for s in members if s['reason'] and s['source_id'] in failed_ids),''),'stages':lane_stages})
    run_cols={r[1] for r in c.execute('PRAGMA table_info(scheduler_runs)')}
    recovery='recovered_at' if 'recovered_at' in run_cols else 'NULL AS recovered_at'
    runs=list(c.execute('SELECT started_at,finished_at,'+recovery+',status FROM scheduler_runs WHERE task_id=? ORDER BY execution_epoch',(task['task_id'],)))
    start=min(float(r['started_at']) for r in runs);end=run['finished_at'];total=max(0.,float(end or time.time())-start)
    recovered=len(runs)>1 or any(r['recovered_at'] is not None or r['status']=='RECOVERABLE' for r in runs)
    timer={'task_start':_date(start),'task_end':_date(end),'total_elapsed':total,'active_processing':None if recovered else total,'paused_time':None if recovered else 0.,'paused':run['status']=='RECOVERABLE','basis':'历史恢复任务未持久化完整暂停时间，不能精确拆分' if recovered else '从任务开始到结束的墙钟时间；并发工作不重复相加','benchmark_elapsed':None}
    parent=Path(task.get('workspace','/nonexistent-workspace')).parent
    try:
        frozen=json.loads((parent/'timing-start.json').read_text())
        if frozen.get('full_benchmark'):
            finished=json.loads((parent/'result.json').read_text()) if (parent/'result.json').exists() else None
            timer['benchmark_elapsed']=finished['elapsed_seconds'] if finished else max(0.,time.monotonic()-frozen['monotonic_start'])
    except (OSError,ValueError,KeyError):pass
    unfinished=sum(s['state'] not in ('COMPLETE','FAILED_FINAL','FAILED') for s in sources)
    closure_backlog=sum(s['state']=='INGESTED' for s in sources)
    running=sum(s['running'] for s in summaries);pending=sum(s['pending'] for s in summaries)
    claimable=sum(s['runnable'] for s in summaries)
    producer_active=sum(s['state']=='INGESTING' for s in sources)
    blocker=next((r['blocker'] for r in summaries if r['pending'] and r['blocker']),'')
    if run['status']=='RUNNING' and unfinished and not running and not pending and not producer_active:
        blocker=('WAITING_INDEX_CLOSURE / 等待索引和来源收尾' if closure_backlog else 'PIPELINE_STARVED / 未完成素材尚未获得新任务')
    control=f'未完成来源 {unfinished} · 等待闭环 {closure_backlog} · 素材读取 {producer_active} · 下游任务运行 {running} · 待处理 {pending} · 可领取 {claimable}'
    selected=None
    if {'v2_selected_candidates','v2_selection_snapshots','v2_active_sources'}<=tables:
        selected=c.execute('''SELECT count(*) FROM v2_selected_candidates q
            JOIN v2_selection_snapshots s ON s.snapshot_id=q.snapshot_id
            JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.source_revision
            WHERE s.status='DONE' AND q.status<>'SUPERSEDED' AND q.purpose='qwen' ''').fetchone()[0]
    high_value=f'高价值筛选：已评估来源 {len(ready)} · 已评估画面 {sum(frames[sid] for sid in ready)}'
    if selected is not None:high_value+=f' · 送入画面描述 {selected}'
    high_value+=' · 质量排除数量：历史未单独记录（不填虚假数字）'
    return {'lanes':lanes,'timer':timer,'stage_summaries':summaries,'sources':sources,'source_total':len(sources),'source_done':sum(s['state']=='COMPLETE' for s in sources),'main_blocker':blocker,'control_summary':control,'high_value_summary':high_value,'gpu_metric':'GPU_METRIC_UNAVAILABLE'}

def source_detail(c,task,source_id):
    from .v2_current_jobs_cache import install_readonly_view
    install_readonly_view(c)
    row=next((r for r in project_source_rows(c,task) if r['source_id']==source_id),None)
    if row is None:raise ValueError('source_not_in_current_task')
    payload=json.loads(row['source_json']);revision=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source_id,)).fetchone()
    revision=revision[0] if revision else None
    tables={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    covered='v2_timelapse_coverage' in tables and c.execute('''SELECT 1 FROM v2_timelapse_coverage x
        JOIN scheduler_runs r ON r.scheduler_run_id=x.scheduler_run_id WHERE r.task_id=?
        AND x.source_id=? AND x.source_revision=? AND x.status='COMPLETE' ''',(task['task_id'],source_id,revision)).fetchone() is not None
    raw='v2_raw_visual_skips' in tables and c.execute("SELECT 1 FROM v2_raw_visual_skips WHERE source_id=? AND source_revision=? AND status IN ('ACTIVE','COMPLETE')",(source_id,revision)).fetchone() is not None
    jobs=[dict(r) for r in c.execute('SELECT execution_key,stage,status,item_id FROM v2_current_jobs WHERE source_id=? AND source_revision=?',(source_id,revision))]
    eos=c.execute('SELECT eos FROM v2_sources WHERE source_id=? AND revision=?',(source_id,revision)).fetchone() if 'v2_sources' in tables else None
    rows=[{'key':'producer','name':'图片预处理 / 视频抽帧','state':'SKIPPED' if covered or raw else 'DONE' if eos and eos[0]=='EOS' else 'RUNNING' if row['state']=='INGESTING' else 'FAILED' if row['state']=='FAILED_FINAL' else 'PENDING','done':0,'total':0,'reason':'由延时摄影代表图覆盖' if covered else 'RAW 视觉跳过，音频仍处理' if raw else row['error'] or ''}]
    snapshot=c.execute("SELECT status FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? ORDER BY rowid DESC LIMIT 1",(source_id,revision)).fetchone() if 'v2_selection_snapshots' in tables else None
    visual_ready=bool(snapshot and snapshot[0]=='DONE')
    for key,name in (('source_ready','视觉证据齐全'),('v25','高价值画面筛选'),('candidate','本轮处理清单')):
        rows.append({'key':key,'name':name,'state':'SKIPPED' if covered or raw else 'DONE' if visual_ready else 'WAITING_DEPENDENCY','done':int(visual_ready),'total':0 if covered or raw else 1,'reason':'视觉按规则跳过' if covered or raw else '' if visual_ready else '等待本来源证据与正式筛选结果'})
    for stage,label in LABELS.items():
        js=[j for j in jobs if j['stage']==stage];b=collections.Counter(j['status'] for j in js)
        visual=stage in ('yoloe','openclip','dhash','geometry','quality','insightface','qwen','ocr')
        reason=''
        if visual and (covered or raw):state='SKIPPED';reason='由代表图覆盖' if covered else 'RAW 视觉按规则跳过'
        elif b['FAILED_FINAL'] or b['FAILED']:state='FAILED'
        elif b['RUNNING']:state='RUNNING'
        elif b['PENDING'] or b['FAILED_RETRYABLE']:state='PENDING';reason='任务尚未完成；领取条件以调度器判定为准'
        elif js:state='DONE'
        elif stage in ('qwen','ocr') and row['state']=='COMPLETE':state='SKIPPED';reason='按候选规则无需执行'
        elif stage=='insightface' and stage not in task.get('v2_contracts',{}):state='SKIPPED';reason='本任务未启用'
        elif row['state']=='COMPLETE':state='SKIPPED';reason='此来源无需此阶段；正式来源闭环已通过'
        else:state='WAITING_DEPENDENCY';reason='等待本来源上游结果'
        rows.append({'key':stage,'name':label,'state':state,'done':b['DONE'],'total':len(js),'reason':reason})
    candidate_lines = []
    if 'v2_selected_candidates' in tables:
        candidates = [json.loads(r[0]) for r in c.execute("SELECT payload FROM v2_selected_candidates WHERE source_id=? AND source_revision=? AND purpose='qwen' AND status!='SUPERSEDED'", (source_id, revision))]
        reasons = sorted({reason for item in candidates for reason in str(item.get('reason_codes') or '').split('|') if reason})
        tags = (task.get('candidate_repair') or {}).get('finder_tags', []) + list(task.get('finder_tags') or [])
        tagged = any(t.get('source_content_id') == source_id for t in tags) or any('tag' in r.lower() for r in reasons)
        candidate_lines = ['Finder Tag：' + ('是' if tagged else '否'), '高价值候选：' + ('是' if candidates else '否')]
        if tagged and candidates: candidate_lines.append('候选原因：Finder Tag')
        candidate_lines.extend('候选原因：' + reason for reason in reasons)
    return {'status':'PASS','source_id':source_id,'filename':Path(payload.get('source_path',payload.get('path',''))).name or source_id,'lifecycle':row['state'],'active_revision':revision,'steps':rows,'candidate_reason_lines':candidate_lines,'first_blocking_reason':next((r['reason'] or r['name']+' 尚未完成' for r in rows if r['state'] not in ('DONE','SKIPPED')),'已完成'),'central_database_write':False,'model_run':False}
