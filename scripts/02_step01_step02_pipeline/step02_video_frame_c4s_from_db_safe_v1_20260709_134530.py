#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step02 C4S-ID-INC Video Frames From Step01 Queue
================================================

定位：
- 本地素材大整理项目 Step02 / 视频抽帧适配版。
- 改造自 C4S-ID-INC 视频抽帧强关联增量断点逻辑。
- 输入不再是递归扫描视频目录，而是读取 Step01 生成的：
    queues/process_queue_video.jsonl

核心能力：
- 只处理 Step01 canonical 视频队列。
- 重复 alias 不会进入本脚本。
- 每个视频仍生成 source_video_id：size + head 1MB + tail 1MB sha256 前 24 位。
- 每个帧仍生成 frame_id 与 frame_file_sha256。
- 保留 SQLite 增量断点：成功跳过、失败重试、running/interrupted 重试。
- 新增 Step01 强父级字段：
    parent_source_file_id
    parent_source_content_id
    parent_source_path_at_processing_time
    parent_media_kind

停止策略：
- --limit-new 15 表示“本次最多启动 15 个未完成视频任务”。
- 不强杀已经启动的 ffmpeg。
- 四路并发下，已启动任务全部完成后结束；不再启动第 16 个。
- 第二次用同一个 --out 继续运行时，已完成的视频会跳过，只处理剩余未完成视频。

目录结构：
    OUT/
      derived/video_frames/c4s_id_incremental_jpg1280/
      manifests/video_frame_c4s_step01_queue_manifest.csv
      manifests/video_extract_c4s_step01_queue_report.csv
      manifests/source_video_identity_step01_queue.csv
      reports/video_frame_c4s_step01_queue_summary.json
      reports/video_frame_c4s_step01_queue_summary.md
      identity/source_videos/vid_<source_video_id>.json
      state/video_frame_c4s_step01_queue_state.sqlite
      telemetry/history/<run_invocation_id>_step02_video_frame_<phase>_resource_samples.csv
      telemetry/history/<run_invocation_id>_step02_video_frame_<phase>_per_video_timing.csv
      telemetry/step02_video_frame_performance_summary_latest.json
      telemetry/step02_video_frame_performance_summary_latest.md
      final_report/step02_video_frame_final_report_latest.json
      final_report/step02_video_frame_final_report_latest.md

用法：
    python3 step02_video_frame_c4s_id_from_step01_queue.py \
      --step01-workspace /path/to/step01_workspace \
      --out /path/to/step02_output \
      --limit-new 15

继续剩余：
    python3 step02_video_frame_c4s_id_from_step01_queue.py \
      --step01-workspace /path/to/step01_workspace \
      --out /path/to/step02_output

