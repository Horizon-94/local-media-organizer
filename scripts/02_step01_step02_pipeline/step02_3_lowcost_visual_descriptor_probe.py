#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step02-3 low-cost visual descriptor probe v3 image evidence.
No model. No YOLOE/high-value routing. No numpy. Pillow only.
Purpose:
  1) read all extracted JPG frames with cheap image metrics;
  2) copy actual frame images into review folders so humans see pictures, not CSV only;
  3) print terminal counts and paths;
  4) expose whether raw/content layout signals can distinguish portrait/landscape.
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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageFilter, ImageOps, ImageStat
except Exception as e:
    print("ERROR: Pillow is required. Install with: python3 -m pip install pillow")
    raise

SCRIPT_VERSION = "step02_3_lowcost_visual_descriptor_probe_v3_image_evidence_20260707"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:180]


def parse_time_ms(path: Path, idx: int) -> int:
    name = path.stem
    # common frame names may include 0049000ms, t049000, time_ms_49000, etc.
    pats = [r"(\d{4,9})\s*ms", r"time[_-]?ms[_-]?(\d{4,9})", r"t[_-]?(\d{4,9})", r"_(\d{4,9})_"]
    for p in pats:
        m = re.search(p, name, flags=re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return idx * 3000


def aspect_class(w: int, h: int) -> str:
    if h <= 0:
        return "unknown"
    r = w / h
    if r >= 1.18:
        return "landscape"
    if r <= 0.85:
        return "portrait"
    return "square_or_near"


def resize_fit(im: Image.Image, size: int = 160) -> Image.Image:
    im = im.convert("RGB")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    bg = Image.new("RGB", (size, size), (0, 0, 0))
    x = (size - im.width) // 2
    y = (size - im.height) // 2
    bg.paste(im, (x, y))
    return bg


def crop_grid(im: Image.Image, rows: int, cols: int) -> List[Image.Image]:
    w, h = im.size
    out = []
    for r in range(rows):
        for c in range(cols):
            x0 = int(c * w / cols)
            x1 = int((c + 1) * w / cols)
            y0 = int(r * h / rows)
            y1 = int((r + 1) * h / rows)
            out.append(im.crop((x0, y0, x1, y1)))
    return out


def mean_gray(im: Image.Image) -> float:
    return ImageStat.Stat(im.convert("L")).mean[0] / 255.0


def std_gray(im: Image.Image) -> float:
    return ImageStat.Stat(im.convert("L")).stddev[0] / 128.0


def mean_saturation(im: Image.Image) -> float:
    hsv = im.convert("HSV")
    return ImageStat.Stat(hsv.getchannel(1)).mean[0] / 255.0


def edge_image(gray: Image.Image) -> Image.Image:
    return gray.filter(ImageFilter.FIND_EDGES)


def sharpness_proxy(gray: Image.Image) -> float:
    # Low-cost edge strength proxy, not true Laplacian variance.
    e = edge_image(gray)
    return min(1.0, ImageStat.Stat(e).mean[0] / 48.0)


def edge_density(gray: Image.Image) -> float:
    e = edge_image(gray)
    hist = e.histogram()
    total = sum(hist) or 1
    strong = sum(hist[32:])
    return strong / total


def under_over_ratio(gray: Image.Image) -> Tuple[float, float]:
    hist = gray.histogram()
    total = sum(hist) or 1
    under = sum(hist[:28]) / total
    over = sum(hist[235:]) / total
    return under, over


def absdiff_mean(a: Image.Image, b: Image.Image) -> float:
    a = a.convert("L")
    b = b.convert("L")
    # Pillow-only mean abs diff
    pa = a.load(); pb = b.load()
    w, h = a.size
    s = 0
    for y in range(h):
        for x in range(w):
            s += abs(pa[x, y] - pb[x, y])
    return s / (w * h * 255.0)


def changed_area_ratio(a: Image.Image, b: Image.Image, threshold: int = 24) -> float:
    a = a.convert("L")
    b = b.convert("L")
    pa = a.load(); pb = b.load()
    w, h = a.size
    n = 0
    for y in range(h):
        for x in range(w):
            if abs(pa[x, y] - pb[x, y]) >= threshold:
                n += 1
    return n / (w * h)


def hist_l1(a: Image.Image, b: Image.Image) -> float:
    ha = a.convert("RGB").histogram()
    hb = b.convert("RGB").histogram()
    denom = sum(ha) + sum(hb) or 1
    return sum(abs(x - y) for x, y in zip(ha, hb)) / denom


def grid_means(gray: Image.Image, rows: int = 3, cols: int = 3) -> List[float]:
    return [mean_gray(c) for c in crop_grid(gray, rows, cols)]


def vector_l1(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def nonblack_bbox(gray: Image.Image) -> Tuple[int, int, int, int, float, str, float, float]:
    # Detect letterbox/content bbox based on pixels brighter than very dark.
    # If whole frame is low light, fallback to full frame to avoid false empty bbox.
    w, h = gray.size
    pix = gray.load()
    xs: List[int] = []
    ys: List[int] = []
    for y in range(h):
        for x in range(w):
            if pix[x, y] > 12:
                xs.append(x); ys.append(y)
    if len(xs) < max(50, int(w * h * 0.02)):
        return 0, 0, w, h, w / h if h else 0.0, aspect_class(w, h), 1.0, 1.0
    x0, x1 = min(xs), max(xs) + 1
    y0, y1 = min(ys), max(ys) + 1
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    area_ratio = (bw * bh) / (w * h)
    return x0, y0, x1, y1, bw / bh, aspect_class(bw, bh), bw / w, bh / h


def dominant_orientation(gray: Image.Image) -> str:
    # Cheap gradient orientation proxy using shifted differences on small image.
    im = gray.resize((80, 80), Image.Resampling.BILINEAR)
    p = im.load(); w, h = im.size
    vx = 0; vy = 0; diag = 0
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            dx = abs(p[x + 1, y] - p[x - 1, y])
            dy = abs(p[x, y + 1] - p[x, y - 1])
            if dx > dy * 1.25:
                vx += dx
            elif dy > dx * 1.25:
                vy += dy
            else:
                diag += max(dx, dy)
    if vx > vy * 1.2 and vx > diag * 0.8:
        return "vertical_edges"
    if vy > vx * 1.2 and vy > diag * 0.8:
        return "horizontal_edges"
    return "mixed_edges"


@dataclass
class Row:
    index: int
    time_ms: int
    file_name: str
    file_path: str
    width: int
    height: int
    raw_aspect: float
    raw_aspect_class: str
    bbox_x0: int
    bbox_y0: int
    bbox_x1: int
    bbox_y1: int
    content_aspect: float
    content_aspect_class: str
    content_width_ratio: float
    content_height_ratio: float
    mean_luma: float
    contrast: float
    saturation: float
    underexposed_ratio: float
    overexposed_ratio: float
    sharpness_score: float
    edge_density: float
    dominant_edge_orientation: str
    quality_score: float
    pixel_mad_prev: float
    changed_area_prev: float
    edge_mad_prev: float
    grid_luma_diff_prev: float
    hist_l1_prev: float
    luma_diff_prev: float
    transition_like_score: float
    stability_score: float
    anchor_score: float
    evidence_bucket: str


def calc_rows(paths: List[Path]) -> List[Row]:
    rows: List[Row] = []
    prev_small: Optional[Image.Image] = None
    prev_edge: Optional[Image.Image] = None
    prev_grid: Optional[List[float]] = None
    prev_rgb: Optional[Image.Image] = None
    prev_luma: Optional[float] = None

    smalls: List[Image.Image] = []
    edges: List[Image.Image] = []
    grids: List[List[float]] = []
    rgbs: List[Image.Image] = []

    for idx, p in enumerate(paths):
        with Image.open(p) as im0:
            im0 = ImageOps.exif_transpose(im0).convert("RGB")
            w, h = im0.size
            small = resize_fit(im0, 160)
            gray = small.convert("L")
            e = edge_image(gray)
            grid = grid_means(gray, 3, 3)
            rgbs.append(small)
            smalls.append(gray)
            edges.append(e)
            grids.append(grid)

            mean_l = mean_gray(gray)
            contrast = min(1.0, std_gray(gray))
            sat = mean_saturation(small)
            under, over = under_over_ratio(gray)
            sharp = sharpness_proxy(gray)
            edge_d = edge_density(gray)
            dom = dominant_orientation(gray)
            x0, y0, x1, y1, car, cac, cwr, chr_ = nonblack_bbox(gray)

            # Quality is intentionally simple and visible, not tuned for beauty.
            q = 0.0
            q += 0.28 * min(1.0, sharp / 0.35)
            q += 0.22 * min(1.0, contrast / 0.55)
            q += 0.15 * min(1.0, sat / 0.45)
            q += 0.20 * max(0.0, 1.0 - under / 0.85)
            q += 0.10 * max(0.0, 1.0 - over / 0.25)
            q += 0.05 * min(1.0, edge_d / 0.35)
            q = round(max(0.0, min(1.0, q)), 6)

            if prev_small is None:
                pix = area = ediff = gdiff = hd = ldiff = 0.0
            else:
                pix = absdiff_mean(gray, prev_small)
                area = changed_area_ratio(gray, prev_small)
                ediff = absdiff_mean(e, prev_edge) if prev_edge else 0.0
                gdiff = vector_l1(grid, prev_grid or [])
                hd = hist_l1(small, prev_rgb) if prev_rgb else 0.0
                ldiff = abs(mean_l - (prev_luma or mean_l))

            trans = max(pix * 2.5, area * 1.6, ediff * 2.0, gdiff * 3.0, ldiff * 3.0)
            trans = round(min(1.0, trans), 6)
            stability = round(max(0.0, 1.0 - trans), 6)
            anchor = round((0.58 * q) + (0.42 * stability), 6)

            if trans >= 0.38:
                bucket = "strong_change_candidate"
            elif q >= 0.48 and stability >= 0.78:
                bucket = "stable_quality_anchor_candidate"
            elif q < 0.30:
                bucket = "low_quality_or_underexposed"
            elif stability < 0.65:
                bucket = "unstable_or_transition_like"
            else:
                bucket = "ordinary_repeated_frame"

            rows.append(Row(
                index=idx,
                time_ms=parse_time_ms(p, idx),
                file_name=p.name,
                file_path=str(p),
                width=w,
                height=h,
                raw_aspect=round(w / h if h else 0.0, 6),
                raw_aspect_class=aspect_class(w, h),
                bbox_x0=x0, bbox_y0=y0, bbox_x1=x1, bbox_y1=y1,
                content_aspect=round(car, 6),
                content_aspect_class=cac,
                content_width_ratio=round(cwr, 6),
                content_height_ratio=round(chr_, 6),
                mean_luma=round(mean_l, 6),
                contrast=round(contrast, 6),
                saturation=round(sat, 6),
                underexposed_ratio=round(under, 6),
                overexposed_ratio=round(over, 6),
                sharpness_score=round(sharp, 6),
                edge_density=round(edge_d, 6),
                dominant_edge_orientation=dom,
                quality_score=q,
                pixel_mad_prev=round(pix, 6),
                changed_area_prev=round(area, 6),
                edge_mad_prev=round(ediff, 6),
                grid_luma_diff_prev=round(gdiff, 6),
                hist_l1_prev=round(hd, 6),
                luma_diff_prev=round(ldiff, 6),
                transition_like_score=trans,
                stability_score=stability,
                anchor_score=anchor,
                evidence_bucket=bucket,
            ))
            prev_small, prev_edge, prev_grid, prev_rgb, prev_luma = gray, e, grid, small, mean_l
    return rows


def write_csv(path: Path, rows: List[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))


def copy_review_images(rows: List[Row], out: Path, max_each: int = 24) -> Dict[str, int]:
    root = out / "review_images"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    groups: Dict[str, List[Row]] = {}
    groups["01_all_frames_thumb_source_order"] = rows[:]
    groups["02_top_anchor_score"] = sorted(rows, key=lambda r: r.anchor_score, reverse=True)[:max_each]
    groups["03_top_quality_score"] = sorted(rows, key=lambda r: r.quality_score, reverse=True)[:max_each]
    groups["04_top_transition_like"] = sorted(rows, key=lambda r: r.transition_like_score, reverse=True)[:max_each]
    groups["05_low_quality"] = sorted(rows, key=lambda r: r.quality_score)[:max_each]
    groups["06_stable_quality_candidates"] = [r for r in sorted(rows, key=lambda r: r.anchor_score, reverse=True) if r.evidence_bucket == "stable_quality_anchor_candidate"][:max_each]
    groups["07_strong_change_candidates"] = [r for r in sorted(rows, key=lambda r: r.transition_like_score, reverse=True) if r.evidence_bucket == "strong_change_candidate"][:max_each]

    # Periodic visual overview: every Nth original image, max 40.
    if rows:
        step = max(1, math.ceil(len(rows) / 40))
        groups["00_timeline_every_n_frames"] = rows[::step]

    counts: Dict[str, int] = {}
    for g, rs in groups.items():
        d = root / g
        d.mkdir(parents=True, exist_ok=True)
        counts[g] = len(rs)
        for rank, r in enumerate(rs, 1):
            src = Path(r.file_path)
            dst_name = f"{rank:03d}_idx{r.index:04d}_t{r.time_ms:07d}_q{r.quality_score:.3f}_a{r.anchor_score:.3f}_tr{r.transition_like_score:.3f}_{safe_name(src.name)}"
            shutil.copy2(src, d / dst_name)
    return counts


def write_html(rows: List[Row], out: Path) -> None:
    d = out / "contact_sheet"
    d.mkdir(parents=True, exist_ok=True)
    # HTML uses original absolute image paths so it is a quick visual sheet.
    cards = []
    for r in rows:
        cards.append(f"""
        <div class='card'>
          <img src='file://{html.escape(r.file_path)}'>
          <div>idx={r.index} t={r.time_ms}ms</div>
          <div>{html.escape(r.evidence_bucket)}</div>
          <div>raw={r.raw_aspect_class} content={r.content_aspect_class}</div>
          <div>q={r.quality_score:.3f} st={r.stability_score:.3f} a={r.anchor_score:.3f}</div>
          <div>tr={r.transition_like_score:.3f} area={r.changed_area_prev:.3f}</div>
        </div>
        """)
    html_text = """<!doctype html><meta charset='utf-8'>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:16px;background:#fafafa}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.card{background:white;border:1px solid #ddd;padding:8px;font-size:12px}
.card img{width:100%;height:160px;object-fit:contain;background:#111}
</style>
<h1>Low-cost visual descriptor image sheet</h1>
<div class='grid'>%s</div>""" % "\n".join(cards)
    (d / "lowcost_descriptor_contact_sheet.html").write_text(html_text, encoding="utf-8")


def count_by(rows: List[Row], field: str) -> Dict[str, int]:
    d: Dict[str, int] = {}
    for r in rows:
        v = str(getattr(r, field))
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items()))


