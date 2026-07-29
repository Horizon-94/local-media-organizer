#!/usr/bin/env python3
"""Dynamic, append-only Qwen-VL supplement runner for missing image descriptions."""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


CONTRACT_VERSION = "stop03_3_qwenvl_supplement_v1"
SCRIPT_VERSION = "stop03_3_qwenvl_supplement_orchestrator_v1"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def execution_key(row: Mapping[str, Any], model_fingerprint: str, prompt_sha: str, max_tokens: int) -> str:
    return sha256_text(stable_json({
        "candidate_id": row["candidate_id"],
        "runtime_visual_file_sha256": row["runtime_visual_file_sha256"],
        "model_fingerprint_sha256": model_fingerprint,
        "prompt_sha256": prompt_sha,
        "max_tokens": max_tokens,
        "contract_version": CONTRACT_VERSION,
        "script_version": SCRIPT_VERSION,
    }))


def prepare_run(
    db: Path, *, workers: int, max_tokens: int,
    model_fingerprint: str, prompt_sha: str, max_attempts: int = 3,
) -> tuple[str | None, int]:
    con = connect(db)
    try:
        rows = [dict(row) for row in con.execute(
            """SELECT c.* FROM stop03_3_qwenvl_supplement_candidates c
               WHERE NOT EXISTS(
                   SELECT 1 FROM stop03_3_qwenvl_supplement_results r
                   WHERE r.candidate_id=c.candidate_id AND r.result_status='success'
               ) ORDER BY c.candidate_id"""
        )]
        if not rows:
            return None, 0
        keys = [execution_key(row, model_fingerprint, prompt_sha, max_tokens) for row in rows]
        if len(keys) != len(set(keys)):
            raise RuntimeError("supplement_execution_key_duplicate")
        key_rows = con.execute(
            "SELECT execution_key,run_id FROM stop03_3_qwenvl_supplement_items "
            "WHERE execution_key IN (%s)" % ",".join("?" for _ in keys),
            keys,
        ).fetchall()
        owner_run_ids = {str(row["run_id"]) for row in key_rows}
        if len(owner_run_ids) > 1:
            raise RuntimeError("supplement_execution_keys_span_multiple_runs")
        # Compatibility with runs created by earlier app versions: an existing
        # execution key belongs to one run globally, so resume that owning run
        # instead of trying to insert a duplicate item into a new run.
        run_id = (
            next(iter(owner_run_ids)) if owner_run_ids
            else "stop03_3_supplement_" + sha256_text("\n".join(keys))[:24]
        )
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT status FROM stop03_3_qwenvl_supplement_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE stop03_3_qwenvl_supplement_items SET status='pending',started_at=NULL "
                "WHERE run_id=? AND status IN ('running','failed','review') AND attempt_count<?",
                (run_id, max_attempts),
            )
            existing_keys = {str(row["execution_key"]) for row in key_rows}
            for row, key in zip(rows, keys):
                if key in existing_keys:
                    continue
                con.execute(
                    """INSERT INTO stop03_3_qwenvl_supplement_items
                    (run_id,candidate_id,execution_key,status,created_at)
                    VALUES(?,?,?,'pending',?)""",
                    (run_id, row["candidate_id"], key, utc_now()),
                )
            item_count = int(con.execute(
                "SELECT COUNT(*) FROM stop03_3_qwenvl_supplement_items WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])
            con.execute(
                "UPDATE stop03_3_qwenvl_supplement_runs SET status='running',workers=?,candidate_count=?,finished_at=NULL,error_message='' WHERE run_id=?",
                (workers, item_count, run_id),
            )
        else:
            con.execute(
                """INSERT INTO stop03_3_qwenvl_supplement_runs
                (run_id,contract_version,candidate_count,workers,max_tokens,
                 model_fingerprint_sha256,prompt_sha256,status,created_at)
                VALUES(?,?,?,?,?,?,?,'running',?)""",
                (run_id, CONTRACT_VERSION, len(rows), workers, max_tokens, model_fingerprint, prompt_sha, utc_now()),
            )
            for row, key in zip(rows, keys):
                con.execute(
                    """INSERT INTO stop03_3_qwenvl_supplement_items
                    (run_id,candidate_id,execution_key,status,created_at)
                    VALUES(?,?,?,'pending',?)""",
                    (run_id, row["candidate_id"], key, utc_now()),
                )
        con.commit()
        actionable_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_3_qwenvl_supplement_items "
            "WHERE run_id=? AND status IN ('pending','failed','review') AND attempt_count<?",
            (run_id, max_attempts),
        ).fetchone()[0])
        return run_id, actionable_count
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_one(db: Path, run_id: str, max_attempts: int) -> dict[str, Any] | None:
    con = connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT i.*,c.runtime_visual_file,c.runtime_visual_file_sha256
               FROM stop03_3_qwenvl_supplement_items i
               JOIN stop03_3_qwenvl_supplement_candidates c USING(candidate_id)
               WHERE i.run_id=? AND i.status IN ('pending','failed','review')
                 AND i.attempt_count<?
                 AND NOT EXISTS(
                     SELECT 1 FROM stop03_3_qwenvl_supplement_results r
                     WHERE r.execution_key=i.execution_key AND r.result_status='success'
                 )
               ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END,
                        i.attempt_count,i.candidate_id LIMIT 1""",
            (run_id, max_attempts),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        updated = con.execute(
            """UPDATE stop03_3_qwenvl_supplement_items
               SET status='running',attempt_count=attempt_count+1,started_at=?,finished_at=NULL
               WHERE run_id=? AND candidate_id=? AND status=? AND attempt_count=?""",
            (utc_now(), run_id, row["candidate_id"], row["status"], row["attempt_count"]),
        )
        if updated.rowcount != 1:
            con.rollback()
            return None
        con.commit()
        item = dict(row)
        item["attempt_count"] = int(row["attempt_count"]) + 1
        return item
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def store_outcome(db: Path, run_id: str, item: Mapping[str, Any], outcome: Mapping[str, Any], elapsed: float) -> None:
    raw_status = str(outcome.get("result_status") or "failed")
    status = "success" if raw_status == "success" else "review" if raw_status not in {"failed"} else "failed"
    clean_text = str(outcome.get("clean_text") or "")
    key = str(item["execution_key"])
    con = connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        if status in {"success", "review"}:
            con.execute(
                """INSERT INTO stop03_3_qwenvl_supplement_results
                (result_id,run_id,candidate_id,execution_key,evidence_id,result_status,
                 clean_text,clean_text_sha256,generation_tokens,finish_reason,
                 runtime_visual_file_sha256,output_contract_version,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(execution_key) DO UPDATE SET
                    result_status=excluded.result_status,clean_text=excluded.clean_text,
                    clean_text_sha256=excluded.clean_text_sha256,
                    generation_tokens=excluded.generation_tokens,finish_reason=excluded.finish_reason,
                    created_at=excluded.created_at""",
                (
                    "qres_sup_" + key[:32], run_id, item["candidate_id"], key,
                    "qev_sup_" + key[:32], status, clean_text, sha256_text(clean_text),
                    outcome.get("generation_tokens"), str(outcome.get("finish_reason") or ""),
                    item["runtime_visual_file_sha256"], "qwenvl_output_contract_v2.0", utc_now(),
                ),
            )
        con.execute(
            """UPDATE stop03_3_qwenvl_supplement_items
               SET status=?,elapsed_seconds=?,finished_at=?
               WHERE run_id=? AND candidate_id=?""",
            (status, elapsed, utc_now(), run_id, item["candidate_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def store_failure(db: Path, run_id: str, item: Mapping[str, Any], elapsed: float) -> None:
    con = connect(db)
    try:
        con.execute(
            "UPDATE stop03_3_qwenvl_supplement_items SET status='failed',elapsed_seconds=?,finished_at=? "
            "WHERE run_id=? AND candidate_id=?",
            (elapsed, utc_now(), run_id, item["candidate_id"]),
        )
        con.commit()
    finally:
        con.close()


def finalize_run(db: Path, run_id: str, max_attempts: int) -> dict[str, Any]:
    con = connect(db)
    try:
        counts = {
            str(status): int(count)
            for status, count in con.execute(
                "SELECT status,COUNT(*) FROM stop03_3_qwenvl_supplement_items WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        }
        exhausted = int(con.execute(
            "SELECT COUNT(*) FROM stop03_3_qwenvl_supplement_items "
            "WHERE run_id=? AND status IN ('failed','review') AND attempt_count>=?",
            (run_id, max_attempts),
        ).fetchone()[0])
        queue_drained = not counts.get("pending") and not counts.get("running")
        status = "success" if queue_drained else "failed"
        terminal_issue_count = counts.get("review", 0) + counts.get("failed", 0)
        con.execute(
            """UPDATE stop03_3_qwenvl_supplement_runs SET status=?,success_count=?,review_count=?,
               failed_count=?,finished_at=?,error_message=? WHERE run_id=?""",
            (status, counts.get("success", 0), counts.get("review", 0), counts.get("failed", 0),
             utc_now(), (
                 f"terminal_review_or_failed_items={terminal_issue_count}"
                 if status == "success" and terminal_issue_count else
                 "" if status == "success" else f"exhausted_items={exhausted}"
             ), run_id),
        )
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        con.commit()
    finally:
        con.close()
    technical_pass = status == "success" and integrity == "ok" and foreign_keys == 0
    return {"status": "PASS" if technical_pass else "FAIL",
            "policy_status": "REVIEW" if technical_pass and terminal_issue_count else "PASS" if technical_pass else "FAIL",
            "run_id": run_id, "counts": counts, "exhausted_count": exhausted,
            "terminal_issue_count": terminal_issue_count,
            "database_integrity_check": integrity, "foreign_key_error_count": foreign_keys}


def worker_loop(
    db: Path, run_id: str, max_attempts: int,
    infer_one: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    while True:
        item = claim_one(db, run_id, max_attempts)
        if item is None:
            return
        started = time.monotonic()
        try:
            outcome = infer_one(item)
            store_outcome(db, run_id, item, outcome, time.monotonic() - started)
        except Exception:
            store_failure(db, run_id, item, time.monotonic() - started)


def run_fake_concurrent(
    db: Path, run_id: str, *, workers: int, max_attempts: int,
    infer_one: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker_loop, db, run_id, max_attempts, infer_one) for _ in range(workers)]
        for future in futures:
            future.result()
    return finalize_run(db, run_id, max_attempts)


def real_worker(db: Path, run_id: str, max_attempts: int, model: Path, prompt: str, max_tokens: int) -> None:
    from stop03_3f_qwenvl_batch75_diagnostic_v1 import PersistentCorrectedBatchAdapter
    adapter = PersistentCorrectedBatchAdapter(model_path=model, max_tokens=max_tokens)
    adapter.load_once()

    def infer(item: Mapping[str, Any]) -> Mapping[str, Any]:
        outcome = adapter.generate_one(
            candidate_id=str(item["candidate_id"]),
            image_path=str(item["runtime_visual_file"]), prompt=prompt,
        )
        return {
            "result_status": outcome.result_status,
            "clean_text": outcome.clean_text,
            "generation_tokens": outcome.generation_tokens,
            "finish_reason": outcome.inferred_finish_reason,
        }
    worker_loop(db, run_id, max_attempts, infer)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("run",), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=384)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_central_db_write:
        raise RuntimeError("supplement_run_requires_confirmation")
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
    script_dir = Path(__file__).resolve().parents[1] / "03_stop03_visual_analysis"
    sys.path.insert(0, str(script_dir))
    import stop03_3c_qwenvl_db_orchestrator_v1 as base
    config = base.load_config(args.config.resolve(strict=True))
    fingerprint = base.model_fingerprint(args.model.resolve(strict=True), config)
    prompt = args.prompt.resolve(strict=True).read_text(encoding="utf-8").strip()
    prompt_sha = sha256_text(prompt)
    db = args.db.resolve(strict=True)
    out = args.out.resolve()
    # The stage output is deterministic and must be reusable by "resume".
    # Existing files are retained; only summary.json is refreshed at the end.
    out.mkdir(parents=True, exist_ok=True)
    run_id, count = prepare_run(
        db, workers=args.workers, max_tokens=args.max_tokens,
        model_fingerprint=str(fingerprint["model_fingerprint_sha256"]), prompt_sha=prompt_sha,
        max_attempts=args.max_attempts,
    )
    if run_id is None:
        report = {"status": "PASS", "candidate_count": 0, "model_run": False, "existing_success_reexecuted": 0}
    elif count == 0:
        report = finalize_run(db, run_id, args.max_attempts)
        report.update({"candidate_count": 0, "workers": args.workers, "model_run": False, "existing_success_reexecuted": 0})
    else:
        context = mp.get_context("spawn")
        processes = [context.Process(
            target=real_worker,
            args=(db, run_id, args.max_attempts, args.model.resolve(strict=True), prompt, args.max_tokens),
        ) for _ in range(args.workers)]
        for process in processes: process.start()
        for process in processes: process.join()
        if any(process.exitcode != 0 for process in processes):
            raise RuntimeError("supplement_worker_failed:" + ",".join(str(process.exitcode) for process in processes))
        report = finalize_run(db, run_id, args.max_attempts)
        report.update({"candidate_count": count, "workers": args.workers, "model_run": True, "existing_success_reexecuted": 0})
    report.update({"network_used": False, "original_media_write": False})
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
