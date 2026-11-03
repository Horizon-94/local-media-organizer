"""Resource-vector admission only; never loads models or changes evidence."""
import re
import subprocess
import threading
import time


def reclaimable_memory_mb(statistics):
    page=re.search(r'page size of (\d+) bytes',statistics)
    if not page:raise ValueError('vm_stat_page_size_missing')
    pages=0
    # Purgeable pages overlap other classes: do not count them twice.
    for name in ('free','inactive','speculative'):
        match=re.search(r'Pages '+name+r':\s*(\d+)\.',statistics)
        if not match:raise ValueError('vm_stat_'+name+'_missing')
        pages+=int(match[1])
    return pages*int(page[1])/1024/1024


class CachedSystemMemory:
    """Bounded system probes, cached across admission checks for one second."""
    def __init__(self, ttl=1.0, runner=subprocess.run):
        self.ttl=ttl;self.runner=runner;self.lock=threading.Lock()
        self.last=0.;self.value=None

    def __call__(self):
        with self.lock:
            if self.value is not None and time.monotonic()-self.last<self.ttl:return dict(self.value)
            try:
                def run(args):
                    return self.runner(args,capture_output=True,text=True,timeout=3,check=True).stdout
                statistics=run(['/usr/bin/vm_stat'])
                system=run(['/usr/sbin/sysctl','hw.memsize','vm.swapusage','kern.memorystatus_vm_pressure_level'])
                physical=re.search(r'hw.memsize:\s*(\d+)',system)
                level=re.search(r'kern.memorystatus_vm_pressure_level:\s*(\d+)',system)
                used=re.search(r'used = ([\d.]+)M',system)
                if not physical or not level or not used:raise ValueError('memory_system_sample_missing')
                physical=int(physical[1])/1024/1024
                available=min(physical,reclaimable_memory_mb(statistics))
                value={'healthy':True,'free_percent':available*100/physical,'physical_mb':physical,
                       'available_mb':available,'available_source':'vm_stat_free_inactive_speculative_estimate',
                       'pressure_level':int(level[1]),'swap_used_mb':float(used[1]),
                       'sampled_at':time.time()}
            except Exception as exc:
                value={'healthy':False,'free_percent':0.,'available_mb':0.,
                       'probe_error':type(exc).__name__+':'+str(exc),'sampled_at':time.time()}
            self.last=time.monotonic();self.value=value;return dict(value)


def profile(policy,kind):
    budget=policy.get('resources',{})
    profiles=budget.get('profiles',{})
    p=dict(profiles.get(kind,{}))
    p.setdefault('cpu_units',1.)
    p.setdefault('memory_mb',0.)
    return p


def decision(policy,kind,active,pressure):
    """Keep cheap/IO/fairness pools intact; a separate bounded CPU-model pool."""
    budget=policy.get('resources',{})
    if not budget.get('enabled'):return None
    p=profile(policy,kind);active=[dict(r) for r in active]
    models=budget.get('cpu_model_classes',[])
    pressure=pressure if isinstance(pressure,dict) else {'free_percent':float(pressure)}
    if kind not in budget.get('profiles',{}):return None
    if p['memory_mb'] and not pressure.get('healthy',True):
        return {'reason':'memory_probe_unavailable','capacity':0,'holders':active,'pressure':pressure}
    free=float(pressure.get('free_percent',0))
    physical=float(pressure.get('physical_mb',budget.get('fixture_physical_mb',32768)))
    available=float(pressure.get('available_mb',physical*free/100))
    if kind in models:
        limit=float(budget.get('cpu_model_units',4))
        if free<float(budget.get('high_memory_free_percent',40)):
            limit=min(limit,float(budget.get('high_memory_cpu_model_units',2)))
        holders=[r for r in active if r['workload_class'] in models]
        used=sum(float(r.get('resource_cpu_units') or profile(policy,r['workload_class'])['cpu_units']) for r in holders)
        if used+float(p['cpu_units'])>limit:
            return {'reason':'cpu_model_budget','capacity':limit,'holders':holders,
                    'requested_cpu_units':p['cpu_units'],'active_cpu_units':used,'pressure':pressure}
    reserve=max(float(budget.get('minimum_free_mb',4096)),physical*float(budget.get('reserve_percent',20))/100)
    reserved=sum(float(r.get('resource_memory_mb') or 0) for r in active)
    if p['memory_mb'] and available-reserved-float(p['memory_mb'])<reserve:
        return {'reason':'memory_headroom','capacity':max(0,available-reserve),'holders':active,
                'requested_memory_mb':p['memory_mb'],'reserved_memory_mb':reserved,'reserve_mb':reserve,'pressure':pressure}
    return None
