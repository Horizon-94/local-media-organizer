#!/usr/bin/env python3
"""Rebuild database-only search entry points without reading source media.

The current search engine scans persisted visual/text embeddings and evidence
tables.  This maintenance stage backs up the database, restores only missing
SQLite lookup indexes, verifies searchable evidence counts, and emits a
manifest.  It never calls a model and never opens an original media file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence


CONTRACT = "media_archive_database_search_rebuild_v1"
INDEX_SPECS = {
    "idx_search_source_assets_content": ("source_assets", ("source_content_id",)),
    "idx_search_derived_source": ("derived_assets", ("source_content_id",)),
    "idx_search_visual_source": ("visual_units", ("source_content_id",)),
    "idx_search_visual_derived": ("visual_units", ("derived_id",)),
    "idx_search_embedding_visual": ("embeddings", ("visual_unit_id",)),
    "idx_search_labels_visual_label": ("visual_labels", ("visual_unit_id", "label")),
    "idx_search_text_document": ("text_embeddings", ("document_id",)),
}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}


def backup_database_once(database: Path, out: Path) -> Path:
    target = out / "backups" / "media_archive_before_search_rebuild.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    source = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(str(target), timeout=30)
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]) != "ok":
            raise RuntimeError("search_rebuild_backup_quick_check_failed")
    finally:
        destination.close()
        source.close()
    return target


def rebuild(database: Path, out: Path, allowed_root: Path) -> dict[str, Any]:
    database = database.expanduser().resolve(strict=True)
    out = out.expanduser().resolve()
    allowed_root = allowed_root.expanduser().resolve(strict=True)
    if not within(database, allowed_root) or not within(out, allowed_root):
        raise RuntimeError("search_rebuild_path_outside_selected_library")
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    backup = backup_database_once(database, out)
    created: list[str] = []
    retained: list[str] = []
    counts: dict[str, int] = {}
    with sqlite3.connect(str(database), timeout=60) as con:
        con.execute("PRAGMA foreign_keys=ON")
        tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"source_assets", "derived_assets", "visual_units", "embeddings"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("search_rebuild_required_tables_missing:" + ",".join(missing))
        before = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        con.execute("BEGIN IMMEDIATE")
        try:
            for index_name, (table, columns) in INDEX_SPECS.items():
                if table not in tables or not set(columns).issubset(table_columns(con, table)):
                    continue
                column_sql = ",".join(f'"{column}"' for column in columns)
                con.execute(
                    f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ({column_sql})'
                )
                (retained if index_name in before else created).append(index_name)
            con.commit()
        except Exception:
            con.rollback()
            raise
        for table in (
            "source_assets", "derived_assets", "visual_units", "embeddings",
            "visual_labels", "stop03_3_qwenvl_results", "stop03_4_ocr_results",
            "text_embedding_documents", "text_embeddings",
        ):
            if table in tables:
                counts[table] = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        quick = str(con.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    searchable = counts.get("visual_units", 0) > 0 and counts.get("embeddings", 0) > 0
    report = {
        "contract": CONTRACT,
        "status": "PASS" if quick == "ok" and foreign_keys == 0 and searchable else "FAIL",
        "database": str(database),
        "database_backup": str(backup),
        "created_indexes": created,
        "retained_indexes": retained,
        "evidence_counts": counts,
        "quick_check": quick,
        "foreign_key_error_count": foreign_keys,
        "searchable_evidence_ready": searchable,
        "source_media_read": False,
        "model_run": False,
        "network_used": False,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    atomic_json(out / "reports/search_rebuild_summary.json", report)
    print("stage_progress=" + json.dumps({
        "contract": "media_archive_stage_runtime_contract_v1",
        "completed": 1, "total": 1,
        "success": int(report["status"] == "PASS"),
        "failed": int(report["status"] != "PASS"),
        "remaining": 0,
        "current_item": "database_search_entry",
        "output_files": 2,
    }, ensure_ascii=False), flush=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_central_db_write:
        raise RuntimeError("search_rebuild_database_write_not_confirmed")
    report = rebuild(args.db, args.out, args.allowed_output_root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
