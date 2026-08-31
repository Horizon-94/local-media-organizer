from __future__ import annotations

import csv
import html
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional, Sequence

from .central_database import (
    append_event,
    central_audit,
    claim_work_item,
    enqueue_work_items,
    ensure_schema as ensure_central_schema,
    finish_work_item,
    heartbeat_work_item,
    sync_artifact_lineage,
    sync_original_files,
    update_search_state,
)
from .stage_runtime_contract import (
    database_stats,
    live_eta,
    parse_progress_line,
    stage_output_dir,
    tree_stats,
    write_stage_reports,
)


PIPELINE_CONTRACT = "media_archive_image_video_pipeline_v1"
STAGE_ERROR_TAIL_LINES = 80
STAGE_ERROR_DETAILS_MAX_CHARS = 16 * 1024


def required_runtime_path(mapping: dict[str, Any], key: str, section: str) -> str:
    """Return one deployment path without falling back to a developer machine.

    Portable releases materialize every runtime/model location from the signed
    runtime contract.  A missing entry must be a visible configuration error;
    silently substituting the developer's home directory would make a build
    appear portable while remaining tied to one Mac.
    """
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise ValueError(f"missing_runtime_path:{section}.{key}")
    return value


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_stage_failure(lines: Sequence[str], exit_code: int) -> tuple[str, str]:
    cleaned = [line.strip() for line in lines if line.strip() and line.strip() not in {"}", "]", "]}"}]
    details = "\n".join(cleaned[-STAGE_ERROR_TAIL_LINES:])[-STAGE_ERROR_DETAILS_MAX_CHARS:]
    preferred_markers = (
        "python_mismatch:", "stage_acceptance_failed", "reason_code=",
        "RuntimeError:", "ValueError:",
        "ModuleNotFoundError:", "FileNotFoundError:", "PermissionError:",
        "FileExistsError:", "missing_required_", "output_outside_",
        "Traceback (most recent call last):",
    )
    for marker in preferred_markers:
        for line in reversed(cleaned):
            if marker in line:
                return line.strip(' \t",'), details
    if cleaned:
        return cleaned[-1][:1000], details
    return f"子进程退出码 {exit_code}，没有产生可读取的错误输出", details


def command_for_resume(stage: dict[str, Any], workspace: Path) -> list[str]:
    """Turn an interrupted Qwen stage into a true database-backed resume."""
    command = [str(value) for value in stage.get("command", [])]
    if stage.get("key") != "qwen_optional_v2":
        return command
    run_id_path = stage_output_dir(stage, workspace) / "run_id.txt"
    if not run_id_path.is_file():
        return command
    run_id = run_id_path.read_text(encoding="utf-8").strip()
    if not run_id:
        raise RuntimeError(f"qwen_resume_run_id_empty:{run_id_path}")
    mode_indexes = [
        index for index, value in enumerate(command[:-1])
        if value == "--mode" and command[index + 1] == "run"
    ]
    if not mode_indexes:
        raise RuntimeError("qwen_resume_mode_argument_missing")
    command[mode_indexes[-1] + 1] = "resume"
    if "--run-id" in command:
        command[command.index("--run-id") + 1] = run_id
    else:
        command.extend(["--run-id", run_id])
    # The database runner verifies that only an explicitly compatible script
    # upgrade may resume an existing run.  This bridge has already selected
    # the same task, output root and run id, so preserve that safe path rather
    # than silently falling back to a fresh mkdir-based run.
    if "--confirm-compatible-script-resume" not in command:
        command.append("--confirm-compatible-script-resume")
    return command


def qwen_database_progress(database: Path, output: Path, run_id: str) -> dict[str, Any]:
    """Read committed progress and discard stale worker-status files."""
    errors: list[str] = []
    con: sqlite3.Connection | None = None
    # Finished libraries on some external/APFS paths can reject a normal WAL
    # readonly lock.  This observation-only progress query can safely use the
    # immutable fallback; task writes always use the normal writer connection.
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"{database.resolve().as_uri()}?{query}", uri=True, timeout=5.0)
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            break
        except sqlite3.Error as exc:
            errors.append(str(exc))
    if con is None:
        raise sqlite3.OperationalError("qwen_progress_database_open_failed:" + " | ".join(errors))
    try:
        con.execute("PRAGMA query_only=ON")
        counts = {
            str(status): int(count)
            for status, count in con.execute(
                "SELECT status,COUNT(*) FROM stop03_3_qwenvl_run_items "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        }
    finally:
        con.close()
    active_workers = 0
    alive_workers = 0
    started_workers = 0
    crashed_workers = 0
    current_items: list[str] = []
    now = time.time()
    for path in (output / "worker_status").glob("worker_*.json"):
        try:
            status = load_json(path)
            if status.get("pid"):
                started_workers += 1
            lifecycle = str(status.get("lifecycle") or "")
            fresh = now - path.stat().st_mtime <= 90.0
            if lifecycle in {"failed", "restart_exhausted", "restarting"}:
                crashed_workers += 1
            if lifecycle == "running" and fresh:
                alive_workers += 1
                if status.get("current_candidate_id"):
                    active_workers += 1
                    current_items.append(str(status["current_candidate_id"]))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    success = counts.get("success", 0)
    skipped = counts.get("skipped", 0)
    failed = sum(
        count for status, count in counts.items()
        if status not in {"pending", "running", "success", "skipped"}
    )
    return {
        "completed": success + skipped + failed,
        "total": sum(counts.values()),
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "started_workers": started_workers,
        "alive_workers": alive_workers,
        "actual_workers": alive_workers,
        "active_workers": active_workers,
        "idle_workers": max(0, alive_workers - active_workers),
        "crashed_workers": crashed_workers,
        "restart_count": crashed_workers,
        "model_workers": alive_workers,
        "queue_pending": counts.get("pending", 0),
        "queue_running": counts.get("running", 0),
        "current_item": ", ".join(current_items),
    }


def _open_acceptance_database(database: Path) -> sqlite3.Connection:
    """Open an existing task database without ever creating or modifying it."""
    errors: list[str] = []
    for query in ("mode=ro", "mode=ro&immutable=1"):
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(
                f"{Path(database).expanduser().resolve().as_uri()}?{query}",
                uri=True,
                timeout=5.0,
            )
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return con
        except sqlite3.Error as exc:
            errors.append(str(exc))
            if con is not None:
                con.close()
    raise sqlite3.OperationalError(
        "acceptance_database_open_failed:" + " | ".join(errors)
    )


def write_final_task_report(task: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    """Write a database-derived, durable completion report for one task.

    This intentionally reads only task outputs.  It lets reopened historical
    libraries show the same counts without relying on stale SwiftUI counters.
    """
    workspace = Path(task["workspace"])
    reports = workspace / "reports"
    db = Path(task["database"])
    counts: dict[str, int] = {}
    if db.is_file():
        with sqlite3.connect(str(db), timeout=5.0) as con:
            for name, query in {
                "source_images": "SELECT COUNT(*) FROM source_assets WHERE media_type='image' AND COALESCE(is_deleted_or_missing,0)=0",
                "source_videos": "SELECT COUNT(*) FROM source_assets WHERE media_type='video' AND COALESCE(is_deleted_or_missing,0)=0",
                "visual_units": "SELECT COUNT(*) FROM visual_units",
                "openclip_vectors": "SELECT COUNT(*) FROM embeddings",
                "yoloe_labels": "SELECT COUNT(*) FROM visual_labels",
                "text_vectors": "SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE status='success'",
            }.items():
                try:
                    counts[name] = int(con.execute(query).fetchone()[0])
                except sqlite3.Error:
                    counts[name] = 0
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
    else:
        integrity = "database_missing"
    payload = {
        "contract": "media_archive_final_task_report_v1",
        "task_id": str(task.get("task_id") or ""),
        "task_name": str(task.get("name") or ""),
        "task_mode": str(task.get("mode") or "full"),
        "status": str(state.get("status") or "unknown"),
        "database": str(db),
        "database_integrity": integrity,
        "counts": counts,
        "stage_count": int(state.get("stage_count") or 0),
        "completed_stage_count": int(state.get("completed_stage_count") or 0),
        "elapsed_seconds": round(
            max(0.0, float(state.get("finished_at_epoch") or time.time()) - float(state.get("started_at_epoch") or time.time())), 3
        ),
        "workspace_storage": tree_stats(workspace),
        "generated_at_epoch": time.time(),
    }
    json_path = reports / "final_task_report.json"
    md_path = reports / "final_task_report.md"
    csv_path = reports / "final_task_report.csv"
    html_path = reports / "final_task_report.html"
    atomic_json(json_path, payload)
    lines = ["# 本地素材整理最终报告", "", f"- 任务：{payload['task_name']}", f"- 状态：{payload['status']}", f"- 数据库完整性：{integrity}", "", "## 数据库统计", ""]
    lines.extend(f"- {key}: {value}" for key, value in counts.items())
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["section", "key", "value"])
        for key in ("task_id", "task_name", "task_mode", "status", "database_integrity", "elapsed_seconds"):
            writer.writerow(["task", key, payload[key]])
        for key, value in sorted(counts.items()):
            writer.writerow(["counts", key, value])
        for stage in state.get("stages", []):
            writer.writerow(["stage", str(stage.get("key") or ""), str(stage.get("status") or "")])
    table_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in sorted(counts.items())
    )
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'><title>本地素材整理最终报告</title>"
        "<style>body{font:15px -apple-system;margin:32px;max-width:900px}"
        "table{border-collapse:collapse;width:100%}th,td{padding:8px;border:1px solid #ddd;text-align:left}</style>"
        f"<h1>本地素材整理最终报告</h1><p>任务：{html.escape(payload['task_name'])}</p>"
        f"<p>状态：{html.escape(payload['status'])}；数据库：{html.escape(integrity)}</p>"
        f"<table>{table_rows}</table>",
        encoding="utf-8",
    )
    return {
        "json": str(json_path), "markdown": str(md_path),
        "csv": str(csv_path), "html": str(html_path),
    }


