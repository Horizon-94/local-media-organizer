from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from media_archive import app

TEST_ROOT = Path("/tmp/media_archive_v041_test")
SOURCE = TEST_ROOT / "source"
WORKSPACE = TEST_ROOT / "workspace"
FIXTURES = TEST_ROOT / "fixtures"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def write_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_report() -> dict[str, object]:
    return json.loads((WORKSPACE / "stages/v0.4/reports/v04_contract_compat_report.json").read_text())


def clean_root() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    WORKSPACE.mkdir(parents=True)
    FIXTURES.mkdir(parents=True)
    for rel in ("a/one.jpg", "a/two.jpg", "v/one.mov", "raw/A001.braw"):
        path = SOURCE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel, encoding="utf-8")


def fixture_paths() -> dict[str, Path]:
    return {
        "v01": FIXTURES / "v01_scan_manifest.jsonl",
        "v02_unified": FIXTURES / "v02_unified_media_manifest.jsonl",
        "quality": FIXTURES / "v02_quality_summary.json",
        "diff": FIXTURES / "v03_incremental_diff_plan.jsonl",
        "invalidation": FIXTURES / "v03_artifact_invalidation_plan.jsonl",
        "task_state": FIXTURES / "v03_task_state_manifest.jsonl",
        "resume": FIXTURES / "v03_resume_plan.jsonl",
        "missing": FIXTURES / "v03_missing_artifact_plan.jsonl",
    }


def build_fixture(
    *,
    invalidation_rows: int = 4,
    running: bool = False,
    execution_allowed: bool = False,
    repair_allowed: bool = False,
    artifact_inside_source: bool = False,
    missing_unified_source_key: bool = False,
) -> dict[str, Path]:
    paths = fixture_paths()
    sources = [
        ("a/one.jpg", "image", "baseline"),
        ("a/two.jpg", "image", "unchanged"),
        ("v/one.mov", "video", "modified"),
        ("raw/A001.braw", "other", "deleted"),
    ]
    write_jsonl(
        paths["v01"],
        [
            {
                "source_root": str(SOURCE),
                "source_absolute_path": str(SOURCE / rel),
                "source_relative_path": rel,
                "media_kind": media_kind,
                "file_size": len(rel),
                "mtime_ns": 1_700_000_000_000_000_000 + index,
            }
            for index, (rel, media_kind, _status) in enumerate(sources)
        ],
    )
    artifact_path = str(SOURCE / "a/one_preview.jpg") if artifact_inside_source else str(WORKSPACE / "artifacts/one_preview.jpg")
    unified_records = []
    for rel, media_kind, _status in sources:
        row = {
            "source_relative_path": rel,
            "media_kind": media_kind,
            "pipeline_status": "available",
            "source_read_only": True,
            "model_loaded": False,
            "artifact_path": artifact_path,
        }
        if missing_unified_source_key:
            row.pop("source_relative_path")
        unified_records.append(row)
    write_jsonl(paths["v02_unified"], unified_records)
    write_json(paths["quality"], {"source_read_only": True, "action_taken": "record_only", "model_loaded": False})
    diff_records = [
        {"source_relative_path": rel, "previous_source_relative_path": rel, "change_status": status}
        for rel, _media_kind, status in sources
    ]
    write_jsonl(paths["diff"], diff_records)
    invalidation_records = [
        {
            "source_relative_path": rel,
            "previous_source_relative_path": rel,
            "change_status": status,
            "artifact_status": "reusable" if status == "unchanged" else "needs_invalidation",
            "artifact_action": "no_action" if status == "unchanged" else "mark_invalid",
            "source_read_only": True,
            "model_loaded": False,
        }
        for rel, _media_kind, status in sources[:invalidation_rows]
    ]
    write_jsonl(paths["invalidation"], invalidation_records)
    task_records = [
        {
            "task_id": f"task-{index}",
            "source_relative_path": rel,
            "previous_source_relative_path": rel,
            "change_status": status,
            "task_status": "running" if running and index == 0 else ("skipped_unchanged" if status == "unchanged" else "pending"),
            "resume_action": "retry_after_interrupted" if running and index == 0 else ("skip_unchanged" if status == "unchanged" else "create_pending_task"),
            "source_read_only": True,
            "model_loaded": False,
        }
        for index, (rel, _media_kind, status) in enumerate(sources)
    ]
    write_jsonl(paths["task_state"], task_records)
    resume_records = [
        {
            "task_id": record["task_id"],
            "source_relative_path": record["source_relative_path"],
            "previous_source_relative_path": record["previous_source_relative_path"],
            "task_status": "pending_retry" if record["task_status"] == "running" else record["task_status"],
            "resume_action": record["resume_action"],
            "execution_allowed_in_v03_3": execution_allowed and index == 0,
            "source_read_only": True,
            "model_loaded": False,
        }
        for index, record in enumerate(task_records)
    ]
    write_jsonl(paths["resume"], resume_records)
    missing_records = [
        {
            "artifact_check_id": f"check-{index}",
            "source_relative_path": rel,
            "task_id": f"task-{index}",
            "artifact_kind": "image_preview" if media_kind == "image" else "video_frame",
            "artifact_path": str(WORKSPACE / f"artifacts/{index}.jpg"),
            "artifact_exists": False,
            "artifact_expected": status != "deleted",
            "missing_status": "not_expected" if status == "deleted" else "missing",
            "repair_allowed_in_v03_4": repair_allowed and index == 0,
            "source_read_only": True,
            "model_loaded": False,
        }
        for index, (rel, media_kind, status) in enumerate(sources)
    ]
    write_jsonl(paths["missing"], missing_records)
    return paths


