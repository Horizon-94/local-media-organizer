from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from media_archive import app


TEST_ROOT = Path("/tmp/media_archive_v034_test")
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


def task_state(task_id: str, source_relative_path: str, status: str, resume_action: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "source_relative_path": source_relative_path,
        "previous_source_relative_path": source_relative_path,
        "change_status": "unchanged",
        "media_kind": "image",
        "artifact_scope": "image_preview",
        "artifact_status": "reusable",
        "artifact_action": "no_action",
        "task_stage": "v03_artifact_state",
        "task_status": status,
        "previous_task_status": "",
        "resume_action": resume_action,
        "retry_count": 0,
        "max_retries": 3,
        "last_error_code": "",
        "last_error_message": "",
        "state_reason": "test",
        "state_observed_at": "2026-07-02T00:00:00+00:00",
        "checkpoint_tag_id": "checkpoint-test",
        "source_read_only": True,
        "model_loaded": False,
        "v03_incremental_enabled": True,
        "v03_invalidation_enabled": True,
        "v03_resume_enabled": True,
    }


def resume_record(task_id: str, source_relative_path: str, status: str, resume_action: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "source_relative_path": source_relative_path,
        "previous_source_relative_path": source_relative_path,
        "change_status": "unchanged",
        "media_kind": "image",
        "artifact_scope": "image_preview",
        "task_status": status,
        "resume_action": resume_action,
        "resume_priority": "none",
        "should_execute_later": status in {"pending", "pending_retry"},
        "execution_allowed_in_v03_3": False,
        "reason": "test",
        "source_read_only": True,
        "model_loaded": False,
        "v03_resume_enabled": True,
    }


def write_state_and_resume(records: list[tuple[str, str, str, str]]) -> None:
    state_path = WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl"
    resume_path = WORKSPACE / "stages/v0.3/manifests/resume_plan.jsonl"
    write_jsonl(state_path, [task_state(*record) for record in records])
    write_jsonl(resume_path, [resume_record(*record) for record in records])


def run_missing(*artifact_manifests: Path) -> int:
    argv = ["v03-plan-missing-artifacts", "--workspace", str(WORKSPACE)]
    for manifest in artifact_manifests:
        argv.extend(["--artifact-manifest", str(manifest)])
    return app.main(argv)


def by_artifact_path(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["artifact_path"]): record for record in records}


def assert_no_generated_outputs() -> None:
    assert not (WORKSPACE / "previews/generated.jpg").exists()
    assert not (WORKSPACE / "frames/generated.jpg").exists()
    assert not (WORKSPACE / "video_frames").exists()
    assert not (WORKSPACE / "image_timelapse_keyframes").exists()
    assert not (WORKSPACE / "video_frames_by_source").exists()


