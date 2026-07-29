#!/usr/bin/env python3
"""Prepare the generic Stop03 visual-label database contract.

Step01/02 create source, derived, visual-unit, embedding and model-run tables.
The historical project database already contained ``visual_labels`` before
YOLOE was run, but a new desktop library does not.  This migration supplies
that missing generic contract without changing the frozen YOLOE stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCRIPT_VERSION = "prepare_visual_analysis_schema_v1"
REQUIRED_COLUMNS = {
    "label_id", "visual_unit_id", "source_content_id", "label", "confidence",
    "bbox", "model_name", "model_path", "text_encoder_asset", "run_id", "created_at",
}
MODEL_RUN_REQUIRED_COLUMNS = {
    "run_id", "stage", "model_name", "model_path", "script_version", "script_path",
    "input_count", "output_count", "status", "started_at", "finished_at", "error_message",
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


def backup_database_once(db: Path, out: Path) -> Path:
    target = out / "backups" / "database_before_visual_schema.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    source = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("visual_schema_database_backup_failed")
    return target


def prepare_schema(
    db: Path,
    out: Path,
    allowed_output_root: Path,
    timelapse_manifest_path: Path | None = None,
) -> dict[str, Any]:
    db = db.expanduser().resolve(strict=True)
    out = out.expanduser().resolve()
    allowed = allowed_output_root.expanduser().resolve(strict=True)
    if not within(db, allowed) or not within(out, allowed):
        raise RuntimeError("visual_schema_path_outside_selected_library")
    out.mkdir(parents=True, exist_ok=True)
    backup_path = backup_database_once(db, out)
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        existing_tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_base = sorted({"source_assets", "visual_units"} - existing_tables)
        if missing_base:
            raise RuntimeError("visual_schema_missing_base_tables:" + ",".join(missing_base))
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS model_runs (
                run_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_path TEXT NOT NULL,
                script_version TEXT NOT NULL DEFAULT '',
                script_path TEXT NOT NULL,
                input_count INTEGER NOT NULL DEFAULT 0,
                output_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME,
                error_message TEXT
            )
            """
        )
        model_run_columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(model_runs)")
        }
        added_model_run_columns: list[str] = []
        if "script_version" not in model_run_columns:
            con.execute(
                "ALTER TABLE model_runs ADD COLUMN script_version TEXT NOT NULL DEFAULT ''"
            )
            model_run_columns.add("script_version")
            added_model_run_columns.append("script_version")
        missing_model_run_columns = sorted(MODEL_RUN_REQUIRED_COLUMNS - model_run_columns)
        if missing_model_run_columns:
            raise RuntimeError(
                "visual_schema_model_runs_missing_columns:" + ",".join(missing_model_run_columns)
            )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_labels (
                label_id INTEGER PRIMARY KEY AUTOINCREMENT,
                visual_unit_id TEXT NOT NULL,
                source_content_id TEXT NOT NULL,
                label TEXT NOT NULL,
                confidence REAL NOT NULL,
                bbox TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_path TEXT NOT NULL,
                text_encoder_asset TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (visual_unit_id) REFERENCES visual_units(visual_unit_id) ON DELETE CASCADE,
                FOREIGN KEY (source_content_id) REFERENCES source_assets(source_content_id) ON DELETE CASCADE
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_labels_vu ON visual_labels(visual_unit_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_labels_name ON visual_labels(label)")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS step02_image_timelapse_keyframes (
                visual_unit_id TEXT PRIMARY KEY,
                preview_role TEXT NOT NULL,
                sequence_id TEXT NOT NULL,
                representative_position TEXT NOT NULL,
                source_relative_path TEXT,
                visual_file TEXT,
                parent_source_content_id TEXT,
                preview_artifact_id TEXT,
                producer_step TEXT,
                producer_version TEXT,
                source_manifest_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_step02_tl_seq "
            "ON step02_image_timelapse_keyframes(sequence_id, representative_position)"
        )
        timelapse_manifest = (
            timelapse_manifest_path.expanduser().resolve()
            if timelapse_manifest_path is not None
            else allowed / "stages/02_image_preview/manifests/image_preview_visual_unit_manifest.csv"
        )
        if not within(timelapse_manifest, allowed):
            raise RuntimeError(
                f"timelapse_manifest_outside_selected_library:{timelapse_manifest}"
            )
        timelapse_rows_imported = 0
        if timelapse_manifest.is_file():
            # The manifest is a complete result of the current Step02 pass.
            # Replacing this small routing table prevents obsolete groups from
            # accumulating after users merge or move timelapse folders.
            con.execute("DELETE FROM step02_image_timelapse_keyframes")
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with timelapse_manifest.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    sequence_id = str(row.get("sequence_id") or "").strip()
                    visual_unit_id = str(row.get("visual_unit_id") or "").strip()
                    visual_file = Path(str(row.get("visual_file") or "")).expanduser().resolve()
                    if not sequence_id or not visual_unit_id:
                        continue
                    if not within(visual_file, allowed) or not visual_file.is_file():
                        raise RuntimeError(
                            f"timelapse_derived_visual_outside_selected_library:{visual_file}"
                        )
                    con.execute(
                        """INSERT OR REPLACE INTO step02_image_timelapse_keyframes(
                           visual_unit_id,preview_role,sequence_id,representative_position,
                           source_relative_path,visual_file,parent_source_content_id,
                           preview_artifact_id,producer_step,producer_version,
                           source_manifest_path,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            visual_unit_id,
                            str(row.get("preview_role") or "timelapse_keyframe"),
                            sequence_id,
                            str(row.get("representative_position") or ""),
                            str(row.get("source_relative_path") or ""),
                            str(visual_file),
                            str(row.get("parent_source_content_id") or ""),
                            str(row.get("preview_artifact_id") or ""),
                            str(row.get("producer_step") or "step02_2_image_preview"),
                            str(row.get("producer_version") or ""),
                            str(timelapse_manifest),
                            created_at,
                        ),
                    )
                    timelapse_rows_imported += 1
        columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(visual_labels)")
        }
        missing_columns = sorted(REQUIRED_COLUMNS - columns)
        if missing_columns:
            raise RuntimeError("visual_schema_missing_columns:" + ",".join(missing_columns))
        con.commit()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = len(con.execute("PRAGMA foreign_key_check").fetchall())
        timelapse_keyframe_count = int(
            con.execute("SELECT COUNT(*) FROM step02_image_timelapse_keyframes").fetchone()[0]
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    report = {
        "status": "PASS" if integrity == "ok" and foreign_key_errors == 0 else "FAIL",
        "script_version": SCRIPT_VERSION,
        "visual_labels_columns": sorted(columns),
        "model_runs_columns": sorted(model_run_columns),
        "model_runs_columns_added": added_model_run_columns,
        "timelapse_keyframe_count": timelapse_keyframe_count,
        "timelapse_rows_imported": timelapse_rows_imported,
        "timelapse_manifest_path": str(timelapse_manifest),
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_key_errors,
        "database_backup_path": str(backup_path),
        "fixed_input_count": False,
        "original_media_read": False,
        "model_run": False,
        "network_used": False,
    }
    atomic_json(out / "reports/visual_analysis_schema_summary.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--timelapse-manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = prepare_schema(
        args.db, args.out, args.allowed_output_root, args.timelapse_manifest,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
