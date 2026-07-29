#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
RULE_PATH = PROJECT_ROOT / "docs/pipeline_rules/STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md"
SCRIPT_PATH = PROJECT_ROOT / "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py"
CONFIG_PATH = PROJECT_ROOT / "configs/stop03_2_high_value_policy_v25.json"
TEST_PATH = PROJECT_ROOT / "tests/test_stop03_2_v25_candidate_queues.py"
MIGRATION_PATH = PROJECT_ROOT / "migrations/20260711_stop03_2_candidate_queue_v25.sql"
DEFAULT_BASELINE = TEST_OUTPUT_ROOT / "stop03_2_v24_dry_run_20260711_011828/manifests/qwenvl_high_value_candidate_queue.jsonl"
POLICY_VERSION = "stop03_2_generic_high_value_policy_v25"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def assert_output(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    resolved.relative_to(TEST_OUTPUT_ROOT.resolve())
    if resolved == TEST_OUTPUT_ROOT.resolve() or resolved.exists():
        raise RuntimeError(f"finalizer_output_must_be_new_subdirectory:{resolved}")
    return resolved


def offline_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "ULTRALYTICS_OFFLINE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/stop03_2_v25_pycache",
    })
    return env


def run_command(command: Sequence[str], log_path: Path, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command), cwd=str(cwd), env=offline_env(), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"command_failed:{result.returncode}:{' '.join(command)}:{log_path}")
    return result


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"jsonl_invalid:{path}:{line_number}:{exc}") from exc
    return rows


def candidate_command(
    python: Path, mode: str, db: Path, out: Path, baseline: Path | None,
    *, clear: bool = False, preflight: bool = False,
) -> list[str]:
    command = [
        str(python), str(SCRIPT_PATH), "--db", str(db), "--out", str(out),
        "--config", str(CONFIG_PATH),
    ]
    if preflight:
        command.append("--preflight-only")
    else:
        command.extend(["--mode", mode])
    if baseline is not None:
        command.extend(["--video-regression-baseline", str(baseline)])
    if clear:
        command.append("--clear-existing-candidate-items")
    return command


def require_equal(summary: Mapping[str, Any], expected: Mapping[str, Any], stage: str) -> None:
    mismatches = {
        key: {"expected": value, "actual": summary.get(key)}
        for key, value in expected.items() if summary.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"{stage}_regression_mismatch:" + json.dumps(mismatches, ensure_ascii=False, sort_keys=True))


def validate_current_regression(summary: Mapping[str, Any], policy_status: str, stage: str) -> None:
    require_equal(summary, {
        "technical_status": "PASS", "dry_run_status": "PASS",
        "commit_status": "DO_NOT_COMMIT", "policy_status": policy_status,
        "coverage_missing_count": 0, "coverage_refill_failed_count": 0,
        "tail_anchor_remap_failed_count": 0, "non_short_video_tail_fallback_count": 0,
        "normal_video_group_count": 91, "screen_recording_video_group_count": 6,
        "screen_recording_qwenvl_leak_count": 0,
        "non_screen_video_regression_status": "PASS",
        "non_screen_video_added_count": 0, "non_screen_video_removed_count": 0,
        "excluded_rpreplay_source_count": 6,
        "finder_tag_input_row_count": 127, "finder_tag_unique_source_count": 127,
        "finder_tag_image_source_count": 125, "finder_tag_non_image_source_count": 2,
        "finder_tag_missing_source_count": 0, "xmp_sidecar_source_count": 0,
        "timelapse_input_row_count": 12, "timelapse_sequence_count": 4,
        "timelapse_select_one_sequence_count": 3,
        "timelapse_select_two_sequence_count": 0,
        "timelapse_select_three_sequence_count": 1,
        "timelapse_selected_canonical_count": 6,
        "final_unique_image_qwen_count": 131,
        "image_generic_visual_signal_candidate_count": 0,
        "central_duplicate_queue_leak_count": 0,
        "black_leak_qwen_count": 0, "black_leak_ocr_count": 0,
        "deterministic_recompute_match": True,
    }, stage)
    roles = summary.get("qwen_role_counts") or {}
    expected_roles = {
        "image_finder_tag_seed": 125,
        "image_xmp_sidecar_seed": 0,
        "image_timelapse_representative": 6,
    }
    role_mismatches = {
        key: {"expected": value, "actual": int(roles.get(key, 0))}
        for key, value in expected_roles.items() if int(roles.get(key, 0)) != value
    }
    if role_mismatches:
        raise RuntimeError(f"{stage}_image_role_regression_mismatch:" + json.dumps(role_mismatches, sort_keys=True))


