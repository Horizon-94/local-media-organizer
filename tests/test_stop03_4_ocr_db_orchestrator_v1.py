from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
MONITOR_DIR = ROOT / "scripts/stop03_monitor"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(MONITOR_DIR))

import stop03_4_ocr_db_orchestrator_v1 as ocr  # noqa: E402
import stop03_4_ocr_db_node_v1 as frozen_node  # noqa: E402
import stop03_4_ocr_db_monitor as monitor  # noqa: E402


class FakeInference:
    def __init__(self, *, fail_first: bool = False, no_text_for: str = "") -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: dict[str, int] = {}
        self.fail_first = fail_first
        self.no_text_for = no_text_for

    def __call__(self, item: dict, _out: str) -> dict:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls[item["candidate_id"]] = self.calls.get(item["candidate_id"], 0) + 1
            call = self.calls[item["candidate_id"]]
        try:
            time.sleep(0.01)
            if self.fail_first and item["candidate_id"].endswith("000") and call == 1:
                return {
                    "status": "failed",
                    "error_code": "fake_failure",
                    "error_message": "fail once",
                    "elapsed_seconds": 0.01,
                    "worker_pid": threading.get_ident(),
                    "started_at": ocr.utc_now(),
                    "finished_at": ocr.utc_now(),
                    "output_json_path": "",
                    "output_json_sha256": "",
                }
            text = "" if item["candidate_id"] == self.no_text_for else "测试文字"
            lines = [] if not text else [{"text": text, "confidence": 0.9, "box": []}]
            return {
                "status": "no_text" if not text else "success",
                "error_code": "",
                "error_message": "",
                "elapsed_seconds": 0.01,
                "worker_pid": threading.get_ident(),
                "started_at": ocr.utc_now(),
                "finished_at": ocr.utc_now(),
                "output_json_path": "/tmp/fake.json",
                "output_json_sha256": "f" * 64,
                "ocr_text": text,
                "ocr_lines": lines,
                "ocr_line_count": len(lines),
                "mean_confidence": 0.9 if lines else None,
                "min_confidence": 0.9 if lines else None,
                "max_confidence": 0.9 if lines else None,
                "ocr_api_used": "fake",
            }
        finally:
            with self.lock:
                self.active -= 1


