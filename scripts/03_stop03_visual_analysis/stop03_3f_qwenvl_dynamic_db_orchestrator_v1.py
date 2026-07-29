#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3F corrected batch Qwen-VL dynamic DB orchestrator.

The queue length is discovered from the frozen execution view. Workers keep one
model instance each and atomically claim the next available DB item. Inference
never runs while a SQLite transaction is open.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import queue
import signal
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


SCRIPT_VERSION = "stop03_3f_qwenvl_dynamic_db_orchestrator_v1_20260716"
BACKEND_VERSION = "mlx_vlm_batch_generate_dynamic_claim_greedy_v1"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_QWEN_PYTHON = Path("/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python")
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03_3f_qwenvl_dynamic_db_full"
DEFAULT_WORKERS = 3
DEFAULT_MAX_TOKENS = 384
DEFAULT_MAX_ATTEMPTS = 2
COMPACT_RETRY_PROMPT_VERSION = "compact_retry_prompt_v1"
COMPATIBLE_RESUME_SCRIPT_SHAS = {
    # Initial dynamic-claim version used by the first formal run. Compatibility
    # is intentionally exact and requires an explicit CLI confirmation.
    "682e60a00e8ecbf82d95970f1350fdb58c899d019b424429a52c389a8b116013",
}
RETRYABLE_STATUSES = (
    "failed",
    "review",
    "truncated",
    "parse_failed",
    "missing_required_fields",
    "input_fingerprint_mismatch",
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def current_script_sha256() -> str:
    return contract.sha256_file(Path(__file__).resolve())


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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def effective_prompt(base_prompt: str, attempt_count: int) -> tuple[str, str]:
    if attempt_count <= 1:
        return base_prompt, "base_prompt"
    retry_instruction = """

【格式恢复约束】
上一次回答过长、重复或未完成。请重新独立观察图像，并严格只输出以下三段：
1）概括：一句话，不超过60个中文字符。
2）元素：一句话，合并同类项，不超过160个中文字符；同一物体不得重复。
3）检索价值：一句话，不超过80个中文字符。
禁止重复词组、禁止逐个穷举相似物体；第三段结束后立即停止。
""".strip()
    return f"{base_prompt.rstrip()}\n\n{retry_instruction}", COMPACT_RETRY_PROMPT_VERSION


def load_frozen_queue(db: Path) -> list[dict[str, Any]]:
    con = contract.readonly_connection(db)
    try:
        rows = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM v_stop03_2_v25_qwenvl_execution_queue ORDER BY candidate_id"
            )
        ]
    finally:
        con.close()
    if not rows:
        raise RuntimeError("dynamic_db_queue_empty")
    candidate_ids = [str(row["candidate_id"]) for row in rows]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError("dynamic_db_candidate_id_duplicate")
    return rows


def execution_key(
    row: Mapping[str, Any],
    *,
    model_fingerprint_sha256: str,
    prompt_sha256: str,
    max_tokens: int,
) -> str:
    return contract.sha256_text(
        stable_json(
            {
                "candidate_id": str(row.get("candidate_id") or ""),
                "runtime_visual_file_sha256": str(
                    row.get("runtime_visual_file_sha256") or ""
                ),
                "model_fingerprint_sha256": model_fingerprint_sha256,
                "prompt_sha256": prompt_sha256,
                "output_contract_version": output_contract.CONTRACT_VERSION,
                "max_tokens": max_tokens,
                "backend_version": BACKEND_VERSION,
                "script_sha256": current_script_sha256(),
            }
        )
    )


def prepare_execution_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    pre: Mapping[str, Any],
    max_tokens: int,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["execution_key"] = execution_key(
            row,
            model_fingerprint_sha256=str(pre["model_fingerprint_sha256"]),
            prompt_sha256=str(pre["prompt_sha256"]),
            max_tokens=max_tokens,
        )
        prepared.append(row)
    keys = [str(row["execution_key"]) for row in prepared]
    if len(keys) != len(set(keys)):
        raise RuntimeError("dynamic_db_execution_key_duplicate")
    return prepared


