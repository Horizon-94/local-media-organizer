#!/usr/bin/env python3
"""Stop03-4 OCR dynamic central-database orchestrator.

The production path reads only v_stop03_2_v25_ocr_execution_queue and derived
visual files.  It never reads original video.  OCR workers use explicit local
PaddleOCR model directories and block socket networking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


CONTRACT_VERSION = "stop03_4_ocr_db_v1"
RESULT_CONTRACT = "stop03_4_ocr_evidence_v1"
TERMINAL_OK = {"success", "no_text"}
TERMINAL_ALL = TERMINAL_OK | {"input_fingerprint_mismatch", "failed", "review"}
REQUIRED_QUEUE_COLUMNS = {
    "candidate_id",
    "source_content_id",
    "visual_unit_id",
    "canonical_visual_unit_id",
    "derived_id",
    "candidate_role",
    "reason_codes",
    "policy_version",
    "media_type",
    "time_position_ms",
    "runtime_visual_file",
    "runtime_visual_file_sha256",
}
_OCR_ENGINE: Any = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any, size: int = 28) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return prefix + sha256_text(payload)[:size]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def readonly_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def writable_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def object_exists(con: sqlite3.Connection, name: str, kind: str | None = None) -> bool:
    if kind:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type=?", (name, kind)
        ).fetchone()
    else:
        row = con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row is not None


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("ocr_config_contract_version_mismatch")
    if data.get("queue_view") != "v_stop03_2_v25_ocr_execution_queue":
        raise RuntimeError("ocr_queue_view_mismatch")
    if data.get("source_policy") != "derived_visual_only":
        raise RuntimeError("ocr_source_policy_mismatch")
    if data.get("network_policy") != "blocked_in_worker":
        raise RuntimeError("ocr_network_policy_mismatch")
    for key in ("text_detection_model_dir", "text_recognition_model_dir"):
        if not Path(str(data.get(key, ""))).is_dir():
            raise RuntimeError(f"missing_local_model_dir:{key}")
    return data


def directory_fingerprint(path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        rows.append(
            {
                "relative_path": file_path.relative_to(path).as_posix(),
                "size_bytes": file_path.stat().st_size,
                "sha256": sha256_file(file_path),
            }
        )
    if not rows:
        raise RuntimeError(f"empty_local_model_dir:{path}")
    inventory_json = canonical_json(rows)
    return {
        "path": str(path),
        "file_count": len(rows),
        "inventory": rows,
        "inventory_sha256": sha256_text(inventory_json),
    }


def build_preflight(
    db: Path,
    config_path: Path,
    script_path: Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    detection = directory_fingerprint(Path(config["text_detection_model_dir"]))
    recognition = directory_fingerprint(Path(config["text_recognition_model_dir"]))
    model_fingerprint = sha256_text(
        canonical_json(
            {
                "detection": detection["inventory_sha256"],
                "recognition": recognition["inventory_sha256"],
                "constructor": {
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                },
            }
        )
    )
    with readonly_connection(db) as con:
        if not object_exists(con, config["queue_view"], "view"):
            raise RuntimeError("frozen_ocr_queue_view_missing")
        columns = {
            row["name"]
            for row in con.execute(f"PRAGMA table_info({config['queue_view']})")
        }
        missing_columns = sorted(REQUIRED_QUEUE_COLUMNS - columns)
        if missing_columns:
            raise RuntimeError(f"ocr_queue_columns_missing:{missing_columns}")
        row = con.execute(
            f"""SELECT COUNT(*) total,COUNT(DISTINCT candidate_id) unique_count,
                SUM(CASE WHEN runtime_visual_file='' THEN 1 ELSE 0 END) missing_path,
                SUM(CASE WHEN runtime_visual_file_sha256='' THEN 1 ELSE 0 END) missing_sha
                FROM {config['queue_view']}"""
        ).fetchone()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(con.execute("PRAGMA foreign_key_check"))
    if int(row["total"]) != int(row["unique_count"]):
        raise RuntimeError("ocr_queue_candidate_id_not_unique")
    if int(row["missing_path"] or 0) or int(row["missing_sha"] or 0):
        raise RuntimeError("ocr_queue_runtime_input_incomplete")
    return {
        "status": "PASS",
        "contract_version": CONTRACT_VERSION,
        "queue_view": config["queue_view"],
        "queue_count": int(row["total"]),
        "queue_unique_count": int(row["unique_count"]),
        "detection_model_dir": detection["path"],
        "recognition_model_dir": recognition["path"],
        "detection_model_sha256": detection["inventory_sha256"],
        "recognition_model_sha256": recognition["inventory_sha256"],
        "model_fingerprint_sha256": model_fingerprint,
        "detection_model_file_count": detection["file_count"],
        "recognition_model_file_count": recognition["file_count"],
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "script_sha256": sha256_file(script_path),
        "database_integrity_check": integrity,
        "foreign_key_error_count": len(foreign_keys),
        "network_policy": "blocked_in_worker",
        "source_policy": "derived_visual_only",
        "original_video_read": False,
        "model_download": False,
        "config": config,
    }


def load_frozen_queue(db: Path, view: str, limit: int = 0) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {view} ORDER BY candidate_id"
    params: tuple[Any, ...] = ()
    if limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    with readonly_connection(db) as con:
        rows = [dict(row) for row in con.execute(sql, params)]
    return rows


def prepare_execution_rows(
    rows: Iterable[dict[str, Any]],
    preflight: dict[str, Any],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        execution_key = sha256_text(
            canonical_json(
                {
                    "candidate_id": row["candidate_id"],
                    "runtime_visual_file_sha256": row["runtime_visual_file_sha256"],
                    "model_fingerprint_sha256": preflight["model_fingerprint_sha256"],
                    "config_sha256": preflight["config_sha256"],
                    "script_sha256": preflight["script_sha256"],
                    "contract_version": CONTRACT_VERSION,
                    "result_contract": RESULT_CONTRACT,
                }
            )
        )
        prepared.append({**row, "execution_key": execution_key})
    if len({row["execution_key"] for row in prepared}) != len(prepared):
        raise RuntimeError("duplicate_execution_key_in_prepared_queue")
    return prepared


def apply_migration(db: Path, migration: Path) -> None:
    sql = migration.read_text(encoding="utf-8")
    con = writable_connection(db)
    try:
        con.executescript(sql)
        con.commit()
    finally:
        con.close()


def backup_database(db: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"{db.stem}_before_stop03_4_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite"
    )
    source_con = readonly_connection(db)
    target_con = sqlite3.connect(str(target))
    try:
        source_con.backup(target_con)
        target_con.commit()
    finally:
        target_con.close()
        source_con.close()
    if sha256_file(target) == sha256_text(""):
        raise RuntimeError("empty_database_backup")
    return target


def validate_migration_in_memory(migration: Path) -> None:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE stop03_2_candidate_queue_items(candidate_id TEXT PRIMARY KEY)")
        con.execute(
            "CREATE TABLE stop03_2_candidate_queue_frozen_v25("
            "candidate_id TEXT PRIMARY KEY)"
        )
        con.executescript(migration.read_text(encoding="utf-8"))
    finally:
        con.close()


def create_run_and_items(
    db: Path,
    rows: list[dict[str, Any]],
    preflight: dict[str, Any],
    *,
    run_kind: str,
    workers: int,
    max_attempts: int,
) -> str:
    if run_kind not in {"smoke", "full"}:
        raise ValueError("invalid_run_kind")
    run_id = "stop03_4_ocr_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    candidate_digest = sha256_text(
        "\n".join(sorted(row["candidate_id"] for row in rows))
    )
    now = utc_now()
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """INSERT INTO stop03_4_ocr_runs(
                run_id,run_kind,contract_version,queue_view,candidate_count,
                candidate_id_set_sha256,model_root,detection_model_dir,
                recognition_model_dir,detection_model_sha256,
                recognition_model_sha256,model_fingerprint_sha256,
                config_path,config_sha256,script_sha256,workers,max_attempts,
                status,pending_count,started_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?)""",
            (
                run_id,
                run_kind,
                CONTRACT_VERSION,
                preflight["queue_view"],
                len(rows),
                candidate_digest,
                preflight["config"]["model_root"],
                preflight["detection_model_dir"],
                preflight["recognition_model_dir"],
                preflight["detection_model_sha256"],
                preflight["recognition_model_sha256"],
                preflight["model_fingerprint_sha256"],
                preflight["config_path"],
                preflight["config_sha256"],
                preflight["script_sha256"],
                workers,
                max_attempts,
                len(rows),
                now,
            ),
        )
        for row in rows:
            existing = con.execute(
                """SELECT result_id,result_status FROM stop03_4_ocr_results
                   WHERE execution_key=?""",
                (row["execution_key"],),
            ).fetchone()
            status = existing["result_status"] if existing else "pending"
            result_id = existing["result_id"] if existing else None
            reused = 1 if existing else 0
            con.execute(
                """INSERT INTO stop03_4_ocr_run_items(
                    run_item_id,run_id,candidate_id,execution_key,result_id,
                    source_content_id,visual_unit_id,canonical_visual_unit_id,
                    derived_id,candidate_role,reason_codes,policy_version,
                    media_type,time_position_ms,runtime_visual_file,
                    runtime_visual_file_sha256,status,reused_existing_result,
                    created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    stable_id("ocr_item_", run_id, row["candidate_id"]),
                    run_id,
                    row["candidate_id"],
                    row["execution_key"],
                    result_id,
                    row["source_content_id"],
                    row["visual_unit_id"],
                    row["canonical_visual_unit_id"],
                    row["derived_id"],
                    row["candidate_role"],
                    row["reason_codes"],
                    row["policy_version"],
                    row["media_type"],
                    int(row["time_position_ms"]),
                    row["runtime_visual_file"],
                    row["runtime_visual_file_sha256"],
                    status,
                    reused,
                    now,
                    now if existing else None,
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    refresh_run_counts(db, run_id)
    return run_id


def refresh_run_counts(db: Path, run_id: str) -> dict[str, int]:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        counts = {
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_4_ocr_run_items
                   WHERE run_id=? GROUP BY status""",
                (run_id,),
            )
        }
        reused = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_4_ocr_run_items
                   WHERE run_id=? AND reused_existing_result=1""",
                (run_id,),
            ).fetchone()[0]
        )
        values = {
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "success": counts.get("success", 0),
            "no_text": counts.get("no_text", 0),
            "failed": sum(
                counts.get(status, 0)
                for status in ("failed", "input_fingerprint_mismatch", "review")
            ),
            "reused": reused,
        }
        con.execute(
            """UPDATE stop03_4_ocr_runs SET
               pending_count=?,running_count=?,success_count=?,no_text_count=?,
               failed_count=?,reused_count=? WHERE run_id=?""",
            (
                values["pending"],
                values["running"],
                values["success"],
                values["no_text"],
                values["failed"],
                values["reused"],
                run_id,
            ),
        )
        con.commit()
        return values
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_next_item(db: Path, run_id: str, worker_label: str) -> dict[str, Any] | None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT * FROM stop03_4_ocr_run_items
               WHERE run_id=? AND status='pending'
               ORDER BY candidate_id LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        now = utc_now()
        con.execute(
            """UPDATE stop03_4_ocr_run_items
               SET status='running',attempt_count=attempt_count+1,
                   claimed_by_worker=?,started_at=?,finished_at=NULL
               WHERE run_item_id=? AND status='pending'""",
            (worker_label, now, row["run_item_id"]),
        )
        claimed = con.execute(
            "SELECT * FROM stop03_4_ocr_run_items WHERE run_item_id=?",
            (row["run_item_id"],),
        ).fetchone()
        con.commit()
        return dict(claimed)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def prepare_resume(
    db: Path,
    run_id: str,
    workers: int,
    preflight: dict[str, Any] | None = None,
) -> None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            "SELECT * FROM stop03_4_ocr_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError("ocr_run_not_found")
        if preflight is not None:
            expected = {
                "contract_version": CONTRACT_VERSION,
                "detection_model_sha256": preflight["detection_model_sha256"],
                "recognition_model_sha256": preflight["recognition_model_sha256"],
                "model_fingerprint_sha256": preflight["model_fingerprint_sha256"],
                "config_sha256": preflight["config_sha256"],
                "script_sha256": preflight["script_sha256"],
            }
            mismatches = {
                key: {"stored": run[key], "current": value}
                for key, value in expected.items()
                if run[key] != value
            }
            if mismatches:
                raise RuntimeError(
                    "ocr_resume_fingerprint_mismatch:" + canonical_json(mismatches)
                )
        con.execute(
            """UPDATE stop03_4_ocr_run_items SET status='pending',
               claimed_by_worker='',started_at=NULL
               WHERE run_id=? AND status='running'""",
            (run_id,),
        )
        con.execute(
            """UPDATE stop03_4_ocr_runs SET status='running',workers=?,
               finished_at=NULL,error_message='' WHERE run_id=?""",
            (workers, run_id),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    refresh_run_counts(db, run_id)


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "json"):
        try:
            payload = value.json
            return to_jsonable(payload() if callable(payload) else payload)
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return to_jsonable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "tolist"):
        try:
            return value.tolist()
        except Exception:
            pass
    return repr(value)


