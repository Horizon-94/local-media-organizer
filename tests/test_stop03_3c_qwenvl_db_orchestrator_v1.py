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

import qwenvl_output_contract_v2 as output_contract  # noqa: E402
import stop03_2_v25_candidate_contract_lock as lock  # noqa: E402
import stop03_3c_qwenvl_db_orchestrator_v1 as orch  # noqa: E402


class QwenVLDBOrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.central = ROOT / "media_archive.sqlite"
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_copy = Path(cls.temp.name) / "orchestrator.sqlite"
        source = sqlite3.connect(f"file:{cls.central}?mode=ro", uri=True)
        target = sqlite3.connect(str(cls.db_copy))
        source.backup(target)
        target.close()
        source.close()
        cls.snapshot = lock.build_snapshot(cls.central, created_at="2026-07-11T00:00:00+00:00")
        lock.apply_snapshot_transaction(cls.db_copy, cls.snapshot)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_committed_view_has_336_forced_id_complete_rows(self) -> None:
        con, source = orch.queue_connection(self.db_copy, allow_simulation=False)
        try:
            rows = orch.load_queue(con)
            audit = orch.validate_queue(rows, verify_runtime_sha=False)
        finally:
            con.close()
        self.assertEqual(source, "central_db_view")
        self.assertEqual(len(rows), 336)
        self.assertEqual(audit["status"], "PASS")
        self.assertTrue(all(value == 0 for value in audit["missing_forced_ids"].values()))

    def test_default_384_and_contract_v2_are_locked(self) -> None:
        config = orch.load_config(ROOT / "configs/stop03_3_qwenvl_db_v1.json")
        self.assertEqual(config["default_max_tokens"], 384)
        self.assertEqual(config["temperature"], 0.0)
        self.assertEqual(config["top_p"], 1.0)
        self.assertEqual(config["output_contract_version"], "qwenvl_output_contract_v2.0")
        self.assertEqual(output_contract.RECOMMENDED_MAX_TOKENS, 384)

    def test_low_token_production_and_unapproved_smoke_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "production_max_tokens_below_384"):
            orch.validate_generation_settings("run", 180, False)
        with self.assertRaisesRegex(RuntimeError, "low_token_smoke_requires"):
            orch.validate_generation_settings("smoke", 180, False)
        orch.validate_generation_settings("smoke", 180, True)

    def test_execution_key_changes_with_input_sha(self) -> None:
        row = {
            "candidate_id": "candidate",
            "runtime_visual_file_sha256": "a" * 64,
        }
        first = orch.execution_key(row, "m" * 64, "p" * 64, output_contract.CONTRACT_VERSION, 384)
        row["runtime_visual_file_sha256"] = "b" * 64
        second = orch.execution_key(row, "m" * 64, "p" * 64, output_contract.CONTRACT_VERSION, 384)
        self.assertNotEqual(first, second)

    def test_registered_model_has_complete_strong_fingerprint(self) -> None:
        config = orch.load_config(ROOT / "configs/stop03_3_qwenvl_db_v1.json")
        fingerprint = orch.model_fingerprint(Path(config["model_path"]), config)
        self.assertEqual(fingerprint["missing_model_fingerprint_files"], [])
        self.assertEqual(len(fingerprint["model_weight_sha256"]), 64)
        self.assertEqual(len(fingerprint["model_config_sha256"]), 64)
        self.assertEqual(len(fingerprint["model_tokenizer_files_sha256"]), 64)
        self.assertEqual(len(fingerprint["model_inventory_sha256"]), 64)
        self.assertEqual(len(fingerprint["model_fingerprint_sha256"]), 64)

    def _row(self) -> dict[str, str]:
        return {
            "candidate_id": "cand", "source_content_id": "source",
            "visual_unit_id": "visual", "canonical_visual_unit_id": "canonical",
            "derived_id": "derived", "runtime_visual_file_sha256": "a" * 64,
        }

    def _valid_stdout(self, generation_tokens: int = 120) -> str:
        return (
            "<|im_start|>assistant\n"
            "1）概括：画面展示一条城市道路及行驶车辆。\n"
            "2）元素：人物：无；物体：汽车、道路标志；场景：城市街道；动作：车辆行驶；环境：白天；文字区域：路牌。\n"
            "3）检索价值：适合检索城市道路、汽车、交通标志和街景，可用于交通与城市环境素材。\n"
            "<|im_end|>\n==========\nPrompt: 100 tokens\n"
            f"Generation: {generation_tokens} tokens\nPeak memory: 4.0 GB\n"
        )

    def test_truncated_and_missing_required_fields_never_become_success(self) -> None:
        truncated = orch.classify_output(
            row=self._row(), returncode=0, raw_stdout=self._valid_stdout(384), stderr="",
            current_input_sha256="a" * 64, max_tokens=384,
            required_sections=("1）概括：", "2）元素：", "3）检索价值："),
        )
        self.assertEqual(truncated["status"], "truncated")
        missing = orch.classify_output(
            row=self._row(), returncode=0,
            raw_stdout="<|im_start|>assistant\n只有一段完整但没有固定结构的较长描述文字，用于确认缺字段状态不会被误写为成功。<|im_end|>",
            stderr="", current_input_sha256="a" * 64, max_tokens=384,
            required_sections=("1）概括：", "2）元素：", "3）检索价值："),
        )
        self.assertEqual(missing["status"], "missing_required_fields")
        absolute_path = orch.classify_output(
            row=self._row(), returncode=0,
            raw_stdout=self._valid_stdout().replace("城市道路", "/private/tmp/secret.jpg 城市道路", 1),
            stderr="", current_input_sha256="a" * 64, max_tokens=384,
            required_sections=("1）概括：", "2）元素：", "3）检索价值："),
        )
        self.assertEqual(absolute_path["status"], "parse_failed")

    def test_input_fingerprint_mismatch_and_resume_skip_success(self) -> None:
        mismatch = orch.classify_output(
            row=self._row(), returncode=0, raw_stdout=self._valid_stdout(), stderr="",
            current_input_sha256="b" * 64, max_tokens=384,
            required_sections=("1）概括：", "2）元素：", "3）检索价值："),
        )
        self.assertEqual(mismatch["status"], "input_fingerprint_mismatch")
        rows = [{"status": "success", "id": 1}, {"status": "failed", "id": 2}, {"status": "review", "id": 3}]
        self.assertEqual([row["id"] for row in orch.resume_filter(rows)], [2, 3])

    def test_dry_run_reads_view_and_does_not_write_database(self) -> None:
        before = orch.db_state(self.db_copy)
        with tempfile.TemporaryDirectory() as out_dir:
            out = Path(out_dir) / "dry"
            pre = {
                "status": "PASS", "technical_status": "PASS", "policy_status": "PASS",
                "commit_status": "DO_NOT_COMMIT", "model_sha256": "m" * 64,
                "model_fingerprint_sha256": "f" * 64,
                "prompt_sha256": "p" * 64, "output_contract_version": output_contract.CONTRACT_VERSION,
            }
            result = orch.dry_run(
                db=self.db_copy, out=out, pre=pre, max_tokens=384,
                allow_simulation=False,
            )
            self.assertEqual(result["execution_plan_count"], 336)
            self.assertEqual(result["execution_key_unique_count"], 336)
            self.assertEqual(result["execution_plan_jsonl_rows"], 336)
        self.assertEqual(orch.db_state(self.db_copy), before)

    def test_result_persistence_and_readback_include_all_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "result.sqlite"
            source = sqlite3.connect(f"file:{self.db_copy}?mode=ro", uri=True)
            target = sqlite3.connect(str(db))
            source.backup(target)
            target.close()
            source.close()
            con, _ = orch.queue_connection(db, allow_simulation=False)
            try:
                row = orch.load_queue(con)[0]
                contract = orch.contract_metadata(con)
            finally:
                con.close()
            pre = {
                "contract": contract, "model_path": "/local/model",
                "model_sha256": "a" * 64, "model_config_sha256": "b" * 64,
                "model_tokenizer_files_json": "{}",
                "model_tokenizer_files_sha256": "c" * 64,
                "model_inventory_json": "[]", "model_inventory_sha256": "d" * 64,
                "model_fingerprint_sha256": "e" * 64,
                "prompt_sha256": "f" * 64, "config_sha256": "1" * 64,
                "temperature": 0.0, "top_p": 1.0,
            }
            run_id, planned = orch.create_run_and_items(
                db=db, rows=[row], pre=pre,
                prompt_path=ROOT / "configs/qwenvl_prompt_v2_384.txt",
                max_tokens=384, workers=1,
            )
            classification = orch.classify_output(
                row=planned[0], returncode=0, raw_stdout=self._valid_stdout(), stderr="",
                current_input_sha256=row["runtime_visual_file_sha256"], max_tokens=384,
                required_sections=("1）概括：", "2）元素：", "3）检索价值："),
            )
            output_row = output_contract.write_qwenvl_contract_outputs(
                evidence_id="test_evidence", raw_stdout=self._valid_stdout(),
                out_dir=Path(temp_dir) / "outputs", max_tokens=384,
            )
            stderr_path = Path(temp_dir) / "stderr.txt"
            stderr_path.write_text("", encoding="utf-8")
            orch.persist_item_result(
                db=db, row=planned[0], run_id=run_id, classification=classification,
                output_row=output_row, stderr_path=stderr_path, pre=pre,
            )
            readback = orch.readback_run(db, run_id, expected_count=1)
            self.assertEqual(readback["status"], "PASS")
            self.assertTrue(readback["result_id_match"])
            self.assertEqual(readback["fingerprint_missing_count"], 0)
            con, _ = orch.queue_connection(db, allow_simulation=False)
            try:
                first_two = orch.load_queue(con)[:2]
            finally:
                con.close()
            next_run, next_planned = orch.create_run_and_items(
                db=db, rows=first_two, pre=pre,
                prompt_path=ROOT / "configs/qwenvl_prompt_v2_384.txt",
                max_tokens=384, workers=1,
            )
            self.assertNotEqual(next_run, run_id)
            self.assertEqual(len(next_planned), 1)
            self.assertEqual(next_planned[0]["candidate_id"], first_two[1]["candidate_id"])

    def test_new_orchestrator_has_no_old_manifest_or_finder_contract(self) -> None:
        source = (SCRIPT_DIR / "stop03_3c_qwenvl_db_orchestrator_v1.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("--stop03-2-base", source)
        self.assertNotIn("--source-root", source)
        self.assertNotIn("scan_finder_tags", source)
        self.assertIn("v_stop03_2_v25_qwenvl_execution_queue", source)
        self.assertNotIn('"--top-p"', source)
        self.assertIn('"--gen-kwargs"', source)


if __name__ == "__main__":
    unittest.main()