def offline_environment(
    workspace: Path,
    tools: Optional[dict[str, str]] = None,
    model_root: Path | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    # The native launcher embeds Python and therefore sets PYTHONHOME for its
    # own process.  Frozen stages run in their dedicated virtual environments;
    # inheriting the embedded PYTHONHOME makes those interpreters look for the
    # standard library inside the app bundle and fail before importing
    # ``encodings``.  Each child runtime must start from its own prefix.
    for inherited in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        environment.pop(inherited, None)
    cache = workspace / "cache"
    tool_dirs = [str(Path(value).parent) for value in (tools or {}).values()]
    inherited_path = environment.get("PATH", "").split(os.pathsep)
    environment["PATH"] = os.pathsep.join(dict.fromkeys(
        [*tool_dirs, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", *inherited_path]
    ))
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "ULTRALYTICS_OFFLINE": "1",
        "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
        "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
        "HF_HOME": str(cache / "huggingface"),
        "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "YOLO_CONFIG_DIR": str(cache / "ultralytics"),
        "NO_ALBUMENTATIONS_UPDATE": "1",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    })
    if model_root is not None:
        environment["MEDIA_ARCHIVE_MODEL_ROOT"] = str(
            Path(model_root).expanduser().absolute()
        )
    return environment


def runtime_model_root(runtime: dict[str, Any]) -> Path | None:
    explicit = str(runtime.get("model_root") or "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    model_paths = [
        str(Path(value).expanduser().absolute())
        for value in (runtime.get("models") or {}).values()
        if str(value or "").strip()
    ]
    if not model_paths:
        return None
    return Path(os.path.commonpath(model_paths))


def validate_stage_acceptance(task: dict[str, Any], stage_key: str) -> str:
    """Reject a zero-output stage when the database proves work existed."""
    db = Path(task["database"])
    if not db.is_file():
        return "DATABASE_MISSING_AFTER_STAGE" if stage_key == "scan" else ""
    with _open_acceptance_database(db) as con:
        def table(name: str) -> bool:
            return con.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
            ).fetchone() is not None

        def count(sql: str) -> int:
            row = con.execute(sql).fetchone()
            return int(row[0] or 0) if row else 0

        if stage_key == "scan" and table("source_assets"):
            if count("SELECT COUNT(*) FROM source_assets WHERE media_type IN ('image','video')") == 0:
                return "NO_IMAGE_OR_VIDEO_SOURCE_FOUND"
        if stage_key == "image_preview" and table("source_assets") and table("visual_units"):
            images = count("SELECT COUNT(*) FROM source_assets WHERE media_type='image'")
            image_visuals = count(
                "SELECT COUNT(*) FROM visual_units v JOIN source_assets s USING(source_content_id) WHERE s.media_type='image'"
            )
            if images > 0 and image_visuals == 0:
                return "IMAGE_INPUT_WITHOUT_DERIVED_PREVIEWS"
        if stage_key == "video_frames" and table("source_assets") and table("visual_units"):
            videos = count("SELECT COUNT(*) FROM source_assets WHERE media_type='video'")
            video_visuals = count(
                "SELECT COUNT(*) FROM visual_units v JOIN source_assets s USING(source_content_id) WHERE s.media_type='video'"
            )
            if videos > 0 and video_visuals == 0:
                return "VIDEO_INPUT_WITHOUT_DERIVED_FRAMES"
        if stage_key in {"openclip", "rebuild_openclip"} and table("visual_units") and table("embeddings"):
            visuals = count("SELECT COUNT(*) FROM visual_units")
            vectors = count("SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings")
            if visuals > 0 and vectors != visuals:
                return f"OPENCLIP_COVERAGE_MISMATCH_{vectors}_OF_{visuals}"
        if stage_key == "person_reid_optional_v1":
            if not table("stop03_1c_person_reid_runs") or not table(
                "stop03_1c_person_reid_run_items",
            ):
                return "PERSON_REID_RESULT_TABLES_MISSING"
            latest = con.execute(
                """SELECT run_id,status,visual_unit_count
                   FROM stop03_1c_person_reid_runs
                   ORDER BY created_at DESC,run_id DESC LIMIT 1"""
            ).fetchone()
            if not latest or str(latest[1]) != "success":
                return "PERSON_REID_LATEST_RUN_NOT_SUCCESS"
            terminal_row = con.execute(
                """SELECT COUNT(*) FROM stop03_1c_person_reid_run_items
                   WHERE run_id=? AND status IN ('success','no_face')""",
                (str(latest[0]),),
            ).fetchone()
            terminal = int(terminal_row[0] or 0) if terminal_row else 0
            expected = int(latest[2] or 0)
            if terminal != expected:
                return f"PERSON_REID_COVERAGE_MISMATCH_{terminal}_OF_{expected}"
    return ""


