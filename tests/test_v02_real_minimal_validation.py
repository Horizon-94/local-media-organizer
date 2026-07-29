import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from media_archive import app
from media_archive.preview.backends import TEST_COPY_JPG
from media_archive.validation.real_minimal_validation import (
    METRIC_MAPPING_TABLE,
    _archive_run_dir,
    _profile_or_blocked,
    derive_expected_frame_count,
    diff_snapshots,
    light_read_probe,
    snapshot_source,
)
from media_archive.video.runners import FAKE_FFMPEG_JPG


ROOT = Path(__file__).resolve().parents[1]


def _write_sample_sources(tmp_path):
    image_source = tmp_path / "图片 source（小样本）"
    video_source = tmp_path / "视频 source（小样本）"
    normal = image_source / "normal"
    timelapse = image_source / "timelapse"
    normal.mkdir(parents=True)
    timelapse.mkdir()
    video_source.mkdir(parents=True)
    jpg = b"\xff\xd8\xff\xe0small-jpg\xff\xd9"
    for index in range(3):
        (normal / f"normal_{index}.jpg").write_bytes(jpg)
    base_ns = 1_700_000_000_000_000_000
    for index in range(60):
        path = timelapse / f"tl_{index:03d}.jpg"
        path.write_bytes(jpg)
        timestamp = base_ns + index * 2_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
    (video_source / "a.mov").write_bytes(b"video-a")
    (video_source / "b.mp4").write_bytes(b"video-b")
    return image_source, video_source


def test_validate_real_minimal_v02_dry_run_writes_verdict_without_running_stages(tmp_path):
    image_source = tmp_path / "中文 image source（dry run）"
    video_source = tmp_path / "中文 video source（dry run）"
    image_source.mkdir()
    video_source.mkdir()
    (image_source / ".hidden").write_bytes(b"hidden")
    (image_source / "empty.jpg").write_bytes(b"")
    (image_source / "probe.jpg").write_bytes(b"probe-image")
    (video_source / "probe.mov").write_bytes(b"probe-video")
    output_root = tmp_path / "output"

    argv = [
        "validate-real-minimal-v02",
        "--image-source",
        str(image_source),
        "--video-source",
        str(video_source),
        "--output-root",
        str(output_root),
        "--dry-run",
    ]
    assert app.main(argv) == 0

    verdict_path = output_root / "reports/v02_real_minimal_verdict.json"
    full_path = output_root / "reports/v02_real_minimal_validation.json"
    assert verdict_path.exists()
    assert full_path.exists()
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    full = json.loads(full_path.read_text(encoding="utf-8"))

    assert verdict["schema_version"] == "1.1"
    assert verdict["validation_status"] == "DRY_RUN"
    assert verdict["real_image_validation_status"] == "DRY_RUN"
    assert verdict["real_video_validation_status"] == "DRY_RUN"
    assert verdict["real_3618_validation_run"] is False
    assert verdict["real_13gb_validation_run"] is False
    assert verdict["real_1_3tb_validation_run"] is False
    assert verdict["real_32tb_validation_run"] is False
    assert verdict["model_loaded"] is False
    assert verdict["v03_incremental_enabled"] is False
    assert full["preflight"]["image_source_raw_arg"] == str(image_source)
    assert full["preflight"]["video_source_raw_arg"] == str(video_source)
    assert full["preflight"]["image_source_light_read_probe_ok"] is True
    assert full["preflight"]["video_source_light_read_probe_ok"] is True
    assert full["preflight"]["image_source_light_read_probe"]["path"].endswith("probe.jpg")
    assert "output_preparation_plan" in full
    assert not (output_root / "image_a9t_real3618/run_clean_current/image_preview").exists()
    assert not (output_root / "video_r2j_real13gb/run_clean_current/video_frames").exists()


