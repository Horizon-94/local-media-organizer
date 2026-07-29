#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from PIL import Image, ImageOps

import stop03_2_v23_video_frame_contact_sheet_20260710_190836 as contact_sheet


SCRIPT_VERSION = "stop03_2_candidate_queues_from_db_safe_v23_0_20260710_190836"
POLICY_VERSION = "stop03_2_generic_high_value_policy_v23"
STAGE = "stop03_2_candidate_queues_v23"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "stop03_2_high_value_policy_v23.json"
RULE_DOCUMENT = PROJECT_ROOT / "docs" / "pipeline_rules" / "STOP03_2_GENERIC_HIGH_VALUE_RULES_V23.md"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-2-candidate-queues-db-safe-v23_0_dry_run"

VIDEO_QWEN_ROLES = {
    "video_coverage_keyframe",
    "video_coverage_high_signal_overlap",
    "video_high_signal_supplement",
}

BASE_FIELDS = [
    "candidate_id", "run_id", "queue_type", "candidate_role", "candidate_score",
    "source_content_id", "visual_unit_id", "canonical_visual_unit_id",
    "duplicate_group_id", "duplicate_reverse_member_count",
    "duplicate_reverse_visual_unit_ids", "derived_id", "frame_index",
    "time_position_ms", "canonical_time_ms", "group_start_ms", "group_end_ms",
    "segment_start_ms", "segment_end_ms", "source_relative_path", "visual_file",
    "media_type", "coverage_anchor_index", "coverage_anchor_visual_unit_id",
    "reason_codes", "dedup_reason", "black_frame_status", "labels",
    "generic_label_categories", "policy_version", "script_version",
    "script_sha256", "config_sha256", "rule_document_sha256",
    "central_dedup_run_id", "yoloe_run_id", "openclip_run_id",
    "execution_mode",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]


def safe_int(value: Any, default: int = -1) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json_text(dict(row)) + "\n")
            count += 1
    return count


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str] | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fields or sorted({key for row in rows for key in row}))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def load_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    config = json.loads(raw)
    if config.get("policy_version") != POLICY_VERSION:
        raise RuntimeError("config_policy_version_mismatch")
    if safe_int(config.get("coverage_stride_frames"), 0) != 6:
        raise RuntimeError("coverage_stride_must_equal_six_sampled_frames")
    if safe_int(config.get("coverage_local_radius_frames"), 0) != 3:
        raise RuntimeError("coverage_local_radius_frames_must_equal_three")
    return config, hashlib.sha256(raw).hexdigest()


