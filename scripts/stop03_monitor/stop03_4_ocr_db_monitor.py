#!/usr/bin/env python3
"""Read-only terminal monitor for Stop03-4 OCR central DB runs."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any


TERMINAL = ("success", "no_text", "failed", "input_fingerprint_mismatch", "review")


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def select_run_id(db: Path, requested: str) -> str:
    if requested:
        return requested
    with connect_ro(db) as con:
        row = con.execute(
            "SELECT run_id FROM stop03_4_ocr_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("no_stop03_4_ocr_run")
    return str(row["run_id"])


def read_state(db: Path, run_id: str) -> dict[str, Any]:
    with connect_ro(db) as con:
        run = con.execute(
            "SELECT * FROM stop03_4_ocr_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError("ocr_run_not_found")
        counts = {
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_4_ocr_run_items
                   WHERE run_id=? GROUP BY status""",
                (run_id,),
            )
        }
        running = [
            dict(row)
            for row in con.execute(
                """SELECT candidate_id,attempt_count,started_at,claimed_by_worker
                   FROM stop03_4_ocr_run_items
                   WHERE run_id=? AND status='running' ORDER BY started_at""",
                (run_id,),
            )
        ]
        recent = [
            dict(row)
            for row in con.execute(
                """SELECT i.candidate_id,i.status,i.attempt_count,i.finished_at,
                          a.elapsed_seconds,a.worker_pid
                   FROM stop03_4_ocr_run_items i
                   LEFT JOIN stop03_4_ocr_attempts a
                     ON a.run_item_id=i.run_item_id
                    AND a.attempt_number=i.attempt_count
                   WHERE i.run_id=? AND i.finished_at IS NOT NULL
                   ORDER BY i.finished_at DESC LIMIT 12""",
                (run_id,),
            )
        ]
        worker_rows = [
            dict(row)
            for row in con.execute(
                """SELECT worker_pid,COUNT(*) completed,
                          ROUND(AVG(elapsed_seconds),2) avg_seconds,
                          ROUND(MAX(elapsed_seconds),2) max_seconds
                   FROM stop03_4_ocr_attempts
                   WHERE run_id=? GROUP BY worker_pid ORDER BY worker_pid""",
                (run_id,),
            )
        ]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = len(list(con.execute("PRAGMA foreign_key_check")))
    total = int(run["candidate_count"])
    completed = sum(counts.get(status, 0) for status in TERMINAL)
    return {
        "run": dict(run),
        "counts": counts,
        "running_items": running,
        "recent": recent,
        "workers": worker_rows,
        "completed": completed,
        "remaining": total - completed,
        "percent": 100.0 * completed / total if total else 100.0,
        "integrity": integrity,
        "fk_errors": fk_errors,
    }


def process_rows() -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,pcpu=,pmem=,rss=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in proc.stdout.splitlines():
        match = re.match(
            r"\s*(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(.*)",
            line,
        )
        if not match:
            continue
        command = match.group(7)
        if "stop03_4_ocr_db_orchestrator_v1.py" not in command and "ForkProcess" not in command:
            continue
        rows.append(
            {
                "pid": int(match.group(1)),
                "ppid": int(match.group(2)),
                "elapsed": match.group(3),
                "cpu": float(match.group(4)),
                "mem": float(match.group(5)),
                "rss_gb": int(match.group(6)) / 1024 / 1024,
                "command": command,
            }
        )
    return rows


def age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    try:
        value = datetime.fromisoformat(timestamp)
        now = datetime.now(value.tzinfo)
        return max(0.0, (now - value).total_seconds())
    except Exception:
        return 0.0


def render(state: dict[str, Any], processes: list[dict[str, Any]]) -> None:
    run = state["run"]
    counts = state["counts"]
    os.system("clear")
    print("Stop03-4 OCR 中心数据库实时监控（只读，Control+C 仅退出监控）")
    print("=" * 100)
    print(f"时间：{datetime.now().isoformat(timespec='seconds')}")
    print(
        f"run_id：{run['run_id']}  类型：{run['run_kind']}  "
        f"状态：{run['status']}  workers={run['workers']}"
    )
    print(
        f"总数={run['candidate_count']} 完成={state['completed']} "
        f"pending={counts.get('pending',0)} running={counts.get('running',0)} "
        f"success={counts.get('success',0)} no_text={counts.get('no_text',0)} "
        f"failed={counts.get('failed',0)+counts.get('input_fingerprint_mismatch',0)+counts.get('review',0)}"
    )
    print(f"完成率={state['percent']:.2f}%  剩余={state['remaining']}  reused={run['reused_count']}")
    print()
    print("当前正在处理：")
    for row in state["running_items"]:
        print(
            f"  {row['claimed_by_worker']}  {row['candidate_id']}  "
            f"attempt={row['attempt_count']}  已运行≈{age_seconds(row['started_at']):.1f}s"
        )
    if not state["running_items"]:
        print("  无")
    print()
    print("OCR worker 完成统计：")
    for row in state["workers"]:
        print(
            f"  pid={row['worker_pid']} completed={row['completed']} "
            f"avg={row['avg_seconds']}s max={row['max_seconds']}s"
        )
    if not state["workers"]:
        print("  暂无完成记录")
    print()
    print(
        f"进程合计：CPU={sum(p['cpu'] for p in processes):.1f}% "
        f"RSS≈{sum(p['rss_gb'] for p in processes):.2f}GB"
    )
    print("PID      PPID     ELAPSED    CPU%    MEM%    RSS-GB")
    for row in processes:
        print(
            f"{row['pid']:<8} {row['ppid']:<8} {row['elapsed']:<10} "
            f"{row['cpu']:<7.1f} {row['mem']:<7.1f} {row['rss_gb']:.2f}"
        )
    print()
    print("最近完成：")
    for row in reversed(state["recent"]):
        print(
            f"  {row['finished_at']} pid={row['worker_pid']} {row['status']:<8} "
            f"{float(row['elapsed_seconds'] or 0):.2f}s attempt={row['attempt_count']} "
            f"{row['candidate_id']}"
        )
    print()
    print(f"SQLite integrity={state['integrity']} foreign_key_errors={state['fk_errors']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(
            "/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite"
        ),
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    args = parser.parse_args()
    run_id = select_run_id(args.db, args.run_id)
    while True:
        state = read_state(args.db, run_id)
        render(state, process_rows())
        if state["run"]["status"] in {"success", "failed", "cancelled"}:
            return 0
        time.sleep(max(1.0, args.refresh_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
