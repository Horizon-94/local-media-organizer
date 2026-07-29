from __future__ import annotations

import json
import subprocess
from pathlib import Path

from media_archive.v05.manifest import read_json, read_jsonl


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fixture_payload() -> dict[str, object]:
    items = [
        {
            "media_id": "img-one",
            "source_relative_path": "a/one.jpg",
            "media_kind": "image",
            "content_hash": "img-one",
            "yoloe_labels": [{"label": "person", "confidence": 0.9}],
            "ocr_text": "雨天",
            "qwen_caption": "person in rain",
            "is_existing_keyframe": True,
            "visual_embedding_ref": "vis:one",
        },
        {
            "media_id": "img-two-low-cost",
            "source_relative_path": "a/two.jpg",
            "media_kind": "image",
            "content_hash": "img-two",
            "visual_difference_score": 0.1,
            "visual_embedding_ref": "vis:two",
        },
        {
            "media_id": "frame-one",
            "source_relative_path": "v/clip.mov",
            "source_path": "/fixture/source/v/clip.mov",
            "media_kind": "video_frame",
            "content_hash": "frame-one",
            "frame_id": "clip-frame-001",
            "frame_path": "/fixture/work/frames/clip_001.jpg",
            "hit_time_ms": 1000,
            "fixed_query_hit": True,
            "fixed_query_score": 0.9,
            "qwen_caption": "tractor crossing field",
        },
        {
            "media_id": "speech-audio",
            "source_relative_path": "audio/speech.wav",
            "media_kind": "audio",
            "content_hash": "speech-audio",
            "planned_audio_path": "audio_proxy/speech.wav",
            "vad_segments": [{"start_time_ms": 0, "end_time_ms": 1200, "confidence": 0.8}],
            "whisper_segments": [{"start_time_ms": 0, "end_time_ms": 1200, "text": "hello world", "confidence": 0.9}],
        },
        {
            "media_id": "silent-audio",
            "source_relative_path": "audio/silent.wav",
            "media_kind": "audio",
            "content_hash": "silent-audio",
            "no_speech": True,
        },
        {
            "media_id": "video-audio-plan",
            "source_relative_path": "v/audio_clip.mov",
            "media_kind": "video",
            "content_hash": "video-audio",
            "reusable_audio_artifact": True,
            "planned_audio_path": "audio_proxy/audio_clip.wav",
            "vad_segments": [{"start_time_ms": 2000, "end_time_ms": 3500}],
        },
        {
            "media_id": "txt-one",
            "source_relative_path": "text/readme.txt",
            "media_kind": "text",
            "content_hash": "txt-one",
            "text": "plain text body",
        },
        {
            "media_id": "sub-one",
            "source_relative_path": "text/captions.srt",
            "media_kind": "subtitle",
            "content_hash": "sub-one",
            "srt_segments": [{"start_time_ms": 1000, "end_time_ms": 3000, "text": "caption line"}],
        },
        {
            "media_id": "xml-one",
            "source_relative_path": "text/sidecar.xml",
            "media_kind": "xml",
            "content_hash": "xml-one",
            "xml_text": "<title>sidecar</title>",
        },
        {
            "media_id": "unsupported-one",
            "source_relative_path": "misc/file.xyz",
            "media_kind": "unsupported",
            "content_hash": "unsupported-one",
            "unsupported": True,
        },
        {
            "media_id": "stale-one",
            "source_relative_path": "a/stale.jpg",
            "media_kind": "image",
            "content_hash": "stale-one",
            "stale": True,
        },
        {
            "media_id": "offline-one",
            "source_relative_path": "a/offline.jpg",
            "media_kind": "image",
            "file_online": False,
            "last_seen_file_identity": "last-seen-id",
        },
    ]
    return {
        "run_id": "fixture-run",
        "created_at": "2026-01-01T00:00:00+00:00",
        "source_root": "/fixture/source",
        "items": items,
    }


def run_fixture(workspace: Path, fixture: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            "-m",
            "apps.media_archive.app",
            "v05-run-analysis-fixture",
            "--workspace",
            str(workspace),
            "--fixture",
            str(fixture),
            "--profile",
            "safe",
        ],
        cwd="/Users/yourname/Documents/AI-Local/media-archive-clean",
        text=True,
        capture_output=True,
        check=False,
    )


