#!/usr/bin/env python3
"""
Step02-2 Image Preview from Step01 Queue — A9T-v3 interface adaptation
======================================================================

Purpose:
- Consume Step01 canonical image queue only: queues/process_queue_image.jsonl
- Preserve A9T-v3 inner behavior: timelapse detection + jpg1280 preview generation
- Preserve source lineage for downstream YOLOE / embedding / Qwen-VL
- Support checkpoint/resume and per-run latest/history reports
- Keep original source media read-only

This script intentionally does NOT run YOLOE, OCR, Qwen-VL, embedding, or search.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_VERSION = "step02_2_image_preview_from_db_safe_v5_20260709_180600"
SCHEME = "step02_2_image_preview_from_step01_queue_a9t_v3_jpg1280_v1"
ARTIFACT_SCHEMA_VERSION = "image_preview_a9t_step01_queue_manifest_v1"
VISUAL_UNIT_SCHEMA_VERSION = "visual_unit_manifest_v1"

MAX_EDGE_PX = 1280
DEFAULT_SIPS_WORKERS = 8
DEFAULT_QL_WORKERS = 8

# A9T-v3 fixed timelapse contract
MIN_TIMELAPSE_COUNT = 60
MIN_INTERVAL_SECONDS = 1.5
MAX_INTERVAL_SECONDS = 10.0
TIME_GAP_SPLIT_SECONDS = 30.0
VALID_INTERVAL_RATIO_REQUIRED = 0.85
NUMERIC_MONOTONIC_RATIO_REQUIRED = 0.90
REPRESENTATIVE_POSITIONS = [("first", 0.0), ("middle", 0.5), ("last", 1.0)]

SIPS_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng", ".heic", ".heif", ".hif"
}
QL_EXTS = {
    ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".rw2", ".raf", ".orf"
}
IMAGE_EXTS = SIPS_EXTS | QL_EXTS

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = Path("/Users/yourname/Documents/AI-Local/test-output") / "step02-2-image-preview-db-safe-v5_20260709_180600"
EXPECTED_PYTHON = Path("/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
CURRENT_TEST_SOURCE_ROOT = Path("/Users/yourname/Documents/001DZLtest")
LEGACY_SOURCE_ROOT = Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE")
MODEL_USAGE_POLICY = "not_used_by_step02_2_image_preview_sips_qlmanage"
LOCAL_TOOL_POLICY = "macos_sips_required_qlmanage_required_exiftool_optional_readonly_metadata"

# Uniform offline defaults. Step02-2 does not use online model hubs, but these
# prevent accidental update/download behavior in imported or future-adjacent code.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
os.environ.setdefault("YOLO_CONFIG_DIR", str(TEST_OUTPUT_ROOT / "ultralytics-offline-config"))

DB_STAGE = "step02_2_image_preview"
DB_DERIVED_TYPE = "image_preview_jpg1280"


SUCCESS_STATUSES = {"success", "skip_already_processed"}


class TeeStream:
    """Write terminal output both to the real terminal and Step02-2 logs.

    This keeps terminal logs inside the node output directory even when the
    caller forgets to pipe through `tee`. It is record-only; it does not
    affect source media or processing outputs.
    """
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return any(getattr(s, "isatty", lambda: False)() for s in self.streams)
        except Exception:
            return False


def setup_internal_terminal_log(dirs: dict, run_invocation_id: str, run_phase: str, enabled: bool = True) -> dict:
    """Mirror stdout/stderr into logs/history and logs/latest under --out.

    The shell may still use external `tee`, but the canonical terminal log is
    always written inside this Step02-2 output directory.
    """
    if not enabled:
        return {
            "terminal_log_enabled": False,
            "terminal_log_history": "",
            "terminal_log_latest": "",
        }
    prefix = f"{run_invocation_id}_step02_2_image_preview_{run_phase}"
    history_log = dirs["logs_history"] / f"{prefix}_terminal.log"
    latest_log = dirs["logs"] / "step02_2_image_preview_terminal_latest.log"
    history_log.parent.mkdir(parents=True, exist_ok=True)
    latest_log.parent.mkdir(parents=True, exist_ok=True)
    # Use line buffering so progress is visible in the file during long runs.
    history_f = history_log.open("w", encoding="utf-8", errors="replace", buffering=1)
    latest_f = latest_log.open("w", encoding="utf-8", errors="replace", buffering=1)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeStream(original_stdout, history_f, latest_f)
    sys.stderr = TeeStream(original_stderr, history_f, latest_f)
    return {
        "terminal_log_enabled": True,
        "terminal_log_history": str(history_log),
        "terminal_log_latest": str(latest_log),
    }


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def sha256_text(text: str, n: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:n]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def csv_fieldnames_union(rows: List[dict], preferred: Optional[List[str]] = None) -> List[str]:
    keys = []
    seen = set()
    for k in preferred or []:
        if k not in seen:
            seen.add(k)
            keys.append(k)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys or ["empty"]


def write_csv(path: Path, rows: List[dict], preferred: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = csv_fieldnames_union(rows, preferred)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_cmd(cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return -1, "", repr(e)


def exiftool_available() -> bool:
    rc, out, _ = run_cmd(["bash", "-lc", "command -v exiftool"], timeout=5)
    return rc == 0 and bool(out)


def _path_info(path: Path, include_realpath: bool = False) -> dict:
    raw = Path(path).expanduser()
    info = {"path": str(raw), "exists": raw.exists(), "size_bytes": None}
    if include_realpath:
        try:
            info["realpath"] = str(raw.resolve(strict=False))
        except Exception:
            info["realpath"] = ""
    try:
        if raw.exists() and raw.is_file():
            info["size_bytes"] = raw.stat().st_size
    except Exception:
        pass
    return info


def _dependency_status(module_name: str, optional: bool = False) -> dict:
    try:
        mod = __import__(module_name)
        return {"ok": True, "optional": optional, "version": getattr(mod, "__version__", "stdlib"), "error": ""}
    except Exception as e:
        return {"ok": False, "optional": optional, "version": "", "error": repr(e)}


def _tool_status(name: str, required: bool = True) -> dict:
    rc, out, err = run_cmd(["bash", "-lc", f"command -v {name}"], timeout=5)
    path = out.strip() if rc == 0 else ""
    version = ""
    if path:
        # sips/qlmanage/exiftool all support a quick version/help command, but
        # failures here should not be treated as missing binaries.
        if name == "sips":
            rc2, out2, err2 = run_cmd([path, "--version"], timeout=5)
        elif name == "qlmanage":
            rc2, out2, err2 = run_cmd([path, "-h"], timeout=5)
        elif name == "mdls":
            rc2, out2, err2 = run_cmd([path, "-h"], timeout=5)
        elif name == "exiftool":
            rc2, out2, err2 = run_cmd([path, "-ver"], timeout=5)
        else:
            rc2, out2, err2 = (-1, "", "")
        version = (out2 or err2 or "").strip().splitlines()[0] if (out2 or err2) else ""
    return {"ok": bool(path), "required": required, "path": path, "version_or_help": version, "error": "" if path else err}


def _is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).expanduser().resolve(strict=False).relative_to(Path(root).expanduser().resolve(strict=False))
        return True
    except Exception:
        return False


def runtime_preflight(db_path: Path, out_path: Path, step01_workspace: Path | None = None, input_mode: str = "db_source_assets") -> dict:
    py_launcher = EXPECTED_PYTHON
    py_real = py_launcher.resolve(strict=False)
    actual_launcher = Path(sys.executable)
    actual_real = actual_launcher.resolve(strict=False)
    deps = {
        "sqlite3": _dependency_status("sqlite3", optional=False),
        "csv": _dependency_status("csv", optional=False),
        "json": _dependency_status("json", optional=False),
        "hashlib": _dependency_status("hashlib", optional=False),
        "pathlib": _dependency_status("pathlib", optional=False),
        "subprocess": _dependency_status("subprocess", optional=False),
        "PIL": _dependency_status("PIL", optional=False),
    }
    tools = {
        "sips": _tool_status("sips", required=True),
        "qlmanage": _tool_status("qlmanage", required=True),
        "mdls": _tool_status("mdls", required=True),
        "exiftool": _tool_status("exiftool", required=False),
    }
    assets = {
        "project_root": _path_info(PROJECT_ROOT),
        "test_output_root": _path_info(TEST_OUTPUT_ROOT),
        "default_db": _path_info(DEFAULT_DB),
        "db": _path_info(Path(db_path)),
        "default_out": _path_info(DEFAULT_OUT),
        "output_base_parent": _path_info(Path(out_path).expanduser().parent),
        "current_test_source_root_read_protected": _path_info(CURRENT_TEST_SOURCE_ROOT),
        "legacy_source_root_read_protected": _path_info(LEGACY_SOURCE_ROOT),
        "expected_python_launcher": _path_info(py_launcher, include_realpath=True),
        "expected_python_realpath": _path_info(py_real),
    }
    if step01_workspace and str(step01_workspace):
        assets["step01_workspace"] = _path_info(Path(step01_workspace))
        assets["step01_image_queue"] = _path_info(Path(step01_workspace) / "queues" / "process_queue_image.jsonl")
    blockers = []
    if actual_real != py_real:
        blockers.append(f"python_mismatch: expected {py_launcher} -> {py_real}, got {actual_launcher} -> {actual_real}")
    if not Path(db_path).expanduser().exists():
        blockers.append(f"db_missing: {db_path}")
    if input_mode == "legacy_step01_queue" and step01_workspace and not (Path(step01_workspace) / "queues" / "process_queue_image.jsonl").exists():
        blockers.append(f"step01_image_queue_missing: {Path(step01_workspace) / 'queues' / 'process_queue_image.jsonl'}")
    if not _is_under(Path(out_path), TEST_OUTPUT_ROOT):
        blockers.append(f"output_outside_test_output_root: {out_path}")
    for name, st in deps.items():
        if not st.get("ok") and not st.get("optional"):
            blockers.append(f"missing_required_dependency: {name}: {st.get('error')}")
    for name, st in tools.items():
        if st.get("required") and not st.get("ok"):
            blockers.append(f"missing_required_local_tool: {name}")
    return {
        "script_version": SCRIPT_VERSION,
        "python_executable": str(actual_launcher),
        "python_realpath": str(actual_real),
        "expected_python": str(py_launcher),
        "expected_python_realpath": str(py_real),
        "expected_python_match": actual_real == py_real,
        "expected_script_local": str(PROJECT_ROOT / "scripts/02_step01_step02_pipeline" / Path(__file__).name),
        "project_root": str(PROJECT_ROOT),
        "test_output_root": str(TEST_OUTPUT_ROOT),
        "default_db": str(DEFAULT_DB),
        "default_out": str(DEFAULT_OUT),
        "input_selection_policy": "default_db_source_assets; legacy_step01_queue_selectable_by_cli_step01_workspace",
        "current_test_source_root_read_protected": str(CURRENT_TEST_SOURCE_ROOT),
        "legacy_source_root_read_protected": str(LEGACY_SOURCE_ROOT),
        "model_usage_policy": MODEL_USAGE_POLICY,
        "local_tool_policy": LOCAL_TOOL_POLICY,
        "source_media_policy": "read_only_no_move_no_rename_no_delete_no_metadata_write",
        "derived_write_policy": "write_only_to_project_or_test_output_roots",
        "required_local_assets": {
            "project_root": str(PROJECT_ROOT),
            "test_output_root": str(TEST_OUTPUT_ROOT),
            "db": str(db_path),
            "output_base_parent": str(Path(out_path).parent),
            "current_test_source_root_read_protected": str(CURRENT_TEST_SOURCE_ROOT),
            "expected_python_launcher": str(py_launcher),
            "expected_python_realpath": str(py_real),
            "sips": tools["sips"].get("path", ""),
            "qlmanage": tools["qlmanage"].get("path", ""),
            "mdls": tools["mdls"].get("path", ""),
            "exiftool_optional": tools["exiftool"].get("path", ""),
        },
        "assets": assets,
        "dependencies": deps,
        "missing_required_dependencies": [k for k, v in deps.items() if not v.get("ok") and not v.get("optional")],
        "local_tools": tools,
        "offline_env": {k: os.environ.get(k, "") for k in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "ULTRALYTICS_OFFLINE", "NO_ALBUMENTATIONS_UPDATE", "YOLO_CONFIG_DIR"]},
        "safety": {
            "network": "blocked_by_offline_env_not_used_by_step02_2_image_preview",
            "download": "not_used",
            "dependency_install": "not_used",
            "source_media_read": "read_only_image_decode_and_metadata_read_sips_qlmanage_mdls_optional_exiftool",
            "source_media_write": "blocked_by_design_and_output_path_guard",
            "model_loading": MODEL_USAGE_POLICY,
        },
        "blockers": blockers,
    }


def parse_datetime_any(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw or raw in {"(null)", "null"}:
        return None
    raw = raw.strip('"')
    raw_no_subsec = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", raw)
    candidates = [
        raw,
        raw_no_subsec,
        raw.replace(":", "-", 2) if re.match(r"^\d{4}:\d{2}:\d{2}", raw) else raw,
        raw_no_subsec.replace(":", "-", 2) if re.match(r"^\d{4}:\d{2}:\d{2}", raw_no_subsec) else raw_no_subsec,
    ]
    fmts = [
        "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
    ]
    for text in candidates:
        for fmt in fmts:
            try:
                return _dt.datetime.strptime(text, fmt).timestamp()
            except Exception:
                pass
    return None


def ts_iso(ts: Optional[float]) -> str:
    if ts is None or ts == "":
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except Exception:
        return ""


def numeric_tail(path: Path) -> Optional[int]:
    nums = re.findall(r"\d+", path.stem)
    if not nums:
        return None
    try:
        return int(nums[-1])
    except Exception:
        return None


def mdls_time(path: Path, key: str) -> Optional[float]:
    rc, out, _ = run_cmd(["mdls", "-raw", "-name", key, str(path)], timeout=5)
    if rc != 0:
        return None
    return parse_datetime_any(out)


def exif_time(path: Path, tag: str, has_exiftool: bool) -> Optional[float]:
    if not has_exiftool:
        return None
    rc, out, _ = run_cmd(["exiftool", "-s3", f"-{tag}", str(path)], timeout=10)
    if rc != 0:
        return None
    return parse_datetime_any(out)


def enrich_slow_times_for_group(items: List[dict], has_exiftool: bool) -> None:
    # Same A9T-v3 policy: only slow fallback when fast mtime is insufficient for a large group.
    for item in items:
        p = Path(item["source_path"])
        if item.get("mdls_acquisition") is None:
            item["mdls_acquisition"] = mdls_time(p, "kMDItemAcquisitionDate")
        if item.get("mdls_content_creation") is None:
            item["mdls_content_creation"] = mdls_time(p, "kMDItemContentCreationDate")
        if item.get("mdls_fs_creation") is None:
            item["mdls_fs_creation"] = mdls_time(p, "kMDItemFSCreationDate")
        if has_exiftool:
            if item.get("exif_datetime_original") is None:
                item["exif_datetime_original"] = exif_time(p, "DateTimeOriginal", has_exiftool)
            if item.get("exif_create_date") is None:
                item["exif_create_date"] = exif_time(p, "CreateDate", has_exiftool)
            if item.get("exif_subsec_datetime_original") is None:
                item["exif_subsec_datetime_original"] = exif_time(p, "SubSecDateTimeOriginal", has_exiftool)


def interval_report_for_source(items: List[dict], source: str) -> dict:
    timed = [x for x in items if x.get(source) is not None]
    if len(timed) < 2:
        return {"source": source, "usable_count": len(timed), "score": -1, "reason": "too_few_timestamps"}
    timed.sort(key=lambda x: (x[source], x["source_relative_path"]))
    intervals = []
    for a, b in zip(timed, timed[1:]):
        dt = b[source] - a[source]
        if dt >= 0:
            intervals.append(dt)
    positive = [x for x in intervals if x > 0]
    if not positive:
        return {"source": source, "usable_count": len(timed), "score": -1, "reason": "no_positive_intervals"}
    valid = [x for x in positive if MIN_INTERVAL_SECONDS <= x <= MAX_INTERVAL_SECONDS]
    valid_ratio = len(valid) / len(positive)
    median_interval = statistics.median(positive)
    score = valid_ratio + (0.25 if MIN_INTERVAL_SECONDS <= median_interval <= MAX_INTERVAL_SECONDS else 0)
    return {
        "source": source,
        "usable_count": len(timed),
        "score": round(score, 4),
        "reason": "ok",
        "valid_interval_ratio": round(valid_ratio, 4),
        "median_interval_seconds": round(median_interval, 3),
        "min_interval_seconds": round(min(positive), 3),
        "max_interval_seconds": round(max(positive), 3),
    }


def choose_best_time_source(items: List[dict], allow_slow_fallback: bool, has_exiftool: bool) -> Tuple[dict, List[dict], bool]:
    fast_sources = ["file_mtime"]
    slow_sources = [
        "exif_datetime_original", "exif_subsec_datetime_original", "exif_create_date",
        "mdls_acquisition", "mdls_content_creation", "mdls_fs_creation", "file_ctime",
    ]
    reports = [interval_report_for_source(items, s) for s in fast_sources]
    best = sorted(reports, key=lambda r: r.get("score", -1), reverse=True)[0]
    if best.get("score", -1) >= 1.0:
        return best, reports, False
    if allow_slow_fallback:
        enrich_slow_times_for_group(items, has_exiftool)
        slow_reports = [interval_report_for_source(items, s) for s in slow_sources]
        reports.extend(slow_reports)
        best = sorted(reports, key=lambda r: r.get("score", -1), reverse=True)[0]
        return best, reports, True
    return best, reports, False


def split_segments(items: List[dict], source: str) -> List[List[dict]]:
    timed = [x for x in items if x.get(source) is not None]
    timed.sort(key=lambda x: (x[source], x["source_relative_path"]))
    segments, current, prev_ts = [], [], None
    for item in timed:
        ts = item[source]
        if prev_ts is not None and ts - prev_ts > TIME_GAP_SPLIT_SECONDS:
            if current:
                segments.append(current)
            current = []
        current.append(item)
        prev_ts = ts
    if current:
        segments.append(current)
    return segments


def judge_segment(segment: List[dict], source: str) -> Tuple[bool, dict]:
    n = len(segment)
    if n < MIN_TIMELAPSE_COUNT:
        return False, {"reason": "too_few_images", "image_count": n}
    intervals = []
    for a, b in zip(segment, segment[1:]):
        dt = b[source] - a[source]
        if dt >= 0:
            intervals.append(dt)
    positive = [x for x in intervals if x > 0]
    if not positive:
        return False, {"reason": "no_positive_intervals", "image_count": n}
    valid = [x for x in positive if MIN_INTERVAL_SECONDS <= x <= MAX_INTERVAL_SECONDS]
    valid_ratio = len(valid) / len(positive)
    median_interval = statistics.median(positive)
    nums = [x["numeric_tail"] for x in segment if x.get("numeric_tail") is not None]
    numeric_ratio = None
    if len(nums) >= 2:
        total = 0
        inc = 0
        for a, b in zip(nums, nums[1:]):
            total += 1
            if b >= a:
                inc += 1
        numeric_ratio = inc / total if total else None
    fail_reasons = []
    if not (MIN_INTERVAL_SECONDS <= median_interval <= MAX_INTERVAL_SECONDS):
        fail_reasons.append("median_interval_out_of_range")
    if valid_ratio < VALID_INTERVAL_RATIO_REQUIRED:
        fail_reasons.append("valid_interval_ratio_too_low")
    if numeric_ratio is not None and numeric_ratio < NUMERIC_MONOTONIC_RATIO_REQUIRED:
        fail_reasons.append("numeric_monotonic_ratio_too_low")
    is_tl = not fail_reasons
    return is_tl, {
        "reason": "high_confidence_timelapse" if is_tl else "|".join(fail_reasons),
        "image_count": n,
        "median_interval_seconds": round(median_interval, 3),
        "min_interval_seconds": round(min(positive), 3),
        "max_interval_seconds": round(max(positive), 3),
        "valid_interval_ratio": round(valid_ratio, 4),
        "numeric_monotonic_ratio": round(numeric_ratio, 4) if numeric_ratio is not None else "",
        "start_time": ts_iso(segment[0][source]),
        "end_time": ts_iso(segment[-1][source]),
        "first_file": segment[0]["source_relative_path"],
        "last_file": segment[-1]["source_relative_path"],
    }


def choose_representatives(segment: List[dict]) -> List[Tuple[str, int, dict]]:
    n = len(segment)
    selected, used = [], set()
    for label, pos in REPRESENTATIVE_POSITIONS:
        idx = round((n - 1) * pos)
        idx = max(0, min(n - 1, idx))
        if idx not in used:
            used.add(idx)
            selected.append((label, idx, segment[idx]))
    return selected


def safe_output_name(preview_artifact_id: str, rel: str) -> str:
    stem = "_".join(Path(rel).parts)
    stem = "".join(c if c.isalnum() or c in "-_.'" else "_" for c in stem)
    if len(stem) > 150:
        stem = stem[-150:]
    return f"{preview_artifact_id}_{stem}.jpg"


def validate_jpg1280(path: Path) -> dict:
    if not path.exists():
        return {"valid": False, "reason": "missing", "size": 0, "width": None, "height": None}
    size = path.stat().st_size
    if size <= 0:
        return {"valid": False, "reason": "zero_size", "size": size, "width": None, "height": None}
    proc = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return {"valid": False, "reason": "unreadable_by_sips", "size": size, "width": None, "height": None}
    width = height = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            try: width = int(line.split(":", 1)[1].strip())
            except Exception: pass
        elif line.startswith("pixelHeight:"):
            try: height = int(line.split(":", 1)[1].strip())
            except Exception: pass
    if not width or not height:
        return {"valid": False, "reason": "invalid_dimensions", "size": size, "width": width, "height": height}
    if max(width, height) > MAX_EDGE_PX:
        return {"valid": False, "reason": f"max_edge_exceeds_{MAX_EDGE_PX}", "size": size, "width": width, "height": height}
    return {"valid": True, "reason": "ok", "size": size, "width": width, "height": height}


def read_jsonl(path: Path) -> List[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows



def db_table_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def db_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if table not in db_table_names(conn):
        return []
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def ensure_project_db_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS source_assets (
        source_content_id TEXT PRIMARY KEY,
        absolute_path TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        file_name TEXT NOT NULL,
        extension TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        mtime INTEGER NOT NULL,
        ctime INTEGER NOT NULL,
        volume_id TEXT NOT NULL DEFAULT 'LOCAL',
        online_status INTEGER DEFAULT 1,
        first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_deleted_or_missing INTEGER DEFAULT 0
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS derived_assets (
        derived_id TEXT PRIMARY KEY,
        source_content_id TEXT NOT NULL,
        derived_type TEXT NOT NULL,
        derived_path TEXT NOT NULL,
        frame_index INTEGER DEFAULT -1,
        time_position_ms INTEGER DEFAULT -1,
        width INTEGER,
        height INTEGER,
        sha256 TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS visual_units (
        visual_unit_id TEXT PRIMARY KEY,
        source_content_id TEXT NOT NULL,
        derived_id TEXT NOT NULL,
        visual_file TEXT NOT NULL,
        time_position_ms INTEGER NOT NULL DEFAULT -1,
        near_black INTEGER DEFAULT 0,
        luma_mean REAL,
        luma_std REAL,
        near_dup_group_id TEXT,
        is_near_dup_representative INTEGER DEFAULT 0
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS embeddings (
        embedding_id TEXT PRIMARY KEY,
        visual_unit_id TEXT NOT NULL,
        source_content_id TEXT NOT NULL,
        model_name TEXT NOT NULL,
        model_path TEXT NOT NULL,
        dimension INTEGER NOT NULL DEFAULT 512,
        vector_key TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS model_runs (
        run_id TEXT PRIMARY KEY,
        stage TEXT,
        script_version TEXT NOT NULL,
        status TEXT,
        input_count INTEGER DEFAULT 0,
        output_count INTEGER DEFAULT 0,
        error_message TEXT,
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        finished_at DATETIME
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS processing_errors (
        error_id TEXT PRIMARY KEY,
        run_id TEXT,
        stage TEXT,
        item_id TEXT,
        item_path TEXT,
        error_type TEXT,
        error_message TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()


def insert_model_run(conn: sqlite3.Connection, run_id: str, stage: str, script_version: str, status: str, input_count: int = 0, output_count: int = 0, error_message: str = "") -> None:
    ensure_project_db_tables(conn)
    cols = db_columns(conn, "model_runs")
    values = {
        "run_id": run_id,
        "stage": stage,
        "model_name": "local_image_preview_sips_qlmanage",
        "model_path": "builtin_macos_sips_qlmanage_no_model",
        "script_version": script_version,
        "script_path": str(Path(__file__).resolve(strict=False)),
        "status": status,
        "input_count": int(input_count or 0),
        "output_count": int(output_count or 0),
        "error_message": error_message or "",
        "started_at": now_iso(),
        "finished_at": None,
    }
    use_cols = [c for c in values.keys() if c in cols]
    placeholders = ",".join(["?"] * len(use_cols))
    sql = f"INSERT OR REPLACE INTO model_runs ({','.join(use_cols)}) VALUES ({placeholders})"
    conn.execute(sql, [values[c] for c in use_cols])
    conn.commit()


def finish_model_run(conn: sqlite3.Connection, run_id: str, status: str, input_count: int, output_count: int, error_message: str = "") -> None:
    cols = db_columns(conn, "model_runs")
    pairs = []
    vals = []
    for c, v in {
        "status": status,
        "input_count": int(input_count or 0),
        "output_count": int(output_count or 0),
        "error_message": error_message or "",
        "finished_at": now_iso(),
    }.items():
        if c in cols:
            pairs.append(f"{c}=?")
            vals.append(v)
    if pairs:
        vals.append(run_id)
        conn.execute(f"UPDATE model_runs SET {', '.join(pairs)} WHERE run_id=?", vals)
        conn.commit()


def fetch_image_queue_rows_from_db(db_path: Path, limit: int = 0) -> List[dict]:
    db_path = db_path.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_project_db_tables(conn)
    sql = """
        SELECT source_content_id, absolute_path, relative_path, file_name, extension, media_type,
               size_bytes, mtime, ctime, volume_id, online_status, is_deleted_or_missing
        FROM source_assets
        WHERE media_type='image'
          AND COALESCE(online_status, 1)=1
          AND COALESCE(is_deleted_or_missing, 0)=0
        ORDER BY relative_path, absolute_path
    """
    params = []
    if limit and int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        p = Path(d["absolute_path"])
        rows.append({
            "source_file_id": d["source_content_id"],
            "source_content_id": d["source_content_id"],
            "source_path": d["absolute_path"],
            "source_relative_path": d.get("relative_path") or p.name,
            "file_name": d.get("file_name") or p.name,
            "extension": (d.get("extension") or p.suffix).lower(),
            "media_kind": "image",
            "media_type": "image",
            "next_action": "process",
            "size_bytes": d.get("size_bytes"),
            "mtime": d.get("mtime"),
            "ctime": d.get("ctime"),
            "volume_id": d.get("volume_id") or "LOCAL",
            "input_source": "project_db_source_assets",
        })
    conn.close()
    return rows


def write_step02_image_outputs_to_project_db(db_path: Path, run_id: str, preview_rows: List[dict], visual_rows: List[dict]) -> dict:
    db_path = db_path.expanduser().resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_project_db_tables(conn)
    insert_model_run(conn, run_id, DB_STAGE, SCRIPT_VERSION, "running", len(preview_rows), 0, "")

    visual_by_preview = {r.get("preview_artifact_id"): r for r in visual_rows}
    derived_count = 0
    visual_count = 0
    errors = 0

    for r in preview_rows:
        if r.get("status") != "success":
            continue
        preview_id = r.get("preview_artifact_id") or ""
        source_content_id = r.get("parent_source_content_id") or r.get("parent_source_file_id") or ""
        out_path = r.get("output_path") or ""
        out_sha = r.get("output_sha256") or ""
        if not preview_id or not source_content_id or not out_path:
            errors += 1
            continue
        derived_id = "der_" + sha256_text(f"{DB_DERIVED_TYPE}|{preview_id}|{out_sha}|{out_path}", 24)
        try:
            width = int(float(r.get("width") or 0)) or None
        except Exception:
            width = None
        try:
            height = int(float(r.get("height") or 0)) or None
        except Exception:
            height = None
        conn.execute("""
            INSERT OR REPLACE INTO derived_assets
            (derived_id, source_content_id, derived_type, derived_path, frame_index, time_position_ms, width, height, sha256)
            VALUES (?, ?, ?, ?, -1, -1, ?, ?, ?)
        """, (derived_id, source_content_id, DB_DERIVED_TYPE, out_path, width, height, out_sha))
        derived_count += 1

        vu = visual_by_preview.get(preview_id)
        visual_unit_id = (vu or {}).get("visual_unit_id") or ("vu_" + sha256_text(f"visual_unit|{derived_id}|{out_sha}", 24))
        conn.execute("""
            INSERT OR REPLACE INTO visual_units
            (visual_unit_id, source_content_id, derived_id, visual_file, time_position_ms, near_black, luma_mean, luma_std, near_dup_group_id, is_near_dup_representative)
            VALUES (?, ?, ?, ?, -1, 0, NULL, NULL, NULL, 1)
        """, (visual_unit_id, source_content_id, derived_id, out_path))
        visual_count += 1

    status = "done" if errors == 0 else "done_with_errors"
    finish_model_run(conn, run_id, status, len(preview_rows), visual_count, "" if errors == 0 else f"db_output_row_errors={errors}")
    conn.close()
    return {
        "db_path": str(db_path),
        "stage": DB_STAGE,
        "run_id": run_id,
        "status": status,
        "derived_assets_upserted": derived_count,
        "visual_units_upserted": visual_count,
        "db_output_row_errors": errors,
    }


def audit_project_db_for_step02(db_path: Path, preflight: Optional[dict] = None) -> dict:
    db_path = db_path.expanduser().resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_project_db_tables(conn)
    out = {"script_version": SCRIPT_VERSION, "mode": "db_audit_only", "db_path": str(db_path), "counts": {}}
    for t in ["source_assets", "derived_assets", "visual_units", "embeddings"]:
        out["counts"][t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    out["pending_image_source_assets"] = conn.execute("""
        SELECT COUNT(*) FROM source_assets
        WHERE media_type='image' AND COALESCE(online_status,1)=1 AND COALESCE(is_deleted_or_missing,0)=0
    """).fetchone()[0]
    out["sample_images"] = [dict(r) for r in conn.execute("""
        SELECT source_content_id, relative_path, extension, absolute_path
        FROM source_assets
        WHERE media_type='image'
        ORDER BY relative_path
        LIMIT 5
    """)]
    conn.close()
    if preflight is not None:
        out["runtime_preflight"] = preflight
    return out


def init_state(db_path: Path, reset: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS image_preview_state (
        preview_artifact_id TEXT PRIMARY KEY,
        source_file_id TEXT,
        source_content_id TEXT,
        source_path_at_processing_time TEXT,
        preview_role TEXT,
        backend TEXT,
        status TEXT,
        output_path TEXT,
        output_sha256 TEXT,
        validation_reason TEXT,
        updated_at TEXT,
        error TEXT
    )
    """)
    conn.commit()
    return conn


