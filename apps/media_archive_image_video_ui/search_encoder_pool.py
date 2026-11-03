"""Bounded query-only worker ownership. The App-owned session is the sole caller."""
import os,time,threading,json,subprocess
from .v2_producer import fingerprint
from . import query_trace as trace

class QueryEncoderPool:
    IDLE_SECONDS=60
    MAX_RESIDENT_MB=6144
    def __init__(self,factory=None,memory=None,clock=time.monotonic,rss=None):
        from .v2_real_handlers import SubprocessModelHandler
        from .v2_resource_budget import CachedSystemMemory
        self.factory=factory or SubprocessModelHandler
        self.memory=memory or CachedSystemMemory()
        self.rss=rss or self._resident_mb
        self.lease_until=0.0;self.clock=clock;self.workers={};self.lock=threading.Lock();self.closed=False
        self.stats={'created':0,'reused':0,'evicted':0,'encode_seconds':0.0}
    def _resident_mb(self):
        pids=[str(w.process.pid) for w,_,_ in self.workers.values()]
        if not pids:return 0
        try:
            output=subprocess.check_output(['/bin/ps','-o','rss=','-p',','.join(pids)],text=True,timeout=2)
            return sum(float(v) for v in output.split())/1024
        except Exception:return float('inf')  # Unknown footprint disables reuse.
    def _close(self,key):
        worker,_,_=self.workers[key]
        # Close uses the existing TERM/wait contract; never SIGKILL. A failure
        # leaves ownership retained and aborts this session rather than leaking.
        with trace.span('model_release'):
            worker.close()
        del self.workers[key];self.stats['evicted']+=1
    def maintain(self):
        with self.lock:
            memory=self.memory();pressure=not memory.get('healthy') or memory.get('pressure_level',0)>1 or memory.get('available_mb',0)<3072
            now=self.clock();over_budget=self.rss()>self.MAX_RESIDENT_MB
            for key in list(self.workers):
                if pressure or over_budget or not self.workers[key][0].is_healthy() or (now>=self.lease_until and now-self.workers[key][1]>=self.IDLE_SECONDS):self._close(key)
    def renew_lease(self):
        self.lease_until=self.clock()+30
    def encode(self,stage,cfg,query,python_path,*,prepare_only=False):
        if stage not in {'embedding','openclip'}:raise ValueError('query_pool_stage_invalid')
        with self.lock:
            if self.closed:raise RuntimeError('query_pool_closed')
            query_cfg={k:v for k,v in cfg.items() if k not in {'_event_log','_inference_event_log','_worker_ready_ack'}}
            key=(stage,str(python_path),fingerprint(query_cfg))
            for old in list(self.workers):
                if old[0]==stage and old!=key:self._close(old)
            if key in self.workers and not self.workers[key][0].is_healthy():self._close(key)
            memory=self.memory();needed=0 if key in self.workers else (4096 if stage=='embedding' else 2048)
            if not memory.get('healthy') or memory.get('pressure_level',0)>1 or memory.get('available_mb',0)-needed<3072:
                for old in list(self.workers):self._close(old)
                raise RuntimeError('semantic_search_memory_headroom_wait:请等待内存释放后重试')
            if key not in self.workers:
                options={**query_cfg,'python_path':str(python_path),'response_timeout_seconds':45,'query_owner_pid':os.getpid(),'_query_trace_id':trace.current_id()}
                if stage=='openclip':options['query_text_only']=True
                self.workers[key]=(self.factory(stage,options),self.clock(),needed);self.stats['created']+=1
            else:self.stats['reused']+=1
            worker,_,reservation=self.workers[key];start=self.clock()
            try:
                with trace.span(stage+'_worker_call'):
                    response=worker({'request_kind':'query_prepare','_query_id':trace.current_id()} if prepare_only else {'text':query,'_query_id':trace.current_id()})
                trace.merge(response.pop('_query_trace',[]))
                vector=None if prepare_only and response.get('query_ready') is True else response['vector']
                if self.rss()>self.MAX_RESIDENT_MB:
                    for old in list(self.workers):self._close(old)
                return vector
            except BaseException:
                self._close(key);raise
            finally:
                self.stats['encode_seconds']+=self.clock()-start
                if key in self.workers:self.workers[key]=(worker,self.clock(),reservation)
    def close(self):
        with self.lock:
            self.closed=True;errors=[]
            for key in list(self.workers):
                try:self._close(key)
                except Exception as error:errors.append(str(error))
            if errors:raise RuntimeError('query_pool_cleanup_failed:'+json.dumps(errors))
