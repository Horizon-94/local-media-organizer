#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop03-2 FIX v2 candidate queues from existing Stop03-1 evidence.

Reads only existing Stop03-1 YOLOE + visual embedding results and Stop02
manifests. It does not run YOLOE, visual embedding, Qwen-VL, OCR, or modify
source media/formal indexes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


POLICY_VERSION = "stop03_2_candidate_queues_fix_v2_20260708"
SCRIPT_VERSION = "stop03_2_candidate_queues_from_stop03_1_fix_v2_20260708"

VIDEO_CANDIDATE_MIN_GAP_MS = 20000
HARD_SCENE_CHANGE_DISTANCE = 0.42
MAJOR_OBJECT_SET_CHANGE = 0.70
REPEAT_SIMILARITY_DISTANCE = 0.12

OCR_PATH_KEYWORDS = [
    "screen", "screenshot", "screenrecording", "screen_recording",
    "screen-recording", "record screen", "recorded screen", "rpreplay",
    "录屏", "屏幕录制", "截屏", "截图", "屏幕", "微信图片", "企业微信",
    "网页", "聊天记录", "文档", "合同", "发票", "收据", "菜单",
    "牌照", "车牌", "路牌", "招牌", "字幕",
]

OCR_LABEL_KEYWORDS = [
    "text", "sign", "screen", "monitor", "display", "phone", "cell phone",
    "laptop", "computer", "tv", "keyboard", "book", "paper", "document",
    "poster", "billboard", "traffic sign", "license plate", "menu",
    "whiteboard", "label",
]

IMAGE_MARKER_FIELDS = [
    "Rating", "XMP:Rating", "xmp:Rating", "Label", "XMP:Label",
    "ColorClass", "Marked", "Pick", "RatingPercent", "photoshop:Urgency",
    "photoshop:SupplementalCategories", "dc:subject", "lr:hierarchicalSubject",
]

COMMON_FIELDS = [
    "candidate_id", "queue_type", "visual_unit_id", "visual_unit_type",
    "visual_file", "visual_file_sha256", "original_source_file_id",
    "original_source_content_id", "original_source_path_at_processing_time",
    "source_relative_path", "time_position_ms", "preview_role",
    "source_manifest", "policy_version", "candidate_score", "reason_codes",
    "selected_at",
]

QWENVL_EXTRA_FIELDS = [
    "source_group_id", "source_group_kind", "source_group_frame_count",
    "video_frame_rank", "video_candidate_budget", "nearest_selected_gap_ms",
    "min_gap_ms", "min_gap_broken", "min_gap_exception_reason",
    "neighbor_prev_3_ids", "neighbor_next_3_ids",
    "neighbor_embedding_distance_min", "neighbor_embedding_distance_mean",
    "neighbor_yoloe_label_jaccard_max", "neighbor_yoloe_label_jaccard_mean",
    "high_value_category", "is_user_marked_image", "image_marker_fields",
    "image_marker_values", "timelapse_sequence_id", "timelapse_sequence_size",
    "timelapse_selected_count", "timelapse_change_score",
]

OCR_EXTRA_FIELDS = [
    "ocr_trigger_source", "ocr_trigger_labels", "ocr_trigger_keywords",
    "ocr_trigger_reason_codes", "known_ocr_like_source_group",
]

