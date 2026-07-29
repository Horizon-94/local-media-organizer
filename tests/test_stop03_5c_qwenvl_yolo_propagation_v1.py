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

import stop03_5c_qwenvl_yolo_propagation_v1 as propagation  # noqa: E402
import stop03_5c_semantic_propagation_v1 as legacy_v1  # noqa: E402
import stop03_5c_semantic_propagation_v2_yolo_gate as legacy_v2  # noqa: E402


class Stop035CPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.db = self.root / "central.sqlite"
        self.config = self.root / "config.json"
        self.out = self.root / "out"
        config = json.loads(
            (ROOT / "configs/stop03_5c_qwenvl_yolo_propagation_v1.json")
            .read_text(encoding="utf-8")
        )
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self._create_db()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_db(self) -> None:
        con = sqlite3.connect(str(self.db))
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE stop03_5_unified_evidence_runs(
                staging_run_id TEXT PRIMARY KEY,status TEXT,created_at TEXT,
                qwen_run_id TEXT,ocr_run_id TEXT
            );
            CREATE TABLE stop03_5_unified_evidence_items(
                staging_run_id TEXT,modality TEXT,quality_status TEXT,
                evidence_id TEXT,candidate_id TEXT,source_content_id TEXT,
                visual_unit_id TEXT,canonical_visual_unit_id TEXT,
                derived_id TEXT,evidence_text TEXT,evidence_text_sha256 TEXT
            );
            CREATE TABLE derived_assets(
                derived_id TEXT PRIMARY KEY,source_content_id TEXT,
                derived_type TEXT,frame_index INTEGER,time_position_ms INTEGER,
                sha256 TEXT
            );
            CREATE TABLE visual_units(
                visual_unit_id TEXT PRIMARY KEY,derived_id TEXT
            );
            CREATE TABLE visual_identity(
                visual_unit_id TEXT PRIMARY KEY,canonical_visual_unit_id TEXT
            );
            CREATE TABLE visual_labels(
                visual_unit_id TEXT,label TEXT,confidence REAL
            );
            CREATE TABLE visual_label_terms(
                label TEXT PRIMARY KEY,label_zh TEXT,search_terms_json TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO stop03_5_unified_evidence_runs VALUES(?,?,?,?,?)",
            ("old", "success", "2026-01-01", "q-old", "o-old"),
        )
        con.execute(
            "INSERT INTO stop03_5_unified_evidence_runs VALUES(?,?,?,?,?)",
            ("latest", "success", "2026-02-01", "q-new", "o-new"),
        )
        con.executemany(
            "INSERT INTO visual_label_terms VALUES(?,?,?)",
            [
                ("person", "人", '["person","人物","行人","人"]'),
                ("car", "车 / 小汽车", '["car","汽车","车辆","车"]'),
                ("chair", "椅子", '["chair","椅子"]'),
            ],
        )
        for index in range(1, 9):
            derived = f"d{index}"
            visual = f"v{index}"
            con.execute(
                "INSERT INTO derived_assets VALUES(?,?,?,?,?,?)",
                (derived, "source1", "video_frame_jpg1280", index, index * 1000, f"s{index}"),
            )
            con.execute("INSERT INTO visual_units VALUES(?,?)", (visual, derived))
            con.execute("INSERT INTO visual_identity VALUES(?,?)", (visual, visual))
            con.executemany(
                "INSERT INTO visual_labels VALUES(?,?,?)",
                [(visual, "person", 0.8), (visual, "car", 0.8)],
            )
        con.execute("INSERT INTO visual_units VALUES('v4_alias','d4')")
        con.execute("INSERT INTO visual_identity VALUES('v4_alias','v4')")
        con.executemany(
            "INSERT INTO visual_labels VALUES(?,?,?)",
            [("v4_alias", "person", 0.9), ("v4_alias", "car", 0.9)],
        )
        text = "1）概括：人物站在汽车旁边。2）元素：人物、汽车和椅子。3）检索价值：人物与车辆。"
        con.execute(
            """INSERT INTO stop03_5_unified_evidence_items
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "latest", "qwenvl", "PASS", "e1", "c1", "source1",
                "v4", "v4", "d4", text, propagation.sha256_text(text),
            ),
        )
        con.execute(
            """INSERT INTO stop03_5_unified_evidence_items
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "old", "qwenvl", "PASS", "old_e", "old_c", "source1",
                "v2", "v2", "d2", text, propagation.sha256_text(text),
            ),
        )
        con.execute(
            """INSERT INTO stop03_5_unified_evidence_items
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "latest", "ocr", "PASS", "ocr_e", "ocr_c", "source1",
                "v5", "v5", "d5", "OCR文字", propagation.sha256_text("OCR文字"),
            ),
        )
        con.commit()
        con.close()

    def test_latest_run_radius_three_and_ocr_excluded(self) -> None:
        summary, rows, targets = propagation.build_distribution(
            self.db, self.config
        )
        self.assertEqual(summary["source_staging_run_id"], "latest")
        self.assertEqual(summary["source_qwen_video_anchor_count"], 1)
        self.assertEqual(summary["source_ocr_anchor_count"], 0)
        self.assertEqual({row["propagation_step"] for row in rows}, {1, 2, 3})
        self.assertEqual(
            {row["target_derived_id"] for row in rows},
            {"d1", "d2", "d3", "d5", "d6", "d7"},
        )
        self.assertEqual(len(targets), 6)

    def test_three_way_intersection_and_no_full_text_copy(self) -> None:
        _summary, rows, _targets = propagation.build_distribution(
            self.db, self.config
        )
        self.assertEqual(
            {row["propagated_label"] for row in rows},
            {"person", "car"},
        )
        self.assertTrue(all("椅子" not in row["propagated_text"] for row in rows))
        self.assertTrue(
            all(
                row["propagated_text"].startswith("相邻高价值帧传播对象：")
                for row in rows
            )
        )

    def test_visual_alias_is_collapsed_before_neighbor_counting(self) -> None:
        summary, rows, _targets = propagation.build_distribution(
            self.db, self.config
        )
        self.assertEqual(summary["unique_video_frame_count"], 8)
        self.assertEqual(summary["visual_unit_aliases_collapsed_count"], 1)
        self.assertFalse(any(row["target_derived_id"] == "d4" for row in rows))

    def test_target_with_direct_qwen_is_recorded_without_override(self) -> None:
        con = sqlite3.connect(str(self.db))
        text = "人物在画面中。"
        con.execute(
            """INSERT INTO stop03_5_unified_evidence_items
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "latest", "qwenvl", "PASS", "e2", "c2", "source1",
                "v5", "v5", "d5", text, propagation.sha256_text(text),
            ),
        )
        con.commit()
        con.close()
        _summary, rows, _targets = propagation.build_distribution(
            self.db, self.config
        )
        self.assertTrue(
            any(
                row["target_derived_id"] == "d5"
                and row["target_has_direct_qwenvl"]
                for row in rows
            )
        )

    def test_dry_run_does_not_modify_database(self) -> None:
        before = propagation.sha256_file(self.db)
        code = propagation.main(
            [
                "--mode", "dry-run",
                "--db", str(self.db),
                "--config", str(self.config),
                "--out", str(self.out),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(before, propagation.sha256_file(self.db))
        self.assertTrue(
            (self.out / "reports/stop03_5c_summary.json").is_file()
        )

    def test_legacy_command_interfaces_are_retired(self) -> None:
        with self.assertRaisesRegex(SystemExit, "RETIRED_STOP03_5C_INTERFACE"):
            legacy_v1.main()
        with self.assertRaisesRegex(SystemExit, "RETIRED_STOP03_5C_INTERFACE"):
            legacy_v2.main([])

    def test_commit_is_atomic_and_idempotent(self) -> None:
        summary, rows, _targets = propagation.build_distribution(
            self.db, self.config
        )
        migration = (
            ROOT
            / "migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql"
        )
        first = propagation.commit(
            self.db, migration, self.out, summary, rows
        )
        second = propagation.commit(
            self.db, migration, self.out, summary, rows
        )
        self.assertEqual(first["commit_status"], "COMMITTED")
        self.assertEqual(second["commit_status"], "IDEMPOTENT_PASS")
        self.assertEqual(second["propagation_row_count"], len(rows))
        self.assertEqual(second["duplicate_propagation_id_count"], 0)
        self.assertEqual(second["duplicate_semantic_count"], 0)
        self.assertEqual(second["ocr_source_count"], 0)
        self.assertEqual(second["latest_view_count"], len(rows))

    def test_cli_commit_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "stop03_5c_commit_confirmation_required"
        ):
            propagation.main(
                [
                    "--mode", "commit",
                    "--db", str(self.db),
                    "--config", str(self.config),
                    "--out", str(self.out),
                ]
            )


if __name__ == "__main__":
    unittest.main()
