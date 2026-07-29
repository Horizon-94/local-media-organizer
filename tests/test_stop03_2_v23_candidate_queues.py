from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


STAGING = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "03_stop03_visual_analysis"
if not SCRIPT_DIR.exists():
    SCRIPT_DIR = STAGING
CANDIDATE_PATH = SCRIPT_DIR / "stop03_2_candidate_queues_from_db_safe_v23_0_20260710_190836.py"
CONFIG_PATH = ROOT / "configs" / "stop03_2_high_value_policy_v23.json"
if not CONFIG_PATH.exists():
    CONFIG_PATH = STAGING / "stop03_2_high_value_policy_v23.json"
RULE_PATH = ROOT / "docs" / "pipeline_rules" / "STOP03_2_GENERIC_HIGH_VALUE_RULES_V23.md"
if not RULE_PATH.exists():
    RULE_PATH = STAGING / "STOP03_2_GENERIC_HIGH_VALUE_RULES_V23.md"
sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    spec = importlib.util.spec_from_file_location("stop03_2_v23_candidate", CANDIDATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v23 = load_module()


def config():
    return v23.load_config(CONFIG_PATH)[0]


def canonical_frame() -> dict:
    return {
        "visual_unit_id": "canonical_0",
        "canonical_visual_unit_id": "canonical_0",
        "source_content_id": "source_1",
        "derived_id": "derived_0",
        "visual_file": "/derived/canonical_0.jpg",
        "source_relative_path": "normal/video.mov",
        "media_type": "video",
        "identity_status": "canonical",
        "duplicate_group_id": "group_1",
        "duplicate_reverse_member_count": 13,
        "duplicate_reverse_visual_unit_ids": "|".join(f"raw_{index}" for index in range(13)),
        "frame_index": 0,
        "time_position_ms": 0,
        "canonical_time_ms": 0,
        "group_start_ms": 0,
        "group_end_ms": 36000,
        "sampled_sequence_index": 0,
        "signature_status": "PASS",
        "black_rejected": False,
        "grid": tuple(float(index) for index in range(80)),
        "grid_std": 20.0,
        "grid_structure": 14.0,
        "vector": (1.0, 0.0, 0.0),
        "labels": [
            {
                "label": "person",
                "confidence": 0.9,
                "area": 0.1,
                "center_distance": 0.1,
                "touches_edge": False,
            }
        ],
        "generic_label_categories": ["human_social"],
        "raw_source_start_ms": 0,
        "raw_source_end_ms": 36000,
    }


def raw_frames(count: int = 13) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                "visual_unit_id": f"raw_{index}",
                "canonical_visual_unit_id": "canonical_0",
                "source_content_id": "source_1",
                "derived_id": f"raw_derived_{index}",
                "frame_index": index,
                "time_position_ms": index * 3000,
                "sampled_sequence_index": index,
                "media_type": "video",
                "eligible_for_heavy_models": index == 0,
                "visual_duplicate_group_id": "group_1",
            }
        )
    return rows


def test_v23_is_standalone_and_reads_canonical_database_views():
    text = CANDIDATE_PATH.read_text(encoding="utf-8")
    assert "FROM canonical_visual_units_for_heavy vu" in text
    assert "JOIN canonical_source_assets_for_heavy sa" in text
    assert "FROM visual_labels" in text
    assert "FROM embeddings" in text
    for token in ("mlx_vlm", "ultralytics", "paddleocr", "ffmpeg", "V22_OUT", "V14_OUT"):
        assert token not in text


def test_anchors_start_at_center_and_expand_by_six_frames():
    assert v23.anchor_indices(13, 6) == [6, 0, 12]
    intervals = v23.anchor_intervals([6, 0, 12], 13, 3)
    assert intervals == [(6, 3, 9), (0, 0, 3), (12, 9, 12)]


def test_central_dedup_refill_reuses_one_canonical_without_duplicate_queue_rows():
    cfg = config()
    result = v23.select_candidates(
        [canonical_frame()],
        {"source_1": raw_frames()},
        cfg,
        "run",
        "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"},
        {},
    )
    assert len(result["q_rows"]) == 1
    assert result["video_budget"][0]["coverage_anchor_count"] == 3
    assert result["video_budget"][0]["coverage_selected_count"] == 3
    assert result["video_budget"][0]["coverage_unique_representative_count"] == 1
    assert result["stats"]["coverage_refill_failed_count"] == 0
    assert result["stats"]["coverage_refill_after_central_dedup_count"] == 2
    assert result["stats"]["coverage_local_candidate_pool_count"] == 3
    assert {row["visual_unit_id"] for row in result["q_rows"]} == {"canonical_0"}
    assert "coverage_reused_across_anchor_intervals" in result["q_rows"][0]["reason_codes"]
    ordinary = next(row for row in result["coverage_reports"] if row["original_anchor_index"] == 6)
    assert ordinary["effective_anchor_index"] == 6
    assert ordinary["anchor_remap_reason"] == ""


