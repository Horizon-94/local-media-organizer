from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from apps.media_archive.v05_2b import low_cost_smoke
from apps.media_archive.v05_2b.low_cost_smoke import (
    OCR_SCRIPT,
    build_full_source_map,
    build_run_inputs,
    build_smoke_manifest,
    judgement,
    parse_roles,
    prepare_runtime_script,
    run_model_role,
    write_jsonl,
)


def _write_prompt_registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "registry_name": "test_registry",
                "schema_version": "test-1",
                "layers": {
                    "A_CORE": {"default_run": True},
                    "B_EXTENDED": {"default_run": False},
                    "C_DELEGATED": {"default_run": False},
                },
                "defaults": {
                    "YOLOE_A_CORE": {"conf": 0.25, "imgsz": 640},
                    "YOLOE_B_EXTENDED": {"conf": 0.35, "imgsz": 768},
                },
                "A_CORE_CLASSES": [{"label": "person"}, {"label": "car"}],
                "B_EXTENDED_CLASSES": [{"label": "sign"}, {"label": "screen"}],
                "C_DELEGATED_CONCEPTS": [{"label": "birthday"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _touch_jpgs(root: Path, rel: str, count: int) -> None:
    directory = root / rel
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"item_{index:03d}.jpg").write_bytes(b"jpg")


def test_smoke_manifest_uses_fixed_4_3_5_breakdown(tmp_path: Path) -> None:
    _touch_jpgs(tmp_path, "a9t_image/normal_preview_pool_jpg", 10)
    _touch_jpgs(tmp_path, "a9t_image/keyframe_pool_jpg", 5)
    for video_index in range(8):
        _touch_jpgs(tmp_path, f"c4_video/video_frame_jpg/video_{video_index:03d}.MOV", 3)

    rows, breakdown = build_smoke_manifest(tmp_path)

    assert len(rows) == 12
    assert breakdown == {"normal_preview": 4, "timelapse_keyframe": 3, "video_frame": 5}
    assert [row["image_group_type"] for row in rows].count("normal_preview") == 4
    assert [row["image_group_type"] for row in rows].count("timelapse_keyframe") == 3
    assert [row["image_group_type"] for row in rows].count("video_frame") == 5
    assert all(row["estimated_frame_time_ms"] is not None for row in rows if row["image_group_type"] == "video_frame")


def test_final_judgement_requires_all_three_models() -> None:
    assert (
        judgement(
            {
                "yoloe": {"attempted": 12, "succeeded": 12},
                "ocr": {"attempted": 12, "succeeded": 12},
                "visual_embedding": {"attempted": 12, "succeeded": 12},
            }
        )
        == "PASS"
    )
    assert (
        judgement(
            {
                "yoloe": {"attempted": 12, "succeeded": 10},
                "ocr": {"attempted": 12, "succeeded": 12},
                "visual_embedding": {"attempted": 12, "succeeded": 12},
            }
        )
        == "PASS_WITH_WARNINGS"
    )
    assert (
        judgement(
            {
                "yoloe": {"attempted": 12, "succeeded": 0},
                "ocr": {"attempted": 12, "succeeded": 12},
                "visual_embedding": {"attempted": 12, "succeeded": 12},
            }
        )
        == "FAIL"
    )


def test_help_exposes_full_run_workers_and_resume(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        low_cost_smoke.main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for flag in [
        "--sample-count",
        "--full-run",
        "--yoloe-workers",
        "--ocr-workers",
        "--embedding-workers",
        "--resume",
        "--roles",
        "--stage-order",
        "--execution-mode",
        "--role-timeout-seconds",
        "--prompt-registry",
        "--yoloe-layers",
    ]:
        assert flag in output
    assert "--embedding-workers {1,2,3,4,5,6}" in output
    assert "--yoloe-workers {1,2,3,4,5,6}" in output
    assert "--ocr-workers {1,2,3,4,5,6}" in output


def test_yoloe_prompt_registry_defaults_to_a_core_and_excludes_c_delegated(tmp_path: Path) -> None:
    registry_path = _write_prompt_registry(tmp_path / "registry.json")

    metadata = low_cost_smoke.load_yoloe_prompt_registry(registry_path)

    assert metadata["prompt_registry_path"] == str(registry_path)
    assert metadata["prompt_registry_schema_version"] == "test-1"
    assert metadata["yoloe_layers_enabled"] == ["A_CORE"]
    assert metadata["yoloe_class_count_by_layer"] == {"A_CORE": 2}
    assert metadata["yoloe_class_count_total"] == 2
    assert metadata["yoloe_class_list"] == ["person", "car"]
    assert "birthday" not in metadata["yoloe_class_list"]
    assert metadata["yoloe_class_list_sha256"] == low_cost_smoke.load_yoloe_prompt_registry(registry_path)[
        "yoloe_class_list_sha256"
    ]


def test_yoloe_prompt_registry_supports_explicit_a_plus_b(tmp_path: Path) -> None:
    registry_path = _write_prompt_registry(tmp_path / "registry.json")

    metadata = low_cost_smoke.load_yoloe_prompt_registry(registry_path, "A_CORE,B_EXTENDED")

    assert metadata["yoloe_layers_enabled"] == ["A_CORE", "B_EXTENDED"]
    assert metadata["yoloe_class_count_by_layer"] == {"A_CORE": 2, "B_EXTENDED": 2}
    assert metadata["yoloe_class_count_total"] == 4
    assert metadata["yoloe_conf_by_layer"] == {"A_CORE": 0.25, "B_EXTENDED": 0.35}
    assert metadata["yoloe_imgsz_by_layer"] == {"A_CORE": 640, "B_EXTENDED": 768}
    assert metadata["yoloe_class_list"] == ["person", "car", "sign", "screen"]


def test_yoloe_prompt_registry_rejects_c_delegated_for_yoloe(tmp_path: Path) -> None:
    registry_path = _write_prompt_registry(tmp_path / "registry.json")

    with pytest.raises(ValueError, match="unsupported YOLOE layer"):
        low_cost_smoke.load_yoloe_prompt_registry(registry_path, "A_CORE,C_DELEGATED")


def test_roles_parse_embedding_alias_and_reject_unknown() -> None:
    assert parse_roles("ocr,embedding") == ["ocr", "visual_embedding"]
    with pytest.raises(ValueError, match="unsupported role"):
        parse_roles("ocr,qwenvl")


def test_periodic_resource_sampling_does_not_call_system_profiler(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="COMMAND %CPU %MEM\npython 1.0 2.0\n", stderr="")

    monkeypatch.setattr(low_cost_smoke.subprocess, "run", fake_run)

    row = low_cost_smoke.sample_resources()

    assert row["python_process_count"] == 1
    assert commands == [["ps", "-axo", "comm,%cpu,%mem"]]
    assert all(command[0] != "system_profiler" for command in commands)


def test_hardware_profile_system_profiler_uses_timeout_and_failure_is_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int | None]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        if command[0] == "system_profiler":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, 0, stdout="Darwin test\n", stderr="")

    monkeypatch.setattr(low_cost_smoke.subprocess, "run", fake_run)

    profile = low_cost_smoke.collect_hardware_profile(use_system_profiler=True)

    assert profile["system_profiler_used"] is True
    assert profile["system_profiler_timeout_seconds"] == 10
    assert profile["system_profiler_error"]
    assert (["system_profiler", "SPHardwareDataType"], 10) in calls


