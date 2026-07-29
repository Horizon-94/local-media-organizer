from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/public_release_audit.py"
SPEC = importlib.util.spec_from_file_location("public_release_audit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PublicReleaseAuditTests(unittest.TestCase):
    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.py").write_text("print('ok')\n", encoding="utf-8")
            self.assertEqual(MODULE.audit(root), [])

    def test_private_database_and_token_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "private.sqlite").write_bytes(b"SQLite format 3")
            (root / "secret.txt").write_text(
                "token=github_pat_" + "a" * 24,
                encoding="utf-8",
            )
            failures = MODULE.audit(root)
            self.assertTrue(any(row.startswith("forbidden_file:") for row in failures))
            self.assertTrue(any(row.startswith("github_token:") for row in failures))

    def test_git_metadata_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text(
                "/Users/" + "alice/Documents/private",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.audit(root), [])

    def test_dev_extra_declares_icon_builder_dependency(self) -> None:
        pyproject = (SCRIPT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dev = ["pytest>=8", "Pillow>=10"]', pyproject)


if __name__ == "__main__":
    unittest.main()
