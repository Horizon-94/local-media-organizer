import json
import os
import subprocess
from pathlib import Path

from media_archive import app
from media_archive import config
from media_archive.preview.backends import (
    SIPS_DIRECT_JPG,
    SLOW_FAKE_SIPS_JPG,
    SLOW_FAKE_SYSTEM_PREVIEW_JPG,
    SYSTEM_PREVIEW_THEN_SIPS,
    TEST_COPY_JPG,
    select_backend,
)
from media_archive.quality.quality_records import (
    build_path_duplicate_manifest_for_test,
    build_unreadable_exception_for_test,
)
from media_archive.unified.builder import choose_primary_duplicate_group
from media_archive.video.runners import FAKE_FFMPEG_JPG, build_ffmpeg_command
from media_archive.workflows.v02_build import STAGE_EXPECTED_ARTIFACTS


ROOT = Path(__file__).resolve().parents[1]


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def test_required_documents_exist():
    required = [
        "README.md",
        "PROJECT_BASELINE.md",
        "AGENTS.md",
        "BACKLOG.md",
        "docs/V0_ROUTE.md",
        "docs/V0.1_FREEZE.md",
        "docs/V0.1_R2I_LIGHT_SCAN_EVIDENCE.md",
        "docs/V0.2_A9T-v3_IMAGE_PREVIEW_FREEZE.md",
        "docs/V0.2_R2J-FIX-C4_VIDEO_FRAME_FREEZE.md",
        "docs/V0.2_REMAINING.md",
        "docs/V0.2_VISUAL_REVIEW_PACK.md",
        "docs/MODEL_STORAGE_POLICY.md",
    ]
    assert all((ROOT / path).exists() for path in required)


def test_frozen_strategy_constants():
    assert config.IMAGE_PREVIEW_STRATEGY == "A9T-v3"
    assert config.VIDEO_FRAME_STRATEGY == "R2J-FIX-C4"
    assert config.VIDEO_DEFAULT_CONCURRENCY == 4
    assert config.VIDEO_HIGH_PERFORMANCE_CONCURRENCY == 6
    assert config.VIDEO_HIGH_PERFORMANCE_CONCURRENCY != config.VIDEO_DEFAULT_CONCURRENCY


def test_remaining_v02_documents_blockers():
    text = (ROOT / "docs/V0.2_REMAINING.md").read_text(encoding="utf-8")
    assert "V0.2 小样本正式入口已通过" in text
    assert "V0.2-5 schema_version 1.1 真实素材最小验收已 PASS" in text
    assert "Visual Review Pack 作为 audit/display 产物" in text
    assert "不重新运行 A9T-v3" in text
    assert "不重新运行 C4" in text
    assert "图片+视频组合验收未完成" not in text
    assert "统一 manifest 未完成" not in text
    assert "不改变 V0.2-5 PASS 判定" in text
    assert "V0.3 尚未开始" in text


def test_v01_r2i_light_scan_evidence_document():
    text = (ROOT / "docs/V0.1_R2I_LIGHT_SCAN_EVIDENCE.md").read_text(
        encoding="utf-8"
    )
    assert "light_scan_default" in text
    assert "ordinary_video_integrity_check: 0" in text
    assert "ordinary_video_sample_decode: 0" in text
    assert "sample_decode_default_enabled: false" in text
    assert "deep_integrity_default_enabled: false" in text
    assert "不证明 V0.6 完整完成" in text
    assert "不替代 A9T-v3" in text
    assert "不替代 R2J-FIX-C4" in text


def test_v01_scan_cli_writes_stable_light_manifest(tmp_path, monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("V0.1 scan must not call external processes")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)

    source = tmp_path / "source"
    output = tmp_path / "output"
    nested = source / "nested"
    nested.mkdir(parents=True)

    inputs = {
        "a.jpg": b"jpg",
        "b.JPG": b"upper jpg",
        "c.heic": b"heic",
        "d.mov": b"mov",
        "e.wav": b"wav",
        "f.txt": b"text",
        "nested/g.png": b"nested png",
    }
    for relative_path, content in inputs.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    before = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }

    argv = ["scan", "--source", str(source), "--output", str(output)]
    assert app.main(argv) == 0

    manifest_path = output / "manifests" / "media_manifest.jsonl"
    summary_path = output / "manifests" / "scan_summary.json"
    assert manifest_path.exists()
    assert summary_path.exists()

    manifest_text = manifest_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in manifest_text.splitlines()]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    manifest_fields = {
        "source_root",
        "source_path",
        "source_relative_path",
        "file_name",
        "extension",
        "media_type",
        "size_bytes",
        "mtime_ns",
        "scan_status",
        "scan_policy",
    }
    assert all(manifest_fields <= set(record) for record in records)

    summary_fields = {
        "source_root",
        "output_dir",
        "scan_policy",
        "source_read_only",
        "total_files",
        "image_files",
        "video_files",
        "audio_files",
        "other_files",
        "manifest_path",
        "metadata_probe_policy",
        "metadata_probe_enabled",
        "deep_integrity_default_enabled",
        "sample_decode_default_enabled",
        "full_integrity_check_default_enabled",
        "ordinary_video_integrity_check",
        "ordinary_video_sample_decode",
        "preview_generated",
        "video_frames_generated",
    }
    assert summary_fields <= set(summary)

    assert summary["total_files"] == 7
    assert summary["image_files"] == 4
    assert summary["video_files"] == 1
    assert summary["audio_files"] == 1
    assert summary["other_files"] == 1

    relative_paths = [record["source_relative_path"] for record in records]
    assert relative_paths == sorted(relative_paths)
    assert relative_paths == [
        "a.jpg",
        "b.JPG",
        "c.heic",
        "d.mov",
        "e.wav",
        "f.txt",
        "nested/g.png",
    ]

    assert all(Path(record["source_root"]).is_absolute() for record in records)
    assert all(Path(record["source_path"]).is_absolute() for record in records)
    assert all(record["extension"] == record["extension"].lower() for record in records)
    assert {record["scan_policy"] for record in records} == {"light_scan_default"}
    assert {record["scan_status"] for record in records} == {"success"}

    media_by_path = {
        record["source_relative_path"]: record["media_type"] for record in records
    }
    assert media_by_path == {
        "a.jpg": "image",
        "b.JPG": "image",
        "c.heic": "image",
        "d.mov": "video",
        "e.wav": "audio",
        "f.txt": "other",
        "nested/g.png": "image",
    }

    assert summary["scan_policy"] == "light_scan_default"
    assert summary["source_read_only"] is True
    assert summary["metadata_probe_policy"] == (
        "documented_not_implemented_in_this_task"
    )
    assert summary["metadata_probe_enabled"] is False
    assert summary["deep_integrity_default_enabled"] is False
    assert summary["sample_decode_default_enabled"] is False
    assert summary["full_integrity_check_default_enabled"] is False
    assert summary["ordinary_video_integrity_check"] == 0
    assert summary["ordinary_video_sample_decode"] == 0
    assert summary["preview_generated"] is False
    assert summary["video_frames_generated"] is False

    assert not (output / "preview").exists()
    assert not (output / "previews").exists()
    assert not (output / "video_frame_jpg").exists()
    assert not (output / "keyframe_pool_jpg").exists()
    assert not (output / "normal_preview_pool_jpg").exists()
    assert not (output / "cache").exists()
    assert not (output / "index").exists()
    assert not (output / "database").exists()
    assert not (ROOT / "configs/models.local.json").exists()

    after = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before

    assert app.main(argv) == 0
    assert manifest_path.read_text(encoding="utf-8") == manifest_text
    repeated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert repeated_summary["total_files"] == summary["total_files"]


