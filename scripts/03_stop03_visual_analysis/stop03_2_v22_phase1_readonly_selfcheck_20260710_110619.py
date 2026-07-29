#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-2 V22 phase-1 read-only self-check.

This script performs only the five checks approved for phase 1:

1. Existing OpenCLIP vector payload loading and integrity validation.
2. 16x5 luma-grid signature loading from existing derived frames.
3. Existing YOLOE bbox normalization using derived width/height.
4. V14 manifest loading and role split.
5. Coverage-window generation statistics.

It opens SQLite in read-only/query-only mode, writes no reports, creates no
candidate queue, and does not load or run any model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageOps
except Exception:  # dependency presence is reported; never installed here.
    Image = None
    ImageOps = None


SCRIPT_VERSION = "stop03_2_v22_phase1_readonly_selfcheck_20260710_110619"
POLICY_VERSION = "stop03_2_generic_high_value_rules_dr_v17_v22_phase1_readonly"

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V14_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v14_0_20260709_232500_full"
)
DEFAULT_COVERAGE_WINDOW_MS = 18_000
DEFAULT_GRID_COLS = 16
DEFAULT_GRID_ROWS = 5

SCREEN_CAPTURE_KEYS = (
    "rpreplay",
    "screenrecording",
    "screen_recording",
    "screen-recording",
    "screen recording",
    "record screen",
    "recorded screen",
    "录屏",
    "屏幕录制",
    "截屏",
    "截图",
    "screenshot",
)

V14_VIDEO_CATEGORY = "video_high_value_segment_candidate"
V14_FALLBACK_REASON = "video_min_one_best_available_frame"
TEXT_BEARING_LABELS = {
    "text",
    "sign",
    "billboard",
    "blackboard",
    "whiteboard",
    "document",
    "screen",
    "screen recording",
    "phone screen",
    "presentation slide",
    "subtitle",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_offline_environment() -> Dict[str, str]:
    values = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "ULTRALYTICS_OFFLINE": "1",
        "NO_ALBUMENTATIONS_UPDATE": "1",
    }
    for key, value in values.items():
        os.environ[key] = value
    return values


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_allowed_existing_file(raw: str, allowed_root: Path) -> Tuple[Optional[Path], str]:
    if not str(raw or "").strip():
        return None, "missing_path"
    candidate = Path(str(raw)).expanduser().resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    if not is_relative_to(candidate, allowed):
        return None, "path_outside_allowed_root"
    if not candidate.exists():
        return None, "missing_file"
    if not candidate.is_file():
        return None, "not_a_file"
    return candidate, "ok"


def connect_readonly(db: Path) -> sqlite3.Connection:
    resolved = db.expanduser().resolve(strict=True)
    con = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    query_only = int(con.execute("PRAGMA query_only").fetchone()[0])
    if query_only != 1:
        con.close()
        raise RuntimeError("sqlite_query_only_not_enabled")
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def require_tables(con: sqlite3.Connection, tables: Iterable[str]) -> None:
    missing = [table for table in tables if not table_exists(con, table)]
    if missing:
        raise RuntimeError("missing_tables:" + ",".join(missing))


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = round((len(ordered) - 1) * fraction)
    return round(ordered[index], 6)


def distribution(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "p10": None, "p50": None, "p90": None, "max": None}
    numeric = [float(value) for value in values]
    return {
        "count": len(numeric),
        "min": round(min(numeric), 6),
        "p10": percentile(numeric, 0.10),
        "p50": percentile(numeric, 0.50),
        "p90": percentile(numeric, 0.90),
        "max": round(max(numeric), 6),
    }


def effective_time_ms(row: Mapping[str, Any]) -> Tuple[int, str]:
    for key, source in (
        ("derived_time_position_ms", "derived_assets.time_position_ms"),
        ("visual_time_position_ms", "visual_units.time_position_ms"),
    ):
        value = row.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = -1
        if parsed >= 0:
            return parsed, source
    for key, source in (
        ("derived_frame_index", "derived_assets.frame_index_x_3000ms_fallback"),
        ("visual_frame_index", "visual_units.frame_index_x_3000ms_fallback"),
    ):
        value = row.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = -1
        if parsed >= 0:
            return parsed * 3000, source
    return -1, "missing_time_position"


