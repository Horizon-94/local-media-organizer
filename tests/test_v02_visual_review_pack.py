import json
import subprocess
from pathlib import Path

from media_archive import app


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def create_fake_workspace(tmp_path: Path) -> tuple[Path, Path, list[Path], list[Path]]:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source with 中文（样本）"
    source.mkdir(parents=True)
    (source / "images").mkdir()
    (source / "videos").mkdir()
    (source / "images" / "原始 first.jpg").write_bytes(b"source image")
    (source / "videos" / "视频 文件(1).MOV").write_bytes(b"source video")

    image_preview_dir = workspace / "formal_hash_images"
    video_frame_dir = workspace / "formal_hash_frames"
    image_preview_dir.mkdir(parents=True)
    video_frame_dir.mkdir(parents=True)

    image_records: list[dict[str, object]] = []
    image_files: list[Path] = []
    image_inputs = [
        ("tl_A", "middle", "延时 组/IMG 0002 middle.jpg"),
        ("tl_A", "first", "延时 组/IMG 0001 first.jpg"),
        ("tl_A", "last", "延时 组/IMG 0003 last.jpg"),
        ("tl_B", "first", "另一个组/复杂（首）.jpg"),
        ("tl_B", "middle", "另一个组/复杂 middle.jpg"),
        ("tl_B", "last", "另一个组/复杂 last.jpg"),
    ]
    for index, (group_id, role, source_relative_path) in enumerate(image_inputs):
        preview_path = image_preview_dir / f"hash_image_{index:02d}.jpg"
        preview_path.write_bytes(f"preview-{index}".encode("utf-8"))
        image_files.append(preview_path)
        image_records.append(
            {
                "image_preview_strategy": "A9T-v3",
                "preview_path": str(preview_path),
                "source_path": str(source / "images" / Path(source_relative_path).name),
                "source_relative_path": source_relative_path,
                "timelapse_group_id": group_id,
                "timelapse_role": role,
            }
        )

    video_records: list[dict[str, object]] = []
    video_files: list[Path] = []
    video_inputs = [
        ("A001/IMG_0001.MOV", 0, 1000),
        ("A001/IMG_0001.MOV", 1, 3000),
        ("复杂 路径/视频 文件(1).MOV", 0, 1000),
        ("复杂 路径/视频 文件(1).MOV", 1, 3000),
        ("复杂 路径/视频 文件(1).MOV", 2, 3000),
    ]
    for index, (source_video_relative_path, frame_index, estimated_ms) in enumerate(video_inputs):
        frame_path = video_frame_dir / f"hash_frame_{index:02d}.jpg"
        frame_path.write_bytes(f"frame-{index}".encode("utf-8"))
        video_files.append(frame_path)
        video_records.append(
            {
                "concurrency": 4,
                "decode_mode": "videotoolbox",
                "estimated_frame_time_ms": estimated_ms,
                "frame_file": frame_path.name,
                "frame_index": frame_index,
                "frame_path": str(frame_path),
                "frame_relative_path": frame_path.relative_to(workspace).as_posix(),
                "frame_status": "success",
                "max_edge_px": 1280,
                "output_format": "jpg",
                "sampling_interval_ms": 2000,
                "sampling_offset_ms": 1000,
                "source_root": str(source / "videos"),
                "source_video_path": str(source / "videos" / Path(source_video_relative_path).name),
                "source_video_relative_path": source_video_relative_path,
                "video_frame_strategy": "R2J-FIX-C4",
            }
        )

    write_jsonl(
        workspace
        / "image_a9t_real3618/run_clean_current/image_preview/keyframe_pool_manifest.jsonl",
        image_records,
    )
    video_manifest_path = (
        workspace
        / "video_r2j_real13gb/run_clean_current/video_frames/video_frame_manifest.jsonl"
    )
    write_jsonl(video_manifest_path, video_records)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "v02_real_minimal_verdict.json").write_text(
        '{"schema_version":"1.1","validation_status":"PASS"}\n',
        encoding="utf-8",
    )
    (reports_dir / "v02_real_minimal_validation.json").write_text(
        '{"validation_status":"PASS"}\n',
        encoding="utf-8",
    )
    return workspace, source, image_files, video_files


def assert_no_forbidden_dirs(workspace: Path) -> None:
    for name in ["speech", "embeddings", "search", "index", "database"]:
        assert not (workspace / name).exists()


