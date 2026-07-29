#!/usr/bin/env python3
"""Run an enrichment stage only when its frozen candidate queue has work."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Sequence


SCRIPT_VERSION = "run_optional_enrichment_stage_v1"
WORKLOAD_SQL = {
    "qwen": "SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue",
    "ocr": "SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue",
    "evidence": (
        "SELECT (SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue) + "
        "(SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue)"
    ),
    "propagation": "SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue",
    "embedding": (
        "SELECT (SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue) + "
        "(SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue)"
    ),
}


def resolve_inside(path: Path, root: Path, *, strict: bool) -> Path:
    resolved = path.expanduser().resolve(strict=strict)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"path_outside_selected_workspace:{resolved}") from exc
    return resolved


def workload_count(db: Path, stage_kind: str) -> int:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        return int(con.execute(WORKLOAD_SQL[stage_kind]).fetchone()[0])
    finally:
        con.close()


def write_no_work_report(
    out: Path,
    stage_kind: str,
    count: int,
    *,
    contract_materialized: bool = False,
) -> dict[str, Any]:
    report = {
        "status": "PASS",
        "technical_status": "PASS",
        "execution_status": (
            "NO_WORK_CONTRACT_MATERIALIZED"
            if contract_materialized
            else "NO_WORK_NOT_APPLICABLE"
        ),
        "reason_code": "FROZEN_CANDIDATE_QUEUE_EMPTY",
        "stage_kind": stage_kind,
        "workload_count": count,
        "model_run": False,
        "database_write": contract_materialized,
        "empty_contract_materialized": contract_materialized,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "script_version": SCRIPT_VERSION,
    }
    reports = out / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "no_work_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Conditional optional enrichment stage")
    parser.add_argument("--stage-kind", choices=tuple(WORKLOAD_SQL), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument(
        "--run-delegate-on-empty",
        action="store_true",
        help=(
            "Run the delegate for an empty queue so it can materialize its "
            "zero-candidate database contract without running inference."
        ),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    allowed = args.allowed_output_root.expanduser().resolve(strict=True)
    db = resolve_inside(args.db, allowed, strict=True)
    out = resolve_inside(args.out, allowed, strict=False)
    if out == allowed:
        raise RuntimeError("optional_stage_output_must_be_child_directory")
    count = workload_count(db, args.stage_kind)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if count == 0:
        contract_materialized = False
        if args.run_delegate_on_empty:
            if not command:
                raise RuntimeError("optional_stage_command_missing")
            delegate_code = int(subprocess.run(command, check=False).returncode)
            if delegate_code:
                return delegate_code
            contract_materialized = True
        report = write_no_work_report(
            out,
            args.stage_kind,
            count,
            contract_materialized=contract_materialized,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    if not command:
        raise RuntimeError("optional_stage_command_missing")
    print(json.dumps({
        "status": "RUNNING", "stage_kind": args.stage_kind,
        "workload_count": count, "script_version": SCRIPT_VERSION,
    }, ensure_ascii=False), flush=True)
    return int(subprocess.run(command, check=False).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
