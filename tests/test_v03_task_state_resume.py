from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from media_archive import app
from media_archive.incremental.task_state import build_task_id


TEST_ROOT = Path("/tmp/media_archive_v033_test")
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


def invalidation_record(
    relative_path: str | None,
    change_status: str,
    artifact_status: str,
    artifact_action: str,
    *,
    previous_relative_path: str | None = None,
    media_kind: str = "image",
    artifact_scope: str = "image_preview",
) -> dict[str, object]:
    return {
        "source_relative_path": relative_path,
        "previous_source_relative_path": previous_relative_path,
        "change_status": change_status,
        "media_kind": media_kind,
        "artifact_scope": artifact_scope,
        "artifact_status": artifact_status,
        "artifact_action": artifact_action,
        "invalidation_reason": "test",
        "invalidate_preview": artifact_scope == "image_preview",
        "invalidate_video_frames": artifact_scope == "video_frames",
        "invalidate_unified_manifest": change_status != "unchanged",
        "delete_artifacts": False,
        "rebuild_artifacts": False,
        "source_deleted": change_status == "deleted",
        "source_read_only": True,
        "model_loaded": False,
        "v03_incremental_enabled": True,
        "v03_invalidation_enabled": True,
    }


def write_default_invalidation_plan(records: list[dict[str, object]]) -> Path:
    path = WORKSPACE / "stages/v0.3/manifests/artifact_invalidation_plan.jsonl"
    write_jsonl(path, records)
    return path


def run_resume(*, fresh_state: bool = False, state_manifest: Path | None = None) -> int:
    argv = ["v03-build-resume-plan", "--workspace", str(WORKSPACE)]
    if fresh_state:
        argv.append("--fresh-state")
    if state_manifest is not None:
        argv.extend(["--state-manifest", str(state_manifest)])
    return app.main(argv)


def task_by_path(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(record["source_relative_path"] or record["previous_source_relative_path"]): record
        for record in records
    }


def old_state_from_record(record: dict[str, object], status: str, *, retry_count: int = 0, max_retries: int = 3) -> dict[str, object]:
    return {
        "task_id": build_task_id(record),
        "source_relative_path": record.get("source_relative_path"),
        "previous_source_relative_path": record.get("previous_source_relative_path"),
        "change_status": record["change_status"],
        "media_kind": record["media_kind"],
        "artifact_scope": record["artifact_scope"],
        "artifact_status": record["artifact_status"],
        "artifact_action": record["artifact_action"],
        "task_stage": "v03_artifact_state",
        "task_status": status,
        "previous_task_status": "",
        "resume_action": "no_action",
        "retry_count": retry_count,
        "max_retries": max_retries,
        "last_error_code": "old_error" if status == "failed" else "",
        "last_error_message": "old failure" if status == "failed" else "",
        "state_reason": "old state",
        "state_observed_at": "2026-07-02T00:00:00+00:00",
        "checkpoint_tag_id": "old",
        "source_read_only": True,
        "model_loaded": False,
        "v03_incremental_enabled": True,
        "v03_invalidation_enabled": True,
        "v03_resume_enabled": True,
    }


def assert_no_execution_outputs() -> None:
    forbidden = [
        "previews",
        "preview",
        "frames",
        "video_frames",
        "image_timelapse_keyframes",
        "video_frames_by_source",
        "missing_artifact_plan.jsonl",
        "missing_artifact_summary.json",
    ]
    for name in forbidden:
        assert not (WORKSPACE / name).exists()