def test_runtime_subprocess_env_is_cache_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(low_cost_smoke.subprocess, "run", fake_run)
    script = tmp_path / "runtime.py"
    script.write_text("print('unused')\n", encoding="utf-8")

    rows, metadata = low_cost_smoke._run_model_batch(
        "yoloe",
        1,
        [{"smoke_image_id": "one", "image_path": "/tmp/one.jpg", "derived_image_path": "/tmp/one.jpg"}],
        {"model_path": "/tmp/model"},
        tmp_path / "workspace",
        script,
        tmp_path / "workspace/yoloe_output",
        0,
    )

    env = captured["env"]
    assert rows == []
    assert captured["cwd"] == tmp_path / "workspace/runner"
    assert metadata["cache_root"] == str(tmp_path / "workspace/runner/cache")
    for key in [
        "XDG_CACHE_HOME",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "MPLCONFIGDIR",
        "ULTRALYTICS_SETTINGS_DIR",
        "YOLO_CONFIG_DIR",
        "PADDLE_HOME",
    ]:
        assert str(env[key]).startswith(str(tmp_path / "workspace/runner/cache"))
    assert env["HF_HOME"] != str(tmp_path / "workspace")


def test_workspace_root_pollution_and_output_size_summary_are_separated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "embedding_output").mkdir(parents=True)
    (workspace / "embedding_output/result.jsonl").write_bytes(b"ee")
    (workspace / "reports").mkdir()
    (workspace / "reports/summary.json").write_bytes(b"rrrr")
    (workspace / "runner/cache").mkdir(parents=True)
    (workspace / "runner/cache/mobileclip2_b.ts").write_bytes(b"cache")
    (workspace / "logs").mkdir()
    (workspace / "mobileclip2_b.ts").write_bytes(b"pollution")

    summary = low_cost_smoke.output_size_summary(workspace)

    assert summary["output_size_by_dir"]["embedding_output"] == 2
    assert summary["output_size_by_dir"]["reports"] == 4
    assert summary["output_size_by_dir"]["runner_cache"] == 5
    assert summary["evidence_output_size_bytes"] == 2
    assert summary["runtime_cache_size_bytes"] == 5
    assert summary["workspace_total_size_bytes"] >= 20
    assert summary["workspace_root_pollution_detected"] is True
    assert summary["workspace_root_unexpected_files"][0]["path"].endswith("mobileclip2_b.ts")


