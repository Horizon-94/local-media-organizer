from __future__ import annotations

import json
from pathlib import Path

from apps.media_archive.v05_2b.bounded_visual_inference import (
    build_duplicate_inference_reuse_audit,
    build_input_selection,
    build_prerequisite_gate,
    write_json,
    write_jsonl,
)


def make_v052a_fixture(root: Path) -> None:
    reports = root / "reports"
    write_json(
        reports / "stage_execution_evidence_audit.json",
        {
            "validation_status": "PASS",
            "full_chain_from_v01_to_v052a": True,
            "dedup_reuse_verified": True,
            "background_supervised_verified": True,
            "v04_probe_only": True,
            "v04_real_inference_executed": False,
        },
    )
    write_json(reports / "canonical_background_command.json", {"canonical_command_status": "selected"})
    write_json(reports / "dedup_reuse_evidence_v4.json", {"validation_status": "PASS", "dedup_reuse_verified": True})
    write_json(reports / "resource_summary.json", {"resource_telemetry_collected": True})
    write_json(reports / "worker_config.json", {"model_workers": 1})
    write_jsonl(reports / "content_identity_manifest.jsonl", [{"content_id": "sha256:dup"}])
    write_jsonl(reports / "artifact_reuse_manifest.jsonl", [{"content_id": "sha256:dup"}])
    write_jsonl(reports / "mini_source_manifest.jsonl", [{"source_path": "/source/a.jpg"}])

    preview_rows = []
    for index in range(8):
        artifact = root / "v02/image_preview/normal" / f"p{index}.jpg"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"jpg")
        content_id = "sha256:dup" if index == 0 else f"sha256:image{index}"
        preview_rows.append(
            {
                "preview_status": "success",
                "preview_path": str(artifact),
                "content_id": content_id,
                "source_path": f"/source/image{index}.jpg",
                "canonical_path": f"/source/image{index}.jpg",
                "all_paths": [f"/source/image{index}.jpg", f"/source/image{index}-copy.jpg"] if index == 0 else [f"/source/image{index}.jpg"],
                "path_count": 2 if index == 0 else 1,
            }
        )
    frame_rows = []
    for index in range(8):
        artifact = root / "v02/video_frames/frames" / f"f{index}.jpg"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"jpg")
        frame_rows.append(
            {
                "frame_status": "success",
                "frame_path": str(artifact),
                "content_id": f"sha256:video{index}",
                "source_path": f"/source/video{index}.mov",
                "canonical_path": f"/source/video{index}.mov",
                "all_paths": [f"/source/video{index}.mov"],
                "path_count": 1,
                "estimated_frame_time_ms": 1000 + index,
                "frame_index": index,
            }
        )
    write_jsonl(root / "v02/image_preview/preview_manifest.jsonl", preview_rows)
    write_jsonl(root / "v02/video_frames/video_frame_manifest.jsonl", frame_rows)


def test_v052b_gate_selection_and_duplicate_audit(tmp_path: Path) -> None:
    v052a = tmp_path / "v052a"
    make_v052a_fixture(v052a)

    gate = build_prerequisite_gate(v052a)
    assert gate["validation_status"] == "PASS"
    assert gate["allowed_to_run_v052b"] is True

    selection = build_input_selection(v052a)
    assert selection["validation_status"] == "PASS"
    assert selection["selected_image_preview_count"] == 8
    assert selection["selected_video_frame_count"] == 8
    assert selection["selected_duplicate_content_group_count"] >= 1
    assert selection["used_raw_original_as_model_input"] is False

    duplicate_item = next(item for item in selection["selected_items"] if item["path_count"] == 2)
    tasks = [
        {
            "content_id": duplicate_item["content_id"],
            "model_role": "object_detection",
            "dedup_inference_action": "generated_once",
        }
    ]
    audit = build_duplicate_inference_reuse_audit(selection["selected_items"], tasks)
    assert audit["validation_status"] == "PASS"
    assert audit["same_content_multiple_paths_detected"] is True
    assert audit["duplicate_paths_repeated_model_inference"] is False
