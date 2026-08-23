#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3F corrected batch_generate diagnostic.

This module is safe to import: mlx/mlx_vlm are imported only by the explicitly
confirmed real diagnostic. The central SQLite database is always opened read-only.
All runtime writes are restricted to a new directory below test-output.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import qwenvl_output_contract_v2 as output_contract


SCRIPT_VERSION = "stop03_3f_qwenvl_batch75_diagnostic_v1_20260711"
PROJECT_ROOT = Path("$APP_RESOURCES/Pipeline")
TEST_OUTPUT_ROOT = Path("$USER_HOME/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("$MODEL_ROOT/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03_3f_qwenvl_batch75_diagnostic"
DEFAULT_LIMIT = 75
DEFAULT_MAX_TOKENS = 384
REQUIRED_SECTIONS = ("1）概括：", "2）元素：", "3）检索价值：")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


class DeterministicDiagnosticError(RuntimeError):
    pass


class BatchAPIContractError(DeterministicDiagnosticError):
    pass


class BatchResponseContractError(DeterministicDiagnosticError):
    pass


class BatchInitializationError(DeterministicDiagnosticError):
    pass


@dataclass
class ParsedBatchResponse:
    text: str
    prompt_tokens: Optional[int]
    generation_tokens: Optional[int]
    generation_tps: Optional[float]
    peak_memory_gb: Optional[float]
    raw_finish_reason: Optional[str]
    inferred_finish_reason: str
    response_shape: str


@dataclass
class DiagnosticOutcome:
    candidate_id: str
    result_status: str
    clean_text: str
    prompt_tokens: Optional[int]
    generation_tokens: Optional[int]
    generation_tps: Optional[float]
    peak_memory_gb: Optional[float]
    raw_finish_reason: Optional[str]
    inferred_finish_reason: str
    truncation_status: str
    cleanup_status: str
    cleanup_warnings: str
    missing_required_sections: list[str]
    response_shape: str
    degenerate_reason: Optional[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_value(source: Any, names: Sequence[str]) -> Any:
    if source is None:
        return None
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source.get(name)
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def parse_batch_response(response: Any, *, max_tokens: int) -> ParsedBatchResponse:
    texts = get_value(response, ("texts",))
    if not isinstance(texts, (list, tuple)):
        raise BatchResponseContractError(
            f"batch_response_texts_missing:{type(response).__name__}"
        )
    if len(texts) != 1:
        raise BatchResponseContractError(f"batch_response_text_count:{len(texts)}")
    text = str(texts[0])
    stats = get_value(response, ("stats",))
    prompt_tokens = optional_int(get_value(stats, ("prompt_tokens", "prompt_token_count")))
    generation_tokens = optional_int(
        get_value(stats, ("generation_tokens", "generated_tokens", "output_tokens"))
    )
    generation_tps = optional_float(
        get_value(stats, ("generation_tps", "tokens_per_second"))
    )
    peak_memory = optional_float(get_value(stats, ("peak_memory", "peak_memory_gb")))
    raw_finish = get_value(response, ("finish_reason",))
    raw_finish_text = str(raw_finish).lower() if raw_finish is not None else None
    if raw_finish_text:
        inferred = raw_finish_text
    elif generation_tokens is not None and generation_tokens >= max_tokens:
        inferred = "length"
    elif text.strip():
        inferred = "stop"
    else:
        inferred = "error"
    return ParsedBatchResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        generation_tokens=generation_tokens,
        generation_tps=generation_tps,
        peak_memory_gb=peak_memory,
        raw_finish_reason=raw_finish_text,
        inferred_finish_reason=inferred,
        response_shape="BatchResponse.texts[1]",
    )


def detect_degenerate_text(text: str) -> Optional[str]:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return None
    if len(compact) >= 64 and set(compact) == {"!"}:
        return "bang_only_repetition"
    if len(compact) >= 64 and len(set(compact)) == 1:
        char = next(iter(set(compact)))
        return f"single_character_repetition:U+{ord(char):04X}"
    return None


def classify_batch_response(
    candidate_id: str, parsed: ParsedBatchResponse, *, max_tokens: int,
) -> DiagnosticOutcome:
    clean = output_contract.extract_clean_assistant_text(parsed.text).strip()
    metrics = {
        "prompt_tokens": parsed.prompt_tokens,
        "generation_tokens": parsed.generation_tokens,
        "generation_tps": parsed.generation_tps,
        "peak_memory_gb": parsed.peak_memory_gb,
    }
    issues = output_contract.detect_text_issues(clean, metrics, max_tokens=max_tokens)
    warnings = [part for part in str(issues.get("cleanup_warnings") or "").split("|") if part]
    missing = [section for section in REQUIRED_SECTIONS if section not in clean]
    degenerate = detect_degenerate_text(clean)
    truncated = (
        parsed.inferred_finish_reason == "length"
        or (parsed.generation_tokens is not None and parsed.generation_tokens >= max_tokens)
        or "generation_reached_max_tokens" in warnings
        or "likely_truncated_by_sentence_tail" in warnings
    )
    if not clean:
        status = "failed"
    elif degenerate or truncated or missing or str(issues.get("cleanup_status") or "ok") != "ok":
        status = "review"
    else:
        status = "success"
    return DiagnosticOutcome(
        candidate_id=candidate_id,
        result_status=status,
        clean_text=clean,
        prompt_tokens=parsed.prompt_tokens,
        generation_tokens=parsed.generation_tokens,
        generation_tps=parsed.generation_tps,
        peak_memory_gb=parsed.peak_memory_gb,
        raw_finish_reason=parsed.raw_finish_reason,
        inferred_finish_reason=parsed.inferred_finish_reason,
        truncation_status="truncated" if truncated else "not_truncated",
        cleanup_status=str(issues.get("cleanup_status") or ("ok" if clean else "failed")),
        cleanup_warnings="|".join(warnings),
        missing_required_sections=missing,
        response_shape=parsed.response_shape,
        degenerate_reason=degenerate,
    )


def describe_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "NoneType", "is_none": True}
    result: dict[str, Any] = {
        "type": f"{type(value).__module__}.{type(value).__name__}",
        "object_id": id(value),
        "is_none": False,
    }
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    if shape is not None:
        try:
            result["shape"] = list(shape)
        except TypeError:
            result["shape"] = str(shape)
    if dtype is not None:
        result["dtype"] = str(dtype)
    if isinstance(value, (str, int, float, bool)):
        result["value"] = value
    elif isinstance(value, (list, tuple, set, dict)):
        result["length"] = len(value)
    return result