def test_full_run_inputs_are_not_fixed_to_12(tmp_path: Path) -> None:
    _touch_jpgs(tmp_path, "a9t_image/normal_preview_pool_jpg", 15)
    _touch_jpgs(tmp_path, "a9t_image/keyframe_pool_jpg", 4)
    for video_index in range(3):
        _touch_jpgs(tmp_path, f"c4_video/video_frame_jpg/video_{video_index:03d}.MOV", 2)

    rows, summary, breakdown, run_mode = build_run_inputs(tmp_path, sample_count=12, full_run=True)

    assert run_mode == "full"
    assert len(rows) == 25
    assert summary["record_count"] == 25
    assert breakdown == {"normal_preview": 15, "timelapse_keyframe": 4, "video_frame": 6}


def test_sample_count_cannot_exceed_discoverable_jpg_count(tmp_path: Path) -> None:
    _touch_jpgs(tmp_path, "a9t_image/normal_preview_pool_jpg", 3)

    with pytest.raises(ValueError, match="exceeds discoverable JPG count"):
        build_run_inputs(tmp_path, sample_count=4, full_run=False)


def test_source_map_uses_stable_fields_from_existing_manifests(tmp_path: Path) -> None:
    preview_dir = tmp_path / "a9t_image/normal_preview_pool_jpg"
    preview_dir.mkdir(parents=True)
    preview = preview_dir / "0000001_one.jpg.jpg"
    preview.write_bytes(b"jpg")
    manifest = tmp_path / "a9t_image/preview_manifest.csv"
    manifest.write_text(
        "source_path,relative_path,preview_role,output_path\n"
        f"/source/one.jpg,one.jpg,normal_image,{preview}\n",
        encoding="utf-8",
    )
    frame_dir = tmp_path / "c4_video/video_frame_jpg/video.mov"
    frame_dir.mkdir(parents=True)
    frame = frame_dir / "frame_000002.jpg"
    frame.write_bytes(b"jpg")
    frame_manifest = tmp_path / "c4_video/video_frame_manifest.csv"
    frame_manifest.write_text(
        "source_video_path,source_video_relative_path,frame_file,frame_index,estimated_frame_time_ms\n"
        f"/source/video.mov,video.mov,{frame},2,3000\n",
        encoding="utf-8",
    )

    rows, summary = build_full_source_map(tmp_path)

    assert summary["record_count"] == 2
    assert summary["has_original_source_path_count"] == 1
    assert summary["has_source_video_path_count"] == 1
    assert {row["upstream_source_type"] for row in rows} == {"a9t_normal", "c4_video_frame"}
    assert all(row["derived_image_path"] and row["derived_image_relative_path"] for row in rows)
    assert next(row for row in rows if row["image_group_type"] == "video_frame")["estimated_frame_time_ms"] == 3000