注意：
- 继续运行必须使用同一个 --out，否则 SQLite 状态账本不在同一处，无法验证跳过。
- 本脚本只读原始素材，不移动、不删除、不重命名源文件。
"""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ============================================================
# 1. 固定参数
# ============================================================
SCRIPT_VERSION = "step02_video_frame_c4s_id_from_db_safe_v1_20260709_134530"

DEFAULT_CONCURRENCY = 4
MAX_EDGE_PX = 1280
SAMPLING_OFFSET_MS = 2000
SAMPLING_INTERVAL_MS = 3000
SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MS = 1500
SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MAX_DURATION_MS = 5000
SHORT_VIDEO_SINGLE_FRAME_MIN_DURATION_MS = 500
LAST_FRAME_TOLERANCE_MISSING_COUNT = 1
FINGERPRINT_CHUNK_BYTES = 1024 * 1024

SOURCE_VIDEO_ID_VERSION = "source_video_id_v1_size_head1mb_tail1mb_sha256_24"
FRAME_ID_VERSION = "frame_id_v1_source_video_id_index_estimated_time_sampling_jpg1280_sha256_24"
FRAME_FILE_SHA256_VERSION = "frame_file_sha256_full_file_content"
ARTIFACT_SCHEMA_VERSION = "video_frame_c4s_step01_queue_manifest_v1"
SCRIPT_SCHEME = "step02_video_frame_c4s_id_from_step01_queue_offset2000_interval3000_jpg1280_plus_short1500_single_frame_fallback_v1"

SUCCESS_STATUSES = {
    "success",
    "success_with_software_fallback",
    "success_with_tolerated_tail_boundary_difference",
    "success_with_software_fallback_and_tolerated_tail_boundary_difference",
    "success_with_lower_than_estimate",
    "success_with_software_fallback_and_lower_than_estimate",
    "success_duration_unknown",
    "success_duration_unknown_with_software_fallback",
    "skipped_too_short_for_sampling_offset",
    "success_with_short_video_single_frame_fallback",
    "success_with_short_video_single_frame_software_fallback",
}

RETRYABLE_STATUSES = {
    "queued",
    "running",
    "interrupted_needs_retry",
    "frame_extract_failed",
    "frame_extract_failed_after_fallback",
    "frame_extract_failed_no_frames",
    "needs_review_too_few_frames",
    "frame_extract_failed_exception",
    "short_video_single_frame_fallback_failed",
}


# These globals are initialized in main().
OUT = None
FRAME_OUT = None
MANIFEST_DIR = None
REPORT_DIR = None
IDENTITY_DIR = None
STATE_DIR = None
SUMMARY_JSON = None
SUMMARY_MD = None
MANIFEST_CSV = None
VIDEO_REPORT_CSV = None
SOURCE_IDENTITY_CSV = None
STATE_DB = None
CONCURRENCY = DEFAULT_CONCURRENCY


def sampling_contract():
    return {
        "scheme": SCRIPT_SCHEME,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "source_video_id_version": SOURCE_VIDEO_ID_VERSION,
        "frame_id_version": FRAME_ID_VERSION,
        "frame_file_sha256_version": FRAME_FILE_SHA256_VERSION,
        "sampling_offset_ms": SAMPLING_OFFSET_MS,
        "sampling_interval_ms": SAMPLING_INTERVAL_MS,
        "max_edge_px": MAX_EDGE_PX,
        "format": "jpg",
        "ffmpeg_jpeg_quality_arg": "-q:v 3",
        "decode_mode_primary": "videotoolbox",
        "fallback_decode_mode": "software",
        "tail_boundary_policy": "missing_one_expected_tail_frame_is_tolerated_and_recorded",
        "short_video_policy": "if standard c4s produces no frames and duration <= 5s, try one frame at 1500ms; if duration < 1500ms use midpoint when >=500ms",
        "short_video_single_frame_fallback_ms": SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MS,
        "short_video_single_frame_fallback_max_duration_ms": SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MAX_DURATION_MS,
        "input_policy": "read_step01_queues_process_queue_video_jsonl_only",
    }


SAMPLING_CONTRACT = sampling_contract()
SAMPLING_CONTRACT_ID = hashlib.sha256(
    json.dumps(SAMPLING_CONTRACT, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:24]


# ============================================================
# 2. 通用工具
# ============================================================
def utc_now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def local_run_id():
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def run_cmd(cmd, timeout=None):
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:
        return -1, "", str(exc)


def require_tool(name: str):
    rc, out, err = run_cmd(["bash", "-lc", f"command -v {name}"], timeout=5)
    if rc != 0 or not out.strip():
        raise SystemExit(f"[错误] 未找到依赖工具：{name}")


def read_jsonl(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as exc:
                raise SystemExit(f"[错误] JSONL 解析失败: {path}:{line_no}: {exc}")
    return rows


def safe_component(text: str, max_len=140):
    text = str(text or "")
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in text)
    if len(stem) > max_len:
        stem = stem[-max_len:]
    return stem or "empty"


def sha256_file(path: Path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def source_video_fingerprint(src: Path):
    st = src.stat()
    size = st.st_size
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))

    h = hashlib.sha256()
    h.update(str(size).encode("utf-8"))
    with src.open("rb") as f:
        head = f.read(FINGERPRINT_CHUNK_BYTES)
        h.update(head)
        if size > FINGERPRINT_CHUNK_BYTES:
            f.seek(max(size - FINGERPRINT_CHUNK_BYTES, 0))
            tail = f.read(FINGERPRINT_CHUNK_BYTES)
            h.update(tail)

    digest = h.hexdigest()
    return {
        "source_video_id": digest[:24],
        "source_video_fingerprint_sha256": digest,
        "source_file_size_bytes": size,
        "source_file_mtime_ns": mtime_ns,
    }


def make_video_key(source_video_id: str):
    return f"{source_video_id}|{SAMPLING_CONTRACT_ID}"


def stable_frame_id(source_video_id: str, frame_index: int, estimated_frame_time_ms: int):
    raw = (
        f"source_video_id={source_video_id}|"
        f"frame_index={frame_index}|"
        f"estimated_frame_time_ms={estimated_frame_time_ms}|"
        f"contract_id={SAMPLING_CONTRACT_ID}|"
        f"offset_ms={SAMPLING_OFFSET_MS}|"
        f"interval_ms={SAMPLING_INTERVAL_MS}|"
        f"max_edge_px={MAX_EDGE_PX}|"
        f"format=jpg"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def ffprobe_duration(src: Path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    rc, out, err = run_cmd(cmd, timeout=60)
    if rc != 0:
        return None, err[-800:]
    try:
        return float(out.strip()), ""
    except Exception:
        return None, f"bad duration output: {out[-800:]}"


def estimate_expected_count(duration_seconds):
    if duration_seconds is None or duration_seconds <= 0:
        return None
    start = SAMPLING_OFFSET_MS / 1000
    interval = SAMPLING_INTERVAL_MS / 1000
    if duration_seconds < start:
        return 0
    return int(math.floor((duration_seconds - start) / interval)) + 1


def validate_jpg(path: Path):
    if not path.exists() or path.stat().st_size <= 0:
        return False, None, None

    rc, out, err = run_cmd(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], timeout=10)
    if rc != 0:
        return False, None, None

    width = None
    height = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            try:
                width = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line.startswith("pixelHeight:"):
            try:
                height = int(line.split(":", 1)[1].strip())
            except Exception:
                pass

    ok = bool(width and height and max(width, height) <= MAX_EDGE_PX)
    return ok, width, height


def validate_jpg_dir(out_dir: Path):
    jpgs = sorted(out_dir.glob("*.jpg"))
    valid_count = 0
    invalid_count = 0
    dimension_rows = []

    for i, jpg in enumerate(jpgs, start=1):
        ok, width, height = validate_jpg(jpg)
        if ok:
            valid_count += 1
        else:
            invalid_count += 1
        dimension_rows.append((jpg, i, ok, width, height))

    return {
        "jpgs": jpgs,
        "produced_count": len(jpgs),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "dimension_rows": dimension_rows,
    }


def clear_partial_frames(out_dir: Path):
    if not out_dir.exists():
        return
    for p in out_dir.glob("*.jpg"):
        try:
            p.unlink()
        except Exception:
            pass


def build_ffmpeg_cmd(src: Path, pattern: Path, vf: str, decode_mode: str):
    if decode_mode == "videotoolbox":
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-hwaccel", "videotoolbox",
            "-i", str(src),
            "-map", "0:v:0",
            "-an", "-sn", "-dn",
            "-vf", vf,
            "-q:v", "3",
            str(pattern),
        ]

    if decode_mode == "software":
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-i", str(src),
            "-map", "0:v:0",
            "-an", "-sn", "-dn",
            "-vf", vf,
            "-q:v", "3",
            str(pattern),
        ]

    raise ValueError(f"unknown decode_mode: {decode_mode}")


def run_single_decode_attempt(src: Path, out_dir: Path, pattern: Path, vf: str, decode_mode: str):
    cmd = build_ffmpeg_cmd(src, pattern, vf, decode_mode)
    t0 = time.perf_counter()
    rc, stdout, stderr = run_cmd(cmd, timeout=None)
    elapsed = time.perf_counter() - t0
    validation = validate_jpg_dir(out_dir)

    success = (
        rc == 0
        and validation["produced_count"] > 0
        and validation["valid_count"] > 0
    )

    return {
        "decode_mode": decode_mode,
        "success": success,
        "returncode": rc,
        "elapsed_seconds": round(elapsed, 3),
        "produced_count": validation["produced_count"],
        "valid_count": validation["valid_count"],
        "invalid_count": validation["invalid_count"],
        "stderr_tail": (stderr or "")[-1500:].replace("\n", " "),
    }


def run_extract_with_fallback(src: Path, out_dir: Path, pattern: Path, vf: str):
    attempts = []

    clear_partial_frames(out_dir)
    first = run_single_decode_attempt(src, out_dir, pattern, vf, "videotoolbox")
    attempts.append(first)
    if first["success"]:
        return {
            "final_decode_mode": "videotoolbox",
            "extract_mode": "standard_c4s",
            "fallback_attempted": False,
            "short_video_single_frame_fallback_attempted": False,
            "short_video_single_frame_fallback_success": False,
            "short_video_single_frame_fallback_time_ms": "",
            "frame_time_ms_list": None,
            "decode_attempts": attempts,
            "ffmpeg_returncode": first["returncode"],
            "ffmpeg_extract_elapsed_seconds": first["elapsed_seconds"],
            "ffmpeg_stderr_tail": first["stderr_tail"],
        }

    clear_partial_frames(out_dir)
    second = run_single_decode_attempt(src, out_dir, pattern, vf, "software")
    attempts.append(second)
    if second["success"]:
        return {
            "final_decode_mode": "software",
            "extract_mode": "standard_c4s",
            "fallback_attempted": True,
            "short_video_single_frame_fallback_attempted": False,
            "short_video_single_frame_fallback_success": False,
            "short_video_single_frame_fallback_time_ms": "",
            "frame_time_ms_list": None,
            "decode_attempts": attempts,
            "ffmpeg_returncode": second["returncode"],
            "ffmpeg_extract_elapsed_seconds": round(sum(a["elapsed_seconds"] for a in attempts), 3),
            "ffmpeg_stderr_tail": second["stderr_tail"],
        }

    return {
        "final_decode_mode": "failed",
        "extract_mode": "standard_c4s",
        "fallback_attempted": True,
        "short_video_single_frame_fallback_attempted": False,
        "short_video_single_frame_fallback_success": False,
        "short_video_single_frame_fallback_time_ms": "",
        "frame_time_ms_list": None,
        "decode_attempts": attempts,
        "ffmpeg_returncode": second["returncode"],
        "ffmpeg_extract_elapsed_seconds": round(sum(a["elapsed_seconds"] for a in attempts), 3),
        "ffmpeg_stderr_tail": second["stderr_tail"],
    }


def choose_short_video_single_frame_fallback_time_ms(duration_seconds):
    if duration_seconds is None or duration_seconds <= 0:
        return None
    duration_ms = int(duration_seconds * 1000)
    if duration_ms < SHORT_VIDEO_SINGLE_FRAME_MIN_DURATION_MS:
        return None
    if duration_ms >= SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MS:
        return SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MS
    return max(300, duration_ms // 2)


def build_single_frame_cmd(src: Path, output_file: Path, time_ms: int, decode_mode: str):
    vf = f"scale='if(gt(iw,ih),{MAX_EDGE_PX},-2)':'if(gt(ih,iw),{MAX_EDGE_PX},-2)'"
    seek = f"{time_ms / 1000:.3f}"
    if decode_mode == "videotoolbox":
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-hwaccel", "videotoolbox",
            "-i", str(src),
            "-ss", seek,
            "-map", "0:v:0",
            "-an", "-sn", "-dn",
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "3",
            str(output_file),
        ]
    if decode_mode == "software":
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-i", str(src),
            "-ss", seek,
            "-map", "0:v:0",
            "-an", "-sn", "-dn",
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "3",
            str(output_file),
        ]
    raise ValueError(f"unknown decode_mode: {decode_mode}")


def run_single_frame_attempt(src: Path, out_dir: Path, time_ms: int, decode_mode: str):
    output_file = out_dir / "frame_000001.jpg"
    cmd = build_single_frame_cmd(src, output_file, time_ms, decode_mode)
    t0 = time.perf_counter()
    rc, stdout, stderr = run_cmd(cmd, timeout=None)
    elapsed = time.perf_counter() - t0
    validation = validate_jpg_dir(out_dir)
    success = (rc == 0 and validation["produced_count"] > 0 and validation["valid_count"] > 0)
    return {
        "decode_mode": f"short_single_frame_{decode_mode}",
        "success": success,
        "returncode": rc,
        "elapsed_seconds": round(elapsed, 3),
        "produced_count": validation["produced_count"],
        "valid_count": validation["valid_count"],
        "invalid_count": validation["invalid_count"],
        "short_video_single_frame_time_ms": time_ms,
        "stderr_tail": (stderr or "")[-1500:].replace("\n", " "),
    }


def run_short_video_single_frame_fallback(src: Path, out_dir: Path, duration_seconds, previous_extract_result):
    time_ms = choose_short_video_single_frame_fallback_time_ms(duration_seconds)
    if time_ms is None:
        result = dict(previous_extract_result)
        result["short_video_single_frame_fallback_attempted"] = False
        result["short_video_single_frame_fallback_success"] = False
        result["short_video_single_frame_fallback_time_ms"] = ""
        return result

    attempts = list(previous_extract_result.get("decode_attempts") or [])

    clear_partial_frames(out_dir)
    first = run_single_frame_attempt(src, out_dir, time_ms, "videotoolbox")
    attempts.append(first)
    if first["success"]:
        return {
            "final_decode_mode": "videotoolbox",
            "extract_mode": "short_video_single_frame_1500ms",
            "fallback_attempted": bool(previous_extract_result.get("fallback_attempted")),
            "short_video_single_frame_fallback_attempted": True,
            "short_video_single_frame_fallback_success": True,
            "short_video_single_frame_fallback_time_ms": time_ms,
            "frame_time_ms_list": [time_ms],
            "decode_attempts": attempts,
            "ffmpeg_returncode": first["returncode"],
            "ffmpeg_extract_elapsed_seconds": round(sum(a.get("elapsed_seconds", 0) for a in attempts), 3),
            "ffmpeg_stderr_tail": first["stderr_tail"],
        }

    clear_partial_frames(out_dir)
    second = run_single_frame_attempt(src, out_dir, time_ms, "software")
    attempts.append(second)
    if second["success"]:
        return {
            "final_decode_mode": "software",
            "extract_mode": "short_video_single_frame_1500ms",
            "fallback_attempted": True,
            "short_video_single_frame_fallback_attempted": True,
            "short_video_single_frame_fallback_success": True,
            "short_video_single_frame_fallback_time_ms": time_ms,
            "frame_time_ms_list": [time_ms],
            "decode_attempts": attempts,
            "ffmpeg_returncode": second["returncode"],
            "ffmpeg_extract_elapsed_seconds": round(sum(a.get("elapsed_seconds", 0) for a in attempts), 3),
            "ffmpeg_stderr_tail": second["stderr_tail"],
        }

    return {
        "final_decode_mode": "failed",
        "extract_mode": "short_video_single_frame_1500ms",
        "fallback_attempted": True,
        "short_video_single_frame_fallback_attempted": True,
        "short_video_single_frame_fallback_success": False,
        "short_video_single_frame_fallback_time_ms": time_ms,
        "frame_time_ms_list": [time_ms],
        "decode_attempts": attempts,
        "ffmpeg_returncode": second["returncode"],
        "ffmpeg_extract_elapsed_seconds": round(sum(a.get("elapsed_seconds", 0) for a in attempts), 3),
        "ffmpeg_stderr_tail": second["stderr_tail"],
    }


def classify_status(duration_seconds, expected_count, produced_count, valid_count, final_decode_mode, fallback_attempted, extract_mode="standard_c4s"):
    if extract_mode == "short_video_single_frame_1500ms" and final_decode_mode != "failed" and produced_count > 0 and valid_count > 0:
        if final_decode_mode == "software":
            return "success_with_short_video_single_frame_software_fallback", False
        return "success_with_short_video_single_frame_fallback", False

    if expected_count == 0:
        return "skipped_too_short_for_sampling_offset", False

    if extract_mode == "short_video_single_frame_1500ms" and (final_decode_mode == "failed" or produced_count <= 0 or valid_count <= 0):
        return "short_video_single_frame_fallback_failed", False

    if final_decode_mode == "failed" or produced_count <= 0 or valid_count <= 0:
        return "frame_extract_failed_after_fallback" if fallback_attempted else "frame_extract_failed", False

    if expected_count is None:
        return "success_duration_unknown_with_software_fallback" if final_decode_mode == "software" else "success_duration_unknown", False

    diff = expected_count - produced_count
    tail_boundary_tolerated = diff == LAST_FRAME_TOLERANCE_MISSING_COUNT

    if diff <= 0:
        return "success_with_software_fallback" if final_decode_mode == "software" else "success", False

    if tail_boundary_tolerated:
        if final_decode_mode == "software":
            return "success_with_software_fallback_and_tolerated_tail_boundary_difference", True
        return "success_with_tolerated_tail_boundary_difference", True

    if expected_count > 0 and produced_count / expected_count >= 0.70:
        if final_decode_mode == "software":
            return "success_with_software_fallback_and_lower_than_estimate", False
        return "success_with_lower_than_estimate", False

    return "needs_review_too_few_frames", False


def rename_frames_to_strong_ids(out_dir: Path, source_video_id: str, frame_time_ms_list=None):
    validation = validate_jpg_dir(out_dir)
    renamed_rows = []

    for old_path, frame_index, ok, width, height in validation["dimension_rows"]:
        if frame_time_ms_list and frame_index <= len(frame_time_ms_list):
            estimated_frame_time_ms = int(frame_time_ms_list[frame_index - 1])
        else:
            estimated_frame_time_ms = SAMPLING_OFFSET_MS + (frame_index - 1) * SAMPLING_INTERVAL_MS
        frame_id = stable_frame_id(source_video_id, frame_index, estimated_frame_time_ms)
        new_name = f"frm_{frame_id}_idx{frame_index:06d}_t{estimated_frame_time_ms:09d}ms.jpg"
        new_path = out_dir / new_name

        if old_path.name != new_name:
            if new_path.exists():
                new_path.unlink()
            old_path.rename(new_path)

        frame_sha = sha256_file(new_path) if new_path.exists() and new_path.stat().st_size > 0 else ""
        renamed_rows.append({
            "frame_path": new_path,
            "frame_index": frame_index,
            "estimated_frame_time_ms": estimated_frame_time_ms,
            "frame_id": frame_id,
            "frame_file_sha256": frame_sha,
            "is_valid_jpg1280": ok,
            "width": width,
            "height": height,
        })

    valid_count = sum(1 for r in renamed_rows if r["is_valid_jpg1280"])
    invalid_count = len(renamed_rows) - valid_count
    return {
        "rows": renamed_rows,
        "produced_count": len(renamed_rows),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
    }




# ============================================================
# 3B. Project DB adapter: read source_assets, write derived_assets/visual_units/model_runs
# ============================================================
PROJECT_DB_STAGE = "step02_video_frame_c4s"
PROJECT_DB_MODEL_NAME = "local_ffmpeg_video_frame_c4s"
PROJECT_DB_MODEL_PATH = "builtin_local_ffmpeg_ffprobe_sips_no_model"


def stable_project_id(prefix: str, *parts: object, n: int = 24) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p if p is not None else "").encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return f"{prefix}_{h.hexdigest()[:n]}"


def db_table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def db_table_info(conn, table_name: str):
    if not db_table_exists(conn, table_name):
        return []
    return [dict(r) for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]


def db_columns(conn, table_name: str):
    return [r["name"] for r in db_table_info(conn, table_name)]


def ensure_project_db_min_schema(conn):
    """Create only minimal missing project tables. Existing project tables are not rebuilt."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_runs (
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_path TEXT NOT NULL,
            script_version TEXT NOT NULL,
            status TEXT NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0,
            output_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_assets (
            derived_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL,
            derived_type TEXT NOT NULL,
            derived_path TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            sha256 TEXT,
            file_size_bytes INTEGER,
            created_at TEXT,
            updated_at TEXT,
            model_run_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visual_units (
            visual_unit_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL,
            derived_id TEXT NOT NULL,
            visual_file TEXT NOT NULL,
            frame_index INTEGER,
            time_ms INTEGER,
            width INTEGER,
            height INTEGER,
            created_at TEXT,
            model_run_id TEXT
        )
        """
    )
    conn.commit()


def project_db_default_for_column(col: str, values: dict, now: str):
    if col in values:
        return values[col]
    c = col.lower()
    if c in {"created_at", "updated_at", "started_at", "finished_at", "processed_at", "generated_at"}:
        return now
    if c in {"error_message", "message", "note", "notes", "description"}:
        return ""
    if c in {"status"}:
        return values.get("status", "done")
    if c in {"stage"}:
        return values.get("stage", PROJECT_DB_STAGE)
    if c in {"script_version"}:
        return SCRIPT_VERSION
    if c in {"model_name"}:
        return PROJECT_DB_MODEL_NAME
    if c in {"model_path"}:
        return PROJECT_DB_MODEL_PATH
    if c in {"model_version"}:
        return "no_model_builtin_local_tools"
    if c in {"input_count", "output_count", "count", "retry_count"}:
        return int(values.get(c, 0) or 0)
    if c.endswith("_count"):
        return int(values.get(c, 0) or 0)
    if c.endswith("_bytes"):
        return int(values.get(c, 0) or 0)
    return ""


def insert_or_replace_dynamic(conn, table_name: str, values: dict):
    cols = db_columns(conn, table_name)
    if not cols:
        return False
    now = utc_now_iso()
    use_cols = []
    use_vals = []
    for col in cols:
        use_cols.append(col)
        use_vals.append(project_db_default_for_column(col, values, now))
    placeholders = ",".join(["?"] * len(use_cols))
    col_sql = ",".join(use_cols)
    conn.execute(
        f"INSERT OR REPLACE INTO {table_name} ({col_sql}) VALUES ({placeholders})",
        use_vals,
    )
    return True


def insert_model_run_project_db(conn, run_id: str, status: str, input_count: int, output_count: int, error_message: str = ""):
    values = {
        "run_id": run_id,
        "stage": PROJECT_DB_STAGE,
        "model_name": PROJECT_DB_MODEL_NAME,
        "model_path": PROJECT_DB_MODEL_PATH,
        "script_version": SCRIPT_VERSION,
        "status": status,
        "input_count": int(input_count or 0),
        "output_count": int(output_count or 0),
        "error_message": error_message or "",
        "started_at": utc_now_iso(),
        "finished_at": utc_now_iso() if status not in {"running", "queued"} else "",
    }
    insert_or_replace_dynamic(conn, "model_runs", values)
    conn.commit()


def fetch_video_queue_from_project_db(db_path: Path):
    db_path = db_path.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"[错误] 找不到项目数据库: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if not db_table_exists(conn, "source_assets"):
        conn.close()
        raise SystemExit(f"[错误] 项目数据库缺少 source_assets 表: {db_path}")
    cols = db_columns(conn, "source_assets")
    if "media_kind" in cols:
        rows = conn.execute("SELECT * FROM source_assets WHERE lower(COALESCE(media_kind,''))='video'").fetchall()
    else:
        rows = conn.execute("SELECT * FROM source_assets").fetchall()
    rows = sorted(
        rows,
        key=lambda rr: (dict(rr).get("relative_path") or dict(rr).get("absolute_path") or dict(rr).get("source_path") or dict(rr).get("path") or ""),
    )
    queue_rows = []
    for r in rows:
        d = dict(r)
        source_path = d.get("absolute_path") or d.get("source_path") or d.get("path") or d.get("file_path") or ""
        relative_path = d.get("relative_path") or d.get("source_relative_path") or Path(source_path).name
        source_content_id = d.get("source_content_id") or d.get("content_id") or ""
        source_file_id = d.get("source_file_id") or d.get("file_id") or source_content_id
        if not source_path:
            continue
        queue_rows.append({
            "source_path": source_path,
            "source_relative_path": relative_path,
            "source_content_id": source_content_id,
            "source_file_id": source_file_id,
            "media_kind": "video",
            "next_action": "process",
            "dedup_role": d.get("dedup_role") or "canonical_from_source_assets",
        })
    conn.close()
    return queue_rows


def project_db_audit(db_path: Path):
    db_path = db_path.expanduser().resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    counts = {}
    for t in ["source_assets", "derived_assets", "visual_units", "embeddings", "model_runs"]:
        if db_table_exists(conn, t):
            counts[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        else:
            counts[t] = None
    videos = []
    if db_table_exists(conn, "source_assets"):
        cols = db_columns(conn, "source_assets")
        if "media_kind" in cols:
            rows = conn.execute("SELECT * FROM source_assets WHERE lower(COALESCE(media_kind,''))='video'").fetchall()
        else:
            rows = conn.execute("SELECT * FROM source_assets").fetchall()
        rows = sorted(
            rows,
            key=lambda rr: (dict(rr).get("relative_path") or dict(rr).get("absolute_path") or dict(rr).get("source_path") or dict(rr).get("path") or ""),
        )[:5]
        for r in rows:
            d = dict(r)
            videos.append({
                "source_content_id": d.get("source_content_id") or d.get("content_id") or "",
                "relative_path": d.get("relative_path") or d.get("source_relative_path") or "",
                "extension": d.get("extension") or "",
                "absolute_path": d.get("absolute_path") or d.get("source_path") or d.get("path") or "",
            })
    pending = 0
    if db_table_exists(conn, "source_assets"):
        cols = db_columns(conn, "source_assets")
        if "media_kind" in cols:
            pending = conn.execute("SELECT COUNT(*) AS c FROM source_assets WHERE lower(COALESCE(media_kind,''))='video'").fetchone()["c"]
        else:
            pending = conn.execute("SELECT COUNT(*) AS c FROM source_assets").fetchone()["c"]
    conn.close()
    return {
        "script_version": SCRIPT_VERSION,
        "mode": "db_audit_only",
        "db_path": str(db_path),
        "counts": counts,
        "pending_video_source_assets": pending,
        "sample_videos": videos,
    }


def write_step02_video_frames_to_project_db(db_path: Path, run_id: str, video_reports, frame_rows_all):
    db_path = db_path.expanduser().resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_project_db_min_schema(conn)

    input_count = len(video_reports)
    output_count = 0
    row_errors = 0
    insert_model_run_project_db(conn, run_id, "running", input_count, 0, "")

    for r in frame_rows_all:
        try:
            source_content_id = r.get("parent_source_content_id") or r.get("source_content_id") or ""
            if not source_content_id:
                row_errors += 1
                continue
            frame_file = r.get("frame_file") or ""
            frame_id = r.get("frame_id") or stable_project_id("frame", source_content_id, frame_file, r.get("estimated_frame_time_ms", ""))
            sha = r.get("frame_file_sha256") or ""
            derived_id = stable_project_id("der", "video_frame_jpg1280", source_content_id, frame_id, sha)
            visual_unit_id = stable_project_id("vu", "video_frame", source_content_id, frame_id, derived_id)
            width = int(r.get("width") or 0) if str(r.get("width") or "").strip() else None
            height = int(r.get("height") or 0) if str(r.get("height") or "").strip() else None
            frame_index = int(r.get("frame_index") or 0) if str(r.get("frame_index") or "").strip() else None
            time_ms = int(r.get("estimated_frame_time_ms") or 0) if str(r.get("estimated_frame_time_ms") or "").strip() else None
            file_size_bytes = 0
            try:
                if frame_file and Path(frame_file).exists():
                    file_size_bytes = Path(frame_file).stat().st_size
            except Exception:
                file_size_bytes = 0

            derived_values = {
                "derived_id": derived_id,
                "source_content_id": source_content_id,
                "derived_type": "video_frame_jpg1280",
                "derived_path": frame_file,
                "relative_path": r.get("frame_relative_path") or "",
                "width": width,
                "height": height,
                "sha256": sha,
                "file_size_bytes": file_size_bytes,
                "model_run_id": run_id,
                "run_id": run_id,
                "stage": PROJECT_DB_STAGE,
                "script_version": SCRIPT_VERSION,
                "model_name": PROJECT_DB_MODEL_NAME,
                "model_path": PROJECT_DB_MODEL_PATH,
                "metadata_json": json.dumps({
                    "frame_id": frame_id,
                    "source_video_id": r.get("source_video_id") or "",
                    "source_video_fingerprint_sha256": r.get("source_video_fingerprint_sha256") or "",
                    "sampling_contract_id": r.get("sampling_contract_id") or SAMPLING_CONTRACT_ID,
                    "frame_index": frame_index,
                    "estimated_frame_time_ms": time_ms,
                    "frame_file_sha256_version": r.get("frame_file_sha256_version") or FRAME_FILE_SHA256_VERSION,
                    "parent_source_path_at_processing_time": r.get("parent_source_path_at_processing_time") or "",
                    "decode_mode": r.get("decode_mode") or "",
                    "task_action": r.get("task_action") or "",
                    "checkpoint_status": r.get("checkpoint_status") or "",
                }, ensure_ascii=False),
            }
            insert_or_replace_dynamic(conn, "derived_assets", derived_values)

            visual_values = {
                "visual_unit_id": visual_unit_id,
                "source_content_id": source_content_id,
                "derived_id": derived_id,
                "visual_file": frame_file,
                "visual_unit_type": "video_frame",
                "unit_type": "video_frame",
                "media_kind": "video",
                "source_kind": "video",
                "frame_id": frame_id,
                "frame_index": frame_index,
                "time_ms": time_ms,
                "start_time_ms": time_ms,
                "end_time_ms": time_ms,
                "width": width,
                "height": height,
                "model_run_id": run_id,
                "run_id": run_id,
                "stage": PROJECT_DB_STAGE,
                "script_version": SCRIPT_VERSION,
                "metadata_json": derived_values["metadata_json"],
            }
            insert_or_replace_dynamic(conn, "visual_units", visual_values)
            output_count += 1
        except Exception:
            row_errors += 1

    status = "done" if row_errors == 0 else "done_with_db_row_errors"
    insert_model_run_project_db(conn, run_id, status, input_count, output_count, "" if row_errors == 0 else f"db_output_row_errors={row_errors}")
    conn.commit()
    conn.close()
    return {
        "db_path": str(db_path),
        "stage": PROJECT_DB_STAGE,
        "run_id": run_id,
        "status": status,
        "derived_assets_upserted": output_count,
        "visual_units_upserted": output_count,
        "db_output_row_errors": row_errors,
    }


# ============================================================
# 3. SQLite 状态账本
# ============================================================
def open_state_db():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_key TEXT PRIMARY KEY,
            source_video_id TEXT NOT NULL,
            sampling_contract_id TEXT NOT NULL,
            artifact_schema_version TEXT NOT NULL,
            source_video_id_version TEXT NOT NULL,
            source_video_fingerprint_sha256 TEXT NOT NULL,
            source_video_path TEXT NOT NULL,
            source_video_relative_path TEXT NOT NULL,
            source_file_size_bytes INTEGER NOT NULL,
            source_file_mtime_ns INTEGER NOT NULL,
            parent_source_file_id TEXT,
            parent_source_content_id TEXT,
            parent_source_path_at_processing_time TEXT,
            parent_media_kind TEXT,
            status TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            task_action TEXT NOT NULL,
            output_dir TEXT,
            output_relative_dir TEXT,
            duration_seconds REAL,
            expected_frame_count_estimate INTEGER,
            produced_frame_count INTEGER,
            valid_jpg1280_count INTEGER,
            invalid_jpg_count INTEGER,
            missing_expected_frame_count INTEGER,
            tail_boundary_tolerated INTEGER,
            fallback_attempted INTEGER,
            decode_mode TEXT,
            decode_attempts_json TEXT,
            report_json TEXT,
            last_error TEXT,
            first_seen_at TEXT,
            updated_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS frames (
            frame_id TEXT PRIMARY KEY,
            video_key TEXT NOT NULL,
            source_video_id TEXT NOT NULL,
            sampling_contract_id TEXT NOT NULL,
            parent_source_file_id TEXT,
            parent_source_content_id TEXT,
            parent_source_path_at_processing_time TEXT,
            parent_media_kind TEXT,
            frame_index INTEGER NOT NULL,
            estimated_frame_time_ms INTEGER NOT NULL,
            frame_file TEXT NOT NULL,
            frame_relative_path TEXT NOT NULL,
            frame_file_sha256 TEXT NOT NULL,
            is_valid_jpg1280 INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            decode_mode TEXT,
            fallback_attempted INTEGER,
            frame_extract_status_for_video TEXT,
            updated_at TEXT,
            FOREIGN KEY(video_key) REFERENCES videos(video_key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_video_key ON frames(video_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_source_video_id ON videos(source_video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_parent_source_file_id ON videos(parent_source_file_id)")
    conn.commit()
    return conn


def mark_stale_incomplete_as_interrupted(conn):
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE videos
        SET status='interrupted_needs_retry',
            checkpoint_status='interrupted',
            task_action='retry_after_interrupted_checkpoint',
            updated_at=?
        WHERE sampling_contract_id=?
          AND status IN ('queued', 'running')
        """,
        (now, SAMPLING_CONTRACT_ID),
    )
    conn.commit()