def test_tail_only_final_anchor_remaps_with_fixed_radius_and_reuses_canonical():
    raw = raw_frames(168)
    result = v23.select_candidates(
        [canonical_frame()], {"source_1": raw}, config(), "run", "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"}, {},
    )
    tail_start = v23.tail_start_ms(raw, config())
    expected_effective = max(
        index for index, frame in enumerate(raw)
        if 0 <= frame["time_position_ms"] < tail_start
    )
    report = next(row for row in result["coverage_reports"] if row["original_anchor_index"] == 167)
    assert report["original_interval_start_index"] == 164
    assert report["original_interval_end_index"] == 167
    assert report["effective_anchor_index"] == expected_effective
    assert report["effective_interval_start_index"] == expected_effective - 3
    assert report["effective_interval_end_index"] == expected_effective + 3
    assert report["anchor_remap_reason"] == "tail_only_anchor_remapped_to_last_non_tail"
    assert result["stats"]["coverage_missing_count"] == 0
    assert result["stats"]["coverage_refill_failed_count"] == 0
    assert result["stats"]["non_short_video_tail_fallback_count"] == 0
    assert result["stats"]["tail_only_anchor_count"] == 1
    assert result["stats"]["tail_anchor_remap_count"] == 1
    assert result["stats"]["tail_anchor_remap_failed_count"] == 0
    assert result["stats"]["tail_anchor_reused_existing_canonical_count"] == 1
    assert len(result["q_rows"]) == 1
    assert "tail_only_anchor_remapped_to_last_non_tail" in result["q_rows"][0]["reason_codes"]


def test_tail_only_remap_fails_when_video_has_no_non_tail_position(monkeypatch):
    monkeypatch.setattr(v23, "tail_start_ms", lambda rows, cfg: 0)
    result = v23.select_candidates(
        [canonical_frame()], {"source_1": raw_frames(7)}, config(), "run", "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"}, {},
    )
    assert result["stats"]["tail_only_anchor_count"] == 1
    assert result["stats"]["tail_anchor_remap_count"] == 0
    assert result["stats"]["tail_anchor_remap_failed_count"] == 1
    assert result["stats"]["coverage_missing_count"] == 1
    assert result["stats"]["non_short_video_tail_fallback_count"] == 0
    report = result["coverage_reports"][0]
    assert report["effective_anchor_index"] == -1
    assert report["anchor_remap_reason"] == "tail_only_anchor_remap_failed_no_non_tail_sampled_frame"


def test_short_video_can_use_last_valid_canonical_tail_fallback():
    frame = canonical_frame()
    frame.update({"sampled_sequence_index": 4, "frame_index": 4, "time_position_ms": 12000, "canonical_time_ms": 12000})
    result = v23.select_candidates(
        [frame], {"source_1": raw_frames()[:5]}, config(), "run", "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"}, {},
    )
    assert result["stats"]["short_video_count"] == 1
    assert result["stats"]["short_video_tail_fallback_count"] == 1
    assert "short_video_tail_fallback" in result["q_rows"][0]["reason_codes"]


