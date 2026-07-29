#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3F three-worker corrected batch_generate validation.

Each worker receives exactly 75 fixed items, loads the model once, and must
cross its own item 71. Importing this module never imports mlx/mlx_vlm.
The central SQLite database is opened read-only; runtime writes go to test-output.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import queue
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import stop03_3f_qwenvl_batch75_diagnostic_v1 as batch75


SCRIPT_VERSION = "stop03_3f_qwenvl_batch_w3x75_validation_v1_20260711"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03_3f_qwenvl_batch_w3x75_validation"
WORKER_COUNT = 3
ITEMS_PER_WORKER = 75
TOTAL_ITEMS = WORKER_COUNT * ITEMS_PER_WORKER
MAX_TOKENS = 384


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assign_fixed_workers(tasks: Sequence[Mapping[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    if len(tasks) != TOTAL_ITEMS:
        raise RuntimeError(f"w3x75_input_count_mismatch:{len(tasks)}:{TOTAL_ITEMS}")
    candidate_ids = [str(task["candidate_id"]) for task in tasks]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError("w3x75_duplicate_candidate_id")
    assignments: dict[int, list[dict[str, Any]]] = {}
    for worker_id in range(1, WORKER_COUNT + 1):
        start = (worker_id - 1) * ITEMS_PER_WORKER
        assigned: list[dict[str, Any]] = []
        for worker_seq, source in enumerate(tasks[start : start + ITEMS_PER_WORKER], start=1):
            task = dict(source)
            task["worker_id"] = worker_id
            task["worker_seq"] = worker_seq
            task["global_seq"] = start + worker_seq
            task["execution_key"] = batch75.sha256_text(stable_json({
                "source_execution_key": str(source["execution_key"]),
                "script_version": SCRIPT_VERSION,
                "worker_id": worker_id,
                "worker_seq": worker_seq,
                "backend": "mlx_vlm.batch_generate.batch_size_1.corrected",
            }))
            assigned.append(task)
        assignments[worker_id] = assigned
    execution_keys = [task["execution_key"] for values in assignments.values() for task in values]
    if len(execution_keys) != len(set(execution_keys)):
        raise RuntimeError("w3x75_duplicate_execution_key")
    return assignments


class W3X75Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def initialize(
        self, assignments: Mapping[int, Sequence[Mapping[str, Any]]],
        metadata: Mapping[str, Any],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE workers (
                worker_id INTEGER PRIMARY KEY,
                assigned_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','loading','running','completed','stopped','failed')),
                pid INTEGER,
                model_load_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                loaded_at TEXT,
                finished_at TEXT,
                error_type TEXT,
                error_message TEXT,
                traceback_text TEXT
            );
            CREATE TABLE items (
                global_seq INTEGER PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                worker_seq INTEGER NOT NULL,
                candidate_id TEXT NOT NULL UNIQUE,
                execution_key TEXT NOT NULL UNIQUE,
                image_path TEXT NOT NULL,
                input_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending','running','success','review','failed')),
                started_at TEXT,
                finished_at TEXT,
                elapsed_seconds REAL,
                prompt_tokens INTEGER,
                generation_tokens INTEGER,
                generation_tps REAL,
                peak_memory_gb REAL,
                raw_finish_reason TEXT,
                inferred_finish_reason TEXT,
                response_shape TEXT,
                truncation_status TEXT,
                cleanup_status TEXT,
                cleanup_warnings TEXT,
                missing_required_sections_json TEXT,
                degenerate_reason TEXT,
                clean_text TEXT,
                clean_text_sha256 TEXT,
                error_type TEXT,
                error_message TEXT,
                traceback_text TEXT,
                FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                UNIQUE(worker_id,worker_seq)
            );
            CREATE TABLE state_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                global_seq INTEGER NOT NULL,
                worker_id INTEGER NOT NULL,
                worker_seq INTEGER NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(global_seq) REFERENCES items(global_seq),
                FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
            );
            CREATE INDEX idx_items_worker_status ON items(worker_id,status,worker_seq);
            CREATE INDEX idx_snapshots_worker_seq ON state_snapshots(worker_id,worker_seq,phase);
            """)
            con.executemany(
                "INSERT INTO metadata(key,value_json) VALUES(?,?)",
                [(str(key), stable_json(value)) for key, value in metadata.items()],
            )
            con.executemany(
                "INSERT INTO workers(worker_id,assigned_count,status) VALUES(?,?,'pending')",
                [(worker_id, len(tasks)) for worker_id, tasks in sorted(assignments.items())],
            )
            all_tasks = [task for worker_id in sorted(assignments) for task in assignments[worker_id]]
            con.executemany(
                """INSERT INTO items(
                global_seq,worker_id,worker_seq,candidate_id,execution_key,image_path,input_sha256,status
                ) VALUES(?,?,?,?,?,?,?,'pending')""",
                [
                    (
                        int(task["global_seq"]), int(task["worker_id"]),
                        int(task["worker_seq"]), str(task["candidate_id"]),
                        str(task["execution_key"]), str(task["image_path"]),
                        str(task.get("input_sha256") or ""),
                    )
                    for task in all_tasks
                ],
            )
            con.commit()
        finally:
            con.close()

    def set_metadata(self, key: str, value: Any) -> None:
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO metadata(key,value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, stable_json(value)),
            )
            con.commit()
        finally:
            con.close()

    def worker_loading(self, worker_id: int, pid: int) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE workers SET status='loading',pid=?,started_at=? WHERE worker_id=?",
                (pid, now_iso(), worker_id),
            )
            con.commit()
        finally:
            con.close()

    def worker_loaded(self, worker_id: int, model_load_count: int) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE workers SET status='running',model_load_count=?,loaded_at=? WHERE worker_id=?",
                (model_load_count, now_iso(), worker_id),
            )
            con.commit()
        finally:
            con.close()

    def worker_finished(self, worker_id: int, status: str) -> None:
        if status not in {"completed", "stopped", "failed"}:
            raise ValueError(f"invalid_worker_terminal_status:{status}")
        con = self.connect()
        try:
            con.execute(
                "UPDATE workers SET status=?,finished_at=? WHERE worker_id=?",
                (status, now_iso(), worker_id),
            )
            con.commit()
        finally:
            con.close()

    def worker_failed(self, worker_id: int, exc: BaseException) -> None:
        con = self.connect()
        try:
            con.execute(
                """UPDATE workers SET status='failed',finished_at=?,error_type=?,
                error_message=?,traceback_text=? WHERE worker_id=?""",
                (now_iso(), type(exc).__name__, str(exc), traceback.format_exc(), worker_id),
            )
            con.commit()
        finally:
            con.close()

    def start_item(self, global_seq: int) -> None:
        con = self.connect()
        try:
            cursor = con.execute(
                "UPDATE items SET status='running',started_at=? "
                "WHERE global_seq=? AND status='pending'",
                (now_iso(), global_seq),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"w3x75_item_claim_failed:{global_seq}")
            con.commit()
        finally:
            con.close()

    def snapshot(self, task: Mapping[str, Any], phase: str, payload: Mapping[str, Any]) -> None:
        con = self.connect()
        try:
            con.execute(
                """INSERT INTO state_snapshots(
                global_seq,worker_id,worker_seq,phase,created_at,payload_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    int(task["global_seq"]), int(task["worker_id"]),
                    int(task["worker_seq"]), phase, now_iso(), stable_json(payload),
                ),
            )
            con.commit()
        finally:
            con.close()

    def complete_item(
        self, global_seq: int, outcome: batch75.DiagnosticOutcome, elapsed: float,
    ) -> None:
        con = self.connect()
        try:
            con.execute(
                """UPDATE items SET status=?,finished_at=?,elapsed_seconds=?,
                prompt_tokens=?,generation_tokens=?,generation_tps=?,peak_memory_gb=?,
                raw_finish_reason=?,inferred_finish_reason=?,response_shape=?,
                truncation_status=?,cleanup_status=?,cleanup_warnings=?,
                missing_required_sections_json=?,degenerate_reason=?,clean_text=?,
                clean_text_sha256=?,error_type=NULL,error_message=NULL,traceback_text=NULL
                WHERE global_seq=?""",
                (
                    outcome.result_status, now_iso(), elapsed,
                    outcome.prompt_tokens, outcome.generation_tokens,
                    outcome.generation_tps, outcome.peak_memory_gb,
                    outcome.raw_finish_reason, outcome.inferred_finish_reason,
                    outcome.response_shape, outcome.truncation_status,
                    outcome.cleanup_status, outcome.cleanup_warnings,
                    stable_json(outcome.missing_required_sections),
                    outcome.degenerate_reason, outcome.clean_text,
                    batch75.sha256_text(outcome.clean_text) if outcome.clean_text else None,
                    global_seq,
                ),
            )
            con.commit()
        finally:
            con.close()

    def fail_item(self, global_seq: int, exc: BaseException, elapsed: float) -> None:
        con = self.connect()
        try:
            con.execute(
                """UPDATE items SET status='failed',finished_at=?,elapsed_seconds=?,
                error_type=?,error_message=?,traceback_text=? WHERE global_seq=?""",
                (now_iso(), elapsed, type(exc).__name__, str(exc), traceback.format_exc(), global_seq),
            )
            con.commit()
        finally:
            con.close()

    def recover_running(self) -> int:
        con = self.connect()
        try:
            cursor = con.execute(
                "UPDATE items SET status='pending',started_at=NULL WHERE status='running'"
            )
            con.commit()
            return cursor.rowcount
        finally:
            con.close()

    def summary(self) -> dict[str, Any]:
        con = self.connect()
        try:
            counts = {
                str(row[0]): int(row[1])
                for row in con.execute("SELECT status,COUNT(*) FROM items GROUP BY status")
            }
            workers = [dict(row) for row in con.execute(
                """SELECT w.worker_id,w.assigned_count,w.status,w.pid,w.model_load_count,
                SUM(CASE WHEN i.status='success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN i.status='review' THEN 1 ELSE 0 END) AS review,
                SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN i.status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) AS running,
                AVG(CASE WHEN i.status IN ('success','review','failed') THEN i.elapsed_seconds END) AS average_seconds
                FROM workers w LEFT JOIN items i ON i.worker_id=w.worker_id
                GROUP BY w.worker_id ORDER BY w.worker_id"""
            )]
            first_degenerate = con.execute(
                """SELECT worker_id,worker_seq,global_seq,candidate_id,degenerate_reason,
                generation_tokens FROM items WHERE degenerate_reason IS NOT NULL
                ORDER BY finished_at,global_seq LIMIT 1"""
            ).fetchone()
            boundary = [dict(row) for row in con.execute(
                """SELECT worker_id,worker_seq,status,generation_tokens,elapsed_seconds,
                inferred_finish_reason,degenerate_reason FROM items
                WHERE worker_seq BETWEEN 65 AND 75 ORDER BY worker_id,worker_seq"""
            )]
            return {
                "counts": {**counts, "total": int(con.execute("SELECT COUNT(*) FROM items").fetchone()[0])},
                "workers": workers,
                "first_degenerate": dict(first_degenerate) if first_degenerate else None,
                "boundary_65_75": boundary,
                "snapshot_count": int(con.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]),
                "integrity_check": str(con.execute("PRAGMA integrity_check").fetchone()[0]),
                "foreign_key_check": [list(row) for row in con.execute("PRAGMA foreign_key_check")],
                "candidate_id_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT candidate_id) FROM items"
                ).fetchone()[0]),
                "execution_key_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM items"
                ).fetchone()[0]),
            }
        finally:
            con.close()


