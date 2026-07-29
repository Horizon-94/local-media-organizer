from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from media_archive import app


TEST_ROOT = Path("/tmp/media_archive_v032_test")
WORKSPACE = TEST_ROOT / "workspace"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def clean_workspace() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    WORKSPACE.mkdir(parents=True)


def diff_record(
    relative_path: str | None,
    status: str,
    *,
    previous_relative_path: str | None = None,
    media_kind: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source_relative_path": relative_path,
        "previous_source_relative_path": previous_relative_path,
        "change_status": status,
        "reason": f"test {status}",
        "previous_file_size": None,
        "current_file_size": 10,
        "previous_mtime_ns": None,
        "current_mtime_ns": 20,
        "action_hint": "no_action",
        "downstream_invalidate_preview": False,
        "downstream_invalidate_video_frames": False,
        "downstream_invalidate_unified_manifest": False,
        "source_read_only": True,
        "model_loaded": False,
        "v03_incremental_enabled": True,
    }
    if media_kind is not None:
        record["media_kind"] = media_kind
    return record


def run_invalidation(*, diff_plan: Path | None = None) -> int:
    argv = [
        "v03-mark-invalidations",
        "--workspace",
        str(WORKSPACE),
    ]
    if diff_plan is not None:
        argv.extend(["--diff-plan", str(diff_plan)])
    return app.main(argv)


def assert_no_new_artifact_dirs() -> None:
    assert not (WORKSPACE / "preview").exists()
    assert not (WORKSPACE / "frames").exists()
    assert not (WORKSPACE / "image_timelapse_keyframes").exists()
    assert not (WORKSPACE / "video_frames_by_source").exists()


def plan_by_path(plan: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(record["source_relative_path"] or record["previous_source_relative_path"]): record
        for record in plan
    }


def test_v03_artifact_invalidation_plan_rules_and_boundaries(monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.3-2 must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)
    monkeypatch.setattr(app, "run_preview_images", fail_external_process)
    monkeypatch.setattr(app, "run_extract_video_frames", fail_external_process)
    monkeypatch.setattr(app, "build_v02", fail_external_process)
    monkeypatch.setattr(app, "validate_real_minimal_v02", fail_external_process)

    clean_workspace()
    existing_preview = WORKSPACE / "previews/existing.jpg"
    existing_frame = WORKSPACE / "video_frames/existing.jpg"
    existing_unified = WORKSPACE / "unified_manifest/existing.jsonl"
    existing_preview.parent.mkdir(parents=True)
    existing_frame.parent.mkdir(parents=True)
    existing_unified.parent.mkdir(parents=True)
    existing_preview.write_text("preview", encoding="utf-8")
    existing_frame.write_text("frame", encoding="utf-8")
    existing_unified.write_text("unified", encoding="utf-8")

    diff_path = WORKSPACE / "stages/v0.3/manifests/incremental_diff_plan.jsonl"
    diff_records = [
        diff_record("a/unchanged.jpg", "unchanged", previous_relative_path="a/unchanged.jpg"),
        diff_record("a/new.jpg", "new"),
        diff_record("b/base.jpg", "baseline", media_kind="image"),
        diff_record("a/modified.jpg", "modified", previous_relative_path="a/modified.jpg"),
        diff_record("v/modified.mov", "modified", previous_relative_path="v/modified.mov"),
        diff_record("v/deleted.mov", "deleted", previous_relative_path="v/deleted.mov"),
        diff_record("a/readme.txt", "unchanged", previous_relative_path="a/readme.txt"),
    ]
    write_jsonl(diff_path, diff_records)

    assert run_invalidation() == 0

    manifest_dir = WORKSPACE / "stages/v0.3/manifests"
    reports_dir = WORKSPACE / "stages/v0.3/reports"
    plan_path = manifest_dir / "artifact_invalidation_plan.jsonl"
    summary_path = reports_dir / "artifact_invalidation_summary.json"
    summary_md_path = reports_dir / "artifact_invalidation_summary.md"
    assert plan_path.exists()
    assert summary_path.exists()
    assert summary_md_path.exists()

    plan = read_jsonl(plan_path)
    summary = read_json(summary_path)
    by_path = plan_by_path(plan)
    assert len(plan) == len(diff_records)
    assert summary["total_diff_records"] == len(diff_records)
    assert summary["total_invalidation_records"] == len(diff_records)

    unchanged = by_path["a/unchanged.jpg"]
    assert unchanged["media_kind"] == "image"
    assert unchanged["artifact_scope"] == "downstream_reference"
    assert unchanged["artifact_status"] == "reusable"
    assert unchanged["artifact_action"] == "no_action"
    assert unchanged["invalidate_preview"] is False
    assert unchanged["invalidate_video_frames"] is False
    assert unchanged["invalidate_unified_manifest"] is False
    assert unchanged["delete_artifacts"] is False
    assert unchanged["rebuild_artifacts"] is False
    assert unchanged["source_deleted"] is False

    new_image = by_path["a/new.jpg"]
    assert new_image["artifact_scope"] == "image_preview"
    assert new_image["artifact_status"] == "new_source_pending_artifact"
    assert new_image["artifact_action"] == "mark_pending_create"
    assert new_image["invalidate_preview"] is True
    assert new_image["invalidate_video_frames"] is False
    assert new_image["invalidate_unified_manifest"] is True
    assert new_image["delete_artifacts"] is False
    assert new_image["rebuild_artifacts"] is False

    baseline = by_path["b/base.jpg"]
    assert baseline["artifact_scope"] == "image_preview"
    assert baseline["artifact_status"] == "new_source_pending_artifact"
    assert baseline["artifact_action"] == "mark_pending_create"

    modified_image = by_path["a/modified.jpg"]
    assert modified_image["artifact_scope"] == "image_preview"
    assert modified_image["artifact_status"] == "needs_invalidation"
    assert modified_image["artifact_action"] == "mark_invalid"
    assert modified_image["invalidate_preview"] is True
    assert modified_image["invalidate_video_frames"] is False
    assert modified_image["invalidate_unified_manifest"] is True

    modified_video = by_path["v/modified.mov"]
    assert modified_video["media_kind"] == "video"
    assert modified_video["artifact_scope"] == "video_frames"
    assert modified_video["artifact_status"] == "needs_invalidation"
    assert modified_video["artifact_action"] == "mark_invalid"
    assert modified_video["invalidate_preview"] is False
    assert modified_video["invalidate_video_frames"] is True
    assert modified_video["invalidate_unified_manifest"] is True

    deleted = by_path["v/deleted.mov"]
    assert deleted["artifact_scope"] == "unified_manifest"
    assert deleted["artifact_status"] == "source_deleted_reference_invalid"
    assert deleted["artifact_action"] == "mark_deleted_reference_invalid"
    assert deleted["source_deleted"] is True
    assert deleted["invalidate_preview"] is False
    assert deleted["invalidate_video_frames"] is False
    assert deleted["invalidate_unified_manifest"] is True
    assert deleted["delete_artifacts"] is False
    assert deleted["rebuild_artifacts"] is False

    text_record = by_path["a/readme.txt"]
    assert text_record["media_kind"] == "text"
    assert text_record["artifact_status"] == "reusable"

    assert summary["v03_invalidation_enabled"] is True
    assert summary["v03_incremental_enabled"] is True
    assert summary["source_read_only"] is True
    assert summary["model_loaded"] is False
    assert summary["action_taken"] == "record_artifact_invalidation_plan_only"
    assert summary["delete_artifacts_count"] == 0
    assert summary["rebuild_artifacts_count"] == 0
    assert summary["failed_count"] == 0
    assert summary["invalid_input_count"] == 0
    assert summary["missing_required_field_count"] == 0
    assert summary["reusable_count"] == 2
    assert summary["new_source_pending_artifact_count"] == 2
    assert summary["needs_invalidation_count"] == 2
    assert summary["source_deleted_reference_invalid_count"] == 1
    assert "one diff record produces one invalidation record" in summary_md_path.read_text(encoding="utf-8")

    assert existing_preview.exists()
    assert existing_frame.exists()
    assert existing_unified.exists()
    assert_no_new_artifact_dirs()


