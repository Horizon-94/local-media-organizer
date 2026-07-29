#!/usr/bin/env python3
"""
Step02-3 Low-cost Visual Descriptor Probe V20
=============================================

定位：本地素材大整理项目 Step02-3 的 descriptor-only 诊断脚本。

本脚本只读 Step02/C4 已抽出的 JPG 帧目录，输出每帧低成本视觉描述信号。
它不选择 YOLOE，不选择 high_value，不触发 OCR，不触发 Qwen-VL，不写入原始帧目录，
不覆盖 V7/V18 结果。用途是先验证：不用模型的低成本规则能否读出布局、稳定性、
画面质量、局部动作和潜在锚点。

安全规则：
- input-frame-dir 只读。
- 输出目录如果已存在且非空，默认拒绝写入；使用 --force 才允许清理输出目录。
- 不删除、不移动、不重命名输入帧。

依赖：Pillow + numpy

用法：
python3 step02_3_lowcost_visual_descriptor_probe_v20.py \
  --input-frame-dir /path/to/c4_frames_for_one_video \
  --out /path/to/output_v20
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit("缺少 numpy。请执行：python3 -m pip install numpy pillow") from exc

try:
    from PIL import Image, ImageFilter
except Exception as exc:  # pragma: no cover
    raise SystemExit("缺少 Pillow。请执行：python3 -m pip install numpy pillow") from exc

SCRIPT_VERSION = "step02_3_lowcost_visual_descriptor_probe_v20_descriptor_only_20260707"
POLICY_VERSION = "lowcost_visual_descriptor_probe_v20_no_selection_no_model_no_overwrite"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ANALYSIS_SIZE = (160, 90)  # w, h, same low-cost scale style as earlier probes
GRID3 = 3
GRID5 = 5


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step02-3 low-cost visual descriptor probe V20")
    p.add_argument("--input-frame-dir", required=True, help="C4 single-video frame directory")
    p.add_argument("--out", required=True, help="versioned output directory, e.g. ..._v20_lowcost_visual_descriptor")
    p.add_argument("--force", action="store_true", help="allow clearing an existing non-empty output directory")
    return p.parse_args()


def safe_prepare_output(out: Path, force: bool) -> Tuple[Path, Path, Path]:
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(
                f"[BLOCKED] 输出目录已存在且非空，默认不覆盖：{out}\n"
                f"如确认要重跑，请先换一个带时间戳的新目录，或显式加 --force。"
            )
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    manifests = out / "manifests"
    diagnostics = out / "diagnostics"
    contact = out / "contact_sheet"
    manifests.mkdir(parents=True, exist_ok=True)
    diagnostics.mkdir(parents=True, exist_ok=True)
    contact.mkdir(parents=True, exist_ok=True)
    (out / "final_report").mkdir(parents=True, exist_ok=True)
    return manifests, diagnostics, contact


def frame_sort_key(p: Path) -> Tuple[int, str]:
    # C4 names are usually frame_000001.jpg. Fall back to all numbers in filename.
    m = re.search(r"frame[_-]?(\d+)", p.stem, flags=re.I)
    if m:
        return int(m.group(1)), p.name
    nums = re.findall(r"\d+", p.stem)
    if nums:
        return int(nums[-1]), p.name
    return 10**12, p.name


def parse_frame_index(p: Path, fallback: int) -> int:
    k, _ = frame_sort_key(p)
    return fallback if k == 10**12 else k


def estimate_time_ms(frame_index: int) -> int:
    # C4 canonical: offset 1000ms, interval 2000ms.
    return 1000 + max(frame_index - 1, 0) * 2000


def load_image(path: Path) -> Tuple[np.ndarray, np.ndarray, Tuple[int, int]]:
    with Image.open(path) as im:
        im = im.convert("RGB")
        raw_w, raw_h = im.size
        small = im.resize(ANALYSIS_SIZE, Image.Resampling.BILINEAR)
        rgb = np.asarray(small).astype(np.float32) / 255.0
        gray_img = small.convert("L")
        gray = np.asarray(gray_img).astype(np.float32) / 255.0
    return rgb, gray, (raw_w, raw_h)


def aspect_class(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return "unknown"
    if w > h * 1.15:
        return "landscape"
    if h > w * 1.15:
        return "portrait"
    return "square_or_uncertain"


def normalize01(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def sobel_edges(gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Simple central-difference proxy; avoids scipy/opencv.
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    mag = np.sqrt(gx * gx + gy * gy)
    return gx, gy, mag


def entropy_gray(gray: np.ndarray) -> float:
    hist, _ = np.histogram(gray, bins=32, range=(0.0, 1.0), density=False)
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    p = hist.astype(np.float64) / total
    p = p[p > 0]
    ent = -float(np.sum(p * np.log2(p)))
    return ent / math.log2(32)


def grid_values(arr: np.ndarray, n: int) -> List[float]:
    h, w = arr.shape[:2]
    vals = []
    for iy in range(n):
        y0 = round(h * iy / n)
        y1 = round(h * (iy + 1) / n)
        for ix in range(n):
            x0 = round(w * ix / n)
            x1 = round(w * (ix + 1) / n)
            cell = arr[y0:y1, x0:x1]
            vals.append(float(cell.mean()) if cell.size else 0.0)
    return vals


def region_stats(gray: np.ndarray, edge: np.ndarray) -> Dict[str, float]:
    h, w = gray.shape
    cx0, cx1 = int(w * 0.25), int(w * 0.75)
    cy0, cy1 = int(h * 0.25), int(h * 0.75)
    center_g = gray[cy0:cy1, cx0:cx1]
    center_e = edge[cy0:cy1, cx0:cx1]
    mask = np.ones_like(gray, dtype=bool)
    mask[cy0:cy1, cx0:cx1] = False
    edge_g = gray[mask]
    edge_e = edge[mask]
    return {
        "center_luma_mean": float(center_g.mean()) if center_g.size else 0.0,
        "border_luma_mean": float(edge_g.mean()) if edge_g.size else 0.0,
        "center_edge_mean": float(center_e.mean()) if center_e.size else 0.0,
        "border_edge_mean": float(edge_e.mean()) if edge_e.size else 0.0,
        "center_vs_border_luma_diff": float(center_g.mean() - edge_g.mean()) if center_g.size and edge_g.size else 0.0,
        "center_vs_border_edge_diff": float(center_e.mean() - edge_e.mean()) if center_e.size and edge_e.size else 0.0,
    }


def content_bbox(gray: np.ndarray, edge: np.ndarray) -> Tuple[int, int, int, int, float, str, float, float]:
    # Generic content mask: brighter/darker departure from median + edge magnitude.
    med = float(np.median(gray))
    contrast_mask = np.abs(gray - med) > max(0.055, float(np.std(gray)) * 0.65)
    edge_thr = float(np.percentile(edge, 78)) if edge.size else 0.0
    edge_mask = edge > max(edge_thr, 0.025)
    mask = contrast_mask | edge_mask
    ys, xs = np.where(mask)
    h, w = gray.shape
    if len(xs) < 20 or len(ys) < 20:
        return 0, 0, w - 1, h - 1, 1.0, aspect_class(w, h), 0.5, 0.5
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw = max(1, x1 - x0 + 1)
    bh = max(1, y1 - y0 + 1)
    area_ratio = (bw * bh) / float(w * h)
    cls = aspect_class(bw, bh)
    # These are not model labels. They are only low-cost layout scores.
    portrait_like = normalize01((bh / max(bw, 1)), 1.05, 2.4)
    landscape_like = normalize01((bw / max(bh, 1)), 1.05, 2.4)
    return x0, y0, x1, y1, area_ratio, cls, portrait_like, landscape_like


def color_hist(rgb: np.ndarray) -> np.ndarray:
    # 8 bins per channel, flattened normalized histogram.
    hists = []
    for c in range(3):
        hist, _ = np.histogram(rgb[..., c], bins=8, range=(0.0, 1.0), density=False)
        hist = hist.astype(np.float64)
        s = hist.sum()
        hists.append(hist / s if s else hist)
    return np.concatenate(hists)


def calc_frame(path: Path, seq_idx: int) -> Dict[str, object]:
    rgb, gray, (raw_w, raw_h) = load_image(path)
    gx, gy, edge = sobel_edges(gray)
    x0, y0, x1, y1, bbox_area, content_cls, portrait_score, landscape_score = content_bbox(gray, edge)
    rstats = region_stats(gray, edge)

    luma_mean = float(gray.mean())
    luma_std = float(gray.std())
    dark_ratio = float((gray < 0.08).mean())
    bright_ratio = float((gray > 0.92).mean())
    edge_mean = float(edge.mean())
    edge_density = float((edge > max(0.035, float(np.percentile(edge, 80)))).mean())
    entropy = entropy_gray(gray)
    noise_proxy = float(np.mean(np.abs(gray - np.asarray(Image.fromarray((gray*255).astype(np.uint8)).filter(ImageFilter.MedianFilter(3))).astype(np.float32)/255.0)))

    horizontal_edge_energy = float(np.mean(np.abs(gy)))
    vertical_edge_energy = float(np.mean(np.abs(gx)))
    edge_orientation_ratio = horizontal_edge_energy / max(vertical_edge_energy, 1e-6)

    frame_index = parse_frame_index(path, seq_idx)
    row: Dict[str, object] = {
        "index": seq_idx,
        "frame_index": frame_index,
        "time_ms": estimate_time_ms(frame_index),
        "time_s": round(estimate_time_ms(frame_index) / 1000.0, 3),
        "file_name": path.name,
        "frame_file": str(path),
        "raw_width": raw_w,
        "raw_height": raw_h,
        "raw_aspect_ratio": round(raw_w / raw_h, 6) if raw_h else "",
        "raw_aspect_class": aspect_class(raw_w, raw_h),
        "content_bbox_x0": x0,
        "content_bbox_y0": y0,
        "content_bbox_x1": x1,
        "content_bbox_y1": y1,
        "content_bbox_area_ratio": round(bbox_area, 6),
        "content_aspect_ratio": round((x1 - x0 + 1) / max((y1 - y0 + 1), 1), 6),
        "content_aspect_class": content_cls,
        "portrait_like_score": round(portrait_score, 6),
        "landscape_like_score": round(landscape_score, 6),
        "luma_mean": round(luma_mean, 6),
        "luma_std_contrast": round(luma_std, 6),
        "dark_pixel_ratio": round(dark_ratio, 6),
        "bright_pixel_ratio": round(bright_ratio, 6),
        "edge_mean": round(edge_mean, 6),
        "edge_density": round(edge_density, 6),
        "entropy": round(entropy, 6),
        "noise_proxy": round(noise_proxy, 6),
        "horizontal_edge_energy": round(horizontal_edge_energy, 6),
        "vertical_edge_energy": round(vertical_edge_energy, 6),
        "edge_orientation_ratio": round(edge_orientation_ratio, 6),
        "grid3_luma": json.dumps([round(x, 6) for x in grid_values(gray, GRID3)]),
        "grid3_edge": json.dumps([round(x, 6) for x in grid_values(edge, GRID3)]),
        "grid5_luma": json.dumps([round(x, 6) for x in grid_values(gray, GRID5)]),
        "grid5_edge": json.dumps([round(x, 6) for x in grid_values(edge, GRID5)]),
        "hist_rgb_8x3": json.dumps([round(float(x), 6) for x in color_hist(rgb)]),
    }
    row.update({k: round(v, 6) for k, v in rstats.items()})
    # keep arrays for pairwise stage in private keys
    row["_gray"] = gray
    row["_edge"] = edge
    row["_hist"] = color_hist(rgb)
    row["_grid3_luma_arr"] = np.array(grid_values(gray, GRID3), dtype=np.float32)
    row["_grid3_edge_arr"] = np.array(grid_values(edge, GRID3), dtype=np.float32)
    return row


def add_neighbor_metrics(rows: List[Dict[str, object]]) -> None:
    for i, r in enumerate(rows):
        if i == 0:
            r.update({
                "pixel_mad_prev": 0.0,
                "edge_mad_prev": 0.0,
                "grid_luma_mad_prev": 0.0,
                "grid_edge_mad_prev": 0.0,
                "luma_diff_prev": 0.0,
                "hist_l1_prev": 0.0,
                "changed_area_ratio_prev": 0.0,
                "neighbor_change_score": 0.0,
            })
            continue
        p = rows[i - 1]
        gray = r["_gray"]
        pgray = p["_gray"]
        edge = r["_edge"]
        pedge = p["_edge"]
        diff = np.abs(gray - pgray)
        pixel_mad = float(diff.mean())
        edge_mad = float(np.abs(edge - pedge).mean())
        grid_luma_mad = float(np.abs(r["_grid3_luma_arr"] - p["_grid3_luma_arr"]).mean())
        grid_edge_mad = float(np.abs(r["_grid3_edge_arr"] - p["_grid3_edge_arr"]).mean())
        luma_diff = abs(float(r["luma_mean"]) - float(p["luma_mean"]))
        hist_l1 = float(np.abs(r["_hist"] - p["_hist"]).sum())
        changed = float((diff > 0.07).mean())
        score = (0.30 * pixel_mad) + (0.22 * edge_mad) + (0.18 * grid_luma_mad) + (0.12 * grid_edge_mad) + (0.10 * luma_diff) + (0.08 * min(hist_l1, 1.0))
        r.update({
            "pixel_mad_prev": round(pixel_mad, 6),
            "edge_mad_prev": round(edge_mad, 6),
            "grid_luma_mad_prev": round(grid_luma_mad, 6),
            "grid_edge_mad_prev": round(grid_edge_mad, 6),
            "luma_diff_prev": round(luma_diff, 6),
            "hist_l1_prev": round(hist_l1, 6),
            "changed_area_ratio_prev": round(changed, 6),
            "neighbor_change_score": round(score, 6),
        })


def add_derived_scores(rows: List[Dict[str, object]]) -> None:
    changes = [float(r["neighbor_change_score"]) for r in rows]
    edge_means = [float(r["edge_mean"]) for r in rows]
    entropies = [float(r["entropy"]) for r in rows]
    contrasts = [float(r["luma_std_contrast"]) for r in rows]
    noise = [float(r["noise_proxy"]) for r in rows]
    def pct(vals, p):
        return float(np.percentile(np.array(vals, dtype=np.float32), p)) if vals else 0.0
    q = {
        "change_p50": pct(changes, 50), "change_p90": pct(changes, 90),
        "edge_p10": pct(edge_means, 10), "edge_p90": pct(edge_means, 90),
        "entropy_p10": pct(entropies, 10), "entropy_p90": pct(entropies, 90),
        "contrast_p10": pct(contrasts, 10), "contrast_p90": pct(contrasts, 90),
        "noise_p10": pct(noise, 10), "noise_p90": pct(noise, 90),
    }
    for i, r in enumerate(rows):
        prev_change = float(rows[i - 1]["neighbor_change_score"]) if i > 0 else float(r["neighbor_change_score"])
        next_change = float(rows[i + 1]["neighbor_change_score"]) if i + 1 < len(rows) else float(r["neighbor_change_score"])
        cur_change = float(r["neighbor_change_score"])
        local_motion_peak = max(0.0, cur_change - max(prev_change, next_change))
        transition_tendency = normalize01(cur_change, q["change_p50"], q["change_p90"])
        # A stable anchor can still be in a changed area, but should not be a one-frame spike.
        local_stability = 1.0 - normalize01(abs(prev_change - next_change) + local_motion_peak, 0.0, max(q["change_p90"], 1e-6))
        local_stability = max(0.0, min(1.0, local_stability))
        quality = (
            0.28 * normalize01(float(r["edge_mean"]), q["edge_p10"], q["edge_p90"]) +
            0.25 * normalize01(float(r["entropy"]), q["entropy_p10"], q["entropy_p90"]) +
            0.22 * normalize01(float(r["luma_std_contrast"]), q["contrast_p10"], q["contrast_p90"]) +
            0.15 * (1.0 - normalize01(float(r["noise_proxy"]), q["noise_p10"], q["noise_p90"])) +
            0.10 * (1.0 - max(float(r["dark_pixel_ratio"]), float(r["bright_pixel_ratio"])))
        )
        # Anchor score is diagnostic only. It must not select YOLOE/high-value.
        anchor = 0.45 * quality + 0.25 * local_stability + 0.20 * transition_tendency + 0.10 * max(float(r["portrait_like_score"]), float(r["landscape_like_score"]))
        r.update({
            "local_motion_peak_score": round(local_motion_peak, 6),
            "transition_frame_tendency": round(transition_tendency, 6),
            "stability_score": round(local_stability, 6),
            "quality_score_relative": round(max(0.0, min(1.0, quality)), 6),
            "anchor_candidate_score": round(max(0.0, min(1.0, anchor)), 6),
        })


def dominant_layout(row: Dict[str, object]) -> str:
    p = float(row["portrait_like_score"])
    l = float(row["landscape_like_score"])
    c = str(row["content_aspect_class"])
    if p >= 0.55 and p >= l + 0.12:
        return "portrait_like_content"
    if l >= 0.55 and l >= p + 0.12:
        return "landscape_like_content"
    if c in {"portrait", "landscape"}:
        return f"{c}_content_weak"
    return "uncertain_content_layout"


def add_layout_classes(rows: List[Dict[str, object]]) -> None:
    prev_raw = prev_content = prev_dom = None
    raw_trans = content_trans = dom_trans = 0
    for r in rows:
        r["dominant_layout_class"] = dominant_layout(r)
        raw = str(r["raw_aspect_class"])
        content = str(r["content_aspect_class"])
        dom = str(r["dominant_layout_class"])
        if prev_raw is not None and raw != prev_raw:
            raw_trans += 1
        if prev_content is not None and content != prev_content:
            content_trans += 1
        if prev_dom is not None and dom != prev_dom:
            dom_trans += 1
        r["raw_layout_transition_index"] = raw_trans
        r["content_layout_transition_index"] = content_trans
        r["dominant_layout_transition_index"] = dom_trans
        prev_raw, prev_content, prev_dom = raw, content, dom


def strip_private(row: Dict[str, object]) -> Dict[str, object]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    clean = [strip_private(r) for r in rows]
    fieldnames = list(clean[0].keys()) if clean else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(clean)


def transition_count(rows: List[Dict[str, object]], key: str) -> int:
    n = 0
    prev = None
    for r in rows:
        cur = r.get(key)
        if prev is not None and cur != prev:
            n += 1
        prev = cur
    return n


def write_anchor_csv(path: Path, rows: List[Dict[str, object]], limit: int = 30) -> None:
    ranked = sorted(rows, key=lambda r: float(r["anchor_candidate_score"]), reverse=True)[:limit]
    cols = [
        "index", "time_s", "file_name", "raw_aspect_class", "content_aspect_class", "dominant_layout_class",
        "portrait_like_score", "landscape_like_score", "quality_score_relative", "stability_score",
        "transition_frame_tendency", "local_motion_peak_score", "anchor_candidate_score", "frame_file"
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in ranked:
            writer.writerow({c: r.get(c, "") for c in cols})


def write_contact_sheet(path: Path, rows: List[Dict[str, object]], title: str) -> None:
    cells = []
    for r in rows:
        fp = Path(str(r["frame_file"])).resolve()
        uri = fp.as_uri()
        label = (
            f"#{r['index']} | {r['time_s']}s<br>"
            f"raw={html.escape(str(r['raw_aspect_class']))}<br>"
            f"content={html.escape(str(r['content_aspect_class']))}<br>"
            f"dom={html.escape(str(r['dominant_layout_class']))}<br>"
            f"p={r['portrait_like_score']} l={r['landscape_like_score']}<br>"
            f"q={r['quality_score_relative']} st={r['stability_score']} a={r['anchor_candidate_score']}"
        )
        cells.append(f"<div class='card'><img src='{uri}'><div class='meta'>{label}</div></div>")
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;margin:20px;background:#111;color:#eee}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
.card{{background:#222;border:1px solid #444;padding:8px;border-radius:8px}}
.card img{{width:100%;height:auto;display:block;background:#000}}
.meta{{font-size:12px;line-height:1.35;margin-top:6px;color:#ddd;word-break:break-word}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>Descriptor-only V20. No YOLOE selection, no high-value selection, no OCR.</p>
<div class="grid">{''.join(cells)}</div>
</body></html>"""
    path.write_text(doc, encoding="utf-8")


