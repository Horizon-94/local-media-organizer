"""Capability-checked measured devices for NEW task materialization only."""
import functools
import os
import platform
import subprocess
import sys


@functools.lru_cache(maxsize=16)
def mps_available(python):
    if not python:return False
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    try:
        result=subprocess.run([python,'-c','import torch; print("MPS_AVAILABLE" if torch.backends.mps.is_available() else "MPS_UNAVAILABLE")'],
            capture_output=True,text=True,timeout=10,env=env)
        return result.returncode==0 and 'MPS_AVAILABLE' in result.stdout.splitlines()
    except (OSError,subprocess.TimeoutExpired):return False


def default_devices(runtime):
    if sys.platform!='darwin' or platform.machine()!='arm64':return {'yoloe':'cpu','openclip':'cpu'}
    python=runtime.get('python') or {}
    return {stage:'mps' if mps_available(python.get(interpreter)) else 'cpu'
            for stage,interpreter in (('yoloe','yolo'),('openclip','visual'))}