def get_video_row(conn, video_key):
    return conn.execute("SELECT * FROM videos WHERE video_key=?", (video_key,)).fetchone()


def verify_existing_artifacts(conn, video_key):
    video = get_video_row(conn, video_key)
    if not video:
        return False, "no_checkpoint"

    if video["status"] not in SUCCESS_STATUSES or video["checkpoint_status"] != "completed":
        return False, f"checkpoint_not_completed:{video['status']}"

    frames = conn.execute("SELECT * FROM frames WHERE video_key=? ORDER BY frame_index", (video_key,)).fetchall()

    # 短视频跳过可以没有帧。
    if video["status"] == "skipped_too_short_for_sampling_offset":
        return True, "completed_short_video_skip"

    if not frames:
        return False, "no_frame_rows_in_checkpoint"

    for fr in frames:
        p = Path(fr["frame_file"])
        if not p.exists():
            return False, f"frame_missing:{p}"
        if p.stat().st_size <= 0:
            return False, f"frame_zero_size:{p}"
        expected_sha = fr["frame_file_sha256"]
        if expected_sha:
            try:
                actual_sha = sha256_file(p)
            except Exception as exc:
                return False, f"frame_sha256_failed:{p}:{exc}"
            if actual_sha != expected_sha:
                return False, f"frame_sha256_mismatch:{p}"
        ok, width, height = validate_jpg(p)
        if not ok:
            return False, f"frame_validation_failed:{p}"

    return True, "checkpoint_and_frames_valid"


def mark_queued(conn, source_base, video_key, output_dir: Path, task_action: str):
    now = utc_now_iso()
    existing = conn.execute("SELECT first_seen_at FROM videos WHERE video_key=?", (video_key,)).fetchone()
    first_seen_at = existing["first_seen_at"] if existing and existing["first_seen_at"] else now
    conn.execute(
        """
        INSERT INTO videos (
            video_key, source_video_id, sampling_contract_id, artifact_schema_version,
            source_video_id_version, source_video_fingerprint_sha256,
            source_video_path, source_video_relative_path,
            source_file_size_bytes, source_file_mtime_ns,
            parent_source_file_id, parent_source_content_id,
            parent_source_path_at_processing_time, parent_media_kind,
            status, checkpoint_status, task_action,
            output_dir, output_relative_dir,
            report_json, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_key) DO UPDATE SET
            source_video_path=excluded.source_video_path,
            source_video_relative_path=excluded.source_video_relative_path,
            source_file_size_bytes=excluded.source_file_size_bytes,
            source_file_mtime_ns=excluded.source_file_mtime_ns,
            parent_source_file_id=excluded.parent_source_file_id,
            parent_source_content_id=excluded.parent_source_content_id,
            parent_source_path_at_processing_time=excluded.parent_source_path_at_processing_time,
            parent_media_kind=excluded.parent_media_kind,
            status=excluded.status,
            checkpoint_status=excluded.checkpoint_status,
            task_action=excluded.task_action,
            output_dir=excluded.output_dir,
            output_relative_dir=excluded.output_relative_dir,
            updated_at=excluded.updated_at
        """,
        (
            video_key,
            source_base["source_video_id"],
            SAMPLING_CONTRACT_ID,
            ARTIFACT_SCHEMA_VERSION,
            SOURCE_VIDEO_ID_VERSION,
            source_base["source_video_fingerprint_sha256"],
            source_base["source_video_path"],
            source_base["source_video_relative_path"],
            source_base["source_file_size_bytes"],
            source_base["source_file_mtime_ns"],
            source_base["parent_source_file_id"],
            source_base["parent_source_content_id"],
            source_base["parent_source_path_at_processing_time"],
            source_base["parent_media_kind"],
            "queued",
            "queued",
            task_action,
            str(output_dir),
            str(output_dir.relative_to(OUT)),
            "",
            first_seen_at,
            now,
        ),
    )
    conn.commit()


def mark_running(conn, video_key):
    now = utc_now_iso()
    conn.execute(
        "UPDATE videos SET status='running', checkpoint_status='running', updated_at=? WHERE video_key=?",
        (now, video_key),
    )
    conn.commit()


