#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-2 V15 video frame contact sheet generator.

Purpose:
- Read central SQLite DB and V15 candidate queue CSV files.
- Generate one local HTML page with all video source groups.
- Each video row shows all existing Step02/YOLOE video visual frames.
- Color code:
  grey   = normal existing frame, no YOLOE label evidence
  green  = YOLOE-labeled frame
  red    = Qwen-VL high-value video frame
  yellow = OCR candidate video frame
  red + yellow outline = both high-value and OCR

Hard constraints:
- No network.
- No downloads.
- No model loading.
- No original media write.
- Does not decode original media.
- Reads central DB and existing derived frame/preview image paths only for HTML references.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Any, Set

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V15_OUT = TEST_OUTPUT_ROOT / "stop03-2-candidate-queues-db-safe-v15_0_20260709_213500_full"
DEFAULT_OUT_DIR = TEST_OUTPUT_ROOT / "stop03-2-v15-video-frame-contact-sheet"
SCRIPT_VERSION = "stop03_2_v15_video_frame_contact_sheet_20260709_235500"

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def file_uri(p: str) -> str:
    if not p:
        return ""
    try:
        return Path(p).expanduser().resolve().as_uri()
    except Exception:
        return ""


def ms_to_tc(ms: int) -> str:
    if ms is None or ms < 0:
        return "--:--"
    s = int(round(ms / 1000))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--v15-out", type=Path, default=DEFAULT_V15_OUT)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--max-img-width", type=int, default=150)
    args = ap.parse_args()

    db = args.db
    v15_out = args.v15_out
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    qwen_csv = v15_out / "manifests" / "qwenvl_high_value_candidate_queue.csv"
    ocr_csv = v15_out / "manifests" / "ocr_trigger_candidate_queue.csv"

    qwen_rows = read_csv(qwen_csv)
    ocr_rows = read_csv(ocr_csv)

    high_video_ids: Set[str] = {
        r.get("visual_unit_id", "") for r in qwen_rows
        if r.get("high_value_category") == "video_high_value_segment_candidate"
    }
    ocr_video_ids: Set[str] = {
        r.get("visual_unit_id", "") for r in ocr_rows
        if (r.get("visual_unit_type") == "video_frame" or r.get("media_type") == "video")
    }

    qwen_reason = {r.get("visual_unit_id", ""): r.get("reason_codes", "") for r in qwen_rows}
    ocr_reason = {r.get("visual_unit_id", ""): (r.get("ocr_trigger_reason_codes") or r.get("reason_codes") or "") for r in ocr_rows}
    qwen_score = {r.get("visual_unit_id", ""): r.get("candidate_score", "") for r in qwen_rows}
    ocr_score = {r.get("visual_unit_id", ""): r.get("candidate_score", "") for r in ocr_rows}

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    label_counts = {
        r["visual_unit_id"]: int(r["n"])
        for r in conn.execute("SELECT visual_unit_id, COUNT(*) AS n FROM visual_labels GROUP BY visual_unit_id")
    }

    rows = conn.execute("""
    SELECT
      vu.visual_unit_id,
      vu.source_content_id,
      vu.derived_id,
      COALESCE(da.time_position_ms, vu.time_position_ms) AS time_position_ms,
      COALESCE(vu.visual_file, da.derived_path) AS visual_path,
      da.derived_path,
      sa.relative_path,
      sa.absolute_path,
      sa.file_name,
      sa.media_type
    FROM visual_units vu
    JOIN derived_assets da ON da.derived_id = vu.derived_id
    JOIN source_assets sa ON sa.source_content_id = vu.source_content_id
    WHERE sa.media_type = 'video'
    ORDER BY sa.relative_path, COALESCE(da.time_position_ms, vu.time_position_ms), vu.visual_unit_id
    """).fetchall()

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    group_path: Dict[str, str] = {}
    for r in rows:
        sid = r["source_content_id"]
        try:
            t = int(r["time_position_ms"] if r["time_position_ms"] is not None else -1)
        except Exception:
            t = -1
        vu = r["visual_unit_id"]
        labels_n = label_counts.get(vu, 0)
        is_high = vu in high_video_ids
        is_ocr = vu in ocr_video_ids
        has_yoloe = labels_n > 0
        if is_high and is_ocr:
            cls = "both"
            status = "HIGH+OCR"
        elif is_high:
            cls = "high"
            status = "HIGH"
        elif is_ocr:
            cls = "ocr"
            status = "OCR"
        elif has_yoloe:
            cls = "yoloe"
            status = "YOLOE"
        else:
            cls = "normal"
            status = "NORMAL"
        item = {
            "visual_unit_id": vu,
            "source_content_id": sid,
            "time_position_ms": t,
            "timecode": ms_to_tc(t),
            "visual_path": r["visual_path"] or r["derived_path"] or "",
            "relative_path": r["relative_path"] or r["absolute_path"] or sid,
            "labels_n": labels_n,
            "is_high": is_high,
            "is_ocr": is_ocr,
            "has_yoloe": has_yoloe,
            "class": cls,
            "status": status,
            "qwen_score": qwen_score.get(vu, ""),
            "ocr_score": ocr_score.get(vu, ""),
            "qwen_reason": qwen_reason.get(vu, ""),
            "ocr_reason": ocr_reason.get(vu, ""),
        }
        groups[sid].append(item)
        group_path[sid] = item["relative_path"]

    for sid in groups:
        groups[sid].sort(key=lambda x: (x["time_position_ms"], x["visual_unit_id"]))

    group_summaries = []
    for sid, items in groups.items():
        c = Counter(x["class"] for x in items)
        duration_ms = 0
        if items:
            ts = [x["time_position_ms"] for x in items if x["time_position_ms"] is not None and x["time_position_ms"] >= 0]
            if ts:
                duration_ms = max(ts) - min(ts)
        group_summaries.append({
            "source_content_id": sid,
            "relative_path": group_path.get(sid, sid),
            "frame_count": len(items),
            "duration_s_from_step02_timecodes": round(duration_ms / 1000, 3),
            "high_count": sum(1 for x in items if x["is_high"]),
            "ocr_count": sum(1 for x in items if x["is_ocr"]),
            "both_count": sum(1 for x in items if x["is_high"] and x["is_ocr"]),
            "yoloe_only_count": c.get("yoloe", 0),
            "normal_count": c.get("normal", 0),
        })

    group_summaries.sort(key=lambda x: (-x["high_count"], -x["ocr_count"], x["relative_path"]))

    summary = {
        "script_version": SCRIPT_VERSION,
        "db": str(db),
        "v15_out": str(v15_out),
        "video_group_count": len(groups),
        "video_frame_count": sum(len(v) for v in groups.values()),
        "high_value_video_frame_count": len(high_video_ids),
        "ocr_video_frame_count": len(ocr_video_ids),
        "both_high_and_ocr_count": len(high_video_ids & ocr_video_ids),
        "output_html": str(out_dir / "v15_video_frame_contact_sheet.html"),
        "output_group_summary_csv": str(out_dir / "v15_video_group_summary.csv"),
    }

    with (out_dir / "v15_video_group_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(group_summaries[0].keys()) if group_summaries else [])
        writer.writeheader()
        writer.writerows(group_summaries)
    (out_dir / "v15_video_contact_sheet_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    css = f"""
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #111; color: #eee; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ color: #bbb; font-size: 14px; margin-bottom: 18px; }}
    .legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 24px; }}
    .legend span {{ display: inline-block; padding: 6px 10px; border-radius: 6px; background: #222; }}
    .lg-normal {{ border-left: 8px solid #777; }}
    .lg-yoloe {{ border-left: 8px solid #30c46b; }}
    .lg-high {{ border-left: 8px solid #ff3b30; }}
    .lg-ocr {{ border-left: 8px solid #ffd60a; color: #111; background: #ddd !important; }}
    .lg-both {{ border-left: 8px solid #ff3b30; border-right: 8px solid #ffd60a; }}
    .group {{ border: 1px solid #333; border-radius: 10px; margin: 18px 0; padding: 12px; background: #181818; }}
    .ghead {{ margin-bottom: 10px; }}
    .gtitle {{ font-size: 16px; font-weight: 650; word-break: break-all; }}
    .gstats {{ color: #aaa; font-size: 13px; margin-top: 4px; }}
    .frames {{ display: flex; overflow-x: auto; gap: 8px; padding-bottom: 10px; }}
    .card {{ flex: 0 0 auto; width: {args.max_img_width}px; background: #222; border: 3px solid #777; border-radius: 8px; padding: 5px; }}
    .card.normal {{ border-color: #777; }}
    .card.yoloe {{ border-color: #30c46b; }}
    .card.high {{ border-color: #ff3b30; }}
    .card.ocr {{ border-color: #ffd60a; }}
    .card.both {{ border-color: #ff3b30; box-shadow: 0 0 0 4px #ffd60a inset; }}
    .thumb {{ width: 100%; height: auto; display: block; background: #333; border-radius: 4px; }}
    .cap {{ font-size: 11px; color: #ddd; margin-top: 4px; line-height: 1.35; word-break: break-word; }}
    .badge {{ display: inline-block; font-size: 10px; padding: 2px 4px; border-radius: 4px; margin-right: 3px; color: #111; font-weight: 700; }}
    .b-normal {{ background: #aaa; }} .b-yoloe {{ background: #30c46b; }} .b-high {{ background: #ff3b30; color:#fff; }} .b-ocr {{ background: #ffd60a; }} .b-both {{ background: linear-gradient(90deg,#ff3b30 0 50%,#ffd60a 50% 100%); color:#111; }}
    details {{ margin-top: 4px; color: #aaa; }}
    summary {{ cursor: pointer; }}
    code {{ white-space: pre-wrap; }}
    """

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<title>Stop03-2 V15 Video Frame Contact Sheet</title>")
    parts.append(f"<style>{css}</style></head><body>")
    parts.append("<h1>Stop03-2 V15 视频帧总览</h1>")
    parts.append(f"<div class='meta'>DB: {html.escape(str(db))}<br>V15 OUT: {html.escape(str(v15_out))}<br>视频组: {summary['video_group_count']}；全部视频帧: {summary['video_frame_count']}；高价值帧: {summary['high_value_video_frame_count']}；OCR帧: {summary['ocr_video_frame_count']}；高价值+OCR重合: {summary['both_high_and_ocr_count']}</div>")
    parts.append("<div class='legend'>")
    parts.append("<span class='lg-normal'>灰色：普通帧</span>")
    parts.append("<span class='lg-yoloe'>绿色：YOLOE有标签帧</span>")
    parts.append("<span class='lg-high'>红色：高价值帧 / Qwen-VL候选</span>")
    parts.append("<span class='lg-ocr'>黄色：OCR候选帧</span>")
    parts.append("<span class='lg-both'>红框+黄内框：同时是高价值和OCR</span>")
    parts.append("</div>")

    for idx, gs in enumerate(group_summaries, start=1):
        sid = gs["source_content_id"]
        items = groups[sid]
        parts.append("<section class='group'>")
        parts.append(f"<div class='ghead'><div class='gtitle'>{idx:03d}. {html.escape(gs['relative_path'])}</div>")
        parts.append(f"<div class='gstats'>source_content_id={html.escape(sid)} ｜ frames={gs['frame_count']} ｜ duration≈{gs['duration_s_from_step02_timecodes']}s ｜ high={gs['high_count']} ｜ OCR={gs['ocr_count']} ｜ both={gs['both_count']} ｜ yoloe_only={gs['yoloe_only_count']} ｜ normal={gs['normal_count']}</div></div>")
        parts.append("<div class='frames'>")
        for it in items:
            cls = it["class"]
            badge_cls = "b-" + cls
            uri = file_uri(it["visual_path"])
            alt = html.escape(it["visual_unit_id"])
            parts.append(f"<div class='card {cls}'>")
            if uri:
                parts.append(f"<a href='{html.escape(uri)}' target='_blank'><img class='thumb' src='{html.escape(uri)}' alt='{alt}' loading='lazy'></a>")
            else:
                parts.append("<div class='thumb'>NO IMAGE</div>")
            parts.append("<div class='cap'>")
            parts.append(f"<span class='badge {badge_cls}'>{html.escape(it['status'])}</span><br>")
            parts.append(f"{html.escape(it['timecode'])} ｜ labels={it['labels_n']}<br>")
            if it["qwen_score"]:
                parts.append(f"Q={html.escape(it['qwen_score'])} ")
            if it["ocr_score"]:
                parts.append(f"OCR={html.escape(it['ocr_score'])}")
            if it["qwen_reason"] or it["ocr_reason"]:
                parts.append("<details><summary>reason</summary><code>")
                if it["qwen_reason"]:
                    parts.append("Qwen: " + html.escape(it["qwen_reason"]) + "\n")
                if it["ocr_reason"]:
                    parts.append("OCR: " + html.escape(it["ocr_reason"]) + "\n")
                parts.append("</code></details>")
            parts.append("</div></div>")
        parts.append("</div></section>")

    parts.append("</body></html>")
    html_path = out_dir / "v15_video_frame_contact_sheet.html"
    html_path.write_text("\n".join(parts), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("HTML:", html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