def shallow_object_state(value: Any) -> dict[str, Any]:
    if value is None:
        return describe_value(value)
    result = describe_value(value)
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        result["attributes"] = {
            str(key): describe_value(item) for key, item in sorted(state.items())
            if not str(key).startswith("__")
        }
    return result


class LocalCorrectedBatchBackend:
    """Lazy backend for the real diagnostic; no model import on module import."""

    def __init__(self) -> None:
        self.mlx_vlm: Any = None
        self.mx: Any = None
        self.batch_generate_fn: Any = None

    def validate_api(self) -> dict[str, Any]:
        try:
            import mlx.core as mx
            import mlx_vlm
            from mlx_vlm.generate import BatchGenerator, batch_generate
        except Exception as exc:
            raise BatchInitializationError(
                f"local_batch_import_failed:{type(exc).__name__}:{exc}"
            ) from exc
        batch_signature = inspect.signature(batch_generate)
        generator_signature = inspect.signature(BatchGenerator)
        generator_parameters = generator_signature.parameters
        if "sampler" not in generator_parameters:
            raise BatchAPIContractError("BatchGenerator_sampler_missing")
        forbidden = [name for name in ("temperature", "top_p") if name in generator_parameters]
        if forbidden:
            raise BatchAPIContractError("BatchGenerator_unexpected_sampling_parameters:" + ",".join(forbidden))
        if "kwargs" not in batch_signature.parameters:
            raise BatchAPIContractError("batch_generate_kwargs_missing")
        self.mlx_vlm = mlx_vlm
        self.mx = mx
        self.batch_generate_fn = batch_generate
        return {
            "batch_generate_signature": str(batch_signature),
            "batch_generator_signature": str(generator_signature),
            "sampling_contract": "greedy_sampler_default;temperature_and_top_p_omitted",
        }

    def load(self, model_path: Path) -> tuple[Any, Any]:
        if self.mlx_vlm is None:
            raise BatchInitializationError("validate_api_required_before_load")
        return self.mlx_vlm.load(str(model_path), lazy=False, strict=True)

    def generate_one(
        self, model: Any, processor: Any, *, image_path: str, prompt: str,
        max_tokens: int,
    ) -> Any:
        if self.batch_generate_fn is None:
            raise BatchInitializationError("batch_generate_unavailable")
        # Deliberately no temperature/top_p/**user kwargs. This exact call is the
        # Stop03-3F A/B subject under test.
        return self.batch_generate_fn(
            model,
            processor,
            images=[image_path],
            prompts=[prompt],
            max_tokens=max_tokens,
            verbose=False,
            group_by_shape=False,
            track_image_sizes=True,
        )

    def snapshot(self, model: Any, processor: Any) -> dict[str, Any]:
        language_model = getattr(model, "language_model", None)
        tokenizer = getattr(processor, "tokenizer", processor)
        stopping = getattr(tokenizer, "stopping_criteria", None)
        detokenizer = getattr(processor, "detokenizer", None)
        memory: dict[str, Any] = {}
        if self.mx is not None:
            for name in ("get_active_memory", "get_cache_memory", "get_peak_memory"):
                function = getattr(self.mx, name, None)
                if callable(function):
                    try:
                        memory[name] = int(function())
                    except Exception as exc:
                        memory[name] = f"ERROR:{type(exc).__name__}:{exc}"
        return {
            "model_object_id": id(model),
            "processor_object_id": id(processor),
            "language_model_object_id": id(language_model) if language_model is not None else None,
            "position_ids": describe_value(getattr(language_model, "_position_ids", None)),
            "rope_deltas": describe_value(getattr(language_model, "_rope_deltas", None)),
            "tokenizer": shallow_object_state(tokenizer),
            "stopping_criteria": shallow_object_state(stopping),
            "detokenizer": shallow_object_state(detokenizer),
            "mlx_memory_bytes": memory,
        }


