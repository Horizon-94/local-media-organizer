#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit high_value distribution in Step02-3 selector output.

Read-only with respect to selector output. Does not run models or change routes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


SCRIPT_VERSION = "step02_3_high_value_distribution_audit_v1_20260708"
EXPECTED_TOTAL_VISUAL_UNITS = 1628
EXPECTED_VIDEOS_WITH_FRAMES = 97
EXPECTED_HIGH_VALUE_TOTAL = 148


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit high_value distribution from Step02-3 selector output")
    p.add_argument("--selector-out", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--high-value-per-video-soft-max", type=int, default=5)
    p.add_argument("--top5-share-warning", type=float, default=0.35)
    p.add_argument("--top10-share-warning", type=float, default=0.50)
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


def fields(preferred: Sequence[str], rows: Sequence[dict]) -> List[str]:
    out = list(preferred)
    seen = set(out)
    for row in rows:
        for key in row:
            if key not in seen:
                out.append(key)
                seen.add(key)
    return out


def write_csv(path: Path, rows: Sequence[dict], preferred: Sequence[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields(preferred, rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    tmp.replace(path)


def prepare_out(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
    for sub in ["reports", "manifests", "tests"]:
        (out / sub).mkdir(parents=True, exist_ok=True)


def selector_files(selector_out: Path) -> Dict[str, Path]:
    return {
        "decision": selector_out / "manifests" / "visual_unit_route_decision_manifest.csv",
        "yoloe": selector_out / "queues" / "yoloe_visual_units.csv",
        "high_value": selector_out / "queues" / "high_value_visual_units.csv",
        "ocr_trigger": selector_out / "queues" / "ocr_trigger_visual_units.csv",
    }


def boolish(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def intish(value: object, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(str(value)))
    except ValueError:
        return default


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


def pct(values: Sequence[int], q: float) -> int:
    if not values:
        return 0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return int(xs[idx])


def median(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    mid = len(xs) // 2
    if len(xs) % 2:
        return float(xs[mid])
    return (xs[mid - 1] + xs[mid]) / 2.0


def risk(risks: List[dict], level: str, risk_type: str, metric_name: str, metric_value: object, threshold: object, explanation: str, row: Optional[dict] = None) -> None:
    row = row or {}
    risks.append({
        "risk_id": f"risk_{len(risks) + 1:05d}",
        "risk_level": level,
        "risk_type": risk_type,
        "source_video_id": row.get("source_video_id", ""),
        "parent_source_file_id": row.get("parent_source_file_id", ""),
        "source_relative_path": row.get("source_video_relative_path") or row.get("source_relative_path", ""),
        "visual_unit_id": row.get("visual_unit_id", ""),
        "metric_name": metric_name,
        "metric_value": metric_value,
        "threshold": threshold,
        "explanation": explanation,
    })


def audit(selector_out: Path, args: argparse.Namespace) -> Tuple[dict, Dict[str, List[dict]]]:
    files = selector_files(selector_out)
    missing = [str(p) for p in files.values() if not p.exists()]
    risks: List[dict] = []
    if missing:
        for p in missing:
            risk(risks, "ERROR", "selector_output_missing", "missing_file", p, "exists", "Cannot find selector queue or manifest file.")
        return {"script_version": SCRIPT_VERSION, "failure_items_count": 0, "error_risk_count": len(risks), "warning_risk_count": 0}, {"risk_items": risks}

    decisions = read_csv(files["decision"])
    yoloe = read_csv(files["yoloe"])
    high = read_csv(files["high_value"])
    ocr = read_csv(files["ocr_trigger"])
    by_id = {r.get("visual_unit_id", ""): r for r in decisions if r.get("visual_unit_id")}
    yoloe_ids = route_ids(yoloe)
    high_ids = route_ids(high)
    ocr_ids = route_ids(ocr)

    video_rows = [r for r in decisions if r.get("visual_unit_type") == "video_frame"]
    video_groups: Dict[str, List[dict]] = defaultdict(list)
    for row in video_rows:
        video_groups[source_key(row)].append(row)
    yoloe_by_video = group_route(yoloe, "video_frame")
    high_by_video = group_route(high, "video_frame")
    ocr_by_video = group_route(ocr, "video_frame")

    route_reason_empty_count = sum(1 for r in [*yoloe, *high, *ocr] if not r.get("route_reason"))
    lineage_missing_count = 0
    for row in [*yoloe, *high, *ocr]:
        missing_lineage = [f for f in ["parent_source_file_id", "parent_source_content_id", "parent_source_path_at_processing_time", "source_relative_path"] if not row.get(f)]
        if missing_lineage:
            lineage_missing_count += 1

    high_not_in_yoloe = sorted(high_ids - yoloe_ids)
    ocr_not_in_yoloe = sorted(ocr_ids - yoloe_ids)
    overlap = sorted(high_ids & ocr_ids)
    for vid in high_not_in_yoloe:
        risk(risks, "ERROR", "high_value_not_in_yoloe", "membership", "not_in_yoloe", "in_yoloe", "high_value item is not present in YOLOE.", by_id.get(vid, {"visual_unit_id": vid}))
    for vid in ocr_not_in_yoloe:
        risk(risks, "ERROR", "ocr_not_in_yoloe", "membership", "not_in_yoloe", "in_yoloe", "OCR_TRIGGER item is not present in YOLOE.", by_id.get(vid, {"visual_unit_id": vid}))
    for vid in overlap:
        risk(risks, "ERROR", "high_value_ocr_overlap", "overlap", "true", "false", "high_value and OCR_TRIGGER overlap.", by_id.get(vid, {"visual_unit_id": vid}))
    if route_reason_empty_count:
        risk(risks, "ERROR", "route_reason_empty", "route_reason_empty_count", route_reason_empty_count, 0, "One or more route rows have empty route_reason.")
    if lineage_missing_count:
        risk(risks, "ERROR", "lineage_missing", "lineage_missing_count", lineage_missing_count, 0, "One or more route rows miss required lineage fields.")

    coverage_rows = []
    missing_high_rows = []
    high_value_by_video = []
    short_or_low_count = 0
    short_or_low_missing = 0
    for key in sorted(video_groups):
        frames = sorted(video_groups[key], key=lambda r: intish(r.get("time_position_ms")))
        base = frames[0]
        hv_rows = sorted(high_by_video.get(key, []), key=lambda r: intish(r.get("time_position_ms")))
        y_rows = yoloe_by_video.get(key, [])
        o_rows = ocr_by_video.get(key, [])
        times = [intish(r.get("time_position_ms")) for r in hv_rows]
        duration_ms = max(0, intish(frames[-1].get("time_position_ms")) - intish(frames[0].get("time_position_ms"))) if len(frames) > 1 else 0
        duration_s = round(duration_ms / 1000.0, 3)
        is_short = duration_s <= 5
        low_frame = len(frames) <= 5
        if is_short or low_frame:
            short_or_low_count += 1
        if (is_short or low_frame) and not hv_rows:
            short_or_low_missing += 1
        hv_gap = max((b - a for a, b in zip(times, times[1:])), default=0)
        risk_level, risk_reason = video_risk_reason(hv_rows, len(frames), duration_s, is_short, low_frame)
        row = {
            "source_video_id": base.get("source_video_id", ""),
            "parent_source_file_id": base.get("parent_source_file_id", ""),
            "parent_source_content_id": base.get("parent_source_content_id", ""),
            "source_relative_path": base.get("source_video_relative_path") or base.get("source_relative_path", ""),
            "parent_source_path_at_processing_time": base.get("parent_source_path_at_processing_time", ""),
            "video_frame_count": len(frames),
            "yoloe_count": len(y_rows),
            "high_value_count": len(hv_rows),
            "ocr_trigger_count": len(o_rows),
            "has_yoloe": bool(y_rows),
            "has_high_value": bool(hv_rows),
            "has_ocr_trigger": bool(o_rows),
            "first_high_value_time_ms": times[0] if times else "",
            "last_high_value_time_ms": times[-1] if times else "",
            "max_high_value_gap_ms": hv_gap,
            "video_duration_seconds": duration_s,
            "is_short_video": is_short,
            "low_frame_video": low_frame,
            "risk_level": risk_level,
            "risk_reason": risk_reason,
        }
        coverage_rows.append(row)
        high_value_by_video.append({
            "source_video_id": row["source_video_id"],
            "parent_source_file_id": row["parent_source_file_id"],
            "source_relative_path": row["source_relative_path"],
            "video_frame_count": len(frames),
            "high_value_count": len(hv_rows),
        })
        if not hv_rows:
            candidate_reason, suggested = missing_reason(row)
            missing = {
                "source_video_id": row["source_video_id"],
                "parent_source_file_id": row["parent_source_file_id"],
                "source_relative_path": row["source_relative_path"],
                "parent_source_path_at_processing_time": row["parent_source_path_at_processing_time"],
                "video_frame_count": row["video_frame_count"],
                "yoloe_count": row["yoloe_count"],
                "ocr_trigger_count": row["ocr_trigger_count"],
                "video_duration_seconds": row["video_duration_seconds"],
                "candidate_reason": candidate_reason,
                "suggested_action": suggested,
            }
            missing_high_rows.append(missing)

    counts = [int(r["high_value_count"]) for r in coverage_rows]
    high_video_total = sum(counts)
    top_counts = sorted(counts, reverse=True)
    top1 = top_counts[0] if top_counts else 0
    top5 = sum(top_counts[:5])
    top10 = sum(top_counts[:10])
    top5_share = round(top5 / high_video_total, 6) if high_video_total else 0.0
    top10_share = round(top10 / high_video_total, 6) if high_video_total else 0.0

    buckets = bucket_rows(counts, len(coverage_rows))
    high_items = [{**r, "decision_source_relative_path": (by_id.get(r.get("visual_unit_id", ""), {}).get("source_video_relative_path") or by_id.get(r.get("visual_unit_id", ""), {}).get("source_relative_path", ""))} for r in high]

    videos_with_high = sum(1 for c in counts if c > 0)
    if len(coverage_rows) != EXPECTED_VIDEOS_WITH_FRAMES:
        risk(risks, "WARNING", "videos_with_frames_mismatch", "videos_with_frames", len(coverage_rows), EXPECTED_VIDEOS_WITH_FRAMES, "Recomputed video denominator differs from expected prior selector summary.")
    if len(decisions) != EXPECTED_TOTAL_VISUAL_UNITS:
        risk(risks, "WARNING", "total_visual_unit_count_mismatch", "total_visual_unit_count", len(decisions), EXPECTED_TOTAL_VISUAL_UNITS, "Recomputed visual unit count differs from expected prior selector summary.")
    if len(high) != EXPECTED_HIGH_VALUE_TOTAL:
        risk(risks, "WARNING", "high_value_total_mismatch", "high_value_total", len(high), EXPECTED_HIGH_VALUE_TOTAL, "Recomputed high_value count differs from expected prior selector summary.")
    if missing_high_rows:
        risk(risks, "WARNING", "videos_missing_high_value", "videos_missing_high_value_count", len(missing_high_rows), 0, "Some videos with frames have no high_value semantic anchor.")
    if counts and max(counts) > args.high_value_per_video_soft_max:
        risk(risks, "WARNING", "high_value_per_video_max_exceeds_soft_max", "high_value_per_video_max", max(counts), args.high_value_per_video_soft_max, "A video exceeds the high_value per-video soft max.")
    if top5_share > args.top5_share_warning:
        risk(risks, "WARNING", "high_value_top5_share_exceeds_threshold", "high_value_top5_share", top5_share, args.top5_share_warning, "Top 5 videos contain more than configured share of video high_value anchors.")
    if top10_share > args.top10_share_warning:
        risk(risks, "WARNING", "high_value_top10_share_exceeds_threshold", "high_value_top10_share", top10_share, args.top10_share_warning, "Top 10 videos contain more than configured share of video high_value anchors.")
    if short_or_low_missing:
        risk(risks, "WARNING", "short_or_low_frame_missing_high_value", "short_or_low_frame_missing_high_value", short_or_low_missing, 0, "Some short or low-frame videos have no high_value anchor.")
    if len({source_key(r) for r in yoloe if r.get("visual_unit_type") == "video_frame"}) == len(coverage_rows):
        risk(risks, "INFO", "yoloe_coverage_complete", "videos_with_yoloe", len(coverage_rows), len(coverage_rows), "YOLOE coverage is complete for videos with frames.")
    risk(risks, "INFO", "ocr_not_expected_for_every_video", "ocr_video_count", len(ocr_by_video), "not_applicable", "OCR coverage is metadata-triggered and not expected for every video.")

    error_count = sum(1 for r in risks if r["risk_level"] == "ERROR")
    warning_count = sum(1 for r in risks if r["risk_level"] == "WARNING")
    summary = {
        "script_version": SCRIPT_VERSION,
        "selector_out": str(selector_out),
        "total_visual_unit_count": len(decisions),
        "videos_with_frames": len(coverage_rows),
        "videos_with_high_value": videos_with_high,
        "videos_missing_high_value_count": len(missing_high_rows),
        "videos_missing_high_value_list": [r["source_video_id"] for r in missing_high_rows],
        "high_value_total": len(high),
        "high_value_video_total": high_video_total,
        "high_value_per_video_min": min(counts) if counts else 0,
        "high_value_per_video_max": max(counts) if counts else 0,
        "high_value_per_video_avg": round(sum(counts) / len(counts), 6) if counts else 0.0,
        "high_value_per_video_median": median(counts),
        "high_value_per_video_p95": pct(counts, 0.95),
        "high_value_top1_video_count": top1,
        "high_value_top5_video_count": top5,
        "high_value_top10_video_count": top10,
        "high_value_top5_share": top5_share,
        "high_value_top10_share": top10_share,
        "short_or_low_frame_video_count": short_or_low_count,
        "short_or_low_frame_with_high_value": short_or_low_count - short_or_low_missing,
        "short_or_low_frame_missing_high_value": short_or_low_missing,
        "high_value_not_in_yoloe_count": len(high_not_in_yoloe),
        "ocr_not_in_yoloe_count": len(ocr_not_in_yoloe),
        "high_value_ocr_overlap_count": len(overlap),
        "route_reason_empty_count": route_reason_empty_count,
        "lineage_missing_count": lineage_missing_count,
        "warning_risk_count": warning_count,
        "error_risk_count": error_count,
        "failure_items_count": 0,
        "blocked_items_count": 0,
    }
    return summary, {
        "risk_items": risks,
        "coverage_rows": coverage_rows,
        "missing_high_rows": missing_high_rows,
        "bucket_rows": buckets,
        "high_items": high_items,
        "high_value_by_video": high_value_by_video,
    }


def group_route(rows: Sequence[dict], unit_type: str) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        if row.get("visual_unit_type") == unit_type:
            out[source_key(row)].append(row)
    return out


def video_risk_reason(hv_rows: Sequence[dict], frame_count: int, duration_s: float, is_short: bool, low_frame: bool) -> Tuple[str, str]:
    if hv_rows:
        return "INFO", "has_high_value_anchor"
    if is_short or low_frame:
        return "WARNING", "short_or_low_frame_video_missing_high_value"
    if duration_s <= 0:
        return "WARNING", "missing_duration_and_missing_high_value"
    return "WARNING", "video_missing_high_value_anchor"


def missing_reason(row: dict) -> Tuple[str, str]:
    if not row.get("parent_source_file_id") or row.get("video_duration_seconds") == "":
        return "missing_lineage_or_duration", "blocked_missing_duration_or_lineage"
    if row.get("is_short_video") == "True" or row.get("is_short_video") is True or int(row.get("video_frame_count", 0)) <= 5:
        return "short_or_low_frame_no_anchor", "possibly_ok_low_information_short_clip"
    if int(row.get("ocr_trigger_count", 0)) > 0:
        return "ocr_route_present_but_no_semantic_anchor", "consider_summary_anchor"
    return "video_has_frames_but_no_high_value", "review_needed"


def bucket_rows(counts: Sequence[int], total_videos: int) -> List[dict]:
    specs = [
        ("0", lambda c: c == 0),
        ("1", lambda c: c == 1),
        ("2", lambda c: c == 2),
        ("3", lambda c: c == 3),
        ("4", lambda c: c == 4),
        ("5", lambda c: c == 5),
        ("6-10", lambda c: 6 <= c <= 10),
        ("11-20", lambda c: 11 <= c <= 20),
        (">20", lambda c: c > 20),
    ]
    rows = []
    for label, pred in specs:
        n = sum(1 for c in counts if pred(c))
        rows.append({
            "bucket": label,
            "video_count": n,
            "percentage_of_videos_with_frames": round(n / total_videos, 6) if total_videos else 0.0,
        })
    return rows


def summary_md(summary: dict) -> str:
    keys = [
        "total_visual_unit_count", "videos_with_frames", "videos_with_high_value", "videos_missing_high_value_count",
        "high_value_total", "high_value_per_video_min", "high_value_per_video_median",
        "high_value_per_video_avg", "high_value_per_video_p95", "high_value_per_video_max",
        "high_value_top5_share", "high_value_top10_share", "short_or_low_frame_video_count",
        "short_or_low_frame_missing_high_value", "high_value_not_in_yoloe_count",
        "high_value_ocr_overlap_count", "route_reason_empty_count", "lineage_missing_count",
        "warning_risk_count", "error_risk_count",
    ]
    lines = ["# Step02-3 High Value Distribution Audit", ""]
    lines.extend(f"- {k}: {summary.get(k)}" for k in keys)
    lines.append("")
    lines.append("Audit-only: selector rules were not modified and no models were run.")
    return "\n".join(lines) + "\n"


def write_outputs(out: Path, summary: dict, artifacts: Dict[str, List[dict]]) -> None:
    write_json(out / "reports" / "high_value_distribution_audit_summary.json", summary)
    atomic_text(out / "reports" / "high_value_distribution_audit_summary.md", summary_md(summary))
    write_csv(out / "reports" / "risk_items.csv", artifacts["risk_items"])
    write_csv(out / "manifests" / "high_value_by_video.csv", artifacts["high_value_by_video"])
    write_csv(out / "manifests" / "videos_missing_high_value.csv", artifacts["missing_high_rows"])
    write_csv(out / "manifests" / "high_value_distribution_buckets.csv", artifacts["bucket_rows"])
    write_csv(out / "manifests" / "high_value_items_with_source.csv", artifacts["high_items"])
    write_csv(out / "manifests" / "video_anchor_coverage.csv", artifacts["coverage_rows"])
    sample = {
        "summary_subset": {
            "videos_with_frames": summary.get("videos_with_frames"),
            "videos_missing_high_value_count": summary.get("videos_missing_high_value_count"),
            "high_value_total": summary.get("high_value_total"),
        },
        "first_missing_high_value_rows": artifacts["missing_high_rows"][:5],
    }
    write_json(out / "tests" / "optional_debug_sample.json", sample)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selector_out = Path(args.selector_out).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    prepare_out(out)
    summary, artifacts = audit(selector_out, args)
    summary["output_dir"] = str(out)
    write_outputs(out, summary, artifacts)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
