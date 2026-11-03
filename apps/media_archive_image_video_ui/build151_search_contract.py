"""Versioned read-only search projection; registry identity, not session paths."""
import time
from pathlib import Path
from . import build151_library_lifecycle as libraries
from . import query_trace as trace

def library_contract(config):
    pointer=libraries.read(libraries.root()/'runtime/active_library.json')
    identity=config.get('library_id') or pointer.get('library_id') or pointer.get('task_id')
    record,path,task=libraries.resolve(identity)
    source=Path(task['source_root'])
    return {'library_id':record['library_id'],'library_name':task['name'],
        'durable_library_path':str(path.parent),'source_root':str(source),'source_online':None,'source_availability':'unchecked',
        'library_ready':record.get('lifecycle_state')=='COMPLETED_SEARCHABLE',
        'resolution_source':'canonical durable registry / library_id','database':task['database']}

def install(g):
    original=g['run_search']
    def run(config,repository,manager,*args,**kwargs):
        started=time.monotonic()
        try:
            with trace.span('library_resolve_catalog'):
                library=library_contract(config)
                if Path(repository.db_path).resolve()!=Path(library['database']).resolve():
                    raise ValueError('SEARCH_LIBRARY_ID_DATABASE_MISMATCH')
        except TimeoutError:
            raise
        except Exception as error:
            return {'status':'FAIL','error':type(error).__name__,'error_reason':str(error),
                'search_view_model':{'contract':'build151_search_view_model_v1','query_state':'error','query_duration':time.monotonic()-started,'result_count':0,'channels':[],
                    'error_code':type(error).__name__,'error_message':str(error),'failed_component':'library_resolve','failed_path':getattr(error,'filename',None),'recoverable':True}}
        # Query failures must retain their own stage; cancellation/deadline propagate.
        response=original(config,repository,manager,*args,**kwargs)
        from contextlib import closing
        from .central_database import connect
        with trace.span('library_result_counts'), closing(connect(Path(library['database']),readonly=True)) as con:
            library['total_source_count']=con.execute('SELECT COUNT(*) FROM release_registry WHERE present=1').fetchone()[0]
            library['searchable_source_count']=con.execute('SELECT COUNT(*) FROM release_registry WHERE present=1 AND committed=1').fetchone()[0]
        channels=getattr(manager,'search_channels',[])
        state=('ready' if response.get('result_total_count',0)>0 else 'empty') if response.get('status')=='PASS' else 'error'
        response['search_view_model']={'contract':'build151_search_view_model_v1','library':library,
            'query_state':state,'query_duration':round(time.monotonic()-started,3),
            'result_count':response.get('result_total_count',0),'channels':channels,
            'error_code':response.get('error_code') or response.get('error'),
            'error_message':response.get('error_message') or response.get('error_reason'),
            'failed_component':response.get('failed_component'),'failed_path':response.get('failed_path'),
            'recoverable':response.get('recoverable',False)}
        libraries.log('search_execution',query=args[0] if args else kwargs.get('query'),library_id=library['library_id'],query_state=state,channels=channels,failed_path=response.get('failed_path'))
        return response
    g['run_search']=run
