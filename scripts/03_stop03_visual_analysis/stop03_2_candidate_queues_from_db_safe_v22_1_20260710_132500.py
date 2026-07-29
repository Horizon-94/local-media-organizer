#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-2 V22.1 visual-quality refinement.

This module reuses the V22 loaders, OCR/image branches, manifests, and
transaction helper.  It replaces only normal-video coverage ranking, tail
protection, and the V14 supplement information-gain gate.  It never decodes
original video and never loads or runs a model.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageOps

import stop03_2_candidate_queues_from_db_safe_v22_0_20260710_112936 as v22
import stop03_2_v22_phase1_readonly_selfcheck_20260710_110619 as phase1


SCRIPT_VERSION = "stop03_2_candidate_queues_from_db_safe_v22_1_20260710_132500"
POLICY_VERSION = "stop03_2_generic_high_value_rules_dr_v18_v22_1_20260710"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V14_OUT = phase1.DEFAULT_V14_OUT
DEFAULT_V20_OUT = v22.DEFAULT_V20_OUT
DEFAULT_V21_OUT = v22.DEFAULT_V21_OUT
DEFAULT_V22_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v22_0_20260710_112936_dry_run_v2"
)
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-2-candidate-queues-db-safe-v22_1_dry_run"

ORIGINAL_ATTACH_SIGNATURES = v22.attach_signatures
ORIGINAL_BUILD_CANDIDATES = v22.build_candidates
ORIGINAL_RUN_PREFLIGHT = v22.run_preflight
ORIGINAL_WRITE_OUTPUTS = v22.write_outputs

PERSON_LABELS = {"person", "people", "human", "body", "crowd"}
FACE_LABELS = {"face"}
PARTIAL_BODY_LABELS = {"hand", "arm", "leg", "foot", "head", "torso"}
VEHICLE_LABEL_PARTS = {
    "car", "truck", "bus", "train", "motorcycle", "bicycle", "boat",
    "airplane", "vehicle",
}
MACHINE_LABEL_PARTS = {
    "machine", "tractor", "harvester", "tool", "camera", "microphone",
    "phone", "laptop", "computer", "tablet", "equipment", "device",
}
ANIMAL_LABEL_PARTS = {
    "dog", "cat", "cow", "sheep", "horse", "bird", "animal", "chicken",
}


def new_audit() -> Dict[str, Any]:
    return {
        "stats": Counter(),
        "human_replacements": [],
        "supplement_audit": [],
        "v22_window_map": {},
        "v22_q_rows": [],
        "frame_by_vu": {},
    }


AUDIT: Dict[str, Any] = new_audit()


def bind_run_stats(stats: Counter[str]) -> None:
    """Bind V22.1 audit counters to V22's per-build Counter exactly once."""
    current: Counter[str] = AUDIT["stats"]
    if current is stats:
        return
    for key, value in current.items():
        stats[key] += value
    AUDIT["stats"] = stats


def safe_int(value: Any, default: int = -1) -> int:
    return v22.safe_int(value, default)


def safe_float(value: Any, default: float = 0.0) -> float:
    return v22.safe_float(value, default)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def sharpness_sobel_energy(path: Path) -> float:
    with Image.open(path) as image:
        logical = ImageOps.exif_transpose(image).convert("L")
        logical.thumbnail((640, 640))
        gray = np.asarray(logical, dtype=np.float32)
    if gray.ndim != 2 or gray.shape[0] < 3 or gray.shape[1] < 3:
        raise ValueError("sharpness_image_too_small")
    gx = (
        -gray[:-2, :-2] + gray[:-2, 2:]
        - 2.0 * gray[1:-1, :-2] + 2.0 * gray[1:-1, 2:]
        - gray[2:, :-2] + gray[2:, 2:]
    )
    gy = (
        -gray[:-2, :-2] - 2.0 * gray[:-2, 1:-1] - gray[:-2, 2:]
        + gray[2:, :-2] + 2.0 * gray[2:, 1:-1] + gray[2:, 2:]
    )
    return float(np.mean(gx * gx + gy * gy))


def attach_signatures_v22_1(
    rows: Sequence[MutableMapping[str, Any]],
) -> Dict[str, Any]:
    result = ORIGINAL_ATTACH_SIGNATURES(rows)
    stats: Counter[str] = AUDIT["stats"]
    for row in rows:
        raw = str(row.get("derived_visual_path") or "")
        path, status = phase1.resolve_allowed_existing_file(raw, TEST_OUTPUT_ROOT)
        if status != "ok" or path is None:
            row["sharpness_score"] = None
            row["sharpness_error"] = status
            stats["sharpness_failed_count"] += 1
            continue
        try:
            value = sharpness_sobel_energy(path)
        except Exception as exc:
            row["sharpness_score"] = None
            row["sharpness_error"] = type(exc).__name__
            stats["sharpness_failed_count"] += 1
            continue
        row["sharpness_score"] = round(value, 6)
        row["sharpness_error"] = ""
        stats["sharpness_evaluated_count"] += 1
    return result


def edge_touch_count(bbox: Any, margin: float = 0.02) -> int:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return 0
    x1, y1, x2, y2 = (safe_float(value) for value in bbox)
    return sum((x1 <= margin, y1 <= margin, x2 >= 1.0 - margin, y2 >= 1.0 - margin))


def item_center_quality(item: Mapping[str, Any]) -> float:
    distance = item.get("center_distance")
    if distance is None:
        return 0.0
    return max(0.0, 1.0 - safe_float(distance) / 0.7072)