def test_v05_2_controlled_analysis_fixture_routes_and_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    fixture = tmp_path / "fixture.json"
    write_json(fixture, fixture_payload())

    result = run_fixture(workspace, fixture)
    assert result.returncode == 0, result.stderr

    stage = workspace / "stages/v0.5"
    evidence_path = stage / "evidence/evidence_records.jsonl"
    task_path = stage / "manifests/analysis_tasks.jsonl"
    output_path = stage / "manifests/analysis_outputs.jsonl"
    folder_state_path = stage / "state/folder_batch_state.jsonl"
    run_state_path = stage / "state/analysis_run_state.json"
    summary_path = stage / "reports/v05_2_controlled_analysis_summary.json"
    summary_md_path = stage / "reports/v05_2_controlled_analysis_summary.md"
    resource_path = stage / "telemetry/resource_report.json"
    for path in [evidence_path, task_path, output_path, folder_state_path, run_state_path, summary_path, summary_md_path, resource_path]:
        assert path.exists()
        assert str(path).startswith(str(stage))

    evidence = read_jsonl(evidence_path)
    tasks = read_jsonl(task_path)
    outputs = read_jsonl(output_path)
    summary = read_json(summary_path)
    resource = read_json(resource_path)

    evidence_types = {row["evidence_type"] for row in evidence}
    assert {
        "visual_object",
        "ocr_text",
        "visual_embedding",
        "vl_caption",
        "vad_segment",
        "speech_text",
        "text_document",
        "subtitle_segment",
        "text_embedding",
        "metadata",
    } <= evidence_types
    assert summary["metadata_evidence_count"] == len(fixture_payload()["items"])
    assert summary["model_loaded"] is False
    assert summary["real_model_inference_run"] is False
    assert summary["search_index_built"] is False
    assert summary["final_search_index_built"] is False
    assert summary["scheduler_decisions_written"] is False
    assert summary["global_flattened_queue_used"] is False
    assert resource["worker_profile"] == "safe"
    assert resource["scheduler_decisions_written"] is False

    assert [row["evidence_id"] for row in evidence] == [
        row["evidence_id"]
        for row in sorted(
            evidence,
            key=lambda row: (
                row["folder_id"],
                row["source_path"],
                row["media_id"],
                row["evidence_type"],
                -1 if row["hit_time_ms"] is None else row["hit_time_ms"],
                -1 if row["start_time_ms"] is None else row["start_time_ms"],
                row["evidence_id"],
            ),
        )
    ]

    captions_by_media = {row["media_id"] for row in evidence if row["evidence_type"] == "vl_caption"}
    assert "img-one" in captions_by_media
    assert "frame-one" in captions_by_media
    assert "img-two-low-cost" not in captions_by_media
    assert any(row["media_id"] == "img-two-low-cost" and row["evidence_type"] == "visual_embedding" for row in evidence)

    embedding_sources = {row["selection_reason"] for row in evidence if row["evidence_type"] == "text_embedding"}
    assert "embedding_from:ocr_text" in embedding_sources
    assert "embedding_from:speech_text" in embedding_sources
    assert "embedding_from:text_document" in embedding_sources
    assert "embedding_from:subtitle_segment" in embedding_sources
    assert "embedding_from:vl_caption" in embedding_sources

    for task in tasks:
        assert {"schema_version", "run_id", "folder_id", "source_path", "media_id", "route", "task_type", "adapter_name", "adapter_mode", "status"} <= set(task)
        assert task["adapter_mode"] == "fake"
        assert task["model_loaded"] is False
    audio_plan = next(row for row in tasks if row["task_type"] == "audio_extraction_plan" and row["media_id"] == "video-audio-plan")
    assert audio_plan["planned_output_path"] == "audio_proxy/audio_clip.wav"
    assert audio_plan["reuse_existing_artifact"] is True

    assert any(row["media_id"] == "unsupported-one" and row["status"] == "blocked" for row in evidence)
    assert any(row["media_id"] == "stale-one" and row["status"] == "stale" and row["is_active"] is False for row in evidence)
    assert any(row["media_id"] == "offline-one" and row["status"] == "offline" and row["is_active"] is False for row in evidence)
    assert outputs
    assert not (workspace / "stages/v0.5/search").exists()
    assert not (workspace / "stages/v0.5/index").exists()
    assert not (workspace / "stages/v0.5/embeddings").exists()

    first_ids = [row["evidence_id"] for row in evidence]
    second = run_fixture(workspace, fixture)
    assert second.returncode == 0, second.stderr
    second_evidence = read_jsonl(evidence_path)
    second_summary = read_json(summary_path)
    assert [row["evidence_id"] for row in second_evidence] == first_ids
    assert second_summary["skipped_existing_count"] == len(first_ids)
    assert second_summary["new_evidence_count"] == 0
