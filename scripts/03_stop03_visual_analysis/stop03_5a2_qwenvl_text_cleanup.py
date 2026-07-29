#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5A2 Qwen-VL text cleanup.

Reads existing Qwen-VL db-ready evidence manifest and extracts model-generated
visual description text from wrapper output. Does not touch original media and
does not rerun any model.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="replace")).hexdigest()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample) if sample else csv.excel
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(f, dialect=dialect))


def write_csv(path: Path, rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def normalize_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Strip invisible/control chars except newline/tab.
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    # Normalize whitespace but keep paragraph breaks.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_assistant_text(raw: str) -> Tuple[str, str, List[str]]:
    """Return clean_text, method, warnings."""
    warnings: List[str] = []
    text = raw or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Primary: output contains chat template and tail metrics.
    start_markers = [
        "<|im_start|>assistant\n",
        "<|im_start|>assistant",
        "assistant\n",
    ]
    start_idx = -1
    marker_used = ""
    for m in start_markers:
        idx = text.find(m)
        if idx >= 0:
            start_idx = idx + len(m)
            marker_used = m
            break

    if start_idx >= 0:
        candidate = text[start_idx:]
        method = "assistant_marker"
    else:
        # Fallback: strip leading file/prompt blocks by finding first numbered answer.
        m = re.search(r"(?:^|\n)\s*1[）\).、]", text)
        if m:
            candidate = text[m.start():]
            method = "numbered_answer_fallback"
            warnings.append("missing_assistant_marker")
        else:
            candidate = text
            method = "raw_fallback"
            warnings.append("missing_assistant_marker")
            warnings.append("missing_numbered_answer")

    # Remove end-of-generation template and runtime metrics.
    end_patterns = [
        r"\n={5,}\s*\nPrompt:\s*\d+\s+tokens.*$",
        r"\nPrompt:\s*\d+\s+tokens.*$",
        r"\nGeneration:\s*\d+\s+tokens.*$",
        r"\nPeak memory:\s*.*$",
        r"<\|im_end\|>.*$",
    ]
    for pat in end_patterns:
        candidate = re.sub(pat, "", candidate, flags=re.S)

    # Remove any leftover wrapper lines.
    lines = []
    for line in candidate.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("=========="):
            continue
        if stripped.startswith("Files: ["):
            continue
        if stripped.startswith("Prompt: <|im_start|>"):
            continue
        if stripped.startswith("<|vision_start|>"):
            continue
        if stripped.startswith("<|im_start|>user"):
            continue
        if stripped.startswith("Prompt:") and "tokens" in stripped:
            continue
        if stripped.startswith("Generation:") and "tokens" in stripped:
            continue
        if stripped.startswith("Peak memory:"):
            continue
        lines.append(line)

    clean = normalize_text("\n".join(lines))

    # Quality flags for cleanup result.
    if "/Users/" in clean or "Documents/001DZLtestbaogao" in clean:
        warnings.append("internal_path_remains")
    if "<|im_start|>" in clean or "<|vision_start|>" in clean or "<|image_pad|>" in clean:
        warnings.append("chat_template_token_remains")
    if "Prompt:" in clean and "tokens" in clean:
        warnings.append("runtime_metric_remains")
    if len(clean) < 40:
        warnings.append("clean_text_too_short")

    return clean, method, sorted(set(warnings))


def status_for(clean: str, warnings: List[str]) -> str:
    if not clean:
        return "cleanup_failed"
    hard = {"internal_path_remains", "chat_template_token_remains", "runtime_metric_remains", "clean_text_too_short"}
    if any(w in hard for w in warnings):
        return "review"
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--qwenvl-db-ready", required=True)
    ap.add_argument("--quality-audit", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    started = datetime.now()
    out = Path(args.out)
    manifests = out / "manifests"
    reports = out / "reports"
    dbdir = out / "database"
    for d in (manifests, reports, dbdir):
        ensure_dir(d)

    in_csv = Path(args.qwenvl_db_ready)
    rows = read_csv_rows(in_csv)
    if not rows:
        raise RuntimeError(f"empty qwenvl manifest: {in_csv}")
    if "qwen_text" not in rows[0]:
        raise RuntimeError(f"qwen_text column not found. columns={list(rows[0].keys())}")

    out_rows: List[Dict[str, object]] = []
    audit_rows: List[Dict[str, object]] = []
    status_counts: Dict[str, int] = {}
    warning_counts: Dict[str, int] = {}

    for idx, r in enumerate(rows, 1):
        raw = r.get("qwen_text", "") or ""
        clean, method, warnings = extract_assistant_text(raw)
        st = status_for(clean, warnings)
        status_counts[st] = status_counts.get(st, 0) + 1
        for w in warnings:
            warning_counts[w] = warning_counts.get(w, 0) + 1

        new = dict(r)
        new.update({
            "qwen_clean_text": clean,
            "qwen_clean_text_preview": clean[:300].replace("\n", "\\n"),
            "qwen_raw_text_sha256": sha256_text(raw),
            "qwen_clean_text_sha256": sha256_text(clean),
            "qwen_raw_text_len": len(raw),
            "qwen_clean_text_len": len(clean),
            "qwen_text_cleanup_status": st,
            "qwen_text_cleanup_method": method,
            "qwen_text_cleanup_warnings": "|".join(warnings),
            "qwen_text_cleanup_version": "stop03_5a2_qwenvl_text_cleanup_v1",
            "qwen_text_cleanup_created_at": started.strftime("%Y%m%d_%H%M%S"),
        })
        out_rows.append(new)
        audit_rows.append({
            "row_index": idx,
            "evidence_id": r.get("evidence_id", ""),
            "visual_unit_id": r.get("visual_unit_id", ""),
            "runtime_source": r.get("runtime_source", ""),
            "visual_unit_type": r.get("visual_unit_type", ""),
            "source_relative_path": r.get("source_relative_path", ""),
            "runtime_input_image_path": r.get("runtime_input_image_path", ""),
            "raw_text_len": len(raw),
            "clean_text_len": len(clean),
            "cleanup_status": st,
            "cleanup_method": method,
            "cleanup_warnings": "|".join(warnings),
            "clean_text_preview_300": clean[:300].replace("\n", "\\n"),
        })

    original_fields = list(rows[0].keys())
    added_fields = [
        "qwen_clean_text",
        "qwen_clean_text_preview",
        "qwen_raw_text_sha256",
        "qwen_clean_text_sha256",
        "qwen_raw_text_len",
        "qwen_clean_text_len",
        "qwen_text_cleanup_status",
        "qwen_text_cleanup_method",
        "qwen_text_cleanup_warnings",
        "qwen_text_cleanup_version",
        "qwen_text_cleanup_created_at",
    ]
    fieldnames = original_fields + [f for f in added_fields if f not in original_fields]

    clean_csv = manifests / "qwenvl_clean_text_manifest.csv"
    audit_csv = reports / "qwenvl_text_cleanup_audit.csv"
    write_csv(clean_csv, out_rows, fieldnames)
    write_csv(audit_csv, audit_rows, list(audit_rows[0].keys()))

    # SQLite copy for next staging step.
    sqlite_path = dbdir / "qwenvl_clean_text.sqlite"
    if sqlite_path.exists():
        sqlite_path.unlink()
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE qwenvl_clean_text (" + ",".join([f'"{c}" TEXT' for c in fieldnames]) + ")")
        placeholders = ",".join(["?"] * len(fieldnames))
        conn.executemany(
            "INSERT INTO qwenvl_clean_text VALUES (" + placeholders + ")",
            [[str(r.get(c, "")) for c in fieldnames] for r in out_rows],
        )
        conn.execute("CREATE INDEX idx_qwenvl_clean_evidence_id ON qwenvl_clean_text(evidence_id)")
        conn.execute("CREATE INDEX idx_qwenvl_clean_visual_unit_id ON qwenvl_clean_text(visual_unit_id)")
        conn.commit()
    finally:
        conn.close()

    row_count = len(rows)
    ok_count = status_counts.get("ok", 0)
    review_count = status_counts.get("review", 0)
    failed_count = status_counts.get("cleanup_failed", 0)
    path_remain = warning_counts.get("internal_path_remains", 0)
    token_remain = warning_counts.get("chat_template_token_remains", 0)
    metric_remain = warning_counts.get("runtime_metric_remains", 0)

    if failed_count == 0 and path_remain == 0 and token_remain == 0 and metric_remain == 0 and ok_count == row_count:
        validation = "PASS"
    elif failed_count == 0 and path_remain == 0 and token_remain == 0 and metric_remain == 0:
        validation = "PASS_WITH_REVIEW"
    else:
        validation = "FAIL"

    summary = {
        "validation_status": validation,
        "generated_at": started.strftime("%Y%m%d_%H%M%S"),
        "mode": "read_existing_qwenvl_manifest_only_no_model_rerun",
        "source_safety": "read_only_no_move_no_delete_no_rename_no_original_media_access_required",
        "network": "not_required_not_used",
        "model_download": "not_required_not_used",
        "run_root": args.run_root,
        "input_qwenvl_db_ready": str(in_csv),
        "quality_audit_reference": args.quality_audit,
        "row_count": row_count,
        "status_counts": status_counts,
        "warning_counts": warning_counts,
        "avg_raw_text_len": round(sum(int(r["qwen_raw_text_len"]) for r in out_rows) / row_count, 2),
        "avg_clean_text_len": round(sum(int(r["qwen_clean_text_len"]) for r in out_rows) / row_count, 2),
        "min_clean_text_len": min(int(r["qwen_clean_text_len"]) for r in out_rows),
        "max_clean_text_len": max(int(r["qwen_clean_text_len"]) for r in out_rows),
        "clean_manifest_csv": str(clean_csv),
        "cleanup_audit_csv": str(audit_csv),
        "sqlite": str(sqlite_path),
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 3),
    }

    summary_json = reports / "stop03_5a2_qwenvl_text_cleanup_summary.json"
    summary_md = reports / "stop03_5a2_qwenvl_text_cleanup_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    decision = ""
    if validation == "PASS":
        decision = "Qwen-VL wrapper text cleanup passed. Clean text can be used by Stop03-5B staging."
    elif validation == "PASS_WITH_REVIEW":
        decision = "Qwen-VL cleanup produced usable text but some rows need low-confidence/review marking in staging."
    else:
        decision = "Qwen-VL cleanup failed for some rows. Do not enter Stop03-5B until inspected."

    md = [
        "# Stop03-5A2 Qwen-VL Text Cleanup",
        "",
        f"- validation_status: `{validation}`",
        f"- generated_at: `{summary['generated_at']}`",
        f"- mode: `{summary['mode']}`",
        f"- source_safety: `{summary['source_safety']}`",
        f"- network: `{summary['network']}`",
        f"- model_download: `{summary['model_download']}`",
        "",
        "## Counts",
        f"- row_count: `{row_count}`",
        f"- status_counts: `{status_counts}`",
        f"- warning_counts: `{warning_counts}`",
        f"- avg_raw_text_len: `{summary['avg_raw_text_len']}`",
        f"- avg_clean_text_len: `{summary['avg_clean_text_len']}`",
        f"- min_clean_text_len: `{summary['min_clean_text_len']}`",
        f"- max_clean_text_len: `{summary['max_clean_text_len']}`",
        "",
        "## Decision",
        decision,
        "",
        "## Outputs",
        f"- clean_manifest_csv: `{clean_csv}`",
        f"- cleanup_audit_csv: `{audit_csv}`",
        f"- sqlite: `{sqlite_path}`",
        f"- summary_json: `{summary_json}`",
        f"- summary_md: `{summary_md}`",
        "",
    ]
    summary_md.write_text("\n".join(md), encoding="utf-8")

    print("== Stop03-5A2 Qwen-VL text cleanup finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if validation in {"PASS", "PASS_WITH_REVIEW"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
