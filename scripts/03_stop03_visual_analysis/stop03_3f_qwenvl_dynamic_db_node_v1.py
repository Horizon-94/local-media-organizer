#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frozen Stop03-3F dynamic Qwen-VL database node.

This is the formal entry point. It verifies every frozen implementation input
before delegating to the validated dynamic orchestrator.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import stop03_3f_qwenvl_dynamic_db_orchestrator_v1 as orchestrator


NODE_VERSION = "stop03_3f_qwenvl_dynamic_db_node_v1_frozen_20260716"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
FROZEN_FILES = {
    PROJECT_ROOT
    / "scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py":
        "920ff3e0573fe4cffde31bf4257e490a88e7dda90cc29f188003d2d70c3b58f3",
    PROJECT_ROOT
    / "scripts/stop03_monitor/stop03_3f_qwenvl_dynamic_db_monitor.py":
        "b70f90e1de219a30bd79046eb386a3699d9ad304c2fc0caeab1206cdee2c5393",
    PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json":
        "a50ff9d366793c3e3509faccf0933669c6c08f8802b8e799547f9d64c25c2e6b",
    PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt":
        "84c95c574720d1fe2b8991b67a2b55a9d5efb975445bc999746aa17d7ce35779",
}
FROZEN_CONTRACT_NAME = "stop03_2_v25_candidate_snapshot"
FROZEN_RUNTIME = {
    "scheduling_mode": "dynamic_database_claim",
    "workers": 3,
    "max_tokens": 384,
    "max_attempts": 3,
    "backend_version": "mlx_vlm_batch_generate_dynamic_claim_greedy_v1",
    "compact_retry_prompt_version": "compact_retry_prompt_v1",
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
        "--max-tokens": str(FROZEN_RUNTIME["max_tokens"]),
        "--max-attempts": str(FROZEN_RUNTIME["max_attempts"]),
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
        ("--max-tokens", FROZEN_RUNTIME["max_tokens"]),
        ("--max-attempts", FROZEN_RUNTIME["max_attempts"]),
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
    qwen_count = sum(item["queue_type"] == "qwenvl_high_value" for item in candidates)
    ocr_count = sum(item["queue_type"] == "ocr_trigger" for item in candidates)
    actual = {
        "status": contract["status"],
        "row_count": len(candidates),
        "qwenvl_count": qwen_count,
        "ocr_count": ocr_count,
        "candidate_id_set_sha256": sha256_text("\n".join(ids)),
        "candidate_semantic_digest_sha256": sha256_text(
            "\n".join(
                f"{item['candidate_id']}:{item['candidate_semantic_sha256']}"
                for item in candidates
            )
        ),
    }
    expected = {key: contract[key] for key in actual}
    if actual != expected or len(ids) != len(set(ids)):
        raise RuntimeError(
            "frozen_node_v25_contract_mismatch:"
            + json.dumps(
                {"actual": actual, "stored": expected},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return contract


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
        actual = orchestrator.contract.sha256_file(path)
        actual_hashes[str(path)] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen_node_file_hash_mismatch:{path}:{actual}:{expected}"
            )
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        actual_contract = verify_current_frozen_contract(con)
    finally:
        con.close()
    return {
        "status": "PASS",
        "node_version": NODE_VERSION,
        "frozen_files": actual_hashes,
        "frozen_contract": actual_contract,
        "frozen_runtime": dict(FROZEN_RUNTIME),
        "network_used": False,
        "download_used": False,
        "model_run": False,
        "central_db_modified": False,
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
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    return orchestrator.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