def extract_lines(value: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            texts = item.get("rec_texts") or item.get("texts") or item.get("text")
            scores = item.get("rec_scores") or item.get("scores") or item.get("score")
            boxes = (
                item.get("rec_polys")
                or item.get("dt_polys")
                or item.get("boxes")
                or item.get("box")
            )
            if isinstance(texts, list):
                for index, text in enumerate(texts):
                    if isinstance(text, str) and text.strip():
                        score = (
                            scores[index]
                            if isinstance(scores, list) and index < len(scores)
                            else None
                        )
                        box = (
                            boxes[index]
                            if isinstance(boxes, list) and index < len(boxes)
                            else None
                        )
                        lines.append(
                            {
                                "text": text.strip(),
                                "confidence": score,
                                "box": to_jsonable(box),
                            }
                        )
                return
            if isinstance(texts, str) and texts.strip():
                lines.append(
                    {
                        "text": texts.strip(),
                        "confidence": scores if isinstance(scores, (int, float)) else None,
                        "box": to_jsonable(boxes),
                    }
                )
                return
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            if (
                len(item) >= 2
                and isinstance(item[1], (list, tuple))
                and len(item[1]) >= 2
                and isinstance(item[1][0], str)
            ):
                lines.append(
                    {
                        "text": item[1][0].strip(),
                        "confidence": item[1][1],
                        "box": to_jsonable(item[0]),
                    }
                )
                return
            for child in item:
                walk(child)

    walk(value)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        key = canonical_json(line)
        if line["text"] and key not in seen:
            seen.add(key)
            unique.append(line)
    return unique


def compact_raw_result(value: Any) -> Any:
    """Keep OCR diagnostics, but never serialize image tensors or font objects.

    PaddleOCR's ``doc_preprocessor_res.output_img`` is a full RGB integer
    tensor.  Serializing it makes one result JSON tens of megabytes even
    though the searchable evidence is already represented by ``ocr_lines``.
    """
    omitted = {"output_img", "vis_fonts"}
    if isinstance(value, dict):
        return {
            str(key): compact_raw_result(item)
            for key, item in value.items()
            if str(key) not in omitted
        }
    if isinstance(value, (list, tuple)):
        return [compact_raw_result(item) for item in value]
    return value


def install_network_block() -> None:
    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"NETWORK_BLOCKED_BY_STOP03_4_OCR args={args!r}")

    socket.socket.connect = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]