def test_a9t_v3_image_preview_strategy_with_test_backend(tmp_path, monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("A9T-v3 pytest path must use test_copy_jpg")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)

    assert select_backend(".JPG", "auto") == SIPS_DIRECT_JPG
    assert select_backend(".arw", "auto") == SYSTEM_PREVIEW_THEN_SIPS
    assert select_backend(".ARW", TEST_COPY_JPG) == TEST_COPY_JPG
    assert select_backend(".jpg", SLOW_FAKE_SIPS_JPG) == SLOW_FAKE_SIPS_JPG
    assert select_backend(".jpg", SLOW_FAKE_SYSTEM_PREVIEW_JPG) == SLOW_FAKE_SYSTEM_PREVIEW_JPG

    source = tmp_path / "source"
    output = tmp_path / "output"
    normal = source / "normal"
    timelapse = source / "timelapse"
    mixed = source / "mixed"
    normal.mkdir(parents=True)
    timelapse.mkdir()
    mixed.mkdir()

    jpg_bytes = b"\xff\xd8\xff\xe0minimal-jpeg\xff\xd9"
    for name in ["normal_1.jpg", "normal_2.jpg", "normal_3.jpg"]:
        (normal / name).write_bytes(jpg_bytes)
    (mixed / "upper.JPG").write_bytes(jpg_bytes)
    (source / "notes.txt").write_text("not an image", encoding="utf-8")
    (source / "clip.mov").write_bytes(b"not processed")

    raw_placeholder = tmp_path / "raw_backend_probe.ARW"
    raw_placeholder.write_bytes(b"raw placeholder")

    base_ns = 1_700_000_000_000_000_000
    step_ns = 2_000_000_000
    for index in range(60):
        path = timelapse / f"tl_{index + 1:03d}.jpg"
        path.write_bytes(jpg_bytes)
        timestamp_ns = base_ns + index * step_ns
        os.utime(path, ns=(timestamp_ns, timestamp_ns))

    before = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }

    argv = [
        "preview-images",
        "--source",
        str(source),
        "--output",
        str(output),
        "--preview-backend",
        TEST_COPY_JPG,
    ]
    assert app.main(argv) == 0

    preview_root = output / "image_preview"
    summary_path = preview_root / "image_preview_summary.json"
    decisions_path = preview_root / "all_image_decisions.jsonl"
    preview_manifest_path = preview_root / "preview_manifest.jsonl"
    keyframe_manifest_path = preview_root / "keyframe_pool_manifest.jsonl"
    sequences_path = preview_root / "timelapse_sequences.json"
    assert summary_path.exists()
    assert decisions_path.exists()
    assert preview_manifest_path.exists()
    assert keyframe_manifest_path.exists()
    assert sequences_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    decisions = [
        json.loads(line) for line in decisions_path.read_text(encoding="utf-8").splitlines()
    ]
    preview_records = [
        json.loads(line)
        for line in preview_manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    keyframe_records = [
        json.loads(line)
        for line in keyframe_manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    sequences = json.loads(sequences_path.read_text(encoding="utf-8"))

    decision_fields = {
        "source_root",
        "source_path",
        "source_relative_path",
        "file_name",
        "extension",
        "image_preview_strategy",
        "group_key",
        "is_timelapse_member",
        "timelapse_group_id",
        "timelapse_role",
        "preview_required",
        "preview_reason",
        "backend",
        "scan_status",
    }
    assert all(decision_fields <= set(record) for record in decisions)
    preview_fields = {
        "source_root",
        "source_path",
        "source_relative_path",
        "preview_path",
        "preview_relative_path",
        "preview_pool",
        "image_preview_strategy",
        "backend",
        "max_edge_px",
        "output_format",
        "preview_status",
    }
    assert all(preview_fields <= set(record) for record in preview_records)

    assert summary["image_preview_strategy"] == "A9T-v3"
    assert summary["total_image_files"] == 64
    assert summary["timelapse_sequence_count"] == 1
    assert summary["timelapse_member_image_count"] == 60
    assert summary["timelapse_keyframe_count"] == 3
    assert summary["timelapse_member_skipped_count"] == 57
    assert summary["normal_image_count"] == 4
    assert summary["preview_jobs_total"] == 7
    assert summary["preview_success_count"] == 7
    assert summary["preview_failed_count"] == 0
    assert summary["max_edge_px"] == 1280
    assert summary["final_output_format"] == "jpg"
    assert summary["sips_workers"] == 8
    assert summary["system_preview_workers"] == 8
    assert summary["source_read_only"] is True
    assert summary["video_processed"] is False
    assert summary["audio_processed"] is False
    assert summary["model_loaded"] is False
    assert summary["real_3618_validation_run"] is False

    assert len(decisions) == 64
    assert [record["source_relative_path"] for record in decisions] == sorted(
        record["source_relative_path"] for record in decisions
    )
    assert all(record["extension"] == record["extension"].lower() for record in decisions)
    assert {record["image_preview_strategy"] for record in decisions} == {"A9T-v3"}
    assert {record["backend"] for record in decisions} == {TEST_COPY_JPG}
    assert "notes.txt" not in {record["source_relative_path"] for record in decisions}
    assert "clip.mov" not in {record["source_relative_path"] for record in decisions}
    assert any(record["extension"] == ".jpg" for record in decisions)

    timelapse_decisions = [
        record for record in decisions if record["source_relative_path"].startswith("timelapse/")
    ]
    assert len(timelapse_decisions) == 60
    assert sum(record["preview_required"] for record in timelapse_decisions) == 3
    assert {
        record["timelapse_role"]
        for record in timelapse_decisions
        if record["preview_required"]
    } == {"first", "middle", "last"}
    assert all(
        record["preview_required"] is False
        for record in timelapse_decisions
        if record["timelapse_role"] == "member"
    )

    assert len(preview_records) == 7
    assert [record["source_relative_path"] for record in preview_records] == sorted(
        record["source_relative_path"] for record in preview_records
    )
    assert {record["preview_pool"] for record in preview_records} == {
        "keyframe_pool_jpg",
        "normal_preview_pool_jpg",
    }
    assert all(record["max_edge_px"] == 1280 for record in preview_records)
    assert all(record["output_format"] == "jpg" for record in preview_records)
    assert all(record["preview_status"] == "success" for record in preview_records)
    assert all(Path(record["preview_path"]).exists() for record in preview_records)
    assert len(keyframe_records) == 3
    assert {record["timelapse_role"] for record in keyframe_records} == {
        "first",
        "middle",
        "last",
    }

    assert len(sequences) == 1
    assert sequences[0]["member_count"] == 60
    assert sequences[0]["keyframe_count"] == 3
    assert sequences[0]["detection_method"] == "file_mtime"
    assert sequences[0]["confidence"] == "high"
    assert sequences[0]["keyframe_roles"] == ["first", "middle", "last"]

    assert not (output / "video_frame_jpg").exists()
    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "database").exists()
    assert not (output / "index").exists()
    assert not (ROOT / "configs/models.local.json").exists()

    after = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before

    assert app.main(argv) == 0
    repeated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    repeated_preview_records = [
        json.loads(line)
        for line in preview_manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    for key in [
        "timelapse_sequence_count",
        "timelapse_member_image_count",
        "timelapse_keyframe_count",
        "timelapse_member_skipped_count",
        "normal_image_count",
        "preview_jobs_total",
        "preview_success_count",
        "preview_failed_count",
    ]:
        assert repeated_summary[key] == summary[key]
    assert len(repeated_preview_records) == len(preview_records)


def test_r2j_fix_c4_video_frame_strategy_with_fake_runner(tmp_path, monkeypatch):
    def fail_external_process(*args, **kwargs):
        raise AssertionError("R2J-FIX-C4 pytest path must use fake_ffmpeg_jpg")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)

    source = tmp_path / "source"
    output = tmp_path / "output"
    nested = source / "nested"
    nested.mkdir(parents=True)
    inputs = {
        "a.mov": b"fake mov",
        "b.mp4": b"fake mp4",
        "Upper.MOV": b"fake upper mov",
        "nested/c.mkv": b"fake mkv",
        "image.jpg": b"not processed",
        "audio.wav": b"not processed",
        "notes.txt": b"not processed",
    }
    for relative_path, content in inputs.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    before = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }

    argv = [
        "extract-video-frames",
        "--source",
        str(source),
        "--output",
        str(output),
        "--video-runner",
        FAKE_FFMPEG_JPG,
        "--fake-frame-count",
        "3",
    ]
    assert app.main(argv) == 0

    video_root = output / "video_frames"
    summary_path = video_root / "video_frame_summary.json"
    manifest_path = video_root / "video_frame_manifest.jsonl"
    report_path = video_root / "video_extract_report.jsonl"
    assert summary_path.exists()
    assert manifest_path.exists()
    assert report_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    frame_records = [
        json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    report_records = [
        json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()
    ]

    summary_fields = {
        "video_frame_strategy",
        "source_root",
        "output_dir",
        "total_video_files",
        "total_produced_frame_count",
        "total_valid_jpg1280_count",
        "total_invalid_jpg_count",
        "frame_extract_status_counts",
        "default_concurrency",
        "high_performance_concurrency",
        "decode_mode",
        "ffmpeg_extra_args",
        "sampling_offset_ms",
        "sampling_interval_ms",
        "max_edge_px",
        "final_output_format",
        "fallback_attempted_count",
        "showinfo_used_count",
        "source_read_only",
        "image_processed",
        "audio_processed",
        "model_loaded",
        "real_13gb_validation_run",
    }
    assert summary_fields <= set(summary)
    frame_fields = {
        "source_root",
        "source_video_path",
        "source_video_relative_path",
        "frame_file",
        "frame_path",
        "frame_relative_path",
        "frame_index",
        "estimated_frame_time_ms",
        "sampling_offset_ms",
        "sampling_interval_ms",
        "video_frame_strategy",
        "decode_mode",
        "concurrency",
        "max_edge_px",
        "output_format",
        "frame_status",
    }
    assert all(frame_fields <= set(record) for record in frame_records)
    report_fields = {
        "source_root",
        "source_video_path",
        "source_video_relative_path",
        "video_frame_strategy",
        "decode_mode",
        "ffmpeg_hwaccel",
        "concurrency",
        "sampling_offset_ms",
        "sampling_interval_ms",
        "max_edge_px",
        "output_format",
        "expected_frame_count_estimate",
        "produced_frame_count",
        "tolerated_boundary_difference",
        "fallback_attempted",
        "showinfo_used",
        "extract_status",
        "error_message",
    }
    assert all(report_fields <= set(record) for record in report_records)

    assert summary["video_frame_strategy"] == "R2J-FIX-C4"
    assert summary["total_video_files"] == 4
    assert summary["total_produced_frame_count"] == 12
    assert summary["total_valid_jpg1280_count"] == 12
    assert summary["total_invalid_jpg_count"] == 0
    assert summary["frame_extract_status_counts"] == {"success": 4}
    assert summary["default_concurrency"] == 4
    assert summary["high_performance_concurrency"] == 6
    assert summary["default_concurrency"] != summary["high_performance_concurrency"]
    assert summary["decode_mode"] == "videotoolbox"
    assert summary["ffmpeg_extra_args"] == ["-hwaccel", "videotoolbox"]
    assert summary["sampling_offset_ms"] == 1000
    assert summary["sampling_interval_ms"] == 2000
    assert summary["max_edge_px"] == 1280
    assert summary["final_output_format"] == "jpg"
    assert summary["fallback_attempted_count"] == 0
    assert summary["showinfo_used_count"] == 0
    assert summary["source_read_only"] is True
    assert summary["image_processed"] is False
    assert summary["audio_processed"] is False
    assert summary["model_loaded"] is False
    assert summary["real_13gb_validation_run"] is False

    assert len(report_records) == 4
    assert [record["source_video_relative_path"] for record in report_records] == sorted(
        record["source_video_relative_path"] for record in report_records
    )
    assert {record["produced_frame_count"] for record in report_records} == {3}
    assert {record["extract_status"] for record in report_records} == {"success"}
    assert {record["fallback_attempted"] for record in report_records} == {False}
    assert {record["showinfo_used"] for record in report_records} == {False}
    assert {record["error_message"] for record in report_records} == {""}

    assert len(frame_records) == 12
    assert [
        (record["source_video_relative_path"], record["frame_index"])
        for record in frame_records
    ] == sorted(
        (record["source_video_relative_path"], record["frame_index"])
        for record in frame_records
    )
    assert {record["video_frame_strategy"] for record in frame_records} == {
        "R2J-FIX-C4"
    }
    assert {record["decode_mode"] for record in frame_records} == {"videotoolbox"}
    assert {record["concurrency"] for record in frame_records} == {4}
    assert {record["max_edge_px"] for record in frame_records} == {1280}
    assert {record["output_format"] for record in frame_records} == {"jpg"}
    assert {record["frame_status"] for record in frame_records} == {"success"}
    assert all(Path(record["source_root"]).is_absolute() for record in frame_records)
    assert all(Path(record["source_video_path"]).is_absolute() for record in frame_records)
    assert all(Path(record["frame_path"]).is_absolute() for record in frame_records)
    assert all(Path(record["frame_path"]).exists() for record in frame_records)
    assert all(
        record["estimated_frame_time_ms"]
        == 1000 + record["frame_index"] * 2000
        for record in frame_records
    )
    assert "image.jpg" not in {
        record["source_video_relative_path"] for record in frame_records
    }
    assert "audio.wav" not in {
        record["source_video_relative_path"] for record in frame_records
    }

    assert not (output / "image_preview").exists()
    assert not (output / "keyframe_pool_jpg").exists()
    assert not (output / "normal_preview_pool_jpg").exists()
    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "database").exists()
    assert not (output / "index").exists()
    assert not (ROOT / "configs/models.local.json").exists()

    after = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before

    assert app.main(argv) == 0
    repeated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    repeated_frame_records = [
        json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    for key in [
        "total_video_files",
        "total_produced_frame_count",
        "total_valid_jpg1280_count",
        "total_invalid_jpg_count",
        "frame_extract_status_counts",
    ]:
        assert repeated_summary[key] == summary[key]
    assert len(repeated_frame_records) == len(frame_records)


def test_r2j_fix_c4_ffmpeg_command_shape(tmp_path):
    source = tmp_path / "clip.mov"
    output_pattern = tmp_path / "frames" / "clip_%06d.jpg"
    command = build_ffmpeg_command(source, output_pattern)
    command_text = " ".join(command)

    assert command[0] == "ffmpeg"
    assert "-hwaccel" in command
    assert "videotoolbox" in command
    assert "showinfo" not in command_text
    assert "fallback" not in command_text
    assert "-ss" not in command
    assert "fps=1/2.0:start_time=1.0" in command_text
    assert "1280" in command_text
    assert "-q:v" in command
    assert "3" in command
    assert str(output_pattern).endswith(".jpg")


def test_v023a_quality_records_cli_outputs_stable_manifests(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    checks = tmp_path / "expected_output_checks.jsonl"
    for directory in ["a", "b", "raw", "nested"]:
        (source / directory).mkdir(parents=True, exist_ok=True)

    inputs = {
        "a/one.jpg": b"same-content",
        "b/one_copy.jpg": b"same-content",
        "a/one.mov": b"same-video-content",
        "b/one_copy.mov": b"same-video-content",
        "raw/A001.braw": b"same-raw-content",
        "raw/A001_copy.BRAW": b"same-raw-content",
        "a/empty.mp4": b"",
        "a/empty_unsupported.xyz": b"",
        "a/readme.xyz": b"not-supported",
        "raw/C001.CRM": b"canon-raw",
        "raw/G001.GPR": b"gopro-raw",
        "a/sound.wav": b"sound",
        "a/unique.jpg": b"unique-image",
        "nested/sub.jpg": b"nested-image",
    }
    for relative_path, content in inputs.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    symlink_path = source / "a" / "linked_one.jpg"
    try:
        os.symlink(source / "a" / "one.jpg", symlink_path)
        symlink_created = True
    except (OSError, NotImplementedError):
        symlink_created = False

    checks.write_text(
        json.dumps(
            {
                "source_relative_path": "a/one.jpg",
                "stage": "test_expected_preview",
                "expected_output": str(output / "missing_preview.jpg"),
                "actual_output": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    before = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    argv = [
        "detect-duplicates-exceptions",
        "--source",
        str(source),
        "--output",
        str(output),
        "--expected-output-checks",
        str(checks),
    ]
    assert app.main(argv) == 0

    quality_root = output / "quality"
    duplicate_manifest_path = quality_root / "duplicate_manifest.jsonl"
    duplicate_summary_path = quality_root / "duplicate_summary.json"
    exception_manifest_path = quality_root / "exception_manifest.jsonl"
    exception_summary_path = quality_root / "exception_summary.json"
    assert duplicate_manifest_path.exists()
    assert duplicate_summary_path.exists()
    assert exception_manifest_path.exists()
    assert exception_summary_path.exists()

    duplicate_text = duplicate_manifest_path.read_text(encoding="utf-8")
    exception_text = exception_manifest_path.read_text(encoding="utf-8")
    duplicate_records = [json.loads(line) for line in duplicate_text.splitlines()]
    exception_records = [json.loads(line) for line in exception_text.splitlines()]
    duplicate_summary = json.loads(duplicate_summary_path.read_text(encoding="utf-8"))
    exception_summary = json.loads(exception_summary_path.read_text(encoding="utf-8"))

    duplicate_fields = {
        "source_root",
        "duplicate_group_id",
        "duplicate_type",
        "content_hash",
        "content_size_bytes",
        "member_count",
        "representative_path",
        "representative_relative_path",
        "member_paths",
        "member_relative_paths",
        "media_type",
        "duplicate_reason",
        "source_read_only",
        "action_taken",
    }
    assert all(duplicate_fields <= set(record) for record in duplicate_records)
    exception_fields = {
        "source_root",
        "source_path",
        "source_relative_path",
        "extension",
        "media_type",
        "stage",
        "error_code",
        "error_subtype",
        "error_message",
        "expected_output",
        "actual_output",
        "recoverable",
        "next_action",
        "source_read_only",
        "action_taken",
    }
    assert all(exception_fields <= set(record) for record in exception_records)

    duplicate_summary_fields = {
        "source_root",
        "output_dir",
        "duplicate_detection_version",
        "total_files_seen",
        "files_hashed",
        "content_duplicate_group_count",
        "path_duplicate_group_count",
        "duplicate_member_file_count",
        "hash_algorithm",
        "source_read_only",
        "action_taken",
        "unified_manifest_generated",
        "v03_incremental_enabled",
        "model_loaded",
    }
    assert duplicate_summary_fields <= set(duplicate_summary)
    exception_summary_fields = {
        "source_root",
        "output_dir",
        "exception_detection_version",
        "total_files_seen",
        "exception_count",
        "zero_size_count",
        "unsupported_extension_count",
        "known_raw_video_unsupported_count",
        "known_raw_image_unsupported_count",
        "unreadable_count",
        "output_missing_count",
        "source_read_only",
        "action_taken",
        "unified_manifest_generated",
        "v03_incremental_enabled",
        "model_loaded",
    }
    assert exception_summary_fields <= set(exception_summary)

    assert duplicate_summary["duplicate_detection_version"] == "V0.2-3A"
    assert duplicate_summary["hash_algorithm"] == "sha256"
    assert duplicate_summary["content_duplicate_group_count"] >= 3
    assert duplicate_summary["path_duplicate_group_count"] == 0
    assert duplicate_summary["duplicate_member_file_count"] >= 6
    assert duplicate_summary["total_files_seen"] == exception_summary["total_files_seen"]
    assert duplicate_summary["total_files_seen"] == len(inputs)
    assert duplicate_summary["files_hashed"] == (
        duplicate_summary["total_files_seen"]
        - exception_summary["zero_size_count"]
        - exception_summary["unreadable_count"]
    )
    assert duplicate_summary["source_read_only"] is True
    assert duplicate_summary["action_taken"] == "record_only"
    assert duplicate_summary["unified_manifest_generated"] is False
    assert duplicate_summary["v03_incremental_enabled"] is False
    assert duplicate_summary["model_loaded"] is False

    assert exception_summary["exception_detection_version"] == "V0.2-3A"
    assert exception_summary["zero_size_count"] >= 2
    assert exception_summary["unsupported_extension_count"] >= 6
    assert exception_summary["known_raw_video_unsupported_count"] >= 3
    assert exception_summary["known_raw_image_unsupported_count"] >= 1
    assert (
        exception_summary["known_raw_video_unsupported_count"]
        + exception_summary["known_raw_image_unsupported_count"]
        <= exception_summary["unsupported_extension_count"]
    )
    assert exception_summary["unreadable_count"] == 0
    assert exception_summary["output_missing_count"] >= 1
    assert exception_summary["source_read_only"] is True
    assert exception_summary["action_taken"] == "record_only"
    assert exception_summary["unified_manifest_generated"] is False
    assert exception_summary["v03_incremental_enabled"] is False
    assert exception_summary["model_loaded"] is False

    assert [record["duplicate_group_id"] for record in duplicate_records] == sorted(
        record["duplicate_group_id"] for record in duplicate_records
    )
    assert all(
        record["duplicate_group_id"]
        == f"content:{record['content_hash'].removeprefix('sha256:')[:16]}"
        for record in duplicate_records
        if record["duplicate_type"] == "content_duplicate"
    )
    member_sets = [set(record["member_relative_paths"]) for record in duplicate_records]
    assert {"a/one.jpg", "b/one_copy.jpg"} in member_sets
    assert {"a/one.mov", "b/one_copy.mov"} in member_sets
    assert {"raw/A001.braw", "raw/A001_copy.BRAW"} in member_sets

    assert [
        (
            record["source_relative_path"],
            record["error_code"],
            record["expected_output"],
        )
        for record in exception_records
    ] == sorted(
        (
            record["source_relative_path"],
            record["error_code"],
            record["expected_output"],
        )
        for record in exception_records
    )
    error_codes = {record["error_code"] for record in exception_records}
    assert {"zero_size", "unsupported_extension", "output_missing"} <= error_codes
    records_by_path_code = {
        (record["source_relative_path"], record["error_code"]): record
        for record in exception_records
    }
    assert ("a/empty_unsupported.xyz", "zero_size") in records_by_path_code
    assert ("a/empty_unsupported.xyz", "unsupported_extension") in records_by_path_code
    assert records_by_path_code[("raw/A001.braw", "unsupported_extension")][
        "error_subtype"
    ] == "known_raw_video_unsupported"
    assert records_by_path_code[("raw/A001_copy.BRAW", "unsupported_extension")][
        "error_subtype"
    ] == "known_raw_video_unsupported"
    assert records_by_path_code[("raw/C001.CRM", "unsupported_extension")][
        "error_subtype"
    ] == "known_raw_video_unsupported"
    assert records_by_path_code[("raw/C001.CRM", "unsupported_extension")][
        "extension"
    ] == ".crm"
    assert records_by_path_code[("raw/G001.GPR", "unsupported_extension")][
        "error_subtype"
    ] == "known_raw_image_unsupported"
    assert records_by_path_code[("raw/G001.GPR", "unsupported_extension")][
        "extension"
    ] == ".gpr"
    assert records_by_path_code[("a/one.jpg", "output_missing")][
        "next_action"
    ] == "retry_later"
    assert all(record["action_taken"] == "record_only" for record in exception_records)
    assert all(record["source_read_only"] is True for record in exception_records)

    assert not (output / "image_preview").exists()
    assert not (output / "video_frames").exists()
    assert not (output / "unified_manifest").exists()
    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "database").exists()
    assert not (output / "index").exists()
    assert not (ROOT / "configs/models.local.json").exists()
    if symlink_created:
        assert "a/linked_one.jpg" not in {
            member
            for record in duplicate_records
            for member in record["member_relative_paths"]
        }

    after = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before

    forbidden_summary_keys = {"timestamp", "elapsed", "random_id"}
    assert not (forbidden_summary_keys & set(duplicate_summary))
    assert not (forbidden_summary_keys & set(exception_summary))

    assert app.main(argv) == 0
    repeated_duplicate_summary = json.loads(
        duplicate_summary_path.read_text(encoding="utf-8")
    )
    repeated_exception_summary = json.loads(
        exception_summary_path.read_text(encoding="utf-8")
    )
    for key in [
        "total_files_seen",
        "files_hashed",
        "content_duplicate_group_count",
        "path_duplicate_group_count",
        "duplicate_member_file_count",
    ]:
        assert repeated_duplicate_summary[key] == duplicate_summary[key]
    for key in [
        "total_files_seen",
        "exception_count",
        "zero_size_count",
        "unsupported_extension_count",
        "known_raw_video_unsupported_count",
        "known_raw_image_unsupported_count",
        "unreadable_count",
        "output_missing_count",
    ]:
        assert repeated_exception_summary[key] == exception_summary[key]
    assert duplicate_manifest_path.read_text(encoding="utf-8") == duplicate_text
    assert exception_manifest_path.read_text(encoding="utf-8") == exception_text


def test_v023a_path_duplicate_and_unreadable_record_helpers(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "one.jpg"
    second = source / "shadow" / "one.jpg"
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    duplicate_records = build_path_duplicate_manifest_for_test(
        source,
        [
            {
                "source_path": str(first),
                "source_relative_path": "one.jpg",
                "size_bytes": first.stat().st_size,
            },
            {
                "source_path": str(second),
                "source_relative_path": "one.jpg",
                "size_bytes": second.stat().st_size,
            },
        ],
    )
    assert len(duplicate_records) == 1
    path_duplicate = duplicate_records[0]
    assert path_duplicate["duplicate_type"] == "path_duplicate"
    assert path_duplicate["duplicate_group_id"].startswith("path:")
    assert len(path_duplicate["duplicate_group_id"]) == len("path:") + 16
    assert path_duplicate["duplicate_reason"] == "same_source_relative_path"
    assert path_duplicate["member_count"] == 2
    assert path_duplicate["action_taken"] == "record_only"

    unreadable = build_unreadable_exception_for_test(source, "secret.mov")
    assert unreadable["error_code"] == "unreadable"
    assert unreadable["stage"] == "V0.2-3A"
    assert unreadable["source_relative_path"] == "secret.mov"
    assert unreadable["recoverable"] is True
    assert unreadable["next_action"] == "retry_later"
    assert unreadable["action_taken"] == "record_only"


def test_v023b_unified_manifest_builds_stable_total_ledger(tmp_path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    source.mkdir()
    for relative_path, content in {
        "a/one.jpg": b"same",
        "b/one_copy.jpg": b"same",
        "raw/A001.braw": b"raw",
        "raw/A001_copy.BRAW": b"raw",
        "a/empty.mp4": b"",
        "a/two.mov": b"video",
        "a/sound.wav": b"audio",
        "a/readme.bin": b"other",
    }.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    scan_records = []
    media_types = {
        ".jpg": "image",
        ".braw": "other",
        ".mp4": "video",
        ".mov": "video",
        ".wav": "audio",
        ".bin": "other",
    }
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source).as_posix()
        extension = path.suffix.lower()
        scan_records.append(
            {
                "source_root": str(source.resolve()),
                "source_path": str(path.resolve()),
                "source_relative_path": relative_path,
                "file_name": path.name,
                "extension": extension,
                "media_type": media_types[extension],
                "size_bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "scan_status": "success",
                "scan_policy": "light_scan_default",
            }
        )
    write_jsonl(workspace / "manifests/media_manifest.jsonl", scan_records)
    write_jsonl(
        workspace / "image_preview/preview_manifest.jsonl",
        [
            {
                "source_relative_path": "a/one.jpg",
                "preview_path": str((workspace / "image_preview/normal/z.jpg").resolve()),
            },
            {
                "source_relative_path": "a/one.jpg",
                "preview_path": str((workspace / "image_preview/normal/a.jpg").resolve()),
            },
            {
                "source_relative_path": "orphan/image.jpg",
                "preview_path": str((workspace / "image_preview/orphan.jpg").resolve()),
            },
        ],
    )
    write_jsonl(
        workspace / "image_preview/keyframe_pool_manifest.jsonl",
        [
            {
                "source_relative_path": "a/one.jpg",
                "preview_path": str((workspace / "image_preview/keyframe/k.jpg").resolve()),
            },
            {
                "source_relative_path": "orphan/image.jpg",
                "preview_path": str((workspace / "image_preview/keyframe/orphan.jpg").resolve()),
            },
        ],
    )
    write_jsonl(
        workspace / "image_preview/all_image_decisions.jsonl",
        [
            {
                "source_relative_path": "a/one.jpg",
                "preview_reason": "normal_image",
            },
            {
                "source_relative_path": "orphan/image.jpg",
                "preview_reason": "normal_image",
            },
        ],
    )
    write_jsonl(
        workspace / "video_frames/video_frame_manifest.jsonl",
        [
            {
                "source_video_relative_path": "a/two.mov",
                "frame_path": str((workspace / "video_frames/video_frame_jpg/2.jpg").resolve()),
                "estimated_frame_time_ms": 3000,
            },
            {
                "source_video_relative_path": "a/two.mov",
                "frame_path": str((workspace / "video_frames/video_frame_jpg/1.jpg").resolve()),
                "estimated_frame_time_ms": 1000,
            },
            {
                "source_video_relative_path": "orphan/video.mov",
                "frame_path": str((workspace / "video_frames/video_frame_jpg/orphan.jpg").resolve()),
                "estimated_frame_time_ms": 1000,
            },
        ],
    )
    write_jsonl(
        workspace / "quality/duplicate_manifest.jsonl",
        [
            {
                "duplicate_group_id": "content:jpgdup00000000",
                "duplicate_type": "content_duplicate",
                "member_count": 2,
                "representative_relative_path": "a/one.jpg",
                "member_relative_paths": ["a/one.jpg", "b/one_copy.jpg"],
            },
            {
                "duplicate_group_id": "content:rawdup00000000",
                "duplicate_type": "content_duplicate",
                "member_count": 2,
                "representative_relative_path": "raw/A001.braw",
                "member_relative_paths": ["raw/A001.braw", "raw/A001_copy.BRAW"],
            },
            {
                "duplicate_group_id": "content:orphan0000000",
                "duplicate_type": "content_duplicate",
                "member_count": 2,
                "representative_relative_path": "ghost/dup.jpg",
                "member_relative_paths": ["ghost/dup.jpg", "ghost/dup.jpg"],
            },
        ],
    )
    write_jsonl(
        workspace / "quality/exception_manifest.jsonl",
        [
            {
                "source_relative_path": "a/one.jpg",
                "error_code": "output_missing",
                "error_subtype": "",
                "recoverable": True,
                "expected_output": "/tmp/missing.jpg",
            },
            {
                "source_relative_path": "raw/A001.braw",
                "error_code": "unsupported_extension",
                "error_subtype": "known_raw_video_unsupported",
                "recoverable": True,
                "expected_output": "",
            },
            {
                "source_relative_path": "raw/A001_copy.BRAW",
                "error_code": "unsupported_extension",
                "error_subtype": "known_raw_video_unsupported",
                "recoverable": True,
                "expected_output": "",
            },
            {
                "source_relative_path": "a/empty.mp4",
                "error_code": "zero_size",
                "error_subtype": "",
                "recoverable": True,
                "expected_output": "",
            },
            {
                "source_relative_path": "ghost/error.mov",
                "error_code": "zero_size",
                "error_subtype": "",
                "recoverable": True,
                "expected_output": "",
            },
            {
                "source_relative_path": "ghost/error.mov",
                "error_code": "unreadable",
                "error_subtype": "",
                "recoverable": True,
                "expected_output": "",
            },
        ],
    )
    input_files_before = {
        path: path.read_bytes()
        for path in [
            workspace / "manifests/media_manifest.jsonl",
            workspace / "image_preview/preview_manifest.jsonl",
            workspace / "video_frames/video_frame_manifest.jsonl",
            workspace / "quality/duplicate_manifest.jsonl",
            workspace / "quality/exception_manifest.jsonl",
        ]
    }
    source_before = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }

    assert app.main(["build-unified-manifest", "--workspace", str(workspace)]) == 0
    unified_path = workspace / "unified/unified_media_manifest.jsonl"
    summary_path = workspace / "unified/unified_manifest_summary.json"
    assert unified_path.exists()
    assert summary_path.exists()
    first_unified_text = unified_path.read_text(encoding="utf-8")
    first_summary_text = summary_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in first_unified_text.splitlines()]
    summary = json.loads(first_summary_text)
    by_path = {record["source_relative_path"]: record for record in records}

    assert len(records) == len(scan_records)
    assert [record["source_relative_path"] for record in records] == sorted(by_path)
    assert all(record["unified_record_version"] == "V0.2-3B" for record in records)
    for record in records:
        assert {"source_root", "workspace", "source_path", "source_relative_path"} <= set(record)
        assert {"duplicate_status", "exception_status", "image_preview_status"} <= set(record)
        assert {"video_frame_status", "pipeline_status", "provenance"} <= set(record)
        assert (
            record["pipeline_status"]["high_cost_processing_policy"]
            == record["duplicate_status"]["high_cost_processing_policy"]
        )

    assert by_path["a/one.jpg"]["duplicate_status"]["is_duplicate"] is True
    assert by_path["a/one.jpg"]["duplicate_status"]["is_representative"] is True
    assert by_path["a/one.jpg"]["pipeline_status"]["analysis_eligible"] is True
    assert by_path["a/one.jpg"]["exception_status"]["blocking_error_codes"] == []
    assert by_path["b/one_copy.jpg"]["duplicate_status"]["is_representative"] is False
    assert by_path["b/one_copy.jpg"]["duplicate_status"]["high_cost_processing_policy"] == "reuse_representative"
    assert by_path["b/one_copy.jpg"]["pipeline_status"]["next_stage_hint"] == "reuse_representative"
    assert by_path["raw/A001.braw"]["duplicate_status"]["is_representative"] is True
    assert by_path["raw/A001.braw"]["duplicate_status"]["high_cost_processing_policy"] == "skip_blocked"
    assert by_path["raw/A001_copy.BRAW"]["duplicate_status"]["is_representative"] is False
    assert by_path["raw/A001_copy.BRAW"]["duplicate_status"]["high_cost_processing_policy"] == "skip_blocked"
    assert by_path["raw/A001_copy.BRAW"]["pipeline_status"]["next_stage_hint"] == "blocked_by_exception"
    assert "unsupported_extension" in by_path["raw/A001.braw"]["exception_status"]["blocking_error_codes"]
    assert by_path["a/empty.mp4"]["pipeline_status"]["analysis_eligible"] is False
    assert by_path["a/sound.wav"]["pipeline_status"]["next_stage_hint"] == "audio_future_stage"
    assert by_path["a/readme.bin"]["pipeline_status"]["next_stage_hint"] == "other_future_stage"
    assert by_path["a/one.jpg"]["image_preview_status"]["applicable"] is True
    assert by_path["a/one.jpg"]["image_preview_status"]["preview_available"] is True
    assert by_path["a/one.jpg"]["image_preview_status"]["preview_paths"] == sorted(
        by_path["a/one.jpg"]["image_preview_status"]["preview_paths"]
    )
    assert by_path["a/two.mov"]["video_frame_status"]["applicable"] is True
    assert by_path["a/two.mov"]["video_frame_status"]["frames_available"] is True
    assert by_path["a/two.mov"]["video_frame_status"]["time_positions_ms"] == [1000, 3000]
    assert by_path["a/two.mov"]["video_frame_status"]["frame_paths"] == sorted(
        by_path["a/two.mov"]["video_frame_status"]["frame_paths"]
    )

    assert summary["unified_manifest_version"] == "V0.2-3B"
    assert summary["total_scan_records"] == len(scan_records)
    assert summary["unified_record_count"] == len(scan_records)
    assert summary["image_record_count"] == 2
    assert summary["video_record_count"] == 2
    assert summary["audio_record_count"] == 1
    assert summary["other_record_count"] == 3
    assert summary["records_with_image_preview"] == 1
    assert summary["records_with_video_frames"] == 1
    assert summary["records_with_duplicate"] == 4
    assert summary["records_with_exception"] == 4
    assert summary["analysis_eligible_count"] == 5
    assert summary["analysis_blocked_count"] == 3
    assert summary["reuse_representative_count"] == 1
    assert summary["process_policy_count"] == 4
    assert summary["skip_blocked_count"] == 3
    assert summary["orphan_image_preview_count"] == 1
    assert summary["orphan_video_frame_count"] == 1
    assert summary["orphan_duplicate_ref_count"] == 1
    assert summary["orphan_exception_ref_count"] == 1
    assert summary["orphan_artifact_count"] == 4
    assert summary["missing_input_manifest_count"] == 0
    assert set(summary["artifact_inputs"]) == set(config.UNIFIED_ARTIFACT_INPUTS)
    assert all("present" in item and "record_count" in item for item in summary["artifact_inputs"].values())
    assert summary["source_read_only"] is True
    assert summary["action_taken"] == "record_only"
    assert summary["model_loaded"] is False
    assert summary["v03_incremental_enabled"] is False
    assert summary["image_preview_generated"] is False
    assert summary["video_frames_generated"] is False
    assert summary["duplicate_detection_rerun"] is False
    assert summary["exception_detection_rerun"] is False
    assert summary["build_v02_generated"] is False

    assert "orphan/image.jpg" not in by_path
    assert not (workspace / "speech").exists()
    assert not (workspace / "embeddings").exists()
    assert not (workspace / "search").exists()
    assert not (workspace / "index").exists()
    assert not (workspace / "database").exists()
    assert not (ROOT / "configs/models.local.json").exists()
    assert source_before == {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    assert input_files_before == {path: path.read_bytes() for path in input_files_before}
    assert not ({"timestamp", "elapsed", "random_id"} & set(summary))

    assert app.main(["build-unified-manifest", "--workspace", str(workspace)]) == 0
    assert unified_path.read_text(encoding="utf-8") == first_unified_text
    assert summary_path.read_text(encoding="utf-8") == first_summary_text


def test_v023b_missing_optional_and_required_inputs(tmp_path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    source.mkdir()
    one = source / "one.jpg"
    one.write_bytes(b"one")
    write_jsonl(
        workspace / "manifests/media_manifest.jsonl",
        [
            {
                "source_root": str(source.resolve()),
                "source_path": str(one.resolve()),
                "source_relative_path": "one.jpg",
                "file_name": "one.jpg",
                "extension": ".jpg",
                "media_type": "image",
                "size_bytes": one.stat().st_size,
                "mtime_ns": one.stat().st_mtime_ns,
                "scan_status": "success",
                "scan_policy": "light_scan_default",
            }
        ],
    )
    assert app.main(["build-unified-manifest", "--workspace", str(workspace)]) == 0
    summary = json.loads((workspace / "unified/unified_manifest_summary.json").read_text())
    assert summary["missing_input_manifest_count"] == 6
    assert summary["artifact_inputs"]["manifests/media_manifest.jsonl"]["present"] is True
    assert all(
        summary["artifact_inputs"][path]["present"] is False
        for path in config.UNIFIED_ARTIFACT_INPUTS[1:]
    )

    missing_workspace = tmp_path / "missing"
    try:
        app.main(["build-unified-manifest", "--workspace", str(missing_workspace)])
        raise AssertionError("missing media_manifest should fail")
    except FileNotFoundError as exc:
        assert "manifests/media_manifest.jsonl" in str(exc)
    assert not (missing_workspace / "unified/unified_media_manifest.jsonl").exists()
    assert not (missing_workspace / "unified/unified_manifest_summary.json").exists()


def test_v023b_primary_duplicate_group_selection():
    groups = [
        {
            "duplicate_group_id": "content:z",
            "member_count": 2,
            "representative_relative_path": "a",
        },
        {
            "duplicate_group_id": "content:b",
            "member_count": 4,
            "representative_relative_path": "b",
        },
        {
            "duplicate_group_id": "content:a",
            "member_count": 4,
            "representative_relative_path": "a",
        },
    ]
    primary = choose_primary_duplicate_group(groups)
    assert primary["duplicate_group_id"] == "content:a"


def test_v023c_combo_validation_passes_on_mixed_workspace(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    checks = tmp_path / "expected_output_checks.jsonl"
    for directory in ["a", "b", "raw"]:
        (source / directory).mkdir(parents=True, exist_ok=True)
    for relative_path, content in {
        "a/one.jpg": b"same-content",
        "b/one_copy.jpg": b"same-content",
        "a/one.mov": b"same-video-content",
        "b/one_copy.mov": b"same-video-content",
        "raw/A001.braw": b"same-raw-content",
        "raw/A001_copy.BRAW": b"same-raw-content",
        "a/two.mov": b"unique-content",
        "a/empty.mp4": b"",
        "a/readme.xyz": b"not-supported",
        "raw/C001.CRM": b"canon-raw",
        "raw/G001.GPR": b"gopro-raw",
        "a/test.wav": b"audio-content",
    }.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    checks.write_text(
        json.dumps(
            {
                "source_relative_path": "a/one.jpg",
                "stage": "test_expected_preview",
                "expected_output": str(output / "missing_preview.jpg"),
                "actual_output": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert app.main(["scan", "--source", str(source), "--output", str(output)]) == 0
    assert app.main(
        [
            "preview-images",
            "--source",
            str(source),
            "--output",
            str(output),
            "--preview-backend",
            TEST_COPY_JPG,
        ]
    ) == 0
    assert app.main(
        [
            "extract-video-frames",
            "--source",
            str(source),
            "--output",
            str(output),
            "--video-runner",
            FAKE_FFMPEG_JPG,
        ]
    ) == 0
    assert app.main(
        [
            "detect-duplicates-exceptions",
            "--source",
            str(source),
            "--output",
            str(output),
            "--expected-output-checks",
            str(checks),
        ]
    ) == 0
    assert app.main(["build-unified-manifest", "--workspace", str(output)]) == 0

    source_before = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    input_paths = [
        output / "manifests/media_manifest.jsonl",
        output / "image_preview/preview_manifest.jsonl",
        output / "image_preview/all_image_decisions.jsonl",
        output / "video_frames/video_frame_manifest.jsonl",
        output / "quality/duplicate_manifest.jsonl",
        output / "quality/exception_manifest.jsonl",
        output / "unified/unified_media_manifest.jsonl",
        output / "unified/unified_manifest_summary.json",
    ]
    input_before = {path: path.read_bytes() for path in input_paths}

    assert app.main(["validate-v02-combo", "--workspace", str(output)]) == 0
    report_path = output / "reports/v02_combo_validation_report.json"
    markdown_path = output / "reports/v02_combo_validation_report.md"
    assert report_path.exists()
    assert markdown_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    report = json.loads(report_text)

    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["warning_check_count"] == 0
    assert set(report["required_inputs"]) == {
        "manifests/media_manifest.jsonl",
        "image_preview/preview_manifest.jsonl",
        "image_preview/all_image_decisions.jsonl",
        "video_frames/video_frame_manifest.jsonl",
        "quality/duplicate_manifest.jsonl",
        "quality/exception_manifest.jsonl",
        "unified/unified_media_manifest.jsonl",
        "unified/unified_manifest_summary.json",
    }
    assert all(item["present"] is True for item in report["required_inputs"].values())
    assert report["total_scan_records"] == report["unified_record_count"]
    assert report["media_counts"]["image"] >= 1
    assert report["media_counts"]["video"] >= 1
    assert report["media_counts"]["audio"] >= 1
    assert report["media_counts"]["raw_unsupported"] >= 3
    assert report["artifact_counts"]["image_preview_records"] >= 1
    assert report["artifact_counts"]["video_frame_records"] >= 1
    assert report["artifact_counts"]["duplicate_groups"] >= 1
    assert report["artifact_counts"]["exception_records"] >= 1
    assert report["routing_checks"]["image_records_with_preview_status_applicable"] == report["routing_checks"]["image_records_total"]
    assert report["routing_checks"]["video_records_with_frame_status_applicable"] == report["routing_checks"]["video_records_total"]
    assert report["routing_checks"]["audio_records_with_preview_or_frame_available"] == 0
    assert report["routing_checks"]["audio_records_with_audio_future_stage"] == report["routing_checks"]["audio_records_total"]
    assert report["routing_checks"]["other_records_with_other_future_stage_or_blocked"] == report["routing_checks"]["other_records_total"]
    assert report["routing_checks"]["raw_records_blocked"] == report["routing_checks"]["raw_records_total"]
    assert report["duplicate_checks"]["reuse_representative_count"] >= 1
    assert report["duplicate_checks"]["blocked_duplicates_with_skip_policy"] >= 1
    assert report["duplicate_checks"]["raw_duplicate_records"] >= 1
    assert report["duplicate_checks"]["raw_duplicate_records_with_skip_blocked"] == report["duplicate_checks"]["raw_duplicate_records"]
    assert report["exception_checks"]["zero_size_blocked_count"] >= 1
    assert report["exception_checks"]["unsupported_blocked_count"] >= 1
    assert report["exception_checks"]["raw_blocked_count"] >= 3
    assert report["exception_checks"]["output_missing_records"] >= 1
    assert report["exception_checks"]["output_missing_blocking_count"] == 0
    assert report["unified_checks"]["orphan_artifact_count"] == 0
    assert report["unified_checks"]["missing_input_manifest_count"] == 0
    assert report["boundary_checks"]["source_read_only"] is True
    assert report["boundary_checks"]["model_loaded"] is False
    assert report["boundary_checks"]["v03_incremental_enabled"] is False
    assert report["boundary_checks"]["build_v02_generated"] is False
    assert report["boundary_checks"]["image_preview_generated_by_validator"] is False
    assert report["boundary_checks"]["video_frames_generated_by_validator"] is False
    assert report["boundary_checks"]["duplicate_detection_rerun_by_validator"] is False
    assert report["boundary_checks"]["exception_detection_rerun_by_validator"] is False
    assert report["boundary_checks"]["unified_manifest_rerun_by_validator"] is False
    assert report["boundary_checks"]["forbidden_outputs_created"] == []
    assert all(check["status"] == "PASS" for check in report["checks"])
    assert report["validation_status"] == (
        "FAIL"
        if any(c["status"] == "FAIL" and c["severity"] == "error" for c in report["checks"])
        else "PASS"
    )
    assert source_before == {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    assert input_before == {path: path.read_bytes() for path in input_paths}
    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "index").exists()
    assert not (output / "database").exists()
    assert not (ROOT / "configs/models.local.json").exists()
    assert "timestamp" not in report
    assert "elapsed" not in report
    assert "random_id" not in report

    assert app.main(["validate-v02-combo", "--workspace", str(output)]) == 0
    assert report_path.read_text(encoding="utf-8") == report_text
    assert markdown_path.read_text(encoding="utf-8") == markdown_text


def test_v023c_combo_validation_missing_required_input_writes_fail_report(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    write_jsonl(
        workspace / "manifests/media_manifest.jsonl",
        [
            {
                "source_root": str((tmp_path / "source").resolve()),
                "source_path": str((tmp_path / "source/one.jpg").resolve()),
                "source_relative_path": "one.jpg",
                "file_name": "one.jpg",
                "extension": ".jpg",
                "media_type": "image",
                "size_bytes": 3,
                "mtime_ns": 1,
                "scan_status": "success",
                "scan_policy": "light_scan_default",
            }
        ],
    )
    assert app.main(["validate-v02-combo", "--workspace", str(workspace)]) == 1
    report_path = workspace / "reports/v02_combo_validation_report.json"
    markdown_path = workspace / "reports/v02_combo_validation_report.md"
    assert report_path.exists()
    assert markdown_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["validation_status"] == "FAIL"
    assert report["failed_check_count"] >= 1
    assert set(report["required_inputs"]) == set(config.COMBO_REQUIRED_INPUTS)
    assert report["required_inputs"]["image_preview/preview_manifest.jsonl"]["present"] is False
    required_check = [
        check for check in report["checks"] if check["check_id"] == "required_inputs_present"
    ][0]
    assert required_check["status"] == "FAIL"
    assert required_check["severity"] == "error"
    assert report["source_read_only"] is True
    assert report["action_taken"] == "record_only"
    assert report["model_loaded"] is False
    assert report["v03_incremental_enabled"] is False
    assert report["build_v02_generated"] is False


def test_v024_build_v02_e2e_entrypoint_passes_and_is_stable(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    checks = tmp_path / "expected_output_checks.jsonl"
    for directory in ["a", "b", "raw"]:
        (source / directory).mkdir(parents=True, exist_ok=True)
    for relative_path, content in {
        "a/one.jpg": b"same-content",
        "b/one_copy.jpg": b"same-content",
        "a/one.mov": b"same-video-content",
        "b/one_copy.mov": b"same-video-content",
        "raw/A001.braw": b"same-raw-content",
        "raw/A001_copy.BRAW": b"same-raw-content",
        "a/two.mov": b"unique-content",
        "a/empty.mp4": b"",
        "a/readme.xyz": b"not-supported",
        "raw/C001.CRM": b"canon-raw",
        "raw/G001.GPR": b"gopro-raw",
        "a/test.wav": b"audio-content",
    }.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    checks.write_text(
        json.dumps(
            {
                "source_relative_path": "a/one.jpg",
                "stage": "test_expected_preview",
                "expected_output": str(output / "missing_preview.jpg"),
                "actual_output": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    parser = app.build_parser()
    command_names = set(
        next(action for action in parser._actions if getattr(action, "choices", None)).choices
    )
    assert {
        "scan",
        "preview-images",
        "extract-video-frames",
        "detect-duplicates-exceptions",
        "build-unified-manifest",
        "validate-v02-combo",
        "build-v02",
    } <= command_names
    assert "test_copy_jpg" == TEST_COPY_JPG
    assert "fake_ffmpeg_jpg" == FAKE_FFMPEG_JPG

    before = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    source_initial_paths = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())

    argv = [
        "build-v02",
        "--source",
        str(source),
        "--output",
        str(output),
        "--preview-backend",
        TEST_COPY_JPG,
        "--video-runner",
        FAKE_FFMPEG_JPG,
        "--expected-output-checks",
        str(checks),
    ]
    assert app.main(argv) == 0

    report_path = output / "reports/v02_e2e_report.json"
    markdown_path = output / "reports/v02_e2e_report.md"
    assert report_path.exists()
    assert markdown_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    report = json.loads(report_text)

    required_report_fields = {
        "e2e_version",
        "e2e_status",
        "source_root",
        "output_dir",
        "execution_order",
        "failed_stage",
        "stage_results",
        "final_artifacts",
        "combo_validation_status",
        "total_scan_records",
        "unified_record_count",
        "media_counts",
        "duplicate_groups",
        "exception_records",
        "records_with_duplicate",
        "records_with_exception",
        "analysis_blocked_count",
        "raw_unsupported_count",
        "raw_blocked_count",
        "output_missing_records",
        "output_missing_blocking_count",
        "source_read_only",
        "action_taken",
        "model_loaded",
        "v03_incremental_enabled",
        "build_v02_generated",
        "real_3618_validation_run",
        "real_13gb_validation_run",
        "real_1_3tb_validation_run",
        "real_32tb_validation_run",
    }
    assert required_report_fields <= set(report)
    assert report["e2e_version"] == "V0.2-4"
    assert report["e2e_status"] == "PASS"
    assert report["failed_stage"] == ""
    assert report["execution_order"] == config.V02_E2E_EXECUTION_ORDER
    assert set(report["stage_results"]) == set(config.V02_E2E_EXECUTION_ORDER)
    assert all(result["status"] == "PASS" for result in report["stage_results"].values())
    assert all(
        result["expected_artifacts"] == sorted(STAGE_EXPECTED_ARTIFACTS[stage])
        for stage, result in report["stage_results"].items()
    )
    assert all(result["missing_artifacts"] == [] for result in report["stage_results"].values())
    assert set(report["final_artifacts"]) == set(config.V02_E2E_FINAL_ARTIFACTS)
    assert all(item["present"] is True for item in report["final_artifacts"].values())
    assert report["combo_validation_status"] == "PASS"
    assert report["total_scan_records"] == report["unified_record_count"]
    assert report["media_counts"]["image"] >= 1
    assert report["media_counts"]["video"] >= 1
    assert report["media_counts"]["audio"] >= 1
    assert report["media_counts"]["raw_unsupported"] >= 3
    assert report["duplicate_groups"] >= 1
    assert report["exception_records"] >= 1
    assert report["records_with_duplicate"] >= 1
    assert report["records_with_exception"] >= 1
    assert report["analysis_blocked_count"] >= 1
    assert report["raw_unsupported_count"] >= 3
    assert report["raw_blocked_count"] >= 3
    assert report["output_missing_records"] >= 1
    assert report["output_missing_blocking_count"] == 0
    assert report["source_read_only"] is True
    assert report["action_taken"] == "record_only"
    assert report["model_loaded"] is False
    assert report["v03_incremental_enabled"] is False
    assert report["build_v02_generated"] is True
    assert report["real_3618_validation_run"] is False
    assert report["real_13gb_validation_run"] is False
    assert report["real_1_3tb_validation_run"] is False
    assert report["real_32tb_validation_run"] is False

    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "index").exists()
    assert not (output / "database").exists()
    assert not (ROOT / "configs/models.local.json").exists()
    assert not ({"timestamp", "elapsed", "random_id"} & set(report))

    after = {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()) == source_initial_paths

    assert app.main(argv) == 0
    assert report_path.read_text(encoding="utf-8") == report_text
    assert markdown_path.read_text(encoding="utf-8") == markdown_text


def test_v024_build_v02_failure_stops_and_writes_fail_report(tmp_path):
    source = tmp_path / "not_a_directory"
    source.write_text("not a directory", encoding="utf-8")
    output = tmp_path / "output"

    argv = ["build-v02", "--source", str(source), "--output", str(output)]
    assert app.main(argv) == 1

    report_path = output / "reports/v02_e2e_report.json"
    markdown_path = output / "reports/v02_e2e_report.md"
    assert report_path.exists()
    assert markdown_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["e2e_status"] == "FAIL"
    assert report["failed_stage"] == "scan"
    assert report["stage_results"]["scan"]["status"] == "FAIL"
    for stage in config.V02_E2E_EXECUTION_ORDER[1:]:
        assert report["stage_results"][stage]["status"] == "SKIPPED"
    assert set(report["final_artifacts"]) == set(config.V02_E2E_FINAL_ARTIFACTS)
    assert report["final_artifacts"]["reports/v02_e2e_report.json"] == {
        "present": True,
        "record_count": 1,
    }
    assert report["final_artifacts"]["reports/v02_e2e_report.md"] == {
        "present": True,
        "record_count": 1,
    }
    for relative_path, artifact in report["final_artifacts"].items():
        if relative_path.startswith("reports/v02_e2e_report."):
            continue
        assert artifact == {"present": False, "record_count": 0}
    assert report["combo_validation_status"] == ""
    assert report["total_scan_records"] == 0
    assert report["unified_record_count"] == 0
    assert report["media_counts"] == {}
    assert report["source_read_only"] is True
    assert report["action_taken"] == "record_only"
    assert report["model_loaded"] is False
    assert report["v03_incremental_enabled"] is False
    assert report["build_v02_generated"] is True
    assert not (output / "unified").exists()
    assert not (output / "speech").exists()
    assert not (output / "embeddings").exists()
    assert not (output / "search").exists()
    assert not (output / "index").exists()
    assert not (output / "database").exists()


def test_model_policy_and_local_config_boundary():
    policy = (ROOT / "docs/MODEL_STORAGE_POLICY.md").read_text(encoding="utf-8")
    assert "/Users/yourname/Documents/model" in policy
    assert (ROOT / "configs/models.local.example.json").exists()
    assert not (ROOT / "configs/models.local.json").exists()

    data = json.loads((ROOT / "configs/models.local.example.json").read_text())
    assert data["model_root"] == "/Users/yourname/Documents/model"
    assert all(model["enabled"] is False for model in data["models"].values())
