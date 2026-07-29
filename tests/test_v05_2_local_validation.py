from __future__ import annotations

import json
import subprocess
from pathlib import Path

from media_archive.v05.manifest import read_json, read_jsonl
from media_archive.v05.adapter_config import load_local_adapter_config
from media_archive.v05.real_adapters import probe_adapter


ROOT = "/Users/yourname/Documents/AI-Local/media-archive-clean"


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def manifest_rows(tmp_path: Path) -> list[dict[str, object]]:
    source = tmp_path / "source"
    image = source / "visual/one.jpg"
    audio = source / "audio/one.wav"
    text = source / "text/one.txt"
    image.parent.mkdir(parents=True, exist_ok=True)
    audio.parent.mkdir(parents=True, exist_ok=True)
    text.parent.mkdir(parents=True, exist_ok=True)
    image.write_text("image", encoding="utf-8")
    audio.write_text("audio", encoding="utf-8")
    text.write_text("hello text", encoding="utf-8")
    return [
        {
            "schema_version": "0.5.2",
            "media_id": "visual-one",
            "folder_id": "visual",
            "source_path": str(image),
            "source_relative_path": "visual/one.jpg",
            "media_type": "visual",
            "route_hint": "visual",
            "status": "active",
            "content_hash": "visual-one",
            "existing_frame_path": str(image),
            "frame_id": "frame-1",
            "hit_time_ms": 1000,
        },
        {
            "schema_version": "0.5.2",
            "media_id": "audio-one",
            "folder_id": "audio",
            "source_path": str(audio),
            "source_relative_path": "audio/one.wav",
            "media_type": "audio",
            "route_hint": "audio",
            "status": "active",
            "content_hash": "audio-one",
        },
        {
            "schema_version": "0.5.2",
            "media_id": "text-one",
            "folder_id": "text",
            "source_path": str(text),
            "source_relative_path": "text/one.txt",
            "media_type": "text",
            "route_hint": "text",
            "status": "active",
            "content_hash": "text-one",
            "text_content_path": str(text),
        },
    ]


