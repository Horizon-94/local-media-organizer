#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only terminal monitor for the Stop03-3F batch75 diagnostic."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence


PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = Path(
    "/Users/yourname/Documents/AI-Local/test-output/"
    "stop03_3f_qwenvl_batch75_diagnostic/run/stop03_3f_state.sqlite"
)
DEFAULT_PID_FILE = PROJECT_ROOT / "logs/stop03_3f_batch75_diagnostic.pid"


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_state(db_path: Path) -> dict[str, Any]:
    # WAL readers may need to create/open SQLite's -shm sidecar. A URI mode=ro
    # connection fails when that sidecar does not yet exist, even though the
    # database itself is valid. Open normally, then enforce SQL-level read-only.
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        metadata = {
            str(row["key"]): parse_json(row["value_json"])
            for row in con.execute("SELECT key,value_json FROM metadata")
        }
        rows = [dict(row) for row in con.execute(
            "SELECT seq,candidate_id,status,started_at,finished_at,elapsed_seconds,"
            "generation_tokens,generation_tps,raw_finish_reason,inferred_finish_reason,"
            "degenerate_reason,clean_text,error_type,error_message "
            "FROM items ORDER BY seq"
        )]
        latest_snapshot = con.execute(
            "SELECT seq,phase,created_at,payload_json FROM state_snapshots "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    finally:
        con.close()
    return {
        "metadata": metadata,
        "rows": rows,
        "latest_snapshot": {
            **dict(latest_snapshot),
            "payload": parse_json(latest_snapshot["payload_json"]),
        } if latest_snapshot else None,
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
    }


def read_process_tree(root_pid: int) -> list[dict[str, Any]]:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,etime=,%cpu=,%mem=,rss=,state=,command="],
        text=True,
    )
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) != 8:
            continue
        try:
            rows.append({
                "pid": int(parts[0]), "ppid": int(parts[1]), "elapsed": parts[2],
                "cpu": float(parts[3]), "mem": float(parts[4]),
                "rss_kb": int(parts[5]), "state": parts[6], "command": parts[7],
            })
        except ValueError:
            continue
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["ppid"] in selected and row["pid"] not in selected:
                selected.add(row["pid"])
                changed = True
    return [row for row in rows if row["pid"] in selected]


def compact_preview(text: Optional[str], limit: int = 48) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def memory_gb_from_snapshot(snapshot: Optional[dict[str, Any]]) -> dict[str, float]:
    if not snapshot:
        return {}
    payload = snapshot.get("payload") or {}
    values = payload.get("mlx_memory_bytes") or {}
    result: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, int):
            result[str(key)] = value / 1_000_000_000
    return result