def create_run_and_items(
    *,
    db: Path,
    rows: Sequence[Mapping[str, Any]],
    pre: Mapping[str, Any],
    prompt_path: Path,
    max_tokens: int,
    workers: int,
) -> str:
    run_id = "stop03_3f_dynamic_db_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    created_at = contract.now_iso()
    script_sha = current_script_sha256()
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
                    "dynamic_db_execution_key_already_exists:"
                    f"{existing['run_id']}:{existing['status']}"
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
                run_id,
                contract_lock.CONTRACT_NAME,
                pre["contract"]["candidate_id_set_sha256"],
                pre["contract"]["candidate_semantic_digest_sha256"],
                len(rows),
                Path(str(pre["model_path"])).name,
                pre["model_path"],
                pre["model_sha256"],
                pre["model_config_sha256"],
                pre["model_tokenizer_files_json"],
                pre["model_tokenizer_files_sha256"],
                pre["model_inventory_json"],
                pre["model_inventory_sha256"],
                pre["model_fingerprint_sha256"],
                str(prompt_path),
                pre["prompt_sha256"],
                pre["config_sha256"],
                output_contract.CONTRACT_VERSION,
                max_tokens,
                pre["temperature"],
                pre["top_p"],
                workers,
                script_sha,
                "running",
                len(rows),
                created_at,
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
                    run_item_id,
                    run_id,
                    row["candidate_id"],
                    row["execution_key"],
                    row["source_content_id"],
                    row["visual_unit_id"],
                    row["canonical_visual_unit_id"],
                    row["derived_id"],
                    row["candidate_role"],
                    row["reason_codes"],
                    row["policy_version"],
                    row["media_type"],
                    row["time_position_ms"],
                    row["runtime_visual_file"],
                    row["runtime_visual_file_sha256"],
                    "pending",
                    0,
                    created_at,
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return run_id


