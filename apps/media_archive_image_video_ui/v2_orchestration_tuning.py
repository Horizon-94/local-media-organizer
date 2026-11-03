"""Choose the first throughput plateau from a matching measured profile."""
import json
import math
import os
import platform
from pathlib import Path


def choose_cheap_batch(profile,hardware):
    if profile.get('hardware')!=hardware or not profile.get('individual_evidence_semantics_verified') or not profile.get('source_advancement_during_batch_regression_verified'):
        return 1
    rows=profile.get('results',[])
    if not rows or any(type(r.get('batch')) is not int or r['batch'] not in (1,2,4,8,16,32)
        or not isinstance(r.get('jobs_per_second'),(float,int)) or not math.isfinite(r['jobs_per_second'])
        or r['jobs_per_second']<=0 for r in rows):return 1
    peak=max(r['jobs_per_second'] for r in rows)
    return min(r['batch'] for r in rows if r['jobs_per_second']>=peak*.95)


def measured_cheap_batch():
    path=Path(__file__).resolve().parents[2]/'configs/v2_orchestration_tuning_v1.json'
    try:
        profile=json.loads(path.read_text())
        hardware={'platform':platform.system(),'machine':platform.machine(),'logical_cores':os.cpu_count()}
        return choose_cheap_batch(profile,hardware)
    except (OSError,ValueError,KeyError,TypeError):return 1
