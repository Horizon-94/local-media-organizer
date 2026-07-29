from __future__ import annotations

import json
from pathlib import Path

from apps.media_archive.v05_2b.high_value_frame_selection import run_selection


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _make_policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    _write_json(path, {"policy_version": "V0.5_QWENVL_HIGH_VALUE_AND_PROPAGATION_RULES_v1.2"})
    return path


def _make_workspace(tmp_path: Path, include_ocr: bool = True, omit_embedding_for: str | None = None, omit_yoloe_for: str | None = None) -> Path:
    workspace = tmp_path / "workspace"
    source_rows = []
    for index, name in enumerate(["a.jpg", "b.jpg", "c.jpg", "frame.jpg"]):
        path = workspace / "derived" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        is_frame = name == "frame.jpg"
        source_rows.append(
            {
                "smoke_image_id": f"image-{index}",
                "image_id": f"image-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": name,
                "image_group_type": "video_frame" if is_frame else "normal_preview",
                "upstream_source_type": "c4_video_frame" if is_frame else "a9t_normal",
                "original_source_path": f"/source/{name}",
                "source_video_path": "/source/v.mov" if is_frame else None,
                "source_video_relative_path": "v.mov" if is_frame else None,
                "estimated_frame_time_ms": 3000 if is_frame else None,
                "frame_index": 1 if is_frame else None,
            }
        )
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    yoloe_rows = []
    embedding_rows = []
    ocr_rows = []
    for row in source_rows:
        key = row["derived_image_path"]
        if key != omit_yoloe_for:
            yoloe_rows.append(
                {
                    "derived_image_path": key,
                    "success": True,
                    "detections": [{"label": "sign", "confidence": 0.8}]
                    if key.endswith("b.jpg")
                    else [{"label": "tree", "confidence": 0.4}]
                    if key.endswith("c.jpg")
                    else [{"label": "person", "confidence": 0.6}],
                }
            )
        if key != omit_embedding_for:
            embedding_rows.append({"derived_image_path": key, "success": True, "embedding_dim": 512})
        ocr_rows.append({"derived_image_path": key, "success": True, "text_block_count": 1 if key.endswith("b.jpg") else 0, "ocr_text_preview": "SALE" if key.endswith("b.jpg") else ""})
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)
    if include_ocr:
        _write_jsonl(workspace / "ocr_output/ocr_results.jsonl", ocr_rows)
    return workspace


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stable_test_name(video_name: str, index: int) -> str:
    safe = video_name.replace("/", "_").replace(".", "_")
    return f"{safe}_{index:03d}.jpg"


