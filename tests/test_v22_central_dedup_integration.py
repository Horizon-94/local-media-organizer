from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "03_stop03_visual_analysis"
if not SCRIPT_DIR.exists():
    SCRIPT_DIR = Path("/Users/yourname/Documents/AI-Local/media-archive-clean/scripts/03_stop03_visual_analysis")
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_2_candidate_queues_from_db_safe_v22_0_20260710_112936 as v22
import stop03_2_v22_video_frame_contact_sheet_20260710_112936 as contact


def create_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE source_assets (
          source_content_id TEXT PRIMARY KEY, absolute_path TEXT, relative_path TEXT,
          media_type TEXT, online_status INTEGER, is_deleted_or_missing INTEGER
        );
        CREATE TABLE derived_assets (
          derived_id TEXT PRIMARY KEY, source_content_id TEXT, derived_path TEXT,
          time_position_ms INTEGER, frame_index INTEGER, width INTEGER, height INTEGER, sha256 TEXT
        );
        CREATE TABLE visual_units (
          visual_unit_id TEXT PRIMARY KEY, source_content_id TEXT, derived_id TEXT,
          visual_file TEXT, time_position_ms INTEGER
        );
        CREATE TABLE visual_identity (
          visual_unit_id TEXT PRIMARY KEY, identity_status TEXT,
          canonical_visual_unit_id TEXT, eligible_for_heavy_models INTEGER
        );
        CREATE TABLE visual_duplicate_groups (
          visual_duplicate_group_id TEXT PRIMARY KEY, canonical_visual_unit_id TEXT
        );
        CREATE TABLE visual_labels (
          label_id TEXT PRIMARY KEY, visual_unit_id TEXT, label TEXT,
          confidence REAL, bbox TEXT
        );
        CREATE VIEW canonical_visual_units_for_heavy AS
          SELECT vu.* FROM visual_units vu JOIN visual_identity vi
          ON vi.visual_unit_id=vu.visual_unit_id
          WHERE vi.eligible_for_heavy_models=1
            AND vi.identity_status IN ('unique','canonical','blocked_decoder');
        """
    )
    con.execute(
        "INSERT INTO source_assets VALUES(?,?,?,?,?,?)",
        ("source", "/readonly/video.mp4", "video.mp4", "video", 1, 0),
    )
    for index, time_ms in enumerate((0, 18000, 36000), 1):
        derived_id = f"derived_{index}"
        visual_id = f"visual_{index}"
        con.execute(
            "INSERT INTO derived_assets VALUES(?,?,?,?,?,?,?,?)",
            (derived_id, "source", f"/derived/{visual_id}.jpg", time_ms, index, 1280, 720, f"sha{index}"),
        )
        con.execute(
            "INSERT INTO visual_units VALUES(?,?,?,?,?)",
            (visual_id, "source", derived_id, f"/derived/{visual_id}.jpg", time_ms),
        )
    con.executemany(
        "INSERT INTO visual_identity VALUES(?,?,?,?)",
        [
            ("visual_1", "canonical", "visual_1", 1),
            ("visual_2", "near_duplicate", "visual_1", 0),
            ("visual_3", "unique", "visual_3", 1),
        ],
    )
    con.execute("INSERT INTO visual_duplicate_groups VALUES(?,?)", ("group_1", "visual_1"))
    con.executemany(
        "INSERT INTO visual_labels VALUES(?,?,?,?,?)",
        [
            ("label_1", "visual_1", "person", 0.9, "[1,1,20,20]"),
            ("label_2", "visual_2", "person", 0.8, "[1,1,20,20]"),
        ],
    )
    con.commit()
    return con


def test_v22_loads_canonical_view_and_preserves_reverse_mapping(tmp_path: Path):
    con = create_db(tmp_path / "db.sqlite")
    try:
        context = v22.central_dedup_context(con)
        rows = v22.load_visual_rows(con)
    finally:
        con.close()
    assert context["raw_visual_input_count"] == 3
    assert context["canonical_visual_input_count"] == 2
    assert context["dedup_excluded_visual_count"] == 1
    assert {row["visual_unit_id"] for row in rows} == {"visual_1", "visual_3"}
    first = next(row for row in rows if row["visual_unit_id"] == "visual_1")
    assert first["central_dedup_reverse_member_count"] == 2
    assert set(first["central_dedup_reverse_visual_unit_ids"].split("|")) == {"visual_1", "visual_2"}
    assert first["central_dedup_raw_group_start_ms"] == 0
    assert first["central_dedup_raw_group_end_ms"] == 36000
    assert first["central_dedup_raw_group_candidate_count"] == 3


def test_candidate_manifest_preserves_lineage_and_reverse_mapping():
    frame = {
        "visual_unit_id": "visual_1", "source_content_id": "source", "derived_id": "derived_1",
        "media_type": "video", "visual_unit_type": "video_frame", "time_position_ms": 1000,
        "canonical_visual_unit_id": "visual_1", "central_dedup_identity_status": "canonical",
        "central_dedup_reverse_member_count": 2,
        "central_dedup_reverse_visual_unit_ids": "visual_1|visual_2",
    }
    row = v22.candidate_row(frame, "qwenvl_high_value", "video_coverage_keyframe", 1.0, [], "run", "dry_run")
    assert row["source_content_id"] == "source"
    assert row["visual_unit_id"] == "visual_1"
    assert row["derived_id"] == "derived_1"
    assert row["time_position_ms"] == 1000
    assert row["central_dedup_reverse_member_count"] == 2


def test_labels_for_dedup_excluded_visuals_do_not_become_bbox_failures(tmp_path: Path):
    con = create_db(tmp_path / "db.sqlite")
    try:
        rows = v22.load_visual_rows(con)
        for row in rows:
            row["width"] = row["db_width"]
            row["height"] = row["db_height"]
        by_vu = {row["visual_unit_id"]: row for row in rows}
        result = v22.load_labels(con, by_vu)
    finally:
        con.close()
    assert result["invalid_bbox_count"] == 0
    assert result["central_dedup_excluded_label_row_count"] == 1
    assert result["central_dedup_excluded_labeled_visual_unit_count"] == 1


def test_pre_dedup_comparison_counts_same_window_refill():
    baseline = {
        "summary": {
            "input_visual_units": 3, "coverage_window_total_count": 1,
            "normal_video_group_with_coverage_count": 1, "normal_video_group_count": 1,
        },
        "q_rows": [
            {"visual_unit_id": "visual_2", "high_value_category": "video_coverage_keyframe"}
        ],
        "o_rows": [{"visual_unit_id": "visual_2"}],
        "window_rows": [
            {"source_content_id": "source", "window_index": "0", "selected_visual_unit_id": "visual_2"}
        ],
    }
    context = {
        "dedup_excluded_visual_unit_ids": {"visual_2"},
    }
    result = v22.compare_pre_dedup_v22(
        [{"visual_unit_id": "visual_1", "high_value_category": "video_coverage_keyframe"}],
        [],
        [{"source_content_id": "source", "window_index": 0, "selected_visual_unit_id": "visual_1"}],
        baseline,
        context,
    )
    assert result["coverage_candidate_excluded_by_central_dedup_count"] == 1
    assert result["coverage_refill_after_central_dedup_count"] == 1
    assert result["coverage_refill_failed_after_central_dedup_count"] == 0
    assert result["qwen_candidate_removed_by_central_dedup_count"] == 1
    assert result["qwen_candidate_replaced_after_central_dedup_count"] == 1
    assert result["ocr_candidate_removed_by_central_dedup_count"] == 1
    assert result["central_dedup_excluded_queue_leak_count"] == 0


def test_raw_bounds_drive_tail_and_window_scope():
    frames = [
        {
            "time_position_ms": 18000,
            "central_dedup_raw_group_start_ms": 0,
            "central_dedup_raw_group_end_ms": 60000,
        }
    ]
    expected = 60000 - v22.phase1.tail_window_ms(60000)
    assert v22.frame_tail_start(frames) == expected


def test_contact_sheet_preflight_uses_canonical_video_count(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    con = create_db(db)
    con.close()
    v22_out = tmp_path / "v22"
    reports = v22_out / "reports"
    reports.mkdir(parents=True)
    for name in (
        "stop03_2_candidate_summary.json", "video_frame_decisions.jsonl",
        "video_budget_report.csv",
    ):
        (reports / name).write_text("{}" if name.endswith(".json") else "", encoding="utf-8")
    result = contact.preflight(db, v22_out, tmp_path / "sheet")
    assert result["raw_db_video_visual_unit_count"] == 3
    assert result["db_video_visual_unit_count"] == 2
    assert result["visual_input_source"] == "canonical_visual_units_for_heavy"


def test_v22_canonical_contract_keeps_model_and_html_resource_logic_unchanged():
    candidate_text = Path(v22.__file__).read_text(encoding="utf-8")
    contact_text = Path(contact.__file__).read_text(encoding="utf-8")
    assert "FROM canonical_visual_units_for_heavy vu" in candidate_text
    assert "materialize_assets(decisions, out_dir)" in contact_text
    assert "grid_cols=16" in candidate_text and "grid_rows=5" in candidate_text
    for token in ("mlx_vlm", "ultralytics.YOLO", "paddleocr", "ffmpeg -i"):
        assert token not in candidate_text
