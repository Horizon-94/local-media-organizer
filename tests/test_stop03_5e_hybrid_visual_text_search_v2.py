from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5e_hybrid_visual_text_search_v2 as hybrid  # noqa: E402
from tests import test_stop03_5e_text_search_smoke_v1 as smoke_tests  # noqa: E402


class Stop035EHybridVisualTextSearchV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = smoke_tests.Stop035ETextSearchSmokeTests(methodName="runTest")
        self.fixture.setUp()
        self.db = self.fixture.db
        self.out = self.fixture.out / "hybrid-v2"
        self.config = ROOT / "configs/stop03_5e_hybrid_visual_text_search_v2.json"
        base_fixture = self.fixture.fixture.fixture.fixture
        self.openclip_python = base_fixture.python
        self.openclip_model = base_fixture.root / "openclip.safetensors"
        self.openclip_model.write_bytes(b"fixture")
        self.payload = base_fixture.root / "openclip_vectors.jsonl"
        self._add_full_visual_fixture()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _add_full_visual_fixture(self) -> None:
        extra_preview = self.fixture.preview_dir / "d5.jpg"
        extra_preview.write_bytes(b"\xff\xd8extra-visual-only\xff\xd9")
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(
                """
                CREATE TABLE embeddings(
                    embedding_id TEXT PRIMARY KEY,visual_unit_id TEXT,
                    source_content_id TEXT,model_name TEXT,model_path TEXT,
                    dimension INTEGER,vector_key TEXT,run_id TEXT,created_at TEXT
                );
                CREATE TABLE model_runs(
                    run_id TEXT PRIMARY KEY,stage TEXT,model_name TEXT,model_path TEXT,
                    script_version TEXT,script_path TEXT,input_count INTEGER,
                    output_count INTEGER,status TEXT,started_at TEXT,finished_at TEXT,
                    error_message TEXT
                );
                CREATE TABLE visual_labels(
                    label_id TEXT,visual_unit_id TEXT,source_content_id TEXT,
                    label TEXT,confidence REAL,bbox TEXT,model_name TEXT,
                    model_path TEXT,text_encoder_asset TEXT,run_id TEXT,created_at TEXT
                );
                CREATE TABLE visual_label_terms(
                    label TEXT PRIMARY KEY,label_zh TEXT,category_zh TEXT,
                    source_layer TEXT,trigger_strength TEXT,used_by_json TEXT,
                    search_terms_json TEXT,embedding_text TEXT,registry_path TEXT,
                    registry_schema_version TEXT,created_at TEXT,updated_at TEXT
                );
                """
            )
            con.execute("INSERT INTO source_assets VALUES(?,?,?)", ("source3", "extra.jpg", "image"))
            con.execute(
                "INSERT INTO derived_assets VALUES(?,?,?,?,?,?)",
                ("d5", "source3", "image_preview_jpg1280", -1, -1, str(extra_preview)),
            )
            con.execute("INSERT INTO visual_units VALUES(?,?)", ("v5", "d5"))
            run_id = "openclip-complete-dynamic"
            con.execute(
                "INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, "stop03_1b_openclip_visual_embedding", "ViT-B-32",
                    str(self.openclip_model), "fixture", "fixture.py", 5, 5,
                    "success", "2026-03-01", "2026-03-01", None,
                ),
            )
            vectors = {
                "v1": [1.0, 0.0],
                "v2": self._normalized([0.8, 0.2]),
                "v3": [0.0, 1.0],
                "v4": self._normalized([0.7, 0.3]),
                "v5": self._normalized([0.99, 0.01]),
            }
            payload_rows = []
            source_by_visual = {"v1": "source1", "v2": "source1", "v3": "source2", "v4": "source1", "v5": "source3"}
            for index, (visual_id, vector) in enumerate(vectors.items(), 1):
                embedding_id = f"clip-{index}"
                packed = json.dumps(vector, separators=(",", ":"))
                payload_rows.append({
                    "embedding_id": embedding_id, "visual_unit_id": visual_id,
                    "source_content_id": source_by_visual[visual_id],
                    "model_name": "ViT-B-32", "model_path": str(self.openclip_model),
                    "dimension": 2,
                    "vector_sha256": hashlib.sha256(packed.encode()).hexdigest(),
                    "vector": vector, "run_id": run_id, "created_at": "2026-03-01",
                })
                con.execute(
                    "INSERT INTO embeddings VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        embedding_id, visual_id, source_by_visual[visual_id],
                        "ViT-B-32", str(self.openclip_model), 2,
                        f"jsonl:{self.payload}#{embedding_id}", run_id, "2026-03-01",
                    ),
                )
            self.payload.write_text(
                "\n".join(json.dumps(row, separators=(",", ":")) for row in payload_rows) + "\n",
                encoding="utf-8",
            )
            con.execute(
                "INSERT INTO visual_label_terms VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "person", "人", "人物", "fixture", "high", "[]",
                    json.dumps(["人物", "人像"]), "人 person 人物", "fixture", "v1",
                    "2026-03-01", "2026-03-01",
                ),
            )
            con.execute(
                "INSERT INTO visual_labels VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "label-v5", "v5", "source3", "person", 0.9, "[]",
                    "fixture", "fixture", "fixture", "yolo-run", "2026-03-01",
                ),
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _normalized(values: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "query": "人物照片", "openclip_python": self.openclip_python,
            "result_offset": 0, "result_limit": 5, "temporal_dedup_ms": 0,
            "preview_window_ms": 10000, "timecode_precision": "millisecond",
            "device": "cpu", "media_type": None, "source_content_id": None,
            "source_relative_path_prefix": None, "time_position_ms_min": None,
            "time_position_ms_max": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def fake_visual_embedder(
        _python: Path, _model_name: str, _model_path: str, _query: str, _device: str
    ) -> tuple[list[float], dict[str, object]]:
        return [1.0, 0.0], {"device": "fake", "openclip_model_load_seconds": 0.0, "openclip_query_embedding_seconds": 0.0}

    @staticmethod
    def fake_text_embedder(
        _model: Path, queries: list[str], _prompt: str, _device: str
    ) -> tuple[list[list[float]], dict[str, object]]:
        vector = [0.0] * 1024
        vector[0] = 1.0
        return [list(vector) for _ in queries], {"text_model_load_seconds": 0.0, "text_query_embedding_seconds": 0.0}

    def test_preflight_proves_dynamic_full_visual_coverage(self) -> None:
        before = hybrid.sha256_file(self.db)
        summary, _runtime = hybrid.build_preflight(self.db, self.config, self.args())
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(summary["visual_unit_count"], 5)
        self.assertEqual(summary["openclip_vector_count"], 5)
        self.assertEqual(summary["text_distinct_visual_unit_count"], 4)
        self.assertTrue(summary["checks"]["all_visual_units_have_openclip_vectors"])
        self.assertEqual(before, hybrid.sha256_file(self.db))

    def test_hybrid_query_scans_all_and_returns_visual_only_unit(self) -> None:
        response, _out = hybrid.execute_query(
            self.db, self.config, self.out, self.args(),
            visual_embedder=self.fake_visual_embedder,
            text_embedder=self.fake_text_embedder,
        )
        self.assertEqual(response["technical_status"], "PASS")
        self.assertEqual(response["scanned_visual_vector_count"], 5)
        self.assertEqual(response["visual_unit_count"], 5)
        visual_only = next(row for row in response["results"] if row["visual_unit_id"] == "v5")
        self.assertFalse(visual_only["text_evidence_present"])
        self.assertFalse(visual_only["yoloe_query_match"])
        self.assertIn("strong_visual_semantic", visual_only["relevance_reasons"])
        self.assertTrue(all("openclip_rank" in row for row in response["results"]))

    def test_media_type_filter_limits_both_scan_and_results(self) -> None:
        response, _out = hybrid.execute_query(
            self.db, self.config, self.out, self.args(media_type="image"),
            visual_embedder=self.fake_visual_embedder,
            text_embedder=self.fake_text_embedder,
        )
        self.assertGreater(response["scanned_visual_vector_count"], 0)
        self.assertTrue(response["results"])
        self.assertTrue(all(row["media_type"] == "image" for row in response["results"]))
        self.assertEqual(
            response["ranking"]["post_temporal_dedup_count_by_media"],
            {"image": response["ranking"]["post_temporal_dedup_result_count"]},
        )

    def test_compound_query_does_not_match_broad_child_yolo_label(self) -> None:
        broad, _labels = hybrid.load_yoloe_evidence(self.db, "绿色人物", {"v5"})
        exact, _labels = hybrid.load_yoloe_evidence(self.db, "人物", {"v5"})
        self.assertNotIn("v5", broad)
        self.assertIn("v5", exact)

    def test_single_cjk_character_requires_isolated_exact_text_evidence(self) -> None:
        self.assertFalse(hybrid.text_has_exact_query("人", "人民公园"))
        self.assertFalse(hybrid.text_has_exact_query("人", "行人在路上"))
        self.assertTrue(hybrid.text_has_exact_query("人", "相邻对象：人（person）"))
        self.assertTrue(hybrid.text_has_exact_query("小麦", "画面里有成熟的小麦田"))

    def test_object_label_requires_independent_semantic_support(self) -> None:
        config = hybrid.load_config(self.config)
        source = {
            "visual_unit_id": "false-positive", "source_content_id": "s1",
            "derived_id": "d1", "media_type": "image",
        }
        labels = {
            "false-positive": [{
                "label": "person", "label_zh": "人", "confidence": 0.9,
                "query_match": True,
            }],
        }
        rows, ranking = hybrid.fuse_results(
            {"false-positive": source},
            {"false-positive": [0.2, 0.0]}, [1.0, 0.0],
            {}, {}, {"false-positive": 0.9}, labels, config,
            self.args(query="人"),
        )
        self.assertEqual(rows, [])
        self.assertEqual(ranking["relevance_rejected_result_count"], 1)

        supported, _ranking = hybrid.fuse_results(
            {"false-positive": source},
            {"false-positive": [0.4, 0.0]}, [1.0, 0.0],
            {}, {}, {"false-positive": 0.9}, labels, config,
            self.args(query="人"),
        )
        self.assertEqual(len(supported), 1)
        self.assertIn("exact_object_label", supported[0]["relevance_reasons"])
        self.assertEqual(supported[0]["matched_object_labels"][0]["label_zh"], "人")
        self.assertGreaterEqual(supported[0]["relevance_score"], 0.0)
        self.assertLessEqual(supported[0]["relevance_score"], 1.0)

    def test_irrelevant_candidates_are_not_used_to_fill_page(self) -> None:
        config = hybrid.load_config(self.config)
        rows, ranking = hybrid.fuse_results(
            {
                "low-image": {"visual_unit_id": "low-image", "source_content_id": "s1", "derived_id": "d1", "media_type": "image"},
                "low-video": {"visual_unit_id": "low-video", "source_content_id": "s2", "derived_id": "d2", "media_type": "video", "time_position_ms": 0},
            },
            {"low-image": [0.1, 0.0], "low-video": [0.05, 0.0]},
            [1.0, 0.0], {}, {}, {}, {}, config,
            self.args(query="完全无关", result_limit=30),
        )
        self.assertEqual(rows, [])
        self.assertEqual(ranking["relevance_eligible_result_count"], 0)
        self.assertEqual(ranking["relevance_rejected_result_count"], 2)

    def test_exact_text_match_survives_relevance_gate_and_reports_pagination(self) -> None:
        config = hybrid.load_config(self.config)
        rows, ranking = hybrid.fuse_results(
            {
                "exact": {"visual_unit_id": "exact", "source_content_id": "s1", "derived_id": "d1", "media_type": "image"},
                "weak": {"visual_unit_id": "weak", "source_content_id": "s2", "derived_id": "d2", "media_type": "image"},
            },
            {"exact": [0.1, 0.0], "weak": [0.05, 0.0]}, [1.0, 0.0],
            {"exact": 0.2, "weak": 0.1},
            {"exact": {"text_semantic_score": 0.2, "text_exact_match": True, "text_preview": "小麦"}},
            {}, {}, config, self.args(query="小麦", result_limit=1),
        )
        self.assertEqual([row["visual_unit_id"] for row in rows], ["exact"])
        self.assertIn("exact_text", rows[0]["relevance_reasons"])
        self.assertEqual(ranking["post_temporal_dedup_result_count"], 1)
        self.assertEqual(ranking["post_temporal_dedup_count_by_media"], {"image": 1})
        self.assertIsNone(ranking["next_result_offset"])

    def test_query_text_is_private_and_assets_are_relative(self) -> None:
        private = "不允许写入报告的查询词"
        response, request_out = hybrid.execute_query(
            self.db, self.config, self.out, self.args(query=private),
            visual_embedder=self.fake_visual_embedder,
            text_embedder=self.fake_text_embedder,
        )
        raw_json = (request_out / "reports/query_response.json").read_text(encoding="utf-8")
        raw_html = (request_out / "reports/query_response.html").read_text(encoding="utf-8")
        self.assertNotIn(private, raw_json)
        self.assertNotIn(private, raw_html)
        self.assertEqual(response["visual_preview"]["preview_asset_missing_count"], 0)
        self.assertEqual(
            response["visual_preview"]["html_img_http_accessible_check_status"],
            "PASS_STATIC_RELATIVE_ASSETS",
        )

    def test_native_app_contract_separates_results_and_skips_preview_copies(self) -> None:
        query = "绿色人物"
        args = self.args(query=query, native_app_result_contract=True)
        response, request_out = hybrid.execute_query(
            self.db, self.config, self.out, args,
            visual_embedder=self.fake_visual_embedder,
            text_embedder=self.fake_text_embedder,
        )
        reports = request_out / "reports"
        result = json.loads((reports / "search_results.json").read_text(encoding="utf-8"))
        summary = json.loads((reports / "search_summary.json").read_text(encoding="utf-8"))

        self.assertEqual(response["technical_status"], "PASS")
        self.assertEqual(result["contract_version"], "media_archive_search_result_v1")
        self.assertEqual(result["query"], query)
        self.assertEqual(result["result_count"], len(result["result_items"]))
        self.assertTrue(result["result_items"])
        self.assertTrue({
            "source_path", "media_type", "preview_path", "time_position_ms",
            "hit_reason", "hit_field", "score", "source_online",
            "can_open_original",
        }.issubset(result["result_items"][0]))
        self.assertGreaterEqual(result["result_items"][0]["score"], 0.0)
        self.assertLessEqual(result["result_items"][0]["score"], 1.0)
        self.assertIn("matched_object_labels", result["result_items"][0])
        self.assertFalse((reports / "query_response.html").exists())
        self.assertFalse((reports / "assets").exists())
        self.assertFalse(summary["html_generated"])
        self.assertFalse(summary["preview_asset_materialization"])

    def test_missing_full_coverage_is_rejected(self) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("DELETE FROM embeddings WHERE visual_unit_id='v5'")
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(RuntimeError, "complete_openclip_run_missing"):
            hybrid.build_preflight(self.db, self.config, self.args())

    def test_real_query_requires_confirmation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
            hybrid.main([
                "--mode", "query", "--db", str(self.db), "--config", str(self.config),
                "--out", str(self.out), "--query", "人物",
                "--openclip-python", str(self.openclip_python),
            ])

    def test_source_and_config_are_generic_and_do_not_read_original_media(self) -> None:
        source = (SCRIPT_DIR / "stop03_5e_hybrid_visual_text_search_v2.py").read_text(encoding="utf-8")
        config = self.config.read_text(encoding="utf-8")
        for forbidden in (
            "stop03_5d_db064", "1628", "758", "407", "/Users/yourname",
            "MEDIA_ARCHIVE_TEST_SOURCE", "VideoCapture(", "requests.", "CREATE VIRTUAL TABLE",
        ):
            self.assertNotIn(forbidden, source)
            self.assertNotIn(forbidden, config)
        self.assertIn("latest_complete_success_openclip_run", config)
        self.assertIn("mode=ro", source)


if __name__ == "__main__":
    unittest.main()
