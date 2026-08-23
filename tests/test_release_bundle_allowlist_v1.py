import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("release_builder_allowlist", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseBundleAllowlistTests(unittest.TestCase):
    def test_runtime_contract_entries_are_shipped(self) -> None:
        builder = load_builder()
        _, sources = builder._release_bundle_sources(ROOT)
        relative = {str(path.relative_to(ROOT)) for path in sources}
        contract = json.loads(
            (ROOT / "configs/media_archive_app_runtime_contract_v1.json").read_text()
        )
        for section in ("scripts", "configs", "migrations"):
            for configured in contract.get(section, {}).values():
                self.assertTrue(str(configured).startswith("$PROJECT_ROOT/"))
                relative_path = str(configured).removeprefix("$PROJECT_ROOT/")
                self.assertIn(relative_path, relative)

    def test_dynamic_helpers_are_shipped_but_historical_versions_are_not(self) -> None:
        builder = load_builder()
        _, sources = builder._release_bundle_sources(ROOT)
        relative = {str(path.relative_to(ROOT)) for path in sources}
        required = {
            "scripts/02_step01_step02_pipeline/step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py",
            "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py",
            "scripts/03_stop03_visual_analysis/stop03_2_v25_candidate_contract_lock.py",
            "scripts/03_stop03_visual_analysis/qwenvl_output_contract_v2.py",
            "scripts/03_stop03_visual_analysis/stop03_3c_qwenvl_db_orchestrator_v1.py",
            "scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_batch75_diagnostic_v1.py",
            "scripts/03_stop03_visual_analysis/stop03_5a_joint_db_quality_audit_v1.py",
            "scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_contract_v1.py",
            "scripts/03_stop03_visual_analysis/stop03_5e_text_search_contract_v1.py",
            "scripts/03_stop03_visual_analysis/stop03_5e_text_search_smoke_v1.py",
            "scripts/04_media_archive_app/restore_source_file_lineage_from_manifest_v1.py",
        }
        self.assertTrue(required <= relative)
        self.assertNotIn("configs/stop03_2_high_value_policy_v23.json", relative)
        self.assertNotIn("configs/stop03_2_high_value_policy_v24.json", relative)
        self.assertNotIn(
            "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v3.py",
            relative,
        )

    def test_copy_preserves_only_allowlisted_release_sources(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "Pipeline"
            report = builder._copy_release_bundle_sources(ROOT, destination)
            copied = {
                str(path.relative_to(destination))
                for path in destination.rglob("*") if path.is_file()
            }
            _, sources = builder._release_bundle_sources(ROOT)
            expected = {str(path.relative_to(ROOT)) for path in sources}
            self.assertEqual(copied, expected)
            self.assertEqual(report["file_count"], len(expected))
            self.assertGreater(report["total_bytes"], 0)

    def test_release_version_is_1_2_3(self) -> None:
        builder = load_builder()
        self.assertEqual(builder.APP_SEMVER, "1.2.3")
        self.assertEqual(builder.APP_BUILD_NUMBER, "123")


if __name__ == "__main__":
    unittest.main()
