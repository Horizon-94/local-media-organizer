from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v05_2_local_minimal_supervised_runner.py"
SPEC = importlib.util.spec_from_file_location("v05_2_local_minimal_supervised_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_background_supervised_args_and_trace_files(tmp_path: Path) -> None:
    log_path = tmp_path / "logs/run.log"
    pid_path = tmp_path / "logs/run.pid"
    output = tmp_path / "v05-2A-bg-canonical-oneshot-test"
    parsed = runner.build_parser().parse_args(
        [
            "--source",
            str(tmp_path / "source"),
            "--workspace",
            str(output),
            "--env-file",
            str(tmp_path / ".env"),
            "--execution-mode",
            "background_supervised",
            "--log-file",
            str(log_path),
            "--pid-file",
            str(pid_path),
        ]
    )

    assert parsed.execution_mode == "background_supervised"
    assert parsed.log_file == log_path
    assert parsed.pid_file == pid_path
    assert runner._is_parallel_evidence_workspace(output)
    assert runner._is_v052a_workspace(output)
    assert runner._is_v052a_supervised_workspace(output)
    assert runner._is_v052a_core_dedup_workspace(output)

    trace = runner.ExecutionTrace(output, output.name, parsed.execution_mode, log_path=log_path, pid_path=pid_path)
    trace.emit("run_start", "preflight", "started")
    summary = trace.write_reports(0, final_reports_created=True)

    assert pid_path.exists()
    assert pid_path.read_text(encoding="utf-8").strip().isdigit()
    log_text = log_path.read_text(encoding="utf-8")
    assert "RUN_START" in log_text
    assert "HEARTBEAT" in log_text
    rows = read_jsonl(output / "reports/run_execution_trace.jsonl")
    assert rows[0]["event_type"] == "run_start"
    assert summary["execution_mode"] == "background_supervised"
    assert summary["pid_file_created"] is True
    assert summary["log_file_created"] is True
    assert summary["trace_file_created"] is True
