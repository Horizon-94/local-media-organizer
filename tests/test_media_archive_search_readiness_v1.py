from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.search_jobs import SearchJobManager  # noqa: E402


class MediaArchiveSearchReadinessTests(unittest.TestCase):
    def test_uncovered_video_is_warning_when_all_derived_units_have_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);
                CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);
                CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);
                CREATE TABLE embeddings(visual_unit_id TEXT);
                CREATE TABLE model_runs(run_id TEXT);
                CREATE TABLE visual_labels(visual_unit_id TEXT);
                CREATE TABLE visual_label_terms(term TEXT);
                INSERT INTO source_assets VALUES('image1','image'),('video1','video');
                INSERT INTO visual_units VALUES('visual1','image1');
                INSERT INTO embeddings VALUES('visual1');
                """
            )
            con.close()
            manager = SearchJobManager(
                db_path=db,
                output_root=root / "out",
                search_script=ROOT / "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py",
                search_config=ROOT / "configs/stop03_5e_hybrid_visual_text_search_v2.json",
                embedding_python=Path(sys.executable),
                openclip_python=Path(sys.executable),
            )
            report = manager.readiness()
        self.assertTrue(report["ready"])
        self.assertFalse(report["checks"]["video_source_coverage"])
        self.assertEqual(report["uncovered_video_source_count"], 1)
        self.assertTrue(all(isinstance(value, bool) for value in report["checks"].values()))
        self.assertTrue(report["checks"]["visual_vector_coverage"])

    def test_search_is_blocked_when_a_derived_visual_unit_has_no_vector(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);
                CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);
                CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);
                CREATE TABLE embeddings(visual_unit_id TEXT);
                CREATE TABLE model_runs(run_id TEXT);
                CREATE TABLE visual_labels(visual_unit_id TEXT);
                CREATE TABLE visual_label_terms(term TEXT);
                INSERT INTO source_assets VALUES('image1','image');
                INSERT INTO visual_units VALUES('visual1','image1'),('visual2','image1');
                INSERT INTO embeddings VALUES('visual1');
                """
            )
            con.close()
            manager = SearchJobManager(
                db_path=db,
                output_root=root / "out",
                search_script=ROOT / "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py",
                search_config=ROOT / "configs/stop03_5e_hybrid_visual_text_search_v2.json",
                embedding_python=Path(sys.executable),
                openclip_python=Path(sys.executable),
            )
            report = manager.readiness()
        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["visual_vector_coverage"])


if __name__ == "__main__":
    unittest.main()
