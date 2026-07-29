#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step02-3 single-video frame dedup probe v7.

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

SCRIPT_VERSION = "step02_3_single_video_frame_dedup_probe_v7_cluster_representative_fix2_coverage_20260707"
POLICY_VERSION = "single_video_frame_dedup_probe_policy_v7_cluster_representative_plus_large_cluster_coverage"

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
    visual_cluster_id: int = -1
    cluster_start_time_ms: int = 0
    cluster_end_time_ms: int = 0
    cluster_size: int = 0
    cluster_role: str = ""
    cluster_select_score: float = 0.0


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


def prepare_image(path: Path, size: Tuple[int, int] = (160, 90)) -> Tuple[Image.Image, Image.Image, Image.Image, List[float], float, float, float, float]:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
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
    return im_small, gray, edge, hist, luma_mean, edge_mean, entropy, quality


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
        rgb, gray, edge, hist, luma, edge_mean, entropy, quality = prepare_image(p)
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
    return metrics


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
    """V7-compatible cheap visual score.

    This remains useful as one signal, but in this cluster version it no longer
    decides YOLOE by a per-minute quota. It is used only inside a visual cluster
    to choose a stable representative.
    """
    return (0.62 * m.quality_score +
            0.16 * clamp(m.changed_area_ratio_prev / 0.18) +
            0.14 * clamp(m.edge_mad_prev / 0.05) +
            0.08 * clamp(m.pixel_mad_prev / 0.07))


def local_instability(metrics: Sequence[FrameMetric], i: int) -> float:
    """Approximate whether frame i is a local motion/change peak.

    High values mean this frame sits on a movement peak or transition zone.
    Such frames can be useful for YOLOE only if they form a distinct cluster,
    but they should be less likely to become the cluster representative.
    """
    m = metrics[i]
    prev_score = m.change_score_prev if i > 0 else 0.0
    next_score = metrics[i + 1].change_score_prev if i + 1 < len(metrics) else 0.0
    prev_area = m.changed_area_ratio_prev if i > 0 else 0.0
    next_area = metrics[i + 1].changed_area_ratio_prev if i + 1 < len(metrics) else 0.0
    return clamp(0.55 * max(prev_score, next_score) + 0.45 * clamp(max(prev_area, next_area) / 0.26))


def cluster_rep_score(metrics: Sequence[FrameMetric], cluster: Sequence[FrameMetric], m: FrameMetric) -> float:
    """Representative score inside one visual cluster.

    Different from V7's peak picking: it favors a frame that is clear enough,
    not too close to the cluster boundary, and not sitting on a local motion peak.
    """
    if not cluster:
        return 0.0
    idx_to_pos = {x.index: pos for pos, x in enumerate(metrics)}
    i = idx_to_pos[m.index]
    t0, t1 = cluster[0].time_ms, cluster[-1].time_ms
    mid = (t0 + t1) / 2.0
    span = max(1.0, (t1 - t0) / 2.0)
    centrality = 1.0 - clamp(abs(m.time_ms - mid) / span)
    # boundary penalty: avoid first/last frame of a cluster unless cluster is tiny
    pos_in_cluster = list(cluster).index(m)
    if len(cluster) <= 2:
        boundary_ok = 0.7
    else:
        boundary_ok = min(pos_in_cluster, len(cluster)-1-pos_in_cluster) / max(1, (len(cluster)-1)/2)
    instability = local_instability(metrics, i)
    # Do not reward change peaks here. Representative means stable anchor.
    score = (0.56 * m.quality_score +
             0.18 * centrality +
             0.14 * boundary_ok +
             0.12 * clamp(m.edge_mean / 0.12) -
             0.26 * instability)
    return round(score, 6)


