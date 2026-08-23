from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .pipeline_orchestrator import (
    atomic_json,
    build_stage_plan,
    execute_pipeline,
    load_json,
    stop_pipeline,
    validate_stage_acceptance,
)
from .local_person_annotations import (
    add_visual_membership, annotation_path, create_identity, detach_cluster,
    ensure_identity, grouped_catalog, load_annotations, merge_identity,
    name_identity, remove_visual_membership, resolve_clusters,
)
from .central_database import (
    asset_annotations,
    ensure_schema as ensure_central_schema,
    list_saved_searches,
    list_search_history,
    load_person_annotations as load_central_person_annotations,
    record_search_history,
    save_search as save_central_search,
    upsert_asset_annotation,
    save_person_annotations as save_central_person_annotations,
    task_id_for_database,
)
from .processing_profile import build_processing_profile, detect_hardware, save_processing_profile
from .person_track_suggestions import load_database_suggestions
from .yoloe_keywords import (
    load_registry as load_yoloe_registry,
    materialise_registry as materialise_yoloe_registry,
    normalise_entries as normalise_yoloe_entries,
    normalise_profile as normalise_yoloe_profile,
    profile_from_registry as yoloe_profile_from_registry,
)
from .repository import ReadonlyMediaRepository
from .runtime_contract import (
    default_model_root,
    load_runtime_contract,
    task_runtime_from_contract,
    validate_runtime_contract,
)
from .search_jobs import SearchJobManager, offline_search_environment
from .storage_audit import (
    apply_cleanup_plan,
    audit_task_storage,
    build_cleanup_plan,
    compare_task_storage,
)


APP_NAME = "本地数据库"
APP_VERSION = "1.2.3"
_RESOURCE_CACHE: tuple[float, int | None, dict[str, Any]] = (0.0, None, {})


def live_resource_snapshot(current_task: Mapping[str, Any] | None) -> dict[str, Any]:
    """Small, read-only process sample for the run page (never scans source)."""
    global _RESOURCE_CACHE
    pid = int((current_task or {}).get("current_child_pid") or 0) or None
    now = time.monotonic()
    cached_at, cached_pid, cached = _RESOURCE_CACHE
    if cached and pid == cached_pid and now - cached_at < 2.0:
        return dict(cached)
    report: dict[str, Any] = {
        "active_pid": pid, "process_alive": False, "cpu_percent": 0.0,
        "memory_bytes": 0, "process_count": 0, "swap_used_bytes": None,
        "sample_error": "",
        "source_scanned": False,
    }
    if pid:
        try:
            sample = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,%cpu=,rss=,state="],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            rows: dict[int, tuple[int, float, int, str]] = {}
            for line in sample.stdout.splitlines():
                fields = line.split(None, 4)
                if len(fields) != 5:
                    continue
                child_pid, parent_pid = int(fields[0]), int(fields[1])
                rows[child_pid] = (
                    parent_pid, float(fields[2]), int(fields[3]), fields[4],
                )
            process_ids = {pid}
            changed = True
            while changed:
                changed = False
                for child_pid, (parent_pid, _, _, _) in rows.items():
                    if parent_pid in process_ids and child_pid not in process_ids:
                        process_ids.add(child_pid)
                        changed = True
            live_rows = [rows[item] for item in process_ids if item in rows]
            if sample.returncode == 0 and live_rows:
                report.update({
                    "process_alive": True,
                    "cpu_percent": round(sum(row[1] for row in live_rows), 1),
                    "memory_bytes": sum(row[2] for row in live_rows) * 1024,
                    "process_count": len(live_rows),
                    "process_state": rows.get(pid, live_rows[0])[3],
                })
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report["sample_error"] = str(exc)
    try:
        swap = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True,
            text=True, timeout=2.0, check=False,
        ).stdout
        match = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap)
        if match:
            scale = 1024 ** (2 if match.group(2) == "M" else 3)
            report["swap_used_bytes"] = int(float(match.group(1)) * scale)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _RESOURCE_CACHE = (now, pid, dict(report))
    return report


def runtime_output_root(config: dict[str, Any]) -> Path:
    configured = str(config.get("output_root") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "素材大整理").resolve()


def application_state_root() -> Path:
    return (Path.home() / "Library" / "Application Support" / "素材大整理").resolve()


