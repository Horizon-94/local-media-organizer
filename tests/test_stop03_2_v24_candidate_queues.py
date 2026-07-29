from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
PROJECT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
SCRIPT_DIR = PROJECT / "scripts" / "03_stop03_visual_analysis"
V24_PATH = HERE / "stop03_2_candidate_queues_from_db_safe_v24_0_20260711.py"
V24_CONFIG = HERE / "stop03_2_high_value_policy_v24.json"
if (SCRIPT_DIR / V24_PATH.name).exists():
    V24_PATH = SCRIPT_DIR / V24_PATH.name
    V24_CONFIG = PROJECT / "configs" / "stop03_2_high_value_policy_v24.json"
sys.path.insert(0, str(SCRIPT_DIR))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v24 = load(V24_PATH, "stop03_2_v24")
v23 = load(
    SCRIPT_DIR / "stop03_2_candidate_queues_from_db_safe_v23_0_20260710_190836.py",
    "stop03_2_v23_for_regression",
)


def cfg():
    return v24.load_config(V24_CONFIG)[0]


def test_v24_formal_policy_identity_is_frozen_candidate():
    config = cfg()
    assert config["policy_version"] == "stop03_2_generic_high_value_policy_v24"
    assert config["policy_status"] == "FROZEN_CANDIDATE"


def label(name: str) -> dict:
    return {
        "label": name, "confidence": 0.9, "area": 0.1,
        "center_distance": 0.1, "touches_edge": False, "category": "other",
    }


def image_frame(
    visual_id: str, source_id: str, *, vector=(1.0, 0.0), grid=None,
    labels=("person",), sha: str | None = None, near_black: bool = False,
) -> dict:
    grid = tuple(float(index) for index in range(80)) if grid is None else tuple(grid)
    return {
        "visual_unit_id": visual_id, "canonical_visual_unit_id": visual_id,
        "source_content_id": source_id, "derived_id": f"derived_{visual_id}",
        "visual_file": f"/derived/{visual_id}.jpg", "source_relative_path": f"album/{visual_id}.jpg",
        "media_type": "image", "identity_status": "canonical", "duplicate_group_id": "",
        "duplicate_reverse_member_count": 1, "duplicate_reverse_visual_unit_ids": visual_id,
        "frame_index": -1, "time_position_ms": -1, "canonical_time_ms": -1,
        "group_start_ms": -1, "group_end_ms": -1, "sampled_sequence_index": -1,
        "signature_status": "PASS", "black_rejected": near_black,
        "grid": grid, "grid_std": 20.0, "grid_structure": 12.0,
        "vector": tuple(vector), "labels": [label(item) for item in labels],
        "generic_label_categories": [], "derived_sha256": sha or f"sha_{visual_id}",
    }


def mapping(original: str, source: str, canonical: str) -> dict:
    return {
        "visual_unit_id": original, "source_content_id": source,
        "canonical_visual_unit_id": canonical, "visual_duplicate_group_id": "",
        "identity_status": "canonical" if original == canonical else "near_duplicate",
    }


def source(source_id: str, path: str, media_type: str, extension: str) -> dict:
    return {
        "source_content_id": source_id, "relative_path": path,
        "file_name": Path(path).name, "extension": extension,
        "media_type": media_type, "is_deleted_or_missing": 0,
    }


def empty_inputs() -> dict:
    return {"finder_tags": [], "source_assets": [], "visual_mappings": [], "timelapse_rows": []}


def select_images(frames: list[dict], inputs: dict):
    return v24.select_v24_image_candidates(
        frames, inputs, cfg(), "run", "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"},
    )


@pytest.mark.parametrize("color", ["red", "yellow", "blue", "green", "purple"])
def test_any_nonempty_finder_tag_color_selects_image(color: str):
    frame = image_frame("canonical", "image_source", near_black=True)
    inputs = empty_inputs()
    inputs["source_assets"] = [source("image_source", "album/a.jpg", "image", ".jpg")]
    inputs["visual_mappings"] = [mapping("canonical", "image_source", "canonical")]
    inputs["finder_tags"] = [{
        "tag_id": f"tag_{color}", "source_content_id": "image_source",
        "tag_raw": color, "tag_name": color, "tag_color": color,
    }]
    result = select_images([frame], inputs)
    assert [row["candidate_role"] for row in result["q_rows"]] == ["image_finder_tag_seed"]


