#!/bin/bash
set -Eeuo pipefail

PROJECT="/Users/yourname/Documents/AI-Local/media-archive-clean"
PY="/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python"
SOURCE_DB="$PROJECT/media_archive.sqlite"
ENGINE="$PROJECT/scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_batch_w3x75_validation_v1.py"
ENGINE_MONITOR="$PROJECT/scripts/stop03_monitor/stop03_3f_batch_w3x75_monitor.py"
CONFIG="$PROJECT/configs/stop03_3_qwenvl_db_v1.json"
PROMPT="$PROJECT/configs/qwenvl_prompt_v2_384.txt"
MODEL="/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit"
TEST_ROOT="/Users/yourname/Documents/AI-Local/test-output"

STAMP="$(date +%Y%m%d_%H%M%S)"
BASE="$TEST_ROOT/stop03_3g_local_db_fullflow_$STAMP"
VALIDATION_DB="$BASE/database/media_archive_stop03_3g_validation.sqlite"
QWEN_OUT="$BASE/qwen_run"
STATE_DB="$QWEN_OUT/run/stop03_3f_w3x75_state.sqlite"
RUNNER_LOG="$BASE/logs/qwen_runner.log"
PID_FILE="$BASE/run/qwen_runner.pid"
EXIT_FILE="$BASE/run/qwen_runner.exit_code"
SOURCE_HASH_BEFORE_FILE="$BASE/run/source_db_sha256_before.txt"
FULLFLOW_REPORT="$BASE/reports/fullflow_report.json"
LATEST_FILE="$PROJECT/logs/stop03_3g_latest_out.txt"

for REQUIRED in \
  "$PY" \
  "$SOURCE_DB" \
  "$ENGINE" \
  "$ENGINE_MONITOR" \
  "$CONFIG" \
  "$PROMPT" \
  "$MODEL/config.json" \
  "$MODEL/model.safetensors" \
  "$PROJECT/docs/model_registry/LOCAL_MODEL_REGISTRY.md" \
  "$PROJECT/docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md"
do
  if [ ! -e "$REQUIRED" ]; then
    echo "BLOCKED_MISSING_PATH=$REQUIRED"
    exit 4
  fi
done

if /usr/bin/pgrep -f 'stop03_3f_qwenvl_batch_w3x75_validation_v1\.py' >/dev/null 2>&1; then
  echo "BLOCKED_EXISTING_STOP03_3F_PROCESS"
  /usr/bin/pgrep -fl 'stop03_3f_qwenvl_batch_w3x75_validation_v1\.py' || true
  exit 5
fi

mkdir -p "$BASE/database" "$BASE/run" "$BASE/logs" "$BASE/reports" "$PROJECT/logs"

printf '%s\n' "$BASE" > "$LATEST_FILE"
printf '%s\n' "$BASE" > "$BASE/run/output_root.txt"

echo "============================================================"
echo "Stop03-3G 本地数据库全流程真实验证"
echo "只读中心库 -> SQLite一致性副本 -> Qwen3-VL 3×75 -> 状态库 -> 写回测试副本 -> 反查"
echo "BASE=$BASE"
echo "中心数据库不会被写入。"
echo "============================================================"

"$PY" - "$SOURCE_DB" "$SOURCE_HASH_BEFORE_FILE" <<'PY'
from __future__ import annotations
import hashlib
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])

h = hashlib.sha256()
with source.open("rb") as f:
    for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
        h.update(chunk)

digest = h.hexdigest()
output.write_text(digest + "\n", encoding="utf-8")
print(f"source_db_sha256_before={digest}")
PY

echo "== 创建中心数据库一致性测试副本 =="
"$PY" - "$SOURCE_DB" "$VALIDATION_DB" <<'PY'
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)

if target.exists():
    raise SystemExit(f"BLOCKED_VALIDATION_DB_EXISTS:{target}")

src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0)
dst = sqlite3.connect(target, timeout=60.0)
try:
    src.backup(dst)
    dst.commit()
    integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = list(dst.execute("PRAGMA foreign_key_check"))
finally:
    dst.close()
    src.close()

if integrity != "ok" or foreign_keys:
    raise SystemExit(
        f"BLOCKED_VALIDATION_DB_BACKUP_INVALID:integrity={integrity}:foreign_keys={foreign_keys}"
    )

print(f"validation_db={target}")
print("validation_db_integrity=ok")
print("validation_db_foreign_keys=0")
PY

echo "== 启动真实 Qwen3-VL 三 worker × 75 =="
echo "实时日志：$RUNNER_LOG"
echo "监控器读取：$LATEST_FILE"

