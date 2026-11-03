"""Bounded, opt-in monotonic instrumentation, independent of media semantics."""
import collections
import contextlib
import functools
import json
import statistics
import threading
import time
from pathlib import Path


class Performance:
    def __init__(self, path=None):
        self.path=Path(path) if path else None
        self.local=threading.local(); self.lock=threading.RLock()
        self.stats={}; self.counters=collections.Counter(); self.started=time.monotonic()
        self.last_flush=self.started; self.events=collections.deque(maxlen=8192)
        self.sql_stats={}

    def record_sql(self,sql,seconds):
        """Bounded statement shapes only; never retain SQL parameters/results."""
        if not self.path:return
        shape=' '.join(sql.split())[:2048]
        with self.lock:
            if shape not in self.sql_stats and len(self.sql_stats)>=256:shape='OTHER_STATEMENTS'
            s=self.sql_stats.setdefault(shape,{'count':0,'seconds':0.,'main_seconds':0.,'samples':collections.deque(maxlen=128)})
            s['count']+=1;s['seconds']+=seconds;s['samples'].append(seconds)
            if threading.current_thread() is threading.main_thread():s['main_seconds']+=seconds

    def event(self, value):
        with self.lock:
            if len(self.events)==self.events.maxlen:self.counters['TIMING_EVENT_SAMPLES_DROPPED']+=1
            self.events.append(value)

    def record(self,name,seconds,stage=None,**values):
        if not self.path:return
        stage=stage or getattr(self.local,'stage','scheduler')
        with self.lock:
            key=stage+':'+name
            s=self.stats.setdefault(key,{'count':0,'seconds':0.,'self_seconds':0.,'main_self_seconds':0.,'samples':collections.deque(maxlen=2048)})
            s['count']+=1;s['seconds']+=seconds;s['samples'].append(seconds)
            own=values.get('self_seconds',seconds);s['self_seconds']+=own
            if threading.current_thread() is threading.main_thread():s['main_self_seconds']+=own

    def count(self,name,n=1):
        with self.lock:self.counters[name]+=n

    @contextlib.contextmanager
    def phase(self,name,stage=None,**values):
        old=getattr(self.local,'stage','scheduler')
        if stage:self.local.stage=stage
        start=time.monotonic()
        stack=getattr(self.local,'stack',None)
        if stack is None:stack=[];self.local.stack=stack
        frame=[0.,name];stack.append(frame)
        # SQL/fetch costs remain fully counted and histogrammed. Repeating
        # two detailed events for every row read had inflated the trace to GBs
        # and displaced scheduler/job boundary events from the bounded ring.
        detailed=self.path and name not in ('DB_QUERY','DB_WRITE','DB_FETCH','DB_CONNECT','DB_TRANSACTION')
        if detailed:
            self.event({'event':name+'_START','time':time.time(),'stage':stage or old,**values})
        try:yield
        finally:
            elapsed=time.monotonic()-start;stack.pop()
            if stack:stack[-1][0]+=elapsed
            self.record(name,elapsed,stage,self_seconds=max(0.,elapsed-frame[0]))
            if detailed:
                self.event({'event':name+'_END','time':time.time(),'stage':stage or old,'ms':elapsed*1000,**values})
            self.local.stage=old

    def flush(self,force=False):
        if not self.path or (not force and time.monotonic()-self.last_flush<5):return
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            result={}
            for key,s in self.stats.items():
                samples=sorted(s['samples'])
                elapsed=time.monotonic()-self.started
                result[key]={'count':s['count'],'seconds':s['seconds'],'self_seconds':s['self_seconds'],'main_self_seconds':s['main_self_seconds'],'mean_ms':s['seconds']*1000/s['count'],'median_ms':statistics.median(samples)*1000,'p95_ms':samples[min(len(samples)-1,int(len(samples)*.95))]*1000,'quantile_sample_count':len(samples),'main_wall_share_percent':s['main_self_seconds']/elapsed*100 if elapsed else 0}
            sql_stats=[]
            for shape,s in sorted(self.sql_stats.items(),key=lambda x:x[1]['main_seconds'],reverse=True):
                samples=sorted(s['samples'])
                sql_stats.append({'statement':shape,'count':s['count'],'seconds':s['seconds'],'main_seconds':s['main_seconds'],
                    'mean_ms':s['seconds']*1000/s['count'],'p95_ms':samples[min(len(samples)-1,int(len(samples)*.95))]*1000})
            with self.path.open('a') as h:
                h.write(json.dumps({'time':time.time(),'elapsed':time.monotonic()-self.started,'stats':result,'sql_stats':sql_stats,'counters':dict(self.counters),'quantiles':'last2048 duration samples; totals include all observations','events':list(self.events)},ensure_ascii=False)+'\n')
            self.events.clear();self.last_flush=time.monotonic()

    def wrap(self,obj,method,name):
        original=getattr(obj,method)
        @functools.wraps(original)
        def timed(*args,**kwargs):
            with self.phase(name):result=original(*args,**kwargs)
            if name=='RESULT_COMMIT':self.count('COMPLETED_'+getattr(self.local,'stage','scheduler'))
            if name=='OUTBOX_PUBLISH':self.count('OUTBOX_EVENTS')
            if name=='OUTBOX_PUBLISH' and len(args)>1:self.count('EVENT_'+str(args[1]))
            return result
        setattr(obj,method,timed)


