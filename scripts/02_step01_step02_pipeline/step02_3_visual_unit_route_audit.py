#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit Step02-3 selector output without changing selector rules or queues."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


SCRIPT_VERSION = "step02_3_visual_unit_route_audit_v1_20260708"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit Step02-3 visual-unit route selector output")
    p.add_argument("--selector-out", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--yoloe-max-gap-ms", type=int, default=10000)
    p.add_argument("--high-value-per-video-soft-max", type=int, default=5)
    p.add_argument("--ocr-per-video-soft-max", type=int, default=8)
    p.add_argument("--no-open", action="store_true")
    return p.parse_args(argv)


def read_csv(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


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
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    tmp.replace(path)


def prepare_out(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
    for sub in ["reports", "manifests", "final_report"]:
        (out / sub).mkdir(parents=True, exist_ok=True)


def required_files(selector_out: Path) -> Dict[str, Path]:
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
    return int(float(str(value)))


def source_key(row: dict) -> str:
    return (
        row.get("source_video_id")
        or row.get("parent_source_file_id")
        or row.get("source_video_relative_path")
        or row.get("source_relative_path")
        or row.get("parent_source_path_at_processing_time")
        or "unknown_source"
    )


def source_path(row: dict) -> str:
    return row.get("source_video_relative_path") or row.get("source_relative_path") or row.get("parent_source_path_at_processing_time") or ""


def add_risk(
    risks: List[dict],
    severity: str,
    risk_type: str,
    row: Optional[dict],
    metric_name: str,
    metric_value: object,
    threshold: object,
    explanation: str,
) -> None:
    row = row or {}
    risks.append({
        "risk_id": f"risk_{len(risks) + 1:05d}",
        "severity": severity,
        "risk_type": risk_type,
        "source_video_id": row.get("source_video_id", ""),
        "parent_source_file_id": row.get("parent_source_file_id", ""),
        "source_relative_path": source_path(row),
        "visual_unit_id": row.get("visual_unit_id", ""),
        "metric_name": metric_name,
        "metric_value": metric_value,
        "threshold": threshold,
        "explanation": explanation,
    })


def percentile(values: Sequence[int], q: float) -> int:
    if not values:
        return 0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return int(xs[idx])


def audit_routes(selector_out: Path, args: argparse.Namespace) -> Tuple[dict, Dict[str, List[dict]]]:
    files = required_files(selector_out)
    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        risks: List[dict] = []
        for p in missing:
            add_risk(risks, "error", "selector_output_missing_or_unreadable", None, "missing_file", p, "exists", "Required selector output is missing.")
        summary = {
            "script_version": SCRIPT_VERSION,
            "failure_items_count": 0,
            "selector_output_missing_count": len(missing),
            "error_risk_count": len(risks),
            "warning_risk_count": 0,
        }
        return summary, {"risk_items": risks}

    decision_rows = read_csv(files["decision"])
    yoloe_rows = read_csv(files["yoloe"])
    high_rows = read_csv(files["high_value"])
    ocr_rows = read_csv(files["ocr_trigger"])
    risks: List[dict] = []

    yoloe_ids = {r.get("visual_unit_id", "") for r in yoloe_rows if r.get("visual_unit_id")}
    high_ids = {r.get("visual_unit_id", "") for r in high_rows if r.get("visual_unit_id")}
    ocr_ids = {r.get("visual_unit_id", "") for r in ocr_rows if r.get("visual_unit_id")}
    by_id = {r.get("visual_unit_id", ""): r for r in decision_rows if r.get("visual_unit_id")}

    route_rows = [*yoloe_rows, *high_rows, *ocr_rows]
    visual_unit_id_missing_count = 0
    route_reason_empty_count = 0
    lineage_missing_count = 0
    for row in route_rows:
        if not row.get("visual_unit_id"):
            visual_unit_id_missing_count += 1
            add_risk(risks, "error", "visual_unit_id_missing", row, "visual_unit_id", "", "non_empty", "Route row has no visual_unit_id.")
        if not row.get("route_reason"):
            route_reason_empty_count += 1
            add_risk(risks, "error", "route_reason_empty", row, "route_reason", "", "non_empty", "Route row has no route reason.")
        missing_lineage = [f for f in ["parent_source_file_id", "parent_source_content_id", "parent_source_path_at_processing_time", "source_relative_path"] if not row.get(f)]
        if missing_lineage:
            lineage_missing_count += 1
            add_risk(risks, "error", "lineage_missing", row, "missing_lineage_fields", ",".join(missing_lineage), "none_missing", "Route row is missing lineage fields.")

    high_not_yoloe = sorted(high_ids - yoloe_ids)
    ocr_not_yoloe = sorted(ocr_ids - yoloe_ids)
    overlap = sorted(high_ids & ocr_ids)
    for vid in high_not_yoloe:
        add_risk(risks, "error", "high_value_not_in_yoloe", by_id.get(vid, {"visual_unit_id": vid}), "membership", "not_in_yoloe", "in_yoloe", "high_value row is not present in YOLOE queue.")
    for vid in ocr_not_yoloe:
        add_risk(risks, "error", "ocr_not_in_yoloe", by_id.get(vid, {"visual_unit_id": vid}), "membership", "not_in_yoloe", "in_yoloe", "OCR_TRIGGER row is not present in YOLOE queue.")
    for vid in overlap:
        add_risk(risks, "error", "high_value_ocr_overlap", by_id.get(vid, {"visual_unit_id": vid}), "overlap", "true", "false", "Visual unit appears in both high_value and OCR_TRIGGER.")

    video_units = [r for r in decision_rows if r.get("visual_unit_type") == "video_frame"]
    image_units = [r for r in decision_rows if r.get("visual_unit_type") == "image_preview"]
    yoloe_video_rows = [r for r in yoloe_rows if r.get("visual_unit_type") == "video_frame"]
    yoloe_image_rows = [r for r in yoloe_rows if r.get("visual_unit_type") == "image_preview"]

    per_video, gap_rows, videos_missing = audit_video_gaps(video_units, yoloe_video_rows, risks, args.yoloe_max_gap_ms)
    gap_values = [intish(r.get("max_yoloe_gap_ms")) for r in gap_rows if r.get("gap_status") != "video_missing_yoloe"]
    high_dist = distribution_rows(high_rows, "high_value")
    ocr_dist = distribution_rows(ocr_rows, "ocr_trigger")
    audit_distribution(high_dist, risks, args.high_value_per_video_soft_max, "high_value_per_video_exceeds_soft_max", "high_value")
    audit_distribution(ocr_dist, risks, args.ocr_per_video_soft_max, "ocr_per_video_exceeds_soft_max", "ocr_trigger")
    audit_concentration(high_dist, risks, "high_value")

    per_media = per_media_kind(decision_rows, yoloe_rows, high_rows, ocr_rows)
    reason_audit = selection_reason_audit(yoloe_rows, high_rows, ocr_rows)
    contract_violations = [r for r in risks if r["severity"] == "error" and r["risk_type"] in {
        "route_reason_empty",
        "high_value_not_in_yoloe",
        "ocr_not_in_yoloe",
        "high_value_ocr_overlap",
        "lineage_missing",
        "visual_unit_id_missing",
        "selector_output_missing_or_unreadable",
    }]

    warning_count = sum(1 for r in risks if r["severity"] == "warning")
    error_count = sum(1 for r in risks if r["severity"] == "error")
    summary = {
        "script_version": SCRIPT_VERSION,
        "selector_out": str(selector_out),
        "total_visual_unit_count": len(decision_rows),
        "video_visual_unit_count": len(video_units),
        "image_visual_unit_count": len(image_units),
        "yoloe_queue_count": len(yoloe_rows),
        "high_value_queue_count": len(high_rows),
        "ocr_trigger_queue_count": len(ocr_rows),
        "videos_with_frames": len({source_key(r) for r in video_units}),
        "videos_with_yoloe": len({source_key(r) for r in yoloe_video_rows}),
        "videos_missing_yoloe": videos_missing,
        "images_with_visual_units": len({source_key(r) for r in image_units}),
        "images_with_yoloe": len({source_key(r) for r in yoloe_image_rows}),
        "high_value_subset_of_yoloe": not high_not_yoloe,
        "ocr_subset_of_yoloe": not ocr_not_yoloe,
        "high_value_ocr_overlap_count": len(overlap),
        "high_value_not_in_yoloe_count": len(high_not_yoloe),
        "ocr_not_in_yoloe_count": len(ocr_not_yoloe),
        "route_reason_empty_count": route_reason_empty_count,
        "visual_unit_id_missing_count": visual_unit_id_missing_count,
        "lineage_missing_count": lineage_missing_count,
        "yoloe_gap_p50_ms": percentile(gap_values, 0.50),
        "yoloe_gap_p90_ms": percentile(gap_values, 0.90),
        "yoloe_gap_p95_ms": percentile(gap_values, 0.95),
        "yoloe_gap_max_ms": max(gap_values) if gap_values else 0,
        "warning_risk_count": warning_count,
        "error_risk_count": error_count,
        "failure_items_count": 0,
    }
    return summary, {
        "risk_items": risks,
        "per_video": per_video,
        "per_media": per_media,
        "gap_rows": gap_rows,
        "high_dist": high_dist,
        "ocr_dist": ocr_dist,
        "contract_violations": contract_violations,
        "reason_audit": reason_audit,
    }


def audit_video_gaps(video_units: Sequence[dict], yoloe_video_rows: Sequence[dict], risks: List[dict], threshold: int) -> Tuple[List[dict], List[dict], List[str]]:
    all_by_source: Dict[str, List[dict]] = defaultdict(list)
    selected_by_source: Dict[str, List[dict]] = defaultdict(list)
    for row in video_units:
        all_by_source[source_key(row)].append(row)
    for row in yoloe_video_rows:
        selected_by_source[source_key(row)].append(row)

    per_video = []
    gap_rows = []
    missing = []
    for key in sorted(all_by_source):
        frames = sorted(all_by_source[key], key=lambda r: intish(r.get("time_position_ms")))
        selected = sorted(selected_by_source.get(key, []), key=lambda r: intish(r.get("time_position_ms")))
        if not selected:
            missing.append(key)
            add_risk(risks, "error", "video_missing_yoloe", frames[0], "yoloe_count", 0, ">=1", "Video has frames but no YOLOE selected visual unit.")
            status = "video_missing_yoloe"
            max_gap = 0
        elif len(selected) <= 1 or (intish(frames[-1].get("time_position_ms")) - intish(frames[0].get("time_position_ms"))) <= threshold:
            status = "single_frame_or_short_video"
            max_gap = 0
        else:
            times = [intish(r.get("time_position_ms")) for r in selected]
            max_gap = max((b - a for a, b in zip(times, times[1:])), default=0)
            status = "ok" if max_gap <= threshold else "gap_exceeds_threshold"
            if max_gap > threshold:
                add_risk(risks, "warning", "yoloe_gap_exceeds_threshold", selected[0], "max_yoloe_gap_ms", max_gap, threshold, "YOLOE selected frames leave a gap above the configured threshold.")
        base = frames[0]
        per_video.append({
            "source_key": key,
            "source_video_id": base.get("source_video_id", ""),
            "parent_source_file_id": base.get("parent_source_file_id", ""),
            "source_relative_path": source_path(base),
            "frame_count": len(frames),
            "yoloe_count": len(selected),
            "high_value_count": sum(1 for r in frames if boolish(r.get("selected_for_high_value"))),
            "ocr_trigger_count": sum(1 for r in frames if boolish(r.get("selected_for_ocr_trigger"))),
            "max_yoloe_gap_ms": max_gap,
            "gap_status": status,
        })
        gap_rows.append({
            "source_key": key,
            "source_video_id": base.get("source_video_id", ""),
            "parent_source_file_id": base.get("parent_source_file_id", ""),
            "source_relative_path": source_path(base),
            "frame_count": len(frames),
            "yoloe_count": len(selected),
            "max_yoloe_gap_ms": max_gap,
            "gap_status": status,
        })
    return per_video, gap_rows, missing


def distribution_rows(route_rows: Sequence[dict], route_name: str) -> List[dict]:
    groups: Dict[str, dict] = {}
    for row in route_rows:
        key = source_key(row)
        g = groups.setdefault(key, {
            "route_name": route_name,
            "visual_unit_type": row.get("visual_unit_type", ""),
            "source_key": key,
            "source_video_id": row.get("source_video_id", ""),
            "parent_source_file_id": row.get("parent_source_file_id", ""),
            "source_relative_path": source_path(row),
            "selected_count": 0,
        })
        g["selected_count"] += 1
    return sorted(groups.values(), key=lambda r: (-int(r["selected_count"]), r["source_key"]))


def audit_distribution(rows: Sequence[dict], risks: List[dict], threshold: int, risk_type: str, route_name: str) -> None:
    for row in rows:
        if row["visual_unit_type"] == "video_frame" and int(row["selected_count"]) > threshold:
            add_risk(risks, "warning", risk_type, row, f"{route_name}_count", row["selected_count"], threshold, f"{route_name} count exceeds configured per-video soft max.")


def audit_concentration(rows: Sequence[dict], risks: List[dict], route_name: str) -> None:
    total = sum(int(r["selected_count"]) for r in rows)
    if total <= 0:
        return
    for row in rows:
        frac = int(row["selected_count"]) / total
        if frac > 0.20:
            add_risk(risks, "warning", f"{route_name}_source_concentration", row, f"{route_name}_share", round(frac, 4), "0.20", f"One source contains more than 20% of all {route_name} selections.")


def per_media_kind(decision_rows: Sequence[dict], yoloe: Sequence[dict], high: Sequence[dict], ocr: Sequence[dict]) -> List[dict]:
    kinds = sorted({r.get("visual_unit_type", "") for r in decision_rows if r.get("visual_unit_type")})
    out = []
    for kind in kinds:
        out.append({
            "visual_unit_type": kind,
            "visual_unit_count": sum(1 for r in decision_rows if r.get("visual_unit_type") == kind),
            "yoloe_count": sum(1 for r in yoloe if r.get("visual_unit_type") == kind),
            "high_value_count": sum(1 for r in high if r.get("visual_unit_type") == kind),
            "ocr_trigger_count": sum(1 for r in ocr if r.get("visual_unit_type") == kind),
        })
    return out


def selection_reason_audit(yoloe: Sequence[dict], high: Sequence[dict], ocr: Sequence[dict]) -> List[dict]:
    counts = Counter()
    for route, rows in [("yoloe", yoloe), ("high_value", high), ("ocr_trigger", ocr)]:
        for row in rows:
            counts[(route, row.get("route_reason", ""))] += 1
    return [{"route_name": route, "route_reason": reason, "count": count} for (route, reason), count in sorted(counts.items())]


def summary_md(summary: dict) -> str:
    lines = ["# Step02-3 Route Audit Summary", ""]
    for key in [
        "total_visual_unit_count", "yoloe_queue_count", "high_value_queue_count", "ocr_trigger_queue_count",
        "videos_with_frames", "videos_with_yoloe", "videos_missing_yoloe", "yoloe_gap_p95_ms",
        "yoloe_gap_max_ms", "high_value_ocr_overlap_count", "high_value_not_in_yoloe_count",
        "ocr_not_in_yoloe_count", "route_reason_empty_count", "warning_risk_count", "error_risk_count",
        "failure_items_count",
    ]:
        lines.append(f"- {key}: {summary.get(key)}")
    lines.append("")
    lines.append("Audit-only: selector rules were not modified and no models were run.")
    return "\n".join(lines) + "\n"


def write_outputs(out: Path, summary: dict, artifacts: Dict[str, List[dict]]) -> None:
    write_json(out / "reports" / "step02_3_route_audit_summary.json", summary)
    write_text(out / "reports" / "step02_3_route_audit_summary.md", summary_md(summary))
    write_csv(out / "reports" / "risk_items.csv", artifacts.get("risk_items", []))
    write_jsonl(out / "reports" / "risk_items.jsonl", artifacts.get("risk_items", []))
    write_csv(out / "manifests" / "per_video_route_audit.csv", artifacts.get("per_video", []))
    write_csv(out / "manifests" / "per_media_kind_route_audit.csv", artifacts.get("per_media", []))
    write_csv(out / "manifests" / "yoloe_gap_audit.csv", artifacts.get("gap_rows", []))
    write_csv(out / "manifests" / "high_value_distribution.csv", artifacts.get("high_dist", []))
    write_csv(out / "manifests" / "ocr_trigger_distribution.csv", artifacts.get("ocr_dist", []))
    write_csv(out / "manifests" / "route_contract_violations.csv", artifacts.get("contract_violations", []))
    write_csv(out / "manifests" / "selection_reason_audit.csv", artifacts.get("reason_audit", []))
    write_json(out / "final_report" / "step02_3_route_audit_final_report_latest.json", summary)
    write_text(out / "final_report" / "step02_3_route_audit_final_report_latest.md", summary_md(summary))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selector_out = Path(args.selector_out).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    prepare_out(out)
    summary, artifacts = audit_routes(selector_out, args)
    summary["selector_out"] = str(selector_out)
    summary["output_dir"] = str(out)
    write_outputs(out, summary, artifacts)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
