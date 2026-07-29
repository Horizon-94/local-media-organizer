#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-4D OCR local model/runtime probe.

Purpose:
- Confirm the exact OCR Python runtime.
- Confirm the exact local OCR model root and internal model directory structure.
- Inspect PaddleOCR/Paddle/PaddleX availability and constructor signatures WITHOUT initializing OCR.
- Detect whether prior PaddleX official cache exists.
- Recommend which local model parameters are likely bindable.

Safety:
- Does NOT instantiate PaddleOCR.
- Does NOT run OCR inference.
- Does NOT download.
- Does NOT modify original media.
- Writes only under --out.

Run with normal system python is OK; it will call the OCR env python for introspection:
  python3 stop03_4d_ocr_local_model_runtime_probe.py \
    --ocr-python /Users/yourname/Documents/AI-Local/envs/media-archive-v06-ocr/bin/python \
    --ocr-model-root /Users/yourname/Documents/model/ocr \
    --out <OUT>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import Counter


MODEL_FILE_EXTS = {
    ".pdmodel", ".pdiparams", ".pdiparams.info", ".yml", ".yaml", ".json",
    ".onnx", ".xml", ".bin", ".txt", ".dict", ".model", ".safetensors",
    ".inference", ".params", ".nb"
}


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


def sha256_file(path: Path, limit_bytes: Optional[int] = None) -> str:
    h = hashlib.sha256()
    read_total = 0
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            if limit_bytes is not None and read_total + len(block) > limit_bytes:
                block = block[: max(0, limit_bytes - read_total)]
            h.update(block)
            read_total += len(block)
            if limit_bytes is not None and read_total >= limit_bytes:
                break
    return h.hexdigest()


def scan_dir(path: Path, max_files: int = 20000) -> Dict[str, Any]:
    total = 0
    file_count = 0
    dir_count = 0
    ext_counter = Counter()
    largest = []
    model_files = []
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "total_bytes": 0,
            "total_human": human_bytes(0),
            "file_count": 0,
            "dir_count": 0,
            "top_extensions": {},
            "largest_files": [],
            "model_files": [],
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
            "largest_files": [{"path": str(path), "bytes": size, "human": human_bytes(size)}],
            "model_files": [{"path": str(path), "bytes": size, "human": human_bytes(size), "suffix": path.suffix.lower()}],
        }

    for root, dirs, files in os.walk(path):
        dir_count += len(dirs)
        rootp = Path(root)
        for name in files:
            if file_count >= max_files:
                break
            fp = rootp / name
            try:
                st = fp.stat()
            except Exception:
                continue
            size = st.st_size
            total += size
            file_count += 1
            suf = fp.suffix.lower() or "__NO_EXT__"
            # Special case for .pdiparams.info.
            if name.lower().endswith(".pdiparams.info"):
                suf = ".pdiparams.info"
            ext_counter[suf] += 1
            largest.append((size, str(fp)))
            if len(largest) > 200:
                largest = sorted(largest, reverse=True)[:50]
            if suf in MODEL_FILE_EXTS or name in {"inference.yml", "inference.pdmodel", "inference.pdiparams"}:
                model_files.append({
                    "path": str(fp),
                    "relative_path": str(fp.relative_to(path)),
                    "bytes": size,
                    "human": human_bytes(size),
                    "suffix": suf,
                })

    largest = sorted(largest, reverse=True)[:50]
    return {
        "exists": True,
        "path": str(path),
        "total_bytes": total,
        "total_human": human_bytes(total),
        "file_count": file_count,
        "dir_count": dir_count,
        "top_extensions": dict(ext_counter.most_common(30)),
        "largest_files": [{"path": p, "bytes": b, "human": human_bytes(b)} for b, p in largest],
        "model_files": model_files[:1000],
    }


def classify_model_dir(path: Path) -> Dict[str, Any]:
    name = path.name.lower()
    files = {p.name.lower() for p in path.iterdir() if p.is_file()} if path.exists() and path.is_dir() else set()
    all_names = " ".join([name] + list(files))

    roles = []
    if any(k in all_names for k in ["det", "detect", "detection"]):
        roles.append("det")
    if any(k in all_names for k in ["rec", "recognition"]):
        roles.append("rec")
    if any(k in all_names for k in ["cls", "angle", "orientation", "textline"]):
        roles.append("cls_or_orientation")
    if any(k in all_names for k in ["doc", "uvdoc", "unwarp", "ori"]):
        roles.append("doc_preprocess")
    if any(k in all_names for k in ["table"]):
        roles.append("table")
    if any(k in all_names for k in ["layout"]):
        roles.append("layout")

    has_paddle_inference = (
        "inference.pdmodel" in files
        or any(f.endswith(".pdmodel") for f in files)
    ) and (
        "inference.pdiparams" in files
        or any(f.endswith(".pdiparams") for f in files)
    )
    has_yml = "inference.yml" in files or any(f.endswith(".yml") or f.endswith(".yaml") for f in files)
    has_onnx = any(f.endswith(".onnx") for f in files)
    has_openvino = any(f.endswith(".xml") for f in files) and any(f.endswith(".bin") for f in files)

    return {
        "path": str(path),
        "name": path.name,
        "roles_guess": roles,
        "has_paddle_inference_files": has_paddle_inference,
        "has_inference_yml_or_yaml": has_yml,
        "has_onnx": has_onnx,
        "has_openvino_xml_bin": has_openvino,
        "top_level_files": sorted(list(files))[:100],
    }


def find_candidate_model_dirs(root: Path) -> List[Dict[str, Any]]:
    if not root.exists():
        return []
    candidates = []
    for p in root.rglob("*"):
        if not p.is_dir():
            continue
        try:
            files = [x for x in p.iterdir() if x.is_file()]
        except Exception:
            continue
        file_names = {x.name.lower() for x in files}
        if (
            "inference.pdmodel" in file_names
            or "inference.pdiparams" in file_names
            or "inference.yml" in file_names
            or any(x.suffix.lower() in {".onnx", ".xml", ".bin"} for x in files)
            or any(x.name.lower().endswith(".pdiparams.info") for x in files)
        ):
            candidates.append(classify_model_dir(p))
    return candidates


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted(set(k for r in rows for k in r.keys()))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run_ocr_python_introspection(ocr_python: Path, out_dir: Path) -> Dict[str, Any]:
    code = r