def load_video_rows(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    sql = """
    SELECT
      vu.visual_unit_id,
      vu.source_content_id,
      vu.derived_id,
      vu.visual_file,
      vu.time_position_ms AS visual_time_position_ms,
      -1 AS visual_frame_index,
      da.derived_path,
      da.time_position_ms AS derived_time_position_ms,
      da.frame_index AS derived_frame_index,
      da.width,
      da.height,
      sa.relative_path AS source_relative_path,
      sa.absolute_path AS source_absolute_path
    FROM visual_units vu
    LEFT JOIN derived_assets da ON da.derived_id = vu.derived_id
    LEFT JOIN source_assets sa ON sa.source_content_id = vu.source_content_id
    WHERE sa.media_type = 'video'
    ORDER BY vu.source_content_id, da.time_position_ms, vu.visual_unit_id
    """
    rows: List[Dict[str, Any]] = []
    for raw in con.execute(sql).fetchall():
        row = dict(raw)
        time_ms, time_source = effective_time_ms(row)
        row["time_position_ms"] = time_ms
        row["time_position_source"] = time_source
        row["source_relative_path"] = str(
            row.get("source_relative_path") or row.get("source_absolute_path") or ""
        )
        row["derived_visual_path"] = str(
            row.get("visual_file") or row.get("derived_path") or ""
        )
        rows.append(row)
    return rows


def parse_vector_key(vector_key: str) -> Tuple[Optional[Path], str, str]:
    raw = str(vector_key or "")
    if not raw.startswith("jsonl:") or "#" not in raw:
        return None, "", "unsupported_vector_key"
    payload_raw, fragment = raw[len("jsonl:") :].rsplit("#", 1)
    if not payload_raw or not fragment:
        return None, fragment, "invalid_vector_key"
    return Path(payload_raw), fragment, "ok"


def check_vector_payload(con: sqlite3.Connection) -> Dict[str, Any]:
    db_rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT embedding_id, visual_unit_id, source_content_id, dimension,
                   vector_key, model_name, model_path, run_id
            FROM embeddings
            ORDER BY embedding_id
            """
        ).fetchall()
    ]
    expected_by_id = {str(row["embedding_id"]): row for row in db_rows}
    payload_to_expected: Dict[Path, set[str]] = defaultdict(set)
    unsupported_key_count = 0
    fragment_mismatch_count = 0
    disallowed_payload_path_count = 0
    missing_payload_path_count = 0
    key_dimension_counts: Counter[int] = Counter()

    for row in db_rows:
        embedding_id = str(row["embedding_id"])
        try:
            key_dimension_counts[int(row["dimension"])] += 1
        except (TypeError, ValueError):
            key_dimension_counts[-1] += 1
        payload_raw, fragment, status = parse_vector_key(str(row.get("vector_key") or ""))
        if status != "ok" or payload_raw is None:
            unsupported_key_count += 1
            continue
        if fragment != embedding_id:
            fragment_mismatch_count += 1
        payload, path_status = resolve_allowed_existing_file(
            str(payload_raw), TEST_OUTPUT_ROOT
        )
        if path_status == "path_outside_allowed_root":
            disallowed_payload_path_count += 1
            continue
        if path_status != "ok" or payload is None:
            missing_payload_path_count += 1
            continue
        payload_to_expected[payload].add(embedding_id)

    seen_ids: set[str] = set()
    duplicate_payload_id_count = 0
    unexpected_payload_id_count = 0
    visual_unit_mismatch_count = 0
    dimension_mismatch_count = 0
    invalid_vector_count = 0
    valid_vector_count = 0
    payload_line_count = 0

    for payload in sorted(payload_to_expected, key=lambda path: str(path)):
        with payload.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                payload_line_count += 1
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    invalid_vector_count += 1
                    continue
                embedding_id = str(item.get("embedding_id") or "")
                expected = expected_by_id.get(embedding_id)
                if expected is None:
                    unexpected_payload_id_count += 1
                    continue
                if embedding_id in seen_ids:
                    duplicate_payload_id_count += 1
                    continue
                seen_ids.add(embedding_id)
                if str(item.get("visual_unit_id") or "") != str(
                    expected.get("visual_unit_id") or ""
                ):
                    visual_unit_mismatch_count += 1
                vector = item.get("vector")
                try:
                    expected_dimension = int(expected.get("dimension") or 0)
                except (TypeError, ValueError):
                    expected_dimension = 0
                if not isinstance(vector, list) or len(vector) != expected_dimension:
                    dimension_mismatch_count += 1
                    invalid_vector_count += 1
                    continue
                if not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in vector
                ):
                    invalid_vector_count += 1
                    continue
                valid_vector_count += 1

    missing_expected_id_count = len(set(expected_by_id) - seen_ids)
    blockers = sum(
        (
            unsupported_key_count,
            fragment_mismatch_count,
            disallowed_payload_path_count,
            missing_payload_path_count,
            duplicate_payload_id_count,
            unexpected_payload_id_count,
            visual_unit_mismatch_count,
            dimension_mismatch_count,
            invalid_vector_count,
            missing_expected_id_count,
        )
    )
    status = "PASS" if db_rows and blockers == 0 and valid_vector_count == len(db_rows) else "FAIL"
    return {
        "technical_status": status,
        "vector_dedup_status": (
            "enabled_existing_jsonl_payload" if status == "PASS" else "skipped_no_vector_payload"
        ),
        "vector_payload_found": bool(payload_to_expected),
        "embedding_db_row_count": len(db_rows),
        "payload_file_count": len(payload_to_expected),
        "payload_paths": [str(path) for path in sorted(payload_to_expected, key=str)],
        "payload_line_count": payload_line_count,
        "valid_vector_count": valid_vector_count,
        "dimension_counts": {str(key): value for key, value in sorted(key_dimension_counts.items())},
        "unsupported_vector_key_count": unsupported_key_count,
        "vector_key_fragment_mismatch_count": fragment_mismatch_count,
        "disallowed_payload_path_count": disallowed_payload_path_count,
        "missing_payload_path_count": missing_payload_path_count,
        "duplicate_payload_id_count": duplicate_payload_id_count,
        "unexpected_payload_id_count": unexpected_payload_id_count,
        "visual_unit_mismatch_count": visual_unit_mismatch_count,
        "dimension_mismatch_count": dimension_mismatch_count,
        "invalid_vector_count": invalid_vector_count,
        "missing_expected_id_count": missing_expected_id_count,
        "db_payload_id_sets_equal": (
            len(expected_by_id) == valid_vector_count
            and missing_expected_id_count == 0
            and unexpected_payload_id_count == 0
        ),
    }


def image_values(image: Any) -> List[int]:
    getter = getattr(image, "get_flattened_data", None)
    if callable(getter):
        return [int(value) for value in getter()]
    return [int(value) for value in image.getdata()]


def grid_signature(path: Path, cols: int, rows: int) -> Dict[str, Any]:
    if Image is None:
        raise RuntimeError("pillow_import_unavailable")
    with Image.open(path) as image:
        logical_image = ImageOps.exif_transpose(image) if ImageOps is not None else image
        grid = logical_image.convert("L").resize((cols, rows))
        values = image_values(grid)
    if len(values) != cols * rows:
        raise ValueError(f"unexpected_grid_length:{len(values)}")
    mean = statistics.fmean(values)
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    std = math.sqrt(variance)
    differences: List[int] = []
    for y in range(rows):
        for x in range(cols):
            index = y * cols + x
            if x + 1 < cols:
                differences.append(abs(values[index] - values[index + 1]))
            if y + 1 < rows:
                differences.append(abs(values[index] - values[index + cols]))
    structure = statistics.fmean(differences) if differences else 0.0
    centered_norm = math.sqrt(sum((value - mean) ** 2 for value in values))
    return {
        "values": tuple(values),
        "mean": mean,
        "std": std,
        "structure": structure,
        "centered_norm": centered_norm,
    }


def check_grid_signatures(
    video_rows: Sequence[Mapping[str, Any]], cols: int, rows: int
) -> Dict[str, Any]:
    if cols <= 0 or rows <= 0:
        return {"technical_status": "FAIL", "reason": "invalid_grid_shape"}
    if Image is None:
        return {"technical_status": "FAIL", "reason": "pillow_import_unavailable"}

    success_count = 0
    missing_path_count = 0
    disallowed_path_count = 0
    decode_failed_count = 0
    means: List[float] = []
    stds: List[float] = []
    structures: List[float] = []
    centered_norms: List[float] = []
    errors: Counter[str] = Counter()

    for row in video_rows:
        path, path_status = resolve_allowed_existing_file(
            str(row.get("derived_visual_path") or ""), TEST_OUTPUT_ROOT
        )
        if path_status == "path_outside_allowed_root":
            disallowed_path_count += 1
            continue
        if path_status != "ok" or path is None:
            missing_path_count += 1
            continue
        try:
            signature = grid_signature(path, cols, rows)
        except Exception as exc:  # only aggregate; do not emit source content.
            decode_failed_count += 1
            errors[type(exc).__name__] += 1
            continue
        success_count += 1
        means.append(float(signature["mean"]))
        stds.append(float(signature["std"]))
        structures.append(float(signature["structure"]))
        centered_norms.append(float(signature["centered_norm"]))

    expected_count = len(video_rows)
    status = (
        "PASS"
        if expected_count > 0
        and success_count == expected_count
        and missing_path_count == 0
        and disallowed_path_count == 0
        and decode_failed_count == 0
        else "FAIL"
    )
    return {
        "technical_status": status,
        "grid_similarity_enabled": status == "PASS",
        "grid_shape": f"{cols}x{rows}",
        "grid_value_count": cols * rows,
        "expected_video_frame_count": expected_count,
        "grid_signature_success_count": success_count,
        "grid_signature_failed_count": (
            missing_path_count + disallowed_path_count + decode_failed_count
        ),
        "missing_path_count": missing_path_count,
        "disallowed_path_count": disallowed_path_count,
        "decode_failed_count": decode_failed_count,
        "decode_error_types": dict(errors),
        "luma_mean_distribution": distribution(means),
        "luma_std_distribution": distribution(stds),
        "structure_distribution": distribution(structures),
        "centered_norm_distribution": distribution(centered_norms),
    }


def parse_bbox_xyxy(raw: Any) -> Optional[Tuple[float, float, float, float]]:
    if raw is None:
        return None
    value = raw
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        if "bbox_xyxy" in value:
            value = value.get("bbox_xyxy")
        elif "bbox" in value:
            value = value.get("bbox")
        elif all(key in value for key in ("x1", "y1", "x2", "y2")):
            value = [value["x1"], value["y1"], value["x2"], value["y2"]]
        else:
            return None
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        result = tuple(float(value[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result):
        return None
    return result  # producer stores YOLOE bbox_xyxy in this column.


def normalize_bbox_xyxy(
    raw: Any, width: Any, height: Any
) -> Tuple[Optional[Tuple[float, float, float, float]], Dict[str, Any]]:
    bbox = parse_bbox_xyxy(raw)
    if bbox is None:
        return None, {"reason": "bbox_parse_failed", "input_space": "unknown", "clamped": False}
    x1, y1, x2, y2 = bbox
    max_abs = max(abs(value) for value in bbox)
    input_space = "normalized" if max_abs <= 2.0 else "absolute_pixels"
    if input_space == "absolute_pixels":
        try:
            numeric_width = float(width)
            numeric_height = float(height)
        except (TypeError, ValueError):
            return None, {"reason": "missing_dimensions", "input_space": input_space, "clamped": False}
        if numeric_width <= 0 or numeric_height <= 0:
            return None, {"reason": "missing_dimensions", "input_space": input_space, "clamped": False}
        x1, x2 = x1 / numeric_width, x2 / numeric_width
        y1, y2 = y1 / numeric_height, y2 / numeric_height
    before_clamp = (x1, y1, x2, y2)
    x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
    y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
    normalized = (x1, y1, x2, y2)
    clamped = normalized != before_clamp
    if x2 <= x1 or y2 <= y1:
        return None, {"reason": "empty_bbox_after_normalization", "input_space": input_space, "clamped": clamped}
    return normalized, {"reason": "ok", "input_space": input_space, "clamped": clamped}


def bbox_area(bbox: Tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def check_bbox_normalization(con: sqlite3.Connection) -> Dict[str, Any]:
    sql = """
    SELECT vl.visual_unit_id, vl.label, vl.confidence, vl.bbox,
           da.width, da.height,
           CASE
             WHEN vu.visual_file IS NOT NULL AND TRIM(vu.visual_file) <> ''
             THEN vu.visual_file
             ELSE da.derived_path
           END AS derived_visual_path
    FROM visual_labels vl
    LEFT JOIN visual_units vu ON vu.visual_unit_id = vl.visual_unit_id
    LEFT JOIN derived_assets da ON da.derived_id = vu.derived_id
    ORDER BY vl.label_id
    """
    total_count = 0
    valid_count = 0
    invalid_count = 0
    clamped_count = 0
    input_spaces: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    dimension_sources: Counter[str] = Counter()
    areas: List[float] = []
    obvious_large_text_proxy_count = 0
    actual_size_cache: Dict[str, Tuple[Optional[Tuple[int, int]], str]] = {}
    actual_size_mismatch_db_count = 0
    actual_size_mismatch_visual_unit_ids: set[str] = set()
    dimension_path_issue_counts: Counter[str] = Counter()

    for raw in con.execute(sql).fetchall():
        row = dict(raw)
        total_count += 1
        raw_path = str(row.get("derived_visual_path") or "")
        if raw_path not in actual_size_cache:
            image_path, path_status = resolve_allowed_existing_file(
                raw_path, TEST_OUTPUT_ROOT
            )
            if path_status != "ok" or image_path is None:
                actual_size_cache[raw_path] = (None, path_status)
            else:
                try:
                    if Image is None:
                        raise RuntimeError("pillow_import_unavailable")
                    with Image.open(image_path) as image:
                        logical_image = (
                            ImageOps.exif_transpose(image)
                            if ImageOps is not None
                            else image
                        )
                        actual_size_cache[raw_path] = (
                            (int(logical_image.width), int(logical_image.height)),
                            "ok",
                        )
                except Exception as exc:
                    actual_size_cache[raw_path] = (
                        None,
                        "decode_failed:" + type(exc).__name__,
                    )
        actual_size, size_status = actual_size_cache[raw_path]
        width = row.get("width")
        height = row.get("height")
        if actual_size is not None:
            actual_width, actual_height = actual_size
            try:
                db_width = int(width)
                db_height = int(height)
            except (TypeError, ValueError):
                db_width, db_height = -1, -1
            if (actual_width, actual_height) != (db_width, db_height):
                actual_size_mismatch_db_count += 1
                actual_size_mismatch_visual_unit_ids.add(
                    str(row.get("visual_unit_id") or "")
                )
            width, height = actual_width, actual_height
            dimension_sources["derived_image_size"] += 1
        else:
            dimension_sources["derived_assets_width_height_fallback"] += 1
            dimension_path_issue_counts[size_status] += 1
        normalized, meta = normalize_bbox_xyxy(
            row.get("bbox"), width, height
        )
        input_spaces[str(meta.get("input_space") or "unknown")] += 1
        if meta.get("clamped"):
            clamped_count += 1
        if normalized is None:
            invalid_count += 1
            failure_reasons[str(meta.get("reason") or "unknown")] += 1
            continue
        valid_count += 1
        area = bbox_area(normalized)
        areas.append(area)
        try:
            confidence = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        label = str(row.get("label") or "").strip().lower()
        if label in TEXT_BEARING_LABELS and confidence >= 0.70 and area >= 0.03:
            obvious_large_text_proxy_count += 1

    probe_absolute, _ = normalize_bbox_xyxy(
        [128.0, 67.6, 640.0, 338.0], 1280, 676
    )
    probe_normalized, _ = normalize_bbox_xyxy([0.1, 0.2, 0.5, 0.6], 1280, 676)
    probe_invalid, _ = normalize_bbox_xyxy([], 1280, 676)
    probe_pass = (
        probe_absolute is not None
        and all(abs(a - b) <= 1e-9 for a, b in zip(probe_absolute, (0.1, 0.1, 0.5, 0.5)))
        and probe_normalized == (0.1, 0.2, 0.5, 0.6)
        and probe_invalid is None
    )
    status = "PASS" if total_count > 0 and valid_count == total_count and probe_pass else "FAIL"
    return {
        "technical_status": status,
        "producer_bbox_format": "xyxy",
        "bbox_row_count": total_count,
        "normalized_bbox_valid_count": valid_count,
        "normalized_bbox_invalid_count": invalid_count,
        "input_space_counts": dict(input_spaces),
        "dimension_source_counts": dict(dimension_sources),
        "unique_derived_image_size_count": len(actual_size_cache),
        "actual_size_mismatch_db_label_row_count": actual_size_mismatch_db_count,
        "actual_size_mismatch_db_visual_unit_count": len(
            actual_size_mismatch_visual_unit_ids
        ),
        "exif_orientation_applied": True,
        "dimension_path_issue_counts": dict(dimension_path_issue_counts),
        "clamped_bbox_count": clamped_count,
        "failure_reason_counts": dict(failure_reasons),
        "normalized_area_distribution": distribution(areas),
        "obvious_large_text_proxy_box_count": obvious_large_text_proxy_count,
        "normalization_probe_status": "PASS" if probe_pass else "FAIL",
    }


def is_screen_capture(path: str) -> bool:
    haystack = str(path or "").lower()
    return any(key.lower() in haystack for key in SCREEN_CAPTURE_KEYS)


def check_v14_manifest(v14_out: Path, db_visual_unit_ids: set[str]) -> Dict[str, Any]:
    manifest = v14_out / "manifests" / "qwenvl_high_value_candidate_queue.csv"
    resolved, path_status = resolve_allowed_existing_file(str(manifest), TEST_OUTPUT_ROOT)
    if path_status != "ok" or resolved is None:
        return {
            "technical_status": "FAIL",
            "manifest_path": str(manifest),
            "reason": path_status,
        }

    total_rows = 0
    video_rows = 0
    missing_db_visual_unit_count = 0
    screen_capture_video_row_count = 0
    duplicate_video_visual_unit_count = 0
    roles: Counter[str] = Counter()
    reason_presence: Counter[str] = Counter()
    seen_video_ids: set[str] = set()

    with resolved.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            total_rows += 1
            if row.get("high_value_category") != V14_VIDEO_CATEGORY:
                continue
            video_rows += 1
            visual_unit_id = str(row.get("visual_unit_id") or "")
            if visual_unit_id in seen_video_ids:
                duplicate_video_visual_unit_count += 1
            seen_video_ids.add(visual_unit_id)
            if visual_unit_id not in db_visual_unit_ids:
                missing_db_visual_unit_count += 1
            reasons = str(row.get("reason_codes") or "")
            if V14_FALLBACK_REASON in reasons:
                roles["video_coverage_fallback"] += 1
            else:
                roles["video_high_signal_keyframe"] += 1
            if is_screen_capture(str(row.get("source_relative_path") or "")):
                screen_capture_video_row_count += 1
            for reason in (
                "video_min_one_best_available_frame",
                "major_object_set_change",
                "high_information_jump",
                "ocr_region_emerges",
                "human_composition_preferred",
                "video_coverage_boundary",
            ):
                if reason in reasons:
                    reason_presence[reason] += 1

    role_total = sum(roles.values())
    status = (
        "PASS"
        if video_rows > 0
        and role_total == video_rows
        and missing_db_visual_unit_count == 0
        and duplicate_video_visual_unit_count == 0
        and screen_capture_video_row_count == 0
        else "FAIL"
    )
    return {
        "technical_status": status,
        "manifest_path": str(resolved),
        "manifest_sha256": sha256_file(resolved),
        "manifest_total_qwenvl_rows": total_rows,
        "v14_video_row_count": video_rows,
        "role_counts": dict(roles),
        "role_split_total_count": role_total,
        "reason_presence_counts": dict(reason_presence),
        "missing_db_visual_unit_count": missing_db_visual_unit_count,
        "duplicate_video_visual_unit_count": duplicate_video_visual_unit_count,
        "screen_capture_video_row_count": screen_capture_video_row_count,
    }


def tail_window_ms(duration_ms: int) -> int:
    if duration_ms <= 30_000:
        return 0
    return max(6_000, min(30_000, round(duration_ms * 0.05)))


def check_coverage_windows(
    video_rows: Sequence[Mapping[str, Any]], coverage_window_ms: int
) -> Dict[str, Any]:
    if coverage_window_ms <= 0:
        return {"technical_status": "FAIL", "reason": "invalid_coverage_window_ms"}

    by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in video_rows:
        by_source[str(row.get("source_content_id") or "")].append(row)

    video_source_group_count = len(by_source)
    screen_groups = 0
    normal_groups = 0
    normal_with_windows = 0
    normal_missing_windows = 0
    coverage_window_total_count = 0
    coverage_nonempty_window_count = 0
    coverage_empty_window_count = 0
    coverage_candidate_evaluated_count = 0
    tail_excluded_frame_count = 0
    missing_time_frame_count = 0
    window_counts_per_video: List[float] = []
    candidate_counts_per_window: List[float] = []
    eligible_frame_gap_ms: List[float] = []
    normal_video_group_with_gap_gt_window_count = 0
    normal_video_group_with_empty_window_count = 0
    max_consecutive_empty_windows = 0
    time_sources: Counter[str] = Counter()

    for source_content_id, rows in sorted(by_source.items()):
        path = str(rows[0].get("source_relative_path") or "") if rows else ""
        if is_screen_capture(path):
            screen_groups += 1
            continue
        normal_groups += 1
        valid_rows: List[Tuple[int, Mapping[str, Any]]] = []
        for row in rows:
            time_source = str(row.get("time_position_source") or "missing_time_position")
            time_sources[time_source] += 1
            raw_time_ms = row.get("time_position_ms")
            try:
                time_ms = (
                    -1
                    if raw_time_ms is None or str(raw_time_ms).strip() == ""
                    else int(raw_time_ms)
                )
            except (TypeError, ValueError):
                time_ms = -1
            if time_ms < 0:
                missing_time_frame_count += 1
                continue
            valid_rows.append((time_ms, row))
        if not valid_rows:
            normal_missing_windows += 1
            continue
        valid_rows.sort(key=lambda pair: (pair[0], str(pair[1].get("visual_unit_id") or "")))
        start_ms = valid_rows[0][0]
        max_ms = valid_rows[-1][0]
        duration_ms = max(0, max_ms - start_ms)
        protected_tail_ms = tail_window_ms(duration_ms)
        # Half-open coverage interval. For long videos the protected tail starts
        # at max_ms-tail and is excluded. For short videos max_ms is included by
        # setting the exclusive end to max_ms+1.
        coverage_end_exclusive_ms = (
            max_ms - protected_tail_ms if protected_tail_ms else max_ms + 1
        )
        if coverage_end_exclusive_ms <= start_ms:
            coverage_end_exclusive_ms = start_ms + 1
        coverage_span_ms = coverage_end_exclusive_ms - start_ms
        planned_windows = max(
            1, (coverage_span_ms + coverage_window_ms - 1) // coverage_window_ms
        )
        window_candidates: Dict[int, int] = defaultdict(int)
        eligible_times: List[int] = []
        for time_ms, _row in valid_rows:
            if time_ms >= coverage_end_exclusive_ms:
                if protected_tail_ms:
                    tail_excluded_frame_count += 1
                continue
            window_index = min(
                planned_windows - 1, (time_ms - start_ms) // coverage_window_ms
            )
            window_candidates[int(window_index)] += 1
            eligible_times.append(time_ms)
            coverage_candidate_evaluated_count += 1
        nonempty_windows = len(window_candidates)
        empty_windows = planned_windows - nonempty_windows
        gaps = [
            later - earlier
            for earlier, later in zip(eligible_times, eligible_times[1:])
        ]
        eligible_frame_gap_ms.extend(float(gap) for gap in gaps)
        if any(gap > coverage_window_ms for gap in gaps):
            normal_video_group_with_gap_gt_window_count += 1
        if empty_windows > 0:
            normal_video_group_with_empty_window_count += 1
            current_empty_run = 0
            for window_index in range(planned_windows):
                if window_index not in window_candidates:
                    current_empty_run += 1
                    max_consecutive_empty_windows = max(
                        max_consecutive_empty_windows, current_empty_run
                    )
                else:
                    current_empty_run = 0
        coverage_window_total_count += planned_windows
        coverage_nonempty_window_count += nonempty_windows
        coverage_empty_window_count += empty_windows
        window_counts_per_video.append(float(planned_windows))
        candidate_counts_per_window.extend(float(count) for count in window_candidates.values())
        if nonempty_windows > 0:
            normal_with_windows += 1
        else:
            normal_missing_windows += 1

    status = (
        "PASS"
        if video_source_group_count > 0
        and normal_groups > 0
        and normal_missing_windows == 0
        and coverage_nonempty_window_count > 0
        and coverage_candidate_evaluated_count > 0
        else "FAIL"
    )
    return {
        "technical_status": status,
        "coverage_window_ms": coverage_window_ms,
        "video_source_group_count": video_source_group_count,
        "normal_video_group_count": normal_groups,
        "screen_capture_video_group_count": screen_groups,
        "normal_video_group_with_coverage_windows_count": normal_with_windows,
        "normal_video_group_missing_coverage_windows_count": normal_missing_windows,
        "coverage_window_total_count": coverage_window_total_count,
        "coverage_anchor_total_count": coverage_nonempty_window_count,
        "coverage_nonempty_window_count": coverage_nonempty_window_count,
        "coverage_empty_window_count": coverage_empty_window_count,
        "normal_video_group_with_empty_window_count": (
            normal_video_group_with_empty_window_count
        ),
        "max_consecutive_empty_windows": max_consecutive_empty_windows,
        "coverage_window_candidate_evaluated_count": coverage_candidate_evaluated_count,
        "tail_excluded_frame_count": tail_excluded_frame_count,
        "missing_time_frame_count": missing_time_frame_count,
        "time_position_source_counts": dict(time_sources),
        "eligible_frame_gap_ms_distribution": distribution(eligible_frame_gap_ms),
        "normal_video_group_with_gap_gt_window_count": (
            normal_video_group_with_gap_gt_window_count
        ),
        "windows_per_normal_video_distribution": distribution(window_counts_per_video),
        "candidates_per_nonempty_window_distribution": distribution(
            candidate_counts_per_window
        ),
        "candidate_queue_generated": False,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="V22 phase-1 read-only loaders and coverage-window statistics"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--v14-out", default=str(DEFAULT_V14_OUT))
    parser.add_argument(
        "--coverage-window-ms", type=int, default=DEFAULT_COVERAGE_WINDOW_MS
    )
    parser.add_argument("--grid-cols", type=int, default=DEFAULT_GRID_COLS)
    parser.add_argument("--grid-rows", type=int, default=DEFAULT_GRID_ROWS)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> Dict[str, Any]:
    offline_env = set_offline_environment()
    db = Path(args.db).expanduser().resolve(strict=True)
    v14_out = Path(args.v14_out).expanduser().resolve(strict=True)
    if not is_relative_to(v14_out, TEST_OUTPUT_ROOT.resolve(strict=False)):
        raise RuntimeError("v14_out_outside_test_output_root")

    db_sha256_before = sha256_file(db)
    db_mtime_ns_before = db.stat().st_mtime_ns
    con = connect_readonly(db)
    try:
        require_tables(
            con,
            (
                "visual_units",
                "derived_assets",
                "source_assets",
                "visual_labels",
                "embeddings",
            ),
        )
        video_rows = load_video_rows(con)
        db_visual_unit_ids = {
            str(row[0]) for row in con.execute("SELECT visual_unit_id FROM visual_units")
        }
        checks = {
            "vector_payload_loader": check_vector_payload(con),
            "grid_16x5_signature_loader": check_grid_signatures(
                video_rows, int(args.grid_cols), int(args.grid_rows)
            ),
            "bbox_normalization": check_bbox_normalization(con),
            "v14_manifest_role_split": check_v14_manifest(
                v14_out, db_visual_unit_ids
            ),
            "coverage_window_generation": check_coverage_windows(
                video_rows, int(args.coverage_window_ms)
            ),
        }
    finally:
        con.close()

    db_sha256_after = sha256_file(db)
    db_mtime_ns_after = db.stat().st_mtime_ns
    db_unchanged = (
        db_sha256_before == db_sha256_after
        and db_mtime_ns_before == db_mtime_ns_after
    )
    module_status_counts = Counter(
        str(check.get("technical_status") or "FAIL") for check in checks.values()
    )
    technical_status = (
        "PASS"
        if module_status_counts.get("FAIL", 0) == 0 and db_unchanged
        else "FAIL"
    )
    return {
        "validation_status": technical_status,
        "technical_status": technical_status,
        "policy_status": "REVIEW",
        "policy_reason_codes": [
            "phase1_loaders_and_window_statistics_only",
            "candidate_selection_not_implemented",
            "phase2_requires_user_confirmation",
        ],
        "script_version": SCRIPT_VERSION,
        "policy_version": POLICY_VERSION,
        "executed_at": now_iso(),
        "python_executable": sys.executable,
        "db_path": str(db),
        "v14_out": str(v14_out),
        "input_video_visual_units": len(video_rows),
        "module_status_counts": dict(module_status_counts),
        "checks": checks,
        "read_only_integrity": {
            "sqlite_open_mode": "mode=ro",
            "sqlite_query_only": True,
            "db_sha256_before": db_sha256_before,
            "db_sha256_after": db_sha256_after,
            "db_mtime_ns_before": db_mtime_ns_before,
            "db_mtime_ns_after": db_mtime_ns_after,
            "db_unchanged": db_unchanged,
            "candidate_queue_items_written": 0,
            "model_runs_written": 0,
            "output_files_created": [],
        },
        "model_rerun": {
            "yoloe": False,
            "openclip": False,
            "qwen_vl": False,
            "ocr": False,
        },
        "safety": {
            "network": "not_used_offline_env_enabled",
            "download": "not_used",
            "dependency_install": "not_used",
            "model_loading": "not_used",
            "original_video_read": False,
            "original_media_write": False,
            "derived_frame_read": "existing_test_output_only_read_only",
            "sqlite_write": False,
            "candidate_queue_generation": False,
            "formal_v22_output_generation": False,
            "offline_env": offline_env,
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        result = {
            "validation_status": "FAIL",
            "technical_status": "FAIL",
            "policy_status": "REVIEW",
            "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "safety": {
                "sqlite_write": False,
                "candidate_queue_generation": False,
                "formal_v22_output_generation": False,
            },
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("technical_status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
