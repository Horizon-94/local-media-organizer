#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-3E persistent Qwen-VL candidate.

Static candidate only. Importing this module never imports mlx/mlx_vlm and never
loads a model. The real-validation CLI is guarded by an explicit confirmation.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import multiprocessing as mp
import os
import queue
import sqlite3
import threading
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import qwenvl_output_contract_v2 as output_contract


SCRIPT_VERSION = "stop03_3e_qwenvl_persistent_runner_v1_20260711"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/qwenvl_prompt_v2_384.txt"
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit")
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03_3e_qwenvl_persistent_candidate_validation"
REQUIRED_SECTIONS = ("1）概括：", "2）元素：", "3）检索价值：")
TERMINAL_STATUSES = {"success", "review", "failed"}


class DeterministicInferenceError(RuntimeError):
    """A code/API/response error for which repeating unchanged work is unsafe."""


class ApiContractError(DeterministicInferenceError):
    pass


class InitializationError(DeterministicInferenceError):
    pass


class ResponseContractError(DeterministicInferenceError):
    pass


@dataclass
class RequestState:
    candidate_id: str
    image_path: str
    prompt: str
    messages: list[dict[str, Any]]
    image_container: list[str]
    generation_kwargs: dict[str, Any]
    cache: Any = None
    response: Any = None
    stats: Any = None


@dataclass
class ParsedResponse:
    text: str
    prompt_tokens: Optional[int]
    generation_tokens: Optional[int]
    peak_memory_gb: Optional[float]
    generation_tps: Optional[float]
    raw_finish_reason: Optional[str]
    inferred_finish_reason: str
    response_shape: str


@dataclass
class GenerationOutcome:
    candidate_id: str
    result_status: str
    clean_text: str
    prompt_tokens: Optional[int]
    generation_tokens: Optional[int]
    peak_memory_gb: Optional[float]
    generation_tps: Optional[float]
    raw_finish_reason: Optional[str]
    inferred_finish_reason: str
    truncation_status: str
    cleanup_status: str
    cleanup_warnings: str
    missing_required_sections: list[str]
    response_shape: str


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _value(source: Any, names: Sequence[str]) -> Any:
    if source is None:
        return None
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source.get(name)
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_generation_response(response: Any, *, max_tokens: int) -> ParsedResponse:
    if isinstance(response, str):
        text = response
        stats = None
        shape = "string"
    else:
        direct = _value(response, ("text", "response", "output_text"))
        texts = _value(response, ("texts",))
        if direct is not None:
            text = str(direct)
            shape = "object.text"
        elif isinstance(texts, (list, tuple)) and len(texts) == 1:
            text = str(texts[0])
            shape = "object.texts[1]"
        elif isinstance(texts, str):
            text = texts
            shape = "object.texts_string"
        elif texts is not None:
            raise ResponseContractError(
                f"response_text_count_mismatch:{len(texts) if hasattr(texts, '__len__') else type(texts).__name__}"
            )
        else:
            raise ResponseContractError(
                f"response_text_field_missing:{type(response).__name__}"
            )
        stats = _value(response, ("stats",))
    prompt_tokens = _optional_int(
        _value(response, ("prompt_tokens", "prompt_token_count", "input_tokens"))
        if not isinstance(response, str) else None
    )
    generation_tokens = _optional_int(
        _value(response, ("generation_tokens", "generated_tokens", "output_tokens", "token_count"))
        if not isinstance(response, str) else None
    )
    peak_memory = _optional_float(
        _value(response, ("peak_memory", "peak_memory_gb"))
        if not isinstance(response, str) else None
    )
    generation_tps = _optional_float(
        _value(response, ("generation_tps", "tokens_per_second"))
        if not isinstance(response, str) else None
    )
    raw_finish = (
        _value(response, ("finish_reason",)) if not isinstance(response, str) else None
    )
    if stats is not None:
        prompt_tokens = prompt_tokens if prompt_tokens is not None else _optional_int(
            _value(stats, ("prompt_tokens", "prompt_token_count", "input_tokens"))
        )
        generation_tokens = generation_tokens if generation_tokens is not None else _optional_int(
            _value(stats, ("generation_tokens", "generated_tokens", "output_tokens", "token_count"))
        )
        peak_memory = peak_memory if peak_memory is not None else _optional_float(
            _value(stats, ("peak_memory", "peak_memory_gb"))
        )
        generation_tps = generation_tps if generation_tps is not None else _optional_float(
            _value(stats, ("generation_tps", "tokens_per_second"))
        )
        raw_finish = raw_finish if raw_finish is not None else _value(stats, ("finish_reason",))
    raw_finish_text = str(raw_finish).lower() if raw_finish is not None else None
    if raw_finish_text:
        inferred = raw_finish_text
    elif generation_tokens is not None and generation_tokens >= max_tokens:
        inferred = "length"
    elif text.strip():
        inferred = "stop"
    else:
        inferred = "error"
    return ParsedResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        generation_tokens=generation_tokens,
        peak_memory_gb=peak_memory,
        generation_tps=generation_tps,
        raw_finish_reason=raw_finish_text,
        inferred_finish_reason=inferred,
        response_shape=shape,
    )


