from __future__ import annotations

import copy
from dataclasses import asdict

import pytest

from media_archive.v05.rules import (
    CONCURRENCY_PROFILES,
    EvidenceRecord,
    EVIDENCE_TYPES,
    FolderBatch,
    ResourceReport,
    SCHEMA_VERSION,
    build_folder_batches,
    classify_folder,
    evaluate_resource_status,
    generate_evidence_id,
    make_evidence_record,
    resolve_file_identity,
    select_high_value_frames,
    validate_evidence_record,
    validate_folder_batch,
)


def identity() -> dict[str, object]:
    return resolve_file_identity(content_hash="abc123")


def evidence_kwargs(evidence_type: str) -> dict[str, object]:
    file_id = identity()
    return {
        "evidence_id": "",
        "run_id": "run-1",
        "media_id": "media-1",
        "source_path": "/source/a/one.jpg",
        "source_root": "/source",
        "folder_id": "folder-a",
        "folder_path": "a",
        "content_hash": "abc123",
        "file_identity": file_id["file_identity"],
        "identity_strategy": file_id["identity_strategy"],
        "evidence_type": evidence_type,
        "producer": "fixture",
        "model_name": "fixture-model",
        "model_version": "1",
    }


def test_all_evidence_types_validate_and_required_fields_are_enforced():
    for evidence_type in sorted(EVIDENCE_TYPES):
        record = make_evidence_record(**evidence_kwargs(evidence_type))
        assert record.schema_version == SCHEMA_VERSION
        validate_evidence_record(record)

    bad = asdict(make_evidence_record(**evidence_kwargs("ocr_text")))
    bad.pop("hit_time_ms")
    with pytest.raises(ValueError, match="missing evidence fields"):
        validate_evidence_record(bad)


def test_image_time_fields_are_present_null_and_stale_offline_are_inactive():
    record = make_evidence_record(**evidence_kwargs("ocr_text"))
    data = asdict(record)
    assert {"hit_time_ms", "start_time_ms", "end_time_ms"} <= set(data)
    assert data["hit_time_ms"] is None
    assert data["start_time_ms"] is None
    assert data["end_time_ms"] is None

    bad_time = dict(data)
    bad_time["hit_time_ms"] = 1000
    with pytest.raises(ValueError, match="image evidence time fields"):
        validate_evidence_record(bad_time)

    stale = dict(data)
    stale["status"] = "stale"
    stale["is_active"] = False
    validate_evidence_record(stale)
    stale["is_active"] = True
    with pytest.raises(ValueError, match="stale/offline"):
        validate_evidence_record(stale)


def test_shared_field_types_are_consistent_across_v05_structures():
    evidence_fields = EvidenceRecord.__dataclass_fields__
    folder_fields = FolderBatch.__dataclass_fields__
    resource_fields = ResourceReport.__dataclass_fields__

    assert evidence_fields["schema_version"].type == folder_fields["schema_version"].type == resource_fields["schema_version"].type
    assert evidence_fields["run_id"].type == resource_fields["run_id"].type
    assert evidence_fields["folder_id"].type == folder_fields["folder_id"].type == resource_fields["folder_id"].type
    assert evidence_fields["folder_path"].type == folder_fields["folder_path"].type == resource_fields["folder_path"].type
    assert evidence_fields["source_root"].type == folder_fields["source_root"].type
    assert evidence_fields["source_path"].type == "str"
    assert evidence_fields["media_id"].type == "str"
    assert evidence_fields["created_at"].type == "str"
    assert evidence_fields["status"].type == folder_fields["status"].type

    required_resource_fields = {
        "schema_version",
        "run_id",
        "folder_id",
        "folder_path",
        "stage_name",
        "worker_profile",
        "cpu_percent",
        "memory_used_bytes",
        "memory_pressure",
        "swap_used_bytes",
        "swap_growth_bytes_per_min",
        "disk_read_mb_s",
        "disk_write_mb_s",
        "disk_busy_percent",
        "queue_depth",
        "avg_task_duration_ms",
        "p95_task_duration_ms",
        "baseline_p95_task_duration_ms",
        "success_count",
        "fail_count",
        "skip_count",
        "retry_count",
        "resource_status",
        "recommendation",
    }
    assert required_resource_fields <= set(resource_fields)


def test_evidence_id_is_deterministic_and_changes_by_type_or_time():
    base = asdict(make_evidence_record(**evidence_kwargs("speech_text"), start_time_ms=0, end_time_ms=1000, hit_time_ms=500, text="hello"))
    first = generate_evidence_id(base)
    second = generate_evidence_id(copy.deepcopy(base))
    assert first == second

    changed_type = dict(base)
    changed_type["evidence_type"] = "subtitle_segment"
    assert generate_evidence_id(changed_type) != first

    changed_time = dict(base)
    changed_time["hit_time_ms"] = 600
    assert generate_evidence_id(changed_time) != first


