#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5C Semantic Propagation v2 - YOLO-gated safe propagation

Purpose:
  Propagate Qwen-VL clean text to nearby video frames only when low-cost YOLO evidence
  supports a shared visible object between source and target frame.

Safety:
  - Reads existing staging sqlite only.
  - Does not read or modify original media.
  - Does not run models.
  - Does not use network.

Key idea:
  Qwen-VL text may describe 100 semantic details. YOLO only confirms a small object subset.
  This script propagates only the YOLO-supported subset / matching sentences, not the whole Qwen text.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = "stop03_5c_semantic_propagation_v2_yolo_gate_v1.0"

COMMON_LABEL_ALIASES = {
    "person": ["人", "人物", "行人", "男子", "女子", "老人", "小孩", "协管员", "工作人员"],
    "car": ["车", "汽车", "轿车", "车辆", "小车", "SUV", "越野车", "本田", "雪佛兰", "出租车"],
    "truck": ["卡车", "货车", "皮卡", "工程车", "车辆"],
    "bus": ["公交", "公交车", "巴士", "客车", "站台"],
    "train": ["列车", "火车", "轻轨", "地铁", "轨道", "高架轨道"],
    "bicycle": ["自行车", "单车", "骑行"],
    "motorcycle": ["摩托", "摩托车", "电动车"],
    "traffic light": ["红绿灯", "交通灯", "信号灯"],
    "stop sign": ["停止标志", "交通标志", "标志牌"],
    "bench": ["长椅", "座椅", "椅子"],
    "bird": ["鸟"],
    "cat": ["猫"],
    "dog": ["狗"],
    "horse": ["马"],
    "cow": ["牛"],
    "sheep": ["羊"],
    "chair": ["椅子", "座椅"],
    "couch": ["沙发"],
    "potted plant": ["盆栽", "植物", "绿植"],
    "dining table": ["桌子", "餐桌"],
    "tv": ["电视", "屏幕", "显示器"],
    "laptop": ["电脑", "笔记本"],
    "cell phone": ["手机"],
    "book": ["书", "书本"],
    "clock": ["钟", "时钟"],
    "bottle": ["瓶", "瓶子"],
    "cup": ["杯", "杯子"],
    "backpack": ["背包"],
    "umbrella": ["伞", "雨伞"],
    "handbag": ["包", "手提包"],
}

GENERIC_NOISE_LABELS = {
    "object", "objects", "thing", "unknown", "none", "null", "nan", "background",
    "image", "frame", "video", "visual", "yolo", "label", "labels", "detected",
}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("select name from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if not table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f"pragma table_info({quote_ident(table)})").fetchall()]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def rows_as_dicts(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    if not table_exists(conn, table):
        return []
    cur = conn.execute(f"select * from {quote_ident(table)}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def pick_col(cols: Sequence[str], candidates: Sequence[str], contains: Optional[Sequence[str]] = None) -> Optional[str]:
    lower_map = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    if contains:
        for c in cols:
            lc = c.lower()
            if all(x.lower() in lc for x in contains):
                return c
    return None


def get_val(row: Dict[str, Any], *names: str, default: Any = "") -> Any:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    # case-insensitive fallback
    lmap = {k.lower(): k for k in row.keys()}
    for n in names:
        k = lmap.get(n.lower())
        if k and row.get(k) not in (None, ""):
            return row[k]
    return default


def to_int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v)))
    except Exception:
        return None


