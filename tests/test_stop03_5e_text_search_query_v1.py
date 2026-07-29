from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as embedding_contract  # noqa: E402
import stop03_5e_text_search_query_v1 as query_entry  # noqa: E402
from tests import test_stop03_5e_text_search_smoke_v1 as smoke_tests  # noqa: E402


class Stop035ETextSearchQueryEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = smoke_tests.Stop035ETextSearchSmokeTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.db = self.fixture.db
        self.config = self.fixture.config
        self.out = self.fixture.out / "production-query"

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @staticmethod
    def args(**overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "group_offset": 0,
            "group_limit": 3,
            "document_offset": 0,
            "documents_per_group": 2,
            "preview_window_ms": 5000,
            "timecode_precision": "millisecond",
            "device": "cpu",
            "media_type": None,
            "document_kind": None,
            "source_content_id": None,
            "source_relative_path_prefix": None,
            "time_position_ms_min": None,
            "time_position_ms_max": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def execute(
        self, query: str = "绝密生产查询词", **overrides: object
    ) -> tuple[dict[str, object], Path]:
        return query_entry.execute_query(
            db=self.db,
            config_path=self.config,
            out=self.out,
            query=query,
            args=self.args(**overrides),
            embedder=self.fixture.fake_embedder,
        )

    def test_preflight_is_dynamic_read_only_and_request_id_is_stable(self) -> None:
        args1 = self.args()
        before = embedding_contract.sha256_file(self.db)
        first, normalized = query_entry.build_query_preflight(
            self.db, self.config, "  城市人物  ", args1
        )
        second, _ = query_entry.build_query_preflight(
            self.db, self.config, "城市人物", self.args()
        )
        self.assertEqual(normalized, "城市人物")
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(
            first["selected_embedding_run_id"], self.fixture.run_id
        )
        self.assertFalse(first["query_model_run"])
        self.assertEqual(before, embedding_contract.sha256_file(self.db))

    def test_fake_query_response_is_complete_read_only_and_private(self) -> None:
        private_query = "绝密生产查询词"
        before = embedding_contract.sha256_file(self.db)
        response, request_out = self.execute(private_query)
        self.assertEqual(response["technical_status"], "PASS")
        self.assertEqual(response["policy_status"], "PASS")
        self.assertFalse(response["query_text_persisted"])
        self.assertFalse(response["query_vector_persisted"])
        self.assertFalse(response["database_write"])
        self.assertFalse(response["search_index_created"])
        self.assertEqual(before, embedding_contract.sha256_file(self.db))
        result = response["queries"][0]
        self.assertEqual(result["result_group_count"], 3)
        self.assertEqual(result["total_result_group_count"], 3)
        self.assertTrue(all(group["documents"] for group in result["result_groups"]))
        documents = [
            document
            for group in result["result_groups"]
            for document in group["documents"]
        ]
        self.assertTrue(all(document["source_content_id"] for document in documents))
        self.assertTrue(all("environment_code" in document for document in documents))
        video_documents = [row for row in documents if row["media_type"] == "video"]
        self.assertTrue(video_documents)
        self.assertTrue(
            all("preview_segment_start_ms" in row for row in video_documents)
        )
        response_json = request_out / "reports/query_response.json"
        response_html = request_out / "reports/query_response.html"
        self.assertTrue(response_json.is_file())
        self.assertTrue(response_html.is_file())
        self.assertNotIn(private_query, response_json.read_text(encoding="utf-8"))
        self.assertNotIn(private_query, response_html.read_text(encoding="utf-8"))

    def test_group_and_document_pagination_are_stable(self) -> None:
        first, _ = self.execute(group_limit=1, documents_per_group=1)
        second, _ = self.execute(
            group_offset=1, group_limit=1, documents_per_group=1
        )
        first_result = first["queries"][0]
        second_result = second["queries"][0]
        self.assertEqual(first_result["next_group_offset"], 1)
        self.assertNotEqual(
            first_result["result_groups"][0]["text_vector_id"],
            second_result["result_groups"][0]["text_vector_id"],
        )
        full, _ = self.execute(group_limit=3, documents_per_group=1)
        reused = next(
            group for group in full["queries"][0]["result_groups"]
            if group["matching_document_count"] == 2
        )
        self.assertEqual(reused["returned_document_count"], 1)
        self.assertEqual(reused["next_document_offset"], 1)

    def test_filter_and_ten_second_preview_are_applied(self) -> None:
        response, _ = self.execute(
            media_type="video", preview_window_ms=10000,
            timecode_precision="second",
        )
        documents = [
            document
            for group in response["queries"][0]["result_groups"]
            for document in group["documents"]
        ]
        self.assertTrue(all(row["media_type"] == "video" for row in documents))
        self.assertTrue(all(row["timecode"].count(".") == 0 for row in documents))
        self.assertTrue(all(
            row["preview_segment_end_ms"] - row["preview_segment_start_ms"]
            == 10000
            for row in documents
        ))

    def test_dry_run_writes_only_plan_and_does_not_load_model(self) -> None:
        before = embedding_contract.sha256_file(self.db)
        code = query_entry.main([
            "--mode", "dry-run",
            "--db", str(self.db),
            "--config", str(self.config),
            "--out", str(self.out),
            "--query", "城市人物",
            "--group-limit", "2",
            "--documents-per-group", "1",
        ])
        self.assertEqual(code, 0)
        plans = list((self.out / "dry-run").glob("*/query_plan.json"))
        self.assertEqual(len(plans), 1)
        plan = json.loads(plans[0].read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "DRY_RUN_PASS")
        self.assertFalse(plan["query_model_run"])
        self.assertEqual(before, embedding_contract.sha256_file(self.db))

    def test_real_query_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
            query_entry.main([
                "--mode", "query",
                "--db", str(self.db),
                "--config", str(self.config),
                "--out", str(self.out),
                "--query", "城市人物",
            ])

    def test_source_is_generic_local_only_and_has_no_dataset_constants(self) -> None:
        source = (
            ROOT / "scripts/03_stop03_visual_analysis/"
            "stop03_5e_text_search_query_v1.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "stop03_5d_db064", "407", "758", "/Users/yourname",
            "MEDIA_ARCHIVE_TEST_SOURCE", "requests.", "VideoCapture(",
            "CREATE VIRTUAL TABLE",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("build_query_preflight", source)
        self.assertIn("confirm-real-local-query", source)
        self.assertIn("real_query_embedder", source)


if __name__ == "__main__":
    unittest.main()