DECISION_FIELDS = [
    "visual_unit_id", "visual_unit_type", "source_group_id",
    "is_qwenvl_candidate", "is_ocr_candidate", "qwenvl_candidate_id",
    "ocr_candidate_id", "qwenvl_reject_reason_codes",
    "ocr_reject_reason_codes", "candidate_score", "reason_codes",
]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stop03-2 FIX v2 candidate queue generator")
    p.add_argument("--stop03-1-base", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--out", default="")
    p.add_argument("--expected-units", type=int, default=0)
    return p.parse_args(argv)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def selected_at() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stop03_paths(base: Path, run_root: Path) -> Dict[str, Path]:
    return {
        "join_csv": base / "03_combined_report/manifests/stop03_1_visual_then_yoloe4_join_manifest.csv",
        "yoloe_jsonl": base / "02_yoloe4_full/manifests/stop03_1a_yoloe_result_manifest.jsonl",
        "yoloe_csv": base / "02_yoloe4_full/manifests/stop03_1a_yoloe_result_manifest.csv",
        "embedding_jsonl": base / "01_visual_embedding_full/manifests/stop03_1b_visual_embedding_result_manifest.jsonl",
        "embedding_csv": base / "01_visual_embedding_full/manifests/stop03_1b_visual_embedding_result_manifest.csv",
        "stop02_video_csv": run_root / "02_1_stop02_video_frames/manifests/video_frame_c4s_step01_queue_manifest.csv",
        "stop02_image_jsonl": run_root / "02_2_stop02_image_preview/manifests/image_preview_visual_unit_manifest.jsonl",
    }


def read_csv(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, data: dict) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    atomic_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def stable_fields(preferred: Sequence[str], rows: Sequence[dict]) -> List[str]:
    fields = list(preferred)
    seen = set(fields)
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    return fields


def write_csv(path: Path, rows: Sequence[dict], preferred: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = stable_fields(preferred, rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    tmp.replace(path)


def parse_time_ms(row: dict) -> int:
    for key in ["time_position_ms", "estimated_frame_time_ms", "short_video_single_frame_fallback_time_ms"]:
        val = row.get(key)
        if val not in (None, ""):
            try:
                return int(float(str(val)))
            except ValueError:
                pass
    m = re.search(r"_t(\d+)ms", row.get("visual_file", "") or row.get("frame_file", ""))
    return int(m.group(1)) if m else 0


def queue_candidate_id(queue_type: str, visual_unit_id: str) -> str:
    return hashlib.sha256((POLICY_VERSION + queue_type + visual_unit_id).encode("utf-8")).hexdigest()


def source_group_id(row: dict) -> str:
    for key in [
        "original_source_content_id", "parent_source_content_id",
        "original_source_file_id", "parent_source_file_id",
        "original_source_path_at_processing_time", "parent_source_path_at_processing_time",
        "source_relative_path",
    ]:
        val = row.get(key)
        if val:
            return str(val)
    return "unknown_source_group"


def normalize_key(path: str) -> str:
    return str(path or "").strip()


def parse_labels(yoloe_row: dict) -> Tuple[List[str], List[dict]]:
    detections: List[dict] = []
    labels: List[str] = []
    raw = yoloe_row.get("detections_json", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                detections = [d for d in parsed if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass
    for det in detections:
        label = str(det.get("label") or det.get("class_name") or det.get("name") or "").strip()
        if label:
            labels.append(label)
    if not labels and yoloe_row.get("detected_labels_json"):
        try:
            parsed = json.loads(yoloe_row["detected_labels_json"])
            if isinstance(parsed, dict):
                labels.extend(str(k) for k, v in parsed.items() if v)
            elif isinstance(parsed, list):
                labels.extend(str(x) for x in parsed)
        except json.JSONDecodeError:
            pass
    if not labels and yoloe_row.get("detected_labels"):
        labels.extend(x.strip() for x in str(yoloe_row["detected_labels"]).replace(",", "|").split("|") if x.strip())
    return sorted(set(labels)), detections


def detection_conf(det: dict) -> float:
    for key in ["confidence", "conf", "score"]:
        val = det.get(key)
        if val not in (None, ""):
            try:
                return float(val)
            except ValueError:
                pass
    return 0.25


def parse_embedding(row: dict) -> List[float]:
    raw = row.get("embedding_json", "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [float(x) for x in parsed] if isinstance(parsed, list) else []


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(2.0, 1.0 - dot / (na * nb)))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = {x.lower() for x in a}
    sb = {x.lower() for x in b}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def label_change(a: Iterable[str], b: Iterable[str]) -> float:
    return 1.0 - jaccard(a, b)


def contains_any(text: str, keywords: Sequence[str]) -> List[str]:
    folded = text.lower()
    return sorted({kw for kw in keywords if kw.lower() in folded})


def is_timelapse(unit: dict) -> bool:
    return "timelapse" in (unit.get("preview_role") or "").lower() or bool(unit.get("sequence_id"))


def is_normal_image(unit: dict) -> bool:
    return unit["visual_unit_type"] != "video_frame" and not is_timelapse(unit)


def load_xmp_sidecar_fields(source_path: str) -> Dict[str, str]:
    if not source_path:
        return {}
    p = Path(source_path)
    candidates = [p.with_suffix(".xmp"), Path(str(p) + ".xmp")]
    text = ""
    for cand in candidates:
        if cand.exists() and cand.is_file():
            try:
                text = cand.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            break
    if not text:
        return {}
    found: Dict[str, str] = {}
    for field in IMAGE_MARKER_FIELDS:
        bare = field.split(":")[-1]
        m = re.search(rf"{re.escape(bare)}=['\"]([^'\"]+)['\"]", text, re.I)
        if m:
            found[field] = m.group(1)
    return found


def marker_fields(row: dict, supplemental: dict) -> Tuple[bool, List[str], Dict[str, str], bool]:
    found: Dict[str, str] = {}
    for source in [row, supplemental]:
        for field in IMAGE_MARKER_FIELDS:
            val = source.get(field)
            if val not in (None, ""):
                found[field] = str(val)
    sidecar = load_xmp_sidecar_fields(row.get("original_source_path_at_processing_time", ""))
    found.update(sidecar)
    marked = False
    for field, value in found.items():
        folded = value.strip().lower()
        if field.lower().endswith("rating") or field == "RatingPercent":
            try:
                marked = marked or float(folded) > 0
            except ValueError:
                marked = marked or folded not in {"", "0", "none", "false"}
        else:
            marked = marked or folded not in {"", "0", "none", "false", "rejected", "reject"}
    metadata_unavailable = not bool(found)
    return marked, sorted(found), found, metadata_unavailable


def video_budget(frame_count: int) -> int:
    if frame_count <= 3:
        return 1
    if frame_count <= 10:
        return 1
    if frame_count <= 30:
        return 2
    if frame_count <= 80:
        return 4
    return 6


def normalize_unit(join: dict, yoloe: dict, emb: dict, image_supp: Dict[str, dict], video_supp: Dict[str, dict]) -> dict:
    labels, detections = parse_labels(yoloe)
    detection_count = int(float(yoloe.get("detection_count") or join.get("yoloe_detection_count") or len(detections) or 0))
    visual_id = join["visual_unit_id"]
    supp = image_supp.get(visual_id) or video_supp.get(visual_id) or {}
    unit = {
        **join,
        **{f"supp_{k}": v for k, v in supp.items()},
        "visual_unit_id": visual_id,
        "visual_unit_type": join.get("visual_unit_type", ""),
        "visual_file": join.get("visual_file") or yoloe.get("visual_file") or emb.get("visual_file", ""),
        "visual_file_sha256": join.get("visual_file_sha256") or emb.get("visual_file_sha256", ""),
        "original_source_file_id": join.get("original_source_file_id") or yoloe.get("parent_source_file_id", ""),
        "original_source_content_id": join.get("original_source_content_id") or yoloe.get("parent_source_content_id", ""),
        "original_source_path_at_processing_time": join.get("original_source_path_at_processing_time") or yoloe.get("parent_source_path_at_processing_time", ""),
        "source_relative_path": join.get("source_relative_path") or yoloe.get("source_relative_path", ""),
        "time_position_ms": parse_time_ms({**yoloe, **join}),
        "preview_role": join.get("preview_role") or yoloe.get("preview_role") or supp.get("preview_role", ""),
        "source_manifest": join.get("source_manifest") or yoloe.get("source_manifest", ""),
        "source_group_id": source_group_id({**yoloe, **join}),
        "source_group_kind": "video" if join.get("visual_unit_type") == "video_frame" else ("timelapse" if (join.get("preview_role") == "timelapse_keyframe" or supp.get("sequence_id")) else "normal_image"),
        "sequence_id": str(supp.get("sequence_id") or ""),
        "representative_position": supp.get("representative_position", ""),
        "yoloe_labels": labels,
        "yoloe_detections": detections,
        "yoloe_detection_count": detection_count,
        "embedding": parse_embedding(emb),
        "embedding_vector_sha256": emb.get("embedding_vector_sha256", ""),
    }
    marked, fields, values, unavailable = marker_fields(unit, supp)
    unit["is_image_user_marked"] = marked
    unit["image_marker_fields"] = fields
    unit["image_marker_values"] = values
    unit["image_metadata_unavailable"] = unavailable if is_normal_image(unit) else False
    return unit


def neighbor_context(group: Sequence[dict], idx: int) -> dict:
    cur = group[idx]
    prevs = list(group[max(0, idx - 3):idx])
    nexts = list(group[idx + 1:idx + 4])
    neighbors = prevs + nexts
    distances = [cosine_distance(cur["embedding"], n["embedding"]) for n in neighbors if cur["embedding"] and n["embedding"]]
    jaccards = [jaccard(cur["yoloe_labels"], n["yoloe_labels"]) for n in neighbors]
    return {
        "neighbor_prev_3_ids": "|".join(n["visual_unit_id"] for n in prevs),
        "neighbor_next_3_ids": "|".join(n["visual_unit_id"] for n in nexts),
        "neighbor_embedding_distance_min": round(min(distances), 6) if distances else 0.0,
        "neighbor_embedding_distance_mean": round(sum(distances) / len(distances), 6) if distances else 0.0,
        "neighbor_yoloe_label_jaccard_max": round(max(jaccards), 6) if jaccards else 1.0,
        "neighbor_yoloe_label_jaccard_mean": round(sum(jaccards) / len(jaccards), 6) if jaccards else 1.0,
    }


def object_score(unit: dict) -> float:
    confs = [detection_conf(d) for d in unit["yoloe_detections"]]
    conf = max(confs) if confs else (0.35 if unit["yoloe_labels"] else 0.0)
    return min(1.0, 0.45 * min(unit["yoloe_detection_count"] / 8, 1) + 0.35 * min(len(unit["yoloe_labels"]) / 5, 1) + 0.20 * conf)


def score_video_unit(unit: dict, ctx: dict) -> Tuple[float, List[str]]:
    novelty = min(1.0, float(ctx["neighbor_embedding_distance_mean"]) / 0.45)
    object_change = 1.0 - float(ctx["neighbor_yoloe_label_jaccard_mean"])
    score = 0.34 * object_score(unit) + 0.30 * novelty + 0.22 * object_change + 0.14 * min(unit["yoloe_detection_count"] / 8, 1)
    reasons: List[str] = []
    if float(ctx["neighbor_embedding_distance_mean"]) >= HARD_SCENE_CHANGE_DISTANCE:
        reasons.append("hard_scene_change")
    if object_change >= MAJOR_OBJECT_SET_CHANGE:
        reasons.append("major_object_set_change")
    if unit["yoloe_detection_count"] >= 8 or len(unit["yoloe_labels"]) >= 4:
        reasons.append("high_information_jump")
    if ocr_reasons(unit)[0]:
        reasons.append("ocr_region_emerges")
    if not reasons:
        reasons.append("best_video_group_anchor")
    return round(score, 6), reasons


def can_break_gap(reasons: Sequence[str]) -> str:
    for r in ["hard_scene_change", "major_object_set_change", "high_information_jump", "ocr_region_emerges", "rare_object_in_video", "video_coverage_boundary"]:
        if r in reasons:
            return r
    return ""


def base_candidate(unit: dict, queue_type: str, score: float, reasons: Sequence[str], ts: str) -> dict:
    return {
        "candidate_id": queue_candidate_id(queue_type, unit["visual_unit_id"]),
        "queue_type": queue_type,
        "visual_unit_id": unit["visual_unit_id"],
        "visual_unit_type": unit["visual_unit_type"],
        "visual_file": unit["visual_file"],
        "visual_file_sha256": unit["visual_file_sha256"],
        "original_source_file_id": unit["original_source_file_id"],
        "original_source_content_id": unit["original_source_content_id"],
        "original_source_path_at_processing_time": unit["original_source_path_at_processing_time"],
        "source_relative_path": unit["source_relative_path"],
        "time_position_ms": unit["time_position_ms"],
        "preview_role": unit["preview_role"],
        "source_manifest": unit["source_manifest"],
        "policy_version": POLICY_VERSION,
        "candidate_score": score,
        "reason_codes": "|".join(reasons),
        "selected_at": ts,
    }


def select_video_group(group: Sequence[dict], ts: str) -> Tuple[List[dict], Dict[str, dict], dict]:
    group = sorted(group, key=lambda u: (u["time_position_ms"], u["visual_unit_id"]))
    budget = video_budget(len(group))
    contexts = {u["visual_unit_id"]: neighbor_context(group, i) for i, u in enumerate(group)}
    for idx, unit in enumerate(group):
        score, reasons = score_video_unit(unit, contexts[unit["visual_unit_id"]])
        if idx == 0 or idx == len(group) - 1:
            reasons = sorted(set([*reasons, "video_coverage_boundary"]))
        unit["_score"] = score
        unit["_reasons"] = reasons
    selected: List[dict] = []
    for unit in sorted(group, key=lambda u: (-u["_score"], u["time_position_ms"], u["visual_unit_id"])):
        if len(selected) >= budget:
            break
        nearest = min((abs(unit["time_position_ms"] - s["time_position_ms"]) for s in selected), default=None)
        exception = ""
        if nearest is not None and nearest < VIDEO_CANDIDATE_MIN_GAP_MS:
            exception = can_break_gap(unit["_reasons"])
            if not exception:
                continue
        selected.append(unit)
    if not selected:
        selected.append(max(group, key=lambda u: (u["_score"], -abs(u["time_position_ms"] - group[len(group) // 2]["time_position_ms"]))))
    rare_labels = Counter(label for u in group for label in u["yoloe_labels"])
    for unit in group:
        if any(rare_labels[l] == 1 for l in unit["yoloe_labels"]):
            unit["_reasons"] = sorted(set([*unit["_reasons"], "rare_object_in_video"]))
    selected_ids = {u["visual_unit_id"] for u in selected}
    candidates: List[dict] = []
    decisions: Dict[str, dict] = {}
    for rank, unit in enumerate(sorted(selected, key=lambda u: u["time_position_ms"]), 1):
        ctx = contexts[unit["visual_unit_id"]]
        nearest = min((abs(unit["time_position_ms"] - s["time_position_ms"]) for s in selected if s["visual_unit_id"] != unit["visual_unit_id"]), default="")
        gap_broken = nearest != "" and nearest < VIDEO_CANDIDATE_MIN_GAP_MS
        exception = can_break_gap(unit["_reasons"]) if gap_broken else ""
        c = base_candidate(unit, "qwenvl_high_value", unit["_score"], unit["_reasons"], ts)
        c.update(ctx)
        c.update({
            "source_group_id": unit["source_group_id"],
            "source_group_kind": "video",
            "source_group_frame_count": len(group),
            "video_frame_rank": rank,
            "video_candidate_budget": budget,
            "nearest_selected_gap_ms": nearest,
            "min_gap_ms": VIDEO_CANDIDATE_MIN_GAP_MS,
            "min_gap_broken": gap_broken,
            "min_gap_exception_reason": exception,
            "high_value_category": "video_frame_candidate",
            "is_user_marked_image": False,
            "image_marker_fields": "",
            "image_marker_values": "",
            "timelapse_sequence_id": "",
            "timelapse_sequence_size": "",
            "timelapse_selected_count": "",
            "timelapse_change_score": "",
        })
        candidates.append(c)
    for unit in group:
        decisions[unit["visual_unit_id"]] = {
            "candidate_score": unit["_score"],
            "reason_codes": "|".join(unit["_reasons"]) if unit["visual_unit_id"] in selected_ids else "",
            "qwenvl_reject_reason_codes": "" if unit["visual_unit_id"] in selected_ids else "not_selected_video_budget_min_gap_or_lower_score",
        }
    return candidates, decisions, {"budget": budget, "selected_count": len(candidates), "budget_exceeded": len(candidates) > budget}


def select_timelapse_sequence(group: Sequence[dict], ts: str) -> Tuple[List[dict], Dict[str, dict], dict]:
    group = sorted(group, key=lambda u: (u.get("representative_position") or "", u["visual_unit_id"]))
    distances = []
    label_changes = []
    for a, b in zip(group, group[1:]):
        distances.append(cosine_distance(a["embedding"], b["embedding"]))
        label_changes.append(label_change(a["yoloe_labels"], b["yoloe_labels"]))
    change_score = round(max(distances + label_changes + [0.0]), 6)
    if change_score < 0.18:
        selected = [group[len(group) // 2]]
        policy = "timelapse_low_change_middle_frame"
    elif change_score < 0.40:
        selected = [group[0], group[-1]] if len(group) > 1 else [group[0]]
        policy = "timelapse_medium_change_edges"
    else:
        selected = list(group)
        policy = "timelapse_high_change_all_representatives"
    decisions: Dict[str, dict] = {}
    candidates = []
    selected_ids = {u["visual_unit_id"] for u in selected}
    for rank, unit in enumerate(selected, 1):
        c = base_candidate(unit, "qwenvl_high_value", max(0.45, change_score), [policy], ts)
        c.update({
            "source_group_id": unit["source_group_id"],
            "source_group_kind": "timelapse",
            "source_group_frame_count": len(group),
            "video_frame_rank": rank,
            "video_candidate_budget": len(selected),
            "nearest_selected_gap_ms": "",
            "min_gap_ms": "",
            "min_gap_broken": False,
            "min_gap_exception_reason": "",
            "neighbor_prev_3_ids": "",
            "neighbor_next_3_ids": "",
            "neighbor_embedding_distance_min": min(distances) if distances else 0.0,
            "neighbor_embedding_distance_mean": round(sum(distances) / len(distances), 6) if distances else 0.0,
            "neighbor_yoloe_label_jaccard_max": "",
            "neighbor_yoloe_label_jaccard_mean": "",
            "high_value_category": "timelapse_candidate",
            "is_user_marked_image": False,
            "image_marker_fields": "",
            "image_marker_values": "",
            "timelapse_sequence_id": unit["sequence_id"],
            "timelapse_sequence_size": len(group),
            "timelapse_selected_count": len(selected),
            "timelapse_change_score": change_score,
        })
        candidates.append(c)
    for unit in group:
        decisions[unit["visual_unit_id"]] = {
            "candidate_score": change_score,
            "reason_codes": policy if unit["visual_unit_id"] in selected_ids else "",
            "qwenvl_reject_reason_codes": "" if unit["visual_unit_id"] in selected_ids else "not_selected_timelapse_change_policy",
        }
    return candidates, decisions, {"sequence_id": group[0]["sequence_id"], "size": len(group), "selected": len(selected), "policy": policy, "change_score": change_score}


def select_marked_image(unit: dict, ts: str) -> Tuple[Optional[dict], dict]:
    if not unit["is_image_user_marked"]:
        return None, {
            "candidate_score": 0.0,
            "reason_codes": "",
            "qwenvl_reject_reason_codes": "normal_image_unmarked_default_not_high_value",
        }
    c = base_candidate(unit, "qwenvl_high_value", 0.75, ["normal_image_user_marked"], ts)
    c.update({
        "source_group_id": unit["source_group_id"],
        "source_group_kind": "normal_image",
        "source_group_frame_count": 1,
        "video_frame_rank": "",
        "video_candidate_budget": "",
        "nearest_selected_gap_ms": "",
        "min_gap_ms": "",
        "min_gap_broken": False,
        "min_gap_exception_reason": "",
        "neighbor_prev_3_ids": "",
        "neighbor_next_3_ids": "",
        "neighbor_embedding_distance_min": "",
        "neighbor_embedding_distance_mean": "",
        "neighbor_yoloe_label_jaccard_max": "",
        "neighbor_yoloe_label_jaccard_mean": "",
        "high_value_category": "normal_image_candidate",
        "is_user_marked_image": True,
        "image_marker_fields": "|".join(unit["image_marker_fields"]),
        "image_marker_values": json.dumps(unit["image_marker_values"], ensure_ascii=False, sort_keys=True),
        "timelapse_sequence_id": "",
        "timelapse_sequence_size": "",
        "timelapse_selected_count": "",
        "timelapse_change_score": "",
    })
    return c, {
        "candidate_score": 0.75,
        "reason_codes": "normal_image_user_marked",
        "qwenvl_reject_reason_codes": "",
    }


def ocr_reasons(unit: dict) -> Tuple[List[str], List[str], List[str], bool]:
    haystack = " ".join([
        unit.get("source_relative_path", ""),
        unit.get("original_source_path_at_processing_time", ""),
        unit.get("visual_file", ""),
        unit.get("preview_role", ""),
    ])
    path_hits = contains_any(haystack, OCR_PATH_KEYWORDS)
    label_hits = []
    for label in unit["yoloe_labels"]:
        if contains_any(label, OCR_LABEL_KEYWORDS):
            label_hits.append(label)
    reason_codes: List[str] = []
    if path_hits:
        if any("screen" in h.lower() or "录屏" in h or "屏幕" in h or "rpreplay" in h.lower() for h in path_hits):
            reason_codes.append("ocr_path_screen_recording")
        if any("screenshot" in h.lower() or "截屏" in h or "截图" in h for h in path_hits):
            reason_codes.append("ocr_path_screenshot")
        if any(h in path_hits for h in ["文档", "合同", "发票", "收据", "菜单"]):
            reason_codes.append("ocr_path_document_like")
        reason_codes.append("ocr_path_text_like")
    if label_hits:
        reason_codes.append("ocr_yoloe_label_trigger")
    known = bool(contains_any(haystack, ["rpreplay", "录屏", "screen", "screenshot", "截屏", "截图"]))
    return sorted(set(reason_codes)), sorted(set(label_hits)), path_hits, known


def build_ocr_candidate(unit: dict, ts: str) -> Optional[dict]:
    reasons, labels, keywords, known = ocr_reasons(unit)
    if not reasons:
        return None
    score = min(1.0, 0.55 + 0.08 * len(labels) + 0.05 * len(keywords))
    c = base_candidate(unit, "ocr_trigger", round(score, 6), reasons, ts)
    c.update({
        "ocr_trigger_source": "path_or_metadata" if keywords else "yoloe_label",
        "ocr_trigger_labels": "|".join(labels),
        "ocr_trigger_keywords": "|".join(keywords),
        "ocr_trigger_reason_codes": "|".join(reasons),
        "known_ocr_like_source_group": known,
    })
    return c


def build_queues(units: Sequence[dict]) -> Tuple[List[dict], List[dict], List[dict], dict]:
    ts = selected_at()
    qwenvl: List[dict] = []
    ocr: List[dict] = []
    decision_info: Dict[str, dict] = {}
    video_reports: List[dict] = []
    timelapse_reports: List[dict] = []
    image_audit: List[dict] = []

    by_video: Dict[str, List[dict]] = defaultdict(list)
    timelapse_groups: Dict[str, List[dict]] = defaultdict(list)
    normal_images: List[dict] = []
    for u in units:
        if u["visual_unit_type"] == "video_frame":
            by_video[u["source_group_id"]].append(u)
        elif is_timelapse(u):
            timelapse_groups[u["sequence_id"] or u["source_group_id"]].append(u)
        else:
            normal_images.append(u)

    for gid, group in by_video.items():
        candidates, decisions, report = select_video_group(group, ts)
        qwenvl.extend(candidates)
        decision_info.update(decisions)
        video_reports.append({
            "source_group_id": gid,
            "frame_count": len(group),
            "video_candidate_budget": report["budget"],
            "selected_count": report["selected_count"],
            "budget_exceeded": report["budget_exceeded"],
        })

    for sid, group in timelapse_groups.items():
        candidates, decisions, report = select_timelapse_sequence(group, ts)
        qwenvl.extend(candidates)
        decision_info.update(decisions)
        timelapse_reports.append(report)

    for unit in normal_images:
        candidate, decision = select_marked_image(unit, ts)
        if candidate:
            qwenvl.append(candidate)
        decision_info[unit["visual_unit_id"]] = decision
        image_audit.append({
            "visual_unit_id": unit["visual_unit_id"],
            "source_relative_path": unit["source_relative_path"],
            "preview_role": unit["preview_role"],
            "is_image_user_marked": unit["is_image_user_marked"],
            "image_marker_fields": "|".join(unit["image_marker_fields"]),
            "image_marker_values": json.dumps(unit["image_marker_values"], ensure_ascii=False, sort_keys=True),
            "metadata_unavailable": unit["image_metadata_unavailable"],
            "selected_for_qwenvl": bool(candidate),
        })

    for unit in units:
        cand = build_ocr_candidate(unit, ts)
        if cand:
            ocr.append(cand)

    q_by_id = {c["visual_unit_id"]: c for c in qwenvl}
    o_by_id = {c["visual_unit_id"]: c for c in ocr}
    decisions_out = []
    for unit in units:
        q = q_by_id.get(unit["visual_unit_id"])
        o = o_by_id.get(unit["visual_unit_id"])
        info = decision_info.get(unit["visual_unit_id"], {"candidate_score": 0.0, "reason_codes": "", "qwenvl_reject_reason_codes": "no_qwenvl_rule_applied"})
        decisions_out.append({
            "visual_unit_id": unit["visual_unit_id"],
            "visual_unit_type": unit["visual_unit_type"],
            "source_group_id": unit["source_group_id"],
            "is_qwenvl_candidate": bool(q),
            "is_ocr_candidate": bool(o),
            "qwenvl_candidate_id": q["candidate_id"] if q else "",
            "ocr_candidate_id": o["candidate_id"] if o else "",
            "qwenvl_reject_reason_codes": "" if q else info.get("qwenvl_reject_reason_codes", ""),
            "ocr_reject_reason_codes": "" if o else "no_ocr_trigger_evidence",
            "candidate_score": q["candidate_score"] if q else info.get("candidate_score", 0.0),
            "reason_codes": "|".join(x for x in [info.get("reason_codes", ""), o.get("reason_codes", "") if o else ""] if x),
        })
    reports = {
        "video_budget_report": video_reports,
        "image_marker_audit": image_audit,
        "timelapse_selection_report": timelapse_reports,
    }
    return qwenvl, ocr, decisions_out, reports


def label_inventory(units: Sequence[dict]) -> List[dict]:
    counter = Counter(label for u in units for label in u["yoloe_labels"])
    rows = []
    for label, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
        rows.append({
            "label": label,
            "count": count,
            "ocr_trigger_label_match": bool(contains_any(label, OCR_LABEL_KEYWORDS)),
        })
    return rows


def build_summary(args: argparse.Namespace, out: Path, paths: Dict[str, Path], units: Sequence[dict], qwenvl: Sequence[dict], ocr: Sequence[dict], decisions: Sequence[dict], reports: dict, labels: Sequence[dict]) -> dict:
    q_ids = {c["visual_unit_id"] for c in qwenvl}
    o_ids = {c["visual_unit_id"] for c in ocr}
    video_groups = {u["source_group_id"] for u in units if u["visual_unit_type"] == "video_frame"}
    selected_video_groups = {c["source_group_id"] for c in qwenvl if c["source_group_kind"] == "video"}
    image_units = [u for u in units if is_normal_image(u)]
    marked_images = [u for u in image_units if u["is_image_user_marked"]]
    timelapse_groups = {u["sequence_id"] or u["source_group_id"] for u in units if u["visual_unit_type"] != "video_frame" and is_timelapse(u)}
    known_groups = defaultdict(bool)
    hit_groups = defaultdict(bool)
    for u in units:
        _, _, _, known = ocr_reasons(u)
        if known:
            known_groups[u["source_group_id"]] = True
        if u["visual_unit_id"] in o_ids:
            hit_groups[u["source_group_id"]] = True
    known_count = len(known_groups)
    hit_count = sum(1 for g in known_groups if hit_groups.get(g))
    missed_count = known_count - hit_count
    ocr_audit = []
    for u in units:
        reasons, labs, kws, known = ocr_reasons(u)
        if reasons or known:
            ocr_audit.append({
                "visual_unit_id": u["visual_unit_id"],
                "source_group_id": u["source_group_id"],
                "source_relative_path": u["source_relative_path"],
                "ocr_triggered": u["visual_unit_id"] in o_ids,
                "ocr_reason_codes": "|".join(reasons),
                "ocr_trigger_labels": "|".join(labs),
                "ocr_trigger_keywords": "|".join(kws),
                "known_ocr_like_source_group": known,
            })
    marker_inventory = Counter(field for u in image_units for field in u["image_marker_fields"])
    top_labels = sorted(labels, key=lambda r: (-int(r["count"]), r["label"]))[:20]
    summary = {
        "script_version": SCRIPT_VERSION,
        "policy_version": POLICY_VERSION,
        "input_visual_units": len(units),
        "qwenvl_total_count": len(qwenvl),
        "qwenvl_video_frame_count": sum(1 for c in qwenvl if c["source_group_kind"] == "video"),
        "qwenvl_image_marked_count": sum(1 for c in qwenvl if c["source_group_kind"] == "normal_image"),
        "qwenvl_timelapse_count": sum(1 for c in qwenvl if c["source_group_kind"] == "timelapse"),
        "ocr_total_count": len(ocr),
        "both_count": len(q_ids & o_ids),
        "neither_count": sum(1 for d in decisions if not d["is_qwenvl_candidate"] and not d["is_ocr_candidate"]),
        "video_source_group_count": len(video_groups),
        "video_selected_group_count": len(selected_video_groups),
        "video_min_one_per_group_pass": selected_video_groups == video_groups,
        "video_budget_exceeded_count": sum(max(0, int(r["selected_count"]) - int(r["video_candidate_budget"])) for r in reports["video_budget_report"]),
        "video_budget_exceeded_group_count": sum(1 for r in reports["video_budget_report"] if r["budget_exceeded"]),
        "top_20_video_groups_by_qwenvl_count": sorted(reports["video_budget_report"], key=lambda r: (-int(r["selected_count"]), r["source_group_id"]))[:20],
        "image_total_count": len(image_units),
        "image_marked_count": len(marked_images),
        "image_metadata_unavailable_count": sum(1 for u in image_units if u["image_metadata_unavailable"]),
        "image_high_value_selected_count": sum(1 for c in qwenvl if c["source_group_kind"] == "normal_image"),
        "image_marker_field_inventory": dict(marker_inventory),
        "timelapse_sequence_count": len(timelapse_groups),
        "timelapse_selected_total_count": sum(1 for c in qwenvl if c["source_group_kind"] == "timelapse"),
        "timelapse_selection_distribution": dict(Counter(r["selected"] for r in reports["timelapse_selection_report"])),
        "yoloe_label_inventory_count": len(labels),
        "top_yoloe_labels": top_labels,
        "ocr_trigger_label_hits": [r for r in labels if r["ocr_trigger_label_match"]],
        "ocr_trigger_path_keyword_hits": len([r for r in ocr_audit if r["ocr_trigger_keywords"]]),
        "known_ocr_like_source_group_count": known_count,
        "known_ocr_like_source_group_hit_count": hit_count,
        "known_ocr_like_source_group_missed_count": missed_count,
        "known_ocr_like_source_group_samples": sorted(list(known_groups))[:20],
        "policy_constants": {
            "VIDEO_CANDIDATE_MIN_GAP_MS": VIDEO_CANDIDATE_MIN_GAP_MS,
            "HARD_SCENE_CHANGE_DISTANCE": HARD_SCENE_CHANGE_DISTANCE,
            "MAJOR_OBJECT_SET_CHANGE": MAJOR_OBJECT_SET_CHANGE,
        },
        "input_stop03_1_base": str(Path(args.stop03_1_base).resolve()),
        "output_dir": str(out),
        "model_rerun": {"yoloe": False, "visual_embedding": False, "qwen_vl": False, "ocr": False},
        "ordinary_images_default_excluded_from_qwenvl": True,
        "not_exclusion_manifest": True,
    }
    if known_count and missed_count:
        summary["fail_reason"] = "known_ocr_like_source_group_missed"
    return summary, ocr_audit


def load_supplemental(paths: Dict[str, Path]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    video_supp: Dict[str, dict] = {}
    if paths["stop02_video_csv"].exists():
        for r in read_csv(paths["stop02_video_csv"]):
            fid = r.get("frame_id")
            if fid:
                video_supp[fid] = r
    image_supp: Dict[str, dict] = {}
    if paths["stop02_image_jsonl"].exists():
        for r in read_jsonl(paths["stop02_image_jsonl"]):
            vid = r.get("visual_unit_id")
            if vid:
                image_supp[vid] = r
    return image_supp, video_supp


def run(args: argparse.Namespace) -> Tuple[int, dict]:
    base = Path(args.stop03_1_base).expanduser().resolve()
    run_root = Path(args.run_root).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else run_root / f"03_2_stop03_candidate_queues_fix_v2_{now_stamp()}"
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[BLOCKED] output directory exists and is non-empty: {out}")
    paths = stop03_paths(base, run_root)
    missing_required = [str(paths[k]) for k in ["join_csv", "yoloe_jsonl", "yoloe_csv", "embedding_jsonl", "embedding_csv"] if not paths[k].exists()]
    if missing_required:
        raise SystemExit("[BLOCKED] missing required input files: " + ", ".join(missing_required))
    join_rows = read_csv(paths["join_csv"])
    yoloe_rows = read_jsonl(paths["yoloe_jsonl"])
    emb_rows = read_jsonl(paths["embedding_jsonl"])
    if args.expected_units and (len(join_rows) != args.expected_units or len(yoloe_rows) != args.expected_units or len(emb_rows) != args.expected_units):
        raise SystemExit(f"[FAIL] input row count mismatch: join={len(join_rows)} yoloe={len(yoloe_rows)} embedding={len(emb_rows)} expected={args.expected_units}")
    yoloe_by_id = {r["visual_unit_id"]: r for r in yoloe_rows}
    emb_by_id = {r["visual_unit_id"]: r for r in emb_rows}
    join_ids = {r["visual_unit_id"] for r in join_rows}
    if join_ids - set(yoloe_by_id) or join_ids - set(emb_by_id):
        raise SystemExit(f"[FAIL] missing aligned rows: yoloe={len(join_ids - set(yoloe_by_id))} embedding={len(join_ids - set(emb_by_id))}")
    image_supp, video_supp = load_supplemental(paths)
    units = [normalize_unit(r, yoloe_by_id[r["visual_unit_id"]], emb_by_id[r["visual_unit_id"]], image_supp, video_supp) for r in join_rows]
    qwenvl, ocr, decisions, reports = build_queues(units)
    labels = label_inventory(units)
    summary, ocr_audit = build_summary(args, out, paths, units, qwenvl, ocr, decisions, reports, labels)
    if summary.get("known_ocr_like_source_group_count", 0) and summary.get("known_ocr_like_source_group_missed_count", 0):
        raise SystemExit("[FAIL] known OCR-like source groups exist but were missed by OCR queue")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifests").mkdir(exist_ok=True)
    (out / "reports").mkdir(exist_ok=True)
    write_jsonl(out / "manifests/qwenvl_high_value_candidate_queue.jsonl", qwenvl)
    write_csv(out / "manifests/qwenvl_high_value_candidate_queue.csv", qwenvl, COMMON_FIELDS + QWENVL_EXTRA_FIELDS)
    write_jsonl(out / "manifests/ocr_trigger_candidate_queue.jsonl", ocr)
    write_csv(out / "manifests/ocr_trigger_candidate_queue.csv", ocr, COMMON_FIELDS + OCR_EXTRA_FIELDS)
    write_jsonl(out / "manifests/visual_unit_candidate_decision_manifest.jsonl", decisions)
    write_csv(out / "manifests/visual_unit_candidate_decision_manifest.csv", decisions, DECISION_FIELDS)
    write_csv(out / "manifests/yoloe_label_inventory.csv", labels, ["label", "count", "ocr_trigger_label_match"])
    write_json(out / "manifests/yoloe_label_inventory.json", {"labels": labels})
    write_json(out / "reports/stop03_2_candidate_summary.json", summary)
    atomic_text(out / "reports/stop03_2_candidate_summary.md", summary_md(summary))
    write_csv(out / "reports/video_budget_report.csv", reports["video_budget_report"], [])
    write_csv(out / "reports/image_marker_audit.csv", reports["image_marker_audit"], [])
    write_csv(out / "reports/timelapse_selection_report.csv", reports["timelapse_selection_report"], [])
    write_csv(out / "reports/ocr_trigger_audit.csv", ocr_audit, [])
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0, summary


def summary_md(summary: dict) -> str:
    keys = [
        "qwenvl_total_count", "qwenvl_video_frame_count", "qwenvl_image_marked_count",
        "qwenvl_timelapse_count", "ocr_total_count", "both_count", "neither_count",
        "video_source_group_count", "video_selected_group_count", "video_min_one_per_group_pass",
        "video_budget_exceeded_group_count", "image_total_count", "image_marked_count",
        "image_metadata_unavailable_count", "image_high_value_selected_count",
        "timelapse_sequence_count", "timelapse_selected_total_count",
        "known_ocr_like_source_group_count", "known_ocr_like_source_group_hit_count",
        "known_ocr_like_source_group_missed_count",
    ]
    lines = ["# Stop03-2 Candidate Summary FIX v2", ""]
    lines.extend(f"- {k}: {summary.get(k)}" for k in keys)
    lines.extend([
        "",
        "普通图片默认不进入 Qwen-VL；只有明确用户/相机/软件标记证据才进入。",
        "视频候选按 source group、预算、20 秒间隔和前后三帧上下文筛选。",
        "OCR 触发综合 YOLOE label、路径/文件名、manifest metadata 与 OCR-like canary。",
        "本阶段未运行 YOLOE、视觉向量、Qwen-VL 或 OCR。",
    ])
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    code, _summary = run(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
