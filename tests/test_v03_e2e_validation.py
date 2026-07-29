from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from media_archive import app


TEST_ROOT = Path("/tmp/media_archive_v035_test")
SOURCE = TEST_ROOT / "source"
WORKSPACE = TEST_ROOT / "workspace"
ARTIFACT_MANIFEST = TEST_ROOT / "artifact_manifest.jsonl"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_file(path: Path, content: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def clean_root() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    WORKSPACE.mkdir(parents=True)


def create_initial_sample() -> None:
    write_file(SOURCE / "a/one.jpg", b"one", 1_700_000_000_000_000_001)
    write_file(SOURCE / "a/two.jpg", b"two", 1_700_000_000_000_000_002)
    write_file(SOURCE / "v/one.mov", b"video", 1_700_000_000_000_000_003)
    write_file(SOURCE / "a/readme.txt", b"readme", 1_700_000_000_000_000_004)
    (WORKSPACE / "previews").mkdir(parents=True)
    (WORKSPACE / "previews/one_existing.jpg").write_text("existing", encoding="utf-8")
    write_jsonl(
        ARTIFACT_MANIFEST,
        [
            {"source_relative_path": "a/one.jpg", "preview_path": "previews/one_existing.jpg"},
            {"source_relative_path": "a/two.jpg", "preview_path": "previews/two_missing.jpg"},
            {"source_relative_path": "v/one.mov", "frame_path": "frames/one_0001.jpg"},
        ],
    )


def source_snapshot() -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(SOURCE).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in SOURCE.rglob("*")
        if path.is_file()
    }


def run_e2e(*, fresh: bool = False, artifact_manifest: Path = ARTIFACT_MANIFEST) -> int:
    argv = [
        "v03-validate-e2e",
        "--source",
        str(SOURCE),
        "--workspace",
        str(WORKSPACE),
        "--artifact-manifest",
        str(artifact_manifest),
    ]
    if fresh:
        argv.append("--fresh")
    return app.main(argv)


def e2e_report() -> dict[str, object]:
    return read_json(WORKSPACE / "stages/v0.3/reports/v03_e2e_validation.json")


def assert_boundaries(report: dict[str, object]) -> None:
    boundary = report["boundary_checks"]
    assert boundary["source_modified_by_v03"] is False
    assert boundary["v02_outputs_modified"] is False
    assert boundary["a9t_rerun"] is False
    assert boundary["c4_rerun"] is False
    assert boundary["preview_generated"] is False
    assert boundary["video_frames_extracted"] is False
    assert boundary["model_loaded"] is False
    assert boundary["pending_tasks_executed"] is False
    assert boundary["missing_artifacts_repaired"] is False
    assert boundary["v04_entered"] is False
    assert boundary["v05_entered"] is False
    assert boundary["v06_entered"] is False
    assert boundary["forbidden_outputs_created"] == []


def assert_source_safety(report: dict[str, object]) -> None:
    safety = report["source_safety_checks"]
    assert safety["source_snapshot_before_count"] > 0
    assert safety["source_snapshot_after_count"] > 0
    assert safety["source_snapshot_before_count"] == safety["source_snapshot_after_count"]
    assert safety["source_modified_by_v03"] is False
    assert safety["source_added_by_v03"] == []
    assert safety["source_deleted_by_v03"] == []
    assert safety["source_stat_changed_by_v03"] == []
    assert safety["source_content_read"] is False
    assert safety["source_sha256_calculated"] is False
    assert safety["source_decoded"] is False


def assert_required_outputs(report: dict[str, object]) -> None:
    assert all(item["exists"] is True for item in report["required_outputs"])


