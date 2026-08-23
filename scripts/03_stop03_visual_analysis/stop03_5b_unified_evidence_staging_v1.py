#!/usr/bin/env python3
"""Stop03-5B generic central-DB unified text-evidence staging."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import stop03_5a_joint_db_quality_audit_v1 as quality


CONTRACT_VERSION = "stop03_5b_unified_evidence_staging_v1"
PROJECT_ROOT = Path("$APP_RESOURCES/Pipeline")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5b_unified_evidence_staging_v1.json"
DEFAULT_MIGRATION = (
    PROJECT_ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql"
)
DEFAULT_OUT = Path(
    "$USER_HOME/Documents/AI-Local/test-output/"
    "stop03_5b_unified_evidence_staging_v1"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("stop03_5b_contract_version_mismatch")
    if data.get("modalities") != ["qwenvl", "ocr"]:
        raise RuntimeError("stop03_5b_modality_contract_mismatch")
    for key in ("original_video_read", "model_run", "network_used", "download_used"):
        if data.get(key) is not False:
            raise RuntimeError(f"stop03_5b_safety_policy_mismatch:{key}")
    audit_config = Path(str(data["quality_audit_config"]))
    if not audit_config.is_absolute():
        audit_config = PROJECT_ROOT / audit_config
    data["quality_audit_config_path"] = audit_config.resolve(strict=True)
    return data


def quality_map(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(row["modality"]), str(row["candidate_id"])): row
        for row in rows
    }


def build_rows(
    db: Path, config_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = load_config(config_path)
    audit_summary, audit_details = quality.run_audit(
        db, config["quality_audit_config_path"]
    )
    if audit_summary["technical_status"] != "PASS":
        raise RuntimeError("stop03_5b_quality_audit_not_technical_pass")
    if audit_summary["staging_readiness"] not in {
        "READY",
        "READY_WITH_QUALITY_FLAGS",
    }:
        raise RuntimeError("stop03_5b_quality_audit_not_ready")
    if (
        audit_summary["quality_review_item_count"]
        and not config["allow_quality_review"]
    ):
        raise RuntimeError("stop03_5b_quality_review_not_allowed")

    audited = quality_map(audit_details["qwen"] + audit_details["ocr"])
    with quality.connect_ro(db) as con:
        qwen_rows = quality.load_qwen(
            con, audit_summary["qwen_acceptance_run_id"]
        )
        ocr_rows = quality.load_ocr(
            con, audit_summary["ocr_acceptance_run_id"]
        )

    rows: list[dict[str, Any]] = []
    created_at = utc_now()
    for modality, source_rows, source_run_id, text_key, text_sha_key in (
        (
            "qwenvl",
            qwen_rows,
            audit_summary["qwen_acceptance_run_id"],
            "clean_text",
            "clean_text_sha256",
        ),
        (
            "ocr",
            ocr_rows,
            audit_summary["ocr_acceptance_run_id"],
            "ocr_text",
            "ocr_text_sha256",
        ),
    ):
        for source in source_rows:
            check = audited[(modality, str(source["candidate_id"]))]
            if check["quality_status"] == "FAIL":
                raise RuntimeError("stop03_5b_hard_fail_evidence_present")
            attributes = (
                {
                    "generation_tokens": source["generation_tokens"],
                    "attempt_count": source["attempt_count"],
                    "truncation_status": source["truncation_status"],
                    "cleanup_status": source["cleanup_status"],
                }
                if modality == "qwenvl"
                else {
                    "ocr_line_count": source["ocr_line_count"],
                    "mean_confidence": source["mean_confidence"],
                    "min_confidence": source["min_confidence"],
                    "max_confidence": source["max_confidence"],
                    "attempt_count": source["attempt_count"],
                    "reused_existing_result": source["reused_existing_result"],
                }
            )
            canonical_key = sha256_text(
                canonical_json(
                    {
                        "modality": modality,
                        "evidence_id": source["evidence_id"],
                        "candidate_id": source["candidate_id"],
                        "text_sha256": source[text_sha_key],
                        "runtime_visual_file_sha256":
                            source["runtime_visual_file_sha256"],
                    }
                )
            )
            rows.append(
                {
                    "staging_item_id": f"stg5b_{canonical_key[:32]}",
                    "canonical_evidence_key": canonical_key,
                    "modality": modality,
                    "evidence_id": source["evidence_id"],
                    "candidate_id": source["candidate_id"],
                    "source_content_id": source["source_content_id"],
                    "visual_unit_id": source["visual_unit_id"],
                    "canonical_visual_unit_id":
                        source["canonical_visual_unit_id"],
                    "derived_id": source["derived_id"],
                    "candidate_role": source["candidate_role"],
                    "reason_codes": source["reason_codes"],
                    "policy_version": source["policy_version"],
                    "runtime_visual_file_sha256":
                        source["runtime_visual_file_sha256"],
                    "evidence_status": source["result_status"],
                    "quality_status": check["quality_status"],
                    "quality_reasons": check["review_reasons"],
                    "evidence_text": source[text_key],
                    "evidence_text_sha256": source[text_sha_key],
                    "evidence_attributes_json": canonical_json(attributes),
                    "source_run_id": source_run_id,
                    "source_result_id": source["result_id"],
                    "source_execution_key": source["execution_key"],
                    "created_at": created_at,
                }
            )

    rows.sort(key=lambda row: (row["modality"], row["candidate_id"]))
    candidate_ids = [str(row["candidate_id"]) for row in rows]
    evidence_ids = [str(row["evidence_id"]) for row in rows]
    canonical_keys = [str(row["canonical_evidence_key"]) for row in rows]
    payload_rows = [
        {key: value for key, value in row.items() if key != "created_at"}
        for row in rows
    ]
    payload_digest = sha256_text(canonical_json(payload_rows))
    counts = Counter(row["quality_status"] for row in rows)
    modality_counts = Counter(row["modality"] for row in rows)
    expected_count = (
        audit_summary["qwen_selected_result_count"]
        + audit_summary["ocr_selected_result_count"]
    )
    checks = {
        "audit_technical_pass": audit_summary["technical_status"] == "PASS",
        "audit_staging_ready": audit_summary["staging_readiness"]
        in {"READY", "READY_WITH_QUALITY_FLAGS"},
        "row_count_matches_selected_evidence": len(rows) == expected_count,
        "candidate_ids_unique": len(candidate_ids) == len(set(candidate_ids)),
        "evidence_ids_unique": len(evidence_ids) == len(set(evidence_ids)),
        "canonical_evidence_keys_unique":
            len(canonical_keys) == len(set(canonical_keys)),
        "no_fail_quality_rows": counts["FAIL"] == 0,
        "qwen_count_matches_audit":
            modality_counts["qwenvl"]
            == audit_summary["qwen_selected_result_count"],
        "ocr_count_matches_audit":
            modality_counts["ocr"]
            == audit_summary["ocr_selected_result_count"],
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    summary = {
        "status": status,
        "technical_status": status,
        "policy_status": "REVIEW" if counts["REVIEW"] else "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "qwen_run_id": audit_summary["qwen_acceptance_run_id"],
        "ocr_run_id": audit_summary["ocr_acceptance_run_id"],
        "qwen_count": modality_counts["qwenvl"],
        "ocr_count": modality_counts["ocr"],
        "evidence_count": len(rows),
        "pass_count": counts["PASS"],
        "review_count": counts["REVIEW"],
        "fail_count": counts["FAIL"],
        "candidate_id_set_sha256": sha256_text("\n".join(sorted(candidate_ids))),
        "evidence_id_set_sha256": sha256_text("\n".join(sorted(evidence_ids))),
        "payload_digest_sha256": payload_digest,
        "staging_run_id": f"stop03_5b_{payload_digest[:24]}",
        "quality_config_sha256":
            quality.sha256_file(config["quality_audit_config_path"]),
        "script_sha256": quality.sha256_file(Path(__file__).resolve()),
        "checks": checks,
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }
    return summary, rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "modality",
        "evidence_id",
        "candidate_id",
        "source_content_id",
        "visual_unit_id",
        "canonical_visual_unit_id",
        "derived_id",
        "quality_status",
        "quality_reasons",
        "evidence_status",
        "source_run_id",
        "source_result_id",
        "evidence_text_sha256",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_dry_run(out: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    write_json(out / "reports/stop03_5b_summary.json", summary)
    write_jsonl(out / "manifests/unified_evidence.jsonl", rows)
    write_csv(out / "manifests/unified_evidence_index.csv", rows)


def backup_database(db: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def commit(
    db: Path,
    migration: Path,
    out: Path,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if summary["technical_status"] != "PASS":
        raise RuntimeError("stop03_5b_commit_requires_technical_pass")
    backup = out / "backups" / f"{db.name}.{utc_now().replace(':', '')}.bak"
    backup_database(db, backup)
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + migration.read_text(encoding="utf-8"))
        existing = con.execute(
            """SELECT * FROM stop03_5_unified_evidence_runs
               WHERE payload_digest_sha256=?""",
            (summary["payload_digest_sha256"],),
        ).fetchone()
        if existing is None:
            now = utc_now()
            con.execute(
                """INSERT INTO stop03_5_unified_evidence_runs(
                   staging_run_id,contract_version,qwen_run_id,ocr_run_id,
                   qwen_count,ocr_count,evidence_count,pass_count,review_count,
                   fail_count,candidate_id_set_sha256,evidence_id_set_sha256,
                   payload_digest_sha256,quality_config_sha256,script_sha256,
                   status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    summary["staging_run_id"],
                    CONTRACT_VERSION,
                    summary["qwen_run_id"],
                    summary["ocr_run_id"],
                    summary["qwen_count"],
                    summary["ocr_count"],
                    summary["evidence_count"],
                    summary["pass_count"],
                    summary["review_count"],
                    summary["fail_count"],
                    summary["candidate_id_set_sha256"],
                    summary["evidence_id_set_sha256"],
                    summary["payload_digest_sha256"],
                    summary["quality_config_sha256"],
                    summary["script_sha256"],
                    "success",
                    now,
                ),
            )
            columns = (
                "staging_item_id",
                "staging_run_id",
                "canonical_evidence_key",
                "modality",
                "evidence_id",
                "candidate_id",
                "source_content_id",
                "visual_unit_id",
                "canonical_visual_unit_id",
                "derived_id",
                "candidate_role",
                "reason_codes",
                "policy_version",
                "runtime_visual_file_sha256",
                "evidence_status",
                "quality_status",
                "quality_reasons",
                "evidence_text",
                "evidence_text_sha256",
                "evidence_attributes_json",
                "source_run_id",
                "source_result_id",
                "source_execution_key",
                "created_at",
            )
            con.executemany(
                f"""INSERT INTO stop03_5_unified_evidence_items(
                    {','.join(columns)}) VALUES({','.join('?' for _ in columns)})""",
                [
                    tuple(
                        summary["staging_run_id"]
                        if column == "staging_run_id"
                        else row[column]
                        for column in columns
                    )
                    for row in rows
                ],
            )
            commit_status = "COMMITTED"
        else:
            if (
                existing["staging_run_id"] != summary["staging_run_id"]
                or existing["evidence_count"] != summary["evidence_count"]
            ):
                raise RuntimeError("stop03_5b_existing_payload_contract_mismatch")
            commit_status = "IDEMPOTENT_PASS"
        con.commit()
        count = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_5_unified_evidence_items
                   WHERE staging_run_id=?""",
                (summary["staging_run_id"],),
            ).fetchone()[0]
        )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(list(con.execute("PRAGMA foreign_key_check")))
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    if count != summary["evidence_count"] or integrity != "ok" or foreign_keys:
        raise RuntimeError("stop03_5b_commit_readback_failure")
    return {
        **summary,
        "status": "PASS",
        "commit_status": commit_status,
        "database_write": True,
        "backup_path": str(backup),
        "readback_evidence_count": count,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "dry-run", "commit"), required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_sha_before = quality.sha256_file(args.db)
    summary, rows = build_rows(args.db, args.config)
    if args.mode == "preflight":
        summary["central_db_sha256"] = db_sha_before
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["technical_status"] == "PASS" else 2
    if args.mode == "dry-run":
        db_sha_after = quality.sha256_file(args.db)
        summary["central_db_sha256_before"] = db_sha_before
        summary["central_db_sha256_after"] = db_sha_after
        summary["central_db_unchanged"] = db_sha_before == db_sha_after
        write_dry_run(args.out, summary, rows)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["technical_status"] == "PASS" else 2
    if not args.confirm_central_db_write:
        raise RuntimeError("stop03_5b_commit_confirmation_required")
    report = commit(args.db, args.migration, args.out, summary, rows)
    write_json(args.out / "reports/stop03_5b_commit_summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
