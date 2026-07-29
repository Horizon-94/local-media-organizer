from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5b_unified_evidence_staging_v1 as staging  # noqa: E402
from tests import test_stop03_5a_joint_db_quality_audit_v1 as fixture_module  # noqa: E402


class Stop035BUnifiedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.Stop035AJointAuditTests()
        self.fixture.setUp()
        self.db = self.fixture.db
        self.out = self.fixture.root / "staging"
        self.config = self.fixture.root / "staging_config.json"
        self.config.write_text(
            """{
              "contract_version": "stop03_5b_unified_evidence_staging_v1",
              "quality_audit_config": "%s",
              "modalities": ["qwenvl", "ocr"],
              "allow_quality_review": true,
              "source_policy": "central_db_complete_runs_and_existing_output_reports_only",
              "original_video_read": false,
              "model_run": false,
              "network_used": false,
              "download_used": false
            }""" % self.fixture.config,
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_build_is_dynamic_and_propagates_review(self) -> None:
        summary, rows = staging.build_rows(self.db, self.config)
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(summary["evidence_count"], 2)
        self.assertEqual(summary["qwen_count"], 1)
        self.assertEqual(summary["ocr_count"], 1)
        self.assertEqual(summary["review_count"], 1)
        self.assertEqual(len({row["evidence_id"] for row in rows}), 2)
        self.assertEqual(
            next(row for row in rows if row["modality"] == "ocr")["quality_status"],
            "REVIEW",
        )

    def test_dry_run_writes_only_output(self) -> None:
        before = staging.quality.sha256_file(self.db)
        code = staging.main(
            [
                "--mode",
                "dry-run",
                "--db",
                str(self.db),
                "--config",
                str(self.config),
                "--out",
                str(self.out),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(before, staging.quality.sha256_file(self.db))
        self.assertTrue((self.out / "reports/stop03_5b_summary.json").is_file())
        self.assertEqual(
            len(
                (self.out / "manifests/unified_evidence.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            2,
        )

    def test_commit_is_atomic_and_idempotent_on_fixture_database(self) -> None:
        summary, rows = staging.build_rows(self.db, self.config)
        first = staging.commit(
            self.db,
            ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql",
            self.out,
            summary,
            rows,
        )
        second = staging.commit(
            self.db,
            ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql",
            self.out,
            summary,
            rows,
        )
        self.assertEqual(first["commit_status"], "COMMITTED")
        self.assertEqual(second["commit_status"], "IDEMPOTENT_PASS")
        con = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM stop03_5_unified_evidence_items"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                con.execute(
                    """SELECT COUNT(*) FROM (
                       SELECT staging_run_id,evidence_id,COUNT(*) n
                       FROM stop03_5_unified_evidence_items
                       GROUP BY staging_run_id,evidence_id HAVING n>1)"""
                ).fetchone()[0],
                0,
            )
            primary_key_columns = [
                name
                for _position, name in sorted(
                    (row[5], row[1])
                    for row in con.execute(
                        "PRAGMA table_info(stop03_5_unified_evidence_items)"
                    )
                    if row[5]
                )
            ]
            self.assertEqual(
                primary_key_columns,
                ["staging_run_id", "staging_item_id"],
            )
        finally:
            con.close()

    def test_commit_requires_explicit_confirmation_at_cli(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "commit_confirmation_required"):
            staging.main(
                [
                    "--mode",
                    "commit",
                    "--db",
                    str(self.db),
                    "--config",
                    str(self.config),
                    "--out",
                    str(self.out),
                ]
            )

    def test_source_has_no_model_network_or_original_video_execution(self) -> None:
        source = (
            ROOT
            / "scripts/03_stop03_visual_analysis/"
            "stop03_5b_unified_evidence_staging_v1.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("VideoCapture(", source)
        self.assertNotIn("mlx_vlm", source)
        self.assertNotIn("PaddleOCR", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("urllib.", source)


if __name__ == "__main__":
    unittest.main()
