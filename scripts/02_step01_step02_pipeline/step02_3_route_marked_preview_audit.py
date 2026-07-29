#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate HTML contact sheets marking Step02-3 route decisions.

Audit-only. The script references existing visual files in HTML and never copies
or modifies Step02-1 extracted frames, Step02-2 previews, selector rules, or indexes.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


SCRIPT_VERSION = "step02_3_route_marked_preview_audit_v1_20260708"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step02-3 route marked preview audit")
    p.add_argument("--selector-out", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--no-open", action="store_true")
    return p.parse_args(argv)


def read_csv(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, data: dict) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def stable_fields(preferred: Sequence[str], rows: Sequence[dict]) -> List[str]:
    fields = list(preferred)
    seen = set(fields)
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    return fields


def write_csv(path: Path, rows: Sequence[dict], preferred: Sequence[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = stable_fields(preferred, rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    tmp.replace(path)


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    atomic_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def prepare_out(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
    for sub in ["final_report", "reports", "contact_sheets/by_video", "manifests"]:
        (out / sub).mkdir(parents=True, exist_ok=True)


def selector_files(selector_out: Path) -> Dict[str, Path]:
    return {
        "decision": selector_out / "manifests" / "visual_unit_route_decision_manifest.csv",
        "yoloe": selector_out / "queues" / "yoloe_visual_units.csv",
        "high_value": selector_out / "queues" / "high_value_visual_units.csv",
        "ocr_trigger": selector_out / "queues" / "ocr_trigger_visual_units.csv",
    }


def optional_summary_anchor(selector_out: Path) -> Path:
    return selector_out / "queues" / "summary_anchor_visual_units.csv"


def boolish(v: object) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def intish(v: object, default: int = 0) -> int:
    if v in (None, ""):
        return default
    try:
        return int(float(str(v)))
    except ValueError:
        return default


def timecode(ms: object) -> str:
    total_ms = intish(ms)
    h = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    m = rem // 60_000
    rem %= 60_000
    s = rem // 1000
    milli = rem % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def safe_name(text: str) -> str:
    text = text.strip() or "unknown"
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text[:180]


def source_key(row: dict) -> str:
    return (
        row.get("source_video_id")
        or row.get("parent_source_file_id")
        or row.get("source_video_relative_path")
        or row.get("source_relative_path")
        or row.get("parent_source_path_at_processing_time")
        or "unknown_source"
    )


def route_ids(rows: Sequence[dict]) -> set[str]:
    return {r.get("visual_unit_id", "") for r in rows if r.get("visual_unit_id")}


def route_reasons(rows: Sequence[dict]) -> Dict[str, str]:
    return {r.get("visual_unit_id", ""): r.get("route_reason", "") for r in rows if r.get("visual_unit_id")}


def add_risk(risks: List[dict], level: str, risk_type: str, row: Optional[dict], explanation: str) -> None:
    row = row or {}
    risks.append({
        "risk_id": f"risk_{len(risks) + 1:05d}",
        "risk_level": level,
        "risk_type": risk_type,
        "source_video_id": row.get("source_video_id", ""),
        "parent_source_file_id": row.get("parent_source_file_id", ""),
        "source_relative_path": row.get("source_video_relative_path") or row.get("source_relative_path", ""),
        "visual_unit_id": row.get("visual_unit_id", ""),
        "explanation": explanation,
    })


def load_selector(selector_out: Path) -> Tuple[Dict[str, List[dict]], List[dict]]:
    files = selector_files(selector_out)
    missing = [str(p) for p in files.values() if not p.exists()]
    risks: List[dict] = []
    if missing:
        for p in missing:
            add_risk(risks, "ERROR", "selector_output_missing_or_unreadable", None, f"Missing required selector output file: {p}")
        return {}, risks
    data = {name: read_csv(path) for name, path in files.items()}
    anchor = optional_summary_anchor(selector_out)
    data["summary_anchor"] = read_csv(anchor) if anchor.exists() else []
    return data, risks


def build_marked_rows(data: Dict[str, List[dict]], risks: List[dict]) -> Tuple[List[dict], dict]:
    decision = data["decision"]
    yoloe_ids = route_ids(data["yoloe"])
    high_ids = route_ids(data["high_value"])
    ocr_ids = route_ids(data["ocr_trigger"])
    summary_ids = route_ids(data["summary_anchor"])
    reasons = {
        "yoloe": route_reasons(data["yoloe"]),
        "high_value": route_reasons(data["high_value"]),
        "ocr_trigger": route_reasons(data["ocr_trigger"]),
        "summary_anchor": route_reasons(data["summary_anchor"]),
    }
    rows = []
    route_reason_empty_count = 0
    lineage_missing_count = 0
    missing_image_count = 0
    overlap = high_ids & ocr_ids
    by_id = {r.get("visual_unit_id", ""): r for r in decision if r.get("visual_unit_id")}
    for vid in sorted(overlap):
        add_risk(risks, "ERROR", "high_value_ocr_overlap", by_id.get(vid, {"visual_unit_id": vid}), "Visual unit is selected for both high_value and OCR_TRIGGER.")

    for row in decision:
        vid = row.get("visual_unit_id", "")
        marked = dict(row)
        marked["is_yoloe"] = vid in yoloe_ids
        marked["is_high_value"] = vid in high_ids
        marked["is_ocr_trigger"] = vid in ocr_ids
        marked["is_summary_anchor"] = vid in summary_ids
        marked["is_unrouted"] = not (marked["is_yoloe"] or marked["is_high_value"] or marked["is_ocr_trigger"] or marked["is_summary_anchor"])
        marked["route_reason_yoloe"] = reasons["yoloe"].get(vid, row.get("route_reason_yoloe", ""))
        marked["route_reason_high_value"] = reasons["high_value"].get(vid, row.get("route_reason_high_value", ""))
        marked["route_reason_ocr_trigger"] = reasons["ocr_trigger"].get(vid, row.get("route_reason_ocr_trigger", ""))
        marked["route_reason_summary_anchor"] = reasons["summary_anchor"].get(vid, "")
        marked["timecode"] = timecode(row.get("time_position_ms")) if row.get("time_position_ms") not in (None, "") else ""
        if (marked["is_yoloe"] and not marked["route_reason_yoloe"]) or (marked["is_high_value"] and not marked["route_reason_high_value"]) or (marked["is_ocr_trigger"] and not marked["route_reason_ocr_trigger"]) or (marked["is_summary_anchor"] and not marked["route_reason_summary_anchor"]):
            route_reason_empty_count += 1
            add_risk(risks, "ERROR", "route_reason_empty", row, "Selected route is missing its route reason.")
        missing_lineage = [f for f in ["parent_source_file_id", "parent_source_content_id", "parent_source_path_at_processing_time"] if not row.get(f)]
        if missing_lineage:
            lineage_missing_count += 1
            add_risk(risks, "ERROR", "lineage_missing", row, "Selected visual unit is missing parent lineage fields.")
        visual_file = row.get("visual_file", "")
        if not visual_file or not Path(visual_file).exists():
            missing_image_count += 1
            add_risk(risks, "WARNING", "thumbnail_image_file_missing", row, "visual_file is missing or does not exist.")
        rows.append(marked)
    counters = {
        "high_value_ocr_overlap_count": len(overlap),
        "route_reason_empty_count": route_reason_empty_count,
        "lineage_missing_count": lineage_missing_count,
        "missing_image_count": missing_image_count,
    }
    return rows, counters


def tile(row: dict) -> str:
    badges = []
    if boolish(row.get("is_yoloe")):
        badges.append("<span class='badge yoloe'>YOLOE</span>")
    if boolish(row.get("is_high_value")):
        badges.append("<span class='badge high'>HIGH_VALUE</span>")
    if boolish(row.get("is_ocr_trigger")):
        badges.append("<span class='badge ocr'>OCR_TRIGGER</span>")
    if boolish(row.get("is_summary_anchor")):
        badges.append("<span class='badge summary'>SUMMARY_ANCHOR</span>")
    if boolish(row.get("is_unrouted")):
        badges.append("<span class='badge unrouted'>UNROUTED</span>")
    src = row.get("visual_file", "")
    img_src = Path(src).resolve().as_uri() if src and Path(src).exists() else ""
    reason_bits = [
        row.get("route_reason_yoloe", ""),
        row.get("route_reason_high_value", ""),
        row.get("route_reason_ocr_trigger", ""),
        row.get("route_reason_summary_anchor", ""),
    ]
    reason = " | ".join(x for x in reason_bits if x)
    return f"""
<div class="tile">
  <div class="badges">{''.join(badges)}</div>
  {'<img src="' + html.escape(img_src) + '" loading="lazy">' if img_src else '<div class="missing">missing image</div>'}
  <div class="meta">
    <div><b>visual_unit_id</b>: {html.escape(row.get('visual_unit_id', ''))}</div>
    <div><b>type</b>: {html.escape(row.get('visual_unit_type', ''))}</div>
    <div><b>source</b>: {html.escape(row.get('source_video_relative_path') or row.get('source_relative_path', ''))}</div>
    <div><b>time_position_ms</b>: {html.escape(str(row.get('time_position_ms', '')))} <b>timecode</b>: {html.escape(row.get('timecode', ''))}</div>
    <div><b>reason</b>: {html.escape(reason)}</div>
    <div><b>parent_source_file_id</b>: {html.escape(row.get('parent_source_file_id', ''))}</div>
    <div><b>parent_source_content_id</b>: {html.escape(row.get('parent_source_content_id', ''))}</div>
    <div><b>parent_path</b>: {html.escape(row.get('parent_source_path_at_processing_time', ''))}</div>
  </div>
</div>"""


def html_doc(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 16px; }}
.legend {{ display:flex; gap:8px; flex-wrap:wrap; margin: 10px 0 16px; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }}
.tile {{ border:1px solid #bbb; border-radius:6px; padding:6px; background:#fff; }}
.tile img {{ width:100%; height:auto; display:block; background:#eee; }}
.badge {{ display:inline-block; padding:2px 6px; border-radius:4px; color:#111; font-size:12px; font-weight:700; margin:0 4px 4px 0; }}
.yoloe {{ background:#69db7c; }}
.high {{ background:#ff8787; }}
.ocr {{ background:#ffd43b; }}
.summary {{ background:#cc5de8; color:white; }}
.unrouted {{ background:#ced4da; }}
.meta {{ font-size:12px; line-height:1.35; overflow-wrap:anywhere; }}
.missing {{ height:120px; display:flex; align-items:center; justify-content:center; background:#f1f3f5; color:#868e96; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="legend">
<span class="badge yoloe">YOLOE</span>
<span class="badge high">HIGH_VALUE</span>
<span class="badge ocr">OCR_TRIGGER</span>
<span class="badge summary">SUMMARY_ANCHOR</span>
<span class="badge unrouted">UNROUTED</span>
</div>
{body}
</body></html>"""


def write_contact_sheets(out: Path, rows: Sequence[dict], risks: List[dict]) -> int:
    video_rows = [r for r in rows if r.get("visual_unit_type") == "video_frame"]
    image_rows = [r for r in rows if r.get("visual_unit_type") == "image_preview"]
    by_video: Dict[str, List[dict]] = defaultdict(list)
    for row in video_rows:
        by_video[source_key(row)].append(row)
    links = []
    count = 0
    for key in sorted(by_video):
        group = sorted(by_video[key], key=lambda r: (intish(r.get("time_position_ms")), intish(r.get("frame_index")), r.get("visual_unit_id", "")))
        first = group[0]
        name = safe_name(first.get("source_video_id") or first.get("source_video_relative_path") or key) + ".html"
        path = out / "contact_sheets" / "by_video" / name
        body = f"<p>Sorted by numeric time_position_ms ascending. Frames: {len(group)}</p><div class='grid'>{''.join(tile(r) for r in group)}</div>"
        atomic_text(path, html_doc(f"Video route marked preview: {key}", body))
        links.append((name, key, len(group)))
        count += 1
    if image_rows:
        group = sorted(image_rows, key=lambda r: (r.get("source_relative_path", ""), r.get("visual_unit_id", "")))
        path = out / "contact_sheets" / "images.html"
        atomic_text(path, html_doc("Image route marked previews", f"<div class='grid'>{''.join(tile(r) for r in group)}</div>"))
        count += 1
    index_links = [f"<li><a href='by_video/{html.escape(name)}'>{html.escape(key)}</a> ({n} frames)</li>" for name, key, n in links]
    if image_rows:
        index_links.append(f"<li><a href='images.html'>image visual units</a> ({len(image_rows)} units)</li>")
    atomic_text(out / "contact_sheets" / "index.html", html_doc("Step02-3 Route Marked Preview Audit Index", "<ul>" + "".join(index_links) + "</ul>"))
    return count


def summarize(rows: Sequence[dict], risks: Sequence[dict], contact_sheet_count: int, missing_selector: bool = False) -> dict:
    video_rows = [r for r in rows if r.get("visual_unit_type") == "video_frame"]
    image_rows = [r for r in rows if r.get("visual_unit_type") == "image_preview"]
    by_video: Dict[str, List[dict]] = defaultdict(list)
    for row in video_rows:
        by_video[source_key(row)].append(row)
    videos_with_yoloe = sorted(k for k, g in by_video.items() if any(boolish(r.get("is_yoloe")) for r in g))
    videos_with_high = sorted(k for k, g in by_video.items() if any(boolish(r.get("is_high_value")) for r in g))
    videos_with_ocr = sorted(k for k, g in by_video.items() if any(boolish(r.get("is_ocr_trigger")) for r in g))
    videos_missing_yoloe = sorted(k for k, g in by_video.items() if not any(boolish(r.get("is_yoloe")) for r in g))
    videos_missing_high = sorted(k for k, g in by_video.items() if not any(boolish(r.get("is_high_value")) for r in g))
    videos_only_ocr_no_high = sorted(k for k, g in by_video.items() if any(boolish(r.get("is_ocr_trigger")) for r in g) and not any(boolish(r.get("is_high_value")) for r in g))
    return {
        "script_version": SCRIPT_VERSION,
        "status": "BLOCKED" if missing_selector else "PASS",
        "total_visual_unit_count": len(rows),
        "video_visual_unit_count": len(video_rows),
        "image_visual_unit_count": len(image_rows),
        "yoloe_count": sum(1 for r in rows if boolish(r.get("is_yoloe"))),
        "high_value_count": sum(1 for r in rows if boolish(r.get("is_high_value"))),
        "ocr_trigger_count": sum(1 for r in rows if boolish(r.get("is_ocr_trigger"))),
        "summary_anchor_count": sum(1 for r in rows if boolish(r.get("is_summary_anchor"))),
        "videos_with_frames": len(by_video),
        "videos_with_yoloe": len(videos_with_yoloe),
        "videos_with_high_value": len(videos_with_high),
        "videos_with_ocr_trigger": len(videos_with_ocr),
        "videos_missing_yoloe": videos_missing_yoloe,
        "videos_missing_high_value": videos_missing_high,
        "videos_missing_high_value_count": len(videos_missing_high),
        "videos_with_only_ocr_no_high_value": videos_only_ocr_no_high,
        "videos_with_only_ocr_no_high_value_count": len(videos_only_ocr_no_high),
        "high_value_ocr_overlap_count": sum(1 for r in rows if boolish(r.get("is_high_value")) and boolish(r.get("is_ocr_trigger"))),
        "route_reason_empty_count": sum(1 for r in risks if r["risk_type"] == "route_reason_empty"),
        "lineage_missing_count": sum(1 for r in risks if r["risk_type"] == "lineage_missing"),
        "contact_sheet_count": contact_sheet_count,
        "warning_risk_count": sum(1 for r in risks if r["risk_level"] == "WARNING"),
        "error_risk_count": sum(1 for r in risks if r["risk_level"] == "ERROR"),
        "failure_items_count": 0,
    }


def summary_md(summary: dict) -> str:
    keys = [
        "status", "total_visual_unit_count", "yoloe_count", "high_value_count", "ocr_trigger_count",
        "videos_with_frames", "videos_with_yoloe", "videos_with_high_value", "videos_missing_high_value_count",
        "videos_with_only_ocr_no_high_value_count", "contact_sheet_count", "warning_risk_count",
        "error_risk_count",
    ]
    return "# Step02-3 Route Marked Preview Audit\n\n" + "\n".join(f"- {k}: {summary.get(k)}" for k in keys) + "\n\nAudit-only: selector rules were not modified and no models were run.\n"


def write_outputs(out: Path, rows: Sequence[dict], risks: Sequence[dict], summary: dict) -> None:
    write_csv(out / "manifests" / "marked_visual_unit_manifest.csv", rows)
    write_jsonl(out / "manifests" / "marked_visual_unit_manifest.jsonl", rows)
    write_csv(out / "reports" / "risk_items.csv", risks)
    write_csv(out / "reports" / "route_marked_preview_summary.csv", [summary])
    write_json(out / "reports" / "route_marked_preview_summary.json", summary)
    write_json(out / "final_report" / "step02_3_route_marked_preview_audit_latest.json", summary)
    atomic_text(out / "final_report" / "step02_3_route_marked_preview_audit_latest.md", summary_md(summary))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selector_out = Path(args.selector_out).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    prepare_out(out)
    data, risks = load_selector(selector_out)
    if not data:
        summary = summarize([], risks, 0, missing_selector=True)
        summary["selector_out"] = str(selector_out)
        summary["output_dir"] = str(out)
        write_outputs(out, [], risks, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 3
    rows, _counters = build_marked_rows(data, risks)
    by_video: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        if row.get("visual_unit_type") == "video_frame":
            by_video[source_key(row)].append(row)
    for key, group in by_video.items():
        if not any(boolish(r.get("is_yoloe")) for r in group):
            add_risk(risks, "ERROR", "video_missing_yoloe", group[0], "Video has frames but no YOLOE route.")
        if any(boolish(r.get("is_yoloe")) for r in group) and not any(boolish(r.get("is_high_value")) for r in group):
            add_risk(risks, "WARNING", "video_yoloe_no_high_value", group[0], "Video has YOLOE route but no high_value route.")
        if any(boolish(r.get("is_ocr_trigger")) for r in group) and not any(boolish(r.get("is_high_value")) for r in group):
            add_risk(risks, "WARNING", "video_only_ocr_no_high_value", group[0], "Video has OCR_TRIGGER route but no high_value route.")
    contact_sheet_count = write_contact_sheets(out, rows, risks)
    summary = summarize(rows, risks, contact_sheet_count)
    summary["selector_out"] = str(selector_out)
    summary["output_dir"] = str(out)
    write_outputs(out, rows, risks, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
