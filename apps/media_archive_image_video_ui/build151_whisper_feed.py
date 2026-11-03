"""Read-only Whisper feed attribution. Never changes readiness, claims or model results."""
import json,os,time,threading,math
from pathlib import Path
from collections import Counter
_REGISTRY={};_REGISTRY_LOCK=threading.RLock()
CATEGORIES=('NO_UPSTREAM_SEGMENT','AUDIO_NOT_READY','VAD_NOT_READY','REGISTERED_NOT_ACTIONABLE','DEPENDENCY_WAIT','ASR_RECOVERY','RETRY_BACKOFF','GOVERNOR_PAUSE','RESOURCE_WAIT','WORKER_AVAILABLE_NOT_CLAIMING','STOP','OTHER')
def percentile(values,p):
 if not values:return None
 a=sorted(values);x=(len(a)-1)*p;i=int(x);return a[i]+(a[min(i+1,len(a)-1)]-a[i])*(x-i)
def classify(f):
 if f.get('stop'):return 'STOP','STOP_ACCEPTED'
 if f.get('resource_reason'):return 'GOVERNOR_PAUSE',str(f['resource_reason'])
 if f.get('model_reason'):return 'RESOURCE_WAIT',str(f['model_reason'])
 blocked=f.get('blocked',{})
 if blocked.get('BACKOFF'):return 'RETRY_BACKOFF','SOURCE_OR_CANDIDATE_NOT_BEFORE'
 if f.get('actionable_ready',0)>0 and f.get('worker_available') and not f.get('running'):return 'WORKER_AVAILABLE_NOT_CLAIMING','ACTIONABLE_AND_WORKER_AVAILABLE_GOVERNOR_ALLOW'
 phase=f.get('worker_state',{}).get('phase','UNKNOWN')
 if f.get('running',0):return 'DEPENDENCY_WAIT','PREVIOUS_OWNED_JOB_'+phase
 if f.get('registered_pending',0):
  if blocked.get('DEPENDENCY'):return 'DEPENDENCY_WAIT',f.get('first_false_condition') or 'CANONICAL_DEPENDENCY_NOT_DURABLE'
  if not f.get('worker_available'):return 'RESOURCE_WAIT','WORKER_NOT_AVAILABLE_'+phase
  return 'REGISTERED_NOT_ACTIONABLE',f.get('first_false_condition') or 'REGISTERED_NATIVE_GATE_NOT_SATISFIED'
 up=f.get('upstream',{})
 if up.get('read_error'):return 'OTHER','UPSTREAM_TRUTH_UNAVAILABLE:'+up['read_error']
 if up.get('audio_pending',0) or up.get('audio_running',0) or up.get('audio_unregistered',0):return 'AUDIO_NOT_READY','AUDIO_EXTRACTION_OR_PUBLICATION_NOT_COMPLETE'
 if up.get('vad_pending',0) or up.get('vad_running',0) or up.get('branches_waiting_vad',0):return 'VAD_NOT_READY','VAD_RESULT_OR_SEGMENTS_NOT_DURABLE'
 if up.get('canonical_whisper_pending',0):return 'REGISTERED_NOT_ACTIONABLE','CANONICAL_SEGMENT_AWAITS_NATIVE_DELIVERY'
 return 'NO_UPSTREAM_SEGMENT','NO_UNCONSUMED_LEGAL_SEGMENT'
def call(ledger,method,*args,**kw):
 tracker=getattr(ledger,'whisper_feed',None)
 if tracker:
  try:return getattr(tracker,method)(*args,**kw)
  except Exception as error:tracker.errors.append(method+':'+repr(error))
def physical(event_log,phase,wall,payload,pid):
 with _REGISTRY_LOCK:tracker=_REGISTRY.get(str(Path(event_log).parent.resolve())) if event_log else None
 if tracker:
  try:tracker.physical(phase,wall,payload,pid)
  except Exception as error:tracker.errors.append('physical:'+repr(error))

