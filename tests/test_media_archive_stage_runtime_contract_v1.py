from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.stage_runtime_contract import (  # noqa: E402
    live_eta,
    parse_progress_line,
    write_stage_reports,
)


class StageRuntimeContractTests(unittest.TestCase):
    def test_progress_parser_accepts_structured_observability_fields(self) -> None:
        parsed = parse_progress_line(json.dumps({
            "contract": "media_archive_stage_runtime_contract_v1",
            "event": "stage_progress",
            "completed": 12,
            "total": 20,
            "success": 11,
            "skipped": 1,
            "failed": 0,
            "current_item": "clip.mov",
            "input_files": 20,
            "input_sources": 20,
            "input_bytes": 4096,
            "output_records": 88,
            "output_files": 12,
            "output_bytes": 2048,
            "actual_workers": 2,
            "ffmpeg_processes": 2,
            "items_per_second": 1.5,
        }))
        self.assertEqual(parsed["completed"], 12)
        self.assertEqual(parsed["input_bytes"], 4096)
        self.assertEqual(parsed["output_records"], 88)
        self.assertEqual(parsed["actual_workers"], 2)

    def test_progress_parser_handles_existing_text_contracts(self) -> None:
        parsed = parse_progress_line(
            "[progress] 50/100 success=48 failed=2 file=clip.mov"
        )
        self.assertEqual(parsed["completed"], 50)
        self.assertEqual(parsed["success"], 48)
        self.assertEqual(parsed["current_item"], "clip.mov")
        scan = parse_progress_line("[hash 990/2683] folder/clip.mov")
        self.assertEqual(scan["completed"], 990)
        self.assertEqual(scan["current_item"], "folder/clip.mov")
        self.assertEqual(parse_progress_line("db_path: /index/library.sqlite"), {})

    def test_eta_requires_a_real_sample(self) -> None:
        self.assertIsNone(live_eta(19, 100, 60)[0])
        self.assertIsNone(live_eta(30, 100, 20)[0])
        self.assertEqual(live_eta(30, 60, 60)[0], 60.0)

    def test_all_stage_01_to_10_keys_get_five_atomic_reports(self) -> None:
        stage_keys = (
            "scan", "image_preview", "video_frames", "visual_schema_v3",
            "yoloe", "openclip", "dedup", "person_reid_optional_v1",
            "candidate_schema", "candidates_generic_v2",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            db.write_bytes(b"sqlite")
            for index, key in enumerate(stage_keys, 1):
                output = root / "stages" / f"{index:02d}_{key}"
                stage = {
                    "key": key,
                    "name": key,
                    "command": [
                        "python", "runner.py", "--script", f"{key}.py",
                        "--out", str(output),
                    ],
                }
                paths = write_stage_reports(
                    stage=stage,
                    workspace=root,
                    database=db,
                    record={
                        "status": "success", "live_completed": 2,
                        "live_total": 2, "elapsed_seconds": 1.0,
                        "configured_workers": 3, "actual_workers": 2,
                        "input_files": 2, "input_sources": 2,
                        "input_bytes": 4096, "output_records": 2,
                    },
                    before_storage={
                        "stage": {"file_count": 0, "bytes": 0},
                        "database": {
                            "sqlite": 0, "wal": 0, "shm": 0, "total": 0,
                        },
                    },
                )
                self.assertEqual(set(paths), {
                    "summary", "failures", "skipped", "storage_delta",
                    "runtime_metrics",
                })
                self.assertTrue(all(Path(path).is_file() for path in paths.values()))
                self.assertFalse(list((output / "reports").glob("*.tmp")))
                summary = json.loads(Path(paths["summary"]).read_text())
                self.assertEqual(summary["success"], 2)
                self.assertEqual(summary["actual_script"], f"{key}.py")
                self.assertEqual(summary["input_file_count"], 2)
                storage = json.loads(Path(paths["storage_delta"]).read_text())
                self.assertEqual(storage["database"]["delta_sqlite_bytes"], 6)
                with Path(paths["failures"]).open(newline="") as handle:
                    self.assertEqual(next(csv.reader(handle)), ["item", "reason"])

    def test_native_ui_exposes_completed_report_and_storage_attribution(self) -> None:
        frontend = (
            ROOT / "apps/media_archive_image_video_ui/native_frontend.swift"
        ).read_text(encoding="utf-8")
        bridge = (
            ROOT / "apps/media_archive_image_video_ui/native_bridge.py"
        ).read_text(encoding="utf-8")
        self.assertIn("打开阶段完成报告", frontend)
        self.assertIn("中心数据库增长", frontend)
        self.assertIn('"stage_output_bytes"', bridge)
        self.assertIn('"database_delta_bytes"', bridge)


if __name__ == "__main__":
    unittest.main()
