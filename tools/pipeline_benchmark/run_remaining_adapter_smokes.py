#!/usr/bin/env python3
"""Run the five remaining production Host smoke tests in one invocation.

Each case owns a fresh DB/output directory and is allowed to fail without
preventing the following case.  A smoke result is promoted to ADAPTER_READY
only when the formal process exits successfully and its benchmark artifacts
exist; no toy inference or CPU fallback is used.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sqlite3
from pathlib import Path

try:
    from formal_adapters import materialize_config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from formal_adapters import materialize_config


ROOT = Path(__file__).resolve().parents[2]
APP = Path("/Applications/本地数据库 1.2.8.app")
PIPELINE = APP / "Contents/Resources/Pipeline"
TEST_ROOT = Path(os.environ.get("PIPELINE_TEST_ROOT", "/Users/horizon/Documents/AI-Local/test-output"))
SMOKE_ROOT = TEST_ROOT / "calibration-adapter-smoke"
FIXTURE_ROOT = Path(os.environ.get("ADAPTER_FIXTURE_ROOT", "/Users/horizon/Documents/AI-Local/test-output/001dzl-pipeline-benchmark-20260929/fixture"))


def fresh_db(run_root: Path) -> Path:
    # OCR/Person require the frozen v25 visual queue; the minimal generic
    # fixture does not contain that view.  Keep each case isolated by copying
    # the appropriate immutable template, never the result of a prior run.
    case = run_root.parent.name
    # OCR and Stage03 need real, existing source media plus frozen candidate
    # rows.  The visual-only fixture intentionally points at derived frames
    # (and its video path is not a playable source), so it is not a valid
    # source-stage fixture.
    handoff_template = Path(
        "/Users/horizon/Documents/AI-Local/test-output/"
        "local-database-stage-handoff-sample-v1/library/tasks/"
        "20260728_133201_1.1.2阶段接力小样本/workspace/media_archive.sqlite"
    )
    ocr_template = Path(
        "/Users/horizon/Documents/AI-Local/test-output/"
        "stop03_3g_local_db_fullflow_20260711_191408/database/"
        "media_archive_stop03_3g_validation.sqlite"
    )
    ffmpeg_template = Path(
        "/Users/horizon/Documents/AI-Local/test-output/"
        "001dzl-pipeline-benchmark-20260929/fixture-10video-r2.sqlite"
    )
    default_name = (
        "fixture-yolo/media_archive.sqlite" if case == "person-reid"
        else "fixture-embedding/media_archive.sqlite" if case == "embedding"
        else "fixture/media_archive.sqlite"
    )
    if case == "ocr" and "ADAPTER_FIXTURE_DB" not in os.environ:
        source = ocr_template
    elif case == "ffmpeg" and "ADAPTER_FIXTURE_DB" not in os.environ:
        source = ffmpeg_template
    elif case == "embedding" and "ADAPTER_FIXTURE_DB" not in os.environ:
        source = Path(
            "/Users/horizon/Documents/AI-Local/test-output/"
            "local-database-stage-handoff-sample-v1/library/tasks/"
            "20260728_133201_1.1.2阶段接力小样本/workspace/media_archive.sqlite"
        )
    else:
        source = Path(os.environ.get("ADAPTER_FIXTURE_DB", str(FIXTURE_ROOT.parent / default_name)))
    if not source.is_file():
        raise FileNotFoundError(f"fresh_fixture_template_missing:{source}")
    target = run_root / "fixture" / "media_archive.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    case = run_root.parent.name
    migrations = {
        "whisper": [PIPELINE / "migrations/20260809_audio_speech_search_v1.sql"],
        "ocr": [PIPELINE / "migrations/20260716_stop03_4_ocr_db_v1.sql"],
        "embedding": [PIPELINE / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql", PIPELINE / "migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql", PIPELINE / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"],
        "person-reid": [PIPELINE / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"],
    }.get(case, [])
    if migrations:
        with sqlite3.connect(target) as con:
            for migration in migrations:
                con.executescript(migration.read_text(encoding="utf-8"))
            con.commit()
    if case == "embedding":
        # The template contains valid upstream 5b/5c success evidence, but its
        # old 5d run must not contaminate a fresh embedding smoke.  Clear only
        # the copied 5d result tables; upstream evidence remains read-only and
        # available to the contract builder.
        with sqlite3.connect(target) as con:
            for table in ("stop03_5d_document_vector_links", "stop03_5d_text_vectors",
                          "stop03_5d_text_documents", "stop03_5d_text_embedding_runs"):
                if con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                    con.execute(f"delete from {table}")
            con.commit()
    if case == "ocr":
        # This immutable validation template already contains OCR-trigger
        # candidates and valid derived files. Clear only copied run history.
        with sqlite3.connect(target) as con:
            for table in ("stop03_4_ocr_results", "stop03_4_ocr_run_items", "stop03_4_ocr_attempts", "stop03_4_ocr_runs"):
                if con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                    con.execute(f"delete from {table}")
            con.commit()
    if case == "whisper":
        # The generic benchmark DB normally points at the offline 001DZL
        # source volume.  Audio smoke needs a local, read-only fixture media
        # file; use the existing generated control clip and rewrite only the
        # fresh clone's lineage row.
        fixture_media = Path(os.environ.get(
            "ADAPTER_WHISPER_MEDIA",
            "/Users/horizon/Documents/AI-Local/test-output/media-archive-v0.1/test-fixtures/valid-control.mp4",
        ))
        if not fixture_media.is_file():
            raise FileNotFoundError(f"whisper_fixture_media_missing:{fixture_media}")
        with sqlite3.connect(target) as con:
            columns = {row[1] for row in con.execute("pragma table_info(source_assets)")}
            updates = {"absolute_path": str(fixture_media), "file_name": fixture_media.name,
                       "extension": fixture_media.suffix, "media_type": "video",
                       "size_bytes": fixture_media.stat().st_size, "is_deleted_or_missing": 0}
            fields = [key for key in updates if key in columns]
            assignments = ", ".join(f"{key}=?" for key in fields)
            con.execute(f"update source_assets set {assignments}", [updates[key] for key in fields])
            con.commit()
    return target


def validate_whisper_fixture(db: Path) -> None:
    """Reject an audio smoke fixture before starting the formal stage.

    The production audio entry point deliberately refuses an offline source
    volume.  Treat that as a fixture error, rather than allowing a short-lived
    process to be reported as a model/Metal failure.
    """
    with sqlite3.connect(db) as con:
        cols = {row[1] for row in con.execute("pragma table_info(source_assets)")}
        if "absolute_path" not in cols:
            raise RuntimeError("whisper_fixture_missing_absolute_path")
        paths = [row[0] for row in con.execute("select absolute_path from source_assets where absolute_path is not null")]
    if not paths:
        raise RuntimeError("whisper_fixture_no_source_assets")
    missing = [path for path in paths if not Path(path).exists()]
    if missing:
        raise RuntimeError(f"SOURCE_VOLUME_OFFLINE:{missing[0]}")


def wrapper(runtime: str, entrypoint: Path, *, db: Path, out: Path, args: list[str]) -> list[str]:
    return [str(APP / "Contents/Helpers/PipelinePython" / runtime),
            str(PIPELINE / "scripts/04_media_archive_app/run_generic_pipeline_stage.py"),
            "--script", str(entrypoint), "--allowed-output-root", str(TEST_ROOT), "--",
            *args, "--db", str(db), "--out", str(out)]


def commands(case: str, run_root: Path, db: Path) -> tuple[list[str], dict[str, str]]:
    out = run_root / "stage"
    env = {"MEDIA_ARCHIVE_MODEL_ROOT": "/Users/horizon/Documents/model", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if case == "whisper":
        validate_whisper_fixture(db)
        script = PIPELINE / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py"
        # The formal audio stage requires every dependency explicitly; paths
        # are resolved here, never substituted with a different backend.
        args = ["--out", str(out), "--migration", str(PIPELINE / "migrations/20260809_audio_speech_search_v1.sql"),
                "--audio-python", str(APP / "Contents/Helpers/PipelinePython/whisper"),
                "--embedding-python", str(APP / "Contents/Helpers/PipelinePython/embedding"),
                "--audio-pilot-script", str(PIPELINE / "scripts/04_media_archive_app/run_audio_search_pilot_v1.py"),
                "--embedding-script", str(PIPELINE / "scripts/04_media_archive_app/run_audio_embedding_commit_v1.py"),
                "--ffmpeg", "/opt/homebrew/bin/ffmpeg", "--ffprobe", "/opt/homebrew/bin/ffprobe",
                "--silero-root", "/Users/horizon/Documents/model/silero-vad", "--whisper-model", "/Users/horizon/Documents/model/whisper-large-v3-turbo",
                "--deep-filter-executable", str(PIPELINE / "tools/deep-filter/deep-filter"), "--deep-filter-model", "/Users/horizon/Documents/model/DeepFilterNet3_onnx.tar.gz",
                "--embedding-model", "/Users/horizon/Documents/model/Qwen3-Embedding-0.6B", "--workers", "1", "--limit", "3", "--confirm-central-db-write"]
        return [str(APP / "Contents/Helpers/PipelinePython/whisper"), str(script), *args, "--db", str(db)], env
    if case == "ocr":
        src = PIPELINE / "configs/stop03_4_ocr_db_v1.json"; cfg = materialize_config(src, run_root / "config.json", task_runtime=run_root)
        script = PIPELINE / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py"
        migration = PIPELINE / "migrations/20260716_stop03_4_ocr_db_v1.sql"
        return wrapper("ocr", script, db=db, out=out, args=["--mode", "run", "--config", str(cfg), "--migration", str(migration), "--workers", "1", "--limit", "3", "--max-attempts", "1", "--confirm-central-db-write"]), env
    if case == "embedding":
        contract = materialize_config(PIPELINE / "configs/stop03_5d_text_embedding_db_contract_v1.json", run_root / "contract.json", task_runtime=run_root)
        runtime = materialize_config(PIPELINE / "configs/stop03_5d_text_embedding_db_orchestrator_v1.json", run_root / "runtime.json", task_runtime=run_root)
        # The packaged contract names a development-only virtualenv
        # (media-archive-embedding).  The installed 1.2.8 app exposes the
        # production embedding runtime as a single helper executable.  Resolve
        # the benchmark copy to that executable without touching the packaged
        # config or model directory.
        contract_data = json.loads(contract.read_text(encoding="utf-8"))
        contract_data["python_path"] = str(APP / "Contents/Helpers/PipelinePython/embedding")
        contract.write_text(json.dumps(contract_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        migration = PIPELINE / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"
        script = PIPELINE / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py"
        args = ["--mode", "run", "--contract-config", str(contract), "--runtime-config", str(runtime), "--migration", str(migration), "--workers", "1", "--max-attempts", "1", "--confirm-central-db-write"]
        return wrapper("embedding", script, db=db, out=out, args=args), env
    if case == "person-reid":
        cfg = materialize_config(PIPELINE / "configs/stop03_1c_person_reid_db_v1.json", run_root / "config.json", task_runtime=run_root)
        migration = PIPELINE / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
        script = PIPELINE / "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py"
        args = ["--mode", "run", "--config", str(cfg), "--migration", str(migration), "--out", str(out), "--allowed-output-root", str(TEST_ROOT), "--workers", "1", "--max-attempts", "1", "--limit", "3", "--backend", "opencv", "--confirm-central-db-write"]
        return wrapper("person_reid", script, db=db, out=out, args=args), env
    if case == "ffmpeg":
        script = PIPELINE / "scripts/04_media_archive_app/step02_video_frame_generic_interval_v1.py"
        # Stage03's formal adapter receives interval=3; all video task
        # parallelism belongs to the coordinator, never ffmpeg -threads.
        args = ["--frame-interval-seconds", "3", "--db", str(db), "--out", str(out), "--limit-new", "1", "--concurrency", "1", "--no-open"]
        # The generic stage wrapper injects TEST_OUTPUT_ROOT and the frozen
        # output-path policy.  Directly invoking the frozen script bypasses
        # that policy and falsely rejects a valid benchmark sibling directory.
        # Keep enough independent videos in the queue for x1..x5 scaling.
        # The coordinator rewrites --concurrency for each campaign case.
        return wrapper("system", script, db=db, out=out, args=["--frame-interval-seconds", "3", "--limit-new", "10", "--concurrency", "1", "--no-open"]), env
    raise ValueError(case)


def run_case(index: int, case: str) -> dict[str, object]:
    run_root = SMOKE_ROOT / case / "latest"
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        db = fresh_db(run_root)
        command, env_delta = commands(case, run_root, db)
        env = os.environ.copy(); env.update(env_delta)
        bench = [sys.executable, str(ROOT / "tools/pipeline_benchmark/run_benchmark.py"), "--name", f"{case}-x1-host-smoke", "--output", str(run_root), "--interval", "1", "--timeout", "3600", "--", *command]
        log = (run_root / "smoke.stdout.log").open("w", encoding="utf-8")
        try:
            completed = subprocess.run(bench, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        finally:
            log.close()
        status = "PASS" if completed.returncode == 0 else "HOST_SMOKE_FAIL"
        error_text = ""
        for candidate in (run_root / "workload.stderr.log", run_root / "workload.stdout.log", run_root / "smoke.stdout.log"):
            if candidate.is_file():
                error_text += candidate.read_text(errors="replace")[-8000:]
        phase = "OUTPUT_CONTRACT" if completed.returncode == 0 else "RUNTIME_IMPORT"
        markers = {
            "SOURCE_VOLUME_OFFLINE": "FIXTURE_BUILD",
            "whisper_fixture_": "FIXTURE_BUILD",
            "missing_local_model_dir": "PREFLIGHT",
            "frozen_ocr_queue_view_missing": "FIXTURE_BUILD",
            "output_path_outside_allowed_roots": "PREFLIGHT",
            "stop03_5d_python_path_missing": "PREFLIGHT",
            "FileNotFoundError": "PREFLIGHT",
            "unrecognized arguments": "COMMAND_CONSTRUCTION",
            "audio_pilot.py": "COMMAND_CONSTRUCTION",
            "output_and_allowed_root_required": "COMMAND_CONSTRUCTION",
            "unsupported operand type": "RUNTIME_IMPORT",
            "no eligible": "FIXTURE_BUILD",
        }
        for marker, marker_phase in markers.items():
            if marker in error_text:
                phase = marker_phase
                break
        result = {"status": status, "workload": case, "exit_code": completed.returncode, "command": bench, "output": str(run_root), "failure_phase": None if status == "PASS" else phase, "stderr_root_error": None if status == "PASS" else error_text[-2000:]}
    except Exception as exc:
        message = str(exc)
        phase = "FIXTURE_BUILD" if any(token in message for token in ("fixture", "SOURCE_VOLUME_OFFLINE")) else "COMMAND_CONSTRUCTION"
        result = {"status": "HOST_SMOKE_FAIL", "workload": case, "reason_code": type(exc).__name__, "error": message, "failure_phase": phase}
    smoke = SMOKE_ROOT / case / "smoke.json"; smoke.parent.mkdir(parents=True, exist_ok=True)
    smoke.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[{index}] {case} {result['status']}", flush=True)
    return result


def main() -> int:
    all_cases = ["whisper", "ocr", "embedding", "ffmpeg", "person-reid"]
    # Do not rerun adapters whose host smoke has already promoted them to
    # ADAPTER_READY.  This keeps the repair command focused and prevents
    # regressions in a previously verified adapter.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from run_full_pipeline_calibration import readiness
        ready = {str(row["id"]) for row in readiness() if row.get("execution_readiness") == "ADAPTER_READY"}
    except Exception:
        ready = set()
    cases = [case for case in all_cases if case not in ready]
    results = [run_case(i, case) for i, case in enumerate(cases, 1)]
    (SMOKE_ROOT / "remaining-smokes.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    check = subprocess.run([sys.executable, str(ROOT / "tools/pipeline_benchmark/run_full_pipeline_calibration.py"), "--check-execution"], cwd=ROOT, check=False)
    return 0 if all(r.get("status") == "PASS" for r in results) and check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