def test_v03_resume_fresh_state_rules_and_boundaries(monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.3-3 must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)
    monkeypatch.setattr(app, "run_preview_images", fail_external_process)
    monkeypatch.setattr(app, "run_extract_video_frames", fail_external_process)
    monkeypatch.setattr(app, "build_v02", fail_external_process)
    monkeypatch.setattr(app, "validate_real_minimal_v02", fail_external_process)

    clean_workspace()
    records = [
        invalidation_record("a/unchanged.jpg", "unchanged", "reusable", "no_action", artifact_scope="downstream_reference"),
        invalidation_record("a/new.jpg", "new", "new_source_pending_artifact", "mark_pending_create"),
        invalidation_record("b/base.jpg", "baseline", "new_source_pending_artifact", "mark_pending_create"),
        invalidation_record("a/modified.jpg", "modified", "needs_invalidation", "mark_invalid"),
        invalidation_record(
            "v/deleted.mov",
            "deleted",
            "source_deleted_reference_invalid",
            "mark_deleted_reference_invalid",
            previous_relative_path="v/deleted.mov",
            media_kind="video",
            artifact_scope="unified_manifest",
        ),
    ]
    write_default_invalidation_plan(records)

    assert run_resume(fresh_state=True) == 0

    state_path = WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl"
    resume_path = WORKSPACE / "stages/v0.3/manifests/resume_plan.jsonl"
    history_path = WORKSPACE / "stages/v0.3/state/run_history.jsonl"
    checkpoint_path = WORKSPACE / "stages/v0.3/state/checkpoint_tags.jsonl"
    summary_path = WORKSPACE / "stages/v0.3/reports/task_state_summary.json"
    summary_md_path = WORKSPACE / "stages/v0.3/reports/task_state_summary.md"
    assert state_path.exists()
    assert resume_path.exists()
    assert history_path.exists()
    assert checkpoint_path.exists()
    assert summary_path.exists()
    assert summary_md_path.exists()

    states = read_jsonl(state_path)
    resume = read_jsonl(resume_path)
    summary = read_json(summary_path)
    by_path = task_by_path(states)
    assert summary["total_task_state_records"] == summary["total_invalidation_records"] == len(records)
    assert summary["total_resume_records"] == len(records)
    assert summary["running_count"] == 0
    assert summary["checkpoint_created"] is True
    assert by_path["a/unchanged.jpg"]["task_status"] == "skipped_unchanged"
    assert by_path["a/unchanged.jpg"]["resume_action"] == "skip_unchanged"
    assert by_path["a/new.jpg"]["task_status"] == "pending"
    assert by_path["a/new.jpg"]["resume_action"] == "create_pending_task"
    assert by_path["b/base.jpg"]["task_status"] == "pending"
    assert by_path["b/base.jpg"]["resume_action"] == "create_pending_task"
    assert by_path["a/modified.jpg"]["task_status"] == "pending"
    assert by_path["a/modified.jpg"]["resume_action"] == "create_pending_task"
    assert by_path["v/deleted.mov"]["task_status"] == "invalidated"
    assert by_path["v/deleted.mov"]["resume_action"] == "mark_invalidated"
    assert all(record["execution_allowed_in_v03_3"] is False for record in resume)
    assert any(record["should_execute_later"] is True for record in resume)
    assert summary["model_loaded"] is False
    assert summary["source_read_only"] is True
    assert summary["action_taken"] == "record_resume_plan_only"
    assert "success is kept only when full task_id identity matches" in summary_md_path.read_text(encoding="utf-8")
    assert len(read_jsonl(history_path)) == 1
    checkpoints = read_jsonl(checkpoint_path)
    assert len(checkpoints) == 1
    assert checkpoints[0]["restore_supported"] is True
    assert checkpoints[0]["source_root"] is None
    assert_no_execution_outputs()


