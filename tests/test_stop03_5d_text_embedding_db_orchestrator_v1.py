from __future__ import annotations

import json
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as contract  # noqa: E402
import stop03_5d_text_embedding_db_orchestrator_v1 as orchestrator  # noqa: E402
from tests import test_stop03_5d_text_embedding_db_contract_v1 as contract_tests  # noqa: E402


class Tracker:
    def __init__(self, parties: int = 1) -> None:
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(parties) if parties > 1 else None
        self.active = 0
        self.maximum = 0
        self.calls = 0


class FakeAdapter:
    def __init__(self, tracker: Tracker, index: int, fail_once: bool = False) -> None:
        self.tracker = tracker
        self.index = index
        self.fail_once = fail_once
        self.model_load_count = 0
        self.device_effective = "fake"

    def load_once(self) -> None:
        self.model_load_count += 1
        if self.tracker.barrier is not None:
            self.tracker.barrier.wait(timeout=5)

    def encode(self, text: str) -> list[float]:
        with self.tracker.lock:
            self.tracker.active += 1
            self.tracker.maximum = max(self.tracker.maximum, self.tracker.active)
            self.tracker.calls += 1
            call = self.tracker.calls
        try:
            time.sleep(0.04)
            if self.fail_once and call == 1:
                raise RuntimeError("intentional_fake_failure")
            vector = [0.0] * 1024
            vector[self.index % 1024] = 1.0
            return vector
        finally:
            with self.tracker.lock:
                self.tracker.active -= 1


