from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

try:
    from .core import CONTRACT_VERSION
    from .editorial_language import analyze_editorial_language
except ImportError:  # Direct-file contract tests.
    from core import CONTRACT_VERSION
    from editorial_language import analyze_editorial_language


REQUIRED_TABLES = {
    "source_assets",
    "derived_assets",
    "stop03_5d_text_documents",
}
ALLOWED_PREVIEW_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def connect_readonly(database: Path) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"central database not found: {path}")
    errors: list[str] = []
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"{path.as_uri()}?{query}", uri=True, timeout=5.0)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return con
        except sqlite3.Error as exc:
            errors.append(str(exc))
    raise sqlite3.OperationalError("readonly_database_open_failed:" + " | ".join(errors))


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _short(value: object, limit: int = 170) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _labels(raw: object) -> list[str]:
    try:
        payload = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    labels: list[str] = []
    if not isinstance(payload, list):
        return labels
    for row in payload:
        if not isinstance(row, dict):
            continue
        label = _clean(row.get("label_zh") or row.get("label"))
        if label and label not in labels:
            labels.append(label)
    return labels


def _concept_terms(qwen_text: str, ocr_text: str, labels: list[str]) -> list[str]:
    terms = list(labels)
    for value in re.findall(r"[“\"]([^”\"]{2,16})[”\"]", qwen_text):
        term = _clean(value).strip("，,。；;：:、 ")
        if term and term not in terms:
            terms.append(term)
    if ocr_text:
        terms.append(_short(ocr_text, 40))
    return terms[:32]


def _role_for(text: str, labels: list[str]) -> str:
    value = text + " " + " ".join(labels)
    if any(word in value for word in ("特写", "细节", "手部", "局部", "纹理", "物件")):
        return "插入/细节"
    if any(word in value for word in ("行走", "移动", "奔跑", "操作", "制作", "动作", "进入", "离开")):
        return "动作覆盖"
    if any(word in value for word in ("表情", "凝视", "低头", "微笑", "哭", "反应")):
        return "反应"
    if any(word in value for word in ("远景", "全景", "街道", "建筑", "村庄", "城市", "现场")):
        return "建立"
    if any(word in value for word in ("海面", "天空", "树林", "田野", "山", "河", "环境", "空镜")):
        return "环境/呼吸"
    if any(word in value for word in ("人", "人物", "男性", "女性", "老人", "儿童")):
        return "主叙述"
    return "证据"


def _pool_for(document_kind: str) -> str:
    if document_kind == "direct_only":
        return "direct"
    if document_kind == "direct_and_propagation":
        return "supplement"
    return "alternative"


def _observations(qwen_text: str, ocr_text: str, labels: list[str]) -> list[str]:
    rows: list[str] = []
    for part in re.split(r"(?:^|\s)[123][）).、]\s*", qwen_text):
        if re.match(r"^检索价值[:：]", part):
            continue
        part = re.sub(r"^(?:概括|元素|检索价值)[:：]\s*", "", _short(part))
        if part not in {"", "无", "无。", "无文字", "无文字。"} and part not in rows:
            rows.append(part)
    if ocr_text:
        rows.append("可见文字：" + _short(ocr_text, 90))
    if labels:
        rows.append("传播标签：" + "、".join(labels[:8]))
    return rows[:3] or ["只有物体标签，缺少完整画面描述"]


