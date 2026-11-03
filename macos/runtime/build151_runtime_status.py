"""Read-only status projection. No processing imports, model access or DB writes."""
import json,time,sys,subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import phasec_view_api as base
import host_resource_monitor as host
from control_resource_projection import lifecycle,memory_semantics
from runtime_semantics import processing_video_orders,storage,sample_windows,estimates
from ingestion_projection import project as project_ingestion

def stage_rows(c):
 return [dict(r) for r in c.execute("WITH grouped AS MATERIALIZED (\n SELECT source_id,revision,stage,status,COUNT(*) AS n\n FROM video_stream_jobs_v1 WHERE status<>'SUPERSEDED'\n GROUP BY source_id,revision,stage,attempts,status)\n SELECT j.stage,j.status,json_extract(r.row_json,'$.media_kind') kind,SUM(j.n) count\n FROM grouped j JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision\n WHERE r.present=1 GROUP BY kind,j.stage,j.status")]

def project_index_activity(lane,state,now,running):
 index=lane.get('index') or {};at=lane.get('at');started=state.get('started_at_epoch')
 fresh=bool(running and isinstance(at,(int,float)) and isinstance(started,(int,float)) and at>=started and 0<=now-at<=15)
 phase=index.get('state','UNKNOWN')
 labels={'DISCOVERING':('INDEXING','正在领取索引批次','按当前待办选择来源'),
         'PREPARING':('INDEXING','正在构建索引','准备索引与覆盖校验；尚未完成本批提交'),
         'COMMITTING':('INDEXING','正在提交索引','等待写者确认原子提交'),
         'READY':('INDEXING','索引批次已就绪','继续领取下一批索引任务'),
         'IDLE':('WAITING','等待索引任务','本轮未领取索引批次'),
         'RESOURCE_PAUSED':('WAITING','索引等待资源','现有内存保护暂停领取'),
         'DEADLINE_BACKOFF':('WAITING','索引等待重试','写者 deadline 后按现有策略退避'),
         'STALE_RETRY':('WAITING','索引重新校验','输入发生变化，等待重新准备')}
 activity,label,detail=labels.get(phase,('UNKNOWN','索引状态未知','尚无可信索引阶段'))
 if not running:activity,label,detail='STOPPED','索引已停止','保留已有提交与进度'
 elif not fresh:activity,label,detail='UNKNOWN','索引状态过期/未知','最近索引状态超过15秒或不属于本次运行'
 return dict(activity=activity,label=label,detail=detail,phase=phase,observed_at=at,fresh=fresh,active_source=index.get('active_source'),revision=index.get('revision'),batch_size=index.get('batch_size'))

