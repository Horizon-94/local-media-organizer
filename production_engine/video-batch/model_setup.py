import os,sys,json,shutil,subprocess,math,sqlite3,time
from pathlib import Path
from apps.media_archive_image_video_ui.video_telemetry import ClosureSpan
REPO=Path(__file__).resolve().parents[2]
from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy
def save(path,data):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(p)

def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default

def specs_for(workspace):
    from apps.media_archive_image_video_ui.native_bridge import selected_model_root
    from apps.media_archive_image_video_ui.runtime_contract import load_runtime_contract,task_runtime_from_contract
    from apps.media_archive_image_video_ui.v2_task_materializer import _runtime_configs
    from apps.media_archive_image_video_ui.video_stream_native import isolated_configs
    payload=load_runtime_contract(REPO/'configs/media_archive_app_runtime_contract_v1.json',model_root=selected_model_root())
    # Use installed, read-only verified interpreter launchers, NOT App pipeline
    # code or any historical acceptance state/configuration.
    helpers=REPO.parents[1]/'Helpers/PipelinePython'
    for stage in ('visual','yolo','person_reid','qwen','ocr','embedding','whisper','vad'):
        launcher=helpers/stage
        if not launcher.is_file():raise ValueError('MODEL_INTERPRETER_MISSING:'+str(launcher))
        payload['python'][stage]=str(launcher)
    current=task_runtime_from_contract(payload,ocr_workers=1,embedding_workers=1,requested_scheduler_mode='event_driven_v2')
    from apps.media_archive_image_video_ui.yoloe_keywords import load_registry,profile_from_registry,materialise_registry
    registry=Path(current['configs']['yoloe_registry'])
    current['configs']['yoloe_registry']=str(materialise_registry(workspace/'yolo-registry.json',registry,profile_from_registry(load_registry(registry))))
    return isolated_configs(_runtime_configs({'runtime':current,'workspace':str(workspace)}),workspace)

def frozen_contracts(specs):
    from apps.media_archive_image_video_ui.v2_model_workers import contract
    contracts={}
    for stage,cfg in specs.items():
        if stage=='audio_extract':continue
        args=[cfg['python_path'],'-c','import sys,json;from apps.media_archive_image_video_ui.v2_model_workers import contract;print(json.dumps(contract(sys.argv[1],json.loads(sys.argv[2]),1)))',stage,json.dumps(cfg)]
        out=subprocess.run(args,cwd=REPO,capture_output=True,text=True,check=True,timeout=300,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(REPO)})
        contracts[stage]=json.loads(out.stdout)
    for stage in ('geometry','dhash','quality','audio_extract'):contracts[stage]=contract(stage,specs.get(stage,{}),1)
    return contracts

def evidence_index_root(root):
    from apps.media_archive_image_video_ui.video_telemetry import CLOSURE_OUTPUT
    import hashlib
    return CLOSURE_OUTPUT/'finalization/evidence-index'/hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()

def evidence_catalog(catalog):
    import hashlib
    return hashlib.sha256(json.dumps(sorted(catalog.items())).encode()).hexdigest()

def evidence_matcher(catalog):
    import re
    paths=[(sid,path.lower()) for sid,path in catalog.items()]
    return paths,re.compile('|'.join(re.escape(path) for sid,path in paths)) if paths else None

def evidence_header(path,paths,pattern):
    stat=path.stat()
    with path.open(errors='replace') as f:header=f.read(65536).lower()
    return dict(version=[stat.st_size,stat.st_mtime_ns],matches=[sid for sid,p in paths if p in header] if pattern is not None and pattern.search(header) else [],
                proven=any(v in header for v in ('using videotoolbox','format videotoolbox chosen','videotoolbox acceleration','format videotoolbox_vld chosen')),
                fallback=any(v in header for v in ('hardware acceleration failed','failed setup for format','no device available','does not support device type')))

def evidence_groups(entries):
    grouped={}
    for entry in entries:
        key=json.dumps(sorted(entry['matches']))
        item=grouped.setdefault(key,dict(matches=sorted(entry['matches']),proven=False,fallback=False,count=0))
        item['proven']|=entry['proven'];item['fallback']|=entry['fallback'];item['count']+=entry.get('count',1)
    return list(grouped.values())

