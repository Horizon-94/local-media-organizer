import json
import time
from pathlib import Path

from media_archive.video.r2j_fix_c4 import run_video_frame_extract
from media_archive.video.runners import SLOW_FAKE_FFMPEG_JPG, derive_expected_frame_count


def test_slow_fake_video_runner_proves_c4_concurrency(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    for index in range(8):
        (source / f"clip_{index:03d}.mov").write_bytes(b"video")

    started = time.monotonic()
    summary = run_video_frame_extract(source, output, SLOW_FAKE_FFMPEG_JPG, fake_frame_count=3)
    elapsed = time.monotonic() - started

    assert summary["total_video_files"] == 8
    assert summary["total_produced_frame_count"] == 24
    assert summary["frame_extract_status_counts"] == {"success": 8}
    assert summary["default_concurrency"] == 4
    assert summary["high_performance_concurrency"] == 6
    assert summary["high_performance_mode_used"] is False
    assert 2 <= summary["actual_max_active_video_workers"] <= 4
    assert elapsed < 8 * 0.2 * 0.75
    manifest = output / "video_frames/video_frame_manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 24
    assert [
        (record["source_video_relative_path"], record["frame_index"])
        for record in records
    ] == sorted((record["source_video_relative_path"], record["frame_index"]) for record in records)


def test_shared_frame_count_helper_uses_offset_and_inclusive_boundary():
    assert derive_expected_frame_count(999) == 0
    assert derive_expected_frame_count(1000) == 1
    assert derive_expected_frame_count(3000) == 2
    assert derive_expected_frame_count(5000) == 3
