from __future__ import annotations

import json
from pathlib import Path

from media_archive import app, config


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def base_record(relative_path: str, media_type: str = "audio") -> dict[str, object]:
    return {
        "source_root": "/tmp/source",
        "source_relative_path": relative_path,
        "media_type": media_type,
        "image_preview_status": {"applicable": False, "preview_available": False},
        "video_frame_status": {"applicable": False, "frames_available": False},
        "duplicate_status": {"is_duplicate": False, "is_representative": True},
        "exception_status": {"has_exception": False, "error_codes": [], "blocking_error_codes": []},
        "pipeline_status": {
            "analysis_eligible": True,
            "next_stage_hint": "audio_future_stage" if media_type == "audio" else "other_future_stage",
            "high_cost_processing_policy": "process",
        },
    }


def zero_size_record() -> dict[str, object]:
    record = base_record("empty.mp4", "other")
    record["exception_status"] = {
        "has_exception": True,
        "error_codes": ["zero_size"],
        "blocking_error_codes": ["zero_size"],
    }
    record["pipeline_status"] = {
        "analysis_eligible": False,
        "next_stage_hint": "blocked_by_exception",
        "high_cost_processing_policy": "skip_blocked",
    }
    return record


def create_combo_workspace(workspace: Path, records: list[dict[str, object]]) -> None:
    for relative_path in config.COMBO_REQUIRED_INPUTS:
        if relative_path == "unified/unified_manifest_summary.json":
            write_json(
                workspace / relative_path,
                {
                    "orphan_artifact_count": 0,
                    "missing_input_manifest_count": 0,
                    "unified_record_count": len(records),
                    "total_scan_records": len(records),
                },
            )
        elif relative_path in {"manifests/media_manifest.jsonl", "unified/unified_media_manifest.jsonl"}:
            write_jsonl(workspace / relative_path, records)
        else:
            write_jsonl(workspace / relative_path, [])


def check_by_id(report: dict[str, object], check_id: str) -> dict[str, object]:
    return next(check for check in report["checks"] if check["check_id"] == check_id)


def test_v02_combo_zero_size_present_requires_all_zero_size_blocked(tmp_path):
    workspace = tmp_path / "workspace"
    create_combo_workspace(workspace, [base_record("sound.wav"), zero_size_record()])

    assert app.main(["validate-v02-combo", "--workspace", str(workspace)]) == 0

    report = json.loads((workspace / "reports/v02_combo_validation_report.json").read_text(encoding="utf-8"))
    zero_check = check_by_id(report, "zero_size_blocked")
    assert report["validation_status"] == "PASS"
    assert report["exception_checks"]["zero_size_records_total"] == 1
    assert report["exception_checks"]["zero_size_blocked_count"] == 1
    assert zero_check["status"] == "PASS"
    assert zero_check["actual"] == {"handled_count": 1, "input_count": 1}
    assert check_by_id(report, "checks_aggregate_consistency")["status"] == "PASS"


def test_v02_combo_zero_size_absent_is_pass_with_zero_input_reason(tmp_path):
    workspace = tmp_path / "workspace"
    create_combo_workspace(workspace, [base_record("sound.wav")])

    assert app.main(["validate-v02-combo", "--workspace", str(workspace)]) == 0

    report = json.loads((workspace / "reports/v02_combo_validation_report.json").read_text(encoding="utf-8"))
    zero_check = check_by_id(report, "zero_size_blocked")
    assert report["validation_status"] == "PASS"
    assert report["failed_check_count"] == 0
    assert report["exception_checks"]["zero_size_records_total"] == 0
    assert report["exception_checks"]["zero_size_blocked_count"] == 0
    assert zero_check["status"] == "PASS"
    assert zero_check["actual"]["input_count"] == 0
    assert zero_check["actual"]["handled_count"] == 0
    assert zero_check["actual"]["reason"] == "zero input for this type in current scan"
    assert "zero input" in zero_check["message"]
    assert check_by_id(report, "checks_aggregate_consistency")["status"] == "PASS"