def prepare_resume(
    db: Path,
    run_id: str,
    *,
    pre: Mapping[str, Any],
    workers: int,
    max_tokens: int,
    confirm_compatible_script_resume: bool = False,
) -> dict[str, Any]:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            "SELECT * FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"dynamic_db_resume_run_missing:{run_id}")
        previous_script_sha = str(run["script_sha256"])
        current_script_sha = current_script_sha256()
        if previous_script_sha != current_script_sha:
            compatible = previous_script_sha in COMPATIBLE_RESUME_SCRIPT_SHAS
            if not compatible or not confirm_compatible_script_resume:
                raise RuntimeError(
                    "dynamic_db_resume_script_sha256_mismatch:"
                    f"{previous_script_sha}:{current_script_sha}:"
                    "explicit_compatible_resume_confirmation_required"
                )
        checks = {
            "model_fingerprint_sha256": str(pre["model_fingerprint_sha256"]),
            "prompt_sha256": str(pre["prompt_sha256"]),
            "output_contract_version": output_contract.CONTRACT_VERSION,
        }
        for field, expected in checks.items():
            if str(run[field]) != expected:
                raise RuntimeError(f"dynamic_db_resume_{field}_mismatch")
        if int(run["max_tokens"]) != max_tokens:
            raise RuntimeError("dynamic_db_resume_max_tokens_mismatch")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status='pending',started_at=NULL,finished_at=NULL
            WHERE run_id=? AND status='running'""",
            (run_id,),
        )
        con.execute(
            """UPDATE stop03_3_qwenvl_runs
            SET status='running',workers=?,script_sha256=?,finished_at=NULL,error_message=''
            WHERE run_id=?""",
            (workers, current_script_sha, run_id),
        )
        count = int(
            con.execute(
                "SELECT COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        if count <= 0:
            raise RuntimeError("dynamic_db_resume_run_empty")
        con.commit()
        return {
            "queue_count": count,
            "previous_script_sha256": previous_script_sha,
            "current_script_sha256": current_script_sha,
            "compatible_script_upgrade": previous_script_sha != current_script_sha,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_next_item(
    db: Path,
    run_id: str,
    *,
    max_attempts: int,
) -> Optional[dict[str, Any]]:
    """Atomically claim one item; the transaction ends before inference starts."""
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in RETRYABLE_STATUSES)
        row = con.execute(
            f"""SELECT i.*
            FROM stop03_3_qwenvl_run_items i
            WHERE i.run_id=?
              AND (
                    i.status='pending'
                    OR (i.status IN ({placeholders}) AND i.attempt_count < ?)
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM stop03_3_qwenvl_results r
                    WHERE r.run_item_id=i.run_item_id AND r.result_status='success'
                  )
            ORDER BY CASE WHEN i.status='pending' THEN 0 ELSE 1 END,
                     i.attempt_count,i.candidate_id
            LIMIT 1""",
            (run_id, *RETRYABLE_STATUSES, max_attempts),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        cursor = con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status='running',attempt_count=attempt_count+1,
                started_at=?,finished_at=NULL
            WHERE run_item_id=? AND status=? AND attempt_count=?""",
            (
                contract.now_iso(),
                row["run_item_id"],
                row["status"],
                row["attempt_count"],
            ),
        )
        if cursor.rowcount != 1:
            con.rollback()
            return None
        claimed = dict(row)
        claimed["attempt_count"] = int(row["attempt_count"]) + 1
        claimed["status_before_claim"] = str(row["status"])
        con.commit()
        return claimed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def classify_result(outcome: batch75.DiagnosticOutcome) -> str:
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
    *,
    out: Path,
    row: Mapping[str, Any],
    outcome: batch75.DiagnosticOutcome,
    worker_id: int,
    worker_sequence: int,
    elapsed: float,
    prompt_strategy: str,
    effective_prompt_sha256: str,
) -> dict[str, Any]:
    candidate_id = str(row["candidate_id"])
    attempt = int(row["attempt_count"])
    relative_base = Path(candidate_id) / f"attempt_{attempt:02d}"
    raw_path = out / "raw_outputs" / relative_base.with_suffix(".json")
    clean_path = out / "clean_outputs" / relative_base.with_suffix(".txt")
    stderr_path = out / "stderr" / relative_base.with_suffix(".txt")
    metrics_path = out / "metrics" / relative_base.with_suffix(".json")
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
        "worker_sequence": worker_sequence,
        "attempt_count": attempt,
        "status_before_claim": row["status_before_claim"],
        "degenerate_reason": outcome.degenerate_reason,
        "prompt_strategy": prompt_strategy,
        "effective_prompt_sha256": effective_prompt_sha256,
    }
    write_json_atomic(
        raw_path,
        {
            "candidate_id": candidate_id,
            "backend_version": BACKEND_VERSION,
            "text": outcome.clean_text,
            "metrics": metrics,
        },
    )
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
    *,
    db: Path,
    run_id: str,
    row: Mapping[str, Any],
    outcome: batch75.DiagnosticOutcome,
    artifacts: Mapping[str, Any],
    pre: Mapping[str, Any],
) -> str:
    status = classify_result(outcome)
    raw_path = Path(artifacts["raw_path"])
    stderr_path = Path(artifacts["stderr_path"])
    metrics_path = Path(artifacts["metrics_path"])
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status=?,last_error_code=?,last_error_message=?,finished_at=?
            WHERE run_item_id=?""",
            (
                status,
                "" if status == "success" else status,
                ""
                if status == "success"
                else str(outcome.degenerate_reason or outcome.cleanup_warnings),
                contract.now_iso(),
                row["run_item_id"],
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
                result_id,
                run_id,
                row["run_item_id"],
                row["candidate_id"],
                row["execution_key"],
                evidence_id,
                row["source_content_id"],
                row["visual_unit_id"],
                row["canonical_visual_unit_id"],
                row["derived_id"],
                row["candidate_role"],
                row["reason_codes"],
                row["policy_version"],
                status,
                outcome.clean_text,
                outcome.clean_text[:500],
                batch75.sha256_text(outcome.clean_text),
                str(raw_path),
                contract.sha256_file(raw_path),
                str(stderr_path),
                contract.sha256_file(stderr_path),
                str(metrics_path),
                contract.sha256_file(metrics_path),
                stable_json(artifacts["metrics"]),
                outcome.prompt_tokens,
                outcome.generation_tokens,
                outcome.peak_memory_gb,
                outcome.inferred_finish_reason,
                "truncated"
                if outcome.truncation_status == "truncated"
                else "complete",
                outcome.cleanup_status,
                outcome.cleanup_warnings,
                output_contract.CONTRACT_VERSION,
                row["runtime_visual_file_sha256"],
                pre["model_sha256"],
                pre["model_config_sha256"],
                pre["model_tokenizer_files_json"],
                pre["model_tokenizer_files_sha256"],
                pre["model_inventory_sha256"],
                pre["model_fingerprint_sha256"],
                artifacts["metrics"]["effective_prompt_sha256"],
                pre["config_sha256"],
                current_script_sha256(),
                contract.now_iso(),
            ),
        )
        con.commit()
        return status
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_item_failed(
    db: Path,
    row: Mapping[str, Any],
    exc: BaseException,
) -> None:
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status='failed',last_error_code=?,last_error_message=?,finished_at=?
            WHERE run_item_id=?""",
            (type(exc).__name__, str(exc), contract.now_iso(), row["run_item_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def write_worker_status(
    out: Path,
    worker_id: int,
    value: Mapping[str, Any],
) -> None:
    write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", value)


def execute_dynamic_worker(
    *,
    worker_id: int,
    db: Path,
    out: Path,
    run_id: str,
    prompt: str,
    model_path: Path,
    max_tokens: int,
    max_attempts: int,
    pre: Mapping[str, Any],
    stop_event: Any,
    report_queue: Any,
    adapter: Optional[batch75.PersistentCorrectedBatchAdapter] = None,
) -> None:
    adapter = adapter or batch75.PersistentCorrectedBatchAdapter(
        model_path=model_path,
        max_tokens=max_tokens,
        backend=batch75.LocalCorrectedBatchBackend(),
    )
    completed_attempts = 0
    successful_attempts = 0
    non_successful_attempts = 0
    elapsed_total = 0.0
    lifecycle = "loading"
    error_message = ""
    state: dict[str, Any] = {
        "worker_id": worker_id,
        "pid": os.getpid(),
        "lifecycle": lifecycle,
        "model_load_count": 0,
        "completed_attempts": 0,
        "successful_attempts": 0,
        "non_successful_attempts": 0,
        "current_candidate_id": None,
        "current_attempt": None,
        "average_seconds": None,
        "last_completed": None,
    }
    write_worker_status(out, worker_id, state)
    try:
        adapter.load_once()
        lifecycle = "running"
        state.update(
            {"lifecycle": lifecycle, "model_load_count": adapter.model_load_count}
        )
        write_worker_status(out, worker_id, state)
        while not stop_event.is_set():
            row = claim_next_item(db, run_id, max_attempts=max_attempts)
            if row is None:
                break
            worker_sequence = completed_attempts + 1
            state.update(
                {
                    "current_candidate_id": row["candidate_id"],
                    "current_attempt": row["attempt_count"],
                    "current_started_at": contract.now_iso(),
                }
            )
            write_worker_status(out, worker_id, state)
            started = time.monotonic()
            try:
                item_prompt, prompt_strategy = effective_prompt(
                    prompt,
                    int(row["attempt_count"]),
                )
                outcome = adapter.generate_one(
                    candidate_id=str(row["candidate_id"]),
                    image_path=str(row["runtime_visual_file"]),
                    prompt=item_prompt,
                )
                elapsed = time.monotonic() - started
                artifacts = write_item_artifacts(
                    out=out,
                    row=row,
                    outcome=outcome,
                    worker_id=worker_id,
                    worker_sequence=worker_sequence,
                    elapsed=elapsed,
                    prompt_strategy=prompt_strategy,
                    effective_prompt_sha256=contract.sha256_text(item_prompt),
                )
                item_status = persist_result(
                    db=db,
                    run_id=run_id,
                    row=row,
                    outcome=outcome,
                    artifacts=artifacts,
                    pre=pre,
                )
                event = {
                    "timestamp": contract.now_iso(),
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "worker_sequence": worker_sequence,
                    "candidate_id": row["candidate_id"],
                    "attempt_count": row["attempt_count"],
                    "status": item_status,
                    "generation_tokens": outcome.generation_tokens,
                    "raw_finish_reason": outcome.raw_finish_reason,
                    "inferred_finish_reason": outcome.inferred_finish_reason,
                    "degenerate_reason": outcome.degenerate_reason,
                    "elapsed_seconds": elapsed,
                    "prompt_strategy": prompt_strategy,
                }
            except Exception as exc:
                elapsed = time.monotonic() - started
                mark_item_failed(db, row, exc)
                item_status = "failed"
                event = {
                    "timestamp": contract.now_iso(),
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "worker_sequence": worker_sequence,
                    "candidate_id": row["candidate_id"],
                    "attempt_count": row["attempt_count"],
                    "status": item_status,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "elapsed_seconds": elapsed,
                }
            completed_attempts += 1
            elapsed_total += elapsed
            if item_status == "success":
                successful_attempts += 1
            else:
                non_successful_attempts += 1
            append_jsonl(
                out / "logs" / f"progress_worker_{worker_id}.jsonl",
                event,
            )
            state.update(
                {
                    "completed_attempts": completed_attempts,
                    "successful_attempts": successful_attempts,
                    "non_successful_attempts": non_successful_attempts,
                    "current_candidate_id": None,
                    "current_attempt": None,
                    "average_seconds": elapsed_total / completed_attempts,
                    "last_completed": event,
                }
            )
            try:
                snapshot = adapter.snapshot()
                state["mlx_memory_bytes"] = snapshot.get("mlx_memory_bytes", {})
            except Exception:
                pass
            write_worker_status(out, worker_id, state)
            print(
                f"[PROGRESS] run_id={run_id} worker={worker_id} "
                f"worker_sequence={worker_sequence} candidate_id={row['candidate_id']} "
                f"attempt={row['attempt_count']}/{max_attempts} status={item_status} "
                f"elapsed_seconds={elapsed:.3f}",
                flush=True,
            )
        lifecycle = "completed" if not stop_event.is_set() else "stopped"
    except Exception as exc:
        lifecycle = "failed"
        error_message = f"{type(exc).__name__}:{exc}"
        state["traceback"] = traceback.format_exc()
        stop_event.set()
    state.update(
        {
            "lifecycle": lifecycle,
            "current_candidate_id": None,
            "current_attempt": None,
            "finished_at": contract.now_iso(),
            "error_message": error_message,
        }
    )
    write_worker_status(out, worker_id, state)
    report_queue.put(state)


def _worker_entry(
    worker_id: int,
    db: str,
    out: str,
    run_id: str,
    prompt: str,
    model_path: str,
    max_tokens: int,
    max_attempts: int,
    pre: Mapping[str, Any],
    stop_event: Any,
    report_queue: Any,
) -> None:
    execute_dynamic_worker(
        worker_id=worker_id,
        db=Path(db),
        out=Path(out),
        run_id=run_id,
        prompt=prompt,
        model_path=Path(model_path),
        max_tokens=max_tokens,
        max_attempts=max_attempts,
        pre=pre,
        stop_event=stop_event,
        report_queue=report_queue,
    )


def reset_interrupted_items(db: Path, run_id: str) -> None:
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status='pending',started_at=NULL,finished_at=NULL
            WHERE run_id=? AND status='running'""",
            (run_id,),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def run_counts(db: Path, run_id: str, *, max_attempts: int) -> dict[str, int]:
    con = contract.readonly_connection(db)
    try:
        counts = Counter(
            {
                str(row[0]): int(row[1])
                for row in con.execute(
                    """SELECT status,COUNT(*) FROM stop03_3_qwenvl_run_items
                    WHERE run_id=? GROUP BY status""",
                    (run_id,),
                )
            }
        )
        total = int(
            con.execute(
                "SELECT COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        result_count = int(
            con.execute(
                "SELECT COUNT(*) FROM stop03_3_qwenvl_results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        retried = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_3_qwenvl_run_items
                WHERE run_id=? AND attempt_count>1""",
                (run_id,),
            ).fetchone()[0]
        )
        terminal_non_success = int(
            con.execute(
                f"""SELECT COUNT(*) FROM stop03_3_qwenvl_run_items
                WHERE run_id=? AND status IN ({','.join('?' for _ in RETRYABLE_STATUSES)})
                  AND attempt_count>=?""",
                (run_id, *RETRYABLE_STATUSES, max_attempts),
            ).fetchone()[0]
        )
        max_attempt = int(
            con.execute(
                """SELECT COALESCE(MAX(attempt_count),0)
                FROM stop03_3_qwenvl_run_items WHERE run_id=?""",
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        con.close()
    return {
        **dict(counts),
        "total": total,
        "results": result_count,
        "retried_items": retried,
        "terminal_non_success": terminal_non_success,
        "max_attempt_count": max_attempt,
    }


def finalize_run(
    db: Path,
    run_id: str,
    *,
    max_attempts: int,
    interrupted: bool,
) -> dict[str, int]:
    counts = run_counts(db, run_id, max_attempts=max_attempts)
    success = counts.get("success", 0)
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    review = counts.get("review", 0)
    failed = sum(
        counts.get(name, 0)
        for name in (
            "failed",
            "truncated",
            "parse_failed",
            "missing_required_fields",
            "input_fingerprint_mismatch",
        )
    )
    if interrupted:
        run_status = "cancelled"
    elif counts["total"] > 0 and success == counts["total"]:
        run_status = "success"
    elif success or review or failed:
        run_status = "partial"
    else:
        run_status = "failed"
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_runs
            SET status=?,pending_count=?,success_count=?,failed_count=?,
                review_count=?,finished_at=?,error_message=?
            WHERE run_id=?""",
            (
                run_status,
                pending,
                success,
                failed,
                review,
                contract.now_iso(),
                ""
                if run_status == "success"
                else f"running={running};terminal_non_success={counts['terminal_non_success']}",
                run_id,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return counts


def database_checks(db: Path) -> dict[str, Any]:
    con = contract.readonly_connection(db)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    finally:
        con.close()
    return {
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
    }


def run_workers(
    *,
    db: Path,
    out: Path,
    run_id: str,
    prompt: str,
    model_path: Path,
    max_tokens: int,
    max_attempts: int,
    workers: int,
    pre: Mapping[str, Any],
) -> dict[str, Any]:
    context = mp.get_context("spawn")
    stop_event = context.Event()
    report_queue = context.Queue()
    processes = [
        context.Process(
            target=_worker_entry,
            args=(
                worker_id,
                str(db),
                str(out),
                run_id,
                prompt,
                str(model_path),
                max_tokens,
                max_attempts,
                dict(pre),
                stop_event,
                report_queue,
            ),
            name=f"stop03f-dynamic-db-worker-{worker_id}",
        )
        for worker_id in range(1, workers + 1)
    ]
    interrupted = False
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_stop)
    try:
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join()
        except KeyboardInterrupt:
            interrupted = True
            stop_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=10)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    reports: list[dict[str, Any]] = []
    for _ in processes:
        try:
            reports.append(dict(report_queue.get(timeout=1.0)))
        except queue.Empty:
            break
    reset_interrupted_items(db, run_id)
    counts = finalize_run(
        db,
        run_id,
        max_attempts=max_attempts,
        interrupted=interrupted,
    )
    process_exit_codes = {process.name: process.exitcode for process in processes}
    expected_count = counts["total"]
    readback = contract.readback_run(db, run_id, expected_count=expected_count)
    db_checks = database_checks(db)
    worker_reports = sorted(reports, key=lambda item: int(item["worker_id"]))
    workers_healthy = (
        len(worker_reports) == workers
        and all(item["lifecycle"] == "completed" for item in worker_reports)
        and all(int(item["model_load_count"]) == 1 for item in worker_reports)
        and all(code == 0 for code in process_exit_codes.values())
    )
    passed = (
        not interrupted
        and expected_count > 0
        and counts.get("success", 0) == expected_count
        and counts.get("terminal_non_success", 0) == 0
        and workers_healthy
        and readback["status"] == "PASS"
        and db_checks["integrity_check"] == "ok"
        and not db_checks["foreign_key_check"]
    )
    return {
        "status": "DYNAMIC_DB_FULL_PASS" if passed else "DYNAMIC_DB_PARTIAL_OR_FAILED",
        "run_id": run_id,
        "queue_count": expected_count,
        "workers_requested": workers,
        "scheduling_mode": "dynamic_database_claim",
        "max_attempts": max_attempts,
        "counts": counts,
        "worker_reports": worker_reports,
        "process_exit_codes": process_exit_codes,
        "workers_healthy": workers_healthy,
        "interrupted": interrupted,
        "readback": readback,
        "database_checks": db_checks,
    }


def build_preflight(
    *,
    db: Path,
    out: Path,
    config_path: Path,
    prompt_path: Path,
    model_path: Path,
    qwen_python: Path,
    max_tokens: int,
    mode: str,
    workers: int,
    max_attempts: int,
) -> dict[str, Any]:
    pre = dict(
        contract.preflight(
            db=db,
            out=out,
            config_path=config_path,
            model_path=model_path,
            qwen_python=qwen_python,
            prompt_path=prompt_path,
            max_tokens=max_tokens,
            mode="run" if mode in {"run", "resume"} else mode,
            allow_low_token_debug=False,
            allow_simulation=False,
        )
    )
    queue_count = len(load_frozen_queue(db))
    queue_audit = dict(pre["queue_audit"])
    queue_audit_checks = dict(queue_audit["checks"])
    queue_audit_checks.pop("row_count_336", None)
    queue_audit_checks["row_count_matches_frozen_contract"] = (
        queue_count == int(pre["contract"]["qwenvl_count"])
    )
    queue_audit["checks"] = queue_audit_checks
    queue_audit["status"] = "PASS" if all(queue_audit_checks.values()) else "FAIL"
    preflight_checks = dict(pre["checks"])
    preflight_checks["queue_audit_pass"] = queue_audit["status"] == "PASS"
    pre["queue_audit"] = queue_audit
    pre["checks"] = preflight_checks
    pre["status"] = "PASS" if all(preflight_checks.values()) else "FAIL"
    pre["technical_status"] = pre["status"]
    pre["policy_status"] = pre["status"]
    pre.update(
        {
            "script_version": SCRIPT_VERSION,
            "backend_version": BACKEND_VERSION,
            "script_sha256": current_script_sha256(),
            "queue_count": queue_count,
            "workers_requested": workers,
            "scheduling_mode": "dynamic_database_claim",
            "max_attempts": max_attempts,
            "fixed_item_count_required": False,
            "compact_retry_prompt_version": COMPACT_RETRY_PROMPT_VERSION,
        }
    )
    return pre


def dry_run(
    *,
    db: Path,
    out: Path,
    pre: Mapping[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    rows = prepare_execution_rows(
        load_frozen_queue(db),
        pre=pre,
        max_tokens=max_tokens,
    )
    out.mkdir(parents=True, exist_ok=False)
    plan_path = out / "manifests/dynamic_db_execution_plan.jsonl"
    for row in rows:
        append_jsonl(plan_path, row)
    result = {
        **dict(pre),
        "mode": "dry-run",
        "status": "PASS",
        "execution_plan_count": len(rows),
        "execution_key_unique_count": len({row["execution_key"] for row in rows}),
        "central_db_modified": False,
        "model_run": False,
        "plan_path": str(plan_path),
    }
    write_json_atomic(out / "reports/dry_run_summary.json", result)
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stop03-3F corrected batch Qwen-VL dynamic DB orchestrator"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("preflight", "dry-run", "run", "resume"),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--qwen-python", default=str(DEFAULT_QWEN_PYTHON))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--confirm-central-db-write", action="store_true")
    parser.add_argument("--confirm-compatible-script-resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.workers < 1:
        raise RuntimeError("dynamic_db_workers_must_be_positive")
    if args.max_attempts < 1:
        raise RuntimeError("dynamic_db_max_attempts_must_be_positive")
    if args.mode in {"run", "resume"} and not args.confirm_central_db_write:
        raise RuntimeError("dynamic_db_central_write_requires_explicit_confirmation")
    db = Path(args.db).resolve(strict=True)
    config_path = Path(args.config).resolve(strict=True)
    prompt_path = Path(args.prompt).resolve(strict=True)
    model_path = Path(args.model).resolve(strict=True)
    qwen_python = Path(args.qwen_python).resolve(strict=True)
    out = batch75.assert_test_output_path(Path(args.out))
    pre = build_preflight(
        db=db,
        out=out,
        config_path=config_path,
        prompt_path=prompt_path,
        model_path=model_path,
        qwen_python=qwen_python,
        max_tokens=args.max_tokens,
        mode=args.mode,
        workers=args.workers,
        max_attempts=args.max_attempts,
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
                load_frozen_queue(db),
                pre=pre,
                max_tokens=args.max_tokens,
            )
            run_id = create_run_and_items(
                db=db,
                rows=prepared,
                pre=pre,
                prompt_path=prompt_path,
                max_tokens=args.max_tokens,
                workers=args.workers,
            )
        else:
            if not args.run_id:
                raise RuntimeError("dynamic_db_resume_requires_run_id")
            if not out.is_dir():
                raise RuntimeError(f"dynamic_db_resume_out_missing:{out}")
            run_id = args.run_id
            resume_metadata = prepare_resume(
                db,
                run_id,
                pre=pre,
                workers=args.workers,
                max_tokens=args.max_tokens,
                confirm_compatible_script_resume=args.confirm_compatible_script_resume,
            )
            write_json_atomic(
                out / "reports/compatible_resume_metadata.json",
                resume_metadata,
            )
        write_text_atomic(out / "run_id.txt", run_id + "\n")
        write_json_atomic(
            out / "reports/run_settings.json",
            {
                "run_id": run_id,
                "workers": args.workers,
                "max_tokens": args.max_tokens,
                "max_attempts": args.max_attempts,
                "scheduling_mode": "dynamic_database_claim",
                "queue_count": pre["queue_count"],
            },
        )
        result = run_workers(
            db=db,
            out=out,
            run_id=run_id,
            prompt=prompt,
            model_path=model_path,
            max_tokens=args.max_tokens,
            max_attempts=args.max_attempts,
            workers=args.workers,
            pre=pre,
        )
        result.update(
            {
                "backend_version": BACKEND_VERSION,
                "central_db_modified": True,
                "network_used": False,
                "download_used": False,
                "original_video_read": False,
            }
        )
        write_json_atomic(out / "reports/final_report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("status") in {"PASS", "DYNAMIC_DB_FULL_PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
