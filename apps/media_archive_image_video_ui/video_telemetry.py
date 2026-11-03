"""External diagnostic append failures cannot undo a durable ACK."""
import collections,json,threading,time
from pathlib import Path
_lock=threading.RLock()
_errors=collections.deque(maxlen=32)
def append(path,value):
    try:
        line=json.dumps(value,sort_keys=True)+'\n'
        with _lock:
            with Path(path).open('a') as f:f.write(line)
        return True
    except (OSError,TypeError,ValueError) as error:
        with _lock:_errors.append({'timestamp':time.time(),'path':str(path),'error':str(error)})
        return False
def errors(root=None):
    with _lock:return [e for e in _errors if root is None or Path(e['path']).is_relative_to(root)]

# Task-scoped diagnostic spans. Never a model/DB/business-path wrapper.
CLOSURE_OUTPUT = Path(__import__('os').environ.get('MEDIA_ARCHIVE_WHISPER_DIAGNOSTICS_ROOT',str(Path.home() / 'Library/Logs/LocalMediaOrganizer/diagnostics')))

class ClosureSpan:
    def __init__(self,workspace,stage,function=None):
        import sys,uuid
        frame=sys._getframe(1)
        self.value=dict(stage=stage,function=function or frame.f_code.co_name,
                        callsite=frame.f_code.co_filename+':'+str(frame.f_lineno),
                        workspace=str(workspace),span_id=uuid.uuid4().hex)
    def emit(self,event,**values):
        import os,json,time,threading
        try:
            path=CLOSURE_OUTPUT/'finalization/timeline.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
            row=dict(self.value,event=event,at=time.time(),mono=time.monotonic(),pid=os.getpid(),thread=threading.current_thread().name)
            row.update(values)
            fd=os.open(path,os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
            try:os.write(fd,(json.dumps(row,default=str)+'\n').encode())
            finally:os.close(fd)
        except OSError as error:
            __import__('sys').stderr.write('CLOSURE_SPAN_WRITE_FAILED '+repr(error)+'\n')
    def __enter__(self):
        import time
        self.started=time.monotonic();self.wall=time.time();self.emit('BEGIN',mono=self.started,at=self.wall);return self
    def __exit__(self,typ,value,tb):
        import time
        ended=time.monotonic();self.emit('END',mono=ended,duration=ended-self.started,error=str(value) if typ else None)