def prepare_evidence_index(root,catalog):
    """One-time startup/maintenance migration; NEVER called by decode/finalize."""
    root=Path(root);index=evidence_index_root(root);index.mkdir(parents=True,exist_ok=True)
    summary=read(index/'summary.json')
    identity=evidence_catalog(catalog)
    if summary and summary['catalog_identity']==identity:return index
    paths,pattern=evidence_matcher(catalog);entries={};started=time.monotonic()
    with ClosureSpan(root,'BOOTSTRAP_HISTORY_INDEX','model_setup.prepare_evidence_index'):
        for path in (root/'ffmpeg-diagnostics').glob('*.log'):
            entries[str(path)]=evidence_header(path,paths,pattern)
    # Full history is retained as an immutable lookup manifest, not traversed
    # or read again by Stop. The compact groups preserve exactly the OR facts
    # for every possible current known-source subset and ordering.
    save(index/('legacy-'+identity+'.json'),dict(workspace=str(root.resolve()),catalog_identity=identity,entries=entries))
    save(index/'summary.json',dict(format=1,workspace=str(root.resolve()),catalog_identity=identity,groups=evidence_groups(entries.values()),history_log_count=len(entries),bootstrap_seconds=time.monotonic()-started))
    # A catalog migration incorporated every extant log into the new seed.
    registry=read(index/'runs.json',{})
    for run in registry.values():run['sealed']=True
    save(index/'runs.json',registry)
    return index

def diagnostic_ffmpeg_launcher(run,rows=None):
    """Unchanged FFmpeg argv/exec; durable per-generation evidence receipt."""
    import shlex,uuid
    real=shutil.which('ffmpeg')
    if not real:raise ValueError('FFMPEG_MISSING')
    root=run/'runtime/launchers';root.mkdir(exist_ok=True)
    diagnostics=run/'runtime/ffmpeg-diagnostics';diagnostics.mkdir(exist_ok=True)
    rows=rows if rows is not None else read(run/'manifest.json',{}).get('files',[])
    catalog={row['source_id']:row['path'] for row in rows}
    index=prepare_evidence_index(run/'runtime',catalog);generation=uuid.uuid4().hex
    journal=index/(generation+'.paths.jsonl');journal.touch()
    registry=read(index/'runs.json',{});registry[generation]=dict(sealed=False,journal=str(journal),created_at=time.time(),launcher_pid=os.getpid());save(index/'runs.json',registry)
    # One shell printf records the exact log path before exec. No extra child,
    # source scan, model load, argv change, or PID-changing wrapper is involved.
    token='__EVIDENCE_PID__';path=str(diagnostics/('ffmpeg-'+generation+'-'+token+'.log'))
    receipt=json.dumps(dict(path=path,generation=generation,pid=token))
    record='"$$"'.join(shlex.quote(piece) for piece in receipt.split(token))
    report='"$$"'.join(shlex.quote(piece) for piece in ('file='+path+':level=48').split(token))
    launcher=root/'ffmpeg'
    launcher.write_text("#!/bin/sh\nprintf '%s\\n' "+record+' >> '+shlex.quote(str(journal))+"\nexport FFREPORT="+report+'\nexec '+shlex.quote(real)+' "$@"\n')
    launcher.chmod(0o700);os.environ['PATH']=str(root)+os.pathsep+os.environ['PATH']
    return generation

def decode_evidence(runtime,*,final=False):
    import threading
    lock=runtime.__dict__.setdefault('_decoder_evidence_lock',threading.RLock())
    with lock:return _decode_evidence(runtime,final=final)

