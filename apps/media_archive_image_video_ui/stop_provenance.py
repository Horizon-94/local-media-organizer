"""Append-only stop evidence. No database writes or automatic recovery."""
from contextlib import contextmanager
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path


def record(root, event, origin, reason_code, **detail):
    root = Path(root)
    error = detail.get('error')
    trace = detail.get('traceback', '')
    if origin == 'PROCESSING_FATAL':
        if 'sqlite3.' in trace or any(s in str(error).lower() for s in
                ('database is locked', 'database disk image', 'rollback', 'commit ambiguity')):
            origin = 'DB_FATAL'
        elif any(s in str(error).lower() for s in ('worker died', 'worker exited', 'broken pipe')):
            origin = 'WORKER_FATAL'
    try:
        state_before = json.loads((root.parent/'run-config.json').read_text()).get('status', 'UNKNOWN')
    except (OSError, ValueError):
        state_before = 'UNKNOWN'
    row = dict(at=time.time(), event=event, origin=origin,
               reason_code=reason_code, pid=os.getpid(), ppid=os.getppid(),
               thread=threading.current_thread().name,
               callsite=traceback.format_stack(limit=8)[:-1], **detail)
    row.update(STOP_REQUESTED_AT=row['at'], STOP_ORIGIN=origin,
               STOP_REASON_CODE=reason_code, STOP_CALLSITE=row['callsite'],
               LAST_ERROR=error, TASK_STATE_BEFORE=state_before)
    data = (json.dumps(row, ensure_ascii=False, default=str) + '\n').encode()
    try:
        path = Path(root) / 'stop-events.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError('short stop telemetry write')
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        # Evidence failure must never prevent a required safety stop.
        print('STOP_TELEMETRY_WRITE_FAILED ' + repr(error), file=sys.stderr, flush=True)
        print(data.decode(), file=sys.stderr, flush=True)
    return row


def signal_origin(root, signum):
    """Only attribute a signal to UI/Quit when a matching durable intent exists."""
    try:
        rows = (Path(root) / 'stop-events.jsonl').read_text().splitlines()
        for raw in reversed(rows):
            row = json.loads(raw)
            if row['event'] == 'STOP_INTENT' and row.get('target_pid') == os.getpid():
                if 0 <= time.time() - row['at'] < 30:
                    return row['origin'], row['reason_code']
                break
    except (OSError, ValueError, KeyError):
        pass
    return 'SYSTEM_SIGNAL', 'SIGNAL_' + str(signum)


class StopEvent(threading.Event):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)
        self._request_lock = threading.RLock()
        self._cutoff_callbacks=[]
        self.cutoff_at=None
        self._critical=threading.local()
        self._pending_request=None

    @contextmanager
    def new_work(self,kind):
        # The check and ownership transition share the Stop acceptance lock.
        # Never hold this lock during inference, source hashing or writer drain.
        with self._request_lock:
            self._critical.depth=getattr(self._critical,'depth',0)+1
            try:
                allowed=not self.is_set()
                yield allowed
            finally:
                self._critical.depth-=1
                if not self._critical.depth and self._pending_request is not None:
                    origin,reason,detail,requested_at=self._pending_request
                    self._pending_request=None
                    self.request(origin,reason,OWNER_TRANSITION_DEFERRED_MS=(time.time()-requested_at)*1000,**detail)

    def work_started(self,kind,**detail):
        # Caller owns new_work's atomic admission fence. This is an actual
        # accepted ownership receipt, not a prefetch or eligibility observation.
        row=dict(at=time.time(),event='NEW_WORK_OWNED',kind=kind,**detail)
        with (self.root/'control-plane-events.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')

    def on_cutoff(self,callback):
        with self._request_lock:
            if self.is_set():callback()
            else:self._cutoff_callbacks.append(callback)

    def request(self, origin, reason_code, **detail):
        with self._request_lock:
            if self.is_set():return
            if getattr(self._critical,'depth',0):
                # Python signal handlers may reenter an RLock on the owning
                # thread. Linearize acceptance at that transition's exit.
                if self._pending_request is None:self._pending_request=(origin,reason_code,detail,time.time())
                return
            # Fail closed before telemetry/fsync or any DB work.
            super().set()
            self.cutoff_at=time.time()
            for callback in self._cutoff_callbacks:callback()
            record(self.root,'STOP_REQUESTED',origin,reason_code,
                   CONTROL_PLANE_CUTOFF_AT=self.cutoff_at,NEW_ADMISSION_CUTOFF_AT=self.cutoff_at,NEW_SELECTION_CUTOFF_AT=self.cutoff_at,NEW_MODEL_CLAIM_CUTOFF_AT=self.cutoff_at,NEW_CHILD_SPAWN_CUTOFF_AT=self.cutoff_at,**detail)

    def set(self):
        stack = traceback.extract_stack(limit=4)
        cleanup = any(frame.name == 'close' for frame in stack)
        self.request('RUNTIME_CLEANUP' if cleanup else 'UNKNOWN',
                     'RUNTIME_CLOSE' if cleanup else 'UNCLASSIFIED_EVENT_SET')

# Explicit call-site helpers, never a process-wide subprocess monkeypatch.
_RUNTIME_STOPS={}


def register_runtime_stop(stop):
    _RUNTIME_STOPS[str(stop.root.resolve())]=stop


def _resolve_stop(value):
    if hasattr(value,'is_set'):return value
    return _RUNTIME_STOPS.get(str(Path(value).resolve()))


def child_spawn(scope,command,**kwargs):
    import subprocess
    from contextlib import nullcontext
    from .video_stream_ledger import StaleResult
    stop=_resolve_stop(scope)
    gate=stop.new_work('child_spawn') if hasattr(stop,'new_work') else nullcontext(not (stop and stop.is_set()))
    with gate as allowed:
        if not allowed:raise StaleResult('STOP_BEFORE_CHILD_SPAWN')
        spawn_started_at=time.time()
        process=subprocess.Popen(command,**kwargs)
        process._build151_spawn_at=spawn_started_at
        process._build151_spawn_return_at=time.time()
    if stop and hasattr(stop,'root'):
        # A receipt includes executable/start identity, never a bare PID.
        try:
            result=subprocess.run(['ps','-p',str(process.pid),'-o','lstart=','-o','comm='],capture_output=True,text=True,timeout=2)
            fields=result.stdout.strip().split(None,5)
            identity={'pid':process.pid,'start_identity':' '.join(fields[:5]),'executable':fields[5]} if len(fields)==6 and result.returncode==0 else None
        except (OSError,subprocess.SubprocessError):
            identity=None  # Observability cannot orphan a successfully spawned child.
        row=dict(event='CHILD_SPAWN',at=spawn_started_at,spawn_return_at=process._build151_spawn_return_at,pid=process.pid,command=command,identity=identity)
        try:
            with (stop.root/'owned-child-spawns.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        except OSError:pass
    return process


def child_run(scope,command,*,input=None,capture_output=False,timeout=None,check=False,**kwargs):
    import subprocess
    if input is not None:
        if kwargs.get('stdin') is not None:raise ValueError('stdin and input arguments may not both be used')
        kwargs['stdin']=subprocess.PIPE
    if capture_output:
        if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:raise ValueError('capture_output conflicts with stdout/stderr')
        kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    process=child_spawn(scope,command,**kwargs)
    try:stdout,stderr=process.communicate(input,timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill();error.output,error.stderr=process.communicate();raise
    except BaseException:
        process.kill();process.wait();raise
    result=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
    if check:result.check_returncode()
    return result
