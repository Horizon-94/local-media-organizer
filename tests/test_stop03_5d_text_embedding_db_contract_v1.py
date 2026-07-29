from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as contract  # noqa: E402


class Stop035DTextEmbeddingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.db = self.root / "central.sqlite"
        self.out = self.root / "out"
        self.model = self.root / "model"
        self.python = self.root / "python"
        self.config = self.root / "config.json"
        self._create_model_fixture()
        self._create_db()
        config = json.loads(
            (
                ROOT
                / "configs/stop03_5d_text_embedding_db_contract_v1.json"
            ).read_text(encoding="utf-8")
        )
        config["model_path"] = str(self.model)
        config["python_path"] = str(self.python)
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_model_fixture(self) -> None:
        (self.model / "1_Pooling").mkdir(parents=True)
        for relative in contract.MODEL_ASSET_FILES:
            path = self.model / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture" if path.name == "model.safetensors" else b"{}")
        self.python.write_text("# fixture", encoding="utf-8")

    def _create_db(self) -> None:
        con = sqlite3.connect(str(self.db))
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE source_assets(
                source_content_id TEXT PRIMARY KEY,
                relative_path TEXT,
                media_type TEXT
            );
            CREATE TABLE derived_assets(
                derived_id TEXT PRIMARY KEY,
                source_content_id TEXT,
                derived_type TEXT,
                frame_index INTEGER,
                time_position_ms INTEGER
            );
            CREATE TABLE visual_units(
                visual_unit_id TEXT PRIMARY KEY,
                derived_id TEXT
            );
            CREATE TABLE stop03_5_unified_evidence_runs(
                staging_run_id TEXT PRIMARY KEY,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE stop03_5_unified_evidence_items(
                staging_run_id TEXT,
                modality TEXT,
                quality_status TEXT,
                evidence_id TEXT,
                candidate_id TEXT,
                source_content_id TEXT,
                canonical_visual_unit_id TEXT,
                derived_id TEXT,
                evidence_text TEXT
            );
            CREATE TABLE stop03_5c_propagation_runs(
                propagation_run_id TEXT PRIMARY KEY,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE stop03_5c_propagation_items(
                propagation_run_id TEXT,
                propagation_id TEXT,
                source_content_id TEXT,
                target_canonical_visual_unit_id TEXT,
                target_derived_id TEXT,
                target_frame_index INTEGER,
                target_time_position_ms INTEGER,
                propagated_label TEXT,
                propagated_label_zh TEXT
            );
            CREATE VIEW v_stop03_5_latest_unified_evidence AS
            SELECT i.* FROM stop03_5_unified_evidence_items i
            WHERE i.staging_run_id='s-new';
            CREATE VIEW v_stop03_5c_latest_propagation AS
            SELECT i.* FROM stop03_5c_propagation_items i
            WHERE i.propagation_run_id='p-new';
            """
        )
        con.executemany(
            "INSERT INTO source_assets VALUES(?,?,?)",
            [
                ("source1", "video1.mov", "video"),
                ("source2", "image1.jpg", "image"),
            ],
        )
        rows = [
            ("d1", "source1", "video_frame_jpg1280", 1, 1000),
            ("d2", "source1", "video_frame_jpg1280", 2, 2000),
            ("d3", "source2", "image_preview_jpg1280", -1, -1),
            ("d4", "source1", "video_frame_jpg1280", 4, 4000),
        ]
        con.executemany("INSERT INTO derived_assets VALUES(?,?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO visual_units VALUES(?,?)",
            [(f"v{i}", f"d{i}") for i in range(1, 5)],
        )
        con.executemany(
            "INSERT INTO stop03_5_unified_evidence_runs VALUES(?,?,?)",
            [("s-old", "success", "2026-01-01"), ("s-new", "success", "2026-02-01")],
        )
        con.executemany(
            """INSERT INTO stop03_5_unified_evidence_items
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                ("s-new", "qwenvl", "PASS", "e1", "c1", "source1", "v1", "d1", "人物在车旁"),
                ("s-new", "ocr", "PASS", "e2", "c2", "source1", "v1", "d1", "商店招牌"),
                ("s-new", "ocr", "REVIEW", "e3", "c3", "source1", "v2", "d2", "弱文字"),
                ("s-new", "qwenvl", "PASS", "e4", "c4", "source2", "v3", "d3", "一张照片"),
            ],
        )
        con.executemany(
            "INSERT INTO stop03_5c_propagation_runs VALUES(?,?,?)",
            [("p-old", "success", "2026-01-01"), ("p-new", "success", "2026-02-01")],
        )
        con.executemany(
            """INSERT INTO stop03_5c_propagation_items
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                ("p-new", "p1", "source1", "v1", "d1", 1, 1000, "person", "人"),
                ("p-new", "p2", "source1", "v2", "d2", 2, 2000, "person", "人"),
                ("p-new", "p3", "source1", "v4", "d4", 4, 4000, "person", "人"),
            ],
        )
        con.commit()
        con.close()

    def test_merges_per_derived_and_excludes_review(self) -> None:
        summary, documents, jobs, excluded = contract.build_documents(
            self.db, self.config
        )
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(summary["document_count"], 4)
        self.assertEqual(summary["direct_only_count"], 1)
        self.assertEqual(summary["propagation_only_count"], 2)
        self.assertEqual(summary["direct_and_propagation_count"], 1)
        self.assertEqual(summary["direct_review_excluded_count"], 1)
        self.assertEqual(len(excluded), 1)
        d1 = next(row for row in documents if row["derived_id"] == "d1")
        self.assertIn("人物在车旁", d1["embedding_text"])
        self.assertIn("商店招牌", d1["embedding_text"])
        self.assertIn("人（person）", d1["embedding_text"])
        d2 = next(row for row in documents if row["derived_id"] == "d2")
        self.assertNotIn("弱文字", d2["embedding_text"])
        self.assertEqual(len(jobs), 3)
        self.assertEqual(summary["reused_document_count"], 1)

    def test_identical_propagation_text_reuses_one_job(self) -> None:
        _summary, documents, jobs, _excluded = contract.build_documents(
            self.db, self.config
        )
        d2 = next(row for row in documents if row["derived_id"] == "d2")
        d4 = next(row for row in documents if row["derived_id"] == "d4")
        self.assertEqual(d2["embedding_text_sha256"], d4["embedding_text_sha256"])
        self.assertEqual(d2["text_vector_id"], d4["text_vector_id"])
        job = next(row for row in jobs if row["text_vector_id"] == d2["text_vector_id"])
        self.assertEqual(job["document_count"], 2)

    def test_dry_run_writes_only_output(self) -> None:
        before = contract.sha256_file(self.db)
        code = contract.main(
            [
                "--mode", "dry-run",
                "--db", str(self.db),
                "--config", str(self.config),
                "--out", str(self.out),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(before, contract.sha256_file(self.db))
        self.assertTrue((self.out / "reports/stop03_5d_summary.json").is_file())
        self.assertEqual(
            len(
                (self.out / "manifests/text_documents.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            4,
        )
        self.assertEqual(
            len(
                (self.out / "manifests/unique_text_jobs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            3,
        )

    def test_migration_creates_blob_and_reuse_contract(self) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(
                (
                    ROOT
                    / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"
                ).read_text(encoding="utf-8")
            )
            columns = {
                row[1]
                for row in con.execute("PRAGMA table_info(stop03_5d_text_vectors)")
            }
            self.assertIn("vector_blob", columns)
            self.assertIn("embedding_text_sha256", columns)
            run_columns = {
                row[1]
                for row in con.execute(
                    "PRAGMA table_info(stop03_5d_text_embedding_runs)"
                )
            }
            self.assertIn("run_payload_digest_sha256", run_columns)
            self.assertIsNotNone(
                con.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE name='stop03_5d_document_vector_links'"""
                ).fetchone()
            )
        finally:
            con.close()

    def test_model_identity_changes_run_not_document_identity(self) -> None:
        first, first_docs, _jobs, _excluded = contract.build_documents(
            self.db, self.config
        )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["model_name"] = "fixture-model-v2"
        second_config = self.root / "config_v2.json"
        second_config.write_text(json.dumps(config), encoding="utf-8")
        second, second_docs, _jobs, _excluded = contract.build_documents(
            self.db, second_config
        )
        self.assertEqual(
            [row["document_id"] for row in first_docs],
            [row["document_id"] for row in second_docs],
        )
        self.assertNotEqual(
            first["run_payload_digest_sha256"],
            second["run_payload_digest_sha256"],
        )
        self.assertNotEqual(
            first["planned_embedding_run_id"],
            second["planned_embedding_run_id"],
        )

    def test_source_does_not_load_model_or_use_network_or_video(self) -> None:
        source = (
            ROOT
            / "scripts/03_stop03_visual_analysis/"
            "stop03_5d_text_embedding_db_contract_v1.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("SentenceTransformer", source)
        self.assertNotIn("AutoModel", source)
        self.assertNotIn("torch.", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("urllib.", source)
        self.assertNotIn("VideoCapture(", source)
        for fixed_count in ("336", "390", "623", "758"):
            self.assertNotIn(fixed_count, source)


if __name__ == "__main__":
    unittest.main()