def classify_response(
    candidate_id: str, parsed: ParsedResponse, *, max_tokens: int,
    required_sections: Sequence[str] = REQUIRED_SECTIONS,
) -> GenerationOutcome:
    clean = output_contract.extract_clean_assistant_text(parsed.text).strip()
    metrics = {
        "prompt_tokens": parsed.prompt_tokens,
        "generation_tokens": parsed.generation_tokens,
        "peak_memory_gb": parsed.peak_memory_gb,
        "generation_tps": parsed.generation_tps,
    }
    issues = output_contract.detect_text_issues(clean, metrics, max_tokens=max_tokens)
    warnings = [item for item in str(issues.get("cleanup_warnings") or "").split("|") if item]
    missing = [section for section in required_sections if section not in clean]
    truncated = (
        parsed.inferred_finish_reason == "length"
        or (parsed.generation_tokens is not None and parsed.generation_tokens >= max_tokens)
        or "generation_reached_max_tokens" in warnings
        or "likely_truncated_by_sentence_tail" in warnings
    )
    if not clean:
        status = "failed"
    elif truncated or missing or str(issues.get("cleanup_status") or "ok") != "ok":
        status = "review"
    else:
        status = "success"
    return GenerationOutcome(
        candidate_id=candidate_id,
        result_status=status,
        clean_text=clean,
        prompt_tokens=parsed.prompt_tokens,
        generation_tokens=parsed.generation_tokens,
        peak_memory_gb=parsed.peak_memory_gb,
        generation_tps=parsed.generation_tps,
        raw_finish_reason=parsed.raw_finish_reason,
        inferred_finish_reason=parsed.inferred_finish_reason,
        truncation_status="truncated" if truncated else "not_truncated",
        cleanup_status=str(issues.get("cleanup_status") or ("ok" if clean else "failed")),
        cleanup_warnings="|".join(warnings),
        missing_required_sections=missing,
        response_shape=parsed.response_shape,
    )


class LocalMLXVLMBackend:
    """Lazy real backend. No mlx import occurs until load() in a worker."""

    def __init__(self) -> None:
        self.module: Any = None
        self.generate_fn: Any = None
        self.batch_generate_fn: Any = None

    def validate_api(self) -> None:
        try:
            import mlx_vlm
            from mlx_vlm.generate import BatchGenerator, batch_generate, generate, generate_step
        except Exception as exc:
            raise InitializationError(f"mlx_vlm_import_failed:{type(exc).__name__}:{exc}") from exc
        step_parameters = inspect.signature(generate_step).parameters
        missing = [name for name in ("max_tokens", "temperature", "top_p", "prompt_cache") if name not in step_parameters]
        if missing:
            raise ApiContractError("generate_step_parameters_missing:" + ",".join(missing))
        batch_parameters = inspect.signature(BatchGenerator).parameters
        if "sampler" not in batch_parameters or "temperature" in batch_parameters or "top_p" in batch_parameters:
            raise ApiContractError("batch_generator_sampler_contract_unexpected")
        if "kwargs" not in inspect.signature(batch_generate).parameters:
            raise ApiContractError("batch_generate_kwargs_contract_missing")
        self.module = mlx_vlm
        self.generate_fn = generate
        self.batch_generate_fn = batch_generate

    def load(self, model_path: Path) -> tuple[Any, Any]:
        if self.module is None:
            raise InitializationError("validate_api_must_run_before_load")
        return self.module.load(str(model_path), lazy=False, strict=True)

    @staticmethod
    def reset_request_state(model: Any, processor: Any) -> None:
        language_model = getattr(model, "language_model", None)
        if language_model is not None:
            if hasattr(language_model, "_position_ids"):
                language_model._position_ids = None
            if hasattr(language_model, "_rope_deltas"):
                language_model._rope_deltas = None
        detokenizer = getattr(processor, "detokenizer", None)
        if detokenizer is not None and hasattr(detokenizer, "reset"):
            detokenizer.reset()

    def format_prompt(self, model: Any, processor: Any, request: RequestState) -> str:
        if self.module is None:
            raise InitializationError("backend_not_validated")
        return str(self.module.apply_chat_template(
            processor, getattr(model, "config", None), request.prompt, num_images=1
        ))

    def generate(self, model: Any, processor: Any, formatted_prompt: str, request: RequestState) -> Any:
        if self.generate_fn is None:
            raise InitializationError("generate_api_unavailable")
        return self.generate_fn(
            model,
            processor,
            formatted_prompt,
            image=request.image_path,
            verbose=False,
            max_tokens=int(request.generation_kwargs["max_tokens"]),
            temperature=float(request.generation_kwargs["temperature"]),
            top_p=float(request.generation_kwargs["top_p"]),
        )

    def batch_generate_one(self, model: Any, processor: Any, image_path: str, prompt: str, max_tokens: int) -> Any:
        """Correct greedy batch API: omit unsupported temperature/top_p kwargs."""
        if self.batch_generate_fn is None:
            raise InitializationError("batch_generate_api_unavailable")
        return self.batch_generate_fn(
            model,
            processor,
            images=[image_path],
            prompts=[prompt],
            max_tokens=max_tokens,
            verbose=False,
            group_by_shape=False,
        )