def test_non_short_video_cannot_use_tail_fallback_and_reports_missing_coverage():
    frame = canonical_frame()
    frame.update({"sampled_sequence_index": 6, "frame_index": 6, "time_position_ms": 18000, "canonical_time_ms": 18000})
    result = v23.select_candidates(
        [frame], {"source_1": raw_frames()[:7]}, config(), "run", "dry-run",
        {"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        {"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"}, {},
    )
    assert result["q_rows"] == []
    assert result["stats"]["short_video_tail_fallback_count"] == 0
    assert result["stats"]["non_short_video_tail_fallback_count"] == 0
    assert result["stats"]["coverage_refill_failed_count"] == 1
    assert result["stats"]["coverage_missing_count"] == 1


def test_duplicate_reverse_mapping_preserves_raw_and_canonical_timecodes():
    canonical = canonical_frame()
    rows = v23.reverse_mapping_rows(raw_frames(), {"canonical_0": canonical})
    last = rows[-1]
    assert last["visual_unit_id"] == "raw_12"
    assert last["canonical_visual_unit_id"] == "canonical_0"
    assert last["duplicate_group_id"] == "group_1"
    assert last["time_position_ms"] == 36000
    assert last["canonical_time_ms"] == 0
    assert (last["group_start_ms"], last["group_end_ms"]) == (0, 36000)


def test_empty_label_sets_do_not_create_label_only_duplicates():
    cfg = config()
    left = {"time_position_ms": 0, "labels": [], "vector": (1.0, 0.0), "grid": tuple([1.0] * 80)}
    right = {"time_position_ms": 1000, "labels": [], "vector": (0.0, 1.0), "grid": tuple([20.0] * 80)}
    evidence = v23.duplicate_evidence(left, right, cfg)
    assert evidence["label_jaccard"] is None
    assert evidence["duplicate"] is False


def test_openclip_payload_is_fully_validated_but_only_canonical_vectors_are_bound(tmp_path: Path):
    payload = tmp_path / "vectors.jsonl"
    payload.write_text(
        "\n".join(
            json.dumps({"embedding_id": embedding_id, "visual_unit_id": visual_id, "vector": vector})
            for embedding_id, visual_id, vector in (
                ("emb_canonical", "canonical", [1.0, 0.0]),
                ("emb_duplicate", "duplicate", [0.0, 1.0]),
            )
        ) + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE embeddings(embedding_id TEXT,visual_unit_id TEXT,vector_key TEXT,run_id TEXT)")
    con.executemany(
        "INSERT INTO embeddings VALUES(?,?,?,?)",
        [
            ("emb_canonical", "canonical", f"jsonl:{payload}#emb_canonical", "clip_run"),
            ("emb_duplicate", "duplicate", f"jsonl:{payload}#emb_duplicate", "clip_run"),
        ],
    )
    vectors, stats = v23.load_vectors(con, {"canonical"})
    con.close()
    assert set(vectors) == {"canonical"}
    assert stats["vector_payload_row_count"] == 2
    assert stats["vector_db_row_count"] == 2


def test_screen_recording_is_routed_out_of_qwen_video_candidates():
    cfg = config()
    assert v23.is_screen_recording("captures/Screen Recording 2026.mov", cfg)
    reasons = v23.hard_rejection_reasons(
        {"signature_status": "PASS", "black_rejected": False, "identity_status": "canonical", "time_position_ms": 0},
        screen_recording=True,
        tail_start=None,
    )
    assert "screen_recording_routed_to_ocr" in reasons


def test_candidate_rows_preserve_required_lineage_fields():
    row = v23.make_candidate(
        canonical_frame(),
        queue_type="qwenvl_high_value",
        role="video_coverage_keyframe",
        score=3.0,
        reasons=["coverage"],
        run_id="run",
        mode="dry-run",
        hashes={"script_sha256": "s", "config_sha256": "c", "rule_document_sha256": "r"},
        lineage={"central_dedup_run_id": "dedup", "yoloe_run_id": "yoloe", "openclip_run_id": "clip"},
        config=config(),
    )
    required = {
        "source_content_id", "visual_unit_id", "canonical_visual_unit_id", "duplicate_group_id",
        "derived_id", "frame_index", "time_position_ms", "canonical_time_ms", "group_start_ms",
        "group_end_ms", "reason_codes", "policy_version", "script_version", "script_sha256",
        "config_sha256", "central_dedup_run_id", "yoloe_run_id", "openclip_run_id",
    }
    assert required <= row.keys()
    assert row["execution_mode"] == "dry-run"


def test_config_freezes_stride_grid_and_review_only_policy():
    cfg = config()
    assert cfg["coverage_stride_frames"] == 6
    assert cfg["coverage_local_radius_frames"] == 3
    assert (cfg["grid_cols"], cfg["grid_rows"]) == (16, 5)
    assert cfg["policy_status"] == "FROZEN_CANDIDATE"


def test_rule_remains_frozen_candidate_without_v22_acceptance_dependency():
    text = RULE_PATH.read_text(encoding="utf-8")
    assert "状态：`FROZEN_CANDIDATE`" in text
    assert "policy_version = stop03_2_generic_high_value_policy_v23" in text
    assert "与 V22 canonical 结果做差异说明" not in text


def test_defined_local_radius_must_be_applied_or_technical_status_fails():
    stats = Counter({
        "normal_video_group_count": 1,
        "normal_video_group_with_coverage_count": 1,
        "coverage_anchor_total_count": 1,
        "coverage_local_candidate_evaluation_count": 1,
        "local_candidate_evaluation_count": 1,
        "vector_grid_label_time_pair_evaluation_count": 1,
    })
    row = canonical_frame()
    q_row = {
        "visual_unit_id": "canonical_0",
        "candidate_role": "video_coverage_keyframe",
        "source_relative_path": "normal/video.mov",
    }
    result = {"rows": [row], "q_rows": [q_row], "o_rows": [], "stats": stats,
              "coverage_reports": [], "video_budget": []}
    pre = {
        "config": config(), "script_sha256": "s", "config_sha256": "c",
        "rule_document_sha256": "r", "run_id": "run", "raw_visual_input_count": 1,
        "canonical_visual_input_count": 1, "canonical_source_input_count": 1,
        "dedup_excluded_visual_count": 0, "central_dedup_run_id": "dedup",
        "yoloe_run_id": "yoloe", "openclip_run_id": "clip",
    }
    summary = v23.build_summary(
        result, pre,
        {"grid_signature_success_count": 1, "grid_signature_failed_count": 0},
        {"vector_payload_integrity_status": "PASS"},
        {"bbox_invalid_count": 0},
        "dry-run", True, True,
    )
    assert summary["technical_status"] == "FAIL"
    assert summary["automatic_acceptance_gates"]["coverage_local_radius_applied"] is False


def test_preflight_snapshot_does_not_precreate_reports_directory(tmp_path: Path):
    out = tmp_path / "v23"
    out.mkdir()
    v23.write_json(out / "preflight.json", {"technical_status": "PASS"})
    paths = v23.write_outputs(
        out,
        {"q_rows": [], "o_rows": [], "decisions": [], "coverage_reports": [], "video_budget": []},
        {"technical_status": "PASS"},
        [],
    )
    assert Path(paths["summary_json"]).is_file()
    assert (out / "preflight.json").is_file()
