from __future__ import annotations

import json
import shutil
from pathlib import Path

from media_archive import app


TEST_ROOT = Path("/tmp/media_archive_v052_pipeline_test")
SOURCE = TEST_ROOT / "source"
WORKSPACE = TEST_ROOT / "workspace"


def clean() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    SOURCE.mkdir(parents=True)
    WORKSPACE.mkdir(parents=True)


def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def create_source() -> None:
    write_file(SOURCE / "a/one.jpg", b"image-1")
    write_file(SOURCE / "a/two.ARW", b"raw-image-previewable")
    write_file(SOURCE / "v/one.mov", b"video-1")
    write_file(SOURCE / "audio/one.wav", b"audio-1")
    write_file(SOURCE / "text/one.txt", b"text-1")
    write_file(SOURCE / "raw/A001.braw", b"braw")
    write_file(SOURCE / "raw/C001.CRM", b"crm")
    write_file(SOURCE / "raw/G001.GPR", b"gpr")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def run_pipeline(*extra: str) -> int:
    return app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "plan_only",
            "--fresh",
            "--preview-backend",
            "test_copy_jpg",
            "--video-runner",
            "fake_ffmpeg_jpg",
            *extra,
        ]
    )


def write_generated_real_local_queue(task_count: int = 1) -> None:
    stage = WORKSPACE / "stages/v0.5"
    write_jsonl(
        stage / "manifests/analysis_tasks.jsonl",
        [
            {
                "schema_version": "0.5.2",
                "task_id": f"task-{index}",
                "folder_id": "folder-1",
                "source_root": str(SOURCE),
                "source_path": str(SOURCE / "a/one.jpg"),
                "source_relative_path": f"a/one-{index}.jpg",
                "source_media_type": "image",
                "model_input_path": str(WORKSPACE / f"v02/image_preview/a/one-{index}.jpg"),
                "model_input_kind": "image_preview",
                "frame_time_ms": None,
                "route_hint": "visual",
                "adapter_targets": ["yoloe_object_detector"],
                "status": "blocked",
                "error_code": "probe_failed",
                "model_identity": None,
            }
            for index in range(1, task_count + 1)
        ],
    )
    write_json(
        stage / "state/analysis_run_state.json",
        {
            "schema_version": "0.5.2",
            "stage": "V0.5-2",
            "status": "ready_for_user_real_local_validation",
            "last_successful_checkpoint": "analysis_tasks_generated",
            "task_count": task_count,
            "source_read_only": True,
            "model_loaded": False,
            "search_index_built": False,
        },
    )


def write_success_probe_config(tmp_path: Path) -> Path:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text("import sys\nsys.stdin.read()\nprint('{\"status\":\"success\"}')\n", encoding="utf-8")
    config = tmp_path / "adapters.json"
    write_json(
        config,
        {
            "adapters": {
                "yoloe_object_detector": {
                    "model_path": str(model_dir),
                    "weights_path_or_id": str(model_dir),
                    "runtime": "python3",
                    "probe_command": ["python3", str(probe)],
                    "probe_timeout_seconds": 5,
                }
            }
        },
    )
    return config


def write_probe_env(tmp_path: Path, *, probe: Path | None = None, model_dir: Path | None = None) -> Path:
    resolved_model = model_dir or tmp_path / "model"
    resolved_model.mkdir(exist_ok=True)
    lines = [f"MEDIA_ARCHIVE_YOLOE_MODEL_PATH={resolved_model}"]
    if probe is not None:
        lines.append(f"V05_YOLOE_PROBE_COMMAND=python3 {probe}")
    env = tmp_path / ".env.v05_local_models"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


