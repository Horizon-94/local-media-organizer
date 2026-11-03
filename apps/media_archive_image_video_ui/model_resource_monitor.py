"""Exact-PID, cheap macOS resource sampling (no vmmap/stack sampling).

proc_pid_rusage is the public libproc API. Its phys_footprint includes charged
unified-memory allocations, unlike RSS. Unavailable measurements remain None.
No model libraries, SQLite connection or production shutdown dependency here.
"""
from __future__ import annotations
import ctypes
import sys
import threading
import time
from collections import deque


class RUsageV2(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins',
        'wired_size', 'resident_size', 'phys_footprint', 'proc_start_abstime',
        'proc_exit_abstime', 'child_user_time', 'child_system_time',
        'child_pkg_idle_wkups', 'child_interrupt_wkups', 'child_pageins',
        'child_elapsed_abstime', 'diskio_bytesread', 'diskio_byteswritten')]


def native_sample(pid):
    if sys.platform != 'darwin':
        return None
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    call = lib.proc_pid_rusage
    call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    call.restype = ctypes.c_int
    usage = RUsageV2()
    if call(int(pid), 2, ctypes.byref(usage)) != 0:
        return None
    result = {name: getattr(usage, name) for name in (
        'user_time', 'system_time', 'resident_size', 'phys_footprint',
        'proc_start_abstime', 'diskio_bytesread', 'diskio_byteswritten')}
    # libproc CPU times are mach absolute units, NOT nanoseconds on M1.
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    base = Timebase()
    system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    system.mach_timebase_info(ctypes.byref(base))
    if not base.denom:
        return None
    for name in ('user_time', 'system_time'):
        result[name] = result[name] * base.numer // base.denom
    return result


class ModelResourceMonitor:
    STATES = {'LOADING', 'READY', 'INFERENCE', 'IDLE', 'CLOSED'}

    def __init__(self, sampler=native_sample, clock=time.monotonic):
        self.sampler, self.clock = sampler, clock
        self.workers = {}
        self.lifecycle_samples = deque(maxlen=4096)
        self.stage_footprints = {}
        self._lock = threading.RLock()

    def register(self, stage, worker_id, pid):
        with self._lock:
            if worker_id in self.workers:
                raise ValueError('worker_identity_already_registered')
            self.workers[worker_id] = dict(stage=stage, worker_id=worker_id, pid=int(pid),
                state='LOADING', started_at=time.time(), cpu_percent=None,
                rss_bytes=None, phys_footprint=None, peak_footprint=None,
                process_birth=None, sampled_at=None, _previous=None)
        record=self.sample(worker_id)
        with self._lock:self.lifecycle_samples.append(record)
        return record

    def transition(self, worker_id, state):
        if state not in self.STATES:
            raise ValueError('invalid_worker_state')
        # Lifecycle samples happen before changing CLOSED while PID is valid.
        record = self.sample(worker_id)
        with self._lock:
            self.workers[worker_id]['state'] = state
            record['state'] = state
            self.lifecycle_samples.append(record)
            value=record['phys_footprint']
            if value is not None and state in ('READY','IDLE'):
                stage=self.stage_footprints.setdefault(record['stage'],{'EWMA':value,'historical_peak':value})
                stage['EWMA']=.2*value+.8*stage['EWMA']
                stage['historical_peak']=max(stage['historical_peak'],value)
        return record

    def sample(self, worker_id):
        with self._lock:
            worker = self.workers[worker_id]
            now = self.clock()
            try:
                value = self.sampler(worker['pid']) if worker['state'] != 'CLOSED' else None
            except (OSError, ValueError):
                value = None
            if value is not None:
                birth = value['proc_start_abstime']
                if worker['process_birth'] not in (None, birth):
                    value = None  # PID reused; never attribute another process.
                else:
                    worker['process_birth'] = birth
            if value is None:
                worker.update(cpu_percent=None, rss_bytes=None, phys_footprint=None)
            else:
                cpu_ns = value['user_time'] + value['system_time']
                previous = worker['_previous']
                cpu = None
                if previous and now > previous[0] and cpu_ns >= previous[1]:
                    cpu = (cpu_ns - previous[1]) / 1e9 / (now - previous[0]) * 100
                footprint = value['phys_footprint']
                worker.update(cpu_percent=cpu, rss_bytes=value['resident_size'],
                    phys_footprint=footprint, peak_footprint=max(worker['peak_footprint'] or 0, footprint),
                    _previous=(now, cpu_ns))
            worker['sampled_at'] = time.time()
            return {k: v for k, v in worker.items() if not k.startswith('_')}

    def sample_all(self):
        with self._lock:
            ids = list(self.workers)
        return [self.sample(key) for key in ids]