class PersistentQwenGenerationAdapter:
    def __init__(
        self, *, model_path: Path, max_tokens: int = 384,
        temperature: float = 0.0, top_p: float = 1.0,
        backend: Optional[Any] = None,
        request_observer: Optional[Callable[[RequestState], None]] = None,
    ) -> None:
        self.model_path = model_path
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.backend = backend or LocalMLXVLMBackend()
        self.request_observer = request_observer
        self.model: Any = None
        self.processor: Any = None
        self.model_load_count = 0

    def load_once(self) -> None:
        if self.model_load_count:
            raise InitializationError("model_load_once_called_more_than_once")
        try:
            self.backend.validate_api()
            self.model, self.processor = self.backend.load(self.model_path)
        except DeterministicInferenceError:
            raise
        except Exception as exc:
            raise InitializationError(f"model_initialization_failed:{type(exc).__name__}:{exc}") from exc
        self.model_load_count = 1

    def generate_one(self, *, candidate_id: str, image_path: str, prompt: str) -> GenerationOutcome:
        if self.model_load_count != 1:
            raise InitializationError("model_not_loaded_exactly_once")
        request = RequestState(
            candidate_id=candidate_id,
            image_path=image_path,
            prompt=str(prompt),
            messages=[{
                "role": "user",
                "content": [{"type": "image"}, {"type": "text", "text": str(prompt)}],
            }],
            image_container=[str(image_path)],
            generation_kwargs={
                "max_tokens": int(self.max_tokens),
                "temperature": float(self.temperature),
                "top_p": float(self.top_p),
            },
        )
        if self.request_observer is not None:
            self.request_observer(request)
        try:
            self.backend.reset_request_state(self.model, self.processor)
            formatted_prompt = self.backend.format_prompt(self.model, self.processor, request)
            response = self.backend.generate(self.model, self.processor, formatted_prompt, request)
        except DeterministicInferenceError:
            raise
        except TypeError as exc:
            raise ApiContractError(f"generation_api_type_error:{exc}") from exc
        request.response = response
        parsed = parse_generation_response(response, max_tokens=self.max_tokens)
        request.stats = {
            "prompt_tokens": parsed.prompt_tokens,
            "generation_tokens": parsed.generation_tokens,
            "raw_finish_reason": parsed.raw_finish_reason,
        }
        outcome = classify_response(candidate_id, parsed, max_tokens=self.max_tokens)
        request.cache = None
        request.response = None
        request.stats = None
        return outcome


