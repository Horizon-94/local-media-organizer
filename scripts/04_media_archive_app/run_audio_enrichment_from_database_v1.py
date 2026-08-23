#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CONTRACT = "media_archive_audio_enrichment_v1"
RUNTIME_CONTRACT = "media_archive_stage_runtime_contract_v1"
DEFAULT_MAX_ITEM_ATTEMPTS = 2


def stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def progress(
    completed: int, total: int, success: int, skipped: int, failed: int,
    current: str = "", *, configured_workers: int = 1, actual_workers: int = 0,
) -> None:
    print(json.dumps({
        "contract": RUNTIME_CONTRACT, "event": "stage_progress",
        "completed": completed, "total": total, "success": success,
        "skipped": skipped, "failed": failed,
        "remaining": max(0, total - completed), "current_item": current,
        "model_workers": configured_workers, "actual_workers": actual_workers,
    }, ensure_ascii=False), flush=True)


def source_rows(db: Path, limit: int) -> list[sqlite3.Row]:
    with sqlite3.connect(db, timeout=30.0) as con:
        con.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(source_assets)")}
        required = {"source_content_id", "absolute_path", "media_type"}
        if not required <= columns:
            raise RuntimeError("audio_enrichment_source_schema_incompatible")
        query = (
            "SELECT source_content_id,absolute_path,relative_path FROM source_assets "
            "WHERE media_type='video' AND COALESCE(is_deleted_or_missing,0)=0 "
            "ORDER BY relative_path,source_content_id"
        )
        if "is_deleted_or_missing" not in columns:
            query = (
                "SELECT source_content_id,absolute_path,relative_path FROM source_assets "
                "WHERE media_type='video' ORDER BY relative_path,source_content_id"
            )
        rows = con.execute(query).fetchall()
    return rows[:limit] if limit else rows


def load_preextracted_audio_manifest(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    resolved = path.expanduser().absolute()
    # Historical tasks finished stage 3 before co-extraction existed.  Their
    # resume path must remain usable and deliberately falls back to source.
    if not resolved.is_file():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line_no, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"audio_coextract_manifest_invalid_json:line={line_no}"
            ) from exc
        source_id = str(row.get("source_content_id") or "")
        if source_id:
            rows[source_id] = row
    return rows


def clean_temporary_audio(output: Path) -> None:
    for candidate in (
        output / "audio_16k_mono.wav", output / "audio_48k_mono.wav",
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
    deepfilter = output / "deepfilter"
    if deepfilter.is_dir():
        for candidate in deepfilter.glob("*.wav"):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def child_runtime_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("PYTHON") or key in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}:
            environment.pop(key, None)
    environment.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
    })
    return environment


def run_pilot_process(command: list[str], output: Path) -> dict[str, Any]:
    """Run one isolated Whisper worker and return its durable JSON report.

    Audio inference is isolated per child so two or three workers can actually
    overlap without sharing MLX state.  Temporary WAV files are removed in the
    worker completion path; only the JSON report is handed to the database
    writer in the parent process.
    """
    report_path = output / "audio_search_pilot.json"
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True,
            env=child_runtime_environment(), check=False,
        )
        if completed.returncode != 0 or not report_path.is_file():
            diagnostic = (completed.stderr or completed.stdout)[-4000:]
            raise RuntimeError(
                f"audio_pilot_exit={completed.returncode}:{diagnostic}"
            )
        return json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        clean_temporary_audio(output)


def verify_audio_runtime(python: Path) -> None:
    """Fail before touching source media when the packaged worker is incomplete."""
    completed = subprocess.run(
        [str(python), "-c", "import numpy, torch"],
        text=True, capture_output=True, env=child_runtime_environment(), check=False,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout)[-2000:]
        raise RuntimeError(
            f"AUDIO_RUNTIME_PREFLIGHT_FAILED:python={python}:"
            f"exit={completed.returncode}:{diagnostic}"
        )