def search_runtime_cache_root(config: dict[str, Any] | None = None) -> Path:
    """Keep bounded query artifacts outside the user-selected library."""
    configured = str((config or {}).get("search_cache_root") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return application_state_root() / "cache" / "search_runtime"


def active_library_path() -> Path:
    return application_state_root() / "runtime" / "active_library.json"


def library_registry_path() -> Path:
    return application_state_root() / "runtime" / "library_registry_v1.json"


def model_root_config_path() -> Path:
    return application_state_root() / "settings" / "model_root.json"


def selected_model_root() -> Path:
    path = model_root_config_path()
    if path.is_file():
        try:
            value = str(load_json(path).get("model_root") or "").strip()
            if value:
                return Path(value).expanduser().absolute()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return default_model_root()


def save_model_root(path: Path) -> dict[str, Any]:
    selected = Path(path).expanduser().absolute()
    if not selected.is_dir():
        raise ValueError("请选择一个已经存在的模型总目录")
    destination = model_root_config_path()
    atomic_json(destination, {
        "contract": "media_archive_external_model_root_v1",
        "model_root": str(selected),
        "model_directory_access": "read_only",
    })
    return {
        "status": "PASS",
        "message": "模型位置已保存；模型仍由用户自行下载和管理",
        "path": str(selected),
        "model_directory_write": False,
    }


def load_active_library(config: dict[str, Any]) -> dict[str, Any]:
    path = active_library_path()
    if not path.is_file():
        return config
    try:
        active = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config
    merged = dict(config)
    merged.update({key: value for key, value in active.items() if value not in (None, "")})
    return merged


def _library_task_path_from_config(config: dict[str, Any]) -> str:
    """Return the owning library task, not a short-lived maintenance task."""
    return str(config.get("library_task_path") or config.get("task_path") or "").strip()


def _controlled_library_task_paths(candidates: Sequence[Path]) -> list[Path]:
    """Discover sibling tasks only below already-known index roots.

    This deliberately never walks an arbitrary user folder.  A task created by
    the app lives in ``<index-root>/tasks/<task>/task.json``; once one task in
    that index root is known, its siblings are safe, small metadata reads.
    """
    discovered: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        tasks_root = resolved.parent.parent if resolved.parent.parent.name == "tasks" else None
        if tasks_root is None or not tasks_root.is_dir():
            continue
        for child in sorted(tasks_root.iterdir()):
            task_path = child / "task.json"
            if child.is_dir() and task_path.is_file():
                discovered.append(task_path)
    return discovered


def _existing_library_record(task_path: Path) -> dict[str, Any] | None:
    try:
        task = load_json(task_path.expanduser().resolve(strict=True))
        database = Path(str(task["database"])).expanduser().resolve(strict=True)
        state_path = Path(str(task.get("state_path") or "")).expanduser()
        state = load_json(state_path) if state_path.is_file() else {}
        with ReadonlyMediaRepository(database).connect() as con:
            source_counts = {
                str(media_type): int(count)
                for media_type, count in con.execute(
                    "SELECT media_type,COUNT(*) FROM source_assets "
                    "WHERE media_type IN ('image','video') GROUP BY media_type"
                )
            }
        return {
            "task_id": str(task.get("task_id") or task_path.parent.name),
            "task_name": str(task.get("name") or "未命名素材库"),
            "task_path": str(task_path.resolve()),
            "database": str(database),
            "source_root": str(task.get("source_root") or ""),
            "created_at": str(task.get("created_at") or ""),
            "status": str(state.get("status") or task.get("status") or "unknown"),
            "image_count": source_counts.get("image", 0),
            "video_count": source_counts.get("video", 0),
            "elapsed_seconds": _state_elapsed_seconds(state),
            "elapsed_human": human_duration(_state_elapsed_seconds(state)),
        }
    except (OSError, KeyError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return None


def existing_libraries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return registered libraries plus sibling tasks below known index roots."""
    candidates: list[Path] = []
    current = _library_task_path_from_config(config)
    if current:
        candidates.append(Path(current))
    registry = library_registry_path()
    if registry.is_file():
        try:
            payload = load_json(registry)
            candidates.extend(
                Path(str(row.get("task_path") or ""))
                for row in payload.get("libraries", [])
                if str(row.get("task_path") or "").strip()
            )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    candidates.extend(_controlled_library_task_paths(candidates))
    active_task_path = _library_task_path_from_config(config)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        row = _existing_library_record(candidate)
        if row is None or row["database"] in seen:
            continue
        seen.add(row["database"])
        row["is_active"] = str(row["task_path"]) == active_task_path
        rows.append(row)
    rows.sort(key=lambda row: (row["created_at"], row["task_name"]), reverse=True)
    return rows


def task_detail(task_path: Path) -> dict[str, Any]:
    """Read one historical task without changing the active library."""
    resolved_task = task_path.expanduser().resolve(strict=True)
    task = load_json(resolved_task)
    database = Path(str(task["database"])).expanduser().resolve(strict=True)
    state_text = str(task.get("state_path") or "").strip()
    state_path = Path(state_text).expanduser() if state_text else resolved_task.parent / "pipeline_state.json"
    if not state_path.is_file():
        raise RuntimeError("该历史任务缺少 pipeline_state.json")
    state = load_json(state_path)
    # Older app builds did not persist the explicit failed-stage fields.
    # Preserve their history, but infer enough information for the current UI
    # to expose a useful, copyable failure report.
    if str(state.get("status") or task.get("status") or "") == "failed":
        state = dict(state)
        failed_row = next(
            (
                row for row in state.get("stages", [])
                if str(row.get("status") or "") == "failed"
            ),
            None,
        )
        state["failed_stage_key"] = str(
            state.get("failed_stage_key")
            or (failed_row or {}).get("key")
            or state.get("current_stage_key")
            or ""
        )
        state["failed_stage_name"] = str(
            state.get("failed_stage_name")
            or (failed_row or {}).get("name")
            or state.get("current_stage_name")
            or "未知阶段"
        )
        state["error_summary"] = str(
            state.get("error_summary")
            or task.get("error_summary")
            or state.get("error")
            or task.get("error")
            or "历史任务失败，但旧版本没有保存错误摘要"
        )
        state["error_details"] = str(
            state.get("error_details") or task.get("error_details") or ""
        )
        default_log_path = resolved_task.parent / "logs" / "pipeline.log"
        state["error_log_path"] = str(
            state.get("error_log_path")
            or task.get("error_log_path")
            or task.get("log_path")
            or (default_log_path if default_log_path.is_file() else "")
        )
    repository = ReadonlyMediaRepository(database)
    detail_config = {
        "task_path": str(resolved_task),
        "database": str(database),
    }
    acceptance_errors = task_output_acceptance(detail_config, state)
    metrics = repository.stage_metrics()
    # Directory size is intentionally calculated only for an explicit history
    # detail request.  existing_libraries() is refreshed with the main UI and
    # must never walk a large index directory on every refresh.
    storage = audit_task_storage(resolved_task, largest_limit=1)
    return {
        "status": "PASS",
        "task_id": str(task.get("task_id") or resolved_task.parent.name),
        "task_name": str(task.get("name") or "未命名素材库"),
        "task_path": str(resolved_task),
        "source_root": str(task.get("source_root") or ""),
        "created_at": str(task.get("created_at") or ""),
        "task_status": str(state.get("status") or task.get("status") or "unknown"),
        "started_at": epoch_timecode(state.get("started_at_epoch")),
        "finished_at": epoch_timecode(state.get("finished_at_epoch")),
        "elapsed_seconds": _state_elapsed_seconds(state),
        "elapsed_human": human_duration(_state_elapsed_seconds(state)),
        "index_storage": {
            "total_bytes": int(storage["total_bytes"]),
            "total_file_count": int(storage["total_file_count"]),
            "status": str(storage["status"]),
            "source_root_scanned": bool(storage["source_root_scanned"]),
        },
        "pipeline": task_pipeline(state, acceptance_errors, metrics),
        "error": str(state.get("error") or task.get("error") or ""),
        "database_write": False,
        "original_media_read": False,
    }


def register_library(task_path: Path) -> None:
    """Remember a library by task path; user-visible names remain in task.json."""
    path = library_registry_path()
    payload: dict[str, Any] = {"contract": "media_archive_library_registry_v1", "libraries": []}
    if path.is_file():
        try:
            payload = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    rows = list(payload.get("libraries") or [])
    resolved = str(task_path.expanduser().resolve())
    rows = [row for row in rows if str(row.get("task_path") or "") != resolved]
    rows.append({"task_path": resolved, "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    atomic_json(path, {"contract": "media_archive_library_registry_v1", "libraries": rows})


def activate_library(config: dict[str, Any], task_path: Path) -> dict[str, Any]:
    """Switch all read-only library views to one completed, registered task.

    The pointer is intentionally tiny and stores absolute paths taken from the
    selected task contract.  It neither rewrites the task nor touches its
    SQLite database, source material, models, or stage outputs.
    """
    selected = task_path.expanduser().resolve(strict=True)
    known = {str(row["task_path"]) for row in existing_libraries(config)}
    if str(selected) not in known:
        raise RuntimeError("请选择当前索引位置中由本软件建立的素材库")
    record = _existing_library_record(selected)
    if record is None:
        raise RuntimeError("所选素材库的任务清单或数据库不可读取")
    task = load_json(selected)
    task["task_path"] = str(selected)
    task.setdefault("software_version", APP_VERSION)
    central_schema = ensure_central_schema(
        Path(str(task["database"])), task=task,
        backup_dir=selected.parent / "backups" / "central_schema",
    )
    state_text = str(task.get("state_path") or "").strip()
    state_path = Path(state_text).expanduser() if state_text else selected.parent / "pipeline_state.json"
    active = {
        "active_library_contract": "media_archive_active_library_v2",
        "library_task_path": str(selected),
        "task_path": str(selected),
        "database": str(Path(str(task["database"])).expanduser().resolve(strict=True)),
        "output_root": str(Path(str(task.get("workspace") or selected.parent / "workspace")).expanduser().absolute()),
        "task_id": str(task.get("task_id") or selected.parent.name),
        "task_state_path": str(state_path),
        "task_name": str(task.get("name") or "未命名素材库"),
        "task_directory": str(selected.parent),
        "activated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    atomic_json(active_library_path(), active)
    register_library(selected)
    return {
        "status": "PASS",
        "message": f"已切换到素材库“{active['task_name']}”；搜索和浏览将只读取该任务数据库",
        "task_path": str(selected),
        "database": active["database"],
        "database_write": True,
        "central_schema": central_schema,
        "original_media_read": False,
        "model_run": False,
    }


def load_runtime(
    config_path: Path,
) -> tuple[dict[str, Any], Optional[ReadonlyMediaRepository], Optional[SearchJobManager]]:
    config_path = Path(config_path).expanduser().absolute()
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    contents = config_path.parent.parent
    for key, value in list(raw_config.items()):
        if isinstance(value, str):
            raw_config[key] = value.replace(
                "$APP_CONTENTS", str(contents),
            ).replace("$APP_RESOURCES", str(contents / "Resources"))
    config = load_active_library(raw_config)
    database_text = str(config.get("database") or "").strip()
    if not database_text or not Path(database_text).expanduser().is_file():
        return config, None, None
    repository = ReadonlyMediaRepository(Path(database_text))
    contract_text = str(config.get("runtime_contract_path") or "").strip()
    model_root = selected_model_root()
    contract_report = (
        validate_runtime_contract(Path(contract_text), model_root=model_root)
        if contract_text else {"ready": False}
    )
    if contract_text and not contract_report["ready"]:
        return config, repository, None
    contract = (
        load_runtime_contract(Path(contract_text), model_root=model_root)
        if contract_report["ready"] else None
    )
    manager = SearchJobManager(
        db_path=Path(database_text),
        output_root=runtime_output_root(config),
        search_script=Path(contract["scripts"]["search_adapter"] if contract else config["search_script"]),
        search_config=Path(contract["configs"]["hybrid_search"] if contract else config["search_config"]),
        embedding_python=Path(contract["python"]["embedding"] if contract else config["embedding_python"]),
        openclip_python=Path(contract["python"]["visual"] if contract else config["openclip_python"]),
    )
    return config, repository, manager


def task_state(config: dict[str, Any]) -> dict[str, Any] | None:
    value = str(config.get("task_state_path") or "").strip()
    path = Path(value).expanduser() if value else None
    if not path or not path.is_file():
        return None
    try:
        state = load_json(path)
        if state.get("status") in {"queued", "running"}:
            pid = int(state.get("worker_pid") or 0)
            if pid > 1 and not process_is_alive(pid):
                state = dict(state)
                state["status"] = "failed"
                state["error"] = "后台任务进程已经退出；可从最后成功阶段继续"
                state["reason_code"] = "STALE_RUNNING_PROCESS_EXITED"
                state["current_child_pid"] = None
                for row in state.get("stages", []):
                    if row.get("status") == "running":
                        row["status"] = "failed"
                        row["reason_code"] = "STALE_RUNNING_PROCESS_EXITED"
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def process_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def epoch_timecode(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return str(value)


def human_duration(value: Any) -> str:
    precise = max(0.0, float(value or 0))
    if 0 < precise < 1:
        return "少于1秒"
    total = max(0, int(round(precise)))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def _state_elapsed_seconds(state: dict[str, Any]) -> float:
    """Return a truthful task duration from timestamps or recorded stages."""
    try:
        started = float(state.get("started_at_epoch"))
        finished_value = state.get("finished_at_epoch")
        if finished_value not in (None, ""):
            return round(max(0.0, float(finished_value) - started), 1)
        if str(state.get("status") or "") in {"queued", "running"}:
            return round(max(0.0, time.time() - started), 1)
    except (TypeError, ValueError):
        pass
    return round(sum(
        max(0.0, float(row.get("elapsed_seconds") or 0))
        for row in state.get("stages", [])
    ), 1)


def task_active_run(state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("status") not in {"queued", "running"}:
        return None
    total = int(state.get("stage_count") or 0)
    completed = int(state.get("completed_stage_count") or 0)
    elapsed = sum(float(row.get("elapsed_seconds") or 0) for row in state.get("stages", []))
    current = next((row for row in state.get("stages", []) if row.get("status") == "running"), None)
    if current and current.get("started_at_epoch"):
        elapsed += max(0.0, time.time() - float(current["started_at_epoch"]))
    remaining = max(0, total - completed)
    return {
        "run_id": state.get("task_id"),
        "stage": state.get("current_stage_name") or "准备启动",
        "total": total,
        "completed": completed,
        "pending": remaining,
        "elapsed_seconds": round(elapsed, 1),
        "remaining": remaining,
        "percent": round(completed / total * 100.0, 2) if total else 0.0,
        # Stage durations differ by orders of magnitude.  A stage-count average
        # produces a rising, misleading ETA, so the model-specific DB run below
        # is the only source of a numerical estimate.
        "eta_seconds": None,
        "eta_basis": "等待当前阶段的逐项进度",
    }


def task_pipeline(
    state: dict[str, Any], acceptance_errors: Optional[dict[str, str]] = None,
    metrics: Optional[dict[str, dict[str, Any]]] = None,
    overall_eta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    acceptance_errors = acceptance_errors or {}
    metrics = metrics or {}
    names = {
        "scan": "素材扫描",
        "image_preview": "图片预览与延时摄影分组",
        "video_frames": "视频抽帧",
        "visual_schema_v3": "初始化视觉分析数据库结构",
        "yoloe": "物体识别（YOLOE）",
        "openclip": "全量视觉向量（OpenCLIP）",
        "dedup": "来源与画面去重",
        "person_reid_optional_v1": "可见人脸待确认人物组（InsightFace，本地匿名）",
        "candidate_schema": "初始化候选队列数据库结构",
        "candidates_generic_v2": "高价值画面与 OCR 候选筛选",
        "candidate_snapshot": "冻结当前候选快照",
        "qwen_optional_v2": "高价值画面描述（Qwen-VL）",
        "all_image_supplement_contract": "准备全部图片补充队列",
        "all_image_supplement_qwen": "补充其余图片描述（Qwen-VL）",
        "all_image_evidence_merge": "合并全部图片搜索证据",
        "ocr_optional_v2": "画面文字识别（OCR）",
        "evidence_optional_v2": "合并语义与文字证据",
        "propagation_optional_v2": "相邻画面语义传播",
        "embedding_optional_v2": "文本搜索向量（Qwen3-Embedding）",
        "repair_finder_tags": "补读图片 Finder 标签",
        "repair_candidate_dry_run": "按冻结 V25 规则重算缺失图片候选",
        "repair_supplement_contract": "建立缺失图片补充队列",
        "repair_supplement_qwen": "补充图片高价值画面描述（Qwen-VL）",
        "repair_evidence_merge": "合并新增图片描述与现有搜索证据",
        "repair_propagation": "重建相关视频帧语义传播",
        "repair_embedding": "更新文本搜索向量（Qwen3-Embedding）",
        "rebuild_scan": "重新扫描素材位置并对账当前清单",
        "rebuild_image_preview": "重新识别图片与延时摄影分组",
        "rebuild_restore_lineage": "恢复重扫前的历史文件引用",
        "rebuild_visual_schema": "用当前分组替换旧的特殊素材入口",
        "rebuild_openclip": "补齐新增画面的视觉向量（OpenCLIP）",
        "rebuild_search_index": "从已有数据库重建搜索入口",
        "audio_search_enrichment": "提取人声并建立音频文本搜索",
    }
    rows = []
    for stage in state.get("stages", []):
        key = str(stage.get("key") or "stage")
        status = "failed" if key in acceptance_errors else str(stage.get("status") or "pending")
        metric = metrics.get(key, {})
        live_total = int(stage.get("live_total") or 0)
        use_live = status == "running" and live_total > 0
        done = int(stage.get("live_completed") or 0) if use_live else int(metric.get("done") or 0)
        total_items = live_total if use_live else int(metric.get("total") or 0)
        detail = str(metric.get("description") or "").strip()
        duration = (
            f"用时 {human_duration(stage.get('elapsed_seconds'))}"
            if stage.get("elapsed_seconds") is not None else ""
        )
        report_paths = dict(stage.get("report_paths") or {})
        report_summary: dict[str, Any] = {}
        summary_path = Path(str(report_paths.get("summary") or "")).expanduser()
        if summary_path.is_file():
            try:
                report_summary = load_json(summary_path)
            except (OSError, ValueError, json.JSONDecodeError):
                report_summary = {}
        description = "；".join(part for part in (detail, duration) if part)
        rows.append({
            "key": key,
            "name": names.get(key, str(stage.get("name") or "处理阶段")),
            "status": status,
            "done": done,
            "total": total_items,
            "percent": min(100.0, done / total_items * 100.0) if total_items else (100.0 if status == "success" else 0.0),
            "description": (
                f"产物验收失败：{acceptance_errors[key]}"
                if key in acceptance_errors else
                description or "等待上一步完成后确定数量"
            ),
            "error_summary": str(stage.get("error_summary") or ""),
            "log_path": str(stage.get("log_path") or state.get("error_log_path") or ""),
            "current_item": str(stage.get("current_item") or ""),
            "success_count": int(stage.get("live_success") or 0),
            "skipped_count": int(stage.get("live_skipped") or 0),
            "failed_count": int(stage.get("live_failed") or 0),
            "eta_seconds": stage.get("eta_seconds"),
            "eta_basis": str(stage.get("eta_basis") or ""),
            "configured_workers": stage.get("configured_workers"),
            "actual_workers": stage.get("actual_workers"),
            "ffmpeg_processes": stage.get("ffmpeg_processes"),
            "model_workers": stage.get("model_workers"),
            "bytes_processed": int(stage.get("bytes_processed") or 0),
            "output_files": int(
                stage.get("output_files")
                or report_summary.get("output_file_count")
                or 0
            ),
            "stage_output_bytes": int(report_summary.get("output_bytes") or 0),
            "database_delta_bytes": int(report_summary.get("database_delta_bytes") or 0),
            "actual_script": str(report_summary.get("actual_script") or ""),
            "items_per_second": report_summary.get("items_per_second"),
            "report_paths": report_paths,
        })
    total = int(state.get("stage_count") or len(rows))
    completed = sum(row["status"] == "success" for row in rows)
    result = {
        "stages": rows,
        "overall_percent": round(completed / total * 100.0, 2) if total else 0.0,
        "failed_record_count": len(acceptance_errors) or (1 if state.get("status") == "failed" else 0),
        "search_ready": state.get("status") == "success" and not acceptance_errors,
        "full_pipeline_launcher_status": (
            "FAILED_OUTPUT_ACCEPTANCE" if acceptance_errors else str(state.get("status") or "ready").upper()
        ),
        "failed_stage_key": state.get("failed_stage_key"),
        "failed_stage_name": state.get("failed_stage_name"),
        "error_summary": str(state.get("error_summary") or state.get("error") or ""),
        "error_details": str(state.get("error_details") or ""),
        "error_log_path": str(state.get("error_log_path") or ""),
    }
    result.update(overall_eta or {
        "overall_eta_seconds": None,
        "overall_eta_basis": "等待产生可计数的模型任务",
    })
    return result


def estimate_task_remaining(
    state: dict[str, Any], metrics: dict[str, dict[str, Any]], task: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Expose only an ETA backed by this run's observed throughput.

    Different stages, files and devices vary by orders of magnitude.  Fixed
    per-model constants produced confident but visibly wrong countdowns.  The
    stage runner now waits for a minimum live sample before publishing an ETA.
    Unknown future stages are deliberately not guessed.
    """
    running = next(
        (row for row in state.get("stages", []) if row.get("status") == "running"),
        None,
    )
    if running and running.get("eta_seconds") is not None:
        return {
            "overall_eta_seconds": float(running["eta_seconds"]),
            "overall_eta_basis": (
                f"当前阶段估算：{running.get('eta_basis') or '按本次实际吞吐量'}；"
                "尚未开始的阶段不做虚假预测"
            ),
        }
    return {
        "overall_eta_seconds": None,
        "overall_eta_basis": (
            str((running or {}).get("eta_basis") or "正在估算；尚未开始的阶段不做虚假预测")
        ),
    }


def task_output_acceptance(config: dict[str, Any], state: dict[str, Any] | None) -> dict[str, str]:
    if not state or state.get("status") != "success":
        return {}
    task_path_text = str(config.get("task_path") or "").strip()
    if not task_path_text or not Path(task_path_text).is_file():
        return {}
    try:
        task = load_json(Path(task_path_text))
        # Maintenance tasks have their own smaller plan and acceptance rules.
        # Applying the full 15-stage contract here incorrectly turns a
        # successful repair/rebuild into FAILED_OUTPUT_ACCEPTANCE.
        if str(task.get("mode") or "full") in {"repair_images", "rebuild_search", "audio_enrichment"}:
            return {}
        return {
            key: reason for key in ("scan", "image_preview", "video_frames", "openclip")
            if (reason := validate_stage_acceptance(task, key))
        }
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        return {"scan": "TASK_OUTPUT_ACCEPTANCE_UNREADABLE"}


def reconcile_task_pipeline_with_library(
    pipeline: dict[str, Any],
    task_definition: dict[str, Any],
    readiness: dict[str, Any],
    repository_pipeline: dict[str, Any],
) -> dict[str, Any]:
    """Keep maintenance-stage progress separate from library search readiness."""
    result = dict(pipeline)
    result["search_ready"] = bool(readiness.get("ready"))
    mode = str(task_definition.get("mode") or "full")
    if mode in {"repair", "repair_images", "rebuild_search", "audio_enrichment"}:
        result["failed_record_count"] = int(
            repository_pipeline.get("failed_record_count") or 0
        )
    if (
        mode == "rebuild_search"
        and result.get("full_pipeline_launcher_status") == "SUCCESS"
        and not result["search_ready"]
    ):
        result["full_pipeline_launcher_status"] = "MAINTENANCE_SEARCH_INCOMPLETE"
    return result


def _timelapse_payload(repository: ReadonlyMediaRepository) -> dict[str, Any]:
    payload = repository.timelapse_groups(limit=20)
    for group in payload["items"]:
        for frame in group.get("frames", []):
            derived = repository.derived_path(str(frame.get("derived_id") or ""))
            if derived:
                frame["preview_path"] = str(derived)
            else:
                frame["preview_path"] = str(frame.get("preview_path") or "")
    return payload


def _yoloe_registry_path(config: dict[str, Any]) -> Path:
    contract_text = str(config.get("runtime_contract_path") or "").strip()
    if not contract_text:
        return Path(__file__).resolve().parents[2] / "configs/yoloe_keyword_registry_default_v1.json"
    contract_path = Path(contract_text).expanduser().absolute()
    contract = load_runtime_contract(contract_path, model_root=selected_model_root())
    configured = str((contract.get("configs") or {}).get("yoloe_registry") or "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return Path(__file__).resolve().parents[2] / "configs/yoloe_keyword_registry_default_v1.json"


def _effective_yoloe_profile(config: dict[str, Any], profile: Any = None) -> dict[str, Any]:
    registry = load_yoloe_registry(_yoloe_registry_path(config))
    value = profile.get("yoloe_keywords") if isinstance(profile, dict) else None
    return normalise_yoloe_profile(value, fallback_registry=registry)


def _materialise_task_yoloe_registry(
    config: dict[str, Any], runtime: dict[str, Any], profile: dict[str, Any], target_dir: Path,
) -> None:
    profile["yoloe_keywords"] = _effective_yoloe_profile(config, profile)
    configured = str((runtime.get("configs") or {}).get("yoloe_registry") or "").strip()
    registry_path = (
        Path(configured) if configured
        else Path(target_dir) / "yoloe_keyword_registry_default_v1.json"
    )
    base_registry = registry_path if registry_path.is_file() else _yoloe_registry_path(config)
    materialise_yoloe_registry(registry_path, base_registry, profile["yoloe_keywords"])
    runtime.setdefault("configs", {})["yoloe_registry"] = str(registry_path.absolute())


def snapshot(
    config: dict[str, Any], repository: Optional[ReadonlyMediaRepository], manager: Optional[SearchJobManager],
) -> dict[str, Any]:
    profile_path = application_state_root() / "profiles" / "processing_profile_v1.json"
    saved_profile = None
    if profile_path.is_file():
        try:
            saved_profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved_profile = None
    yoloe_keyword_profile = _effective_yoloe_profile(config, saved_profile)
    yoloe_default_keyword_profile = yoloe_profile_from_registry(
        load_yoloe_registry(_yoloe_registry_path(config))
    )
    current_task = task_state(config)
    acceptance_errors = task_output_acceptance(config, current_task)
    configured = repository is not None and manager is not None
    empty_overview = {
        "visible_media_types": ["image", "video"], "hidden_media_interfaces": ["audio", "text"],
        "source": {"image": {"count": 0, "bytes": 0}, "video": {"count": 0, "bytes": 0}},
        "source_total_count": 0, "source_total_bytes": 0,
        "visual_units": {"image": 0, "video": 0}, "visual_unit_total_count": 0,
        "recognition": {"openclip_visual_units": 0, "yoloe_detected_visual_units": 0, "qwen_success": 0, "ocr_completed": 0, "text_vectors": 0},
        "duplicate_group_count": 0, "timelapse_group_count": 0, "processing_error_count": 0,
        "latest_pipeline_activity": None, "storage": None,
    }
    empty_pipeline = {
        "stages": [], "overall_percent": 0.0, "failed_record_count": 0,
        "search_ready": False, "full_pipeline_launcher_status": "NOT_CONFIGURED",
    }
    database_read_error = ""

    def read_or(default: Any, operation: Any) -> Any:
        nonlocal database_read_error
        if operation is None:
            return default
        try:
            return operation()
        except (OSError, sqlite3.Error) as exc:
            database_read_error = str(exc)
            return default

    database = read_or(
        {"integrity_check": "temporarily_unavailable", "foreign_key_error_count": 0},
        repository.integrity if repository else None,
    )
    readiness = read_or(
        {"ready": False, "checks": {"library_configured": repository is not None}},
        manager.readiness if manager else None,
    )
    if current_task and current_task.get("status") != "success":
        readiness["ready"] = False
        readiness.setdefault("checks", {})["pipeline_complete"] = False
    elif current_task:
        readiness.setdefault("checks", {})["pipeline_complete"] = True
    if database.get("integrity_check") != "ok" or int(database.get("foreign_key_error_count") or 0) != 0:
        readiness["ready"] = False
        readiness.setdefault("checks", {})["database_integrity"] = False
    else:
        readiness.setdefault("checks", {})["database_integrity"] = True
    if acceptance_errors:
        readiness["ready"] = False
        readiness.setdefault("checks", {})["pipeline_output_acceptance"] = False
    repository_pipeline = read_or(empty_pipeline, repository.pipeline if repository else None)
    stage_metrics = read_or({}, repository.stage_metrics if repository else None)
    task_definition: dict[str, Any] = {}
    task_path_text = str(config.get("task_path") or "").strip()
    if task_path_text and Path(task_path_text).is_file():
        try:
            task_definition = load_json(Path(task_path_text))
        except (OSError, ValueError, json.JSONDecodeError):
            task_definition = {}
    pipeline = (
        task_pipeline(
            current_task, acceptance_errors, stage_metrics,
            estimate_task_remaining(current_task, stage_metrics, task_definition),
        )
        if current_task else repository_pipeline
    )
    if current_task:
        pipeline = reconcile_task_pipeline_with_library(
            pipeline, task_definition, readiness, repository_pipeline,
        )
    active_runs = read_or([], repository.active_runs if repository else None)
    if current_task:
        active = task_active_run(current_task)
        if active:
            active_runs.insert(0, active)
    recent_runs = read_or([], (lambda: repository.recent_runs(limit=30)) if repository else None)
    if current_task:
        recent_runs.insert(0, {
            "run_id": current_task.get("task_id"), "stage": "完整素材整理",
            "status": current_task.get("status"), "input_count": current_task.get("stage_count"),
            "output_count": current_task.get("completed_stage_count"),
            "started_at": epoch_timecode(current_task.get("started_at_epoch")),
            "finished_at": epoch_timecode(current_task.get("finished_at_epoch")),
            "error_message": current_task.get("error") or "",
        })
    contract_text = str(config.get("runtime_contract_path") or "").strip()
    contract_status = (
        validate_runtime_contract(Path(contract_text), model_root=selected_model_root())
        if contract_text else {
            "status": "NOT_CONFIGURED", "ready": False, "contract_version": None,
            "contract_path": "", "missing": ["runtime_contract_path"], "errors": [],
        }
    )
    report = {
        "status": (
            "RUNNING" if current_task and current_task.get("status") in {"queued", "running"}
            else "FAIL" if current_task and current_task.get("status") == "failed"
            else "FAIL" if acceptance_errors
            else "PASS" if configured and readiness["ready"]
            else "FIRST_RUN"
        ),
        "app": APP_NAME,
        "version": APP_VERSION,
        "ui_kind": "native_swiftui_python_backend",
        "web_server_used": False,
        "configuration_state": (
            "configured" if configured else
            ("processing" if current_task and current_task.get("status") in {"queued", "running"} else "first_run_clean")
        ),
        "overview": read_or(empty_overview, repository.overview if repository else None),
        "pipeline": pipeline,
        "database": database,
        "search_runtime": readiness,
        "runtime_contract": contract_status,
        "hardware": detect_hardware(),
        "resources": live_resource_snapshot(current_task),
        "recent_runs": recent_runs,
        "existing_libraries": existing_libraries(config),
        "active_runs": active_runs,
        "duplicate_groups": read_or({"total": 0, "offset": 0, "limit": 30, "items": []}, (lambda: repository.duplicate_groups(limit=30)) if repository else None),
        "timelapse_groups": read_or({"total": 0, "offset": 0, "limit": 20, "items": []}, (lambda: _timelapse_payload(repository)) if repository else None),
        "database_read_error": database_read_error,
        "saved_profile_path": str(profile_path),
        "has_saved_profile": saved_profile is not None,
        "saved_profile": saved_profile,
        "yoloe_keyword_profile": yoloe_keyword_profile,
        "yoloe_default_keyword_profile": yoloe_default_keyword_profile,
        "central_database_write": False,
        "model_run": False,
        "original_media_read": False,
    }
    return report


def save_profile(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    hardware = detect_hardware()
    profile = build_processing_profile(
        hardware,
        scheduler_mode=args.scheduler_mode,
        model_workers=args.model_workers,
        frame_extract_workers=args.frame_extract_workers,
        video_frame_interval_seconds=args.frame_interval_seconds,
        high_value_mode=args.high_value_mode,
        image_scope=args.image_scope,
    )
    registry = load_yoloe_registry(_yoloe_registry_path(config))
    default_keywords = yoloe_profile_from_registry(registry)
    profile["yoloe_keywords"] = normalise_yoloe_profile({
        "enable_b_extended": (
            getattr(args, "yoloe_enable_b_extended", None) == "true"
            if getattr(args, "yoloe_enable_b_extended", None) is not None
            else default_keywords["enable_b_extended"]
        ),
        "a_core": normalise_yoloe_entries(
            getattr(args, "yoloe_a_keywords", default_keywords["a_core"]), layer_name="A 层"
        ),
        "b_extended": normalise_yoloe_entries(
            getattr(args, "yoloe_b_keywords", default_keywords["b_extended"]), layer_name="B 层"
        ),
    }, fallback_registry=registry)
    # A default processing profile belongs to the application, not to whichever
    # library happens to be active while the user opens Settings.  New-task
    # creation reads this same path.
    path = save_processing_profile(application_state_root(), profile)
    return {
        "status": "PASS",
        "message": "处理方案已保存；新建任务将使用这份配置",
        "path": str(path),
        "identifier": profile["profile_id"],
        "central_database_write": False,
        "model_run": False,
    }


def save_task_draft(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve()
    name = " ".join(args.name.split())
    if not source.is_dir():
        raise ValueError("请选择一个当前可以读取的素材文件夹")
    if not name:
        raise ValueError("任务名称不能为空")
    semantic = f"{source}\0{name}\0{time.time_ns()}"
    task_id = "task_" + hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:20]
    workspace_root = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else runtime_output_root(config)
    task_dir = workspace_root / "task_drafts" / task_id
    task_dir.mkdir(parents=True, exist_ok=False)
    payload = {
        "task_contract": "media_archive_image_video_task_draft_v1",
        "task_id": task_id,
        "name": name,
        "source_root": str(source),
        "source_access": "read_only",
        "visible_media_types": ["image", "video"],
        "hidden_media_interfaces": ["audio", "text"],
        "mode": args.task_mode,
        "workspace": str(task_dir / "workspace"),
        "status": "AWAITING_FROZEN_PIPELINE_ORCHESTRATOR",
        "central_database_write": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = task_dir / "task.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "PASS",
        "message": "任务配置已保存；尚未启动模型",
        "path": str(path),
        "identifier": task_id,
        "central_database_write": False,
        "model_run": False,
    }


def _load_or_default_profile(config: dict[str, Any]) -> dict[str, Any]:
    path = application_state_root() / "profiles" / "processing_profile_v1.json"
    if path.is_file():
        profile = json.loads(path.read_text(encoding="utf-8"))
        profile["yoloe_keywords"] = _effective_yoloe_profile(config, profile)
        return profile
    hardware = detect_hardware()
    profile = build_processing_profile(
        hardware,
        scheduler_mode="stage_serial",
        model_workers=hardware["recommendation"]["model_workers"],
        frame_extract_workers=hardware["recommendation"]["frame_extract_workers"],
        video_frame_interval_seconds=3.0,
        high_value_mode="target_15",
        image_scope="frozen_current_policy",
    )
    profile["yoloe_keywords"] = _effective_yoloe_profile(config)
    return profile


def _helper_command(config_path: Path, command: str, *arguments: str) -> list[str]:
    package = Path(__file__).resolve()
    bundled = package.parents[2] / "Helpers" / "素材大整理Python"
    if bundled.is_file():
        return [str(bundled), "--config", str(config_path), command, *arguments]
    return [sys.executable, "-m", "media_archive_image_video_ui.native_bridge", "--config", str(config_path), command, *arguments]


def _launch_task_worker(config_path: Path, task_path: Path, log_path: Path, *, resume: bool) -> int:
    arguments = ["--task", str(task_path)]
    if resume:
        arguments.append("--resume")
    command = _helper_command(config_path, "pipeline-worker", *arguments)
    environment = os.environ.copy()
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    })
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command, stdout=log_handle, stderr=subprocess.STDOUT,
            env=environment, start_new_session=True,
        )
    finally:
        log_handle.close()
    return process.pid


def start_task(config: dict[str, Any], config_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    lock_path = application_state_root() / "runtime" / "start_task.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("任务正在启动，请不要重复点击") from exc
        active_path = active_library_path()
        if active_path.is_file():
            try:
                active = json.loads(active_path.read_text(encoding="utf-8"))
                state_path = Path(str(active.get("task_state_path") or ""))
                active_state = load_json(state_path) if state_path.is_file() else {}
            except (OSError, ValueError, json.JSONDecodeError):
                active_state = {}
            if active_state.get("status") in {"queued", "running"}:
                raise RuntimeError("已有完整整理任务正在运行，不能重复启动")
        return _start_task_locked(config, config_path, args)


def start_existing_task(
    config: dict[str, Any], config_path: Path, args: argparse.Namespace,
) -> dict[str, Any]:
    if args.task_mode not in {"incremental", "repair", "repair_images", "rebuild_search", "audio_enrichment"}:
        raise ValueError(f"不支持的已有素材库维护方式：{args.task_mode}")
    library_task_path = Path(args.task).expanduser().resolve(strict=True)
    library_record = _existing_library_record(library_task_path)
    if library_record is None:
        raise ValueError("所选素材库记录不完整，无法修复")
    known = {row["task_path"] for row in existing_libraries(config)}
    if str(library_task_path) not in known:
        raise ValueError("请选择列表中已经登记的素材库")

    lock_path = application_state_root() / "runtime" / "start_task.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("任务正在启动，请不要重复点击") from exc
        active_path = active_library_path()
        if active_path.is_file():
            try:
                active = load_json(active_path)
                active_state_path = Path(str(active.get("task_state_path") or ""))
                active_state = load_json(active_state_path) if active_state_path.is_file() else {}
            except (OSError, ValueError, json.JSONDecodeError):
                active_state = {}
            if active_state.get("status") in {"queued", "running"}:
                pid = int(active_state.get("worker_pid") or 0)
                if pid <= 1 or process_is_alive(pid):
                    raise RuntimeError("已有整理或修复任务正在运行，不能重复启动")

        library_task = load_json(library_task_path)
        database = Path(str(library_task["database"])).expanduser().resolve(strict=True)
        source = Path(str(library_task["source_root"])).expanduser().resolve(strict=True)
        workspace = Path(str(library_task["workspace"])).expanduser().resolve(strict=True)
        contract_path = Path(str(config.get("runtime_contract_path") or "")).expanduser().absolute()
        model_root = selected_model_root()
        contract_report = validate_runtime_contract(contract_path, model_root=model_root)
        if not contract_report["ready"]:
            raise RuntimeError("本机固定运行组件自检失败：" + "、".join(
                contract_report["missing"] + contract_report["errors"]
            ))
        contract = load_runtime_contract(contract_path, model_root=model_root)
        profile = dict(library_task.get("profile") or _load_or_default_profile(config))
        hardware = detect_hardware()
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        directory_time = time.strftime("%Y%m%d_%H%M%S")
        mode_labels = {
            "incremental": "增量整理",
            "repair": "修复缺失内容",
            "repair_images": "补充缺失图片描述",
            "rebuild_search": "重建搜索入口",
            "audio_enrichment": "补充音频搜索",
        }
        task_prefixes = {
            "incremental": "incremental_",
            "repair": "repair_",
            "repair_images": "repair_images_",
            "rebuild_search": "rebuild_",
            "audio_enrichment": "audio_",
        }
        mode_label = mode_labels[args.task_mode]
        task_prefix = task_prefixes[args.task_mode]
        task_id = task_prefix + hashlib.sha256(
            f"{library_task_path}\0{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:20]
        repair_dir = library_task_path.parent / "maintenance_runs" / f"{directory_time}_{mode_label}"
        suffix = 2
        while repair_dir.exists():
            repair_dir = repair_dir.with_name(f"{directory_time}_{mode_label}_{suffix}")
            suffix += 1
        state_path = repair_dir / "pipeline_state.json"
        log_path = repair_dir / "logs/pipeline.log"
        stage_output_root = workspace / "maintenance_runs" / task_id / "stages"
        repair_dir.mkdir(parents=True, exist_ok=False)
        log_path.parent.mkdir(parents=True)
        runtime = task_runtime_from_contract(
            contract,
            ocr_workers=int(hardware["recommendation"]["ocr_workers"]),
            embedding_workers=int(hardware["recommendation"]["embedding_workers"]),
            requested_scheduler_mode=str(profile.get("scheduler", {}).get("mode") or "stage_serial"),
            effective_config_dir=repair_dir / "runtime_configs",
        )
        _materialise_task_yoloe_registry(config, runtime, profile, repair_dir / "runtime_configs")
        payload = {
            "task_contract": "media_archive_image_video_maintenance_task_v1",
            "task_id": task_id, "name": str(library_task.get("name") or "未命名素材库"),
            "mode": args.task_mode, "library_task_path": str(library_task_path),
            "central_library_task_id": str(
                library_task.get("central_library_task_id")
                or library_task.get("task_id")
                or library_task_path.parent.name
            ),
            "source_root": str(source), "source_access": "read_only",
            "workspace": str(workspace), "database": str(database),
            "stage_output_root": str(stage_output_root),
            "state_path": str(state_path), "log_path": str(log_path),
            "profile": profile, "runtime": runtime, "status": "queued",
            "software_version": APP_VERSION,
            "central_contract_required": True,
            "strong_fingerprint_required": True,
            "created_at": created_at,
        }
        task_path = repair_dir / "task.json"
        task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        active = {
            "database": str(database), "output_root": str(workspace),
            "task_id": task_id, "task_path": str(task_path),
            "library_task_path": str(library_task_path),
            "task_state_path": str(state_path), "task_name": payload["name"],
            "task_directory": str(repair_dir), "created_at": created_at,
        }
        active_path.parent.mkdir(parents=True, exist_ok=True)
        active_path.write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        register_library(library_task_path)
        pid = _launch_task_worker(config_path, task_path, log_path, resume=False)
        (repair_dir / "pipeline.pid").write_text(f"{pid}\n", encoding="utf-8")
    messages = {
        "incremental": "增量整理已启动；沿用所选素材库，只处理新增、变更或缺失结果",
        "repair": "缺失内容修复已启动；已有成功结果不会重跑",
        "repair_images": "图片描述专项修复已启动；已有成功结果不会重跑",
        "rebuild_search": "搜索入口重建已启动；只复用已有数据库，不读取素材、不运行识别模型",
        "audio_enrichment": "音频搜索补充已启动；只处理视频人声并写入当前索引，前19阶段不会重跑，临时音频会在转写落账后删除",
    }
    return {
        "status": "PASS", "message": messages[args.task_mode],
        "path": str(task_path), "identifier": task_id,
        "central_database_write": True,
        "model_run": args.task_mode in {"incremental", "repair", "repair_images", "audio_enrichment"},
    }


def _start_task_locked(config: dict[str, Any], config_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve(strict=True)
    library_root = Path(args.workspace_root).expanduser().resolve()
    name = " ".join(args.name.split())
    if not source.is_dir():
        raise ValueError("请选择一个当前可以读取的素材文件夹")
    if not name:
        raise ValueError("任务名称不能为空")
    if args.task_mode != "full":
        raise ValueError("当前正式入口先开放“第一次完整整理”；增量和修复将在完整流程验收后开放")
    if library_root == source or library_root in source.parents or source in library_root.parents:
        raise ValueError("索引保存位置必须与原始素材文件夹分开")
    contract_text = str(config.get("runtime_contract_path") or "").strip()
    if not contract_text:
        raise RuntimeError("应用缺少固定运行时合同，请重新安装完整运行版")
    contract_path = Path(contract_text).expanduser().absolute()
    model_root = selected_model_root()
    contract_report = validate_runtime_contract(contract_path, model_root=model_root)
    if not contract_report["ready"]:
        details = contract_report["missing"] + contract_report["errors"]
        raise RuntimeError("本机固定运行组件自检失败：" + "、".join(details))
    contract = load_runtime_contract(contract_path, model_root=model_root)
    project_root = Path(contract["project_root"]).expanduser().absolute()
    library_root.mkdir(parents=True, exist_ok=True)
    profile = _load_or_default_profile(config)
    sampling = profile.get("video_sampling", {})
    policy = profile.get("high_value_policy", {})
    if float(sampling.get("frame_interval_seconds") or 3) not in {1, 2, 3, 4, 5}:
        raise ValueError("视频抽帧间隔必须是 1、2、3、4 或 5 秒")
    if policy.get("mode") not in {
        "frozen_v25_compatible", "target_15", "target_20", "target_30"
    }:
        raise ValueError("高价值分析密度配置无效，请重新保存处理设置")
    if policy.get("image_scope") not in {"frozen_current_policy", "all_images"}:
        raise ValueError("图片分析范围配置无效，请重新保存处理设置")
    requested_scheduler = str(profile.get("scheduler", {}).get("mode") or "auto")
    profile["execution_scheduler_mode"] = "stage_serial"
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    directory_time = time.strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("._")[:60] or "素材整理"
    semantic = f"{source}\0{library_root}\0{name}\0{time.time_ns()}"
    task_id = "task_" + hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:20]
    task_base = library_root / "tasks" / f"{directory_time}_{safe_name}"
    task_dir = task_base
    suffix = 2
    while task_dir.exists():
        task_dir = task_base.with_name(f"{task_base.name}_{suffix}")
        suffix += 1
    workspace = task_dir / "workspace"
    state_path = task_dir / "pipeline_state.json"
    log_path = task_dir / "logs" / "pipeline.log"
    task_dir.mkdir(parents=True, exist_ok=False)
    workspace.mkdir(parents=True)
    log_path.parent.mkdir(parents=True)
    hardware = detect_hardware()
    runtime = task_runtime_from_contract(
        contract,
        ocr_workers=int(hardware["recommendation"]["ocr_workers"]),
        embedding_workers=int(hardware["recommendation"]["embedding_workers"]),
        requested_scheduler_mode=requested_scheduler,
        effective_config_dir=task_dir / "runtime_configs",
    )
    _materialise_task_yoloe_registry(config, runtime, profile, task_dir / "runtime_configs")
    payload = {
        "task_contract": "media_archive_image_video_task_v1",
        "task_id": task_id,
        "task_directory_name": task_dir.name,
        "name": name,
        "source_root": str(source),
        "source_access": "read_only",
        "mode": "full",
        "workspace": str(workspace),
        "database": str(workspace / "media_archive.sqlite"),
        "state_path": str(state_path),
        "log_path": str(log_path),
        "status": "queued",
        "profile": profile,
        "runtime": runtime,
        "software_version": APP_VERSION,
        "central_contract_required": True,
        "strong_fingerprint_required": True,
        "created_at": created_at,
    }
    task_path = task_dir / "task.json"
    task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    active = {
        "database": payload["database"],
        "output_root": str(workspace),
        "task_id": task_id,
        "task_path": str(task_path),
        "task_state_path": str(state_path),
        "task_name": name,
        "task_directory": str(task_dir),
        "created_at": created_at,
    }
    active_path = active_library_path()
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    register_library(task_path)
    pid = _launch_task_worker(config_path, task_path, log_path, resume=False)
    (task_dir / "pipeline.pid").write_text(f"{pid}\n", encoding="utf-8")
    return {
        "status": "PASS",
        "message": (
            "完整整理已启动，正在按阶段自动接力"
            + ("；数据库流水线异步尚未开放，本次使用可靠的阶段串行" if requested_scheduler == "pipeline_async" else "")
        ),
        "path": str(task_path),
        "identifier": task_id,
        "central_database_write": True,
        "model_run": True,
    }


def resume_active_task(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    task_path_text = str(config.get("task_path") or "").strip()
    if not task_path_text:
        raise RuntimeError("没有可以继续的任务")
    task_path = Path(task_path_text).expanduser().resolve(strict=True)
    task = load_json(task_path)
    state_path = Path(task["state_path"])
    if not state_path.is_file():
        raise RuntimeError("任务尚未留下可恢复的阶段状态")
    state = load_json(state_path)
    if state.get("status") in {"queued", "running"}:
        pid = int(state.get("worker_pid") or 0)
        if process_is_alive(pid):
            raise RuntimeError("任务仍在运行，不需要重复启动")
    if state.get("status") == "success":
        records = {
            str(row.get("key") or ""): row
            for row in state.get("stages", [])
            if isinstance(row, dict)
        }
        current_plan = build_stage_plan(task)
        plan_is_complete = all(
            records.get(str(stage["key"]), {}).get("status") == "success"
            and not validate_stage_acceptance(task, str(stage["key"]))
            for stage in current_plan
        )
        if plan_is_complete:
            return {"status": "PASS", "message": "任务已经全部完成，不需要续跑"}
    log_path = Path(task["log_path"])
    pid = _launch_task_worker(config_path, task_path, log_path, resume=True)
    task["status"] = "queued"
    task.pop("finished_at_epoch", None)
    task.pop("error", None)
    atomic_path = task_path.with_name(f".{task_path.name}.{os.getpid()}.tmp")
    atomic_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(atomic_path, task_path)
    (task_path.parent / "pipeline.pid").write_text(f"{pid}\n", encoding="utf-8")
    return {
        "status": "PASS",
        "message": "已从断点继续；成功阶段不会重跑",
        "path": str(task_path),
        "identifier": task.get("task_id"),
        "central_database_write": True,
        "model_run": True,
    }


def stop_active_task(config: dict[str, Any]) -> dict[str, Any]:
    state = task_state(config)
    if not state:
        return {"status": "PASS", "message": "当前没有运行中的任务"}
    return stop_pipeline(Path(str(config["task_state_path"])))


SEARCH_RESULT_CONTRACT_VERSION = "media_archive_search_result_v1"
SEARCH_LOG_MAX_BYTES = 32 * 1024
SEARCH_RESULT_MAX_BYTES = 2 * 1024 * 1024
SEARCH_PROGRESS_PREFIX = "SEARCH_PROGRESS_JSON="


def _atomic_search_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _bounded_log_text(path: Path, maximum_bytes: int = SEARCH_LOG_MAX_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - maximum_bytes))
            return handle.read(maximum_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _search_environment(
    cache_root: Path,
    *,
    version: str = APP_VERSION,
) -> dict[str, str]:
    environment = offline_search_environment(cache_root)
    namespace = hashlib.sha256(version.encode("utf-8")).hexdigest()[:8]
    # Keep the Unix socket path short enough for macOS' AF_UNIX limit.
    worker_root = Path("/tmp") / f"mo-search-{os.getuid()}-{namespace}"
    environment["MEDIA_ARCHIVE_SEARCH_WORKER_ROOT"] = str(worker_root)
    environment["MEDIA_ARCHIVE_SEARCH_DATA_CACHE"] = str(cache_root / "data_cache_v1")
    return environment


def _run_streaming_search_command(
    command: list[str],
    log_path: Path,
    environment: dict[str, str],
) -> int:
    """Capture the full child log while forwarding only safe progress records."""
    terminated_by_signal = 0
    process: subprocess.Popen[str] | None = None
    previous_handlers: dict[int, Any] = {}

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal terminated_by_signal
        terminated_by_signal = signum
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    pass

    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            start_new_session=True,
        )
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            except ValueError:
                pass
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                if line.startswith(SEARCH_PROGRESS_PREFIX):
                    sys.stderr.write(line)
                    sys.stderr.flush()
            return_code = int(process.wait())
        finally:
            for signum, previous in previous_handlers.items():
                try:
                    signal.signal(signum, previous)
                except ValueError:
                    pass
    return 128 + terminated_by_signal if terminated_by_signal else return_code


def _search_file_snapshot(root: Path) -> dict[str, int]:
    if not root.is_dir():
        return {}
    result: dict[str, int] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "search_summary.json":
            continue
        try:
            result[str(path.relative_to(root))] = path.stat().st_size
        except OSError:
            continue
    return result


def _search_disk_audit(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    changed = [
        {
            "path": name,
            "before_bytes": int(before.get(name, 0)),
            "after_bytes": int(after[name]),
            "delta_bytes": int(after[name] - before.get(name, 0)),
        }
        for name in sorted(after)
        if before.get(name) != after[name]
    ]
    removed = sorted(name for name in before if name not in after)
    return {
        "changed_file_count": len(changed),
        "changed_files": changed,
        "removed_files": removed,
        "total_bytes_before": sum(before.values()),
        "total_bytes_after": sum(after.values()),
        "total_byte_change": sum(after.values()) - sum(before.values()),
        "measurement_excludes": ["current/search_summary.json"],
    }


def _validate_search_result_contract(payload: dict[str, Any], query: str) -> None:
    required = {"query", "result_count", "result_items"}
    missing = sorted(required - set(payload))
    if payload.get("contract_version") != SEARCH_RESULT_CONTRACT_VERSION:
        raise ValueError("search_result_contract_version_invalid")
    if missing:
        raise ValueError("search_result_contract_fields_missing:" + ",".join(missing))
    if payload.get("query") != query:
        raise ValueError("search_result_contract_query_mismatch")
    items = payload.get("result_items")
    if not isinstance(items, list) or int(payload.get("result_count", -1)) != len(items):
        raise ValueError("search_result_contract_count_invalid")
    item_fields = {
        "source_path", "media_type", "preview_path", "time_position_ms",
        "hit_reason", "hit_field", "score", "source_online", "can_open_original",
    }
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"search_result_item_not_object:{index}")
        missing_item = sorted(item_fields - set(item))
        if missing_item:
            raise ValueError(
                f"search_result_item_fields_missing:{index}:" + ",".join(missing_item)
            )
        reasons = item.get("relevance_reasons") or []
        labels = item.get("matched_object_labels") or []
        if "exact_object_label" in reasons and not labels:
            raise ValueError(f"search_result_object_label_evidence_missing:{index}")


def _media_timecode(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _legacy_person_annotations(database: Path) -> tuple[Path, dict[str, Any]]:
    path = annotation_path(application_state_root(), database)
    return path, load_annotations(path)


def _load_person_annotation_payload(database: Path) -> tuple[dict[str, Any], str, str]:
    """Read task-owned metadata first without modifying a legacy library."""
    try:
        task_id = task_id_for_database(database)
        central = load_central_person_annotations(database, task_id)
        if central.get("identities") or central.get("cluster_to_identity"):
            return central, "central_database", task_id
        legacy_path, legacy = _legacy_person_annotations(database)
        if legacy.get("identities") or legacy.get("cluster_to_identity"):
            return legacy, str(legacy_path), task_id
        return central, "central_database", task_id
    except (OSError, RuntimeError, sqlite3.Error):
        path, payload = _legacy_person_annotations(database)
        return payload, str(path), ""


def _task_payload_for_database(database: Path) -> dict[str, Any]:
    task_path = database.parent.parent / "task.json"
    if task_path.is_file():
        try:
            payload = load_json(task_path)
            payload["task_path"] = str(task_path)
            payload.setdefault("software_version", APP_VERSION)
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    stable = hashlib.sha256(str(database.resolve()).encode("utf-8")).hexdigest()[:20]
    return {
        "task_id": f"legacy_{stable}",
        "name": database.parent.parent.name or "历史素材库",
        "task_path": str(task_path) if task_path.is_file() else "",
        "source_root": "",
        "workspace": str(database.parent),
        "database": str(database),
        "software_version": APP_VERSION,
        "mode": "legacy_migration",
        "status": "completed",
    }


def _load_writable_person_annotation_payload(
    database: Path,
) -> tuple[dict[str, Any], str]:
    """Migrate local person metadata once, with a SQLite backup, on first edit."""
    legacy_path, legacy = _legacy_person_annotations(database)
    report = ensure_central_schema(
        database,
        task=_task_payload_for_database(database),
        backup_dir=database.parent.parent / "backups" / "central_schema",
    )
    task_id = task_id_for_database(database)
    payload = load_central_person_annotations(database, task_id)
    if (
        not payload.get("identities")
        and not payload.get("cluster_to_identity")
        and (legacy.get("identities") or legacy.get("cluster_to_identity"))
    ):
        save_central_person_annotations(database, task_id, legacy)
        payload = load_central_person_annotations(database, task_id)
    return payload, task_id


def _save_person_annotation_payload(
    database: Path, task_id: str, payload: dict[str, Any]
) -> None:
    save_central_person_annotations(database, task_id, payload)


def _manual_person_links(
    database: Path | None, visual_unit_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    identifiers = {str(value) for value in visual_unit_ids if str(value)}
    if not identifiers or database is None:
        return {}
    payload = _load_person_annotation_payload(database)[0]
    identities = dict(payload.get("identities") or {})
    result: dict[str, list[dict[str, Any]]] = {}
    for identity_id, memberships in dict(payload.get("visual_memberships") or {}).items():
        identity = dict(identities.get(identity_id) or {})
        clean_members = [dict(value or {}) for value in memberships or []]
        sources = {str(value.get("source_content_id") or "") for value in clean_members}
        for value in clean_members:
            visual_id = str(value.get("visual_unit_id") or "")
            if visual_id not in identifiers:
                continue
            result.setdefault(visual_id, []).append({
                "person_cluster_id": str(identity_id),
                "member_count": len(clean_members),
                "distinct_source_count": len({source for source in sources if source}),
                "cluster_confidence": "human_confirmed",
                "human_review_status": "confirmed",
                "display_name": str(identity.get("display_name") or "").strip() or "自建人物",
                "is_local_identity": True,
                "manual_assignment": True,
            })
    return result


def _merge_person_links(
    machine: dict[str, list[dict[str, Any]]],
    manual: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged = {key: [dict(value) for value in values] for key, values in machine.items()}
    for visual_id, links in manual.items():
        existing = {str(value.get("person_cluster_id") or "") for value in merged.get(visual_id, [])}
        merged.setdefault(visual_id, []).extend(
            dict(value) for value in links
            if str(value.get("person_cluster_id") or "") not in existing
        )
    return merged


def run_source_frame_search(
    repository: ReadonlyMediaRepository,
    source_content_id: str,
    preview_window_ms: int,
    result_offset: int = 0,
    result_limit: int = 60,
) -> dict[str, Any]:
    """Browse all existing indexed frames for one source, without models."""
    started = time.monotonic()
    page = repository.visual_frame_results(
        source_content_id=source_content_id,
        offset=result_offset,
        limit=result_limit,
    )
    half_window = max(0, int(preview_window_ms) // 2)
    items: list[dict[str, Any]] = []
    for row in page["items"]:
        position = max(0, int(row.get("time_position_ms") or 0))
        items.append({
            "result_id": f"source_frame_{row['visual_unit_id']}",
            "visual_unit_id": str(row["visual_unit_id"]),
            "source_content_id": str(row["source_content_id"]),
            "source_frame_count": int(row.get("source_frame_count") or page["total"]),
            "result_level": "frame",
            "derived_id": str(row["derived_id"]),
            "source_relative_path": str(row.get("relative_path") or ""),
            "media_type": str(row.get("media_type") or ""),
            "time_position_ms": position,
            "timecode": _media_timecode(position),
            "preview_segment_start_ms": max(0, position - half_window),
            "preview_segment_start_timecode": _media_timecode(max(0, position - half_window)),
            "preview_segment_end_timecode": _media_timecode(position + half_window),
            "preview_path": str(row.get("preview_path") or ""),
            "source_path": str(row.get("source_path") or ""),
            "source_online": bool(row.get("source_online")),
            "can_open_original": bool(row.get("can_open_original")),
            "score": 1.0,
            "hybrid_score": None,
            "openclip_cosine": None,
            "text_semantic_score": None,
            "text_preview": f"该视频在 {_media_timecode(position)} 的已索引画面。",
            "environment_label": "该视频全部索引画面",
            "hit_reason": "source_timeline",
            "hit_field": "source_content_id",
            "relevance_reasons": ["source_timeline"],
        })
    visual_ids = [str(row.get("visual_unit_id") or "") for row in items]
    links = _merge_person_links(
        repository.person_clusters_for_visual_units(visual_ids),
        _manual_person_links(getattr(repository, "db_path", None), visual_ids),
    )
    for item in items:
        item["person_clusters"] = links.get(str(item["visual_unit_id"]), [])
    return {
        "status": "PASS", "query": "该视频全部索引画面",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "coverage": None, "result_count": len(items), "result_items": items,
        "result_total_count": int(page["total"]),
        "result_offset": int(page["offset"]), "result_limit": int(page["limit"]),
        "next_result_offset": page["next_offset"],
        "result_count_by_media": page["count_by_media"],
        "database_write": False, "model_run": False, "network_used": False,
        "original_media_read": False,
    }


def run_person_cluster_search(
    repository: ReadonlyMediaRepository,
    person_cluster_id: str,
    media_type: str,
    preview_window_ms: int,
    result_offset: int = 0,
    result_limit: int = 30,
    source_content_id: str | None = None,
) -> dict[str, Any]:
    """Return machine clusters or one locally merged identity, read-only."""
    started = time.monotonic()
    repository_db = getattr(repository, "db_path", None)
    annotations = _load_person_annotation_payload(Path(repository_db))[0] if repository_db else {
        "contract": "media_archive_local_person_annotations_v1", "identities": {},
        "cluster_to_identity": {}, "visual_memberships": {},
    }
    resolved_cluster_ids = resolve_clusters(annotations, person_cluster_id)
    cluster_selector: str | list[str] = (
        resolved_cluster_ids[0] if len(resolved_cluster_ids) == 1
        else (resolved_cluster_ids or [person_cluster_id])
    )
    identities = dict(annotations.get("identities") or {})
    cluster_to_identity = dict(annotations.get("cluster_to_identity") or {})
    identity_id = (
        person_cluster_id if person_cluster_id in identities
        else str(cluster_to_identity.get(person_cluster_id) or "")
    )
    manual_memberships = list(
        dict(annotations.get("visual_memberships") or {}).get(identity_id, [])
    )
    extra_visual_ids = [str(value.get("visual_unit_id") or "") for value in manual_memberships]
    if source_content_id:
        page = repository.person_cluster_results(
            cluster_selector, media_type, result_offset, result_limit, source_content_id,
            extra_visual_ids, identity_id,
        )
    else:
        page = repository.person_cluster_results(
            cluster_selector, media_type, result_offset, result_limit, None,
            extra_visual_ids, identity_id,
        )
    half_window = max(0, int(preview_window_ms) // 2)
    items = []
    for row in page["items"]:
        position = max(0, int(row.get("time_position_ms") or 0))
        start_ms = max(0, position - half_window)
        end_ms = position + half_window
        cluster_id = str(row["person_cluster_id"])
        visual_id = str(row["visual_unit_id"])
        row_identity_id = str(cluster_to_identity.get(cluster_id) or identity_id or person_cluster_id)
        identity = dict(identities.get(row_identity_id) or {})
        person_name = str(identity.get("display_name") or "").strip() or "同一匿名人物"
        items.append({
            "result_id": f"person5e_{cluster_id}_{visual_id}",
            "visual_unit_id": visual_id,
            "source_content_id": str(row["source_content_id"]),
            "source_frame_count": int(row.get("source_frame_count") or 1),
            "result_level": str(row.get("result_level") or "frame"),
            "derived_id": str(row["derived_id"]),
            "source_relative_path": str(row.get("relative_path") or ""),
            "media_type": str(row["media_type"]),
            "time_position_ms": position,
            "timecode": _media_timecode(position),
            "preview_segment_start_ms": start_ms,
            "preview_segment_start_timecode": _media_timecode(start_ms),
            "preview_segment_end_timecode": _media_timecode(end_ms),
            "preview_path": str(row.get("preview_path") or ""),
            "source_path": str(row.get("source_path") or ""),
            "source_online": bool(row.get("source_online")),
            "can_open_original": bool(row.get("can_open_original")),
            "score": float(row.get("similarity_to_representative") or 0.0),
            "hybrid_score": None,
            "openclip_cosine": None,
            "text_semantic_score": None,
            "text_preview": (
                f"该素材内有 {int(row.get('source_frame_count') or 1)} 个画面属于所选待确认人物组；"
                if row.get("result_level") == "source" else
                "该画面属于所选待确认人物组；"
            ) + "这只表示本地人脸特征相似，不代表已确认真实身份。",
            "environment_label": "同一人物扩展",
            "hit_reason": "same_person_reid",
            "hit_field": "person_reid",
            "relevance_reasons": ["same_person_reid"],
            "person_clusters": [{
                "person_cluster_id": row_identity_id,
                "member_count": int(row.get("member_count") or 0),
                "distinct_source_count": int(row.get("distinct_source_count") or 0),
                "cluster_confidence": str(row.get("cluster_confidence") or ""),
                "human_review_status": str(row.get("human_review_status") or ""),
                "display_name": person_name,
                "is_local_identity": row_identity_id in identities,
                "manual_assignment": visual_id in extra_visual_ids,
            }],
        })
    return {
        "status": "PASS",
        "query": "同一匿名人物",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "coverage": None,
        "result_count": len(items),
        "result_items": items,
        "result_total_count": int(page["total"]),
        "result_offset": int(page["offset"]),
        "result_limit": int(page["limit"]),
        "next_result_offset": page["next_offset"],
        "result_count_by_media": page["count_by_media"],
        "person_frame_total_count": int(page.get("frame_total") or page["total"]),
        "result_level": "frame" if source_content_id else "source",
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "original_media_read": False,
    }


def run_person_track_suggestions(
    repository: ReadonlyMediaRepository,
    person_cluster_id: str,
    media_type: str,
    preview_window_ms: int,
    result_offset: int = 0,
    result_limit: int = 30,
) -> dict[str, Any]:
    """Suggest nearby side/back views without changing machine or human identity data."""
    started = time.monotonic()
    annotations = _load_person_annotation_payload(repository.db_path)[0]
    resolved_cluster_ids = resolve_clusters(annotations, person_cluster_id)
    identities = dict(annotations.get("identities") or {})
    cluster_to_identity = dict(annotations.get("cluster_to_identity") or {})
    identity_id = (
        person_cluster_id if person_cluster_id in identities
        else str(cluster_to_identity.get(person_cluster_id) or "")
    )
    identity = dict(identities.get(identity_id) or {})
    display_name = str(identity.get("display_name") or "").strip() or "所选待确认人物"
    existing_manual_ids = {
        str(value.get("visual_unit_id") or "")
        for value in dict(annotations.get("visual_memberships") or {}).get(identity_id, [])
    }
    suggestions = [
        value for value in load_database_suggestions(
            repository.db_path, resolved_cluster_ids or [person_cluster_id]
        )
        if value.visual_unit_id not in existing_manual_ids
    ]
    if media_type == "image":
        suggestions = []
    safe_offset = max(0, int(result_offset))
    safe_limit = max(1, min(int(result_limit), 100))
    selected = suggestions[safe_offset:safe_offset + safe_limit]
    page = repository.visual_frame_results(
        visual_unit_ids=[value.visual_unit_id for value in selected],
        media_type="video",
        offset=0,
        limit=max(1, len(selected)),
    ) if selected else {"items": []}
    rows = {str(row["visual_unit_id"]): dict(row) for row in page["items"]}
    half_window = max(0, int(preview_window_ms) // 2)
    items: list[dict[str, Any]] = []
    for suggestion in selected:
        row = rows.get(suggestion.visual_unit_id)
        if row is None:
            continue
        position = max(0, int(row.get("time_position_ms") or suggestion.time_position_ms))
        start_ms = max(0, position - half_window)
        items.append({
            "result_id": f"person_track_{suggestion.visual_unit_id}",
            "visual_unit_id": suggestion.visual_unit_id,
            "source_content_id": suggestion.source_content_id,
            "source_frame_count": 1,
            "result_level": "frame",
            "derived_id": str(row["derived_id"]),
            "source_relative_path": str(row.get("relative_path") or ""),
            "media_type": str(row.get("media_type") or "video"),
            "time_position_ms": position,
            "timecode": _media_timecode(position),
            "preview_segment_start_ms": start_ms,
            "preview_segment_start_timecode": _media_timecode(start_ms),
            "preview_segment_end_timecode": _media_timecode(position + half_window),
            "preview_path": str(row.get("preview_path") or ""),
            "source_path": str(row.get("source_path") or ""),
            "source_online": bool(row.get("source_online")),
            "can_open_original": bool(row.get("can_open_original")),
            "score": suggestion.score,
            "hybrid_score": None,
            "openclip_cosine": None,
            "text_semantic_score": None,
            "text_preview": (
                f"这是“{display_name}”在同一视频、邻近时间中的侧脸/背影候选；"
                f"距离已识别人脸锚点 {suggestion.anchor_distance_ms / 1000:.1f} 秒。"
                f"{suggestion.review_reason}。确认前不会加入人物。"
            ),
            "environment_label": "同视频人物轨迹候选",
            "hit_reason": "same_person_track_suggestion",
            "hit_field": "person_track_suggestion",
            "relevance_reasons": ["same_person_track_suggestion"],
            "person_clusters": [],
        })
    return {
        "status": "PASS",
        "query": f"{display_name}的侧脸/背影候选",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "coverage": None,
        "result_count": len(items),
        "result_items": items,
        "result_total_count": len(suggestions),
        "result_offset": safe_offset,
        "result_limit": safe_limit,
        "next_result_offset": (
            safe_offset + safe_limit
            if safe_offset + safe_limit < len(suggestions) else None
        ),
        "result_count_by_media": {"video": len(suggestions)},
        "database_write": False,
        "model_run": False,
        "original_media_read": False,
    }


def run_favorite_collection(
    repository: ReadonlyMediaRepository,
    result_offset: int = 0,
    result_limit: int = 200,
) -> dict[str, Any]:
    """Return source-level user favorites without running search or models.

    Existing 1.2.0 annotations are keyed by source_content_id.  Keeping that
    contract makes this view immediately useful for historical libraries and
    avoids a destructive schema migration.  The representative frame and
    descriptive evidence are read from already-generated local artifacts.
    """
    started = time.monotonic()
    task_id = task_id_for_database(repository.db_path)
    safe_offset = max(0, int(result_offset))
    safe_limit = max(1, min(int(result_limit), 500))
    with repository.connect() as con:
        if not ReadonlyMediaRepository._table_exists(con, "user_asset_annotations"):
            return {
                "status": "PASS", "result_count": 0, "result_items": [],
                "result_total_count": 0, "result_offset": safe_offset,
                "result_limit": safe_limit, "next_result_offset": None,
                "database_write": False, "model_run": False,
                "original_media_read": False,
            }
        total = int(con.execute(
            "SELECT COUNT(*) FROM user_asset_annotations WHERE task_id=? AND favorite=1",
            (task_id,),
        ).fetchone()[0])
        rows = con.execute(
            """SELECT a.source_content_id,a.tags_json,a.note,a.favorite,a.rating,
                      a.ignored,a.updated_at,s.relative_path,s.absolute_path,
                      s.media_type
               FROM user_asset_annotations AS a
               JOIN source_assets AS s USING(source_content_id)
               WHERE a.task_id=? AND a.favorite=1
               ORDER BY a.updated_at DESC,a.source_content_id
               LIMIT ? OFFSET ?""",
            (task_id, safe_limit, safe_offset),
        ).fetchall()
        has_qwen = ReadonlyMediaRepository._table_exists(con, "stop03_3_qwenvl_results")
        has_ocr = ReadonlyMediaRepository._table_exists(con, "stop03_4_ocr_results")
        has_labels = ReadonlyMediaRepository._table_exists(con, "visual_labels")
        result_rows: list[dict[str, Any]] = []
        for row in rows:
            source_id = str(row["source_content_id"])
            visual = con.execute(
                """SELECT v.visual_unit_id,v.derived_id,
                          CASE WHEN v.time_position_ms >= 0 THEN v.time_position_ms
                               WHEN d.time_position_ms >= 0 THEN d.time_position_ms ELSE 0 END AS time_position_ms,
                          d.derived_path
                   FROM visual_units AS v
                   JOIN derived_assets AS d USING(derived_id)
                   WHERE v.source_content_id=?
                   ORDER BY COALESCE(v.near_black,0),v.time_position_ms,v.visual_unit_id
                   LIMIT 1""",
                (source_id,),
            ).fetchone()
            visual_id = str(visual["visual_unit_id"] if visual else "")
            position = max(0, int(visual["time_position_ms"] if visual else 0))
            description = ""
            if has_qwen:
                qwen = con.execute(
                    """SELECT clean_text FROM stop03_3_qwenvl_results
                       WHERE source_content_id=? AND result_status='success'
                       ORDER BY created_at DESC LIMIT 1""",
                    (source_id,),
                ).fetchone()
                description = str(qwen[0] or "") if qwen else ""
            ocr_text = ""
            if has_ocr:
                ocr = con.execute(
                    """SELECT ocr_text FROM stop03_4_ocr_results
                       WHERE source_content_id=? AND result_status='success'
                         AND TRIM(ocr_text)<>'' ORDER BY created_at DESC LIMIT 1""",
                    (source_id,),
                ).fetchone()
                ocr_text = str(ocr[0] or "") if ocr else ""
            labels: list[dict[str, Any]] = []
            if has_labels and visual_id:
                labels = [
                    {"label": str(label[0]), "label_zh": None, "confidence": float(label[1])}
                    for label in con.execute(
                        """SELECT label,MAX(confidence) FROM visual_labels
                           WHERE visual_unit_id=? GROUP BY label
                           ORDER BY MAX(confidence) DESC,label LIMIT 5""",
                        (visual_id,),
                    )
                ]
            try:
                tags = json.loads(str(row["tags_json"] or "[]"))
            except json.JSONDecodeError:
                tags = []
            source_path = Path(str(row["absolute_path"] or "")).expanduser()
            preview_path = Path(str(visual["derived_path"] or "")).expanduser() if visual else None
            half_window = 5_000
            note = str(row["note"] or "")
            text_parts = [value for value in (note, description, ocr_text) if value]
            result_rows.append({
                "result_id": f"favorite_{source_id}",
                "visual_unit_id": visual_id,
                "source_content_id": source_id,
                "source_frame_count": int(con.execute(
                    "SELECT COUNT(*) FROM visual_units WHERE source_content_id=?", (source_id,),
                ).fetchone()[0]),
                "result_level": "source",
                "derived_id": str(visual["derived_id"] if visual else ""),
                "source_relative_path": str(row["relative_path"] or ""),
                "media_type": str(row["media_type"] or ""),
                "time_position_ms": position,
                "timecode": _media_timecode(position),
                "preview_segment_start_ms": max(0, position - half_window),
                "preview_segment_start_timecode": _media_timecode(max(0, position - half_window)),
                "preview_segment_end_timecode": _media_timecode(position + half_window),
                "preview_path": str(preview_path) if preview_path and preview_path.is_file() else "",
                "source_path": str(source_path) if source_path.is_file() else "",
                "source_online": source_path.is_file(),
                "can_open_original": source_path.is_file(),
                "score": 1.0,
                "hybrid_score": None,
                "openclip_cosine": None,
                "text_semantic_score": None,
                "text_preview": "\n".join(text_parts) or "本地收藏素材",
                "environment_label": "我的收藏",
                "hit_reason": "user_favorite",
                "hit_field": "user_annotation",
                "relevance_reasons": ["user_favorite"],
                "matched_object_labels": labels,
                "matched_text_terms": [],
                "user_annotation": {
                    "tags": tags if isinstance(tags, list) else [],
                    "note": note, "favorite": True,
                    "rating": int(row["rating"] or 0),
                    "ignored": bool(row["ignored"]),
                    "updated_at": float(row["updated_at"] or 0),
                },
            })
    favorite_visual_ids = [str(row["visual_unit_id"]) for row in result_rows]
    cluster_links = _merge_person_links(
        repository.person_clusters_for_visual_units(favorite_visual_ids),
        _manual_person_links(
            getattr(repository, "db_path", None), favorite_visual_ids,
        ),
    )
    annotations = _load_person_annotation_payload(repository.db_path)[0]
    identities = dict(annotations.get("identities") or {})
    cluster_to_identity = dict(annotations.get("cluster_to_identity") or {})
    for row in result_rows:
        links = cluster_links.get(str(row["visual_unit_id"]), [])
        for link in links:
            identity = identities.get(str(cluster_to_identity.get(link["person_cluster_id"]) or ""), {})
            link["display_name"] = str(identity.get("display_name") or "").strip() or "同一匿名人物"
        row["person_clusters"] = links
    next_offset = safe_offset + len(result_rows)
    return {
        "status": "PASS", "query": "我的收藏",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "coverage": None, "result_count": len(result_rows),
        "result_items": result_rows, "result_total_count": total,
        "result_offset": safe_offset, "result_limit": safe_limit,
        "next_result_offset": next_offset if next_offset < total else None,
        "database_write": False, "model_run": False,
        "network_used": False, "original_media_read": False,
    }


def run_person_cluster_catalog(
    repository: ReadonlyMediaRepository,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Expose reliable anonymous-person groups as a query-only UI catalog."""
    requested_limit = max(1, min(int(limit), 200))
    page = repository.person_cluster_catalog(0, requested_limit)
    repository_db = getattr(repository, "db_path", None)
    annotations = _load_person_annotation_payload(Path(repository_db))[0] if repository_db else {
        "contract": "media_archive_local_person_annotations_v1", "identities": {},
        "cluster_to_identity": {}, "visual_memberships": {},
    }
    grouped = grouped_catalog(page["items"], annotations)
    identities = dict(annotations.get("identities") or {})
    visual_memberships = dict(annotations.get("visual_memberships") or {})
    grouped_by_id = {str(row["person_cluster_id"]): row for row in grouped}
    for identity_id, memberships in visual_memberships.items():
        clean_members = [dict(value or {}) for value in memberships or []]
        visual_ids = [str(value.get("visual_unit_id") or "") for value in clean_members]
        if not visual_ids:
            continue
        frames = repository.visual_frame_results(
            visual_unit_ids=visual_ids, offset=0, limit=5_000,
        )["items"]
        if not frames:
            continue
        annotation = dict(identities.get(identity_id) or {})
        existing = grouped_by_id.get(str(identity_id))
        if existing is not None:
            existing["member_count"] = max(
                int(existing.get("member_count") or 0), len(frames),
            )
            existing["distinct_source_count"] = max(
                int(existing.get("distinct_source_count") or 0),
                len({str(row.get("source_content_id") or "") for row in frames}),
            )
            existing["is_local_identity"] = True
            continue
        first = frames[0]
        item = {
            **first,
            "person_cluster_id": str(identity_id),
            "display_name": str(annotation.get("display_name") or ""),
            "tags": list(annotation.get("tags") or []),
            "member_count": len(frames),
            "distinct_source_count": len({
                str(row.get("source_content_id") or "") for row in frames
            }),
            "cluster_confidence": "human_confirmed",
            "human_review_status": "confirmed",
            "merged_cluster_count": 0,
            "is_local_identity": True,
        }
        grouped.append(item)
        grouped_by_id[str(identity_id)] = item
    grouped.sort(key=lambda row: (
        -int(row.get("member_count") or 0), str(row.get("person_cluster_id") or "")
    ))
    page = {
        "total": len(grouped), "offset": max(0, int(offset)),
        "limit": requested_limit,
        "items": grouped[max(0, int(offset)):max(0, int(offset)) + requested_limit],
    }
    items = []
    for index, row in enumerate(page["items"], start=int(page["offset"]) + 1):
        stored_name = str(row.get("display_name") or row.get("anonymous_display_name") or "").strip()
        items.append({
            "person_cluster_id": str(row["person_cluster_id"]),
            "display_name": stored_name or f"匿名人物 {index:02d}",
            "member_count": int(row.get("member_count") or 0),
            "distinct_source_count": int(row.get("distinct_source_count") or 0),
            "cluster_confidence": str(row.get("cluster_confidence") or ""),
            "human_review_status": str(row.get("human_review_status") or ""),
            "preview_path": str(row.get("preview_path") or ""),
            "media_type": str(row.get("media_type") or ""),
            "time_position_ms": int(row.get("time_position_ms") or 0),
            "tags": list(row.get("tags") or []),
            "merged_cluster_count": int(row.get("merged_cluster_count") or 1),
            "is_local_identity": bool(row.get("is_local_identity")),
        })
    return {
        "status": "PASS",
        "total": int(page["total"]),
        "offset": int(page["offset"]),
        "limit": int(page["limit"]),
        "items": items,
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "original_media_read": False,
        "capability_note": (
            "机器分组只依据可见人脸；你可以把背影、遮挡或漏检画面人工加入自建人物，"
            "人工结果独立保存且不会改写机器识别。"
        ),
    }


def _validated_visual_membership(
    repository: ReadonlyMediaRepository, visual_unit_id: str, source_content_id: str,
) -> tuple[str, str]:
    visual_id = str(visual_unit_id or "").strip()
    source_id = str(source_content_id or "").strip()
    if not visual_id or not source_id:
        raise ValueError("请选择一个有效画面")
    page = repository.visual_frame_results(
        visual_unit_ids=[visual_id], offset=0, limit=1,
    )
    if not page["items"] or str(page["items"][0].get("source_content_id") or "") != source_id:
        raise ValueError("画面与素材来源不匹配")
    return visual_id, source_id


def update_person_create(
    repository: ReadonlyMediaRepository, display_name: str, tags: str,
    visual_unit_id: str, source_content_id: str,
) -> dict[str, Any]:
    name = " ".join(str(display_name or "").split())[:120]
    if not name:
        raise ValueError("请填写新人物名称")
    visual_id, source_id = _validated_visual_membership(
        repository, visual_unit_id, source_content_id,
    )
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identity_id = create_identity(
        payload, name,
        [value for value in re.split(r"[,，;；]", tags) if value.strip()],
    )
    add_visual_membership(payload, identity_id, visual_id, source_id)
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    return {
        "status": "PASS", "message": f"已新建人物“{name}”并加入当前画面",
        "person_identity_id": identity_id, "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def update_person_add_visual(
    repository: ReadonlyMediaRepository, identifier: str,
    visual_unit_id: str, source_content_id: str,
) -> dict[str, Any]:
    visual_id, source_id = _validated_visual_membership(
        repository, visual_unit_id, source_content_id,
    )
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identity_id = add_visual_membership(payload, identifier, visual_id, source_id)
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    name = str(dict(payload.get("identities") or {}).get(identity_id, {}).get("display_name") or "自建人物")
    return {
        "status": "PASS", "message": f"当前画面已加入“{name}”",
        "person_identity_id": identity_id, "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def update_person_remove_visual(
    repository: ReadonlyMediaRepository, identifier: str, visual_unit_id: str,
) -> dict[str, Any]:
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identity_id = remove_visual_membership(payload, identifier, str(visual_unit_id or "").strip())
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    return {
        "status": "PASS", "message": "已移除当前画面的人工人物关联；机器结果未修改",
        "person_identity_id": identity_id, "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def update_person_name(
    repository: ReadonlyMediaRepository, identifier: str, display_name: str, tags: str,
) -> dict[str, Any]:
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identity_id = name_identity(
        payload, identifier, display_name,
        [value for value in re.split(r"[,，;；]", tags) if value.strip()],
    )
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    return {
        "status": "PASS", "message": "人物名称与标签已保存在本机",
        "person_identity_id": identity_id, "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def update_person_merge(
    repository: ReadonlyMediaRepository, source: str, target: str,
) -> dict[str, Any]:
    if source == target:
        raise ValueError("请选择另一个人物组作为合并目标")
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identity_id = merge_identity(payload, source, target)
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    return {
        "status": "PASS", "message": "已归入同一个本地人物；机器识别原记录未修改",
        "person_identity_id": identity_id, "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def update_person_detach(
    repository: ReadonlyMediaRepository, cluster_id: str,
) -> dict[str, Any]:
    payload, task_id = _load_writable_person_annotation_payload(repository.db_path)
    identities = dict(payload.get("identities") or {})
    mapped_identity = (
        cluster_id if cluster_id in identities
        else str(dict(payload.get("cluster_to_identity") or {}).get(cluster_id) or "")
    )
    retained_visuals = list(
        dict(payload.get("visual_memberships") or {}).get(mapped_identity, [])
    )
    clusters = resolve_clusters(payload, cluster_id)
    detached = [detach_cluster(payload, cluster_id) for cluster_id in clusters]
    if retained_visuals and detached:
        payload.setdefault("visual_memberships", {})[detached[0]] = retained_visuals
        if mapped_identity != detached[0]:
            payload["visual_memberships"].pop(mapped_identity, None)
    _save_person_annotation_payload(repository.db_path, task_id, payload)
    return {
        "status": "PASS", "message": f"已拆分为 {len(detached)} 个独立本地人物；机器识别原记录未修改",
        "person_identity_id": detached[0] if detached else "", "annotation_path": str(repository.db_path),
        "database_write": True, "local_metadata_write": True,
    }


def run_search(
    config: dict[str, Any],
    repository: ReadonlyMediaRepository,
    manager: SearchJobManager,
    query: str,
    media_type: str,
    preview_window_ms: int,
    result_offset: int = 0,
    result_limit: int = 30,
    path_prefix: str = "",
    source_mtime_min: int | None = None,
    source_mtime_max: int | None = None,
    has_ocr: bool = False,
    has_person: bool = False,
) -> dict[str, Any]:
    clean_query = " ".join(query.split())
    if not (1 <= len(clean_query) <= 512):
        raise ValueError("搜索文字长度必须在 1 到 512 个字符之间")
    readiness = manager.readiness()
    if not readiness["ready"]:
        preflight = dict(readiness.get("database_preflight") or {})
        database_error = str(preflight.get("database_error") or "")
        if database_error:
            raise RuntimeError(
                "搜索数据库不可用："
                f"{database_error}（{preflight.get('database_path', '')}）"
            )
        missing = [name for name, passed in dict(readiness["checks"]).items() if not passed]
        raise RuntimeError("搜索运行环境未通过预检：" + "、".join(missing))

    job_id = "native5e_" + uuid.uuid4().hex[:20]
    # Search is a read-only operation from the library's point of view.  Its
    # three bounded, replace-in-place diagnostics belong to application state,
    # not inside the user-selected index/library directory.
    shared_cache_root = search_runtime_cache_root(config)
    shared_cache_root.mkdir(parents=True, exist_ok=True)
    current_root = shared_cache_root / "current"
    current_root.mkdir(parents=True, exist_ok=True)
    stable_result_path = current_root / "search_results.json"
    stable_summary_path = current_root / "search_summary.json"
    stable_log_path = current_root / "search.log"
    request = {
        "media_type": media_type,
        "preview_window_ms": preview_window_ms,
        "temporal_dedup_ms": 5000,
        "offset": max(0, int(result_offset)),
        "limit": max(1, min(int(result_limit), 200)),
        "device": "auto",
        "path_prefix": path_prefix.strip(),
        "source_mtime_min": source_mtime_min,
        "source_mtime_max": source_mtime_max,
        "has_ocr": bool(has_ocr),
        "has_person": bool(has_person),
    }
    environment = _search_environment(shared_cache_root)
    disk_before = _search_file_snapshot(shared_cache_root)
    started = time.monotonic()
    payload: dict[str, Any] | None = None
    summary_payload: dict[str, Any] = {}
    return_code = -1
    candidate_count = 0
    contract_error = ""
    transient_log_text = ""
    with tempfile.TemporaryDirectory(prefix="material-organizer-search-") as temp:
        job_root = Path(temp)
        output_root = job_root / "output"
        command = manager.build_command(clean_query, request, output_root)
        log_path = job_root / "search.log"
        return_code = _run_streaming_search_command(command, log_path, environment)
        candidates = sorted(output_root.glob("query5ev2_*/reports/search_results.json"))
        summaries = sorted(output_root.glob("query5ev2_*/reports/search_summary.json"))
        candidate_count = len(candidates)
        transient_log_text = _bounded_log_text(log_path)
        if len(summaries) == 1:
            try:
                summary_payload = json.loads(summaries[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                summary_payload = {}
        if len(candidates) == 1:
            try:
                if candidates[0].stat().st_size > SEARCH_RESULT_MAX_BYTES:
                    raise ValueError("search_result_contract_file_too_large")
                payload = json.loads(candidates[0].read_text(encoding="utf-8"))
                _validate_search_result_contract(payload, clean_query)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                contract_error = str(exc)

    if return_code in {
        -signal.SIGINT,
        -signal.SIGTERM,
        128 + signal.SIGINT,
        128 + signal.SIGTERM,
    }:
        # A user cancellation is not a failed search contract.  Keep the last
        # successful bounded result, summary, and log mutually consistent.
        return {
            "status": "CANCELLED",
            "error": "SEARCH_CANCELLED",
            "error_name": "SEARCH_CANCELLED",
            "error_reason": "用户取消了本次搜索",
            "exit_code": return_code,
            "database_write": False,
            "network_used": False,
            "original_media_read": False,
        }

    stable_log_path.write_text(transient_log_text, encoding="utf-8")
    if return_code != 0 or candidate_count != 1 or payload is None or contract_error:
        current_result_written = False
        if payload is not None and not contract_error:
            _atomic_search_json(stable_result_path, payload)
            current_result_written = True
        error_name = (
            "SEARCH_CONTRACT_VALIDATION_FAILED"
            if return_code == 2 or contract_error or candidate_count != 1
            else "SEARCH_RUNTIME_ERROR"
        )
        reason_parts = [f"搜索退出码 {return_code}", f"正式结果文件数量 {candidate_count}"]
        if contract_error:
            reason_parts.append(contract_error)
        diagnostic = _bounded_log_text(stable_log_path, 4000)
        error_report = {
            "status": "FAIL",
            "error": error_name,
            "error_name": error_name,
            "error_reason": "；".join(reason_parts),
            "log_path": str(stable_log_path),
            "result_path": str(stable_result_path) if current_result_written else "",
            "diagnostic": diagnostic,
            "exit_code": return_code,
        }
        _atomic_search_json(stable_summary_path, {
            **summary_payload,
            **error_report,
            "disk_audit": _search_disk_audit(
                disk_before, _search_file_snapshot(shared_cache_root)
            ),
        })
        return error_report

    assert payload is not None
    result_rows = list(payload.get("result_items", []))
    cluster_links = (
        repository.person_clusters_for_visual_units(
            str(row.get("visual_unit_id") or "") for row in result_rows
        )
        if hasattr(repository, "person_clusters_for_visual_units")
        else {}
    )
    result_visual_ids = [str(row.get("visual_unit_id") or "") for row in result_rows]
    cluster_links = _merge_person_links(
        cluster_links,
        _manual_person_links(
            getattr(repository, "db_path", None), result_visual_ids,
        ),
    )
    public_results = []
    for row in result_rows:
        derived_path = repository.derived_path(str(row.get("derived_id") or ""))
        source = repository.source_media(str(row.get("source_content_id") or ""))
        item = dict(row)
        item.update({
            "preview_path": str(derived_path) if derived_path else "",
            "source_path": str(source.get("resolved_path")) if source and source.get("available") else "",
            "source_online": bool(source and source.get("available")),
            "can_open_original": bool(source and source.get("available")),
            "person_clusters": cluster_links.get(
                str(row.get("visual_unit_id") or ""), [],
            ),
        })
        public_results.append(item)
    annotations: dict[str, dict[str, Any]] = {}
    database_path = getattr(repository, "db_path", None)
    if database_path:
        try:
            annotation_task_id = task_id_for_database(Path(database_path))
            annotations = asset_annotations(
                Path(database_path), task_id=annotation_task_id,
                source_content_ids=[
                    str(row.get("source_content_id") or "") for row in public_results
                ],
            )
        except (OSError, RuntimeError, sqlite3.Error):
            annotations = {}
    for item in public_results:
        item["user_annotation"] = annotations.get(
            str(item.get("source_content_id") or ""),
            {"tags": [], "note": "", "favorite": False, "rating": 0, "ignored": False},
        )
    final_payload = {
        **payload,
        "result_count": len(public_results),
        "result_items": public_results,
    }
    _atomic_search_json(stable_result_path, final_payload)
    disk_after = _search_file_snapshot(shared_cache_root)
    _atomic_search_json(stable_summary_path, {
        **summary_payload,
        "status": "PASS",
        "formal_result_path": str(stable_result_path),
        "bounded_log_path": str(stable_log_path),
        "temporary_query_directory_retained": False,
        "preview_files_copied": 0,
        "storage_policy": "bounded_replace_three_files_v1",
        "files_replaced_not_appended": True,
        "cumulative_search_growth_expected": False,
        "retained_file_count": 3,
        "result_max_bytes": SEARCH_RESULT_MAX_BYTES,
        "log_max_bytes": SEARCH_LOG_MAX_BYTES,
        "disk_audit": _search_disk_audit(disk_before, disk_after),
    })
    elapsed_seconds = round(time.monotonic() - started, 3)
    history_recorded = False
    database_path = getattr(repository, "db_path", None)
    if database_path:
        try:
            history_task_id = task_id_for_database(Path(database_path))
            record_search_history(
                Path(database_path),
                task_id=history_task_id,
                query_text=clean_query,
                filters={
                    "media_type": media_type, "preview_window_ms": preview_window_ms,
                    "path_prefix": path_prefix.strip(),
                    "source_mtime_min": source_mtime_min,
                    "source_mtime_max": source_mtime_max,
                    "has_ocr": bool(has_ocr), "has_person": bool(has_person),
                },
                result_count=int(payload.get("result_total_count") or len(public_results)),
                elapsed_seconds=elapsed_seconds,
            )
            history_recorded = True
        except (OSError, RuntimeError, sqlite3.Error):
            pass
    return {
        "status": "PASS",
        "job_id": job_id,
        "query": clean_query,
        "elapsed_seconds": elapsed_seconds,
        "coverage": {
            "eligible_visual_unit_count": summary_payload.get(
                "eligible_visual_unit_count",
                payload.get("eligible_visual_unit_count", 0),
            ),
            "scanned_visual_vector_count": summary_payload.get(
                "scanned_visual_vector_count",
                payload.get("scanned_visual_vector_count", 0),
            ),
            "scanned_text_vector_count": summary_payload.get(
                "scanned_text_vector_count",
                payload.get("scanned_text_vector_count", 0),
            ),
        },
        "result_count": len(public_results),
        "result_items": public_results,
        "result_total_count": int(payload.get("result_total_count") or 0),
        "result_offset": int(payload.get("result_offset") or 0),
        "result_limit": int(payload.get("result_limit") or request["limit"]),
        "next_result_offset": payload.get("next_result_offset"),
        "result_count_by_media": payload.get("result_count_by_media") or {},
        "result_path": str(stable_result_path),
        "summary_path": str(stable_summary_path),
        "log_path": str(stable_log_path),
        "database_write": history_recorded,
        "search_results_read_only": True,
        "search_history_recorded": history_recorded,
        "network_used": False,
        "original_media_read": False,
    }


def run_search_prewarm(
    config: dict[str, Any],
    manager: SearchJobManager,
) -> dict[str, Any]:
    """Warm query encoders in background-capable workers without using a query."""
    readiness = manager.readiness()
    if not readiness.get("ready"):
        raise RuntimeError("搜索运行环境不完整")
    shared_cache_root = search_runtime_cache_root(config)
    shared_cache_root.mkdir(parents=True, exist_ok=True)
    environment = _search_environment(shared_cache_root)
    command = [
        str(manager.embedding_python),
        str(manager.search_script),
        "--mode",
        "warmup",
        "--db",
        str(manager.db_path),
        "--config",
        str(manager.search_config),
        "--out",
        str(shared_cache_root / "current"),
        "--openclip-python",
        str(manager.openclip_python),
        "--device",
        "auto",
    ]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="material-organizer-prewarm-") as temp:
        log_path = Path(temp) / "prewarm.log"
        return_code = _run_streaming_search_command(command, log_path, environment)
        diagnostic = _bounded_log_text(log_path, 4000)
    return {
        "status": "PASS" if return_code == 0 else "FAIL",
        "message": (
            "搜索模型已预热，下一次搜索会更快"
            if return_code == 0
            else "搜索模型预热未完成；仍可使用兼容搜索路径"
        ),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_code": return_code,
        "diagnostic": "" if return_code == 0 else diagnostic,
        "database_write": False,
        "query_text_used": False,
        "query_vector_persisted": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }


def search_metadata(repository: ReadonlyMediaRepository) -> dict[str, Any]:
    """Return per-library search history and saved queries without source reads."""
    task_id = task_id_for_database(repository.db_path)
    return {
        "status": "PASS",
        "task_id": task_id,
        "history": list_search_history(repository.db_path, task_id, limit=30),
        "saved_searches": list_saved_searches(repository.db_path, task_id),
        "database_write": False,
        "original_media_read": False,
        "model_run": False,
    }


def save_search_metadata(
    repository: ReadonlyMediaRepository,
    *,
    display_name: str,
    query_text: str,
    media_type: str,
    preview_window_ms: int,
    path_prefix: str = "",
    source_mtime_min: int | None = None,
    source_mtime_max: int | None = None,
    has_ocr: bool = False,
    has_person: bool = False,
) -> dict[str, Any]:
    task_id = task_id_for_database(repository.db_path)
    saved_search_id = save_central_search(
        repository.db_path,
        task_id=task_id,
        display_name=display_name,
        query_text=query_text,
        filters={
            "media_type": media_type, "preview_window_ms": preview_window_ms,
            "path_prefix": path_prefix.strip(),
            "source_mtime_min": source_mtime_min,
            "source_mtime_max": source_mtime_max,
            "has_ocr": bool(has_ocr), "has_person": bool(has_person),
        },
    )
    return {
        "status": "PASS",
        "message": "搜索条件已保存在当前素材库",
        "saved_search_id": saved_search_id,
        "database_write": True,
        "original_media_read": False,
        "model_run": False,
    }


def save_asset_annotation(
    repository: ReadonlyMediaRepository,
    *,
    source_content_id: str,
    tags: str,
    note: str,
    favorite: bool,
    rating: int,
    ignored: bool,
) -> dict[str, Any]:
    task_id = task_id_for_database(repository.db_path)
    annotation_id = upsert_asset_annotation(
        repository.db_path,
        task_id=task_id,
        source_content_id=source_content_id,
        tags=[value for value in re.split(r"[,，]", tags) if value.strip()],
        note=note,
        favorite=favorite,
        rating=rating,
        ignored=ignored,
    )
    return {
        "status": "PASS",
        "message": "素材标签与备注已保存在当前素材库；模型结果未修改",
        "identifier": annotation_id,
        "database_write": True,
        "original_media_read": False,
        "model_run": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native macOS UI bridge")
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot")
    activate = subparsers.add_parser("activate-library")
    activate.add_argument("--task", type=Path, required=True)
    subparsers.add_parser("search-prewarm")
    subparsers.add_parser("search-metadata")
    saved_search = subparsers.add_parser("save-search")
    saved_search.add_argument("--name", required=True)
    saved_search.add_argument("--query", required=True)
    saved_search.add_argument("--media-type", choices=("all", "image", "video", "audio"), default="all")
    saved_search.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    saved_search.add_argument("--path-prefix", default="")
    saved_search.add_argument("--source-mtime-min", type=int)
    saved_search.add_argument("--source-mtime-max", type=int)
    saved_search.add_argument("--has-ocr", action="store_true")
    saved_search.add_argument("--has-person", action="store_true")
    annotation = subparsers.add_parser("annotate-source")
    annotation.add_argument("--source-content-id", required=True)
    annotation.add_argument("--tags", default="")
    annotation.add_argument("--note", default="")
    annotation.add_argument("--favorite", choices=("true", "false"), default="false")
    annotation.add_argument("--rating", type=int, choices=range(0, 6), default=0)
    annotation.add_argument("--ignored", choices=("true", "false"), default="false")
    history = subparsers.add_parser("task-detail")
    history.add_argument("--task", type=Path, required=True)
    storage = subparsers.add_parser("storage-audit")
    storage.add_argument("--task", type=Path, required=True)
    storage.add_argument("--largest-limit", type=int, choices=range(1, 201), default=30)
    cleanup_plan = subparsers.add_parser("storage-cleanup-plan")
    cleanup_plan.add_argument("--task", type=Path, required=True)
    cleanup_plan.add_argument("--include-resume-affecting", action="store_true")
    cleanup_apply = subparsers.add_parser("storage-cleanup-apply")
    cleanup_apply.add_argument("--task", type=Path, required=True)
    cleanup_apply.add_argument("--plan-id", required=True)
    cleanup_apply.add_argument("--confirmation-phrase", required=True)
    comparison = subparsers.add_parser("compare-tasks")
    comparison.add_argument("--left-task", type=Path, required=True)
    comparison.add_argument("--right-task", type=Path, required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--media-type", choices=("all", "image", "video", "audio"), default="all")
    search.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    search.add_argument("--result-offset", type=int, default=0)
    search.add_argument("--result-limit", type=int, choices=range(1, 201), default=30)
    search.add_argument("--path-prefix", default="")
    search.add_argument("--source-mtime-min", type=int)
    search.add_argument("--source-mtime-max", type=int)
    search.add_argument("--has-ocr", action="store_true")
    search.add_argument("--has-person", action="store_true")
    favorites = subparsers.add_parser("favorites")
    favorites.add_argument("--result-offset", type=int, default=0)
    favorites.add_argument("--result-limit", type=int, choices=range(1, 501), default=200)
    source_frames = subparsers.add_parser("source-frames")
    source_frames.add_argument("--source-content-id", required=True)
    source_frames.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    source_frames.add_argument("--result-offset", type=int, default=0)
    source_frames.add_argument("--result-limit", type=int, choices=range(1, 201), default=60)
    person = subparsers.add_parser("person-cluster")
    person.add_argument("--cluster-id", required=True)
    person.add_argument("--media-type", choices=("all", "image", "video"), default="all")
    person.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    person.add_argument("--result-offset", type=int, default=0)
    person.add_argument("--result-limit", type=int, choices=range(1, 101), default=30)
    person.add_argument("--source-content-id", default="")
    person_tracks = subparsers.add_parser("person-track-suggestions")
    person_tracks.add_argument("--cluster-id", required=True)
    person_tracks.add_argument("--media-type", choices=("all", "image", "video"), default="all")
    person_tracks.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    person_tracks.add_argument("--result-offset", type=int, default=0)
    person_tracks.add_argument("--result-limit", type=int, choices=range(1, 101), default=30)
    people = subparsers.add_parser("person-clusters")
    people.add_argument("--result-offset", type=int, default=0)
    people.add_argument("--result-limit", type=int, choices=range(1, 201), default=100)
    person_name = subparsers.add_parser("person-name")
    person_name.add_argument("--person-id", required=True)
    person_name.add_argument("--display-name", required=True)
    person_name.add_argument("--tags", default="")
    person_merge = subparsers.add_parser("person-merge")
    person_merge.add_argument("--source-person-id", required=True)
    person_merge.add_argument("--target-person-id", required=True)
    person_detach = subparsers.add_parser("person-detach")
    person_detach.add_argument("--person-id", required=True)
    person_create = subparsers.add_parser("person-create")
    person_create.add_argument("--display-name", required=True)
    person_create.add_argument("--tags", default="")
    person_create.add_argument("--visual-unit-id", required=True)
    person_create.add_argument("--source-content-id", required=True)
    person_add_visual = subparsers.add_parser("person-add-visual")
    person_add_visual.add_argument("--person-id", required=True)
    person_add_visual.add_argument("--visual-unit-id", required=True)
    person_add_visual.add_argument("--source-content-id", required=True)
    person_remove_visual = subparsers.add_parser("person-remove-visual")
    person_remove_visual.add_argument("--person-id", required=True)
    person_remove_visual.add_argument("--visual-unit-id", required=True)
    profile = subparsers.add_parser("save-profile")
    profile.add_argument("--scheduler-mode", choices=("auto", "pipeline_async", "stage_serial"), required=True)
    profile.add_argument("--model-workers", type=int, required=True)
    profile.add_argument("--frame-extract-workers", type=int, required=True)
    profile.add_argument("--frame-interval-seconds", type=float, required=True)
    profile.add_argument("--high-value-mode", choices=("frozen_v25_compatible", "target_15", "target_20", "target_30"), required=True)
    profile.add_argument("--image-scope", choices=("frozen_current_policy", "all_images"), required=True)
    profile.add_argument("--yoloe-a-keywords", required=True)
    profile.add_argument("--yoloe-b-keywords", required=True)
    profile.add_argument("--yoloe-enable-b-extended", choices=("true", "false"), required=True)
    models = subparsers.add_parser("save-model-root")
    models.add_argument("--path", type=Path, required=True)
    task = subparsers.add_parser("save-task")
    task.add_argument("--source", required=True)
    task.add_argument("--name", required=True)
    task.add_argument("--task-mode", choices=("full", "incremental", "repair", "repair_images", "rebuild_search", "audio_enrichment"), required=True)
    task.add_argument("--workspace-root")
    start = subparsers.add_parser("start-task")
    start.add_argument("--source", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--task-mode", choices=("full", "incremental", "repair", "repair_images", "rebuild_search", "audio_enrichment"), required=True)
    start.add_argument("--workspace-root", required=True)
    existing = subparsers.add_parser("start-existing-task")
    existing.add_argument("--task", required=True)
    existing.add_argument("--task-mode", choices=("incremental", "repair", "repair_images", "rebuild_search", "audio_enrichment"), required=True)
    subparsers.add_parser("resume-task")
    subparsers.add_parser("stop-task")
    worker = subparsers.add_parser("pipeline-worker")
    worker.add_argument("--task", type=Path, required=True)
    worker.add_argument("--resume", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config, repository, manager = load_runtime(args.config)
    try:
        if args.command == "snapshot":
            report = snapshot(config, repository, manager)
        elif args.command == "activate-library":
            report = activate_library(config, args.task)
        elif args.command == "search-prewarm":
            if manager is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_search_prewarm(config, manager)
        elif args.command == "search-metadata":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = search_metadata(repository)
        elif args.command == "save-search":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = save_search_metadata(
                repository,
                display_name=args.name,
                query_text=args.query,
                media_type=args.media_type,
                preview_window_ms=args.preview_window_ms,
                path_prefix=args.path_prefix,
                source_mtime_min=args.source_mtime_min,
                source_mtime_max=args.source_mtime_max,
                has_ocr=args.has_ocr,
                has_person=args.has_person,
            )
        elif args.command == "annotate-source":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = save_asset_annotation(
                repository,
                source_content_id=args.source_content_id,
                tags=args.tags,
                note=args.note,
                favorite=args.favorite == "true",
                rating=args.rating,
                ignored=args.ignored == "true",
            )
        elif args.command == "task-detail":
            report = task_detail(args.task)
        elif args.command == "storage-audit":
            report = audit_task_storage(args.task, largest_limit=args.largest_limit)
        elif args.command == "storage-cleanup-plan":
            report = build_cleanup_plan(
                args.task,
                include_resume_affecting=args.include_resume_affecting,
            )
        elif args.command == "storage-cleanup-apply":
            plan = build_cleanup_plan(args.task)
            if str(plan.get("plan_id") or "") != args.plan_id:
                raise RuntimeError("存储内容已变化，请重新生成并核对清理计划")
            report = apply_cleanup_plan(
                args.task,
                plan,
                confirmation_phrase=args.confirmation_phrase,
            )
        elif args.command == "compare-tasks":
            report = compare_task_storage(args.left_task, args.right_task)
        elif args.command == "search":
            if repository is None or manager is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_search(
                config, repository, manager, args.query, args.media_type, args.preview_window_ms,
                args.result_offset, args.result_limit, args.path_prefix,
                args.source_mtime_min, args.source_mtime_max,
                args.has_ocr, args.has_person,
            )
        elif args.command == "favorites":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_favorite_collection(
                repository, args.result_offset, args.result_limit,
            )
        elif args.command == "source-frames":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_source_frame_search(
                repository, args.source_content_id, args.preview_window_ms,
                args.result_offset, args.result_limit,
            )
        elif args.command == "person-cluster":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_person_cluster_search(
                repository, args.cluster_id, args.media_type,
                args.preview_window_ms, args.result_offset, args.result_limit,
                args.source_content_id or None,
            )
        elif args.command == "person-track-suggestions":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_person_track_suggestions(
                repository, args.cluster_id, args.media_type,
                args.preview_window_ms, args.result_offset, args.result_limit,
            )
        elif args.command == "person-clusters":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_person_cluster_catalog(
                repository, args.result_offset, args.result_limit,
            )
        elif args.command == "person-name":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_name(
                repository, args.person_id, args.display_name, args.tags,
            )
        elif args.command == "person-merge":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_merge(
                repository, args.source_person_id, args.target_person_id,
            )
        elif args.command == "person-detach":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_detach(repository, args.person_id)
        elif args.command == "person-create":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_create(
                repository, args.display_name, args.tags,
                args.visual_unit_id, args.source_content_id,
            )
        elif args.command == "person-add-visual":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_add_visual(
                repository, args.person_id,
                args.visual_unit_id, args.source_content_id,
            )
        elif args.command == "person-remove-visual":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = update_person_remove_visual(
                repository, args.person_id, args.visual_unit_id,
            )
        elif args.command == "save-profile":
            report = save_profile(config, args)
        elif args.command == "save-model-root":
            report = save_model_root(args.path)
        elif args.command == "save-task":
            report = save_task_draft(config, args)
        elif args.command == "start-task":
            report = start_task(config, args.config, args)
        elif args.command == "start-existing-task":
            report = start_existing_task(config, args.config, args)
        elif args.command == "resume-task":
            report = resume_active_task(config, args.config)
        elif args.command == "stop-task":
            report = stop_active_task(config)
        else:
            state = execute_pipeline(args.task, resume=args.resume)
            report = {
                "status": "PASS" if state.get("status") == "success" else "FAIL",
                "message": state.get("error") or "完整整理已完成",
                "identifier": state.get("task_id"),
            }
    except Exception as exc:
        if getattr(args, "command", None) == "pipeline-worker" and getattr(args, "task", None):
            try:
                task = load_json(args.task)
                state_path = Path(task["state_path"])
                state = load_json(state_path) if state_path.is_file() else {}
                now = time.time()
                state.update({
                    "status": "failed", "current_child_pid": None,
                    "updated_at_epoch": now, "finished_at_epoch": now,
                    "error": str(exc), "reason_code": "PIPELINE_WORKER_UNHANDLED_EXCEPTION",
                })
                for row in state.get("stages", []):
                    if row.get("status") == "running":
                        row.update({"status": "failed", "finished_at_epoch": now, "reason_code": "PIPELINE_WORKER_UNHANDLED_EXCEPTION"})
                atomic_json(state_path, state)
            except Exception:
                pass
        report = {"status": "FAIL", "error": str(exc)}
        print(json.dumps(report, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("status") in {"PASS", "FIRST_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