def should_split_cluster(prev: FrameMetric, cur: FrameMetric, current_cluster: Sequence[FrameMetric], args) -> bool:
    """Decide whether cur starts a new visual cluster.

    This is the new YOLOE core: cluster split is based on measured visual change,
    not a per-minute quota. A single normal_change does not automatically split;
    it needs enough spatial area/edge/pixel support. This is meant to keep local
    selfie motion from creating many false representatives.
    """
    if cur.change_class_prev == "strong_change":
        return True
    if cur.changed_area_ratio_prev >= args.cluster_split_area and cur.change_score_prev >= args.cluster_split_score:
        return True
    if cur.pixel_mad_prev >= args.cluster_split_pixel and cur.edge_mad_prev >= args.cluster_split_edge:
        return True
    # If a cluster has drifted for a long time, allow a coverage split, but only
    # when there is at least normal visual evidence at the current boundary. This
    # is not the old per-minute quota; it prevents one 8-minute cluster from
    # collapsing into a single YOLOE representative.
    if current_cluster:
        span_ms = cur.time_ms - current_cluster[0].time_ms
        if span_ms >= args.cluster_max_span_ms and cur.change_class_prev in {"normal_change", "strong_change"}:
            if cur.changed_area_ratio_prev >= args.cluster_coverage_area or cur.change_score_prev >= args.cluster_coverage_score:
                return True
    return False


def build_visual_clusters(metrics: Sequence[FrameMetric], args) -> List[List[FrameMetric]]:
    if not metrics:
        return []
    clusters: List[List[FrameMetric]] = [[metrics[0]]]
    for i in range(1, len(metrics)):
        cur = metrics[i]
        prev = metrics[i - 1]
        if should_split_cluster(prev, cur, clusters[-1], args):
            clusters.append([cur])
        else:
            clusters[-1].append(cur)
    for cid, cluster in enumerate(clusters):
        for m in cluster:
            m.visual_cluster_id = cid
            m.cluster_start_time_ms = cluster[0].time_ms
            m.cluster_end_time_ms = cluster[-1].time_ms
            m.cluster_size = len(cluster)
    return clusters


def choose_cluster_representative(metrics: Sequence[FrameMetric], cluster: Sequence[FrameMetric], role: str = "cluster_representative") -> FrameMetric:
    if not cluster:
        raise ValueError("empty cluster")
    # If the cluster is large enough, avoid first/last two frames because they are
    # often transition boundaries.
    pool = list(cluster)
    if len(pool) >= 5:
        pool = pool[2:-2]
    rep = max(pool, key=lambda m: (cluster_rep_score(metrics, cluster, m), m.quality_score, -local_instability(metrics, metrics.index(m))))
    rep.cluster_role = role
    rep.cluster_select_score = cluster_rep_score(metrics, cluster, rep)
    return rep


def cluster_needs_coverage(cluster: Sequence[FrameMetric], args) -> bool:
    if not cluster:
        return False
    span_ms = cluster[-1].time_ms - cluster[0].time_ms
    if len(cluster) < args.cluster_coverage_min_frames:
        return False
    if span_ms < args.cluster_internal_coverage_span_ms:
        return False
    max_area = max((m.changed_area_ratio_prev for m in cluster[1:]), default=0.0)
    max_score = max((m.change_score_prev for m in cluster[1:]), default=0.0)
    normal_count = sum(1 for m in cluster[1:] if m.change_class_prev in {"normal_change", "strong_change"})
    return (max_area >= args.cluster_internal_coverage_area or max_score >= args.cluster_internal_coverage_score or normal_count >= args.cluster_internal_coverage_normal_count)


def split_cluster_for_coverage(cluster: Sequence[FrameMetric], args) -> List[List[FrameMetric]]:
    """Split one large cluster into a few coverage windows.

    This is not the old per-minute main quota. The first-level cluster still
    defines duplicates. This only prevents a long, internally changing cluster
    from collapsing into one poor representative.
    """
    if not cluster_needs_coverage(cluster, args):
        return [list(cluster)]
    span_ms = max(1, cluster[-1].time_ms - cluster[0].time_ms)
    n = max(1, min(args.cluster_internal_max_reps, int(math.ceil(span_ms / args.cluster_internal_coverage_span_ms))))
    if n <= 1:
        return [list(cluster)]
    t0 = cluster[0].time_ms
    windows: List[List[FrameMetric]] = [[] for _ in range(n)]
    for m in cluster:
        pos = int((m.time_ms - t0) / span_ms * n)
        pos = max(0, min(n - 1, pos))
        windows[pos].append(m)
    return [w for w in windows if w]