def test_resume_skips_existing_success_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    samples = [
        {
            "smoke_image_id": "normal_preview-00001",
            "image_path": "/tmp/one.jpg",
            "derived_image_path": "/tmp/one.jpg",
        },
        {
            "smoke_image_id": "normal_preview-00002",
            "image_path": "/tmp/two.jpg",
            "derived_image_path": "/tmp/two.jpg",
        },
    ]
    result_path = workspace / "yoloe_output/yoloe_results.jsonl"
    write_jsonl(
        result_path,
        [
            {
                "smoke_image_id": "normal_preview-00001",
                "image_path": "/tmp/one.jpg",
                "derived_image_path": "/tmp/one.jpg",
                "success": True,
                "detected_object_count": 0,
                "detections": [],
            }
        ],
    )

    def fake_batch(role, batch_index, tasks, adapter, workspace_arg, script, output_dir, role_timeout_seconds):
        assert [task["derived_image_path"] for task in tasks] == ["/tmp/two.jpg"]
        return (
            [
                {
                    "smoke_image_id": "normal_preview-00002",
                    "image_path": "/tmp/two.jpg",
                    "derived_image_path": "/tmp/two.jpg",
                    "success": True,
                    "detected_object_count": 1,
                    "detections": [{"label": "x"}],
                }
            ],
            {"worker_id": "yoloe-1", "returncode": 0, "elapsed_seconds": 0.01},
        )

    monkeypatch.setattr(low_cost_smoke, "_run_model_batch", fake_batch)

    rows, metadata = run_model_role(
        "yoloe",
        samples,
        {"model_name": "test", "model_path": "/tmp/model"},
        workspace,
        workers=2,
        resume=True,
    )

    assert metadata["skipped_success_count"] == 1
    assert metadata["pending_count"] == 1
    assert [row["derived_image_path"] for row in rows] == ["/tmp/one.jpg", "/tmp/two.jpg"]
    assert all(row["success"] for row in rows)


def test_yoloe_registry_metadata_and_class_list_are_passed_to_worker_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = low_cost_smoke.load_yoloe_prompt_registry(_write_prompt_registry(tmp_path / "registry.json"), "A_CORE,B_EXTENDED")
    samples = [
        {
            "smoke_image_id": "normal_preview-00001",
            "image_path": "/tmp/one.jpg",
            "derived_image_path": "/tmp/one.jpg",
        }
    ]
    captured_tasks: list[dict[str, object]] = []

    def fake_batch(role, batch_index, tasks, adapter, workspace_arg, script, output_dir, role_timeout_seconds):
        captured_tasks.extend(tasks)
        row = {
            **tasks[0],
            "success": True,
            "detected_object_count": 1,
            "detections": [{"label": "person"}],
        }
        row.pop("yoloe_class_list", None)
        return [row], {"worker_id": "yoloe-1", "returncode": 0, "elapsed_seconds": 0.01}

    monkeypatch.setattr(low_cost_smoke, "_run_model_batch", fake_batch)

    rows, _ = run_model_role(
        "yoloe",
        samples,
        {"model_name": "test", "model_path": "/tmp/model"},
        tmp_path / "workspace",
        yoloe_registry=registry,
    )

    assert captured_tasks[0]["yoloe_class_list"] == ["person", "car", "sign", "screen"]
    assert captured_tasks[0]["prompt_registry_path"] == str(tmp_path / "registry.json")
    assert captured_tasks[0]["yoloe_layers_enabled"] == ["A_CORE", "B_EXTENDED"]
    assert rows[0]["prompt_registry_path"] == str(tmp_path / "registry.json")
    assert rows[0]["yoloe_class_list_sha256"] == registry["yoloe_class_list_sha256"]


