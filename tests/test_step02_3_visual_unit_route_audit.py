import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02_step01_step02_pipeline" / "step02_3_visual_unit_route_audit.py"


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


def _row(vid: str, source: str, t: int, unit_type: str = "video_frame") -> dict:
    return {
        "visual_unit_id": vid,
        "visual_unit_type": unit_type,
        "parent_source_file_id": "sf_" + source,
        "parent_source_content_id": "sc_" + source,
        "parent_source_path_at_processing_time": f"/source/{source}.mov" if unit_type == "video_frame" else f"/source/{source}.jpg",
        "source_relative_path": f"{source}.mov" if unit_type == "video_frame" else f"{source}.jpg",
        "source_video_id": source if unit_type == "video_frame" else "",
        "source_video_relative_path": f"{source}.mov" if unit_type == "video_frame" else "",
        "time_position_ms": str(t) if unit_type == "video_frame" else "",
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
        for row in rows:
            writer.writerow(row)


def _make_selector_fixture(root: Path) -> Path:
    selector = root / "selector"
    rows = [
        _row("vu1", "video_a", 0),
        _row("vu2", "video_a", 6000),
        _row("vu3", "video_a", 12000),
        _row("vu4", "video_a", 24000),
        _row("vu_short", "video_short", 0),
        _row("vu_img", "image_a", 0, "image_preview"),
    ]
    by_id = {r["visual_unit_id"]: r for r in rows}
    by_id["vu1"]["selected_for_yoloe"] = "True"
    by_id["vu1"]["selected_for_high_value"] = "True"
    by_id["vu1"]["selected_for_ocr_trigger"] = "True"
    by_id["vu4"]["selected_for_yoloe"] = "True"
    by_id["vu_short"]["selected_for_yoloe"] = "True"
    by_id["vu_img"]["selected_for_yoloe"] = "True"
    by_id["vu2"]["selected_for_high_value"] = "True"
    by_id["vu3"]["selected_for_ocr_trigger"] = "True"
    _write_csv(selector / "manifests" / "visual_unit_route_decision_manifest.csv", rows)

    yoloe_rows = [
        {"route_name": "yoloe", "route_reason": "first", **by_id["vu1"]},
        {"route_name": "yoloe", "route_reason": "coverage", **by_id["vu4"]},
        {"route_name": "yoloe", "route_reason": "single", **by_id["vu_short"]},
        {"route_name": "yoloe", "route_reason": "", **by_id["vu_img"]},
    ]
    high_rows = [
        {"route_name": "high_value", "route_reason": "hv", **by_id["vu1"]},
        {"route_name": "high_value", "route_reason": "hv_not_subset", **by_id["vu2"]},
    ]
    ocr_rows = [
        {"route_name": "ocr_trigger", "route_reason": "ocr_overlap", **by_id["vu1"]},
        {"route_name": "ocr_trigger", "route_reason": "ocr_not_subset", **by_id["vu3"]},
    ]
    _write_csv(selector / "queues" / "yoloe_visual_units.csv", yoloe_rows)
    _write_csv(selector / "queues" / "high_value_visual_units.csv", high_rows)
    _write_csv(selector / "queues" / "ocr_trigger_visual_units.csv", ocr_rows)
    return selector


def _tree_hash(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class Step023VisualUnitRouteAuditTest(unittest.TestCase):
    def test_audit_detects_contract_risks_and_writes_reports_without_modifying_selector_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            selector = _make_selector_fixture(tmp_path)
            before_hash = _tree_hash(selector)
            out = tmp_path / "audit"
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = "/tmp/step02_3_audit_pycache"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--selector-out",
                    str(selector),
                    "--out",
                    str(out),
                    "--yoloe-max-gap-ms",
                    "10000",
                    "--high-value-per-video-soft-max",
                    "5",
                    "--ocr-per-video-soft-max",
                    "8",
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
            self.assertEqual(before_hash, _tree_hash(selector))

            summary = json.loads((out / "reports" / "step02_3_route_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_visual_unit_count"], 6)
            self.assertEqual(summary["high_value_not_in_yoloe_count"], 1)
            self.assertEqual(summary["ocr_not_in_yoloe_count"], 1)
            self.assertEqual(summary["high_value_ocr_overlap_count"], 1)
            self.assertEqual(summary["route_reason_empty_count"], 1)
            self.assertEqual(summary["yoloe_gap_max_ms"], 24000)
            self.assertGreater(summary["error_risk_count"], 0)

            with (out / "manifests" / "yoloe_gap_audit.csv").open(encoding="utf-8", newline="") as f:
                gap_rows = list(csv.DictReader(f))
            by_source = {r["source_key"]: r for r in gap_rows}
            self.assertEqual(by_source["video_a"]["max_yoloe_gap_ms"], "24000")
            self.assertEqual(by_source["video_short"]["gap_status"], "single_frame_or_short_video")

            for path in [
                out / "reports" / "risk_items.csv",
                out / "reports" / "risk_items.jsonl",
                out / "manifests" / "per_video_route_audit.csv",
                out / "manifests" / "route_contract_violations.csv",
                out / "final_report" / "step02_3_route_audit_final_report_latest.json",
                out / "final_report" / "step02_3_route_audit_final_report_latest.md",
            ]:
                self.assertTrue(path.exists(), str(path))


if __name__ == "__main__":
    unittest.main()