def best_bbox(items: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if not items:
        return None
    return max(
        items,
        key=lambda item: (
            min(1.0, safe_float(item.get("area")) / 0.18)
            + item_center_quality(item)
            - 0.4 * edge_touch_count(item.get("bbox")),
            safe_float(item.get("confidence")),
        ),
    )


def bbox_overlap_ratio(inner: Any, outer: Any) -> float:
    if not isinstance(inner, (tuple, list)) or not isinstance(outer, (tuple, list)):
        return 0.0
    if len(inner) != 4 or len(outer) != 4:
        return 0.0
    ix1, iy1, ix2, iy2 = (safe_float(value) for value in inner)
    ox1, oy1, ox2, oy2 = (safe_float(value) for value in outer)
    area = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if area <= 0:
        return 0.0
    intersection = max(0.0, min(ix2, ox2) - max(ix1, ox1)) * max(
        0.0, min(iy2, oy2) - max(iy1, oy1)
    )
    return intersection / area


def human_quality_components(frame: Mapping[str, Any]) -> Dict[str, Any]:
    labels = list(frame.get("labels") or [])
    persons = [item for item in labels if str(item.get("label") or "") in PERSON_LABELS]
    faces = [item for item in labels if str(item.get("label") or "") in FACE_LABELS]
    partials = [
        item for item in labels if str(item.get("label") or "") in PARTIAL_BODY_LABELS
    ]
    person = best_bbox(persons)
    face = best_bbox(faces)
    person_area = safe_float(person.get("area")) if person else 0.0
    face_area = safe_float(face.get("area")) if face else 0.0
    person_center = safe_float(person.get("center_distance"), 1.0) if person else None
    face_center = safe_float(face.get("center_distance"), 1.0) if face else None
    person_edges = edge_touch_count(person.get("bbox")) if person else 0
    face_edges = edge_touch_count(face.get("bbox")) if face else 0
    overlap = bool(
        person
        and face
        and bbox_overlap_ratio(face.get("bbox"), person.get("bbox")) >= 0.50
    )
    partial_only = bool(partials and not persons and not faces)
    person_crop_penalty = 0.75 * person_edges
    face_crop_penalty = 0.60 * face_edges
    score = 0.0
    if person:
        score += 2.0
        score += 2.0 * min(1.0, person_area / 0.16)
        score += 1.4 * item_center_quality(person)
        if person_area < 0.008:
            score -= 1.2
    if face:
        score += 1.0
        score += 1.2 * min(1.0, face_area / 0.035)
        score += 0.8 * item_center_quality(face)
        if face_area < 0.0015:
            score -= 0.7
    face_person_overlap_bonus = 2.2 if overlap else 0.0
    score += face_person_overlap_bonus
    score -= person_crop_penalty + face_crop_penalty
    if partial_only:
        score -= 2.0
    return {
        "human_present": bool(persons or faces or partials),
        "face_present": bool(faces),
        "person_bbox_area_ratio": round(person_area, 8),
        "face_bbox_area_ratio": round(face_area, 8),
        "person_center_distance": None if person_center is None else round(person_center, 8),
        "face_center_distance": None if face_center is None else round(face_center, 8),
        "person_edge_touch_count": person_edges,
        "face_edge_touch_count": face_edges,
        "person_crop_penalty": round(person_crop_penalty, 6),
        "face_crop_penalty": round(face_crop_penalty, 6),
        "sharpness_score": frame.get("sharpness_score"),
        "sharpness_relative_score": 0.0,
        "human_partial_only_penalty": 2.0 if partial_only else 0.0,
        "human_blur_penalty": 0.0,
        "face_person_overlap_bonus": face_person_overlap_bonus,
        "face_person_overlap": overlap,
        "complete_person": bool(person and person_edges == 0 and person_area >= 0.008),
        "human_quality_base": round(score, 6),
        "human_quality_score": round(score, 6),
    }


def apply_window_human_quality(
    candidates: Sequence[MutableMapping[str, Any]], stats: Counter[str]
) -> bool:
    available = [
        safe_float(frame.get("sharpness_score"))
        for frame in candidates
        if frame.get("sharpness_score") is not None
    ]
    ordered = sorted(available)
    median = statistics.median(available) if available else 0.0
    for frame in candidates:
        components = human_quality_components(frame)
        raw = frame.get("sharpness_score")
        if raw is None or not ordered:
            relative = 0.0
        elif len(ordered) == 1:
            relative = 1.0
        else:
            relative = sum(value <= safe_float(raw) for value in ordered) / len(ordered)
        blur_penalty = 0.0
        if len(ordered) >= 3 and relative <= 0.34 and safe_float(raw) < median * 0.65:
            blur_penalty = 1.4
        quality = (
            safe_float(components["human_quality_base"])
            + 1.5 * relative
            - blur_penalty
        )
        components["sharpness_relative_score"] = round(relative, 6)
        components["human_blur_penalty"] = blur_penalty
        components["human_quality_score"] = round(quality, 6)
        frame.update({
            "human_present": components["human_present"],
            "face_present": components["face_present"],
            "human_quality_score": components["human_quality_score"],
            "human_quality_components": components,
            "sharpness_relative_score": components["sharpness_relative_score"],
        })
    human_window = any(bool(frame.get("human_present")) for frame in candidates)
    if human_window:
        stats["human_window_count"] += 1
        stats["human_candidate_evaluated_count"] += len(candidates)
        for frame in candidates:
            components = frame.get("human_quality_components") or {}
            if safe_int(components.get("person_edge_touch_count"), 0) > 0:
                stats["human_edge_crop_penalty_count"] += 1
            if safe_int(components.get("face_edge_touch_count"), 0) > 0:
                stats["human_face_edge_crop_penalty_count"] += 1
            if safe_float(components.get("human_partial_only_penalty")) > 0:
                stats["human_partial_body_penalty_count"] += 1
            if safe_float(components.get("human_blur_penalty")) > 0:
                stats["human_blur_penalty_count"] += 1
    return human_window


def v22_1_tail_window(frames: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    times = sorted(
        safe_int(frame.get("time_position_ms"), -1)
        for frame in frames
        if safe_int(frame.get("time_position_ms"), -1) >= 0
    )
    if not times:
        return {"start_ms": None, "end_ms": None, "span_ms": 0, "window_ms": 0}
    span = times[-1] - times[0]
    if span <= 30_000:
        return {"start_ms": None, "end_ms": times[-1], "span_ms": span, "window_ms": 0}
    window = max(6_000, min(30_000, int(round(span * 0.05))))
    return {
        "start_ms": times[-1] - window,
        "end_ms": times[-1],
        "span_ms": span,
        "window_ms": window,
    }


def mark_tail_status(frames: Sequence[MutableMapping[str, Any]], stats: Counter[str]) -> Dict[str, Any]:
    policy = v22_1_tail_window(frames)
    tail_start = policy["start_ms"]
    end_ms = policy["end_ms"]
    for frame in frames:
        time_ms = safe_int(frame.get("time_position_ms"), -1)
        protected = bool(tail_start is not None and time_ms >= safe_int(tail_start))
        frame["tail_excluded"] = protected
        frame["tail_status"] = (
            "tail_protected" if protected else
            "short_video_no_tail_protection" if tail_start is None else "not_tail"
        )
        frame["tail_action"] = "rejected_tail_protected" if protected else "eligible"
        frame["is_last_frame"] = bool(end_ms is not None and time_ms == end_ms)
        if protected:
            stats["tail_candidate_rejected_count"] += 1
            frame.setdefault("rejection_reason_codes", []).append("tail_protected_rejected")
    return policy


def coverage_rank_key(
    frame: MutableMapping[str, Any], anchor_ms: int, window_ms: int, human_window: bool
) -> Tuple[Any, ...]:
    components = frame.get("human_quality_components") or {}
    high_signal, _ = v22.is_high_signal(frame)
    v14_signal = frame.get("v14_role") == "video_high_signal_keyframe"
    overlap = bool(components.get("face_person_overlap"))
    complete = bool(components.get("complete_person"))
    sharp_relative = safe_float(components.get("sharpness_relative_score"))
    edge_penalty = safe_int(components.get("person_edge_touch_count"), 0) + safe_int(
        components.get("face_edge_touch_count"), 0
    )
    partial = safe_float(components.get("human_partial_only_penalty")) > 0
    first_layer = (
        4.0 * int(overlap)
        + 3.0 * int(complete)
        + 2.0 * int(v14_signal)
        + 1.2 * int(high_signal)
        + 1.5 * sharp_relative
        - 1.1 * edge_penalty
        - 2.0 * int(partial)
    )
    if human_window and not frame.get("human_present"):
        first_layer -= 3.0
    content, _ = v22.content_score(frame, anchor_ms, window_ms)
    return (
        -first_layer,
        -safe_float(frame.get("human_quality_score")),
        -safe_float(v22.label_features(frame).get("score")),
        -content,
        -safe_float(frame.get("grid_structure")),
        abs(safe_int(frame.get("time_position_ms"), -1) - anchor_ms),
        str(frame.get("visual_unit_id") or ""),
    )


def record_human_replacement(
    source_id: str,
    source_path: str,
    window_index: int,
    selected: MutableMapping[str, Any],
    candidate_by_id: Mapping[str, MutableMapping[str, Any]],
) -> None:
    old_id = str(AUDIT["v22_window_map"].get((source_id, window_index), ""))
    new_id = str(selected.get("visual_unit_id") or "")
    old = candidate_by_id.get(old_id)
    if not old or old_id == new_id:
        return
    old_quality = safe_float(old.get("human_quality_score"))
    new_quality = safe_float(selected.get("human_quality_score"))
    if new_quality <= old_quality + 0.05:
        return
    reasons: List[str] = ["human_quality_higher_than_v22"]
    old_components = old.get("human_quality_components") or {}
    new_components = selected.get("human_quality_components") or {}
    if new_components.get("face_person_overlap") and not old_components.get("face_person_overlap"):
        reasons.append("face_person_overlap_preferred")
    if safe_int(new_components.get("person_edge_touch_count"), 0) < safe_int(
        old_components.get("person_edge_touch_count"), 0
    ):
        reasons.append("person_edge_crop_reduced")
    if safe_float(new_components.get("sharpness_relative_score")) > safe_float(
        old_components.get("sharpness_relative_score")
    ):
        reasons.append("window_relative_sharpness_improved")
    AUDIT["human_replacements"].append(
        {
            "source_content_id": source_id,
            "source_relative_path": source_path,
            "window_index": window_index,
            "old_visual_unit_id": old_id,
            "new_visual_unit_id": new_id,
            "old_time_position_ms": safe_int(old.get("time_position_ms"), -1),
            "new_time_position_ms": safe_int(selected.get("time_position_ms"), -1),
            "old_human_quality": round(old_quality, 6),
            "new_human_quality": round(new_quality, 6),
            "replacement_reason_codes": reasons,
        }
    )


def build_coverage_for_video_v22_1(
    frames: Sequence[MutableMapping[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    bind_run_stats(stats)
    timed = sorted(
        [frame for frame in frames if safe_int(frame.get("time_position_ms"), -1) >= 0],
        key=lambda frame: (safe_int(frame.get("time_position_ms"), -1), str(frame.get("visual_unit_id"))),
    )
    if not timed:
        return [], []
    tail_policy = mark_tail_status(timed, stats)
    start_ms = safe_int(timed[0].get("time_position_ms"), 0)
    end_ms = safe_int(timed[-1].get("time_position_ms"), start_ms)
    span = max(1, end_ms - start_ms + 1)
    planned_windows = max(1, (span + args.coverage_window_ms - 1) // args.coverage_window_ms)
    all_window_frames: Dict[int, List[MutableMapping[str, Any]]] = defaultdict(list)
    for frame in timed:
        index = min(
            planned_windows - 1,
            max(0, (safe_int(frame.get("time_position_ms"), start_ms) - start_ms) // args.coverage_window_ms),
        )
        frame["coverage_window_index"] = int(index)
        all_window_frames[int(index)].append(frame)
    stats["coverage_window_total_count"] += planned_windows
    stats["coverage_anchor_total_count"] += planned_windows
    stats["coverage_window_candidate_evaluated_count"] += sum(
        len(items) for items in all_window_frames.values()
    )

    plans: List[Dict[str, Any]] = []
    tail_only_indices: List[int] = []
    for index in range(planned_windows):
        all_candidates = all_window_frames.get(index, [])
        candidates = [
            frame for frame in all_candidates
            if not frame.get("black_rejected") and not frame.get("tail_excluded")
        ]
        window_start = start_ms + index * args.coverage_window_ms
        window_end = min(end_ms + 1, window_start + args.coverage_window_ms)
        anchor_ms = window_start + max(0, window_end - window_start - 1) // 2
        if not candidates:
            stats["coverage_empty_window_count"] += 1
            if all_candidates and all(frame.get("tail_excluded") for frame in all_candidates):
                tail_only_indices.append(index)
            continue
        human_window = apply_window_human_quality(candidates, stats)
        anchor = min(
            candidates,
            key=lambda frame: (
                abs(safe_int(frame.get("time_position_ms"), -1) - anchor_ms),
                str(frame.get("visual_unit_id")),
            ),
        )
        ranked_frames = sorted(
            candidates,
            key=lambda frame: coverage_rank_key(frame, anchor_ms, args.coverage_window_ms, human_window),
        )
        ranked: List[Tuple[float, MutableMapping[str, Any], List[str]]] = []
        for rank, frame in enumerate(ranked_frames, start=1):
            content, reasons = v22.content_score(frame, anchor_ms, args.coverage_window_ms)
            combined = content + safe_float(frame.get("human_quality_score"))
            frame["window_rank"] = rank
            frame.setdefault("selection_reason_codes", [])
            if rank > 1:
                frame.setdefault("rejection_reason_codes", []).append("window_lower_quality_rank")
            ranked.append((combined, frame, reasons))
        plans.append(
            {
                "window_index": index,
                "window_start_ms": window_start,
                "window_end_exclusive_ms": window_end,
                "anchor_ms": anchor_ms,
                "anchor_visual_unit_id": str(anchor.get("visual_unit_id")),
                "human_window": human_window,
                "ranked": ranked,
            }
        )

    selected: List[Dict[str, Any]] = []
    reports: List[Dict[str, Any]] = []
    plan_by_index = {safe_int(plan["window_index"]): plan for plan in plans}
    source_id = str(timed[0].get("source_content_id") or "")
    source_path = str(timed[0].get("source_relative_path") or "")
    for plan in plans:
        selected_score, selected_frame, selected_reasons = plan["ranked"][0]
        top_frame = selected_frame
        duplicate_evidence_top: Optional[Dict[str, Any]] = None
        for previous in selected:
            evidence = v22.duplicate_evidence(selected_frame, previous["frame"], args)
            stats["coverage_dedup_candidate_pair_count"] += 1
            if evidence["duplicate"]:
                duplicate_evidence_top = evidence
                break
        if duplicate_evidence_top is not None:
            replacement = None
            for score, candidate, reasons in plan["ranked"][1:]:
                if all(
                    not v22.duplicate_evidence(candidate, previous["frame"], args)["duplicate"]
                    for previous in selected
                ):
                    replacement = (score, candidate, reasons)
                    break
            if replacement:
                v22.mark_dedup_drop(stats, duplicate_evidence_top, "coverage")
                stats["coverage_refill_count"] += 1
                top_frame.setdefault("rejection_reason_codes", []).append(
                    "coverage_dedup_dropped_and_refilled"
                )
                selected_score, selected_frame, selected_reasons = replacement
                selected_reasons = list(selected_reasons) + ["coverage_refill_after_dedup"]
            else:
                stats["coverage_refill_failed_count"] += 1
                selected_reasons = list(selected_reasons) + ["dedup_kept_for_coverage"]
        high_signal, high_reasons = v22.is_high_signal(selected_frame)
        role = "video_coverage_high_signal_overlap" if high_signal else "video_coverage_keyframe"
        if str(selected_frame.get("visual_unit_id")) != plan["anchor_visual_unit_id"]:
            stats["coverage_anchor_local_best_shift_count"] += 1
            if selected_frame.get("v14_role") == "video_high_signal_keyframe":
                stats["v14_high_signal_window_replacement_count"] += 1
        else:
            stats["coverage_anchor_exact_selected_count"] += 1
        if role == "video_coverage_high_signal_overlap":
            stats["coverage_high_signal_overlap_count"] += 1
        components = selected_frame.get("human_quality_components") or {}
        if plan["human_window"]:
            stats["human_candidate_selected_count"] += 1
            stats["human_candidate_rejected_count"] += max(0, len(plan["ranked"]) - 1)
            if components.get("face_person_overlap"):
                stats["face_person_overlap_preferred_count"] += 1
        selected_reasons = list(selected_reasons) + list(high_reasons) + [
            "v22_1_layered_window_ranking",
            f"human_quality_score:{safe_float(selected_frame.get('human_quality_score')):.6f}",
            f"sharpness_score:{safe_float(selected_frame.get('sharpness_score')):.6f}",
            f"coverage_window_index:{plan['window_index']}",
            f"coverage_window_ms:{args.coverage_window_ms}",
        ]
        selected_frame["selection_reason_codes"] = list(dict.fromkeys(selected_reasons))
        selected_frame["tail_action"] = "selected_coverage"
        selected.append(
            {
                "frame": selected_frame,
                "score": selected_score,
                "role": role,
                "reasons": selected_reasons,
                "window_index": plan["window_index"],
            }
        )
        if plan["human_window"]:
            candidate_by_id = {
                str(item[1].get("visual_unit_id")): item[1] for item in plan["ranked"]
            }
            record_human_replacement(
                source_id, source_path, safe_int(plan["window_index"]), selected_frame, candidate_by_id
            )
        reports.append(
            {
                "source_content_id": source_id,
                "window_index": plan["window_index"],
                "window_start_ms": plan["window_start_ms"],
                "window_end_exclusive_ms": plan["window_end_exclusive_ms"],
                "candidate_count": len(plan["ranked"]),
                "anchor_visual_unit_id": plan["anchor_visual_unit_id"],
                "selected_visual_unit_id": str(selected_frame.get("visual_unit_id")),
                "selected_role": role,
                "selected_score": round(float(selected_score), 6),
                "human_window": int(plan["human_window"]),
                "selected_human_quality": selected_frame.get("human_quality_score"),
                "selected_sharpness": selected_frame.get("sharpness_score"),
                "local_best_shifted": int(
                    str(selected_frame.get("visual_unit_id")) != plan["anchor_visual_unit_id"]
                ),
                "v14_high_signal_selected": int(
                    selected_frame.get("v14_role") == "video_high_signal_keyframe"
                ),
                "tail_action": selected_frame.get("tail_action"),
            }
        )

    selected_ids = {str(item["frame"].get("visual_unit_id")) for item in selected}
    for index in tail_only_indices:
        previous_plan = plan_by_index.get(index - 1)
        borrowed = None
        if previous_plan:
            for score, candidate, reasons in previous_plan["ranked"]:
                candidate_id = str(candidate.get("visual_unit_id"))
                if candidate_id in selected_ids:
                    continue
                if all(
                    not v22.duplicate_evidence(candidate, item["frame"], args)["duplicate"]
                    for item in selected
                ):
                    borrowed = (score, candidate, reasons)
                    break
        if borrowed:
            score, frame, reasons = borrowed
            frame["tail_action"] = "borrowed_refill_for_tail_window"
            frame["selection_reason_codes"] = list(dict.fromkeys(
                list(reasons) + ["tail_window_borrow_refill", f"coverage_window_index:{index}"]
            ))
            selected.append(
                {
                    "frame": frame,
                    "score": score,
                    "role": "video_coverage_fallback",
                    "reasons": frame["selection_reason_codes"],
                    "window_index": index,
                }
            )
            selected_ids.add(str(frame.get("visual_unit_id")))
            stats["tail_window_borrow_refill_count"] += 1
            reports.append(
                {
                    "source_content_id": source_id,
                    "window_index": index,
                    "window_start_ms": start_ms + index * args.coverage_window_ms,
                    "window_end_exclusive_ms": min(end_ms + 1, start_ms + (index + 1) * args.coverage_window_ms),
                    "candidate_count": len(all_window_frames.get(index, [])),
                    "anchor_visual_unit_id": "",
                    "selected_visual_unit_id": str(frame.get("visual_unit_id")),
                    "selected_role": "video_coverage_fallback",
                    "selected_score": round(float(score), 6),
                    "human_window": 0,
                    "selected_human_quality": frame.get("human_quality_score"),
                    "selected_sharpness": frame.get("sharpness_score"),
                    "local_best_shifted": 1,
                    "v14_high_signal_selected": 0,
                    "tail_action": "borrowed_refill_for_tail_window",
                }
            )
        else:
            stats["tail_window_collapsed_into_previous_count"] += 1

    if not selected:
        fallback_pool = [frame for frame in timed if not frame.get("black_rejected")]
        if fallback_pool:
            frame = max(fallback_pool, key=lambda item: safe_float(item.get("sharpness_score")))
            frame["tail_action"] = "unavoidable_tail_fallback"
            frame["selection_reason_codes"] = ["unavoidable_tail_fallback", "coverage_fallback_only"]
            selected.append(
                {
                    "frame": frame,
                    "score": safe_float(frame.get("sharpness_score")),
                    "role": "video_coverage_fallback",
                    "reasons": frame["selection_reason_codes"],
                    "window_index": frame.get("coverage_window_index", 0),
                }
            )
            stats["unavoidable_tail_fallback_count"] += 1
    stats["coverage_selected_count"] += len(selected)
    return selected, reports


def labels_set(frame: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("label") or "").strip().lower()
        for item in frame.get("labels") or []
        if str(item.get("label") or "").strip().lower() not in v22.WEAK_BACKGROUND_LABELS
    }


def contains_part(labels: Iterable[str], parts: set[str]) -> bool:
    return any(any(part == label or part in label for part in parts) for label in labels)


def supplement_gain(
    frame: MutableMapping[str, Any], nearest: Mapping[str, Any]
) -> Tuple[List[str], Dict[str, Any]]:
    current_labels = labels_set(frame)
    nearest_labels = labels_set(nearest)
    new_labels = sorted(current_labels - nearest_labels)
    current_person = contains_part(current_labels, PERSON_LABELS)
    nearest_person = contains_part(nearest_labels, PERSON_LABELS)
    current_vehicle = contains_part(current_labels, VEHICLE_LABEL_PARTS)
    nearest_vehicle = contains_part(nearest_labels, VEHICLE_LABEL_PARTS)
    current_machine = contains_part(current_labels, MACHINE_LABEL_PARTS)
    nearest_machine = contains_part(nearest_labels, MACHINE_LABEL_PARTS)
    current_animal = contains_part(current_labels, ANIMAL_LABEL_PARTS)
    nearest_animal = contains_part(nearest_labels, ANIMAL_LABEL_PARTS)
    current_text = bool(labels_set(frame) & set(v22.TEXT_BEARING_LABELS))
    nearest_text = bool(labels_set(nearest) & set(v22.TEXT_BEARING_LABELS))
    current_boxes = len(frame.get("labels") or [])
    nearest_boxes = len(nearest.get("labels") or [])
    current_central = safe_float(v22.label_features(frame).get("central_bonus"))
    nearest_central = safe_float(v22.label_features(nearest).get("central_bonus"))
    grid_mad, grid_corr = v22.grid_similarity(frame, nearest)
    cosine = v22.vector_cosine(frame, nearest)
    reasons: List[str] = []
    if new_labels:
        reasons.append("supplement_gain_new_label")
    if current_person and not nearest_person:
        reasons.append("supplement_gain_new_person")
    if current_vehicle and not nearest_vehicle:
        reasons.append("supplement_gain_new_vehicle")
    if current_animal and not nearest_animal:
        reasons.append("supplement_gain_new_animal")
    if current_machine and not nearest_machine:
        reasons.append("supplement_gain_new_machine")
    if current_text and not nearest_text:
        reasons.append("supplement_gain_new_text_carrier")
    if current_boxes >= nearest_boxes + 2 and current_boxes >= 3:
        reasons.append("supplement_gain_subject_count_increase")
    if current_central >= nearest_central + 0.22:
        reasons.append("supplement_gain_composition_improvement")
    if frame.get("v14_role") == "video_high_signal_keyframe" and nearest.get("v14_role") != "video_high_signal_keyframe":
        reasons.append("supplement_gain_v14_high_signal")
    if grid_mad is not None and grid_mad >= 12.0 and current_labels and new_labels:
        reasons.append("supplement_gain_scene_structure_with_subject")
    return list(dict.fromkeys(reasons)), {
        "new_labels": new_labels,
        "current_label_count": len(current_labels),
        "nearest_label_count": len(nearest_labels),
        "current_box_count": current_boxes,
        "nearest_box_count": nearest_boxes,
        "vector_cosine": cosine,
        "grid_mad": grid_mad,
        "grid_corr": grid_corr,
        "label_jaccard": v22.label_jaccard(frame, nearest),
    }


def add_v14_supplements_v22_1(
    frames: Sequence[MutableMapping[str, Any]],
    coverage: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
    stats: Counter[str],
) -> List[Dict[str, Any]]:
    bind_run_stats(stats)
    if not coverage:
        return []
    selected_ids = {str(item["frame"].get("visual_unit_id")) for item in coverage}
    selected_frames = [item["frame"] for item in coverage]
    timed = sorted(
        [frame for frame in frames if safe_int(frame.get("time_position_ms"), -1) >= 0],
        key=lambda frame: safe_int(frame.get("time_position_ms"), -1),
    )
    duration_ms = (
        safe_int(timed[-1].get("time_position_ms"), 0)
        - safe_int(timed[0].get("time_position_ms"), 0)
        if timed else 0
    )
    cap = v22.supplement_cap(max(0, duration_ms))
    pool: List[Tuple[float, MutableMapping[str, Any], List[str]]] = []
    for frame in timed:
        visual_unit_id = str(frame.get("visual_unit_id") or "")
        if frame.get("v14_role") != "video_high_signal_keyframe" or visual_unit_id in selected_ids:
            continue
        stats["supplement_candidate_count"] += 1
        score, reasons = v22.content_score(
            frame, safe_int(frame.get("time_position_ms"), 0), args.coverage_window_ms
        )
        pool.append((score, frame, reasons))
    pool.sort(key=lambda item: (-item[0], safe_int(item[1].get("time_position_ms"), -1)))
    supplements: List[Dict[str, Any]] = []
    for score, frame, reasons in pool:
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
        duplicate = v22.duplicate_evidence(frame, nearest, args)
        stats["final_video_dedup_candidate_pair_count"] += 1
        gains, evidence = supplement_gain(frame, nearest)
        reject_reasons: List[str] = []
        if frame.get("black_rejected"):
            reject_reasons.append("supplement_black_or_invalid")
        if frame.get("tail_excluded"):
            reject_reasons.append("supplement_tail_protected")
        if gap < args.high_signal_supplement_min_gap_ms:
            reject_reasons.append("supplement_min_time_gap_failed")
            stats["high_signal_reject_near_coverage_count"] += 1
        if duplicate["duplicate"]:
            reject_reasons.append("supplement_near_duplicate")
            stats["supplement_near_duplicate_reject_count"] += 1
            v22.mark_dedup_drop(stats, duplicate, "final_video")
        if not gains:
            reject_reasons.append("supplement_grid_or_vector_only")
            cosine = evidence.get("vector_cosine")
            grid_mad = evidence.get("grid_mad")
            if grid_mad is not None and safe_float(grid_mad) > 10.0:
                stats["supplement_grid_only_reject_count"] += 1
            if cosine is not None and safe_float(cosine, 1.0) < 0.96:
                stats["supplement_vector_only_reject_count"] += 1
        if len(supplements) >= cap:
            reject_reasons.append("supplement_cap_reached")
            stats["high_signal_reject_cap_count"] += 1
        accepted = not reject_reasons and bool(gains)
        if accepted:
            stats["supplement_information_gain_pass_count"] += 1
            supplement_reasons = list(reasons) + [
                "v14_high_signal_supplement",
                *gains,
                f"nearest_selected_gap_ms:{gap}",
                f"vector_cosine_to_nearest:{evidence.get('vector_cosine')}",
                f"grid_mad_to_nearest:{evidence.get('grid_mad')}",
            ]
            frame["tail_action"] = "selected_supplement"
            frame["supplement_information_gain"] = True
            frame["supplement_gain_reason_codes"] = gains
            frame["selection_reason_codes"] = list(dict.fromkeys(supplement_reasons))
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
            if "supplement_gain_new_label" in gains:
                stats["supplement_new_label_count"] += 1
            if any(reason.startswith("supplement_gain_new_") for reason in gains):
                stats["supplement_new_subject_count"] += 1
            for reason, counter in (
                ("supplement_gain_new_person", "supplement_new_person_count"),
                ("supplement_gain_new_vehicle", "supplement_new_vehicle_count"),
                ("supplement_gain_new_machine", "supplement_new_machine_count"),
                ("supplement_gain_new_text_carrier", "supplement_new_text_carrier_count"),
                ("supplement_gain_composition_improvement", "supplement_composition_improvement_count"),
                ("supplement_gain_v14_high_signal", "supplement_v14_signal_count"),
            ):
                if reason in gains:
                    stats[counter] += 1
        else:
            stats["supplement_information_gain_reject_count"] += 1
            frame["supplement_information_gain"] = False
            frame["supplement_gain_reason_codes"] = gains
            frame.setdefault("rejection_reason_codes", []).extend(reject_reasons)
        frame["vector_cosine_to_nearest_selected"] = evidence.get("vector_cosine")
        frame["grid_mad_to_nearest_selected"] = evidence.get("grid_mad")
        frame["grid_corr_to_nearest_selected"] = evidence.get("grid_corr")
        AUDIT["supplement_audit"].append(
            {
                "source_content_id": str(frame.get("source_content_id") or ""),
                "source_relative_path": str(frame.get("source_relative_path") or ""),
                "visual_unit_id": str(frame.get("visual_unit_id") or ""),
                "time_position_ms": safe_int(frame.get("time_position_ms"), -1),
                "nearest_selected_visual_unit_id": str(nearest.get("visual_unit_id") or ""),
                "nearest_selected_time_position_ms": safe_int(nearest.get("time_position_ms"), -1),
                "nearest_selected_gap_ms": gap,
                "accepted": accepted,
                "supplement_gain_reason_codes": gains,
                "rejection_reason_codes": reject_reasons,
                **evidence,
            }
        )
    return supplements


def baseline_inputs(v22_out: Path) -> Dict[str, Any]:
    summary = read_json(v22_out / "reports" / "stop03_2_candidate_summary.json")
    q_rows = read_csv(v22_out / "manifests" / "qwenvl_high_value_candidate_queue.csv")
    windows = read_csv(v22_out / "reports" / "coverage_window_report.csv")
    window_map = {
        (str(row.get("source_content_id") or ""), safe_int(row.get("window_index"), -1)):
        str(row.get("selected_visual_unit_id") or "")
        for row in windows
    }
    return {"summary": summary, "q_rows": q_rows, "window_map": window_map}


def enrich_decisions_and_summary(
    result: Dict[str, Any], args: argparse.Namespace, baseline: Dict[str, Any]
) -> None:
    stats: Counter[str] = AUDIT["stats"]
    frame_by_vu = {str(row.get("visual_unit_id") or ""): row for row in result["rows"]}
    AUDIT["frame_by_vu"] = frame_by_vu
    q_video_rows = [
        row for row in result["q_rows"]
        if str(row.get("high_value_category") or "") in v22.VIDEO_OUTPUT_ROLES
    ]
    selected_by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    selection_reasons: Dict[str, List[str]] = {}
    for row in q_video_rows:
        frame = frame_by_vu.get(str(row.get("visual_unit_id") or ""))
        if frame:
            selected_by_source[str(frame.get("source_content_id") or "")].append(frame)
        selection_reasons[str(row.get("visual_unit_id") or "")] = [
            reason for reason in str(row.get("reason_codes") or "").split("|") if reason
        ]
    supplement_by_vu = {
        str(row.get("visual_unit_id") or ""): row for row in AUDIT["supplement_audit"]
    }
    for decision in result["decisions"]:
        visual_unit_id = str(decision.get("visual_unit_id") or "")
        frame = frame_by_vu.get(visual_unit_id) or {}
        components = frame.get("human_quality_components") or human_quality_components(frame)
        decision.update(
            {
                "human_quality_score": components.get("human_quality_score"),
                "human_quality_components": components,
                "sharpness_score": frame.get("sharpness_score"),
                "tail_status": frame.get("tail_status", "not_evaluated_screen_capture"),
                "tail_action": frame.get("tail_action", "not_selected"),
                "supplement_information_gain": frame.get("supplement_information_gain", False),
                "supplement_gain_reason_codes": frame.get("supplement_gain_reason_codes", []),
                "window_rank": frame.get("window_rank", ""),
                "selection_reason_codes": selection_reasons.get(visual_unit_id, frame.get("selection_reason_codes", [])),
                "rejection_reason_codes": list(dict.fromkeys(frame.get("rejection_reason_codes", []))),
            }
        )
        audit_row = supplement_by_vu.get(visual_unit_id)
        if audit_row:
            decision["vector_cosine_to_nearest_selected"] = audit_row.get("vector_cosine")
            decision["grid_mad_to_nearest_selected"] = audit_row.get("grid_mad")
            decision["grid_corr_to_nearest_selected"] = audit_row.get("grid_corr")
        else:
            source_selected = [
                item for item in selected_by_source.get(str(frame.get("source_content_id") or ""), [])
                if str(item.get("visual_unit_id") or "") != visual_unit_id
            ]
            if source_selected:
                nearest = min(
                    source_selected,
                    key=lambda other: abs(
                        safe_int(frame.get("time_position_ms"), -1)
                        - safe_int(other.get("time_position_ms"), -1)
                    ),
                )
                decision["vector_cosine_to_nearest_selected"] = v22.vector_cosine(frame, nearest)
                grid_mad, grid_corr = v22.grid_similarity(frame, nearest)
                decision["grid_mad_to_nearest_selected"] = grid_mad
                decision["grid_corr_to_nearest_selected"] = grid_corr
            else:
                decision["vector_cosine_to_nearest_selected"] = None
                decision["grid_mad_to_nearest_selected"] = None
                decision["grid_corr_to_nearest_selected"] = None
        reasons = [
            reason for reason in decision.get("decision_reason_codes", [])
            if reason != "not_selected_by_v22_video_policy"
        ]
        if not decision.get("qwen_selected") and not decision.get("screen_capture"):
            reasons.append("not_selected_by_v22_1_video_policy")
        decision["decision_reason_codes"] = list(dict.fromkeys(reasons))

    baseline_q_rows = baseline["q_rows"]
    baseline_video_rows = [
        row for row in baseline_q_rows
        if str(row.get("high_value_category") or "") in {
            "video_coverage_keyframe", "video_coverage_high_signal_overlap",
            "video_high_signal_supplement", "video_coverage_fallback",
        }
    ]
    before_ids = {str(row.get("visual_unit_id") or "") for row in baseline_video_rows}
    after_ids = {str(row.get("visual_unit_id") or "") for row in q_video_rows}
    added_ids = sorted(after_ids - before_ids)
    removed_ids = sorted(before_ids - after_ids)
    new_window_map = {
        (str(row.get("source_content_id") or ""), safe_int(row.get("window_index"), -1)):
        str(row.get("selected_visual_unit_id") or "")
        for row in result["window_reports"]
    }
    replacement_mappings: List[Dict[str, Any]] = []
    for key in sorted(set(baseline["window_map"]) & set(new_window_map)):
        old_id = baseline["window_map"][key]
        new_id = new_window_map[key]
        if not old_id or not new_id or old_id == new_id:
            continue
        old = frame_by_vu.get(old_id) or {}
        new = frame_by_vu.get(new_id) or {}
        replacement_mappings.append(
            {
                "source_content_id": key[0],
                "source_relative_path": str(new.get("source_relative_path") or old.get("source_relative_path") or ""),
                "window_index": key[1],
                "old_visual_unit_id": old_id,
                "new_visual_unit_id": new_id,
                "old_time_position_ms": safe_int(old.get("time_position_ms"), -1),
                "new_time_position_ms": safe_int(new.get("time_position_ms"), -1),
                "old_human_quality": old.get("human_quality_score"),
                "new_human_quality": new.get("human_quality_score"),
                "old_tail_status": old.get("tail_status"),
                "new_tail_status": new.get("tail_status"),
            }
        )
    baseline_last = 0
    baseline_tail = 0
    baseline_tail_rows: List[Dict[str, Any]] = []
    for row in baseline_video_rows:
        frame = frame_by_vu.get(str(row.get("visual_unit_id") or "")) or {}
        if frame.get("is_last_frame"):
            baseline_last += 1
        if frame.get("tail_excluded"):
            baseline_tail += 1
            baseline_tail_rows.append(dict(row))
    selected_last_rows = []
    selected_tail_rows = []
    role_breakdown: Counter[str] = Counter()
    for row in q_video_rows:
        frame = frame_by_vu.get(str(row.get("visual_unit_id") or "")) or {}
        if frame.get("is_last_frame"):
            selected_last_rows.append(row)
            role_breakdown[str(row.get("high_value_category") or "unknown")] += 1
        if frame.get("tail_excluded"):
            selected_tail_rows.append(row)
    tail_changes: List[Dict[str, Any]] = []
    after_by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in q_video_rows:
        after_by_source[str(row.get("source_content_id") or "")].append(row)
    for old_row in baseline_tail_rows:
        source_id = str(old_row.get("source_content_id") or "")
        old_time = safe_int(old_row.get("time_position_ms"), -1)
        replacement = None
        if after_by_source.get(source_id):
            replacement = min(
                after_by_source[source_id],
                key=lambda row: abs(safe_int(row.get("time_position_ms"), -1) - old_time),
            )
        tail_changes.append(
            {
                "source_content_id": source_id,
                "source_relative_path": str(old_row.get("source_relative_path") or ""),
                "old_visual_unit_id": str(old_row.get("visual_unit_id") or ""),
                "old_time_position_ms": old_time,
                "new_visual_unit_id": str((replacement or {}).get("visual_unit_id") or ""),
                "new_time_position_ms": safe_int((replacement or {}).get("time_position_ms"), -1),
                "action": "tail_protected_removed_or_replaced",
            }
        )

    summary = result["summary"]
    baseline_summary = baseline["summary"]
    required_counter_fields = [
        "human_window_count", "human_candidate_evaluated_count",
        "human_edge_crop_penalty_count", "human_face_edge_crop_penalty_count",
        "human_partial_body_penalty_count", "human_blur_penalty_count",
        "face_person_overlap_preferred_count", "human_candidate_selected_count",
        "human_candidate_rejected_count", "tail_candidate_rejected_count",
        "tail_window_borrow_refill_count", "tail_window_collapsed_into_previous_count",
        "unavoidable_tail_fallback_count", "supplement_candidate_count",
        "supplement_information_gain_pass_count", "supplement_information_gain_reject_count",
        "supplement_new_label_count", "supplement_new_subject_count",
        "supplement_new_person_count", "supplement_new_vehicle_count",
        "supplement_new_machine_count", "supplement_new_text_carrier_count",
        "supplement_composition_improvement_count", "supplement_v14_signal_count",
        "supplement_grid_only_reject_count", "supplement_vector_only_reject_count",
        "supplement_near_duplicate_reject_count",
    ]
    for key in required_counter_fields:
        summary[key] = stats[key]
    summary.update(
        {
            "commit_status": "DO_NOT_COMMIT" if summary.get("execution_mode") != "commit" else "COMMITTED",
            "visual_review_status": "PENDING_USER_REVIEW",
            "human_quality_replacement_count": len(AUDIT["human_replacements"]),
            "sharpness_metric_name": "sobel_tenengrad_energy_pillow_numpy",
            "sharpness_metric_available": True,
            "sharpness_evaluated_count": stats["sharpness_evaluated_count"],
            "sharpness_failed_count": stats["sharpness_failed_count"],
            "selected_last_frame_count": len(selected_last_rows),
            "selected_last_frame_ratio": round(len(selected_last_rows) / max(1, len(q_video_rows)), 8),
            "selected_last_frame_reason_breakdown": dict(role_breakdown),
            "selected_tail_protected_frame_count": len(selected_tail_rows),
            "same_as_v22_video_visual_unit_set": before_ids == after_ids,
            "v22_1_added_video_visual_unit_count": len(added_ids),
            "v22_1_removed_video_visual_unit_count": len(removed_ids),
            "v22_1_replaced_video_visual_unit_count": len(replacement_mappings),
            "v22_baseline_selected_last_frame_count": baseline_last,
            "v22_baseline_selected_tail_protected_frame_count": baseline_tail,
            "v22_vs_v22_1": {
                "qwenvl_total_before": safe_int(baseline_summary.get("qwenvl_total_count"), 0),
                "qwenvl_total_after": len(result["q_rows"]),
                "video_total_before": len(baseline_video_rows),
                "video_total_after": len(q_video_rows),
                "ocr_total_before": safe_int(baseline_summary.get("ocr_total_count"), 0),
                "ocr_total_after": len(result["o_rows"]),
                "coverage_before": safe_int(baseline_summary.get("coverage_selected_count"), 0),
                "coverage_after": summary.get("coverage_selected_count"),
                "overlap_before": safe_int(baseline_summary.get("coverage_high_signal_overlap_count"), 0),
                "overlap_after": summary.get("coverage_high_signal_overlap_count"),
                "supplement_before": safe_int(baseline_summary.get("v14_high_signal_supplement_added_count"), 0),
                "supplement_after": summary.get("v14_high_signal_supplement_added_count"),
                "selected_last_frame_before": baseline_last,
                "selected_last_frame_after": len(selected_last_rows),
                "tail_protected_selected_before": baseline_tail,
                "tail_protected_selected_after": len(selected_tail_rows),
                "human_quality_replacements": len(AUDIT["human_replacements"]),
                "supplement_rejected_by_information_gain": stats["supplement_information_gain_reject_count"],
                "normal_video_missing_coverage_before": safe_int(baseline_summary.get("normal_video_group_missing_coverage_count"), 0),
                "normal_video_missing_coverage_after": summary.get("normal_video_group_missing_coverage_count"),
            },
        }
    )
    summary["policy_version"] = POLICY_VERSION
    summary["script_version"] = SCRIPT_VERSION
    summary["policy_status"] = "REVIEW"
    summary["policy_reason_codes"] = ["v22_1_dry_run_pending_user_visual_review"]
    if not AUDIT["human_replacements"]:
        summary["policy_status"] = "FAIL"
        summary["policy_reason_codes"].append("human_quality_replacement_count_zero")
    if not stats["tail_candidate_rejected_count"]:
        summary["policy_status"] = "FAIL"
        summary["policy_reason_codes"].append("tail_protection_not_executed")
    if not stats["supplement_candidate_count"] or not stats["supplement_information_gain_reject_count"]:
        summary["policy_status"] = "FAIL"
        summary["policy_reason_codes"].append("supplement_information_gain_gate_not_demonstrated")
    if before_ids == after_ids and not AUDIT["human_replacements"]:
        summary["policy_status"] = "FAIL"
        summary["policy_reason_codes"].append("v22_1_identical_without_mechanism_effect")
    result["human_replacements"] = AUDIT["human_replacements"]
    result["supplement_audit"] = AUDIT["supplement_audit"]
    result["replacement_mappings"] = replacement_mappings
    result["added_video_rows"] = [
        row for row in q_video_rows if str(row.get("visual_unit_id") or "") in set(added_ids)
    ]
    result["removed_video_rows"] = [
        row for row in baseline_video_rows if str(row.get("visual_unit_id") or "") in set(removed_ids)
    ]
    result["tail_changes"] = tail_changes


def build_candidates_v22_1(
    con: sqlite3.Connection,
    args: argparse.Namespace,
    run_id: str,
    mode: str,
) -> Dict[str, Any]:
    global AUDIT
    AUDIT = new_audit()
    baseline = baseline_inputs(Path(args.v22_out).expanduser().resolve(strict=True))
    AUDIT["v22_window_map"] = baseline["window_map"]
    AUDIT["v22_q_rows"] = baseline["q_rows"]
    result = ORIGINAL_BUILD_CANDIDATES(con, args, run_id, mode)
    enrich_decisions_and_summary(result, args, baseline)
    return result


def write_outputs_v22_1(out: Path, result: Dict[str, Any]) -> Dict[str, str]:
    outputs = ORIGINAL_WRITE_OUTPUTS(out, result)
    reports = out / "reports"
    additions = {
        "human_quality_replacements_jsonl": reports / "human_quality_replacements.jsonl",
        "supplement_information_gain_audit_jsonl": reports / "supplement_information_gain_audit.jsonl",
        "v22_v22_1_replacement_mapping_jsonl": reports / "v22_v22_1_replacement_mapping.jsonl",
        "v22_1_added_video_rows_jsonl": reports / "v22_1_added_video_rows.jsonl",
        "v22_1_removed_video_rows_jsonl": reports / "v22_1_removed_video_rows.jsonl",
        "tail_change_samples_jsonl": reports / "tail_change_samples.jsonl",
    }
    write_jsonl(additions["human_quality_replacements_jsonl"], result["human_replacements"])
    write_jsonl(additions["supplement_information_gain_audit_jsonl"], result["supplement_audit"])
    write_jsonl(additions["v22_v22_1_replacement_mapping_jsonl"], result["replacement_mappings"])
    write_jsonl(additions["v22_1_added_video_rows_jsonl"], result["added_video_rows"])
    write_jsonl(additions["v22_1_removed_video_rows_jsonl"], result["removed_video_rows"])
    write_jsonl(additions["tail_change_samples_jsonl"], result["tail_changes"])
    outputs.update({key: str(path) for key, path in additions.items()})
    result["summary"]["outputs"] = outputs
    return outputs


def run_preflight_v22_1(args: argparse.Namespace) -> Dict[str, Any]:
    result = ORIGINAL_RUN_PREFLIGHT(args)
    v22_out = Path(args.v22_out).expanduser().resolve(strict=True)
    checks = {
        "v22_summary_exists": (v22_out / "reports" / "stop03_2_candidate_summary.json").is_file(),
        "v22_qwenvl_manifest_exists": (v22_out / "manifests" / "qwenvl_high_value_candidate_queue.csv").is_file(),
        "v22_window_report_exists": (v22_out / "reports" / "coverage_window_report.csv").is_file(),
        "pillow_available": Image is not None,
        "numpy_available": np is not None,
    }
    status = "PASS" if result.get("technical_status") == "PASS" and all(checks.values()) else "FAIL"
    result.update(
        {
            "validation_status": status,
            "technical_status": status,
            "policy_status": "REVIEW",
            "commit_status": "DO_NOT_COMMIT",
            "visual_review_status": "NOT_RUN",
            "script_version": SCRIPT_VERSION,
            "policy_version": POLICY_VERSION,
            "v22_1_checks": checks,
            "v22_baseline_out": str(v22_out),
            "python_executable": sys.executable,
            "model_loading": False,
            "will_download": False,
            "will_modify_original_media": False,
        }
    )
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-2 V22.1 visual-quality refinement")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight-only", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--commit", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--v14-out", default=str(DEFAULT_V14_OUT))
    parser.add_argument("--v20-out", default=str(DEFAULT_V20_OUT))
    parser.add_argument("--v21-out", default=str(DEFAULT_V21_OUT))
    parser.add_argument("--v22-out", default=str(DEFAULT_V22_OUT))
    parser.add_argument("--clear-existing-candidate-items", action="store_true")
    parser.add_argument("--coverage-window-ms", type=int, default=18_000)
    parser.add_argument("--video-stride", type=int, default=6)
    parser.add_argument("--image-yolo-threshold", type=float, default=4.2)
    parser.add_argument("--high-signal-supplement-min-gap-ms", type=int, default=9_000)
    parser.add_argument("--dedup-time-gap-ms", type=int, default=12_000)
    parser.add_argument("--dedup-vector-threshold", type=float, default=0.985)
    parser.add_argument("--dedup-grid-mad-threshold", type=float, default=6.0)
    parser.add_argument("--dedup-grid-corr-threshold", type=float, default=0.98)
    parser.add_argument("--dedup-label-jaccard-threshold", type=float, default=0.80)
    parser.add_argument("--normal-video-ocr-cap", type=int, default=1)
    parser.add_argument("--normal-video-ocr-min-score", type=float, default=0.0)
    parser.add_argument("--normal-video-ocr-min-gap-ms", type=int, default=20_000)
    parser.add_argument("--final-video-dedup-min-gap-ms", type=int, default=12_000)
    parser.add_argument("--final-video-dedup-vector-threshold", type=float, default=None)
    parser.add_argument("--final-video-dedup-grid-mad-threshold", type=float, default=None)
    parser.add_argument("--final-video-dedup-label-sim-threshold", type=float, default=None)
    return parser.parse_args(argv)


def readonly_db_counts(db: Path) -> Dict[str, int]:
    con = phase1.connect_readonly(db)
    try:
        return {
            "candidate_queue_items": int(
                con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_items").fetchone()[0]
            ),
            "model_runs": int(con.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]),
            "v22_1_model_runs": int(
                con.execute(
                    "SELECT COUNT(*) FROM model_runs WHERE script_version=? OR run_id LIKE ?",
                    (SCRIPT_VERSION, "%v22_1%"),
                ).fetchone()[0]
            ),
        }
    finally:
        con.close()


def configure_v22_module() -> None:
    v22.SCRIPT_VERSION = SCRIPT_VERSION
    v22.POLICY_VERSION = POLICY_VERSION
    v22.DEFAULT_OUT = DEFAULT_OUT
    v22.VIDEO_OUTPUT_ROLES.add("video_coverage_fallback")
    v22.attach_signatures = attach_signatures_v22_1
    v22.build_coverage_for_video = build_coverage_for_video_v22_1
    v22.add_v14_supplements = add_v14_supplements_v22_1


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_v22_module()
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
            result = run_preflight_v22_1(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.get("technical_status") == "PASS" else 2

        mode = "commit" if args.commit else "dry_run"
        out = v22.assert_test_output_path(Path(args.out), may_exist=False)
        db = Path(args.db).expanduser().resolve(strict=True)
        before_sha = phase1.sha256_file(db)
        before_mtime = db.stat().st_mtime_ns
        before_counts = readonly_db_counts(db)
        preflight = run_preflight_v22_1(args)
        if preflight.get("technical_status") != "PASS":
            raise RuntimeError("preflight_failed_before_" + mode)
        con = phase1.connect_readonly(db)
        try:
            run_id = f"{SCRIPT_VERSION}_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
            built = build_candidates_v22_1(con, args, run_id, mode)
        finally:
            con.close()
        if built["summary"].get("technical_status") != "PASS":
            raise RuntimeError("candidate_validation_failed")
        write_outputs_v22_1(out, built)
        if args.commit:
            commit_result = v22.commit_candidate_rows(
                db, built, run_id, bool(args.clear_existing_candidate_items)
            )
            built["summary"]["commit"] = commit_result
            built["summary"]["commit_status"] = "COMMITTED"
        after_sha = phase1.sha256_file(db)
        after_mtime = db.stat().st_mtime_ns
        after_counts = readonly_db_counts(db)
        unchanged = (
            before_sha == after_sha
            and before_mtime == after_mtime
            and before_counts == after_counts
        )
        built["summary"]["read_only_integrity"] = {
            "db_sha256_before": before_sha,
            "db_sha256_after": after_sha,
            "db_mtime_ns_before": before_mtime,
            "db_mtime_ns_after": after_mtime,
            "db_counts_before": before_counts,
            "db_counts_after": after_counts,
            "db_unchanged": unchanged,
            "candidate_queue_items_written": 0 if mode == "dry_run" else len(built["q_rows"]) + len(built["o_rows"]),
            "model_runs_written": 0 if mode == "dry_run" else 1,
        }
        if mode == "dry_run" and not unchanged:
            built["summary"]["technical_status"] = "FAIL"
            built["summary"]["validation_status"] = "FAIL"
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
            "commit_status": "DO_NOT_COMMIT",
            "visual_review_status": "NOT_RUN",
            "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
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