def _decode_evidence(runtime,*,final=False):
    result=[]
    known={s['source_id']:s['path'] for s,revision in runtime.sources}
    catalog={**getattr(runtime,'decoder_catalog',{}),**known}
    identity=evidence_catalog(catalog);index=evidence_index_root(runtime.root)
    state=getattr(runtime,'_decoder_index_state',None)
    if state is None:
        summary=read(index/'summary.json')
        if not summary or summary['catalog_identity']!=identity:raise ValueError('DECODER_EVIDENCE_INDEX_STARTUP_MIGRATION_REQUIRED')
        paths,pattern=evidence_matcher(catalog)
        state=dict(summary=summary,paths=paths,pattern=pattern,runs={});runtime._decoder_index_state=state
    if state['summary']['catalog_identity']!=identity:raise ValueError('DECODER_EVIDENCE_CATALOG_CHANGED_REQUIRES_STARTUP_RECONSTRUCTION')
    registry=read(index/'runs.json',{});new_entries=[]
    with ClosureSpan(runtime.root,'A_INCREMENTAL_EVIDENCE','model_setup.decode_evidence.index_tail'):
        for generation,run in registry.items():
            if run['sealed']:continue
            live=state['runs'].setdefault(generation,dict(offset=0,paths=set(),entries={}))
            journal=Path(run['journal'])
            with journal.open('rb') as f:
                f.seek(live['offset']);tail=f.read()
            if tail and not tail.endswith(b'\n'):tail=tail[:tail.rfind(b'\n')+1]
            for line in tail.splitlines():
                row=json.loads(line);live['paths'].add(row['path'])
            live['offset']+=len(tail)
            # Only this run's newly receipted logs (or an unsealed Resume
            # generation) are stat/read candidates. No directory enumeration.
            for name in live['paths']:
                path=Path(name)
                try:stat=path.stat()
                except FileNotFoundError:continue
                previous=live['entries'].get(name)
                if previous and previous['version']==[stat.st_size,stat.st_mtime_ns]:continue
                live['entries'][name]=evidence_header(path,state['paths'],state['pattern'])
            new_entries.extend(live['entries'].values())
        groups=evidence_groups(state['summary']['groups']+new_entries)
        if final:
            for generation,live in state['runs'].items():
                if registry[generation]['sealed']:continue
                save(index/(generation+'.sealed.json'),dict(generation=generation,entries=live['entries'],journal_offset=live['offset'],source_revisions={sid:rev for source,rev in runtime.sources for sid in [source['source_id']]}))
                registry[generation]['sealed']=True
            summary={**state['summary'],'groups':groups,'sealed_at':time.time()}
            save(index/'summary.json',summary);save(index/'runs.json',registry);state['summary']=summary;state['runs']={}
    order={sid:n for n,sid in enumerate(known)};by_source={}
    for entry in groups:
        matches=[sid for sid in entry['matches'] if sid in order]
        sid=min(matches,key=order.get) if matches else None
        if sid is not None:by_source.setdefault(sid,[]).append(entry)
    with ClosureSpan(runtime.root,'D_DECODE_AGGREGATION','decode_evidence.attempt_aggregation'):
        with runtime.ledger.connection() as c:
            for r in c.execute('SELECT attempt_id,source_id,path,result,status FROM video_stream_read_attempts_v1'):
                data=json.loads(r['result']) if r['result'] else {};log=Path(r['path'])/'ffmpeg.log';text=log.read_text(errors='replace') if log.exists() else ''
                requested='videotoolbox' in data.get('command',[])
                fallback=any(s in text.lower() for s in ('hardware acceleration failed','failed setup for format','no device available','does not support device type'))
                proven=any(s in text.lower() for s in ('using videotoolbox','format videotoolbox chosen','videotoolbox acceleration','format videotoolbox_vld chosen'))
                diagnostic=by_source.get(r['source_id'],[])
                proven=proven or any(v['proven'] for v in diagnostic);fallback=fallback or any(v['fallback'] for v in diagnostic)
                if not data and runtime.producer_variant in ('B','C') and sys.platform=='darwin':requested=True
                result.append({'source_id':r['source_id'],'status':r['status'],'requested':'VideoToolbox' if requested else 'UNKNOWN','hardware_active':True if proven else False if fallback else 'UNVERIFIED','fallback':fallback,'fallback_reason':text[-2000:] if fallback else 'No affirmative decoder log evidence' if not proven else None,'frames':data.get('frames'),'audio_ready_at':data.get('audio_ready_at'),'video_eos_at':data.get('video_eos_at')})
    return result
