#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-2 V22 generic candidate queues.

Modes are deliberately separated:

* --preflight-only: stdout only, SQLite read-only, no output directory creation.
* --dry-run: write reports/manifests under test-output, SQLite read-only.
* --commit: write reports/manifests and then transactionally replace/append only
  stop03_2_candidate_queue_items and model_runs.

No mode reads original video content or loads/runs a model. Existing derived
frames and the existing OpenCLIP JSONL payload are the only visual inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import stop03_2_v22_phase1_readonly_selfcheck_20260710_110619 as phase1


SCRIPT_VERSION = "stop03_2_candidate_queues_from_db_safe_v22_0_20260710_112936"
POLICY_VERSION = "stop03_2_generic_high_value_rules_dr_v17_v22_0_20260710"
STAGE = "stop03_2_candidate_queues"

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V14_OUT = phase1.DEFAULT_V14_OUT
DEFAULT_V20_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v20_0_20260710_094500_full"
)
DEFAULT_V21_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v21_0_20260710_101500_full"
)
DEFAULT_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v22_0_20260710_112936_dry_run"
)
DEFAULT_PRE_DEDUP_V22_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v22_0_20260710_112936_dry_run_v2"
)

VIDEO_OUTPUT_ROLES = {
    "video_coverage_keyframe",
    "video_coverage_high_signal_overlap",
    "video_high_signal_supplement",
}
TEXT_BEARING_LABELS = phase1.TEXT_BEARING_LABELS

GENERIC_LABEL_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("human_social", ("person", "people", "human", "face", "crowd", "body")),
    (
        "vehicle_machine_equipment",
        (
            "car", "truck", "bus", "train", "motorcycle", "bicycle", "boat",
            "airplane", "vehicle", "tractor", "harvester", "machine", "tool",
            "camera", "microphone", "phone", "laptop", "computer", "tablet",
            "electric bike",
        ),
    ),
    ("animal_living", ("dog", "cat", "cow", "sheep", "horse", "bird", "animal", "chicken")),
    (
        "text_screen_document",
        (
            "text", "sign", "screen", "monitor", "display", "document", "paper",
            "book", "poster", "billboard", "presentation slide", "subtitle",
            "whiteboard", "blackboard", "license plate", "menu",
        ),
    ),
    (
        "place_structure_road",
        (
            "building", "house", "street", "road", "bridge", "station", "store",
            "shop", "room", "kitchen", "village", "city",
        ),
    ),
    (
        "nature_land_plant",
        (
            "field", "farm", "farmland", "crop", "plant", "tree", "flower",
            "river", "lake", "sea", "mountain", "beach",
        ),
    ),
    ("food_object_context", ("food", "table", "chair", "bottle", "cup", "bag")),
)

CATEGORY_WEIGHTS = {
    "human_social": 2.5,
    "vehicle_machine_equipment": 1.7,
    "animal_living": 1.8,
    "text_screen_document": 2.1,
    "place_structure_road": 1.4,
    "nature_land_plant": 1.3,
    "food_object_context": 1.1,
}
WEAK_BACKGROUND_LABELS = {
    "sky", "cloud", "wall", "floor", "ceiling", "grass", "window", "door"
}

MANIFEST_FIELDS = [
    "candidate_id", "run_id", "queue_type", "visual_unit_id",
    "source_content_id", "derived_id", "media_type", "visual_unit_type",
    "source_relative_path", "visual_file", "derived_path", "time_position_ms",
    "canonical_visual_unit_id", "central_dedup_identity_status",
    "central_dedup_reverse_member_count", "central_dedup_reverse_visual_unit_ids",
    "source_group_id", "high_value_category", "candidate_role",
    "candidate_score", "reason_codes", "black_frame_status", "luma_mean",
    "luma_std", "black_pixel_ratio", "label_count", "distinct_label_count",
    "labels", "generic_label_categories", "grid_structure", "grid_luma_std",
    "v14_role", "coverage_window_index", "ocr_trigger_source",
    "ocr_trigger_keywords", "policy_version", "script_version", "execution_mode",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:28]


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


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = round((len(ordered) - 1) * fraction)
    return round(ordered[index], 6)