def _candidate_from_row(row: sqlite3.Row, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    extras = extras or {}
    qwen_text = _clean(row["qwen_text"])
    ocr_text = _clean(row["ocr_text"])
    labels = _labels(row["propagated_labels_json"])
    concept_terms = _concept_terms(qwen_text, ocr_text, labels)
    combined = " ".join([qwen_text, ocr_text, " ".join(labels)])
    media_type = _clean(row["media_type"])
    raw_time = int(row["time_position_ms"] or 0)
    anchor_ms = max(0, raw_time)
    if media_type == "video":
        start_ms = max(0, anchor_ms - 2000)
        end_ms = anchor_ms + 2000
        time_basis = "sample_anchor_window"
        risks = ["这是抽样帧前后各 2 秒的候选窗口，不是已经验证的镜头边界"]
    else:
        start_ms = 0
        end_ms = 5000
        time_basis = "still_image_hold"
        risks = ["静态图片的 5 秒仅是候选展示时长，需要剪辑者决定实际停留时间"]
    if not qwen_text:
        risks.append("缺少直接画面描述，当前主要依靠传播标签匹配")
    preview_path = Path(str(row["derived_path"] or ""))
    preview_available = preview_path.is_file() and preview_path.suffix.lower() in ALLOWED_PREVIEW_SUFFIXES
    if not preview_available:
        risks.append("派生预览当前不可用；不会回退读取原始素材")

    evidence = []
    if qwen_text:
        evidence.append("已有视觉描述")
    if ocr_text:
        evidence.append("已有 OCR 文字")
    if labels:
        evidence.append("已有传播物体标签：" + "、".join(labels[:6]))
    evidence.append(f"数据库文档类型：{row['document_kind']}")

    document_id = str(row["document_id"])
    source_id = str(row["source_content_id"])
    visual_unit_id = str(row["canonical_visual_unit_id"] or "")
    annotation = (extras.get("annotations") or {}).get(source_id, {})
    technical = (extras.get("technical") or {}).get(visual_unit_id, {})
    queue = (extras.get("candidate_scores") or {}).get(visual_unit_id, {})
    people = (extras.get("people") or {}).get(visual_unit_id, [])
    nearby_audio = ""
    audio_rows = (extras.get("audio") or {}).get(source_id, [])
    if media_type == "video" and audio_rows:
        nearest = min(audio_rows, key=lambda item: abs(int(item[0]) - anchor_ms))
        if abs(int(nearest[0]) - anchor_ms) <= 15_000:
            nearby_audio = _short(nearest[1], 240)
    observations = _observations(qwen_text, ocr_text, labels)
    role = _role_for(combined, labels)
    return {
        "candidate_id": document_id,
        "pool": _pool_for(str(row["document_kind"])),
        "source_content_id": source_id,
        "visual_unit_id": visual_unit_id,
        "source_file": str(row["source_relative_path"]),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "anchor_time_ms": anchor_ms if media_type == "video" else None,
        "time_basis": time_basis,
        "role": role,
        "editorial_language": analyze_editorial_language(combined, current_role=role),
        "display_title": _short(observations[0], 72),
        "observations": observations,
        "evidence": evidence,
        "risks": risks,
        "tags": labels,
        "concept_terms": concept_terms,
        "preferred_tracks": ["documentary", "short_video"],
        "searchable_text": combined,
        "visual": "real-preview" if preview_available else "real-missing",
        "preview_available": preview_available,
        "preview_absolute_path": str(preview_path.resolve()) if preview_available else "",
        "preview_url": f"/api/preview?candidate_id={document_id}" if preview_available else "",
        "media_type": media_type,
        "document_kind": str(row["document_kind"]),
        "evidence_sources": {
            "qwenvl": bool(qwen_text),
            "ocr": bool(ocr_text),
            "yoloe_propagation": bool(labels),
            "nearby_asr": bool(nearby_audio),
            "person_cluster": bool(people),
        },
        "nearby_asr": nearby_audio,
        "person_cluster_ids": people,
        "technical_score": technical.get("technical_score"),
        "high_value_score": queue.get("candidate_score"),
        "favorite": bool(annotation.get("favorite")),
        "user_rating": annotation.get("rating"),
        "user_note": str(annotation.get("note") or ""),
        "user_tags": annotation.get("tags") or [],
        "source_metadata": {
            "extension": str(row["source_extension"] or ""),
            "size_bytes": int(row["source_size_bytes"] or 0),
            "preview_width": int(row["preview_width"] or 0),
            "preview_height": int(row["preview_height"] or 0),
        },
    }


def _optional_evidence(con: sqlite3.Connection, available: set[str]) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "annotations": {}, "technical": {}, "candidate_scores": {}, "audio": {}, "people": {},
    }
    if "user_asset_annotations" in available:
        for row in con.execute("SELECT source_content_id,tags_json,note,favorite,rating FROM user_asset_annotations"):
            try:
                tags = json.loads(str(row["tags_json"] or "[]"))
            except json.JSONDecodeError:
                tags = []
            extras["annotations"][str(row["source_content_id"])] = {
                "tags": tags if isinstance(tags, list) else [],
                "note": str(row["note"] or ""),
                "favorite": bool(row["favorite"]),
                "rating": row["rating"],
            }
    if "visual_identity" in available:
        for row in con.execute("SELECT visual_unit_id,sharpness_score FROM visual_identity"):
            score = row["sharpness_score"]
            extras["technical"][str(row["visual_unit_id"])] = {
                "technical_score": None if score is None else max(0.0, float(score)),
            }
    if "stop03_2_candidate_queue_frozen_v25" in available:
        for row in con.execute(
            """SELECT canonical_visual_unit_id,MAX(candidate_score) candidate_score
               FROM stop03_2_candidate_queue_frozen_v25
               WHERE canonical_visual_unit_id IS NOT NULL GROUP BY canonical_visual_unit_id"""
        ):
            extras["candidate_scores"][str(row["canonical_visual_unit_id"])] = {
                "candidate_score": row["candidate_score"],
            }
    if "audio_speech_evidence" in available:
        for row in con.execute(
            "SELECT source_content_id,hit_time_ms,transcript_text FROM audio_speech_evidence ORDER BY source_content_id,hit_time_ms"
        ):
            extras["audio"].setdefault(str(row["source_content_id"]), []).append(
                (int(row["hit_time_ms"] or 0), str(row["transcript_text"] or ""))
            )
    if {"stop03_1c_face_embeddings", "stop03_1c_person_cluster_members"}.issubset(available):
        for row in con.execute(
            """SELECT f.visual_unit_id,m.person_cluster_id
               FROM stop03_1c_face_embeddings f
               JOIN stop03_1c_person_cluster_members m
                 ON m.run_id=f.run_id AND m.face_id=f.face_id"""
        ):
            values = extras["people"].setdefault(str(row["visual_unit_id"]), [])
            cluster = str(row["person_cluster_id"])
            if cluster not in values:
                values.append(cluster)
    return extras


