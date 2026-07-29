from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_2_v25_candidate_contract_lock as contract_lock  # noqa: E402
import stop03_3c_qwenvl_db_orchestrator_v1 as orch  # noqa: E402


class QwenVLRealConcurrencyTests(unittest.TestCase):
    VALID_STDOUT = (
        "<|im_start|>assistant\n"
        "1）概括：画面展示城市道路及行驶车辆。\n"
        "2）元素：人物：无；物体：汽车、道路标志；场景：城市街道；动作：车辆行驶；环境：白天；文字区域：路牌。\n"
        "3）检索价值：适合检索城市道路、汽车、交通标志和街景，可用于交通与城市环境素材。\n"
        "<|im_end|>\n==========\nPrompt: 100 tokens\nGeneration: 120 tokens\nPeak memory: 1.0 GB\n"
    )

    def setUp(self) -> None:
        orch._SHUTDOWN_REQUESTED.clear()

    def _database(self, row_count: int) -> Tuple[Path, tempfile.TemporaryDirectory[str], Dict[str, Any], list[dict[str, Any]]]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = Path(temp.name) / "concurrency.sqlite"
        source = sqlite3.connect(f"file:{ROOT / 'media_archive.sqlite'}?mode=ro", uri=True)
        target = sqlite3.connect(str(db))
        source.backup(target)
        target.close()
        source.close()
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("DELETE FROM stop03_3_qwenvl_results")
        con.execute("DELETE FROM stop03_3_qwenvl_run_items")
        con.execute("DELETE FROM stop03_3_qwenvl_runs")
        con.commit()
        con.close()
        read, _ = orch.queue_connection(db, allow_simulation=False)
        try:
            rows = orch.load_queue(read)[:row_count]
            contract = orch.contract_metadata(read)
        finally:
            read.close()
        pre = {
            "queue_source": "central_db_view", "contract": contract,
            "model_path": "/local/model", "model_sha256": "a" * 64,
            "model_config_sha256": "b" * 64, "model_tokenizer_files_json": "{}",
            "model_tokenizer_files_sha256": "c" * 64,
            "model_inventory_json": "[]", "model_inventory_sha256": "d" * 64,
            "model_fingerprint_sha256": "e" * 64, "prompt_sha256": "f" * 64,
            "config_sha256": "1" * 64, "temperature": 0.0, "top_p": 1.0,
        }
        run_id, planned = orch.create_run_and_items(
            db=db, rows=rows, pre=pre,
            prompt_path=ROOT / "configs/qwenvl_prompt_v2_384.txt",
            max_tokens=384, workers=4,
        )
        return db, temp, pre, planned

    def _execute(self, db: Path, out: Path, pre: Dict[str, Any], rows: list[dict[str, Any]], workers: int, fake: Any, observer: Any = None) -> Dict[str, Any]:
        return orch.execute_items(
            db=db, out=out, run_id=self._run_id(db), rows=rows, pre=pre,
            qwen_python=Path("/local/python"), model_path=Path("/local/model"),
            prompt="prompt", required_sections=("1）概括：", "2）元素：", "3）检索价值："),
            max_tokens=384, timeout=30, workers=workers, inference_fn=fake,
            connection_observer=observer,
        )

    @staticmethod
    def _run_id(db: Path) -> str:
        con = sqlite3.connect(str(db))
        try:
            return str(con.execute("SELECT run_id FROM stop03_3_qwenvl_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0])
        finally:
            con.close()

    def test_workers_four_really_enter_four_fake_inferences_and_use_distinct_connections(self) -> None:
        db, temp, pre, rows = self._database(4)
        active = 0
        peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        connections: list[sqlite3.Connection] = []

        def observer(con: sqlite3.Connection, _phase: str, _row: dict[str, Any]) -> None:
            connections.append(con)

        def fake(**kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                kwargs["process_started_callback"](active)
            barrier.wait(timeout=5)
            time.sleep(0.05)
            with lock:
                active -= 1
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        result = self._execute(db, Path(temp.name) / "out", pre, rows, 4, fake, observer)
        self.assertEqual(peak, 4)
        self.assertEqual(result["workers_effective"], 4)
        self.assertEqual(len({id(con) for con in connections}), 4)
        self.assertEqual(result["status_counts"], {"success": 4})
        self.assertGreaterEqual(result["db_running_peak"], 3)
        progress = (Path(temp.name) / "out/logs/progress.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(progress), 4)
        print("MEASURED_FAKE_MAX_CONCURRENCY=4")

    def test_resume_branch_uses_pool_honors_limit_and_reclaims_stale_running(self) -> None:
        db, temp, pre, rows = self._database(6)
        run_id = self._run_id(db)
        con = sqlite3.connect(str(db))
        con.execute("UPDATE stop03_3_qwenvl_run_items SET status='running' WHERE run_item_id=?", (rows[0]["run_item_id"],))
        con.commit()
        con.close()
        active = 0
        peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def fake(**kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                kwargs["process_started_callback"](active)
            barrier.wait(timeout=5)
            time.sleep(0.03)
            with lock:
                active -= 1
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        config = orch.load_config(ROOT / "configs/stop03_3_qwenvl_db_v1.json")
        report = orch.production_run(
            mode="resume", db=db, out=Path(temp.name) / "resume", pre=pre,
            config=config, model_path=Path("/local/model"), qwen_python=Path("/local/python"),
            prompt_path=ROOT / "configs/qwenvl_prompt_v2_384.txt", max_tokens=384,
            workers=4, timeout=30, limit=4, run_id=run_id, inference_fn=fake,
        )
        self.assertEqual(peak, 4)
        self.assertEqual(report["execution"]["processed_count"], 4)
        con = sqlite3.connect(str(db))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=? AND status='success'", (run_id,)).fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT workers FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)).fetchone()[0], 4)
        finally:
            con.close()

    def test_inference_holds_no_database_write_lock(self) -> None:
        db, temp, pre, rows = self._database(1)
        lock_probe = []

        def fake(**_kwargs: Any) -> subprocess.CompletedProcess[str]:
            con = sqlite3.connect(str(db), timeout=0.2)
            try:
                con.execute("BEGIN IMMEDIATE")
                lock_probe.append(True)
                con.rollback()
            finally:
                con.close()
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        self._execute(db, Path(temp.name) / "out", pre, rows, 1, fake)
        self.assertEqual(lock_probe, [True])

    def test_success_is_not_reexecuted_and_execution_keys_remain_unique(self) -> None:
        db, temp, pre, rows = self._database(1)
        calls = 0

        def fake(**_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        self._execute(db, Path(temp.name) / "first", pre, rows, 1, fake)
        self.assertFalse(orch.claim_item(db, rows[0]))
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            retryable = orch.resume_filter([dict(row) for row in con.execute("SELECT * FROM stop03_3_qwenvl_run_items")])
            duplicate_count = con.execute("SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM stop03_3_qwenvl_run_items").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(calls, 1)
        self.assertEqual(retryable, [])
        self.assertEqual(duplicate_count, 0)

    def test_one_failure_does_not_stop_other_workers_and_progress_updates(self) -> None:
        db, temp, pre, rows = self._database(4)
        failed_id = rows[0]["candidate_id"]

        def fake(**kwargs: Any) -> subprocess.CompletedProcess[str]:
            if kwargs["row"]["candidate_id"] == failed_id:
                return subprocess.CompletedProcess([], 1, "", "fake failure")
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        result = self._execute(db, Path(temp.name) / "out", pre, rows, 4, fake)
        self.assertEqual(result["status_counts"].get("success"), 3)
        self.assertEqual(result["status_counts"].get("failed"), 1)
        progress = (Path(temp.name) / "out/logs/progress.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(progress), 4)

    def test_workers_one_remains_serial(self) -> None:
        db, temp, pre, rows = self._database(3)
        active = 0
        peak = 0

        def fake(**_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.01)
            active -= 1
            return subprocess.CompletedProcess([], 0, self.VALID_STDOUT, "")

        result = self._execute(db, Path(temp.name) / "out", pre, rows, 1, fake)
        self.assertEqual(peak, 1)
        self.assertEqual(result["workers_effective"], 1)

    def test_source_has_offline_and_derived_only_guards(self) -> None:
        source = (SCRIPT_DIR / "stop03_3c_qwenvl_db_orchestrator_v1.py").read_text(encoding="utf-8")
        self.assertIn('"HF_HUB_OFFLINE": "1"', source)
        self.assertIn("runtime_is_derived_only", source)
        self.assertNotIn("--video", source)
        self.assertNotIn("requests.get", source)
        self.assertNotIn("pip install", source)

    def test_verified_launcher_is_resume_only_and_signal_safe(self) -> None:
        launcher = Path("/Users/yourname/Documents/AI-Local/test-output/run_qwenvl_resume_w4_verified.sh")
        source = launcher.read_text(encoding="utf-8")
        self.assertIn('RUN_ID="stop03_3c_qwenvl_db_20260711_095949_823512"', source)
        self.assertIn("--mode resume", source)
        self.assertIn("--workers 4", source)
        self.assertIn("--max-tokens 384", source)
        self.assertIn("BLOCKED_DUPLICATE_PROCESS", source)
        self.assertIn("trap forward_signal INT TERM", source)
        self.assertIn('kill -TERM "$CHILD_PID"', source)
        self.assertNotIn("--mode run", source)
        self.assertNotIn("--limit", source)


if __name__ == "__main__":
    unittest.main()
