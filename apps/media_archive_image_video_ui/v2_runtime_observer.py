"""Opt-in owned-process stack capture; no scheduler/evidence mutation."""
import json
import os
from pathlib import Path
import signal
import sys
import threading
import traceback


def register_live_stack_signal(driver,task):
    trace=task.get('scheduler_trace_path')
    if not trace or task.get('fixture_handlers') or not hasattr(signal,'SIGUSR2') or threading.current_thread() is not threading.main_thread():return
    path=Path(trace).with_name(Path(trace).name+'.'+str(os.getpid())+'.live-stack')
    handle=path.open('a')
    def capture(_signum,_frame):
        # Run at a Python safe point with the GIL, NOT inside faulthandler's
        # asynchronous native frame walker (which crashed this bundled 3.12
        # while another thread was executing SQLite/lineage callbacks).
        # Keep strong frame references until formatting finishes.
        try:
            frames=sys._current_frames()
            names={t.ident:t.name for t in threading.enumerate()}
            chunks=[]
            for ident,frame in frames.items():
                chunks.append(f'Thread {ident} ({names.get(ident,"unknown")}):\n')
                chunks.extend(traceback.format_stack(frame))
            handle.write(''.join(chunks));handle.flush()
        except Exception:
            # Observability cannot fail or change the production workflow.
            pass
    signal.signal(signal.SIGUSR2,capture)
    driver._live_stack_handle=handle
    metadata={'pid':os.getpid(),'signal':int(signal.SIGUSR2),'stack_log_path':str(path),
        'run_id':driver.owner['scheduler_run_id'],'epoch':driver.owner['execution_epoch'],
        'ready':True,'capture_only':True,'capture_method':'python_safe_point'}
    path.with_suffix('.stack-signal.json').write_text(json.dumps(metadata,sort_keys=True))
