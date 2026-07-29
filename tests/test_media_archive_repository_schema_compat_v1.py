from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.repository import ReadonlyMediaRepository  # noqa: E402


class MediaArchiveRepositorySchemaCompatTests(unittest.TestCase):
    def test_person_reid_links_and_cluster_results_are_query_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "person.sqlite"
            preview = Path(temp) / "preview.jpg"
            source = Path(temp) / "source.mov"
            preview.write_bytes(b"preview")
            source.write_bytes(b"source")
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(
                    source_content_id TEXT PRIMARY KEY,absolute_path TEXT,
                    relative_path TEXT,file_name TEXT,extension TEXT,
                    media_type TEXT,size_bytes INTEGER
                );
                CREATE TABLE derived_assets(
                    derived_id TEXT PRIMARY KEY,source_content_id TEXT,
                    derived_path TEXT
                );
                CREATE TABLE visual_units(
                    visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT
                );
                CREATE VIEW v_stop03_1c_latest_person_cluster_members AS
                SELECT 'run1' run_id,'cluster1' person_cluster_id,
                       'face1' representative_face_id,2 member_count,
                       2 distinct_source_count,'high' cluster_confidence,
                       'unreviewed' human_review_status,
                       '' anonymous_display_name,'face1' face_id,
                       0.91 similarity_to_representative,
                       'visual1' visual_unit_id,'source1' source_content_id,
                       'derived1' derived_id,'' visual_file,
                       'video' media_type,12000 time_position_ms
                UNION ALL
                SELECT 'run1','cluster1','face1',2,2,'high','unreviewed',
                       '','face2',0.87,'visual2','source2','derived2','',
                       'image',0;
                """
            )
            con.executemany(
                "INSERT INTO source_assets VALUES(?,?,?,?,?,?,?)",
                [
                    ("source1", str(source), "folder/source.mov", "source.mov", ".mov", "video", 6),
                    ("source2", str(source), "folder/image.jpg", "image.jpg", ".jpg", "image", 6),
                ],
            )
            con.executemany(
                "INSERT INTO derived_assets VALUES(?,?,?)",
                [
                    ("derived1", "source1", str(preview)),
                    ("derived2", "source2", str(preview)),
                ],
            )
            con.executemany(
                "INSERT INTO visual_units VALUES(?,?)",
                [("visual1", "source1"), ("visual2", "source2")],
            )
            con.commit()
            before = db.stat().st_size
            con.close()
            repository = ReadonlyMediaRepository(db)
            links = repository.person_clusters_for_visual_units(["visual1"])
            page = repository.person_cluster_results("cluster1", "all", 0, 30)
            catalog = repository.person_cluster_catalog()
            after = db.stat().st_size
        self.assertEqual(links["visual1"][0]["person_cluster_id"], "cluster1")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["count_by_media"], {"video": 1, "image": 1})
        self.assertTrue(page["items"][0]["can_open_original"])
        self.assertEqual(catalog["total"], 1)
        self.assertEqual(catalog["items"][0]["person_cluster_id"], "cluster1")
        self.assertEqual(
            catalog["items"][0]["preview_path"], str(preview.resolve()),
        )
        self.assertEqual(before, after)

    def test_person_catalog_hides_unreviewed_same_source_only_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "person.sqlite"
            preview = Path(temp) / "preview.jpg"
            source = Path(temp) / "source.mov"
            preview.write_bytes(b"preview")
            source.write_bytes(b"source")
            con = sqlite3.connect(db)
            con.executescript(
                f"""
                CREATE TABLE source_assets(
                    source_content_id TEXT PRIMARY KEY,absolute_path TEXT,
                    relative_path TEXT,file_name TEXT,extension TEXT,
                    media_type TEXT,size_bytes INTEGER
                );
                CREATE TABLE derived_assets(
                    derived_id TEXT PRIMARY KEY,source_content_id TEXT,
                    derived_path TEXT
                );
                CREATE VIEW v_stop03_1c_latest_person_cluster_members AS
                SELECT 'run1' run_id,'same_source' person_cluster_id,
                       'face1' representative_face_id,3 member_count,
                       1 distinct_source_count,'review' cluster_confidence,
                       'unreviewed' human_review_status,
                       '' anonymous_display_name,'face1' face_id,
                       1.0 similarity_to_representative,
                       'visual1' visual_unit_id,'source1' source_content_id,
                       'derived1' derived_id,'' visual_file,
                       'video' media_type,0 time_position_ms;
                INSERT INTO source_assets VALUES(
                    'source1','{source}','source.mov','source.mov','.mov','video',6
                );
                INSERT INTO derived_assets VALUES(
                    'derived1','source1','{preview}'
                );
                """
            )
            con.commit()
            con.close()
            catalog = ReadonlyMediaRepository(db).person_cluster_catalog()
        self.assertEqual(catalog["total"], 0)

    def test_stage_metrics_use_database_counts_not_project_constants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "media_archive.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);
                CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);
                CREATE TABLE visual_labels(visual_unit_id TEXT);
                CREATE TABLE stop03_2_candidate_queue_frozen_v25(queue_type TEXT,media_type TEXT);
                CREATE TABLE model_runs(
                    stage TEXT,status TEXT,input_count INTEGER,output_count INTEGER,
                    started_at TEXT,finished_at TEXT
                );
                INSERT INTO source_assets VALUES
                    ('i1','image'),('i2','image'),
                    ('v1','video'),('v2','video'),('v3','video');
                INSERT INTO visual_units VALUES
                    ('iv1','i1'),('iv2','i2'),
                    ('vv1','v1'),('vv2','v1'),('vv3','v2'),
                    ('vv4','v2'),('vv5','v3');
                INSERT INTO visual_labels VALUES('iv1'),('vv1');
                INSERT INTO stop03_2_candidate_queue_frozen_v25 VALUES
                    ('qwenvl_high_value','image'),('qwenvl_high_value','video');
                INSERT INTO model_runs VALUES(
                    'stop03_yoloe_full','done',7,2,'2026-01-01','2026-01-02'
                );
                """
            )
            con.commit(); con.close()
            metrics = ReadonlyMediaRepository(db).stage_metrics()
        self.assertEqual((metrics["scan"]["done"], metrics["scan"]["total"]), (5, 5))
        self.assertEqual((metrics["video_frames"]["done"], metrics["video_frames"]["total"]), (3, 3))
        self.assertEqual((metrics["yoloe"]["done"], metrics["yoloe"]["total"]), (7, 7))
        self.assertIn("其中 2 张检测到物体", metrics["yoloe"]["description"])
        self.assertIn("视频 1 张", metrics["qwen_optional_v2"]["description"])
        self.assertIn("图片冻结候选 1 张", metrics["qwen_optional_v2"]["description"])

    def test_completed_evidence_metric_never_shows_done_greater_than_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "media_archive.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(
                    source_content_id TEXT PRIMARY KEY,media_type TEXT
                );
                CREATE TABLE stop03_5_unified_evidence_items(item_id TEXT);
                INSERT INTO stop03_5_unified_evidence_items VALUES
                    ('e1'),('e2'),('e3');
                """
            )
            con.close()
            metric = ReadonlyMediaRepository(db).stage_metrics()["evidence_optional_v2"]
        self.assertEqual((metric["done"], metric["total"]), (3, 3))

    def test_overview_accepts_generic_processing_error_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                CREATE TABLE source_assets(
                    source_content_id TEXT PRIMARY KEY,
                    media_type TEXT,
                    size_bytes INTEGER,
                    is_deleted_or_missing INTEGER DEFAULT 0
                );
                CREATE TABLE visual_units(
                    visual_unit_id TEXT PRIMARY KEY,
                    source_content_id TEXT
                );
                CREATE TABLE processing_errors(
                    error_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    stage TEXT,
                    item_id TEXT,
                    item_path TEXT,
                    error_type TEXT,
                    error_message TEXT
                );
                INSERT INTO source_assets VALUES('src1','image',100,0);
                INSERT INTO visual_units VALUES('vis1','src1');
                INSERT INTO processing_errors(error_id,stage,item_id) VALUES('err1','scan','src1');
                """
            )
            con.close()
            overview = ReadonlyMediaRepository(db).overview()
        self.assertEqual(overview["source_total_count"], 1)
        self.assertEqual(overview["visual_unit_total_count"], 1)
        self.assertEqual(overview["processing_error_count"], 1)

    def test_timelapse_page_falls_back_to_step02_derived_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            db = workspace / "media_archive.sqlite"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE step02_image_timelapse_keyframes("
                "visual_unit_id TEXT,sequence_id TEXT,source_relative_path TEXT,created_at TEXT)"
            )
            con.commit(); con.close()
            manifest = workspace / "stages/02_image_preview/manifests/image_preview_manifest.csv"
            manifest.parent.mkdir(parents=True)
            preview = workspace / "preview.jpg"
            preview.write_bytes(b"jpg")
            source_folder = workspace / "original_timelapse"
            source_folder.mkdir()
            fields = [
                "sequence_id", "representative_position", "source_relative_path",
                "source_path", "status", "output_path",
            ]
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for role in ("first", "middle", "last"):
                    writer.writerow({
                        "sequence_id": "4", "representative_position": role,
                        "source_relative_path": f"group/{role}.dng",
                        "source_path": str(source_folder / f"{role}.dng"),
                        "status": "success", "output_path": str(preview),
                    })
            sequence_manifest = manifest.parent / "image_timelapse_sequences.csv"
            with sequence_manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "sequence_id", "relative_dir", "image_count", "first_file", "last_file",
                ])
                writer.writeheader()
                writer.writerow({
                    "sequence_id": "4", "relative_dir": "group", "image_count": "60",
                    "first_file": "group/0001.dng", "last_file": "group/0060.dng",
                })
            payload = ReadonlyMediaRepository(db).timelapse_groups()
            overview = ReadonlyMediaRepository(db).overview()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(overview["timelapse_group_count"], 1)
        self.assertEqual(len(payload["items"][0]["frames"]), 3)
        self.assertEqual(payload["items"][0]["source_photo_count"], 60)
        self.assertEqual(payload["items"][0]["source_relative_dir"], "group")
        self.assertEqual(payload["items"][0]["source_folder"], str(source_folder))
        self.assertEqual(
            payload["items"][0]["source"], "step02_derived_manifest_fallback",
        )


if __name__ == "__main__":
    unittest.main()
