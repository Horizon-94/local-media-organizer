"""Owned, cancellable full DB audit. Not an interactive snapshot request."""
import os,sys,sqlite3,threading,signal,runpy
from pathlib import Path
if os.getpgrp()!=os.getpid():os.setsid()
owner=os.getppid();cancelled=threading.Event();done=threading.Event();original=sqlite3.connect
def connect(*a,**kw):
 c=original(*a,**kw);c.set_progress_handler(lambda:int(cancelled.is_set()),10000);return c
sqlite3.connect=connect
signal.signal(signal.SIGTERM,lambda *_:cancelled.set())
def watch():
 while not done.wait(.5):
  if os.getppid()!=owner:cancelled.set();return
threading.Thread(target=watch,daemon=True).start()
sys.argv[0]=str(Path(__file__).resolve().with_name('python_bridge.py'))
try:runpy.run_path(sys.argv[0],run_name='__main__')
finally:done.set()