def write_probe_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_v05_pipeline_handoff_builds_tasks_from_full_pipeline_outputs():
    clean()
    create_source()

    assert run_pipeline() == 0

    stage = WORKSPACE / "stages/v0.5"
    report = read_json(stage / "reports/v05_2_pipeline_handoff_report.json")
    tasks = read_jsonl(stage / "manifests/analysis_tasks.jsonl")
    outputs = read_jsonl(stage / "manifests/analysis_outputs.jsonl")
    folder_states = read_jsonl(stage / "state/folder_batch_state.jsonl")
    resource = read_json(stage / "telemetry/resource_report.json")
    samples = read_jsonl(stage / "telemetry/resource_samples.jsonl")

    assert report["validation_status"] == "PASS_WITH_BACKLOG"
    assert report["pipeline_handoff_source"] == "V0.1/V0.2/V0.3/V0.4 formal outputs"
    assert report["handwritten_source_only_manifest_used_as_real_validation"] is False
    assert report["v05_3_started"] is False
    assert report["search_index_built"] is False
    assert (WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl").exists()
    assert (WORKSPACE / "v02/unified/unified_media_manifest.jsonl").exists()
    assert (WORKSPACE / "stages/v0.3/manifests/incremental_diff_plan.jsonl").exists()
    assert (WORKSPACE / "stages/v0.4/reports/v04_contract_compat_report.json").exists()

    assert tasks
    assert outputs
    assert folder_states
    assert samples
    assert resource["samples"]
    assert all("source_path" in task and "model_input_path" in task for task in tasks)
    assert {task["folder_id"] for task in tasks} <= {row["folder_id"] for row in folder_states}
    assert any(task["source_path"] != task["model_input_path"] for task in tasks if task["route_hint"] == "visual")
    assert any(task["source_relative_path"].endswith("two.ARW") and task["route_hint"] == "visual" for task in tasks)

    visual_tasks = [task for task in tasks if task["route_hint"] == "visual"]
    assert visual_tasks
    assert all(
        {"yoloe_object_detector", "ocr_detector", "visual_embedding"} <= set(task["adapter_targets"])
        for task in visual_tasks
    )
    qwen_tasks = [task for task in visual_tasks if "qwen_vl_caption" in task["adapter_targets"]]
    assert qwen_tasks
    assert len(qwen_tasks) < len(visual_tasks)

    frame_tasks = [task for task in tasks if task["model_input_kind"] == "video_frame"]
    assert frame_tasks
    assert all(isinstance(task["frame_time_ms"], int) for task in frame_tasks)

    braw_task = next(task for task in tasks if task["source_relative_path"].endswith("A001.braw"))
    crm_task = next(task for task in tasks if task["source_relative_path"].endswith("C001.CRM"))
    gpr_task = next(task for task in tasks if task["source_relative_path"].endswith("G001.GPR"))
    assert braw_task["route_hint"] == "audio"
    assert crm_task["route_hint"] == "audio"
    assert all("visual_embedding" not in task["adapter_targets"] for task in [braw_task, crm_task])
    assert gpr_task["status"] == "blocked"
    assert gpr_task["route_hint"] == "metadata"

    audio_tasks = [task for task in tasks if task["route_hint"] == "audio"]
    assert audio_tasks
    for task in audio_tasks:
        assert task["adapter_targets"][:3] == ["ffmpeg_audio_probe_extract", "vad_segmenter", "whisper_transcriber"]
        assert task["adapter_targets"][-1] == "text_embedding"

    assert all("stage_status" in row and row["last_successful_checkpoint"] == "task_queue_generated" for row in folder_states)
    required_sample_fields = {
        "timestamp",
        "source_root",
        "folder_id",
        "stage",
        "adapter_name",
        "active_workers",
        "pending_queue_size",
        "running_count",
        "completed_count",
        "blocked_count",
        "failed_count",
        "cpu_percent",
        "memory_used_bytes",
        "memory_available_bytes",
        "memory_pressure",
        "swap_used_bytes",
        "disk_read_bytes_per_sec",
        "disk_write_bytes_per_sec",
        "workspace_free_bytes",
        "source_volume_free_bytes",
        "model_volume_free_bytes",
        "avg_task_duration_ms",
        "p95_task_duration_ms",
        "resource_status",
    }
    assert required_sample_fields <= set(samples[0])
    assert not (stage / "search").exists()
    assert not (stage / "index").exists()


def test_v05_pipeline_resume_reuses_outputs_and_fresh_only_clears_v05_stage():
    clean()
    create_source()
    assert run_pipeline() == 0
    v01_manifest = WORKSPACE / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl"
    v01_before = v01_manifest.read_text(encoding="utf-8")
    marker = WORKSPACE / "stages/v0.5/state/marker.tmp"
    marker.write_text("remove me", encoding="utf-8")

    assert app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "plan_only",
            "--resume",
            "--preview-backend",
            "test_copy_jpg",
            "--video-runner",
            "fake_ffmpeg_jpg",
        ]
    ) == 0
    assert marker.exists()

    assert run_pipeline() == 0
    assert not marker.exists()
    assert v01_manifest.read_text(encoding="utf-8") == v01_before