def execute_worker_assignment(
    *, worker_id: int, tasks: Sequence[Mapping[str, Any]], store_path: Path,
    prompt: str, model_path: Path, max_tokens: int, stop_event: Any,
    report_queue: Any, adapter: Optional[batch75.PersistentCorrectedBatchAdapter] = None,
) -> None:
    store = W3X75Store(store_path)
    adapter = adapter or batch75.PersistentCorrectedBatchAdapter(
        model_path=model_path, max_tokens=max_tokens,
        backend=batch75.LocalCorrectedBatchBackend(),
    )
    completed = 0
    failed = 0
    fuse_reason: Optional[str] = None
    store.worker_loading(worker_id, os.getpid())
    try:
        adapter.load_once()
        store.worker_loaded(worker_id, adapter.model_load_count)
        print(
            f"[WORKER_LOADED] worker={worker_id} pid={os.getpid()} "
            f"model_load_count={adapter.model_load_count}",
            flush=True,
        )
        for task in tasks:
            if stop_event.is_set():
                break
            global_seq = int(task["global_seq"])
            worker_seq = int(task["worker_seq"])
            candidate_id = str(task["candidate_id"])
            store.start_item(global_seq)
            started = time.monotonic()
            try:
                store.snapshot(task, "before", adapter.snapshot())
                outcome = adapter.generate_one(
                    candidate_id=candidate_id,
                    image_path=str(task["image_path"]),
                    prompt=prompt,
                )
                elapsed = time.monotonic() - started
                store.snapshot(task, "after", adapter.snapshot())
                store.complete_item(global_seq, outcome, elapsed)
                completed += 1
                print(
                    f"[PROGRESS] worker={worker_id} worker_seq={worker_seq}/75 "
                    f"global_seq={global_seq}/225 candidate_id={candidate_id} "
                    f"status={outcome.result_status} tokens={outcome.generation_tokens} "
                    f"finish_raw={outcome.raw_finish_reason} "
                    f"finish_inferred={outcome.inferred_finish_reason} "
                    f"degenerate={outcome.degenerate_reason} elapsed_seconds={elapsed:.3f}",
                    flush=True,
                )
                if outcome.degenerate_reason:
                    fuse_reason = (
                        f"worker_{worker_id}_seq_{worker_seq}:"
                        f"{outcome.degenerate_reason}"
                    )
                    stop_event.set()
                    break
            except batch75.DeterministicDiagnosticError as exc:
                elapsed = time.monotonic() - started
                store.fail_item(global_seq, exc, elapsed)
                failed += 1
                fuse_reason = (
                    f"worker_{worker_id}_seq_{worker_seq}:"
                    f"{type(exc).__name__}:{exc}"
                )
                stop_event.set()
                print(f"[FUSE] {fuse_reason}", flush=True)
                break
            except Exception as exc:
                elapsed = time.monotonic() - started
                store.fail_item(global_seq, exc, elapsed)
                failed += 1
                fuse_reason = (
                    f"worker_{worker_id}_seq_{worker_seq}:"
                    f"unexpected:{type(exc).__name__}:{exc}"
                )
                stop_event.set()
                print(f"[FATAL] {fuse_reason}", flush=True)
                break
        terminal_status = (
            "completed" if completed == len(tasks) and failed == 0 and not fuse_reason
            else "stopped"
        )
        store.worker_finished(worker_id, terminal_status)
    except Exception as exc:
        failed += 1
        fuse_reason = f"worker_{worker_id}_initialization:{type(exc).__name__}:{exc}"
        store.worker_failed(worker_id, exc)
        stop_event.set()
        print(f"[FATAL] {fuse_reason}", flush=True)
    report_queue.put({
        "worker_id": worker_id,
        "model_load_count": adapter.model_load_count,
        "completed": completed,
        "failed": failed,
        "fuse_reason": fuse_reason,
    })


