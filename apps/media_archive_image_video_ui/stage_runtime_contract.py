from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence


CONTRACT = "media_archive_stage_runtime_contract_v1"
PROGRESS_PATTERNS = (
    re.compile(
        r"\[progress\]\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)"
        r"(?:.*?success[=:](?P<success>\d+))?"
        r"(?:.*?(?:failed|failure)[=:](?P<failed>\d+))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[(?:skip-existing|done|completed)[^\d]*(?P<done>\d+)\s*/\s*(?P<total>\d+)\]",
        re.IGNORECASE,
    ),
)
QWEN_PROGRESS_PATTERN = re.compile(
    r"\[PROGRESS\].*?run_id=(?P<run_id>\S+).*?worker=(?P<worker>\d+)"
    r".*?candidate_id=(?P<candidate>\S+).*?status=(?P<status>\S+)",
    re.IGNORECASE,
)
SCAN_PHASE_PROGRESS_PATTERN = re.compile(
    r"\[(?P<phase>scan|hash)\s+(?P<done>\d+)\s*/\s*(?P<total>\d+)\]"
    r"(?:\s+(?P<item>.+))?$",
    re.IGNORECASE,
)


def _argument_value(command: Sequence[str], names: set[str]) -> str:
    result = ""
    for index, value in enumerate(command[:-1]):
        if value in names:
            result = str(command[index + 1])
    return result


def stage_output_dir(stage: dict[str, Any], workspace: Path) -> Path:
    command = [str(value) for value in stage.get("command", [])]
    configured = _argument_value(command, {"--out", "--output-root"})
    if configured:
        candidate = Path(configured).expanduser().absolute()
        try:
            candidate.relative_to(workspace.expanduser().absolute())
            return candidate
        except ValueError:
            pass
    return workspace / "stages" / "_runtime_contracts" / str(stage.get("key") or "stage")


def tree_stats(path: Path) -> dict[str, int]:
    files = 0
    size = 0
    if not path.exists():
        return {"file_count": 0, "bytes": 0}
    for root, _directories, names in os.walk(path):
        for name in names:
            candidate = Path(root) / name
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    files += 1
                    size += candidate.stat().st_size
            except OSError:
                continue
    return {"file_count": files, "bytes": size}


