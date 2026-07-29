from __future__ import annotations

import contextlib
import io
import queue
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

import stop03_3f_qwenvl_batch75_diagnostic_v1 as batch75  # noqa: E402
import stop03_3f_qwenvl_batch_w3x75_validation_v1 as w3  # noqa: E402
import stop03_3f_batch_w3x75_monitor as monitor  # noqa: E402


VALID_TEXT = (
    "1）概括：画面展示白天城市道路上的车辆、建筑与交通设施，主体和环境清晰。\n"
    "2）元素：人物：无；物体：汽车、路灯和标志；场景：城市道路；动作：车辆行驶；环境：白天；文字区域：远处路牌。\n"
    "3）检索价值：适合使用城市道路、车辆、交通设施和白天街景检索，可用于交通与城市环境素材。"
)


def make_tasks(count: int) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"candidate-{index:03d}",
            "execution_key": f"source-execution-{index:03d}",
            "image_path": f"/derived/frame-{index:03d}.jpg",
            "input_sha256": f"{index:064x}"[-64:],
        }
        for index in range(1, count + 1)
    ]


class FakeBackend:
    def __init__(self, *, degenerate_at: int | None = None, delay: float = 0.0) -> None:
        self.degenerate_at = degenerate_at
        self.delay = delay
        self.load_count = 0
        self.generate_count = 0

    def validate_api(self):
        return {"fake": True, "sampling_contract": "no_temperature_or_top_p"}

    def load(self, _model_path):
        self.load_count += 1
        return SimpleNamespace(language_model=SimpleNamespace()), SimpleNamespace()

    def snapshot(self, model, processor):
        return {
            "model_object_id": id(model),
            "processor_object_id": id(processor),
            "position_ids": {"is_none": True},
            "rope_deltas": {"is_none": True},
            "mlx_memory_bytes": {
                "get_active_memory": 3_000_000_000 + self.generate_count,
                "get_cache_memory": 0,
                "get_peak_memory": 5_000_000_000,
            },
        }

    def generate_one(self, _model, _processor, *, image_path, prompt, max_tokens):
        del image_path, prompt
        self.generate_count += 1
        if self.delay:
            time.sleep(self.delay)
        if self.generate_count == self.degenerate_at:
            text = "!" * max_tokens
            tokens = max_tokens
        else:
            text = VALID_TEXT
            tokens = 120
        return SimpleNamespace(
            texts=[text],
            stats=SimpleNamespace(
                prompt_tokens=80, generation_tokens=tokens,
                generation_tps=10.0, peak_memory=4.0,
            ),
            image_sizes=[(720, 1280)],
        )


class W3X75Tests(unittest.TestCase):
    def test_fixed_assignments_are_exact_unique_3_by_75(self) -> None:
        assignments = w3.assign_fixed_workers(make_tasks(225))
        self.assertEqual(set(assignments), {1, 2, 3})
        self.assertEqual([len(assignments[index]) for index in (1, 2, 3)], [75, 75, 75])
        for worker_id in (1, 2, 3):
            self.assertEqual(
                [item["worker_seq"] for item in assignments[worker_id]],
                list(range(1, 76)),
            )
            self.assertTrue(all(item["worker_id"] == worker_id for item in assignments[worker_id]))
        all_items = [item for values in assignments.values() for item in values]
        self.assertEqual(len({item["candidate_id"] for item in all_items}), 225)
        self.assertEqual(len({item["execution_key"] for item in all_items}), 225)
        self.assertEqual([item["global_seq"] for item in all_items], list(range(1, 226)))

    def _run_threads(self, *, degenerate_worker: int | None = None):
        assignments = w3.assign_fixed_workers(make_tasks(225))
        temp = tempfile.TemporaryDirectory()
        db_path = Path(temp.name) / "w3.sqlite"
        store = w3.W3X75Store(db_path)
        store.initialize(assignments, {"status": "RUNNING", "assignment_mode": "fixed_75_per_worker"})
        stop_event = threading.Event()
        report_queue: queue.Queue = queue.Queue()
        backends = {
            worker_id: FakeBackend(
                degenerate_at=71 if worker_id == degenerate_worker else None,
                delay=0.0005,
            )
            for worker_id in (1, 2, 3)
        }
        threads = []
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for worker_id in (1, 2, 3):
                adapter = batch75.PersistentCorrectedBatchAdapter(
                    model_path=Path("/registered/model"), max_tokens=384,
                    backend=backends[worker_id],
                )
                thread = threading.Thread(
                    target=w3.execute_worker_assignment,
                    kwargs={
                        "worker_id": worker_id,
                        "tasks": assignments[worker_id],
                        "store_path": db_path,
                        "prompt": "fixed prompt",
                        "model_path": Path("/registered/model"),
                        "max_tokens": 384,
                        "stop_event": stop_event,
                        "report_queue": report_queue,
                        "adapter": adapter,
                    },
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        reports = [report_queue.get_nowait() for _ in range(3)]
        return temp, db_path, store, backends, reports

    def test_three_fake_workers_each_cross_71_and_complete_75(self) -> None:
        temp, db_path, store, backends, reports = self._run_threads()
        try:
            summary = store.summary()
            state = monitor.read_state(db_path)
            self.assertEqual(summary["counts"].get("success"), 225)
            self.assertEqual(summary["counts"].get("pending", 0), 0)
            self.assertEqual(summary["counts"].get("running", 0), 0)
            self.assertEqual(summary["snapshot_count"], 450)
            self.assertEqual(summary["integrity_check"], "ok")
            self.assertEqual(summary["foreign_key_check"], [])
            self.assertEqual(summary["candidate_id_duplicate_count"], 0)
            self.assertEqual(summary["execution_key_duplicate_count"], 0)
            self.assertEqual(len(summary["boundary_65_75"]), 33)
            self.assertTrue(all(row["status"] == "success" for row in summary["boundary_65_75"]))
            self.assertTrue(all(backend.load_count == 1 for backend in backends.values()))
            self.assertTrue(all(backend.generate_count == 75 for backend in backends.values()))
            self.assertTrue(all(report["model_load_count"] == 1 for report in reports))
            self.assertTrue(all(report["completed"] == 75 for report in reports))
            self.assertEqual(len(state["workers"]), 3)
            self.assertEqual(len(state["items"]), 225)
        finally:
            temp.cleanup()

    def test_worker_1_degenerate_at_71_sets_global_fuse(self) -> None:
        temp, _db_path, store, backends, reports = self._run_threads(degenerate_worker=1)
        try:
            summary = store.summary()
            first = summary["first_degenerate"]
            self.assertIsNotNone(first)
            self.assertEqual(first["worker_id"], 1)
            self.assertEqual(first["worker_seq"], 71)
            self.assertEqual(first["degenerate_reason"], "bang_only_repetition")
            worker1 = next(row for row in summary["workers"] if row["worker_id"] == 1)
            self.assertEqual(worker1["success"], 70)
            self.assertEqual(worker1["review"], 1)
            self.assertEqual(backends[1].generate_count, 71)
            self.assertTrue(any(report["fuse_reason"] for report in reports))
            self.assertGreater(summary["counts"].get("pending", 0), 0)
        finally:
            temp.cleanup()

    def test_rejects_non_225_input(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "input_count_mismatch"):
            w3.assign_fixed_workers(make_tasks(224))


if __name__ == "__main__":
    unittest.main()
