#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5A OCR + Qwen-VL quality audit.

Reads existing DB-ready/provenance manifests only. Does NOT read, move, rename,
delete, or modify original media. Does NOT run OCR/Qwen/model inference.
Does NOT download anything.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TEXT_COL_CANDIDATES = [
    "text",
    "ocr_text",
    "recognized_text",
    "full_text",
    "caption",
    "qwenvl_caption",
    "description",
    "semantic_text",
    "evidence_text",
    "model_output_text",
    "output_text",
    "response_text",
    "raw_text",
    "content",
]
SOURCE_COL_CANDIDATES = [
    "original_source_path",
    "source_path",
    "source_media_path",
    "media_path",
    "original_path",
    "path",
    "input_path",
]
ID_COL_CANDIDATES = [
    "evidence_id",
    "visual_unit_id",
    "unit_id",
    "image_id",
    "frame_id",
    "row_id",
    "id",
]
TIME_COL_CANDIDATES = [
    "time_position_ms",
    "estimated_frame_time_ms",
    "frame_time_ms",
    "start_time_ms",
    "timestamp_ms",
]
TYPE_COL_CANDIDATES = [
    "visual_unit_type",
    "unit_type",
    "media_type",
    "source_type",
]

GENERIC_QWEN_PATTERNS = [
    "这是一张图片",
    "这张图片",
    "图中显示",
    "画面中",
    "可以看到",
    "无法确定",
    "无法判断",
    "不清楚",
    "看起来像",
]
OCR_NOISE_PATTERNS = [
    r"^[\W_]+$",
    r"^(.)\1{8,}$",
    r"^[0-9\s:：./\\\-_,，。]+$",
]

CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
DIGIT_RE = re.compile(r"[0-9]")
PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_rows(path: Path) -> List[Dict[str, Any]]:
    """Read jsonl/csv/tsv manifests robustly.

    Some generated manifests use .csv names but may be empty, one-line, tab-separated,
    or contain long JSON/text fields that make csv.Sniffer fail. This function never
    lets Sniffer be the only delimiter decision.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))

    # First inspect a small prefix.
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        sample = f.read(65536)
        f.seek(0)

        stripped = sample.lstrip()
        # Allow jsonl even when extension is not .jsonl.
        if path.suffix.lower() == ".jsonl" or stripped.startswith("{"):
            rows: List[Dict[str, Any]] = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                    else:
                        rows.append({"_jsonl_non_dict": json.dumps(obj, ensure_ascii=False)[:500]})
                except json.JSONDecodeError:
                    rows.append({"_raw_jsonl_parse_error": line[:500]})
            return rows

        if not sample:
            return []

        # Prefer deterministic delimiter detection over Sniffer for stability.
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        tab_count = first_line.count("\t")
        comma_count = first_line.count(",")
        semi_count = first_line.count(";")

        if tab_count > comma_count and tab_count >= 1:
            dialect = csv.excel_tab
        elif comma_count >= 1:
            dialect = csv.excel
        elif semi_count >= 1:
            dialect = csv.excel
            dialect.delimiter = ";"
        else:
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                # Single-column or malformed-but-readable file. Preserve raw lines.
                f.seek(0)
                return [{"_raw_line": line.rstrip("\n")} for line in f if line.strip()]

        f.seek(0)
        reader = csv.DictReader(f, dialect=dialect)
        return [dict(r) for r in reader]


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
        fieldnames = keys or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r if r else {"empty": ""})


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def first_present(row: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    lower_map = {str(k).lower(): k for k in row.keys()}
    for c in candidates:
        if c in row:
            return c
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def infer_text_col(rows: List[Dict[str, Any]], preferred: Optional[str] = None) -> Optional[str]:
    if preferred:
        return preferred
    if not rows:
        return None
    keys = list(rows[0].keys())
    key_lower = {k.lower(): k for k in keys}
    for c in TEXT_COL_CANDIDATES:
        if c in rows[0]:
            return c
        if c.lower() in key_lower:
            return key_lower[c.lower()]
    # heuristic: choose string column with highest average length and some natural text signal
    scores: List[Tuple[float, str]] = []
    sample_rows = rows[: min(100, len(rows))]
    for k in keys:
        vals = [str(r.get(k, "") or "") for r in sample_rows]
        avg_len = sum(len(v) for v in vals) / max(1, len(vals))
        cjk = sum(len(CJK_RE.findall(v)) for v in vals)
        latin = sum(len(LATIN_RE.findall(v)) for v in vals)
        if avg_len >= 5 and (cjk + latin) >= 5:
            penalty = 0
            lk = k.lower()
            if "path" in lk or "file" in lk or "sha" in lk or "hash" in lk:
                penalty += 1000
            scores.append((avg_len + cjk * 0.5 + latin * 0.1 - penalty, k))
    scores.sort(reverse=True)
    return scores[0][1] if scores and scores[0][0] > 0 else None


def infer_col(rows: List[Dict[str, Any]], candidates: List[str]) -> Optional[str]:
    if not rows:
        return None
    return first_present(rows[0], candidates)


def norm_text(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    # Sometimes text may be JSON list/dict serialized. Keep readable but compact.
    s = s.replace("\\n", "\n")
    s = SPACE_RE.sub(" ", s).strip()
    return s


def text_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


def char_metrics(text: str) -> Dict[str, Any]:
    n = len(text)
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    digits = len(DIGIT_RE.findall(text))
    spaces = sum(1 for ch in text if ch.isspace())
    punct = len(PUNCT_RE.findall(text))
    unique_chars = len(set(text)) if text else 0
    lines = [ln.strip() for ln in re.split(r"[\n;；|]", text) if ln.strip()]
    unique_lines = len(set(lines)) if lines else 0
    repeat_line_ratio = 0.0
    if lines:
        repeat_line_ratio = 1.0 - (unique_lines / max(1, len(lines)))
    return {
        "text_len": n,
        "cjk_count": cjk,
        "latin_count": latin,
        "digit_count": digits,
        "punct_count": punct,
        "space_count": spaces,
        "unique_char_count": unique_chars,
        "unique_char_ratio": round(unique_chars / n, 4) if n else 0,
        "line_count": len(lines),
        "unique_line_count": unique_lines,
        "repeat_line_ratio": round(repeat_line_ratio, 4),
        "cjk_ratio": round(cjk / n, 4) if n else 0,
        "latin_ratio": round(latin / n, 4) if n else 0,
        "digit_ratio": round(digits / n, 4) if n else 0,
    }


def max_repeated_char_run(text: str) -> int:
    max_run = 0
    cur = 0
    prev = None
    for ch in text:
        if ch == prev:
            cur += 1
        else:
            cur = 1
            prev = ch
        max_run = max(max_run, cur)
    return max_run


def audit_text(text: str, modality: str) -> Dict[str, Any]:
    m = char_metrics(text)
    reasons: List[str] = []
    score = 0
    if not text:
        reasons.append("empty_text")
        score += 100
    if m["text_len"] < (8 if modality == "ocr" else 20):
        reasons.append("too_short")
        score += 25
    if m["text_len"] > 3000:
        reasons.append("very_long_text")
        score += 10
    if m["unique_char_ratio"] < 0.08 and m["text_len"] >= 40:
        reasons.append("low_unique_char_ratio")
        score += 30
    if m["repeat_line_ratio"] > 0.5 and m["line_count"] >= 4:
        reasons.append("repeated_lines")
        score += 30
    repeated = max_repeated_char_run(text)
    if repeated >= 8:
        reasons.append("long_repeated_char_run")
        score += 25
    if modality == "ocr":
        for pat in OCR_NOISE_PATTERNS:
            if re.match(pat, text):
                reasons.append("ocr_noise_pattern")
                score += 35
                break
        if m["digit_ratio"] > 0.85 and m["text_len"] >= 20:
            reasons.append("mostly_digits")
            score += 15
        if (m["cjk_count"] + m["latin_count"]) == 0 and m["text_len"] > 0:
            reasons.append("no_language_chars")
            score += 35
    else:
        # Qwen-VL: too generic or refusal-like output is less useful.
        generic_hits = [p for p in GENERIC_QWEN_PATTERNS if p in text]
        if generic_hits and m["text_len"] < 80:
            reasons.append("generic_short_caption")
            score += 25
        if "无法" in text and ("识别" in text or "确定" in text or "判断" in text):
            reasons.append("uncertain_or_refusal_like")
            score += 25
    status = "ok"
    if score >= 60:
        status = "suspicious_high"
    elif score >= 30:
        status = "suspicious_medium"
    elif score > 0:
        status = "review_low"
    return {
        **m,
        "max_repeated_char_run": repeated,
        "quality_score": score,
        "quality_status": status,
        "quality_reasons": "|".join(reasons),
    }


def group_key(path: str) -> str:
    if not path:
        return "UNKNOWN"
    p = Path(path)
    # Stable coarse grouping: parent directory + file stem for videos/images.
    try:
        return str(p.parent / p.name)
    except Exception:
        return path


def source_bucket(path: str) -> str:
    if not path:
        return "UNKNOWN"
    p = Path(path)
    parts = p.parts
    # Keep the last 3 path components' directory context when possible.
    if len(parts) >= 4:
        return str(Path(*parts[-4:-1]))
    return str(p.parent)


def build_audit_rows(rows: List[Dict[str, Any]], modality: str, text_col: Optional[str], source_col: Optional[str], id_col: Optional[str], time_col: Optional[str], type_col: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if text_col is None:
        for i, r in enumerate(rows):
            out.append({
                "modality": modality,
                "row_index": i,
                "quality_status": "suspicious_high",
                "quality_score": 100,
                "quality_reasons": "missing_text_column",
            })
        return out
    for i, r in enumerate(rows):
        text = norm_text(r.get(text_col, ""))
        source = str(r.get(source_col, "") or "") if source_col else ""
        item_id = str(r.get(id_col, "") or "") if id_col else ""
        t = str(r.get(time_col, "") or "") if time_col else ""
        utype = str(r.get(type_col, "") or "") if type_col else ""
        metrics = audit_text(text, modality)
        out.append({
            "modality": modality,
            "row_index": i,
            "item_id": item_id,
            "visual_unit_type": utype,
            "time_position_ms": t,
            "source_path": source,
            "source_bucket": source_bucket(source),
            "text_hash16": text_hash(text),
            "text_preview_200": text[:200],
            **metrics,
        })
    return out


def summarize_audit(name: str, path: Optional[Path], rows: List[Dict[str, Any]], audit_rows: List[Dict[str, Any]], text_col: Optional[str], source_col: Optional[str]) -> Dict[str, Any]:
    status_counts = Counter(r.get("quality_status", "") for r in audit_rows)
    reason_counts: Counter[str] = Counter()
    for r in audit_rows:
        for reason in str(r.get("quality_reasons", "") or "").split("|"):
            if reason:
                reason_counts[reason] += 1
    nonempty = sum(1 for r in audit_rows if int(r.get("text_len", 0) or 0) > 0)
    suspicious = sum(1 for r in audit_rows if str(r.get("quality_status", "")).startswith("suspicious"))
    review = sum(1 for r in audit_rows if str(r.get("quality_status", "")) == "review_low")
    lens = [int(r.get("text_len", 0) or 0) for r in audit_rows]
    avg_len = sum(lens) / max(1, len(lens))
    source_count = len(set(str(r.get("source_path", "")) for r in audit_rows if r.get("source_path")))
    return {
        "name": name,
        "input_path": str(path) if path else None,
        "row_count": len(rows),
        "audit_row_count": len(audit_rows),
        "text_column": text_col,
        "source_column": source_col,
        "nonempty_text_count": nonempty,
        "empty_text_count": len(audit_rows) - nonempty,
        "suspicious_count": suspicious,
        "suspicious_ratio": round(suspicious / max(1, len(audit_rows)), 4),
        "review_low_count": review,
        "avg_text_len": round(avg_len, 2),
        "min_text_len": min(lens) if lens else 0,
        "max_text_len": max(lens) if lens else 0,
        "source_path_count": source_count,
        "quality_status_counts": dict(status_counts),
        "top_quality_reasons": dict(reason_counts.most_common(20)),
    }


def source_distribution(audit_rows: List[Dict[str, Any]], modality: str) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in audit_rows:
        grouped[str(r.get("source_path") or "UNKNOWN")].append(r)
    out = []
    for src, rs in grouped.items():
        lens = [int(r.get("text_len", 0) or 0) for r in rs]
        suspicious = sum(1 for r in rs if str(r.get("quality_status", "")).startswith("suspicious"))
        out.append({
            "modality": modality,
            "source_path": src,
            "count": len(rs),
            "suspicious_count": suspicious,
            "suspicious_ratio": round(suspicious / max(1, len(rs)), 4),
            "avg_text_len": round(sum(lens) / max(1, len(lens)), 2),
            "max_text_len": max(lens) if lens else 0,
            "source_bucket": source_bucket(src),
        })
    out.sort(key=lambda r: (-int(r["count"]), -float(r["suspicious_ratio"]), r["source_path"]))
    return out


def deterministic_samples(audit_rows: List[Dict[str, Any]], sample_per_modality: int) -> List[Dict[str, Any]]:
    if not audit_rows:
        return []
    # Mix suspicious, short, long, and source-diverse samples.
    selected: List[Dict[str, Any]] = []
    seen_idx = set()

    def add(rs: Iterable[Dict[str, Any]], limit: int) -> None:
        nonlocal selected
        for r in rs:
            key = (r.get("modality"), r.get("row_index"))
            if key in seen_idx:
                continue
            selected.append(r)
            seen_idx.add(key)
            if len(selected) >= limit:
                return

    limit = sample_per_modality
    suspicious = sorted(audit_rows, key=lambda r: (-int(r.get("quality_score", 0) or 0), -int(r.get("text_len", 0) or 0)))
    add(suspicious, max(1, limit // 3))
    long_rows = sorted(audit_rows, key=lambda r: -int(r.get("text_len", 0) or 0))
    add(long_rows, max(1, 2 * limit // 3))
    # source diverse: first per source
    by_source: Dict[str, Dict[str, Any]] = {}
    for r in audit_rows:
        by_source.setdefault(str(r.get("source_path") or "UNKNOWN"), r)
    add(by_source.values(), limit)
    if len(selected) < limit:
        add(audit_rows, limit)
    return selected[:limit]


def create_sqlite(db_path: Path, audit_rows: List[Dict[str, Any]], summaries: Dict[str, Any]) -> None:
    ensure_dir(db_path.parent)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
        CREATE TABLE quality_audit (
            modality TEXT,
            row_index INTEGER,
            item_id TEXT,
            visual_unit_type TEXT,
            time_position_ms TEXT,
            source_path TEXT,
            source_bucket TEXT,
            text_hash16 TEXT,
            text_preview_200 TEXT,
            text_len INTEGER,
            cjk_count INTEGER,
            latin_count INTEGER,
            digit_count INTEGER,
            line_count INTEGER,
            unique_char_ratio REAL,
            repeat_line_ratio REAL,
            max_repeated_char_run INTEGER,
            quality_score INTEGER,
            quality_status TEXT,
            quality_reasons TEXT
        )
        """)
        for r in audit_rows:
            con.execute("""
            INSERT INTO quality_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r.get("modality"), r.get("row_index"), r.get("item_id"), r.get("visual_unit_type"),
                r.get("time_position_ms"), r.get("source_path"), r.get("source_bucket"), r.get("text_hash16"),
                r.get("text_preview_200"), r.get("text_len"), r.get("cjk_count"), r.get("latin_count"),
                r.get("digit_count"), r.get("line_count"), r.get("unique_char_ratio"), r.get("repeat_line_ratio"),
                r.get("max_repeated_char_run"), r.get("quality_score"), r.get("quality_status"), r.get("quality_reasons")
            ))
        con.execute("CREATE TABLE audit_summary (key TEXT PRIMARY KEY, value_json TEXT)")
        for k, v in summaries.items():
            con.execute("INSERT INTO audit_summary VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()


def render_md(summary: Dict[str, Any]) -> str:
    lines = []
    lines.append("# Stop03-5A OCR + Qwen-VL Quality Audit")
    lines.append("")
    lines.append(f"- validation_status: `{summary['validation_status']}`")
    lines.append(f"- generated_at: `{summary['generated_at']}`")
    lines.append(f"- mode: `{summary['mode']}`")
    lines.append(f"- source_safety: `{summary['source_safety']}`")
    lines.append(f"- network: `{summary['network']}`")
    lines.append(f"- model_download: `{summary['model_download']}`")
    lines.append("")
    lines.append("## Inputs")
    for k, v in summary.get("inputs", {}).items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("## Modality summaries")
    for name, s in summary.get("modality_summaries", {}).items():
        lines.append(f"### {name}")
        for key in [
            "row_count", "nonempty_text_count", "empty_text_count", "suspicious_count",
            "suspicious_ratio", "review_low_count", "avg_text_len", "min_text_len", "max_text_len",
            "source_path_count", "text_column", "source_column"
        ]:
            lines.append(f"- {key}: `{s.get(key)}`")
        lines.append(f"- quality_status_counts: `{s.get('quality_status_counts')}`")
        lines.append(f"- top_quality_reasons: `{s.get('top_quality_reasons')}`")
        lines.append("")
    lines.append("## Decision")
    lines.append(summary.get("decision_note", ""))
    lines.append("")
    lines.append("## Outputs")
    for k, v in summary.get("outputs", {}).items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stop03-5A OCR + Qwen-VL quality audit")
    ap.add_argument("--run-root", type=Path, required=False, help="Parent run root, used only for summary/provenance.")
    ap.add_argument("--qwenvl-db-ready", type=Path, required=True)
    ap.add_argument("--ocr-db-ready", type=Path, required=True)
    ap.add_argument("--qwenvl-provenance", type=Path, required=False)
    ap.add_argument("--ocr-provenance", type=Path, required=False)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample-per-modality", type=int, default=60)
    ap.add_argument("--qwenvl-text-col", type=str, default=None)
    ap.add_argument("--ocr-text-col", type=str, default=None)
    ap.add_argument("--fail-suspicious-ratio", type=float, default=0.35)
    ap.add_argument("--review-suspicious-ratio", type=float, default=0.15)
    args = ap.parse_args()

    out = args.out
    reports = out / "reports"
    manifests = out / "manifests"
    database = out / "database"
    ensure_dir(reports)
    ensure_dir(manifests)
    ensure_dir(database)

    start = time.time()

    qw_rows = read_rows(args.qwenvl_db_ready)
    ocr_rows = read_rows(args.ocr_db_ready)

    # optional provenance just for count/parsing sanity, not required for quality scoring
    qw_prov_count = None
    ocr_prov_count = None
    if args.qwenvl_provenance and args.qwenvl_provenance.exists():
        qw_prov_count = len(read_rows(args.qwenvl_provenance))
    if args.ocr_provenance and args.ocr_provenance.exists():
        ocr_prov_count = len(read_rows(args.ocr_provenance))

    qw_text_col = infer_text_col(qw_rows, args.qwenvl_text_col)
    ocr_text_col = infer_text_col(ocr_rows, args.ocr_text_col)
    qw_source_col = infer_col(qw_rows, SOURCE_COL_CANDIDATES)
    ocr_source_col = infer_col(ocr_rows, SOURCE_COL_CANDIDATES)
    qw_id_col = infer_col(qw_rows, ID_COL_CANDIDATES)
    ocr_id_col = infer_col(ocr_rows, ID_COL_CANDIDATES)
    qw_time_col = infer_col(qw_rows, TIME_COL_CANDIDATES)
    ocr_time_col = infer_col(ocr_rows, TIME_COL_CANDIDATES)
    qw_type_col = infer_col(qw_rows, TYPE_COL_CANDIDATES)
    ocr_type_col = infer_col(ocr_rows, TYPE_COL_CANDIDATES)

    qw_audit = build_audit_rows(qw_rows, "qwenvl", qw_text_col, qw_source_col, qw_id_col, qw_time_col, qw_type_col)
    ocr_audit = build_audit_rows(ocr_rows, "ocr", ocr_text_col, ocr_source_col, ocr_id_col, ocr_time_col, ocr_type_col)
    all_audit = qw_audit + ocr_audit

    qw_summary = summarize_audit("qwenvl", args.qwenvl_db_ready, qw_rows, qw_audit, qw_text_col, qw_source_col)
    ocr_summary = summarize_audit("ocr", args.ocr_db_ready, ocr_rows, ocr_audit, ocr_text_col, ocr_source_col)

    suspicious_ratio_max = max(qw_summary["suspicious_ratio"], ocr_summary["suspicious_ratio"])
    missing_text_col = (qw_text_col is None) or (ocr_text_col is None)
    empty_bad = (qw_summary["empty_text_count"] > 0) or (ocr_summary["empty_text_count"] > 0)

    validation_status = "PASS"
    decision_note = "质量抽检未发现高比例明显异常。可以进入 Stop03-5B 统一证据 staging。"
    if missing_text_col:
        validation_status = "FAIL"
        decision_note = "未能识别 Qwen-VL 或 OCR 文本字段，不能继续统一入库。需要先检查 manifest 字段名。"
    elif suspicious_ratio_max >= args.fail_suspicious_ratio:
        validation_status = "FAIL"
        decision_note = "疑似低质量文本比例过高，暂不建议进入统一证据库。需要先查看 suspicious 报告并决定是否重跑或收紧队列。"
    elif suspicious_ratio_max >= args.review_suspicious_ratio or empty_bad:
        validation_status = "PASS_WITH_REVIEW"
        decision_note = "可进入统一证据 staging，但必须把 suspicious/review_low 文本标记为低置信度，后续 embedding/search 不应同权重使用。"

    output_paths = {
        "quality_audit_csv": str(manifests / "quality_audit_manifest.csv"),
        "suspicious_text_csv": str(reports / "suspicious_text_report.csv"),
        "sample_text_csv": str(reports / "text_sample_report.csv"),
        "source_distribution_csv": str(reports / "source_distribution_report.csv"),
        "sqlite": str(database / "quality_audit.sqlite"),
        "summary_json": str(reports / "stop03_5a_quality_audit_summary.json"),
        "summary_md": str(reports / "stop03_5a_quality_audit_summary.md"),
    }

    # write reports
    write_csv(Path(output_paths["quality_audit_csv"]), all_audit)
    suspicious_rows = [r for r in all_audit if str(r.get("quality_status", "")).startswith("suspicious") or r.get("quality_status") == "review_low"]
    suspicious_rows.sort(key=lambda r: (-int(r.get("quality_score", 0) or 0), r.get("modality", ""), r.get("source_path", "")))
    write_csv(Path(output_paths["suspicious_text_csv"]), suspicious_rows)
    samples = deterministic_samples(qw_audit, args.sample_per_modality) + deterministic_samples(ocr_audit, args.sample_per_modality)
    write_csv(Path(output_paths["sample_text_csv"]), samples)
    dist = source_distribution(qw_audit, "qwenvl") + source_distribution(ocr_audit, "ocr")
    write_csv(Path(output_paths["source_distribution_csv"]), dist)

    summary = {
        "validation_status": validation_status,
        "decision_note": decision_note,
        "generated_at": now_stamp(),
        "elapsed_seconds": round(time.time() - start, 3),
        "mode": "read_existing_manifests_only_no_model_rerun",
        "source_safety": "read_only_no_move_no_delete_no_rename_no_original_media_access_required",
        "network": "not_required_not_used",
        "model_download": "not_required_not_used",
        "inputs": {
            "run_root": str(args.run_root) if args.run_root else None,
            "qwenvl_db_ready": str(args.qwenvl_db_ready),
            "ocr_db_ready": str(args.ocr_db_ready),
            "qwenvl_provenance": str(args.qwenvl_provenance) if args.qwenvl_provenance else None,
            "ocr_provenance": str(args.ocr_provenance) if args.ocr_provenance else None,
        },
        "input_counts": {
            "qwenvl_db_ready_rows": len(qw_rows),
            "ocr_db_ready_rows": len(ocr_rows),
            "qwenvl_provenance_rows": qw_prov_count,
            "ocr_provenance_rows": ocr_prov_count,
        },
        "modality_summaries": {
            "qwenvl": qw_summary,
            "ocr": ocr_summary,
        },
        "thresholds": {
            "fail_suspicious_ratio": args.fail_suspicious_ratio,
            "review_suspicious_ratio": args.review_suspicious_ratio,
        },
        "outputs": output_paths,
    }

    create_sqlite(Path(output_paths["sqlite"]), all_audit, summary)
    write_json(Path(output_paths["summary_json"]), summary)
    Path(output_paths["summary_md"]).write_text(render_md(summary), encoding="utf-8")

    print("== Stop03-5A quality audit finished ==")
    print(json.dumps({
        "validation_status": validation_status,
        "elapsed_seconds": summary["elapsed_seconds"],
        "qwenvl_rows": len(qw_rows),
        "ocr_rows": len(ocr_rows),
        "qwenvl_text_col": qw_text_col,
        "ocr_text_col": ocr_text_col,
        "qwenvl_suspicious_ratio": qw_summary["suspicious_ratio"],
        "ocr_suspicious_ratio": ocr_summary["suspicious_ratio"],
        "summary_md": output_paths["summary_md"],
        "suspicious_text_csv": output_paths["suspicious_text_csv"],
    }, ensure_ascii=False, indent=2))
    return 0 if validation_status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
