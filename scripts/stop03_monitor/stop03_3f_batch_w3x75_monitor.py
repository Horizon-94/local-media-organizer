#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only terminal monitor for Stop03-3F fixed 3x75 validation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence

import stop03_3f_batch75_monitor as common


PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = Path(
    "/Users/yourname/Documents/AI-Local/test-output/"
    "stop03_3f_qwenvl_batch_w3x75_validation/run/stop03_3f_w3x75_state.sqlite"
)
DEFAULT_PID_FILE = PROJECT_ROOT / "logs/stop03_3f_batch_w3x75_validation.pid"


def parse_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_state(db_path: Path) -> dict[str, Any]:
    # Normal open + query_only is required for a live WAL database because a
    # reader may need to create/open SQLite's technical -shm sidecar.
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        metadata = {
            str(row["key"]): parse_json(row["value_json"])
            for row in con.execute("SELECT key,value_json FROM metadata")
        }
        workers = [dict(row) for row in con.execute(
            """SELECT w.worker_id,w.assigned_count,w.status,w.pid,w.model_load_count,
            w.started_at,w.loaded_at,w.finished_at,w.error_type,w.error_message,
            SUM(CASE WHEN i.status='success' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN i.status='review' THEN 1 ELSE 0 END) AS review,
            SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN i.status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) AS running,
            AVG(CASE WHEN i.status IN ('success','review','failed') THEN i.elapsed_seconds END) AS average_seconds
            FROM workers w LEFT JOIN items i ON i.worker_id=w.worker_id
            GROUP BY w.worker_id ORDER BY w.worker_id"""
        )]
        items = [dict(row) for row in con.execute(
            """SELECT global_seq,worker_id,worker_seq,candidate_id,status,started_at,
            finished_at,elapsed_seconds,generation_tokens,generation_tps,
            raw_finish_reason,inferred_finish_reason,degenerate_reason,clean_text,
            error_type,error_message FROM items ORDER BY global_seq"""
        )]
        snapshots = [dict(row) for row in con.execute(
            """WITH ranked AS (
            SELECT worker_id,worker_seq,phase,created_at,payload_json,
            ROW_NUMBER() OVER(PARTITION BY worker_id ORDER BY snapshot_id DESC) AS rn
            FROM state_snapshots
            ) SELECT worker_id,worker_seq,phase,created_at,payload_json
            FROM ranked WHERE rn=1 ORDER BY worker_id"""
        )]
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    finally:
        con.close()
    for snapshot in snapshots:
        snapshot["payload"] = parse_json(snapshot.pop("payload_json"))
    return {
        "metadata": metadata,
        "workers": workers,
        "items": items,
        "snapshots": snapshots,
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
    }


