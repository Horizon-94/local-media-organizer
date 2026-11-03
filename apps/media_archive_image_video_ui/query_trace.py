"""Bounded query-only diagnostic spans; never stores queries, vectors or source paths."""
import contextlib,contextvars,ctypes,os,resource,time
_state=contextvars.ContextVar('query_trace',default=None)
class Usage(ctypes.Structure):
 _fields_=[('uuid',ctypes.c_ubyte*16)]+[(x,ctypes.c_uint64) for x in 'user system pkg intr pageins wired resident footprint start exit childuser childsystem childpkg childintr childpageins childelapsed read write'.split()]
try:_lib=ctypes.CDLL('/usr/lib/libproc.dylib')
except OSError:_lib=None

# Python 3.9 on macOS uses a process-relative monotonic epoch. Use the
# same boot-relative mach clock as Swift DispatchTime for every runtime.
class _Timebase(ctypes.Structure):
 _fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
_system=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
_system.mach_absolute_time.restype=ctypes.c_uint64
_tb=_Timebase()
if _system.mach_timebase_info(ctypes.byref(_tb))!=0 or not _tb.denom:raise RuntimeError('mach_timebase_unavailable')
def now():return _system.mach_absolute_time()*_tb.numer/_tb.denom/1e9

def begin(query_id):
 if not isinstance(query_id,str) or not query_id or len(query_id)>128:raise ValueError('trace_query_id_invalid')
 _state.set({'query_id':query_id,'events':[],'truncated':False})
def current_id():return (_state.get() or {}).get('query_id')
def stats():
 r=resource.getrusage(resource.RUSAGE_SELF);s={'cpu_s':r.ru_utime+r.ru_stime,'read_bytes':None,'write_bytes':None}
 if _lib:
  u=Usage()
  if _lib.proc_pid_rusage(os.getpid(),2,ctypes.byref(u))==0:s.update(read_bytes=u.read,write_bytes=u.write)
 return s

def append(event):
 state=_state.get()
 if state is None:return
 if len(state['events'])<128:state['events'].append(event)
 else:state['truncated']=True

@contextlib.contextmanager
def span(name):
 state=_state.get()
 if state is None:yield;return
 start=now();a=stats();outcome='ok'
 try:yield
 except BaseException:outcome='error';raise
 finally:
  b=stats();end=now()
  append({'query_id':state['query_id'],'pid':os.getpid(),'stage':name,'start_mono':start,'end_mono':end,'cpu_s':b['cpu_s']-a['cpu_s'],
          'read_bytes':None if a['read_bytes'] is None or b['read_bytes'] is None else b['read_bytes']-a['read_bytes'],
          'write_bytes':None if a['write_bytes'] is None or b['write_bytes'] is None else b['write_bytes']-a['write_bytes'],'outcome':outcome})

def merge(events):
 for event in events:
  if event.get('query_id')!=current_id():raise ValueError('query_trace_identity_mismatch')
  append(event)
def take():
 state=_state.get()
 if state is None:return []
 result=state['events'];state['events']=[];return result

def release_record(reason,events):
 if not events:return
 # Bounded optional diagnostic; failures never change search or cleanup.
 try:
  import json
  from pathlib import Path
  root=os.environ.get('MEDIA_ARCHIVE_APP_STATE_ROOT')
  if not root:return
  p=Path(root)/'logs/query-release.jsonl'
  if p.exists() and p.stat().st_size>=1024*1024:return
  p.parent.mkdir(parents=True,exist_ok=True)
  with p.open('a') as f:f.write(json.dumps({'query_id':current_id(),'pid':os.getpid(),'reason':reason,'event_count':len(events),'truncated':len(events)>8,'events':events[-8:]})+'\n')
 except Exception:pass


def checkpoint():return (now(),stats())
def complete(name,sample,outcome='ok'):
 start,a=sample;b=stats()
 append({'query_id':current_id(),'pid':os.getpid(),'stage':name,'start_mono':start,'end_mono':now(),'cpu_s':b['cpu_s']-a['cpu_s'],
 'read_bytes':None if a['read_bytes'] is None or b['read_bytes'] is None else b['read_bytes']-a['read_bytes'],
 'write_bytes':None if a['write_bytes'] is None or b['write_bytes'] is None else b['write_bytes']-a['write_bytes'],'outcome':outcome})
