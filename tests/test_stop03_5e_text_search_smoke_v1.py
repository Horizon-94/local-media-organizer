from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as embedding_contract  # noqa: E402
import stop03_5e_text_search_smoke_v1 as smoke  # noqa: E402
from tests import test_stop03_5e_text_search_contract_v1 as contract_tests  # noqa: E402


class Stop035ETextSearchSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = contract_tests.Stop035ETextSearchContractTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.run_id = self.fixture.complete_embedding_run()
        self.db = self.fixture.db
        self.config = self.fixture.config
        self.out = self.fixture.out / "query-smoke"
        self.preview_dir = self.fixture.fixture.fixture.root / "derived-previews"
        self.preview_dir.mkdir()
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("ALTER TABLE derived_assets ADD COLUMN derived_path TEXT")
            for (derived_id,) in con.execute(
                "SELECT derived_id FROM derived_assets ORDER BY derived_id"
            ).fetchall():
                preview = self.preview_dir / f"{derived_id}.jpg"
                preview.write_bytes(b"\xff\xd8fixture-jpeg\xff\xd9")
                con.execute(
                    "UPDATE derived_assets SET derived_path=? WHERE derived_id=?",
                    (str(preview), derived_id),
                )
            con.commit()
        finally:
            con.close()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @staticmethod
    def args(**overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "device": "cpu",
            "top_groups": 3,
            "documents_per_group": 2,
            "timecode_precision": "millisecond",
            "media_type": None,
            "document_kind": None,
            "source_content_id": None,
            "source_relative_path_prefix": None,
            "time_position_ms_min": None,
            "time_position_ms_max": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def fake_embedder(
        _model_path: Path,
        queries: list[str],
        _prompt_name: str,
        _device: str,
    ) -> tuple[list[list[float]], dict[str, object]]:
        vector = [0.0] * 1024
        vector[0] = 1.0
        return [list(vector) for _ in queries], {
            "device": "fake",
            "model_load_seconds": 0.0,
            "query_embedding_seconds": 0.0,
        }

    def test_query_normalization_and_duplicate_rejection(self) -> None:
        config = smoke.search_contract.load_config(self.config)
        self.assertEqual(smoke.validate_queries(["  人\u3000物  "], config), ["人 物"])
        with self.assertRaisesRegex(
            RuntimeError, "duplicate_normalized_queries"
        ):
            smoke.validate_queries(["人物", " 人物 "], config)

    def test_timecode_is_human_readable_and_precision_is_selectable(self) -> None:
        self.assertEqual(smoke.format_timecode(365000, "millisecond"), "06:05.000")
        self.assertEqual(smoke.format_timecode(365000, "second"), "06:05")
        self.assertEqual(
            smoke.format_timecode(3_665_042, "millisecond"), "01:01:05.042"
        )

    def test_environment_labels_are_conservative_and_expose_ambiguity(self) -> None:
        self.assertEqual(
            smoke.classify_environment_texts(["人物坐在室内房间"])[
                "environment_code"
            ],
            "indoor",
        )
        self.assertEqual(
            smoke.classify_environment_texts(["夜晚户外城市街道"])[
                "environment_code"
            ],
            "outdoor_night",
        )
        ambiguous = smoke.classify_environment_texts([
            "背景可能为室内暗色墙面", "相邻画面为夜间城市建筑",
        ])
        self.assertEqual(ambiguous["environment_code"], "night_or_indoor")
        self.assertEqual(ambiguous["environment_label"], "夜间/室内（待确认）")
        self.assertTrue(ambiguous["environment_user_confirmation_required"])

    def test_vectors_are_streamed_in_configurable_chunks_and_ranked(self) -> None:
        documents = smoke.load_search_documents(self.db, self.run_id, "", [])
        chunks = list(
            smoke.iter_vector_chunks(self.db, self.run_id, "", [], chunk_size=1)
        )
        self.assertEqual(len(chunks), len(documents))
        self.assertTrue(all(len(chunk) == 1 for chunk in chunks))
        first_vector_id = str(chunks[0][0]["text_vector_id"])
        result, stats = smoke.scan_cosine_groups(
            ["通用查询"],
            [self.fake_embedder(Path(), ["q"], "query", "cpu")[0][0]],
            chunks,
            documents,
            top_groups=3,
            documents_per_group=10,
        )
        self.assertEqual(result[0]["result_groups"][0]["text_vector_id"], first_vector_id)
        self.assertEqual(stats["scanned_vector_count"], len(documents))
        self.assertEqual(stats["scanned_chunk_count"], len(chunks))
        duplicate_group = next(
            group
            for group in result[0]["result_groups"]
            if group["matching_document_count"] == 2
        )
        self.assertEqual(len(duplicate_group["documents"]), 2)

    def test_fake_real_flow_is_read_only_traceable_and_does_not_persist_query(self) -> None:
        before = embedding_contract.sha256_file(self.db)
        private_queries = ["私密查询甲", "私密查询乙", "私密查询丙"]
        report = smoke.execute_smoke(
            db=self.db,
            config_path=self.config,
            out=self.out,
            queries=private_queries,
            args=self.args(),
            embedder=self.fake_embedder,
        )
        self.assertEqual(report["technical_status"], "PASS")
        self.assertEqual(report["policy_status"], "REVIEW")
        self.assertFalse(report["query_text_persisted"])
        self.assertFalse(report["query_vectors_persisted"])
        self.assertFalse(report["database_write"])
        self.assertFalse(report["search_index_created"])
        self.assertEqual(report["visual_preview"]["preview_asset_missing_count"], 0)
        self.assertEqual(
            report["visual_preview"]["html_img_total_count"],
            report["visual_preview"]["displayed_document_occurrence_count"],
        )
        self.assertEqual(
            report["visual_preview"]["html_img_http_accessible_check_status"],
            "PASS_STATIC_RELATIVE_ASSETS",
        )
        self.assertGreater(report["visual_preview"]["video_preview_segment_count"], 0)
        self.assertEqual(before, embedding_contract.sha256_file(self.db))
        raw_json = (
            self.out / "reports/stop03_5e_query_smoke_results.json"
        ).read_text(encoding="utf-8")
        raw_html = (
            self.out / "reports/stop03_5e_query_smoke_results.html"
        ).read_text(encoding="utf-8")
        for query in private_queries:
            self.assertNotIn(query, raw_json)
            self.assertNotIn(query, raw_html)
        self.assertIn('src="assets/', raw_html)
        self.assertIn("视频预览区间", raw_html)
        self.assertIn("命中时间：", raw_html)
        self.assertIn("场景：", raw_html)
        self.assertNotIn(" · t=", raw_html)
        self.assertNotIn('src="file://', raw_html)
        self.assertNotIn('src="/', raw_html)
        self.assertTrue(report["technical_checks"]["all_eligible_vectors_scanned"])

    def test_existing_report_can_be_rebuilt_without_query_model(self) -> None:
        private_queries = ["私密查询甲", "私密查询乙", "私密查询丙"]
        smoke.execute_smoke(
            db=self.db,
            config_path=self.config,
            out=self.out,
            queries=private_queries,
            args=self.args(),
            embedder=self.fake_embedder,
        )
        before = embedding_contract.sha256_file(self.db)
        rebuilt = smoke.render_existing_report(self.db, self.config, self.out)
        self.assertEqual(rebuilt["technical_status"], "PASS")
        self.assertTrue(rebuilt["query_model_run"])
        self.assertEqual(before, embedding_contract.sha256_file(self.db))
        self.assertEqual(
            rebuilt["visual_preview"]["html_img_missing_asset_count"], 0
        )

    def test_document_filters_apply_before_vector_scan(self) -> None:
        args = self.args(
            media_type="image",
            source_relative_path_prefix="image",
        )
        sql, values = smoke.build_document_filter_sql(args)
        documents = smoke.load_search_documents(self.db, self.run_id, sql, values)
        self.assertEqual(sum(map(len, documents.values())), 1)
        only_document = next(iter(documents.values()))[0]
        self.assertEqual(only_document["media_type"], "image")
        chunks = list(
            smoke.iter_vector_chunks(
                self.db, self.run_id, sql, values, chunk_size=2048
            )
        )
        self.assertEqual(sum(map(len, chunks)), 1)

    def test_preflight_does_not_write_output_or_call_embedder(self) -> None:
        before = embedding_contract.sha256_file(self.db)
        code = smoke.main([
            "--mode", "preflight",
            "--db", str(self.db),
            "--config", str(self.config),
            "--out", str(self.out),
            "--query", "人物",
            "--query", "车辆",
            "--query", "自然风景",
        ])
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, embedding_contract.sha256_file(self.db))

    def test_real_smoke_confirmation_is_required(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
            smoke.main([
                "--mode", "real-smoke",
                "--db", str(self.db),
                "--config", str(self.config),
                "--out", str(self.out),
                "--query", "人物",
                "--query", "车辆",
                "--query", "自然风景",
            ])

    def test_source_is_generic_local_only_and_contains_no_fixed_dataset_size(self) -> None:
        source = (
            ROOT / "scripts/03_stop03_visual_analysis/"
            "stop03_5e_text_search_smoke_v1.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "stop03_5d_db064", "407", "758", "/Users/yourname",
            "MEDIA_ARCHIVE_TEST_SOURCE", "requests.", "VideoCapture(",
            "CREATE VIRTUAL TABLE",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("local_files_only=True", source)
        self.assertIn("fetchmany(chunk_size)", source)
        self.assertIn("temporal_qwen_consensus_non_destructive_v1", source)
        self.assertIn("connect_ro", source)


if __name__ == "__main__":
    unittest.main()
