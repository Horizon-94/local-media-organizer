import os
import json
import time
from pathlib import Path

from media_archive.preview.a9t_v3 import run_image_preview
from media_archive.preview.backends import SLOW_FAKE_SIPS_JPG, SLOW_FAKE_SYSTEM_PREVIEW_JPG


def _make_images(source: Path, count: int) -> None:
    source.mkdir(parents=True, exist_ok=True)
    jpg = b"\xff\xd8\xff\xe0slow-preview\xff\xd9"
    for index in range(count):
        path = source / f"img_{index:03d}.jpg"
        path.write_bytes(jpg)
        timestamp = 1_700_000_000_000_000_000 + index * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))


def test_slow_fake_sips_preview_backend_proves_concurrency(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    _make_images(source, 16)

    started = time.monotonic()
    summary = run_image_preview(source, output, SLOW_FAKE_SIPS_JPG)
    elapsed = time.monotonic() - started

    assert summary["preview_jobs_total"] == 16
    assert summary["preview_failed_count"] == 0
    assert 2 <= summary["actual_max_active_sips_workers"] <= 8
    assert summary["actual_max_active_system_preview_workers"] == 0
    assert elapsed < 16 * 0.2 * 0.70
    manifest = output / "image_preview/preview_manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 16
    assert [record["source_relative_path"] for record in records] == sorted(
        record["source_relative_path"] for record in records
    )
    assert not any(source.rglob("*.tmp"))


def test_slow_fake_system_preview_backend_proves_concurrency(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    _make_images(source, 16)

    started = time.monotonic()
    summary = run_image_preview(source, output, SLOW_FAKE_SYSTEM_PREVIEW_JPG)
    elapsed = time.monotonic() - started

    assert summary["preview_jobs_total"] == 16
    assert summary["preview_failed_count"] == 0
    assert summary["actual_max_active_sips_workers"] == 0
    assert 2 <= summary["actual_max_active_system_preview_workers"] <= 8
    assert elapsed < 16 * 0.2 * 0.70


def test_multiple_timelapse_candidates_filter_small_regular_bursts(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    jpg = b"\xff\xd8\xff\xe0grouping\xff\xd9"
    for directory, count in [("large_a", 60), ("large_b", 65), ("small_regular_1", 42), ("small_regular_2", 42)]:
        root = source / directory
        root.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            path = root / f"img_{index:03d}.jpg"
            path.write_bytes(jpg)
            timestamp = 1_700_000_000_000_000_000 + index * 2_000_000_000
            os.utime(path, ns=(timestamp, timestamp))

    summary = run_image_preview(source, output, SLOW_FAKE_SIPS_JPG)

    assert summary["total_image_files"] == 209
    assert summary["timelapse_sequence_count"] == 2
    assert summary["timelapse_member_image_count"] == 125
    assert summary["timelapse_keyframe_count"] == 6
    assert summary["normal_image_count"] == 84
    assert summary["preview_jobs_total"] == 90
