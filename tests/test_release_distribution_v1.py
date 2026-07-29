from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_artifact_audit.py"
SPEC = importlib.util.spec_from_file_location("release_artifact_audit", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(AUDIT)


def clean_fixture(root: Path) -> Path:
    bundle = root / "本地数据库.app"
    resources = bundle / "Contents/Resources"
    documents = resources / "Documentation"
    documents.mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(b"plist")
    config = {
        "configuration_state": "first_run_clean",
        "database": "",
        "output_root": "",
        "models_included": False,
        "portable_pipeline_runtimes": True,
        "project_root": "$APP_RESOURCES/Pipeline",
        "author": "Horizon-94",
        "official_source": "https://github.com/Horizon-94/local-media-organizer",
        "license": "GPL-3.0-only",
    }
    (resources / "app_config.json").write_text(json.dumps(config), encoding="utf-8")
    (resources / "portable_runtime_manifest.json").write_text(
        json.dumps({
            "models_included": False,
            "network_download_implemented": False,
        }),
        encoding="utf-8",
    )
    for name in (
        "LICENSE-GPL-3.0.txt", "LICENSE_HISTORY.md", "NOTICE.txt",
        "MODEL_SOURCES.md", "MODEL_SETUP.md",
    ):
        (documents / name).write_text("public release document", encoding="utf-8")
    return bundle


class ReleaseDistributionTests(unittest.TestCase):
    def test_clean_generic_app_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = clean_fixture(Path(temp))
            self.assertEqual(AUDIT.audit_app_bundle(bundle), [])

    def test_private_path_database_model_and_token_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = clean_fixture(Path(temp))
            resources = bundle / "Contents/Resources"
            (resources / "private.sqlite").write_bytes(b"SQLite format 3")
            (resources / "weight.pt").write_bytes(b"model")
            (resources / "note.txt").write_bytes(
                b"/Users/" + b"horizon/private github_pat_" + b"a" * 24
            )
            failures = AUDIT.audit_app_bundle(bundle)
            self.assertTrue(any(row.startswith("forbidden_file:") for row in failures))
            self.assertTrue(any(row.startswith("private_user_path:") for row in failures))
            self.assertTrue(any(row.startswith("github_token:") for row in failures))

    def test_model_manifest_is_external_and_complete(self) -> None:
        manifest = json.loads(
            (ROOT / "configs/model_sources_v1.json").read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["automatic_download"])
        self.assertEqual(
            manifest["default_model_root"],
            "~/Library/Application Support/素材大整理/Models",
        )
        paths = {item["relative_path"] for item in manifest["models"]}
        self.assertIn("yoloe26-l-seg/weights/yoloe-26l-seg.pt", paths)
        self.assertIn(
            "openclip-vit-b-32-laion2b-s34b-b79k/model.safetensors", paths
        )
        self.assertTrue(all(item["upstream"].startswith("https://") for item in manifest["models"]))

    def test_current_license_and_official_identity_are_fixed(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn('license = { text = "GPL-3.0-only" }', pyproject)
        self.assertIn("Horizon-94", notice)
        self.assertIn("https://github.com/Horizon-94/local-media-organizer", notice)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", (ROOT / "LICENSE").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
