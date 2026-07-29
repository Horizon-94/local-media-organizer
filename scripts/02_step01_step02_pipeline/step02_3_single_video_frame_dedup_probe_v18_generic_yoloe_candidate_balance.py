#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step02-3 single-video frame dedup probe v18.

Purpose:
  Probe one video's extracted JPG frames and choose a smaller set of visual
  representatives for YOLOE plus a stricter high-value subset.

Boundary:
  - Reads only the input frame directory.
  - Does not modify/delete/move source frames.
  - Writes only to --out.
  - This is a probe, not the formal Step02-3 pipeline.

Core change from v4/v6:
  - Does not use human labels such as "talking head".
  - Classifies scene dynamics from measurable frame differences:
      pixel_mad, changed_area_ratio, edge_mad, hist_l1, luma_diff.
  - Color histogram is an auxiliary signal, not a solo scene-cut trigger.
  - YOLOE representatives and high-value representatives are separated.

Core change in v18:
  - Keeps the V7 low-cost metric base but changes formal YOLOE selection to
    generic candidate balancing: time coverage, orientation-stage coverage,
    segment representatives, and stable replacements near change peaks.
  - Adds orientation_class/orientation_stage_id/local_stability_score.
  - Adds OCR/high-value pre-hints only; final OCR/high-value routing remains
    pending until YOLOE results exist.
  - Pairwise tournament is report-only and never affects selected_yoloe_frames.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat, ImageOps
except Exception:
    print("ERROR: Pillow is required. Install with: python3 -m pip install pillow", file=sys.stderr)
    raise SystemExit(2)

SCRIPT_VERSION = "step02_3_single_video_frame_dedup_probe_v18_generic_yoloe_candidate_balance_20260707"
POLICY_VERSION = "single_video_frame_dedup_probe_policy_v18_generic_yoloe_candidate_balance_prehint"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TIME_RE = re.compile(r"t(\d+)ms", re.IGNORECASE)
IDX_RE = re.compile(r"idx(\d+)", re.IGNORECASE)


@dataclass
class FrameMetric:
    index: int
    file_name: str
    source_path: str
    time_ms: int
    sha256_16: str
    luma_mean: float
    edge_mean: float
    entropy: float
    quality_score: float
    width: int = 0
    height: int = 0
    aspect_ratio: float = 0.0
    orientation_class: str = "unknown"
    orientation_stage_id: int = 0
    local_stability_score: float = 0.0
    local_representative_score: float = 0.0
    yoloe_candidate_reason: str = ""
    ocr_pre_hint: bool = False
    ocr_pre_hint_reason: str = ""
    high_value_pre_hint: bool = False
    high_value_pre_hint_reason: str = ""
    route_ocr_after_yoloe: str = "pending_yoloe"
    route_high_value_after_yoloe: str = "pending_yoloe"
    pairwise_report_only_round: str = ""
    pixel_mad_prev: float = 0.0
    changed_area_ratio_prev: float = 0.0
    edge_mad_prev: float = 0.0
    hist_l1_prev: float = 0.0
    luma_diff_prev: float = 0.0
    change_score_prev: float = 0.0
    change_class_prev: str = "first_frame"
    segment_id: int = 0
    kept_or_dropped: str = "dropped"
    route_yoloe: bool = False
    route_high_value: bool = False
    drop_reason: str = "not_selected"
    select_reason: str = ""
    selected_rank_yoloe: str = ""
    selected_rank_high_value: str = ""


@dataclass
class VideoDynamics:
    frame_count: int
    duration_seconds_estimated: float
    p50_pixel_mad: float
    p90_pixel_mad: float
    max_pixel_mad: float
    p90_changed_area_ratio: float
    max_changed_area_ratio: float
    p90_edge_mad: float
    max_edge_mad: float
    p90_hist_l1: float
    max_hist_l1: float
    p90_luma_diff: float
    max_luma_diff: float
    strong_change_count: int
    normal_change_count: int
    minor_motion_count: int
    near_duplicate_count: int
    segment_count: int
    scene_dynamics_class: str
    scene_dynamics_reason: str


def parse_time_ms(path: Path, fallback_index: int) -> int:
    m = TIME_RE.search(path.name)
    if m:
        return int(m.group(1))
    # C4 sampling is every 3000ms from 2000ms in this project. Use fallback only for probes.
    return 2000 + fallback_index * 3000


