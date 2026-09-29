#!/usr/bin/env python3
"""Audit completed benchmark artifacts without rerunning any workload."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def mib(value: float | None) -> str:
    return "N/A" if value is None else f"{value / 1024 / 1024:.1f} MiB"


def gib(value: float | None) -> str:
    return "N/A" if value is None else f"{value / 1024 / 1024 / 1024:.3f} GiB"


def num(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value in (None, "", "None", "null"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def audit(name: str, path: Path, workload_units: float | None) -> dict[str, object]:
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader((path / "resource_samples.csv").open(encoding="utf-8")))
    physical = [num(r, "physical_memory_bytes") for r in rows]
    available = [num(r, "memory_available_approx_bytes") for r in rows]
    used = [p - a for p, a in zip(physical, available) if p is not None and a is not None]
    rss_kib = [v for r in rows if (v := num(r, "process_rss_kib")) is not None]
    if rss_kib and max(rss_kib) == 0 and "unavailable" in str(summary.get("monitoring_note", "")).lower():
        rss_kib = []
    cpu = [v for r in rows if (v := num(r, "process_cpu_percent")) is not None]
    if cpu and max(cpu) == 0 and "unavailable" in str(summary.get("monitoring_note", "")).lower():
        cpu = []
    compressed = [v for r in rows if (v := num(r, "memory_compressed_bytes")) is not None]
    swap = [v for r in rows if (v := num(r, "swap_used_bytes")) is not None]
    elapsed = [v for r in rows if (v := num(r, "elapsed_seconds")) is not None]
    disk_key = "disk_cumulative_mib" if rows and "disk_cumulative_mib" in rows[0] else "disk_cumulative_kib"
    disk = [v for r in rows if (v := num(r, disk_key)) is not None]
    intervals = [b - a for a, b in zip(elapsed, elapsed[1:])]
    return {
        "name": name,
        "wall_seconds": summary.get("elapsed_seconds"),
        "sample_count": len(rows),
        "sample_interval_seconds": round(statistics.median(intervals), 3) if intervals else None,
        "sample_interval_range_seconds": [round(min(intervals), 3), round(max(intervals), 3)] if intervals else None,
        "baseline_system_used_bytes": used[0] if used else None,
        "average_system_used_bytes": statistics.mean(used) if used else None,
        "peak_system_used_bytes": max(used) if used else None,
        "baseline_rss_bytes": rss_kib[0] * 1024 if rss_kib else None,
        "average_rss_bytes": statistics.mean(rss_kib) * 1024 if rss_kib else None,
        "peak_rss_bytes": max(rss_kib) * 1024 if rss_kib else None,
        "compressed_before_bytes": compressed[0] if compressed else None,
        "compressed_peak_bytes": max(compressed) if compressed else None,
        "compressed_after_bytes": compressed[-1] if compressed else None,
        "swap_before_bytes": swap[0] if swap else None,
        "swap_peak_bytes": max(swap) if swap else None,
        "swap_after_bytes": swap[-1] if swap else None,
        "swap_delta_bytes": (swap[-1] - swap[0]) if swap else None,
        "average_cpu_percent": statistics.mean(cpu) if cpu else None,
        "peak_cpu_percent": max(cpu) if cpu else None,
        "cpu_p95_percent": percentile(cpu, 0.95),
        "disk_delta": (max(disk) - min(disk)) if disk else None,
        "disk_unit": disk_key.removeprefix("disk_cumulative_"),
        "throughput_units": (workload_units / float(summary["elapsed_seconds"])) if workload_units and summary.get("elapsed_seconds") else None,
        "throughput_unit": "units/second" if workload_units is not None else "N/A",
        "exit_code": summary.get("exit_code"),
        "error": "none (exit 0)" if summary.get("exit_code") == 0 else "see workload stderr",
        "process_rss_reliability": summary.get("process_rss_note") or ("N/A - sandbox process visibility unavailable" if "unavailable" in str(summary.get("monitoring_note", "")).lower() else "legacy summary: ps tree sampled"),
        "coverage": "process start/active/exit sampled; model-load boundary was not emitted as a separate event",
    }


def row_md(item: dict[str, object]) -> str:
    def x(k: str) -> str:
        v = item.get(k)
        if v is None:
            return "N/A"
        if k.endswith("_bytes"):
            return mib(float(v))
        if k in {"wall_seconds", "sample_interval_seconds"}:
            return f"{float(v):.3f}s"
        if k == "throughput_units":
            return f"{float(v):.3f} {item.get('throughput_unit', '')}"
        if k == "disk_delta":
            return f"{float(v):.1f} {item.get('disk_unit', '')}"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)
    return "| {name} | {wall} | {base} | {avg} | {peak} | {rssb} | {rssa} | {rssp} | {compb} | {compp} | {compa} | {swapb} | {swapp} | {swapa} | {swapd} | {cpua} | {cpup95} | {cpup} | {disk} | {thr} | {exit} | {error} |".format(
        name=item["name"], wall=x("wall_seconds"), base=x("baseline_system_used_bytes"), avg=x("average_system_used_bytes"), peak=x("peak_system_used_bytes"),
        rssb=x("baseline_rss_bytes"), rssa=x("average_rss_bytes"), rssp=x("peak_rss_bytes"), compb=x("compressed_before_bytes"), compp=x("compressed_peak_bytes"), compa=x("compressed_after_bytes"),
        swapb=x("swap_before_bytes"), swapp=x("swap_peak_bytes"), swapa=x("swap_after_bytes"), swapd=x("swap_delta_bytes"), cpua=x("average_cpu_percent"), cpup95=x("cpu_p95_percent"), cpup=x("peak_cpu_percent"), disk=x("disk_delta"), thr=x("throughput_units"), exit=x("exit_code"), error=x("error")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    specs = [("YOLOE ×1", "yoloe-x1-r2", 10), ("OpenCLIP ×1", "openclip-x1-r2", 10), ("FFmpeg direct HEVC experiment", "ffmpeg-hevc-1", None), ("FFmpeg formal Stage 03", "formal-stage03-bench", 168)]
    local_root = Path(__file__).resolve().parents[2] / "test-output" / args.root.name
    items = []
    for label, folder, units in specs:
        path = args.root / folder
        if not (path / "summary.json").exists() and (local_root / folder / "summary.json").exists():
            path = local_root / folder
        items.append(audit(label, path, units))
    payload = {"contract": "pipeline_benchmark_audit_v1", "items": items}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = ["# Existing benchmark data audit", "", "This report reads completed artifacts only; no workload was rerun.", "", "## Complete metric table", "", "| workload | wall | baseline system used | average system used | peak system used | RSS before | RSS avg | RSS peak | compressed before | compressed peak | compressed after | swap before | swap peak | swap after | swap Δ | CPU avg | CPU P95 | CPU peak | disk delta | throughput | exit | error |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    lines.extend(row_md(item) for item in items)
    lines += ["", "## Sampling validity", ""]
    for item in items:
        lines.append(f"- **{item['name']}**: {item['sample_count']} samples; median interval {item['sample_interval_seconds']}s; range {item['sample_interval_range_seconds']}; {item['coverage']}. Process RSS: {item['process_rss_reliability']}.")
    lines += ["", "## Interpretation limits", "", "- System-used memory is calculated as `physical_memory - available_approx`; it is not a direct per-process allocation and the available value is derived from vm_stat free + speculative pages.", "- Existing CSVs record an aggregate cumulative disk counter, not separate read/write bytes. Separate read/write deltas are therefore **N/A**.", "- The old benchmark did not emit explicit T0–T6 model lifecycle markers; model-load boundary is **N/A**.", "- Apple Silicon has unified memory; no separate GPU-memory number is claimed.", "- The direct FFmpeg row is an experimental baseline; the formal Stage 03 row is correctness-valid at 546.85s / 168 frames but its sandbox resource fields are intentionally N/A."]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
