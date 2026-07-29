import csv
import json
from pathlib import Path

from media_archive import app
from media_archive.validation.v025_repro_diff import analyze_v025_repro_diff


def test_analyze_v025_repro_diff_reports_frozen_actual_drift(tmp_path):
    old_a9t = tmp_path / "old_a9t.json"
    old_a9t.write_text(
        json.dumps(
            {
                "image_files_total": 3618,
                "timelapse_sequence_count": 6,
                "timelapse_member_image_count": 2728,
                "timelapse_keyframe_count": 18,
                "normal_image_count": 890,
                "preview_jobs_total": 908,
                "preview_failed_count": 0,
                "saved_preview_count_by_timelapse": 2710,
            }
        ),
        encoding="utf-8",
    )
    old_summary = tmp_path / "r2j.md"
    old_summary.write_text(
        "\n".join(
            [
                "- decode_mode: videotoolbox",
                "- concurrency: 4",
                "- video_files_total: 34",
                "- total_produced_frame_count: 672",
                "- frame_extract_status_counts: {'success': 34}",
            ]
        ),
        encoding="utf-8",
    )
    old_manifest = tmp_path / "old_frames.csv"
    with old_manifest.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["source_video_relative_path"])
        writer.writeheader()
        for video in ["a.mov", "b.mov"]:
            for _ in range(3):
                writer.writerow({"source_video_relative_path": video})
    new_manifest = tmp_path / "video_frame_manifest.jsonl"
    with new_manifest.open("w", encoding="utf-8") as file_obj:
        for video in ["a.mov", "b.mov"]:
            for _ in range(2):
                file_obj.write(json.dumps({"source_video_relative_path": video}) + "\n")
    new_verdict = tmp_path / "verdict.json"
    new_verdict.write_text(
        json.dumps(
            {
                "validation_status": "FAIL",
                "source_safety_status": "PASS",
                "actual_source_image_count": 3618,
                "actual_high_confidence_timelapse_sequence_count": 8,
                "actual_timelapse_total_image_count": 2812,
                "actual_timelapse_keyframe_count": 24,
                "actual_normal_image_count": 806,
                "actual_final_preview_count": 830,
                "actual_failed_count": 0,
                "actual_preview_reduction_count": 2788,
                "image_count_match_final_preview": True,
                "actual_video_files_total": 34,
                "actual_total_produced_frame_count": 638,
                "actual_success_video_count": 34,
                "actual_decode_mode": "videotoolbox",
                "actual_concurrency": 4,
                "video_count_match_frames": True,
            }
        ),
        encoding="utf-8",
    )
    new_full = tmp_path / "full.json"
    new_full.write_text(
        json.dumps(
            {
                "evidence_source_files": [
                    {"path": str(new_manifest)},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.md"

    report = analyze_v025_repro_diff(
        old_a9t,
        old_summary,
        old_manifest,
        new_verdict,
        new_full,
        output,
    )

    assert output.exists()
    assert report["image_diff"]["timelapse_sequence_count"]["actual"] == 8
    assert report["image_diff"]["final_preview_count"]["actual"] == 830
    assert report["image_diff"]["timelapse_total_image_count"]["actual"] == 2812
    assert report["source_safety_status"] == "PASS"
    assert report["video_diff"]["total_produced_frame_count"]["actual"] == 638
    assert report["video_diff"]["all_videos_minus_one_frame"] is True
    assert report["verdict_field_semantic_issue"]["video_count_match_frames"] is True


def test_analyze_v025_repro_diff_cli(tmp_path):
    old_a9t = tmp_path / "old_a9t.json"
    old_a9t.write_text(json.dumps({"image_files_total": 1}), encoding="utf-8")
    old_summary = tmp_path / "r2j.md"
    old_summary.write_text("- video_files_total: 1\n- total_produced_frame_count: 1\n", encoding="utf-8")
    old_manifest = tmp_path / "old.csv"
    old_manifest.write_text("source_video_relative_path\none.mov\n", encoding="utf-8")
    new_manifest = tmp_path / "video_frame_manifest.jsonl"
    new_manifest.write_text(json.dumps({"source_video_relative_path": "one.mov"}) + "\n", encoding="utf-8")
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"source_safety_status": "PASS"}), encoding="utf-8")
    full = tmp_path / "full.json"
    full.write_text(json.dumps({"evidence_source_files": [{"path": str(new_manifest)}]}), encoding="utf-8")
    output = tmp_path / "out.md"

    assert app.main(
        [
            "analyze-v025-repro-diff",
            "--old-a9t-summary",
            str(old_a9t),
            "--old-r2j-summary-md",
            str(old_summary),
            "--old-r2j-frame-manifest",
            str(old_manifest),
            "--new-verdict",
            str(verdict),
            "--new-full-report",
            str(full),
            "--output",
            str(output),
        ]
    ) == 0
    assert "Do not enter V0.3" in output.read_text(encoding="utf-8")