def test_file_identity_priority_and_blocking_rules():
    content = resolve_file_identity(content_hash="abc")
    assert content["identity_strategy"] == "content_hash"
    assert content["status"] == "success"

    pathstat = resolve_file_identity(
        source_root="/src",
        normalized_relative_path="a/one.jpg",
        size_bytes=10,
        mtime_ns=99,
        volume_id="vol",
    )
    assert pathstat["identity_strategy"] == "pathstat"
    assert pathstat["file_identity"] != content["file_identity"]

    offline = resolve_file_identity(file_online=False, last_seen_file_identity="last-id")
    assert offline == {
        "file_identity": "last-id",
        "identity_strategy": "last_seen_offline",
        "status": "offline",
        "is_active": False,
        "error_code": None,
    }

    blocked = resolve_file_identity(source_root="/src")
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "insufficient_file_identity"


def frame(frame_id: str, **overrides: object) -> dict[str, object]:
    data = {
        "frame_id": frame_id,
        "media_id": "media-a",
        "folder_id": "folder-a",
        "source_path": f"/src/{frame_id}.jpg",
        "hit_time_ms": 10_000 * int(frame_id.split("-")[-1]),
        "yoloe_labels": [],
        "ocr_text": "",
        "visual_cluster_id": None,
        "embedding_distance": 0.1,
        "visual_difference_score": 0.1,
        "is_existing_keyframe": False,
        "fixed_query_hit": False,
        "fixed_query_score": 0,
    }
    data.update(overrides)
    return data


def by_id(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["frame_id"]): row for row in rows}


def test_high_value_reasons_are_derived_and_output_is_deterministic(tmp_path):
    frames = [
        frame("f-1", ocr_text="下雨", yoloe_labels=[{"label": "person", "confidence": 0.9}], fixed_query_score=0.6),
        frame("f-2", yoloe_labels=[{"label": "cloud", "confidence": 0.99}], visual_difference_score=0.4, is_existing_keyframe=True),
        frame("f-3", is_existing_keyframe=True, is_time_distribution_representative=True),
    ]
    first, report = select_high_value_frames(frames, output_dir=tmp_path)
    second, _ = select_high_value_frames(copy.deepcopy(frames))
    assert first == second
    assert report["schema_version"] == SCHEMA_VERSION
    assert (tmp_path / "high_value_frame_candidates.jsonl").exists()
    assert (tmp_path / "high_value_frame_selection_report.json").exists()

    rows = by_id(first)
    assert rows["f-1"]["selected_for_qwen_vl"] is True
    assert rows["f-1"]["selection_reason"] == "ocr_text_found"
    assert "yoloe_important_object" in rows["f-1"]["matched_reasons"]
    assert "fixed_query_related" in rows["f-1"]["matched_reasons"]
    assert rows["f-2"]["selected_for_qwen_vl"] is True
    assert "visual_difference_high" in rows["f-2"]["matched_reasons"]
    assert rows["f-3"]["excluded_reason"] == "below_threshold"


def test_duplicate_similarity_and_time_gap_rules_do_not_infer_pairwise_from_embedding_distance():
    same_cluster = [
        frame("f-1", visual_cluster_id="c1", ocr_text="abc", embedding_distance=0.9, hit_time_ms=0),
        frame("f-2", visual_cluster_id="c1", ocr_text="def", embedding_distance=0.8, hit_time_ms=10_000),
    ]
    rows, _ = select_high_value_frames(same_cluster)
    assert any(str(row["excluded_reason"]).startswith("duplicate_of_frame:") for row in rows)

    pair = "f-3|f-4"
    pairwise = [
        frame("f-3", ocr_text="abc", hit_time_ms=20_000, frame_pair_similarity={pair: 0.93}),
        frame("f-4", ocr_text="def", hit_time_ms=30_000, frame_pair_similarity={pair: 0.93}),
    ]
    rows = select_high_value_frames(pairwise)[0]
    assert any(str(row["excluded_reason"]).startswith("duplicate_of_frame:") for row in rows)

    no_pairwise = [
        frame("f-5", ocr_text="abc", hit_time_ms=40_000, embedding_distance=0.99),
        frame("f-6", ocr_text="def", hit_time_ms=50_000, embedding_distance=0.99),
    ]
    rows = select_high_value_frames(no_pairwise)[0]
    assert all(row["selected_for_qwen_vl"] for row in rows)

    close = [
        frame("f-7", ocr_text="abc", hit_time_ms=60_000),
        frame("f-8", ocr_text="def", hit_time_ms=61_000),
    ]
    rows = select_high_value_frames(close)[0]
    assert any(str(row["excluded_reason"]).startswith("time_gap_too_close:") for row in rows)