def test_yoloe_label_hit_summary_counts_fake_detection_and_marks_empty_as_not_valid() -> None:
    summary = low_cost_smoke.build_yoloe_label_hit_summary(
        [
            {"detections": [{"label": "person"}, {"label": "person"}, {"label": "car"}]},
            {"detections": [{"class_name": "car"}]},
            {"detections": []},
        ]
    )

    assert summary["total_images"] == 3
    assert summary["images_with_any_detection"] == 2
    assert summary["total_detection_count"] == 4
    assert summary["label_detection_counts"] == {"car": 2, "person": 2}
    assert summary["label_image_counts"] == {"car": 2, "person": 1}
    assert summary["nonzero_detection_evidence"] is True
    assert low_cost_smoke.build_yoloe_label_hit_summary([{"detections": []}])["nonzero_detection_evidence"] is False


def test_cross_model_mode_submits_ocr_and_embedding_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    samples = [
        {
            "schema_version": "x",
            "stage": "x",
            "smoke_image_id": "sample-1",
            "image_id": "sample-1",
            "image_path": "/tmp/one.jpg",
            "image_relative_path": "one.jpg",
            "derived_image_path": "/tmp/one.jpg",
            "derived_image_relative_path": "one.jpg",
            "image_group_type": "normal_preview",
            "upstream_source_type": "a9t_normal",
        }
    ]
    source_summary = {
        "record_count": 1,
        "unique_derived_images": 1,
        "has_original_source_path_count": 1,
        "has_source_video_path_count": 0,
        "group_counts": {"normal_preview": 1, "timelapse_keyframe": 0, "video_frame": 0},
        "source_jpg_root": str(tmp_path),
    }
    monkeypatch.setattr(low_cost_smoke, "build_run_inputs", lambda *args, **kwargs: (samples, source_summary, source_summary["group_counts"], "full"))
    monkeypatch.setattr(low_cost_smoke, "build_full_source_map", lambda *args, **kwargs: (samples, source_summary))
    monkeypatch.setattr(
        low_cost_smoke,
        "load_local_adapter_config",
        lambda workspace, env_file: {
            "adapters": {
                "ocr_detector": {"model_name": "ocr", "model_path": "/tmp/ocr"},
                "visual_embedding": {"model_name": "embed", "model_path": "/tmp/embed"},
                "yoloe_object_detector": {"model_name": "yoloe", "model_path": "/tmp/yoloe"},
            }
        },
    )

    calls: dict[str, tuple[float, float]] = {}

    def fake_run_model_role(
        role, samples_arg, adapter, workspace, workers=1, resume=False, role_timeout_seconds=900, yoloe_registry=None
    ):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        calls[role] = (start, end)
        rows = [{**samples_arg[0], "success": True}]
        return rows, {
            "workers": workers,
            "resume": resume,
            "pending_count": len(samples_arg),
            "skipped_success_count": 0,
            "returncode": 0,
            "elapsed_seconds": end - start,
            "started_at": "start",
            "ended_at": "end",
            "started_monotonic": start,
            "ended_monotonic": end,
            "role_timeout_seconds": role_timeout_seconds,
        }

    monkeypatch.setattr(low_cost_smoke, "run_model_role", fake_run_model_role)

    summary = low_cost_smoke.run_smoke(
        tmp_path,
        tmp_path / "workspace",
        tmp_path / ".env",
        full_run=True,
        roles=["ocr", "visual_embedding"],
        execution_mode="cross-model",
        ocr_workers=3,
        embedding_workers=2,
        role_timeout_seconds=0,
        resume=True,
    )

    assert set(calls) == {"ocr", "visual_embedding"}
    assert calls["ocr"][0] < calls["visual_embedding"][1]
    assert calls["visual_embedding"][0] < calls["ocr"][1]
    assert summary["execution_mode"] == "cross-model"
    assert summary["roles"] == ["ocr", "visual_embedding"]
    assert summary["ocr_workers"] == 3
    assert summary["embedding_workers"] == 2
    assert summary["ocr_embedding_overlap_seconds"] > 0
    assert summary["max_active_total_model_workers"] == 5
    assert (tmp_path / "workspace/reports/run_progress.json").exists()