class Stop03EStateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def initialize(self, tasks: Sequence[Mapping[str, Any]]) -> None:
        ids = [str(task["candidate_id"]) for task in tasks]
        keys = [str(task["execution_key"]) for task in tasks]
        if len(ids) != len(set(ids)):
            raise RuntimeError("duplicate_candidate_id")
        if len(keys) != len(set(keys)):
            raise RuntimeError("duplicate_execution_key")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        try:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                candidate_id TEXT PRIMARY KEY,
                execution_key TEXT NOT NULL UNIQUE,
                image_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','success','review','failed')),
                worker_id INTEGER,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error_type TEXT,
                error_message TEXT,
                traceback_text TEXT
            );
            """)
            con.executemany(
                "INSERT INTO tasks(candidate_id,execution_key,image_path,status) VALUES(?,?,?,'pending')",
                [(str(task["candidate_id"]), str(task["execution_key"]), str(task["image_path"])) for task in tasks],
            )
            con.commit()
        finally:
            con.close()

    def recover_running(self) -> int:
        con = self.connect()
        try:
            cursor = con.execute("UPDATE tasks SET status='pending',worker_id=NULL WHERE status='running'")
            con.commit()
            return cursor.rowcount
        finally:
            con.close()

    def claim(self, candidate_id: str, worker_id: int) -> bool:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute(
                "UPDATE tasks SET status='running',worker_id=?,attempt_count=attempt_count+1 WHERE candidate_id=? AND status='pending'",
                (worker_id, candidate_id),
            )
            con.commit()
            return cursor.rowcount == 1
        finally:
            con.close()

    def complete(self, candidate_id: str, outcome: GenerationOutcome) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE tasks SET status=?,result_json=?,error_type=NULL,error_message=NULL,traceback_text=NULL WHERE candidate_id=?",
                (outcome.result_status, stable_json(asdict(outcome)), candidate_id),
            )
            con.commit()
        finally:
            con.close()

    def fail(self, candidate_id: str, exc: BaseException, traceback_text: str) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE tasks SET status='failed',error_type=?,error_message=?,traceback_text=? WHERE candidate_id=?",
                (type(exc).__name__, str(exc), traceback_text, candidate_id),
            )
            con.commit()
        finally:
            con.close()

    def counts(self) -> Dict[str, int]:
        con = self.connect()
        try:
            counts = Counter({str(row[0]): int(row[1]) for row in con.execute(
                "SELECT status,COUNT(*) FROM tasks GROUP BY status"
            )})
            counts["total"] = int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            return dict(counts)
        finally:
            con.close()

    def integrity(self) -> Dict[str, Any]:
        con = self.connect()
        try:
            return {
                "integrity_check": str(con.execute("PRAGMA integrity_check").fetchone()[0]),
                "foreign_key_check": [list(row) for row in con.execute("PRAGMA foreign_key_check")],
                "candidate_id_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT candidate_id) FROM tasks"
                ).fetchone()[0]),
                "execution_key_duplicate_count": int(con.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT execution_key) FROM tasks"
                ).fetchone()[0]),
            }
        finally:
            con.close()


def _worker_loop(
    worker_id: int,
    task_queue: Any,
    stop_event: Any,
    store_path: Path,
    adapter_factory: Callable[[int], PersistentQwenGenerationAdapter],
    prompt: str,
    worker_reports: list[dict[str, Any]],
    report_lock: Any,
) -> None:
    store = Stop03EStateStore(store_path)
    adapter: Optional[PersistentQwenGenerationAdapter] = None
    completed = 0
    failed = 0
    while not stop_event.is_set():
        try:
            task = task_queue.get_nowait()
        except queue.Empty:
            break
        if task is None:
            break
        candidate_id = str(task["candidate_id"])
        if not store.claim(candidate_id, worker_id):
            continue
        try:
            if adapter is None:
                adapter = adapter_factory(worker_id)
                adapter.load_once()
            outcome = adapter.generate_one(
                candidate_id=candidate_id,
                image_path=str(task["image_path"]),
                prompt=prompt,
            )
            store.complete(candidate_id, outcome)
            completed += 1
        except DeterministicInferenceError as exc:
            store.fail(candidate_id, exc, traceback.format_exc())
            failed += 1
            stop_event.set()
            break
        except Exception as exc:
            store.fail(candidate_id, exc, traceback.format_exc())
            failed += 1
    report = {
        "worker_id": worker_id,
        "model_load_count": adapter.model_load_count if adapter is not None else 0,
        "completed": completed,
        "failed": failed,
    }
    with report_lock:
        worker_reports.append(report)


def run_threaded_fake_scheduler(
    *, tasks: Sequence[Mapping[str, Any]], worker_count: int,
    adapter_factory: Callable[[int], PersistentQwenGenerationAdapter],
    prompt: str, db_path: Path,
) -> Dict[str, Any]:
    store = Stop03EStateStore(db_path)
    store.initialize(tasks)
    store.recover_running()
    task_queue: queue.Queue[Any] = queue.Queue()
    for task in tasks:
        task_queue.put(dict(task))
    stop_event = threading.Event()
    worker_reports: list[dict[str, Any]] = []
    report_lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_worker_loop,
            args=(worker_id, task_queue, stop_event, db_path, adapter_factory, prompt, worker_reports, report_lock),
            name=f"stop03e-worker-{worker_id}",
        )
        for worker_id in range(1, worker_count + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {
        "status": "FUSED" if stop_event.is_set() else "COMPLETE",
        "counts": store.counts(),
        "workers": sorted(worker_reports, key=lambda item: item["worker_id"]),
        "integrity": store.integrity(),
    }


def assert_test_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    return resolved


def load_v25_tasks_readonly(
    db: Path, *, limit: int, model_fingerprint_sha256: str,
    prompt_sha256: str, max_tokens: int,
) -> list[dict[str, Any]]:
    import stop03_3c_qwenvl_db_orchestrator_v1 as contract

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM v_stop03_2_v25_qwenvl_execution_queue ORDER BY candidate_id"
        )]
    finally:
        con.close()
    if limit > 0:
        rows = rows[:limit]
    tasks = []
    for row in rows:
        tasks.append({
            "candidate_id": row["candidate_id"],
            "execution_key": contract.execution_key(
                row, model_fingerprint_sha256, prompt_sha256,
                output_contract.CONTRACT_VERSION, max_tokens,
            ),
            "image_path": row["runtime_visual_file"],
            "input_sha256": row["runtime_visual_file_sha256"],
        })
    return tasks


def _real_worker_process(
    worker_id: int, task_queue: Any, stop_event: Any, store_path: str,
    model_path: str, max_tokens: int, prompt: str, report_queue: Any,
) -> None:
    reports: list[dict[str, Any]] = []
    lock = threading.Lock()
    factory = lambda _worker_id: PersistentQwenGenerationAdapter(
        model_path=Path(model_path), max_tokens=max_tokens,
        backend=LocalMLXVLMBackend(),
    )
    _worker_loop(
        worker_id, task_queue, stop_event, Path(store_path), factory,
        prompt, reports, lock,
    )
    report_queue.put(reports[0])


def run_real_validation(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.confirm_real_model_validation:
        raise RuntimeError("real_model_validation_requires_explicit_confirmation")
    import stop03_3c_qwenvl_db_orchestrator_v1 as contract

    config = contract.load_config(Path(args.config).resolve(strict=True))
    model_path = Path(args.model).resolve(strict=True)
    fingerprint = contract.model_fingerprint(model_path, config)
    prompt_path = Path(args.prompt).resolve(strict=True)
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    tasks = load_v25_tasks_readonly(
        Path(args.db).resolve(strict=True), limit=args.limit,
        model_fingerprint_sha256=fingerprint["model_fingerprint_sha256"],
        prompt_sha256=sha256_text(prompt), max_tokens=args.max_tokens,
    )
    out = assert_test_output_path(Path(args.out))
    out.mkdir(parents=True, exist_ok=False)
    state_db = out / "run/stop03_3e_state.sqlite"
    store = Stop03EStateStore(state_db)
    store.initialize(tasks)
    context = mp.get_context("spawn")
    task_queue = context.Queue()
    stop_event = context.Event()
    report_queue = context.Queue()
    for task in tasks:
        task_queue.put(task)
    processes = [
        context.Process(
            target=_real_worker_process,
            args=(worker_id, task_queue, stop_event, str(state_db), str(model_path), args.max_tokens, prompt, report_queue),
            name=f"stop03e-persistent-worker-{worker_id}",
        )
        for worker_id in range(1, args.workers + 1)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    reports = [report_queue.get() for _ in processes]
    return {
        "status": "STATIC_CANDIDATE_REAL_VALIDATION_RESULT",
        "counts": store.counts(),
        "workers": sorted(reports, key=lambda item: item["worker_id"]),
        "integrity": store.integrity(),
        "state_db": str(state_db),
        "network_used": False,
        "download_used": False,
        "central_db_modified": False,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop03-3E persistent Qwen-VL candidate")
    parser.add_argument("--mode", required=True, choices=("real-validate",))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=384)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--confirm-real-model-validation", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    })
    result = run_real_validation(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