def database_stats(database: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for suffix, label in (("", "sqlite"), ("-wal", "wal"), ("-shm", "shm")):
        candidate = Path(str(database) + suffix)
        try:
            result[label] = candidate.stat().st_size if candidate.is_file() else 0
        except OSError:
            result[label] = 0
    result["total"] = sum(result.values())
    return result


def parse_progress_line(line: str) -> dict[str, Any]:
    stripped = line.strip()
    payload: dict[str, Any] = {}
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            candidate = json.loads(stripped)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and (
            candidate.get("contract") == CONTRACT
            or candidate.get("event") in {"progress", "stage_progress"}
        ):
            for key in (
                "completed", "total", "success", "skipped", "failed", "remaining",
                "current_item", "bytes_processed", "actual_workers",
                "ffmpeg_processes", "model_workers", "output_files",
                "configured_workers", "started_workers", "alive_workers",
                "active_workers", "idle_workers", "crashed_workers",
                "restart_count", "queue_pending", "queue_running",
                "event", "reason_code", "error_code", "error_message",
                "input_files", "input_sources", "input_bytes",
                "output_records", "output_bytes", "items_per_second",
                "frames_per_second", "megabytes_per_second",
            ):
                if key in candidate:
                    payload[key] = candidate[key]
            return payload
    qwen_match = QWEN_PROGRESS_PATTERN.search(stripped)
    if qwen_match:
        return {
            "qwen_run_id": qwen_match.group("run_id"),
            "current_item": qwen_match.group("candidate"),
            "worker_id": int(qwen_match.group("worker")),
            "item_status": qwen_match.group("status").rstrip(","),
        }
    scan_match = SCAN_PHASE_PROGRESS_PATTERN.search(stripped)
    if scan_match:
        completed = int(scan_match.group("done"))
        return {
            "completed": completed,
            "total": int(scan_match.group("total")),
            "success": completed,
            "current_item": str(scan_match.group("item") or "").strip()[:1000],
        }
    for pattern in PROGRESS_PATTERNS:
        match = pattern.search(stripped)
        if match:
            payload["completed"] = int(match.group("done"))
            payload["total"] = int(match.group("total"))
            if match.groupdict().get("success"):
                payload["success"] = int(match.group("success"))
            if match.groupdict().get("failed"):
                payload["failed"] = int(match.group("failed"))
            if "skip-existing" in stripped.lower():
                payload["skipped"] = int(match.group("done"))
            break
    # A database/output ``path=`` is configuration, not the item currently
    # being processed.  Treating it as live work made Stage 01 show the index
    # database instead of the selected source file.
    current = re.search(r"(?:file|source|current)[=:](.+?)(?:\s+[a-z_]+[=:]|$)", stripped)
    if current:
        payload["current_item"] = current.group(1).strip()[:1000]
    return payload


def live_eta(completed: int, total: int, elapsed_seconds: float) -> tuple[float | None, str]:
    if completed < 20 or elapsed_seconds < 30 or total <= completed:
        return None, "正在估算；至少完成 20 项并运行 30 秒后显示"
    rate = completed / elapsed_seconds
    if rate <= 0:
        return None, "当前没有可用吞吐样本"
    return round((total - completed) / rate, 1), "按本阶段当前实际吞吐量估算"


def write_stage_reports(
    *,
    stage: dict[str, Any],
    workspace: Path,
    database: Path,
    record: dict[str, Any],
    before_storage: dict[str, Any],
    failure_rows: Sequence[dict[str, Any]] = (),
    skipped_rows: Sequence[dict[str, Any]] = (),
) -> dict[str, str]:
    output = stage_output_dir(stage, workspace)
    reports = output / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    after_tree = tree_stats(output)
    after_db = database_stats(database)
    before_tree = dict(before_storage.get("stage") or {})
    before_db = dict(before_storage.get("database") or {})
    storage_delta = {
        "contract": CONTRACT,
        "stage_key": stage.get("key"),
        "stage_output": {
            "before": before_tree,
            "after": after_tree,
            "delta_bytes": after_tree["bytes"] - int(before_tree.get("bytes") or 0),
            "delta_files": after_tree["file_count"] - int(before_tree.get("file_count") or 0),
        },
        "database": {
            "before": before_db,
            "after": after_db,
            "delta_sqlite_bytes": after_db["sqlite"] - int(before_db.get("sqlite") or 0),
            "delta_wal_bytes": after_db["wal"] - int(before_db.get("wal") or 0),
            "delta_shm_bytes": after_db["shm"] - int(before_db.get("shm") or 0),
            "delta_total_bytes": after_db["total"] - int(before_db.get("total") or 0),
        },
    }
    completed = int(record.get("live_completed") or 0)
    total = int(record.get("live_total") or 0)
    success = int(record.get("live_success") or 0)
    skipped = int(record.get("live_skipped") or 0)
    failed = int(record.get("live_failed") or 0)
    # Several established stages historically emitted only ``completed/total``.
    # A successful process with a complete counter is reliable evidence that
    # those items succeeded; keeping success at zero made the five reports
    # contradict the database and the UI.
    if (
        record.get("status") == "success"
        and completed > 0
        and success == 0
        and skipped == 0
        and failed == 0
    ):
        success = completed
    elapsed = float(record.get("elapsed_seconds") or 0.0)
    items_per_second = (
        round(completed / elapsed, 6) if completed > 0 and elapsed > 0 else None
    )
    bytes_processed = int(record.get("bytes_processed") or 0)
    megabytes_per_second = (
        round(bytes_processed / 1_000_000 / elapsed, 6)
        if bytes_processed > 0 and elapsed > 0 else None
    )
    command = [str(value) for value in stage.get("command", [])]
    script = ""
    if "--script" in command:
        index = len(command) - 1 - command[::-1].index("--script")
        if index + 1 < len(command):
            script = command[index + 1]
    elif len(command) > 1:
        script = command[1]
    runtime = {
        "contract": CONTRACT,
        "stage_key": stage.get("key"),
        "stage_name": stage.get("name"),
        "actual_script": script,
        "actual_command": command,
        "status": record.get("status"),
        "started_at_epoch": record.get("started_at_epoch"),
        "finished_at_epoch": record.get("finished_at_epoch"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "input_file_count": record.get("input_files"),
        "input_source_count": record.get("input_sources"),
        "input_bytes": record.get("input_bytes"),
        "completed": completed,
        "total": total,
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "remaining": max(0, total - completed) if total else 0,
        "current_item": str(record.get("current_item") or ""),
        "configured_workers": record.get("configured_workers"),
        "started_workers": record.get("started_workers"),
        "alive_workers": record.get("alive_workers"),
        "active_workers": record.get("active_workers"),
        "idle_workers": record.get("idle_workers"),
        "actual_workers": record.get("actual_workers"),
        "ffmpeg_processes": record.get("ffmpeg_processes"),
        "model_workers": record.get("model_workers"),
        "crashed_workers": record.get("crashed_workers"),
        "restart_count": record.get("restart_count"),
        "queue_pending": record.get("queue_pending"),
        "queue_running": record.get("queue_running"),
        "bytes_processed": bytes_processed,
        "items_per_second": record.get("items_per_second") or items_per_second,
        "frames_per_second": record.get("frames_per_second"),
        "megabytes_per_second": (
            record.get("megabytes_per_second") or megabytes_per_second
        ),
        "eta_seconds": record.get("eta_seconds"),
        "eta_basis": str(record.get("eta_basis") or ""),
    }
    summary = {
        **runtime,
        "exit_code": record.get("exit_code"),
        "output_path": str(output),
        "output_record_count": record.get("output_records"),
        "output_file_count": int(record.get("output_files") or after_tree["file_count"]),
        "output_bytes": int(record.get("output_bytes") or after_tree["bytes"]),
        "database_delta_bytes": storage_delta["database"]["delta_total_bytes"],
        "failure_count": len(failure_rows),
        "skipped_count": len(skipped_rows),
        "error_summary": str(record.get("error_summary") or ""),
    }

    def write_json(name: str, value: Any) -> None:
        target = reports / name
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    def write_csv(name: str, rows: Sequence[dict[str, Any]]) -> None:
        target = reports / name
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        fields = sorted({key for row in rows for key in row}) or ["item", "reason"]
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)

    write_json("stage_summary.json", summary)
    write_json("stage_storage_delta.json", storage_delta)
    write_json("stage_runtime_metrics.json", runtime)
    write_csv("stage_failures.csv", failure_rows)
    write_csv("stage_skipped.csv", skipped_rows)
    return {
        "summary": str(reports / "stage_summary.json"),
        "failures": str(reports / "stage_failures.csv"),
        "skipped": str(reports / "stage_skipped.csv"),
        "storage_delta": str(reports / "stage_storage_delta.json"),
        "runtime_metrics": str(reports / "stage_runtime_metrics.json"),
    }
