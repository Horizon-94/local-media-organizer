#!/usr/bin/env python3
"""Append successful Qwen image supplements to the latest unified evidence run.

The previous evidence run remains immutable.  A new deterministic staging run
is created by copying its rows and adding successful supplement results.  OCR
is never propagated or recomputed here, and no model is loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "stop03_5b_qwenvl_supplement_merge_v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db: Path, *, query_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    if query_only:
        con.execute("PRAGMA query_only=ON")
    return con


def build_merge(db: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    con = connect(db, query_only=True)
    try:
        base_run = con.execute(
            """SELECT * FROM stop03_5_unified_evidence_runs
               WHERE status='success'
               ORDER BY created_at DESC,staging_run_id DESC LIMIT 1"""
        ).fetchone()
        if base_run is None:
            raise RuntimeError("supplement_merge_base_evidence_run_missing")
        base_rows = [dict(row) for row in con.execute(
            "SELECT * FROM stop03_5_unified_evidence_items WHERE staging_run_id=?",
            (base_run["staging_run_id"],),
        )]
        supplement_source = [dict(row) for row in con.execute(
            """SELECT r.result_id,r.run_id,r.candidate_id,r.execution_key,r.evidence_id,
                      r.clean_text,r.clean_text_sha256,r.generation_tokens,r.finish_reason,
                      r.runtime_visual_file_sha256,r.created_at,
                      c.source_content_id,c.visual_unit_id,c.canonical_visual_unit_id,
                      c.derived_id,c.candidate_role,c.reason_codes,c.policy_version
               FROM stop03_3_qwenvl_supplement_results r
               JOIN stop03_3_qwenvl_supplement_candidates c USING(candidate_id)
               WHERE r.result_status='success'
               ORDER BY r.candidate_id,r.created_at,r.result_id"""
        )]
    finally:
        con.close()

    latest_supplement: dict[str, dict[str, Any]] = {}
    for row in supplement_source:
        latest_supplement[str(row["candidate_id"])] = row
    existing_evidence_ids = {str(row["evidence_id"]) for row in base_rows}
    existing_candidates = {str(row["candidate_id"]) for row in base_rows}
    created_at = utc_now()
    added_rows: list[dict[str, Any]] = []
    for candidate_id in sorted(latest_supplement):
        source = latest_supplement[candidate_id]
        if candidate_id in existing_candidates or str(source["evidence_id"]) in existing_evidence_ids:
            continue
        clean_text = str(source["clean_text"] or "").strip()
        if not clean_text:
            raise RuntimeError(f"supplement_merge_empty_success_text:{candidate_id}")
        canonical_key = sha256_text(canonical_json({
            "modality": "qwenvl",
            "evidence_id": source["evidence_id"],
            "candidate_id": candidate_id,
            "text_sha256": source["clean_text_sha256"],
            "runtime_visual_file_sha256": source["runtime_visual_file_sha256"],
        }))
        added_rows.append({
            "staging_item_id": f"stg5b_sup_{canonical_key[:28]}",
            "canonical_evidence_key": canonical_key,
            "modality": "qwenvl",
            "evidence_id": str(source["evidence_id"]),
            "candidate_id": candidate_id,
            "source_content_id": str(source["source_content_id"]),
            "visual_unit_id": str(source["visual_unit_id"]),
            "canonical_visual_unit_id": str(source["canonical_visual_unit_id"]),
            "derived_id": str(source["derived_id"]),
            "candidate_role": str(source["candidate_role"]),
            "reason_codes": str(source["reason_codes"]),
            "policy_version": str(source["policy_version"]),
            "runtime_visual_file_sha256": str(source["runtime_visual_file_sha256"]),
            "evidence_status": "success",
            "quality_status": "PASS",
            "quality_reasons": "supplement_success_contract",
            "evidence_text": clean_text,
            "evidence_text_sha256": str(source["clean_text_sha256"]),
            "evidence_attributes_json": canonical_json({
                "generation_tokens": source["generation_tokens"],
                "finish_reason": source["finish_reason"],
                "supplement_contract": "stop03_3_qwenvl_supplement_v1",
            }),
            "source_run_id": str(source["run_id"]),
            "source_result_id": str(source["result_id"]),
            "source_execution_key": str(source["execution_key"]),
            "created_at": created_at,
        })

    if not added_rows:
        return ({
            "status": "PASS", "technical_status": "PASS",
            "commit_status": "NOT_APPLICABLE_NO_NEW_SUPPLEMENT",
            "contract_version": CONTRACT_VERSION,
            "base_staging_run_id": str(base_run["staging_run_id"]),
            "supplement_evidence_added_count": 0,
            "database_write": False, "model_run": False,
            "network_used": False, "original_media_write": False,
        }, [])

    combined: list[dict[str, Any]] = []
    for row in base_rows:
        copied = {key: value for key, value in row.items() if key != "staging_run_id"}
        copied["created_at"] = created_at
        combined.append(copied)
    combined.extend(added_rows)
    combined.sort(key=lambda row: (str(row["modality"]), str(row["candidate_id"])))
    candidate_ids = [str(row["candidate_id"]) for row in combined]
    evidence_ids = [str(row["evidence_id"]) for row in combined]
    canonical_keys = [str(row["canonical_evidence_key"]) for row in combined]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError("supplement_merge_candidate_id_duplicate")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise RuntimeError("supplement_merge_evidence_id_duplicate")
    if len(canonical_keys) != len(set(canonical_keys)):
        raise RuntimeError("supplement_merge_canonical_key_duplicate")
    payload_rows = [
        {key: value for key, value in row.items() if key != "created_at"}
        for row in combined
    ]
    payload_digest = sha256_text(canonical_json(payload_rows))
    counts = Counter(str(row["modality"]) for row in combined)
    quality = Counter(str(row["quality_status"]) for row in combined)
    supplement_run_ids = sorted({str(row["source_run_id"]) for row in added_rows})
    summary = {
        "status": "PASS", "technical_status": "PASS", "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "base_staging_run_id": str(base_run["staging_run_id"]),
        "staging_run_id": f"stop03_5b_sup_{payload_digest[:20]}",
        "qwen_run_id": "supplement_merge:" + sha256_text("\n".join(supplement_run_ids))[:24],
        "ocr_run_id": str(base_run["ocr_run_id"]),
        "qwen_count": counts["qwenvl"], "ocr_count": counts["ocr"],
        "evidence_count": len(combined), "pass_count": quality["PASS"],
        "review_count": quality["REVIEW"], "fail_count": 0,
        "candidate_id_set_sha256": sha256_text("\n".join(sorted(candidate_ids))),
        "evidence_id_set_sha256": sha256_text("\n".join(sorted(evidence_ids))),
        "payload_digest_sha256": payload_digest,
        "quality_config_sha256": str(base_run["quality_config_sha256"]),
        "script_sha256": sha256_text(Path(__file__).read_text(encoding="utf-8")),
        "supplement_evidence_added_count": len(added_rows),
        "database_write": False, "model_run": False,
        "network_used": False, "original_media_write": False,
    }
    return summary, combined


def commit_merge(
    db: Path, migration: Path, summary: dict[str, Any], rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rows:
        return summary
    con = connect(db)
    try:
        con.executescript(migration.read_text(encoding="utf-8"))
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT staging_run_id,evidence_count FROM stop03_5_unified_evidence_runs WHERE payload_digest_sha256=?",
            (summary["payload_digest_sha256"],),
        ).fetchone()
        if existing is None:
            con.execute(
                """INSERT INTO stop03_5_unified_evidence_runs(
                   staging_run_id,contract_version,qwen_run_id,ocr_run_id,qwen_count,ocr_count,
                   evidence_count,pass_count,review_count,fail_count,candidate_id_set_sha256,
                   evidence_id_set_sha256,payload_digest_sha256,quality_config_sha256,
                   script_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    summary["staging_run_id"], CONTRACT_VERSION, summary["qwen_run_id"],
                    summary["ocr_run_id"], summary["qwen_count"], summary["ocr_count"],
                    summary["evidence_count"], summary["pass_count"], summary["review_count"],
                    0, summary["candidate_id_set_sha256"], summary["evidence_id_set_sha256"],
                    summary["payload_digest_sha256"], summary["quality_config_sha256"],
                    summary["script_sha256"], "success", utc_now(),
                ),
            )
            columns = (
                "staging_item_id", "staging_run_id", "canonical_evidence_key", "modality",
                "evidence_id", "candidate_id", "source_content_id", "visual_unit_id",
                "canonical_visual_unit_id", "derived_id", "candidate_role", "reason_codes",
                "policy_version", "runtime_visual_file_sha256", "evidence_status",
                "quality_status", "quality_reasons", "evidence_text", "evidence_text_sha256",
                "evidence_attributes_json", "source_run_id", "source_result_id",
                "source_execution_key", "created_at",
            )
            con.executemany(
                f"INSERT INTO stop03_5_unified_evidence_items({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [tuple(summary["staging_run_id"] if key == "staging_run_id" else row[key] for key in columns) for row in rows],
            )
            commit_status = "COMMITTED"
        else:
            if str(existing["staging_run_id"]) != summary["staging_run_id"] or int(existing["evidence_count"]) != len(rows):
                raise RuntimeError("supplement_merge_existing_payload_mismatch")
            commit_status = "IDEMPOTENT_PASS"
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("supplement_merge_database_check_failed")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {
        **summary, "commit_status": commit_status, "database_write": True,
        "database_integrity_check": integrity, "foreign_key_error_count": foreign_keys,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("plan", "commit"), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args(argv)
    db = args.db.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    summary, rows = build_merge(db)
    if args.mode == "commit" and rows:
        if not args.confirm_central_db_write:
            raise RuntimeError("supplement_merge_commit_requires_confirmation")
        summary = commit_merge(db, args.migration.resolve(strict=True), summary, rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
