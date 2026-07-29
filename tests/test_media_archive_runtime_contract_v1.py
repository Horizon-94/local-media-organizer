from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.runtime_contract import (  # noqa: E402
    load_runtime_contract,
    materialize_runtime_configs,
    task_runtime_from_contract,
    validate_runtime_contract,
)


class MediaArchiveRuntimeContractTests(unittest.TestCase):
    def test_fixed_runtime_contract_is_complete_without_loading_models(self) -> None:
        report = validate_runtime_contract(ROOT / "configs/media_archive_app_runtime_contract_v1.json")
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["counts"]["python"], 6)
        self.assertEqual(report["counts"]["tools"], 3)
        self.assertEqual(report["counts"]["models"], 8)
        self.assertIn("person_reid", {row["key"] for row in report["model_items"]})

    def test_runtime_contract_reports_exact_missing_key(self) -> None:
        source = ROOT / "configs/media_archive_app_runtime_contract_v1.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["python"]["qwen"] = "/definitely/missing/qwen-python"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "contract.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = validate_runtime_contract(path)
        self.assertFalse(report["ready"])
        self.assertIn("python.qwen", report["missing"])

    def test_task_runtime_preserves_venv_entry_and_uses_contract_maps(self) -> None:
        contract = load_runtime_contract(ROOT / "configs/media_archive_app_runtime_contract_v1.json")
        runtime = task_runtime_from_contract(
            contract, ocr_workers=3, embedding_workers=2,
            requested_scheduler_mode="pipeline_async",
        )
        self.assertEqual(runtime["python"]["qwen"], contract["python"]["qwen"])
        self.assertEqual(runtime["tools"]["ffmpeg"], contract["tools"]["ffmpeg"])
        self.assertIn("candidate_select", runtime["scripts"])
        self.assertIn("hybrid_search", runtime["configs"])
        self.assertEqual(runtime["models"]["qwen"], contract["models"]["qwen"]["path"])
        self.assertEqual(runtime["effective_scheduler_mode"], "stage_serial")

    def test_portable_placeholders_and_effective_configs_are_resolved(self) -> None:
        source = ROOT / "configs/media_archive_app_runtime_contract_v1.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["project_root"] = "$APP_RESOURCES/Pipeline"
        payload["models"]["person_reid"]["path"] = "$MODEL_ROOT/insightface/buffalo_l"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "Example.app/Contents/Resources"
            resources.mkdir(parents=True)
            contract_path = resources / "runtime_contract.json"
            contract_path.write_text(json.dumps(payload), encoding="utf-8")
            contract = load_runtime_contract(
                contract_path, model_root=root / "ExternalModels",
            )
            self.assertEqual(
                contract["project_root"],
                str(resources / "Pipeline"),
            )
            self.assertEqual(
                contract["models"]["person_reid"]["path"],
                str(root / "ExternalModels/insightface/buffalo_l"),
            )

    def test_effective_configs_redirect_models_without_modifying_sources(self) -> None:
        contract = load_runtime_contract(
            ROOT / "configs/media_archive_app_runtime_contract_v1.json"
        )
        before = (
            ROOT / "configs/stop03_1c_person_reid_db_v1.json"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            configs = materialize_runtime_configs(contract, Path(temp))
            person = json.loads(Path(configs["person_reid"]).read_text(encoding="utf-8"))
            qwen = json.loads(Path(configs["qwen"]).read_text(encoding="utf-8"))
            self.assertEqual(person["model_dir"], contract["models"]["person_reid"]["path"])
            self.assertEqual(qwen["qwen_python"], contract["python"]["qwen"])
        self.assertEqual(
            before,
            (ROOT / "configs/stop03_1c_person_reid_db_v1.json").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
