#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5A3 Qwen-VL clean text rerun v2

Purpose:
- Re-run Qwen-VL for an existing Qwen candidate manifest using a cleaner prompt and higher max_tokens.
- Write clean assistant text directly, separating raw stdout and runtime metrics.
- Keep original media read-only. This script reads only derived preview/frame images referenced by manifests.
- No network, no model download; model path must be local.

Inputs:
- --qwenvl-clean: previous qwenvl_clean_text_manifest.csv or db-ready Qwen manifest containing runtime_input_image_path.

Outputs:
- manifests/qwenvl_clean_text_manifest.csv
- manifests/qwenvl_clean_text_manifest.jsonl
- database/qwenvl_clean_text_rerun.sqlite
- outputs/clean_text/*.txt
- outputs/raw_stdout/*.txt
- outputs/stderr/*.txt
- outputs/metrics/*.json
- reports/stop03_5a3_qwenvl_clean_rerun_v2_summary.md/json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_VERSION = "stop03_5a3_qwenvl_clean_rerun_v2.0"
DEFAULT_PROMPT = (
    "只输出中文画面分析正文，不要输出文件路径、Prompt、assistant、token、速度、显存、代码块。\n"
    "按固定结构输出，内容要短而完整：\n"
    "1）概括：一句话说明画面。\n"
    "2）元素：人物、物体、场景、动作、环境、文字区域。没有就写“无”。\n"
    "3）检索价值：说明适合用哪些关键词检索，以及为什么有素材价值。\n"
    "只基于画面可见信息，不要编造。"
)

METRIC_PATTERNS = {
    "prompt_tokens": re.compile(r"Prompt:\s*([0-9]+)\s*tokens", re.I),
    "prompt_tokens_per_sec": re.compile(r"Prompt:\s*[0-9]+\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", re.I),
    "generation_tokens": re.compile(r"Generation:\s*([0-9]+)\s*tokens", re.I),
    "generation_tokens_per_sec": re.compile(r"Generation:\s*[0-9]+\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", re.I),
    "peak_memory_gb": re.compile(r"Peak memory:\s*([0-9.]+)\s*GB", re.I),
}

WRAPPER_MARKERS = [
    "Files:", "Prompt:", "Generation:", "Peak memory:", "tokens-per-sec",
    "<|im_start|>", "<|im_end|>", "<|vision_start|>", "<|vision_end|>", "<|image_pad|>",
]
TAIL_BAD_PATTERNS = [
    r"[，,、：:；;（(]$",
    r"(因为|由于|因此|所以|应为|背景为|场景为|可见|包括|例如|主要是|可能是)$",
    r"(人物|物体|场景|动作|环境|文字区域)[:：]?\s*$",
]


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_id(value: str) -> str:
    v = sha256_text(value or "empty")[:16]
    return v


def ensure_dirs(out: Path) -> None:
    for p in [
        out / "manifests",
        out / "reports",
        out / "database",
        out / "outputs/clean_text",
        out / "outputs/raw_stdout",
        out / "outputs/stderr",
        out / "outputs/metrics",
        out / "checkpoints",
        out / "telemetry",
    ]:
        p.mkdir(parents=True, exist_ok=True)


def read_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() == ".jsonl":
        rows = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    rows.append({str(k): "" if v is None else str(v) for k, v in obj.items()})
        return rows
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except Exception:
            dialect = csv.excel_tab if "\t" in sample.splitlines()[0] else csv.excel
        return list(csv.DictReader(f, dialect=dialect))


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted(set(k for r in rows for k in r.keys())) or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def first_existing(row: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def extract_runtime_metrics(raw: str) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for key, pat in METRIC_PATTERNS.items():
        m = pat.search(raw or "")
        if not m:
            metrics[key] = None
            continue
        val = m.group(1)
        try:
            metrics[key] = int(val) if key.endswith("tokens") else float(val)
        except Exception:
            metrics[key] = None
    return metrics


def extract_clean_assistant_text(raw_stdout: str) -> str:
    text = (raw_stdout or "").replace("\r\n", "\n").replace("\r", "\n")

    # Drop file banner before prompt/assistant.
    if "<|im_start|>assistant" in text:
        text = text.split("<|im_start|>assistant", 1)[1]
    elif "assistant\n" in text:
        text = text.split("assistant\n", 1)[1]

    # Drop trailing runtime block. mlx-vlm prints a separator line before metrics.
    for marker in ["\n==========\nPrompt:", "\nPrompt:", "\nGeneration:", "\nPeak memory:"]:
        if marker in text:
            text = text.split(marker, 1)[0]

    # Remove remaining chat-template/control tokens and common wrapper lines.
    text = re.sub(r"<\|[^|]+\|>", "", text)
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("Files:") or s.startswith("Prompt:") or s.startswith("Generation:") or s.startswith("Peak memory:"):
            continue
        if s == "==========":
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines).strip()

    # Collapse excessive blank lines.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def detect_text_issues(clean: str, raw: str, metrics: Dict[str, Any], max_tokens: int) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    if not clean.strip():
        return "failed", ["empty_clean_text"]
    if len(clean) < 80:
        warnings.append("too_short")
    if any(m in clean for m in WRAPPER_MARKERS) or "/Users/" in clean:
        warnings.append("wrapper_or_internal_text_remains")
    gen_tokens = metrics.get("generation_tokens")
    if isinstance(gen_tokens, int) and gen_tokens >= max_tokens - 2:
        warnings.append("generation_reached_max_tokens")
    tail = clean.strip()[-12:]
    if any(re.search(p, tail) for p in TAIL_BAD_PATTERNS):
        warnings.append("likely_truncated_by_sentence_tail")
    if len(clean) > 2000:
        warnings.append("too_long")
    status = "ok" if not warnings else "review"
    return status, warnings


class ProgressLogger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: Dict[str, Any]) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_one(row: Dict[str, str], idx: int, args: argparse.Namespace, out: Path, progress: ProgressLogger) -> Dict[str, Any]:
    evidence_id = first_existing(row, ["evidence_id", "candidate_runtime_id", "candidate_id", "visual_unit_id"]) or f"qwenvl_rerun_{idx:06d}"
    visual_unit_id = first_existing(row, ["visual_unit_id", "candidate_runtime_id", "candidate_id"])
    image_path = first_existing(row, ["runtime_input_image_path", "image_path", "runtime_visual_file", "visual_file", "preview_path"])
    sid = f"{idx:06d}_{safe_id(evidence_id + image_path)}"

    raw_path = out / "outputs/raw_stdout" / f"{sid}.txt"
    err_path = out / "outputs/stderr" / f"{sid}.txt"
    clean_path = out / "outputs/clean_text" / f"{sid}.txt"
    metrics_path = out / "outputs/metrics" / f"{sid}.json"

    base: Dict[str, Any] = dict(row)
    base.update({
        "rerun_contract_version": CONTRACT_VERSION,
        "rerun_idx": idx,
        "rerun_evidence_id": evidence_id,
        "visual_unit_id": visual_unit_id,
        "runtime_input_image_path": image_path,
        "qwen_model_path": args.model,
        "qwen_python": args.qwen_python,
        "qwen_prompt_version": "v2_short_complete",
        "qwen_max_tokens": args.max_tokens,
        "qwen_temperature": args.temperature,
        "qwen_top_p": args.top_p,
        "created_at": now_ts(),
    })

    if not image_path or not Path(image_path).exists():
        base.update({
            "status": "failed",
            "qwen_text_cleanup_status": "failed",
            "qwen_text_cleanup_warnings": "missing_input_image",
            "returncode": -998,
            "elapsed_seconds": 0,
            "qwen_clean_text": "",
            "qwen_clean_text_path": "",
            "qwen_raw_stdout_path": "",
            "qwen_stderr_path": "",
            "qwen_runtime_metrics_path": "",
        })
        progress.write({"idx": idx, "status": "failed", "reason": "missing_input_image", "image_path": image_path})
        return base

    cmd = [
        args.qwen_python,
        "-m", "mlx_vlm.generate",
        "--model", args.model,
        "--image", image_path,
        "--prompt", args.prompt,
        "--max-tokens", str(args.max_tokens),
    ]
    # Optional CLI knobs; mlx-vlm versions may ignore unknown args, so only add when requested by user.
    if args.add_temperature_args:
        cmd.extend(["--temp", str(args.temperature)])
        cmd.extend(["--top-p", str(args.top_p)])

    env = os.environ.copy()
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    env.setdefault("HF_DATASETS_OFFLINE", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    t0 = time.perf_counter()
    stdout = ""
    stderr = ""
    returncode = -999
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout, env=env)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = (e.stderr or "") + "\nTIMEOUT_EXPIRED"
        returncode = -997
    except Exception as e:
        stderr = repr(e)
        returncode = -999
    elapsed = time.perf_counter() - t0

    raw_path.write_text(stdout, encoding="utf-8", errors="replace")
    err_path.write_text(stderr, encoding="utf-8", errors="replace")
    metrics = extract_runtime_metrics(stdout + "\n" + stderr)
    metrics.update({
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 3),
        "command": cmd,
    })
    clean = extract_clean_assistant_text(stdout)
    text_status, warnings = detect_text_issues(clean, stdout, metrics, args.max_tokens)
    clean_path.write_text(clean, encoding="utf-8", errors="replace")
    write_json(metrics_path, metrics)

    status = "success" if returncode == 0 and clean.strip() else "failed"
    if status == "failed" and text_status == "ok":
        text_status = "review"
        warnings.append("nonzero_returncode_or_empty_stdout")

    base.update({
        "status": status,
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 3),
        "qwen_text": clean,  # New contract: clean text only.
        "qwen_clean_text": clean,
        "qwen_text_preview": clean[:500],
        "qwen_clean_text_preview": clean[:500],
        "qwen_text_sha256": sha256_text(clean),
        "qwen_clean_text_sha256": sha256_text(clean),
        "qwen_raw_stdout_sha256": sha256_text(stdout),
        "qwen_stderr_sha256": sha256_text(stderr),
        "qwen_output_text_path": str(clean_path),
        "qwen_clean_text_path": str(clean_path),
        "qwen_raw_stdout_path": str(raw_path),
        "qwen_stderr_path": str(err_path),
        "qwen_runtime_metrics_path": str(metrics_path),
        "qwen_runtime_metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
        "qwen_text_cleanup_status": text_status,
        "qwen_text_cleanup_warnings": "|".join(sorted(set(warnings))),
        "generation_tokens": metrics.get("generation_tokens"),
        "prompt_tokens": metrics.get("prompt_tokens"),
        "peak_memory_gb": metrics.get("peak_memory_gb"),
        "stdout_chars": len(stdout),
        "stderr_chars": len(stderr),
        "runtime_input_image_sha256": row.get("runtime_input_image_sha256") or (sha256_file(Path(image_path)) if Path(image_path).exists() else ""),
    })
    progress.write({
        "idx": idx,
        "status": status,
        "text_status": text_status,
        "warnings": warnings,
        "elapsed_seconds": round(elapsed, 3),
        "visual_unit_id": visual_unit_id,
        "image_path": image_path,
    })
    return base


def write_sqlite(db_path: Path, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("pragma journal_mode=wal")
    conn.execute("""
    create table qwenvl_clean_rerun (
        rerun_idx integer,
        evidence_id text,
        visual_unit_id text,
        status text,
        cleanup_status text,
        cleanup_warnings text,
        runtime_input_image_path text,
        qwen_clean_text text,
        qwen_clean_text_sha256 text,
        qwen_raw_stdout_path text,
        qwen_stderr_path text,
        qwen_runtime_metrics_path text,
        elapsed_seconds real,
        returncode integer,
        generation_tokens integer,
        prompt_tokens integer,
        peak_memory_gb real
    )
    """)
    for r in rows:
        conn.execute("""
        insert into qwenvl_clean_rerun values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(r.get("rerun_idx") or 0),
            r.get("evidence_id") or r.get("rerun_evidence_id"),
            r.get("visual_unit_id"),
            r.get("status"),
            r.get("qwen_text_cleanup_status"),
            r.get("qwen_text_cleanup_warnings"),
            r.get("runtime_input_image_path"),
            r.get("qwen_clean_text"),
            r.get("qwen_clean_text_sha256"),
            r.get("qwen_raw_stdout_path"),
            r.get("qwen_stderr_path"),
            r.get("qwen_runtime_metrics_path"),
            float(r.get("elapsed_seconds") or 0),
            int(r.get("returncode") or 0),
            int(r.get("generation_tokens") or 0) if str(r.get("generation_tokens") or "").isdigit() else None,
            int(r.get("prompt_tokens") or 0) if str(r.get("prompt_tokens") or "").isdigit() else None,
            float(r.get("peak_memory_gb") or 0) if str(r.get("peak_memory_gb") or "") not in ("", "None") else None,
        ))
    conn.execute("create table run_summary (key text primary key, value text)")
    for k, v in summary.items():
        conn.execute("insert or replace into run_summary values (?,?)", (k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)))
    conn.commit()
    conn.close()


def write_summary(out: Path, summary: Dict[str, Any]) -> None:
    write_json(out / "reports/stop03_5a3_qwenvl_clean_rerun_v2_summary.json", summary)
    md = [
        "# Stop03-5A3 Qwen-VL Clean Text Rerun v2",
        "",
        f"- validation_status: `{summary.get('validation_status')}`",
        f"- contract_version: `{CONTRACT_VERSION}`",
        f"- mode: `rerun_qwenvl_on_existing_derived_images_only`",
        f"- source_safety: `read_only_no_move_no_delete_no_rename_no_original_media_access_required`",
        f"- network: `disabled_by_offline_env_vars_no_download_logic`",
        f"- model_download: `not_allowed_model_path_must_be_local`",
        "",
        "## Counts",
        f"- input_rows: `{summary.get('input_rows')}`",
        f"- selected_rows: `{summary.get('selected_rows')}`",
        f"- success_count: `{summary.get('success_count')}`",
        f"- failed_count: `{summary.get('failed_count')}`",
        f"- cleanup_ok_count: `{summary.get('cleanup_ok_count')}`",
        f"- cleanup_review_count: `{summary.get('cleanup_review_count')}`",
        f"- cleanup_failed_count: `{summary.get('cleanup_failed_count')}`",
        f"- warning_counts: `{summary.get('warning_counts')}`",
        "",
        "## Timing",
        f"- wall_seconds: `{summary.get('wall_seconds')}`",
        f"- sum_task_seconds: `{summary.get('sum_task_seconds')}`",
        f"- avg_task_seconds: `{summary.get('avg_task_seconds')}`",
        f"- effective_wall_seconds_per_image: `{summary.get('effective_wall_seconds_per_image')}`",
        "",
        "## Settings",
        f"- workers: `{summary.get('workers')}`",
        f"- max_tokens: `{summary.get('max_tokens')}`",
        f"- qwen_python: `{summary.get('qwen_python')}`",
        f"- model: `{summary.get('model')}`",
        "",
        "## Decision",
        summary.get("decision", ""),
        "",
        "## Outputs",
        f"- clean_manifest_csv: `{summary.get('clean_manifest_csv')}`",
        f"- sqlite: `{summary.get('sqlite')}`",
        f"- progress_jsonl: `{summary.get('progress_jsonl')}`",
    ]
    (out / "reports/stop03_5a3_qwenvl_clean_rerun_v2_summary.md").write_text("\n".join(md), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--qwenvl-clean", required=True, help="Previous qwenvl_clean_text_manifest.csv or Qwen db-ready manifest")
    ap.add_argument("--out", required=True)
    ap.add_argument("--qwen-python", default="/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python")
    ap.add_argument("--model", default="/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=384)
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--top-p", type=float, default=1.0)
    ap.add_argument("--add-temperature-args", action="store_true", help="Add --temp/--top-p to mlx_vlm.generate; disabled by default for CLI compatibility")
    ap.add_argument("--limit", type=int, default=0, help="0 means full")
    ap.add_argument("--expect-count", type=int, default=268)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args()

    out = Path(args.out)
    ensure_dirs(out)
    t0 = time.perf_counter()

    problems: List[str] = []
    model_path = Path(args.model)
    qwen_python = Path(args.qwen_python)
    if not model_path.exists():
        problems.append(f"missing_model_path:{model_path}")
    if not qwen_python.exists():
        problems.append(f"missing_qwen_python:{qwen_python}")
    if problems:
        summary = {
            "validation_status": "FAIL",
            "problems": problems,
            "run_root": args.run_root,
            "input": args.qwenvl_clean,
            "out": str(out),
        }
        write_summary(out, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    rows = read_rows(Path(args.qwenvl_clean))
    selected = rows[:args.limit] if args.limit and args.limit > 0 else rows
    write_csv(out / "manifests/qwenvl_rerun_selected_queue.csv", selected)

    progress = ProgressLogger(out / "checkpoints/run_progress.jsonl")
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = [pool.submit(run_one, r, i, args, out, progress) for i, r in enumerate(selected, 1)]
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: int(r.get("rerun_idx") or 0))

    fields = list(dict.fromkeys(
        list(rows[0].keys() if rows else []) + [
            "rerun_contract_version", "rerun_idx", "rerun_evidence_id", "qwen_prompt_version", "qwen_max_tokens", "qwen_temperature", "qwen_top_p",
            "status", "returncode", "elapsed_seconds", "qwen_text", "qwen_clean_text", "qwen_text_preview", "qwen_clean_text_preview",
            "qwen_text_sha256", "qwen_clean_text_sha256", "qwen_raw_stdout_sha256", "qwen_stderr_sha256",
            "qwen_output_text_path", "qwen_clean_text_path", "qwen_raw_stdout_path", "qwen_stderr_path", "qwen_runtime_metrics_path", "qwen_runtime_metrics_json",
            "qwen_text_cleanup_status", "qwen_text_cleanup_warnings", "generation_tokens", "prompt_tokens", "peak_memory_gb", "stdout_chars", "stderr_chars",
            "created_at",
        ]
    ))
    clean_manifest = out / "manifests/qwenvl_clean_text_manifest.csv"
    write_csv(clean_manifest, results, fields)
    write_jsonl(out / "manifests/qwenvl_clean_text_manifest.jsonl", results)

    success = sum(1 for r in results if r.get("status") == "success")
    failed = len(results) - success
    ok = sum(1 for r in results if r.get("qwen_text_cleanup_status") == "ok")
    review = sum(1 for r in results if r.get("qwen_text_cleanup_status") == "review")
    cleanup_failed = sum(1 for r in results if r.get("qwen_text_cleanup_status") == "failed")
    warn_counts: Dict[str, int] = {}
    for r in results:
        for w in (r.get("qwen_text_cleanup_warnings") or "").split("|"):
            if w:
                warn_counts[w] = warn_counts.get(w, 0) + 1
    wall = time.perf_counter() - t0
    sum_task = sum(float(r.get("elapsed_seconds") or 0) for r in results)

    validation = "PASS"
    if failed or cleanup_failed:
        validation = "FAIL"
    elif review:
        validation = "PASS_WITH_REVIEW"
    if args.expect_count and len(results) != args.expect_count:
        problems.append(f"count_mismatch:expected_{args.expect_count}_got_{len(results)}")
        validation = "FAIL" if validation == "PASS" else validation

    summary: Dict[str, Any] = {
        "validation_status": validation,
        "generated_at": now_ts(),
        "run_root": args.run_root,
        "input_qwenvl_clean": args.qwenvl_clean,
        "out": str(out),
        "input_rows": len(rows),
        "selected_rows": len(results),
        "success_count": success,
        "failed_count": failed,
        "cleanup_ok_count": ok,
        "cleanup_review_count": review,
        "cleanup_failed_count": cleanup_failed,
        "warning_counts": warn_counts,
        "problems": problems,
        "workers": args.workers,
        "max_tokens": args.max_tokens,
        "timeout": args.timeout,
        "qwen_python": args.qwen_python,
        "model": args.model,
        "wall_seconds": round(wall, 3),
        "sum_task_seconds": round(sum_task, 3),
        "avg_task_seconds": round(sum_task / len(results), 3) if results else None,
        "effective_wall_seconds_per_image": round(wall / len(results), 3) if results else None,
        "clean_manifest_csv": str(clean_manifest),
        "sqlite": str(out / "database/qwenvl_clean_text_rerun.sqlite"),
        "progress_jsonl": str(out / "checkpoints/run_progress.jsonl"),
        "decision": "Qwen-VL clean rerun passed. Use this clean manifest as QWENVL_CLEAN for Stop03-5B rerun." if validation == "PASS" else "Review or fix failed/review rows before using this manifest for final staging.",
    }
    write_sqlite(out / "database/qwenvl_clean_text_rerun.sqlite", results, summary)
    write_summary(out, summary)

    print("== Stop03-5A3 Qwen-VL clean rerun v2 finished ==")
    print(json.dumps({
        "validation_status": validation,
        "elapsed_seconds": summary["wall_seconds"],
        "selected_rows": len(results),
        "success_count": success,
        "failed_count": failed,
        "cleanup_ok_count": ok,
        "cleanup_review_count": review,
        "cleanup_failed_count": cleanup_failed,
        "warning_counts": warn_counts,
        "clean_manifest_csv": str(clean_manifest),
        "summary_md": str(out / "reports/stop03_5a3_qwenvl_clean_rerun_v2_summary.md"),
    }, ensure_ascii=False, indent=2))
    return 0 if validation in ("PASS", "PASS_WITH_REVIEW") else 3


if __name__ == "__main__":
    raise SystemExit(main())