def init_ocr_worker(config: dict[str, Any]) -> None:
    global _OCR_ENGINE
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    install_network_block()
    from paddleocr import PaddleOCR  # type: ignore

    _OCR_ENGINE = PaddleOCR(
        text_detection_model_dir=config["text_detection_model_dir"],
        text_recognition_model_dir=config["text_recognition_model_dir"],
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_rec_score_thresh=float(config.get("text_rec_score_thresh", 0.0)),
    )


def infer_ocr_item(item: dict[str, Any], out_dir: str) -> dict[str, Any]:
    started_at = utc_now()
    started = time.monotonic()
    output_path = (
        Path(out_dir)
        / "outputs"
        / item["run_id"]
        / f"{item['candidate_id']}_attempt{item['attempt_count']}.json"
    )
    result: dict[str, Any] = {
        "status": "failed",
        "error_code": "",
        "error_message": "",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": 0.0,
        "worker_pid": os.getpid(),
        "output_json_path": str(output_path),
        "output_json_sha256": "",
    }
    try:
        image_path = Path(item["runtime_visual_file"])
        if not image_path.is_file():
            raise FileNotFoundError(f"runtime_visual_file_missing:{image_path}")
        actual_sha = sha256_file(image_path)
        if actual_sha != item["runtime_visual_file_sha256"]:
            result["status"] = "input_fingerprint_mismatch"
            result["error_code"] = "runtime_visual_file_sha256_mismatch"
            result["error_message"] = f"expected={item['runtime_visual_file_sha256']} actual={actual_sha}"
            return result
        if _OCR_ENGINE is None:
            raise RuntimeError("ocr_engine_not_initialized")
        raw = _OCR_ENGINE.predict(
            str(image_path),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        raw_json = to_jsonable(raw)
        lines = extract_lines(raw_json)
        compact_raw = compact_raw_result(raw_json)
        text = "\n".join(line["text"] for line in lines).strip()
        confidences = [
            float(line["confidence"])
            for line in lines
            if isinstance(line.get("confidence"), (int, float))
        ]
        payload = {
            "contract_version": CONTRACT_VERSION,
            "candidate_id": item["candidate_id"],
            "execution_key": item["execution_key"],
            "runtime_visual_file": str(image_path),
            "runtime_visual_file_sha256": actual_sha,
            "ocr_api_used": "PaddleOCR.predict",
            "ocr_text": text,
            "ocr_text_sha256": sha256_text(text),
            "ocr_line_count": len(lines),
            "ocr_lines": lines,
            "mean_confidence": (
                sum(confidences) / len(confidences) if confidences else None
            ),
            "min_confidence": min(confidences) if confidences else None,
            "max_confidence": max(confidences) if confidences else None,
            "raw_result": compact_raw,
            "raw_result_retention_policy": "compact_no_image_tensor_no_font_objects_v1",
        }
        write_json(output_path, payload)
        result.update(payload)
        result["status"] = "success" if text else "no_text"
        result["output_json_sha256"] = sha256_file(output_path)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error_code"] = type(exc).__name__
        result["error_message"] = repr(exc)
        result["traceback"] = traceback.format_exc()
        return result
    finally:
        result["finished_at"] = utc_now()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)