class PersistentCorrectedBatchAdapter:
    def __init__(
        self, *, model_path: Path, max_tokens: int,
        backend: Optional[Any] = None,
    ) -> None:
        self.model_path = model_path
        self.max_tokens = max_tokens
        self.backend = backend or LocalCorrectedBatchBackend()
        self.model: Any = None
        self.processor: Any = None
        self.model_load_count = 0
        self.api_contract: dict[str, Any] = {}

    def load_once(self) -> None:
        if self.model_load_count:
            raise BatchInitializationError("model_load_once_called_more_than_once")
        try:
            self.api_contract = dict(self.backend.validate_api())
            self.model, self.processor = self.backend.load(self.model_path)
        except DeterministicDiagnosticError:
            raise
        except Exception as exc:
            raise BatchInitializationError(
                f"model_initialization_failed:{type(exc).__name__}:{exc}"
            ) from exc
        self.model_load_count = 1

    def snapshot(self) -> dict[str, Any]:
        if self.model_load_count != 1:
            raise BatchInitializationError("model_not_loaded_exactly_once")
        return dict(self.backend.snapshot(self.model, self.processor))

    def generate_one(self, *, candidate_id: str, image_path: str, prompt: str) -> DiagnosticOutcome:
        if self.model_load_count != 1:
            raise BatchInitializationError("model_not_loaded_exactly_once")
        try:
            response = self.backend.generate_one(
                self.model,
                self.processor,
                image_path=str(image_path),
                prompt=str(prompt),
                max_tokens=self.max_tokens,
            )
        except DeterministicDiagnosticError:
            raise
        except TypeError as exc:
            raise BatchAPIContractError(f"batch_generate_type_error:{exc}") from exc
        parsed = parse_batch_response(response, max_tokens=self.max_tokens)
        return classify_batch_response(candidate_id, parsed, max_tokens=self.max_tokens)


class DiagnosticStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def initialize(self, tasks: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE items (
                seq INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE,
                execution_key TEXT NOT NULL UNIQUE,
                image_path TEXT NOT NULL,
                input_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending','running','success','review','failed')),
                started_at TEXT,
                finished_at TEXT,
                elapsed_seconds REAL,
                prompt_tokens INTEGER,
                generation_tokens INTEGER,
                generation_tps REAL,
                peak_memory_gb REAL,
                raw_finish_reason TEXT,
                inferred_finish_reason TEXT,
                response_shape TEXT,
                truncation_status TEXT,
                cleanup_status TEXT,
                cleanup_warnings TEXT,
                missing_required_sections_json TEXT,
                degenerate_reason TEXT,
                clean_text TEXT,
                clean_text_sha256 TEXT,
                error_type TEXT,
                error_message TEXT,
                traceback_text TEXT
            );
            CREATE TABLE state_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(seq) REFERENCES items(seq)
            );
            """)
            con.executemany(
                "INSERT INTO metadata(key,value_json) VALUES(?,?)",
                [(str(key), stable_json(value)) for key, value in metadata.items()],
            )
            con.executemany(
                "INSERT INTO items(seq,candidate_id,execution_key,image_path,input_sha256,status) "
                "VALUES(?,?,?,?,?,'pending')",
                [
                    (
                        index,
                        str(task["candidate_id"]),
                        str(task["execution_key"]),
                        str(task["image_path"]),
                        str(task.get("input_sha256") or ""),
                    )
                    for index, task in enumerate(tasks, start=1)
                ],
            )
            con.commit()
        finally:
            con.close()

    def set_metadata(self, key: str, value: Any) -> None:
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO metadata(key,value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, stable_json(value)),
            )
            con.commit()
        finally:
            con.close()

    def start(self, seq: int) -> None:
        con = self.connect()
        try:
            cursor = con.execute(
                "UPDATE items SET status='running',started_at=? WHERE seq=? AND status='pending'",
                (now_iso(), seq),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"diagnostic_item_claim_failed:{seq}")
            con.commit()
        finally:
            con.close()

    def snapshot(self, seq: int, phase: str, payload: Mapping[str, Any]) -> None:
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO state_snapshots(seq,phase,created_at,payload_json) VALUES(?,?,?,?)",
                (seq, phase, now_iso(), stable_json(payload)),
            )
            con.commit()
        finally:
            con.close()

    def complete(self, seq: int, outcome: DiagnosticOutcome, elapsed: float) -> None:
        con = self.connect()
        try:
            con.execute(
                """UPDATE items SET status=?,finished_at=?,elapsed_seconds=?,
                prompt_tokens=?,generation_tokens=?,generation_tps=?,peak_memory_gb=?,
                raw_finish_reason=?,inferred_finish_reason=?,response_shape=?,
                truncation_status=?,cleanup_status=?,cleanup_warnings=?,
                missing_required_sections_json=?,degenerate_reason=?,clean_text=?,
                clean_text_sha256=?,error_type=NULL,error_message=NULL,traceback_text=NULL
                WHERE seq=?""",
                (
                    outcome.result_status,
                    now_iso(), elapsed,
                    outcome.prompt_tokens, outcome.generation_tokens,
                    outcome.generation_tps, outcome.peak_memory_gb,
                    outcome.raw_finish_reason, outcome.inferred_finish_reason,
                    outcome.response_shape, outcome.truncation_status,
                    outcome.cleanup_status, outcome.cleanup_warnings,
                    stable_json(outcome.missing_required_sections),
                    outcome.degenerate_reason, outcome.clean_text,
                    sha256_text(outcome.clean_text) if outcome.clean_text else None,
                    seq,
                ),
            )
            con.commit()
        finally:
            con.close()

    def fail(self, seq: int, exc: BaseException, elapsed: float) -> None:
        con = self.connect()
        try:
            con.execute(
                """UPDATE items SET status='failed',finished_at=?,elapsed_seconds=?,
                error_type=?,error_message=?,traceback_text=? WHERE seq=?""",
                (now_iso(), elapsed, type(exc).__name__, str(exc), traceback.format_exc(), seq),
            )
            con.commit()
        finally:
            con.close()

    def recover_running(self) -> int:
        con = self.connect()
        try:
            cursor = con.execute(
                "UPDATE items SET status='pending',started_at=NULL WHERE status='running'"
            )
            con.commit()
            return cursor.rowcount
        finally:
            con.close()

    def summary(self) -> dict[str, Any]:
        con = self.connect()
        try:
            counts = {
                str(row[0]): int(row[1])
                for row in con.execute("SELECT status,COUNT(*) FROM items GROUP BY status")
            }
            total = int(con.execute("SELECT COUNT(*) FROM items").fetchone()[0])
            first_degenerate = con.execute(
                "SELECT seq,candidate_id,degenerate_reason,generation_tokens FROM items "
                "WHERE degenerate_reason IS NOT NULL ORDER BY seq LIMIT 1"
            ).fetchone()
            timing = {
                str(row[0]): {
                    "count": int(row[1]),
                    "average_seconds": float(row[2]) if row[2] is not None else None,
                    "minimum_seconds": float(row[3]) if row[3] is not None else None,
                    "maximum_seconds": float(row[4]) if row[4] is not None else None,
                }
                for row in con.execute(
                    "SELECT status,COUNT(*),AVG(elapsed_seconds),MIN(elapsed_seconds),MAX(elapsed_seconds) "
                    "FROM items WHERE status IN ('success','review','failed') GROUP BY status"
                )
            }
            return {
                "counts": {**counts, "total": total},
                "first_degenerate": dict(first_degenerate) if first_degenerate else None,
                "timing_by_status": timing,
                "integrity_check": str(con.execute("PRAGMA integrity_check").fetchone()[0]),
                "foreign_key_check": [list(row) for row in con.execute("PRAGMA foreign_key_check")],
                "candidate_id_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT candidate_id) FROM items"
                ).fetchone()[0]),
                "execution_key_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM items"
                ).fetchone()[0]),
                "snapshot_count": int(con.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]),
            }
        finally:
            con.close()


def assert_test_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    return resolved


def load_tasks_readonly(
    db_path: Path, *, limit: int, prompt_sha256: str, max_tokens: int,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        rows = [dict(row) for row in con.execute(
            "SELECT candidate_id,runtime_visual_file,runtime_visual_file_sha256 "
            "FROM v_stop03_2_v25_qwenvl_execution_queue ORDER BY candidate_id LIMIT ?",
            (limit,),
        )]
    finally:
        con.close()
    if len(rows) != limit:
        raise RuntimeError(f"diagnostic_input_count_mismatch:{len(rows)}:{limit}")
    tasks: list[dict[str, Any]] = []
    for row in rows:
        image_path = Path(str(row["runtime_visual_file"]))
        if image_path.suffix.lower() not in IMAGE_SUFFIXES:
            raise RuntimeError(f"non_image_runtime_input:{row['candidate_id']}:{image_path}")
        if not image_path.is_file():
            raise RuntimeError(f"derived_frame_missing:{row['candidate_id']}:{image_path}")
        key_payload = {
            "candidate_id": row["candidate_id"],
            "input_sha256": row["runtime_visual_file_sha256"],
            "prompt_sha256": prompt_sha256,
            "max_tokens": max_tokens,
            "backend": "mlx_vlm.batch_generate.batch_size_1.corrected",
            "script_version": SCRIPT_VERSION,
        }
        tasks.append({
            "candidate_id": str(row["candidate_id"]),
            "execution_key": sha256_text(stable_json(key_payload)),
            "image_path": str(image_path),
            "input_sha256": str(row["runtime_visual_file_sha256"]),
        })
    return tasks


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_diagnostic(
    *, tasks: Sequence[Mapping[str, Any]], prompt: str, output_dir: Path,
    model_path: Path, max_tokens: int, backend: Optional[Any] = None,
    stop_on_degenerate: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    state_db = output_dir / "run/stop03_3f_state.sqlite"
    store = DiagnosticStore(state_db)
    store.initialize(tasks, {
        "script_version": SCRIPT_VERSION,
        "status": "INITIALIZED",
        "started_at": now_iso(),
        "pid": os.getpid(),
        "limit": len(tasks),
        "max_tokens": max_tokens,
        "worker_count": 1,
        "batch_size": 1,
        "model_path": str(model_path),
        "prompt_sha256": sha256_text(prompt),
        "central_db_modified": False,
        "network_used": False,
        "download_used": False,
    })
    adapter = PersistentCorrectedBatchAdapter(
        model_path=model_path, max_tokens=max_tokens, backend=backend,
    )
    fuse_reason: Optional[str] = None
    fatal_error: Optional[str] = None
    try:
        adapter.load_once()
        store.set_metadata("model_load_count", adapter.model_load_count)
        store.set_metadata("api_contract", adapter.api_contract)
        store.set_metadata("status", "RUNNING")
        for seq, task in enumerate(tasks, start=1):
            candidate_id = str(task["candidate_id"])
            store.start(seq)
            started = time.monotonic()
            try:
                store.snapshot(seq, "before", adapter.snapshot())
                outcome = adapter.generate_one(
                    candidate_id=candidate_id,
                    image_path=str(task["image_path"]),
                    prompt=prompt,
                )
                elapsed = time.monotonic() - started
                store.snapshot(seq, "after", adapter.snapshot())
                store.complete(seq, outcome, elapsed)
                print(
                    f"[PROGRESS] seq={seq}/{len(tasks)} candidate_id={candidate_id} "
                    f"status={outcome.result_status} tokens={outcome.generation_tokens} "
                    f"finish_raw={outcome.raw_finish_reason} "
                    f"finish_inferred={outcome.inferred_finish_reason} "
                    f"degenerate={outcome.degenerate_reason} elapsed_seconds={elapsed:.3f}",
                    flush=True,
                )
                if outcome.degenerate_reason and stop_on_degenerate:
                    fuse_reason = f"degenerate_output_at_seq_{seq}:{outcome.degenerate_reason}"
                    break
            except DeterministicDiagnosticError as exc:
                elapsed = time.monotonic() - started
                store.fail(seq, exc, elapsed)
                fuse_reason = f"deterministic_error_at_seq_{seq}:{type(exc).__name__}:{exc}"
                print(f"[FUSE] {fuse_reason}", flush=True)
                break
            except Exception as exc:
                elapsed = time.monotonic() - started
                store.fail(seq, exc, elapsed)
                fatal_error = f"unexpected_error_at_seq_{seq}:{type(exc).__name__}:{exc}"
                print(f"[FATAL] {fatal_error}", flush=True)
                break
    except Exception as exc:
        fatal_error = f"initialization_error:{type(exc).__name__}:{exc}"
        store.set_metadata("initialization_traceback", traceback.format_exc())
        print(f"[FATAL] {fatal_error}", flush=True)
    except KeyboardInterrupt:
        fatal_error = "keyboard_interrupt"
        store.recover_running()
        print("[INTERRUPTED] running item returned to pending", flush=True)

    summary = store.summary()
    counts = summary["counts"]
    completed = int(counts.get("success", 0)) + int(counts.get("review", 0)) + int(counts.get("failed", 0))
    if summary["first_degenerate"]:
        status = "BATCH_PATH_DEGENERATE_REPRODUCED"
    elif fuse_reason:
        status = "BATCH_PATH_DETERMINISTIC_ERROR_FUSED"
    elif fatal_error:
        status = "BATCH_PATH_DIAGNOSTIC_FAILED"
    elif completed == len(tasks) and not summary["first_degenerate"]:
        status = "BATCH_PATH_75_PASS_PENDING_THREE_WORKER_VALIDATION"
    else:
        status = "BATCH_PATH_DIAGNOSTIC_INCOMPLETE"
    store.set_metadata("status", status)
    store.set_metadata("finished_at", now_iso())
    store.set_metadata("fuse_reason", fuse_reason)
    store.set_metadata("fatal_error", fatal_error)
    report = {
        "status": status,
        "script_version": SCRIPT_VERSION,
        "model_load_count": adapter.model_load_count,
        "api_contract": adapter.api_contract,
        "fuse_reason": fuse_reason,
        "fatal_error": fatal_error,
        "summary": summary,
        "state_db": str(state_db),
        "central_db_modified": False,
        "network_used": False,
        "download_used": False,
    }
    report_path = output_dir / "reports/final_report.json"
    write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-3F corrected batch_generate 75-item diagnostic")
    parser.add_argument("--mode", required=True, choices=("real-diagnostic",))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--continue-after-degenerate", action="store_true")
    parser.add_argument("--confirm-real-model-diagnostic", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm_real_model_diagnostic:
        raise RuntimeError("real_model_diagnostic_requires_explicit_confirmation")
    if args.limit != DEFAULT_LIMIT:
        raise RuntimeError(f"stop03_3f_limit_must_be_{DEFAULT_LIMIT}")
    if args.max_tokens != DEFAULT_MAX_TOKENS:
        raise RuntimeError(f"stop03_3f_max_tokens_must_be_{DEFAULT_MAX_TOKENS}")
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    })
    output_dir = assert_test_output_path(Path(args.out))
    db_path = Path(args.db).resolve(strict=True)
    prompt_path = Path(args.prompt).resolve(strict=True)
    model_path = Path(args.model).resolve(strict=True)
    config_path = Path(args.config).resolve(strict=True)
    # The formal config is read only to verify the registered model and locked settings.
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if Path(str(config["model_path"])).resolve(strict=True) != model_path:
        raise RuntimeError("diagnostic_model_does_not_match_formal_config")
    if int(config["default_max_tokens"]) != args.max_tokens:
        raise RuntimeError("diagnostic_max_tokens_does_not_match_formal_config")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    tasks = load_tasks_readonly(
        db_path, limit=args.limit, prompt_sha256=sha256_text(prompt),
        max_tokens=args.max_tokens,
    )
    report = run_diagnostic(
        tasks=tasks,
        prompt=prompt,
        output_dir=output_dir,
        model_path=model_path,
        max_tokens=args.max_tokens,
        backend=LocalCorrectedBatchBackend(),
        stop_on_degenerate=not args.continue_after_degenerate,
    )
    if report["status"] == "BATCH_PATH_75_PASS_PENDING_THREE_WORKER_VALIDATION":
        return 0
    if report["status"] == "BATCH_PATH_DEGENERATE_REPRODUCED":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
