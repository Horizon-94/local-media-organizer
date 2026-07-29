from __future__ import annotations

import contextlib
import io
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
MONITOR_DIR = ROOT / "scripts/stop03_monitor"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(MONITOR_DIR))

import stop03_3c_qwenvl_db_orchestrator_v1 as contract  # noqa: E402
import stop03_3f_qwenvl_batch75_diagnostic_v1 as batch75  # noqa: E402
import stop03_3f_qwenvl_dynamic_db_orchestrator_v1 as dynamic  # noqa: E402
import stop03_3f_qwenvl_dynamic_db_node_v1 as frozen_node  # noqa: E402
import stop03_3f_qwenvl_dynamic_db_monitor as monitor  # noqa: E402


VALID_TEXT = (
    "1）概括：画面展示白天城市道路上的车辆、建筑与交通设施，主体和环境清晰。\n"
    "2）元素：人物：无；物体：汽车、路灯和标志；场景：城市道路；"
    "动作：车辆行驶；环境：白天；文字区域：远处路牌。\n"
    "3）检索价值：适合使用城市道路、车辆、交通设施和白天街景检索。"
)


class SharedFakeControl:
    def __init__(self, fail_once: bool = False) -> None:
        self.lock = threading.Lock()
        self.failed_once = not fail_once
        self.write_lock_checks = 0
        self.write_lock_failures = 0


class FakeBackend:
    def __init__(
        self,
        *,
        db_path: Path,
        control: SharedFakeControl,
        delay: float,
    ) -> None:
        self.db_path = db_path
        self.control = control
        self.delay = delay
        self.load_count = 0
        self.generate_count = 0
        self.prompts: list[str] = []

    def validate_api(self):
        return {"fake": True, "sampling_contract": "greedy"}

    def load(self, _model_path):
        self.load_count += 1
        return SimpleNamespace(language_model=SimpleNamespace()), SimpleNamespace()

    def snapshot(self, _model, _processor):
        return {
            "mlx_memory_bytes": {
                "get_active_memory": 1_000_000_000 + self.generate_count,
                "get_cache_memory": 0,
                "get_peak_memory": 2_000_000_000,
            }
        }

    def generate_one(self, _model, _processor, *, image_path, prompt, max_tokens):
        del image_path, max_tokens
        self.generate_count += 1
        self.prompts.append(prompt)
        con = sqlite3.connect(str(self.db_path), timeout=1.0)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            with self.control.lock:
                self.control.write_lock_checks += 1
        except sqlite3.OperationalError:
            with self.control.lock:
                self.control.write_lock_failures += 1
            raise
        finally:
            con.close()
        with self.control.lock:
            if not self.control.failed_once:
                self.control.failed_once = True
                raise TypeError("fake first-attempt failure")
        time.sleep(self.delay)
        return SimpleNamespace(
            texts=[VALID_TEXT],
            stats=SimpleNamespace(
                prompt_tokens=80,
                generation_tokens=120,
                generation_tps=10.0,
                peak_memory=4.0,
            ),
        )


class DynamicDbOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.temp_path = Path(self.temp.name)
        self.db = self.temp_path / "central.sqlite"
        source = sqlite3.connect(str(ROOT / "media_archive.sqlite"))
        target = sqlite3.connect(str(self.db))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
        con = contract.readonly_connection(self.db)
        try:
            metadata = contract.contract_metadata(con)
        finally:
            con.close()
        digest = "a" * 64
        self.pre = {
            "contract": metadata,
            "model_path": "/registered/model",
            "model_sha256": digest,
            "model_config_sha256": digest,
            "model_tokenizer_files_json": "[]",
            "model_tokenizer_files_sha256": digest,
            "model_inventory_json": "[]",
            "model_inventory_sha256": digest,
            "model_fingerprint_sha256": digest,
            "prompt_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "temperature": 0.0,
            "top_p": 1.0,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_test_run(self, count: int = 24) -> tuple[str, Path]:
        rows = dynamic.prepare_execution_rows(
            dynamic.load_frozen_queue(self.db)[:count],
            pre=self.pre,
            max_tokens=384,
        )
        run_id = dynamic.create_run_and_items(
            db=self.db,
            rows=rows,
            pre=self.pre,
            prompt_path=ROOT / "configs/qwenvl_prompt_v2_384.txt",
            max_tokens=384,
            workers=3,
        )
        out = self.temp_path / "out"
        out.mkdir()
        return run_id, out

    def run_fake_workers(
        self,
        *,
        run_id: str,
        out: Path,
        fail_once: bool,
    ) -> tuple[list[FakeBackend], list[dict]]:
        control = SharedFakeControl(fail_once=fail_once)
        backends = [
            FakeBackend(db_path=self.db, control=control, delay=delay)
            for delay in (0.004, 0.0002, 0.0002)
        ]
        stop_event = threading.Event()
        reports: queue.Queue = queue.Queue()
        threads = []
        with contextlib.redirect_stdout(io.StringIO()):
            for worker_id, backend in enumerate(backends, start=1):
                adapter = batch75.PersistentCorrectedBatchAdapter(
                    model_path=Path("/registered/model"),
                    max_tokens=384,
                    backend=backend,
                )
                thread = threading.Thread(
                    target=dynamic.execute_dynamic_worker,
                    kwargs={
                        "worker_id": worker_id,
                        "db": self.db,
                        "out": out,
                        "run_id": run_id,
                        "prompt": "fixed prompt",
                        "model_path": Path("/registered/model"),
                        "max_tokens": 384,
                        "max_attempts": 2,
                        "pre": self.pre,
                        "stop_event": stop_event,
                        "report_queue": reports,
                        "adapter": adapter,
                    },
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        report_rows = [reports.get_nowait() for _ in range(3)]
        self.assertEqual(control.write_lock_failures, 0)
        if sum(backend.generate_count for backend in backends):
            self.assertGreater(control.write_lock_checks, 0)
        return backends, report_rows

    def test_queue_count_is_discovered_and_execution_keys_are_unique(self) -> None:
        rows = dynamic.load_frozen_queue(self.db)
        prepared = dynamic.prepare_execution_rows(
            rows[:17],
            pre=self.pre,
            max_tokens=384,
        )
        self.assertEqual(len(prepared), 17)
        self.assertEqual(len({row["execution_key"] for row in prepared}), 17)
        self.assertTrue(all("assigned_worker_id" not in row for row in prepared))

    def test_three_workers_dynamically_claim_and_write_each_success(self) -> None:
        run_id, out = self.create_test_run(24)
        backends, reports = self.run_fake_workers(
            run_id=run_id,
            out=out,
            fail_once=False,
        )
        counts = dynamic.finalize_run(
            self.db,
            run_id,
            max_attempts=2,
            interrupted=False,
        )
        state = monitor.read_state(self.db, run_id, max_attempts=2)
        self.assertEqual(counts["total"], 24)
        self.assertEqual(counts["success"], 24)
        self.assertEqual(counts["results"], 24)
        self.assertEqual(counts["terminal_non_success"], 0)
        self.assertEqual(state["counts"]["success"], 24)
        self.assertEqual(sum(backend.generate_count for backend in backends), 24)
        self.assertTrue(all(backend.load_count == 1 for backend in backends))
        self.assertTrue(all(report["lifecycle"] == "completed" for report in reports))
        self.assertTrue(all(backend.generate_count > 0 for backend in backends))
        self.assertGreater(max(backend.generate_count for backend in backends), 8)
        self.assertLess(backends[0].generate_count, max(
            backends[1].generate_count, backends[2].generate_count
        ))
        self.assertEqual(
            contract.readback_run(self.db, run_id, expected_count=24)["status"],
            "PASS",
        )

    def test_failed_item_is_retried_once_and_success_is_not_reexecuted(self) -> None:
        run_id, out = self.create_test_run(18)
        backends, _reports = self.run_fake_workers(
            run_id=run_id,
            out=out,
            fail_once=True,
        )
        counts = dynamic.finalize_run(
            self.db,
            run_id,
            max_attempts=2,
            interrupted=False,
        )
        first_total_calls = sum(backend.generate_count for backend in backends)
        self.assertEqual(counts["success"], 18)
        self.assertEqual(counts["retried_items"], 1)
        self.assertEqual(first_total_calls, 19)
        self.assertEqual(counts["max_attempt_count"], 2)
        all_prompts = [prompt for backend in backends for prompt in backend.prompts]
        self.assertTrue(any("【格式恢复约束】" in prompt for prompt in all_prompts))
        con = contract.readonly_connection(self.db)
        try:
            retried = con.execute(
                """SELECT r.prompt_sha256,r.runtime_metrics_json
                FROM stop03_3_qwenvl_results r
                JOIN stop03_3_qwenvl_run_items i ON i.run_item_id=r.run_item_id
                WHERE r.run_id=? AND i.attempt_count=2""",
                (run_id,),
            ).fetchone()
            retry_metrics = __import__("json").loads(retried["runtime_metrics_json"])
            self.assertEqual(
                retry_metrics["prompt_strategy"],
                dynamic.COMPACT_RETRY_PROMPT_VERSION,
            )
            self.assertEqual(
                retried["prompt_sha256"],
                retry_metrics["effective_prompt_sha256"],
            )
            self.assertNotEqual(retried["prompt_sha256"], self.pre["prompt_sha256"])
        finally:
            con.close()

        second_backends, _reports = self.run_fake_workers(
            run_id=run_id,
            out=out,
            fail_once=False,
        )
        self.assertEqual(sum(backend.generate_count for backend in second_backends), 0)
        con = contract.readonly_connection(self.db)
        try:
            self.assertEqual(
                int(
                    con.execute(
                        "SELECT COUNT(*) FROM stop03_3_qwenvl_results WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                18,
            )
            self.assertEqual(
                int(
                    con.execute(
                        """SELECT COUNT(*) FROM stop03_3_qwenvl_run_items
                        WHERE run_id=? AND attempt_count>1""",
                        (run_id,),
                    ).fetchone()[0]
                ),
                1,
            )
        finally:
            con.close()

    def test_stale_running_is_reset_for_resume(self) -> None:
        run_id, _out = self.create_test_run(9)
        claimed = dynamic.claim_next_item(self.db, run_id, max_attempts=2)
        self.assertIsNotNone(claimed)
        dynamic.prepare_resume(
            self.db,
            run_id,
            pre=self.pre,
            workers=3,
            max_tokens=384,
        )
        con = contract.readonly_connection(self.db)
        try:
            statuses = dict(
                con.execute(
                    """SELECT status,COUNT(*) FROM stop03_3_qwenvl_run_items
                    WHERE run_id=? GROUP BY status""",
                    (run_id,),
                )
            )
        finally:
            con.close()
        self.assertEqual(statuses, {"pending": 9})

    def test_compatible_script_upgrade_requires_explicit_confirmation(self) -> None:
        run_id, _out = self.create_test_run(6)
        old_sha = next(iter(dynamic.COMPATIBLE_RESUME_SCRIPT_SHAS))
        con = sqlite3.connect(str(self.db))
        try:
            con.execute(
                "UPDATE stop03_3_qwenvl_runs SET script_sha256=? WHERE run_id=?",
                (old_sha, run_id),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(
            RuntimeError,
            "explicit_compatible_resume_confirmation_required",
        ):
            dynamic.prepare_resume(
                self.db,
                run_id,
                pre=self.pre,
                workers=3,
                max_tokens=384,
            )
        metadata = dynamic.prepare_resume(
            self.db,
            run_id,
            pre=self.pre,
            workers=3,
            max_tokens=384,
            confirm_compatible_script_resume=True,
        )
        self.assertTrue(metadata["compatible_script_upgrade"])
        self.assertEqual(metadata["previous_script_sha256"], old_sha)

    def test_frozen_node_verifies_hashes_contract_and_locked_args(self) -> None:
        report = frozen_node.verify_node(self.db)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["frozen_contract"]["row_count"],
            report["frozen_contract"]["qwenvl_count"]
            + report["frozen_contract"]["ocr_count"],
        )
        self.assertEqual(
            report["frozen_runtime"]["scheduling_mode"],
            "dynamic_database_claim",
        )
        frozen_node.verify_locked_args(
            ["--workers", "3", "--max-tokens", "384", "--max-attempts", "3"]
        )
        normalized = frozen_node.normalize_locked_args([])
        self.assertEqual(
            normalized,
            ["--workers", "3", "--max-tokens", "384", "--max-attempts", "3"],
        )
        with self.assertRaisesRegex(RuntimeError, "locked_argument_mismatch"):
            frozen_node.verify_locked_args(["--workers", "2"])
        changed = dict(frozen_node.FROZEN_FILES)
        path = next(iter(changed))
        changed[path] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "file_hash_mismatch"):
            frozen_node.verify_node(self.db, frozen_files=changed)


if __name__ == "__main__":
    unittest.main()
