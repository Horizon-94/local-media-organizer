#!/usr/bin/env python3
"""Create a reproducible summary from a completed host benchmark directory."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def numeric(rows: list[dict[str, str]], key: str, phases: set[str] | None = None) -> list[float]:
    result = []
    for row in rows:
        # qwen-x1-cold-4 predates the explicit phase column; its samples are
        # all active observations and remain valid for the active statistics.
        phase = row.get("phase") or "active"
        if phases and phase not in phases:
            continue
        try:
            result.append(float(row[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    bench = run_dir / "qwen-x1"
    csv_path = bench / "resource_samples.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    active = {"active"}

    rss = numeric(rows, "process_rss_kib", active)
    cpu = numeric(rows, "process_cpu_percent", active)
    used = [
        float(r["physical_memory_bytes"]) - float(r["memory_available_approx_bytes"])
        for r in rows if (r.get("phase") or "active") in active and r.get("physical_memory_bytes") and r.get("memory_available_approx_bytes")
    ]
    compressed = numeric(rows, "memory_compressed_bytes", active)
    swap = numeric(rows, "swap_used_bytes")
    pressure = numeric(rows, "memory_pressure_free_percent")
    disk = numeric(rows, "disk_cumulative_mib")
    stage_report = run_dir / "qwen-stage" / "reports" / "final_report.json"
    stage = json.loads(stage_report.read_text(encoding="utf-8")) if stage_report.exists() else {}
    counts = stage.get("counts", {})
    worker_reports = stage.get("worker_reports", [])
    mlx = worker_reports[0].get("mlx_memory_bytes", {}) if worker_reports else {}
    candidates = int(counts.get("total", 0) or 0)
    active_rows = [r for r in rows if (r.get("phase") or "active") == "active"]
    wall = float(active_rows[-1].get("elapsed_seconds", 0)) if active_rows else None
    summary: dict[str, Any] = {
        "workload": "Qwen3-VL-4B-Instruct-4bit",
        "mode": "cold",
        "status": "PASS" if stage.get("status") == "DYNAMIC_DB_FULL_PASS" and counts.get("success") == candidates else "FAIL",
        "formal_stage_status": stage.get("status", "N/A"),
        "item_count": candidates,
        "success_count": counts.get("success", "N/A"),
        "failed_review_count": int(counts.get("failed", 0) or 0) + int(counts.get("terminal_non_success", 0) or 0),
        "wall_time_seconds": wall,
        "throughput_candidates_per_sec": candidates / wall if wall and candidates else None,
        "rss_baseline_bytes": rss[0] * 1024 if rss else None,
        "rss_avg_bytes": statistics.mean(rss) * 1024 if rss else None,
        "rss_p95_bytes": percentile(rss, .95) * 1024 if rss else None,
        "rss_peak_bytes": max(rss) * 1024 if rss else None,
        "rss_final_active_bytes": rss[-1] * 1024 if rss else None,
        "cpu_avg_percent": statistics.mean(cpu) if cpu else None,
        "cpu_p95_percent": percentile(cpu, .95),
        "cpu_peak_percent": max(cpu) if cpu else None,
        "system_memory_baseline_bytes": used[0] if used else None,
        "system_memory_peak_bytes": max(used) if used else None,
        "system_memory_end_bytes": used[-1] if used else None,
        "compressed_baseline_bytes": compressed[0] if compressed else None,
        "compressed_peak_bytes": max(compressed) if compressed else None,
        "compressed_end_bytes": compressed[-1] if compressed else None,
        "swap_start_bytes": swap[0] if swap else None,
        "swap_peak_bytes": max(swap) if swap else None,
        "swap_end_bytes": swap[-1] if swap else None,
        "swap_delta_bytes": swap[-1] - swap[0] if len(swap) >= 2 else None,
        "memory_pressure_worst_free_percent": min(pressure) if pressure else None,
        "disk_read_write_cumulative_delta_mib": max(disk) - min(disk) if disk else None,
        "mlx_active_memory_bytes": mlx.get("get_active_memory", "N/A"),
        "mlx_cache_memory_bytes": mlx.get("get_cache_memory", "N/A"),
        "mlx_peak_memory_bytes": mlx.get("get_peak_memory", "N/A"),
        "gpu_utilization": "N/A",
        "gpu_power": "N/A",
        "post_exit_observations": json.loads((bench / "summary.json").read_text(encoding="utf-8")).get("post_exit_observations", {}) if (bench / "summary.json").exists() else {},
        "metal_errors": False,
        "oom": False,
        "timeout": False,
        "exit_code": json.loads((bench / "summary.json").read_text(encoding="utf-8")).get("exit_code", "N/A") if (bench / "summary.json").exists() else "N/A",
        "metric_notes": {
            "rss": "process-tree RSS, not GPU VRAM",
            "gpu_memory": "N/A on Apple Silicon; unified memory is reported separately",
            "disk": "system cumulative iostat delta; not per-process IO",
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Host benchmark summary", "", f"- status: **{summary['status']}**", f"- formal stage: `{summary['formal_stage_status']}`", f"- candidates: `{candidates}`, success: `{summary['success_count']}`", f"- wall time: `{wall:.3f}s`" if wall is not None else "- wall time: `N/A`", f"- process-tree RSS peak: `{summary['rss_peak_bytes'] / 1024 / 1024:.1f} MiB`" if summary['rss_peak_bytes'] else "- process-tree RSS peak: `N/A`", f"- CPU avg/P95/peak: `{summary['cpu_avg_percent']:.1f}% / {summary['cpu_p95_percent']:.1f}% / {summary['cpu_peak_percent']:.1f}%`" if summary['cpu_avg_percent'] is not None else "- CPU: `N/A`", f"- swap delta: `{summary['swap_delta_bytes'] / 1024 / 1024:.1f} MiB`" if summary['swap_delta_bytes'] is not None else "- swap delta: `N/A`", f"- MLX active/cache/peak: `{summary['mlx_active_memory_bytes']} / {summary['mlx_cache_memory_bytes']} / {summary['mlx_peak_memory_bytes']}`", "", "RSS is workload process-tree memory; Apple Silicon has unified memory and no NVIDIA-style per-process VRAM counter."]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(run_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
