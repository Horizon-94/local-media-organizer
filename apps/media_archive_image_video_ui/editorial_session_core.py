"""Portable Script/Session Core for RC2 Editorial.

This module deliberately contains no database, search, model, preview, or
candidate-retrieval dependency.  Search candidates can be attached later as
versioned snapshots; human decisions remain the authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCRIPT_CONTRACT = "editorial_script_v1"
SESSION_CONTRACT = "editorial_session_v2"
MAX_SCRIPT_CHARS = 20_000
MAX_SESSION_BYTES = 128 * 1024 * 1024
HUMAN_STATUSES = {"UNREVIEWED", "SELECTED", "BACKUP", "REJECTED", "MANUAL"}
_CHAPTER_CARD_RE = re.compile(
    r"^第\s*[一二三四五六七八九十百千万零〇两0-9]+\s*章(?:\s*[:：·—-]\s*.*)?$"
)


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _purpose(text: str, order: int, total: int) -> tuple[str, list[str]]:
    if order == 1:
        return "开场引入", ["钩子", "建立", "主叙述"]
    if "？" in text or "?" in text:
        return "提出问题", ["钩子", "建立"]
    if any(word in text for word in ("但是", "然而", "反而", "却")):
        return "转折与矛盾", ["证据", "反应", "环境/呼吸"]
    if any(word in text for word in ("以前", "曾经", "历史", "多年", "过去", "小时候")):
        return "回忆与背景", ["证据", "插入/细节", "环境/呼吸"]
    if any(word in text for word in ("决定", "开始", "尝试", "行动", "回来")):
        return "行动与选择", ["主叙述", "动作覆盖", "收束"]
    if any(word in text for word in ("看", "听", "风", "光", "颜色", "变化", "慢慢")):
        return "观察与感受", ["证据", "插入/细节", "环境/呼吸"]
    if order == total:
        return "段落收束", ["收束", "证据", "环境/呼吸"]
    return "事实说明", ["主叙述", "证据", "插入/细节"]


def _split_long_chunk(text: str, max_chars: int = 42) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    clauses = [part for part in re.split(r"(?<=[，,：:])", text) if part]
    if len(clauses) <= 1:
        return [text]
    rows: list[str] = []
    current = ""
    for clause in clauses:
        if current and len(current) + len(clause) > max_chars:
            rows.append(current)
            current = clause
        else:
            current += clause
    if current:
        rows.append(current)
    return rows


def _is_chapter_card(text: str) -> bool:
    if "\n" in text or "\r" in text:
        return False
    compact = _clean(text)
    return len(compact) <= 80 and _CHAPTER_CARD_RE.fullmatch(compact) is not None


def normalize_script(script: str) -> str:
    if not isinstance(script, str):
        raise ValueError("script must be text")
    cleaned = script.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise ValueError("script is empty")
    if len(cleaned) > MAX_SCRIPT_CHARS:
        raise ValueError("script exceeds 20000 characters")
    return cleaned


def split_script(script: str, *, track: str = "documentary") -> list[dict[str, Any]]:
    """Split only text; this function never consults a library or database."""
    if track not in {"documentary", "short_video"}:
        raise ValueError("unsupported track")
    cleaned = normalize_script(script)
    explicit = "\u2063" in cleaned
    if explicit:
        source = [
            _clean(part) for part in cleaned.split("\u2063")
            if _clean(part) and not _is_chapter_card(part)
        ]
    else:
        source = [
            _clean(part) for part in re.split(
                r"(?<=[。！？!?；;])(?![】》」』”’）)])\s*|\n+", cleaned
            ) if _clean(part) and not _is_chapter_card(part)
        ]
    if not source:
        source = [cleaned]
    chunks = source if explicit else [part for item in source for part in _split_long_chunk(item)]
    total = len(chunks)
    beats: list[dict[str, Any]] = []
    for order, text in enumerate(chunks, 1):
        purpose, roles = _purpose(text, order, total)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        beats.append({
            "beat_id": f"beat-{order:04d}-{digest}",
            "order": order,
            "text": text,
            "purpose": purpose,
            "estimated_narration_ms": max(1, round(len(text) / 4 * 1000 / 2.5)),
            "context_before": [],
            "context_after": [],
            "required_roles": roles,
            "visual_strategy": {},
            "selection_requirement": {},
            "user_note": "",
            "candidate_snapshots": [],
        })
    for index, beat in enumerate(beats):
        beat["context_before"] = [row["text"] for row in beats[max(0, index - 2):index]]
        beat["context_after"] = [row["text"] for row in beats[index + 1:index + 3]]
    return beats


def script_identity(script: str) -> str:
    return hashlib.sha256(normalize_script(script).encode("utf-8")).hexdigest()


def _finite_number(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _validate_range(start: object, end: object) -> tuple[int, int]:
    start_ms = int(_finite_number(start, "start_ms"))
    end_ms = int(_finite_number(end, "end_ms"))
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("invalid trim range")
    return start_ms, end_ms


def validate_candidate_snapshot(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate snapshot must be an object")
    result = dict(candidate)
    candidate_id = str(result.get("candidate_id") or "").strip()
    if not candidate_id:
        raise ValueError("candidate_id is required")
    result["candidate_id"] = candidate_id
    status = str(result.get("human_status") or "UNREVIEWED").upper()
    if status not in HUMAN_STATUSES:
        raise ValueError(f"unknown human status: {status}")
    result["human_status"] = status
    if "locked" in result and not isinstance(result["locked"], bool):
        raise ValueError("locked must be boolean")
    if "score" in result and result["score"] is not None:
        result["score"] = _finite_number(result["score"], "score")
    if ("start_ms" in result) != ("end_ms" in result):
        raise ValueError("start_ms and end_ms must be provided together")
    if "start_ms" in result:
        result["start_ms"], result["end_ms"] = _validate_range(result["start_ms"], result["end_ms"])
    reasons = result.get("match_reasons", [])
    if not isinstance(reasons, list) or any(not isinstance(row, Mapping) for row in reasons):
        raise ValueError("match_reasons must be a list of objects")
    provenance = result.get("provenance", {})
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be an object")
    result["match_reasons"] = [dict(row) for row in reasons]
    result["provenance"] = dict(provenance)
    result["manual_origin"] = bool(result.get("manual_origin", False))
    return result


def new_session(
    script: str,
    *,
    script_revision: int = 1,
    track: str = "documentary",
    session_id: str | None = None,
    timeline_name: str = "Editorial rough cut",
    frame_rate: str = "30000/1001",
) -> dict[str, Any]:
    if int(script_revision) < 1:
        raise ValueError("script_revision must be positive")
    normalized = normalize_script(script)
    beats = split_script(normalized, track=track)
    sid = session_id or str(uuid.uuid4())
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("session_id is required")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "contract": SESSION_CONTRACT,
        "contract_version": SESSION_CONTRACT,
        "session_id": sid,
        "created_at": now,
        "updated_at": now,
        "script_contract": SCRIPT_CONTRACT,
        "script": normalized,
        "script_id": script_identity(normalized),
        "script_revision": int(script_revision),
        "track": track,
        "beats": beats,
        "active_beat_id": beats[0]["beat_id"] if beats else None,
        "decisions": {},
        "cut_overrides": {},
        "locked_cuts": [],
        "timeline_name": str(timeline_name),
        "frame_rate": str(frame_rate),
        "include_backups": True,
        "project_label": "",
        "library_reference": None,
        "candidate_snapshots": [],
        "unresolved_items": [],
    }


def _decision_key(beat_id: str, candidate_id: str) -> str:
    return f"{beat_id}::{candidate_id}"


def set_decision(
    session: dict[str, Any],
    beat_id: str,
    candidate_id: str,
    status: str,
    *,
    locked: bool | None = None,
    trim: tuple[int, int] | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    validate_session(session)
    original = json.loads(json.dumps(session, ensure_ascii=False))
    status = str(status).upper()
    if status not in HUMAN_STATUSES:
        raise ValueError(f"unknown human status: {status}")
    beat = next((row for row in session["beats"] if row["beat_id"] == beat_id), None)
    if beat is None:
        raise ValueError("unknown beat_id")
    if trim is not None:
        _validate_range(*trim)
        session.setdefault("cut_overrides", {})[_decision_key(beat_id, candidate_id)] = [int(trim[0]), int(trim[1])]
    key = _decision_key(beat_id, candidate_id)
    decisions = dict(session.get("decisions") or {})
    old = dict(decisions.get(key) or {})
    old.update({"beat_id": beat_id, "candidate_id": candidate_id, "status": status,
                "manual_origin": bool(manual or old.get("manual_origin", False))})
    if locked is not None:
        old["locked"] = bool(locked)
    decisions[key] = old
    if old.get("locked"):
        locked_cuts = set(session.get("locked_cuts") or [])
        locked_cuts.add(key)
        session["locked_cuts"] = sorted(locked_cuts)
    elif locked is False:
        session["locked_cuts"] = [item for item in session.get("locked_cuts", []) if item != key]
    session["decisions"] = decisions
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        validate_session(session)
    except Exception:
        session.clear()
        session.update(original)
        raise
    return session


def attach_candidate_snapshot(session: dict[str, Any], beat_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    validate_session(session)
    if not any(row["beat_id"] == beat_id for row in session["beats"]):
        raise ValueError("unknown beat_id")
    snapshot = validate_candidate_snapshot(candidate)
    rows = [row for row in session.get("candidate_snapshots", [])
            if (row.get("beat_id"), row.get("candidate_id")) != (beat_id, snapshot["candidate_id"])]
    snapshot["beat_id"] = beat_id
    rows.append(snapshot)
    session["candidate_snapshots"] = rows
    for beat in session["beats"]:
        if beat["beat_id"] == beat_id:
            beat["candidate_snapshots"] = [row for row in rows if row.get("beat_id") == beat_id]
    validate_session(session)
    return session


def validate_session(session: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(session, Mapping):
        raise ValueError("session must be an object")
    if session.get("contract") != SESSION_CONTRACT or session.get("contract_version") != SESSION_CONTRACT:
        raise ValueError("Unsupported editorial session version")
    required = {"session_id", "created_at", "updated_at", "script", "script_id", "script_revision", "beats", "decisions", "cut_overrides", "locked_cuts"}
    missing = sorted(required - set(session))
    if missing:
        raise ValueError("missing session fields: " + ",".join(missing))
    if not str(session["session_id"]).strip():
        raise ValueError("session_id is required")
    if int(session["script_revision"]) < 1:
        raise ValueError("script_revision must be positive")
    normalize_script(str(session["script"]))
    if session["script_id"] != script_identity(str(session["script"])):
        raise ValueError("script_id does not match script")
    beats = session["beats"]
    if not isinstance(beats, list) or not beats:
        raise ValueError("beats must be a non-empty list")
    ids: set[str] = set()
    orders: set[int] = set()
    for beat in beats:
        if not isinstance(beat, Mapping):
            raise ValueError("beat must be an object")
        bid = str(beat.get("beat_id") or "")
        order = int(beat.get("order") or 0)
        if not bid or bid in ids:
            raise ValueError("duplicate or missing beat_id")
        if order in orders or order < 1:
            raise ValueError("invalid or duplicate beat order")
        if not str(beat.get("text") or "").strip():
            raise ValueError("beat text is empty")
        ids.add(bid); orders.add(order)
        snapshots = beat.get("candidate_snapshots", [])
        if not isinstance(snapshots, list):
            raise ValueError("candidate_snapshots must be a list")
        for row in snapshots:
            validate_candidate_snapshot(row)
    if not isinstance(session["decisions"], Mapping):
        raise ValueError("decisions must be an object")
    primary_by_beat: dict[str, int] = {}
    for key, decision in session["decisions"].items():
        if not isinstance(decision, Mapping) or decision.get("beat_id") not in ids:
            raise ValueError("decision references an unknown beat")
        status = str(decision.get("status") or "").upper()
        if status not in HUMAN_STATUSES:
            raise ValueError(f"unknown human status: {status}")
        if status == "SELECTED":
            bid = str(decision["beat_id"])
            primary_by_beat[bid] = primary_by_beat.get(bid, 0) + 1
    if any(count > 1 for count in primary_by_beat.values()):
        raise ValueError("a beat cannot have two SELECTED primary candidates")
    if not isinstance(session["cut_overrides"], Mapping):
        raise ValueError("cut_overrides must be an object")
    for trim in session["cut_overrides"].values():
        if not isinstance(trim, list) or len(trim) != 2:
            raise ValueError("invalid trim override")
        _validate_range(trim[0], trim[1])
    unresolved = session.get("unresolved_items", [])
    if not isinstance(unresolved, list):
        raise ValueError("unresolved_items must be a list")
    snapshots = session.get("candidate_snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("candidate_snapshots must be a list")
    for row in snapshots:
        validate_candidate_snapshot(row)
    return dict(session)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("session contains a non-finite number")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def save_session(session: Mapping[str, Any], path: str | Path) -> Path:
    payload = validate_session(session)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    data = (json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_session(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise ValueError("editorial session file not found")
    if target.stat().st_size > MAX_SESSION_BYTES:
        raise ValueError("editorial session exceeds 128MB")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid editorial session JSON") from exc
    validate_session(payload)
    return payload


def convert_legacy_editorial_session(_: object) -> None:
    """Reserved importer boundary; legacy session conversion is B4 scope."""
    raise NotImplementedError("legacy editorial session conversion is deferred")
