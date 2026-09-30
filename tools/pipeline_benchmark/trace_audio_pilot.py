#!/usr/bin/env python3
"""Observe the production MLX Whisper call, including load and inference.

No replacement transcription implementation. The installed audio pilot runs
unchanged via runpy; timing output is restricted to its benchmark item output.
"""
import json
import os
import runpy
import sys
import time
from pathlib import Path

starts={}


def trace(frame,event,arg):
    if frame.f_code.co_name!='transcribe' or frame.f_globals.get('__name__')!='mlx_whisper.transcribe': return
    key=id(frame)
    if event=='call':
        starts[key]=time.time()
        output=Path(sys.argv[sys.argv.index('--output-dir')+1])/'benchmark-whisper-events.jsonl'
        with output.open('a') as f:
            f.write(json.dumps({'event':'FORMAL_WHISPER_BACKEND_START','timestamp_epoch':starts[key],'pid':os.getpid(),'source':'exact_function_entry'})+'\n')
    elif event=='return' and key in starts:
        output=Path(sys.argv[sys.argv.index('--output-dir')+1])/'benchmark-whisper-timing.jsonl'
        # Some Whisper versions do not expose mx in transcribe's globals.
        # Read the already-loaded module; do not import/load another backend.
        mx=frame.f_globals.get('mx') or sys.modules.get('mlx.core')
        memory={}
        for name in ('get_active_memory','get_cache_memory','get_peak_memory'):
            try:
                getter=getattr(mx,name,None) or getattr(getattr(mx,'metal',None),name,None)
                memory[name]=int(getter()) if getter else None
            except (AttributeError,RuntimeError): memory[name]=None
        with output.open('a') as f:
            f.write(json.dumps({'pid':os.getpid(),'started_epoch':starts.pop(key),'finished_epoch':time.time(),
                                'scope':'formal_mlx_transcribe_load_and_inference',
                                'mlx_memory_bytes_at_transcribe_return':memory})+'\n')


if __name__=='__main__':
    official='/Applications/本地数据库 1.2.8.app/Contents/Resources/Pipeline/scripts/04_media_archive_app/run_audio_search_pilot_v1.py'
    sys.setprofile(trace)
    runpy.run_path(official,run_name='__main__')
