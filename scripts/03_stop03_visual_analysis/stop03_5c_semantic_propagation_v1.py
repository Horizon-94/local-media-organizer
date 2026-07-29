#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5C Semantic Propagation v1

Read-only semantic propagation from Qwen-VL direct evidence to nearby video visual units.
- Reads Stop03-5B evidence_staging.sqlite only.
- Does not modify staging DB or original media.
- Does not run models, use network, or download anything.

Propagation policy v1:
- source must be direct Qwen-VL text on video_frame
- source cleanup_status must be ok unless --include-review-source is set
- source text_len >= --min-source-text-len
- source text must not look like black/empty frame unless --include-black-source is set
- target must be video_frame in same original_source_content_id
- target must not already have direct Qwen-VL evidence unless --allow-direct-target is set
- target is within +/- radius neighbor rows and <= max_time_delta_ms
- for duplicate target candidates, keep nearest / highest confidence / longer source text
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "stop03_5c_semantic_propagation_v1.0"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def short_id(prefix: str, payload: str, n: int = 24) -> str:
    return f"{prefix}_{sha256_text(payload)[:n]}"


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        keys = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def is_black_or_empty_text(text: str) -> bool:
    t = (text or "").strip()
    needles = [
        "纯黑色", "一片黑", "全黑", "无任何可见内容", "画面为黑", "黑屏", "元素：无", "检索价值：无",
    ]
    hit = sum(1 for n in needles if n in t)
    return hit >= 2 or ("纯黑" in t and "无" in t)


def confidence_for_step(step: int) -> float:
    step = abs(int(step))
    if step <= 1:
        return 0.85
    if step == 2:
        return 0.75
    if step == 3:
        return 0.65
    return max(0.25, 0.65 - 0.08 * (step - 3))


def connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def create_out_db(path: Path) -> sqlite3.Connection:
    ensure_dir(path.parent)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(
        """
        CREATE TABLE processing_stage (
            stage_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            source_safety TEXT NOT NULL,
            network TEXT NOT NULL,
            model_download TEXT NOT NULL,
            raw_json TEXT
        );

        CREATE TABLE semantic_propagation (
            propagation_id TEXT PRIMARY KEY,
            propagated_evidence_id TEXT NOT NULL,
            propagated_evidence_text_id TEXT NOT NULL,
            source_evidence_id TEXT NOT NULL,
            source_evidence_text_id TEXT NOT NULL,
            source_visual_unit_id TEXT NOT NULL,
            target_visual_unit_id TEXT NOT NULL,
            original_source_content_id TEXT,
            source_time_position_ms INTEGER,
            target_time_position_ms INTEGER,
            time_delta_ms INTEGER,
            propagation_direction TEXT,
            propagation_step INTEGER,
            propagation_radius INTEGER,
            propagation_confidence REAL,
            propagation_reason TEXT,
            blocked_reason TEXT,
            source_text_sha256 TEXT,
            propagated_text_sha256 TEXT,
            created_at TEXT,
            raw_json TEXT
        );

        CREATE TABLE propagated_evidence_text (
            evidence_text_id TEXT PRIMARY KEY,
            evidence_id TEXT NOT NULL,
            source_evidence_id TEXT NOT NULL,
            source_evidence_text_id TEXT NOT NULL,
            target_visual_unit_id TEXT NOT NULL,
            modality TEXT NOT NULL,
            text_kind TEXT NOT NULL,
            text TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            text_len INTEGER NOT NULL,
            propagation_confidence REAL,
            quality_status TEXT,
            cleanup_status TEXT,
            is_propagated INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        );

        CREATE INDEX idx_prop_target ON semantic_propagation(target_visual_unit_id);
        CREATE INDEX idx_prop_source ON semantic_propagation(source_evidence_id);
        CREATE INDEX idx_prop_group_time ON semantic_propagation(original_source_content_id, target_time_position_ms);
        """
    )
    return conn


