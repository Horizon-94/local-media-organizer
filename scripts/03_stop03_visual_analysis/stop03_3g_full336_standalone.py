#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import multiprocessing as mp
import os
import queue
import re
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_SOURCE_DB = PROJECT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_OUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
LATEST_POINTER = PROJECT / "logs/stop03_3g_full336_standalone_latest.txt"
REGISTRIES = (
    PROJECT / "docs/model_registry/LOCAL_MODEL_REGISTRY.md",
    PROJECT / "docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md",
)
QWEN_VIEW = "v_stop03_2_v25_qwenvl_execution_queue"
RUNNER_VERSION = "stop03_3g_full336_standalone_corrected_batch_v1"
GENERATION_BACKEND = "mlx_vlm_batch_generate_batch_size_1_corrected_v1"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

STOP03_SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS stop03_3g_run (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_db_path TEXT NOT NULL,
    source_db_sha256_before TEXT NOT NULL,
    source_db_sha256_after TEXT,
    source_db_unchanged INTEGER,
    validation_db_path TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    workers INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    timeout_per_item INTEGER NOT NULL,
    generation_backend TEXT NOT NULL,
    runner_version TEXT NOT NULL,
    model_path TEXT NOT NULL,
    model_fingerprint_sha256 TEXT NOT NULL,
    prompt_path TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    config_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    network_used INTEGER NOT NULL DEFAULT 0,
    downloads_performed INTEGER NOT NULL DEFAULT 0,
    original_media_modified INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT,
    failure_message TEXT
);