def test_v03_missing_artifact_detection_rules_and_boundaries(monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.3-4 must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)
    monkeypatch.setattr(app, "run_preview_images", fail_external_process)
    monkeypatch.setattr(app, "run_extract_video_frames", fail_external_process)
    monkeypatch.setattr(app, "run_build_unified_manifest", fail_external_process)
    monkeypatch.setattr(app, "build_v02", fail_external_process)
    monkeypatch.setattr(app, "validate_real_minimal_v02", fail_external_process)

    clean_workspace()
    (WORKSPACE / "previews").mkdir()
    (WORKSPACE / "previews/existing.jpg").write_text("jpg", encoding="utf-8")
    (WORKSPACE / "workspace_existing.jpg").write_text("workspace relative", encoding="utf-8")
    custom_manifest = WORKSPACE / "custom/manifests/artifacts.jsonl"
    (WORKSPACE / "custom/outputs").mkdir(parents=True)
    (WORKSPACE / "custom/outputs/relative_existing.jpg").write_text("manifest relative", encoding="utf-8")

    write_state_and_resume(
        [
            ("task_existing", "a/existing.jpg", "success", "keep_success"),
            ("task_missing", "a/missing.jpg", "success", "keep_success"),
            ("task_skipped_missing", "a/skipped.jpg", "skipped_unchanged", "skip_unchanged"),
            ("task_video", "v/clip.mov", "pending_retry", "retry_failed"),
            ("task_unified", "a/one.jpg", "success", "keep_success"),
            ("task_invalidated", "v/deleted.mov", "invalidated", "mark_invalidated"),
            ("task_blocked", "a/blocked.jpg", "blocked", "blocked_needs_manual_review"),
            ("task_nested", "a/nested.jpg", "success", "keep_success"),
            ("task_workspace_rel", "a/workspace_rel.jpg", "success", "keep_success"),
            ("task_manifest_rel", "a/manifest_rel.jpg", "success", "keep_success"),
        ]
    )
    manifest = WORKSPACE / "test_artifact_manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {"task_id": "task_existing", "source_relative_path": "a/existing.jpg", "preview_path": "previews/existing.jpg"},
            {"task_id": "task_missing", "source_relative_path": "a/missing.jpg", "preview_path": "previews/missing.jpg"},
            {
                "task_id": "task_skipped_missing",
                "source_relative_path": "a/skipped.jpg",
                "preview_path": "previews/skipped_missing.jpg",
            },
            {"task_id": "task_video", "source_relative_path": "v/clip.mov", "frame_path": "frames/clip_0001.jpg"},
            {
                "task_id": "task_unified",
                "source_relative_path": "a/one.jpg",
                "unified_manifest_path": "unified/unified_media_manifest.jsonl",
            },
            {"task_id": "task_invalidated", "source_relative_path": "v/deleted.mov", "preview_path": "previews/deleted.jpg"},
            {"task_id": "task_blocked", "source_relative_path": "a/blocked.jpg", "preview_path": "previews/blocked.jpg"},
            {"source_relative_path": "a/unmatched.jpg", "preview_path": "previews/unmatched.jpg"},
            {
                "task_id": "task_nested",
                "source_relative_path": "a/nested.jpg",
                "artifacts": {
                    "preview": "previews/nested_missing.jpg",
                    "sidecar": "sidecars/nested.json",
                },
                "frame_paths": ["frames/a.jpg", "frames/b.jpg"],
            },
            {"task_id": "task_workspace_rel", "source_relative_path": "a/workspace_rel.jpg", "artifact_path": "workspace_existing.jpg"},
        ],
    )
    write_jsonl(
        custom_manifest,
        [
            {
                "task_id": "task_manifest_rel",
                "source_relative_path": "a/manifest_rel.jpg",
                "artifact_path": "../outputs/relative_existing.jpg",
            }
        ],
    )

    assert run_missing(manifest, custom_manifest) == 0

    plan_path = WORKSPACE / "stages/v0.3/manifests/missing_artifact_plan.jsonl"
    summary_path = WORKSPACE / "stages/v0.3/reports/missing_artifact_summary.json"
    summary_md_path = WORKSPACE / "stages/v0.3/reports/missing_artifact_summary.md"
    assert plan_path.exists()
    assert summary_path.exists()
    assert summary_md_path.exists()
    plan = read_jsonl(plan_path)
    summary = read_json(summary_path)
    by_path = by_artifact_path(plan)

    assert by_path["previews/existing.jpg"]["artifact_exists"] is True
    assert by_path["previews/existing.jpg"]["missing_status"] == "present"
    assert by_path["previews/existing.jpg"]["repair_action_hint"] == "no_action"
    assert by_path["previews/missing.jpg"]["artifact_exists"] is False
    assert by_path["previews/missing.jpg"]["missing_status"] == "missing"
    assert by_path["previews/missing.jpg"]["repair_action_hint"] == "plan_recreate_preview"
    assert by_path["previews/missing.jpg"]["repair_allowed_in_v03_4"] is False
    assert by_path["previews/skipped_missing.jpg"]["artifact_expected"] is True
    assert by_path["previews/skipped_missing.jpg"]["missing_status"] == "missing"
    assert by_path["previews/skipped_missing.jpg"]["repair_action_hint"] == "plan_recreate_preview"
    assert by_path["frames/clip_0001.jpg"]["artifact_kind"] == "video_frame"
    assert by_path["frames/clip_0001.jpg"]["missing_status"] == "missing"
    assert by_path["frames/clip_0001.jpg"]["repair_action_hint"] == "plan_recreate_video_frames"
    assert by_path["unified/unified_media_manifest.jsonl"]["artifact_kind"] == "unified_manifest"
    assert by_path["unified/unified_media_manifest.jsonl"]["missing_status"] == "missing"
    assert by_path["unified/unified_media_manifest.jsonl"]["repair_action_hint"] == "plan_rebuild_manifest_reference"
    assert by_path["previews/deleted.jpg"]["artifact_expected"] is False
    assert by_path["previews/deleted.jpg"]["missing_status"] == "not_expected"
    assert by_path["previews/blocked.jpg"]["artifact_expected"] is False
    assert by_path["previews/blocked.jpg"]["missing_status"] == "blocked_by_task_state"
    assert by_path["previews/blocked.jpg"]["repair_action_hint"] == "plan_manual_review"
    assert by_path["previews/unmatched.jpg"]["task_status"] is None
    assert by_path["previews/unmatched.jpg"]["missing_status"] == "blocked_by_task_state"
    assert by_path["previews/unmatched.jpg"]["repair_action_hint"] == "plan_manual_review"
    assert by_path["previews/nested_missing.jpg"]["artifact_kind"] == "image_preview"
    assert by_path["sidecars/nested.json"]["artifact_kind"] == "sidecar"
    assert by_path["frames/a.jpg"]["artifact_kind"] == "video_frame"
    assert by_path["frames/b.jpg"]["artifact_kind"] == "video_frame"
    assert by_path["workspace_existing.jpg"]["artifact_exists"] is True
    assert by_path["workspace_existing.jpg"]["artifact_path_resolved"] == str((WORKSPACE / "workspace_existing.jpg").resolve())
    assert by_path["../outputs/relative_existing.jpg"]["artifact_exists"] is True
    assert by_path["../outputs/relative_existing.jpg"]["artifact_path_resolved"] == str(
        (custom_manifest.parent / "../outputs/relative_existing.jpg").resolve()
    )

    assert summary["v03_missing_artifact_enabled"] is True
    assert summary["source_read_only"] is True
    assert summary["model_loaded"] is False
    assert summary["action_taken"] == "record_missing_artifact_plan_only"
    assert summary["missing_artifact_count"] >= 5
    assert summary["unmatched_artifact_count"] >= 1
    assert summary["plan_recreate_preview_count"] >= 2
    assert summary["plan_recreate_video_frames_count"] >= 1
    assert summary["plan_rebuild_manifest_reference_count"] >= 1
    assert summary["plan_manual_review_count"] >= 1
    assert summary["delete_artifacts_count"] == 0
    assert summary["rebuild_artifacts_count"] == 0
    assert summary["failed_input_count"] == 0
    assert summary["invalid_manifest_count"] == 0
    assert "repair_allowed_in_v03_4: false" in summary_md_path.read_text(encoding="utf-8")
    assert_no_generated_outputs()


def test_invalid_task_state_input_does_not_pretend_to_pass():
    clean_workspace()
    state_path = WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl"
    resume_path = WORKSPACE / "stages/v0.3/manifests/resume_plan.jsonl"
    manifest = WORKSPACE / "test_artifact_manifest.jsonl"
    bad_state = task_state("task_bad", "a/bad.jpg", "success", "keep_success")
    bad_state.pop("task_id")
    write_jsonl(state_path, [bad_state])
    write_jsonl(resume_path, [resume_record("task_bad", "a/bad.jpg", "success", "keep_success")])
    write_jsonl(manifest, [{"task_id": "task_bad", "source_relative_path": "a/bad.jpg", "preview_path": "previews/bad.jpg"}])

    assert run_missing(manifest) == 1

    plan = read_jsonl(WORKSPACE / "stages/v0.3/manifests/missing_artifact_plan.jsonl")
    summary = read_json(WORKSPACE / "stages/v0.3/reports/missing_artifact_summary.json")
    assert summary["failed_input_count"] > 0
    assert summary["model_loaded"] is False
    assert all(record["missing_status"] == "blocked_by_task_state" for record in plan)
    assert_no_generated_outputs()