def display(
    state: dict[str, Any], pid: int, processes: list[dict[str, Any]], *, alive: bool,
) -> None:
    rows = state["rows"]
    metadata = state["metadata"]
    counts = Counter(str(row["status"]) for row in rows)
    total = len(rows)
    completed_rows = [row for row in rows if row["status"] in {"success", "review", "failed"}]
    done = len(completed_rows)
    remaining = counts["pending"] + counts["running"]
    percent = done * 100 / total if total else 0.0
    elapsed_values = [float(row["elapsed_seconds"]) for row in completed_rows if row["elapsed_seconds"] is not None]
    total_item_seconds = sum(elapsed_values)
    average_seconds = total_item_seconds / len(elapsed_values) if elapsed_values else None
    rate = 60.0 / average_seconds if average_seconds and average_seconds > 0 else None
    eta = remaining * average_seconds / 60.0 if average_seconds is not None else None
    total_cpu = sum(row["cpu"] for row in processes)
    total_rss = sum(row["rss_kb"] for row in processes) / 1024 / 1024
    current = [row for row in rows if row["status"] == "running"]
    latest = completed_rows[-12:]
    boundary = [row for row in rows if row["seq"] >= 65 and row["status"] != "pending"]
    snapshot_memory = memory_gb_from_snapshot(state["latest_snapshot"])

    print("\033[2J\033[H", end="")
    print("Stop03-3F corrected batch_generate 75条诊断监控（只读，Control+C仅退出监控）")
    print("=" * 100)
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"主进程 PID: {pid}  存活: {alive}  "
        f"运行状态: {metadata.get('status', 'UNKNOWN')}  "
        f"模型加载次数: {metadata.get('model_load_count', 0)}"
    )
    print()
    print(
        f"总数={total}  完成={done}  pending={counts['pending']}  running={counts['running']}  "
        f"success={counts['success']}  review={counts['review']}  failed={counts['failed']}"
    )
    print(f"完成率: {percent:.2f}%  剩余: {remaining}")
    if average_seconds is not None:
        print(
            f"数据库精确平均耗时: {average_seconds:.2f} 秒/条  "
            f"单worker平均速度: {rate:.2f} 条/分钟  粗略ETA: {eta:.1f} 分钟"
        )
    else:
        print("数据库精确平均耗时: 等待第一条完成")

    print()
    print("当前正在处理:")
    if current:
        for row in current:
            print(
                f"  seq={row['seq']}/75  candidate={row['candidate_id']}  "
                f"started_at={row['started_at']}"
            )
    else:
        print("  无")

    print()
    print(f"进程树合计: CPU={total_cpu:.1f}%  RSS≈{total_rss:.2f} GB")
    print("PID      PPID     ELAPSED   CPU%   MEM%   RSS-GB   STATE")
    for row in processes:
        print(
            f"{row['pid']:<8} {row['ppid']:<8} {row['elapsed']:<9} "
            f"{row['cpu']:>5.1f}  {row['mem']:>5.1f}  "
            f"{row['rss_kb']/1024/1024:>6.2f}   {row['state']}"
        )
    if snapshot_memory:
        print(
            "MLX状态快照: " + "  ".join(
                f"{key}={value:.2f}GB" for key, value in sorted(snapshot_memory.items())
            )
        )

    print()
    print("最近完成项目（耗时来自状态库，不是监控估算）:")
    if not latest:
        print("  等待第一条完成……")
    for row in latest:
        print(
            f"  seq={row['seq']:>2}  {row['status']:<7}  "
            f"{float(row['elapsed_seconds'] or 0):>6.2f}s  "
            f"tokens={str(row['generation_tokens']):>4}  "
            f"finish={str(row['raw_finish_reason'] or 'null')}/{str(row['inferred_finish_reason'] or 'null')}  "
            f"degenerate={row['degenerate_reason'] or '-'}  "
            f"{compact_preview(row['clean_text'])}"
        )

    print()
    print("第65–75条边界状态:")
    if not boundary:
        print("  尚未到达第65条")
    for row in boundary:
        print(
            f"  seq={row['seq']:>2}  {row['status']:<7}  "
            f"elapsed={float(row['elapsed_seconds'] or 0):>6.2f}s  "
            f"tokens={str(row['generation_tokens']):>4}  "
            f"degenerate={row['degenerate_reason'] or '-'}"
        )

    snapshot = state["latest_snapshot"]
    if snapshot:
        print()
        print(
            f"最新内部状态快照: seq={snapshot['seq']} phase={snapshot['phase']} "
            f"created_at={snapshot['created_at']}"
        )
    print(
        f"SQLite: integrity={state['integrity_check']}  "
        f"foreign_key_errors={len(state['foreign_key_check'])}"
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Stop03-3F batch75 monitor")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    while not db_path.is_file():
        if args.once:
            raise RuntimeError(f"state_db_missing:{db_path}")
        print(f"等待状态库创建: {db_path}", flush=True)
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
                    f"状态库正在初始化或短暂锁定，{args.refresh_seconds:.1f}秒后重试: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                time.sleep(args.refresh_seconds)
                continue
            alive = process_alive(pid)
            processes = read_process_tree(pid) if alive else []
            display(state, pid, processes, alive=alive)
            if args.once:
                print("\n一次性监控快照完成。", flush=True)
                return 0
            if not alive:
                print("\n主进程已退出，监控结束。", flush=True)
                return 0
            time.sleep(args.refresh_seconds)
    except KeyboardInterrupt:
        print("\n监控已退出；诊断主进程未被停止。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
