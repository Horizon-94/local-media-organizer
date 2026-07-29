import json
import shutil
import subprocess
from pathlib import Path

from media_archive import app


TEST_ROOT = Path("/tmp/media_archive_v031_test")
SOURCE = TEST_ROOT / "source"
WORKSPACE = TEST_ROOT / "workspace"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_file(path: Path, content: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ns = (mtime_ns, mtime_ns)
    path.touch()
    import os

    os.utime(path, ns=ns)


def source_snapshot() -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(SOURCE).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in SOURCE.rglob("*")
        if path.is_file()
    }


def assert_source_unchanged(before: dict[str, tuple[int, int]]) -> None:
    assert source_snapshot() == before


def assert_no_downstream_outputs() -> None:
    assert not (WORKSPACE / "image_preview").exists()
    assert not (WORKSPACE / "video_frames").exists()
    assert not (WORKSPACE / "stages/v0.3/image_preview").exists()
    assert not (WORKSPACE / "stages/v0.3/video_frames").exists()


def status_by_path(plan: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["source_relative_path"]): record for record in plan}


def run_incremental(*, fresh: bool = False) -> int:
    argv = [
        "v03-incremental-scan",
        "--source",
        str(SOURCE),
        "--workspace",
        str(WORKSPACE),
    ]
    if fresh:
        argv.append("--fresh")
    return app.main(argv)


def test_v03_incremental_state_baseline_diff_and_idempotence(monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.3-1 must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)

    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    WORKSPACE.mkdir(parents=True)

    write_file(SOURCE / "a/one.jpg", b"one", 1_700_000_000_000_000_001)
    write_file(SOURCE / "a/two.mov", b"two-video", 1_700_000_000_000_000_002)
    write_file(SOURCE / "a/audio.wav", b"audio", 1_700_000_000_000_000_003)
    write_file(SOURCE / "a/readme.txt", b"readme", 1_700_000_000_000_000_004)

    before_fresh = source_snapshot()
    assert run_incremental(fresh=True) == 0
    assert_source_unchanged(before_fresh)

    state_dir = WORKSPACE / "stages/v0.3/state"
    manifest_dir = WORKSPACE / "stages/v0.3/manifests"
    reports_dir = WORKSPACE / "stages/v0.3/reports"
    current_path = state_dir / "source_snapshot_current.jsonl"
    previous_path = state_dir / "source_snapshot_previous.jsonl"
    plan_path = manifest_dir / "incremental_diff_plan.jsonl"
    summary_path = reports_dir / "incremental_summary.json"
    summary_md_path = reports_dir / "incremental_summary.md"

    assert current_path.exists()
    assert previous_path.exists()
    assert plan_path.exists()
    assert summary_path.exists()
    assert summary_md_path.exists()
    assert previous_path.read_text(encoding="utf-8") == ""

    current_records = read_jsonl(current_path)
    previous_records = read_jsonl(previous_path)
    plan = read_jsonl(plan_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert previous_records == []
    assert len(current_records) == 4
    assert {record["change_status"] for record in plan} == {"baseline"}
    assert all(record["action_hint"] == "register_new" for record in plan)
    assert all(record["v03_incremental_enabled"] is True for record in plan)
    assert all(record["source_read_only"] is True for record in plan)
    assert all(record["model_loaded"] is False for record in plan)
    assert {record["fingerprint_level"] for record in current_records} == {"stat_size_mtime"}
    assert summary["total_previous_files"] == 0
    assert summary["baseline_count"] == 4
    assert summary["v03_incremental_enabled"] is True
    assert summary["source_read_only"] is True
    assert summary["model_loaded"] is False
    assert summary["path_reused_or_reappeared_count"] == 0
    assert summary["action_taken"] == "record_incremental_plan_only"
    assert "sha256: not calculated" in summary_md_path.read_text(encoding="utf-8")
    assert "image_preview_generated: false" in summary_md_path.read_text(encoding="utf-8")
    assert "video_frames_extracted: false" in summary_md_path.read_text(encoding="utf-8")
    assert_no_downstream_outputs()

    first_current_text = current_path.read_text(encoding="utf-8")
    before_unchanged = source_snapshot()
    assert run_incremental() == 0
    assert_source_unchanged(before_unchanged)
    assert previous_path.read_text(encoding="utf-8") == first_current_text
    unchanged_plan = read_jsonl(plan_path)
    unchanged_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert {record["change_status"] for record in unchanged_plan} == {"unchanged"}
    assert all(record["action_hint"] == "no_action" for record in unchanged_plan)
    assert all(record["downstream_invalidate_preview"] is False for record in unchanged_plan)
    assert all(record["downstream_invalidate_video_frames"] is False for record in unchanged_plan)
    assert all(record["downstream_invalidate_unified_manifest"] is False for record in unchanged_plan)
    assert unchanged_summary["unchanged_count"] == 4
    assert unchanged_summary["failed_count"] == 0
    assert_no_downstream_outputs()

    write_file(SOURCE / "b/new.jpg", b"new", 1_700_000_000_000_000_005)
    write_file(SOURCE / "a/one.jpg", b"one-modified", 1_700_000_000_000_000_006)
    (SOURCE / "a/two.mov").unlink()
    before_changed_run = source_snapshot()
    assert run_incremental() == 0
    assert_source_unchanged(before_changed_run)
    changed = status_by_path(read_jsonl(plan_path))
    changed_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert changed["b/new.jpg"]["change_status"] == "new"
    assert changed["b/new.jpg"]["downstream_invalidate_preview"] is True
    assert changed["b/new.jpg"]["downstream_invalidate_video_frames"] is False
    assert changed["b/new.jpg"]["downstream_invalidate_unified_manifest"] is True
    assert changed["a/one.jpg"]["change_status"] == "modified"
    assert changed["a/one.jpg"]["previous_source_relative_path"] == "a/one.jpg"
    assert changed["a/one.jpg"]["action_hint"] == "reprocess_changed"
    assert changed["a/one.jpg"]["downstream_invalidate_preview"] is True
    assert changed["a/two.mov"]["change_status"] == "deleted"
    assert changed["a/two.mov"]["previous_source_relative_path"] == "a/two.mov"
    assert changed["a/two.mov"]["current_file_size"] is None
    assert changed["a/two.mov"]["current_mtime_ns"] is None
    assert changed["a/two.mov"]["action_hint"] == "mark_deleted"
    assert changed["a/audio.wav"]["change_status"] == "unchanged"
    assert changed_summary["new_count"] == 1
    assert changed_summary["modified_count"] == 1
    assert changed_summary["deleted_count"] == 1
    assert changed_summary["failed_count"] == 0
    assert_no_downstream_outputs()

    before_idempotent = source_snapshot()
    assert run_incremental() == 0
    assert_source_unchanged(before_idempotent)
    repeated = status_by_path(read_jsonl(plan_path))
    repeated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert repeated["b/new.jpg"]["change_status"] == "unchanged"
    assert repeated["a/one.jpg"]["change_status"] == "unchanged"
    assert "a/two.mov" not in repeated
    assert repeated_summary["deleted_count"] == 0
    assert repeated_summary["failed_count"] == 0
    assert repeated_summary["path_reused_or_reappeared_count"] == 0
    assert repeated_summary["model_loaded"] is False
    assert_no_downstream_outputs()