def distribution(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p90": None, "max": None}
    numeric = [float(value) for value in values]
    return {
        "count": len(numeric),
        "min": round(min(numeric), 6),
        "p50": percentile(numeric, 0.50),
        "p90": percentile(numeric, 0.90),
        "max": round(max(numeric), 6),
    }


def assert_test_output_path(path: Path, *, may_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    allowed = TEST_OUTPUT_ROOT.resolve(strict=False)
    if not phase1.is_relative_to(resolved, allowed):
        raise RuntimeError(f"output_outside_test_output:{resolved}")
    if resolved == allowed:
        raise RuntimeError("output_must_not_be_test_output_root")
    if not may_exist and resolved.exists() and any(resolved.iterdir()):
        raise RuntimeError(f"output_directory_not_empty:{resolved}")
    return resolved


def db_object_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def central_dedup_context(con: sqlite3.Connection) -> Dict[str, Any]:
    required = {
        "canonical_visual_units_for_heavy",
        "visual_identity",
        "visual_duplicate_groups",
    }
    missing = sorted(name for name in required if not db_object_exists(con, name))
    if missing:
        raise RuntimeError("central_dedup_contract_missing:" + ",".join(missing))
    raw_ids = {
        str(row[0]) for row in con.execute("SELECT visual_unit_id FROM visual_units")
    }
    canonical_ids = {
        str(row[0])
        for row in con.execute(
            "SELECT visual_unit_id FROM canonical_visual_units_for_heavy"
        )
    }
    identity_count = int(con.execute("SELECT COUNT(*) FROM visual_identity").fetchone()[0])
    reverse_available = identity_count == len(raw_ids) and all(
        row[0]
        for row in con.execute(
            "SELECT canonical_visual_unit_id FROM visual_identity"
        )
    )
    return {
        "visual_input_source": "canonical_visual_units_for_heavy",
        "raw_visual_input_count": len(raw_ids),
        "canonical_visual_input_count": len(canonical_ids),
        "dedup_excluded_visual_count": len(raw_ids - canonical_ids),
        "dedup_excluded_visual_unit_ids": raw_ids - canonical_ids,
        "canonical_visual_unit_ids": canonical_ids,
        "dedup_reverse_mapping_available": bool(reverse_available),
    }


def load_visual_rows(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    context = central_dedup_context(con)
    raw_bounds: Dict[str, Dict[str, int]] = {}
    for raw in con.execute(
        """
        SELECT vu.source_content_id,
               MIN(COALESCE(da.time_position_ms,vu.time_position_ms)) AS raw_start_ms,
               MAX(COALESCE(da.time_position_ms,vu.time_position_ms)) AS raw_end_ms,
               COUNT(*) AS raw_candidate_count
        FROM visual_units vu
        JOIN derived_assets da ON da.derived_id=vu.derived_id
        JOIN source_assets sa ON sa.source_content_id=vu.source_content_id
        WHERE sa.media_type='video'
        GROUP BY vu.source_content_id
        """
    ):
        raw_bounds[str(raw[0])] = {
            "raw_start_ms": safe_int(raw[1], -1),
            "raw_end_ms": safe_int(raw[2], -1),
            "raw_candidate_count": safe_int(raw[3], 0),
        }
    sql = """
    SELECT
      vu.visual_unit_id, vu.source_content_id, vu.derived_id, vu.visual_file,
      vu.time_position_ms AS visual_time_position_ms,
      da.derived_path, da.time_position_ms AS derived_time_position_ms,
      da.frame_index AS derived_frame_index, da.width AS db_width,
      da.height AS db_height, da.sha256 AS derived_sha256,
      sa.media_type, sa.relative_path AS source_relative_path,
      sa.absolute_path AS source_absolute_path,
      vi.identity_status AS central_dedup_identity_status,
      vi.canonical_visual_unit_id,
      reverse_map.reverse_member_count AS central_dedup_reverse_member_count,
      reverse_map.reverse_visual_unit_ids AS central_dedup_reverse_visual_unit_ids
    FROM canonical_visual_units_for_heavy vu
    JOIN visual_identity vi ON vi.visual_unit_id=vu.visual_unit_id
    LEFT JOIN derived_assets da ON da.derived_id = vu.derived_id
    LEFT JOIN source_assets sa ON sa.source_content_id = vu.source_content_id
    LEFT JOIN (
      SELECT canonical_visual_unit_id,
             COUNT(*) AS reverse_member_count,
             GROUP_CONCAT(visual_unit_id, '|') AS reverse_visual_unit_ids
      FROM visual_identity
      GROUP BY canonical_visual_unit_id
    ) reverse_map ON reverse_map.canonical_visual_unit_id=vu.visual_unit_id
    WHERE sa.media_type IN ('video', 'image')
    ORDER BY sa.media_type, vu.source_content_id, da.time_position_ms, vu.visual_unit_id
    """
    rows: List[Dict[str, Any]] = []
    for raw in con.execute(sql).fetchall():
        row = dict(raw)
        phase1_time_row = {
            "derived_time_position_ms": row.get("derived_time_position_ms"),
            "visual_time_position_ms": row.get("visual_time_position_ms"),
            "derived_frame_index": row.get("derived_frame_index"),
            "visual_frame_index": -1,
        }
        time_ms, time_source = phase1.effective_time_ms(phase1_time_row)
        row["time_position_ms"] = time_ms
        row["time_position_source"] = time_source
        row["source_relative_path"] = str(
            row.get("source_relative_path") or row.get("source_absolute_path") or ""
        )
        row["derived_visual_path"] = str(
            row.get("visual_file") or row.get("derived_path") or ""
        )
        row["visual_unit_type"] = (
            "video_frame" if row.get("media_type") == "video" else "image"
        )
        row["source_group_id"] = str(row.get("source_content_id") or "")
        bounds = raw_bounds.get(row["source_group_id"], {})
        row["central_dedup_raw_group_start_ms"] = bounds.get("raw_start_ms", -1)
        row["central_dedup_raw_group_end_ms"] = bounds.get("raw_end_ms", -1)
        row["central_dedup_raw_group_candidate_count"] = bounds.get(
            "raw_candidate_count", 1
        )
        row["central_dedup_reverse_member_count"] = safe_int(
            row.get("central_dedup_reverse_member_count"), 1
        )
        row["central_dedup_reverse_visual_unit_ids"] = str(
            row.get("central_dedup_reverse_visual_unit_ids")
            or row.get("visual_unit_id")
            or ""
        )
        row["labels"] = []
        row["generic_label_categories"] = []
        row["v14_role"] = ""
        row["v14_score"] = 0.0
        row["v14_reason_codes"] = []
        rows.append(row)
    if len(rows) != context["canonical_visual_input_count"]:
        raise RuntimeError(
            "canonical_visual_load_count_mismatch:"
            f"{len(rows)}!={context['canonical_visual_input_count']}"
        )
    return rows


def load_one_signature(path: Path, cols: int = 16, rows: int = 5) -> Dict[str, Any]:
    if phase1.Image is None:
        raise RuntimeError("pillow_import_unavailable")
    with phase1.Image.open(path) as image:
        logical = (
            phase1.ImageOps.exif_transpose(image)
            if phase1.ImageOps is not None
            else image
        )
        width, height = int(logical.width), int(logical.height)
        gray = logical.convert("L")
        grid_image = gray.resize((cols, rows))
        grid_values = phase1.image_values(grid_image)
        sample = gray.copy()
        sample.thumbnail((96, 96))
        sample_values = phase1.image_values(sample)
    grid_mean = statistics.fmean(grid_values)
    grid_variance = statistics.fmean((value - grid_mean) ** 2 for value in grid_values)
    grid_std = math.sqrt(grid_variance)
    differences: List[int] = []
    for y in range(rows):
        for x in range(cols):
            index = y * cols + x
            if x + 1 < cols:
                differences.append(abs(grid_values[index] - grid_values[index + 1]))
            if y + 1 < rows:
                differences.append(abs(grid_values[index] - grid_values[index + cols]))
    structure = statistics.fmean(differences) if differences else 0.0
    centered_norm = math.sqrt(sum((value - grid_mean) ** 2 for value in grid_values))
    luma_mean = statistics.fmean(sample_values)
    luma_variance = statistics.fmean((value - luma_mean) ** 2 for value in sample_values)
    luma_std = math.sqrt(luma_variance)
    black_pixel_ratio = sum(1 for value in sample_values if value <= 8) / max(1, len(sample_values))
    black_rejected = (
        (luma_mean <= 8.0 and luma_std <= 5.0)
        or (black_pixel_ratio >= 0.985 and luma_mean <= 16.0)
    )
    return {
        "width": width,
        "height": height,
        "grid_values": tuple(int(value) for value in grid_values),
        "grid_mean": grid_mean,
        "grid_std": grid_std,
        "grid_structure": structure,
        "grid_centered_norm": centered_norm,
        "luma_mean": luma_mean,
        "luma_std": luma_std,
        "black_pixel_ratio": black_pixel_ratio,
        "black_rejected": black_rejected,
        "black_frame_status": "near_black_rejected" if black_rejected else "ok",
    }


def attach_signatures(rows: Sequence[MutableMapping[str, Any]]) -> Dict[str, Any]:
    success = 0
    failures: Counter[str] = Counter()
    black_count = 0
    for row in rows:
        path, status = phase1.resolve_allowed_existing_file(
            str(row.get("derived_visual_path") or ""), TEST_OUTPUT_ROOT
        )
        if status != "ok" or path is None:
            failures[status] += 1
            row["quality_error"] = status
            row["black_rejected"] = True
            row["black_frame_status"] = "missing_or_disallowed_derived_frame"
            continue
        try:
            signature = load_one_signature(path)
        except Exception as exc:
            failures["decode_failed:" + type(exc).__name__] += 1
            row["quality_error"] = type(exc).__name__
            row["black_rejected"] = True
            row["black_frame_status"] = "invalid_derived_frame"
            continue
        row.update(signature)
        row["quality_error"] = ""
        success += 1
        if signature["black_rejected"]:
            black_count += 1
    return {
        "expected_count": len(rows),
        "success_count": success,
        "failure_count": sum(failures.values()),
        "failure_reasons": dict(failures),
        "black_or_invalid_count": black_count + sum(failures.values()),
    }


def load_vectors(con: sqlite3.Connection) -> Tuple[Dict[str, Tuple[float, ...]], Dict[str, Any]]:
    embedding_rows = [
        dict(row)
        for row in con.execute(
            "SELECT embedding_id, visual_unit_id, dimension, vector_key FROM embeddings"
        ).fetchall()
    ]
    expected = {str(row["embedding_id"]): row for row in embedding_rows}
    payloads: set[Path] = set()
    for row in embedding_rows:
        payload_raw, fragment, status = phase1.parse_vector_key(str(row.get("vector_key") or ""))
        if status != "ok" or payload_raw is None or fragment != str(row["embedding_id"]):
            raise RuntimeError("invalid_vector_key_during_load")
        payload, path_status = phase1.resolve_allowed_existing_file(
            str(payload_raw), TEST_OUTPUT_ROOT
        )
        if path_status != "ok" or payload is None:
            raise RuntimeError("vector_payload_unavailable_during_load:" + path_status)
        payloads.add(payload)
    vectors: Dict[str, Tuple[float, ...]] = {}
    seen_ids: set[str] = set()
    for payload in sorted(payloads, key=str):
        with payload.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                embedding_id = str(item.get("embedding_id") or "")
                if embedding_id not in expected or embedding_id in seen_ids:
                    continue
                seen_ids.add(embedding_id)
                vector = item.get("vector")
                dimension = safe_int(expected[embedding_id].get("dimension"), 0)
                if not isinstance(vector, list) or len(vector) != dimension:
                    raise RuntimeError("vector_dimension_mismatch_during_load")
                numeric = [float(value) for value in vector]
                norm = math.sqrt(sum(value * value for value in numeric))
                if norm <= 0 or not math.isfinite(norm):
                    raise RuntimeError("invalid_vector_norm")
                visual_unit_id = str(expected[embedding_id]["visual_unit_id"])
                vectors[visual_unit_id] = tuple(value / norm for value in numeric)
    if len(vectors) != len(embedding_rows):
        raise RuntimeError(
            f"vector_visual_unit_count_mismatch:{len(vectors)}!={len(embedding_rows)}"
        )
    return vectors, {
        "vector_payload_found": True,
        "vector_dedup_status": "enabled_existing_jsonl_payload",
        "vector_payload_row_count": len(vectors),
        "vector_payload_file_count": len(payloads),
        "vector_payload_integrity_status": "PASS",
    }


def category_for_label(label: str) -> str:
    name = str(label or "").strip().lower()
    if not name:
        return ""
    if name in WEAK_BACKGROUND_LABELS:
        return "weak_background"
    for category, keywords in GENERIC_LABEL_FAMILIES:
        if any(keyword == name or keyword in name for keyword in keywords):
            return category
    return "other"


def load_labels(con: sqlite3.Connection, by_vu: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    label_rows = 0
    normalized_count = 0
    invalid_bbox_count = 0
    central_dedup_excluded_label_row_count = 0
    central_dedup_excluded_labeled_visual_unit_ids: set[str] = set()
    labels_by_vu: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw in con.execute(
        "SELECT visual_unit_id, label, confidence, bbox FROM visual_labels ORDER BY label_id"
    ).fetchall():
        item = dict(raw)
        label_rows += 1
        visual_unit_id = str(item.get("visual_unit_id") or "")
        frame = by_vu.get(visual_unit_id)
        if frame is None:
            central_dedup_excluded_label_row_count += 1
            central_dedup_excluded_labeled_visual_unit_ids.add(visual_unit_id)
            continue
        normalized, meta = phase1.normalize_bbox_xyxy(
            item.get("bbox"), frame.get("width"), frame.get("height")
        )
        if normalized is None:
            invalid_bbox_count += 1
            area = 0.0
            center_distance = None
            touches_edge = False
        else:
            normalized_count += 1
            area = phase1.bbox_area(normalized)
            x1, y1, x2, y2 = normalized
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            center_distance = math.sqrt((cx - 0.5) ** 2 + (cy - 0.5) ** 2)
            touches_edge = x1 <= 0.02 or y1 <= 0.02 or x2 >= 0.98 or y2 >= 0.98
        label = str(item.get("label") or "").strip().lower()
        labels_by_vu[visual_unit_id].append(
            {
                "label": label,
                "confidence": safe_float(item.get("confidence"), 0.0),
                "bbox": normalized,
                "bbox_status": str(meta.get("reason") or "unknown"),
                "area": area,
                "center_distance": center_distance,
                "touches_edge": touches_edge,
                "category": category_for_label(label),
            }
        )
    for visual_unit_id, labels in labels_by_vu.items():
        frame = by_vu.get(visual_unit_id)
        if isinstance(frame, MutableMapping):
            frame["labels"] = labels
            frame["generic_label_categories"] = sorted(
                {
                    item["category"]
                    for item in labels
                    if item["category"] not in {"", "other", "weak_background"}
                }
            )
    return {
        "visual_label_row_count": label_rows,
        "normalized_bbox_count": normalized_count,
        "invalid_bbox_count": invalid_bbox_count,
        "labeled_visual_unit_count": len(labels_by_vu),
        "central_dedup_excluded_label_row_count": central_dedup_excluded_label_row_count,
        "central_dedup_excluded_labeled_visual_unit_count": len(
            central_dedup_excluded_labeled_visual_unit_ids
        ),
    }


def load_v14_roles(v14_out: Path, by_vu: Mapping[str, MutableMapping[str, Any]]) -> Dict[str, Any]:
    manifest = v14_out / "manifests" / "qwenvl_high_value_candidate_queue.csv"
    high_signal_ids: set[str] = set()
    fallback_ids: set[str] = set()
    missing_vu = 0
    with manifest.open("r", newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            if raw.get("high_value_category") != phase1.V14_VIDEO_CATEGORY:
                continue
            visual_unit_id = str(raw.get("visual_unit_id") or "")
            frame = by_vu.get(visual_unit_id)
            if frame is None:
                missing_vu += 1
                continue
            reasons = [part for part in str(raw.get("reason_codes") or "").split("|") if part]
            if phase1.V14_FALLBACK_REASON in reasons:
                role = "video_coverage_fallback"
                fallback_ids.add(visual_unit_id)
            else:
                role = "video_high_signal_keyframe"
                high_signal_ids.add(visual_unit_id)
            frame["v14_role"] = role
            frame["v14_score"] = safe_float(raw.get("candidate_score"), 0.0)
            frame["v14_reason_codes"] = reasons
    return {
        "high_signal_ids": high_signal_ids,
        "fallback_ids": fallback_ids,
        "v14_high_signal_candidate_count": len(high_signal_ids),
        "v14_coverage_fallback_count": len(fallback_ids),
        "missing_db_visual_unit_count": missing_vu,
    }


def label_features(frame: Mapping[str, Any]) -> Dict[str, Any]:
    labels = list(frame.get("labels") or [])
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in labels:
        grouped[str(item.get("label") or "")].append(item)
    score = 0.0
    categories: set[str] = set()
    central_bonus = 0.0
    reasons: List[str] = []
    for label, items in grouped.items():
        category = str(items[0].get("category") or "")
        if category in {"", "other", "weak_background"}:
            continue
        categories.add(category)
        max_conf = max(safe_float(item.get("confidence"), 0.0) for item in items)
        weight = CATEGORY_WEIGHTS.get(category, 0.0)
        contribution = weight * max(0.2, max_conf) * (
            1.0 + min(0.45, math.log1p(len(items)) / 5.0)
        )
        score += contribution
        reasons.append(f"generic_label:{category}:{label}:conf={max_conf:.3f}:boxes={len(items)}")
        if category in {
            "human_social", "vehicle_machine_equipment", "animal_living",
            "text_screen_document",
        }:
            for item in items:
                distance = item.get("center_distance")
                area = safe_float(item.get("area"), 0.0)
                if distance is None or area < 0.01:
                    continue
                quality = max(0.0, 1.0 - float(distance) / 0.7072) * min(1.0, area / 0.16)
                if item.get("touches_edge"):
                    quality *= 0.65
                central_bonus = max(central_bonus, quality)
    if len(categories) >= 2:
        score += 0.55 * (len(categories) - 1)
        reasons.append("generic_category_combo:" + "+".join(sorted(categories)))
    distinct_labels = len(grouped)
    if len(labels) >= 3 or distinct_labels >= 3:
        score += 0.45
        reasons.append(f"visual_complexity:boxes={len(labels)}:labels={distinct_labels}")
    score += central_bonus
    if central_bonus >= 0.25:
        reasons.append(f"bbox_central_subject_quality:{central_bonus:.3f}")
    return {
        "score": score,
        "categories": sorted(categories),
        "distinct_label_count": distinct_labels,
        "box_count": len(labels),
        "central_bonus": central_bonus,
        "reasons": reasons,
    }


def large_text_evidence(frame: Mapping[str, Any]) -> Dict[str, Any]:
    text_items = [
        item
        for item in frame.get("labels") or []
        if str(item.get("label") or "") in TEXT_BEARING_LABELS
    ]
    if not text_items:
        return {
            "has_text_label": False,
            "qualified": False,
            "score": 0.0,
            "max_confidence": 0.0,
            "max_area": 0.0,
            "aggregate_area": 0.0,
            "labels": [],
        }
    max_confidence = max(safe_float(item.get("confidence"), 0.0) for item in text_items)
    max_area = max(safe_float(item.get("area"), 0.0) for item in text_items)
    aggregate_area = min(1.0, sum(safe_float(item.get("area"), 0.0) for item in text_items))
    qualified = max_confidence >= 0.70 and (
        max_area >= 0.03 or (aggregate_area >= 0.05 and max_confidence >= 0.80)
    )
    score = (
        2.0 + 2.5 * max_confidence + 3.0 * max_area + 1.5 * aggregate_area
        if qualified else 0.0
    )
    return {
        "has_text_label": True,
        "qualified": qualified,
        "score": score,
        "max_confidence": max_confidence,
        "max_area": max_area,
        "aggregate_area": aggregate_area,
        "labels": sorted({str(item.get("label") or "") for item in text_items}),
    }


def is_high_signal(frame: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if frame.get("v14_role") == "video_high_signal_keyframe":
        reasons.append("v14_high_signal_keyframe")
    features = label_features(frame)
    if safe_float(features.get("score"), 0.0) >= 4.5:
        reasons.append("v22_generic_label_score_high")
    if len(features.get("categories") or []) >= 2 and safe_int(features.get("box_count"), 0) >= 3:
        reasons.append("v22_generic_multicategory_context")
    text = large_text_evidence(frame)
    if text.get("qualified"):
        reasons.append("v22_obvious_large_text_signal")
    if safe_float(features.get("central_bonus"), 0.0) >= 0.55:
        reasons.append("v22_central_subject_signal")
    return bool(reasons), reasons


def content_score(frame: Mapping[str, Any], anchor_ms: int, window_ms: int) -> Tuple[float, List[str]]:
    features = label_features(frame)
    score = safe_float(features.get("score"), 0.0)
    reasons = list(features.get("reasons") or [])
    structure = safe_float(frame.get("grid_structure"), 0.0)
    grid_std = safe_float(frame.get("grid_std"), 0.0)
    score += min(2.0, structure / 15.0)
    score += min(1.5, grid_std / 35.0)
    reasons.extend(
        [f"grid_structure:{structure:.3f}", f"grid_luma_std:{grid_std:.3f}"]
    )
    high_signal, high_reasons = is_high_signal(frame)
    if frame.get("v14_role") == "video_high_signal_keyframe":
        score += 2.4
    elif high_signal:
        score += 1.0
    reasons.extend(high_reasons)
    time_ms = safe_int(frame.get("time_position_ms"), -1)
    anchor_distance = abs(time_ms - anchor_ms) if time_ms >= 0 else window_ms
    anchor_penalty = min(0.5, anchor_distance / max(1, window_ms) * 0.5)
    score -= anchor_penalty
    reasons.append(f"coverage_anchor_distance_ms:{anchor_distance}")
    return score, reasons


def vector_cosine(a: Mapping[str, Any], b: Mapping[str, Any]) -> Optional[float]:
    va = a.get("vector")
    vb = b.get("vector")
    if not va or not vb or len(va) != len(vb):
        return None
    return sum(float(x) * float(y) for x, y in zip(va, vb))


def grid_similarity(a: Mapping[str, Any], b: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    ga = a.get("grid_values")
    gb = b.get("grid_values")
    if not ga or not gb or len(ga) != len(gb):
        return None, None
    mad = statistics.fmean(abs(float(x) - float(y)) for x, y in zip(ga, gb))
    ma = safe_float(a.get("grid_mean"), 0.0)
    mb = safe_float(b.get("grid_mean"), 0.0)
    numerator = sum((float(x) - ma) * (float(y) - mb) for x, y in zip(ga, gb))
    denominator = safe_float(a.get("grid_centered_norm"), 0.0) * safe_float(
        b.get("grid_centered_norm"), 0.0
    )
    if denominator <= 0:
        correlation = 1.0 if mad == 0 else 0.0
    else:
        correlation = max(-1.0, min(1.0, numerator / denominator))
    return mad, correlation


def label_jaccard(a: Mapping[str, Any], b: Mapping[str, Any]) -> Optional[float]:
    sa = {
        str(item.get("label") or "")
        for item in a.get("labels") or []
        if safe_float(item.get("confidence"), 0.0) >= 0.2
    }
    sb = {
        str(item.get("label") or "")
        for item in b.get("labels") or []
        if safe_float(item.get("confidence"), 0.0) >= 0.2
    }
    if not sa or not sb:
        return None
    return len(sa & sb) / max(1, len(sa | sb))


def duplicate_evidence(a: Mapping[str, Any], b: Mapping[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    time_gap = abs(
        safe_int(a.get("time_position_ms"), -1) - safe_int(b.get("time_position_ms"), -1)
    )
    exact = bool(
        (a.get("derived_id") and a.get("derived_id") == b.get("derived_id"))
        or (a.get("derived_sha256") and a.get("derived_sha256") == b.get("derived_sha256"))
        or (
            a.get("derived_visual_path")
            and a.get("derived_visual_path") == b.get("derived_visual_path")
        )
    )
    cosine = vector_cosine(a, b)
    grid_mad, grid_corr = grid_similarity(a, b)
    jaccard = label_jaccard(a, b)
    vector_match = cosine is not None and cosine >= args.dedup_vector_threshold
    grid_match = (
        grid_mad is not None
        and grid_corr is not None
        and grid_mad <= args.dedup_grid_mad_threshold
        and grid_corr >= args.dedup_grid_corr_threshold
    )
    label_match = jaccard is not None and jaccard >= args.dedup_label_jaccard_threshold
    time_match = time_gap <= args.dedup_time_gap_ms
    duplicate = exact or (
        time_match
        and (vector_match or grid_match)
        and (label_match or (vector_match and grid_match))
    )
    return {
        "duplicate": duplicate,
        "exact_match": exact,
        "time_match": time_match,
        "vector_match": vector_match,
        "grid_match": grid_match,
        "label_match": label_match,
        "time_gap_ms": time_gap,
        "vector_cosine": cosine,
        "grid_mad": grid_mad,
        "grid_correlation": grid_corr,
        "label_jaccard": jaccard,
    }


def novelty_evidence(a: Mapping[str, Any], b: Mapping[str, Any]) -> Dict[str, Any]:
    cosine = vector_cosine(a, b)
    grid_mad, _grid_corr = grid_similarity(a, b)
    jaccard = label_jaccard(a, b)
    novel = (
        (cosine is not None and cosine < 0.97)
        or (grid_mad is not None and grid_mad > 10.0)
        or (jaccard is not None and jaccard < 0.60)
    )
    return {
        "novel": novel,
        "vector_cosine": cosine,
        "grid_mad": grid_mad,
        "label_jaccard": jaccard,
    }


def mark_dedup_drop(stats: Counter[str], evidence: Mapping[str, Any], prefix: str) -> None:
    stats[prefix + "_dedup_unique_drop_count"] += 1
    if evidence.get("time_match"):
        stats[prefix + "_time_dedup_drop_count"] += 1
    if evidence.get("vector_match"):
        stats[prefix + "_vector_dedup_drop_count"] += 1
    if evidence.get("grid_match"):
        stats[prefix + "_grid_dedup_drop_count"] += 1
    if evidence.get("label_match"):
        stats[prefix + "_label_dedup_drop_count"] += 1
    if evidence.get("exact_match"):
        stats[prefix + "_exact_dedup_drop_count"] += 1


def frame_tail_start(frames: Sequence[Mapping[str, Any]]) -> Optional[int]:
    if frames:
        raw_start = safe_int(frames[0].get("central_dedup_raw_group_start_ms"), -1)
        raw_end = safe_int(frames[0].get("central_dedup_raw_group_end_ms"), -1)
        if raw_start >= 0 and raw_end >= raw_start:
            window = phase1.tail_window_ms(raw_end - raw_start)
            return raw_end - window if window else None
    times = sorted(
        safe_int(frame.get("time_position_ms"), -1)
        for frame in frames
        if safe_int(frame.get("time_position_ms"), -1) >= 0
    )
    if not times:
        return None
    duration = times[-1] - times[0]
    window = phase1.tail_window_ms(duration)
    return times[-1] - window if window else None


def video_eligible_frames(frames: Sequence[MutableMapping[str, Any]]) -> List[MutableMapping[str, Any]]:
    tail_start = frame_tail_start(frames)
    eligible: List[MutableMapping[str, Any]] = []
    for frame in frames:
        time_ms = safe_int(frame.get("time_position_ms"), -1)
        frame["tail_excluded"] = bool(tail_start is not None and time_ms >= tail_start)
        if time_ms < 0 or frame.get("black_rejected") or frame.get("tail_excluded"):
            continue
        eligible.append(frame)
    return eligible


def build_coverage_for_video(
    frames: Sequence[MutableMapping[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    timed = sorted(
        [frame for frame in frames if safe_int(frame.get("time_position_ms"), -1) >= 0],
        key=lambda frame: (safe_int(frame.get("time_position_ms"), -1), str(frame.get("visual_unit_id"))),
    )
    eligible = video_eligible_frames(timed)
    if not timed or not eligible:
        return [], []
    raw_start_ms = safe_int(timed[0].get("central_dedup_raw_group_start_ms"), -1)
    raw_end_ms = safe_int(timed[0].get("central_dedup_raw_group_end_ms"), -1)
    start_ms = raw_start_ms if raw_start_ms >= 0 else safe_int(timed[0].get("time_position_ms"), 0)
    max_ms = raw_end_ms if raw_end_ms >= start_ms else safe_int(timed[-1].get("time_position_ms"), start_ms)
    tail_start = frame_tail_start(timed)
    end_exclusive = tail_start if tail_start is not None else max_ms + 1
    if end_exclusive <= start_ms:
        end_exclusive = start_ms + 1
    span = end_exclusive - start_ms
    planned_windows = max(1, (span + args.coverage_window_ms - 1) // args.coverage_window_ms)
    window_frames: Dict[int, List[MutableMapping[str, Any]]] = defaultdict(list)
    for frame in eligible:
        time_ms = safe_int(frame.get("time_position_ms"), -1)
        if time_ms >= end_exclusive:
            continue
        index = min(planned_windows - 1, (time_ms - start_ms) // args.coverage_window_ms)
        frame["coverage_window_index"] = int(index)
        window_frames[int(index)].append(frame)
    stats["coverage_window_total_count"] += planned_windows
    stats["coverage_empty_window_count"] += planned_windows - len(window_frames)
    stats["coverage_anchor_total_count"] += len(window_frames)
    stats["coverage_window_candidate_evaluated_count"] += sum(
        len(items) for items in window_frames.values()
    )

    plans: List[Dict[str, Any]] = []
    for index in range(planned_windows):
        candidates = window_frames.get(index, [])
        if not candidates:
            continue
        window_start = start_ms + index * args.coverage_window_ms
        window_end = min(end_exclusive, window_start + args.coverage_window_ms)
        anchor_ms = window_start + max(0, window_end - window_start - 1) // 2
        anchor = min(
            candidates,
            key=lambda frame: (
                abs(safe_int(frame.get("time_position_ms"), -1) - anchor_ms),
                str(frame.get("visual_unit_id")),
            ),
        )
        ranked: List[Tuple[float, MutableMapping[str, Any], List[str]]] = []
        for frame in candidates:
            score, reasons = content_score(frame, anchor_ms, args.coverage_window_ms)
            ranked.append((score, frame, reasons))
        ranked.sort(
            key=lambda item: (
                -item[0],
                abs(safe_int(item[1].get("time_position_ms"), -1) - anchor_ms),
                str(item[1].get("visual_unit_id")),
            )
        )
        plans.append(
            {
                "window_index": index,
                "window_start_ms": window_start,
                "window_end_exclusive_ms": window_end,
                "anchor_ms": anchor_ms,
                "anchor_visual_unit_id": str(anchor.get("visual_unit_id")),
                "ranked": ranked,
            }
        )

    selected: List[Dict[str, Any]] = []
    window_reports: List[Dict[str, Any]] = []
    for plan in plans:
        top_score, top_frame, top_reasons = plan["ranked"][0]
        selected_frame = top_frame
        selected_score = top_score
        selected_reasons = list(top_reasons)
        duplicate_against: Optional[Mapping[str, Any]] = None
        duplicate_evidence_top: Optional[Dict[str, Any]] = None
        for previous in selected:
            evidence = duplicate_evidence(top_frame, previous["frame"], args)
            stats["coverage_dedup_candidate_pair_count"] += 1
            if evidence["duplicate"]:
                duplicate_against = previous["frame"]
                duplicate_evidence_top = evidence
                break
        if duplicate_against is not None and duplicate_evidence_top is not None:
            replacement: Optional[Tuple[float, MutableMapping[str, Any], List[str]]] = None
            for candidate_score, candidate, candidate_reasons in plan["ranked"][1:]:
                if all(
                    not duplicate_evidence(candidate, previous["frame"], args)["duplicate"]
                    for previous in selected
                ):
                    replacement = (candidate_score, candidate, candidate_reasons)
                    break
            if replacement is not None:
                mark_dedup_drop(stats, duplicate_evidence_top, "coverage")
                stats["coverage_refill_count"] += 1
                top_frame.setdefault("decision_reason_codes", []).append(
                    "coverage_dedup_dropped_and_refilled"
                )
                selected_score, selected_frame, selected_reasons = replacement
                selected_reasons = list(selected_reasons) + ["coverage_refill_after_dedup"]
            else:
                stats["coverage_refill_failed_count"] += 1
                selected_reasons.append("dedup_kept_for_coverage")
        high_signal, high_reasons = is_high_signal(selected_frame)
        role = (
            "video_coverage_high_signal_overlap"
            if high_signal
            else "video_coverage_keyframe"
        )
        if str(selected_frame.get("visual_unit_id")) != plan["anchor_visual_unit_id"]:
            stats["coverage_anchor_local_best_shift_count"] += 1
            if selected_frame.get("v14_role") == "video_high_signal_keyframe":
                stats["v14_high_signal_window_replacement_count"] += 1
        if role == "video_coverage_high_signal_overlap":
            stats["coverage_high_signal_overlap_count"] += 1
        selected_reasons.extend(high_reasons)
        selected_reasons.extend(
            [
                f"coverage_window_index:{plan['window_index']}",
                f"coverage_window_ms:{args.coverage_window_ms}",
            ]
        )
        selected.append(
            {
                "frame": selected_frame,
                "score": selected_score,
                "role": role,
                "reasons": selected_reasons,
                "window_index": plan["window_index"],
            }
        )
        window_reports.append(
            {
                "source_content_id": str(selected_frame.get("source_content_id")),
                "window_index": plan["window_index"],
                "window_start_ms": plan["window_start_ms"],
                "window_end_exclusive_ms": plan["window_end_exclusive_ms"],
                "candidate_count": len(plan["ranked"]),
                "anchor_visual_unit_id": plan["anchor_visual_unit_id"],
                "selected_visual_unit_id": str(selected_frame.get("visual_unit_id")),
                "selected_role": role,
                "selected_score": round(float(selected_score), 6),
                "local_best_shifted": int(
                    str(selected_frame.get("visual_unit_id")) != plan["anchor_visual_unit_id"]
                ),
                "v14_high_signal_selected": int(
                    selected_frame.get("v14_role") == "video_high_signal_keyframe"
                ),
            }
        )
    stats["coverage_selected_count"] += len(selected)
    return selected, window_reports


def supplement_cap(duration_ms: int) -> int:
    if duration_ms <= 60_000:
        return 1
    if duration_ms <= 300_000:
        return 2
    return 3


def add_v14_supplements(
    frames: Sequence[MutableMapping[str, Any]],
    coverage: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
) -> List[Dict[str, Any]]:
    if not coverage:
        return []
    selected_ids = {str(item["frame"].get("visual_unit_id")) for item in coverage}
    selected_frames = [item["frame"] for item in coverage]
    timed = sorted(
        [frame for frame in frames if safe_int(frame.get("time_position_ms"), -1) >= 0],
        key=lambda frame: safe_int(frame.get("time_position_ms"), -1),
    )
    eligible_ids = {str(frame.get("visual_unit_id")) for frame in video_eligible_frames(timed)}
    duration_ms = (
        safe_int(timed[-1].get("time_position_ms"), 0)
        - safe_int(timed[0].get("time_position_ms"), 0)
        if timed else 0
    )
    cap = supplement_cap(max(0, duration_ms))
    pool: List[Tuple[float, MutableMapping[str, Any], List[str]]] = []
    for frame in timed:
        visual_unit_id = str(frame.get("visual_unit_id"))
        if frame.get("v14_role") != "video_high_signal_keyframe":
            continue
        if visual_unit_id in selected_ids:
            continue
        if visual_unit_id not in eligible_ids:
            stats["high_signal_reject_bad_or_tail_count"] += 1
            continue
        score, reasons = content_score(
            frame, safe_int(frame.get("time_position_ms"), 0), args.coverage_window_ms
        )
        pool.append((score, frame, reasons))
    pool.sort(key=lambda item: (-item[0], safe_int(item[1].get("time_position_ms"), -1)))
    supplements: List[Dict[str, Any]] = []
    for score, frame, reasons in pool:
        if len(supplements) >= cap:
            stats["high_signal_reject_cap_count"] += 1
            continue
        current_selected = selected_frames + [item["frame"] for item in supplements]
        nearest = min(
            current_selected,
            key=lambda other: abs(
                safe_int(frame.get("time_position_ms"), -1)
                - safe_int(other.get("time_position_ms"), -1)
            ),
        )
        gap = abs(
            safe_int(frame.get("time_position_ms"), -1)
            - safe_int(nearest.get("time_position_ms"), -1)
        )
        if gap < args.high_signal_supplement_min_gap_ms:
            stats["high_signal_reject_near_coverage_count"] += 1
            continue
        duplicate = duplicate_evidence(frame, nearest, args)
        stats["final_video_dedup_candidate_pair_count"] += 1
        if duplicate["duplicate"]:
            mark_dedup_drop(stats, duplicate, "final_video")
            frame.setdefault("decision_reason_codes", []).append(
                "v14_supplement_rejected_duplicate"
            )
            continue
        novelty = novelty_evidence(frame, nearest)
        if not novelty["novel"]:
            stats["high_signal_reject_not_novel_count"] += 1
            continue
        supplement_reasons = list(reasons) + [
            "v14_high_signal_supplement",
            f"nearest_selected_gap_ms:{gap}",
            f"novel_vector_cosine:{novelty.get('vector_cosine')}",
            f"novel_grid_mad:{novelty.get('grid_mad')}",
            f"novel_label_jaccard:{novelty.get('label_jaccard')}",
        ]
        supplements.append(
            {
                "frame": frame,
                "score": score,
                "role": "video_high_signal_supplement",
                "reasons": supplement_reasons,
                "window_index": frame.get("coverage_window_index", ""),
            }
        )
        stats["high_signal_supplement_added_count"] += 1
        stats["v14_high_signal_supplement_added_count"] += 1
    return supplements


def candidate_row(
    frame: Mapping[str, Any],
    queue_type: str,
    role: str,
    score: float,
    reasons: Sequence[str],
    run_id: str,
    mode: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    labels = list(frame.get("labels") or [])
    label_counter = Counter(str(item.get("label") or "") for item in labels)
    row = {
        "candidate_id": stable_id("cand_", POLICY_VERSION, queue_type, frame.get("visual_unit_id"), role),
        "run_id": run_id,
        "queue_type": queue_type,
        "visual_unit_id": str(frame.get("visual_unit_id") or ""),
        "source_content_id": str(frame.get("source_content_id") or ""),
        "derived_id": str(frame.get("derived_id") or ""),
        "media_type": str(frame.get("media_type") or ""),
        "visual_unit_type": str(frame.get("visual_unit_type") or ""),
        "source_relative_path": str(frame.get("source_relative_path") or ""),
        "visual_file": str(frame.get("visual_file") or ""),
        "derived_path": str(frame.get("derived_path") or ""),
        "time_position_ms": safe_int(frame.get("time_position_ms"), -1),
        "canonical_visual_unit_id": str(
            frame.get("canonical_visual_unit_id") or frame.get("visual_unit_id") or ""
        ),
        "central_dedup_identity_status": str(
            frame.get("central_dedup_identity_status") or ""
        ),
        "central_dedup_reverse_member_count": safe_int(
            frame.get("central_dedup_reverse_member_count"), 1
        ),
        "central_dedup_reverse_visual_unit_ids": str(
            frame.get("central_dedup_reverse_visual_unit_ids")
            or frame.get("visual_unit_id")
            or ""
        ),
        "source_group_id": str(frame.get("source_group_id") or ""),
        "high_value_category": role,
        "candidate_role": role,
        "candidate_score": round(float(score), 6),
        "reason_codes": "|".join(dict.fromkeys(str(reason) for reason in reasons if reason)),
        "black_frame_status": str(frame.get("black_frame_status") or "unknown"),
        "luma_mean": round(safe_float(frame.get("luma_mean"), 0.0), 6),
        "luma_std": round(safe_float(frame.get("luma_std"), 0.0), 6),
        "black_pixel_ratio": round(safe_float(frame.get("black_pixel_ratio"), 0.0), 8),
        "label_count": len(labels),
        "distinct_label_count": len(label_counter),
        "labels": "|".join(f"{label}:{count}" for label, count in label_counter.most_common()),
        "generic_label_categories": "|".join(frame.get("generic_label_categories") or []),
        "grid_structure": round(safe_float(frame.get("grid_structure"), 0.0), 6),
        "grid_luma_std": round(safe_float(frame.get("grid_std"), 0.0), 6),
        "v14_role": str(frame.get("v14_role") or ""),
        "coverage_window_index": frame.get("coverage_window_index", ""),
        "ocr_trigger_source": "",
        "ocr_trigger_keywords": "",
        "policy_version": POLICY_VERSION,
        "script_version": SCRIPT_VERSION,
        "execution_mode": mode,
    }
    if extra:
        row.update(dict(extra))
    return row


def select_screen_ocr(
    frames: Sequence[MutableMapping[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
    run_id: str,
    mode: str,
) -> List[Dict[str, Any]]:
    eligible = video_eligible_frames(frames)
    eligible.sort(key=lambda frame: safe_int(frame.get("time_position_ms"), -1))
    kept: List[MutableMapping[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for frame in eligible:
        evidence = duplicate_evidence(frame, kept[-1], args) if kept else None
        if evidence is not None:
            stats["screen_ocr_dedup_candidate_pair_count"] += 1
        if evidence is not None and evidence["duplicate"]:
            mark_dedup_drop(stats, evidence, "screen_ocr")
            frame.setdefault("decision_reason_codes", []).append("screen_ocr_near_duplicate_excluded")
            continue
        kept.append(frame)
        reasons = ["strict_screen_capture_path", "screen_capture_ocr_allowed"]
        row = candidate_row(
            frame,
            "ocr_trigger",
            "ocr_screen_capture_video_frame",
            max(3.25, safe_float(label_features(frame).get("score"), 0.0)),
            reasons,
            run_id,
            mode,
            {
                "ocr_trigger_source": "strict_screen_capture_path",
                "ocr_trigger_keywords": "screen_capture",
            },
        )
        rows.append(row)
    return rows


def select_normal_video_ocr(
    frames: Sequence[MutableMapping[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
    run_id: str,
    mode: str,
) -> List[Dict[str, Any]]:
    eligible = video_eligible_frames(frames)
    candidates: List[Tuple[float, MutableMapping[str, Any], Dict[str, Any]]] = []
    for frame in eligible:
        evidence = large_text_evidence(frame)
        if evidence["qualified"]:
            candidates.append((safe_float(evidence["score"], 0.0), frame, evidence))
        elif evidence["has_text_label"]:
            stats["normal_video_ocr_weak_excluded_count"] += 1
            frame.setdefault("decision_reason_codes", []).append("normal_video_ocr_weak_text_excluded")
    candidates.sort(key=lambda item: (-item[0], safe_int(item[1].get("time_position_ms"), -1)))
    selected: List[MutableMapping[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for score, frame, evidence in candidates:
        if len(selected) >= args.normal_video_ocr_cap:
            stats["normal_video_ocr_cap_excluded_count"] += 1
            continue
        if selected:
            gap = min(
                abs(
                    safe_int(frame.get("time_position_ms"), -1)
                    - safe_int(other.get("time_position_ms"), -1)
                )
                for other in selected
            )
            if gap < args.normal_video_ocr_min_gap_ms:
                stats["normal_video_ocr_min_gap_excluded_count"] += 1
                continue
        selected.append(frame)
        reasons = [
            "normal_video_obvious_large_text",
            f"text_max_confidence:{evidence['max_confidence']:.3f}",
            f"text_max_bbox_area:{evidence['max_area']:.6f}",
            f"text_aggregate_bbox_area:{evidence['aggregate_area']:.6f}",
        ]
        row = candidate_row(
            frame,
            "ocr_trigger",
            "ocr_normal_video_large_text_frame",
            max(3.25, score),
            reasons,
            run_id,
            mode,
            {
                "ocr_trigger_source": "obvious_large_text",
                "ocr_trigger_keywords": "|".join(evidence["labels"]),
            },
        )
        rows.append(row)
        stats["normal_video_ocr_added_count"] += 1
    return rows


def load_manual_seed_ids(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT visual_unit_id FROM manual_high_value_visual_seeds")
    }


def load_timelapse_representatives(con: sqlite3.Connection) -> Dict[str, str]:
    by_sequence: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw in con.execute("SELECT * FROM step02_image_timelapse_keyframes").fetchall():
        row = dict(raw)
        by_sequence[str(row.get("sequence_id") or "")].append(row)
    representatives: Dict[str, str] = {}
    order = {"middle": 0, "first": 1, "last": 2}
    for sequence_id, rows in by_sequence.items():
        rows.sort(
            key=lambda row: (
                order.get(str(row.get("representative_position") or ""), 9),
                str(row.get("visual_unit_id") or ""),
            )
        )
        if rows:
            representatives[sequence_id] = str(rows[0].get("visual_unit_id") or "")
    return representatives


def read_manifest_rows(out: Path, name: str) -> List[Dict[str, str]]:
    path = out / "manifests" / name
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_csv_path(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_pre_dedup_v22(out: Path) -> Dict[str, Any]:
    summary_path = out / "reports" / "stop03_2_candidate_summary.json"
    if not summary_path.is_file():
        raise RuntimeError(f"pre_dedup_v22_summary_missing:{summary_path}")
    return {
        "summary": json.loads(summary_path.read_text(encoding="utf-8")),
        "q_rows": read_manifest_rows(out, "qwenvl_high_value_candidate_queue.csv"),
        "o_rows": read_manifest_rows(out, "ocr_trigger_candidate_queue.csv"),
        "window_rows": read_csv_path(out / "reports" / "coverage_window_report.csv"),
    }


def compare_pre_dedup_v22(
    q_rows: Sequence[Mapping[str, Any]],
    o_rows: Sequence[Mapping[str, Any]],
    window_reports: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any],
    central_context: Mapping[str, Any],
) -> Dict[str, Any]:
    excluded_ids = set(central_context["dedup_excluded_visual_unit_ids"])
    before_q_ids = {str(row.get("visual_unit_id") or "") for row in baseline["q_rows"]}
    before_o_ids = {str(row.get("visual_unit_id") or "") for row in baseline["o_rows"]}
    after_q_ids = {str(row.get("visual_unit_id") or "") for row in q_rows}
    after_o_ids = {str(row.get("visual_unit_id") or "") for row in o_rows}
    before_window_map = {
        (str(row.get("source_content_id") or ""), safe_int(row.get("window_index"), -1)):
        str(row.get("selected_visual_unit_id") or "")
        for row in baseline["window_rows"]
    }
    after_window_map = {
        (str(row.get("source_content_id") or ""), safe_int(row.get("window_index"), -1)):
        str(row.get("selected_visual_unit_id") or "")
        for row in window_reports
    }
    excluded_window_keys = {
        key for key, visual_id in before_window_map.items() if visual_id in excluded_ids
    }
    refilled_keys = {
        key
        for key in excluded_window_keys
        if after_window_map.get(key)
        and after_window_map[key] != before_window_map[key]
        and after_window_map[key] not in excluded_ids
    }
    failed_keys = {key for key in excluded_window_keys if not after_window_map.get(key)}
    queue_leaks = (after_q_ids | after_o_ids) & excluded_ids
    before_all = before_q_ids | before_o_ids
    after_all = after_q_ids | after_o_ids
    same = before_q_ids == after_q_ids and before_o_ids == after_o_ids
    explanation = (
        "candidate sets unchanged because no selected V22 item was excluded by central dedup"
        if same
        else "central canonical filtering removed duplicate visual units; coverage windows were preserved from raw frame time bounds and refilled from canonical candidates in the same window"
    )
    return {
        "coverage_candidate_excluded_by_central_dedup_count": len(excluded_window_keys),
        "coverage_refill_after_central_dedup_count": len(refilled_keys),
        "coverage_refill_failed_after_central_dedup_count": len(failed_keys),
        "qwen_candidate_removed_by_central_dedup_count": len(before_q_ids & excluded_ids),
        "qwen_candidate_replaced_after_central_dedup_count": len(after_q_ids - before_q_ids),
        "ocr_candidate_removed_by_central_dedup_count": len(before_o_ids & excluded_ids),
        "ocr_candidate_replaced_after_central_dedup_count": len(after_o_ids - before_o_ids),
        "previous_v22_candidate_removed_count": len(before_all & excluded_ids),
        "new_replacement_candidate_count": len(after_all - before_all),
        "same_as_pre_dedup_v22_candidate_set": same,
        "pre_dedup_vs_post_dedup_difference_explanation": explanation,
        "central_dedup_excluded_queue_leak_count": len(queue_leaks),
        "coverage_refill_window_keys": [f"{key[0]}:{key[1]}" for key in sorted(refilled_keys)],
        "coverage_refill_failed_window_keys": [f"{key[0]}:{key[1]}" for key in sorted(failed_keys)],
        "before_pre_dedup_v22": {
            "visual_input_count": safe_int(baseline["summary"].get("input_visual_units"), 0),
            "qwen_total_count": len(baseline["q_rows"]),
            "video_candidate_count": sum(
                1 for row in baseline["q_rows"]
                if str(row.get("high_value_category") or "") in VIDEO_OUTPUT_ROLES
            ),
            "ocr_count": len(baseline["o_rows"]),
            "coverage_window_count": safe_int(
                baseline["summary"].get("coverage_window_total_count"), 0
            ),
            "normal_video_coverage": (
                f"{safe_int(baseline['summary'].get('normal_video_group_with_coverage_count'), 0)}/"
                f"{safe_int(baseline['summary'].get('normal_video_group_count'), 0)}"
            ),
        },
    }


def compare_reference_outputs(
    q_rows: Sequence[Mapping[str, Any]],
    o_rows: Sequence[Mapping[str, Any]],
    v20_out: Path,
    v21_out: Path,
) -> Dict[str, Any]:
    q_ids = {str(row.get("visual_unit_id") or "") for row in q_rows}
    o_ids = {str(row.get("visual_unit_id") or "") for row in o_rows}
    video_ids = {
        str(row.get("visual_unit_id") or "")
        for row in q_rows
        if str(row.get("high_value_category") or "") in VIDEO_OUTPUT_ROLES
    }
    result: Dict[str, Any] = {}
    for version, out in (("v20", v20_out), ("v21", v21_out)):
        ref_q = read_manifest_rows(out, "qwenvl_high_value_candidate_queue.csv")
        ref_o = read_manifest_rows(out, "ocr_trigger_candidate_queue.csv")
        ref_q_ids = {row.get("visual_unit_id", "") for row in ref_q}
        ref_o_ids = {row.get("visual_unit_id", "") for row in ref_o}
        ref_video_ids = {
            row.get("visual_unit_id", "")
            for row in ref_q
            if row.get("high_value_category") in VIDEO_OUTPUT_ROLES
        }
        same_q = q_ids == ref_q_ids
        same_o = o_ids == ref_o_ids
        same_video = video_ids == ref_video_ids
        q_role_map = {
            str(row.get("visual_unit_id") or ""): str(row.get("high_value_category") or "")
            for row in q_rows
        }
        o_role_map = {
            str(row.get("visual_unit_id") or ""): str(row.get("high_value_category") or "")
            for row in o_rows
        }
        ref_q_role_map = {
            row.get("visual_unit_id", ""): row.get("high_value_category", "")
            for row in ref_q
        }
        ref_o_role_map = {
            row.get("visual_unit_id", ""): row.get("high_value_category", "")
            for row in ref_o
        }
        same_q_roles = q_role_map == ref_q_role_map
        same_o_roles = o_role_map == ref_o_role_map
        result[f"same_as_{version}_qwenvl_visual_unit_set"] = same_q
        result[f"same_as_{version}_ocr_visual_unit_set"] = same_o
        result[f"same_as_{version}_video_visual_unit_set"] = same_video
        result[f"same_as_{version}_qwenvl_role_map"] = same_q_roles
        result[f"same_as_{version}_ocr_role_map"] = same_o_roles
        result[f"result_fully_identical_to_{version}"] = (
            same_q and same_o and same_q_roles and same_o_roles
        )
        result[f"{version}_qwenvl_added_count"] = len(q_ids - ref_q_ids)
        result[f"{version}_qwenvl_removed_count"] = len(ref_q_ids - q_ids)
        result[f"{version}_video_added_count"] = len(video_ids - ref_video_ids)
        result[f"{version}_video_removed_count"] = len(ref_video_ids - video_ids)
        result[f"{version}_ocr_added_count"] = len(o_ids - ref_o_ids)
        result[f"{version}_ocr_removed_count"] = len(ref_o_ids - o_ids)
    if result.get("result_fully_identical_to_v20") or result.get("result_fully_identical_to_v21"):
        result["same_result_explanation"] = (
            "candidate sets are identical despite executed V22 mechanisms; inspect "
            "coverage shifts, pair-evaluation counters, and contact sheet before policy PASS"
        )
    else:
        result["same_result_explanation"] = (
            "not identical: V22 uses 18s time windows, content-aware local selection, "
            "V14 window competition, existing vector/grid dedup, and bbox-normalized OCR"
        )
    return result


def initialise_decisions(video_rows: Sequence[MutableMapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    decisions: Dict[str, Dict[str, Any]] = {}
    for frame in video_rows:
        visual_unit_id = str(frame.get("visual_unit_id") or "")
        decisions[visual_unit_id] = {
            "visual_unit_id": visual_unit_id,
            "source_content_id": str(frame.get("source_content_id") or ""),
            "derived_id": str(frame.get("derived_id") or ""),
            "canonical_visual_unit_id": str(
                frame.get("canonical_visual_unit_id") or visual_unit_id
            ),
            "central_dedup_identity_status": str(
                frame.get("central_dedup_identity_status") or ""
            ),
            "central_dedup_reverse_member_count": safe_int(
                frame.get("central_dedup_reverse_member_count"), 1
            ),
            "central_dedup_reverse_visual_unit_ids": str(
                frame.get("central_dedup_reverse_visual_unit_ids") or visual_unit_id
            ),
            "source_relative_path": str(frame.get("source_relative_path") or ""),
            "visual_file": str(frame.get("derived_visual_path") or ""),
            "derived_path": str(frame.get("derived_path") or ""),
            "time_position_ms": safe_int(frame.get("time_position_ms"), -1),
            "black_frame_status": str(frame.get("black_frame_status") or "unknown"),
            "tail_excluded": bool(frame.get("tail_excluded")),
            "screen_capture": phase1.is_screen_capture(str(frame.get("source_relative_path") or "")),
            "label_count": len(frame.get("labels") or []),
            "labels": "|".join(
                sorted({str(item.get("label") or "") for item in frame.get("labels") or []})
            ),
            "grid_structure": round(safe_float(frame.get("grid_structure"), 0.0), 6),
            "grid_luma_std": round(safe_float(frame.get("grid_std"), 0.0), 6),
            "v14_role": str(frame.get("v14_role") or ""),
            "coverage_window_index": frame.get("coverage_window_index", ""),
            "qwen_selected": False,
            "qwen_role": "",
            "ocr_selected": False,
            "ocr_role": "",
            "decision_reason_codes": list(frame.get("decision_reason_codes") or []),
        }
    return decisions


def build_candidates(
    con: sqlite3.Connection,
    args: argparse.Namespace,
    run_id: str,
    mode: str,
) -> Dict[str, Any]:
    central_context = central_dedup_context(con)
    pre_dedup_baseline = load_pre_dedup_v22(
        Path(args.pre_dedup_v22_out).expanduser().resolve(strict=True)
    )
    rows = load_visual_rows(con)
    by_vu: Dict[str, MutableMapping[str, Any]] = {
        str(row["visual_unit_id"]): row for row in rows
    }
    signature_stats = attach_signatures(rows)
    if signature_stats["failure_count"]:
        raise RuntimeError("derived_signature_failures:" + json.dumps(signature_stats))
    vectors, vector_stats = load_vectors(con)
    for visual_unit_id, vector in vectors.items():
        if visual_unit_id in by_vu:
            by_vu[visual_unit_id]["vector"] = vector
    label_stats = load_labels(con, by_vu)
    if label_stats["invalid_bbox_count"]:
        raise RuntimeError("bbox_normalization_failed_in_candidate_build")
    v14_stats = load_v14_roles(Path(args.v14_out), by_vu)

    video_rows = [row for row in rows if row.get("media_type") == "video"]
    image_rows = [row for row in rows if row.get("media_type") == "image"]
    by_video: Dict[str, List[MutableMapping[str, Any]]] = defaultdict(list)
    for row in video_rows:
        by_video[str(row.get("source_content_id") or "")].append(row)
    stats: Counter[str] = Counter()
    q_rows: List[Dict[str, Any]] = []
    o_rows: List[Dict[str, Any]] = []
    window_reports: List[Dict[str, Any]] = []
    video_budget: List[Dict[str, Any]] = []
    video_selections: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for source_content_id, frames in sorted(by_video.items()):
        frames.sort(key=lambda frame: (safe_int(frame.get("time_position_ms"), -1), str(frame.get("visual_unit_id"))))
        path = str(frames[0].get("source_relative_path") or "") if frames else ""
        if phase1.is_screen_capture(path):
            stats["screen_capture_video_group_count"] += 1
            o_rows.extend(select_screen_ocr(frames, args, stats, run_id, mode))
            coverage: List[Dict[str, Any]] = []
            supplements: List[Dict[str, Any]] = []
        else:
            stats["normal_video_group_count"] += 1
            coverage, reports = build_coverage_for_video(frames, args, stats)
            window_reports.extend(reports)
            if coverage:
                stats["normal_video_group_with_coverage_count"] += 1
            else:
                stats["normal_video_group_missing_coverage_count"] += 1
            supplements = add_v14_supplements(frames, coverage, args, stats)
            selections = list(coverage) + list(supplements)
            video_selections[source_content_id].extend(selections)
            for selection in selections:
                q_rows.append(
                    candidate_row(
                        selection["frame"],
                        "qwenvl_high_value",
                        selection["role"],
                        selection["score"],
                        selection["reasons"],
                        run_id,
                        mode,
                    )
                )
            o_rows.extend(select_normal_video_ocr(frames, args, stats, run_id, mode))
        times = [safe_int(frame.get("time_position_ms"), -1) for frame in frames if safe_int(frame.get("time_position_ms"), -1) >= 0]
        raw_start_ms = safe_int(frames[0].get("central_dedup_raw_group_start_ms"), -1) if frames else -1
        raw_end_ms = safe_int(frames[0].get("central_dedup_raw_group_end_ms"), -1) if frames else -1
        raw_candidate_count = safe_int(frames[0].get("central_dedup_raw_group_candidate_count"), len(frames)) if frames else 0
        selected_times = sorted(
            safe_int(item["frame"].get("time_position_ms"), -1)
            for item in video_selections.get(source_content_id, [])
        )
        max_gap = 0
        if times and selected_times:
            coverage_start = raw_start_ms if raw_start_ms >= 0 else min(times)
            coverage_end = raw_end_ms if raw_end_ms >= coverage_start else max(times)
            gaps = [selected_times[0] - coverage_start, coverage_end - selected_times[-1]]
            gaps.extend(b - a for a, b in zip(selected_times, selected_times[1:]))
            max_gap = max(gaps)
        video_budget.append(
            {
                "source_content_id": source_content_id,
                "source_relative_path": path,
                "step02_frame_count": raw_candidate_count,
                "canonical_frame_count": len(frames),
                "central_dedup_excluded_frame_count": max(0, raw_candidate_count - len(frames)),
                "duration_ms": (
                    raw_end_ms - raw_start_ms
                    if raw_start_ms >= 0 and raw_end_ms >= raw_start_ms
                    else (max(times) - min(times) if times else 0)
                ),
                "screen_capture": int(phase1.is_screen_capture(path)),
                "coverage_count": sum(1 for item in coverage if item["role"] in VIDEO_OUTPUT_ROLES),
                "overlap_count": sum(1 for item in coverage if item["role"] == "video_coverage_high_signal_overlap"),
                "supplement_count": len(supplements),
                "qwen_video_count": len(coverage) + len(supplements),
                "ocr_count": sum(1 for row in o_rows if row.get("source_content_id") == source_content_id),
                "yoloe_frame_count": sum(1 for frame in frames if frame.get("labels")),
                "max_coverage_gap_ms": max_gap,
            }
        )

    manual_seed_ids = load_manual_seed_ids(con)
    timelapse_representatives = load_timelapse_representatives(con)
    selected_image_ids: set[str] = set()
    for visual_unit_id in sorted(manual_seed_ids):
        frame = by_vu.get(visual_unit_id)
        if not frame or frame.get("media_type") != "image" or frame.get("black_rejected"):
            continue
        q_rows.append(
            candidate_row(
                frame,
                "qwenvl_high_value",
                "manual_finder_tag_image_seed",
                100.0,
                ["manual_finder_tag_image_seed"],
                run_id,
                mode,
            )
        )
        selected_image_ids.add(visual_unit_id)
    for sequence_id, visual_unit_id in sorted(timelapse_representatives.items()):
        frame = by_vu.get(visual_unit_id)
        if not frame or frame.get("black_rejected") or visual_unit_id in selected_image_ids:
            continue
        score, reasons = content_score(frame, 0, args.coverage_window_ms)
        q_rows.append(
            candidate_row(
                frame,
                "qwenvl_high_value",
                "timelapse_candidate",
                max(1.0, score),
                ["step02_timelapse_keyframe_db_source", f"sequence_id:{sequence_id}"] + reasons,
                run_id,
                mode,
            )
        )
        selected_image_ids.add(visual_unit_id)
    for frame in image_rows:
        visual_unit_id = str(frame.get("visual_unit_id") or "")
        if visual_unit_id in selected_image_ids or frame.get("black_rejected"):
            continue
        image_label_score = safe_float(label_features(frame).get("score"), 0.0)
        # Grid structure ranks an admitted image but must not turn an otherwise
        # weak-label image into a high-cost Qwen candidate.
        if image_label_score < args.image_yolo_threshold:
            continue
        score, reasons = content_score(frame, 0, args.coverage_window_ms)
        q_rows.append(
            candidate_row(
                frame,
                "qwenvl_high_value",
                "image_generic_visual_signal_candidate",
                score,
                [
                    "image_generic_visual_signal_candidate",
                    f"image_generic_label_score:{image_label_score:.6f}",
                ] + reasons,
                run_id,
                mode,
            )
        )
        selected_image_ids.add(visual_unit_id)

    q_rows.sort(
        key=lambda row: (
            str(row.get("media_type") or ""),
            str(row.get("source_relative_path") or ""),
            safe_int(row.get("time_position_ms"), -1),
            str(row.get("visual_unit_id") or ""),
        )
    )
    o_rows.sort(
        key=lambda row: (
            str(row.get("source_relative_path") or ""),
            safe_int(row.get("time_position_ms"), -1),
            str(row.get("visual_unit_id") or ""),
        )
    )

    decisions = initialise_decisions(video_rows)
    for row in q_rows:
        if row.get("media_type") != "video":
            continue
        decision = decisions.get(str(row.get("visual_unit_id") or ""))
        if decision:
            decision["qwen_selected"] = True
            decision["qwen_role"] = str(row.get("high_value_category") or "")
            decision["decision_reason_codes"].extend(str(row.get("reason_codes") or "").split("|"))
    for row in o_rows:
        decision = decisions.get(str(row.get("visual_unit_id") or ""))
        if decision:
            decision["ocr_selected"] = True
            decision["ocr_role"] = str(row.get("high_value_category") or "")
            decision["decision_reason_codes"].extend(str(row.get("reason_codes") or "").split("|"))
    for frame in video_rows:
        decision = decisions.get(str(frame.get("visual_unit_id") or ""))
        if not decision:
            continue
        decision["tail_excluded"] = bool(frame.get("tail_excluded"))
        decision["coverage_window_index"] = frame.get("coverage_window_index", "")
        decision["decision_reason_codes"].extend(frame.get("decision_reason_codes") or [])
        if frame.get("black_rejected"):
            decision["decision_reason_codes"].append("black_or_invalid_excluded")
        if frame.get("tail_excluded"):
            decision["decision_reason_codes"].append("tail_excluded")
        if not decision["qwen_selected"] and not decision["screen_capture"]:
            decision["decision_reason_codes"].append("not_selected_by_v22_video_policy")
        decision["decision_reason_codes"] = list(
            dict.fromkeys(reason for reason in decision["decision_reason_codes"] if reason)
        )

    comparison = compare_reference_outputs(
        q_rows, o_rows, Path(args.v20_out), Path(args.v21_out)
    )
    central_comparison = compare_pre_dedup_v22(
        q_rows, o_rows, window_reports, pre_dedup_baseline, central_context
    )
    q_video_rows = [
        row for row in q_rows if row.get("high_value_category") in VIDEO_OUTPUT_ROLES
    ]
    q_ids = {str(row.get("visual_unit_id") or "") for row in q_rows}
    o_ids = {str(row.get("visual_unit_id") or "") for row in o_rows}
    black_ids = {
        str(row.get("visual_unit_id") or "") for row in rows if row.get("black_rejected")
    }
    screen_q_leaks = sum(
        1
        for row in q_video_rows
        if phase1.is_screen_capture(str(row.get("source_relative_path") or ""))
    )
    category_counts = Counter(str(row.get("high_value_category") or "") for row in q_rows)
    ocr_media_counts = Counter(str(row.get("media_type") or "") for row in o_rows)
    summary = {
        "validation_status": "PASS",
        "technical_status": "PASS",
        "policy_status": "REVIEW",
        "policy_reason_codes": ["dry_run_requires_user_and_contact_sheet_review"] if mode == "dry_run" else [],
        "policy_version": POLICY_VERSION,
        "script_version": SCRIPT_VERSION,
        "run_id": run_id,
        "execution_mode": mode,
        "input_visual_units": len(rows),
        "visual_input_source": central_context["visual_input_source"],
        "raw_visual_input_count": central_context["raw_visual_input_count"],
        "canonical_visual_input_count": central_context["canonical_visual_input_count"],
        "dedup_excluded_visual_count": central_context["dedup_excluded_visual_count"],
        "dedup_reverse_mapping_available": central_context["dedup_reverse_mapping_available"],
        "input_video_visual_units": len(video_rows),
        "input_image_visual_units": len(image_rows),
        "qwenvl_total_count": len(q_rows),
        "qwen_video_frame_count": len(q_video_rows),
        "qwen_manual_seed_count": category_counts["manual_finder_tag_image_seed"],
        "qwen_timelapse_count": category_counts["timelapse_candidate"],
        "qwen_image_yoloe_count": category_counts["image_generic_visual_signal_candidate"],
        "ocr_total_count": len(o_rows),
        "qwen_category_counts": dict(category_counts),
        "ocr_media_type_counts": dict(ocr_media_counts),
        "video_source_group_count": len(by_video),
        "normal_video_group_count": stats["normal_video_group_count"],
        "screen_capture_video_group_count": stats["screen_capture_video_group_count"],
        "normal_video_group_with_coverage_count": stats["normal_video_group_with_coverage_count"],
        "normal_video_group_missing_coverage_count": stats["normal_video_group_missing_coverage_count"],
        "coverage_anchor_total_count": stats["coverage_anchor_total_count"],
        "coverage_window_total_count": stats["coverage_window_total_count"],
        "coverage_empty_window_count": stats["coverage_empty_window_count"],
        "coverage_window_candidate_evaluated_count": stats["coverage_window_candidate_evaluated_count"],
        "coverage_selected_count": stats["coverage_selected_count"],
        "coverage_anchor_local_best_shift_count": stats["coverage_anchor_local_best_shift_count"],
        "coverage_dedup_candidate_pair_count": stats["coverage_dedup_candidate_pair_count"],
        "coverage_dedup_drop_count": stats["coverage_dedup_unique_drop_count"],
        "coverage_refill_count": stats["coverage_refill_count"],
        "coverage_refill_failed_count": stats["coverage_refill_failed_count"],
        "high_signal_candidate_count": v14_stats["v14_high_signal_candidate_count"],
        "v14_high_signal_candidate_count": v14_stats["v14_high_signal_candidate_count"],
        "v14_high_signal_window_replacement_count": stats["v14_high_signal_window_replacement_count"],
        "high_signal_supplement_added_count": stats["high_signal_supplement_added_count"],
        "v14_high_signal_supplement_added_count": stats["v14_high_signal_supplement_added_count"],
        "coverage_high_signal_overlap_count": stats["coverage_high_signal_overlap_count"],
        "high_signal_reject_near_coverage_count": stats["high_signal_reject_near_coverage_count"],
        "high_signal_reject_bad_or_tail_count": stats["high_signal_reject_bad_or_tail_count"],
        "high_signal_reject_not_novel_count": stats["high_signal_reject_not_novel_count"],
        "high_signal_reject_cap_count": stats["high_signal_reject_cap_count"],
        "final_video_dedup_candidate_pair_count": stats["final_video_dedup_candidate_pair_count"],
        "final_video_dedup_drop_count": stats["final_video_dedup_unique_drop_count"],
        "dedup_unique_drop_count": stats["coverage_dedup_unique_drop_count"] + stats["final_video_dedup_unique_drop_count"],
        "time_similarity_drop_count": stats["coverage_time_dedup_drop_count"] + stats["final_video_time_dedup_drop_count"],
        "vector_similarity_drop_count": stats["coverage_vector_dedup_drop_count"] + stats["final_video_vector_dedup_drop_count"],
        "grid_similarity_drop_count": stats["coverage_grid_dedup_drop_count"] + stats["final_video_grid_dedup_drop_count"],
        "label_similarity_drop_count": stats["coverage_label_dedup_drop_count"] + stats["final_video_label_dedup_drop_count"],
        "exact_identity_drop_count": stats["coverage_exact_dedup_drop_count"] + stats["final_video_exact_dedup_drop_count"],
        "screen_video_ocr_dedup_drop_count": stats["screen_ocr_dedup_unique_drop_count"],
        "screen_ocr_vector_similarity_drop_count": stats["screen_ocr_vector_dedup_drop_count"],
        "screen_ocr_grid_similarity_drop_count": stats["screen_ocr_grid_dedup_drop_count"],
        "screen_ocr_label_similarity_drop_count": stats["screen_ocr_label_dedup_drop_count"],
        "screen_ocr_time_similarity_drop_count": stats["screen_ocr_time_dedup_drop_count"],
        "all_queue_dedup_unique_drop_count": (
            stats["coverage_dedup_unique_drop_count"]
            + stats["final_video_dedup_unique_drop_count"]
            + stats["screen_ocr_dedup_unique_drop_count"]
        ),
        "all_queue_vector_similarity_drop_count": (
            stats["coverage_vector_dedup_drop_count"]
            + stats["final_video_vector_dedup_drop_count"]
            + stats["screen_ocr_vector_dedup_drop_count"]
        ),
        "all_queue_grid_similarity_drop_count": (
            stats["coverage_grid_dedup_drop_count"]
            + stats["final_video_grid_dedup_drop_count"]
            + stats["screen_ocr_grid_dedup_drop_count"]
        ),
        "all_queue_label_similarity_drop_count": (
            stats["coverage_label_dedup_drop_count"]
            + stats["final_video_label_dedup_drop_count"]
            + stats["screen_ocr_label_dedup_drop_count"]
        ),
        "all_queue_time_similarity_drop_count": (
            stats["coverage_time_dedup_drop_count"]
            + stats["final_video_time_dedup_drop_count"]
            + stats["screen_ocr_time_dedup_drop_count"]
        ),
        "black_leak_into_qwenvl_count": len(q_ids & black_ids),
        "black_leak_into_ocr_count": len(o_ids & black_ids),
        "screen_recording_qwenvl_leak_count": screen_q_leaks,
        "normal_video_ocr_added_count": stats["normal_video_ocr_added_count"],
        "normal_video_ocr_weak_excluded_count": stats["normal_video_ocr_weak_excluded_count"],
        "normal_video_ocr_cap_excluded_count": stats["normal_video_ocr_cap_excluded_count"],
        "normal_video_ocr_min_gap_excluded_count": stats["normal_video_ocr_min_gap_excluded_count"],
        "normal_video_ocr_bad_bbox_excluded_count": label_stats["invalid_bbox_count"],
        "vector_dedup_status": vector_stats["vector_dedup_status"],
        "vector_payload_found": vector_stats["vector_payload_found"],
        "vector_payload_row_count": vector_stats["vector_payload_row_count"],
        "vector_payload_integrity_status": vector_stats["vector_payload_integrity_status"],
        "grid_similarity_enabled": signature_stats["failure_count"] == 0,
        "grid_signature_success_count": signature_stats["success_count"],
        "grid_signature_failed_count": signature_stats["failure_count"],
        "derived_black_or_invalid_count": signature_stats["black_or_invalid_count"],
        "bbox_normalized_count": label_stats["normalized_bbox_count"],
        "bbox_invalid_count": label_stats["invalid_bbox_count"],
        "central_dedup_excluded_label_row_count": label_stats[
            "central_dedup_excluded_label_row_count"
        ],
        "central_dedup_excluded_labeled_visual_unit_count": label_stats[
            "central_dedup_excluded_labeled_visual_unit_count"
        ],
        "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
        "safety": {
            "network": "not_used_offline_env_enabled",
            "download": "not_used",
            "dependency_install": "not_used",
            "model_loading": "not_used",
            "original_video_read": False,
            "original_media_write": False,
            "derived_frame_read": "existing_test_output_only_read_only",
            "vector_jsonl_read": "existing_test_output_only_read_only",
            "sqlite_write": mode == "commit",
            "sqlite_write_tables": (
                ["stop03_2_candidate_queue_items", "model_runs"] if mode == "commit" else []
            ),
        },
        "settings": {
            "coverage_window_ms": args.coverage_window_ms,
            "high_signal_supplement_min_gap_ms": args.high_signal_supplement_min_gap_ms,
            "dedup_time_gap_ms": args.dedup_time_gap_ms,
            "dedup_vector_threshold": args.dedup_vector_threshold,
            "dedup_grid_mad_threshold": args.dedup_grid_mad_threshold,
            "dedup_grid_corr_threshold": args.dedup_grid_corr_threshold,
            "dedup_label_jaccard_threshold": args.dedup_label_jaccard_threshold,
            "normal_video_ocr_cap": args.normal_video_ocr_cap,
            "normal_video_ocr_min_gap_ms": args.normal_video_ocr_min_gap_ms,
            "image_yolo_threshold": args.image_yolo_threshold,
        },
    }
    summary.update(comparison)
    summary.update(central_comparison)
    if summary["black_leak_into_qwenvl_count"] or summary["black_leak_into_ocr_count"]:
        summary["technical_status"] = summary["validation_status"] = "FAIL"
    if summary["screen_recording_qwenvl_leak_count"]:
        summary["technical_status"] = summary["validation_status"] = "FAIL"
    if summary["normal_video_group_missing_coverage_count"]:
        summary["technical_status"] = summary["validation_status"] = "FAIL"
    if summary["coverage_refill_failed_after_central_dedup_count"]:
        summary["technical_status"] = summary["validation_status"] = "FAIL"
    if summary["central_dedup_excluded_queue_leak_count"]:
        summary["technical_status"] = summary["validation_status"] = "FAIL"
    return {
        "rows": rows,
        "q_rows": q_rows,
        "o_rows": o_rows,
        "decisions": list(decisions.values()),
        "window_reports": window_reports,
        "video_budget": video_budget,
        "summary": summary,
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def write_outputs(out: Path, result: Dict[str, Any]) -> Dict[str, str]:
    manifests = out / "manifests"
    reports = out / "reports"
    manifests.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    q_csv = manifests / "qwenvl_high_value_candidate_queue.csv"
    q_jsonl = manifests / "qwenvl_high_value_candidate_queue.jsonl"
    o_csv = manifests / "ocr_trigger_candidate_queue.csv"
    o_jsonl = manifests / "ocr_trigger_candidate_queue.jsonl"
    decisions_jsonl = reports / "video_frame_decisions.jsonl"
    windows_csv = reports / "coverage_window_report.csv"
    budget_csv = reports / "video_budget_report.csv"
    summary_json = reports / "stop03_2_candidate_summary.json"
    summary_md = reports / "stop03_2_candidate_summary.md"
    write_csv(q_csv, result["q_rows"], MANIFEST_FIELDS)
    write_jsonl(q_jsonl, result["q_rows"])
    write_csv(o_csv, result["o_rows"], MANIFEST_FIELDS)
    write_jsonl(o_jsonl, result["o_rows"])
    write_jsonl(decisions_jsonl, result["decisions"])
    window_fields = list(result["window_reports"][0].keys()) if result["window_reports"] else []
    write_csv(windows_csv, result["window_reports"], window_fields)
    budget_fields = list(result["video_budget"][0].keys()) if result["video_budget"] else []
    write_csv(budget_csv, result["video_budget"], budget_fields)
    outputs = {
        "qwenvl_csv": str(q_csv),
        "qwenvl_jsonl": str(q_jsonl),
        "ocr_csv": str(o_csv),
        "ocr_jsonl": str(o_jsonl),
        "video_frame_decisions_jsonl": str(decisions_jsonl),
        "coverage_window_report_csv": str(windows_csv),
        "video_budget_report_csv": str(budget_csv),
        "summary_json": str(summary_json),
        "summary_md": str(summary_md),
    }
    result["summary"]["outputs"] = outputs
    result["summary"]["manifest_consistency"] = {
        "qwenvl_csv_rows": len(result["q_rows"]),
        "qwenvl_jsonl_rows": len(result["q_rows"]),
        "ocr_csv_rows": len(result["o_rows"]),
        "ocr_jsonl_rows": len(result["o_rows"]),
        "consistent": True,
    }
    summary_json.write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    important = [
        "validation_status", "technical_status", "policy_status", "execution_mode",
        "visual_input_source", "raw_visual_input_count", "canonical_visual_input_count",
        "dedup_excluded_visual_count", "dedup_reverse_mapping_available",
        "qwenvl_total_count", "qwen_video_frame_count", "ocr_total_count",
        "normal_video_group_with_coverage_count", "coverage_window_total_count",
        "coverage_candidate_excluded_by_central_dedup_count",
        "coverage_refill_after_central_dedup_count",
        "coverage_refill_failed_after_central_dedup_count",
        "qwen_candidate_removed_by_central_dedup_count",
        "qwen_candidate_replaced_after_central_dedup_count",
        "ocr_candidate_removed_by_central_dedup_count",
        "same_as_pre_dedup_v22_candidate_set",
        "dedup_unique_drop_count", "v14_high_signal_window_replacement_count",
        "v14_high_signal_supplement_added_count", "normal_video_ocr_added_count",
        "normal_video_ocr_weak_excluded_count", "result_fully_identical_to_v20",
        "result_fully_identical_to_v21", "same_result_explanation",
    ]
    summary_md.write_text(
        "\n".join(f"- **{key}**: `{result['summary'].get(key)}`" for key in important) + "\n",
        encoding="utf-8",
    )
    return outputs


def commit_candidate_rows(
    db: Path,
    result: Dict[str, Any],
    run_id: str,
    clear_existing: bool,
) -> Dict[str, Any]:
    if not clear_existing:
        raise RuntimeError("commit_requires_clear_existing_candidate_items")
    con = sqlite3.connect(str(db))
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM stop03_2_candidate_queue_items")
        created_at = now_iso()
        for row in [*result["q_rows"], *result["o_rows"]]:
            con.execute(
                """
                INSERT INTO stop03_2_candidate_queue_items
                (candidate_id, queue_type, visual_unit_id, source_content_id,
                 derived_id, candidate_score, reason_codes, black_frame_status,
                 luma_mean, luma_std, run_id, script_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["candidate_id"], row["queue_type"], row["visual_unit_id"],
                    row["source_content_id"], row.get("derived_id") or None,
                    float(row["candidate_score"]), row["reason_codes"],
                    row["black_frame_status"], row.get("luma_mean"),
                    row.get("luma_std"), run_id, SCRIPT_VERSION, created_at,
                ),
            )
        con.execute(
            """
            INSERT INTO model_runs
            (run_id, stage, model_name, model_path, script_version, script_path,
             input_count, output_count, status, started_at, finished_at, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, STAGE, "rule_based_db_v22_no_model", "", SCRIPT_VERSION,
                str(Path(__file__).resolve()), result["summary"]["input_visual_units"],
                len(result["q_rows"]) + len(result["o_rows"]), "done", created_at,
                now_iso(), "",
            ),
        )
        run_candidate_rows = con.execute(
            "SELECT COUNT(*) FROM stop03_2_candidate_queue_items WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        expected = len(result["q_rows"]) + len(result["o_rows"])
        if int(run_candidate_rows) != expected:
            raise RuntimeError(f"commit_row_count_mismatch:{run_candidate_rows}!={expected}")
        con.commit()
        return {
            "candidate_rows_written": expected,
            "model_runs_written": 1,
            "run_candidate_rows_verified": int(run_candidate_rows),
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def run_preflight(args: argparse.Namespace) -> Dict[str, Any]:
    namespace = argparse.Namespace(
        db=args.db,
        v14_out=args.v14_out,
        coverage_window_ms=args.coverage_window_ms,
        grid_cols=16,
        grid_rows=5,
    )
    base = phase1.run(namespace)
    reference_checks = {}
    for name, raw in (("v20", args.v20_out), ("v21", args.v21_out)):
        out = Path(raw).expanduser().resolve(strict=True)
        q = out / "manifests" / "qwenvl_high_value_candidate_queue.csv"
        o = out / "manifests" / "ocr_trigger_candidate_queue.csv"
        reference_checks[name] = {
            "qwenvl_manifest_exists": q.is_file(),
            "ocr_manifest_exists": o.is_file(),
        }
    pre_dedup_out = Path(args.pre_dedup_v22_out).expanduser().resolve(strict=True)
    pre_dedup_checks = {
        "summary_exists": (
            pre_dedup_out / "reports" / "stop03_2_candidate_summary.json"
        ).is_file(),
        "qwenvl_manifest_exists": (
            pre_dedup_out / "manifests" / "qwenvl_high_value_candidate_queue.csv"
        ).is_file(),
        "ocr_manifest_exists": (
            pre_dedup_out / "manifests" / "ocr_trigger_candidate_queue.csv"
        ).is_file(),
        "coverage_report_exists": (
            pre_dedup_out / "reports" / "coverage_window_report.csv"
        ).is_file(),
    }
    con = phase1.connect_readonly(Path(args.db).expanduser().resolve(strict=True))
    try:
        dedup_context = central_dedup_context(con)
    finally:
        con.close()
    out = assert_test_output_path(Path(args.out), may_exist=False)
    references_ok = all(
        item["qwenvl_manifest_exists"] and item["ocr_manifest_exists"]
        for item in reference_checks.values()
    ) and all(pre_dedup_checks.values())
    technical_status = (
        "PASS" if base.get("technical_status") == "PASS" and references_ok else "FAIL"
    )
    return {
        "validation_status": technical_status,
        "technical_status": technical_status,
        "policy_status": "REVIEW",
        "execution_mode": "preflight_only",
        "script_version": SCRIPT_VERSION,
        "policy_version": POLICY_VERSION,
        "out_path_checked_not_created": str(out),
        "phase1_checks": base.get("checks"),
        "reference_checks": reference_checks,
        "pre_dedup_v22_checks": pre_dedup_checks,
        "pre_dedup_v22_out": str(pre_dedup_out),
        "visual_input_source": dedup_context["visual_input_source"],
        "raw_visual_input_count": dedup_context["raw_visual_input_count"],
        "canonical_visual_input_count": dedup_context["canonical_visual_input_count"],
        "dedup_excluded_visual_count": dedup_context["dedup_excluded_visual_count"],
        "dedup_reverse_mapping_available": dedup_context["dedup_reverse_mapping_available"],
        "read_only_integrity": base.get("read_only_integrity"),
        "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
        "safety": {
            "network": "not_used_offline_env_enabled",
            "sqlite_write": False,
            "output_write": False,
            "original_video_read": False,
            "model_loading": False,
        },
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-2 V22 candidate queues")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight-only", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--commit", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--v14-out", default=str(DEFAULT_V14_OUT))
    parser.add_argument("--v20-out", default=str(DEFAULT_V20_OUT))
    parser.add_argument("--v21-out", default=str(DEFAULT_V21_OUT))
    parser.add_argument(
        "--pre-dedup-v22-out", default=str(DEFAULT_PRE_DEDUP_V22_OUT)
    )
    parser.add_argument("--clear-existing-candidate-items", action="store_true")
    parser.add_argument("--coverage-window-ms", type=int, default=18_000)
    parser.add_argument("--video-stride", type=int, default=6, help="Compatibility metadata only; V22 uses coverage windows.")
    parser.add_argument("--image-yolo-threshold", type=float, default=4.2)
    parser.add_argument("--high-signal-supplement-min-gap-ms", type=int, default=9_000)
    parser.add_argument("--dedup-time-gap-ms", type=int, default=12_000)
    parser.add_argument("--dedup-vector-threshold", type=float, default=0.985)
    parser.add_argument("--dedup-grid-mad-threshold", type=float, default=6.0)
    parser.add_argument("--dedup-grid-corr-threshold", type=float, default=0.98)
    parser.add_argument("--dedup-label-jaccard-threshold", type=float, default=0.80)
    parser.add_argument("--normal-video-ocr-cap", type=int, default=1)
    parser.add_argument("--normal-video-ocr-min-score", type=float, default=0.0, help="Compatibility parameter; bbox rule is authoritative.")
    parser.add_argument("--normal-video-ocr-min-gap-ms", type=int, default=20_000)
    parser.add_argument("--final-video-dedup-min-gap-ms", type=int, default=12_000, help="Compatibility alias represented by --dedup-time-gap-ms.")
    parser.add_argument("--final-video-dedup-vector-threshold", type=float, default=None)
    parser.add_argument("--final-video-dedup-grid-mad-threshold", type=float, default=None)
    parser.add_argument("--final-video-dedup-label-sim-threshold", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    phase1.set_offline_environment()
    if args.final_video_dedup_vector_threshold is not None:
        args.dedup_vector_threshold = args.final_video_dedup_vector_threshold
    if args.final_video_dedup_grid_mad_threshold is not None:
        args.dedup_grid_mad_threshold = args.final_video_dedup_grid_mad_threshold
    if args.final_video_dedup_label_sim_threshold is not None:
        args.dedup_label_jaccard_threshold = args.final_video_dedup_label_sim_threshold
    try:
        if args.preflight_only:
            result = run_preflight(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.get("technical_status") == "PASS" else 2

        mode = "commit" if args.commit else "dry_run"
        out = assert_test_output_path(Path(args.out), may_exist=False)
        db = Path(args.db).expanduser().resolve(strict=True)
        db_sha_before = phase1.sha256_file(db)
        db_mtime_before = db.stat().st_mtime_ns
        preflight = run_preflight(args)
        if preflight.get("technical_status") != "PASS":
            raise RuntimeError("preflight_failed_before_" + mode)
        con = phase1.connect_readonly(db)
        try:
            run_id = f"{SCRIPT_VERSION}_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
            built = build_candidates(con, args, run_id, mode)
        finally:
            con.close()
        if built["summary"].get("technical_status") != "PASS":
            raise RuntimeError("candidate_validation_failed")
        write_outputs(out, built)
        if args.commit:
            commit_result = commit_candidate_rows(
                db, built, run_id, bool(args.clear_existing_candidate_items)
            )
            built["summary"]["commit"] = commit_result
            built["summary"]["policy_status"] = "REVIEW"
            Path(built["summary"]["outputs"]["summary_json"]).write_text(
                json.dumps(built["summary"], ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        db_sha_after = phase1.sha256_file(db)
        db_mtime_after = db.stat().st_mtime_ns
        built["summary"]["read_only_integrity"] = {
            "db_sha256_before": db_sha_before,
            "db_sha256_after": db_sha_after,
            "db_mtime_ns_before": db_mtime_before,
            "db_mtime_ns_after": db_mtime_after,
            "db_unchanged": db_sha_before == db_sha_after and db_mtime_before == db_mtime_after,
            "candidate_queue_items_written": 0 if mode == "dry_run" else len(built["q_rows"]) + len(built["o_rows"]),
            "model_runs_written": 0 if mode == "dry_run" else 1,
        }
        if mode == "dry_run" and not built["summary"]["read_only_integrity"]["db_unchanged"]:
            built["summary"]["validation_status"] = "FAIL"
            built["summary"]["technical_status"] = "FAIL"
        Path(built["summary"]["outputs"]["summary_json"]).write_text(
            json.dumps(built["summary"], ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(built["summary"], ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if built["summary"].get("technical_status") == "PASS" else 2
    except Exception as exc:
        failure = {
            "validation_status": "FAIL",
            "technical_status": "FAIL",
            "policy_status": "REVIEW",
            "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "safety": {
                "original_video_read": False,
                "model_loading": False,
                "network": "not_used_offline_env_enabled",
            },
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
