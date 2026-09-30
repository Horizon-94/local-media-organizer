#!/usr/bin/env python3
"""Benchmark-only task timing around unmodified production stage functions.

Uses the bundled generic runner and its safety contract. No model/backend or
task parameters are changed. Each spawned worker writes its own timing file.
"""
import concurrent.futures
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

TARGETS = {"worker_predict_one", "infer_one"}
_starts = {}


def profile(frame, event, arg):
    if frame.f_code.co_name not in TARGETS:
        return
    if Path(frame.f_code.co_filename).name != os.environ.get("BENCHMARK_TRACE_STAGE"):
        return
    key = id(frame)
    if event == "call":
        _starts[key] = time.time()
    elif event == "return" and key in _starts:
        start = _starts.pop(key)
        record = {"pid": os.getpid(), "function": frame.f_code.co_name,
                  "started_epoch": start, "finished_epoch": time.time(),
                  "scope": "production_task_call_not_gpu_kernel"}
        root = Path(os.environ["BENCHMARK_TRACE_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        with (root / f"worker-{os.getpid()}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")


def traced_initializer(original, args):
    sys.setprofile(profile)
    if original:
        original(*args)


class TracedPool(concurrent.futures.ProcessPoolExecutor):
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        super().__init__(*args, initializer=traced_initializer,
                         initargs=(initializer, initargs), **kwargs)


def main():
    official = Path("/Applications/本地数据库 1.2.8.app/Contents/Resources/Pipeline/scripts/04_media_archive_app/run_generic_pipeline_stage.py")
    spec = importlib.util.spec_from_file_location("bundled_generic_benchmark_runner", official)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.build_parser().parse_args()
    os.environ["BENCHMARK_TRACE_STAGE"] = args.script.name
    os.environ["BENCHMARK_TRACE_DIR"] = str(Path(args.stage_args[args.stage_args.index("--out") + 1]) / "benchmark-task-timing")
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.setprofile(profile)
    stage_args = args.stage_args[1:] if args.stage_args[:1] == ["--"] else args.stage_args
    return module.execute(args.script, args.allowed_output_root, args.allowed_source_root, stage_args)


if __name__ == "__main__":
    raise SystemExit(main())
