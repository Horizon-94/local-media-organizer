#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-3C Qwen-VL full orchestrator with strong provenance + DB-ready index.

This script is a wrapper around the already-tested Qwen-VL runner:
  scripts/03_stop03_visual_analysis/stop03_3b_qwenvl_smoke_runner.py

It runs full queue with --limit 0, monitors the child process, then runs:
  scripts/03_stop03_visual_analysis/stop03_3_qwenvl_provenance_audit.py

Finally it builds DB-ready artifacts:
- manifests/qwenvl_db_ready_evidence_manifest.csv
- manifests/qwenvl_db_ready_evidence_manifest.jsonl
- database/qwenvl_evidence.sqlite
- reports/stop03_3c_qwenvl_full_orchestrator_summary.md/json
- telemetry/qwenvl_full_orchestrator_resource_samples.csv

Safety:
- Does not modify, move, delete, rename, or write to original media.
- Does not edit Stop03-2 output.
- Writes only under --out.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def find_one(base: Path, patterns: List[str]) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(base.glob(pat))
        if hits:
            return hits[0]
    return None


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_seconds": round(time.time() - t0, 3),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cmd": cmd,
        }
    except Exception as e:
        return {
            "ok": False,
            "returncode": None,
            "elapsed_seconds": round(time.time() - t0, 3),
            "stdout": "",
            "stderr": repr(e),
            "cmd": cmd,
        }


def ps_snapshot() -> List[Dict[str, Any]]:
    res = run_cmd(["ps", "-axo", "pid=,ppid=,pcpu=,rss=,command="], timeout=10)
    rows = []
    if not res["ok"] and not res["stdout"]:
        return rows
    for line in res["stdout"].splitlines():
        line = line.rstrip()
        m = re.match(r"\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(.*)$", line)
        if not m:
            continue
        rows.append({
            "pid": int(m.group(1)),
            "ppid": int(m.group(2)),
            "pcpu": float(m.group(3)),
            "rss_kb": int(m.group(4)),
            "command": m.group(5),
        })
    return rows