def test_high_value_selection_happy_path_with_optional_ocr(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, include_ocr=True)
    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["validation_status"] == "PASS"
    assert summary["input_alignment_status"] == "PASS"
    assert summary["candidate_count"] == 2
    assert summary["rejection_count"] == 2
    assert summary["candidate_ratio"] < 1.0
    assert summary["selection_policy_applied"] is True
    assert summary["bounded_selection_applied"] is True
    assert summary["content_diversity_applied"] is True
    assert summary["early_frame_penalty_applied"] is True
    for key in [
        "near_duplicate_rejected_count",
        "repeated_time_pattern_rejected_count",
        "selected_video_count",
        "selected_timelapse_sequence_count",
        "selected_normal_image_count",
        "average_selected_per_video",
        "max_selected_per_video",
        "videos_with_more_than_3_selected",
        "selected_frame_time_ms_distribution",
        "rejected_reason_counts",
        "machine_quality_pass",
    ]:
        assert key in summary
    assert summary["global_limit"] == 80
    assert summary["per_video_limit"] == 3
    assert summary["jpg_only_candidate_rule"] is True
    assert summary["normal_image_rejected_count"] == 2
    assert summary["ocr_downgraded_for_visual_selection"] is True
    assert summary["ocr_only_candidate_count"] == 0
    assert summary["candidates_without_visual_primary_evidence"] == 0
    assert summary["videos_with_more_than_3_selected"] == 0
    assert "rejected_reason_counts" in summary
    assert "selected_frame_time_ms_distribution" in summary
    assert summary["qwen_vl_ready"] is True
    assert summary["qwen_vl_not_run"] is True
    assert summary["search_index_built"] is False
    assert summary["a9t_rerun"] is False
    assert summary["c4_rerun"] is False
    assert summary["yoloe_rerun"] is False
    assert summary["ocr_rerun"] is False
    assert summary["embedding_rerun"] is False
    candidates = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_candidates.jsonl")
    assert all(row["visual_primary_evidence"] for row in candidates)
    assert any(row["video_time_ms"] == 3000 for row in candidates)
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    assert any(row["rejection_reason"] == "normal_image_preview_low_cost_only" for row in rejections)
    assert any(row["rejection_reason"] == "ocr_only_or_text_heavy_not_visual_high_value" for row in rejections)
    assert (workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl").exists()
    assert (workspace / "high_value_frame_selection/high_value_frame_groups.jsonl").exists()
    assert (workspace / "reports/high_value_frame_selection_summary.md").exists()


def test_missing_ocr_does_not_block_selection_when_policy_does_not_require_ocr(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, include_ocr=False)
    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["validation_status"] == "PASS"
    assert summary["ocr_rows"] == 0
    assert summary["ocr_required_for_qwen_vl_candidate_selection"] is False
    assert summary["ocr_missing_blocks_qwen_vl_ready"] is False
    assert summary["qwen_vl_ready"] is True


def test_text_bearing_yoloe_label_marks_ocr_recommended(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, include_ocr=False)
    run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    text_rejection = next(row for row in rejections if row["preview_path"].endswith("b.jpg"))
    assert text_rejection["rejection_reason"] == "ocr_only_or_text_heavy_not_visual_high_value"


def test_missing_embedding_blocks_readiness_if_required(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, omit_embedding_for=str(tmp_path / "workspace/derived/a.jpg"))
    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["validation_status"] == "BLOCKED"
    assert summary["qwen_vl_ready"] is False
    assert summary["input_alignment_status"] == "WARN"
    assert summary["input_alignment_details"]["missing_embedding_count"] == 1
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    assert any(row["rejection_reason"] == "missing_embedding_evidence" for row in rejections)


def test_yoloe_rows_fewer_than_source_map_are_reported(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, omit_yoloe_for=str(tmp_path / "workspace/derived/a.jpg"))
    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["yoloe_rows"] == 3
    assert summary["source_map_rows"] == 4
    assert summary["input_alignment_status"] == "WARN"
    assert summary["input_alignment_details"]["missing_yoloe_count"] == 1


def test_video_frames_are_grouped_capped_and_adjacent_frames_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    for index in range(20):
        path = workspace / "derived" / f"frame_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        source_rows.append(
            {
                "smoke_image_id": f"frame-{index}",
                "image_id": f"frame-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": path.name,
                "image_group_type": "video_frame",
                "upstream_source_type": "c4_video_frame",
                "source_video_path": "/source/v.mov",
                "source_video_relative_path": "v.mov",
                "estimated_frame_time_ms": index * 1000,
                "frame_index": index,
            }
        )
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "person", "confidence": 0.9 - index * 0.01}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["candidate_count"] == 1
    assert summary["rejection_count"] == 19
    assert summary["video_source_count_selected"] == 1
    assert summary["temporal_spacing_applied"] is True
    assert summary["content_diversity_applied"] is True
    assert summary["early_frame_penalty_applied"] is True
    assert summary["input_alignment_details"]["early_frame_rejected_count"] > 0
    assert summary["input_alignment_details"]["near_duplicate_rejected_count"] > 0
    assert summary["input_alignment_details"]["repeated_time_pattern_rejected_count"] > 0
    assert summary["videos_with_1_selected"] == 1
    assert summary["videos_with_more_than_3_selected"] == 0
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    reasons = {row["rejection_reason"] for row in rejections}
    assert "early_frame_low_value" in reasons
    assert "same_composition_repeated_frame" in reasons
    assert "repeated_time_pattern" in reasons


