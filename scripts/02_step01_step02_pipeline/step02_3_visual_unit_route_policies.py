#!/usr/bin/env python3
"""Production Step02-3 visual-unit routing policies.

This module is metadata-only. It does not call visual models or inspect media.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple


POLICY_VERSION = "step02_3_route_policy_v1"

OCR_HINT_TOKENS = [
    "RPReplay",
    "screen",
    "screenshot",
    "screenrecord",
    "screen_record",
    "录屏",
    "截屏",
    "截图",
    "字幕",
    "ocr",
    "text",
    "文字",
]


def route_visual_units(
    units: Sequence[dict],
    *,
    yoloe_coverage_ms: int = 6000,
    yoloe_max_gap_ms: int = 10000,
    high_value_min_gap_ms: int = 20000,
) -> Dict[str, dict]:
    decisions = {
        u["visual_unit_id"]: {
            "selected_for_yoloe": False,
            "selected_for_high_value": False,
            "selected_for_ocr_trigger": False,
            "route_reason_yoloe": "",
            "route_reason_high_value": "",
            "route_reason_ocr_trigger": "",
            "route_conflict_resolution": "",
        }
        for u in units
    }
    _select_yoloe(units, decisions, yoloe_coverage_ms, yoloe_max_gap_ms)
    _select_high_value(units, decisions, high_value_min_gap_ms)
    _select_ocr(units, decisions)
    _resolve_conflicts(decisions)
    return decisions


def _select_yoloe(units: Sequence[dict], decisions: Dict[str, dict], coverage_ms: int, max_gap_ms: int) -> None:
    by_video: Dict[str, List[dict]] = defaultdict(list)
    for u in units:
        if u["visual_unit_type"] == "video_frame":
            by_video[u["source_video_id"]].append(u)
        elif u["visual_unit_type"] == "image_preview":
            d = decisions[u["visual_unit_id"]]
            d["selected_for_yoloe"] = True
            d["route_reason_yoloe"] = "yoloe_image_a9t_preselected"

    for _video_id, frames in by_video.items():
        frames = sorted(frames, key=lambda u: (_time_ms(u), _frame_index(u), u["visual_unit_id"]))
        if not frames:
            continue
        selected_ids: Set[str] = set()
        _mark_yoloe(frames[0], decisions, selected_ids, "yoloe_video_first_frame")
        if len(frames) == 1:
            continue
        start = _time_ms(frames[0])
        end = _time_ms(frames[-1])
        if end - start <= coverage_ms:
            _mark_yoloe(frames[-1], decisions, selected_ids, "yoloe_video_short_fallback")
        else:
            anchor = start + coverage_ms
            while anchor <= end:
                nearest = min(frames, key=lambda u: (abs(_time_ms(u) - anchor), _time_ms(u)))
                _mark_yoloe(nearest, decisions, selected_ids, "yoloe_video_time_coverage")
                anchor += coverage_ms
        _enforce_max_gap(frames, decisions, selected_ids, max_gap_ms)


def _mark_yoloe(unit: dict, decisions: Dict[str, dict], selected_ids: Set[str], reason: str) -> None:
    vid = unit["visual_unit_id"]
    d = decisions[vid]
    d["selected_for_yoloe"] = True
    if not d["route_reason_yoloe"]:
        d["route_reason_yoloe"] = reason
    selected_ids.add(vid)


def _enforce_max_gap(frames: Sequence[dict], decisions: Dict[str, dict], selected_ids: Set[str], max_gap_ms: int) -> None:
    if max_gap_ms <= 0:
        return
    while True:
        selected = [u for u in frames if u["visual_unit_id"] in selected_ids]
        selected = sorted(selected, key=_time_ms)
        worst_gap = 0
        insert_unit = None
        for left, right in zip(selected, selected[1:]):
            gap = _time_ms(right) - _time_ms(left)
            if gap <= max_gap_ms:
                continue
            between = [u for u in frames if _time_ms(left) < _time_ms(u) < _time_ms(right) and u["visual_unit_id"] not in selected_ids]
            if between and gap > worst_gap:
                midpoint = (_time_ms(left) + _time_ms(right)) // 2
                insert_unit = min(between, key=lambda u: (abs(_time_ms(u) - midpoint), _time_ms(u)))
                worst_gap = gap
        if insert_unit is None:
            return
        _mark_yoloe(insert_unit, decisions, selected_ids, "yoloe_video_max_gap_fill")


def _select_high_value(units: Sequence[dict], decisions: Dict[str, dict], min_gap_ms: int) -> None:
    by_video: Dict[str, List[dict]] = defaultdict(list)
    by_dir: Dict[str, List[dict]] = defaultdict(list)
    for u in units:
        if u["visual_unit_type"] == "video_frame" and decisions[u["visual_unit_id"]]["selected_for_yoloe"]:
            by_video[u["source_video_id"]].append(u)
        elif u["visual_unit_type"] == "image_preview" and decisions[u["visual_unit_id"]]["selected_for_yoloe"]:
            by_dir[str(Path(u.get("source_relative_path") or "").parent)].append(u)

    for _video_id, frames in by_video.items():
        frames = sorted(frames, key=lambda u: (_time_ms(u), _frame_index(u)))
        budget = _video_high_value_budget(frames)
        for unit, reason in _temporal_representatives(frames, budget, min_gap_ms):
            d = decisions[unit["visual_unit_id"]]
            d["selected_for_high_value"] = True
            d["route_reason_high_value"] = reason

    for _dir_name, rows in by_dir.items():
        rows = sorted(rows, key=lambda u: (u.get("source_relative_path") or "", u["visual_unit_id"]))
        timelapse = [u for u in rows if (u.get("preview_role") or "").lower() == "timelapse_keyframe"]
        for unit in timelapse:
            d = decisions[unit["visual_unit_id"]]
            d["selected_for_high_value"] = True
            d["route_reason_high_value"] = "high_value_timelapse_keyframe"
        normal = [u for u in rows if u not in timelapse]
        budget = max(1, math.ceil(len(normal) / 40)) if normal else 0
        for unit in _sparse_pick(normal, budget):
            d = decisions[unit["visual_unit_id"]]
            d["selected_for_high_value"] = True
            d["route_reason_high_value"] = "high_value_normal_image_sparse_directory_representative"


def _select_ocr(units: Sequence[dict], decisions: Dict[str, dict]) -> None:
    for u in units:
        d = decisions[u["visual_unit_id"]]
        if not d["selected_for_yoloe"]:
            continue
        reason = _ocr_reason(u)
        if reason:
            d["selected_for_ocr_trigger"] = True
            d["route_reason_ocr_trigger"] = reason


def _resolve_conflicts(decisions: Dict[str, dict]) -> None:
    for d in decisions.values():
        if d["selected_for_high_value"] and d["selected_for_ocr_trigger"]:
            d["selected_for_high_value"] = False
            d["route_reason_high_value"] = ""
            d["route_conflict_resolution"] = "ocr_over_high_value"


def _ocr_reason(unit: dict) -> str:
    explicit_fields = ["ocr_hint", "text_hint", "screen_recording_hint"]
    for field in explicit_fields:
        val = str(unit.get(field) or "").strip().lower()
        if val in {"1", "true", "yes", "y"}:
            return "ocr_trigger_explicit_manifest_hint"
    preview_role = str(unit.get("preview_role") or "")
    if _has_hint(preview_role):
        return "ocr_trigger_explicit_manifest_hint"
    haystacks = [
        unit.get("source_relative_path") or "",
        unit.get("source_video_relative_path") or "",
        Path(unit.get("visual_file") or "").name,
    ]
    joined = " ".join(haystacks)
    if _has_hint(joined):
        lowered = joined.lower()
        if "screenshot" in lowered or "截屏" in joined or "截图" in joined:
            return "ocr_trigger_screenshot_filename_hint"
        return "ocr_trigger_screen_recording_filename_hint"
    return ""


def _has_hint(text: str) -> bool:
    folded = text.lower()
    return any(token.lower() in folded for token in OCR_HINT_TOKENS)


def _video_high_value_budget(frames: Sequence[dict]) -> int:
    if not frames:
        return 0
    duration_s = max(0, _time_ms(frames[-1]) - _time_ms(frames[0])) / 1000.0
    if duration_s <= 30:
        return 1
    if duration_s <= 60:
        return 2 if len(frames) >= 2 else 1
    if duration_s <= 180:
        return min(math.ceil(duration_s / 60), 3)
    if duration_s <= 300:
        return min(math.ceil(duration_s / 60), 5)
    return min(math.ceil(duration_s / 60), 10)


def _temporal_representatives(frames: Sequence[dict], budget: int, min_gap_ms: int) -> List[Tuple[dict, str]]:
    if budget <= 0 or not frames:
        return []
    if budget == 1:
        unit = frames[len(frames) // 2]
        reason = "short_video_representative" if len(frames) <= 3 else "temporal_start_or_middle_representative"
        return [(unit, reason)]
    start = _time_ms(frames[0])
    end = _time_ms(frames[-1])
    picked: List[dict] = []
    for i in range(budget):
        anchor = round(start + (end - start) * i / max(1, budget - 1))
        candidates = sorted(frames, key=lambda u: (abs(_time_ms(u) - anchor), _time_ms(u)))
        for cand in candidates:
            if all(abs(_time_ms(cand) - _time_ms(existing)) >= min_gap_ms for existing in picked):
                picked.append(cand)
                break
    if not picked:
        picked = [frames[len(frames) // 2]]
    reason = "long_video_sparse_representative" if budget >= 4 else "temporal_coverage_representative"
    return [(u, reason) for u in sorted(picked, key=_time_ms)]


def _sparse_pick(rows: Sequence[dict], budget: int) -> List[dict]:
    if budget <= 0 or not rows:
        return []
    if len(rows) <= budget:
        return list(rows)
    if budget == 1:
        return [rows[len(rows) // 2]]
    picked = []
    for i in range(budget):
        idx = round((len(rows) - 1) * i / max(1, budget - 1))
        picked.append(rows[idx])
    return picked


def _time_ms(unit: dict) -> int:
    val = unit.get("time_position_ms")
    if val in (None, ""):
        return 0
    return int(float(val))


def _frame_index(unit: dict) -> int:
    val = unit.get("frame_index")
    if val in (None, ""):
        return 0
    return int(float(val))