CREATE TABLE IF NOT EXISTS stop03_3g_candidate (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    selected_order INTEGER NOT NULL,
    source_content_id TEXT,
    visual_unit_id TEXT,
    canonical_visual_unit_id TEXT,
    derived_id TEXT,
    media_type TEXT,
    candidate_role TEXT,
    queue_type TEXT,
    time_position_ms INTEGER,
    runtime_visual_file TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    status TEXT NOT NULL,
    current_attempt INTEGER NOT NULL DEFAULT 0,
    selected_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    assigned_worker_id INTEGER,
    PRIMARY KEY (run_id, candidate_id),
    UNIQUE (run_id, selected_order),
    UNIQUE (run_id, execution_key),
    FOREIGN KEY (run_id) REFERENCES stop03_3g_run(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stop03_3g_worker (
    run_id TEXT NOT NULL,
    worker_id INTEGER NOT NULL,
    pid INTEGER,
    lifecycle TEXT NOT NULL,
    model_load_count INTEGER NOT NULL DEFAULT 0,
    processor_load_count INTEGER NOT NULL DEFAULT 0,
    model_load_seconds REAL,
    assigned_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    current_candidate_id TEXT,
    current_selected_order INTEGER,
    active_memory_gb REAL,
    cache_memory_gb REAL,
    peak_memory_gb REAL,
    rss_mb REAL,
    cpu_percent REAL,
    heartbeat_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    error_message TEXT,
    PRIMARY KEY (run_id, worker_id),
    FOREIGN KEY (run_id) REFERENCES stop03_3g_run(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stop03_3g_attempt (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    worker_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_seconds REAL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER,
    generated_tokens INTEGER,
    raw_finish_reason TEXT,
    inferred_finish_reason TEXT,
    degeneration_detected INTEGER,
    truncation_detected INTEGER,
    error_type TEXT,
    error_message TEXT,
    traceback_text TEXT,
    UNIQUE (run_id, candidate_id, attempt_number),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES stop03_3g_candidate(run_id, candidate_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, worker_id)
        REFERENCES stop03_3g_worker(run_id, worker_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stop03_3g_result (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    worker_id INTEGER NOT NULL,
    result_status TEXT NOT NULL,
    raw_text TEXT,
    clean_text TEXT,
    clean_text_sha256 TEXT,
    raw_output_path TEXT,
    clean_output_path TEXT,
    prompt_tokens INTEGER,
    generated_tokens INTEGER,
    raw_finish_reason TEXT,
    inferred_finish_reason TEXT,
    degeneration_detected INTEGER NOT NULL,
    truncation_detected INTEGER NOT NULL,
    cleanup_status TEXT,
    cleanup_warnings TEXT,
    missing_required_sections_json TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    active_memory_gb REAL,
    cache_memory_gb REAL,
    peak_memory_gb REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id),
    UNIQUE (run_id, execution_key),
    FOREIGN KEY (attempt_id) REFERENCES stop03_3g_attempt(attempt_id),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES stop03_3g_candidate(run_id, candidate_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stop03_3g_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    worker_id INTEGER,
    candidate_id TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES stop03_3g_run(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stop03_3g_system_telemetry (
    telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    active_workers INTEGER,
    completed_count INTEGER,
    pending_count INTEGER,
    running_count INTEGER,
    success_count INTEGER,
    review_count INTEGER,
    failed_count INTEGER,
    worker_rss_sum_mb REAL,
    memory_pressure_percent REAL,
    swap_used_bytes INTEGER,
    FOREIGN KEY (run_id) REFERENCES stop03_3g_run(run_id) ON DELETE CASCADE
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path, announce: bool = False) -> str:
    if announce:
        print(f"[HASH] start {path}", flush=True)
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if announce:
        print(f"[HASH] done  {path.name} {digest}", flush=True)
    return digest


def file_state(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {
        "sha256": sha256_file(path),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def stable_id(prefix: str, *parts: Any) -> str:
    return prefix + sha256_text("\x1f".join(str(part) for part in parts))[:32]


def set_offline_environment() -> None:
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "ULTRALYTICS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })


def install_network_guard() -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def blocked_connect(sock: socket.socket, address: Any) -> Any:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"network_access_blocked:{address}")
        return original_connect(sock, address)

    def blocked_connect_ex(sock: socket.socket, address: Any) -> int:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"network_access_blocked:{address}")
        return original_connect_ex(sock, address)

    def blocked_create_connection(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"network_access_blocked:{args[:1]}")

    socket.socket.connect = blocked_connect
    socket.socket.connect_ex = blocked_connect_ex
    socket.create_connection = blocked_create_connection


def read_required_registry_files() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for path in REGISTRIES:
        if not path.is_file():
            raise RuntimeError(f"missing_registry:{path}")
        text = path.read_text(encoding="utf-8", errors="strict")
        if not text.strip():
            raise RuntimeError(f"empty_registry:{path}")
        result[str(path)] = sha256_text(text)
    return result


def model_fingerprint(model_path: Path) -> Dict[str, Any]:
    expected = [
        model_path / "config.json",
        model_path / "model.safetensors",
        model_path / "tokenizer.json",
        model_path / "tokenizer_config.json",
    ]
    missing = [str(path) for path in expected if not path.is_file()]
    if missing:
        raise RuntimeError("model_files_missing:" + json.dumps(missing, ensure_ascii=False))
    rows = []
    for path in expected:
        rows.append({
            "relative_path": str(path.relative_to(model_path)),
            "size": int(path.stat().st_size),
            "sha256": sha256_file(path, announce=True),
        })
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"files": rows, "aggregate_sha256": sha256_text(payload)}


def backup_sqlite_readonly(source_db: Path, target_db: Path) -> None:
    if target_db.exists():
        raise RuntimeError(f"validation_db_exists:{target_db}")
    source = sqlite3.connect(f"file:{source_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    target = sqlite3.connect(str(target_db), timeout=60.0)
    try:
        source.backup(target)
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = list(target.execute("PRAGMA foreign_key_check"))
    finally:
        target.close()
        source.close()
    if integrity != "ok" or foreign:
        raise RuntimeError(
            f"validation_db_backup_invalid:integrity={integrity}:foreign={foreign}"
        )


def open_validation_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=60.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(STOP03_SCHEMA)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def normalized(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def load_candidates(validation_db: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    con = sqlite3.connect(f"file:{validation_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    con.row_factory = sqlite3.Row
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
            (QWEN_VIEW,),
        ).fetchone()
        if not exists:
            raise RuntimeError(f"missing_view:{QWEN_VIEW}")
        rows = [
            dict(row)
            for row in con.execute(
                f'SELECT * FROM "{QWEN_VIEW}" ORDER BY candidate_id'
            )
        ]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    finally:
        con.close()

    if len(rows) != 336:
        raise RuntimeError(f"candidate_total_not_336:{len(rows)}")

    candidate_ids = [str(row.get("candidate_id") or "") for row in rows]
    if not all(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError("candidate_id_missing_or_duplicate")

    path_values: List[str] = []
    for index, row in enumerate(rows, 1):
        candidate_id = str(row["candidate_id"])
        path = Path(str(row.get("runtime_visual_file") or ""))
        expected_sha = str(row.get("runtime_visual_file_sha256") or "")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise RuntimeError(f"runtime_input_not_image:{candidate_id}:{path}")
        if not path.is_file():
            raise RuntimeError(f"runtime_input_missing:{candidate_id}:{path}")
        actual_sha = sha256_file(path)
        if not expected_sha or actual_sha != expected_sha:
            raise RuntimeError(
                f"runtime_input_sha_mismatch:{candidate_id}:{actual_sha}:{expected_sha}"
            )
        path_values.append(str(path.resolve()))
        if index % 25 == 0 or index == len(rows):
            print(f"[INPUT] verified {index}/{len(rows)}", flush=True)

    if len(path_values) != len(set(path_values)):
        raise RuntimeError("runtime_visual_file_duplicate")

    return rows, {
        "candidate_count": len(rows),
        "candidate_unique": len(set(candidate_ids)),
        "runtime_file_unique": len(set(path_values)),
        "integrity_check": integrity,
        "foreign_key_errors": foreign,
    }


def load_prompt_and_contract(
    config_path: Path,
    prompt_path: Path,
) -> Tuple[Dict[str, Any], str, List[str]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("prompt_empty")
    required_sections = [
        str(value)
        for value in config.get("required_output_sections", [])
        if str(value).strip()
    ]
    return config, prompt, required_sections


def execution_key(
    row: Mapping[str, Any],
    model_fingerprint_sha: str,
    prompt_sha: str,
    config_sha: str,
    max_tokens: int,
) -> str:
    payload = {
        "candidate_id": str(row["candidate_id"]),
        "runtime_visual_file_sha256": str(row["runtime_visual_file_sha256"]),
        "model_fingerprint_sha256": model_fingerprint_sha,
        "prompt_sha256": prompt_sha,
        "config_sha256": config_sha,
        "generation_backend": GENERATION_BACKEND,
        "runner_version": RUNNER_VERSION,
        "max_tokens": max_tokens,
    }
    return "exec_" + sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def init_run_database(
    con: sqlite3.Connection,
    run_id: str,
    source_db: Path,
    source_before: Mapping[str, Any],
    validation_db: Path,
    candidates: Sequence[Mapping[str, Any]],
    workers: int,
    max_tokens: int,
    timeout_per_item: int,
    model_path: Path,
    model_fp_sha: str,
    prompt_path: Path,
    prompt_sha: str,
    config_path: Path,
    config_sha: str,
) -> None:
    con.execute(
        """
        INSERT INTO stop03_3g_run (
            run_id,status,started_at,source_db_path,source_db_sha256_before,
            validation_db_path,candidate_count,workers,max_tokens,timeout_per_item,
            generation_backend,runner_version,model_path,model_fingerprint_sha256,
            prompt_path,prompt_sha256,config_path,config_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, "running", now_iso(), str(source_db), source_before["sha256"],
            str(validation_db), len(candidates), workers, max_tokens, timeout_per_item,
            GENERATION_BACKEND, RUNNER_VERSION, str(model_path), model_fp_sha,
            str(prompt_path), prompt_sha, str(config_path), config_sha,
        ),
    )
    for worker_id in range(1, workers + 1):
        con.execute(
            """
            INSERT INTO stop03_3g_worker
            (run_id,worker_id,lifecycle,started_at,heartbeat_at)
            VALUES (?,?,?,?,?)
            """,
            (run_id, worker_id, "starting", now_iso(), now_iso()),
        )
    for index, row in enumerate(candidates, 1):
        con.execute(
            """
            INSERT INTO stop03_3g_candidate (
                run_id,candidate_id,selected_order,source_content_id,visual_unit_id,
                canonical_visual_unit_id,derived_id,media_type,candidate_role,
                queue_type,time_position_ms,runtime_visual_file,
                runtime_visual_file_sha256,execution_key,status,selected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                str(row["candidate_id"]),
                index,
                normalized(row, "source_content_id"),
                normalized(row, "visual_unit_id"),
                normalized(row, "canonical_visual_unit_id"),
                normalized(row, "derived_id"),
                normalized(row, "media_type", "source_media_type"),
                normalized(row, "candidate_role", "role"),
                normalized(row, "queue_type", "route_type", "route"),
                row.get("time_position_ms"),
                str(row["runtime_visual_file"]),
                str(row["runtime_visual_file_sha256"]),
                str(row["execution_key"]),
                "pending",
                now_iso(),
            ),
        )
    con.execute(
        """
        INSERT INTO stop03_3g_event
        (run_id,timestamp,event_type,payload_json)
        VALUES (?,?,?,?)
        """,
        (
            run_id, now_iso(), "candidate_materialized",
            json.dumps({"count": len(candidates)}, ensure_ascii=False),
        ),
    )
    con.commit()


def resume_run_database(
    con: sqlite3.Connection,
    run_id: str,
) -> List[Dict[str, Any]]:
    con.execute(
        """
        UPDATE stop03_3g_candidate
        SET status='pending', started_at=NULL, assigned_worker_id=NULL
        WHERE run_id=? AND status='running'
        """,
        (run_id,),
    )
    con.execute(
        """
        UPDATE stop03_3g_worker
        SET lifecycle='starting', pid=NULL, current_candidate_id=NULL,
            current_selected_order=NULL, heartbeat_at=?, finished_at=NULL,
            exit_code=NULL, error_message=NULL
        WHERE run_id=?
        """,
        (now_iso(), run_id),
    )
    con.execute(
        """
        UPDATE stop03_3g_run
        SET status='running', finished_at=NULL, failure_code=NULL, failure_message=NULL
        WHERE run_id=?
        """,
        (run_id,),
    )
    rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT * FROM stop03_3g_candidate
            WHERE run_id=? AND status NOT IN ('success','review','failed')
            ORDER BY selected_order
            """,
            (run_id,),
        )
    ]
    con.commit()
    return rows


def _metric_value(source: Any, names: Sequence[str]) -> Any:
    if source is None:
        return None
    if isinstance(source, (list, tuple)):
        if not source:
            return None
        return _metric_value(source[0], names)
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source.get(name)
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def extract_batch_response(
    response: Any,
    processor: Any,
    prompt: str,
    max_tokens: int,
) -> Tuple[str, Dict[str, Any]]:
    texts = getattr(response, "texts", None)
    if isinstance(texts, str):
        text = texts
    elif isinstance(texts, (list, tuple)) and len(texts) == 1:
        text = str(texts[0])
    else:
        actual = "none" if texts is None else (
            str(len(texts)) if hasattr(texts, "__len__") else type(texts).__name__
        )
        raise RuntimeError(f"batch_text_count_mismatch:expected=1:actual={actual}")

    stats = getattr(response, "stats", None)
    metrics: Dict[str, Any] = {}
    names = {
        "prompt_tokens": ("prompt_tokens", "prompt_token_count", "input_tokens"),
        "generated_tokens": (
            "generation_tokens", "generated_tokens", "output_tokens", "token_count"
        ),
        "active_memory_gb": ("active_memory_gb", "active_memory"),
        "cache_memory_gb": ("cache_memory_gb", "cache_memory"),
        "peak_memory_gb": ("peak_memory_gb", "peak_memory"),
        "raw_finish_reason": ("finish_reason",),
    }
    for target, candidates in names.items():
        value = _metric_value(stats, candidates)
        if value is None:
            value = _metric_value(response, candidates)
        if value is not None:
            metrics[target] = value

    tokenizer = getattr(processor, "tokenizer", None)
    if tokenizer is not None:
        if not metrics.get("prompt_tokens"):
            try:
                metrics["prompt_tokens"] = len(tokenizer.encode(prompt))
            except Exception:
                pass
        if not metrics.get("generated_tokens"):
            try:
                metrics["generated_tokens"] = len(tokenizer.encode(text))
            except Exception:
                pass

    for key in ("prompt_tokens", "generated_tokens"):
        try:
            metrics[key] = int(metrics.get(key) or 0)
        except Exception:
            metrics[key] = 0

    # mlx_vlm batch API often has no per-item finish reason.
    raw_finish = metrics.get("raw_finish_reason")
    if raw_finish in ("", "None", None):
        metrics["raw_finish_reason"] = None
    metrics["inferred_finish_reason"] = (
        "length" if metrics["generated_tokens"] >= max_tokens else "stop"
    )
    return text, metrics


def fallback_clean_text(raw_text: str) -> str:
    text = raw_text.strip()
    text = re.sub(r"^```(?:json|text|markdown)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def classify_text(
    raw_text: str,
    metrics: Mapping[str, Any],
    required_sections: Sequence[str],
    max_tokens: int,
) -> Dict[str, Any]:
    clean = fallback_clean_text(raw_text)
    missing = [section for section in required_sections if section and section not in clean]
    generated_tokens = int(metrics.get("generated_tokens") or 0)
    raw_finish = metrics.get("raw_finish_reason")
    inferred_finish = str(metrics.get("inferred_finish_reason") or "")
    repeated_char = bool(re.search(r"(.)\1{15,}", clean, flags=re.S))
    repeated_chunk = False
    if len(clean) >= 80:
        for size in (8, 12, 16, 24):
            chunks = [clean[i:i+size] for i in range(0, len(clean) - size + 1, size)]
            counts: Dict[str, int] = {}
            for chunk in chunks:
                if chunk.strip():
                    counts[chunk] = counts.get(chunk, 0) + 1
            if counts and max(counts.values()) >= 6:
                repeated_chunk = True
                break

    truncation = (
        generated_tokens >= max_tokens
        or str(raw_finish or "").lower() == "length"
        or inferred_finish == "length"
    )
    degeneration = repeated_char or repeated_chunk
    warnings: List[str] = []
    if repeated_char:
        warnings.append("repeated_character_run")
    if repeated_chunk:
        warnings.append("repeated_chunk_pattern")
    if truncation:
        warnings.append("generation_reached_max_tokens")
    if missing:
        warnings.append("missing_required_sections")

    if not clean:
        status = "failed"
    elif degeneration or truncation or missing:
        status = "review"
    else:
        status = "success"

    return {
        "result_status": status,
        "clean_text": clean,
        "clean_text_sha256": sha256_text(clean) if clean else "",
        "missing_required_sections": missing,
        "cleanup_status": "ok" if not warnings else "warning",
        "cleanup_warnings": "|".join(warnings),
        "degeneration_detected": degeneration,
        "truncation_detected": truncation,
    }


def mlx_memory() -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {
        "active_memory_gb": None,
        "cache_memory_gb": None,
        "peak_memory_gb": None,
    }
    try:
        import mlx.core as mx  # type: ignore
        for key, name in (
            ("active_memory_gb", "get_active_memory"),
            ("cache_memory_gb", "get_cache_memory"),
            ("peak_memory_gb", "get_peak_memory"),
        ):
            fn = getattr(mx, name, None)
            if callable(fn):
                result[key] = float(fn()) / (1024 ** 3)
    except Exception:
        pass
    return result


def alarm_handler(signum: int, frame: Any) -> None:
    raise TimeoutError("generation_timeout")


def worker_main(
    worker_id: int,
    task_queue: Any,
    event_queue: Any,
    stop_event: Any,
    model_path: str,
    prompt: str,
    required_sections: Sequence[str],
    max_tokens: int,
    timeout_per_item: int,
) -> None:
    set_offline_environment()
    install_network_guard()
    pid = os.getpid()
    event_queue.put({
        "type": "worker_state",
        "worker_id": worker_id,
        "pid": pid,
        "lifecycle": "loading_model",
        "timestamp": now_iso(),
    })
    load_started = time.monotonic()
    try:
        from mlx_vlm.generate import batch_generate, load  # type: ignore
        signature = inspect.signature(batch_generate)
        for required in ("images", "prompts", "max_tokens", "verbose", "group_by_shape"):
            if required not in signature.parameters:
                raise RuntimeError(f"batch_generate_missing_parameter:{required}:{signature}")
        model, processor = load(model_path)
    except Exception as exc:
        event_queue.put({
            "type": "fatal",
            "worker_id": worker_id,
            "pid": pid,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
            "timestamp": now_iso(),
        })
        return

    event_queue.put({
        "type": "worker_loaded",
        "worker_id": worker_id,
        "pid": pid,
        "model_load_count": 1,
        "processor_load_count": 1,
        "model_load_seconds": time.monotonic() - load_started,
        "timestamp": now_iso(),
    })

    old_handler = signal.signal(signal.SIGALRM, alarm_handler)
    try:
        while True:
            if stop_event.is_set():
                break
            try:
                task = task_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task is None:
                break

            candidate_id = str(task["candidate_id"])
            selected_order = int(task["selected_order"])
            attempt_number = int(task["attempt_number"])
            image_path = str(task["runtime_visual_file"])
            item_started = time.monotonic()

            event_queue.put({
                "type": "candidate_started",
                "worker_id": worker_id,
                "pid": pid,
                "candidate_id": candidate_id,
                "selected_order": selected_order,
                "attempt_number": attempt_number,
                "timestamp": now_iso(),
            })

            try:
                signal.setitimer(signal.ITIMER_REAL, float(timeout_per_item))
                try:
                    response = batch_generate(
                        model,
                        processor,
                        images=[image_path],
                        prompts=[prompt],
                        max_tokens=max_tokens,
                        verbose=False,
                        group_by_shape=False,
                    )
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0.0)

                raw_text, metrics = extract_batch_response(
                    response, processor, prompt, max_tokens
                )
                classification = classify_text(
                    raw_text, metrics, required_sections, max_tokens
                )
                memory = mlx_memory()
                event_queue.put({
                    "type": "candidate_result",
                    "worker_id": worker_id,
                    "pid": pid,
                    "candidate_id": candidate_id,
                    "selected_order": selected_order,
                    "attempt_number": attempt_number,
                    "status": classification["result_status"],
                    "raw_text": raw_text,
                    "classification": classification,
                    "metrics": metrics,
                    "memory": memory,
                    "elapsed_seconds": time.monotonic() - item_started,
                    "timestamp": now_iso(),
                })
            except Exception as exc:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                event_queue.put({
                    "type": "candidate_error",
                    "worker_id": worker_id,
                    "pid": pid,
                    "candidate_id": candidate_id,
                    "selected_order": selected_order,
                    "attempt_number": attempt_number,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "traceback": traceback.format_exc(),
                    "elapsed_seconds": time.monotonic() - item_started,
                    "timestamp": now_iso(),
                })
    finally:
        signal.signal(signal.SIGALRM, old_handler)
        event_queue.put({
            "type": "worker_stopped",
            "worker_id": worker_id,
            "pid": pid,
            "timestamp": now_iso(),
        })


def parse_ps(pid: int) -> Tuple[Optional[float], Optional[float]]:
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "%cpu=,rss="],
        capture_output=True,
        text=True,
        check=False,
    )
    text = completed.stdout.strip()
    if not text:
        return None, None
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]) / 1024.0
    except Exception:
        return None, None


def memory_pressure_and_swap() -> Tuple[Optional[float], Optional[int]]:
    pressure: Optional[float] = None
    swap_used: Optional[int] = None
    try:
        text = subprocess.check_output(
            ["/usr/bin/memory_pressure", "-Q"],
            text=True,
            stderr=subprocess.STDOUT,
        )
        match = re.search(r"free percentage:\s*(\d+)%", text, flags=re.I)
        if match:
            pressure = 100.0 - float(match.group(1))
    except Exception:
        pass
    try:
        text = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            text=True,
        )
        match = re.search(r"used = ([0-9.]+)([MG])", text)
        if match:
            value = float(match.group(1))
            swap_used = int(value * (1024 ** 2 if match.group(2) == "M" else 1024 ** 3))
    except Exception:
        pass
    return pressure, swap_used


def write_event(
    con: sqlite3.Connection,
    run_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    worker_id: Optional[int] = None,
    candidate_id: Optional[str] = None,
) -> None:
    con.execute(
        """
        INSERT INTO stop03_3g_event
        (run_id,timestamp,event_type,worker_id,candidate_id,payload_json)
        VALUES (?,?,?,?,?,?)
        """,
        (
            run_id, now_iso(), event_type, worker_id, candidate_id,
            json.dumps(dict(payload), ensure_ascii=False),
        ),
    )


def write_telemetry(
    con: sqlite3.Connection,
    run_id: str,
    workers: Sequence[mp.Process],
) -> None:
    rss_sum = 0.0
    active_workers = 0
    for index, process in enumerate(workers, 1):
        if process.is_alive():
            active_workers += 1
        cpu, rss = parse_ps(process.pid or 0)
        if rss is not None:
            rss_sum += rss
        con.execute(
            """
            UPDATE stop03_3g_worker
            SET cpu_percent=?, rss_mb=?, heartbeat_at=?
            WHERE run_id=? AND worker_id=?
            """,
            (cpu, rss, now_iso(), run_id, index),
        )

    counts = {
        str(row[0]): int(row[1])
        for row in con.execute(
            """
            SELECT status,COUNT(*) FROM stop03_3g_candidate
            WHERE run_id=? GROUP BY status
            """,
            (run_id,),
        )
    }
    pressure, swap = memory_pressure_and_swap()
    con.execute(
        """
        INSERT INTO stop03_3g_system_telemetry (
            run_id,timestamp,active_workers,completed_count,pending_count,
            running_count,success_count,review_count,failed_count,
            worker_rss_sum_mb,memory_pressure_percent,swap_used_bytes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, now_iso(), active_workers,
            counts.get("success", 0) + counts.get("review", 0) + counts.get("failed", 0),
            counts.get("pending", 0), counts.get("running", 0),
            counts.get("success", 0), counts.get("review", 0), counts.get("failed", 0),
            rss_sum, pressure, swap,
        ),
    )
    con.commit()


def write_result_files(
    run_dir: Path,
    candidate_id: str,
    raw_text: str,
    clean_text: str,
) -> Tuple[str, str]:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", candidate_id)[:120]
    raw_path = run_dir / "raw_outputs" / f"{safe}.txt"
    clean_path = run_dir / "clean_outputs" / f"{safe}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw_text, encoding="utf-8")
    clean_path.write_text(clean_text, encoding="utf-8")
    return str(raw_path), str(clean_path)


def create_report(
    con: sqlite3.Connection,
    run_id: str,
    run_dir: Path,
    source_db: Path,
    source_before: Mapping[str, Any],
    source_after: Mapping[str, Any],
    validation_db: Path,
    workers: Sequence[mp.Process],
    candidate_audit: Mapping[str, Any],
    model_fp: Mapping[str, Any],
    registry_hashes: Mapping[str, str],
    prompt_path: Path,
    prompt_sha: str,
    config_path: Path,
    config_sha: str,
    started_monotonic: float,
    failure_code: Optional[str],
    failure_message: Optional[str],
) -> Dict[str, Any]:
    status_counts = {
        str(row[0]): int(row[1])
        for row in con.execute(
            """
            SELECT result_status,COUNT(*) FROM stop03_3g_result
            WHERE run_id=? GROUP BY result_status
            """,
            (run_id,),
        )
    }
    candidate_counts = {
        str(row[0]): int(row[1])
        for row in con.execute(
            """
            SELECT status,COUNT(*) FROM stop03_3g_candidate
            WHERE run_id=? GROUP BY status
            """,
            (run_id,),
        )
    }
    worker_rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT * FROM stop03_3g_worker
            WHERE run_id=? ORDER BY worker_id
            """,
            (run_id,),
        )
    ]
    result_rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT * FROM stop03_3g_result
            WHERE run_id=? ORDER BY candidate_id
            """,
            (run_id,),
        )
    ]
    elapsed_values = [
        float(row["elapsed_seconds"])
        for row in result_rows
        if row["elapsed_seconds"] is not None
    ]
    token_values = [
        int(row["generated_tokens"])
        for row in result_rows
        if row["generated_tokens"] is not None
    ]
    duplicate_execution_keys = int(
        con.execute(
            """
            SELECT COUNT(*)-COUNT(DISTINCT execution_key)
            FROM stop03_3g_result WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()[0]
    )
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
    source_unchanged = source_before == source_after
    all_workers_loaded_once = all(
        int(row["model_load_count"] or 0) == 1
        and int(row["processor_load_count"] or 0) == 1
        for row in worker_rows
    )
    all_workers_exited_zero = all(
        (row["exit_code"] in (0, None)) for row in worker_rows
    )
    success_count = status_counts.get("success", 0)
    review_count = status_counts.get("review", 0)
    failed_count = status_counts.get("failed", 0)
    pending_count = candidate_counts.get("pending", 0)
    running_count = candidate_counts.get("running", 0)
    degeneration_count = sum(
        int(row["degeneration_detected"] or 0) for row in result_rows
    )
    truncation_count = sum(
        int(row["truncation_detected"] or 0) for row in result_rows
    )

    checks = {
        "candidate_count_336": candidate_audit.get("candidate_count") == 336,
        "success_count_336": success_count == 336,
        "review_count_zero": review_count == 0,
        "failed_count_zero": failed_count == 0,
        "pending_zero": pending_count == 0,
        "running_zero": running_count == 0,
        "workers_three": len(worker_rows) == 3,
        "each_worker_loaded_model_once": all_workers_loaded_once,
        "each_worker_completed_at_least_one": all(
            int(row["assigned_count"] or 0) > 0 for row in worker_rows
        ),
        "workers_exit_ok": all_workers_exited_zero,
        "degeneration_zero": degeneration_count == 0,
        "truncation_zero": truncation_count == 0,
        "duplicate_execution_keys_zero": duplicate_execution_keys == 0,
        "validation_db_integrity_ok": integrity == "ok",
        "validation_db_foreign_keys_zero": foreign == [],
        "source_db_unchanged": source_unchanged,
        "no_failure_code": failure_code is None,
    }
    status = "PASS" if all(checks.values()) else "FAIL"

    report = {
        "validation_status": status,
        "run_id": run_id,
        "runner_version": RUNNER_VERSION,
        "generation_backend": GENERATION_BACKEND,
        "execution_mode": "full_336_three_persistent_workers_dynamic_queue",
        "run_dir": str(run_dir),
        "source_db": str(source_db),
        "source_db_state_before": dict(source_before),
        "source_db_state_after": dict(source_after),
        "source_db_unchanged": source_unchanged,
        "validation_db": str(validation_db),
        "candidate_audit": dict(candidate_audit),
        "success_count": success_count,
        "review_count": review_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "running_count": running_count,
        "degeneration_count": degeneration_count,
        "truncation_count": truncation_count,
        "duplicate_execution_keys": duplicate_execution_keys,
        "persistent_worker_count": len(worker_rows),
        "dynamic_queue": True,
        "worker_model_load_count_one": all_workers_loaded_once,
        "workers": worker_rows,
        "generated_token_stats": {
            "count": len(token_values),
            "min": min(token_values) if token_values else None,
            "max": max(token_values) if token_values else None,
            "mean": statistics.mean(token_values) if token_values else None,
            "median": statistics.median(token_values) if token_values else None,
        },
        "elapsed_stats": {
            "count": len(elapsed_values),
            "min": min(elapsed_values) if elapsed_values else None,
            "max": max(elapsed_values) if elapsed_values else None,
            "mean": statistics.mean(elapsed_values) if elapsed_values else None,
            "median": statistics.median(elapsed_values) if elapsed_values else None,
            "wall_clock_seconds": time.monotonic() - started_monotonic,
        },
        "validation_db_integrity_check": integrity,
        "validation_db_foreign_key_check": foreign,
        "model_path": str(DEFAULT_MODEL),
        "model_fingerprint": dict(model_fp),
        "registry_hashes": dict(registry_hashes),
        "prompt_path": str(prompt_path),
        "prompt_sha256": prompt_sha,
        "config_path": str(config_path),
        "config_sha256": config_sha,
        "network_used": False,
        "downloads_performed": False,
        "original_media_modified": False,
        "central_database_modified": False,
        "failure_code": failure_code,
        "failure_message": failure_message,
        "checks": checks,
        "created_at": now_iso(),
    }

    con.execute(
        """
        UPDATE stop03_3g_run
        SET status=?,finished_at=?,source_db_sha256_after=?,
            source_db_unchanged=?,failure_code=?,failure_message=?
        WHERE run_id=?
        """,
        (
            status, now_iso(), source_after["sha256"], int(source_unchanged),
            failure_code, failure_message, run_id,
        ),
    )
    con.commit()
    json_dump(run_dir / "reports" / "final_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stop03-3G standalone full 336 Qwen3-VL database flow"
    )
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=384)
    parser.add_argument("--timeout-per-item", type=int, default=1200)
    parser.add_argument("--max-failed-retries", type=int, default=2)
    parser.add_argument("--telemetry-seconds", type=float, default=3.0)
    parser.add_argument("--resume-run", default="")
    parser.add_argument("--confirm-real-full336", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.confirm_real_full336:
        print("BLOCKED_CONFIRMATION_REQUIRED: add --confirm-real-full336")
        return 4
    if args.workers != 3:
        print("BLOCKED_WORKERS_MUST_EQUAL_3")
        return 4
    if args.max_tokens != 384:
        print("BLOCKED_MAX_TOKENS_MUST_EQUAL_VALIDATED_384")
        return 4
    if args.timeout_per_item <= 0 or args.max_failed_retries < 0:
        print("BLOCKED_INVALID_RUNTIME_ARGUMENT")
        return 4

    set_offline_environment()
    install_network_guard()
    mp.freeze_support()
    started_monotonic = time.monotonic()

    source_db = Path(args.source_db).expanduser().resolve(strict=True)
    config_path = Path(args.config).expanduser().resolve(strict=True)
    prompt_path = Path(args.prompt).expanduser().resolve(strict=True)
    model_path = Path(args.model).expanduser().resolve(strict=True)
    out_root = Path(args.out_root).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    registry_hashes = read_required_registry_files()
    config, prompt, required_sections = load_prompt_and_contract(config_path, prompt_path)
    config_sha = sha256_file(config_path)
    prompt_sha = sha256_file(prompt_path)
    source_before = file_state(source_db)

    if args.resume_run:
        run_dir = Path(args.resume_run).expanduser().resolve(strict=True)
        validation_db = run_dir / "database" / "media_archive_stop03_3g_full336.sqlite"
        if not validation_db.is_file():
            raise RuntimeError(f"resume_validation_db_missing:{validation_db}")
        con = open_validation_db(validation_db)
        row = con.execute(
            "SELECT * FROM stop03_3g_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("resume_run_record_missing")
        run_id = str(row["run_id"])
        if int(row["candidate_count"]) != 336 or int(row["workers"]) != 3:
            raise RuntimeError("resume_contract_mismatch")
        candidates = resume_run_database(con, run_id)
        candidate_audit = {
            "candidate_count": 336,
            "resume_pending_count": len(candidates),
        }
        model_fp = {
            "aggregate_sha256": str(row["model_fingerprint_sha256"]),
            "files": [],
        }
        print(f"[RESUME] run_id={run_id} pending={len(candidates)}", flush=True)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = out_root / f"stop03_3g_full336_standalone_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=False)
        for name in (
            "database", "reports", "raw_outputs", "clean_outputs",
            "logs", "run", "telemetry"
        ):
            (run_dir / name).mkdir(parents=True, exist_ok=True)

        LATEST_POINTER.parent.mkdir(parents=True, exist_ok=True)
        LATEST_POINTER.write_text(str(run_dir) + "\n", encoding="utf-8")
        validation_db = run_dir / "database" / "media_archive_stop03_3g_full336.sqlite"

        print("[DB] creating SQLite consistent validation copy", flush=True)
        backup_sqlite_readonly(source_db, validation_db)
        candidates, candidate_audit = load_candidates(validation_db)
        model_fp = model_fingerprint(model_path)

        for row in candidates:
            row["execution_key"] = execution_key(
                row, model_fp["aggregate_sha256"], prompt_sha,
                config_sha, args.max_tokens,
            )

        run_id = stable_id(
            "stop03_3g_", now_iso(), source_before["sha256"],
            model_fp["aggregate_sha256"], prompt_sha,
        )
        con = open_validation_db(validation_db)
        init_run_database(
            con, run_id, source_db, source_before, validation_db,
            candidates, args.workers, args.max_tokens, args.timeout_per_item,
            model_path, model_fp["aggregate_sha256"], prompt_path,
            prompt_sha, config_path, config_sha,
        )
        json_dump(run_dir / "run" / "candidate_audit.json", candidate_audit)
        json_dump(run_dir / "run" / "model_fingerprint.json", model_fp)
        json_dump(run_dir / "run" / "registry_hashes.json", registry_hashes)

    print("=" * 88)
    print("Stop03-3G standalone full 336")
    print(f"RUN_DIR={run_dir}")
    print(f"VALIDATION_DB={validation_db}")
    print("TOTAL=336")
    print("WORKERS=3")
    print("ASSIGNMENT=dynamic_queue")
    print("GENERATION_BACKEND=corrected batch_generate")
    print("MAX_TOKENS=384")
    print("CENTER_DB_WRITE=NO")
    print("=" * 88, flush=True)

    ctx = mp.get_context("spawn")
    task_queue = ctx.Queue()
    event_queue = ctx.Queue()
    stop_event = ctx.Event()

    workers: List[mp.Process] = []
    for worker_id in range(1, args.workers + 1):
        process = ctx.Process(
            target=worker_main,
            args=(
                worker_id, task_queue, event_queue, stop_event,
                str(model_path), prompt, required_sections,
                args.max_tokens, args.timeout_per_item,
            ),
            name=f"stop03_3g_worker_{worker_id}",
        )
        process.start()
        workers.append(process)

    for row in candidates:
        task_queue.put({
            "candidate_id": str(row["candidate_id"]),
            "selected_order": int(row["selected_order"]),
            "runtime_visual_file": str(row["runtime_visual_file"]),
            "attempt_number": int(row.get("current_attempt") or 0) + 1,
        })

    total_expected = 336
    terminal_count = int(
        con.execute(
            """
            SELECT COUNT(*) FROM stop03_3g_candidate
            WHERE run_id=? AND status IN ('success','review','failed')
            """,
            (run_id,),
        ).fetchone()[0]
    )
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    last_telemetry = 0.0
    retry_counts: Dict[str, int] = {}
    fatal = False

    try:
        while terminal_count < total_expected and not fatal:
            now = time.monotonic()
            if now - last_telemetry >= args.telemetry_seconds:
                write_telemetry(con, run_id, workers)
                last_telemetry = now

            try:
                event = event_queue.get(timeout=0.5)
            except queue.Empty:
                dead_nonzero = [
                    process for process in workers
                    if not process.is_alive() and process.exitcode not in (None, 0)
                ]
                if dead_nonzero:
                    fatal = True
                    failure_code = "WORKER_PROCESS_EXIT_NONZERO"
                    failure_message = ",".join(
                        f"{process.name}:{process.exitcode}" for process in dead_nonzero
                    )
                continue

            event_type = str(event.get("type"))
            worker_id = int(event.get("worker_id") or 0)
            candidate_id = str(event.get("candidate_id") or "") or None

            write_event(
                con, run_id, event_type, event,
                worker_id=worker_id or None,
                candidate_id=candidate_id,
            )

            if event_type == "worker_state":
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET pid=?,lifecycle=?,heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        event.get("pid"), event.get("lifecycle"),
                        event.get("timestamp"), run_id, worker_id,
                    ),
                )
                con.commit()

            elif event_type == "worker_loaded":
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET pid=?,lifecycle='idle',model_load_count=?,
                        processor_load_count=?,model_load_seconds=?,heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        event.get("pid"), event.get("model_load_count"),
                        event.get("processor_load_count"),
                        event.get("model_load_seconds"), event.get("timestamp"),
                        run_id, worker_id,
                    ),
                )
                con.commit()
                print(
                    f"[WORKER {worker_id}] model loaded once "
                    f"{float(event.get('model_load_seconds') or 0):.2f}s",
                    flush=True,
                )

            elif event_type == "candidate_started":
                attempt_number = int(event["attempt_number"])
                attempt_id = stable_id(
                    "attempt_", run_id, candidate_id, attempt_number
                )
                con.execute(
                    """
                    UPDATE stop03_3g_candidate
                    SET status='running',current_attempt=?,started_at=?,
                        assigned_worker_id=?
                    WHERE run_id=? AND candidate_id=?
                    """,
                    (
                        attempt_number, event.get("timestamp"), worker_id,
                        run_id, candidate_id,
                    ),
                )
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET lifecycle='generating',assigned_count=assigned_count+1,
                        current_candidate_id=?,current_selected_order=?,heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        candidate_id, event.get("selected_order"),
                        event.get("timestamp"), run_id, worker_id,
                    ),
                )
                con.execute(
                    """
                    INSERT OR REPLACE INTO stop03_3g_attempt (
                        attempt_id,run_id,candidate_id,worker_id,attempt_number,
                        started_at,status
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        attempt_id, run_id, candidate_id, worker_id,
                        attempt_number, event.get("timestamp"), "running",
                    ),
                )
                con.commit()

            elif event_type == "candidate_result":
                attempt_number = int(event["attempt_number"])
                attempt_id = stable_id(
                    "attempt_", run_id, candidate_id, attempt_number
                )
                status = str(event["status"])
                classification = dict(event["classification"])
                metrics = dict(event["metrics"])
                memory = dict(event.get("memory") or {})
                raw_text = str(event.get("raw_text") or "")
                clean_text = str(classification.get("clean_text") or "")
                elapsed = float(event.get("elapsed_seconds") or 0.0)
                raw_path, clean_path = write_result_files(
                    run_dir, candidate_id or "unknown", raw_text, clean_text
                )
                execution_key_value = str(
                    con.execute(
                        """
                        SELECT execution_key FROM stop03_3g_candidate
                        WHERE run_id=? AND candidate_id=?
                        """,
                        (run_id, candidate_id),
                    ).fetchone()[0]
                )
                con.execute(
                    """
                    UPDATE stop03_3g_attempt
                    SET finished_at=?,elapsed_seconds=?,status=?,
                        prompt_tokens=?,generated_tokens=?,raw_finish_reason=?,
                        inferred_finish_reason=?,degeneration_detected=?,
                        truncation_detected=?
                    WHERE attempt_id=?
                    """,
                    (
                        event.get("timestamp"), elapsed, status,
                        metrics.get("prompt_tokens"), metrics.get("generated_tokens"),
                        metrics.get("raw_finish_reason"),
                        metrics.get("inferred_finish_reason"),
                        int(bool(classification.get("degeneration_detected"))),
                        int(bool(classification.get("truncation_detected"))),
                        attempt_id,
                    ),
                )
                con.execute(
                    """
                    INSERT OR REPLACE INTO stop03_3g_result (
                        run_id,candidate_id,execution_key,attempt_id,worker_id,
                        result_status,raw_text,clean_text,clean_text_sha256,
                        raw_output_path,clean_output_path,prompt_tokens,
                        generated_tokens,raw_finish_reason,inferred_finish_reason,
                        degeneration_detected,truncation_detected,cleanup_status,
                        cleanup_warnings,missing_required_sections_json,
                        elapsed_seconds,active_memory_gb,cache_memory_gb,
                        peak_memory_gb,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, candidate_id, execution_key_value, attempt_id, worker_id,
                        status, raw_text, clean_text,
                        classification.get("clean_text_sha256"), raw_path, clean_path,
                        metrics.get("prompt_tokens"), metrics.get("generated_tokens"),
                        metrics.get("raw_finish_reason"),
                        metrics.get("inferred_finish_reason"),
                        int(bool(classification.get("degeneration_detected"))),
                        int(bool(classification.get("truncation_detected"))),
                        classification.get("cleanup_status"),
                        classification.get("cleanup_warnings"),
                        json.dumps(
                            classification.get("missing_required_sections") or [],
                            ensure_ascii=False,
                        ),
                        elapsed, memory.get("active_memory_gb"),
                        memory.get("cache_memory_gb"), memory.get("peak_memory_gb"),
                        event.get("timestamp"),
                    ),
                )
                con.execute(
                    """
                    UPDATE stop03_3g_candidate
                    SET status=?,finished_at=?
                    WHERE run_id=? AND candidate_id=?
                    """,
                    (status, event.get("timestamp"), run_id, candidate_id),
                )
                counter_col = {
                    "success": "success_count",
                    "review": "review_count",
                    "failed": "failed_count",
                }[status]
                con.execute(
                    f"""
                    UPDATE stop03_3g_worker
                    SET lifecycle='idle',current_candidate_id=NULL,
                        current_selected_order=NULL,{counter_col}={counter_col}+1,
                        active_memory_gb=?,cache_memory_gb=?,peak_memory_gb=?,
                        heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        memory.get("active_memory_gb"),
                        memory.get("cache_memory_gb"),
                        memory.get("peak_memory_gb"),
                        event.get("timestamp"), run_id, worker_id,
                    ),
                )
                con.commit()
                terminal_count += 1

                print(
                    f"[{terminal_count:03d}/336] worker={worker_id} "
                    f"order={int(event.get('selected_order') or 0):03d} "
                    f"status={status.upper()} "
                    f"tokens={int(metrics.get('generated_tokens') or 0)} "
                    f"elapsed={elapsed:.2f}s",
                    flush=True,
                )

                if bool(classification.get("degeneration_detected")):
                    fatal = True
                    failure_code = "CIRCUIT_BREAKER_DEGENERATION"
                    failure_message = (
                        f"candidate={candidate_id},worker={worker_id},"
                        f"order={event.get('selected_order')}"
                    )

            elif event_type == "candidate_error":
                attempt_number = int(event["attempt_number"])
                attempt_id = stable_id(
                    "attempt_", run_id, candidate_id, attempt_number
                )
                con.execute(
                    """
                    UPDATE stop03_3g_attempt
                    SET finished_at=?,elapsed_seconds=?,status='failed',
                        error_type=?,error_message=?,traceback_text=?
                    WHERE attempt_id=?
                    """,
                    (
                        event.get("timestamp"), event.get("elapsed_seconds"),
                        event.get("error_type"), event.get("error_message"),
                        event.get("traceback"), attempt_id,
                    ),
                )
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET lifecycle='idle',current_candidate_id=NULL,
                        current_selected_order=NULL,heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (event.get("timestamp"), run_id, worker_id),
                )
                con.commit()

                used_retries = retry_counts.get(candidate_id or "", 0)
                if used_retries < args.max_failed_retries:
                    retry_counts[candidate_id or ""] = used_retries + 1
                    task_queue.put({
                        "candidate_id": candidate_id,
                        "selected_order": int(event["selected_order"]),
                        "runtime_visual_file": str(
                            con.execute(
                                """
                                SELECT runtime_visual_file FROM stop03_3g_candidate
                                WHERE run_id=? AND candidate_id=?
                                """,
                                (run_id, candidate_id),
                            ).fetchone()[0]
                        ),
                        "attempt_number": attempt_number + 1,
                    })
                    con.execute(
                        """
                        UPDATE stop03_3g_candidate
                        SET status='pending',started_at=NULL,assigned_worker_id=NULL
                        WHERE run_id=? AND candidate_id=?
                        """,
                        (run_id, candidate_id),
                    )
                    con.commit()
                    print(
                        f"[RETRY] candidate={candidate_id} "
                        f"retry={used_retries + 1}/{args.max_failed_retries}",
                        flush=True,
                    )
                else:
                    con.execute(
                        """
                        UPDATE stop03_3g_candidate
                        SET status='failed',finished_at=?
                        WHERE run_id=? AND candidate_id=?
                        """,
                        (event.get("timestamp"), run_id, candidate_id),
                    )
                    con.execute(
                        """
                        UPDATE stop03_3g_worker
                        SET failed_count=failed_count+1
                        WHERE run_id=? AND worker_id=?
                        """,
                        (run_id, worker_id),
                    )
                    execution_key_value = str(
                        con.execute(
                            """
                            SELECT execution_key FROM stop03_3g_candidate
                            WHERE run_id=? AND candidate_id=?
                            """,
                            (run_id, candidate_id),
                        ).fetchone()[0]
                    )
                    con.execute(
                        """
                        INSERT OR REPLACE INTO stop03_3g_result (
                            run_id,candidate_id,execution_key,attempt_id,worker_id,
                            result_status,raw_text,clean_text,clean_text_sha256,
                            prompt_tokens,generated_tokens,raw_finish_reason,
                            inferred_finish_reason,degeneration_detected,
                            truncation_detected,cleanup_status,cleanup_warnings,
                            missing_required_sections_json,elapsed_seconds,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id, candidate_id, execution_key_value, attempt_id,
                            worker_id, "failed", "", "", "", 0, 0, None, "error",
                            0, 0, "failed", event.get("error_message"),
                            "[]", float(event.get("elapsed_seconds") or 0.0),
                            event.get("timestamp"),
                        ),
                    )
                    con.commit()
                    terminal_count += 1
                    print(
                        f"[{terminal_count:03d}/336] worker={worker_id} "
                        f"order={int(event.get('selected_order') or 0):03d} FAILED",
                        flush=True,
                    )

            elif event_type == "fatal":
                fatal = True
                failure_code = "WORKER_FATAL"
                failure_message = (
                    f"worker={worker_id}:{event.get('error_type')}:"
                    f"{event.get('error_message')}"
                )
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET lifecycle='fatal',error_message=?,heartbeat_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        failure_message, event.get("timestamp"),
                        run_id, worker_id,
                    ),
                )
                con.commit()

            elif event_type == "worker_stopped":
                con.execute(
                    """
                    UPDATE stop03_3g_worker
                    SET lifecycle='stopped',heartbeat_at=?,finished_at=?
                    WHERE run_id=? AND worker_id=?
                    """,
                    (
                        event.get("timestamp"), event.get("timestamp"),
                        run_id, worker_id,
                    ),
                )
                con.commit()

        if fatal:
            stop_event.set()
            con.execute(
                """
                UPDATE stop03_3g_run
                SET status='circuit_breaker',failure_code=?,failure_message=?
                WHERE run_id=?
                """,
                (failure_code, failure_message, run_id),
            )
            con.commit()
    except KeyboardInterrupt:
        failure_code = "INTERRUPTED_BY_USER"
        failure_message = "KeyboardInterrupt"
        stop_event.set()
        con.execute(
            """
            UPDATE stop03_3g_run
            SET status='interrupted',failure_code=?,failure_message=?
            WHERE run_id=?
            """,
            (failure_code, failure_message, run_id),
        )
        con.commit()
    finally:
        stop_event.set()
        for _ in workers:
            task_queue.put(None)
        for process in workers:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        for index, process in enumerate(workers, 1):
            con.execute(
                """
                UPDATE stop03_3g_worker
                SET exit_code=?,finished_at=COALESCE(finished_at,?),
                    lifecycle=CASE
                        WHEN ?=0 THEN 'finished'
                        ELSE 'exited_nonzero'
                    END
                WHERE run_id=? AND worker_id=?
                """,
                (
                    process.exitcode, now_iso(), process.exitcode,
                    run_id, index,
                ),
            )
        con.commit()
        write_telemetry(con, run_id, workers)

    source_after = file_state(source_db)
    report = create_report(
        con, run_id, run_dir, source_db, source_before, source_after,
        validation_db, workers, candidate_audit, model_fp,
        registry_hashes, prompt_path, prompt_sha,
        config_path, config_sha, started_monotonic,
        failure_code, failure_message,
    )
    con.close()

    print("=" * 88)
    print(f"VALIDATION_STATUS={report['validation_status']}")
    print(f"SUCCESS={report['success_count']}")
    print(f"REVIEW={report['review_count']}")
    print(f"FAILED={report['failed_count']}")
    print(f"PENDING={report['pending_count']}")
    print(f"RUNNING={report['running_count']}")
    print(f"SOURCE_DB_UNCHANGED={report['source_db_unchanged']}")
    print(f"VALIDATION_DB={validation_db}")
    print(f"FINAL_REPORT={run_dir / 'reports' / 'final_report.json'}")
    print("=" * 88)
    return 0 if report["validation_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
