from __future__ import annotations

import json
import os
import hashlib
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


CONTRACT = "media_archive_storage_audit_v1"
CLEANUP_CONTRACT = "media_archive_storage_cleanup_plan_v1"


def _category(relative: Path, name: str) -> tuple[str, bool, bool, str]:
    parts = tuple(part.lower() for part in relative.parts)
    lower = name.lower()
    if lower in {"media_archive.sqlite", "media_archive.sqlite-wal", "media_archive.sqlite-shm"}:
        return "central_database", False, True, "中央数据库及事务文件"
    if "backup" in parts or "backups" in parts or ".backup" in lower:
        return "database_backups", False, True, "数据库恢复备份；删除会影响回滚"
    if parts and parts[0] == "logs" or lower.endswith(".log"):
        return "logs", False, False, "诊断日志；默认保留"
    if "reports" in parts:
        return "reports", False, False, "正式审计与完成报告"
    if any(part in {"tmp", "temp", "temporary", "__pycache__"} for part in parts) or lower.endswith(".tmp"):
        return "temporary", True, False, "纯临时文件；仍需用户确认后删除"
    if "cache" in parts:
        return "recoverable_cache", True, True, "可重建缓存；删除可能增加恢复耗时"
    if len(parts) >= 2 and parts[0] == "workspace" and parts[1] == "stages":
        return "stage_artifacts", False, True, "阶段正式产物或断点"
    if parts and parts[0] == "workspace":
        return "workspace_formal", False, True, "任务正式工作区"
    if lower.endswith((".dmg", ".app")) or "build" in parts or "dist" in parts:
        return "build_or_release", True, False, "构建或发布副本；不属于任务索引"
    return "task_metadata", False, True, "任务定义、状态或其他元数据"


def audit_task_storage(task_path: Path, *, largest_limit: int = 30) -> dict[str, Any]:
    task_path = Path(task_path).expanduser().resolve(strict=True)
    if not task_path.is_file():
        raise ValueError("storage_audit_task_json_missing")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    root = task_path.parent.resolve()
    categories: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"bytes": 0, "file_count": 0, "safe_to_remove_count": 0, "affects_resume_count": 0}
    )
    largest: list[dict[str, Any]] = []
    scan_errors: list[dict[str, str]] = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                size = int(path.stat().st_size)
                relative = path.relative_to(root)
            except OSError as exc:
                scan_errors.append({"path": str(path), "error": str(exc)})
                continue
            category, safe, affects_resume, reason = _category(relative, name)
            bucket = categories[category]
            bucket["bytes"] += size
            bucket["file_count"] += 1
            bucket["safe_to_remove_count"] += int(safe)
            bucket["affects_resume_count"] += int(affects_resume)
            largest.append({
                "path": str(path), "relative_path": relative.as_posix(), "bytes": size,
                "category": category, "safe_to_remove": safe,
                "affects_resume": affects_resume, "reason": reason,
            })
    largest.sort(key=lambda row: (-int(row["bytes"]), str(row["path"])))
    return {
        "contract": CONTRACT,
        "status": "PASS" if not scan_errors else "PASS_WITH_WARNINGS",
        "task_id": str(task.get("task_id") or root.name),
        "task_name": str(task.get("name") or root.name),
        "task_path": str(task_path),
        "root": str(root),
        "source_root_scanned": False,
        "read_only": True,
        "deletion_performed": False,
        "total_bytes": sum(int(row["bytes"]) for row in categories.values()),
        "total_file_count": sum(int(row["file_count"]) for row in categories.values()),
        "categories": dict(sorted(categories.items())),
        "largest_files": largest[: max(1, min(int(largest_limit), 200))],
        "scan_errors": scan_errors[:100],
        "policy": "只读分类；任何删除均需另行明确确认",
    }


def compare_task_storage(left_task: Path, right_task: Path) -> dict[str, Any]:
    left = audit_task_storage(left_task, largest_limit=10)
    right = audit_task_storage(right_task, largest_limit=10)
    names = sorted(set(left["categories"]) | set(right["categories"]))
    differences = {}
    for name in names:
        a = left["categories"].get(name, {"bytes": 0, "file_count": 0})
        b = right["categories"].get(name, {"bytes": 0, "file_count": 0})
        differences[name] = {
            "file_count_delta_right_minus_left": int(b["file_count"]) - int(a["file_count"]),
            "bytes_delta_right_minus_left": int(b["bytes"]) - int(a["bytes"]),
        }
    return {
        "contract": "media_archive_task_comparison_v1", "status": "PASS",
        "left": left, "right": right, "category_difference": differences,
        "interpretation": "正式产物、数据库、备份、日志和临时文件分开比较；总文件数差异不等于素材漏处理。",
        "read_only": True, "deletion_performed": False,
    }


