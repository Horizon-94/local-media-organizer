#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.media_archive_image_video_ui.audio_search_index import (  # noqa: E402
    database_checks,
    initialize_database,
    search_database,
    upsert_evidence_and_vector,
)


FIXTURE_DOCUMENTS = (
    ("fixture_seaside", "海边有蓝色海水和岩石。"),
    ("fixture_city", "城市夜景中有汽车和道路。"),
    ("fixture_meeting", "室内会议桌旁有人讨论工作。"),
)


def stable_id(prefix: str, text: str) -> str:
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def main() -> int:
    parser = argparse.ArgumentParser()
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--audio-pilot-json", type=Path)
    source_group.add_argument("--audio-folder-pilot-json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embedding-model", type=Path, required=True)
    args = parser.parse_args()

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    pilot_paths: list[Path]
    if args.audio_pilot_json:
        pilot_paths = [args.audio_pilot_json.resolve(strict=True)]
    else:
        folder_path = args.audio_folder_pilot_json.resolve(strict=True)
        folder = json.loads(folder_path.read_text(encoding="utf-8"))
        if folder.get("status") != "PASS" or not folder.get("source_read_only"):
            raise RuntimeError("audio_v2_folder_pilot_not_accepted")
        pilot_paths = [
            Path(str(row["pilot_report"])).resolve(strict=True)
            for row in folder.get("items") or []
            if row.get("status") == "PASS_SPEECH" and row.get("pilot_report")
        ]
    pilots = [json.loads(path.read_text(encoding="utf-8")) for path in pilot_paths]
    if not pilots or any(
        pilot.get("status") != "PASS" or not pilot.get("source_read_only")
        for pilot in pilots
    ):
        raise RuntimeError("audio_v2_pilot_not_accepted")
    evidence_with_source: list[tuple[dict[str, Any], Path]] = []
    source_stats: dict[Path, os.stat_result] = {}
    for pilot in pilots:
        source = Path(str(pilot["source_video"])).resolve(strict=True)
        source_stats[source] = source.stat()
        evidence_with_source.extend(
            (dict(row), source) for row in pilot.get("search_evidence") or []
        )
    if not evidence_with_source:
        raise RuntimeError("audio_v2_search_evidence_missing")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    db = output / "audio_search_pilot.sqlite"
    report_path = output / "audio_embedding_search_pilot.json"
    model_path = args.embedding_model.resolve(strict=True)
    started = time.monotonic()

    speech_documents = [str(row["text"]) for row, _source in evidence_with_source]
    documents = speech_documents + [text for _key, text in FIXTURE_DOCUMENTS]

    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    load_started = time.monotonic()
    model = SentenceTransformer(
        str(model_path), device=device, local_files_only=True, trust_remote_code=False
    )
    load_seconds = time.monotonic() - load_started
    vectors = model.encode(
        documents,
        batch_size=len(documents),
        show_progress_bar=False,
        precision="float32",
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32", copy=False)
    combined_transcript = " ".join(
        str(pilot.get("transcript_text") or "") for pilot in pilots
    )
    if "麦子" in combined_transcript:
        speech_queries = ["麦子", "成熟的麦子", "开始收割"]
    else:
        speech_queries = [
            value for value in ("收割机", "收割", "感觉", "不对称")
            if value in combined_transcript
        ][:3]
        seen_queries: set[str] = set()
        for row, _source in evidence_with_source:
            if len(speech_queries) == 3:
                break
            query = str(row["text"]).strip()
            if len(query) < 4 or query in seen_queries:
                continue
            seen_queries.add(query)
            speech_queries.append(query)
    if not speech_queries:
        raise RuntimeError("audio_v3_no_searchable_speech_queries")
    queries = speech_queries + ["海边岩石"]
    query_vectors = model.encode(
        queries,
        prompt_name="query",
        batch_size=len(queries),
        show_progress_bar=False,
        precision="float32",
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32", copy=False)

    initialize_database(db)
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys=ON")
        for index, (row, source) in enumerate(evidence_with_source):
            evidence_id = stable_id(
                "speech_",
                f"{row['source_content_id']}:{row['start_time_ms']}:{row['end_time_ms']}:{row['text']}",
            )
            upsert_evidence_and_vector(
                con,
                evidence_id=evidence_id,
                source_content_id=str(row["source_content_id"]),
                source_path=str(source),
                start_time_ms=int(row["start_time_ms"]),
                end_time_ms=int(row["end_time_ms"]),
                hit_time_ms=int(row["hit_time_ms"]),
                transcript_text=str(row["text"]),
                language=str(row.get("language") or "") or None,
                preview_windows=dict(row["preview_windows"]),
                model_name=model_path.name,
                vector=vectors[index].tolist(),
            )
        fixture_offset = len(evidence_with_source)
        for fixture_index, (key, text) in enumerate(FIXTURE_DOCUMENTS):
            upsert_evidence_and_vector(
                con,
                evidence_id=key,
                source_content_id=key,
                source_path=None,
                start_time_ms=0,
                end_time_ms=1,
                hit_time_ms=0,
                transcript_text=text,
                language="zh",
                preview_windows={},
                model_name=model_path.name,
                vector=vectors[fixture_offset + fixture_index].tolist(),
                is_fixture=True,
            )
        con.commit()

    query_results: list[dict[str, Any]] = []
    for query, vector in zip(queries, query_vectors):
        results = search_database(
            db, query=query, query_vector=vector.tolist(), limit=len(documents)
        )
        query_results.append({
            "query": query,
            "top_result": results[0],
            "result_count": len(results),
        })

    checks = database_checks(db)
    expected_speech_queries = query_results[: len(speech_queries)]
    expected_document_count = len(documents)
    technical_checks = {
        "source_unchanged": all(
            (before.st_size, before.st_mtime_ns)
            == (source.stat().st_size, source.stat().st_mtime_ns)
            for source, before in source_stats.items()
        ),
        "database_integrity_ok": checks["integrity_check"] == "ok",
        "foreign_keys_ok": checks["foreign_key_error_count"] == 0,
        "evidence_vector_counts_match": (
            checks["evidence_count"] == checks["vector_count"] == expected_document_count
        ),
        "embedding_dimension_1024": (
            vectors.shape == (expected_document_count, 1024)
            and query_vectors.shape == (len(queries), 1024)
        ),
        "speech_queries_top1": all(
            not row["top_result"]["is_fixture"]
            for row in expected_speech_queries
        ),
        "distractor_query_top1": query_results[-1]["top_result"]["evidence_id"] == "fixture_seaside",
        "speech_result_traceable": all(
            Path(str(row["top_result"]["source_path"])).resolve() in source_stats
            and int(row["top_result"]["end_time_ms"]) > int(row["top_result"]["start_time_ms"])
            and set(row["top_result"]["preview_windows"]) == {"5000", "10000"}
            for row in expected_speech_queries
        ),
    }
    status = "PASS" if all(technical_checks.values()) else "FAIL"
    report = {
        "contract": "media_archive_audio_embedding_search_pilot_v1",
        "status": status,
        "technical_checks": technical_checks,
        "database": str(db),
        "database_checks": checks,
        "model": str(model_path),
        "device": device,
        "dimension": int(vectors.shape[1]),
        "document_count": len(documents),
        "query_results": query_results,
        "source_read_only": technical_checks["source_unchanged"],
        "production_database_write": False,
        "test_database_write": True,
        "network_used": False,
        "non_speech_policy": "ignored_by_product_scope",
        "model_load_seconds": round(load_seconds, 3),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": status,
        "database": str(db),
        "report": str(report_path),
        "checks": technical_checks,
    }, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