def test_ocr_runtime_streams_each_result_and_progress_with_single_image_failure(tmp_path: Path) -> None:
    fake_module = tmp_path / "paddleocr.py"
    fake_module.write_text(
        "class PaddleOCR:\n"
        "    def __init__(self, **kwargs): pass\n"
        "    def predict(self, path):\n"
        "        if 'bad' in path: raise RuntimeError('bad image')\n"
        "        return [{'rec_texts': ['ok']}]\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    script = prepare_runtime_script(workspace, "ocr")
    input_path = tmp_path / "ocr_input.jsonl"
    output_path = tmp_path / "ocr_results.jsonl"
    progress_path = tmp_path / "ocr_progress_worker_1.json"
    write_jsonl(
        input_path,
        [
            {"smoke_image_id": "one", "image_path": "/tmp/good.jpg", "derived_image_path": "/tmp/good.jpg"},
            {"smoke_image_id": "two", "image_path": "/tmp/bad.jpg", "derived_image_path": "/tmp/bad.jpg"},
        ],
    )
    env = {"PYTHONPATH": str(tmp_path)}
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--model-path",
            "/tmp/model",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--worker-id",
            "1",
            "--progress",
            str(progress_path),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    rows = low_cost_smoke.read_jsonl(output_path)
    assert len(rows) == 2
    assert [row["success"] for row in rows] == [True, False]
    progress = progress_path.read_text(encoding="utf-8")
    assert '"completed": 2' in progress
    assert '"failed": 1' in progress


def test_embedding_runtime_template_is_streaming_and_progress_enabled() -> None:
    assert "append(args.output, row)" in low_cost_smoke.EMBEDDING_SCRIPT
    assert "write_progress(args.progress" in low_cost_smoke.EMBEDDING_SCRIPT
    assert "except Exception as exc:" in low_cost_smoke.EMBEDDING_SCRIPT


def test_yoloe_runtime_template_is_streaming_and_progress_enabled() -> None:
    assert "append(args.output, row)" in low_cost_smoke.YOLOE_SCRIPT
    assert "write_progress(args.progress" in low_cost_smoke.YOLOE_SCRIPT
    assert "except Exception as exc:" in low_cost_smoke.YOLOE_SCRIPT
    assert "set_classes" in low_cost_smoke.YOLOE_SCRIPT
    assert "get_text_pe" in low_cost_smoke.YOLOE_SCRIPT
    assert "yoloe_open_vocab_class_injection_unsupported" in low_cost_smoke.YOLOE_SCRIPT


def test_staged_6w_benchmark_records_order_progress_summary_and_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = []
    for index in range(3):
        samples.append(
            {
                "schema_version": "x",
                "stage": "x",
                "smoke_image_id": f"sample-{index}",
                "image_id": f"sample-{index}",
                "image_path": f"/tmp/{index}.jpg",
                "image_relative_path": f"{index}.jpg",
                "derived_image_path": f"/tmp/{index}.jpg",
                "derived_image_relative_path": f"{index}.jpg",
                "image_group_type": "normal_preview",
                "upstream_source_type": "a9t_normal",
            }
        )
    source_summary = {
        "record_count": 3,
        "unique_derived_images": 3,
        "has_original_source_path_count": 3,
        "has_source_video_path_count": 0,
        "group_counts": {"normal_preview": 3, "timelapse_keyframe": 0, "video_frame": 0},
        "source_jpg_root": str(tmp_path),
    }
    monkeypatch.setattr(low_cost_smoke, "build_run_inputs", lambda *args, **kwargs: (samples, source_summary, source_summary["group_counts"], "full"))
    monkeypatch.setattr(low_cost_smoke, "build_full_source_map", lambda *args, **kwargs: (samples, source_summary))
    monkeypatch.setattr(
        low_cost_smoke,
        "load_local_adapter_config",
        lambda workspace, env_file: {
            "adapters": {
                "ocr_detector": {"model_name": "ocr", "model_path": "/tmp/ocr"},
                "visual_embedding": {"model_name": "embed", "model_path": "/tmp/embed"},
                "yoloe_object_detector": {"model_name": "yoloe", "model_path": "/tmp/yoloe"},
            }
        },
    )
    call_order: list[str] = []

    def fake_run_model_role(
        role, samples_arg, adapter, workspace, workers=1, resume=False, role_timeout_seconds=900, yoloe_registry=None
    ):
        call_order.append(role)
        output_dir = workspace / low_cost_smoke.ROLE_OUTPUT_DIRS[role]
        output_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                **sample,
                **(low_cost_smoke.yoloe_task_metadata(yoloe_registry) if role == "yoloe" else {}),
                "success": True,
                "detections": [{"label": "person"}] if role == "yoloe" else [],
            }
            for sample in samples_arg
        ]
        low_cost_smoke.write_jsonl(output_dir / low_cost_smoke.ROLE_RESULT_FILENAMES[role], rows)
        (output_dir / f"{role}_marker.bin").write_bytes(b"x" * (workers + 1))
        start = time.monotonic()
        end = start + 0.01
        return rows, {
            "workers": workers,
            "resume": resume,
            "pending_count": len(samples_arg),
            "skipped_success_count": 0,
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "started_at": "start",
            "ended_at": "end",
            "started_monotonic": start,
            "ended_monotonic": end,
            "role_timeout_seconds": role_timeout_seconds,
        }

    monkeypatch.setattr(low_cost_smoke, "run_model_role", fake_run_model_role)

    workspace = tmp_path / "workspace"
    summary = low_cost_smoke.run_smoke(
        tmp_path,
        workspace,
        tmp_path / ".env",
        full_run=True,
        roles=["visual_embedding", "yoloe", "ocr"],
        stage_order=["visual_embedding", "yoloe", "ocr"],
        execution_mode="staged",
        embedding_workers=6,
        yoloe_workers=6,
        ocr_workers=6,
        role_timeout_seconds=0,
        prompt_registry=_write_prompt_registry(tmp_path / "registry.json"),
        yoloe_layers="A_CORE,B_EXTENDED",
    )

    assert call_order == ["visual_embedding", "yoloe", "ocr"]
    assert summary["stage_order"] == ["visual_embedding", "yoloe", "ocr"]
    assert summary["embedding_workers"] == 6
    assert summary["yoloe_workers"] == 6
    assert summary["ocr_workers"] == 6
    assert summary["total_input_jpg"] == 3
    assert summary["stage_timings"]["visual_embedding"]["attempted"] == 3
    assert "stage_cpu_summary" in summary
    assert "stage_mem_summary" in summary
    assert summary["output_size_by_dir"]["embedding_output"] > 0
    assert summary["alignment_checks"]["visual_embedding_result_count"] == 3
    assert summary["alignment_checks"]["yoloe_result_count"] == 3
    assert summary["alignment_checks"]["ocr_result_count"] == 3
    assert summary["alignment_checks"]["missing_in_visual_embedding"] == 0
    assert summary["alignment_checks"]["missing_in_yoloe"] == 0
    assert summary["alignment_checks"]["missing_in_ocr"] == 0
    assert summary["prompt_registry_schema_version"] == "test-1"
    assert summary["yoloe_layers_enabled"] == ["A_CORE", "B_EXTENDED"]
    assert summary["yoloe_class_count_total"] == 4
    assert summary["yoloe_label_hit_summary"]["images_with_any_detection"] == 3
    assert (workspace / "reports/yoloe_registry_summary.json").exists()
    assert (workspace / "reports/yoloe_label_hit_summary.json").exists()
    assert (workspace / "reports/staged_6w_benchmark_summary.json").exists()
    progress = (workspace / "reports/run_progress.json").read_text(encoding="utf-8")
    assert '"stage_order": [' in progress
    assert '"current_stage": "finished"' in progress