def mean(rows: List[Row], field: str) -> float:
    if not rows:
        return 0.0
    return round(sum(float(getattr(r, field)) for r in rows) / len(rows), 6)


def write_report(rows: List[Row], out: Path, review_counts: Dict[str, int]) -> Dict[str, object]:
    out_report = out / "final_report"
    out_report.mkdir(parents=True, exist_ok=True)
    raw_counts = count_by(rows, "raw_aspect_class")
    content_counts = count_by(rows, "content_aspect_class")
    bucket_counts = count_by(rows, "evidence_bucket")
    edge_counts = count_by(rows, "dominant_edge_orientation")
    summary: Dict[str, object] = {
        "script_version": SCRIPT_VERSION,
        "status": "DONE",
        "mode": "descriptor_only_image_evidence_no_model_no_routing",
        "frame_count": len(rows),
        "raw_aspect_class_counts": raw_counts,
        "content_aspect_class_counts": content_counts,
        "evidence_bucket_counts": bucket_counts,
        "dominant_edge_orientation_counts": edge_counts,
        "mean_quality_score": mean(rows, "quality_score"),
        "mean_stability_score": mean(rows, "stability_score"),
        "mean_underexposed_ratio": mean(rows, "underexposed_ratio"),
        "mean_sharpness_score": mean(rows, "sharpness_score"),
        "review_image_counts": review_counts,
        "review_images_root": str(out / "review_images"),
        "timeline_images": str(out / "review_images/00_timeline_every_n_frames"),
        "top_anchor_images": str(out / "review_images/02_top_anchor_score"),
        "top_quality_images": str(out / "review_images/03_top_quality_score"),
        "top_transition_images": str(out / "review_images/04_top_transition_like"),
    }
    md = [
        "# Step02-3 Low-cost Visual Descriptor Probe v3",
        "",
        f"- script_version: `{SCRIPT_VERSION}`",
        "- mode: `descriptor_only_image_evidence_no_model_no_routing`",
        f"- frame_count: `{len(rows)}`",
        "",
        "## Layout / aspect signals",
        f"- raw_aspect_class_counts: `{raw_counts}`",
        f"- content_aspect_class_counts: `{content_counts}`",
        "",
        "## Quality / stability signals",
        f"- evidence_bucket_counts: `{bucket_counts}`",
        f"- dominant_edge_orientation_counts: `{edge_counts}`",
        f"- mean_quality_score: `{summary['mean_quality_score']}`",
        f"- mean_stability_score: `{summary['mean_stability_score']}`",
        f"- mean_underexposed_ratio: `{summary['mean_underexposed_ratio']}`",
        f"- mean_sharpness_score: `{summary['mean_sharpness_score']}`",
        "",
        "## Actual image evidence folders",
        f"- timeline overview: `{summary['timeline_images']}`",
        f"- top anchor images: `{summary['top_anchor_images']}`",
        f"- top quality images: `{summary['top_quality_images']}`",
        f"- transition-like images: `{summary['top_transition_images']}`",
        "",
        "## Review image counts",
    ]
    for k, v in review_counts.items():
        md.append(f"- {k}: `{v}`")
    md += [
        "",
        "## Guardrail",
        "This probe does not select YOLOE or high_value frames. It only copies real frame images into evidence folders and writes metrics for diagnosis.",
    ]
    (out_report / "lowcost_visual_descriptor_report.md").write_text("\n".join(md), encoding="utf-8")
    (out_report / "lowcost_visual_descriptor_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-frame-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    in_dir = Path(args.input_frame_dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    paths = sorted([p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS])
    if not paths:
        raise SystemExit(f"ERROR: no image frames found in {in_dir}")

    rows = calc_rows(paths)
    write_csv(out / "manifests/frame_lowcost_descriptor.csv", rows)
    # Keep a small candidates CSV for quick terminal/csv inspection.
    anchors = sorted(rows, key=lambda r: r.anchor_score, reverse=True)[:30]
    write_csv(out / "diagnostics/potential_anchor_images.csv", anchors)
    transitions = sorted(rows, key=lambda r: r.transition_like_score, reverse=True)[:30]
    write_csv(out / "diagnostics/potential_transition_images.csv", transitions)

    review_counts = copy_review_images(rows, out, max_each=24)
    write_html(rows, out)
    summary = write_report(rows, out, review_counts)

    print(json.dumps({
        "script_version": SCRIPT_VERSION,
        "status": "DONE",
        "frame_count": len(rows),
        "raw_aspect_class_counts": summary["raw_aspect_class_counts"],
        "content_aspect_class_counts": summary["content_aspect_class_counts"],
        "evidence_bucket_counts": summary["evidence_bucket_counts"],
        "review_images_root": summary["review_images_root"],
        "open_this_first": summary["timeline_images"],
        "top_anchor_images": summary["top_anchor_images"],
        "top_quality_images": summary["top_quality_images"],
        "report": str(out / "final_report/lowcost_visual_descriptor_report.md"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
