import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02_step01_step02_pipeline" / "step02_3_high_value_distribution_audit.py"


FIELDS = [
    "route_name",
    "route_reason",
    "visual_unit_id",
    "visual_unit_type",
    "parent_source_file_id",
    "parent_source_content_id",
    "parent_source_path_at_processing_time",
    "source_relative_path",
    "source_video_id",
    "source_video_relative_path",
    "time_position_ms",
    "selected_for_yoloe",
    "selected_for_high_value",
    "selected_for_ocr_trigger",
]


def _unit(vid: str, video: str, t: int) -> dict:
    return {
        "visual_unit_id": vid,
        "visual_unit_type": "video_frame",
        "parent_source_file_id": f"sf_{video}",
        "parent_source_content_id": f"sc_{video}",
        "parent_source_path_at_processing_time": f"/source/{video}.mov",
        "source_relative_path": f"{video}.mov",
        "source_video_id": video,
        "source_video_relative_path": f"{video}.mov",
        "time_position_ms": str(t),
        "selected_for_yoloe": "False",
        "selected_for_high_value": "False",
        "selected_for_ocr_trigger": "False",
    }


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


def _make_fixture(root: Path) -> Path:
    selector = root / "selector"
    rows = [
        _unit("a1", "video_a", 0),
        _unit("a2", "video_a", 6000),
        _unit("b1", "video_b", 0),
        _unit("c1", "video_c", 0),
    ]
    by_id = {r["visual_unit_id"]: r for r in rows}
    for vid in ["a1", "a2", "b1", "c1"]:
        by_id[vid]["selected_for_yoloe"] = "True"
    by_id["a1"]["selected_for_high_value"] = "True"
    by_id["a2"]["selected_for_high_value"] = "True"
    by_id["c1"]["selected_for_ocr_trigger"] = "True"
    _write_csv(selector / "manifests" / "visual_unit_route_decision_manifest.csv", rows)

    yoloe = [
        {"route_name": "yoloe", "route_reason": "first", **by_id["a1"]},
        {"route_name": "yoloe", "route_reason": "coverage", **by_id["a2"]},
        {"route_name": "yoloe", "route_reason": "first", **by_id["b1"]},
        {"route_name": "yoloe", "route_reason": "first", **by_id["c1"]},
    ]
    high = [
        {"route_name": "high_value", "route_reason": "hv", **by_id["a1"]},
        {"route_name": "high_value", "route_reason": "", **by_id["a2"]},
        {"route_name": "high_value", "route_reason": "not_subset", **{**_unit("missing_hv", "video_x", 0), "parent_source_file_id": ""}},
    ]
    ocr = [
        {"route_name": "ocr_trigger", "route_reason": "ocr", **by_id["c1"]},
        {"route_name": "ocr_trigger", "route_reason": "overlap", **by_id["a1"]},
    ]
    _write_csv(selector / "queues" / "yoloe_visual_units.csv", yoloe)
    _write_csv(selector / "queues" / "high_value_visual_units.csv", high)
    _write_csv(selector / "queues" / "ocr_trigger_visual_units.csv", ocr)
    return selector


class Step023HighValueDistributionAuditTest(unittest.TestCase):
    def test_high_value_distribution_audit_detects_expected_risks_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            selector = _make_fixture(tmp_path)
            out = tmp_path / "audit"
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = "/tmp/step02_3_hv_audit_pycache"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--selector-out",
                    str(selector),
                    "--out",
                    str(out),
                    "--high-value-per-video-soft-max",
                    "5",
                    "--top5-share-warning",
                    "0.35",
                    "--top10-share-warning",
                    "0.50",
                    "--no-open",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads((out / "reports" / "high_value_distribution_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["videos_with_frames"], 3)
            self.assertEqual(summary["videos_with_high_value"], 1)
            self.assertEqual(summary["videos_missing_high_value_count"], 2)
            self.assertEqual(summary["high_value_not_in_yoloe_count"], 1)
            self.assertEqual(summary["high_value_ocr_overlap_count"], 1)
            self.assertEqual(summary["route_reason_empty_count"], 1)
            self.assertEqual(summary["lineage_missing_count"], 1)
            self.assertIn("high_value_per_video_p95", summary)

            with (out / "manifests" / "high_value_distribution_buckets.csv").open(encoding="utf-8", newline="") as f:
                buckets = {r["bucket"]: r for r in csv.DictReader(f)}
            self.assertEqual(buckets["0"]["video_count"], "2")
            self.assertEqual(buckets["2"]["video_count"], "1")

            for path in [
                out / "reports" / "risk_items.csv",
                out / "manifests" / "high_value_by_video.csv",
                out / "manifests" / "videos_missing_high_value.csv",
                out / "manifests" / "high_value_items_with_source.csv",
                out / "manifests" / "video_anchor_coverage.csv",
                out / "tests" / "optional_debug_sample.json",
            ]:
                self.assertTrue(path.exists(), str(path))


if __name__ == "__main__":
    unittest.main()
