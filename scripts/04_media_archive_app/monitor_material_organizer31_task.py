#!/usr/bin/env python3
"""Read-only terminal monitor for the active Material Organizer task."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence


TERMINAL_STATES = {"success", "failed", "cancelled"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def tail_lines(path: Path, limit: int = 8, block_size: int = 8192) -> list[str]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        data = b""
        position = end
        while position > 0 and data.count(b"\n") <= limit:
            size = min(block_size, position)
            position -= size
            handle.seek(position)
            data = handle.read(size) + data
    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def process_stats(pids: list[int]) -> list[str]:
    valid = [str(pid) for pid in pids if pid > 1]
    if not valid:
        return []
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,ppid=,etime=,%cpu=,%mem=,rss=,state=", "-p", ",".join(valid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )
    except OSError as exc:
        return [f"进程资源统计暂不可用: {exc}"]
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 7:
            try:
                rss_gb = int(parts[5]) / 1024 / 1024
                parts[5] = f"{rss_gb:.2f}GB"
            except ValueError:
                pass
            rows.append(" ".join(parts))
    return rows


def database_summary(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        result: dict[str, int] = {}
        if "source_assets" in tables:
            result["active_sources"] = int(con.execute(
                "SELECT COUNT(*) FROM source_assets WHERE COALESCE(online_status,1)=1 "
                "AND COALESCE(is_deleted_or_missing,0)=0"
            ).fetchone()[0])
            result["missing_sources"] = int(con.execute(
                "SELECT COUNT(*) FROM source_assets WHERE COALESCE(online_status,1)=0 "
                "OR COALESCE(is_deleted_or_missing,0)!=0"
            ).fetchone()[0])
        if "step02_image_timelapse_keyframes" in tables:
            result["timelapse_groups"] = int(con.execute(
                "SELECT COUNT(DISTINCT sequence_id) FROM step02_image_timelapse_keyframes"
            ).fetchone()[0])
        return result
    finally:
        con.close()


def render(active_path: Path) -> str:
    active = load_json(active_path)
    state_path = Path(str(active.get("task_state_path") or ""))
    state = load_json(state_path) if state_path.is_file() else {}
    task_path = Path(str(active.get("task_path") or ""))
    task = load_json(task_path) if task_path.is_file() else {}
    database = Path(str(task.get("database") or active.get("database") or ""))
    db_counts = database_summary(database)
    stages = list(state.get("stages") or [])
    completed = sum(row.get("status") == "success" for row in stages)
    total = len(stages)
    percent = (completed / total * 100.0) if total else 0.0
    worker_pid = int(state.get("worker_pid") or 0)
    child_pid = int(state.get("current_child_pid") or 0)
    log_path = Path(str(task.get("log_path") or active.get("log_path") or ""))

    lines = [
        "素材大整理31 实时监测（只读，Control+C 只退出监测）",
        "=" * 72,
        f"任务: {state.get('task_name') or active.get('task_name') or '-'}",
        f"状态: {state.get('status') or '等待状态文件'}",
        f"当前阶段: {state.get('current_stage_name') or '-'}",
        f"阶段进度: {completed}/{total}  ({percent:.1f}%)",
        f"任务记录: {task_path}",
        (
            "数据库: 当前素材 {active_sources}，旧位置/缺失 {missing_sources}，"
            "延时摄影 {timelapse_groups} 组"
        ).format(
            active_sources=db_counts.get("active_sources", "-"),
            missing_sources=db_counts.get("missing_sources", "-"),
            timelapse_groups=db_counts.get("timelapse_groups", "-"),
        ),
        "",
        "阶段明细:",
    ]
    for index, row in enumerate(stages, 1):
        elapsed = row.get("elapsed_seconds")
        elapsed_text = f"  {elapsed:.1f}秒" if isinstance(elapsed, (int, float)) else ""
        lines.append(f"  {index:>2}. {row.get('status','pending'):<9} {row.get('name','-')}{elapsed_text}")
    lines.extend(["", "进程 PID PPID ELAPSED CPU% MEM% RSS STATE:"])
    lines.extend("  " + row for row in process_stats([worker_pid, child_pid]))
    lines.extend(["", "最近输出:"])
    lines.extend("  " + row for row in tail_lines(log_path))
    if state.get("error"):
        lines.extend(["", f"错误: {state['error']}"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--active-library",
        type=Path,
        default=Path.home() / "Library/Application Support/素材大整理/runtime/active_library.json",
    )
    parser.add_argument("--refresh-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.active_library.is_file():
        print(f"尚未找到活动任务: {args.active_library}")
        return 2
    while True:
        try:
            output = render(args.active_library)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            output = f"正在等候状态文件稳定写入: {exc}"
        print("\033[2J\033[H" + output, flush=True)
        try:
            active = load_json(args.active_library)
            state_path = Path(str(active.get("task_state_path") or ""))
            status = load_json(state_path).get("status") if state_path.is_file() else None
        except (OSError, ValueError, json.JSONDecodeError):
            status = None
        if args.once or status in TERMINAL_STATES:
            return 0 if status in {None, "success"} else 1
        time.sleep(max(1.0, args.refresh_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