def persist_completed_video(conn, video_key, report, frame_rows, source_base):
    now = utc_now_iso()
    status = report["frame_extract_status"]
    checkpoint_status = "completed" if status in SUCCESS_STATUSES else "failed"
    last_error = "" if status in SUCCESS_STATUSES else report.get("ffmpeg_stderr_tail", "")[:1000]
    report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)

    conn.execute("DELETE FROM frames WHERE video_key=?", (video_key,))
    for r in frame_rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO frames (
                frame_id, video_key, source_video_id, sampling_contract_id,
                parent_source_file_id, parent_source_content_id,
                parent_source_path_at_processing_time, parent_media_kind,
                frame_index, estimated_frame_time_ms,
                frame_file, frame_relative_path, frame_file_sha256,
                is_valid_jpg1280, width, height,
                decode_mode, fallback_attempted, frame_extract_status_for_video,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["frame_id"],
                video_key,
                r["source_video_id"],
                SAMPLING_CONTRACT_ID,
                r["parent_source_file_id"],
                r["parent_source_content_id"],
                r["parent_source_path_at_processing_time"],
                r["parent_media_kind"],
                int(r["frame_index"]),
                int(r["estimated_frame_time_ms"]),
                r["frame_file"],
                r["frame_relative_path"],
                r["frame_file_sha256"],
                1 if r["is_valid_jpg1280"] else 0,
                r.get("width"),
                r.get("height"),
                r.get("decode_mode", ""),
                1 if r.get("fallback_attempted") else 0,
                r.get("frame_extract_status_for_video", ""),
                now,
            ),
        )

    existing_first_seen = conn.execute("SELECT first_seen_at FROM videos WHERE video_key=?", (video_key,)).fetchone()
    first_seen_at = existing_first_seen["first_seen_at"] if existing_first_seen and existing_first_seen["first_seen_at"] else now

    conn.execute(
        """
        INSERT INTO videos (
            video_key, source_video_id, sampling_contract_id, artifact_schema_version,
            source_video_id_version, source_video_fingerprint_sha256,
            source_video_path, source_video_relative_path,
            source_file_size_bytes, source_file_mtime_ns,
            parent_source_file_id, parent_source_content_id,
            parent_source_path_at_processing_time, parent_media_kind,
            status, checkpoint_status, task_action,
            output_dir, output_relative_dir,
            duration_seconds, expected_frame_count_estimate,
            produced_frame_count, valid_jpg1280_count, invalid_jpg_count,
            missing_expected_frame_count, tail_boundary_tolerated,
            fallback_attempted, decode_mode, decode_attempts_json,
            report_json, last_error, first_seen_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_key) DO UPDATE SET
            source_video_path=excluded.source_video_path,
            source_video_relative_path=excluded.source_video_relative_path,
            source_file_size_bytes=excluded.source_file_size_bytes,
            source_file_mtime_ns=excluded.source_file_mtime_ns,
            parent_source_file_id=excluded.parent_source_file_id,
            parent_source_content_id=excluded.parent_source_content_id,
            parent_source_path_at_processing_time=excluded.parent_source_path_at_processing_time,
            parent_media_kind=excluded.parent_media_kind,
            status=excluded.status,
            checkpoint_status=excluded.checkpoint_status,
            task_action=excluded.task_action,
            output_dir=excluded.output_dir,
            output_relative_dir=excluded.output_relative_dir,
            duration_seconds=excluded.duration_seconds,
            expected_frame_count_estimate=excluded.expected_frame_count_estimate,
            produced_frame_count=excluded.produced_frame_count,
            valid_jpg1280_count=excluded.valid_jpg1280_count,
            invalid_jpg_count=excluded.invalid_jpg_count,
            missing_expected_frame_count=excluded.missing_expected_frame_count,
            tail_boundary_tolerated=excluded.tail_boundary_tolerated,
            fallback_attempted=excluded.fallback_attempted,
            decode_mode=excluded.decode_mode,
            decode_attempts_json=excluded.decode_attempts_json,
            report_json=excluded.report_json,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at,
            completed_at=excluded.completed_at
        """,
        (
            video_key,
            source_base["source_video_id"],
            SAMPLING_CONTRACT_ID,
            ARTIFACT_SCHEMA_VERSION,
            SOURCE_VIDEO_ID_VERSION,
            source_base["source_video_fingerprint_sha256"],
            source_base["source_video_path"],
            source_base["source_video_relative_path"],
            source_base["source_file_size_bytes"],
            source_base["source_file_mtime_ns"],
            source_base["parent_source_file_id"],
            source_base["parent_source_content_id"],
            source_base["parent_source_path_at_processing_time"],
            source_base["parent_media_kind"],
            status,
            checkpoint_status,
            report.get("task_action", "processed"),
            report.get("frame_output_dir", ""),
            report.get("frame_output_relative_dir", ""),
            float(report["duration_seconds"]) if report.get("duration_seconds") != "" else None,
            int(report["expected_frame_count_estimate"]) if report.get("expected_frame_count_estimate") != "" else None,
            int(report.get("produced_frame_count") or 0),
            int(report.get("valid_jpg1280_count") or 0),
            int(report.get("invalid_jpg_count") or 0),
            int(report["missing_expected_frame_count"]) if report.get("missing_expected_frame_count") != "" else None,
            1 if report.get("tail_boundary_tolerated") else 0,
            1 if report.get("fallback_attempted") else 0,
            report.get("decode_mode", ""),
            report.get("decode_attempts_json", ""),
            report_json,
            last_error,
            first_seen_at,
            now,
            now if checkpoint_status == "completed" else None,
        ),
    )
    conn.commit()


def skipped_rows_from_db(conn, video_key, source_base):
    video = get_video_row(conn, video_key)
    frames = conn.execute("SELECT * FROM frames WHERE video_key=? ORDER BY frame_index", (video_key,)).fetchall()

    report = dict(source_base)
    report.update({
        "duration_seconds": round(video["duration_seconds"], 3) if video["duration_seconds"] is not None else "",
        "expected_frame_count_estimate": video["expected_frame_count_estimate"] if video["expected_frame_count_estimate"] is not None else "",
        "produced_frame_count": video["produced_frame_count"] or 0,
        "valid_jpg1280_count": video["valid_jpg1280_count"] or 0,
        "invalid_jpg_count": video["invalid_jpg_count"] or 0,
        "missing_expected_frame_count": video["missing_expected_frame_count"] if video["missing_expected_frame_count"] is not None else "",
        "tail_boundary_tolerated": bool(video["tail_boundary_tolerated"]),
        "frame_extract_status": video["status"],
        "fallback_attempted": bool(video["fallback_attempted"]),
        "decode_mode": video["decode_mode"] or "",
        "decode_mode_primary": "videotoolbox",
        "fallback_decode_mode": "software",
        "decode_attempts_json": video["decode_attempts_json"] or "",
        "frame_output_dir": video["output_dir"] or "",
        "frame_output_relative_dir": video["output_relative_dir"] or "",
        "task_action": "skip_already_processed",
        "checkpoint_status": "completed",
        "concurrency": CONCURRENCY,
        "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
        "ffmpeg_returncode": "",
        "ffprobe_elapsed_seconds": 0,
        "ffmpeg_extract_elapsed_seconds": 0,
        "validation_elapsed_seconds": 0,
        "frame_sha256_elapsed_seconds": 0,
        "task_elapsed_seconds": 0,
        "ffprobe_error_tail": "",
        "ffmpeg_stderr_tail": "",
    })

    frame_rows = []
    for fr in frames:
        row = dict(source_base)
        row.update({
            "frame_id": fr["frame_id"],
            "frame_id_version": FRAME_ID_VERSION,
            "frame_file_sha256": fr["frame_file_sha256"],
            "frame_file_sha256_version": FRAME_FILE_SHA256_VERSION,
            "frame_file": fr["frame_file"],
            "frame_relative_path": fr["frame_relative_path"],
            "frame_index": fr["frame_index"],
            "estimated_frame_time_ms": fr["estimated_frame_time_ms"],
            "sampling_offset_ms": SAMPLING_OFFSET_MS,
            "sampling_interval_ms": SAMPLING_INTERVAL_MS,
            "sampling_contract_id": SAMPLING_CONTRACT_ID,
            "is_valid_jpg1280": bool(fr["is_valid_jpg1280"]),
            "width": fr["width"],
            "height": fr["height"],
            "decode_mode": fr["decode_mode"],
            "fallback_attempted": bool(fr["fallback_attempted"]),
            "tail_boundary_tolerated_for_video": bool(video["tail_boundary_tolerated"]),
            "frame_extract_status_for_video": fr["frame_extract_status_for_video"],
            "task_action": "skip_already_processed",
            "checkpoint_status": "completed",
            "concurrency": CONCURRENCY,
            "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
        })
        frame_rows.append(row)
    return report, frame_rows


# ============================================================
# 4. Step01 队列适配
# ============================================================
def make_source_base_from_queue_row(row: dict):
    src = Path(row["source_path"]).expanduser().resolve()
    source_identity = source_video_fingerprint(src)
    parent_source_file_id = row.get("source_file_id") or row.get("path_file_id") or ""
    parent_source_content_id = row.get("source_content_id") or ""
    source_relative_path = row.get("source_relative_path") or src.name

    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "source_video_id": source_identity["source_video_id"],
        "source_video_id_version": SOURCE_VIDEO_ID_VERSION,
        "source_video_fingerprint_sha256": source_identity["source_video_fingerprint_sha256"],
        "source_video_path": str(src),
        "source_video_relative_path": source_relative_path,
        "source_file_size_bytes": source_identity["source_file_size_bytes"],
        "source_file_mtime_ns": source_identity["source_file_mtime_ns"],
        "sampling_contract_id": SAMPLING_CONTRACT_ID,
        "parent_source_file_id": parent_source_file_id,
        "parent_source_content_id": parent_source_content_id,
        "parent_source_path_at_processing_time": str(src),
        "parent_media_kind": row.get("media_kind", "video"),
        "step01_source_path": row.get("source_path", ""),
        "step01_source_relative_path": row.get("source_relative_path", ""),
        "step01_dedup_role": row.get("dedup_role", ""),
        "step01_next_action": row.get("next_action", ""),
    }


def output_dir_for_source(source_base, existing_output_dir: str = ""):
    if existing_output_dir:
        p = Path(existing_output_dir)
        try:
            if str(p.resolve()).startswith(str(OUT.resolve())):
                return p
        except Exception:
            pass

    source_video_id = source_base["source_video_id"]
    safe_parent = safe_component(source_base.get("parent_source_file_id", "no_parent"), 80)
    safe_stem = safe_component(source_base.get("source_video_relative_path", ""), 120)
    return FRAME_OUT / f"vid_{source_video_id}_{safe_parent}_{safe_stem}"