def test_multiple_finder_tags_and_duplicate_visual_collapse_to_one_canonical():
    frame = image_frame("canonical", "canonical_source")
    inputs = empty_inputs()
    inputs["source_assets"] = [source("tagged_source", "album/a.jpg", "image", ".jpg")]
    inputs["visual_mappings"] = [mapping("duplicate", "tagged_source", "canonical")]
    inputs["finder_tags"] = [
        {"tag_id": "t1", "source_content_id": "tagged_source", "tag_raw": "red", "tag_name": "red", "tag_color": "red"},
        {"tag_id": "t2", "source_content_id": "tagged_source", "tag_raw": "blue", "tag_name": "blue", "tag_color": "blue"},
    ]
    result = select_images([frame], inputs)
    assert len(result["q_rows"]) == 1
    assert result["q_rows"][0]["visual_unit_id"] == "canonical"
    assert result["summary"]["finder_tag_canonical_unique_count"] == 1
    assert result["summary"]["finder_tag_canonical_collapse_count"] == 1


def test_non_image_finder_tag_is_counted_but_not_selected():
    inputs = empty_inputs()
    inputs["source_assets"] = [source("video", "clips/a.mov", "video", ".mov")]
    inputs["finder_tags"] = [{"tag_id": "t", "source_content_id": "video", "tag_raw": "red", "tag_name": "red", "tag_color": "red"}]
    result = select_images([], inputs)
    assert result["q_rows"] == []
    assert result["summary"]["finder_tag_non_image_source_count"] == 1


@pytest.mark.parametrize("extension", [".xmp", ".XMP", "xMp"])
def test_xmp_same_directory_and_case_insensitive_extension_matches(extension: str):
    frame = image_frame("canonical", "image")
    inputs = empty_inputs()
    inputs["source_assets"] = [
        source("image", "Album/Shot01.JPG", "image", ".JPG"),
        source("xmp", "Album/shot01.XMP", "other", extension),
    ]
    inputs["visual_mappings"] = [mapping("canonical", "image", "canonical")]
    result = select_images([frame], inputs)
    assert [row["candidate_role"] for row in result["q_rows"]] == ["image_xmp_sidecar_seed"]


def test_xmp_different_directory_and_xml_do_not_match():
    frame = image_frame("canonical", "image")
    inputs = empty_inputs()
    inputs["source_assets"] = [
        source("image", "AlbumA/shot.jpg", "image", ".jpg"),
        source("xmp_other", "AlbumB/shot.xmp", "other", ".xmp"),
        source("xml", "AlbumA/shot.xml", "other", ".xml"),
    ]
    inputs["visual_mappings"] = [mapping("canonical", "image", "canonical")]
    result = select_images([frame], inputs)
    assert result["q_rows"] == []
    assert result["summary"]["xmp_sidecar_source_count"] == 1
    assert result["summary"]["xmp_unmatched_sidecar_count"] == 1


def test_one_xmp_can_match_multiple_image_sources():
    frames = [image_frame("c1", "i1"), image_frame("c2", "i2")]
    inputs = empty_inputs()
    inputs["source_assets"] = [
        source("i1", "Album/shot.jpg", "image", ".jpg"),
        source("i2", "Album/shot.tif", "image", ".tif"),
        source("x", "Album/shot.xmp", "other", ".xmp"),
    ]
    inputs["visual_mappings"] = [mapping("c1", "i1", "c1"), mapping("c2", "i2", "c2")]
    result = select_images(frames, inputs)
    assert len(result["q_rows"]) == 2
    assert result["summary"]["xmp_multi_image_match_group_count"] == 1


def timelapse_inputs(frames: list[dict]) -> dict:
    inputs = empty_inputs()
    inputs["visual_mappings"] = [mapping(frame["visual_unit_id"], frame["source_content_id"], frame["visual_unit_id"]) for frame in frames]
    inputs["timelapse_rows"] = [
        {"visual_unit_id": frame["visual_unit_id"], "sequence_id": "seq", "representative_position": position,
         "source_relative_path": frame["source_relative_path"], "visual_file": frame["visual_file"],
         "parent_source_content_id": frame["source_content_id"]}
        for frame, position in zip(frames, ("first", "middle", "last"))
    ]
    return inputs


def changed_frame(visual_id: str, source_id: str) -> dict:
    return image_frame(
        visual_id, source_id, vector=(0.0, 1.0),
        grid=tuple(float(200 - index) for index in range(80)), labels=("vehicle",),
    )


def test_timelapse_both_similar_selects_one_middle():
    frames = [image_frame("first", "s1", sha="same"), image_frame("middle", "s2", sha="same"), image_frame("last", "s3", sha="same")]
    result = select_images(frames, timelapse_inputs(frames))
    assert [row["visual_unit_id"] for row in result["q_rows"]] == ["middle"]
    assert result["summary"]["timelapse_select_one_sequence_count"] == 1


def test_timelapse_one_changed_segment_selects_two():
    frames = [image_frame("first", "s1", sha="same"), image_frame("middle", "s2", sha="same"), changed_frame("last", "s3")]
    result = select_images(frames, timelapse_inputs(frames))
    assert {row["visual_unit_id"] for row in result["q_rows"]} == {"middle", "last"}
    assert result["summary"]["timelapse_select_two_sequence_count"] == 1