def test_build_visual_review_pack_outputs_readable_review(tmp_path, monkeypatch):
    workspace, source, image_files, video_files = create_fake_workspace(tmp_path)
    source_before = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    formal_before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in image_files + video_files
    }

    def fail_external_process(*args, **kwargs):
        raise AssertionError("visual review pack must not call external processes")

    def fail_a9t_or_c4(*args, **kwargs):
        raise AssertionError("visual review pack must not rerun A9T-v3 or C4")

    monkeypatch.setattr(subprocess, "run", fail_external_process)
    monkeypatch.setattr(subprocess, "Popen", fail_external_process)
    monkeypatch.setattr(app, "run_a9t_v3_image_preview", fail_a9t_or_c4)
    monkeypatch.setattr(app, "run_video_frames_c4", fail_a9t_or_c4)

    assert app.main(["build-visual-review-pack", "--workspace", str(workspace)]) == 0

    review_root = workspace / "reports" / "visual_review"
    summary_path = review_root / "visual_review_summary.json"
    image_manifest_path = review_root / "image_timelapse_review_manifest.jsonl"
    video_manifest_path = review_root / "video_frame_review_manifest.jsonl"
    html_path = review_root / "review_index.html"

    assert summary_path.exists()
    assert image_manifest_path.exists()
    assert video_manifest_path.exists()
    assert html_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["visual_review_status"] == "PASS"
    assert summary["timelapse_group_count"] == 2
    assert summary["timelapse_keyframe_input_count"] == 6
    assert summary["timelapse_keyframe_review_count"] == 6
    assert summary["video_source_count"] == 2
    assert summary["video_frame_input_count"] == 5
    assert summary["video_frame_review_count"] == 5
    assert summary["missing_image_preview_count"] == 0
    assert summary["missing_video_frame_count"] == 0
    assert summary["source_read_only"] is True
    assert summary["formal_outputs_modified"] is False
    assert summary["v03_incremental_enabled"] is False
    assert summary["model_loaded"] is False
    assert summary["core_v025_status_unchanged"] is True

    image_review_records = read_jsonl(image_manifest_path)
    assert [record["timelapse_role"] for record in image_review_records[:3]] == [
        "first",
        "middle",
        "last",
    ]
    assert all(record["copy_status"] == "success" for record in image_review_records)
    assert all(record["source_relative_path"] for record in image_review_records)
    assert all(Path(record["original_preview_path"]).exists() for record in image_review_records)
    assert (review_root / "image_timelapse_keyframes" / "timelapse_0001").exists()
    assert any(
        path.name.startswith("01_first__")
        for path in (review_root / "image_timelapse_keyframes" / "timelapse_0001").glob("*.jpg")
    )
    assert any(
        path.name.startswith("02_middle__")
        for path in (review_root / "image_timelapse_keyframes" / "timelapse_0001").glob("*.jpg")
    )
    assert any(
        path.name.startswith("03_last__")
        for path in (review_root / "image_timelapse_keyframes" / "timelapse_0001").glob("*.jpg")
    )

    video_review_records = read_jsonl(video_manifest_path)
    assert all(record["copy_status"] == "success" for record in video_review_records)
    assert {record["display_timecode"] for record in video_review_records} >= {
        "00-00-01",
        "00-00-03",
    }
    assert any(record["display_frame_index"] == 1 for record in video_review_records)
    assert any(
        path.name == "000001__00-00-01.jpg"
        for path in (review_root / "video_frames_by_source").rglob("*.jpg")
    )
    assert any(
        path.name == "000002__00-00-03.jpg"
        for path in (review_root / "video_frames_by_source").rglob("*.jpg")
    )
    assert any("复杂" in path.parent.name for path in (review_root / "video_frames_by_source").rglob("*.jpg"))

    html = html_path.read_text(encoding="utf-8")
    assert "Image Timelapse" in html
    assert "Video Frames" in html
    assert "tl_A" in html
    assert "A001/IMG_0001.MOV" in html

    for path, (content, mtime_ns) in formal_before.items():
        assert path.exists()
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == mtime_ns

    source_after = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    assert source_after == source_before
    assert_no_forbidden_dirs(workspace)


def test_build_visual_review_pack_records_missing_without_core_manifest_changes(tmp_path):
    workspace, _source, image_files, video_files = create_fake_workspace(tmp_path)
    image_files[0].unlink()
    video_files[0].unlink()
    keyframe_manifest = (
        workspace
        / "image_a9t_real3618/run_clean_current/image_preview/keyframe_pool_manifest.jsonl"
    )
    video_manifest = (
        workspace
        / "video_r2j_real13gb/run_clean_current/video_frames/video_frame_manifest.jsonl"
    )
    verdict = workspace / "reports" / "v02_real_minimal_verdict.json"
    full_report = workspace / "reports" / "v02_real_minimal_validation.json"
    before = {
        keyframe_manifest: keyframe_manifest.read_text(encoding="utf-8"),
        video_manifest: video_manifest.read_text(encoding="utf-8"),
        verdict: verdict.read_text(encoding="utf-8"),
        full_report: full_report.read_text(encoding="utf-8"),
    }

    assert app.main(["build-visual-review-pack", "--workspace", str(workspace)]) == 1

    review_root = workspace / "reports" / "visual_review"
    summary = json.loads((review_root / "visual_review_summary.json").read_text(encoding="utf-8"))
    assert summary["visual_review_status"] == "FAIL"
    assert summary["missing_image_preview_count"] == 1
    assert summary["missing_video_frame_count"] == 1
    assert summary["core_v025_status_unchanged"] is True

    image_review_records = read_jsonl(review_root / "image_timelapse_review_manifest.jsonl")
    video_review_records = read_jsonl(review_root / "video_frame_review_manifest.jsonl")
    assert any(record["copy_status"] == "missing" for record in image_review_records)
    assert any(record["copy_status"] == "missing" for record in video_review_records)
    assert any("does not exist" in record["error_message"] for record in image_review_records)
    assert any("does not exist" in record["error_message"] for record in video_review_records)

    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text
    assert_no_forbidden_dirs(workspace)