# ============================================================
# 5. 单视频处理
# ============================================================
def process_one(job_no, total_jobs, src: Path, source_base, output_dir: Path, task_action: str):
    task_started = time.perf_counter()
    rel = source_base["source_video_relative_path"]
    source_video_id = source_base["source_video_id"]
    out_dir = output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[start {job_no}/{total_jobs}] vid={source_video_id} action={task_action} {rel}", flush=True)

    probe_t0 = time.perf_counter()
    duration_seconds, probe_error = ffprobe_duration(src)
    probe_elapsed = time.perf_counter() - probe_t0
    expected_count = estimate_expected_count(duration_seconds)

    # 对短于标准 2s 起抽规则的视频，也尝试最少产出 1 帧。
    # 规则：优先 1500ms；如果视频短于 1500ms，则在 >=500ms 的前提下取视频中点。
    if expected_count == 0:
        fallback_time_ms = choose_short_video_single_frame_fallback_time_ms(duration_seconds)
        if fallback_time_ms is None:
            clear_partial_frames(out_dir)
            task_elapsed = time.perf_counter() - task_started
            report = dict(source_base)
            report.update({
                "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else "",
                "expected_frame_count_estimate": 0,
                "produced_frame_count": 0,
                "valid_jpg1280_count": 0,
                "invalid_jpg_count": 0,
                "missing_expected_frame_count": 0,
                "tail_boundary_tolerated": False,
                "frame_extract_status": "skipped_too_short_for_sampling_offset",
                "fallback_attempted": False,
                "extract_mode": "skipped_too_short_for_sampling_offset",
                "short_video_single_frame_fallback_attempted": False,
                "short_video_single_frame_fallback_success": False,
                "short_video_single_frame_fallback_time_ms": "",
                "decode_mode": "skipped",
                "decode_mode_primary": "videotoolbox",
                "fallback_decode_mode": "software",
                "decode_attempts_json": "[]",
                "frame_output_dir": str(out_dir),
                "frame_output_relative_dir": str(out_dir.relative_to(OUT)),
                "task_action": task_action,
                "checkpoint_status": "completed",
                "concurrency": CONCURRENCY,
                "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
                "ffmpeg_returncode": "",
                "ffprobe_elapsed_seconds": round(probe_elapsed, 3),
                "ffmpeg_extract_elapsed_seconds": 0,
                "validation_elapsed_seconds": 0,
                "frame_sha256_elapsed_seconds": 0,
                "task_elapsed_seconds": round(task_elapsed, 3),
                "ffprobe_error_tail": probe_error[-800:] if probe_error else "",
                "ffmpeg_stderr_tail": "",
            })
            sidecar_path = IDENTITY_DIR / f"vid_{source_video_id}.json"
            sidecar_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[skip-short {job_no}/{total_jobs}] vid={source_video_id} duration={duration_seconds} file={rel}", flush=True)
            return report, [], source_base

        pattern = out_dir / "frame_%06d.jpg"
        extract_result = run_short_video_single_frame_fallback(
            src,
            out_dir,
            duration_seconds,
            {
                "final_decode_mode": "failed",
                "extract_mode": "short_video_single_frame_1500ms",
                "fallback_attempted": False,
                "decode_attempts": [],
                "ffmpeg_returncode": "",
                "ffmpeg_extract_elapsed_seconds": 0,
                "ffmpeg_stderr_tail": "",
            },
        )

        id_t0 = time.perf_counter()
        frame_identity = rename_frames_to_strong_ids(out_dir, source_video_id, extract_result.get("frame_time_ms_list"))
        id_elapsed = time.perf_counter() - id_t0

        produced_count = frame_identity["produced_count"]
        valid_count = frame_identity["valid_count"]
        invalid_count = frame_identity["invalid_count"]
        final_decode_mode = extract_result["final_decode_mode"]
        fallback_attempted = extract_result["fallback_attempted"]

        status, tail_boundary_tolerated = classify_status(
            duration_seconds=duration_seconds,
            expected_count=expected_count,
            produced_count=produced_count,
            valid_count=valid_count,
            final_decode_mode=final_decode_mode,
            fallback_attempted=fallback_attempted,
            extract_mode=extract_result.get("extract_mode", "short_video_single_frame_1500ms"),
        )

        missing_expected = 0

        frame_rows = []
        for fr in frame_identity["rows"]:
            row = dict(source_base)
            row.update({
                "frame_id": fr["frame_id"],
                "frame_id_version": FRAME_ID_VERSION,
                "frame_file_sha256": fr["frame_file_sha256"],
                "frame_file_sha256_version": FRAME_FILE_SHA256_VERSION,
                "frame_file": str(fr["frame_path"]),
                "frame_relative_path": str(fr["frame_path"].relative_to(OUT)),
                "frame_index": fr["frame_index"],
                "estimated_frame_time_ms": fr["estimated_frame_time_ms"],
                "sampling_offset_ms": SAMPLING_OFFSET_MS,
                "sampling_interval_ms": SAMPLING_INTERVAL_MS,
                "is_valid_jpg1280": fr["is_valid_jpg1280"],
                "width": fr["width"],
                "height": fr["height"],
                "decode_mode": final_decode_mode,
                "fallback_attempted": fallback_attempted,
                "extract_mode": extract_result.get("extract_mode", "short_video_single_frame_1500ms"),
                "short_video_single_frame_fallback_attempted": extract_result.get("short_video_single_frame_fallback_attempted", False),
                "short_video_single_frame_fallback_success": extract_result.get("short_video_single_frame_fallback_success", False),
                "short_video_single_frame_fallback_time_ms": extract_result.get("short_video_single_frame_fallback_time_ms", ""),
                "tail_boundary_tolerated_for_video": tail_boundary_tolerated,
                "frame_extract_status_for_video": status,
                "task_action": task_action,
                "checkpoint_status": "completed" if status in SUCCESS_STATUSES else "failed",
                "concurrency": CONCURRENCY,
                "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
            })
            frame_rows.append(row)

        task_elapsed = time.perf_counter() - task_started
        report = dict(source_base)
        report.update({
            "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else "",
            "expected_frame_count_estimate": expected_count if expected_count is not None else "",
            "produced_frame_count": produced_count,
            "valid_jpg1280_count": valid_count,
            "invalid_jpg_count": invalid_count,
            "missing_expected_frame_count": missing_expected,
            "tail_boundary_tolerated": tail_boundary_tolerated,
            "frame_extract_status": status,
            "fallback_attempted": fallback_attempted,
            "extract_mode": extract_result.get("extract_mode", "short_video_single_frame_1500ms"),
            "short_video_single_frame_fallback_attempted": extract_result.get("short_video_single_frame_fallback_attempted", False),
            "short_video_single_frame_fallback_success": extract_result.get("short_video_single_frame_fallback_success", False),
            "short_video_single_frame_fallback_time_ms": extract_result.get("short_video_single_frame_fallback_time_ms", ""),
            "decode_mode": final_decode_mode,
            "decode_mode_primary": "videotoolbox",
            "fallback_decode_mode": "software",
            "decode_attempts_json": json.dumps(extract_result["decode_attempts"], ensure_ascii=False),
            "frame_output_dir": str(out_dir),
            "frame_output_relative_dir": str(out_dir.relative_to(OUT)),
            "task_action": task_action,
            "checkpoint_status": "completed" if status in SUCCESS_STATUSES else "failed",
            "concurrency": CONCURRENCY,
            "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
            "ffmpeg_returncode": extract_result["ffmpeg_returncode"],
            "ffprobe_elapsed_seconds": round(probe_elapsed, 3),
            "ffmpeg_extract_elapsed_seconds": extract_result["ffmpeg_extract_elapsed_seconds"],
            "validation_elapsed_seconds": 0,
            "frame_sha256_elapsed_seconds": round(id_elapsed, 3),
            "task_elapsed_seconds": round(task_elapsed, 3),
            "ffprobe_error_tail": probe_error[-800:] if probe_error else "",
            "ffmpeg_stderr_tail": extract_result["ffmpeg_stderr_tail"],
        })

        sidecar = dict(report)
        sidecar["frame_rows_count"] = len(frame_rows)
        sidecar_path = IDENTITY_DIR / f"vid_{source_video_id}.json"
        sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

        print(
            f"[short-fallback {job_no}/{total_jobs}] vid={source_video_id} duration={duration_seconds} "
            f"time_ms={extract_result.get('short_video_single_frame_fallback_time_ms')} "
            f"produced={produced_count} valid={valid_count} status={status} file={rel}",
            flush=True,
        )
        return report, frame_rows, source_base

    pattern = out_dir / "frame_%06d.jpg"
    vf = (
        f"fps=1/{SAMPLING_INTERVAL_MS / 1000}:start_time={SAMPLING_OFFSET_MS / 1000},"
        f"scale='if(gt(iw,ih),{MAX_EDGE_PX},-2)':'if(gt(ih,iw),{MAX_EDGE_PX},-2)'"
    )

    extract_result = run_extract_with_fallback(src, out_dir, pattern, vf)

    # 短视频例外兜底：当前标准 C4S 规则是 2s 起抽。
    # 对 5 秒以内、标准规则没有产出帧的视频，尝试在 1500ms 抽单帧。
    # 如果视频本身短于 1500ms，则在 >=500ms 的前提下取视频中点，避免必然失败。
    if (
        duration_seconds is not None
        and duration_seconds > 0
        and duration_seconds * 1000 <= SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MAX_DURATION_MS
        and not extract_result.get("short_video_single_frame_fallback_success")
        and (extract_result.get("final_decode_mode") == "failed")
    ):
        extract_result = run_short_video_single_frame_fallback(src, out_dir, duration_seconds, extract_result)

    id_t0 = time.perf_counter()
    frame_identity = rename_frames_to_strong_ids(out_dir, source_video_id, extract_result.get("frame_time_ms_list"))
    id_elapsed = time.perf_counter() - id_t0

    produced_count = frame_identity["produced_count"]
    valid_count = frame_identity["valid_count"]
    invalid_count = frame_identity["invalid_count"]
    final_decode_mode = extract_result["final_decode_mode"]
    fallback_attempted = extract_result["fallback_attempted"]

    status, tail_boundary_tolerated = classify_status(
        duration_seconds=duration_seconds,
        expected_count=expected_count,
        produced_count=produced_count,
        valid_count=valid_count,
        final_decode_mode=final_decode_mode,
        fallback_attempted=fallback_attempted,
        extract_mode=extract_result.get("extract_mode", "standard_c4s"),
    )

    missing_expected = max(expected_count - produced_count, 0) if expected_count is not None else None

    frame_rows = []
    for fr in frame_identity["rows"]:
        row = dict(source_base)
        row.update({
            "frame_id": fr["frame_id"],
            "frame_id_version": FRAME_ID_VERSION,
            "frame_file_sha256": fr["frame_file_sha256"],
            "frame_file_sha256_version": FRAME_FILE_SHA256_VERSION,
            "frame_file": str(fr["frame_path"]),
            "frame_relative_path": str(fr["frame_path"].relative_to(OUT)),
            "frame_index": fr["frame_index"],
            "estimated_frame_time_ms": fr["estimated_frame_time_ms"],
            "sampling_offset_ms": SAMPLING_OFFSET_MS,
            "sampling_interval_ms": SAMPLING_INTERVAL_MS,
            "is_valid_jpg1280": fr["is_valid_jpg1280"],
            "width": fr["width"],
            "height": fr["height"],
            "decode_mode": final_decode_mode,
            "fallback_attempted": fallback_attempted,
            "extract_mode": extract_result.get("extract_mode", "standard_c4s"),
            "short_video_single_frame_fallback_attempted": extract_result.get("short_video_single_frame_fallback_attempted", False),
            "short_video_single_frame_fallback_success": extract_result.get("short_video_single_frame_fallback_success", False),
            "short_video_single_frame_fallback_time_ms": extract_result.get("short_video_single_frame_fallback_time_ms", ""),
            "tail_boundary_tolerated_for_video": tail_boundary_tolerated,
            "frame_extract_status_for_video": status,
            "task_action": task_action,
            "checkpoint_status": "completed" if status in SUCCESS_STATUSES else "failed",
            "concurrency": CONCURRENCY,
            "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
        })
        frame_rows.append(row)

    task_elapsed = time.perf_counter() - task_started
    report = dict(source_base)
    report.update({
        "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else "",
        "expected_frame_count_estimate": expected_count if expected_count is not None else "",
        "produced_frame_count": produced_count,
        "valid_jpg1280_count": valid_count,
        "invalid_jpg_count": invalid_count,
        "missing_expected_frame_count": missing_expected if missing_expected is not None else "",
        "tail_boundary_tolerated": tail_boundary_tolerated,
        "frame_extract_status": status,
        "fallback_attempted": fallback_attempted,
        "extract_mode": extract_result.get("extract_mode", "standard_c4s"),
        "short_video_single_frame_fallback_attempted": extract_result.get("short_video_single_frame_fallback_attempted", False),
        "short_video_single_frame_fallback_success": extract_result.get("short_video_single_frame_fallback_success", False),
        "short_video_single_frame_fallback_time_ms": extract_result.get("short_video_single_frame_fallback_time_ms", ""),
        "decode_mode": final_decode_mode,
        "decode_mode_primary": "videotoolbox",
        "fallback_decode_mode": "software",
        "decode_attempts_json": json.dumps(extract_result["decode_attempts"], ensure_ascii=False),
        "frame_output_dir": str(out_dir),
        "frame_output_relative_dir": str(out_dir.relative_to(OUT)),
        "task_action": task_action,
        "checkpoint_status": "completed" if status in SUCCESS_STATUSES else "failed",
        "concurrency": CONCURRENCY,
        "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
        "ffmpeg_returncode": extract_result["ffmpeg_returncode"],
        "ffprobe_elapsed_seconds": round(probe_elapsed, 3),
        "ffmpeg_extract_elapsed_seconds": extract_result["ffmpeg_extract_elapsed_seconds"],
        "validation_elapsed_seconds": 0,
        "frame_sha256_elapsed_seconds": round(id_elapsed, 3),
        "task_elapsed_seconds": round(task_elapsed, 3),
        "ffprobe_error_tail": probe_error[-800:] if probe_error else "",
        "ffmpeg_stderr_tail": extract_result["ffmpeg_stderr_tail"],
    })

    sidecar = dict(report)
    sidecar["frame_rows_count"] = len(frame_rows)
    sidecar_path = IDENTITY_DIR / f"vid_{source_video_id}.json"
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[done {job_no}/{total_jobs}] vid={source_video_id} duration={duration_seconds} "
        f"expected={expected_count} produced={produced_count} valid={valid_count} "
        f"decode={final_decode_mode} fallback={fallback_attempted} status={status} file={rel}",
        flush=True,
    )
    return report, frame_rows, source_base


