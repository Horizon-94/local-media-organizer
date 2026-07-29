from __future__ import annotations

import json
from pathlib import Path

from media_archive.preview.backends import TEST_COPY_JPG
from media_archive.video.runners import FAKE_FFMPEG_JPG
from media_archive.workflows.v02_build import build_v02


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_v02_reuses_duplicate_derived_preview_and_frames(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    (source / "images").mkdir(parents=True)
    (source / "videos").mkdir(parents=True)
    (source / "images/a.jpg").write_bytes(b"same image")
    (source / "images/b.jpg").write_bytes(b"same image")
    (source / "videos/a.mov").write_bytes(b"same video")
    (source / "videos/b.mov").write_bytes(b"same video")

    report = build_v02(
        source,
        output,
        preview_backend=TEST_COPY_JPG,
        video_runner=FAKE_FFMPEG_JPG,
    )

    assert report["e2e_status"] == "PASS"

    content_rows = read_jsonl(output / "quality/content_identity_manifest.jsonl")
    assert all(row["content_id"].startswith("sha256:") for row in content_rows)
    assert any(row["path_count"] == 2 for row in content_rows)

    preview_rows = read_jsonl(output / "image_preview/preview_manifest.jsonl")
    previews_by_relative = {row["source_relative_path"]: row for row in preview_rows}
    assert previews_by_relative["images/a.jpg"]["artifact_generation_action"] == "generated_once"
    assert previews_by_relative["images/b.jpg"]["artifact_generation_action"] == "reused_existing"
    assert previews_by_relative["images/a.jpg"]["preview_path"] == previews_by_relative["images/b.jpg"]["preview_path"]
    assert previews_by_relative["images/b.jpg"]["reused_existing_artifact"] is True
    assert previews_by_relative["images/b.jpg"]["reused_for_duplicate"] is True

    frame_rows = read_jsonl(output / "video_frames/video_frame_manifest.jsonl")
    frames_by_relative: dict[str, list[dict[str, object]]] = {}
    for row in frame_rows:
        frames_by_relative.setdefault(str(row["source_video_relative_path"]), []).append(row)
    assert {row["artifact_generation_action"] for row in frames_by_relative["videos/a.mov"]} == {"generated_once"}
    assert {row["artifact_generation_action"] for row in frames_by_relative["videos/b.mov"]} == {"reused_existing"}
    assert [row["frame_path"] for row in frames_by_relative["videos/a.mov"]] == [
        row["frame_path"] for row in frames_by_relative["videos/b.mov"]
    ]
    assert all(row["reused_existing_artifact"] is True for row in frames_by_relative["videos/b.mov"])

    unified_rows = read_jsonl(output / "unified/unified_media_manifest.jsonl")
    unified_by_relative = {row["source_relative_path"]: row for row in unified_rows}
    assert unified_by_relative["images/b.jpg"]["content_id"] == unified_by_relative["images/a.jpg"]["content_id"]
    assert unified_by_relative["videos/b.mov"]["path_count"] == 2
