"""Read-only display estimates and inventory/storage interpretation. No scheduler owner."""
import json,time,collections
from pathlib import Path

def read(path,default):
 try:return json.loads(Path(path).read_text())
 except (OSError,ValueError):return default

def video_order(workspace):
 manifest=read(Path(workspace)/'manifest.json',{})
 rows=[r for r in manifest.get('files',[]) if r.get('media_kind')=='video'];ids=[r.get('source_id') for r in rows]
 if not rows or len(set(ids))!=len(ids):return {},'序号未提供'
 return {sid:{'source_index':i+1,'source_total':len(ids),'source_order_basis':'manifest.files 原始项目顺序（仅过滤 video，不重新排序）'} for i,sid in enumerate(ids)},'manifest.json / files original video order'

def processing_video_orders(c):
 """Assign display ordinals only to completed extraction, separately per lane.

 Read-only: no manifest position fallback, schema changes or persistent counter.
 Successful timestamps are durable facts; source/revision stays the identity.
 """
 visual=list(c.execute("SELECT s.source_id,s.revision,json_extract(s.payload,'$.video_eos_at') completed_at FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision JOIN release_registry r ON r.source_id=s.source_id AND r.revision=s.revision AND r.present=1 WHERE s.state='EXTRACTION_DONE' AND json_extract(r.row_json,'$.media_kind')='video'"))
 audio=list(c.execute("SELECT j.source_id,j.revision,MIN(a.finished_at) completed_at FROM video_stream_jobs_v1 j JOIN video_stream_attempts_v1 a ON a.job_id=j.job_id AND a.attempt_number=j.attempts JOIN v2_active_sources v ON v.source_id=j.source_id AND v.source_revision=j.revision JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision AND r.present=1 WHERE json_extract(r.row_json,'$.media_kind')='video' AND j.stage='audio_extract' AND j.status='DONE' GROUP BY j.source_id,j.revision"))
 result={}
 for lane,rows in (('visual',visual),('audio',audio)):
  completed=[tuple(r) for r in rows if isinstance(r[2],(int,float)) and r[2]>0]
  completed.sort(key=lambda r:(r[2],r[0],r[1]));total=len(completed)
  result[lane]={(sid,rev):dict(source_index=i+1,source_total=total,source_processing_lane=lane,source_order_basis='actual extraction completion time, source/revision tie break',source_processing_completed_at=at) for i,(sid,rev,at) in enumerate(completed)}
 return result

def storage(c,task):
 db=Path(task['database']);paths=[db,Path(str(db)+'-wal'),Path(str(db)+'-shm')]
 files=[{'path':str(p),'bytes':p.stat().st_size if p.exists() else 0,'exists':p.exists()} for p in paths]
 names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 groups=[('FTS',['v2_text_fts','v2_text_fts_data','v2_text_fts_idx','v2_text_fts_content','v2_text_fts_docsize','v2_text_fts_config']),('Fusion',['v2_fusion']),('Text Vector',['v2_vectors(namespace=text:*)']),('Visual Vector',['v2_vectors(namespace=visual:*)']),('Projection',['v2_final_projection_heads','v2_final_projection_documents','mixed_audio_projection_heads_v1']),('Search publication',['release_search_heads','release_search_segments','release_publication_queue'])]
 rows=[]
 for label,tables in groups:
  actual=[t for t in tables if t.split('(')[0] in names]
  if actual:rows.append({'label':label,'classification':'IN_MAIN_SQLITE_DB','tables':actual,'path':str(db),'size_text':'包含于主数据库','exclusive_bytes':None})
 return {'items':rows,'unique_files':files,'main_db_bytes':files[0]['bytes'],'wal_bytes':files[1]['bytes'],'shm_bytes':files[2]['bytes'],'total_identifiable_bytes':sum(r['bytes'] for r in files),'total_scope':'SQLite 共享容器（含 processing 数据），主文件 + WAL + SHM，各文件只计一次；不是独立搜索索引净大小','exclusive_index_bytes':None,'observed_at':time.time(),'refresh_interval_seconds':60}

def sample_windows(workspace,now):
 """Read at most 2MiB of existing cumulative telemetry; never scan artifacts."""
 p=Path(workspace)/'metrics/samples.jsonl'
 if not p.exists():return {}
 with p.open('rb') as f:
  size=p.stat().st_size;offset=max(0,size-2*1024**2);f.seek(offset)
  if offset:f.readline()
  lines=f.read(2*1024**2).splitlines()
 samples=[]
 for line in lines:
  try:samples.append(json.loads(line))
  except ValueError:pass
 if len(samples)<2:return {}
 # Last recorded window remains historical after stop; report the endpoint time.
 end=samples[-1];result={}
 def counter(row,key):
  if key=='source':return sum(r.get('complete',0) for r in row.get('sources',{}).values())
  return row.get('coverage',{}).get({'fts':'fts','vector':'vector','visual':'visual-vector'}[key])
 for minutes in [1,5,15]:
  prior=[r for r in samples if r.get('at',0)<=end.get('at',0)-60*minutes]
  if not prior:continue
  start=prior[-1];span=end['at']-start['at']
  if span<=0:continue
  # A gap while stopped is not a sampled rolling window. Never stretch a
  # minute label across an old run simply because its counter is available.
  window=[start]+[r for r in samples if start['at']<r.get('at',0)<=end['at']]
  if end['at']-60*minutes-start['at']>30 or any(b['at']-a['at']>30 for a,b in zip(window,window[1:])):continue
  for key in ['source','fts','vector','visual']:
   a,b=counter(start,key),counter(end,key)
   if a is None or b is None or b<a:continue
   result.setdefault(key,{})[str(minutes)]=(b-a)*60/span
 result['_window_end']=end.get('at');return result