set +e
PYTHONPYCACHEPREFIX="$BASE/pycache" \
HF_HUB_OFFLINE=1 \
TRANSFORMERS_OFFLINE=1 \
HF_DATASETS_OFFLINE=1 \
ULTRALYTICS_OFFLINE=1 \
TOKENIZERS_PARALLELISM=false \
NO_PROXY='*' \
no_proxy='*' \
"$PY" "$ENGINE" \
  --mode real-validation \
  --db "$VALIDATION_DB" \
  --config "$CONFIG" \
  --prompt "$PROMPT" \
  --model "$MODEL" \
  --out "$QWEN_OUT" \
  --workers 3 \
  --items-per-worker 75 \
  --max-tokens 384 \
  --confirm-real-model-validation \
  > "$RUNNER_LOG" 2>&1 &

RUNNER_PID=$!
printf '%s\n' "$RUNNER_PID" > "$PID_FILE"
echo "runner_pid=$RUNNER_PID"

wait "$RUNNER_PID"
RUNNER_EXIT=$?
set -e

printf '%s\n' "$RUNNER_EXIT" > "$EXIT_FILE"
echo "qwen_runner_exit_code=$RUNNER_EXIT"

echo "== 把本次真实运行状态和结果写回测试数据库副本，并执行反查 =="

set +e
"$PY" - \
  "$SOURCE_DB" \
  "$SOURCE_HASH_BEFORE_FILE" \
  "$VALIDATION_DB" \
  "$STATE_DB" \
  "$QWEN_OUT/reports/final_report.json" \
  "$FULLFLOW_REPORT" \
  "$RUNNER_EXIT" \
  "$BASE" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

source_db = Path(sys.argv[1])
before_file = Path(sys.argv[2])
validation_db = Path(sys.argv[3])
state_db = Path(sys.argv[4])
engine_report_path = Path(sys.argv[5])
report_path = Path(sys.argv[6])
runner_exit = int(sys.argv[7])
base = Path(sys.argv[8])

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'

