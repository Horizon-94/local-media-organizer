import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02_step01_step02_pipeline" / "step02_3_route_marked_preview_audit.py"


FIELDS = [
    "route_name",
    "route_reason",
    "visual_unit_id",
    "visual_unit_type",
    "visual_file",
    "parent_source_file_id",
    "parent_source_content_id",
    "parent_source_path_at_processing_time",
    "source_relative_path",
    "source_video_id",
    "source_video_relative_path",
    "time_position_ms",
    "frame_index",
    "selected_for_yoloe",
    "selected_for_high_value",
    "selected_for_ocr_trigger",
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(FIELDS)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _unit(tmp: Path, vid: str, video: str, time_ms: int, frame_index: int) -> dict:
    img = tmp / f"{vid}.jpg"
    img.write_bytes(b"fake-jpg-placeholder")
    return {
        "visual_unit_id": vid,
        "visual_unit_type": "video_frame",
        "visual_file": str(img),
        "parent_source_file_id": f"sf_{video}",
        "parent_source_content_id": f"sc_{video}",
        "parent_source_path_at_processing_time": f"/source/{video}.mov",
        "source_relative_path": f"{video}.mov",
        "source_video_id": video,
        "source_video_relative_path": f"{video}.mov",
        "time_position_ms": str(time_ms),
        "frame_index": str(frame_index),
        "selected_for_yoloe": "False",
        "selected_for_high_value": "False",
        "selected_for_ocr_trigger": "False",
    }


def _fixture(root: Path) -> Path:
    selector = root / "selector"
    rows = [
        _unit(root, "late_name_a", "video_a", 2000, 2),
        _unit(root, "early_name_z", "video_a", 1000, 1),
        _unit(root, "b1", "video_b", 0, 1),
        _unit(root, "b2_unrouted", "video_b", 1000, 2),
    ]
    by_id = {r["visual_unit_id"]: r for r in rows}
    by_id["late_name_a"]["selected_for_yoloe"] = "True"
    by_id["early_name_z"]["selected_for_yoloe"] = "True"
    by_id["early_name_z"]["selected_for_high_value"] = "True"
    by_id["early_name_z"]["selected_for_ocr_trigger"] = "True"
    by_id["b1"]["selected_for_yoloe"] = "True"
    _write_csv(selector / "manifests" / "visual_unit_route_decision_manifest.csv", rows)
    _write_csv(selector / "queues" / "yoloe_visual_units.csv", [
        {"route_name": "yoloe", "route_reason": "coverage", **by_id["late_name_a"]},
        {"route_name": "yoloe", "route_reason": "first", **by_id["early_name_z"]},
        {"route_name": "yoloe", "route_reason": "first", **by_id["b1"]},
    ])
    _write_csv(selector / "queues" / "high_value_visual_units.csv", [
        {"route_name": "high_value", "route_reason": "", **by_id["early_name_z"]},
    ])
    _write_csv(selector / "queues" / "ocr_trigger_visual_units.csv", [
        {"route_name": "ocr_trigger", "route_reason": "ocr", **by_id["early_name_z"]},
    ])
    return selector


def _run(selector: Path, out: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = "/tmp/step02_3_marked_preview_pycache"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--selector-out",
            str(selector),
            "--out",
            str(out),
            "--no-open",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class Step023RouteMarkedPreviewAuditTest(unittest.TestCase):
    def test_marked_preview_outputs_badges_risks_and_time_sorted_video_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            selector = _fixture(tmp_path)
            out = tmp_path / "out"
            result = _run(selector, out)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads((out / "reports" / "route_marked_preview_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_visual_unit_count"], 4)
            self.assertEqual(summary["high_value_ocr_overlap_count"], 1)
            self.assertEqual(summary["route_reason_empty_count"], 1)
            self.assertGreaterEqual(summary["warning_risk_count"], 1)
            self.assertTrue((out / "contact_sheets" / "index.html").exists())
            self.assertTrue((out / "final_report" / "step02_3_route_marked_preview_audit_latest.json").exists())

            html_files = list((out / "contact_sheets" / "by_video").glob("video_a*.html"))
            self.assertEqual(len(html_files), 1)
            html = html_files[0].read_text(encoding="utf-8")
            self.assertIn("YOLOE", html)
            self.assertIn("HIGH_VALUE", html)
            self.assertIn("OCR_TRIGGER", html)
            self.assertIn("UNROUTED", (out / "contact_sheets" / "by_video" / "video_b.html").read_text(encoding="utf-8"))
            self.assertIn("00:00:01.000", html)
            self.assertIn("00:00:02.000", html)
            self.assertLess(html.index("time_position_ms</b>: 1000"), html.index("time_position_ms</b>: 2000"))

            risks = (out / "reports" / "risk_items.csv").read_text(encoding="utf-8")
            self.assertIn("video_yoloe_no_high_value", risks)
            self.assertIn("route_reason_empty", risks)
            self.assertIn("high_value_ocr_overlap", risks)

    def test_missing_required_selector_output_blocks_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            selector = tmp_path / "missing_selector"
            out = tmp_path / "out"
            result = _run(selector, out)
            self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
            summary = json.loads((out / "reports" / "route_marked_preview_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "BLOCKED")
            risks = (out / "reports" / "risk_items.csv").read_text(encoding="utf-8")
            self.assertIn("selector_output_missing_or_unreadable", risks)


if __name__ == "__main__":
    unittest.main()
