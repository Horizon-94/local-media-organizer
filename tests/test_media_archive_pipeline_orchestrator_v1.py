from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))
TEST_MODELS = {
    "yoloe": "/model/yoloe.pt",
    "yoloe_mobileclip": "/model/mobileclip.ts",
    "openclip": "/model/openclip.safetensors",
    "qwen": "/model/qwen",
}

from media_archive_image_video_ui.pipeline_orchestrator import (  # noqa: E402
    _open_acceptance_database,
    build_stage_plan,
    command_for_resume,
    execute_pipeline,
    offline_environment,
    qwen_database_progress,
    required_runtime_path,
    runtime_model_root,
    summarize_stage_failure,
    validate_stage_acceptance,
)


def load_script_namespace(relative: str) -> dict[str, object]:
    script = ROOT / relative
    namespace: dict[str, object] = {"__file__": str(script), "__name__": "test_script"}
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
    return namespace


def write_task(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    log_path = root / "logs" / "pipeline.log"
    task = {
        "task_id": "task_fake_e2e",
        "name": "fake end to end",
        "workspace": str(workspace),
        "database": str(workspace / "media_archive.sqlite"),
        "source_root": str(root / "source"),
        "state_path": str(root / "pipeline_state.json"),
        "log_path": str(log_path),
        "runtime": {"project_root": str(ROOT)},
    }
    path = root / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


def marker_stage(key: str, marker: Path, *, exit_code: int = 0) -> dict[str, object]:
    code = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text({key!r}, encoding='utf-8'); "
        f"raise SystemExit({exit_code})"
    )
    return {"key": key, "name": key, "command": [sys.executable, "-c", code]}


def fake_inventory_plan(root: Path, stage_count: int) -> list[dict[str, object]]:
    db = root / "workspace" / "media_archive.sqlite"
    scan_code = (
        "import sqlite3; "
        f"con=sqlite3.connect({str(db)!r}); "
        "con.executescript(\"CREATE TABLE source_assets("
        "source_content_id TEXT PRIMARY KEY,media_type TEXT NOT NULL);"
        "\" + ''.join("
        "\"INSERT INTO source_assets VALUES('image%d','image');\" % i for i in range(10)"
        ") + ''.join("
        "\"INSERT INTO source_assets VALUES('video%d','video');\" % i for i in range(2)"
        ")); con.close()"
    )
    image_code = (
        "import sqlite3; "
        f"con=sqlite3.connect({str(db)!r}); "
        "con.execute(\"CREATE TABLE visual_units("
        "visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT NOT NULL)\"); "
        "con.executemany(\"INSERT INTO visual_units VALUES(?,?)\", "
        "[(\"image-vu-%d\"%i,\"image%d\"%i) for i in range(10)]); "
        "con.commit(); con.close()"
    )
    video_code = (
        "import sqlite3; "
        f"con=sqlite3.connect({str(db)!r}); "
        "con.executemany(\"INSERT INTO visual_units VALUES(?,?)\", "
        "[(\"video-vu-%d\"%i,\"video%d\"%i) for i in range(2)]); "
        "con.commit(); con.close()"
    )
    plan: list[dict[str, object]] = [
        {"key": "scan", "name": "scan", "command": [sys.executable, "-c", scan_code]},
        {
            "key": "image_preview", "name": "image_preview",
            "command": [sys.executable, "-c", image_code],
        },
        {
            "key": "video_frames", "name": "video_frames",
            "command": [sys.executable, "-c", video_code],
        },
    ]
    for index in range(3, stage_count):
        plan.append(marker_stage(f"stage_{index + 1}", root / f"stage_{index + 1}.txt"))
    return plan


