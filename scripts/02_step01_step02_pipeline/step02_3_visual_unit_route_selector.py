#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step02-3 production visual-unit route selector.

This selector converts existing Step02 visual units into pre-model queues. It
does not run YOLOE, Qwen-VL, OCR, Embedding, or modify source media/indexes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step02_3_visual_unit_route_policies import POLICY_VERSION, route_visual_units


SCRIPT_VERSION = "step02_3_visual_unit_route_selector_v1_20260708"

NORMALIZED_FIELDS = [
    "visual_unit_id",
    "visual_unit_type",
    "visual_file",
    "visual_file_sha256",
    "parent_source_file_id",
    "parent_source_content_id",
    "parent_source_path_at_processing_time",
    "parent_media_kind",
    "source_relative_path",
    "source_video_id",
    "source_video_relative_path",
    "time_position_ms",
    "frame_index",
    "preview_role",
    "producer_step",
    "input_manifest_path",
]

QUEUE_FIELDS = [
    "route_name",
    "route_policy_version",
    "route_reason",
    "route_rank_within_source",
    *NORMALIZED_FIELDS,
    "run_invocation_id",
    "created_at",
]

DECISION_EXTRA_FIELDS = [
    "selected_for_yoloe",
    "selected_for_high_value",
    "selected_for_ocr_trigger",
    "route_reason_yoloe",
    "route_reason_high_value",
    "route_reason_ocr_trigger",
    "route_conflict_resolution",
    "blocked_reason",
    "failure_reason",
]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step02-3 visual-unit route selector")
    p.add_argument("--video-frame-manifest", required=True)
    p.add_argument("--image-visual-unit-manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--policy", default=POLICY_VERSION)
    p.add_argument("--yoloe-coverage-ms", type=int, default=6000)
    p.add_argument("--yoloe-max-gap-ms", type=int, default=10000)
    p.add_argument("--high-value-min-gap-ms", type=int, default=20000)
    p.add_argument("--run-phase", default="manual")
    p.add_argument("--no-open", action="store_true")
    return p.parse_args(argv)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def prepare_output(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
    for sub in ["queues", "manifests", "reports", "final_report/history"]:
        (out / sub).mkdir(parents=True, exist_ok=True)


def read_video_manifest(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: Path) -> List[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            rows.append(row)
    return rows


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def normalize_video_row(row: dict, manifest_path: Path, row_index: int) -> Tuple[dict, Optional[dict]]:
    frame_id = row.get("frame_id") or ""
    visual_unit_id = row.get("visual_unit_id") or (f"vu_video_frame_{frame_id}" if frame_id else f"vu_video_frame_row_{row_index:06d}")
    unit = {
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": "video_frame",
        "visual_file": row.get("frame_file") or row.get("visual_file") or "",
        "visual_file_sha256": row.get("frame_file_sha256") or row.get("visual_file_sha256") or "",
        "parent_source_file_id": row.get("parent_source_file_id") or "",
        "parent_source_content_id": row.get("parent_source_content_id") or "",
        "parent_source_path_at_processing_time": row.get("parent_source_path_at_processing_time") or row.get("source_video_path") or "",
        "parent_media_kind": row.get("parent_media_kind") or "video",
        "source_relative_path": row.get("step01_source_relative_path") or row.get("source_video_relative_path") or "",
        "source_video_id": row.get("source_video_id") or "",
        "source_video_relative_path": row.get("source_video_relative_path") or row.get("step01_source_relative_path") or "",
        "time_position_ms": row.get("estimated_frame_time_ms") or row.get("time_position_ms") or "",
        "frame_index": row.get("frame_index") or "",
        "preview_role": "",
        "producer_step": "step02_1_video_frame",
        "input_manifest_path": str(manifest_path),
    }
    passthrough_raw_fields(unit, row)
    return unit, lineage_block(unit, "video", row_index, row)


def normalize_image_row(row: dict, manifest_path: Path, row_index: int) -> Tuple[dict, Optional[dict]]:
    derivation_reason = ""
    visual_unit_id = row.get("visual_unit_id") or ""
    if not visual_unit_id:
        key = row.get("preview_artifact_id") or row.get("visual_file") or json.dumps(row, sort_keys=True, ensure_ascii=False)
        visual_unit_id = "vu_image_preview_" + stable_hash(str(key))
        derivation_reason = "visual_unit_id_derived_from_preview_artifact_or_visual_file"
    unit = {
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": "image_preview",
        "visual_file": row.get("visual_file") or "",
        "visual_file_sha256": row.get("visual_file_sha256") or "",
        "parent_source_file_id": row.get("parent_source_file_id") or "",
        "parent_source_content_id": row.get("parent_source_content_id") or "",
        "parent_source_path_at_processing_time": row.get("parent_source_path_at_processing_time") or "",
        "parent_media_kind": row.get("parent_media_kind") or "image",
        "source_relative_path": row.get("source_relative_path") or "",
        "source_video_id": "",
        "source_video_relative_path": "",
        "time_position_ms": row.get("time_position_ms") or "",
        "frame_index": "",
        "preview_role": row.get("preview_role") or "",
        "producer_step": row.get("producer_step") or "step02_2_image_preview",
        "input_manifest_path": str(manifest_path),
    }
    for optional_hint in ["ocr_hint", "text_hint", "screen_recording_hint", "preview_artifact_id"]:
        if optional_hint in row:
            unit[optional_hint] = row.get(optional_hint)
    if derivation_reason:
        unit["visual_unit_id_derivation_reason"] = derivation_reason
    passthrough_raw_fields(unit, row)
    return unit, lineage_block(unit, "image", row_index, row)


def passthrough_raw_fields(unit: dict, raw_row: dict) -> None:
    for key, value in raw_row.items():
        if key.startswith("_"):
            continue
        if key not in unit:
            unit[key] = value


def lineage_block(unit: dict, input_type: str, row_index: int, raw_row: dict) -> Optional[dict]:
    required = [
        "visual_unit_id",
        "visual_unit_type",
        "visual_file",
        "parent_source_file_id",
        "parent_source_content_id",
        "parent_source_path_at_processing_time",
        "parent_media_kind",
        "source_relative_path",
        "producer_step",
    ]
    if unit["visual_unit_type"] == "video_frame":
        required.extend(["source_video_id", "source_video_relative_path", "time_position_ms", "frame_index"])
    missing = [field for field in required if unit.get(field) in (None, "")]
    if not missing:
        return None
    return {
        "blocked_reason": "missing_required_lineage_field",
        "missing_fields": ",".join(missing),
        "input_type": input_type,
        "input_row_index": row_index,
        "visual_unit_id": unit.get("visual_unit_id", ""),
        "source_relative_path": unit.get("source_relative_path", ""),
        "source_row_json": json.dumps(raw_row, ensure_ascii=False, sort_keys=True),
    }


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    atomic_write_text(path, text)


def stable_field_union(preferred: Sequence[str], rows: Sequence[dict]) -> List[str]:
    fields = list(preferred)
    seen = set(fields)
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fields.append(key)
                seen.add(key)
    return fields


def write_csv(path: Path, rows: Sequence[dict], preferred_fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = stable_field_union(preferred_fields, rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    tmp.replace(path)


def build_route_rows(
    route_name: str,
    units: Sequence[dict],
    decisions: Dict[str, dict],
    run_invocation_id: str,
    created_at: str,
) -> List[dict]:
    selected_key = f"selected_for_{route_name}"
    reason_key = f"route_reason_{route_name}"
    if route_name == "ocr_trigger":
        selected_key = "selected_for_ocr_trigger"
        reason_key = "route_reason_ocr_trigger"
    rows = []
    by_source_rank: Dict[str, int] = defaultdict(int)
    for unit in units:
        decision = decisions[unit["visual_unit_id"]]
        if not decision[selected_key]:
            continue
        source_key = unit["source_video_id"] if unit["visual_unit_type"] == "video_frame" else unit["source_relative_path"]
        by_source_rank[source_key] += 1
        rows.append({
            "route_name": route_name,
            "route_policy_version": POLICY_VERSION,
            "route_reason": decision[reason_key],
            "route_rank_within_source": by_source_rank[source_key],
            **unit,
            "run_invocation_id": run_invocation_id,
            "created_at": created_at,
        })
    return rows


def decision_rows(units: Sequence[dict], decisions: Dict[str, dict], blocked_by_id: Dict[str, dict]) -> List[dict]:
    rows = []
    for unit in units:
        block = blocked_by_id.get(unit["visual_unit_id"], {})
        rows.append({
            **unit,
            **decisions.get(unit["visual_unit_id"], {}),
            "blocked_reason": block.get("blocked_reason", ""),
            "failure_reason": "",
        })
    return rows


def route_summary_by_source(units: Sequence[dict], decisions: Dict[str, dict]) -> List[dict]:
    summary: Dict[Tuple[str, str], dict] = {}
    for u in units:
        source_id = u["source_video_id"] if u["visual_unit_type"] == "video_frame" else u["source_relative_path"]
        key = (u["visual_unit_type"], source_id)
        row = summary.setdefault(key, {
            "visual_unit_type": u["visual_unit_type"],
            "source_id": source_id,
            "source_relative_path": u["source_video_relative_path"] or u["source_relative_path"],
            "visual_unit_count": 0,
            "yoloe_count": 0,
            "high_value_count": 0,
            "ocr_trigger_count": 0,
        })
        d = decisions[u["visual_unit_id"]]
        row["visual_unit_count"] += 1
        row["yoloe_count"] += int(bool(d["selected_for_yoloe"]))
        row["high_value_count"] += int(bool(d["selected_for_high_value"]))
        row["ocr_trigger_count"] += int(bool(d["selected_for_ocr_trigger"]))
    return sorted(summary.values(), key=lambda r: (r["visual_unit_type"], r["source_id"]))


def max_yoloe_gap_summary(units: Sequence[dict], decisions: Dict[str, dict]) -> Tuple[dict, List[str]]:
    gaps = {}
    missing = []
    by_video: Dict[str, List[dict]] = defaultdict(list)
    for u in units:
        if u["visual_unit_type"] == "video_frame":
            by_video[u["source_video_id"]].append(u)
    for video_id, frames in by_video.items():
        selected = [u for u in frames if decisions[u["visual_unit_id"]]["selected_for_yoloe"]]
        if not selected:
            missing.append(video_id)
            continue
        times = sorted(int(float(u["time_position_ms"])) for u in selected if u.get("time_position_ms") not in ("", None))
        if len(times) <= 1:
            gaps[video_id] = 0
        else:
            gaps[video_id] = max(b - a for a, b in zip(times, times[1:]))
    values = list(gaps.values())
    return {
        "video_count": len(gaps),
        "max_gap_ms": max(values) if values else 0,
        "min_gap_ms": min(values) if values else 0,
        "avg_gap_ms": round(sum(values) / len(values), 3) if values else 0,
        "by_video": gaps,
    }, sorted(missing)


def build_summary(
    *,
    args: argparse.Namespace,
    run_invocation_id: str,
    units: Sequence[dict],
    decisions: Dict[str, dict],
    blocked_items: Sequence[dict],
    failure_items: Sequence[dict],
    out: Path,
) -> dict:
    yoloe_ids = {uid for uid, d in decisions.items() if d["selected_for_yoloe"]}
    high_ids = {uid for uid, d in decisions.items() if d["selected_for_high_value"]}
    ocr_ids = {uid for uid, d in decisions.items() if d["selected_for_ocr_trigger"]}
    video_units = [u for u in units if u["visual_unit_type"] == "video_frame"]
    image_units = [u for u in units if u["visual_unit_type"] == "image_preview"]
    videos_with_frames = sorted({u["source_video_id"] for u in video_units})
    gap_summary, videos_missing = max_yoloe_gap_summary(units, decisions)
    summary = {
        "script_version": SCRIPT_VERSION,
        "run_invocation_id": run_invocation_id,
        "run_phase": args.run_phase,
        "video_visual_unit_count": len(video_units),
        "image_visual_unit_count": len(image_units),
        "total_visual_unit_count": len(units),
        "yoloe_queue_count": len(yoloe_ids),
        "high_value_queue_count": len(high_ids),
        "ocr_trigger_queue_count": len(ocr_ids),
        "high_value_subset_of_yoloe": high_ids.issubset(yoloe_ids),
        "ocr_subset_of_yoloe": ocr_ids.issubset(yoloe_ids),
        "high_value_ocr_overlap_count": len(high_ids & ocr_ids),
        "videos_with_frames": len(videos_with_frames),
        "videos_with_yoloe": len(videos_with_frames) - len(videos_missing),
        "videos_missing_yoloe": videos_missing,
        "max_yoloe_gap_ms_by_video_summary": gap_summary,
        "blocked_items_count": len(blocked_items),
        "failure_items_count": len(failure_items),
        "input_video_manifest": str(Path(args.video_frame_manifest).resolve()),
        "input_image_manifest": str(Path(args.image_visual_unit_manifest).resolve()),
        "output_dir": str(out.resolve()),
        "model_execution": {
            "yoloe": False,
            "qwen_vl": False,
            "ocr": False,
            "embedding": False,
        },
    }
    if not summary["high_value_subset_of_yoloe"]:
        failure_items.append({"failure_reason": "high_value_not_subset_of_yoloe"})
    if not summary["ocr_subset_of_yoloe"]:
        failure_items.append({"failure_reason": "ocr_not_subset_of_yoloe"})
    if summary["high_value_ocr_overlap_count"]:
        failure_items.append({"failure_reason": "high_value_ocr_overlap"})
    if videos_missing:
        failure_items.append({"failure_reason": "video_missing_yoloe", "videos": ",".join(videos_missing)})
    summary["failure_items_count"] = len(failure_items)
    return summary


def summary_md(summary: dict) -> str:
    lines = [
        "# Step02-3 Visual Unit Route Selector Summary",
        "",
        f"- script_version: {summary['script_version']}",
        f"- run_invocation_id: {summary['run_invocation_id']}",
        f"- run_phase: {summary['run_phase']}",
        f"- total_visual_unit_count: {summary['total_visual_unit_count']}",
        f"- video_visual_unit_count: {summary['video_visual_unit_count']}",
        f"- image_visual_unit_count: {summary['image_visual_unit_count']}",
        f"- yoloe_queue_count: {summary['yoloe_queue_count']}",
        f"- high_value_queue_count: {summary['high_value_queue_count']}",
        f"- ocr_trigger_queue_count: {summary['ocr_trigger_queue_count']}",
        f"- videos_with_frames: {summary['videos_with_frames']}",
        f"- videos_with_yoloe: {summary['videos_with_yoloe']}",
        f"- videos_missing_yoloe: {summary['videos_missing_yoloe']}",
        f"- high_value_subset_of_yoloe: {summary['high_value_subset_of_yoloe']}",
        f"- ocr_subset_of_yoloe: {summary['ocr_subset_of_yoloe']}",
        f"- high_value_ocr_overlap_count: {summary['high_value_ocr_overlap_count']}",
        f"- blocked_items_count: {summary['blocked_items_count']}",
        f"- failure_items_count: {summary['failure_items_count']}",
        "- model_execution: YOLOE=false, Qwen-VL=false, OCR=false, Embedding=false",
        "",
        "This selector writes routing queues only. It does not modify original media or formal indexes.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.policy != POLICY_VERSION:
        print(f"[FAIL] unsupported policy: {args.policy}", file=sys.stderr)
        return 2

    video_manifest = Path(args.video_frame_manifest).expanduser().resolve()
    image_manifest = Path(args.image_visual_unit_manifest).expanduser().resolve()
    missing_inputs = [str(p) for p in [video_manifest, image_manifest] if not p.exists()]
    if missing_inputs:
        for path in missing_inputs:
            print(f"[BLOCKED] missing input manifest: {path}", file=sys.stderr)
        return 3

    out = Path(args.out).expanduser().resolve()
    prepare_output(out)
    run_invocation_id = uuid.uuid4().hex
    created_at = utc_now()

    blocked_items: List[dict] = []
    failure_items: List[dict] = []
    units: List[dict] = []

    for idx, row in enumerate(read_video_manifest(video_manifest), 1):
        unit, blocked = normalize_video_row(row, video_manifest, idx)
        if blocked:
            blocked_items.append(blocked)
        else:
            units.append(unit)

    for idx, row in enumerate(read_jsonl(image_manifest), 1):
        unit, blocked = normalize_image_row(row, image_manifest, idx)
        if blocked:
            blocked_items.append(blocked)
        else:
            units.append(unit)

    decisions = route_visual_units(
        units,
        yoloe_coverage_ms=args.yoloe_coverage_ms,
        yoloe_max_gap_ms=args.yoloe_max_gap_ms,
        high_value_min_gap_ms=args.high_value_min_gap_ms,
    )
    blocked_by_id = {item.get("visual_unit_id", ""): item for item in blocked_items if item.get("visual_unit_id")}
    decisions_manifest = decision_rows(units, decisions, blocked_by_id)

    yoloe_rows = build_route_rows("yoloe", units, decisions, run_invocation_id, created_at)
    high_rows = build_route_rows("high_value", units, decisions, run_invocation_id, created_at)
    ocr_rows = build_route_rows("ocr_trigger", units, decisions, run_invocation_id, created_at)

    write_jsonl(out / "queues" / "yoloe_visual_units.jsonl", yoloe_rows)
    write_csv(out / "queues" / "yoloe_visual_units.csv", yoloe_rows, QUEUE_FIELDS)
    write_jsonl(out / "queues" / "high_value_visual_units.jsonl", high_rows)
    write_csv(out / "queues" / "high_value_visual_units.csv", high_rows, QUEUE_FIELDS)
    write_jsonl(out / "queues" / "ocr_trigger_visual_units.jsonl", ocr_rows)
    write_csv(out / "queues" / "ocr_trigger_visual_units.csv", ocr_rows, QUEUE_FIELDS)

    write_jsonl(out / "manifests" / "visual_unit_route_decision_manifest.jsonl", decisions_manifest)
    write_csv(out / "manifests" / "visual_unit_route_decision_manifest.csv", decisions_manifest, [*NORMALIZED_FIELDS, *DECISION_EXTRA_FIELDS])
    write_csv(out / "manifests" / "route_summary_by_source.csv", route_summary_by_source(units, decisions), [])
    write_jsonl(out / "manifests" / "blocked_items.jsonl", blocked_items)
    write_jsonl(out / "manifests" / "failure_items.jsonl", failure_items)

    summary = build_summary(
        args=args,
        run_invocation_id=run_invocation_id,
        units=units,
        decisions=decisions,
        blocked_items=blocked_items,
        failure_items=failure_items,
        out=out,
    )
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    summary_markdown = summary_md(summary)
    atomic_write_text(out / "reports" / "step02_3_visual_unit_route_selector_summary.json", summary_json)
    atomic_write_text(out / "reports" / "step02_3_visual_unit_route_selector_summary.md", summary_markdown)
    atomic_write_text(out / "final_report" / "step02_3_visual_unit_route_selector_final_report_latest.json", summary_json)
    atomic_write_text(out / "final_report" / "step02_3_visual_unit_route_selector_final_report_latest.md", summary_markdown)
    history_base = out / "final_report" / "history" / f"{run_invocation_id}_step02_3_visual_unit_route_selector_{args.run_phase}_final_report"
    atomic_write_text(history_base.with_suffix(".json"), summary_json)
    atomic_write_text(history_base.with_suffix(".md"), summary_markdown)

    print(summary_json)
    if blocked_items:
        return 3
    if failure_items:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