def load_database_project(
    database: Path,
    *,
    limit: int = 1200,
    include_propagation_only: bool = True,
) -> dict[str, Any]:
    if limit <= 0 or limit > 25000:
        raise ValueError("candidate limit must be between 1 and 25000")
    with connect_readonly(database) as con:
        available = _tables(con)
        missing = sorted(REQUIRED_TABLES - available)
        if missing:
            raise ValueError(f"central database missing required tables: {missing}")
        latest = con.execute(
            """SELECT embedding_run_id
               FROM stop03_5d_text_documents
               GROUP BY embedding_run_id
               ORDER BY MAX(created_at) DESC LIMIT 1"""
        ).fetchone()
        if latest is None:
            raise ValueError("central database has no searchable text documents")
        document_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(stop03_5d_text_documents)")}
        source_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(source_assets)")}
        derived_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(derived_assets)")}
        visual_expr = "d.canonical_visual_unit_id" if "canonical_visual_unit_id" in document_columns else "NULL"
        extension_expr = "s.extension" if "extension" in source_columns else "NULL"
        size_expr = "s.size_bytes" if "size_bytes" in source_columns else "NULL"
        width_expr = "a.width" if "width" in derived_columns else "NULL"
        height_expr = "a.height" if "height" in derived_columns else "NULL"
        rows = con.execute(
            f"""SELECT d.document_id,d.source_content_id,d.media_type,d.time_position_ms,
                      d.document_kind,d.qwen_text,d.ocr_text,d.propagated_labels_json,
                      d.source_relative_path,d.derived_id,a.derived_path,
                      {visual_expr} AS canonical_visual_unit_id,
                      {extension_expr} AS source_extension,{size_expr} AS source_size_bytes,
                      {width_expr} AS preview_width,{height_expr} AS preview_height
               FROM stop03_5d_text_documents AS d
               LEFT JOIN derived_assets AS a ON a.derived_id=d.derived_id
               JOIN source_assets AS s ON s.source_content_id=d.source_content_id
               WHERE d.embedding_run_id=? AND d.quality_status='PASS'
                 AND d.media_type IN ('image','video')
                 AND (?=1 OR d.document_kind!='propagation_only')
                 AND COALESCE(s.is_deleted_or_missing,0)=0
               ORDER BY CASE d.document_kind
                          WHEN 'direct_only' THEN 0
                          WHEN 'direct_and_propagation' THEN 1 ELSE 2 END,
                        d.document_id
               LIMIT ?""",
            (str(latest[0]), 1 if include_propagation_only else 0, limit),
        ).fetchall()
        extras = _optional_evidence(con, available)
    candidates = [_candidate_from_row(row, extras) for row in rows]
    if not candidates:
        raise ValueError("central database has no eligible image/video candidates")
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": "central-database-readonly",
        "project_title": "当前真实素材库 · 1.2.5 只读候选",
        "track": "documentary",
        "script": "人们来到现场，故事从这里开始。\n镜头记录下他们正在做的事情。\n环境与人物的变化推动故事继续发展。",
        "source_mode": "real_database_read_only",
        "database_path": str(Path(database).expanduser().resolve()),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def resolve_preview_path(database: Path, candidate_id: str) -> Path | None:
    if not candidate_id or len(candidate_id) > 200:
        return None
    with connect_readonly(database) as con:
        if not REQUIRED_TABLES.issubset(_tables(con)):
            return None
        row = con.execute(
            """SELECT a.derived_path
               FROM stop03_5d_text_documents AS d
               JOIN derived_assets AS a ON a.derived_id=d.derived_id
               WHERE d.document_id=? LIMIT 1""",
            (candidate_id,),
        ).fetchone()
    if row is None:
        return None
    path = Path(str(row[0] or "")).expanduser().resolve()
    if path.suffix.lower() not in ALLOWED_PREVIEW_SUFFIXES or not path.is_file():
        return None
    return path


