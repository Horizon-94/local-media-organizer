"""Read-only lifecycle capabilities and explicit host metric semantics; no state writes."""
import re,os
from pathlib import Path
import host_resource_monitor as host

# The existing task_state owner maps these durable run-config statuses.
BACKEND_STATES={'PASS_BATCH':'success','FAIL_BATCH':'failed','INTERRUPTED_RESUMABLE':'recoverable','RUNNING':'running'}

def lifecycle(task,state,run_config,processes):
 if not task:return {'task_state':'idle','lifecycle_state':'IDLE','controls':{'resume':False,'pause':False,'stop':False,'retry':False},'scheduler_active':False}
 pid=state.get('worker_pid')
 root=next((p for p in processes if p['pid']==pid and str(Path(task['workspace']).parent/'task.json') in p['command']),None)
 # Read the same durable state as build150_product.task_state; never persist a projection.
 raw=BACKEND_STATES.get(run_config.get('status'),state.get('status','idle'))
 if root and state.get('status')=='queued':raw='queued'
 if raw=='running' and processes and not root:raw='recoverable'
 if state.get('status') in ('paused','stopping'):raw=state['status']
 run=Path(task['workspace'])
 checkpoint=all((run/n).is_file() for n in ['run-config.json','manifest.json','contracts.json','runtime/video.sqlite'])
 resumable=checkpoint and not root and raw in ('recoverable','stopped','interrupted','failed','paused')
 key={'running':'RUNNING','queued':'STARTING','stopping':'STOPPING','paused':'PAUSED','success':'COMPLETED','completed':'COMPLETED','blocked':'BLOCKED','idle':'IDLE','failed':'FAILED_RESUMABLE' if resumable else 'FAILED','recoverable':'STOPPED_RESUMABLE' if resumable else 'STOPPED','stopped':'STOPPED_RESUMABLE' if resumable else 'STOPPED','interrupted':'STOPPED_RESUMABLE' if resumable else 'STOPPED'}.get(raw,'UNKNOWN')
 return {'task_state':raw,'lifecycle_state':key,'scheduler_pid':pid,'scheduler_active':bool(root) if processes else None,
  'controls':{'resume':resumable,'pause':False,'stop':bool(root) and raw in ('queued','running','paused'),'retry':False,'checkpoint_present':checkpoint},
  'task_state_source':'build150_product.task_state durable run-config mapping + recorded task PID ownership',
  'lifecycle_error':state.get('error') or run_config.get('error')}

def memory_semantics(sample):
 # Keep the original monitor for physical/pressure/compressed/swap sampling.
 # Add an explicitly named estimate excluding file-backed reclaimable cache.
 _,vm,_=host.run(['/usr/bin/vm_stat'])
 m=re.search(r'page size of (\d+) bytes',vm)
 page=int(m.group(1)) if m else None
 def value(label):
  match=re.search(r'^'+re.escape(label)+r':\s+(\d+)\.',vm,re.M)
  return int(match.group(1))*page if match and page else None
 fields={k:value(v) for k,v in [('anonymous_bytes','Anonymous pages'),('wired_bytes','Pages wired down'),('purgeable_bytes','Pages purgeable'),('compressed_memory_bytes','Pages occupied by compressor')]}
 vals=list(fields.values())
 estimate=max(0,fields['anonymous_bytes']-fields['purgeable_bytes'])+fields['wired_bytes']+fields['compressed_memory_bytes'] if all(v is not None for v in vals) else None
 out={**sample,**fields,'system_occupied_estimate_bytes':estimate,'memory_metric_source':'vm_stat + sysctl hw.memsize; pressure: kern.memorystatus_vm_pressure_level; swap: vm.swapusage',
  'memory_used_semantics':'系统已占用（估算）= max(0, Anonymous pages − Pages purgeable) + Pages wired down + Pages occupied by compressor; all pages × actual page size. Excludes file-backed cache; not exact Activity Monitor Memory Used, not App RSS.',
  'cpu_metric_source':'host_resource_monitor.cpu_snapshot: /usr/bin/top -l 2 -s 1 -n 0; second sample only',
  'cpu_metric_semantics':'全系统 CPU user + sys；1秒差分窗口；每5秒刷新；非rolling；所有核心归一化到 0–100%，不是 App 进程 CPU', 'memory_unit':'GiB'}
 out.pop('memory_used_bytes',None)
 return out
