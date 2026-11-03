"""Sequential, scoped exact federation with explicit incomplete outcomes."""
import hashlib,sqlite3,time

def scoped(library,rows):
    prefix=hashlib.sha256(library['database'].encode()).hexdigest()[:24]
    out=[]
    for rank,original in enumerate(rows,1):
        row=dict(original);local_id=str(row.get('result_id') or row.get('visual_unit_id') or rank)
        row.update(result_id=prefix+':'+local_id,library_local_result_id=local_id,
            library_task_path=library['task_path'],library_name=library['task_name'],
            library_database=library['database'],federation_rank=rank)
        out.append(row)
    return out

def search_libraries(libraries,execute,*,offset=0,limit=30,on_results=None):
    offset,limit=max(0,offset),max(1,min(200,limit));wanted=offset+limit
    items=[];errors=[];completed=[];total=0;coverage={};coverage_complete=True;incomplete=False
    required={'eligible_visual_unit_count','scanned_visual_vector_count','scanned_text_vector_count'}
    for index,library in enumerate(libraries):
        local=[];cursor=0;visited=set();library_total=0;library_coverage={}
        try:
            while len(local)<wanted:
                if cursor in visited:raise RuntimeError('library_pagination_not_advancing')
                visited.add(cursor);report=execute(library,cursor)
                state=report.get('status','PASS')
                if state in {'INCOMPLETE','CANCELLED'} or report.get('error')=='TimeoutError':
                    partial=report.get('result_items') or []
                    items.extend(scoped(library,partial[:wanted]));items.sort(key=lambda r:(r['federation_rank'],r['library_database'],r['result_id']));items=items[:wanted]
                    total+=len(partial)
                    raise TimeoutError(report.get('error_reason') or 'search_incomplete')
                if state!='PASS':raise RuntimeError(report.get('error_reason') or report.get('error') or 'search_failed')
                local.extend(report.get('result_items') or [])
                if cursor==0:library_total=int(report.get('result_total_count',len(local)));library_coverage=report.get('coverage') or {}
                cursor=report.get('next_result_offset')
                if cursor is None:break
            total+=library_total;completed.append({'task_path':library['task_path'],'name':library['task_name'],'count':library_total})
            if not all(isinstance(library_coverage.get(k),int) for k in required):coverage_complete=False
            for name,value in library_coverage.items():
                if isinstance(value,(int,float)):coverage[name]=coverage.get(name,0)+value
            items.extend(scoped(library,local[:wanted]));items.sort(key=lambda r:(r['federation_rank'],r['library_database'],r['result_id']));items=items[:wanted]
            if on_results:on_results(items[offset:offset+limit],total,index+1,len(libraries))
        except TimeoutError as error:
            incomplete=True;coverage_complete=False
            errors.append({'library':library['task_name'],'task_path':library['task_path'],'reason':str(error)})
            errors.extend({'library':x['task_name'],'task_path':x['task_path'],'reason':'总查询预算耗尽，尚未执行'} for x in libraries[index+1:]);break
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
            coverage_complete=False;errors.append({'library':library['task_name'],'task_path':library['task_path'],'reason':str(error)})
    selected=items[offset:offset+limit]
    return dict(status='INCOMPLETE' if incomplete else 'PASS' if completed else 'FAIL',
        result_items=selected,result_count=len(selected),result_total_count=total,
        result_offset=offset,result_limit=limit,next_result_offset=offset+limit if not incomplete and offset+limit<total else None,
        coverage=coverage if coverage_complete else None,library_errors=errors,completed_libraries=completed,
        partial_results=bool(errors),search_results_read_only=True,
        error='TimeoutError' if incomplete else '所选素材库均无法搜索' if not completed else None,
        error_reason='搜索未完成，可重试' if incomplete else '',cross_library_person_merge=False)

class RequestEncoderCache:
    """One query only, after each library's own weights/space validation."""
    def __init__(self,encoder,max_bytes=8*1024*1024,max_entries=8):
        from collections import OrderedDict
        self.encoder=encoder;self.max_bytes=max_bytes;self.max_entries=max_entries;self.entries=OrderedDict();self.bytes=0;self.hits=0;self.misses=0
    def __call__(self,stage,cfg,query,python_path):
        import json,sys
        from pathlib import Path
        clean={k:v for k,v in cfg.items() if k not in {'_event_log','_inference_event_log','_worker_ready_ack'}}
        key=(stage,str(python_path),json.dumps(clean,sort_keys=True),query)
        if key in self.entries:
            self.hits+=1;self.entries.move_to_end(key);return list(self.entries[key][0])
        self.misses+=1;vector=self.encoder(stage,cfg,query,python_path);frozen=tuple(vector)
        size=sys.getsizeof(frozen)+sum(sys.getsizeof(x) for x in frozen)+sum(sys.getsizeof(x) for x in key)
        if size<=self.max_bytes:
            while self.entries and (len(self.entries)>=self.max_entries or self.bytes+size>self.max_bytes):
                _,(_,old)=self.entries.popitem(last=False);self.bytes-=old
            self.entries[key]=(frozen,size);self.bytes+=size
        return vector
