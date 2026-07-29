from __future__ import annotations

import json
import shutil
from pathlib import Path

from media_archive import app
from media_archive.v04 import baseline_30gb
from media_archive.v04.preflight_30gb import SnapshotResult, empty_inventory


TEST_ROOT = Path("/tmp/media_archive_v044_test")
SOURCE = TEST_ROOT / "MEDIA_ARCHIVE_TEST_SOURCE"
WORKSPACE = TEST_ROOT / "workspace"


def clean_root() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)


def write_file(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def create_fixture(source: Path = SOURCE) -> None:
    write_file(source / "a/one.jpg", b"image")
    write_file(source / "v/one.mov", b"video")
    write_file(source / "a/test.wav", b"audio")
    write_file(source / "raw/A001.braw", b"braw")
    write_file(source / "raw/C001.CRM", b"crm")
    write_file(source / "raw/G001.GPR", b"gpr")
    write_file(source / "a/readme.xyz", b"other")
    write_file(source / "a/empty.mp4", b"")


def run_cli(
    *,
    source: Path = SOURCE,
    workspace: Path = WORKSPACE,
    fresh: bool = True,
    allow_test_backends: bool = True,
    preview_backend: str | None = "test_copy_jpg",
    video_runner: str | None = "fake_ffmpeg_jpg",
    min_total_bytes: int = 1,
    extra: list[str] | None = None,
) -> int:
    argv = [
        "v04-validate-30gb-baseline",
        "--source",
        str(source),
        "--workspace",
        str(workspace),
        "--min-total-bytes",
        str(min_total_bytes),
        "--max-total-bytes",
        "1000000000",
        "--min-file-count",
        "1",
        "--telemetry-sample-interval-seconds",
        "2",
        "--required-free-ratio",
        "2.0",
        "--inventory-timeout-seconds",
        "30",
    ]
    if fresh:
        argv.append("--fresh")
    if allow_test_backends:
        argv.append("--allow-test-backends-for-tests")
    if preview_backend is not None:
        argv += ["--preview-backend", preview_backend]
    if video_runner is not None:
        argv += ["--video-runner", video_runner]
    if extra:
        argv.extend(extra)
    return app.main(argv)


def read_report(workspace: Path = WORKSPACE) -> dict[str, object]:
    return json.loads((workspace / "stages/v0.4/reports/v04_30gb_baseline_report.json").read_text(encoding="utf-8"))


def test_v04_30gb_baseline_small_fixture_passes_with_test_backends():
    clean_root()
    create_fixture()

    assert run_cli() == 0

    report = read_report()
    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["real_30gb_run"] is False
    assert report["test_backend_used"] is True
    assert report["production_backend_used"] is False
    for phase in ["inline_preflight", "v01_scan", "v02_build", "v03_e2e", "v04_contract_check", "v04_4_report"]:
        assert report["phase_results"][phase]["status"] == "PASS"
    assert report["source_safety_checks"]["source_modified_by_v04_4"] is False
    assert report["source_safety_checks"]["source_safety_status"] == "PASS"
    assert report["forbidden_output_checks"]["forbidden_outputs_created"] == []
    assert report["model_loaded"] is False
    assert report["semantic_search_used"] is False
    assert report["adaptive_scheduler_enabled"] is False
    assert report["workers_changed_by_this_run"] is False
    assert report["scheduler_decisions_written"] is False
    assert report["background_monitor_started"] is False
    assert report["v02_result"]["v02_used_test_copy_jpg"] is True
    assert report["v02_result"]["v02_used_fake_ffmpeg_jpg"] is True


def test_inline_preflight_fail_stops_later_stages():
    clean_root()
    wrong_source = TEST_ROOT / "Wrong_Source"
    create_fixture(wrong_source)

    assert run_cli(source=wrong_source) == 1

    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["phase_results"]["inline_preflight"]["status"] == "FAIL"
    assert report["phase_results"]["v01_scan"]["status"] == "not_run"
    assert report["phase_results"]["v02_build"]["status"] == "not_run"
    assert report["phase_results"]["v03_e2e"]["status"] == "not_run"
    assert report["phase_results"]["v04_contract_check"]["status"] == "not_run"


def test_source_size_change_detection_fails(monkeypatch):
    clean_root()
    create_fixture()
    inventory = empty_inventory(30, skipped=False)
    inventory.update({"file_count": 1, "total_bytes": 10, "media_kind_estimate": {"image": 1, "video": 0, "audio": 0, "raw_unsupported": 0, "other": 0}})
    before = {"a/one.jpg": {"relative_path": "a/one.jpg", "file_size": 10, "mtime_ns": 1, "is_file": True, "is_dir": False}}
    after = {"a/one.jpg": {"relative_path": "a/one.jpg", "file_size": 11, "mtime_ns": 2, "is_file": True, "is_dir": False}}
    snapshots = [SnapshotResult(before, inventory), SnapshotResult(after, inventory)]
    original = baseline_30gb.collect_source_snapshot

    def fake_snapshot(*args, **kwargs):
        if snapshots:
            return snapshots.pop(0)
        return original(*args, **kwargs)

    monkeypatch.setattr(baseline_30gb, "collect_source_snapshot", fake_snapshot)
    assert run_cli() == 1
    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["source_safety_checks"]["source_safety_status"] == "FAIL"
    assert report["source_safety_checks"]["source_modified_by_v04_4"] is True


def test_v02_failure_stops_v03_and_contract(monkeypatch):
    clean_root()
    create_fixture()

    def fail_v02(*args, **kwargs):
        return {"e2e_status": "FAIL", "combo_validation_status": "FAIL"}

    monkeypatch.setattr(baseline_30gb, "build_v02", fail_v02)
    assert run_cli() == 1
    report = read_report()
    assert report["phase_results"]["v01_scan"]["status"] == "PASS"
    assert report["phase_results"]["v02_build"]["status"] == "FAIL"
    assert report["phase_results"]["v03_e2e"]["status"] == "not_run"
    assert report["phase_results"]["v04_contract_check"]["status"] == "not_run"


def test_telemetry_schema_written():
    clean_root()
    create_fixture()
    assert run_cli() == 0
    telemetry_dir = WORKSPACE / "stages/v0.4/telemetry"
    for name in [
        "telemetry_run.json",
        "hardware_profile.json",
        "storage_profile.json",
        "resource_samples.jsonl",
        "telemetry_summary.json",
    ]:
        assert (telemetry_dir / name).exists()
    summary = json.loads((telemetry_dir / "telemetry_summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "V0.4-4"
    assert summary["task_mode"] == "real_30gb_baseline_validation"
    assert summary["start_sample_written"] is True
    assert summary["end_sample_written"] is True
    assert summary["sampling_completed"] is True
    assert summary["worker_fields_present"] is True
    assert summary["queue_fields_present"] is True
    assert summary["throughput_fields_present"] is True
    assert summary["adaptive_scheduler_enabled"] is False
    assert summary["workers_changed_by_this_run"] is False
    assert summary["scheduler_decisions_written"] is False
    assert summary["background_monitor_started"] is False


def test_test_backend_rejected_without_allow_flag():
    clean_root()
    create_fixture()
    assert run_cli(allow_test_backends=False, fresh=True) == 1
    report = app.run_validate_v04_30gb_baseline(
        SOURCE,
        WORKSPACE,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        2,
        2.0,
        30,
        True,
        False,
        False,
        None,
        False,
        "test_copy_jpg",
        "fake_ffmpeg_jpg",
    )
    assert report["validation_status"] == "FAIL"


def test_workspace_inside_source_does_not_write_source():
    clean_root()
    create_fixture()
    nested_workspace = SOURCE / "workspace"
    report = app.run_validate_v04_30gb_baseline(
        SOURCE,
        nested_workspace,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        2,
        2.0,
        30,
        True,
        False,
        False,
        None,
        True,
        "test_copy_jpg",
        "fake_ffmpeg_jpg",
    )
    assert report["validation_status"] == "FAIL"
    assert report["report_output_mode"] == "stdout_only_due_to_unsafe_workspace"
    assert report["workspace_safety_checks"]["report_output_mode"] == "stdout_only_due_to_unsafe_workspace"
    assert not nested_workspace.exists()
    assert not (SOURCE / "stages").exists()


def test_existing_workspace_requires_fresh_or_resume():
    clean_root()
    create_fixture()
    write_file(WORKSPACE / "old.txt", b"old")
    assert run_cli(fresh=False) == 1
    report = app.run_validate_v04_30gb_baseline(
        SOURCE,
        WORKSPACE,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        2,
        2.0,
        30,
        False,
        False,
        False,
        None,
        True,
        "test_copy_jpg",
        "fake_ffmpeg_jpg",
    )
    assert report["existing_workspace_conflict"] is True
    assert report["workspace_safety_checks"]["existing_workspace_conflict"] is True


def test_report_contract_top_level_fields_exist():
    clean_root()
    create_fixture()
    assert run_cli() == 0
    report = read_report()
    for key in [
        "schema_version",
        "stage",
        "task_name",
        "validation_status",
        "source",
        "workspace",
        "real_30gb_run",
        "test_backend_used",
        "production_backend_used",
        "report_output_mode",
        "existing_workspace_conflict",
        "run_id",
        "started_at",
        "finished_at",
        "elapsed_seconds",
        "preflight_gate",
        "phase_results",
        "v01_result",
        "v02_result",
        "v03_result",
        "v04_contract_result",
        "source_safety_checks",
        "workspace_safety_checks",
        "artifact_integrity_checks",
        "telemetry_outputs",
        "telemetry_summary_checks",
        "scheduler_boundary_checks",
        "model_boundary_checks",
        "semantic_boundary_checks",
        "forbidden_output_checks",
        "progress_state_checks",
        "regression_checks",
        "next_step_plan",
    ]:
        assert key in report
    assert "v04_4_report" in report["phase_results"]
    assert "real_30gb_run" in report["v02_result"]
    assert "test_backend_used" in report["v02_result"]
    assert "production_backend_used" in report["v02_result"]
