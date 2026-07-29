#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
LATEST_POINTER = PROJECT / "logs/stop03_3g_full336_standalone_latest.txt"


def clear() -> None:
    print("\033[2J\033[H", end="")


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def dict_rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, args)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default="")
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
    else:
        if not LATEST_POINTER.is_file():
            print(f"BLOCKED_LATEST_POINTER_MISSING={LATEST_POINTER}")
            return 4
        run_dir = Path(
            LATEST_POINTER.read_text(encoding="utf-8").strip()
        ).expanduser().resolve()

    db_path = run_dir / "database" / "media_archive_stop03_3g_full336.sqlite"
    report_path = run_dir / "reports" / "final_report.json"

    while not db_path.is_file():
        clear()
        print("Stop03-3G full336 monitor")
        print(f"RUN_DIR={run_dir}")
        print(f"等待数据库：{db_path}")
        time.sleep(1)

    while True:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            run_row = con.execute(
                "SELECT * FROM stop03_3g_run ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if run_row is None:
                time.sleep(1)
                continue
            run = dict(run_row)
            run_id = str(run["run_id"])

            candidate_counts = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    """
                    SELECT status,COUNT(*) FROM stop03_3g_candidate
                    WHERE run_id=? GROUP BY status
                    """,
                    (run_id,),
                )
            }
            result_counts = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    """
                    SELECT result_status,COUNT(*) FROM stop03_3g_result
                    WHERE run_id=? GROUP BY result_status
                    """,
                    (run_id,),
                )
            }
            workers = dict_rows(
                con,
                """
                SELECT worker_id,pid,lifecycle,model_load_count,
                       processor_load_count,assigned_count,success_count,
                       review_count,failed_count,current_candidate_id,
                       current_selected_order,active_memory_gb,cache_memory_gb,
                       peak_memory_gb,rss_mb,cpu_percent,heartbeat_at,exit_code
                FROM stop03_3g_worker
                WHERE run_id=? ORDER BY worker_id
                """,
                (run_id,),
            )
            recent = dict_rows(
                con,
                """
                SELECT worker_id,candidate_id,result_status,generated_tokens,
                       elapsed_seconds,degeneration_detected,truncation_detected,
                       inferred_finish_reason,created_at
                FROM stop03_3g_result
                WHERE run_id=?
                ORDER BY created_at DESC LIMIT 12
                """,
                (run_id,),
            )
            telemetry = con.execute(
                """
                SELECT * FROM stop03_3g_system_telemetry
                WHERE run_id=? ORDER BY telemetry_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_count = len(list(con.execute("PRAGMA foreign_key_check")))
        finally:
            con.close()

        success = result_counts.get("success", 0)
        review = result_counts.get("review", 0)
        failed = result_counts.get("failed", 0)
        completed = success + review + failed
        percent = completed / 336 * 100.0

        clear()
        print("=" * 100)
        print("Stop03-3G FULL 336 | 3个常驻worker共同处理336条动态队列")
        print("=" * 100)
        print(f"RUN_DIR={run_dir}")
        print(f"STATUS={run.get('status')}")
        print(
            f"完成={completed}/336 ({percent:.2f}%) "
            f"SUCCESS={success} REVIEW={review} FAILED={failed} "
            f"PENDING={candidate_counts.get('pending', 0)} "
            f"RUNNING={candidate_counts.get('running', 0)}"
        )
        print(
            f"backend={run.get('generation_backend')} "
            f"max_tokens={run.get('max_tokens')} workers={run.get('workers')}"
        )
        print(
            f"SQLite integrity={integrity} "
            f"foreign_key_errors={foreign_count}"
        )
        if telemetry is not None:
            tel = dict(telemetry)
            print(
                f"worker_rss_sum_mb={tel.get('worker_rss_sum_mb')} "
                f"memory_pressure_percent={tel.get('memory_pressure_percent')} "
                f"swap_used_bytes={tel.get('swap_used_bytes')}"
            )

        print()
        print("Worker状态：")
        for row in workers:
            print(
                f"  W{row['worker_id']} pid={row['pid']} "
                f"state={row['lifecycle']} "
                f"load={row['model_load_count']}/{row['processor_load_count']} "
                f"assigned={row['assigned_count']} "
                f"S/R/F={row['success_count']}/{row['review_count']}/{row['failed_count']} "
                f"current={row['current_selected_order']}:{row['current_candidate_id']} "
                f"CPU={row['cpu_percent']} RSS={row['rss_mb']}MB "
                f"MLX={row['active_memory_gb']}/{row['cache_memory_gb']}/{row['peak_memory_gb']}GB "
                f"heartbeat={row['heartbeat_at']}"
            )

        print()
        print("最近12条：")
        for row in recent:
            print(
                f"  W{row['worker_id']} {row['candidate_id']} "
                f"{str(row['result_status']).upper()} "
                f"tokens={row['generated_tokens']} "
                f"elapsed={float(row['elapsed_seconds'] or 0):.2f}s "
                f"finish={row['inferred_finish_reason']} "
                f"deg={row['degeneration_detected']} "
                f"trunc={row['truncation_detected']}"
            )

        if report_path.is_file():
            print()
            print("=" * 100)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            print("最终报告已生成")
            print(f"VALIDATION_STATUS={report.get('validation_status')}")
            print(f"SOURCE_DB_UNCHANGED={report.get('source_db_unchanged')}")
            print(f"REPORT={report_path}")
            return 0 if report.get("validation_status") == "PASS" else 2

        time.sleep(max(1.0, args.refresh_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
