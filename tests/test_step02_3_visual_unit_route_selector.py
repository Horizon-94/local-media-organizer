import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02_step01_step02_pipeline" / "step02_3_visual_unit_route_selector.py"


def _write_video_manifest(path: Path, *, missing_lineage: bool = False) -> None:
    fields = [
        "source_video_id",
        "source_video_relative_path",
        "frame_id",
        "frame_file_sha256",
        "frame_file",
        "frame_index",
        "estimated_frame_time_ms",
        "parent_source_file_id",
        "parent_source_content_id",
        "parent_source_path_at_processing_time",
        "parent_media_kind",
        "step01_source_relative_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i in range(8):
            writer.writerow({
                "source_video_id": "vid_screen_001",
                "source_video_relative_path": "clips/RPReplay_screenrecord.mov",
                "frame_id": f"frame_{i:03d}",
                "frame_file_sha256": f"sha{i}",
                "frame_file": f"/tmp/frm_{i:03d}.jpg",
                "frame_index": str(i + 1),
                "estimated_frame_time_ms": str(i * 6000),
                "parent_source_file_id": "" if missing_lineage else "sf_video",
                "parent_source_content_id": "sc_video",
                "parent_source_path_at_processing_time": "/source/RPReplay_screenrecord.mov",
                "parent_media_kind": "video",
                "step01_source_relative_path": "clips/RPReplay_screenrecord.mov",
            })


def _write_image_manifest(path: Path) -> None:
    rows = [
        {
            "visual_unit_id": "vu_image_keep",
            "visual_unit_type": "image_preview",
            "visual_file": "/tmp/image_keep.jpg",
            "visual_file_sha256": "image_sha",
            "parent_source_file_id": "sf_img",
            "parent_source_content_id": "sc_img",
            "parent_source_path_at_processing_time": "/source/images/a.jpg",
            "parent_media_kind": "image",
            "source_relative_path": "images/a.jpg",
            "preview_role": "normal_image",
            "producer_step": "step02_2_image_preview",
            "extra_only_on_one_row": "field_union_probe",
        },
        {
            "visual_unit_type": "image_preview",
            "visual_file": "/tmp/screenshot.jpg",
            "visual_file_sha256": "screen_sha",
            "parent_source_file_id": "sf_screen",
            "parent_source_content_id": "sc_screen",
            "parent_source_path_at_processing_time": "/source/images/screenshot.jpg",
            "parent_media_kind": "image",
            "source_relative_path": "images/screenshot.jpg",
            "preview_artifact_id": "ip_screen",
            "preview_role": "timelapse_keyframe",
            "producer_step": "step02_2_image_preview",
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run_selector(tmp_path: Path, *, missing_lineage: bool = False) -> subprocess.CompletedProcess:
    video = tmp_path / "video.csv"
    image = tmp_path / "image.jsonl"
    out = tmp_path / "out"
    _write_video_manifest(video, missing_lineage=missing_lineage)
    _write_image_manifest(image)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = "/tmp/step02_3_selector_pycache"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--video-frame-manifest",
            str(video),
            "--image-visual-unit-manifest",
            str(image),
            "--out",
            str(out),
            "--policy",
            "step02_3_route_policy_v1",
            "--yoloe-coverage-ms",
            "6000",
            "--yoloe-max-gap-ms",
            "10000",
            "--high-value-min-gap-ms",
            "20000",
            "--run-phase",
            "unit",
            "--no-open",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class Step023VisualUnitRouteSelectorTest(unittest.TestCase):
    def test_selector_contracts_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = _run_selector(tmp_path)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            out = tmp_path / "out"
            summary = json.loads((out / "reports" / "step02_3_visual_unit_route_selector_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["video_visual_unit_count"], 8)
            self.assertEqual(summary["image_visual_unit_count"], 2)
            self.assertGreater(summary["yoloe_queue_count"], 0)
            self.assertGreaterEqual(summary["videos_with_yoloe"], 1)
            self.assertTrue(summary["high_value_subset_of_yoloe"])
            self.assertTrue(summary["ocr_subset_of_yoloe"])
            self.assertEqual(summary["high_value_ocr_overlap_count"], 0)
            self.assertEqual(summary["blocked_items_count"], 0)
            self.assertEqual(summary["failure_items_count"], 0)

            with (out / "manifests" / "visual_unit_route_decision_manifest.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            by_id = {r["visual_unit_id"]: r for r in rows}
            self.assertIn("vu_video_frame_frame_000", by_id)
            self.assertIn("vu_image_keep", by_id)
            derived = [r for r in rows if r["visual_unit_type"] == "image_preview" and r["visual_unit_id"].startswith("vu_image_preview_")]
            self.assertEqual(len(derived), 1)

            video_yoloe = [r for r in rows if r["visual_unit_type"] == "video_frame" and r["selected_for_yoloe"] == "True"]
            self.assertGreaterEqual(len(video_yoloe), 4)
            ocr_rows = [r for r in rows if r["selected_for_ocr_trigger"] == "True"]
            self.assertTrue(ocr_rows)
            for r in ocr_rows:
                self.assertEqual(r["selected_for_high_value"], "False")
                if r["route_reason_high_value"] == "":
                    self.assertIn(r["route_conflict_resolution"], ("", "ocr_over_high_value"))

            with (out / "queues" / "yoloe_visual_units.csv").open(encoding="utf-8", newline="") as f:
                header = next(csv.reader(f))
            self.assertIn("extra_only_on_one_row", header)
            self.assertTrue((out / "final_report" / "step02_3_visual_unit_route_selector_final_report_latest.json").exists())
            history = list((out / "final_report" / "history").glob("*_step02_3_visual_unit_route_selector_unit_final_report.json"))
            self.assertEqual(len(history), 1)

    def test_missing_lineage_blocks_without_fake_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = _run_selector(tmp_path, missing_lineage=True)
            self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
            out = tmp_path / "out"
            summary = json.loads((out / "reports" / "step02_3_visual_unit_route_selector_summary.json").read_text(encoding="utf-8"))
            self.assertGreater(summary["blocked_items_count"], 0)
            blocked = (out / "manifests" / "blocked_items.jsonl").read_text(encoding="utf-8")
            self.assertIn("missing_required_lineage_field", blocked)
            self.assertIn("parent_source_file_id", blocked)


if __name__ == "__main__":
    unittest.main()
