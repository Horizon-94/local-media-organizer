#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stop03_1_monitor_process_tree_db_safe_v7_20260709_172800.py

Purpose:
  Local/offline resource monitor for one process tree.
  Writes monitoring samples into SQLite first, then optional CSV/JSON exports.

Safety:
  - No network.
  - No model loading.
  - No dependency installation.
  - No source media read/write.
  - Output path is restricted to allowed project/test output roots.

Dependency policy:
  psutil is optional. If psutil is not installed, this script uses macOS/local ps fallback.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_VERSION = "v7_20260709_172800"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean").resolve()
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output").resolve()
PROJECT_TEST_OUTPUT_ROOT = (PROJECT_ROOT / "test-output").resolve()
PROJECT_OUTPUT_ROOT = (PROJECT_ROOT / "outputs").resolve()
SOURCE_ROOT = Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE").resolve()
CURRENT_TEST_SOURCE_ROOT = Path("/Users/yourname/Documents/001DZLtest").resolve()

EXPECTED_PYTHON_LAUNCHER = Path("/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python")
EXPECTED_PYTHON_REALPATH = EXPECTED_PYTHON_LAUNCHER.resolve()
DEFAULT_OUTPUT_DIR = TEST_OUTPUT_ROOT / "stop03-monitor-process-tree-db-safe-v7_20260709_172800"

# This monitor does not load any ML model. Keep these explicit so the script
# can report that model paths are intentionally unused instead of silently unknown.
MODEL_USAGE_POLICY = "not_used_by_monitor_script"
REQUIRED_LOCAL_ASSETS = {
    "project_root": PROJECT_ROOT,
    "test_output_root": TEST_OUTPUT_ROOT,
    "source_root_read_protected": SOURCE_ROOT,
    "current_test_source_root_read_protected": CURRENT_TEST_SOURCE_ROOT,
    "expected_python_launcher": EXPECTED_PYTHON_LAUNCHER,
    "expected_python_realpath": EXPECTED_PYTHON_REALPATH,
}
REQUIRED_DEPENDENCIES = ["sqlite3", "csv", "json", "subprocess"]
OPTIONAL_DEPENDENCIES = ["psutil"]

ALLOWED_OUTPUT_ROOTS = [
    TEST_OUTPUT_ROOT,
    PROJECT_TEST_OUTPUT_ROOT,
    PROJECT_OUTPUT_ROOT,
]

def install_offline_env() -> Dict[str, str]:
    # No network is required by this monitor. These env vars prevent accidental
    # online behavior if a future dependency import tries to initialize telemetry/update checks.
    offline = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "ULTRALYTICS_OFFLINE": "1",
        "NO_ALBUMENTATIONS_UPDATE": "1",
        "YOLO_CONFIG_DIR": str(TEST_OUTPUT_ROOT / "ultralytics-offline-config"),
    }
    for k, v in offline.items():
        os.environ.setdefault(k, v)
    return {k: os.environ.get(k, "") for k in offline}


OFFLINE_ENV = install_offline_env()

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
    PSUTIL_IMPORT_ERROR = ""
except Exception as exc:
    psutil = None  # type: ignore
    PSUTIL_AVAILABLE = False
    PSUTIL_IMPORT_ERROR = repr(exc)


def _dependency_status(name: str, *, optional: bool = False) -> Dict[str, Any]:
    try:
        mod = __import__(name)
        return {"ok": True, "optional": optional, "version": getattr(mod, "__version__", "stdlib"), "error": ""}
    except Exception as exc:
        return {"ok": False, "optional": optional, "version": "", "error": repr(exc)}


