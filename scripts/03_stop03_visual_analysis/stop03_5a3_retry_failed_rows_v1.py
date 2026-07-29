#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5A3 retry failed/review Qwen-VL rows v1

Purpose:
- Read a previous Stop03-5A3 qwenvl_clean_text_manifest.csv.
- Retry only failed/review/problem rows, not all 268 rows.
- Merge successful old rows + retry rows into a new clean manifest.
- Keep original media read-only; reads only derived images already referenced by manifest.
- No network/download; local model path only.

Outputs:
- manifests/qwenvl_clean_text_manifest.csv/jsonl   (merged 268 rows)
- manifests/qwenvl_retry_queue.csv                 (only bad rows)
- manifests/qwenvl_retry_results.csv               (only retry outputs)
- reports/stop03_5a3_retry_failed_rows_summary.md/json
- database/qwenvl_retry_failed_rows.sqlite
- checkpoints/run_progress.jsonl
"""
from __future__ import annotations

import argparse, csv, hashlib, json, os, re, sqlite3, subprocess, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_VERSION = "stop03_5a3_retry_failed_rows_v1.0"
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
WRAPPER_MARKERS = ["Files:", "Prompt:", "Generation:", "Peak memory:", "tokens-per-sec", "<|im_start|>", "<|im_end|>", "<|vision_start|>", "<|vision_end|>", "<|image_pad|>"]
TAIL_BAD_PATTERNS = [r"[，,、：:；;（(]$", r"(因为|由于|因此|所以|应为|背景为|场景为|可见|包括|例如|主要是|可能是)$", r"(人物|物体|场景|动作|环境|文字区域)[:：]?\s*$"]


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
    return sha256_text(value or "empty")[:16]

def ensure_dirs(out: Path) -> None:
    for p in ["manifests", "reports", "database", "outputs/clean_text", "outputs/raw_stdout", "outputs/stderr", "outputs/metrics", "checkpoints"]:
        (out / p).mkdir(parents=True, exist_ok=True)

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
            first = sample.splitlines()[0] if sample.splitlines() else ""
            dialect = csv.excel_tab if "\t" in first else csv.excel
        return list(csv.DictReader(f, dialect=dialect))

def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(dict.fromkeys([k for r in rows for k in r.keys()])) or ["empty"]
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

def row_key(row: Dict[str, Any]) -> str:
    return first_existing(row, ["evidence_id", "rerun_evidence_id", "candidate_runtime_id", "candidate_id", "visual_unit_id", "runtime_input_image_path"])

def is_bad_row(row: Dict[str, str], include_review: bool) -> bool:
    status = (row.get("status") or "").strip().lower()
    cstatus = (row.get("qwen_text_cleanup_status") or "").strip().lower()
    warnings = row.get("qwen_text_cleanup_warnings") or ""
    clean = row.get("qwen_clean_text") or row.get("qwen_text") or ""
    if status != "success":
        return True
    if cstatus in ("failed", ""):
        return True
    if "empty_clean_text" in warnings or not clean.strip():
        return True
    if include_review and cstatus == "review":
        return True
    return False

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
    if "<|im_start|>assistant" in text:
        text = text.split("<|im_start|>assistant", 1)[1]
    elif "assistant\n" in text:
        text = text.split("assistant\n", 1)[1]
    for marker in ["\n==========\nPrompt:", "\nPrompt:", "\nGeneration:", "\nPeak memory:"]:
        if marker in text:
            text = text.split(marker, 1)[0]
    text = re.sub(r"<\|[^|]+\|>", "", text)
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith(("Files:", "Prompt:", "Generation:", "Peak memory:")):
            continue
        if s == "==========":
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

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
    return ("ok" if not warnings else "review"), warnings

class ProgressLogger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
    def write(self, row: Dict[str, Any]) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

def run_one(row: Dict[str, str], retry_idx: int, original_order: int, args: argparse.Namespace, out: Path, progress: ProgressLogger) -> Dict[str, Any]:
    evidence_id = first_existing(row, ["evidence_id", "rerun_evidence_id", "candidate_runtime_id", "candidate_id", "visual_unit_id"]) or f"qwenvl_retry_{retry_idx:06d}"
    visual_unit_id = first_existing(row, ["visual_unit_id", "candidate_runtime_id", "candidate_id"])
    image_path = first_existing(row, ["runtime_input_image_path", "image_path", "runtime_visual_file", "visual_file", "preview_path"])
    sid = f"retry{retry_idx:04d}_orig{original_order:06d}_{safe_id(evidence_id + image_path)}"

    raw_path = out / "outputs/raw_stdout" / f"{sid}.txt"
    err_path = out / "outputs/stderr" / f"{sid}.txt"
    clean_path = out / "outputs/clean_text" / f"{sid}.txt"
    metrics_path = out / "outputs/metrics" / f"{sid}.json"

    base: Dict[str, Any] = dict(row)
    base.update({
        "retry_contract_version": CONTRACT_VERSION,
        "retry_idx": retry_idx,
        "original_rerun_idx": row.get("rerun_idx") or original_order,
        "rerun_idx": row.get("rerun_idx") or original_order,
        "rerun_evidence_id": evidence_id,
        "visual_unit_id": visual_unit_id,
        "runtime_input_image_path": image_path,
        "qwen_model_path": args.model,
        "qwen_python": args.qwen_python,
        "qwen_prompt_version": "v2_short_complete_retry",
        "qwen_max_tokens": args.max_tokens,
        "created_at": now_ts(),
    })

    if not image_path or not Path(image_path).exists():
        base.update({"status": "failed", "qwen_text_cleanup_status": "failed", "qwen_text_cleanup_warnings": "missing_input_image", "returncode": -998, "elapsed_seconds": 0, "qwen_clean_text": "", "qwen_text": ""})
        progress.write({"retry_idx": retry_idx, "status": "failed", "reason": "missing_input_image", "image_path": image_path})
        return base

    cmd = [args.qwen_python, "-m", "mlx_vlm.generate", "--model", args.model, "--image", image_path, "--prompt", args.prompt, "--max-tokens", str(args.max_tokens)]
    env = os.environ.copy()
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    env.setdefault("HF_DATASETS_OFFLINE", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    t0 = time.perf_counter(); stdout = ""; stderr = ""; returncode = -999
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout, env=env)
        stdout = proc.stdout or ""; stderr = proc.stderr or ""; returncode = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""; stderr = (e.stderr or "") + "\nTIMEOUT_EXPIRED"; returncode = -997
    except Exception as e:
        stderr = repr(e); returncode = -999
    elapsed = time.perf_counter() - t0

    raw_path.write_text(stdout, encoding="utf-8", errors="replace")
    err_path.write_text(stderr, encoding="utf-8", errors="replace")
    metrics = extract_runtime_metrics(stdout + "\n" + stderr)
    metrics.update({"returncode": returncode, "elapsed_seconds": round(elapsed, 3), "command": cmd})
    clean = extract_clean_assistant_text(stdout)
    text_status, warnings = detect_text_issues(clean, stdout, metrics, args.max_tokens)
    status = "success" if returncode == 0 and clean.strip() else "failed"
    if status == "failed" and "nonzero_returncode_or_empty_stdout" not in warnings:
        warnings.append("nonzero_returncode_or_empty_stdout")
    if status == "failed" and text_status == "ok":
        text_status = "review"
    clean_path.write_text(clean, encoding="utf-8", errors="replace")
    write_json(metrics_path, metrics)

    base.update({
        "status": status,
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 3),
        "qwen_text": clean,
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
    progress.write({"retry_idx": retry_idx, "original_order": original_order, "status": status, "text_status": text_status, "warnings": warnings, "elapsed_seconds": round(elapsed, 3), "visual_unit_id": visual_unit_id})
    return base

def write_sqlite(db_path: Path, merged: List[Dict[str, Any]], retry_results: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    if db_path.exists(): db_path.unlink()
    conn = sqlite3.connect(str(db_path)); conn.execute("pragma journal_mode=wal")
    conn.execute("create table merged_qwenvl_clean (row_json text)")
    conn.execute("create table retry_results (row_json text)")
    conn.execute("create table run_summary (key text primary key, value text)")
    conn.executemany("insert into merged_qwenvl_clean values (?)", [(json.dumps(r, ensure_ascii=False),) for r in merged])
    conn.executemany("insert into retry_results values (?)", [(json.dumps(r, ensure_ascii=False),) for r in retry_results])
    for k, v in summary.items():
        conn.execute("insert or replace into run_summary values (?,?)", (k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)))
    conn.commit(); conn.close()

def summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    success = sum(1 for r in rows if r.get("status") == "success")
    failed = len(rows) - success
    ok = sum(1 for r in rows if r.get("qwen_text_cleanup_status") == "ok")
    review = sum(1 for r in rows if r.get("qwen_text_cleanup_status") == "review")
    cleanup_failed = sum(1 for r in rows if r.get("qwen_text_cleanup_status") == "failed")
    warn_counts: Dict[str, int] = {}
    for r in rows:
        for w in (r.get("qwen_text_cleanup_warnings") or "").split("|"):
            if w: warn_counts[w] = warn_counts.get(w, 0) + 1
    return {"count": len(rows), "success_count": success, "failed_count": failed, "cleanup_ok_count": ok, "cleanup_review_count": review, "cleanup_failed_count": cleanup_failed, "warning_counts": warn_counts}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--previous-manifest", required=True, help="Previous Stop03-5A3 qwenvl_clean_text_manifest.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--qwen-python", default="/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python")
    ap.add_argument("--model", default="/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=384)
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--include-review", action="store_true", help="Retry review rows too; default retries failed/empty only")
    ap.add_argument("--expect-count", type=int, default=268)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args()

    out = Path(args.out); ensure_dirs(out); t0 = time.perf_counter()
    problems = []
    if not Path(args.model).exists(): problems.append(f"missing_model_path:{args.model}")
    if not Path(args.qwen_python).exists(): problems.append(f"missing_qwen_python:{args.qwen_python}")
    rows = read_rows(Path(args.previous_manifest))
    if args.expect_count and len(rows) != args.expect_count:
        problems.append(f"input_count_mismatch:expected_{args.expect_count}_got_{len(rows)}")

    indexed = [(i + 1, r) for i, r in enumerate(rows)]
    retry_items = [(i, r) for i, r in indexed if is_bad_row(r, include_review=args.include_review)]
    write_csv(out / "manifests/qwenvl_retry_queue.csv", [r for _, r in retry_items])

    retry_results: List[Dict[str, Any]] = []
    if not problems and retry_items:
        progress = ProgressLogger(out / "checkpoints/run_progress.jsonl")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = [pool.submit(run_one, r, retry_idx, original_order, args, out, progress) for retry_idx, (original_order, r) in enumerate(retry_items, 1)]
            for fut in as_completed(futs):
                retry_results.append(fut.result())
        retry_results.sort(key=lambda r: int(r.get("original_rerun_idx") or r.get("rerun_idx") or 0))

    # Merge: replace retried rows only if retry improved or user requests review retries.
    retry_by_original = {int(r.get("original_rerun_idx") or r.get("rerun_idx") or 0): r for r in retry_results}
    merged: List[Dict[str, Any]] = []
    for original_order, old in indexed:
        new = retry_by_original.get(original_order)
        if new is None:
            merged.append(old)
            continue
        # Always use retry result for selected bad rows. If it still fails, summary remains FAIL.
        merged.append(new)

    fields = list(dict.fromkeys([k for r in (rows + retry_results) for k in r.keys()]))
    write_csv(out / "manifests/qwenvl_retry_results.csv", retry_results, fields)
    write_csv(out / "manifests/qwenvl_clean_text_manifest.csv", merged, fields)
    write_jsonl(out / "manifests/qwenvl_clean_text_manifest.jsonl", merged)

    before = summarize_rows(rows); retry_sum = summarize_rows(retry_results); after = summarize_rows(merged)
    validation = "PASS"
    if problems or after["failed_count"] or after["cleanup_failed_count"]:
        validation = "FAIL"
    elif after["cleanup_review_count"]:
        validation = "PASS_WITH_REVIEW"
    wall = time.perf_counter() - t0
    summary = {
        "validation_status": validation,
        "contract_version": CONTRACT_VERSION,
        "mode": "retry_failed_or_review_rows_only_and_merge",
        "source_safety": "read_existing_manifest_and_derived_images_only_no_original_media_write",
        "network": "disabled_by_offline_env_vars_no_download_logic",
        "model_download": "not_allowed_model_path_must_be_local",
        "previous_manifest": args.previous_manifest,
        "out": str(out),
        "input_rows": len(rows),
        "retry_queue_count": len(retry_items),
        "include_review": args.include_review,
        "workers": args.workers,
        "max_tokens": args.max_tokens,
        "before": before,
        "retry_results": retry_sum,
        "after_merge": after,
        "problems": problems,
        "wall_seconds": round(wall, 3),
        "clean_manifest_csv": str(out / "manifests/qwenvl_clean_text_manifest.csv"),
        "retry_results_csv": str(out / "manifests/qwenvl_retry_results.csv"),
        "sqlite": str(out / "database/qwenvl_retry_failed_rows.sqlite"),
        "decision": "Retry merge passed. Use this manifest as QWENVL_CLEAN for Stop03-5B." if validation == "PASS" else "Review remaining failed/review rows before final staging, or rerun this retry script with workers=1."
    }
    write_json(out / "reports/stop03_5a3_retry_failed_rows_summary.json", summary)
    md = [
        "# Stop03-5A3 Retry Failed Rows", "",
        f"- validation_status: `{validation}`",
        f"- contract_version: `{CONTRACT_VERSION}`",
        "- mode: `retry_failed_or_review_rows_only_and_merge`",
        "- source_safety: `read_existing_manifest_and_derived_images_only_no_original_media_write`",
        "- network: `disabled_by_offline_env_vars_no_download_logic`",
        "- model_download: `not_allowed_model_path_must_be_local`", "",
        "## Counts",
        f"- input_rows: `{len(rows)}`",
        f"- retry_queue_count: `{len(retry_items)}`",
        f"- include_review: `{args.include_review}`",
        f"- before: `{before}`",
        f"- retry_results: `{retry_sum}`",
        f"- after_merge: `{after}`", "",
        "## Settings", f"- workers: `{args.workers}`", f"- max_tokens: `{args.max_tokens}`", "",
        "## Decision", summary["decision"], "",
        "## Outputs", f"- clean_manifest_csv: `{summary['clean_manifest_csv']}`", f"- retry_results_csv: `{summary['retry_results_csv']}`", f"- sqlite: `{summary['sqlite']}`",
    ]
    (out / "reports/stop03_5a3_retry_failed_rows_summary.md").write_text("\n".join(md), encoding="utf-8")
    write_sqlite(out / "database/qwenvl_retry_failed_rows.sqlite", merged, retry_results, summary)

    print("== Stop03-5A3 retry failed rows finished ==")
    print(json.dumps({
        "validation_status": validation,
        "retry_queue_count": len(retry_items),
        "before": before,
        "retry_results": retry_sum,
        "after_merge": after,
        "clean_manifest_csv": summary["clean_manifest_csv"],
        "summary_md": str(out / "reports/stop03_5a3_retry_failed_rows_summary.md"),
    }, ensure_ascii=False, indent=2))
    return 0 if validation in ("PASS", "PASS_WITH_REVIEW") else 3

if __name__ == "__main__":
    raise SystemExit(main())
