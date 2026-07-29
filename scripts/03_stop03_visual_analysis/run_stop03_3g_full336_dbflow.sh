#!/bin/bash
set -Eeuo pipefail

PROJECT="/Users/yourname/Documents/AI-Local/media-archive-clean"
PY="/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python"
SOURCE_DB="$PROJECT/media_archive.sqlite"
TEST_ROOT="/Users/yourname/Documents/AI-Local/test-output"
STAMP="$(date +%Y%m%d_%H%M%S)"
BASE="$TEST_ROOT/stop03_3g_full336_dbflow_$STAMP"

VALIDATION_DB="$BASE/database/media_archive_stop03_3g_full336.sqlite"
ENGINE_LOG="$BASE/logs/full336_engine.log"
ENGINE_PID_FILE="$BASE/run/full336_engine.pid"
ENGINE_RUN_DIR_FILE="$BASE/run/engine_run_dir.txt"
SOURCE_HASH_BEFORE_FILE="$BASE/run/source_db_sha256_before.txt"
FINAL_REPORT="$BASE/reports/fullflow_336_report.json"
LATEST_POINTER="$PROJECT/logs/stop03_3g_full336_latest.txt"

ENGINE=""
for CANDIDATE in \
  "$PROJECT/scripts/03_stop03_visual_analysis/run_stop03_3d_batchgen_v3_1_auto.sh" \
  "$PROJECT/run_stop03_3d_batchgen_v3_1_auto.sh" \
  "$HOME/Downloads/run_stop03_3d_batchgen_v3_1_auto.sh"
do
  if [ -f "$CANDIDATE" ]; then
    ENGINE="$CANDIDATE"
    break
  fi
done

if [ -z "$ENGINE" ]; then
  echo "BLOCKED_MISSING_FULL336_ENGINE"
  echo "需要以下文件之一："
  echo "  $PROJECT/scripts/03_stop03_visual_analysis/run_stop03_3d_batchgen_v3_1_auto.sh"
  echo "  $HOME/Downloads/run_stop03_3d_batchgen_v3_1_auto.sh"
  exit 4
fi

for REQUIRED in \
  "$PY" \
  "$SOURCE_DB" \
  "$PROJECT/configs/stop03_3_qwenvl_db_v1.json" \
  "$PROJECT/scripts/03_stop03_visual_analysis/qwenvl_output_contract_v2.py" \
  "$PROJECT/docs/model_registry/LOCAL_MODEL_REGISTRY.md" \
  "$PROJECT/docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md" \
  "/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit/config.json" \
  "/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit/model.safetensors" \
  "/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit/tokenizer.json" \
  "/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit/tokenizer_config.json"
do
  if [ ! -e "$REQUIRED" ]; then
    echo "BLOCKED_MISSING_PATH=$REQUIRED"
    exit 4
  fi
done

if /usr/bin/pgrep -f 'qwen_persistent_runner_batchgen_v3_1\.py' >/dev/null 2>&1; then
  echo "BLOCKED_EXISTING_FULL336_RUNNER"
  /usr/bin/pgrep -fl 'qwen_persistent_runner_batchgen_v3_1\.py' || true
  exit 5
fi

mkdir -p "$BASE/database" "$BASE/logs" "$BASE/run" "$BASE/reports" "$PROJECT/logs"
printf '%s\n' "$BASE" > "$LATEST_POINTER"
printf '%s\n' "$ENGINE" > "$BASE/run/full336_engine_path.txt"

echo "============================================================"
echo "Stop03-3G 全量 336 条数据库闭环"
echo "中心数据库读取 336 条 -> 3 个常驻 worker 动态并发 -> corrected batch_generate"
echo "-> benchmark.sqlite -> 写回中心数据库测试副本 -> 数据库反查"
echo "============================================================"
echo "BASE=$BASE"
echo "ENGINE=$ENGINE"
echo "TOTAL=336"
echo "WORKERS=3"
echo "ASSIGNMENT=dynamic_queue"
echo "CENTER_DB_WRITE=NO"
echo

