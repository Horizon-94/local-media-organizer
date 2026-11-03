"""Own UI helper subprocess groups; no independent daemon or processing driver."""
import os,sys,runpy,threading,signal,time,json,io
from contextlib import redirect_stdout,nullcontext
from pathlib import Path
if os.getpgrp()!=os.getpid():os.setsid()
owner=os.getppid();done=threading.Event();started=time.monotonic();deadline=started+45
shutdown_reason="HELPER_CANCELLED"
sql_guard=None
# Parent loss and bounded helper lifetime cannot leave a model/search subprocess orphan.
def watch():
 global shutdown_reason
 while not done.wait(.5):
  if os.getppid()!=owner or time.monotonic()>deadline:
   shutdown_reason="HELPER_OWNER_LOST" if os.getppid()!=owner else "HELPER_DEADLINE_EXCEEDED"
   os.killpg(os.getpgrp(),signal.SIGTERM);return
threading.Thread(target=watch,daemon=True).start()
def shutdown(*_):
 if sql_guard is not None:sql_guard.cancel()
 signal.signal(signal.SIGTERM,signal.SIG_IGN);os.killpg(os.getpgrp(),signal.SIGTERM)
 message="搜索或读取超过现有时限，未完成；处理进度未重置" if shutdown_reason=="HELPER_DEADLINE_EXCEEDED" else "本次操作已取消"
 if sql_guard is None:print(json.dumps({"status":"FAIL","error_name":message,"error_reason":shutdown_reason,"exit_code":143,"diagnostic":"helper pid="+str(os.getpid())+" elapsed="+str(round(time.monotonic()-started,3))},ensure_ascii=False),flush=True)
 raise SystemExit(143)
signal.signal(signal.SIGTERM,shutdown)
script=sys.argv.pop(1);sys.argv[0]=script
if '--config' in sys.argv:
 index=sys.argv.index('--config')+2
 command=sys.argv[index] if index<len(sys.argv) else ''
 if command in {'search','snapshot','video-content-timeline','spoken-content','person-cluster','person-clusters','library-catalog'}:
  import build151_sqlite_interruptible as sql_guard
  sql_guard.install()
buffer=io.StringIO() if sql_guard is not None else None
try:
 with redirect_stdout(buffer) if buffer is not None else nullcontext():
  try:runpy.run_path(script,run_name='__main__')
  except Exception as error:
   print(json.dumps({'status':'FAIL','error':str(error),'helper':script,'pid':os.getpid()},ensure_ascii=False),flush=True)
   raise SystemExit(2)
finally:
 done.set();signal.signal(signal.SIGTERM,signal.SIG_IGN);os.killpg(os.getpgrp(),signal.SIGTERM)
 if buffer is not None:
  if sql_guard.is_cancelled():
   incomplete=shutdown_reason=='HELPER_DEADLINE_EXCEEDED'
   print(json.dumps({'status':'INCOMPLETE' if incomplete else 'CANCELLED','reason_code':shutdown_reason,'error_name':'读取超过现有时限，未完成' if incomplete else '本次操作已取消','error_reason':shutdown_reason,'exit_code':143},ensure_ascii=False),flush=True)
   raise SystemExit(143)
  print(buffer.getvalue(),end='',flush=True)
