from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.native_bridge import (  # noqa: E402
    _validate_search_result_contract,
    _load_or_default_profile,
    activate_library,
    existing_libraries,
    estimate_task_remaining,
    human_duration,
    run_favorite_collection,
    run_person_cluster_catalog,
    run_person_cluster_search,
    run_search,
    reconcile_task_pipeline_with_library,
    resume_active_task,
    save_profile,
    start_existing_task,
    start_task,
    task_active_run,
    task_detail,
    task_output_acceptance,
    task_pipeline,
)
from media_archive_image_video_ui import native_bridge  # noqa: E402
from media_archive_image_video_ui.repository import ReadonlyMediaRepository  # noqa: E402


class MediaArchiveNativeBridgeStateTests(unittest.TestCase):
    def test_person_annotations_fall_back_to_named_legacy_overlay(self) -> None:
        legacy = {
            "contract": "media_archive_local_person_annotations_v1",
            "identities": {
                "person-local": {
                    "display_name": "康康", "tags": [], "cluster_ids": ["cluster-1"],
                },
            },
            "cluster_to_identity": {"cluster-1": "person-local"},
        }
        with mock.patch.object(
            native_bridge, "task_id_for_database", return_value="task-1"
        ), mock.patch.object(
            native_bridge, "load_central_person_annotations",
            return_value={
                "contract": "media_archive_local_person_annotations_v1",
                "identities": {}, "cluster_to_identity": {},
            },
        ), mock.patch.object(
            native_bridge, "_legacy_person_annotations",
            return_value=(Path("/legacy/people.json"), legacy),
        ):
            payload, source, task_id = native_bridge._load_person_annotation_payload(
                Path("/library.sqlite")
            )
        self.assertEqual(payload["identities"]["person-local"]["display_name"], "康康")
        self.assertEqual(source, "/legacy/people.json")
        self.assertEqual(task_id, "task-1")

    def test_initial_resource_snapshot_never_returns_empty_contract(self) -> None:
        native_bridge._RESOURCE_CACHE = (0.0, None, {})
        completed = SimpleNamespace(stdout="", returncode=0)
        with mock.patch.object(native_bridge.time, "monotonic", return_value=0.1), mock.patch.object(
            native_bridge.subprocess, "run", return_value=completed
        ):
            report = native_bridge.live_resource_snapshot(None)
        self.assertEqual(report["active_pid"], None)
        self.assertFalse(report["process_alive"])
        self.assertEqual(report["cpu_percent"], 0.0)
        self.assertEqual(report["memory_bytes"], 0)
        self.assertFalse(report["source_scanned"])

    def test_resource_snapshot_aggregates_the_complete_process_tree(self) -> None:
        native_bridge._RESOURCE_CACHE = (0.0, None, {})
        process_rows = SimpleNamespace(
            stdout=(
                "100 1 0.1 1000 S\n"
                "101 100 25.0 2000 S\n"
                "102 101 50.0 3000 R\n"
                "999 1 99.0 9000 R\n"
            ),
            returncode=0,
        )
        swap = SimpleNamespace(stdout="total = 4096.00M used = 512.00M free = 3584.00M", returncode=0)
        with mock.patch.object(native_bridge.time, "monotonic", return_value=10.0), mock.patch.object(
            native_bridge.subprocess, "run", side_effect=[process_rows, swap]
        ):
            report = native_bridge.live_resource_snapshot({"current_child_pid": 100})
        self.assertTrue(report["process_alive"])
        self.assertEqual(report["process_count"], 3)
        self.assertEqual(report["cpu_percent"], 75.1)
        self.assertEqual(report["memory_bytes"], 6000 * 1024)
        self.assertEqual(report["swap_used_bytes"], 512 * 1024 * 1024)

    def test_swift_ui_discards_stale_snapshot_responses(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "apps/media_archive_image_video_ui/native_frontend.swift"
        ).read_text(encoding="utf-8")
        self.assertIn("private var snapshotGeneration = 0", source)
        self.assertIn("let requestedGeneration = snapshotGeneration", source)
        self.assertIn(
            "guard requestedGeneration == self.snapshotGeneration else { return }",
            source,
        )

    def test_swift_ui_clears_previous_search_coverage_for_new_query(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "apps/media_archive_image_video_ui/native_frontend.swift"
        ).read_text(encoding="utf-8")
        new_query = source[source.index("if !continuing {"):]
        new_query = new_query[:new_query.index("searchStatus = continuing")]
        self.assertIn("searchCoverage = nil", new_query)
        self.assertIn("if model.searching {", source)
        self.assertIn("本次扫描统计：正在计算当前搜索范围", source)
        self.assertIn("else if let coverage = model.searchCoverage", source)

    def test_person_cluster_catalog_is_query_only_and_anonymous(self) -> None:
        class FakeRepository:
            def person_cluster_catalog(self, offset, limit):
                self.call = (offset, limit)
                return {
                    "total": 1, "offset": offset, "limit": limit,
                    "items": [{
                        "person_cluster_id": "cluster-1",
                        "anonymous_display_name": "",
                        "member_count": 8,
                        "distinct_source_count": 3,
                        "cluster_confidence": "high",
                        "human_review_status": "unreviewed",
                        "preview_path": "/derived/face.jpg",
                        "media_type": "video",
                        "time_position_ms": 12000,
                    }],
                }

        repository = FakeRepository()
        report = run_person_cluster_catalog(repository, 0, 100)
        self.assertEqual(repository.call, (0, 100))
        self.assertEqual(report["items"][0]["display_name"], "匿名人物 01")
        self.assertFalse(report["database_write"])
        self.assertFalse(report["model_run"])
        self.assertIn("背影", report["capability_note"])

    def test_person_cluster_search_reads_existing_cluster_without_model(self) -> None:
        class FakeRepository:
            def person_cluster_results(
                self, cluster_id, media_type, offset, limit,
                source_content_id=None, extra_visual_unit_ids=(), extra_person_id="",
            ):
                self.call = (
                    cluster_id, media_type, offset, limit, source_content_id,
                    list(extra_visual_unit_ids), extra_person_id,
                )
                return {
                    "total": 1, "offset": 0, "limit": 30,
                    "next_offset": None, "count_by_media": {"video": 1},
                    "items": [{
                        "person_cluster_id": cluster_id,
                        "visual_unit_id": "visual-1",
                        "source_content_id": "source-1",
                        "derived_id": "derived-1",
                        "relative_path": "folder/source.mov",
                        "media_type": "video",
                        "time_position_ms": 12000,
                        "similarity_to_representative": 0.91,
                        "preview_path": "/derived/preview.jpg",
                        "source_path": "/source/source.mov",
                        "source_online": True,
                        "can_open_original": True,
                        "member_count": 3,
                        "distinct_source_count": 2,
                        "cluster_confidence": "high",
                        "human_review_status": "unreviewed",
                    }],
                }

        repository = FakeRepository()
        report = run_person_cluster_search(
            repository, "cluster-1", "all", 10000, 0, 30,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["database_write"])
        self.assertFalse(report["model_run"])
        self.assertEqual(report["result_count"], 1)
        self.assertEqual(
            report["result_items"][0]["hit_reason"], "same_person_reid",
        )
        self.assertEqual(
            report["result_items"][0]["preview_segment_start_timecode"],
            "00:07.000",
        )
        self.assertEqual(
            repository.call,
            ("cluster-1", "all", 0, 30, None, [], ""),
        )

    def test_favorite_collection_reuses_existing_annotations_without_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mov"; source.write_bytes(b"source")
            preview = root / "preview.jpg"; preview.write_bytes(b"preview")
            database = root / "media_archive.sqlite"
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,relative_path TEXT,absolute_path TEXT,media_type TEXT);"
                "CREATE TABLE user_asset_annotations(task_id TEXT,source_content_id TEXT,tags_json TEXT,note TEXT,favorite INTEGER,rating INTEGER,ignored INTEGER,updated_at REAL);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY,derived_path TEXT,time_position_ms INTEGER);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT,derived_id TEXT,time_position_ms INTEGER,near_black INTEGER);"
            )
            con.execute("INSERT INTO source_assets VALUES(?,?,?,?)", ("source-1", "folder/source.mov", str(source), "video"))
            con.execute("INSERT INTO user_asset_annotations VALUES(?,?,?,?,?,?,?,?)", ("task-1", "source-1", '["人物甲","采访"]', "优先剪辑", 1, 5, 0, 123.0))
            con.execute("INSERT INTO derived_assets VALUES(?,?,?)", ("derived-1", str(preview), 12000))
            con.execute("INSERT INTO visual_units VALUES(?,?,?,?,?)", ("visual-1", "source-1", "derived-1", -1, 0))
            con.commit(); con.close()

            with mock.patch.object(native_bridge, "task_id_for_database", return_value="task-1"):
                report = run_favorite_collection(ReadonlyMediaRepository(database), 0, 200)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["result_total_count"], 1)
        self.assertEqual(report["result_items"][0]["time_position_ms"], 12000)
        self.assertEqual(report["result_items"][0]["user_annotation"]["tags"], ["人物甲", "采访"])
        self.assertEqual(report["result_items"][0]["user_annotation"]["note"], "优先剪辑")
        self.assertFalse(report["database_write"])
        self.assertFalse(report["model_run"])
        self.assertFalse(report["original_media_read"])

    def test_object_label_reason_requires_concrete_label_evidence(self) -> None:
        payload = {
            "contract_version": "media_archive_search_result_v1",
            "query": "人",
            "result_count": 1,
            "result_items": [{
                "source_path": "image.jpg", "media_type": "image",
                "preview_path": "preview.jpg", "time_position_ms": 0,
                "hit_reason": "exact_object_label", "hit_field": "object_label",
                "score": 0.9, "source_online": True, "can_open_original": True,
                "relevance_reasons": ["exact_object_label"],
                "matched_object_labels": [],
            }],
        }
        with self.assertRaisesRegex(
            ValueError, "search_result_object_label_evidence_missing:0",
        ):
            _validate_search_result_contract(payload, "人")

    def test_subsecond_database_stage_is_not_displayed_as_zero_seconds(self) -> None:
        self.assertEqual(human_duration(0.05), "少于1秒")
        self.assertEqual(human_duration(0), "0秒")

    def test_saved_profile_and_new_task_read_the_same_application_path(self) -> None:
        args = argparse.Namespace(
            scheduler_mode="stage_serial", model_workers=3,
            frame_extract_workers=5, frame_interval_seconds=3.0,
            high_value_mode="frozen_v25_compatible", image_scope="all_images",
        )
        hardware = {
            "cpu_cores_total": 10, "unified_memory_gb": 32,
            "recommendation": {"model_workers": 3, "frame_extract_workers": 5},
        }
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            active_library = Path(temp) / "active-library"
            application_root = home / "Library/Application Support/素材大整理"
            with mock.patch(
                "media_archive_image_video_ui.native_bridge.application_state_root",
                return_value=application_root,
            ), mock.patch(
                "media_archive_image_video_ui.native_bridge.detect_hardware",
                return_value=hardware,
            ):
                report = save_profile({"output_root": str(active_library)}, args)
                loaded = _load_or_default_profile({"output_root": str(active_library)})

            self.assertEqual(loaded["high_value_policy"]["image_scope"], "all_images")
            self.assertTrue(Path(report["path"]).is_relative_to(application_root.resolve()))
            self.assertFalse((active_library / "profiles/processing_profile_v1.json").exists())

    def test_native_search_reuses_one_cache_and_leaves_no_per_query_job(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.outputs: list[Path] = []
                self.requests: list[dict[str, object]] = []

            def readiness(self) -> dict[str, bool]:
                return {"ready": True}

            def build_command(self, query, request, output):
                self.outputs.append(Path(output))
                self.requests.append(dict(request))
                return ["fake-search", query]

        class FakeRepository:
            def derived_path(self, derived_id):
                return Path("/derived") / derived_id

            def source_media(self, source_content_id):
                return {"available": True, "resolved_path": f"/source/{source_content_id}"}

        environments: list[dict[str, str]] = []

        def fake_run(command, log_path, env):
            environments.append(env)
            output = manager.outputs[-1] / "query5ev2_test" / "reports"
            output.mkdir(parents=True)
            query = command[-1]
            (output / "search_results.json").write_text(json.dumps({
                "contract_version": "media_archive_search_result_v1",
                "status": "PASS",
                "query": query,
                "result_count": 1,
                "result_total_count": 75,
                "result_offset": manager.requests[-1]["offset"],
                "result_limit": manager.requests[-1]["limit"],
                "next_result_offset": int(manager.requests[-1]["offset"]) + int(manager.requests[-1]["limit"]),
                "result_count_by_media": {"image": 75},
                "result_items": [{
                    "result_id": "result-1", "derived_id": "derived-1",
                    "source_content_id": "source-1", "media_type": "image",
                    "source_path": "image.jpg", "preview_path": "/derived/derived-1",
                    "time_position_ms": 0, "hit_reason": "visual_vector",
                    "hit_field": "visual_vector", "score": 0.8,
                    "source_online": None, "can_open_original": None,
                }],
            }), encoding="utf-8")
            (output / "search_summary.json").write_text(json.dumps({
                "status": "PASS", "eligible_visual_unit_count": 75,
                "scanned_visual_vector_count": 75, "scanned_text_vector_count": 20,
            }), encoding="utf-8")
            Path(log_path).write_text("fixture search passed\n", encoding="utf-8")
            return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app_state = root / "application-state"
            manager = FakeManager()
            with mock.patch(
                "media_archive_image_video_ui.native_bridge._run_streaming_search_command",
                fake_run,
            ), mock.patch(
                "media_archive_image_video_ui.native_bridge.application_state_root",
                return_value=app_state,
            ):
                first = run_search(
                    {"output_root": str(root)}, FakeRepository(), manager,
                    "小麦", "all", 10000,
                )
                second = run_search(
                    {"output_root": str(root)}, FakeRepository(), manager,
                    "河流", "image", 10000, 30, 30,
                )
                current = app_state / "cache/search_runtime/current"
                second_sizes = {
                    path.name: path.stat().st_size for path in current.iterdir()
                }
                third = run_search(
                    {"output_root": str(root)}, FakeRepository(), manager,
                    "河流", "image", 10000, 30, 30,
                )
                third_sizes = {
                    path.name: path.stat().st_size for path in current.iterdir()
                }

            self.assertEqual(first["status"], "PASS")
            self.assertEqual(second["status"], "PASS")
            self.assertEqual(third["status"], "PASS")
            self.assertTrue(all(env["PYTHONDONTWRITEBYTECODE"] == "1" for env in environments))
            self.assertTrue(all("PYTHONPYCACHEPREFIX" not in env for env in environments))
            self.assertFalse((root / "native_search_jobs").exists())
            self.assertFalse((root / "cache/search_runtime").exists())
            self.assertTrue(all(not output.exists() for output in manager.outputs))
            self.assertEqual(manager.requests[1]["media_type"], "image")
            self.assertEqual(manager.requests[1]["offset"], 30)
            self.assertEqual(second["result_total_count"], 75)
            self.assertEqual(second["next_result_offset"], 60)
            self.assertEqual(second["coverage"], {
                "eligible_visual_unit_count": 75,
                "scanned_visual_vector_count": 75,
                "scanned_text_vector_count": 20,
            })
            self.assertEqual(
                sorted(path.name for path in current.iterdir()),
                ["search.log", "search_results.json", "search_summary.json"],
            )
            self.assertEqual(set(second_sizes), set(third_sizes))
            self.assertLessEqual(third_sizes["search_results.json"], 2 * 1024 * 1024)
            self.assertLessEqual(third_sizes["search.log"], 32 * 1024)
            self.assertLess(sum(third_sizes.values()), 3 * 1024 * 1024)
            stable = json.loads((current / "search_results.json").read_text(encoding="utf-8"))
            self.assertEqual(stable["query"], "河流")
            self.assertEqual(stable["result_items"][0]["source_online"], True)
            summary = json.loads((current / "search_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["preview_files_copied"], 0)
            self.assertFalse(summary["temporary_query_directory_retained"])
            self.assertEqual(summary["storage_policy"], "bounded_replace_three_files_v1")
            self.assertTrue(summary["files_replaced_not_appended"])
            self.assertFalse(summary["cumulative_search_growth_expected"])
            self.assertEqual(summary["retained_file_count"], 3)
            self.assertIn("disk_audit", summary)

    def test_native_search_exit_two_returns_structured_bounded_error(self) -> None:
        class FakeManager:
            def readiness(self):
                return {"ready": True}

            def build_command(self, query, request, output):
                self.query = query
                self.output = Path(output)
                return ["fake-search"]

        class FakeRepository:
            def derived_path(self, _derived_id):
                return None

            def source_media(self, _source_content_id):
                return None

        manager = FakeManager()

        def fake_run(command, log_path, env):
            report = manager.output / "query5ev2_failed" / "reports"
            report.mkdir(parents=True)
            (report / "search_results.json").write_text(json.dumps({
                "contract_version": "media_archive_search_result_v1",
                "status": "FAIL", "query": manager.query, "result_count": 0,
                "result_total_count": 0, "result_offset": 0, "result_limit": 30,
                "next_result_offset": None, "result_count_by_media": {},
                "result_items": [],
            }), encoding="utf-8")
            (report / "search_summary.json").write_text(
                json.dumps({"status": "FAIL", "reason_code": "fixture_contract_failure"}),
                encoding="utf-8",
            )
            Path(log_path).write_text("x" * 200_000, encoding="utf-8")
            return 2

        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "media_archive_image_video_ui.native_bridge._run_streaming_search_command",
            fake_run,
        ):
            report = run_search(
                {
                    "output_root": temp,
                    "search_cache_root": str(Path(temp) / "search-cache"),
                },
                FakeRepository(), manager,
                "绿色人物", "all", 10000,
            )

            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["error_name"], "SEARCH_CONTRACT_VALIDATION_FAILED")
            self.assertIn("搜索退出码 2", report["error_reason"])
            self.assertTrue(Path(report["result_path"]).is_file())
            self.assertTrue(Path(report["log_path"]).is_file())
            self.assertLessEqual(len(report["diagnostic"].encode("utf-8")), 4000)
            self.assertLessEqual(Path(report["log_path"]).stat().st_size, 128 * 1024)

    def test_native_search_cancel_preserves_last_successful_diagnostics(self) -> None:
        class FakeManager:
            def readiness(self):
                return {"ready": True}

            def build_command(self, query, request, output):
                return ["fake-search"]

        class FakeRepository:
            pass

        def fake_run(command, log_path, env):
            Path(log_path).write_text("cancelled progress", encoding="utf-8")
            return 143

        with tempfile.TemporaryDirectory() as temp:
            search_root = Path(temp) / "search-cache"
            current = search_root / "current"
            current.mkdir(parents=True)
            expected = {
                "search_results.json": b'{"status":"PASS","result_count":1}',
                "search_summary.json": b'{"status":"PASS"}',
                "search.log": b"last successful search",
            }
            for name, content in expected.items():
                (current / name).write_bytes(content)

            with mock.patch(
                "media_archive_image_video_ui.native_bridge._run_streaming_search_command",
                fake_run,
            ):
                report = run_search(
                    {
                        "output_root": temp,
                        "search_cache_root": str(search_root),
                    },
                    FakeRepository(), FakeManager(),
                    "取消测试", "all", 10000,
                )

            self.assertEqual(report["status"], "CANCELLED")
            self.assertEqual(report["error_name"], "SEARCH_CANCELLED")
            self.assertEqual(report["exit_code"], 143)
            self.assertFalse(report["database_write"])
            for name, content in expected.items():
                self.assertEqual((current / name).read_bytes(), content)

    def test_native_search_zero_results_is_success(self) -> None:
        class FakeManager:
            def readiness(self):
                return {"ready": True}

            def build_command(self, query, request, output):
                self.query = query
                self.output = Path(output)
                return ["fake-search"]

        class FakeRepository:
            def derived_path(self, _derived_id):
                return None

            def source_media(self, _source_content_id):
                return None

        manager = FakeManager()

        def fake_run(command, log_path, env):
            report = manager.output / "query5ev2_empty" / "reports"
            report.mkdir(parents=True)
            (report / "search_results.json").write_text(json.dumps({
                "contract_version": "media_archive_search_result_v1",
                "status": "PASS", "query": manager.query, "result_count": 0,
                "result_total_count": 0, "result_offset": 0, "result_limit": 30,
                "next_result_offset": None, "result_count_by_media": {},
                "result_items": [],
            }), encoding="utf-8")
            (report / "search_summary.json").write_text(
                json.dumps({"status": "PASS"}), encoding="utf-8",
            )
            Path(log_path).write_text("fixture empty result\n", encoding="utf-8")
            return 0

        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "media_archive_image_video_ui.native_bridge._run_streaming_search_command",
            fake_run,
        ):
            report = run_search(
                {
                    "output_root": temp,
                    "search_cache_root": str(Path(temp) / "search-cache"),
                },
                FakeRepository(), manager,
                "不存在的对象", "all", 10000,
            )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["result_count"], 0)
        self.assertEqual(report["result_items"], [])

    def test_existing_library_is_presented_by_human_name_not_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / "opaque_task_93ad18"
            root.mkdir()
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT,media_type TEXT);"
                "INSERT INTO source_assets VALUES('i1','image'),('i2','image'),('v1','video');"
            )
            con.close()
            state = root / "state.json"
            state.write_text(json.dumps({"status": "success"}), encoding="utf-8")
            task = root / "task.json"
            task.write_text(json.dumps({
                "task_id": "task_opaque", "name": "西湖素材整理", "database": str(db),
                "source_root": "/Volumes/素材/西湖", "created_at": "2026-07-20T10:00:00+0800",
                "state_path": str(state), "status": "success",
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                rows = existing_libraries({"task_path": str(task)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_name"], "西湖素材整理")
        self.assertEqual((rows[0]["image_count"], rows[0]["video_count"]), (2, 1))
        self.assertNotIn("opaque_task_93ad18", rows[0]["task_name"])

    def test_known_index_root_discovers_siblings_and_activation_switches_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; home.mkdir()
            index_root = Path(temp) / "index"
            tasks_root = index_root / "tasks"

            def create_task(directory: str, name: str) -> Path:
                task_dir = tasks_root / directory; task_dir.mkdir(parents=True)
                workspace = task_dir / "workspace"; workspace.mkdir()
                database = workspace / "media_archive.sqlite"
                con = sqlite3.connect(database)
                con.executescript(
                    "CREATE TABLE source_assets(source_content_id TEXT,media_type TEXT);"
                    "INSERT INTO source_assets VALUES('image-1','image'),('video-1','video');"
                )
                con.close()
                state_path = task_dir / "pipeline_state.json"
                state_path.write_text(json.dumps({"status": "success"}), encoding="utf-8")
                task_path = task_dir / "task.json"
                task_path.write_text(json.dumps({
                    "task_id": directory, "name": name, "database": str(database),
                    "workspace": str(workspace), "state_path": str(state_path),
                    "created_at": "2026-08-06T10:00:00+0800",
                }), encoding="utf-8")
                return task_path

            first = create_task("20260801_001", "001项目")
            second = create_task("20260802_wheat", "麦子熟了")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                libraries = existing_libraries({"task_path": str(first)})
                report = activate_library({"task_path": str(first)}, second)
                active = json.loads(
                    (home / "Library/Application Support/素材大整理/runtime/active_library.json").read_text()
                )
                switched = existing_libraries(active)

        self.assertEqual({row["task_name"] for row in libraries}, {"001项目", "麦子熟了"})
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(Path(active["task_path"]), second.resolve())
        self.assertEqual([row["task_name"] for row in switched if row["is_active"]], ["麦子熟了"])

    def test_historical_task_detail_reads_selected_task_not_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "history.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "INSERT INTO source_assets VALUES('i1','image'),('v1','video');"
            )
            con.close()
            state = root / "pipeline_state.json"
            state.write_text(json.dumps({
                "status": "success", "stage_count": 1, "completed_stage_count": 1,
                "started_at_epoch": 1, "finished_at_epoch": 2,
                "stages": [{"key": "scan", "status": "success", "elapsed_seconds": 0.2}],
            }), encoding="utf-8")
            task = root / "task.json"
            task.write_text(json.dumps({
                "task_id": "historical-task", "name": "旧素材任务",
                "database": str(db), "state_path": str(state),
                "source_root": "/Volumes/旧素材", "created_at": "2026-07-01",
            }), encoding="utf-8")

            report = task_detail(task)

        self.assertEqual(report["task_id"], "historical-task")
        self.assertEqual(report["task_name"], "旧素材任务")
        self.assertEqual(report["elapsed_seconds"], 1.0)
        self.assertEqual(report["elapsed_human"], "1秒")
        self.assertGreater(report["index_storage"]["total_bytes"], 0)
        self.assertEqual(report["index_storage"]["total_file_count"], 3)
        self.assertFalse(report["index_storage"]["source_root_scanned"])
        self.assertEqual(report["pipeline"]["stages"][0]["done"], 2)
        self.assertIn("少于1秒", report["pipeline"]["stages"][0]["description"])
        self.assertFalse(report["database_write"])

    def test_historical_task_detail_infers_legacy_failure_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "history.sqlite"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE source_assets("
                "source_content_id TEXT PRIMARY KEY,media_type TEXT)"
            )
            con.close()
            (root / "logs").mkdir()
            log = root / "logs" / "pipeline.log"
            log.write_text("legacy failure\n", encoding="utf-8")
            state = root / "pipeline_state.json"
            state.write_text(json.dumps({
                "status": "failed",
                "error": "阶段失败：图片预览与延时摄影分组（exit 1）",
                "current_stage_key": "image_preview",
                "current_stage_name": "图片预览与延时摄影分组",
                "stage_count": 2,
                "completed_stage_count": 1,
                "stages": [
                    {"key": "scan", "name": "素材扫描", "status": "success"},
                    {"key": "image_preview", "name": "图片预览与延时摄影分组",
                     "status": "pending"},
                ],
            }), encoding="utf-8")
            task = root / "task.json"
            task.write_text(json.dumps({
                "task_id": "legacy-failed", "name": "旧版失败任务",
                "database": str(db), "state_path": str(state),
                "source_root": "/Volumes/旧素材", "created_at": "2026-07-01",
                "status": "failed",
            }), encoding="utf-8")

            report = task_detail(task)

        self.assertEqual(
            report["pipeline"]["failed_stage_name"],
            "图片预览与延时摄影分组",
        )
        self.assertIn("阶段失败", report["pipeline"]["error_summary"])
        self.assertEqual(
            Path(report["pipeline"]["error_log_path"]).resolve(),
            log.resolve(),
        )
        self.assertIn("阶段失败", report["error"])

    def test_task_pipeline_uses_real_metrics_and_human_duration(self) -> None:
        state = {
            "status": "success", "stage_count": 2, "completed_stage_count": 2,
            "stages": [
                {"key": "qwen_optional_v2", "status": "success", "elapsed_seconds": 907.6},
                {"key": "visual_schema_v3", "status": "success", "elapsed_seconds": 0.1},
            ],
        }
        result = task_pipeline(state, metrics={
            "qwen_optional_v2": {"done": 205, "total": 205, "description": "高价值画面"},
            "visual_schema_v3": {"done": 0, "total": 0, "description": "数据库结构准备阶段"},
        })
        self.assertEqual(result["stages"][0]["name"], "高价值画面描述（Qwen-VL）")
        self.assertEqual((result["stages"][0]["done"], result["stages"][0]["total"]), (205, 205))
        self.assertIn("15分8秒", result["stages"][0]["description"])
        self.assertEqual(result["stages"][1]["total"], 0)
        self.assertEqual(human_duration(3662), "1小时1分2秒")

    def test_all_image_supplement_stage_has_user_facing_name_and_dynamic_count(self) -> None:
        result = task_pipeline({
            "status": "running", "stage_count": 1,
            "stages": [{"key": "all_image_supplement_qwen", "status": "running"}],
        }, metrics={
            "all_image_supplement_qwen": {
                "done": 17, "total": 302,
                "description": "用户选择全部图片后新增分析 17/302 张",
            },
        })
        stage = result["stages"][0]
        self.assertEqual(stage["name"], "补充其余图片描述（Qwen-VL）")
        self.assertEqual((stage["done"], stage["total"]), (17, 302))

    def test_task_level_eta_does_not_average_unlike_stages(self) -> None:
        state = {
            "status": "running", "task_id": "task-1", "stage_count": 15,
            "completed_stage_count": 9, "current_stage_name": "Qwen",
            "stages": [{"status": "success", "elapsed_seconds": 10.0}] * 9,
        }
        run = task_active_run(state)
        assert run is not None
        self.assertEqual(run["elapsed_seconds"], 90.0)
        self.assertIsNone(run["eta_seconds"])

    def test_overall_eta_waits_for_observed_live_throughput(self) -> None:
        state = {"stages": [
            {"key": "qwen_optional_v2", "status": "running"},
            {"key": "ocr_optional_v2", "status": "pending"},
            {"key": "embedding_optional_v2", "status": "pending"},
        ]}
        metrics = {
            "qwen_optional_v2": {"done": 10, "total": 20},
            "ocr_optional_v2": {"done": 0, "total": 30},
            "embedding_optional_v2": {"done": 0, "total": 0},
        }
        task = {
            "profile": {"scheduler": {"model_workers": 2}},
            "runtime": {"ocr_workers": 3, "embedding_workers": 3},
        }
        eta = estimate_task_remaining(state, metrics, task)
        self.assertIsNone(eta["overall_eta_seconds"])
        self.assertIn("不做虚假预测", eta["overall_eta_basis"])

        state["stages"][0].update({
            "eta_seconds": 42.5,
            "eta_basis": "按本阶段当前实际吞吐量估算",
        })
        observed = estimate_task_remaining(state, metrics, task)
        self.assertEqual(observed["overall_eta_seconds"], 42.5)
        self.assertIn("当前阶段估算", observed["overall_eta_basis"])

    def test_completed_task_exposes_missing_video_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "INSERT INTO source_assets VALUES('video1','video');"
            )
            con.close()
            task = {"database": str(db)}
            task_path = root / "task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            errors = task_output_acceptance(
                {"task_path": str(task_path)}, {"status": "success"},
            )
        self.assertEqual(errors["video_frames"], "VIDEO_INPUT_WITHOUT_DERIVED_FRAMES")

    def test_successful_maintenance_task_does_not_use_full_pipeline_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_path = Path(temp) / "task.json"
            task_path.write_text(
                json.dumps({"mode": "rebuild_search"}), encoding="utf-8",
            )
            errors = task_output_acceptance(
                {"task_path": str(task_path)}, {"status": "success"},
            )
        self.assertEqual(errors, {})

    def test_rebuild_task_uses_real_library_readiness_and_offers_incremental_resume(self) -> None:
        pipeline = reconcile_task_pipeline_with_library(
            {
                "search_ready": True,
                "failed_record_count": 1,
                "full_pipeline_launcher_status": "SUCCESS",
            },
            {"mode": "rebuild_search"},
            {"ready": False},
            {"failed_record_count": 0},
        )
        self.assertFalse(pipeline["search_ready"])
        self.assertEqual(pipeline["failed_record_count"], 0)
        self.assertEqual(
            pipeline["full_pipeline_launcher_status"],
            "MAINTENANCE_SEARCH_INCOMPLETE",
        )

    def test_successful_old_rebuild_can_resume_a_new_missing_plan_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "pipeline_state.json"
            state_path.write_text(json.dumps({
                "status": "success",
                "stages": [
                    {"key": "rescan", "status": "success"},
                    {"key": "rebuild_openclip", "status": "success"},
                ],
            }), encoding="utf-8")
            task_path = root / "task.json"
            task_path.write_text(json.dumps({
                "task_id": "rebuild_1",
                "state_path": str(state_path),
                "log_path": str(root / "pipeline.log"),
                "status": "success",
            }), encoding="utf-8")
            with mock.patch(
                "media_archive_image_video_ui.native_bridge.build_stage_plan",
                return_value=[
                    {"key": "rescan"},
                    {"key": "restore_lineage"},
                    {"key": "rebuild_openclip"},
                ],
            ), mock.patch(
                "media_archive_image_video_ui.native_bridge.validate_stage_acceptance",
                return_value="",
            ), mock.patch(
                "media_archive_image_video_ui.native_bridge._launch_task_worker",
                return_value=4321,
            ):
                report = resume_active_task(
                    {"task_path": str(task_path)}, root / "app_config.json",
                )
            updated_task = json.loads(task_path.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "PASS")
        self.assertIn("断点继续", report["message"])
        self.assertEqual(updated_task["status"], "queued")

    def test_running_task_blocks_duplicate_start_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            runtime = home / "Library/Application Support/素材大整理/runtime"
            runtime.mkdir(parents=True)
            state = runtime / "state.json"
            state.write_text(json.dumps({"status": "running"}), encoding="utf-8")
            (runtime / "active_library.json").write_text(
                json.dumps({"task_state_path": str(state)}), encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(RuntimeError, "不能重复启动"):
                    start_task({}, root := Path("/missing/config"), argparse.Namespace())

    def test_repair_selects_human_library_and_creates_separate_maintenance_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"; home.mkdir()
            source = root / "source"; source.mkdir()
            task_dir = root / "20260720_西湖素材整理"; task_dir.mkdir()
            workspace = task_dir / "workspace"; workspace.mkdir()
            db = workspace / "media_archive.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT,media_type TEXT);"
                "INSERT INTO source_assets VALUES('i1','image'),('v1','video');"
            )
            con.close()
            state_path = task_dir / "pipeline_state.json"
            state_path.write_text(json.dumps({"status": "success"}), encoding="utf-8")
            library_task_path = task_dir / "task.json"
            library_task_path.write_text(json.dumps({
                "task_id": "library_task", "name": "西湖素材整理", "database": str(db),
                "source_root": str(source), "workspace": str(workspace),
                "state_path": str(state_path), "status": "success",
                "profile": {"scheduler": {"mode": "stage_serial", "model_workers": 3,
                                             "frame_extract_workers": 4}},
            }), encoding="utf-8")
            fake_contract = {
                "contract_version": "media_archive_app_runtime_contract_v1",
                "contract_path": str(root / "contract.json"), "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "tools": {"ffmpeg": "/tool/ffmpeg", "ffprobe": "/tool/ffprobe", "sips": "/usr/bin/sips"},
                "models": {key: {"path": f"/model/{key}"} for key in (
                    "yoloe", "yoloe_mobileclip", "openclip", "qwen", "ocr_detection",
                    "ocr_recognition", "text_embedding"
                )},
                "scripts": {}, "configs": {}, "migrations": {}, "policies": {},
            }
            hardware = {"recommendation": {"ocr_workers": 2, "embedding_workers": 3}}
            args = argparse.Namespace(task=str(library_task_path), task_mode="repair")
            with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                 mock.patch("media_archive_image_video_ui.native_bridge.validate_runtime_contract",
                            return_value={"ready": True, "missing": [], "errors": []}), \
                 mock.patch("media_archive_image_video_ui.native_bridge.load_runtime_contract",
                            return_value=fake_contract), \
                 mock.patch("media_archive_image_video_ui.native_bridge.detect_hardware",
                            return_value=hardware), \
                 mock.patch("media_archive_image_video_ui.native_bridge._launch_task_worker",
                            return_value=4321):
                report = start_existing_task(
                    {"task_path": str(library_task_path), "runtime_contract_path": str(root / "contract.json")},
                    root / "app_config.json", args,
                )
                active = json.loads((home / "Library/Application Support/素材大整理/runtime/active_library.json").read_text())
            maintenance_task = json.loads(Path(report["path"]).read_text())

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(maintenance_task["mode"], "repair")
        self.assertEqual(Path(maintenance_task["library_task_path"]), library_task_path.resolve())
        self.assertEqual(Path(maintenance_task["database"]), db.resolve())
        self.assertEqual(Path(maintenance_task["workspace"]), workspace.resolve())
        self.assertTrue(Path(maintenance_task["stage_output_root"]).is_relative_to(workspace.resolve()))
        self.assertEqual(Path(active["library_task_path"]), library_task_path.resolve())
        self.assertEqual(active["task_path"], report["path"])


if __name__ == "__main__":
    unittest.main()
