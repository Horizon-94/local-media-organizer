#!/usr/bin/env python3
"""Checkpointed host-side Pipeline V2 calibration campaign.

This coordinator never guesses a workload command.  Qwen uses the verified
1.2.8 host entry; stages without a formal fixture/entry are recorded as
NOT_CONFIGURED rather than silently replaced by a different test.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
import shutil as which_module
from datetime import datetime, timezone
from pathlib import Path
try:
    from formal_adapters import AdapterSpec, adapter_contract_complete
except ModuleNotFoundError:  # loaded by a test/importlib from repository root
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from formal_adapters import AdapterSpec, adapter_contract_complete

try:
    from run_remaining_adapter_smokes import commands as smoke_commands, fresh_db as smoke_fresh_db, wrapper as smoke_wrapper
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_remaining_adapter_smokes import commands as smoke_commands, fresh_db as smoke_fresh_db, wrapper as smoke_wrapper


REPO = Path(__file__).resolve().parents[2]
TEST_ROOT = Path("/Users/horizon/Documents/AI-Local/test-output")
FIXTURE_SOURCE = TEST_ROOT / "qwen-host-fixture-v2"
PRISTINE_PREFIX = "qwen-host-fixture-pristine-template"
APP = Path("/Applications/本地数据库 1.2.8.app")
MODEL_ROOT = Path("/Users/horizon/Documents/model")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize_visual_fixture(db: Path) -> None:
    """Reset copied visual-stage history without touching the source template.

    The immutable fixture template may have been used by an earlier YOLOE or
    OpenCLIP run.  Those rows make OpenCLIP legitimately report
    ``blocked_empty_visual_units`` and can make a campaign case appear to
    pass without doing inference.  The campaign owns a fresh clone, so clear
    only run/result tables in that clone and retain the visual-unit inputs.
    """
    with sqlite3.connect(db) as con:
        for table in ("embeddings", "model_runs", "processing_errors"):
            if con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                con.execute(f"delete from {table}")
        con.commit()
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = con.execute("pragma foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise RuntimeError(f"visual_fixture_integrity_failed:{integrity}:foreign_keys={len(foreign_keys)}")


def formal_stage_result(kind: str, output: Path) -> tuple[bool, str | None]:
    """Require a real formal-stage report, not just a zero process exit."""
    reports = output / "stage" / "reports"
    if kind == "yoloe":
        report = reports / "stop03_yoloe_full_summary.json"
        if not report.is_file():
            return False, "formal_stage_report_missing"
        data = json.loads(report.read_text(encoding="utf-8"))
        counts = data.get("counts", {})
        if data.get("validation_status") != "PASS" or int(counts.get("processed_units", 0)) <= 0:
            return False, f"formal_stage_not_pass:{data.get('validation_status')}"
        return True, None
    if kind == "openclip":
        report = reports / "stop03_1b_openclip_db_safe_summary.json"
        if not report.is_file():
            return False, "formal_stage_report_missing"
        data = json.loads(report.read_text(encoding="utf-8"))
        if data.get("status") != "success" or int(data.get("input_visual_units", 0)) <= 0 or data.get("success_count") != data.get("input_visual_units") or data.get("embedding_dim") != 512:
            return False, f"formal_stage_not_pass:{data.get('status')}"
        return True, None
    if kind == "ocr":
        reports = sorted(reports.glob("*_summary.json"), key=lambda p: p.stat().st_mtime)
        if not reports:
            return False, "formal_stage_report_missing"
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
        if data.get("run_status") != "success":
            return False, f"formal_stage_not_pass:{data.get('run_status')}"
        candidates = int(data.get("candidate_count", 0) or 0)
        completed = sum(int(data.get(key, 0) or 0) for key in ("success_count", "no_text_count", "reused_count"))
        if candidates <= 0:
            return False, "formal_stage_empty_candidates"
        if completed <= 0 and int(data.get("result_count", 0) or 0) <= 0:
            return False, "formal_stage_no_ocr_results"
        return True, None
    if kind == "ffmpeg":
        queue_reports = sorted((output / "stage" / "reports").glob("*queue_summary.json"), key=lambda p: p.stat().st_mtime)
        if not queue_reports:
            return False, "formal_stage_report_missing"
        data = json.loads(queue_reports[-1].read_text(encoding="utf-8"))
        # A zero-exit Stage03 with an empty source queue is a harness/fixture
        # result, not a successful extraction benchmark.
        summary = data.get("step02_summary", data)
        queue_status = str(summary.get("queue_status", summary.get("status", ""))).lower()
        if queue_status in {"empty", "no_input", "no_eligible_input"} or (output / "stage" / "manifests" / "video_extract_c4s_step01_queue_report.csv").exists() and (output / "stage" / "manifests" / "video_extract_c4s_step01_queue_report.csv").read_text(encoding="utf-8", errors="replace").strip().lower() == "empty":
            return False, "formal_stage_empty_video_queue"
        frames_dir = output / "stage" / "derived" / "video_frames" / "c4s_id_incremental_jpg1280"
        if not any(frames_dir.rglob("*.jpg")):
            return False, "formal_stage_no_frames"
        if int(data.get("video_report_rows_this_run", 0)) != int(data.get("planned_new_job_count", -1)) or int(data.get("total_valid_jpg1280_count_in_report", 0)) <= 0:
            return False, "formal_stage_incomplete_extraction"
        return True, None
    report_names = {"embedding": "stage/reports/stop03_5d_full_summary.json",
                    "whisper": "stage/audio_enrichment_summary.json",
                    "person-reid": "stage/run_summary.json",
                    "qwen": "qwen-stage/reports/final_report.json"}
    if kind not in report_names:
        return False, "formal_success_parser_missing"
    path = output / report_names[kind]
    if not path.is_file():
        return False, "formal_stage_report_missing"
    d = json.loads(path.read_text())
    if kind == "qwen":
        counts = d.get("counts", {})
        ok = d.get("status") == "DYNAMIC_DB_FULL_PASS" and counts.get("total", 0) > 0 and counts.get("success") == counts.get("total")
    elif kind == "whisper":
        ok = d.get("status") == "SUCCESS" and d.get("source_video_count", 0) > 0 and d.get("transcript_segment_count", 0) > 0 and d.get("vector_count", 0) > 0
    elif kind == "embedding":
        ok = d.get("status") == "PASS" and d.get("document_count", 0) > 0 and d.get("success_count") == d.get("document_count") and d.get("vector_blobs_valid") is True
    else:
        ok = d.get("run_status") == "success" and d.get("visual_unit_count", 0) > 0 and d.get("failed_count", 0) == 0 and d.get("success_count", 0) + d.get("no_face_count", 0) > 0
    return (True, None) if ok else (False, "formal_stage_semantic_failure")


def campaign_plan() -> list[dict[str, object]]:
    # Qwen is the only heavy formal host command currently proven in this
    # repository.  Other entries remain explicit campaign rows so failures or
    # missing formal contracts are visible and resumable.
    rows: list[dict[str, object]] = []
    for mode, workers in (("cold", 1), ("resident", 1), ("homogeneous", 2), ("homogeneous", 3)):
        rows.append({"id": f"qwen-{mode}-x{workers}", "kind": "qwen", "mode": mode, "workers": workers})
    for name, kind in (("whisper-cold-x1", "whisper"), ("whisper-resident-x1", "whisper"), ("yoloe-x1", "yoloe"), ("openclip-x1", "openclip"), ("ocr-x1", "ocr"), ("embedding-x1", "embedding"), ("ffmpeg-stage03-x1", "ffmpeg"), ("person-reid-x1", "person-reid")):
        rows.append({"id": name, "kind": kind, "mode": "resident" if "resident" in name else "baseline", "workers": 1})
    for name in ("whisper-x2", "yoloe-x2", "yoloe-x3", "openclip-x2", "openclip-x3", "ocr-x2", "embedding-x2", "ffmpeg-stage03-x2", "ffmpeg-stage03-x3", "ffmpeg-stage03-x4", "ffmpeg-stage03-x5"):
        kind = "ffmpeg" if name.startswith("ffmpeg-") else name.split("-")[0]
        rows.append({"id": name, "kind": kind, "mode": "homogeneous", "workers": int(name.rsplit("x", 1)[1])})
    for name in ("ffmpeg+yoloe", "ffmpeg+openclip", "ffmpeg+ocr", "ffmpeg+qwen", "yoloe+openclip", "yoloe+ocr", "yoloe+qwen", "openclip+qwen", "ocr+qwen", "embedding+qwen", "qwen+whisper", "whisper+yoloe", "whisper+openclip", "qwen+ffmpeg+yoloe", "qwen+ffmpeg+ocr", "qwen+openclip+ocr", "whisper+ffmpeg+yoloe", "qwen+whisper+ffmpeg", "event-driven-mixed-soak", "hdd-validation"):
        rows.append({"id": name, "kind": "combination", "mode": "representative", "workers": 1})
    return rows


def workload_registry() -> list[dict[str, object]]:
    pipeline = APP / "Contents/Resources/Pipeline"
    return [
        {"id": "qwen", "status": "ACTIVE", "runtime": "PipelinePython/qwen", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py", "model": MODEL_ROOT / "Qwen3-VL-4B-Instruct-4bit", "fixture_builder": REPO / "tools/pipeline_benchmark/prepare_qwen_host_fixture.py", "adapter_impl": True, "adapter": "run_host_benchmark.sh + exact formal Qwen stage"},
        {"id": "whisper", "status": "ACTIVE", "runtime": "PipelinePython/whisper", "entrypoint": pipeline / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py", "model": MODEL_ROOT / "whisper-large-v3-turbo", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host audio adapter implemented; smoke required"},
        # These production entrypoints consume the concrete local weight file,
        # not the parent model directory.  Keeping the file-level contract in
        # the registry prevents the campaign coordinator from constructing a
        # command that passes preflight but fails during model initialization.
        {"id": "yoloe", "status": "ACTIVE", "runtime": "PipelinePython/yolo", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_yoloe_full_from_db_safe_v6_20260709_170200.py", "model": MODEL_ROOT / "yoloe26-l-seg" / "weights" / "yoloe26-l-seg.pt", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host YOLOE adapter implemented; smoke required"},
        {"id": "openclip", "status": "ACTIVE", "runtime": "PipelinePython/visual", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py", "model": MODEL_ROOT / "openclip-vit-b-32-laion2b-s34b-b79k" / "open_clip_model.safetensors", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host OpenCLIP adapter implemented; smoke required"},
        {"id": "ocr", "status": "ACTIVE", "runtime": "PipelinePython/ocr", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py", "model": MODEL_ROOT / "ocr/PP-OCRv6_medium_det", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host OCR adapter implemented; smoke required"},
        {"id": "embedding", "status": "ACTIVE", "runtime": "PipelinePython/embedding", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py", "model": MODEL_ROOT / "Qwen3-Embedding-0.6B", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host embedding adapter implemented; smoke required"},
        {"id": "ffmpeg", "status": "ACTIVE", "runtime": "host ffmpeg/VideoToolbox", "entrypoint": pipeline / "scripts/04_media_archive_app/step02_video_frame_generic_interval_v1.py", "model": Path(which_module.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"), "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal Stage03 host adapter implemented; smoke required"},
        {"id": "person-reid", "status": "ACTIVE", "runtime": "PipelinePython/person_reid", "entrypoint": pipeline / "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py", "model": MODEL_ROOT / "insightface/buffalo_l", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host Person/ReID adapter implemented; smoke required"},
        {"id": "silero-vad", "status": "OPTIONAL_NOT_ACTIVE", "runtime": "audio enrichment optional", "entrypoint": pipeline / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py", "model": MODEL_ROOT / "silero-vad", "fixture_builder": None, "reason": "not part of the current visual 19-stage default path"},
        {"id": "deepfilternet3", "status": "OPTIONAL_NOT_ACTIVE", "runtime": "audio enrichment optional", "entrypoint": pipeline / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py", "model": MODEL_ROOT / "DeepFilterNet3_onnx.tar.gz", "fixture_builder": None, "reason": "not part of the current visual 19-stage default path"},
    ]


def readiness() -> list[dict[str, object]]:
    result = []
    for item in workload_registry():
        if item["status"] == "OPTIONAL_NOT_ACTIVE":
            result.append({**item, "contract_ready": None, "execution_adapter_ready": None, "readiness": "OPTIONAL_NOT_ACTIVE"})
            continue
        missing = []
        if not Path(item["entrypoint"]).is_file():
            missing.append("entrypoint")
        if not Path(item["model"]).exists():
            missing.append("model_or_tool")
        if item.get("fixture_builder") and not Path(item["fixture_builder"]).is_file():
            missing.append("fixture_builder")
        contract_ready = not missing
        smoke = TEST_ROOT / "calibration-adapter-smoke" / str(item["id"]) / "smoke.json"
        adapter_implemented = bool(item.get("adapter_impl")) and contract_ready
        smoke_pass = False
        if smoke.is_file():
            try:
                smoke_pass = json.loads(smoke.read_text(encoding="utf-8")).get("status") == "PASS"
            except (OSError, ValueError, TypeError):
                smoke_pass = False
        adapter_ready = adapter_implemented and smoke_pass
        result.append({**item, "contract_ready": contract_ready, "execution_adapter_ready": adapter_ready,
                       "readiness": "CONTRACT_READY" if contract_ready else "CONTRACT_NOT_READY",
                       "execution_readiness": "ADAPTER_READY" if adapter_ready else ("ADAPTER_IMPLEMENTED_HOST_UNVERIFIED" if adapter_implemented else "ADAPTER_NOT_READY"),
                       "smoke_result": str(smoke) if smoke.is_file() else None,
                       "missing": missing})
    return result


def print_readiness(execution: bool = False) -> int:
    rows = readiness()
    print("CALIBRATION READINESS")
    for row in rows:
        if row["readiness"] == "OPTIONAL_NOT_ACTIVE":
            print(f"{row['id']:<18} OPTIONAL_NOT_ACTIVE")
        else:
            print(f"{row['id']:<18} {row['readiness']:<18} {row['execution_readiness']}")
    plan = campaign_plan()
    print(f"TOTAL TEST CASES = {len(plan)}")
    active_missing = [row["id"] for row in rows if row["readiness"] == "CONTRACT_NOT_READY"]
    adapter_missing = [row["id"] for row in rows if row.get("execution_readiness") != "ADAPTER_READY" and row["readiness"] != "OPTIONAL_NOT_ACTIVE"]
    print(f"ACTIVE_CONTRACT_NOT_READY = {len(active_missing)}")
    print(f"ACTIVE_ADAPTER_NOT_READY = {len(adapter_missing)}")
    if active_missing:
        print("CONTRACT_MISSING = " + ", ".join(active_missing))
    if adapter_missing:
        print("ADAPTER_MISSING = " + ", ".join(adapter_missing))
    return 0 if not active_missing and (not execution or not adapter_missing) else 2


def pristine_template() -> Path:
    """Return a clean, immutable-by-convention Qwen fixture template."""
    candidates = sorted(TEST_ROOT.glob(PRISTINE_PREFIX + "*"))
    for candidate in candidates:
        report = candidate / "fixture-preparation.json"
        db = candidate / "qwen_fixture.sqlite"
        if report.is_file() and db.is_file():
            try:
                value = json.loads(report.read_text(encoding="utf-8"))
                scrub = value.get("history_scrub", {}).get("after", {})
                if value.get("status") == "PASS" and all(int(v) == 0 for v in scrub.values()):
                    # The report is not sufficient evidence: older builders
                    # could leave queue rows behind while claiming the run
                    # history was scrubbed.  Verify the exact table used by
                    # the formal Qwen execution contract before reusing it.
                    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
                        count = con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_run_items").fetchone()[0]
                    if int(count) == 0:
                        return candidate
            except (OSError, ValueError, TypeError):
                pass
    suffix = len(candidates) + 1
    target = TEST_ROOT / f"{PRISTINE_PREFIX}-v{suffix}"
    command = ["python3", str(REPO / "tools/pipeline_benchmark/prepare_qwen_host_fixture.py"),
               "--source-db", str(FIXTURE_SOURCE / "qwen_fixture.sqlite"),
               "--source-derived-dir", str(FIXTURE_SOURCE / "derived"),
               "--target-root", str(target),
               "--config", str(FIXTURE_SOURCE / "qwen-config.json")]
    completed = subprocess.run(command, cwd=REPO, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError("HARNESS_FIXTURE_CONTAMINATED: pristine fixture preparation failed\n" + completed.stdout + completed.stderr)
    return target


def fresh_qwen_fixture(run_root: Path) -> tuple[Path, Path]:
    fixture = run_root / "fixture" / "qwen_fixture.sqlite"
    config = run_root / "fixture" / "qwen-config.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    template = pristine_template()
    shutil.copy2(template / "qwen_fixture.sqlite", fixture)
    shutil.copy2(template / "qwen-config.json", config)
    # Fail closed if a template was modified or contaminated after creation.
    con = sqlite3.connect(f"file:{fixture}?mode=ro", uri=True)
    try:
        count = con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_run_items").fetchone()[0]
    finally:
        con.close()
    if count:
        raise RuntimeError("HARNESS_FIXTURE_CONTAMINATED: planned Qwen fixture contains execution history")
    return fixture, config


def run_qwen(row: dict[str, object], run_root: Path) -> dict[str, object]:
    if row.get("mode") == "resident":
        return {"status": "NOT_SUPPORTED_YET", "reason_code": "production_entry_has_no_multi_batch_resident_contract", "workers": row["workers"]}
    try:
        fixture, config = fresh_qwen_fixture(run_root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"status": "HARNESS_FAIL", "reason_code": "HARNESS_FIXTURE_CONTAMINATED", "error": str(exc), "workers": row["workers"]}
    output = run_root / "output"
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"RUN_HEAVY": "1", "QWEN_DB": str(fixture), "QWEN_CONFIG": str(config), "QWEN_WORKERS": str(row["workers"])})
    command = [str(REPO / "tools/pipeline_benchmark/run_host_benchmark.sh"), str(output)]
    started = time.time()
    completed = subprocess.run(command, cwd=REPO, env=env, check=False)
    status = "PASS" if completed.returncode == 0 else "MODEL_FAIL"
    reason_code = None
    if status == "PASS":
        ok, reason_code = formal_stage_result("qwen", output)
        if not ok:
            status = "HARNESS_FAIL"
    if completed.returncode != 0 and row.get("mode") == "resident":
        status = "NOT_SUPPORTED_YET"
    metrics = {}
    for candidate in (output / "qwen-x1" / "summary.json", output / "summary.json"):
        if candidate.is_file():
            try:
                metrics = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                metrics = {}
            break
    return {"status": status, "reason_code": reason_code, "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "output": str(output), "fixture": str(fixture), "workers": row["workers"], "metrics": metrics}


def run_formal_adapter(row: dict[str, object], run_root: Path) -> dict[str, object]:
    """Dispatch a non-Qwen case through the already smoke-validated adapter."""
    kind = str(row["kind"])
    if row.get("mode") == "resident":
        return {"status": "NOT_SUPPORTED_YET", "reason_code": "production_entry_has_no_multi_batch_resident_contract", "workers": row["workers"]}
    adapter_root = run_root / "adapter" / kind / "latest"
    # Frozen production wrappers accept output only below TEST_ROOT.  Keep
    # coordinator state anywhere the caller requested, but place the actual
    # workload artifacts under the same safe root used by smoke adapters.
    try:
        adapter_root.resolve().relative_to(TEST_ROOT.resolve())
    except ValueError:
        adapter_root = TEST_ROOT / "pipeline-v2-dispatch" / run_root.parent.name / run_root.name / kind / "latest"
    adapter_root.mkdir(parents=True, exist_ok=True)
    try:
        if kind in {"whisper", "ocr", "embedding", "ffmpeg", "person-reid"}:
            db = smoke_fresh_db(adapter_root)
            command, env_delta = smoke_commands(kind, adapter_root, db)
        elif kind in {"yoloe", "openclip"}:
            db = adapter_root / "fixture" / "media_archive.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            template = TEST_ROOT / "001dzl-pipeline-benchmark-20260929" / "fixture-yolo" / "media_archive.sqlite"
            shutil.copy2(template, db)
            sanitize_visual_fixture(db)
            out = adapter_root / "stage"
            spec = next(item for item in workload_registry() if item["id"] == kind)
            runtime = APP / "Contents/Helpers/PipelinePython" / ("yolo" if kind == "yoloe" else "visual")
            args = (["--model", str(spec["model"]), "--device", "mps", "--limit", "5", "--concurrency", str(row["workers"])] if kind == "yoloe" else ["--model", str(spec["model"]), "--model-name", "ViT-B-32", "--device", "mps", "--workers", str(row["workers"]), "--limit", "5"])
            command = smoke_wrapper(str(runtime.name), Path(spec["entrypoint"]), db=db, out=out, args=args)
            env_delta = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "MEDIA_ARCHIVE_MODEL_ROOT": str(MODEL_ROOT)}
        else:
            return {"status": "HARNESS_FAIL", "reason_code": "unsupported_campaign_adapter", "workers": row["workers"]}
        # Smoke adapters default to x1.  Homogeneous campaign rows must alter
        # the actual production pool argument, not merely the case label.
        workers = str(row.get("workers", 1))
        for flag in ("--workers", "--concurrency"):
            for pos in range(len(command) - 1):
                if command[pos] == flag:
                    command[pos + 1] = workers
        output = adapter_root
        bench = ["python3", str(REPO / "tools/pipeline_benchmark/run_benchmark.py"), "--name", str(row["id"]), "--output", str(output), "--interval", "1", "--timeout", "3600", "--", *command]
        env = os.environ.copy(); env.update(env_delta)
        started = time.time()
        completed = subprocess.run(bench, cwd=REPO, env=env, check=False)
        summary_path = output / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        status = "PASS" if completed.returncode == 0 else "MODEL_FAIL"
        reason_code = None
        if status == "PASS":
            ok, reason_code = formal_stage_result(kind, output)
            if not ok:
                status = "HARNESS_FAIL" if reason_code in {
                    "formal_stage_empty_candidates",
                    "formal_stage_no_ocr_results",
                    "formal_stage_empty_video_queue",
                    "formal_stage_no_frames",
                } else "MODEL_FAIL"
        result = {"status": status, "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "output": str(output), "fixture": str(db), "command": bench, "metrics": summary, "workers": row["workers"]}
        if reason_code:
            result["reason_code"] = reason_code
        return result
    except Exception as exc:
        return {"status": "HARNESS_FAIL", "reason_code": type(exc).__name__, "error": str(exc), "workers": row["workers"]}


def run_composite(row: dict[str, object], run_root: Path) -> dict[str, object]:
    """Run constituent formal adapters concurrently under one campaign case."""
    name = str(row["id"])
    if name in {"event-driven-mixed-soak", "hdd-validation"}:
        return {"status": "SKIPPED", "reason_code": "composite_harness_not_yet_implemented", "workers": 1}
    kinds = name.split("+")
    rows = [{"id": f"{name}:{kind}", "kind": kind, "mode": "baseline", "workers": 1} for kind in kinds]
    with ThreadPoolExecutor(max_workers=len(rows), thread_name_prefix="campaign-composite") as pool:
        futures = [pool.submit(run_qwen if item["kind"] == "qwen" else run_formal_adapter, item, run_root / item["kind"]) for item in rows]
        results = [future.result() for future in futures]
    statuses = [str(result.get("status")) for result in results]
    status = "PASS" if all(value == "PASS" for value in statuses) else "MODEL_FAIL"
    if any(value == "HARNESS_FAIL" for value in statuses):
        status = "HARNESS_FAIL"
    return {"status": status, "constituents": results, "workers": 1}


def run_monitored_composite(row: dict[str, object], run_root: Path) -> dict[str, object]:
    """Sample the shared coordinator PID and descendants on one clock."""
    if row["id"] in {"event-driven-mixed-soak", "hdd-validation"}:
        return run_composite(row, run_root)
    command = ["python3", str(REPO / "tools/pipeline_benchmark/run_benchmark.py"),
               "--name", str(row["id"]), "--output", str(run_root / "aggregate"),
               "--timeout", "7200", "--", "python3", str(Path(__file__).resolve()),
               "--internal-composite", str(row["id"]), "--root", str(run_root)]
    completed = subprocess.run(command, cwd=REPO, check=False)
    path = run_root / "composite-result.json"
    result = json.loads(path.read_text()) if path.exists() else {"status": "HARNESS_FAIL", "reason_code": "composite_result_missing"}
    metrics_path = run_root / "aggregate" / "summary.json"
    result["metrics"] = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
    result["exit_code"] = completed.returncode
    result["elapsed_seconds"] = result["metrics"].get("workload_elapsed_seconds")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=TEST_ROOT / f"pipeline-v2-calibration-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--check", action="store_true", help="contract readiness only; never runs a model")
    ap.add_argument("--check-execution", action="store_true", help="strict contract + host adapter readiness; never runs a model")
    ap.add_argument("--allow-partial", action="store_true", help="allow campaign rows with missing adapters")
    ap.add_argument("--case", help="dispatch one exact campaign case for regression validation")
    ap.add_argument("--internal-composite", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.internal_composite:
        row = next(r for r in campaign_plan() if r["id"] == args.internal_composite)
        result = run_composite(row, args.root)
        write_json(args.root / "composite-result.json", result)
        return 0 if result.get("status") == "PASS" else 1
    if args.check or args.check_execution:
        return print_readiness(execution=args.check_execution)
    missing = [row["id"] for row in readiness() if row["readiness"] == "CONTRACT_NOT_READY"]
    adapter_missing = [row["id"] for row in readiness() if row.get("execution_readiness") != "ADAPTER_READY" and row["readiness"] != "OPTIONAL_NOT_ACTIVE"]
    if (missing or adapter_missing) and not args.allow_partial:
        print("FULL CAMPAIGN REFUSED: execution readiness incomplete: " + ", ".join(missing + adapter_missing))
        print("Run --check-execution for details, or explicitly use --allow-partial.")
        return 2
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "calibration-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"campaign": "pipeline-v2-resource-calibration-v1", "started_at": utc_now(), "results": {}}
    results: dict[str, object] = state.setdefault("results", {})
    plan = [row for row in campaign_plan() if not args.case or row["id"] == args.case]
    if args.case and not plan:
        print(f"UNKNOWN_CASE: {args.case}")
        return 2
    for index, row in enumerate(plan, 1):
        item_id = str(row["id"])
        previous = results.get(item_id)
        if isinstance(previous, dict) and previous.get("status") in {"PASS", "MODEL_FAIL", "HARNESS_FAIL", "UNSAFE", "BORDERLINE", "CONTRACT_NOT_READY", "NOT_SUPPORTED_YET", "SKIPPED_DUE_TO_UNSAFE_PARENT"}:
            continue
        item_root = root / "runs" / item_id
        print(f"[{index:02d}/{len(plan):02d}] {item_id} RUNNING", flush=True)
        if row["kind"] == "qwen":
            result = run_qwen(row, item_root)
        elif row["kind"] != "combination":
            result = run_formal_adapter(row, item_root)
        else:
            result = run_monitored_composite(row, item_root)
        result.update({"id": item_id, "kind": row["kind"], "mode": row["mode"], "finished_at": utc_now()})
        results[item_id] = result
        write_json(state_path, state)
        metrics = result.get("metrics") if isinstance(result, dict) else {}
        print(f"[{index:02d}/{len(plan):02d}] {item_id} {result.get('status')} elapsed={result.get('elapsed_seconds', 'N/A')} rss_peak={metrics.get('peak_process_tree_rss_bytes', 'N/A') if isinstance(metrics, dict) else 'N/A'}", flush=True)
    summary = {"campaign": state["campaign"], "finished_at": utc_now(), "tested": len(results), "passed": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "PASS"), "failed": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") in {"MODEL_FAIL", "HARNESS_FAIL", "UNSAFE"}), "unresolved": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "ADAPTER_UNRESOLVED"), "results": results}
    write_json(root / "calibration-results.json", summary)
    lines = ["# Pipeline V2 calibration campaign", "", f"- campaign: `{summary['campaign']}`", f"- tested rows: `{summary['tested']}`", f"- pass: `{summary['passed']}`", f"- fail: `{summary['failed']}`", f"- unresolved adapters: `{summary['unresolved']}`", "", "Readiness and execution are separate: a registered production contract can be READY while its host execution adapter remains unresolved."]
    (root / "calibration-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "calibration-summary.md")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