def verify_manifest_hashes(output: Path) -> None:
    expected = {
        "policy_version": POLICY_VERSION,
        "script_sha256": sha256_file(SCRIPT_PATH),
        "config_sha256": sha256_file(CONFIG_PATH),
        "rule_document_sha256": sha256_file(RULE_PATH),
    }
    rows = load_jsonl(output / "manifests/all_candidate_queue.jsonl")
    for row in rows:
        for key, value in expected.items():
            if row.get(key) != value:
                raise RuntimeError(f"final_manifest_hash_mismatch:{row.get('candidate_id')}:{key}")


def freeze_policy_files() -> tuple[bytes, bytes]:
    original_rule = RULE_PATH.read_bytes()
    original_config = CONFIG_PATH.read_bytes()
    rule_text = original_rule.decode("utf-8")
    if rule_text.count("状态：`FROZEN_CANDIDATE`") != 1:
        raise RuntimeError("rule_frozen_candidate_marker_not_exactly_once")
    RULE_PATH.write_text(rule_text.replace("状态：`FROZEN_CANDIDATE`", "状态：`FROZEN`", 1), encoding="utf-8")
    config = json.loads(original_config)
    if config.get("policy_status") != "FROZEN_CANDIDATE":
        raise RuntimeError("config_not_frozen_candidate_before_finalize")
    config["policy_status"] = "FROZEN"
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return original_rule, original_config


def restore_policy_files(original_rule: bytes, original_config: bytes) -> None:
    RULE_PATH.write_bytes(original_rule)
    CONFIG_PATH.write_bytes(original_config)


def backup_database(db: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db))
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def restore_database(backup: Path, db: Path) -> None:
    source = sqlite3.connect(str(backup))
    target = sqlite3.connect(str(db))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


ALTER_RE = re.compile(
    r"^ALTER\s+TABLE\s+stop03_2_candidate_queue_items\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$",
    re.IGNORECASE,
)


def apply_migration_if_needed(db: Path) -> dict[str, Any]:
    statements = []
    for raw in MIGRATION_PATH.read_text(encoding="utf-8").split(";"):
        statement = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--")).strip()
        if not statement:
            continue
        match = ALTER_RE.match(statement)
        if not match:
            raise RuntimeError(f"unsupported_migration_statement:{statement}")
        statements.append((match.group(1), statement))
    con = sqlite3.connect(str(db))
    try:
        existing = {str(row[1]) for row in con.execute("PRAGMA table_info(stop03_2_candidate_queue_items)")}
        missing = [name for name, _ in statements if name not in existing]
        con.execute("BEGIN IMMEDIATE")
        for name, statement in statements:
            if name in missing:
                con.execute(statement)
        con.commit()
        after = {str(row[1]) for row in con.execute("PRAGMA table_info(stop03_2_candidate_queue_items)")}
        unresolved = sorted(set(missing) - after)
        if unresolved:
            raise RuntimeError("migration_columns_still_missing:" + ",".join(unresolved))
        integrity = [str(row[0]) for row in con.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"migration_integrity_failed:{integrity}")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"migration_applied": bool(missing), "migration_added_columns": missing}


READBACK_FIELDS = (
    "candidate_id", "queue_type", "candidate_role", "source_content_id",
    "visual_unit_id", "canonical_visual_unit_id", "time_position_ms",
    "policy_version", "script_sha256", "config_sha256", "rule_document_sha256",
)


def normalized(value: Any) -> Any:
    return "" if value is None else value


