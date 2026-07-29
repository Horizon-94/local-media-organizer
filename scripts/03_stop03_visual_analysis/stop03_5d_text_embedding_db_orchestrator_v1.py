#!/usr/bin/env python3
"""Dynamic local-only Stop03-5D text embedding central-DB orchestrator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import queue
import resource
import shutil
import signal
import socket
import sqlite3
import struct
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as contract


ORCHESTRATOR_VERSION = "stop03_5d_text_embedding_db_orchestrator_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_CONFIG = (
    PROJECT_ROOT / "configs/stop03_5d_text_embedding_db_orchestrator_v1.json"
)
DEFAULT_OUT = contract.DEFAULT_OUTPUT_ROOT / "stop03_5d_text_embedding_db_full_v1"
TERMINAL_STATUSES = {"success"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(prefix: str, *parts: Any, size: int = 28) -> str:
    return prefix + contract.sha256_text("\x1f".join(str(part) for part in parts))[:size]


def writable_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def readonly_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_runtime_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "scheduling_mode": "dynamic_database_claim",
        "worker_model_load_policy": "once_per_worker",
        "claim_policy": "one_unique_text_at_a_time",
        "completion_policy": "database_counts_without_fixed_total",
        "retry_policy": "failed_below_max_attempts",
        "stale_running_resume_policy": "reset_to_pending",
        "successful_vector_rerun": False,
        "vector_storage": "central_sqlite_blob",
        "progress_policy": "append_after_each_completed_item",
        "network_policy": "offline_environment_and_blocked_socket",
        "source_policy": "central_db_text_only",
        "original_video_read": False,
        "search_index_created": False,
    }
    mismatches = {
        key: {"actual": value.get(key), "expected": expected_value}
        for key, expected_value in expected.items()
        if value.get(key) != expected_value
    }
    if mismatches:
        raise RuntimeError("stop03_5d_runtime_config_mismatch:" + canonical_json(mismatches))
    if int(value.get("default_workers", 0)) <= 0:
        raise RuntimeError("stop03_5d_runtime_workers_invalid")
    if int(value.get("default_max_attempts", 0)) <= 0:
        raise RuntimeError("stop03_5d_runtime_max_attempts_invalid")
    return value


def build_preflight(
    db: Path,
    contract_config_path: Path,
    runtime_config_path: Path,
    migration: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    runtime = load_runtime_config(runtime_config_path)
    summary, documents, jobs, excluded = contract.build_documents(db, contract_config_path)
    if summary["technical_status"] != "PASS":
        raise RuntimeError("stop03_5d_source_contract_not_pass")
    if not migration.is_file():
        raise RuntimeError(f"stop03_5d_migration_missing:{migration}")
    preflight = {
        **summary,
        "status": "PASS",
        "technical_status": "PASS",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "scheduling_mode": runtime["scheduling_mode"],
        "runtime_config_path": str(runtime_config_path),
        "runtime_config_sha256": contract.sha256_file(runtime_config_path),
        "migration_path": str(migration),
        "migration_sha256": contract.sha256_file(migration),
        "orchestrator_script_sha256": contract.sha256_file(Path(__file__).resolve()),
        "excluded_direct_evidence_count": len(excluded),
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
        "search_index_created": False,
    }
    return preflight, documents, jobs


def backup_database(db: Path, out: Path) -> Path:
    target = out / "backups" / (
        f"{db.stem}_before_stop03_5d_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    source = readonly_connection(db)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    if target.stat().st_size <= 0:
        raise RuntimeError("stop03_5d_backup_empty")
    return target


def apply_migration(db: Path, migration: Path) -> None:
    con = writable_connection(db)
    try:
        con.executescript(migration.read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()


def validate_migration_on_copy(db: Path, migration: Path, out: Path) -> dict[str, Any]:
    target = out / "dry_run" / "stop03_5d_schema_validation.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source = readonly_connection(db)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.executescript(migration.read_text(encoding="utf-8"))
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(destination.execute("PRAGMA foreign_key_check").fetchall())
        objects = int(
            destination.execute(
                """SELECT COUNT(*) FROM sqlite_master
                   WHERE name LIKE 'stop03_5d%'
                      OR name='v_stop03_5d_latest_text_documents'"""
            ).fetchone()[0]
        )
    finally:
        destination.close()
        source.close()
    return {
        "validation_db_path": str(target),
        "validation_db_size_bytes": target.stat().st_size,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "stop03_5d_object_count": objects,
    }


def create_run_and_queue(
    db: Path,
    preflight: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    *,
    workers: int,
    max_attempts: int,
) -> tuple[str, bool]:
    run_id = str(preflight["planned_embedding_run_id"])
    now = utc_now()
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            mismatches = {
                key: {"stored": existing[key], "current": value}
                for key, value in {
                    "contract_version": contract.CONTRACT_VERSION,
                    "source_staging_run_id": preflight["source_staging_run_id"],
                    "source_propagation_run_id": preflight["source_propagation_run_id"],
                    "run_payload_digest_sha256": preflight["run_payload_digest_sha256"],
                    "model_inventory_sha256": preflight["model_inventory_sha256"],
                    "model_config_sha256": preflight["model_config_sha256"],
                }.items()
                if existing[key] != value
            }
            if mismatches:
                raise RuntimeError("stop03_5d_existing_run_identity_mismatch:" + canonical_json(mismatches))
            if existing["status"] == "success":
                con.commit()
                return run_id, True
            raise RuntimeError(f"stop03_5d_run_exists_use_resume:{run_id}")

        con.execute(
            """INSERT INTO stop03_5d_text_embedding_runs(
               embedding_run_id,contract_version,source_staging_run_id,
               source_propagation_run_id,model_name,model_path,
               model_inventory_sha256,model_config_sha256,model_dimension,
               vector_dtype,normalize_embeddings,scheduling_mode,workers,max_attempts,
               document_count,unique_text_count,reused_document_count,
               direct_only_count,propagation_only_count,direct_and_propagation_count,
               pending_count,running_count,success_count,failed_count,
               document_id_set_sha256,text_job_id_set_sha256,
               document_payload_digest_sha256,run_payload_digest_sha256,
               policy_config_sha256,script_sha256,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?,'running',?)""",
            (
                run_id, contract.CONTRACT_VERSION,
                preflight["source_staging_run_id"], preflight["source_propagation_run_id"],
                preflight["model_name"], preflight["model_path"],
                preflight["model_inventory_sha256"], preflight["model_config_sha256"],
                int(preflight["model_dimension"]), preflight["vector_dtype"],
                int(bool(preflight["normalize_embeddings"])),
                "dynamic_database_claim", workers, max_attempts,
                len(documents), len(jobs), len(documents) - len(jobs),
                int(preflight["direct_only_count"]), int(preflight["propagation_only_count"]),
                int(preflight["direct_and_propagation_count"]), len(jobs),
                preflight["document_id_set_sha256"], preflight["text_job_id_set_sha256"],
                preflight["document_payload_digest_sha256"], preflight["run_payload_digest_sha256"],
                preflight["policy_config_sha256"], preflight["orchestrator_script_sha256"], now,
            ),
        )
        document_sql = """INSERT INTO stop03_5d_text_documents(
            embedding_run_id,document_id,contract_version,source_content_id,derived_id,
            canonical_visual_unit_id,media_type,derived_type,frame_index,time_position_ms,
            source_relative_path,document_kind,qwen_text,ocr_text,propagated_labels_json,
            embedding_text,embedding_text_sha256,source_evidence_ids_json,
            source_propagation_ids_json,direct_qwen_count,direct_ocr_count,
            propagation_row_count,quality_status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        for row in documents:
            con.execute(document_sql, (
                run_id, row["document_id"], row["contract_version"], row["source_content_id"],
                row["derived_id"], row["canonical_visual_unit_id"], row["media_type"],
                row["derived_type"], int(row["frame_index"]), int(row["time_position_ms"]),
                row["source_relative_path"], row["document_kind"], row["qwen_text"],
                row["ocr_text"], row["propagated_labels_json"], row["embedding_text"],
                row["embedding_text_sha256"], row["source_evidence_ids_json"],
                row["source_propagation_ids_json"], int(row["direct_qwen_count"]),
                int(row["direct_ocr_count"]), int(row["propagation_row_count"]),
                row["quality_status"], now,
            ))
        for row in jobs:
            con.execute(
                """INSERT INTO stop03_5d_text_vectors(
                   embedding_run_id,text_vector_id,execution_key,embedding_text_sha256,
                   model_inventory_sha256,model_config_sha256,model_dimension,
                   vector_dtype,normalized,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
                (run_id, row["text_vector_id"], row["execution_key"],
                 row["embedding_text_sha256"], preflight["model_inventory_sha256"],
                 preflight["model_config_sha256"], int(preflight["model_dimension"]),
                 preflight["vector_dtype"], int(bool(preflight["normalize_embeddings"])), now),
            )
        for row in documents:
            con.execute(
                "INSERT INTO stop03_5d_document_vector_links VALUES(?,?,?,?)",
                (run_id, row["document_id"], row["text_vector_id"], now),
            )
        con.commit()
        return run_id, False
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def prepare_resume(
    db: Path,
    run_id: str,
    preflight: Mapping[str, Any],
    *,
    workers: int,
    max_attempts: int,
) -> None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"stop03_5d_resume_run_missing:{run_id}")
        expected = {
            "contract_version": contract.CONTRACT_VERSION,
            "source_staging_run_id": preflight["source_staging_run_id"],
            "source_propagation_run_id": preflight["source_propagation_run_id"],
            "run_payload_digest_sha256": preflight["run_payload_digest_sha256"],
            "model_inventory_sha256": preflight["model_inventory_sha256"],
            "model_config_sha256": preflight["model_config_sha256"],
        }
        mismatches = {
            key: {"stored": run[key], "current": value}
            for key, value in expected.items() if run[key] != value
        }
        if mismatches:
            raise RuntimeError("stop03_5d_resume_identity_mismatch:" + canonical_json(mismatches))
        con.execute(
            """UPDATE stop03_5d_text_vectors
               SET status='pending',claimed_by_worker='',worker_pid=NULL,started_at=NULL
               WHERE embedding_run_id=? AND status='running'""", (run_id,)
        )
        con.execute(
            """UPDATE stop03_5d_text_embedding_runs
               SET status='running',workers=?,max_attempts=?,finished_at=NULL,error_message=''
               WHERE embedding_run_id=?""", (workers, max_attempts, run_id)
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    refresh_run_counts(db, run_id)


def claim_next_item(
    db: Path, run_id: str, worker_label: str, *, max_attempts: int
) -> Optional[dict[str, Any]]:
    """Atomically claim one unique text and release the write transaction."""
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT v.*,
               (SELECT d.embedding_text FROM stop03_5d_text_documents d
                WHERE d.embedding_run_id=v.embedding_run_id
                  AND d.embedding_text_sha256=v.embedding_text_sha256
                ORDER BY d.document_id LIMIT 1) AS embedding_text
               FROM stop03_5d_text_vectors v
               WHERE v.embedding_run_id=?
                 AND (v.status='pending' OR (v.status='failed' AND v.attempt_count<?))
                 AND v.vector_blob IS NULL
               ORDER BY CASE WHEN v.status='pending' THEN 0 ELSE 1 END,
                        v.attempt_count,v.text_vector_id LIMIT 1""",
            (run_id, max_attempts),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        cursor = con.execute(
            """UPDATE stop03_5d_text_vectors
               SET status='running',attempt_count=attempt_count+1,
                   claimed_by_worker=?,worker_pid=?,started_at=?,finished_at=NULL,
                   last_error_code='',last_error_message=''
               WHERE embedding_run_id=? AND text_vector_id=?
                 AND status=? AND attempt_count=?""",
            (worker_label, os.getpid(), utc_now(), run_id, row["text_vector_id"],
             row["status"], row["attempt_count"]),
        )
        if cursor.rowcount != 1:
            con.rollback()
            return None
        claimed = dict(row)
        claimed["attempt_count"] = int(row["attempt_count"]) + 1
        claimed["claimed_by_worker"] = worker_label
        con.commit()
        if not claimed.get("embedding_text"):
            raise RuntimeError(f"stop03_5d_claimed_text_missing:{claimed['text_vector_id']}")
        return claimed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def validate_vector(vector: Sequence[float], expected_dimension: int) -> tuple[bytes, float]:
    if len(vector) != expected_dimension:
        raise RuntimeError(f"stop03_5d_vector_dimension_mismatch:{len(vector)}!={expected_dimension}")
    if not all(math.isfinite(float(value)) for value in vector):
        raise RuntimeError("stop03_5d_vector_non_finite")
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if abs(norm - 1.0) > 0.001:
        raise RuntimeError(f"stop03_5d_vector_not_normalized:{norm}")
    payload = struct.pack(f"<{expected_dimension}f", *vector)
    return payload, norm


def persist_success(
    db: Path,
    item: Mapping[str, Any],
    vector: Sequence[float],
    *,
    elapsed_seconds: float,
    worker_pid: int,
) -> None:
    payload, _norm = validate_vector(vector, int(item["model_dimension"]))
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """UPDATE stop03_5d_text_vectors SET
               status='success',vector_blob=?,vector_byte_length=?,vector_sha256=?,
               claimed_by_worker='',worker_pid=?,elapsed_seconds=?,finished_at=?,
               last_error_code='',last_error_message=''
               WHERE embedding_run_id=? AND text_vector_id=? AND status='running'""",
            (payload, len(payload), sha256_bytes(payload), worker_pid, elapsed_seconds,
             utc_now(), item["embedding_run_id"], item["text_vector_id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"stop03_5d_success_write_lost_claim:{item['text_vector_id']}")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def persist_failure(
    db: Path,
    item: Mapping[str, Any],
    exc: BaseException,
    *,
    elapsed_seconds: float,
    worker_pid: int,
) -> None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_5d_text_vectors SET
               status='failed',claimed_by_worker='',worker_pid=?,elapsed_seconds=?,
               last_error_code=?,last_error_message=?,finished_at=?
               WHERE embedding_run_id=? AND text_vector_id=? AND status='running'""",
            (worker_pid, elapsed_seconds, type(exc).__name__, str(exc)[:4000], utc_now(),
             item["embedding_run_id"], item["text_vector_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def refresh_run_counts(db: Path, run_id: str) -> dict[str, int]:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        counts = Counter({
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_5d_text_vectors
                   WHERE embedding_run_id=? GROUP BY status""", (run_id,)
            )
        })
        con.execute(
            """UPDATE stop03_5d_text_embedding_runs SET
               pending_count=?,running_count=?,success_count=?,failed_count=?
               WHERE embedding_run_id=?""",
            (counts["pending"], counts["running"], counts["success"], counts["failed"], run_id),
        )
        con.commit()
        return {key: int(counts[key]) for key in ("pending", "running", "success", "failed")}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def read_progress(db: Path, run_id: str, *, max_attempts: int) -> dict[str, Any]:
    con = readonly_connection(db)
    try:
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"stop03_5d_run_missing:{run_id}")
        counts = Counter({
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_5d_text_vectors
                   WHERE embedding_run_id=? GROUP BY status""", (run_id,)
            )
        })
        retryable = int(con.execute(
            """SELECT COUNT(*) FROM stop03_5d_text_vectors
               WHERE embedding_run_id=? AND status='failed' AND attempt_count<?""",
            (run_id, max_attempts),
        ).fetchone()[0])
        last = con.execute(
            """SELECT text_vector_id,status,elapsed_seconds,finished_at
               FROM stop03_5d_text_vectors WHERE embedding_run_id=?
                 AND finished_at IS NOT NULL ORDER BY finished_at DESC,text_vector_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    finally:
        con.close()
    total = int(run["unique_text_count"])
    terminal_failed = counts["failed"] - retryable
    completed = counts["success"] + terminal_failed
    remaining = total - completed
    return {
        "timestamp": utc_now(), "run_id": run_id,
        "workers_requested": int(run["workers"]), "total": total,
        "pending": counts["pending"], "running": counts["running"],
        "success": counts["success"], "failed": counts["failed"],
        "retryable": retryable, "terminal_failed": terminal_failed,
        "remaining": remaining,
        "percent": round(100.0 * completed / total, 2) if total else 100.0,
        "last_completed_text_vector_id": last["text_vector_id"] if last else "",
        "last_completed_status": last["status"] if last else "",
        "last_completed_elapsed_seconds": last["elapsed_seconds"] if last else None,
    }


def print_progress(value: Mapping[str, Any]) -> None:
    print(
        " ".join([
            f"run_id={value['run_id']}", f"workers={value['workers_requested']}",
            f"pending={value['pending']}", f"running={value['running']}",
            f"success={value['success']}", f"failed={value['failed']}",
            f"remaining={value['remaining']}", f"percent={value['percent']:.2f}",
            f"last={value['last_completed_text_vector_id']}",
            f"elapsed={value['last_completed_elapsed_seconds']}",
        ]), flush=True,
    )


class LocalSentenceTransformerAdapter:
    def __init__(self, model_path: Path, device: str) -> None:
        self.model_path = model_path
        self.device_requested = device
        self.device_effective = device
        self.model: Any = None
        self.model_load_count = 0

    def load_once(self) -> None:
        if self.model is not None:
            return
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import torch
        from sentence_transformers import SentenceTransformer
        if self.device_requested == "auto":
            self.device_effective = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = SentenceTransformer(
            str(self.model_path), device=self.device_effective,
            local_files_only=True, trust_remote_code=False,
        )
        self.model_load_count += 1

    def encode(self, text: str) -> list[float]:
        if self.model is None:
            raise RuntimeError("stop03_5d_model_not_loaded")
        value = self.model.encode(
            [text], batch_size=1, show_progress_bar=False, precision="float32",
            convert_to_numpy=True, normalize_embeddings=True,
        )[0]
        return value.astype("float32", copy=False).tolist()


def block_worker_network() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    original_connect = socket.socket.connect

    def blocked_connect(self: socket.socket, address: Any) -> Any:
        raise RuntimeError(f"stop03_5d_network_blocked:{address}")

    socket.socket.connect = blocked_connect  # type: ignore[assignment]


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def execute_dynamic_worker(
    *, worker_id: int, db: Path, out: Path, run_id: str,
    model_path: Path, device: str, max_attempts: int,
    stop_event: Any, report_queue: Any, adapter: Any = None,
) -> None:
    label = f"worker_{worker_id}"
    adapter = adapter or LocalSentenceTransformerAdapter(model_path, device)
    state: dict[str, Any] = {
        "worker_id": worker_id, "pid": os.getpid(), "lifecycle": "loading",
        "model_load_count": 0, "completed_attempts": 0,
        "successful_attempts": 0, "failed_attempts": 0,
        "current_text_vector_id": None, "average_seconds": None,
        "peak_rss_bytes": 0, "device": device,
    }
    write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", state)
    elapsed_total = 0.0
    try:
        block_worker_network()
        adapter.load_once()
        state.update({
            "lifecycle": "running", "model_load_count": adapter.model_load_count,
            "device": getattr(adapter, "device_effective", device),
        })
        write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", state)
        while not stop_event.is_set():
            item = claim_next_item(db, run_id, label, max_attempts=max_attempts)
            if item is None:
                break
            state["current_text_vector_id"] = item["text_vector_id"]
            state["current_attempt"] = item["attempt_count"]
            write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", state)
            started = time.monotonic()
            status = "success"
            error = ""
            try:
                text = str(item["embedding_text"])
                if contract.sha256_text(text) != item["embedding_text_sha256"]:
                    raise RuntimeError("stop03_5d_claimed_text_sha_mismatch")
                vector = adapter.encode(text)
                elapsed = time.monotonic() - started
                persist_success(db, item, vector, elapsed_seconds=elapsed, worker_pid=os.getpid())
                state["successful_attempts"] += 1
            except BaseException as exc:
                elapsed = time.monotonic() - started
                status = "failed"
                error = f"{type(exc).__name__}:{exc}"
                persist_failure(db, item, exc, elapsed_seconds=elapsed, worker_pid=os.getpid())
                state["failed_attempts"] += 1
            state["completed_attempts"] += 1
            elapsed_total += elapsed
            state.update({
                "current_text_vector_id": None, "current_attempt": None,
                "average_seconds": elapsed_total / state["completed_attempts"],
                "last_completed": {
                    "text_vector_id": item["text_vector_id"], "status": status,
                    "elapsed_seconds": elapsed, "attempt_count": item["attempt_count"],
                    "error": error, "finished_at": utc_now(),
                },
                "peak_rss_bytes": peak_rss_bytes(),
            })
            write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", state)
            report_queue.put({"event": "completed", "worker_id": worker_id,
                              "text_vector_id": item["text_vector_id"], "status": status})
        state["lifecycle"] = "completed"
    except BaseException as exc:
        state["lifecycle"] = "failed"
        state["error_message"] = repr(exc)
        state["traceback"] = traceback.format_exc()
    finally:
        state["current_text_vector_id"] = None
        state["peak_rss_bytes"] = peak_rss_bytes()
        write_json_atomic(out / "worker_status" / f"worker_{worker_id}.json", state)
        report_queue.put({"event": "worker_finished", "report": state})


def _worker_entry(
    worker_id: int, db: str, out: str, run_id: str, model_path: str,
    device: str, max_attempts: int, stop_event: Any, report_queue: Any,
) -> None:
    execute_dynamic_worker(
        worker_id=worker_id, db=Path(db), out=Path(out), run_id=run_id,
        model_path=Path(model_path), device=device, max_attempts=max_attempts,
        stop_event=stop_event, report_queue=report_queue,
    )


def reset_running_items(db: Path, run_id: str) -> None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """UPDATE stop03_5d_text_vectors SET status='pending',claimed_by_worker='',
               worker_pid=NULL,started_at=NULL WHERE embedding_run_id=? AND status='running'""",
            (run_id,),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def finalize_run(db: Path, run_id: str, *, max_attempts: int, interrupted: bool) -> dict[str, Any]:
    counts = refresh_run_counts(db, run_id)
    progress = read_progress(db, run_id, max_attempts=max_attempts)
    if not interrupted and progress["remaining"] == 0 and counts["failed"] == 0:
        status = "success"
    elif counts["pending"] or counts["running"] or progress["retryable"]:
        status = "running"
    else:
        status = "failed"
    con = writable_connection(db)
    try:
        con.execute(
            """UPDATE stop03_5d_text_embedding_runs SET status=?,finished_at=?,error_message=?
               WHERE embedding_run_id=?""",
            (status, utc_now() if status != "running" else None,
             "interrupted" if interrupted else "", run_id),
        )
        con.commit()
    finally:
        con.close()
    return readback_run(db, run_id, max_attempts=max_attempts)


def readback_run(db: Path, run_id: str, *, max_attempts: int = 3) -> dict[str, Any]:
    con = readonly_connection(db)
    try:
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"stop03_5d_readback_run_missing:{run_id}")
        vector_rows = list(con.execute(
            """SELECT text_vector_id,execution_key,status,attempt_count,vector_blob,
               vector_byte_length,vector_sha256,model_dimension,elapsed_seconds
               FROM stop03_5d_text_vectors WHERE embedding_run_id=?""", (run_id,)
        ))
        document_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_5d_text_documents WHERE embedding_run_id=?", (run_id,)
        ).fetchone()[0])
        link_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_5d_document_vector_links WHERE embedding_run_id=?", (run_id,)
        ).fetchone()[0])
        duplicate_keys = int(con.execute(
            """SELECT COUNT(*) FROM (SELECT execution_key FROM stop03_5d_text_vectors
               GROUP BY embedding_run_id,execution_key HAVING COUNT(*)>1)"""
        ).fetchone()[0])
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        con.close()
    counts = Counter(str(row["status"]) for row in vector_rows)
    successful = [row for row in vector_rows if row["status"] == "success"]
    blobs_valid = all(
        row["vector_blob"] is not None
        and int(row["vector_byte_length"] or 0) == len(row["vector_blob"])
        and len(row["vector_blob"]) == int(row["model_dimension"]) * 4
        and sha256_bytes(row["vector_blob"]) == row["vector_sha256"]
        for row in successful
    )
    passed = (
        run["status"] == "success"
        and counts["success"] == int(run["unique_text_count"])
        and not counts["pending"] and not counts["running"] and not counts["failed"]
        and document_count == int(run["document_count"])
        and link_count == document_count and blobs_valid
        and duplicate_keys == 0 and integrity == "ok" and foreign_keys == 0
    )
    return {
        "status": "PASS" if passed else "PARTIAL_OR_FAILED",
        "technical_status": "PASS" if passed else "REVIEW",
        "run_id": run_id, "run_status": run["status"],
        "scheduling_mode": run["scheduling_mode"], "workers": int(run["workers"]),
        "document_count": document_count, "unique_text_count": len(vector_rows),
        "success_count": counts["success"], "pending_count": counts["pending"],
        "running_count": counts["running"], "failed_count": counts["failed"],
        "link_count": link_count, "vector_blobs_valid": blobs_valid,
        "execution_key_duplicates": duplicate_keys,
        "database_integrity_check": integrity, "foreign_key_error_count": foreign_keys,
        "original_video_read": False, "network_used": False, "download_used": False,
        "search_index_created": False,
    }


