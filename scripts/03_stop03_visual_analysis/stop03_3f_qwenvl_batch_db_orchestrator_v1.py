#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3F formal corrected-batch Qwen-VL DB orchestrator.

Reads the frozen V25 execution view, creates an isolated backend-aware run,
executes three fixed persistent workers, and writes results with short central
SQLite transactions. Importing this module never imports mlx/mlx_vlm.
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
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import qwenvl_output_contract_v2 as output_contract
import stop03_2_v25_candidate_contract_lock as contract_lock
import stop03_3c_qwenvl_db_orchestrator_v1 as contract
import stop03_3f_qwenvl_batch75_diagnostic_v1 as batch75


SCRIPT_VERSION = "stop03_3f_qwenvl_batch_db_orchestrator_v1_20260711"
BACKEND_VERSION = "mlx_vlm_batch_generate_b1_greedy_v1"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_QWEN_PYTHON = Path("/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python")
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03_3f_qwenvl_batch_db_full"
WORKER_COUNT = 3
TOTAL_ITEMS = 336
ITEMS_PER_WORKER = 112
MAX_TOKENS = 384
RESUME_STATUSES = {
    "pending", "running", "failed", "review", "truncated", "parse_failed",
    "missing_required_fields", "input_fingerprint_mismatch",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def script_sha256() -> str:
    return contract.sha256_file(Path(__file__).resolve())


def backend_execution_key(
    row: Mapping[str, Any], *, model_fingerprint_sha256: str,
    prompt_sha256: str, max_tokens: int, current_script_sha256: str,
) -> str:
    return contract.sha256_text(stable_json({
        "candidate_id": str(row.get("candidate_id") or ""),
        "runtime_visual_file_sha256": str(row.get("runtime_visual_file_sha256") or ""),
        "model_fingerprint_sha256": model_fingerprint_sha256,
        "prompt_sha256": prompt_sha256,
        "output_contract_version": output_contract.CONTRACT_VERSION,
        "max_tokens": max_tokens,
        "backend_version": BACKEND_VERSION,
        "script_sha256": current_script_sha256,
    }))


def load_frozen_queue(db: Path) -> list[dict[str, Any]]:
    con = contract.readonly_connection(db)
    try:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM v_stop03_2_v25_qwenvl_execution_queue ORDER BY candidate_id"
        )]
    finally:
        con.close()
    if len(rows) != TOTAL_ITEMS:
        raise RuntimeError(f"formal_batch_queue_count_mismatch:{len(rows)}:{TOTAL_ITEMS}")
    return rows