def readback_audit(db: Path, manifest: Path) -> dict[str, Any]:
    manifest_rows = load_jsonl(manifest)
    expected = {
        str(row["candidate_id"]): tuple(normalized(row.get(field)) for field in READBACK_FIELDS)
        for row in manifest_rows
    }
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        db_rows = [dict(row) for row in con.execute(
            "SELECT " + ",".join(READBACK_FIELDS) + " FROM stop03_2_candidate_queue_items"
        )]
        actual = {
            str(row["candidate_id"]): tuple(normalized(row.get(field)) for field in READBACK_FIELDS)
            for row in db_rows
        }
        duplicate_ids = int(con.execute(
            "SELECT COUNT(*)-COUNT(DISTINCT candidate_id) FROM stop03_2_candidate_queue_items"
        ).fetchone()[0])
        canonical_queue_duplicates = int(con.execute(
            """SELECT COUNT(*) FROM (
                 SELECT canonical_visual_unit_id,queue_type,COUNT(*) AS n
                 FROM stop03_2_candidate_queue_items
                 GROUP BY canonical_visual_unit_id,queue_type HAVING n>1
               )"""
        ).fetchone()[0])
        integrity = [str(row[0]) for row in con.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    finally:
        con.close()
    status = (
        expected == actual and duplicate_ids == 0 and canonical_queue_duplicates == 0
        and integrity == ["ok"] and not foreign_keys
    )
    return {
        "db_readback_status": "PASS" if status else "FAIL",
        "candidate_rows_written": len(expected), "candidate_rows_readback": len(actual),
        "candidate_id_set_match": set(expected) == set(actual),
        "manifest_db_consistency": expected == actual,
        "duplicate_candidate_id_count": duplicate_ids,
        "canonical_queue_duplicate_count": canonical_queue_duplicates,
        "integrity_check": integrity, "foreign_key_check": foreign_keys,
    }


def semantic_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item["candidate_id"])):
        filtered = {key: value for key, value in row.items() if key != "run_id"}
        digest.update(json.dumps(filtered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def parse_test_count(output: str) -> int:
    matches = re.findall(r"(\d+) passed", output)
    return int(matches[-1]) if matches else 0


def final_usage_command(python: Path, db: Path) -> str:
    return (
        f"{python} {SCRIPT_PATH} --mode commit --db {db} "
        f"--out {TEST_OUTPUT_ROOT}/stop03_2_v25_production_<timestamp> "
        f"--config {CONFIG_PATH} --clear-existing-candidate-items"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize Stop03-2 V25")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--video-regression-baseline", default=str(DEFAULT_BASELINE))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db = Path(args.db).expanduser().resolve(strict=True)
    python = Path(args.python).expanduser()
    if not python.is_absolute():
        python = Path(os.path.abspath(str(python)))
    if not python.is_file():
        raise RuntimeError(f"python_interpreter_not_found:{python}")
    baseline = Path(args.video_regression_baseline).expanduser().resolve(strict=True)
    out = assert_output(Path(args.out) if args.out else TEST_OUTPUT_ROOT / f"stop03_2_v25_final_{now_stamp()}")
    required = [RULE_PATH, SCRIPT_PATH, CONFIG_PATH, TEST_PATH, MIGRATION_PATH, baseline]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print(json.dumps({"status": "FAIL", "reason": "required_files_missing", "missing": missing}, ensure_ascii=False))
        return 2
    config_before = load_json(CONFIG_PATH)
    rule_before_text = RULE_PATH.read_text(encoding="utf-8")
    if config_before.get("policy_status") != "FROZEN_CANDIDATE" or "状态：`FROZEN_CANDIDATE`" not in rule_before_text:
        print(json.dumps({"status": "FAIL", "reason": "v25_not_in_frozen_candidate_state"}, ensure_ascii=False))
        return 2
    out.mkdir(parents=True)
    (out / "preflight").mkdir()
    (out / "reports").mkdir()
    (out / "manifests").mkdir()
    stage = "A_initialize"
    original_rule = RULE_PATH.read_bytes()
    original_config = CONFIG_PATH.read_bytes()
    policy_frozen = False
    db_backup = out / "database_backup" / db.name
    db_touched = False
    summary: dict[str, Any] = {
        "status": "FAIL", "technical_status": "FAIL", "policy_status": "FROZEN_CANDIDATE",
        "commit_status": "DO_NOT_COMMIT", "db_readback_status": "NOT_RUN",
        "idempotency_status": "NOT_RUN", "final_output_directory": str(out),
        "created_files": [str(RULE_PATH), str(SCRIPT_PATH), str(CONFIG_PATH), str(TEST_PATH), str(Path(__file__).resolve()), str(MIGRATION_PATH)],
        "modified_existing_files": [],
    }
    try:
        stage = "A_py_compile"
        run_command([str(python), "-m", "py_compile", str(SCRIPT_PATH), str(Path(__file__).resolve())], out / "preflight/py_compile.log")
        stage = "A_targeted_tests"
        test_result = run_command(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", str(TEST_PATH)],
            out / "preflight/targeted_tests_candidate.log",
        )
        tests_passed = parse_test_count(test_result.stdout)
        if tests_passed <= 0:
            raise RuntimeError("targeted_test_pass_count_unavailable")
        summary.update({"tests_passed": tests_passed, "tests_failed": 0})
        stage = "A_preflight"
        preflight_result = run_command(
            candidate_command(python, "dry-run", db, out / "candidate_dry_run", baseline, preflight=True),
            out / "preflight/candidate_preflight.log",
        )
        preflight = json.loads(preflight_result.stdout)
        write_json(out / "preflight/candidate_preflight.json", preflight)
        if preflight.get("technical_status") != "PASS":
            raise RuntimeError("candidate_preflight_failed")
        summary["preflight_status"] = "PASS"
        db_sha_before = sha256_file(db)
        db_mtime_before = db.stat().st_mtime_ns
        stage = "A_candidate_dry_run"
        run_command(
            candidate_command(python, "dry-run", db, out / "candidate_dry_run", baseline),
            out / "preflight/candidate_dry_run.log",
        )
        candidate_summary = load_json(out / "candidate_dry_run/reports/stop03_2_candidate_summary.json")
        validate_current_regression(candidate_summary, "FROZEN_CANDIDATE", "candidate_dry_run")
        if sha256_file(db) != db_sha_before or db.stat().st_mtime_ns != db_mtime_before:
            raise RuntimeError("candidate_dry_run_modified_database")
        summary["candidate_dry_run_status"] = "PASS"
        summary["candidate_dry_run_output"] = str(out / "candidate_dry_run")

        stage = "B_freeze_files"
        freeze_backup = out / "freeze_file_backup"
        freeze_backup.mkdir()
        (freeze_backup / RULE_PATH.name).write_bytes(original_rule)
        (freeze_backup / CONFIG_PATH.name).write_bytes(original_config)
        policy_frozen = True
        freeze_policy_files()
        final_rule_sha = sha256_file(RULE_PATH)
        final_config_sha = sha256_file(CONFIG_PATH)
        final_script_sha = sha256_file(SCRIPT_PATH)
        stage = "B_targeted_tests"
        frozen_tests = run_command(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", str(TEST_PATH)],
            out / "preflight/targeted_tests_frozen.log",
        )
        if parse_test_count(frozen_tests.stdout) != tests_passed:
            raise RuntimeError("frozen_targeted_test_count_changed")
        stage = "B_preflight"
        frozen_preflight_result = run_command(
            candidate_command(python, "dry-run", db, out / "frozen_dry_run", baseline, preflight=True),
            out / "preflight/frozen_preflight.log",
        )
        frozen_preflight = json.loads(frozen_preflight_result.stdout)
        write_json(out / "preflight/frozen_preflight.json", frozen_preflight)
        if frozen_preflight.get("technical_status") != "PASS" or frozen_preflight.get("policy_status") != "FROZEN":
            raise RuntimeError("frozen_preflight_failed")
        frozen_db_sha_before = sha256_file(db)
        frozen_db_mtime_before = db.stat().st_mtime_ns
        stage = "B_frozen_dry_run"
        run_command(
            candidate_command(python, "dry-run", db, out / "frozen_dry_run", baseline),
            out / "preflight/frozen_dry_run.log",
        )
        frozen_summary = load_json(out / "frozen_dry_run/reports/stop03_2_candidate_summary.json")
        validate_current_regression(frozen_summary, "FROZEN", "frozen_dry_run")
        verify_manifest_hashes(out / "frozen_dry_run")
        if sha256_file(db) != frozen_db_sha_before or db.stat().st_mtime_ns != frozen_db_mtime_before:
            raise RuntimeError("frozen_dry_run_modified_database")
        summary.update({
            "frozen_dry_run_status": "PASS", "frozen_dry_run_output": str(out / "frozen_dry_run"),
            "rule_document_sha256": final_rule_sha, "script_sha256": final_script_sha,
            "config_sha256": final_config_sha,
        })

        stage = "C_database_backup"
        backup_database(db, db_backup)
        summary["database_backup_path"] = str(db_backup)
        stage = "C_migration"
        db_touched = True
        migration = apply_migration_if_needed(db)
        summary.update(migration)
        stage = "C_commit_run_1"
        db_touched = True
        run_command(
            candidate_command(python, "commit", db, out / "commit_run_1", baseline, clear=True),
            out / "preflight/commit_run_1.log",
        )
        commit1_summary = load_json(out / "commit_run_1/reports/stop03_2_candidate_summary.json")
        if commit1_summary.get("commit_status") != "COMMITTED":
            raise RuntimeError("commit_run_1_not_committed")
        readback1 = readback_audit(db, out / "commit_run_1/manifests/all_candidate_queue.jsonl")
        if readback1["db_readback_status"] != "PASS":
            raise RuntimeError("commit_run_1_readback_failed")
        rows1 = load_jsonl(out / "commit_run_1/manifests/all_candidate_queue.jsonl")
        ids1 = {str(row["candidate_id"]) for row in rows1}
        digest1 = semantic_digest(rows1)
        count1 = readback1["candidate_rows_readback"]

        stage = "D_commit_run_2"
        run_command(
            candidate_command(python, "commit", db, out / "commit_run_2", baseline, clear=True),
            out / "preflight/commit_run_2.log",
        )
        commit2_summary = load_json(out / "commit_run_2/reports/stop03_2_candidate_summary.json")
        if commit2_summary.get("commit_status") != "COMMITTED":
            raise RuntimeError("commit_run_2_not_committed")
        readback2 = readback_audit(db, out / "commit_run_2/manifests/all_candidate_queue.jsonl")
        if readback2["db_readback_status"] != "PASS":
            raise RuntimeError("commit_run_2_readback_failed")
        rows2 = load_jsonl(out / "commit_run_2/manifests/all_candidate_queue.jsonl")
        ids2 = {str(row["candidate_id"]) for row in rows2}
        digest2 = semantic_digest(rows2)
        idempotent = (
            ids1 == ids2 and digest1 == digest2
            and count1 == readback2["candidate_rows_readback"]
            and readback2["duplicate_candidate_id_count"] == 0
            and readback2["canonical_queue_duplicate_count"] == 0
        )
        if not idempotent:
            raise RuntimeError("idempotency_validation_failed")
        for path in (out / "commit_run_2/manifests").iterdir():
            if path.is_file():
                shutil.copy2(path, out / "manifests" / path.name)
        summary.update({
            **frozen_summary, **readback2,
            "status": "PASS", "technical_status": "PASS", "policy_status": "FROZEN",
            "commit_status": "COMMITTED", "db_readback_status": "PASS",
            "idempotency_status": "PASS", "candidate_dry_run_status": "PASS",
            "frozen_dry_run_status": "PASS", "preflight_status": "PASS",
            "commit_run_1_output": str(out / "commit_run_1"),
            "commit_run_2_output": str(out / "commit_run_2"),
            "candidate_id_set_match": ids1 == ids2,
            "semantic_digest_match": digest1 == digest2,
            "final_usage_command": final_usage_command(python, db),
            "database_backup_path": str(db_backup),
            "final_output_directory": str(out),
        })
        write_json(out / "reports/stop03_2_v25_finalization_summary.json", summary)
        md_keys = (
            "status", "technical_status", "policy_status", "commit_status",
            "db_readback_status", "idempotency_status", "candidate_rows_written",
            "candidate_rows_readback", "duplicate_candidate_id_count", "integrity_check",
        )
        (out / "reports/stop03_2_v25_finalization_summary.md").write_text(
            "\n".join(f"- **{key}**: `{summary.get(key)}`" for key in md_keys) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        if db_touched and db_backup.is_file():
            restore_database(db_backup, db)
        if policy_frozen:
            restore_policy_files(original_rule, original_config)
        summary.update({
            "status": "FAIL", "technical_status": "FAIL", "policy_status": "FROZEN_CANDIDATE",
            "commit_status": "ROLLED_BACK" if db_touched else "DO_NOT_COMMIT",
            "db_readback_status": "FAIL" if db_touched else "NOT_RUN",
            "idempotency_status": "FAIL" if stage.startswith("D_") else "NOT_RUN",
            "failure_stage": stage, "failure_type": type(exc).__name__, "failure_reason": str(exc),
        })
        write_json(out / "reports/stop03_2_v25_finalization_summary.json", summary)
        (out / "reports/stop03_2_v25_finalization_summary.md").write_text(
            f"- **status**: `FAIL`\n- **failure_stage**: `{stage}`\n- **failure_reason**: `{exc}`\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