def test_deleted_record_uses_previous_path_for_media_kind():
    clean_workspace()
    custom_diff = TEST_ROOT / "custom_diff.jsonl"
    write_jsonl(
        custom_diff,
        [
            diff_record(
                None,
                "deleted",
                previous_relative_path="v/deleted.mov",
            )
        ],
    )

    assert run_invalidation(diff_plan=custom_diff) == 0

    plan = read_jsonl(WORKSPACE / "stages/v0.3/manifests/artifact_invalidation_plan.jsonl")
    summary = read_json(WORKSPACE / "stages/v0.3/reports/artifact_invalidation_summary.json")
    assert len(plan) == 1
    assert plan[0]["media_kind"] == "video"
    assert plan[0]["artifact_scope"] == "unified_manifest"
    assert plan[0]["artifact_status"] == "source_deleted_reference_invalid"
    assert plan[0]["source_deleted"] is True
    assert plan[0]["delete_artifacts"] is False
    assert plan[0]["rebuild_artifacts"] is False
    assert summary["failed_count"] == 0
    assert_no_new_artifact_dirs()


def test_invalid_change_status_does_not_pretend_to_pass():
    clean_workspace()
    diff_path = WORKSPACE / "stages/v0.3/manifests/incremental_diff_plan.jsonl"
    write_jsonl(diff_path, [diff_record("a/bad.jpg", "unknown_status")])

    assert run_invalidation() == 1

    plan = read_jsonl(WORKSPACE / "stages/v0.3/manifests/artifact_invalidation_plan.jsonl")
    summary = read_json(WORKSPACE / "stages/v0.3/reports/artifact_invalidation_summary.json")
    assert plan == []
    assert summary["failed_count"] > 0
    assert summary["invalid_input_count"] > 0
    assert summary["total_diff_records"] == 1
    assert summary["total_invalidation_records"] == 0
    assert summary["model_loaded"] is False
    assert_no_new_artifact_dirs()
