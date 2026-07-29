from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts/04_media_archive_app/benchmark_stop03_1c_person_reid_concurrency_v1.py"
)
SPEC = importlib.util.spec_from_file_location("person_reid_benchmark_v1", SCRIPT)
assert SPEC and SPEC.loader
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


class PersonReidConcurrencyBenchmarkTests(unittest.TestCase):
    def test_select_sample_prioritizes_person_labeled_visuals_without_fixed_count(self) -> None:
        rows = [{"visual_unit_id": value} for value in ("a", "b", "c", "d")]
        selected = BENCHMARK.select_sample(rows, 3, {"c", "a"})
        self.assertEqual(
            [row["visual_unit_id"] for row in selected],
            ["a", "c", "b"],
        )

    def test_parse_workers_is_positive_unique_and_ordered(self) -> None:
        self.assertEqual(BENCHMARK.parse_workers("1,2,2,4,8"), [1, 2, 4, 8])

    def test_percentile_uses_observed_item_timings(self) -> None:
        self.assertEqual(BENCHMARK.percentile([1.0, 2.0, 3.0, 4.0], 0.5), 3.0)
        self.assertEqual(BENCHMARK.percentile([1.0, 2.0, 3.0, 4.0], 0.95), 4.0)
        self.assertIsNone(BENCHMARK.percentile([], 0.95))

    def test_item_timing_summary_reports_average_tail_and_worker_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "timings.sqlite"
            con = sqlite3.connect(db)
            con.execute(
                """
                CREATE TABLE stop03_1c_person_reid_run_items(
                    visual_unit_id TEXT,
                    claimed_by_worker TEXT,
                    elapsed_seconds REAL,
                    status TEXT
                )
                """
            )
            con.executemany(
                "INSERT INTO stop03_1c_person_reid_run_items VALUES(?,?,?,?)",
                [
                    ("a", "worker-1", 1.0, "success"),
                    ("b", "worker-1", 3.0, "no_face"),
                    ("c", "worker-2", 6.0, "success"),
                    ("d", "worker-2", 99.0, "failed"),
                ],
            )
            con.commit()
            con.close()
            summary = BENCHMARK.item_timing_summary(db)
        self.assertEqual(summary["completed_item_timing_count"], 3)
        self.assertAlmostEqual(summary["average_item_seconds"], 10.0 / 3.0)
        self.assertEqual(summary["p95_item_seconds"], 6.0)
        self.assertEqual(summary["max_item_seconds"], 6.0)
        self.assertEqual(
            summary["per_worker_average_item_seconds"],
            {"worker-1": 2.0, "worker-2": 6.0},
        )


if __name__ == "__main__":
    unittest.main()
