#!/usr/bin/env python3
"""Read-only hybrid search over every OpenCLIP-covered visual unit."""

from __future__ import annotations

import argparse
import fcntl
import gc
import hashlib
import html
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover - compatibility fallback
    np = None


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5e_text_search_contract_v1 as text_contract
import stop03_5e_text_search_smoke_v1 as text_search


CONTRACT_VERSION = "stop03_5e_hybrid_visual_text_search_v2"
NATIVE_RESULT_CONTRACT_VERSION = "media_archive_search_result_v1"
SEARCH_PROGRESS_PREFIX = "SEARCH_PROGRESS_JSON="
SEARCH_WORKER_PROTOCOL_VERSION = "media_archive_query_worker_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5e_hybrid_visual_text_search_v2.json"
DEFAULT_OUT = text_contract.DEFAULT_OUTPUT_ROOT / CONTRACT_VERSION
VisualEmbedder = Callable[
    [Path, str, str, str, str], tuple[list[float], dict[str, Any]]
]
TextEmbedder = Callable[
    [Path, list[str], str, str], tuple[list[list[float]], dict[str, Any]]
]


def emit_search_progress(
    stage: str,
    stage_index: int,
    total_stages: int,
    message: str,
    *,
    completed: Optional[int] = None,
    total: Optional[int] = None,
    detail: str = "",
    started: Optional[float] = None,
) -> None:
    """Emit privacy-safe, machine-readable progress for the native UI.

    Query text and query vectors are intentionally excluded.  The native bridge
    forwards only these prefixed records while keeping ordinary subprocess
    output in its bounded local log.
    """
    payload: dict[str, Any] = {
        "contract": "media_archive_search_progress_v1",
        "stage": stage,
        "stage_index": max(1, int(stage_index)),
        "total_stages": max(1, int(total_stages)),
        "message": str(message),
        "detail": str(detail),
    }
    if completed is not None:
        payload["completed"] = max(0, int(completed))
    if total is not None:
        payload["total"] = max(0, int(total))
    if started is not None:
        payload["elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 3)
    print(
        SEARCH_PROGRESS_PREFIX
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_version": CONTRACT_VERSION,
        "visual_scope": "all_visual_units",
        "visual_vector_selector": "latest_complete_success_openclip_run",
        "visual_vector_payload_scheme": "jsonl",
        "text_vector_selector": "latest_success_stop03_5d",
        "object_label_source": "visual_labels_with_visual_label_terms",
        "fusion_method": "weighted_reciprocal_rank_fusion_v1",
        "query_persistence": False,
        "database_write": False,
        "search_index_created": False,
        "original_media_read": False,
        "original_video_clip_generation": False,
        "result_thumbnail_asset_policy": "relative_symlink_then_readonly_copy",
    }
    bad = {
        key: {"actual": value.get(key), "expected": expected_value}
        for key, expected_value in expected.items()
        if value.get(key) != expected_value
    }
    if bad:
        raise RuntimeError(
            "stop03_5e_v2_config_mismatch:"
            + json.dumps(bad, ensure_ascii=False, sort_keys=True)
        )
    for key in (
        "rrf_k", "query_min_characters", "query_max_characters",
        "default_result_limit", "max_result_limit",
        "video_preview_anchor_offset_ms", "environment_neighbor_count_each_side",
    ):
        if int(value.get(key, 0)) < 1:
            raise RuntimeError(f"stop03_5e_v2_config_integer_invalid:{key}")
    for key in ("visual_rank_weight", "text_rank_weight", "yoloe_rank_weight"):
        if float(value.get(key, 0)) <= 0:
            raise RuntimeError(f"stop03_5e_v2_config_weight_invalid:{key}")
    for key in (
        "minimum_visual_cosine", "minimum_text_semantic_cosine",
        "minimum_combined_visual_cosine", "minimum_combined_text_semantic_cosine",
        "minimum_object_label_confidence",
        "minimum_object_support_visual_cosine",
        "minimum_object_support_text_cosine",
    ):
        threshold = float(value.get(key, -2))
        if not -1.0 <= threshold <= 1.0:
            raise RuntimeError(f"stop03_5e_v2_config_threshold_invalid:{key}")
    if value["max_result_limit"] < value["default_result_limit"]:
        raise RuntimeError("stop03_5e_v2_result_limit_range_invalid")
    if value["video_preview_window_options_ms"] != [5000, 10000]:
        raise RuntimeError("stop03_5e_v2_preview_windows_invalid")
    if value["timecode_precision_choices"] != ["second", "millisecond"]:
        raise RuntimeError("stop03_5e_v2_timecode_choices_invalid")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def readonly_database_identity(path: Path) -> tuple[int, int]:
    """Cheap mutation guard for interactive read-only searches.

    The task completion gate already performs the expensive integrity and hash
    audits. Re-hashing a multi-GB SQLite file several times for every query made
    an otherwise sub-second warm search take tens of seconds. The query opens
    SQLite in mode=ro with query_only enabled, while this identity detects an
    unexpected concurrent replacement or write during the request.
    """
    stat = path.stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


def connect_ro(db: Path) -> sqlite3.Connection:
    # The immutable fallback is only used for an existing, completed database
    # when its external/APFS volume rejects SQLite's shared-lock sidecar.
    path = Path(db).expanduser().resolve(strict=True)
    errors: list[str] = []
    for suffix in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"{path.as_uri()}?{suffix}", uri=True, timeout=30.0)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return con
        except sqlite3.Error as exc:
            errors.append(str(exc))
    raise sqlite3.OperationalError("readonly_database_open_failed:" + " | ".join(errors))


def object_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)
    ).fetchone() is not None


def table_columns(con: sqlite3.Connection, name: str) -> set[str]:
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({name})")}


def select_complete_openclip_run(
    con: sqlite3.Connection,
) -> tuple[dict[str, Any], int]:
    visual_count = int(con.execute("SELECT COUNT(*) FROM visual_units").fetchone()[0])
    runs = con.execute(
        """SELECT * FROM model_runs
           WHERE stage='stop03_1b_openclip_visual_embedding' AND status='success'
           ORDER BY started_at DESC,run_id DESC"""
    ).fetchall()
    for row in runs:
        run_id = str(row["run_id"])
        count, distinct_count = con.execute(
            "SELECT COUNT(*),COUNT(DISTINCT visual_unit_id) FROM embeddings WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if (
            int(row["input_count"]) == int(row["output_count"])
            and int(count) == visual_count
            and int(distinct_count) == visual_count
        ):
            return dict(row), visual_count
    raise RuntimeError("stop03_5e_v2_complete_openclip_run_missing")


def parse_vector_key(
    value: str, path_availability: Optional[dict[Path, bool]] = None,
) -> tuple[Path, str]:
    if not value.startswith("jsonl:") or "#" not in value:
        raise RuntimeError(f"stop03_5e_v2_vector_key_invalid:{value}")
    path_text, embedding_id = value[6:].rsplit("#", 1)
    path = Path(path_text).expanduser()
    if path_availability is None:
        path_exists = path.is_file()
    elif path in path_availability:
        path_exists = path_availability[path]
    else:
        path_exists = path.is_file()
        path_availability[path] = path_exists
    if path.suffix.lower() != ".jsonl" or not path_exists or not embedding_id:
        raise RuntimeError(f"stop03_5e_v2_vector_payload_missing:{path}")
    return path, embedding_id


def _visual_cache_paths() -> tuple[Path, Path, Path] | None:
    configured = str(os.environ.get("MEDIA_ARCHIVE_SEARCH_DATA_CACHE") or "").strip()
    if not configured or np is None:
        return None
    root = Path(configured).expanduser().absolute() / "current_visual"
    return root / "metadata.json", root / "visual_ids.json", root / "vectors.npy"