def run_contract_check(paths: dict[str, Path]) -> int:
    return app.main(
        [
            "v04-check-contracts",
            "--workspace",
            str(WORKSPACE),
            "--source-root",
            str(SOURCE),
            "--v01-scan-manifest",
            str(paths["v01"]),
            "--v02-unified-manifest",
            str(paths["v02_unified"]),
            "--v02-quality-summary",
            str(paths["quality"]),
            "--v03-diff-plan",
            str(paths["diff"]),
            "--v03-invalidation-plan",
            str(paths["invalidation"]),
            "--v03-task-state",
            str(paths["task_state"]),
            "--v03-resume-plan",
            str(paths["resume"]),
            "--v03-missing-artifact-plan",
            str(paths["missing"]),
        ]
    )


def assert_no_forbidden_outputs() -> None:
    for rel in (
        "previews",
        "frames",
        "video_frames",
        "image_timelapse_keyframes",
        "video_frames_by_source",
        "unified/unified_media_manifest.jsonl",
    ):
        assert not (WORKSPACE / rel).exists()


def test_v04_contract_compat_pass(monkeypatch):
    def fail_forbidden_command(*args, **kwargs):
        raise AssertionError("V0.4-1 must not call upstream commands")

    monkeypatch.setattr(subprocess, "run", fail_forbidden_command)
    monkeypatch.setattr(app, "run_v01_scan", fail_forbidden_command)
    monkeypatch.setattr(app, "run_scan", fail_forbidden_command)
    monkeypatch.setattr(app, "run_preview_images", fail_forbidden_command)
    monkeypatch.setattr(app, "run_extract_video_frames", fail_forbidden_command)
    monkeypatch.setattr(app, "build_v02", fail_forbidden_command)
    monkeypatch.setattr(app, "run_build_unified_manifest", fail_forbidden_command)
    monkeypatch.setattr(app, "validate_real_minimal_v02", fail_forbidden_command)
    monkeypatch.setattr(app, "run_validate_v03_e2e", fail_forbidden_command)
    clean_root()
    paths = build_fixture()

    assert run_contract_check(paths) == 0

    report = read_report()
    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["v03_one_to_one_checks"]["diff_to_invalidation_one_to_one"] is True
    assert report["v03_one_to_one_checks"]["invalidation_to_task_state_one_to_one"] is True
    assert report["v03_one_to_one_checks"]["task_state_to_resume_one_to_one"] is True
    assert report["state_safety_checks"]["running_count_zero"] is True
    assert report["state_safety_checks"]["execution_allowed_in_v03_3_all_false"] is True
    assert report["state_safety_checks"]["repair_allowed_in_v03_4_all_false"] is True
    assert report["state_safety_checks"]["source_read_only_all_true"] is True
    assert report["state_safety_checks"]["model_loaded_all_false"] is True
    assert report["path_separation_checks"]["artifact_path_inside_source_root"] is False
    assert report["input_file_safety_checks"]["input_snapshot_before_count"] > 0
    assert report["input_file_safety_checks"]["input_snapshot_after_count"] > 0
    assert report["input_file_safety_checks"]["input_files_changed_by_v04"] is False
    assert report["input_file_safety_checks"]["input_files_added_by_v04"] == []
    assert report["input_file_safety_checks"]["input_files_deleted_by_v04"] == []
    assert report["input_file_safety_checks"]["input_file_stat_changed_by_v04"] == []
    assert report["boundary_checks"]["v04_generated_preview"] is False
    assert report["boundary_checks"]["v04_extracted_frames"] is False
    assert report["boundary_checks"]["v04_loaded_model"] is False
    assert report["boundary_checks"]["v04_entered_v04_2"] is False
    assert report["regression_checks"]["v02_current_valid_strategy"] == "A9T-v3 + C4"
    assert_no_forbidden_outputs()


def test_v04_contract_compat_rejects_v03_count_mismatch():
    clean_root()
    paths = build_fixture(invalidation_rows=3)

    assert run_contract_check(paths) == 1

    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["failed_check_count"] > 0
    assert report["v03_one_to_one_checks"]["diff_to_invalidation_one_to_one"] is False


def test_v04_contract_compat_rejects_running_residual():
    clean_root()
    paths = build_fixture(running=True)

    assert run_contract_check(paths) == 1

    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["state_safety_checks"]["running_count_zero"] is False


def test_v04_contract_compat_rejects_execution_or_repair_allowed():
    clean_root()
    paths = build_fixture(execution_allowed=True)
    assert run_contract_check(paths) == 1
    assert read_report()["state_safety_checks"]["execution_allowed_in_v03_3_all_false"] is False

    clean_root()
    paths = build_fixture(repair_allowed=True)
    assert run_contract_check(paths) == 1
    assert read_report()["state_safety_checks"]["repair_allowed_in_v03_4_all_false"] is False


def test_v04_contract_compat_rejects_artifact_inside_source_root():
    clean_root()
    paths = build_fixture(artifact_inside_source=True)

    assert run_contract_check(paths) == 1

    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["path_separation_checks"]["artifact_path_inside_source_root"] is True
    assert str((SOURCE / "a/one_preview.jpg").resolve()) in report["path_separation_checks"]["artifact_path_inside_source_root_paths"]


def test_v04_contract_compat_rejects_missing_required_field():
    clean_root()
    paths = build_fixture(missing_unified_source_key=True)

    assert run_contract_check(paths) == 1

    report = read_report()
    assert report["validation_status"] == "FAIL"
    assert report["required_field_checks"]["missing_required_field_count"] > 0