def run_workers(
    *, db: Path, out: Path, run_id: str, model_path: Path,
    device: str, max_attempts: int, workers: int,
) -> dict[str, Any]:
    context = mp.get_context("spawn")
    stop_event = context.Event()
    report_queue = context.Queue()
    processes = [
        context.Process(
            target=_worker_entry,
            args=(worker_id, str(db), str(out), run_id, str(model_path),
                  device, max_attempts, stop_event, report_queue),
            name=f"stop03-5d-embedding-worker-{worker_id}",
        ) for worker_id in range(1, workers + 1)
    ]
    interrupted = False
    worker_reports: list[dict[str, Any]] = []
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_stop)

    def handle_event(event: Mapping[str, Any]) -> None:
        if event.get("event") == "completed":
            refresh_run_counts(db, run_id)
            progress = read_progress(db, run_id, max_attempts=max_attempts)
            append_jsonl(out / "logs" / "progress.jsonl", progress)
            print_progress(progress)
        elif event.get("event") == "worker_finished":
            worker_reports.append(dict(event["report"]))

    try:
        for process in processes:
            process.start()
        try:
            while any(process.is_alive() for process in processes):
                try:
                    event = report_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                handle_event(event)
            for process in processes:
                process.join()
        except KeyboardInterrupt:
            interrupted = True
            stop_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=10)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    while True:
        try:
            event = report_queue.get_nowait()
        except queue.Empty:
            break
        handle_event(event)
    if interrupted:
        reset_running_items(db, run_id)
    result = finalize_run(db, run_id, max_attempts=max_attempts, interrupted=interrupted)
    result.update({
        "interrupted": interrupted,
        "worker_reports": sorted(worker_reports, key=lambda row: int(row["worker_id"])),
        "worker_exit_codes": {process.name: process.exitcode for process in processes},
        "workers_requested": workers,
        "workers_effective": sum(
            int(row.get("model_load_count", 0)) == 1 for row in worker_reports
        ),
    })
    write_json_atomic(out / "reports" / "stop03_5d_full_summary.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "dry-run", "run", "resume", "readback"), required=True)
    parser.add_argument("--db", type=Path, default=contract.DEFAULT_DB)
    parser.add_argument("--contract-config", type=Path, default=contract.DEFAULT_CONFIG)
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--migration", type=Path, default=contract.DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--run-id")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers <= 0 or args.max_attempts <= 0:
        raise RuntimeError("stop03_5d_workers_or_attempts_invalid")
    if args.mode == "readback":
        if not args.run_id:
            raise RuntimeError("stop03_5d_readback_run_id_required")
        result = readback_run(args.db, args.run_id, max_attempts=args.max_attempts)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 2
    preflight, documents, jobs = build_preflight(
        args.db, args.contract_config, args.runtime_config, args.migration
    )
    preflight.update({"workers_requested": args.workers, "max_attempts": args.max_attempts})
    if args.mode == "preflight":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "dry-run":
        db_before = contract.sha256_file(args.db)
        validation = validate_migration_on_copy(args.db, args.migration, args.out)
        db_after = contract.sha256_file(args.db)
        result = {
            **preflight, "status": "PASS", "technical_status": "PASS",
            "dry_run_schema_validation": validation,
            "central_db_sha256_before": db_before, "central_db_sha256_after": db_after,
            "central_db_unchanged": db_before == db_after,
            "database_write": False, "model_run": False,
        }
        write_json_atomic(args.out / "reports" / "stop03_5d_dry_run_summary.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["central_db_unchanged"] else 2
    if not args.confirm_central_db_write:
        raise RuntimeError("stop03_5d_central_db_write_confirmation_required")
    if args.mode == "run":
        backup = backup_database(args.db, args.out)
        apply_migration(args.db, args.migration)
        run_id, reused = create_run_and_queue(
            args.db, preflight, documents, jobs,
            workers=args.workers, max_attempts=args.max_attempts,
        )
        write_text_atomic(args.out / "run_id.txt", run_id + "\n")
        if reused:
            result = readback_run(args.db, run_id, max_attempts=args.max_attempts)
            result["idempotent_existing_success"] = True
            result["backup_path"] = str(backup)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "PASS" else 2
    else:
        if not args.run_id:
            raise RuntimeError("stop03_5d_resume_run_id_required")
        run_id = args.run_id
        prepare_resume(
            args.db, run_id, preflight,
            workers=args.workers, max_attempts=args.max_attempts,
        )
        write_text_atomic(args.out / "run_id.txt", run_id + "\n")
    result = run_workers(
        db=args.db, out=args.out, run_id=run_id,
        model_path=Path(str(preflight["model_path"])), device=args.device,
        max_attempts=args.max_attempts, workers=args.workers,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
