#!/usr/bin/env python3
"""Build generic frame-level text documents for Stop03-5D without model use."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


CONTRACT_VERSION = "stop03_5d_text_embedding_db_contract_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "MEDIA_ARCHIVE_TEST_OUTPUT_ROOT",
        str(PROJECT_ROOT.parent / "test-output"),
    )
).expanduser()
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5d_text_embedding_db_contract_v1.json"
DEFAULT_MIGRATION = (
    PROJECT_ROOT
    / "migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql"
)
DEFAULT_OUT = DEFAULT_OUTPUT_ROOT / "stop03_5d_text_embedding_db_contract_v1"
MODEL_ASSET_FILES = (
    "config.json",
    "config_sentence_transformers.json",
    "generation_config.json",
    "merges.txt",
    "model.safetensors",
    "modules.json",
    "1_Pooling/config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)
MODEL_CONFIG_FILES = tuple(
    value for value in MODEL_ASSET_FILES if value != "model.safetensors"
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def normalize_text(value: str) -> str:
    lines = []
    for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = re.sub(r"[ \t]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_version": CONTRACT_VERSION,
        "source_evidence_selector": "latest_success_stop03_5b_view",
        "source_propagation_selector": "latest_success_stop03_5c_view",
        "included_direct_quality_statuses": ["PASS"],
        "excluded_direct_quality_statuses": ["REVIEW"],
        "included_modalities": ["qwenvl", "ocr"],
        "one_document_per_derived_id": True,
        "text_section_order": ["qwenvl", "ocr", "propagated_labels"],
        "deduplicate_direct_text_by_sha256": True,
        "deduplicate_propagated_labels": True,
        "reuse_identical_embedding_text": True,
        "vector_dtype": "float32",
        "normalize_embeddings": True,
        "document_prompt_name": "document",
        "document_prompt_text": "",
        "vector_storage": "central_sqlite_blob",
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
            "stop03_5d_policy_mismatch:"
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    if int(config.get("model_dimension", 0)) <= 0:
        raise RuntimeError("stop03_5d_model_dimension_invalid")
    model_path = Path(str(config["model_path"])).expanduser().absolute()
    python_path = Path(str(config["python_path"])).expanduser().absolute()
    if not model_path.is_dir():
        raise RuntimeError(f"stop03_5d_local_model_path_missing:{model_path}")
    if not python_path.is_file():
        raise RuntimeError(f"stop03_5d_python_path_missing:{python_path}")
    required_model_files = [model_path / name for name in MODEL_ASSET_FILES]
    missing = [str(file) for file in required_model_files if not file.is_file()]
    if missing:
        raise RuntimeError(f"stop03_5d_local_model_files_missing:{missing}")
    config["model_path_resolved"] = model_path
    config["python_path_resolved"] = python_path
    config["required_model_files"] = required_model_files
    return config


def model_inventory(config: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    root = config["model_path_resolved"]
    inventory = [
        {
            "relative_path": str(path.relative_to(root)),
            "size_bytes": path.stat().st_size,
        }
        for path in config["required_model_files"]
    ]
    config_hashes = [
        {
            "relative_path": str(path.relative_to(root)),
            "sha256": sha256_file(path),
        }
        for path in (root / name for name in MODEL_CONFIG_FILES)
    ]
    return (
        sha256_text(canonical_json(inventory)),
        sha256_text(canonical_json(config_hashes)),
        inventory,
    )


def latest_run(
    con: sqlite3.Connection, table: str, id_column: str
) -> sqlite3.Row:
    row = con.execute(
        f"""SELECT * FROM {table}
            WHERE status='success'
            ORDER BY created_at DESC,{id_column} DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError(f"stop03_5d_latest_success_run_missing:{table}")
    return row


def join_texts(values: dict[str, str]) -> str:
    return "\n\n".join(values[key] for key in sorted(values))


