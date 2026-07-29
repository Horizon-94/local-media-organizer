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
from typing import Any, Optional, Sequence

from .pipeline_orchestrator import (
    atomic_json,
    build_stage_plan,
    execute_pipeline,
    load_json,
    stop_pipeline,
    validate_stage_acceptance,
)
from .processing_profile import build_processing_profile, detect_hardware, save_processing_profile
from .repository import MODEL_SECONDS_PER_ITEM_PER_WORKER, ReadonlyMediaRepository
from .runtime_contract import (
    default_model_root,
    load_runtime_contract,
    task_runtime_from_contract,
    validate_runtime_contract,
)
from .search_jobs import SearchJobManager, offline_search_environment


APP_NAME = "本地数据库"
APP_VERSION = "1.1.4-search-progress-warm-cache"


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


def _existing_library_record(task_path: Path) -> dict[str, Any] | None:
    try:
        task = load_json(task_path.expanduser().resolve(strict=True))
        database = Path(str(task["database"])).expanduser().resolve(strict=True)
        state_path = Path(str(task.get("state_path") or "")).expanduser()
        state = load_json(state_path) if state_path.is_file() else {}
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as con:
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
    """Return human-readable known libraries without scanning arbitrary folders."""
    candidates: list[Path] = []
    current = str(config.get("library_task_path") or config.get("task_path") or "").strip()
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
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        row = _existing_library_record(candidate)
        if row is None or row["database"] in seen:
            continue
        seen.add(row["database"])
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
        "visual_schema_v3": "准备视觉分析数据库",
        "yoloe": "物体识别（YOLOE）",
        "openclip": "全量视觉向量（OpenCLIP）",
        "dedup": "来源与画面去重",
        "person_reid_optional_v1": "同一人物归并（InsightFace，本地匿名）",
        "candidate_schema": "准备候选数据库",
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
    }
    rows = []
    for stage in state.get("stages", []):
        key = str(stage.get("key") or "stage")
        status = "failed" if key in acceptance_errors else str(stage.get("status") or "pending")
        metric = metrics.get(key, {})
        done = int(metric.get("done") or 0)
        total_items = int(metric.get("total") or 0)
        detail = str(metric.get("description") or "").strip()
        duration = (
            f"用时 {human_duration(stage.get('elapsed_seconds'))}"
            if stage.get("elapsed_seconds") is not None else ""
        )
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
    """Estimate remaining model time from this task's live counts, never fixed totals."""
    task = task or {}
    profile_scheduler = dict((task.get("profile") or {}).get("scheduler") or {})
    runtime = dict(task.get("runtime") or {})
    model_workers = max(1, int(profile_scheduler.get("model_workers") or 1))
    ocr_workers = max(1, int(runtime.get("ocr_workers") or model_workers))
    embedding_workers = max(1, int(runtime.get("embedding_workers") or model_workers))
    person_reid_workers = max(
        1, int(runtime.get("person_reid_workers") or min(8, model_workers * 3)),
    )
    statuses = {str(row.get("key") or ""): str(row.get("status") or "pending") for row in state.get("stages", [])}

    def remaining(stage_key: str, metric_key: str) -> int:
        if statuses.get(stage_key) == "success":
            return 0
        metric = metrics.get(metric_key) or {}
        return max(0, int(metric.get("total") or 0) - int(metric.get("done") or 0))

    qwen_remaining = remaining("qwen_optional_v2", "qwen_optional_v2")
    qwen_remaining += remaining("all_image_supplement_qwen", "all_image_supplement_qwen")
    qwen_remaining += remaining("repair_supplement_qwen", "repair_supplement_qwen")
    ocr_remaining = remaining("ocr_optional_v2", "ocr_optional_v2")
    person_reid_remaining = remaining(
        "person_reid_optional_v1", "person_reid_optional_v1",
    )
    embedding_remaining = remaining("embedding_optional_v2", "embedding_optional_v2")
    embedding_remaining += remaining("repair_embedding", "repair_embedding")
    if embedding_remaining == 0 and any(
        statuses.get(key) not in (None, "success")
        for key in ("embedding_optional_v2", "repair_embedding")
    ):
        # The final number of distinct texts is unknown before evidence merge.
        # Use this task's current candidate population as a transparent upper estimate.
        embedding_remaining = max(
            int((metrics.get("qwen_optional_v2") or {}).get("total") or 0),
            int((metrics.get("ocr_optional_v2") or {}).get("total") or 0),
            int((metrics.get("repair_supplement_qwen") or {}).get("total") or 0),
            int((metrics.get("all_image_supplement_qwen") or {}).get("total") or 0),
        )
    seconds = (
        person_reid_remaining
        * MODEL_SECONDS_PER_ITEM_PER_WORKER["person_reid"]
        / person_reid_workers
        + qwen_remaining * MODEL_SECONDS_PER_ITEM_PER_WORKER["qwen_vl"] / model_workers
        + ocr_remaining * MODEL_SECONDS_PER_ITEM_PER_WORKER["ocr"] / ocr_workers
        + embedding_remaining * MODEL_SECONDS_PER_ITEM_PER_WORKER["embedding"] / embedding_workers
    )
    if seconds <= 0:
        return {
            "overall_eta_seconds": None,
            "overall_eta_basis": "等待产生可计数的模型任务",
        }
    return {
        "overall_eta_seconds": round(seconds, 1),
        "overall_eta_basis": "按当前任务剩余数量、历史单项平均速度和实际并发动态估算",
        "overall_eta_item_counts": {
            "person_reid": person_reid_remaining,
            "qwen_vl": qwen_remaining,
            "ocr": ocr_remaining,
            "embedding_estimate": embedding_remaining,
        },
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
        if str(task.get("mode") or "full") in {"repair", "rebuild_search"}:
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
    if mode in {"repair", "rebuild_search"}:
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
        "recent_runs": recent_runs,
        "existing_libraries": existing_libraries(config),
        "active_runs": active_runs,
        "duplicate_groups": read_or({"total": 0, "offset": 0, "limit": 30, "items": []}, (lambda: repository.duplicate_groups(limit=30)) if repository else None),
        "timelapse_groups": read_or({"total": 0, "offset": 0, "limit": 20, "items": []}, (lambda: _timelapse_payload(repository)) if repository else None),
        "database_read_error": database_read_error,
        "saved_profile_path": str(profile_path),
        "has_saved_profile": saved_profile is not None,
        "saved_profile": saved_profile,
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
        return json.loads(path.read_text(encoding="utf-8"))
    hardware = detect_hardware()
    return build_processing_profile(
        hardware,
        scheduler_mode="stage_serial",
        model_workers=hardware["recommendation"]["model_workers"],
        frame_extract_workers=hardware["recommendation"]["frame_extract_workers"],
        video_frame_interval_seconds=3.0,
        high_value_mode="target_15",
        image_scope="frozen_current_policy",
    )


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
    if args.task_mode not in {"repair", "rebuild_search"}:
        labels = {"incremental": "增量整理"}
        raise ValueError(f"{labels.get(args.task_mode, args.task_mode)}尚未开放")
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
        mode_label = "修复缺失内容" if args.task_mode == "repair" else "重建素材位置与特殊素材入口"
        task_prefix = "repair_" if args.task_mode == "repair" else "rebuild_"
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
        payload = {
            "task_contract": "media_archive_image_video_maintenance_task_v1",
            "task_id": task_id, "name": str(library_task.get("name") or "未命名素材库"),
            "mode": args.task_mode, "library_task_path": str(library_task_path),
            "source_root": str(source), "source_access": "read_only",
            "workspace": str(workspace), "database": str(database),
            "stage_output_root": str(stage_output_root),
            "state_path": str(state_path), "log_path": str(log_path),
            "profile": profile, "runtime": runtime, "status": "queued",
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
    message = (
        "缺失内容修复已启动；已有成功结果不会重跑"
        if args.task_mode == "repair"
        else "素材位置与特殊素材分组重建已启动；不运行识别模型"
    )
    return {
        "status": "PASS", "message": message,
        "path": str(task_path), "identifier": task_id,
        "central_database_write": True, "model_run": args.task_mode == "repair",
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


def run_person_cluster_search(
    repository: ReadonlyMediaRepository,
    person_cluster_id: str,
    media_type: str,
    preview_window_ms: int,
    result_offset: int = 0,
    result_limit: int = 30,
) -> dict[str, Any]:
    """Return existing ReID cluster members using a query-only DB connection."""
    started = time.monotonic()
    page = repository.person_cluster_results(
        person_cluster_id, media_type, result_offset, result_limit,
    )
    half_window = max(0, int(preview_window_ms) // 2)
    items = []
    for row in page["items"]:
        position = max(0, int(row.get("time_position_ms") or 0))
        start_ms = max(0, position - half_window)
        end_ms = position + half_window
        cluster_id = str(row["person_cluster_id"])
        visual_id = str(row["visual_unit_id"])
        items.append({
            "result_id": f"person5e_{cluster_id}_{visual_id}",
            "visual_unit_id": visual_id,
            "source_content_id": str(row["source_content_id"]),
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
                "该画面与所选画面属于同一匿名人物簇；"
                "这只表示本地人脸特征相似，不代表姓名、服装或场景相同。"
            ),
            "environment_label": "同一人物扩展",
            "hit_reason": "same_person_reid",
            "hit_field": "person_reid",
            "relevance_reasons": ["same_person_reid"],
            "person_clusters": [{
                "person_cluster_id": cluster_id,
                "member_count": int(row.get("member_count") or 0),
                "distinct_source_count": int(row.get("distinct_source_count") or 0),
                "cluster_confidence": str(row.get("cluster_confidence") or ""),
                "human_review_status": str(row.get("human_review_status") or ""),
                "display_name": "同一匿名人物",
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
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "original_media_read": False,
    }


def run_person_cluster_catalog(
    repository: ReadonlyMediaRepository,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Expose reliable anonymous-person groups as a query-only UI catalog."""
    page = repository.person_cluster_catalog(offset, limit)
    items = []
    for index, row in enumerate(page["items"], start=int(page["offset"]) + 1):
        stored_name = str(row.get("anonymous_display_name") or "").strip()
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
            "当前只按可见人脸归并；背影、严重遮挡和过小人脸不会仅凭服装强行合并。"
        ),
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
) -> dict[str, Any]:
    clean_query = " ".join(query.split())
    if not (1 <= len(clean_query) <= 512):
        raise ValueError("搜索文字长度必须在 1 到 512 个字符之间")
    if not manager.readiness()["ready"]:
        raise RuntimeError("搜索运行环境不完整")

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
    return {
        "status": "PASS",
        "job_id": job_id,
        "query": clean_query,
        "elapsed_seconds": round(time.monotonic() - started, 3),
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
        "database_write": False,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native macOS UI bridge")
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot")
    subparsers.add_parser("search-prewarm")
    history = subparsers.add_parser("task-detail")
    history.add_argument("--task", type=Path, required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--media-type", choices=("all", "image", "video"), default="all")
    search.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    search.add_argument("--result-offset", type=int, default=0)
    search.add_argument("--result-limit", type=int, choices=range(1, 201), default=30)
    person = subparsers.add_parser("person-cluster")
    person.add_argument("--cluster-id", required=True)
    person.add_argument("--media-type", choices=("all", "image", "video"), default="all")
    person.add_argument("--preview-window-ms", type=int, choices=(5000, 10000), default=10000)
    person.add_argument("--result-offset", type=int, default=0)
    person.add_argument("--result-limit", type=int, choices=range(1, 101), default=30)
    people = subparsers.add_parser("person-clusters")
    people.add_argument("--result-offset", type=int, default=0)
    people.add_argument("--result-limit", type=int, choices=range(1, 201), default=100)
    profile = subparsers.add_parser("save-profile")
    profile.add_argument("--scheduler-mode", choices=("auto", "pipeline_async", "stage_serial"), required=True)
    profile.add_argument("--model-workers", type=int, required=True)
    profile.add_argument("--frame-extract-workers", type=int, required=True)
    profile.add_argument("--frame-interval-seconds", type=float, required=True)
    profile.add_argument("--high-value-mode", choices=("frozen_v25_compatible", "target_15", "target_20", "target_30"), required=True)
    profile.add_argument("--image-scope", choices=("frozen_current_policy", "all_images"), required=True)
    models = subparsers.add_parser("save-model-root")
    models.add_argument("--path", type=Path, required=True)
    task = subparsers.add_parser("save-task")
    task.add_argument("--source", required=True)
    task.add_argument("--name", required=True)
    task.add_argument("--task-mode", choices=("full", "incremental", "repair", "rebuild_search"), required=True)
    task.add_argument("--workspace-root")
    start = subparsers.add_parser("start-task")
    start.add_argument("--source", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--task-mode", choices=("full", "incremental", "repair", "rebuild_search"), required=True)
    start.add_argument("--workspace-root", required=True)
    existing = subparsers.add_parser("start-existing-task")
    existing.add_argument("--task", required=True)
    existing.add_argument("--task-mode", choices=("incremental", "repair", "rebuild_search"), required=True)
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
        elif args.command == "search-prewarm":
            if manager is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_search_prewarm(config, manager)
        elif args.command == "task-detail":
            report = task_detail(args.task)
        elif args.command == "search":
            if repository is None or manager is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_search(
                config, repository, manager, args.query, args.media_type, args.preview_window_ms,
                args.result_offset, args.result_limit,
            )
        elif args.command == "person-cluster":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_person_cluster_search(
                repository, args.cluster_id, args.media_type,
                args.preview_window_ms, args.result_offset, args.result_limit,
            )
        elif args.command == "person-clusters":
            if repository is None:
                raise RuntimeError("请先新建或连接一个素材库")
            report = run_person_cluster_catalog(
                repository, args.result_offset, args.result_limit,
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
