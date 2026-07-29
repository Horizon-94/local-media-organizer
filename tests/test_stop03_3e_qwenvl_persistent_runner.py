from __future__ import annotations

import ast
import inspect
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
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_3e_qwenvl_persistent_runner_v1 as runner  # noqa: E402


VALID_TEXT = (
    "1）概括：画面展示城市街道中的车辆与道路设施，视野清晰且主体明确。\n"
    "2）元素：人物：无；物体：汽车、路灯和标志；场景：城市道路；动作：车辆行驶；环境：白天；文字区域：远处路牌。\n"
    "3）检索价值：适合用城市道路、汽车、交通设施和白天街景检索，可用于交通与城市环境素材。"
)


def make_tasks(count: int) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"candidate-{index:04d}",
            "execution_key": f"execution-{index:04d}",
            "image_path": f"/derived/frame-{index:04d}.jpg",
        }
        for index in range(count)
    ]


class FakeBackend:
    def __init__(self, *, response_factory=None, delay: float = 0.0, init_error=None) -> None:
        self.response_factory = response_factory or self._default_response
        self.delay = delay
        self.init_error = init_error
        self.validate_count = 0
        self.load_count = 0
        self.generate_count = 0
        self.reset_count = 0
        self.requests: list[runner.RequestState] = []
        self.formatted_prompts: list[str] = []
        self.lock = threading.Lock()

    @staticmethod
    def _default_response(_call_number, _request):
        return SimpleNamespace(
            text=VALID_TEXT,
            prompt_tokens=80,
            generation_tokens=120,
            peak_memory=4.5,
            generation_tps=10.0,
            finish_reason="stop",
        )

    def validate_api(self) -> None:
        self.validate_count += 1
        if self.init_error is not None:
            raise self.init_error

    def load(self, _model_path: Path):
        self.load_count += 1
        return SimpleNamespace(language_model=SimpleNamespace()), SimpleNamespace()

    def reset_request_state(self, _model, _processor) -> None:
        self.reset_count += 1

    def format_prompt(self, _model, _processor, request: runner.RequestState) -> str:
        formatted = f"formatted:{request.prompt}"
        self.formatted_prompts.append(formatted)
        return formatted

    def generate(self, _model, _processor, _formatted_prompt, request: runner.RequestState):
        with self.lock:
            self.generate_count += 1
            call_number = self.generate_count
            self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        return self.response_factory(call_number, request)