def append_failure(path: Path, payload: dict[str, Any]) -> None:
    """Persist each failure immediately so cancellation cannot erase evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_unprocessed_reports(
    db: Path, out: Path, config_sha: str, max_attempts: int,
) -> tuple[Path, Path, list[dict[str, Any]]]:
    """Write the durable user-facing list of terminal per-file failures."""
    with sqlite3.connect(db, timeout=30.0) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(row) for row in con.execute(
            """SELECT source_content_id,source_path,attempt_count,last_error_code,
                      last_error_message,finished_at
               FROM audio_processing_items
               WHERE config_sha256=? AND status='failed' AND attempt_count>=?
               ORDER BY source_path,source_content_id""",
            (config_sha, max_attempts),
        )]
    json_path = out / "audio_unprocessed_files.json"
    csv_path = out / "audio_unprocessed_files.csv"
    payload = {
        "contract": CONTRACT,
        "status": "COMPLETED_WITH_WARNINGS" if rows else "SUCCESS",
        "max_item_attempts": max_attempts,
        "unprocessed_count": len(rows),
        "items": rows,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fieldnames = [
        "source_content_id", "source_path", "attempt_count",
        "last_error_code", "last_error_message", "finished_at",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--audio-python", type=Path)
    parser.add_argument("--embedding-python", type=Path, required=True)
    parser.add_argument("--audio-pilot-script", type=Path, required=True)
    parser.add_argument("--embedding-script", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--silero-root", type=Path, required=True)
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--deep-filter-executable", type=Path, required=True)
    parser.add_argument("--deep-filter-model", type=Path, required=True)
    parser.add_argument("--embedding-model", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--max-item-attempts", type=int, default=DEFAULT_MAX_ITEM_ATTEMPTS,
    )
    parser.add_argument("--preextracted-audio-manifest", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_central_db_write:
        raise RuntimeError("audio_enrichment_central_db_write_not_confirmed")
    if args.limit < 0:
        raise ValueError("audio_enrichment_limit_invalid")
    if not 1 <= args.workers <= 3:
        raise ValueError("audio_enrichment_workers_must_be_1_to_3")
    if args.max_item_attempts < 1:
        raise ValueError("audio_enrichment_max_item_attempts_must_be_positive")
    db = args.db.resolve(strict=True)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    preextracted = load_preextracted_audio_manifest(
        args.preextracted_audio_manifest
    )
    spool_paths: dict[str, Path] = {}
    audio_python = (args.audio_python or Path(sys.executable)).absolute()
    if not audio_python.is_file():
        raise FileNotFoundError(f"audio Python missing: {audio_python}")
    migration = args.migration.resolve(strict=True)
    runtime_paths = [
        audio_python, args.embedding_python, args.audio_pilot_script,
        args.embedding_script, args.ffmpeg, args.ffprobe, args.silero_root,
        args.whisper_model, args.deep_filter_executable, args.deep_filter_model,
        args.embedding_model,
    ]
    for path in runtime_paths:
        path.resolve(strict=True)
    # This must happen before source_rows(), is_file(), stat(), ffprobe or
    # ffmpeg.  A broken portable runtime must never churn through an HDD.
    try:
        verify_audio_runtime(audio_python)
    except Exception as exc:
        failure = {
            "contract": RUNTIME_CONTRACT,
            "event": "stage_failed",
            "reason_code": "AUDIO_RUNTIME_PREFLIGHT_FAILED",
            "error_message": str(exc)[:2000],
        }
        append_failure(out / "stage_failures.jsonl", failure)
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        raise
    config = {
        "contract": CONTRACT,
        "silero": str(args.silero_root.resolve()),
        "whisper": str(args.whisper_model.resolve()),
        "deep_filter": str(args.deep_filter_model.resolve()),
        "embedding": str(args.embedding_model.resolve()),
    }
    config_sha = stable_hash(config)
    with sqlite3.connect(db, timeout=30.0) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(migration.read_text(encoding="utf-8"))
        con.execute(
            "UPDATE audio_processing_items SET status='pending',updated_at=CURRENT_TIMESTAMP "
            "WHERE status='running'"
        )
        con.commit()
    videos = source_rows(db, args.limit)
    selected_source_ids = {str(row["source_content_id"]) for row in videos}
    runnable_source_ids: set[str] = set()
    offline_unfinished_ids: set[str] = set()
    for row in videos:
        source_id = str(row["source_content_id"])
        path = Path(str(row["absolute_path"]))
        manifest_row = preextracted.get(source_id) or {}
        manifest_status = str(manifest_row.get("status") or "")
        manifest_audio = Path(str(manifest_row.get("audio_path") or ""))
        source_available = path.is_file()
        derived_audio_available = (
            manifest_status == "ready" and manifest_audio.is_file()
        )
        derived_no_audio = manifest_status == "no_audio_stream"
        with sqlite3.connect(db, timeout=30.0) as con:
            prior = con.execute(
                "SELECT source_size_bytes,source_mtime_ns,config_sha256,status,attempt_count "
                "FROM audio_processing_items WHERE source_content_id=?",
                (source_id,),
            ).fetchone()
        prior_matches = bool(prior and str(prior[2]) == config_sha)
        prior_status = str(prior[3]) if prior else ""
        if prior_matches and prior_status in {"success", "no_speech", "no_audio"}:
            # A detached source disk must never erase a durable terminal result.
            continue
        if (
            prior_matches and prior_status == "failed"
            and int(prior[4] or 0) >= args.max_item_attempts
        ):
            # The item already received the bounded number of real attempts.
            # Preserve it as a reportable terminal warning instead of making
            # every future resume repeat it forever.
            continue
        if not source_available and not derived_audio_available and not derived_no_audio:
            offline_unfinished_ids.add(source_id)
            if prior is None:
                with sqlite3.connect(db, timeout=30.0) as con:
                    con.execute(
                        """INSERT INTO audio_processing_items(
                               source_content_id,source_path,source_size_bytes,source_mtime_ns,
                               config_sha256,status,last_error_code,last_error_message
                           ) VALUES(?,?,0,0,?,'failed','SOURCE_OFFLINE',?)""",
                        (source_id, str(path), config_sha, "原始视频不存在或素材盘未连接"),
                    )
                    con.commit()
            # Preserve a previous real decoder error instead of replacing it
            # with the less specific consequence of a detached volume.
            continue
        runnable_source_ids.add(source_id)
        if source_available:
            source_stat = path.stat()
            stat_size = source_stat.st_size
            stat_mtime_ns = source_stat.st_mtime_ns
        elif prior:
            stat_size, stat_mtime_ns = int(prior[0]), int(prior[1])
        else:
            stat_size = int(manifest_row.get("size_bytes") or 0)
            stat_mtime_ns = 0
        with sqlite3.connect(db, timeout=30.0) as con:
            prior = con.execute(
                "SELECT source_size_bytes,source_mtime_ns,config_sha256,status,attempt_count "
                "FROM audio_processing_items WHERE source_content_id=?",
                (source_id,),
            ).fetchone()
            unchanged = bool(prior and prior[:3] == (stat_size, stat_mtime_ns, config_sha))
            status = str(prior[3]) if prior else ""
            if unchanged and status in {"success", "no_speech", "no_audio"}:
                continue
            con.execute(
                """INSERT INTO audio_processing_items(
                       source_content_id,source_path,source_size_bytes,source_mtime_ns,
                       config_sha256,status
                   ) VALUES(?,?,?,?,?,'pending')
                   ON CONFLICT(source_content_id) DO UPDATE SET
                       source_path=excluded.source_path,
                       source_size_bytes=excluded.source_size_bytes,
                       source_mtime_ns=excluded.source_mtime_ns,
                       config_sha256=excluded.config_sha256,status='pending',
                       last_error_code='',last_error_message='',updated_at=CURRENT_TIMESTAMP""",
                (source_id, str(path), stat_size, stat_mtime_ns, config_sha),
            )
            con.commit()
    with sqlite3.connect(db, timeout=30.0) as con:
        queue = [row for row in con.execute(
            """SELECT source_content_id,source_path FROM audio_processing_items
               WHERE config_sha256=? AND status IN ('pending','failed')
               ORDER BY source_content_id""",
            (config_sha,),
        ).fetchall() if str(row[0]) in selected_source_ids and str(row[0]) in runnable_source_ids]
    if not queue and offline_unfinished_ids:
        failure = {
            "contract": RUNTIME_CONTRACT,
            "event": "stage_failed",
            "reason_code": "SOURCE_VOLUME_OFFLINE",
            "failed": len(offline_unfinished_ids),
            "error_message": "素材盘未连接；已完成项目保持不变，连接后只重试未完成项目",
        }
        append_failure(out / "stage_failures.jsonl", failure)
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        return 2
    total = len(videos)
    with sqlite3.connect(db, timeout=30.0) as con:
        existing_rows = [row for row in con.execute(
            "SELECT source_content_id,status,attempt_count FROM audio_processing_items "
            "WHERE config_sha256=?",
            (config_sha,),
        ) if str(row[0]) in selected_source_ids]
    success = sum(
        1 for _source_id, status, _attempts in existing_rows
        if str(status) in {"success", "no_speech", "no_audio"}
    )
    failed = sum(
        1 for _source_id, status, attempts in existing_rows
        if str(status) == "failed" and int(attempts or 0) >= args.max_item_attempts
    )
    skipped = 0
    run_id = "audio_" + hashlib.sha256(f"{db}:{config_sha}:{time.time_ns()}".encode()).hexdigest()[:24]
    with sqlite3.connect(db, timeout=30.0) as con:
        con.execute(
            "INSERT INTO audio_enrichment_runs(run_id,contract_version,config_sha256,source_video_count,status) "
            "VALUES(?,?,?,?, 'running')",
            (run_id, CONTRACT, config_sha, total),
        )
        con.commit()
    completed_count = success + failed
    active_limit = min(args.workers, len(queue))
    progress(
        completed_count, total, success, skipped, failed,
        configured_workers=args.workers, actual_workers=active_limit,
    )

    def submit_item(
        executor: concurrent.futures.ThreadPoolExecutor,
        source_id: str,
        source_path: str,
    ) -> concurrent.futures.Future[dict[str, Any]]:
        item_out = out / "items" / hashlib.sha256(str(source_id).encode()).hexdigest()[:20]
        item_out.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db, timeout=30.0) as con:
            con.execute(
                "UPDATE audio_processing_items SET status='running',attempt_count=attempt_count+1,"
                "started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE source_content_id=?",
                (source_id,),
            )
            con.commit()
        pilot_command = [
            str(audio_python), str(args.audio_pilot_script),
            "--video", str(source_path), "--source-root", str(Path(source_path).parent),
            "--source-content-id", str(source_id), "--output-dir", str(item_out),
            "--ffmpeg", str(args.ffmpeg), "--ffprobe", str(args.ffprobe),
            "--silero-root", str(args.silero_root), "--whisper-model", str(args.whisper_model),
            "--deep-filter-executable", str(args.deep_filter_executable),
            "--deep-filter-model", str(args.deep_filter_model),
            "--enhancement-failure-policy", "fallback", "--allow-no-audio",
        ]
        manifest_row = preextracted.get(str(source_id)) or {}
        manifest_status = str(manifest_row.get("status") or "")
        manifest_audio = str(manifest_row.get("audio_path") or "")
        if manifest_status == "ready" and manifest_audio:
            audio_path = Path(manifest_audio)
            if audio_path.is_file():
                pilot_command.extend(["--audio-input", str(audio_path)])
                spool_paths[str(source_id)] = audio_path
        elif manifest_status == "no_audio_stream":
            pilot_command.append("--known-no-audio")
        return executor.submit(run_pilot_process, pilot_command, item_out)

    queue_iter = iter((str(row[0]), str(row[1])) for row in queue)
    running: dict[concurrent.futures.Future[dict[str, Any]], tuple[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for _ in range(active_limit):
            source_id, source_path = next(queue_iter)
            running[submit_item(executor, source_id, source_path)] = (source_id, source_path)
        while running:
            done, _pending = concurrent.futures.wait(
                running, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                source_id, source_path = running.pop(future)
                try:
                    report = future.result()
                    report_path = (
                        out / "items" / hashlib.sha256(source_id.encode()).hexdigest()[:20]
                        / "audio_search_pilot.json"
                    )
                    evidence = list(report.get("search_evidence") or [])
                    status = (
                        "no_audio" if int(report.get("audio_stream_count") or 0) == 0
                        else ("success" if evidence else "no_speech")
                    )
                    with sqlite3.connect(db, timeout=30.0) as con:
                        con.execute("PRAGMA foreign_keys=ON")
                        old = [row[0] for row in con.execute(
                            "SELECT evidence_id FROM audio_speech_evidence WHERE source_content_id=?",
                            (source_id,),
                        )]
                        if old:
                            con.executemany(
                                "DELETE FROM audio_text_embeddings WHERE evidence_id=?",
                                [(item,) for item in old],
                            )
                        con.execute(
                            "DELETE FROM audio_speech_evidence WHERE source_content_id=?",
                            (source_id,),
                        )
                        for item in evidence:
                            text = str(item["text"]).strip()
                            evidence_id = "speech_" + hashlib.sha256(
                                f"{source_id}:{item['start_time_ms']}:{item['end_time_ms']}:{text}".encode()
                            ).hexdigest()[:24]
                            con.execute(
                                """INSERT INTO audio_speech_evidence(
                                       evidence_id,source_content_id,source_path,start_time_ms,end_time_ms,
                                       hit_time_ms,transcript_text,language,preview_windows_json,transcript_sha256
                                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    evidence_id, source_id, source_path,
                                    int(item["start_time_ms"]), int(item["end_time_ms"]),
                                    int(item["hit_time_ms"]), text,
                                    str(item.get("language") or "") or None,
                                    json.dumps(
                                        item["preview_windows"], ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                    hashlib.sha256(text.encode()).hexdigest(),
                                ),
                            )
                        con.execute(
                            """UPDATE audio_processing_items SET status=?,transcript_segment_count=?,
                               output_report_path=?,last_error_code='',last_error_message='',
                               finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                               WHERE source_content_id=?""",
                            (status, len(evidence), str(report_path), source_id),
                        )
                        con.commit()
                    spool_path = spool_paths.get(source_id)
                    if spool_path is not None:
                        try:
                            spool_path.unlink(missing_ok=True)
                        except OSError as exc:
                            append_failure(out / "stage_failures.jsonl", {
                                "contract": RUNTIME_CONTRACT,
                                "event": "temporary_audio_cleanup_warning",
                                "source_content_id": source_id,
                                "error_code": type(exc).__name__,
                                "error_message": str(exc)[:1000],
                            })
                    success += 1
                except Exception as exc:
                    with sqlite3.connect(db, timeout=30.0) as con:
                        attempt_count = int(con.execute(
                            "SELECT attempt_count FROM audio_processing_items "
                            "WHERE source_content_id=?",
                            (source_id,),
                        ).fetchone()[0])
                    will_retry = attempt_count < args.max_item_attempts
                    failure = {
                        "contract": RUNTIME_CONTRACT,
                        "event": "stage_item_failed",
                        "source_content_id": source_id,
                        "current_item": source_path,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc)[:2000],
                        "attempt_count": attempt_count,
                        "max_item_attempts": args.max_item_attempts,
                        "will_retry": will_retry,
                    }
                    append_failure(out / "stage_failures.jsonl", failure)
                    print(json.dumps(failure, ensure_ascii=False), flush=True)
                    with sqlite3.connect(db, timeout=30.0) as con:
                        con.execute(
                            """UPDATE audio_processing_items SET status=?,last_error_code=?,
                               last_error_message=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                               WHERE source_content_id=?""",
                            (
                                "pending" if will_retry else "failed",
                                type(exc).__name__, str(exc)[:4000], source_id,
                            ),
                        )
                        con.commit()
                    if will_retry:
                        running[submit_item(executor, source_id, source_path)] = (
                            source_id, source_path,
                        )
                        progress(
                            completed_count, total, success, skipped, failed,
                            source_path, configured_workers=args.workers,
                            actual_workers=len(running),
                        )
                        continue
                    failed += 1
                completed_count += 1
                try:
                    next_source_id, next_source_path = next(queue_iter)
                except StopIteration:
                    pass
                else:
                    running[
                        submit_item(executor, next_source_id, next_source_path)
                    ] = (next_source_id, next_source_path)
                progress(
                    completed_count, total, success, skipped, failed, source_path,
                    configured_workers=args.workers, actual_workers=len(running),
                )

    embedding = subprocess.run([
        str(args.embedding_python), str(args.embedding_script), "--db", str(db),
        "--model", str(args.embedding_model), "--confirm-central-db-write",
    ], text=True, capture_output=True, env=child_runtime_environment())
    with sqlite3.connect(db, timeout=30.0) as con:
        counts = dict(con.execute(
            "SELECT status,COUNT(*) FROM audio_processing_items "
            "WHERE config_sha256=? GROUP BY status",
            (config_sha,),
        ).fetchall())
        segment_count = int(con.execute("SELECT COUNT(*) FROM audio_speech_evidence").fetchone()[0])
        vector_count = int(con.execute(
            "SELECT COUNT(*) FROM audio_text_embeddings WHERE status='success'"
        ).fetchone()[0])
        terminal_failed_count = int(con.execute(
            "SELECT COUNT(*) FROM audio_processing_items "
            "WHERE config_sha256=? AND status='failed' AND attempt_count>=?",
            (config_sha, args.max_item_attempts),
        ).fetchone()[0])
        retryable_failed_count = int(con.execute(
            "SELECT COUNT(*) FROM audio_processing_items "
            "WHERE config_sha256=? AND status='failed' AND attempt_count<?",
            (config_sha, args.max_item_attempts),
        ).fetchone()[0])
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        run_succeeded = (
            embedding.returncode == 0 and vector_count == segment_count
            and retryable_failed_count == 0 and integrity == "ok"
            and foreign_keys == 0
        )
        final_status = "success" if run_succeeded else "failed"
        con.execute(
            """UPDATE audio_enrichment_runs SET completed_video_count=?,speech_video_count=?,
               no_speech_video_count=?,no_audio_video_count=?,failed_video_count=?,
               transcript_segment_count=?,status=?,finished_at=CURRENT_TIMESTAMP,error_message=?
               WHERE run_id=?""",
            (
                sum(int(counts.get(key, 0)) for key in ("success", "no_speech", "no_audio"))
                + terminal_failed_count,
                int(counts.get("success", 0)), int(counts.get("no_speech", 0)),
                int(counts.get("no_audio", 0)), int(counts.get("failed", 0)),
                segment_count, final_status,
                embedding.stderr[-2000:] if embedding.returncode else "", run_id,
            ),
        )
        con.commit()
    unprocessed_json, unprocessed_csv, unprocessed_rows = write_unprocessed_reports(
        db, out, config_sha, args.max_item_attempts,
    )
    retained_spool_count = sum(
        1 for path in spool_paths.values() if path.is_file()
    )
    summary = {
        "contract": CONTRACT,
        "status": (
            "COMPLETED_WITH_WARNINGS"
            if run_succeeded and unprocessed_rows else final_status.upper()
        ),
        "run_id": run_id,
        "source_video_count": total, "counts": counts,
        "transcript_segment_count": segment_count, "vector_count": vector_count,
        "integrity_check": integrity, "foreign_key_error_count": foreign_keys,
        "source_read_only": True, "non_speech_policy": "ignored_by_product_scope",
        "temporary_audio_cleaned": retained_spool_count == 0,
        "retained_audio_file_count": retained_spool_count,
        "preextracted_audio_manifest": (
            str(args.preextracted_audio_manifest.resolve())
            if args.preextracted_audio_manifest else None
        ),
        "retention_policy": "retain_transcript_timestamps_vectors_only",
        "max_item_attempts": args.max_item_attempts,
        "terminal_unprocessed_count": terminal_failed_count,
        "retryable_failed_count": retryable_failed_count,
        "unprocessed_report_json": str(unprocessed_json),
        "unprocessed_report_csv": str(unprocessed_csv),
    }
    (out / "audio_enrichment_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if run_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
