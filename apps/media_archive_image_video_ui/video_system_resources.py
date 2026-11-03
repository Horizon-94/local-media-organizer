"""Bounded external system metrics. UNKNOWN stays UNKNOWN; no vmmap."""
import ctypes
import re
import shutil
import subprocess
import sys
import time


def command(args):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=2)
        return result.stdout if result.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):return None


def swap_bytes(value):
    if value is None:return None
    match=re.search(r'used\s*=\s*([\d.]+)([KMGT])',value)
    return int(float(match[1])*1024**('KMGT'.index(match[2])+1)) if match else None


class SystemResources:
    def __init__(self):
        self.previous_cpu=None;self.swap_start=None;self.swap_history=[];self.cached=None;self.last=0
        self.swap_timed_history=[]

    def sample(self,directory):
        now=time.monotonic()
        if self.cached is not None and now-self.last<5:return self.cached
        cpu=None
        if sys.platform=='darwin':
            lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            lib.mach_host_self.restype=ctypes.c_uint32
            values=(ctypes.c_uint32*4)();count=ctypes.c_uint32(4)
            lib.host_statistics.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
            if lib.host_statistics(lib.mach_host_self(),3,values,ctypes.byref(count))==0:
                current=list(values)
                if self.previous_cpu:
                    delta=[(a-b)%(2**32) for a,b in zip(current,self.previous_cpu)]
                    total=sum(delta)
                    if total:cpu={name:100*value/total for name,value in zip(('user','system','idle','nice'),delta)}
                self.previous_cpu=current
        swap=swap_bytes(command(['/usr/sbin/sysctl','vm.swapusage']))
        pressure=command(['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level'])
        level=int(pressure.strip()) if pressure and pressure.strip().isdigit() else None
        if swap is not None:
            if self.swap_start is None:self.swap_start=swap
            self.swap_history.append(swap);self.swap_history=self.swap_history[-4:]
            self.swap_timed_history.append((time.time(),swap));self.swap_timed_history=self.swap_timed_history[-4:]
        # Ignore historical absolute swap. Only sustained growth blocks new work.
        growing=len(self.swap_history)==4 and all(b-a>=64*1024**2 for a,b in zip(self.swap_history,self.swap_history[1:]))
        rate=None
        if len(self.swap_timed_history) >= 2:
            start,value0=self.swap_timed_history[0];end,value1=self.swap_timed_history[-1]
            if end>start:rate=max(0,(value1-value0)/(end-start))
        self.cached={'timestamp':time.time(),'system_cpu':cpu,'vm_stat':command(['/usr/bin/vm_stat']),
            'memory_pressure_level':level,'memory_pressure_query':command(['/usr/bin/memory_pressure','-Q']),
            'swap_used_bytes':swap,'swap_delta_bytes':swap-self.swap_start if swap is not None else None,
            'disk_free_bytes':shutil.disk_usage(directory).free,
            'sustained_swap_growth':growing,'swap_growth_rate_bytes_per_second':rate,
            'blocked_reason':'MEMORY_PRESSURE_CRITICAL' if level==4 else 'SUSTAINED_SWAP_GROWTH' if growing else None}
        self.last=now
        return self.cached