# ============================================================
# 6. 输出与报告
# ============================================================
def write_csv(path: Path, rows, fallback_fieldnames):
    """Write heterogeneous dict rows safely.

    V4 used the first row's keys as CSV fieldnames. On resume runs, early
    skip-existing rows and later processed rows can carry different fields
    such as short_video_single_frame_fallback_* and extract_mode. That made
    DictWriter raise ValueError after all video work had already completed.
    V5 builds a stable union schema for this file, preserving first-seen
    order and filling missing cells with an empty string.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        fieldnames = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
    else:
        fieldnames = list(fallback_fieldnames)

    normalized_rows = []
    for r in rows:
        normalized_rows.append({k: r.get(k, "") for k in fieldnames})

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if normalized_rows:
            writer.writerows(normalized_rows)


def write_reports(
    args,
    video_reports,
    frame_rows_all,
    source_identity_rows,
    plan_rows,
    scan_elapsed,
    fingerprint_elapsed,
    total_elapsed,
    queue_count,
    eligible_queue_count,
    action_counts,
):
    video_reports.sort(key=lambda r: r["source_video_relative_path"])
    frame_rows_all.sort(key=lambda r: (r["source_video_relative_path"], int(r["frame_index"])))
    source_identity_rows.sort(key=lambda r: r["source_video_relative_path"])

    write_csv(VIDEO_REPORT_CSV, video_reports, ["empty"])
    write_csv(MANIFEST_CSV, frame_rows_all, ["empty"])
    write_csv(SOURCE_IDENTITY_CSV, source_identity_rows, [
        "artifact_schema_version", "source_video_id", "source_video_id_version",
        "source_video_fingerprint_sha256", "source_video_path", "source_video_relative_path",
        "source_file_size_bytes", "source_file_mtime_ns", "sampling_contract_id",
        "parent_source_file_id", "parent_source_content_id",
        "parent_source_path_at_processing_time", "parent_media_kind",
    ])

    plan_path = MANIFEST_DIR / "video_frame_c4s_step01_queue_plan.csv"
    write_csv(plan_path, plan_rows, ["empty"])

    status_counts = Counter(r["frame_extract_status"] for r in video_reports)
    decode_counts = Counter(r.get("decode_mode", "") for r in video_reports)

    summary = {
        "script_version": SCRIPT_VERSION,
        "scheme": SCRIPT_SCHEME,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "step01_workspace": str(args.step01_workspace) if args.step01_workspace else ".",
        "step01_video_queue": str((args.step01_workspace / "queues" / "process_queue_video.jsonl") if args.step01_workspace else "."),
        "output_dir": str(OUT),
        "state_db": str(STATE_DB),
        "source_safety": "source_read_only_no_write_no_move_no_delete_no_rename",
        "input_policy": "consume_step01_canonical_video_queue_only",
        "limit_policy": "limit_new_means_max_new_jobs_to_start_this_run_running_jobs_are_not_killed",
        "limit_new": args.limit_new,
        "incremental_policy": "skip_existing_success_when_checkpoint_and_frame_sha256_validation_pass",
        "checkpoint_policy": "sqlite_video_checkpoint_written_after_each_completed_video",
        "lineage_policy": "every_report_and_frame_row_contains_parent_source_file_id_and_parent_source_content_id",
        "source_video_id_version": SOURCE_VIDEO_ID_VERSION,
        "frame_id_version": FRAME_ID_VERSION,
        "frame_file_sha256_version": FRAME_FILE_SHA256_VERSION,
        "sampling_contract_id": SAMPLING_CONTRACT_ID,
        "sampling_contract": SAMPLING_CONTRACT,
        "decode_mode_primary": "videotoolbox",
        "fallback_decode_mode": "software",
        "fallback_policy": "retry_with_software_decode_when_videotoolbox_fails_or_produces_no_valid_jpg",
        "ffmpeg_primary_extra_args": ["-hwaccel", "videotoolbox"],
        "concurrency": CONCURRENCY,
        "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
        "max_edge_px": MAX_EDGE_PX,
        "final_output_format": "jpg",
        "ffmpeg_jpeg_quality_arg": "-q:v 3",
        "sampling_offset_ms": SAMPLING_OFFSET_MS,
        "sampling_interval_ms": SAMPLING_INTERVAL_MS,
        "tail_boundary_policy": "missing_one_expected_tail_frame_is_tolerated_and_recorded",
        "short_video_policy": "if standard c4s produces no frames and duration <= 5s, try one frame at 1500ms; if duration < 1500ms use midpoint when >=500ms",
        "short_video_single_frame_fallback_ms": SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MS,
        "short_video_single_frame_fallback_max_duration_ms": SHORT_VIDEO_SINGLE_FRAME_FALLBACK_MAX_DURATION_MS,
        "directory_contract": {
            "derived_video_frames": str(FRAME_OUT),
            "manifests": str(MANIFEST_DIR),
            "reports": str(REPORT_DIR),
            "source_identity_sidecars": str(IDENTITY_DIR),
            "state": str(STATE_DIR),
        },
        "stage_elapsed_seconds": {
            "queue_read_and_filter": round(scan_elapsed, 3),
            "source_video_fingerprint_total_wall": round(fingerprint_elapsed, 3),
            "ffprobe_total_sum": round(sum(float(r.get("ffprobe_elapsed_seconds") or 0) for r in video_reports), 3),
            "ffmpeg_extract_total_sum": round(sum(float(r.get("ffmpeg_extract_elapsed_seconds") or 0) for r in video_reports), 3),
            "frame_sha256_total_sum": round(sum(float(r.get("frame_sha256_elapsed_seconds") or 0) for r in video_reports), 3),
            "total_task_wall": round(total_elapsed, 3),
        },
        "step01_video_queue_total_rows": queue_count,
        "eligible_video_queue_count": eligible_queue_count,
        "planned_new_job_count": int(action_counts.get("process_new", 0))
            + sum(v for k, v in action_counts.items() if str(k).startswith("retry_from_") or str(k).startswith("reprocess_because_")),
        "action_counts": dict(action_counts),
        "video_report_rows_this_run": len(video_reports),
        "frame_manifest_rows_this_run": len(frame_rows_all),
        "total_produced_frame_count_in_report": sum(int(r.get("produced_frame_count") or 0) for r in video_reports),
        "total_valid_jpg1280_count_in_report": sum(int(r.get("valid_jpg1280_count") or 0) for r in video_reports),
        "total_invalid_jpg_count_in_report": sum(int(r.get("invalid_jpg_count") or 0) for r in video_reports),
        "frame_extract_status_counts": dict(status_counts),
        "decode_mode_counts": dict(decode_counts),
        "fallback_attempted_count": sum(1 for r in video_reports if bool(r.get("fallback_attempted"))),
        "tail_boundary_tolerated_count": sum(1 for r in video_reports if bool(r.get("tail_boundary_tolerated"))),
        "short_video_single_frame_fallback_attempted_count": sum(1 for r in video_reports if bool(r.get("short_video_single_frame_fallback_attempted"))),
        "short_video_single_frame_fallback_success_count": sum(1 for r in video_reports if bool(r.get("short_video_single_frame_fallback_success"))),
        "video_report_csv": str(VIDEO_REPORT_CSV),
        "video_frame_manifest_csv": str(MANIFEST_CSV),
        "source_identity_csv": str(SOURCE_IDENTITY_CSV),
        "plan_csv": str(plan_path),
        "frame_output_dir": str(FRAME_OUT),
        "manual_visual_check_required": True,
    }

    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_MD.write_text(
        "# Step02 Video Frame C4S-ID From Step01 Queue Summary\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- step01_workspace: {args.step01_workspace if args.step01_workspace else '.'}\n"
        f"- output_dir: {OUT}\n"
        f"- state_db: {STATE_DB}\n"
        f"- source_safety: {summary['source_safety']}\n"
        f"- input_policy: {summary['input_policy']}\n"
        f"- limit_new: {args.limit_new}\n"
        f"- limit_policy: {summary['limit_policy']}\n"
        f"- artifact_schema_version: {ARTIFACT_SCHEMA_VERSION}\n"
        f"- sampling_contract_id: {SAMPLING_CONTRACT_ID}\n"
        f"- source_video_id_version: {SOURCE_VIDEO_ID_VERSION}\n"
        f"- frame_id_version: {FRAME_ID_VERSION}\n"
        f"- decode_mode_primary: videotoolbox\n"
        f"- fallback_decode_mode: software\n"
        f"- concurrency: {CONCURRENCY}\n"
        f"- sampling_offset_ms: {SAMPLING_OFFSET_MS}\n"
        f"- sampling_interval_ms: {SAMPLING_INTERVAL_MS}\n"
        f"- max_edge_px: {MAX_EDGE_PX}\n"
        f"- step01_video_queue_total_rows: {summary['step01_video_queue_total_rows']}\n"
        f"- eligible_video_queue_count: {summary['eligible_video_queue_count']}\n"
        f"- action_counts: {summary['action_counts']}\n"
        f"- total_produced_frame_count_in_report: {summary['total_produced_frame_count_in_report']}\n"
        f"- total_valid_jpg1280_count_in_report: {summary['total_valid_jpg1280_count_in_report']}\n"
        f"- fallback_attempted_count: {summary['fallback_attempted_count']}\n"
        f"- tail_boundary_tolerated_count: {summary['tail_boundary_tolerated_count']}\n"
        f"- short_video_single_frame_fallback_attempted_count: {summary.get('short_video_single_frame_fallback_attempted_count')}\n"
        f"- short_video_single_frame_fallback_success_count: {summary.get('short_video_single_frame_fallback_success_count')}\n"
        f"- frame_extract_status_counts: {summary['frame_extract_status_counts']}\n"
        f"- decode_mode_counts: {summary['decode_mode_counts']}\n\n"
        "## Resume Rule\n\n"
        "Run again with the same `--out` and without `--limit-new` to continue remaining videos. "
        "Completed videos are skipped only if SQLite checkpoint and frame sha256 validation pass.\n",
        encoding="utf-8",
    )
    return summary


def ensure_dirs():
    FRAME_OUT.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def init_paths(out_dir: Path, concurrency: int):
    global OUT, FRAME_OUT, MANIFEST_DIR, REPORT_DIR, IDENTITY_DIR, STATE_DIR
    global SUMMARY_JSON, SUMMARY_MD, MANIFEST_CSV, VIDEO_REPORT_CSV, SOURCE_IDENTITY_CSV, STATE_DB
    global CONCURRENCY

    OUT = out_dir.expanduser().resolve()
    FRAME_OUT = OUT / "derived" / "video_frames" / "c4s_id_incremental_jpg1280"
    MANIFEST_DIR = OUT / "manifests"
    REPORT_DIR = OUT / "reports"
    IDENTITY_DIR = OUT / "identity" / "source_videos"
    STATE_DIR = OUT / "state"

    SUMMARY_JSON = REPORT_DIR / "video_frame_c4s_step01_queue_summary.json"
    SUMMARY_MD = REPORT_DIR / "video_frame_c4s_step01_queue_summary.md"
    MANIFEST_CSV = MANIFEST_DIR / "video_frame_c4s_step01_queue_manifest.csv"
    VIDEO_REPORT_CSV = MANIFEST_DIR / "video_extract_c4s_step01_queue_report.csv"
    SOURCE_IDENTITY_CSV = MANIFEST_DIR / "source_video_identity_step01_queue.csv"
    STATE_DB = STATE_DIR / "video_frame_c4s_step01_queue_state.sqlite"
    CONCURRENCY = concurrency



# ============================================================
# 6B. 内置资源监控与最终报告
# ============================================================
RESOURCE_FIELDS = [
    "run_invocation_id", "run_phase",
    "timestamp", "elapsed_seconds", "stage", "root_pid", "pid_count", "ffmpeg_pid_count",
    "process_cpu_percent_sum", "process_cpu_cores_estimated", "process_rss_mb_sum",
    "ffmpeg_cpu_percent_sum", "ffmpeg_rss_mb_sum",
    "system_cpu_percent_sum_all_processes", "system_cpu_cores_estimated_all_processes",
    "memory_free_mb", "memory_active_mb", "memory_inactive_mb", "memory_wired_mb",
    "memory_compressed_mb", "swap_used_mb", "disk_read_kb_s", "disk_write_kb_s", "sample_error",
]


def append_resource_csv(path: Path, row: dict, header_state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0 or not header_state.get(str(path))
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESOURCE_FIELDS)
        if write_header:
            writer.writeheader()
            header_state[str(path)] = True
        writer.writerow({k: row.get(k, "") for k in RESOURCE_FIELDS})


def append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def get_process_tree_pids(root_pid):
    if not root_pid:
        return []
    seen = set()
    pending = [int(root_pid)]
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        rc, out, err = run_cmd(["pgrep", "-P", str(pid)], timeout=2)
        if rc == 0 and out.strip():
            for x in out.split():
                try:
                    child = int(x)
                    if child not in seen:
                        pending.append(child)
                except Exception:
                    pass
    return sorted(seen)


def get_process_stats(pids):
    if not pids:
        return {
            "pid_count": 0, "ffmpeg_pid_count": 0,
            "process_cpu_percent_sum": 0.0, "process_cpu_cores_estimated": 0.0,
            "process_rss_mb_sum": 0.0, "ffmpeg_cpu_percent_sum": 0.0, "ffmpeg_rss_mb_sum": 0.0,
        }
    pid_arg = ",".join(str(p) for p in pids)
    rc, out, err = run_cmd(["ps", "-o", "pid=,pcpu=,rss=,comm=", "-p", pid_arg], timeout=3)
    total_cpu = total_rss_kb = ffmpeg_cpu = ffmpeg_rss_kb = 0.0
    ffmpeg_count = 0
    if rc == 0:
        for line in out.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            try:
                cpu = float(parts[1])
                rss_kb = float(parts[2])
                comm = parts[3].lower()
            except Exception:
                continue
            total_cpu += cpu
            total_rss_kb += rss_kb
            if "ffmpeg" in comm:
                ffmpeg_count += 1
                ffmpeg_cpu += cpu
                ffmpeg_rss_kb += rss_kb
    return {
        "pid_count": len(pids),
        "ffmpeg_pid_count": ffmpeg_count,
        "process_cpu_percent_sum": round(total_cpu, 3),
        "process_cpu_cores_estimated": round(total_cpu / 100.0, 3),
        "process_rss_mb_sum": round(total_rss_kb / 1024.0, 3),
        "ffmpeg_cpu_percent_sum": round(ffmpeg_cpu, 3),
        "ffmpeg_rss_mb_sum": round(ffmpeg_rss_kb / 1024.0, 3),
    }


def get_system_cpu_from_ps():
    rc, out, err = run_cmd(["bash", "-lc", "ps -A -o %cpu= | awk '{s+=$1} END {print s+0}'"], timeout=3)
    total = 0.0
    if rc == 0:
        try:
            total = float(out.strip() or 0)
        except Exception:
            total = 0.0
    return {
        "system_cpu_percent_sum_all_processes": round(total, 3),
        "system_cpu_cores_estimated_all_processes": round(total / 100.0, 3),
    }


def get_swap_used_mb():
    rc, out, err = run_cmd(["sysctl", "vm.swapusage"], timeout=3)
    if rc != 0:
        return 0.0
    m = re.search(r"used\s*=\s*([0-9.]+)M", out)
    return round(float(m.group(1)), 3) if m else 0.0


def get_vm_stat_memory():
    rc, out, err = run_cmd(["vm_stat"], timeout=3)
    if rc != 0:
        return {}
    page_size = 4096
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page_size = int(m.group(1))
    values = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        num = re.sub(r"[^0-9]", "", v)
        if num:
            values[k.strip()] = int(num)
    def mb(name):
        return round(values.get(name, 0) * page_size / 1024 / 1024, 3)
    return {
        "memory_free_mb": mb("Pages free"),
        "memory_active_mb": mb("Pages active"),
        "memory_inactive_mb": mb("Pages inactive"),
        "memory_wired_mb": mb("Pages wired down"),
        "memory_compressed_mb": mb("Pages occupied by compressor"),
        "swap_used_mb": get_swap_used_mb(),
    }


def get_iostat_sample():
    rc, out, err = run_cmd(["bash", "-lc", "iostat -d -K -w 1 -c 2 2>/dev/null | tail -n 1"], timeout=5)
    if rc != 0 or not out.strip():
        return {"disk_read_kb_s": "", "disk_write_kb_s": ""}
    nums = []
    for token in out.strip().split():
        try:
            nums.append(float(token))
        except Exception:
            pass
    if len(nums) >= 2:
        return {"disk_read_kb_s": round(nums[-2], 3), "disk_write_kb_s": round(nums[-1], 3)}
    return {"disk_read_kb_s": "", "disk_write_kb_s": ""}


class ResourceMonitor:
    def __init__(self, telemetry_dir: Path, interval: float, root_pid: int, run_invocation_id: str, run_phase: str):
        self.telemetry_dir = telemetry_dir
        self.interval = max(float(interval), 1.0)
        self.root_pid = int(root_pid)
        self.run_invocation_id = run_invocation_id
        self.run_phase = run_phase
        self.history_dir = telemetry_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.history_dir / f"{run_invocation_id}_step02_video_frame_{run_phase}_resource_samples.csv"
        self.jsonl_path = self.history_dir / f"{run_invocation_id}_step02_video_frame_{run_phase}_resource_samples.jsonl"
        self.latest_csv_path = telemetry_dir / "step02_video_frame_resource_samples_latest.csv"
        self.latest_jsonl_path = telemetry_dir / "step02_video_frame_resource_samples_latest.jsonl"
        self.stop_event = threading.Event()
        self.stage = "starting"
        self.lock = threading.Lock()
        self.thread = None
        self.started = time.time()
        self.header_state = {}
        self.sample_count = 0
        self.max_values = {
            "process_cpu_percent_sum": 0.0,
            "process_rss_mb_sum": 0.0,
            "ffmpeg_cpu_percent_sum": 0.0,
            "ffmpeg_rss_mb_sum": 0.0,
            "system_cpu_percent_sum_all_processes": 0.0,
            "swap_used_mb": 0.0,
            "disk_read_kb_s": 0.0,
            "disk_write_kb_s": 0.0,
        }

    def set_stage(self, stage: str):
        with self.lock:
            self.stage = stage

    def start(self):
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=self.interval + 2)
        self.write_latest_aliases()

    def write_latest_aliases(self):
        try:
            if self.csv_path.exists():
                shutil.copy2(self.csv_path, self.latest_csv_path)
            if self.jsonl_path.exists():
                shutil.copy2(self.jsonl_path, self.latest_jsonl_path)
        except Exception:
            pass

    def _loop(self):
        while not self.stop_event.is_set():
            row = self.sample()
            self.sample_count += 1
            for k in self.max_values:
                try:
                    v = float(row.get(k) or 0)
                    if v > self.max_values[k]:
                        self.max_values[k] = v
                except Exception:
                    pass
            append_resource_csv(self.csv_path, row, self.header_state)
            append_jsonl(self.jsonl_path, row)
            self.stop_event.wait(self.interval)

    def sample(self):
        with self.lock:
            stage = self.stage
        row = {k: "" for k in RESOURCE_FIELDS}
        row.update({
            "run_invocation_id": self.run_invocation_id,
            "run_phase": self.run_phase,
            "timestamp": utc_now_iso(),
            "elapsed_seconds": round(time.time() - self.started, 3),
            "stage": stage,
            "root_pid": self.root_pid,
            "sample_error": "",
        })
        errors = []
        try:
            row.update(get_process_stats(get_process_tree_pids(self.root_pid)))
        except Exception as exc:
            errors.append(f"process:{exc}")
        try:
            row.update(get_system_cpu_from_ps())
        except Exception as exc:
            errors.append(f"system_cpu:{exc}")
        try:
            row.update(get_vm_stat_memory())
        except Exception as exc:
            errors.append(f"memory:{exc}")
        try:
            row.update(get_iostat_sample())
        except Exception as exc:
            errors.append(f"disk:{exc}")
        row["sample_error"] = "|".join(errors)
        return row


def write_telemetry_reports(args, summary: dict, video_reports: list, telemetry_dir: Path, final_report_dir: Path, monitor: ResourceMonitor):
    """Write per-run history reports and latest aliases.

    V4 rule:
    - Every invocation writes immutable history files with run_invocation_id + run_phase in filenames.
    - Latest files are convenience aliases only.
    - Legacy final_run_report.md/json are not rewritten by V4; old files may remain from earlier runs.
    """
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    final_report_dir.mkdir(parents=True, exist_ok=True)
    telemetry_history_dir = telemetry_dir / "history"
    final_history_dir = final_report_dir / "history"
    manifest_history_dir = MANIFEST_DIR / "history"
    report_history_dir = REPORT_DIR / "history"
    api_dir = telemetry_dir / "api"
    for d in [telemetry_history_dir, final_history_dir, manifest_history_dir, report_history_dir, api_dir]:
        d.mkdir(parents=True, exist_ok=True)

    run_invocation_id = getattr(args, "run_invocation_id", local_run_id())
    run_phase = getattr(args, "run_phase_resolved", "first15" if args.limit_new else "resume")
    name_prefix = f"{run_invocation_id}_step02_video_frame_{run_phase}"

    per_video_rows = []
    for r in video_reports:
        try:
            valid_frames = int(r.get("valid_jpg1280_count") or 0)
        except Exception:
            valid_frames = 0
        try:
            ffmpeg_sec = float(r.get("ffmpeg_extract_elapsed_seconds") or 0)
        except Exception:
            ffmpeg_sec = 0.0
        try:
            task_sec = float(r.get("task_elapsed_seconds") or 0)
        except Exception:
            task_sec = 0.0
        per_video_rows.append({
            "run_invocation_id": run_invocation_id,
            "run_phase": run_phase,
            "source_video_id": r.get("source_video_id", ""),
            "source_video_relative_path": r.get("source_video_relative_path", ""),
            "parent_source_file_id": r.get("parent_source_file_id", ""),
            "frame_extract_status": r.get("frame_extract_status", ""),
            "task_action": r.get("task_action", ""),
            "decode_mode": r.get("decode_mode", ""),
            "fallback_attempted": r.get("fallback_attempted", ""),
            "extract_mode": r.get("extract_mode", ""),
            "short_video_single_frame_fallback_attempted": r.get("short_video_single_frame_fallback_attempted", ""),
            "short_video_single_frame_fallback_success": r.get("short_video_single_frame_fallback_success", ""),
            "short_video_single_frame_fallback_time_ms": r.get("short_video_single_frame_fallback_time_ms", ""),
            "duration_seconds": r.get("duration_seconds", ""),
            "produced_frame_count": r.get("produced_frame_count", ""),
            "valid_jpg1280_count": valid_frames,
            "ffprobe_elapsed_seconds": r.get("ffprobe_elapsed_seconds", ""),
            "ffmpeg_extract_elapsed_seconds": ffmpeg_sec,
            "frame_sha256_elapsed_seconds": r.get("frame_sha256_elapsed_seconds", ""),
            "task_elapsed_seconds": task_sec,
            "avg_ffmpeg_seconds_per_valid_frame": round(ffmpeg_sec / valid_frames, 6) if valid_frames else "",
            "avg_task_seconds_per_valid_frame": round(task_sec / valid_frames, 6) if valid_frames else "",
        })

    per_video_fields = [
        "run_invocation_id", "run_phase", "source_video_id", "source_video_relative_path", "parent_source_file_id",
        "frame_extract_status", "task_action", "decode_mode", "fallback_attempted",
        "extract_mode", "short_video_single_frame_fallback_attempted",
        "short_video_single_frame_fallback_success", "short_video_single_frame_fallback_time_ms",
        "duration_seconds", "produced_frame_count", "valid_jpg1280_count",
        "ffprobe_elapsed_seconds", "ffmpeg_extract_elapsed_seconds", "frame_sha256_elapsed_seconds",
        "task_elapsed_seconds", "avg_ffmpeg_seconds_per_valid_frame", "avg_task_seconds_per_valid_frame",
    ]
    per_video_history_csv = telemetry_history_dir / f"{name_prefix}_per_video_timing.csv"
    per_video_latest_csv = telemetry_dir / "step02_video_frame_per_video_timing_latest.csv"
    write_csv(per_video_history_csv, per_video_rows, per_video_fields)
    shutil.copy2(per_video_history_csv, per_video_latest_csv)

    avg_task_vals = [float(r["avg_task_seconds_per_valid_frame"]) for r in per_video_rows if r.get("avg_task_seconds_per_valid_frame") not in ("", None)]
    avg_ffmpeg_vals = [float(r["avg_ffmpeg_seconds_per_valid_frame"]) for r in per_video_rows if r.get("avg_ffmpeg_seconds_per_valid_frame") not in ("", None)]

    resource_samples_csv = str(getattr(monitor, "csv_path", telemetry_dir / "resource_samples.csv"))
    resource_samples_jsonl = str(getattr(monitor, "jsonl_path", telemetry_dir / "resource_samples.jsonl"))
    latest_resource_samples_csv = str(getattr(monitor, "latest_csv_path", telemetry_dir / "step02_video_frame_resource_samples_latest.csv"))
    latest_resource_samples_jsonl = str(getattr(monitor, "latest_jsonl_path", telemetry_dir / "step02_video_frame_resource_samples_latest.jsonl"))

    performance_summary = {
        "script_version": SCRIPT_VERSION,
        "run_invocation_id": run_invocation_id,
        "run_phase": run_phase,
        "telemetry_interval_seconds": args.telemetry_interval,
        "resource_sample_count": monitor.sample_count,
        "resource_samples_csv": resource_samples_csv,
        "resource_samples_jsonl": resource_samples_jsonl,
        "resource_samples_latest_csv": latest_resource_samples_csv,
        "resource_samples_latest_jsonl": latest_resource_samples_jsonl,
        "per_video_timing_csv": str(per_video_history_csv),
        "per_video_timing_latest_csv": str(per_video_latest_csv),
        "max_values": monitor.max_values,
        "avg_task_seconds_per_valid_frame_over_reported_videos": round(sum(avg_task_vals) / len(avg_task_vals), 6) if avg_task_vals else None,
        "avg_ffmpeg_seconds_per_valid_frame_over_reported_videos": round(sum(avg_ffmpeg_vals) / len(avg_ffmpeg_vals), 6) if avg_ffmpeg_vals else None,
    }
    performance_history_json = telemetry_history_dir / f"{name_prefix}_performance_summary.json"
    performance_history_md = telemetry_history_dir / f"{name_prefix}_performance_summary.md"
    performance_latest_json = telemetry_dir / "step02_video_frame_performance_summary_latest.json"
    performance_latest_md = telemetry_dir / "step02_video_frame_performance_summary_latest.md"
    performance_history_json.write_text(json.dumps(performance_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    performance_history_md.write_text(
        "# Step02 Video Frame Performance Summary\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- run_invocation_id: {run_invocation_id}\n"
        f"- run_phase: {run_phase}\n"
        f"- telemetry_interval_seconds: {args.telemetry_interval}\n"
        f"- resource_sample_count: {monitor.sample_count}\n"
        f"- resource_samples_csv: `{resource_samples_csv}`\n"
        f"- max_process_cpu_percent_sum: {monitor.max_values.get('process_cpu_percent_sum')}\n"
        f"- max_process_cpu_cores_estimated: {round(monitor.max_values.get('process_cpu_percent_sum', 0) / 100, 3)}\n"
        f"- max_process_rss_mb_sum: {monitor.max_values.get('process_rss_mb_sum')}\n"
        f"- max_ffmpeg_cpu_percent_sum: {monitor.max_values.get('ffmpeg_cpu_percent_sum')}\n"
        f"- max_ffmpeg_rss_mb_sum: {monitor.max_values.get('ffmpeg_rss_mb_sum')}\n"
        f"- max_swap_used_mb: {monitor.max_values.get('swap_used_mb')}\n"
        f"- avg_task_seconds_per_valid_frame: {performance_summary['avg_task_seconds_per_valid_frame_over_reported_videos']}\n"
        f"- avg_ffmpeg_seconds_per_valid_frame: {performance_summary['avg_ffmpeg_seconds_per_valid_frame_over_reported_videos']}\n",
        encoding="utf-8",
    )
    shutil.copy2(performance_history_json, performance_latest_json)
    shutil.copy2(performance_history_md, performance_latest_md)

    # Archive current Step02 latest manifests/reports for this invocation so first15/resume are auditable.
    archive_map = {
        MANIFEST_CSV: manifest_history_dir / f"{name_prefix}_video_frame_manifest.csv",
        VIDEO_REPORT_CSV: manifest_history_dir / f"{name_prefix}_video_extract_report.csv",
        SOURCE_IDENTITY_CSV: manifest_history_dir / f"{name_prefix}_source_video_identity.csv",
        MANIFEST_DIR / "video_frame_c4s_step01_queue_plan.csv": manifest_history_dir / f"{name_prefix}_plan.csv",
        SUMMARY_JSON: report_history_dir / f"{name_prefix}_summary.json",
        SUMMARY_MD: report_history_dir / f"{name_prefix}_summary.md",
    }
    for src, dst in archive_map.items():
        try:
            if src and src.exists():
                shutil.copy2(src, dst)
        except Exception:
            pass

    telemetry_api_contract = {
        "contract_name": "step02_resource_telemetry_api_v1",
        "status": "record_only_contract_no_dynamic_scheduler_in_this_step",
        "latest_status_json": str(api_dir / "step02_resource_status_latest.json"),
        "resource_samples_latest_csv": latest_resource_samples_csv,
        "resource_samples_latest_jsonl": latest_resource_samples_jsonl,
        "performance_summary_latest_json": str(performance_latest_json),
        "fields_for_future_scheduler": [
            "process_cpu_cores_estimated", "ffmpeg_pid_count", "ffmpeg_cpu_percent_sum",
            "process_rss_mb_sum", "swap_used_mb", "disk_read_kb_s", "disk_write_kb_s",
        ],
        "future_scheduler_boundary": "Future scheduler may read this API, but V4 does not dynamically change concurrency.",
    }
    (api_dir / "step02_resource_monitor_contract.json").write_text(json.dumps(telemetry_api_contract, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_resource_status = {
        "script_version": SCRIPT_VERSION,
        "run_invocation_id": run_invocation_id,
        "run_phase": run_phase,
        "created_at": utc_now_iso(),
        "stage": "finished",
        "resource_sample_count": monitor.sample_count,
        "max_values": monitor.max_values,
        "performance_summary_latest_json": str(performance_latest_json),
        "resource_samples_latest_csv": latest_resource_samples_csv,
    }
    (api_dir / "step02_resource_status_latest.json").write_text(json.dumps(latest_resource_status, ensure_ascii=False, indent=2), encoding="utf-8")

    final_report = {
        "script_version": SCRIPT_VERSION,
        "created_at": utc_now_iso(),
        "run_invocation_id": run_invocation_id,
        "run_phase": run_phase,
        "step01_workspace": str(args.step01_workspace),
        "output_dir": str(OUT),
        "frames_output_dir": str(FRAME_OUT),
        "manifests_dir": str(MANIFEST_DIR),
        "reports_dir": str(REPORT_DIR),
        "telemetry_dir": str(telemetry_dir),
        "final_report_dir": str(final_report_dir),
        "step02_summary": summary,
        "performance_summary": performance_summary,
        "telemetry_api": telemetry_api_contract,
        "resume_command": f"python3 step02_video_frame_c4s_id_from_step01_queue.py --step01-workspace '{args.step01_workspace}' --out '{OUT}' --concurrency {args.concurrency}",
    }
    final_history_json = final_history_dir / f"{name_prefix}_final_report.json"
    final_history_md = final_history_dir / f"{name_prefix}_final_report.md"
    final_latest_json = final_report_dir / "step02_video_frame_final_report_latest.json"
    final_latest_md = final_report_dir / "step02_video_frame_final_report_latest.md"
    final_history_json.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")
    final_history_md.write_text(
        "# Step02 Video Frame Final Report\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- run_invocation_id: {run_invocation_id}\n"
        f"- run_phase: {run_phase}\n"
        f"- step01_workspace: `{args.step01_workspace}`\n"
        f"- output_dir: `{OUT}`\n"
        f"- frames_output_dir: `{FRAME_OUT}`\n"
        f"- state_db: `{STATE_DB}`\n"
        f"- limit_new: {summary.get('limit_new')}\n"
        f"- eligible_video_queue_count: {summary.get('eligible_video_queue_count')}\n"
        f"- action_counts: {summary.get('action_counts')}\n"
        f"- frame_extract_status_counts: {summary.get('frame_extract_status_counts')}\n"
        f"- total_valid_jpg1280_count_in_report: {summary.get('total_valid_jpg1280_count_in_report')}\n"
        f"- short_video_single_frame_fallback_success_count: {summary.get('short_video_single_frame_fallback_success_count')}\n"
        f"- telemetry_dir: `{telemetry_dir}`\n"
        f"- telemetry_api_status: `{api_dir / 'step02_resource_status_latest.json'}`\n"
        f"- resource_samples_csv: `{resource_samples_csv}`\n"
        f"- per_video_timing_csv: `{per_video_history_csv}`\n"
        f"- max_process_cpu_cores_estimated: {round(monitor.max_values.get('process_cpu_percent_sum', 0) / 100, 3)}\n"
        f"- max_process_rss_mb_sum: {monitor.max_values.get('process_rss_mb_sum')}\n"
        f"- max_swap_used_mb: {monitor.max_values.get('swap_used_mb')}\n"
        f"- avg_task_seconds_per_valid_frame: {performance_summary['avg_task_seconds_per_valid_frame_over_reported_videos']}\n\n"
        "## Resume command\n\n"
        "```bash\n"
        f"python3 step02_video_frame_c4s_id_from_step01_queue.py --step01-workspace '{args.step01_workspace}' --out '{OUT}' --concurrency {args.concurrency}\n"
        "```\n",
        encoding="utf-8",
    )
    shutil.copy2(final_history_json, final_latest_json)
    shutil.copy2(final_history_md, final_latest_md)
    return performance_summary

def parse_args():
    p = argparse.ArgumentParser(description="Step02 C4S-ID-INC video frame extraction from Step01 video queue or project DB.")
    p.add_argument("--step01-workspace", default=None, type=Path, help="Legacy Step01 workspace directory. Not required when --db is provided.")
    p.add_argument("--db", default=None, type=Path, help="Project SQLite DB. When provided, video tasks are read from source_assets and results are written back to derived_assets/visual_units/model_runs.")
    p.add_argument("--db-audit-only", action="store_true", help="Only inspect project DB video inputs; do not run ffmpeg.")
    p.add_argument("--out", required=False, type=Path, help="Step02 output directory. Reuse same dir for resume.")
    p.add_argument("--limit-new", type=int, default=0, help="Max number of new/retry video jobs to start this run. 0 means no limit.")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Worker count. Default 4.")
    p.add_argument("--reset-state", action="store_true", help="Danger: delete Step02 output state/derived/manifests/reports before run.")
    p.add_argument("--telemetry-interval", type=float, default=2.0, help="Resource telemetry sampling interval seconds. Default 2.")
    p.add_argument("--disable-telemetry", action="store_true", help="Disable resource telemetry and final report generation.")
    p.add_argument("--run-invocation-id", default="", help="Optional explicit id for this Step02 invocation. Default: current local timestamp.")
    p.add_argument("--run-phase", choices=["auto", "first15", "first30", "resume", "manual"], default="auto", help="Report phase label. auto: first{limit_new} when --limit-new > 0 else resume.")
    p.add_argument("--no-open", action="store_true", help="Do not open output directory.")
    return p.parse_args()


# ============================================================
# 7. 主流程
# ============================================================
def main():
    args = parse_args()
    started_total = time.perf_counter()

    if args.limit_new < 0:
        raise SystemExit("[错误] --limit-new 不能小于 0")
    if args.concurrency < 1:
        raise SystemExit("[错误] --concurrency 不能小于 1")
    if not args.run_invocation_id:
        args.run_invocation_id = local_run_id()
    args.run_phase_resolved = (f"first{args.limit_new}" if args.limit_new > 0 else "resume") if args.run_phase == "auto" else args.run_phase

    if args.db_audit_only:
        if not args.db:
            raise SystemExit("[错误] --db-audit-only 需要同时提供 --db")
        print(json.dumps(project_db_audit(args.db), ensure_ascii=False, indent=2))
        return 0

    if not args.out:
        raise SystemExit("[错误] 必须提供 --out")
    if not args.db and not args.step01_workspace:
        raise SystemExit("[错误] 必须提供 --db 或 --step01-workspace")

    require_tool("ffmpeg")
    require_tool("ffprobe")
    require_tool("sips")

    if args.db:
        step01_workspace = Path(".")
        queue_path = Path(".")
    else:
        step01_workspace = args.step01_workspace.expanduser().resolve()
        queue_path = step01_workspace / "queues" / "process_queue_video.jsonl"
        if not queue_path.exists():
            raise SystemExit(f"[错误] 找不到 Step01 视频队列: {queue_path}")

    init_paths(args.out, args.concurrency)

    if args.reset_state and OUT.exists():
        print(f"[警告] reset-state 删除 Step02 输出目录: {OUT}", flush=True)
        shutil.rmtree(OUT)

    ensure_dirs()

    telemetry_dir = OUT / "telemetry"
    final_report_dir = OUT / "final_report"
    monitor = None
    if not args.disable_telemetry:
        monitor = ResourceMonitor(telemetry_dir, args.telemetry_interval, os.getpid(), args.run_invocation_id, args.run_phase_resolved)
        monitor.set_stage("step02_starting")
        monitor.start()

    conn = open_state_db()
    mark_stale_incomplete_as_interrupted(conn)

    print("== step02_video_frame_c4s_id_from_step01_queue start ==")
    print(f"script_version: {SCRIPT_VERSION}")
    print(f"run_invocation_id: {args.run_invocation_id}")
    print(f"run_phase: {args.run_phase_resolved}")
    print(f"step01_workspace: {step01_workspace}")
    print(f"input_mode: {input_mode}")
    print(f"db_path: {args.db if args.db else ''}")
    print(f"queue_path: {queue_path}")
    print(f"output_dir: {OUT}")
    print(f"state_db: {STATE_DB}")
    print(f"limit_new: {args.limit_new}")
    print(f"concurrency: {CONCURRENCY}")
    print(f"sampling: offset={SAMPLING_OFFSET_MS}ms interval={SAMPLING_INTERVAL_MS}ms jpg{MAX_EDGE_PX}")
    print("decode: primary=videotoolbox fallback=software")
    print(f"contract_id: {SAMPLING_CONTRACT_ID}")

    scan_t0 = time.perf_counter()
    if args.db:
        queue_rows_all = fetch_video_queue_from_project_db(args.db)
        queue_rows = list(queue_rows_all)
        input_mode = "db_source_assets"
    else:
        queue_rows_all = read_jsonl(queue_path)
        queue_rows = [
            r for r in queue_rows_all
            if r.get("media_kind") == "video" and r.get("next_action") == "process"
        ]
        input_mode = "legacy_step01_queue"
    scan_elapsed = time.perf_counter() - scan_t0

    print(f"step01_video_queue_total_rows: {len(queue_rows_all)}")
    print(f"eligible_video_queue_count: {len(queue_rows)}")

    video_reports = []
    frame_rows_all = []
    source_identity_rows = []
    jobs = []
    plan_rows = []
    action_counts = Counter()

    if monitor:
        monitor.set_stage("queue_fingerprint_and_incremental_plan")
    fingerprint_t0 = time.perf_counter()

    planned_new_count = 0
    for i, row in enumerate(queue_rows, start=1):
        src = Path(row["source_path"]).expanduser().resolve()
        base_plan = {
            "queue_index": i,
            "source_path": str(src),
            "source_relative_path": row.get("source_relative_path", ""),
            "parent_source_file_id": row.get("source_file_id", ""),
            "parent_source_content_id": row.get("source_content_id", ""),
            "media_kind": row.get("media_kind", ""),
            "next_action": row.get("next_action", ""),
            "plan_action": "",
            "plan_reason": "",
        }

        if not src.exists():
            base_plan.update({
                "plan_action": "cannot_process_source_missing",
                "plan_reason": "source_path_missing_at_processing_time",
            })
            plan_rows.append(base_plan)
            action_counts["source_missing_before_fingerprint"] += 1
            report = {
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "source_video_id": "",
                "source_video_id_version": SOURCE_VIDEO_ID_VERSION,
                "source_video_fingerprint_sha256": "",
                "source_video_path": str(src),
                "source_video_relative_path": row.get("source_relative_path", ""),
                "source_file_size_bytes": "",
                "source_file_mtime_ns": "",
                "sampling_contract_id": SAMPLING_CONTRACT_ID,
                "parent_source_file_id": row.get("source_file_id", ""),
                "parent_source_content_id": row.get("source_content_id", ""),
                "parent_source_path_at_processing_time": str(src),
                "parent_media_kind": row.get("media_kind", "video"),
                "duration_seconds": "",
                "expected_frame_count_estimate": "",
                "produced_frame_count": 0,
                "valid_jpg1280_count": 0,
                "invalid_jpg_count": 0,
                "missing_expected_frame_count": "",
                "tail_boundary_tolerated": False,
                "frame_extract_status": "source_missing_at_processing_time",
                "fallback_attempted": False,
                "decode_mode": "",
                "decode_attempts_json": "[]",
                "frame_output_dir": "",
                "frame_output_relative_dir": "",
                "task_action": "source_missing",
                "checkpoint_status": "failed",
                "concurrency": CONCURRENCY,
                "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
                "ffmpeg_returncode": "",
                "ffprobe_elapsed_seconds": 0,
                "ffmpeg_extract_elapsed_seconds": 0,
                "validation_elapsed_seconds": 0,
                "frame_sha256_elapsed_seconds": 0,
                "task_elapsed_seconds": 0,
                "ffprobe_error_tail": "",
                "ffmpeg_stderr_tail": "source_path_missing_at_processing_time",
            }
            video_reports.append(report)
            continue

        try:
            source_base = make_source_base_from_queue_row(row)
        except Exception as exc:
            base_plan.update({
                "plan_action": "cannot_process_fingerprint_failed",
                "plan_reason": repr(exc)[:500],
            })
            plan_rows.append(base_plan)
            action_counts["fingerprint_failed"] += 1
            continue

        source_identity_rows.append(source_base)
        source_video_id = source_base["source_video_id"]
        video_key = make_video_key(source_video_id)

        existing = get_video_row(conn, video_key)
        existing_output_dir = existing["output_dir"] if existing and existing["output_dir"] else ""
        out_dir = output_dir_for_source(source_base, existing_output_dir=existing_output_dir)

        ok, reason = verify_existing_artifacts(conn, video_key)
        if ok:
            report, frames = skipped_rows_from_db(conn, video_key, source_base)
            video_reports.append(report)
            frame_rows_all.extend(frames)
            action_counts["skip_already_processed"] += 1
            base_plan.update({
                "source_video_id": source_video_id,
                "video_key": video_key,
                "plan_action": "skip_already_processed",
                "plan_reason": reason,
                "output_dir": str(out_dir),
            })
            plan_rows.append(base_plan)
            print(f"[skip-existing {i}/{len(queue_rows)}] vid={source_video_id} reason={reason} file={source_base['source_video_relative_path']}", flush=True)
            continue

        if args.limit_new and planned_new_count >= args.limit_new:
            action_counts["not_started_limit_reached"] += 1
            base_plan.update({
                "source_video_id": source_video_id,
                "video_key": video_key,
                "plan_action": "not_started_limit_reached",
                "plan_reason": f"limit_new={args.limit_new}",
                "output_dir": str(out_dir),
            })
            plan_rows.append(base_plan)
            continue

        if existing and existing["status"] in RETRYABLE_STATUSES:
            task_action = f"retry_from_{existing['status']}"
        elif existing and existing["status"] in SUCCESS_STATUSES:
            task_action = f"reprocess_because_checkpoint_invalid:{reason}"
        else:
            task_action = "process_new"

        mark_queued(conn, source_base, video_key, out_dir, task_action)
        jobs.append({
            "queue_index": i,
            "src": src,
            "source_base": source_base,
            "video_key": video_key,
            "out_dir": out_dir,
            "task_action": task_action,
        })
        planned_new_count += 1
        action_counts[task_action] += 1
        base_plan.update({
            "source_video_id": source_video_id,
            "video_key": video_key,
            "plan_action": task_action,
            "plan_reason": reason,
            "output_dir": str(out_dir),
        })
        plan_rows.append(base_plan)

    fingerprint_elapsed = time.perf_counter() - fingerprint_t0

    print(
        f"incremental plan: skip={action_counts.get('skip_already_processed', 0)} "
        f"new_or_retry={len(jobs)} not_started_limit={action_counts.get('not_started_limit_reached', 0)}",
        flush=True,
    )

    if monitor:
        monitor.set_stage("video_frame_extraction")

    if jobs:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futures = {}
            total_jobs = len(jobs)
            for job_no, job in enumerate(jobs, start=1):
                mark_running(conn, job["video_key"])
                fut = ex.submit(
                    process_one,
                    job_no,
                    total_jobs,
                    job["src"],
                    job["source_base"],
                    job["out_dir"],
                    job["task_action"],
                )
                futures[fut] = job

            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    report, frame_rows, source_base = fut.result()
                except Exception as exc:
                    source_base = job["source_base"]
                    report = dict(source_base)
                    report.update({
                        "duration_seconds": "",
                        "expected_frame_count_estimate": "",
                        "produced_frame_count": 0,
                        "valid_jpg1280_count": 0,
                        "invalid_jpg_count": 0,
                        "missing_expected_frame_count": "",
                        "tail_boundary_tolerated": False,
                        "frame_extract_status": "frame_extract_failed_exception",
                        "fallback_attempted": False,
                        "decode_mode": "exception",
                        "decode_attempts_json": "[]",
                        "frame_output_dir": str(job["out_dir"]),
                        "frame_output_relative_dir": str(job["out_dir"].relative_to(OUT)),
                        "task_action": job["task_action"],
                        "checkpoint_status": "failed",
                        "concurrency": CONCURRENCY,
                        "scheduler_profile": "c4s_id_incremental_step01_queue_with_checkpoint",
                        "ffmpeg_returncode": "",
                        "ffprobe_elapsed_seconds": 0,
                        "ffmpeg_extract_elapsed_seconds": 0,
                        "validation_elapsed_seconds": 0,
                        "frame_sha256_elapsed_seconds": 0,
                        "task_elapsed_seconds": 0,
                        "ffprobe_error_tail": "",
                        "ffmpeg_stderr_tail": str(exc)[-1500:],
                    })
                    frame_rows = []
                    print(f"[failed-exception] vid={source_base['source_video_id']} error={exc}", flush=True)

                persist_completed_video(conn, job["video_key"], report, frame_rows, source_base)
                video_reports.append(report)
                frame_rows_all.extend(frame_rows)

    if monitor:
        monitor.set_stage("writing_step02_reports")
    total_elapsed = time.perf_counter() - started_total
    summary = write_reports(
        args,
        video_reports,
        frame_rows_all,
        source_identity_rows,
        plan_rows,
        scan_elapsed,
        fingerprint_elapsed,
        total_elapsed,
        len(queue_rows_all),
        len(queue_rows),
        action_counts,
    )
    summary["run_invocation_id"] = args.run_invocation_id
    summary["run_phase"] = args.run_phase_resolved
    summary["input_mode"] = input_mode

    if args.db:
        db_write_summary = write_step02_video_frames_to_project_db(args.db, args.run_invocation_id, video_reports, frame_rows_all)
        summary["db_write_summary"] = db_write_summary
        print("DB_WRITE_SUMMARY=" + json.dumps(db_write_summary, ensure_ascii=False), flush=True)

    conn.close()

    if monitor:
        monitor.set_stage("writing_telemetry_reports")
        time.sleep(min(args.telemetry_interval, 2.0))
        monitor.stop()
        write_telemetry_reports(args, summary, video_reports, telemetry_dir, final_report_dir, monitor)

    print("== step02_video_frame_c4s_id_from_step01_queue finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.no_open:
        try:
            subprocess.run(["open", str(OUT)], check=False)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main() or 0)
