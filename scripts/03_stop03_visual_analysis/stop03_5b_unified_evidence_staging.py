#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5B Unified Evidence Staging

Reads existing Stop02/Stop03 manifests and creates a local SQLite staging database.
No model rerun. No network. No model download. No original media writes.

Design intent:
- Build a traceable evidence staging layer before semantic propagation / text embedding.
- Preserve raw rows as JSON for auditability.
- Put Qwen-VL clean text and OCR text into evidence_text for later Qwen3-Embedding.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "stop03_5b_evidence_staging_v1.0"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def short_id(prefix: str, payload: str, n: int = 24) -> str:
    return f"{prefix}_{sha256_text(payload)[:n]}"


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_text_safe(p: Path, limit: Optional[int] = None) -> str:
    data = p.read_text(encoding="utf-8", errors="replace")
    return data if limit is None else data[:limit]


def detect_delimiter(sample: str) -> str:
    if "\t" in sample and sample.count("\t") >= sample.count(","):
        return "\t"
    if ";" in sample and sample.count(";") > sample.count(","):
        return ";"
    return ","


def read_rows(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.stat().st_size == 0:
        return []

    suffix = p.suffix.lower()
    txt_sample = read_text_safe(p, 4096).lstrip("\ufeff\n\r\t ")
    rows: List[Dict[str, Any]] = []

    if suffix == ".jsonl" or txt_sample.startswith("{"):
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        obj.setdefault("__line_no", line_no)
                        rows.append(obj)
                    else:
                        rows.append({"value": obj, "__line_no": line_no})
                except json.JSONDecodeError:
                    rows.append({"raw_line": line, "__line_no": line_no, "__parse_error": "json_decode"})
        return rows

    delimiter = detect_delimiter(txt_sample)
    with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        if not reader.fieldnames:
            return []
        for i, r in enumerate(reader, 2):
            rr = dict(r)
            rr.setdefault("__line_no", i)
            rows.append(rr)
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        keys: List[str] = []
        for r in rows:
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
        fieldnames = keys or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def first_nonempty(row: Dict[str, Any], keys: Iterable[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def int_or_none(v: Any) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def boolish(v: Any) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "exists", "ok"}:
        return 1
    if s in {"0", "false", "no", "n", "missing"}:
        return 0
    return None


SOURCE_PATH_KEYS = [
    "resolved_original_source_path", "original_source_path", "source_path", "media_path",
    "source_file_path", "absolute_source_path", "file_path", "path",
]
SOURCE_REL_KEYS = ["source_relative_path", "relative_path", "source_relpath", "relpath"]
CONTENT_ID_KEYS = ["original_source_content_id", "source_content_id", "content_id", "media_content_id"]
VISUAL_UNIT_ID_KEYS = ["visual_unit_id", "frame_id", "preview_id", "image_id", "unit_id"]
DERIVED_IMAGE_KEYS = ["runtime_input_image_path", "derived_image_path", "preview_path", "frame_path", "image_path", "jpg_path"]
DERIVED_SHA_KEYS = ["runtime_input_image_sha256", "derived_image_sha256", "image_sha256", "frame_sha256", "preview_sha256"]
TIME_KEYS = ["time_position_ms", "estimated_frame_time_ms", "frame_time_ms", "time_ms", "timestamp_ms"]
VISUAL_TYPE_KEYS = ["visual_unit_type", "unit_type", "media_unit_type", "type"]


def source_media_from_row(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    abs_path = first_nonempty(row, SOURCE_PATH_KEYS)
    rel_path = first_nonempty(row, SOURCE_REL_KEYS)
    content_id = first_nonempty(row, CONTENT_ID_KEYS)
    payload = content_id or abs_path or rel_path or jdump(row)[:1000]
    sid = content_id or short_id("src", payload)
    exists = boolish(row.get("resolved_original_source_exists"))
    return sid, {
        "source_media_id": sid,
        "original_source_path": abs_path,
        "source_relative_path": rel_path,
        "original_source_content_id": content_id,
        "resolved_original_source_exists": exists,
        "raw_json": jdump(row),
    }


def visual_unit_from_row(row: Dict[str, Any], fallback_type: str, source_name: str) -> Tuple[str, Dict[str, Any]]:
    vu = first_nonempty(row, VISUAL_UNIT_ID_KEYS)
    dpath = first_nonempty(row, DERIVED_IMAGE_KEYS)
    dsha = first_nonempty(row, DERIVED_SHA_KEYS)
    rel = first_nonempty(row, SOURCE_REL_KEYS)
    content_id = first_nonempty(row, CONTENT_ID_KEYS)
    tms = int_or_none(first_nonempty(row, TIME_KEYS))
    vtype = first_nonempty(row, VISUAL_TYPE_KEYS) or fallback_type
    if not vu:
        vu = short_id("vu", "|".join([dsha, dpath, rel, str(tms), content_id, source_name]))
    sid, _ = source_media_from_row(row)
    return vu, {
        "visual_unit_id": vu,
        "visual_unit_type": vtype,
        "source_media_id": sid,
        "time_position_ms": tms,
        "derived_image_path": dpath,
        "derived_image_sha256": dsha,
        "source_relative_path": rel,
        "original_source_content_id": content_id,
        "source_manifest": source_name,
        "raw_json": jdump(row),
    }


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS processing_stage (
            stage_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            run_root TEXT NOT NULL,
            source_safety TEXT NOT NULL,
            network TEXT NOT NULL,
            model_download TEXT NOT NULL,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS source_media (
            source_media_id TEXT PRIMARY KEY,
            original_source_path TEXT,
            source_relative_path TEXT,
            original_source_content_id TEXT,
            resolved_original_source_exists INTEGER,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS visual_unit (
            visual_unit_id TEXT PRIMARY KEY,
            visual_unit_type TEXT,
            source_media_id TEXT,
            time_position_ms INTEGER,
            derived_image_path TEXT,
            derived_image_sha256 TEXT,
            source_relative_path TEXT,
            original_source_content_id TEXT,
            source_manifest TEXT,
            raw_json TEXT,
            FOREIGN KEY(source_media_id) REFERENCES source_media(source_media_id)
        );

        CREATE TABLE IF NOT EXISTS model_evidence (
            evidence_id TEXT PRIMARY KEY,
            modality TEXT NOT NULL,
            evidence_type TEXT,
            visual_unit_id TEXT,
            source_media_id TEXT,
            stage_name TEXT,
            runtime_source TEXT,
            runtime_reason_codes TEXT,
            model_path TEXT,
            model_python TEXT,
            status TEXT,
            evidence_sha256 TEXT,
            created_at TEXT,
            raw_json TEXT,
            FOREIGN KEY(visual_unit_id) REFERENCES visual_unit(visual_unit_id),
            FOREIGN KEY(source_media_id) REFERENCES source_media(source_media_id)
        );

        CREATE TABLE IF NOT EXISTS evidence_text (
            evidence_text_id TEXT PRIMARY KEY,
            evidence_id TEXT NOT NULL,
            modality TEXT NOT NULL,
            text_kind TEXT NOT NULL,
            text TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            text_len INTEGER NOT NULL,
            quality_status TEXT,
            cleanup_status TEXT,
            is_clean INTEGER NOT NULL DEFAULT 0,
            raw_text_sha256 TEXT,
            FOREIGN KEY(evidence_id) REFERENCES model_evidence(evidence_id)
        );

        CREATE TABLE IF NOT EXISTS evidence_file_ref (
            file_ref_id TEXT PRIMARY KEY,
            evidence_id TEXT NOT NULL,
            ref_kind TEXT NOT NULL,
            path TEXT,
            sha256 TEXT,
            FOREIGN KEY(evidence_id) REFERENCES model_evidence(evidence_id)
        );

        CREATE TABLE IF NOT EXISTS quality_flag (
            quality_flag_id TEXT PRIMARY KEY,
            evidence_id TEXT,
            modality TEXT,
            quality_status TEXT,
            quality_reasons TEXT,
            source_stage TEXT,
            raw_json TEXT,
            FOREIGN KEY(evidence_id) REFERENCES model_evidence(evidence_id)
        );

        CREATE TABLE IF NOT EXISTS evidence_source_link (
            link_id TEXT PRIMARY KEY,
            evidence_id TEXT,
            visual_unit_id TEXT,
            source_media_id TEXT,
            modality TEXT,
            link_status TEXT,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_visual_unit_source_media ON visual_unit(source_media_id);
        CREATE INDEX IF NOT EXISTS idx_visual_unit_time ON visual_unit(original_source_content_id, time_position_ms);
        CREATE INDEX IF NOT EXISTS idx_model_evidence_vu ON model_evidence(visual_unit_id);
        CREATE INDEX IF NOT EXISTS idx_model_evidence_modality ON model_evidence(modality);
        CREATE INDEX IF NOT EXISTS idx_evidence_text_evidence ON evidence_text(evidence_id);
        """
    )


def upsert_source(conn: sqlite3.Connection, item: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO source_media(source_media_id, original_source_path, source_relative_path, original_source_content_id,
                                 resolved_original_source_exists, raw_json)
        VALUES(:source_media_id, :original_source_path, :source_relative_path, :original_source_content_id,
               :resolved_original_source_exists, :raw_json)
        ON CONFLICT(source_media_id) DO UPDATE SET
            original_source_path=COALESCE(NULLIF(excluded.original_source_path,''), source_media.original_source_path),
            source_relative_path=COALESCE(NULLIF(excluded.source_relative_path,''), source_media.source_relative_path),
            original_source_content_id=COALESCE(NULLIF(excluded.original_source_content_id,''), source_media.original_source_content_id),
            resolved_original_source_exists=COALESCE(excluded.resolved_original_source_exists, source_media.resolved_original_source_exists)
        """,
        item,
    )


def upsert_visual_unit(conn: sqlite3.Connection, item: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO visual_unit(visual_unit_id, visual_unit_type, source_media_id, time_position_ms, derived_image_path,
                                derived_image_sha256, source_relative_path, original_source_content_id, source_manifest, raw_json)
        VALUES(:visual_unit_id, :visual_unit_type, :source_media_id, :time_position_ms, :derived_image_path,
               :derived_image_sha256, :source_relative_path, :original_source_content_id, :source_manifest, :raw_json)
        ON CONFLICT(visual_unit_id) DO UPDATE SET
            visual_unit_type=COALESCE(NULLIF(excluded.visual_unit_type,''), visual_unit.visual_unit_type),
            source_media_id=COALESCE(NULLIF(excluded.source_media_id,''), visual_unit.source_media_id),
            time_position_ms=COALESCE(excluded.time_position_ms, visual_unit.time_position_ms),
            derived_image_path=COALESCE(NULLIF(excluded.derived_image_path,''), visual_unit.derived_image_path),
            derived_image_sha256=COALESCE(NULLIF(excluded.derived_image_sha256,''), visual_unit.derived_image_sha256),
            source_relative_path=COALESCE(NULLIF(excluded.source_relative_path,''), visual_unit.source_relative_path),
            original_source_content_id=COALESCE(NULLIF(excluded.original_source_content_id,''), visual_unit.original_source_content_id)
        """,
        item,
    )


def insert_model_evidence(conn: sqlite3.Connection, item: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO model_evidence(evidence_id, modality, evidence_type, visual_unit_id, source_media_id,
            stage_name, runtime_source, runtime_reason_codes, model_path, model_python, status, evidence_sha256,
            created_at, raw_json)
        VALUES(:evidence_id, :modality, :evidence_type, :visual_unit_id, :source_media_id,
            :stage_name, :runtime_source, :runtime_reason_codes, :model_path, :model_python, :status, :evidence_sha256,
            :created_at, :raw_json)
        """,
        item,
    )


def insert_text(conn: sqlite3.Connection, item: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO evidence_text(evidence_text_id, evidence_id, modality, text_kind, text, text_sha256,
            text_len, quality_status, cleanup_status, is_clean, raw_text_sha256)
        VALUES(:evidence_text_id, :evidence_id, :modality, :text_kind, :text, :text_sha256,
            :text_len, :quality_status, :cleanup_status, :is_clean, :raw_text_sha256)
        """,
        item,
    )


def insert_file_ref(conn: sqlite3.Connection, evidence_id: str, ref_kind: str, path: str, sha: str = "") -> None:
    if not path and not sha:
        return
    fid = short_id("fref", "|".join([evidence_id, ref_kind, path, sha]))
    conn.execute(
        "INSERT OR REPLACE INTO evidence_file_ref(file_ref_id, evidence_id, ref_kind, path, sha256) VALUES(?,?,?,?,?)",
        (fid, evidence_id, ref_kind, path, sha),
    )


def insert_link(conn: sqlite3.Connection, evidence_id: str, visual_unit_id: str, source_media_id: str, modality: str, status: str, raw: Dict[str, Any]) -> None:
    lid = short_id("link", "|".join([evidence_id, visual_unit_id, source_media_id, modality]))
    conn.execute(
        "INSERT OR REPLACE INTO evidence_source_link(link_id, evidence_id, visual_unit_id, source_media_id, modality, link_status, raw_json) VALUES(?,?,?,?,?,?,?)",
        (lid, evidence_id, visual_unit_id, source_media_id, modality, status, jdump(raw)),
    )


def load_visual_units(conn: sqlite3.Connection, rows: List[Dict[str, Any]], fallback_type: str, source_name: str) -> int:
    count = 0
    for r in rows:
        sid, sitem = source_media_from_row(r)
        upsert_source(conn, sitem)
        vu, vitem = visual_unit_from_row(r, fallback_type, source_name)
        upsert_visual_unit(conn, vitem)
        count += 1
    return count


def evidence_base_from_row(row: Dict[str, Any], modality: str, evidence_type: str, stage_name: str) -> Dict[str, Any]:
    vu, _ = visual_unit_from_row(row, first_nonempty(row, VISUAL_TYPE_KEYS) or "unknown", stage_name)
    sid, _ = source_media_from_row(row)
    eid = first_nonempty(row, ["evidence_id", "model_evidence_id", "detection_id", "embedding_id"])
    if not eid:
        eid = short_id(f"ev_{modality}", jdump(row))
    return {
        "evidence_id": eid,
        "modality": modality,
        "evidence_type": evidence_type,
        "visual_unit_id": vu,
        "source_media_id": sid,
        "stage_name": stage_name,
        "runtime_source": first_nonempty(row, ["runtime_source", "source_stage", "candidate_source"]),
        "runtime_reason_codes": first_nonempty(row, ["runtime_reason_codes", "reason_codes", "selection_reasons"]),
        "model_path": first_nonempty(row, ["qwen_model_path", "ocr_model_path", "model_path", "openclip_model_path", "yoloe_model_path"]),
        "model_python": first_nonempty(row, ["qwen_python", "ocr_python", "python", "python_path"]),
        "status": first_nonempty(row, ["status", "result_status", "quality_status"]) or "unknown",
        "evidence_sha256": first_nonempty(row, ["qwen_output_text_sha256", "ocr_text_sha256", "evidence_sha256", "runtime_input_image_sha256"]),
        "created_at": first_nonempty(row, ["created_at", "generated_at"]) or datetime.now().isoformat(timespec="seconds"),
        "raw_json": jdump(row),
    }


def load_qwenvl_clean(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    ev_count = 0
    text_count = 0
    for r in rows:
        sid, sitem = source_media_from_row(r)
        upsert_source(conn, sitem)
        vu, vitem = visual_unit_from_row(r, first_nonempty(r, VISUAL_TYPE_KEYS) or "unknown", "qwenvl_clean_text_manifest")
        upsert_visual_unit(conn, vitem)
        ev = evidence_base_from_row(r, "qwenvl", "visual_semantic_description", "stop03_3c_qwenvl_full_cleaned_by_stop03_5a2")
        insert_model_evidence(conn, ev)
        txt = first_nonempty(r, ["qwen_clean_text", "clean_text", "qwen_text"])
        raw_sha = first_nonempty(r, ["qwen_raw_text_sha256", "qwen_text_sha256", "qwen_output_text_sha256"])
        if txt:
            tsha = first_nonempty(r, ["qwen_clean_text_sha256", "clean_text_sha256"]) or sha256_text(txt)
            insert_text(conn, {
                "evidence_text_id": short_id("txt", ev["evidence_id"] + "|qwenvl_clean"),
                "evidence_id": ev["evidence_id"],
                "modality": "qwenvl",
                "text_kind": "qwen_clean_text",
                "text": txt,
                "text_sha256": tsha,
                "text_len": len(txt),
                "quality_status": first_nonempty(r, ["quality_status"]),
                "cleanup_status": first_nonempty(r, ["qwen_text_cleanup_status", "cleanup_status"]),
                "is_clean": 1,
                "raw_text_sha256": raw_sha,
            })
            text_count += 1
        insert_file_ref(conn, ev["evidence_id"], "runtime_input_image", first_nonempty(r, ["runtime_input_image_path"]), first_nonempty(r, ["runtime_input_image_sha256"]))
        insert_file_ref(conn, ev["evidence_id"], "qwen_output_text", first_nonempty(r, ["qwen_output_text_path"]), first_nonempty(r, ["qwen_output_text_sha256"]))
        insert_link(conn, ev["evidence_id"], ev["visual_unit_id"], ev["source_media_id"], "qwenvl", "linked", r)
        ev_count += 1
    return ev_count, text_count


def load_ocr(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    ev_count = 0
    text_count = 0
    for r in rows:
        sid, sitem = source_media_from_row(r)
        upsert_source(conn, sitem)
        vu, vitem = visual_unit_from_row(r, first_nonempty(r, VISUAL_TYPE_KEYS) or "unknown", "ocr_db_ready_manifest")
        upsert_visual_unit(conn, vitem)
        ev = evidence_base_from_row(r, "ocr", "ocr_text", "stop03_4b_ocr_full")
        insert_model_evidence(conn, ev)
        txt = first_nonempty(r, ["ocr_text", "text", "recognized_text", "clean_text"])
        if txt:
            tsha = first_nonempty(r, ["ocr_text_sha256", "text_sha256"]) or sha256_text(txt)
            insert_text(conn, {
                "evidence_text_id": short_id("txt", ev["evidence_id"] + "|ocr"),
                "evidence_id": ev["evidence_id"],
                "modality": "ocr",
                "text_kind": "ocr_text",
                "text": txt,
                "text_sha256": tsha,
                "text_len": len(txt),
                "quality_status": first_nonempty(r, ["quality_status"]),
                "cleanup_status": "not_required",
                "is_clean": 1,
                "raw_text_sha256": tsha,
            })
            text_count += 1
        insert_file_ref(conn, ev["evidence_id"], "runtime_input_image", first_nonempty(r, ["runtime_input_image_path"]), first_nonempty(r, ["runtime_input_image_sha256"]))
        insert_file_ref(conn, ev["evidence_id"], "ocr_output_text", first_nonempty(r, ["ocr_output_text_path", "output_text_path"]), first_nonempty(r, ["ocr_output_text_sha256"]))
        insert_link(conn, ev["evidence_id"], ev["visual_unit_id"], ev["source_media_id"], "ocr", "linked", r)
        ev_count += 1
    return ev_count, text_count


def load_lowcost_join(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    # Store one audit/link evidence row per join row. Detailed YOLOE detection expansion can be done later if needed.
    count = 0
    for r in rows:
        sid, sitem = source_media_from_row(r)
        upsert_source(conn, sitem)
        vu, vitem = visual_unit_from_row(r, first_nonempty(r, VISUAL_TYPE_KEYS) or "unknown", "stop03_1_visual_yoloe_join")
        upsert_visual_unit(conn, vitem)
        eid = short_id("ev_lowcost", jdump(r))
        ev = {
            "evidence_id": eid,
            "modality": "low_cost_visual",
            "evidence_type": "yoloe_openclip_join_ref",
            "visual_unit_id": vu,
            "source_media_id": sid,
            "stage_name": "stop03_1_visual_embedding_then_yoloe4_join",
            "runtime_source": "stop03_1_join_manifest",
            "runtime_reason_codes": "",
            "model_path": "",
            "model_python": "",
            "status": first_nonempty(r, ["status", "join_status"]) or "recorded",
            "evidence_sha256": first_nonempty(r, DERIVED_SHA_KEYS),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "raw_json": jdump(r),
        }
        insert_model_evidence(conn, ev)
        insert_link(conn, eid, vu, sid, "low_cost_visual", "linked", r)
        count += 1
    return count


def load_quality_flags(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    count = 0
    for r in rows:
        eid = first_nonempty(r, ["evidence_id"])
        modality = first_nonempty(r, ["modality"])
        qstatus = first_nonempty(r, ["quality_status"])
        qreasons = first_nonempty(r, ["quality_reasons"])
        fid = short_id("qflag", jdump(r))
        conn.execute(
            "INSERT OR REPLACE INTO quality_flag(quality_flag_id, evidence_id, modality, quality_status, quality_reasons, source_stage, raw_json) VALUES(?,?,?,?,?,?,?)",
            (fid, eid, modality, qstatus, qreasons, "stop03_5a_quality_audit", jdump(r)),
        )
        count += 1
    return count


def dump_table(conn: sqlite3.Connection, table: str, path: Path) -> int:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    write_csv(path, rows, cols)
    return len(rows)


def scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stop03-5B unified evidence staging database builder")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--video-frame-manifest", required=False, default="")
    ap.add_argument("--image-preview-manifest", required=False, default="")
    ap.add_argument("--stop03-1-join", required=False, default="")
    ap.add_argument("--candidate-decision", required=False, default="")
    ap.add_argument("--qwenvl-clean", required=True)
    ap.add_argument("--ocr-db-ready", required=True)
    ap.add_argument("--quality-audit-manifest", required=False, default="")
    ap.add_argument("--expect-qwenvl", type=int, default=268)
    ap.add_argument("--expect-ocr", type=int, default=226)
    args = ap.parse_args(argv)

    start = time.time()
    out = Path(args.out)
    ensure_dir(out / "database")
    ensure_dir(out / "manifests")
    ensure_dir(out / "reports")

    input_paths = [args.qwenvl_clean, args.ocr_db_ready]
    for opt in [args.video_frame_manifest, args.image_preview_manifest, args.stop03_1_join, args.candidate_decision, args.quality_audit_manifest]:
        if opt:
            input_paths.append(opt)
    missing = [p for p in input_paths if not Path(p).exists()]
    if missing:
        print(json.dumps({"validation_status": "FAIL", "reason": "missing_input", "missing": missing}, ensure_ascii=False, indent=2))
        return 2

    rows_video = read_rows(args.video_frame_manifest) if args.video_frame_manifest else []
    rows_image = read_rows(args.image_preview_manifest) if args.image_preview_manifest else []
    rows_join = read_rows(args.stop03_1_join) if args.stop03_1_join else []
    rows_decision = read_rows(args.candidate_decision) if args.candidate_decision else []
    rows_qwen = read_rows(args.qwenvl_clean)
    rows_ocr = read_rows(args.ocr_db_ready)
    rows_quality = read_rows(args.quality_audit_manifest) if args.quality_audit_manifest else []

    db_path = out / "database" / "evidence_staging.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    stage_id = short_id("stage", args.run_root + "|" + str(out))
    conn.execute(
        "INSERT OR REPLACE INTO processing_stage(stage_id, schema_version, generated_at, run_root, source_safety, network, model_download, raw_json) VALUES(?,?,?,?,?,?,?,?)",
        (
            stage_id,
            SCHEMA_VERSION,
            now_id(),
            args.run_root,
            "read_existing_manifests_only_no_original_write",
            "not_required_not_used",
            "not_required_not_used",
            jdump(vars(args)),
        ),
    )

    c_video = load_visual_units(conn, rows_video, "video_frame", "stop02_video_frame_manifest")
    c_image = load_visual_units(conn, rows_image, "image_preview", "stop02_image_preview_manifest")
    c_join = load_lowcost_join(conn, rows_join) if rows_join else 0
    # Candidate decisions are stored as quality/source links only; they can also add missing visual units.
    c_decision_vu = load_visual_units(conn, rows_decision, "decision_visual_unit", "stop03_2_candidate_decision_manifest") if rows_decision else 0
    c_qwen_ev, c_qwen_txt = load_qwenvl_clean(conn, rows_qwen)
    c_ocr_ev, c_ocr_txt = load_ocr(conn, rows_ocr)
    c_quality = load_quality_flags(conn, rows_quality) if rows_quality else 0

    conn.commit()

    export_counts = {
        "unified_visual_units": dump_table(conn, "visual_unit", out / "manifests" / "unified_visual_units.csv"),
        "unified_model_evidence": dump_table(conn, "model_evidence", out / "manifests" / "unified_model_evidence.csv"),
        "unified_evidence_text": dump_table(conn, "evidence_text", out / "manifests" / "unified_evidence_text.csv"),
        "evidence_source_link_manifest": dump_table(conn, "evidence_source_link", out / "manifests" / "evidence_source_link_manifest.csv"),
    }

    db_counts = {
        "source_media": scalar(conn, "SELECT COUNT(*) FROM source_media"),
        "visual_unit": scalar(conn, "SELECT COUNT(*) FROM visual_unit"),
        "model_evidence": scalar(conn, "SELECT COUNT(*) FROM model_evidence"),
        "evidence_text": scalar(conn, "SELECT COUNT(*) FROM evidence_text"),
        "qwen_text": scalar(conn, "SELECT COUNT(*) FROM evidence_text WHERE modality='qwenvl' AND text_kind='qwen_clean_text'"),
        "ocr_text": scalar(conn, "SELECT COUNT(*) FROM evidence_text WHERE modality='ocr' AND text_kind='ocr_text'"),
        "low_cost_visual_evidence": scalar(conn, "SELECT COUNT(*) FROM model_evidence WHERE modality='low_cost_visual'"),
        "quality_flag": scalar(conn, "SELECT COUNT(*) FROM quality_flag"),
        "links": scalar(conn, "SELECT COUNT(*) FROM evidence_source_link"),
    }

    conn.close()

    problems: List[str] = []
    if args.expect_qwenvl >= 0 and db_counts["qwen_text"] != args.expect_qwenvl:
        problems.append(f"qwen_text_count_mismatch expected={args.expect_qwenvl} actual={db_counts['qwen_text']}")
    if args.expect_ocr >= 0 and db_counts["ocr_text"] != args.expect_ocr:
        problems.append(f"ocr_text_count_mismatch expected={args.expect_ocr} actual={db_counts['ocr_text']}")
    if db_counts["evidence_text"] != db_counts["qwen_text"] + db_counts["ocr_text"]:
        problems.append("evidence_text_count_not_equal_qwen_plus_ocr")
    if db_counts["qwen_text"] == 0 or db_counts["ocr_text"] == 0:
        problems.append("missing_core_text_evidence")

    validation_status = "PASS" if not problems else "FAIL"
    summary = {
        "validation_status": validation_status,
        "generated_at": now_id(),
        "schema_version": SCHEMA_VERSION,
        "mode": "read_existing_manifests_only_no_model_rerun",
        "source_safety": "read_only_no_move_no_delete_no_rename_no_original_media_access_required",
        "network": "not_required_not_used",
        "model_download": "not_required_not_used",
        "elapsed_seconds": round(time.time() - start, 3),
        "run_root": args.run_root,
        "database": str(db_path),
        "input_row_counts": {
            "video_frame_manifest": len(rows_video),
            "image_preview_manifest": len(rows_image),
            "stop03_1_join": len(rows_join),
            "candidate_decision": len(rows_decision),
            "qwenvl_clean": len(rows_qwen),
            "ocr_db_ready": len(rows_ocr),
            "quality_audit_manifest": len(rows_quality),
        },
        "loaded_counts": {
            "video_visual_units_loaded": c_video,
            "image_visual_units_loaded": c_image,
            "lowcost_join_evidence_loaded": c_join,
            "decision_visual_units_loaded": c_decision_vu,
            "qwenvl_evidence_loaded": c_qwen_ev,
            "qwenvl_text_loaded": c_qwen_txt,
            "ocr_evidence_loaded": c_ocr_ev,
            "ocr_text_loaded": c_ocr_txt,
            "quality_flags_loaded": c_quality,
        },
        "db_counts": db_counts,
        "export_counts": export_counts,
        "problems": problems,
        "outputs": {
            "sqlite": str(db_path),
            "unified_visual_units_csv": str(out / "manifests" / "unified_visual_units.csv"),
            "unified_model_evidence_csv": str(out / "manifests" / "unified_model_evidence.csv"),
            "unified_evidence_text_csv": str(out / "manifests" / "unified_evidence_text.csv"),
            "evidence_source_link_manifest_csv": str(out / "manifests" / "evidence_source_link_manifest.csv"),
            "summary_json": str(out / "reports" / "stop03_5b_unified_evidence_staging_summary.json"),
            "summary_md": str(out / "reports" / "stop03_5b_unified_evidence_staging_summary.md"),
        },
    }

    (out / "reports" / "stop03_5b_unified_evidence_staging_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = [
        "# Stop03-5B Unified Evidence Staging",
        "",
        f"- validation_status: `{validation_status}`",
        f"- schema_version: `{SCHEMA_VERSION}`",
        "- mode: `read_existing_manifests_only_no_model_rerun`",
        "- source_safety: `read_only_no_move_no_delete_no_rename_no_original_media_access_required`",
        "- network: `not_required_not_used`",
        "- model_download: `not_required_not_used`",
        "",
        "## Input row counts",
    ]
    for k, v in summary["input_row_counts"].items():
        md.append(f"- {k}: `{v}`")
    md += ["", "## Loaded counts"]
    for k, v in summary["loaded_counts"].items():
        md.append(f"- {k}: `{v}`")
    md += ["", "## Database counts"]
    for k, v in db_counts.items():
        md.append(f"- {k}: `{v}`")
    md += ["", "## Decision"]
    if validation_status == "PASS":
        md.append("Unified evidence staging passed. Next stage can be Stop03-5C semantic propagation, after optional spot-check of staging rows.")
    else:
        md.append("Unified evidence staging failed. Do not continue to semantic propagation until problems are fixed.")
        for p in problems:
            md.append(f"- {p}")
    md += ["", "## Outputs"]
    for k, v in summary["outputs"].items():
        md.append(f"- {k}: `{v}`")
    (out / "reports" / "stop03_5b_unified_evidence_staging_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("== Stop03-5B unified evidence staging finished ==")
    print(json.dumps({
        "validation_status": validation_status,
        "elapsed_seconds": summary["elapsed_seconds"],
        "db_counts": db_counts,
        "problems": problems,
        "summary_md": summary["outputs"]["summary_md"],
        "sqlite": str(db_path),
    }, ensure_ascii=False, indent=2))
    return 0 if validation_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
