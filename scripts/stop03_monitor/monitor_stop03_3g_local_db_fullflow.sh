#!/bin/bash
set -Eeuo pipefail

PROJECT="/Users/yourname/Documents/AI-Local/media-archive-clean"
LATEST_FILE="$PROJECT/logs/stop03_3g_latest_out.txt"
ENGINE_MONITOR="$PROJECT/scripts/stop03_monitor/stop03_3f_batch_w3x75_monitor.py"

BASE="${1:-}"

if [ -z "$BASE" ]; then
  if [ ! -f "$LATEST_FILE" ]; then
    echo "BLOCKED_LATEST_OUTPUT_POINTER_MISSING=$LATEST_FILE"
    exit 4
  fi
  BASE="$(cat "$LATEST_FILE")"
fi

STATE_DB="$BASE/qwen_run/run/stop03_3f_w3x75_state.sqlite"
PID_FILE="$BASE/run/qwen_runner.pid"
FULLFLOW_REPORT="$BASE/reports/fullflow_report.json"
VALIDATION_DB="$BASE/database/media_archive_stop03_3g_validation.sqlite"

if [ ! -f "$ENGINE_MONITOR" ]; then
  echo "BLOCKED_MONITOR_PROGRAM_MISSING=$ENGINE_MONITOR"
  exit 4
fi

if [ ! -f "$PID_FILE" ]; then
  echo "BLOCKED_PID_FILE_MISSING=$PID_FILE"
  exit 4
fi

PID="$(cat "$PID_FILE")"

echo "Stop03-3G 监控"
echo "BASE=$BASE"
echo "runner_pid=$PID"
echo "等待状态数据库：$STATE_DB"

while [ ! -f "$STATE_DB" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "RUNNER_EXITED_BEFORE_STATE_DB_CREATED"
    if [ -f "$BASE/logs/qwen_runner.log" ]; then
      tail -100 "$BASE/logs/qwen_runner.log"
    fi
    exit 1
  fi
  sleep 1
done

set +e
python3 "$ENGINE_MONITOR" \
  --db "$STATE_DB" \
  --pid-file "$PID_FILE" \
  --refresh-seconds 3
MONITOR_EXIT=$?
set -e

echo
echo "== Qwen监控结束，等待全流程写回报告 =="

for _ in $(seq 1 120); do
  if [ -f "$FULLFLOW_REPORT" ]; then
    break
  fi
  sleep 1
done

if [ ! -f "$FULLFLOW_REPORT" ]; then
  echo "FULLFLOW_REPORT_NOT_READY=$FULLFLOW_REPORT"
  echo "查看主程序日志：$BASE/logs/qwen_runner.log"
  exit "$MONITOR_EXIT"
fi

python3 - "$FULLFLOW_REPORT" "$VALIDATION_DB" <<'PY'
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
db_path = Path(sys.argv[2])
report = json.loads(report_path.read_text(encoding="utf-8"))

print("=" * 72)
print("Stop03-3G 数据库闭环最终结果")
print("=" * 72)
print("validation_status:", report.get("validation_status"))
print("runner_exit_code:", report.get("runner_exit_code"))
print("source_db_unchanged:", report.get("source_db_unchanged"))
print("copied_table_count:", report.get("copied_table_count"))
print("copied_row_count:", report.get("copied_row_count"))
print("validation_db_integrity:", report.get("validation_db_integrity"))
print("foreign_key_errors:", len(report.get("validation_db_foreign_key_errors") or []))
print("writeback_error:", report.get("writeback_error"))
print("fullflow_report:", report_path)
print("validation_db:", db_path)

if db_path.is_file():
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT source_table, copied_table, copied_row_count
            FROM stop03_3g_state_table_inventory
            ORDER BY copied_row_count DESC, source_table
            """
        ).fetchall()
        print()
        print("写回表清单：")
        for source_table, copied_table, count in rows:
            print(f"  {source_table} -> {copied_table}: {count}")
    finally:
        con.close()
PY

exit "$MONITOR_EXIT"
