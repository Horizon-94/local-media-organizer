#!/bin/bash
set -Eeuo pipefail

PROJECT="/Users/yourname/Documents/AI-Local/media-archive-clean"
LATEST_POINTER="$PROJECT/logs/stop03_3g_full336_latest.txt"
BASE="${1:-}"

if [ -z "$BASE" ]; then
  if [ ! -f "$LATEST_POINTER" ]; then
    echo "BLOCKED_LATEST_POINTER_MISSING=$LATEST_POINTER"
    exit 4
  fi
  BASE="$(cat "$LATEST_POINTER")"
fi

ENGINE_LOG="$BASE/logs/full336_engine.log"
PID_FILE="$BASE/run/full336_engine.pid"
RUN_DIR_FILE="$BASE/run/engine_run_dir.txt"
FINAL_REPORT="$BASE/reports/fullflow_336_report.json"

if [ ! -f "$PID_FILE" ]; then
  echo "BLOCKED_PID_FILE_MISSING=$PID_FILE"
  exit 4
fi

PID="$(cat "$PID_FILE")"
echo "Stop03-3G 全量 336 监控"
echo "BASE=$BASE"
echo "ENGINE_PID=$PID"
echo "等待 benchmark.sqlite..."

RUN_DIR=""
while [ -z "$RUN_DIR" ]; do
  if [ -f "$RUN_DIR_FILE" ]; then
    RUN_DIR="$(cat "$RUN_DIR_FILE")"
  else
    LINE="$(grep '^RUN_DIR=' "$ENGINE_LOG" 2>/dev/null | tail -1 || true)"
    RUN_DIR="${LINE#RUN_DIR=}"
  fi

  if [ -n "$RUN_DIR" ]; then
    break
  fi

  if ! kill -0 "$PID" 2>/dev/null; then
    echo "RUNNER_EXITED_BEFORE_RUN_DIR_DISCOVERED"
    tail -120 "$ENGINE_LOG" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done

DB="$RUN_DIR/benchmark.sqlite"
ENGINE_REPORT="$RUN_DIR/reports/final_report.json"

while [ ! -f "$DB" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "RUNNER_EXITED_BEFORE_BENCHMARK_DB_CREATED"
    tail -120 "$ENGINE_LOG" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done

python3 - "$DB" "$PID" "$ENGINE_LOG" "$ENGINE_REPORT" "$FINAL_REPORT" <<'PY'
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

db_path = Path(sys.argv[1])
pid = int(sys.argv[2])
engine_log = Path(sys.argv[3])
engine_report_path = Path(sys.argv[4])
fullflow_report_path = Path(sys.argv[5])

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None

def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}

def rows_as_dicts(con: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, args)]

