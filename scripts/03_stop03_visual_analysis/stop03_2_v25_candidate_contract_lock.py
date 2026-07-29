#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lock the committed Stop03-2 V25 candidate ledger into an immutable DB contract.

Preflight and dry-run open the central SQLite database with mode=ro and
query_only=ON.  Only commit may write, and commit is deliberately not executed
by this task.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


SCRIPT_VERSION = "stop03_2_v25_candidate_contract_lock_v1_20260711"
CONTRACT_NAME = "stop03_2_v25_candidate_snapshot"
SNAPSHOT_CONTRACT_VERSION = "stop03_2_v25_candidate_snapshot_v1"
EXPECTED_TOTAL = 390
EXPECTED_QWEN = 336
EXPECTED_OCR = 54
EXPECTED_CANDIDATE_ID_SET_SHA256 = "d14c7570230b6c2e3a605c0a3f35d04f3cf4aec62a680838907138570ef84e15"
EXPECTED_CANDIDATE_SEMANTIC_DIGEST_SHA256 = "de34d067fec2d132d6b67bfe7baee251d8dd63c7174fbc556cbae84d243b1b22"

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
MIGRATION = PROJECT_ROOT / "migrations/20260711_stop03_2_v25_candidate_snapshot_qwenvl_v1.sql"
RULE_DOCUMENT = PROJECT_ROOT / "docs/pipeline_rules/STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md"
POLICY_CONFIG = PROJECT_ROOT / "configs/stop03_2_high_value_policy_v25.json"
CANDIDATE_SCRIPT = PROJECT_ROOT / "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-2-v25-candidate-contract-lock-dry-run"