def resolve_timeline_sources(database: Path, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(str(value) for value in candidate_ids if value))
    if not unique_ids or len(unique_ids) > 500 or any(len(value) > 200 for value in unique_ids):
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    with connect_readonly(database) as con:
        if not REQUIRED_TABLES.issubset(_tables(con)):
            return {}
        rows = con.execute(
            f"""SELECT d.document_id,d.source_content_id,d.source_relative_path,d.media_type,
                       s.absolute_path,s.file_name,s.volume_id,s.online_status
                FROM stop03_5d_text_documents AS d
                JOIN source_assets AS s ON s.source_content_id=d.source_content_id
                WHERE d.document_id IN ({placeholders})
                  AND COALESCE(s.is_deleted_or_missing,0)=0""",
            unique_ids,
        ).fetchall()
        # Manual search choices can reference an existing visual without a text
        # document. The namespace is an ID reference, never a caller-supplied path.
        visual_ids = [value.removeprefix('manual-visual::') for value in unique_ids if value.startswith('manual-visual::')]
        if visual_ids and 'visual_units' in _tables(con):
            marks = ','.join('?' for _ in visual_ids)
            rows += con.execute(f"""SELECT 'manual-visual::'||v.visual_unit_id AS document_id,
                    s.source_content_id,s.relative_path AS source_relative_path,s.media_type,
                    s.absolute_path,s.file_name,s.volume_id,s.online_status
                FROM visual_units v JOIN derived_assets a USING(derived_id)
                JOIN source_assets s ON s.source_content_id=v.source_content_id
                WHERE v.visual_unit_id IN ({marks}) AND COALESCE(s.is_deleted_or_missing,0)=0
                  AND s.media_type IN ('image','video')""", visual_ids).fetchall()
    return {
        str(row["document_id"]): {
            "source_content_id": str(row["source_content_id"]),
            "source_file": str(row["source_relative_path"]),
            "source_absolute_path": str(row["absolute_path"]),
            "source_file_name": str(row["file_name"]),
            "media_type": str(row["media_type"]),
            "volume_id": str(row["volume_id"]),
            "online_status": bool(row["online_status"]),
        }
        for row in rows
    }


def resolve_source_paths(database: Path, source_content_ids: list[str]) -> dict[str, str]:
    unique_ids = list(dict.fromkeys(str(value) for value in source_content_ids if value))
    if not unique_ids or len(unique_ids) > 5000 or any(len(value) > 200 for value in unique_ids):
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    with connect_readonly(database) as con:
        if "source_assets" not in _tables(con):
            return {}
        rows = con.execute(
            f"""SELECT source_content_id,absolute_path
                FROM source_assets
                WHERE source_content_id IN ({placeholders})
                  AND COALESCE(is_deleted_or_missing,0)=0""",
            unique_ids,
        ).fetchall()
    return {
        str(row["source_content_id"]): str(row["absolute_path"])
        for row in rows
        if row["absolute_path"]
    }
