"""Private stdin/stdout query execution unit owned by one native App process."""
import os,sys,json,time,signal,select,threading,contextlib
from pathlib import Path
contents=Path(__file__).resolve().parents[1]
pipeline=contents/'Resources/Pipeline'
sys.path.insert(0,str(pipeline))
os.environ.setdefault('MEDIA_ARCHIVE_APP_STATE_ROOT',str(Path.home()/'Library/Application Support/素材大整理/1.3.2-build151'))
os.environ.setdefault('MEDIA_ARCHIVE_APP_CONTENTS',str(contents))
os.environ.setdefault('MEDIA_ARCHIVE_PROJECT_ROOT',str(pipeline))
os.environ.setdefault('MEDIA_ARCHIVE_ENV_ROOT',str(contents/'Resources/RuntimeEnvs'))
os.environ['PYTHONDONTWRITEBYTECODE']='1'
import importlib.abc
class _NoLegacyProcessing(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.rsplit('.',1)[-1] in {'product_execution','v2_product_driver','scheduler_switching'}:
            raise RuntimeError('BUILD149_PROCESSING_DISABLED:'+fullname)
sys.meta_path.insert(0,_NoLegacyProcessing())
from apps.media_archive_image_video_ui import query_trace as trace
OUT=sys.stdout;ERR=sys.stderr

def send(kind,query_id,**value):
    OUT.write(json.dumps({'kind':kind,'query_id':query_id,**value},ensure_ascii=False)+'\n');OUT.flush()

class Progress:
    def __init__(self,query_id,suppress_items=False,label=''):self.query_id=query_id;self.pending='';self.suppress_items=suppress_items;self.label=label
    def write(self,text):
        self.pending+=text
        while '\n' in self.pending:
            line,self.pending=self.pending.split('\n',1)
            if line.startswith('SEARCH_PROGRESS_JSON='):
                try:
                    event=json.loads(line.split('=',1)[1])
                    if self.suppress_items:
                        for k in ('result_items','total_count','current_source_result_count','next_result_offset'):event.pop(k,None)
                        event['detail']=self.label+' · '+str(event.get('detail',''))
                    send('progress',self.query_id,event=event)
                except ValueError:ERR.write('invalid search progress\n')
            else:ERR.write(line+'\n')
        return len(text)
    def flush(self):ERR.flush()


@contextlib.contextmanager
def phase_metrics(bridge, manager, totals, repository=None):
    # One serial App-owned query; wrappers preserve arguments/results/exceptions.
    # Only cumulative durations/counts, never vectors or evidence bodies.
    from apps.media_archive_image_video_ui import v2_index, v2_semantic_search, v2_release_search, search_result_contract, search_jobs
    targets=[(manager,'readiness','readiness'),
             (manager,'_run_v2_current_search','assemble'),
             (v2_index.SQLiteExactVectors,'search','exact_vectors'),
             (v2_semantic_search,'retrieve','semantic_channels'),
             (v2_release_search,'filter_rows','publication_filter'),
             (search_jobs,'source_results','source_grouping'),
             (bridge,'_manual_person_links','manual_person_decoration'),
             (bridge,'_validate_search_result_contract','result_contract_validation'),
             (bridge,'_atomic_search_json','result_json_write')]
    if repository is not None and callable(getattr(repository,'person_clusters_for_visual_units',None)):
        targets.append((repository,'person_clusters_for_visual_units','person_decoration'))
    restored=[]
    try:
        for obj,attr,label in targets:
            original=getattr(obj,attr)
            def wrapped(*args,_original=original,_label=label,**kwargs):
                name=_label
                if name=='assemble' and len(args)>2:
                    req=args[2];name+=':lexical' if req.get('_lexical_only') else ':semantic_partial' if req.get('_semantic_preview') else ':full'
                start=time.monotonic()
                try:
                    with trace.span(name):return _original(*args,**kwargs)
                finally:
                    entry=totals.setdefault(name,{'count':0,'seconds':0.0});entry['count']+=1;entry['seconds']+=time.monotonic()-start
            setattr(obj,attr,wrapped);restored.append((obj,attr,original))
        yield
    finally:
        for obj,attr,original in reversed(restored):setattr(obj,attr,original)

def normalize_result(result, *, cancelled=False, deadline=False, error=None):
    """Control state wins even if an inner boundary returned a failure dictionary."""
    result=dict(result or {})
    if cancelled or deadline or isinstance(error, TimeoutError) or result.get('error') == 'TimeoutError':
        status='CANCELLED' if cancelled else 'INCOMPLETE'
        reason=('query_session_cancelled' if cancelled else 'search_deadline_45_seconds' if deadline else str(error) if error is not None else result.get('error_reason','query_timeout'))
        result.update(status=status,error='CancelledError' if cancelled else 'TimeoutError',
                      error_reason=reason,failed_component='query',recoverable=True)
        vm=dict(result.get('search_view_model') or {})
        vm.update(query_state='cancelled' if cancelled else 'incomplete',
                  error_code=result['error'],error_message=reason,failed_component='query',recoverable=True)
        result['search_view_model']=vm
    elif error is not None:
        result.update(status='FAIL',error=type(error).__name__,error_reason=str(error),failed_component='query')
    return result

def run_prepared_federation(bridge,prepared,config,base_manager,options,query_id,phases):
    from collections import OrderedDict
    from apps.media_archive_image_video_ui.prepared_federation import search_libraries,RequestEncoderCache
    from apps.media_archive_image_video_ui import v2_semantic_search
    targets=[]
    for path in dict.fromkeys(str(Path(p).expanduser().resolve()) for p in options.library_task_path):
        try:row=prepared.registry_row(path)
        except Exception:row={}
        targets.append({**row,'task_path':path,'task_name':row.get('task_name') or Path(path).parent.name,'database':row.get('database','')})
    cache=RequestEncoderCache(v2_semantic_search.encode);stats={};started=time.monotonic()
    def execute(library,offset):
        key=None;ok=False
        try:
            with trace.span('federated_library_prepare'):
                key,entry=prepared.get(library['task_path'],config,base_manager)
            library.update(entry['record']);manager=entry['manager'];store=entry['store']
            manager.request_query_cache={}
            repo=bridge.ReadonlyMediaRepository(manager.db_path)
            with contextlib.redirect_stderr(Progress(query_id,True,library['task_name'])),phase_metrics(bridge,manager,phases,repo):
                result=bridge.run_search(entry['config'],repo,manager,options.query,options.media_type,options.preview_window_ms,
                    offset,30,options.path_prefix,options.source_mtime_min,options.source_mtime_max,
                    options.has_ocr,options.has_person,record_history=False,search_intent=options.search_intent)
            ok=result.get('status')=='PASS';stats[key]=dict(store.stats);return result
        finally:
            if key is not None:prepared.finish(key,ok)
    def progress(items,total,done,count):
        send('progress',query_id,event={'contract':'media_archive_search_progress_v1','stage':'federated_library_complete',
            'stage_index':done,'total_stages':count,'message':f'已完成 {done}/{count} 个素材库',
            'detail':'结果已按所属库验证；其余素材库继续顺序检索','elapsed_seconds':time.monotonic()-started,
            'result_items':items,'total_count':total,'ordering':'federated_rank','update_mode':'replace'})
    original_encoder=v2_semantic_search.encode;v2_semantic_search.encode=cache
    try:result=search_libraries(targets,execute,offset=options.result_offset,limit=options.result_limit,on_results=progress)
    finally:v2_semantic_search.encode=original_encoder
    result.update(query=options.query,search_scope='selected_libraries',database_write=False,original_media_read=False,
        elapsed_seconds=time.monotonic()-started,library_states=prepared.status(),prepared_library_metrics=stats,
        query_vector_cache_entries=len(cache.entries),query_vector_cache_hits=cache.hits,query_vector_cache_misses=cache.misses,query_vector_cache_bytes=cache.bytes)
    return result

def request_totals(sample):
    start,before=sample;after=trace.stats()
    return {'start_mono':start,'end_mono':trace.now(),'pid':os.getpid(),
            **{k:None if before[k] is None or after[k] is None else after[k]-before[k] for k in ('cpu_s','read_bytes','write_bytes')}}

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--owner-pid',type=int,required=True);parser.add_argument('--initial-query-id');args=parser.parse_args()
    if args.owner_pid!=os.getppid():raise RuntimeError('query_session_owner_mismatch')
    import build151_sqlite_interruptible as sql_guard
    sql_guard.install()
    stop_requested=False;deadline_hit=False
    if args.initial_query_id:trace.begin(args.initial_query_id)
    with trace.span('session_module_import'):
        from apps.media_archive_image_video_ui import native_bridge as bridge,v2_semantic_search
    from apps.media_archive_image_video_ui.search_encoder_pool import QueryEncoderPool
    from apps.media_archive_image_video_ui.search_preparation import PreparedLibraries
    from apps.media_archive_image_video_ui.search_numeric_view import NumericViews,install
    views=NumericViews();install(views)
    prepared=PreparedLibraries(bridge)
    prepared_files={};prepared_keys=set();pool=QueryEncoderPool();views.memory=pool.memory;views.model_identity=lambda:tuple(sorted(pool.workers));v2_semantic_search.encode=pool.encode;store=None
    def stopped(signum,frame):
        nonlocal stop_requested
        stop_requested=True;sql_guard.cancel()
        raise SystemExit('query_session_stopped')
    def deadline(signum,frame):
        nonlocal deadline_hit
        deadline_hit=True;sql_guard.cancel()
        raise TimeoutError('search_deadline_45_seconds')
    signal.signal(signal.SIGTERM,stopped);signal.signal(signal.SIGALRM,deadline)
    alive=threading.Event();alive.set()
    def owner_watch():
        while alive.wait(0.5):
            if os.getppid()!=args.owner_pid:os.kill(os.getpid(),signal.SIGTERM);return
            time.sleep(0.5)
    threading.Thread(target=owner_watch,daemon=True).start()
    last=time.monotonic();query_id=None
    try:
        send('ready',None,pid=os.getpid())
        while not stop_requested and (time.monotonic()-last<60 or time.monotonic()<pool.lease_until):
            if not select.select([sys.stdin],[],[],2)[0]:
                pool.maintain();views.maintain();trace.release_record('idle_maintenance',trace.take());continue
            line=sys.stdin.readline(65537)
            if not line:break
            if len(line)>65536:raise ValueError('query_session_request_too_large')
            request=json.loads(line);query_id=request.get('query_id')
            if not isinstance(query_id,str) or len(query_id)>128:raise ValueError('query_id_invalid')
            if trace.current_id()!=query_id:trace.begin(query_id)
            query_sample=trace.checkpoint()
            with trace.span('query_received'):pass
            operation=request.get('operation','search')
            if operation not in {'search','prepare','lease'}:raise ValueError('query_operation_invalid')
            if operation in {'prepare','lease'}:pool.renew_lease()
            if operation=='lease':
                try:
                    unchanged=all((lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))(Path(p).stat())==identity for p,identity in prepared_files.items())
                except OSError:unchanged=False
                if not unchanged:
                    views.clear()
                    for key in list(pool.workers):pool._close(key)
                    prepared_keys.clear()
                pool.maintain();views.maintain()
                send('result',query_id,result={'status':'PASS','semantic_ready':bool(prepared_keys) and prepared_keys.issubset(pool.workers),'workers':len(pool.workers),'library_states':prepared.status()})
                continue
            options=bridge.build_parser().parse_args(['--config',args.config]+request['arguments'])
            views.select_scope(options.library_task_path or ['CURRENT'])
            if options.command!='search':raise ValueError('query_session_read_only_search_required')
            deadline_hit=False;sql_guard.reset()
            result={};entry_key=None;store=None
            remaining=min(45.0,float(request.get('deadline_mono',time.monotonic()+45))-time.monotonic()) if operation=='search' else 45.0
            if remaining<=0:
                send('result',query_id,result=normalize_result({},deadline=True));continue
            signal.setitimer(signal.ITIMER_REAL,remaining)
            try:
                with contextlib.redirect_stderr(Progress(query_id)),contextlib.redirect_stdout(ERR):
                    phases={};phase_start=time.monotonic()
                    with trace.span('load_runtime'):
                        config,repo,manager=bridge.load_runtime(Path(args.config))
                    phases["load_runtime"]={"count":1,"seconds":time.monotonic()-phase_start}
                    if options.library_task_path and operation=='search':
                        result=run_prepared_federation(bridge,prepared,config,manager,options,query_id,phases)
                    else:
                        if len(options.library_task_path or [])>1:raise ValueError('prepare_one_library_per_request')
                        with trace.span('session_store_prepare'):
                            entry_key,entry=prepared.get((options.library_task_path or [None])[0],config,manager)
                            config=entry['config'];manager=entry['manager'];repo=bridge.ReadonlyMediaRepository(manager.db_path);store=entry['store']
                        if operation=='prepare':
                            with trace.span('search_prepare'):
                                ready=manager.readiness()
                                if not ready.get('ready'):raise RuntimeError('selected_library_not_searchable')
                                with store.connect() as c:bindings=v2_semantic_search.bindings(c)
                                for stage,namespace,cfg,proof in bindings:
                                    if options.search_intent=='text' and stage=='openclip':continue
                                    if not v2_semantic_search.weights_match(cfg,proof):raise RuntimeError('semantic_search_index_model_contract_mismatch')
                                    pool.encode(stage,cfg,None,manager.embedding_python if stage=='embedding' else manager.openclip_python,prepare_only=True)
                                prepared_keys=set(pool.workers);pool.renew_lease()
                                paths=[Path(args.config),manager.db_path.parent/'model-config.json']
                                for stage,namespace,cfg,proof in bindings:
                                    root=Path(cfg['model_path']).resolve()
                                    paths.extend(root/rel if root.is_dir() else root for rel in proof.get('model_files',{}).get(str(root),{}))
                                prepared_files={str(p):(lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))(p.stat()) for p in paths}
                                result={'status':'PASS','semantic_ready':bool(pool.workers),'workers':len(pool.workers),'library_states':prepared.status()}
                        else:
                            with phase_metrics(bridge,manager,phases,repo):
                                result=bridge.run_search(config,repo,manager,options.query,options.media_type,options.preview_window_ms,
                                    options.result_offset,options.result_limit,options.path_prefix,options.source_mtime_min,options.source_mtime_max,
                                    options.has_ocr,options.has_person,search_intent=options.search_intent)
                    result=normalize_result(result,cancelled=stop_requested,deadline=deadline_hit)
                    with trace.span('store_finish'):
                        if entry_key is not None:prepared.finish(entry_key,result.get('status')=='PASS')
                    if operation=='prepare':result['library_states']=prepared.status()
                signal.setitimer(signal.ITIMER_REAL,0)
                trace.complete('query_total',query_sample)
                result['query_session_metrics']={'pool':dict(pool.stats),'snapshot':dict(store.stats) if store is not None else {},'rss_mb':pool.rss(),'pid':os.getpid(),'phases':phases,'numeric_view':dict(views.stats,bytes=views.bytes,share=views.share),'request_totals':request_totals(query_sample),'trace':trace.take()}
                with trace.span('ipc_send'):
                    send('result',query_id,result=result)
                trace.release_record('query_ipc',trace.take())
                ERR.write('QUERY_SESSION_STATS='+json.dumps({'query_id':query_id,'pool':pool.stats,'snapshot':store.stats if store is not None else {},'rss_mb':pool.rss()})+'\n');ERR.flush()
            except Exception as error:
                signal.setitimer(signal.ITIMER_REAL,0);sql_guard.reset()
                if entry_key is not None:
                    prepared.finish(entry_key,False)
                    if operation=='prepare':
                        if entry_key in prepared.states:prepared.states[entry_key].update(state='unavailable',reason=str(error),retained=False)
                        prepared.invalidate(entry_key)
                trace.complete('query_total',query_sample,'error')
                result=normalize_result(result,cancelled=stop_requested,deadline=deadline_hit,error=error)
                result['query_session_metrics']={'numeric_view':dict(views.stats,bytes=views.bytes,share=views.share),'request_totals':request_totals(query_sample),'trace':trace.take()}
                send('result',query_id,result=result)
            last=time.monotonic()
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);sql_guard.reset();alive.clear()
        try:
            prepared.close()
        finally:
            with trace.span('session_release'):views.clear();pool.close()
            trace.release_record('session_release',trace.take())
        ERR.write('QUERY_SESSION_CLOSED\n');ERR.flush()

if __name__=='__main__':main()