def test_old_running_success_failed_blocked_and_stale_success_transitions():
    clean_workspace()
    running = invalidation_record("a/running.jpg", "new", "new_source_pending_artifact", "mark_pending_create")
    success = invalidation_record("a/success.jpg", "new", "new_source_pending_artifact", "mark_pending_create")
    failed_retry = invalidation_record("a/failed_retry.jpg", "modified", "needs_invalidation", "mark_invalid")
    failed_blocked = invalidation_record("a/failed_blocked.jpg", "modified", "needs_invalidation", "mark_invalid")
    blocked = invalidation_record("a/blocked.jpg", "modified", "needs_invalidation", "mark_invalid")
    stale_old = invalidation_record("a/one.jpg", "unchanged", "reusable", "no_action", artifact_scope="downstream_reference")
    stale_current = invalidation_record("a/one.jpg", "modified", "needs_invalidation", "mark_invalid")
    records = [running, success, failed_retry, failed_blocked, blocked, stale_current]
    write_default_invalidation_plan(records)
    state_path = WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl"
    write_jsonl(
        state_path,
        [
            old_state_from_record(running, "running"),
            old_state_from_record(success, "success"),
            old_state_from_record(failed_retry, "failed", retry_count=1, max_retries=3),
            old_state_from_record(failed_blocked, "failed", retry_count=3, max_retries=3),
            old_state_from_record(blocked, "blocked"),
            old_state_from_record(stale_old, "success"),
        ],
    )

    assert run_resume() == 0

    states = task_by_path(read_jsonl(state_path))
    resume = task_by_path(read_jsonl(WORKSPACE / "stages/v0.3/manifests/resume_plan.jsonl"))
    summary = read_json(WORKSPACE / "stages/v0.3/reports/task_state_summary.json")
    assert states["a/running.jpg"]["previous_task_status"] == "running"
    assert states["a/running.jpg"]["task_status"] == "pending_retry"
    assert states["a/running.jpg"]["resume_action"] == "retry_after_interrupted"
    assert states["a/success.jpg"]["task_status"] == "success"
    assert states["a/success.jpg"]["resume_action"] == "keep_success"
    assert resume["a/success.jpg"]["should_execute_later"] is False
    assert states["a/failed_retry.jpg"]["previous_task_status"] == "failed"
    assert states["a/failed_retry.jpg"]["task_status"] == "pending_retry"
    assert states["a/failed_retry.jpg"]["resume_action"] == "retry_failed"
    assert states["a/failed_retry.jpg"]["retry_count"] == 2
    assert resume["a/failed_retry.jpg"]["should_execute_later"] is True
    assert states["a/failed_blocked.jpg"]["task_status"] == "blocked"
    assert states["a/failed_blocked.jpg"]["resume_action"] == "blocked_needs_manual_review"
    assert states["a/blocked.jpg"]["task_status"] == "blocked"
    assert states["a/blocked.jpg"]["resume_action"] == "blocked_needs_manual_review"
    assert states["a/one.jpg"]["task_status"] == "pending"
    assert states["a/one.jpg"]["resume_action"] == "create_pending_task"
    assert summary["running_count"] == 0
    assert summary["retry_after_interrupted_count"] >= 1
    assert summary["keep_success_count"] >= 1
    assert summary["retry_failed_count"] >= 1
    assert summary["blocked_needs_manual_review_count"] >= 2
    assert summary["stale_success_prevented_count"] >= 1
    assert all(record["execution_allowed_in_v03_3"] is False for record in resume.values())
    assert_no_execution_outputs()


def test_run_history_and_checkpoint_append_with_fresh_state_preserved():
    clean_workspace()
    records = [invalidation_record("a/new.jpg", "new", "new_source_pending_artifact", "mark_pending_create")]
    write_default_invalidation_plan(records)

    assert run_resume() == 0
    assert run_resume() == 0
    assert run_resume(fresh_state=True) == 0

    history = read_jsonl(WORKSPACE / "stages/v0.3/state/run_history.jsonl")
    checkpoints = read_jsonl(WORKSPACE / "stages/v0.3/state/checkpoint_tags.jsonl")
    assert len(history) == 3
    assert len(checkpoints) == 3
    assert len({record["checkpoint_tag_id"] for record in checkpoints}) == 3
    assert all(record["restore_supported"] is True for record in checkpoints)
    assert all(record["source_root"] is None for record in checkpoints)
    assert_no_execution_outputs()


def test_invalid_invalidation_input_does_not_pretend_to_pass():
    clean_workspace()
    bad_record = invalidation_record("a/bad.jpg", "new", "new_source_pending_artifact", "mark_pending_create")
    bad_record.pop("artifact_status")
    write_default_invalidation_plan([bad_record])

    assert run_resume() == 1

    states = read_jsonl(WORKSPACE / "stages/v0.3/state/task_state_manifest.jsonl")
    resume = read_jsonl(WORKSPACE / "stages/v0.3/manifests/resume_plan.jsonl")
    summary = read_json(WORKSPACE / "stages/v0.3/reports/task_state_summary.json")
    assert states == []
    assert resume == []
    assert summary["failed_input_count"] > 0
    assert summary["total_task_state_records"] == 0
    assert summary["total_resume_records"] == 0
    assert summary["model_loaded"] is False
    assert summary["running_count"] == 0
    assert_no_execution_outputs()