def descendants(root_pid: int, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_ppid: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        by_ppid.setdefault(r["ppid"], []).append(r)
    out = []
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        for child in by_ppid.get(pid, []):
            cpid = child["pid"]
            if cpid in seen:
                continue
            seen.add(cpid)
            out.append(child)
            stack.append(cpid)
    return out


def swap_used_mb() -> Optional[float]:
    res = run_cmd(["sysctl", "vm.swapusage"], timeout=5)
    text = (res.get("stdout") or res.get("stderr") or "").strip()
    m = re.search(r"used\s*=\s*([\d.]+)M", text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def monitor_process(proc: subprocess.Popen, telemetry_csv: Path, interval: float = 2.0) -> Dict[str, Any]:
    telemetry_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sample_time", "elapsed_seconds", "root_pid", "process_count",
        "cpu_percent_sum", "rss_mb_sum", "swap_used_mb",
        "top_processes"
    ]
    t0 = time.time()
    max_cpu = 0.0
    max_rss = 0.0
    max_swap = 0.0
    sample_count = 0

    with telemetry_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        while proc.poll() is None:
            rows = ps_snapshot()
            root_row = next((r for r in rows if r["pid"] == proc.pid), None)
            tree = []
            if root_row:
                tree.append(root_row)
            tree.extend(descendants(proc.pid, rows))

            cpu = sum(r["pcpu"] for r in tree)
            rss = sum(r["rss_kb"] for r in tree) / 1024.0
            swp = swap_used_mb()
            max_cpu = max(max_cpu, cpu)
            max_rss = max(max_rss, rss)
            if swp is not None:
                max_swap = max(max_swap, swp)

            top = sorted(tree, key=lambda r: r["rss_kb"], reverse=True)[:8]
            w.writerow({
                "sample_time": now_ts(),
                "elapsed_seconds": round(time.time() - t0, 3),
                "root_pid": proc.pid,
                "process_count": len(tree),
                "cpu_percent_sum": round(cpu, 3),
                "rss_mb_sum": round(rss, 3),
                "swap_used_mb": "" if swp is None else round(swp, 3),
                "top_processes": json.dumps([
                    {
                        "pid": r["pid"],
                        "pcpu": r["pcpu"],
                        "rss_mb": round(r["rss_kb"] / 1024.0, 3),
                        "command": r["command"][:200],
                    }
                    for r in top
                ], ensure_ascii=False),
            })
            f.flush()
            sample_count += 1
            time.sleep(interval)

    # one final sample
    return {
        "sample_count": sample_count,
        "max_cpu_percent_sum": round(max_cpu, 3),
        "max_cpu_cores_estimated": round(max_cpu / 100.0, 3),
        "max_rss_mb_sum": round(max_rss, 3),
        "max_swap_used_mb": round(max_swap, 3),
        "telemetry_csv": str(telemetry_csv),
    }


def run_full_qwenvl(args: argparse.Namespace, out_dir: Path) -> Dict[str, Any]:
    runner = Path(args.project_root) / "scripts/03_stop03_visual_analysis/stop03_3b_qwenvl_smoke_runner.py"
    if not runner.exists():
        raise FileNotFoundError(f"missing runner: {runner}")

    terminal_log = out_dir / "terminal.log"
    telemetry_csv = out_dir / "telemetry/qwenvl_full_orchestrator_resource_samples.csv"

    cmd = [
        sys.executable, str(runner),
        "--run-root", args.run_root,
        "--stop03-2-base", args.stop03_2_base,
        "--source-root", args.source_root,
        "--out", str(out_dir),
        "--qwen-python", args.qwen_python,
        "--model", args.model,
        "--workers", str(args.workers),
        "--limit", "0",
        "--max-tokens", str(args.max_tokens),
        "--timeout", str(args.timeout),
    ]

    t0 = time.time()
    with terminal_log.open("w", encoding="utf-8") as log:
        log.write("COMMAND: " + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=args.project_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        mon = monitor_process(proc, telemetry_csv, interval=args.monitor_interval)
        returncode = proc.wait()

    wall = time.time() - t0
    return {
        "cmd": cmd,
        "returncode": returncode,
        "ok": returncode == 0,
        "wall_seconds": round(wall, 3),
        "terminal_log": str(terminal_log),
        "monitor": mon,
    }


def run_provenance_audit(args: argparse.Namespace, out_dir: Path) -> Dict[str, Any]:
    audit_script = Path(args.project_root) / "scripts/03_stop03_visual_analysis/stop03_3_qwenvl_provenance_audit.py"
    if not audit_script.exists():
        raise FileNotFoundError(f"missing provenance audit: {audit_script}")

    cmd = [
        sys.executable, str(audit_script),
        "--qwen-run-dir", str(out_dir),
        "--source-root", args.source_root,
    ]
    return run_cmd(cmd, cwd=Path(args.project_root), timeout=600)


def read_text_file(path: str, max_chars: int = 20000) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
        return text[:max_chars]
    except Exception:
        return ""


def build_db_ready_index(args: argparse.Namespace, out_dir: Path) -> Dict[str, Any]:
    prov_csv = out_dir / "manifests/qwenvl_result_provenance_manifest.csv"
    rows = read_csv(prov_csv)
    db_rows: List[Dict[str, Any]] = []

    for r in rows:
        qwen_text = read_text_file(r.get("qwen_output_text_path", ""))
        qwen_text_sha256 = r.get("qwen_output_text_sha256") or sha256_text(qwen_text) if qwen_text else ""

        evidence_basis = "|".join([
            r.get("provenance_id", ""),
            r.get("candidate_runtime_id", ""),
            r.get("visual_unit_id", ""),
            r.get("runtime_input_image_sha256", ""),
            qwen_text_sha256,
            args.model,
        ])
        evidence_id = "qwe_" + sha256_text(evidence_basis)[:24]

        source_path = r.get("resolved_original_source_path") or r.get("original_source_path_at_processing_time") or ""
        source_rel = r.get("source_relative_path") or r.get("parent_source_relative_path") or ""

        db_rows.append({
            "evidence_id": evidence_id,
            "evidence_type": "qwenvl_visual_description",
            "database_contract_version": "stop03_3c_qwenvl_evidence_v1",
            "provenance_id": r.get("provenance_id", ""),
            "candidate_runtime_id": r.get("candidate_runtime_id", ""),
            "candidate_id": r.get("candidate_id", ""),
            "visual_unit_id": r.get("visual_unit_id", ""),
            "runtime_source": r.get("runtime_source", ""),
            "runtime_reason_codes": r.get("runtime_reason_codes", "") or r.get("reason_codes", ""),
            "visual_unit_type": r.get("visual_unit_type", "") or r.get("candidate_type", "") or r.get("preview_role", ""),
            "time_position_ms": r.get("time_position_ms", ""),
            "source_relative_path": source_rel,
            "resolved_original_source_path": source_path,
            "resolved_original_source_exists": r.get("resolved_original_source_exists", ""),
            "original_source_content_id": r.get("original_source_content_id", "") or r.get("parent_source_content_id", ""),
            "runtime_input_image_path": r.get("runtime_input_image_path", ""),
            "runtime_input_image_sha256": r.get("runtime_input_image_sha256", ""),
            "qwen_output_text_path": r.get("qwen_output_text_path", ""),
            "qwen_output_text_sha256": qwen_text_sha256,
            "qwen_text": qwen_text,
            "qwen_text_preview": qwen_text[:500],
            "qwen_model_path": args.model,
            "qwen_python": args.qwen_python,
            "status": r.get("status", ""),
            "elapsed_seconds": r.get("elapsed_seconds", ""),
            "returncode": r.get("returncode", ""),
            "stdout_path": r.get("stdout_path", ""),
            "stderr_path": r.get("stderr_path", ""),
            "finder_tags": r.get("finder_tags", ""),
            "created_at": now_ts(),
        })

    fields = [
        "evidence_id", "evidence_type", "database_contract_version", "provenance_id",
        "candidate_runtime_id", "candidate_id", "visual_unit_id", "runtime_source",
        "runtime_reason_codes", "visual_unit_type", "time_position_ms",
        "source_relative_path", "resolved_original_source_path", "resolved_original_source_exists",
        "original_source_content_id", "runtime_input_image_path", "runtime_input_image_sha256",
        "qwen_output_text_path", "qwen_output_text_sha256", "qwen_text", "qwen_text_preview",
        "qwen_model_path", "qwen_python", "status", "elapsed_seconds", "returncode",
        "stdout_path", "stderr_path", "finder_tags", "created_at",
    ]

    csv_path = out_dir / "manifests/qwenvl_db_ready_evidence_manifest.csv"
    jsonl_path = out_dir / "manifests/qwenvl_db_ready_evidence_manifest.jsonl"
    sqlite_path = out_dir / "database/qwenvl_evidence.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    write_csv(csv_path, db_rows, fields)
    write_jsonl(jsonl_path, db_rows)

    con = sqlite3.connect(str(sqlite_path))
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS qwenvl_evidence (
                evidence_id TEXT PRIMARY KEY,
                evidence_type TEXT,
                database_contract_version TEXT,
                provenance_id TEXT,
                candidate_runtime_id TEXT,
                candidate_id TEXT,
                visual_unit_id TEXT,
                runtime_source TEXT,
                runtime_reason_codes TEXT,
                visual_unit_type TEXT,
                time_position_ms TEXT,
                source_relative_path TEXT,
                resolved_original_source_path TEXT,
                resolved_original_source_exists TEXT,
                original_source_content_id TEXT,
                runtime_input_image_path TEXT,
                runtime_input_image_sha256 TEXT,
                qwen_output_text_path TEXT,
                qwen_output_text_sha256 TEXT,
                qwen_text TEXT,
                qwen_text_preview TEXT,
                qwen_model_path TEXT,
                qwen_python TEXT,
                status TEXT,
                elapsed_seconds TEXT,
                returncode TEXT,
                stdout_path TEXT,
                stderr_path TEXT,
                finder_tags TEXT,
                created_at TEXT
            )
        """)
        con.execute("DELETE FROM qwenvl_evidence")
        placeholders = ",".join(["?"] * len(fields))
        con.executemany(
            f"INSERT OR REPLACE INTO qwenvl_evidence ({','.join(fields)}) VALUES ({placeholders})",
            [[str(row.get(f, "")) for f in fields] for row in db_rows]
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_qwenvl_visual_unit_id ON qwenvl_evidence(visual_unit_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qwenvl_source_relative_path ON qwenvl_evidence(source_relative_path)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qwenvl_original_source_content_id ON qwenvl_evidence(original_source_content_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qwenvl_runtime_source ON qwenvl_evidence(runtime_source)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qwenvl_status ON qwenvl_evidence(status)")
        con.commit()
    finally:
        con.close()

    counters = {
        "status_counts": dict(Counter(r["status"] for r in db_rows)),
        "visual_unit_type_counts": dict(Counter(r["visual_unit_type"] for r in db_rows)),
        "runtime_source_counts": dict(Counter(r["runtime_source"] for r in db_rows)),
        "missing_original_source_path_count": sum(1 for r in db_rows if not r["resolved_original_source_path"]),
        "missing_input_sha256_count": sum(1 for r in db_rows if not r["runtime_input_image_sha256"]),
        "missing_output_sha256_count": sum(1 for r in db_rows if not r["qwen_output_text_sha256"]),
    }

    return {
        "db_ready_row_count": len(db_rows),
        "db_ready_csv": str(csv_path),
        "db_ready_jsonl": str(jsonl_path),
        "db_ready_sqlite": str(sqlite_path),
        **counters,
    }


def read_json_if_exists(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return {}
    return {}


def write_summary(out_dir: Path, summary: Dict[str, Any]) -> None:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "stop03_3c_qwenvl_full_orchestrator_summary.json"
    md_path = reports / "stop03_3c_qwenvl_full_orchestrator_summary.md"

    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    qwen_run = summary.get("qwen_run", {})
    db = summary.get("db_ready_index", {})
    prov = summary.get("provenance_summary", {})
    smoke = summary.get("qwen_runner_summary", {})

    lines = [
        "# Stop03-3C Qwen-VL Full Orchestrator Summary",
        "",
        f"- status: {summary.get('status')}",
        f"- source_safety: {summary.get('source_safety')}",
        f"- out: {summary.get('out')}",
        "",
        "## Qwen-VL run",
        f"- returncode: {qwen_run.get('returncode')}",
        f"- wall_seconds_wrapper: {qwen_run.get('wall_seconds')}",
        f"- terminal_log: {qwen_run.get('terminal_log')}",
        f"- runner_summary_status: {smoke.get('status')}",
        f"- rows_selected: {smoke.get('rows_selected')}",
        f"- success_count: {smoke.get('success_count')}",
        f"- failed_count: {smoke.get('failed_count')}",
        f"- workers_requested: {smoke.get('workers_requested')}",
        f"- runner_wall_seconds: {smoke.get('wall_seconds')}",
        f"- avg_task_seconds_measured_inside_worker: {smoke.get('avg_task_seconds_measured_inside_worker')}",
        f"- effective_wall_seconds_per_completed_image: {smoke.get('effective_wall_seconds_per_completed_image')}",
        f"- parallel_adjusted_single_lane_estimate_seconds: {smoke.get('parallel_adjusted_single_lane_estimate_seconds')}",
        "",
        "## Resource monitor",
        f"- sample_count: {qwen_run.get('monitor', {}).get('sample_count')}",
        f"- max_cpu_percent_sum: {qwen_run.get('monitor', {}).get('max_cpu_percent_sum')}",
        f"- max_cpu_cores_estimated: {qwen_run.get('monitor', {}).get('max_cpu_cores_estimated')}",
        f"- max_rss_mb_sum: {qwen_run.get('monitor', {}).get('max_rss_mb_sum')}",
        f"- max_swap_used_mb: {qwen_run.get('monitor', {}).get('max_swap_used_mb')}",
        f"- telemetry_csv: {qwen_run.get('monitor', {}).get('telemetry_csv')}",
        "",
        "## Provenance",
        f"- provenance_status: {prov.get('status')}",
        f"- provenance_row_count: {prov.get('provenance_row_count')}",
        f"- missing_counts: {prov.get('missing_counts')}",
        f"- provenance_csv: {prov.get('provenance_csv')}",
        f"- provenance_jsonl: {prov.get('provenance_jsonl')}",
        "",
        "## DB-ready index",
        f"- db_ready_row_count: {db.get('db_ready_row_count')}",
        f"- status_counts: {db.get('status_counts')}",
        f"- visual_unit_type_counts: {db.get('visual_unit_type_counts')}",
        f"- runtime_source_counts: {db.get('runtime_source_counts')}",
        f"- missing_original_source_path_count: {db.get('missing_original_source_path_count')}",
        f"- missing_input_sha256_count: {db.get('missing_input_sha256_count')}",
        f"- missing_output_sha256_count: {db.get('missing_output_sha256_count')}",
        f"- db_ready_csv: {db.get('db_ready_csv')}",
        f"- db_ready_jsonl: {db.get('db_ready_jsonl')}",
        f"- db_ready_sqlite: {db.get('db_ready_sqlite')}",
        "",
        "PASS condition:",
        "- Qwen runner returncode is 0.",
        "- Provenance status is PASS.",
        "- DB-ready row count equals provenance row count.",
        "- Missing input/output sha256 counts are 0.",
    ]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    summary["summary_json"] = str(json_path)
    summary["summary_md"] = str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/Users/yourname/Documents/AI-Local/media-archive-clean")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--stop03-2-base", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qwen-python", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=180)
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--monitor-interval", type=float, default=2.0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "status": "UNKNOWN",
        "created_at": now_ts(),
        "source_safety": "read_only_no_original_media_modification",
        "project_root": args.project_root,
        "run_root": args.run_root,
        "stop03_2_base": args.stop03_2_base,
        "source_root": args.source_root,
        "out": str(out_dir),
        "qwen_python": args.qwen_python,
        "model": args.model,
        "workers": args.workers,
        "max_tokens": args.max_tokens,
        "timeout": args.timeout,
    }

    try:
        qwen_run = run_full_qwenvl(args, out_dir)
        summary["qwen_run"] = qwen_run
        if not qwen_run["ok"]:
            summary["status"] = "FAIL_QWENVL_RUN"
            write_summary(out_dir, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            sys.exit(2)

        prov_run = run_provenance_audit(args, out_dir)
        summary["provenance_audit_run"] = prov_run
        if not prov_run["ok"]:
            summary["status"] = "FAIL_PROVENANCE_AUDIT"
            write_summary(out_dir, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            sys.exit(3)

        runner_summary = read_json_if_exists(out_dir / "reports/qwenvl_smoke_summary.json")
        prov_summary = read_json_if_exists(out_dir / "reports/qwenvl_provenance_audit_summary.json")
        summary["qwen_runner_summary"] = runner_summary
        summary["provenance_summary"] = prov_summary

        db_ready = build_db_ready_index(args, out_dir)
        summary["db_ready_index"] = db_ready

        pass_condition = (
            prov_summary.get("status") == "PASS"
            and db_ready.get("db_ready_row_count") == prov_summary.get("provenance_row_count")
            and db_ready.get("missing_input_sha256_count") == 0
            and db_ready.get("missing_output_sha256_count") == 0
        )
        summary["status"] = "PASS" if pass_condition else "PASS_WITH_REVIEW"
        write_summary(out_dir, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        summary["status"] = "FAIL_EXCEPTION"
        summary["exception"] = repr(e)
        write_summary(out_dir, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