def complete_item(
    db: Path,
    item: dict[str, Any],
    outcome: dict[str, Any],
    preflight: dict[str, Any],
    *,
    max_attempts: int,
) -> None:
    raw_status = str(outcome.get("status") or "failed")
    terminal_status = raw_status
    if raw_status == "failed" and int(item["attempt_count"]) < max_attempts:
        terminal_status = "pending"
    now = utc_now()
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """INSERT INTO stop03_4_ocr_attempts(
               attempt_id,run_id,run_item_id,candidate_id,execution_key,
               attempt_number,status,error_code,error_message,elapsed_seconds,
               worker_pid,output_json_path,output_json_sha256,started_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stable_id(
                    "ocr_attempt_",
                    item["run_item_id"],
                    item["attempt_count"],
                ),
                item["run_id"],
                item["run_item_id"],
                item["candidate_id"],
                item["execution_key"],
                item["attempt_count"],
                raw_status,
                str(outcome.get("error_code") or ""),
                str(outcome.get("error_message") or "")[:4000],
                float(outcome.get("elapsed_seconds") or 0.0),
                int(outcome.get("worker_pid") or 0),
                str(outcome.get("output_json_path") or ""),
                str(outcome.get("output_json_sha256") or ""),
                str(outcome.get("started_at") or now),
                str(outcome.get("finished_at") or now),
            ),
        )
        result_id = None
        if raw_status in TERMINAL_OK:
            result_id = stable_id("ocr_result_", item["execution_key"])
            evidence_id = stable_id("ocr_ev_", item["execution_key"])
            lines = outcome.get("ocr_lines") or []
            text = str(outcome.get("ocr_text") or "")
            con.execute(
                """INSERT INTO stop03_4_ocr_results(
                   result_id,execution_key,candidate_id,evidence_id,result_status,
                   source_content_id,visual_unit_id,canonical_visual_unit_id,
                   derived_id,candidate_role,reason_codes,policy_version,media_type,
                   time_position_ms,runtime_visual_file,runtime_visual_file_sha256,
                   ocr_text,ocr_text_preview,ocr_text_sha256,ocr_lines_json,
                   ocr_line_count,mean_confidence,min_confidence,max_confidence,
                   output_json_path,output_json_sha256,elapsed_seconds,worker_pid,
                   ocr_api_used,detection_model_sha256,recognition_model_sha256,
                   model_fingerprint_sha256,config_sha256,script_sha256,
                   contract_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(execution_key) DO NOTHING""",
                (
                    result_id,
                    item["execution_key"],
                    item["candidate_id"],
                    evidence_id,
                    raw_status,
                    item["source_content_id"],
                    item["visual_unit_id"],
                    item["canonical_visual_unit_id"],
                    item["derived_id"],
                    item["candidate_role"],
                    item["reason_codes"],
                    item["policy_version"],
                    item["media_type"],
                    int(item["time_position_ms"]),
                    item["runtime_visual_file"],
                    item["runtime_visual_file_sha256"],
                    text,
                    text[:500],
                    sha256_text(text),
                    canonical_json(lines),
                    len(lines),
                    outcome.get("mean_confidence"),
                    outcome.get("min_confidence"),
                    outcome.get("max_confidence"),
                    str(outcome.get("output_json_path") or ""),
                    str(outcome.get("output_json_sha256") or ""),
                    float(outcome.get("elapsed_seconds") or 0.0),
                    int(outcome.get("worker_pid") or 0),
                    str(outcome.get("ocr_api_used") or "PaddleOCR.predict"),
                    preflight["detection_model_sha256"],
                    preflight["recognition_model_sha256"],
                    preflight["model_fingerprint_sha256"],
                    preflight["config_sha256"],
                    preflight["script_sha256"],
                    CONTRACT_VERSION,
                    now,
                ),
            )
            stored = con.execute(
                "SELECT result_id,result_status FROM stop03_4_ocr_results WHERE execution_key=?",
                (item["execution_key"],),
            ).fetchone()
            result_id = stored["result_id"]
            terminal_status = stored["result_status"]
        con.execute(
            """UPDATE stop03_4_ocr_run_items SET status=?,result_id=?,
               last_error_code=?,last_error_message=?,claimed_by_worker='',
               finished_at=? WHERE run_item_id=?""",
            (
                terminal_status,
                result_id,
                str(outcome.get("error_code") or ""),
                str(outcome.get("error_message") or "")[:4000],
                now if terminal_status != "pending" else None,
                item["run_item_id"],
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def read_progress(db: Path, run_id: str) -> dict[str, Any]:
    with readonly_connection(db) as con:
        run = con.execute(
            "SELECT * FROM stop03_4_ocr_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError("ocr_run_not_found")
        counts = {
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_4_ocr_run_items
                   WHERE run_id=? GROUP BY status""",
                (run_id,),
            )
        }
        last = con.execute(
            """SELECT candidate_id,status,finished_at FROM stop03_4_ocr_run_items
               WHERE run_id=? AND finished_at IS NOT NULL
               ORDER BY finished_at DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    total = int(run["candidate_count"])
    completed = sum(counts.get(status, 0) for status in TERMINAL_ALL)
    return {
        "timestamp": utc_now(),
        "run_id": run_id,
        "run_kind": run["run_kind"],
        "workers_requested": int(run["workers"]),
        "total": total,
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "success": counts.get("success", 0),
        "no_text": counts.get("no_text", 0),
        "failed": counts.get("failed", 0)
        + counts.get("input_fingerprint_mismatch", 0)
        + counts.get("review", 0),
        "completed": completed,
        "remaining": total - completed,
        "percent": round(100.0 * completed / total, 2) if total else 100.0,
        "last_completed_candidate_id": last["candidate_id"] if last else "",
        "last_completed_status": last["status"] if last else "",
    }


def print_progress(progress: dict[str, Any]) -> None:
    print(
        " ".join(
            [
                f"run_id={progress['run_id']}",
                f"workers={progress['workers_requested']}",
                f"pending={progress['pending']}",
                f"running={progress['running']}",
                f"success={progress['success']}",
                f"no_text={progress['no_text']}",
                f"failed={progress['failed']}",
                f"remaining={progress['remaining']}",
                f"percent={progress['percent']:.2f}",
                f"last={progress['last_completed_candidate_id']}",
            ]
        ),
        flush=True,
    )


def execute_dynamic_pool(
    db: Path,
    run_id: str,
    out: Path,
    preflight: dict[str, Any],
    *,
    workers: int,
    max_attempts: int,
    executor_factory: Callable[..., Any] = ProcessPoolExecutor,
    inference_function: Callable[[dict[str, Any], str], dict[str, Any]] = infer_ocr_item,
    executor_initializer: Callable[..., Any] | None = init_ocr_worker,
) -> dict[str, Any]:
    with readonly_connection(db) as con:
        candidate_count = int(
            con.execute(
                "SELECT candidate_count FROM stop03_4_ocr_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    if candidate_count == 0:
        return finalize_run(db, run_id)

    progress_path = out / "logs" / "progress.jsonl"
    kwargs: dict[str, Any] = {"max_workers": workers}
    if executor_initializer is not None:
        kwargs["initializer"] = executor_initializer
        kwargs["initargs"] = (preflight["config"],)
    active: dict[Any, dict[str, Any]] = {}
    claim_sequence = 0
    with executor_factory(**kwargs) as executor:
        while True:
            while len(active) < workers:
                claim_sequence += 1
                item = claim_next_item(db, run_id, f"slot_{claim_sequence}")
                if item is None:
                    break
                future = executor.submit(inference_function, item, str(out))
                active[future] = item
            if not active:
                break
            done, _pending = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                item = active.pop(future)
                try:
                    outcome = future.result()
                except BaseException as exc:
                    outcome = {
                        "status": "failed",
                        "error_code": type(exc).__name__,
                        "error_message": repr(exc),
                        "elapsed_seconds": 0.0,
                        "worker_pid": 0,
                        "started_at": utc_now(),
                        "finished_at": utc_now(),
                        "output_json_path": "",
                        "output_json_sha256": "",
                    }
                complete_item(
                    db,
                    item,
                    outcome,
                    preflight,
                    max_attempts=max_attempts,
                )
                refresh_run_counts(db, run_id)
                progress = read_progress(db, run_id)
                append_jsonl(progress_path, progress)
                print_progress(progress)
    return finalize_run(db, run_id)


def finalize_run(db: Path, run_id: str) -> dict[str, Any]:
    counts = refresh_run_counts(db, run_id)
    with readonly_connection(db) as con:
        total = int(
            con.execute(
                "SELECT candidate_count FROM stop03_4_ocr_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    terminal_ok = counts["success"] + counts["no_text"]
    if terminal_ok == total and counts["failed"] == 0:
        status = "success"
    elif counts["pending"] or counts["running"]:
        status = "partial"
    else:
        status = "failed"
    con = writable_connection(db)
    try:
        con.execute(
            """UPDATE stop03_4_ocr_runs SET status=?,finished_at=?
               WHERE run_id=?""",
            (status, utc_now(), run_id),
        )
        con.commit()
    finally:
        con.close()
    return readback_run(db, run_id)


def readback_run(db: Path, run_id: str) -> dict[str, Any]:
    with readonly_connection(db) as con:
        run = con.execute(
            "SELECT * FROM stop03_4_ocr_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError("ocr_run_not_found")
        counts = {
            row["status"]: int(row["count"])
            for row in con.execute(
                """SELECT status,COUNT(*) count FROM stop03_4_ocr_run_items
                   WHERE run_id=? GROUP BY status""",
                (run_id,),
            )
        }
        result_count = int(
            con.execute(
                """SELECT COUNT(DISTINCT r.result_id)
                   FROM stop03_4_ocr_run_items i
                   JOIN stop03_4_ocr_results r ON r.result_id=i.result_id
                   WHERE i.run_id=?""",
                (run_id,),
            ).fetchone()[0]
        )
        duplicate_keys = int(
            con.execute(
                """SELECT COUNT(*) FROM (
                   SELECT execution_key FROM stop03_4_ocr_results
                   GROUP BY execution_key HAVING COUNT(*)>1)"""
            ).fetchone()[0]
        )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = len(list(con.execute("PRAGMA foreign_key_check")))
    total = int(run["candidate_count"])
    ok_count = counts.get("success", 0) + counts.get("no_text", 0)
    status = (
        "PASS"
        if run["status"] == "success"
        and ok_count == total
        and result_count == total
        and duplicate_keys == 0
        and integrity == "ok"
        and fk_errors == 0
        else "FAIL"
    )
    return {
        "status": status,
        "run_id": run_id,
        "run_status": run["status"],
        "run_kind": run["run_kind"],
        "candidate_count": total,
        "success_count": counts.get("success", 0),
        "no_text_count": counts.get("no_text", 0),
        "failed_count": counts.get("failed", 0)
        + counts.get("input_fingerprint_mismatch", 0)
        + counts.get("review", 0),
        "pending_count": counts.get("pending", 0),
        "running_count": counts.get("running", 0),
        "result_count": result_count,
        "reused_count": int(run["reused_count"]),
        "execution_key_duplicates": duplicate_keys,
        "database_integrity_check": integrity,
        "foreign_key_error_count": fk_errors,
    }


def build_parser() -> argparse.ArgumentParser:
    project = Path("$APP_RESOURCES/Pipeline")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("preflight", "dry-run", "run", "resume", "readback"),
        required=True,
    )
    parser.add_argument("--db", type=Path, default=project / "media_archive.sqlite")
    parser.add_argument(
        "--config", type=Path, default=project / "configs/stop03_4_ocr_db_v1.json"
    )
    parser.add_argument(
        "--migration",
        type=Path,
        default=project / "migrations/20260716_stop03_4_ocr_db_v1.sql",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "$USER_HOME/Documents/AI-Local/test-output/stop03_4_ocr_db_v1"
        ),
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-kind", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers < 1 or args.max_attempts < 1:
        raise SystemExit("workers/max-attempts must be >= 1")
    script_path = Path(__file__).resolve()
    if args.mode in {"resume", "readback"}:
        if not args.run_id:
            raise SystemExit("--run-id is required")
        if args.mode == "readback":
            print(json.dumps(readback_run(args.db, args.run_id), ensure_ascii=False, indent=2))
            return 0
    preflight = build_preflight(args.db, args.config, script_path)
    validate_migration_in_memory(args.migration)
    if args.mode == "preflight":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "dry-run":
        rows = prepare_execution_rows(
            load_frozen_queue(args.db, preflight["queue_view"], args.limit),
            preflight,
        )
        summary = {
            **{key: value for key, value in preflight.items() if key != "config"},
            "mode": "dry-run",
            "selected_count": len(rows),
            "execution_key_unique_count": len({row["execution_key"] for row in rows}),
            "central_db_write": False,
            "ocr_run": False,
            "status": "PASS",
        }
        write_json(args.out / "reports" / "dry_run_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_central_db_write:
        raise SystemExit("--confirm-central-db-write is required for run/resume")
    backup_path = backup_database(args.db, args.out / "backups")
    print(f"database_backup={backup_path}", flush=True)
    apply_migration(args.db, args.migration)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == "run":
        limit = args.limit
        if args.run_kind == "full":
            if limit not in (0,):
                raise SystemExit("full run requires --limit 0")
        elif limit < 1 or limit > 5:
            raise SystemExit("smoke run requires --limit between 1 and 5")
        rows = prepare_execution_rows(
            load_frozen_queue(args.db, preflight["queue_view"], limit),
            preflight,
        )
        run_id = create_run_and_items(
            args.db,
            rows,
            preflight,
            run_kind=args.run_kind,
            workers=args.workers,
            max_attempts=args.max_attempts,
        )
    else:
        run_id = args.run_id
        prepare_resume(args.db, run_id, args.workers, preflight)
    print(f"run_id={run_id}", flush=True)
    report = execute_dynamic_pool(
        args.db,
        run_id,
        args.out,
        preflight,
        workers=args.workers,
        max_attempts=args.max_attempts,
    )
    write_json(args.out / "reports" / f"{run_id}_summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
