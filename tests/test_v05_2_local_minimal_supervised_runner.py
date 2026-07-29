from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v05_2_local_minimal_supervised_runner.py"
SPEC = importlib.util.spec_from_file_location("v05_2_local_minimal_supervised_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


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


def make_source(source: Path) -> None:
    for relative, data in {
        "images/one.jpg": b"image",
        "videos/one.mov": b"video",
        "audio/one.wav": b"audio",
        "text/one.txt": b"text",
    }.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def args(source: Path, output: Path, env_file: Path) -> argparse.Namespace:
    return argparse.Namespace(source=source, workspace=output, env_file=env_file, profile="safe", timeout_seconds=30)


def fake_success_runner(command: list[str], log_path: Path, timeout_seconds: int) -> int:
    workspace = Path(command[command.index("--workspace") + 1])
    run_id = "run_test"
    write_jsonl(workspace / "stages/v0.1/manifests/V0.1_SCAN_MANIFEST.jsonl", [{"ok": True}])
    write_jsonl(workspace / "v02/unified/unified_media_manifest.jsonl", [{"ok": True}])
    write_json(workspace / "stages/v0.3/reports/v03_e2e_validation.json", {"validation_status": "PASS"})
    write_json(workspace / "stages/v0.4/reports/v04_contract_compat_report.json", {"validation_status": "PASS"})
    write_jsonl(
        workspace / "stages/v0.5/manifests/analysis_tasks.jsonl",
        [
            {
                "task_id": "task-1",
                "route_hint": "visual",
                "adapter_targets": ["yoloe_object_detector"],
                "model_input_path": "mini.jpg",
            }
        ],
    )
    write_jsonl(
        workspace / "stages/v0.5/manifests/analysis_outputs.jsonl",
        [
            {"run_id": None, "status": "success", "adapter_name": "old_fake"},
            {"run_id": run_id, "status": "success", "adapter_name": "yoloe_object_detector"},
        ],
    )
    write_json(
        workspace / "stages/v0.5/reports/v05_2_controlled_analysis_summary.json",
        {
            "schema_version": "0.5.2",
            "stage": "V0.5-2",
            "validation_status": "PASS",
            "run_id": run_id,
            "candidate_task_count": 1,
            "selected_adapter_target_count": 1,
            "current_run_counts": {"success": 1, "blocked": 0, "failed": 0, "skipped": 0},
            "real_adapter_invoked_or_probed": True,
            "source_read_only": True,
            "search_index_built": False,
            "adapter_statuses": [
                {
                    "adapter_name": "yoloe_object_detector",
                    "status": "success",
                    "error_code": None,
                    "probe_command_or_null": ["python3", "-m", "apps.media_archive.v05.local_probe"],
                    "returncode": 0,
                    "stdout_tail_or_null": "{}",
                    "stderr_tail_or_null": None,
                    "minimal_fix": None,
                }
            ],
        },
    )
    write_json(workspace / "stages/v0.5/state/analysis_run_state.json", {"status": "completed"})
    write_json(workspace / "stages/v0.5/telemetry/resource_report.json", {"resource_status": "unknown"})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("fake runner log\n", encoding="utf-8")
    return 0


def test_runner_creates_mini_source_manifest_and_preserves_source(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    make_source(source)
    before = {path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns) for path in source.rglob("*") if path.is_file()}

    rows = runner.create_mini_source(source, output)

    after = {path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns) for path in source.rglob("*") if path.is_file()}
    assert before == after
    linked = [row for row in rows if row.get("mini_source_path")]
    assert len(linked) == 4
    assert all(Path(row["mini_source_path"]).is_file() for row in linked)
    assert all(not Path(row["mini_source_path"]).is_symlink() for row in linked)
    assert {row["mini_source_materialization"] for row in linked} <= {"hardlink", "copy2", "existing_regular_file"}
    manifest = read_jsonl(output / "reports/mini_source_manifest.jsonl")
    assert {row["media_type"] for row in manifest if row.get("mini_source_path")} == {"image", "video", "audio", "text"}


def test_runner_writes_final_reports_and_does_not_delete_existing_workspace(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    env_file = tmp_path / ".env.v05_local_models"
    make_source(source)
    env_file.write_text("MEDIA_ARCHIVE_MODEL_ROOT=/tmp/model\n", encoding="utf-8")
    marker = output / "workspace/old_marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep", encoding="utf-8")

    verdict = runner.run(args(source, output, env_file), command_runner=fake_success_runner)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert verdict["validation_status"] == "PASS"
    assert verdict["stage"] == "V0.5-2"
    assert verdict["entered_v05_3"] is False
    assert verdict["mini_source_count"] == 4
    assert verdict["candidate_task_count"] == 1
    assert verdict["selected_adapter_target_count"] == 1
    assert verdict["current_run_counts"]["success"] == 1
    assert verdict["real_adapter_invoked_or_probed"] is True
    assert (output / "reports/stage_trace.jsonl").exists()
    assert (output / "reports/adapter_probe_summary.json").exists()
    assert (output / "reports/final_verdict.json").exists()
    assert (output / "reports/final_verdict.md").exists()
    saved = read_json(output / "reports/final_verdict.json")
    assert saved["validation_status"] == "PASS"


def test_runner_blocks_when_no_sample_is_available(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    env_file = tmp_path / ".env.v05_local_models"
    source.mkdir()
    env_file.write_text("MEDIA_ARCHIVE_MODEL_ROOT=/tmp/model\n", encoding="utf-8")

    verdict = runner.run(args(source, output, env_file), command_runner=fake_success_runner)

    assert verdict["validation_status"] == "BLOCKED"
    assert verdict["blocked_reason_code"] == "sample_not_found"
    assert read_json(output / "reports/final_verdict.json")["blocked_reason_code"] == "sample_not_found"


def test_parallel_overlap_is_computed_from_task_windows():
    rows = [
        {"started_monotonic": 1.0, "ended_monotonic": 5.0},
        {"started_monotonic": 2.0, "ended_monotonic": 4.0},
        {"started_monotonic": 3.0, "ended_monotonic": 6.0},
        {"started_monotonic": 7.0, "ended_monotonic": 8.0},
    ]

    assert runner.max_observed_overlap(rows) == 3
