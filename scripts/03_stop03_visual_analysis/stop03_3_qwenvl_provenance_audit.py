#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-3B/3C Qwen-VL provenance audit.

Read-only audit for an existing Qwen-VL smoke/full output directory.
It joins:
- qwenvl_*result_manifest.csv
- qwenvl_*selected_queue.csv
- qwenvl_runtime_union_candidate_queue.csv

Then computes:
- image/content sha256 of the actual visual input used by Qwen-VL
- qwen output text sha256
- source/original path resolution when available
- missing lineage counts

It does not modify original media, does not run Qwen-VL, and writes only under the Qwen run dir.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import Counter


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(block_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path or not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def first_existing(row: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return ""


def find_one(base: Path, patterns: List[str]) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(base.glob(pat))
        if hits:
            return hits[0]
    return None


def index_rows(rows: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in rows:
        for k in keys:
            v = r.get(k)
            if v:
                out[str(v)] = r
    return out


def resolve_original_path(row: Dict[str, Any], source_root: Path) -> str:
    for k in [
        "original_source_path_at_processing_time",
        "parent_source_path_at_processing_time",
        "source_path_at_processing_time",
        "source_path",
    ]:
        v = row.get(k)
        if v and Path(str(v)).exists():
            return str(v)

    rel = first_existing(row, ["source_relative_path", "parent_source_relative_path"])
    if rel:
        p = source_root / rel
        if p.exists():
            return str(p)

    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qwen-run-dir", required=True)
    ap.add_argument("--source-root", default="/Users/yourname/Documents/001DZLtest")
    args = ap.parse_args()

    qwen_dir = Path(args.qwen_run_dir)
    source_root = Path(args.source_root)

    manifests_dir = qwen_dir / "manifests"
    reports_dir = qwen_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    result_csv = find_one(qwen_dir, [
        "manifests/qwenvl_*result_manifest.csv",
        "**/qwenvl_*result_manifest.csv",
    ])
    selected_csv = find_one(qwen_dir, [
        "manifests/qwenvl_*selected_queue.csv",
        "**/qwenvl_*selected_queue.csv",
    ])
    runtime_csv = find_one(qwen_dir, [
        "manifests/qwenvl_runtime_union_candidate_queue.csv",
        "**/qwenvl_runtime_union_candidate_queue.csv",
    ])

    result_rows = read_csv(result_csv) if result_csv else []
    selected_rows = read_csv(selected_csv) if selected_csv else []
    runtime_rows = read_csv(runtime_csv) if runtime_csv else []

    selected_idx = index_rows(selected_rows, ["candidate_runtime_id", "candidate_id", "visual_unit_id"])
    runtime_idx = index_rows(runtime_rows, ["candidate_runtime_id", "candidate_id", "visual_unit_id"])

    out_rows: List[Dict[str, Any]] = []
    missing = Counter()
    status_counter = Counter()
    type_counter = Counter()
    runtime_source_counter = Counter()

    for r in result_rows:
        rid = first_existing(r, ["candidate_runtime_id", "candidate_id", "visual_unit_id"])
        joined = {}
        joined.update(runtime_idx.get(rid, {}))
        joined.update(selected_idx.get(rid, {}))
        # If candidate_runtime_id not matched, fall back by visual_unit_id.
        vu = r.get("visual_unit_id")
        if vu:
            joined.update(runtime_idx.get(vu, {}))
            joined.update(selected_idx.get(vu, {}))
        joined.update(r)

        image_path = first_existing(joined, ["image_path", "runtime_visual_file", "visual_file", "preview_path"])
        stdout_path = first_existing(joined, ["stdout_path"])
        original_path = resolve_original_path(joined, source_root)

        image_sha256 = ""
        image_exists = False
        if image_path:
            p = Path(image_path)
            image_exists = p.exists()
            if image_exists:
                try:
                    image_sha256 = sha256_file(p)
                except Exception as e:
                    joined["image_sha256_error"] = repr(e)

        qwen_output_sha256 = ""
        stdout_exists = False
        if stdout_path:
            p = Path(stdout_path)
            stdout_exists = p.exists()
            if stdout_exists:
                try:
                    qwen_output_sha256 = sha256_file(p)
                except Exception as e:
                    joined["qwen_output_sha256_error"] = repr(e)

        provenance_id_basis = "|".join([
            first_existing(joined, ["candidate_runtime_id"]),
            first_existing(joined, ["visual_unit_id"]),
            image_sha256,
            qwen_output_sha256,
        ])
        provenance_id = "qvp_" + hashlib.sha256(provenance_id_basis.encode("utf-8", errors="replace")).hexdigest()[:24]

        joined.update({
            "provenance_id": provenance_id,
            "runtime_input_image_path": image_path,
            "runtime_input_image_exists": image_exists,
            "runtime_input_image_sha256": image_sha256,
            "qwen_output_text_path": stdout_path,
            "qwen_output_text_exists": stdout_exists,
            "qwen_output_text_sha256": qwen_output_sha256,
            "resolved_original_source_path": original_path,
            "resolved_original_source_exists": bool(original_path and Path(original_path).exists()),
        })

        for key in ["candidate_runtime_id", "visual_unit_id", "runtime_input_image_path", "source_relative_path"]:
            if not joined.get(key):
                missing[key] += 1
        if not image_exists:
            missing["runtime_input_image_file_missing"] += 1
        if not stdout_exists:
            missing["qwen_output_text_file_missing"] += 1
        if not original_path:
            missing["resolved_original_source_path_missing"] += 1

        status_counter[joined.get("status", "")] += 1
        type_counter[first_existing(joined, ["visual_unit_type", "candidate_type", "preview_role"]) or "__EMPTY__"] += 1
        runtime_source_counter[joined.get("runtime_source", "") or "__EMPTY__"] += 1

        out_rows.append(joined)

    fields = sorted(set(k for row in out_rows for k in row.keys()))
    provenance_csv = manifests_dir / "qwenvl_result_provenance_manifest.csv"
    provenance_jsonl = manifests_dir / "qwenvl_result_provenance_manifest.jsonl"
    write_csv(provenance_csv, out_rows, fields)
    write_jsonl(provenance_jsonl, out_rows)

    summary = {
        "status": "PASS" if result_rows and not missing.get("runtime_input_image_file_missing") and not missing.get("qwen_output_text_file_missing") else "FAIL",
        "qwen_run_dir": str(qwen_dir),
        "source_safety": "read_only_no_original_media_modification_no_model_inference",
        "result_csv": str(result_csv) if result_csv else "",
        "selected_csv": str(selected_csv) if selected_csv else "",
        "runtime_csv": str(runtime_csv) if runtime_csv else "",
        "result_row_count": len(result_rows),
        "selected_row_count": len(selected_rows),
        "runtime_row_count": len(runtime_rows),
        "provenance_row_count": len(out_rows),
        "status_counts": dict(status_counter),
        "visual_unit_type_counts": dict(type_counter),
        "runtime_source_counts": dict(runtime_source_counter),
        "missing_counts": dict(missing),
        "provenance_csv": str(provenance_csv),
        "provenance_jsonl": str(provenance_jsonl),
    }

    summary_json = reports_dir / "qwenvl_provenance_audit_summary.json"
    summary_md = reports_dir / "qwenvl_provenance_audit_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# Qwen-VL Provenance Audit Summary",
        "",
        f"- status: {summary['status']}",
        f"- qwen_run_dir: {summary['qwen_run_dir']}",
        f"- result_row_count: {summary['result_row_count']}",
        f"- selected_row_count: {summary['selected_row_count']}",
        f"- runtime_row_count: {summary['runtime_row_count']}",
        f"- provenance_row_count: {summary['provenance_row_count']}",
        f"- status_counts: {summary['status_counts']}",
        f"- visual_unit_type_counts: {summary['visual_unit_type_counts']}",
        f"- runtime_source_counts: {summary['runtime_source_counts']}",
        f"- missing_counts: {summary['missing_counts']}",
        f"- provenance_csv: {summary['provenance_csv']}",
        f"- provenance_jsonl: {summary['provenance_jsonl']}",
        "",
        "PASS means every Qwen result has an existing runtime input image and output text file.",
        "resolved_original_source_path_missing may occur if an item only carried relative lineage; inspect CSV before freezing search integration.",
    ]
    summary_md.write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