def test_validate_real_minimal_v02_small_expected_profile_passes(tmp_path):
    image_source, video_source = _write_sample_sources(tmp_path)
    output_root = tmp_path / "output"
    source_before = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in [*image_source.rglob("*"), *video_source.rglob("*")]
        if path.is_file()
    }

    argv = [
        "validate-real-minimal-v02",
        "--image-source",
        str(image_source),
        "--video-source",
        str(video_source),
        "--output-root",
        str(output_root),
        "--preview-backend",
        TEST_COPY_JPG,
        "--video-runner",
        FAKE_FFMPEG_JPG,
        "--expected-image-count",
        "63",
        "--expected-preview-count",
        "6",
        "--expected-video-count",
        "2",
        "--expected-frame-count",
        "6",
        "--expected-timelapse-sequence-count",
        "1",
        "--expected-timelapse-total-image-count",
        "60",
        "--expected-timelapse-keyframe-count",
        "3",
        "--expected-normal-image-count",
        "3",
    ]
    assert app.main(argv) == 0

    verdict_path = output_root / "reports/v02_real_minimal_verdict.json"
    full_path = output_root / "reports/v02_real_minimal_validation.json"
    markdown_path = ROOT / "docs/V0.2_REAL_MINIMAL_VALIDATION.md"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    full = json.loads(full_path.read_text(encoding="utf-8"))
    markdown_text = markdown_path.read_text(encoding="utf-8")

    assert verdict["validation_status"] == "PASS"
    assert verdict["real_image_validation_status"] == "PASS"
    assert verdict["real_video_validation_status"] == "PASS"
    assert verdict["source_safety_status"] == "PASS"
    assert verdict["actual_source_image_count"] == 63
    assert verdict["actual_high_confidence_timelapse_sequence_count"] == 1
    assert verdict["actual_timelapse_total_image_count"] == 60
    assert verdict["actual_timelapse_keyframe_count"] == 3
    assert verdict["actual_normal_image_count"] == 3
    assert verdict["actual_final_preview_count"] == 6
    assert verdict["actual_preview_reduction_count"] == 57
    assert verdict["actual_preview_reduction_ratio"] == 57 / 63
    assert verdict["actual_preview_reduction_ratio_formula"] == "preview_reduction_count / source_image_count"
    assert verdict["image_frozen_match_final_preview_count"] is True
    assert verdict["image_count_match_final_preview"] is None
    assert verdict["actual_video_files_total"] == 2
    assert verdict["actual_total_produced_frame_count"] == 6
    assert verdict["actual_success_video_count"] == 2
    assert verdict["actual_failed_video_count"] == 0
    assert verdict["actual_decode_mode"] == "videotoolbox"
    assert verdict["actual_concurrency"] == 4
    assert verdict["video_frozen_match_total_produced_frame_count"] is True
    assert verdict["video_count_match_frames"] is None
    assert verdict["image_internal_normal_plus_timelapse_equals_source"] is True
    assert verdict["image_internal_normal_plus_keyframes_equals_preview"] is True
    assert verdict["image_internal_reduction_count_ok"] is True
    assert verdict["video_all_frames_valid_jpg1280"] is True
    assert verdict["evidence_source_files_summary"]
    assert all("sha256" in item for item in verdict["evidence_source_files_summary"])
    assert "frozen.timelapse_sequence_count" in verdict["metric_mapping_table_summary"]
    assert METRIC_MAPPING_TABLE["frozen.image_extension_counts"]["audit_only"] is True
    assert "V0.2-5 Real Minimal Validation" in markdown_text
    assert "真实验收尚未执行" not in markdown_text
    assert "full_report_path" in verdict
    assert "frame_records" not in verdict
    assert "preview_records" not in verdict
    assert not (output_root / "speech").exists()
    assert not (output_root / "embeddings").exists()
    assert not (output_root / "search").exists()
    assert not (output_root / "index").exists()
    assert not (output_root / "database").exists()
    assert not (ROOT / "configs/models.local.json").exists()
    assert full["profile_name"] == "small_expected"

    source_after = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in [*image_source.rglob("*"), *video_source.rglob("*")]
        if path.is_file()
    }
    assert source_after == source_before


def test_real_frozen_profile_rejects_expected_overrides():
    result = _profile_or_blocked(False, {"expected_image_count": 1})
    assert result["ok"] is False
    assert "does not allow expected overrides" in result["reason"]


def test_archive_run_dir_suffix_retry_and_rename_failure(tmp_path, monkeypatch):
    run_dir = tmp_path / "run_clean_current"
    run_dir.mkdir()
    (run_dir / "old.txt").write_text("old", encoding="utf-8")
    base = "20260101T000000000000Z"
    (tmp_path / f"run_clean_current_archived_before_{base}").mkdir()
    archived = _archive_run_dir(run_dir, lambda: base)
    assert archived[0].endswith(f"{base}_001")
    assert not run_dir.exists()

    blocked_dir = tmp_path / "blocked_current"
    blocked_dir.mkdir()
    (blocked_dir / "old.txt").write_text("old", encoding="utf-8")

    original_rename = Path.rename

    def fail_rename(self, target):
        if self == blocked_dir:
            raise OSError("rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError):
        _archive_run_dir(blocked_dir, lambda: "20260101T000001000000Z")


def test_source_safety_snapshot_diff_nfc_and_ctime_warning(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nfd_name = "Cafe\u0301.jpg"
    path = source / nfd_name
    path.write_bytes(b"same")
    before = snapshot_source(source)
    after = snapshot_source(source)
    assert "Café.jpg" in before["records"]
    diff = diff_snapshots(before, after)
    assert diff["new_file_count"] == 0
    assert diff["deleted_file_count"] == 0

    changed = json.loads(json.dumps(after))
    changed["records"]["Café.jpg"]["ctime_ns"] += 1
    ctime_diff = diff_snapshots(before, changed)
    assert ctime_diff["ctime_changed_count"] == 1
    assert ctime_diff["ctime_warning_only"] is True

    size_changed = json.loads(json.dumps(after))
    size_changed["records"]["Café.jpg"]["size_bytes"] += 1
    fail_diff = diff_snapshots(before, size_changed)
    assert fail_diff["size_changed_count"] == 1
    assert fail_diff["ctime_warning_only"] is False


def test_light_read_probe_and_derived_frame_count(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".hidden").write_bytes(b"hidden")
    (source / "empty.mov").write_bytes(b"")
    (source / "probe.mov").write_bytes(b"probe")
    probe = light_read_probe(source)
    assert probe["ok"] is True
    assert probe["path"].endswith("probe.mov")
    assert 1 <= probe["bytes_read"] <= 4096
    assert derive_expected_frame_count(1000) == 1
    assert derive_expected_frame_count(5000) == 3
    assert derive_expected_frame_count(999) == 0
