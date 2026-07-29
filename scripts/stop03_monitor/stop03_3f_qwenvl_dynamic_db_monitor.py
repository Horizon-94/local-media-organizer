#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only terminal monitor for the Stop03-3F dynamic DB orchestrator."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence

import stop03_3f_batch75_monitor as common


PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = Path(
    "/Users/yourname/Documents/AI-Local/test-output/stop03_3f_qwenvl_dynamic_db_full"
)
DEFAULT_PID_FILE = PROJECT_ROOT / "logs/stop03_3f_qwenvl_dynamic_db_full.pid"
RETRYABLE_STATUSES = {
    "failed",
    "review",
    "truncated",
    "parse_failed",
    "missing_required_fields",
    "input_fingerprint_mismatch",
}


def parse_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_worker_statuses(out: Path) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for path in sorted((out / "worker_status").glob("worker_*.json")):
        try:
            statuses.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(statuses, key=lambda row: int(row.get("worker_id", 0)))


def read_state(
    db_path: Path,
    run_id: str,
    *,
    max_attempts: int,
) -> dict[str, Any]:
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        run = con.execute(
            "SELECT * FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"dynamic_monitor_run_missing:{run_id}")
        items = [
            dict(row)
            for row in con.execute(
                """SELECT run_item_id,candidate_id,status,attempt_count,
                started_at,finished_at,last_error_code,last_error_message
                FROM stop03_3_qwenvl_run_items
                WHERE run_id=? ORDER BY candidate_id""",
                (run_id,),
            )
        ]
        recent = [
            dict(row)
            for row in con.execute(
                """SELECT r.candidate_id,r.result_status,r.generation_tokens,
                r.finish_reason,r.runtime_metrics_json,r.qwen_text_preview,
                r.created_at,i.attempt_count
                FROM stop03_3_qwenvl_results r
                JOIN stop03_3_qwenvl_run_items i ON i.run_item_id=r.run_item_id
                WHERE r.run_id=? ORDER BY r.created_at DESC LIMIT 12""",
                (run_id,),
            )
        ]
    finally:
        con.close()
    counts = Counter(str(row["status"]) for row in items)
    retryable = sum(
        row["status"] in RETRYABLE_STATUSES
        and int(row["attempt_count"]) < max_attempts
        for row in items
    )
    terminal_error = sum(
        row["status"] in RETRYABLE_STATUSES
        and int(row["attempt_count"]) >= max_attempts
        for row in items
    )
    for row in recent:
        row["runtime_metrics"] = parse_json(row.pop("runtime_metrics_json")) or {}
    return {
        "run": dict(run),
        "items": items,
        "counts": dict(counts),
        "retryable": retryable,
        "terminal_error": terminal_error,
        "recent": recent,
    }


def display(
    state: dict[str, Any],
    worker_statuses: list[dict[str, Any]],
    pid: int,
    processes: list[dict[str, Any]],
    *,
    alive: bool,
    max_attempts: int,
) -> None:
    run = state["run"]
    items = state["items"]
    counts = Counter(state["counts"])
    total = len(items)
    retryable = int(state["retryable"])
    terminal_error = int(state["terminal_error"])
    final_done = counts["success"] + terminal_error
    remaining = counts["pending"] + counts["running"] + retryable
    percent = final_done * 100 / total if total else 0.0
    metrics = [
        row["runtime_metrics"]
        for row in state["recent"]
        if row["runtime_metrics"].get("elapsed_seconds") is not None
    ]
    average_recent = (
        sum(float(row["elapsed_seconds"]) for row in metrics) / len(metrics)
        if metrics
        else None
    )
    effective_workers = max(1, sum(
        row.get("lifecycle") in {"loading", "running"} for row in worker_statuses
    ))
    eta = (
        remaining * average_recent / effective_workers / 60.0
        if average_recent is not None
        else None
    )
    total_cpu = sum(row["cpu"] for row in processes)
    total_rss = sum(row["rss_kb"] for row in processes) / 1024 / 1024
    status_by_candidate = {
        str(row.get("current_candidate_id")): row
        for row in worker_statuses
        if row.get("current_candidate_id")
    }

    print("\033[2J\033[H", end="")
    print("Stop03-3F Qwen-VL 动态数据库队列监控（只读，Control+C仅退出监控）")
    print("=" * 112)
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"run_id: {run['run_id']}  主进程PID: {pid}  存活: {alive}  "
        f"数据库run状态: {run['status']}"
    )
    print(
        f"调度模式: dynamic_database_claim  workers={run['workers']}  "
        f"max_tokens={run['max_tokens']}  max_attempts={max_attempts}"
    )
    print(
        f"总数={total}  最终完成={final_done}  pending={counts['pending']}  "
        f"running={counts['running']}  success={counts['success']}  "
        f"待重试={retryable}  最终错误={terminal_error}"
    )
    print(
        f"最终完成率={percent:.2f}%  剩余={remaining}  "
        + (
            f"最近结果平均={average_recent:.2f}秒/条  粗略ETA={eta:.1f}分钟"
            if average_recent is not None
            else "等待首条完成"
        )
    )

    print()
    print("动态worker状态（谁先完成，谁领取下一条）:")
    if not worker_statuses:
        print("  等待worker状态文件……")
    for worker in worker_statuses:
        memory = worker.get("mlx_memory_bytes") or {}
        memory_text = " ".join(
            f"{key}={float(value)/1_000_000_000:.2f}GB"
            for key, value in sorted(memory.items())
            if isinstance(value, int)
        )
        print(
            f"  worker {worker.get('worker_id')}: lifecycle={worker.get('lifecycle')} "
            f"load_count={worker.get('model_load_count')} "
            f"完成尝试={worker.get('completed_attempts',0)} "
            f"成功尝试={worker.get('successful_attempts',0)} "
            f"非成功尝试={worker.get('non_successful_attempts',0)} "
            f"平均={float(worker.get('average_seconds') or 0):.2f}s "
            f"{memory_text or 'MLX=等待快照'}"
        )

    print()
    print("当前正在处理:")
    running = [row for row in items if row["status"] == "running"]
    if not running:
        print("  无")
    for row in running:
        worker = status_by_candidate.get(str(row["candidate_id"]), {})
        print(
            f"  worker={worker.get('worker_id','?')} "
            f"candidate={row['candidate_id']} "
            f"attempt={row['attempt_count']}/{max_attempts} "
            f"started_at={row['started_at']}"
        )

    print()
    print(f"进程树合计: CPU={total_cpu:.1f}%  RSS≈{total_rss:.2f}GB")
    print("PID      PPID     ELAPSED   CPU%   MEM%   RSS-GB   STATE")
    for row in processes:
        print(
            f"{row['pid']:<8} {row['ppid']:<8} {row['elapsed']:<9} "
            f"{row['cpu']:>5.1f}  {row['mem']:>5.1f}  "
            f"{row['rss_kb']/1024/1024:>6.2f}   {row['state']}"
        )

    print()
    print("数据库最近写回结果（每条完成后立即出现）:")
    if not state["recent"]:
        print("  等待第一条写回……")
    for row in reversed(state["recent"]):
        runtime = row["runtime_metrics"]
        print(
            f"  worker={runtime.get('worker_id','?')} "
            f"{row['result_status']:<24} "
            f"{float(runtime.get('elapsed_seconds') or 0):>6.2f}s "
            f"attempt={row['attempt_count']}/{max_attempts} "
            f"tokens={str(row['generation_tokens']):>4} "
            f"finish={runtime.get('raw_finish_reason') or 'null'}/"
            f"{runtime.get('inferred_finish_reason') or row['finish_reason']} "
            f"candidate={row['candidate_id']}"
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Stop03-3F dynamic DB monitor"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser()
    out = Path(args.out).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    run_id_file = out / "run_id.txt"
    while not pid_file.is_file() or not run_id_file.is_file():
        if args.once:
            raise RuntimeError(
                f"dynamic_monitor_pid_or_run_id_missing:{pid_file}:{run_id_file}"
            )
        print(f"等待主程序建立PID和run_id: {run_id_file}", flush=True)
        time.sleep(args.refresh_seconds)
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    run_id = run_id_file.read_text(encoding="utf-8").strip()
    try:
        while True:
            try:
                state = read_state(
                    db_path,
                    run_id,
                    max_attempts=args.max_attempts,
                )
            except (sqlite3.OperationalError, RuntimeError) as exc:
                if args.once:
                    raise
                print(
                    f"数据库尚未就绪或短暂锁定，{args.refresh_seconds:.1f}秒后重试: {exc}",
                    flush=True,
                )
                time.sleep(args.refresh_seconds)
                continue
            alive = common.process_alive(pid)
            processes = common.read_process_tree(pid) if alive else []
            worker_statuses = read_worker_statuses(out)
            display(
                state,
                worker_statuses,
                pid,
                processes,
                alive=alive,
                max_attempts=args.max_attempts,
            )
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