def write_report(path: Path, rows: List[Dict[str, object]], input_dir: Path, out: Path, elapsed: float) -> Dict[str, object]:
    summary = {
        "script_version": SCRIPT_VERSION,
        "policy_version": POLICY_VERSION,
        "input_frame_dir": str(input_dir),
        "output_dir": str(out),
        "source_safety": "input_frames_read_only_no_write_no_move_no_delete_no_rename",
        "frame_count": len(rows),
        "duration_seconds_estimated": float(rows[-1]["time_s"]) if rows else 0.0,
        "raw_aspect_class_counts": dict(Counter(str(r["raw_aspect_class"]) for r in rows)),
        "content_aspect_class_counts": dict(Counter(str(r["content_aspect_class"]) for r in rows)),
        "dominant_layout_class_counts": dict(Counter(str(r["dominant_layout_class"]) for r in rows)),
        "portrait_like_count_ge_0_55": sum(1 for r in rows if float(r["portrait_like_score"]) >= 0.55),
        "landscape_like_count_ge_0_55": sum(1 for r in rows if float(r["landscape_like_score"]) >= 0.55),
        "raw_layout_transition_count": transition_count(rows, "raw_aspect_class"),
        "content_layout_transition_count": transition_count(rows, "content_aspect_class"),
        "dominant_layout_transition_count": transition_count(rows, "dominant_layout_class"),
        "quality_score_relative_p50": round(float(np.percentile([float(r["quality_score_relative"]) for r in rows], 50)), 6) if rows else 0.0,
        "stability_score_p50": round(float(np.percentile([float(r["stability_score"]) for r in rows], 50)), 6) if rows else 0.0,
        "anchor_candidate_score_p90": round(float(np.percentile([float(r["anchor_candidate_score"]) for r in rows], 90)), 6) if rows else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "outputs": {
            "frame_lowcost_descriptor_csv": str(out / "manifests" / "frame_lowcost_descriptor.csv"),
            "potential_layout_anchors_csv": str(out / "diagnostics" / "potential_layout_anchors.csv"),
            "contact_sheet_html": str(out / "contact_sheet" / "lowcost_descriptor_contact_sheet.html"),
        },
    }
    md = [
        "# Step02-3 Low-cost Visual Descriptor Probe V20",
        "",
        f"- script_version: {SCRIPT_VERSION}",
        f"- policy_version: {POLICY_VERSION}",
        f"- input_frame_dir: `{input_dir}`",
        f"- output_dir: `{out}`",
        "- source_safety: input_frames_read_only_no_write_no_move_no_delete_no_rename",
        f"- frame_count: {summary['frame_count']}",
        f"- duration_seconds_estimated: {summary['duration_seconds_estimated']}",
        "",
        "## Layout diagnostics",
        "",
        f"- raw_aspect_class_counts: {summary['raw_aspect_class_counts']}",
        f"- content_aspect_class_counts: {summary['content_aspect_class_counts']}",
        f"- dominant_layout_class_counts: {summary['dominant_layout_class_counts']}",
        f"- portrait_like_count_ge_0_55: {summary['portrait_like_count_ge_0_55']}",
        f"- landscape_like_count_ge_0_55: {summary['landscape_like_count_ge_0_55']}",
        f"- raw_layout_transition_count: {summary['raw_layout_transition_count']}",
        f"- content_layout_transition_count: {summary['content_layout_transition_count']}",
        f"- dominant_layout_transition_count: {summary['dominant_layout_transition_count']}",
        "",
        "## Quality / stability diagnostics",
        "",
        f"- quality_score_relative_p50: {summary['quality_score_relative_p50']}",
        f"- stability_score_p50: {summary['stability_score_p50']}",
        f"- anchor_candidate_score_p90: {summary['anchor_candidate_score_p90']}",
        "",
        "## Interpretation rule",
        "",
        "- This script does not select YOLOE frames.",
        "- This script does not select high-value frames.",
        "- If portrait_like_count_ge_0_55 and landscape_like_count_ge_0_55 are both positive, low-cost layout signals may support stage-aware selection.",
        "- If one side is zero, do not keep tuning selection rules blindly; model evidence or stronger layout descriptors are needed.",
        "",
        "## Outputs",
        "",
        f"- frame_lowcost_descriptor_csv: `{summary['outputs']['frame_lowcost_descriptor_csv']}`",
        f"- potential_layout_anchors_csv: `{summary['outputs']['potential_layout_anchors_csv']}`",
        f"- contact_sheet_html: `{summary['outputs']['contact_sheet_html']}`",
    ]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    (path.parent.parent / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_frame_dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"[ERROR] input-frame-dir 不存在或不是目录：{input_dir}")
    manifests, diagnostics, contact = safe_prepare_output(out, args.force)
    started = time.perf_counter()

    files = sorted([p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS], key=frame_sort_key)
    if not files:
        raise SystemExit(f"[ERROR] 未找到可读图片帧：{input_dir}")

    print("== Step02-3 low-cost visual descriptor V20 start ==")
    print(f"input_frame_dir: {input_dir}")
    print(f"output_dir: {out}")
    print(f"frame_count: {len(files)}")
    print("mode: descriptor_only_no_yoloe_no_high_value_no_ocr")

    rows: List[Dict[str, object]] = []
    for i, fp in enumerate(files, start=1):
        try:
            rows.append(calc_frame(fp, i))
        except Exception as exc:
            print(f"[WARN] failed frame: {fp.name} err={exc}", file=sys.stderr)
    if not rows:
        raise SystemExit("[ERROR] 所有帧读取失败。")

    add_neighbor_metrics(rows)
    add_derived_scores(rows)
    add_layout_classes(rows)

    write_csv(manifests / "frame_lowcost_descriptor.csv", rows)
    write_anchor_csv(diagnostics / "potential_layout_anchors.csv", rows, limit=40)
    write_contact_sheet(contact / "lowcost_descriptor_contact_sheet.html", rows, "Step02-3 Low-cost Visual Descriptor V20")
    elapsed = time.perf_counter() - started
    summary = write_report(out / "final_report" / "lowcost_visual_descriptor_report.md", rows, input_dir, out, elapsed)

    print("== Step02-3 low-cost visual descriptor V20 finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
