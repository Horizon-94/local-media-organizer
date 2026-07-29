from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
MONITOR_DIR = ROOT / "scripts/stop03_monitor"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(MONITOR_DIR))

import stop03_3f_qwenvl_batch75_diagnostic_v1 as diagnostic  # noqa: E402
import stop03_3f_batch75_monitor as monitor  # noqa: E402


VALID_TEXT = (
    "1）概括：画面展示白天城市街道中的车辆与道路设施，主体和环境清晰。\n"
    "2）元素：人物：无；物体：汽车、路灯和标志；场景：城市道路；动作：车辆行驶；环境：白天；文字区域：远处路牌。\n"
    "3）检索价值：适合使用城市道路、汽车、交通设施和白天街景检索，可用于交通与城市环境素材。"
)


def make_tasks(count: int) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"candidate-{index:03d}",
            "execution_key": f"execution-{index:03d}",
            "image_path": f"/derived/frame-{index:03d}.jpg",
            "input_sha256": f"{index:064x}"[-64:],
        }
        for index in range(1, count + 1)
    ]


class FakeBatchBackend:
    def __init__(self, *, degenerate_at: int | None = None, fail_at: int | None = None) -> None:
        self.degenerate_at = degenerate_at
        self.fail_at = fail_at
        self.validate_count = 0
        self.load_count = 0
        self.generate_count = 0
        self.calls: list[dict[str, object]] = []

    def validate_api(self):
        self.validate_count += 1
        return {"fake": True, "sampling_contract": "no_temperature_or_top_p"}

    def load(self, _model_path):
        self.load_count += 1
        return SimpleNamespace(language_model=SimpleNamespace()), SimpleNamespace()

    def snapshot(self, model, processor):
        return {
            "model_object_id": id(model),
            "processor_object_id": id(processor),
            "call_count": self.generate_count,
            "mlx_memory_bytes": {"get_active_memory": self.generate_count * 100},
        }

    def generate_one(self, model, processor, *, image_path, prompt, max_tokens):
        self.generate_count += 1
        self.calls.append({
            "model_id": id(model), "processor_id": id(processor),
            "image_path": image_path, "prompt": prompt, "max_tokens": max_tokens,
        })
        if self.fail_at == self.generate_count:
            raise TypeError("fake deterministic API mismatch")
        if self.degenerate_at == self.generate_count:
            text = "!" * max_tokens
            tokens = max_tokens
        else:
            text = VALID_TEXT
            tokens = 120
        return SimpleNamespace(
            texts=[text],
            stats=SimpleNamespace(
                prompt_tokens=80,
                generation_tokens=tokens,
                generation_tps=10.0,
                peak_memory=4.0,
            ),
            image_sizes=[(720, 1280)],
        )


