"""Read-only durable aggregate observer; no claims, retries or closure calls."""
import collections,json,os,sqlite3,time,sys
from contextlib import nullcontext,closing
from apps.media_archive_image_video_ui.video_telemetry import ClosureSpan
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
_static_cache={}

def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default
def static(path,default=None):
    path=Path(path)
    try:
        st=path.stat();key=(str(path),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    except OSError:return default
    if key not in _static_cache:
        for old in [k for k in _static_cache if k[0]==str(path)]:_static_cache.pop(old)
        _static_cache[key]=read(path,default)
    return _static_cache[key]
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(path)
def connect(db):
    c=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c
def alive(pid):
    if not pid:return False
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return None
def ffmpeg_identity(pid):
    """PID reuse cannot turn an unrelated executable into owned FFmpeg."""
    import subprocess
    result=subprocess.run([str(Path(__file__).resolve().parents[4]/'Helpers/ps-readonly'),'-p',str(int(pid)),'-o','lstart=','-o','comm='],
                          capture_output=True,text=True,timeout=2)
    fields=result.stdout.strip().split(None,5)
    if result.returncode or len(fields)!=6:return None
    started=' '.join(fields[:5]);executable=fields[5]
    if Path(executable).name.lower()!='ffmpeg':return None
    return {'pid':int(pid),'start_identity':started,'executable':executable}

def snapshot(root,connection=None,projection_cache=None):
    root=Path(root);config=read(root/'run-config.json',{});db=root/'runtime/video.sqlite'
    v={'at':time.time(),'status':config.get('status','PREPARED_NOT_STARTED'),'workspace':str(root),
       'runner_alive':alive(config.get('pid')),'started_at':config.get('started_at'),'finished_at':config.get('finished_at'),
       'files':config.get('inventory_counts',{}),'stages':{},'sources':{},'documents':{},'coverage':{},
       'model_remaining':{},'closure_pending':{},'errors':read(root/'runtime/runtime.json',{}).get('errors',[]),
       'runtime':read(root/'runtime/runtime.json',{}),
       'resources':read(root/'runtime/resources.json',{}),'lifecycle':read(root/'runtime/model-lifecycle.json',{}),
       'writer':read(root/'runtime/writer-summary.json',{}),
       'preparation_costs':read(root/'runtime/preparation-costs.json',{}),
       'producer':read(root/'metrics/producer.json',[]),
       'sqlite_bytes':db.stat().st_size if db.exists() else 0,
       'wal_bytes':Path(str(db)+'-wal').stat().st_size if Path(str(db)+'-wal').exists() else 0}
    expected={kind:config.get('inventory_counts',{}).get(kind,0) for kind in ('image','video','audio')}
    v['expected_sources']=expected
    metadata=static(root/'metadata.json',{})
    v['metadata']={'extraction_success':sum(r.get('status')=='READ' for r in metadata.values()),
                   'extraction_failed':sum(r.get('status')!='READ' for r in metadata.values()),
                   'optional_field_incomplete':sum(bool(r.get('_unknown_fields')) for r in metadata.values())}
    v['lanes']=read(root/'runtime/lane-state.json',{})
    if not db.exists():return v
    if projection_cache is not None and connection is None:
        v.update(projection_cache.get(root,config,v['runner_alive'],lambda c: database_projection(root,expected,c)))
    else:
        with (nullcontext(connection) if connection is not None else closing(connect(db))) as c:
            v.update(database_projection(root,expected,c))
    from mixed_eta import estimate
    v['eta']=estimate(v,static(root/'image-plan.json',{}),static(root/'manifest.json',{}))
    return v

def database_projection(root,expected,c):
    v={'stages':{},'sources':{},'documents':{},'coverage':{},'model_remaining':{},'closure_pending':{}}
    if not c.in_transaction:c.execute('BEGIN')
    v['stages_by_kind']={}
    for kind,stage,status,n in c.execute("WITH grouped AS MATERIALIZED (SELECT source_id,revision,stage,status,count(*) n FROM video_stream_jobs_v1 GROUP BY source_id,revision,stage,attempts,status) SELECT json_extract(i.source_json,'$.media_kind'),j.stage,j.status,sum(j.n) FROM grouped j JOIN video_stream_sources_v1 a ON a.source_id=j.source_id AND a.revision=j.revision JOIN video_stream_inputs_v1 i ON i.source_id=j.source_id AND i.revision=j.revision GROUP BY 1,2,3"):
        v['stages_by_kind'].setdefault(kind or 'video',{}).setdefault(stage,{})[status]=n
    summary_index = ' INDEXED BY video_stream_source_wave_v1' if not c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='video_stream_jobs_v1'").fetchone() and c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='video_stream_source_wave_v1'").fetchone() else ''
    for s,status,n,attempts in c.execute('SELECT j.stage,j.status,count(*),sum(j.attempts) FROM video_stream_jobs_v1 j'+summary_index+' JOIN video_stream_sources_v1 a ON a.source_id=j.source_id AND a.revision=j.revision GROUP BY j.stage,j.status'):
        v['stages'].setdefault(s,{})[status]={'count':n,'attempts':attempts}
        if status in ('PENDING','RUNNING','RETRY_PENDING'):v['model_remaining'][s]=v['model_remaining'].get(s,0)+n
    for r in c.execute('SELECT i.source_id,i.revision,i.source_json,EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=i.source_id AND s.revision=i.revision AND s.state=\'SOURCE_COMPLETE\') complete,EXISTS(SELECT 1 FROM video_stream_source_states_v1 s WHERE s.source_id=i.source_id AND s.revision=i.revision AND s.state=\'SOURCE_FAILED_FINAL\') failed FROM video_stream_inputs_v1 i JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision'):
        source=json.loads(r['source_json']);kind=source.get('media_kind','video')
        counts=v['sources'].setdefault(kind,{'total':0,'complete':0,'failed':0,'pending':0});counts['total']+=1
        counts['complete' if r['complete'] else 'failed' if r['failed'] else 'pending']+=1
        if not r['complete']:v['closure_pending'][r['source_id']]={'source_path':source.get('relative_path'),'media_kind':kind,'source_complete':False}
    doc_ready=set();doc_counts=collections.Counter()
    for sid,status,n in c.execute('SELECT d.source_id,d.status,count(*) FROM v2_documents d JOIN v2_active_sources a ON a.source_id=d.source_id AND a.source_revision=d.source_revision GROUP BY d.source_id,d.status'):
        doc_counts[status]+=n
        if status=='READY':doc_ready.add(sid)
    v['documents']=dict(doc_counts)
    active=set();live_sources=set()
    for sid,status,n in c.execute("SELECT j.source_id,j.status,count(*) FROM video_stream_jobs_v1 j JOIN video_stream_sources_v1 a ON a.source_id=j.source_id AND a.revision=j.revision WHERE j.status IN ('RUNNING','PENDING','RETRY_PENDING') GROUP BY j.source_id,j.status"):
        live_sources.add(sid)
        if status=='RUNNING':active.add(sid)
    kinds={r[0]:json.loads(r[1]).get('media_kind','video') for r in c.execute('SELECT i.source_id,i.source_json FROM video_stream_inputs_v1 i JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision')}
    for kind,n in expected.items():
        if not n:continue
        row=v['sources'].setdefault(kind,{'total':0,'complete':0,'failed':0,'pending':0})
        registered=row['total'];row['registered']=registered;row['total']=n;row['waiting']=max(0,n-registered)
        row['active']=sum(kinds.get(sid)==kind for sid in active);row['pending']=max(0,registered-row['complete']-row['failed']-row['active'])
    v['coverage']=dict(c.execute("SELECT index_kind,count(*) FROM v2_index_coverage WHERE status='READY' GROUP BY index_kind"))
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'release_folders' in tables:
        from search_publication import status as publication_status
        v['search_publication']=publication_status(c)
        v['speech_evidence']=dict(c.execute('SELECT e.tier,count(*) FROM release_transcript_evidence e JOIN release_registry r ON r.source_id=e.source_id AND r.revision=e.revision AND r.present=1 GROUP BY e.tier'))
        v['speech_evidence']['NO_SPEECH']=c.execute("SELECT count(*) FROM release_vad_facts e JOIN release_registry r ON r.source_id=e.source_id AND r.revision=e.revision AND r.present=1 WHERE e.status='NO_SPEECH'").fetchone()[0]
    v['projection_heads']=c.execute('SELECT count(*) FROM v2_final_projection_heads h JOIN v2_active_sources a ON a.source_id=h.source_id AND a.source_revision=h.source_revision').fetchone()[0] if 'v2_final_projection_heads' in tables else 0
    if 'mixed_audio_projection_heads_v1' in tables:v['projection_heads']+=c.execute("SELECT count(*) FROM mixed_audio_projection_heads_v1 h JOIN v2_active_sources a ON a.source_id=h.source_id AND a.source_revision=h.source_revision WHERE h.status='COMPLETE'").fetchone()[0]
    state_sets=collections.defaultdict(set);state_counts=collections.Counter()
    for sid,state in c.execute('SELECT s.source_id,s.state FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision'):
        state_sets[sid].add(state);state_counts[state]+=1
    v['states']=dict(state_counts);v['selection_done']=state_counts['SELECTION_DONE']
    v['video_transcript_quality']={state:state_counts[state] for state in ('VIDEO_TRANSCRIPT_COMPLETE','VIDEO_TRANSCRIPT_PARTIAL','VIDEO_TRANSCRIPT_UNAVAILABLE','VIDEO_NO_SPEECH')}
    v['video_transcript_quality']['rejected_clips']=sum(n for state,n in state_counts.items() if state.startswith('WHISPER_REJECTED_FINAL:'))
    v['selected_source_ids_by_kind']={k:[sid for sid,ss in state_sets.items() if 'SELECTION_DONE' in ss and kinds.get(sid)==k] for k in ('image','video')}
    candidate_counts=collections.Counter();candidate_sources=collections.defaultdict(list)
    for sid,n in c.execute("SELECT x.source_id,count(*) FROM v2_selected_candidates x JOIN v2_active_sources a ON a.source_id=x.source_id AND a.source_revision=x.source_revision WHERE x.purpose='qwen' AND x.status<>'SUPERSEDED' GROUP BY x.source_id"):
        candidate_counts[kinds[sid]]+=n;candidate_sources[kinds[sid]].append(sid)
    v['qwen_candidate_counts_by_kind']=dict(candidate_counts);v['qwen_candidate_sources_by_kind']=dict(candidate_sources)
    decisions={x['parent_source_content_id']:x for x in static(root/'image-plan.json',{}).get('decisions',[])}
    for sid,item in v['closure_pending'].items():
        k=item['media_kind'];ss=state_sets.get(sid,set())
        if k=='image' and sid in decisions and not decisions[sid]['should_generate_preview']:gate='representative_coverage'
        elif {'image':'IMAGE_PREVIEW_READY','video':'EXTRACTION_DONE','audio':'AUDIO_INPUT_SEALED'}[k] not in ss:gate='frontend'
        elif k in ('image','video') and 'SELECTION_DONE' not in ss:gate='selection'
        elif sid in live_sources:gate='models'
        elif sid in doc_ready:gate='embedding_projection'
        else:gate='index_projection_closure'
        item['next_gate']=gate
    v['failed_sources']=[]
    for sid,ss in state_sets.items():
        if 'SOURCE_FAILED_FINAL' not in ss:continue
        reason=c.execute("SELECT s.payload FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE s.source_id=? AND s.state='SOURCE_FAILED_FINAL' LIMIT 1",(sid,)).fetchone()
        quality=c.execute("SELECT s.payload FROM video_stream_source_states_v1 s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE s.source_id=? AND s.state='ASR_QUALITY_REVIEW' LIMIT 1",(sid,)).fetchone()
        path=c.execute("SELECT json_extract(i.source_json,'$.relative_path') FROM video_stream_inputs_v1 i JOIN v2_active_sources a ON a.source_id=i.source_id AND a.source_revision=i.revision WHERE i.source_id=?",(sid,)).fetchone()
        v['failed_sources'].append({'source_id':sid,'source_path':path[0] if path else sid,**(json.loads(reason[0]) if reason else {}),**({'asr_quality':json.loads(quality[0])} if quality else {})})
    v['failed_jobs']=[dict(r) for r in c.execute("SELECT j.source_id,j.stage,j.job_id,j.attempts,json_extract(i.source_json,'$.relative_path') source_path,a.error_type,a.error_message FROM main.video_stream_jobs_v1 j INDEXED BY video_stream_pending_v1 JOIN video_stream_sources_v1 s ON s.source_id=j.source_id AND s.revision=j.revision JOIN video_stream_inputs_v1 i ON i.source_id=j.source_id AND i.revision=j.revision LEFT JOIN video_stream_attempts_v1 a ON a.job_id=j.job_id AND a.attempt_number=j.attempts WHERE j.status='TERMINAL_FAILED' ORDER BY j.rowid LIMIT 20")]
    from apps.media_archive_image_video_ui.video_peer_catalog import dirty_heads
    for sid,rev,head in dirty_heads(c,1):v['closure_pending'].setdefault(sid,{})['peer_projection']=head
    return v