def test_v05_pipeline_rejects_model_root_source(tmp_path):
    clean()
    model_root = tmp_path / "model"
    model_root.mkdir()
    env = tmp_path / ".env.v05_local_models"
    env.write_text(f"MEDIA_ARCHIVE_MODEL_ROOT={model_root}\n", encoding="utf-8")

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(model_root),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "plan_only",
            "--env-file",
            str(env),
            "--fresh",
        ]
    )
    assert result == 1


def test_real_local_first_pipeline_run_generates_tasks_then_probes(capsys):
    clean()
    create_source()

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--fresh",
            "--preview-backend",
            "test_copy_jpg",
            "--video-runner",
            "fake_ffmpeg_jpg",
        ]
    )
    captured = capsys.readouterr()
    stage = WORKSPACE / "stages/v0.5"
    tasks = read_jsonl(stage / "manifests/analysis_tasks.jsonl")
    outputs = read_jsonl(stage / "manifests/analysis_outputs.jsonl")
    state = read_json(stage / "state/analysis_run_state.json")
    summary = read_json(stage / "reports/v05_2_controlled_analysis_summary.json")

    assert result in {0, 2}
    if result == 2:
        assert "V0.5-2 BLOCKED" in captured.out
    assert tasks
    assert outputs
    assert len(outputs) == summary["selected_adapter_target_count"]
    assert len(outputs) <= 8
    assert state["status"] in {"completed", "blocked"}
    assert state["last_successful_checkpoint"] in {"analysis_tasks_generated", "real_local_execution_phase"}
    assert summary["validation_status"] in {"PASS", "BLOCKED"}
    assert summary["task_count"] == len(tasks)
    assert summary["candidate_task_count"] > 0
    assert summary["selected_adapter_target_count"] > 0
    assert summary["execution_started"] is True
    assert summary.get("blocked_reason_code") != "all_tasks_filtered"
    assert summary.get("blocked_reason_code") != "probe_command_missing"
    assert summary["real_adapter_invoked_or_probed"] is True
    assert (stage / "reports/v05_2_pipeline_handoff_report.json").exists()
    assert not (stage / "search").exists()
    assert not (stage / "index").exists()


def test_real_local_safe_sample_is_bounded_per_adapter(capsys):
    clean()
    create_source()
    write_generated_real_local_queue(task_count=3)

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--resume",
        ]
    )
    capsys.readouterr()
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    outputs = [
        row
        for row in read_jsonl(WORKSPACE / "stages/v0.5/manifests/analysis_outputs.jsonl")
        if row.get("run_id") == summary["run_id"]
    ]

    assert result == 2
    assert summary["candidate_task_count"] == 3
    assert summary["selected_adapter_target_count"] == 1
    assert len(outputs) == 1
    assert outputs[0]["adapter_name"] == "yoloe_object_detector"


def test_real_local_resume_blocked_probe_failed_task_is_current_run_candidate(capsys):
    clean()
    create_source()
    write_generated_real_local_queue()

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--resume",
        ]
    )
    captured = capsys.readouterr()
    stage = WORKSPACE / "stages/v0.5"
    state = read_json(stage / "state/analysis_run_state.json")
    summary = read_json(stage / "reports/v05_2_controlled_analysis_summary.json")
    resource = read_json(stage / "telemetry/resource_report.json")

    assert result == 2
    assert captured.out
    assert "V0.5-2 BLOCKED" in captured.out
    assert state["status"] == "blocked"
    assert state["last_successful_checkpoint"] == "analysis_tasks_generated"
    assert summary["validation_status"] == "BLOCKED"
    assert summary["blocked_reason_code"] == "model_path_missing"
    assert summary["candidate_task_count"] == 1
    assert summary["selected_adapter_target_count"] == 1
    assert summary["execution_started"] is True
    assert summary["per_task_outputs_written"] is True
    assert summary["real_adapter_invoked_or_probed"] is True
    assert summary["task_count"] == 1
    assert summary["run_id"] == state["run_id"] == resource["run_id"]
    outputs = read_jsonl(stage / "manifests/analysis_outputs.jsonl")
    assert len([row for row in outputs if row.get("run_id") == summary["run_id"]]) == 1
    assert outputs[-1]["status"] == "blocked"
    assert outputs[-1]["reason_code"] == "model_path_missing"


