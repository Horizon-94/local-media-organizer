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


def campaign_plan() -> list[dict[str, object]]:
    # Qwen is the only heavy formal host command currently proven in this
    # repository.  Other entries remain explicit campaign rows so failures or
    # missing formal contracts are visible and resumable.
    rows: list[dict[str, object]] = []
    for mode, workers in (("cold", 1), ("resident", 1), ("homogeneous", 2), ("homogeneous", 3)):
        rows.append({"id": f"qwen-{mode}-x{workers}", "kind": "qwen", "mode": mode, "workers": workers})
    for name, kind in (("whisper-cold-x1", "whisper"), ("whisper-resident-x1", "whisper"), ("yoloe-x1", "yoloe"), ("openclip-x1", "openclip"), ("ocr-x1", "ocr"), ("embedding-x1", "embedding"), ("ffmpeg-stage03-x1", "ffmpeg"), ("person-reid-x1", "person-reid")):
        rows.append({"id": name, "kind": kind, "mode": "baseline", "workers": 1})
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
        {"id": "yoloe", "status": "ACTIVE", "runtime": "PipelinePython/yolo", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_yoloe_full_from_db_safe_v6_20260709_170200.py", "model": MODEL_ROOT / "yoloe26-l-seg", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host YOLOE adapter implemented; smoke required"},
        {"id": "openclip", "status": "ACTIVE", "runtime": "PipelinePython/visual", "entrypoint": pipeline / "scripts/03_stop03_visual_analysis/stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py", "model": MODEL_ROOT / "openclip-vit-b-32-laion2b-s34b-b79k", "fixture_builder": REPO / "tools/pipeline_benchmark/build_media_fixture.py", "adapter_impl": True, "adapter_reason": "formal host OpenCLIP adapter implemented; smoke required"},
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
    return {"status": status, "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "output": str(output), "fixture": str(fixture), "workers": row["workers"], "metrics": metrics, "note": "resident mode requires a true multi-batch idle harness; 1.2.8 formal entry currently exposes only one batch."}


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
        return {"status": status, "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "output": str(output), "fixture": str(db), "command": bench, "metrics": summary, "workers": row["workers"]}
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=TEST_ROOT / f"pipeline-v2-calibration-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--check", action="store_true", help="contract readiness only; never runs a model")
    ap.add_argument("--check-execution", action="store_true", help="strict contract + host adapter readiness; never runs a model")
    ap.add_argument("--allow-partial", action="store_true", help="allow campaign rows with missing adapters")
    ap.add_argument("--case", help="dispatch one exact campaign case for regression validation")
    args = ap.parse_args()
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
            result = run_composite(row, item_root)
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
