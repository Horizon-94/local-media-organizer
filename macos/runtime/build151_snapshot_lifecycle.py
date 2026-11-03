"""Bounded snapshot read: cancellation/incomplete are not database failures."""
import os,sys,signal,threading,sqlite3,runpy,time,json,io
from contextlib import redirect_stdout
from pathlib import Path

class ReadCancellation:
 def __init__(self):
  self.event=threading.Event();self._reason=None;self.lock=threading.Lock();self.signal_cancelled=False
 @property
 def reason(self):
  return self._reason or ("SNAPSHOT_READ_CANCELLED" if self.signal_cancelled else None)
 def signal_cancel(self,*_):
  # Signal handlers may interrupt cancel/Event.set while their locks are held.
  # Only publish a flag here; never acquire a lock or set an Event in a handler.
  self.signal_cancelled=True
 def is_cancelled(self):
  return self.signal_cancelled or self.event.is_set()
 def cancel(self,reason):
  with self.lock:
   if self.reason is None:self._reason=reason;self.event.set()
 def response(self):
  if self.reason is None:return None
  return {'status':'INCOMPLETE' if self.reason=='SNAPSHOT_READ_BUDGET_EXCEEDED' else 'CANCELLED',
          'reason_code':self.reason,'observed_at':time.time()}

def main():
 owner=os.getppid();state=ReadCancellation();finished=threading.Event();original=sqlite3.connect
 deadline=time.monotonic()+15
 class Cancellable(sqlite3.Connection):
  def execute(self,*args,**kwargs):
   if state.is_cancelled():raise sqlite3.OperationalError(state.reason)
   return super().execute(*args,**kwargs)
 def connect(*args,**kwargs):
  kwargs.setdefault('factory',Cancellable);c=original(*args,**kwargs)
  c.set_progress_handler(lambda:int(state.is_cancelled()),10000);return c
 sqlite3.connect=connect
 signal.signal(signal.SIGTERM,state.signal_cancel)
 def watch():
  while not finished.wait(.5):
   if os.getppid()!=owner:state.cancel('SNAPSHOT_OWNER_EXITED');return
   if time.monotonic()>=deadline:state.cancel('SNAPSHOT_READ_BUDGET_EXCEEDED');return
 threading.Thread(target=watch,daemon=True).start()
 sys.argv[0]=str(Path(__file__).resolve().parent/'python_bridge.py')
 buffer=io.StringIO();code=0
 try:
  with redirect_stdout(buffer):
   try:runpy.run_path(sys.argv[0],run_name='__main__')
   except SystemExit as error:code=error.code or 0
   except (sqlite3.Error,OSError) as error:
    if state.reason is None:
     print(json.dumps({'status':'FAIL','error':type(error).__name__,'error_reason':str(error)},ensure_ascii=False))
    code=2
 finally:finished.set();sqlite3.connect=original
 response=state.response()
 if response is not None:print(json.dumps(response,ensure_ascii=False));return 2
 print(buffer.getvalue(),end='');return code

if __name__=='__main__':raise SystemExit(main())
