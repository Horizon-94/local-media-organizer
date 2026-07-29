#!/usr/bin/env python3
"""Restore missing source_file_records referenced by durable lineage tables.

This is a local database repair for libraries whose older scan snapshot was
incorrectly deleted during a path/timelapse rebuild.  It reads only existing
scan manifests inside the selected library workspace.  It never reads or
modifies original media and never runs a model.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def missing_reference_columns(con: sqlite3.Connection) -> list[str]:
    return sorted({
        str(row[3])
        for row in con.execute("PRAGMA foreign_key_list(source_asset_identity)")
        if str(row[2]) == "source_file_records" and str(row[4]) == "source_file_id"
    })


def missing_reference_ids(con: sqlite3.Connection, columns: Iterable[str]) -> set[str]:
    missing: set[str] = set()
    for column in columns:
        query = (
            f"SELECT DISTINCT i.{column} "
            "FROM source_asset_identity i "
            f"LEFT JOIN source_file_records f ON f.source_file_id=i.{column} "
            f"WHERE i.{column} IS NOT NULL AND i.{column}<>'' "
            "AND f.source_file_id IS NULL"
        )
        missing.update(str(row[0]) for row in con.execute(query))
    return missing


def load_manifest_rows(manifest_root: Path, wanted: set[str]) -> tuple[dict[str, dict], list[str]]:
    rows: dict[str, dict] = {}
    manifests: list[str] = []
    for manifest in sorted(manifest_root.rglob("source_files_manifest.csv")):
        manifests.append(str(manifest))
        with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source_file_id = str(row.get("source_file_id") or "")
                if source_file_id in wanted and source_file_id not in rows:
                    rows[source_file_id] = row
        if len(rows) == len(wanted):
            break
    return rows, manifests


def source_file_values(row: dict, repair_run_id: str) -> tuple:
    return (
        row.get("source_file_id", ""),
        row.get("source_content_id", ""),
        row.get("source_path") or row.get("absolute_path") or "",
        row.get("source_relative_path") or row.get("relative_path") or "",
        row.get("source_root", ""),
        row.get("file_name", ""),
        row.get("extension", ""),
        row.get("media_kind", ""),
        row.get("support_status", ""),
        row.get("support_reason", ""),
        int(row["file_size_bytes"]) if row.get("file_size_bytes") else None,
        int(row["mtime_ns"]) if row.get("mtime_ns") else None,
        int(row["ctime_ns"]) if row.get("ctime_ns") else None,
        row.get("content_sha256", ""),
        row.get("dedup_role", ""),
        row.get("next_action", ""),
        row.get("canonical_source_file_id", ""),
        row.get("folder_path", ""),
        row.get("file_stem", ""),
        row.get("stem_key", ""),
        row.get("finder_tag_status", ""),
        row.get("finder_tags_json", "[]"),
        repair_run_id,
    )


def backup_database(db: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def restore(db: Path, manifest_root: Path, out: Path, allowed_root: Path) -> dict:
    for path, label in ((db, "db"), (manifest_root, "manifest_root"), (out, "out")):
        if not is_within(path, allowed_root):
            raise ValueError(f"{label}_outside_allowed_root:{path}")
    if not db.is_file():
        raise FileNotFoundError(f"database_not_found:{db}")
    if not manifest_root.is_dir():
        raise FileNotFoundError(f"manifest_root_not_found:{manifest_root}")

    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    try:
        columns = missing_reference_columns(con)
        if not columns:
            raise RuntimeError("source_asset_identity_source_file_fk_not_found")
        before_ids = missing_reference_ids(con, columns)
    finally:
        con.close()

    rows, manifests = load_manifest_rows(manifest_root, before_ids)
    unresolved_in_manifests = sorted(before_ids - set(rows))
    repair_run_id = "restore_lineage_" + hashlib.sha256(
        ("|".join(sorted(before_ids)) + "|" + now_iso()).encode("utf-8")
    ).hexdigest()[:20]
    backup = out / "backups" / "media_archive_before_source_lineage_restore.sqlite"
    backup_database(db, backup)

    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        for source_file_id in sorted(rows):
            con.execute(
                """
                INSERT INTO source_file_records
                (source_file_id, source_content_id, absolute_path, relative_path, source_root,
                 file_name, extension, media_kind, support_status, support_reason, size_bytes,
                 mtime_ns, ctime_ns, content_sha256, dedup_role, next_action,
                 canonical_source_file_id, folder_path, file_stem, stem_key,
                 finder_tag_status, finder_tags_json, scan_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP)
                ON CONFLICT(source_file_id) DO NOTHING
                """,
                source_file_values(rows[source_file_id], repair_run_id),
            )
        con.commit()
        after_ids = missing_reference_ids(con, columns)
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    summary = {
        "status": "PASS" if not after_ids and integrity == "ok" and not foreign_key_errors else "FAIL",
        "repair_version": "restore_source_file_lineage_from_manifest_v1",
        "repair_run_id": repair_run_id,
        "missing_reference_id_count_before": len(before_ids),
        "manifest_row_match_count": len(rows),
        "restored_source_file_record_count": len(before_ids - after_ids),
        "missing_reference_id_count_after": len(after_ids),
        "unresolved_manifest_id_count": len(unresolved_in_manifests),
        "foreign_key_error_count_after": len(foreign_key_errors),
        "database_integrity_check": integrity,
        "manifest_files_read": manifests,
        "database_backup": str(backup),
        "original_media_read": False,
        "original_media_write": False,
        "model_run": False,
        "network_used": False,
        "finished_at": now_iso(),
    }
    (out / "restore_source_file_lineage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--manifest-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--allowed-output-root", required=True, type=Path)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_central_db_write:
        raise SystemExit("central_db_write_confirmation_required")
    summary = restore(
        args.db.expanduser().resolve(),
        args.manifest_root.expanduser().resolve(),
        args.out.expanduser().resolve(),
        args.allowed_output_root.expanduser().resolve(),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
