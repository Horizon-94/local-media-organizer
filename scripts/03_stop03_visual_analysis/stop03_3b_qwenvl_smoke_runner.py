#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-3B Qwen-VL smoke runner from Stop03-2 queue + macOS Finder-tag images.

Purpose
- Build a runtime Qwen-VL input queue without modifying Stop03-2 outputs.
- Union existing Stop03-2 Qwen-VL candidates with images carrying macOS Finder tags.
- Deduplicate by visual_unit_id / visual_file / source path.
- Run a small Qwen-VL smoke batch in a NEW output folder.
- Record per-item timing, wall time, CPU/RSS/swap telemetry, command stdout/stderr.

Safety
- Original media is read-only.
- This script does not move/delete/rename/modify source files.
- It writes only under --out.
- It uses derived preview visual_file when available for Finder-tag images.

Default model/runtime
- qwen python: /Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python
- qwen model:  /Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng", ".heic", ".heif", ".hif", ".arw", ".cr2", ".cr3", ".nef", ".rw2", ".raf", ".orf"}

DEFAULT_PROMPT = (
    "请用中文分析这张图片或视频关键帧。要求："
    "1）先用一句话概括画面；"
    "2）列出人物、物体、场景、动作、环境、文字区域；"
    "3）判断它为什么可能是素材检索中的高价值画面；"
    "4）不要编造看不见的信息。"
)


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def sha256_text(s: str, n: int = 24) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:n]


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
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


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def first_existing(row: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return ""


def normalize_path_for_match(p: str) -> str:
    return str(Path(p).expanduser())


def rel_to_source(path: str, source_root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(source_root.resolve()))
    except Exception:
        return path


def parse_mdls_tags(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw or raw == "(null)":
        return []
    tags = re.findall(r'"([^"]+)"', raw)
    return tags


def scan_finder_tags(source_root: Path) -> List[Dict[str, Any]]:
    rows = []
    files = [p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    for p in files:
        try:
            proc = subprocess.run(["mdls", "-raw", "-name", "kMDItemUserTags", str(p)], capture_output=True, text=True, timeout=5)
            tags = parse_mdls_tags(proc.stdout)
        except Exception:
            tags = []
        if tags:
            rows.append({
                "source_path": str(p),
                "source_relative_path": rel_to_source(str(p), source_root),
                "source_ext": p.suffix.lower(),
                "finder_tags": "|".join(tags),
                "finder_tag_count": len(tags),
            })
    return rows


def load_stop02_image_visual_manifest(run_root: Path) -> List[Dict[str, Any]]:
    p = run_root / "02_2_stop02_image_preview/manifests/image_preview_visual_unit_manifest.jsonl"
    return read_jsonl(p)


def build_image_preview_index(visual_rows: List[Dict[str, Any]], source_root: Path) -> Dict[str, Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {}
    for r in visual_rows:
        keys = []
        for k in ["source_relative_path", "parent_source_path_at_processing_time", "source_path_at_processing_time"]:
            v = r.get(k)
            if v:
                keys.append(str(v))
                if str(v).startswith(str(source_root)):
                    keys.append(rel_to_source(str(v), source_root))
        for key in keys:
            if key:
                idx[key] = r
                idx[key.lower()] = r
    return idx


def candidate_from_existing_qwen(row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row)
    r["runtime_source"] = "stop03_2_qwenvl_queue"
    r["runtime_reason_codes"] = first_existing(r, ["reason_codes", "selection_reason", "candidate_reason"])
    r["finder_tags"] = r.get("finder_tags", "")
    r["candidate_runtime_id"] = "qr_" + sha256_text("stop03_3b|" + first_existing(r, ["visual_unit_id", "visual_file", "source_relative_path"]))
    return r


def candidate_from_finder_tag(tag_row: Dict[str, Any], vu: Dict[str, Any], source_root: Path) -> Dict[str, Any]:
    source_rel = tag_row.get("source_relative_path") or vu.get("source_relative_path") or ""
    visual_unit_id = str(vu.get("visual_unit_id") or "finder_" + sha256_text(source_rel))
    visual_file = str(vu.get("visual_file") or "")
    return {
        "candidate_runtime_id": "qr_" + sha256_text("stop03_3b_finder_tag|" + visual_unit_id + "|" + source_rel),
        "candidate_id": "finder_tag_" + sha256_text(source_rel),
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": str(vu.get("visual_unit_type") or "image_preview"),
        "visual_file": visual_file,
        "visual_file_sha256": str(vu.get("visual_file_sha256") or ""),
        "source_relative_path": source_rel,
        "parent_source_path_at_processing_time": str(vu.get("parent_source_path_at_processing_time") or tag_row.get("source_path") or ""),
        "parent_source_content_id": str(vu.get("parent_source_content_id") or ""),
        "parent_source_file_id": str(vu.get("parent_source_file_id") or ""),
        "preview_role": str(vu.get("preview_role") or "finder_tag_image_preview"),
        "time_position_ms": "",
        "sequence_id": str(vu.get("sequence_id") or ""),
        "representative_position": str(vu.get("representative_position") or ""),
        "runtime_source": "macos_finder_tag",
        "runtime_reason_codes": "macos_finder_tag_high_value_image",
        "reason_codes": "macos_finder_tag_high_value_image",
        "finder_tags": tag_row.get("finder_tags", ""),
        "candidate_score": "",
    }


def build_runtime_queue(stop03_2_base: Path, run_root: Path, source_root: Path, out_dir: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    qwen_csv = stop03_2_base / "manifests/qwenvl_high_value_candidate_queue.csv"
    existing = [candidate_from_existing_qwen(r) for r in read_csv(qwen_csv)]

    finder_tag_rows = scan_finder_tags(source_root)
    visual_rows = load_stop02_image_visual_manifest(run_root)
    preview_index = build_image_preview_index(visual_rows, source_root)

    finder_candidates = []
    finder_unmatched = []
    for tr in finder_tag_rows:
        key1 = tr.get("source_relative_path", "")
        key2 = tr.get("source_path", "")
        vu = preview_index.get(key1) or preview_index.get(key1.lower()) or preview_index.get(key2) or preview_index.get(key2.lower())
        if not vu:
            finder_unmatched.append(tr)
            continue
        cand = candidate_from_finder_tag(tr, vu, source_root)
        if not cand.get("visual_file") or not Path(cand["visual_file"]).exists():
            cand["unmatched_reason"] = "preview_visual_file_missing"
            finder_unmatched.append({**tr, **cand})
            continue
        finder_candidates.append(cand)

    # Union/dedupe. Existing queue wins; finder tag enriches if duplicate.
    union: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}

    def dedupe_key(r: Dict[str, Any]) -> str:
        return first_existing(r, ["visual_unit_id", "visual_file", "source_relative_path", "parent_source_path_at_processing_time"])

    for r in existing + finder_candidates:
        key = dedupe_key(r)
        if not key:
            key = r.get("candidate_runtime_id") or sha256_text(json.dumps(r, ensure_ascii=False, sort_keys=True))
        if key in by_key:
            old = by_key[key]
            old["runtime_source"] = "|".join(sorted(set(filter(None, (old.get("runtime_source", "") + "|" + r.get("runtime_source", "")).split("|")))))
            old["runtime_reason_codes"] = "|".join(sorted(set(filter(None, (old.get("runtime_reason_codes", "") + "|" + r.get("runtime_reason_codes", "")).split("|")))))
            if r.get("finder_tags") and not old.get("finder_tags"):
                old["finder_tags"] = r.get("finder_tags")
        else:
            by_key[key] = dict(r)
            union.append(by_key[key])

    # Keep only rows with existing visual_file.
    invalid = []
    valid = []
    for r in union:
        vf = first_existing(r, ["visual_file", "image_path", "preview_path"])
        r["runtime_visual_file"] = vf
        if vf and Path(vf).exists():
            valid.append(r)
        else:
            r["invalid_reason"] = "visual_file_missing"
            invalid.append(r)

    manifest_dir = out_dir / "manifests"
    report_dir = out_dir / "reports"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    fields = sorted(set(k for r in valid + invalid for k in r.keys()))
    write_csv(manifest_dir / "qwenvl_runtime_union_candidate_queue.csv", valid, fields)
    write_jsonl(manifest_dir / "qwenvl_runtime_union_candidate_queue.jsonl", valid)
    write_csv(report_dir / "finder_tag_image_candidates.csv", finder_candidates, sorted(set(k for r in finder_candidates for k in r.keys())) or ["empty"])
    write_csv(report_dir / "finder_tag_image_unmatched.csv", finder_unmatched, sorted(set(k for r in finder_unmatched for k in r.keys())) or ["empty"])
    write_csv(report_dir / "invalid_runtime_qwenvl_candidates.csv", invalid, fields or ["empty"])

    type_counts = Counter(first_existing(r, ["visual_unit_type", "candidate_type", "preview_role"]) for r in valid)
    source_counts = Counter(r.get("runtime_source", "") for r in valid)
    summary = {
        "stop03_2_qwenvl_existing_rows": len(existing),
        "finder_tag_source_rows": len(finder_tag_rows),
        "finder_tag_matched_preview_rows": len(finder_candidates),
        "finder_tag_unmatched_rows": len(finder_unmatched),
        "runtime_union_valid_rows": len(valid),
        "runtime_union_invalid_rows": len(invalid),
        "runtime_union_type_counts": dict(type_counts),
        "runtime_union_source_counts": dict(source_counts),
        "queue_csv": str(manifest_dir / "qwenvl_runtime_union_candidate_queue.csv"),
        "queue_jsonl": str(manifest_dir / "qwenvl_runtime_union_candidate_queue.jsonl"),
    }
    (report_dir / "qwenvl_runtime_union_queue_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return valid, summary


class ResourceMonitor:
    def __init__(self, out_csv: Path, interval: float = 1.0):
        self.out_csv = out_csv
        self.interval = interval
        self.active: Dict[int, str] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.samples: List[Dict[str, Any]] = []

    def add(self, pid: int, label: str) -> None:
        with self.lock:
            self.active[pid] = label

    def remove(self, pid: int) -> None:
        with self.lock:
            self.active.pop(pid, None)

    def start(self) -> None:
        self.out_csv.parent.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        fields = ["ts", "active_pids", "process_count", "cpu_percent_sum", "rss_mb_sum", "swap_used_mb"]
        write_csv(self.out_csv, self.samples, fields)

    def _swap_mb(self) -> float:
        try:
            proc = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=2)
            text = proc.stdout + proc.stderr
            m = re.search(r"used = ([0-9.]+)([MG])", text)
            if not m:
                return 0.0
            val = float(m.group(1))
            return val * 1024 if m.group(2) == "G" else val
        except Exception:
            return 0.0

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                pids = list(self.active.keys())
            cpu_sum = 0.0
            rss_kb = 0
            if pids:
                try:
                    proc = subprocess.run(["ps", "-o", "pid=,pcpu=,rss=", "-p", ",".join(map(str, pids))], capture_output=True, text=True, timeout=2)
                    for line in proc.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 3:
                            cpu_sum += float(parts[1])
                            rss_kb += int(float(parts[2]))
                except Exception:
                    pass
            self.samples.append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "active_pids": "|".join(map(str, pids)),
                "process_count": len(pids),
                "cpu_percent_sum": round(cpu_sum, 3),
                "rss_mb_sum": round(rss_kb / 1024, 3),
                "swap_used_mb": round(self._swap_mb(), 3),
            })
            time.sleep(self.interval)


def select_smoke_rows(queue: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if limit <= 0 or len(queue) <= limit:
        return queue
    buckets = {
        "video": [],
        "finder_image": [],
        "timelapse": [],
        "other_image": [],
        "other": [],
    }
    for r in queue:
        typ = first_existing(r, ["visual_unit_type", "candidate_type", "preview_role"]).lower()
        reason = (r.get("runtime_reason_codes") or r.get("reason_codes") or "").lower()
        src = (r.get("runtime_source") or "").lower()
        if "video" in typ:
            buckets["video"].append(r)
        elif "macos_finder_tag" in reason or "macos_finder_tag" in src:
            buckets["finder_image"].append(r)
        elif r.get("sequence_id") or "timelapse" in reason or "timelapse" in typ:
            buckets["timelapse"].append(r)
        elif "image" in typ:
            buckets["other_image"].append(r)
        else:
            buckets["other"].append(r)

    selected = []
    target_order = ["video", "finder_image", "timelapse", "other_image", "other"]
    # Round-robin for diversity.
    while len(selected) < limit and any(buckets[k] for k in target_order):
        for k in target_order:
            if len(selected) >= limit:
                break
            if buckets[k]:
                selected.append(buckets[k].pop(0))
    return selected


def run_qwen_one(row: Dict[str, Any], idx: int, qwen_python: str, model_path: str, prompt: str, max_tokens: int, timeout: int, monitor: ResourceMonitor, out_text_dir: Path) -> Dict[str, Any]:
    image_path = first_existing(row, ["runtime_visual_file", "visual_file", "image_path", "preview_path"])
    item_id = first_existing(row, ["candidate_runtime_id", "candidate_id", "visual_unit_id"]) or f"item_{idx}"
    out_txt = out_text_dir / f"{idx:04d}_{sha256_text(item_id)}.txt"
    cmd = [qwen_python, "-m", "mlx_vlm.generate", "--model", model_path, "--image", image_path, "--prompt", prompt, "--max-tokens", str(max_tokens)]
    t0 = time.perf_counter()
    proc = None
    stdout = ""
    stderr = ""
    returncode = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        monitor.add(proc.pid, item_id)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=10)
            stderr = (stderr or "") + "\nTIMEOUT_EXPIRED"
        returncode = proc.returncode
    except Exception as e:
        stderr = repr(e)
        returncode = -999
    finally:
        if proc and proc.pid:
            monitor.remove(proc.pid)
    elapsed = time.perf_counter() - t0
    out_txt.write_text(stdout or "", encoding="utf-8", errors="replace")
    return {
        "idx": idx,
        "candidate_runtime_id": item_id,
        "visual_unit_id": row.get("visual_unit_id", ""),
        "source_relative_path": row.get("source_relative_path", ""),
        "visual_unit_type": first_existing(row, ["visual_unit_type", "candidate_type", "preview_role"]),
        "runtime_source": row.get("runtime_source", ""),
        "runtime_reason_codes": row.get("runtime_reason_codes", row.get("reason_codes", "")),
        "finder_tags": row.get("finder_tags", ""),
        "image_path": image_path,
        "returncode": returncode,
        "status": "success" if returncode == 0 and bool((stdout or "").strip()) else "failed",
        "elapsed_seconds": round(elapsed, 3),
        "stdout_chars": len(stdout or ""),
        "stderr_chars": len(stderr or ""),
        "stdout_path": str(out_txt),
        "stderr_tail": (stderr or "")[-2000:].replace("\n", "\\n"),
    }


def run_smoke(queue: List[Dict[str, Any]], out_dir: Path, qwen_python: str, model_path: str, prompt: str, limit: int, workers: int, max_tokens: int, timeout: int) -> Dict[str, Any]:
    run_rows = select_smoke_rows(queue, limit)
    run_manifest = out_dir / "manifests/qwenvl_smoke_selected_queue.csv"
    write_csv(run_manifest, run_rows, sorted(set(k for r in run_rows for k in r.keys())) or ["empty"])

    telemetry_dir = out_dir / "telemetry"
    result_dir = out_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    monitor = ResourceMonitor(telemetry_dir / "qwen_vl_resource_samples.csv", interval=1.0)
    monitor.start()

    t0 = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = []
        for i, r in enumerate(run_rows, 1):
            futs.append(pool.submit(run_qwen_one, r, i, qwen_python, model_path, prompt, max_tokens, timeout, monitor, result_dir))
        for fut in as_completed(futs):
            results.append(fut.result())
    wall = time.perf_counter() - t0
    monitor.stop()

    results.sort(key=lambda r: int(r.get("idx") or 0))
    write_csv(out_dir / "manifests/qwenvl_smoke_result_manifest.csv", results, sorted(set(k for r in results for k in r.keys())) or ["empty"])
    write_jsonl(out_dir / "manifests/qwenvl_smoke_result_manifest.jsonl", results)

    success = sum(1 for r in results if r.get("status") == "success")
    failed = len(results) - success
    sum_task = sum(float(r.get("elapsed_seconds") or 0) for r in results)
    summary = {
        "status": "PASS" if success == len(results) and len(results) > 0 else "FAIL",
        "mode": "qwen_vl_smoke",
        "source_safety": "read_only_no_original_media_modification",
        "qwen_python": qwen_python,
        "model_path": model_path,
        "workers_requested": workers,
        "rows_selected": len(run_rows),
        "success_count": success,
        "failed_count": failed,
        "wall_seconds": round(wall, 3),
        "sum_task_seconds": round(sum_task, 3),
        "avg_task_seconds_measured_inside_worker": round(sum_task / len(results), 3) if results else None,
        "effective_wall_seconds_per_completed_image": round(wall / len(results), 3) if results else None,
        "parallel_adjusted_single_lane_estimate_seconds": round(wall * max(1, workers) / len(results), 3) if results else None,
        "user_requested_wall_div_count_div_workers_seconds": round(wall / len(results) / max(1, workers), 3) if results else None,
        "note_on_timing": "wall/count is effective throughput per image under concurrency; wall*workers/count is the rough single-lane estimate. wall/count/workers is recorded only because requested, not used as the primary performance metric.",
        "result_manifest_csv": str(out_dir / "manifests/qwenvl_smoke_result_manifest.csv"),
        "selected_queue_csv": str(run_manifest),
        "resource_samples_csv": str(telemetry_dir / "qwen_vl_resource_samples.csv"),
    }
    (out_dir / "reports/qwenvl_smoke_summary.json").parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "reports/qwenvl_smoke_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Stop03-3B Qwen-VL Smoke Summary",
        "",
        f"- status: {summary['status']}",
        f"- rows_selected: {summary['rows_selected']}",
        f"- success_count: {summary['success_count']}",
        f"- failed_count: {summary['failed_count']}",
        f"- workers_requested: {summary['workers_requested']}",
        f"- wall_seconds: {summary['wall_seconds']}",
        f"- sum_task_seconds: {summary['sum_task_seconds']}",
        f"- avg_task_seconds_measured_inside_worker: {summary['avg_task_seconds_measured_inside_worker']}",
        f"- effective_wall_seconds_per_completed_image: {summary['effective_wall_seconds_per_completed_image']}",
        f"- parallel_adjusted_single_lane_estimate_seconds: {summary['parallel_adjusted_single_lane_estimate_seconds']}",
        f"- user_requested_wall_div_count_div_workers_seconds: {summary['user_requested_wall_div_count_div_workers_seconds']}",
        "",
        summary["note_on_timing"],
    ]
    (out_dir / "reports/qwenvl_smoke_summary.md").write_text("\n".join(md), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--stop03-2-base", required=True)
    ap.add_argument("--source-root", default="/Users/yourname/Documents/001DZLtest")
    ap.add_argument("--out", required=True)
    ap.add_argument("--qwen-python", default="/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python")
    ap.add_argument("--model", default="/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=12, help="Smoke batch size. Use 0 for full runtime queue.")
    ap.add_argument("--max-tokens", type=int, default=180)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    run_root = Path(args.run_root)
    stop03_2_base = Path(args.stop03_2_base)
    source_root = Path(args.source_root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifests").mkdir(exist_ok=True)
    (out_dir / "reports").mkdir(exist_ok=True)

    if not Path(args.qwen_python).exists():
        raise SystemExit(f"qwen python not found: {args.qwen_python}")
    if not Path(args.model).exists():
        raise SystemExit(f"qwen model not found: {args.model}")

    queue, queue_summary = build_runtime_queue(stop03_2_base, run_root, source_root, out_dir)
    summary = {
        "status": "QUEUE_BUILT",
        "source_safety": "read_only_no_original_media_modification",
        "out_dir": str(out_dir),
        "queue_summary": queue_summary,
    }
    if not args.build_only:
        smoke_summary = run_smoke(queue, out_dir, args.qwen_python, args.model, args.prompt, args.limit, args.workers, args.max_tokens, args.timeout)
        summary["status"] = smoke_summary["status"]
        summary["smoke_summary"] = smoke_summary

    (out_dir / "reports/stop03_3b_run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