def run_local(workspace: Path, manifest: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            "-m",
            "apps.media_archive.app",
            "v05-run-analysis-local",
            "--workspace",
            str(workspace),
            "--input-manifest",
            str(manifest),
            *extra,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def stub_script(path: Path, stdout: str, exit_code: int = 0) -> None:
    path.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({stdout!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )


def full_adapter_config(path: Path, probe_script: Path, model_dir: Path) -> None:
    adapters = {}
    for name in [
        "yoloe_object_detector",
        "ocr_detector",
        "visual_embedding",
        "qwen_vl_caption",
        "ffmpeg_audio_probe_extract",
        "vad_segmenter",
        "whisper_transcriber",
        "text_embedding",
    ]:
        adapters[name] = {
            "model_path": str(model_dir),
            "weights_path_or_id": str(model_dir),
            "runtime": "python3",
            "probe_command": ["python3", str(probe_script)],
            "probe_timeout_seconds": 5,
        }
    write_json(path, {"adapters": adapters})


def test_local_real_stub_probe_outputs_model_identity_and_no_search_index(tmp_path):
    workspace = tmp_path / "workspace"
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(manifest, manifest_rows(tmp_path))
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    probe = tmp_path / "probe.py"
    stub_script(
        probe,
        json.dumps(
            {
                "status": "success",
                "labels": [{"label": "stub", "confidence": 0.8}],
                "text": "stub text",
                "embedding_ref": "stub-embedding",
                "segments": [{"start_time_ms": 0, "end_time_ms": 1000, "text": "stub speech"}],
            }
        ),
    )
    config = tmp_path / "adapters.json"
    full_adapter_config(config, probe, model_dir)

    result = run_local(workspace, manifest, "--adapter-mode", "real_local", "--adapter-config", str(config), "--fresh")
    assert result.returncode == 0, result.stdout + result.stderr

    stage = workspace / "stages/v0.5"
    summary = read_json(stage / "reports/v05_2_controlled_analysis_summary.json")
    tasks = read_jsonl(stage / "manifests/analysis_tasks.jsonl")
    outputs = read_jsonl(stage / "manifests/analysis_outputs.jsonl")
    evidence = read_jsonl(stage / "evidence/evidence_records.jsonl")
    resource = read_json(stage / "telemetry/resource_report.json")
    assert summary["real_local_count"] > 0
    assert summary["final_search_index_built"] is False
    assert not (stage / "search").exists()
    assert not (stage / "index").exists()
    assert all("model_identity" in task for task in tasks if task["route"] in {"visual", "audio", "embedding"})
    assert all(task["adapter_mode"] == "real_local" for task in tasks if task["route"] in {"visual", "audio", "embedding"})
    assert any(row["evidence_type"] == "vl_caption" for row in evidence)
    assert any(row["evidence_type"] == "text_embedding" for row in evidence)
    assert resource["profile"] == "safe"
    assert resource["cpu_percent_samples_or_null"] is None
    assert outputs
    for row in evidence:
        for key in ("hit_time_ms", "start_time_ms", "end_time_ms"):
            assert row[key] is None or isinstance(row[key], int)


def test_missing_dependency_and_invalid_json_probe_are_blocked(tmp_path):
    workspace = tmp_path / "workspace"
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(manifest, manifest_rows(tmp_path))
    bad_probe = tmp_path / "bad_probe.py"
    stub_script(bad_probe, "not-json")
    config = tmp_path / "adapters.json"
    missing = tmp_path / "missing-model"
    adapters = {
        "yoloe_object_detector": {"model_path": str(missing), "weights_path_or_id": str(missing), "probe_command": ["python3", str(bad_probe)]},
        "ocr_detector": {"model_path": str(tmp_path), "weights_path_or_id": str(tmp_path), "probe_command": ["python3", str(bad_probe)]},
    }
    write_json(config, {"adapters": adapters})

    result = run_local(workspace, manifest, "--adapter-mode", "real_local", "--adapter-config", str(config), "--fresh")
    assert result.returncode == 0
    summary = read_json(workspace / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    error_codes = {row["error_code"] for row in summary["adapter_statuses"] if row["status"] == "blocked"}
    assert "model_path_missing" in error_codes
    assert "probe_json_invalid" in error_codes


def test_plan_only_ffmpeg_task_schema_fail_and_rerun_fresh_rules(tmp_path):
    workspace = tmp_path / "workspace"
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(manifest, manifest_rows(tmp_path))

    first = run_local(workspace, manifest, "--adapter-mode", "plan_only", "--fresh")
    assert first.returncode == 0, first.stdout + first.stderr
    tasks = read_jsonl(workspace / "stages/v0.5/manifests/analysis_tasks.jsonl")
    ffmpeg_task = next(row for row in tasks if row["task_type"] == "ffmpeg_audio_probe_extract")
    assert ffmpeg_task["adapter_mode"] == "plan_only"
    assert ffmpeg_task["allow_frame_extract"] is False
    assert ffmpeg_task["planned_output_path"].startswith(str(workspace / "stages/v0.5"))

    second = run_local(workspace, manifest, "--adapter-mode", "plan_only")
    assert second.returncode == 1
    assert "pass --fresh" in second.stdout

    third = run_local(workspace, manifest, "--adapter-mode", "plan_only", "--fresh")
    assert third.returncode == 0

    bad_manifest = tmp_path / "bad.jsonl"
    write_jsonl(bad_manifest, [{"schema_version": "0.5.2"}])
    bad = run_local(tmp_path / "bad-workspace", bad_manifest, "--adapter-mode", "plan_only", "--fresh")
    assert bad.returncode == 1
    assert "missing required fields" in bad.stdout


def test_subprocess_probe_nonzero_timeout_and_model_root_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(manifest, manifest_rows(tmp_path))
    model_root = tmp_path / "model-root"
    model_root.mkdir()
    env_file = tmp_path / ".env.v05_local_models"
    env_file.write_text(f"MEDIA_ARCHIVE_MODEL_ROOT={model_root}\n", encoding="utf-8")

    source_inside_model = tmp_path / "bad_model_manifest.jsonl"
    bad_row = manifest_rows(tmp_path)[0]
    bad_model_file = model_root / "bad.jpg"
    bad_model_file.write_text("bad", encoding="utf-8")
    bad_row["source_path"] = str(bad_model_file)
    write_jsonl(source_inside_model, [bad_row])
    bad = run_local(workspace, source_inside_model, "--adapter-mode", "plan_only", "--env-file", str(env_file), "--fresh")
    assert bad.returncode == 1
    assert "model directory" in bad.stdout

    nonzero_probe = tmp_path / "nonzero.py"
    stub_script(nonzero_probe, "{}", exit_code=2)
    config = tmp_path / "adapters.json"
    full_adapter_config(config, nonzero_probe, model_root)
    result = run_local(workspace, manifest, "--adapter-mode", "real_local", "--adapter-config", str(config), "--fresh")
    assert result.returncode == 0
    summary = read_json(workspace / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json")
    assert "adapter_probe_failed" in {row["error_code"] for row in summary["adapter_statuses"] if row["status"] == "blocked"}

    timeout_probe = tmp_path / "timeout.py"
    timeout_probe.write_text("import time\ntime.sleep(3)\nprint('{}')\n", encoding="utf-8")
    timeout_adapter = {
        "adapter_name": "qwen_vl_caption",
        "adapter_mode": "real_local",
        "model_name": "Qwen-VL",
        "model_version": "local",
        "model_path": str(model_root),
        "weights_path_or_id": str(model_root),
        "runtime": "python3",
        "probe_command": ["python3", str(timeout_probe)],
        "probe_timeout_seconds": 1,
    }
    assert probe_adapter(timeout_adapter)["error_code"] == "probe_timeout"


def test_ocr_three_part_env_config_is_canonical(tmp_path):
    ocr_root = tmp_path / "ocr"
    det = ocr_root / "det"
    rec = ocr_root / "rec"
    det.mkdir(parents=True)
    rec.mkdir(parents=True)
    env_file = tmp_path / ".env.v05_local_models"
    env_file.write_text(
        "\n".join(
            [
                f"MEDIA_ARCHIVE_MODEL_ROOT={tmp_path / 'models'}",
                f"MEDIA_ARCHIVE_OCR_MODEL_PATH={ocr_root}",
                f"MEDIA_ARCHIVE_OCR_DET_MODEL_PATH={det}",
                f"MEDIA_ARCHIVE_OCR_REC_MODEL_PATH={rec}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_local_adapter_config(tmp_path / "workspace", env_file=env_file)
    ocr = config["adapters"]["ocr_detector"]
    assert ocr["model_path"] == str(ocr_root)
    assert ocr["extra_paths"]["MEDIA_ARCHIVE_OCR_DET_MODEL_PATH"] == str(det)
    assert ocr["extra_paths"]["MEDIA_ARCHIVE_OCR_REC_MODEL_PATH"] == str(rec)


def test_probe_command_env_is_loaded_and_missing_command_is_specific(tmp_path):
    model_root = tmp_path / "model"
    model_root.mkdir()
    probe = tmp_path / "probe.py"
    stub_script(probe, json.dumps({"status": "success"}))
    env_file = tmp_path / ".env.v05_local_models"
    env_file.write_text(
        "\n".join(
            [
                f"MEDIA_ARCHIVE_YOLOE_MODEL_PATH={model_root}",
                f"V05_YOLOE_PROBE_COMMAND=python3 {probe}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_local_adapter_config(tmp_path / "workspace", env_file=env_file)
    yoloe = config["adapters"]["yoloe_object_detector"]
    assert yoloe["probe_command"] == ["python3", str(probe)]
    assert probe_adapter(yoloe)["status"] == "success"

    missing_command = dict(yoloe)
    missing_command["probe_command"] = None
    blocked = probe_adapter(missing_command)
    assert blocked["error_code"] == "probe_command_missing"
    assert blocked["probe_command_or_null"] is None

    alias_env_file = tmp_path / ".env.alias"
    alias_env_file.write_text(
        "\n".join(
            [
                f"MEDIA_ARCHIVE_YOLOE_MODEL_PATH={model_root}",
                f"MEDIA_ARCHIVE_YOLOE_PROBE_COMMAND=python3 {probe}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    alias_config = load_local_adapter_config(tmp_path / "workspace", env_file=alias_env_file)
    assert alias_config["adapters"]["yoloe_object_detector"]["probe_command"] == ["python3", str(probe)]


def test_default_local_probe_command_checks_paths(tmp_path):
    model_root = tmp_path / "model"
    model_root.mkdir()
    env_file = tmp_path / ".env.v05_local_models"
    env_file.write_text(f"MEDIA_ARCHIVE_YOLOE_MODEL_PATH={model_root}\n", encoding="utf-8")

    config = load_local_adapter_config(tmp_path / "workspace", env_file=env_file)
    yoloe = config["adapters"]["yoloe_object_detector"]
    assert yoloe["probe_command"]
    assert yoloe["probe_command"][1].endswith("local_probe.py")
    result = probe_adapter(yoloe)
    assert result["status"] == "success"
    assert result["probe_payload"]["model_loaded"] is False


def test_probe_command_not_executable_and_nonzero_returncode(tmp_path):
    model_root = tmp_path / "model"
    model_root.mkdir()
    adapter = {
        "adapter_name": "yoloe_object_detector",
        "adapter_mode": "real_local",
        "model_name": "YOLOE-26L",
        "model_version": "local",
        "model_path": str(model_root),
        "weights_path_or_id": str(model_root),
        "runtime": None,
        "probe_command": [str(tmp_path / "missing-probe")],
        "probe_timeout_seconds": 5,
    }
    missing = probe_adapter(adapter)
    assert missing["error_code"] == "probe_command_not_executable"

    nonzero_probe = tmp_path / "nonzero.py"
    nonzero_probe.write_text("import sys\nsys.stderr.write('bad probe')\nraise SystemExit(7)\n", encoding="utf-8")
    adapter["probe_command"] = ["python3", str(nonzero_probe)]
    failed = probe_adapter(adapter)
    assert failed["error_code"] == "adapter_probe_failed"
    assert failed["returncode"] == 7
    assert "bad probe" in failed["stderr_tail_or_null"]