def validate_final_pipeline_acceptance(task: dict[str, Any]) -> dict[str, str]:
    """Run the final read-only full-pipeline gate before search can be opened."""
    db = Path(task["database"])
    errors: dict[str, str] = {}
    if not db.is_file():
        return {"database": "DATABASE_MISSING_AFTER_PIPELINE"}
    with sqlite3.connect(str(db), timeout=30.0) as con:
        con.execute("PRAGMA query_only=ON")

        def table(name: str) -> bool:
            return con.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
                (name,),
            ).fetchone() is not None

        def count(sql: str, params: Sequence[Any] = ()) -> int:
            row = con.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0

        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors["integrity_check"] = integrity
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        if foreign_keys:
            errors["foreign_key_check"] = str(foreign_keys)
        required = (
            "source_assets", "derived_assets", "visual_units", "embeddings",
            "visual_labels", "stop03_2_candidate_queue_frozen_v25",
            "stop03_5_unified_evidence_items", "stop03_5d_text_documents",
            "stop03_5d_text_vectors", "stop03_5d_document_vector_links",
        )
        for name in required:
            if not table(name):
                errors[f"table.{name}"] = "missing"
        if errors:
            return errors
        visuals = count("SELECT COUNT(*) FROM visual_units")
        visual_vectors = count("SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings")
        if visuals <= 0 or visual_vectors != visuals:
            errors["visual_vector_coverage"] = f"{visual_vectors}/{visuals}"
        candidates = count("SELECT COUNT(*) FROM stop03_2_candidate_queue_frozen_v25")
        if visuals > 0 and candidates <= 0:
            errors["candidate_queue"] = "empty"
        documents = count("SELECT COUNT(*) FROM stop03_5d_text_documents")
        links = count("SELECT COUNT(*) FROM stop03_5d_document_vector_links")
        vectors = count("SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE status='success'")
        if documents <= 0 or links != documents or vectors <= 0:
            errors["text_vector_coverage"] = (
                f"documents={documents},links={links},vectors={vectors}"
            )
        person_error = validate_stage_acceptance(task, "person_reid_optional_v1")
        if person_error:
            errors["person_reid_coverage"] = person_error
    return errors


def _stage(
    key: str,
    name: str,
    python: Path,
    entry: Path,
    script: Path,
    allowed: Path,
    source: Path,
    arguments: Sequence[str],
    *,
    requires_source: bool = False,
) -> dict[str, Any]:
    command = [
        str(python), str(entry), "--script", str(script),
        "--allowed-output-root", str(allowed),
    ]
    if requires_source:
        command.extend(["--allowed-source-root", str(source)])
    command.extend(["--", *map(str, arguments)])
    return {
        "key": key,
        "name": name,
        "command": command,
    }