def prepare_execution_rows(
    rows: Sequence[Mapping[str, Any]], *, pre: Mapping[str, Any],
    max_tokens: int,
) -> list[dict[str, Any]]:
    current_sha = script_sha256()
    prepared: list[dict[str, Any]] = []
    for global_seq, source in enumerate(rows, start=1):
        worker_id = ((global_seq - 1) // ITEMS_PER_WORKER) + 1
        worker_seq = ((global_seq - 1) % ITEMS_PER_WORKER) + 1
        row = dict(source)
        row.update({
            "global_seq": global_seq,
            "assigned_worker_id": worker_id,
            "worker_seq": worker_seq,
            "execution_key": backend_execution_key(
                row,
                model_fingerprint_sha256=str(pre["model_fingerprint_sha256"]),
                prompt_sha256=str(pre["prompt_sha256"]),
                max_tokens=max_tokens,
                current_script_sha256=current_sha,
            ),
        })
        prepared.append(row)
    if Counter(row["assigned_worker_id"] for row in prepared) != Counter({1: 112, 2: 112, 3: 112}):
        raise RuntimeError("formal_batch_fixed_assignment_invalid")
    keys = [str(row["execution_key"]) for row in prepared]
    if len(keys) != len(set(keys)):
        raise RuntimeError("formal_batch_execution_key_duplicate")
    return prepared


def fixed_assignments(rows: Sequence[Mapping[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    assignments = {worker_id: [] for worker_id in range(1, WORKER_COUNT + 1)}
    for source in rows:
        row = dict(source)
        assignments[int(row["assigned_worker_id"])].append(row)
    for worker_id, values in assignments.items():
        values.sort(key=lambda item: int(item["worker_seq"]))
        expected = list(range(1, ITEMS_PER_WORKER + 1))
        if [int(item["worker_seq"]) for item in values] != expected:
            raise RuntimeError(f"formal_batch_worker_assignment_incomplete:{worker_id}")
    return assignments


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def create_run_and_items(
    *, db: Path, rows: Sequence[Mapping[str, Any]], pre: Mapping[str, Any],
    prompt_path: Path, max_tokens: int, workers: int,
) -> str:
    run_id = "stop03_3f_batch_db_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    created = contract.now_iso()
    current_sha = script_sha256()
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        for row in rows:
            existing = con.execute(
                "SELECT run_id,status FROM stop03_3_qwenvl_run_items WHERE execution_key=?",
                (row["execution_key"],),
            ).fetchone()
            if existing is not None:
                raise RuntimeError(
                    f"batch_execution_key_already_exists:{existing['run_id']}:{existing['status']}"
                )
        con.execute(
            """INSERT INTO stop03_3_qwenvl_runs
            (run_id,v25_contract_name,candidate_id_set_sha256,candidate_semantic_digest_sha256,
             candidate_count,model_name,model_path,model_sha256,model_config_sha256,
             model_tokenizer_files_json,model_tokenizer_files_sha256,model_inventory_json,
             model_inventory_sha256,model_fingerprint_sha256,prompt_path,prompt_sha256,
             orchestrator_config_sha256,output_contract_version,max_tokens,temperature,top_p,
             workers,script_sha256,status,pending_count,started_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, contract_lock.CONTRACT_NAME,
                pre["contract"]["candidate_id_set_sha256"],
                pre["contract"]["candidate_semantic_digest_sha256"],
                len(rows), Path(str(pre["model_path"])).name, pre["model_path"],
                pre["model_sha256"], pre["model_config_sha256"],
                pre["model_tokenizer_files_json"], pre["model_tokenizer_files_sha256"],
                pre["model_inventory_json"], pre["model_inventory_sha256"],
                pre["model_fingerprint_sha256"], str(prompt_path), pre["prompt_sha256"],
                pre["config_sha256"], output_contract.CONTRACT_VERSION, max_tokens,
                pre["temperature"], pre["top_p"], workers, current_sha,
                "pending", len(rows), created,
            ),
        )
        for row in rows:
            run_item_id = contract.stable_id("qri_", row["execution_key"])
            con.execute(
                """INSERT INTO stop03_3_qwenvl_run_items
                (run_item_id,run_id,candidate_id,execution_key,source_content_id,visual_unit_id,
                 canonical_visual_unit_id,derived_id,candidate_role,reason_codes,policy_version,
                 media_type,time_position_ms,runtime_visual_file,runtime_visual_file_sha256,
                 status,attempt_count,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_item_id, run_id, row["candidate_id"], row["execution_key"],
                    row["source_content_id"], row["visual_unit_id"],
                    row["canonical_visual_unit_id"], row["derived_id"],
                    row["candidate_role"], row["reason_codes"], row["policy_version"],
                    row["media_type"], row["time_position_ms"],
                    row["runtime_visual_file"], row["runtime_visual_file_sha256"],
                    "pending", 0, created,
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return run_id


def load_run_rows(db: Path, run_id: str) -> list[dict[str, Any]]:
    con = contract.readonly_connection(db)
    try:
        rows = [dict(row) for row in con.execute(
            """SELECT i.*,f.runtime_visual_file_sha256 AS frozen_input_sha256
            FROM stop03_3_qwenvl_run_items i
            JOIN stop03_2_candidate_queue_frozen_v25 f ON f.candidate_id=i.candidate_id
            WHERE i.run_id=? ORDER BY i.candidate_id""",
            (run_id,),
        )]
    finally:
        con.close()
    if len(rows) != TOTAL_ITEMS:
        raise RuntimeError(f"formal_batch_run_item_count_mismatch:{len(rows)}")
    for global_seq, row in enumerate(rows, start=1):
        row["global_seq"] = global_seq
        row["assigned_worker_id"] = ((global_seq - 1) // ITEMS_PER_WORKER) + 1
        row["worker_seq"] = ((global_seq - 1) % ITEMS_PER_WORKER) + 1
    return rows


def prepare_resume(db: Path, run_id: str, *, pre: Mapping[str, Any]) -> None:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            "SELECT * FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"batch_resume_run_missing:{run_id}")
        checks = {
            "script_sha256": script_sha256(),
            "model_fingerprint_sha256": str(pre["model_fingerprint_sha256"]),
            "prompt_sha256": str(pre["prompt_sha256"]),
            "output_contract_version": output_contract.CONTRACT_VERSION,
        }
        for field, expected in checks.items():
            if str(run[field]) != expected:
                raise RuntimeError(f"batch_resume_{field}_mismatch")
        if int(run["max_tokens"]) != MAX_TOKENS or int(run["candidate_count"]) != TOTAL_ITEMS:
            raise RuntimeError("batch_resume_locked_settings_mismatch")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items SET status='pending',started_at=NULL,
            finished_at=NULL WHERE run_id=? AND status='running'""",
            (run_id,),
        )
        con.execute(
            "UPDATE stop03_3_qwenvl_runs SET status='running',workers=?,finished_at=NULL WHERE run_id=?",
            (WORKER_COUNT, run_id),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_item(db: Path, run_item_id: str) -> bool:
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in RESUME_STATUSES)
        cursor = con.execute(
            f"""UPDATE stop03_3_qwenvl_run_items
            SET status='running',attempt_count=attempt_count+1,started_at=?,finished_at=NULL
            WHERE run_item_id=? AND status IN ({placeholders})
            AND NOT EXISTS (
                SELECT 1 FROM stop03_3_qwenvl_results r
                WHERE r.run_item_id=stop03_3_qwenvl_run_items.run_item_id
                  AND r.result_status='success'
            )""",
            (contract.now_iso(), run_item_id, *sorted(RESUME_STATUSES)),
        )
        con.commit()
        return cursor.rowcount == 1
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def result_status(outcome: batch75.DiagnosticOutcome) -> str:
    if not outcome.clean_text:
        return "failed"
    if outcome.degenerate_reason:
        return "review"
    if outcome.missing_required_sections:
        return "missing_required_fields"
    if outcome.truncation_status == "truncated":
        return "truncated"
    if outcome.cleanup_status != "ok":
        return "parse_failed"
    return "success"


def write_item_artifacts(
    *, out: Path, row: Mapping[str, Any], outcome: batch75.DiagnosticOutcome,
    worker_id: int, worker_seq: int, elapsed: float,
) -> dict[str, Any]:
    candidate_id = str(row["candidate_id"])
    raw_path = out / "raw_outputs" / f"{candidate_id}.json"
    clean_path = out / "clean_outputs" / f"{candidate_id}.txt"
    stderr_path = out / "stderr" / f"{candidate_id}.txt"
    metrics_path = out / "metrics" / f"{candidate_id}.json"
    metrics = {
        "backend_version": BACKEND_VERSION,
        "response_shape": outcome.response_shape,
        "prompt_tokens": outcome.prompt_tokens,
        "generation_tokens": outcome.generation_tokens,
        "generation_tps": outcome.generation_tps,
        "peak_memory_gb": outcome.peak_memory_gb,
        "raw_finish_reason": outcome.raw_finish_reason,
        "inferred_finish_reason": outcome.inferred_finish_reason,
        "elapsed_seconds": elapsed,
        "worker_id": worker_id,
        "worker_seq": worker_seq,
        "degenerate_reason": outcome.degenerate_reason,
    }
    raw_payload = {
        "candidate_id": candidate_id,
        "backend_version": BACKEND_VERSION,
        "response_shape": outcome.response_shape,
        "text": outcome.clean_text,
        "metrics": metrics,
    }
    write_json_atomic(raw_path, raw_payload)
    write_text_atomic(clean_path, outcome.clean_text + "\n")
    write_text_atomic(stderr_path, "")
    write_json_atomic(metrics_path, metrics)
    return {
        "raw_path": raw_path,
        "clean_path": clean_path,
        "stderr_path": stderr_path,
        "metrics_path": metrics_path,
        "metrics": metrics,
    }


def persist_result(
    *, db: Path, run_id: str, row: Mapping[str, Any],
    outcome: batch75.DiagnosticOutcome, artifacts: Mapping[str, Any],
    pre: Mapping[str, Any],
) -> str:
    status = result_status(outcome)
    raw_path = Path(artifacts["raw_path"])
    stderr_path = Path(artifacts["stderr_path"])
    metrics_path = Path(artifacts["metrics_path"])
    runtime_metrics_json = stable_json(artifacts["metrics"])
    current_sha = script_sha256()
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items SET status=?,last_error_code=?,
            last_error_message=?,finished_at=? WHERE run_item_id=?""",
            (
                status, "" if status == "success" else status,
                "" if status == "success" else str(outcome.degenerate_reason or outcome.cleanup_warnings),
                contract.now_iso(), row["run_item_id"],
            ),
        )
        result_id = contract.stable_id("qres_", row["execution_key"])
        evidence_id = contract.stable_id("qev_", row["execution_key"])
        con.execute(
            """INSERT OR REPLACE INTO stop03_3_qwenvl_results
            (result_id,run_id,run_item_id,candidate_id,execution_key,evidence_id,
             source_content_id,visual_unit_id,canonical_visual_unit_id,derived_id,
             candidate_role,reason_codes,policy_version,result_status,clean_text,
             qwen_text_preview,clean_text_sha256,raw_stdout_path,raw_stdout_sha256,
             stderr_path,stderr_sha256,metrics_path,metrics_sha256,runtime_metrics_json,
             prompt_tokens,generation_tokens,peak_memory_gb,finish_reason,truncation_status,
             cleanup_status,cleanup_warnings,output_contract_version,
             runtime_visual_file_sha256,model_sha256,model_config_sha256,
             model_tokenizer_files_json,model_tokenizer_files_sha256,
             model_inventory_sha256,model_fingerprint_sha256,prompt_sha256,
             orchestrator_config_sha256,script_sha256,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result_id, run_id, row["run_item_id"], row["candidate_id"],
                row["execution_key"], evidence_id, row["source_content_id"],
                row["visual_unit_id"], row["canonical_visual_unit_id"], row["derived_id"],
                row["candidate_role"], row["reason_codes"], row["policy_version"], status,
                outcome.clean_text, outcome.clean_text[:500],
                batch75.sha256_text(outcome.clean_text), str(raw_path),
                contract.sha256_file(raw_path), str(stderr_path),
                contract.sha256_file(stderr_path), str(metrics_path),
                contract.sha256_file(metrics_path), runtime_metrics_json,
                outcome.prompt_tokens, outcome.generation_tokens, outcome.peak_memory_gb,
                outcome.inferred_finish_reason,
                "truncated" if outcome.truncation_status == "truncated" else "complete",
                outcome.cleanup_status, outcome.cleanup_warnings,
                output_contract.CONTRACT_VERSION, row["runtime_visual_file_sha256"],
                pre["model_sha256"], pre["model_config_sha256"],
                pre["model_tokenizer_files_json"], pre["model_tokenizer_files_sha256"],
                pre["model_inventory_sha256"], pre["model_fingerprint_sha256"],
                pre["prompt_sha256"], pre["config_sha256"], current_sha,
                contract.now_iso(),
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return status


def mark_item_failed(db: Path, row: Mapping[str, Any], exc: BaseException) -> None:
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items SET status='failed',last_error_code=?,
            last_error_message=?,finished_at=? WHERE run_item_id=?""",
            (type(exc).__name__, str(exc), contract.now_iso(), row["run_item_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def write_worker_status(out: Path, worker_id: int, value: Mapping[str, Any]) -> None:
    write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", value)


def execute_worker(
    *, worker_id: int, rows: Sequence[Mapping[str, Any]], db: Path, out: Path,
    run_id: str, prompt: str, model_path: Path, max_tokens: int,
    pre: Mapping[str, Any], stop_event: Any, report_queue: Any,
    adapter: Optional[batch75.PersistentCorrectedBatchAdapter] = None,
) -> None:
    adapter = adapter or batch75.PersistentCorrectedBatchAdapter(
        model_path=model_path, max_tokens=max_tokens,
        backend=batch75.LocalCorrectedBatchBackend(),
    )
    completed = 0
    success = 0
    non_success = 0
    skipped = 0
    elapsed_total = 0.0
    fuse_reason: Optional[str] = None
    status = {
        "worker_id": worker_id, "pid": os.getpid(), "lifecycle": "loading",
        "assigned_count": len(rows), "model_load_count": 0, "completed": 0,
        "success": 0, "non_success": 0, "skipped_success": 0,
        "current_candidate_id": None, "current_worker_seq": None,
        "average_seconds": None, "last_completed": None,
    }
    write_worker_status(out, worker_id, status)
    try:
        adapter.load_once()
        status.update({"lifecycle": "running", "model_load_count": adapter.model_load_count})
        write_worker_status(out, worker_id, status)
        for row in rows:
            if stop_event.is_set():
                break
            worker_seq = int(row["worker_seq"])
            if not claim_item(db, str(row["run_item_id"])):
                skipped += 1
                status["skipped_success"] = skipped
                write_worker_status(out, worker_id, status)
                continue
            status.update({
                "current_candidate_id": row["candidate_id"],
                "current_worker_seq": worker_seq,
                "current_started_at": contract.now_iso(),
            })
            write_worker_status(out, worker_id, status)
            started = time.monotonic()
            try:
                outcome = adapter.generate_one(
                    candidate_id=str(row["candidate_id"]),
                    image_path=str(row["runtime_visual_file"]),
                    prompt=prompt,
                )
                elapsed = time.monotonic() - started
                artifacts = write_item_artifacts(
                    out=out, row=row, outcome=outcome, worker_id=worker_id,
                    worker_seq=worker_seq, elapsed=elapsed,
                )
                item_status = persist_result(
                    db=db, run_id=run_id, row=row, outcome=outcome,
                    artifacts=artifacts, pre=pre,
                )
                completed += 1
                elapsed_total += elapsed
                if item_status == "success":
                    success += 1
                else:
                    non_success += 1
                event = {
                    "timestamp": contract.now_iso(), "run_id": run_id,
                    "worker_id": worker_id, "worker_seq": worker_seq,
                    "candidate_id": row["candidate_id"], "status": item_status,
                    "generation_tokens": outcome.generation_tokens,
                    "raw_finish_reason": outcome.raw_finish_reason,
                    "inferred_finish_reason": outcome.inferred_finish_reason,
                    "degenerate_reason": outcome.degenerate_reason,
                    "elapsed_seconds": elapsed,
                }
                append_jsonl(out / "logs" / f"progress_worker_{worker_id}.jsonl", event)
                status.update({
                    "completed": completed, "success": success,
                    "non_success": non_success,
                    "average_seconds": elapsed_total / completed,
                    "current_candidate_id": None, "current_worker_seq": None,
                    "last_completed": event,
                })
                try:
                    snapshot = adapter.snapshot()
                    status["mlx_memory_bytes"] = snapshot.get("mlx_memory_bytes", {})
                except Exception:
                    pass
                write_worker_status(out, worker_id, status)
                print(
                    f"[PROGRESS] run_id={run_id} worker={worker_id} "
                    f"worker_seq={worker_seq}/{len(rows)} candidate_id={row['candidate_id']} "
                    f"status={item_status} tokens={outcome.generation_tokens} "
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
                mark_item_failed(db, row, exc)
                non_success += 1
                fuse_reason = f"worker_{worker_id}_seq_{worker_seq}:{type(exc).__name__}:{exc}"
                stop_event.set()
                break
            except Exception as exc:
                mark_item_failed(db, row, exc)
                non_success += 1
                fuse_reason = f"worker_{worker_id}_seq_{worker_seq}:unexpected:{type(exc).__name__}:{exc}"
                stop_event.set()
                break
        status["lifecycle"] = (
            "completed" if completed + skipped == len(rows) and not fuse_reason else "stopped"
        )
    except Exception as exc:
        fuse_reason = f"worker_{worker_id}_initialization:{type(exc).__name__}:{exc}"
        status.update({
            "lifecycle": "failed", "error_type": type(exc).__name__,
            "error_message": str(exc), "traceback": traceback.format_exc(),
        })
        stop_event.set()
    status.update({
        "current_candidate_id": None, "current_worker_seq": None,
        "finished_at": contract.now_iso(), "fuse_reason": fuse_reason,
    })
    write_worker_status(out, worker_id, status)
    report_queue.put(status)


def _real_worker_entry(
    worker_id: int, rows: Sequence[Mapping[str, Any]], db: str, out: str,
    run_id: str, prompt: str, model_path: str, max_tokens: int,
    pre: Mapping[str, Any], stop_event: Any, report_queue: Any,
) -> None:
    execute_worker(
        worker_id=worker_id, rows=rows, db=Path(db), out=Path(out),
        run_id=run_id, prompt=prompt, model_path=Path(model_path),
        max_tokens=max_tokens, pre=pre, stop_event=stop_event,
        report_queue=report_queue,
    )


def run_counts(db: Path, run_id: str) -> dict[str, int]:
    con = contract.readonly_connection(db)
    try:
        counts = Counter({
            str(row[0]): int(row[1]) for row in con.execute(
                "SELECT status,COUNT(*) FROM stop03_3_qwenvl_run_items "
                "WHERE run_id=? GROUP BY status", (run_id,),
            )
        })
        counts["total"] = int(con.execute(
            "SELECT COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        counts["results"] = int(con.execute(
            "SELECT COUNT(*) FROM stop03_3_qwenvl_results WHERE run_id=?", (run_id,)
        ).fetchone()[0])
    finally:
        con.close()
    return dict(counts)


def finalize_run(db: Path, run_id: str) -> dict[str, int]:
    counts = run_counts(db, run_id)
    success = counts.get("success", 0)
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    review = counts.get("review", 0)
    failed = sum(counts.get(name, 0) for name in (
        "failed", "truncated", "parse_failed", "missing_required_fields",
        "input_fingerprint_mismatch",
    ))
    if success == counts["total"]:
        status = "success"
    elif success or review or failed:
        status = "partial"
    else:
        status = "failed"
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_runs SET status=?,pending_count=?,success_count=?,
            failed_count=?,review_count=?,finished_at=?,error_message=? WHERE run_id=?""",
            (
                status, pending, success, failed, review, contract.now_iso(),
                "" if status == "success" else f"remaining_running={running}", run_id,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return counts


def run_workers(
    *, db: Path, out: Path, run_id: str, rows: Sequence[Mapping[str, Any]],
    prompt: str, model_path: Path, max_tokens: int, pre: Mapping[str, Any],
) -> dict[str, Any]:
    assignments = fixed_assignments(rows)
    context = mp.get_context("spawn")
    stop_event = context.Event()
    report_queue = context.Queue()
    processes = [
        context.Process(
            target=_real_worker_entry,
            args=(
                worker_id, assignments[worker_id], str(db), str(out), run_id,
                prompt, str(model_path), max_tokens, dict(pre), stop_event,
                report_queue,
            ),
            name=f"stop03f-batch-db-worker-{worker_id}",
        )
        for worker_id in range(1, WORKER_COUNT + 1)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        stop_event.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=10)
        raise
    reports: list[dict[str, Any]] = []
    for _ in processes:
        try:
            reports.append(dict(report_queue.get(timeout=1.0)))
        except queue.Empty:
            break
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE stop03_3_qwenvl_run_items SET status='pending',started_at=NULL "
            "WHERE run_id=? AND status='running'", (run_id,),
        )
        con.commit()
    finally:
        con.close()
    counts = finalize_run(db, run_id)
    process_exit_codes = {process.name: process.exitcode for process in processes}
    readback = contract.readback_run(db, run_id, expected_count=TOTAL_ITEMS)
    status = (
        "FORMAL_BATCH_DB_FULL_PASS"
        if counts.get("success", 0) == TOTAL_ITEMS
        and all(code == 0 for code in process_exit_codes.values())
        and readback["status"] == "PASS"
        else "FORMAL_BATCH_DB_PARTIAL_OR_FAILED"
    )
    return {
        "status": status, "run_id": run_id, "counts": counts,
        "worker_reports": sorted(reports, key=lambda item: item["worker_id"]),
        "process_exit_codes": process_exit_codes, "readback": readback,
    }


def build_preflight(
    *, db: Path, out: Path, config_path: Path, prompt_path: Path,
    model_path: Path, qwen_python: Path, max_tokens: int, mode: str,
) -> dict[str, Any]:
    pre = contract.preflight(
        db=db, out=out, config_path=config_path, model_path=model_path,
        qwen_python=qwen_python, prompt_path=prompt_path,
        max_tokens=max_tokens, mode="run" if mode in {"run", "resume"} else mode,
        allow_low_token_debug=False, allow_simulation=False,
    )
    pre = dict(pre)
    pre.update({
        "script_version": SCRIPT_VERSION,
        "backend_version": BACKEND_VERSION,
        "script_sha256": script_sha256(),
        "fixed_workers": WORKER_COUNT,
        "items_per_worker": ITEMS_PER_WORKER,
    })
    return pre


def dry_run(
    *, db: Path, out: Path, pre: Mapping[str, Any], max_tokens: int,
) -> dict[str, Any]:
    rows = prepare_execution_rows(load_frozen_queue(db), pre=pre, max_tokens=max_tokens)
    out.mkdir(parents=True, exist_ok=False)
    plan_path = out / "manifests/formal_batch_execution_plan.jsonl"
    for row in rows:
        append_jsonl(plan_path, row)
    result = {
        **dict(pre), "mode": "dry-run", "status": "PASS",
        "execution_plan_count": len(rows),
        "execution_key_unique_count": len({row["execution_key"] for row in rows}),
        "worker_counts": dict(Counter(row["assigned_worker_id"] for row in rows)),
        "central_db_modified": False, "model_run": False,
        "plan_path": str(plan_path),
    }
    write_json_atomic(out / "reports/dry_run_summary.json", result)
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-3F formal corrected-batch DB orchestrator")
    parser.add_argument("--mode", required=True, choices=("preflight", "dry-run", "run", "resume"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--qwen-python", default=str(DEFAULT_QWEN_PYTHON))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--workers", type=int, default=WORKER_COUNT)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.workers != WORKER_COUNT or args.max_tokens != MAX_TOKENS:
        raise RuntimeError("formal_batch_locked_settings_require_workers_3_max_tokens_384")
    if args.mode in {"run", "resume"} and not args.confirm_central_db_write:
        raise RuntimeError("formal_batch_central_db_write_requires_explicit_confirmation")
    db = Path(args.db).resolve(strict=True)
    config_path = Path(args.config).resolve(strict=True)
    prompt_path = Path(args.prompt).resolve(strict=True)
    model_path = Path(args.model).resolve(strict=True)
    qwen_python = Path(args.qwen_python).resolve(strict=True)
    out = batch75.assert_test_output_path(Path(args.out))
    pre = build_preflight(
        db=db, out=out, config_path=config_path, prompt_path=prompt_path,
        model_path=model_path, qwen_python=qwen_python,
        max_tokens=args.max_tokens, mode=args.mode,
    )
    if pre["status"] != "PASS":
        print(json.dumps(pre, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        return 2
    if args.mode == "preflight":
        result = pre
    elif args.mode == "dry-run":
        result = dry_run(db=db, out=out, pre=pre, max_tokens=args.max_tokens)
    else:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if args.mode == "run":
            out.mkdir(parents=True, exist_ok=False)
            prepared = prepare_execution_rows(
                load_frozen_queue(db), pre=pre, max_tokens=args.max_tokens,
            )
            run_id = create_run_and_items(
                db=db, rows=prepared, pre=pre, prompt_path=prompt_path,
                max_tokens=args.max_tokens, workers=args.workers,
            )
            write_text_atomic(out / "run_id.txt", run_id + "\n")
            rows = load_run_rows(db, run_id)
        else:
            if not args.run_id:
                raise RuntimeError("formal_batch_resume_requires_run_id")
            if not out.is_dir():
                raise RuntimeError(f"formal_batch_resume_out_missing:{out}")
            run_id = args.run_id
            prepare_resume(db, run_id, pre=pre)
            write_text_atomic(out / "run_id.txt", run_id + "\n")
            rows = load_run_rows(db, run_id)
        result = run_workers(
            db=db, out=out, run_id=run_id, rows=rows, prompt=prompt,
            model_path=model_path, max_tokens=args.max_tokens, pre=pre,
        )
        result.update({
            "backend_version": BACKEND_VERSION,
            "central_db_modified": True,
            "network_used": False, "download_used": False,
        })
        write_json_atomic(out / "reports/final_report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("status") in {"PASS", "FORMAL_BATCH_DB_FULL_PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