def test_timelapse_both_changed_segments_selects_three():
    first = image_frame("first", "s1", vector=(1.0, 0.0, 0.0), labels=("person",))
    middle = image_frame("middle", "s2", vector=(0.0, 1.0, 0.0), grid=tuple(float(200-index) for index in range(80)), labels=("vehicle",))
    last = image_frame("last", "s3", vector=(0.0, 0.0, 1.0), grid=tuple(float((index*17)%255) for index in range(80)), labels=("document",))
    frames = [first, middle, last]
    result = select_images(frames, timelapse_inputs(frames))
    assert len(result["q_rows"]) == 3
    assert result["summary"]["timelapse_select_three_sequence_count"] == 1


def test_empty_label_sets_cannot_alone_make_timelapse_pair_similar():
    left = image_frame("left", "s1", vector=(1.0, 0.0), grid=tuple(float(index) for index in range(80)), labels=())
    right = image_frame("right", "s2", vector=(0.0, 1.0), grid=tuple(float(200-index) for index in range(80)), labels=())
    evidence = v24.timelapse_pair_similarity(left, right, cfg())
    assert evidence["label_jaccard"] is None
    assert evidence["similar"] is False


def test_finder_xmp_timelapse_overlap_produces_one_finder_priority_candidate():
    frame = image_frame("canonical", "image")
    inputs = timelapse_inputs([frame])
    inputs["source_assets"] = [
        source("image", "Album/shot.jpg", "image", ".jpg"),
        source("xmp", "Album/shot.xmp", "other", ".xmp"),
    ]
    inputs["finder_tags"] = [{"tag_id": "t", "source_content_id": "image", "tag_raw": "blue", "tag_name": "blue", "tag_color": "blue"}]
    result = select_images([frame], inputs)
    assert len(result["q_rows"]) == 1
    assert result["q_rows"][0]["candidate_role"] == "image_finder_tag_seed"
    assert result["summary"]["all_three_overlap_count"] == 1


def test_ordinary_image_without_authoritative_source_is_never_auto_selected():
    result = select_images([image_frame("ordinary", "source")], empty_inputs())
    assert result["q_rows"] == []
    assert result["summary"]["image_generic_visual_signal_candidate_count"] == 0


def video_frame() -> dict:
    frame = image_frame("video_canonical", "video_source")
    frame.update({
        "media_type": "video", "source_relative_path": "normal/video.mov",
        "frame_index": 0, "time_position_ms": 0, "canonical_time_ms": 0,
        "group_start_ms": 0, "group_end_ms": 36000, "sampled_sequence_index": 0,
        "raw_source_start_ms": 0, "raw_source_end_ms": 36000,
    })
    return frame


def raw_video_frames() -> list[dict]:
    return [{
        "visual_unit_id": f"raw_{index}", "canonical_visual_unit_id": "video_canonical",
        "source_content_id": "video_source", "derived_id": f"raw_d_{index}",
        "frame_index": index, "time_position_ms": index * 3000,
        "sampled_sequence_index": index, "media_type": "video",
        "eligible_for_heavy_models": index == 0,
    } for index in range(13)]


def test_v24_video_semantics_match_v23_fixture_including_coverage_contract():
    hashes = {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"}
    lineage = {"central_dedup_run_id": "d", "yoloe_run_id": "y", "openclip_run_id": "o"}
    v24_result = v24.select_candidates([video_frame()], {"video_source": raw_video_frames()}, cfg(), "run", "dry-run", hashes, lineage, empty_inputs())
    v23_cfg = v23.load_config(PROJECT / "configs" / "stop03_2_high_value_policy_v23.json")[0]
    v23_result = v23.select_candidates([video_frame()], {"video_source": raw_video_frames()}, v23_cfg, "run", "dry-run", hashes, lineage, {})
    assert v24.video_semantic_set(v24_result["q_rows"]) == v24.video_semantic_set(v23_result["q_rows"])
    assert v24.anchor_indices(13, 6) == v23.anchor_indices(13, 6)
    assert v24.anchor_intervals([6, 0, 12], 13, 3) == v23.anchor_intervals([6, 0, 12], 13, 3)


def test_video_regression_report_passes_for_equal_semantic_sets(tmp_path: Path):
    row = {
        "media_type": "video", "source_content_id": "s", "visual_unit_id": "v",
        "canonical_visual_unit_id": "v", "candidate_role": "video_coverage_keyframe",
        "time_position_ms": 1000,
    }
    baseline = tmp_path / "baseline.jsonl"
    baseline.write_text(__import__("json").dumps(row) + "\n", encoding="utf-8")
    report = v24.compare_v23_video_semantics([row], baseline)
    assert report["status"] == "PASS"
    assert report["video_logic_changed"] is False