def fast():
 path,task=base.bind_task();now=time.time()
 out=dict(status='PASS',observed_at=now,activeTasks=[],system={})
 with ThreadPoolExecutor(max_workers=2) as pool:
  cpu=pool.submit(host.cpu_snapshot);mem=pool.submit(host.vm_snapshot)
  out['system']={**cpu.result(),**mem.result()}
 _,pressure,_=host.run(['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level'])
 out['system']['pressure_level']=int(pressure.strip()) if pressure.strip().isdigit() else None
 out['system']=memory_semantics(out['system'])
 processes=host.ps_rows()
 if not task:
  out.update(lifecycle(None,{}, {},processes));return out
 state=base.read(task['state_path'],{});out['heartbeat']=Path(task['state_path']).stat().st_mtime
 run_config=base.read(Path(task['workspace'])/'run-config.json',{})
 out.update(lifecycle(task,state,run_config,processes))
 running=out['scheduler_active'] and out['task_state'] in ('running','queued','stopping')
 tree=host.process_tree(processes,out.get('scheduler_pid')) if out['scheduler_active'] else []
 app_prefix=str(Path(__file__).resolve().parents[1]/'MacOS')+'/'
 relevant=[p for p in processes if p['command'].startswith(app_prefix)]+[p for p in tree if p['type']!='UNKNOWN']
 out['system']['processes']=[{k:p[k] for k in ['pid','type','cpu_percent','rss_kib']} for p in relevant]
 for p in out['system']['processes']:
  if p['type']=='UNKNOWN':p['type']='本地数据库 App'
 from terminal_search_projection import project as terminal_project
 out['terminal_search']=terminal_project(task,state,out.get('scheduler_active'),base.state_root())
 if out['terminal_search'].get('ended') is True:
  out['controls']={**out.get('controls',{}),'resume':False,'resume_reason':'本次无合法待办；保留失败原因，可直接搜索已发布素材'}
 out['library_name']=task['name']
 out['elapsed_seconds']=max(0,(now if running else state.get('build150_snapshot',{}).get('finished_at') or out['heartbeat'])-state['started_at_epoch']) if state.get('started_at_epoch') else None
 pending=project_ingestion(task,state,out.get('scheduler_active'))
 if pending:
  out['preparation']=pending
  if pending['phase_is_preparing']:out['lifecycle_state']='PREPARING'
 if not Path(task['database']).exists():return out
 with base.connect(task) as c:
  # Only live leases are active; stopped snapshot admission records are not current workers.
  rows=list(c.execute("SELECT job_id,source_id,revision,stage,attempts,payload,lease_until FROM video_stream_jobs_v1 INDEXED BY video_stream_pending_v1 WHERE status='RUNNING' AND lease_until>? LIMIT 64",(now,))) if running else []
  for r in rows:
   d=dict(r);payload=json.loads(d.pop('payload'));reg=c.execute('SELECT relative_path,row_json FROM release_registry WHERE source_id=? AND revision=? AND present=1',(d['source_id'],d['revision'])).fetchone()
   if reg:
    d['source_name']=Path(reg['relative_path']).name;d['source']=reg['relative_path'];d['kind']=json.loads(reg['row_json']).get('media_kind')
   d['retry']=max(0,d.pop('attempts')-1);d['candidate_id']=payload.get('candidate_id');d['segment_id']=payload.get('segment_id')
   ordered=list(c.execute("SELECT job_id,payload,status FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=? AND status<>'SUPERSEDED'",(d['source_id'],d['revision'],d['stage'])))
   ordered.sort(key=lambda x:(json.loads(x['payload']).get('start_ms',0),x['job_id']))
   d['position']=next((i+1 for i,x in enumerate(ordered) if x['job_id']==d['job_id']),None);d['position_total']=len(ordered)
   d['position_basis']='start_ms / job_id，当前素材内顺序'
   d['worker_count']=sum(x['stage']==d['stage'] for x in rows)
   attempt=c.execute('SELECT started_at FROM video_stream_attempts_v1 WHERE job_id=? ORDER BY attempt_number DESC LIMIT 1',(d['job_id'],)).fetchone()
   if attempt:d['started_at']=attempt[0];d['elapsed_seconds']=max(0,now-attempt[0])
   # Runtime does not define processing ordinal across videos. Do not invent one.
   out['activeTasks'].append(d)
 if out['activeTasks'] and out.get('lifecycle_state')=='PREPARING':out['lifecycle_state']='RUNNING'
 out['source_order_source']='actual completed extraction order, separate visual/audio lanes'
 if any(d.get('kind')=='video' for d in out['activeTasks']):
  with base.connect(task) as c:orders=processing_video_orders(c)
  for d in out['activeTasks']:
   if d.get('kind')!='video':continue
   lane='audio' if d['stage'] in ('audio_extract','vad','whisper') else 'visual'
   d['source_processing_lane']=lane
   d.update(orders[lane].get((d['source_id'],d['revision']),{}))
 from frontend_truth import project
 with base.connect(task) as c:out.update(project(c,task,bool(running),processes,time.time()))
 out['index_activity']=project_index_activity(base.read(Path(task['workspace'])/'runtime/lane-state.json',{}),state,time.time(),bool(running))
 if running and out.get('lifecycle_state')=='PREPARING' and out['index_activity']['fresh'] and out['index_activity']['activity']=='INDEXING':
  out['lifecycle_state']='RUNNING'
 if running and out.get('lifecycle_state')=='RUNNING' and not any(m['inference_state']=='ACTIVE' for m in out.get('model_states',[])):
  out['runtime_activity']=out['index_activity']['activity']
 else:out['runtime_activity']='ACTIVE' if running else 'STOPPED'
 return out