def get_state(conn: sqlite3.Connection, preview_artifact_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT preview_artifact_id, source_file_id, source_content_id, source_path_at_processing_time, preview_role, backend, status, output_path, output_sha256, validation_reason, updated_at, error FROM image_preview_state WHERE preview_artifact_id=?", (preview_artifact_id,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ["preview_artifact_id", "source_file_id", "source_content_id", "source_path_at_processing_time", "preview_role", "backend", "status", "output_path", "output_sha256", "validation_reason", "updated_at", "error"]
    return dict(zip(keys, row))


def upsert_state(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute("""
    INSERT OR REPLACE INTO image_preview_state
    (preview_artifact_id, source_file_id, source_content_id, source_path_at_processing_time, preview_role, backend, status, output_path, output_sha256, validation_reason, updated_at, error)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row.get("preview_artifact_id"), row.get("parent_source_file_id"), row.get("parent_source_content_id"), row.get("parent_source_path_at_processing_time"),
        row.get("preview_role"), row.get("backend"), row.get("status"), row.get("output_path"), row.get("output_sha256"), row.get("validation_reason"), now_iso(), row.get("stderr_tail") or row.get("error") or "",
    ))
    conn.commit()


class TelemetrySampler:
    def __init__(self, telemetry_dir: Path, api_dir: Path, run_invocation_id: str, run_phase: str, interval: float):
        self.telemetry_dir = telemetry_dir
        self.api_dir = api_dir
        self.run_invocation_id = run_invocation_id
        self.run_phase = run_phase
        self.interval = float(interval or 0)
        self.stop_event = threading.Event()
        self.rows: List[dict] = []
        self.thread: Optional[threading.Thread] = None
        self.started = time.perf_counter()

    def start(self):
        if self.interval <= 0:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        if self.interval <= 0:
            return
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(2, self.interval + 1))

    def _loop(self):
        while not self.stop_event.is_set():
            self.sample("image_preview_generation")
            self.stop_event.wait(self.interval)

    def sample(self, stage: str):
        elapsed = round(time.perf_counter() - self.started, 3)
        row = {
            "run_invocation_id": self.run_invocation_id,
            "run_phase": self.run_phase,
            "elapsed_seconds": elapsed,
            "timestamp": now_iso(),
            "stage": stage,
            "sips_pid_count": 0,
            "qlmanage_pid_count": 0,
            "sips_cpu_percent_sum": 0.0,
            "qlmanage_cpu_percent_sum": 0.0,
            "process_cpu_cores_estimated": 0.0,
            "process_rss_mb_sum": 0.0,
        }
        try:
            p = subprocess.run(["ps", "-axo", "comm,%cpu,rss"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            for line in p.stdout.splitlines()[1:]:
                parts = line.rsplit(None, 2)
                if len(parts) != 3:
                    continue
                comm, cpu_s, rss_s = parts
                name = Path(comm).name.lower()
                try: cpu = float(cpu_s)
                except Exception: cpu = 0.0
                try: rss = float(rss_s) / 1024.0
                except Exception: rss = 0.0
                if name == "sips":
                    row["sips_pid_count"] += 1
                    row["sips_cpu_percent_sum"] += cpu
                    row["process_rss_mb_sum"] += rss
                elif name == "qlmanage":
                    row["qlmanage_pid_count"] += 1
                    row["qlmanage_cpu_percent_sum"] += cpu
                    row["process_rss_mb_sum"] += rss
            row["process_cpu_cores_estimated"] = round((row["sips_cpu_percent_sum"] + row["qlmanage_cpu_percent_sum"]) / 100.0, 3)
            row["sips_cpu_percent_sum"] = round(row["sips_cpu_percent_sum"], 3)
            row["qlmanage_cpu_percent_sum"] = round(row["qlmanage_cpu_percent_sum"], 3)
            row["process_rss_mb_sum"] = round(row["process_rss_mb_sum"], 3)
        except Exception as e:
            row["sample_error"] = repr(e)
        self.rows.append(row)
        self.api_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.api_dir / "step02_2_resource_status_latest.json", row)


def make_dirs(out: Path, run_invocation_id: str) -> dict:
    dirs = {
        "out": out,
        "derived": out / "derived" / "image_previews" / "a9t_v3_jpg1280",
        "keyframes": out / "derived" / "image_previews" / "a9t_v3_jpg1280" / "keyframe_pool_jpg",
        "normal": out / "derived" / "image_previews" / "a9t_v3_jpg1280" / "normal_preview_pool_jpg",
        "tmp": out / "tmp" / run_invocation_id,
        "manifests": out / "manifests",
        "manifests_history": out / "manifests" / "history",
        "reports": out / "reports",
        "reports_history": out / "reports" / "history",
        "final_report": out / "final_report",
        "final_report_history": out / "final_report" / "history",
        "telemetry": out / "telemetry",
        "telemetry_history": out / "telemetry" / "history",
        "telemetry_api": out / "telemetry" / "api",
        "logs": out / "logs",
        "logs_history": out / "logs" / "history",
        "state": out / "state",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def build_items(queue_rows: List[dict]) -> List[dict]:
    items = []
    for r in queue_rows:
        if r.get("media_kind") != "image" or r.get("next_action") not in {"process", ""}:
            continue
        ext = (r.get("extension") or Path(r.get("source_path", "")).suffix).lower()
        if ext not in IMAGE_EXTS:
            continue
        p = Path(r["source_path"])
        try:
            st = p.stat()
            stat_status = "ok"
            stat_error = ""
            mtime = st.st_mtime
            ctime = st.st_ctime
        except Exception as e:
            stat_status = "failed"
            stat_error = repr(e)
            mtime = ctime = None
        rel = r.get("source_relative_path") or p.name
        items.append({
            **r,
            "source_path": str(p),
            "source_relative_path": rel,
            "source_relative_dir": str(Path(rel).parent) if str(Path(rel).parent) != "." else "",
            "extension": ext,
            "file_mtime": mtime,
            "file_ctime": ctime,
            "numeric_tail": numeric_tail(p),
            "stat_status": stat_status,
            "stat_error": stat_error,
            "mdls_acquisition": None,
            "mdls_content_creation": None,
            "mdls_fs_creation": None,
            "exif_datetime_original": None,
            "exif_subsec_datetime_original": None,
            "exif_create_date": None,
        })
    return items


def detect_timelapse(items: List[dict], has_exiftool: bool) -> Tuple[List[dict], List[dict], List[dict], Dict[str, dict], set, set, int, float]:
    t0 = time.perf_counter()
    groups = defaultdict(list)
    for item in items:
        if item.get("stat_status") == "ok":
            groups[(item["source_relative_dir"], item["extension"])].append(item)

    group_rows, timelapse_rows, keyframe_rows = [], [], []
    timelapse_member_ids, keyframe_ids = set(), set()
    path_to_sequence_info: Dict[str, dict] = {}
    sequence_id = 0
    slow_fallback_group_count = 0

    for (relative_dir, ext), group_items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        group_count = len(group_items)
        if group_count < MIN_TIMELAPSE_COUNT:
            group_rows.append({
                "relative_dir": relative_dir, "extension": ext, "image_count": group_count,
                "best_time_source": "", "best_score": "", "slow_fallback_used": False,
                "segment_count": 0, "timelapse_segment_count": 0, "reason": "too_few_for_timelapse",
            })
            continue
        best, _reports, slow_used = choose_best_time_source(group_items, allow_slow_fallback=True, has_exiftool=has_exiftool)
        if slow_used:
            slow_fallback_group_count += 1
        best_source = best.get("source")
        segments = split_segments(group_items, best_source) if best_source else []
        tl_count_in_group = 0
        for segment_index, segment in enumerate(segments, start=1):
            is_tl, info = judge_segment(segment, best_source)
            if not is_tl:
                continue
            sequence_id += 1
            tl_count_in_group += 1
            reps = choose_representatives(segment)
            for item in segment:
                timelapse_member_ids.add(item["source_file_id"])
                path_to_sequence_info[item["source_file_id"]] = {
                    "sequence_id": sequence_id,
                    "timelapse_role": "member",
                    "representative_position": "",
                }
            for pos_label, index_in_sequence, item in reps:
                keyframe_ids.add(item["source_file_id"])
                path_to_sequence_info[item["source_file_id"]] = {
                    "sequence_id": sequence_id,
                    "timelapse_role": "keyframe",
                    "representative_position": pos_label,
                }
                keyframe_rows.append({
                    "sequence_id": sequence_id,
                    "representative_position": pos_label,
                    "index_in_sequence": index_in_sequence,
                    "relative_dir": relative_dir,
                    "source_relative_path": item["source_relative_path"],
                    "parent_source_file_id": item.get("source_file_id"),
                    "parent_source_content_id": item.get("source_content_id"),
                    "extension": ext,
                    "source_used": best_source,
                    "timestamp": item.get(best_source),
                    "timestamp_iso": ts_iso(item.get(best_source)),
                    "backend_planned": "sips_direct_jpg" if ext in SIPS_EXTS else "system_preview_then_jpg",
                    "keyframe_pool": "keyframe_pool_jpg",
                })
            timelapse_rows.append({
                "sequence_id": sequence_id,
                "relative_dir": relative_dir,
                "extension": ext,
                "segment_index": segment_index,
                "source_used": best_source,
                "image_count": info.get("image_count"),
                "selected_representatives": len(reps),
                "median_interval_seconds": info.get("median_interval_seconds"),
                "min_interval_seconds": info.get("min_interval_seconds"),
                "max_interval_seconds": info.get("max_interval_seconds"),
                "valid_interval_ratio": info.get("valid_interval_ratio"),
                "numeric_monotonic_ratio": info.get("numeric_monotonic_ratio"),
                "start_time": info.get("start_time"),
                "end_time": info.get("end_time"),
                "first_file": info.get("first_file"),
                "last_file": info.get("last_file"),
                "reason": info.get("reason"),
            })
        group_rows.append({
            "relative_dir": relative_dir, "extension": ext, "image_count": group_count,
            "best_time_source": best_source, "best_score": best.get("score"),
            "best_valid_interval_ratio": best.get("valid_interval_ratio", ""),
            "best_median_interval_seconds": best.get("median_interval_seconds", ""),
            "best_min_interval_seconds": best.get("min_interval_seconds", ""),
            "best_max_interval_seconds": best.get("max_interval_seconds", ""),
            "slow_fallback_used": slow_used, "segment_count": len(segments),
            "timelapse_segment_count": tl_count_in_group,
            "reason": "ok" if tl_count_in_group else "no_timelapse_segment",
        })
    return group_rows, timelapse_rows, keyframe_rows, path_to_sequence_info, timelapse_member_ids, keyframe_ids, slow_fallback_group_count, time.perf_counter() - t0


def make_preview_artifact_id(item: dict, preview_role: str, sequence_id: Any, rep: str) -> str:
    raw = f"image_preview_v1|{item.get('source_file_id')}|{item.get('source_content_id')}|{preview_role}|seq={sequence_id}|rep={rep}|jpg1280"
    return "ip_" + sha256_text(raw, 24)


def plan_jobs(items: List[dict], dirs: dict, seq_info: Dict[str, dict], member_ids: set, keyframe_ids: set) -> Tuple[List[dict], List[dict]]:
    decisions, jobs = [], []
    for item in items:
        ext = item["extension"]
        sid = item.get("source_file_id")
        if item.get("stat_status") != "ok":
            decisions.append({
                "parent_source_file_id": sid,
                "parent_source_content_id": item.get("source_content_id"),
                "source_path": item.get("source_path"),
                "source_relative_path": item.get("source_relative_path"),
                "extension": ext,
                "preview_role": "source_stat_failed",
                "should_generate_preview": False,
                "backend": "",
                "sequence_id": "",
                "representative_position": "",
                "skipped_reason": item.get("stat_error") or "stat_failed",
            })
            continue
        if sid in keyframe_ids:
            seq = seq_info[sid]
            preview_role = "timelapse_keyframe"
            should = True
            out_pool = dirs["keyframes"]
            sequence_id = seq["sequence_id"]
            rep = seq["representative_position"]
            skipped_reason = ""
        elif sid in member_ids:
            seq = seq_info[sid]
            preview_role = "timelapse_member_skipped"
            should = False
            out_pool = None
            sequence_id = seq["sequence_id"]
            rep = ""
            skipped_reason = "covered_by_timelapse_keyframes"
        else:
            preview_role = "normal_image"
            should = True
            out_pool = dirs["normal"]
            sequence_id = ""
            rep = ""
            skipped_reason = ""
        backend = "sips_direct_jpg" if ext in SIPS_EXTS else "system_preview_then_jpg"
        preview_artifact_id = make_preview_artifact_id(item, preview_role, sequence_id, rep) if should else ""
        output_path = str(out_pool / safe_output_name(preview_artifact_id, item["source_relative_path"])) if should and out_pool else ""
        d = {
            "preview_artifact_id": preview_artifact_id,
            "parent_source_file_id": sid,
            "parent_source_content_id": item.get("source_content_id"),
            "parent_source_path_at_processing_time": item.get("source_path"),
            "parent_media_kind": "image",
            "source_path": item.get("source_path"),
            "source_relative_path": item.get("source_relative_path"),
            "source_relative_dir": item.get("source_relative_dir"),
            "extension": ext,
            "preview_role": preview_role,
            "should_generate_preview": should,
            "backend": backend if should else "",
            "sequence_id": sequence_id,
            "representative_position": rep,
            "skipped_reason": skipped_reason,
            "planned_output_path": output_path,
        }
        decisions.append(d)
        if should:
            jobs.append({**d, "output_path": output_path})
    return decisions, jobs


def state_success_valid(conn: sqlite3.Connection, job: dict) -> Optional[dict]:
    st = get_state(conn, job["preview_artifact_id"])
    if not st or st.get("status") != "success":
        return None
    out = Path(st.get("output_path") or job.get("output_path") or "")
    val = validate_jpg1280(out)
    if not val["valid"]:
        return None
    digest = sha256_file(out)
    if st.get("output_sha256") and st.get("output_sha256") != digest:
        return None
    return {
        **job,
        "task_action": "skip_already_processed",
        "status": "success",
        "output_path": str(out),
        "output_size": val["size"],
        "width": val["width"],
        "height": val["height"],
        "validation_reason": "ok",
        "output_sha256": digest,
        "sips_direct_elapsed_seconds": 0.0,
        "system_preview_elapsed_seconds": 0.0,
        "system_transcode_to_jpg_elapsed_seconds": 0.0,
        "total_task_elapsed_seconds": 0.0,
        "returncode": 0,
        "stderr_tail": "",
    }


def run_sips_job(job: dict) -> dict:
    src = Path(job["source_path"])
    dst = Path(job["output_path"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["sips", "-s", "format", "jpeg", "-Z", str(MAX_EDGE_PX), str(src), "--out", str(dst)]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    elapsed = time.perf_counter() - t0
    val = validate_jpg1280(dst)
    status = "success" if proc.returncode == 0 and val["valid"] else "failed"
    digest = sha256_file(dst) if status == "success" else ""
    return {
        **job,
        "task_action": job.get("task_action", "process_new"),
        "backend": "sips_direct_jpg",
        "status": status,
        "sips_direct_elapsed_seconds": round(elapsed, 6),
        "system_preview_elapsed_seconds": 0.0,
        "system_transcode_to_jpg_elapsed_seconds": 0.0,
        "total_task_elapsed_seconds": round(elapsed, 6),
        "output_size": val["size"], "width": val["width"], "height": val["height"],
        "validation_reason": val["reason"], "returncode": proc.returncode,
        "output_sha256": digest,
        "stderr_tail": (proc.stderr or "")[-800:].replace("\n", " "),
    }


def run_ql_job(job: dict, tmp_root: Path) -> dict:
    src = Path(job["source_path"])
    dst = Path(job["output_path"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    one_tmp = tmp_root / job["preview_artifact_id"]
    one_tmp.mkdir(parents=True, exist_ok=True)
    ql_cmd = ["qlmanage", "-t", "-s", str(MAX_EDGE_PX), "-o", str(one_tmp), str(src)]
    ql_t0 = time.perf_counter()
    ql_proc = subprocess.run(ql_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ql_elapsed = time.perf_counter() - ql_t0
    produced_files = [p for p in one_tmp.glob("*") if p.is_file()]
    produced = max(produced_files, key=lambda p: p.stat().st_mtime) if produced_files else None
    transcode_elapsed = 0.0
    tr_rc = None
    tr_stderr = ""
    if produced and produced.exists():
        tr_cmd = ["sips", "-s", "format", "jpeg", "-Z", str(MAX_EDGE_PX), str(produced), "--out", str(dst)]
        tr_t0 = time.perf_counter()
        tr_proc = subprocess.run(tr_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        transcode_elapsed = time.perf_counter() - tr_t0
        tr_rc = tr_proc.returncode
        tr_stderr = tr_proc.stderr or ""
    val = validate_jpg1280(dst)
    status = "success" if ql_proc.returncode == 0 and produced is not None and tr_rc == 0 and val["valid"] else "failed"
    digest = sha256_file(dst) if status == "success" else ""
    shutil.rmtree(one_tmp, ignore_errors=True)
    return {
        **job,
        "task_action": job.get("task_action", "process_new"),
        "backend": "system_preview_then_jpg",
        "status": status,
        "sips_direct_elapsed_seconds": 0.0,
        "system_preview_elapsed_seconds": round(ql_elapsed, 6),
        "system_transcode_to_jpg_elapsed_seconds": round(transcode_elapsed, 6),
        "total_task_elapsed_seconds": round(ql_elapsed + transcode_elapsed, 6),
        "output_size": val["size"], "width": val["width"], "height": val["height"],
        "validation_reason": val["reason"], "returncode": f"ql={ql_proc.returncode};transcode={tr_rc}",
        "output_sha256": digest,
        "stderr_tail": ((ql_proc.stderr or "") + " " + tr_stderr)[-800:].replace("\n", " "),
    }


def resolve_run_phase(run_phase: str, limit_new: int) -> str:
    if run_phase != "auto":
        return run_phase
    return f"first{limit_new}" if limit_new and limit_new > 0 else "resume"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Step02-2 image preview from project DB source_assets or Step01 image queue, A9T-v3 compatible internals.")
    ap.add_argument("--step01-workspace", required=False, default="", help="Legacy mode: Step01 workspace containing queues/process_queue_image.jsonl. If omitted, DB mode is used.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Project SQLite DB. DB mode reads source_assets and writes derived_assets/visual_units.")
    ap.add_argument("--db-audit-only", action="store_true", help="Only audit DB structure/counts; no preview generation.")
    ap.add_argument("--preflight-only", action="store_true", help="Only check fixed paths, runtime, dependencies, local tools, and safety policy; no preview generation.")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--sips-concurrency", type=int, default=DEFAULT_SIPS_WORKERS)
    ap.add_argument("--ql-concurrency", type=int, default=DEFAULT_QL_WORKERS)
    ap.add_argument("--limit-new", type=int, default=0, help="Max new preview jobs to start this run. 0 means no limit.")
    ap.add_argument("--telemetry-interval", type=float, default=0.0)
    ap.add_argument("--run-phase", default="auto", choices=["auto", "manual", "resume", "first30", "first15", "first1997"])
    ap.add_argument("--disable-internal-terminal-log", action="store_true", help="Disable canonical terminal log mirror under OUT/logs. Default: enabled.")
    ap.add_argument("--reset-state", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    run_invocation_id = now_stamp()
    run_phase = resolve_run_phase(args.run_phase, args.limit_new)
    db_path = Path(args.db).expanduser().resolve()
    step01_workspace = Path(args.step01_workspace).expanduser().resolve() if args.step01_workspace else Path("")
    out = Path(args.out).expanduser().resolve()

    input_mode = "legacy_step01_queue" if args.step01_workspace else "db_source_assets"
    preflight = runtime_preflight(db_path=db_path, out_path=out, step01_workspace=step01_workspace, input_mode=input_mode)
    if args.preflight_only:
        print(json.dumps({"validation_status": "PASS" if not preflight.get("blockers") else "BLOCKED_PREFLIGHT", "runtime_preflight": preflight}, ensure_ascii=False, indent=2))
        return 0 if not preflight.get("blockers") else 2
    if preflight.get("blockers"):
        print(json.dumps({"validation_status": "BLOCKED_PREFLIGHT", "runtime_preflight": preflight}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    if args.db_audit_only:
        print(json.dumps(audit_project_db_for_step02(db_path, preflight=preflight), ensure_ascii=False, indent=2))
        return 0

    queue_path = Path("")
    if args.step01_workspace:
        queue_path = step01_workspace / "queues" / "process_queue_image.jsonl"
        if not queue_path.exists():
            raise SystemExit(f"Step01 image queue not found: {queue_path}")

    dirs = make_dirs(out, run_invocation_id)
    terminal_log_info = setup_internal_terminal_log(
        dirs, run_invocation_id, run_phase, enabled=not args.disable_internal_terminal_log
    )
    conn = init_state(dirs["state"] / "step02_2_image_preview_state.sqlite", reset=args.reset_state)
    telemetry = TelemetrySampler(dirs["telemetry"], dirs["telemetry_api"], run_invocation_id, run_phase, args.telemetry_interval)
    write_json(dirs["telemetry_api"] / "step02_2_resource_monitor_contract.json", {
        "contract": "step02_2_resource_monitor_contract_v1",
        "producer": SCRIPT_VERSION,
        "latest_status_json": str(dirs["telemetry_api"] / "step02_2_resource_status_latest.json"),
        "fields": ["run_invocation_id", "run_phase", "elapsed_seconds", "sips_pid_count", "qlmanage_pid_count", "process_cpu_cores_estimated", "process_rss_mb_sum"],
        "policy": "record_only_for_future_scheduler_api_no_auto_scaling_in_this_step",
    })

    started = time.perf_counter()
    has_exiftool = exiftool_available()

    print("== step02_2_image_preview_from_step01_queue start ==", flush=True)
    print(f"script_version={SCRIPT_VERSION}", flush=True)
    print(f"step01_workspace={step01_workspace}", flush=True)
    print(f"image_queue={queue_path}", flush=True)
    print(f"out={out}", flush=True)
    print(f"sips_concurrency={args.sips_concurrency} ql_concurrency={args.ql_concurrency} limit_new={args.limit_new}", flush=True)
    if terminal_log_info.get("terminal_log_enabled"):
        print(f"terminal_log_history={terminal_log_info.get('terminal_log_history')}", flush=True)
        print(f"terminal_log_latest={terminal_log_info.get('terminal_log_latest')}", flush=True)

    t_queue = time.perf_counter()
    if input_mode == "db_source_assets":
        queue_rows = fetch_image_queue_rows_from_db(db_path, limit=0)
        generated_queue_path = dirs["manifests"] / "db_image_source_assets_input_queue.jsonl"
        write_jsonl(generated_queue_path, queue_rows)
        queue_path = generated_queue_path
        step01_workspace = Path("")
    else:
        queue_rows = read_jsonl(queue_path)
    items = build_items(queue_rows)
    queue_elapsed = time.perf_counter() - t_queue
    print(f"input read done: input_mode={input_mode} queue_rows={len(queue_rows)} eligible_images={len(items)} elapsed={queue_elapsed:.3f}s", flush=True)

    group_rows, timelapse_rows, keyframe_rows, seq_info, member_ids, keyframe_ids, slow_fallback_group_count, detect_elapsed = detect_timelapse(items, has_exiftool)
    print(f"timelapse detection done: sequences={len(timelapse_rows)} keyframes={len(keyframe_rows)} elapsed={detect_elapsed:.3f}s", flush=True)

    decisions, all_jobs = plan_jobs(items, dirs, seq_info, member_ids, keyframe_ids)

    preview_rows: List[dict] = []
    jobs_to_run: List[dict] = []
    skipped_existing = 0
    limited = 0
    new_started = 0
    for job in all_jobs:
        skip = state_success_valid(conn, job)
        if skip:
            preview_rows.append(skip)
            skipped_existing += 1
            continue
        if args.limit_new and new_started >= args.limit_new:
            limited += 1
            continue
        job["task_action"] = "process_new" if not get_state(conn, job["preview_artifact_id"]) else "retry_from_previous_failure"
        jobs_to_run.append(job)
        new_started += 1

    print(f"preview plan: total_jobs={len(all_jobs)} skip_existing={skipped_existing} process_new_or_retry={len(jobs_to_run)} not_started_limit={limited}", flush=True)

    telemetry.start()
    preview_t0 = time.perf_counter()
    sips_jobs = [j for j in jobs_to_run if j["backend"] == "sips_direct_jpg"]
    ql_jobs = [j for j in jobs_to_run if j["backend"] == "system_preview_then_jpg"]

    with ThreadPoolExecutor(max_workers=max(1, args.sips_concurrency)) as sips_pool, ThreadPoolExecutor(max_workers=max(1, args.ql_concurrency)) as ql_pool:
        future_map = {}
        for j in sips_jobs:
            future_map[sips_pool.submit(run_sips_job, j)] = j
        for j in ql_jobs:
            future_map[ql_pool.submit(run_ql_job, j, dirs["tmp"])] = j
        done = 0
        total = len(future_map)
        for fut in as_completed(future_map):
            try:
                row = fut.result()
            except Exception as e:
                j = future_map[fut]
                row = {**j, "status": "failed", "validation_reason": "exception", "stderr_tail": repr(e), "output_sha256": ""}
            preview_rows.append(row)
            upsert_state(conn, row)
            done += 1
            if done % 100 == 0 or done == total:
                print(f"preview completed {done}/{total}", flush=True)
    preview_elapsed = time.perf_counter() - preview_t0
    telemetry.sample("finalizing_reports")
    telemetry.stop()

    preview_rows.sort(key=lambda r: (str(r.get("source_relative_path")), str(r.get("preview_artifact_id"))))

    # Visual units: successful previews only, for YOLOE / embedding / Qwen-VL input.
    visual_rows = []
    for r in preview_rows:
        if r.get("status") != "success":
            continue
        visual_rows.append({
            "visual_unit_id": "vu_" + sha256_text(f"visual_unit_v1|{r.get('preview_artifact_id')}|{r.get('output_sha256')}", 24),
            "visual_unit_type": "image_preview",
            "visual_file": r.get("output_path"),
            "visual_file_sha256": r.get("output_sha256"),
            "visual_width": r.get("width"),
            "visual_height": r.get("height"),
            "preview_artifact_id": r.get("preview_artifact_id"),
            "preview_role": r.get("preview_role"),
            "producer_step": "step02_2_image_preview",
            "producer_version": SCRIPT_VERSION,
            "parent_source_file_id": r.get("parent_source_file_id"),
            "parent_source_content_id": r.get("parent_source_content_id"),
            "parent_source_path_at_processing_time": r.get("parent_source_path_at_processing_time"),
            "parent_media_kind": "image",
            "source_relative_path": r.get("source_relative_path"),
            "time_position_ms": "",
            "sequence_id": r.get("sequence_id"),
            "representative_position": r.get("representative_position"),
        })

    # Latest + history outputs.
    prefix = f"{run_invocation_id}_step02_2_image_preview_{run_phase}"
    manifest_pref = [
        "preview_artifact_id", "parent_source_file_id", "parent_source_content_id", "parent_source_path_at_processing_time", "parent_media_kind",
        "source_relative_path", "extension", "preview_role", "backend", "task_action", "status", "output_path", "output_sha256", "width", "height", "validation_reason",
        "sequence_id", "representative_position", "total_task_elapsed_seconds", "stderr_tail",
    ]
    write_csv(dirs["manifests"] / "image_preview_manifest.csv", preview_rows, manifest_pref)
    write_csv(dirs["manifests_history"] / f"{prefix}_image_preview_manifest.csv", preview_rows, manifest_pref)
    write_csv(dirs["manifests"] / "image_preview_decision_manifest.csv", decisions)
    write_csv(dirs["manifests_history"] / f"{prefix}_image_preview_decision_manifest.csv", decisions)
    write_csv(dirs["manifests"] / "image_timelapse_sequences.csv", timelapse_rows)
    write_csv(dirs["manifests_history"] / f"{prefix}_image_timelapse_sequences.csv", timelapse_rows)
    write_csv(dirs["manifests"] / "image_group_timelapse_diagnosis.csv", group_rows)
    write_csv(dirs["manifests"] / "image_preview_visual_unit_manifest.csv", visual_rows)
    write_csv(dirs["manifests_history"] / f"{prefix}_image_preview_visual_unit_manifest.csv", visual_rows)
    write_jsonl(dirs["manifests"] / "image_preview_visual_unit_manifest.jsonl", visual_rows)
    write_jsonl(dirs["manifests_history"] / f"{prefix}_image_preview_visual_unit_manifest.jsonl", visual_rows)

    db_write_summary = {}
    if input_mode == "db_source_assets":
        db_write_summary = write_step02_image_outputs_to_project_db(db_path, run_invocation_id, preview_rows, visual_rows)
        print("DB_WRITE_SUMMARY=" + json.dumps(db_write_summary, ensure_ascii=False, sort_keys=True), flush=True)

    if telemetry.rows:
        tele_csv = dirs["telemetry_history"] / f"{prefix}_resource_samples.csv"
        write_csv(tele_csv, telemetry.rows)
        write_jsonl(dirs["telemetry_history"] / f"{prefix}_resource_samples.jsonl", telemetry.rows)
        write_csv(dirs["telemetry"] / "step02_2_image_preview_resource_samples_latest.csv", telemetry.rows)
        write_jsonl(dirs["telemetry"] / "step02_2_image_preview_resource_samples_latest.jsonl", telemetry.rows)
    else:
        tele_csv = ""

    status_counts = Counter(r.get("status") for r in preview_rows)
    role_counts = Counter(r.get("preview_role") for r in preview_rows)
    backend_counts = Counter(r.get("backend") for r in preview_rows)
    action_counts = Counter(r.get("task_action") for r in preview_rows)
    elapsed_total = time.perf_counter() - started
    task_elapsed = sum(float(r.get("total_task_elapsed_seconds") or 0) for r in preview_rows)

    summary = {
        "script_version": SCRIPT_VERSION,
        "scheme": SCHEME,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "visual_unit_schema_version": VISUAL_UNIT_SCHEMA_VERSION,
        "run_invocation_id": run_invocation_id,
        "run_phase": run_phase,
        "input_mode": input_mode,
        "db_path": str(db_path),
        "step01_workspace": str(step01_workspace),
        "step01_image_queue": str(queue_path),
        "output_dir": str(out),
        "source_safety": "source_read_only_no_write_no_move_no_delete_no_rename",
        "runtime_preflight": preflight,
        "input_policy": "primary_db_source_assets_fallback_legacy_step01_queue",
        "checkpoint_policy": "sqlite_image_preview_checkpoint_written_after_each_completed_preview",
        "lineage_policy": "every_preview_and_visual_unit_contains_parent_source_file_id_and_parent_source_content_id",
        "sips_concurrency": args.sips_concurrency,
        "ql_concurrency": args.ql_concurrency,
        "limit_new": args.limit_new,
        "step01_image_queue_total_rows": len(queue_rows),
        "eligible_image_count": len(items),
        "preview_jobs_total_planned": len(all_jobs),
        "preview_rows_this_report": len(preview_rows),
        "visual_unit_count": len(visual_rows),
        "action_counts": dict(action_counts),
        "status_counts": dict(status_counts),
        "preview_role_counts": dict(role_counts),
        "preview_backend_counts": dict(backend_counts),
        "timelapse_sequence_count": len(timelapse_rows),
        "timelapse_keyframe_count": len(keyframe_rows),
        "timelapse_member_skipped_count": sum(1 for d in decisions if d.get("preview_role") == "timelapse_member_skipped"),
        "normal_image_count": sum(1 for d in decisions if d.get("preview_role") == "normal_image"),
        "slow_time_fallback_group_count": slow_fallback_group_count,
        "stage_elapsed_seconds": {
            "queue_read_and_prepare": round(queue_elapsed, 3),
            "timelapse_detection": round(detect_elapsed, 3),
            "preview_generation_wall": round(preview_elapsed, 3),
            "preview_task_elapsed_sum": round(task_elapsed, 3),
            "total_task_wall": round(elapsed_total, 3),
        },
        "directory_contract": {
            "derived_image_previews": str(dirs["derived"]),
            "normal_preview_pool_jpg": str(dirs["normal"]),
            "keyframe_pool_jpg": str(dirs["keyframes"]),
            "manifests": str(dirs["manifests"]),
            "state": str(dirs["state"]),
            "telemetry": str(dirs["telemetry"]),
            "logs": str(dirs["logs"]),
            "final_report": str(dirs["final_report"]),
        },
        "image_preview_manifest_csv": str(dirs["manifests"] / "image_preview_manifest.csv"),
        "visual_unit_manifest_csv": str(dirs["manifests"] / "image_preview_visual_unit_manifest.csv"),
        "visual_unit_manifest_jsonl": str(dirs["manifests"] / "image_preview_visual_unit_manifest.jsonl"),
        "telemetry_resource_samples_csv": str(tele_csv),
        "terminal_log_history": terminal_log_info.get("terminal_log_history", ""),
        "terminal_log_latest": terminal_log_info.get("terminal_log_latest", ""),
        "terminal_log_policy": "canonical_terminal_log_is_written_inside_this_step02_2_output_logs_directory",
        "manual_visual_check_required": True,
        "db_write_summary": db_write_summary,
        "downstream_ready_for": ["YOLOE", "Qwen-VL", "embedding"],
    }

    write_json(dirs["final_report"] / "step02_2_image_preview_final_report_latest.json", summary)
    write_json(dirs["final_report_history"] / f"{prefix}_final_report.json", summary)

    md = (
        "# Step02-2 Image Preview Final Report\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- run_invocation_id: {run_invocation_id}\n"
        f"- run_phase: {run_phase}\n"
        f"- input_mode: {input_mode}\n"
        f"- db_path: `{db_path}`\n"
        f"- step01_workspace: `{step01_workspace}`\n"
        f"- output_dir: `{out}`\n"
        f"- limit_new: {args.limit_new}\n"
        f"- eligible_image_count: {len(items)}\n"
        f"- preview_jobs_total_planned: {len(all_jobs)}\n"
        f"- action_counts: {dict(action_counts)}\n"
        f"- status_counts: {dict(status_counts)}\n"
        f"- preview_role_counts: {dict(role_counts)}\n"
        f"- preview_backend_counts: {dict(backend_counts)}\n"
        f"- timelapse_sequence_count: {len(timelapse_rows)}\n"
        f"- timelapse_keyframe_count: {len(keyframe_rows)}\n"
        f"- visual_unit_count: {len(visual_rows)}\n"
        f"- sips_concurrency: {args.sips_concurrency}\n"
        f"- ql_concurrency: {args.ql_concurrency}\n"
        f"- telemetry_api_status: `{dirs['telemetry_api'] / 'step02_2_resource_status_latest.json'}`\n"
        f"- visual_unit_manifest_jsonl: `{dirs['manifests'] / 'image_preview_visual_unit_manifest.jsonl'}`\n\n"
        "## Resume command\n\n"
        "```bash\n"
        f"python3 step02_2_image_preview_from_step01_queue.py --step01-workspace '{step01_workspace}' --out '{out}' --sips-concurrency {args.sips_concurrency} --ql-concurrency {args.ql_concurrency}\n"
        "```\n"
    )
    (dirs["final_report"] / "step02_2_image_preview_final_report_latest.md").write_text(md, encoding="utf-8")
    (dirs["final_report_history"] / f"{prefix}_final_report.md").write_text(md, encoding="utf-8")

    shutil.rmtree(dirs["tmp"], ignore_errors=True)

    print("== step02_2_image_preview_from_step01_queue finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.no_open:
        subprocess.run(["open", str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