class TimedConnection:
    def __init__(self,connection,perf):self.connection=connection;self.perf=perf
    def __getattr__(self,key):return getattr(self.connection,key)
    def __enter__(self):self.connection.__enter__();return self
    def __exit__(self,*args):
        with self.perf.phase('DB_TRANSACTION'):return self.connection.__exit__(*args)
    def execute(self,sql,*args):
        operation='DB_QUERY' if sql.lstrip().upper().startswith(('SELECT','WITH','PRAGMA')) else 'DB_WRITE'
        start=time.monotonic()
        try:
            with self.perf.phase(operation):cursor=self.connection.execute(sql,*args)
        finally:self.perf.record_sql(sql,time.monotonic()-start)
        if any(f[1]=='INDEX_CALLBACK' for f in getattr(self.perf.local,'stack',[])):
            self.perf.count('INDEX_SQL_COUNT')
            if operation=='DB_WRITE':
                self.perf.count('INDEX_WRITE_STATEMENTS')
                self.perf.count('INDEX_WRITES',max(0,cursor.rowcount))
        return TimedCursor(cursor,self.perf,sql)
    def executescript(self,*args):
        with self.perf.phase('DB_WRITE'):return self.connection.executescript(*args)


class TimedCursor:
    def __init__(self,cursor,perf,sql):self.cursor=cursor;self.perf=perf;self.sql=sql.lower()
    def __getattr__(self,key):return getattr(self.cursor,key)
    def _rows(self,count):
        if 'from v2_sources' in self.sql:self.perf.count('SOURCES_SCANNED',count)
        if 'from v2_artifacts' in self.sql:self.perf.count('ARTIFACTS_SCANNED',count)
        if any(f[1]=='SOURCE_ADVANCE' for f in getattr(self.perf.local,'stack',[])):
            if 'from v2_sources' in self.sql:self.perf.count('SOURCE_ADVANCE_SOURCE_ROWS',count)
            if 'from v2_artifacts' in self.sql:self.perf.count('SOURCE_ADVANCE_ARTIFACT_ROWS',count)
    def fetchone(self):
        with self.perf.phase('DB_FETCH'):row=self.cursor.fetchone()
        self._rows(int(row is not None));return row
    def fetchall(self):
        with self.perf.phase('DB_FETCH'):rows=self.cursor.fetchall()
        self._rows(len(rows));return rows
    def __iter__(self):
        while True:
            row=self.fetchone()
            if row is None:return
            yield row


def attach(perf,store,pipeline):
    if not perf.path:return
    original=store.connect
    def connection():
        with perf.phase('DB_CONNECT'):c=original()
        return TimedConnection(c,perf)
    store.connect=connection
    if getattr(pipeline,'index',None) is not None:
        pipeline.index.performance=perf
        perf.wrap(pipeline.index,'callback','INDEX_CALLBACK')
    if getattr(pipeline,'final_projection',None) is not None:
        pipeline.final_projection.performance=perf
    for method,name in [('claim','DB_CLAIM'),('complete','RESULT_COMMIT'),('emit','OUTBOX_PUBLISH'),('has_runnable_jobs','RUNNABLE_FILTER'),('peek_runnable_job','QUEUE_DISCOVERY'),('deliver','EVENT_DELIVERY'),('deliver_subscribed','EVENT_DELIVERY')]:
        if hasattr(store,method):perf.wrap(store,method,name)
    if hasattr(pipeline,'selection'):
        perf.wrap(pipeline.selection,'select','SELECTION_ADVANCE')
    if hasattr(pipeline,'_canonicalize_local'):
        perf.wrap(pipeline,'_canonicalize_local','JOIN_CHECK')
    for obj,method,name in [(getattr(pipeline,'people',None),'ingest_ready','MARKER_PEOPLE'),(getattr(pipeline,'people',None),'build_source','MARKER_PEOPLE'),(getattr(pipeline,'documents',None),'advance','MARKER_DOCUMENTS'),(getattr(pipeline,'index',None),'advance','MARKER_INDEX'),(getattr(pipeline,'final_projection',None),'reconcile_heads','MARKER_HEADS')]:
        if obj and hasattr(obj,method):perf.wrap(obj,method,name)
