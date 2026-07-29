from __future__ import annotations

import copy
import json
from pathlib import Path

from apps.media_archive.rules.qwenvl_rule_schema import (
    NormalImageArtifact,
    NormalImageHighValueCandidate,
    PropagationDecision,
    SemanticAtom,
    SemanticPropagation,
    VideoFrameArtifact,
    VideoHighValueCandidate,
)
from apps.media_archive.rules.validate_qwenvl_rules import (
    REQUIRED_CONSTANTS,
    validate_high_value_candidate,
    validate_policy,
    validate_policy_file,
    validate_semantic_propagation,
)


POLICY_PATH = Path("apps/media_archive/rules/qwenvl_high_value_policy_v1_2.json")


def load_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def test_policy_json_loads_and_contains_all_v12_thresholds() -> None:
    policy = load_policy()
    assert validate_policy(policy) == []
    assert REQUIRED_CONSTANTS <= set(policy["constants"])
    assert policy["constants"]["WEAK_OBJECT_EMBEDDING_DISTANCE_THRESHOLD"] == 0.18
    assert policy["related_object_check_mode"] == "yoloe_if_supported_else_weak_visual"
    assert policy["count_propagation_requires_yoloe"] is True
    assert policy["count_propagation_tolerance"] == 0
    assert policy["direct_anchor_required"] is True
    assert policy["exclusion_manifest_allowed"] is False


def test_video_candidate_gap_is_twice_propagation_radius() -> None:
    constants = load_policy()["constants"]
    assert constants["VIDEO_CANDIDATE_MIN_GAP_MS"] == 2 * constants["VIDEO_PROPAGATION_RADIUS_MS"]


def test_count_propagation_cannot_use_weak_visual_similarity() -> None:
    propagation = {
        "semantic_atom_id": "atom-count",
        "confidence": 0.5,
        "atom_type": "count",
        "propagation_source": "propagated_object_weak_visual",
        "propagated_count_verified": False,
    }
    errors = validate_semantic_propagation(propagation)
    assert "count propagation cannot use weak visual similarity" in errors
    assert "count propagation must have propagated_count_verified" in errors


def test_text_propagation_cannot_bypass_ocr() -> None:
    propagation = {
        "semantic_atom_id": "atom-text",
        "confidence": 0.8,
        "atom_type": "text",
        "propagation_source": "propagated_text_verified_by_ocr",
    }
    assert "text propagation must have OCR verification source" in validate_semantic_propagation(propagation)


def test_direct_anchor_is_legal_and_exclusion_manifest_is_forbidden() -> None:
    policy = load_policy()
    assert policy["exclusion_manifest_allowed"] is False
    propagation = {
        "semantic_atom_id": "atom-1",
        "confidence": 1.0,
        "atom_type": "scene",
        "propagation_source": "direct_anchor",
    }
    assert validate_semantic_propagation(propagation) == []


def test_schema_objects_minimal_examples_can_be_built() -> None:
    VideoFrameArtifact(
        artifact_id="artifact-video",
        artifact_type="video_frame",
        artifact_path="/tmp/frame.jpg",
        source_media_id="media-video",
        source_path="/tmp/video.mov",
        source_relative_path="video.mov",
        width=1920,
        height=1080,
        created_time=None,
        file_mtime="2026-07-03T00:00:00Z",
        source_video_id="video-1",
        source_video_path="/tmp/video.mov",
        source_video_relative_path="video.mov",
        frame_index=0,
        estimated_frame_time_ms=1000,
        video_frame_id="frame-1",
    )
    NormalImageArtifact(
        artifact_id="artifact-image",
        artifact_type="normal_image",
        artifact_path="/tmp/image.jpg",
        source_media_id="media-image",
        source_path="/tmp/image.jpg",
        source_relative_path="image.jpg",
        width=100,
        height=100,
        created_time=None,
        file_mtime="2026-07-03T00:00:00Z",
        relative_dir=".",
    )
    VideoHighValueCandidate(
        candidate_id="candidate-video",
        candidate_type="video_frame_candidate",
        artifact_id="artifact-video",
        source_media_id="media-video",
        artifact_path="/tmp/frame.jpg",
        source_path="/tmp/video.mov",
        selection_score=0.9,
        selection_reason="strong_scene_change",
        selection_policy_version="qwen-vl-high-value-selection-v1.2",
        candidate_rank_in_group=1,
        source_video_id="video-1",
        source_video_path="/tmp/video.mov",
        source_video_relative_path="video.mov",
        video_frame_id="frame-1",
        estimated_frame_time_ms=1000,
    )
    NormalImageHighValueCandidate(
        candidate_id="candidate-image",
        candidate_type="normal_image_candidate",
        artifact_id="artifact-image",
        source_media_id="media-image",
        artifact_path="/tmp/image.jpg",
        source_path="/tmp/image.jpg",
        selection_score=0.7,
        selection_reason="cluster_representative",
        selection_policy_version="qwen-vl-high-value-selection-v1.2",
        candidate_rank_in_group=1,
    )
    SemanticAtom(
        semantic_atom_id="atom-1",
        qwen_anchor_id="anchor-1",
        atom_type="text",
        raw_text="hello",
        normalized_text="hello",
        canonical_label=None,
        confidence_at_anchor=0.8,
        anchor_artifact_id="artifact-image",
        anchor_time_ms=None,
        text_value="hello",
    )
    SemanticPropagation(
        propagation_id="prop-1",
        semantic_atom_id="atom-1",
        qwen_anchor_id="anchor-1",
        source_media_id="media-image",
        target_type="normal_image",
        target_artifact_id="artifact-image",
        target_start_ms=None,
        target_end_ms=None,
        propagated_text="hello",
        atom_type="text",
        confidence=1.0,
        propagation_source="direct_anchor",
        propagation_rule_version="qwen-vl-semantic-propagation-v1.2",
    )
    PropagationDecision(allowed=True, confidence=1.0, propagation_source="direct_anchor")


def test_validator_fails_on_missing_policy_constant(tmp_path: Path) -> None:
    policy = copy.deepcopy(load_policy())
    policy["constants"].pop("WEAK_OBJECT_EMBEDDING_DISTANCE_THRESHOLD")
    bad_policy = tmp_path / "bad_policy.json"
    bad_policy.write_text(json.dumps(policy), encoding="utf-8")
    result = validate_policy_file(bad_policy)
    assert result["validation_status"] == "FAIL"
    assert any("WEAK_OBJECT_EMBEDDING_DISTANCE_THRESHOLD" in error for error in result["errors"])


def test_validator_fails_on_missing_candidate_fields() -> None:
    assert validate_high_value_candidate({"candidate_type": "normal_image_candidate"})
    errors = validate_high_value_candidate(
        {
            "candidate_type": "video_frame_candidate",
            "artifact_id": "artifact-video",
            "estimated_frame_time_ms": 1000,
        }
    )
    assert "video candidate must have video_frame_id" in errors
