from __future__ import annotations

import json
from pathlib import Path

from apps.media_archive.v05_2b.keyframe_selection import run_keyframe_selection


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _make_v052a_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "v05-2A-bg-canonical-oneshot-20260703-140319"
    reports = workspace / "reports"
    _write_json(
        reports / "stage_execution_evidence_audit.json",
        {
            "validation_status": "PASS",
            "full_chain_from_v01_to_v052a": True,
            "dedup_reuse_verified": True,
            "background_supervised_verified": True,
            "source_read_only": True,
            "model_dir_written": False,
        },
    )
    _write_json(reports / "dedup_reuse_evidence_v4.json", {"validation_status": "PASS", "dedup_reuse_verified": True})
    _write_json(reports / "resource_summary.json", {"resource_telemetry_collected": True})
    _write_json(reports / "worker_config.json", {"sips_workers": 8})
    _write_jsonl(reports / "content_identity_manifest.jsonl", [{"content_id": "sha256:image1"}])
    _write_jsonl(reports / "artifact_reuse_manifest.jsonl", [{"content_id": "sha256:image1"}])
    _write_jsonl(reports / "mini_source_manifest.jsonl", [{"mini_source_path": str(workspace / "mini-source/a.jpg")}])

    preview1 = workspace / "v02/image_preview/normal_preview_pool_jpg/a.jpg"
    preview2 = workspace / "v02/image_preview/normal_preview_pool_jpg/b.jpg"
    frame1 = workspace / "v02/video_frames/video_frame_jpg/v_000001.jpg"
    frame2 = workspace / "v02/video_frames/video_frame_jpg/v_000002.jpg"
    frame3 = workspace / "v02/video_frames/video_frame_jpg/v_000003.jpg"
    for path in (preview1, preview2, frame1, frame2, frame3):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpg")

    _write_jsonl(
        workspace / "v02/image_preview/preview_manifest.jsonl",
        [
            {
                "preview_path": str(preview1),
                "preview_status": "success",
                "content_id": "sha256:image1",
                "canonical_path": "/source/a.jpg",
                "source_path": "/source/a.jpg",
                "all_paths": ["/source/a.jpg", "/source/a-copy.jpg"],
                "path_count": 2,
            },
            {
                "preview_path": str(preview1),
                "preview_status": "success",
                "content_id": "sha256:image1",
                "canonical_path": "/source/a.jpg",
                "source_path": "/source/a-copy.jpg",
                "all_paths": ["/source/a.jpg", "/source/a-copy.jpg"],
                "path_count": 2,
            },
            {
                "preview_path": str(preview2),
                "preview_status": "success",
                "content_id": "sha256:image2",
                "canonical_path": "/source/b.heic",
                "source_path": "/source/b.heic",
                "all_paths": ["/source/b.heic"],
                "path_count": 1,
            },
        ],
    )
    _write_jsonl(
        workspace / "v02/video_frames/video_frame_manifest.jsonl",
        [
            {
                "frame_path": str(frame1),
                "frame_status": "success",
                "content_id": "sha256:video1",
                "canonical_path": "/source/v.mov",
                "source_video_path": "/source/v.mov",
                "all_paths": ["/source/v.mov"],
                "path_count": 1,
                "estimated_frame_time_ms": 1000,
                "frame_index": 0,
            },
            {
                "frame_path": str(frame2),
                "frame_status": "success",
                "content_id": "sha256:video1",
                "canonical_path": "/source/v.mov",
                "source_video_path": "/source/v.mov",
                "all_paths": ["/source/v.mov"],
                "path_count": 1,
                "estimated_frame_time_ms": 3000,
                "frame_index": 1,
            },
            {
                "frame_path": str(frame3),
                "frame_status": "success",
                "content_id": "sha256:video1",
                "canonical_path": "/source/v.mov",
                "source_video_path": "/source/v.mov",
                "all_paths": ["/source/v.mov"],
                "path_count": 1,
                "estimated_frame_time_ms": 5000,
                "frame_index": 2,
            },
        ],
    )
    _write_json(workspace / "v02/image_preview/timelapse_sequences.json", [])
    _write_json(workspace / "v02/image_preview/group_timelapse_diagnosis.json", [])
    _write_jsonl(workspace / "v02/image_preview/keyframe_pool_manifest.jsonl", [])
    return workspace


def test_v052b0_keyframe_selection_writes_contract_reports(tmp_path: Path) -> None:
    input_workspace = _make_v052a_workspace(tmp_path)
    previous = tmp_path / "v05-2B-1-bounded-visual-inference-20260703-143540"
    _write_json(previous / "reports/v052b_stage_execution_audit.json", {"validation_status": "PASS_WITH_BACKLOG"})
    workspace = tmp_path / "v05-2B-0-keyframe-selection-test"

    audit = run_keyframe_selection(input_workspace, previous, workspace)

    reports = workspace / "reports"
    assert audit["validation_status"] == "PASS"
    assert audit["model_inference_executed"] is False
    assert audit["search_index_built"] is False
    assert audit["previous_v052b1_reference_available"] is True
    eligible = json.loads((reports / "v052b_eligible_visual_artifacts.json").read_text())
    base = json.loads((reports / "v052b_base_model_input_manifest.json").read_text())
    high = json.loads((reports / "v052b_high_value_keyframe_manifest.json").read_text())
    propagation = json.loads((reports / "v052b_qwenvl_propagation_target_manifest.json").read_text())
    assert eligible["eligible_visual_artifact_count"] == 5
    assert eligible["dedup_skipped_model_input_count"] == 1
    assert base["same_input_set_for_yoloe_ocr_embedding"] is True
    assert base["yoloe_input_count"] == base["ocr_input_count"] == base["visual_embedding_input_count"]
    assert high["high_value_keyframe_count"] > 0
    assert high["qwenvl_input_policy"] == "high_value_keyframes_only"
    assert propagation["propagation_window_ms"] == 2000
    rows = [json.loads(line) for line in (reports / "v052b_high_value_keyframe_manifest.jsonl").read_text().splitlines()]
    assert any(row["artifact_kind"] == "video_frame" and row["video_time_ms"] is not None for row in rows)
    assert (reports / "git_provenance.json").exists()
    assert (reports / "codex_execution_trace.jsonl").exists()
