"""Read-only, current-only video content timeline projection.

The timeline intentionally aggregates already durable V2 evidence.  It never
claims shot boundaries and never starts a model, index, or schema migration.
"""
from __future__ import annotations

import json
import os
import sqlite3
import signal
import threading
import time
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote


CONTRACT = "media_archive_video_content_timeline_v1"
TIME_PROXIMITY_GROUPING_MS = 15_000


def _json(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _tables(con: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


@contextmanager
def _open_readonly(path: Path):
    """One fixed read snapshot, targeted exact ancestry, cancellable SQLite VM."""
    resolved = Path(path).expanduser().resolve(strict=True)
    owner = os.getppid()
    deadline = time.monotonic() + 45  # Existing owned-helper budget, not extended.
    cancelled = False
    def cancel(*_):
        nonlocal cancelled
        cancelled = True  # Signal handler must not acquire locks or set Event.
    def interrupted():
        nonlocal cancelled
        cancelled = cancelled or os.getppid() != owner or time.monotonic() >= deadline
        return cancelled
    previous = None
    owns_signal = threading.current_thread() is threading.main_thread()
    con = None
    try:
        if owns_signal:
            previous = signal.signal(signal.SIGTERM, cancel)
        uri = f"file:{quote(resolved.as_posix())}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        con.set_progress_handler(lambda: int(interrupted()), 1000)
        con.execute("BEGIN")
        # Existing exact root-relative predicate includes cross-source ancestors,
        # missing parents, revisions, statuses and cycles. Only TEMP schema changes.
        from .v2_current_jobs_cache import correlated_predicate
        con.execute("CREATE TEMP VIEW v2_current_jobs AS SELECT j.* FROM main.v2_jobs j "
                    "WHERE j.status<>'SUPERSEDED' AND " + correlated_predicate(con))
        con.execute("PRAGMA query_only=ON")
        yield con
    except sqlite3.OperationalError as error:
        if interrupted():
            raise RuntimeError("视频内容读取已取消或超过现有45秒预算；数据未修改") from error
        raise
    finally:
        if con is not None:
            con.close()
        if owns_signal:
            signal.signal(signal.SIGTERM, previous)


def _source_metadata(con: sqlite3.Connection, source_id: str) -> tuple[str, dict[str, Any], str]:
    row = con.execute(
        """SELECT active.source_revision, metadata.path, metadata.metadata
           FROM v2_active_sources active
           JOIN v2_source_metadata metadata
             ON metadata.source_id=active.source_id
            AND metadata.source_revision=active.source_revision
           WHERE active.source_id=?""",
        (source_id,),
    ).fetchone()
    if row is None:
        raise ValueError("current_video_source_not_found")
    revision = str(row["source_revision"])
    if revision.startswith(("aborted:", "fenced:")):
        raise ValueError("current_video_source_not_available")
    metadata = _json(row["metadata"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    media_type = str(metadata.get("media_type") or metadata.get("parent_media_kind") or "video").lower()
    if media_type != "video":
        raise ValueError("video_content_timeline_requires_video_source")
    return revision, metadata, str(row["path"] or "")


def _document_evidence(con: sqlite3.Connection, source_id: str, revision: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """SELECT d.document_id,d.artifact_id,d.start_ms,d.end_ms,d.document_type,d.text,
                  d.provenance,d.raw_result_ref,j.stage
           FROM v2_documents d JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref
           WHERE d.source_id=? AND d.source_revision=? AND j.source_id=?
             AND j.source_revision=? AND j.status='DONE'
             AND d.document_type IN ('qwen','ocr','whisper')
           ORDER BY COALESCE(d.start_ms,0),COALESCE(d.end_ms,0),d.document_id""",
        (source_id, revision, source_id, revision),
    ).fetchall()
    result = []
    for row in rows:
        start = max(0, int(row["start_ms"] or 0))
        end = max(start, int(row["end_ms"] or start))
        kind = str(row["document_type"])
        result.append({
            "kind": kind, "stage": str(row["stage"]), "start_time_ms": start, "end_time_ms": end,
            "text": str(row["text"] or "").strip(),
            "evidence_ref": {"execution_key": str(row["raw_result_ref"]), "document_id": str(row["document_id"]),
                             "artifact_id": row["artifact_id"], "source_revision": revision, "stage": str(row["stage"]),
                             "provenance": _json(row["provenance"], {})},
        })
    return result


def _visual_evidence(con: sqlite3.Connection, source_id: str, revision: str) -> tuple[list[dict[str, Any]], int]:
    rows = con.execute(
        """SELECT j.execution_key,j.item_id,j.result_json,a.time_ms
           FROM v2_current_jobs j JOIN v2_artifacts a ON a.artifact_id=j.item_id
           WHERE j.source_id=? AND j.source_revision=? AND j.status='DONE'
             AND j.stage='yoloe' AND a.source_id=? AND a.revision=?
           ORDER BY a.time_ms,j.execution_key""",
        (source_id, revision, source_id, revision),
    ).fetchall()
    result = []
    for row in rows:
        payload = _json(row["result_json"], {})
        detections = payload.get("detections", []) if isinstance(payload, dict) else []
        objects = [{"label": str(item.get("label") or ""), "confidence": item.get("confidence")}
                   for item in detections if isinstance(item, dict) and str(item.get("label") or "")]
        if not objects:
            continue
        point = max(0, int(row["time_ms"] or 0))
        result.append({
            "kind": "objects", "stage": "yoloe", "start_time_ms": point, "end_time_ms": point,
            "objects": objects,
            "evidence_ref": {"execution_key": str(row["execution_key"]), "artifact_id": str(row["item_id"]),
                             "source_revision": revision, "stage": "yoloe"},
        })
    openclip_count = int(con.execute(
        """SELECT count(*) FROM v2_current_jobs j JOIN v2_artifacts a ON a.artifact_id=j.item_id
           WHERE j.source_id=? AND j.source_revision=? AND j.status='DONE' AND j.stage='openclip'
             AND a.source_id=? AND a.revision=?""", (source_id, revision, source_id, revision)
    ).fetchone()[0])
    return result, openclip_count


def _people_evidence(con: sqlite3.Connection, source_id: str, revision: str, tables: set[str]) -> list[dict[str, Any]]:
    required = {"v2_fusion", "v2_observation_heads"}
    if not required <= tables:
        return []
    rows = con.execute(
        """SELECT f.artifact_id,f.start_ms,f.end_ms,f.result_ref,f.payload
           FROM v2_fusion f WHERE f.source_id=? AND f.source_revision=? AND f.kind='person'
             AND EXISTS(SELECT 1 FROM v2_observation_heads h
                        WHERE h.observation_id=json_extract(f.payload, '$.observation_id'))
           ORDER BY f.start_ms,f.link_id""", (source_id, revision)
    ).fetchall()
    result = []
    for row in rows:
        payload = _json(row["payload"], {})
        if not isinstance(payload, dict):
            continue
        person_id = str(payload.get("global_person_id") or payload.get("source_person_id") or "")
        if not person_id:
            continue
        start = max(0, int(row["start_ms"] or 0))
        end = max(start, int(row["end_ms"] or start))
        result.append({
            "kind": "people", "stage": "person", "start_time_ms": start, "end_time_ms": end,
            "people": [{"person_id": person_id, "identity_status": payload.get("identity_status", "UNKNOWN")}],
            "evidence_ref": {"execution_key": str(row["result_ref"]), "artifact_id": row["artifact_id"],
                             "source_revision": revision, "stage": "person",
                             "observation_id": payload.get("observation_id")},
        })
    return result


def _minute_buckets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["kind"] == "whisper" and event.get("text"):
            buckets[event["start_time_ms"] // 60_000].append(event)
    output = []
    for minute in sorted(buckets):
        rows = sorted(buckets[minute], key=lambda row: (row["start_time_ms"], row["end_time_ms"], row["evidence_ref"]["document_id"]))
        output.append({"start_time_ms": minute * 60_000, "end_time_ms": max(row["end_time_ms"] for row in rows),
                       "text": "，".join(row["text"] for row in rows),
                       "evidence_refs": [row["evidence_ref"] for row in rows]})
    return output


def _segments(events: list[dict[str, Any]], gap_ms: int) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for event in sorted(events, key=lambda row: (row["start_time_ms"], row["end_time_ms"], row["kind"], json.dumps(row["evidence_ref"], sort_keys=True))):
        if not groups or event["start_time_ms"] - max(row["end_time_ms"] for row in groups[-1]) > gap_ms:
            groups.append([])
        groups[-1].append(event)
    output = []
    for index, rows in enumerate(groups, 1):
        by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_kind[row["kind"]].append(row)
        refs = []
        seen = set()
        for row in rows:
            marker = json.dumps(row["evidence_ref"], sort_keys=True, ensure_ascii=False)
            if marker not in seen:
                seen.add(marker); refs.append(row["evidence_ref"])
        transcript = "，".join(row["text"] for row in by_kind.get("whisper", []) if row.get("text"))
        qwen = "；".join(row["text"] for row in by_kind.get("qwen", []) if row.get("text"))
        ocr = "；".join(row["text"] for row in by_kind.get("ocr", []) if row.get("text"))
        objects = [obj for row in by_kind.get("objects", []) for obj in row.get("objects", [])]
        people = [person for row in by_kind.get("people", []) for person in row.get("people", [])]
        output.append({
            "start_time_ms": min(row["start_time_ms"] for row in rows),
            "end_time_ms": max(row["end_time_ms"] for row in rows),
            "title": f"内容片段 {index}", "short_description": qwen[:240] or transcript[:240] or "",
            "visual_description": qwen, "transcript_text": transcript, "transcript_summary": transcript[:500],
            "objects": objects, "ocr_text": ocr, "people": people, "evidence_refs": refs,
            "provenance": {"source_revision": rows[0]["evidence_ref"]["source_revision"],
                           "evidence_types": sorted(kind for kind, values in by_kind.items() if values)},
            "confidence": None,
            "evidence_availability": "MULTIMODAL" if len(by_kind) > 1 else ("TRANSCRIPT_ONLY" if "whisper" in by_kind else "VISUAL_ONLY"),
        })
    return output


def _overview(segments: list[dict[str, Any]]) -> str:
    transcript_count = sum(bool(row["transcript_text"]) for row in segments)
    visual_count = sum(bool(row["visual_description"]) for row in segments)
    labels = sorted({str(obj.get("label")) for row in segments for obj in row["objects"] if obj.get("label")})
    parts = []
    if transcript_count:
        parts.append(f"已有 {transcript_count} 个含转写的内容片段")
    if visual_count:
        parts.append(f"已有 {visual_count} 个当前视觉描述")
    if labels:
        parts.append("当前对象检测包括：" + "、".join(labels[:8]))
    return "；".join(parts)


def build_video_content_timeline(db_path: Path, source_id: str, *, expected_revision: str | None = None, grouping_gap_ms: int = TIME_PROXIMITY_GROUPING_MS) -> dict[str, Any]:
    """Return the dynamic current-only contract for one active video source."""
    if not str(source_id or "").strip():
        raise ValueError("video_content_timeline_source_id_required")
    if grouping_gap_ms < 0:
        raise ValueError("video_content_timeline_invalid_grouping_gap")
    with _open_readonly(Path(db_path)) as con:
        tables = _tables(con)
        required = {"v2_active_sources", "v2_source_metadata", "v2_current_jobs", "v2_documents", "v2_artifacts"}
        if not required <= tables:
            raise ValueError("video_content_timeline_v2_authority_unavailable")
        revision, metadata, source_path = _source_metadata(con, str(source_id))
        if expected_revision is not None and revision != expected_revision:
            raise ValueError("spoken_catalog_revision_changed_reload_required")
        from .v2_release_search import enabled,filter_rows
        if enabled(con) and not filter_rows(con,[{'source_id':str(source_id),'source_revision':revision}]):
            raise ValueError("video_content_not_current_published")
        documents = _document_evidence(con, str(source_id), revision)
        visual, openclip_count = _visual_evidence(con, str(source_id), revision)
        people = _people_evidence(con, str(source_id), revision, tables)
    events = documents + visual + people
    minutes = _minute_buckets(documents)
    segments = _segments(events, grouping_gap_ms)
    duration = metadata.get("video_duration_ms", metadata.get("duration_ms"))
    if duration is None and metadata.get("duration_seconds") is not None:
        duration = int(float(metadata["duration_seconds"]) * 1000)
    try:
        duration = max(0, int(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    path = str(source_path or metadata.get("source_path") or "")
    relative = str(metadata.get("source_relative_path") or metadata.get("relative_path") or "")
    key_moments = [{"start_time_ms": row["start_time_ms"], "end_time_ms": row["end_time_ms"],
                    "reason_types": row["provenance"]["evidence_types"], "evidence_refs": row["evidence_refs"]}
                   for row in sorted(segments, key=lambda row: (-len(row["evidence_refs"]), row["start_time_ms"]))[:5]]
    return {
        "status": "PASS", "contract": CONTRACT, "source_id": str(source_id), "source_revision": revision,
        "file_name": Path(relative or path).name, "relative_path": relative, "source_path": path,
        "source_online": False, "source_availability": "unchecked", "can_open_original": bool(path), "video_duration_ms": duration,
        "segments": segments, "transcript_minutes": minutes, "key_moments": key_moments,
        "overview": _overview(segments), "qa_clues": [], "qa_clue_policy": "DEFERRED_CURRENT_V2_ADAPTATION",
        "evidence_summary": {"whisper_document_count": sum(row["kind"] == "whisper" for row in documents),
                             "qwen_document_count": sum(row["kind"] == "qwen" for row in documents),
                             "ocr_document_count": sum(row["kind"] == "ocr" for row in documents),
                             "yoloe_evidence_count": len(visual), "openclip_current_job_count": openclip_count,
                             "person_evidence_count": len(people), "evidence_total": len(events)},
        "segment_grouping_policy": {"kind": "TIME_PROXIMITY_GROUPING", "gap_ms": grouping_gap_ms,
                                    "not_shot_boundary_detection": True},
        "database_write": False, "model_run": False, "network_used": False, "original_media_read": False,
    }