def runtime_preflight() -> Dict[str, Any]:
    assets = {}
    for k, p in REQUIRED_LOCAL_ASSETS.items():
        try:
            exists = p.exists()
            size = p.stat().st_size if exists and p.is_file() else None
        except Exception:
            exists = False
            size = None
        assets[k] = {"path": str(p), "exists": bool(exists), "size_bytes": size}
    deps: Dict[str, Dict[str, Any]] = {}
    for name in REQUIRED_DEPENDENCIES:
        deps[name] = _dependency_status(name, optional=False)
    for name in OPTIONAL_DEPENDENCIES:
        deps[name] = _dependency_status(name, optional=True)
    missing_required = [k for k, v in deps.items() if not v.get("ok") and not v.get("optional")]
    blockers: List[str] = []
    current_python_launcher = Path(sys.executable)
    current_python_realpath = current_python_launcher.resolve()
    expected_python_match = (
        current_python_launcher == EXPECTED_PYTHON_LAUNCHER
        or current_python_realpath == EXPECTED_PYTHON_REALPATH
    )
    if not expected_python_match:
        blockers.append("UNEXPECTED_PYTHON_ENV")
    for k in ["project_root", "test_output_root"]:
        if not assets.get(k, {}).get("exists"):
            blockers.append(f"MISSING_REQUIRED_ASSET:{k}")
    if missing_required:
        blockers.append("MISSING_REQUIRED_DEPENDENCIES:" + ",".join(missing_required))
    return {
        "python_executable": str(current_python_launcher),
        "python_realpath": str(current_python_realpath),
        "expected_python": str(EXPECTED_PYTHON_LAUNCHER),
        "expected_python_realpath": str(EXPECTED_PYTHON_REALPATH),
        "expected_python_match": expected_python_match,
        "script_version": SCRIPT_VERSION,
        "expected_script_local": str(PROJECT_ROOT / "scripts/03_stop03_visual_analysis/stop03_1_monitor_process_tree_db_safe_v7_20260709_172800.py"),
        "project_root": str(PROJECT_ROOT),
        "test_output_root": str(TEST_OUTPUT_ROOT),
        "source_root_read_protected": str(SOURCE_ROOT),
        "current_test_source_root_read_protected": str(CURRENT_TEST_SOURCE_ROOT),
        "model_usage_policy": MODEL_USAGE_POLICY,
        "required_local_assets": {k: str(v) for k, v in REQUIRED_LOCAL_ASSETS.items()},
        "assets": assets,
        "dependencies": deps,
        "missing_required_dependencies": missing_required,
        "psutil_available": PSUTIL_AVAILABLE,
        "psutil_import_error": PSUTIL_IMPORT_ERROR,
        "offline_env": OFFLINE_ENV,
        "blockers": blockers,
    }


