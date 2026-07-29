#!/usr/bin/env python3
"""Append-only Qwen-VL supplement candidate contract for an existing library.

The frozen V25 snapshot is never edited.  This adapter consumes a fresh V25
dry-run manifest and records only image candidates which do not already have a
successful Qwen-VL result.  It performs no model inference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


CONTRACT_VERSION = "stop03_3_qwenvl_supplement_v1"
ALL_IMAGE_POLICY_VERSION = "stop03_3_all_image_scope_v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def select_missing_image_candidates(
    db: Path, rows: Iterable[dict[str, Any]], allowed_output_root: Path,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        existing = {
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT visual_unit_id FROM stop03_3_qwenvl_results "
                "WHERE result_status='success'"
            )
        }
        known = {
            str(row[0])
            for row in con.execute(
                "SELECT visual_unit_id FROM visual_units"
            )
        }
    finally:
        con.close()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        if str(source.get("queue_type") or "") != "qwenvl_high_value":
            continue
        if str(source.get("media_type") or "") != "image":
            continue
        visual = str(source.get("visual_unit_id") or "")
        if not visual or visual in existing or visual in seen:
            continue
        if visual not in known:
            raise RuntimeError(f"supplement_visual_unit_missing:{visual}")
        runtime = Path(str(source.get("visual_file") or "")).expanduser().resolve(strict=True)
        if not within(runtime, allowed_output_root):
            raise RuntimeError(f"supplement_runtime_outside_library:{runtime}")
        selected.append({
            "candidate_id": str(source["candidate_id"]),
            "source_content_id": str(source["source_content_id"]),
            "visual_unit_id": visual,
            "canonical_visual_unit_id": str(source.get("canonical_visual_unit_id") or visual),
            "derived_id": str(source["derived_id"]),
            "candidate_role": str(source.get("candidate_role") or source.get("high_value_category") or "image_high_value_supplement"),
            "reason_codes": str(source.get("reason_codes") or ""),
            "policy_version": str(source.get("policy_version") or "stop03_2_generic_high_value_policy_v25"),
            "media_type": "image",
            "source_relative_path": str(source.get("source_relative_path") or ""),
            "runtime_visual_file": str(runtime),
            "runtime_visual_file_sha256": sha256_file(runtime),
            "candidate_score": float(source.get("candidate_score") or 0.0),
            "source_candidate_run_id": str(source.get("run_id") or ""),
        })
        seen.add(visual)
    selected.sort(key=lambda row: row["candidate_id"])
    return selected


def select_all_image_visual_units(
    db: Path, allowed_output_root: Path,
) -> list[dict[str, Any]]:
    """Select every canonical image visual not already handled by frozen V25.

    The count is derived entirely from the current library database.  No
    project count or percentage cap is used.  Frozen V25 rows remain untouched;
    this function only creates candidates for its append-only supplement queue.
    """
    allowed_output_root = allowed_output_root.expanduser().resolve(strict=True)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        objects = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "canonical_visual_units_for_heavy" not in objects:
            raise RuntimeError("all_image_scope_requires_canonical_visual_units")
        frozen = {
            str(row[0]) for row in con.execute(
                "SELECT visual_unit_id FROM stop03_2_candidate_queue_frozen_v25 "
                "WHERE queue_type='qwenvl_high_value'"
            )
        } if "stop03_2_candidate_queue_frozen_v25" in objects else set()
        successful = {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT visual_unit_id FROM stop03_3_qwenvl_results "
                "WHERE result_status='success'"
            )
        } if "stop03_3_qwenvl_results" in objects else set()
        supplemented = {
            str(row[0]) for row in con.execute(
                "SELECT visual_unit_id FROM stop03_3_qwenvl_supplement_candidates"
            )
        } if "stop03_3_qwenvl_supplement_candidates" in objects else set()
        rows = [dict(row) for row in con.execute(
            """SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,
                      vu.visual_file,s.relative_path
               FROM canonical_visual_units_for_heavy vu
               JOIN source_assets s ON s.source_content_id=vu.source_content_id
               WHERE s.media_type='image'
                 AND COALESCE(s.is_deleted_or_missing,0)=0
               ORDER BY s.relative_path,vu.visual_unit_id"""
        )]
    finally:
        con.close()

    selected: list[dict[str, Any]] = []
    excluded = frozen | successful | supplemented
    for source in rows:
        visual = str(source["visual_unit_id"])
        if visual in excluded:
            continue
        runtime = Path(str(source["visual_file"])).expanduser().resolve(strict=True)
        if not within(runtime, allowed_output_root):
            raise RuntimeError(f"supplement_runtime_outside_library:{runtime}")
        candidate_id = "cand_allimg_" + sha256_text(
            f"{ALL_IMAGE_POLICY_VERSION}|{visual}"
        )[:24]
        selected.append({
            "candidate_id": candidate_id,
            "source_content_id": str(source["source_content_id"]),
            "visual_unit_id": visual,
            "canonical_visual_unit_id": visual,
            "derived_id": str(source["derived_id"]),
            "candidate_role": "image_all_scope_supplement",
            "reason_codes": "user_selected_all_images",
            "policy_version": ALL_IMAGE_POLICY_VERSION,
            "media_type": "image",
            "source_relative_path": str(source["relative_path"]),
            "runtime_visual_file": str(runtime),
            "runtime_visual_file_sha256": sha256_file(runtime),
            "candidate_score": 1.0,
            "source_candidate_run_id": ALL_IMAGE_POLICY_VERSION,
        })
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def commit_candidates(db: Path, migration: Path, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.execute("PRAGMA foreign_keys=ON")
    created_at = utc_now()
    inserted = 0
    try:
        con.executescript(migration.read_text(encoding="utf-8"))
        con.execute("BEGIN IMMEDIATE")
        for row in rows:
            cursor = con.execute(
                """INSERT OR IGNORE INTO stop03_3_qwenvl_supplement_candidates
                (candidate_id,source_content_id,visual_unit_id,canonical_visual_unit_id,
                 derived_id,candidate_role,reason_codes,policy_version,media_type,
                 source_relative_path,runtime_visual_file,runtime_visual_file_sha256,
                 candidate_score,source_candidate_run_id,contract_version,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["candidate_id"], row["source_content_id"], row["visual_unit_id"],
                    row["canonical_visual_unit_id"], row["derived_id"], row["candidate_role"],
                    row["reason_codes"], row["policy_version"], "image",
                    row["source_relative_path"], row["runtime_visual_file"],
                    row["runtime_visual_file_sha256"], row["candidate_score"],
                    row["source_candidate_run_id"], CONTRACT_VERSION, created_at,
                ),
            )
            inserted += int(cursor.rowcount == 1)
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("supplement_candidate_database_check_failed")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"inserted_count": inserted, "database_integrity_check": integrity, "foreign_key_error_count": foreign_keys}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("plan", "commit"), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--selection-mode", choices=("frozen-missing-images", "all-image-visual-units"),
        default="frozen-missing-images",
    )
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args(argv)
    db = args.db.expanduser().resolve(strict=True)
    allowed = args.allowed_output_root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    if args.selection_mode == "all-image-visual-units":
        rows = select_all_image_visual_units(db, allowed)
    else:
        if args.candidate_manifest is None:
            raise RuntimeError("frozen_missing_images_requires_candidate_manifest")
        manifest = args.candidate_manifest.expanduser().resolve(strict=True)
        rows = select_missing_image_candidates(db, read_jsonl(manifest), allowed)
    result: dict[str, Any] = {
        "status": "PASS", "contract_version": CONTRACT_VERSION,
        "selection_mode": args.selection_mode,
        "candidate_count": len(rows), "database_write": False,
        "frozen_v25_modified": False, "existing_success_reexecuted": 0,
        "model_run": False, "network_used": False, "original_media_write": False,
    }
    if args.mode == "commit":
        if not args.confirm_central_db_write:
            raise RuntimeError("supplement_commit_requires_confirmation")
        result.update(commit_candidates(db, args.migration.resolve(strict=True), rows))
        result["database_write"] = True
    (out / "supplement_candidates.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