def test_timelapse_difference_rules_and_limits():
    obvious = [
        frame("f-1", timelapse_group_id="tl1", visual_difference_score=0.5, ocr_text="abc", hit_time_ms=0),
        frame("f-2", timelapse_group_id="tl1", visual_difference_score=0.6, yoloe_labels=[{"label": "car", "confidence": 0.8}], hit_time_ms=10_000),
        frame("f-3", timelapse_group_id="tl1", visual_difference_score=0.7, fixed_query_hit=True, hit_time_ms=20_000),
    ]
    rows = select_high_value_frames(obvious)[0]
    assert all(by_id(rows)[fid]["selected_for_qwen_vl"] for fid in ["f-1", "f-2", "f-3"])

    redundant = [
        frame("f-4", timelapse_group_id="tl2", ocr_text="abc", embedding_distance=0.1, hit_time_ms=40_000),
        frame("f-5", timelapse_group_id="tl2", ocr_text="def", embedding_distance=0.2, hit_time_ms=50_000),
        frame("f-6", timelapse_group_id="tl2", ocr_text="ghi", embedding_distance=0.3, hit_time_ms=60_000),
    ]
    rows = select_high_value_frames(redundant)[0]
    assert sum(1 for row in rows if row["selected_for_qwen_vl"]) == 1
    assert sum(1 for row in rows if row["excluded_reason"] == "timelapse_redundant_low_cost_kept") == 2

    limited = [frame(f"f-{i}", media_id="m", folder_id="folder-a", ocr_text=f"text{i}", hit_time_ms=i * 10_000) for i in range(10, 15)]
    rows = select_high_value_frames(limited, {"max_selected_per_media": 2, "max_selected_per_folder": 3})[0]
    assert sum(1 for row in rows if row["selected_for_qwen_vl"]) == 2
    assert any(row["excluded_reason"] == "media_limit_exceeded" for row in rows)


def resource_sample(**overrides: object) -> dict[str, object]:
    data = {
        "cpu_percent": 50,
        "swap_growth_bytes_per_min": 0,
        "disk_busy_percent": 20,
        "success_count": 10,
        "fail_count": 0,
        "p95_task_duration_ms": 100,
        "baseline_p95_task_duration_ms": 100,
        "queue_depth": 12,
    }
    data.update(overrides)
    return data


def test_resource_status_safe_warning_dangerous_and_unknown():
    safe = evaluate_resource_status(resource_sample())
    assert safe["resource_status"] == "safe"
    assert safe["recommendation"] == "can_try_balanced"

    no_queue = evaluate_resource_status(resource_sample(queue_depth=1))
    assert no_queue["recommendation"] == "hold_current_profile"

    cpu_warning = evaluate_resource_status(resource_sample(cpu_percent=75))
    assert cpu_warning["resource_status"] == "warning"

    swap_warning = evaluate_resource_status(resource_sample(swap_growth_bytes_per_min=1))
    assert swap_warning["recommendation"] == "swap_bound_do_not_increase"

    disk_warning = evaluate_resource_status(resource_sample(disk_busy_percent=75))
    assert disk_warning["recommendation"] == "io_bound_do_not_increase"

    p95_warning = evaluate_resource_status(resource_sample(p95_task_duration_ms=130))
    assert p95_warning["resource_status"] == "warning"

    dangerous = evaluate_resource_status(resource_sample(cpu_percent=95, queue_depth=None))
    assert dangerous["resource_status"] == "dangerous"

    unknown = evaluate_resource_status(resource_sample(cpu_percent=None))
    assert unknown["resource_status"] == "unknown"
    assert unknown["recommendation"] == "unknown_insufficient_metrics"

    assert set(CONCURRENCY_PROFILES["profiles"]) == {"safe", "balanced", "aggressive"}
    assert CONCURRENCY_PROFILES["profiles"]["safe"]["qwen_vl_workers"] <= CONCURRENCY_PROFILES["profiles"]["aggressive"]["qwen_vl_workers"]


def test_folder_batch_classification_boundaries_and_plan_fields():
    assert classify_folder({"video_count": 6, "image_count": 4}) == "video_dominant"
    assert classify_folder({"video_count": 59, "image_count": 41}) == "mixed"
    assert classify_folder({"other_count": 10}) == "unsupported_or_empty"
    assert classify_folder({"image_count": 5, "audio_count": 5}, {"dominant_ratio": 0.5}) == "image_dominant"

    records = [
        {"source_relative_path": "video/a.mov", "media_kind": "video"},
        {"source_relative_path": "video/b.mov", "media_kind": "video"},
        {"source_relative_path": "image/a.jpg", "media_kind": "image"},
        {"source_relative_path": "audio/a.wav", "media_kind": "audio"},
        {"source_relative_path": "text/a.srt", "media_kind": "text"},
        {"source_relative_path": "empty/a.xyz", "media_kind": "other"},
    ]
    batches = build_folder_batches(records, "/src")
    assert len(batches) == 5
    assert {batch["folder_path"] for batch in batches} == {"video", "image", "audio", "text", "empty"}
    assert all(batch["status"] == "pending" for batch in batches)
    assert all(batch["checkpoint_ref"] for batch in batches)
    assert all("current_folder" in batch["progress"] for batch in batches)
    assert all("global_folder_progress" in batch["progress"] for batch in batches)
    for batch in batches:
        validate_folder_batch(batch)
