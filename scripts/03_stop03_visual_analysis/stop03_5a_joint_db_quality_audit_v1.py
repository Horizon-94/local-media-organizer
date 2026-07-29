#!/usr/bin/env python3
"""Stop03-5A read-only joint quality audit for frozen Qwen-VL and OCR evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTRACT_VERSION = "stop03_5a_joint_quality_audit_v1"
QWEN_MARKERS = (
    "/Users/",
    "<|im_start|>",
    "<|im_end|>",
    "<|vision_start|>",
    "<|image_pad|>",
    "Prompt:",
    "Generation:",
    "Peak memory:",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def object_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        is not None
    )


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("stop03_5a_contract_version_mismatch")
    if data.get("original_video_read") is not False:
        raise RuntimeError("stop03_5a_original_video_policy_mismatch")
    if data.get("model_run") is not False or data.get("database_write") is not False:
        raise RuntimeError("stop03_5a_read_only_policy_mismatch")
    if data.get("qwen_run_selector") != "latest_complete_for_current_queue":
        raise RuntimeError("stop03_5a_qwen_run_selector_mismatch")
    if data.get("ocr_run_selector") != "latest_complete_full_for_current_queue":
        raise RuntimeError("stop03_5a_ocr_run_selector_mismatch")
    return data


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    fields = fields or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def table_columns(con: sqlite3.Connection, name: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({name})")}


def select_qwen_run(con: sqlite3.Connection, queue_count: int) -> sqlite3.Row:
    row = con.execute(
        """SELECT * FROM stop03_3_qwenvl_runs
           WHERE status='success' AND candidate_count=?
             AND pending_count=0 AND failed_count=0 AND review_count=0
             AND success_count=candidate_count
           ORDER BY started_at DESC, run_id DESC LIMIT 1""",
        (queue_count,),
    ).fetchone()
    if row is None:
        raise RuntimeError("stop03_5a_complete_qwen_run_missing")
    return row


def select_ocr_run(con: sqlite3.Connection, queue_count: int) -> sqlite3.Row:
    row = con.execute(
        """SELECT * FROM stop03_4_ocr_runs
           WHERE run_kind='full' AND status='success' AND candidate_count=?
             AND pending_count=0 AND running_count=0 AND failed_count=0
             AND success_count + no_text_count=candidate_count
           ORDER BY started_at DESC, run_id DESC LIMIT 1""",
        (queue_count,),
    ).fetchone()
    if row is None:
        raise RuntimeError("stop03_5a_complete_ocr_run_missing")
    return row


def preflight(db: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    required_objects = (
        "v_stop03_2_v25_qwenvl_execution_queue",
        "v_stop03_2_v25_ocr_execution_queue",
        "stop03_3_qwenvl_runs",
        "stop03_3_qwenvl_run_items",
        "stop03_3_qwenvl_results",
        "stop03_4_ocr_runs",
        "stop03_4_ocr_run_items",
        "stop03_4_ocr_results",
    )
    with connect_ro(db) as con:
        missing = [name for name in required_objects if not object_exists(con, name)]
        if missing:
            raise RuntimeError(f"stop03_5a_database_objects_missing:{missing}")
        qwen_queue_count = int(
            con.execute(
                "SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue"
            ).fetchone()[0]
        )
        ocr_queue_count = int(
            con.execute(
                "SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue"
            ).fetchone()[0]
        )
        qwen_run = select_qwen_run(con, qwen_queue_count)
        ocr_run = select_ocr_run(con, ocr_queue_count)
        qwen_run_id = str(qwen_run["run_id"])
        ocr_run_id = str(ocr_run["run_id"])
        qwen_selected_count = int(
            con.execute(
                "SELECT COUNT(*) FROM stop03_3_qwenvl_results WHERE run_id=?",
                (qwen_run_id,),
            ).fetchone()[0]
        )
        qwen_history_count = int(
            con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_results").fetchone()[0]
        ) - qwen_selected_count
        ocr_selected_count = int(
            con.execute(
                """SELECT COUNT(DISTINCT r.result_id)
                   FROM stop03_4_ocr_run_items i
                   JOIN stop03_4_ocr_results r ON r.result_id=i.result_id
                   WHERE i.run_id=?""",
                (ocr_run_id,),
            ).fetchone()[0]
        )
        ocr_history_count = int(
            con.execute("SELECT COUNT(*) FROM stop03_4_ocr_results").fetchone()[0]
        ) - ocr_selected_count
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = len(list(con.execute("PRAGMA foreign_key_check")))
    return {
        "status": "PASS",
        "contract_version": CONTRACT_VERSION,
        "qwen_acceptance_run_id": qwen_run_id,
        "ocr_acceptance_run_id": ocr_run_id,
        "qwen_queue_count": qwen_queue_count,
        "ocr_queue_count": ocr_queue_count,
        "qwen_selected_result_count": qwen_selected_count,
        "ocr_selected_result_count": ocr_selected_count,
        "qwen_history_result_rows": qwen_history_count,
        "ocr_history_result_rows": ocr_history_count,
        "database_integrity_check": integrity,
        "foreign_key_error_count": fk_errors,
        "database_write": False,
        "model_run": False,
        "original_video_read": False,
        "config": config,
    }


def load_qwen(con: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            """SELECT
                 i.run_item_id,i.status item_status,i.attempt_count,
                 i.candidate_id item_candidate_id,
                 i.source_content_id item_source_content_id,
                 i.visual_unit_id item_visual_unit_id,
                 i.canonical_visual_unit_id item_canonical_visual_unit_id,
                 i.derived_id item_derived_id,
                 i.candidate_role item_candidate_role,
                 i.reason_codes item_reason_codes,
                 i.policy_version item_policy_version,
                 i.runtime_visual_file item_runtime_visual_file,
                 i.runtime_visual_file_sha256 item_runtime_visual_file_sha256,
                 r.*
               FROM stop03_3_qwenvl_run_items i
               JOIN stop03_3_qwenvl_results r ON r.run_item_id=i.run_item_id
               WHERE i.run_id=? ORDER BY i.candidate_id""",
            (run_id,),
        )
    ]


def load_ocr(con: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            """SELECT
                 i.run_item_id,i.status item_status,i.attempt_count,
                 i.reused_existing_result,
                 i.candidate_id item_candidate_id,
                 i.source_content_id item_source_content_id,
                 i.visual_unit_id item_visual_unit_id,
                 i.canonical_visual_unit_id item_canonical_visual_unit_id,
                 i.derived_id item_derived_id,
                 i.candidate_role item_candidate_role,
                 i.reason_codes item_reason_codes,
                 i.policy_version item_policy_version,
                 i.runtime_visual_file item_runtime_visual_file,
                 i.runtime_visual_file_sha256 item_runtime_visual_file_sha256,
                 r.*
               FROM stop03_4_ocr_run_items i
               JOIN stop03_4_ocr_results r ON r.result_id=i.result_id
               WHERE i.run_id=? ORDER BY i.candidate_id""",
            (run_id,),
        )
    ]


def load_queue(con: sqlite3.Connection, view: str) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: dict(row) for row in con.execute(f"SELECT * FROM {view}")}


def lineage_errors(row: dict[str, Any], queue: dict[str, Any]) -> list[str]:
    mappings = {
        "source_content_id": "source_content_id",
        "visual_unit_id": "visual_unit_id",
        "canonical_visual_unit_id": "canonical_visual_unit_id",
        "derived_id": "derived_id",
        "candidate_role": "candidate_role",
        "reason_codes": "reason_codes",
        "policy_version": "policy_version",
        "runtime_visual_file_sha256": "runtime_visual_file_sha256",
    }
    errors = []
    for result_key, queue_key in mappings.items():
        if str(row.get(result_key, "")) != str(queue.get(queue_key, "")):
            errors.append(f"lineage_mismatch:{result_key}")
    return errors


def validate_file(path_value: str, expected_sha: str) -> tuple[bool, str]:
    path = Path(path_value)
    if not path.is_file():
        return False, "file_missing"
    if sha256_file(path) != expected_sha:
        return False, "file_sha256_mismatch"
    return True, ""


def duplicate_members(rows: Iterable[dict[str, Any]], text_key: str) -> set[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[sha256_text(normalize_text(str(row[text_key])))].append(
            str(row["candidate_id"])
        )
    return {
        candidate
        for members in groups.values()
        if len(members) > 1
        for candidate in members
    }


def audit_qwen(
    rows: list[dict[str, Any]],
    queue: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    duplicates = duplicate_members(rows, "clean_text")
    output: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str(row["candidate_id"])
        hard: list[str] = []
        review: list[str] = []
        queue_row = queue.get(candidate_id)
        if queue_row is None:
            hard.append("candidate_missing_from_frozen_qwen_queue")
        else:
            hard.extend(lineage_errors(row, queue_row))
        if row["item_status"] != "success" or row["result_status"] != "success":
            hard.append("non_success_status")
        text = str(row["clean_text"])
        if not text.strip():
            hard.append("empty_clean_text")
        if sha256_text(text) != row["clean_text_sha256"]:
            hard.append("clean_text_sha256_mismatch")
        missing_sections = [
            section for section in config["qwen_required_sections"] if section not in text
        ]
        if missing_sections:
            hard.append("missing_required_sections:" + "|".join(missing_sections))
        if any(marker in text for marker in QWEN_MARKERS):
            hard.append("wrapper_or_internal_path_remains")
        if row["truncation_status"] != "complete":
            hard.append("truncation_status_not_complete")
        if row["cleanup_status"] != "ok":
            hard.append("cleanup_status_not_ok")
        try:
            metrics = json.loads(row["runtime_metrics_json"])
            if not isinstance(metrics, dict):
                hard.append("runtime_metrics_not_object")
        except Exception:
            hard.append("runtime_metrics_json_invalid")
        for path_key, sha_key in (
            ("raw_stdout_path", "raw_stdout_sha256"),
            ("stderr_path", "stderr_sha256"),
            ("metrics_path", "metrics_sha256"),
        ):
            valid, reason = validate_file(str(row[path_key]), str(row[sha_key]))
            if not valid:
                hard.append(f"{path_key}:{reason}")
        length = len(text)
        if length < int(config["qwen_min_text_chars_review"]):
            review.append("qwen_text_short")
        if length > int(config["qwen_max_text_chars_review"]):
            review.append("qwen_text_long")
        if candidate_id in duplicates:
            review.append("qwen_exact_normalized_text_duplicate")
        status = "FAIL" if hard else ("REVIEW" if review else "PASS")
        output.append(
            {
                "modality": "qwenvl",
                "candidate_id": candidate_id,
                "evidence_id": row["evidence_id"],
                "source_content_id": row["source_content_id"],
                "visual_unit_id": row["visual_unit_id"],
                "canonical_visual_unit_id": row["canonical_visual_unit_id"],
                "derived_id": row["derived_id"],
                "candidate_role": row["candidate_role"],
                "quality_status": status,
                "hard_fail_reasons": "|".join(sorted(set(hard))),
                "review_reasons": "|".join(sorted(set(review))),
                "text_chars": length,
                "generation_tokens": row["generation_tokens"],
                "attempt_count": row["attempt_count"],
                "text_sha256": row["clean_text_sha256"],
                "text_preview": text[:240].replace("\n", " | "),
            }
        )
    return output


def audit_ocr(
    rows: list[dict[str, Any]],
    queue: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    duplicates = duplicate_members(rows, "ocr_text")
    output: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str(row["candidate_id"])
        hard: list[str] = []
        review: list[str] = []
        queue_row = queue.get(candidate_id)
        if queue_row is None:
            hard.append("candidate_missing_from_frozen_ocr_queue")
        else:
            hard.extend(lineage_errors(row, queue_row))
        if row["item_status"] not in {"success", "no_text"}:
            hard.append("run_item_non_terminal_success")
        if row["result_status"] not in {"success", "no_text"}:
            hard.append("result_non_terminal_success")
        text = str(row["ocr_text"])
        if row["result_status"] == "success" and not text.strip():
            hard.append("empty_ocr_success")
        if sha256_text(text) != row["ocr_text_sha256"]:
            hard.append("ocr_text_sha256_mismatch")
        try:
            lines = json.loads(row["ocr_lines_json"])
            if not isinstance(lines, list):
                hard.append("ocr_lines_not_list")
                lines = []
            if len(lines) != int(row["ocr_line_count"]):
                hard.append("ocr_line_count_mismatch")
        except Exception:
            hard.append("ocr_lines_json_invalid")
            lines = []
        valid, reason = validate_file(
            str(row["output_json_path"]), str(row["output_json_sha256"])
        )
        if not valid:
            hard.append(f"output_json_path:{reason}")
        length = len(text)
        if length < int(config["ocr_min_text_chars_review"]):
            review.append("ocr_text_short")
        confidence = row["mean_confidence"]
        if confidence is None or float(confidence) < float(
            config["ocr_mean_confidence_review_threshold"]
        ):
            review.append("ocr_mean_confidence_low")
        if candidate_id in duplicates:
            review.append("ocr_exact_normalized_text_duplicate")
        status = "FAIL" if hard else ("REVIEW" if review else "PASS")
        output.append(
            {
                "modality": "ocr",
                "candidate_id": candidate_id,
                "evidence_id": row["evidence_id"],
                "source_content_id": row["source_content_id"],
                "visual_unit_id": row["visual_unit_id"],
                "canonical_visual_unit_id": row["canonical_visual_unit_id"],
                "derived_id": row["derived_id"],
                "candidate_role": row["candidate_role"],
                "quality_status": status,
                "hard_fail_reasons": "|".join(sorted(set(hard))),
                "review_reasons": "|".join(sorted(set(review))),
                "text_chars": length,
                "ocr_line_count": row["ocr_line_count"],
                "mean_confidence": row["mean_confidence"],
                "min_confidence": row["min_confidence"],
                "max_confidence": row["max_confidence"],
                "attempt_count": row["attempt_count"],
                "reused_existing_result": row["reused_existing_result"],
                "text_sha256": row["ocr_text_sha256"],
                "text_preview": text[:240].replace("\n", " | "),
            }
        )
    return output


def cross_modal_overlap(
    qwen_rows: list[dict[str, Any]], ocr_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    q_by_visual = {row["visual_unit_id"]: row for row in qwen_rows}
    output = []
    for ocr_row in ocr_rows:
        qwen_row = q_by_visual.get(ocr_row["visual_unit_id"])
        if qwen_row is None:
            continue
        errors = []
        for key in (
            "source_content_id",
            "visual_unit_id",
            "canonical_visual_unit_id",
            "derived_id",
            "runtime_visual_file_sha256",
        ):
            if qwen_row[key] != ocr_row[key]:
                errors.append(f"cross_modal_lineage_mismatch:{key}")
        output.append(
            {
                "visual_unit_id": ocr_row["visual_unit_id"],
                "source_content_id": ocr_row["source_content_id"],
                "derived_id": ocr_row["derived_id"],
                "qwenvl_candidate_id": qwen_row["candidate_id"],
                "ocr_candidate_id": ocr_row["candidate_id"],
                "runtime_visual_file_sha256": ocr_row["runtime_visual_file_sha256"],
                "qwenvl_text_chars": len(str(qwen_row["clean_text"])),
                "ocr_text_chars": len(str(ocr_row["ocr_text"])),
                "ocr_mean_confidence": ocr_row["mean_confidence"],
                "quality_status": "FAIL" if errors else "PASS",
                "hard_fail_reasons": "|".join(errors),
            }
        )
    return output


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["quality_status"] for row in rows)
    return {key: counts.get(key, 0) for key in ("PASS", "REVIEW", "FAIL")}


def run_audit(db: Path, config_path: Path) -> tuple[dict[str, Any], dict[str, list]]:
    pre = preflight(db, config_path)
    config = pre["config"]
    with connect_ro(db) as con:
        qwen_rows = load_qwen(con, pre["qwen_acceptance_run_id"])
        ocr_rows = load_ocr(con, pre["ocr_acceptance_run_id"])
        qwen_queue = load_queue(con, "v_stop03_2_v25_qwenvl_execution_queue")
        ocr_queue = load_queue(con, "v_stop03_2_v25_ocr_execution_queue")
        qwen_history = int(
            con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_results").fetchone()[0]
        ) - len(qwen_rows)
        ocr_history = int(
            con.execute("SELECT COUNT(*) FROM stop03_4_ocr_results").fetchone()[0]
        ) - len({row["result_id"] for row in ocr_rows})

    qwen_ids = {row["candidate_id"] for row in qwen_rows}
    ocr_ids = {row["candidate_id"] for row in ocr_rows}
    qwen_set_equal = qwen_ids == set(qwen_queue)
    ocr_set_equal = ocr_ids == set(ocr_queue)
    qwen_audit = audit_qwen(qwen_rows, qwen_queue, config)
    ocr_audit = audit_ocr(ocr_rows, ocr_queue, config)
    overlap = cross_modal_overlap(qwen_rows, ocr_rows)

    evidence_ids = [row["evidence_id"] for row in qwen_rows + ocr_rows]
    candidate_ids = [row["candidate_id"] for row in qwen_rows + ocr_rows]
    execution_keys = [row["execution_key"] for row in qwen_rows + ocr_rows]
    global_hard = []
    if not qwen_set_equal:
        global_hard.append("qwen_candidate_set_not_equal_frozen_view")
    if not ocr_set_equal:
        global_hard.append("ocr_candidate_set_not_equal_frozen_view")
    if len(evidence_ids) != len(set(evidence_ids)):
        global_hard.append("combined_evidence_id_collision")
    if len(candidate_ids) != len(set(candidate_ids)):
        global_hard.append("combined_candidate_id_collision")
    if len(execution_keys) != len(set(execution_keys)):
        global_hard.append("combined_execution_key_collision")
    if any(row["quality_status"] == "FAIL" for row in overlap):
        global_hard.append("cross_modal_overlap_lineage_failure")
    if pre["database_integrity_check"] != "ok" or pre["foreign_key_error_count"]:
        global_hard.append("database_integrity_failure")

    all_audit = qwen_audit + ocr_audit
    hard_fail_count = sum(row["quality_status"] == "FAIL" for row in all_audit)
    review_items = [row for row in all_audit if row["quality_status"] == "REVIEW"]
    technical_status = "PASS" if not global_hard and hard_fail_count == 0 else "FAIL"
    policy_status = "REVIEW" if review_items else "PASS"
    if technical_status == "PASS":
        staging = (
            "READY_WITH_QUALITY_FLAGS"
            if review_items and config["allow_quality_review_for_staging"]
            else "READY"
        )
    else:
        staging = "DO_NOT_STAGE"
    status = (
        "PASS"
        if technical_status == "PASS" and policy_status == "PASS"
        else (
            "PASS_WITH_REVIEW"
            if technical_status == "PASS" and staging == "READY_WITH_QUALITY_FLAGS"
            else "FAIL"
        )
    )
    summary = {
        "status": status,
        "technical_status": technical_status,
        "policy_status": policy_status,
        "staging_readiness": staging,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
        "qwen_acceptance_run_id": pre["qwen_acceptance_run_id"],
        "ocr_acceptance_run_id": pre["ocr_acceptance_run_id"],
        "qwen_queue_count": len(qwen_queue),
        "qwen_selected_result_count": len(qwen_rows),
        "qwen_history_result_rows_excluded": qwen_history,
        "qwen_candidate_set_equal": qwen_set_equal,
        "qwen_quality_counts": status_counts(qwen_audit),
        "ocr_queue_count": len(ocr_queue),
        "ocr_selected_result_count": len(ocr_rows),
        "ocr_history_result_rows_excluded": ocr_history,
        "ocr_candidate_set_equal": ocr_set_equal,
        "ocr_quality_counts": status_counts(ocr_audit),
        "combined_evidence_count": len(evidence_ids),
        "combined_unique_evidence_count": len(set(evidence_ids)),
        "combined_unique_candidate_count": len(set(candidate_ids)),
        "combined_execution_key_count": len(set(execution_keys)),
        "cross_modal_candidate_overlap_count": len(qwen_ids & ocr_ids),
        "cross_modal_visual_overlap_count": len(overlap),
        "cross_modal_visual_overlap_fail_count": sum(
            row["quality_status"] == "FAIL" for row in overlap
        ),
        "hard_fail_item_count": hard_fail_count,
        "global_hard_fail_reasons": global_hard,
        "quality_review_item_count": len(review_items),
        "quality_review_reason_counts": dict(
            Counter(
                reason
                for row in review_items
                for reason in str(row["review_reasons"]).split("|")
                if reason
            )
        ),
        "database_integrity_check": pre["database_integrity_check"],
        "foreign_key_error_count": pre["foreign_key_error_count"],
        "central_db_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }
    return summary, {
        "qwen": qwen_audit,
        "ocr": ocr_audit,
        "overlap": overlap,
        "review": review_items,
    }


def write_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Stop03-5A Joint Evidence Quality Audit",
        "",
        f"- status: `{summary['status']}`",
        f"- technical_status: `{summary['technical_status']}`",
        f"- policy_status: `{summary['policy_status']}`",
        f"- staging_readiness: `{summary['staging_readiness']}`",
        "",
        "## Formal evidence sets",
        "",
        f"- Qwen-VL: {summary['qwen_selected_result_count']} selected; "
        f"{summary['qwen_history_result_rows_excluded']} historical rows excluded",
        f"- OCR: {summary['ocr_selected_result_count']} selected; "
        f"{summary['ocr_history_result_rows_excluded']} historical rows excluded",
        f"- combined evidence: {summary['combined_evidence_count']}",
        "",
        "## Quality",
        "",
        f"- Qwen-VL: `{summary['qwen_quality_counts']}`",
        f"- OCR: `{summary['ocr_quality_counts']}`",
        f"- review items: {summary['quality_review_item_count']}",
        f"- review reasons: `{summary['quality_review_reason_counts']}`",
        f"- hard fail items: {summary['hard_fail_item_count']}",
        "",
        "## Cross-modal",
        "",
        f"- candidate overlap: {summary['cross_modal_candidate_overlap_count']}",
        f"- visual overlap: {summary['cross_modal_visual_overlap_count']}",
        f"- overlap lineage failures: {summary['cross_modal_visual_overlap_fail_count']}",
        "",
        "## Safety and integrity",
        "",
        f"- SQLite integrity: `{summary['database_integrity_check']}`",
        f"- foreign key errors: {summary['foreign_key_error_count']}",
        "- central DB write: false",
        "- model run: false",
        "- network/download: false",
        "- original video read: false",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    root = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "audit"), required=True)
    parser.add_argument("--db", type=Path, default=root / "media_archive.sqlite")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "configs/stop03_5a_joint_quality_audit_v1.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "/Users/yourname/Documents/AI-Local/test-output/"
            "stop03_5a_joint_db_quality_audit_v1"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_sha_before = sha256_file(args.db)
    if args.mode == "preflight":
        report = preflight(args.db, args.config)
        report.pop("config", None)
        report["central_db_sha256"] = db_sha_before
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    summary, details = run_audit(args.db, args.config)
    db_sha_after = sha256_file(args.db)
    summary["central_db_sha256_before"] = db_sha_before
    summary["central_db_sha256_after"] = db_sha_after
    summary["central_db_unchanged"] = db_sha_before == db_sha_after
    if not summary["central_db_unchanged"]:
        summary["status"] = "FAIL"
        summary["technical_status"] = "FAIL"
        summary["staging_readiness"] = "DO_NOT_STAGE"
        summary["global_hard_fail_reasons"].append("central_database_changed")
    write_json(args.out / "reports/stop03_5a_joint_quality_summary.json", summary)
    write_markdown(args.out / "reports/stop03_5a_joint_quality_summary.md", summary)
    write_csv(args.out / "manifests/qwen_quality_audit.csv", details["qwen"])
    write_csv(args.out / "manifests/ocr_quality_audit.csv", details["ocr"])
    write_csv(args.out / "manifests/cross_modal_visual_overlap.csv", details["overlap"])
    write_csv(args.out / "manifests/quality_review_items.csv", details["review"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