def connect_readonly(db: Path) -> sqlite3.Connection:
    resolved = db.expanduser().resolve(strict=True)
    con = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def connect_write(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db.expanduser().resolve(strict=True)))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def object_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def assert_output_path(path: Path, *, may_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(TEST_OUTPUT_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    if resolved == TEST_OUTPUT_ROOT.resolve():
        raise RuntimeError("output_must_not_equal_test_output_root")
    if not may_exist and resolved.exists() and any(resolved.iterdir()):
        raise RuntimeError(f"output_directory_not_empty:{resolved}")
    return resolved


def set_offline_environment() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["ULTRALYTICS_OFFLINE"] = "1"


def runtime_lineage(con: sqlite3.Connection) -> dict[str, str]:
    central = {str(row[0]) for row in con.execute("SELECT DISTINCT run_id FROM visual_identity")}
    yoloe = {str(row[0]) for row in con.execute("SELECT DISTINCT run_id FROM visual_labels")}
    openclip = {str(row[0]) for row in con.execute("SELECT DISTINCT run_id FROM embeddings")}
    if len(central) != 1 or len(yoloe) != 1 or len(openclip) != 1:
        raise RuntimeError(
            f"runtime_lineage_not_unique:central={sorted(central)},yoloe={sorted(yoloe)},openclip={sorted(openclip)}"
        )
    return {
        "central_dedup_run_id": next(iter(central)),
        "yoloe_run_id": next(iter(yoloe)),
        "openclip_run_id": next(iter(openclip)),
    }


def preflight(db: Path, out: Path, config_path: Path) -> dict[str, Any]:
    config, config_sha = load_config(config_path)
    required = {
        "canonical_visual_units_for_heavy", "canonical_source_assets_for_heavy",
        "visual_units", "source_assets", "derived_assets", "visual_identity",
        "visual_duplicate_groups", "dedup_runs", "visual_labels", "embeddings",
        "model_runs", "stop03_2_candidate_queue_items",
    }
    with connect_readonly(db) as con:
        missing = sorted(name for name in required if not object_exists(con, name))
        if missing:
            raise RuntimeError("required_database_objects_missing:" + ",".join(missing))
        integrity = [str(row[0]) for row in con.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
        lineage = runtime_lineage(con)
        raw_visual = int(con.execute("SELECT COUNT(*) FROM visual_units").fetchone()[0])
        canonical_visual = int(con.execute("SELECT COUNT(*) FROM canonical_visual_units_for_heavy").fetchone()[0])
        canonical_source = int(con.execute("SELECT COUNT(*) FROM canonical_source_assets_for_heavy").fetchone()[0])
        missing_derived = int(
            con.execute(
                """SELECT COUNT(*) FROM canonical_visual_units_for_heavy vu
                   JOIN derived_assets da ON da.derived_id=vu.derived_id
                   WHERE COALESCE(vu.visual_file,da.derived_path,'')=''"""
            ).fetchone()[0]
        )
        vector_keys = [str(row[0]) for row in con.execute("SELECT DISTINCT vector_key FROM embeddings")]
    payload_paths = sorted(
        {
            key.removeprefix("jsonl:").split("#", 1)[0]
            for key in vector_keys
            if key.startswith("jsonl:")
        }
    )
    missing_payloads = [path for path in payload_paths if not Path(path).is_file()]
    status = "PASS" if integrity == ["ok"] and not foreign_keys and not missing_derived and payload_paths and not missing_payloads else "FAIL"
    return {
        "technical_status": status,
        "policy_status": "REVIEW",
        "commit_status": "DO_NOT_COMMIT",
        "policy_version": POLICY_VERSION,
        "script_version": SCRIPT_VERSION,
        "database_path": str(db.resolve()),
        "output_path_checked_not_created": str(assert_output_path(out, may_exist=False)),
        "config_path": str(config_path.resolve()),
        "config_sha256": config_sha,
        "rule_document_path": str(RULE_DOCUMENT),
        "rule_document_sha256": sha256_file(RULE_DOCUMENT),
        "raw_visual_input_count": raw_visual,
        "canonical_visual_input_count": canonical_visual,
        "canonical_source_input_count": canonical_source,
        "dedup_excluded_visual_count": raw_visual - canonical_visual,
        "missing_derived_path_rows": missing_derived,
        "vector_payload_paths": payload_paths,
        "missing_vector_payload_paths": missing_payloads,
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
        **lineage,
        "coverage_stride_frames": config["coverage_stride_frames"],
        "coverage_local_radius_frames": config["coverage_local_radius_frames"],
        "input_contract": {
            "heavy_visual": "canonical_visual_units_for_heavy",
            "heavy_source": "canonical_source_assets_for_heavy",
            "raw_visual_metadata_only": "visual_units",
        },
        "safety": {
            "sqlite_open_mode": "mode=ro",
            "sqlite_write": False,
            "original_video_read": False,
            "model_loading": False,
            "network": "disabled",
            "download_install": False,
        },
    }


def effective_time(row: Mapping[str, Any]) -> int:
    derived = safe_int(row.get("derived_time_position_ms"), -1)
    return derived if derived >= 0 else safe_int(row.get("visual_time_position_ms"), -1)


def load_raw_visual_metadata(con: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows: list[dict[str, Any]] = []
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sql = """
    SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,vu.time_position_ms AS visual_time_position_ms,
           da.time_position_ms AS derived_time_position_ms,da.frame_index,sa.media_type,sa.relative_path,
           vi.canonical_visual_unit_id,vi.visual_duplicate_group_id,vi.identity_status,
           vi.eligible_for_heavy_models
    FROM visual_units vu
    JOIN derived_assets da ON da.derived_id=vu.derived_id
    JOIN source_assets sa ON sa.source_content_id=vu.source_content_id
    JOIN visual_identity vi ON vi.visual_unit_id=vu.visual_unit_id
    WHERE sa.media_type IN ('video','image')
    ORDER BY sa.media_type,vu.source_content_id,da.time_position_ms,da.frame_index,vu.visual_unit_id
    """
    for raw in con.execute(sql):
        row = dict(raw)
        row["time_position_ms"] = effective_time(row)
        rows.append(row)
        if row["media_type"] == "video":
            by_source[str(row["source_content_id"])].append(row)
    for source_rows in by_source.values():
        source_rows.sort(key=lambda row: (safe_int(row["time_position_ms"]), safe_int(row["frame_index"]), str(row["visual_unit_id"])))
        for index, row in enumerate(source_rows):
            row["sampled_sequence_index"] = index
    return rows, by_source


def load_canonical_visuals(
    con: sqlite3.Connection,
    raw_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    raw_index = {
        str(row["visual_unit_id"]): safe_int(row.get("sampled_sequence_index"), -1)
        for rows in raw_by_source.values() for row in rows
    }
    sql = """
    SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,vu.visual_file,
           vu.time_position_ms AS visual_time_position_ms,vu.near_black,vu.luma_mean,vu.luma_std,
           da.derived_path,da.time_position_ms AS derived_time_position_ms,da.frame_index,
           da.width AS db_width,da.height AS db_height,da.sha256 AS derived_sha256,
           sa.media_type,sa.relative_path AS source_relative_path,sa.absolute_path AS source_absolute_path,
           vi.identity_status,vi.canonical_visual_unit_id,vi.visual_duplicate_group_id AS duplicate_group_id,
           vi.blocked_reason,vi.run_id AS central_dedup_run_id,
           reverse_map.member_count AS duplicate_reverse_member_count,
           reverse_map.member_ids AS duplicate_reverse_visual_unit_ids,
           reverse_map.group_start_ms,reverse_map.group_end_ms
    FROM canonical_visual_units_for_heavy vu
    JOIN canonical_source_assets_for_heavy sa ON sa.source_content_id=vu.source_content_id
    JOIN derived_assets da ON da.derived_id=vu.derived_id
    JOIN visual_identity vi ON vi.visual_unit_id=vu.visual_unit_id
    LEFT JOIN (
      SELECT vi2.canonical_visual_unit_id,COUNT(*) AS member_count,
             GROUP_CONCAT(vi2.visual_unit_id,'|') AS member_ids,
             MIN(COALESCE(da2.time_position_ms,vu2.time_position_ms)) AS group_start_ms,
             MAX(COALESCE(da2.time_position_ms,vu2.time_position_ms)) AS group_end_ms
      FROM visual_identity vi2
      JOIN visual_units vu2 ON vu2.visual_unit_id=vi2.visual_unit_id
      JOIN derived_assets da2 ON da2.derived_id=vu2.derived_id
      GROUP BY vi2.canonical_visual_unit_id
    ) reverse_map ON reverse_map.canonical_visual_unit_id=vu.visual_unit_id
    WHERE sa.media_type IN ('video','image')
    ORDER BY sa.media_type,vu.source_content_id,da.time_position_ms,da.frame_index,vu.visual_unit_id
    """
    rows: list[dict[str, Any]] = []
    for raw in con.execute(sql):
        row = dict(raw)
        row["time_position_ms"] = effective_time(row)
        row["canonical_time_ms"] = row["time_position_ms"]
        row["sampled_sequence_index"] = raw_index.get(str(row["visual_unit_id"]), -1)
        row["duplicate_reverse_member_count"] = safe_int(row.get("duplicate_reverse_member_count"), 1)
        row["duplicate_reverse_visual_unit_ids"] = str(row.get("duplicate_reverse_visual_unit_ids") or row["visual_unit_id"])
        row["group_start_ms"] = safe_int(row.get("group_start_ms"), row["time_position_ms"])
        row["group_end_ms"] = safe_int(row.get("group_end_ms"), row["time_position_ms"])
        row["dedup_reason"] = (
            "central_visual_duplicate_group_representative"
            if row.get("duplicate_group_id") else "central_visual_unique"
        )
        row["labels"] = []
        row["generic_label_categories"] = []
        rows.append(row)
    return rows


def fingerprint_one(row: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(row.get("visual_file") or row.get("derived_path") or ""))
    source_path = Path(str(row.get("source_absolute_path") or ""))
    if not path.is_file() or path == source_path:
        return {"visual_unit_id": row["visual_unit_id"], "signature_status": "FAIL", "signature_error": "missing_or_invalid_derived_frame"}
    try:
        with Image.open(path) as image:
            logical = ImageOps.exif_transpose(image).convert("L")
            width, height = logical.size
            grid_image = logical.resize((safe_int(config["grid_cols"]), safe_int(config["grid_rows"])), Image.Resampling.BILINEAR)
            grid = tuple(float(value) for value in grid_image.getdata())
            sample = logical.copy()
            sample.thumbnail((320, 320), Image.Resampling.BILINEAR)
            pixels = [float(value) for value in sample.getdata()]
        mean = sum(pixels) / max(1, len(pixels))
        variance = sum((value - mean) ** 2 for value in pixels) / max(1, len(pixels))
        black_ratio = sum(value <= safe_float(config["black_pixel_luma_threshold"]) for value in pixels) / max(1, len(pixels))
        grid_mean = sum(grid) / len(grid)
        grid_std = math.sqrt(sum((value - grid_mean) ** 2 for value in grid) / len(grid))
        cols = safe_int(config["grid_cols"])
        rows_count = safe_int(config["grid_rows"])
        diffs = []
        for y in range(rows_count):
            for x in range(cols - 1):
                diffs.append(abs(grid[y * cols + x + 1] - grid[y * cols + x]))
        for y in range(rows_count - 1):
            for x in range(cols):
                diffs.append(abs(grid[(y + 1) * cols + x] - grid[y * cols + x]))
        structure = sum(diffs) / max(1, len(diffs))
        near_black = bool(row.get("near_black")) or mean <= safe_float(config["black_luma_mean_threshold"]) or black_ratio >= safe_float(config["black_pixel_ratio_threshold"])
        return {
            "visual_unit_id": row["visual_unit_id"], "signature_status": "PASS", "signature_error": "",
            "width": width, "height": height, "grid": grid, "grid_mean": round(grid_mean, 6),
            "grid_std": round(grid_std, 6), "grid_structure": round(structure, 6),
            "luma_mean_actual": round(mean, 6), "luma_std_actual": round(math.sqrt(variance), 6),
            "black_pixel_ratio": round(black_ratio, 8), "black_rejected": near_black,
        }
    except Exception as exc:
        return {"visual_unit_id": row["visual_unit_id"], "signature_status": "FAIL", "signature_error": f"{type(exc).__name__}:{exc}"}


def attach_signatures(rows: list[dict[str, Any]], config: Mapping[str, Any]) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=max(1, safe_int(config.get("signature_workers"), 8))) as pool:
        results = list(pool.map(lambda row: fingerprint_one(row, config), rows))
    by_id = {str(row["visual_unit_id"]): row for row in rows}
    failures = []
    black_count = 0
    for result in results:
        frame = by_id[str(result["visual_unit_id"])]
        frame.update(result)
        if result["signature_status"] != "PASS":
            failures.append({"visual_unit_id": result["visual_unit_id"], "error": result["signature_error"]})
        if result.get("black_rejected"):
            black_count += 1
    return {
        "grid_signature_success_count": len(rows) - len(failures),
        "grid_signature_failed_count": len(failures),
        "grid_signature_failures": failures[:50],
        "derived_black_or_invalid_count": black_count + len(failures),
        "grid_shape": f"{config['grid_cols']}x{config['grid_rows']}",
    }


def normalize_vector(values: Sequence[Any]) -> tuple[float, ...]:
    numeric = tuple(float(value) for value in values)
    norm = math.sqrt(sum(value * value for value in numeric))
    if not numeric or not norm or not math.isfinite(norm):
        raise RuntimeError("invalid_openclip_vector")
    return tuple(value / norm for value in numeric)


def load_vectors(con: sqlite3.Connection, canonical_ids: set[str]) -> tuple[dict[str, tuple[float, ...]], dict[str, Any]]:
    expected: dict[str, dict[str, str]] = {}
    payload_paths: set[Path] = set()
    for raw in con.execute("SELECT embedding_id,visual_unit_id,vector_key,run_id FROM embeddings"):
        row = dict(raw)
        key = str(row["vector_key"])
        if not key.startswith("jsonl:") or "#" not in key:
            raise RuntimeError(f"unsupported_vector_key:{key}")
        path_text, fragment = key.removeprefix("jsonl:").split("#", 1)
        path = Path(path_text)
        if not path.is_file():
            raise RuntimeError(f"vector_payload_missing:{path}")
        expected[str(row["embedding_id"])] = {
            "visual_unit_id": str(row["visual_unit_id"]), "fragment": fragment, "run_id": str(row["run_id"])
        }
        payload_paths.add(path)
    vectors: dict[str, tuple[float, ...]] = {}
    payload_rows = 0
    for path in sorted(payload_paths):
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                payload_rows += 1
                row = json.loads(line)
                embedding_id = str(row.get("embedding_id") or "")
                visual_id = str(row.get("visual_unit_id") or "")
                if embedding_id not in expected:
                    raise RuntimeError(f"unexpected_vector_embedding:{path}:{line_number}:{embedding_id}")
                if expected[embedding_id]["visual_unit_id"] != visual_id:
                    raise RuntimeError(f"vector_visual_unit_mismatch:{embedding_id}")
                vector = normalize_vector(row.get("vector") or [])
                if visual_id in canonical_ids:
                    vectors[visual_id] = vector
    missing = sorted(canonical_ids - set(vectors))
    if missing:
        raise RuntimeError(f"canonical_openclip_vector_missing:{len(missing)}:{missing[:10]}")
    run_ids = sorted({item["run_id"] for item in expected.values()})
    return vectors, {
        "vector_payload_found": bool(payload_paths), "vector_payload_paths": [str(path) for path in sorted(payload_paths)],
        "vector_payload_row_count": payload_rows, "vector_db_row_count": len(expected),
        "canonical_vector_coverage_count": len(canonical_ids), "canonical_vector_missing_count": 0,
        "openclip_run_id": run_ids[0] if len(run_ids) == 1 else "|".join(run_ids),
        "vector_payload_integrity_status": "PASS",
    }


def normalize_bbox(raw_bbox: Any, width: int, height: int) -> tuple[tuple[float, float, float, float] | None, str]:
    try:
        values = json.loads(raw_bbox) if isinstance(raw_bbox, str) else list(raw_bbox)
        if len(values) != 4:
            return None, "bbox_length_invalid"
        x1, y1, x2, y2 = (float(value) for value in values)
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) > 1.5:
            x1, x2 = x1 / width, x2 / width
            y1, y2 = y1 / height, y2 / height
        x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
        x2, y2 = max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))
        if x2 <= x1 or y2 <= y1:
            return None, "bbox_geometry_invalid"
        return (x1, y1, x2, y2), "normalized_xyxy"
    except Exception:
        return None, "bbox_parse_failed"


def category_for_label(label: str, config: Mapping[str, Any]) -> str:
    name = label.strip().lower()
    if name in set(config["weak_background_labels"]):
        return "weak_background"
    for category, terms in config["generic_label_families"].items():
        if any(name == term or term in name for term in terms):
            return str(category)
    return "other"


def load_labels(
    con: sqlite3.Connection,
    by_visual_id: Mapping[str, MutableMapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = 0
    invalid = 0
    excluded = 0
    run_ids: set[str] = set()
    for raw in con.execute("SELECT visual_unit_id,label,confidence,bbox,run_id FROM visual_labels ORDER BY label_id"):
        row = dict(raw)
        run_ids.add(str(row["run_id"]))
        frame = by_visual_id.get(str(row["visual_unit_id"]))
        if frame is None:
            excluded += 1
            continue
        bbox, status = normalize_bbox(row["bbox"], safe_int(frame.get("width"), 0), safe_int(frame.get("height"), 0))
        if bbox is None:
            invalid += 1
            continue
        normalized += 1
        x1, y1, x2, y2 = bbox
        area = (x2 - x1) * (y2 - y1)
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        label = str(row["label"] or "").strip().lower()
        frame["labels"].append({
            "label": label, "confidence": safe_float(row["confidence"]), "bbox": bbox,
            "bbox_status": status, "area": area,
            "center_distance": math.sqrt((cx - 0.5) ** 2 + (cy - 0.5) ** 2),
            "touches_edge": x1 <= safe_float(config["subject_edge_margin"]) or y1 <= safe_float(config["subject_edge_margin"]) or x2 >= 1.0 - safe_float(config["subject_edge_margin"]) or y2 >= 1.0 - safe_float(config["subject_edge_margin"]),
            "category": category_for_label(label, config),
        })
    for frame in by_visual_id.values():
        frame["generic_label_categories"] = sorted({
            item["category"] for item in frame["labels"]
            if item["category"] not in {"", "other", "weak_background"}
        })
    if len(run_ids) != 1:
        raise RuntimeError(f"yoloe_run_id_not_unique:{sorted(run_ids)}")
    return {
        "bbox_normalized_count": normalized, "bbox_invalid_count": invalid,
        "central_dedup_excluded_label_row_count": excluded,
        "yoloe_run_id": next(iter(run_ids)),
    }


def cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    return sum(a * b for a, b in zip(left, right))


def grid_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[float | None, float | None]:
    a, b = left.get("grid"), right.get("grid")
    if not a or not b or len(a) != len(b):
        return None, None
    mad = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    centered_a = [x - mean_a for x in a]
    centered_b = [x - mean_b for x in b]
    denom = math.sqrt(sum(x * x for x in centered_a) * sum(y * y for y in centered_b))
    corr = sum(x * y for x, y in zip(centered_a, centered_b)) / denom if denom else 1.0
    return mad, corr


def label_jaccard(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    a = {str(item["label"]) for item in left.get("labels") or [] if item.get("label")}
    b = {str(item["label"]) for item in right.get("labels") or [] if item.get("label")}
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def duplicate_evidence(
    left: Mapping[str, Any], right: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    gap = abs(safe_int(left.get("time_position_ms")) - safe_int(right.get("time_position_ms")))
    exact = bool(left.get("derived_sha256")) and left.get("derived_sha256") == right.get("derived_sha256")
    vector = cosine(left.get("vector"), right.get("vector"))
    mad, corr = grid_similarity(left, right)
    labels = label_jaccard(left, right)
    time_close = gap <= safe_int(config["dedup_time_gap_ms"])
    vector_close = vector is not None and vector >= safe_float(config["dedup_vector_cosine_threshold"])
    grid_close = mad is not None and corr is not None and mad <= safe_float(config["dedup_grid_mad_threshold"]) and corr >= safe_float(config["dedup_grid_correlation_threshold"])
    label_close = labels is not None and labels >= safe_float(config["dedup_label_jaccard_threshold"])
    consensus = exact or (time_close and (vector_close or grid_close) and (label_close or (vector_close and grid_close)))
    return {
        "duplicate": consensus, "exact_identity": exact, "time_gap_ms": gap,
        "vector_cosine": vector, "grid_mad": mad, "grid_correlation": corr,
        "label_jaccard": labels, "time_close": time_close,
        "vector_close": vector_close, "grid_close": grid_close, "label_close": label_close,
    }


def text_evidence(frame: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    text_terms = set(config["text_bearing_labels"])
    weak_terms = set(config["weak_text_carrier_labels"])
    strong = [item for item in frame.get("labels") or [] if item["label"] in text_terms and item["label"] not in weak_terms]
    return {
        "has_strong_text": bool(strong),
        "max_confidence": max((safe_float(item["confidence"]) for item in strong), default=0.0),
        "max_area": max((safe_float(item["area"]) for item in strong), default=0.0),
        "aggregate_area": sum(safe_float(item["area"]) for item in strong),
        "labels": sorted({str(item["label"]) for item in strong}),
    }


def attach_generic_scores(
    rows: Sequence[MutableMapping[str, Any]],
    by_source: Mapping[str, Sequence[MutableMapping[str, Any]]],
    config: Mapping[str, Any],
) -> None:
    for source_rows in by_source.values():
        ordered = sorted(source_rows, key=lambda row: (safe_int(row.get("sampled_sequence_index")), str(row["visual_unit_id"])))
        for index, frame in enumerate(ordered):
            labels = frame.get("labels") or []
            unique_labels = {str(item["label"]) for item in labels}
            categories = set(frame.get("generic_label_categories") or [])
            valid_subjects = [
                item for item in labels
                if safe_float(config["reasonable_subject_area_min"]) <= safe_float(item["area"]) <= safe_float(config["reasonable_subject_area_max"])
            ]
            centered = [item for item in valid_subjects if safe_float(item["center_distance"], 1.0) <= 0.45 and not item["touches_edge"]]
            confidence = max((safe_float(item["confidence"]) for item in labels), default=0.0)
            neighbor_scores = []
            for neighbor in ordered[max(0, index - 1): index] + ordered[index + 1: index + 2]:
                neighbor_labels = {str(item["label"]) for item in neighbor.get("labels") or []}
                union = unique_labels | neighbor_labels
                label_change = 1.0 - (len(unique_labels & neighbor_labels) / len(union)) if union else 0.0
                vector_sim = cosine(frame.get("vector"), neighbor.get("vector"))
                mad, _ = grid_similarity(frame, neighbor)
                neighbor_scores.append((label_change, 1.0 - vector_sim if vector_sim is not None else 0.0, min(1.0, safe_float(mad) / 32.0)))
            label_change = max((item[0] for item in neighbor_scores), default=0.0)
            vector_novelty = max((item[1] for item in neighbor_scores), default=0.0)
            grid_change = max((item[2] for item in neighbor_scores), default=0.0)
            text = text_evidence(frame, config)
            components = {
                "generic_category_diversity": min(3.0, len(categories) * 0.9),
                "label_diversity": min(2.0, len(unique_labels) * 0.2),
                "confidence": min(1.5, confidence * 1.5),
                "subject_completeness": min(1.5, len(centered) * 0.5),
                "grid_structure": min(1.0, safe_float(frame.get("grid_structure")) / 24.0),
                "label_change": min(1.0, label_change),
                "vector_novelty": min(1.0, vector_novelty * 2.0),
                "grid_change": min(1.0, grid_change),
                "text_region": 0.5 if text["has_strong_text"] else 0.0,
            }
            score = round(sum(components.values()), 6)
            frame["generic_score_components"] = components
            frame["generic_high_signal_score"] = score
            frame["generic_high_signal"] = score >= safe_float(config["high_signal_score_threshold"])
            frame["text_evidence"] = text
            frame["label_change_score"] = round(label_change, 6)
            frame["vector_novelty_score"] = round(vector_novelty, 6)
            frame["grid_change_score"] = round(grid_change, 6)


def is_screen_recording(path: str, config: Mapping[str, Any]) -> bool:
    lowered = path.lower()
    return any(str(marker).lower() in lowered for marker in config["screen_recording_path_markers"])


def tail_start_ms(raw_rows: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> int | None:
    times = [safe_int(row.get("time_position_ms"), -1) for row in raw_rows if safe_int(row.get("time_position_ms"), -1) >= 0]
    if not times:
        return None
    start, end = min(times), max(times)
    duration = end - start
    window = min(
        safe_int(config["tail_max_window_ms"]),
        max(safe_int(config["tail_min_window_ms"]), int(duration * safe_float(config["tail_duration_ratio"]))),
    )
    return end - window if duration > window else None


def anchor_indices(count: int, stride: int) -> list[int]:
    if count <= 0:
        return []
    middle = (count - 1) // 2
    result = [middle]
    step = 1
    while middle - step * stride >= 0 or middle + step * stride < count:
        left, right = middle - step * stride, middle + step * stride
        if left >= 0:
            result.append(left)
        if right < count:
            result.append(right)
        step += 1
    return result


def anchor_intervals(indices: Sequence[int], count: int, radius: int) -> list[tuple[int, int, int]]:
    if radius < 0:
        raise RuntimeError("coverage_local_radius_must_not_be_negative")
    return [
        (anchor, max(0, anchor - radius), min(count - 1, anchor + radius))
        for anchor in indices
    ]


def tail_only_anchor_remap(
    raw_frames: Sequence[Mapping[str, Any]], original_anchor_index: int,
    radius: int, tail_start: int | None, short_video: bool,
) -> dict[str, Any]:
    original_start = max(0, original_anchor_index - radius)
    original_end = min(len(raw_frames) - 1, original_anchor_index + radius)
    original_valid_times = [
        safe_int(frame.get("time_position_ms"), -1)
        for frame in raw_frames[original_start:original_end + 1]
        if safe_int(frame.get("time_position_ms"), -1) >= 0
    ]
    tail_only = bool(
        not short_video
        and tail_start is not None
        and original_valid_times
        and all(time_ms >= tail_start for time_ms in original_valid_times)
    )
    if not tail_only:
        return {
            "tail_only": False, "remap_failed": False,
            "original_anchor_index": original_anchor_index,
            "original_interval_start_index": original_start,
            "original_interval_end_index": original_end,
            "effective_anchor_index": original_anchor_index,
            "effective_interval_start_index": original_start,
            "effective_interval_end_index": original_end,
            "anchor_remap_reason": "",
        }
    non_tail_indices = [
        index for index, frame in enumerate(raw_frames)
        if 0 <= safe_int(frame.get("time_position_ms"), -1) < safe_int(tail_start)
    ]
    if not non_tail_indices:
        return {
            "tail_only": True, "remap_failed": True,
            "original_anchor_index": original_anchor_index,
            "original_interval_start_index": original_start,
            "original_interval_end_index": original_end,
            "effective_anchor_index": -1,
            "effective_interval_start_index": -1,
            "effective_interval_end_index": -1,
            "anchor_remap_reason": "tail_only_anchor_remap_failed_no_non_tail_sampled_frame",
        }
    effective_anchor = non_tail_indices[-1]
    return {
        "tail_only": True, "remap_failed": False,
        "original_anchor_index": original_anchor_index,
        "original_interval_start_index": original_start,
        "original_interval_end_index": original_end,
        "effective_anchor_index": effective_anchor,
        "effective_interval_start_index": max(0, effective_anchor - radius),
        "effective_interval_end_index": min(len(raw_frames) - 1, effective_anchor + radius),
        "anchor_remap_reason": "tail_only_anchor_remapped_to_last_non_tail",
    }


def hard_rejection_reasons(
    frame: Mapping[str, Any], *, screen_recording: bool, tail_start: int | None
) -> list[str]:
    reasons = []
    if frame.get("signature_status") != "PASS":
        reasons.append("missing_or_invalid_derived_frame")
    if frame.get("black_rejected"):
        reasons.append("near_black")
    if tail_start is not None and safe_int(frame.get("time_position_ms")) >= tail_start:
        reasons.append("tail_landing")
    if screen_recording:
        reasons.append("screen_recording_routed_to_ocr")
    if frame.get("identity_status") not in {"unique", "canonical", "blocked_decoder"}:
        reasons.append("noncanonical_central_duplicate")
    return reasons


def coverage_rank(
    frame: Mapping[str, Any], anchor_time: int, config: Mapping[str, Any]
) -> tuple[Any, ...]:
    high = int(bool(frame.get("generic_high_signal")))
    subjects = [item for item in frame.get("labels") or [] if not item.get("touches_edge")]
    composition = min(3.0, len(subjects) * 0.4 + max((1.0 - safe_float(item.get("center_distance"), 1.0) for item in subjects), default=0.0))
    grid_nonflat = int(safe_float(frame.get("grid_std")) >= safe_float(config["grid_flat_std_threshold"]))
    return (
        -high, -safe_float(frame.get("generic_high_signal_score")), -composition,
        -grid_nonflat, -safe_float(frame.get("grid_structure")),
        abs(safe_int(frame.get("time_position_ms")) - anchor_time),
        safe_int(frame.get("time_position_ms")), str(frame.get("visual_unit_id")),
    )


def evidence_reason_codes(frame: Mapping[str, Any]) -> list[str]:
    reasons = []
    if frame.get("generic_high_signal"):
        reasons.append("generic_high_signal_evidence")
    if frame.get("generic_label_categories"):
        reasons.append("generic_category_diversity")
    if safe_float(frame.get("label_change_score")) > 0.45:
        reasons.append("adjacent_label_set_change")
    if safe_float(frame.get("vector_novelty_score")) > 0.12:
        reasons.append("openclip_local_novelty")
    if safe_float(frame.get("grid_change_score")) > 0.15:
        reasons.append("grid_structure_change")
    if (frame.get("text_evidence") or {}).get("has_strong_text"):
        reasons.append("obvious_text_region")
    return reasons


def make_candidate(
    frame: Mapping[str, Any], *, queue_type: str, role: str, score: float,
    reasons: Sequence[str], run_id: str, mode: str, hashes: Mapping[str, str],
    lineage: Mapping[str, str], config: Mapping[str, Any], anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    time_ms = safe_int(frame.get("time_position_ms"), -1)
    raw_group_start = safe_int(frame.get("raw_source_start_ms"), time_ms)
    raw_group_end = safe_int(frame.get("raw_source_end_ms"), time_ms)
    segment_start = max(0, min(time_ms - safe_int(config["search_segment_pre_buffer_ms"]), safe_int(frame.get("group_start_ms"), time_ms)))
    segment_end = min(raw_group_end, max(time_ms + safe_int(config["search_segment_post_buffer_ms"]), safe_int(frame.get("group_end_ms"), time_ms)))
    label_counter = Counter(str(item["label"]) for item in frame.get("labels") or [])
    return {
        "candidate_id": stable_id("cand_v23_", POLICY_VERSION, queue_type, role, frame["visual_unit_id"]),
        "run_id": run_id, "queue_type": queue_type, "candidate_role": role,
        "high_value_category": role, "candidate_score": round(float(score), 6),
        "source_content_id": str(frame["source_content_id"]),
        "visual_unit_id": str(frame["visual_unit_id"]),
        "canonical_visual_unit_id": str(frame.get("canonical_visual_unit_id") or frame["visual_unit_id"]),
        "duplicate_group_id": str(frame.get("duplicate_group_id") or ""),
        "duplicate_reverse_member_count": safe_int(frame.get("duplicate_reverse_member_count"), 1),
        "duplicate_reverse_visual_unit_ids": str(frame.get("duplicate_reverse_visual_unit_ids") or frame["visual_unit_id"]),
        "derived_id": str(frame["derived_id"]), "frame_index": safe_int(frame.get("frame_index"), -1),
        "time_position_ms": time_ms, "canonical_time_ms": safe_int(frame.get("canonical_time_ms"), time_ms),
        "group_start_ms": safe_int(frame.get("group_start_ms"), time_ms),
        "group_end_ms": safe_int(frame.get("group_end_ms"), time_ms),
        "segment_start_ms": segment_start, "segment_end_ms": segment_end,
        "source_relative_path": str(frame.get("source_relative_path") or ""),
        "visual_file": str(frame.get("visual_file") or ""), "media_type": str(frame.get("media_type") or ""),
        "coverage_anchor_index": safe_int((anchor or {}).get("anchor_index"), -1),
        "coverage_anchor_visual_unit_id": str((anchor or {}).get("anchor_visual_unit_id") or ""),
        "reason_codes": "|".join(dict.fromkeys(str(item) for item in reasons if item)),
        "dedup_reason": str(frame.get("dedup_reason") or ""),
        "black_frame_status": "near_black" if frame.get("black_rejected") else "ok",
        "labels": "|".join(f"{label}:{count}" for label, count in label_counter.most_common()),
        "generic_label_categories": "|".join(frame.get("generic_label_categories") or []),
        "policy_version": POLICY_VERSION, "script_version": SCRIPT_VERSION,
        "script_sha256": hashes["script_sha256"], "config_sha256": hashes["config_sha256"],
        "rule_document_sha256": hashes["rule_document_sha256"],
        "central_dedup_run_id": lineage["central_dedup_run_id"],
        "yoloe_run_id": lineage["yoloe_run_id"], "openclip_run_id": lineage["openclip_run_id"],
        "execution_mode": mode,
    }


def supplement_cap(raw_count: int, config: Mapping[str, Any]) -> int:
    if raw_count <= 20:
        return safe_int(config["high_signal_supplement_cap_short"])
    if raw_count <= 100:
        return safe_int(config["high_signal_supplement_cap_medium"])
    return safe_int(config["high_signal_supplement_cap_long"])


def select_screen_ocr(
    frames: Sequence[MutableMapping[str, Any]], config: Mapping[str, Any], stats: Counter[str]
) -> list[MutableMapping[str, Any]]:
    candidates = [frame for frame in frames if frame.get("signature_status") == "PASS" and not frame.get("black_rejected")]
    candidates.sort(key=lambda frame: (
        -int(bool((frame.get("text_evidence") or {}).get("has_strong_text"))),
        -safe_float((frame.get("text_evidence") or {}).get("aggregate_area")),
        -safe_float(frame.get("grid_structure")), safe_int(frame.get("time_position_ms")), str(frame["visual_unit_id"]),
    ))
    selected: list[MutableMapping[str, Any]] = []
    for frame in candidates:
        if len(selected) >= safe_int(config["screen_recording_ocr_cap"]):
            break
        if any(abs(safe_int(frame["time_position_ms"]) - safe_int(other["time_position_ms"])) < safe_int(config["screen_recording_ocr_min_gap_ms"]) for other in selected):
            continue
        duplicate = False
        for other in selected:
            stats["vector_grid_label_time_pair_evaluation_count"] += 1
            if duplicate_evidence(frame, other, config)["duplicate"]:
                duplicate = True
                stats["screen_ocr_duplicate_drop_count"] += 1
                break
        if not duplicate:
            selected.append(frame)
    return sorted(selected, key=lambda frame: (safe_int(frame["time_position_ms"]), str(frame["visual_unit_id"])))


def select_normal_video_ocr(
    frames: Sequence[MutableMapping[str, Any]], config: Mapping[str, Any]
) -> list[MutableMapping[str, Any]]:
    qualified = []
    for frame in frames:
        text = frame.get("text_evidence") or {}
        if frame.get("signature_status") != "PASS" or frame.get("black_rejected"):
            continue
        if not text.get("has_strong_text"):
            continue
        if safe_float(text.get("max_confidence")) < safe_float(config["normal_video_ocr_min_confidence"]):
            continue
        if safe_float(text.get("max_area")) < safe_float(config["normal_video_ocr_min_bbox_area"]) and safe_float(text.get("aggregate_area")) < safe_float(config["normal_video_ocr_min_aggregate_area"]):
            continue
        qualified.append(frame)
    qualified.sort(key=lambda frame: (
        -safe_float((frame.get("text_evidence") or {}).get("aggregate_area")),
        -safe_float((frame.get("text_evidence") or {}).get("max_confidence")),
        safe_int(frame.get("time_position_ms")), str(frame["visual_unit_id"]),
    ))
    return qualified[: safe_int(config["normal_video_ocr_cap"])]


def select_candidates(
    canonical_rows: list[dict[str, Any]], raw_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
    config: Mapping[str, Any], run_id: str, mode: str, hashes: Mapping[str, str],
    lineage: Mapping[str, str], timelapse_representatives: Mapping[str, str],
) -> dict[str, Any]:
    rows = copy.deepcopy(canonical_rows)
    by_id = {str(row["visual_unit_id"]): row for row in rows}
    by_source: dict[str, list[MutableMapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[str(row["source_content_id"])].append(row)
    attach_generic_scores(rows, by_source, config)
    stats: Counter[str] = Counter()
    q_rows: list[dict[str, Any]] = []
    o_rows: list[dict[str, Any]] = []
    coverage_reports: list[dict[str, Any]] = []
    budgets: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}

    for frame in rows:
        if frame["media_type"] == "video":
            decisions[str(frame["visual_unit_id"])] = {
                **{key: frame.get(key) for key in (
                    "source_content_id", "visual_unit_id", "canonical_visual_unit_id", "duplicate_group_id",
                    "derived_id", "frame_index", "time_position_ms", "canonical_time_ms", "group_start_ms", "group_end_ms",
                    "source_relative_path", "visual_file", "duplicate_reverse_member_count", "duplicate_reverse_visual_unit_ids",
                )},
                "candidate_score": frame.get("generic_high_signal_score"),
                "labels": "|".join(sorted({str(item["label"]) for item in frame.get("labels") or []})),
                "generic_label_categories": "|".join(frame.get("generic_label_categories") or []),
                "black_frame_status": "near_black" if frame.get("black_rejected") else "ok",
                "qwen_selected": False, "qwen_role": "", "ocr_selected": False, "ocr_role": "",
                "coverage_anchor_index": -1, "coverage_anchor_indices": [],
                "selection_reason_codes": [], "decision_reason_codes": [],
                "high_signal_pool_member": bool(frame.get("generic_high_signal")),
                "high_signal_role": "video_high_signal_keyframe" if frame.get("generic_high_signal") else "",
            }

    for source_id, raw_frames in sorted(raw_by_source.items()):
        canonical_frames = sorted(
            [frame for frame in by_source.get(source_id, []) if frame["media_type"] == "video"],
            key=lambda frame: (safe_int(frame.get("sampled_sequence_index")), safe_int(frame.get("time_position_ms")), safe_int(frame.get("frame_index")), str(frame["visual_unit_id"])),
        )
        if not canonical_frames:
            stats["video_group_without_canonical_frames_count"] += 1
            continue
        raw_start = min(safe_int(frame["time_position_ms"]) for frame in raw_frames)
        raw_end = max(safe_int(frame["time_position_ms"]) for frame in raw_frames)
        for frame in canonical_frames:
            frame["raw_source_start_ms"] = raw_start
            frame["raw_source_end_ms"] = raw_end
        path = str(canonical_frames[0].get("source_relative_path") or "")
        screen = is_screen_recording(path, config)
        if screen:
            stats["screen_recording_video_group_count"] += 1
            screen_selected = select_screen_ocr(canonical_frames, config, stats)
            for frame in screen_selected:
                reasons = ["screen_recording_routed_to_ocr", "screen_ocr_black_and_duplicate_gate_pass"]
                candidate = make_candidate(
                    frame, queue_type="ocr_trigger", role="ocr_screen_recording_frame",
                    score=max(1.0, safe_float(frame.get("generic_high_signal_score"))), reasons=reasons,
                    run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config,
                )
                o_rows.append(candidate)
                decision = decisions[str(frame["visual_unit_id"])]
                decision.update({"ocr_selected": True, "ocr_role": candidate["candidate_role"], "selection_reason_codes": reasons})
            budgets.append({
                "source_content_id": source_id, "source_relative_path": path, "screen_recording": 1,
                "raw_step02_frame_count": len(raw_frames), "canonical_frame_count": len(canonical_frames),
                "coverage_anchor_count": 0, "coverage_selected_count": 0, "high_signal_pool_count": 0,
                "supplement_count": 0, "qwen_video_count": 0, "ocr_count": len(screen_selected),
                "group_start_ms": raw_start, "group_end_ms": raw_end,
            })
            continue

        stats["normal_video_group_count"] += 1
        valid_sampled_frame_count = sum(
            1 for frame in raw_frames
            if safe_int(frame.get("time_position_ms"), -1) >= 0
            and safe_int(frame.get("frame_index"), -1) >= 0
        )
        stride = safe_int(config["coverage_stride_frames"])
        local_radius = safe_int(config["coverage_local_radius_frames"])
        short_video = valid_sampled_frame_count < stride
        if short_video:
            stats["short_video_count"] += 1
        tail_start = tail_start_ms(raw_frames, config)
        indices = anchor_indices(len(raw_frames), stride)
        intervals = anchor_intervals(indices, len(raw_frames), local_radius)
        stats["coverage_anchor_total_count"] += len(intervals)
        selected_coverage: list[MutableMapping[str, Any]] = []
        covered_anchor_count = 0
        source_reports = []
        for anchor_number, (anchor_index, interval_start, interval_end) in enumerate(intervals):
            remap = tail_only_anchor_remap(
                raw_frames, anchor_index, local_radius, tail_start, short_video,
            )
            stats["coverage_local_radius_applied_anchor_count"] += 1
            if remap["tail_only"]:
                stats["tail_only_anchor_count"] += 1
            original_raw_anchor = raw_frames[anchor_index]
            if remap["remap_failed"]:
                stats["tail_anchor_remap_failed_count"] += 1
                stats["coverage_refill_failed_count"] += 1
                stats["coverage_missing_count"] += 1
                source_reports.append({
                    "source_content_id": source_id, "anchor_number": anchor_number,
                    "anchor_index": anchor_index,
                    "anchor_visual_unit_id": original_raw_anchor["visual_unit_id"],
                    "anchor_time_ms": safe_int(original_raw_anchor["time_position_ms"]),
                    **{key: remap[key] for key in (
                        "original_anchor_index", "original_interval_start_index",
                        "original_interval_end_index", "effective_anchor_index",
                        "effective_interval_start_index", "effective_interval_end_index",
                        "anchor_remap_reason",
                    )},
                    "tail_start_ms": tail_start, "canonical_candidate_count": 0,
                    "eligible_candidate_count": 0, "selected_visual_unit_id": "",
                    "selected_time_ms": -1, "selected_role": "",
                    "central_anchor_excluded": int(not bool(original_raw_anchor["eligible_for_heavy_models"])),
                    "refill_status": "failed_tail_only_anchor_remap",
                })
                continue
            effective_anchor_index = safe_int(remap["effective_anchor_index"])
            interval_start = safe_int(remap["effective_interval_start_index"])
            interval_end = safe_int(remap["effective_interval_end_index"])
            raw_anchor = raw_frames[effective_anchor_index]
            anchor_time = safe_int(raw_anchor["time_position_ms"])
            interval_raw = raw_frames[interval_start:interval_end + 1]
            interval_canonical_ids = {
                str(frame.get("canonical_visual_unit_id") or "")
                for frame in interval_raw
                if frame.get("canonical_visual_unit_id")
            }
            pool_by_id = {
                str(frame["visual_unit_id"]): frame
                for frame in canonical_frames
                if interval_start <= safe_int(frame.get("sampled_sequence_index"), -1) <= interval_end
                or str(frame["visual_unit_id"]) in interval_canonical_ids
            }
            pool = list(pool_by_id.values())
            stats["coverage_local_candidate_pool_count"] += len(pool)
            stats["coverage_local_candidate_evaluation_count"] += len(pool)
            stats["local_candidate_evaluation_count"] += len(pool)
            eligible = []
            for frame in pool:
                reject = hard_rejection_reasons(frame, screen_recording=False, tail_start=tail_start)
                frame.setdefault("hard_rejection_reasons", reject)
                if not reject:
                    eligible.append(frame)
            short_video_tail_fallback = False
            if not eligible and short_video:
                fallback = [
                    frame for frame in pool
                    if frame.get("signature_status") == "PASS"
                    and not frame.get("black_rejected")
                    and frame.get("identity_status") in {"unique", "canonical", "blocked_decoder"}
                ]
                if fallback:
                    eligible = [max(
                        fallback,
                        key=lambda frame: (
                            safe_int(frame.get("time_position_ms")),
                            safe_int(frame.get("frame_index")),
                            str(frame.get("visual_unit_id")),
                        ),
                    )]
                    short_video_tail_fallback = True
            ranked = sorted(eligible, key=lambda frame: coverage_rank(frame, anchor_time, config))
            selected = None
            selected_evidence = None
            top_was_duplicate = False
            candidate_refill = False
            for position, frame in enumerate(ranked):
                duplicate = False
                evidence = None
                for previous in selected_coverage:
                    stats["vector_grid_label_time_pair_evaluation_count"] += 1
                    evidence = duplicate_evidence(frame, previous, config)
                    if evidence["duplicate"]:
                        duplicate = True
                        break
                if not duplicate:
                    selected = frame
                    selected_evidence = evidence
                    if position > 0:
                        candidate_refill = True
                    break
                top_was_duplicate = True
            dedup_kept = False
            if selected is None and ranked:
                selected = ranked[0]
                dedup_kept = True
                stats["coverage_refill_unavailable_kept_count"] += 1
            if selected is None:
                if remap["tail_only"]:
                    stats["tail_anchor_remap_failed_count"] += 1
                stats["coverage_refill_failed_count"] += 1
                stats["coverage_missing_count"] += 1
                source_reports.append({
                    "source_content_id": source_id, "anchor_number": anchor_number,
                    "anchor_index": anchor_index,
                    "anchor_visual_unit_id": original_raw_anchor["visual_unit_id"],
                    "anchor_time_ms": safe_int(original_raw_anchor["time_position_ms"]),
                    **{key: remap[key] for key in (
                        "original_anchor_index", "original_interval_start_index",
                        "original_interval_end_index", "effective_anchor_index",
                        "effective_interval_start_index", "effective_interval_end_index",
                        "anchor_remap_reason",
                    )},
                    "tail_start_ms": tail_start,
                    "interval_start_index": interval_start, "interval_end_index": interval_end,
                    "canonical_candidate_count": len(pool),
                    "eligible_candidate_count": 0, "selected_visual_unit_id": "", "selected_role": "",
                    "central_anchor_excluded": int(not bool(raw_anchor["eligible_for_heavy_models"])),
                    "refill_status": "failed_no_canonical_candidate",
                })
                continue
            high = bool(selected.get("generic_high_signal"))
            role = "video_coverage_high_signal_overlap" if high else "video_coverage_keyframe"
            reasons = ["coverage_anchor_every_six_step02_frames", "anchor_local_deterministic_best"] + evidence_reason_codes(selected)
            if remap["tail_only"]:
                stats["tail_anchor_remap_count"] += 1
                reasons.append("tail_only_anchor_remapped_to_last_non_tail")
            if not raw_anchor["eligible_for_heavy_models"]:
                stats["coverage_anchor_excluded_by_central_dedup_count"] += 1
                stats["coverage_refill_after_central_dedup_count"] += 1
                reasons.append("coverage_anchor_refilled_after_central_dedup")
            if candidate_refill or not raw_anchor["eligible_for_heavy_models"]:
                stats["coverage_refill_count"] += 1
            if top_was_duplicate:
                reasons.append("coverage_refill_after_multi_evidence_dedup")
            if dedup_kept:
                reasons.append("dedup_kept_for_coverage")
            if short_video_tail_fallback:
                stats["short_video_tail_fallback_count"] += 1
                reasons.append("short_video_tail_fallback")
            anchor_meta = {"anchor_index": anchor_index, "anchor_visual_unit_id": original_raw_anchor["visual_unit_id"]}
            decision = decisions[str(selected["visual_unit_id"])]
            selected_already_used = any(
                str(frame["visual_unit_id"]) == str(selected["visual_unit_id"])
                for frame in selected_coverage
            )
            if selected_already_used:
                reasons.append("coverage_reused_existing_canonical_representative")
                if remap["tail_only"]:
                    stats["tail_anchor_reused_existing_canonical_count"] += 1
                for candidate in q_rows:
                    if candidate["queue_type"] == "qwenvl_high_value" and candidate["visual_unit_id"] == selected["visual_unit_id"]:
                        candidate["reason_codes"] = "|".join(dict.fromkeys([
                            *str(candidate["reason_codes"]).split("|"),
                            *reasons,
                            "coverage_reused_across_anchor_intervals",
                        ]))
                        break
            else:
                candidate = make_candidate(
                    selected, queue_type="qwenvl_high_value", role=role,
                    score=safe_float(selected.get("generic_high_signal_score")), reasons=reasons,
                    run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config, anchor=anchor_meta,
                )
                q_rows.append(candidate)
                selected_coverage.append(selected)
            covered_anchor_count += 1
            decision["coverage_anchor_indices"].append(anchor_index)
            decision.update({
                "qwen_selected": True, "qwen_role": role,
                "coverage_anchor_index": decision["coverage_anchor_indices"][0],
                "selection_reason_codes": list(dict.fromkeys([*decision["selection_reason_codes"], *reasons])),
            })
            source_reports.append({
                "source_content_id": source_id, "anchor_number": anchor_number,
                "anchor_index": anchor_index,
                "anchor_visual_unit_id": original_raw_anchor["visual_unit_id"],
                "anchor_time_ms": safe_int(original_raw_anchor["time_position_ms"]),
                **{key: remap[key] for key in (
                    "original_anchor_index", "original_interval_start_index",
                    "original_interval_end_index", "effective_anchor_index",
                    "effective_interval_start_index", "effective_interval_end_index",
                    "anchor_remap_reason",
                )},
                "tail_start_ms": tail_start, "interval_start_index": interval_start,
                "interval_end_index": interval_end, "canonical_candidate_count": len(pool),
                "eligible_candidate_count": len(eligible), "selected_visual_unit_id": selected["visual_unit_id"],
                "selected_time_ms": selected["time_position_ms"], "selected_role": role,
                "central_anchor_excluded": int(not bool(raw_anchor["eligible_for_heavy_models"])),
                "coverage_local_radius_frames": local_radius,
                "valid_sampled_frame_count": valid_sampled_frame_count,
                "short_video": int(short_video),
                "refill_status": "central_refill" if not raw_anchor["eligible_for_heavy_models"] else ("reused_canonical" if selected_already_used else ("candidate_refill" if candidate_refill else "not_needed")),
                "selected_evidence": json_text(selected_evidence or {}),
            })
        coverage_reports.extend(source_reports)
        if covered_anchor_count == len(intervals):
            stats["normal_video_group_with_coverage_count"] += 1
        else:
            stats["normal_video_group_missing_coverage_count"] += 1

        selected_ids = {str(frame["visual_unit_id"]) for frame in selected_coverage}
        high_signal_pool = [
            frame for frame in canonical_frames
            if frame.get("generic_high_signal") and str(frame["visual_unit_id"]) not in selected_ids
            and not hard_rejection_reasons(frame, screen_recording=False, tail_start=tail_start)
        ]
        for frame in high_signal_pool:
            decisions[str(frame["visual_unit_id"])]["decision_reason_codes"].append("video_high_signal_keyframe_pool_member")
        high_signal_pool.sort(key=lambda frame: (-safe_float(frame.get("generic_high_signal_score")), safe_int(frame.get("time_position_ms")), str(frame["visual_unit_id"])))
        supplements = []
        cap = supplement_cap(len(raw_frames), config)
        for frame in high_signal_pool:
            if len(supplements) >= cap:
                break
            if selected_coverage and min(abs(safe_int(frame["time_position_ms"]) - safe_int(other["time_position_ms"])) for other in selected_coverage) < safe_int(config["high_signal_supplement_min_gap_ms"]):
                continue
            duplicate = False
            for other in [*selected_coverage, *supplements]:
                stats["vector_grid_label_time_pair_evaluation_count"] += 1
                if duplicate_evidence(frame, other, config)["duplicate"]:
                    duplicate = True
                    stats["high_signal_supplement_duplicate_reject_count"] += 1
                    break
            if duplicate:
                continue
            supplements.append(frame)
            reasons = ["video_high_signal_keyframe", "high_signal_supplement_novel_after_multi_evidence_dedup"] + evidence_reason_codes(frame)
            candidate = make_candidate(
                frame, queue_type="qwenvl_high_value", role="video_high_signal_supplement",
                score=safe_float(frame.get("generic_high_signal_score")), reasons=reasons,
                run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config,
            )
            q_rows.append(candidate)
            decisions[str(frame["visual_unit_id"])].update({"qwen_selected": True, "qwen_role": "video_high_signal_supplement", "selection_reason_codes": reasons})

        normal_ocr = select_normal_video_ocr(canonical_frames, config)
        for frame in normal_ocr:
            text = frame["text_evidence"]
            reasons = [
                "normal_video_narrow_ocr", "obvious_large_text_bbox_and_confidence_gate",
                f"text_max_confidence:{safe_float(text['max_confidence']):.6f}",
                f"text_max_bbox_area:{safe_float(text['max_area']):.6f}",
            ]
            candidate = make_candidate(
                frame, queue_type="ocr_trigger", role="ocr_normal_video_large_text_frame",
                score=max(1.0, safe_float(text["aggregate_area"]) * 10.0 + safe_float(text["max_confidence"])),
                reasons=reasons, run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config,
            )
            o_rows.append(candidate)
            decisions[str(frame["visual_unit_id"])].update({"ocr_selected": True, "ocr_role": candidate["candidate_role"], "selection_reason_codes": reasons})
        budgets.append({
            "source_content_id": source_id, "source_relative_path": path, "screen_recording": 0,
            "raw_step02_frame_count": len(raw_frames), "canonical_frame_count": len(canonical_frames),
            "valid_sampled_frame_count": valid_sampled_frame_count, "short_video": int(short_video),
            "coverage_stride_frames": stride, "coverage_local_radius_frames": local_radius,
            "coverage_anchor_count": len(intervals), "coverage_selected_count": covered_anchor_count,
            "coverage_unique_representative_count": len(selected_coverage),
            "high_signal_pool_count": len(high_signal_pool), "supplement_count": len(supplements),
            "qwen_video_count": len(selected_coverage) + len(supplements), "ocr_count": len(normal_ocr),
            "group_start_ms": raw_start, "group_end_ms": raw_end,
        })

    image_rows = [frame for frame in rows if frame["media_type"] == "image"]
    selected_images: set[str] = set()
    for sequence_id, visual_id in sorted(timelapse_representatives.items()):
        frame = by_id.get(visual_id)
        if not frame or frame["media_type"] != "image" or frame.get("black_rejected"):
            continue
        frame["raw_source_start_ms"] = frame["raw_source_end_ms"] = max(0, safe_int(frame.get("time_position_ms"), 0))
        reasons = ["timelapse_representative_from_central_database", f"sequence_id:{sequence_id}"]
        q_rows.append(make_candidate(
            frame, queue_type="qwenvl_high_value", role="image_timelapse_representative",
            score=max(1.0, safe_float(frame.get("generic_high_signal_score"))), reasons=reasons,
            run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config,
        ))
        selected_images.add(visual_id)
    for frame in image_rows:
        visual_id = str(frame["visual_unit_id"])
        if visual_id in selected_images or frame.get("black_rejected") or frame.get("signature_status") != "PASS":
            continue
        if safe_float(frame.get("generic_high_signal_score")) < safe_float(config["image_high_signal_score_threshold"]):
            continue
        frame["raw_source_start_ms"] = frame["raw_source_end_ms"] = max(0, safe_int(frame.get("time_position_ms"), 0))
        reasons = ["image_generic_high_signal_candidate"] + evidence_reason_codes(frame)
        q_rows.append(make_candidate(
            frame, queue_type="qwenvl_high_value", role="image_generic_visual_signal_candidate",
            score=safe_float(frame.get("generic_high_signal_score")), reasons=reasons,
            run_id=run_id, mode=mode, hashes=hashes, lineage=lineage, config=config,
        ))
        selected_images.add(visual_id)

    q_rows.sort(key=lambda row: (row["media_type"], row["source_relative_path"], safe_int(row["time_position_ms"]), row["visual_unit_id"], row["candidate_role"]))
    o_rows.sort(key=lambda row: (row["source_relative_path"], safe_int(row["time_position_ms"]), row["visual_unit_id"], row["candidate_role"]))
    for decision in decisions.values():
        if not decision["qwen_selected"] and not decision["ocr_selected"]:
            decision["decision_reason_codes"].append("not_selected_by_v23_policy")
    return {
        "rows": rows, "q_rows": q_rows, "o_rows": o_rows,
        "decisions": sorted(decisions.values(), key=lambda row: (row["source_relative_path"], safe_int(row["time_position_ms"]), row["visual_unit_id"])),
        "coverage_reports": coverage_reports, "video_budget": budgets, "stats": stats,
    }


def load_timelapse_representatives(con: sqlite3.Connection, canonical_ids: set[str]) -> dict[str, str]:
    if not object_exists(con, "step02_image_timelapse_keyframes"):
        return {}
    rows = [dict(row) for row in con.execute("SELECT * FROM step02_image_timelapse_keyframes")]
    by_sequence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        visual_id = str(row.get("visual_unit_id") or "")
        if visual_id in canonical_ids:
            by_sequence[str(row.get("sequence_id") or "")].append(row)
    order = {"middle": 0, "first": 1, "last": 2}
    result = {}
    for sequence_id, members in by_sequence.items():
        members.sort(key=lambda row: (order.get(str(row.get("representative_position") or ""), 9), str(row.get("visual_unit_id") or "")))
        if members:
            result[sequence_id] = str(members[0]["visual_unit_id"])
    return result


def semantic_candidate_digest(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for name in ("q_rows", "o_rows", "coverage_reports", "video_budget"):
        for row in result[name]:
            filtered = {key: value for key, value in row.items() if key != "run_id"}
            digest.update(json_text(filtered).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def reverse_mapping_rows(raw_rows: Sequence[Mapping[str, Any]], canonical_by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in raw_rows:
        canonical_id = str(row.get("canonical_visual_unit_id") or "")
        canonical = canonical_by_id.get(canonical_id) or {}
        time_ms = safe_int(row.get("time_position_ms"), -1)
        output.append({
            "source_content_id": row.get("source_content_id"), "visual_unit_id": row.get("visual_unit_id"),
            "canonical_visual_unit_id": canonical_id, "duplicate_group_id": row.get("visual_duplicate_group_id") or "",
            "derived_id": row.get("derived_id"), "frame_index": safe_int(row.get("frame_index"), -1),
            "time_position_ms": time_ms, "canonical_time_ms": safe_int(canonical.get("canonical_time_ms"), time_ms),
            "group_start_ms": safe_int(canonical.get("group_start_ms"), time_ms),
            "group_end_ms": safe_int(canonical.get("group_end_ms"), time_ms),
            "dedup_reason": row.get("identity_status"),
            "eligible_for_heavy_models": bool(row.get("eligible_for_heavy_models")),
        })
    return sorted(output, key=lambda row: (str(row["source_content_id"]), safe_int(row["time_position_ms"]), str(row["visual_unit_id"])))


def build_summary(
    result: Mapping[str, Any], pre: Mapping[str, Any], signature_stats: Mapping[str, Any],
    vector_stats: Mapping[str, Any], label_stats: Mapping[str, Any], mode: str,
    deterministic: bool, db_unchanged: bool | None,
) -> dict[str, Any]:
    q_rows, o_rows = result["q_rows"], result["o_rows"]
    q_video = [row for row in q_rows if row["candidate_role"] in VIDEO_QWEN_ROLES]
    q_ids = {row["visual_unit_id"] for row in q_rows}
    o_ids = {row["visual_unit_id"] for row in o_rows}
    row_by_id = {str(row["visual_unit_id"]): row for row in result["rows"]}
    black_ids = {visual_id for visual_id, row in row_by_id.items() if row.get("black_rejected")}
    canonical_ids = set(row_by_id)
    screen_leaks = sum(1 for row in q_video if is_screen_recording(row["source_relative_path"], pre["config"]))
    stats: Counter[str] = result["stats"]
    central_leaks = len((q_ids | o_ids) - canonical_ids)
    role_counts = Counter(row["candidate_role"] for row in q_rows)
    configured_radius = safe_int(pre["config"].get("coverage_local_radius_frames"), -1)
    radius_applied = (
        configured_radius == 3
        and stats["coverage_anchor_total_count"] > 0
        and stats["coverage_local_radius_applied_anchor_count"] == stats["coverage_anchor_total_count"]
    )
    gates = {
        "normal_video_coverage_complete": stats["normal_video_group_missing_coverage_count"] == 0,
        "coverage_refill_failed_zero": stats["coverage_refill_failed_count"] == 0,
        "coverage_missing_zero": stats["coverage_missing_count"] == 0,
        "non_short_video_tail_fallback_zero": stats["non_short_video_tail_fallback_count"] == 0,
        "tail_only_anchor_accounting_consistent": (
            stats["tail_anchor_remap_count"] + stats["tail_anchor_remap_failed_count"]
            == stats["tail_only_anchor_count"]
        ),
        "tail_anchor_remap_failed_zero": stats["tail_anchor_remap_failed_count"] == 0,
        "coverage_local_radius_applied": radius_applied,
        "black_qwen_leak_zero": not (q_ids & black_ids),
        "black_ocr_leak_zero": not (o_ids & black_ids),
        "screen_recording_qwen_leak_zero": screen_leaks == 0,
        "central_duplicate_queue_leak_zero": central_leaks == 0,
        "vector_payload_valid": vector_stats["vector_payload_integrity_status"] == "PASS",
        "grid_executed": signature_stats["grid_signature_success_count"] > 0 and signature_stats["grid_signature_failed_count"] == 0,
        "coverage_executed": stats["coverage_anchor_total_count"] > 0,
        "local_candidate_evaluation_executed": stats["local_candidate_evaluation_count"] > 0,
        "multi_evidence_pair_evaluation_executed": stats["vector_grid_label_time_pair_evaluation_count"] > 0,
        "bbox_normalization_valid": label_stats["bbox_invalid_count"] == 0,
        "deterministic_recompute_match": deterministic,
    }
    if mode == "dry-run":
        gates["sqlite_unchanged"] = bool(db_unchanged)
    technical = "PASS" if all(gates.values()) else "FAIL"
    return {
        "validation_status": technical, "technical_status": technical,
        "dry_run_status": technical if mode == "dry-run" else "NOT_APPLICABLE",
        "policy_status": "REVIEW" if pre["config"]["policy_status"] == "FROZEN_CANDIDATE" else "PASS",
        "commit_status": "DO_NOT_COMMIT" if mode == "dry-run" else "PENDING_COMMIT",
        "policy_reason_codes": ["v23_frozen_candidate_pending_single_human_freeze_review"] if pre["config"]["policy_status"] == "FROZEN_CANDIDATE" else [],
        "policy_version": POLICY_VERSION, "script_version": SCRIPT_VERSION,
        "script_sha256": pre["script_sha256"], "config_sha256": pre["config_sha256"],
        "rule_document_sha256": pre["rule_document_sha256"], "run_id": pre["run_id"],
        "execution_mode": mode, "visual_input_source": "canonical_visual_units_for_heavy",
        "source_input_source": "canonical_source_assets_for_heavy",
        "raw_visual_input_count": pre["raw_visual_input_count"],
        "canonical_visual_input_count": pre["canonical_visual_input_count"],
        "canonical_source_input_count": pre["canonical_source_input_count"],
        "dedup_excluded_visual_count": pre["dedup_excluded_visual_count"],
        "input_video_visual_units": sum(1 for row in result["rows"] if row["media_type"] == "video"),
        "input_image_visual_units": sum(1 for row in result["rows"] if row["media_type"] == "image"),
        "qwenvl_total_count": len(q_rows), "qwen_video_frame_count": len(q_video),
        "ocr_total_count": len(o_rows), "qwen_role_counts": dict(role_counts),
        "normal_video_group_count": stats["normal_video_group_count"],
        "normal_video_group_with_coverage_count": stats["normal_video_group_with_coverage_count"],
        "normal_video_group_missing_coverage_count": stats["normal_video_group_missing_coverage_count"],
        "screen_recording_video_group_count": stats["screen_recording_video_group_count"],
        "coverage_stride_frames": safe_int(pre["config"]["coverage_stride_frames"]),
        "coverage_local_radius_frames": configured_radius,
        "coverage_local_radius_frames_applied": configured_radius if radius_applied else 0,
        "coverage_anchor_count": stats["coverage_anchor_total_count"],
        "coverage_anchor_total_count": stats["coverage_anchor_total_count"],
        "coverage_local_candidate_pool_count": stats["coverage_local_candidate_pool_count"],
        "coverage_local_candidate_evaluation_count": stats["coverage_local_candidate_evaluation_count"],
        "short_video_count": stats["short_video_count"],
        "short_video_tail_fallback_count": stats["short_video_tail_fallback_count"],
        "non_short_video_tail_fallback_count": stats["non_short_video_tail_fallback_count"],
        "coverage_refill_count": stats["coverage_refill_count"],
        "coverage_refill_unavailable_kept_count": stats["coverage_refill_unavailable_kept_count"],
        "coverage_refill_failed_count": stats["coverage_refill_failed_count"],
        "coverage_missing_count": stats["coverage_missing_count"],
        "tail_only_anchor_count": stats["tail_only_anchor_count"],
        "tail_anchor_remap_count": stats["tail_anchor_remap_count"],
        "tail_anchor_remap_failed_count": stats["tail_anchor_remap_failed_count"],
        "tail_anchor_reused_existing_canonical_count": stats["tail_anchor_reused_existing_canonical_count"],
        "coverage_anchor_excluded_by_central_dedup_count": stats["coverage_anchor_excluded_by_central_dedup_count"],
        "coverage_refill_after_central_dedup_count": stats["coverage_refill_after_central_dedup_count"],
        "local_candidate_evaluation_count": stats["local_candidate_evaluation_count"],
        "vector_grid_label_time_pair_evaluation_count": stats["vector_grid_label_time_pair_evaluation_count"],
        "high_signal_supplement_duplicate_reject_count": stats["high_signal_supplement_duplicate_reject_count"],
        "screen_ocr_duplicate_drop_count": stats["screen_ocr_duplicate_drop_count"],
        "black_leak_into_qwenvl_count": len(q_ids & black_ids),
        "black_leak_into_ocr_count": len(o_ids & black_ids),
        "black_leak_qwen_count": len(q_ids & black_ids),
        "black_leak_ocr_count": len(o_ids & black_ids),
        "screen_recording_qwenvl_leak_count": screen_leaks,
        "central_duplicate_queue_leak_count": central_leaks,
        **signature_stats, **vector_stats, **label_stats,
        "central_dedup_run_id": pre["central_dedup_run_id"],
        "yoloe_run_id": pre["yoloe_run_id"], "openclip_run_id": pre["openclip_run_id"],
        "deterministic_recompute_match": deterministic,
        "deterministic_candidate_digest": semantic_candidate_digest(result),
        "automatic_acceptance_gates": gates,
        "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
        "safety": {
            "network": "disabled", "download_install": False, "model_loading": False,
            "original_video_read": False, "original_media_write": False,
            "derived_frame_access": "read_only", "sqlite_write": mode == "commit",
        },
        "config": pre["config"],
    }


def write_outputs(out: Path, result: dict[str, Any], summary: dict[str, Any], reverse_rows: list[dict[str, Any]]) -> dict[str, str]:
    manifests, reports = out / "manifests", out / "reports"
    manifests.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    all_rows = sorted([*result["q_rows"], *result["o_rows"]], key=lambda row: (row["queue_type"], row["source_relative_path"], safe_int(row["time_position_ms"]), row["visual_unit_id"]))
    paths = {
        "qwenvl_csv": manifests / "qwenvl_high_value_candidate_queue.csv",
        "qwenvl_jsonl": manifests / "qwenvl_high_value_candidate_queue.jsonl",
        "ocr_csv": manifests / "ocr_trigger_candidate_queue.csv",
        "ocr_jsonl": manifests / "ocr_trigger_candidate_queue.jsonl",
        "all_candidates_csv": manifests / "all_candidate_queue.csv",
        "all_candidates_jsonl": manifests / "all_candidate_queue.jsonl",
        "duplicate_reverse_mapping_jsonl": manifests / "duplicate_reverse_mapping.jsonl",
        "video_frame_decisions_jsonl": reports / "video_frame_decisions.jsonl",
        "high_signal_candidate_pool_jsonl": reports / "high_signal_candidate_pool.jsonl",
        "coverage_anchor_report_csv": reports / "coverage_anchor_report.csv",
        "video_budget_report_csv": reports / "video_budget_report.csv",
        "summary_json": reports / "stop03_2_candidate_summary.json",
        "summary_md": reports / "stop03_2_candidate_summary.md",
    }
    write_csv(paths["qwenvl_csv"], result["q_rows"], BASE_FIELDS)
    write_jsonl(paths["qwenvl_jsonl"], result["q_rows"])
    write_csv(paths["ocr_csv"], result["o_rows"], BASE_FIELDS)
    write_jsonl(paths["ocr_jsonl"], result["o_rows"])
    write_csv(paths["all_candidates_csv"], all_rows, BASE_FIELDS)
    write_jsonl(paths["all_candidates_jsonl"], all_rows)
    write_jsonl(paths["duplicate_reverse_mapping_jsonl"], reverse_rows)
    write_jsonl(paths["video_frame_decisions_jsonl"], result["decisions"])
    write_jsonl(
        paths["high_signal_candidate_pool_jsonl"],
        (row for row in result["decisions"] if row.get("high_signal_pool_member")),
    )
    write_csv(paths["coverage_anchor_report_csv"], result["coverage_reports"])
    write_csv(paths["video_budget_report_csv"], result["video_budget"])
    summary["manifest_consistency"] = {
        "qwenvl_csv_rows": len(result["q_rows"]), "qwenvl_jsonl_rows": len(result["q_rows"]),
        "ocr_csv_rows": len(result["o_rows"]), "ocr_jsonl_rows": len(result["o_rows"]),
        "all_candidate_rows": len(all_rows), "consistent": True,
    }
    summary["outputs"] = {key: str(path) for key, path in paths.items()}
    write_json(paths["summary_json"], summary)
    important = [
        "technical_status", "policy_status", "commit_status", "canonical_visual_input_count",
        "qwenvl_total_count", "qwen_video_frame_count", "ocr_total_count",
        "coverage_anchor_total_count", "normal_video_group_with_coverage_count",
        "normal_video_group_missing_coverage_count", "coverage_refill_count",
        "coverage_refill_failed_count", "central_duplicate_queue_leak_count",
        "black_leak_into_qwenvl_count", "black_leak_into_ocr_count",
        "screen_recording_qwenvl_leak_count", "deterministic_recompute_match",
    ]
    paths["summary_md"].write_text("\n".join(f"- **{key}**: `{summary.get(key)}`" for key in important) + "\n", encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


V23_DB_COLUMNS = {
    "candidate_role": "TEXT", "canonical_visual_unit_id": "TEXT", "duplicate_group_id": "TEXT",
    "frame_index": "INTEGER", "time_position_ms": "INTEGER", "canonical_time_ms": "INTEGER",
    "group_start_ms": "INTEGER", "group_end_ms": "INTEGER", "segment_start_ms": "INTEGER",
    "segment_end_ms": "INTEGER", "policy_version": "TEXT", "script_sha256": "TEXT",
    "config_sha256": "TEXT", "rule_document_sha256": "TEXT",
    "central_dedup_run_id": "TEXT", "yoloe_run_id": "TEXT",
    "openclip_run_id": "TEXT",
}


def backup_database(db: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    source = sqlite3.connect(str(db))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def restore_database(backup: Path, db: Path) -> None:
    source = sqlite3.connect(str(backup))
    target = sqlite3.connect(str(db))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def ensure_candidate_columns(con: sqlite3.Connection) -> None:
    existing = table_columns(con, "stop03_2_candidate_queue_items")
    for name, sql_type in V23_DB_COLUMNS.items():
        if name not in existing:
            con.execute(f"ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN {name} {sql_type}")


def commit_candidates(
    db: Path, out: Path, result: Mapping[str, Any], summary: MutableMapping[str, Any], clear_existing: bool,
) -> dict[str, Any]:
    if not clear_existing:
        raise RuntimeError("commit_requires_clear_existing_candidate_items")
    if summary["technical_status"] != "PASS":
        raise RuntimeError("commit_blocked_technical_status_not_pass")
    if summary["policy_status"] != "PASS":
        raise RuntimeError("commit_blocked_policy_not_frozen")
    backup = out / "database_backup" / db.name
    backup_database(db, backup)
    rows = [*result["q_rows"], *result["o_rows"]]
    con = connect_write(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        ensure_candidate_columns(con)
        con.execute("DELETE FROM stop03_2_candidate_queue_items")
        created_at = now_iso()
        columns = [
            "candidate_id", "queue_type", "visual_unit_id", "source_content_id", "derived_id",
            "candidate_score", "reason_codes", "black_frame_status", "luma_mean", "luma_std",
            "run_id", "script_version", "created_at", *V23_DB_COLUMNS,
        ]
        sql = f"INSERT INTO stop03_2_candidate_queue_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        by_visual = {str(row["visual_unit_id"]): row for row in result["rows"]}
        for row in rows:
            frame = by_visual[str(row["visual_unit_id"])]
            values = {
                **row, "luma_mean": frame.get("luma_mean_actual"), "luma_std": frame.get("luma_std_actual"),
                "created_at": created_at,
            }
            con.execute(sql, [values.get(column) for column in columns])
        con.execute(
            """INSERT INTO model_runs
            (run_id,stage,model_name,model_path,script_version,script_path,input_count,output_count,status,started_at,finished_at,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                summary["run_id"], STAGE, "rule_based_v23_no_model", "", SCRIPT_VERSION,
                str(Path(__file__).resolve()), summary["canonical_visual_input_count"], len(rows),
                "done", created_at, now_iso(), "",
            ),
        )
        db_ids = {str(row[0]) for row in con.execute("SELECT candidate_id FROM stop03_2_candidate_queue_items WHERE run_id=?", (summary["run_id"],))}
        manifest_ids = {str(row["candidate_id"]) for row in rows}
        if db_ids != manifest_ids:
            raise RuntimeError("db_manifest_candidate_id_mismatch_before_commit")
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
        if foreign_keys:
            raise RuntimeError(f"foreign_key_check_failed:{foreign_keys[:10]}")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    with connect_readonly(db) as readback:
        integrity = [str(row[0]) for row in readback.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in readback.execute("PRAGMA foreign_key_check")]
        readback_ids = {str(row[0]) for row in readback.execute("SELECT candidate_id FROM stop03_2_candidate_queue_items WHERE run_id=?", (summary["run_id"],))}
        duplicate_ids = int(readback.execute("SELECT COUNT(*)-COUNT(DISTINCT candidate_id) FROM stop03_2_candidate_queue_items").fetchone()[0])
    status = "PASS" if integrity == ["ok"] and not foreign_keys and readback_ids == {str(row["candidate_id"]) for row in rows} and duplicate_ids == 0 else "FAIL"
    if status != "PASS":
        restore_database(backup, db)
        raise RuntimeError("commit_readback_failed_database_restored_from_backup")
    return {
        "technical_status": status, "commit_status": "COMMITTED" if status == "PASS" else "FAIL",
        "database_backup_path": str(backup), "candidate_rows_written": len(rows),
        "candidate_rows_readback": len(readback_ids), "manifest_db_consistency": readback_ids == {str(row["candidate_id"]) for row in rows},
        "integrity_check": integrity, "foreign_key_check": foreign_keys,
        "duplicate_candidate_id_count": duplicate_ids, "rerun_idempotent_candidate_ids": duplicate_ids == 0,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Stop03-2 V23 generic candidate queues")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--mode", choices=["dry-run", "commit"], default="dry-run")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--clear-existing-candidate-items", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    set_offline_environment()
    db = Path(args.db).expanduser().resolve(strict=True)
    out = assert_output_path(Path(args.out), may_exist=False)
    config_path = Path(args.config).expanduser().resolve(strict=True)
    try:
        pre = preflight(db, out, config_path)
        if args.preflight_only:
            print(json.dumps(pre, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if pre["technical_status"] == "PASS" else 2
        if pre["technical_status"] != "PASS":
            raise RuntimeError("v23_preflight_failed")
        mode = args.mode
        run_id = f"{SCRIPT_VERSION}_{mode.replace('-', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        script_sha = sha256_file(Path(__file__).resolve())
        config, config_sha = load_config(config_path)
        hashes = {
            "script_sha256": script_sha, "config_sha256": config_sha,
            "rule_document_sha256": sha256_file(RULE_DOCUMENT),
        }
        db_sha_before = sha256_file(db)
        db_mtime_before = db.stat().st_mtime_ns
        con = connect_readonly(db)
        try:
            lineage = runtime_lineage(con)
            raw_rows, raw_by_source = load_raw_visual_metadata(con)
            canonical_rows = load_canonical_visuals(con, raw_by_source)
            canonical_ids = {str(row["visual_unit_id"]) for row in canonical_rows}
            signature_stats = attach_signatures(canonical_rows, config)
            if signature_stats["grid_signature_failed_count"]:
                raise RuntimeError("v23_derived_signature_failures")
            vectors, vector_stats = load_vectors(con, canonical_ids)
            by_id = {str(row["visual_unit_id"]): row for row in canonical_rows}
            for visual_id, vector in vectors.items():
                by_id[visual_id]["vector"] = vector
            label_stats = load_labels(con, by_id, config)
            timelapse = load_timelapse_representatives(con, canonical_ids)
        finally:
            con.close()
        for source_id, raw_source_rows in raw_by_source.items():
            if not raw_source_rows:
                continue
            start = min(safe_int(row["time_position_ms"]) for row in raw_source_rows)
            end = max(safe_int(row["time_position_ms"]) for row in raw_source_rows)
            for frame in canonical_rows:
                if frame["source_content_id"] == source_id:
                    frame["raw_source_start_ms"] = start
                    frame["raw_source_end_ms"] = end
        pre_context = {
            **pre, "config": config, "script_sha256": script_sha,
            "config_sha256": config_sha, "rule_document_sha256": hashes["rule_document_sha256"],
            "run_id": run_id,
        }
        result = select_candidates(canonical_rows, raw_by_source, config, run_id, mode, hashes, lineage, timelapse)
        result_repeat = select_candidates(canonical_rows, raw_by_source, config, run_id, mode, hashes, lineage, timelapse)
        deterministic = semantic_candidate_digest(result) == semantic_candidate_digest(result_repeat)
        db_sha_after_compute = sha256_file(db)
        db_mtime_after_compute = db.stat().st_mtime_ns
        db_unchanged = db_sha_before == db_sha_after_compute and db_mtime_before == db_mtime_after_compute
        summary = build_summary(result, pre_context, signature_stats, vector_stats, label_stats, mode, deterministic, db_unchanged if mode == "dry-run" else None)
        if summary["technical_status"] != "PASS":
            raise RuntimeError("v23_candidate_technical_gates_failed:" + json_text(summary["automatic_acceptance_gates"]))
        reverse_rows = reverse_mapping_rows(raw_rows, {str(row["visual_unit_id"]): row for row in canonical_rows})
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "preflight.json", pre)
        write_outputs(out, result, summary, reverse_rows)
        contact_result = contact_sheet.generate_contact_sheet(out, out / "html")
        summary["contact_sheet"] = contact_result
        summary["outputs"]["contact_sheet_html"] = contact_result["html_path"]
        summary["outputs"]["contact_sheet_audit"] = contact_result["audit_path"]
        if mode == "commit":
            commit_result = commit_candidates(db, out, result, summary, bool(args.clear_existing_candidate_items))
            summary["commit"] = commit_result
            summary["commit_status"] = commit_result["commit_status"]
            summary["technical_status"] = commit_result["technical_status"]
        db_sha_final = sha256_file(db)
        db_mtime_final = db.stat().st_mtime_ns
        summary["read_only_integrity"] = {
            "db_sha256_before": db_sha_before, "db_sha256_after": db_sha_final,
            "db_mtime_ns_before": db_mtime_before, "db_mtime_ns_after": db_mtime_final,
            "db_unchanged": db_sha_before == db_sha_final and db_mtime_before == db_mtime_final,
            "candidate_queue_items_written": 0 if mode == "dry-run" else len(result["q_rows"]) + len(result["o_rows"]),
            "model_runs_written": 0 if mode == "dry-run" else 1,
        }
        if mode == "dry-run" and not summary["read_only_integrity"]["db_unchanged"]:
            summary["technical_status"] = summary["validation_status"] = "FAIL"
        write_json(Path(summary["outputs"]["summary_json"]), summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["technical_status"] == "PASS" else 2
    except Exception as exc:
        failure = {
            "technical_status": "FAIL", "policy_status": "REVIEW", "commit_status": "DO_NOT_COMMIT",
            "policy_version": POLICY_VERSION, "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__, "error_message": str(exc),
            "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
            "safety": {"network": "disabled", "original_video_read": False, "sqlite_write": False},
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
