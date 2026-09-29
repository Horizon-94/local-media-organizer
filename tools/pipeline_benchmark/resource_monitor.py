#!/usr/bin/env python3
"""Sample macOS process and unified-memory resource data to CSV/JSON."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import time
from pathlib import Path


def command_text(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True).stdout
    except OSError:
        return ""


def scalar(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.MULTILINE)
    return int(match.group(1)) if match else None


def memory_snapshot() -> dict[str, int | None]:
    page_size = 4096
    vm = command_text(["vm_stat"])
    match = re.search(r"page size of (\d+) bytes", vm)
    if match:
        page_size = int(match.group(1))
    pages = {
        "free": scalar(vm, r"^Pages free:\s+(\d+)\."),
        "active": scalar(vm, r"^Pages active:\s+(\d+)\."),
        "inactive": scalar(vm, r"^Pages inactive:\s+(\d+)\."),
        "wired": scalar(vm, r"^Pages wired down:\s+(\d+)\."),
        "compressed": scalar(vm, r"^Pages occupied by compressor:\s+(\d+)\."),
    }
    swap = command_text(["sysctl", "-n", "vm.swapusage"])
    swap_used = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap)
    multiplier = 1024 ** 3 if swap_used and swap_used.group(2) == "G" else 1024 ** 2
    return {
        "memory_free_bytes": pages["free"] * page_size if pages["free"] is not None else None,
        "memory_active_bytes": pages["active"] * page_size if pages["active"] is not None else None,
        "memory_inactive_bytes": pages["inactive"] * page_size if pages["inactive"] is not None else None,
        "memory_wired_bytes": pages["wired"] * page_size if pages["wired"] is not None else None,
        "memory_compressed_bytes": pages["compressed"] * page_size if pages["compressed"] is not None else None,
        "swap_used_bytes": int(float(swap_used.group(1)) * multiplier) if swap_used else None,
    }


def process_snapshot(pid: int) -> dict[str, str | int | None]:
    text = command_text(["ps", "-p", str(pid), "-o", "pid=,ppid=,%cpu=,%mem=,rss=,state=,etime=,command="]).strip()
    if not text:
        return {"process_alive": 0, "pid": pid}
    parts = text.split(None, 7)
    return {
        "process_alive": 1,
        "pid": int(parts[0]),
        "ppid": int(parts[1]),
        "cpu_percent": parts[2],
        "memory_percent": parts[3],
        "rss_kib": int(parts[4]),
        "state": parts[5],
        "elapsed": parts[6],
        "command": parts[7] if len(parts) > 7 else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-seconds", type=float, default=3600)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    csv_path = args.output / "resource_samples.csv"
    started = time.time()
    rows: list[dict[str, object]] = []
    fields: list[str] = []
    while time.time() - started <= args.max_seconds:
        row: dict[str, object] = {
            "timestamp_epoch": round(time.time(), 3),
            "elapsed_seconds": round(time.time() - started, 3),
            **memory_snapshot(),
            **process_snapshot(args.pid),
        }
        rows.append(row)
        if not fields:
            fields = list(row)
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
        else:
            with csv_path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=fields).writerow(row)
        if not row.get("process_alive"):
            break
        time.sleep(max(0.1, args.interval))
    numeric = [row for row in rows if row.get("process_alive")]
    summary = {
        "contract": "local_media_pipeline_resource_monitor_v1",
        "pid": args.pid,
        "sample_count": len(rows),
        "duration_seconds": round(time.time() - started, 3),
        "peak_rss_kib": max((int(row["rss_kib"]) for row in numeric if row.get("rss_kib") is not None), default=0),
        "peak_compressed_bytes": max((int(row["memory_compressed_bytes"]) for row in rows if row.get("memory_compressed_bytes") is not None), default=0),
        "swap_start_bytes": rows[0].get("swap_used_bytes") if rows else None,
        "swap_end_bytes": rows[-1].get("swap_used_bytes") if rows else None,
        "gpu_memory_note": "macOS does not expose a reliable separate GPU-memory value on Apple Silicon; unified memory, swap and Metal errors are used instead.",
        "csv": str(csv_path),
    }
    (args.output / "resource_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