def build_stage_plan(task: dict[str, Any]) -> list[dict[str, Any]]:
    project = Path(task["runtime"]["project_root"])
    workspace = Path(task["workspace"])
    stages = Path(task.get("stage_output_root") or (workspace / "stages"))
    db = Path(task["database"])
    source = Path(task["source_root"])
    runtimes = {key: Path(value) for key, value in task["runtime"]["python"].items()}
    tools = {key: Path(value) for key, value in task["runtime"].get("tools", {}).items()}
    models = task["runtime"].get("models") or {}
    entry = project / "scripts/04_media_archive_app/run_generic_pipeline_stage.py"
    visual = project / "scripts/03_stop03_visual_analysis"
    early = project / "scripts/02_step01_step02_pipeline"
    app_scripts = project / "scripts/04_media_archive_app"
    scripts = {
        "stage_runner": str(app_scripts / "run_generic_pipeline_stage.py"),
        "source_scan": str(early / "step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py"),
        "image_preview": str(early / "step02_2_image_preview_from_db_safe_v6_20260709_182200.py"),
        "video_frames": str(app_scripts / "step02_video_frame_generic_interval_v1.py"),
        "prepare_visual_schema": str(app_scripts / "prepare_visual_analysis_schema_v1.py"),
        "yoloe": str(visual / "stop03_yoloe_full_from_db_safe_v6_20260709_170200.py"),
        "openclip": str(visual / "stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py"),
        "dedup": str(project / "scripts/source_frame_dedup_central_db.py"),
        "person_reid": str(app_scripts / "stop03_1c_person_reid_db_orchestrator_v1.py"),
        "candidate_snapshot": str(app_scripts / "stop03_2_v25_dynamic_snapshot_v1.py"),
        "candidate_select": str(app_scripts / "stop03_2_candidate_queues_generic_library_v1.py"),
        "optional_stage": str(app_scripts / "run_optional_enrichment_stage_v1.py"),
        "qwen": str(visual / "stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py"),
        "ocr": str(visual / "stop03_4_ocr_db_orchestrator_v1.py"),
        "evidence": str(visual / "stop03_5b_unified_evidence_staging_v1.py"),
        "propagation": str(visual / "stop03_5c_qwenvl_yolo_propagation_v1.py"),
        "embedding": str(visual / "stop03_5d_text_embedding_db_orchestrator_v1.py"),
        "finder_tag_refresh": str(app_scripts / "refresh_existing_library_finder_tags_v1.py"),
        "search_rebuild": str(app_scripts / "rebuild_search_index_from_database_v1.py"),
        "source_lineage_restore": str(
            app_scripts / "restore_source_file_lineage_from_manifest_v1.py"
        ),
        "supplement_contract": str(app_scripts / "stop03_3_qwenvl_supplement_contract_v1.py"),
        "supplement_qwen": str(app_scripts / "stop03_3_qwenvl_supplement_orchestrator_v1.py"),
        "supplement_evidence_merge": str(app_scripts / "stop03_5b_merge_qwenvl_supplement_v1.py"),
        "audio_enrichment": str(app_scripts / "run_audio_enrichment_from_database_v1.py"),
        "audio_pilot": str(app_scripts / "run_audio_search_pilot_v1.py"),
        "audio_embedding_commit": str(app_scripts / "run_audio_embedding_commit_v1.py"),
    }
    scripts.update(task["runtime"].get("scripts") or {})
    configs = {
        "candidate": str(project / "configs/stop03_2_high_value_policy_v25.json"),
        "person_reid": str(project / "configs/stop03_1c_person_reid_db_v1.json"),
        "qwen": str(project / "configs/stop03_3_qwenvl_db_v1.json"),
        "qwen_prompt": str(project / "configs/qwenvl_prompt_v2_384.txt"),
        "ocr": str(project / "configs/stop03_4_ocr_db_v1.json"),
        "evidence": str(project / "configs/stop03_5b_unified_evidence_staging_v1.json"),
        "propagation": str(project / "configs/stop03_5c_qwenvl_yolo_propagation_v1.json"),
        "embedding_contract": str(project / "configs/stop03_5d_text_embedding_db_contract_v1.json"),
        "embedding_runtime": str(project / "configs/stop03_5d_text_embedding_db_orchestrator_v1.json"),
        "yoloe_registry": str(project / "configs/yoloe_keyword_registry_default_v1.json"),
    }
    configs.update(task["runtime"].get("configs") or {})
    migrations = {
        "person_reid": str(project / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"),
        "ocr": str(project / "migrations/20260716_stop03_4_ocr_db_v1.sql"),
        "evidence": str(project / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql"),
        "propagation": str(project / "migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql"),
        "embedding": str(project / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"),
        "supplement": str(project / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql"),
        "audio": str(project / "migrations/20260809_audio_speech_search_v1.sql"),
    }
    migrations.update(task["runtime"].get("migrations") or {})
    entry = Path(scripts["stage_runner"])
    model_workers = int(task["profile"]["scheduler"]["model_workers"])
    frame_workers = int(task["profile"]["scheduler"]["frame_extract_workers"])
    frame_interval = int(
        float(task["profile"].get("video_sampling", {}).get("frame_interval_seconds") or 3)
    )
    high_value_mode = str(
        task["profile"].get("high_value_policy", {}).get("mode")
        or "frozen_v25_compatible"
    )
    ocr_workers = int(task["runtime"].get("ocr_workers") or model_workers)
    embedding_workers = int(task["runtime"].get("embedding_workers") or model_workers)
    person_reid_workers = int(
        task["runtime"].get("person_reid_workers") or min(8, max(1, model_workers * 3))
    )

    def optional_stage(
        key: str,
        name: str,
        stage_kind: str,
        out: Path,
        inner: dict[str, Any],
        *,
        run_delegate_on_empty: bool = False,
    ) -> dict[str, Any]:
        optional_arguments = [
            str(runtimes["system"]),
            str(scripts["optional_stage"]),
            "--stage-kind", stage_kind,
            "--db", str(db),
            "--out", str(out),
            "--allowed-output-root", str(workspace),
        ]
        if run_delegate_on_empty:
            optional_arguments.append("--run-delegate-on-empty")
        return {
            "key": key,
            "name": name,
            "command": [*optional_arguments, "--", *inner["command"]],
        }

    task_mode = str(task.get("mode") or "full")

    audio_runtime_available = (
        {"whisper", "embedding"} <= set(runtimes)
        and {"ffmpeg", "ffprobe", "deep_filter"} <= set(tools)
        and {"silero_vad", "whisper", "deep_filter", "text_embedding"} <= set(models)
    )

    def audio_enrichment_stage(
        out: Path, preextracted_audio_manifest: Path | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "--db", str(db), "--out", str(out),
            "--migration", str(migrations["audio"]),
            "--audio-python", str(runtimes["whisper"]),
            "--embedding-python", str(runtimes["embedding"]),
            "--audio-pilot-script", str(scripts["audio_pilot"]),
            "--embedding-script", str(scripts["audio_embedding_commit"]),
            "--ffmpeg", str(tools["ffmpeg"]), "--ffprobe", str(tools["ffprobe"]),
            "--silero-root", required_runtime_path(models, "silero_vad", "models"),
            "--whisper-model", required_runtime_path(models, "whisper", "models"),
            "--deep-filter-executable", str(tools["deep_filter"]),
            "--deep-filter-model", required_runtime_path(models, "deep_filter", "models"),
            "--embedding-model", required_runtime_path(models, "text_embedding", "models"),
            "--workers", str(min(3, max(1, model_workers))),
            "--confirm-central-db-write",
        ]
        if preextracted_audio_manifest is not None:
            arguments.extend([
                "--preextracted-audio-manifest",
                str(preextracted_audio_manifest),
            ])
        return _stage(
            "audio_search_enrichment", "提取人声并建立音频文本搜索",
            runtimes["whisper"], entry, Path(scripts["audio_enrichment"]),
            workspace, source,
            arguments,
            requires_source=True,
        )

    if task_mode == "audio_enrichment":
        if not audio_runtime_available:
            raise ValueError("audio_enrichment_runtime_incomplete")
        return [audio_enrichment_stage(stages / "01_audio_search_enrichment")]
    # Keep the earlier image-only maintenance plan available for existing
    # task definitions, but do not expose it as the generic repair mode.
    if task_mode == "repair_images":
        candidate_out = stages / "02_candidate_dry_run"
        repair_all_images = (
            str(task.get("profile", {}).get("high_value_policy", {}).get("image_scope"))
            == "all_images"
        )
        supplement_contract_args = [
            "--mode", "commit", "--db", str(db),
            "--migration", str(migrations["supplement"]),
            "--allowed-output-root", str(workspace),
            "--out", str(stages / "03_supplement_contract"),
            "--confirm-central-db-write",
        ]
        if repair_all_images:
            supplement_contract_args.extend(
                ["--selection-mode", "all-image-visual-units"]
            )
        else:
            supplement_contract_args.extend(
                [
                    "--selection-mode", "frozen-missing-images",
                    "--candidate-manifest",
                    str(candidate_out / "manifests/all_candidate_queue.jsonl"),
                ]
            )
        return [
            _stage(
                "repair_finder_tags", "补读现有图片的 Finder 标签",
                runtimes["system"], entry, Path(scripts["finder_tag_refresh"]),
                workspace, source,
                ["--db", str(db), "--out", str(stages / "01_finder_tag_refresh"),
                 "--confirm-central-db-write"], requires_source=True,
            ),
            _stage(
                "repair_candidate_dry_run", "按当前高价值策略只读重算缺失图片候选",
                runtimes["qwen"], entry, Path(scripts["candidate_select"]),
                workspace, source,
                ["--mode", "dry-run", "--db", str(db), "--out", str(candidate_out),
                 "--config", str(configs["candidate"]),
                 "--allowed-output-root", str(workspace),
                 "--high-value-mode", high_value_mode],
            ),
            _stage(
                "repair_supplement_contract", "建立缺失图片补充队列",
                runtimes["system"], entry, Path(scripts["supplement_contract"]),
                workspace, source,
                supplement_contract_args,
            ),
            _stage(
                "repair_supplement_qwen", "补充缺失图片的高价值画面描述（Qwen-VL）",
                runtimes["qwen"], entry, Path(scripts["supplement_qwen"]),
                workspace, source,
                ["--mode", "run", "--db", str(db), "--config", str(configs["qwen"]),
                 "--prompt", str(configs["qwen_prompt"]),
                 "--model", str(task["runtime"]["models"]["qwen"]),
                 "--out", str(stages / "04_supplement_qwen"),
                 "--workers", str(model_workers), "--max-tokens", "384",
                 "--max-attempts", "3", "--confirm-central-db-write"],
            ),
            _stage(
                "repair_evidence_merge", "合并新增图片描述与现有搜索证据",
                runtimes["system"], entry, Path(scripts["supplement_evidence_merge"]),
                workspace, source,
                ["--mode", "commit", "--db", str(db),
                 "--migration", str(migrations["evidence"]),
                 "--out", str(stages / "05_evidence_merge"),
                 "--confirm-central-db-write"],
            ),
            _stage(
                "repair_propagation", "重建相关视频帧的语义传播",
                runtimes["system"], entry, Path(scripts["propagation"]),
                workspace, source,
                ["--mode", "commit", "--db", str(db),
                 "--config", str(configs["propagation"]),
                 "--migration", str(migrations["propagation"]),
                 "--out", str(stages / "06_propagation"),
                 "--confirm-central-db-write"],
            ),
            _stage(
                "repair_embedding", "更新新增描述的文本搜索向量",
                runtimes["embedding"], entry, Path(scripts["embedding"]),
                workspace, source,
                ["--mode", "run", "--db", str(db),
                 "--contract-config", str(configs["embedding_contract"]),
                 "--runtime-config", str(configs["embedding_runtime"]),
                 "--migration", str(migrations["embedding"]),
                 "--out", str(stages / "07_embedding"),
                 "--workers", str(embedding_workers), "--max-attempts", "3",
                 "--device", "auto", "--confirm-central-db-write"],
            ),
        ]
    if task_mode == "rebuild_search":
        return [
            {
                "key": "rebuild_search_index",
                "name": "从已有数据库重建搜索入口",
                "command": [
                    str(runtimes["system"]), str(scripts["search_rebuild"]),
                    "--db", str(db), "--out", str(stages / "01_search_rebuild"),
                    "--allowed-output-root", str(workspace),
                    "--confirm-central-db-write",
                ],
            },
        ]
    if task_mode not in {"full", "incremental", "repair"}:
        raise ValueError(f"unsupported_task_mode:{task_mode}")

    # Incremental maintenance deliberately uses the same durable database as
    # the selected library.  The source scan refreshes only the current file
    # snapshot; all downstream workers are idempotent and claim only rows
    # without a valid result for the current stage.  Do not clear candidate
    # queues here: doing so would turn a one-file incremental run into a full
    # Qwen requeue.
    maintenance = task_mode in {"incremental", "repair"}
    incremental = task_mode == "incremental"
    mode_prefix = "增量" if incremental else ("修复缺失" if task_mode == "repair" else "")

    plan = [
        _stage("scan", f"{mode_prefix}扫描并建立素材清单", runtimes["visual"], entry,
               Path(scripts["source_scan"]),
               workspace, source,
               [str(source), "--out", str(stages / "01_scan"), "--db", str(db),
                "--scan-mac-tags", "--hash-all", "--no-open"], requires_source=True),
        _stage("image_preview", f"{mode_prefix}生成图片预览", runtimes["visual"], entry,
               Path(scripts["image_preview"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "02_image_preview"),
                "--sips-concurrency", str(frame_workers), "--ql-concurrency", str(max(1, min(2, frame_workers))),
                "--limit-new", "0", "--run-phase", "auto", "--no-open"], requires_source=True),
        _stage("video_frames", f"{mode_prefix}视频抽帧（每 {frame_interval} 秒一帧）", runtimes["visual"], entry,
               Path(scripts["video_frames"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "03_video_frames"),
                "--frame-interval-seconds", str(frame_interval),
                "--limit-new", "0", "--concurrency", str(frame_workers), "--run-phase", "auto", "--no-open"],
               requires_source=True),
        {
            "key": "visual_schema_v3", "name": "初始化视觉分析数据库结构",
            "command": [
                str(runtimes["visual"]), str(scripts["prepare_visual_schema"]),
                "--db", str(db), "--out", str(stages / "04_visual_schema"),
                "--allowed-output-root", str(workspace),
            ],
        },
        _stage("yoloe", "识别画面物体", runtimes["yolo"], entry,
               Path(scripts["yoloe"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "05_yoloe"),
                "--model", required_runtime_path(models, "yoloe", "models"),
                "--mobileclip", required_runtime_path(models, "yoloe_mobileclip", "models"),
                "--registry", str(configs["yoloe_registry"]),
                *(["--include-b-extended"] if bool(
                    task.get("profile", {}).get("yoloe_keywords", {}).get("enable_b_extended", False)
                ) else []),
                "--device", "mps", "--limit", "0", "--concurrency", str(model_workers)]),
        _stage("openclip", "建立全量视觉向量", runtimes["visual"], entry,
               Path(scripts["openclip"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "06_openclip"),
                "--model", required_runtime_path(models, "openclip", "models"),
                "--workers", str(max(1, min(3, model_workers))), "--device", "auto", "--limit", "0"]),
        _stage("dedup", "建立来源与画面去重关系", runtimes["visual"], entry,
               Path(scripts["dedup"]),
               workspace, source,
               ["--db", str(db), "--output-root", str(stages / "07_dedup"),
                "--mode", "commit", "--max-workers", str(max(1, frame_workers)),
                "--decode-backend", "pillow", "--force-commit-review"]),
        _stage(
            "person_reid_optional_v1", "归并可见人脸为待确认人物组（InsightFace）",
            runtimes.get("person_reid", runtimes["yolo"]), entry,
            Path(scripts["person_reid"]), workspace, source,
            [
                "--mode", "run", "--db", str(db),
                "--config", str(configs["person_reid"]),
                "--migration", str(migrations["person_reid"]),
                "--out", str(stages / "07b_person_reid_optional_v1"),
                "--allowed-output-root", str(workspace),
                "--workers", str(max(1, person_reid_workers)),
                "--max-attempts", "3",
                "--confirm-central-db-write",
            ],
        ),
        {
            "key": "candidate_schema", "name": "初始化候选队列数据库结构",
            "command": [
                str(runtimes["system"]), str(scripts["candidate_snapshot"]),
                "--mode", "prepare-ledger", "--db", str(db), "--project-root", str(project),
                "--allowed-output-root", str(workspace), "--out", str(stages / "08_candidate_schema"),
            ],
        },
        {
            "key": "candidates_generic_v2", "name": "选择高价值与文字候选",
            "command": [
                str(runtimes["qwen"]),
                str(scripts["candidate_select"]),
                "--mode", "commit", "--db", str(db),
                "--out", str(stages / "09_candidates_generic_v2"),
                "--config", str(configs["candidate"]),
                "--allowed-output-root", str(workspace),
                "--high-value-mode", high_value_mode,
                *( [] if maintenance else ["--clear-existing-candidate-items"] ),
            ],
        },
        {
            "key": "candidate_snapshot", "name": "冻结当前素材库候选执行快照",
            "command": [
                str(runtimes["system"]), str(scripts["candidate_snapshot"]),
                "--mode", "commit", "--db", str(db), "--project-root", str(project),
                "--allowed-output-root", str(workspace), "--out", str(stages / "10_candidate_snapshot"),
            ],
        },
        optional_stage("qwen_optional_v2", "生成高价值画面描述", "qwen", stages / "11_qwen_optional_v2",
            _stage("inner", "inner", runtimes["qwen"], entry,
                Path(scripts["qwen"]), workspace, source,
                ["--mode", "run", "--db", str(db),
                 "--config", str(configs["qwen"]),
                 "--prompt", str(configs["qwen_prompt"]),
                 "--model", str(task["runtime"]["models"]["qwen"]),
                 "--qwen-python", str(runtimes["qwen"]), "--out", str(stages / "11_qwen_optional_v2"),
                 "--workers", str(model_workers), "--max-tokens", "384", "--max-attempts", "3",
                 "--confirm-central-db-write"])),
        optional_stage("ocr_optional_v2", "识别画面文字", "ocr", stages / "12_ocr_optional_v2",
            _stage("inner", "inner", runtimes["ocr"], entry,
                Path(scripts["ocr"]), workspace, source,
                ["--mode", "run", "--run-kind", "full", "--limit", "0",
                 "--workers", str(ocr_workers), "--max-attempts", "3", "--db", str(db),
                 "--config", str(configs["ocr"]),
                 "--migration", str(migrations["ocr"]),
                 "--out", str(stages / "12_ocr_optional_v2"), "--confirm-central-db-write"]),
            run_delegate_on_empty=True),
        optional_stage("evidence_optional_v2", "合并内容描述与文字证据", "evidence", stages / "13_evidence_optional_v2",
            _stage("inner", "inner", runtimes["system"], entry,
                Path(scripts["evidence"]), workspace, source,
                ["--mode", "commit", "--db", str(db),
                 "--config", str(configs["evidence"]),
                 "--migration", str(migrations["evidence"]),
                 "--out", str(stages / "13_evidence_optional_v2"), "--confirm-central-db-write"])),
        optional_stage("propagation_optional_v2", "向相邻派生帧传播相关语义", "propagation", stages / "14_propagation_optional_v2",
            _stage("inner", "inner", runtimes["system"], entry,
                Path(scripts["propagation"]), workspace, source,
                ["--mode", "commit", "--db", str(db),
                 "--config", str(configs["propagation"]),
                 "--migration", str(migrations["propagation"]),
                 "--out", str(stages / "14_propagation_optional_v2"), "--confirm-central-db-write"])),
        optional_stage("embedding_optional_v2", "生成文本搜索向量", "embedding", stages / "15_embedding_optional_v2",
            _stage("inner", "inner", runtimes["embedding"], entry,
                Path(scripts["embedding"]), workspace, source,
                ["--mode", "run", "--db", str(db),
                 "--contract-config", str(configs["embedding_contract"]),
                 "--runtime-config", str(configs["embedding_runtime"]),
                 "--migration", str(migrations["embedding"]),
                 "--out", str(stages / "15_embedding_optional_v2"), "--workers", str(embedding_workers),
                 "--max-attempts", "3", "--device", "auto", "--confirm-central-db-write"])),
    ]
    if str(task.get("profile", {}).get("high_value_policy", {}).get("image_scope")) == "all_images":
        all_image_contract = _stage(
            "all_image_supplement_contract", "建立全部图片补充分析队列",
            runtimes["system"], entry, Path(scripts["supplement_contract"]),
            workspace, source,
            ["--mode", "commit", "--selection-mode", "all-image-visual-units",
             "--db", str(db), "--migration", str(migrations["supplement"]),
             "--allowed-output-root", str(workspace),
             "--out", str(stages / "11a_all_image_supplement_contract"),
             "--confirm-central-db-write"],
        )
        all_image_qwen = _stage(
            "all_image_supplement_qwen", "生成其余图片的画面描述（Qwen-VL）",
            runtimes["qwen"], entry, Path(scripts["supplement_qwen"]),
            workspace, source,
            ["--mode", "run", "--db", str(db), "--config", str(configs["qwen"]),
             "--prompt", str(configs["qwen_prompt"]),
             "--model", str(task["runtime"]["models"]["qwen"]),
             "--out", str(stages / "11b_all_image_supplement_qwen"),
             "--workers", str(model_workers), "--max-tokens", "384",
             "--max-attempts", "3", "--confirm-central-db-write"],
        )
        all_image_merge = _stage(
            "all_image_evidence_merge", "合并全部图片描述与现有搜索证据",
            runtimes["system"], entry, Path(scripts["supplement_evidence_merge"]),
            workspace, source,
            ["--mode", "commit", "--db", str(db),
             "--migration", str(migrations["evidence"]),
             "--out", str(stages / "13a_all_image_evidence_merge"),
             "--confirm-central-db-write"],
        )

        def insert_after(key: str, stage: dict[str, Any]) -> None:
            index = next(i for i, row in enumerate(plan) if row["key"] == key)
            plan.insert(index + 1, stage)

        insert_after("candidate_snapshot", all_image_contract)
        insert_after("qwen_optional_v2", all_image_qwen)
        insert_after("evidence_optional_v2", all_image_merge)
    if task_mode == "full" and audio_runtime_available:
        video_stage_index = next(
            index for index, row in enumerate(plan) if row["key"] == "video_frames"
        )
        plan[video_stage_index]["command"].append("--coextract-audio")
        coextract_manifest = stages / "03_video_frames" / "audio_coextract_manifest.jsonl"
        plan.insert(
            video_stage_index + 1,
            audio_enrichment_stage(
                stages / "03b_audio_search_enrichment",
                preextracted_audio_manifest=coextract_manifest,
            ),
        )
    return plan


def initial_state(task: dict[str, Any], plan: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pipeline_contract": PIPELINE_CONTRACT,
        "task_id": task["task_id"],
        "task_name": task["name"],
        "status": "queued",
        "stage_count": len(plan),
        "completed_stage_count": 0,
        "current_stage_key": None,
        "current_stage_name": None,
        "current_child_pid": None,
        "worker_pid": os.getpid(),
        "started_at_epoch": time.time(),
        "updated_at_epoch": time.time(),
        "finished_at_epoch": None,
        "error": "",
        "failed_stage_key": None,
        "failed_stage_name": None,
        "error_summary": "",
        "error_details": "",
        "error_log_path": str(task.get("log_path") or ""),
        "stages": [
            {"key": row["key"], "name": row["name"], "status": "pending", "exit_code": None,
             "started_at_epoch": None, "finished_at_epoch": None, "elapsed_seconds": None,
             "error_summary": "", "error_details": "", "current_item": "",
             "live_completed": 0, "live_total": 0, "live_success": 0,
             "live_skipped": 0, "live_failed": 0, "eta_seconds": None,
             "eta_basis": "正在估算", "report_paths": {},
             "log_path": str(task.get("log_path") or "")}
            for row in plan
        ],
    }


def execute_pipeline(
    task_path: Path,
    *,
    plan: Optional[Sequence[dict[str, Any]]] = None,
    resume: bool = False,
) -> dict[str, Any]:
    task_path = task_path.expanduser().resolve(strict=True)
    task = load_json(task_path)
    workspace = Path(task["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "stages").mkdir(parents=True, exist_ok=True)
    database = Path(task["database"])
    task["task_path"] = str(task_path)
    library_task: dict[str, Any] | None = None
    library_task_text = str(task.get("library_task_path") or "").strip()
    if library_task_text:
        library_task_path = Path(library_task_text).expanduser().resolve(strict=True)
        library_task = load_json(library_task_path)
        library_task["task_path"] = str(library_task_path)
        ensure_central_schema(
            database,
            task=library_task,
            backup_dir=workspace / "backups" / "central_database_v2",
        )
    data_task_id = str(
        task.get("central_library_task_id")
        or (library_task or {}).get("task_id")
        or task["task_id"]
    )
    central_schema_report = ensure_central_schema(
        database,
        task=task,
        backup_dir=workspace / "backups" / "central_database_v2",
    )
    state_path = Path(task["state_path"])
    plan = list(plan or build_stage_plan(task))
    if resume and state_path.is_file():
        state = load_json(state_path)
        by_key = {row["key"]: row for row in state.get("stages", [])}
        state["status"] = "running"
        state["worker_pid"] = os.getpid()
        state["error"] = ""
        state["failed_stage_key"] = None
        state["failed_stage_name"] = None
        state["error_summary"] = ""
        state["error_details"] = ""
        state["error_log_path"] = str(task.get("log_path") or "")
        for row in plan:
            by_key.setdefault(row["key"], {
                "key": row["key"], "name": row["name"], "status": "pending", "exit_code": None,
                "started_at_epoch": None, "finished_at_epoch": None, "elapsed_seconds": None,
                "error_summary": "", "error_details": "",
                "log_path": str(task.get("log_path") or ""),
            })
        state["stages"] = [by_key[row["key"]] for row in plan]
        state["stage_count"] = len(plan)
        invalid_from: int | None = None
        for index, row in enumerate(plan):
            if by_key[row["key"]].get("status") != "success":
                continue
            if validate_stage_acceptance(task, row["key"]):
                invalid_from = index
                break
        if invalid_from is not None:
            for row in state["stages"][invalid_from:]:
                row.update({
                    "status": "pending", "exit_code": None, "reason_code": None,
                    "started_at_epoch": None, "finished_at_epoch": None, "elapsed_seconds": None,
                    "error_summary": "", "error_details": "",
                })
        state["completed_stage_count"] = sum(
            row.get("status") == "success" for row in state["stages"]
        )
    else:
        state = initial_state(task, plan)
    atomic_json(state_path, state)
    state["central_database"] = central_schema_report
    atomic_json(state_path, state)
    task_runtime = task.get("runtime", {})
    environment = offline_environment(
        workspace,
        task_runtime.get("tools"),
        model_root=runtime_model_root(task_runtime),
    )
    active_child: subprocess.Popen[Any] | None = None

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal active_child
        if active_child and active_child.poll() is None:
            try:
                os.killpg(active_child.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                active_child.terminate()
        state["status"] = "cancelled"
        state["error"] = "用户停止任务"
        state["updated_at_epoch"] = time.time()
        state["finished_at_epoch"] = time.time()
        atomic_json(state_path, state)
        task["status"] = "cancelled"
        task["finished_at_epoch"] = state["finished_at_epoch"]
        atomic_json(task_path, task)
        append_event(
            database, task_id=str(task["task_id"]), event_type="task_cancelled",
            severity="warning", message="用户停止任务",
        )
        update_search_state(
            database,
            task_id=data_task_id,
            status="DEGRADED" if library_task is not None else "FAILED",
            checks={
                "schema_compatible": True,
                "database_readable": database.is_file(),
                "required_tables_ready": library_task is not None,
                "search_preflight_ready": library_task is not None,
                "reason_code": "TASK_CANCELLED",
            },
        )
        raise SystemExit(143)

    old_term = signal.signal(signal.SIGTERM, terminate)
    old_int = signal.signal(signal.SIGINT, terminate)
    try:
        state["status"] = "running"
        atomic_json(state_path, state)
        append_event(
            database, task_id=str(task["task_id"]), event_type="task_started",
            payload={"resume": resume, "stage_count": len(plan)},
        )
        for index, stage in enumerate(plan):
            record = state["stages"][index]
            if resume and record.get("status") == "success":
                continue
            record.update({
                "status": "running", "started_at_epoch": time.time(), "exit_code": None,
                "error_summary": "", "error_details": "",
                "current_item": "", "live_completed": 0, "live_total": 0,
                "live_success": 0, "live_skipped": 0, "live_failed": 0,
                "bytes_processed": 0, "actual_workers": None,
                "ffmpeg_processes": None, "model_workers": None,
                "started_workers": None, "alive_workers": None,
                "active_workers": None, "idle_workers": None,
                "crashed_workers": 0, "restart_count": 0,
                "queue_pending": None, "queue_running": None,
                "output_files": 0,
                "eta_seconds": None, "eta_basis": "正在估算", "report_paths": {},
                "log_path": str(task.get("log_path") or ""),
            })
            command = (
                command_for_resume(stage, workspace)
                if resume else [str(value) for value in stage.get("command", [])]
            )
            stage = {**stage, "command": command}
            configured_workers = None
            for option in ("--workers", "--concurrency", "--max-workers"):
                indexes = [i for i, value in enumerate(command[:-1]) if value == option]
                if indexes:
                    try:
                        configured_workers = int(command[indexes[-1] + 1])
                    except ValueError:
                        configured_workers = None
            record["configured_workers"] = configured_workers
            stage_item_key = "__stage__"
            enqueue_work_items(
                database,
                task_id=str(task["task_id"]),
                stage_key=str(stage["key"]),
                items=[{"item_key": stage_item_key}],
                max_attempts=20,
            )
            stage_claim = claim_work_item(
                database,
                task_id=str(task["task_id"]),
                stage_key=str(stage["key"]),
                worker_id=f"pipeline-{os.getpid()}",
                lease_seconds=120,
            )
            lease_stop = threading.Event()
            lease_lost = threading.Event()
            lease_thread: threading.Thread | None = None
            if stage_claim:
                def pulse_stage_lease() -> None:
                    while not lease_stop.wait(10.0):
                        try:
                            if not heartbeat_work_item(
                                database,
                                work_item_id=str(stage_claim["work_item_id"]),
                                worker_id=f"pipeline-{os.getpid()}",
                                lease_seconds=120,
                            ):
                                lease_lost.set()
                                return
                        except (OSError, sqlite3.Error):
                            # One transient write-lock must not kill a healthy
                            # stage.  The 120-second lease permits later pulses.
                            continue
                lease_thread = threading.Thread(
                    target=pulse_stage_lease,
                    name=f"stage-lease-{stage['key']}",
                    daemon=True,
                )
                lease_thread.start()
            output_before = stage_output_dir(stage, workspace)
            storage_before = {
                "stage": tree_stats(output_before),
                "database": database_stats(Path(task["database"])),
            }
            state.update({
                "current_stage_key": stage["key"], "current_stage_name": stage["name"],
                "completed_stage_count": sum(row.get("status") == "success" for row in state["stages"]),
                "updated_at_epoch": time.time(),
            })
            atomic_json(state_path, state)
            append_event(
                database, task_id=str(task["task_id"]), stage_key=str(stage["key"]),
                event_type="stage_started", payload={
                    "name": stage["name"], "configured_workers": configured_workers,
                    "command": command,
                },
            )
            print(f"stage_start key={stage['key']} name={stage['name']}", flush=True)
            active_child = subprocess.Popen(
                command,
                cwd=str(task["runtime"]["project_root"]),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            state["current_child_pid"] = active_child.pid
            atomic_json(state_path, state)
            output_tail: deque[str] = deque(maxlen=STAGE_ERROR_TAIL_LINES)
            failure_rows: list[dict[str, Any]] = []
            skipped_rows: list[dict[str, Any]] = []
            last_progress_write = 0.0
            last_qwen_progress_read = 0.0
            if active_child.stdout is not None:
                for line in active_child.stdout:
                    output_tail.append(line.rstrip("\n"))
                    print(line, end="", flush=True)
                    progress = parse_progress_line(line)
                    if progress.get("qwen_run_id"):
                        now = time.monotonic()
                        if now - last_qwen_progress_read >= 1.0:
                            progress.update(qwen_database_progress(
                                Path(task["database"]),
                                stage_output_dir(stage, workspace),
                                str(progress["qwen_run_id"]),
                            ))
                            last_qwen_progress_read = now
                    if progress:
                        if progress.get("event") in {"stage_item_failed", "stage_failed"}:
                            record["reason_code"] = str(
                                progress.get("reason_code")
                                or progress.get("error_code")
                                or "STAGE_ITEM_FAILED"
                            )
                            record["error_summary"] = str(
                                progress.get("error_message")
                                or record["reason_code"]
                            )[:2000]
                        for source_key, target_key in (
                            ("completed", "live_completed"), ("total", "live_total"),
                            ("success", "live_success"), ("skipped", "live_skipped"),
                            ("failed", "live_failed"), ("remaining", "live_remaining"),
                            ("current_item", "current_item"),
                            ("bytes_processed", "bytes_processed"),
                            ("actual_workers", "actual_workers"),
                            ("ffmpeg_processes", "ffmpeg_processes"),
                            ("model_workers", "model_workers"),
                            ("started_workers", "started_workers"),
                            ("alive_workers", "alive_workers"),
                            ("active_workers", "active_workers"),
                            ("idle_workers", "idle_workers"),
                            ("crashed_workers", "crashed_workers"),
                            ("restart_count", "restart_count"),
                            ("queue_pending", "queue_pending"),
                            ("queue_running", "queue_running"),
                            ("output_files", "output_files"),
                        ):
                            if source_key in progress:
                                record[target_key] = progress[source_key]
                        elapsed = max(0.0, time.time() - float(record["started_at_epoch"]))
                        eta, basis = live_eta(
                            int(record.get("live_completed") or 0),
                            int(record.get("live_total") or 0),
                            elapsed,
                        )
                        record["eta_seconds"] = eta
                        record["eta_basis"] = basis
                        now = time.monotonic()
                        if now - last_progress_write >= 1.0:
                            state["updated_at_epoch"] = time.time()
                            atomic_json(state_path, state)
                            last_progress_write = now
                    if "skip" in line.lower():
                        skipped_rows.append({
                            "item": str(progress.get("current_item") or ""),
                            "reason": line.strip()[:2000],
                        })
                    if "failed" in line.lower() or "error=" in line.lower():
                        failure_rows.append({
                            "item": str(progress.get("current_item") or ""),
                            "reason": line.strip()[:2000],
                        })
                active_child.stdout.close()
            exit_code = active_child.wait()
            active_child = None
            lease_stop.set()
            if lease_thread is not None:
                lease_thread.join(timeout=2.0)
            if lease_lost.is_set() and exit_code == 0:
                exit_code = 4
                record["reason_code"] = "CENTRAL_STAGE_LEASE_LOST"
                output_tail.append(
                    "stage_acceptance_failed reason_code=CENTRAL_STAGE_LEASE_LOST"
                )
            acceptance_error = validate_stage_acceptance(task, stage["key"]) if exit_code == 0 else ""
            if acceptance_error:
                print(
                    f"stage_acceptance_failed key={stage['key']} reason_code={acceptance_error}",
                    flush=True,
                )
                exit_code = 3
                record["reason_code"] = acceptance_error
                output_tail.append(
                    f"stage_acceptance_failed key={stage['key']} reason_code={acceptance_error}"
                )
            error_summary = ""
            error_details = ""
            if exit_code != 0:
                error_summary, error_details = summarize_stage_failure(
                    list(output_tail), exit_code,
                )
            record.update({
                "status": "success" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "finished_at_epoch": time.time(),
                "elapsed_seconds": round(time.time() - float(record["started_at_epoch"]), 3),
                "error_summary": error_summary,
                "error_details": error_details,
            })
            record["report_paths"] = write_stage_reports(
                stage=stage,
                workspace=workspace,
                database=Path(task["database"]),
                record=record,
                before_storage=storage_before,
                failure_rows=failure_rows,
                skipped_rows=skipped_rows,
            )
            state["current_child_pid"] = None
            state["completed_stage_count"] = sum(row.get("status") == "success" for row in state["stages"])
            state["updated_at_epoch"] = time.time()
            atomic_json(state_path, state)
            if stage_claim:
                finish_work_item(
                    database,
                    work_item_id=str(stage_claim["work_item_id"]),
                    worker_id=f"pipeline-{os.getpid()}",
                    status="success" if exit_code == 0 else "failed",
                    error_code=str(record.get("reason_code") or error_summary),
                    output_payload={"report_paths": record["report_paths"]},
                )
            append_event(
                database, task_id=str(task["task_id"]), stage_key=str(stage["key"]),
                event_type="stage_finished" if exit_code == 0 else "stage_failed",
                severity="info" if exit_code == 0 else "error",
                message=error_summary,
                payload={"exit_code": exit_code, "elapsed_seconds": record["elapsed_seconds"]},
            )
            print(f"stage_end key={stage['key']} exit_code={exit_code}", flush=True)
            if stage["key"] == "scan" and exit_code == 0:
                state["strong_fingerprint"] = sync_original_files(
                    database, data_task_id
                )
                atomic_json(state_path, state)
            if exit_code != 0:
                state.update({
                    "status": "failed",
                    "error": (
                        f"阶段失败：{stage['name']}（{acceptance_error}）"
                        if acceptance_error else f"阶段失败：{stage['name']}（exit {exit_code}）"
                    ),
                    "failed_stage_key": stage["key"],
                    "failed_stage_name": stage["name"],
                    "error_summary": error_summary,
                    "error_details": error_details,
                    "error_log_path": str(task.get("log_path") or ""),
                    "finished_at_epoch": time.time(),
                })
                atomic_json(state_path, state)
                task["status"] = "failed"
                task["finished_at_epoch"] = state["finished_at_epoch"]
                task["error"] = state["error"]
                task["failed_stage_key"] = stage["key"]
                task["failed_stage_name"] = stage["name"]
                task["error_summary"] = error_summary
                task["error_details"] = error_details
                task["error_log_path"] = str(task.get("log_path") or "")
                atomic_json(task_path, task)
                state["final_report_paths"] = write_final_task_report(task, state)
                atomic_json(state_path, state)
                update_search_state(
                    database,
                    task_id=data_task_id,
                    status="DEGRADED" if library_task is not None else "FAILED",
                    checks={
                        "schema_compatible": True,
                        "database_readable": database.is_file(),
                        "required_tables_ready": library_task is not None,
                        "search_preflight_ready": library_task is not None,
                        "reason_code": str(record.get("reason_code") or "STAGE_FAILED"),
                    },
                )
                return state
        canonical_full_keys = {
            "scan", "image_preview", "video_frames", "visual_schema_v3",
            "yoloe", "openclip", "dedup", "person_reid_optional_v1",
            "candidate_schema", "candidates_generic_v2", "candidate_snapshot",
            "qwen_optional_v2", "all_image_supplement_contract",
            "all_image_supplement_qwen", "ocr_optional_v2",
            "evidence_optional_v2", "all_image_evidence_merge",
            "propagation_optional_v2", "embedding_optional_v2",
        }
        plan_keys = {str(row.get("key") or "") for row in plan}
        final_errors = (
            validate_final_pipeline_acceptance(task)
            if canonical_full_keys.issubset(plan_keys) else {}
        )
        if not final_errors:
            sync_original_files(database, data_task_id)
            sync_artifact_lineage(database, data_task_id)
            central_report = central_audit(database, data_task_id)
            state["central_database_audit"] = central_report
            if central_report["required_tables_missing"]:
                final_errors["central_tables"] = ",".join(
                    central_report["required_tables_missing"]
                )
            if central_report["lineage_missing_original"]:
                final_errors["lineage_missing_original"] = str(
                    central_report["lineage_missing_original"]
                )
            if task.get("strong_fingerprint_required") and not central_report[
                "strong_fingerprint_complete"
            ]:
                final_errors["strong_fingerprint"] = json.dumps(
                    central_report["fingerprint"], ensure_ascii=False, sort_keys=True
                )
        if final_errors:
            final_summary = "最终完整性检查未通过：" + "; ".join(
                f"{key}={value}" for key, value in sorted(final_errors.items())
            )
            state.update({
                "status": "failed", "current_stage_key": None,
                "current_stage_name": None, "current_child_pid": None,
                "completed_stage_count": len(plan), "updated_at_epoch": time.time(),
                "finished_at_epoch": time.time(), "error": final_summary,
                "failed_stage_key": "final_acceptance",
                "failed_stage_name": "最终完整性检查",
                "error_summary": final_summary,
                "error_details": json.dumps(final_errors, ensure_ascii=False, indent=2),
            })
            atomic_json(state_path, state)
            task.update({
                "status": "failed", "finished_at_epoch": state["finished_at_epoch"],
                "error": final_summary, "failed_stage_key": "final_acceptance",
                "failed_stage_name": "最终完整性检查",
                "error_summary": final_summary,
                "error_details": state["error_details"],
            })
            atomic_json(task_path, task)
            state["final_report_paths"] = write_final_task_report(task, state)
            atomic_json(state_path, state)
            update_search_state(
                database,
                task_id=data_task_id,
                status="DEGRADED" if library_task is not None else "FAILED",
                checks={
                    "schema_compatible": True,
                    "database_readable": database.is_file(),
                    "required_tables_ready": False,
                    "search_preflight_ready": False,
                    "reason_code": "FINAL_ACCEPTANCE_FAILED",
                    "failures": final_errors,
                },
            )
            return state
        state.update({
            "status": "success", "current_stage_key": None, "current_stage_name": None,
            "current_child_pid": None, "completed_stage_count": len(plan),
            "updated_at_epoch": time.time(), "finished_at_epoch": time.time(), "error": "",
            "failed_stage_key": None, "failed_stage_name": None,
            "error_summary": "", "error_details": "",
        })
        atomic_json(state_path, state)
        task["status"] = "success"
        task["finished_at_epoch"] = state["finished_at_epoch"]
        for key in (
            "error", "failed_stage_key", "failed_stage_name",
            "error_summary", "error_details", "error_log_path",
        ):
            task.pop(key, None)
        atomic_json(task_path, task)
        update_search_state(
            database,
            task_id=data_task_id,
            status="READY",
            checks={
                "schema_compatible": True,
                "database_readable": True,
                "required_tables_ready": True,
                "search_preflight_ready": True,
            },
        )
        append_event(
            database, task_id=str(task["task_id"]), event_type="task_succeeded",
            payload={"stage_count": len(plan)},
        )
        state["final_report_paths"] = write_final_task_report(task, state)
        atomic_json(state_path, state)
        return state
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def stop_pipeline(state_path: Path) -> dict[str, Any]:
    state_path = state_path.expanduser().resolve(strict=True)
    state = load_json(state_path)
    pid = int(state.get("worker_pid") or 0)
    if state.get("status") not in {"queued", "running"} or pid <= 1:
        return {"status": "PASS", "message": "当前没有可停止的任务"}
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"status": "PASS", "message": "任务进程已经退出，状态文件将在下次打开时保留"}
    return {"status": "PASS", "message": "已发送停止请求"}