def assert_preflight_ok() -> Dict[str, Any]:
    pf = runtime_preflight()
    if pf.get("blockers"):
        print(json.dumps({"validation_status": "BLOCKED_PREFLIGHT", "runtime_preflight": pf}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    return pf


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(code: str, detail: str, exit_code: int = 2) -> None:
    print(f"{code}: {detail}", file=sys.stderr)
    raise SystemExit(exit_code)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def validate_output_path(path: Path, *, kind: str, overwrite: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if is_relative_to(resolved, SOURCE_ROOT):
        fail("BLOCKED_OUTPUT_INSIDE_SOURCE_ROOT", f"{kind} path is inside source media root: {resolved}")
    if not any(is_relative_to(resolved, root) for root in ALLOWED_OUTPUT_ROOTS):
        fail(
            "BLOCKED_OUTPUT_OUTSIDE_ALLOWED_ROOTS",
            f"{kind} path is outside allowed output roots: {resolved}",
        )
    if resolved.exists() and not overwrite:
        fail("BLOCKED_OUTPUT_EXISTS", f"{kind} already exists, refusing overwrite: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def validate_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if is_relative_to(resolved, SOURCE_ROOT):
        fail("BLOCKED_OUTPUT_DIR_INSIDE_SOURCE_ROOT", f"output dir is inside source media root: {resolved}")
    if not any(is_relative_to(resolved, root) for root in ALLOWED_OUTPUT_ROOTS):
        fail("BLOCKED_OUTPUT_DIR_OUTSIDE_ALLOWED_ROOTS", f"output dir outside allowed roots: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def init_db(db_path: Path, *, overwrite: bool) -> sqlite3.Connection:
    db_path = validate_output_path(db_path, kind="sqlite", overwrite=overwrite)
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS stop03_resource_monitor_runs (
            run_id TEXT PRIMARY KEY,
            script_version TEXT NOT NULL,
            script_path TEXT NOT NULL,
            script_sha256 TEXT,
            label TEXT NOT NULL,
            target_pid INTEGER NOT NULL,
            monitor_backend TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            output_dir TEXT NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS stop03_resource_monitor_samples (
            run_id TEXT NOT NULL,
            sample_index INTEGER NOT NULL,
            ts TEXT NOT NULL,
            elapsed_seconds REAL NOT NULL,
            target_pid INTEGER NOT NULL,
            process_count INTEGER NOT NULL,
            cpu_percent_total REAL,
            rss_mb_total REAL,
            loadavg_1m REAL,
            backend TEXT NOT NULL,
            note TEXT,
            PRIMARY KEY (run_id, sample_index)
        );

        CREATE TABLE IF NOT EXISTS stop03_resource_monitor_summary (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            label TEXT NOT NULL,
            target_pid INTEGER NOT NULL,
            samples INTEGER NOT NULL,
            elapsed_seconds REAL NOT NULL,
            cpu_percent_max REAL,
            rss_mb_max REAL,
            proc_count_max INTEGER,
            monitor_backend TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            error_message TEXT
        );
        """
    )
    con.commit()
    return con


def local_ps_rows() -> List[Dict[str, Any]]:
    # Local macOS/Linux ps only. No shell. No network.
    cmd = ["/bin/ps", "-axo", "pid=,ppid=,stat=,pcpu=,rss=,command="]
    try:
        cp = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as exc:
        fail("BLOCKED_PS_FALLBACK_FAILED", f"local ps fallback failed: {exc}")
    rows: List[Dict[str, Any]] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            stat = str(parts[2])
            pcpu = float(parts[3])
            rss_kb = float(parts[4])
            command = parts[5] if len(parts) >= 6 else ""
        except Exception:
            continue
        rows.append({"pid": pid, "ppid": ppid, "stat": stat, "pcpu": pcpu, "rss_kb": rss_kb, "command": command})
    return rows


def tree_stats_ps_fallback(pid: int) -> Tuple[bool, Dict[str, Any]]:
    rows = local_ps_rows()
    by_ppid: Dict[int, List[Dict[str, Any]]] = {}
    by_pid: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        by_pid[int(r["pid"])] = r
        by_ppid.setdefault(int(r["ppid"]), []).append(r)
    if pid not in by_pid:
        return False, {
            "process_count": 0,
            "cpu_percent_total": 0.0,
            "rss_mb_total": 0.0,
            "note": "target_pid_not_found",
        }
    if "Z" in str(by_pid[pid].get("stat", "")):
        return False, {
            "process_count": 0,
            "cpu_percent_total": 0.0,
            "rss_mb_total": 0.0,
            "note": "target_pid_zombie",
        }
    stack = [pid]
    seen = set()
    selected: List[Dict[str, Any]] = []
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        r = by_pid.get(cur)
        if r is not None and "Z" not in str(r.get("stat", "")):
            selected.append(r)
        for child in by_ppid.get(cur, []):
            stack.append(int(child["pid"]))
    cpu = sum(float(r.get("pcpu") or 0.0) for r in selected)
    rss_mb = sum(float(r.get("rss_kb") or 0.0) for r in selected) / 1024.0
    return True, {
        "process_count": len(selected),
        "cpu_percent_total": round(cpu, 4),
        "rss_mb_total": round(rss_mb, 4),
        "note": "ps_fallback_no_per_process_io",
    }


def tree_stats_psutil(pid: int) -> Tuple[bool, Dict[str, Any]]:
    assert psutil is not None
    try:
        root = psutil.Process(pid)
        if root.status() == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
            return False, {
                "process_count": 0,
                "cpu_percent_total": 0.0,
                "rss_mb_total": 0.0,
                "note": "target_pid_zombie",
            }
        procs = [root] + root.children(recursive=True)
    except psutil.NoSuchProcess:
        return False, {
            "process_count": 0,
            "cpu_percent_total": 0.0,
            "rss_mb_total": 0.0,
            "note": "target_pid_not_found",
        }
    except Exception as exc:
        return False, {
            "process_count": 0,
            "cpu_percent_total": 0.0,
            "rss_mb_total": 0.0,
            "note": f"psutil_error:{exc}",
        }
    cpu = 0.0
    rss = 0
    alive = []
    for p in procs:
        try:
            if not p.is_running():
                continue
            try:
                if p.status() == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
                    continue
            except Exception:
                pass
            alive.append(p)
            cpu += float(p.cpu_percent(interval=None))
            rss += int(p.memory_info().rss)
        except Exception:
            continue
    return True, {
        "process_count": len(alive),
        "cpu_percent_total": round(cpu, 4),
        "rss_mb_total": round(rss / 1024 / 1024, 4),
        "note": "psutil",
    }


def tree_stats(pid: int) -> Tuple[bool, Dict[str, Any], str]:
    if PSUTIL_AVAILABLE:
        alive, stats = tree_stats_psutil(pid)
        return alive, stats, "psutil"
    alive, stats = tree_stats_ps_fallback(pid)
    return alive, stats, "ps_fallback"


def insert_run(con: sqlite3.Connection, row: Dict[str, Any]) -> None:
    con.execute(
        """
        INSERT INTO stop03_resource_monitor_runs
        (run_id, script_version, script_path, script_sha256, label, target_pid, monitor_backend, status,
         started_at, finished_at, output_dir, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["run_id"], row["script_version"], row["script_path"], row.get("script_sha256"),
            row["label"], row["target_pid"], row["monitor_backend"], row["status"],
            row["started_at"], row.get("finished_at"), row["output_dir"], row.get("error_message"),
        ),
    )
    con.commit()


def update_run_done(con: sqlite3.Connection, run_id: str, status: str, error_message: Optional[str]) -> None:
    con.execute(
        """
        UPDATE stop03_resource_monitor_runs
        SET status=?, finished_at=?, error_message=?
        WHERE run_id=?
        """,
        (status, now_iso(), error_message, run_id),
    )
    con.commit()


def insert_sample(con: sqlite3.Connection, run_id: str, row: Dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO stop03_resource_monitor_samples
        (run_id, sample_index, ts, elapsed_seconds, target_pid, process_count, cpu_percent_total,
         rss_mb_total, loadavg_1m, backend, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["sample_index"], row["ts"], row["elapsed_seconds"], row["target_pid"],
            row["process_count"], row.get("cpu_percent_total"), row.get("rss_mb_total"),
            row.get("loadavg_1m"), row["backend"], row.get("note"),
        ),
    )
    con.commit()


def insert_summary(con: sqlite3.Connection, summary: Dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO stop03_resource_monitor_summary
        (run_id, status, label, target_pid, samples, elapsed_seconds, cpu_percent_max,
         rss_mb_max, proc_count_max, monitor_backend, started_at, finished_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary["run_id"], summary["status"], summary["label"], summary["target_pid"],
            summary["samples"], summary["elapsed_seconds"], summary.get("cpu_percent_max"),
            summary.get("rss_mb_max"), summary.get("proc_count_max"), summary["monitor_backend"],
            summary["started_at"], summary["finished_at"], summary.get("error_message"),
        ),
    )
    con.commit()


def export_csv(con: sqlite3.Connection, run_id: str, csv_path: Path, *, overwrite: bool) -> None:
    csv_path = validate_output_path(csv_path, kind="csv", overwrite=overwrite)
    cur = con.execute(
        """
        SELECT sample_index, ts, elapsed_seconds, target_pid, process_count,
               cpu_percent_total, rss_mb_total, loadavg_1m, backend, note
        FROM stop03_resource_monitor_samples
        WHERE run_id=?
        ORDER BY sample_index
        """,
        (run_id,),
    )
    fields = [d[0] for d in cur.description]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        w.writerows(cur.fetchall())


def export_json(summary: Dict[str, Any], json_path: Path, *, overwrite: bool) -> None:
    json_path = validate_output_path(json_path, kind="json", overwrite=overwrite)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def monitor_pid(
    *,
    pid: int,
    label: str,
    output_dir: Path,
    interval: float,
    duration_seconds: Optional[float],
    overwrite: bool,
) -> Dict[str, Any]:
    preflight = assert_preflight_ok()
    if interval < 1.0:
        fail("BLOCKED_INTERVAL_TOO_SMALL", f"interval must be >= 1.0 seconds, got {interval}")
    output_dir = validate_output_dir(output_dir)
    db_path = output_dir / "resource_monitor.sqlite"
    csv_path = output_dir / "resource_samples.csv"
    json_path = output_dir / "resource_summary.json"
    script_path = Path(__file__).resolve()
    script_hash = file_sha256(script_path) if script_path.exists() else None
    run_id = f"stop03_resource_monitor_{SCRIPT_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}"

    con = init_db(db_path, overwrite=overwrite)
    started = now_iso()
    _, _, initial_backend = tree_stats(pid)
    insert_run(con, {
        "run_id": run_id,
        "script_version": SCRIPT_VERSION,
        "script_path": str(script_path),
        "script_sha256": script_hash,
        "label": label,
        "target_pid": pid,
        "monitor_backend": initial_backend,
        "status": "running",
        "started_at": started,
        "output_dir": str(output_dir),
    })

    sample_index = 0
    cpu_max: Optional[float] = None
    rss_max: Optional[float] = None
    proc_max: Optional[int] = None
    status = "completed"
    error_message: Optional[str] = None
    t0 = time.time()
    final_backend = initial_backend

    try:
        while True:
            elapsed = time.time() - t0
            alive, stats, backend = tree_stats(pid)
            final_backend = backend
            load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
            row = {
                "sample_index": sample_index,
                "ts": now_iso(),
                "elapsed_seconds": round(elapsed, 4),
                "target_pid": pid,
                "process_count": int(stats.get("process_count") or 0),
                "cpu_percent_total": stats.get("cpu_percent_total"),
                "rss_mb_total": stats.get("rss_mb_total"),
                "loadavg_1m": load1,
                "backend": backend,
                "note": stats.get("note"),
            }
            insert_sample(con, run_id, row)
            sample_index += 1
            cpu_val = row.get("cpu_percent_total")
            rss_val = row.get("rss_mb_total")
            proc_val = row.get("process_count")
            if cpu_val is not None:
                cpu_max = max(cpu_max or 0.0, float(cpu_val))
            if rss_val is not None:
                rss_max = max(rss_max or 0.0, float(rss_val))
            if proc_val is not None:
                proc_max = max(proc_max or 0, int(proc_val))

            if not alive:
                status = "target_exited"
                break
            if duration_seconds is not None and elapsed >= duration_seconds:
                status = "duration_reached"
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        status = "interrupted"
        error_message = "KeyboardInterrupt"
    except Exception as exc:
        status = "failed"
        error_message = repr(exc)
    finally:
        finished = now_iso()
        elapsed_total = round(time.time() - t0, 4)
        summary = {
            "run_id": run_id,
            "status": status,
            "label": label,
            "target_pid": pid,
            "samples": sample_index,
            "elapsed_seconds": elapsed_total,
            "cpu_percent_max": cpu_max,
            "rss_mb_max": rss_max,
            "proc_count_max": proc_max,
            "monitor_backend": final_backend,
            "psutil_available": PSUTIL_AVAILABLE,
            "started_at": started,
            "finished_at": finished,
            "error_message": error_message,
            "outputs": {
                "sqlite": str(db_path),
                "csv": str(csv_path),
                "json": str(json_path),
            },
            "runtime_preflight": preflight,
            "safety": {
                "network": "blocked_by_offline_env_not_used_by_monitor",
                "download": "not_used",
                "dependency_install": "not_used",
                "source_media_read": "not_used",
                "source_media_write": "blocked_by_output_path_guard",
                "model_loading": MODEL_USAGE_POLICY,
            },
        }
        insert_summary(con, summary)
        update_run_done(con, run_id, status, error_message)
        export_csv(con, run_id, csv_path, overwrite=overwrite)
        export_json(summary, json_path, overwrite=overwrite)
        con.close()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary


def run_self_smoke(overwrite: bool) -> int:
    output_dir = TEST_OUTPUT_ROOT / f"stop03-monitor-db-safe-{SCRIPT_VERSION}-smoke"
    if output_dir.exists() and overwrite:
        # Do not delete directory. Only allow DB/CSV/JSON overwrite through file guards.
        pass
    cmd = [sys.executable, "-c", "import time; time.sleep(6)"]
    proc = subprocess.Popen(cmd)
    try:
        monitor_pid(
            pid=int(proc.pid),
            label="self_smoke",
            output_dir=output_dir,
            interval=1.0,
            duration_seconds=10.0,
            overwrite=overwrite,
        )
    finally:
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Local offline DB-first process-tree resource monitor.")
    ap.add_argument("--self-smoke", action="store_true", help="Run built-in self test. No PID input needed.")
    ap.add_argument("--preflight-only", action="store_true", help="Print runtime/path/dependency/offline preflight and exit without monitoring.")
    ap.add_argument("--pid", type=int, help="Target PID to monitor.")
    ap.add_argument("--label", default="manual", help="Run label.")
    ap.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR), help="Allowed output directory for SQLite/CSV/JSON. Defaults to fixed project test-output path.")
    ap.add_argument("--interval", type=float, default=2.0, help="Sampling interval seconds. Must be >= 1.")
    ap.add_argument("--duration-seconds", type=float, default=None, help="Optional max monitoring duration.")
    ap.add_argument("--overwrite", action="store_true", help="Allow overwriting existing SQLite/CSV/JSON output files.")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.preflight_only:
        pf = runtime_preflight()
        status = "PASS" if not pf.get("blockers") else "BLOCKED_PREFLIGHT"
        print(json.dumps({"validation_status": status, "runtime_preflight": pf}, ensure_ascii=False, indent=2))
        return 0 if status == "PASS" else 2
    if args.self_smoke:
        return run_self_smoke(overwrite=True)
    if args.pid is None:
        fail("BLOCKED_MISSING_PID", "use --self-smoke or provide --pid")
    monitor_pid(
        pid=int(args.pid),
        label=str(args.label),
        output_dir=Path(args.output_dir),
        interval=float(args.interval),
        duration_seconds=args.duration_seconds,
        overwrite=bool(args.overwrite),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