def test_different_video_content_can_select_more_than_one_frame(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    ocr_rows = []
    for index, (timestamp, label, text) in enumerate([(1000, "person", ""), (9000, "tractor", ""), (17000, "sign", "NOTICE")]):
        path = workspace / "derived" / f"frame_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((f"jpg-{index}" * (index + 1) * 100).encode())
        source_rows.append(
            {
                "smoke_image_id": f"frame-{index}",
                "image_id": f"frame-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": path.name,
                "image_group_type": "video_frame",
                "upstream_source_type": "c4_video_frame",
                "source_video_path": "/source/v.mov",
                "source_video_relative_path": "v.mov",
                "estimated_frame_time_ms": timestamp,
                "frame_index": index,
            }
        )
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": label, "confidence": 0.8}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
        ocr_rows.append({"derived_image_path": str(path), "success": True, "text_block_count": 1 if text else 0, "ocr_text_preview": text})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)
    _write_jsonl(workspace / "ocr_output/ocr_results.jsonl", ocr_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["candidate_count"] == 2
    candidates = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_candidates.jsonl")
    assert all(row["selection_gate_passed"] is True for row in candidates)
    assert all(row["content_diversity_status"] == "content_diverse" for row in candidates)


def test_ocr_difference_does_not_override_same_composition_rejection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    ocr_rows = []
    for index, text in enumerate(["HELLO", "DIFFERENT"]):
        path = workspace / "derived" / f"screen_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"same-composition-bytes")
        source_rows.append(
            {
                "smoke_image_id": f"screen-{index}",
                "image_id": f"screen-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": path.name,
                "image_group_type": "video_frame",
                "upstream_source_type": "c4_video_frame",
                "source_video_path": "/source/screen.mov",
                "source_video_relative_path": "screen.mov",
                "estimated_frame_time_ms": 3000 + index * 8000,
                "frame_index": index,
            }
        )
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "person", "confidence": 0.8}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
        ocr_rows.append({"derived_image_path": str(path), "success": True, "text_block_count": 1, "ocr_text_preview": text})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)
    _write_jsonl(workspace / "ocr_output/ocr_results.jsonl", ocr_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["candidate_count"] == 1
    assert summary["ocr_difference_not_visual_difference_rejected_count"] == 1
    assert summary["ocr_only_candidate_count"] == 0
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    assert rejections[0]["rejection_reason"] == "ocr_difference_not_visual_difference"


def test_video_coverage_floor_can_exceed_target_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    for index in range(5):
        path = workspace / "derived" / f"video_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"video-{index}".encode())
        source_rows.append(
            {
                "smoke_image_id": f"video-{index}",
                "image_id": f"video-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": path.name,
                "image_group_type": "video_frame",
                "upstream_source_type": "c4_video_frame",
                "source_video_path": f"/source/v{index}.mov",
                "source_video_relative_path": f"v{index}.mov",
                "estimated_frame_time_ms": 3000,
                "frame_index": 1,
            }
        )
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "person", "confidence": 0.8}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path), global_limit=2)

    assert summary["target_global_limit"] == 2
    assert summary["effective_global_limit"] == 5
    assert summary["candidate_count"] == 5
    assert summary["total_source_video_count"] == 5
    assert summary["eligible_video_count"] == 5
    assert summary["selected_video_count"] == 5
    assert summary["zero_selected_eligible_video_count"] == 0
    assert summary["video_coverage_pass"] is True


def test_screen_recording_and_text_heavy_videos_are_routed_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    ocr_rows = []
    specs = [
        ("normal.mov", 1, False),
        ("IPhone/RPReplay_Final123.MP4", 2, True),
        ("text-heavy.mov", 10, True),
    ]
    for video_name, frame_count, with_ocr in specs:
        for index in range(frame_count):
            path = workspace / "derived" / stable_test_name(video_name, index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{video_name}-{index}".encode())
            source_rows.append(
                {
                    "smoke_image_id": f"{video_name}-{index}",
                    "image_id": f"{video_name}-{index}",
                    "derived_image_path": str(path),
                    "derived_image_relative_path": path.name,
                    "image_group_type": "video_frame",
                    "upstream_source_type": "c4_video_frame",
                    "source_video_path": f"/source/{video_name}",
                    "source_video_relative_path": video_name,
                    "estimated_frame_time_ms": 3000 + index * 8000,
                    "frame_index": index,
                }
            )
            yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "person", "confidence": 0.8}]})
            embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
            ocr_rows.append({"derived_image_path": str(path), "success": True, "text_block_count": 1 if with_ocr else 0, "ocr_text_preview": "TEXT" if with_ocr else ""})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)
    _write_jsonl(workspace / "ocr_output/ocr_results.jsonl", ocr_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["total_source_video_count"] == 3
    assert summary["screen_or_text_heavy_video_count"] == 2
    assert summary["screen_recording_video_routed_count"] == 1
    assert summary["text_heavy_video_routed_count"] == 2
    assert summary["eligible_video_count"] == 1
    assert summary["selected_video_count"] == 1
    assert summary["video_coverage_pass"] is True
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    reasons = {row["rejection_reason"] for row in rejections}
    assert "screen_or_recording_not_visual_high_value" in reasons
    assert "text_heavy_video_routed" in reasons


def test_normal_image_preview_requires_high_value_evidence_and_respects_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    for index in range(8):
        path = workspace / "derived" / f"normal_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        source_rows.append(
            {
                "smoke_image_id": f"normal-{index}",
                "image_id": f"normal-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": path.name,
                "image_group_type": "normal_preview",
                "upstream_source_type": "a9t_normal",
            }
        )
        label = "person" if index < 5 else "sign"
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": label, "confidence": 0.7}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path), normal_image_global_limit=3)

    assert summary["candidate_count"] == 3
    assert summary["normal_image_selected_count"] == 3
    assert summary["normal_image_rejected_count"] == 5
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    assert sum(row["rejection_reason"] == "normal_image_global_limit_exceeded" for row in rejections) == 2
    assert sum(row["rejection_reason"] == "ocr_only_or_text_heavy_not_visual_high_value" for row in rejections) == 3


