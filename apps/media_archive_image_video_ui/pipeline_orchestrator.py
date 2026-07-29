from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional, Sequence


PIPELINE_CONTRACT = "media_archive_image_video_pipeline_v1"
STAGE_ERROR_TAIL_LINES = 80
STAGE_ERROR_DETAILS_MAX_CHARS = 16 * 1024


def default_model_root() -> Path:
    configured = os.environ.get("MEDIA_ARCHIVE_MODEL_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / "Library/Application Support/素材大整理/Models").absolute()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_stage_failure(lines: Sequence[str], exit_code: int) -> tuple[str, str]:
    cleaned = [line.strip() for line in lines if line.strip()]
    details = "\n".join(cleaned[-STAGE_ERROR_TAIL_LINES:])[-STAGE_ERROR_DETAILS_MAX_CHARS:]
    preferred_markers = (
        "python_mismatch:", "stage_acceptance_failed", "reason_code=",
        "Traceback (most recent call last):", "RuntimeError:", "ValueError:",
        "ModuleNotFoundError:", "FileNotFoundError:", "PermissionError:",
        "missing_required_", "output_outside_",
    )
    for marker in preferred_markers:
        for line in reversed(cleaned):
            if marker in line:
                return line.strip(' \t",'), details
    if cleaned:
        return cleaned[-1][:1000], details
    return f"子进程退出码 {exit_code}，没有产生可读取的错误输出", details


def offline_environment(workspace: Path, tools: Optional[dict[str, str]] = None) -> dict[str, str]:
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
    return environment


def validate_stage_acceptance(task: dict[str, Any], stage_key: str) -> str:
    """Reject a zero-output stage when the database proves work existed."""
    db = Path(task["database"])
    if not db.is_file():
        return "DATABASE_MISSING_AFTER_STAGE" if stage_key == "scan" else ""
    with sqlite3.connect(str(db), timeout=5.0) as con:
        con.execute("PRAGMA query_only=ON")
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


def _stage(
    key: str,
    name: str,
    python: Path,
    entry: Path,
    script: Path,
    allowed: Path,
    source: Path,
    arguments: Sequence[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "command": [
            str(python), str(entry), "--script", str(script),
            "--allowed-output-root", str(allowed),
            "--allowed-source-root", str(source), "--", *map(str, arguments),
        ],
    }


def build_stage_plan(task: dict[str, Any]) -> list[dict[str, Any]]:
    project = Path(task["runtime"]["project_root"])
    workspace = Path(task["workspace"])
    stages = Path(task.get("stage_output_root") or (workspace / "stages"))
    db = Path(task["database"])
    source = Path(task["source_root"])
    runtimes = {key: Path(value) for key, value in task["runtime"]["python"].items()}
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
        "source_lineage_restore": str(
            app_scripts / "restore_source_file_lineage_from_manifest_v1.py"
        ),
        "supplement_contract": str(app_scripts / "stop03_3_qwenvl_supplement_contract_v1.py"),
        "supplement_qwen": str(app_scripts / "stop03_3_qwenvl_supplement_orchestrator_v1.py"),
        "supplement_evidence_merge": str(app_scripts / "stop03_5b_merge_qwenvl_supplement_v1.py"),
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
    }
    configs.update(task["runtime"].get("configs") or {})
    migrations = {
        "person_reid": str(project / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"),
        "ocr": str(project / "migrations/20260716_stop03_4_ocr_db_v1.sql"),
        "evidence": str(project / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql"),
        "propagation": str(project / "migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql"),
        "embedding": str(project / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"),
        "supplement": str(project / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql"),
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
    if task_mode == "repair":
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
                 "--confirm-central-db-write"],
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
        image_preview_out = stages / "02_image_preview"
        return [
            _stage(
                "rebuild_scan", "重新扫描素材位置并对账当前清单",
                runtimes["visual"], entry, Path(scripts["source_scan"]),
                workspace, source,
                [str(source), "--out", str(stages / "01_scan"), "--db", str(db),
                 "--scan-mac-tags", "--no-open"],
            ),
            _stage(
                "rebuild_image_preview", "重新识别图片与延时摄影分组",
                runtimes["visual"], entry, Path(scripts["image_preview"]),
                workspace, source,
                ["--db", str(db), "--out", str(image_preview_out),
                 "--sips-concurrency", str(frame_workers),
                 "--ql-concurrency", str(max(1, min(2, frame_workers))),
                 "--limit-new", "0", "--run-phase", "auto", "--no-open"],
            ),
            {
                "key": "rebuild_restore_lineage",
                "name": "恢复重扫前的历史文件引用",
                "command": [
                    str(runtimes["system"]), str(scripts["source_lineage_restore"]),
                    "--db", str(db),
                    "--manifest-root", str(workspace / "stages" / "01_scan"),
                    "--out", str(stages / "03_lineage_restore"),
                    "--allowed-output-root", str(workspace),
                    "--confirm-central-db-write",
                ],
            },
            {
                "key": "rebuild_visual_schema",
                "name": "用当前分组替换旧的特殊素材入口",
                "command": [
                    str(runtimes["visual"]), str(scripts["prepare_visual_schema"]),
                    "--db", str(db), "--out", str(stages / "04_visual_schema"),
                    "--allowed-output-root", str(workspace),
                    "--timelapse-manifest",
                    str(image_preview_out / "manifests/image_preview_visual_unit_manifest.csv"),
                ],
            },
            _stage(
                "rebuild_openclip", "补齐新增画面的视觉向量（OpenCLIP）",
                runtimes["visual"], entry, Path(scripts["openclip"]),
                workspace, source,
                ["--db", str(db), "--out", str(stages / "05_openclip_incremental"),
                 "--model", str(
                     models.get("openclip")
                     or default_model_root() / "openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors"
                 ),
                 "--workers", str(max(1, min(3, model_workers))),
                 "--device", "auto", "--limit", "0"],
            ),
        ]
    if task_mode != "full":
        raise ValueError(f"unsupported_task_mode:{task_mode}")

    plan = [
        _stage("scan", "扫描并建立素材清单", runtimes["visual"], entry,
               Path(scripts["source_scan"]),
               workspace, source,
               [str(source), "--out", str(stages / "01_scan"), "--db", str(db),
                "--scan-mac-tags", "--no-open"]),
        _stage("image_preview", "生成图片预览", runtimes["visual"], entry,
               Path(scripts["image_preview"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "02_image_preview"),
                "--sips-concurrency", str(frame_workers), "--ql-concurrency", str(max(1, min(2, frame_workers))),
                "--limit-new", "0", "--run-phase", "auto", "--no-open"]),
        _stage("video_frames", f"视频抽帧（每 {frame_interval} 秒一帧）", runtimes["visual"], entry,
               Path(scripts["video_frames"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "03_video_frames"),
                "--frame-interval-seconds", str(frame_interval),
                "--limit-new", "0", "--concurrency", str(frame_workers), "--run-phase", "auto", "--no-open"]),
        {
            "key": "visual_schema_v3", "name": "准备视觉分析数据库合同",
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
                "--model", str(models.get("yoloe") or default_model_root() / "yoloe26-l-seg/weights/yoloe26-l-seg.pt"),
                "--mobileclip", str(models.get("yoloe_mobileclip") or default_model_root() / "yoloe26-l-seg/mobileclip2_b.ts"),
                "--device", "mps", "--limit", "0", "--concurrency", str(model_workers)]),
        _stage("openclip", "建立全量视觉向量", runtimes["visual"], entry,
               Path(scripts["openclip"]),
               workspace, source,
               ["--db", str(db), "--out", str(stages / "06_openclip"),
                "--model", str(models.get("openclip") or default_model_root() / "openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors"),
                "--workers", str(max(1, min(3, model_workers))), "--device", "auto", "--limit", "0"]),
        _stage("dedup", "建立来源与画面去重关系", runtimes["visual"], entry,
               Path(scripts["dedup"]),
               workspace, source,
               ["--db", str(db), "--output-root", str(stages / "07_dedup"),
                "--mode", "commit", "--max-workers", str(max(1, frame_workers)),
                "--decode-backend", "pillow", "--force-commit-review"]),
        _stage(
            "person_reid_optional_v1", "识别并归并同一人物（InsightFace）",
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
            "key": "candidate_schema", "name": "准备通用候选数据库合同",
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
                "--clear-existing-candidate-items",
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
             "error_summary": "", "error_details": "",
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
    environment = offline_environment(workspace, task.get("runtime", {}).get("tools"))
    active_child: subprocess.Popen[Any] | None = None

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal active_child
        if active_child and active_child.poll() is None:
            active_child.terminate()
        state["status"] = "cancelled"
        state["error"] = "用户停止任务"
        state["updated_at_epoch"] = time.time()
        state["finished_at_epoch"] = time.time()
        atomic_json(state_path, state)
        task["status"] = "cancelled"
        task["finished_at_epoch"] = state["finished_at_epoch"]
        atomic_json(task_path, task)
        raise SystemExit(143)

    old_term = signal.signal(signal.SIGTERM, terminate)
    old_int = signal.signal(signal.SIGINT, terminate)
    try:
        state["status"] = "running"
        atomic_json(state_path, state)
        for index, stage in enumerate(plan):
            record = state["stages"][index]
            if resume and record.get("status") == "success":
                continue
            record.update({
                "status": "running", "started_at_epoch": time.time(), "exit_code": None,
                "error_summary": "", "error_details": "",
                "log_path": str(task.get("log_path") or ""),
            })
            state.update({
                "current_stage_key": stage["key"], "current_stage_name": stage["name"],
                "completed_stage_count": sum(row.get("status") == "success" for row in state["stages"]),
                "updated_at_epoch": time.time(),
            })
            atomic_json(state_path, state)
            print(f"stage_start key={stage['key']} name={stage['name']}", flush=True)
            active_child = subprocess.Popen(
                list(stage["command"]),
                cwd=str(task["runtime"]["project_root"]),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            state["current_child_pid"] = active_child.pid
            atomic_json(state_path, state)
            output_tail: deque[str] = deque(maxlen=STAGE_ERROR_TAIL_LINES)
            if active_child.stdout is not None:
                for line in active_child.stdout:
                    output_tail.append(line.rstrip("\n"))
                    print(line, end="", flush=True)
                active_child.stdout.close()
            exit_code = active_child.wait()
            active_child = None
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
            state["current_child_pid"] = None
            state["completed_stage_count"] = sum(row.get("status") == "success" for row in state["stages"])
            state["updated_at_epoch"] = time.time()
            atomic_json(state_path, state)
            print(f"stage_end key={stage['key']} exit_code={exit_code}", flush=True)
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