def _real_worker_entry(
    worker_id: int, tasks: Sequence[Mapping[str, Any]], store_path: str,
    prompt: str, model_path: str, max_tokens: int, stop_event: Any,
    report_queue: Any,
) -> None:
    execute_worker_assignment(
        worker_id=worker_id, tasks=tasks, store_path=Path(store_path),
        prompt=prompt, model_path=Path(model_path), max_tokens=max_tokens,
        stop_event=stop_event, report_queue=report_queue,
    )


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_real_validation(
    *, assignments: Mapping[int, Sequence[Mapping[str, Any]]], prompt: str,
    output_dir: Path, model_path: Path, max_tokens: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    state_db = output_dir / "run/stop03_3f_w3x75_state.sqlite"
    store = W3X75Store(state_db)
    store.initialize(assignments, {
        "script_version": SCRIPT_VERSION,
        "status": "INITIALIZED",
        "started_at": now_iso(),
        "parent_pid": os.getpid(),
        "worker_count": WORKER_COUNT,
        "items_per_worker": ITEMS_PER_WORKER,
        "total_items": TOTAL_ITEMS,
        "max_tokens": max_tokens,
        "batch_size": 1,
        "assignment_mode": "fixed_75_per_worker",
        "model_path": str(model_path),
        "prompt_sha256": batch75.sha256_text(prompt),
        "central_db_modified": False,
        "network_used": False,
        "download_used": False,
    })
    context = mp.get_context("spawn")
    stop_event = context.Event()
    report_queue = context.Queue()
    processes = [
        context.Process(
            target=_real_worker_entry,
            args=(
                worker_id, list(assignments[worker_id]), str(state_db), prompt,
                str(model_path), max_tokens, stop_event, report_queue,
            ),
            name=f"stop03f-batch-worker-{worker_id}",
        )
        for worker_id in range(1, WORKER_COUNT + 1)
    ]
    store.set_metadata("status", "RUNNING")
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    reports: list[dict[str, Any]] = []
    for _ in processes:
        try:
            reports.append(dict(report_queue.get(timeout=1.0)))
        except queue.Empty:
            break
    recovered_running = store.recover_running()
    summary = store.summary()
    process_exit_codes = {
        process.name: process.exitcode for process in processes
    }
    workers_pass = all(
        int(worker["success"] or 0) == ITEMS_PER_WORKER
        and int(worker["review"] or 0) == 0
        and int(worker["failed"] or 0) == 0
        and int(worker["pending"] or 0) == 0
        and int(worker["running"] or 0) == 0
        and int(worker["model_load_count"] or 0) == 1
        and str(worker["status"]) == "completed"
        for worker in summary["workers"]
    )
    if summary["first_degenerate"]:
        status = "BATCH_PATH_W3X75_DEGENERATE_REPRODUCED"
    elif workers_pass and all(code == 0 for code in process_exit_codes.values()):
        status = "BATCH_PATH_W3X75_PASS_PENDING_FORMAL_INTEGRATION"
    else:
        status = "BATCH_PATH_W3X75_FAILED"
    store.set_metadata("status", status)
    store.set_metadata("finished_at", now_iso())
    store.set_metadata("recovered_running", recovered_running)
    report = {
        "status": status,
        "script_version": SCRIPT_VERSION,
        "worker_reports": sorted(reports, key=lambda item: item["worker_id"]),
        "process_exit_codes": process_exit_codes,
        "recovered_running": recovered_running,
        "summary": summary,
        "state_db": str(state_db),
        "central_db_modified": False,
        "network_used": False,
        "download_used": False,
    }
    write_report(output_dir / "reports/final_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-3F corrected batch_generate 3x75 validation")
    parser.add_argument("--mode", required=True, choices=("real-validation",))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--workers", type=int, default=WORKER_COUNT)
    parser.add_argument("--items-per-worker", type=int, default=ITEMS_PER_WORKER)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--confirm-real-model-validation", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm_real_model_validation:
        raise RuntimeError("w3x75_real_model_validation_requires_explicit_confirmation")
    if args.workers != WORKER_COUNT or args.items_per_worker != ITEMS_PER_WORKER:
        raise RuntimeError("w3x75_fixed_assignment_must_be_3_workers_x_75_items")
    if args.max_tokens != MAX_TOKENS:
        raise RuntimeError(f"w3x75_max_tokens_must_be_{MAX_TOKENS}")
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    })
    output_dir = batch75.assert_test_output_path(Path(args.out))
    db_path = Path(args.db).resolve(strict=True)
    config_path = Path(args.config).resolve(strict=True)
    prompt_path = Path(args.prompt).resolve(strict=True)
    model_path = Path(args.model).resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if Path(str(config["model_path"])).resolve(strict=True) != model_path:
        raise RuntimeError("w3x75_model_does_not_match_formal_config")
    if int(config["default_max_tokens"]) != args.max_tokens:
        raise RuntimeError("w3x75_max_tokens_does_not_match_formal_config")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    tasks = batch75.load_tasks_readonly(
        db_path, limit=TOTAL_ITEMS,
        prompt_sha256=batch75.sha256_text(prompt), max_tokens=args.max_tokens,
    )
    assignments = assign_fixed_workers(tasks)
    report = run_real_validation(
        assignments=assignments, prompt=prompt, output_dir=output_dir,
        model_path=model_path, max_tokens=args.max_tokens,
    )
    if report["status"] == "BATCH_PATH_W3X75_PASS_PENDING_FORMAL_INTEGRATION":
        return 0
    if report["status"] == "BATCH_PATH_W3X75_DEGENERATE_REPRODUCED":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
