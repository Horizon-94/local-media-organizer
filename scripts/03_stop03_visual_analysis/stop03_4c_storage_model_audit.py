#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-4C storage + OCR model cache audit.

Purpose:
- Audit OCR/PaddleX official model cache size and local OCR model-root size.
- Audit generated artifact sizes for Stop02 video frames, Stop02 image previews,
  YOLOE, visual embedding, Qwen-VL, OCR, and candidate queues.
- Read-only. Does not modify source media or prior outputs.

Outputs:
  <run-root>/03_4c_storage_model_audit_<timestamp>/
    reports/storage_model_audit_summary.md
    reports/storage_model_audit_summary.json
    manifests/stage_size_summary.csv
    manifests/stage_size_summary.jsonl
    manifests/ocr_model_cache_size_audit.csv
    manifests/large_files_top.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.2f} {u}"
        x /= 1024
    return f"{n} B"


def safe_rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except Exception:
        return str(p)


def scan_tree(path: Path, base_for_rel: Optional[Path] = None, max_large_files: int = 30) -> Dict[str, Any]:
    base_for_rel = base_for_rel or path
    total = 0
    file_count = 0
    dir_count = 0
    ext_counter: Counter[str] = Counter()
    largest: List[Tuple[int, str]] = []
    immediate_dir_bytes: Dict[str, int] = defaultdict(int)

    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "total_bytes": 0,
            "total_human": human_bytes(0),
            "file_count": 0,
            "dir_count": 0,
            "top_extensions": {},
            "top_immediate_dirs": {},
            "largest_files": [],
        }

    if path.is_file():
        size = path.stat().st_size
        return {
            "exists": True,
            "path": str(path),
            "total_bytes": size,
            "total_human": human_bytes(size),
            "file_count": 1,
            "dir_count": 0,
            "top_extensions": {path.suffix.lower() or "__NO_EXT__": 1},
            "top_immediate_dirs": {},
            "largest_files": [{"bytes": size, "human": human_bytes(size), "path": str(path)}],
        }

    for root, dirs, files in os.walk(path):
        rootp = Path(root)
        dir_count += len(dirs)
        for name in files:
            fp = rootp / name
            try:
                st = fp.stat()
            except Exception:
                continue
            size = st.st_size
            total += size
            file_count += 1
            ext_counter[fp.suffix.lower() or "__NO_EXT__"] += 1

            rel_parts = fp.relative_to(path).parts
            if len(rel_parts) > 1:
                immediate_dir_bytes[rel_parts[0]] += size
            else:
                immediate_dir_bytes["."] += size

            largest.append((size, str(fp)))
            if len(largest) > max_large_files * 4:
                largest = sorted(largest, reverse=True)[:max_large_files]

    largest = sorted(largest, reverse=True)[:max_large_files]
    return {
        "exists": True,
        "path": str(path),
        "total_bytes": total,
        "total_human": human_bytes(total),
        "file_count": file_count,
        "dir_count": dir_count,
        "top_extensions": dict(ext_counter.most_common(15)),
        "top_immediate_dirs": {
            k: {"bytes": v, "human": human_bytes(v)}
            for k, v in sorted(immediate_dir_bytes.items(), key=lambda kv: kv[1], reverse=True)[:20]
        },
        "largest_files": [{"bytes": b, "human": human_bytes(b), "path": p} for b, p in largest],
    }


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted(set(k for r in rows for k in r.keys()))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def latest_glob(base: Path, pattern: str) -> Optional[Path]:
    hits = [p for p in base.glob(pattern) if p.exists()]
    if not hits:
        return None
    return sorted(hits, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def first_glob(base: Path, patterns: List[str]) -> Optional[Path]:
    for pat in patterns:
        hits = sorted([p for p in base.glob(pat) if p.exists()])
        if hits:
            return hits[0]
    return None


def count_csv_rows(path: Path) -> Optional[int]:
    if not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            count = sum(1 for _ in reader)
        return max(0, count - 1)
    except Exception:
        return None


def count_jsonl_rows(path: Path) -> Optional[int]:
    if not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return None


def collect_stage_paths(run_root: Path) -> List[Dict[str, Any]]:
    paths: List[Dict[str, Any]] = []

    explicit = [
        ("stop02_video_frames", run_root / "02_1_stop02_video_frames"),
        ("stop02_image_preview", run_root / "02_2_stop02_image_preview"),
        ("stop03_2_candidate_queues_fix_v2", run_root / "03_2_stop03_candidate_queues_fix_v2_20260708_164229"),
    ]
    for name, p in explicit:
        if p.exists():
            paths.append({"stage": name, "path": p, "note": "explicit"})

    # latest / known patterns
    patterns = [
        ("stop03_1_visual_then_yoloe4", "03_1_stop03_visual_then_yoloe4_*"),
        ("stop03_1a_yoloe_only", "03_1a_stop03_yoloe_only_clean_*"),
        ("stop03_1b_openclip_smoke20", "03_1b_stop03_openclip_visual_embedding_smoke20_*"),
        ("stop03_3b_qwenvl_smoke", "03_3b_qwenvl_smoke_*"),
        ("stop03_3c_qwenvl_full", "03_3c_qwenvl_full_*"),
        ("stop03_4a_ocr_smoke", "03_4a_ocr_smoke_*"),
        ("stop03_4b_ocr_full", "03_4b_ocr_full_*"),
    ]
    for stage, pat in patterns:
        p = latest_glob(run_root, pat)
        if p:
            paths.append({"stage": stage, "path": p, "note": "latest_glob"})

    # Specific subdirs inside sequential Stop03-1 if present.
    seq = latest_glob(run_root, "03_1_stop03_visual_then_yoloe4_*")
    if seq:
        for child_pat, stage in [
            ("*visual*", "stop03_1_visual_embedding_subdir"),
            ("*yolo*", "stop03_1_yoloe_subdir"),
            ("*combined*", "stop03_1_combined_report_subdir"),
        ]:
            for p in sorted(seq.glob(child_pat)):
                if p.is_dir():
                    paths.append({"stage": stage, "path": p, "note": f"subdir_of_{seq.name}"})

    # De-duplicate by path.
    out = []
    seen = set()
    for item in paths:
        s = str(item["path"])
        if s not in seen:
            seen.add(s)
            out.append(item)
    return out


def audit_model_cache(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    dirs = [
        ("local_model_ocr_root", Path(args.local_ocr_model_root)),
        ("paddlex_official_models_root", Path(args.paddlex_cache_root)),
    ]
    model_rows: List[Dict[str, Any]] = []

    for label, root in dirs:
        s = scan_tree(root, max_large_files=20)
        model_rows.append({
            "scope": label,
            "model_name": "__ROOT__",
            "path": str(root),
            "exists": s["exists"],
            "total_bytes": s["total_bytes"],
            "total_human": s["total_human"],
            "file_count": s["file_count"],
            "dir_count": s["dir_count"],
            "top_extensions_json": json.dumps(s["top_extensions"], ensure_ascii=False),
        })

        if root.exists() and root.is_dir():
            for child in sorted([p for p in root.iterdir() if p.is_dir()]):
                cs = scan_tree(child, max_large_files=10)
                model_rows.append({
                    "scope": label,
                    "model_name": child.name,
                    "path": str(child),
                    "exists": cs["exists"],
                    "total_bytes": cs["total_bytes"],
                    "total_human": cs["total_human"],
                    "file_count": cs["file_count"],
                    "dir_count": cs["dir_count"],
                    "top_extensions_json": json.dumps(cs["top_extensions"], ensure_ascii=False),
                })

    total_summary = {
        "local_ocr_model_root": str(Path(args.local_ocr_model_root)),
        "paddlex_cache_root": str(Path(args.paddlex_cache_root)),
        "rows": len(model_rows),
    }
    return model_rows, total_summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--source-root", default="/Users/yourname/Documents/001DZLtest")
    ap.add_argument("--local-ocr-model-root", default="/Users/yourname/Documents/model/ocr")
    ap.add_argument("--paddlex-cache-root", default=str(Path.home() / ".paddlex/official_models"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    run_root = Path(args.run_root)
    if not run_root.exists():
        raise SystemExit(f"run-root not found: {run_root}")

    out_dir = Path(args.out) if args.out else run_root / f"03_4c_storage_model_audit_{now_stamp()}"
    manifests = out_dir / "manifests"
    reports = out_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    stage_rows: List[Dict[str, Any]] = []
    large_file_rows: List[Dict[str, Any]] = []

    for item in collect_stage_paths(run_root):
        stage = item["stage"]
        p = item["path"]
        s = scan_tree(p, base_for_rel=run_root, max_large_files=25)
        stage_rows.append({
            "stage": stage,
            "path": str(p),
            "exists": s["exists"],
            "total_bytes": s["total_bytes"],
            "total_human": s["total_human"],
            "file_count": s["file_count"],
            "dir_count": s["dir_count"],
            "top_extensions_json": json.dumps(s["top_extensions"], ensure_ascii=False),
            "top_immediate_dirs_json": json.dumps(s["top_immediate_dirs"], ensure_ascii=False),
            "note": item["note"],
        })
        for lf in s["largest_files"]:
            large_file_rows.append({
                "stage": stage,
                "bytes": lf["bytes"],
                "human": lf["human"],
                "path": lf["path"],
            })

    # Known manifest counts for quick sanity.
    key_files = []
    for pattern, label in [
        ("02_1_stop02_video_frames/**/video_frame_c4s_step01_queue_manifest.csv", "stop02_video_frame_manifest"),
        ("02_2_stop02_image_preview/**/image_preview_visual_unit_manifest.jsonl", "stop02_image_preview_visual_unit_manifest"),
        ("03_2_stop03_candidate_queues_fix_v2_*/manifests/qwenvl_high_value_candidate_queue.csv", "stop03_2_qwenvl_queue"),
        ("03_2_stop03_candidate_queues_fix_v2_*/manifests/ocr_trigger_candidate_queue.csv", "stop03_2_ocr_queue"),
        ("03_3c_qwenvl_full_*/manifests/qwenvl_db_ready_evidence_manifest.csv", "qwenvl_db_ready_evidence"),
        ("03_4b_ocr_full_*/manifests/ocr_db_ready_evidence_manifest.csv", "ocr_db_ready_evidence"),
    ]:
        p = latest_glob(run_root, pattern)
        if p:
            key_files.append({
                "label": label,
                "path": str(p),
                "rows_csv": count_csv_rows(p) if p.suffix.lower() == ".csv" else None,
                "rows_jsonl": count_jsonl_rows(p) if p.suffix.lower() == ".jsonl" else None,
                "bytes": p.stat().st_size,
                "human": human_bytes(p.stat().st_size),
            })

    model_rows, model_meta = audit_model_cache(args)

    write_csv(manifests / "stage_size_summary.csv", stage_rows)
    write_jsonl(manifests / "stage_size_summary.jsonl", stage_rows)
    write_csv(manifests / "large_files_top.csv", sorted(large_file_rows, key=lambda r: r["bytes"], reverse=True)[:200])
    write_csv(manifests / "ocr_model_cache_size_audit.csv", model_rows)
    write_csv(manifests / "key_manifest_counts.csv", key_files)

    total_generated_bytes = sum(r["total_bytes"] for r in stage_rows if r["exists"])
    summary = {
        "status": "PASS",
        "source_safety": "read_only_no_original_media_modification",
        "run_root": str(run_root),
        "source_root": args.source_root,
        "out": str(out_dir),
        "total_audited_generated_bytes": total_generated_bytes,
        "total_audited_generated_human": human_bytes(total_generated_bytes),
        "stage_count": len(stage_rows),
        "model_cache_meta": model_meta,
        "stage_rows": stage_rows,
        "model_rows": model_rows,
        "key_manifest_counts": key_files,
        "outputs": {
            "stage_size_summary_csv": str(manifests / "stage_size_summary.csv"),
            "model_cache_csv": str(manifests / "ocr_model_cache_size_audit.csv"),
            "large_files_csv": str(manifests / "large_files_top.csv"),
            "key_manifest_counts_csv": str(manifests / "key_manifest_counts.csv"),
        }
    }

    (reports / "storage_model_audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    lines = [
        "# Stop03-4C Storage + OCR Model Cache Audit",
        "",
        f"- status: {summary['status']}",
        f"- source_safety: {summary['source_safety']}",
        f"- run_root: {summary['run_root']}",
        f"- total_audited_generated_size: {summary['total_audited_generated_human']}",
        "",
        "## Stage sizes",
        "",
        "| stage | size | files | path |",
        "|---|---:|---:|---|",
    ]
    for r in sorted(stage_rows, key=lambda x: x["total_bytes"], reverse=True):
        lines.append(f"| {r['stage']} | {r['total_human']} | {r['file_count']} | `{r['path']}` |")

    lines.extend([
        "",
        "## OCR model/cache sizes",
        "",
        "| scope | model_name | size | files | path |",
        "|---|---|---:|---:|---|",
    ])
    for r in sorted(model_rows, key=lambda x: x["total_bytes"], reverse=True):
        lines.append(f"| {r['scope']} | {r['model_name']} | {r['total_human']} | {r['file_count']} | `{r['path']}` |")

    lines.extend([
        "",
        "## Key manifest counts",
        "",
        "| label | rows | size | path |",
        "|---|---:|---:|---|",
    ])
    for k in key_files:
        rows = k.get("rows_csv")
        if rows is None:
            rows = k.get("rows_jsonl")
        lines.append(f"| {k['label']} | {rows} | {k['human']} | `{k['path']}` |")

    lines.extend([
        "",
        "## Output files",
        f"- stage_size_summary_csv: `{summary['outputs']['stage_size_summary_csv']}`",
        f"- model_cache_csv: `{summary['outputs']['model_cache_csv']}`",
        f"- large_files_csv: `{summary['outputs']['large_files_csv']}`",
        f"- key_manifest_counts_csv: `{summary['outputs']['key_manifest_counts_csv']}`",
    ])

    (reports / "storage_model_audit_summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({
        "status": "PASS",
        "out": str(out_dir),
        "summary_md": str(reports / "storage_model_audit_summary.md"),
        "total_audited_generated_human": summary["total_audited_generated_human"],
        "stage_count": len(stage_rows),
        "model_row_count": len(model_rows),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