def test_real_local_resume_default_probe_command_runs_when_env_has_only_model_path(tmp_path, capsys):
    clean()
    create_source()
    write_generated_real_local_queue()
    env = write_probe_env(tmp_path)

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--env-file",
            str(env),
            "--resume",
        ]
    )
    captured = capsys.readouterr()
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    outputs = [
        row
        for row in read_jsonl(WORKSPACE / "stages/v0.5/manifests/analysis_outputs.jsonl")
        if row.get("run_id") == summary["run_id"]
    ]

    assert result == 0
    assert "V0.5-2 BLOCKED" not in captured.out
    assert summary["validation_status"] == "PASS"
    assert "blocked_reason_code" not in summary
    assert summary["candidate_task_count"] == 1
    assert summary["selected_adapter_target_count"] == 1
    assert summary["adapter_statuses"][0]["status"] == "success"
    assert summary["adapter_statuses"][0]["probe_command_or_null"][1].endswith("local_probe.py")
    assert outputs[0]["status"] == "success"


def test_real_local_resume_probe_command_nonzero_records_returncode(tmp_path):
    clean()
    create_source()
    write_generated_real_local_queue()
    probe = write_probe_script(
        tmp_path / "bad_probe.py",
        "import sys\nsys.stdin.read()\nsys.stderr.write('probe failed')\nraise SystemExit(9)\n",
    )
    env = write_probe_env(tmp_path, probe=probe)

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--env-file",
            str(env),
            "--resume",
        ]
    )
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    status = summary["adapter_statuses"][0]

    assert result == 2
    assert summary["blocked_reason_code"] == "adapter_probe_failed"
    assert status["error_code"] == "adapter_probe_failed"
    assert status["returncode"] == 9
    assert "probe failed" in status["stderr_tail_or_null"]


def test_real_local_resume_probe_command_success_writes_current_run_pass(tmp_path):
    clean()
    create_source()
    write_generated_real_local_queue()
    write_jsonl(
        WORKSPACE / "stages/v0.5/manifests/analysis_outputs.jsonl",
        [{"run_id": None, "status": "success", "adapter_mode": "fake_fixture"}],
    )
    probe = write_probe_script(
        tmp_path / "ok_probe.py",
        "import json, sys\nsys.stdin.read()\nprint(json.dumps({'status': 'success'}))\n",
    )
    env = write_probe_env(tmp_path, probe=probe)

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--env-file",
            str(env),
            "--resume",
        ]
    )
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    outputs = [
        row
        for row in read_jsonl(WORKSPACE / "stages/v0.5/manifests/analysis_outputs.jsonl")
        if row.get("run_id") == summary["run_id"]
    ]

    assert result == 0
    assert summary["validation_status"] == "PASS"
    assert summary["real_adapter_invoked_or_probed"] is True
    assert summary["current_run_counts"]["success"] == 1
    assert len(outputs) == 1
    assert outputs[0]["status"] == "success"


def test_real_local_resume_missing_env_file_blocks_with_specific_reason(capsys):
    clean()
    create_source()
    write_generated_real_local_queue()

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--env-file",
            str(WORKSPACE / "missing.env"),
            "--resume",
        ]
    )
    captured = capsys.readouterr()
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")

    assert result == 2
    assert "V0.5-2 BLOCKED" in captured.out
    assert summary["blocked_reason_code"] == "env_file_missing"
    assert summary["validation_status"] == "BLOCKED"
    assert summary["candidate_task_count"] == 1
    assert summary["selected_adapter_target_count"] == 1
    assert summary["current_run_counts"]["blocked"] == 1


def test_real_local_resume_ignores_old_null_run_outputs_for_current_verdict():
    clean()
    create_source()
    write_generated_real_local_queue()
    write_jsonl(
        WORKSPACE / "stages/v0.5/manifests/analysis_outputs.jsonl",
        [{"run_id": None, "status": "success", "adapter_mode": "fake_fixture"}],
    )

    result = app.main(
        [
            "v05-run-analysis-pipeline",
            "--source",
            str(SOURCE),
            "--workspace",
            str(WORKSPACE),
            "--profile",
            "safe",
            "--adapter-mode",
            "real_local",
            "--resume",
        ]
    )
    summary = read_json(WORKSPACE / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")

    assert result == 2
    assert summary["run_id"]
    assert summary["current_run_counts"] == {"success": 0, "blocked": 1, "failed": 0, "skipped": 0}
    assert summary["validation_status"] == "BLOCKED"
    assert summary["blocked_reason_code"] == "model_path_missing"
