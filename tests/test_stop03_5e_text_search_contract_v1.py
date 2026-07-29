from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as embedding_contract  # noqa: E402
import stop03_5d_text_embedding_db_orchestrator_v1 as embedding_runner  # noqa: E402
import stop03_5e_text_search_contract_v1 as search_contract  # noqa: E402
from tests import test_stop03_5d_text_embedding_db_orchestrator_v1 as runner_tests  # noqa: E402


class Stop035ETextSearchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = runner_tests.Stop035DTextEmbeddingDBOrchestratorTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.db = self.fixture.db
        self.out = self.fixture.out / "search"
        self.config = ROOT / "configs/stop03_5e_text_search_contract_v1.json"

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def complete_embedding_run(self) -> str:
        run_id = self.fixture.create_run(workers=1, max_attempts=2)
        index = 0
        while True:
            item = embedding_runner.claim_next_item(
                self.db, run_id, "fixture", max_attempts=2
            )
            if item is None:
                break
            vector = [0.0] * int(item["model_dimension"])
            vector[index % len(vector)] = 1.0
            embedding_runner.persist_success(
                self.db, item, vector, elapsed_seconds=0.01, worker_pid=1
            )
            index += 1
        result = embedding_runner.finalize_run(
            self.db, run_id, max_attempts=2, interrupted=False
        )
        self.assertEqual(result["status"], "PASS")
        return run_id

    def test_preflight_uses_dynamic_run_counts_and_blob_contract(self) -> None:
        run_id = self.complete_embedding_run()
        summary = search_contract.build_preflight(self.db, self.config)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["selected_embedding_run_id"], run_id)
        self.assertEqual(
            summary["observed_document_count"], len(self.fixture.documents)
        )
        self.assertEqual(
            summary["observed_unique_vector_count"], len(self.fixture.jobs)
        )
        self.assertEqual(
            summary["observed_link_count"], len(self.fixture.documents)
        )
        self.assertTrue(all(summary["checks"].values()))

    def test_dry_run_writes_only_output_and_does_not_change_db(self) -> None:
        self.complete_embedding_run()
        before = embedding_contract.sha256_file(self.db)
        code = search_contract.main([
            "--mode", "dry-run", "--db", str(self.db),
            "--config", str(self.config), "--out", str(self.out),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(before, embedding_contract.sha256_file(self.db))
        report = self.out / "reports/stop03_5e_text_search_contract_summary.json"
        self.assertTrue(report.is_file())
        summary = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(summary["query_model_run"])
        self.assertFalse(summary["search_index_created"])
        self.assertFalse(summary["database_write"])

    def test_preflight_rejects_database_without_successful_embedding_run(self) -> None:
        self.fixture.create_run(workers=1, max_attempts=2)
        with self.assertRaisesRegex(
            RuntimeError, "latest_success_embedding_run_missing"
        ):
            search_contract.build_preflight(self.db, self.config)

    def test_config_freezes_baseline_not_current_dataset_size(self) -> None:
        value = search_contract.load_config(self.config)
        self.assertEqual(
            value["baseline_backend"], "sqlite_blob_streaming_cosine_v1"
        )
        self.assertEqual(
            value["source_run_selector"], "latest_success_stop03_5d"
        )
        self.assertTrue(value["result_thumbnail_required"])
        self.assertEqual(
            value["result_thumbnail_asset_policy"],
            "relative_symlink_then_readonly_copy",
        )
        self.assertEqual(value["video_preview_window_ms"], 5000)
        self.assertEqual(value["timecode_default_precision"], "millisecond")
        self.assertEqual(
            value["timecode_precision_choices"], ["second", "millisecond"]
        )
        self.assertFalse(value["original_video_clip_generation"])
        self.assertEqual(value["video_preview_window_options_ms"], [5000, 10000])
        self.assertEqual(
            value["environment_label_policy"],
            "temporal_qwen_consensus_non_destructive_v1",
        )
        self.assertTrue(value["environment_user_confirmation_supported"])
        self.assertGreaterEqual(
            value["max_documents_per_group"], value["default_documents_per_group"]
        )
        raw = self.config.read_text(encoding="utf-8")
        for forbidden in (
            "stop03_5d_db064", "407", "758", "/Users/yourname",
            "MEDIA_ARCHIVE_TEST_SOURCE",
        ):
            self.assertNotIn(forbidden, raw)

    def test_generic_contract_has_no_current_project_identity(self) -> None:
        path = (
            ROOT / "docs/pipeline_rules/"
            "STOP03_5E_GENERIC_TEXT_SEARCH_CONTRACT_DESIGN_V1.md"
        )
        raw = path.read_text(encoding="utf-8")
        for forbidden in (
            "stop03_5d_db064", "stop03_5b_8ae", "stop03_5c_9f",
            "407", "758", "/Users/yourname", "MEDIA_ARCHIVE_TEST_SOURCE",
        ):
            self.assertNotIn(forbidden, raw)

    def test_preflight_source_is_read_only_and_does_not_load_model(self) -> None:
        source = (
            ROOT / "scripts/03_stop03_visual_analysis/"
            "stop03_5e_text_search_contract_v1.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("SentenceTransformer", source)
        self.assertNotIn("AutoModel", source)
        self.assertNotIn("torch.", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("VideoCapture(", source)
        self.assertNotIn("CREATE VIRTUAL TABLE", source.upper())
        self.assertIn("mode=ro", source)
        self.assertIn("PRAGMA query_only=ON", source)


if __name__ == "__main__":
    unittest.main()