"$PY" - "$SOURCE_DB" "$SOURCE_HASH_BEFORE_FILE" <<'PY'
from __future__ import annotations
import hashlib
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]).resolve(strict=True)
hash_file = Path(sys.argv[2])

h = hashlib.sha256()
with db.open("rb") as f:
    for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
        h.update(chunk)
digest = h.hexdigest()
hash_file.write_text(digest + "\n", encoding="utf-8")

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
try:
    con.execute("PRAGMA query_only=ON")
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = list(con.execute("PRAGMA foreign_key_check"))
    view = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' "
        "AND name='v_stop03_2_v25_qwenvl_execution_queue'"
    ).fetchone()
    if view is None:
        raise SystemExit("BLOCKED_MISSING_V25_QWEN_VIEW")
    count = con.execute(
        "SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue"
    ).fetchone()[0]
finally:
    con.close()

print(f"source_db_sha256_before={digest}")
print(f"source_db_integrity={integrity}")
print(f"source_db_foreign_key_errors={len(foreign)}")
print(f"qwen_candidate_count={count}")

if integrity != "ok":
    raise SystemExit("BLOCKED_SOURCE_DB_INTEGRITY")
if foreign:
    raise SystemExit("BLOCKED_SOURCE_DB_FOREIGN_KEYS")
if count != 336:
    raise SystemExit(f"BLOCKED_QWEN_CANDIDATE_COUNT_NOT_336:{count}")
PY

echo "== 创建中心数据库一致性测试副本 =="
"$PY" - "$SOURCE_DB" "$VALIDATION_DB" <<'PY'
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2])
if target.exists():
    raise SystemExit(f"BLOCKED_VALIDATION_DB_EXISTS:{target}")

src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
dst = sqlite3.connect(target, timeout=60)
try:
    src.backup(dst)
    dst.commit()
    integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = list(dst.execute("PRAGMA foreign_key_check"))
finally:
    dst.close()
    src.close()

print(f"validation_db={target}")
print(f"validation_db_integrity={integrity}")
print(f"validation_db_foreign_key_errors={len(foreign)}")

if integrity != "ok" or foreign:
    raise SystemExit("BLOCKED_VALIDATION_DB_BACKUP_INVALID")
PY

echo
echo "== 启动全量真实运行：3 个 worker 共同处理 336 条 =="
echo "这不是每个 worker 固定 75 条，也不是每个 worker 固定 112 条。"
echo "这是一个 336 条动态队列，三个 worker 同时取任务，合计完成 336 条。"
echo "实时日志：$ENGINE_LOG"

set +e
HF_HUB_OFFLINE=1 \
TRANSFORMERS_OFFLINE=1 \
HF_DATASETS_OFFLINE=1 \
ULTRALYTICS_OFFLINE=1 \
TOKENIZERS_PARALLELISM=false \
NO_PROXY='*' \
no_proxy='*' \
bash "$ENGINE" full > "$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!
set -e

printf '%s\n' "$ENGINE_PID" > "$ENGINE_PID_FILE"
echo "engine_pid=$ENGINE_PID"

# 等待原始 full runner 输出 RUN_DIR，供监控程序使用。
for _ in $(seq 1 600); do
  RUN_DIR_LINE="$(grep '^RUN_DIR=' "$ENGINE_LOG" 2>/dev/null | tail -1 || true)"
  if [ -n "$RUN_DIR_LINE" ]; then
    ENGINE_RUN_DIR="${RUN_DIR_LINE#RUN_DIR=}"
    printf '%s\n' "$ENGINE_RUN_DIR" > "$ENGINE_RUN_DIR_FILE"
    echo "engine_run_dir=$ENGINE_RUN_DIR"
    break
  fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

set +e
wait "$ENGINE_PID"
ENGINE_EXIT=$?
set -e

printf '%s\n' "$ENGINE_EXIT" > "$BASE/run/full336_engine.exit_code"
echo "full336_engine_exit_code=$ENGINE_EXIT"

ENGINE_RUN_DIR=""
if [ -f "$ENGINE_RUN_DIR_FILE" ]; then
  ENGINE_RUN_DIR="$(cat "$ENGINE_RUN_DIR_FILE")"
