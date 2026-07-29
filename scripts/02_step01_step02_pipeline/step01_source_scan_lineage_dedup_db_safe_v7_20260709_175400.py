#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step01 Source Scan + Lineage + Dedup + Telemetry
================================================

定位：
- 本地素材大整理项目 Step01 / 原 V0.1 扫描层改进版。
- 只读扫描一个或多个素材目录。
- 生成五类分类、路径重复、内容重复、canonical 队列、父子 lineage 契约。
- 内置 Step01 资源监控和最终报告。
- 不调用 Step02，不抽帧，不生成预览，不跑 OCR/YOLOE/Qwen-VL/Whisper。

输入：
    一个或多个 source 根目录。

输出：
    OUT/<stage_label>_<run_id>_<hash>/
      manifests/
      queues/
      lineage/
      reports/
      checkpoints/file_index/
      telemetry/
      final_report/

默认测试：
    source_dir: /Users/yourname/Documents/001DZLtest
    report_root: /Users/yourname/Documents/001DZLtestbaogao
    建议由终端命令传入 --out "$RUN_ROOT/01_step01_scan/workspace"

命名规则：
1. 脚本名：
    step01_source_scan_lineage_dedup.py

2. Step01 workspace 名：
    step01_source_scan_lineage_dedup_YYYYMMDD_HHMMSS_<hash>

3. Step01 输出目录：
    RUN_ROOT/01_step01_scan/workspace/step01_source_scan_lineage_dedup_YYYYMMDD_HHMMSS_<hash>/

4. Step01 最终报告：
    <step01_workspace>/final_report/final_run_report.md

安全规则：
- 原始素材只读。
- 输出目录不能放在任何输入素材目录内部。
- 不移动、不删除、不重命名、不写入源目录。
- 重复文件只记录，不进入处理队列。
- 后续模块只能读取 queues/process_queue_*.jsonl。

用法：
    python3 step01_source_scan_lineage_dedup.py \
      --out /path/to/RUN_ROOT/01_step01_scan/workspace \
      --stage-label step01_source_scan_lineage_dedup \
      --hash-all \
      /Users/yourname/Documents/001DZLtest
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import platform
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


SCRIPT_VERSION = "step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400"

VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".insv",
    ".mxf", ".braw", ".r3d", ".crm", ".ari"
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".hif",
    ".dng", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".rw2", ".raf", ".orf",
    ".webp", ".bmp", ".gif"
}

AUDIO_EXTS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".aiff", ".aif", ".ogg", ".opus", ".wma"
}

TEXT_EXTS = {
    ".txt", ".srt", ".vtt", ".ass", ".ssa", ".json", ".csv", ".tsv", ".xml",
    ".xmp", ".md", ".rtf", ".log", ".cue", ".edl", ".fcpxml", ".otio"
}

# Metadata sidecars are scanned and linked to their parent asset, but are not sent
# into media processing queues. This covers Adobe XMP and common editor sidecars.
METADATA_SIDECAR_EXTS = {
    ".xmp", ".xaml", ".aae", ".dop", ".cos", ".pp3", ".on1", ".lmnr"
}

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
# Default paths are fixed for reproducible local tests, but source roots remain selectable.
# In production/front-end use, pass one or more source roots as positional args or via SRC/SOURCE_ROOT.
SOURCE_ROOT_GUARD = Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE")  # legacy test source, read-only guard only
CURRENT_TEST_SOURCE_ROOT = Path("/Users/yourname/Documents/001DZLtest")      # current test source, read-only guard only
EXPECTED_PYTHON = Path("/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python")
DEFAULT_SOURCE_ROOT = CURRENT_TEST_SOURCE_ROOT
DEFAULT_OUT = TEST_OUTPUT_ROOT / "step01-source-scan-db-safe-v7_20260709_175400"
# V7 keeps the configured venv launcher path visible in preflight, while also reporting realpath.


PACKAGE_OR_ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".dmg", ".iso"
}

IGNORED_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini"
}

RESOURCE_FIELDS = [
    "timestamp",
    "elapsed_seconds",
    "stage",
    "pid",
    "process_cpu_percent",
    "process_cpu_cores_estimated",
    "process_rss_mb",
    "system_cpu_percent_sum_all_processes",
    "system_cpu_cores_estimated_all_processes",
    "memory_free_mb",
    "memory_active_mb",
    "memory_inactive_mb",
    "memory_wired_mb",
    "memory_compressed_mb",
    "swap_used_mb",
    "disk_read_kb_s",
    "disk_write_kb_s",
    "sample_error",
]