def fetch_visual_units(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT visual_unit_id, visual_unit_type, source_media_id, time_position_ms,
               derived_image_path, derived_image_sha256, source_relative_path,
               original_source_content_id, source_manifest
        FROM visual_unit
        WHERE visual_unit_type='video_frame'
          AND original_source_content_id IS NOT NULL
          AND original_source_content_id != ''
        ORDER BY original_source_content_id, time_position_ms, visual_unit_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_direct_qwen(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT me.evidence_id, me.visual_unit_id, me.modality, me.status,
               et.evidence_text_id, et.text, et.text_sha256, et.text_len,
               et.quality_status, et.cleanup_status,
               vu.visual_unit_type, vu.original_source_content_id, vu.time_position_ms,
               vu.source_relative_path, vu.derived_image_path
        FROM model_evidence me
        JOIN evidence_text et ON et.evidence_id = me.evidence_id
        JOIN visual_unit vu ON vu.visual_unit_id = me.visual_unit_id
        WHERE me.modality='qwenvl'
          AND et.modality='qwenvl'
          AND et.text_kind='qwen_clean_text'
          AND vu.visual_unit_type='video_frame'
          AND vu.original_source_content_id IS NOT NULL
          AND vu.original_source_content_id != ''
        ORDER BY vu.original_source_content_id, vu.time_position_ms, me.evidence_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def group_units(units: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for u in units:
        groups.setdefault(u.get("original_source_content_id") or "", []).append(u)
    for k in groups:
        groups[k].sort(key=lambda r: (int(r.get("time_position_ms") or 0), r.get("visual_unit_id") or ""))
    return groups


def main() -> int:
    raise SystemExit(
        "RETIRED_STOP03_5C_INTERFACE: use "
        "stop03_5c_qwenvl_yolo_propagation_v1.py with the central database"
    )
    ap = argparse.ArgumentParser(description="Stop03-5C semantic propagation")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--staging-db", required=True, help="Stop03-5B evidence_staging.sqlite")
    ap.add_argument("--out", required=True)
    ap.add_argument("--radius", type=int, default=3)
    ap.add_argument("--max-time-delta-ms", type=int, default=30000)
    ap.add_argument("--min-source-text-len", type=int, default=80)
    ap.add_argument("--allow-direct-target", action="store_true")
    ap.add_argument("--include-review-source", action="store_true")
    ap.add_argument("--include-black-source", action="store_true")
    ap.add_argument("--expect-direct-qwenvl", type=int, default=268)
    args = ap.parse_args()

    t0 = time.time()
    out = Path(args.out)
    ensure_dir(out)
    for sub in ["database", "manifests", "reports"]:
        ensure_dir(out / sub)

    staging_db = Path(args.staging_db)
    src_conn = connect_ro(staging_db)
    direct_qwen = fetch_direct_qwen(src_conn)
    video_units = fetch_visual_units(src_conn)
    groups = group_units(video_units)

    direct_qwen_by_vu = {r["visual_unit_id"]: r for r in direct_qwen}
    direct_vu_ids = set(direct_qwen_by_vu.keys())

    source_rows: List[Dict[str, Any]] = []
    skipped_sources: List[Dict[str, Any]] = []
    for r in direct_qwen:
        reasons = []
        text = r.get("text") or ""
        if not args.include_review_source and (r.get("cleanup_status") not in ("ok", "", None)):
            reasons.append(f"cleanup_status_{r.get('cleanup_status')}")
        if len(text.strip()) < args.min_source_text_len:
            reasons.append("source_text_too_short")
        if (not args.include_black_source) and is_black_or_empty_text(text):
            reasons.append("source_black_or_empty_frame")
        if reasons:
            item = dict(r)
            item["skip_reasons"] = "|".join(reasons)
            skipped_sources.append(item)
        else:
            source_rows.append(r)

    candidates: Dict[str, Dict[str, Any]] = {}
    blocked: List[Dict[str, Any]] = []

    for src in source_rows:
        gid = src.get("original_source_content_id") or ""
        units = groups.get(gid, [])
        if not units:
            continue
        vu_id = src["visual_unit_id"]
        idx = next((i for i, u in enumerate(units) if u.get("visual_unit_id") == vu_id), None)
        if idx is None:
            continue
        src_time = int(src.get("time_position_ms") or 0)
        for offset in range(-args.radius, args.radius + 1):
            if offset == 0:
                continue
            j = idx + offset
            if j < 0 or j >= len(units):
                continue
            tgt = units[j]
            tgt_id = tgt["visual_unit_id"]
            tgt_time = int(tgt.get("time_position_ms") or 0)
            delta = tgt_time - src_time
            abs_delta = abs(delta)
            direction = "previous" if offset < 0 else "next"
            block_reasons = []
            if abs_delta > args.max_time_delta_ms:
                block_reasons.append("time_delta_exceeds_limit")
            if (not args.allow_direct_target) and tgt_id in direct_vu_ids:
                block_reasons.append("target_has_direct_qwenvl")
            if block_reasons:
                blocked.append({
                    "source_evidence_id": src["evidence_id"],
                    "source_visual_unit_id": vu_id,
                    "target_visual_unit_id": tgt_id,
                    "original_source_content_id": gid,
                    "source_time_position_ms": src_time,
                    "target_time_position_ms": tgt_time,
                    "time_delta_ms": delta,
                    "propagation_step": abs(offset),
                    "propagation_direction": direction,
                    "blocked_reason": "|".join(block_reasons),
                })
                continue
            conf = confidence_for_step(abs(offset))
            payload = f"{src['evidence_id']}->{tgt_id}:{offset}:{src.get('text_sha256')}"
            prop_id = short_id("prop", payload)
            ev_id = short_id("ev_prop", payload)
            et_id = short_id("txt_prop", payload)
            item = {
                "propagation_id": prop_id,
                "propagated_evidence_id": ev_id,
                "propagated_evidence_text_id": et_id,
                "source_evidence_id": src["evidence_id"],
                "source_evidence_text_id": src["evidence_text_id"],
                "source_visual_unit_id": vu_id,
                "target_visual_unit_id": tgt_id,
                "original_source_content_id": gid,
                "source_time_position_ms": src_time,
                "target_time_position_ms": tgt_time,
                "time_delta_ms": delta,
                "propagation_direction": direction,
                "propagation_step": abs(offset),
                "propagation_radius": args.radius,
                "propagation_confidence": conf,
                "propagation_reason": "same_source_content_id|neighbor_frame_window|direct_qwenvl_to_adjacent_video_frame|no_scene_boundary_check_v1",
                "blocked_reason": "",
                "source_text_sha256": src.get("text_sha256") or sha256_text(src.get("text") or ""),
                "propagated_text_sha256": src.get("text_sha256") or sha256_text(src.get("text") or ""),
                "text": src.get("text") or "",
                "text_len": len(src.get("text") or ""),
                "quality_status": "propagated_from_direct_qwenvl",
                "cleanup_status": "ok",
                "created_at": now_id(),
                "raw_json": json.dumps({"source": src, "target": tgt}, ensure_ascii=False),
            }
            # One propagated text per target visual unit in v1. Keep nearest, then confidence, then longer source text.
            old = candidates.get(tgt_id)
            if old is None:
                candidates[tgt_id] = item
            else:
                old_key = (abs(int(old["time_delta_ms"])), -float(old["propagation_confidence"]), -int(old["text_len"]))
                new_key = (abs(int(item["time_delta_ms"])), -float(item["propagation_confidence"]), -int(item["text_len"]))
                if new_key < old_key:
                    candidates[tgt_id] = item

    propagation_rows = list(candidates.values())
    propagation_rows.sort(key=lambda r: (r.get("original_source_content_id") or "", int(r.get("target_time_position_ms") or 0), r.get("target_visual_unit_id") or ""))

    text_rows: List[Dict[str, Any]] = []
    for r in propagation_rows:
        text_rows.append({
            "evidence_text_id": r["propagated_evidence_text_id"],
            "evidence_id": r["propagated_evidence_id"],
            "source_evidence_id": r["source_evidence_id"],
            "source_evidence_text_id": r["source_evidence_text_id"],
            "target_visual_unit_id": r["target_visual_unit_id"],
            "modality": "qwenvl_propagated",
            "text_kind": "qwen_propagated_text",
            "text": r["text"],
            "text_sha256": r["propagated_text_sha256"],
            "text_len": r["text_len"],
            "propagation_confidence": r["propagation_confidence"],
            "quality_status": r["quality_status"],
            "cleanup_status": r["cleanup_status"],
            "is_propagated": 1,
            "created_at": r["created_at"],
        })

    out_db = out / "database" / "semantic_propagation.sqlite"
    dst = create_out_db(out_db)
    stage_payload = {
        "run_root": args.run_root,
        "staging_db": str(staging_db),
        "radius": args.radius,
        "max_time_delta_ms": args.max_time_delta_ms,
        "min_source_text_len": args.min_source_text_len,
        "allow_direct_target": bool(args.allow_direct_target),
        "include_review_source": bool(args.include_review_source),
        "include_black_source": bool(args.include_black_source),
    }
    dst.execute(
        "INSERT INTO processing_stage VALUES (?,?,?,?,?,?,?)",
        (
            "stop03_5c_semantic_propagation",
            SCHEMA_VERSION,
            now_id(),
            "read_staging_db_only_no_original_media_write",
            "not_required_not_used",
            "not_required_not_used",
            json.dumps(stage_payload, ensure_ascii=False),
        ),
    )

    for r in propagation_rows:
        dst.execute(
            """
            INSERT INTO semantic_propagation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r["propagation_id"], r["propagated_evidence_id"], r["propagated_evidence_text_id"],
                r["source_evidence_id"], r["source_evidence_text_id"], r["source_visual_unit_id"],
                r["target_visual_unit_id"], r["original_source_content_id"], r["source_time_position_ms"],
                r["target_time_position_ms"], r["time_delta_ms"], r["propagation_direction"],
                r["propagation_step"], r["propagation_radius"], r["propagation_confidence"],
                r["propagation_reason"], r["blocked_reason"], r["source_text_sha256"],
                r["propagated_text_sha256"], r["created_at"], r["raw_json"],
            ),
        )
    for r in text_rows:
        dst.execute(
            """
            INSERT INTO propagated_evidence_text VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r["evidence_text_id"], r["evidence_id"], r["source_evidence_id"], r["source_evidence_text_id"],
                r["target_visual_unit_id"], r["modality"], r["text_kind"], r["text"], r["text_sha256"],
                r["text_len"], r["propagation_confidence"], r["quality_status"], r["cleanup_status"], r["is_propagated"],
            ),
        )
    dst.commit()
    dst.close()
    src_conn.close()

    prop_csv = out / "manifests" / "semantic_propagation_manifest.csv"
    text_csv = out / "manifests" / "propagated_evidence_text_manifest.csv"
    skipped_csv = out / "reports" / "semantic_propagation_skipped_sources.csv"
    blocked_csv = out / "reports" / "semantic_propagation_blocked_candidates.csv"
    write_csv(prop_csv, propagation_rows)
    write_csv(text_csv, text_rows)
    write_csv(skipped_csv, skipped_sources)
    write_csv(blocked_csv, blocked)

    direct_video_qwen = len(direct_qwen)
    source_usable = len(source_rows)
    video_unit_count = len(video_units)
    source_group_count = len(groups)
    validation_status = "PASS"
    problems = []
    if args.expect_direct_qwenvl and direct_video_qwen > args.expect_direct_qwenvl:
        problems.append(f"direct_video_qwen_exceeds_expected:{direct_video_qwen}>{args.expect_direct_qwenvl}")
    if direct_video_qwen == 0:
        problems.append("no_direct_video_qwen_source")
    if len(propagation_rows) == 0:
        problems.append("no_propagation_rows")
    if problems:
        validation_status = "FAIL"

    step_counts: Dict[str, int] = {}
    direction_counts: Dict[str, int] = {}
    for r in propagation_rows:
        step_counts[str(r["propagation_step"])] = step_counts.get(str(r["propagation_step"]), 0) + 1
        direction_counts[r["propagation_direction"]] = direction_counts.get(r["propagation_direction"], 0) + 1

    summary = {
        "validation_status": validation_status,
        "schema_version": SCHEMA_VERSION,
        "elapsed_seconds": round(time.time() - t0, 3),
        "mode": "read_staging_db_only_no_model_rerun",
        "source_safety": "read_only_no_move_no_delete_no_rename_no_original_media_access_required",
        "network": "not_required_not_used",
        "model_download": "not_required_not_used",
        "inputs": {"run_root": args.run_root, "staging_db": str(staging_db)},
        "settings": {
            "radius": args.radius,
            "max_time_delta_ms": args.max_time_delta_ms,
            "min_source_text_len": args.min_source_text_len,
            "allow_direct_target": bool(args.allow_direct_target),
            "include_review_source": bool(args.include_review_source),
            "include_black_source": bool(args.include_black_source),
        },
        "counts": {
            "video_visual_units": video_unit_count,
            "source_group_count": source_group_count,
            "direct_qwenvl_video_sources": direct_video_qwen,
            "usable_qwenvl_sources": source_usable,
            "skipped_sources": len(skipped_sources),
            "blocked_candidates": len(blocked),
            "propagation_rows": len(propagation_rows),
            "propagated_text_rows": len(text_rows),
        },
        "step_counts": step_counts,
        "direction_counts": direction_counts,
        "problems": problems,
        "outputs": {
            "sqlite": str(out_db),
            "semantic_propagation_csv": str(prop_csv),
            "propagated_evidence_text_csv": str(text_csv),
            "skipped_sources_csv": str(skipped_csv),
            "blocked_candidates_csv": str(blocked_csv),
        },
    }

    summary_json = out / "reports" / "stop03_5c_semantic_propagation_summary.json"
    summary_md = out / "reports" / "stop03_5c_semantic_propagation_summary.md"
    write_json(summary_json, summary)
    md = [
        "# Stop03-5C Semantic Propagation",
        "",
        f"- validation_status: `{validation_status}`",
        f"- schema_version: `{SCHEMA_VERSION}`",
        "- mode: `read_staging_db_only_no_model_rerun`",
        "- source_safety: `read_only_no_move_no_delete_no_rename_no_original_media_access_required`",
        "- network: `not_required_not_used`",
        "- model_download: `not_required_not_used`",
        "",
        "## Settings",
        f"- radius: `{args.radius}`",
        f"- max_time_delta_ms: `{args.max_time_delta_ms}`",
        f"- min_source_text_len: `{args.min_source_text_len}`",
        f"- allow_direct_target: `{bool(args.allow_direct_target)}`",
        f"- include_review_source: `{bool(args.include_review_source)}`",
        f"- include_black_source: `{bool(args.include_black_source)}`",
        "",
        "## Counts",
    ]
    for k, v in summary["counts"].items():
        md.append(f"- {k}: `{v}`")
    md += ["", "## Step counts", f"- {step_counts}", "", "## Direction counts", f"- {direction_counts}", "", "## Decision"]
    if validation_status == "PASS":
        md.append("Semantic propagation passed. Next stage can be text embedding / FTS staging after optional spot-check.")
    else:
        md.append("Semantic propagation failed. Review problems before using propagated text.")
    md += ["", "## Outputs"]
    for k, v in summary["outputs"].items():
        md.append(f"- {k}: `{v}`")
    summary_md.write_text("\n".join(md) + "\n", encoding="utf-8")

    print("== Stop03-5C semantic propagation finished ==")
    print(json.dumps({
        "validation_status": validation_status,
        "elapsed_seconds": summary["elapsed_seconds"],
        "counts": summary["counts"],
        "step_counts": step_counts,
        "direction_counts": direction_counts,
        "problems": problems,
        "summary_md": str(summary_md),
        "sqlite": str(out_db),
    }, ensure_ascii=False, indent=2))
    return 0 if validation_status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
