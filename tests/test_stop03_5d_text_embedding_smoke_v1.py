from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as contract  # noqa: E402
import stop03_5d_text_embedding_smoke_v1 as smoke  # noqa: E402
from tests import test_stop03_5d_text_embedding_db_contract_v1 as contract_tests  # noqa: E402


class Stop035DTextEmbeddingSmokeTests(unittest.TestCase):
    def test_sample_is_deterministic_unique_and_not_fixed_to_current_count(self) -> None:
        jobs = [
            {
                "text_vector_id": f"v{index}",
                "embedding_text": "字" * (index + 1),
                "embedding_text_sha256": f"h{index}",
                "document_count": 20 if index == 7 else 1,
            }
            for index in range(9)
        ]
        first = smoke.select_smoke_jobs(jobs, 5)
        second = smoke.select_smoke_jobs(list(reversed(jobs)), 5)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)
        self.assertEqual(len({row["text_vector_id"] for row in first}), 5)
        self.assertIn("v7", {row["text_vector_id"] for row in first})

    def test_vector_validation_accepts_normalized_top1(self) -> None:
        documents = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
        queries = [list(row) for row in documents]
        checks, matrix = smoke.validate_vectors(documents, queries, 2)
        self.assertTrue(checks["all_checks_pass"])
        self.assertEqual(matrix[0], [1.0, 0.0, -1.0])

    def test_vector_validation_rejects_wrong_dimension(self) -> None:
        checks, _matrix = smoke.validate_vectors(
            [[1.0, 0.0], [0.0, 1.0]],
            [[1.0, 0.0], [0.0, 1.0]],
            3,
        )
        self.assertFalse(checks["all_checks_pass"])
        self.assertFalse(checks["document_dimensions_match"])

    def test_fake_smoke_writes_outputs_without_db_change(self) -> None:
        fixture = contract_tests.Stop035DTextEmbeddingContractTests(
            methodName="runTest"
        )
        fixture.setUp()
        try:
            before = contract.sha256_file(fixture.db)

            def fake_inference(model, texts, dimension, device):
                self.assertEqual(dimension, 1024)
                vectors = []
                for index in range(len(texts)):
                    vector = [0.0] * dimension
                    vector[index] = 1.0
                    vectors.append(vector)
                return vectors, [list(row) for row in vectors], {
                    "device": "fake",
                    "model_load_seconds": 0.0,
                    "inference_seconds": 0.0,
                }

            summary = smoke.execute_smoke(
                db=fixture.db,
                config_path=fixture.config,
                out=fixture.out,
                sample_count=3,
                device="auto",
                inference=fake_inference,
            )
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(before, contract.sha256_file(fixture.db))
            self.assertFalse(summary["central_db_write"])
            self.assertFalse(summary["network_used"])
            self.assertFalse(summary["original_video_read"])
            self.assertTrue(
                (fixture.out / "reports/stop03_5d_smoke_summary.json").is_file()
            )
            self.assertEqual(
                (fixture.out / "vectors/document_vectors_float32.bin").stat().st_size,
                3 * 1024 * 4,
            )
        finally:
            fixture.tearDown()

    def test_preflight_does_not_import_model_runtime_or_write_output(self) -> None:
        fixture = contract_tests.Stop035DTextEmbeddingContractTests(
            methodName="runTest"
        )
        fixture.setUp()
        try:
            original = smoke.real_sentence_transformer_inference

            def forbidden(*args, **kwargs):
                raise AssertionError("model runtime must not be called")

            smoke.real_sentence_transformer_inference = forbidden
            code = smoke.main(
                [
                    "--mode", "preflight",
                    "--db", str(fixture.db),
                    "--config", str(fixture.config),
                    "--out", str(fixture.out),
                    "--sample-count", "3",
                ]
            )
            self.assertEqual(code, 0)
            self.assertFalse(fixture.out.exists())
        finally:
            smoke.real_sentence_transformer_inference = original
            fixture.tearDown()

    def test_confirmation_is_required_for_real_smoke(self) -> None:
        fixture = contract_tests.Stop035DTextEmbeddingContractTests(
            methodName="runTest"
        )
        fixture.setUp()
        try:
            with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
                smoke.main(
                    [
                        "--mode", "real-smoke",
                        "--db", str(fixture.db),
                        "--config", str(fixture.config),
                        "--out", str(fixture.out),
                        "--sample-count", "3",
                    ]
                )
            self.assertFalse(fixture.out.exists())
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
