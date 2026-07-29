import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02_step01_step02_pipeline" / "step02_3_rule_search_evaluator.py"


def _make_frame(path: Path, color: tuple[int, int, int], text: str) -> None:
    im = Image.new("RGB", (160, 90), color)
    draw = ImageDraw.Draw(im)
    draw.rectangle((8, 8, 130, 30), outline=(255, 255, 255), width=2)
    draw.text((12, 12), text, fill=(255, 255, 255))
    im.save(path, quality=90)


class Step023RuleSearchEvaluatorTest(unittest.TestCase):
    def test_cli_outputs_summary_and_route_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frame_dir = tmp_path / "frames" / "video_a"
            frame_dir.mkdir(parents=True)
            names = [
                "frm_a_idx000001_t000002000ms.jpg",
                "frm_b_idx000002_t000005000ms.jpg",
                "frm_c_idx000003_t000008000ms.jpg",
                "frm_d_idx000004_t000011000ms.jpg",
            ]
            for i, name in enumerate(names):
                _make_frame(frame_dir / name, (30 + i * 35, 45 + i * 20, 80 + i * 15), f"F{i}")

            labels = tmp_path / "labels.csv"
            with labels.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "video_id",
                        "video_path",
                        "file_name",
                        "time_ms",
                        "label",
                        "also_yoloe",
                        "also_high_value",
                        "also_ocr_trigger",
                        "stage",
                        "color_dot",
                        "annotation_strength",
                        "exhaustiveness",
                        "reason_zh",
                        "source_csv",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "video_id": "unit_video",
                    "video_path": str(frame_dir),
                    "file_name": names[1],
                    "time_ms": "5000",
                    "label": "accept_high_value",
                    "also_yoloe": "true",
                    "also_high_value": "true",
                    "also_ocr_trigger": "false",
                    "stage": "unit",
                    "color_dot": "red",
                    "annotation_strength": "positive_or_negative_user_mark",
                    "exhaustiveness": "sparse_positive_only",
                    "reason_zh": "unit",
                    "source_csv": "unit.csv",
                })

            out = tmp_path / "eval_out"
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--labels",
                    str(labels),
                    "--out",
                    str(out),
                    "--policies",
                    "cluster_rep_fix2_like,ocr_text_screen_policy",
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
            summary = json.loads((out / "summary" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["videos_in_csv"], 1)
            self.assertEqual(summary["evaluated_videos"], 1)
            self.assertTrue(summary["blocked_items_empty"])
            self.assertTrue(summary["failure_items_empty"])
            self.assertTrue((out / "manifests" / "frame_metrics.csv").exists())
            self.assertTrue((out / "summary" / "policy_scores.csv").exists())

            for policy in ["cluster_rep_fix2_like", "ocr_text_screen_policy"]:
                manifest = out / "policies" / policy / "unit_video" / "manifests" / "frame_candidate_manifest.csv"
                with manifest.open(encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertTrue(rows)
                for row in rows:
                    is_yoloe = row["route_yoloe"] == "True"
                    is_high = row["route_high_value"] == "True"
                    is_ocr = row["route_ocr_trigger"] == "True"
                    self.assertFalse(is_high and is_ocr)
                    if is_high or is_ocr:
                        self.assertTrue(is_yoloe)


if __name__ == "__main__":
    unittest.main()
