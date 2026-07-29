from __future__ import annotations

import json
import shutil
from pathlib import Path

from media_archive import app

TEST_ROOT = Path("/tmp/media_archive_v042_test")
SOURCE = TEST_ROOT / "source"
WORKSPACE = TEST_ROOT / "workspace"


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def clean_root() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    WORKSPACE.mkdir(parents=True)


def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def create_sample_source(source: Path = SOURCE) -> None:
    write_file(source / "a/one.jpg", b"same-image")
    write_file(source / "a/one_copy.jpg", b"same-image")
    write_file(source / "a/two.jpg", b"unique-image")
    write_file(source / "v/one.mov", b"same-video")
    write_file(source / "v/one_copy.mov", b"same-video")
    write_file(source / "a/test.wav", b"audio")
    write_file(source / "raw/A001.braw", b"blackmagic-raw")
    write_file(source / "raw/C001.CRM", b"canon-raw")
    write_file(source / "raw/G001.GPR", b"gopro-raw")
    write_file(source / "a/readme.xyz", b"unsupported")
    (source / "a/empty.mp4").parent.mkdir(parents=True, exist_ok=True)
    (source / "a/empty.mp4").write_bytes(b"")


def source_snapshot(source: Path = SOURCE) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }


def run_small_chain(
    *,
    source: Path = SOURCE,
    workspace: Path = WORKSPACE,
    fresh: bool = True,
    preview_backend: str | None = "test_copy_jpg",
    video_runner: str | None = "fake_ffmpeg_jpg",
) -> int:
    argv = [
        "v04-validate-small-chain",
        "--source",
        str(source),
        "--workspace",
        str(workspace),
    ]
    if fresh:
        argv.append("--fresh")
    if preview_backend is not None:
        argv += ["--preview-backend", preview_backend]
    if video_runner is not None:
        argv += ["--video-runner", video_runner]
    return app.main(argv)


def report(workspace: Path = WORKSPACE) -> dict[str, object]:
    return read_json(workspace / "stages/v0.4/reports/v04_small_chain_validation.json")


def assert_normal_pass(report_data: dict[str, object]) -> None:
    assert report_data["validation_status"] == "PASS"
    assert report_data["failed_check_count"] == 0
    assert report_data["execution_order"] == [
        "source-stat-before",
        "v01-scan",
        "build-v02-test-backend",
        "v03-validate-e2e",
        "v04-check-contracts",
        "source-stat-after",
        "v04-small-chain-validation-report",
    ]
    counts = report_data["record_counts"]
    assert counts["v01_scan_records"] >= 8
    assert counts["v02_unified_records"] >= 8
    assert counts["v03_diff_records"] >= 8
    assert counts["v03_diff_records"] == counts["v03_invalidation_records"]
    assert counts["v03_invalidation_records"] == counts["v03_task_state_records"]
    assert counts["v03_task_state_records"] == counts["v03_resume_records"]
    safety = report_data["source_write_safety_checks"]
    assert safety["source_snapshot_before_count"] > 0
    assert safety["source_snapshot_after_count"] > 0
    assert safety["source_modified_by_v04_2"] is False
    assert safety["source_added_by_v04_2"] == []
    assert safety["source_deleted_by_v04_2"] == []
    assert safety["source_stat_changed_by_v04_2"] == []
    assert report_data["v01_checks"]["v01_scan_manifest_exists"] is True
    assert report_data["v02_checks"]["v02_e2e_status"] == "PASS"
    assert report_data["v02_checks"]["v02_used_test_preview_backend"] is True
    assert report_data["v02_checks"]["v02_used_fake_video_runner"] is True
    assert report_data["v03_checks"]["v03_validation_status"] == "PASS"
    assert report_data["v03_checks"]["v03_running_count_zero"] is True
    assert report_data["v03_checks"]["v03_execution_allowed_all_false"] is True
    assert report_data["v03_checks"]["v03_repair_allowed_all_false"] is True
    assert report_data["v04_contract_checks"]["v04_contract_validation_status"] == "PASS"
    assert report_data["v04_contract_checks"]["v04_contract_failed_check_count"] == 0
    raw = report_data["raw_unsupported_checks"]
    assert raw["raw_records_count"] >= 3
    assert raw["raw_video_unsupported_count"] >= 3
    assert raw["raw_blocked_count"] >= 3
    assert raw["raw_records_with_video_frames"] == 0
    assert raw["raw_records_analysis_eligible_false"] is True
    path_checks = report_data["path_separation_checks"]
    assert path_checks["source_inside_workspace"] is False
    assert path_checks["workspace_inside_source"] is False
    assert path_checks["workspace_outputs_inside_source"] is False
    assert path_checks["source_modified_by_v04_2"] is False
    boundary = report_data["boundary_checks"]
    assert boundary["v04_2_ran_real_material"] is False
    assert boundary["v04_2_ran_30gb"] is False
    assert boundary["v04_2_ran_1_3tb"] is False
    assert boundary["v04_2_ran_32tb"] is False
    assert boundary["v04_2_used_real_a9t"] is False
    assert boundary["v04_2_used_real_c4"] is False
    assert boundary["v04_2_used_test_preview_backend"] is True
    assert boundary["v04_2_used_fake_video_runner"] is True
    assert boundary["v04_2_loaded_model"] is False
    assert boundary["v04_2_used_semantic_search"] is False
    assert boundary["v04_2_entered_v04_3"] is False
    assert boundary["v04_2_entered_v05"] is False
    assert boundary["v04_2_entered_v06"] is False
    assert boundary["forbidden_outputs_created"] == []