def compact(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def try_json_loads(s: Any) -> Any:
    if not isinstance(s, str):
        return None
    st = s.strip()
    if not st or st[0] not in "[{\"":
        return None
    try:
        return json.loads(st)
    except Exception:
        return None


def walk_json_labels(obj: Any, out: Set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if any(x in lk for x in ["label", "class", "name", "category"]):
                if isinstance(v, str):
                    add_label(v, out)
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        if isinstance(item, str):
                            add_label(item, out)
                        else:
                            walk_json_labels(item, out)
                else:
                    walk_json_labels(v, out)
            else:
                walk_json_labels(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_json_labels(v, out)


def add_label(label: str, out: Set[str]) -> None:
    lab = compact(label).strip("'\"[]{}()")
    if not lab:
        return
    lab = lab.replace("_", " ").lower()
    if len(lab) > 40:
        return
    if lab in GENERIC_NOISE_LABELS:
        return
    # avoid hashes / paths / numeric-only fragments
    if "/" in lab or "\\" in lab or re.fullmatch(r"[a-f0-9]{10,}", lab) or re.fullmatch(r"\d+(\.\d+)?", lab):
        return
    out.add(lab)


def extract_labels_from_blob(blob: str) -> Set[str]:
    labels: Set[str] = set()
    if not blob:
        return labels

    # JSON first
    obj = try_json_loads(blob)
    if obj is not None:
        walk_json_labels(obj, labels)

    # Pattern examples: label=car, class_name: person, labels: ['car','person']
    patterns = [
        r"(?:label|class_name|class|category|name)\s*[:=]\s*['\"]?([A-Za-z][A-Za-z _-]{1,40})['\"]?",
        r"['\"](?:label|class_name|class|category|name)['\"]\s*:\s*['\"]([^'\"]{1,40})['\"]",
    ]
    for pat in patterns:
        for m in re.finditer(pat, blob, flags=re.I):
            add_label(m.group(1), labels)

    # If a known COCO-style label appears as a standalone word, collect it.
    low = blob.lower()
    for lab in COMMON_LABEL_ALIASES.keys():
        if re.search(r"\b" + re.escape(lab) + r"\b", low):
            add_label(lab, labels)

    return labels


def build_yolo_label_map(model_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Set[str]], Dict[str, int]]:
    """Return visual_unit_id -> label set. Intentionally broad/introspective."""
    label_map: Dict[str, Set[str]] = {}
    debug = {"candidate_rows": 0, "rows_with_labels": 0}
    for r in model_rows:
        modality = str(get_val(r, "modality", "evidence_type", "model", "stage", default="")).lower()
        joined_all = " ".join(str(v) for v in r.values() if v is not None).lower()
        if not ("yolo" in modality or "yolo" in joined_all or "detect" in joined_all or "low_cost" in modality or "lowcost" in modality):
            continue
        vu = str(get_val(r, "visual_unit_id", "target_visual_unit_id", default="")).strip()
        if not vu:
            continue
        debug["candidate_rows"] += 1
        labels: Set[str] = set()
        for k, v in r.items():
            lk = k.lower()
            if any(x in lk for x in ["label", "object", "detect", "class", "json", "result", "summary", "raw"]):
                if isinstance(v, str):
                    labels.update(extract_labels_from_blob(v))
                else:
                    labels.update(extract_labels_from_blob(str(v)))
        # Fallback: scan all row text for common labels only.
        if not labels:
            labels.update(extract_labels_from_blob(" ".join(str(v) for v in r.values() if isinstance(v, str))))
        if labels:
            debug["rows_with_labels"] += 1
            label_map.setdefault(vu, set()).update(labels)
    return label_map, debug


def split_sentences_zh(text: str) -> List[str]:
    if not text:
        return []
    # preserve bullet-ish chunks too
    parts = re.split(r"(?<=[。！？；;])\s*|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def label_terms(label: str) -> List[str]:
    terms = [label]
    terms.extend(COMMON_LABEL_ALIASES.get(label.lower(), []))
    # add simple Chinese broad synonyms for compound English labels
    if "car" in label:
        terms.extend(COMMON_LABEL_ALIASES["car"])
    if "person" in label:
        terms.extend(COMMON_LABEL_ALIASES["person"])
    return list(dict.fromkeys([t for t in terms if t]))


def extract_yolo_supported_text(qwen_text: str, overlap_labels: Set[str], max_chars: int = 600) -> str:
    """Keep only sentences/clauses matching shared YOLO labels. Do not propagate full Qwen text."""
    if not qwen_text or not overlap_labels:
        return ""
    terms: List[str] = []
    for lab in sorted(overlap_labels):
        terms.extend(label_terms(lab))
    terms = list(dict.fromkeys(terms))

    matched: List[str] = []
    for sent in split_sentences_zh(qwen_text):
        if any(t and t.lower() in sent.lower() for t in terms):
            matched.append(sent)

    label_part = "、".join(sorted(overlap_labels))
    if matched:
        body = " ".join(matched)
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        return f"相邻帧语义传播（YOLO共同对象：{label_part}）：{body}"
    return f"相邻帧语义传播（YOLO共同对象：{label_part}）：源帧与目标帧共享这些可见对象；未传播源帧中未被YOLO共同确认的其他语义。"


def is_bad_source_text(text: str, min_len: int) -> Tuple[bool, str]:
    t = compact(text)
    if len(t) < min_len:
        return True, "source_text_too_short"
    bad_phrases = ["纯黑", "一片黑", "无任何可见内容", "检索价值：无", "画面为黑"]
    if any(p in t for p in bad_phrases):
        return True, "black_or_empty_frame_text"
    return False, ""


def confidence_for_step(step: int, overlap_count: int) -> float:
    base = {1: 0.85, 2: 0.75, 3: 0.65}.get(step, max(0.35, 0.85 - 0.10 * (step - 1)))
    if overlap_count <= 1:
        base -= 0.05
    elif overlap_count >= 3:
        base += 0.03
    return round(max(0.1, min(0.95, base)), 3)


def create_output_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=WAL")
    conn.execute("""
    create table semantic_propagation (
      propagation_id text primary key,
      schema_version text,
      source_evidence_id text,
      source_visual_unit_id text,
      target_visual_unit_id text,
      source_original_content_id text,
      target_original_content_id text,
      source_time_position_ms integer,
      target_time_position_ms integer,
      time_delta_ms integer,
      propagation_direction text,
      propagation_step integer,
      propagation_confidence real,
      propagation_reason text,
      yolo_gate_status text,
      yolo_overlap_labels text,
      source_yolo_labels text,
      target_yolo_labels text,
      created_at text
    )
    """)
    conn.execute("""
    create table propagated_evidence_text (
      propagated_text_id text primary key,
      propagation_id text,
      source_evidence_id text,
      source_visual_unit_id text,
      target_visual_unit_id text,
      modality text,
      text_kind text,
      text text,
      text_sha256 text,
      propagation_confidence real,
      yolo_overlap_labels text,
      source_text_sha256 text,
      source_text_len integer,
      propagated_text_len integer,
      created_at text
    )
    """)
    return conn


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main(argv: Optional[Sequence[str]] = None) -> int:
    raise SystemExit(
        "RETIRED_STOP03_5C_INTERFACE: use "
        "stop03_5c_qwenvl_yolo_propagation_v1.py with the central database"
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--staging-db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--radius", type=int, default=3)
    ap.add_argument("--max-time-delta-ms", type=int, default=30000)
    ap.add_argument("--min-source-text-len", type=int, default=80)
    ap.add_argument("--expect-direct-qwenvl", type=int, default=0)
    ap.add_argument("--disable-yolo-gate", action="store_true", help="Not recommended: direct neighbor propagation without YOLO overlap gate.")
    ap.add_argument("--allow-missing-yolo-labels", action="store_true", help="If set, allows propagation when labels are missing. Default blocks missing labels.")
    args = ap.parse_args(argv)

    out = Path(args.out)
    ensure_dir(out / "database")
    ensure_dir(out / "manifests")
    ensure_dir(out / "reports")

    started = dt.datetime.now()
    problems: List[str] = []

    src = sqlite3.connect(args.staging_db)
    src.row_factory = sqlite3.Row

    visual_rows = rows_as_dicts(src, "visual_unit")
    model_rows = rows_as_dicts(src, "model_evidence")
    text_rows = rows_as_dicts(src, "evidence_text")

    if not visual_rows:
        problems.append("visual_unit table empty or missing")
    if not text_rows:
        problems.append("evidence_text table empty or missing")

    vu_by_id: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in visual_rows:
        vu = str(get_val(r, "visual_unit_id", "id", default="")).strip()
        if not vu:
            continue
        vtype = str(get_val(r, "visual_unit_type", "unit_type", "type", default="")).strip()
        ocid = str(get_val(r, "original_source_content_id", "source_content_id", "content_id", default="")).strip()
        tms = to_int_or_none(get_val(r, "time_position_ms", "estimated_frame_time_ms", "start_time_ms", default=""))
        rr = dict(r)
        rr["_visual_unit_id"] = vu
        rr["_visual_unit_type"] = vtype
        rr["_original_source_content_id"] = ocid
        rr["_time_position_ms"] = tms
        vu_by_id[vu] = rr
        if ocid and tms is not None and ("video" in vtype.lower() or vtype.lower() == "video_frame"):
            groups.setdefault(ocid, []).append(rr)
    for g in groups.values():
        g.sort(key=lambda x: (x.get("_time_position_ms") if x.get("_time_position_ms") is not None else -1, x.get("_visual_unit_id", "")))

    # evidence_id -> visual_unit_id via model_evidence
    ev_to_vu: Dict[str, str] = {}
    ev_modality: Dict[str, str] = {}
    for r in model_rows:
        ev = str(get_val(r, "evidence_id", "model_evidence_id", "id", default="")).strip()
        vu = str(get_val(r, "visual_unit_id", default="")).strip()
        mod = str(get_val(r, "modality", "evidence_type", default="")).strip()
        if ev and vu:
            ev_to_vu[ev] = vu
            ev_modality[ev] = mod

    qwen_sources: List[Dict[str, Any]] = []
    direct_qwen_vus: Set[str] = set()
    for r in text_rows:
        modality = str(get_val(r, "modality", default="")).lower()
        kind = str(get_val(r, "text_kind", default="")).lower()
        if modality != "qwenvl" and "qwen" not in modality:
            continue
        if kind and "qwen" not in kind:
            continue
        ev = str(get_val(r, "evidence_id", default="")).strip()
        vu = str(get_val(r, "visual_unit_id", default="")).strip() or ev_to_vu.get(ev, "")
        text = str(get_val(r, "text", "qwen_clean_text", "clean_text", default=""))
        if not vu:
            continue
        vu_row = vu_by_id.get(vu)
        if not vu_row:
            continue
        direct_qwen_vus.add(vu)
        bad, reason = is_bad_source_text(text, args.min_source_text_len)
        if bad:
            continue
        if "video" not in str(vu_row.get("_visual_unit_type", "")).lower():
            # propagate only video frames. still counted as direct source but skipped.
            continue
        if not vu_row.get("_original_source_content_id") or vu_row.get("_time_position_ms") is None:
            continue
        qwen_sources.append({
            "evidence_id": ev,
            "visual_unit_id": vu,
            "text": text,
            "text_sha256": sha256_text(text),
            "source_row": vu_row,
        })

    if args.expect_direct_qwenvl and len(direct_qwen_vus) != args.expect_direct_qwenvl:
        problems.append(f"direct_qwenvl_count_mismatch expected={args.expect_direct_qwenvl} actual={len(direct_qwen_vus)}")

    yolo_labels, yolo_debug = build_yolo_label_map(model_rows)

    prop_rows: List[Dict[str, Any]] = []
    text_out_rows: List[Dict[str, Any]] = []
    blocked_counts: Dict[str, int] = {}
    candidate_pairs = 0

    created_at = now_stamp()
    # group indices
    index_by_vu: Dict[str, int] = {}
    for ocid, g in groups.items():
        for i, r in enumerate(g):
            index_by_vu[r["_visual_unit_id"]] = i

    source_count_yolo_missing = 0
    target_count_yolo_missing = 0

    for src_item in qwen_sources:
        source_vu = src_item["visual_unit_id"]
        srow = src_item["source_row"]
        ocid = srow["_original_source_content_id"]
        group = groups.get(ocid, [])
        if source_vu not in index_by_vu:
            continue
        sidx = index_by_vu[source_vu]
        stime = srow["_time_position_ms"]
        source_labels = yolo_labels.get(source_vu, set())
        if not source_labels:
            source_count_yolo_missing += 1
        for offset in range(-args.radius, args.radius + 1):
            if offset == 0:
                continue
            tidx = sidx + offset
            if tidx < 0 or tidx >= len(group):
                continue
            target = group[tidx]
            target_vu = target["_visual_unit_id"]
            if target_vu in direct_qwen_vus:
                blocked_counts["target_has_direct_qwenvl"] = blocked_counts.get("target_has_direct_qwenvl", 0) + 1
                continue
            ttime = target.get("_time_position_ms")
            if stime is None or ttime is None:
                blocked_counts["missing_time"] = blocked_counts.get("missing_time", 0) + 1
                continue
            delta = int(ttime) - int(stime)
            if abs(delta) > args.max_time_delta_ms:
                blocked_counts["time_delta_exceeded"] = blocked_counts.get("time_delta_exceeded", 0) + 1
                continue
            candidate_pairs += 1
            target_labels = yolo_labels.get(target_vu, set())
            if not target_labels:
                target_count_yolo_missing += 1
            overlap = set(source_labels) & set(target_labels)
            gate_status = "disabled"
            reason = []
            if not args.disable_yolo_gate:
                if not source_labels or not target_labels:
                    gate_status = "missing_yolo_labels"
                    if not args.allow_missing_yolo_labels:
                        blocked_counts["missing_yolo_labels"] = blocked_counts.get("missing_yolo_labels", 0) + 1
                        continue
                elif not overlap:
                    gate_status = "no_yolo_overlap"
                    blocked_counts["no_yolo_overlap"] = blocked_counts.get("no_yolo_overlap", 0) + 1
                    continue
                else:
                    gate_status = "passed_yolo_overlap"
                    reason.append("yolo_overlap_gate")
            else:
                overlap = overlap or set()
                reason.append("direct_neighbor_no_yolo_gate")

            if not overlap and args.allow_missing_yolo_labels:
                # fallback text is deliberately conservative.
                propagated_text = "相邻帧语义传播：YOLO标签缺失，未传播具体画面细节；仅保留相邻帧关系供后续人工/算法复核。"
            else:
                propagated_text = extract_yolo_supported_text(src_item["text"], overlap)
            if not propagated_text.strip():
                blocked_counts["empty_propagated_text"] = blocked_counts.get("empty_propagated_text", 0) + 1
                continue

            step = abs(offset)
            direction = "previous" if offset < 0 else "next"
            conf = confidence_for_step(step, len(overlap))
            pid_seed = f"{src_item['evidence_id']}|{source_vu}|{target_vu}|{offset}|{sha256_text(propagated_text)[:12]}"
            pid = "prop_" + sha256_text(pid_seed)[:24]
            ptid = "ptext_" + sha256_text(pid + "|text")[:24]
            overlap_s = "|".join(sorted(overlap))
            source_label_s = "|".join(sorted(source_labels))
            target_label_s = "|".join(sorted(target_labels))
            prop_rows.append({
                "propagation_id": pid,
                "schema_version": SCHEMA_VERSION,
                "source_evidence_id": src_item["evidence_id"],
                "source_visual_unit_id": source_vu,
                "target_visual_unit_id": target_vu,
                "source_original_content_id": ocid,
                "target_original_content_id": target.get("_original_source_content_id", ""),
                "source_time_position_ms": stime,
                "target_time_position_ms": ttime,
                "time_delta_ms": delta,
                "propagation_direction": direction,
                "propagation_step": step,
                "propagation_confidence": conf,
                "propagation_reason": "|".join(reason),
                "yolo_gate_status": gate_status,
                "yolo_overlap_labels": overlap_s,
                "source_yolo_labels": source_label_s,
                "target_yolo_labels": target_label_s,
                "created_at": created_at,
            })
            text_out_rows.append({
                "propagated_text_id": ptid,
                "propagation_id": pid,
                "source_evidence_id": src_item["evidence_id"],
                "source_visual_unit_id": source_vu,
                "target_visual_unit_id": target_vu,
                "modality": "qwenvl_propagated",
                "text_kind": "qwen_yolo_supported_propagated_text",
                "text": propagated_text,
                "text_sha256": sha256_text(propagated_text),
                "propagation_confidence": conf,
                "yolo_overlap_labels": overlap_s,
                "source_text_sha256": src_item["text_sha256"],
                "source_text_len": len(src_item["text"]),
                "propagated_text_len": len(propagated_text),
                "created_at": created_at,
            })

    # Write output DB with explicit column insert. Fixes v1 15-columns/14-values issue.
    out_db = out / "database" / "semantic_propagation.sqlite"
    dst = create_output_db(out_db)
    prop_cols = [
        "propagation_id", "schema_version", "source_evidence_id", "source_visual_unit_id", "target_visual_unit_id",
        "source_original_content_id", "target_original_content_id", "source_time_position_ms", "target_time_position_ms",
        "time_delta_ms", "propagation_direction", "propagation_step", "propagation_confidence", "propagation_reason",
        "yolo_gate_status", "yolo_overlap_labels", "source_yolo_labels", "target_yolo_labels", "created_at",
    ]
    text_cols = [
        "propagated_text_id", "propagation_id", "source_evidence_id", "source_visual_unit_id", "target_visual_unit_id",
        "modality", "text_kind", "text", "text_sha256", "propagation_confidence", "yolo_overlap_labels",
        "source_text_sha256", "source_text_len", "propagated_text_len", "created_at",
    ]
    if prop_rows:
        dst.executemany(
            "insert into semantic_propagation (" + ",".join(prop_cols) + ") values (" + ",".join(["?"] * len(prop_cols)) + ")",
            [[r.get(c) for c in prop_cols] for r in prop_rows],
        )
    if text_out_rows:
        dst.executemany(
            "insert into propagated_evidence_text (" + ",".join(text_cols) + ") values (" + ",".join(["?"] * len(text_cols)) + ")",
            [[r.get(c) for c in text_cols] for r in text_out_rows],
        )
    dst.commit()
    dst.close()

    write_csv(out / "manifests" / "semantic_propagation_manifest.csv", prop_rows, prop_cols)
    write_csv(out / "manifests" / "propagated_evidence_text_manifest.csv", text_out_rows, text_cols)

    elapsed = (dt.datetime.now() - started).total_seconds()
    status = "PASS" if prop_rows and not problems else ("PASS_WITH_REVIEW" if prop_rows else "FAIL")
    summary = {
        "validation_status": status,
        "schema_version": SCHEMA_VERSION,
        "elapsed_seconds": round(elapsed, 3),
        "mode": "read_staging_db_only_yolo_gated_no_model_rerun",
        "source_safety": "read_only_no_move_no_delete_no_rename_no_original_media_access_required",
        "network": "not_required_not_used",
        "model_download": "not_required_not_used",
        "run_root": args.run_root,
        "staging_db": args.staging_db,
        "settings": {
            "radius": args.radius,
            "max_time_delta_ms": args.max_time_delta_ms,
            "min_source_text_len": args.min_source_text_len,
            "yolo_gate_enabled": not args.disable_yolo_gate,
            "allow_missing_yolo_labels": args.allow_missing_yolo_labels,
        },
        "counts": {
            "visual_unit_rows": len(visual_rows),
            "model_evidence_rows": len(model_rows),
            "evidence_text_rows": len(text_rows),
            "direct_qwenvl_visual_units": len(direct_qwen_vus),
            "eligible_qwenvl_video_sources": len(qwen_sources),
            "video_groups": len(groups),
            "yolo_label_visual_units": len(yolo_labels),
            "candidate_neighbor_pairs_after_basic_filters": candidate_pairs,
            "propagation_rows": len(prop_rows),
            "propagated_text_rows": len(text_out_rows),
            "source_count_yolo_missing": source_count_yolo_missing,
            "target_count_yolo_missing": target_count_yolo_missing,
        },
        "yolo_debug": yolo_debug,
        "blocked_counts": blocked_counts,
        "problems": problems,
        "outputs": {
            "sqlite": str(out_db),
            "semantic_propagation_manifest_csv": str(out / "manifests" / "semantic_propagation_manifest.csv"),
            "propagated_evidence_text_manifest_csv": str(out / "manifests" / "propagated_evidence_text_manifest.csv"),
            "summary_json": str(out / "reports" / "stop03_5c_semantic_propagation_summary.json"),
            "summary_md": str(out / "reports" / "stop03_5c_semantic_propagation_summary.md"),
        },
    }

    with open(out / "reports" / "stop03_5c_semantic_propagation_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    md = []
    md.append("# Stop03-5C Semantic Propagation v2 - YOLO Gate\n")
    md.append(f"- validation_status: `{summary['validation_status']}`")
    md.append(f"- schema_version: `{SCHEMA_VERSION}`")
    md.append("- mode: `read_staging_db_only_yolo_gated_no_model_rerun`")
    md.append("- source_safety: `read_only_no_move_no_delete_no_rename_no_original_media_access_required`")
    md.append("- network: `not_required_not_used`")
    md.append("- model_download: `not_required_not_used`\n")
    md.append("## Settings")
    for k, v in summary["settings"].items():
        md.append(f"- {k}: `{v}`")
    md.append("\n## Counts")
    for k, v in summary["counts"].items():
        md.append(f"- {k}: `{v}`")
    md.append("\n## YOLO debug")
    for k, v in yolo_debug.items():
        md.append(f"- {k}: `{v}`")
    md.append("\n## Blocked counts")
    md.append(f"- blocked_counts: `{blocked_counts}`")
    md.append("\n## Decision")
    if status == "PASS":
        md.append("YOLO-gated semantic propagation passed. Propagated text contains only YOLO-supported shared object semantics, not full Qwen-VL text.")
    elif prop_rows:
        md.append("Propagation produced rows but has review notes. Inspect blocked_counts/problems before using as final input.")
    else:
        md.append("No propagation rows were produced. Check whether YOLO labels were available in staging DB or whether the gate was too strict.")
    md.append("\n## Outputs")
    for k, v in summary["outputs"].items():
        md.append(f"- {k}: `{v}`")
    with open(out / "reports" / "stop03_5c_semantic_propagation_summary.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print("== Stop03-5C semantic propagation v2 finished ==")
    print(json.dumps({
        "validation_status": status,
        "propagation_rows": len(prop_rows),
        "propagated_text_rows": len(text_out_rows),
        "direct_qwenvl_visual_units": len(direct_qwen_vus),
        "eligible_qwenvl_video_sources": len(qwen_sources),
        "yolo_label_visual_units": len(yolo_labels),
        "blocked_counts": blocked_counts,
        "summary_md": str(out / "reports" / "stop03_5c_semantic_propagation_summary.md"),
        "sqlite": str(out_db),
    }, ensure_ascii=False, indent=2))
    return 0 if status in ("PASS", "PASS_WITH_REVIEW") else 2


if __name__ == "__main__":
    raise SystemExit(main())