def test_non_jpg_and_unknown_role_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    rows = []
    yoloe_rows = []
    embedding_rows = []
    for name, role_fields in [
        ("not_allowed.jpeg", {"image_group_type": "normal_preview", "upstream_source_type": "a9t_normal"}),
        ("unknown.jpg", {}),
    ]:
        path = workspace / "derived" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        row = {
            "smoke_image_id": name,
            "image_id": name,
            "derived_image_path": str(path),
            "derived_image_relative_path": name,
            **role_fields,
        }
        rows.append(row)
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "person", "confidence": 0.8}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path))

    assert summary["validation_status"] == "BLOCKED"
    assert summary["candidate_count"] == 0
    assert summary["non_jpg_rejected_count"] == 1
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    reasons = {row["rejection_reason"] for row in rejections}
    assert "non_jpg_derived_artifact_not_allowed" in reasons
    assert "unknown_or_unsupported_source_role" in reasons


def test_timelapse_keyframes_are_grouped_and_capped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_rows = []
    yoloe_rows = []
    embedding_rows = []
    for index in range(5):
        path = workspace / "derived" / f"timelapse_{index:03d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        source_rows.append(
            {
                "smoke_image_id": f"tl-{index}",
                "image_id": f"tl-{index}",
                "derived_image_path": str(path),
                "derived_image_relative_path": f"sequence/timelapse_{index:03d}.jpg",
                "original_relative_path": f"sequence/source_{index:03d}.jpg",
                "image_group_type": "timelapse_keyframe",
                "upstream_source_type": "a9t_timelapse",
            }
        )
        yoloe_rows.append({"derived_image_path": str(path), "success": True, "detections": [{"label": "field", "confidence": 0.8}]})
        embedding_rows.append({"derived_image_path": str(path), "success": True, "embedding_dim": 512})
    _write_jsonl(workspace / "reports/full_source_map.jsonl", source_rows)
    _write_jsonl(workspace / "yoloe_output/yoloe_results.jsonl", yoloe_rows)
    _write_jsonl(workspace / "embedding_output/visual_embedding_results.jsonl", embedding_rows)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path), per_timelapse_sequence_limit=3)

    assert summary["candidate_count"] == 1
    assert summary["timelapse_sequence_count_selected"] == 1
    assert summary["input_alignment_details"]["near_duplicate_rejected_count"] == 4
    rejections = _read_jsonl(workspace / "high_value_frame_selection/high_value_frame_rejections.jsonl")
    assert all(row["rejection_reason"] == "same_composition_repeated_frame" for row in rejections)


def test_review_pack_contains_only_selected_jpg_candidates(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)

    summary = run_selection(workspace=workspace, policy=_make_policy(tmp_path), write_review_pack=True)

    assert summary["review_pack_written"] is True
    review_dir = workspace / "reports/high_value_frame_selection_review"
    assert (review_dir / "review_index.html").exists()
    assert (review_dir / "review_summary.json").exists()
    review_rows = _read_jsonl(review_dir / "review_manifest.jsonl")
    assert len(review_rows) == summary["candidate_count"]
    assert all(Path(row["thumb_path"]).suffix == ".jpg" for row in review_rows)
    assert all(Path(row["artifact_path"]).suffix == ".jpg" for row in review_rows)
    assert all("content_diversity_status" in row for row in review_rows)
    assert all("selection_gate_reasons" in row for row in review_rows)


def test_candidate_order_is_deterministic_and_source_is_not_written(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    source = tmp_path / "source_original.jpg"
    source.write_bytes(b"source")
    before = source.stat()

    first = run_selection(workspace=workspace, policy=_make_policy(tmp_path))
    first_rows = (workspace / "high_value_frame_selection/high_value_frame_candidates.jsonl").read_text(encoding="utf-8")
    second = run_selection(workspace=workspace, policy=_make_policy(tmp_path))
    second_rows = (workspace / "high_value_frame_selection/high_value_frame_candidates.jsonl").read_text(encoding="utf-8")

    assert first["candidate_count"] == second["candidate_count"]
    assert first_rows == second_rows
    after = source.stat()
    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns


def test_project_docs_record_canonical_low_cost_order_and_ocr_conditional_policy() -> None:
    for path in [Path("README.md"), Path("PROJECT_BASELINE.md")]:
        text = path.read_text(encoding="utf-8")
        assert "visual_embedding full evidence run, 6 workers" in text
        assert "YOLOE full evidence run, 6 workers" in text
        assert "OCR is conditional / selective / fallback evidence" in text