def _visual_cache_identity(
    run_id: str, db_rows: Sequence[Mapping[str, Any]], paths: set[Path], dimension: int,
) -> dict[str, Any]:
    payloads = []
    for path in sorted(paths):
        stat = path.stat()
        payloads.append({
            "path": str(path.absolute()), "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        })
    return {
        "contract": "media_archive_visual_search_data_cache_v1",
        "run_id": run_id, "vector_count": len(db_rows), "dimension": dimension,
        "payloads": payloads,
    }


def _load_visual_cache(
    identity: Mapping[str, Any], expected_visual_ids: set[str],
) -> tuple[dict[str, Sequence[float]], dict[str, Any]] | None:
    paths = _visual_cache_paths()
    if paths is None:
        return None
    metadata_path, ids_path, vectors_path = paths
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata != dict(identity):
            return None
        visual_ids = json.loads(ids_path.read_text(encoding="utf-8"))
        matrix = np.load(vectors_path, mmap_mode="r", allow_pickle=False)
        if (
            len(visual_ids) != int(identity["vector_count"])
            or matrix.shape != (int(identity["vector_count"]), int(identity["dimension"]))
            or set(map(str, visual_ids)) != expected_visual_ids
        ):
            return None
        return (
            {str(visual_id): matrix[index] for index, visual_id in enumerate(visual_ids)},
            {"runtime_cache_hit": True, "runtime_cache_path": str(vectors_path)},
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_visual_cache(
    identity: Mapping[str, Any], vectors: Mapping[str, Sequence[float]],
) -> str:
    paths = _visual_cache_paths()
    if paths is None or np is None:
        return ""
    metadata_path, ids_path, vectors_path = paths
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    visual_ids = sorted(vectors)
    matrix = np.asarray([vectors[visual_id] for visual_id in visual_ids], dtype=np.float32)
    suffix = f".{os.getpid()}.tmp"
    metadata_tmp = metadata_path.with_name(metadata_path.name + suffix)
    ids_tmp = ids_path.with_name(ids_path.name + suffix)
    vectors_tmp = vectors_path.with_name(vectors_path.name + suffix)
    ids_tmp.write_text(json.dumps(visual_ids, separators=(",", ":")), encoding="utf-8")
    with vectors_tmp.open("wb") as handle:
        np.save(handle, matrix, allow_pickle=False)
    metadata_tmp.write_text(
        json.dumps(dict(identity), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(ids_tmp, ids_path)
    os.replace(vectors_tmp, vectors_path)
    os.replace(metadata_tmp, metadata_path)
    return str(vectors_path)


def load_openclip_vectors(
    con: sqlite3.Connection, run_id: str
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    db_rows = [dict(row) for row in con.execute(
        """SELECT embedding_id,visual_unit_id,source_content_id,model_name,
                  model_path,dimension,vector_key,run_id
           FROM embeddings WHERE run_id=? ORDER BY embedding_id""",
        (run_id,),
    )]
    expected: dict[str, dict[str, Any]] = {}
    paths: set[Path] = set()
    path_availability: dict[Path, bool] = {}
    for row in db_rows:
        # A complete run commonly stores tens of thousands of vectors in one
        # JSONL payload.  Stat each distinct payload once, not once per vector.
        path, key_id = parse_vector_key(
            str(row["vector_key"]), path_availability
        )
        if key_id != str(row["embedding_id"]):
            raise RuntimeError("stop03_5e_v2_vector_key_id_mismatch")
        expected[key_id] = row
        paths.add(path)
    dimensions = sorted({int(row["dimension"]) for row in db_rows})
    if len(dimensions) != 1:
        raise RuntimeError("stop03_5e_v2_openclip_dimension_mixed")
    identity = _visual_cache_identity(run_id, db_rows, paths, dimensions[0])
    cached = _load_visual_cache(
        identity, {str(row["visual_unit_id"]) for row in db_rows},
    )
    if cached is not None:
        vectors, cache_stats = cached
        return vectors, {
            "database_embedding_count": len(db_rows),
            "payload_row_count": len(db_rows),
            "validated_payload_vector_count": len(vectors),
            "visual_vector_count": len(vectors),
            "payload_file_count": len(paths), "dimension": dimensions[0],
            "payload_paths": [str(path) for path in sorted(paths)],
            **cache_stats,
        }
    found: dict[str, list[float]] = {}
    payload_rows = 0
    invalid = 0
    for path in sorted(paths):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                payload_rows += 1
                row = json.loads(line)
                embedding_id = str(row.get("embedding_id") or "")
                if embedding_id not in expected:
                    continue
                db_row = expected[embedding_id]
                vector = [float(value) for value in row.get("vector", [])]
                packed = json.dumps(vector, ensure_ascii=False, separators=(",", ":"))
                norm = math.sqrt(sum(value * value for value in vector))
                valid = (
                    str(row.get("visual_unit_id")) == str(db_row["visual_unit_id"])
                    and str(row.get("run_id")) == run_id
                    and len(vector) == int(db_row["dimension"])
                    and all(math.isfinite(value) for value in vector)
                    and abs(norm - 1.0) <= 0.001
                    and str(row.get("vector_sha256"))
                    == hashlib.sha256(packed.encode("utf-8")).hexdigest()
                )
                if not valid:
                    invalid += 1
                    continue
                if embedding_id in found:
                    raise RuntimeError(
                        f"stop03_5e_v2_duplicate_payload_embedding:{embedding_id}"
                    )
                found[embedding_id] = vector
    missing_ids = sorted(set(expected) - set(found))
    if invalid or missing_ids:
        raise RuntimeError(
            f"stop03_5e_v2_payload_invalid:invalid={invalid}:missing={len(missing_ids)}"
        )
    vectors = {
        str(expected[embedding_id]["visual_unit_id"]): vector
        for embedding_id, vector in found.items()
    }
    if len(vectors) != len(expected):
        raise RuntimeError("stop03_5e_v2_visual_vector_not_unique")
    cache_path = _save_visual_cache(identity, vectors)
    return vectors, {
        "database_embedding_count": len(db_rows),
        "payload_row_count": payload_rows,
        "validated_payload_vector_count": len(found),
        "visual_vector_count": len(vectors),
        "payload_file_count": len(paths),
        "dimension": dimensions[0] if dimensions else 0,
        "payload_paths": [str(path) for path in sorted(paths)],
        "runtime_cache_hit": False,
        "runtime_cache_path": cache_path,
    }


def latest_text_run(con: sqlite3.Connection) -> Optional[dict[str, Any]]:
    if not object_exists(con, "stop03_5d_text_embedding_runs"):
        return None
    row = con.execute(
        """SELECT * FROM stop03_5d_text_embedding_runs WHERE status='success'
           ORDER BY created_at DESC,embedding_run_id DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row is not None else None


def visual_filter_sql(
    args: argparse.Namespace, *, source_columns: set[str], database_objects: set[str]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if args.media_type:
        clauses.append("s.media_type=?")
        values.append(args.media_type)
    if args.source_content_id:
        clauses.append("d.source_content_id=?")
        values.append(args.source_content_id)
    if args.source_relative_path_prefix:
        escaped = args.source_relative_path_prefix.replace("\\", "\\\\")
        escaped = escaped.replace("%", "\\%").replace("_", "\\_")
        clauses.append("s.relative_path LIKE ? ESCAPE '\\'")
        values.append(escaped + "%")
    source_mtime_min = getattr(args, "source_mtime_min", None)
    source_mtime_max = getattr(args, "source_mtime_max", None)
    has_ocr = bool(getattr(args, "has_ocr", False))
    has_person = bool(getattr(args, "has_person", False))
    if source_mtime_min is not None:
        if "mtime" not in source_columns:
            raise RuntimeError("stop03_5e_v2_source_mtime_filter_unavailable")
        clauses.append("s.mtime>=?")
        values.append(source_mtime_min)
    if source_mtime_max is not None:
        if "mtime" not in source_columns:
            raise RuntimeError("stop03_5e_v2_source_mtime_filter_unavailable")
        clauses.append("s.mtime<=?")
        values.append(source_mtime_max)
    if has_ocr:
        if "stop03_5d_text_documents" not in database_objects:
            raise RuntimeError("stop03_5e_v2_ocr_filter_unavailable")
        clauses.append(
            "EXISTS(SELECT 1 FROM stop03_5d_text_documents td "
            "WHERE td.canonical_visual_unit_id=v.visual_unit_id "
            "AND length(trim(td.ocr_text))>0)"
        )
    if has_person:
        if "stop03_1c_person_reid_run_items" not in database_objects:
            raise RuntimeError("stop03_5e_v2_person_filter_unavailable")
        clauses.append(
            "EXISTS(SELECT 1 FROM stop03_1c_person_reid_run_items pi "
            "WHERE pi.visual_unit_id=v.visual_unit_id AND pi.status='success' "
            "AND pi.face_count>0)"
        )
    if args.time_position_ms_min is not None:
        clauses.append("COALESCE(d.time_position_ms,-1)>=?")
        values.append(args.time_position_ms_min)
    if args.time_position_ms_max is not None:
        clauses.append("COALESCE(d.time_position_ms,-1)<=?")
        values.append(args.time_position_ms_max)
    return (" WHERE " + " AND ".join(clauses) if clauses else "", values)


def load_visual_rows(
    con: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, dict[str, Any]]:
    objects = {
        str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    source_columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(source_assets)")
    }
    where, values = visual_filter_sql(
        args, source_columns=source_columns, database_objects=objects,
    )
    rows = con.execute(
        """SELECT v.visual_unit_id,v.derived_id,d.source_content_id,
                  d.derived_type,d.frame_index,COALESCE(d.time_position_ms,-1) time_position_ms,
                  d.derived_path,s.relative_path source_relative_path,s.media_type
           FROM visual_units v
           JOIN derived_assets d ON d.derived_id=v.derived_id
           JOIN source_assets s ON s.source_content_id=d.source_content_id"""
        + where + " ORDER BY v.visual_unit_id",
        values,
    ).fetchall()
    return {str(row["visual_unit_id"]): dict(row) for row in rows}


def coverage_by_media(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows.values():
        counts[str(row["media_type"])] += 1
    return dict(sorted(counts.items()))


def build_preflight(
    db: Path, config_path: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config(config_path)
    normalized = text_search.validate_queries([args.query], config)[0]
    if args.result_limit is None:
        args.result_limit = int(config["default_result_limit"])
    if not 1 <= args.result_limit <= int(config["max_result_limit"]):
        raise RuntimeError("stop03_5e_v2_result_limit_invalid")
    if args.result_offset < 0 or args.temporal_dedup_ms < 0:
        raise RuntimeError("stop03_5e_v2_pagination_or_dedup_invalid")
    if args.preview_window_ms not in config["video_preview_window_options_ms"]:
        raise RuntimeError("stop03_5e_v2_preview_window_invalid")
    if not args.openclip_python.is_file():
        raise RuntimeError("stop03_5e_v2_openclip_python_missing")
    db_before = readonly_database_identity(db)
    with connect_ro(db) as con:
        required = (
            "visual_units", "derived_assets", "source_assets", "embeddings",
            "model_runs", "visual_labels", "visual_label_terms",
        )
        missing = [name for name in required if not object_exists(con, name)]
        if missing:
            raise RuntimeError(f"stop03_5e_v2_database_objects_missing:{missing}")
        run, visual_count = select_complete_openclip_run(con)
        all_visuals = load_visual_rows(con, args)
        vectors, vector_stats = load_openclip_vectors(con, str(run["run_id"]))
        text_run = latest_text_run(con)
        text_document_count = 0
        text_visual_count = 0
        if text_run:
            text_document_count, text_visual_count = con.execute(
                """SELECT COUNT(*),COUNT(DISTINCT canonical_visual_unit_id)
                   FROM stop03_5d_text_documents WHERE embedding_run_id=?""",
                (text_run["embedding_run_id"],),
            ).fetchone()
        yoloe_visual_count = int(con.execute(
            "SELECT COUNT(DISTINCT visual_unit_id) FROM visual_labels"
        ).fetchone()[0])
        native_readiness_verified = bool(
            getattr(args, "native_readiness_verified", False)
        )
        if native_readiness_verified:
            # The native bridge has already opened this exact database in
            # query-only mode and verified schema/vector/source coverage.
            # Re-reading every page of a large database for every query made
            # cold searches spend tens of seconds before ranking began.
            integrity = "delegated_to_native_readiness"
            foreign_keys = 0
        else:
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    eligible_ids = set(all_visuals)
    vector_ids = set(vectors)
    missing_filtered = sorted(eligible_ids - vector_ids)
    all_scope_match = visual_count == len(vectors)
    checks = {
        "latest_openclip_run_complete": int(run["input_count"]) == int(run["output_count"]),
        "all_visual_units_have_openclip_vectors": all_scope_match,
        "all_filtered_visual_units_have_openclip_vectors": not missing_filtered,
        "openclip_payload_vectors_valid": (
            vector_stats["validated_payload_vector_count"] == visual_count
        ),
        "database_integrity_ok": integrity in {"ok", "delegated_to_native_readiness"},
        "foreign_keys_ok": foreign_keys == 0,
        "central_db_unchanged": db_before == readonly_database_identity(db),
    }
    if not all(checks.values()):
        raise RuntimeError(
            "stop03_5e_v2_preflight_failed:"
            + json.dumps(checks, ensure_ascii=False, sort_keys=True)
        )
    identity = {
        "contract_version": CONTRACT_VERSION,
        "openclip_run_id": run["run_id"],
        "text_embedding_run_id": text_run["embedding_run_id"] if text_run else None,
        "query_sha256": text_search.sha256_text(normalized),
        "filters": {
            "media_type": args.media_type,
            "source_content_id": args.source_content_id,
            "source_relative_path_prefix": args.source_relative_path_prefix,
            "time_position_ms_min": args.time_position_ms_min,
            "time_position_ms_max": args.time_position_ms_max,
            "source_mtime_min": args.source_mtime_min,
            "source_mtime_max": args.source_mtime_max,
            "has_ocr": bool(args.has_ocr),
            "has_person": bool(args.has_person),
            "audio_evidence_only": bool(getattr(args, "audio_evidence_only", False)),
            "disable_audio_evidence": bool(getattr(args, "disable_audio_evidence", False)),
            "native_readiness_verified": bool(
                getattr(args, "native_readiness_verified", False)
            ),
        },
        "pagination": {"result_offset": args.result_offset, "result_limit": args.result_limit},
        "temporal_dedup_ms": args.temporal_dedup_ms,
        "preview_window_ms": args.preview_window_ms,
        "timecode_precision": args.timecode_precision,
    }
    request_id = "query5ev2_" + text_search.sha256_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )[:24]
    summary = {
        "status": "PASS",
        "technical_status": "PASS",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "request": identity,
        "openclip_run_id": run["run_id"],
        "openclip_model_name": run["model_name"],
        "openclip_model_path": run["model_path"],
        "text_embedding_run_id": text_run["embedding_run_id"] if text_run else None,
        "text_model_path": text_run["model_path"] if text_run else None,
        "visual_unit_count": visual_count,
        "visual_unit_count_by_media": coverage_by_media(load_visual_rows_for_all(db)),
        "eligible_visual_unit_count": len(all_visuals),
        "eligible_visual_unit_count_by_media": coverage_by_media(all_visuals),
        "openclip_vector_count": len(vectors),
        "text_document_count": int(text_document_count),
        "text_distinct_visual_unit_count": int(text_visual_count),
        "yoloe_detected_visual_unit_count": yoloe_visual_count,
        "openclip_payload": vector_stats,
        "checks": checks,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "query_preflight_scope": (
            "readonly_schema_vector_coverage"
            if native_readiness_verified
            else "full_integrity_and_foreign_keys"
        ),
        "full_database_integrity_checked": not native_readiness_verified,
        "query_text_persisted": False,
        "query_vector_persisted": False,
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }
    runtime = {
        "normalized_query": normalized,
        "visual_rows": all_visuals,
        "visual_vectors": {key: vectors[key] for key in all_visuals},
        "openclip_run": run,
        "text_run": text_run,
        "config": config,
    }
    return summary, runtime


def load_visual_rows_for_all(db: Path) -> dict[str, dict[str, Any]]:
    placeholder = argparse.Namespace(
        media_type=None, source_content_id=None, source_relative_path_prefix=None,
        time_position_ms_min=None, time_position_ms_max=None,
    )
    with connect_ro(db) as con:
        return load_visual_rows(con, placeholder)


def ranks_desc(values: Mapping[str, float]) -> dict[str, int]:
    ordered = sorted(values, key=lambda key: (-float(values[key]), str(key)))
    return {key: index for index, key in enumerate(ordered, 1)}


SEARCH_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    # Local lexical aliases supplement semantic vectors. They are narrow,
    # inspectable and query-only: no model result or task database is changed.
    "麦子": ("麦田", "小麦", "麦穗", "麦地"),
    "树": ("树木", "树林", "树叶", "树枝", "林木"),
    "路": ("道路", "公路", "小路", "土路", "小径", "路面"),
}


def matching_text_terms(query: str, text: str) -> list[str]:
    """Return direct query/alias evidence found in one stored description."""
    folded_query = text_search.normalize_query(query).casefold()
    folded_text = text_search.normalize_query(text).casefold()
    if not folded_query:
        return []
    terms = (folded_query, *SEARCH_QUERY_ALIASES.get(folded_query, ()))
    matches: list[str] = []
    for term in dict.fromkeys(terms):
        if len(term) == 1 and "\u3400" <= term <= "\u9fff":
            # Retain the conservative rule for arbitrary single-character
            # searches.  Concept aliases above still make “树”和“路” useful.
            tokens = {
                text_search.normalize_query(token).casefold()
                for token in re.split(r"[\s,，。:：;；/|()（）\[\]{}<>《》]+", folded_text)
                if text_search.normalize_query(token)
            }
            if term in tokens:
                matches.append(term)
        elif term in folded_text:
            matches.append(term)
    return matches


def text_has_exact_query(query: str, text: str) -> bool:
    return bool(matching_text_terms(query, text))


def score_text_evidence(
    db: Path, run: Optional[Mapping[str, Any]], query: str,
    query_vector: Optional[Sequence[float]], eligible_ids: set[str],
) -> tuple[dict[str, float], dict[str, dict[str, Any]], int]:
    if not run or query_vector is None:
        return {}, {}, 0
    run_id = str(run["embedding_run_id"])
    documents = text_search.load_search_documents(db, run_id, "", [])
    scores: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    scanned = 0
    query_array = (
        np.asarray(query_vector, dtype=np.float32)
        if np is not None else None
    )
    for chunk in text_search.iter_vector_chunks(db, run_id, "", [], 2048):
        chunk_scores: Sequence[float]
        if np is not None and query_array is not None and chunk:
            matrix = np.vstack([
                np.frombuffer(
                    row["vector_blob"], dtype="<f4", count=int(row["model_dimension"]),
                )
                for row in chunk
            ])
            chunk_scores = matrix @ query_array
        else:
            chunk_scores = [
                sum(
                    float(a) * float(b)
                    for a, b in zip(
                        query_vector,
                        struct.unpack(
                            "<" + str(int(row["model_dimension"])) + "f",
                            row["vector_blob"],
                        ),
                    )
                )
                for row in chunk
            ]
        for row, raw_score in zip(chunk, chunk_scores):
            scanned += 1
            score = float(raw_score)
            for document in documents[str(row["text_vector_id"])]:
                visual_id = str(document["canonical_visual_unit_id"])
                if visual_id not in eligible_ids:
                    continue
                exact_terms = matching_text_terms(query, str(document["embedding_text"]))
                exact = bool(exact_terms)
                if visual_id not in scores or score > scores[visual_id]:
                    previous_exact = bool(evidence.get(visual_id, {}).get("text_exact_match"))
                    previous_preview = evidence.get(visual_id, {}).get("text_preview")
                    previous_terms = list(evidence.get(visual_id, {}).get("matched_text_terms") or [])
                    scores[visual_id] = score
                    evidence[visual_id] = {
                        "text_vector_id": row["text_vector_id"],
                        "text_semantic_score": score,
                        "text_exact_match": exact or previous_exact,
                        "matched_text_terms": sorted(set(previous_terms + exact_terms)),
                        "text_preview": (
                            previous_preview if previous_exact and not exact
                            else str(document["embedding_text"])[:500]
                        ),
                    }
                elif exact:
                    # Exact evidence must not be hidden by a different document with a
                    # slightly higher semantic cosine for the same visual unit.
                    evidence[visual_id]["text_exact_match"] = True
                    evidence[visual_id]["matched_text_terms"] = sorted(set(
                        list(evidence[visual_id].get("matched_text_terms") or []) + exact_terms
                    ))
                    evidence[visual_id]["text_preview"] = str(document["embedding_text"])[:500]
    return scores, evidence, scanned


def score_audio_evidence(
    db: Path, query: str, query_vector: Optional[Sequence[float]],
    visual_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, dict[str, Any]], int]:
    """Map transcript vectors to the nearest searchable frame in each video."""
    if query_vector is None:
        return {}, {}, 0
    by_source: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for visual_id, row in visual_rows.items():
        if str(row.get("media_type")) != "video":
            continue
        by_source[str(row["source_content_id"])].append(
            (int(row.get("time_position_ms") or 0), str(visual_id))
        )
    if not by_source:
        return {}, {}, 0
    with connect_ro(db) as con:
        objects = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"audio_speech_evidence", "audio_text_embeddings"} <= objects:
            return {}, {}, 0
        rows = con.execute(
            """SELECT e.evidence_id,e.source_content_id,e.start_time_ms,e.end_time_ms,
                      e.hit_time_ms,e.transcript_text,v.dimension,v.vector_blob
               FROM audio_speech_evidence e
               JOIN audio_text_embeddings v USING(evidence_id)
               WHERE v.status='success'
               ORDER BY e.source_content_id,e.start_time_ms,e.evidence_id"""
        ).fetchall()
    query_array = np.asarray(query_vector, dtype=np.float32) if np is not None else None
    scores: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    scanned = 0
    for row in rows:
        source_id = str(row["source_content_id"])
        candidates = by_source.get(source_id)
        if not candidates:
            continue
        dimension = int(row["dimension"])
        if dimension != len(query_vector):
            continue
        scanned += 1
        if query_array is not None:
            vector = np.frombuffer(row["vector_blob"], dtype="<f4", count=dimension)
            score = float(vector @ query_array)
        else:
            vector = struct.unpack("<" + str(dimension) + "f", row["vector_blob"])
            score = sum(float(a) * float(b) for a, b in zip(query_vector, vector))
        hit_time = int(row["hit_time_ms"])
        _frame_time, visual_id = min(candidates, key=lambda item: abs(item[0] - hit_time))
        transcript = str(row["transcript_text"])
        exact_terms = matching_text_terms(query, transcript)
        exact = bool(exact_terms)
        previous = evidence.get(visual_id, {})
        if visual_id not in scores or score > scores[visual_id] or (exact and not previous.get("text_exact_match")):
            scores[visual_id] = max(score, scores.get(visual_id, -1.0))
            evidence[visual_id] = {
                "text_vector_id": row["evidence_id"],
                "text_semantic_score": score,
                "text_exact_match": exact,
                "matched_text_terms": exact_terms,
                "text_preview": transcript[:500],
                "audio_transcript_match": True,
                "audio_evidence_id": row["evidence_id"],
                "audio_start_time_ms": int(row["start_time_ms"]),
                "audio_end_time_ms": int(row["end_time_ms"]),
                "audio_hit_time_ms": hit_time,
                "time_position_ms": hit_time,
            }
    return scores, evidence, scanned


def should_scan_audio_evidence(args: argparse.Namespace) -> bool:
    """Keep speech search behind its explicit interface in the native app."""
    return bool(getattr(args, "audio_evidence_only", False)) or not bool(
        getattr(args, "disable_audio_evidence", False)
    )


def merge_audio_text_evidence(
    text_scores: dict[str, float], text_evidence: dict[str, dict[str, Any]],
    audio_scores: Mapping[str, float], audio_evidence: Mapping[str, Mapping[str, Any]],
) -> None:
    for visual_id, audio_score in audio_scores.items():
        audio_row = dict(audio_evidence[visual_id])
        previous = text_evidence.get(visual_id)
        if previous is None or audio_score > text_scores.get(visual_id, -1.0):
            text_scores[visual_id] = float(audio_score)
            text_evidence[visual_id] = audio_row
        elif bool(audio_row.get("text_exact_match")):
            merged = dict(previous)
            merged.update(audio_row)
            merged["text_semantic_score"] = max(
                float(previous.get("text_semantic_score") or -1.0), float(audio_score)
            )
            merged["matched_text_terms"] = sorted(set(
                list(previous.get("matched_text_terms") or [])
                + list(audio_row.get("matched_text_terms") or [])
            ))
            text_evidence[visual_id] = merged


def load_yoloe_evidence(
    db: Path, query: str, eligible_ids: set[str], minimum_confidence: float = 0.0,
) -> tuple[dict[str, float], dict[str, list[dict[str, Any]]]]:
    folded = text_search.normalize_query(query).casefold()
    matched: dict[str, float] = {}
    labels: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with connect_ro(db) as con:
        definitions: dict[str, dict[str, Any]] = {}
        for row in con.execute(
            """SELECT label,label_zh,category_zh
               FROM visual_label_terms ORDER BY label"""
        ):
            # A detector label only proves its canonical object class.  Free-form
            # aliases may be narrower attributes (for example age, identity or
            # state) that the detector did not observe, so they must not become
            # authoritative exact-object evidence.
            terms = [str(row["label"]), str(row["label_zh"] or "")]
            normalized_terms = {
                text_search.normalize_query(term).casefold()
                for term in terms if text_search.normalize_query(term)
            }
            if folded in normalized_terms:
                definitions[str(row["label"])] = dict(row)
        if not definitions:
            return {}, {}
        placeholders = ",".join("?" for _ in definitions)
        rows = con.execute(
            f"""SELECT visual_unit_id,label,confidence FROM visual_labels
                 WHERE label IN ({placeholders}) AND confidence>=?
                 ORDER BY visual_unit_id,confidence DESC,label""",
            [*definitions, float(minimum_confidence)],
        ).fetchall()
    for row in rows:
        visual_id = str(row["visual_unit_id"])
        if visual_id not in eligible_ids:
            continue
        definition = definitions[str(row["label"])]
        item = {
            "label": row["label"], "label_zh": definition["label_zh"],
            "category_zh": definition["category_zh"],
            "confidence": float(row["confidence"]), "query_match": True,
        }
        labels[visual_id].append(item)
        matched[visual_id] = max(matched.get(visual_id, 0.0), float(row["confidence"]))
    return matched, dict(labels)


def group_visual_results_by_source(
    rows: Sequence[dict[str, Any]], args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], int]:
    """Use one representative frame per video in visual-only result pages.

    A source video can contribute dozens of sampled frames to one query. The
    main visual search shows its best representative; the existing browse-all
    action exposes the timeline. Speech search keeps separate transcript times.
    """
    enabled = bool(getattr(args, "disable_audio_evidence", False)) and not bool(
        getattr(args, "audio_evidence_only", False)
    )
    if not enabled:
        return list(rows), 0
    grouped: list[dict[str, Any]] = []
    representative_by_source: dict[str, dict[str, Any]] = {}
    merged = 0
    for original in rows:
        row = dict(original)
        if row.get("media_type") != "video":
            row["source_match_count"] = 1
            grouped.append(row)
            continue
        source_id = str(row.get("source_content_id") or row.get("visual_unit_id") or "")
        point = int(row.get("time_position_ms") or 0)
        representative = representative_by_source.get(source_id)
        if representative is None:
            row["source_match_count"] = 1
            row["source_match_time_positions_ms"] = [point]
            row["source_match_time_span_start_ms"] = point
            row["source_match_time_span_end_ms"] = point
            representative_by_source[source_id] = row
            grouped.append(row)
            continue
        representative["source_match_count"] = int(
            representative.get("source_match_count") or 1
        ) + 1
        positions = list(representative.get("source_match_time_positions_ms") or [])
        if len(positions) < 24:
            positions.append(point)
            representative["source_match_time_positions_ms"] = sorted(set(positions))
        representative["source_match_time_span_start_ms"] = min(
            int(representative.get("source_match_time_span_start_ms") or point), point,
        )
        representative["source_match_time_span_end_ms"] = max(
            int(representative.get("source_match_time_span_end_ms") or point), point,
        )
        merged += 1
    return grouped, merged


def fuse_results(
    visual_rows: Mapping[str, Mapping[str, Any]],
    visual_vectors: Mapping[str, Sequence[float]], visual_query: Sequence[float],
    text_scores: Mapping[str, float], text_evidence: Mapping[str, Mapping[str, Any]],
    yoloe_matches: Mapping[str, float], yoloe_labels: Mapping[str, Sequence[Mapping[str, Any]]],
    config: Mapping[str, Any], args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    visual_ids = list(visual_vectors)
    if np is not None and visual_ids:
        matrix = np.asarray(
            [visual_vectors[visual_id] for visual_id in visual_ids],
            dtype=np.float32,
        )
        query_array = np.asarray(visual_query, dtype=np.float32)
        visual_scores = {
            visual_id: float(score)
            for visual_id, score in zip(visual_ids, matrix @ query_array)
        }
    else:
        visual_scores = {
            visual_id: sum(float(a) * float(b) for a, b in zip(visual_query, vector))
            for visual_id, vector in visual_vectors.items()
        }
    if not all(math.isfinite(value) for value in visual_scores.values()):
        raise RuntimeError("stop03_5e_v2_visual_score_non_finite")
    visual_ranks = ranks_desc(visual_scores)
    text_ranks = ranks_desc(text_scores)
    yoloe_ranks = ranks_desc(yoloe_matches)
    k = int(config["rrf_k"])
    rows: list[dict[str, Any]] = []
    query_sha256 = getattr(
        args, "query_sha256", text_search.sha256_text(text_search.normalize_query(args.query))
    )
    for visual_id, source in visual_rows.items():
        score = float(config["visual_rank_weight"]) / (k + visual_ranks[visual_id])
        if visual_id in text_ranks:
            score += float(config["text_rank_weight"]) / (k + text_ranks[visual_id])
        if visual_id in yoloe_ranks:
            score += float(config["yoloe_rank_weight"]) / (k + yoloe_ranks[visual_id])
        text_row = dict(text_evidence.get(visual_id, {}))
        rows.append({
            **dict(source),
            "result_id": "hybrid5e_" + hashlib.sha256(
                (str(source["visual_unit_id"]) + "\0" + str(query_sha256)).encode("utf-8")
            ).hexdigest()[:24],
            "hybrid_score": score,
            "openclip_cosine": visual_scores[visual_id],
            "openclip_rank": visual_ranks[visual_id],
            "text_rank": text_ranks.get(visual_id),
            "yoloe_rank": yoloe_ranks.get(visual_id),
            "text_evidence_present": visual_id in text_scores,
            "yoloe_query_match": visual_id in yoloe_matches,
            "yoloe_labels": list(yoloe_labels.get(visual_id, ())),
            **text_row,
        })
    ordered = sorted(
        rows,
        key=lambda row: (-float(row["hybrid_score"]), -float(row["openclip_cosine"]), str(row["visual_unit_id"])),
    )
    relevant: list[dict[str, Any]] = []
    rejected = 0
    relevant_by_media: dict[str, int] = defaultdict(int)
    for row in ordered:
        visual_score = float(row["openclip_cosine"])
        text_score = row.get("text_semantic_score")
        audio_candidate = bool(row.get("audio_transcript_match"))
        audio_exact = audio_candidate and bool(row.get("text_exact_match"))
        audio_semantic = (
            audio_candidate
            and text_score is not None
            and float(text_score) >= float(config["minimum_text_semantic_cosine"])
        )
        audio_relevant = audio_exact or audio_semantic
        # ``audio_transcript_match`` is a user-facing claim, not merely a flag
        # saying that some transcript exists near this frame.  Keep it false
        # when the transcript did not pass either the exact or semantic gate.
        row["audio_transcript_match"] = audio_relevant
        if bool(getattr(args, "audio_evidence_only", False)) and not audio_relevant:
            rejected += 1
            continue
        object_confidence = float(yoloe_matches.get(str(row["visual_unit_id"]), 0.0))
        object_supported = (
            object_confidence >= float(config["minimum_object_label_confidence"])
            and (
                visual_score >= float(config["minimum_object_support_visual_cosine"])
                or (
                    text_score is not None
                    and float(text_score) >= float(config["minimum_object_support_text_cosine"])
                )
            )
        )
        reasons: list[str] = []
        if bool(row.get("text_exact_match")):
            reasons.append("exact_text")
        if audio_relevant:
            reasons.append(
                "audio_transcript_exact"
                if audio_exact
                else "audio_transcript_semantic"
            )
        if bool(row.get("yoloe_query_match")) and object_supported:
            reasons.append("exact_object_label")
        if visual_score >= float(config["minimum_visual_cosine"]):
            reasons.append("strong_visual_semantic")
        if text_score is not None and float(text_score) >= float(config["minimum_text_semantic_cosine"]):
            reasons.append("strong_text_semantic")
        if (
            text_score is not None
            and visual_score >= float(config["minimum_combined_visual_cosine"])
            and float(text_score) >= float(config["minimum_combined_text_semantic_cosine"])
        ):
            reasons.append("combined_visual_text")
        if not reasons:
            rejected += 1
            continue
        row["relevance_reasons"] = reasons
        row["matched_object_labels"] = [
            {
                "label": item.get("label"),
                "label_zh": item.get("label_zh"),
                "confidence": float(item.get("confidence") or 0.0),
            }
            for item in row.get("yoloe_labels", ())
            if item.get("query_match")
            and float(item.get("confidence") or 0.0)
            >= float(config["minimum_object_label_confidence"])
        ] if "exact_object_label" in reasons else []
        evidence_scores: list[float] = []
        if "exact_text" in reasons:
            evidence_scores.append(0.88)
        if "exact_object_label" in reasons:
            evidence_scores.append(min(0.92, 0.55 + 0.4 * object_confidence))
        if "strong_visual_semantic" in reasons:
            evidence_scores.append(min(0.90, max(0.0, (visual_score + 1.0) / 2.0)))
        if "strong_text_semantic" in reasons:
            evidence_scores.append(min(0.92, max(0.0, (float(text_score) + 1.0) / 2.0)))
        if "combined_visual_text" in reasons:
            combined = ((visual_score + 1.0) / 2.0 + (float(text_score) + 1.0) / 2.0) / 2.0
            evidence_scores.append(min(0.94, max(0.0, combined + 0.08)))
        row["relevance_score"] = min(
            0.99,
            max(evidence_scores) + min(0.08, 0.02 * max(0, len(reasons) - 1)),
        )
        relevant.append(row)
        relevant_by_media[str(row["media_type"])] += 1
    relevant.sort(
        key=lambda row: (
            -float(row["relevance_score"]),
            -float(row["hybrid_score"]),
            str(row["visual_unit_id"]),
        )
    )
    deduped: list[dict[str, Any]] = []
    selected_times: dict[str, list[int]] = defaultdict(list)
    for row in relevant:
        if row["media_type"] == "video" and args.temporal_dedup_ms > 0:
            source_id = str(row["source_content_id"])
            point = int(row["time_position_ms"])
            if any(abs(point - other) < args.temporal_dedup_ms for other in selected_times[source_id]):
                continue
            selected_times[source_id].append(point)
        deduped.append(row)
    deduped_by_media: dict[str, int] = defaultdict(int)
    for row in deduped:
        deduped_by_media[str(row["media_type"])] += 1
    source_grouped, source_grouped_count = group_visual_results_by_source(deduped, args)
    source_grouped_by_media: dict[str, int] = defaultdict(int)
    for row in source_grouped:
        source_grouped_by_media[str(row["media_type"])] += 1
    page = source_grouped[args.result_offset:args.result_offset + args.result_limit]
    return page, {
        "scanned_visual_vector_count": len(visual_scores),
        "text_scored_visual_unit_count": len(text_scores),
        "yoloe_query_matched_visual_unit_count": len(yoloe_matches),
        "ranked_candidate_count": len(ordered),
        "relevance_eligible_result_count": len(relevant),
        "relevance_rejected_result_count": rejected,
        "relevance_eligible_count_by_media": dict(sorted(relevant_by_media.items())),
        "pre_temporal_dedup_result_count": len(relevant),
        "post_temporal_dedup_result_count": len(deduped),
        "post_temporal_dedup_count_by_media": dict(sorted(deduped_by_media.items())),
        "source_grouping_enabled": bool(
            getattr(args, "disable_audio_evidence", False)
            and not getattr(args, "audio_evidence_only", False)
        ),
        "source_grouped_frame_count": source_grouped_count,
        "post_source_group_result_count": len(source_grouped),
        "post_source_group_count_by_media": dict(sorted(source_grouped_by_media.items())),
        "result_offset": args.result_offset,
        "result_limit": args.result_limit,
        "returned_result_count": len(page),
        "next_result_offset": (
            args.result_offset + args.result_limit
            if len(source_grouped) > args.result_offset + args.result_limit else None
        ),
    }


def materialize_assets(
    db: Path, out: Path, response: dict[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    assets = out / "reports/assets"
    assets.mkdir(parents=True, exist_ok=True)
    context: dict[str, list[dict[str, Any]]] = defaultdict(list)
    text_run_id = response.get("text_embedding_run_id")
    if text_run_id:
        with connect_ro(db) as con:
            for row in con.execute(
                """SELECT source_content_id,time_position_ms,qwen_text
                   FROM stop03_5d_text_documents
                   WHERE embedding_run_id=? AND qwen_text<>''
                   ORDER BY source_content_id,time_position_ms,document_id""",
                (text_run_id,),
            ):
                context[str(row["source_content_id"])].append(dict(row))
    statuses: list[str] = []
    missing = 0
    segments = 0
    for row in response["results"]:
        source = Path(str(row["derived_path"]))
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(row["derived_id"]))
        name = f"{safe}_{text_search.sha256_text(str(source))[:16]}{source.suffix.lower() or '.jpg'}"
        target = assets / name
        if target.exists():
            status = "existing_asset"
        elif os.path.lexists(target):
            status = "broken_existing_asset"
        elif not source.is_file():
            status = "missing_source_derived_frame"
        else:
            try:
                target.symlink_to(os.path.relpath(source, assets))
                status = "relative_symlink"
            except OSError:
                try:
                    shutil.copy2(source, target)
                    status = "readonly_copy"
                except OSError:
                    status = "asset_materialization_failed"
        statuses.append(status)
        if status in {"relative_symlink", "readonly_copy", "existing_asset"}:
            row["preview_asset_src"] = f"assets/{name}"
        else:
            missing += 1
        row["preview_asset_status"] = status
        point = int(row["time_position_ms"])
        nearby = sorted(
            context.get(str(row["source_content_id"]), []),
            key=lambda item: abs(int(item["time_position_ms"]) - point),
        )[: int(config["environment_neighbor_count_each_side"]) * 2 + 1]
        row.update(text_search.classify_environment_texts(
            [str(item["qwen_text"]) for item in nearby]
        ))
        if row["media_type"] == "video" and point >= 0:
            start = max(0, point - int(config["video_preview_anchor_offset_ms"]))
            end = start + int(response["video_preview_window_ms"])
            row["timecode"] = text_search.format_timecode(point, response["timecode_precision"])
            row["preview_segment_start_ms"] = start
            row["preview_segment_end_ms"] = end
            row["preview_segment_start_timecode"] = text_search.format_timecode(start, response["timecode_precision"])
            row["preview_segment_end_timecode"] = text_search.format_timecode(end, response["timecode_precision"])
            row["preview_segment_requires_source_duration_clamp"] = True
            segments += 1
        else:
            row["timecode"] = None
    return {
        "displayed_result_count": len(response["results"]),
        "unique_preview_asset_count": len({row.get("preview_asset_src") for row in response["results"] if row.get("preview_asset_src")}),
        "preview_asset_relative_symlink_count": statuses.count("relative_symlink"),
        "preview_asset_readonly_copy_count": statuses.count("readonly_copy"),
        "preview_asset_existing_count": statuses.count("existing_asset"),
        "preview_asset_missing_count": missing,
        "video_preview_segment_count": segments,
        "video_preview_window_ms": response["video_preview_window_ms"],
        "original_video_clip_generated": False,
    }


def native_hit_field(reasons: Sequence[str]) -> str:
    fields = []
    mapping = {
        "exact_text": "text",
        "exact_object_label": "object_label",
        "strong_visual_semantic": "visual_vector",
        "strong_text_semantic": "semantic_text",
        "combined_visual_text": "visual_and_text",
        "audio_transcript_exact": "audio_transcript",
        "audio_transcript_semantic": "audio_transcript",
    }
    for reason in reasons:
        field = mapping.get(str(reason), str(reason))
        if field not in fields:
            fields.append(field)
    return ",".join(fields) or "visual_vector"


def build_native_result_contract(
    query: str,
    response: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in response["results"]:
        point = int(row.get("time_position_ms") or 0)
        is_video = row.get("media_type") == "video"
        start = (
            max(0, point - int(config["video_preview_anchor_offset_ms"]))
            if is_video else None
        )
        end = start + int(response["video_preview_window_ms"]) if start is not None else None
        reasons = [str(value) for value in row.get("relevance_reasons") or []]
        hit_field = native_hit_field(reasons)
        if row.get("audio_transcript_match"):
            hit_field = "audio_transcript" + ("," + hit_field if hit_field else "")
        items.append({
            "result_id": row.get("result_id"),
            "source_content_id": row.get("source_content_id"),
            "visual_unit_id": row.get("visual_unit_id"),
            "derived_id": row.get("derived_id"),
            "query": query,
            "source_path": row.get("source_relative_path") or "",
            "source_relative_path": row.get("source_relative_path") or "",
            "media_type": row.get("media_type"),
            "preview_path": row.get("derived_path") or "",
            "time_position_ms": point,
            "timecode": (
                text_search.format_timecode(point, response["timecode_precision"])
                if is_video else None
            ),
            "preview_segment_start_ms": start,
            "preview_segment_end_ms": end,
            "preview_segment_start_timecode": (
                text_search.format_timecode(start, response["timecode_precision"])
                if start is not None else None
            ),
            "preview_segment_end_timecode": (
                text_search.format_timecode(end, response["timecode_precision"])
                if end is not None else None
            ),
            "hit_reason": ",".join(reasons) or "visual_vector",
            "hit_field": hit_field,
            "score": row.get("relevance_score"),
            "source_online": None,
            "can_open_original": None,
            "hybrid_score": row.get("hybrid_score"),
            "openclip_cosine": row.get("openclip_cosine"),
            "text_semantic_score": row.get("text_semantic_score"),
            "text_exact_match": row.get("text_exact_match"),
            "yoloe_query_match": row.get("yoloe_query_match"),
            "relevance_reasons": reasons,
            "matched_object_labels": row.get("matched_object_labels") or [],
            "text_preview": row.get("text_preview"),
            "matched_text_terms": row.get("matched_text_terms") or [],
            "audio_transcript_match": bool(row.get("audio_transcript_match")),
            "audio_evidence_id": row.get("audio_evidence_id"),
            "audio_start_time_ms": row.get("audio_start_time_ms"),
            "audio_end_time_ms": row.get("audio_end_time_ms"),
            "audio_hit_time_ms": row.get("audio_hit_time_ms"),
            "environment_label": row.get("environment_label"),
            "environment_user_confirmation_required": row.get(
                "environment_user_confirmation_required"
            ),
            "source_match_count": int(row.get("source_match_count") or 1),
            "source_match_time_positions_ms": row.get("source_match_time_positions_ms") or [],
            "source_match_timecodes": [
                text_search.format_timecode(int(value), response["timecode_precision"])
                for value in row.get("source_match_time_positions_ms") or []
            ],
            "source_match_time_span_start_ms": row.get("source_match_time_span_start_ms"),
            "source_match_time_span_end_ms": row.get("source_match_time_span_end_ms"),
            "yoloe_labels": row.get("yoloe_labels") or [],
        })
    ranking = response.get("ranking") or {}
    return {
        "contract_version": NATIVE_RESULT_CONTRACT_VERSION,
        "status": response.get("technical_status"),
        "query": query,
        "result_count": len(items),
        "result_total_count": int(
            ranking.get("post_source_group_result_count")
            if ranking.get("post_source_group_result_count") is not None
            else len(items)
        ),
        "result_offset": int(ranking.get("result_offset") or 0),
        "result_limit": int(ranking.get("result_limit") or len(items)),
        "next_result_offset": ranking.get("next_result_offset"),
        "result_count_by_media": (
            ranking.get("post_source_group_count_by_media")
            or ranking.get("post_temporal_dedup_count_by_media")
            or {}
        ),
        "result_items": items,
    }


def render_html(response: Mapping[str, Any]) -> str:
    cards: list[str] = []
    for rank, row in enumerate(response["results"], 1):
        image = (
            f'<img loading="lazy" src="{html.escape(str(row.get("preview_asset_src")), quote=True)}" alt="derived preview">'
            if row.get("preview_asset_src") else '<div class="missing">派生预览图缺失</div>'
        )
        segment = "静态图片"
        if row.get("timecode"):
            segment = (
                f'命中 {html.escape(str(row["timecode"]))}；播放 '
                f'{html.escape(str(row["preview_segment_start_timecode"]))}–'
                f'{html.escape(str(row["preview_segment_end_timecode"]))}'
            )
        channels = ["OpenCLIP全量视觉"]
        if row["text_evidence_present"]:
            channels.append("Qwen/OCR文本")
        if row["yoloe_query_match"]:
            channels.append("YOLOE物体匹配")
        labels = "、".join(
            str(item.get("label_zh") or item.get("label"))
            for item in row.get("yoloe_labels", [])[:8]
        ) or "无明确物体标签"
        preview = html.escape(str(row.get("text_preview") or "仅视觉召回，无详细文本描述"))
        cards.append(
            f'<article><h2>#{rank} hybrid={row["hybrid_score"]:.6f} visual={row["openclip_cosine"]:.6f}</h2>'
            + image + '<div><p>' + html.escape(str(row["source_relative_path"]))
            + ' · ' + segment + '</p><p>召回证据：' + html.escape(" + ".join(channels))
            + '</p><p>YOLOE：' + html.escape(labels) + '</p><p>场景：'
            + html.escape(str(row["environment_label"])) + '</p><pre>' + preview
            + '</pre></div></article>'
        )
    return """<!doctype html><meta charset="utf-8"><title>Stop03-5E 混合全视觉搜索</title>
<style>body{font-family:system-ui;margin:24px;background:#f4f4f4;color:#222}article{display:grid;
grid-template-columns:minmax(260px,420px) 1fr;gap:18px;background:white;padding:16px;margin:16px 0;
border-radius:10px}article h2{grid-column:1/-1}img{width:100%;max-height:300px;object-fit:contain;
background:#111;border-radius:8px}pre{white-space:pre-wrap}.missing{padding:70px;background:#222;color:white}
@media(max-width:760px){article{grid-template-columns:1fr}}</style><h1>Stop03-5E 混合全视觉搜索</h1>
<p>查询内容未保存；以下结果由全量视觉向量、已有文本证据和YOLOE标签共同排序。</p>""" + "".join(cards)


def validate_html(html_text: str, report_dir: Path) -> dict[str, Any]:
    sources = re.findall(r'<img\s+[^>]*src="([^"]+)"', html_text)
    relative = [src for src in sources if src.startswith("assets/")]
    missing = [src for src in relative if not (report_dir / src).is_file()]
    invalid = [src for src in sources if src.startswith("/") or src.startswith("file://") or ".." in Path(src).parts]
    passed = len(relative) == len(sources) and not missing and not invalid
    return {
        "html_img_total_count": len(sources),
        "html_img_relative_assets_count": len(relative),
        "html_img_invalid_path_count": len(invalid),
        "html_img_missing_asset_count": len(missing),
        "html_img_http_accessible_check_status": "PASS_STATIC_RELATIVE_ASSETS" if passed else "FAIL",
    }


def _query_worker_root() -> Optional[Path]:
    configured = str(os.environ.get("MEDIA_ARCHIVE_SEARCH_WORKER_ROOT") or "").strip()
    if not configured:
        return None
    root = Path(configured).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _worker_exchange(socket_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(90.0)
        client.connect(str(socket_path))
        client.sendall(request)
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 8 * 1024 * 1024:
                raise RuntimeError("stop03_5e_query_worker_response_too_large")
    line = bytes(response).split(b"\n", 1)[0]
    if not line:
        raise RuntimeError("stop03_5e_query_worker_empty_response")
    value = json.loads(line.decode("utf-8"))
    if value.get("status") != "PASS":
        raise RuntimeError(str(value.get("error") or "stop03_5e_query_worker_failed"))
    return value


def _request_query_worker(
    role: str,
    python_path: Path,
    payload: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    root = _query_worker_root()
    if root is None:
        return None
    if role not in {"openclip", "text"}:
        raise RuntimeError("stop03_5e_query_worker_role_invalid")
    socket_path = root / f"{role}.sock"
    try:
        return _worker_exchange(socket_path, payload)
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, socket.timeout, OSError):
        pass

    lock_path = root / f"{role}.lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                return _worker_exchange(socket_path, payload)
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, socket.timeout, OSError):
                try:
                    socket_path.unlink(missing_ok=True)
                except OSError:
                    pass
                command = [
                    str(python_path), str(Path(__file__).resolve()),
                    "--internal-query-worker", role,
                    "--socket-path", str(socket_path),
                    "--idle-timeout-seconds", "300",
                ]
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={
                        **os.environ,
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "HF_DATASETS_OFFLINE": "1",
                        "TOKENIZERS_PARALLELISM": "false",
                    },
                    start_new_session=True,
                    close_fds=True,
                )
                deadline = time.monotonic() + 20.0
                last_error: Optional[Exception] = None
                while time.monotonic() < deadline:
                    try:
                        return _worker_exchange(socket_path, payload)
                    except (
                        FileNotFoundError, ConnectionRefusedError, ConnectionResetError,
                        socket.timeout, OSError,
                    ) as exc:
                        last_error = exc
                        time.sleep(0.1)
                raise RuntimeError(
                    "stop03_5e_query_worker_start_timeout:"
                    + (str(last_error) if last_error else role)
                )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _query_worker_response(
    connection: socket.socket, payload: Mapping[str, Any]
) -> None:
    connection.sendall(
        json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def internal_query_worker(
    role: str, socket_path: Path, idle_timeout_seconds: int,
) -> int:
    """Serve query embeddings from memory; never persist query text or vectors."""
    text_search.block_network()
    if role not in {"openclip", "text"}:
        raise RuntimeError("stop03_5e_query_worker_role_invalid")
    socket_path = Path(socket_path).expanduser().absolute()
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o600)
    server.listen(8)
    server.settimeout(1.0)
    last_activity = time.monotonic()
    model: Any = None
    tokenizer: Any = None
    signature: tuple[str, str, str] | None = None
    effective_device = "cpu"
    try:
        while time.monotonic() - last_activity < max(30, int(idle_timeout_seconds)):
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            last_activity = time.monotonic()
            with connection:
                connection.settimeout(90.0)
                raw = bytearray()
                while b"\n" not in raw:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        break
                try:
                    request = json.loads(bytes(raw).split(b"\n", 1)[0].decode("utf-8"))
                    requested_device = str(request.get("device") or "auto")
                    model_path = str(request["model_path"])
                    model_name = str(request.get("model_name") or "")
                    requested_signature = (model_name, model_path, requested_device)
                    load_seconds = 0.0
                    worker_reused = model is not None and requested_signature == signature
                    if not worker_reused:
                        model = None
                        tokenizer = None
                        gc.collect()
                        load_started = time.monotonic()
                        if role == "openclip":
                            import torch
                            import open_clip
                            from safetensors.torch import load_file

                            effective_device = requested_device
                            if requested_device == "auto":
                                effective_device = (
                                    "mps" if torch.backends.mps.is_available() else "cpu"
                                )
                            model, _, _ = open_clip.create_model_and_transforms(
                                model_name, pretrained=None, device=effective_device,
                            )
                            state = load_file(model_path, device="cpu")
                            missing, unexpected = model.load_state_dict(state, strict=False)
                            if len(unexpected) > max(20, len(state) // 5):
                                raise RuntimeError(
                                    "stop03_5e_v2_openclip_state_incompatible:"
                                    f"{len(missing)}:{len(unexpected)}"
                                )
                            model = model.to(effective_device).eval()
                            tokenizer = open_clip.get_tokenizer(model_name)
                        else:
                            import torch
                            from sentence_transformers import SentenceTransformer

                            effective_device = requested_device
                            if requested_device == "auto":
                                effective_device = (
                                    "mps" if torch.backends.mps.is_available() else "cpu"
                                )
                            model = SentenceTransformer(
                                model_path,
                                device=effective_device,
                                local_files_only=True,
                                trust_remote_code=False,
                            )
                        signature = requested_signature
                        load_seconds = time.monotonic() - load_started
                    if str(request.get("action") or "embed") == "prewarm":
                        _query_worker_response(connection, {
                            "status": "PASS",
                            "contract": SEARCH_WORKER_PROTOCOL_VERSION,
                            "role": role,
                            "device": effective_device,
                            "model_load_seconds": load_seconds,
                            "worker_reused": worker_reused,
                        })
                        continue

                    query = str(request.get("query") or "")
                    embed_started = time.monotonic()
                    if role == "openclip":
                        import torch

                        assert tokenizer is not None
                        with torch.no_grad():
                            encoded = model.encode_text(
                                tokenizer([query]).to(effective_device)
                            )
                            encoded = encoded / encoded.norm(dim=-1, keepdim=True)
                        vector = encoded.detach().cpu().float().numpy()[0].tolist()
                        vectors = [vector]
                    else:
                        array = model.encode(
                            [query],
                            prompt_name=str(request.get("prompt_name") or "query"),
                            batch_size=1,
                            show_progress_bar=False,
                            precision="float32",
                            convert_to_numpy=True,
                            normalize_embeddings=True,
                        )
                        vectors = array.astype("float32", copy=False).tolist()
                    _query_worker_response(connection, {
                        "status": "PASS",
                        "contract": SEARCH_WORKER_PROTOCOL_VERSION,
                        "role": role,
                        "device": effective_device,
                        "vectors": vectors,
                        "model_load_seconds": load_seconds,
                        "query_embedding_seconds": time.monotonic() - embed_started,
                        "worker_reused": worker_reused,
                    })
                except Exception as exc:
                    _query_worker_response(connection, {
                        "status": "FAIL",
                        "contract": SEARCH_WORKER_PROTOCOL_VERSION,
                        "role": role,
                        "error": str(exc)[:1000],
                    })
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
    return 0


def prewarm_query_worker(
    role: str,
    python_path: Path,
    model_path: Path,
    model_name: str,
    device: str,
) -> dict[str, Any]:
    response = _request_query_worker(role, python_path, {
        "action": "prewarm",
        "model_path": str(model_path),
        "model_name": str(model_name),
        "device": str(device),
    })
    return response or {
        "status": "SKIPPED",
        "contract": SEARCH_WORKER_PROTOCOL_VERSION,
        "role": role,
        "reason": "worker_root_not_configured",
    }


def prewarm_query_models(
    db: Path,
    openclip_python: Path,
    device: str,
) -> dict[str, Any]:
    """Load both query models into short-lived local workers without a query."""
    if not openclip_python.is_file():
        raise RuntimeError("stop03_5e_v2_openclip_python_missing")
    db_before = readonly_database_identity(db)
    with connect_ro(db) as con:
        openclip_run, _visual_count = select_complete_openclip_run(con)
        text_run = latest_text_run(con)
        cache_started = time.monotonic()
        _cached_vectors, visual_cache = load_openclip_vectors(
            con, str(openclip_run["run_id"]),
        )
        visual_cache_seconds = time.monotonic() - cache_started
    started = time.monotonic()
    openclip = prewarm_query_worker(
        "openclip",
        openclip_python,
        Path(str(openclip_run["model_path"])),
        str(openclip_run["model_name"]),
        device,
    )
    text = (
        prewarm_query_worker(
            "text",
            Path(sys.executable),
            Path(str(text_run["model_path"])),
            "",
            device,
        )
        if text_run
        else {
            "status": "SKIPPED",
            "contract": SEARCH_WORKER_PROTOCOL_VERSION,
            "role": "text",
            "reason": "text_embedding_run_missing",
        }
    )
    db_after = readonly_database_identity(db)
    return {
        "status": (
            "PASS"
            if openclip.get("status") in {"PASS", "SKIPPED"}
            and text.get("status") in {"PASS", "SKIPPED"}
            and db_before == db_after
            else "FAIL"
        ),
        "contract": "media_archive_search_prewarm_v1",
        "openclip": openclip,
        "text": text,
        "visual_data_cache": {
            "ready": int(visual_cache.get("visual_vector_count") or 0) > 0,
            "cache_hit": bool(visual_cache.get("runtime_cache_hit")),
            "vector_count": int(visual_cache.get("visual_vector_count") or 0),
            "path": str(visual_cache.get("runtime_cache_path") or ""),
            "elapsed_seconds": visual_cache_seconds,
            "contains_query_data": False,
        },
        "elapsed_seconds": time.monotonic() - started,
        "database_write": False,
        "central_db_unchanged": db_before == db_after,
        "query_text_used": False,
        "query_vector_persisted": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }


def real_openclip_embedder(
    python_path: Path, model_name: str, model_path: str, query: str, device: str
) -> tuple[list[float], dict[str, Any]]:
    worker_started = time.monotonic()
    worker_error = ""
    try:
        worker = _request_query_worker("openclip", python_path, {
            "action": "embed",
            "model_name": model_name,
            "model_path": str(model_path),
            "query": str(query),
            "device": device,
        })
        if worker is not None:
            return [float(value) for value in worker["vectors"][0]], {
                "device": worker["device"],
                "openclip_model_load_seconds": float(worker["model_load_seconds"]),
                "openclip_query_embedding_seconds": float(worker["query_embedding_seconds"]),
                "openclip_subprocess_seconds": time.monotonic() - worker_started,
                "openclip_warm_worker_used": True,
                "openclip_warm_worker_reused": bool(worker.get("worker_reused")),
            }
    except Exception as exc:
        worker_error = str(exc)[:500]

    payload = {"model_name": model_name, "model_path": str(model_path), "query": str(query), "device": device}
    started = time.monotonic()
    completed = subprocess.run(
        [str(python_path), str(Path(__file__).resolve()), "--internal-openclip-text-embed"],
        input=json.dumps(payload, ensure_ascii=False), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"},
    )
    if completed.returncode != 0:
        raise RuntimeError("stop03_5e_v2_openclip_query_failed:" + completed.stderr[-1000:])
    lines = [line for line in completed.stdout.splitlines() if line.startswith("OPENCLIP_QUERY_JSON=")]
    if len(lines) != 1:
        raise RuntimeError("stop03_5e_v2_openclip_query_output_invalid")
    result = json.loads(lines[0].split("=", 1)[1])
    return [float(value) for value in result["vector"]], {
        "device": result["device"], "openclip_model_load_seconds": result["model_load_seconds"],
        "openclip_query_embedding_seconds": result["query_embedding_seconds"],
        "openclip_subprocess_seconds": time.monotonic() - started,
        "openclip_warm_worker_used": False,
        "openclip_warm_worker_reused": False,
        "openclip_warm_worker_error": worker_error,
    }


def real_text_embedder(
    model_path: Path,
    queries: list[str],
    prompt_name: str,
    device: str,
) -> tuple[list[list[float]], dict[str, Any]]:
    if len(queries) != 1:
        return text_search.real_query_embedder(model_path, queries, prompt_name, device)
    worker_error = ""
    worker_started = time.monotonic()
    try:
        worker = _request_query_worker("text", Path(sys.executable), {
            "action": "embed",
            "model_name": "",
            "model_path": str(model_path),
            "query": str(queries[0]),
            "prompt_name": prompt_name,
            "device": device,
        })
        if worker is not None:
            return [
                [float(value) for value in worker["vectors"][0]]
            ], {
                "device": worker["device"],
                "model_load_seconds": float(worker["model_load_seconds"]),
                "query_embedding_seconds": float(worker["query_embedding_seconds"]),
                "text_warm_worker_used": True,
                "text_warm_worker_reused": bool(worker.get("worker_reused")),
                "text_worker_roundtrip_seconds": time.monotonic() - worker_started,
            }
    except Exception as exc:
        worker_error = str(exc)[:500]
    vectors, runtime = text_search.real_query_embedder(
        model_path, queries, prompt_name, device,
    )
    return vectors, {
        **runtime,
        "text_warm_worker_used": False,
        "text_warm_worker_reused": False,
        "text_warm_worker_error": worker_error,
    }


def internal_openclip_text_embed() -> int:
    text_search.block_network()
    payload = json.loads(sys.stdin.read())
    import torch
    import open_clip
    from safetensors.torch import load_file

    device = str(payload["device"])
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    load_started = time.monotonic()
    model, _, _ = open_clip.create_model_and_transforms(
        str(payload["model_name"]), pretrained=None, device=device
    )
    state = load_file(str(payload["model_path"]), device="cpu")
    missing, unexpected = model.load_state_dict(state, strict=False)
    if len(unexpected) > max(20, len(state) // 5):
        raise RuntimeError(f"stop03_5e_v2_openclip_state_incompatible:{len(missing)}:{len(unexpected)}")
    model = model.to(device).eval()
    tokenizer = open_clip.get_tokenizer(str(payload["model_name"]))
    load_seconds = time.monotonic() - load_started
    embed_started = time.monotonic()
    with torch.no_grad():
        vector = model.encode_text(tokenizer([str(payload["query"])]).to(device))
        vector = vector / vector.norm(dim=-1, keepdim=True)
    values = vector.detach().cpu().float().numpy()[0].tolist()
    print("OPENCLIP_QUERY_JSON=" + json.dumps({
        "vector": values, "device": device, "model_load_seconds": load_seconds,
        "query_embedding_seconds": time.monotonic() - embed_started,
    }, separators=(",", ":")), flush=True)
    return 0


def execute_query(
    db: Path, config_path: Path, out: Path, args: argparse.Namespace,
    visual_embedder: VisualEmbedder = real_openclip_embedder,
    text_embedder: TextEmbedder = real_text_embedder,
) -> tuple[dict[str, Any], Path]:
    progress_started = time.monotonic()
    emit_search_progress(
        "scope", 1, 7, "正在核对只读数据库与搜索范围",
        detail="检查视觉向量、文本向量和素材筛选条件",
        started=progress_started,
    )
    preflight, runtime = build_preflight(db, config_path, args)
    config = runtime["config"]
    query = runtime["normalized_query"]
    args.query_sha256 = preflight["request"]["query_sha256"]
    media_counts = preflight.get("eligible_visual_unit_count_by_media") or {}
    emit_search_progress(
        "scope", 1, 7, "搜索范围已确认",
        completed=int(preflight["eligible_visual_unit_count"]),
        total=int(preflight["eligible_visual_unit_count"]),
        detail=(
            f"图片画面 {int(media_counts.get('image', 0))} · "
            f"视频画面 {int(media_counts.get('video', 0))} · "
            f"文本向量 {int(preflight.get('text_document_count') or 0)}"
        ),
        started=progress_started,
    )
    db_before = readonly_database_identity(db)
    started = time.monotonic()
    emit_search_progress(
        "visual_query", 2, 7, "正在生成视觉查询向量",
        detail="使用本地 OpenCLIP；查询内容不会写入数据库",
        started=progress_started,
    )
    visual_query, visual_runtime = visual_embedder(
        args.openclip_python, str(preflight["openclip_model_name"]),
        str(preflight["openclip_model_path"]), query, args.device,
    )
    text_search.validate_query_vectors(
        [visual_query], 1, int(preflight["openclip_payload"]["dimension"])
    )
    emit_search_progress(
        "visual_query", 2, 7, "视觉查询向量已生成",
        completed=1, total=1,
        detail=(
            "已复用预热模型"
            if visual_runtime.get("openclip_warm_worker_reused")
            else "本地模型已就绪"
        ),
        started=progress_started,
    )
    text_query: Optional[list[float]] = None
    text_runtime: dict[str, Any] = {}
    if runtime["text_run"]:
        emit_search_progress(
            "text_query", 3, 7, "正在生成文本查询向量",
            detail="使用本地文本模型融合 AI 描述与 OCR 证据",
            started=progress_started,
        )
        vectors, text_runtime = text_embedder(
            Path(str(runtime["text_run"]["model_path"])), [query], "query", args.device
        )
        text_search.validate_query_vectors(
            vectors, 1, int(runtime["text_run"]["model_dimension"])
        )
        text_query = vectors[0]
        emit_search_progress(
            "text_query", 3, 7, "文本查询向量已生成",
            completed=1, total=1,
            detail=(
                "已复用预热模型"
                if text_runtime.get("text_warm_worker_reused")
                else "本地模型已就绪"
            ),
            started=progress_started,
        )
    else:
        emit_search_progress(
            "text_query", 3, 7, "当前素材库没有文本向量，跳过文本语义",
            completed=0, total=0, started=progress_started,
        )
    gc.collect()
    eligible_ids = set(runtime["visual_rows"])
    emit_search_progress(
        "text_scan", 4, 7, "正在比对已有文本证据",
        completed=0,
        total=int(preflight.get("text_document_count") or 0),
        detail="融合 AI 描述、OCR 文字和已有语义证据",
        started=progress_started,
    )
    text_scores, text_evidence, text_vectors_scanned = score_text_evidence(
        db, runtime["text_run"], query, text_query, eligible_ids
    )
    if should_scan_audio_evidence(args):
        audio_scores, audio_evidence, audio_vectors_scanned = score_audio_evidence(
            db, query, text_query, runtime["visual_rows"]
        )
    else:
        audio_scores, audio_evidence, audio_vectors_scanned = {}, {}, 0
    merge_audio_text_evidence(text_scores, text_evidence, audio_scores, audio_evidence)
    text_vectors_scanned += audio_vectors_scanned
    emit_search_progress(
        "text_scan", 4, 7, "文本证据比对完成",
        completed=text_vectors_scanned, total=text_vectors_scanned,
        detail=(
            f"已扫描 {text_vectors_scanned} 个画面描述/OCR文本向量；人声转写仅在音频筛选中搜索"
            if not should_scan_audio_evidence(args)
            else f"已扫描 {text_vectors_scanned} 个文本向量"
        ),
        started=progress_started,
    )
    emit_search_progress(
        "object_labels", 5, 7, "正在匹配物体标签",
        detail="核对中文标签、别名和置信度",
        started=progress_started,
    )
    yoloe_matches, yoloe_labels = load_yoloe_evidence(
        db, query, eligible_ids,
        minimum_confidence=float(config["minimum_object_label_confidence"]),
    )
    emit_search_progress(
        "object_labels", 5, 7, "物体标签匹配完成",
        completed=len(yoloe_matches), total=len(eligible_ids),
        detail=f"找到 {len(yoloe_matches)} 个直接标签候选",
        started=progress_started,
    )
    emit_search_progress(
        "ranking", 6, 7, "正在扫描全部画面并融合排序",
        completed=0, total=len(runtime["visual_vectors"]),
        detail="综合视觉、文本和物体标签证据",
        started=progress_started,
    )
    results, ranking = fuse_results(
        runtime["visual_rows"], runtime["visual_vectors"], visual_query,
        text_scores, text_evidence, yoloe_matches, yoloe_labels, config, args,
    )
    emit_search_progress(
        "ranking", 6, 7, "全量画面排序完成",
        completed=int(ranking["scanned_visual_vector_count"]),
        total=len(runtime["visual_vectors"]),
        detail=(
            f"形成 {int(ranking['post_source_group_result_count'])} 个素材结果；"
            f"合并 {int(ranking['source_grouped_frame_count'])} 个同源抽帧"
        ),
        started=progress_started,
    )
    emit_search_progress(
        "results", 7, 7, "正在整理首屏结果与预览信息",
        completed=0, total=len(results),
        detail="结果仍保持中心数据库只读",
        started=progress_started,
    )
    response = {
        **{key: value for key, value in preflight.items() if key not in {"openclip_payload", "checks"}},
        "status": "PASS", "technical_status": "PASS", "policy_status": "PASS",
        "query_model_run": True, "timecode_precision": args.timecode_precision,
        "video_preview_window_ms": args.preview_window_ms,
        "scanned_visual_vector_count": ranking["scanned_visual_vector_count"],
        "scanned_text_vector_count": text_vectors_scanned,
        "scanned_audio_text_vector_count": audio_vectors_scanned,
        "audio_evidence_policy": (
            "explicit_audio_filter_only"
            if not should_scan_audio_evidence(args)
            else "included_for_audio_filter"
        ),
        "ranking": ranking, "results": results,
        "runtime": {**visual_runtime, **text_runtime, "total_query_seconds": time.monotonic() - started},
        "technical_checks": {
            "all_eligible_visual_vectors_scanned": ranking["scanned_visual_vector_count"] == len(runtime["visual_rows"]),
            "full_database_visual_coverage": preflight["openclip_vector_count"] == preflight["visual_unit_count"],
            # An empty page is a valid search answer after relevance gating.
            "result_page_valid": len(results) <= int(args.result_limit),
            "all_scores_finite": all(math.isfinite(float(row["hybrid_score"])) for row in results),
            "all_results_traceable": all(row["visual_unit_id"] and row["source_content_id"] and row["derived_id"] for row in results),
            "central_db_unchanged": db_before == readonly_database_identity(db),
        },
        "central_db_identity_before": list(db_before),
        "central_db_identity_after": list(readonly_database_identity(db)),
        "database_write": False, "query_text_persisted": False, "query_vector_persisted": False,
        "network_used": False, "download_used": False, "original_media_read": False,
        "search_index_created": False,
    }
    request_out = out / str(preflight["request_id"])
    native_contract = bool(getattr(args, "native_app_result_contract", False))
    if not native_contract:
        response["visual_preview"] = materialize_assets(db, request_out, response, config)
        html_text = render_html(response)
        response["visual_preview"].update(validate_html(html_text, request_out / "reports"))
        response["technical_checks"]["all_displayed_results_have_preview_assets"] = (
            response["visual_preview"]["preview_asset_missing_count"] == 0
            and response["visual_preview"]["html_img_http_accessible_check_status"] == "PASS_STATIC_RELATIVE_ASSETS"
        )
    passed = all(response["technical_checks"].values())
    response["status"] = response["technical_status"] = "PASS" if passed else "FAIL"
    report_dir = request_out / "reports"
    if native_contract:
        result_contract = build_native_result_contract(query, response, config)
        summary = {key: value for key, value in response.items() if key != "results"}
        summary.update({
            "result_contract_version": NATIVE_RESULT_CONTRACT_VERSION,
            "result_count": result_contract["result_count"],
            "formal_result_file": "search_results.json",
            "html_generated": False,
            "preview_asset_materialization": False,
        })
        text_search.write_json(report_dir / "search_results.json", result_contract)
        text_search.write_json(report_dir / "search_summary.json", summary)
    else:
        text_search.write_json(report_dir / "query_response.json", response)
        (report_dir / "query_response.html").write_text(render_html(response), encoding="utf-8")
    emit_search_progress(
        "results", 7, 7, "搜索完成",
        completed=len(results), total=len(results),
        detail="正式结果已就绪",
        started=progress_started,
    )
    return response, request_out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("preflight", "dry-run", "query", "warmup"), required=True
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--query", default="")
    parser.add_argument("--openclip-python", type=Path, required=True)
    parser.add_argument("--result-offset", type=int, default=0)
    parser.add_argument("--result-limit", type=int)
    parser.add_argument("--temporal-dedup-ms", type=int, default=5000)
    parser.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    parser.add_argument("--timecode-precision", choices=("second", "millisecond"), default="millisecond")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--media-type", choices=("image", "video"))
    parser.add_argument("--source-content-id")
    parser.add_argument("--source-relative-path-prefix")
    parser.add_argument("--time-position-ms-min", type=int)
    parser.add_argument("--time-position-ms-max", type=int)
    parser.add_argument("--source-mtime-min", type=int)
    parser.add_argument("--source-mtime-max", type=int)
    parser.add_argument("--has-ocr", action="store_true")
    parser.add_argument("--has-person", action="store_true")
    parser.add_argument("--audio-evidence-only", action="store_true")
    parser.add_argument("--disable-audio-evidence", action="store_true")
    parser.add_argument("--native-readiness-verified", action="store_true")
    parser.add_argument("--confirm-real-local-query", action="store_true")
    parser.add_argument("--native-app-result-contract", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])
    if raw_args == ["--internal-openclip-text-embed"]:
        return internal_openclip_text_embed()
    if "--internal-query-worker" in raw_args:
        worker_parser = argparse.ArgumentParser()
        worker_parser.add_argument(
            "--internal-query-worker", choices=("openclip", "text"), required=True
        )
        worker_parser.add_argument("--socket-path", type=Path, required=True)
        worker_parser.add_argument("--idle-timeout-seconds", type=int, default=300)
        worker_args = worker_parser.parse_args(raw_args)
        return internal_query_worker(
            worker_args.internal_query_worker,
            worker_args.socket_path,
            worker_args.idle_timeout_seconds,
        )
    args = build_parser().parse_args(raw_args)
    if args.mode == "warmup":
        result = prewarm_query_models(args.db, args.openclip_python, args.device)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] == "PASS" else 2
    if not str(args.query).strip():
        raise RuntimeError("stop03_5e_v2_query_required")
    if args.mode == "preflight":
        preflight, _runtime = build_preflight(args.db, args.config, args)
        public = {key: value for key, value in preflight.items() if key != "openclip_payload"}
        print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.mode == "dry-run":
        preflight, _runtime = build_preflight(args.db, args.config, args)
        plan = {**preflight, "status": "DRY_RUN_PASS", "planned_output_root": str(args.out)}
        request_out = args.out / "dry-run" / str(preflight["request_id"])
        text_search.write_json(request_out / "query_plan.json", plan)
        print(json.dumps({key: value for key, value in plan.items() if key != "openclip_payload"}, ensure_ascii=False, indent=2), flush=True)
        return 0
    if not args.confirm_real_local_query:
        raise RuntimeError("stop03_5e_v2_real_query_confirmation_required")
    response, request_out = execute_query(args.db, args.config, args.out, args)
    public = {key: value for key, value in response.items() if key != "results"}
    if args.native_app_result_contract:
        public["result_json"] = str(request_out / "reports/search_results.json")
        public["summary_json"] = str(request_out / "reports/search_summary.json")
    else:
        public["response_json"] = str(request_out / "reports/query_response.json")
        public["response_html"] = str(request_out / "reports/query_response.html")
    print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
    return 0 if response["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
