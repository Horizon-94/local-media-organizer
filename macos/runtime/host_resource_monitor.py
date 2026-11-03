#!/usr/bin/env python3
"""Live macOS host resource monitor for model/pipeline benchmarks.

Run this from an ordinary macOS Terminal.  It intentionally reports unified
memory and process RSS separately; it never invents per-process GPU memory.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKLOAD_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    # These are deliberately high-confidence tokens.  The monitor is scoped
    # to a benchmark root PID; broad system-wide substring matching caused
    # Apple services and unrelated applications to be mislabelled before.
    ("QWEN", ("/qwen", "qwenvl", "mlx_vlm", "qwen3-vl")),
    ("WHISPER", ("/whisper", "mlx_whisper", "large-v3-turbo")),
    ("YOLOE", ("/yoloe", "ultralytics")),
    ("OPENCLIP", ("openclip", "open_clip")),
    ("OCR", ("paddleocr", "pp-ocr", "/ocr/")),
    ("EMBEDDING", ("/embedding/", "sentence-transformer")),
    ("FFMPEG", ("/ffmpeg",)),
    ("SQLITE", ("/sqlite", "sqlite3")),
]


def run(command: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def classify(command: str) -> str:
    lower = command.lower()
    for kind, needles in WORKLOAD_PATTERNS:
        if any(needle in lower for needle in needles):
            return kind
    # A generic Python worker/orchestrator is not enough evidence to assign a
    # workload.  It remains part of the PID tree but is labelled UNKNOWN.
    return "UNKNOWN"


def ps_rows() -> list[dict[str, Any]]:
    code, out, _ = run([str(Path(__file__).resolve().parents[1]/'Helpers/ps-readonly'), "-axo", "pid=,ppid=,%cpu=,rss=,etime=,command="], 5)
    if code != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 5:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            cpu, rss = float(parts[2]), int(parts[3])
        except ValueError:
            continue
        command = parts[5] if len(parts) > 5 else ""
        rows.append({"pid": pid, "ppid": ppid, "cpu_percent": cpu, "rss_kib": rss,
                     "elapsed": parts[4], "command": command, "type": classify(command)})
    return rows


def process_tree(rows: list[dict[str, Any]], root_pid: int | None) -> list[dict[str, Any]]:
    if root_pid is None:
        return []
    by_pid = {r["pid"]: r for r in rows}
    if root_pid not in by_pid:
        return []
    relevant = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["ppid"] in relevant and row["pid"] not in relevant:
                relevant.add(row["pid"])
                changed = True
    return [r for r in rows if r["pid"] in relevant]


def vm_snapshot() -> dict[str, Any]:
    _, vm, _ = run(["/usr/bin/vm_stat"])
    match = re.search(r"page size of (\d+) bytes", vm)
    page = int(match.group(1)) if match else 4096

    def pages(label: str) -> int | None:
        m = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", vm, re.MULTILINE)
        return int(m.group(1)) * page if m else None

    _, memsize, _ = run(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    total = int(memsize.strip()) if memsize.strip().isdigit() else None
    free = pages("Pages free")
    speculative = pages("Pages speculative") or 0
    _, pressure, _ = run(["/usr/bin/memory_pressure", "-Q"])
    pm = re.search(r"free percentage:\s*([0-9.]+)%", pressure)
    _, swap_text, _ = run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    sm = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap_text)
    swap = None
    if sm:
        swap = int(float(sm.group(1)) * (1024 ** 3 if sm.group(2) == "G" else 1024 ** 2))
    available = free + speculative if free is not None else None
    return {"physical_memory_bytes": total, "memory_available_bytes": available,
            "memory_used_bytes": total - available if total is not None and available is not None else None,
            "compressed_memory_bytes": pages("Pages occupied by compressor"),
            "swap_used_bytes": swap, "memory_pressure_free_percent": float(pm.group(1)) if pm else None}


def cpu_snapshot() -> dict[str, Any]:
    _, out, _ = run(["/usr/bin/top", "-l", "2", "-s", "1", "-n", "0"], 5)
    # Typical macOS line: CPU usage: 12.34% user, 4.56% sys, 83.10% idle
    matches = list(re.finditer(r"CPU usage:\s*([0-9.]+)% user,\s*([0-9.]+)% sys,\s*([0-9.]+)% idle", out))
    m = matches[-1] if len(matches)>=2 else None
    if not m:
        return {"cpu_total_percent": None, "cpu_idle_percent": None, "cpu_source": "N/A"}
    user, system, idle = map(float, m.groups())
    return {"cpu_total_percent": user + system, "cpu_idle_percent": idle, "cpu_source": "top"}


def disk_snapshot() -> dict[str, Any]:
    _, out, _ = run(["/usr/sbin/iostat", "-Id", "-c", "1"], 5)
    nums: list[float] = []
    for line in out.splitlines():
        if re.fullmatch(r"[0-9.\s]+", line.strip()):
            nums = [float(x) for x in line.split()]
    if len(nums) < 3:
        return {"disk_read_cumulative": None, "disk_write_cumulative": None, "disk_source": "N/A"}
    # macOS triplets are KB/transfer, transfer count, combined MB; the first
    # two fields are NOT separate read/write byte counters.
    return {"disk_read_cumulative": None, "disk_write_cumulative": None,
            "disk_combined_cumulative_iostat_mb": sum(nums[2::3]),
            "disk_source": "iostat combined whole-system MB; read/write separation N/A"}


def format_bytes(value: Any) -> str:
    if value is None:
        return "N/A"
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return "N/A"


def format_system(s: dict[str, Any]) -> str:
    return (f"SYSTEM {s['timestamp']} used={format_bytes(s['memory_used_bytes'])} "
            f"avail={format_bytes(s['memory_available_bytes'])} "
            f"compressed={format_bytes(s['compressed_memory_bytes'])} "
            f"swap={format_bytes(s['swap_used_bytes'])} Δswap={format_bytes(s['swap_delta_bytes'])} "
            f"pressure_free={s['memory_pressure_free_percent'] if s['memory_pressure_free_percent'] is not None else 'N/A'}% "
            f"cpu={s['cpu_total_percent'] if s['cpu_total_percent'] is not None else 'N/A'}% "
            f"idle={s['cpu_idle_percent'] if s['cpu_idle_percent'] is not None else 'N/A'}% "
            f"disk_read={s['disk_read_cumulative'] if s['disk_read_cumulative'] is not None else 'N/A'} "
            f"disk_write={s['disk_write_cumulative'] if s['disk_write_cumulative'] is not None else 'N/A'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Host macOS resource monitor")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--output-dir", type=Path, default=Path.cwd())
    ap.add_argument("--duration", type=float, default=0.0, help="0 means until Ctrl-C")
    ap.add_argument("--mark", action="append", default=[], help="write an inferred/manual event marker immediately")
    ap.add_argument("--marker-file", type=Path, help="tail newline-delimited event names while running")
    ap.add_argument("--root-pid", type=int, help="benchmark root PID; only this PID and descendants are workload scope")
    ap.add_argument("--pid-file", type=Path, help="read benchmark root PID from this file")
    ap.add_argument("--powermetrics", action="store_true", help="optional; requires sudo and is sampled separately")
    args = ap.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = args.output_dir / f"host-monitor-{stamp}.csv"
    jsonl_path = args.output_dir / f"host-monitor-{stamp}.jsonl"
    marker_path = args.output_dir / f"host-monitor-{stamp}.markers.jsonl"
    csv_file = csv_path.open("w", newline="", encoding="utf-8")
    jsonl_file = jsonl_path.open("w", encoding="utf-8")
    marker_file = marker_path.open("w", encoding="utf-8")
    fieldnames = ["timestamp", "event", "phase", "memory", "cpu", "disk", "process_tree_rss_bytes", "process_tree_cpu_percent", "processes"]
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()
    start = time.monotonic()
    previous_swap: int | None = None
    marker_offset = 0
    root_pid = args.root_pid
    if args.pid_file:
        try:
            root_pid = int(args.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            root_pid = None
    if root_pid is None:
        print("WARNING: no --root-pid/--pid-file; workload scope is empty (system metrics only)", flush=True)
    else:
        print(f"WORKLOAD_SCOPE_ROOT_PID={root_pid}", flush=True)
    seen_pids: set[int] = set()
    root_seen = False
    root_exit_monotonic: float | None = None
    post_events_emitted: set[int] = set()
    stop = False

    def on_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    for text in args.mark:
        marker = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": text, "source": "manual"}
        marker_file.write(json.dumps(marker) + "\n")
        marker_file.flush()
    print(f"HOST MONITOR running; interval={args.interval}s; CSV={csv_path}; JSONL={jsonl_path}")
    print("GPU_MEMORY_PROCESS=N/A (Apple Silicon unified memory; no NVIDIA-style per-process VRAM)")
    if args.powermetrics:
        print("POWER/GPU=N/A: powermetrics collection requires explicit sudo and is not started automatically")
    try:
        while not stop and (args.duration <= 0 or time.monotonic() - start < args.duration):
            now = datetime.now(timezone.utc).isoformat()
            mem = vm_snapshot()
            swap = mem["swap_used_bytes"]
            mem["swap_delta_bytes"] = swap - previous_swap if swap is not None and previous_swap is not None else None
            previous_swap = swap
            cpu = cpu_snapshot()
            disk = disk_snapshot()
            rows = process_tree(ps_rows(), root_pid)
            if root_pid is not None and root_pid in {int(r["pid"]) for r in rows}:
                root_seen = True
            elif root_seen and root_exit_monotonic is None:
                root_exit_monotonic = time.monotonic()
            if root_exit_monotonic is not None:
                elapsed_exit = time.monotonic() - root_exit_monotonic
                for offset in (10, 30):
                    if elapsed_exit >= offset and offset not in post_events_emitted:
                        marker_file.write(json.dumps({"timestamp": now, "event": f"POST_EXIT_{offset}S", "pid": root_pid, "source": "inferred"}) + "\n")
                        marker_file.flush()
                        post_events_emitted.add(offset)
            current_pids = {int(r["pid"]) for r in rows}
            for pid in sorted(current_pids - seen_pids):
                event = {"timestamp": now, "event": "PROCESS_START", "pid": pid, "source": "inferred"}
                marker_file.write(json.dumps(event) + "\n")
            for pid in sorted(seen_pids - current_pids):
                event = {"timestamp": now, "event": "PROCESS_EXIT", "pid": pid, "source": "inferred"}
                marker_file.write(json.dumps(event) + "\n")
            seen_pids = current_pids
            if args.marker_file and args.marker_file.exists():
                external = args.marker_file.read_text(encoding="utf-8").splitlines()
                for text in external[marker_offset:]:
                    marker_file.write(json.dumps({"timestamp": now, "event": text, "source": "external_marker_file"}) + "\n")
                marker_offset = len(external)
            marker_file.flush()
            rss_total = sum(int(r["rss_kib"]) * 1024 for r in rows)
            cpu_total = sum(float(r["cpu_percent"]) for r in rows)
            processes = [{**r, "rss_mib": round(int(r["rss_kib"]) / 1024, 1)} for r in rows]
            phase = "active" if root_exit_monotonic is None else "post_exit"
            sample = {"timestamp": now, "event": "SAMPLE", "phase": phase, "memory": mem, "cpu": cpu, "disk": disk,
                      "gpu_memory_process": None, "process_tree_rss_bytes": rss_total,
                      "process_tree_cpu_percent": cpu_total, "processes": processes}
            writer.writerow({"timestamp": now, "event": "SAMPLE", "phase": phase, "memory": json.dumps(mem), "cpu": json.dumps(cpu),
                             "disk": json.dumps(disk), "process_tree_rss_bytes": rss_total,
                             "process_tree_cpu_percent": round(cpu_total, 2), "processes": json.dumps(processes, ensure_ascii=False)})
            csv_file.flush()
            jsonl_file.write(json.dumps(sample, ensure_ascii=False) + "\n")
            jsonl_file.flush()
            print(format_system({**mem, **cpu, **disk, "timestamp": now}), flush=True)
            for row in processes:
                print(f"PID={row['pid']} PPID={row['ppid']} TYPE={row['type']} CPU={row['cpu_percent']:.1f}% RSS={row['rss_mib']:.1f}MiB ELAPSED={row['elapsed']} COMMAND={row['command'][:180]}", flush=True)
            print(f"PROCESS_TREE_RSS_TOTAL={format_bytes(rss_total)} PROCESS_TREE_CPU={cpu_total:.1f}%", flush=True)
            time.sleep(max(0.1, args.interval))
    finally:
        csv_file.close(); jsonl_file.close(); marker_file.close()
        print(f"HOST MONITOR stopped; CSV={csv_path}; JSONL={jsonl_path}; MARKERS={marker_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
