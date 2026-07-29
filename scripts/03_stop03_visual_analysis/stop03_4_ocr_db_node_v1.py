#!/usr/bin/env python3
"""Frozen Stop03-4 OCR central database node."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import stop03_4_ocr_db_orchestrator_v1 as orchestrator


NODE_VERSION = "stop03_4_ocr_db_node_v1_frozen_20260716"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
FROZEN_FILES = {
    PROJECT_ROOT
    / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py":
        "27b616f0738b4cf74b7bc69ce936fa11f245c734381c736e22c23ebdd90592ec",
    PROJECT_ROOT / "scripts/stop03_monitor/stop03_4_ocr_db_monitor.py":
        "b04666992ac1ba4369f9bafe6a8a7c4b99dd92eb1b85b1720b594292f2593bd8",
    PROJECT_ROOT / "configs/stop03_4_ocr_db_v1.json":
        "2e5142611b41b9bb4d7d5d0db4f829ae44dd1191b312a06d3737229e87a0e1c5",
    PROJECT_ROOT / "migrations/20260716_stop03_4_ocr_db_v1.sql":
        "1bafd4096da75fa8aeeecaac2e0bb4fe821e3df775af6375a2692c5277bc7844",
}
# The accepted full OCR run predates the retention-only compact JSON change.
# Model inputs, searchable OCR text and database evidence are unchanged; only
# embedded image tensors/font objects were removed from diagnostic JSON files.
FROZEN_ACCEPTANCE_RUN_SCRIPT_SHA256 = (
    "94bc2a96dac92a6aca7058e7cec24646d975be90a9c58b32c8cb31d85c0a4080"
)
FROZEN_CONTRACT_NAME = "stop03_2_v25_candidate_snapshot"
FROZEN_MODELS = {
    "detection_model_sha256":
        "efbea5fae8c00c180dd2ce21d3e27d2139c75f84bcd7cf70bfc4778dd91a63f4",
    "recognition_model_sha256":
        "96690ab688e0c480d84f21e9b01f7a47c830ea3c31da93b8f40404e84aea05d5",
    "model_fingerprint_sha256":
        "cb05388209680f5bb4e953cf33768d4015e1899f3b617d43312f91055370c217",
}
FROZEN_RUNTIME = {
    "scheduling_mode": "dynamic_database_claim",
    "workers": 3,
    "max_attempts": 3,
    "run_kind": "full",
    "limit": 0,
    "network_policy": "blocked_in_worker",
    "source_policy": "derived_visual_only",
}
def find_arg(argv: Sequence[str], name: str, default: str) -> str:
    try:
        index = list(argv).index(name)
    except ValueError:
        return default
    if index + 1 >= len(argv):
        raise RuntimeError(f"frozen_node_argument_value_missing:{name}")
    return str(argv[index + 1])


def verify_locked_args(argv: Sequence[str]) -> None:
    checks = {
        "--workers": str(FROZEN_RUNTIME["workers"]),
        "--max-attempts": str(FROZEN_RUNTIME["max_attempts"]),
        "--run-kind": str(FROZEN_RUNTIME["run_kind"]),
        "--limit": str(FROZEN_RUNTIME["limit"]),
    }
    for name, expected in checks.items():
        actual = find_arg(argv, name, expected)
        if actual != expected:
            raise RuntimeError(
                f"frozen_node_locked_argument_mismatch:{name}:{actual}:{expected}"
            )


def normalize_locked_args(argv: Sequence[str]) -> list[str]:
    normalized = list(argv)
    for name, value in (
        ("--workers", FROZEN_RUNTIME["workers"]),
        ("--max-attempts", FROZEN_RUNTIME["max_attempts"]),
        ("--run-kind", FROZEN_RUNTIME["run_kind"]),
        ("--limit", FROZEN_RUNTIME["limit"]),
    ):
        if name not in normalized:
            normalized.extend((name, str(value)))
    verify_locked_args(normalized)
    return normalized


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_current_frozen_contract(con: sqlite3.Connection) -> dict[str, Any]:
    row = con.execute(
        """SELECT contract_name,status,row_count,qwenvl_count,ocr_count,
                  candidate_id_set_sha256,candidate_semantic_digest_sha256
           FROM pipeline_frozen_contracts WHERE contract_name=?""",
        (FROZEN_CONTRACT_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("frozen_node_v25_contract_missing")
    contract = dict(row)
    candidates = list(
        con.execute(
            """SELECT candidate_id,queue_type,candidate_semantic_sha256
               FROM stop03_2_candidate_queue_frozen_v25
               ORDER BY candidate_id"""
        )
    )
    ids = [str(item["candidate_id"]) for item in candidates]
    actual = {
        "status": contract["status"],
        "row_count": len(candidates),
        "qwenvl_count": sum(
            item["queue_type"] == "qwenvl_high_value" for item in candidates
        ),
        "ocr_count": sum(item["queue_type"] == "ocr_trigger" for item in candidates),
        "candidate_id_set_sha256": sha256_text("\n".join(ids)),
        "candidate_semantic_digest_sha256": sha256_text(
            "\n".join(
                f"{item['candidate_id']}:{item['candidate_semantic_sha256']}"
                for item in candidates
            )
        ),
    }
    stored = {key: contract[key] for key in actual}
    if actual != stored or len(ids) != len(set(ids)):
        raise RuntimeError(
            "frozen_node_v25_contract_mismatch:"
            + json.dumps(
                {"actual": actual, "stored": stored},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return contract


def select_latest_complete_full_run(
    con: sqlite3.Connection, queue_count: int
) -> sqlite3.Row:
    row = con.execute(
        """SELECT run_id,run_kind,status,candidate_count,pending_count,
                  running_count,success_count,no_text_count,failed_count,
                  reused_count,workers,max_attempts,detection_model_sha256,
                  recognition_model_sha256,model_fingerprint_sha256,
                  config_sha256,script_sha256,started_at
           FROM stop03_4_ocr_runs
           WHERE run_kind='full' AND status='success'
             AND candidate_count=?
             AND pending_count=0 AND running_count=0 AND failed_count=0
             AND success_count + no_text_count = candidate_count
           ORDER BY started_at DESC, run_id DESC
           LIMIT 1""",
        (queue_count,),
    ).fetchone()
    if row is None:
        raise RuntimeError("frozen_node_complete_full_run_missing")
    return row


def verify_node(
    db: Path,
    *,
    frozen_files: Optional[Mapping[Path, str]] = None,
) -> dict[str, Any]:
    files = dict(frozen_files or FROZEN_FILES)
    actual_hashes: dict[str, str] = {}
    for path, expected in files.items():
        if not path.is_file():
            raise RuntimeError(f"frozen_node_file_missing:{path}")
        actual = orchestrator.sha256_file(path)
        actual_hashes[str(path)] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen_node_file_hash_mismatch:{path}:{actual}:{expected}"
            )

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        contract_row = verify_current_frozen_contract(con)
        queue_count = int(
            con.execute(
                "SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue"
            ).fetchone()[0]
        )
        run = select_latest_complete_full_run(con, queue_count)
        acceptance_run_id = str(run["run_id"])
        candidate_set_equal = int(
            con.execute(
                """SELECT COUNT(*) FROM (
                     SELECT candidate_id FROM v_stop03_2_v25_ocr_execution_queue
                     EXCEPT
                     SELECT candidate_id FROM stop03_4_ocr_run_items
                     WHERE run_id=?
                   )""",
                (acceptance_run_id,),
            ).fetchone()[0]
        ) == 0 and int(
            con.execute(
                """SELECT COUNT(*) FROM (
                     SELECT candidate_id FROM stop03_4_ocr_run_items
                     WHERE run_id=?
                     EXCEPT
                     SELECT candidate_id FROM v_stop03_2_v25_ocr_execution_queue
                   )""",
                (acceptance_run_id,),
            ).fetchone()[0]
        ) == 0
        result_count = int(
            con.execute(
                """SELECT COUNT(DISTINCT result_id)
                   FROM stop03_4_ocr_run_items WHERE run_id=?""",
                (acceptance_run_id,),
            ).fetchone()[0]
        )
        duplicate_keys = int(
            con.execute(
                """SELECT COUNT(*) FROM (
                     SELECT execution_key FROM stop03_4_ocr_results
                     GROUP BY execution_key HAVING COUNT(*)>1
                   )"""
            ).fetchone()[0]
        )
        empty_success = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_4_ocr_run_items i
                   JOIN stop03_4_ocr_results r ON r.result_id=i.result_id
                   WHERE i.run_id=? AND r.result_status='success'
                     AND (trim(r.ocr_text)='' OR r.ocr_line_count<1)""",
                (acceptance_run_id,),
            ).fetchone()[0]
        )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = len(list(con.execute("PRAGMA foreign_key_check")))
    finally:
        con.close()

    run_dict = dict(run)
    for name, expected in FROZEN_MODELS.items():
        if run_dict[name] != expected:
            raise RuntimeError(f"frozen_node_model_fingerprint_mismatch:{name}")
    expected_run = {
        "run_kind": "full",
        "status": "success",
        "pending_count": 0,
        "running_count": 0,
        "failed_count": 0,
        "workers": 3,
        "max_attempts": 3,
        "script_sha256": FROZEN_ACCEPTANCE_RUN_SCRIPT_SHA256,
        "config_sha256": FROZEN_FILES[
            PROJECT_ROOT / "configs/stop03_4_ocr_db_v1.json"
        ],
    }
    mismatches = {
        name: {"actual": run_dict[name], "expected": expected}
        for name, expected in expected_run.items()
        if run_dict[name] != expected
    }
    if run_dict["success_count"] + run_dict["no_text_count"] != run_dict["candidate_count"]:
        mismatches["terminal_success_count"] = {
            "actual": run_dict["success_count"] + run_dict["no_text_count"],
            "expected": run_dict["candidate_count"],
        }
    if result_count != run_dict["candidate_count"]:
        mismatches["result_count"] = {
            "actual": result_count,
            "expected": run_dict["candidate_count"],
        }
    if mismatches:
        raise RuntimeError(
            "frozen_node_acceptance_run_mismatch:"
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    if not candidate_set_equal:
        raise RuntimeError("frozen_node_candidate_set_mismatch")
    if duplicate_keys or empty_success:
        raise RuntimeError("frozen_node_result_contract_mismatch")
    if integrity != "ok" or foreign_key_errors:
        raise RuntimeError("frozen_node_database_integrity_failure")

    return {
        "status": "PASS",
        "node_version": NODE_VERSION,
        "acceptance_run_id": acceptance_run_id,
        "acceptance_candidate_count": run_dict["candidate_count"],
        "acceptance_success_count": run_dict["success_count"],
        "acceptance_no_text_count": run_dict["no_text_count"],
        "acceptance_reused_count": run_dict["reused_count"],
        "acceptance_inference_count":
            run_dict["candidate_count"] - run_dict["reused_count"],
        "result_count": result_count,
        "candidate_set_equal": candidate_set_equal,
        "execution_key_duplicates": duplicate_keys,
        "empty_success_count": empty_success,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_key_errors,
        "frozen_files": actual_hashes,
        "frozen_contract": contract_row,
        "frozen_models": dict(FROZEN_MODELS),
        "frozen_runtime": dict(FROZEN_RUNTIME),
        "network_used": False,
        "download_used": False,
        "model_run": False,
        "central_db_modified": False,
        "original_video_read": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    verify_only = "--verify-node-only" in arguments
    if verify_only:
        arguments.remove("--verify-node-only")
    arguments = normalize_locked_args(arguments)
    db = Path(find_arg(arguments, "--db", str(DEFAULT_DB))).expanduser().resolve(
        strict=True
    )
    report = verify_node(db)
    if verify_only:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return orchestrator.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