class PersistentRunnerTests(unittest.TestCase):
    def make_adapter(self, backend: FakeBackend, observer=None):
        return runner.PersistentQwenGenerationAdapter(
            model_path=Path("/registered/local/model"),
            max_tokens=384,
            temperature=0.0,
            top_p=1.0,
            backend=backend,
            request_observer=observer,
        )

    def test_single_worker_75_requests_loads_once_and_isolates_request_state(self) -> None:
        backend = FakeBackend()
        observed: list[runner.RequestState] = []
        adapter = self.make_adapter(backend, observed.append)
        adapter.load_once()
        outcomes = [
            adapter.generate_one(
                candidate_id=f"candidate-{index}",
                image_path=f"/derived/{index}.jpg",
                prompt="fixed prompt",
            )
            for index in range(75)
        ]

        self.assertEqual(adapter.model_load_count, 1)
        self.assertEqual(backend.load_count, 1)
        self.assertEqual(backend.generate_count, 75)
        self.assertEqual([item.result_status for item in outcomes], ["success"] * 75)
        for attribute in ("messages", "image_container", "generation_kwargs"):
            self.assertEqual(len({id(getattr(item, attribute)) for item in observed}), 75)
        self.assertEqual(len({id(item.messages[0]) for item in observed}), 75)
        self.assertTrue(all(item.cache is None for item in observed))
        self.assertTrue(all(item.response is None for item in observed))
        self.assertTrue(all(item.stats is None for item in observed))
        self.assertTrue(all(item.generation_kwargs == {
            "max_tokens": 384, "temperature": 0.0, "top_p": 1.0,
        } for item in observed))
        self.assertEqual({item.prompt for item in observed}, {"fixed prompt"})

    def test_three_worker_dynamic_queue_completes_336_uniquely(self) -> None:
        tasks = make_tasks(336)
        backends: dict[int, FakeBackend] = {}

        def factory(worker_id: int):
            backend = FakeBackend(delay=0.001)
            backends[worker_id] = backend
            return self.make_adapter(backend)

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "three-worker.sqlite"
            result = runner.run_threaded_fake_scheduler(
                tasks=tasks,
                worker_count=3,
                adapter_factory=factory,
                prompt="fixed prompt",
                db_path=db_path,
            )
            con = sqlite3.connect(str(db_path))
            candidate_unique, execution_unique = con.execute(
                "SELECT COUNT(DISTINCT candidate_id),COUNT(DISTINCT execution_key) FROM tasks"
            ).fetchone()
            con.close()

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["counts"].get("success"), 336)
        self.assertEqual(result["counts"].get("pending", 0), 0)
        self.assertEqual(result["counts"].get("running", 0), 0)
        self.assertEqual(candidate_unique, 336)
        self.assertEqual(execution_unique, 336)
        self.assertEqual(set(backends), {1, 2, 3})
        self.assertTrue(all(item.load_count == 1 for item in backends.values()))
        self.assertTrue(all(item.generate_count > 0 for item in backends.values()))
        self.assertEqual(sum(item.generate_count for item in backends.values()), 336)
        self.assertEqual(result["integrity"]["integrity_check"], "ok")
        self.assertEqual(result["integrity"]["foreign_key_check"], [])
        self.assertEqual(result["integrity"]["candidate_id_duplicate_count"], 0)
        self.assertEqual(result["integrity"]["execution_key_duplicate_count"], 0)

    def test_each_worker_runs_beyond_75_without_threshold_state_change(self) -> None:
        for worker_id in range(1, 4):
            backend = FakeBackend()
            observed: list[runner.RequestState] = []
            adapter = self.make_adapter(backend, observed.append)
            adapter.load_once()
            outcomes = [
                adapter.generate_one(
                    candidate_id=f"w{worker_id}-{index}",
                    image_path=f"/derived/w{worker_id}-{index}.jpg",
                    prompt="immutable prompt",
                )
                for index in range(80)
            ]
            self.assertEqual(adapter.model_load_count, 1)
            self.assertEqual(backend.generate_count, 80)
            self.assertTrue(all(item.result_status == "success" for item in outcomes))
            self.assertEqual({item.prompt for item in observed}, {"immutable prompt"})
            self.assertEqual({tuple(sorted(item.generation_kwargs.items())) for item in observed}, {
                (("max_tokens", 384), ("temperature", 0.0), ("top_p", 1.0))
            })
            self.assertEqual(len({id(item.messages) for item in observed}), 80)
            self.assertEqual(len({id(item.image_container) for item in observed}), 80)
            self.assertEqual(len({id(item.generation_kwargs) for item in observed}), 80)

    def test_first_deterministic_api_error_fuses_remaining_74_pending(self) -> None:
        def fail_first(_call_number, _request):
            raise TypeError("unexpected keyword argument 'temperature'")

        backend = FakeBackend(response_factory=fail_first)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuse.sqlite"
            result = runner.run_threaded_fake_scheduler(
                tasks=make_tasks(75),
                worker_count=1,
                adapter_factory=lambda _worker_id: self.make_adapter(backend),
                prompt="fixed prompt",
                db_path=db_path,
            )
            con = sqlite3.connect(str(db_path))
            failed = con.execute(
                "SELECT error_type,error_message,traceback_text FROM tasks WHERE status='failed'"
            ).fetchone()
            con.close()

        self.assertEqual(result["status"], "FUSED")
        self.assertEqual(backend.generate_count, 1)
        self.assertEqual(result["counts"].get("failed"), 1)
        self.assertEqual(result["counts"].get("pending"), 74)
        self.assertEqual(failed[0], "ApiContractError")
        self.assertIn("generation_api_type_error", failed[1])
        self.assertIn("Traceback", failed[2])
        self.assertIn("unexpected keyword argument", failed[2])

    def test_response_parser_supports_string_text_texts_stats_and_null_metrics(self) -> None:
        string_result = runner.parse_generation_response(VALID_TEXT, max_tokens=512)
        self.assertEqual(string_result.response_shape, "string")
        self.assertIsNone(string_result.generation_tokens)
        self.assertIsNone(string_result.raw_finish_reason)
        self.assertEqual(string_result.inferred_finish_reason, "stop")

        text_result = runner.parse_generation_response(
            SimpleNamespace(text=VALID_TEXT, generation_tokens=511, finish_reason="stop"),
            max_tokens=512,
        )
        self.assertEqual(text_result.response_shape, "object.text")
        self.assertEqual(text_result.generation_tokens, 511)
        self.assertEqual(text_result.raw_finish_reason, "stop")

        texts_result = runner.parse_generation_response(
            SimpleNamespace(
                texts=[VALID_TEXT],
                stats=SimpleNamespace(prompt_tokens=90, generation_tokens=512, peak_memory=5.0),
            ),
            max_tokens=512,
        )
        self.assertEqual(texts_result.response_shape, "object.texts[1]")
        self.assertEqual(texts_result.prompt_tokens, 90)
        self.assertEqual(texts_result.generation_tokens, 512)
        self.assertEqual(texts_result.inferred_finish_reason, "length")
        self.assertIsNone(texts_result.raw_finish_reason)
        self.assertEqual(
            runner.classify_response("candidate", texts_result, max_tokens=512).result_status,
            "review",
        )

    def test_response_contract_handles_empty_missing_sections_and_bad_shapes(self) -> None:
        empty = runner.parse_generation_response("", max_tokens=384)
        self.assertEqual(runner.classify_response("empty", empty, max_tokens=384).result_status, "failed")

        missing_sections = runner.parse_generation_response(
            "画面只有一辆汽车，缺少固定结构。" * 10, max_tokens=384
        )
        outcome = runner.classify_response("missing", missing_sections, max_tokens=384)
        self.assertEqual(outcome.result_status, "review")
        self.assertEqual(list(outcome.missing_required_sections), list(runner.REQUIRED_SECTIONS))

        with self.assertRaisesRegex(runner.ResponseContractError, "response_text_field_missing"):
            runner.parse_generation_response(SimpleNamespace(stats={}), max_tokens=384)
        with self.assertRaisesRegex(runner.ResponseContractError, "response_text_count_mismatch"):
            runner.parse_generation_response(SimpleNamespace(texts=["a", "b"]), max_tokens=384)

    def test_initialization_error_is_deterministic_and_load_once_is_enforced(self) -> None:
        adapter = self.make_adapter(FakeBackend(init_error=ValueError("bad signature")))
        with self.assertRaisesRegex(runner.InitializationError, "model_initialization_failed"):
            adapter.load_once()

        good = self.make_adapter(FakeBackend())
        good.load_once()
        with self.assertRaisesRegex(runner.InitializationError, "more_than_once"):
            good.load_once()

    def test_batch_api_helper_omits_unsupported_sampling_kwargs(self) -> None:
        captured = {}

        def fake_batch_generate(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(texts=[VALID_TEXT], stats=SimpleNamespace())

        backend = runner.LocalMLXVLMBackend()
        backend.batch_generate_fn = fake_batch_generate
        backend.batch_generate_one(object(), object(), "/derived/frame.jpg", "prompt", 384)
        self.assertNotIn("temperature", captured)
        self.assertNotIn("top_p", captured)
        self.assertNotIn("sampler", captured)
        self.assertEqual(captured["max_tokens"], 384)
        self.assertEqual(captured["images"], ["/derived/frame.jpg"])
        self.assertEqual(captured["prompts"], ["prompt"])

    def test_sqlite_integrity_uniqueness_and_running_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = runner.Stop03EStateStore(Path(temp_dir) / "state.sqlite")
            tasks = make_tasks(3)
            store.initialize(tasks)
            self.assertTrue(store.claim(tasks[0]["candidate_id"], worker_id=1))
            self.assertEqual(store.counts().get("running"), 1)
            self.assertEqual(store.recover_running(), 1)
            self.assertEqual(store.counts().get("pending"), 3)
            integrity = store.integrity()
            self.assertEqual(integrity["integrity_check"], "ok")
            self.assertEqual(integrity["foreign_key_check"], [])
            self.assertEqual(integrity["candidate_id_duplicate_count"], 0)
            self.assertEqual(integrity["execution_key_duplicate_count"], 0)

        duplicate_id = make_tasks(2)
        duplicate_id[1]["candidate_id"] = duplicate_id[0]["candidate_id"]
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "duplicate_candidate_id"):
                runner.Stop03EStateStore(Path(temp_dir) / "duplicate.sqlite").initialize(duplicate_id)

        duplicate_key = make_tasks(2)
        duplicate_key[1]["execution_key"] = duplicate_key[0]["execution_key"]
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "duplicate_execution_key"):
                runner.Stop03EStateStore(Path(temp_dir) / "duplicate.sqlite").initialize(duplicate_key)

    def test_source_has_no_per_item_subprocess_or_fixed_count_rotation(self) -> None:
        source = inspect.getsource(runner)
        tree = ast.parse(source)
        integer_constants = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        }
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.", source)
        self.assertNotIn("worker_rotation", source)
        self.assertNotIn("restart_worker", source)
        self.assertTrue({60, 70, 71}.isdisjoint(integer_constants))
        self.assertNotIn("requests.", source)
        self.assertNotIn("urllib.", source)
        self.assertNotIn("socket.", source)


if __name__ == "__main__":
    unittest.main()
