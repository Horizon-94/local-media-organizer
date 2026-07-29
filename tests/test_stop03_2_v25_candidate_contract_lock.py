from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import stop03_2_v25_candidate_contract_lock as lock  # noqa: E402


class V25CandidateContractLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ROOT / "media_archive.sqlite"
        cls.db_state_before = lock.file_state(cls.db)
        cls.snapshot = lock.build_snapshot(cls.db, created_at="2026-07-11T00:00:00+00:00")

    @classmethod
    def tearDownClass(cls) -> None:
        assert lock.file_state(cls.db) == cls.db_state_before

    def test_real_snapshot_counts_ids_and_runtime_fingerprints(self) -> None:
        summary = self.snapshot["summary"]
        rows = self.snapshot["rows"]
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual((summary["row_count"], summary["qwenvl_count"], summary["ocr_count"]), (390, 336, 54))
        self.assertEqual(summary["runtime_sha_ready_count"], 390)
        self.assertEqual(summary["runtime_sha_mismatch_count"], 0)
        self.assertTrue(all(value == 0 for value in summary["missing_forced_ids"].values()))
        self.assertEqual(len({row["candidate_id"] for row in rows}), 390)
        self.assertTrue(all(Path(row["runtime_visual_file"]).is_file() for row in rows))
        self.assertTrue(all(len(row["runtime_visual_file_sha256"]) == 64 for row in rows))
        self.assertTrue(all(row["yoloe_label_status"] in {"labeled", "no_label"} for row in rows))
        self.assertTrue(all(row["yoloe_labels_json"] == "[]" for row in rows if row["yoloe_label_status"] == "no_label"))

    def test_snapshot_digests_are_deterministic(self) -> None:
        rows = list(reversed(self.snapshot["rows"]))
        ids = sorted(row["candidate_id"] for row in rows)
        id_digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()
        semantics = "\n".join(
            f"{row['candidate_id']}:{row['candidate_semantic_sha256']}"
            for row in sorted(rows, key=lambda item: item["candidate_id"])
        )
        semantic_digest = hashlib.sha256(semantics.encode()).hexdigest()
        self.assertEqual(id_digest, self.snapshot["summary"]["candidate_id_set_sha256"])
        self.assertEqual(semantic_digest, self.snapshot["summary"]["candidate_semantic_digest_sha256"])

    def test_commit_to_temporary_copy_is_idempotent_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_copy = Path(temp_dir) / "contract.sqlite"
            source = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            target = sqlite3.connect(str(db_copy))
            source.backup(target)
            target.close()
            source.close()

            clean = sqlite3.connect(str(db_copy))
            clean.execute("PRAGMA foreign_keys=OFF")
            for trigger in (
                "trg_stop03_2_frozen_v25_no_update",
                "trg_stop03_2_frozen_v25_no_delete",
                "trg_stop03_2_frozen_v25_no_insert_after_lock",
                "trg_pipeline_frozen_contracts_no_update",
                "trg_pipeline_frozen_contracts_no_delete",
            ):
                clean.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            clean.execute("DROP VIEW IF EXISTS v_stop03_2_v25_qwenvl_execution_queue")
            clean.execute("DROP VIEW IF EXISTS v_stop03_2_v25_ocr_execution_queue")
            clean.execute("DROP TABLE IF EXISTS stop03_3_qwenvl_results")
            clean.execute("DROP TABLE IF EXISTS stop03_3_qwenvl_run_items")
            clean.execute("DROP TABLE IF EXISTS stop03_3_qwenvl_runs")
            clean.execute("DROP TABLE IF EXISTS stop03_2_candidate_queue_frozen_v25")
            clean.execute("DROP TABLE IF EXISTS pipeline_frozen_contracts")
            clean.commit()
            clean.close()

            first = lock.apply_snapshot_transaction(db_copy, self.snapshot)
            second = lock.apply_snapshot_transaction(db_copy, self.snapshot)
            self.assertEqual(first["status"], "PASS")
            self.assertFalse(first["idempotent"])
            self.assertEqual(second["status"], "IDEMPOTENT_PASS")
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["readback"]["row_count"], 390)
            self.assertEqual(first["readback"]["qwenvl_count"], 336)
            self.assertEqual(first["readback"]["ocr_count"], 54)
            self.assertEqual(first["candidate_ledger_before"], first["candidate_ledger_after"])
            self.assertEqual(second["candidate_ledger_before"], second["candidate_ledger_after"])

            con = sqlite3.connect(str(db_copy))
            candidate_id = self.snapshot["rows"][0]["candidate_id"]
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE stop03_2_candidate_queue_frozen_v25 SET candidate_score=candidate_score WHERE candidate_id=?",
                    (candidate_id,),
                )
            con.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "DELETE FROM stop03_2_candidate_queue_frozen_v25 WHERE candidate_id=?",
                    (candidate_id,),
                )
            con.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE pipeline_frozen_contracts SET status='FROZEN' WHERE contract_name=?",
                    (lock.CONTRACT_NAME,),
                )
            con.close()

    def test_preflight_and_snapshot_do_not_modify_central_db(self) -> None:
        before = lock.file_state(self.db)
        result = lock.preflight(self.db, lock.DEFAULT_OUT)
        self.assertEqual(result["technical_status"], "PASS")
        self.assertFalse(result["central_db_modified"])
        self.assertEqual(lock.file_state(self.db), before)


if __name__ == "__main__":
    unittest.main()