class Stop035DTextEmbeddingDBOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = contract_tests.Stop035DTextEmbeddingContractTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.db = self.fixture.db
        self.out = self.fixture.out
        self.contract_config = self.fixture.config
        self.runtime_config = (
            ROOT / "configs/stop03_5d_text_embedding_db_orchestrator_v1.json"
        )
        self.migration = (
            ROOT / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"
        )
        self.preflight, self.documents, self.jobs = orchestrator.build_preflight(
            self.db, self.contract_config, self.runtime_config, self.migration
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def create_run(self, workers: int = 3, max_attempts: int = 3) -> str:
        orchestrator.apply_migration(self.db, self.migration)
        run_id, reused = orchestrator.create_run_and_queue(
            self.db, self.preflight, self.documents, self.jobs,
            workers=workers, max_attempts=max_attempts,
        )
        self.assertFalse(reused)
        return run_id

    def test_migration_has_dynamic_worker_fields(self) -> None:
        orchestrator.apply_migration(self.db, self.migration)
        con = sqlite3.connect(str(self.db))
        try:
            run_columns = {
                row[1] for row in con.execute(
                    "PRAGMA table_info(stop03_5d_text_embedding_runs)"
                )
            }
            vector_columns = {
                row[1] for row in con.execute(
                    "PRAGMA table_info(stop03_5d_text_vectors)"
                )
            }
        finally:
            con.close()
        self.assertTrue({"workers", "max_attempts", "scheduling_mode"} <= run_columns)
        self.assertTrue({"claimed_by_worker", "worker_pid", "elapsed_seconds"} <= vector_columns)

    def test_create_run_uses_dynamic_database_counts_and_links(self) -> None:
        run_id = self.create_run()
        con = sqlite3.connect(str(self.db))
        try:
            run = con.execute(
                "SELECT document_count,unique_text_count,workers,scheduling_mode "
                "FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?",
                (run_id,),
            ).fetchone()
            vectors = con.execute(
                "SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE embedding_run_id=?",
                (run_id,),
            ).fetchone()[0]
            links = con.execute(
                "SELECT COUNT(*) FROM stop03_5d_document_vector_links WHERE embedding_run_id=?",
                (run_id,),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(run[0], len(self.documents))
        self.assertEqual(run[1], len(self.jobs))
        self.assertEqual(run[2], 3)
        self.assertEqual(run[3], "dynamic_database_claim")
        self.assertEqual(vectors, len(self.jobs))
        self.assertEqual(links, len(self.documents))

    def test_three_workers_really_run_three_fake_inferences_concurrently(self) -> None:
        run_id = self.create_run()
        tracker = Tracker(parties=3)
        stop = threading.Event()
        reports: queue.Queue = queue.Queue()
        original_block = orchestrator.block_worker_network
        orchestrator.block_worker_network = lambda: None
        try:
            threads = [
                threading.Thread(
                    target=orchestrator.execute_dynamic_worker,
                    kwargs={
                        "worker_id": index,
                        "db": self.db,
                        "out": self.out,
                        "run_id": run_id,
                        "model_path": self.fixture.model,
                        "device": "auto",
                        "max_attempts": 3,
                        "stop_event": stop,
                        "report_queue": reports,
                        "adapter": FakeAdapter(tracker, index),
                    },
                )
                for index in range(1, 4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
        finally:
            orchestrator.block_worker_network = original_block
        result = orchestrator.finalize_run(
            self.db, run_id, max_attempts=3, interrupted=False
        )
        self.assertEqual(tracker.maximum, 3)
        self.assertEqual(tracker.calls, len(self.jobs))
        self.assertEqual(result["status"], "PASS")
        statuses = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.out / "worker_status").glob("worker_*.json"))
        ]
        self.assertEqual(len(statuses), 3)
        self.assertTrue(all(row["model_load_count"] == 1 for row in statuses))

    def test_success_rows_are_not_reexecuted(self) -> None:
        run_id = self.create_run(workers=1)
        tracker = Tracker()
        original_block = orchestrator.block_worker_network
        orchestrator.block_worker_network = lambda: None
        try:
            orchestrator.execute_dynamic_worker(
                worker_id=1, db=self.db, out=self.out, run_id=run_id,
                model_path=self.fixture.model, device="auto", max_attempts=3,
                stop_event=threading.Event(), report_queue=queue.Queue(),
                adapter=FakeAdapter(tracker, 1),
            )
            first_calls = tracker.calls
            orchestrator.prepare_resume(
                self.db, run_id, self.preflight, workers=1, max_attempts=3
            )
            orchestrator.execute_dynamic_worker(
                worker_id=1, db=self.db, out=self.out, run_id=run_id,
                model_path=self.fixture.model, device="auto", max_attempts=3,
                stop_event=threading.Event(), report_queue=queue.Queue(),
                adapter=FakeAdapter(tracker, 1),
            )
        finally:
            orchestrator.block_worker_network = original_block
        self.assertEqual(first_calls, len(self.jobs))
        self.assertEqual(tracker.calls, first_calls)

    def test_resume_reclaims_stale_running(self) -> None:
        run_id = self.create_run()
        item = orchestrator.claim_next_item(
            self.db, run_id, "dead_worker", max_attempts=3
        )
        self.assertIsNotNone(item)
        orchestrator.prepare_resume(
            self.db, run_id, self.preflight, workers=3, max_attempts=3
        )
        reclaimed = []
        while True:
            row = orchestrator.claim_next_item(
                self.db, run_id, "new_worker", max_attempts=3
            )
            if row is None:
                break
            reclaimed.append(row)
        stale = next(
            row for row in reclaimed
            if row["text_vector_id"] == item["text_vector_id"]
        )
        self.assertEqual(stale["attempt_count"], 2)

    def test_one_failure_does_not_block_and_is_retried(self) -> None:
        run_id = self.create_run(workers=1, max_attempts=2)
        tracker = Tracker()
        original_block = orchestrator.block_worker_network
        orchestrator.block_worker_network = lambda: None
        try:
            orchestrator.execute_dynamic_worker(
                worker_id=1, db=self.db, out=self.out, run_id=run_id,
                model_path=self.fixture.model, device="auto", max_attempts=2,
                stop_event=threading.Event(), report_queue=queue.Queue(),
                adapter=FakeAdapter(tracker, 1, fail_once=True),
            )
        finally:
            orchestrator.block_worker_network = original_block
        result = orchestrator.finalize_run(
            self.db, run_id, max_attempts=2, interrupted=False
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(tracker.calls, len(self.jobs) + 1)

    def test_dry_run_copy_does_not_change_central_db(self) -> None:
        before = contract.sha256_file(self.db)
        result = orchestrator.validate_migration_on_copy(
            self.db, self.migration, self.out
        )
        self.assertEqual(result["database_integrity_check"], "ok")
        self.assertEqual(result["foreign_key_error_count"], 0)
        self.assertGreaterEqual(result["stop03_5d_object_count"], 5)
        self.assertEqual(before, contract.sha256_file(self.db))

    def test_different_database_size_uses_new_dynamic_counts(self) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.execute(
                "INSERT INTO derived_assets VALUES(?,?,?,?,?)",
                ("d5", "source1", "video_frame_jpg1280", 5, 5000),
            )
            con.execute("INSERT INTO visual_units VALUES(?,?)", ("v5", "d5"))
            con.execute(
                """INSERT INTO stop03_5_unified_evidence_items
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "s-new", "qwenvl", "PASS", "e5", "c5", "source1",
                    "v5", "d5", "完全不同的新增场景文字",
                ),
            )
            con.commit()
        finally:
            con.close()
        preflight, documents, jobs = orchestrator.build_preflight(
            self.db, self.contract_config, self.runtime_config, self.migration
        )
        self.assertEqual(len(documents), len(self.documents) + 1)
        self.assertEqual(len(jobs), len(self.jobs) + 1)
        orchestrator.apply_migration(self.db, self.migration)
        run_id, reused = orchestrator.create_run_and_queue(
            self.db, preflight, documents, jobs, workers=2, max_attempts=2
        )
        self.assertFalse(reused)
        con = sqlite3.connect(str(self.db))
        try:
            stored = con.execute(
                """SELECT document_count,unique_text_count,workers
                   FROM stop03_5d_text_embedding_runs
                   WHERE embedding_run_id=?""",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(stored, (len(documents), len(jobs), 2))

    def test_generic_frozen_files_exclude_current_project_identity(self) -> None:
        generic_files = [
            ROOT / "docs/pipeline_rules/STOP03_5D_GENERIC_TEXT_EMBEDDING_DB_CONTRACT_DESIGN_V1.md",
            ROOT / "docs/pipeline_rules/STOP03_5D_TEXT_EMBEDDING_DYNAMIC_DB_NODE_V1.md",
        ]
        forbidden = (
            "stop03_5d_db064", "stop03_5b_8ae", "stop03_5c_9f",
            "407", "758", "351", "623", "390", "336",
        )
        for path in generic_files:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, text, f"{value} leaked into {path.name}")

    def test_entry_scripts_do_not_hardcode_current_user_project_path(self) -> None:
        paths = [
            ROOT / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_contract_v1.py",
            ROOT / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_smoke_v1.py",
            ROOT / "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py",
            ROOT / "scripts/stop03_monitor/stop03_5d_text_embedding_db_monitor.py",
        ]
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/yourname", source)
            self.assertNotIn("MEDIA_ARCHIVE_TEST_SOURCE", source)

    def test_source_has_no_fixed_project_counts_or_original_video_access(self) -> None:
        source = (
            ROOT / "scripts/03_stop03_visual_analysis/"
            "stop03_5d_text_embedding_db_orchestrator_v1.py"
        ).read_text(encoding="utf-8")
        for fixed_count in ("336", "390", "407", "623", "758"):
            self.assertNotIn(fixed_count, source)
        self.assertNotIn("VideoCapture(", source)
        self.assertNotIn("requests.", source)
        self.assertIn("local_files_only=True", source)
        self.assertIn("BEGIN IMMEDIATE", source)


if __name__ == "__main__":
    unittest.main()