def display(
    state: dict[str, Any], pid: int, processes: list[dict[str, Any]], *, alive: bool,
) -> None:
    items = state["items"]
    workers = state["workers"]
    metadata = state["metadata"]
    counts = Counter(str(row["status"]) for row in items)
    done = counts["success"] + counts["review"] + counts["failed"]
    total = len(items)
    remaining = counts["pending"] + counts["running"]
    total_cpu = sum(row["cpu"] for row in processes)
    total_rss = sum(row["rss_kb"] for row in processes) / 1024 / 1024
    worker_eta: list[float] = []
    for worker in workers:
        average = worker["average_seconds"]
        left = int(worker["pending"] or 0) + int(worker["running"] or 0)
        if average is not None:
            worker_eta.append(left * float(average) / 60.0)
    eta = max(worker_eta) if worker_eta else None
    snapshot_by_worker = {int(row["worker_id"]): row for row in state["snapshots"]}

    print("\033[2J\033[H", end="")
    print("Stop03-3F corrected batch_generate 三worker固定75条监控（只读，Control+C仅退出监控）")
    print("=" * 110)
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"主进程 PID: {pid}  存活: {alive}  运行状态: {metadata.get('status','UNKNOWN')}  "
        f"分配模式: {metadata.get('assignment_mode','UNKNOWN')}"
    )
    print(
        f"总数={total}  完成={done}  pending={counts['pending']}  running={counts['running']}  "
        f"success={counts['success']}  review={counts['review']}  failed={counts['failed']}"
    )
    print(
        f"完成率: {(done * 100 / total if total else 0):.2f}%  剩余={remaining}  "
        + (f"按最慢worker粗略ETA={eta:.1f}分钟" if eta is not None else "ETA=等待首条完成")
    )

    print()
    print("各worker固定分片状态:")
    for worker in workers:
        average = worker["average_seconds"]
        snapshot = snapshot_by_worker.get(int(worker["worker_id"]))
        memory = common.memory_gb_from_snapshot(snapshot)
        memory_text = " ".join(
            f"{key}={value:.2f}GB" for key, value in sorted(memory.items())
        ) if memory else "MLX=等待快照"
        print(
            f"  worker {worker['worker_id']}: lifecycle={worker['status']:<9} "
            f"load_count={worker['model_load_count']}  success={worker['success']} "
            f"review={worker['review']} failed={worker['failed']} "
            f"pending={worker['pending']} running={worker['running']}  "
            f"avg={(f'{float(average):.2f}s' if average is not None else '-')}  {memory_text}"
        )

    print()
    print("当前正在处理:")
    running = [row for row in items if row["status"] == "running"]
    if not running:
        print("  无")
    for row in running:
        print(
            f"  worker={row['worker_id']} worker_seq={row['worker_seq']}/75 "
            f"global_seq={row['global_seq']}/225 candidate={row['candidate_id']} "
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
    print("每个worker最近4条完成结果（耗时来自状态库）:")
    for worker_id in (1, 2, 3):
        completed = [
            row for row in items
            if row["worker_id"] == worker_id
            and row["status"] in {"success", "review", "failed"}
        ][-4:]
        if not completed:
            print(f"  worker {worker_id}: 等待第一条完成")
            continue
        for row in completed:
            print(
                f"  w{worker_id} seq={row['worker_seq']:>2} {row['status']:<7} "
                f"{float(row['elapsed_seconds'] or 0):>6.2f}s tokens={str(row['generation_tokens']):>4} "
                f"finish={row['raw_finish_reason'] or 'null'}/{row['inferred_finish_reason'] or 'null'} "
                f"degenerate={row['degenerate_reason'] or '-'} "
                f"{common.compact_preview(row['clean_text'], 34)}"
            )

    print()
    print("三个worker第65–75条边界:")
    boundary = [row for row in items if row["worker_seq"] >= 65 and row["status"] != "pending"]
    if not boundary:
        print("  尚未到达第65条")
    for row in boundary:
        print(
            f"  w{row['worker_id']} seq={row['worker_seq']:>2} {row['status']:<7} "
            f"elapsed={float(row['elapsed_seconds'] or 0):>6.2f}s "
            f"tokens={str(row['generation_tokens']):>4} "
            f"degenerate={row['degenerate_reason'] or '-'}"
        )
    print(
        f"SQLite: integrity={state['integrity_check']}  "
        f"foreign_key_errors={len(state['foreign_key_check'])}"
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Stop03-3F fixed 3x75 monitor")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    while not db_path.is_file() or not pid_file.is_file():
        if args.once:
            raise RuntimeError(f"state_or_pid_missing:{db_path}:{pid_file}")
        print(f"等待状态库和PID文件创建: {db_path}", flush=True)
        time.sleep(args.refresh_seconds)
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        while True:
            try:
                state = read_state(db_path)
            except sqlite3.OperationalError as exc:
                if args.once:
                    raise
                print(
                    f"状态库初始化或短暂锁定，{args.refresh_seconds:.1f}秒后重试: {exc}",
                    flush=True,
                )
                time.sleep(args.refresh_seconds)
                continue
            alive = common.process_alive(pid)
            processes = common.read_process_tree(pid) if alive else []
            display(state, pid, processes, alive=alive)
            if args.once:
                print("\n一次性监控快照完成。", flush=True)
                return 0
            if not alive:
                print("\n主进程已退出，监控结束。", flush=True)
                return 0
            time.sleep(args.refresh_seconds)
    except KeyboardInterrupt:
        print("\n监控已退出；验证主进程未被停止。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
