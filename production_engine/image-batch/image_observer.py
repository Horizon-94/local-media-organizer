"""Read-only aggregates and terminal acceptance; no runtime/model imports."""
import json,os,sqlite3,time,zlib,math
from pathlib import Path
_inventory_cache={}
def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default
def save(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    temporary=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));temporary.replace(p)
def alive(pid):
    try:
        if int(pid)<=1:return False
        os.kill(int(pid),0);return True
    except (ValueError,TypeError,ProcessLookupError,PermissionError):return False
def ro(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    c.execute('PRAGMA query_only=ON');return c

METADATA_FIELDS=('DateTimeOriginal','CreateDate','Make','Model','LensModel','ImageWidth','ImageHeight','Orientation','GPSLatitude','GPSLongitude','Rating','Copyright','Software','ModifyDate')
def metadata_observability(inv):
    ok=inv.get('metadata_read_ok');partial=inv.get('metadata_partial')
    return {'metadata_extraction_success':ok+partial if ok is not None and partial is not None else None,
            'metadata_optional_field_incomplete':partial,
            'metadata_extraction_failed':inv.get('metadata_failed')}

def terminal_observability(value,final=False):
    """Annotate evidence without changing runtime state or historical samples."""
    terminal=final or (value.get('status') in ('PASS_BATCH','PASS_FIXTURE','FAIL_BATCH','INTERRUPTED_RESUMABLE','PREFLIGHT_READY')
                       and not value.get('runner_alive',False))
    value.setdefault('inventory',{}).update(metadata_observability(value.get('inventory',{})))
    evidence={}
    for key in ('resources','lifecycle'):
        at=value.get(key,{}).get('at')
        age=max(0,value.get('at',at)-at) if at is not None else None
        evidence[key]={'scope':'LAST/HISTORICAL' if terminal or age is None or age>15 else 'LIVE',
                       'sample_at':at,'sample_age_seconds':age}
    evidence['worker_exit']={'scope':'FINAL' if terminal else 'UNCONFIRMED',
                            'audit':value.get('worker_exit_audit',{})}
    value['observability']=evidence
    if terminal and value.get('status') in ('PASS_BATCH','PASS_FIXTURE'):
        if value.get('eta',{}).get('status')!='COMPLETE':
            value['last_live_eta']=value.get('eta') or critical_path_eta(value)
        value['eta']={'status':'COMPLETE','seconds':0,'remaining':0,'range_seconds':[0,0],
                      'qwen_remaining_with_future':0,'confidence':'FINAL','reasons':[]}
    return value

def metadata_summary(fields):
    result={'metadata_read_ok':0,'metadata_partial':0,'metadata_failed':0,'metadata_unknown_fields':{k:0 for k in METADATA_FIELDS}}
    for row in fields.values():
        readable=row.get('status') in ('READ','READ_WITH_WARNING')
        unknown=[k for k in METADATA_FIELDS if row.get(k) is None]
        for k in unknown:result['metadata_unknown_fields'][k]+=1
        key='metadata_failed' if not readable else 'metadata_partial' if unknown or row.get('status')=='READ_WITH_WARNING' else 'metadata_read_ok'
        result[key]+=1
    result.update(metadata_observability(result))
    return result

def failure_details(c,root):
    failures=[]
    for job,source,stage,attempts,payload in c.execute("SELECT job_id,source_id,stage,attempts,payload FROM video_stream_jobs_v1 WHERE status='TERMINAL_FAILED' LIMIT 100"):
        p=json.loads(payload);row=c.execute('SELECT decision FROM image_sources_v1 WHERE source_id=?',(source,)).fetchone()
        decision=json.loads(row[0]) if row else {};candidate=c.execute('SELECT payload FROM v2_selected_candidates WHERE candidate_id=?',(p.get('candidate_id'),)).fetchone()
        selected=json.loads(candidate[0]) if candidate else {};audit=[]
        for n,start,end,typ,message in c.execute('SELECT attempt_number,started_at,finished_at,error_type,error_message FROM video_stream_attempts_v1 WHERE job_id=? ORDER BY attempt_number',(job,)):
            try:
                ref=json.loads(message).get('$canonical_result')
                body=c.execute('SELECT body FROM canonical_result WHERE result_id=? OR result_sha=?',(ref,ref)).fetchone()
                if body:message=json.loads(zlib.decompress(body[0]))
            except (ValueError,TypeError,zlib.error):pass
            audit.append({'attempt':n,'start':start,'end':end,'error_type':typ,'error':message})
        reason=audit[-1]['error'] if audit else 'NO_ATTEMPT_AUDIT'
        if isinstance(reason,str):
            try:reason=json.loads(reason[reason.index('{'):])['outcome']['cleanup_warnings']
            except (ValueError,KeyError,TypeError):pass
        failures.append({'FAILED_SOURCE':source,'FAILED_PATH':decision.get('source_relative_path',source),
            'FAILED_STAGE':stage,'CANDIDATE_ID':p.get('candidate_id'),'CANDIDATE_REASON':selected.get('candidate_role'),
            'ATTEMPTS':attempts,'FINAL_FAILURE_REASON':reason,'job_id':job,'attempt_evidence':audit,
            'evidence_file':str(Path(root)/'runtime/native-qwen-events.jsonl')})
    return failures

def critical_path_eta(v,history=()):
    """Plan-derived future work, not the burst of cheap covered sources.

    Parallel branches use max, followed by the final document chain. Ranges
    explicitly allow unknown adaptive selection/retry/load/arrival costs.
    """
    inv=v.get('inventory',{});stats=v.get('runtime',{}).get('service_stats',{});stages=v.get('stages',{})
    service=lambda s:stats.get(s,{}).get('EWMA_ms_per_item')
    completed=lambda s:stages.get(s,{}).get('DONE',{}).get('count',0)+stages.get(s,{}).get('TERMINAL_FAILED',{}).get('count',0)
    qknown=max(v.get('candidates',0),inv.get('finder_tag_sources',0))
    qupper=max(qknown,inv.get('finder_tag_sources',0)+inv.get('representatives',0))
    future=max(0,qupper-completed('qwen'))
    reasons=[];branches={};preview=inv.get('preview_required',0)
    for s in ('frame_basics','yoloe','openclip','insightface'):
        remaining=max(0,preview-completed(s))
        if remaining and not service(s):reasons.append('NO_SERVICE_SAMPLE:'+s)
        branches[s]=remaining*(service(s) or 0)/1000
    prior=next((r for r in history if v['at']-r['at']>=60),None)
    produced=sum(r.get('count',0) for r in stages.get('frame_basics',{}).values())
    if preview>produced:
        before=sum(r.get('count',0) for r in (prior or {}).get('stages',{}).get('frame_basics',{}).values())
        rate=(produced-before)/(v['at']-prior['at']) if prior and produced>before else 0
        if rate:branches['preview']=(preview-produced)/rate
        else:reasons.append('NO_PREVIEW_THROUGHPUT')
    boost=v.get('lifecycle',{}).get('tail_boost',{})
    # A trial is not assumed to double throughput. Only qualified measured
    # benefit is used; otherwise conservative single-lane service remains.
    concurrency=max(1,boost.get('effective_speedup') or 1) if boost.get('speedup_evaluation_qualified') and boost.get('mode')=='TAIL_BOOST' else 1
    qpending=sum(stages.get('qwen',{}).get(s,{}).get('count',0) for s in ('PENDING','RUNNING','RETRY_PENDING'))
    branches['qwen']=max(future,qpending)*(service('qwen') or 0)/1000/concurrency
    if future and not service('qwen'):reasons.append('NO_QWEN_SERVICE_SAMPLE')
    embedpending=sum(stages.get('embedding',{}).get(s,{}).get('count',0) for s in ('PENDING','RUNNING','RETRY_PENDING'))
    branches['embedding']=max(embedpending,2*future)*(service('embedding') or 0)/1000
    # Incremental index/closure measurement is separate from source count.
    indexdef=sum(v.get('closure_pending',{}).get(k,0) for k in ('fts','text_vector','visual_vector','projection'))
    if indexdef and prior:
        delta=sum(v.get('coverage',{}).values())-sum(prior.get('coverage',{}).values())
        if delta>0:branches['index_closure']=indexdef*(v['at']-prior['at'])/delta
        else:reasons.append('INDEX_CLOSURE_RATE_UNKNOWN')
    elif indexdef:reasons.append('INDEX_CLOSURE_RATE_UNKNOWN')
    tail=(service('qwen') or 0)/1000+(service('embedding') or 0)/1000
    seconds=max(branches.values(),default=0)+tail
    return {'seconds':None if any(r.startswith(('NO_SERVICE','NO_QWEN','NO_PREVIEW')) for r in reasons) else seconds,
        'range_seconds':[seconds*.9,seconds*1.5], 'confidence':'LOW' if reasons or produced<preview else 'MEDIUM',
        'reasons':reasons+['future selection/retries/load and upstream arrival uncertain'],
        'branches_seconds':branches,'known_qwen_lower':qknown,'known_qwen_upper':qupper,'qwen_remaining_with_future':max(future,qpending),
        'model_profile':v.get('qwen_profile','current-session-only'),'physical_workers':v.get('qwen_workers',1),
        'service_scope':'current workspace/current frozen profile only; profile change on Resume rejected'}
def snapshot(workspace):
    root=Path(workspace);config=read(root/'run-config.json',{});runtime=root/'runtime';db=runtime/'video.sqlite'
    value={'at':time.time(),'status':config.get('status','STARTING'),'workspace':str(root),
           'runner_alive':alive(config.get('pid')),'started_at':config.get('started_at'),
           'finished_at':config.get('finished_at'),'active_wall_seconds':config.get('active_wall_seconds'),
           'qwen_profile':config.get('qwen_profile','historical-profile-not-recorded'),'qwen_workers':config.get('qwen_workers',1),
           'inventory':read(root/'inventory-summary.json',{}),'stages':{},'states':{},'documents':{},'coverage':{},
           'resources':read(runtime/'resources.json',{}),'runtime':read(runtime/'runtime.json',{}),
           'writer':read(runtime/'writer-summary.json',{}),
           'lifecycle':read(runtime/'model-lifecycle.json',{}),
           'sqlite_bytes':db.stat().st_size if db.exists() else 0,
           'wal_bytes':Path(str(db)+'-wal').stat().st_size if Path(str(db)+'-wal').exists() else 0}
    cache_key=(str(root),(root/'manifest.json').stat().st_mtime_ns if (root/'manifest.json').exists() else 0)
    if cache_key not in _inventory_cache:
        manifest=read(root/'manifest.json',{});groups={}
        for row in manifest.get('files',[]):
            kind=row['media_kind'];ext=row['extension'];groups.setdefault(kind,{})[ext]=groups.setdefault(kind,{}).get(ext,0)+1
        _inventory_cache[cache_key]={'format_groups':groups,**metadata_summary(read(root/'metadata.json',{}))}
    value['inventory']={**value['inventory'],**_inventory_cache[cache_key]}
    value['worker_exit_audit']=read(runtime/'worker-exit-audit.json',{})
    if not db.exists():return terminal_observability(value)
    try:
        with ro(db) as c:
            c.execute('BEGIN')
            value['stages']={stage:{status:{'count':n,'attempts':attempt} for s,status,n,attempt in c.execute(
                'SELECT stage,status,count(*),sum(attempts) FROM video_stream_jobs_v1 WHERE stage=? GROUP BY status',(stage,))} for stage in ('frame_basics','yoloe','openclip','insightface','qwen','ocr','embedding')}
            value['states']=dict(c.execute('SELECT state,count(*) FROM video_stream_source_states_v1 GROUP BY state'))
            value['documents']=dict(c.execute('SELECT status,count(*) FROM v2_documents GROUP BY status'))
            value['coverage']=dict(c.execute("SELECT index_kind,count(*) FROM v2_index_coverage WHERE status='READY' GROUP BY index_kind"))
            value['projection_heads']=c.execute('SELECT count(*) FROM v2_final_projection_heads').fetchone()[0]
            value['projection_documents']=c.execute('SELECT count(*) FROM v2_final_projection_documents').fetchone()[0]
            value['covered_sources']=c.execute('SELECT count(*) FROM image_group_coverage_v1').fetchone()[0]
            value['sources']=c.execute('SELECT count(*) FROM image_sources_v1').fetchone()[0]
            value['candidates']=c.execute('SELECT count(*) FROM v2_selected_candidates').fetchone()[0]
            value['failures']=failure_details(c,root)
            value['candidate_reasons']={}
            for (body,) in c.execute("SELECT payload FROM v2_selected_candidates WHERE status<>'SUPERSEDED'"):
                row=json.loads(body);role=row.get('candidate_role') or row.get('selection_reason') or row.get('reason_code') or row.get('queue_role') or 'see selection proof'
                if isinstance(role,(list,dict)):role=json.dumps(role,ensure_ascii=False)
                value['candidate_reasons'][role]=value['candidate_reasons'].get(role,0)+1
    except sqlite3.OperationalError as error:value['read_warning']=str(error)
    value['model_remaining']={s:sum(v['count'] for status,v in states.items() if status in ('PENDING','RUNNING','RETRY_PENDING')) for s,states in value['stages'].items()}
    value['model_remaining']={k:v for k,v in value['model_remaining'].items() if v}
    total=value['inventory'].get('counts',{}).get('image',0);complete=value['states'].get('SOURCE_COMPLETE',0)
    embedded=value['documents'].get('EMBEDDED',0);preview_required=value['inventory'].get('preview_required',0)
    value['closure_pending']={key:n for key,n in {
        'sources':max(0,total-complete),'documents':value['documents'].get('READY',0),
        'fts':max(0,embedded-value['coverage'].get('fts',0)),
        'text_vector':max(0,embedded-value['coverage'].get('vector',0)),
        'visual_vector':max(0,preview_required-value['coverage'].get('visual-vector',0)),
        'projection':max(0,preview_required-value.get('projection_heads',0))}.items() if n}
    value['eta']=critical_path_eta(value)
    return terminal_observability(value)

class WriterSummary:
    """Runner-only incremental trace cursor; Monitor reads the tiny summary."""
    def __init__(self):self.offset=0;self.holds=[];self.busy=0;self.max_hold=None;self.count=0
    def update(self,runtime):
        path=Path(runtime)/'serialized-transactions.jsonl'
        if not path.exists():return
        with path.open() as stream:
            stream.seek(self.offset)
            while True:
                position=stream.tell();line=stream.readline()
                if not line:break
                if not line.endswith('\n'):
                    stream.seek(position);break
                value=json.loads(line);hold=value.get('writer_hold_ms')
                self.count+=1;self.busy+=int(bool(value.get('busy_locked')))
                if hold is not None:
                    self.holds.append(hold);self.max_hold=max(self.max_hold or 0,hold)
                    self.holds=self.holds[-1000:]
            self.offset=stream.tell()
        ordered=sorted(self.holds)
        quantile=lambda q:ordered[min(len(ordered)-1,int((len(ordered)-1)*q))] if ordered else None
        save(Path(runtime)/'writer-summary.json',{'at':time.time(),'commit_count':self.count,'P50_ms':quantile(.5),
            'P95_ms':quantile(.95),'max_hold_ms':self.max_hold,'busy_locked':self.busy,'percentile_scope':'last1000'})

def report(workspace,status,protection=None):
    root=Path(workspace);value=snapshot(root);value['status']=status
    value['runner_alive_at_report_generation']=value.pop('runner_alive',None)
    value['runner_state']='FINALIZING'
    value['production_protection']='NOT_RECORDED_NO_PRODUCTION_ACCESS'
    value['writes_to_production_by_runner']=0
    config=read(root/'run-config.json',{})
    value['run_config']=config
    value['active_wall_seconds']=config.get('active_wall_seconds')
    value['resource_summary']={}
    resources=root/'runtime/resources.jsonl'
    if resources.exists():
        summary={'swap_start':None,'swap_peak':None,'swap_end':None,'pressure_states':[],
                 'observed_worker_peak_footprints':{},'observed_worker_peak_rss':{}}
        for line in resources.open():
            sample=json.loads(line);system=sample.get('system') or {};swap=system.get('swap_used_bytes')
            if swap is not None:
                if summary['swap_start'] is None:summary['swap_start']=swap
                summary['swap_peak']=max(summary['swap_peak'] or 0,swap);summary['swap_end']=swap
            pressure=system.get('memory_pressure_level')
            if pressure not in summary['pressure_states']:summary['pressure_states'].append(pressure)
            for worker in sample.get('model_workers',[]):
                key=worker['worker_id']
                for field,target in (('peak_footprint','observed_worker_peak_footprints'),('rss_bytes','observed_worker_peak_rss')):
                    value0=worker.get(field)
                    if value0 is not None:summary[target][key]=max(summary[target].get(key,0),value0)
        summary['swap_growth']=summary['swap_end']-summary['swap_start'] if summary['swap_start'] is not None else None
        summary['note']='observed per-worker peaks, never summed into unique unified memory'
        value['resource_summary']=summary
    value['worker_exit_audit']=read(root/'runtime/worker-exit-audit.json',{'note':'no real worker registered / fixture mode'})
    tail=value.get('lifecycle',{}).get('tail_boost',{})
    value['QWEN_TAIL_BOOST']=('NOT_ENOUGH_EVIDENCE' if tail.get('reason')=='WORKER2_ERROR' and not tail.get('speedup_evaluation_qualified')
        else 'REJECTED' if tail.get('result') in ('TAIL_BOOST_NOT_BENEFICIAL','REJECTED_RESOURCE_OR_WORKER')
        else 'ACCEPTED' if status=='PASS_BATCH' and tail.get('result')=='POSITIVE_MEASURED_THROUGHPUT'
        else 'NOT_ENOUGH_EVIDENCE')
    value['validation']={};db=root/'runtime/video.sqlite'
    if db.exists():
        with ro(db) as c:
            value['validation']['integrity']=[r[0] for r in c.execute('PRAGMA integrity_check')]
            value['validation']['foreign_keys']=list(c.execute('PRAGMA foreign_key_check'))
            value['table_sizes']=[dict(name=r[0],bytes=r[1]) for r in c.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC')]
            value['validation']['duplicate_canonical']=c.execute('SELECT count(*) FROM (SELECT result_sha FROM canonical_result GROUP BY result_sha HAVING count(*)>1)').fetchone()[0]
            value['validation']['duplicate_documents']=c.execute("SELECT count(*) FROM (SELECT source_id,source_revision,document_type,raw_result_ref,count(*) n FROM v2_documents WHERE status<>'SUPERSEDED' GROUP BY source_id,source_revision,document_type,raw_result_ref HAVING n>1)").fetchone()[0]
            value['validation']['duplicate_embedding']=c.execute("SELECT count(*) FROM (SELECT item_id,input_sha,count(*) n FROM v2_jobs WHERE stage='embedding' AND status<>'SUPERSEDED' GROUP BY item_id,input_sha HAVING n>1)").fetchone()[0]
            value['validation']['registry_rows']=c.execute('SELECT count(*) FROM yolo_registry').fetchone()[0]
            value['validation']['duplicate_registry']=c.execute('SELECT count(*) FROM (SELECT registry_sha FROM yolo_registry GROUP BY registry_sha HAVING count(*)>1)').fetchone()[0]
            value['dependency_edges']=c.execute('SELECT count(*) FROM v2_job_dependencies').fetchone()[0]
            value['selection_proofs']=c.execute('SELECT count(*) FROM video_stream_selection_proofs_v1').fetchone()[0]
    if protection:value['resume_done_protection']=protection
    terminal_observability(value,final=True)
    directory=root/'reports';directory.mkdir(exist_ok=True)
    save(directory/'FINAL_IMAGE_REPORT.json',value)
    failure_text='\n\n'.join('\n'.join(f'{k} = {r.get(k)}' for k in ('FAILED_SOURCE','FAILED_PATH','FAILED_STAGE','CANDIDATE_ID','CANDIDATE_REASON','ATTEMPTS','FINAL_FAILURE_REASON','evidence_file')) for r in value.get('failures',[]))
    (directory/'FINAL_IMAGE_REPORT.md').write_text('# IMAGE batch report\n\nSTATUS = '+status+'\n\n'+failure_text+'\n\n```json\n'+json.dumps(value,ensure_ascii=False,indent=2)+'\n```\n')
    (directory/'FINAL_IMAGE_CHECKPOINT.md').write_text('# IMAGE checkpoint\n\nSTATUS = '+status+'\n\nWorkspace: '+str(root)+'\n\nResume uses the same inventory/plan/model contracts. DONE is never reset. No video/audio processing.\n')
    return value
