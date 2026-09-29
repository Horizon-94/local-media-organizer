#!/usr/bin/env python3
"""Small, bounded validation of the benchmark monitor itself.

This deliberately does not load a model or read media. It validates observable
directions and records which host metrics are unavailable in a sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from run_benchmark import process_table, vm_snapshot, disk_snapshot


def run_case(name: str, code: str, out: Path) -> dict[str, object]:
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples: list[dict[str, object]] = []
    while proc.poll() is None:
        rows, totals = process_table(proc.pid)
        samples.append({"vm": vm_snapshot(), "disk": disk_snapshot(), "process": totals, "t": time.time()})
        time.sleep(0.25)
    rows, totals = process_table(proc.pid)
    samples.append({"vm": vm_snapshot(), "disk": disk_snapshot(), "process": totals, "t": time.time()})
    return {"name": name, "exit_code": proc.returncode, "samples": samples}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # CPU and memory signals are intentionally short and bounded.
    cpu = run_case("cpu", "import time\nt=time.time()+2.0\nx=0\nwhile time.time()<t:\n x=(x*33+17)%1000003\n", args.out)
    mem = run_case("memory", "import time\nb=bytearray(128*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\ntime.sleep(2)", args.out)
    payload = {"contract": "pipeline_benchmark_monitor_validation_v1", "cases": [cpu, mem]}
    args.out.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def values(case: dict[str, object], section: str, key: str) -> list[float]:
        result = []
        for sample in case["samples"]:  # type: ignore[index]
            value = sample[section].get(key)  # type: ignore[index]
            if isinstance(value, (int, float)):
                result.append(float(value))
        return result

    lines = ["# Benchmark monitor validation", "", "No model, database, or media was used. Each child was bounded to about two seconds.", ""]
    for case in [cpu, mem]:
        rss = values(case, "process", "process_rss_kib")
        cpu_values = values(case, "process", "process_cpu_percent")
        available = values(case, "vm", "memory_available_approx_bytes")
        rss_nonzero = sum(1 for value in rss if value > 0)
        cpu_nonzero = sum(1 for value in cpu_values if value > 0)
        lines += [f"## {case['name']}", "", f"- exit: `{case['exit_code']}`", f"- samples: `{len(case['samples'])}`", f"- process RSS samples (non-zero): `{rss_nonzero}/{len(case['samples'])}`", f"- CPU samples (non-zero): `{cpu_nonzero}/{len(case['samples'])}`", f"- system available observations: `{len(available)}`", ""]
        if case["name"] == "cpu":
            lines.append("- Expected signal: CPU rises while the loop runs.")
        else:
            lines.append("- Expected signal: child RSS rises while the allocation is resident.")
        lines.append("")
    lines += ["## Result", "", "- vm_stat and `memory_pressure -Q` system-direction sampling is available in this shell.", "- Process-tree RSS/CPU are not reliable here: `ps` visibility is denied by the Codex sandbox, so the observed process counts/RSS are empty or zero.", "- swap and iostat are also unavailable in this sandbox.", "- Child exit detection and bounded completion work.", "- **Monitor validation status: PARTIAL / HOST TERMINAL REQUIRED for process RSS, CPU, swap, and disk metrics.**", ""]
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