fi
if [ -z "$ENGINE_RUN_DIR" ]; then
  ENGINE_RUN_DIR_LINE="$(grep '^RUN_DIR=' "$ENGINE_LOG" 2>/dev/null | tail -1 || true)"
  ENGINE_RUN_DIR="${ENGINE_RUN_DIR_LINE#RUN_DIR=}"
fi

BENCHMARK_DB=""
ENGINE_REPORT=""
if [ -n "$ENGINE_RUN_DIR" ]; then
  BENCHMARK_DB="$ENGINE_RUN_DIR/benchmark.sqlite"
  ENGINE_REPORT="$ENGINE_RUN_DIR/reports/final_report.json"
fi

if [ ! -f "$BENCHMARK_DB" ]; then
  BENCHMARK_LINE="$(grep '^BENCHMARK_DB=' "$ENGINE_LOG" 2>/dev/null | tail -1 || true)"
  BENCHMARK_DB="${BENCHMARK_LINE#BENCHMARK_DB=}"
fi
if [ ! -f "$ENGINE_REPORT" ]; then
  REPORT_LINE="$(grep '^REPORT=' "$ENGINE_LOG" 2>/dev/null | tail -1 || true)"
  ENGINE_REPORT="${REPORT_LINE#REPORT=}"
fi

echo
echo "== 写回测试数据库副本并执行反查 =="
echo "benchmark_db=$BENCHMARK_DB"
echo "engine_report=$ENGINE_REPORT"

set +e
"$PY" - \
  "$SOURCE_DB" \
  "$SOURCE_HASH_BEFORE_FILE" \
  "$VALIDATION_DB" \
  "$BENCHMARK_DB" \
  "$ENGINE_REPORT" \
  "$FINAL_REPORT" \
  "$ENGINE_EXIT" \
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
source_hash_before_file = Path(sys.argv[2])
validation_db = Path(sys.argv[3])
benchmark_db = Path(sys.argv[4]) if sys.argv[4] else Path("/nonexistent")
engine_report_path = Path(sys.argv[5]) if sys.argv[5] else Path("/nonexistent")
final_report_path = Path(sys.argv[6])
engine_exit = int(sys.argv[7])
base = Path(sys.argv[8])

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_")[:80]

source_hash_before = source_hash_before_file.read_text(encoding="utf-8").strip()
source_hash_after = sha256_file(source_db)
source_db_unchanged = source_hash_before == source_hash_after

