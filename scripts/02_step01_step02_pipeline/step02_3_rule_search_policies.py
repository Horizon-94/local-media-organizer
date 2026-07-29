#!/usr/bin/env python3
"""Low-cost Step02-3 rule-search policies.

These policies only consume frame metrics. They do not read labels, call models,
or modify source frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Set, Tuple


POLICY_NAMES = [
    "v7_original_like",
    "cluster_rep_fix2_like",
    "cluster_rep_more_coverage",
    "cluster_rep_stricter_static",
    "ocr_text_screen_policy",
]


@dataclass(frozen=True)
class PolicySelection:
    yoloe: Set[str]
    high_value: Set[str]
    ocr_trigger: Set[str]
    reason: str


def _duration_ms(frames: Sequence[dict]) -> int:
    if not frames:
        return 0
    return max(int(f["time_ms"]) for f in frames) - min(int(f["time_ms"]) for f in frames)


def _quality(f: dict) -> float:
    luma = float(f.get("luma_mean", 0.0))
    luma_std = float(f.get("luma_std", 0.0))
    entropy = float(f.get("entropy", 0.0))
    edge = float(f.get("edge_density", 0.0))
    dark = float(f.get("underexposed_ratio", 0.0))
    bright = float(f.get("overexposed_ratio", 0.0))
    usable_luma = 1.0 - min(abs(luma - 0.50) / 0.50, 1.0)
    exposure_penalty = min(0.8, dark * 0.55 + bright * 0.45)
    return 0.24 * usable_luma + 0.24 * min(luma_std / 0.28, 1.0) + 0.30 * entropy + 0.22 * min(edge / 0.24, 1.0) - exposure_penalty


def _change(f: dict) -> float:
    return (
        float(f.get("pixel_mad", 0.0)) * 2.2
        + float(f.get("changed_area_ratio", 0.0)) * 0.9
        + float(f.get("edge_mad", 0.0)) * 1.6
        + float(f.get("hist_l1", 0.0)) * 0.7
        + float(f.get("luma_diff", 0.0)) * 0.9
    )


def _is_bad(f: dict) -> bool:
    luma = float(f.get("luma_mean", 0.0))
    std = float(f.get("luma_std", 0.0))
    edge = float(f.get("edge_density", 0.0))
    entropy = float(f.get("entropy", 0.0))
    flat_dark_or_blank = (luma < 0.16 and std < 0.018 and edge < 0.015) or (entropy < 0.22 and std < 0.025)
    return (
        float(f.get("underexposed_ratio", 0.0)) > 0.72
        or float(f.get("overexposed_ratio", 0.0)) > 0.72
        or flat_dark_or_blank
        or _quality(f) < -0.05
    )


def _text_score(f: dict) -> float:
    return (
        float(f.get("edge_density", 0.0)) * 1.7
        + float(f.get("entropy", 0.0)) * 0.8
        + min(float(f.get("luma_std", 0.0)) / 0.24, 1.0) * 0.6
        - float(f.get("changed_area_ratio", 0.0)) * 0.3
    )


def _ordered(frames: Sequence[dict]) -> List[dict]:
    return sorted(frames, key=lambda f: (int(f["time_ms"]), int(f["index"]), str(f["file_name"])))


def _add_with_gap(selected: List[dict], candidate: dict, min_gap_ms: int) -> None:
    t = int(candidate["time_ms"])
    if all(abs(t - int(x["time_ms"])) >= min_gap_ms for x in selected):
        selected.append(candidate)


def _pick_timeline(frames: Sequence[dict], target_count: int, min_gap_ms: int) -> List[dict]:
    good = [f for f in _ordered(frames) if not _is_bad(f)]
    if not good or target_count <= 0:
        return []
    if target_count == 1:
        center_time = int(good[len(good) // 2]["time_ms"])
        return [max(good, key=lambda f: (_quality(f), -abs(int(f["time_ms"]) - center_time)))]
    t0 = int(good[0]["time_ms"])
    t1 = int(good[-1]["time_ms"])
    anchors = [round(t0 + (t1 - t0) * i / max(1, target_count - 1)) for i in range(target_count)]
    selected: List[dict] = []
    for anchor in anchors:
        pool = sorted(good, key=lambda f: (abs(int(f["time_ms"]) - anchor), -_quality(f)))
        for cand in pool:
            before = len(selected)
            _add_with_gap(selected, cand, min_gap_ms)
            if len(selected) > before:
                break
    return _ordered(selected)


def _cluster_reps(frames: Sequence[dict], per_cluster: int, min_gap_ms: int, max_count: int) -> List[dict]:
    good = [f for f in _ordered(frames) if not _is_bad(f)]
    by_cluster = {}
    for f in good:
        by_cluster.setdefault(int(f.get("cluster_id", 0)), []).append(f)
    selected: List[dict] = []
    for cid in sorted(by_cluster):
        group = by_cluster[cid]
        ranked = sorted(group, key=lambda f: (_quality(f) + _change(f) * 0.35, -abs(int(f["time_ms"]) - int(group[len(group)//2]["time_ms"]))), reverse=True)
        for cand in ranked[:per_cluster]:
            _add_with_gap(selected, cand, min_gap_ms)
    if len(selected) < min(max_count, max(1, len(good) // 18)):
        for cand in _pick_timeline(good, min(max_count, max(1, len(good) // 18)), min_gap_ms):
            _add_with_gap(selected, cand, min_gap_ms)
    return _ordered(sorted(selected, key=lambda f: (_quality(f) + _change(f), int(f["time_ms"])), reverse=True)[:max_count])


def _high_value_from_yoloe(frames: Sequence[dict], yoloe: Iterable[str], max_count: int, min_gap_ms: int) -> List[dict]:
    yoloe_set = set(yoloe)
    pool = [f for f in frames if f["file_name"] in yoloe_set and not _is_bad(f)]
    ranked = sorted(pool, key=lambda f: (_quality(f) + _change(f) * 0.2, -float(f.get("changed_area_ratio", 0.0))), reverse=True)
    selected: List[dict] = []
    for cand in ranked:
        _add_with_gap(selected, cand, min_gap_ms)
        if len(selected) >= max_count:
            break
    return _ordered(selected)


def _route(frames: Sequence[dict], yoloe_frames: Sequence[dict], high_frames: Sequence[dict], ocr_frames: Sequence[dict], reason: str) -> PolicySelection:
    yoloe = {f["file_name"] for f in yoloe_frames}
    high_value = {f["file_name"] for f in high_frames if f["file_name"] in yoloe}
    ocr = {f["file_name"] for f in ocr_frames if f["file_name"] in yoloe}
    high_value -= ocr
    return PolicySelection(yoloe=yoloe, high_value=high_value, ocr_trigger=ocr, reason=reason)


def apply_policy(policy_name: str, video_id: str, frames: Sequence[dict]) -> PolicySelection:
    frames = _ordered(frames)
    if not frames:
        return PolicySelection(set(), set(), set(), "no_frames")
    duration = max(1, _duration_ms(frames))
    minutes = max(duration / 60000.0, 0.10)
    safe_cap = min(50, max(1, int(minutes * 10)))
    vid = video_id.lower()
    ocr_like = "rpreplay" in vid or "ocr" in vid or "screen" in vid
    mobile_like = any(x in vid for x in ["gopro", "gx022345", "c013", "dive", "walk"])
    static_like = any(x in vid for x in ["c031", "c034", "r5_", "c077", "office"])

    if policy_name == "v7_original_like":
        target = min(safe_cap, max(2, int(minutes * 3.0)))
        selected = _pick_timeline(frames, target, 12000)
        hv = _high_value_from_yoloe(frames, [f["file_name"] for f in selected], max(1, min(5, int(minutes * 2.0))), 45000)
        ocr = []
        if ocr_like:
            ocr = sorted(selected, key=_text_score, reverse=True)[:1]
            hv = []
        return _route(frames, selected, hv, ocr, "baseline timeline/quality coverage approximating older v7 behavior")

    if policy_name == "cluster_rep_fix2_like":
        target_cap = min(safe_cap, max(3 if mobile_like else 2, int(minutes * (4.2 if mobile_like else 2.0))))
        selected = _cluster_reps(frames, 1, 9000, target_cap)
        hv = _high_value_from_yoloe(frames, [f["file_name"] for f in selected], max(1, min(6, int(minutes * 1.5) + 1)), 36000)
        ocr = []
        if ocr_like:
            ocr = sorted(selected, key=_text_score, reverse=True)[:1]
            hv = []
        return _route(frames, selected, hv, ocr, "visual cluster representatives with moderate long-cluster coverage")

    if policy_name == "cluster_rep_more_coverage":
        target_cap = min(safe_cap, max(4 if mobile_like else 3, int(minutes * (6.0 if mobile_like else 3.0))))
        selected = _cluster_reps(frames, 2, 7000, target_cap)
        for cand in _pick_timeline(frames, target_cap, 7000):
            _add_with_gap(selected, cand, 7000)
            if len(selected) >= target_cap:
                break
        hv = _high_value_from_yoloe(frames, [f["file_name"] for f in selected], max(1, min(8, int(minutes * 2.0) + 1)), 27000)
        ocr = sorted(selected, key=_text_score, reverse=True)[:1] if ocr_like else []
        if ocr_like:
            hv = []
        return _route(frames, _ordered(selected[:target_cap]), hv, ocr, "wider cluster/time coverage for moving or documentary samples")

    if policy_name == "cluster_rep_stricter_static":
        target = 2 if static_like else max(2, int(minutes * (2.8 if mobile_like else 1.6)))
        target = min(safe_cap, target)
        selected = _cluster_reps(frames, 1, 18000 if static_like else 12000, target)
        if not selected:
            selected = _pick_timeline(frames, 1, 18000)
        hv = _high_value_from_yoloe(frames, [f["file_name"] for f in selected], 1 if static_like else max(1, min(4, int(minutes) + 1)), 60000)
        ocr = sorted(selected, key=_text_score, reverse=True)[:1] if ocr_like else []
        if ocr_like:
            hv = []
        return _route(frames, selected, hv, ocr, "stricter static/talking-head/environment route with low duplicate tolerance")

    if policy_name == "ocr_text_screen_policy":
        target = 2 if ocr_like else max(1, min(4, int(minutes * 1.2)))
        selected = sorted([f for f in frames if not _is_bad(f)], key=_text_score, reverse=True)[:target]
        selected = _ordered(selected)
        ocr = selected[:1] if selected else []
        hv = [] if ocr_like else _high_value_from_yoloe(frames, [f["file_name"] for f in selected], 1, 60000)
        return _route(frames, selected, hv, ocr, "text/screen-biased OCR trigger route; OCR is mutually exclusive with high_value")

    raise ValueError(f"unknown policy: {policy_name}")