def test_v04_small_chain_passes_with_test_backend_and_fake_runner():
    clean_root()
    create_sample_source()
    before = source_snapshot()

    assert run_small_chain() == 0

    assert source_snapshot() == before
    report_data = report()
    assert_normal_pass(report_data)
    for path in [
        WORKSPACE / "stages/v0.4/reports/v04_small_chain_validation.json",
        WORKSPACE / "stages/v0.4/reports/v04_small_chain_validation.md",
        WORKSPACE / "stages/v0.4/reports/v04_contract_compat_report.json",
        WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl",
        WORKSPACE / "v02/reports/v02_e2e_report.json",
        WORKSPACE / "v02/unified/unified_media_manifest.jsonl",
        WORKSPACE / "stages/v0.3/reports/v03_e2e_validation.json",
    ]:
        assert path.exists()
    assert not (SOURCE / "v02").exists()
    assert not (SOURCE / "stages").exists()


def test_v04_small_chain_rejects_source_inside_workspace():
    clean_root()
    nested_source = WORKSPACE / "source"
    create_sample_source(nested_source)

    assert run_small_chain(source=nested_source, workspace=WORKSPACE) == 1

    report_data = report()
    assert report_data["validation_status"] == "FAIL"
    assert report_data["path_separation_checks"]["source_inside_workspace"] is True
    assert not (WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl").exists()


def test_v04_small_chain_rejects_workspace_inside_source_without_writing_source():
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    create_sample_source(SOURCE)
    nested_workspace = SOURCE / "workspace"

    result = app.run_validate_v04_small_chain(
        SOURCE,
        nested_workspace,
        True,
        "test_copy_jpg",
        "fake_ffmpeg_jpg",
    )

    assert result["validation_status"] == "FAIL"
    assert result["path_separation_checks"]["workspace_inside_source"] is True
    assert not nested_workspace.exists()


def test_v04_small_chain_rejects_missing_test_preview_backend():
    clean_root()
    create_sample_source()

    assert run_small_chain(preview_backend=None) == 1

    report_data = report()
    assert report_data["validation_status"] == "FAIL"
    assert report_data["v02_checks"]["v02_used_test_preview_backend"] is False
    assert report_data["boundary_checks"]["v04_2_used_real_a9t"] is True
    assert not (WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl").exists()


def test_v04_small_chain_rejects_missing_fake_video_runner():
    clean_root()
    create_sample_source()

    assert run_small_chain(video_runner=None) == 1

    report_data = report()
    assert report_data["validation_status"] == "FAIL"
    assert report_data["v02_checks"]["v02_used_fake_video_runner"] is False
    assert report_data["boundary_checks"]["v04_2_used_real_c4"] is True
    assert not (WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl").exists()