def estimates(out,task,c,now=None):
 now=now or time.time();workspace=Path(task['workspace']);runtime=read(workspace/'runtime/runtime.json',{});services=runtime.get('service_stats',{});lanes=read(workspace/'runtime/lane-state.json',{});workers=lanes.get('worker_count',{});state=read(task['state_path'],{})
 elapsed=max(0,now-state.get('started_at_epoch',now));rolling=out.get('throughput',{});grouped=collections.defaultdict(collections.Counter)
 for r in out.get('stages',[]):grouped[r['stage']][r['status']]+=r['count']
 attempts={r[0]:{'1':r[1],'5':r[2],'15':r[3]} for r in c.execute("""SELECT j.stage,
  SUM(a.finished_at>?),SUM(a.finished_at>?),COUNT(*) FROM video_stream_attempts_v1 a
  JOIN video_stream_jobs_v1 j ON a.job_id=j.job_id WHERE a.status='SUCCESS' AND a.finished_at>? GROUP BY j.stage""",(now-60,now-300,max(now-900,state.get('started_at_epoch',now))))}
 backlog=[]
 for stage,counts in grouped.items():
  remain=sum(counts[k] for k in ['PENDING','RUNNING','RETRY_PENDING'])
  if remain<=0:continue
  rate=None;basis=None;confidence='LOW'
  for m,observed in [(5,300),(15,900),(1,0)]:
   n=attempts.get(stage,{}).get(str(m),0)
   if n>0 and (m==1 or (elapsed>=observed and n>=2)):
    rate=n/m;basis=f'最近 {m} 分钟滚动平均';confidence='MEDIUM' if m==5 else 'LOW';break
  if state.get('status') not in ('running','queued'):rate=None;basis=None
  seconds=remain*60/rate if rate else None
  status='RUNNING' if counts['RUNNING'] else 'RETRY_PENDING' if counts['RETRY_PENDING'] else 'PENDING'
  backlog.append({'stage':stage,'remaining':remain,'queued':counts['PENDING'],'running':counts['RUNNING'],'retrying':counts['RETRY_PENDING'],'effective_rate':rate,'rate_basis':basis,'eta_seconds':seconds,'confidence':confidence,'state':status})
 backlog.sort(key=lambda r:(r['eta_seconds'] is not None,r['eta_seconds'] or r['remaining']),reverse=True)
 cost=read(workspace/'runtime/preparation-costs.json',{}).get('stages',{})
 # Only currently materialized serial publication work is added. Future documents are unknown.
 q=c.execute("SELECT COUNT(*) FROM release_publication_queue WHERE state IN ('PENDING','READY','RUNNING','RETRY_PENDING')").fetchone()[0]
 serial=cost.get('closure',{});median=serial.get('P50_ms_per_item') or serial.get('mean_ms_per_item');closure=q*median/1000 if median else 0
 available=[r for r in backlog if r['eta_seconds'] is not None];unknown=[r['stage'] for r in backlog if r['eta_seconds'] is None]
 primary=available[0] if available else (backlog[0] if backlog else None)
 seconds=max((r['eta_seconds'] for r in available),default=0)+closure
 eta={'status':'READY' if available else 'COMPLETE' if not backlog else 'INSUFFICIENT_DATA','seconds':seconds if available else 0 if not backlog else None,'confidence':'LOW' if unknown or any(r['confidence']=='LOW' for r in available) else 'MEDIUM','method':'max(materialized stage remaining / effective stage rate) + measured serial publication closure; parallel stages are not summed','basis':'基于历史吞吐估算' if any('历史' in (r['rate_basis'] or '') for r in available) else '基于滚动吞吐估算','unknown_stages':unknown,'serial_closure_seconds':closure,'scope_note':'已知剩余工作近似值；不承诺尚未生成的候选、文档和闭环开销','observed_at':now}
 # Project ETA must not mistake an empty materialized queue for project completion.
 registry=c.execute("SELECT COUNT(*),COALESCE(SUM(committed),0),COALESCE(SUM(terminal),0) FROM release_registry WHERE present=1").fetchone()
 total,committed,terminal=registry
 ingestion=read(workspace.parent/'ingestion_state.json',{})
 project_complete=total>0 and committed==total and terminal==total and state.get('status')=='success'
 prepared=ingestion.get('total_sources')==total and ingestion.get('enqueued_count',0)>=total and ingestion.get('verified_count',0)>=total
 eta.update(scope='PROJECT',source_total=total,source_committed=committed,source_prepared=ingestion.get('enqueued_count'))
 if project_complete:
  eta.update(status='COMPLETE',seconds=0,scope_note='全部素材处理完成，搜索结果已提交。')
 elif not prepared or not backlog or unknown or (q and not median):
  eta.update(status='INSUFFICIENT_DATA',seconds=None,scope_note='还有素材未准备或后续工作量尚不完整，暂时无法估算整个项目的剩余时间。' if not prepared else '当前任务队列不能完整代表项目剩余工作，正在估算。')
 out.update(backlogs=backlog,primary_bottleneck=primary,display_eta=eta)
 return out
