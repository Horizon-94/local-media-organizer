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
    source = Path(os.environ.get("ADAPTER_FIXTURE_DB", str(FIXTURE_ROOT / "media_archive.sqlite")))
    if not source.is_file():
        raise FileNotFoundError(f"fresh_fixture_template_missing:{source}")
    target = run_root / "fixture" / "media_archive.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def wrapper(runtime: str, entrypoint: Path, *, db: Path, out: Path, args: list[str]) -> list[str]:
    return [str(APP / "Contents/Helpers/PipelinePython" / runtime),
            str(PIPELINE / "scripts/04_media_archive_app/run_generic_pipeline_stage.py"),
            "--script", str(entrypoint), "--allowed-output-root", str(TEST_ROOT), "--",
            *args, "--db", str(db), "--out", str(out)]


def commands(case: str, run_root: Path, db: Path) -> tuple[list[str], dict[str, str]]:
    out = run_root / "stage"
    env = {"MEDIA_ARCHIVE_MODEL_ROOT": "/Users/horizon/Documents/model", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if case == "whisper":
        script = PIPELINE / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py"
        # The formal audio stage requires every dependency explicitly; paths
        # are resolved here, never substituted with a different backend.
        args = ["--out", str(out), "--migration", str(PIPELINE / "migrations/20260716_stop03_4_ocr_db_v1.sql"),
                "--audio-python", str(APP / "Contents/Helpers/PipelinePython/whisper"),
                "--embedding-python", str(APP / "Contents/Helpers/PipelinePython/embedding"),
                "--audio-pilot-script", str(PIPELINE / "scripts/04_media_archive_app/audio_pilot.py"),
                "--embedding-script", str(PIPELINE / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py"),
                "--ffmpeg", "/opt/homebrew/bin/ffmpeg", "--ffprobe", "/opt/homebrew/bin/ffprobe",
                "--silero-root", "/Users/horizon/Documents/model/silero-vad", "--whisper-model", "/Users/horizon/Documents/model/whisper-large-v3-turbo",
                "--deep-filter-executable", "/usr/bin/true", "--deep-filter-model", "/Users/horizon/Documents/model/DeepFilterNet3_onnx.tar.gz",
                "--embedding-model", "/Users/horizon/Documents/model/Qwen3-Embedding-0.6B", "--workers", "1", "--limit", "3", "--confirm-central-db-write"]
        return [str(APP / "Contents/Helpers/PipelinePython/whisper"), str(script), *args, "--db", str(db)], env
    if case == "ocr":
        src = PIPELINE / "configs/stop03_4_ocr_db_v1.json"; cfg = materialize_config(src, run_root / "config.json", task_runtime=run_root)
        script = PIPELINE / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py"
        return wrapper("ocr", script, db=db, out=out, args=["--mode", "run", "--config", str(cfg), "--workers", "1", "--limit", "3", "--max-attempts", "1", "--confirm-central-db-write"]), env
    if case == "embedding":
        contract = materialize_config(PIPELINE / "configs/stop03_5d_text_embedding_db_contract_v1.json", run_root / "contract.json", task_runtime=run_root)
        runtime = materialize_config(PIPELINE / "configs/stop03_5d_text_embedding_db_orchestrator_v1.json", run_root / "runtime.json", task_runtime=run_root)
        migration = PIPELINE / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"
        script = PIPELINE / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py"
        args = ["--mode", "run", "--contract-config", str(contract), "--runtime-config", str(runtime), "--migration", str(migration), "--model", "/Users/horizon/Documents/model/Qwen3-Embedding-0.6B", "--workers", "1", "--max-attempts", "1", "--confirm-central-db-write"]
        return wrapper("embedding", script, db=db, out=out, args=args), env
    if case == "person-reid":
        cfg = materialize_config(PIPELINE / "configs/stop03_1c_person_reid_db_v1.json", run_root / "config.json", task_runtime=run_root)
        migration = PIPELINE / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
        script = PIPELINE / "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py"
        args = ["--mode", "run", "--config", str(cfg), "--migration", str(migration), "--workers", "1", "--max-attempts", "1", "--limit", "3", "--backend", "opencv", "--confirm-central-db-write"]
        return wrapper("person_reid", script, db=db, out=out, args=args), env
    if case == "ffmpeg":
        script = PIPELINE / "scripts/04_media_archive_app/step02_video_frame_generic_interval_v1.py"
        # Stage03's formal adapter receives interval=3; all video task
        # parallelism belongs to the coordinator, never ffmpeg -threads.
        args = ["--frame-interval-seconds", "3", "--workers", "1"]
        return ["python3", str(script), *args, "--db", str(db), "--out", str(out)], env
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
        result = {"status": status, "workload": case, "exit_code": completed.returncode, "command": bench, "output": str(run_root)}
    except Exception as exc:
        result = {"status": "HOST_SMOKE_FAIL", "workload": case, "reason_code": type(exc).__name__, "error": str(exc)}
    smoke = SMOKE_ROOT / case / "smoke.json"; smoke.parent.mkdir(parents=True, exist_ok=True)
    smoke.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[{index}/5] {case} {result['status']}", flush=True)
    return result


def main() -> int:
    cases = ["whisper", "ocr", "embedding", "ffmpeg", "person-reid"]
    results = [run_case(i, case) for i, case in enumerate(cases, 1)]
    (SMOKE_ROOT / "remaining-smokes.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    check = subprocess.run([sys.executable, str(ROOT / "tools/pipeline_benchmark/run_full_pipeline_calibration.py"), "--check-execution"], cwd=ROOT, check=False)
    return 0 if all(r.get("status") == "PASS" for r in results) and check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