def sha256_16(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def pct(values: Sequence[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        return 0.0
    idx = int(round((len(xs) - 1) * q))
    return float(xs[idx])


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def image_entropy(gray: Image.Image) -> float:
    hist = gray.histogram()
    total = sum(hist) or 1
    e = 0.0
    for c in hist:
        if c:
            p = c / total
            e -= p * math.log2(p)
    return e / 8.0


def aggregate_hist_16(rgb: Image.Image) -> List[float]:
    # 16 bins per channel, concatenated. Normalized per channel then across channels.
    hist = rgb.histogram()
    out: List[float] = []
    for ch in range(3):
        h = hist[ch * 256:(ch + 1) * 256]
        total = sum(h) or 1
        for i in range(16):
            out.append(sum(h[i * 16:(i + 1) * 16]) / total)
    return out


def hist_l1(a: Sequence[float], b: Sequence[float]) -> float:
    # Average channel-normalized L1. Range roughly 0..2, but typical values lower.
    return sum(abs(x - y) for x, y in zip(a, b)) / 3.0


def orientation_class(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "unknown"
    if width > height * 1.15:
        return "landscape"
    if height > width * 1.15:
        return "portrait"
    return "square_or_uncertain"


def prepare_image(path: Path, size: Tuple[int, int] = (160, 90)) -> Tuple[Image.Image, Image.Image, Image.Image, List[float], float, float, float, float, int, int, float, str]:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    width, height = im.size
    aspect = round(width / max(1, height), 6)
    orient = orientation_class(width, height)
    im_small = im.resize(size, Image.Resampling.BILINEAR)
    gray = im_small.convert("L")
    edge = gray.filter(ImageFilter.FIND_EDGES)
    hist = aggregate_hist_16(im_small)
    luma_mean = ImageStat.Stat(gray).mean[0] / 255.0
    edge_mean = ImageStat.Stat(edge).mean[0] / 255.0
    entropy = image_entropy(gray)
    # Quality favors usable images, not a model score.
    brightness_score = 1.0 - min(abs(luma_mean - 0.50) / 0.50, 1.0)
    edge_score = clamp(edge_mean / 0.12)
    entropy_score = clamp(entropy / 0.85)
    quality = 0.25 * brightness_score + 0.35 * edge_score + 0.40 * entropy_score
    return im_small, gray, edge, hist, luma_mean, edge_mean, entropy, quality, width, height, aspect, orient


def diff_metrics(prev_rgb: Image.Image, rgb: Image.Image, prev_gray: Image.Image, gray: Image.Image, prev_edge: Image.Image, edge: Image.Image, prev_hist: Sequence[float], hist: Sequence[float], prev_luma: float, luma: float) -> Tuple[float, float, float, float, float, float, str]:
    rgb_diff = ImageChops.difference(prev_rgb, rgb)
    mad_channels = ImageStat.Stat(rgb_diff).mean[:3]
    pixel_mad = sum(mad_channels) / (3.0 * 255.0)

    gray_diff = ImageChops.difference(prev_gray, gray)
    gh = gray_diff.histogram()
    total = sum(gh) or 1
    # Area whose luma changed by at least 18/255. This catches real-area changes while ignoring tiny compression noise.
    changed_area_ratio = sum(gh[18:]) / total

    edge_diff = ImageChops.difference(prev_edge, edge)
    edge_mad = ImageStat.Stat(edge_diff).mean[0] / 255.0
    hdiff = hist_l1(prev_hist, hist)
    ldiff = abs(luma - prev_luma)

    # Hist is auxiliary. Strong score comes mostly from spatial pixel/edge change and changed area.
    change_score = (
        0.38 * clamp(pixel_mad / 0.080) +
        0.30 * clamp(changed_area_ratio / 0.220) +
        0.22 * clamp(edge_mad / 0.055) +
        0.07 * clamp(ldiff / 0.080) +
        0.03 * clamp(hdiff / 0.550)
    )

    # A strong change needs spatial evidence. Hist alone cannot make it strong.
    if ((pixel_mad >= 0.085 and changed_area_ratio >= 0.20) or
        (edge_mad >= 0.060 and changed_area_ratio >= 0.18) or
        (changed_area_ratio >= 0.32 and pixel_mad >= 0.060)):
        cls = "strong_change"
    elif (pixel_mad >= 0.052 or changed_area_ratio >= 0.13 or edge_mad >= 0.036):
        cls = "normal_change"
    elif (pixel_mad >= 0.018 or changed_area_ratio >= 0.035 or edge_mad >= 0.014 or hdiff >= 0.18):
        cls = "minor_motion"
    else:
        cls = "near_duplicate"
    return pixel_mad, changed_area_ratio, edge_mad, hdiff, ldiff, change_score, cls


def list_frames(input_dir: Path) -> List[Path]:
    frames = [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    frames.sort(key=lambda p: (parse_time_ms(p, 0), p.name))
    return frames


def compute_metrics(frames: Sequence[Path]) -> List[FrameMetric]:
    prepared = []
    metrics: List[FrameMetric] = []
    for i, p in enumerate(frames):
        rgb, gray, edge, hist, luma, edge_mean, entropy, quality, width, height, aspect, orient = prepare_image(p)
        fm = FrameMetric(
            index=i,
            file_name=p.name,
            source_path=str(p),
            time_ms=parse_time_ms(p, i),
            sha256_16=sha256_16(p),
            luma_mean=round(luma, 6),
            edge_mean=round(edge_mean, 6),
            entropy=round(entropy, 6),
            quality_score=round(quality, 6),
            width=width,
            height=height,
            aspect_ratio=aspect,
            orientation_class=orient,
        )
        if i > 0:
            prgb, pgray, pedge, phist, pluma = prepared[-1]
            pmad, car, emad, hdiff, ldiff, score, cls = diff_metrics(prgb, rgb, pgray, gray, pedge, edge, phist, hist, pluma, luma)
            fm.pixel_mad_prev = round(pmad, 6)
            fm.changed_area_ratio_prev = round(car, 6)
            fm.edge_mad_prev = round(emad, 6)
            fm.hist_l1_prev = round(hdiff, 6)
            fm.luma_diff_prev = round(ldiff, 6)
            fm.change_score_prev = round(score, 6)
            fm.change_class_prev = cls
        prepared.append((rgb, gray, edge, hist, luma))
        metrics.append(fm)
    enrich_v18_metrics(metrics)
    return metrics



def normalized_value(x: float, p10: float, p90: float) -> float:
    if p90 <= p10:
        return 0.5
    return clamp((x - p10) / (p90 - p10))


def assign_orientation_stages(metrics: Sequence[FrameMetric]) -> None:
    stage_id = 0
    prev = None
    for m in metrics:
        cur = m.orientation_class
        if prev is None:
            m.orientation_stage_id = stage_id
        else:
            if cur != prev and cur != "square_or_uncertain":
                stage_id += 1
            m.orientation_stage_id = stage_id
        prev = cur


def compute_local_stability(metrics: Sequence[FrameMetric]) -> None:
    vals = [m.change_score_prev for m in metrics[1:]]
    p90_change = max(0.0001, pct(vals, 0.90))
    max_change = max(vals or [0.0001])
    qs = [m.quality_score for m in metrics]
    q10, q90 = pct(qs, 0.10), pct(qs, 0.90)
    for i, m in enumerate(metrics):
        prev_change = m.change_score_prev if i > 0 else 0.0
        next_change = metrics[i + 1].change_score_prev if i + 1 < len(metrics) else 0.0
        local_max = max(prev_change, next_change)
        spike = abs(prev_change - next_change)
        # Stable representatives should not be the exact center of a violent one-frame spike.
        stability = 1.0 - clamp(0.55 * (local_max / max_change) + 0.45 * (spike / max(0.0001, p90_change)))
        rel_quality = normalized_value(m.quality_score, q10, q90)
        m.local_stability_score = round(stability, 6)
        m.local_representative_score = round(0.50 * frame_score(m) + 0.30 * stability + 0.20 * rel_quality, 6)


def compute_pre_hints(metrics: Sequence[FrameMetric]) -> None:
    edges = [m.edge_mean for m in metrics]
    ents = [m.entropy for m in metrics]
    p75_edge = pct(edges, 0.75)
    p75_entropy = pct(ents, 0.75)
    for m in metrics:
        text_like = m.edge_mean >= p75_edge and m.entropy >= p75_entropy and m.local_stability_score >= 0.25
        if text_like:
            m.ocr_pre_hint = True
            m.ocr_pre_hint_reason = "dense_edge_high_entropy_text_like_pre_hint_only"
        # Final high-value must wait for YOLOE. This is only a low-cost pre-hint.
        if not text_like and m.local_stability_score >= 0.35:
            m.high_value_pre_hint = True
            m.high_value_pre_hint_reason = "stable_non_ocr_like_pre_hint_only_pending_yoloe"


def enrich_v18_metrics(metrics: Sequence[FrameMetric]) -> None:
    assign_orientation_stages(metrics)
    compute_local_stability(metrics)
    compute_pre_hints(metrics)


def choose_local_representative(metrics: Sequence[FrameMetric], center: FrameMetric, window_ms: int = 6000) -> FrameMetric:
    band = [m for m in metrics if abs(m.time_ms - center.time_ms) <= window_ms]
    if not band:
        return center
    return max(band, key=lambda m: (m.local_representative_score, m.quality_score, -abs(m.time_ms - center.time_ms)))


def add_unique_candidate(pool: Dict[int, Tuple[FrameMetric, str]], m: FrameMetric, reason: str) -> None:
    old = pool.get(m.index)
    if old is None:
        pool[m.index] = (m, reason)
        return
    # Preserve accumulated reasons for auditability.
    reasons = set(filter(None, old[1].split("|")))
    reasons.add(reason)
    pool[m.index] = (m, "|".join(sorted(reasons)))


def choose_band_representatives(metrics: Sequence[FrameMetric], count: int, reason: str) -> List[Tuple[FrameMetric, str]]:
    if not metrics or count <= 0:
        return []
    t0, t1 = metrics[0].time_ms, metrics[-1].time_ms
    span = max(1, t1 - t0)
    out = []
    for b in range(count):
        a = t0 + span * b / count
        z = t0 + span * (b + 1) / count
        band = [m for m in metrics if a <= m.time_ms <= z]
        if band:
            out.append((max(band, key=lambda m: (m.local_representative_score, m.quality_score)), reason))
    return out


def choose_v18_balanced_yoloe(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args, max_yoloe: int, min_gap_ms: int) -> Tuple[List[FrameMetric], str]:
    if not metrics:
        return [], "no frames"
    max_yoloe = max(1, min(max_yoloe, len(metrics)))
    pool: Dict[int, Tuple[FrameMetric, str]] = {}

    # 1. Time coverage anchors: generic protection against one time span consuming all slots.
    time_quota = max(1, int(round(max_yoloe * 0.30)))
    for m, reason in choose_band_representatives(metrics, time_quota, "time_coverage_anchor"):
        add_unique_candidate(pool, m, reason)

    # 2. Orientation/stage anchors: handles portrait/landscape or other orientation-stage changes generically.
    by_stage: Dict[int, List[FrameMetric]] = {}
    for m in metrics:
        by_stage.setdefault(m.orientation_stage_id, []).append(m)
    for stage_id, group in sorted(by_stage.items()):
        best = max(group, key=lambda m: (m.local_representative_score, m.quality_score))
        add_unique_candidate(pool, best, f"orientation_stage_anchor:{stage_id}:{best.orientation_class}")

    # 3. Segment representatives from V7 dynamics, but use local representative score.
    by_seg: Dict[int, List[FrameMetric]] = {}
    for m in metrics:
        by_seg.setdefault(m.segment_id, []).append(m)
    seg_reps = [max(g, key=lambda m: (m.local_representative_score, m.quality_score)) for g in by_seg.values()]
    for m in sorted(seg_reps, key=lambda m: m.local_representative_score, reverse=True)[:max(1, int(round(max_yoloe * 0.35)))]:
        add_unique_candidate(pool, m, "segment_representative")

    # 4. Change peaks may enter only through stable local replacement.
    peak_quota = max(1, int(round(max_yoloe * 0.25)))
    peaks = [m for m in metrics[1:] if m.change_class_prev in {"normal_change", "strong_change"}]
    peaks.sort(key=lambda m: (m.change_score_prev, m.changed_area_ratio_prev, m.edge_mad_prev), reverse=True)
    for p in peaks[:max(peak_quota * 3, peak_quota)]:
        rep = choose_local_representative(metrics, p, window_ms=args.v18_peak_replacement_window_ms)
        add_unique_candidate(pool, rep, "local_stable_replacement_near_change_peak")
        if len(pool) >= max_yoloe * 2:
            break

    # Final ranking with gap control. First pass respects min gap, second pass fills if needed.
    candidates = [(m, reason) for m, reason in pool.values()]
    candidates.sort(key=lambda x: (x[0].local_representative_score, x[0].quality_score), reverse=True)
    selected: List[FrameMetric] = []
    reason_by_idx: Dict[int, str] = {}
    for m, reason in candidates:
        if len(selected) >= max_yoloe:
            break
        if all(abs(m.time_ms - x.time_ms) >= min_gap_ms for x in selected):
            selected.append(m)
            reason_by_idx[m.index] = reason
    if len(selected) < min(max_yoloe, len(candidates)):
        for m, reason in candidates:
            if len(selected) >= max_yoloe:
                break
            if m.index not in {x.index for x in selected}:
                selected.append(m)
                reason_by_idx[m.index] = reason + "|gap_fill"
    selected = sorted(selected, key=lambda m: m.time_ms)
    for m in selected:
        m.yoloe_candidate_reason = reason_by_idx.get(m.index, "v18_balanced_candidate")
    return selected, f"v18_generic_yoloe_candidate_balance max_yoloe={max_yoloe}; components=time/orientation_stage/segment/local_peak_replacement"


def select_v18_pre_high_value(selected: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> List[FrameMetric]:
    # This is pre-HV only. Final high-value must be selected after YOLOE output.
    if not selected:
        return []
    duration = max(1.0, dynamics.duration_seconds_estimated)
    if dynamics.scene_dynamics_class == "low_dynamics_stable_scene":
        max_hv = min(2, len(selected))
    else:
        max_hv = max(1, min(args.normal_max_high_value_absolute, int(math.ceil(duration / 180.0))))
    candidates = [m for m in selected if m.high_value_pre_hint and not m.ocr_pre_hint]
    if not candidates:
        candidates = list(selected)
    hv: List[FrameMetric] = []
    # Prefer coverage by orientation stage first, then representative score.
    by_stage: Dict[int, List[FrameMetric]] = {}
    for m in candidates:
        by_stage.setdefault(m.orientation_stage_id, []).append(m)
    for _, group in sorted(by_stage.items()):
        if len(hv) >= max_hv:
            break
        best = max(group, key=lambda m: (m.local_representative_score, m.quality_score))
        if all(abs(best.time_ms - x.time_ms) >= args.high_value_min_gap_ms for x in hv):
            hv.append(best)
    for m in sorted(candidates, key=lambda m: (m.local_representative_score, m.quality_score), reverse=True):
        if len(hv) >= max_hv:
            break
        if m.index not in {x.index for x in hv} and all(abs(m.time_ms - x.time_ms) >= args.high_value_min_gap_ms for x in hv):
            hv.append(m)
    return sorted(hv, key=lambda m: m.time_ms)


def pairwise_tournament_report_only(metrics: Sequence[FrameMetric], out_dir: Path) -> None:
    rows: List[Dict[str, object]] = []
    current = list(metrics)
    round_id = 1
    while len(current) > 1:
        next_round: List[FrameMetric] = []
        for i in range(0, len(current), 2):
            a = current[i]
            b = current[i + 1] if i + 1 < len(current) else None
            if b is None:
                winner = a
                loser = None
            else:
                winner = a if a.local_representative_score >= b.local_representative_score else b
                loser = b if winner is a else a
            next_round.append(winner)
            rows.append({
                "round": round_id,
                "a_index": a.index,
                "a_time_ms": a.time_ms,
                "a_score": a.local_representative_score,
                "b_index": b.index if b else "",
                "b_time_ms": b.time_ms if b else "",
                "b_score": b.local_representative_score if b else "",
                "winner_index": winner.index,
                "winner_time_ms": winner.time_ms,
                "loser_index": loser.index if loser else "",
                "mode": "report_only_not_used_for_selected_yoloe",
            })
        current = next_round
        round_id += 1
    write_csv(out_dir / "manifests" / "pairwise_tournament_report_only.csv", rows)


def classify_dynamics(metrics: Sequence[FrameMetric]) -> VideoDynamics:
    vals = list(metrics[1:])
    pixels = [m.pixel_mad_prev for m in vals]
    areas = [m.changed_area_ratio_prev for m in vals]
    edges = [m.edge_mad_prev for m in vals]
    hists = [m.hist_l1_prev for m in vals]
    lumas = [m.luma_diff_prev for m in vals]
    strong_count = sum(1 for m in vals if m.change_class_prev == "strong_change")
    normal_count = sum(1 for m in vals if m.change_class_prev == "normal_change")
    minor_count = sum(1 for m in vals if m.change_class_prev == "minor_motion")
    near_count = sum(1 for m in vals if m.change_class_prev == "near_duplicate")

    seg_id = 0
    for i, m in enumerate(metrics):
        if i == 0:
            m.segment_id = 0
            continue
        if m.change_class_prev == "strong_change":
            seg_id += 1
        m.segment_id = seg_id
    segment_count = seg_id + 1 if metrics else 0

    duration = 0.0
    if metrics:
        duration = max(0.0, (metrics[-1].time_ms - metrics[0].time_ms) / 1000.0)

    p90_pixel = pct(pixels, 0.90)
    p90_area = pct(areas, 0.90)
    p90_edge = pct(edges, 0.90)
    p90_hist = pct(hists, 0.90)
    p90_luma = pct(lumas, 0.90)
    strong_ratio = strong_count / max(1, len(vals))
    normal_ratio = normal_count / max(1, len(vals))

    # Low dynamics: stable spatial structure. Hist can be moderately high due to local color/face/hand/compression.
    if (p90_pixel < 0.045 and p90_area < 0.115 and p90_edge < 0.032 and p90_luma < 0.035 and strong_ratio <= 0.025):
        cls = "low_dynamics_stable_scene"
        reason = "low spatial/edge/luma movement and rare strong spatial changes"
    elif (strong_ratio >= 0.18 or p90_pixel >= 0.090 or p90_area >= 0.260 or p90_edge >= 0.070 or segment_count >= max(8, int(duration // 25))):
        cls = "high_dynamics_many_real_changes"
        reason = "frequent strong spatial changes or high p90 spatial dynamics"
    else:
        cls = "normal_dynamics_mixed_scene"
        reason = "some real movement or scene changes, but not high dynamics"

    return VideoDynamics(
        frame_count=len(metrics),
        duration_seconds_estimated=round(duration, 3),
        p50_pixel_mad=round(pct(pixels, 0.50), 6),
        p90_pixel_mad=round(p90_pixel, 6),
        max_pixel_mad=round(max(pixels or [0]), 6),
        p90_changed_area_ratio=round(p90_area, 6),
        max_changed_area_ratio=round(max(areas or [0]), 6),
        p90_edge_mad=round(p90_edge, 6),
        max_edge_mad=round(max(edges or [0]), 6),
        p90_hist_l1=round(p90_hist, 6),
        max_hist_l1=round(max(hists or [0]), 6),
        p90_luma_diff=round(p90_luma, 6),
        max_luma_diff=round(max(lumas or [0]), 6),
        strong_change_count=strong_count,
        normal_change_count=normal_count,
        minor_motion_count=minor_count,
        near_duplicate_count=near_count,
        segment_count=segment_count,
        scene_dynamics_class=cls,
        scene_dynamics_reason=reason,
    )


def frame_score(m: FrameMetric) -> float:
    # Representative score: quality plus change evidence. For low dynamics, quality dominates.
    return (0.62 * m.quality_score +
            0.16 * clamp(m.changed_area_ratio_prev / 0.18) +
            0.14 * clamp(m.edge_mad_prev / 0.05) +
            0.08 * clamp(m.pixel_mad_prev / 0.07))


def dedup_by_time(metrics: Sequence[FrameMetric], selected: List[FrameMetric], min_gap_ms: int) -> List[FrameMetric]:
    selected = sorted(selected, key=lambda m: (m.time_ms, m.index))
    out: List[FrameMetric] = []
    for m in selected:
        if all(abs(m.time_ms - x.time_ms) >= min_gap_ms for x in out):
            out.append(m)
        else:
            # Keep the better one if too close.
            for i, x in enumerate(out):
                if abs(m.time_ms - x.time_ms) < min_gap_ms and frame_score(m) > frame_score(x):
                    out[i] = m
                    break
    return sorted(out, key=lambda m: m.time_ms)


def choose_best(metrics: Sequence[FrameMetric], prefer_middle: bool = False) -> FrameMetric:
    if not metrics:
        raise ValueError("no frames")
    if not prefer_middle:
        return max(metrics, key=lambda m: (frame_score(m), m.quality_score, -abs(m.time_ms - metrics[len(metrics)//2].time_ms)))
    mid = (metrics[0].time_ms + metrics[-1].time_ms) / 2.0
    return max(metrics, key=lambda m: (frame_score(m) - 0.12 * min(abs(m.time_ms - mid) / max(1.0, mid), 1.0), m.quality_score))


def select_low_dynamics(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], List[FrameMetric], str]:
    # Low dynamics does not mean "human said talking head"; it means measured low spatial scene dynamics.
    duration = dynamics.duration_seconds_estimated
    if duration < args.low_dynamics_two_rep_seconds:
        target_yoloe = 1
    elif duration < args.low_dynamics_three_rep_seconds:
        target_yoloe = 2
    else:
        target_yoloe = 3
    target_yoloe = min(target_yoloe, args.low_dynamics_max_yoloe, len(metrics))

    chosen: List[FrameMetric] = []
    if target_yoloe == 1:
        chosen = [choose_best(metrics, prefer_middle=True)]
    else:
        # Divide the timeline into target_yoloe bands and choose best per band.
        t0, t1 = metrics[0].time_ms, metrics[-1].time_ms
        span = max(1, t1 - t0)
        for b in range(target_yoloe):
            a = t0 + span * b / target_yoloe
            z = t0 + span * (b + 1) / target_yoloe
            band = [m for m in metrics if a <= m.time_ms <= z]
            if band:
                chosen.append(choose_best(band, prefer_middle=True))
        # Ensure de-dup.
        uniq: Dict[int, FrameMetric] = {m.index: m for m in chosen}
        chosen = sorted(uniq.values(), key=lambda m: m.time_ms)

    hv = [choose_best(chosen, prefer_middle=True)] if chosen else []
    return chosen, hv, f"low_dynamics target_yoloe={target_yoloe}; high_value=best_one_from_yoloe"


def select_normal_dynamics(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], List[FrameMetric], str]:
    # Pick one per segment when strong changes exist, but also control by time gap.
    candidates: List[FrameMetric] = []
    by_seg: Dict[int, List[FrameMetric]] = {}
    for m in metrics:
        by_seg.setdefault(m.segment_id, []).append(m)
    for seg_frames in by_seg.values():
        candidates.append(choose_best(seg_frames, prefer_middle=True))

    # Add local peaks of normal changes, but not mechanical every 20s.
    peaks = sorted(metrics[1:], key=lambda m: (m.change_score_prev, m.changed_area_ratio_prev, m.edge_mad_prev), reverse=True)
    for m in peaks:
        if m.change_class_prev in {"normal_change", "strong_change"}:
            candidates.append(m)

    max_yoloe = max(1, min(args.normal_max_yoloe_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 60.0 * args.normal_max_yoloe_per_minute))))
    selected: List[FrameMetric] = []
    for m in sorted(candidates, key=lambda m: frame_score(m), reverse=True):
        if len(selected) >= max_yoloe:
            break
        if all(abs(m.time_ms - x.time_ms) >= args.normal_min_gap_ms for x in selected):
            selected.append(m)
    if not selected:
        selected = [choose_best(metrics, prefer_middle=True)]
    selected = sorted(selected, key=lambda m: m.time_ms)

    # High value is smaller than YOLOE; pick top by quality/change with wider gap.
    max_hv = max(1, min(args.normal_max_high_value_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 180.0))))
    hv: List[FrameMetric] = []
    for m in sorted(selected, key=lambda m: frame_score(m), reverse=True):
        if len(hv) >= max_hv:
            break
        if all(abs(m.time_ms - x.time_ms) >= args.high_value_min_gap_ms for x in hv):
            hv.append(m)
    hv = sorted(hv, key=lambda m: m.time_ms)
    return selected, hv, f"normal_dynamics max_yoloe={max_yoloe}; max_high_value={max_hv}; selected_by_segments_and_change_peaks"


def select_high_dynamics(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], List[FrameMetric], str]:
    by_seg: Dict[int, List[FrameMetric]] = {}
    for m in metrics:
        by_seg.setdefault(m.segment_id, []).append(m)
    candidates = [choose_best(v, prefer_middle=True) for v in by_seg.values()]
    peaks = sorted(metrics[1:], key=lambda m: (m.change_score_prev, m.changed_area_ratio_prev, m.edge_mad_prev), reverse=True)
    candidates.extend([m for m in peaks if m.change_class_prev == "strong_change"])
    max_yoloe = max(1, min(args.high_max_yoloe_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 60.0 * args.high_max_yoloe_per_minute))))
    selected: List[FrameMetric] = []
    for m in sorted(candidates, key=lambda m: frame_score(m), reverse=True):
        if len(selected) >= max_yoloe:
            break
        if all(abs(m.time_ms - x.time_ms) >= args.high_min_gap_ms for x in selected):
            selected.append(m)
    if not selected:
        selected = [choose_best(metrics, prefer_middle=True)]
    selected = sorted(selected, key=lambda m: m.time_ms)

    max_hv = max(1, min(args.high_max_high_value_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 120.0))))
    hv: List[FrameMetric] = []
    for m in sorted(selected, key=lambda m: frame_score(m), reverse=True):
        if len(hv) >= max_hv:
            break
        if all(abs(m.time_ms - x.time_ms) >= args.high_value_min_gap_ms for x in hv):
            hv.append(m)
    hv = sorted(hv, key=lambda m: m.time_ms)
    return selected, hv, f"high_dynamics max_yoloe={max_yoloe}; max_high_value={max_hv}; selected_by_strong_change_segments"


def select_frames(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], List[FrameMetric], str]:
    if not metrics:
        return [], [], "no frames"
    if dynamics.scene_dynamics_class == "low_dynamics_stable_scene":
        if dynamics.duration_seconds_estimated < args.low_dynamics_two_rep_seconds:
            max_yoloe = 1
        elif dynamics.duration_seconds_estimated < args.low_dynamics_three_rep_seconds:
            max_yoloe = 2
        else:
            max_yoloe = 3
        max_yoloe = min(max_yoloe, args.low_dynamics_max_yoloe, len(metrics))
        min_gap = max(args.normal_min_gap_ms, 30000)
    elif dynamics.scene_dynamics_class == "high_dynamics_many_real_changes":
        max_yoloe = max(1, min(args.high_max_yoloe_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 60.0 * args.high_max_yoloe_per_minute))))
        min_gap = args.high_min_gap_ms
    else:
        max_yoloe = max(1, min(args.normal_max_yoloe_absolute, int(math.ceil(dynamics.duration_seconds_estimated / 60.0 * args.normal_max_yoloe_per_minute))))
        min_gap = args.normal_min_gap_ms

    selected, reason = choose_v18_balanced_yoloe(metrics, dynamics, args, max_yoloe=max_yoloe, min_gap_ms=min_gap)
    hv = select_v18_pre_high_value(selected, dynamics, args)

    selected_ids = {m.index for m in selected}
    hv_ids = {m.index for m in hv}
    rank_y = {m.index: i + 1 for i, m in enumerate(selected)}
    rank_h = {m.index: i + 1 for i, m in enumerate(hv)}
    for m in metrics:
        m.route_ocr_after_yoloe = "pending_yoloe"
        m.route_high_value_after_yoloe = "pending_yoloe"
        if m.index in selected_ids:
            m.kept_or_dropped = "kept"
            m.route_yoloe = True
            m.drop_reason = ""
            m.select_reason = reason
            m.selected_rank_yoloe = str(rank_y[m.index])
            if not m.yoloe_candidate_reason:
                m.yoloe_candidate_reason = "v18_balanced_candidate"
        else:
            m.kept_or_dropped = "dropped"
            m.route_yoloe = False
            m.drop_reason = f"not_selected_by_{dynamics.scene_dynamics_class}_dedup_v18_generic_balance"
        if m.index in hv_ids:
            m.route_high_value = True
            m.selected_rank_high_value = str(rank_h[m.index])
            if not m.high_value_pre_hint_reason:
                m.high_value_pre_hint_reason = "pre_hv_selected_from_yoloe_candidates_pending_yoloe"
        else:
            m.route_high_value = False
    return selected, hv, reason