def compose_embedding_text(
    qwen_text: str, ocr_text: str, propagated_labels: list[dict[str, str]]
) -> str:
    sections = []
    if qwen_text:
        sections.append(f"画面描述：\n{qwen_text}")
    if ocr_text:
        sections.append(f"画面文字：\n{ocr_text}")
    if propagated_labels:
        label_text = "、".join(
            f"{row['label_zh']}（{row['label']}）" for row in propagated_labels
        )
        sections.append(f"相邻画面确认对象：{label_text}")
    return "\n\n".join(sections)


def build_documents(
    db: Path, config_path: Path
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    config = load_config(config_path)
    inventory_sha, model_config_sha, inventory = model_inventory(config)
    required = (
        "v_stop03_5_latest_unified_evidence",
        "v_stop03_5c_latest_propagation",
        "stop03_5_unified_evidence_runs",
        "stop03_5c_propagation_runs",
        "derived_assets",
        "source_assets",
        "visual_units",
    )
    with connect_ro(db) as con:
        missing = [name for name in required if not object_exists(con, name)]
        if missing:
            raise RuntimeError(f"stop03_5d_database_objects_missing:{missing}")
        staging = latest_run(
            con, "stop03_5_unified_evidence_runs", "staging_run_id"
        )
        propagation_run = latest_run(
            con, "stop03_5c_propagation_runs", "propagation_run_id"
        )
        direct_rows = [
            dict(row)
            for row in con.execute(
                """SELECT e.*,d.derived_type,d.frame_index,d.time_position_ms,
                          s.media_type,s.relative_path
                   FROM v_stop03_5_latest_unified_evidence e
                   JOIN derived_assets d ON d.derived_id=e.derived_id
                   JOIN source_assets s
                     ON s.source_content_id=e.source_content_id
                   WHERE e.modality IN ('qwenvl','ocr')
                   ORDER BY e.derived_id,e.modality,e.evidence_id"""
            )
        ]
        propagation_rows = [
            dict(row)
            for row in con.execute(
                """SELECT p.*,d.derived_type,s.media_type,s.relative_path
                   FROM v_stop03_5c_latest_propagation p
                   JOIN derived_assets d
                     ON d.derived_id=p.target_derived_id
                   JOIN source_assets s
                     ON s.source_content_id=p.source_content_id
                   ORDER BY p.target_derived_id,p.propagated_label,
                            p.propagation_id"""
            )
        ]
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(list(con.execute("PRAGMA foreign_key_check")))

    included_quality = set(config["included_direct_quality_statuses"])
    groups: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []

    def group_for(
        derived_id: str,
        source_content_id: str,
        canonical_visual_unit_id: str,
        media_type: str,
        derived_type: str,
        frame_index: int,
        time_position_ms: int,
        relative_path: str,
    ) -> dict[str, Any]:
        group = groups.setdefault(
            derived_id,
            {
                "derived_id": derived_id,
                "source_content_id": source_content_id,
                "canonical_visual_unit_id": canonical_visual_unit_id,
                "media_type": media_type,
                "derived_type": derived_type,
                "frame_index": int(frame_index),
                "time_position_ms": int(time_position_ms),
                "source_relative_path": relative_path,
                "qwen": {},
                "ocr": {},
                "evidence_ids": set(),
                "propagated_labels": {},
                "propagation_ids": set(),
                "propagation_row_count": 0,
            },
        )
        identity = (
            source_content_id,
            canonical_visual_unit_id,
            media_type,
            derived_type,
            int(frame_index),
            int(time_position_ms),
            relative_path,
        )
        current = (
            group["source_content_id"],
            group["canonical_visual_unit_id"],
            group["media_type"],
            group["derived_type"],
            group["frame_index"],
            group["time_position_ms"],
            group["source_relative_path"],
        )
        if current != identity:
            raise RuntimeError(f"stop03_5d_derived_identity_conflict:{derived_id}")
        return group

    for row in direct_rows:
        if row["quality_status"] not in included_quality:
            excluded.append(
                {
                    "evidence_id": row["evidence_id"],
                    "candidate_id": row["candidate_id"],
                    "derived_id": row["derived_id"],
                    "modality": row["modality"],
                    "quality_status": row["quality_status"],
                    "reason": "direct_quality_status_excluded",
                }
            )
            continue
        group = group_for(
            str(row["derived_id"]),
            str(row["source_content_id"]),
            str(row["canonical_visual_unit_id"]),
            str(row["media_type"]),
            str(row["derived_type"]),
            int(row["frame_index"]),
            int(row["time_position_ms"]),
            str(row["relative_path"]),
        )
        text = normalize_text(str(row["evidence_text"]))
        if not text:
            raise RuntimeError(
                f"stop03_5d_empty_pass_evidence_text:{row['evidence_id']}"
            )
        text_sha = sha256_text(text)
        modality_key = (
            "qwen" if str(row["modality"]) == "qwenvl" else str(row["modality"])
        )
        group[modality_key].setdefault(text_sha, text)
        group["evidence_ids"].add(str(row["evidence_id"]))

    for row in propagation_rows:
        group = group_for(
            str(row["target_derived_id"]),
            str(row["source_content_id"]),
            str(row["target_canonical_visual_unit_id"]),
            str(row["media_type"]),
            str(row["derived_type"]),
            int(row["target_frame_index"]),
            int(row["target_time_position_ms"]),
            str(row["relative_path"]),
        )
        label = str(row["propagated_label"])
        label_zh = str(row["propagated_label_zh"])
        existing = group["propagated_labels"].get(label)
        if existing is not None and existing != label_zh:
            raise RuntimeError(f"stop03_5d_propagated_label_conflict:{label}")
        group["propagated_labels"][label] = label_zh
        group["propagation_ids"].add(str(row["propagation_id"]))
        group["propagation_row_count"] += 1

    documents: list[dict[str, Any]] = []
    for derived_id in sorted(groups):
        group = groups[derived_id]
        qwen_text = join_texts(group["qwen"])
        ocr_text = join_texts(group["ocr"])
        propagated_labels = [
            {"label": label, "label_zh": group["propagated_labels"][label]}
            for label in sorted(group["propagated_labels"])
        ]
        has_direct = bool(qwen_text or ocr_text)
        has_propagation = bool(propagated_labels)
        if has_direct and has_propagation:
            document_kind = "direct_and_propagation"
        elif has_direct:
            document_kind = "direct_only"
        elif has_propagation:
            document_kind = "propagation_only"
        else:
            raise RuntimeError(f"stop03_5d_empty_document:{derived_id}")
        embedding_text = compose_embedding_text(
            qwen_text, ocr_text, propagated_labels
        )
        text_sha = sha256_text(embedding_text)
        document_seed = canonical_json(
            {
                "contract_version": CONTRACT_VERSION,
                "source_staging_run_id": staging["staging_run_id"],
                "source_propagation_run_id":
                    propagation_run["propagation_run_id"],
                "derived_id": derived_id,
                "embedding_text_sha256": text_sha,
            }
        )
        document_id = f"txtdoc5d_{sha256_text(document_seed)[:32]}"
        text_vector_id = f"txtvec5d_{text_sha[:32]}"
        documents.append(
            {
                "document_id": document_id,
                "text_vector_id": text_vector_id,
                "contract_version": CONTRACT_VERSION,
                "source_content_id": group["source_content_id"],
                "derived_id": derived_id,
                "canonical_visual_unit_id":
                    group["canonical_visual_unit_id"],
                "media_type": group["media_type"],
                "derived_type": group["derived_type"],
                "frame_index": group["frame_index"],
                "time_position_ms": group["time_position_ms"],
                "source_relative_path": group["source_relative_path"],
                "document_kind": document_kind,
                "qwen_text": qwen_text,
                "ocr_text": ocr_text,
                "propagated_labels_json": canonical_json(propagated_labels),
                "embedding_text": embedding_text,
                "embedding_text_sha256": text_sha,
                "source_evidence_ids_json":
                    canonical_json(sorted(group["evidence_ids"])),
                "source_propagation_ids_json":
                    canonical_json(sorted(group["propagation_ids"])),
                "direct_qwen_count": len(group["qwen"]),
                "direct_ocr_count": len(group["ocr"]),
                "propagation_row_count": group["propagation_row_count"],
                "quality_status": "PASS",
            }
        )

    text_jobs_by_sha: dict[str, dict[str, Any]] = {}
    job_documents: dict[str, list[str]] = defaultdict(list)
    for document in documents:
        text_sha = document["embedding_text_sha256"]
        job_documents[text_sha].append(document["document_id"])
        text_jobs_by_sha.setdefault(
            text_sha,
            {
                "text_vector_id": document["text_vector_id"],
                "execution_key": sha256_text(
                    canonical_json(
                        {
                            "contract_version": CONTRACT_VERSION,
                            "model_inventory_sha256": inventory_sha,
                            "model_config_sha256": model_config_sha,
                            "embedding_text_sha256": text_sha,
                            "dimension": config["model_dimension"],
                            "dtype": config["vector_dtype"],
                            "normalized": config["normalize_embeddings"],
                            "prompt_name": config["document_prompt_name"],
                            "prompt_text": config["document_prompt_text"],
                        }
                    )
                ),
                "embedding_text_sha256": text_sha,
                "embedding_text": document["embedding_text"],
                "document_count": 0,
                "document_ids_json": "[]",
            },
        )
    for text_sha, job in text_jobs_by_sha.items():
        ids = sorted(job_documents[text_sha])
        job["document_count"] = len(ids)
        job["document_ids_json"] = canonical_json(ids)
    text_jobs = sorted(
        text_jobs_by_sha.values(), key=lambda row: row["text_vector_id"]
    )

    documents.sort(key=lambda row: (row["source_content_id"], row["frame_index"], row["derived_id"]))
    document_ids = [row["document_id"] for row in documents]
    job_ids = [row["text_vector_id"] for row in text_jobs]
    kinds = Counter(row["document_kind"] for row in documents)
    media = Counter(row["media_type"] for row in documents)
    payload = [
        {key: value for key, value in row.items() if key != "text_vector_id"}
        for row in documents
    ]
    payload_digest = sha256_text(canonical_json(payload))
    run_payload_digest = sha256_text(
        canonical_json(
            {
                "contract_version": CONTRACT_VERSION,
                "document_payload_digest_sha256": payload_digest,
                "model_name": config["model_name"],
                "model_inventory_sha256": inventory_sha,
                "model_config_sha256": model_config_sha,
                "model_dimension": int(config["model_dimension"]),
                "vector_dtype": config["vector_dtype"],
                "normalize_embeddings": bool(config["normalize_embeddings"]),
                "document_prompt_name": config["document_prompt_name"],
                "document_prompt_text": config["document_prompt_text"],
            }
        )
    )
    planned_run_id = f"stop03_5d_{run_payload_digest[:24]}"
    checks = {
        "latest_staging_run_success": staging["status"] == "success",
        "latest_propagation_run_success":
            propagation_run["status"] == "success",
        "included_direct_rows_are_pass": all(
            row["quality_status"] == "PASS"
            for row in direct_rows
            if row["quality_status"] in included_quality
        ),
        "one_document_per_derived_id":
            len(documents)
            == len({row["derived_id"] for row in documents}),
        "document_ids_unique":
            len(document_ids) == len(set(document_ids)),
        "embedding_text_nonempty":
            all(bool(row["embedding_text"]) for row in documents),
        "embedding_text_hashes_match":
            all(
                sha256_text(row["embedding_text"])
                == row["embedding_text_sha256"]
                for row in documents
            ),
        "text_job_ids_unique": len(job_ids) == len(set(job_ids)),
        "documents_link_to_text_jobs":
            {row["text_vector_id"] for row in documents} == set(job_ids),
        "identical_text_reused":
            len(text_jobs)
            == len({row["embedding_text_sha256"] for row in documents}),
        "model_files_present":
            all(path.is_file() for path in config["required_model_files"]),
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
        "planned_embedding_run_id": planned_run_id,
        "source_staging_run_id": str(staging["staging_run_id"]),
        "source_propagation_run_id":
            str(propagation_run["propagation_run_id"]),
        "direct_evidence_row_count": len(direct_rows),
        "direct_pass_row_count": sum(
            row["quality_status"] in included_quality for row in direct_rows
        ),
        "direct_review_excluded_count": sum(
            row["quality_status"] == "REVIEW" for row in direct_rows
        ),
        "direct_frame_count": len(
            {
                row["derived_id"]
                for row in direct_rows
                if row["quality_status"] in included_quality
            }
        ),
        "qwen_pass_row_count": sum(
            row["modality"] == "qwenvl"
            and row["quality_status"] in included_quality
            for row in direct_rows
        ),
        "ocr_pass_row_count": sum(
            row["modality"] == "ocr"
            and row["quality_status"] in included_quality
            for row in direct_rows
        ),
        "propagation_row_count": len(propagation_rows),
        "propagation_target_count":
            len({row["target_derived_id"] for row in propagation_rows}),
        "propagated_label_count":
            len({row["propagated_label"] for row in propagation_rows}),
        "document_count": len(documents),
        "direct_only_count": kinds["direct_only"],
        "propagation_only_count": kinds["propagation_only"],
        "direct_and_propagation_count":
            kinds["direct_and_propagation"],
        "image_document_count": media["image"],
        "video_document_count": media["video"],
        "unique_text_inference_count": len(text_jobs),
        "reused_document_count": len(documents) - len(text_jobs),
        "max_documents_per_text": max(
            (row["document_count"] for row in text_jobs), default=0
        ),
        "model_name": config["model_name"],
        "model_path": str(config["model_path_resolved"]),
        "python_path": str(config["python_path_resolved"]),
        "model_dimension": int(config["model_dimension"]),
        "vector_dtype": config["vector_dtype"],
        "normalize_embeddings": bool(config["normalize_embeddings"]),
        "model_inventory_file_count": len(inventory),
        "model_inventory_sha256": inventory_sha,
        "model_config_sha256": model_config_sha,
        "document_id_set_sha256":
            sha256_text("\n".join(sorted(document_ids))),
        "text_job_id_set_sha256":
            sha256_text("\n".join(sorted(job_ids))),
        "document_payload_digest_sha256": payload_digest,
        "run_payload_digest_sha256": run_payload_digest,
        "policy_config_sha256": sha256_file(config_path),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "checks": checks,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }
    return summary, documents, text_jobs, excluded


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "dry-run"), required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_sha_before = sha256_file(args.db)
    summary, documents, text_jobs, excluded = build_documents(
        args.db, args.config
    )
    if args.mode == "preflight":
        summary["central_db_sha256"] = db_sha_before
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["technical_status"] == "PASS" else 2
    db_sha_after = sha256_file(args.db)
    summary["central_db_sha256_before"] = db_sha_before
    summary["central_db_sha256_after"] = db_sha_after
    summary["central_db_unchanged"] = db_sha_before == db_sha_after
    if not summary["central_db_unchanged"]:
        summary["status"] = "FAIL"
        summary["technical_status"] = "FAIL"
    write_json(args.out / "reports/stop03_5d_summary.json", summary)
    write_jsonl(args.out / "manifests/text_documents.jsonl", documents)
    write_csv(args.out / "manifests/text_documents.csv", documents)
    write_jsonl(args.out / "manifests/unique_text_jobs.jsonl", text_jobs)
    write_csv(args.out / "manifests/unique_text_jobs.csv", text_jobs)
    write_csv(args.out / "manifests/excluded_direct_evidence.csv", excluded)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
