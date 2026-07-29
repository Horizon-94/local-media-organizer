from __future__ import annotations

import json
import shutil
from pathlib import Path

from media_archive import app
from media_archive.v04 import preflight_30gb


TEST_ROOT = Path("/tmp/media_archive_v043_test")
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
    min_total_bytes: int = 1,
    max_total_bytes: int = 1_000_000_000,
    min_file_count: int = 1,
    extra: list[str] | None = None,
) -> int:
    argv = [
        "v04-preflight-30gb",
        "--source",
        str(source),
        "--workspace",
        str(workspace),
        "--min-total-bytes",
        str(min_total_bytes),
        "--max-total-bytes",
        str(max_total_bytes),
        "--min-file-count",
        str(min_file_count),
        "--telemetry-sample-interval-seconds",
        "2",
        "--inventory-timeout-seconds",
        "30",
    ]
    if extra:
        argv.extend(extra)
    return app.main(argv)


def read_report(workspace: Path = WORKSPACE) -> dict[str, object]:
    return json.loads((workspace / "stages/v0.4/reports/v04_30gb_preflight_report.json").read_text(encoding="utf-8"))


def test_v04_preflight_30gb_passes_and_writes_telemetry():
    clean_root()
    create_fixture()

    assert run_cli() == 0

    report = read_report()
    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["source_path_checks"]["source_name_matches_expected"] is True
    assert report["source_size_checks"]["within_expected_size_range"] is True
    assert report["source_size_checks"]["file_count_meets_minimum"] is True
    assert report["workspace_path_checks"]["workspace_is_writable"] is True
    safety = report["source_read_safety_checks"]
    assert safety["source_modified_by_v04_3"] is False
    assert safety["source_content_read"] is False
    assert safety["source_sha256_calculated"] is False
    assert safety["source_decoded"] is False
    assert report["boundary_checks"]["v04_3_called_ffmpeg"] is False
    assert report["boundary_checks"]["v04_3_called_ffprobe"] is False
    assert report["forbidden_output_checks"]["forbidden_outputs_created"] == []
    assert report["source_file_inventory"]["media_kind_estimate"]["raw_unsupported"] == 3

    telemetry_dir = WORKSPACE / "stages/v0.4/telemetry"
    for name in [
        "telemetry_run.json",
        "hardware_profile.json",
        "storage_profile.json",
        "resource_samples.jsonl",
        "telemetry_summary.json",
    ]:
        assert (telemetry_dir / name).exists()
    samples = [
        json.loads(line)
        for line in (telemetry_dir / "resource_samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(samples) >= 1
    sample = samples[0]
    for key in [
        "cpu_load_percent",
        "memory_used_bytes",
        "swap_used_bytes",
        "disk_read_bytes_per_sec",
        "gpu_utilization_percent",
        "gpu_probe_available",
        "pipeline",
    ]:
        assert key in sample
    assert {"sips_workers", "queue_depth", "throughput_items_per_sec"} <= set(sample["pipeline"])
    summary = json.loads((telemetry_dir / "telemetry_summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "V0.4-3"
    assert summary["task_mode"] == "preflight_30gb_only_with_record_only_telemetry"
    assert summary["resource_sample_count"] >= 1
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


def test_source_missing_fails():
    clean_root()
    assert run_cli() == 1
    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["source_path_checks"]["source_exists"] is False


def test_source_inside_workspace_fails():
    clean_root()
    nested = WORKSPACE / "source"
    create_fixture(nested)
    assert run_cli(source=nested, workspace=WORKSPACE) == 1
    report = read_report()
    assert report["source_path_checks"]["source_inside_workspace"] is True


def test_workspace_inside_source_fails_without_writing_source():
    clean_root()
    create_fixture()
    nested_workspace = SOURCE / "workspace"
    report = app.run_preflight_v04_30gb(
        SOURCE,
        nested_workspace,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        False,
        2,
        30,
        2.0,
        None,
    )
    assert report["validation_status"] == "FAIL"
    assert report["workspace_path_checks"]["workspace_inside_source"] is True
    assert report["workspace_path_checks"]["workspace_output_inside_source"] is True
    assert report["report_output_checks"]["report_output_mode"] == "stdout_only_due_to_unsafe_workspace"
    assert report["report_output_checks"]["report_files_written"] is False
    assert report["report_output_checks"]["telemetry_files_written"] is False
    assert report["report_output_checks"]["workspace_write_probe_skipped_due_to_unsafe_workspace"] is True
    assert report["report_output_checks"]["telemetry_skipped_due_to_unsafe_workspace"] is True
    assert not nested_workspace.exists()
    assert not (SOURCE / "stages").exists()
    assert not (SOURCE / "telemetry").exists()
    assert not (SOURCE / "reports").exists()
    assert not (SOURCE / ".v04_preflight_write_probe.tmp").exists()


def test_source_name_mismatch_and_size_range_fail():
    clean_root()
    wrong = TEST_ROOT / "Wrong_Source"
    create_fixture(wrong)
    assert run_cli(source=wrong) == 1
    assert read_report()["source_path_checks"]["source_name_matches_expected"] is False

    clean_root()
    create_fixture()
    assert run_cli(min_total_bytes=999_999_999_999) == 1
    assert read_report()["source_size_checks"]["within_expected_size_range"] is False


def test_builtin_dangerous_path_skips_inventory(monkeypatch):
    clean_root()
    calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("inventory should not run")

    monkeypatch.setattr(preflight_30gb, "collect_source_snapshot", fail_if_called)
    report = app.run_preflight_v04_30gb(
        Path.home(),
        WORKSPACE,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        False,
        2,
        30,
        2.0,
        None,
    )
    assert calls["count"] == 0
    assert report["validation_status"] == "FAIL"
    assert report["source_path_checks"]["source_path_is_home_or_root"] is True
    assert report["source_path_checks"]["source_path_matches_builtin_forbidden_rule"] is True
    assert report["source_read_safety_checks"]["inventory_walk_skipped_due_to_early_path_fail"] is True
    assert report["source_read_safety_checks"]["source_content_read"] is False
    assert report["source_read_safety_checks"]["source_sha256_calculated"] is False
    assert report["source_read_safety_checks"]["source_decoded"] is False


def test_user_forbidden_substring_fails():
    clean_root()
    source = TEST_ROOT / "danger_test" / "MEDIA_ARCHIVE_TEST_SOURCE"
    create_fixture(source)
    assert run_cli(source=source, extra=["--forbidden-source-substring", "danger_test"]) == 1
    report = read_report()
    assert report["source_path_checks"]["source_path_matches_user_forbidden_substring"] is True
    assert "danger_test" in report["source_path_checks"]["matched_user_forbidden_substrings"]


def test_forbidden_outputs_not_created_and_sampler_not_background():
    clean_root()
    create_fixture()
    assert run_cli() == 0
    report = read_report()
    for rel in [
        "previews",
        "frames",
        "video_frames",
        "image_timelapse_keyframes",
        "video_frames_by_source",
        "unified/unified_media_manifest.jsonl",
        "reports/v02_e2e_report.json",
        "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl",
        "stages/v0.3/reports/v03_e2e_validation.json",
        "stages/v0.4/reports/v04_small_chain_validation.json",
        "scheduler_decisions.jsonl",
    ]:
        assert not (WORKSPACE / rel).exists()
    assert report["scheduler_boundary_checks"]["background_monitor_started"] is False
    assert report["scheduler_boundary_checks"]["scheduler_decisions_written"] is False


def test_mtime_only_change_is_warning_not_fail(monkeypatch):
    clean_root()
    create_fixture()
    inventory = preflight_30gb.empty_inventory(30, skipped=False)
    inventory.update(
        {
            "file_count": 1,
            "total_bytes": 10,
            "extension_count": {".jpg": 1},
            "extension_summary": [{"extension": ".jpg", "count": 1, "total_bytes": 10, "media_kind_estimate": "image"}],
            "media_kind_estimate": {"image": 1, "video": 0, "audio": 0, "raw_unsupported": 0, "other": 0},
        }
    )
    before = {"a/one.jpg": {"relative_path": "a/one.jpg", "file_size": 10, "mtime_ns": 1, "is_file": True, "is_dir": False}}
    after = {"a/one.jpg": {"relative_path": "a/one.jpg", "file_size": 10, "mtime_ns": 2, "is_file": True, "is_dir": False}}
    snapshots = [
        preflight_30gb.SnapshotResult(before, inventory),
        preflight_30gb.SnapshotResult(after, inventory),
    ]
    monkeypatch.setattr(preflight_30gb, "collect_source_snapshot", lambda *args, **kwargs: snapshots.pop(0))
    assert run_cli() == 0
    report = read_report()
    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["warning_check_count"] > 0
    assert report["source_read_safety_checks"]["source_mtime_changed_warning_paths"] == ["a/one.jpg"]
    assert report["source_read_safety_checks"]["source_size_changed_by_v04_3"] == []
    assert report["source_read_safety_checks"]["source_modified_by_v04_3"] is False


def test_home_documents_exact_match_only(monkeypatch):
    clean_root()
    fake_home = TEST_ROOT / "fake_home"
    docs = fake_home / "Documents"
    legal_child = docs / "MEDIA_ARCHIVE_TEST_SOURCE"
    create_fixture(legal_child)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    report = app.run_preflight_v04_30gb(
        docs,
        WORKSPACE,
        "Documents",
        1,
        1_000_000_000,
        1,
        False,
        2,
        30,
        2.0,
        None,
    )
    assert report["source_path_checks"]["source_path_is_home_or_root"] is True
    assert report["validation_status"] == "FAIL"

    report = app.run_preflight_v04_30gb(
        legal_child,
        WORKSPACE,
        "MEDIA_ARCHIVE_TEST_SOURCE",
        1,
        1_000_000_000,
        1,
        False,
        2,
        30,
        2.0,
        None,
    )
    assert report["source_path_checks"]["source_path_is_home_or_root"] is False
    assert report["source_path_checks"]["source_path_matches_builtin_forbidden_rule"] is False
    assert report["source_path_checks"]["matched_builtin_forbidden_rules"] == []


def test_gpu_unavailable_does_not_fail():
    clean_root()
    create_fixture()
    assert run_cli() == 0
    report = read_report()
    assert report["gpu_sampling_checks"]["gpu_probe_available"] is False
    telemetry = json.loads((WORKSPACE / "stages/v0.4/telemetry/telemetry_summary.json").read_text(encoding="utf-8"))
    assert telemetry["gpu_probe_available"] is False