class WhisperFeed:
 def __init__(self,runtime,output_root=None):
  self.runtime=runtime;self.lock=threading.RLock();self.errors=[];self.session=str(os.getpid())+'-'+str(time.time_ns());self.job={};self.stage_jobs={};self.registered={};self.actionable={};self.claims={};self.starts={};self.active_inference={};self.last_end=None;self.gap_open=False;self.last_physical_end=None;self.samples=[];self.gaps=[];self.physical_gaps=[];self.eligible_open=None;self.last_claim_mono=-1;self.eligible_waits=[];self.incidents=[];self.upstream={};self.latest={};self.max_actionable=0;self.last_publish=0
  try:self.task_id=json.loads((runtime.root.parent.parent/'task.json').read_text()).get('task_id',str(runtime.root.parent))
  except (OSError,ValueError):self.task_id=str(runtime.root.parent)
  base=Path(output_root or os.environ.get('MEDIA_ARCHIVE_WHISPER_DIAGNOSTICS_ROOT') or runtime.root/'whisper-feed')
  label=self.task_id.replace('/','_');self.output=base/label/self.session;self.output.mkdir(parents=True,exist_ok=True)
  with _REGISTRY_LOCK:_REGISTRY[str(runtime.root.resolve())]=self
  self.event('SESSION_START',workspace=str(runtime.root),owner=runtime.ledger.owner,epoch=runtime.ledger.epoch)
 def event(self,kind,**values):
  row=dict(event=kind,at=time.time(),mono=time.monotonic(),session=self.session,pid=os.getpid(),task_id=self.task_id,**values)
  with (self.output/'events.jsonl').open('a') as f:f.write(json.dumps(row,default=str,ensure_ascii=False)+'\n')
  return row
 def identity(self,job):
  j=dict(job)
  if not j.get('execution_key'):
   payload=j.get('payload')
   if payload is None:
    import sqlite3
    with sqlite3.connect(Path(self.runtime.ledger.path).as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
     row=c.execute('SELECT payload FROM video_stream_jobs_v1 WHERE job_id=?',(j['job_id'],)).fetchone();payload=json.loads(row[0]) if row else {}
   elif isinstance(payload,str):payload=json.loads(payload)
   j.update(execution_key=payload.get('_evidence_key'),segment_id=payload.get('segment_id'),start_ms=payload.get('start_ms'),end_ms=payload.get('end_ms'),vad_execution_key=payload.get('vad_execution_key'))
  return {k:j.get(k) for k in ('job_id','stage','source_id','revision','execution_key','segment_id','start_ms','end_ms','vad_execution_key')}
 def registered_job(self,stage,key,sid,revision,payload):
  if stage not in ('whisper','vad','audio_extract'):return
  with self.lock:
   job=self.identity(dict(job_id=key,stage=stage,source_id=sid,revision=revision,payload=payload));self.registered.setdefault(key,time.time());self.event('REGISTERED',**job)
 def claimed(self,job,wall,mono):
  if job['stage'] not in ('whisper','vad','audio_extract'):return
  with self.lock:
   j=self.identity(job);self.stage_jobs[j['stage']]=j;self.claims[j['job_id']]=wall
   if j['stage']=='whisper':
    self.job=j;self.last_claim_mono=mono
    if self.eligible_open is not None:self.eligible_waits.append(max(0,mono-self.eligible_open['mono']));self.eligible_open=None
   self.event('CLAIM',claimed_at=wall,claimed_mono=mono,**j)
 def phase(self,stage,phase):
  if stage not in ('whisper','vad','audio_extract'):return
  with self.lock:
   j=self.stage_jobs.get(stage)
   if not j:return
   if phase=='INFERENCE':
    wall=time.time();mono=time.monotonic();self.starts[j['job_id']]=wall;self.active_inference[stage]=(j,mono)
    if stage=='whisper' and self.last_end and self.gap_open:
     gap=self.finish_gap(self.last_end,wall,mono,j,'LOGICAL');self.gaps.append(gap);self.gap_open=False
    if stage=='whisper':self.gap_open=False
    self.event('START',started_at=wall,started_mono=mono,claim_to_start_seconds=wall-self.claims.get(j['job_id'],wall),**j)
   elif phase=='RESULTS_READY':
    wall=time.time();mono=time.monotonic();self.active_inference.pop(stage,None);self.event('END',ended_at=wall,ended_mono=mono,**j)
    if stage=='whisper':self.last_end=dict(at=wall,mono=mono,job=j);self.gap_open=True;self.samples=[]
   elif stage=='whisper':self.event('WORKER_PHASE',phase=phase,**j)
 def attempt_finished(self,stage,operation):
  if stage not in ('whisper','vad','audio_extract'):return
  with self.lock:
   active=self.active_inference.pop(stage,None)
   if not active:return
   j,_=active;wall=time.time();mono=time.monotonic();self.event('END',ended_at=wall,ended_mono=mono,outcome='FAILED_OR_INTERRUPTED_ATTEMPT',last_operation=operation,**j)
   if stage=='whisper':self.last_end=dict(at=wall,mono=mono,job=j);self.gap_open=True;self.samples=[]
 def ack(self,job):
  if job['stage'] in ('whisper','vad','audio_extract'):
   with self.lock:self.event('DURABLE_ACK',**self.identity(job))
 def upstream_committed(self,events,consumer):
  if consumer!='video-stream-documents-v1':return
  with self.lock:
   for e in events:
    p=e.get('payload',{});kind=e.get('event_type');sid=e['source_id'];rev=e['source_revision'];base=dict(source_id=sid,revision=rev,event_id=e['event_id'])
    if kind=='AUDIO_ARTIFACT_READY' and p.get('status')=='READY':self.event('AUDIO_READY',ready_at=e['committed_at'],**base)
    elif kind=='EVIDENCE_READY' and p.get('stage')=='audio_extract' and p.get('result',{}).get('status')=='READY':self.event('AUDIO_READY',ready_at=e['committed_at'],**base)
    elif kind=='EVIDENCE_READY' and p.get('stage')=='vad':
     self.event('VAD_RESULT',result_at=e['committed_at'],vad_execution_key=p['execution_key'],**base)
     metrics=[m for m in list(self.runtime.ledger.commit_queue.metrics) if m.get('source')==sid and m.get('operation')=='consume_batch:'+consumer and not m.get('rollback')]
     committed=metrics[-1]['timestamp'] if metrics else time.time()
     self.event('SEGMENTS_CREATED',created_at=committed,creation_clock='WRITER_POST_COMMIT' if metrics else 'DELIVERY_ACK_UPPER_BOUND',delivery_ack_at=time.time(),vad_execution_key=p['execution_key'],segment_count=len(p['result']['segments']),segments=p['result']['segments'],**base)
 def read_upstream(self,c):
  result={};counts={}
  for row in c.execute("SELECT j.stage,j.status,count(*) FROM video_stream_jobs_v1 j JOIN v2_active_sources a ON a.source_id=j.source_id AND a.source_revision=j.revision WHERE j.stage IN ('audio_extract','vad') GROUP BY j.stage,j.status"):counts[(row[0],row[1])]=row[2]
  for name,stage in [('audio','audio_extract'),('vad','vad')]:
   result[name+'_pending']=sum(counts.get((stage,x),0) for x in ('PENDING','RETRY_PENDING'));result[name+'_running']=counts.get((stage,'RUNNING'),0);result[name+'_done']=counts.get((stage,'DONE'),0)
  result['canonical_whisper_pending']=c.execute("SELECT count(*) FROM v2_jobs j JOIN v2_active_sources a ON a.source_id=j.source_id AND a.source_revision=j.source_revision WHERE j.stage='whisper' AND j.status IN ('PENDING','FAILED_RETRYABLE')").fetchone()[0]
  result['branches_waiting_vad']=c.execute("SELECT count(*) FROM v2_audio_branches b JOIN v2_active_sources a ON a.source_id=b.source_id AND a.source_revision=b.source_revision WHERE b.status='READY'").fetchone()[0]
  result['segment_count']=c.execute("SELECT coalesce(sum(json_array_length(b.segments_json)),0) FROM v2_audio_branches b JOIN v2_active_sources a ON a.source_id=b.source_id AND a.source_revision=b.source_revision").fetchone()[0]
  expected=sum(k in ('video','audio') for k in getattr(self.runtime,'expected_sources',{}).values());result['audio_unregistered']=max(0,expected-sum(v for (s,_),v in counts.items() if s=='audio_extract'))
  return result
 def observe(self,facts,c):
  wall=time.time();mono=time.monotonic()
  with self.lock:
   if facts.get('snapshot_started_mono',mono)<self.last_claim_mono:return
   if self.runtime.stop.is_set():facts={**facts,'stop':True,'actionable_ready':0,'ready_jobs':[]}
   if self.gap_open and self.last_end and mono>=self.last_end['mono']:
    try:self.upstream=self.read_upstream(c)
    except Exception as error:self.upstream={'read_error':repr(error)};self.errors.append('upstream:'+repr(error))
   f={k:facts.get(k) for k in ('registered_pending','actionable_ready','running','worker_available','worker_state','stop','resource_reason','model_reason','governor_state','writer_state','backoff','blocked','db_state')};f['upstream']=dict(self.upstream);f['first_false_condition']=next((j.get('blocker_reason') for j in facts.get('jobs',[]) if j.get('blocker_reason')),None)
   cat,reason=classify(f);sample=dict(at=wall,mono=mono,category=cat,reason=reason,**f);self.latest=sample;self.max_actionable=max(self.max_actionable,f.get('actionable_ready') or 0)
   for j in facts.get('ready_jobs',[]):
    key=j['job_id']
    if key not in self.actionable:self.actionable[key]=wall;self.event('ACTIONABLE_READY',job_id=key,source_id=j['source_id'],revision=j['revision'],registered_pending=f['registered_pending'],actionable_ready=f['actionable_ready'])
   eligible=cat=='WORKER_AVAILABLE_NOT_CLAIMING'
   if eligible:
    if self.eligible_open is None:self.eligible_open=dict(at=wall,mono=mono,incident=False)
    age=mono-self.eligible_open['mono']
    if age>1 and not self.eligible_open['incident']:self.eligible_open['incident']=True;self.incidents.append(dict(at=wall,gap_seconds=age,facts=sample));self.event('CLAIM_LIVENESS_REGRESSION',gap_seconds=age,facts=sample)
   elif self.eligible_open is not None:
    self.eligible_waits.append(mono-self.eligible_open['mono']);self.eligible_open=None
   if self.gap_open:
    if not self.samples or mono-self.samples[-1]['mono']>=.15:self.samples.append(sample)
   if wall-self.last_publish>=.5:self.publish();self.last_publish=wall
 def finish_gap(self,end,wall,mono,next_job,scope):
  duration=max(0,mono-end['mono']);points=[s for s in self.samples if end['mono']<=s['mono']<=mono];parts=[];cursor=end['mono'];previous=self.latest if not points else points[0]
  for point in points:
   if point['mono']>cursor:parts.append(dict(category=previous.get('category','OTHER'),reason=previous.get('reason','NO_SAMPLE_AT_END_BOUNDARY'),duration_seconds=point['mono']-cursor))
   cursor=point['mono'];previous=point
  if mono>cursor:parts.append(dict(category=previous.get('category','OTHER'),reason=previous.get('reason','NO_SAMPLE_AT_END_BOUNDARY'),duration_seconds=mono-cursor))
  if not parts:parts=[dict(category='OTHER',reason='GAP_SHORTER_THAN_OBSERVER_SAMPLING_INTERVAL',duration_seconds=duration)]
  by=Counter()
  for part in parts:by[part['category']]+=part['duration_seconds']
  row=dict(sampling_resolution_seconds=.2,scope=scope,from_job=end['job'],to_job=next_job,end_at=end['at'],start_at=wall,INTER_JOB_GAP_SECONDS=duration,category=max(by,key=by.get),attribution_seconds=dict(by),parts=parts,samples=points,unexplained_seconds=by.get('WORKER_AVAILABLE_NOT_CLAIMING',0))
  with (self.output/'gaps.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
  self.event('INTER_JOB_GAP',scope=scope,duration_seconds=duration,from_job=end['job'].get('job_id'),to_job=next_job.get('job_id'),category=row['category']);self.samples=[];return row
 def physical(self,phase,wall,payload,pid):
  with self.lock:
   if phase not in ('start','finish'):return
   j=dict(self.job);mono=time.monotonic()
   if phase=='start' and self.last_physical_end:
    old=self.last_physical_end;duration=max(0,mono-old['mono']);same=old['job'].get('job_id')==j.get('job_id')
    category='ASR_RECOVERY' if same else classify(self.latest)[0] if self.latest else 'OTHER';reason='SAME_LOGICAL_JOB_CONDITIONAL_ASR_RECOVERY' if same else self.latest.get('reason','NO_OBSERVED_BOUNDARY')
    row=dict(scope='PHYSICAL',from_job=old['job'],to_job=j,end_at=old['at'],start_at=wall,INTER_JOB_GAP_SECONDS=duration,category=category,reason=reason,same_logical_job=same);self.physical_gaps.append(row)
    with (self.output/'physical-gaps.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
   if phase=='start':self.last_physical_end=None
   self.event('PHYSICAL_'+phase.upper(),timestamp=wall,worker_pid=pid,local_repair=bool(payload.get('_asr_local_zero')),**j)
   if phase=='finish':self.last_physical_end=dict(at=wall,mono=mono,job=j)
 def publish(self):
  values=[g['INTER_JOB_GAP_SECONDS'] for g in self.gaps];recovery=[g for g in self.physical_gaps if g['same_logical_job']]
  result=dict(status='PASS' if not self.errors else 'TELEMETRY_INCOMPLETE',task_id=self.task_id,session=self.session,workspace=str(self.runtime.root),observed_at=time.time(),GAP_COUNT=len(values),WHISPER_INTER_JOB_GAP_P50=percentile(values,.5),WHISPER_INTER_JOB_GAP_P95=percentile(values,.95),WHISPER_INTER_JOB_GAP_MAX=max(values,default=None),GAP_GT_1S_COUNT=sum(x>1 for x in values),GAP_GT_2S_COUNT=sum(x>2 for x in values),GAP_GT_5S_COUNT=sum(x>5 for x in values),GAP_GT_10S_COUNT=sum(x>10 for x in values),WHISPER_UNEXPLAINED_GAP_GT_2S_COUNT=sum(g['unexplained_seconds']>2 for g in self.gaps),WHISPER_CLAIM_LIVENESS_REGRESSION=len(self.incidents),WHISPER_ACTIONABLE_MAX=self.max_actionable,ASR_RECOVERY_GAP_COUNT=len(recovery),ASR_RECOVERY_GAP_SECONDS_TOTAL=sum(g['INTER_JOB_GAP_SECONDS'] for g in recovery),latest=self.latest,errors=self.errors[-16:])
  p=self.output/'summary.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(result,default=str,ensure_ascii=False,indent=2));tmp.replace(p);return result
 def finish(self):
  with self.lock:self.event('SESSION_STOP',open_gap_censored=self.gap_open,stop=self.runtime.stop.is_set());self.publish()