def safe_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def write_csv(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                fields.append(k)
                seen.add(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def make_thumbnails(metrics: Sequence[FrameMetric], out_dir: Path, width: int = 180) -> Dict[int, str]:
    thumb_dir = out_dir / "contact_sheet" / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    rels: Dict[int, str] = {}
    for m in metrics:
        src = Path(m.source_path)
        dst = thumb_dir / f"{m.index:04d}_{src.name}"
        try:
            im = Image.open(src)
            im = ImageOps.exif_transpose(im).convert("RGB")
            h = int(im.height * (width / max(1, im.width)))
            im = im.resize((width, max(1, h)), Image.Resampling.BILINEAR)
            im.save(dst, quality=85)
        except Exception:
            safe_copy(src, dst)
        rels[m.index] = os.path.relpath(dst, out_dir / "contact_sheet")
    return rels


def write_contact_sheet(out_dir: Path, metrics: Sequence[FrameMetric], dynamics: VideoDynamics, selected: Sequence[FrameMetric], hv: Sequence[FrameMetric]) -> None:
    cs = out_dir / "contact_sheet"
    cs.mkdir(parents=True, exist_ok=True)
    rels = make_thumbnails(metrics, out_dir)
    html_path = cs / "selection_contact_sheet.html"
    selected_ids = {m.index for m in selected}
    hv_ids = {m.index for m in hv}
    cards = []
    for m in metrics:
        if m.index in hv_ids:
            cls = "hv"
            badge = "HV+Y"
        elif m.index in selected_ids:
            cls = "yoloe"
            badge = "Y"
        else:
            cls = "drop"
            badge = "drop"
        cards.append(f"""
        <div class="card {cls}">
          <div class="badge">{badge}</div>
          <img src="{html.escape(rels.get(m.index, ''))}" />
          <div class="meta">
            <div><b>idx</b> {m.index} <b>t</b> {m.time_ms/1000:.1f}s</div>
            <div><b>change</b> {html.escape(m.change_class_prev)}</div>
            <div>mad={m.pixel_mad_prev:.4f} area={m.changed_area_ratio_prev:.3f} edge={m.edge_mad_prev:.4f}</div>
            <div>hist={m.hist_l1_prev:.3f} quality={m.quality_score:.3f}</div>
            <div>orient={html.escape(m.orientation_class)} stage={m.orientation_stage_id} stable={m.local_stability_score:.3f}</div>
            <div>reason={html.escape(m.yoloe_candidate_reason or m.select_reason or m.drop_reason)}</div>
            <div>ocr_pre={m.ocr_pre_hint} hv_pre={m.high_value_pre_hint}</div>
          </div>
        </div>
        """)
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Step02-3 v18 generic YOLOE candidate balance</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 18px; }}
.summary {{ padding: 12px; background:#f3f3f3; border-radius: 8px; margin-bottom: 16px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(205px, 1fr)); gap: 10px; }}
.card {{ border: 3px solid #aaa; padding: 6px; border-radius: 8px; background:#fff; }}
.card img {{ width: 100%; height: auto; display:block; }}
.card.hv {{ border-color: #d9480f; background: #fff4e6; }}
.card.yoloe {{ border-color: #2b8a3e; background: #ebfbee; }}
.card.drop {{ border-color: #adb5bd; opacity: 0.62; }}
.badge {{ font-weight: 700; margin-bottom: 4px; }}
.meta {{ font-size: 12px; line-height: 1.35; word-break: break-all; }}
</style></head><body>
<h1>Step02-3 v18 generic YOLOE candidate balance</h1>
<div class="summary">
<div><b>script_version</b>: {html.escape(SCRIPT_VERSION)}</div>
<div><b>policy_version</b>: {html.escape(POLICY_VERSION)}</div>
<div><b>scene_dynamics_class</b>: {html.escape(dynamics.scene_dynamics_class)}</div>
<div><b>reason</b>: {html.escape(dynamics.scene_dynamics_reason)}</div>
<div><b>frames</b>: {dynamics.frame_count}; <b>duration</b>: {dynamics.duration_seconds_estimated}s; <b>segments</b>: {dynamics.segment_count}</div>
<div><b>selected_yoloe</b>: {len(selected)}; <b>selected_high_value</b>: {len(hv)}; <b>dropped</b>: {len(metrics)-len(selected)}</div>
<div><b>p90</b>: pixel={dynamics.p90_pixel_mad}, area={dynamics.p90_changed_area_ratio}, edge={dynamics.p90_edge_mad}, hist={dynamics.p90_hist_l1}, luma={dynamics.p90_luma_diff}</div>
</div>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
    html_path.write_text(doc, encoding="utf-8")


def write_report(out_dir: Path, input_dir: Path, dynamics: VideoDynamics, selected: Sequence[FrameMetric], hv: Sequence[FrameMetric], reason: str) -> Dict[str, object]:
    final = out_dir / "final_report"
    final.mkdir(parents=True, exist_ok=True)
    summary = {
        "script_version": SCRIPT_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_frame_dir": str(input_dir),
        "output_dir": str(out_dir),
        "source_safety": "source_frames_read_only_no_write_no_move_no_delete_no_rename",
        **asdict(dynamics),
        "selected_yoloe_count": len(selected),
        "selected_high_value_count": len(hv),
        "dropped_count": dynamics.frame_count - len(selected),
        "selection_reason": reason,
        "routes": {
            "selected_yoloe_frames": str(out_dir / "selected_yoloe_frames"),
            "selected_high_value_frames": str(out_dir / "selected_high_value_frames"),
            "contact_sheet_html": str(out_dir / "contact_sheet" / "selection_contact_sheet.html"),
            "frame_candidate_manifest_csv": str(out_dir / "manifests" / "frame_candidate_manifest.csv"),
            "neighbor_diffs_csv": str(out_dir / "manifests" / "neighbor_diffs.csv"),
            "pairwise_tournament_report_only_csv": str(out_dir / "manifests" / "pairwise_tournament_report_only.csv"),
        },
        "v18_contract": {
            "ocr_final_routing": "pending_yoloe",
            "high_value_final_routing": "pending_yoloe",
            "selected_high_value_frames_folder_is": "pre_high_value_only_pending_yoloe",
            "pairwise_tournament": "report_only_not_used_for_selected_yoloe"
        }
    }
    (final / "step02_3_single_video_dedup_probe_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Step02-3 Single Video Frame Dedup Probe Report",
        "",
        f"- script_version: {SCRIPT_VERSION}",
        f"- policy_version: {POLICY_VERSION}",
        f"- input_frame_dir: `{input_dir}`",
        f"- output_dir: `{out_dir}`",
        "- source_safety: source_frames_read_only_no_write_no_move_no_delete_no_rename",
        f"- frame_count: {dynamics.frame_count}",
        f"- duration_seconds_estimated: {dynamics.duration_seconds_estimated}",
        f"- scene_dynamics_class: {dynamics.scene_dynamics_class}",
        f"- scene_dynamics_reason: {dynamics.scene_dynamics_reason}",
        f"- segment_count: {dynamics.segment_count}",
        f"- selected_yoloe_count: {len(selected)}",
        f"- selected_high_value_count: {len(hv)}",
        f"- dropped_count: {dynamics.frame_count - len(selected)}",
        f"- p90_pixel_mad: {dynamics.p90_pixel_mad}",
        f"- p90_changed_area_ratio: {dynamics.p90_changed_area_ratio}",
        f"- p90_edge_mad: {dynamics.p90_edge_mad}",
        f"- p90_hist_l1: {dynamics.p90_hist_l1}",
        f"- p90_luma_diff: {dynamics.p90_luma_diff}",
        "",
        "## Selection logic",
        "",
        "- Scene dynamics are measured from actual frames, not from a human label.",
        "- Pixel/edge/changed-area/luma decide real scene dynamics; color histogram is auxiliary only.",
        "- Low-dynamics long videos may keep 2-3 YOLOE representatives for temporal coverage, but high-value remains stricter.",
        "- V18 uses generic YOLOE candidate balancing: time coverage, orientation-stage coverage, segment representatives, and stable replacements near change peaks.",
        "- OCR/high-value are pre-hints only in this probe; final routing remains pending_yoloe.",
        "- Pairwise tournament, if enabled, is report-only and never affects selected_yoloe_frames.",
        "",
        "## Outputs",
        "",
        f"- contact_sheet_html: `{out_dir / 'contact_sheet' / 'selection_contact_sheet.html'}`",
        f"- selected_yoloe_frames: `{out_dir / 'selected_yoloe_frames'}`",
        f"- selected_high_value_frames: `{out_dir / 'selected_high_value_frames'}`",
        f"- frame_candidate_manifest: `{out_dir / 'manifests' / 'frame_candidate_manifest.csv'}`",
        f"- neighbor_diffs: `{out_dir / 'manifests' / 'neighbor_diffs.csv'}`",
    ]
    (final / "step02_3_single_video_dedup_probe_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return summary


def clean_output_dirs(out: Path) -> None:
    # Probe output only. Do not touch input frames.
    for sub in ["selected_yoloe_frames", "selected_high_value_frames", "contact_sheet", "manifests", "final_report"]:
        p = out / sub
        if p.exists():
            shutil.rmtree(p)
    out.mkdir(parents=True, exist_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Step02-3 single-video frame dedup probe v18")
    ap.add_argument("--input-frame-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--low-dynamics-two-rep-seconds", type=float, default=180.0)
    ap.add_argument("--low-dynamics-three-rep-seconds", type=float, default=420.0)
    ap.add_argument("--low-dynamics-max-yoloe", type=int, default=3)
    ap.add_argument("--normal-min-gap-ms", type=int, default=20000)
    ap.add_argument("--normal-max-yoloe-per-minute", type=float, default=2.0)
    ap.add_argument("--normal-max-yoloe-absolute", type=int, default=18)
    ap.add_argument("--normal-max-high-value-absolute", type=int, default=6)
    ap.add_argument("--high-min-gap-ms", type=int, default=9000)
    ap.add_argument("--high-max-yoloe-per-minute", type=float, default=5.0)
    ap.add_argument("--high-max-yoloe-absolute", type=int, default=80)
    ap.add_argument("--high-max-high-value-absolute", type=int, default=18)
    ap.add_argument("--high-value-min-gap-ms", type=int, default=60000)
    ap.add_argument("--v18-peak-replacement-window-ms", type=int, default=6000)
    args = ap.parse_args(argv)

    input_dir = Path(args.input_frame_dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"ERROR: input frame directory not found: {input_dir}", file=sys.stderr)
        return 2
    frames = list_frames(input_dir)
    if not frames:
        print(f"ERROR: no image frames found in {input_dir}", file=sys.stderr)
        return 2

    print(f"== {SCRIPT_VERSION} ==")
    print(f"input_frame_dir={input_dir}")
    print(f"out={out}")
    print(f"frames={len(frames)}")

    clean_output_dirs(out)
    metrics = compute_metrics(frames)
    dynamics = classify_dynamics(metrics)
    selected, hv, reason = select_frames(metrics, dynamics, args)

    manifests = out / "manifests"
    write_csv(manifests / "frame_candidate_manifest.csv", [asdict(m) for m in metrics])
    neigh_rows = [asdict(m) for m in metrics[1:]]
    write_csv(manifests / "neighbor_diffs.csv", neigh_rows)
    pairwise_tournament_report_only(metrics, out)
    seg_rows: List[Dict[str, object]] = []
    for sid in sorted({m.segment_id for m in metrics}):
        group = [m for m in metrics if m.segment_id == sid]
        seg_rows.append({
            "segment_id": sid,
            "start_time_ms": group[0].time_ms,
            "end_time_ms": group[-1].time_ms,
            "frame_count": len(group),
            "selected_yoloe_count": sum(1 for m in group if m.route_yoloe),
            "selected_high_value_count": sum(1 for m in group if m.route_high_value),
            "max_change_score_prev": max((m.change_score_prev for m in group), default=0),
        })
    write_csv(manifests / "dedup_segments.csv", seg_rows)

    ydir = out / "selected_yoloe_frames"
    hdir = out / "selected_high_value_frames"
    for i, m in enumerate(selected, 1):
        src = Path(m.source_path)
        safe_copy(src, ydir / f"yoloe_{i:03d}_idx{m.index:04d}_t{m.time_ms:09d}ms_{src.name}")
    for i, m in enumerate(hv, 1):
        src = Path(m.source_path)
        safe_copy(src, hdir / f"highvalue_{i:03d}_idx{m.index:04d}_t{m.time_ms:09d}ms_{src.name}")

    write_contact_sheet(out, metrics, dynamics, selected, hv)
    summary = write_report(out, input_dir, dynamics, selected, hv, reason)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("== probe finished ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