class Stop03FDiagnosticTests(unittest.TestCase):
    def test_fake_batch_path_completes_75_with_one_model_load(self) -> None:
        backend = FakeBatchBackend()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            report = diagnostic.run_diagnostic(
                tasks=make_tasks(75), prompt="fixed prompt", output_dir=output,
                model_path=Path("/registered/model"), max_tokens=384,
                backend=backend,
            )
            state = monitor.read_state(output / "run/stop03_3f_state.sqlite")

        self.assertEqual(report["status"], "BATCH_PATH_75_PASS_PENDING_THREE_WORKER_VALIDATION")
        self.assertEqual(report["model_load_count"], 1)
        self.assertEqual(backend.validate_count, 1)
        self.assertEqual(backend.load_count, 1)
        self.assertEqual(backend.generate_count, 75)
        self.assertEqual(report["summary"]["counts"].get("success"), 75)
        self.assertEqual(report["summary"]["counts"].get("pending", 0), 0)
        self.assertEqual(report["summary"]["snapshot_count"], 150)
        self.assertEqual(report["summary"]["integrity_check"], "ok")
        self.assertEqual(report["summary"]["foreign_key_check"], [])
        self.assertEqual(state["metadata"]["model_load_count"], 1)
        self.assertEqual(len(state["rows"]), 75)
        self.assertTrue(all(call["max_tokens"] == 384 for call in backend.calls))
        self.assertEqual(len({call["model_id"] for call in backend.calls}), 1)
        self.assertEqual(len({call["processor_id"] for call in backend.calls}), 1)

    def test_seq_71_degenerate_output_fuses_and_leaves_four_pending(self) -> None:
        backend = FakeBatchBackend(degenerate_at=71)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            report = diagnostic.run_diagnostic(
                tasks=make_tasks(75), prompt="fixed prompt", output_dir=output,
                model_path=Path("/registered/model"), max_tokens=384,
                backend=backend, stop_on_degenerate=True,
            )
            con = sqlite3.connect(str(output / "run/stop03_3f_state.sqlite"))
            boundary = con.execute(
                "SELECT seq,status,generation_tokens,degenerate_reason FROM items "
                "WHERE seq BETWEEN 69 AND 72 ORDER BY seq"
            ).fetchall()
            con.close()

        self.assertEqual(report["status"], "BATCH_PATH_DEGENERATE_REPRODUCED")
        self.assertEqual(backend.generate_count, 71)
        self.assertEqual(report["summary"]["counts"].get("success"), 70)
        self.assertEqual(report["summary"]["counts"].get("review"), 1)
        self.assertEqual(report["summary"]["counts"].get("pending"), 4)
        self.assertEqual(report["summary"]["first_degenerate"]["seq"], 71)
        self.assertEqual(boundary[0][1], "success")
        self.assertEqual(boundary[1][1], "success")
        self.assertEqual(boundary[2][1:], ("review", 384, "bang_only_repetition"))
        self.assertEqual(boundary[3][1], "pending")

    def test_first_type_error_is_wrapped_and_fused(self) -> None:
        backend = FakeBatchBackend(fail_at=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            report = diagnostic.run_diagnostic(
                tasks=make_tasks(75), prompt="fixed prompt",
                output_dir=Path(temp_dir) / "run",
                model_path=Path("/registered/model"), max_tokens=384,
                backend=backend,
            )
        self.assertEqual(report["status"], "BATCH_PATH_DETERMINISTIC_ERROR_FUSED")
        self.assertEqual(backend.generate_count, 1)
        self.assertEqual(report["summary"]["counts"].get("failed"), 1)
        self.assertEqual(report["summary"]["counts"].get("pending"), 74)
        self.assertIn("BatchAPIContractError", report["fuse_reason"])

    def test_batch_response_parser_keeps_raw_finish_null_and_infers_length(self) -> None:
        response = SimpleNamespace(
            texts=["!" * 384],
            stats=SimpleNamespace(generation_tokens=384),
        )
        parsed = diagnostic.parse_batch_response(response, max_tokens=384)
        outcome = diagnostic.classify_batch_response("candidate", parsed, max_tokens=384)
        self.assertIsNone(parsed.raw_finish_reason)
        self.assertEqual(parsed.inferred_finish_reason, "length")
        self.assertEqual(outcome.result_status, "review")
        self.assertEqual(outcome.degenerate_reason, "bang_only_repetition")
        self.assertEqual(outcome.generation_tokens, 384)

        with self.assertRaisesRegex(diagnostic.BatchResponseContractError, "text_count"):
            diagnostic.parse_batch_response(
                SimpleNamespace(texts=["one", "two"], stats=None), max_tokens=384
            )

    def test_local_batch_backend_call_omits_temperature_and_top_p(self) -> None:
        captured = {}

        def fake_batch(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(texts=[VALID_TEXT], stats=SimpleNamespace())

        backend = diagnostic.LocalCorrectedBatchBackend()
        backend.batch_generate_fn = fake_batch
        backend.generate_one(
            object(), object(), image_path="/derived/frame.jpg",
            prompt="prompt", max_tokens=384,
        )
        self.assertEqual(captured["images"], ["/derived/frame.jpg"])
        self.assertEqual(captured["prompts"], ["prompt"])
        self.assertEqual(captured["max_tokens"], 384)
        self.assertNotIn("temperature", captured)
        self.assertNotIn("top_p", captured)
        self.assertNotIn("sampler", captured)

    def test_output_path_guard_rejects_non_test_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "output_outside_test_output"):
            diagnostic.assert_test_output_path(ROOT / "not-allowed")


if __name__ == "__main__":
    unittest.main()
