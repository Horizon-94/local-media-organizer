#!/usr/bin/env python3
"""Read-only Stop03-5E generic text-search contract preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence


CONTRACT_VERSION = "stop03_5e_text_search_contract_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5e_text_search_contract_v1.json"
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "MEDIA_ARCHIVE_TEST_OUTPUT_ROOT",
        str(PROJECT_ROOT.parent / "test-output"),
    )
).expanduser()
DEFAULT_OUT = DEFAULT_OUTPUT_ROOT / "stop03_5e_text_search_contract_v1"
REQUIRED_VIEW_COLUMNS = {
    "embedding_run_id", "document_id", "source_content_id", "derived_id",
    "canonical_visual_unit_id", "media_type", "document_kind", "time_position_ms",
    "source_relative_path", "embedding_text", "embedding_text_sha256",
    "text_vector_id", "vector_status", "model_dimension", "vector_dtype", "normalized",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_version": CONTRACT_VERSION,
        "source_view": "v_stop03_5d_latest_text_documents",
        "source_run_selector": "latest_success_stop03_5d",
        "query_model_policy": "must_match_selected_embedding_run",
        "query_prompt_name": "query",
        "query_normalize_embeddings": True,
        "semantic_score": "cosine_dot_normalized_float32",
        "baseline_backend": "sqlite_blob_streaming_cosine_v1",
        "ranking_unit": "unique_text_vector_group",
        "document_pagination": True,
        "fts5_policy": "capability_only_not_indexed_in_v1",
        "result_thumbnail_required": True,
        "result_thumbnail_source": "derived_assets.derived_path",
        "result_thumbnail_asset_policy": "relative_symlink_then_readonly_copy",
        "timecode_display_format": "adaptive_hh_mm_ss",
        "timecode_default_precision": "millisecond",
        "environment_label_policy": "temporal_qwen_consensus_non_destructive_v1",
        "environment_ambiguous_display_label": "夜间/室内（待确认）",
        "environment_user_confirmation_supported": True,
        "playback_contract": "ui_resolves_source_content_id_and_seeks_preview_segment",
        "original_video_clip_generation": False,
        "query_persistence": False,
        "result_traceability": True,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
        "database_write_in_preflight_or_dry_run": False,
    }
    mismatches = {
        key: {"actual": value.get(key), "expected": expected_value}
        for key, expected_value in expected.items()
        if value.get(key) != expected_value
    }
    if mismatches:
        raise RuntimeError(
            "stop03_5e_config_mismatch:"
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    integer_keys = (
        "query_min_characters", "query_max_characters", "vector_scan_chunk_size",
        "default_vector_group_limit", "max_vector_group_limit",
        "default_documents_per_group",
        "max_documents_per_group",
        "video_preview_window_ms", "video_preview_anchor_offset_ms",
        "environment_neighbor_count_each_side",
    )
    for key in integer_keys:
        if int(value.get(key, 0)) < 1:
            raise RuntimeError(f"stop03_5e_invalid_config_integer:{key}")
    if value["query_max_characters"] < value["query_min_characters"]:
        raise RuntimeError("stop03_5e_query_length_range_invalid")
    if value["max_vector_group_limit"] < value["default_vector_group_limit"]:
        raise RuntimeError("stop03_5e_group_limit_range_invalid")
    if value["max_documents_per_group"] < value["default_documents_per_group"]:
        raise RuntimeError("stop03_5e_document_limit_range_invalid")
    if value["video_preview_anchor_offset_ms"] >= value["video_preview_window_ms"]:
        raise RuntimeError("stop03_5e_video_preview_window_invalid")
    if value.get("timecode_precision_choices") != ["second", "millisecond"]:
        raise RuntimeError("stop03_5e_timecode_precision_choices_invalid")
    if value.get("video_preview_window_options_ms") != [5000, 10000]:
        raise RuntimeError("stop03_5e_video_preview_window_options_invalid")
    expected_environment_categories = [
        "indoor", "outdoor_day", "outdoor_night", "outdoor",
        "night_or_indoor", "indoor_or_outdoor", "unknown",
    ]
    if value.get("environment_categories") != expected_environment_categories:
        raise RuntimeError("stop03_5e_environment_categories_invalid")
    return value


def object_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)
    ).fetchone() is not None


def build_preflight(db: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    db_sha_before = sha256_file(db)
    with connect_ro(db) as con:
        required = (
            "stop03_5d_text_embedding_runs", "stop03_5d_text_documents",
            "stop03_5d_text_vectors", "stop03_5d_document_vector_links",
            config["source_view"],
        )
        missing = [name for name in required if not object_exists(con, name)]
        if missing:
            raise RuntimeError(f"stop03_5e_database_objects_missing:{missing}")
        columns = {
            row["name"] for row in con.execute(
                f"PRAGMA table_info({config['source_view']})"
            )
        }
        missing_columns = sorted(REQUIRED_VIEW_COLUMNS - columns)
        if missing_columns:
            raise RuntimeError(
                f"stop03_5e_source_view_columns_missing:{missing_columns}"
            )
        run = con.execute(
            """SELECT * FROM stop03_5d_text_embedding_runs
               WHERE status='success'
               ORDER BY created_at DESC,embedding_run_id DESC LIMIT 1"""
        ).fetchone()
        if run is None:
            raise RuntimeError("stop03_5e_latest_success_embedding_run_missing")
        run_id = str(run["embedding_run_id"])
        vector_rows = list(con.execute(
            """SELECT text_vector_id,execution_key,status,model_dimension,
               vector_dtype,normalized,vector_blob,vector_byte_length,vector_sha256
               FROM stop03_5d_text_vectors WHERE embedding_run_id=?""",
            (run_id,),
        ))
        document_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_5d_text_documents WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()[0])
        link_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_5d_document_vector_links WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()[0])
        view_count = int(con.execute(
            f"SELECT COUNT(*) FROM {config['source_view']}"
        ).fetchone()[0])
        duplicate_keys = int(con.execute(
            """SELECT COUNT(*) FROM (
               SELECT execution_key FROM stop03_5d_text_vectors
               WHERE embedding_run_id=? GROUP BY execution_key HAVING COUNT(*)>1)""",
            (run_id,),
        ).fetchone()[0])
        compile_options = [row[0] for row in con.execute("PRAGMA compile_options")]
        json1_available = int(con.execute("SELECT json_valid('[]')").fetchone()[0]) == 1
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())

    expected_dimension = int(run["model_dimension"])
    expected_bytes = expected_dimension * 4
    successful = [row for row in vector_rows if row["status"] == "success"]
    invalid_blob_count = 0
    invalid_sha_count = 0
    invalid_contract_count = 0
    for row in successful:
        blob = row["vector_blob"] or b""
        invalid_blob_count += int(
            len(blob) != expected_bytes
            or len(blob) != int(row["vector_byte_length"] or 0)
        )
        invalid_sha_count += int(
            hashlib.sha256(blob).hexdigest() != row["vector_sha256"]
        )
        invalid_contract_count += int(
            int(row["model_dimension"]) != expected_dimension
            or row["vector_dtype"] != "float32"
            or int(row["normalized"]) != 1
        )
    checks = {
        "latest_run_success": run["status"] == "success",
        "document_count_matches_run": document_count == int(run["document_count"]),
        "view_count_matches_documents": view_count == document_count,
        "link_count_matches_documents": link_count == document_count,
        "vector_count_matches_run": len(vector_rows) == int(run["unique_text_count"]),
        "all_vectors_success": len(successful) == len(vector_rows),
        "vector_blob_lengths_valid": invalid_blob_count == 0,
        "vector_blob_hashes_valid": invalid_sha_count == 0,
        "vector_contract_matches": invalid_contract_count == 0,
        "execution_keys_unique": duplicate_keys == 0,
        "model_identity_present": bool(
            run["model_name"] and run["model_inventory_sha256"]
            and run["model_config_sha256"]
        ),
        "database_integrity_ok": integrity == "ok",
        "foreign_keys_ok": foreign_keys == 0,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    scan_chunk = int(config["vector_scan_chunk_size"])
    vector_count = len(vector_rows)
    summary = {
        "status": status,
        "technical_status": status,
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "selected_embedding_run_id": run_id,
        "selected_model_name": run["model_name"],
        "selected_model_inventory_sha256": run["model_inventory_sha256"],
        "selected_model_config_sha256": run["model_config_sha256"],
        "model_dimension": expected_dimension,
        "vector_dtype": run["vector_dtype"],
        "normalized": bool(run["normalize_embeddings"]),
        "observed_document_count": document_count,
        "observed_unique_vector_count": vector_count,
        "observed_link_count": link_count,
        "planned_scan_chunk_size": scan_chunk,
        "planned_scan_chunk_count": math.ceil(vector_count / scan_chunk) if vector_count else 0,
        "planned_max_chunk_vector_bytes": min(vector_count, scan_chunk) * expected_bytes,
        "baseline_backend": config["baseline_backend"],
        "fts5_available": any("ENABLE_FTS5" in value for value in compile_options),
        "fts5_index_planned": False,
        "json1_available": json1_available,
        "checks": checks,
        "invalid_blob_count": invalid_blob_count,
        "invalid_sha_count": invalid_sha_count,
        "invalid_contract_count": invalid_contract_count,
        "execution_key_duplicates": duplicate_keys,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "config_sha256": sha256_file(config_path),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "central_db_sha256_before": db_sha_before,
        "central_db_sha256_after": sha256_file(db),
        "database_write": False,
        "query_model_run": False,
        "real_search_run": False,
        "query_persisted": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }
    summary["central_db_unchanged"] = (
        summary["central_db_sha256_before"] == summary["central_db_sha256_after"]
    )
    if not summary["central_db_unchanged"]:
        summary["status"] = "FAIL"
        summary["technical_status"] = "FAIL"
    return summary


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "dry-run"), required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    summary = build_preflight(args.db, args.config)
    if args.mode == "dry-run":
        write_json(args.out / "reports/stop03_5e_text_search_contract_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