def throughput(root,now):
    path=Path(root)/'metrics/samples.jsonl'
    if not path.exists():return {}
    size=path.stat().st_size;offset=max(0,size-2*1024**2)
    with path.open('rb') as f:
        f.seek(offset)
        if offset:f.readline()
        lines=f.read(2*1024**2).splitlines()
    samples=[]
    for line in lines:
        try:samples.append(json.loads(line))
        except ValueError:continue
    if len(samples)<2:return {}
    last=samples[-1]
    def counters(v):
        return {'sources':sum(r['complete'] for r in v.get('sources',{}).values()),
                **{s:v.get('stages',{}).get(s,{}).get('DONE',{}).get('count',0) for s in ('qwen','whisper','embedding')},
                **{s:v.get('coverage',{}).get(s,0) for s in ('fts','vector','visual-vector')}}
    end=counters(last);result={}
    for seconds in (60,300,900):
        candidates=[s for s in samples if s['at']<=last['at']-seconds]
        first=candidates[-1] if candidates else samples[0];span=last['at']-first['at'];start=counters(first)
        result[str(seconds//60)+'min']={'sample_age_seconds':max(0,now-last['at']),'observed_seconds':span,
             'per_minute':{k:max(0,(end[k]-start[k])*60/span) for k in end} if span>=10 else None}
    return result
def protected(db,connection=None):
    if not Path(db).exists():return {}
    with (nullcontext(connection) if connection is not None else closing(connect(db))) as c:return {r[0]:list(r)[1:] for r in c.execute("SELECT job_id,status,attempts,result_sha FROM video_stream_jobs_v1 WHERE status='DONE'")}
def source_stat_audit(root):
    with ClosureSpan(root,'N_SOURCE_STAT','mixed_observer.source_stat_audit'):
        manifest=static(root/'manifest.json',{})
        changed=[]
        for row in manifest.get('files',[]):
            try:
                st=Path(row['path']).stat()
                if [st.st_size,st.st_mtime_ns,st.st_ino]!=[row['bytes'],row['mtime_ns'],row['inode']]:changed.append(row['relative_path'])
            except OSError:changed.append(row['relative_path'])
        return changed

def integrity_audit(root):
    # A bounded, isolated read-only audit; the full check and its process lifetime
    # are included in this span. No model/producer child or main DB write.
    import subprocess
    from apps.media_archive_image_video_ui.video_telemetry import CLOSURE_OUTPUT
    code="""import sqlite3,sys,json,time,resource
from pathlib import Path
p=Path(sys.argv[1]);c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-131072');c.execute('PRAGMA mmap_size=2147418112');c.execute('BEGIN')
t=time.monotonic();cpu=time.process_time();rows=c.execute('PRAGMA main.integrity_check').fetchall()
print(json.dumps(dict(integrity=rows[0][0],rows=len(rows),seconds=time.monotonic()-t,cpu_seconds=time.process_time()-cpu,memory_peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,sqlite_version=sqlite3.sqlite_version)))
c.close()
"""
    with ClosureSpan(root,'I_INTEGRITY','mixed_observer.integrity_audit'):
        base=CLOSURE_OUTPUT/'finalization'/('integrity-child-'+str(os.getpid()))
        with base.with_suffix('.log').open('w') as log:
            process=subprocess.Popen([sys.executable,'-S','-c',code,str(Path(root)/'runtime/video.sqlite')],stdout=subprocess.PIPE,stderr=log,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            base.with_suffix('.pid').write_text(str(process.pid));out,_=process.communicate()
        base.with_suffix('.exit').write_text(str(process.returncode))
        if process.returncode:raise RuntimeError('INTEGRITY_READONLY_AUDIT_FAILED:'+str(process.returncode))
        result=json.loads(out);save(base.with_suffix('.json'),result)
        return result['integrity']

def finalize(root,status,before,failures):
    root=Path(root);db=root/'runtime/video.sqlite'
    from concurrent.futures import ThreadPoolExecutor
    # One read-only filesystem audit overlaps the independent database checks.
    # Both remain inside post-drain finalization and must complete before publish.
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='final-readonly-audit') as audits, closing(connect(db)) as c:
        source_stats=audits.submit(source_stat_audit,root)
        integrity=audits.submit(integrity_audit,root)
        c.execute('BEGIN')
        # Private bounded page reuse; no persistent database/schema settings.
        c.execute('PRAGMA cache_size=-131072')
        c.execute('PRAGMA mmap_size=2147418112')
        with ClosureSpan(root,'G0_FIXED_NATIVE_HEADER_PROOF','mixed_observer.finalize'):
            c.execute('PRAGMA query_only=OFF')
            c.execute('CREATE TEMP TABLE final_native_headers AS SELECT job_id,source_id,revision,stage,status,attempts,result_sha FROM main.video_stream_jobs_v1')
            c.execute('CREATE TEMP VIEW video_stream_jobs_v1 AS SELECT * FROM final_native_headers')
            c.execute('PRAGMA query_only=ON')
        return _finalize_snapshot(root,status,before,failures,c,source_stats,integrity)

def _finalize_snapshot(root,status,before,failures,c,source_stats,integrity):
    with ClosureSpan(root,'G_OBSERVER_SNAPSHOT','mixed_observer.snapshot'):
        v=snapshot(root,connection=c)
    with ClosureSpan(root,'H_PROTECTED_DONE','mixed_observer.protected'):
        after=protected(root/'runtime/video.sqlite',connection=c)
    diff={k:{'before':old,'after':after.get(k)} for k,old in before.items() if after.get(k)!=old}
    checks={}
    audit_started=time.monotonic()
    with nullcontext(c) as c:
        # One private mode=ro transaction pins all lineage reads to one snapshot.
        # Only TEMP structures are written; no persistent schema/index changes.
        if not c.in_transaction:c.execute('BEGIN')
        with ClosureSpan(root,'J_FOREIGN_KEYS','mixed_observer.finalize'):
            checks['FK']=[list(r) for r in c.execute('PRAGMA foreign_key_check')]
        with ClosureSpan(root,'K_DOCUMENT_DUPLICATES','mixed_observer.finalize'):
            checks['duplicate_documents']=c.execute('SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM v2_documents GROUP BY document_type,raw_result_ref HAVING n>1)').fetchone()[0]
        with ClosureSpan(root,'L_EMBEDDING_DUPLICATES','mixed_observer.finalize'):
            from apps.media_archive_image_video_ui.v2_current_jobs_cache import install_readonly_snapshot_view
            lineage_started=time.monotonic()
            install_readonly_snapshot_view(c)
            checks['duplicate_embedding']=c.execute("SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM v2_current_jobs WHERE stage='embedding' AND status='DONE' GROUP BY item_id,input_sha,provenance HAVING n>1)").fetchone()[0]
            checks['embedding_audit_seconds']=time.monotonic()-lineage_started
        with ClosureSpan(root,'M_CANONICAL_DUPLICATES','mixed_observer.finalize'):
            checks['duplicate_canonical']=c.execute('SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM canonical_result GROUP BY result_sha HAVING n>1)').fetchone()[0]
    checks['integrity']=integrity.result()
    checks['database_audit_seconds']=time.monotonic()-audit_started
    checks['protected_done_diff']=diff;checks['protected_done_count']=len(before)
    checks['protected_done_evidence']={'status':'RECORDED','baseline_count':len(before),'resume_preservation_verified':bool(before) and not diff,
                                      'scope':'existing DONE subset' if before else 'fresh empty baseline; no Resume claim'}
    changed=source_stats.result();checks['source_stat_changes']=changed
    with ClosureSpan(root,'O_ORPHAN_WORKERS','mixed_observer.finalize'):
        exits=read(root/'runtime/worker-exit-audit.json',{})
        workers=list(exits.values()) if isinstance(exits,dict) else exits
        checks['orphan_model_pids']=[w['pid'] for w in workers if w.get('state')!='CLOSED' or alive(w.get('pid'))]
    # These are strictly own PID receipts inside this new workspace, not a
    # machine-wide model/ffmpeg scan. Never kill anything from this audit.
    with ClosureSpan(root,'P_OWNED_FFMPEG','mixed_observer.finalize'):
        receipts=[*list((root/'runtime/spool').glob('*/*ffmpeg.pid')),
                  *list((root/'runtime/audio').glob('*.pid'))]
        checks['orphan_ffmpeg_pids']=[]
        child_receipts={}
        spawn_log=root/'runtime/owned-child-spawns.jsonl'
        if spawn_log.exists():
            for line in spawn_log.read_text().splitlines():
                item=json.loads(line)
                if item.get('identity'):child_receipts[item['pid']]=item['identity']
        for path in receipts:
            try:
                pid=int(path.read_text())
                if alive(pid):
                    identity=ffmpeg_identity(pid)
                    recorded=child_receipts.get(pid) or read(path.with_name(path.name+'.identity.json'),{})
                    if identity and recorded and identity==recorded:
                        checks['orphan_ffmpeg_pids'].append(pid)
                    elif identity:
                        checks.setdefault('unverified_ffmpeg_pid_receipts',[]).append(str(path))
            except (OSError,ValueError):checks.setdefault('invalid_pid_receipts',[]).append(str(path))
    with ClosureSpan(root,'F_REPORT_PREPARATION','mixed_observer.finalize'):
        terminal_failed=any('TERMINAL_FAILED' in d for d in v['stages'].values())
        checks['required_source_coverage']=all(v['sources'].get(k,{}).get('complete',0)==n and v['sources'].get(k,{}).get('failed',0)==0 for k,n in v['expected_sources'].items())
        valid=(not checks.get('unverified_ffmpeg_pid_receipts') and checks['required_source_coverage'] and not v['errors'] and not failures and not v['model_remaining'] and not v['closure_pending'] and not terminal_failed and not diff and checks['integrity']=='ok' and not checks['FK'] and not any(checks[k] for k in ('duplicate_documents','duplicate_embedding','duplicate_canonical','source_stat_changes','orphan_model_pids','orphan_ffmpeg_pids')))
        if status=='PASS_BATCH' and not valid:status='FAIL_BATCH'
        config=read(root/'run-config.json',{});config.update(status=status,finished_at=time.time());save(root/'run-config.json',config)
        v.update(status=status,finished_at=config['finished_at'],runner_alive_at_report_generation=v.pop('runner_alive'),runner_state='FINALIZED',failures=failures+v.get('failed_jobs',[])+v.get('failed_sources',[]),checks=checks,
                 full_mixed_started=config.get('full_mixed_started'),source_files_written=None,production_writes=None)
        v['protection_evidence']={'production_writes':{'status':'NOT_RECORDED','declared_policy':'READ_ONLY_NO_PRODUCTION_ACCESS'},
            'source_writes':{'status':'NOT_RECORDED'},'source_stats':{'status':'RECORDED','changed':changed,'content_hash_diff':'NOT_RECORDED'},
            'full_run_activation':{'status':'RECORDED' if 'full_mixed_started' in config else 'NOT_RECORDED','processing_started_at':config.get('processing_started_at')}}
        v['worker_exit_audit']=exits
        v['observability']={'resources':{'scope':'LAST/HISTORICAL','sample_age_seconds':max(0,time.time()-v.get('resources',{}).get('at',time.time()))},
                            'lifecycle':{'scope':'FINAL EXIT STATE' if v.get('lifecycle',{}).get('final_exit_state') else 'LAST/HISTORICAL'}}
        v['last_live_eta']=v.get('eta')
        v['eta']={'status':'COMPLETE','remaining':0,'range_seconds':[0,0],'confidence':'HIGH'} if status=='PASS_BATCH' else {'status':'STOPPED_INCOMPLETE','range_seconds':None,'confidence':'LOW'}
    with ClosureSpan(root,'E_CHECKPOINT_PREPARATION','mixed_observer.finalize'):
        checkpoint=status+'\nWorkspace: '+str(root)+'\nSame-workspace Resume preserves DONE identities/attempts/SHA.\n'
    with ClosureSpan(root,'Q_REPORT_CHECKPOINT_WRITE','mixed_observer.finalize'):
        reports=root/'reports';save(reports/'FINAL_BATCH_REPORT.json',v)
        (reports/'FINAL_BATCH_REPORT.md').write_text(status+'\n\n```json\n'+json.dumps({k:v[k] for k in ('sources','stages','documents','coverage','model_remaining','closure_pending','failures','checks')},ensure_ascii=False,indent=2)+'\n```\n')
        (reports/'FINAL_BATCH_CHECKPOINT.md').write_text(checkpoint)
    return status