FORCED_ID_FIELDS = (
    "candidate_id", "source_content_id", "visual_unit_id",
    "canonical_visual_unit_id", "derived_id",
)
SNAPSHOT_COLUMNS = (
    "candidate_id", "queue_type", "candidate_role", "candidate_score", "reason_codes",
    "source_content_id", "visual_unit_id", "canonical_visual_unit_id", "derived_id",
    "duplicate_group_id", "frame_index", "time_position_ms", "canonical_time_ms",
    "group_start_ms", "group_end_ms", "segment_start_ms", "segment_end_ms",
    "media_type", "visual_unit_type", "source_relative_path", "runtime_visual_file",
    "runtime_visual_file_sha256", "size_bytes", "mtime_ns", "yoloe_labels_json",
    "yoloe_label_count", "yoloe_labels_sha256", "yoloe_label_status",
    "policy_version", "rule_document_sha256", "config_sha256",
    "candidate_script_sha256", "central_dedup_run_id", "yoloe_run_id",
    "openclip_run_id", "candidate_semantic_sha256", "snapshot_contract_version",
    "snapshot_created_at", "frozen_status",
)
SEMANTIC_FIELDS = (
    "candidate_id", "queue_type", "candidate_role", "candidate_score", "reason_codes",
    "source_content_id", "visual_unit_id", "canonical_visual_unit_id", "derived_id",
    "duplicate_group_id", "frame_index", "time_position_ms", "canonical_time_ms",
    "group_start_ms", "group_end_ms", "segment_start_ms", "segment_end_ms",
    "media_type", "visual_unit_type", "source_relative_path",
    "runtime_visual_file_sha256", "yoloe_labels_sha256", "yoloe_label_status",
    "policy_version", "rule_document_sha256", "config_sha256",
    "candidate_script_sha256", "central_dedup_run_id", "yoloe_run_id",
    "openclip_run_id", "snapshot_contract_version",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_offline_environment() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect_readonly(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def connect_write(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def object_exists(con: sqlite3.Connection, name: str, kind: Optional[str] = None) -> bool:
    if kind:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
        ).fetchone()
    else:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
    return row is not None


def assert_output_path(path: Path, *, may_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    root = TEST_OUTPUT_ROOT.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    if resolved == root:
        raise RuntimeError("output_must_not_equal_test_output_root")
    if not may_exist and resolved.exists() and any(resolved.iterdir()):
        raise RuntimeError(f"output_not_empty:{resolved}")
    return resolved


def file_state(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "mtime_ns": stat.st_mtime_ns,
        "size_bytes": stat.st_size,
    }


def central_counts(con: sqlite3.Connection) -> Dict[str, int]:
    return {
        "candidate_queue_items": int(
            con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_items").fetchone()[0]
        ),
        "model_runs": int(con.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]),
        "frozen_rows": int(
            con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_frozen_v25").fetchone()[0]
        ) if object_exists(con, "stop03_2_candidate_queue_frozen_v25", "table") else 0,
        "frozen_contracts": int(
            con.execute("SELECT COUNT(*) FROM pipeline_frozen_contracts").fetchone()[0]
        ) if object_exists(con, "pipeline_frozen_contracts", "table") else 0,
    }


def candidate_ledger_audit(con: sqlite3.Connection) -> Dict[str, Any]:
    """Fingerprint the original candidate ledger without changing its schema or rows."""
    columns = [str(row[1]) for row in con.execute(
        "PRAGMA table_info(stop03_2_candidate_queue_items)"
    )]
    if "candidate_id" not in columns:
        raise RuntimeError("candidate_ledger_candidate_id_missing")
    quoted = ",".join(f'"{name}"' for name in columns)
    rows = [dict(row) for row in con.execute(
        f"SELECT {quoted} FROM stop03_2_candidate_queue_items ORDER BY candidate_id"
    )]
    ids = [str(row.get("candidate_id") or "") for row in rows]
    return {
        "row_count": len(rows),
        "candidate_id_set_sha256": sha256_text("\n".join(ids)),
        "ledger_semantic_sha256": sha256_text(
            "\n".join(stable_json(row) for row in rows)
        ),
        "columns": columns,
    }


def candidate_base_rows(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    sql = """
    SELECT c.candidate_id,c.queue_type,c.candidate_role,c.candidate_score,c.reason_codes,
           c.source_content_id,c.visual_unit_id,c.canonical_visual_unit_id,c.derived_id,
           COALESCE(c.duplicate_group_id,'') AS duplicate_group_id,
           COALESCE(c.frame_index,da.frame_index,-1) AS frame_index,
           COALESCE(c.time_position_ms,da.time_position_ms,vu.time_position_ms,-1) AS time_position_ms,
           COALESCE(c.canonical_time_ms,c.time_position_ms,da.time_position_ms,-1) AS canonical_time_ms,
           COALESCE(c.group_start_ms,c.time_position_ms,da.time_position_ms,-1) AS group_start_ms,
           COALESCE(c.group_end_ms,c.time_position_ms,da.time_position_ms,-1) AS group_end_ms,
           COALESCE(c.segment_start_ms,0) AS segment_start_ms,
           COALESCE(c.segment_end_ms,0) AS segment_end_ms,
           sa.media_type,
           CASE WHEN sa.media_type='video' THEN 'video_frame' ELSE 'image_preview' END AS visual_unit_type,
           sa.relative_path AS source_relative_path,vu.visual_file AS runtime_visual_file,
           c.policy_version,c.rule_document_sha256,c.config_sha256,
           c.script_sha256 AS candidate_script_sha256,
           c.central_dedup_run_id,c.yoloe_run_id,c.openclip_run_id
    FROM stop03_2_candidate_queue_items AS c
    JOIN visual_units AS vu ON vu.visual_unit_id=c.visual_unit_id
    JOIN derived_assets AS da ON da.derived_id=c.derived_id
    JOIN source_assets AS sa ON sa.source_content_id=c.source_content_id
    WHERE c.script_version='stop03_2_candidate_queues_from_db_safe_v25_0_20260711'
    ORDER BY c.candidate_id
    """
    return [dict(row) for row in con.execute(sql)]


def load_yoloe_labels(con: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    sql = """
    SELECT visual_unit_id,label,confidence,bbox,model_name,model_path,
           text_encoder_asset,run_id
    FROM visual_labels
    ORDER BY visual_unit_id,label,confidence,bbox,label_id
    """
    for raw in con.execute(sql):
        row = dict(raw)
        grouped[str(row.pop("visual_unit_id"))].append(row)
    return grouped


def actual_frozen_hashes() -> Dict[str, str]:
    return {
        "rule_document_sha256": sha256_file(RULE_DOCUMENT),
        "config_sha256": sha256_file(POLICY_CONFIG),
        "candidate_script_sha256": sha256_file(CANDIDATE_SCRIPT),
        "migration_sha256": sha256_file(MIGRATION),
    }


def preflight(db: Path, out: Path) -> Dict[str, Any]:
    hashes = actual_frozen_hashes()
    con = connect_readonly(db)
    try:
        rows = candidate_base_rows(con)
        counts = Counter(str(row.get("queue_type") or "") for row in rows)
        ids = [str(row.get("candidate_id") or "") for row in rows]
        missing_forced = {
            field: sum(not str(row.get(field) or "").strip() for row in rows)
            for field in FORCED_ID_FIELDS
        }
        distinct_hashes = {
            key: sorted({str(row.get(key) or "") for row in rows})
            for key in ("rule_document_sha256", "config_sha256", "candidate_script_sha256")
        }
        runtime_exists = sum(Path(str(row["runtime_visual_file"])).is_file() for row in rows)
        runtime_under_test_output = 0
        for row in rows:
            path = Path(str(row["runtime_visual_file"])).expanduser().resolve(strict=False)
            try:
                path.relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
                runtime_under_test_output += 1
            except ValueError:
                pass
        candidate_id_digest = sha256_text("\n".join(sorted(ids)))
        checks = {
            "row_count_390": len(rows) == EXPECTED_TOTAL,
            "qwenvl_count_336": counts["qwenvl_high_value"] == EXPECTED_QWEN,
            "ocr_count_54": counts["ocr_trigger"] == EXPECTED_OCR,
            "candidate_ids_unique": len(ids) == len(set(ids)),
            "candidate_id_digest_frozen": candidate_id_digest == EXPECTED_CANDIDATE_ID_SET_SHA256,
            "forced_ids_complete": all(value == 0 for value in missing_forced.values()),
            "runtime_files_exist": runtime_exists == len(rows),
            "runtime_files_are_derived_test_output": runtime_under_test_output == len(rows),
            "rule_sha_matches": distinct_hashes["rule_document_sha256"] == [hashes["rule_document_sha256"]],
            "config_sha_matches": distinct_hashes["config_sha256"] == [hashes["config_sha256"]],
            "script_sha_matches": distinct_hashes["candidate_script_sha256"] == [hashes["candidate_script_sha256"]],
        }
        status = "PASS" if all(checks.values()) else "FAIL"
        return {
            "status": status,
            "technical_status": status,
            "policy_status": "PASS" if status == "PASS" else "FAIL",
            "commit_status": "DO_NOT_COMMIT",
            "mode": "preflight",
            "script_version": SCRIPT_VERSION,
            "contract_name": CONTRACT_NAME,
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "row_count": len(rows),
            "qwenvl_count": counts["qwenvl_high_value"],
            "ocr_count": counts["ocr_trigger"],
            "missing_forced_ids": missing_forced,
            "runtime_sha_ready_count": runtime_exists,
            "runtime_under_test_output_count": runtime_under_test_output,
            "candidate_id_set_sha256": candidate_id_digest,
            "frozen_hashes": hashes,
            "candidate_distinct_hashes": distinct_hashes,
            "checks": checks,
            "central_counts": central_counts(con),
            "frozen_table_exists": object_exists(con, "stop03_2_candidate_queue_frozen_v25", "table"),
            "qwen_view_exists": object_exists(con, "v_stop03_2_v25_qwenvl_execution_queue", "view"),
            "out_path_checked_not_created": str(out),
            "sqlite_open_mode": "mode=ro",
            "sqlite_query_only": True,
            "central_db_modified": False,
            "model_run": False,
            "original_video_read": False,
            "network_used": False,
        }
    finally:
        con.close()


def build_snapshot(db: Path, *, created_at: Optional[str] = None) -> Dict[str, Any]:
    created_at = created_at or now_iso()
    con = connect_readonly(db)
    try:
        rows = candidate_base_rows(con)
        labels_by_vu = load_yoloe_labels(con)
    finally:
        con.close()
    snapshot: List[Dict[str, Any]] = []
    missing_forced = Counter()
    runtime_sha_mismatch_count = 0
    no_label_count = 0
    for base in rows:
        for field in FORCED_ID_FIELDS:
            if not str(base.get(field) or "").strip():
                missing_forced[field] += 1
        runtime = Path(str(base.get("runtime_visual_file") or "")).expanduser().resolve(strict=True)
        try:
            runtime.relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
        except ValueError as exc:
            raise RuntimeError(f"runtime_not_derived_test_output:{runtime}") from exc
        runtime_sha = sha256_file(runtime)
        stat = runtime.stat()
        labels = labels_by_vu.get(str(base["visual_unit_id"]), [])
        labels_json = stable_json(labels)
        labels_sha = sha256_text(labels_json)
        label_status = "labeled" if labels else "no_label"
        if not labels:
            no_label_count += 1
        row = {
            **base,
            "runtime_visual_file": str(runtime),
            "runtime_visual_file_sha256": runtime_sha,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "yoloe_labels_json": labels_json,
            "yoloe_label_count": len(labels),
            "yoloe_labels_sha256": labels_sha,
            "yoloe_label_status": label_status,
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "snapshot_created_at": created_at,
            "frozen_status": "FROZEN",
        }
        semantic = {field: row.get(field) for field in SEMANTIC_FIELDS}
        row["candidate_semantic_sha256"] = sha256_text(stable_json(semantic))
        snapshot.append({field: row.get(field) for field in SNAPSHOT_COLUMNS})
    counts = Counter(str(row["queue_type"]) for row in snapshot)
    ids = sorted(str(row["candidate_id"]) for row in snapshot)
    candidate_id_set_sha = sha256_text("\n".join(ids))
    semantic_digest = sha256_text(
        "\n".join(
            f"{row['candidate_id']}:{row['candidate_semantic_sha256']}"
            for row in sorted(snapshot, key=lambda item: str(item["candidate_id"]))
        )
    )
    hashes = actual_frozen_hashes()
    technical = (
        len(snapshot) == EXPECTED_TOTAL
        and counts["qwenvl_high_value"] == EXPECTED_QWEN
        and counts["ocr_trigger"] == EXPECTED_OCR
        and len(ids) == len(set(ids))
        and not missing_forced
        and runtime_sha_mismatch_count == 0
        and candidate_id_set_sha == EXPECTED_CANDIDATE_ID_SET_SHA256
        and semantic_digest == EXPECTED_CANDIDATE_SEMANTIC_DIGEST_SHA256
    )
    return {
        "rows": snapshot,
        "summary": {
            "status": "PASS" if technical else "FAIL",
            "technical_status": "PASS" if technical else "FAIL",
            "policy_status": "PASS" if technical else "FAIL",
            "commit_status": "DO_NOT_COMMIT",
            "contract_name": CONTRACT_NAME,
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "row_count": len(snapshot),
            "qwenvl_count": counts["qwenvl_high_value"],
            "ocr_count": counts["ocr_trigger"],
            "candidate_id_set_sha256": candidate_id_set_sha,
            "candidate_semantic_digest_sha256": semantic_digest,
            "missing_forced_ids": {field: missing_forced[field] for field in FORCED_ID_FIELDS},
            "runtime_sha_ready_count": len(snapshot),
            "runtime_sha_mismatch_count": runtime_sha_mismatch_count,
            "yoloe_no_label_count": no_label_count,
            "yoloe_labeled_count": len(snapshot) - no_label_count,
            "frozen_hashes": hashes,
            "central_db_modified": False,
            "model_run": False,
            "original_video_read": False,
            "network_used": False,
        },
    }


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> int:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SNAPSHOT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def contract_record(summary: Mapping[str, Any], locked_at: str) -> Dict[str, Any]:
    hashes = summary["frozen_hashes"]
    return {
        "contract_name": CONTRACT_NAME,
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "row_count": summary["row_count"],
        "qwenvl_count": summary["qwenvl_count"],
        "ocr_count": summary["ocr_count"],
        "candidate_id_set_sha256": summary["candidate_id_set_sha256"],
        "candidate_semantic_digest_sha256": summary["candidate_semantic_digest_sha256"],
        "rule_document_sha256": hashes["rule_document_sha256"],
        "config_sha256": hashes["config_sha256"],
        "candidate_script_sha256": hashes["candidate_script_sha256"],
        "locked_at": locked_at,
        "status": "FROZEN",
    }


def readback(con: sqlite3.Connection) -> Dict[str, Any]:
    if not object_exists(con, "stop03_2_candidate_queue_frozen_v25", "table"):
        raise RuntimeError("frozen_snapshot_table_missing")
    if not object_exists(con, "pipeline_frozen_contracts", "table"):
        raise RuntimeError("pipeline_frozen_contracts_missing")
    contract = con.execute(
        "SELECT * FROM pipeline_frozen_contracts WHERE contract_name=?", (CONTRACT_NAME,)
    ).fetchone()
    if contract is None:
        raise RuntimeError("v25_frozen_contract_missing")
    row_count = int(con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_frozen_v25").fetchone()[0])
    qwen_count = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue").fetchone()[0])
    ocr_count = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue").fetchone()[0])
    ids = [str(row[0]) for row in con.execute(
        "SELECT candidate_id FROM stop03_2_candidate_queue_frozen_v25 ORDER BY candidate_id"
    )]
    semantic = [f"{row[0]}:{row[1]}" for row in con.execute(
        "SELECT candidate_id,candidate_semantic_sha256 FROM stop03_2_candidate_queue_frozen_v25 ORDER BY candidate_id"
    )]
    result = {
        "row_count": row_count,
        "qwenvl_count": qwen_count,
        "ocr_count": ocr_count,
        "candidate_id_set_sha256": sha256_text("\n".join(ids)),
        "candidate_semantic_digest_sha256": sha256_text("\n".join(semantic)),
        "contract": dict(contract),
    }
    result["status"] = "PASS" if (
        row_count == EXPECTED_TOTAL
        and qwen_count == EXPECTED_QWEN
        and ocr_count == EXPECTED_OCR
        and result["candidate_id_set_sha256"] == contract["candidate_id_set_sha256"]
        and result["candidate_semantic_digest_sha256"] == contract["candidate_semantic_digest_sha256"]
        and result["candidate_id_set_sha256"] == EXPECTED_CANDIDATE_ID_SET_SHA256
        and result["candidate_semantic_digest_sha256"] == EXPECTED_CANDIDATE_SEMANTIC_DIGEST_SHA256
    ) else "FAIL"
    return result


def apply_snapshot_transaction(
    db: Path, snapshot: Mapping[str, Any], migration: Path = MIGRATION
) -> Dict[str, Any]:
    summary = snapshot["summary"]
    rows = snapshot["rows"]
    if summary["candidate_id_set_sha256"] != EXPECTED_CANDIDATE_ID_SET_SHA256:
        raise RuntimeError("candidate_id_digest_mismatch_from_frozen_v25")
    if summary["candidate_semantic_digest_sha256"] != EXPECTED_CANDIDATE_SEMANTIC_DIGEST_SHA256:
        raise RuntimeError("candidate_semantic_digest_mismatch_from_frozen_v25")
    expected_contract = contract_record(summary, now_iso())
    con = connect_write(db)
    try:
        ledger_before = candidate_ledger_audit(con)
        existing = None
        if object_exists(con, "pipeline_frozen_contracts", "table"):
            existing = con.execute(
                "SELECT * FROM pipeline_frozen_contracts WHERE contract_name=?", (CONTRACT_NAME,)
            ).fetchone()
        if existing is not None:
            stable_fields = (
                "snapshot_contract_version", "row_count", "qwenvl_count", "ocr_count",
                "candidate_id_set_sha256", "candidate_semantic_digest_sha256",
                "rule_document_sha256", "config_sha256", "candidate_script_sha256", "status",
            )
            differences = {
                field: {"db": existing[field], "computed": expected_contract[field]}
                for field in stable_fields if existing[field] != expected_contract[field]
            }
            if differences:
                raise RuntimeError("idempotency_digest_mismatch:" + stable_json(differences))
            rb = readback(con)
            if rb["status"] != "PASS":
                raise RuntimeError("idempotent_readback_failed")
            ledger_after = candidate_ledger_audit(con)
            if ledger_after != ledger_before:
                raise RuntimeError("candidate_ledger_changed_during_idempotent_check")
            return {
                "status": "IDEMPOTENT_PASS", "idempotent": True,
                "readback": rb, "candidate_ledger_before": ledger_before,
                "candidate_ledger_after": ledger_after,
            }
        migration_sql = migration.read_text(encoding="utf-8")
        con.executescript("BEGIN IMMEDIATE;\n" + migration_sql)
        existing_rows = int(
            con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_frozen_v25").fetchone()[0]
        )
        if existing_rows:
            raise RuntimeError(f"orphan_frozen_rows_without_contract:{existing_rows}")
        placeholders = ",".join("?" for _ in SNAPSHOT_COLUMNS)
        con.executemany(
            f"INSERT INTO stop03_2_candidate_queue_frozen_v25 ({','.join(SNAPSHOT_COLUMNS)}) VALUES ({placeholders})",
            [[row.get(field) for field in SNAPSHOT_COLUMNS] for row in rows],
        )
        contract_fields = tuple(expected_contract)
        con.execute(
            f"INSERT INTO pipeline_frozen_contracts ({','.join(contract_fields)}) VALUES ({','.join('?' for _ in contract_fields)})",
            [expected_contract[field] for field in contract_fields],
        )
        rb = readback(con)
        if rb["status"] != "PASS":
            raise RuntimeError("snapshot_readback_failed")
        ledger_after = candidate_ledger_audit(con)
        if ledger_after != ledger_before:
            raise RuntimeError("candidate_ledger_changed_during_contract_commit")
        con.commit()
        return {
            "status": "PASS", "idempotent": False, "readback": rb,
            "candidate_ledger_before": ledger_before,
            "candidate_ledger_after": ledger_after,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def backup_database(db: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def restore_database(backup: Path, db: Path) -> None:
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    destination = sqlite3.connect(str(db))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def commit_with_backup(db: Path, snapshot: Mapping[str, Any], out: Path) -> Dict[str, Any]:
    backup = out / "backups" / f"media_archive_before_v25_contract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
    backup_database(db, backup)
    before = file_state(db)
    try:
        result = apply_snapshot_transaction(db, snapshot)
    except Exception:
        restore_database(backup, db)
        raise
    result["backup_path"] = str(backup)
    result["db_before"] = before
    result["db_after"] = file_state(db)
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lock the Stop03-2 V25 candidate DB contract")
    parser.add_argument("--mode", required=True, choices=("preflight", "dry-run", "commit", "readback"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    set_offline_environment()
    args = parse_args(argv)
    db = Path(args.db).expanduser().resolve(strict=True)
    out = assert_output_path(Path(args.out), may_exist=args.mode in {"readback"})
    try:
        if args.mode == "preflight":
            result = preflight(db, out)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["technical_status"] == "PASS" else 2
        if args.mode == "readback":
            con = connect_readonly(db)
            try:
                rb = readback(con)
            finally:
                con.close()
            result = {
                "status": rb["status"], "technical_status": rb["status"],
                "policy_status": "PASS" if rb["status"] == "PASS" else "FAIL",
                "commit_status": "READBACK_ONLY", "readback": rb,
                "central_db_modified": False, "model_run": False,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if rb["status"] == "PASS" else 2

        pre = preflight(db, out)
        if pre["technical_status"] != "PASS":
            raise RuntimeError("preflight_failed")
        db_before = file_state(db)
        con = connect_readonly(db)
        try:
            counts_before = central_counts(con)
        finally:
            con.close()
        snapshot = build_snapshot(db)
        if snapshot["summary"]["technical_status"] != "PASS":
            raise RuntimeError("snapshot_build_failed")
        out.mkdir(parents=True, exist_ok=False)
        reports = out / "reports"
        manifests = out / "manifests"
        reports.mkdir()
        manifests.mkdir()
        csv_rows = write_csv(manifests / "stop03_2_candidate_queue_frozen_v25.csv", snapshot["rows"])
        jsonl_rows = write_jsonl(manifests / "stop03_2_candidate_queue_frozen_v25.jsonl", snapshot["rows"])
        result = dict(snapshot["summary"])
        result.update({
            "mode": args.mode,
            "script_version": SCRIPT_VERSION,
            "manifest_csv_rows": csv_rows,
            "manifest_jsonl_rows": jsonl_rows,
            "outputs": {
                "snapshot_csv": str(manifests / "stop03_2_candidate_queue_frozen_v25.csv"),
                "snapshot_jsonl": str(manifests / "stop03_2_candidate_queue_frozen_v25.jsonl"),
                "summary_json": str(reports / "stop03_2_v25_contract_lock_summary.json"),
            },
        })
        if args.mode == "commit":
            result["commit"] = commit_with_backup(db, snapshot, out)
            result["commit_status"] = "COMMITTED"
        db_after = file_state(db)
        con = connect_readonly(db)
        try:
            counts_after = central_counts(con)
        finally:
            con.close()
        unchanged = db_before == db_after and counts_before == counts_after
        result["read_only_integrity"] = {
            "db_before": db_before, "db_after": db_after,
            "counts_before": counts_before, "counts_after": counts_after,
            "central_db_unchanged": unchanged,
        }
        result["central_db_modified"] = not unchanged
        if args.mode == "dry-run" and not unchanged:
            result["status"] = result["technical_status"] = "FAIL"
        (reports / "stop03_2_v25_contract_lock_summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["technical_status"] == "PASS" else 2
    except Exception as exc:
        failure = {
            "status": "FAIL", "technical_status": "FAIL", "policy_status": "FAIL",
            "commit_status": "DO_NOT_COMMIT", "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__, "error_message": str(exc),
            "central_db_modified": False, "model_run": False,
            "original_video_read": False, "network_used": False,
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
