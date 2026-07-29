#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-4A OCR runner with full monitoring, checkpoint/resume, strong provenance, and DB-ready staging.

Run this script with the OCR Python environment:
  /Users/yourname/Documents/AI-Local/envs/media-archive-v06-ocr/bin/python

Safety:
- Does not modify / move / delete / rename original media.
- Does not edit Stop03-2 queue.
- Writes only under --out.

Inputs:
- Stop03-2 OCR queue:
  <stop03-2-base>/manifests/ocr_trigger_candidate_queue.csv

Outputs:
- manifests/ocr_runtime_queue.csv/jsonl
- manifests/ocr_result_manifest.csv/jsonl
- manifests/ocr_result_provenance_manifest.csv/jsonl
- manifests/ocr_db_ready_evidence_manifest.csv/jsonl
- database/ocr_evidence.sqlite
- outputs/<candidate_runtime_id>.json / .txt
- checkpoints/run_progress.jsonl
- telemetry/ocr_resource_samples.csv
- reports/stop03_4a_ocr_summary.md/json

Features:
- Dynamic scheduling: whichever worker finishes grabs the next task.
- Persistent worker OCR engine: each worker loads PaddleOCR once.
- Per-candidate progress append.
- Resume support: --resume skips successful candidate_runtime_id.
- Atomic writes: writes .tmp then replace.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


OCR_ENGINE = None
OCR_INIT_INFO: Dict[str, Any] = {}


IMAGE_PATH_KEYS = [
    "runtime_visual_file", "visual_file", "visual_path", "image_path", "preview_path",
    "derived_preview_path", "preview_file", "visual_unit_file", "candidate_visual_file",
    "source_preview_path", "frame_path", "thumbnail_path", "input_image_path"
]

SOURCE_PATH_KEYS = [
    "original_source_path_at_processing_time", "parent_source_path_at_processing_time",
    "source_path_at_processing_time", "original_source_path", "source_path",
]

RELATIVE_PATH_KEYS = [
    "source_relative_path", "parent_source_relative_path", "original_source_relative_path",
]

