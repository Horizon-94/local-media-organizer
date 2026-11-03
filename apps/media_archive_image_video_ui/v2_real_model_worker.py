"""JSON-line resident worker used by the normal V2 real-handler boundary."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import threading
import time

def _watch_query_owner(owner):
    """Query-only children have no durable output and must not outlive UI cancel."""
    while os.getppid()==owner:
        time.sleep(0.5)
    os._exit(0)

from . import query_trace as trace
# Model startup is attributed to the creating query; processing stays untraced.
_boot_config=json.loads(sys.argv[sys.argv.index('--config-json')+1]) if '--config-json' in sys.argv else {}
if _boot_config.get('query_owner_pid') and _boot_config.get('_query_trace_id'):trace.begin(_boot_config['_query_trace_id'])
with trace.span('worker_module_import'):
    from .v2_model_workers import create_handler


def _jsonable(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--config-json", required=True)
    args = parser.parse_args()
    config = json.loads(args.config_json)
    config.pop('_query_trace_id',None)
    ready_ack = config.pop('_worker_ready_ack', False)
    owner=config.pop('query_owner_pid',None)
    if owner is not None:
        if args.stage not in ('embedding','openclip') or type(owner) is not int or owner!=os.getppid():
            raise ValueError('query_worker_owner_invalid')
        threading.Thread(target=_watch_query_owner,args=(owner,),daemon=True).start()
    with contextlib.redirect_stdout(sys.stderr):
        with trace.span('model_initialize'):
            handler = create_handler(args.stage, config)
    if ready_ack:
        print(json.dumps({'ok': True, 'worker_state': 'READY'}), flush=True)
    for line in sys.stdin:
        payload = json.loads(line)
        if owner is not None and '_query_id' in payload:
            qid=payload.pop('_query_id')
            if qid and qid!=trace.current_id():trace.begin(qid)
        if payload.get('request_kind')=='v2_worker_shutdown':
            # No handler is running here; all prior results were flushed.
            # Paddle SIGTERM/finalizer teardown crashes on this runtime.
            # The stateless inference child owns no DB transaction; OS exit
            # safely reclaims its native threads and model memory.
            print(json.dumps({'ok':True,'shutdown':'ack'}),flush=True)
            if args.stage=='ocr':os._exit(0)
            return
        try:
            with contextlib.redirect_stdout(sys.stderr):
                if payload.get('request_kind')=='query_prepare':
                    if owner is None:raise ValueError('query_prepare_requires_owner')
                    result={'query_ready':True}
                elif payload.get('request_kind')=='v2_true_batch':
                    batch=getattr(handler,'batch',None)
                    if batch is None:raise ValueError('true_batch_not_supported:'+args.stage)
                    result=batch(payload['items'])
                else:
                    with trace.span('query_vector_generate'):result = handler(payload)
                if owner is not None and isinstance(result,dict):result={**result,'_query_trace':trace.take()}
            print(json.dumps({"ok": True, "result": _jsonable(result)}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}), flush=True)


if __name__ == "__main__":
    main()
