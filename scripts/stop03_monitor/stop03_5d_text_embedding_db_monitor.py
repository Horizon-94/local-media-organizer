#!/usr/bin/env python3
"""Read-only terminal monitor for Stop03-5D dynamic text embeddings."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_3f_batch75_monitor as common


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = PROJECT_ROOT.parent / "test-output/stop03_5d_text_embedding_db_full_v1"
DEFAULT_PID_FILE = PROJECT_ROOT / "logs/stop03_5d_text_embedding_db_full_v1.pid"


def read_worker_statuses(out: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((out / "worker_status").glob("worker_*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda row: int(row.get("worker_id", 0)))


def read_state(db: Path, run_id: str, max_attempts: int) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError(f"stop03_5d_monitor_run_missing:{run_id}")
        vectors = [dict(row) for row in con.execute(
            """SELECT text_vector_id,status,attempt_count,claimed_by_worker,
               worker_pid,elapsed_seconds,started_at,finished_at,last_error_code
               FROM stop03_5d_text_vectors WHERE embedding_run_id=?
               ORDER BY text_vector_id""", (run_id,)
        )]
        recent = [dict(row) for row in con.execute(
            """SELECT text_vector_id,status,attempt_count,worker_pid,
               elapsed_seconds,finished_at,last_error_code
               FROM stop03_5d_text_vectors WHERE embedding_run_id=?
                 AND finished_at IS NOT NULL
               ORDER BY finished_at DESC,text_vector_id DESC LIMIT 12""", (run_id,)
        )]
    finally:
        con.close()
    counts = Counter(row["status"] for row in vectors)
    retryable = sum(
        row["status"] == "failed" and int(row["attempt_count"]) < max_attempts
        for row in vectors
    )
    terminal_failed = counts["failed"] - retryable
    completed = counts["success"] + terminal_failed
    total = len(vectors)
    return {
        "run": dict(run), "vectors": vectors, "recent": recent,
        "counts": counts, "retryable": retryable,
        "terminal_failed": terminal_failed, "completed": completed,
        "total": total, "remaining": total - completed,
    }


def display(
    state: dict[str, Any], workers: list[dict[str, Any]],
    pid: int, processes: list[dict[str, Any]], alive: bool,
) -> None:
    run = state["run"]
    counts = Counter(state["counts"])
    total = state["total"]
    completed = state["completed"]
    remaining = state["remaining"]
    percent = completed * 100.0 / total if total else 100.0
    recent_elapsed = [
        float(row["elapsed_seconds"]) for row in state["recent"]
        if row.get("elapsed_seconds") is not None and row["status"] == "success"
    ]
    average = sum(recent_elapsed) / len(recent_elapsed) if recent_elapsed else None
    active_workers = max(1, sum(
        row.get("lifecycle") in {"loading", "running"} for row in workers
    ))
    eta = remaining * average / active_workers / 60 if average is not None else None
    total_cpu = sum(float(row["cpu"]) for row in processes)
    total_rss = sum(float(row["rss_kb"]) for row in processes) / 1024 / 1024

    print("\033[2J\033[H", end="")
    print("Stop03-5D 文本向量动态数据库监控（只读，Control+C 仅退出监控）")
    print("=" * 112)
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"run_id: {run['embedding_run_id']}  主进程PID: {pid}  存活: {alive}  "
        f"数据库run状态: {run['status']}"
    )
    print(
        f"调度: {run['scheduling_mode']}  workers={run['workers']}  "
        f"max_attempts={run['max_attempts']}  documents={run['document_count']}"
    )
    print(
        f"唯一文本总数={total}  完成={completed}  pending={counts['pending']}  "
        f"running={counts['running']}  success={counts['success']}  "
        f"待重试={state['retryable']}  最终失败={state['terminal_failed']}"
    )
    speed_text = (
        f"最近平均={average:.2f}秒/条  粗略ETA={eta:.1f}分钟"
        if average is not None else "等待首条完成"
    )
    print(f"完成率={percent:.2f}%  剩余={remaining}  {speed_text}")

    print("\n动态 worker 状态（谁先完成，谁领取下一条）:")
    if not workers:
        print("  等待 worker 状态文件……")
    for row in workers:
        print(
            f"  worker {row.get('worker_id')}: lifecycle={row.get('lifecycle')} "
            f"device={row.get('device')} load_count={row.get('model_load_count')} "
            f"完成={row.get('completed_attempts',0)} 成功={row.get('successful_attempts',0)} "
            f"失败尝试={row.get('failed_attempts',0)} "
            f"平均={float(row.get('average_seconds') or 0):.2f}s "
            f"peak_RSS={float(row.get('peak_rss_bytes') or 0)/1_000_000_000:.2f}GB"
        )

    print("\n当前正在处理:")
    running = [row for row in state["vectors"] if row["status"] == "running"]
    if not running:
        print("  无")
    for row in running:
        print(
            f"  {row['claimed_by_worker']} pid={row['worker_pid']} "
            f"vector={row['text_vector_id']} attempt={row['attempt_count']} "
            f"started_at={row['started_at']}"
        )

    print(f"\n进程树合计: CPU={total_cpu:.1f}%  RSS≈{total_rss:.2f}GB")
    print("PID      PPID     ELAPSED   CPU%   MEM%   RSS-GB   STATE")
    for row in processes:
        print(
            f"{row['pid']:<8} {row['ppid']:<8} {row['elapsed']:<9} "
            f"{row['cpu']:>5.1f}  {row['mem']:>5.1f}  "
            f"{row['rss_kb']/1024/1024:>6.2f}   {row['state']}"
        )

    print("\n数据库最近逐条写回:")
    if not state["recent"]:
        print("  等待第一条写回……")
    for row in reversed(state["recent"]):
        print(
            f"  {str(row['finished_at'] or '')[-8:]} "
            f"{row['status']:<8} {float(row.get('elapsed_seconds') or 0):>6.2f}s "
            f"attempt={row['attempt_count']} pid={row['worker_pid']} "
            f"vector={row['text_vector_id']} {row['last_error_code'] or ''}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    run_id_file = args.out / "run_id.txt"
    while not args.pid_file.is_file() or not run_id_file.is_file():
        if args.once:
            raise RuntimeError("stop03_5d_monitor_pid_or_run_id_missing")
        print("等待主程序建立 PID 和 run_id……", flush=True)
        time.sleep(args.refresh_seconds)
    pid = int(args.pid_file.read_text(encoding="utf-8").strip())
    run_id = run_id_file.read_text(encoding="utf-8").strip()
    try:
        while True:
            try:
                state = read_state(args.db, run_id, args.max_attempts)
            except (sqlite3.OperationalError, RuntimeError) as exc:
                if args.once:
                    raise
                print(f"数据库尚未就绪或短暂锁定: {exc}", flush=True)
                time.sleep(args.refresh_seconds)
                continue
            alive = common.process_alive(pid)
            processes = common.read_process_tree(pid) if alive else []
            display(state, read_worker_statuses(args.out), pid, processes, alive)
            if args.once:
                print("\n一次性监控快照完成。", flush=True)
                return 0
            if not alive:
                print("\n主进程已退出，监控结束。", flush=True)
                return 0
            time.sleep(args.refresh_seconds)
    except KeyboardInterrupt:
        print("\n监控已退出；主程序未被停止。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
