#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-4D OCR local-only no-download smoke.

Goal:
- Prove OCR can run using ONLY local model files under --ocr-model-root.
- Block network during OCR init/inference.
- Detect any mutation under ~/.paddlex/official_models.
- Fail if PaddleOCR logs show automatic download or implicit official model usage.
- Run only a small smoke by default; do NOT run full OCR here.

Safety:
- Does NOT modify original media.
- Does NOT modify Stop03-2 queue.
- Does NOT run full OCR by default.
- Writes only under --out.
- Network is monkey-patched blocked before PaddleOCR init.

Recommended run:
  /Users/yourname/Documents/AI-Local/envs/media-archive-v06-ocr/bin/python \
    scripts/03_stop03_visual_analysis/stop03_4d_ocr_local_only_smoke.py \
    --run-root "$RUN_ROOT" \
    --stop03-2-base "$STOP03_2_BASE" \
    --source-root /Users/yourname/Documents/001DZLtest \
    --ocr-model-root /Users/yourname/Documents/model/ocr \
    --paddlex-cache-root /Users/yourname/.paddlex/official_models \
    --out "$OUT" \
    --limit 3
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import inspect
import io
import json
import os
import re
import socket
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.2f} {u}"
        x /= 1024
    return f"{n} B"


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


def ensure_out(out: Path) -> Tuple[Path, Path, Path, Path]:
    reports = out / "reports"
    manifests = out / "manifests"
    outputs = out / "outputs"
    database = out / "database"
    for p in [reports, manifests, outputs, database]:
        p.mkdir(parents=True, exist_ok=True)
    return reports, manifests, outputs, database


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted(set(k for r in rows for k in r.keys())) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


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


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def latest_glob(base: Path, pattern: str) -> Optional[Path]:
    hits = [p for p in base.glob(pattern) if p.exists()]
    if not hits:
        return None
    return sorted(hits, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def resolve_candidate_path(raw: str, roots: List[Path]) -> str:
    if not raw:
        return ""
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    for root in roots:
        cand = root / raw
        if cand.exists():
            return str(cand)
    return str(p)


def snapshot_tree(path: Path) -> Dict[str, Dict[str, Any]]:
    snap: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return snap
    if path.is_file():
        st = path.stat()
        snap[str(path)] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
        return snap
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.stat()
            except Exception:
                continue
            snap[str(fp)] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    return snap


def compare_snapshots(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    bkeys = set(before.keys())
    akeys = set(after.keys())
    added = sorted(akeys - bkeys)
    removed = sorted(bkeys - akeys)
    modified = []
    for k in sorted(bkeys & akeys):
        if before[k] != after[k]:
            modified.append(k)
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "modified_count": len(modified),
        "added": added[:200],
        "removed": removed[:200],
        "modified": modified[:200],
    }


def scan_dir_size(path: Path) -> Dict[str, Any]:
    total = 0
    files = 0
    dirs = 0
    if not path.exists():
        return {"exists": False, "bytes": 0, "human": human_bytes(0), "files": 0, "dirs": 0}
    if path.is_file():
        st = path.stat()
        return {"exists": True, "bytes": st.st_size, "human": human_bytes(st.st_size), "files": 1, "dirs": 0}
    for _root, dnames, fnames in os.walk(path):
        dirs += len(dnames)
        for name in fnames:
            fp = Path(_root) / name
            try:
                total += fp.stat().st_size
                files += 1
            except Exception:
                pass
    return {"exists": True, "bytes": total, "human": human_bytes(total), "files": files, "dirs": dirs}


def classify_model_dir(path: Path) -> Dict[str, Any]:
    try:
        files = [p for p in path.iterdir() if p.is_file()]
    except Exception:
        files = []

    names = [path.name.lower()] + [p.name.lower() for p in files]
    text = " ".join(names)

    roles = []
    if any(k in text for k in ["det", "detection"]):
        roles.append("det")
    if any(k in text for k in ["rec", "recognition"]):
        roles.append("rec")
    if any(k in text for k in ["cls", "angle", "orientation", "textline"]):
        roles.append("cls_or_orientation")
    if any(k in text for k in ["doc", "uvdoc", "unwarp"]):
        roles.append("doc_preprocess")

    fnames = {p.name.lower() for p in files}
    has_pdmodel = "inference.pdmodel" in fnames or any(n.endswith(".pdmodel") for n in fnames)
    has_pdiparams = "inference.pdiparams" in fnames or any(n.endswith(".pdiparams") for n in fnames)
    has_yml = "inference.yml" in fnames or any(n.endswith(".yml") or n.endswith(".yaml") for n in fnames)
    has_onnx = any(n.endswith(".onnx") for n in fnames)
    has_openvino = any(n.endswith(".xml") for n in fnames) and any(n.endswith(".bin") for n in fnames)

    size = scan_dir_size(path)
    return {
        "path": str(path),
        "name": path.name,
        "roles_guess": roles,
        "roles_guess_text": ",".join(roles),
        "has_paddle_inference_files": bool(has_pdmodel and has_pdiparams),
        "has_inference_yml_or_yaml": bool(has_yml),
        "has_onnx": bool(has_onnx),
        "has_openvino_xml_bin": bool(has_openvino),
        "total_bytes": size["bytes"],
        "total_human": size["human"],
        "file_count": size["files"],
        "top_level_files": sorted(list(fnames))[:100],
    }


def find_candidate_model_dirs(root: Path) -> List[Dict[str, Any]]:
    candidates = []
    if not root.exists():
        return candidates
    for p in root.rglob("*"):
        if not p.is_dir():
            continue
        try:
            files = [x for x in p.iterdir() if x.is_file()]
        except Exception:
            continue
        names = [x.name.lower() for x in files]
        if (
            "inference.pdmodel" in names
            or "inference.pdiparams" in names
            or "inference.yml" in names
            or any(n.endswith(".pdiparams.info") for n in names)
            or any(x.suffix.lower() in {".onnx", ".xml", ".bin"} for x in files)
        ):
            candidates.append(classify_model_dir(p))
    candidates.sort(key=lambda c: (
        0 if c["has_paddle_inference_files"] else 1,
        -int(c["total_bytes"]),
        c["path"],
    ))
    return candidates


def pick_role(candidates: List[Dict[str, Any]], role: str) -> Optional[str]:
    matched = [c for c in candidates if role in c.get("roles_guess", [])]
    if not matched:
        return None
    # Prefer Paddle inference files for PaddleOCR.
    matched.sort(key=lambda c: (
        0 if c["has_paddle_inference_files"] else 1,
        0 if c["has_inference_yml_or_yaml"] else 1,
        -int(c["total_bytes"]),
        c["path"],
    ))
    return matched[0]["path"]


def build_runtime_queue(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    run_root = Path(args.run_root)
    stop03_2_base = Path(args.stop03_2_base)
    source_root = Path(args.source_root)

    qpath = latest_glob(stop03_2_base, "manifests/ocr_trigger_candidate_queue.csv")
    if not qpath:
        qpath = latest_glob(stop03_2_base, "**/ocr_trigger_candidate_queue.csv")
    if not qpath:
        raise FileNotFoundError(f"Cannot find ocr_trigger_candidate_queue.csv under {stop03_2_base}")

    rows = read_csv(qpath)
    roots = [run_root, stop03_2_base, source_root, Path(args.project_root)]
    out_rows = []
    invalid = []

    for idx, r in enumerate(rows):
        image_raw = first_existing(r, IMAGE_PATH_KEYS)
        image_path = resolve_candidate_path(image_raw, roots)

        original_raw = first_existing(r, SOURCE_PATH_KEYS)
        original_path = resolve_candidate_path(original_raw, roots) if original_raw else ""
        if not original_path:
            rel = first_existing(r, RELATIVE_PATH_KEYS)
            if rel:
                cand = source_root / rel
                if cand.exists():
                    original_path = str(cand)

        visual_unit_id = first_existing(r, ["visual_unit_id", "visual_id", "unit_id"])
        candidate_id = first_existing(r, ["candidate_id", "ocr_candidate_id"])
        source_relative_path = first_existing(r, RELATIVE_PATH_KEYS)
        time_position_ms = first_existing(r, ["time_position_ms", "frame_time_ms", "estimated_frame_time_ms"])
        visual_unit_type = first_existing(r, ["visual_unit_type", "candidate_type", "preview_role"])
        reason_codes = first_existing(r, ["ocr_trigger_reason_codes", "reason_codes", "candidate_reason_codes"])

        cid = stable_id("ocr_local_rt_", [
            "stop03_4d_local_only_v1",
            visual_unit_id,
            candidate_id,
            source_relative_path,
            time_position_ms,
            image_path,
            reason_codes,
        ])

        rr = dict(r)
        rr.update({
            "runtime_index": idx,
            "candidate_runtime_id": cid,
            "candidate_id": candidate_id,
            "visual_unit_id": visual_unit_id,
            "runtime_input_image_path": image_path,
            "runtime_input_image_exists": Path(image_path).exists(),
            "resolved_original_source_path": original_path,
            "resolved_original_source_exists": bool(original_path and Path(original_path).exists()),
            "source_relative_path": source_relative_path,
            "time_position_ms": time_position_ms,
            "visual_unit_type": visual_unit_type,
            "runtime_reason_codes": reason_codes,
            "runtime_source": "stop03_2_ocr_queue_local_only_smoke",
        })
        if not rr["runtime_input_image_exists"]:
            rr["invalid_reason"] = "runtime_input_image_missing"
            invalid.append(rr)
        else:
            out_rows.append(rr)

    if args.limit > 0:
        selected = out_rows[:args.limit]
    else:
        selected = out_rows

    return selected, {
        "ocr_queue_csv": str(qpath),
        "source_queue_count": len(rows),
        "valid_runtime_queue_count": len(out_rows),
        "invalid_runtime_queue_count": len(invalid),
        "selected_count": len(selected),
        "invalid_rows": invalid[:50],
    }


def install_network_block() -> Dict[str, Any]:
    original_socket_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_socket_connect(self, address):
        raise RuntimeError(f"NETWORK_BLOCKED_BY_STOP03_4D_OCR_LOCAL_ONLY address={address!r}")

    def blocked_create_connection(*args, **kwargs):
        raise RuntimeError(f"NETWORK_BLOCKED_BY_STOP03_4D_OCR_LOCAL_ONLY args={args!r} kwargs={kwargs!r}")

    socket.socket.connect = blocked_socket_connect
    socket.create_connection = blocked_create_connection

    return {
        "installed": True,
        "blocked": ["socket.socket.connect", "socket.create_connection"],
        "note": "Network is blocked inside this Python process during OCR init/inference.",
    }


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    return repr(obj)


def extract_lines_from_result(obj: Any) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []

    def walk(x: Any) -> None:
        if x is None:
            return

        if isinstance(x, dict):
            texts = x.get("rec_texts") or x.get("texts") or x.get("text")
            scores = x.get("rec_scores") or x.get("scores") or x.get("score")
            polys = x.get("rec_polys") or x.get("dt_polys") or x.get("boxes") or x.get("box")

            if isinstance(texts, list):
                for i, t in enumerate(texts):
                    if not isinstance(t, str):
                        continue
                    score = scores[i] if isinstance(scores, list) and i < len(scores) else None
                    box = polys[i] if isinstance(polys, list) and i < len(polys) else None
                    lines.append({"text": t, "confidence": score, "box": to_jsonable(box)})
                return
            if isinstance(texts, str):
                lines.append({"text": texts, "confidence": scores if isinstance(scores, (int, float)) else None, "box": to_jsonable(polys)})
                return

            for v in x.values():
                walk(v)
            return

        if isinstance(x, (list, tuple)):
            if len(x) >= 2 and isinstance(x[1], (list, tuple)) and len(x[1]) >= 2 and isinstance(x[1][0], str):
                lines.append({"text": x[1][0], "confidence": x[1][1], "box": to_jsonable(x[0])})
                return
            for item in x:
                walk(item)

    walk(obj)

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


def suspicious_log_hit(log_text: str) -> List[str]:
    patterns = [
        "automatically downloaded",
        "will be automatically downloaded",
        "download",
        "Using official model",
        "official_models",
        ".paddlex/official_models",
        "aistudio.baidu.com",
        "paddle-model-ecology",
    ]
    hits = []
    low = log_text.lower()
    for p in patterns:
        if p.lower() in low:
            hits.append(p)
    return hits


def build_paddleocr_configs(signature: str, candidates: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    det = pick_role(candidates, "det")
    rec = pick_role(candidates, "rec")
    cls = pick_role(candidates, "cls_or_orientation")
    doc = pick_role(candidates, "doc_preprocess")

    if not det or not rec:
        return []

    supports_kwargs = "**" in signature or "kwargs" in signature
    def supported(name: str) -> bool:
        return supports_kwargs or name in signature

    configs = []

    # Newer PaddleOCR/PaddleX-style parameter names.
    cfg_v3: Dict[str, Any] = {}
    if supported("lang"):
        cfg_v3["lang"] = args.lang
    if supported("text_detection_model_dir"):
        cfg_v3["text_detection_model_dir"] = det
    if supported("text_recognition_model_dir"):
        cfg_v3["text_recognition_model_dir"] = rec
    if cls and supported("textline_orientation_model_dir"):
        cfg_v3["textline_orientation_model_dir"] = cls
    if supported("use_textline_orientation"):
        cfg_v3["use_textline_orientation"] = bool(cls and args.use_orientation)
    if supported("use_doc_orientation_classify"):
        cfg_v3["use_doc_orientation_classify"] = False
    if supported("use_doc_unwarping"):
        cfg_v3["use_doc_unwarping"] = False
    # Do not bind doc model unless explicitly requested; it was one source of prior downloads.
    if args.allow_doc_preprocess and doc:
        if supported("doc_orientation_classify_model_dir"):
            cfg_v3["doc_orientation_classify_model_dir"] = doc
        if supported("doc_unwarping_model_dir"):
            cfg_v3["doc_unwarping_model_dir"] = doc
    if supported("show_log"):
        cfg_v3["show_log"] = True
    if supported("use_gpu"):
        cfg_v3["use_gpu"] = False
    if "text_detection_model_dir" in cfg_v3 and "text_recognition_model_dir" in cfg_v3:
        configs.append({"name": "paddleocr_v3_local_dirs", "params": cfg_v3})

    # Older PaddleOCR v2-style parameter names.
    cfg_v2: Dict[str, Any] = {}
    if supported("lang"):
        cfg_v2["lang"] = args.lang
    if supported("det_model_dir"):
        cfg_v2["det_model_dir"] = det
    if supported("rec_model_dir"):
        cfg_v2["rec_model_dir"] = rec
    if cls and supported("cls_model_dir"):
        cfg_v2["cls_model_dir"] = cls
    if supported("use_angle_cls"):
        cfg_v2["use_angle_cls"] = bool(cls and args.use_orientation)
    if supported("show_log"):
        cfg_v2["show_log"] = True
    if supported("use_gpu"):
        cfg_v2["use_gpu"] = False
    if "det_model_dir" in cfg_v2 and "rec_model_dir" in cfg_v2:
        configs.append({"name": "paddleocr_v2_local_dirs", "params": cfg_v2})

    return configs


def init_and_run_ocr_local_only(args: argparse.Namespace, candidates: List[Dict[str, Any]], queue: List[Dict[str, Any]], outputs_dir: Path) -> Dict[str, Any]:
    network_block = install_network_block()

    import paddleocr  # type: ignore
    from paddleocr import PaddleOCR  # type: ignore

    signature = str(inspect.signature(PaddleOCR))
    configs = build_paddleocr_configs(signature, candidates, args)
    if not configs:
        return {
            "ok": False,
            "error": "No bindable local det/rec model config could be built from model root and PaddleOCR signature.",
            "paddleocr_signature": signature,
            "configs_tried": [],
            "network_block": network_block,
        }

    attempts = []
    chosen = None
    ocr = None

    for cfg in configs:
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                ocr = PaddleOCR(**cfg["params"])
            log_text = buf_out.getvalue() + "\n" + buf_err.getvalue()
            hits = suspicious_log_hit(log_text)
            attempt = {
                "config_name": cfg["name"],
                "params": cfg["params"],
                "ok": True,
                "elapsed_seconds": round(time.time() - t0, 3),
                "suspicious_log_hits": hits,
                "captured_log_excerpt": log_text[:4000],
            }
            attempts.append(attempt)
            if hits:
                # This config is not acceptable; try next if available.
                ocr = None
                continue
            chosen = cfg
            break
        except Exception as e:
            log_text = buf_out.getvalue() + "\n" + buf_err.getvalue()
            attempts.append({
                "config_name": cfg["name"],
                "params": cfg["params"],
                "ok": False,
                "elapsed_seconds": round(time.time() - t0, 3),
                "error": repr(e),
                "traceback": traceback.format_exc(),
                "suspicious_log_hits": suspicious_log_hit(log_text),
                "captured_log_excerpt": log_text[:4000],
            })
            ocr = None

    if ocr is None or chosen is None:
        return {
            "ok": False,
            "error": "PaddleOCR local-only initialization failed or showed suspicious official/download logs.",
            "paddleocr_signature": signature,
            "configs_tried": attempts,
            "network_block": network_block,
        }

    results = []
    for row in queue:
        cid = row["candidate_runtime_id"]
        image_path = Path(row["runtime_input_image_path"])
        t0 = time.time()
        out_json = outputs_dir / f"{cid}.ocr.local_only.json"
        out_txt = outputs_dir / f"{cid}.ocr.local_only.txt"

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        try:
            image_sha = sha256_file(image_path)
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                raw = None
                api_used = ""
                try:
                    raw = ocr.ocr(str(image_path), cls=bool(chosen["params"].get("use_angle_cls", False)))
                    api_used = "ocr_cls"
                except TypeError:
                    try:
                        raw = ocr.ocr(str(image_path))
                        api_used = "ocr"
                    except Exception:
                        if hasattr(ocr, "predict"):
                            raw = ocr.predict(str(image_path))
                            api_used = "predict"
                        else:
                            raise
                except Exception:
                    if hasattr(ocr, "predict"):
                        raw = ocr.predict(str(image_path))
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
                "runtime_input_image_path": str(image_path),
                "runtime_input_image_sha256": image_sha,
                "source_relative_path": row.get("source_relative_path", ""),
                "resolved_original_source_path": row.get("resolved_original_source_path", ""),
                "time_position_ms": row.get("time_position_ms", ""),
                "ocr_api_used": api_used,
                "ocr_text": text,
                "ocr_text_sha256": text_sha,
                "ocr_line_count": len(lines),
                "ocr_lines": lines,
                "raw_result": raw_jsonable,
            }
            write_json(out_json, payload)
            out_txt.write_text(text, encoding="utf-8")

            infer_log = buf_out.getvalue() + "\n" + buf_err.getvalue()
            hits = suspicious_log_hit(infer_log)
            status = "success" if not hits else "failed_suspicious_log"
            results.append({
                **row,
                "status": status,
                "elapsed_seconds": round(time.time() - t0, 3),
                "runtime_input_image_sha256": image_sha,
                "ocr_output_json_path": str(out_json),
                "ocr_output_text_path": str(out_txt),
                "ocr_text_sha256": text_sha,
                "ocr_text_length": len(text),
                "ocr_line_count": len(lines),
                "ocr_api_used": api_used,
                "suspicious_log_hits": ",".join(hits),
                "captured_infer_log_excerpt": infer_log[:2000],
                "error": "",
            })
        except Exception as e:
            infer_log = buf_out.getvalue() + "\n" + buf_err.getvalue()
            results.append({
                **row,
                "status": "failed",
                "elapsed_seconds": round(time.time() - t0, 3),
                "runtime_input_image_sha256": "",
                "ocr_output_json_path": str(out_json),
                "ocr_output_text_path": str(out_txt),
                "ocr_text_sha256": "",
                "ocr_text_length": 0,
                "ocr_line_count": 0,
                "ocr_api_used": "",
                "suspicious_log_hits": ",".join(suspicious_log_hit(infer_log)),
                "captured_infer_log_excerpt": infer_log[:2000],
                "error": repr(e),
                "traceback": traceback.format_exc(),
            })

    return {
        "ok": True,
        "paddleocr_signature": signature,
        "chosen_config": chosen,
        "configs_tried": attempts,
        "network_block": network_block,
        "results": results,
    }


def build_db_ready(args: argparse.Namespace, results: List[Dict[str, Any]], database_dir: Path, manifests_dir: Path) -> Dict[str, Any]:
    db_rows = []
    prov_rows = []

    for r in results:
        cid = r.get("candidate_runtime_id", "")
        provenance_id = stable_id("ocr_local_prov_", [
            cid,
            r.get("visual_unit_id", ""),
            r.get("runtime_input_image_sha256", ""),
            r.get("ocr_text_sha256", ""),
            args.ocr_model_root,
        ])
        evidence_id = stable_id("ocr_local_ev_", [
            provenance_id,
            cid,
            r.get("visual_unit_id", ""),
            r.get("ocr_text_sha256", ""),
        ])
        text = ""
        txt_path = r.get("ocr_output_text_path", "")
        if txt_path and Path(txt_path).exists():
            try:
                text = Path(txt_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = ""

        pr = dict(r)
        pr.update({
            "provenance_id": provenance_id,
            "ocr_model_root": args.ocr_model_root,
            "ocr_python": sys.executable,
            "local_only_contract": "no_download_no_official_cache_mutation",
        })
        prov_rows.append(pr)

        db_rows.append({
            "evidence_id": evidence_id,
            "evidence_type": "ocr_text",
            "database_contract_version": "stop03_4d_ocr_local_only_smoke_v1",
            "provenance_id": provenance_id,
            "candidate_runtime_id": cid,
            "visual_unit_id": r.get("visual_unit_id", ""),
            "runtime_source": r.get("runtime_source", ""),
            "runtime_reason_codes": r.get("runtime_reason_codes", ""),
            "visual_unit_type": r.get("visual_unit_type", ""),
            "time_position_ms": r.get("time_position_ms", ""),
            "source_relative_path": r.get("source_relative_path", ""),
            "resolved_original_source_path": r.get("resolved_original_source_path", ""),
            "runtime_input_image_path": r.get("runtime_input_image_path", ""),
            "runtime_input_image_sha256": r.get("runtime_input_image_sha256", ""),
            "ocr_output_json_path": r.get("ocr_output_json_path", ""),
            "ocr_output_text_path": r.get("ocr_output_text_path", ""),
            "ocr_text_sha256": r.get("ocr_text_sha256", ""),
            "ocr_text": text,
            "ocr_text_preview": text[:500],
            "ocr_model_root": args.ocr_model_root,
            "ocr_python": sys.executable,
            "status": r.get("status", ""),
            "elapsed_seconds": r.get("elapsed_seconds", ""),
            "created_at": now_ts(),
        })

    write_csv(manifests_dir / "ocr_local_only_result_provenance_manifest.csv", prov_rows)
    write_jsonl(manifests_dir / "ocr_local_only_result_provenance_manifest.jsonl", prov_rows)
    write_csv(manifests_dir / "ocr_local_only_db_ready_evidence_manifest.csv", db_rows)
    write_jsonl(manifests_dir / "ocr_local_only_db_ready_evidence_manifest.jsonl", db_rows)

    sqlite_path = database_dir / "ocr_local_only_evidence.sqlite"
    fields = [
        "evidence_id", "evidence_type", "database_contract_version", "provenance_id",
        "candidate_runtime_id", "visual_unit_id", "runtime_source", "runtime_reason_codes",
        "visual_unit_type", "time_position_ms", "source_relative_path", "resolved_original_source_path",
        "runtime_input_image_path", "runtime_input_image_sha256", "ocr_output_json_path",
        "ocr_output_text_path", "ocr_text_sha256", "ocr_text", "ocr_text_preview",
        "ocr_model_root", "ocr_python", "status", "elapsed_seconds", "created_at",
    ]

    con = sqlite3.connect(str(sqlite_path))
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ocr_local_only_evidence (
                evidence_id TEXT PRIMARY KEY,
                evidence_type TEXT,
                database_contract_version TEXT,
                provenance_id TEXT,
                candidate_runtime_id TEXT,
                visual_unit_id TEXT,
                runtime_source TEXT,
                runtime_reason_codes TEXT,
                visual_unit_type TEXT,
                time_position_ms TEXT,
                source_relative_path TEXT,
                resolved_original_source_path TEXT,
                runtime_input_image_path TEXT,
                runtime_input_image_sha256 TEXT,
                ocr_output_json_path TEXT,
                ocr_output_text_path TEXT,
                ocr_text_sha256 TEXT,
                ocr_text TEXT,
                ocr_text_preview TEXT,
                ocr_model_root TEXT,
                ocr_python TEXT,
                status TEXT,
                elapsed_seconds TEXT,
                created_at TEXT
            )
        """)
        con.execute("DELETE FROM ocr_local_only_evidence")
        placeholders = ",".join(["?"] * len(fields))
        con.executemany(
            f"INSERT OR REPLACE INTO ocr_local_only_evidence ({','.join(fields)}) VALUES ({placeholders})",
            [[str(r.get(f, "")) for f in fields] for r in db_rows],
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_local_visual_unit_id ON ocr_local_only_evidence(visual_unit_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_local_source_relative_path ON ocr_local_only_evidence(source_relative_path)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ocr_local_status ON ocr_local_only_evidence(status)")
        con.commit()
    finally:
        con.close()

    return {
        "provenance_row_count": len(prov_rows),
        "db_ready_row_count": len(db_rows),
        "db_ready_sqlite": str(sqlite_path),
        "provenance_csv": str(manifests_dir / "ocr_local_only_result_provenance_manifest.csv"),
        "db_ready_csv": str(manifests_dir / "ocr_local_only_db_ready_evidence_manifest.csv"),
    }


def write_summary_md(path: Path, summary: Dict[str, Any]) -> None:
    cfg = summary.get("ocr_run", {}).get("chosen_config", {})
    queue_meta = summary.get("queue_meta", {})
    cache_diff = summary.get("paddlex_cache_diff", {})
    results = summary.get("result_counts", {})
    db = summary.get("db_ready", {})
    md = [
        "# Stop03-4D OCR Local-Only No-Download Smoke",
        "",
        f"- status: {summary.get('status')}",
        f"- source_safety: {summary.get('source_safety')}",
        f"- out: `{summary.get('out')}`",
        f"- ocr_python: `{summary.get('ocr_python')}`",
        f"- ocr_model_root: `{summary.get('ocr_model_root')}`",
        f"- paddlex_cache_root: `{summary.get('paddlex_cache_root')}`",
        "",
        "## Queue",
        f"- ocr_queue_csv: `{queue_meta.get('ocr_queue_csv')}`",
        f"- source_queue_count: {queue_meta.get('source_queue_count')}",
        f"- valid_runtime_queue_count: {queue_meta.get('valid_runtime_queue_count')}",
        f"- invalid_runtime_queue_count: {queue_meta.get('invalid_runtime_queue_count')}",
        f"- selected_count: {queue_meta.get('selected_count')}",
        "",
        "## Local model binding",
        f"- chosen_config_name: {cfg.get('name')}",
        "",
        "```json",
        json.dumps(cfg.get("params", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## No-download guard",
        f"- network_block_installed: {summary.get('ocr_run', {}).get('network_block', {}).get('installed')}",
        f"- cache_added_count: {cache_diff.get('added_count')}",
        f"- cache_modified_count: {cache_diff.get('modified_count')}",
        f"- cache_removed_count: {cache_diff.get('removed_count')}",
        f"- suspicious_log_hit_count: {summary.get('suspicious_log_hit_count')}",
        f"- suspicious_log_hits: {summary.get('suspicious_log_hits')}",
        "",
        "## OCR smoke result",
        f"- selected_count: {results.get('selected_count')}",
        f"- success_count: {results.get('success_count')}",
        f"- failed_count: {results.get('failed_count')}",
        f"- nonempty_ocr_text_count: {results.get('nonempty_ocr_text_count')}",
        f"- empty_ocr_text_count: {results.get('empty_ocr_text_count')}",
        f"- avg_task_seconds: {results.get('avg_task_seconds')}",
        "",
        "## DB-ready smoke output",
        f"- provenance_row_count: {db.get('provenance_row_count')}",
        f"- db_ready_row_count: {db.get('db_ready_row_count')}",
        f"- db_ready_sqlite: `{db.get('db_ready_sqlite')}`",
        f"- provenance_csv: `{db.get('provenance_csv')}`",
        f"- db_ready_csv: `{db.get('db_ready_csv')}`",
        "",
        "PASS condition:",
        "- PaddleOCR initialized with local model dirs.",
        "- Network block installed.",
        "- No added/modified/removed files under PaddleX official cache.",
        "- No suspicious logs about automatic download or official_models.",
        "- All selected smoke images succeeded.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(md), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/Users/yourname/Documents/AI-Local/media-archive-clean")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--stop03-2-base", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--ocr-model-root", required=True)
    ap.add_argument("--paddlex-cache-root", default=str(Path.home() / ".paddlex/official_models"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--use-orientation", action="store_true")
    ap.add_argument("--allow-doc-preprocess", action="store_true")
    args = ap.parse_args()

    # Hard defaults for no-download / low thread explosion.
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")

    out_dir = Path(args.out)
    reports, manifests, outputs, database = ensure_out(out_dir)

    summary: Dict[str, Any] = {
        "status": "UNKNOWN",
        "source_safety": "read_only_no_original_modification_local_only_no_download_smoke",
        "created_at": now_ts(),
        "out": str(out_dir),
        "project_root": args.project_root,
        "run_root": args.run_root,
        "stop03_2_base": args.stop03_2_base,
        "source_root": args.source_root,
        "ocr_python": sys.executable,
        "ocr_model_root": args.ocr_model_root,
        "paddlex_cache_root": args.paddlex_cache_root,
        "limit": args.limit,
        "env": {
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": os.environ.get("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"),
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
        },
    }

    try:
        model_root = Path(args.ocr_model_root)
        cache_root = Path(args.paddlex_cache_root)

        local_model_size = scan_dir_size(model_root)
        cache_size_before = scan_dir_size(cache_root)
        cache_before = snapshot_tree(cache_root)

        candidates = find_candidate_model_dirs(model_root)
        write_csv(manifests / "ocr_local_candidate_model_dirs.csv", [
            {**c, "roles_guess": c.get("roles_guess_text", "")}
            for c in candidates
        ])

        queue, queue_meta = build_runtime_queue(args)
        write_csv(manifests / "ocr_local_only_runtime_queue.csv", queue)
        write_jsonl(manifests / "ocr_local_only_runtime_queue.jsonl", queue)

        summary["local_model_root_size"] = local_model_size
        summary["paddlex_cache_size_before"] = cache_size_before
        summary["candidate_model_dir_count"] = len(candidates)
        summary["candidate_model_dirs"] = candidates
        summary["queue_meta"] = queue_meta

        if not model_root.exists():
            summary["status"] = "BLOCKED_LOCAL_MODEL_ROOT_MISSING"
            raise RuntimeError(f"Local OCR model root missing: {model_root}")
        if not candidates:
            summary["status"] = "BLOCKED_NO_LOCAL_MODEL_CANDIDATES"
            raise RuntimeError(f"No local OCR model candidate dirs found under: {model_root}")
        if not pick_role(candidates, "det") or not pick_role(candidates, "rec"):
            summary["status"] = "BLOCKED_LOCAL_DET_OR_REC_MISSING"
            raise RuntimeError("Local OCR model root does not expose both det and rec candidate dirs.")
        if not queue:
            summary["status"] = "BLOCKED_EMPTY_OCR_QUEUE"
            raise RuntimeError("No valid OCR smoke inputs selected.")

        t0 = time.time()
        ocr_run = init_and_run_ocr_local_only(args, candidates, queue, outputs)
        wall = time.time() - t0
        summary["ocr_run"] = {k: v for k, v in ocr_run.items() if k != "results"}
        results = ocr_run.get("results", [])

        cache_after = snapshot_tree(cache_root)
        cache_size_after = scan_dir_size(cache_root)
        cache_diff = compare_snapshots(cache_before, cache_after)

        write_csv(manifests / "ocr_local_only_result_manifest.csv", results)
        write_jsonl(manifests / "ocr_local_only_result_manifest.jsonl", results)

        db_ready = build_db_ready(args, results, database, manifests)

        suspicious = []
        if not ocr_run.get("ok"):
            suspicious.append(str(ocr_run.get("error", "")))
        for a in ocr_run.get("configs_tried", []):
            suspicious.extend(a.get("suspicious_log_hits", []))
        for r in results:
            if r.get("suspicious_log_hits"):
                suspicious.extend(str(r.get("suspicious_log_hits")).split(","))

        result_counts = {
            "selected_count": len(queue),
            "success_count": sum(1 for r in results if r.get("status") == "success"),
            "failed_count": sum(1 for r in results if r.get("status") != "success"),
            "nonempty_ocr_text_count": sum(1 for r in results if int(r.get("ocr_text_length", 0) or 0) > 0),
            "empty_ocr_text_count": sum(1 for r in results if int(r.get("ocr_text_length", 0) or 0) == 0),
            "wall_seconds": round(wall, 3),
            "sum_task_seconds": round(sum(float(r.get("elapsed_seconds", 0) or 0) for r in results), 3),
            "avg_task_seconds": round(
                sum(float(r.get("elapsed_seconds", 0) or 0) for r in results) / len(results),
                3
            ) if results else None,
        }

        summary.update({
            "paddlex_cache_size_after": cache_size_after,
            "paddlex_cache_diff": cache_diff,
            "result_counts": result_counts,
            "db_ready": db_ready,
            "suspicious_log_hits": sorted(set(x for x in suspicious if x)),
            "suspicious_log_hit_count": len([x for x in suspicious if x]),
        })

        pass_condition = (
            ocr_run.get("ok") is True
            and result_counts["selected_count"] == result_counts["success_count"]
            and cache_diff["added_count"] == 0
            and cache_diff["modified_count"] == 0
            and cache_diff["removed_count"] == 0
            and summary["suspicious_log_hit_count"] == 0
            and db_ready.get("provenance_row_count") == result_counts["selected_count"]
            and db_ready.get("db_ready_row_count") == result_counts["selected_count"]
        )

        summary["status"] = "PASS_LOCAL_ONLY_SMOKE" if pass_condition else "FAIL_LOCAL_ONLY_CONTRACT"

        write_json(reports / "stop03_4d_ocr_local_only_smoke_summary.json", summary)
        write_summary_md(reports / "stop03_4d_ocr_local_only_smoke_summary.md", summary)
        print(json.dumps({
            "status": summary["status"],
            "summary_md": str(reports / "stop03_4d_ocr_local_only_smoke_summary.md"),
            "summary_json": str(reports / "stop03_4d_ocr_local_only_smoke_summary.json"),
            "selected_count": result_counts["selected_count"],
            "success_count": result_counts["success_count"],
            "failed_count": result_counts["failed_count"],
            "cache_added_count": cache_diff["added_count"],
            "cache_modified_count": cache_diff["modified_count"],
            "suspicious_log_hit_count": summary["suspicious_log_hit_count"],
        }, ensure_ascii=False, indent=2))
        return 0 if pass_condition else 2

    except Exception as e:
        if "status" not in summary or summary["status"] == "UNKNOWN":
            summary["status"] = "FAIL_EXCEPTION"
        summary["exception"] = repr(e)
        summary["traceback"] = traceback.format_exc()

        try:
            cache_after = snapshot_tree(Path(args.paddlex_cache_root))
            summary["paddlex_cache_diff"] = compare_snapshots(snapshot_tree(Path(args.paddlex_cache_root)), cache_after)
        except Exception:
            pass

        write_json(reports / "stop03_4d_ocr_local_only_smoke_summary.json", summary)
        write_summary_md(reports / "stop03_4d_ocr_local_only_smoke_summary.md", summary)
        print(json.dumps({
            "status": summary["status"],
            "summary_md": str(reports / "stop03_4d_ocr_local_only_smoke_summary.md"),
            "exception": repr(e),
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