def test_v03_e2e_fresh_unchanged_running_and_changed_flow(monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.3-5 must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)
    monkeypatch.setattr(app, "run_preview_images", fail_external_process)
    monkeypatch.setattr(app, "run_extract_video_frames", fail_external_process)
    monkeypatch.setattr(app, "run_build_unified_manifest", fail_external_process)
    monkeypatch.setattr(app, "build_v02", fail_external_process)
    monkeypatch.setattr(app, "validate_real_minimal_v02", fail_external_process)

    clean_root()
    create_initial_sample()
    before = source_snapshot()

    assert run_e2e(fresh=True) == 0
    fresh_report = e2e_report()
    assert source_snapshot() == before
    assert fresh_report["validation_status"] == "PASS"
    assert fresh_report["execution_order"] == [
        "v03-incremental-scan",
        "v03-mark-invalidations",
        "v03-build-resume-plan",
        "v03-plan-missing-artifacts",
        "v03-e2e-validation-report",
    ]
    assert fresh_report["counts"]["baseline_count"] >= 4
    assert fresh_report["counts"]["total_diff_records"] == fresh_report["counts"]["total_invalidation_records"]
    assert fresh_report["counts"]["total_task_state_records"] == fresh_report["counts"]["total_invalidation_records"]
    assert fresh_report["counts"]["total_resume_records"] == fresh_report["counts"]["total_task_state_records"]
    assert fresh_report["counts"]["present_artifact_count"] >= 1
    assert fresh_report["counts"]["missing_artifact_count"] >= 1
    assert fresh_report["contract_checks"]["running_count_zero"] is True
    assert fresh_report["contract_checks"]["repair_allowed_in_v03_4_all_false"] is True
    assert_boundaries(fresh_report)
    assert_source_safety(fresh_report)
    assert_required_outputs(fresh_report)

    assert run_e2e() == 0
    unchanged_report = e2e_report()
    assert unchanged_report["validation_status"] == "PASS"
    assert unchanged_report["counts"]["unchanged_count"] >= 4
    assert unchanged_report["counts"]["skipped_unchanged_count"] >= 1
    assert unchanged_report["contract_checks"]["skipped_unchanged_missing_detectable"] is True
    assert unchanged_report["counts"]["missing_artifact_count"] >= 1
    assert_boundaries(unchanged_report)
    assert_source_safety(unchanged_report)

    state_path = WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl"
    states = read_jsonl(state_path)
    states[0]["task_status"] = "running"
    states[0]["resume_action"] = "no_action"
    write_jsonl(state_path, states)
    assert run_e2e() == 0
    running_report = e2e_report()
    assert running_report["validation_status"] == "PASS"
    assert running_report["counts"]["pending_retry_count"] >= 1
    assert running_report["contract_checks"]["running_count_zero"] is True
    assert_source_safety(running_report)

    write_file(SOURCE / "a/new.jpg", b"new", 1_700_000_000_000_000_005)
    write_file(SOURCE / "a/one.jpg", b"one-modified", 1_700_000_000_000_000_006)
    (SOURCE / "a/two.jpg").unlink()
    assert run_e2e() == 0
    changed_report = e2e_report()
    assert changed_report["validation_status"] == "PASS"
    assert changed_report["counts"]["new_count"] >= 1
    assert changed_report["counts"]["modified_count"] >= 1
    assert changed_report["counts"]["deleted_count"] >= 1
    assert changed_report["counts"]["unchanged_count"] >= 1
    assert changed_report["counts"]["needs_invalidation_count"] >= 1
    assert changed_report["counts"]["new_source_pending_artifact_count"] >= 1
    assert changed_report["counts"]["source_deleted_reference_invalid_count"] >= 1
    assert changed_report["counts"]["invalidated_count"] >= 1
    assert changed_report["contract_checks"]["deleted_reference_not_expected"] is True
    assert changed_report["contract_checks"]["diff_to_invalidation_one_to_one"] is True
    assert changed_report["contract_checks"]["invalidation_to_task_state_one_to_one"] is True
    assert changed_report["contract_checks"]["task_state_to_resume_one_to_one"] is True
    assert changed_report["contract_checks"]["execution_allowed_in_v03_3_all_false"] is True
    assert changed_report["contract_checks"]["repair_allowed_in_v03_4_all_false"] is True
    assert_boundaries(changed_report)
    assert_source_safety(changed_report)


def test_v03_e2e_abnormal_input_does_not_pass():
    clean_root()
    create_initial_sample()
    missing_manifest = TEST_ROOT / "missing_artifact_manifest.jsonl"

    assert run_e2e(fresh=True, artifact_manifest=missing_manifest) == 1

    report = e2e_report()
    assert report["validation_status"] == "FAIL"
    assert report["failed_check_count"] > 0
    assert report["model_loaded"] is False
    assert_boundaries(report)
    assert_source_safety(report)
