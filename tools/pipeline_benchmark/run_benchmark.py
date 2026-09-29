#!/usr/bin/env python3
"""Run one benchmark command with bounded macOS resource observation."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def run_text(command: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def vm_snapshot() -> dict[str, int | None]:
    vm = run_text(["/usr/bin/vm_stat"])
    page_match = re.search(r"page size of (\d+) bytes", vm)
    page_size = int(page_match.group(1)) if page_match else 4096

    def pages(label: str) -> int | None:
        match = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", vm, re.MULTILINE)
        return int(match.group(1)) if match else None

    def bytes_for(label: str) -> int | None:
        value = pages(label)
        return value * page_size if value is not None else None

    swap_text = run_text(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    swap_match = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap_text)
    multiplier = 1024 ** 3 if swap_match and swap_match.group(2) == "G" else 1024 ** 2
    swap_used = int(float(swap_match.group(1)) * multiplier) if swap_match else None
    physical_text = run_text(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
    physical = int(physical_text) if physical_text.isdigit() else None
    free = bytes_for("Pages free")
    speculative = bytes_for("Pages speculative") or 0
    return {
        "physical_memory_bytes": physical,
        "memory_available_approx_bytes": free + speculative if free is not None else None,
        "memory_active_bytes": bytes_for("Pages active"),
        "memory_inactive_bytes": bytes_for("Pages inactive"),
        "memory_wired_bytes": bytes_for("Pages wired down"),
        "memory_compressed_bytes": bytes_for("Pages occupied by compressor"),
        "swap_used_bytes": swap_used,
    }


def process_table(root_pid: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text = run_text([
        "/bin/ps", "-axo", "pid=,ppid=,%cpu=,%mem=,rss=,state=,command="
    ])
    all_rows: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            all_rows[pid] = {
                "pid": pid,
                "ppid": int(parts[1]),
                "cpu_percent": float(parts[2]),
                "memory_percent": float(parts[3]),
                "rss_kib": int(parts[4]),
                "state": parts[5],
                "command": parts[6] if len(parts) > 6 else "",
            }
        except ValueError:
            continue
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, row in all_rows.items():
            if row["ppid"] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    rows = [all_rows[pid] for pid in selected if pid in all_rows]
    return rows, {
        "process_count": len(rows),
        "process_rss_kib": sum(row["rss_kib"] for row in rows),
        "process_cpu_percent": round(sum(row["cpu_percent"] for row in rows), 2),
        "root_alive": int(root_pid in all_rows),
    }


def disk_snapshot() -> dict[str, int | None]:
    # `iostat -Id` is cumulative since boot. Deltas between samples provide
    # system disk throughput; per-process disk bytes are not reliably exposed
    # without privileged tracing and are intentionally not fabricated.
    text = run_text(["/usr/sbin/iostat", "-Id", "-c", "1"])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numeric: list[float] = []
    for line in lines:
        if re.fullmatch(r"[0-9.\s]+", line):
            numeric = [float(value) for value in line.split()]
    if not numeric:
        return {"disk_cumulative_mib": None}
    # macOS prints repeating triplets: KB/t, transfers, cumulative MB.
    # Only the third value of each triplet is additive across disks.
    return {"disk_cumulative_mib": int(sum(numeric[2::3]))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("benchmark_command_missing")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    stdout_path = output / "workload.stdout.log"
    stderr_path = output / "workload.stderr.log"
    pid_path = output / "workload.pid"
    exit_path = output / "workload.exit"
    samples_path = output / "resource_samples.csv"
    processes_path = output / "process_samples.jsonl"
    started = time.time()
    before = vm_snapshot()
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
        samples: list[dict[str, Any]] = []
        with processes_path.open("w", encoding="utf-8") as process_handle:
            while True:
                process_rows, process_totals = process_table(process.pid)
                row = {
                    "timestamp_epoch": round(time.time(), 3),
                    "elapsed_seconds": round(time.time() - started, 3),
                    **vm_snapshot(),
                    **disk_snapshot(),
                    **process_totals,
                }
                samples.append(row)
                process_handle.write(json.dumps({
                    "timestamp_epoch": row["timestamp_epoch"],
                    "processes": process_rows,
                }, ensure_ascii=False) + "\n")
                process_handle.flush()
                if process.poll() is not None:
                    break
                if time.time() - started > args.timeout:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    break
                time.sleep(max(0.25, args.interval))
    exit_code = process.returncode if process.returncode is not None else 124
    exit_path.write_text(f"{exit_code}\n", encoding="utf-8")
    fields = list(samples[0]) if samples else []
    with samples_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(samples)
    after = vm_snapshot()
    peak_rss = max((int(row["process_rss_kib"]) for row in samples), default=0)
    peak_cpu = max((float(row["process_cpu_percent"]) for row in samples), default=0.0)
    compressed = [int(row["memory_compressed_bytes"]) for row in samples if row.get("memory_compressed_bytes") is not None]
    disk = [int(row["disk_cumulative_mib"]) for row in samples if row.get("disk_cumulative_mib") is not None]
    summary = {
        "contract": "local_media_pipeline_benchmark_run_v1",
        "name": args.name,
        "command": command,
        "pid": process.pid,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.time() - started, 3),
        "sample_count": len(samples),
        "peak_process_tree_rss_bytes": peak_rss * 1024,
        "peak_process_tree_cpu_percent": peak_cpu,
        "peak_compressed_memory_bytes": max(compressed, default=0),
        "swap_start_bytes": before.get("swap_used_bytes"),
        "swap_end_bytes": after.get("swap_used_bytes"),
        "swap_delta_bytes": (
            int(after["swap_used_bytes"]) - int(before["swap_used_bytes"])
            if after.get("swap_used_bytes") is not None and before.get("swap_used_bytes") is not None
            else None
        ),
        "system_disk_delta_mib": max(disk) - min(disk) if disk else None,
        "gpu_memory_metric": None,
        "gpu_memory_note": "No reliable separate GPU-memory counter is exposed on Apple Silicon; unified memory, swap and Metal errors are the safety signals.",
        "artifacts": {
            "samples_csv": str(samples_path),
            "process_samples_jsonl": str(processes_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "pid": str(pid_path),
            "exit": str(exit_path),
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if exit_code == 0 and not timed_out else exit_code or 124


if __name__ == "__main__":
    raise SystemExit(main())
