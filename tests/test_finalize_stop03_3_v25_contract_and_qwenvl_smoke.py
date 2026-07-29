from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts/03_stop03_visual_analysis"
sys.path.insert(0, str(SCRIPT_DIR))

import finalize_stop03_3_v25_contract_and_qwenvl_smoke as finalizer  # noqa: E402


class V25QwenVLFinalizerTests(unittest.TestCase):
    def test_stage_contract_is_exact_and_ordered(self) -> None:
        self.assertEqual(finalizer.STAGES, (
            "A_registry_and_path_check", "B_py_compile", "C_targeted_tests",
            "D_candidate_contract_preflight", "E_candidate_contract_dry_run",
            "F_database_backup", "G_candidate_contract_commit",
            "H_candidate_contract_readback", "I_candidate_contract_idempotency",
            "J_qwenvl_preflight", "K_qwenvl_smoke_3_at_384",
            "L_qwenvl_database_readback", "M_final_integrity_and_summary",
        ))

    def test_cli_locks_smoke_to_three_and_repair_cycles_to_three(self) -> None:
        args = finalizer.parse_args(["--mode", "run"])
        self.assertEqual(args.smoke_limit, 3)
        self.assertEqual(args.max_repair_cycles, 3)
        self.assertEqual(args.repair_cycles_used, 0)

    def test_output_guard_rejects_project_paths(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "output_outside_test_output"):
            finalizer.assert_output_path(ROOT / "not-test-output")

    def test_resume_skips_only_passed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            obj = object.__new__(finalizer.Finalizer)
            obj.mode = "resume"
            obj.out = Path(temp_dir)
            (obj.out / "logs").mkdir()
            obj.state = {"stages": {finalizer.STAGES[0]: {"status": "PASS"}}}
            called = []
            obj.run_stage(finalizer.STAGES[0], lambda: called.append(True))
            self.assertEqual(called, [])

    def test_migration_persists_required_result_and_fingerprint_fields(self) -> None:
        sql = (ROOT / "migrations/20260711_stop03_2_v25_candidate_snapshot_qwenvl_v1.sql").read_text(encoding="utf-8")
        for field in (
            "run_id", "run_item_id", "candidate_id", "source_content_id",
            "visual_unit_id", "canonical_visual_unit_id", "derived_id",
            "candidate_role", "reason_codes", "policy_version", "qwen_text_preview",
            "runtime_metrics_json", "prompt_tokens", "generation_tokens",
            "peak_memory_gb", "finish_reason", "truncation_status", "cleanup_status",
            "runtime_visual_file_sha256", "model_config_sha256",
            "model_tokenizer_files_sha256", "model_inventory_sha256",
            "model_fingerprint_sha256", "prompt_sha256", "script_sha256",
        ):
            self.assertIn(field, sql)


if __name__ == "__main__":
    unittest.main()