# ============================================================
# Utility
# ============================================================
def now_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_text(text: str, n: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def resolve_path(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(path)


def realpath_string(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except Exception:
        return str(path.absolute())


def run_cmd(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:
        return -1, "", repr(exc)


def write_jsonl(path: Path, rows: Iterable[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_csv(path: Path, row: dict, fieldnames: List[str], header_state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0 or not header_state.get(str(path))
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
            header_state[str(path)] = True
        w.writerow({k: row.get(k, "") for k in fieldnames})


# ============================================================
# Telemetry
# ============================================================
class TelemetryMonitor:
    def __init__(self, telemetry_dir: Path, interval: float):
        self.telemetry_dir = telemetry_dir
        self.interval = max(float(interval), 1.0)
        self.csv_path = telemetry_dir / "resource_samples.csv"
        self.jsonl_path = telemetry_dir / "resource_samples.jsonl"
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.stage = "idle"
        self.pid = os.getpid()
        self.start_time = time.time()
        self.thread = None
        self.header_state = {}
        self.sample_count = 0
        self.max_values = {
            "process_cpu_percent": 0.0,
            "process_rss_mb": 0.0,
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
        self.thread = threading.Thread(target=self._loop, name="step01_telemetry", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=self.interval + 2)

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
            append_csv(self.csv_path, row, RESOURCE_FIELDS, self.header_state)
            append_jsonl(self.jsonl_path, row)
            self.stop_event.wait(self.interval)

    def sample(self) -> dict:
        with self.lock:
            stage = self.stage
        elapsed = time.time() - self.start_time

        row = {k: "" for k in RESOURCE_FIELDS}
        row.update({
            "timestamp": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
            "stage": stage,
            "pid": self.pid,
            "sample_error": "",
        })

        errors = []
        try:
            row.update(get_process_stats(self.pid))
        except Exception as exc:
            errors.append(f"process_stats:{exc}")

        try:
            row.update(get_system_cpu_from_ps())
        except Exception as exc:
            errors.append(f"system_cpu:{exc}")

        try:
            row.update(get_vm_stat_memory())
        except Exception as exc:
            errors.append(f"vm_stat:{exc}")

        try:
            row.update(get_iostat_sample())
        except Exception as exc:
            errors.append(f"iostat:{exc}")

        row["sample_error"] = "|".join(errors)
        return row


def get_process_stats(pid: int) -> dict:
    rc, out, err = run_cmd(["ps", "-o", "pid=,pcpu=,rss=,comm=", "-p", str(pid)], timeout=3)
    cpu = 0.0
    rss_kb = 0.0
    if rc == 0 and out.strip():
        parts = out.strip().split(None, 3)
        if len(parts) >= 3:
            try:
                cpu = float(parts[1])
                rss_kb = float(parts[2])
            except Exception:
                pass
    return {
        "process_cpu_percent": round(cpu, 3),
        "process_cpu_cores_estimated": round(cpu / 100.0, 3),
        "process_rss_mb": round(rss_kb / 1024.0, 3),
    }


def get_system_cpu_from_ps() -> dict:
    rc, out, err = run_cmd(["bash", "-lc", "ps -A -o %cpu= | awk '{s+=$1} END {print s+0}'"], timeout=3)
    total = float(out.strip() or 0) if rc == 0 else 0.0
    return {
        "system_cpu_percent_sum_all_processes": round(total, 3),
        "system_cpu_cores_estimated_all_processes": round(total / 100.0, 3),
    }


def get_vm_stat_memory() -> dict:
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
        if not num:
            continue
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


def get_swap_used_mb() -> float:
    rc, out, err = run_cmd(["sysctl", "vm.swapusage"], timeout=3)
    if rc != 0:
        return 0.0
    m = re.search(r"used\s*=\s*([0-9.]+)M", out)
    return round(float(m.group(1)), 3) if m else 0.0


def get_iostat_sample() -> dict:
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


# ============================================================
# Workspace
# ============================================================
def make_workspace_dir(base_out: Path, stage_label: str, run_id: str, source_roots: List[Path]) -> Path:
    raw = "|".join([stage_label, run_id] + [str(x) for x in source_roots])
    run_hash = sha256_text(raw, 12)
    return base_out / f"{stage_label}_{run_id}_{run_hash}"


def prepare_workspace(workspace: Path) -> Dict[str, Path]:
    dirs = {
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "queues": workspace / "queues",
        "lineage": workspace / "lineage",
        "reports": workspace / "reports",
        "checkpoint_file_index": workspace / "checkpoints" / "file_index",
        "telemetry": workspace / "telemetry",
        "final_report": workspace / "final_report",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def validate_output_not_inside_sources(output_base: Path, source_roots: List[Path]):
    output_base = resolve_path(output_base)
    for src in source_roots:
        src = resolve_path(src)
        if is_relative_to(output_base, src):
            raise SystemExit(
                "错误：输出目录不能位于任何输入素材目录内部。这样会污染源目录并破坏“原始素材只读”原则。\n"
                f"output_base={output_base}\n"
                f"source_root={src}"
            )


# ============================================================
# Scan + dedup
# ============================================================
def classify_media_kind(path: Path, stat_ok: bool, size: Optional[int], stat_error: str = "") -> Tuple[str, str, str]:
    name = path.name.lower()
    ext = path.suffix.lower()

    if name in IGNORED_NAMES:
        return "unsupported", "unsupported", "system_sidecar_ignored_name"
    if not stat_ok:
        return "unsupported", "blocked", f"stat_failed:{stat_error[:160]}"
    if size is None:
        return "unsupported", "blocked", "size_unknown"
    if size == 0:
        return "unsupported", "unsupported", "zero_byte_file"
    if ext in VIDEO_EXTS:
        return "video", "supported", ""
    if ext in IMAGE_EXTS:
        return "image", "supported", ""
    if ext in AUDIO_EXTS:
        return "audio", "supported", ""
    if ext in METADATA_SIDECAR_EXTS:
        return "metadata_sidecar", "metadata_sidecar", "linked_metadata_sidecar_not_processing_queue"
    if ext in TEXT_EXTS:
        return "text", "supported", ""
    if ext in PACKAGE_OR_ARCHIVE_EXTS:
        return "unsupported", "unsupported", "archive_or_disk_image_not_processed_in_step01"
    if ext == "":
        return "unsupported", "unsupported", "missing_extension"
    return "unsupported", "unsupported", f"unsupported_extension:{ext}"


def stat_file(path: Path):
    try:
        st = path.stat()
        return True, st, ""
    except Exception as exc:
        return False, None, repr(exc)


def make_source_file_id(root_index: int, root: Path, path: Path, size: Optional[int], mtime_ns: Optional[int]) -> str:
    rel = safe_rel(path, root)
    raw = f"source_file_id_v1|root_index={root_index}|root={realpath_string(root)}|rel={rel}|size={size}|mtime_ns={mtime_ns}"
    return "sf_" + sha256_text(raw, 24)


def make_source_content_id(full_hash: Optional[str], fallback_source_file_id: str) -> str:
    if full_hash:
        return "sc_" + sha256_text(f"source_content_id_v1|sha256={full_hash}", 24)
    return "sc_unhashed_" + fallback_source_file_id.replace("sf_", "")


def scan_roots(source_roots: List[Path]) -> Tuple[List[dict], List[dict]]:
    root_rows: List[dict] = []
    file_rows: List[dict] = []
    seen_walk_paths = set()

    for root_index, root in enumerate(source_roots, start=1):
        root = resolve_path(root)
        root_id = "sr_" + sha256_text(f"source_root_v1|{root_index}|{root}", 24)
        root_row = {
            "record_type": "source_root",
            "source_root_id": root_id,
            "root_index": root_index,
            "source_root": str(root),
            "scan_status": "pending",
            "source_read_policy": "read_only_no_write_no_move_no_delete_no_rename",
        }

        if not root.exists():
            root_row.update({"scan_status": "root_missing", "error": "source_root_does_not_exist"})
            root_rows.append(root_row)
            continue

        root_row["scan_status"] = "ok"
        root_rows.append(root_row)

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            walk_key = str(path)
            if walk_key in seen_walk_paths:
                continue
            seen_walk_paths.add(walk_key)

            stat_ok, st, stat_error = stat_file(path)
            size = int(st.st_size) if st else None
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))) if st else None
            ctime_ns = int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1_000_000_000))) if st else None

            media_kind, support_status, support_reason = classify_media_kind(path, stat_ok, size, stat_error)
            source_relative_path = safe_rel(path, root)
            source_path = str(path)
            normalized_path = realpath_string(path)
            source_file_id = make_source_file_id(root_index, root, path, size, mtime_ns)

            file_rows.append({
                "record_type": "source_file",
                "source_root_id": root_id,
                "source_file_id": source_file_id,
                "source_path": source_path,
                "source_relative_path": source_relative_path,
                "source_root": str(root),
                "root_index": root_index,
                "normalized_path": normalized_path,
                "file_name": path.name,
                "extension": path.suffix.lower(),
                "media_kind": media_kind,
                "support_status": support_status,
                "support_reason": support_reason,
                "file_size_bytes": size,
                "mtime_ns": mtime_ns,
                "ctime_ns": ctime_ns,
                "stat_status": "ok" if stat_ok else "failed",
                "stat_error": stat_error,
                "source_read_policy": "read_only_no_write_no_move_no_delete_no_rename",
            })

    return root_rows, file_rows


def compute_dedup(source_rows: List[dict], hash_all: bool, telemetry: Optional[TelemetryMonitor]) -> Tuple[List[dict], List[dict], List[dict], List[dict], List[dict], List[dict], float]:
    unsupported_rows = [r for r in source_rows if r.get("support_status") != "supported"]

    normalized_groups: Dict[str, List[dict]] = defaultdict(list)
    for r in source_rows:
        normalized_groups[r["normalized_path"]].append(r)

    path_duplicate_alias_ids = set()
    path_duplicate_group_by_file = {}
    path_duplicate_group_rows = []

    for normalized_path, group in normalized_groups.items():
        if len(group) <= 1:
            continue
        group_sorted = sorted(group, key=lambda x: (str(x.get("source_path")), str(x.get("source_file_id"))))
        canonical = group_sorted[0]
        group_id = "pdg_" + sha256_text(f"path_duplicate_group_v1|{normalized_path}", 24)

        path_duplicate_group_rows.append({
            "duplicate_group_id": group_id,
            "duplicate_type": "path_duplicate",
            "canonical_source_file_id": canonical["source_file_id"],
            "canonical_source_path": canonical["source_path"],
            "normalized_path": normalized_path,
            "member_count": len(group_sorted),
            "member_source_file_ids": [x["source_file_id"] for x in group_sorted],
            "member_paths": [x["source_path"] for x in group_sorted],
            "action": "process_canonical_only_skip_aliases",
        })

        for member in group_sorted:
            path_duplicate_group_by_file[member["source_file_id"]] = {
                "path_duplicate_group_id": group_id,
                "path_duplicate_canonical_source_file_id": canonical["source_file_id"],
            }
            if member["source_file_id"] != canonical["source_file_id"]:
                path_duplicate_alias_ids.add(member["source_file_id"])

    size_groups: Dict[int, List[dict]] = defaultdict(list)
    for r in source_rows:
        if r.get("support_status") != "supported":
            continue
        if r["source_file_id"] in path_duplicate_alias_ids:
            continue
        size = r.get("file_size_bytes")
        if size is not None:
            size_groups[int(size)].append(r)

    rows_to_hash = []
    for _, group in size_groups.items():
        if hash_all or len(group) > 1:
            rows_to_hash.extend(group)

    if telemetry:
        telemetry.set_stage("hash_content")
    hash_started = time.perf_counter()
    hash_by_source_file_id: Dict[str, str] = {}
    for index, r in enumerate(rows_to_hash, start=1):
        path = Path(r["source_path"])
        try:
            digest = sha256_file(path)
            hash_by_source_file_id[r["source_file_id"]] = digest
            print(f"[hash {index}/{len(rows_to_hash)}] {r['source_relative_path']}", flush=True)
        except Exception as exc:
            r["support_status"] = "blocked"
            r["support_reason"] = f"hash_failed:{repr(exc)[:160]}"
    hash_elapsed = time.perf_counter() - hash_started

    content_hash_groups: Dict[str, List[dict]] = defaultdict(list)
    for r in source_rows:
        h = hash_by_source_file_id.get(r["source_file_id"])
        if h:
            content_hash_groups[h].append(r)

    content_duplicate_alias_ids = set()
    content_duplicate_group_rows = []
    skipped_duplicate_rows = []

    for full_hash, group in content_hash_groups.items():
        if len(group) <= 1:
            continue

        group_sorted = sorted(
            group,
            key=lambda x: (
                len(str(x.get("source_relative_path", ""))),
                str(x.get("source_relative_path", "")),
                str(x.get("source_path", "")),
            ),
        )
        canonical = group_sorted[0]
        source_content_id = make_source_content_id(full_hash, canonical["source_file_id"])
        group_id = "cdg_" + sha256_text(f"content_duplicate_group_v1|sha256={full_hash}", 24)

        content_duplicate_group_rows.append({
            "duplicate_group_id": group_id,
            "duplicate_type": "content_duplicate_confirmed",
            "source_content_id": source_content_id,
            "content_sha256": full_hash,
            "file_size_bytes": canonical.get("file_size_bytes"),
            "canonical_source_file_id": canonical["source_file_id"],
            "canonical_source_path": canonical["source_path"],
            "member_count": len(group_sorted),
            "member_source_file_ids": [x["source_file_id"] for x in group_sorted],
            "member_paths": [x["source_path"] for x in group_sorted],
            "action": "process_canonical_only_skip_aliases",
        })

        for member in group_sorted:
            if member["source_file_id"] == canonical["source_file_id"]:
                continue
            content_duplicate_alias_ids.add(member["source_file_id"])
            skipped_duplicate_rows.append({
                "source_file_id": member["source_file_id"],
                "source_path": member["source_path"],
                "source_relative_path": member["source_relative_path"],
                "media_kind": member["media_kind"],
                "duplicate_type": "content_duplicate_confirmed",
                "duplicate_group_id": group_id,
                "duplicate_of_source_file_id": canonical["source_file_id"],
                "duplicate_of_source_path": canonical["source_path"],
                "source_content_id": source_content_id,
                "content_sha256": full_hash,
                "next_action": "skip_high_cost_processing",
                "reason": "same_full_sha256_as_canonical",
            })

    for r in source_rows:
        if r["source_file_id"] not in path_duplicate_alias_ids:
            continue
        info = path_duplicate_group_by_file.get(r["source_file_id"], {})
        skipped_duplicate_rows.append({
            "source_file_id": r["source_file_id"],
            "source_path": r["source_path"],
            "source_relative_path": r["source_relative_path"],
            "media_kind": r["media_kind"],
            "duplicate_type": "path_duplicate",
            "duplicate_group_id": info.get("path_duplicate_group_id", ""),
            "duplicate_of_source_file_id": info.get("path_duplicate_canonical_source_file_id", ""),
            "duplicate_of_source_path": "",
            "source_content_id": "",
            "content_sha256": "",
            "next_action": "skip_high_cost_processing",
            "reason": "same_normalized_path_seen_more_than_once",
        })

    duplicate_group_rows = path_duplicate_group_rows + content_duplicate_group_rows
    enriched_manifest_rows = []
    canonical_rows = []
    lineage_root_rows = []
    skipped_by_id = {r["source_file_id"]: r for r in skipped_duplicate_rows}

    for r in source_rows:
        rr = dict(r)
        full_hash = hash_by_source_file_id.get(r["source_file_id"])
        source_content_id = make_source_content_id(full_hash, r["source_file_id"])

        rr["content_sha256"] = full_hash or ""
        rr["source_content_id"] = source_content_id
        rr["path_duplicate_group_id"] = path_duplicate_group_by_file.get(r["source_file_id"], {}).get("path_duplicate_group_id", "")
        rr["path_duplicate_canonical_source_file_id"] = path_duplicate_group_by_file.get(r["source_file_id"], {}).get("path_duplicate_canonical_source_file_id", "")

        if r.get("support_status") != "supported":
            dedup_role = "unsupported_or_blocked"
            next_action = "do_not_process"
            canonical_source_file_id = ""
        elif r["source_file_id"] in path_duplicate_alias_ids:
            dedup_role = "path_duplicate_alias"
            next_action = "skip_high_cost_processing"
            canonical_source_file_id = rr["path_duplicate_canonical_source_file_id"]
        elif r["source_file_id"] in content_duplicate_alias_ids:
            dedup_role = "content_duplicate_alias"
            next_action = "skip_high_cost_processing"
            canonical_source_file_id = skipped_by_id.get(r["source_file_id"], {}).get("duplicate_of_source_file_id", "")
        else:
            dedup_role = "canonical"
            next_action = "process"
            canonical_source_file_id = r["source_file_id"]

        rr["dedup_role"] = dedup_role
        rr["next_action"] = next_action
        rr["canonical_source_file_id"] = canonical_source_file_id
        enriched_manifest_rows.append(rr)

        if dedup_role == "canonical" and r.get("support_status") == "supported":
            canonical = {
                "source_file_id": r["source_file_id"],
                "source_content_id": source_content_id,
                "content_sha256": full_hash or "",
                "source_path": r["source_path"],
                "source_relative_path": r["source_relative_path"],
                "normalized_path": r["normalized_path"],
                "media_kind": r["media_kind"],
                "extension": r["extension"],
                "file_size_bytes": r["file_size_bytes"],
                "mtime_ns": r["mtime_ns"],
                "dedup_role": "canonical",
                "next_action": "process",
                "required_parent_fields_for_derived_outputs": [
                    "parent_source_file_id",
                    "parent_source_content_id",
                    "parent_source_path_at_processing_time",
                    "parent_media_kind",
                ],
            }
            canonical_rows.append(canonical)

            lineage_root_rows.append({
                "lineage_role": "root_source",
                "source_file_id": r["source_file_id"],
                "source_content_id": source_content_id,
                "source_path": r["source_path"],
                "media_kind": r["media_kind"],
                "allowed_downstream": True,
                "downstream_queue": f"queues/process_queue_{r['media_kind']}.jsonl",
            })

    return enriched_manifest_rows, duplicate_group_rows, skipped_duplicate_rows, canonical_rows, unsupported_rows, lineage_root_rows, hash_elapsed


def write_kind_queues(dirs: Dict[str, Path], canonical_rows: List[dict]) -> Dict[str, int]:
    counts = {}
    for kind in ["video", "image", "audio", "text"]:
        rows = [r for r in canonical_rows if r.get("media_kind") == kind]
        counts[kind] = len(rows)
        write_jsonl(dirs["queues"] / f"process_queue_{kind}.jsonl", rows)
        write_csv(dirs["queues"] / f"process_queue_{kind}.csv", rows)
    return counts


def write_lineage_contract(dirs: Dict[str, Path]):
    contract = {
        "contract_name": "source_lineage_contract_v1",
        "purpose": "All downstream derived artifacts must preserve root source identity.",
        "root_source_fields": {
            "source_file_id": "stable id for a path-level original source file record",
            "source_content_id": "stable id for confirmed file content when sha256 exists",
            "source_path": "original file path at scan time",
            "media_kind": "video/image/audio/text",
        },
        "required_parent_fields_for_every_derived_artifact": [
            "parent_source_file_id",
            "parent_source_content_id",
            "parent_source_path_at_processing_time",
            "parent_media_kind",
        ],
        "derived_folder_rule": {
            "video_frames": "Step02 output: derived/video_frames/...",
            "image_previews": "future Step image output",
            "audio_extracts": "future Step audio output",
            "ocr_results": "future OCR output",
            "yoloe_results": "future YOLOE output",
            "qwen_vl_results": "future Qwen-VL output",
        },
    }
    write_json(dirs["lineage"] / "source_lineage_contract.json", contract)


def write_file_index(dirs: Dict[str, Path], stage_label: str):
    rows = []
    for p in sorted(dirs["workspace"].rglob("*")):
        if not p.is_file():
            continue
        rows.append({
            "path": str(p),
            "relative_path": str(p.relative_to(dirs["workspace"])),
            "size_bytes": p.stat().st_size,
            "record_type": "local_record_only_file_index",
            "zip_path": None,
        })
    file_index_path = dirs["checkpoint_file_index"] / f"{stage_label}_record_only_file_index.jsonl"
    write_jsonl(file_index_path, rows)
    return file_index_path, len(rows)


def write_reports(
    dirs: Dict[str, Path],
    args,
    source_roots: List[Path],
    root_rows: List[dict],
    manifest_rows: List[dict],
    duplicate_groups: List[dict],
    skipped_duplicates: List[dict],
    canonical_rows: List[dict],
    lineage_root_rows: List[dict],
    queue_counts: Dict[str, int],
    file_index_path: Path,
    file_index_count: int,
    stage_timings: List[dict],
    telemetry: TelemetryMonitor,
):
    media_counts = Counter(r.get("media_kind", "") for r in manifest_rows)
    support_counts = Counter(r.get("support_status", "") for r in manifest_rows)
    dedup_counts = Counter(r.get("dedup_role", "") for r in manifest_rows)
    duplicate_type_counts = Counter(r.get("duplicate_type", "") for r in skipped_duplicates)

    summary = {
        "script_version": SCRIPT_VERSION,
        "stage_label": args.stage_label,
        "run_id": args.run_id,
        "workspace": str(dirs["workspace"]),
        "source_roots": [str(x) for x in source_roots],
        "source_safety": "source_read_only_no_write_no_move_no_delete_no_rename",
        "hash_policy": "full_hash_all_supported_files" if args.hash_all else "full_hash_only_same_size_candidates",
        "total_source_roots": len(root_rows),
        "total_source_files": len(manifest_rows),
        "media_kind_counts": dict(media_counts),
        "support_status_counts": dict(support_counts),
        "dedup_role_counts": dict(dedup_counts),
        "duplicate_group_count": len(duplicate_groups),
        "skipped_duplicate_count": len(skipped_duplicates),
        "skipped_duplicate_type_counts": dict(duplicate_type_counts),
        "canonical_process_count": len(canonical_rows),
        "lineage_root_count": len(lineage_root_rows),
        "process_queue_counts": queue_counts,
        "stage_timings": stage_timings,
        "telemetry": {
            "interval_seconds": args.telemetry_interval,
            "sample_count": telemetry.sample_count,
            "resource_samples_csv": str(dirs["telemetry"] / "resource_samples.csv"),
            "resource_samples_jsonl": str(dirs["telemetry"] / "resource_samples.jsonl"),
            "max_values": telemetry.max_values,
        },
        "file_index_path": str(file_index_path),
        "file_index_record_count": file_index_count,
        "outputs": {
            "manifests": str(dirs["manifests"]),
            "queues": str(dirs["queues"]),
            "lineage": str(dirs["lineage"]),
            "reports": str(dirs["reports"]),
            "telemetry": str(dirs["telemetry"]),
            "final_report": str(dirs["final_report"]),
            "checkpoint_file_index": str(dirs["checkpoint_file_index"]),
        },
        "next_stage_rule": "Only queues/process_queue_*.jsonl may be consumed by downstream stages. Duplicate aliases must not enter high-cost processing.",
        "platform": {
            "python": sys.version,
            "system": platform.platform(),
        },
    }

    write_json(dirs["reports"] / "source_scan_summary.json", summary)
    (dirs["reports"] / "source_scan_summary.md").write_text(
        "# Step01 Source Scan + Lineage + Dedup Summary\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- stage_label: {args.stage_label}\n"
        f"- run_id: {args.run_id}\n"
        f"- workspace: {dirs['workspace']}\n"
        f"- source_safety: {summary['source_safety']}\n"
        f"- hash_policy: {summary['hash_policy']}\n"
        f"- total_source_files: {summary['total_source_files']}\n"
        f"- media_kind_counts: {summary['media_kind_counts']}\n"
        f"- support_status_counts: {summary['support_status_counts']}\n"
        f"- dedup_role_counts: {summary['dedup_role_counts']}\n"
        f"- duplicate_group_count: {summary['duplicate_group_count']}\n"
        f"- skipped_duplicate_count: {summary['skipped_duplicate_count']}\n"
        f"- canonical_process_count: {summary['canonical_process_count']}\n"
        f"- process_queue_counts: {summary['process_queue_counts']}\n"
        f"- telemetry_sample_count: {telemetry.sample_count}\n"
        f"- file_index_path: {file_index_path}\n\n"
        "## Rule\n\n"
        "Downstream modules must read only `queues/process_queue_*.jsonl`. "
        "Every derived artifact must carry `parent_source_file_id` and `parent_source_content_id`.\n",
        encoding="utf-8",
    )

    write_json(dirs["telemetry"] / "stage_timing.json", {
        "script_version": SCRIPT_VERSION,
        "workspace": str(dirs["workspace"]),
        "stage_timings": stage_timings,
    })

    performance_summary = {
        "telemetry_interval_seconds": args.telemetry_interval,
        "resource_sample_count": telemetry.sample_count,
        "resource_samples_csv": str(dirs["telemetry"] / "resource_samples.csv"),
        "resource_samples_jsonl": str(dirs["telemetry"] / "resource_samples.jsonl"),
        "stage_timing_json": str(dirs["telemetry"] / "stage_timing.json"),
        "max_values": telemetry.max_values,
    }
    write_json(dirs["telemetry"] / "performance_summary.json", performance_summary)
    (dirs["telemetry"] / "performance_summary.md").write_text(
        "# Step01 Performance Summary\n\n"
        f"- telemetry_interval_seconds: {args.telemetry_interval}\n"
        f"- resource_sample_count: {telemetry.sample_count}\n"
        f"- max_process_cpu_percent: {telemetry.max_values.get('process_cpu_percent')}\n"
        f"- max_process_cpu_cores_estimated: {round(telemetry.max_values.get('process_cpu_percent', 0) / 100, 3)}\n"
        f"- max_process_rss_mb: {telemetry.max_values.get('process_rss_mb')}\n"
        f"- max_system_cpu_percent_sum_all_processes: {telemetry.max_values.get('system_cpu_percent_sum_all_processes')}\n"
        f"- max_swap_used_mb: {telemetry.max_values.get('swap_used_mb')}\n"
        f"- max_disk_read_kb_s: {telemetry.max_values.get('disk_read_kb_s')}\n"
        f"- max_disk_write_kb_s: {telemetry.max_values.get('disk_write_kb_s')}\n",
        encoding="utf-8",
    )

    stage_lines = [
        f"- {r['stage']}: {r['elapsed_seconds']} sec"
        for r in stage_timings
    ]

    final = {
        "script_version": SCRIPT_VERSION,
        "created_at": now_iso(),
        "workspace": str(dirs["workspace"]),
        "source_roots": [str(x) for x in source_roots],
        "frames_next_stage_queue": str(dirs["queues"] / "process_queue_video.jsonl"),
        "step01_summary": summary,
        "performance_summary": performance_summary,
        "step02_expected_input": str(dirs["queues"] / "process_queue_video.jsonl"),
    }
    write_json(dirs["final_report"] / "final_run_report.json", final)
    (dirs["final_report"] / "final_run_report.md").write_text(
        "# Step01 Final Run Report\n\n"
        f"- script_version: {SCRIPT_VERSION}\n"
        f"- created_at: {now_iso()}\n"
        f"- workspace: `{dirs['workspace']}`\n"
        f"- source_roots: {summary['source_roots']}\n"
        f"- source_safety: {summary['source_safety']}\n\n"
        "## Stage timing\n\n"
        + "\n".join(stage_lines)
        + "\n\n## Scan result\n\n"
        f"- total_source_files: {summary['total_source_files']}\n"
        f"- media_kind_counts: {summary['media_kind_counts']}\n"
        f"- support_status_counts: {summary['support_status_counts']}\n"
        f"- dedup_role_counts: {summary['dedup_role_counts']}\n"
        f"- duplicate_group_count: {summary['duplicate_group_count']}\n"
        f"- skipped_duplicate_count: {summary['skipped_duplicate_count']}\n"
        f"- canonical_process_count: {summary['canonical_process_count']}\n"
        f"- process_queue_counts: {summary['process_queue_counts']}\n\n"
        "## Output\n\n"
        f"- video_queue: `{dirs['queues'] / 'process_queue_video.jsonl'}`\n"
        f"- image_queue: `{dirs['queues'] / 'process_queue_image.jsonl'}`\n"
        f"- audio_queue: `{dirs['queues'] / 'process_queue_audio.jsonl'}`\n"
        f"- text_queue: `{dirs['queues'] / 'process_queue_text.jsonl'}`\n"
        f"- telemetry: `{dirs['telemetry']}`\n"
        f"- summary_json: `{dirs['reports'] / 'source_scan_summary.json'}`\n\n"
        "## Next stage\n\n"
        "Step02 must consume only `queues/process_queue_video.jsonl` from this workspace.\n",
        encoding="utf-8",
    )

    return summary


def timed_stage(stage_timings: List[dict], telemetry: TelemetryMonitor, stage: str, fn):
    telemetry.set_stage(stage)
    t0 = time.perf_counter()
    start_iso = now_iso()
    result = fn()
    elapsed = time.perf_counter() - t0
    stage_timings.append({
        "stage": stage,
        "start_time": start_iso,
        "end_time": now_iso(),
        "elapsed_seconds": round(elapsed, 3),
    })
    return result



# ============================================================
# Runtime preflight / offline guard
# ============================================================
def install_offline_env() -> Dict[str, str]:
    """Set local/offline environment flags. Step01 does not use models, but these
    guards keep accidental imports/checkers from trying network access."""
    values = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "ULTRALYTICS_OFFLINE": "1",
        "NO_ALBUMENTATIONS_UPDATE": "1",
        "YOLO_CONFIG_DIR": str(TEST_OUTPUT_ROOT / "ultralytics-offline-config"),
    }
    for k, v in values.items():
        os.environ[k] = v
    Path(values["YOLO_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    return values


def dependency_status(module_name: str, optional: bool = False) -> dict:
    try:
        mod = __import__(module_name)
        version = getattr(mod, "__version__", "stdlib")
        return {"ok": True, "optional": optional, "version": version, "error": ""}
    except Exception as exc:
        return {"ok": False, "optional": optional, "version": "", "error": repr(exc)}


def asset_status(path: Path) -> dict:
    rp = resolve_path(path)
    try:
        exists = rp.exists()
        size = rp.stat().st_size if exists and rp.is_file() else None
    except Exception:
        exists, size = False, None
    return {"path": str(rp), "exists": bool(exists), "size_bytes": size}


def launcher_asset_status(path: Path) -> dict:
    """Report the venv launcher path exactly as configured, plus its realpath.

    This avoids hiding the project venv behind Homebrew's resolved Python path.
    """
    p = Path(path)
    rp = resolve_path(p)
    try:
        exists = p.exists()
        size = p.stat().st_size if exists and p.is_file() else None
    except Exception:
        exists, size = False, None
    return {
        "path": str(p),
        "realpath": str(rp),
        "exists": bool(exists),
        "size_bytes": size,
    }


def runtime_preflight(source_roots: Optional[List[Path]] = None, db_path: Optional[Path] = None,
                      out_path: Optional[Path] = None, scan_macos_tags: bool = False) -> dict:
    offline_env = install_offline_env()
    source_roots = source_roots or []
    python_executable = Path(sys.executable)
    python_realpath = resolve_path(python_executable)
    expected_python_realpath = resolve_path(EXPECTED_PYTHON)
    bundled_app_python = (
        "/Contents/Frameworks/PipelinePython" in str(python_realpath)
        and "/Python.framework/Versions/" in str(python_realpath)
    )
    deps = {
        "sqlite3": dependency_status("sqlite3", optional=False),
        "csv": dependency_status("csv", optional=False),
        "json": dependency_status("json", optional=False),
        "hashlib": dependency_status("hashlib", optional=False),
        "pathlib": dependency_status("pathlib", optional=False),
        "subprocess": dependency_status("subprocess", optional=False),
        "platform": dependency_status("platform", optional=False),
    }
    missing_required = [k for k, v in deps.items() if not v["ok"] and not v.get("optional")]
    mdls_path = shutil_which("mdls") if platform.system() == "Darwin" else ""
    assets = {
        "project_root": asset_status(PROJECT_ROOT),
        "test_output_root": asset_status(TEST_OUTPUT_ROOT),
        "legacy_source_root_read_protected": asset_status(SOURCE_ROOT_GUARD),
        "current_test_source_root_read_protected": asset_status(CURRENT_TEST_SOURCE_ROOT),
        "expected_python_launcher": launcher_asset_status(EXPECTED_PYTHON),
        "expected_python_realpath": asset_status(expected_python_realpath),
    }
    if db_path is not None:
        assets["db"] = asset_status(db_path)
    if out_path is not None:
        assets["output_base_parent"] = asset_status(Path(out_path).parent)
    for i, src in enumerate(source_roots):
        assets[f"source_root_{i}"] = asset_status(src)

    blockers = []
    if missing_required:
        blockers.append("MISSING_REQUIRED_DEPENDENCIES:" + ",".join(missing_required))
    if not PROJECT_ROOT.exists():
        blockers.append(f"PROJECT_ROOT_MISSING:{PROJECT_ROOT}")
    if not TEST_OUTPUT_ROOT.exists():
        blockers.append(f"TEST_OUTPUT_ROOT_MISSING:{TEST_OUTPUT_ROOT}")
    if not EXPECTED_PYTHON.exists() and not bundled_app_python:
        blockers.append(f"EXPECTED_PYTHON_MISSING:{EXPECTED_PYTHON}")
    for src in source_roots:
        if not resolve_path(src).exists():
            blockers.append(f"SOURCE_ROOT_MISSING:{resolve_path(src)}")
    if scan_macos_tags and platform.system() == "Darwin" and not mdls_path:
        blockers.append("SCAN_MAC_TAGS_REQUESTED_BUT_MDLS_MISSING")

    expected_python_match = (
        str(python_executable) == str(EXPECTED_PYTHON)
        or str(python_realpath) == str(expected_python_realpath)
        or bundled_app_python
    )

    return {
        "script_version": SCRIPT_VERSION,
        "python_executable": str(python_executable),
        "python_realpath": str(python_realpath),
        "expected_python": str(EXPECTED_PYTHON),
        "expected_python_realpath": str(expected_python_realpath),
        "expected_python_match": expected_python_match,
        "bundled_app_python": bundled_app_python,
        "expected_script_local": str(PROJECT_ROOT / "scripts/02_step01_step02_pipeline" / f"{SCRIPT_VERSION}.py"),
        "project_root": str(PROJECT_ROOT),
        "test_output_root": str(TEST_OUTPUT_ROOT),
        "default_source_root": str(DEFAULT_SOURCE_ROOT),
        "source_root_selection_policy": "selectable_by_cli_positional_args_or_SRC_SOURCE_ROOT_env; default_is_current_test_source_only",
        "default_db": str(DEFAULT_DB),
        "default_out": str(DEFAULT_OUT),
        "model_usage_policy": "not_used_by_step01_source_scan",
        "source_media_policy": "read_only_no_move_no_rename_no_delete_no_metadata_write",
        "derived_write_policy": "write_only_to_project_or_test_output_roots",
        "required_local_assets": {
            "project_root": str(PROJECT_ROOT),
            "test_output_root": str(TEST_OUTPUT_ROOT),
            "current_test_source_root_read_protected": str(CURRENT_TEST_SOURCE_ROOT),
            "legacy_source_root_read_protected": str(SOURCE_ROOT_GUARD),
            "expected_python_launcher": str(EXPECTED_PYTHON),
            "expected_python_realpath": str(expected_python_realpath),
        },
        "assets": assets,
        "dependencies": deps,
        "missing_required_dependencies": missing_required,
        "macos_tools": {"mdls": mdls_path, "scan_macos_tags_requested": bool(scan_macos_tags)},
        "offline_env": offline_env,
        "safety": {
            "network": "blocked_by_offline_env_not_used_by_step01",
            "download": "not_used",
            "dependency_install": "not_used",
            "source_media_read": "read_only_scan_stat_hash_optional_mdls_readonly",
            "source_media_write": "blocked_by_design_and_output_path_guard",
            "model_loading": "not_used_by_step01_source_scan",
        },
        "blockers": blockers,
    }

# ============================================================
# CLI
# ============================================================

def is_allowed_project_output_path(path: Path) -> bool:
    """Allow DB/output writes only inside the project or test-output roots."""
    rp = resolve_path(path)
    allowed_roots = [
        resolve_path(PROJECT_ROOT),
        resolve_path(TEST_OUTPUT_ROOT),
    ]
    return any(is_relative_to(rp, root) or rp == root for root in allowed_roots)


def validate_db_path(db_path: Path, source_roots: List[Path]):
    db_path = resolve_path(db_path)
    if any(is_relative_to(db_path, resolve_path(src)) or db_path == resolve_path(src) for src in source_roots):
        raise SystemExit(f"BLOCKED_DB_INSIDE_SOURCE_ROOT: {db_path}")
    if is_relative_to(db_path, SOURCE_ROOT_GUARD) or db_path == SOURCE_ROOT_GUARD:
        raise SystemExit(f"BLOCKED_DB_INSIDE_RAW_SOURCE_GUARD: {db_path}")
    if not is_allowed_project_output_path(db_path):
        raise SystemExit(
            "BLOCKED_DB_PATH_OUTSIDE_PROJECT_OUTPUT: "
            f"{db_path} must be inside {PROJECT_ROOT} or {TEST_OUTPUT_ROOT}"
        )


def path_folder(path_text: str) -> str:
    try:
        return str(Path(path_text).parent)
    except Exception:
        return ""


def path_stem_key(path_text: str) -> str:
    p = Path(path_text)
    return str(p.with_suffix("")).lower()


def parse_macos_tag(raw: str) -> Tuple[str, str]:
    """Best-effort parse for mdls/xattr tag text. Returns (tag_name, tag_color)."""
    s = str(raw or "").strip()
    if not s:
        return "", ""
    s = s.strip('"')
    parts = [x.strip() for x in re.split(r"\\n|\n", s) if x.strip()]
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), parts[-1]
    return s, ""


def shutil_which(name: str) -> str:
    # local small replacement to avoid adding dependency or installing anything.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / name
        if cand.exists() and os.access(str(cand), os.X_OK):
            return str(cand)
    return ""


def read_macos_finder_tags(path: Path, enabled: bool) -> Tuple[List[dict], str]:
    """Read-only macOS tag probe. Does not write xattrs. Safe to fail closed."""
    if not enabled or platform.system() != "Darwin":
        return [], "disabled"
    mdls = shutil_which("mdls")
    if not mdls:
        return [], "mdls_missing"
    try:
        p = subprocess.run(
            [mdls, "-raw", "-name", "kMDItemUserTags", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
        raw = (p.stdout or "").strip()
        if p.returncode != 0:
            return [], f"mdls_error:{(p.stderr or '').strip()[:120]}"
        if raw in ("", "(null)", "null"):
            return [], "none"
        cleaned = raw.replace("(", "").replace(")", "").replace(",", "\n")
        tags = []
        for part in cleaned.splitlines():
            part = part.strip().strip('"')
            if not part:
                continue
            name, color = parse_macos_tag(part)
            tags.append({"tag_raw": part, "tag_name": name, "tag_color": color})
        return tags, "ok"
    except Exception as exc:
        return [], f"tag_read_failed:{repr(exc)[:120]}"


def enrich_with_local_metadata(manifest_rows: List[dict], scan_macos_tags: bool) -> Tuple[List[dict], List[dict]]:
    """Attach folder/stem keys and optional Finder tags to already scanned rows."""
    finder_tag_rows: List[dict] = []
    for r in manifest_rows:
        p = Path(r.get("source_path", ""))
        r["folder_path"] = path_folder(r.get("source_path", ""))
        r["file_stem"] = p.stem
        r["stem_key"] = path_stem_key(r.get("source_path", ""))
        r["is_metadata_sidecar"] = 1 if r.get("media_kind") == "metadata_sidecar" else 0
        tags, status = read_macos_finder_tags(p, scan_macos_tags)
        r["finder_tag_status"] = status
        r["finder_tags_json"] = json.dumps(tags, ensure_ascii=False) if tags else "[]"
        for tag in tags:
            finder_tag_rows.append({
                "tag_id": "tag_" + sha256_text(f"finder_tag_v1|{r.get('source_file_id')}|{tag.get('tag_raw','')}", 24),
                "source_file_id": r.get("source_file_id", ""),
                "source_content_id": r.get("source_content_id", ""),
                "source_path": r.get("source_path", ""),
                "tag_raw": tag.get("tag_raw", ""),
                "tag_name": tag.get("tag_name", ""),
                "tag_color": tag.get("tag_color", ""),
            })
    return manifest_rows, finder_tag_rows


def build_sidecar_links(manifest_rows: List[dict]) -> List[dict]:
    """Link .xmp/.aae/etc. files to same-folder same-stem media files."""
    primary_by_key: Dict[str, List[dict]] = defaultdict(list)
    sidecars: List[dict] = []
    for r in manifest_rows:
        key = path_stem_key(r.get("source_path", ""))
        if r.get("media_kind") in {"video", "image", "audio", "text"} and r.get("dedup_role") == "canonical":
            primary_by_key[key].append(r)
        if r.get("media_kind") == "metadata_sidecar":
            sidecars.append(r)

    links: List[dict] = []
    for s in sidecars:
        key = path_stem_key(s.get("source_path", ""))
        parents = primary_by_key.get(key, [])
        for parent in parents:
            sidecar_type = s.get("extension", "").lstrip(".").lower() or "unknown"
            link_id = "scx_" + sha256_text(
                f"source_sidecar_link_v1|{parent.get('source_file_id')}|{s.get('source_file_id')}", 24
            )
            links.append({
                "sidecar_link_id": link_id,
                "parent_source_file_id": parent.get("source_file_id", ""),
                "parent_source_content_id": parent.get("source_content_id", ""),
                "parent_source_path": parent.get("source_path", ""),
                "sidecar_source_file_id": s.get("source_file_id", ""),
                "sidecar_source_content_id": s.get("source_content_id", ""),
                "sidecar_path": s.get("source_path", ""),
                "sidecar_extension": s.get("extension", ""),
                "sidecar_type": sidecar_type,
                "match_rule": "same_folder_same_stem",
            })
    return links


def ensure_step01_db_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
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
        );

        CREATE TABLE IF NOT EXISTS source_file_records (
            source_file_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            source_root TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            media_kind TEXT NOT NULL,
            support_status TEXT NOT NULL,
            support_reason TEXT,
            size_bytes INTEGER,
            mtime_ns INTEGER,
            ctime_ns INTEGER,
            content_sha256 TEXT,
            dedup_role TEXT,
            next_action TEXT,
            canonical_source_file_id TEXT,
            folder_path TEXT,
            file_stem TEXT,
            stem_key TEXT,
            finder_tag_status TEXT,
            finder_tags_json TEXT,
            scan_run_id TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS source_sidecars (
            sidecar_link_id TEXT PRIMARY KEY,
            parent_source_file_id TEXT NOT NULL,
            parent_source_content_id TEXT NOT NULL,
            parent_source_path TEXT NOT NULL,
            sidecar_source_file_id TEXT NOT NULL,
            sidecar_source_content_id TEXT NOT NULL,
            sidecar_path TEXT NOT NULL,
            sidecar_extension TEXT,
            sidecar_type TEXT,
            match_rule TEXT NOT NULL,
            scan_run_id TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS source_finder_tags (
            tag_id TEXT PRIMARY KEY,
            source_file_id TEXT NOT NULL,
            source_content_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            tag_raw TEXT NOT NULL,
            tag_name TEXT,
            tag_color TEXT,
            scan_run_id TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS source_scan_folder_groups (
            folder_group_id TEXT PRIMARY KEY,
            folder_path TEXT NOT NULL,
            source_root TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            media_counts_json TEXT NOT NULL,
            scan_run_id TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS model_runs (
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_path TEXT NOT NULL,
            script_path TEXT NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0,
            output_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME,
            error_message TEXT
        );
        """
    )




def sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> Dict[str, dict]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    cols = {}
    for r in rows:
        # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        cols[str(r[1])] = {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": int(r[3] or 0),
            "default": r[4],
            "pk": int(r[5] or 0),
        }
    return cols


def model_run_payload(
    run_id: str,
    status: str,
    started_at: str,
    script_path: str,
    input_count: int,
    output_count: int,
    error_message: str = "",
    finished_at: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "run_id": run_id,
        "stage": "stop01_source_scan",
        "model_name": "none",
        "model_path": "none",
        "model_local_path": "none",
        "model_version": "none",
        "script_path": script_path,
        "script_sha256": sha256_file(Path(script_path)) if Path(script_path).exists() else "unknown",
        "script_version": SCRIPT_VERSION,
        "input_count": int(input_count),
        "output_count": int(output_count),
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "error_message": error_message or "",
    }


def upsert_model_run_compatible(conn: sqlite3.Connection, payload: Dict[str, object]) -> None:
    """Insert/update model_runs without assuming one fixed schema.

    Earlier scripts created model_runs with extra NOT NULL columns such as script_version.
    This function fills every known column when present, and avoids breaking on schema drift.
    """
    cols = sqlite_table_columns(conn, "model_runs")
    if not cols:
        raise RuntimeError("model_runs table is missing after schema initialization")

    insert_data: Dict[str, object] = {}
    for col, info in cols.items():
        if col in payload:
            insert_data[col] = payload[col]
        elif info.get("notnull") and info.get("default") is None and not info.get("pk"):
            # Best-effort fallback for future NOT NULL columns without defaults.
            insert_data[col] = "" if "TEXT" in str(info.get("type", "")).upper() else 0

    # run_id is primary key and must always be present.
    insert_data["run_id"] = payload["run_id"]

    col_names = list(insert_data.keys())
    placeholders = ", ".join(["?"] * len(col_names))
    col_sql = ", ".join(col_names)
    update_cols = [c for c in col_names if c != "run_id"]
    update_sql = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
    sql = f"INSERT INTO model_runs ({col_sql}) VALUES ({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {update_sql}"
    conn.execute(sql, [insert_data[c] for c in col_names])

def write_step01_database(
    db_path: Path,
    manifest_rows: List[dict],
    canonical_rows: List[dict],
    sidecar_rows: List[dict],
    finder_tag_rows: List[dict],
    source_roots: List[Path],
    run_id: str,
    script_path: str,
) -> dict:
    db_path = resolve_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        ensure_step01_db_schema(conn)
        # A scan is the authoritative snapshot of the selected library at this
        # moment.  Preserve source_assets identities for downstream lineage,
        # but mark every previous item absent until the current snapshot sees
        # it again.  This makes folder moves/merges observable without deleting
        # recognition results or touching original media.
        previously_active = int(conn.execute(
            "SELECT COUNT(*) FROM source_assets "
            "WHERE COALESCE(online_status,1)=1 AND COALESCE(is_deleted_or_missing,0)=0"
        ).fetchone()[0])
        conn.execute(
            "UPDATE source_assets SET online_status=0, is_deleted_or_missing=1"
        )
        # The current scan replaces the derived snapshot tables below, but
        # source_file_records is durable lineage.  Downstream identity rows
        # reference its source_file_id values, so deleting historical rows
        # would break those foreign keys when a file is moved or disappears.
        # Current rows are upserted below with the new scan_run_id; historical
        # rows remain available for traceability.
        for snapshot_table in (
            "source_sidecars", "source_finder_tags", "source_scan_folder_groups",
        ):
            conn.execute(f"DELETE FROM {snapshot_table}")
        upsert_model_run_compatible(
            conn,
            model_run_payload(
                run_id=run_id,
                status="running",
                started_at=started,
                script_path=script_path,
                input_count=len(manifest_rows),
                output_count=0,
                error_message="",
            ),
        )

        for r in manifest_rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_file_records
                (source_file_id, source_content_id, absolute_path, relative_path, source_root, file_name,
                 extension, media_kind, support_status, support_reason, size_bytes, mtime_ns, ctime_ns,
                 content_sha256, dedup_role, next_action, canonical_source_file_id, folder_path, file_stem,
                 stem_key, finder_tag_status, finder_tags_json, scan_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    r.get("source_file_id", ""),
                    r.get("source_content_id", ""),
                    r.get("source_path", ""),
                    r.get("source_relative_path", ""),
                    r.get("source_root", ""),
                    r.get("file_name", ""),
                    r.get("extension", ""),
                    r.get("media_kind", ""),
                    r.get("support_status", ""),
                    r.get("support_reason", ""),
                    r.get("file_size_bytes"),
                    r.get("mtime_ns"),
                    r.get("ctime_ns"),
                    r.get("content_sha256", ""),
                    r.get("dedup_role", ""),
                    r.get("next_action", ""),
                    r.get("canonical_source_file_id", ""),
                    r.get("folder_path", ""),
                    r.get("file_stem", ""),
                    r.get("stem_key", ""),
                    r.get("finder_tag_status", ""),
                    r.get("finder_tags_json", "[]"),
                    run_id,
                ),
            )

        source_assets_upserted = 0
        for r in canonical_rows:
            if r.get("next_action") != "process":
                continue
            if r.get("media_kind") not in {"video", "image", "audio", "text"}:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO source_assets
                (source_content_id, absolute_path, relative_path, file_name, extension, media_type,
                 size_bytes, mtime, ctime, volume_id, online_status, last_seen_at, is_deleted_or_missing)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'LOCAL', 1, CURRENT_TIMESTAMP, 0)
                """,
                (
                    r.get("source_content_id", ""),
                    r.get("source_path", ""),
                    r.get("source_relative_path", ""),
                    r.get("file_name", ""),
                    r.get("extension", ""),
                    r.get("media_kind", ""),
                    int(r.get("file_size_bytes") or 0),
                    int((r.get("mtime_ns") or 0) // 1_000_000_000),
                    int((r.get("ctime_ns") or 0) // 1_000_000_000),
                ),
            )
            source_assets_upserted += 1

        for r in sidecar_rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_sidecars
                (sidecar_link_id, parent_source_file_id, parent_source_content_id, parent_source_path,
                 sidecar_source_file_id, sidecar_source_content_id, sidecar_path, sidecar_extension,
                 sidecar_type, match_rule, scan_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    r.get("sidecar_link_id", ""),
                    r.get("parent_source_file_id", ""),
                    r.get("parent_source_content_id", ""),
                    r.get("parent_source_path", ""),
                    r.get("sidecar_source_file_id", ""),
                    r.get("sidecar_source_content_id", ""),
                    r.get("sidecar_path", ""),
                    r.get("sidecar_extension", ""),
                    r.get("sidecar_type", ""),
                    r.get("match_rule", ""),
                    run_id,
                ),
            )

        for r in finder_tag_rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_finder_tags
                (tag_id, source_file_id, source_content_id, source_path, tag_raw, tag_name, tag_color, scan_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    r.get("tag_id", ""),
                    r.get("source_file_id", ""),
                    r.get("source_content_id", ""),
                    r.get("source_path", ""),
                    r.get("tag_raw", ""),
                    r.get("tag_name", ""),
                    r.get("tag_color", ""),
                    run_id,
                ),
            )

        folder_groups: Dict[str, dict] = {}
        for r in manifest_rows:
            folder = r.get("folder_path") or path_folder(r.get("source_path", ""))
            root = r.get("source_root", "")
            k = f"{root}|{folder}"
            if k not in folder_groups:
                folder_groups[k] = {"folder_path": folder, "source_root": root, "file_count": 0, "media_counts": Counter()}
            folder_groups[k]["file_count"] += 1
            folder_groups[k]["media_counts"][r.get("media_kind", "")] += 1
        for _, g in folder_groups.items():
            gid = "fg_" + sha256_text(f"source_scan_folder_group_v1|{g['source_root']}|{g['folder_path']}", 24)
            conn.execute(
                """
                INSERT OR REPLACE INTO source_scan_folder_groups
                (folder_group_id, folder_path, source_root, file_count, media_counts_json, scan_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    gid,
                    g["folder_path"],
                    g["source_root"],
                    int(g["file_count"]),
                    json.dumps(dict(g["media_counts"]), ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )

        upsert_model_run_compatible(
            conn,
            model_run_payload(
                run_id=run_id,
                status="done",
                started_at=started,
                script_path=script_path,
                input_count=len(manifest_rows),
                output_count=source_assets_upserted,
                error_message="",
                finished_at=now_iso(),
            ),
        )
        conn.commit()
        return {
            "db_path": str(db_path),
            "source_file_records_upserted": len(manifest_rows),
            "source_assets_upserted": source_assets_upserted,
            "source_sidecars_upserted": len(sidecar_rows),
            "source_finder_tags_upserted": len(finder_tag_rows),
            "source_scan_folder_groups_upserted": len(folder_groups),
            "previously_active_source_assets_marked_for_reconciliation": previously_active,
            "current_active_source_assets": source_assets_upserted,
            "source_snapshot_reconciliation": (
                "authoritative_current_scan_preserve_content_and_file_lineage"
            ),
            "model_run_id": run_id,
        }
    except Exception as exc:
        conn.rollback()
        try:
            upsert_model_run_compatible(
                conn,
                model_run_payload(
                    run_id=run_id,
                    status="failed",
                    started_at=started,
                    script_path=script_path,
                    input_count=len(manifest_rows),
                    output_count=0,
                    error_message=repr(exc)[:500],
                    finished_at=now_iso(),
                ),
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def parse_args():
    env_src = os.environ.get("SRC", "").strip() or os.environ.get("SOURCE_ROOT", "").strip()
    env_out = os.environ.get("OUT", "").strip() or os.environ.get("OUTPUT_DIR", "").strip()

    p = argparse.ArgumentParser(description="Step01 local source scan, lineage, dedup, telemetry and process queue builder.")
    p.add_argument("sources", nargs="*", help="One or more source roots. Defaults to project test source when omitted.")
    p.add_argument("--out", default=env_out or str(DEFAULT_OUT), help="Base workspace output directory.")
    p.add_argument("--stage-label", default="step01_source_scan_lineage_dedup", help="Stage label for workspace naming.")
    p.add_argument("--run-id", default=now_run_id(), help="Run id.")
    p.add_argument("--hash-all", action="store_true", help="Compute full sha256 for all supported files.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="Project SQLite database path. Must stay inside project or test-output.")
    p.add_argument("--no-db-write", action="store_true", help="Do not write Step01 scan results into SQLite database.")
    p.add_argument("--scan-mac-tags", action="store_true", help="Read macOS Finder tags with mdls. Read-only; may slow large scans.")
    p.add_argument("--telemetry-interval", type=float, default=2.0, help="Telemetry sample interval seconds.")
    p.add_argument("--no-open", action="store_true", help="Do not open output directory after run.")
    p.add_argument("--preflight-only", action="store_true", help="Only print fixed runtime/path/dependency/safety preflight and exit without scanning.")
    args = p.parse_args()

    if not args.sources and env_src:
        args.sources = [env_src]
    if not args.sources:
        args.sources = [str(DEFAULT_SOURCE_ROOT)]
    if not args.out:
        args.out = str(DEFAULT_OUT)
    return args


def main():
    args = parse_args()
    source_roots = [resolve_path(Path(x)) for x in args.sources]
    base_out = resolve_path(Path(args.out))
    db_path = resolve_path(Path(args.db))
    preflight = runtime_preflight(source_roots=source_roots, db_path=db_path, out_path=base_out, scan_macos_tags=args.scan_mac_tags)
    if args.preflight_only:
        print(json.dumps({"validation_status": "PASS" if not preflight["blockers"] else "BLOCKED_PREFLIGHT", "runtime_preflight": preflight}, ensure_ascii=False, indent=2))
        return 0 if not preflight["blockers"] else 2
    if preflight["blockers"]:
        print(json.dumps({"validation_status": "BLOCKED_PREFLIGHT", "runtime_preflight": preflight}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    validate_output_not_inside_sources(base_out, source_roots)
    if not args.no_db_write:
        validate_db_path(db_path, source_roots)

    workspace = make_workspace_dir(base_out, args.stage_label, args.run_id, source_roots)
    dirs = prepare_workspace(workspace)
    write_json(dirs["reports"] / "step01_runtime_preflight.json", preflight)

    telemetry = TelemetryMonitor(dirs["telemetry"], args.telemetry_interval)
    stage_timings: List[dict] = []

    print("== Step01 Source Scan + Lineage + Dedup + Telemetry start ==")
    print(f"script_version: {SCRIPT_VERSION}")
    print(f"stage_label: {args.stage_label}")
    print(f"run_id: {args.run_id}")
    print(f"workspace: {workspace}")
    print("source_roots:")
    for s in source_roots:
        print(f"  - {s}")
    print(f"hash_policy: {'hash_all' if args.hash_all else 'same_size_candidates_only'}")
    print(f"db_write: {not args.no_db_write}")
    print(f"db_path: {db_path}")
    print(f"scan_macos_tags: {args.scan_mac_tags}")
    print(f"telemetry_interval: {args.telemetry_interval}")
    print("runtime_preflight=" + json.dumps(preflight, ensure_ascii=False, sort_keys=True))

    telemetry.start()
    try:
        root_rows, source_rows = timed_stage(
            stage_timings,
            telemetry,
            "scan_roots",
            lambda: scan_roots(source_roots)
        )
        print(f"scan done: source_roots={len(root_rows)} source_file_records={len(source_rows)}")

        (
            manifest_rows,
            duplicate_groups,
            skipped_duplicates,
            canonical_rows,
            unsupported_rows,
            lineage_root_rows,
            hash_elapsed,
        ) = timed_stage(
            stage_timings,
            telemetry,
            "dedup_and_hash",
            lambda: compute_dedup(source_rows, args.hash_all, telemetry)
        )

        (manifest_rows, finder_tag_rows) = timed_stage(
            stage_timings,
            telemetry,
            "enrich_sidecars_and_macos_tags",
            lambda: enrich_with_local_metadata(manifest_rows, args.scan_mac_tags)
        )
        sidecar_link_rows = timed_stage(
            stage_timings,
            telemetry,
            "link_metadata_sidecars",
            lambda: build_sidecar_links(manifest_rows)
        )

        def write_outputs():
            write_jsonl(dirs["manifests"] / "source_roots_manifest.jsonl", root_rows)
            write_csv(dirs["manifests"] / "source_roots_manifest.csv", root_rows)

            write_jsonl(dirs["manifests"] / "source_files_manifest.jsonl", manifest_rows)
            write_csv(dirs["manifests"] / "source_files_manifest.csv", manifest_rows)

            write_jsonl(dirs["manifests"] / "source_canonical_manifest.jsonl", canonical_rows)
            write_csv(dirs["manifests"] / "source_canonical_manifest.csv", canonical_rows)

            write_jsonl(dirs["manifests"] / "source_duplicate_groups.jsonl", duplicate_groups)
            write_csv(dirs["manifests"] / "source_duplicate_groups.csv", duplicate_groups)

            write_jsonl(dirs["manifests"] / "source_skipped_duplicates.jsonl", skipped_duplicates)
            write_csv(dirs["manifests"] / "source_skipped_duplicates.csv", skipped_duplicates)

            write_jsonl(dirs["manifests"] / "source_unsupported_files.jsonl", unsupported_rows)
            write_csv(dirs["manifests"] / "source_unsupported_files.csv", unsupported_rows)

            write_jsonl(dirs["manifests"] / "source_metadata_sidecars.jsonl", sidecar_link_rows)
            write_csv(dirs["manifests"] / "source_metadata_sidecars.csv", sidecar_link_rows)
            write_jsonl(dirs["manifests"] / "source_finder_tags.jsonl", finder_tag_rows)
            write_csv(dirs["manifests"] / "source_finder_tags.csv", finder_tag_rows)

            queue_counts = write_kind_queues(dirs, canonical_rows)
            write_lineage_contract(dirs)
            write_jsonl(dirs["lineage"] / "source_lineage_roots.jsonl", lineage_root_rows)
            write_csv(dirs["lineage"] / "source_lineage_roots.csv", lineage_root_rows)
            file_index_path, file_index_count = write_file_index(dirs, args.stage_label)
            return queue_counts, file_index_path, file_index_count

        queue_counts, file_index_path, file_index_count = timed_stage(
            stage_timings,
            telemetry,
            "write_outputs",
            write_outputs
        )

        db_write_summary = {"db_write": False, "reason": "no_db_write"}
        if not args.no_db_write:
            db_write_summary = timed_stage(
                stage_timings,
                telemetry,
                "write_database",
                lambda: write_step01_database(
                    db_path=db_path,
                    manifest_rows=manifest_rows,
                    canonical_rows=canonical_rows,
                    sidecar_rows=sidecar_link_rows,
                    finder_tag_rows=finder_tag_rows,
                    source_roots=source_roots,
                    run_id=args.run_id,
                    script_path=str(Path(__file__).resolve()),
                )
            )
            write_json(dirs["reports"] / "step01_database_write_summary.json", db_write_summary)
            print("DB_WRITE_SUMMARY=" + json.dumps(db_write_summary, ensure_ascii=False, sort_keys=True))

    finally:
        telemetry.set_stage("finalizing")
        time.sleep(min(args.telemetry_interval, 2.0))
        telemetry.stop()

    summary = write_reports(
        dirs=dirs,
        args=args,
        source_roots=source_roots,
        root_rows=root_rows,
        manifest_rows=manifest_rows,
        duplicate_groups=duplicate_groups,
        skipped_duplicates=skipped_duplicates,
        canonical_rows=canonical_rows,
        lineage_root_rows=lineage_root_rows,
        queue_counts=queue_counts,
        file_index_path=file_index_path,
        file_index_count=file_index_count,
        stage_timings=stage_timings,
        telemetry=telemetry,
    )

    summary["runtime_preflight"] = preflight
    write_json(dirs["reports"] / "source_scan_summary_with_preflight.json", summary)

    # Refresh file index after reports are written.
    write_file_index(dirs, args.stage_label)

    print("== Step01 Source Scan + Lineage + Dedup + Telemetry finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"STEP01_WORKSPACE={workspace}")
    print(f"VIDEO_QUEUE={dirs['queues'] / 'process_queue_video.jsonl'}")
    print(f"FINAL_REPORT={dirs['final_report'] / 'final_run_report.md'}")

    if not args.no_open:
        try:
            subprocess.run(["open", str(workspace)], check=False)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main() or 0)