def choose_representatives_for_cluster(metrics: Sequence[FrameMetric], cluster: Sequence[FrameMetric], args) -> List[FrameMetric]:
    windows = split_cluster_for_coverage(cluster, args)
    reps: List[FrameMetric] = []
    for wi, window in enumerate(windows):
        role = "cluster_representative" if len(windows) == 1 else f"cluster_coverage_representative_{wi+1}_of_{len(windows)}"
        rep = choose_cluster_representative(metrics, window, role=role)
        reps.append(rep)
    # If adjacent coverage windows chose very close frames, keep the stronger one.
    out: List[FrameMetric] = []
    for rep in sorted(reps, key=lambda m: m.time_ms):
        if out and rep.time_ms - out[-1].time_ms < args.cluster_internal_min_gap_ms:
            if (rep.cluster_select_score, rep.quality_score) > (out[-1].cluster_select_score, out[-1].quality_score):
                out[-1] = rep
        else:
            out.append(rep)
    return out


def select_yoloe_by_clusters(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], str]:
    clusters = build_visual_clusters(metrics, args)
    selected: List[FrameMetric] = []
    expanded_clusters = 0
    for c in clusters:
        reps = choose_representatives_for_cluster(metrics, c, args)
        if len(reps) > 1:
            expanded_clusters += 1
        selected.extend(reps)
    # Protect against pathological over-splitting. This is an emergency cap only;
    # report it explicitly instead of pretending it is a semantic rule.
    reason = f"cluster_representative clusters={len(clusters)} expanded_clusters={expanded_clusters}"
    if len(selected) > args.emergency_max_yoloe:
        ranked = sorted(selected, key=lambda m: (m.cluster_select_score, m.quality_score), reverse=True)
        selected = sorted(ranked[:args.emergency_max_yoloe], key=lambda m: m.time_ms)
        reason += f"; emergency_cap_applied={args.emergency_max_yoloe}"
    return sorted(selected, key=lambda m: m.time_ms), reason