class Stop034OcrDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "central.sqlite"
        self.out = self.root / "out"
        self.out.mkdir()
        self.model_root = self.root / "model"
        self.det = self.model_root / "det"
        self.rec = self.model_root / "rec"
        self.det.mkdir(parents=True)
        self.rec.mkdir(parents=True)
        (self.det / "inference.pdiparams").write_bytes(b"det")
        (self.rec / "inference.pdiparams").write_bytes(b"rec")
        self.image_dir = self.root / "derived"
        self.image_dir.mkdir()
        self._create_candidate_schema(12)
        ocr.apply_migration(
            self.db, ROOT / "migrations/20260716_stop03_4_ocr_db_v1.sql"
        )
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "contract_version": ocr.CONTRACT_VERSION,
                    "queue_view": "v_stop03_2_v25_ocr_execution_queue",
                    "ocr_python": sys.executable,
                    "model_root": str(self.model_root),
                    "text_detection_model_dir": str(self.det),
                    "text_recognition_model_dir": str(self.rec),
                    "paddlex_cache_root": str(self.root / "cache"),
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                    "text_rec_score_thresh": 0.0,
                    "workers": 3,
                    "max_attempts": 3,
                    "network_policy": "blocked_in_worker",
                    "source_policy": "derived_visual_only",
                    "result_contract": ocr.RESULT_CONTRACT,
                }
            ),
            encoding="utf-8",
        )
        self.pre = ocr.build_preflight(
            self.db,
            self.config,
            ROOT
            / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_candidate_schema(self, count: int) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE stop03_2_candidate_queue_items(
                    candidate_id TEXT PRIMARY KEY
                );
                CREATE TABLE stop03_2_candidate_queue_frozen_v25(
                    candidate_id TEXT PRIMARY KEY,
                    source_content_id TEXT NOT NULL,
                    visual_unit_id TEXT NOT NULL,
                    canonical_visual_unit_id TEXT NOT NULL,
                    derived_id TEXT NOT NULL,
                    candidate_role TEXT NOT NULL,
                    reason_codes TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    time_position_ms INTEGER NOT NULL,
                    runtime_visual_file TEXT NOT NULL,
                    runtime_visual_file_sha256 TEXT NOT NULL
                );
                CREATE VIEW v_stop03_2_v25_ocr_execution_queue AS
                SELECT * FROM stop03_2_candidate_queue_frozen_v25;
                """
            )
            for index in range(count):
                candidate_id = f"cand_{index:03d}"
                image = self.image_dir / f"{candidate_id}.jpg"
                image.write_bytes(f"image-{index}".encode())
                con.execute(
                    "INSERT INTO stop03_2_candidate_queue_items VALUES(?)",
                    (candidate_id,),
                )
                con.execute(
                    """INSERT INTO stop03_2_candidate_queue_frozen_v25
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id,
                        f"source_{index}",
                        f"visual_{index}",
                        f"visual_{index}",
                        f"derived_{index}",
                        "ocr_test",
                        "test",
                        "v25",
                        "video",
                        index * 1000,
                        str(image),
                        ocr.sha256_file(image),
                    ),
                )
            con.commit()
        finally:
            con.close()

    def prepared(self, count: int) -> list[dict]:
        return ocr.prepare_execution_rows(
            ocr.load_frozen_queue(
                self.db, "v_stop03_2_v25_ocr_execution_queue", count
            ),
            self.pre,
        )

    def execute(self, run_id: str, fake: FakeInference, workers: int = 3) -> dict:
        return ocr.execute_dynamic_pool(
            self.db,
            run_id,
            self.out,
            self.pre,
            workers=workers,
            max_attempts=3,
            executor_factory=ThreadPoolExecutor,
            inference_function=fake,
            executor_initializer=None,
        )

    def test_preflight_reads_frozen_db_queue_and_local_models(self) -> None:
        self.assertEqual(self.pre["status"], "PASS")
        self.assertEqual(self.pre["queue_count"], 12)
        self.assertEqual(self.pre["queue_unique_count"], 12)
        self.assertEqual(self.pre["network_policy"], "blocked_in_worker")
        self.assertFalse(self.pre["original_video_read"])

    def test_real_result_json_omits_paddle_image_tensor(self) -> None:
        class FakeEngine:
            def predict(self, *_args, **_kwargs):
                return [{
                    "doc_preprocessor_res": {"output_img": [[[1, 2, 3]]]},
                    "rec_texts": ["测试文字"],
                    "rec_scores": [0.9],
                    "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                    "vis_fonts": ["large-font-object"],
                }]

        item = self.prepared(1)[0] | {
            "run_id": "compact-test",
            "attempt_count": 1,
        }
        with mock.patch.object(ocr, "_OCR_ENGINE", FakeEngine()):
            result = ocr.infer_ocr_item(item, str(self.out))
        payload = json.loads(Path(result["output_json_path"]).read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("output_img", serialized)
        self.assertNotIn("vis_fonts", serialized)
        self.assertEqual(payload["ocr_text"], "测试文字")
        self.assertEqual(
            payload["raw_result_retention_policy"],
            "compact_no_image_tensor_no_font_objects_v1",
        )

    def test_three_workers_reach_real_dynamic_concurrency_and_write_each_item(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(12),
            self.pre,
            run_kind="smoke",
            workers=3,
            max_attempts=3,
        )
        fake = FakeInference()
        report = self.execute(run_id, fake)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["success_count"], 12)
        self.assertEqual(report["result_count"], 12)
        self.assertEqual(fake.max_active, 3)
        state = monitor.read_state(self.db, run_id)
        self.assertEqual(state["remaining"], 0)
        self.assertTrue((self.out / "logs/progress.jsonl").is_file())
        self.assertEqual(
            len((self.out / "logs/progress.jsonl").read_text().splitlines()), 12
        )

    def test_failure_retries_without_stopping_other_items(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(8),
            self.pre,
            run_kind="smoke",
            workers=3,
            max_attempts=3,
        )
        fake = FakeInference(fail_first=True)
        report = self.execute(run_id, fake)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(fake.calls["cand_000"], 2)
        self.assertTrue(all(fake.calls[f"cand_{i:03d}"] >= 1 for i in range(8)))
        with ocr.readonly_connection(self.db) as con:
            attempts = con.execute(
                """SELECT COUNT(*) FROM stop03_4_ocr_attempts
                   WHERE run_id=? AND candidate_id='cand_000'""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(attempts, 2)

    def test_no_text_is_technical_terminal_not_failure(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(4),
            self.pre,
            run_kind="smoke",
            workers=2,
            max_attempts=3,
        )
        report = self.execute(run_id, FakeInference(no_text_for="cand_002"), workers=2)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["success_count"], 3)
        self.assertEqual(report["no_text_count"], 1)
        self.assertEqual(report["failed_count"], 0)

    def test_smoke_success_is_reused_by_full_run_without_reexecution(self) -> None:
        smoke_id = ocr.create_run_and_items(
            self.db,
            self.prepared(3),
            self.pre,
            run_kind="smoke",
            workers=3,
            max_attempts=3,
        )
        first = FakeInference()
        self.execute(smoke_id, first)
        full_id = ocr.create_run_and_items(
            self.db,
            self.prepared(8),
            self.pre,
            run_kind="full",
            workers=3,
            max_attempts=3,
        )
        second = FakeInference()
        report = self.execute(full_id, second)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["reused_count"], 3)
        self.assertEqual(sum(second.calls.values()), 5)
        self.assertFalse(any(candidate in second.calls for candidate in first.calls))

    def test_resume_resets_stale_running_and_does_not_rerun_success(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(5),
            self.pre,
            run_kind="smoke",
            workers=2,
            max_attempts=3,
        )
        claimed = ocr.claim_next_item(self.db, run_id, "stale")
        self.assertIsNotNone(claimed)
        ocr.prepare_resume(self.db, run_id, workers=2)
        with ocr.readonly_connection(self.db) as con:
            states = dict(
                con.execute(
                    """SELECT status,COUNT(*) FROM stop03_4_ocr_run_items
                       WHERE run_id=? GROUP BY status""",
                    (run_id,),
                )
            )
        self.assertEqual(states, {"pending": 5})
        fake = FakeInference()
        self.execute(run_id, fake, workers=2)
        calls_after_success = sum(fake.calls.values())
        ocr.prepare_resume(self.db, run_id, workers=2)
        again = FakeInference()
        report = self.execute(run_id, again, workers=2)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(sum(again.calls.values()), 0)
        self.assertEqual(calls_after_success, 5)

    def test_resume_rejects_changed_execution_fingerprint(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(2),
            self.pre,
            run_kind="smoke",
            workers=1,
            max_attempts=3,
        )
        changed = dict(self.pre)
        changed["script_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "ocr_resume_fingerprint_mismatch"):
            ocr.prepare_resume(self.db, run_id, workers=1, preflight=changed)

    def test_execution_keys_unique_and_db_integrity(self) -> None:
        rows = self.prepared(12)
        self.assertEqual(len({row["execution_key"] for row in rows}), 12)
        with ocr.readonly_connection(self.db) as con:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(len(list(con.execute("PRAGMA foreign_key_check"))), 0)

    def test_workers_one_remains_serial(self) -> None:
        run_id = ocr.create_run_and_items(
            self.db,
            self.prepared(5),
            self.pre,
            run_kind="smoke",
            workers=1,
            max_attempts=3,
        )
        fake = FakeInference()
        report = self.execute(run_id, fake, workers=1)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(fake.max_active, 1)

    def test_frozen_node_verifies_acceptance_run_and_locked_args(self) -> None:
        report = frozen_node.verify_node(ROOT / "media_archive.sqlite")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["acceptance_success_count"] + report["acceptance_no_text_count"],
            report["acceptance_candidate_count"],
        )
        self.assertEqual(
            report["acceptance_reused_count"]
            + report["acceptance_inference_count"],
            report["acceptance_candidate_count"],
        )
        self.assertTrue(report["candidate_set_equal"])
        frozen_node.verify_locked_args(
            [
                "--workers",
                "3",
                "--max-attempts",
                "3",
                "--run-kind",
                "full",
                "--limit",
                "0",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "locked_argument_mismatch"):
            frozen_node.verify_locked_args(["--workers", "2"])

    def test_source_contains_no_network_or_original_video_execution(self) -> None:
        source = (
            ROOT
            / "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn("install_network_block()", source)
        self.assertIn("runtime_visual_file", source)
        self.assertNotIn("VideoCapture(", source)
        self.assertNotIn("requests.get(", source)
        self.assertNotIn("urllib.request", source)


if __name__ == "__main__":
    unittest.main()
