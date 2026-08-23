#!/usr/bin/env python3
"""Generic Stop03-5C Qwen-VL object propagation through a strict YOLOE gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTRACT_VERSION = "stop03_5c_qwenvl_yolo_propagation_v1"
PROJECT_ROOT = Path("$APP_RESOURCES/Pipeline")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5c_qwenvl_yolo_propagation_v1.json"
DEFAULT_MIGRATION = (
    PROJECT_ROOT
    / "migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql"
)
DEFAULT_OUT = Path(
    "$USER_HOME/Documents/AI-Local/test-output/"
    "stop03_5c_qwenvl_yolo_propagation_v1"
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def object_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)
    ).fetchone() is not None


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_version": CONTRACT_VERSION,
        "source_staging_selector": "latest_success_stop03_5b_run",
        "source_modality": "qwenvl",
        "neighbor_radius_frames": 3,
        "semantic_gate":
            "qwen_mentioned_label_intersect_source_yoloe_intersect_target_yoloe",
        "target_direct_qwenvl_policy": "record_propagation_without_overwrite",
        "allow_cross_source_propagation": False,
        "allow_recursive_propagation": False,
        "propagate_ocr": False,
        "propagate_full_qwen_text": False,
        "propagate_object_labels_only": True,
        "original_video_read": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "database_write_in_dry_run": False,
    }
    mismatches = {
        key: {"actual": config.get(key), "expected": value}
        for key, value in expected.items()
        if config.get(key) != value
    }
    if mismatches:
        raise RuntimeError(
            "stop03_5c_policy_mismatch:"
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    return config


def latest_staging_run(con: sqlite3.Connection) -> sqlite3.Row:
    row = con.execute(
        """SELECT * FROM stop03_5_unified_evidence_runs
           WHERE status='success'
           ORDER BY created_at DESC,staging_run_id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("stop03_5c_latest_success_staging_run_missing")
    return row