class PipelineOrchestratorTests(unittest.TestCase):
    def test_runtime_paths_never_fall_back_to_developer_home(self) -> None:
        self.assertEqual(
            required_runtime_path({"qwen": "/portable/models/qwen"}, "qwen", "models"),
            "/portable/models/qwen",
        )
        with self.assertRaisesRegex(ValueError, "missing_runtime_path:models.qwen"):
            required_runtime_path({}, "qwen", "models")

    def test_qwen_resume_reuses_existing_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            out = workspace / "stages" / "11_qwen_optional_v2"
            out.mkdir(parents=True)
            (out / "run_id.txt").write_text("run_existing\n", encoding="utf-8")
            stage = {
                "key": "qwen_optional_v2",
                "command": [
                    "wrapper", "--", "qwen", "--mode", "run", "--out", str(out),
                ],
            }
            command = command_for_resume(stage, workspace)
            self.assertEqual(command[command.index("--mode") + 1], "resume")
            self.assertEqual(command[command.index("--run-id") + 1], "run_existing")
            self.assertIn("--confirm-compatible-script-resume", command)

    def test_qwen_database_progress_counts_commits_and_live_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "db.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE stop03_3_qwenvl_run_items(run_id TEXT,status TEXT)")
            con.executemany(
                "INSERT INTO stop03_3_qwenvl_run_items VALUES('run1',?)",
                [("success",), ("success",), ("pending",), ("failed",)],
            )
            con.commit()
            con.close()
            workers = root / "worker_status"
            workers.mkdir()
            (workers / "worker_1.json").write_text(
                json.dumps({"lifecycle": "running"}), encoding="utf-8"
            )
            progress = qwen_database_progress(db, root, "run1")
            self.assertEqual(progress["completed"], 3)
            self.assertEqual(progress["total"], 4)
            self.assertEqual(progress["success"], 2)
            self.assertEqual(progress["failed"], 1)
            self.assertEqual(progress["actual_workers"], 1)

    def test_failure_summary_prefers_exception_over_traceback_header(self) -> None:
        summary, _details = summarize_stage_failure([
            "Traceback (most recent call last):",
            "FileExistsError: already exists",
        ], 1)
        self.assertEqual(summary, "FileExistsError: already exists")

    def test_external_stage_environment_does_not_inherit_embedded_python_home(self) -> None:
        previous = {key: os.environ.get(key) for key in (
            "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__",
        )}
        try:
            for key in previous:
                os.environ[key] = f"/embedded/{key.lower()}"
            environment = offline_environment(Path("/tmp/library"))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        for key in previous:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")

    def test_external_stage_environment_exposes_fixed_ffmpeg_tools(self) -> None:
        environment = offline_environment(Path("/tmp/library"), {
            "ffmpeg": "/opt/homebrew/bin/ffmpeg",
            "ffprobe": "/opt/homebrew/bin/ffprobe",
        })
        self.assertTrue(environment["PATH"].startswith("/opt/homebrew/bin"))

    def test_external_stage_environment_forces_model_runtimes_offline(self) -> None:
        environment = offline_environment(
            Path("/tmp/library"), model_root=Path("/example/models")
        )
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(environment["HF_DATASETS_OFFLINE"], "1")
        self.assertEqual(environment["ULTRALYTICS_OFFLINE"], "1")
        self.assertEqual(environment["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"], "True")
        self.assertEqual(environment["HF_HOME"], "/tmp/library/cache/huggingface")
        self.assertEqual(environment["TORCH_HOME"], "/tmp/library/cache/torch")
        self.assertEqual(environment["MEDIA_ARCHIVE_MODEL_ROOT"], "/example/models")

    def test_existing_task_infers_model_root_from_model_paths(self) -> None:
        runtime = {
            "models": {
                "yoloe": "/example/models/yolo/weights/model.pt",
                "qwen": "/example/models/qwen/model",
                "ocr": "/example/models/ocr/model",
            }
        }
        self.assertEqual(runtime_model_root(runtime), Path("/example/models"))

    def test_video_stage_cannot_pass_when_all_video_frames_are_missing(self) -> None:
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
            reason = validate_stage_acceptance({"database": str(db)}, "video_frames")
        self.assertEqual(reason, "VIDEO_INPUT_WITHOUT_DERIVED_FRAMES")

    def test_acceptance_database_falls_back_to_immutable_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "library.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE source_assets(source_content_id TEXT)")
            real_connect = sqlite3.connect
            opened: list[str] = []

            def flaky_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
                uri = str(database)
                opened.append(uri)
                if uri.endswith("?mode=ro"):
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(database, *args, **kwargs)

            with mock.patch(
                "media_archive_image_video_ui.pipeline_orchestrator.sqlite3.connect",
                side_effect=flaky_connect,
            ):
                with _open_acceptance_database(db) as con:
                    self.assertEqual(
                        con.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0], 0,
                    )
        self.assertTrue(opened[0].endswith("?mode=ro"))
        self.assertTrue(opened[1].endswith("?mode=ro&immutable=1"))

    def test_stage_plan_uses_each_frozen_runtime(self) -> None:
        task = {
            "workspace": "/tmp/library",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {
                "scheduler": {"model_workers": 3, "frame_extract_workers": 4},
                "video_sampling": {"frame_interval_seconds": 2},
                "high_value_policy": {"mode": "target_20"},
            },
            "runtime": {
                "project_root": str(ROOT),
                "python": {
                    "system": "/runtime/system",
                    "visual": "/runtime/visual",
                    "yolo": "/runtime/yolo",
                    "person_reid": "/runtime/person-reid",
                    "qwen": "/runtime/qwen",
                    "ocr": "/runtime/ocr",
                    "embedding": "/runtime/embedding",
                },
                "models": TEST_MODELS,
                "ocr_workers": 2,
                "embedding_workers": 2,
            },
        }
        plan = {stage["key"]: stage for stage in build_stage_plan(task)}
        self.assertEqual(plan["scan"]["command"][0], "/runtime/visual")
        self.assertIn("--hash-all", plan["scan"]["command"])
        self.assertEqual(plan["image_preview"]["command"][0], "/runtime/visual")
        self.assertEqual(plan["video_frames"]["command"][0], "/runtime/visual")
        self.assertIn(
            "step02_video_frame_generic_interval_v1.py",
            plan["video_frames"]["command"][
                plan["video_frames"]["command"].index("--script") + 1
            ],
        )
        self.assertEqual(
            plan["video_frames"]["command"][
                plan["video_frames"]["command"].index("--frame-interval-seconds") + 1
            ],
            "2",
        )
        self.assertEqual(
            plan["candidates_generic_v2"]["command"][
                plan["candidates_generic_v2"]["command"].index("--high-value-mode") + 1
            ],
            "target_20",
        )
        self.assertEqual(plan["visual_schema_v3"]["command"][0], "/runtime/visual")
        self.assertEqual(plan["yoloe"]["command"][0], "/runtime/yolo")
        self.assertEqual(plan["openclip"]["command"][0], "/runtime/visual")
        self.assertEqual(plan["person_reid_optional_v1"]["command"][0], "/runtime/person-reid")
        self.assertIn(
            "stop03_1c_person_reid_db_orchestrator_v1.py",
            " ".join(plan["person_reid_optional_v1"]["command"]),
        )
        self.assertIn("/runtime/qwen", plan["qwen_optional_v2"]["command"])
        self.assertIn("/runtime/ocr", plan["ocr_optional_v2"]["command"])
        self.assertIn(
            "--run-delegate-on-empty", plan["ocr_optional_v2"]["command"]
        )
        self.assertNotIn(
            "--run-delegate-on-empty", plan["qwen_optional_v2"]["command"]
        )
        self.assertIn("/runtime/embedding", plan["embedding_optional_v2"]["command"])

    def test_all_images_profile_adds_generic_supplement_without_changing_v25(self) -> None:
        task = {
            "workspace": "/tmp/library", "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {
                "scheduler": {"model_workers": 3, "frame_extract_workers": 4},
                "video_sampling": {"frame_interval_seconds": 3},
                "high_value_policy": {
                    "mode": "target_15",
                    "image_scope": "all_images",
                },
            },
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
            },
        }
        plan = build_stage_plan(task)
        keys = [stage["key"] for stage in plan]
        self.assertEqual(keys.count("candidates_generic_v2"), 1)
        self.assertIn("all_image_supplement_contract", keys)
        self.assertIn("all_image_supplement_qwen", keys)
        self.assertIn("all_image_evidence_merge", keys)
        contract = next(row for row in plan if row["key"] == "all_image_supplement_contract")
        self.assertIn("all-image-visual-units", contract["command"])
        self.assertLess(keys.index("candidate_snapshot"), keys.index("all_image_supplement_contract"))
        self.assertLess(keys.index("qwen_optional_v2"), keys.index("all_image_supplement_qwen"))
        self.assertLess(keys.index("evidence_optional_v2"), keys.index("all_image_evidence_merge"))

    def test_repair_reuses_all_images_and_density_profile(self) -> None:
        task = {
            "mode": "repair_images",
            "workspace": "/tmp/library",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {
                "scheduler": {"model_workers": 3, "frame_extract_workers": 4},
                "video_sampling": {"frame_interval_seconds": 2},
                "high_value_policy": {
                    "mode": "target_30",
                    "image_scope": "all_images",
                },
            },
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
            },
        }
        plan = build_stage_plan(task)
        candidate = next(row for row in plan if row["key"] == "repair_candidate_dry_run")
        self.assertEqual(
            candidate["command"][candidate["command"].index("--high-value-mode") + 1],
            "target_30",
        )
        supplement = next(row for row in plan if row["key"] == "repair_supplement_contract")
        self.assertEqual(
            supplement["command"][supplement["command"].index("--selection-mode") + 1],
            "all-image-visual-units",
        )
        self.assertNotIn("--candidate-manifest", supplement["command"])

    def test_stage_plan_uses_runtime_contract_script_and_config_maps(self) -> None:
        task = {
            "workspace": "/tmp/library",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {"scheduler": {"model_workers": 3, "frame_extract_workers": 4}},
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
                "scripts": {
                    "stage_runner": "/contract/stage_runner.py",
                    "source_scan": "/contract/source_scan.py",
                    "candidate_select": "/contract/candidates.py",
                },
                "configs": {"candidate": "/contract/candidate.json"},
            },
        }
        plan = {stage["key"]: stage for stage in build_stage_plan(task)}
        self.assertEqual(plan["scan"]["command"][1], "/contract/stage_runner.py")
        self.assertIn("/contract/source_scan.py", plan["scan"]["command"])
        self.assertEqual(plan["candidates_generic_v2"]["command"][1], "/contract/candidates.py")
        self.assertIn("/contract/candidate.json", plan["candidates_generic_v2"]["command"])

    def test_repair_stage_plan_only_supplements_missing_image_semantics(self) -> None:
        task = {
            "mode": "repair_images",
            "workspace": "/tmp/library",
            "stage_output_root": "/tmp/library/maintenance/repair_1/stages",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {"scheduler": {"model_workers": 3, "frame_extract_workers": 4}},
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
                "embedding_workers": 3,
            },
        }
        plan = build_stage_plan(task)
        keys = [stage["key"] for stage in plan]
        self.assertEqual(keys, [
            "repair_finder_tags", "repair_candidate_dry_run",
            "repair_supplement_contract", "repair_supplement_qwen",
            "repair_evidence_merge", "repair_propagation", "repair_embedding",
        ])
        all_arguments = "\n".join(" ".join(stage["command"]) for stage in plan)
        self.assertNotIn("stop03_4_ocr", all_arguments)
        self.assertNotIn("--clear-existing-candidate-items", all_arguments)
        self.assertIn("--mode dry-run", all_arguments)
        self.assertIn("20260720_stop03_3_qwenvl_supplement_v1.sql", all_arguments)
        self.assertIn("/tmp/library/maintenance/repair_1/stages", all_arguments)

    def test_rebuild_search_uses_existing_database_without_models_or_source_scan(self) -> None:
        task = {
            "mode": "rebuild_search",
            "workspace": "/tmp/library",
            "stage_output_root": "/tmp/library/maintenance/rebuild_1/stages",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {"scheduler": {"model_workers": 3, "frame_extract_workers": 4}},
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
            },
        }
        plan = build_stage_plan(task)
        self.assertEqual([stage["key"] for stage in plan], ["rebuild_search_index"])
        command_text = "\n".join(" ".join(stage["command"]) for stage in plan)
        self.assertIn("rebuild_search_index_from_database_v1.py", command_text)
        self.assertIn("--confirm-central-db-write", command_text)
        self.assertNotIn("source_scan", command_text)
        for forbidden in (
            "stop03_yoloe", "stop03_3f_qwenvl",
            "stop03_4_ocr_db", "stop03_5d_text_embedding_db",
        ):
            self.assertNotIn(forbidden, command_text.lower())

    def test_general_repair_checks_every_stage_without_clearing_successes(self) -> None:
        task = {
            "mode": "repair",
            "workspace": "/tmp/library",
            "stage_output_root": "/tmp/library/maintenance/repair_all/stages",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {
                "scheduler": {"model_workers": 3, "frame_extract_workers": 4},
                "video_sampling": {"frame_interval_seconds": 3},
            },
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": {
                    "yoloe": "/model/yoloe.pt",
                    "yoloe_mobileclip": "/model/mobileclip.ts",
                    "openclip": "/model/openclip.safetensors",
                    "qwen": "/model/qwen",
                },
            },
        }
        plan = build_stage_plan(task)
        keys = [stage["key"] for stage in plan]
        self.assertEqual(keys[0], "scan")
        self.assertIn("修复缺失扫描", plan[0]["name"])
        self.assertIn("video_frames", keys)
        self.assertIn("yoloe", keys)
        self.assertIn("qwen_optional_v2", keys)
        self.assertIn("embedding_optional_v2", keys)
        command_text = "\n".join(" ".join(stage["command"]) for stage in plan)
        self.assertNotIn("--clear-existing-candidate-items", command_text)
        self.assertIn("--limit-new 0", command_text)

    def test_only_source_dependent_stages_require_the_original_volume(self) -> None:
        task = {
            "workspace": "/tmp/library",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/Volumes/source/library",
            "profile": {
                "scheduler": {"model_workers": 3, "frame_extract_workers": 4},
                "video_sampling": {"frame_interval_seconds": 3},
                "high_value_policy": {"mode": "frozen_v25_compatible"},
            },
            "runtime": {
                "project_root": str(ROOT),
                "python": {
                    key: "/runtime/python" for key in
                    ("system", "visual", "yolo", "qwen", "ocr", "embedding")
                },
                "models": TEST_MODELS,
            },
        }
        plan = build_stage_plan(task)
        source_dependent = {"scan", "image_preview", "video_frames"}
        for stage in plan:
            has_source_gate = "--allowed-source-root" in stage["command"]
            self.assertEqual(
                has_source_gate,
                stage["key"] in source_dependent,
                stage["key"],
            )

    def test_incremental_plan_reuses_library_database_without_clearing_candidates(self) -> None:
        task = {
            "mode": "incremental",
            "workspace": "/tmp/library",
            "stage_output_root": "/tmp/library/maintenance/incremental_1/stages",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {"scheduler": {"model_workers": 3, "frame_extract_workers": 4}},
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
            },
        }
        plan = build_stage_plan(task)
        command_text = "\n".join(" ".join(stage["command"]) for stage in plan)
        self.assertEqual(plan[0]["key"], "scan")
        self.assertIn("增量扫描并建立素材清单", plan[0]["name"])
        self.assertNotIn("--clear-existing-candidate-items", command_text)
        self.assertIn("--limit-new 0", command_text)

    def test_rebuild_openclip_acceptance_requires_complete_visual_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY);"
                "CREATE TABLE embeddings(embedding_id TEXT PRIMARY KEY,visual_unit_id TEXT);"
                "INSERT INTO visual_units VALUES('vu1'),('vu2');"
                "INSERT INTO embeddings VALUES('emb1','vu1');"
            )
            con.close()
            reason = validate_stage_acceptance(
                {"database": str(db)}, "rebuild_openclip",
            )
        self.assertEqual(reason, "OPENCLIP_COVERAGE_MISMATCH_1_OF_2")

    def test_source_scan_reconciles_moved_and_missing_paths_without_deleting_identity(self) -> None:
        namespace = load_script_namespace(
            "scripts/02_step01_step02_pipeline/"
            "step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"

            def row(content_id: str, relative: str) -> dict[str, object]:
                absolute = root / "source" / relative
                return {
                    "source_file_id": f"file_{content_id}",
                    "source_content_id": content_id,
                    "source_path": str(absolute),
                    "source_relative_path": relative,
                    "source_root": str(root / "source"),
                    "file_name": absolute.name,
                    "extension": ".jpg", "media_kind": "image",
                    "support_status": "supported", "support_reason": "",
                    "file_size_bytes": 10, "mtime_ns": 1_000_000_000,
                    "ctime_ns": 1_000_000_000, "content_sha256": content_id,
                    "dedup_role": "canonical", "next_action": "process",
                    "canonical_source_file_id": f"file_{content_id}",
                    "folder_path": str(absolute.parent), "file_stem": absolute.stem,
                    "stem_key": absolute.stem, "finder_tag_status": "none",
                    "finder_tags_json": "[]",
                }

            first_rows = [row("same-content", "split/A.jpg"), row("removed", "split/B.jpg")]
            namespace["write_step01_database"](
                db, first_rows, first_rows, [], [], [root / "source"],
                "scan_1", str(root / "scan.py"),
            )
            con = sqlite3.connect(db)
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(
                """
                CREATE TABLE source_asset_identity(
                    identity_id TEXT PRIMARY KEY,
                    source_file_record_id TEXT NOT NULL
                        REFERENCES source_file_records(source_file_id)
                );
                INSERT INTO source_asset_identity
                VALUES('identity_removed','file_removed');
                """
            )
            con.commit()
            con.close()
            moved_rows = [row("same-content", "merged/A.jpg")]
            report = namespace["write_step01_database"](
                db, moved_rows, moved_rows, [], [], [root / "source"],
                "scan_2", str(root / "scan.py"),
            )
            con = sqlite3.connect(db)
            active = con.execute(
                "SELECT relative_path,online_status,is_deleted_or_missing "
                "FROM source_assets WHERE source_content_id='same-content'"
            ).fetchone()
            missing = con.execute(
                "SELECT online_status,is_deleted_or_missing FROM source_assets "
                "WHERE source_content_id='removed'"
            ).fetchone()
            snapshot_records = con.execute("SELECT COUNT(*) FROM source_file_records").fetchone()[0]
            current_snapshot_records = con.execute(
                "SELECT COUNT(*) FROM source_file_records WHERE scan_run_id='scan_2'"
            ).fetchone()[0]
            foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            con.close()

        self.assertEqual(active, ("merged/A.jpg", 1, 0))
        self.assertEqual(missing, (0, 1))
        self.assertEqual(snapshot_records, 2)
        self.assertEqual(current_snapshot_records, 1)
        self.assertEqual(foreign_key_errors, [])
        self.assertEqual(report["current_active_source_assets"], 1)

    def test_source_lineage_restore_repairs_only_manifest_backed_missing_rows(self) -> None:
        scan_namespace = load_script_namespace(
            "scripts/02_step01_step02_pipeline/"
            "step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py"
        )
        restore_namespace = load_script_namespace(
            "scripts/04_media_archive_app/restore_source_file_lineage_from_manifest_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            db = workspace / "media_archive.sqlite"
            con = sqlite3.connect(db)
            scan_namespace["ensure_step01_db_schema"](con)
            con.executescript(
                """
                PRAGMA foreign_keys=OFF;
                CREATE TABLE source_asset_identity(
                    identity_id TEXT PRIMARY KEY,
                    source_file_record_id TEXT NOT NULL
                        REFERENCES source_file_records(source_file_id),
                    canonical_source_file_record_id TEXT
                        REFERENCES source_file_records(source_file_id)
                );
                INSERT INTO source_asset_identity
                VALUES('identity_old','file_old','file_old');
                """
            )
            con.commit()
            con.close()
            manifest_dir = workspace / "stages" / "01_scan" / "run" / "manifests"
            manifest_dir.mkdir(parents=True)
            manifest = manifest_dir / "source_files_manifest.csv"
            manifest.write_text(
                "source_file_id,source_content_id,source_path,source_relative_path,"
                "source_root,file_name,extension,media_kind,support_status,support_reason,"
                "file_size_bytes,mtime_ns,ctime_ns,content_sha256,dedup_role,next_action,"
                "canonical_source_file_id,folder_path,file_stem,stem_key,"
                "finder_tag_status,finder_tags_json\n"
                "file_old,content_old,/readonly/old.jpg,old.jpg,/readonly,old.jpg,.jpg,"
                "image,supported,,10,100,90,abc,canonical,process,file_old,/readonly,"
                "old,old,none,[]\n",
                encoding="utf-8",
            )
            summary = restore_namespace["restore"](
                db, workspace / "stages" / "01_scan",
                workspace / "repair", workspace,
            )
            con = sqlite3.connect(db)
            restored = con.execute(
                "SELECT source_file_id,absolute_path FROM source_file_records"
            ).fetchall()
            foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            con.close()

        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["missing_reference_id_count_before"], 1)
        self.assertEqual(summary["restored_source_file_record_count"], 1)
        self.assertEqual(summary["missing_reference_id_count_after"], 0)
        self.assertEqual(restored, [("file_old", "/readonly/old.jpg")])
        self.assertEqual(foreign_key_errors, [])

    def test_visual_schema_bootstrap_is_generic_and_idempotent(self) -> None:
        script = ROOT / "scripts/04_media_archive_app/prepare_visual_analysis_schema_v1.py"
        namespace: dict[str, object] = {}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY);"
            )
            con.close()
            manifest = root / "stages/02_image_preview/manifests/image_preview_visual_unit_manifest.csv"
            manifest.parent.mkdir(parents=True)
            preview = root / "stages/02_image_preview/preview.jpg"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"jpg")
            fields = [
                "visual_unit_id", "preview_role", "sequence_id",
                "representative_position", "source_relative_path", "visual_file",
                "parent_source_content_id", "preview_artifact_id", "producer_step",
                "producer_version",
            ]
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for index, role in enumerate(("first", "middle", "last"), 1):
                    writer.writerow({
                        "visual_unit_id": f"vu{index}", "preview_role": "timelapse_keyframe",
                        "sequence_id": "seq1", "representative_position": role,
                        "source_relative_path": f"raw/{role}.dng", "visual_file": str(preview),
                        "parent_source_content_id": f"src{index}",
                        "preview_artifact_id": f"ip{index}", "producer_step": "step02",
                        "producer_version": "v1",
                    })
            first = namespace["prepare_schema"](db, root / "schema", root)
            # A later authoritative manifest represents a folder merge: the
            # previous group must be replaced instead of accumulated.
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for index, role in enumerate(("first", "middle", "last"), 4):
                    writer.writerow({
                        "visual_unit_id": f"vu{index}", "preview_role": "timelapse_keyframe",
                        "sequence_id": "seq2", "representative_position": role,
                        "source_relative_path": f"merged/{role}.dng", "visual_file": str(preview),
                        "parent_source_content_id": f"src{index}",
                        "preview_artifact_id": f"ip{index}", "producer_step": "step02",
                        "producer_version": "v1",
                    })
            second = namespace["prepare_schema"](db, root / "schema", root)
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(second["status"], "PASS")
            self.assertFalse(first["fixed_input_count"])
            con = sqlite3.connect(db)
            columns = {row[1] for row in con.execute("PRAGMA table_info(visual_labels)")}
            model_run_columns = {row[1] for row in con.execute("PRAGMA table_info(model_runs)")}
            con.close()
            self.assertEqual(columns, namespace["REQUIRED_COLUMNS"])
            self.assertEqual(model_run_columns, namespace["MODEL_RUN_REQUIRED_COLUMNS"])
            con = sqlite3.connect(db)
            timelapse_columns = {
                row[1] for row in con.execute("PRAGMA table_info(step02_image_timelapse_keyframes)")
            }
            con.close()
            self.assertIn("sequence_id", timelapse_columns)
            self.assertIn("representative_position", timelapse_columns)
            self.assertEqual(first["timelapse_rows_imported"], 3)
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM step02_image_timelapse_keyframes").fetchone()[0],
                3,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(DISTINCT sequence_id) FROM step02_image_timelapse_keyframes"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute(
                    "SELECT DISTINCT sequence_id FROM step02_image_timelapse_keyframes"
                ).fetchone()[0],
                "seq2",
            )
            con.close()

    def test_visual_schema_upgrades_step01_model_runs(self) -> None:
        script = ROOT / "scripts/04_media_archive_app/prepare_visual_analysis_schema_v1.py"
        namespace: dict[str, object] = {}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY);"
                "CREATE TABLE model_runs("
                "run_id TEXT PRIMARY KEY,stage TEXT NOT NULL,model_name TEXT NOT NULL,"
                "model_path TEXT NOT NULL,script_path TEXT NOT NULL,input_count INTEGER,"
                "output_count INTEGER,status TEXT NOT NULL,started_at TEXT,finished_at TEXT,"
                "error_message TEXT);"
            )
            con.close()
            report = namespace["prepare_schema"](db, root / "schema", root)
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["model_runs_columns_added"], ["script_version"])

    def test_generic_stage_adapter_supports_spawn_worker_pickling(self) -> None:
        adapter = ROOT / "scripts/04_media_archive_app/run_generic_pipeline_stage.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / "toy_parallel_stage.py"
            stage.write_text(
                "from multiprocessing.reduction import ForkingPickler\n"
                "def square(value): return value * value\n"
                "def main(argv=None):\n"
                "    payload = ForkingPickler.dumps(square)\n"
                "    return 0 if payload else 2\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(adapter), "--script", str(stage),
                 "--allowed-output-root", str(root), "--"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_generic_stage_adapter_expands_release_placeholders(self) -> None:
        adapter = ROOT / "scripts/04_media_archive_app/run_generic_pipeline_stage.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline = root / "Example.app/Contents/Resources/Pipeline"
            stage = pipeline / "scripts/02_scan/toy_release_stage.py"
            output = (root / "task/workspace").resolve()
            stage.parent.mkdir(parents=True)
            output.mkdir(parents=True)
            stage.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "PROJECT_ROOT = Path('$APP_RESOURCES/Pipeline')\n"
                "EXPECTED_PYTHON = Path('$BUNDLED_PIPELINE_ENVS/scan/bin/python')\n"
                "MODEL_ROOT = Path('$MODEL_ROOT')\n"
                "TASK_RUNTIME = Path('$TASK_RUNTIME')\n"
                "REGISTRY_FILES = [PROJECT_ROOT / 'docs/one.md', "
                "Path('$APP_RESOURCES/Pipeline/docs/two.md')]\n"
                "NESTED_PATHS = {'model': (Path('$MODEL_ROOT/model.bin'),)}\n"
                "TEST_OUTPUT_ROOT = Path('$USER_HOME/old-output')\n"
                "DEFAULT_DB = PROJECT_ROOT / 'media_archive.sqlite'\n"
                "def main(argv=None):\n"
                "    expected_root = Path(__file__).resolve().parents[2]\n"
                "    checks = [PROJECT_ROOT == expected_root, "
                "EXPECTED_PYTHON == Path(sys.executable), "
                "MODEL_ROOT == Path('/example/models'), "
                f"TASK_RUNTIME == Path({str(output)!r}), "
                "REGISTRY_FILES == [expected_root / 'docs/one.md', "
                "expected_root / 'docs/two.md'], "
                "NESTED_PATHS == {'model': (Path('/example/models/model.bin'),)}, "
                f"TEST_OUTPUT_ROOT == Path({str(output)!r}), "
                "DEFAULT_DB == expected_root / 'media_archive.sqlite']\n"
                "    return 0 if all(checks) else 7\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["MEDIA_ARCHIVE_MODEL_ROOT"] = "/example/models"
            completed = subprocess.run(
                [sys.executable, str(adapter), "--script", str(stage),
                 "--allowed-output-root", str(output), "--"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_fake_stages_automatically_reach_success_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            markers = [root / f"{index}.txt" for index in range(3)]
            plan = [marker_stage(f"stage_{index}", marker) for index, marker in enumerate(markers)]
            state = execute_pipeline(task, plan=plan)
            self.assertEqual(state["status"], "success")
            self.assertEqual(state["completed_stage_count"], 3)
            self.assertEqual([row["status"] for row in state["stages"]], ["success"] * 3)
            self.assertEqual([path.read_text() for path in markers], ["stage_0", "stage_1", "stage_2"])

    def test_failure_stops_before_later_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            first, failed, forbidden = root / "first", root / "failed", root / "forbidden"
            state = execute_pipeline(task, plan=[
                marker_stage("first", first),
                marker_stage("failed", failed, exit_code=7),
                marker_stage("forbidden", forbidden),
            ])
            self.assertEqual(state["status"], "failed")
            self.assertTrue(first.is_file())
            self.assertTrue(failed.is_file())
            self.assertFalse(forbidden.exists())
            self.assertEqual(state["stages"][2]["status"], "pending")
            self.assertEqual(json.loads(task.read_text(encoding="utf-8"))["status"], "failed")

    def test_resume_skips_success_and_continues_from_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            first = root / "first"
            failed = root / "failed"
            last = root / "last"
            execute_pipeline(task, plan=[
                marker_stage("first", first),
                marker_stage("failed", failed, exit_code=2),
                marker_stage("last", last),
            ])
            first.write_text("must_not_change", encoding="utf-8")
            state = execute_pipeline(task, plan=[
                marker_stage("first", first),
                marker_stage("failed", failed),
                marker_stage("last", last),
            ], resume=True)
            self.assertEqual(state["status"], "success")
            self.assertEqual(first.read_text(), "must_not_change")
            self.assertEqual(failed.read_text(), "failed")
            self.assertEqual(last.read_text(), "last")

    def test_failed_stage_persists_copyable_summary_and_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_path = write_task(root)
            message = (
                "python_mismatch: expected /legacy/python, "
                "got /Applications/Test.app/Contents/Frameworks/PipelinePython312/python"
            )
            failed = {
                "key": "image_preview",
                "name": "生成图片预览",
                "command": [
                    sys.executable, "-c",
                    f"print({message!r}); raise SystemExit(2)",
                ],
            }
            state = execute_pipeline(task_path, plan=[failed])
            task = json.loads(task_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failed_stage_key"], "image_preview")
            self.assertEqual(state["failed_stage_name"], "生成图片预览")
            self.assertIn("python_mismatch:", state["error_summary"])
            self.assertEqual(state["error_log_path"], str(root / "logs" / "pipeline.log"))
            self.assertEqual(state["stages"][0]["error_summary"], state["error_summary"])
            self.assertEqual(task["error_summary"], state["error_summary"])
            self.assertEqual(task["error_log_path"], state["error_log_path"])

    def test_zero_failure_progress_does_not_create_a_false_failure_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_path = write_task(root)
            payload = json.dumps({
                "contract": "media_archive_stage_runtime_contract_v1",
                "event": "stage_progress", "completed": 2, "total": 2,
                "success": 2, "skipped": 0, "failed": 0,
            })
            state = execute_pipeline(task_path, plan=[{
                "key": "scan", "name": "scan",
                "command": [sys.executable, "-c", f"print({payload!r})"],
            }])
            failures = Path(state["stages"][0]["report_paths"]["failures"])
            self.assertEqual(failures.read_text(encoding="utf-8"), "item,reason\n")

    def test_resume_does_not_repeat_successful_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            scan_count = root / "scan_count.txt"
            scan_code = (
                "from pathlib import Path; "
                f"p=Path({str(scan_count)!r}); "
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
            )
            scan = {
                "key": "inventory", "name": "inventory",
                "command": [sys.executable, "-c", scan_code],
            }
            execute_pipeline(task, plan=[
                scan,
                marker_stage("image_preview", root / "failed", exit_code=2),
            ])
            state = execute_pipeline(task, plan=[
                scan,
                marker_stage("image_preview", root / "recovered"),
            ], resume=True)
            self.assertEqual(state["status"], "success")
            self.assertEqual(scan_count.read_text(encoding="utf-8"), "1")

    def test_fake_10_image_2_video_library_completes_16_stage_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            state = execute_pipeline(task, plan=fake_inventory_plan(root, 16))
            self.assertEqual(state["status"], "success")
            self.assertEqual(state["completed_stage_count"], 16)
            self.assertEqual([row["status"] for row in state["stages"]], ["success"] * 16)

    def test_fake_10_image_2_video_library_completes_19_stage_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = write_task(root)
            state = execute_pipeline(task, plan=fake_inventory_plan(root, 19))
            self.assertEqual(state["status"], "success")
            self.assertEqual(state["completed_stage_count"], 19)
            self.assertEqual([row["status"] for row in state["stages"]], ["success"] * 19)

    def test_packaged_python_is_accepted_by_all_active_strict_preflights(self) -> None:
        fake_python = (
            "/Applications/本地数据库.app/Contents/Frameworks/"
            "PipelinePython312/Python.framework/Versions/3.12/bin/python3.12"
        )
        scripts = (
            "scripts/02_step01_step02_pipeline/"
            "step02_2_image_preview_from_db_safe_v6_20260709_182200.py",
            "scripts/02_step01_step02_pipeline/"
            "step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py",
            "scripts/03_stop03_visual_analysis/"
            "stop03_yoloe_full_from_db_safe_v6_20260709_170200.py",
        )
        original = sys.executable
        try:
            sys.executable = fake_python
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = root / "library.sqlite"
                sqlite3.connect(db).close()
                image = load_script_namespace(scripts[0])
                image_report = image["runtime_preflight"](
                    db, root / "fake-image"
                )
                video = load_script_namespace(scripts[1])
                video_report = video["runtime_preflight"](
                    db_path=db, out_path=ROOT / "fake-video-output"
                )
                yolo = load_script_namespace(scripts[2])
                model = root / "model.pt"; model.write_bytes(b"fake")
                mobileclip = root / "mobileclip.ts"; mobileclip.write_bytes(b"fake")
                registry = root / "registry.json"; registry.write_text("{}", encoding="utf-8")
                yolo_report = yolo["runtime_preflight"](
                    db, model, mobileclip, registry,
                )
        finally:
            sys.executable = original
        for report in (image_report, video_report, yolo_report):
            self.assertTrue(report["bundled_app_python"])
            self.assertFalse(any(
                str(blocker).startswith("python_mismatch")
                for blocker in report.get("blockers", [])
            ))

    def test_dynamic_candidate_prepare_is_count_independent(self) -> None:
        script = ROOT / "scripts/04_media_archive_app/stop03_2_v25_dynamic_snapshot_v1.py"
        namespace: dict[str, object] = {}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "library.sqlite"
            sqlite3.connect(db).close()
            report = namespace["prepare_ledger"](
                db,
                ROOT / "migrations/20260711_stop03_2_candidate_queue_v25.sql",
                Path(temp) / "candidate-ledger-output",
            )
            self.assertEqual(report["status"], "PASS")
            con = sqlite3.connect(db)
            columns = {row[1] for row in con.execute("PRAGMA table_info(stop03_2_candidate_queue_items)")}
            con.close()
            self.assertIn("candidate_role", columns)
            self.assertIn("openclip_run_id", columns)

    def test_pure_image_library_marks_only_video_gates_not_applicable(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py"
        )
        video_gates = set(namespace["VIDEO_ONLY_GATES"])
        gates = {name: False for name in video_gates}
        gates.update({"vector_payload_valid": True, "bbox_normalization_valid": True})
        summary = namespace["apply_gate_applicability"]({
            "input_video_visual_units": 0,
            "execution_mode": "commit",
            "automatic_acceptance_gates": gates,
            "technical_status": "FAIL",
            "policy_reason_codes": [],
        })
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(summary["automatic_acceptance_gates_raw"], gates)
        self.assertTrue(all(
            summary["automatic_acceptance_gate_applicability"][name]
            == "NOT_APPLICABLE_NO_VIDEO_INPUT"
            for name in video_gates
        ))

    def test_pure_image_library_does_not_relax_non_video_failure(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py"
        )
        video_gates = set(namespace["VIDEO_ONLY_GATES"])
        gates = {name: False for name in video_gates}
        gates.update({"vector_payload_valid": False, "bbox_normalization_valid": True})
        summary = namespace["apply_gate_applicability"]({
            "input_video_visual_units": 0,
            "execution_mode": "dry-run",
            "automatic_acceptance_gates": gates,
            "technical_status": "FAIL",
        })
        self.assertEqual(summary["technical_status"], "FAIL")
        self.assertEqual(summary["dry_run_status"], "FAIL")

    def test_video_library_keeps_all_frozen_v25_gates_applicable(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py"
        )
        gates = {"coverage_executed": False, "vector_payload_valid": True}
        summary = namespace["apply_gate_applicability"]({
            "input_video_visual_units": 1,
            "execution_mode": "commit",
            "automatic_acceptance_gates": gates,
            "technical_status": "FAIL",
        })
        self.assertEqual(summary["technical_status"], "FAIL")
        self.assertEqual(summary["video_gate_applicability"], "APPLICABLE")

    def test_video_library_allows_missing_pair_gate_when_no_pair_exists(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py"
        )
        gates = {
            "coverage_executed": True,
            "multi_evidence_pair_evaluation_executed": False,
            "vector_payload_valid": True,
        }
        summary = namespace["apply_gate_applicability"]({
            "input_video_visual_units": 6,
            "multi_evidence_pair_opportunity_count": 0,
            "execution_mode": "commit",
            "automatic_acceptance_gates": gates,
            "technical_status": "FAIL",
            "policy_reason_codes": [],
        })
        self.assertEqual(summary["technical_status"], "PASS")
        self.assertEqual(
            summary["automatic_acceptance_gate_applicability"][
                "multi_evidence_pair_evaluation_executed"
            ],
            "NOT_APPLICABLE_NO_PAIR_OPPORTUNITY",
        )
        self.assertNotIn(
            "multi_evidence_pair_evaluation_executed",
            summary["automatic_acceptance_applicable_gates"],
        )
        self.assertEqual(summary["automatic_acceptance_gates_raw"], gates)

    def test_video_library_requires_pair_gate_when_pair_exists(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py"
        )
        gates = {
            "coverage_executed": True,
            "multi_evidence_pair_evaluation_executed": False,
            "vector_payload_valid": True,
        }
        summary = namespace["apply_gate_applicability"]({
            "input_video_visual_units": 6,
            "multi_evidence_pair_opportunity_count": 1,
            "execution_mode": "commit",
            "automatic_acceptance_gates": gates,
            "technical_status": "FAIL",
        })
        self.assertEqual(summary["technical_status"], "FAIL")
        self.assertEqual(
            summary["automatic_acceptance_gate_applicability"][
                "multi_evidence_pair_evaluation_executed"
            ],
            "APPLICABLE",
        )

    def test_stage_plan_uses_generic_candidate_adapter(self) -> None:
        task = {
            "workspace": "/tmp/library",
            "database": "/tmp/library/media_archive.sqlite",
            "source_root": "/tmp/source",
            "profile": {"scheduler": {"model_workers": 3, "frame_extract_workers": 4}},
            "runtime": {
                "project_root": str(ROOT),
                "python": {key: f"/runtime/{key}" for key in (
                    "system", "visual", "yolo", "qwen", "ocr", "embedding"
                )},
                "models": TEST_MODELS,
            },
        }
        plan = {stage["key"]: stage for stage in build_stage_plan(task)}
        command = plan["candidates_generic_v2"]["command"]
        self.assertIn("stop03_2_candidate_queues_generic_library_v1.py", command[1])
        self.assertNotIn("stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py", command[1])
        self.assertIn("--scan-mac-tags", plan["scan"]["command"])

    def test_optional_enrichment_empty_queue_is_success_without_delegate(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/run_optional_enrichment_stage_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE candidates(queue_type TEXT);"
                "CREATE VIEW v_stop03_2_v25_qwenvl_execution_queue AS "
                "SELECT * FROM candidates WHERE queue_type='qwen';"
                "CREATE VIEW v_stop03_2_v25_ocr_execution_queue AS "
                "SELECT * FROM candidates WHERE queue_type='ocr';"
            )
            con.close()
            out = root / "qwen"
            result = namespace["main"]([
                "--stage-kind", "qwen", "--db", str(db), "--out", str(out),
                "--allowed-output-root", str(root), "--", "/must/not/run",
            ])
            self.assertEqual(result, 0)
            report = json.loads((out / "reports/no_work_summary.json").read_text())
            self.assertEqual(report["execution_status"], "NO_WORK_NOT_APPLICABLE")
            self.assertFalse(report["model_run"])
            self.assertFalse(report["empty_contract_materialized"])

    def test_optional_enrichment_can_materialize_empty_database_contract(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/run_optional_enrichment_stage_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE candidates(queue_type TEXT);"
                "CREATE VIEW v_stop03_2_v25_qwenvl_execution_queue AS "
                "SELECT * FROM candidates WHERE queue_type='qwen';"
                "CREATE VIEW v_stop03_2_v25_ocr_execution_queue AS "
                "SELECT * FROM candidates WHERE queue_type='ocr';"
            )
            con.close()
            marker = root / "contract.txt"
            out = root / "ocr"
            result = namespace["main"]([
                "--stage-kind", "ocr", "--db", str(db), "--out", str(out),
                "--allowed-output-root", str(root), "--run-delegate-on-empty",
                "--", sys.executable, "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ok')",
                str(marker),
            ])
            self.assertEqual(result, 0)
            self.assertEqual(marker.read_text(), "ok")
            report = json.loads((out / "reports/no_work_summary.json").read_text())
            self.assertEqual(
                report["execution_status"], "NO_WORK_CONTRACT_MATERIALIZED"
            )
            self.assertTrue(report["database_write"])
            self.assertFalse(report["model_run"])

    def test_ocr_zero_candidate_run_does_not_create_worker_pool(self) -> None:
        namespace = load_script_namespace(
            "scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE stop03_2_candidate_queue_frozen_v25("
                "candidate_id TEXT PRIMARY KEY)"
            )
            con.commit()
            con.close()
            namespace["apply_migration"](
                db, ROOT / "migrations/20260716_stop03_4_ocr_db_v1.sql"
            )
            preflight = {
                "queue_view": "v_stop03_2_v25_ocr_execution_queue",
                "config": {"model_root": "/model"},
                "detection_model_dir": "/model/det",
                "recognition_model_dir": "/model/rec",
                "detection_model_sha256": "det",
                "recognition_model_sha256": "rec",
                "model_fingerprint_sha256": "model",
                "config_path": "/config.json",
                "config_sha256": "config",
                "script_sha256": "script",
            }
            run_id = namespace["create_run_and_items"](
                db, [], preflight, run_kind="full", workers=3, max_attempts=3
            )

            class ExplodingExecutor:
                def __init__(self, **_kwargs: object) -> None:
                    raise AssertionError("worker pool must not be created")

            report = namespace["execute_dynamic_pool"](
                db,
                run_id,
                root / "out",
                preflight,
                workers=3,
                max_attempts=3,
                executor_factory=ExplodingExecutor,
            )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["result_count"], 0)

    def test_qwenvl_supplement_contract_adds_only_missing_images(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_3_qwenvl_supplement_contract_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);"
                "CREATE TABLE stop03_3_qwenvl_results(visual_unit_id TEXT,result_status TEXT);"
                "CREATE TABLE stop03_2_candidate_queue_frozen_v25(candidate_id TEXT PRIMARY KEY);"
                "INSERT INTO source_assets VALUES('s1','image'),('s2','image'),('s3','video');"
                "INSERT INTO visual_units VALUES('v1','s1'),('v2','s2'),('v3','s3');"
                "INSERT INTO derived_assets VALUES('d1'),('d2'),('d3');"
                "INSERT INTO stop03_3_qwenvl_results VALUES('v1','success');"
                "INSERT INTO stop03_2_candidate_queue_frozen_v25 VALUES('old_video');"
            )
            con.close()
            preview = root / "preview.jpg"
            preview.write_bytes(b"derived preview")
            manifest = root / "candidate.jsonl"
            rows = [
                {"candidate_id": "old_image", "queue_type": "qwenvl_high_value", "media_type": "image", "visual_unit_id": "v1", "source_content_id": "s1", "derived_id": "d1", "visual_file": str(preview)},
                {"candidate_id": "new_image", "queue_type": "qwenvl_high_value", "media_type": "image", "visual_unit_id": "v2", "source_content_id": "s2", "derived_id": "d2", "visual_file": str(preview), "candidate_role": "image_finder_tag_seed", "reason_codes": "finder_tag"},
                {"candidate_id": "new_video", "queue_type": "qwenvl_high_value", "media_type": "video", "visual_unit_id": "v3", "source_content_id": "s3", "derived_id": "d3", "visual_file": str(preview)},
            ]
            manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            out = root / "out"
            exit_code = namespace["main"]([
                "--mode", "commit", "--db", str(db),
                "--candidate-manifest", str(manifest),
                "--migration", str(ROOT / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql"),
                "--allowed-output-root", str(root), "--out", str(out),
                "--confirm-central-db-write",
            ])
            con = sqlite3.connect(db)
            supplement = con.execute(
                "SELECT candidate_id,media_type,candidate_role FROM stop03_3_qwenvl_supplement_candidates"
            ).fetchall()
            frozen = con.execute("SELECT candidate_id FROM stop03_2_candidate_queue_frozen_v25").fetchall()
            con.close()
            summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(exit_code, 0)
        self.assertEqual(supplement, [("new_image", "image", "image_finder_tag_seed")])
        self.assertEqual(frozen, [("old_video",)])
        self.assertFalse(summary["frozen_v25_modified"])
        self.assertEqual(summary["existing_success_reexecuted"], 0)

    def test_all_image_scope_derives_count_from_database_and_excludes_frozen_rows(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_3_qwenvl_supplement_contract_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            preview = root / "preview.jpg"; preview.write_bytes(b"preview")
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """CREATE TABLE source_assets(
                   source_content_id TEXT PRIMARY KEY,media_type TEXT,relative_path TEXT,
                   is_deleted_or_missing INTEGER);
                   CREATE TABLE visual_units(
                   visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT,derived_id TEXT,
                   visual_file TEXT);
                   CREATE VIEW canonical_visual_units_for_heavy AS SELECT * FROM visual_units;
                   CREATE TABLE stop03_2_candidate_queue_frozen_v25(
                   candidate_id TEXT PRIMARY KEY,queue_type TEXT,visual_unit_id TEXT);
                   CREATE TABLE stop03_3_qwenvl_results(visual_unit_id TEXT,result_status TEXT);
                   INSERT INTO source_assets VALUES('s1','image','a.jpg',0);
                   INSERT INTO source_assets VALUES('s2','image','b.jpg',0);
                   INSERT INTO source_assets VALUES('s3','image','c.jpg',0);
                   INSERT INTO source_assets VALUES('s4','video','d.mov',0);"""
            )
            for index in range(1, 5):
                con.execute(
                    "INSERT INTO visual_units VALUES(?,?,?,?)",
                    (f"v{index}", f"s{index}", f"d{index}", str(preview)),
                )
            con.execute(
                "INSERT INTO stop03_2_candidate_queue_frozen_v25 VALUES('f1','qwenvl_high_value','v1')"
            )
            con.execute("INSERT INTO stop03_3_qwenvl_results VALUES('v2','success')")
            con.commit(); con.close()
            rows = namespace["select_all_image_visual_units"](db, root)

        self.assertEqual([row["visual_unit_id"] for row in rows], ["v3"])
        self.assertEqual(rows[0]["candidate_role"], "image_all_scope_supplement")
        self.assertEqual(rows[0]["reason_codes"], "user_selected_all_images")

    def test_qwenvl_supplement_orchestrator_is_dynamic_concurrent_and_idempotent(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_3_qwenvl_supplement_orchestrator_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);"
            )
            con.executescript(
                (ROOT / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql").read_text(encoding="utf-8")
            )
            preview = root / "preview.jpg"
            preview.write_bytes(b"derived preview")
            for index in range(6):
                source_id = f"source_{index}"
                visual_id = f"visual_{index}"
                derived_id = f"derived_{index}"
                candidate_id = f"candidate_{index}"
                con.execute("INSERT INTO source_assets VALUES(?, 'image')", (source_id,))
                con.execute("INSERT INTO visual_units VALUES(?, ?)", (visual_id, source_id))
                con.execute("INSERT INTO derived_assets VALUES(?)", (derived_id,))
                con.execute(
                    """INSERT INTO stop03_3_qwenvl_supplement_candidates
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id, source_id, visual_id, visual_id, derived_id,
                        "image_finder_tag_seed", "finder_tag", "v25", "image",
                        f"image_{index}.jpg", str(preview), f"sha_{index}", 1.0,
                        "candidate_run", "supplement_v1", "2026-07-20T00:00:00+0800",
                    ),
                )
            con.commit()
            con.close()

            run_id, count = namespace["prepare_run"](
                db, workers=3, max_tokens=384,
                model_fingerprint="model", prompt_sha="prompt",
            )
            self.assertIsNotNone(run_id)
            self.assertEqual(count, 6)
            lock = threading.Lock()
            release = threading.Event()
            active = 0
            maximum_active = 0
            calls: list[str] = []

            def fake_infer(item: dict[str, object]) -> dict[str, object]:
                nonlocal active, maximum_active
                with lock:
                    calls.append(str(item["candidate_id"]))
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if maximum_active == 3:
                        release.set()
                self.assertTrue(release.wait(timeout=2.0))
                time.sleep(0.02)
                with lock:
                    active -= 1
                return {
                    "result_status": "success",
                    "clean_text": f"description {item['candidate_id']}",
                    "generation_tokens": 12,
                    "finish_reason": "stop",
                }

            report = namespace["run_fake_concurrent"](
                db, str(run_id), workers=3, max_attempts=2, infer_one=fake_infer,
            )
            con = sqlite3.connect(db)
            item_rows = con.execute(
                "SELECT status,attempt_count FROM stop03_3_qwenvl_supplement_items ORDER BY candidate_id"
            ).fetchall()
            result_count, distinct_keys = con.execute(
                "SELECT COUNT(*),COUNT(DISTINCT execution_key) FROM stop03_3_qwenvl_supplement_results"
            ).fetchone()
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
            con.close()
            second_run_id, second_count = namespace["prepare_run"](
                db, workers=3, max_tokens=384,
                model_fingerprint="model", prompt_sha="prompt",
            )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(maximum_active, 3)
        self.assertEqual(len(calls), 6)
        self.assertEqual(item_rows, [("success", 1)] * 6)
        self.assertEqual(result_count, 6)
        self.assertEqual(distinct_keys, 6)
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])
        self.assertIsNone(second_run_id)
        self.assertEqual(second_count, 0)

    def test_qwenvl_supplement_failure_retries_without_blocking_other_items(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_3_qwenvl_supplement_orchestrator_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);"
            )
            con.executescript(
                (ROOT / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql").read_text(encoding="utf-8")
            )
            preview = root / "preview.jpg"
            preview.write_bytes(b"derived preview")
            for index in range(3):
                con.execute("INSERT INTO source_assets VALUES(?, 'image')", (f"s{index}",))
                con.execute("INSERT INTO visual_units VALUES(?, ?)", (f"v{index}", f"s{index}"))
                con.execute("INSERT INTO derived_assets VALUES(?)", (f"d{index}",))
                con.execute(
                    "INSERT INTO stop03_3_qwenvl_supplement_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"c{index}", f"s{index}", f"v{index}", f"v{index}", f"d{index}",
                        "image_finder_tag_seed", "finder_tag", "v25", "image",
                        f"image_{index}.jpg", str(preview), f"sha_{index}", 1.0,
                        "candidate_run", "supplement_v1", "2026-07-20T00:00:00+0800",
                    ),
                )
            con.commit()
            con.close()
            run_id, _ = namespace["prepare_run"](
                db, workers=3, max_tokens=384,
                model_fingerprint="model", prompt_sha="prompt",
            )
            attempts: dict[str, int] = {}

            def flaky_infer(item: dict[str, object]) -> dict[str, object]:
                candidate = str(item["candidate_id"])
                attempts[candidate] = attempts.get(candidate, 0) + 1
                if candidate == "c0" and attempts[candidate] == 1:
                    raise RuntimeError("fake first-attempt failure")
                return {"result_status": "success", "clean_text": candidate}

            report = namespace["run_fake_concurrent"](
                db, str(run_id), workers=3, max_attempts=2, infer_one=flaky_infer,
            )
            con = sqlite3.connect(db)
            rows = con.execute(
                "SELECT candidate_id,status,attempt_count FROM stop03_3_qwenvl_supplement_items ORDER BY candidate_id"
            ).fetchall()
            con.close()

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(rows, [("c0", "success", 2), ("c1", "success", 1), ("c2", "success", 1)])

    def test_qwenvl_supplement_resume_reuses_owner_run_and_does_not_block_on_terminal_review(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_3_qwenvl_supplement_orchestrator_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);"
            )
            con.executescript(
                (ROOT / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql").read_text(encoding="utf-8")
            )
            preview = root / "preview.jpg"; preview.write_bytes(b"derived preview")
            con.execute("INSERT INTO source_assets VALUES('s1','image')")
            con.execute("INSERT INTO visual_units VALUES('v1','s1')")
            con.execute("INSERT INTO derived_assets VALUES('d1')")
            con.execute(
                "INSERT INTO stop03_3_qwenvl_supplement_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("c1", "s1", "v1", "v1", "d1", "image_finder_tag_seed", "finder_tag", "v25", "image", "image.jpg", str(preview), "sha", 1.0, "candidate_run", "supplement_v1", "2026-07-20"),
            )
            con.commit(); con.close()
            first_run, first_count = namespace["prepare_run"](
                db, workers=3, max_tokens=384, model_fingerprint="model", prompt_sha="prompt", max_attempts=3,
            )
            con = sqlite3.connect(db)
            con.execute(
                "UPDATE stop03_3_qwenvl_supplement_items SET status='review',attempt_count=3 WHERE run_id=?",
                (first_run,),
            )
            con.commit(); con.close()
            resumed_run, actionable = namespace["prepare_run"](
                db, workers=3, max_tokens=384, model_fingerprint="model", prompt_sha="prompt", max_attempts=3,
            )
            report = namespace["finalize_run"](db, str(resumed_run), 3)
            con = sqlite3.connect(db)
            duplicate_count = con.execute(
                "SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM stop03_3_qwenvl_supplement_items"
            ).fetchone()[0]
            con.close()

        self.assertEqual(first_count, 1)
        self.assertEqual(resumed_run, first_run)
        self.assertEqual(actionable, 0)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["policy_status"], "REVIEW")
        self.assertEqual(report["terminal_issue_count"], 1)
        self.assertEqual(duplicate_count, 0)

    def test_qwenvl_supplement_evidence_merge_is_append_only_and_idempotent(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/stop03_5b_merge_qwenvl_supplement_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY,media_type TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT PRIMARY KEY);"
            )
            con.executescript(
                (ROOT / "migrations/20260720_stop03_3_qwenvl_supplement_v1.sql").read_text(encoding="utf-8")
            )
            con.executescript(
                (ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql").read_text(encoding="utf-8")
            )
            con.execute(
                """INSERT INTO stop03_5_unified_evidence_runs VALUES(
                   'base','base_contract','base_qwen','base_ocr',1,1,2,2,0,0,
                   'candidate_sha','evidence_sha','base_payload','quality_sha','script_sha','success','2020-01-01T00:00:00+00:00')"""
            )
            item_columns = (
                "staging_item_id,staging_run_id,canonical_evidence_key,modality,evidence_id,candidate_id,"
                "source_content_id,visual_unit_id,canonical_visual_unit_id,derived_id,candidate_role,"
                "reason_codes,policy_version,runtime_visual_file_sha256,evidence_status,quality_status,"
                "quality_reasons,evidence_text,evidence_text_sha256,evidence_attributes_json,source_run_id,"
                "source_result_id,source_execution_key,created_at"
            )
            for modality, suffix in (("qwenvl", "q"), ("ocr", "o")):
                con.execute(
                    f"INSERT INTO stop03_5_unified_evidence_items({item_columns}) VALUES({','.join('?' for _ in range(24))})",
                    (
                        f"item_{suffix}", "base", f"key_{suffix}", modality, f"ev_{suffix}", f"cand_{suffix}",
                        f"src_{suffix}", f"vis_{suffix}", f"vis_{suffix}", f"der_{suffix}", "role", "reason",
                        "v25", f"sha_{suffix}", "success", "PASS", "", f"text_{suffix}", f"text_sha_{suffix}",
                        "{}", f"run_{suffix}", f"result_{suffix}", f"exec_{suffix}", "2020-01-01T00:00:00+00:00",
                    ),
                )
            con.execute("INSERT INTO source_assets VALUES('src_new','image')")
            con.execute("INSERT INTO visual_units VALUES('vis_new','src_new')")
            con.execute("INSERT INTO derived_assets VALUES('der_new')")
            con.execute(
                "INSERT INTO stop03_3_qwenvl_supplement_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "cand_new", "src_new", "vis_new", "vis_new", "der_new", "image_finder_tag_seed",
                    "finder_tag", "v25", "image", "new.jpg", str(root / "preview.jpg"), "runtime_sha",
                    1.0, "candidate_run", "supplement_v1", "2026-07-20T00:00:00+00:00",
                ),
            )
            con.execute(
                "INSERT INTO stop03_3_qwenvl_supplement_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "supplement_run", "supplement_v1", 1, 1, 0, 0, 3, 384, "model", "prompt",
                    "success", "2026-07-20T00:00:00+00:00", "2026-07-20T00:01:00+00:00", "",
                ),
            )
            con.execute(
                "INSERT INTO stop03_3_qwenvl_supplement_items VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "supplement_run", "cand_new", "exec_new", "success", 1, 1.0,
                    "2026-07-20T00:00:00+00:00", "2026-07-20T00:00:00+00:00", "2026-07-20T00:01:00+00:00",
                ),
            )
            clean_text = "一张被用户标记为高价值的图片"
            con.execute(
                "INSERT INTO stop03_3_qwenvl_supplement_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "result_new", "supplement_run", "cand_new", "exec_new", "ev_new", "success",
                    clean_text, namespace["sha256_text"](clean_text), 20, "stop", "runtime_sha",
                    "qwenvl_output_contract_v2.0", "2026-07-20T00:01:00+00:00",
                ),
            )
            con.commit()
            con.close()

            summary, merged_rows = namespace["build_merge"](db)
            committed = namespace["commit_merge"](
                db, ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql",
                summary, merged_rows,
            )
            second = namespace["commit_merge"](
                db, ROOT / "migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql",
                summary, merged_rows,
            )
            con = sqlite3.connect(db)
            base_count = con.execute(
                "SELECT COUNT(*) FROM stop03_5_unified_evidence_items WHERE staging_run_id='base'"
            ).fetchone()[0]
            merged_count = con.execute(
                "SELECT COUNT(*) FROM stop03_5_unified_evidence_items WHERE staging_run_id=?",
                (summary["staging_run_id"],),
            ).fetchone()[0]
            latest_count = con.execute("SELECT COUNT(*) FROM v_stop03_5_latest_unified_evidence").fetchone()[0]
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
            con.close()

        self.assertEqual(summary["supplement_evidence_added_count"], 1)
        self.assertEqual(summary["qwen_count"], 2)
        self.assertEqual(summary["ocr_count"], 1)
        self.assertEqual(committed["commit_status"], "COMMITTED")
        self.assertEqual(second["commit_status"], "IDEMPOTENT_PASS")
        self.assertEqual(base_count, 2)
        self.assertEqual(merged_count, 3)
        self.assertEqual(latest_count, 3)
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])

    def test_existing_library_finder_tag_refresh_is_append_only_and_generic(self) -> None:
        namespace = load_script_namespace(
            "scripts/04_media_archive_app/refresh_existing_library_finder_tags_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_a = root / "a.jpg"; image_a.write_bytes(b"a")
            image_b = root / "b.jpg"; image_b.write_bytes(b"b")
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """CREATE TABLE source_assets(
                   source_content_id TEXT PRIMARY KEY,absolute_path TEXT,relative_path TEXT,
                   media_type TEXT,is_deleted_or_missing INTEGER);
                   CREATE TABLE source_file_records(
                   source_file_id TEXT PRIMARY KEY,source_content_id TEXT,dedup_role TEXT,
                   finder_tag_status TEXT,finder_tags_json TEXT,updated_at TEXT);
                   CREATE TABLE source_finder_tags(
                   tag_id TEXT PRIMARY KEY,source_file_id TEXT NOT NULL,source_content_id TEXT NOT NULL,
                   source_path TEXT NOT NULL,tag_raw TEXT NOT NULL,tag_name TEXT,tag_color TEXT,
                   scan_run_id TEXT NOT NULL,created_at TEXT);
                   INSERT INTO source_assets VALUES('s1','""" + str(image_a) + """','a.jpg','image',0);
                   INSERT INTO source_assets VALUES('s2','""" + str(image_b) + """','b.jpg','image',0);
                   INSERT INTO source_file_records VALUES('f1','s1','canonical','','[]','');
                   INSERT INTO source_file_records VALUES('f2','s2','canonical','','[]','');
                   INSERT INTO source_finder_tags VALUES(
                   'old','f1','s1','""" + str(image_a) + """','旧标签','旧标签','','old_run','old');"""
            )
            con.close()
            namespace["read_tags"] = lambda path: (
                ([{"tag_raw": "红色", "tag_name": "红色", "tag_color": "6"}], "ok")
                if Path(path).name == "a.jpg" else ([], "none")
            )
            report = namespace["refresh"](db)
            con = sqlite3.connect(db)
            tag_rows = con.execute(
                "SELECT tag_raw FROM source_finder_tags ORDER BY tag_raw"
            ).fetchall()
            statuses = con.execute(
                "SELECT source_content_id,finder_tag_status FROM source_file_records ORDER BY source_content_id"
            ).fetchall()
            con.close()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["image_source_count"], 2)
        self.assertEqual(report["tagged_source_count"], 1)
        self.assertEqual(tag_rows, [("旧标签",), ("红色",)])
        self.assertEqual(statuses, [("s1", "ok"), ("s2", "none")])
        self.assertFalse(report["original_media_write"])

    def test_schema_writers_create_database_backup_before_change(self) -> None:
        visual_schema = load_script_namespace(
            "scripts/04_media_archive_app/prepare_visual_analysis_schema_v1.py"
        )
        candidate_schema = load_script_namespace(
            "scripts/04_media_archive_app/stop03_2_v25_dynamic_snapshot_v1.py"
        )
        person_reid = load_script_namespace(
            "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY);"
                "CREATE TABLE visual_units("
                "visual_unit_id TEXT PRIMARY KEY,"
                "source_content_id TEXT REFERENCES source_assets(source_content_id));"
            )
            con.commit()
            con.close()

            visual_report = visual_schema["prepare_schema"](
                db, root / "visual", root, None
            )
            candidate_report = candidate_schema["prepare_ledger"](
                db,
                ROOT / "migrations/20260711_stop03_2_candidate_queue_v25.sql",
                root / "candidate",
            )
            person_backup = person_reid["backup_database_once"](
                db, root / "person"
            )

            backup_paths = [
                Path(visual_report["database_backup_path"]),
                Path(candidate_report["backup_path"]),
                Path(person_backup),
            ]
            for backup in backup_paths:
                self.assertTrue(backup.is_file())
                self.assertGreater(backup.stat().st_size, 0)
                con = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
                try:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    con.close()

    def test_native_frontend_exposes_real_start_and_stop(self) -> None:
        source = (ROOT / "apps/media_archive_image_video_ui/native_frontend.swift").read_text(encoding="utf-8")
        bridge = (ROOT / "apps/media_archive_image_video_ui/native_bridge.py").read_text(encoding="utf-8")
        self.assertIn("开始第一次完整整理", source)
        self.assertIn('let taskModes = ["第一次完整整理", "增量整理", "修复缺失内容", "重建搜索入口", "补充音频搜索"]', source)
        self.assertIn("Text(mode).tag(mode)", source)
        self.assertIn("已有素材库", source)
        self.assertIn('"start-existing-task"', source)
        self.assertIn('"增量整理":"incremental"', source)
        self.assertIn("开始修复缺失内容", source)
        self.assertIn("停止当前任务", source)
        self.assertIn("从断点继续", source)
        self.assertIn("当前阶段预计剩余", source)
        self.assertNotIn("全任务预计剩余", source)
        self.assertLess(
            source.index('Button("从断点继续")'),
            source.index('Button("进入搜索素材")'),
        )
        self.assertIn('"start-task"', bridge)
        self.assertIn('"resume-task"', bridge)
        self.assertIn('"pipeline-worker"', bridge)
        self.assertIn("查看阶段明细", source)
        self.assertIn("整组 \\($0.formatted()) 张原始照片", source)
        self.assertIn("打开原始文件夹", source)
        self.assertIn("previewTimelapseFrame", source)
        self.assertIn('return "\\(sourceRelativePath ?? "")|\\(representativePosition ?? "representative")"', source)
        self.assertIn("任务在“\\(failedStage)”失败", source)
        self.assertIn("复制错误信息", source)
        self.assertIn("完整日志：\\(logPath)", source)
        self.assertIn("失败阶段：\\(detail.pipeline.failedStageName", source)
        self.assertIn("detail.pipeline.errorSummary ?? detail.error", source)
        self.assertIn('"failed_stage_key": state.get("failed_stage_key")', bridge)
        self.assertIn('"error_summary": str(state.get("error_summary")', bridge)

    def test_embedded_python_does_not_mutate_signed_app_bundle(self) -> None:
        source = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn('setenv("PYTHONDONTWRITEBYTECODE", "1", 1);', source)

    def test_app_launcher_marks_private_python_as_portable_runtime(self) -> None:
        builder = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn('setenv("MEDIA_ARCHIVE_PORTABLE_RUNTIME", "1", 1);', builder)


if __name__ == "__main__":
    unittest.main()
