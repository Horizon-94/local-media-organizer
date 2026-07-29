#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step02-3 rule search evaluator for manually labeled frame samples.

Boundary:
- Reads manual label CSV and already-extracted Step02 JPG frame directories.
- Writes only to a new evaluator output directory.
- Does not call YOLOE, Qwen-VL, OCR, Embedding, or any model.
- Does not change source frames or formal indexes.
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
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing numpy. This evaluator requires numpy and Pillow.") from exc

try:
    from PIL import Image, ImageOps
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing Pillow. This evaluator requires numpy and Pillow.") from exc

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step02_3_rule_search_policies import POLICY_NAMES, PolicySelection, apply_policy


SCRIPT_VERSION = "step02_3_rule_search_evaluator_v1_20260707"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TIME_RE = re.compile(r"t(\d+)ms", re.IGNORECASE)
IDX_RE = re.compile(r"idx(\d+)", re.IGNORECASE)

EXPECTED_NOTES = {
    "P1010641_lighting_test": "灯光测试：黑屏帧不得选；灯光节点需覆盖。",
    "A001_05111040_C077_anniversary_ritual_phone_field": "祭祀/文字短片：少量帧，文字明显帧可 high_value + YOLOE。",
    "C031_park_environment": "公园环境/空镜：一张 high_value，可少量 YOLOE，不要因行人走动多选。",
    "C034_self_talk_shanghai": "口播自述：一张面容/正面好帧 high_value，少量姿势 YOLOE。",
    "RPReplay_driving_exam_ocr": "驾照录屏：OCR_TRIGGER + YOLOE，不进 high_value。",
    "A7M4_4896_camera_gear": "相机设备展示：可有多张 high_value，设备不同语义需要覆盖。",
    "R5_5F4A0190_night_food_order": "夜晚店铺点餐短片：一张 high_value 足够代表夜晚/人物/招牌/灯光。",
    "C035_old_office_memory": "旧办公室回忆：high_value 可来自叙事/记忆意义，不一定是变化最大。",
    "GX022345_gopro_xinjiang_walk": "GoPro 新疆走路：稀疏正样本；移动长视频不应压成一两张，也不应全选。",
    "A001_05272253_C013_selfie_portrait_to_landscape": "夜晚自拍视频 C013：high_value 覆盖横竖/阶段变化；YOLOE 覆盖手势状态但去除相近重复。",
    "GoPro0718_SDX66_philippines_dive_team": "菲律宾潜水/海面纪录：团队人员和潜水节点可 high_value；长纪录视频需要较均匀 YOLOE 覆盖。",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step02-3 rule search evaluator")
    p.add_argument("--labels", required=True, help="manual_labels_v1_11_samples_aggregated.csv")
    p.add_argument("--out", required=True, help="new evaluator output directory")
    p.add_argument("--policies", default="all", help="all or comma-separated policy names")
    p.add_argument("--limit-videos", type=int, default=0, help="debug limit; 0 means all")
    p.add_argument("--no-open", action="store_true", help="accepted for CLI compatibility; no browser is opened")
    p.add_argument("--force", action="store_true", help="allow clearing an existing non-empty output directory")
    return p.parse_args(argv)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_index(path_or_name: str, fallback: int) -> int:
    m = IDX_RE.search(Path(path_or_name).name)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", Path(path_or_name).stem)
    return int(nums[-1]) if nums else fallback


def parse_time_ms(path_or_name: str, fallback_index: int) -> int:
    m = TIME_RE.search(Path(path_or_name).name)
    if m:
        return int(m.group(1))
    return 2000 + max(0, fallback_index) * 3000


def frame_sort_key(path: Path) -> Tuple[int, int, str]:
    idx = parse_index(path.name, 10**9)
    t = parse_time_ms(path.name, idx)
    return t, idx, path.name


def list_frames(frame_dir: Path) -> List[Path]:
    return sorted([p for p in frame_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS], key=frame_sort_key)


def entropy_gray(gray: np.ndarray) -> float:
    hist, _ = np.histogram(gray, bins=32, range=(0.0, 1.0), density=False)
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    probs = hist.astype(np.float64) / total
    probs = probs[probs > 0]
    return float(-np.sum(probs * np.log2(probs)) / math.log2(32))


def edge_mag(gray: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    return np.sqrt(gx * gx + gy * gy)


def color_hist(rgb: np.ndarray) -> np.ndarray:
    hists = []
    for ch in range(3):
        hist, _ = np.histogram(rgb[..., ch], bins=8, range=(0.0, 1.0), density=False)
        hist = hist.astype(np.float64)
        hists.append(hist / max(1.0, float(hist.sum())))
    return np.concatenate(hists)


def change_class(pixel_mad: float, changed_area_ratio: float, edge_mad: float, hist_l1: float) -> str:
    if pixel_mad < 0.015 and changed_area_ratio < 0.04 and hist_l1 < 0.050:
        return "near_duplicate"
    if pixel_mad < 0.040 and changed_area_ratio < 0.12 and hist_l1 < 0.120:
        return "minor_motion"
    if pixel_mad < 0.105 and changed_area_ratio < 0.34 and edge_mad < 0.050:
        return "normal_change"
    return "strong_change"


def calc_frame(path: Path, seq: int, prev: Optional[dict]) -> dict:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        width, height = im.size
        small = im.resize((160, 90), Image.Resampling.BILINEAR)
    rgb = np.asarray(small).astype(np.float32) / 255.0
    gray = np.asarray(small.convert("L")).astype(np.float32) / 255.0
    edge = edge_mag(gray)
    hist = color_hist(rgb)

    idx = parse_index(path.name, seq)
    t_ms = parse_time_ms(path.name, idx)
    luma_mean = float(gray.mean())
    luma_std = float(gray.std())
    under = float((gray < 0.08).mean())
    over = float((gray > 0.92).mean())
    edge_density = float((edge > max(0.035, float(np.percentile(edge, 80)))).mean())
    entropy = entropy_gray(gray)

    if prev is None:
        pixel_mad = changed_area = edge_mad = hist_l1 = luma_diff = 0.0
        cls = "first_frame"
    else:
        prev_rgb = prev["_rgb"]
        prev_gray = prev["_gray"]
        prev_edge = prev["_edge"]
        prev_hist = prev["_hist"]
        diff = np.abs(rgb - prev_rgb)
        gray_diff = np.abs(gray - prev_gray)
        pixel_mad = float(diff.mean())
        changed_area = float((gray_diff > 0.055).mean())
        edge_mad = float(np.abs(edge - prev_edge).mean())
        hist_l1 = float(np.abs(hist - prev_hist).sum() / 3.0)
        luma_diff = abs(luma_mean - float(prev["luma_mean"]))
        cls = change_class(pixel_mad, changed_area, edge_mad, hist_l1)

    row = {
        "file_name": path.name,
        "source_path": str(path),
        "index": idx,
        "time_ms": t_ms,
        "width": width,
        "height": height,
        "aspect": round(width / max(1, height), 6),
        "luma_mean": round(luma_mean, 6),
        "luma_std": round(luma_std, 6),
        "underexposed_ratio": round(under, 6),
        "overexposed_ratio": round(over, 6),
        "edge_density": round(edge_density, 6),
        "entropy": round(entropy, 6),
        "pixel_mad": round(pixel_mad, 6),
        "changed_area_ratio": round(changed_area, 6),
        "edge_mad": round(edge_mad, 6),
        "hist_l1": round(hist_l1, 6),
        "luma_diff": round(luma_diff, 6),
        "change_class": cls,
        "_rgb": rgb,
        "_gray": gray,
        "_edge": edge,
        "_hist": hist,
    }
    return row


def add_clusters(frames: List[dict]) -> None:
    cluster_id = 0
    for i, row in enumerate(frames):
        if i > 0 and row["change_class"] in {"normal_change", "strong_change"}:
            cluster_id += 1
        row["cluster_id"] = cluster_id
    by_cluster: Dict[int, List[dict]] = defaultdict(list)
    for row in frames:
        by_cluster[int(row["cluster_id"])].append(row)
    for group in by_cluster.values():
        span = int(group[-1]["time_ms"]) - int(group[0]["time_ms"])
        for row in group:
            row["cluster_size"] = len(group)
            row["cluster_span_ms"] = span


def compute_metrics(video_id: str, frame_dir: Path) -> List[dict]:
    frames: List[dict] = []
    prev = None
    for seq, path in enumerate(list_frames(frame_dir), 1):
        row = calc_frame(path, seq, prev)
        row["video_id"] = video_id
        row["frame_dir"] = str(frame_dir)
        frames.append(row)
        prev = row
    add_clusters(frames)
    for row in frames:
        for private_key in ["_rgb", "_gray", "_edge", "_hist"]:
            row.pop(private_key, None)
    return frames


def write_csv(path: Path, rows: Sequence[dict], fieldnames: Optional[Sequence[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        seen = set()
        fields = []
        for row in rows:
            for key in row:
                if key not in seen:
                    fields.append(key)
                    seen.add(key)
    else:
        fields = list(fieldnames)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_labels(labels_path: Path, limit_videos: int) -> Tuple[List[dict], List[str]]:
    with labels_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"[FAIL] labels CSV is empty: {labels_path}")
    ordered_videos = []
    seen = set()
    for row in rows:
        vid = row["video_id"]
        if vid not in seen:
            ordered_videos.append(vid)
            seen.add(vid)
    if limit_videos and limit_videos > 0:
        keep = set(ordered_videos[:limit_videos])
        rows = [r for r in rows if r["video_id"] in keep]
        ordered_videos = ordered_videos[:limit_videos]
    for row in rows:
        row["time_ms_int"] = int(row["time_ms"] or parse_time_ms(row["file_name"], parse_index(row["file_name"], 0)))
        row["also_yoloe_bool"] = parse_bool(row.get("also_yoloe", "false"))
        row["also_high_value_bool"] = parse_bool(row.get("also_high_value", "false"))
        row["also_ocr_trigger_bool"] = parse_bool(row.get("also_ocr_trigger", "false"))
    return rows, ordered_videos


def prepare_out(out: Path, force: bool) -> None:
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    for sub in ["manifests", "summary", "policies"]:
        (out / sub).mkdir(parents=True, exist_ok=True)


def validate_inputs(labels: Sequence[dict], videos: Sequence[str]) -> Tuple[Dict[str, Path], List[dict]]:
    blocked = []
    video_dirs: Dict[str, Path] = {}
    for vid in videos:
        rows = [r for r in labels if r["video_id"] == vid]
        frame_dir = Path(rows[0]["video_path"])
        video_dirs[vid] = frame_dir
        if not frame_dir.exists() or not frame_dir.is_dir():
            blocked.append({"type": "missing_frame_dir", "video_id": vid, "path": str(frame_dir)})
            continue
        frame_names = {p.name for p in list_frames(frame_dir)}
        if not frame_names:
            blocked.append({"type": "empty_frame_dir", "video_id": vid, "path": str(frame_dir)})
            continue
        for row in rows:
            if row["file_name"] not in frame_names:
                blocked.append({"type": "missing_labeled_frame", "video_id": vid, "file_name": row["file_name"], "path": str(frame_dir / row["file_name"])})
    return video_dirs, blocked


def copy_selected(selection: PolicySelection, frames: Sequence[dict], out_dir: Path) -> None:
    route_dirs = {
        "yoloe": out_dir / "selected_yoloe_frames",
        "high_value": out_dir / "selected_high_value_frames",
        "ocr_trigger": out_dir / "selected_ocr_trigger_frames",
    }
    for d in route_dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    by_name = {f["file_name"]: f for f in frames}
    routes = [
        ("yoloe", selection.yoloe, "yoloe"),
        ("high_value", selection.high_value, "highvalue"),
        ("ocr_trigger", selection.ocr_trigger, "ocr"),
    ]
    for route, names, prefix in routes:
        for rank, name in enumerate(sorted(names, key=lambda n: (int(by_name[n]["time_ms"]), n)), 1):
            src = Path(by_name[name]["source_path"])
            dst = route_dirs[route] / f"{prefix}_{rank:03d}_idx{int(by_name[name]['index']):04d}_t{int(by_name[name]['time_ms']):09d}ms_{name}"
            shutil.copy2(src, dst)


def write_contact_sheet(selection: PolicySelection, frames: Sequence[dict], out_dir: Path, title: str) -> None:
    cs = out_dir / "contact_sheet"
    thumbs = cs / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    cards = []
    for f in sorted(frames, key=lambda x: (int(x["time_ms"]), int(x["index"]))):
        name = f["file_name"]
        if name in selection.high_value:
            cls, badge = "high", "HV+Y"
        elif name in selection.ocr_trigger:
            cls, badge = "ocr", "OCR+Y"
        elif name in selection.yoloe:
            cls, badge = "yoloe", "Y"
        else:
            cls, badge = "drop", "drop"
        thumb = thumbs / f"{int(f['index']):04d}_{name}"
        try:
            with Image.open(f["source_path"]) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                width = 190
                height = max(1, int(im.height * width / max(1, im.width)))
                im.resize((width, height), Image.Resampling.BILINEAR).save(thumb, quality=82)
        except Exception:
            shutil.copy2(f["source_path"], thumb)
        rel = os.path.relpath(thumb, cs)
        cards.append(
            f"<div class='card {cls}'><div class='badge'>{badge}</div>"
            f"<img src='{html.escape(rel)}'>"
            f"<div class='meta'>idx={f['index']} t={int(f['time_ms'])/1000:.1f}s<br>"
            f"change={html.escape(str(f['change_class']))} cluster={f['cluster_id']} size={f['cluster_size']}<br>"
            f"mad={float(f['pixel_mad']):.4f} area={float(f['changed_area_ratio']):.3f} edge={float(f['edge_mad']):.4f}<br>"
            f"luma={float(f['luma_mean']):.3f} std={float(f['luma_std']):.3f} entropy={float(f['entropy']):.3f}</div></div>"
        )
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 16px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 10px; }}
.card {{ border: 2px solid #b8b8b8; padding: 6px; border-radius: 6px; }}
.card img {{ width: 100%; height: auto; display: block; }}
.high {{ border-color: #d9480f; background: #fff4e6; }}
.yoloe {{ border-color: #2b8a3e; background: #ebfbee; }}
.ocr {{ border-color: #1c7ed6; background: #e7f5ff; }}
.drop {{ opacity: 0.55; }}
.badge {{ font-weight: 700; margin-bottom: 4px; }}
.meta {{ font-size: 12px; line-height: 1.35; word-break: break-all; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>Unlabeled frames are not treated as errors; they are shown only for review.</p>
<p>YOLOE={len(selection.yoloe)} high_value={len(selection.high_value)} OCR_TRIGGER={len(selection.ocr_trigger)}</p>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
    (cs / "selection_contact_sheet.html").write_text(doc, encoding="utf-8")


def route_manifest(selection: PolicySelection, frames: Sequence[dict]) -> List[dict]:
    rows = []
    for f in sorted(frames, key=lambda x: (int(x["time_ms"]), int(x["index"]))):
        name = f["file_name"]
        rows.append({
            **{k: v for k, v in f.items() if k not in {"source_path"}},
            "route_yoloe": name in selection.yoloe,
            "route_high_value": name in selection.high_value,
            "route_ocr_trigger": name in selection.ocr_trigger,
            "source_path": f["source_path"],
        })
    return rows


def has_hv_window(label_row: dict, frames_by_name: Dict[str, dict], high_names: Iterable[str]) -> bool:
    label_name = label_row["file_name"]
    if label_name in high_names:
        return True
    if label_name not in frames_by_name:
        return False
    label_idx = int(frames_by_name[label_name]["index"])
    label_t = int(frames_by_name[label_name]["time_ms"])
    for name in high_names:
        f = frames_by_name.get(name)
        if f is None:
            continue
        if abs(int(f["index"]) - label_idx) <= 3 or abs(int(f["time_ms"]) - label_t) <= 9000:
            return True
    return False


def video_profile(video_id: str) -> str:
    low = video_id.lower()
    if "rpreplay" in low or "ocr" in low:
        return "ocr_screen"
    if any(x in low for x in ["gopro", "gx022345", "c013", "dive", "walk"]):
        return "mobile_or_documentary"
    if any(x in low for x in ["c031", "c034", "r5_", "c077", "office"]):
        return "static_or_sparse"
    return "mixed"


def score_policy_video(policy: str, video_id: str, labels: Sequence[dict], frames: Sequence[dict], selection: PolicySelection) -> Tuple[dict, List[dict]]:
    frames_by_name = {f["file_name"]: f for f in frames}
    failures: List[dict] = []
    yoloe = selection.yoloe
    high = selection.high_value
    ocr = selection.ocr_trigger

    route_consistency = 5.0
    if not high.issubset(yoloe):
        route_consistency -= 20.0
        failures.append({"type": "high_value_not_subset_yoloe", "policy": policy, "video_id": video_id})
    if not ocr.issubset(yoloe):
        route_consistency -= 20.0
        failures.append({"type": "ocr_not_subset_yoloe", "policy": policy, "video_id": video_id})
    if high & ocr:
        route_consistency -= 20.0
        failures.append({"type": "high_value_ocr_overlap", "policy": policy, "video_id": video_id, "count": len(high & ocr)})

    yoloe_hit = 0.0
    high_hit = 0.0
    ocr_hit = 0.0
    bad_penalty = 0.0
    matched_accept_high = 0
    matched_accept_yoloe = 0
    matched_accept_ocr = 0
    bad_selected = 0

    for row in labels:
        name = row["file_name"]
        label = row["label"]
        in_y = name in yoloe
        in_h = name in high
        in_o = name in ocr
        if label == "accept_high_value":
            if in_y:
                yoloe_hit += 2.0
            if has_hv_window(row, frames_by_name, high):
                high_hit += 5.0
                matched_accept_high += 1
            elif in_y:
                high_hit += 0.8
            if in_h and not in_y:
                route_consistency -= 15.0
        elif label == "accept_yoloe":
            if in_y:
                yoloe_hit += 3.0
                matched_accept_yoloe += 1
        elif label == "accept_ocr_trigger":
            if in_y:
                yoloe_hit += 2.0
            if in_o:
                ocr_hit += 6.0
                matched_accept_ocr += 1
            if in_h:
                bad_penalty -= 8.0
                failures.append({"type": "ocr_label_entered_high_value", "policy": policy, "video_id": video_id, "file_name": name})
        elif label == "reject_all":
            if in_y or in_h or in_o:
                bad_penalty -= 12.0
                bad_selected += 1
                failures.append({"type": "reject_all_selected", "policy": policy, "video_id": video_id, "file_name": name})
        elif label == "reject_high_value":
            if in_h:
                bad_penalty -= 6.0
                bad_selected += 1
                failures.append({"type": "reject_high_value_selected_high_value", "policy": policy, "video_id": video_id, "file_name": name})

    temporal = 0.0
    profile = video_profile(video_id)
    duration = max(1, max(int(f["time_ms"]) for f in frames) - min(int(f["time_ms"]) for f in frames))
    minutes = max(duration / 60000.0, 0.1)
    y_count = len(yoloe)
    h_count = len(high)
    if profile == "static_or_sparse":
        if y_count <= 3:
            temporal += 5.0
        elif y_count <= 6:
            temporal += 2.0
        else:
            temporal -= min(8.0, (y_count - 6) * 1.0)
    elif profile == "mobile_or_documentary":
        if y_count >= min(4, len(frames)):
            temporal += 4.0
        else:
            temporal -= 4.0
        if yoloe:
            selected_times = [int(frames_by_name[n]["time_ms"]) for n in yoloe if n in frames_by_name]
            if selected_times and (max(selected_times) - min(selected_times)) / duration >= 0.35:
                temporal += 3.0
            else:
                temporal -= 3.0
    elif profile == "ocr_screen":
        if len(ocr) >= 1 and h_count == 0:
            temporal += 6.0
        if h_count:
            temporal -= 8.0
    else:
        temporal += 2.0 if 1 <= y_count <= max(5, int(minutes * 8)) else -2.0

    if h_count > 1:
        h_times = [int(frames_by_name[n]["time_ms"]) for n in high if n in frames_by_name]
        if h_times and (max(h_times) - min(h_times)) < min(12000, duration * 0.08):
            temporal -= 2.0

    total = yoloe_hit + high_hit + ocr_hit + bad_penalty + route_consistency + temporal
    row = {
        "policy": policy,
        "video_id": video_id,
        "score_total": round(total, 3),
        "score_yoloe_hit": round(yoloe_hit, 3),
        "score_high_value_hit": round(high_hit, 3),
        "score_ocr_hit": round(ocr_hit, 3),
        "score_bad_frame_penalty": round(bad_penalty, 3),
        "score_route_consistency": round(route_consistency, 3),
        "score_temporal_coverage": round(temporal, 3),
        "selected_yoloe_count": y_count,
        "selected_high_value_count": h_count,
        "selected_ocr_trigger_count": len(ocr),
        "matched_accept_high_value_count": matched_accept_high,
        "matched_accept_yoloe_count": matched_accept_yoloe,
        "matched_accept_ocr_trigger_count": matched_accept_ocr,
        "bad_selected_count": bad_selected,
        "frame_count": len(frames),
        "label_count": len(labels),
        "profile": profile,
    }
    return row, failures


def write_policy_report(out_dir: Path, policy: str, video_id: str, selection: PolicySelection, score: dict) -> None:
    report_dir = out_dir / "final_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    md = [
        "# Step02-3 Rule Search Policy Report",
        "",
        f"- script_version: {SCRIPT_VERSION}",
        f"- policy: {policy}",
        f"- video_id: {video_id}",
        f"- reason: {selection.reason}",
        f"- expected_sample_note: {EXPECTED_NOTES.get(video_id, 'n/a')}",
        "- source_safety: source frames read-only; evaluator only copies selected frames to output.",
        "- unlabeled_frame_rule: frames without manual labels are not automatically counted as errors.",
        "- route_contract: YOLOE is the entry route; high_value and OCR_TRIGGER must be YOLOE subsets; high_value and OCR_TRIGGER are mutually exclusive.",
        "",
        "## Score",
    ]
    for key in [
        "score_total",
        "score_yoloe_hit",
        "score_high_value_hit",
        "score_ocr_hit",
        "score_bad_frame_penalty",
        "score_route_consistency",
        "score_temporal_coverage",
        "selected_yoloe_count",
        "selected_high_value_count",
        "selected_ocr_trigger_count",
        "matched_accept_high_value_count",
        "matched_accept_yoloe_count",
        "matched_accept_ocr_trigger_count",
        "bad_selected_count",
    ]:
        md.append(f"- {key}: {score[key]}")
    (report_dir / "policy_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def write_summary(out: Path, scores: Sequence[dict], failures: Sequence[dict], blocked: Sequence[dict], videos: Sequence[str], labels: Sequence[dict]) -> dict:
    by_policy: Dict[str, List[dict]] = defaultdict(list)
    for row in scores:
        by_policy[row["policy"]].append(row)
    policy_totals = []
    for policy, rows in sorted(by_policy.items()):
        policy_totals.append({
            "policy": policy,
            "score_total": round(sum(float(r["score_total"]) for r in rows), 3),
            "score_yoloe_hit": round(sum(float(r["score_yoloe_hit"]) for r in rows), 3),
            "score_high_value_hit": round(sum(float(r["score_high_value_hit"]) for r in rows), 3),
            "score_ocr_hit": round(sum(float(r["score_ocr_hit"]) for r in rows), 3),
            "score_bad_frame_penalty": round(sum(float(r["score_bad_frame_penalty"]) for r in rows), 3),
            "score_route_consistency": round(sum(float(r["score_route_consistency"]) for r in rows), 3),
            "score_temporal_coverage": round(sum(float(r["score_temporal_coverage"]) for r in rows), 3),
            "selected_yoloe_count": sum(int(r["selected_yoloe_count"]) for r in rows),
            "selected_high_value_count": sum(int(r["selected_high_value_count"]) for r in rows),
            "selected_ocr_trigger_count": sum(int(r["selected_ocr_trigger_count"]) for r in rows),
            "matched_accept_high_value_count": sum(int(r["matched_accept_high_value_count"]) for r in rows),
            "matched_accept_yoloe_count": sum(int(r["matched_accept_yoloe_count"]) for r in rows),
            "matched_accept_ocr_trigger_count": sum(int(r["matched_accept_ocr_trigger_count"]) for r in rows),
            "bad_selected_count": sum(int(r["bad_selected_count"]) for r in rows),
            "evaluated_videos": len(rows),
        })
    best = max(policy_totals, key=lambda r: float(r["score_total"]))["policy"] if policy_totals else ""

    write_csv(out / "summary" / "policy_scores.csv", scores)
    write_csv(out / "summary" / "policy_totals.csv", policy_totals)
    if failures:
        write_csv(out / "summary" / "failure_items.csv", list(failures))
    else:
        write_csv(out / "summary" / "failure_items.csv", [])
    if blocked:
        write_csv(out / "summary" / "blocked_items.csv", list(blocked))
    else:
        write_csv(out / "summary" / "blocked_items.csv", [])

    label_counter = Counter(r["label"] for r in labels)
    lines = [
        "# Step02-3 Rule Search Evaluator Summary",
        "",
        f"- script_version: {SCRIPT_VERSION}",
        f"- created_at: {datetime.now().isoformat(timespec='seconds')}",
        f"- videos_in_csv: {len(set(r['video_id'] for r in labels))}",
        f"- evaluated_videos: {len(videos) if not blocked else 0}",
        f"- label_rows: {len(labels)}",
        f"- label_counts: {dict(label_counter)}",
        f"- best_policy: {best}",
        f"- failure_items_empty: {not bool(failures)}",
        f"- blocked_items_empty: {not bool(blocked)}",
        "- unlabeled_frame_rule: manual labels are sparse positives plus limited negatives; unlabeled frames are not automatically scored as errors.",
        "- route_contract: high_value is a YOLOE subset, OCR_TRIGGER is a YOLOE subset, and high_value/OCR_TRIGGER are mutually exclusive.",
        "",
        "## Policy Totals",
        "",
        "| policy | total | yoloe_hit | high_value_hit | ocr_hit | bad_penalty | route | temporal | yoloe | high_value | ocr | bad_selected |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(policy_totals, key=lambda r: float(r["score_total"]), reverse=True):
        lines.append(
            f"| {row['policy']} | {row['score_total']} | {row['score_yoloe_hit']} | {row['score_high_value_hit']} | "
            f"{row['score_ocr_hit']} | {row['score_bad_frame_penalty']} | {row['score_route_consistency']} | "
            f"{row['score_temporal_coverage']} | {row['selected_yoloe_count']} | {row['selected_high_value_count']} | "
            f"{row['selected_ocr_trigger_count']} | {row['bad_selected_count']} |"
        )
    lines.extend(["", "## Sample Expectations", ""])
    for vid in videos:
        lines.append(f"- {vid}: {EXPECTED_NOTES.get(vid, 'n/a')}")
    lines.extend(["", "## Failure Items", ""])
    lines.append("None" if not failures else json.dumps(list(failures), ensure_ascii=False, indent=2))
    lines.extend(["", "## Blocked Items", ""])
    lines.append("None" if not blocked else json.dumps(list(blocked), ensure_ascii=False, indent=2))
    (out / "summary" / "policy_scores.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "script_version": SCRIPT_VERSION,
        "videos_in_csv": len(set(r["video_id"] for r in labels)),
        "evaluated_videos": len(videos) if not blocked else 0,
        "best_policy": best,
        "failure_items_empty": not bool(failures),
        "blocked_items_empty": not bool(blocked),
        "policy_totals": policy_totals,
        "failure_items": list(failures),
        "blocked_items": list(blocked),
    }
    (out / "summary" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    labels_path = Path(args.labels).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not labels_path.exists():
        print(f"[BLOCKED] labels CSV not found: {labels_path}", file=sys.stderr)
        return 3
    policies = POLICY_NAMES if args.policies == "all" else [p.strip() for p in args.policies.split(",") if p.strip()]
    unknown = [p for p in policies if p not in POLICY_NAMES]
    if unknown:
        print(f"[FAIL] unknown policies: {', '.join(unknown)}", file=sys.stderr)
        return 2

    prepare_out(out, args.force)
    labels, videos = load_labels(labels_path, args.limit_videos)
    video_dirs, blocked = validate_inputs(labels, videos)
    if blocked:
        summary = write_summary(out, [], [], blocked, videos, labels)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 3

    all_metrics: List[dict] = []
    metrics_by_video: Dict[str, List[dict]] = {}
    for vid in videos:
        metrics = compute_metrics(vid, video_dirs[vid])
        metrics_by_video[vid] = metrics
        all_metrics.extend(metrics)
    metric_fields = [
        "video_id", "frame_dir", "file_name", "source_path", "index", "time_ms", "width", "height", "aspect",
        "luma_mean", "luma_std", "underexposed_ratio", "overexposed_ratio", "edge_density", "entropy",
        "pixel_mad", "changed_area_ratio", "edge_mad", "hist_l1", "luma_diff", "change_class",
        "cluster_id", "cluster_size", "cluster_span_ms",
    ]
    write_csv(out / "manifests" / "frame_metrics.csv", all_metrics, metric_fields)

    labels_by_video: Dict[str, List[dict]] = defaultdict(list)
    for row in labels:
        labels_by_video[row["video_id"]].append(row)

    scores: List[dict] = []
    failures: List[dict] = []
    for policy in policies:
        for vid in videos:
            frames = metrics_by_video[vid]
            selection = apply_policy(policy, vid, frames)
            policy_video_out = out / "policies" / policy / vid
            policy_video_out.mkdir(parents=True, exist_ok=True)
            copy_selected(selection, frames, policy_video_out)
            write_contact_sheet(selection, frames, policy_video_out, f"{policy} / {vid}")
            manifest_rows = route_manifest(selection, frames)
            write_csv(policy_video_out / "manifests" / "frame_candidate_manifest.csv", manifest_rows)
            score, score_failures = score_policy_video(policy, vid, labels_by_video[vid], frames, selection)
            scores.append(score)
            failures.extend(score_failures)
            write_policy_report(policy_video_out, policy, vid, selection, score)

    summary = write_summary(out, scores, failures, [], videos, labels)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
