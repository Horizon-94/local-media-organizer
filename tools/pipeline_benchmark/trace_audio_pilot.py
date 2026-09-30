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
    if event=='call': starts[key]=time.time()
    elif event=='return' and key in starts:
        output=Path(sys.argv[sys.argv.index('--output-dir')+1])/'benchmark-whisper-timing.jsonl'
        with output.open('a') as f:
            f.write(json.dumps({'pid':os.getpid(),'started_epoch':starts.pop(key),'finished_epoch':time.time(),
                                'scope':'formal_mlx_transcribe_load_and_inference'})+'\n')


if __name__=='__main__':
    official='/Applications/本地数据库 1.2.8.app/Contents/Resources/Pipeline/scripts/04_media_archive_app/run_audio_search_pilot_v1.py'
    sys.setprofile(trace)
    runpy.run_path(official,run_name='__main__')