TEXT_TRUE = {"1", "true", "yes", "y", "marked", "success"}


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(block_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stable_id(prefix: str, parts: List[str], n: int = 24) -> str:
    return prefix + hashlib.sha256("|".join(parts).encode("utf-8", errors="replace")).hexdigest()[:n]


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


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


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def first_existing(row: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return ""


def resolve_candidate_path(raw: str, roots: List[Path]) -> str:
    if not raw:
        return ""
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return str(p)
    if p.is_absolute():
        return str(p)
    for root in roots:
        cand = root / raw
        if cand.exists():
            return str(cand)
    return str(p)


def find_one(base: Path, patterns: List[str]) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(base.glob(pat))
        if hits:
            return hits[0]
    return None


def build_runtime_queue(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    run_root = Path(args.run_root)
    stop03_2_base = Path(args.stop03_2_base)
    source_root = Path(args.source_root)
    out_dir = Path(args.out)

    ocr_queue_csv = find_one(stop03_2_base, [
        "manifests/ocr_trigger_candidate_queue.csv",
        "**/ocr_trigger_candidate_queue.csv",
        "**/*ocr*queue*.csv",
    ])
    if not ocr_queue_csv:
        raise FileNotFoundError(f"Cannot find OCR queue CSV under {stop03_2_base}")

    rows = read_csv(ocr_queue_csv)
    roots = [run_root, stop03_2_base, source_root, Path(args.project_root)]

    runtime_rows: List[Dict[str, Any]] = []
    invalid_rows: List[Dict[str, Any]] = []

    for idx, r in enumerate(rows):
        image_raw = first_existing(r, IMAGE_PATH_KEYS)
        image_path = resolve_candidate_path(image_raw, roots)

        original_raw = first_existing(r, SOURCE_PATH_KEYS)
        original_path = resolve_candidate_path(original_raw, roots) if original_raw else ""
        if not original_path:
            rel = first_existing(r, RELATIVE_PATH_KEYS)
            if rel:
                p = source_root / rel
                if p.exists():
                    original_path = str(p)

        visual_unit_id = first_existing(r, ["visual_unit_id", "visual_id", "unit_id"])
        candidate_id = first_existing(r, ["candidate_id", "ocr_candidate_id"])
        source_relative_path = first_existing(r, RELATIVE_PATH_KEYS)
        time_position_ms = first_existing(r, ["time_position_ms", "frame_time_ms", "estimated_frame_time_ms"])
        reason_codes = first_existing(r, ["ocr_trigger_reason_codes", "reason_codes", "candidate_reason_codes"])
        visual_unit_type = first_existing(r, ["visual_unit_type", "candidate_type", "preview_role"])

        image_exists = bool(image_path and Path(image_path).exists())
        original_exists = bool(original_path and Path(original_path).exists())

        basis = [
            "stop03_4a_ocr_runtime_v1",
            visual_unit_id,
            candidate_id,
            source_relative_path,
            time_position_ms,
            image_path,
            reason_codes,
        ]
        candidate_runtime_id = stable_id("ocr_rt_", basis)

        rr = dict(r)
        rr.update({
            "runtime_index": idx,
            "candidate_runtime_id": candidate_runtime_id,
            "candidate_id": candidate_id,
            "visual_unit_id": visual_unit_id,
            "runtime_input_image_path": image_path,
            "runtime_input_image_exists": image_exists,
            "resolved_original_source_path": original_path,
            "resolved_original_source_exists": original_exists,
            "source_relative_path": source_relative_path,
            "time_position_ms": time_position_ms,
            "visual_unit_type": visual_unit_type,
            "runtime_reason_codes": reason_codes,
            "runtime_source": "stop03_2_ocr_queue",
        })

        if not image_exists:
            rr["invalid_reason"] = "runtime_input_image_missing"
            invalid_rows.append(rr)
        else:
            runtime_rows.append(rr)

    if args.limit and args.limit > 0:
        selected = runtime_rows[:args.limit]
    else:
        selected = runtime_rows

    manifests = out_dir / "manifests"
    write_csv(manifests / "ocr_runtime_queue.csv", selected)
    write_jsonl(manifests / "ocr_runtime_queue.jsonl", selected)
    write_csv(manifests / "ocr_invalid_runtime_candidates.csv", invalid_rows)
    write_jsonl(manifests / "ocr_invalid_runtime_candidates.jsonl", invalid_rows)

    meta = {
        "ocr_queue_csv": str(ocr_queue_csv),
        "source_queue_count": len(rows),
        "valid_runtime_queue_count": len(runtime_rows),
        "invalid_runtime_queue_count": len(invalid_rows),
        "selected_count": len(selected),
        "limit": args.limit,
    }
    return selected, meta


def load_completed_success(progress_path: Path) -> set:
    done = set()
    if not progress_path.exists():
        return done
    with progress_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("status", "")).lower() == "success":
                cid = r.get("candidate_runtime_id")
                if cid:
                    done.add(cid)
    return done


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    # numpy arrays and scalars
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    return repr(obj)


def init_ocr_worker(lang: str, use_angle_cls: bool, model_root: str, cpu_threads: int) -> None:
    global OCR_ENGINE, OCR_INIT_INFO

    os.environ.setdefault("OMP_NUM_THREADS", str(cpu_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(cpu_threads))

    init_attempts = []
    try:
        from paddleocr import PaddleOCR  # type: ignore

        configs = [
            # PaddleOCR v2 style
            {
                "lang": lang,
                "use_angle_cls": use_angle_cls,
                "show_log": False,
                "use_gpu": False,
            },
            # PaddleOCR v3 style candidates
            {
                "lang": lang,
                "use_textline_orientation": use_angle_cls,
            },
            {
                "lang": lang,
            },
            {},
        ]

        for cfg in configs:
            try:
                OCR_ENGINE = PaddleOCR(**cfg)
                OCR_INIT_INFO = {
                    "ok": True,
                    "config": cfg,
                    "pid": os.getpid(),
                    "model_root": model_root,
                }
                return
            except Exception as e:
                init_attempts.append({"config": cfg, "error": repr(e)})

        raise RuntimeError(f"PaddleOCR init failed: {init_attempts}")

    except Exception as e:
        OCR_ENGINE = None
        OCR_INIT_INFO = {
            "ok": False,
            "pid": os.getpid(),
            "error": repr(e),
            "attempts": init_attempts,
            "traceback": traceback.format_exc(),
        }
        raise


def extract_lines_from_result(obj: Any) -> List[Dict[str, Any]]:
    """
    Supports common PaddleOCR result formats:
    - v2: [[box, (text, score)], ...] or [ [[box, (text, score)], ...] ]
    - v3-ish dict/list formats with rec_texts/rec_scores/rec_polys.
    """
    lines: List[Dict[str, Any]] = []

    def walk(x: Any) -> None:
        if x is None:
            return

        if isinstance(x, dict):
            # PaddleOCR v3 often returns dict-like results.
            texts = x.get("rec_texts") or x.get("texts") or x.get("text")
            scores = x.get("rec_scores") or x.get("scores") or x.get("score")
            polys = x.get("rec_polys") or x.get("dt_polys") or x.get("boxes") or x.get("box")

            if isinstance(texts, list):
                for i, t in enumerate(texts):
                    if not isinstance(t, str):
                        continue
                    score = None
                    if isinstance(scores, list) and i < len(scores):
                        score = scores[i]
                    box = None
                    if isinstance(polys, list) and i < len(polys):
                        box = polys[i]
                    lines.append({"text": t, "confidence": score, "box": to_jsonable(box)})
                return
            if isinstance(texts, str):
                lines.append({"text": texts, "confidence": scores if isinstance(scores, (int, float)) else None, "box": to_jsonable(polys)})
                return

            for v in x.values():
                walk(v)
            return

        if isinstance(x, (list, tuple)):
            # v2 single line: [box, [text, conf]] or [box, (text, conf)]
            if len(x) >= 2 and isinstance(x[1], (list, tuple)) and len(x[1]) >= 2 and isinstance(x[1][0], str):
                lines.append({
                    "text": x[1][0],
                    "confidence": x[1][1],
                    "box": to_jsonable(x[0]),
                })
                return

            # v2 nested: [ [box, [text, conf]], ... ]
            for item in x:
                walk(item)
            return

    walk(obj)

    # Deduplicate empty and exact duplicate lines while preserving order.
    out = []
    seen = set()
    for l in lines:
        text = str(l.get("text", "")).strip()
        if not text:
            continue
        key = (text, json.dumps(l.get("box"), ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        l["text"] = text
        out.append(l)
    return out


def run_single_ocr(row: Dict[str, Any], out_dir_str: str) -> Dict[str, Any]:
    global OCR_ENGINE, OCR_INIT_INFO

    out_dir = Path(out_dir_str)
    outputs_dir = out_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    cid = row["candidate_runtime_id"]
    image_path = Path(row["runtime_input_image_path"])
    json_path = outputs_dir / f"{cid}.ocr.json"
    txt_path = outputs_dir / f"{cid}.ocr.txt"

    t0 = time.time()
    result: Dict[str, Any] = {
        "candidate_runtime_id": cid,
        "visual_unit_id": row.get("visual_unit_id", ""),
        "runtime_input_image_path": str(image_path),
        "status": "failed",
        "started_at": now_ts(),
        "worker_pid": os.getpid(),
        "ocr_init_info": OCR_INIT_INFO,
    }

    try:
        if OCR_ENGINE is None:
            raise RuntimeError("OCR_ENGINE is not initialized")

        image_sha = sha256_file(image_path)

        # Try old and new APIs.
        raw = None
        api_used = ""
        try:
            raw = OCR_ENGINE.ocr(str(image_path), cls=True)
            api_used = "ocr_cls_true"
        except TypeError:
            try:
                raw = OCR_ENGINE.ocr(str(image_path))
                api_used = "ocr"
            except Exception:
                if hasattr(OCR_ENGINE, "predict"):
                    raw = OCR_ENGINE.predict(str(image_path))
                    api_used = "predict"
                else:
                    raise
        except Exception:
            if hasattr(OCR_ENGINE, "predict"):
                raw = OCR_ENGINE.predict(str(image_path))
                api_used = "predict"
            else:
                raise

        raw_jsonable = to_jsonable(raw)
        lines = extract_lines_from_result(raw_jsonable)
        text = "\n".join([l["text"] for l in lines]).strip()
        text_sha = sha256_text(text)

        payload = {
            "candidate_runtime_id": cid,
            "visual_unit_id": row.get("visual_unit_id", ""),
            "source_relative_path": row.get("source_relative_path", ""),
            "resolved_original_source_path": row.get("resolved_original_source_path", ""),
            "time_position_ms": row.get("time_position_ms", ""),
            "runtime_input_image_path": str(image_path),
            "runtime_input_image_sha256": image_sha,
            "ocr_api_used": api_used,
            "ocr_line_count": len(lines),
            "ocr_text": text,
            "ocr_text_sha256": text_sha,
            "ocr_lines": lines,
            "raw_result": raw_jsonable,
        }

        atomic_write_json(json_path, payload)
        atomic_write_text(txt_path, text)

        elapsed = time.time() - t0
        result.update({
            "status": "success",
            "completed_at": now_ts(),
            "elapsed_seconds": round(elapsed, 3),
            "runtime_input_image_sha256": image_sha,
            "ocr_output_json_path": str(json_path),
            "ocr_output_text_path": str(txt_path),
            "ocr_text_sha256": text_sha,
            "ocr_text_length": len(text),
            "ocr_line_count": len(lines),
            "ocr_api_used": api_used,
            "error": "",
        })
        return result

    except Exception as e:
        elapsed = time.time() - t0
        err_payload = {
            "candidate_runtime_id": cid,
            "visual_unit_id": row.get("visual_unit_id", ""),
            "runtime_input_image_path": str(image_path),
            "status": "failed",
            "error": repr(e),
            "traceback": traceback.format_exc(),
        }
        try:
            atomic_write_json(json_path, err_payload)
        except Exception:
            pass
        result.update({
            "status": "failed",
            "completed_at": now_ts(),
            "elapsed_seconds": round(elapsed, 3),
            "ocr_output_json_path": str(json_path),
            "ocr_output_text_path": str(txt_path),
            "ocr_text_sha256": "",
            "ocr_text_length": 0,
            "ocr_line_count": 0,
            "ocr_api_used": "",
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return result


def run_cmd(cmd: List[str], timeout: int = 10) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": repr(e), "returncode": None}


def ps_snapshot() -> List[Dict[str, Any]]:
    res = run_cmd(["ps", "-axo", "pid=,ppid=,pcpu=,rss=,command="], timeout=10)
    rows = []
    text = res.get("stdout", "")
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(.*)$", line.rstrip())
        if not m:
            continue
        rows.append({
            "pid": int(m.group(1)),
            "ppid": int(m.group(2)),
            "pcpu": float(m.group(3)),
            "rss_kb": int(m.group(4)),
            "command": m.group(5),
        })
    return rows


def descendants(root_pid: int, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_ppid = defaultdict(list)
    for r in rows:
        by_ppid[r["ppid"]].append(r)
    out = []
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        for child in by_ppid.get(pid, []):
            cpid = child["pid"]
            if cpid in seen:
                continue
            seen.add(cpid)
            out.append(child)
            stack.append(cpid)
    return out


def swap_used_mb() -> Optional[float]:
    res = run_cmd(["sysctl", "vm.swapusage"], timeout=5)
    text = (res.get("stdout") or res.get("stderr") or "").strip()
    m = re.search(r"used\s*=\s*([\d.]+)M", text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


class ResourceMonitor:
    def __init__(self, csv_path: Path, interval: float):
        self.csv_path = csv_path
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.summary = {
            "sample_count": 0,
            "max_cpu_percent_sum": 0.0,
            "max_cpu_cores_estimated": 0.0,
            "max_rss_mb_sum": 0.0,
            "max_swap_used_mb": 0.0,
            "telemetry_csv": str(csv_path),
        }

    def start(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> Dict[str, Any]:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        return self.summary

    def _run(self) -> None:
        fields = [
            "sample_time", "elapsed_seconds", "root_pid", "process_count",
            "cpu_percent_sum", "rss_mb_sum", "swap_used_mb", "top_processes",
        ]
        root_pid = os.getpid()
        t0 = time.time()
        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            while not self.stop_event.is_set():
                rows = ps_snapshot()
                root_row = next((r for r in rows if r["pid"] == root_pid), None)
                tree = []
                if root_row:
                    tree.append(root_row)
                tree.extend(descendants(root_pid, rows))

                cpu = sum(r["pcpu"] for r in tree)
                rss = sum(r["rss_kb"] for r in tree) / 1024.0
                swp = swap_used_mb()

                self.summary["sample_count"] += 1
                self.summary["max_cpu_percent_sum"] = max(self.summary["max_cpu_percent_sum"], round(cpu, 3))
                self.summary["max_cpu_cores_estimated"] = max(self.summary["max_cpu_cores_estimated"], round(cpu / 100.0, 3))
                self.summary["max_rss_mb_sum"] = max(self.summary["max_rss_mb_sum"], round(rss, 3))
                if swp is not None:
                    self.summary["max_swap_used_mb"] = max(self.summary["max_swap_used_mb"], round(swp, 3))

                top = sorted(tree, key=lambda r: r["rss_kb"], reverse=True)[:8]
                w.writerow({
                    "sample_time": now_ts(),
                    "elapsed_seconds": round(time.time() - t0, 3),
                    "root_pid": root_pid,
                    "process_count": len(tree),
                    "cpu_percent_sum": round(cpu, 3),
                    "rss_mb_sum": round(rss, 3),
                    "swap_used_mb": "" if swp is None else round(swp, 3),
                    "top_processes": json.dumps([
                        {
                            "pid": r["pid"],
                            "pcpu": r["pcpu"],
                            "rss_mb": round(r["rss_kb"] / 1024.0, 3),
                            "command": r["command"][:200],
                        }
                        for r in top
                    ], ensure_ascii=False),
                })
                f.flush()
                time.sleep(self.interval)


def build_provenance_and_db(args: argparse.Namespace, results: List[Dict[str, Any]], runtime_rows_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out_dir = Path(args.out)
    manifests = out_dir / "manifests"
    db_dir = out_dir / "database"
    db_dir.mkdir(parents=True, exist_ok=True)

    provenance_rows: List[Dict[str, Any]] = []
    db_rows: List[Dict[str, Any]] = []
    missing = Counter()

    for res in results:
        cid = res.get("candidate_runtime_id", "")
        base = dict(runtime_rows_by_id.get(cid, {}))
        row = {}
        row.update(base)
        row.update(res)

        input_sha = res.get("runtime_input_image_sha256", "")
        text_sha = res.get("ocr_text_sha256", "")
        provenance_id = stable_id("ocr_prov_", [
            cid,
            row.get("visual_unit_id", ""),
            input_sha,
            text_sha,
            row.get("ocr_output_json_path", ""),
        ])

        row.update({
            "provenance_id": provenance_id,
            "resolved_original_source_exists": bool(row.get("resolved_original_source_path") and Path(str(row.get("resolved_original_source_path"))).exists()),
            "runtime_input_image_exists": bool(row.get("runtime_input_image_path") and Path(str(row.get("runtime_input_image_path"))).exists()),
            "ocr_output_json_exists": bool(row.get("ocr_output_json_path") and Path(str(row.get("ocr_output_json_path"))).exists()),
            "ocr_output_text_exists": bool(row.get("ocr_output_text_path") and Path(str(row.get("ocr_output_text_path"))).exists()),
        })

        if not row.get("runtime_input_image_sha256"):
            missing["runtime_input_image_sha256_missing"] += 1
        if row.get("status") == "success" and not row.get("ocr_output_json_exists"):
            missing["ocr_output_json_missing"] += 1
        if row.get("status") == "success" and not row.get("ocr_output_text_exists"):
            missing["ocr_output_text_missing"] += 1
        if not row.get("resolved_original_source_path"):
            missing["resolved_original_source_path_missing"] += 1

        provenance_rows.append(row)

        # Read OCR text from text file if present.
        ocr_text = ""
        txt_path = row.get("ocr_output_text_path", "")
        if txt_path and Path(txt_path).exists():
            try:
                ocr_text = Path(txt_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                ocr_text = ""

        evidence_id = stable_id("ocr_ev_", [
            provenance_id,
            cid,
            row.get("visual_unit_id", ""),
            input_sha,
            text_sha,
        ])

        db_rows.append({
            "evidence_id": evidence_id,
            "evidence_type": "ocr_text",
            "database_contract_version": "stop03_4a_ocr_evidence_v1",
            "provenance_id": provenance_id,
            "candidate_runtime_id": cid,
            "candidate_id": row.get("candidate_id", ""),
            "visual_unit_id": row.get("visual_unit_id", ""),
            "runtime_source": row.get("runtime_source", "stop03_2_ocr_queue"),
            "runtime_reason_codes": row.get("runtime_reason_codes", ""),
            "visual_unit_type": row.get("visual_unit_type", ""),
            "time_position_ms": row.get("time_position_ms", ""),
            "source_relative_path": row.get("source_relative_path", ""),
            "resolved_original_source_path": row.get("resolved_original_source_path", ""),
            "resolved_original_source_exists": str(row.get("resolved_original_source_exists", "")),
            "original_source_content_id": row.get("original_source_content_id", "") or row.get("parent_source_content_id", ""),
            "runtime_input_image_path": row.get("runtime_input_image_path", ""),
            "runtime_input_image_sha256": input_sha,
            "ocr_output_json_path": row.get("ocr_output_json_path", ""),
            "ocr_output_text_path": row.get("ocr_output_text_path", ""),
            "ocr_text_sha256": text_sha,
            "ocr_text": ocr_text,
            "ocr_text_preview": ocr_text[:500],
            "ocr_line_count": row.get("ocr_line_count", ""),
            "ocr_text_length": row.get("ocr_text_length", ""),
            "ocr_api_used": row.get("ocr_api_used", ""),
            "status": row.get("status", ""),
            "elapsed_seconds": row.get("elapsed_seconds", ""),
            "error": row.get("error", ""),
            "created_at": now_ts(),
        })

    write_csv(manifests / "ocr_result_provenance_manifest.csv", provenance_rows)
    write_jsonl(manifests / "ocr_result_provenance_manifest.jsonl", provenance_rows)
    write_csv(manifests / "ocr_db_ready_evidence_manifest.csv", db_rows)
    write_jsonl(manifests / "ocr_db_ready_evidence_manifest.jsonl", db_rows)

    sqlite_path = db_dir / "ocr_evidence.sqlite"
    fields = [
        "evidence_id", "evidence_type", "database_contract_version", "provenance_id",
        "candidate_runtime_id", "candidate_id", "visual_unit_id", "runtime_source",
        "runtime_reason_codes", "visual_unit_type", "time_position_ms",
        "source_relative_path", "resolved_original_source_path", "resolved_original_source_exists",
        "original_source_content_id", "runtime_input_image_path", "runtime_input_image_sha256",
        "ocr_output_json_path", "ocr_output_text_path", "ocr_text_sha256", "ocr_text",
        "ocr_text_preview", "ocr_line_count", "ocr_text_length", "ocr_api_used",
        "status", "elapsed_seconds", "error", "created_at",
    ]

    con = sqlite3.connect(str(sqlite_path))
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ocr_evidence (
                evidence_id TEXT PRIMARY KEY,
                evidence_type TEXT,
                database_contract_version TEXT,
                provenance_id TEXT,
                candidate_runtime_id TEXT,
                candidate_id TEXT,
                visual_unit_id TEXT,
                runtime_source TEXT,
                runtime_reason_codes TEXT,
                visual_unit_type TEXT,
                time_position_ms TEXT,
                source_relative_path TEXT,
                resolved_original_source_path TEXT,
                resolved_original_source_exists TEXT,
                original_source_content_id TEXT,
                runtime_input_image_path TEXT,
                runtime_input_image_sha256 TEXT,
                ocr_output_json_path TEXT,
                ocr_output_text_path TEXT,
                ocr_text_sha256 TEXT,
                ocr_text TEXT,
                ocr_text_preview TEXT,
                ocr_line_count TEXT,
                ocr_text_length TEXT,
                ocr_api_used TEXT,
                status TEXT,
                elapsed_seconds TEXT,
                error TEXT,
                created_at TEXT
            )
        """)
        con.execute("DELETE FROM ocr_evidence")
        placeholders = ",".join(["?"] * len(fields))
        con.executemany(
            f"INSERT OR REPLACE INTO ocr_evidence ({','.join(fields)}) VALUES ({placeholders})",
            [[str(r.get(f, "")) for f in fields] for r in db_rows],
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_visual_unit_id ON ocr_evidence(visual_unit_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_source_relative_path ON ocr_evidence(source_relative_path)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_original_source_content_id ON ocr_evidence(original_source_content_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_runtime_source ON ocr_evidence(runtime_source)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_status ON ocr_evidence(status)")
        con.commit()
    finally:
        con.close()

    return {
        "provenance_row_count": len(provenance_rows),
        "db_ready_row_count": len(db_rows),
        "missing_counts": dict(missing),
        "provenance_csv": str(manifests / "ocr_result_provenance_manifest.csv"),
        "provenance_jsonl": str(manifests / "ocr_result_provenance_manifest.jsonl"),
        "db_ready_csv": str(manifests / "ocr_db_ready_evidence_manifest.csv"),
        "db_ready_jsonl": str(manifests / "ocr_db_ready_evidence_manifest.jsonl"),
        "db_ready_sqlite": str(sqlite_path),
        "status_counts": dict(Counter(r["status"] for r in db_rows)),
        "visual_unit_type_counts": dict(Counter(r["visual_unit_type"] for r in db_rows)),
        "runtime_source_counts": dict(Counter(r["runtime_source"] for r in db_rows)),
        "nonempty_ocr_text_count": sum(1 for r in db_rows if r.get("ocr_text", "").strip()),
        "empty_ocr_text_count": sum(1 for r in db_rows if not r.get("ocr_text", "").strip()),
    }


def write_summary(args: argparse.Namespace, summary: Dict[str, Any]) -> None:
    out_dir = Path(args.out)
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    json_path = reports / "stop03_4a_ocr_summary.json"
    md_path = reports / "stop03_4a_ocr_summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Stop03-4A OCR Summary",
        "",
        f"- status: {summary.get('status')}",
        f"- source_safety: {summary.get('source_safety')}",
        f"- out: {args.out}",
        "",
        "## Queue",
        f"- source_queue_count: {summary.get('queue_meta', {}).get('source_queue_count')}",
        f"- valid_runtime_queue_count: {summary.get('queue_meta', {}).get('valid_runtime_queue_count')}",
        f"- invalid_runtime_queue_count: {summary.get('queue_meta', {}).get('invalid_runtime_queue_count')}",
        f"- selected_count: {summary.get('queue_meta', {}).get('selected_count')}",
        f"- skipped_success_count: {summary.get('skipped_success_count')}",
        f"- pending_count: {summary.get('pending_count')}",
        "",
        "## OCR run",
        f"- workers_requested: {summary.get('workers_requested')}",
        f"- success_count: {summary.get('success_count')}",
        f"- failed_count: {summary.get('failed_count')}",
        f"- wall_seconds: {summary.get('wall_seconds')}",
        f"- sum_task_seconds: {summary.get('sum_task_seconds')}",
        f"- avg_task_seconds_measured_inside_worker: {summary.get('avg_task_seconds_measured_inside_worker')}",
        f"- effective_wall_seconds_per_completed_image: {summary.get('effective_wall_seconds_per_completed_image')}",
        f"- parallel_adjusted_single_lane_estimate_seconds: {summary.get('parallel_adjusted_single_lane_estimate_seconds')}",
        f"- nonempty_ocr_text_count: {summary.get('provenance_db', {}).get('nonempty_ocr_text_count')}",
        f"- empty_ocr_text_count: {summary.get('provenance_db', {}).get('empty_ocr_text_count')}",
        "",
        "## Resource monitor",
        f"- sample_count: {summary.get('resource_monitor', {}).get('sample_count')}",
        f"- max_cpu_percent_sum: {summary.get('resource_monitor', {}).get('max_cpu_percent_sum')}",
        f"- max_cpu_cores_estimated: {summary.get('resource_monitor', {}).get('max_cpu_cores_estimated')}",
        f"- max_rss_mb_sum: {summary.get('resource_monitor', {}).get('max_rss_mb_sum')}",
        f"- max_swap_used_mb: {summary.get('resource_monitor', {}).get('max_swap_used_mb')}",
        f"- telemetry_csv: {summary.get('resource_monitor', {}).get('telemetry_csv')}",
        "",
        "## Provenance / DB-ready",
        f"- provenance_row_count: {summary.get('provenance_db', {}).get('provenance_row_count')}",
        f"- db_ready_row_count: {summary.get('provenance_db', {}).get('db_ready_row_count')}",
        f"- missing_counts: {summary.get('provenance_db', {}).get('missing_counts')}",
        f"- status_counts: {summary.get('provenance_db', {}).get('status_counts')}",
        f"- visual_unit_type_counts: {summary.get('provenance_db', {}).get('visual_unit_type_counts')}",
        f"- provenance_csv: {summary.get('provenance_db', {}).get('provenance_csv')}",
        f"- db_ready_csv: {summary.get('provenance_db', {}).get('db_ready_csv')}",
        f"- db_ready_sqlite: {summary.get('provenance_db', {}).get('db_ready_sqlite')}",
        "",
        "PASS condition:",
        "- All selected runtime candidates completed successfully.",
        "- Provenance row count equals selected_count.",
        "- DB-ready row count equals selected_count.",
        "- Missing input image sha256 and output files count is 0.",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    summary["summary_json"] = str(json_path)
    summary["summary_md"] = str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/Users/yourname/Documents/AI-Local/media-archive-clean")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--stop03-2-base", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=12, help="0 means full")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--use-angle-cls", action="store_true")
    ap.add_argument("--model-root", default="/Users/yourname/Documents/model/ocr")
    ap.add_argument("--cpu-threads-per-worker", type=int, default=1)
    ap.add_argument("--monitor-interval", type=float, default=2.0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "checkpoints/run_progress.jsonl"

    t0 = time.time()
    monitor = ResourceMonitor(out_dir / "telemetry/ocr_resource_samples.csv", args.monitor_interval)
    monitor.start()

    summary: Dict[str, Any] = {
        "status": "UNKNOWN",
        "created_at": now_ts(),
        "source_safety": "read_only_no_original_media_modification",
        "project_root": args.project_root,
        "run_root": args.run_root,
        "stop03_2_base": args.stop03_2_base,
        "source_root": args.source_root,
        "out": args.out,
        "workers_requested": args.workers,
        "limit": args.limit,
        "resume": args.resume,
        "ocr_runtime": {
            "python": sys.executable,
            "model_root": args.model_root,
            "lang": args.lang,
            "use_angle_cls": args.use_angle_cls,
            "cpu_threads_per_worker": args.cpu_threads_per_worker,
        },
    }

    try:
        runtime_rows, queue_meta = build_runtime_queue(args)
        summary["queue_meta"] = queue_meta

        completed = load_completed_success(progress_path) if args.resume else set()
        pending = [r for r in runtime_rows if r["candidate_runtime_id"] not in completed]
        summary["skipped_success_count"] = len(runtime_rows) - len(pending)
        summary["pending_count"] = len(pending)

        print(f"[OCR] selected={len(runtime_rows)} pending={len(pending)} skipped={summary['skipped_success_count']} workers={args.workers}", flush=True)

        results: List[Dict[str, Any]] = []
        result_manifest_path = out_dir / "manifests/ocr_result_manifest.csv"
        result_jsonl_path = out_dir / "manifests/ocr_result_manifest.jsonl"

        if pending:
            with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=init_ocr_worker,
                initargs=(args.lang, args.use_angle_cls, args.model_root, args.cpu_threads_per_worker),
            ) as ex:
                futures = {
                    ex.submit(run_single_ocr, row, args.out): row
                    for row in pending
                }

                total = len(pending)
                completed_count = 0
                success_count_live = 0
                failed_count_live = 0
                task_seconds_live: List[float] = []

                for fut in as_completed(futures):
                    row = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {
                            "candidate_runtime_id": row.get("candidate_runtime_id", ""),
                            "visual_unit_id": row.get("visual_unit_id", ""),
                            "runtime_input_image_path": row.get("runtime_input_image_path", ""),
                            "status": "failed",
                            "elapsed_seconds": 0,
                            "error": repr(e),
                            "traceback": traceback.format_exc(),
                        }

                    results.append(res)
                    append_jsonl(progress_path, res)

                    completed_count += 1
                    if res.get("status") == "success":
                        success_count_live += 1
                    else:
                        failed_count_live += 1
                    try:
                        task_seconds_live.append(float(res.get("elapsed_seconds", 0)))
                    except Exception:
                        pass

                    avg = sum(task_seconds_live) / len(task_seconds_live) if task_seconds_live else 0
                    remaining = total - completed_count
                    eta_wall = (time.time() - t0) / max(completed_count, 1) * remaining
                    print(
                        f"[OCR progress] completed={completed_count}/{total} "
                        f"success={success_count_live} failed={failed_count_live} "
                        f"last={res.get('elapsed_seconds')}s avg_task={avg:.3f}s "
                        f"eta_wall≈{eta_wall:.1f}s cid={res.get('candidate_runtime_id')}",
                        flush=True,
                    )

        # Include skipped successful outputs from previous progress in resume mode only as progress rows are already present.
        # For current run summary and DB artifacts, use this run's selected set. If resume skipped rows exist,
        # we read progress rows and include latest success/failure for every selected candidate.
        all_progress_latest: Dict[str, Dict[str, Any]] = {}
        if progress_path.exists():
            with progress_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    cid = r.get("candidate_runtime_id")
                    if cid:
                        all_progress_latest[cid] = r

        selected_ids = {r["candidate_runtime_id"] for r in runtime_rows}
        final_results = [all_progress_latest[cid] for cid in selected_ids if cid in all_progress_latest]
        # If not resume and a result somehow didn't reach progress, include direct results.
        existing = {r.get("candidate_runtime_id") for r in final_results}
        for r in results:
            if r.get("candidate_runtime_id") not in existing:
                final_results.append(r)

        write_csv(result_manifest_path, final_results)
        write_jsonl(result_jsonl_path, final_results)

        runtime_by_id = {r["candidate_runtime_id"]: r for r in runtime_rows}
        provenance_db = build_provenance_and_db(args, final_results, runtime_by_id)
        summary["provenance_db"] = provenance_db

        resource_summary = monitor.stop()
        wall = time.time() - t0

        success_count = sum(1 for r in final_results if r.get("status") == "success")
        failed_count = sum(1 for r in final_results if r.get("status") != "success")
        task_seconds = []
        for r in final_results:
            try:
                task_seconds.append(float(r.get("elapsed_seconds", 0)))
            except Exception:
                pass

        selected_count = len(runtime_rows)
        sum_task = sum(task_seconds)
        avg_task = sum_task / len(task_seconds) if task_seconds else None
        effective = wall / selected_count if selected_count else None
        single_lane = wall * args.workers / selected_count if selected_count else None

        summary.update({
            "success_count": success_count,
            "failed_count": failed_count,
            "wall_seconds": round(wall, 3),
            "sum_task_seconds": round(sum_task, 3),
            "avg_task_seconds_measured_inside_worker": None if avg_task is None else round(avg_task, 3),
            "effective_wall_seconds_per_completed_image": None if effective is None else round(effective, 3),
            "parallel_adjusted_single_lane_estimate_seconds": None if single_lane is None else round(single_lane, 3),
            "result_manifest_csv": str(result_manifest_path),
            "result_manifest_jsonl": str(result_jsonl_path),
            "resource_monitor": resource_summary,
        })

        pass_condition = (
            failed_count == 0
            and provenance_db.get("provenance_row_count") == selected_count
            and provenance_db.get("db_ready_row_count") == selected_count
            and not provenance_db.get("missing_counts")
        )
        summary["status"] = "PASS" if pass_condition else "PASS_WITH_REVIEW"

        write_summary(args, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    except Exception as e:
        try:
            resource_summary = monitor.stop()
        except Exception:
            resource_summary = {}
        summary["status"] = "FAIL_EXCEPTION"
        summary["exception"] = repr(e)
        summary["traceback"] = traceback.format_exc()
        summary["resource_monitor"] = resource_summary
        write_summary(args, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
