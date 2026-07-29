from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py"
)
SPEC = importlib.util.spec_from_file_location("person_reid_v1", SCRIPT)
assert SPEC and SPEC.loader
PERSON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PERSON)


def create_base_db(root: Path, count: int = 6) -> tuple[Path, list[dict[str, object]]]:
    db = root / "library.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE source_assets(
            source_content_id TEXT PRIMARY KEY,
            absolute_path TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            ctime INTEGER NOT NULL,
            volume_id TEXT NOT NULL DEFAULT 'LOCAL',
            online_status INTEGER DEFAULT 1,
            is_deleted_or_missing INTEGER DEFAULT 0
        );
        CREATE TABLE derived_assets(
            derived_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL
        );
        CREATE TABLE visual_units(
            visual_unit_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL,
            derived_id TEXT NOT NULL,
            visual_file TEXT NOT NULL,
            time_position_ms INTEGER NOT NULL DEFAULT -1,
            near_black INTEGER DEFAULT 0,
            near_dup_group_id TEXT,
            is_near_dup_representative INTEGER DEFAULT 0
        );
        CREATE TABLE model_runs(
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_path TEXT NOT NULL,
            script_version TEXT NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0,
            output_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL
        );
        """
    )
    rows: list[dict[str, object]] = []
    for index in range(count):
        source_id = f"source_{index}"
        derived_id = f"derived_{index}"
        visual_id = f"visual_{index}"
        source = root / "original" / f"{source_id}.mov"
        visual = root / "derived" / f"{visual_id}.jpg"
        visual.parent.mkdir(parents=True, exist_ok=True)
        visual.write_bytes(b"fixture")
        con.execute(
            "INSERT INTO source_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id, str(source), source.name, source.name, ".mov", "video",
                100, 1, 1, "LOCAL", 1, 0,
            ),
        )
        con.execute("INSERT INTO derived_assets VALUES(?,?)", (derived_id, source_id))
        con.execute(
            "INSERT INTO visual_units VALUES(?,?,?,?,?,?,?,?)",
            (visual_id, source_id, derived_id, str(visual), index * 3000, 0, None, 0),
        )
        rows.append({
            "visual_unit_id": visual_id,
            "source_content_id": source_id,
            "derived_id": derived_id,
            "visual_file": str(visual),
            "time_position_ms": index * 3000,
            "media_type": "video",
            "visual_size_bytes": visual.stat().st_size,
            "visual_mtime_ns": visual.stat().st_mtime_ns,
        })
    con.commit()
    con.close()
    return db, rows


def make_test_config(root: Path) -> dict[str, object]:
    path = root / "config.json"
    value = {
        "model_name": "fake",
        "default_workers": 3,
        "default_max_attempts": 1,
        "auto_merge_cosine_min": 0.58,
        "_config_path": str(path),
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def inventory() -> dict[str, str]:
    return {
        "model_dir": "/model/fake",
        "detector_sha256": "detector",
        "recognizer_sha256": "recognizer",
    }


def face(vector: list[float], *, x: int = 1) -> dict[str, object]:
    return {
        "bbox": [x, 1, x + 50, 51],
        "landmarks": [[10, 10], [30, 10], [20, 20], [12, 35], [28, 35]],
        "detection_score": 0.99,
        "quality_score": 0.9,
        "embedding": vector,
    }


class PersonReidOrchestratorTests(unittest.TestCase):
    def test_global_clustering_is_input_order_independent(self) -> None:
        rows = [
            {
                "row": {
                    "face_id": "face_a", "visual_unit_id": "visual_a",
                    "source_content_id": "source_a",
                },
                "vector": np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
            },
            {
                "row": {
                    "face_id": "face_b", "visual_unit_id": "visual_b",
                    "source_content_id": "source_b",
                },
                "vector": np.asarray([0.94, 0.34, 0.0], dtype=np.float32),
            },
            {
                "row": {
                    "face_id": "face_c", "visual_unit_id": "visual_c",
                    "source_content_id": "source_c",
                },
                "vector": np.asarray([0.92, 0.39, 0.0], dtype=np.float32),
            },
        ]
        forward = PERSON._cluster_face_records(rows, 0.8)
        reverse = PERSON._cluster_face_records(list(reversed(rows)), 0.8)
        normalize = lambda clusters: [
            sorted(str(row["face_id"]) for row, _similarity in cluster["members"])
            for cluster in clusters
        ]
        self.assertEqual(normalize(forward), normalize(reverse))
        self.assertEqual(normalize(forward), [["face_a", "face_b", "face_c"]])

    def test_representative_similarity_is_measured_against_actual_representative(self) -> None:
        records = [
            {
                "row": {
                    "face_id": "face_a", "visual_unit_id": "visual_a",
                    "source_content_id": "source_a",
                },
                "vector": np.asarray([1.0, 0.0], dtype=np.float32),
            },
            {
                "row": {
                    "face_id": "face_b", "visual_unit_id": "visual_b",
                    "source_content_id": "source_b",
                },
                "vector": np.asarray([0.8, 0.6], dtype=np.float32),
            },
        ]
        cluster = PERSON._cluster_face_records(records, 0.5)[0]
        similarities = {
            str(row["face_id"]): similarity
            for row, similarity in cluster["members"]
        }
        representative = str(cluster["representative_face_id"])
        self.assertAlmostEqual(similarities[representative], 1.0, places=6)
        self.assertAlmostEqual(min(similarities.values()), 0.8, places=6)

    def test_scrfd_interleaved_outputs_are_grouped_by_tensor_role(self) -> None:
        outputs = [
            np.full((12800, 1), 0.1, dtype=np.float32),
            np.full((12800, 4), 1.1, dtype=np.float32),
            np.full((12800, 10), 2.1, dtype=np.float32),
            np.full((3200, 1), 0.2, dtype=np.float32),
            np.full((3200, 4), 1.2, dtype=np.float32),
            np.full((3200, 10), 2.2, dtype=np.float32),
            np.full((800, 1), 0.3, dtype=np.float32),
            np.full((800, 4), 1.3, dtype=np.float32),
            np.full((800, 10), 2.3, dtype=np.float32),
        ]
        groups = PERSON._scrfd_output_groups(outputs, np)
        self.assertEqual([len(group[0]) for group in groups], [12800, 3200, 800])
        self.assertAlmostEqual(float(groups[0][0][0, 0]), 0.1, places=6)
        self.assertAlmostEqual(float(groups[0][1][0, 0]), 1.1, places=6)
        self.assertAlmostEqual(float(groups[0][2][0, 0]), 2.1, places=6)

    def test_preflight_reads_derived_visual_units_not_original_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, _ = create_base_db(root, 2)
            rows = PERSON.eligible_visual_units(db)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("source_absolute_path" in row for row in rows))
        self.assertFalse(any(row["is_original_path"] for row in rows))
        self.assertTrue(all(row["visual_exists"] for row in rows))

    def test_three_workers_reach_three_concurrent_fake_inferences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, rows = create_base_db(root, 6)
            PERSON.apply_migration(
                db, ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
            )
            config = make_test_config(root)
            run_id = PERSON.prepare_run(db, rows, config, inventory(), 3, 1)
            fixture = {row["visual_unit_id"]: [face([1.0, 0.0, 0.0, 0.0])] for row in rows}
            report = PERSON.run_workers(
                db, run_id, 3, 1,
                lambda _index: PERSON.FakeBackend(fixture, 0.05),
                root / "progress.jsonl",
            )
        self.assertEqual(report["measured_max_concurrency"], 3)
        self.assertEqual(sum(row["completed"] for row in report["worker_reports"]), 6)

    def test_one_failed_item_does_not_block_other_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, rows = create_base_db(root, 4)
            PERSON.apply_migration(
                db, ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
            )
            config = make_test_config(root)
            run_id = PERSON.prepare_run(db, rows, config, inventory(), 3, 1)
            fixture = {
                rows[0]["visual_unit_id"]: {"error": "fixture_failure"},
                **{
                    row["visual_unit_id"]: [face([1.0, 0.0, 0.0, 0.0])]
                    for row in rows[1:]
                },
            }
            PERSON.run_workers(
                db, run_id, 3, 1,
                lambda _index: PERSON.FakeBackend(fixture, 0.01),
                root / "progress.jsonl",
            )
            counts = PERSON.refresh_counts(db, run_id)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["success"], 3)

    def test_cross_source_similar_faces_merge_but_same_frame_faces_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, rows = create_base_db(root, 2)
            PERSON.apply_migration(
                db, ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
            )
            config = make_test_config(root)
            run_id = PERSON.prepare_run(db, rows, config, inventory(), 1, 1)
            fixture = {
                rows[0]["visual_unit_id"]: [
                    face([1.0, 0.0, 0.0, 0.0], x=1),
                    face([1.0, 0.0, 0.0, 0.0], x=60),
                ],
                rows[1]["visual_unit_id"]: [
                    face([0.999, 0.0447, 0.0, 0.0], x=1),
                ],
            }
            PERSON.run_workers(
                db, run_id, 1, 1,
                lambda _index: PERSON.FakeBackend(fixture),
                root / "progress.jsonl",
            )
            cluster_count = PERSON.build_clusters(db, run_id, 0.58)
            con = sqlite3.connect(db)
            sizes = [
                row[0] for row in con.execute(
                    "SELECT member_count FROM stop03_1c_person_clusters "
                    "WHERE run_id=? ORDER BY member_count",
                    (run_id,),
                )
            ]
            con.close()
        self.assertEqual(cluster_count, 2)
        self.assertEqual(sizes, [1, 2])

    def test_no_face_is_successful_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, rows = create_base_db(root, 1)
            PERSON.apply_migration(
                db, ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
            )
            config = make_test_config(root)
            run_id = PERSON.prepare_run(db, rows, config, inventory(), 1, 1)
            PERSON.run_workers(
                db, run_id, 1, 1,
                lambda _index: PERSON.FakeBackend({rows[0]["visual_unit_id"]: []}),
                root / "progress.jsonl",
            )
            PERSON.build_clusters(db, run_id, 0.58)
            final = PERSON.finish_run(db, run_id)
        self.assertEqual(final["status"], "PASS")
        self.assertEqual(final["no_face_count"], 1)
        self.assertEqual(final["face_count"], 0)

    def test_limit_selects_a_deterministic_prefix_without_fixed_dataset_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, _rows = create_base_db(root, 6)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "det.onnx").write_bytes(b"detector")
            (model_dir / "rec.onnx").write_bytes(b"recognizer")
            config = {
                "contract_version": PERSON.CONTRACT_VERSION,
                "model_name": "fake",
                "model_dir": str(model_dir),
                "detector_file": "det.onnx",
                "recognizer_file": "rec.onnx",
                "runtime_backend": "opencv_dnn_cpu",
                "input_scope": "all_searchable_derived_visual_units",
                "original_media_read": False,
                "persist_face_crops": False,
                "network_policy": "blocked",
                "scheduling_mode": "dynamic_database_claim",
                "worker_model_load_policy": "once_per_worker",
                "same_visual_unit_cannot_link": True,
                "anonymous_identity_only": True,
                "fixed_dataset_counts": False,
                "embedding_dimension": 4,
                "review_cosine_min": 0.4,
                "auto_merge_cosine_min": 0.6,
                "default_workers": 2,
                "default_max_attempts": 1,
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            fixture = root / "fixture.json"
            fixture.write_text("{}", encoding="utf-8")
            out = root / "limited"
            exit_code = PERSON.main([
                "--mode", "run", "--db", str(db),
                "--config", str(config_path),
                "--migration",
                str(ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"),
                "--out", str(out), "--allowed-output-root", str(root),
                "--backend", "fake", "--fake-fixture", str(fixture),
                "--workers", "2", "--limit", "2", "--confirm-central-db-write",
            ])
            summary = json.loads((out / "run_summary.json").read_text())
            preflight = json.loads((out / "preflight_summary.json").read_text())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["visual_unit_count"], 2)
        self.assertEqual(preflight["eligible_visual_unit_count"], 6)
        self.assertEqual(preflight["selected_visual_unit_count"], 2)
        self.assertFalse(preflight["fixed_input_count"])

    def test_successful_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, rows = create_base_db(root, 1)
            PERSON.apply_migration(
                db, ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
            )
            config = make_test_config(root)
            first = PERSON.prepare_run(db, rows, config, inventory(), 1, 1)
            PERSON.run_workers(
                db, first, 1, 1,
                lambda _index: PERSON.FakeBackend({
                    rows[0]["visual_unit_id"]: [face([1.0, 0.0, 0.0, 0.0])]
                }),
                root / "progress.jsonl",
            )
            PERSON.build_clusters(db, first, 0.58)
            PERSON.finish_run(db, first)
            second = PERSON.prepare_run(db, rows, config, inventory(), 3, 3)
            con = sqlite3.connect(db)
            attempts = con.execute(
                "SELECT attempt_count FROM stop03_1c_person_reid_run_items WHERE run_id=?",
                (first,),
            ).fetchone()[0]
            con.close()
        self.assertEqual(second, first)
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