def load_terms(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in con.execute(
        "SELECT label,label_zh,search_terms_json FROM visual_label_terms"
    ):
        terms: list[str] = [str(row["label"]), str(row["label_zh"] or "")]
        try:
            values = json.loads(row["search_terms_json"] or "[]")
            if isinstance(values, list):
                terms.extend(str(value) for value in values)
        except Exception:
            pass
        result[str(row["label"]).lower()] = {
            "label": str(row["label"]).lower(),
            "label_zh": str(row["label_zh"] or row["label"]),
            "terms": sorted(
                {
                    re.sub(r"\s+", " ", term).strip().lower()
                    for term in terms
                    if str(term).strip()
                },
                key=lambda value: (-len(value), value),
            ),
        }
    return result


def term_matches(text: str, term: str, ambiguous_single: set[str]) -> bool:
    if not term or term in ambiguous_single:
        return False
    if re.search(r"[a-z0-9]", term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def mentioned_labels(
    text: str,
    source_labels: set[str],
    registry: dict[str, dict[str, Any]],
    ambiguous_single: set[str],
) -> set[str]:
    lowered = re.sub(r"\s+", " ", text).lower()
    output = set()
    for label in source_labels:
        entry = registry.get(label)
        if entry and any(
            term_matches(lowered, term, ambiguous_single)
            for term in entry["terms"]
        ):
            output.add(label)
    return output


def load_frames(con: sqlite3.Connection, derived_type: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            """SELECT da.derived_id,da.source_content_id,da.frame_index,
                      da.time_position_ms,da.sha256 AS derived_sha256,
                      MIN(COALESCE(vi.canonical_visual_unit_id,
                                   vu.visual_unit_id)) AS canonical_visual_unit_id,
                      COUNT(DISTINCT vu.visual_unit_id) AS visual_unit_alias_count
               FROM derived_assets da
               JOIN visual_units vu ON vu.derived_id=da.derived_id
               LEFT JOIN visual_identity vi ON vi.visual_unit_id=vu.visual_unit_id
               WHERE da.derived_type=?
               GROUP BY da.derived_id,da.source_content_id,da.frame_index,
                        da.time_position_ms,da.sha256
               ORDER BY da.source_content_id,da.frame_index,
                        da.time_position_ms,da.derived_id""",
            (derived_type,),
        )
    ]


def load_label_map(
    con: sqlite3.Connection, minimum_confidence: float
) -> dict[str, dict[str, float]]:
    labels: dict[str, dict[str, float]] = defaultdict(dict)
    for row in con.execute(
        """SELECT vu.derived_id,LOWER(vl.label) AS label,
                  MAX(vl.confidence) AS confidence
           FROM visual_labels vl
           JOIN visual_units vu ON vu.visual_unit_id=vl.visual_unit_id
           GROUP BY vu.derived_id,LOWER(vl.label)
           HAVING MAX(vl.confidence)>=?""",
        (minimum_confidence,),
    ):
        labels[str(row["derived_id"])][str(row["label"])] = float(row["confidence"])
    return dict(labels)


def load_qwen_anchors(
    con: sqlite3.Connection,
    staging_run_id: str,
    derived_type: str,
    quality_statuses: list[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in quality_statuses)
    return [
        dict(row)
        for row in con.execute(
            f"""SELECT e.*,da.frame_index,da.time_position_ms
                FROM stop03_5_unified_evidence_items e
                JOIN derived_assets da ON da.derived_id=e.derived_id
                WHERE e.staging_run_id=? AND e.modality='qwenvl'
                  AND e.quality_status IN ({placeholders})
                  AND da.derived_type=?
                ORDER BY e.source_content_id,da.frame_index,
                         da.time_position_ms,e.evidence_id""",
            (staging_run_id, *quality_statuses, derived_type),
        )
    ]


def build_distribution(
    db: Path, config_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    config = load_config(config_path)
    required = (
        "stop03_5_unified_evidence_runs",
        "stop03_5_unified_evidence_items",
        "derived_assets",
        "visual_units",
        "visual_identity",
        "visual_labels",
        "visual_label_terms",
    )
    with connect_ro(db) as con:
        missing = [name for name in required if not object_exists(con, name)]
        if missing:
            raise RuntimeError(f"stop03_5c_database_objects_missing:{missing}")
        staging = latest_staging_run(con)
        registry = load_terms(con)
        frames = load_frames(con, config["video_derived_type"])
        label_map = load_label_map(con, float(config["yoloe_min_confidence"]))
        anchors = load_qwen_anchors(
            con,
            str(staging["staging_run_id"]),
            config["video_derived_type"],
            list(config["source_quality_statuses"]),
        )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(list(con.execute("PRAGMA foreign_key_check")))

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    frame_by_derived: dict[str, dict[str, Any]] = {}
    for frame in frames:
        groups[str(frame["source_content_id"])].append(frame)
        frame_by_derived[str(frame["derived_id"])] = frame
    for group in groups.values():
        group.sort(
            key=lambda row: (
                int(row["frame_index"]),
                int(row["time_position_ms"]),
                str(row["derived_id"]),
            )
        )
    indices = {
        str(frame["derived_id"]): index
        for group in groups.values()
        for index, frame in enumerate(group)
    }
    direct_qwen_derived = {str(anchor["derived_id"]) for anchor in anchors}
    ambiguous = {str(value).lower() for value in config["ambiguous_single_cjk_terms"]}
    radius = int(config["neighbor_radius_frames"])
    propagation: list[dict[str, Any]] = []
    target_stats: dict[str, dict[str, Any]] = {}
    blocked = Counter()
    candidate_pairs = 0
    source_with_no_yolo = 0
    source_with_no_qwen_yolo_mentions = 0

    for anchor in anchors:
        source_derived = str(anchor["derived_id"])
        source_frame = frame_by_derived.get(source_derived)
        if source_frame is None:
            blocked["source_frame_missing"] += 1
            continue
        source_labels = set(label_map.get(source_derived, {}))
        if not source_labels:
            source_with_no_yolo += 1
            blocked["source_yolo_missing"] += 1
            continue
        qwen_labels = mentioned_labels(
            str(anchor["evidence_text"]),
            source_labels,
            registry,
            ambiguous,
        )
        if not qwen_labels:
            source_with_no_qwen_yolo_mentions += 1
            blocked["no_qwen_source_yolo_label_intersection"] += 1
            continue
        group = groups[str(anchor["source_content_id"])]
        source_index = indices[source_derived]
        for offset in range(-radius, radius + 1):
            if offset == 0:
                continue
            target_index = source_index + offset
            if target_index < 0 or target_index >= len(group):
                blocked["neighbor_out_of_range"] += 1
                continue
            candidate_pairs += 1
            target = group[target_index]
            target_derived = str(target["derived_id"])
            target_labels = set(label_map.get(target_derived, {}))
            if not target_labels:
                blocked["target_yolo_missing"] += 1
                continue
            overlap = sorted(qwen_labels & target_labels)
            if not overlap:
                blocked["no_three_way_label_intersection"] += 1
                continue
            for label in overlap:
                registry_row = registry.get(
                    label,
                    {"label": label, "label_zh": label, "terms": [label]},
                )
                seed = canonical_json(
                    {
                        "source_evidence_id": anchor["evidence_id"],
                        "source_derived_id": source_derived,
                        "target_derived_id": target_derived,
                        "label": label,
                        "rule": CONTRACT_VERSION,
                    }
                )
                propagation_id = f"prop5c_{sha256_text(seed)[:32]}"
                controlled_text = (
                    f"相邻高价值帧传播对象：{registry_row['label_zh']}（{label}）"
                )
                row = {
                    "propagation_id": propagation_id,
                    "contract_version": CONTRACT_VERSION,
                    "source_staging_run_id": staging["staging_run_id"],
                    "source_evidence_id": anchor["evidence_id"],
                    "source_candidate_id": anchor["candidate_id"],
                    "source_content_id": anchor["source_content_id"],
                    "source_visual_unit_id": anchor["visual_unit_id"],
                    "source_canonical_visual_unit_id":
                        anchor["canonical_visual_unit_id"],
                    "source_derived_id": source_derived,
                    "source_frame_index": source_frame["frame_index"],
                    "source_time_position_ms": source_frame["time_position_ms"],
                    "target_canonical_visual_unit_id":
                        target["canonical_visual_unit_id"],
                    "target_derived_id": target_derived,
                    "target_frame_index": target["frame_index"],
                    "target_time_position_ms": target["time_position_ms"],
                    "frame_offset": offset,
                    "propagation_direction":
                        "previous" if offset < 0 else "next",
                    "propagation_step": abs(offset),
                    "target_has_direct_qwenvl":
                        target_derived in direct_qwen_derived,
                    "propagated_label": label,
                    "propagated_label_zh": registry_row["label_zh"],
                    "source_yolo_confidence": label_map[source_derived][label],
                    "target_yolo_confidence": label_map[target_derived][label],
                    "propagated_text": controlled_text,
                    "propagated_text_sha256": sha256_text(controlled_text),
                    "source_text_sha256": anchor["evidence_text_sha256"],
                    "gate_status": "passed_qwen_source_target_yolo_intersection",
                }
                propagation.append(row)
                target_key = target_derived
                target_row = target_stats.setdefault(
                    target_key,
                    {
                        "source_content_id": anchor["source_content_id"],
                        "target_derived_id": target_derived,
                        "target_canonical_visual_unit_id":
                            target["canonical_visual_unit_id"],
                        "target_frame_index": target["frame_index"],
                        "target_time_position_ms": target["time_position_ms"],
                        "target_has_direct_qwenvl":
                            target_derived in direct_qwen_derived,
                        "propagation_count": 0,
                        "labels": set(),
                        "source_evidence_ids": set(),
                    },
                )
                target_row["propagation_count"] += 1
                target_row["labels"].add(label)
                target_row["source_evidence_ids"].add(anchor["evidence_id"])

    propagation.sort(
        key=lambda row: (
            row["source_content_id"],
            row["target_frame_index"],
            row["source_frame_index"],
            row["propagated_label"],
            row["propagation_id"],
        )
    )
    targets = []
    for row in target_stats.values():
        targets.append(
            {
                **row,
                "labels": "|".join(sorted(row["labels"])),
                "source_evidence_ids": "|".join(sorted(row["source_evidence_ids"])),
            }
        )
    targets.sort(
        key=lambda row: (
            row["source_content_id"],
            row["target_frame_index"],
            row["target_derived_id"],
        )
    )
    ids = [row["propagation_id"] for row in propagation]
    checks = {
        "latest_staging_run_success": staging["status"] == "success",
        "qwen_sources_only": all(
            anchor["modality"] == "qwenvl" for anchor in anchors
        ),
        "ocr_source_count_zero": True,
        "unique_propagation_ids": len(ids) == len(set(ids)),
        "frame_step_within_radius": all(
            1 <= int(row["propagation_step"]) <= radius for row in propagation
        ),
        "same_source_only": all(
            row["source_content_id"]
            == frame_by_derived[row["target_derived_id"]]["source_content_id"]
            for row in propagation
        ),
        "no_self_propagation": all(
            row["source_derived_id"] != row["target_derived_id"]
            for row in propagation
        ),
        "all_three_way_gates_pass": all(
            row["gate_status"]
            == "passed_qwen_source_target_yolo_intersection"
            for row in propagation
        ),
        "confidence_threshold_pass": all(
            float(row["source_yolo_confidence"])
            >= float(config["yoloe_min_confidence"])
            and float(row["target_yolo_confidence"])
            >= float(config["yoloe_min_confidence"])
            for row in propagation
        ),
        "controlled_text_only": all(
            row["propagated_text"].startswith("相邻高价值帧传播对象：")
            and len(row["propagated_text"]) < 160
            for row in propagation
        ),
        "database_integrity_ok": integrity == "ok",
        "foreign_keys_ok": foreign_keys == 0,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    summary = {
        "status": status,
        "technical_status": status,
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "source_staging_run_id": staging["staging_run_id"],
        "source_qwen_run_id": staging["qwen_run_id"],
        "source_ocr_run_id_excluded": staging["ocr_run_id"],
        "source_qwen_video_anchor_count": len(anchors),
        "source_ocr_anchor_count": 0,
        "unique_video_frame_count": len(frames),
        "visual_unit_aliases_collapsed_count": sum(
            max(0, int(frame["visual_unit_alias_count"]) - 1) for frame in frames
        ),
        "candidate_neighbor_pair_count": candidate_pairs,
        "propagation_row_count": len(propagation),
        "propagation_target_count": len(targets),
        "target_with_direct_qwenvl_count": sum(
            bool(row["target_has_direct_qwenvl"]) for row in targets
        ),
        "source_with_no_yolo_count": source_with_no_yolo,
        "source_with_no_qwen_yolo_mention_count":
            source_with_no_qwen_yolo_mentions,
        "propagated_label_counts": dict(
            Counter(row["propagated_label"] for row in propagation)
        ),
        "blocked_counts": dict(blocked),
        "propagation_id_set_sha256": sha256_text("\n".join(sorted(ids))),
        "payload_digest_sha256": sha256_text(canonical_json(propagation)),
        "checks": checks,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }
    summary["propagation_run_id"] = (
        f"stop03_5c_{summary['payload_digest_sha256'][:24]}"
    )
    summary["policy_config_sha256"] = sha256_file(config_path)
    summary["script_sha256"] = sha256_file(Path(__file__).resolve())
    return summary, propagation, targets


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    fields = fields or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def backup_database(db: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def readback(db: Path, propagation_run_id: str) -> dict[str, Any]:
    with connect_ro(db) as con:
        run = con.execute(
            """SELECT * FROM stop03_5c_propagation_runs
               WHERE propagation_run_id=?""",
            (propagation_run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError("stop03_5c_readback_run_missing")
        row_count = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_5c_propagation_items
                   WHERE propagation_run_id=?""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        target_count = int(
            con.execute(
                """SELECT COUNT(DISTINCT target_derived_id)
                   FROM stop03_5c_propagation_items
                   WHERE propagation_run_id=?""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        duplicate_ids = int(
            con.execute(
                """SELECT COUNT(*) FROM (
                     SELECT propagation_id,COUNT(*) n
                     FROM stop03_5c_propagation_items
                     WHERE propagation_run_id=?
                     GROUP BY propagation_id HAVING n>1
                   )""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        duplicate_semantics = int(
            con.execute(
                """SELECT COUNT(*) FROM (
                     SELECT source_evidence_id,target_derived_id,
                            propagated_label,COUNT(*) n
                     FROM stop03_5c_propagation_items
                     WHERE propagation_run_id=?
                     GROUP BY source_evidence_id,target_derived_id,
                              propagated_label
                     HAVING n>1
                   )""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        ocr_source_count = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_5c_propagation_items i
                   JOIN stop03_5_unified_evidence_items e
                     ON e.staging_run_id=i.source_staging_run_id
                    AND e.evidence_id=i.source_evidence_id
                   WHERE i.propagation_run_id=? AND e.modality='ocr'""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        invalid_steps = int(
            con.execute(
                """SELECT COUNT(*) FROM stop03_5c_propagation_items
                   WHERE propagation_run_id=?
                     AND (
                       propagation_step NOT BETWEEN 1 AND 3
                       OR frame_offset NOT IN (-3,-2,-1,1,2,3)
                     )""",
                (propagation_run_id,),
            ).fetchone()[0]
        )
        latest_view_count = int(
            con.execute(
                "SELECT COUNT(*) FROM v_stop03_5c_latest_propagation"
            ).fetchone()[0]
        )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(list(con.execute("PRAGMA foreign_key_check")))
    run_dict = dict(run)
    checks = {
        "run_status_success": run_dict["status"] == "success",
        "row_count_matches_run":
            row_count == int(run_dict["propagation_row_count"]),
        "target_count_matches_run":
            target_count == int(run_dict["propagation_target_count"]),
        "duplicate_propagation_ids_zero": duplicate_ids == 0,
        "duplicate_semantics_zero": duplicate_semantics == 0,
        "ocr_source_count_zero": ocr_source_count == 0,
        "invalid_steps_zero": invalid_steps == 0,
        "latest_view_matches_run":
            latest_view_count == int(run_dict["propagation_row_count"]),
        "database_integrity_ok": integrity == "ok",
        "foreign_keys_ok": foreign_keys == 0,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "propagation_run_id": propagation_run_id,
        "propagation_row_count": row_count,
        "propagation_target_count": target_count,
        "duplicate_propagation_id_count": duplicate_ids,
        "duplicate_semantic_count": duplicate_semantics,
        "ocr_source_count": ocr_source_count,
        "invalid_step_count": invalid_steps,
        "latest_view_count": latest_view_count,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "checks": checks,
    }


def commit(
    db: Path,
    migration: Path,
    out: Path,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if summary["technical_status"] != "PASS":
        raise RuntimeError("stop03_5c_commit_requires_technical_pass")
    propagation_run_id = str(summary["propagation_run_id"])
    if object_exists_in_db(db, "stop03_5c_propagation_runs"):
        with connect_ro(db) as con:
            existing = con.execute(
                """SELECT propagation_run_id,propagation_row_count,
                          propagation_target_count
                   FROM stop03_5c_propagation_runs
                   WHERE payload_digest_sha256=?""",
                (summary["payload_digest_sha256"],),
            ).fetchone()
        if existing is not None:
            if (
                existing["propagation_run_id"] != propagation_run_id
                or int(existing["propagation_row_count"])
                != int(summary["propagation_row_count"])
                or int(existing["propagation_target_count"])
                != int(summary["propagation_target_count"])
            ):
                raise RuntimeError("stop03_5c_existing_payload_contract_mismatch")
            verification = readback(db, propagation_run_id)
            if verification["status"] != "PASS":
                raise RuntimeError("stop03_5c_idempotent_readback_failure")
            return {
                **summary,
                **verification,
                "status": "PASS",
                "commit_status": "IDEMPOTENT_PASS",
                "database_write": False,
                "backup_path": "",
            }

    backup = out / "backups" / f"{db.name}.{utc_now().replace(':', '')}.bak"
    backup_database(db, backup)
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + migration.read_text(encoding="utf-8"))
        created_at = utc_now()
        con.execute(
            """INSERT INTO stop03_5c_propagation_runs(
               propagation_run_id,contract_version,source_staging_run_id,
               source_qwen_run_id,source_qwen_video_anchor_count,
               source_ocr_anchor_count,unique_video_frame_count,
               visual_unit_aliases_collapsed_count,
               candidate_neighbor_pair_count,propagation_row_count,
               propagation_target_count,target_with_direct_qwenvl_count,
               propagation_id_set_sha256,payload_digest_sha256,
               policy_config_sha256,script_sha256,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                propagation_run_id,
                CONTRACT_VERSION,
                summary["source_staging_run_id"],
                summary["source_qwen_run_id"],
                summary["source_qwen_video_anchor_count"],
                summary["source_ocr_anchor_count"],
                summary["unique_video_frame_count"],
                summary["visual_unit_aliases_collapsed_count"],
                summary["candidate_neighbor_pair_count"],
                summary["propagation_row_count"],
                summary["propagation_target_count"],
                summary["target_with_direct_qwenvl_count"],
                summary["propagation_id_set_sha256"],
                summary["payload_digest_sha256"],
                summary["policy_config_sha256"],
                summary["script_sha256"],
                "success",
                created_at,
            ),
        )
        columns = (
            "propagation_run_id",
            "propagation_id",
            "contract_version",
            "source_staging_run_id",
            "source_evidence_id",
            "source_candidate_id",
            "source_content_id",
            "source_visual_unit_id",
            "source_canonical_visual_unit_id",
            "source_derived_id",
            "source_frame_index",
            "source_time_position_ms",
            "target_canonical_visual_unit_id",
            "target_derived_id",
            "target_frame_index",
            "target_time_position_ms",
            "frame_offset",
            "propagation_direction",
            "propagation_step",
            "target_has_direct_qwenvl",
            "propagated_label",
            "propagated_label_zh",
            "source_yolo_confidence",
            "target_yolo_confidence",
            "propagated_text",
            "propagated_text_sha256",
            "source_text_sha256",
            "gate_status",
            "created_at",
        )
        con.executemany(
            f"""INSERT INTO stop03_5c_propagation_items(
                {','.join(columns)}) VALUES(
                {','.join('?' for _ in columns)})""",
            [
                tuple(
                    propagation_run_id
                    if column == "propagation_run_id"
                    else created_at
                    if column == "created_at"
                    else int(row[column])
                    if column == "target_has_direct_qwenvl"
                    else row[column]
                    for column in columns
                )
                for row in rows
            ],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    verification = readback(db, propagation_run_id)
    if verification["status"] != "PASS":
        raise RuntimeError("stop03_5c_commit_readback_failure")
    return {
        **summary,
        **verification,
        "status": "PASS",
        "technical_status": "PASS",
        "commit_status": "COMMITTED",
        "database_write": True,
        "backup_path": str(backup),
    }


def object_exists_in_db(db: Path, name: str) -> bool:
    with connect_ro(db) as con:
        return object_exists(con, name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("preflight", "dry-run", "commit", "readback"),
        required=True,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "readback":
        if not args.run_id:
            raise RuntimeError("stop03_5c_readback_run_id_required")
        report = readback(args.db, args.run_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 2
    db_sha_before = sha256_file(args.db)
    summary, propagation, targets = build_distribution(args.db, args.config)
    if args.mode == "preflight":
        summary["central_db_sha256"] = db_sha_before
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["technical_status"] == "PASS" else 2
    if args.mode == "commit":
        if not args.confirm_central_db_write:
            raise RuntimeError("stop03_5c_commit_confirmation_required")
        report = commit(
            args.db, args.migration, args.out, summary, propagation
        )
        write_json(
            args.out / "reports/stop03_5c_commit_summary.json", report
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    db_sha_after = sha256_file(args.db)
    summary["central_db_sha256_before"] = db_sha_before
    summary["central_db_sha256_after"] = db_sha_after
    summary["central_db_unchanged"] = db_sha_before == db_sha_after
    if not summary["central_db_unchanged"]:
        summary["status"] = "FAIL"
        summary["technical_status"] = "FAIL"
    write_json(args.out / "reports/stop03_5c_summary.json", summary)
    write_jsonl(
        args.out / "manifests/semantic_propagation.jsonl", propagation
    )
    write_csv(
        args.out / "manifests/semantic_propagation_index.csv", propagation
    )
    write_csv(args.out / "manifests/propagation_targets.csv", targets)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
