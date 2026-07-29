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

import stop03_5a_joint_db_quality_audit_v1 as audit  # noqa: E402
import stop03_5a_joint_db_quality_audit_node_v1 as frozen_node  # noqa: E402


QWEN_TEXT = (
    "1）概括：画面展示城市街道和店铺。\n"
    "2）元素：人物：行人；物体：车辆和招牌；场景：街道；动作：行走；"
    "环境：白天；文字区域：店铺招牌。\n"
    "3）检索价值：适合使用城市街景、商店、车辆和行人等关键词检索。"
)


class Stop035AJointAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "central.sqlite"
        self.config = self.root / "config.json"
        self.out = self.root / "out"
        self._create_db()
        self.config.write_text(
            json.dumps(
                {
                    "contract_version": audit.CONTRACT_VERSION,
                    "qwen_run_selector": "latest_complete_for_current_queue",
                    "ocr_run_selector": "latest_complete_full_for_current_queue",
                    "qwen_required_sections": ["1）概括", "2）元素", "3）检索价值"],
                    "qwen_min_text_chars_review": 80,
                    "qwen_max_text_chars_review": 2000,
                    "ocr_min_text_chars_review": 4,
                    "ocr_mean_confidence_review_threshold": 0.8,
                    "allow_quality_review_for_staging": True,
                    "source_policy": "central_db_and_existing_output_reports_only",
                    "original_video_read": False,
                    "model_run": False,
                    "database_write": False,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _file(self, name: str, content: str) -> tuple[str, str]:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return str(path), audit.sha256_file(path)

    def _create_db(self) -> None:
        raw_path, raw_sha = self._file("raw.txt", "raw")
        stderr_path, stderr_sha = self._file("stderr.txt", "")
        metrics_path, metrics_sha = self._file("metrics.json", "{}")
        ocr_output_path, ocr_output_sha = self._file("ocr.json", "{}")
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE stop03_3_qwenvl_runs(
                    run_id TEXT PRIMARY KEY,status TEXT,candidate_count INTEGER,
                    pending_count INTEGER,success_count INTEGER,
                    failed_count INTEGER,review_count INTEGER,started_at TEXT
                );
                CREATE TABLE stop03_3_qwenvl_run_items(
                    run_item_id TEXT PRIMARY KEY,run_id TEXT,candidate_id TEXT,
                    source_content_id TEXT,visual_unit_id TEXT,
                    canonical_visual_unit_id TEXT,derived_id TEXT,
                    candidate_role TEXT,reason_codes TEXT,policy_version TEXT,
                    runtime_visual_file TEXT,runtime_visual_file_sha256 TEXT,
                    status TEXT,attempt_count INTEGER
                );
                CREATE TABLE stop03_3_qwenvl_results(
                    result_id TEXT PRIMARY KEY,run_id TEXT,run_item_id TEXT,
                    candidate_id TEXT,execution_key TEXT,evidence_id TEXT,
                    source_content_id TEXT,visual_unit_id TEXT,
                    canonical_visual_unit_id TEXT,derived_id TEXT,
                    candidate_role TEXT,reason_codes TEXT,policy_version TEXT,
                    result_status TEXT,clean_text TEXT,qwen_text_preview TEXT,
                    clean_text_sha256 TEXT,raw_stdout_path TEXT,
                    raw_stdout_sha256 TEXT,stderr_path TEXT,stderr_sha256 TEXT,
                    metrics_path TEXT,metrics_sha256 TEXT,runtime_metrics_json TEXT,
                    prompt_tokens INTEGER,generation_tokens INTEGER,
                    peak_memory_gb REAL,finish_reason TEXT,truncation_status TEXT,
                    cleanup_status TEXT,cleanup_warnings TEXT,
                    output_contract_version TEXT,runtime_visual_file_sha256 TEXT,
                    model_sha256 TEXT,model_config_sha256 TEXT,
                    model_tokenizer_files_json TEXT,
                    model_tokenizer_files_sha256 TEXT,model_inventory_sha256 TEXT,
                    model_fingerprint_sha256 TEXT,prompt_sha256 TEXT,
                    orchestrator_config_sha256 TEXT,script_sha256 TEXT,
                    created_at TEXT
                );
                CREATE TABLE stop03_4_ocr_runs(
                    run_id TEXT PRIMARY KEY,run_kind TEXT,status TEXT,
                    candidate_count INTEGER,pending_count INTEGER,
                    running_count INTEGER,success_count INTEGER,
                    no_text_count INTEGER,failed_count INTEGER,started_at TEXT
                );
                CREATE TABLE stop03_4_ocr_run_items(
                    run_item_id TEXT PRIMARY KEY,run_id TEXT,candidate_id TEXT,
                    result_id TEXT,source_content_id TEXT,visual_unit_id TEXT,
                    canonical_visual_unit_id TEXT,derived_id TEXT,
                    candidate_role TEXT,reason_codes TEXT,policy_version TEXT,
                    runtime_visual_file TEXT,runtime_visual_file_sha256 TEXT,
                    status TEXT,attempt_count INTEGER,reused_existing_result INTEGER
                );
                CREATE TABLE stop03_4_ocr_results(
                    result_id TEXT PRIMARY KEY,execution_key TEXT,candidate_id TEXT,
                    evidence_id TEXT,result_status TEXT,source_content_id TEXT,
                    visual_unit_id TEXT,canonical_visual_unit_id TEXT,
                    derived_id TEXT,candidate_role TEXT,reason_codes TEXT,
                    policy_version TEXT,media_type TEXT,time_position_ms INTEGER,
                    runtime_visual_file TEXT,runtime_visual_file_sha256 TEXT,
                    ocr_text TEXT,ocr_text_preview TEXT,ocr_text_sha256 TEXT,
                    ocr_lines_json TEXT,ocr_line_count INTEGER,
                    mean_confidence REAL,min_confidence REAL,max_confidence REAL,
                    output_json_path TEXT,output_json_sha256 TEXT,
                    elapsed_seconds REAL,worker_pid INTEGER,ocr_api_used TEXT,
                    detection_model_sha256 TEXT,recognition_model_sha256 TEXT,
                    model_fingerprint_sha256 TEXT,config_sha256 TEXT,
                    script_sha256 TEXT,contract_version TEXT,created_at TEXT
                );
                CREATE TABLE qwen_queue(
                    candidate_id TEXT,source_content_id TEXT,visual_unit_id TEXT,
                    canonical_visual_unit_id TEXT,derived_id TEXT,
                    candidate_role TEXT,reason_codes TEXT,policy_version TEXT,
                    runtime_visual_file_sha256 TEXT
                );
                CREATE TABLE ocr_queue(
                    candidate_id TEXT,source_content_id TEXT,visual_unit_id TEXT,
                    canonical_visual_unit_id TEXT,derived_id TEXT,
                    candidate_role TEXT,reason_codes TEXT,policy_version TEXT,
                    runtime_visual_file_sha256 TEXT
                );
                CREATE VIEW v_stop03_2_v25_qwenvl_execution_queue AS
                    SELECT * FROM qwen_queue;
                CREATE VIEW v_stop03_2_v25_ocr_execution_queue AS
                    SELECT * FROM ocr_queue;
                """
            )
            con.execute(
                """INSERT INTO stop03_3_qwenvl_runs VALUES(
                   'qwen_full','success',1,0,1,0,0,'2026-07-16T02:00:00Z')"""
            )
            con.execute(
                """INSERT INTO stop03_3_qwenvl_runs VALUES(
                   'qwen_old','success',1,0,1,0,0,'2026-07-16T01:00:00Z')"""
            )
            con.execute(
                """INSERT INTO stop03_3_qwenvl_runs VALUES(
                   'qwen_new_incomplete','partial',1,0,0,1,0,
                   '2026-07-16T03:00:00Z')"""
            )
            con.execute(
                """INSERT INTO stop03_4_ocr_runs VALUES(
                   'ocr_full','full','success',1,0,0,1,0,0,
                   '2026-07-16T02:00:00Z')"""
            )
            con.execute(
                """INSERT INTO stop03_4_ocr_runs VALUES(
                   'ocr_smoke_new','smoke','success',1,0,0,1,0,0,
                   '2026-07-16T03:00:00Z')"""
            )
            lineage = (
                "source_1",
                "visual_shared",
                "visual_shared",
                "derived_1",
            )
            con.execute(
                """INSERT INTO qwen_queue VALUES(
                   'q_candidate',?,?,?,?,?,?,?,'image_sha')""",
                (*lineage, "qwen_role", "q_reason", "v25"),
            )
            con.execute(
                """INSERT INTO ocr_queue VALUES(
                   'o_candidate',?,?,?,?,?,?,?,'image_sha')""",
                (*lineage, "ocr_role", "o_reason", "v25"),
            )
            con.execute(
                """INSERT INTO stop03_3_qwenvl_run_items VALUES(
                   'q_item','qwen_full','q_candidate',?,?,?,?,?,?,?,
                   '/derived/frame.jpg','image_sha','success',1)""",
                (*lineage, "qwen_role", "q_reason", "v25"),
            )
            q_values = (
                "q_result",
                "qwen_full",
                "q_item",
                "q_candidate",
                "q_execution",
                "q_evidence",
                *lineage,
                "qwen_role",
                "q_reason",
                "v25",
                "success",
                QWEN_TEXT,
                QWEN_TEXT[:50],
                audit.sha256_text(QWEN_TEXT),
                raw_path,
                raw_sha,
                stderr_path,
                stderr_sha,
                metrics_path,
                metrics_sha,
                "{}",
                10,
                100,
                1.0,
                "stop",
                "complete",
                "ok",
                "",
                "v2",
                "image_sha",
                "m",
                "mc",
                "[]",
                "mt",
                "mi",
                "mf",
                "p",
                "c",
                "s",
                "now",
            )
            con.execute(
                "INSERT INTO stop03_3_qwenvl_results VALUES("
                + ",".join("?" for _ in q_values)
                + ")",
                q_values,
            )
            old_values = list(q_values)
            old_values[0] = "q_old_result"
            old_values[1] = "qwen_old"
            old_values[2] = "q_old_item"
            old_values[3] = "q_old_candidate"
            old_values[4] = "q_old_execution"
            old_values[5] = "q_old_evidence"
            con.execute(
                "INSERT INTO stop03_3_qwenvl_results VALUES("
                + ",".join("?" for _ in old_values)
                + ")",
                old_values,
            )
            con.execute(
                """INSERT INTO stop03_4_ocr_run_items VALUES(
                   'o_item','ocr_full','o_candidate','o_result',?,?,?,?,?,?,?,
                   '/derived/frame.jpg','image_sha','success',1,0)""",
                (*lineage, "ocr_role", "o_reason", "v25"),
            )
            ocr_text = "MISS DIOR 上海展览"
            lines = [{"text": ocr_text, "confidence": 0.7, "box": []}]
            o_values = (
                "o_result",
                "o_execution",
                "o_candidate",
                "o_evidence",
                "success",
                *lineage,
                "ocr_role",
                "o_reason",
                "v25",
                "video",
                1000,
                "/derived/frame.jpg",
                "image_sha",
                ocr_text,
                ocr_text,
                audit.sha256_text(ocr_text),
                json.dumps(lines),
                1,
                0.7,
                0.7,
                0.7,
                ocr_output_path,
                ocr_output_sha,
                1.0,
                123,
                "fake",
                "det",
                "rec",
                "model",
                "cfg",
                "script",
                "v1",
                "now",
            )
            con.execute(
                "INSERT INTO stop03_4_ocr_results VALUES("
                + ",".join("?" for _ in o_values)
                + ")",
                o_values,
            )
            con.commit()
        finally:
            con.close()

    def test_joint_audit_excludes_history_and_allows_flagged_staging(self) -> None:
        before = audit.sha256_file(self.db)
        summary, details = audit.run_audit(self.db, self.config)
        after = audit.sha256_file(self.db)
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(summary["policy_status"], "REVIEW")
        self.assertEqual(summary["staging_readiness"], "READY_WITH_QUALITY_FLAGS")
        self.assertEqual(summary["qwen_history_result_rows_excluded"], 1)
        self.assertEqual(summary["qwen_selected_result_count"], 1)
        self.assertEqual(summary["ocr_selected_result_count"], 1)
        self.assertEqual(summary["cross_modal_visual_overlap_count"], 1)
        self.assertEqual(summary["cross_modal_visual_overlap_fail_count"], 0)
        self.assertEqual(len(details["review"]), 1)
        self.assertEqual(before, after)

    def test_text_sha_mismatch_is_hard_failure(self) -> None:
        con = sqlite3.connect(str(self.db))
        con.execute(
            "UPDATE stop03_3_qwenvl_results SET clean_text_sha256='bad' "
            "WHERE run_id='qwen_full'"
        )
        con.commit()
        con.close()
        summary, _details = audit.run_audit(self.db, self.config)
        self.assertEqual(summary["technical_status"], "FAIL")
        self.assertEqual(summary["staging_readiness"], "DO_NOT_STAGE")

    def test_cross_modal_sha_mismatch_is_hard_failure(self) -> None:
        con = sqlite3.connect(str(self.db))
        con.execute(
            "UPDATE stop03_4_ocr_results SET runtime_visual_file_sha256='other'"
        )
        con.execute("UPDATE ocr_queue SET runtime_visual_file_sha256='other'")
        con.commit()
        con.close()
        summary, _details = audit.run_audit(self.db, self.config)
        self.assertEqual(summary["cross_modal_visual_overlap_fail_count"], 1)
        self.assertEqual(summary["technical_status"], "FAIL")

    def test_preflight_does_not_create_output(self) -> None:
        report = audit.preflight(self.db, self.config)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["qwen_acceptance_run_id"], "qwen_full")
        self.assertEqual(report["ocr_acceptance_run_id"], "ocr_full")
        self.assertFalse(self.out.exists())

    def test_frozen_node_verifies_formal_database(self) -> None:
        report = frozen_node.verify_node(ROOT / "media_archive.sqlite")
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(all(report["generic_invariant_checks"].values()))
        self.assertIn(
            report["audit_summary"]["staging_readiness"],
            {"READY", "READY_WITH_QUALITY_FLAGS"},
        )
        self.assertEqual(
            len(report["review_candidate_ids"]),
            report["audit_summary"]["quality_review_item_count"],
        )


if __name__ == "__main__":
    unittest.main()