def _cleanup_identity(root: Path, rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(str(root).encode("utf-8"))
    for row in rows:
        digest.update(b"\0")
        digest.update(str(row["relative_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(row["mtime_ns"]).encode("ascii"))
    return digest.hexdigest()[:24]


def build_cleanup_plan(
    task_path: Path,
    *,
    include_resume_affecting: bool = False,
) -> dict[str, Any]:
    """Build an exact, read-only cleanup plan for review."""
    task_path = Path(task_path).expanduser().resolve(strict=True)
    audit = audit_task_storage(task_path, largest_limit=200)
    root = Path(audit["root"]).resolve(strict=True)
    rows: list[dict[str, Any]] = []
    excluded_resume = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(root)
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            category, safe, affects_resume, reason = _category(relative, name)
            if not safe:
                continue
            if affects_resume and not include_resume_affecting:
                excluded_resume += 1
                continue
            rows.append({
                "path": str(resolved),
                "relative_path": relative.as_posix(),
                "bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "category": category,
                "affects_resume": bool(affects_resume),
                "reason": reason,
            })
    rows.sort(key=lambda row: str(row["relative_path"]))
    plan_id = _cleanup_identity(root, rows)
    return {
        "contract": CLEANUP_CONTRACT,
        "status": "READY_FOR_REVIEW",
        "plan_id": plan_id,
        "task_id": audit["task_id"],
        "task_path": str(task_path),
        "root": str(root),
        "created_at_epoch": time.time(),
        "candidate_count": len(rows),
        "candidate_bytes": sum(int(row["bytes"]) for row in rows),
        "excluded_resume_affecting_count": excluded_resume,
        "include_resume_affecting": bool(include_resume_affecting),
        "items": rows,
        "confirmation_phrase": f"DELETE_SAFE_ITEMS:{plan_id}",
        "read_only": True,
        "deletion_performed": False,
        "policy": "默认只含不影响恢复的已分类候选；执行前必须再次核对路径、大小和修改时间。",
    }


def apply_cleanup_plan(
    task_path: Path,
    plan: dict[str, Any],
    *,
    confirmation_phrase: str,
) -> dict[str, Any]:
    """Apply a reviewed plan while rejecting stale or out-of-root files."""
    if str(plan.get("contract")) != CLEANUP_CONTRACT:
        raise ValueError("storage_cleanup_plan_contract_invalid")
    expected_phrase = str(plan.get("confirmation_phrase") or "")
    if not expected_phrase or confirmation_phrase != expected_phrase:
        raise PermissionError("storage_cleanup_explicit_confirmation_required")
    task_path = Path(task_path).expanduser().resolve(strict=True)
    root = task_path.parent.resolve(strict=True)
    if root != Path(str(plan.get("root") or "")).resolve(strict=True):
        raise ValueError("storage_cleanup_task_root_mismatch")
    items = list(plan.get("items") or [])
    if str(plan.get("plan_id") or "") != _cleanup_identity(root, items):
        raise ValueError("storage_cleanup_plan_identity_mismatch")
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in items:
        path = Path(str(row.get("path") or ""))
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError) as exc:
            skipped.append({"path": str(path), "reason": f"path_unavailable:{exc}"})
            continue
        if resolved.is_symlink() or not resolved.is_file():
            skipped.append({"path": str(resolved), "reason": "not_regular_file"})
            continue
        if int(stat.st_size) != int(row.get("bytes") or -1) or int(stat.st_mtime_ns) != int(row.get("mtime_ns") or -1):
            skipped.append({"path": str(resolved), "reason": "changed_since_plan"})
            continue
        category, safe, affects_resume, _reason = _category(
            resolved.relative_to(root), resolved.name,
        )
        if not safe or category != str(row.get("category") or ""):
            skipped.append({"path": str(resolved), "reason": "classification_changed"})
            continue
        if affects_resume and not bool(plan.get("include_resume_affecting")):
            skipped.append({"path": str(resolved), "reason": "resume_affecting_not_authorized"})
            continue
        resolved.unlink()
        removed.append({"path": str(resolved), "bytes": int(stat.st_size), "category": category})
    return {
        "contract": "media_archive_storage_cleanup_result_v1",
        "status": "PASS" if not skipped else "PASS_WITH_WARNINGS",
        "plan_id": plan["plan_id"],
        "removed_count": len(removed),
        "removed_bytes": sum(int(row["bytes"]) for row in removed),
        "removed": removed,
        "skipped": skipped,
        "deletion_performed": bool(removed),
        "original_media_touched": False,
        "task_database_touched": False,
    }