def slow(include_storage=True):
 out=base.status();_,task=base.bind_task()
 out['pipelines']=[];out['throughput']={};out['errors']={};out['database']=out.get('database') or {}
 if not task:return out
 state=base.read(task['state_path'],{});snap=state.get('build150_snapshot',{});out['snapshot_at']=snap.get('at');out['eta']=state.get('eta',{});out['overview']=state.get('source_progress',{})
 with base.connect(task) as c:
  out['scoped_stages']=stage_rows(c)
  out['overview']={r['kind']:{'total':r['total'],'complete':r['complete']} for r in c.execute("SELECT json_extract(row_json,'$.media_kind') kind,COUNT(*) total,SUM(terminal) complete FROM release_registry WHERE present=1 GROUP BY kind")}
  from audio_progress import project as audio_project
  out['audio_progress']=audio_project(c)
  out['global_candidates']={r['stage']:{'done':r['done'],'total':r['total']} for r in c.execute("SELECT stage,SUM(status='DONE') done,COUNT(*) total FROM video_stream_jobs_v1 WHERE status<>'SUPERSEDED' AND stage IN ('qwen','whisper') GROUP BY stage")}
  names={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for key,table in [('Documents','v2_documents'),('Projection','v2_final_projection_heads')]:
   if table in names:out['coverage'][key]=c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
  # Remaining publication queue only, not the lifetime number of enqueued sources.
  if 'release_publication_queue' in names:
   cols={x[1] for x in c.execute('PRAGMA table_info(release_publication_queue)')}
   if 'state' in cols:out['coverage']['closure pending']=c.execute("SELECT COUNT(*) FROM release_publication_queue WHERE state NOT IN ('DONE','SEARCH_COMMITTED','COMMITTED','SUPERSEDED')").fetchone()[0]
   else:out['coverage'].pop('closure pending',None)
  # One bounded completion-window query, rather than nine repeated scans.
  sampled_at=time.time()
  window_counts={r[0]:r[1:] for r in c.execute("""SELECT j.stage,
   SUM(a.finished_at>?),SUM(a.finished_at>?),COUNT(*)
   FROM video_stream_attempts_v1 a CROSS JOIN video_stream_jobs_v1 j ON j.job_id=a.job_id
   WHERE a.status='SUCCESS' AND a.finished_at>? AND j.stage IN ('qwen','whisper','embedding') GROUP BY j.stage""",
   (sampled_at-60,sampled_at-300,sampled_at-900))}
  for stage in ['qwen','whisper','embedding']:
   out['throughput'][stage]={str(minutes):n/minutes if n else None for minutes,n in zip((1,5,15),window_counts.get(stage,(0,0,0)))}
  out['stage_throughput']={}
  for r in c.execute("""SELECT json_extract(r.row_json,'$.media_kind') kind,j.stage,COUNT(*) n
   FROM video_stream_attempts_v1 a CROSS JOIN video_stream_jobs_v1 j ON j.job_id=a.job_id
   JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision
   WHERE r.present=1 AND a.status='SUCCESS' AND a.finished_at>? GROUP BY kind,j.stage""",(time.time()-60,)):
   out['stage_throughput'][r['kind']+':'+r['stage']]=r['n']
  windows=sample_windows(task['workspace'],time.time())
  for key in ['source','fts','vector','visual']:
   if key in windows:out['throughput'][key]=windows[key]
  out['throughput_history_end']=windows.get('_window_end')
  if include_storage:out['search_storage']=storage(c,task)
  estimates(out,task,c)
  counts={}
  for r in out['stages']:counts[r['status']]=counts.get(r['status'],0)+r['count']
  out['errors']={'finalFailed':counts.get('TERMINAL_FAILED',0),'retrying':counts.get('RETRY_PENDING',0),'waiting':counts.get('PENDING',0),'blocked':counts.get('BLOCKED',0)}
 out['database']['path']=task['database']
 out['database']['writer']=snap.get('writer',{});out['database']['telemetry_at']=snap.get('writer',{}).get('at')
 out['coverage']['SEARCH_COMMITTED']=out.get('search_committed')
 import shutil
 out['disk_free']=shutil.disk_usage(task['workspace']).free
 out['disk_scope']='索引 workspace 所在卷'
 return out

def read_snapshot(include_storage):
 # Status statistics must not starve lifecycle/Stop capabilities. This budget
 # applies only to read-only projection SQL, never Processing transactions.
 import sqlite3
 deadline=time.monotonic()+4.0
 original=base.connect
 def bounded(task):
  c=original(task)
  c.set_progress_handler(lambda:int(time.monotonic()>=deadline),10000)
  return c
 base.connect=bounded
 partial=False
 try:
  try:statistics=slow(include_storage=include_storage)
  except sqlite3.OperationalError:
   if time.monotonic()<deadline:raise
   statistics={};partial=True
 finally:base.connect=original
 current=fast()
 path,task=base.bind_task()
 current.update(task_path=str(path) if path else None,task_id=task.get("task_id") if task else None)
 return {**statistics,**current,"snapshot_partial":partial,
         "statistics_status":"READ_BUDGET_EXCEEDED" if partial else "PASS",
         "statistics_observed_at":statistics.get("observed_at"),
         "statistics_error":"Slow statistics unavailable; current lifecycle and controls independently verified" if partial else None}

def enable_requested_trace():
 # One opt-in sample of the actual GUI reader; never writes to SQLite.
 import os
 folder=base.state_root()/'runtime';request=folder/'ui-query-trace-once'
 if not request.is_file():return
 try:request.rename(folder/('ui-query-trace-consumed-'+str(os.getpid())))
 except FileNotFoundError:return
 output=folder/('ui-query-trace-'+str(os.getpid())+'.jsonl')
 def record(sql):
  with output.open('a') as stream:stream.write(json.dumps({'at':time.time(),'sql':sql[:1200]})+'\n')
 record('HELPER_BEGIN');original=base.connect
 def traced(task):
  c=original(task);c.set_trace_callback(record);return c
 base.connect=traced

def main():
 enable_requested_trace()
 # This helper is one snapshot process. Resolve current selection once so all
 # nested status/adaptor reads use the same library even if selection changes.
 resolve_current=base.bind_task
 try:
  path,task=resolve_current()
  base.bind_task=lambda:(path,task)
  if task and not Path(task['database']).is_file():
   result=fast();running=result.get('scheduler_active',False)
   result.update(status='PASS',task_id=task['task_id'],task_path=str(path),library_name=task['name'],task_state='initializing' if running else 'CREATE_FAILED',lifecycle_state=result.get('lifecycle_state','INITIALIZING') if running else 'FAILED',controls={'stop':bool(running),'resume':False,'pause':False,'retry':False},initialization_reason='后端正在创建数据库' if running else '初始化未完成，数据库不存在')
   print(json.dumps(result,ensure_ascii=False));return 0
  result=(read_snapshot(include_storage='--storage' in sys.argv) if '--snapshot' in sys.argv else slow() if '--slow' in sys.argv else fast())
  print(json.dumps(result,ensure_ascii=False));return 0
 except Exception as e:print(json.dumps({'status':'FAIL','error':str(e)},ensure_ascii=False));return 2
 finally:base.bind_task=resolve_current
if __name__=='__main__':raise SystemExit(main())