while True:
    running = alive(pid)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_count = len(list(con.execute("PRAGMA foreign_key_check")))

            item_counts: dict[str, int] = {}
            total = 0
            if table_exists(con, "benchmark_items"):
                total = int(con.execute("SELECT COUNT(*) FROM benchmark_items").fetchone()[0])
                item_counts = {
                    str(row[0]): int(row[1])
                    for row in con.execute(
                        "SELECT status,COUNT(*) FROM benchmark_items GROUP BY status"
                    )
                }

            result_counts: dict[str, int] = {}
            result_total = 0
            if table_exists(con, "benchmark_results"):
                result_total = int(con.execute("SELECT COUNT(*) FROM benchmark_results").fetchone()[0])
                result_counts = {
                    str(row[0]): int(row[1])
                    for row in con.execute(
                        "SELECT result_status,COUNT(*) FROM benchmark_results GROUP BY result_status"
                    )
                }

            workers: list[dict[str, Any]] = []
            if table_exists(con, "worker_sessions"):
                wc = columns(con, "worker_sessions")
                wanted = [
                    name for name in (
                        "worker_id", "model_load_count", "items_completed",
                        "items_failed", "exitcode", "model_load_seconds",
                        "peak_rss_mb", "idle_seconds", "started_at", "finished_at"
                    ) if name in wc
                ]
                if wanted:
                    workers = rows_as_dicts(
                        con,
                        "SELECT " + ",".join(f'"{name}"' for name in wanted) +
                        " FROM worker_sessions ORDER BY worker_id"
                    )

            current: list[dict[str, Any]] = []
            if table_exists(con, "benchmark_attempts"):
                ac = columns(con, "benchmark_attempts")
                if {"candidate_id", "worker_id"}.issubset(ac):
                    select_cols = [
                        name for name in (
                            "worker_id", "candidate_id", "attempt_number",
                            "retry_round", "status", "started_at", "finished_at"
                        ) if name in ac
                    ]
                    where = ""
                    if "status" in ac:
                        where = " WHERE status='running'"
                    current = rows_as_dicts(
                        con,
                        "SELECT " + ",".join(f'"{name}"' for name in select_cols) +
                        " FROM benchmark_attempts" + where +
                        " ORDER BY worker_id"
                    )

            errors = 0
            if table_exists(con, "benchmark_errors"):
                errors = int(con.execute("SELECT COUNT(*) FROM benchmark_errors").fetchone()[0])
        finally:
            con.close()
    except Exception as exc:
        integrity = f"read_error:{type(exc).__name__}:{exc}"
        foreign_count = -1
        item_counts = {}
        result_counts = {}
        total = 0
        result_total = 0
        workers = []
        current = []
        errors = -1

    done = sum(result_counts.values())
    percent = (done / 336 * 100.0) if 336 else 0.0

    print("\033[2J\033[H", end="")
    print("=" * 92)
    print("Stop03-3G FULL 336 | 三个 worker 共同处理一个 336 条动态队列")
    print("=" * 92)
    print(f"PID={pid} 存活={running}")
    print(f"DB={db_path}")
    print(f"总候选={total}/336 已生成结果={result_total}/336 完成率={percent:.2f}%")
    print(f"item状态={json.dumps(item_counts, ensure_ascii=False)}")
    print(f"result状态={json.dumps(result_counts, ensure_ascii=False)}")
    print(f"SQLite integrity={integrity} foreign_key_errors={foreign_count} errors={errors}")
    print()
    print("Worker 状态：")
    if workers:
        for row in workers:
            print("  " + json.dumps(row, ensure_ascii=False))
    else:
        print("  尚未记录 worker_sessions")
    print()
    print("当前运行项：")
    if current:
        for row in current:
            print("  " + json.dumps(row, ensure_ascii=False))
    else:
        print("  当前无可见 running attempt，可能正在加载模型或刚完成事务。")
    print()
    print("日志末尾：")
    if engine_log.is_file():
        try:
            tail = subprocess.run(
                ["tail", "-8", str(engine_log)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            print(tail.rstrip())
        except Exception:
            pass

    if not running:
        break
    time.sleep(3)

print()
print("=" * 92)
print("主运行进程已结束，等待最终数据库闭环报告")
print("=" * 92)

for _ in range(120):
    if fullflow_report_path.is_file():
        break
    time.sleep(1)

if engine_report_path.is_file():
    print("Qwen 原始报告：")
    try:
        report = json.loads(engine_report_path.read_text(encoding="utf-8"))
        for key in (
            "status", "candidate_total", "success_count", "review_count",
            "failed_count", "pending", "running", "persistent_worker_count",
            "dynamic_queue", "worker_model_load_count_one",
            "duplicate_execution_keys", "benchmark_db_integrity_check",
            "benchmark_db_foreign_key_check", "source_db_unchanged"
        ):
            print(f"{key}={report.get(key)}")
    except Exception as exc:
        print(f"engine_report_read_error={type(exc).__name__}:{exc}")

if fullflow_report_path.is_file():
    print()
    print("数据库闭环报告：")
    report = json.loads(fullflow_report_path.read_text(encoding="utf-8"))
    for key in (
        "validation_status", "source_db_unchanged",
        "validation_db_integrity", "writeback_error"
    ):
        print(f"{key}={report.get(key)}")
    print(f"report={fullflow_report_path}")
else:
    print(f"FULLFLOW_REPORT_NOT_READY={fullflow_report_path}")
PY
