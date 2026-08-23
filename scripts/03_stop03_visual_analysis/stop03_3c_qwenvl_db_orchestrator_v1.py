#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3C Qwen-VL orchestrator backed by the central V25 DB contract.

Production modes read only v_stop03_2_v25_qwenvl_execution_queue. Preflight
and dry-run may use an explicit in-memory migration simulation before commit;
smoke/run/resume always require the committed central view.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import qwenvl_output_contract_v2 as output_contract
import stop03_2_v25_candidate_contract_lock as contract_lock


SCRIPT_VERSION = "stop03_3c_qwenvl_db_orchestrator_v1_20260711"
PROJECT_ROOT = Path("$APP_RESOURCES/Pipeline")
TEST_OUTPUT_ROOT = Path("$USER_HOME/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-3c-qwenvl-db-v1-dry-run"
QWEN_VIEW = "v_stop03_2_v25_qwenvl_execution_queue"
REQUIRED_IDS = (
    "candidate_id", "source_content_id", "visual_unit_id",
    "canonical_visual_unit_id", "derived_id",
)
SUCCESS_STATUS = "success"
NON_SUCCESS_STATUSES = {
    "truncated", "parse_failed", "missing_required_fields",
    "input_fingerprint_mismatch", "failed",
}
RESUME_STATUSES = {
    "pending", "running", "failed", "review", "truncated", "parse_failed",
    "missing_required_fields", "input_fingerprint_mismatch",
}

_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_SHUTDOWN_REQUESTED = threading.Event()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_offline_environment(config: Mapping[str, Any]) -> None:
    for key, value in dict(config.get("offline_environment") or {}).items():
        os.environ.setdefault(str(key), str(value))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return prefix + sha256_text("|".join(str(part) for part in parts))[:28]


def load_config(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("output_contract_version") != output_contract.CONTRACT_VERSION:
        raise RuntimeError("config_output_contract_version_mismatch")
    if int(value.get("default_max_tokens", 0)) != output_contract.RECOMMENDED_MAX_TOKENS:
        raise RuntimeError("config_default_max_tokens_mismatch")
    return value


def assert_output_path(path: Path, *, may_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    root = TEST_OUTPUT_ROOT.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    if resolved == root:
        raise RuntimeError("output_must_not_equal_test_output_root")
    if not may_exist and resolved.exists() and any(resolved.iterdir()):
        raise RuntimeError(f"output_not_empty:{resolved}")
    return resolved


def object_exists(con: sqlite3.Connection, name: str, kind: Optional[str] = None) -> bool:
    if kind:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
        ).fetchone()
    else:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
    return row is not None


def readonly_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def db_state(db: Path) -> Dict[str, Any]:
    stat = db.stat()
    con = readonly_connection(db)
    try:
        return {
            "sha256": sha256_file(db),
            "mtime_ns": stat.st_mtime_ns,
            "candidate_queue_items": int(
                con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_items").fetchone()[0]
            ),
            "model_runs": int(con.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]),
            "qwenvl_runs": int(
                con.execute("SELECT COUNT(*) FROM stop03_3_qwenvl_runs").fetchone()[0]
            ) if object_exists(con, "stop03_3_qwenvl_runs", "table") else 0,
        }
    finally:
        con.close()


def install_snapshot_in_memory(db: Path) -> sqlite3.Connection:
    """Build the uncommitted migration only inside an ephemeral SQLite DB."""
    source = readonly_connection(db)
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        source.backup(memory)
    finally:
        source.close()
    snapshot = contract_lock.build_snapshot(db)
    if snapshot["summary"]["technical_status"] != "PASS":
        memory.close()
        raise RuntimeError("v25_snapshot_simulation_build_failed")
    memory.execute("PRAGMA foreign_keys=ON")
    memory.executescript(
        "BEGIN IMMEDIATE;\n" + contract_lock.MIGRATION.read_text(encoding="utf-8")
    )
    placeholders = ",".join("?" for _ in contract_lock.SNAPSHOT_COLUMNS)
    memory.executemany(
        f"INSERT INTO stop03_2_candidate_queue_frozen_v25 ({','.join(contract_lock.SNAPSHOT_COLUMNS)}) VALUES ({placeholders})",
        [
            [row.get(field) for field in contract_lock.SNAPSHOT_COLUMNS]
            for row in snapshot["rows"]
        ],
    )
    contract = contract_lock.contract_record(snapshot["summary"], now_iso())
    fields = tuple(contract)
    memory.execute(
        f"INSERT INTO pipeline_frozen_contracts ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        [contract[field] for field in fields],
    )
    memory.commit()
    return memory


def queue_connection(
    db: Path, *, allow_simulation: bool
) -> Tuple[sqlite3.Connection, str]:
    central = readonly_connection(db)
    if object_exists(central, QWEN_VIEW, "view"):
        return central, "central_db_view"
    central.close()
    if not allow_simulation:
        raise RuntimeError("v25_qwenvl_view_missing_migration_commit_required")
    return install_snapshot_in_memory(db), "in_memory_uncommitted_migration_simulation"


def load_queue(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not object_exists(con, QWEN_VIEW, "view"):
        raise RuntimeError("qwenvl_execution_view_missing")
    return [
        dict(row) for row in con.execute(
            f"SELECT * FROM {QWEN_VIEW} ORDER BY candidate_id"
        )
    ]


def contract_metadata(con: sqlite3.Connection) -> Dict[str, Any]:
    row = con.execute(
        "SELECT * FROM pipeline_frozen_contracts WHERE contract_name=?",
        (contract_lock.CONTRACT_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("v25_frozen_contract_missing")
    return dict(row)


def model_weight_path(model_path: Path, config: Mapping[str, Any]) -> Path:
    configured = Path(str(config.get("model_weight_file") or ""))
    if model_path == Path(str(config.get("model_path") or "")) and configured.is_file():
        return configured.resolve(strict=True)
    direct = model_path / "model.safetensors" if model_path.is_dir() else model_path
    return direct.resolve(strict=True)


def model_fingerprint(model_path: Path, config: Mapping[str, Any]) -> Dict[str, Any]:
    """Build an offline, content-addressed fingerprint of the registered model."""
    root = model_path.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError(f"model_directory_required:{root}")
    weight = model_weight_path(root, config)
    config_file = root / str(config.get("model_config_file") or "config.json")
    tokenizer_names = [str(item) for item in config.get("model_tokenizer_files") or ()]
    required = [weight, config_file, *(root / name for name in tokenizer_names)]
    missing = [str(path) for path in required if not path.is_file()]
    digest_cache: Dict[Path, str] = {}

    def digest(path: Path) -> str:
        resolved = path.resolve(strict=True)
        if resolved not in digest_cache:
            digest_cache[resolved] = sha256_file(resolved)
        return digest_cache[resolved]

    weight_sha = digest(weight) if weight.is_file() else "MISSING"
    config_sha = digest(config_file) if config_file.is_file() else "MISSING"
    tokenizer = {
        name: digest(root / name) if (root / name).is_file() else "MISSING"
        for name in tokenizer_names
    }
    tokenizer_json = json.dumps(tokenizer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    inventory: List[Dict[str, Any]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        stat = path.stat()
        inventory.append({
            "relative_path": path.relative_to(root).as_posix(),
            "size_bytes": stat.st_size,
            "sha256": digest(path),
        })
    inventory_json = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tokenizer_sha = sha256_text(tokenizer_json)
    inventory_sha = sha256_text(inventory_json)
    aggregate = {
        "model_weight_sha256": weight_sha,
        "model_config_sha256": config_sha,
        "model_tokenizer_files_sha256": tokenizer_sha,
        "model_inventory_sha256": inventory_sha,
    }
    return {
        **aggregate,
        "model_fingerprint_sha256": sha256_text(
            json.dumps(aggregate, sort_keys=True, separators=(",", ":"))
        ),
        "model_weight_path": str(weight),
        "model_config_path": str(config_file),
        "model_tokenizer_files_json": tokenizer_json,
        "model_inventory_json": inventory_json,
        "model_inventory_file_count": len(inventory),
        "missing_model_fingerprint_files": missing,
        "download_used": False,
    }


def validate_generation_settings(
    mode: str, max_tokens: int, allow_low_token_debug: bool
) -> None:
    if mode in {"run", "resume"} and max_tokens < output_contract.RECOMMENDED_MAX_TOKENS:
        raise RuntimeError("production_max_tokens_below_384")
    if mode == "smoke" and max_tokens < output_contract.RECOMMENDED_MAX_TOKENS and not allow_low_token_debug:
        raise RuntimeError("low_token_smoke_requires_allow_low_token_debug")


def execution_key(
    row: Mapping[str, Any], model_sha256: str, prompt_sha256: str,
    contract_version: str, max_tokens: int,
) -> str:
    payload = "|".join(
        (
            str(row.get("candidate_id") or ""),
            str(row.get("runtime_visual_file_sha256") or ""),
            model_sha256,
            prompt_sha256,
            contract_version,
            str(max_tokens),
        )
    )
    return sha256_text(payload)


def validate_queue(
    rows: Sequence[Mapping[str, Any]], *, verify_runtime_sha: bool
) -> Dict[str, Any]:
    missing = {
        field: sum(not str(row.get(field) or "").strip() for row in rows)
        for field in REQUIRED_IDS
    }
    ids = [str(row.get("candidate_id") or "") for row in rows]
    missing_runtime = 0
    sha_mismatch = 0
    runtime_sha_ready = 0
    original_path_violation = 0
    for row in rows:
        path = Path(str(row.get("runtime_visual_file") or "")).expanduser()
        if not path.is_file():
            missing_runtime += 1
            continue
        try:
            path.resolve(strict=True).relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
        except ValueError:
            original_path_violation += 1
            continue
        expected = str(row.get("runtime_visual_file_sha256") or "")
        if expected:
            runtime_sha_ready += 1
        if verify_runtime_sha and sha256_file(path.resolve(strict=True)) != expected:
            sha_mismatch += 1
    checks = {
        "row_count_336": len(rows) == contract_lock.EXPECTED_QWEN,
        "candidate_ids_unique": len(ids) == len(set(ids)),
        "forced_ids_complete": all(value == 0 for value in missing.values()),
        "runtime_files_exist": missing_runtime == 0,
        "runtime_sha_complete": runtime_sha_ready == len(rows),
        "runtime_sha_matches": sha_mismatch == 0,
        "runtime_is_derived_only": original_path_violation == 0,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "missing_forced_ids": missing,
        "missing_runtime_file_count": missing_runtime,
        "runtime_sha_ready_count": runtime_sha_ready,
        "input_fingerprint_mismatch_count": sha_mismatch,
        "original_path_violation_count": original_path_violation,
    }


def classify_output(
    *, row: Mapping[str, Any], returncode: int, raw_stdout: str, stderr: str,
    current_input_sha256: str, max_tokens: int, required_sections: Sequence[str],
) -> Dict[str, Any]:
    missing_ids = [field for field in REQUIRED_IDS if not str(row.get(field) or "").strip()]
    clean = output_contract.extract_clean_assistant_text(raw_stdout)
    metrics = output_contract.extract_runtime_metrics(raw_stdout)
    issues = output_contract.detect_text_issues(clean, metrics, max_tokens=max_tokens)
    warnings = [item for item in str(issues.get("cleanup_warnings") or "").split("|") if item]
    absolute_path_remains = bool(re.search(
        r"/(?:Users|Volumes|private|tmp|var|home)/[^\s，。；;]+", clean
    ))
    if absolute_path_remains and "absolute_path_remains" not in warnings:
        warnings.append("absolute_path_remains")
    missing_sections = [section for section in required_sections if section not in clean]
    expected_sha = str(row.get("runtime_visual_file_sha256") or "")
    if current_input_sha256 != expected_sha:
        status = "input_fingerprint_mismatch"
    elif returncode != 0 or not raw_stdout.strip() or not clean.strip():
        status = "failed"
    elif missing_ids or missing_sections:
        status = "missing_required_fields"
    elif "wrapper_or_internal_text_remains" in warnings or absolute_path_remains:
        status = "parse_failed"
    elif "generation_reached_max_tokens" in warnings or "likely_truncated_by_sentence_tail" in warnings:
        status = "truncated"
    elif issues.get("cleanup_status") != "ok":
        status = "parse_failed"
    else:
        status = "success"
    finish_reason = (
        "length" if status == "truncated" else "stop" if status == "success" else "error"
    )
    return {
        "status": status,
        "clean_text": clean,
        "metrics": metrics,
        "issues": issues,
        "missing_ids": missing_ids,
        "missing_required_sections": missing_sections,
        "finish_reason": finish_reason,
        "stderr": stderr,
        "cleanup_warnings": "|".join(warnings),
    }


def resume_filter(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("status") or "") in RESUME_STATUSES]


def preflight(
    *, db: Path, out: Path, config_path: Path, model_path: Path,
    qwen_python: Path, prompt_path: Path, max_tokens: int,
    mode: str, allow_low_token_debug: bool, allow_simulation: bool,
) -> Dict[str, Any]:
    config = load_config(config_path)
    set_offline_environment(config)
    validate_generation_settings(mode, max_tokens, allow_low_token_debug)
    fingerprint = model_fingerprint(model_path, config)
    model_weight = Path(str(fingerprint["model_weight_path"]))
    if not qwen_python.is_file() or not os.access(qwen_python, os.X_OK):
        raise RuntimeError(f"qwen_python_unavailable:{qwen_python}")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    prompt_sha = sha256_text(prompt)
    con, source = queue_connection(db, allow_simulation=allow_simulation)
    try:
        rows = load_queue(con)
        contract = contract_metadata(con)
        queue_audit = validate_queue(rows, verify_runtime_sha=True)
    finally:
        con.close()
    checks = {
        "queue_audit_pass": queue_audit["status"] == "PASS",
        "contract_version_v2": output_contract.CONTRACT_VERSION == "qwenvl_output_contract_v2.0",
        "default_or_requested_tokens_valid": max_tokens >= 384 or (
            mode == "smoke" and allow_low_token_debug
        ) or mode in {"preflight", "dry-run"},
        "temperature_zero": float(config["temperature"]) == 0.0,
        "top_p_one": float(config["top_p"]) == 1.0,
        "prompt_nonempty": bool(prompt),
        "model_weight_exists": model_weight.is_file(),
        "model_fingerprint_files_complete": not fingerprint["missing_model_fingerprint_files"],
        "model_inventory_nonempty": int(fingerprint["model_inventory_file_count"]) > 0,
        "qwen_python_exists": qwen_python.is_file(),
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    return {
        "status": status,
        "technical_status": status,
        "policy_status": "PASS" if status == "PASS" else "FAIL",
        "commit_status": "DO_NOT_COMMIT" if mode in {"preflight", "dry-run"} else "NOT_STARTED",
        "mode": mode,
        "script_version": SCRIPT_VERSION,
        "queue_source": source,
        "queue_count": len(rows),
        "queue_audit": queue_audit,
        "contract": contract,
        "checks": checks,
        "model_path": str(model_path),
        "model_weight_path": str(model_weight),
        "model_sha256": fingerprint["model_weight_sha256"],
        **fingerprint,
        "prompt_path": str(prompt_path),
        "prompt_sha256": prompt_sha,
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "qwen_python": str(qwen_python),
        "qwen_default_max_tokens": output_contract.RECOMMENDED_MAX_TOKENS,
        "max_tokens": max_tokens,
        "temperature": float(config["temperature"]),
        "top_p": float(config["top_p"]),
        "output_contract_version": output_contract.CONTRACT_VERSION,
        "out_path_checked_not_created": str(out),
        "central_db_modified": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def dry_run(
    *, db: Path, out: Path, pre: Mapping[str, Any], max_tokens: int,
    allow_simulation: bool,
) -> Dict[str, Any]:
    con, source = queue_connection(db, allow_simulation=allow_simulation)
    try:
        rows = load_queue(con)
    finally:
        con.close()
    execution_rows = []
    for row in rows:
        item = dict(row)
        item["execution_key"] = execution_key(
            item, str(pre["model_fingerprint_sha256"]), str(pre["prompt_sha256"]),
            output_contract.CONTRACT_VERSION, max_tokens,
        )
        item["planned_status"] = "pending"
        execution_rows.append(item)
    out.mkdir(parents=True, exist_ok=False)
    manifests = out / "manifests"
    reports = out / "reports"
    manifests.mkdir()
    reports.mkdir()
    jsonl_count = write_jsonl(
        manifests / "qwenvl_db_execution_plan.jsonl", execution_rows
    )
    result = dict(pre)
    result.update(
        {
            "mode": "dry-run",
            "queue_source": source,
            "execution_plan_count": len(execution_rows),
            "execution_key_unique_count": len({row["execution_key"] for row in execution_rows}),
            "execution_plan_jsonl_rows": jsonl_count,
            "outputs": {
                "execution_plan_jsonl": str(manifests / "qwenvl_db_execution_plan.jsonl"),
                "summary_json": str(reports / "stop03_3c_qwenvl_db_dry_run_summary.json"),
            },
            "central_db_modified": False,
            "model_run": False,
        }
    )
    (reports / "stop03_3c_qwenvl_db_dry_run_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


def create_run_and_items(
    *, db: Path, rows: Sequence[Mapping[str, Any]], pre: Mapping[str, Any],
    prompt_path: Path, max_tokens: int, workers: int,
) -> Tuple[str, List[Dict[str, Any]]]:
    run_id = "stop03_3c_qwenvl_db_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    script_sha = sha256_file(Path(__file__).resolve())
    created = now_iso()
    planned: List[Dict[str, Any]] = []
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        active: List[Tuple[Mapping[str, Any], str]] = []
        for row in rows:
            key = execution_key(
                row, str(pre["model_fingerprint_sha256"]), str(pre["prompt_sha256"]),
                output_contract.CONTRACT_VERSION, max_tokens,
            )
            existing = con.execute(
                "SELECT run_id,status FROM stop03_3_qwenvl_run_items WHERE execution_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) == SUCCESS_STATUS:
                    continue
                raise RuntimeError(
                    f"execution_key_already_registered_use_resume:{existing['run_id']}"
                )
            active.append((row, key))
        if not active:
            raise RuntimeError("all_execution_keys_already_success")
        con.execute(
            """INSERT INTO stop03_3_qwenvl_runs
            (run_id,v25_contract_name,candidate_id_set_sha256,candidate_semantic_digest_sha256,
             candidate_count,model_name,model_path,model_sha256,model_config_sha256,
             model_tokenizer_files_json,model_tokenizer_files_sha256,model_inventory_json,
             model_inventory_sha256,model_fingerprint_sha256,prompt_path,prompt_sha256,
             orchestrator_config_sha256,
             output_contract_version,max_tokens,temperature,top_p,workers,script_sha256,status,
             pending_count,started_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, contract_lock.CONTRACT_NAME,
                pre["contract"]["candidate_id_set_sha256"],
                pre["contract"]["candidate_semantic_digest_sha256"],
                len(active), Path(str(pre["model_path"])).name, pre["model_path"], pre["model_sha256"],
                pre["model_config_sha256"], pre["model_tokenizer_files_json"],
                pre["model_tokenizer_files_sha256"], pre["model_inventory_json"],
                pre["model_inventory_sha256"], pre["model_fingerprint_sha256"],
                str(prompt_path), pre["prompt_sha256"], pre["config_sha256"],
                output_contract.CONTRACT_VERSION,
                max_tokens, pre["temperature"], pre["top_p"], workers, script_sha,
                "pending", len(active), created,
            ),
        )
        for row, key in active:
            run_item_id = stable_id("qri_", key)
            con.execute(
                """INSERT INTO stop03_3_qwenvl_run_items
                (run_item_id,run_id,candidate_id,execution_key,source_content_id,visual_unit_id,
                 canonical_visual_unit_id,derived_id,candidate_role,reason_codes,policy_version,
                 media_type,time_position_ms,runtime_visual_file,runtime_visual_file_sha256,
                 status,attempt_count,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_item_id, run_id, row["candidate_id"], key, row["source_content_id"],
                    row["visual_unit_id"], row["canonical_visual_unit_id"], row["derived_id"],
                    row["candidate_role"], row["reason_codes"], row["policy_version"],
                    row["media_type"], row["time_position_ms"], row["runtime_visual_file"],
                    row["runtime_visual_file_sha256"], "pending", 0, created,
                ),
            )
            planned.append({**dict(row), "run_item_id": run_item_id, "execution_key": key})
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return run_id, planned


def run_qwen_subprocess(
    *, row: Mapping[str, Any], qwen_python: Path, model_path: Path,
    prompt: str, max_tokens: int, timeout: int,
    process_started_callback: Optional[Callable[[int], None]] = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        str(qwen_python), "-m", "mlx_vlm.generate", "--model", str(model_path),
        "--image", str(row["runtime_visual_file"]), "--prompt", prompt,
        "--max-tokens", str(max_tokens), "--temperature", "0.0",
        "--gen-kwargs", json.dumps({"top_p": 1.0}, separators=(",", ":")),
    ]
    env = dict(os.environ)
    env.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
    })
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(proc)
        active_count = sum(item.poll() is None for item in _ACTIVE_PROCESSES)
    if process_started_callback is not None:
        process_started_callback(active_count)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        stderr = (stderr or "") + "\nqwen_subprocess_timeout"
        return subprocess.CompletedProcess(cmd, 124, stdout or "", stderr)
    finally:
        with _ACTIVE_PROCESS_LOCK:
            _ACTIVE_PROCESSES.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout or "", stderr or "")


def active_qwen_child_process_count() -> int:
    with _ACTIVE_PROCESS_LOCK:
        return sum(proc.poll() is None for proc in _ACTIVE_PROCESSES)


def terminate_active_qwen_subprocesses() -> None:
    _SHUTDOWN_REQUESTED.set()
    with _ACTIVE_PROCESS_LOCK:
        processes = [proc for proc in _ACTIVE_PROCESSES if proc.poll() is None]
    for proc in processes:
        proc.terminate()
    deadline = time.monotonic() + 10.0
    for proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        terminate_active_qwen_subprocesses()
        raise KeyboardInterrupt(f"signal_{signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def persist_item_result(
    *, db: Path, row: Mapping[str, Any], run_id: str, classification: Mapping[str, Any],
    output_row: Mapping[str, Any], stderr_path: Path, pre: Mapping[str, Any],
) -> None:
    status = str(classification["status"])
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_run_items
            SET status=?,attempt_count=attempt_count+1,last_error_code=?,last_error_message=?,
                started_at=COALESCE(started_at,?),finished_at=? WHERE run_item_id=?""",
            (
                status, "" if status == SUCCESS_STATUS else status,
                "" if status == SUCCESS_STATUS else str(classification.get("issues") or ""),
                now_iso(), now_iso(), row["run_item_id"],
            ),
        )
        clean_text = str(output_row.get("qwen_text") or "")
        metrics_path = Path(str(output_row["qwen_runtime_metrics_path"]))
        result_id = stable_id("qres_", row["execution_key"])
        evidence_id = stable_id("qev_", row["execution_key"])
        metrics = dict(classification.get("metrics") or {})
        con.execute(
            """INSERT OR REPLACE INTO stop03_3_qwenvl_results
            (result_id,run_id,run_item_id,candidate_id,execution_key,evidence_id,
             source_content_id,visual_unit_id,canonical_visual_unit_id,derived_id,
             candidate_role,reason_codes,policy_version,result_status,clean_text,
             qwen_text_preview,clean_text_sha256,raw_stdout_path,raw_stdout_sha256,
             stderr_path,stderr_sha256,metrics_path,metrics_sha256,runtime_metrics_json,
             prompt_tokens,generation_tokens,peak_memory_gb,finish_reason,truncation_status,
             cleanup_status,cleanup_warnings,output_contract_version,
             runtime_visual_file_sha256,model_sha256,model_config_sha256,
             model_tokenizer_files_json,model_tokenizer_files_sha256,
             model_inventory_sha256,model_fingerprint_sha256,prompt_sha256,
             orchestrator_config_sha256,script_sha256,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result_id, run_id, row["run_item_id"], row["candidate_id"],
                row["execution_key"], evidence_id, row["source_content_id"],
                row["visual_unit_id"], row["canonical_visual_unit_id"], row["derived_id"],
                row["candidate_role"], row["reason_codes"], row["policy_version"], status,
                clean_text, clean_text[:500], output_row["qwen_text_sha256"],
                output_row["qwen_raw_stdout_path"], output_row["qwen_raw_stdout_sha256"],
                str(stderr_path), sha256_file(stderr_path), str(metrics_path),
                sha256_file(metrics_path), output_row["qwen_runtime_metrics_json"],
                metrics.get("prompt_tokens"), metrics.get("generation_tokens"),
                metrics.get("peak_memory_gb"), classification["finish_reason"],
                "truncated" if status == "truncated" else "complete",
                str(classification.get("issues", {}).get("cleanup_status") or "failed"),
                str(classification.get("cleanup_warnings") or classification.get("issues", {}).get("cleanup_warnings") or ""),
                output_contract.CONTRACT_VERSION, row["runtime_visual_file_sha256"],
                pre["model_sha256"], pre["model_config_sha256"],
                pre["model_tokenizer_files_json"], pre["model_tokenizer_files_sha256"],
                pre["model_inventory_sha256"], pre["model_fingerprint_sha256"],
                pre["prompt_sha256"], pre["config_sha256"],
                sha256_file(Path(__file__).resolve()), now_iso(),
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


class ConcurrencyMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.workers_active = 0
        self.workers_active_peak = 0
        self.db_running_peak = 0
        self.mlx_child_process_peak = 0

    def worker_started(self, db_running: int) -> None:
        with self._lock:
            self.workers_active += 1
            self.workers_active_peak = max(self.workers_active_peak, self.workers_active)
            self.db_running_peak = max(self.db_running_peak, db_running)

    def worker_finished(self) -> None:
        with self._lock:
            self.workers_active = max(0, self.workers_active - 1)

    def observe_child_processes(self, active: int) -> None:
        with self._lock:
            self.mlx_child_process_peak = max(self.mlx_child_process_peak, active)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "workers_active": self.workers_active,
                "workers_active_peak": self.workers_active_peak,
                "db_running_peak": self.db_running_peak,
                "mlx_child_process_peak": self.mlx_child_process_peak,
            }


def item_status_counts(db: Path, run_id: str) -> Dict[str, int]:
    con = readonly_connection(db)
    try:
        counts = Counter({
            str(row[0]): int(row[1]) for row in con.execute(
                "SELECT status,COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        })
        total = int(con.execute(
            "SELECT COUNT(*) FROM stop03_3_qwenvl_run_items WHERE run_id=?", (run_id,)
        ).fetchone()[0])
    finally:
        con.close()
    counts["total"] = total
    return dict(counts)


def claim_item(
    db: Path, row: Mapping[str, Any],
    connection_observer: Optional[Callable[[sqlite3.Connection, str, Mapping[str, Any]], None]] = None,
) -> bool:
    """Atomically claim one retryable item, then close the write transaction."""
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        if connection_observer is not None:
            connection_observer(con, "claim", row)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in RESUME_STATUSES)
        values = [now_iso(), row["run_item_id"], *sorted(RESUME_STATUSES)]
        cursor = con.execute(
            f"""UPDATE stop03_3_qwenvl_run_items
            SET status='running',started_at=?,finished_at=NULL
            WHERE run_item_id=? AND status IN ({placeholders})
              AND NOT EXISTS (
                SELECT 1 FROM stop03_3_qwenvl_results AS r
                WHERE r.run_item_id=stop03_3_qwenvl_run_items.run_item_id
                  AND r.result_status='success'
              )""",
            values,
        )
        claimed = cursor.rowcount == 1
        con.commit()
        return claimed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def refresh_run_summary(db: Path, run_id: str) -> Dict[str, int]:
    counts = item_status_counts(db, run_id)
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    success = counts.get("success", 0)
    failed = sum(counts.get(status, 0) for status in NON_SUCCESS_STATUSES)
    review = counts.get("review", 0)
    remaining = counts["total"] - success
    run_status = "success" if remaining == 0 else "running" if running else "partial"
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_3_qwenvl_runs
            SET status=?,pending_count=?,success_count=?,failed_count=?,review_count=?,finished_at=?
            WHERE run_id=?""",
            (
                run_status, pending, success, failed, review,
                now_iso() if remaining == 0 else None, run_id,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return counts


def write_progress(
    *, out: Path, run_id: str, workers_requested: int,
    metrics: ConcurrencyMetrics, counts: Mapping[str, int],
    last_completed_candidate_id: str, last_completed_elapsed_seconds: float,
) -> Dict[str, Any]:
    success = int(counts.get("success", 0))
    total = int(counts.get("total", 0))
    truncated = int(counts.get("truncated", 0))
    failed = sum(int(counts.get(status, 0)) for status in (
        "failed", "parse_failed", "missing_required_fields", "input_fingerprint_mismatch"
    ))
    metric_values = metrics.snapshot()
    payload = {
        "contract": "media_archive_stage_runtime_contract_v1",
        "event": "stage_progress",
        "stage_key": "qwen_optional_v2",
        "timestamp": now_iso(),
        "run_id": run_id,
        "workers_requested": workers_requested,
        "workers_active": metric_values["workers_active"],
        "configured_workers": workers_requested,
        "actual_workers": metric_values["workers_active"],
        "pending": int(counts.get("pending", 0)),
        "running": int(counts.get("running", 0)),
        "completed": success,
        "total": total,
        "success": success,
        "skipped": 0,
        "truncated": truncated,
        "failed": failed,
        "remaining": max(0, total - success),
        "percent": round((success / total * 100.0) if total else 100.0, 3),
        "last_completed_candidate_id": last_completed_candidate_id,
        "last_completed_elapsed_seconds": round(last_completed_elapsed_seconds, 3),
        **metric_values,
    }
    progress_path = out / "logs/progress.jsonl"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return payload


def execute_one_item(
    *, db: Path, out: Path, run_id: str, row: Mapping[str, Any],
    pre: Mapping[str, Any], qwen_python: Path, model_path: Path, prompt: str,
    required_sections: Sequence[str], max_tokens: int, timeout: int,
    metrics: ConcurrencyMetrics,
    inference_fn: Callable[..., subprocess.CompletedProcess[str]],
    connection_observer: Optional[Callable[[sqlite3.Connection, str, Mapping[str, Any]], None]],
) -> Dict[str, Any]:
    started = time.monotonic()
    if _SHUTDOWN_REQUESTED.is_set() or not claim_item(db, row, connection_observer):
        return {
            "status": "skipped", "candidate_id": row["candidate_id"],
            "elapsed_seconds": time.monotonic() - started,
        }
    running = item_status_counts(db, run_id).get("running", 0)
    metrics.worker_started(running)
    output_root = out / "outputs"
    stderr_dir = output_root / "qwenvl_stderr"
    try:
        current_sha = sha256_file(Path(str(row["runtime_visual_file"])))
        raw_stdout = ""
        stderr = ""
        returncode = 1
        if current_sha != str(row["runtime_visual_file_sha256"]):
            classification = {
                "status": "input_fingerprint_mismatch", "clean_text": "", "metrics": {},
                "issues": {"cleanup_status": "failed", "cleanup_warnings": "input_fingerprint_mismatch"},
                "cleanup_warnings": "input_fingerprint_mismatch", "finish_reason": "error",
            }
        else:
            try:
                proc = inference_fn(
                    row=row, qwen_python=qwen_python, model_path=model_path,
                    prompt=prompt, max_tokens=max_tokens, timeout=timeout,
                    process_started_callback=metrics.observe_child_processes,
                )
                raw_stdout, stderr, returncode = proc.stdout or "", proc.stderr or "", proc.returncode
                classification = classify_output(
                    row=row, returncode=returncode, raw_stdout=raw_stdout,
                    stderr=stderr, current_input_sha256=current_sha,
                    max_tokens=max_tokens, required_sections=required_sections,
                )
            except Exception as exc:
                stderr = f"{type(exc).__name__}:{exc}"
                classification = {
                    "status": "failed", "clean_text": "", "metrics": {},
                    "issues": {"cleanup_status": "failed", "cleanup_warnings": "inference_exception"},
                    "cleanup_warnings": "inference_exception", "finish_reason": "error",
                }
        output_row = output_contract.write_qwenvl_contract_outputs(
            evidence_id=stable_id("qev_", row["execution_key"]),
            raw_stdout=raw_stdout, out_dir=output_root, max_tokens=max_tokens,
        )
        stderr_path = stderr_dir / f"{row['run_item_id']}.stderr.txt"
        stderr_path.write_text(stderr, encoding="utf-8")
        persist_item_result(
            db=db, row=row, run_id=run_id, classification=classification,
            output_row=output_row, stderr_path=stderr_path, pre=pre,
        )
        return {
            "status": str(classification["status"]),
            "candidate_id": str(row["candidate_id"]),
            "elapsed_seconds": time.monotonic() - started,
            "returncode": returncode,
        }
    finally:
        metrics.worker_finished()


def execute_items(
    *, db: Path, out: Path, run_id: str, rows: Sequence[Mapping[str, Any]],
    pre: Mapping[str, Any], qwen_python: Path, model_path: Path, prompt: str,
    required_sections: Sequence[str], max_tokens: int, timeout: int, workers: int,
    progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
    inference_fn: Callable[..., subprocess.CompletedProcess[str]] = run_qwen_subprocess,
    connection_observer: Optional[Callable[[sqlite3.Connection, str, Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    output_root = out / "outputs"
    stderr_dir = output_root / "qwenvl_stderr"
    stderr_dir.mkdir(parents=True, exist_ok=True)
    if workers < 1:
        raise RuntimeError("workers_must_be_positive")
    _SHUTDOWN_REQUESTED.clear()
    statuses = Counter()
    metrics = ConcurrencyMetrics()
    row_iter = iter(rows)
    futures: Dict[concurrent.futures.Future[Dict[str, Any]], Mapping[str, Any]] = {}

    def submit_next(executor: concurrent.futures.ThreadPoolExecutor) -> bool:
        if _SHUTDOWN_REQUESTED.is_set():
            return False
        try:
            row = next(row_iter)
        except StopIteration:
            return False
        future = executor.submit(
            execute_one_item,
            db=db, out=out, run_id=run_id, row=row, pre=pre,
            qwen_python=qwen_python, model_path=model_path, prompt=prompt,
            required_sections=required_sections, max_tokens=max_tokens, timeout=timeout,
            metrics=metrics, inference_fn=inference_fn,
            connection_observer=connection_observer,
        )
        futures[future] = row
        return True

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qwenvl")
    try:
        for _ in range(min(workers, len(rows))):
            submit_next(executor)
        while futures:
            done, _ = concurrent.futures.wait(
                tuple(futures), return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                futures.pop(future)
                result = future.result()
                status = str(result["status"])
                statuses[status] += 1
                counts = refresh_run_summary(db, run_id)
                progress = write_progress(
                    out=out, run_id=run_id, workers_requested=workers,
                    metrics=metrics, counts=counts,
                    last_completed_candidate_id=str(result["candidate_id"]),
                    last_completed_elapsed_seconds=float(result["elapsed_seconds"]),
                )
                if progress_callback is not None:
                    progress_callback(progress)
                submit_next(executor)
    except KeyboardInterrupt:
        _SHUTDOWN_REQUESTED.set()
        terminate_active_qwen_subprocesses()
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    final_counts = refresh_run_summary(db, run_id)
    return {
        "status_counts": dict(statuses),
        "processed_count": sum(statuses.values()),
        "workers_requested": workers,
        "workers_effective": metrics.snapshot()["workers_active_peak"],
        "db_running_peak": metrics.snapshot()["db_running_peak"],
        "mlx_child_process_peak": metrics.snapshot()["mlx_child_process_peak"],
        "final_item_status_counts": final_counts,
        "progress_path": str(out / "logs/progress.jsonl"),
    }


def readback_run(db: Path, run_id: str, *, expected_count: Optional[int] = None) -> Dict[str, Any]:
    con = readonly_connection(db)
    try:
        run = con.execute(
            "SELECT * FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"qwenvl_run_missing:{run_id}")
        items = [dict(row) for row in con.execute(
            "SELECT * FROM stop03_3_qwenvl_run_items WHERE run_id=? ORDER BY candidate_id",
            (run_id,),
        )]
        results = [dict(row) for row in con.execute(
            "SELECT * FROM stop03_3_qwenvl_results WHERE run_id=? ORDER BY candidate_id",
            (run_id,),
        )]
    finally:
        con.close()
    status_counts = Counter(str(row["status"]) for row in items)
    result_status_counts = Counter(str(row["result_status"]) for row in results)
    execution_keys = [str(row["execution_key"]) for row in items]
    duplicate_count = len(execution_keys) - len(set(execution_keys))
    result_by_item = {str(row["run_item_id"]): row for row in results}
    missing_result_count = sum(str(row["run_item_id"]) not in result_by_item for row in items)
    forced_id_missing_count = sum(
        any(not str(row.get(field) or "").strip() for field in REQUIRED_IDS)
        for row in results
    )
    result_id_match = all(
        str(row["result_id"]) == stable_id("qres_", row["execution_key"])
        and str(row["evidence_id"]) == stable_id("qev_", row["execution_key"])
        for row in results
    )
    result_item_match = all(
        str(result_by_item.get(str(item["run_item_id"]), {}).get("candidate_id") or "")
        == str(item["candidate_id"])
        and str(result_by_item.get(str(item["run_item_id"]), {}).get("runtime_visual_file_sha256") or "")
        == str(item["runtime_visual_file_sha256"])
        for item in items
    )
    invalid_success_count = sum(
        row["result_status"] == SUCCESS_STATUS and (
            row["truncation_status"] != "complete"
            or row["cleanup_status"] != "ok"
            or row["finish_reason"] != "stop"
            or not str(row["clean_text"] or "").strip()
            or int(row["generation_tokens"] or 0) >= int(run["max_tokens"])
        )
        for row in results
    )
    fingerprint_missing_count = sum(
        any(not str(row.get(field) or "").strip() for field in (
            "runtime_visual_file_sha256", "model_sha256", "model_config_sha256",
            "model_tokenizer_files_json", "model_tokenizer_files_sha256",
            "model_inventory_sha256", "model_fingerprint_sha256", "prompt_sha256",
            "orchestrator_config_sha256", "script_sha256",
        ))
        for row in results
    )
    expected = int(run["candidate_count"]) if expected_count is None else expected_count
    checks = {
        "item_count_matches": len(items) == expected,
        "result_count_matches": len(results) == expected,
        "missing_result_zero": missing_result_count == 0,
        "forced_ids_complete": forced_id_missing_count == 0,
        "execution_keys_unique": duplicate_count == 0,
        "result_ids_match": result_id_match,
        "result_item_input_match": result_item_match,
        "success_gate_valid": invalid_success_count == 0,
        "strong_fingerprints_complete": fingerprint_missing_count == 0,
        "contract_v2": run["output_contract_version"] == output_contract.CONTRACT_VERSION,
        "max_tokens_384": int(run["max_tokens"]) == 384,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "run_id": run_id,
        "run": dict(run),
        "item_count": len(items),
        "result_count": len(results),
        "status_counts": dict(status_counts),
        "result_status_counts": dict(result_status_counts),
        "execution_key_duplicate_count": duplicate_count,
        "missing_result_count": missing_result_count,
        "forced_id_missing_count": forced_id_missing_count,
        "invalid_success_count": invalid_success_count,
        "fingerprint_missing_count": fingerprint_missing_count,
        "result_id_match": result_id_match,
        "checks": checks,
    }


def prepare_resume_run(db: Path, run_id: str, *, workers: int, max_tokens: int) -> None:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            "SELECT max_tokens FROM stop03_3_qwenvl_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"resume_run_missing:{run_id}")
        if int(run["max_tokens"]) != max_tokens:
            raise RuntimeError("resume_max_tokens_mismatch")
        con.execute(
            "UPDATE stop03_3_qwenvl_runs SET workers=?,status='running',finished_at=NULL WHERE run_id=?",
            (workers, run_id),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def production_run(
    *, mode: str, db: Path, out: Path, pre: Mapping[str, Any], config: Mapping[str, Any],
    model_path: Path, qwen_python: Path, prompt_path: Path, max_tokens: int,
    workers: int, timeout: int, limit: int, run_id: str,
    inference_fn: Callable[..., subprocess.CompletedProcess[str]] = run_qwen_subprocess,
    connection_observer: Optional[Callable[[sqlite3.Connection, str, Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    if pre["queue_source"] != "central_db_view":
        raise RuntimeError("production_requires_committed_central_db_view")
    out.mkdir(parents=True, exist_ok=False if mode != "resume" else True)
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if mode == "resume":
        if not run_id:
            raise RuntimeError("resume_requires_run_id")
        prepare_resume_run(db, run_id, workers=workers, max_tokens=max_tokens)
        con = readonly_connection(db)
        try:
            rows = resume_filter([
                dict(row) for row in con.execute(
                    "SELECT * FROM stop03_3_qwenvl_run_items WHERE run_id=? ORDER BY candidate_id",
                    (run_id,),
                )
            ])
        finally:
            con.close()
        if limit > 0:
            rows = rows[:limit]
        selected_run_id = run_id
    else:
        con, _ = queue_connection(db, allow_simulation=False)
        try:
            rows = load_queue(con)
        finally:
            con.close()
        if limit > 0:
            rows = rows[:limit]
        selected_run_id, rows = create_run_and_items(
            db=db, rows=rows, pre=pre, prompt_path=prompt_path,
            max_tokens=max_tokens, workers=workers,
        )
    execution = execute_items(
        db=db, out=out, run_id=selected_run_id, rows=rows, pre=pre,
        qwen_python=qwen_python, model_path=model_path, prompt=prompt,
        required_sections=config["required_output_sections"], max_tokens=max_tokens,
        timeout=timeout, workers=workers, inference_fn=inference_fn,
        connection_observer=connection_observer,
    )
    return {
        "status": "PASS" if execution["status_counts"].get("success", 0) == len(rows) else "PASS_WITH_REVIEW",
        "technical_status": "PASS", "policy_status": "REVIEW",
        "commit_status": "QWENVL_RESULTS_WRITTEN", "run_id": selected_run_id,
        "execution": execution, "central_db_modified": True, "model_run": True,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen-VL orchestrator from the central V25 DB view")
    parser.add_argument("--mode", required=True, choices=("preflight", "dry-run", "smoke", "run", "resume", "readback"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--model")
    parser.add_argument("--qwen-python")
    parser.add_argument("--prompt")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--allow-low-token-debug", action="store_true")
    parser.add_argument("--simulate-uncommitted-contract", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    install_signal_handlers()
    try:
        config_path = Path(args.config).expanduser().resolve(strict=True)
        config = load_config(config_path)
        set_offline_environment(config)
        db = Path(args.db).expanduser().resolve(strict=True)
        out = assert_output_path(Path(args.out), may_exist=args.mode in {"resume", "readback"})
        model_path = Path(args.model or config["model_path"]).expanduser().resolve(strict=True)
        # Preserve the registered environment path instead of replacing it with
        # the Homebrew interpreter target behind the env symlink.
        qwen_python = Path(args.qwen_python or config["qwen_python"]).expanduser().absolute()
        if not qwen_python.is_file():
            raise RuntimeError(f"qwen_python_unavailable:{qwen_python}")
        prompt_path = Path(args.prompt or config["prompt_path"]).expanduser().resolve(strict=True)
        max_tokens = int(args.max_tokens or config["default_max_tokens"])
        workers = int(args.workers or config["default_workers"])
        timeout = int(args.timeout or config["default_timeout_seconds"])
        before = db_state(db)
        if args.mode == "readback":
            if args.run_id:
                rb = readback_run(db, args.run_id)
                result = {
                    "status": rb["status"], "technical_status": rb["status"],
                    "policy_status": "PASS" if rb["status"] == "PASS" else "FAIL",
                    "commit_status": "READBACK_ONLY", "readback": rb,
                    "central_db_modified": False, "model_run": False,
                }
            else:
                con = readonly_connection(db)
                try:
                    if not object_exists(con, "stop03_3_qwenvl_runs", "table"):
                        raise RuntimeError("qwenvl_runs_table_missing")
                    run_counts = [dict(row) for row in con.execute(
                        "SELECT run_id,status,candidate_count,success_count,failed_count,review_count FROM stop03_3_qwenvl_runs ORDER BY started_at"
                    )]
                finally:
                    con.close()
                result = {
                    "status": "PASS", "technical_status": "PASS", "policy_status": "REVIEW",
                    "commit_status": "READBACK_ONLY", "runs": run_counts,
                    "central_db_modified": False, "model_run": False,
                }
        else:
            allow_simulation = bool(
                args.simulate_uncommitted_contract and args.mode in {"preflight", "dry-run"}
            )
            pre = preflight(
                db=db, out=out, config_path=config_path, model_path=model_path,
                qwen_python=qwen_python, prompt_path=prompt_path, max_tokens=max_tokens,
                mode=args.mode, allow_low_token_debug=bool(args.allow_low_token_debug),
                allow_simulation=allow_simulation,
            )
            if pre["technical_status"] != "PASS":
                raise RuntimeError("orchestrator_preflight_failed")
            if args.mode == "preflight":
                result = pre
            elif args.mode == "dry-run":
                result = dry_run(
                    db=db, out=out, pre=pre, max_tokens=max_tokens,
                    allow_simulation=allow_simulation,
                )
            else:
                result = production_run(
                    mode=args.mode, db=db, out=out, pre=pre, config=config,
                    model_path=model_path, qwen_python=qwen_python,
                    prompt_path=prompt_path, max_tokens=max_tokens, workers=workers,
                    timeout=timeout, limit=args.limit, run_id=args.run_id,
                )
        after = db_state(db)
        result["central_db_state_before"] = before
        result["central_db_state_after"] = after
        if args.mode in {"preflight", "dry-run", "readback"}:
            result["central_db_modified"] = before != after
            if before != after:
                result["status"] = result["technical_status"] = "FAIL"
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("technical_status") == "PASS" else 2
    except Exception as exc:
        failure = {
            "status": "FAIL", "technical_status": "FAIL", "policy_status": "FAIL",
            "commit_status": "DO_NOT_COMMIT", "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__, "error_message": str(exc),
            "central_db_modified": False, "model_run": False,
            "network_used": False, "download_used": False, "original_video_read": False,
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
