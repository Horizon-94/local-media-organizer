#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-1 Process Tree Resource Monitor
======================================

Purpose:
- Monitor one running local process tree by PID.
- Record CPU cores, RSS memory, swap, system disk read/write deltas.
- Designed for Stop03-1 visual embedding / YOLOE subprocess monitoring.
- Read-only: does not touch source media or model outputs.

Outputs:
    telemetry/<label>_resource_samples.csv
    telemetry/<label>_resource_summary.json

Usage:
    python3 stop03_1_monitor_process_tree.py \
      --pid 12345 \
      --label yoloe4 \
      --csv /path/to/yoloe4_resource_samples.csv \
      --json /path/to/yoloe4_resource_summary.json
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import psutil

SCRIPT_VERSION = "stop03_1_monitor_process_tree_v1_20260708"


def proc_alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def process_tree(pid: int, cache: dict[int, psutil.Process]) -> list[psutil.Process]:
    try:
        root = cache.setdefault(pid, psutil.Process(pid))
        procs = [root] + root.children(recursive=True)
    except Exception:
        return []

    out: list[psutil.Process] = []
    for p in procs:
        try:
            cache.setdefault(p.pid, p)
            out.append(cache[p.pid])
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--assumed-cpu-cores", type=float, default=10.0)
    args = ap.parse_args()

    out_csv = Path(args.csv)
    out_json = Path(args.json)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "timestamp",
        "script_version",
        "label",
        "elapsed_seconds",
        "process_count",
        "process_cpu_cores",
        "process_cpu_percent_of_assumed_cores",
        "process_rss_mb",
        "process_read_mb",
        "process_write_mb",
        "system_cpu_percent",
        "system_memory_percent",
        "swap_used_mb",
        "system_disk_read_mb_delta",
        "system_disk_write_mb_delta",
        "system_disk_read_mbps",
        "system_disk_write_mbps",
    ]

    cache: dict[int, psutil.Process] = {}
    start = time.perf_counter()

    disk0 = psutil.disk_io_counters()
    disk0_read = getattr(disk0, "read_bytes", 0) if disk0 else 0
    disk0_write = getattr(disk0, "write_bytes", 0) if disk0 else 0

    for p in process_tree(args.pid, cache):
        try:
            p.cpu_percent(interval=None)
        except Exception:
            pass
    psutil.cpu_percent(interval=None)

    max_cpu_cores = 0.0
    max_rss_mb = 0.0
    max_swap_mb = 0.0
    max_read_mbps = 0.0
    max_write_mbps = 0.0
    final_read_delta = 0.0
    final_write_delta = 0.0
    final_proc_read = 0.0
    final_proc_write = 0.0
    samples = 0

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        while proc_alive(args.pid):
            time.sleep(args.interval)
            elapsed = max(time.perf_counter() - start, 0.001)

            procs = process_tree(args.pid, cache)
            cpu_sum = 0.0
            rss_sum = 0.0
            proc_read_bytes = 0
            proc_write_bytes = 0

            for p in procs:
                try:
                    cpu_sum += p.cpu_percent(interval=None)
                except Exception:
                    pass
                try:
                    rss_sum += p.memory_info().rss / 1024 / 1024
                except Exception:
                    pass
                try:
                    io = p.io_counters()
                    proc_read_bytes += getattr(io, "read_bytes", 0)
                    proc_write_bytes += getattr(io, "write_bytes", 0)
                except Exception:
                    pass

            cpu_cores = cpu_sum / 100.0
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            disk = psutil.disk_io_counters()

            disk_read_delta = ((getattr(disk, "read_bytes", 0) - disk0_read) / 1024 / 1024) if disk else 0.0
            disk_write_delta = ((getattr(disk, "write_bytes", 0) - disk0_write) / 1024 / 1024) if disk else 0.0
            disk_read_mbps = disk_read_delta / elapsed
            disk_write_mbps = disk_write_delta / elapsed
            swap_mb = sm.used / 1024 / 1024

            row = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "script_version": SCRIPT_VERSION,
                "label": args.label,
                "elapsed_seconds": round(elapsed, 3),
                "process_count": len(procs),
                "process_cpu_cores": round(cpu_cores, 3),
                "process_cpu_percent_of_assumed_cores": round(cpu_cores / args.assumed_cpu_cores * 100, 2),
                "process_rss_mb": round(rss_sum, 3),
                "process_read_mb": round(proc_read_bytes / 1024 / 1024, 3),
                "process_write_mb": round(proc_write_bytes / 1024 / 1024, 3),
                "system_cpu_percent": psutil.cpu_percent(interval=None),
                "system_memory_percent": vm.percent,
                "swap_used_mb": round(swap_mb, 3),
                "system_disk_read_mb_delta": round(disk_read_delta, 3),
                "system_disk_write_mb_delta": round(disk_write_delta, 3),
                "system_disk_read_mbps": round(disk_read_mbps, 3),
                "system_disk_write_mbps": round(disk_write_mbps, 3),
            }

            w.writerow(row)
            f.flush()

            samples += 1
            max_cpu_cores = max(max_cpu_cores, cpu_cores)
            max_rss_mb = max(max_rss_mb, rss_sum)
            max_swap_mb = max(max_swap_mb, swap_mb)
            max_read_mbps = max(max_read_mbps, disk_read_mbps)
            max_write_mbps = max(max_write_mbps, disk_write_mbps)
            final_read_delta = disk_read_delta
            final_write_delta = disk_write_delta
            final_proc_read = proc_read_bytes / 1024 / 1024
            final_proc_write = proc_write_bytes / 1024 / 1024

    summary = {
        "script_version": SCRIPT_VERSION,
        "label": args.label,
        "samples": samples,
        "assumed_cpu_cores": args.assumed_cpu_cores,
        "wall_seconds_monitor": round(time.perf_counter() - start, 3),
        "max_process_cpu_cores": round(max_cpu_cores, 3),
        "max_process_cpu_percent_of_assumed_cores": round(max_cpu_cores / args.assumed_cpu_cores * 100, 2),
        "max_process_rss_mb": round(max_rss_mb, 3),
        "max_swap_used_mb": round(max_swap_mb, 3),
        "process_read_mb_final": round(final_proc_read, 3),
        "process_write_mb_final": round(final_proc_write, 3),
        "system_disk_read_mb_delta_final": round(final_read_delta, 3),
        "system_disk_write_mb_delta_final": round(final_write_delta, 3),
        "max_system_disk_read_mbps": round(max_read_mbps, 3),
        "max_system_disk_write_mbps": round(max_write_mbps, 3),
    }

    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