engine_report: dict[str, Any] = {}
engine_report_error = None
if engine_report_path.is_file():
    try:
        engine_report = json.loads(engine_report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        engine_report_error = f"{type(exc).__name__}:{exc}"
else:
    engine_report_error = f"missing:{engine_report_path}"

benchmark_integrity = None
benchmark_foreign: list[Any] = []
benchmark_counts: dict[str, Any] = {}
copied_tables: list[dict[str, Any]] = []
writeback_error = None
validation_integrity = None
validation_foreign: list[Any] = []

if not benchmark_db.is_file():
    writeback_error = f"benchmark_db_missing:{benchmark_db}"
else:
    bench = sqlite3.connect(f"file:{benchmark_db}?mode=ro", uri=True, timeout=60)
    bench.row_factory = sqlite3.Row
    try:
        benchmark_integrity = bench.execute("PRAGMA integrity_check").fetchone()[0]
        benchmark_foreign = [list(row) for row in bench.execute("PRAGMA foreign_key_check")]
        for table in ("benchmark_items", "benchmark_results", "benchmark_attempts", "worker_sessions"):
            exists = bench.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            benchmark_counts[table] = (
                int(bench.execute(f"SELECT COUNT(*) FROM {qi(table)}").fetchone()[0])
                if exists else None
            )
        if benchmark_counts.get("benchmark_results") is not None:
            benchmark_counts["result_status_counts"] = {
                str(row[0]): int(row[1])
                for row in bench.execute(
                    "SELECT result_status,COUNT(*) FROM benchmark_results "
                    "GROUP BY result_status"
                )
            }
        duplicate_execution_keys = int(
            bench.execute(
                "SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM benchmark_items"
            ).fetchone()[0]
        )
        benchmark_counts["duplicate_execution_keys"] = duplicate_execution_keys
    finally:
        bench.close()

    con = sqlite3.connect(validation_db, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("ATTACH DATABASE ? AS bench", (str(benchmark_db),))
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM bench.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

        con.execute("DROP TABLE IF EXISTS stop03_3g_full336_table_inventory")
        con.execute(
            """
            CREATE TABLE stop03_3g_full336_table_inventory (
                source_table TEXT PRIMARY KEY,
                copied_table TEXT NOT NULL,
                source_row_count INTEGER NOT NULL,
                copied_row_count INTEGER NOT NULL,
                copied_at TEXT NOT NULL
            )
            """
        )

        for source_table in tables:
            copied_table = "stop03_3g_full336__" + safe_name(source_table)
            con.execute(f"DROP TABLE IF EXISTS {qi(copied_table)}")
            source_count = int(
                con.execute(
                    f"SELECT COUNT(*) FROM bench.{qi(source_table)}"
                ).fetchone()[0]
            )
            con.execute(
                f"CREATE TABLE {qi(copied_table)} AS "
                f"SELECT * FROM bench.{qi(source_table)}"
            )
            copied_count = int(
                con.execute(
                    f"SELECT COUNT(*) FROM {qi(copied_table)}"
                ).fetchone()[0]
            )
            if copied_count != source_count:
                raise RuntimeError(
                    f"copy_count_mismatch:{source_table}:{source_count}:{copied_count}"
                )
            con.execute(
                """
                INSERT INTO stop03_3g_full336_table_inventory
                (source_table,copied_table,source_row_count,copied_row_count,copied_at)
                VALUES (?,?,?,?,?)
                """,
                (source_table, copied_table, source_count, copied_count, now_iso()),
            )
            copied_tables.append({
                "source_table": source_table,
                "copied_table": copied_table,
                "row_count": copied_count,
            })

        con.execute("DROP TABLE IF EXISTS stop03_3g_full336_run")
        con.execute(
            """
            CREATE TABLE stop03_3g_full336_run (
                id INTEGER PRIMARY KEY CHECK (id=1),
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source_db_path TEXT NOT NULL,
                source_db_sha256_before TEXT NOT NULL,
                source_db_sha256_after TEXT NOT NULL,
                source_db_unchanged INTEGER NOT NULL,
                benchmark_db_path TEXT NOT NULL,
                engine_report_path TEXT,
                engine_exit_code INTEGER NOT NULL,
                candidate_total INTEGER,
                success_count INTEGER,
                review_count INTEGER,
                failed_count INTEGER,
                copied_table_count INTEGER NOT NULL,
                copied_row_count INTEGER NOT NULL,
                engine_report_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO stop03_3g_full336_run (
                id,created_at,status,source_db_path,
                source_db_sha256_before,source_db_sha256_after,source_db_unchanged,
                benchmark_db_path,engine_report_path,engine_exit_code,
                candidate_total,success_count,review_count,failed_count,
                copied_table_count,copied_row_count,engine_report_json
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now_iso(),
                "PENDING_FINAL_GATE",
                str(source_db),
                source_hash_before,
                source_hash_after,
                int(source_db_unchanged),
                str(benchmark_db),
                str(engine_report_path) if engine_report_path.is_file() else None,
                engine_exit,
                engine_report.get("candidate_total"),
                engine_report.get("success_count"),
                engine_report.get("review_count"),
                engine_report.get("failed_count"),
                len(copied_tables),
                sum(row["row_count"] for row in copied_tables),
                json.dumps(engine_report, ensure_ascii=False),
            ),
        )
        con.commit()
        con.execute("DETACH DATABASE bench")
        validation_integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        validation_foreign = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    except Exception as exc:
        con.rollback()
        writeback_error = f"{type(exc).__name__}:{exc}"
    finally:
        con.close()

checks = {
    "engine_exit_zero": engine_exit == 0,
    "engine_report_readable": engine_report_error is None,
    "engine_status_pass": engine_report.get("status") == "PASS",
    "candidate_total_336": engine_report.get("candidate_total") == 336,
    "success_count_336": engine_report.get("success_count") == 336,
    "review_count_zero": engine_report.get("review_count") == 0,
    "failed_count_zero": engine_report.get("failed_count") == 0,
    "pending_zero": engine_report.get("pending") == 0,
    "running_zero": engine_report.get("running") == 0,
    "persistent_worker_count_3": engine_report.get("persistent_worker_count") == 3,
    "worker_model_load_count_one": engine_report.get("worker_model_load_count_one") is True,
    "dynamic_queue_true": engine_report.get("dynamic_queue") is True,
    "duplicate_execution_keys_zero": engine_report.get("duplicate_execution_keys") == 0,
    "benchmark_items_336": benchmark_counts.get("benchmark_items") == 336,
    "benchmark_results_336": benchmark_counts.get("benchmark_results") == 336,
    "benchmark_integrity_ok": benchmark_integrity == "ok",
    "benchmark_foreign_keys_zero": benchmark_foreign == [],
    "writeback_completed": writeback_error is None and len(copied_tables) > 0,
    "validation_integrity_ok": validation_integrity == "ok",
    "validation_foreign_keys_zero": validation_foreign == [],
    "source_db_unchanged": source_db_unchanged,
}

status = "PASS" if all(checks.values()) else "FAIL"

if validation_db.is_file():
    con = sqlite3.connect(validation_db, timeout=60)
    try:
        con.execute(
            "UPDATE stop03_3g_full336_run SET status=? WHERE id=1",
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
    "execution_mode": "full_336_three_persistent_workers_dynamic_queue",
    "source_db": str(source_db),
    "source_db_sha256_before": source_hash_before,
    "source_db_sha256_after": source_hash_after,
    "source_db_unchanged": source_db_unchanged,
    "validation_db": str(validation_db),
    "benchmark_db": str(benchmark_db),
    "engine_report": str(engine_report_path),
    "engine_exit_code": engine_exit,
    "engine_report_error": engine_report_error,
    "engine_report_payload": engine_report,
    "benchmark_counts": benchmark_counts,
    "benchmark_integrity": benchmark_integrity,
    "benchmark_foreign_key_errors": benchmark_foreign,
    "copied_tables": copied_tables,
    "writeback_error": writeback_error,
    "validation_db_integrity": validation_integrity,
    "validation_db_foreign_key_errors": validation_foreign,
    "checks": checks,
    "network_used": False,
    "downloads_performed": False,
    "original_media_modified": False,
    "central_database_modified_by_this_program": False,
}

final_report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(json.dumps({
    "validation_status": status,
    "candidate_total": engine_report.get("candidate_total"),
    "success_count": engine_report.get("success_count"),
    "review_count": engine_report.get("review_count"),
    "failed_count": engine_report.get("failed_count"),
    "persistent_worker_count": engine_report.get("persistent_worker_count"),
    "dynamic_queue": engine_report.get("dynamic_queue"),
    "benchmark_items": benchmark_counts.get("benchmark_items"),
    "benchmark_results": benchmark_counts.get("benchmark_results"),
    "source_db_unchanged": source_db_unchanged,
    "validation_db_integrity": validation_integrity,
    "writeback_error": writeback_error,
    "final_report": str(final_report_path),
}, ensure_ascii=False, indent=2))

raise SystemExit(0 if status == "PASS" else 1)
PY
FINALIZE_EXIT=$?
set -e

echo
echo "============================================================"
if [ "$FINALIZE_EXIT" -eq 0 ]; then
  echo "PASS：336 条已由 3 个 worker 共同处理，结果已写入测试数据库副本并反查通过。"
else
  echo "FAIL：全量运行或数据库闭环存在未通过项。"
fi
echo "输出根目录：$BASE"
echo "主运行日志：$ENGINE_LOG"
echo "原始 benchmark DB：$BENCHMARK_DB"
echo "测试数据库副本：$VALIDATION_DB"
echo "最终闭环报告：$FINAL_REPORT"
echo "中心数据库写入：否"
echo "============================================================"

exit "$FINALIZE_EXIT"