def choose_high_values_from_clusters(selected: Sequence[FrameMetric], all_metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> List[FrameMetric]:
    """Select stricter high-value semantic anchors from YOLOE representatives.

    Budget remains a semantic-propagation budget: up to about 3/min, but for this
    single-video probe we prefer stage coverage over filling the budget. For a
    long selfie-like clip, default target is two anchors when possible: earlier
    and later stage. A candidate that is a local instability peak is penalized.
    """
    if not selected:
        return []
    if len(selected) == 1:
        return [selected[0]]
    t0, t1 = all_metrics[0].time_ms, all_metrics[-1].time_ms
    mid = (t0 + t1) / 2.0
    max_hv = max(1, min(args.high_value_absolute_cap, int(math.ceil(dynamics.duration_seconds_estimated / 60.0 * args.high_value_max_per_minute))))
    target = min(max_hv, 2 if dynamics.duration_seconds_estimated >= args.high_value_two_stage_min_seconds else 1, len(selected))

    idx_to_pos = {m.index: i for i, m in enumerate(all_metrics)}
    def hv_score(m: FrameMetric, center: float) -> float:
        i = idx_to_pos[m.index]
        stage_span = max(1.0, (t1 - t0) / 2.0)
        center_penalty = 0.10 * clamp(abs(m.time_ms - center) / stage_span)
        inst = local_instability(all_metrics, i)
        # cluster representative score already avoids peak frames; HV is stricter.
        return (0.62 * m.quality_score +
                0.24 * m.cluster_select_score +
                0.10 * clamp(m.edge_mean / 0.12) -
                0.30 * inst - center_penalty)

    if target == 1:
        return [max(selected, key=lambda m: hv_score(m, (t0+t1)/2.0))]

    early = [m for m in selected if m.time_ms <= mid]
    late = [m for m in selected if m.time_ms > mid]
    hv: List[FrameMetric] = []
    if early:
        hv.append(max(early, key=lambda m: hv_score(m, (t0+mid)/2.0)))
    if late:
        late_best = max(late, key=lambda m: hv_score(m, (mid+t1)/2.0))
        if not hv or abs(late_best.time_ms - hv[0].time_ms) >= args.high_value_min_gap_ms:
            hv.append(late_best)
    if len(hv) < target:
        remaining = [m for m in selected if m.index not in {x.index for x in hv}]
        for m in sorted(remaining, key=lambda x: hv_score(x, (t0+t1)/2.0), reverse=True):
            if all(abs(m.time_ms - x.time_ms) >= args.high_value_min_gap_ms for x in hv):
                hv.append(m)
            if len(hv) >= target:
                break
    return sorted(hv, key=lambda m: m.time_ms)


def select_frames(metrics: Sequence[FrameMetric], dynamics: VideoDynamics, args) -> Tuple[List[FrameMetric], List[FrameMetric], str]:
    if not metrics:
        return [], [], "no frames"
    selected, reason = select_yoloe_by_clusters(metrics, dynamics, args)
    hv = choose_high_values_from_clusters(selected, metrics, dynamics, args)

    selected_ids = {m.index for m in selected}
    hv_ids = {m.index for m in hv}
    rank_y = {m.index: i + 1 for i, m in enumerate(selected)}
    rank_h = {m.index: i + 1 for i, m in enumerate(hv)}
    for m in metrics:
        if m.index in selected_ids:
            m.kept_or_dropped = "kept"
            m.route_yoloe = True
            m.drop_reason = ""
            m.select_reason = reason
            m.selected_rank_yoloe = str(rank_y[m.index])
        else:
            m.kept_or_dropped = "dropped"
            m.route_yoloe = False
            m.drop_reason = f"not_cluster_representative cluster_id={m.visual_cluster_id}"
        if m.index in hv_ids:
            m.route_high_value = True
            m.selected_rank_high_value = str(rank_h[m.index])
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
            <div>{html.escape(m.select_reason or m.drop_reason)}</div>
          </div>
        </div>
        """)
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Step02-3 v7 single-video dedup probe</title>
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
<h1>Step02-3 v7 single-video dedup probe</h1>
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
        "- YOLOE first builds visual duplicate clusters, then allows limited sub-representatives inside long internally changing clusters.",
        "- High-value is no longer just the top-scoring frames; for long clips it tries to keep two timeline-stage anchors when possible.",
        "- This does not claim to detect human body orientation without a model; it is a practical coverage rule for selfie clips whose extracted JPGs may all be portrait-shaped.",
        "- OCR is not triggered in this probe; OCR waits for YOLOE text-like detections.",
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
    ap = argparse.ArgumentParser(description="Step02-3 single-video frame dedup probe v7")
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
    ap.add_argument("--high-value-two-stage-min-seconds", type=float, default=120.0)

    # V7-cluster representative core. These are the active controls for YOLOE
    # dedup; the older per-minute normal/high arguments above are retained only
    # for CLI compatibility and are not the main selection rule in this version.
    ap.add_argument("--cluster-split-area", type=float, default=0.32)
    ap.add_argument("--cluster-split-score", type=float, default=0.78)
    ap.add_argument("--cluster-split-edge", type=float, default=0.065)
    ap.add_argument("--cluster-split-pixel", type=float, default=0.060)
    ap.add_argument("--cluster-max-span-ms", type=int, default=60000)
    ap.add_argument("--cluster-coverage-area", type=float, default=0.22)
    ap.add_argument("--cluster-coverage-score", type=float, default=0.62)
    # Large-cluster coverage: keeps duplicate clustering as the main rule, but
    # allows a long internally changing cluster to contribute a few representatives.
    ap.add_argument("--cluster-internal-coverage-span-ms", type=int, default=45000)
    ap.add_argument("--cluster-internal-max-reps", type=int, default=4)
    ap.add_argument("--cluster-internal-min-gap-ms", type=int, default=15000)
    ap.add_argument("--cluster-coverage-min-frames", type=int, default=10)
    ap.add_argument("--cluster-internal-coverage-area", type=float, default=0.18)
    ap.add_argument("--cluster-internal-coverage-score", type=float, default=0.52)
    ap.add_argument("--cluster-internal-coverage-normal-count", type=int, default=6)
    ap.add_argument("--emergency-max-yoloe", type=int, default=48)
    ap.add_argument("--high-value-max-per-minute", type=float, default=3.0)
    ap.add_argument("--high-value-absolute-cap", type=int, default=24)
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