def safe_suffix(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_")
    return cleaned[:80] or "unnamed"

source_hash_before = before_file.read_text(encoding="utf-8").strip()
source_hash_after = sha256_file(source_db)
source_unchanged = source_hash_before == source_hash_after

engine_report: dict[str, Any] = {}
if engine_report_path.is_file():
    try:
        engine_report = json.loads(engine_report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        engine_report = {"report_parse_error": f"{type(exc).__name__}:{exc}"}

copied_tables: list[dict[str, Any]] = []
writeback_error = None
integrity = None
foreign_keys: list[Any] = []
table_with_225_rows = False
table_with_450_or_more_rows = False

if not state_db.is_file():
    writeback_error = f"state_db_missing:{state_db}"
else:
    con = sqlite3.connect(validation_db, timeout=60.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("ATTACH DATABASE ? AS state_src", (str(state_db),))

        source_tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM state_src.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

        con.execute("DROP TABLE IF EXISTS stop03_3g_state_table_inventory")
        con.execute(
            """
            CREATE TABLE stop03_3g_state_table_inventory (
                source_table TEXT PRIMARY KEY,
                copied_table TEXT NOT NULL,
                source_row_count INTEGER NOT NULL,
                copied_row_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL,
                copied_at TEXT NOT NULL
            )
            """
        )

        for source_name in source_tables:
            copied_name = "stop03_3g_state__" + safe_suffix(source_name)
            con.execute(f"DROP TABLE IF EXISTS {qident(copied_name)}")

            source_count = int(
                con.execute(
                    f"SELECT COUNT(*) FROM state_src.{qident(source_name)}"
                ).fetchone()[0]
            )
            columns = [
                row[1]
                for row in con.execute(
                    f"PRAGMA state_src.table_info({qident(source_name)})"
                )
            ]

            con.execute(
                f"CREATE TABLE {qident(copied_name)} AS "
                f"SELECT * FROM state_src.{qident(source_name)}"
            )
            copied_count = int(
                con.execute(
                    f"SELECT COUNT(*) FROM {qident(copied_name)}"
                ).fetchone()[0]
            )

            if copied_count != source_count:
                raise RuntimeError(
                    f"writeback_count_mismatch:{source_name}:{source_count}:{copied_count}"
                )

            con.execute(
                """
                INSERT INTO stop03_3g_state_table_inventory (
                    source_table, copied_table, source_row_count,
                    copied_row_count, columns_json, copied_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    copied_name,
                    source_count,
                    copied_count,
                    json.dumps(columns, ensure_ascii=False),
                    now_iso(),
                ),
            )

            copied_tables.append(
                {
                    "source_table": source_name,
                    "copied_table": copied_name,
                    "row_count": copied_count,
                    "columns": columns,
                }
            )

        table_with_225_rows = any(item["row_count"] == 225 for item in copied_tables)
        table_with_450_or_more_rows = any(item["row_count"] >= 450 for item in copied_tables)

        con.execute("DROP TABLE IF EXISTS stop03_3g_fullflow_run")
        con.execute(
            """
            CREATE TABLE stop03_3g_fullflow_run (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_db_path TEXT NOT NULL,
                source_db_sha256_before TEXT NOT NULL,
                source_db_sha256_after TEXT NOT NULL,
                source_db_unchanged INTEGER NOT NULL,
                validation_db_path TEXT NOT NULL,
                state_db_path TEXT NOT NULL,
                engine_report_path TEXT,
                runner_exit_code INTEGER NOT NULL,
                copied_table_count INTEGER NOT NULL,
                copied_row_count INTEGER NOT NULL,
                engine_report_json TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
            INSERT INTO stop03_3g_fullflow_run (
                id, status, created_at, source_db_path,
                source_db_sha256_before, source_db_sha256_after,
                source_db_unchanged, validation_db_path, state_db_path,
                engine_report_path, runner_exit_code, copied_table_count,
                copied_row_count, engine_report_json
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "PENDING_FINAL_VALIDATION",
                now_iso(),
                str(source_db),
                source_hash_before,
                source_hash_after,
                int(source_unchanged),
                str(validation_db),
                str(state_db),
                str(engine_report_path) if engine_report_path.is_file() else None,
                runner_exit,
                len(copied_tables),
                sum(item["row_count"] for item in copied_tables),
                json.dumps(engine_report, ensure_ascii=False),
            ),
        )

        con.commit()
        con.execute("DETACH DATABASE state_src")

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    except Exception as exc:
        con.rollback()
        writeback_error = f"{type(exc).__name__}:{exc}"
    finally:
        con.close()

checks = {
    "runner_exit_zero": runner_exit == 0,
    "source_db_unchanged": source_unchanged,
    "state_db_exists": state_db.is_file(),
    "writeback_completed": writeback_error is None and len(copied_tables) > 0,
    "writeback_has_225_row_table": table_with_225_rows,
    "writeback_has_450_or_more_row_table": table_with_450_or_more_rows,
    "validation_db_integrity_ok": integrity == "ok",
    "validation_db_foreign_keys_zero": foreign_keys == [],
}

status = "PASS" if all(checks.values()) else "FAIL"

if validation_db.is_file():
    con = sqlite3.connect(validation_db, timeout=60.0)
    try:
        con.execute(
            "UPDATE stop03_3g_fullflow_run SET status=? WHERE id=1",
            (status,),
        )
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()

report = {
    "validation_status": status,
    "created_at": now_iso(),
    "output_root": str(base),
    "source_db": str(source_db),
    "source_db_sha256_before": source_hash_before,
    "source_db_sha256_after": source_hash_after,
    "source_db_unchanged": source_unchanged,
    "validation_db": str(validation_db),
    "state_db": str(state_db),
    "engine_report": str(engine_report_path) if engine_report_path.is_file() else None,
    "runner_exit_code": runner_exit,
    "checks": checks,
    "writeback_error": writeback_error,
    "copied_table_count": len(copied_tables),
    "copied_row_count": sum(item["row_count"] for item in copied_tables),
    "copied_tables": copied_tables,
    "validation_db_integrity": integrity,
    "validation_db_foreign_key_errors": foreign_keys,
    "network_used": False,
    "downloads_performed": False,
    "original_media_modified": False,
    "central_database_open_mode": "read_only_backup_source",
    "central_database_modified_by_this_program": False,
    "engine_report_payload": engine_report,
}

report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(json.dumps({
    "validation_status": status,
    "runner_exit_code": runner_exit,
    "source_db_unchanged": source_unchanged,
    "copied_table_count": len(copied_tables),
    "copied_row_count": report["copied_row_count"],
    "table_with_225_rows": table_with_225_rows,
    "table_with_450_or_more_rows": table_with_450_or_more_rows,
    "validation_db_integrity": integrity,
    "foreign_key_errors": len(foreign_keys),
    "writeback_error": writeback_error,
    "report": str(report_path),
}, ensure_ascii=False, indent=2))

raise SystemExit(0 if status == "PASS" else 1)
PY

FINALIZE_EXIT=$?
set -e

echo "============================================================"
if [ "$FINALIZE_EXIT" -eq 0 ]; then
  echo "PASS：本地数据库全流程真实验证完成"
else
  echo "FAIL：全流程存在未通过项"
fi
echo "输出根目录：$BASE"
echo "Qwen日志：$RUNNER_LOG"
echo "Qwen原始报告：$QWEN_OUT/reports/final_report.json"
echo "测试数据库副本：$VALIDATION_DB"
echo "全流程报告：$FULLFLOW_REPORT"
echo "中心数据库：只读，未作为写回目标"
echo "============================================================"

exit "$FINALIZE_EXIT"
