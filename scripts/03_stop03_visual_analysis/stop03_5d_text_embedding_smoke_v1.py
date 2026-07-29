#!/usr/bin/env python3
"""Run a local-only Stop03-5D text embedding smoke without DB writes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import resource
import struct
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5d_text_embedding_db_contract_v1 as contract


SMOKE_VERSION = "stop03_5d_text_embedding_smoke_v1"
DEFAULT_OUT = contract.DEFAULT_OUTPUT_ROOT / "stop03_5d_text_embedding_smoke_v1"
InferenceFunction = Callable[
    [Path, list[str], int, str],
    tuple[list[list[float]], list[list[float]], dict[str, Any]],
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )


def select_smoke_jobs(
    jobs: Sequence[dict[str, Any]], sample_count: int
) -> list[dict[str, Any]]:
    """Choose a deterministic, non-count-specific sample with varied text sizes."""
    if sample_count < 3 or sample_count > 5:
        raise RuntimeError("stop03_5d_smoke_sample_count_must_be_3_to_5")
    unique = {
        str(row["text_vector_id"]): dict(row)
        for row in jobs
        if str(row.get("embedding_text") or "").strip()
    }
    ordered = sorted(unique.values(), key=lambda row: str(row["text_vector_id"]))
    if len(ordered) < sample_count:
        raise RuntimeError(
            f"stop03_5d_smoke_not_enough_unique_texts:{len(ordered)}"
        )

    by_length = sorted(
        ordered,
        key=lambda row: (
            len(str(row["embedding_text"])), str(row["text_vector_id"])
        ),
    )
    by_reuse = sorted(
        ordered,
        key=lambda row: (
            -int(row.get("document_count") or 0),
            str(row["text_vector_id"]),
        ),
    )
    candidate_rows = [by_reuse[0], by_length[0], by_length[-1]]
    if len(ordered) > 1:
        candidate_rows.extend(
            ordered[round((len(ordered) - 1) * fraction)]
            for fraction in (0.25, 0.5, 0.75)
        )
    candidate_rows.extend(ordered)

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidate_rows:
        key = str(row["text_vector_id"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) == sample_count:
            break
    return selected


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def validate_vectors(
    document_vectors: Sequence[Sequence[float]],
    query_vectors: Sequence[Sequence[float]],
    expected_dimension: int,
) -> tuple[dict[str, Any], list[list[float]]]:
    count = len(document_vectors)
    checks: dict[str, Any] = {
        "document_vector_count_matches": count > 0,
        "query_vector_count_matches": len(query_vectors) == count,
        "document_dimensions_match": all(
            len(row) == expected_dimension for row in document_vectors
        ),
        "query_dimensions_match": all(
            len(row) == expected_dimension for row in query_vectors
        ),
        "all_values_finite": all(
            math.isfinite(float(value))
            for matrix in (document_vectors, query_vectors)
            for row in matrix
            for value in row
        ),
    }
    document_norms = [math.sqrt(cosine(row, row)) for row in document_vectors]
    query_norms = [math.sqrt(cosine(row, row)) for row in query_vectors]
    checks["document_vectors_normalized"] = all(
        abs(value - 1.0) <= 0.001 for value in document_norms
    )
    checks["query_vectors_normalized"] = all(
        abs(value - 1.0) <= 0.001 for value in query_norms
    )
    matrix = [
        [cosine(query, document) for document in document_vectors]
        for query in query_vectors
    ]
    top_indices = [
        max(range(count), key=lambda index: row[index]) for row in matrix
    ] if count else []
    checks["same_text_query_top1"] = top_indices == list(range(count))
    checks["all_checks_pass"] = all(bool(value) for value in checks.values())
    checks["document_norm_min"] = min(document_norms, default=0.0)
    checks["document_norm_max"] = max(document_norms, default=0.0)
    checks["query_norm_min"] = min(query_norms, default=0.0)
    checks["query_norm_max"] = max(query_norms, default=0.0)
    checks["same_text_query_top_indices"] = top_indices
    checks["same_text_query_diagonal_cosines"] = [
        matrix[index][index] for index in range(count)
    ]
    return checks, matrix


def real_sentence_transformer_inference(
    model_path: Path,
    texts: list[str],
    expected_dimension: int,
    device: str,
) -> tuple[list[list[float]], list[list[float]], dict[str, Any]]:
    # Force every supported Hugging Face component into local-only mode before
    # importing the runtime. The model directory remains read-only.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    import torch
    from sentence_transformers import SentenceTransformer

    effective_device = device
    if device == "auto":
        effective_device = "mps" if torch.backends.mps.is_available() else "cpu"
    load_started = time.monotonic()
    model = SentenceTransformer(
        str(model_path),
        device=effective_device,
        local_files_only=True,
        trust_remote_code=False,
    )
    load_seconds = time.monotonic() - load_started
    infer_started = time.monotonic()
    document_array = model.encode(
        texts,
        batch_size=len(texts),
        show_progress_bar=False,
        precision="float32",
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    query_array = model.encode(
        texts,
        prompt_name="query",
        batch_size=len(texts),
        show_progress_bar=False,
        precision="float32",
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    inference_seconds = time.monotonic() - infer_started
    documents = document_array.astype("float32", copy=False).tolist()
    queries = query_array.astype("float32", copy=False).tolist()
    if documents and len(documents[0]) != expected_dimension:
        raise RuntimeError(
            "stop03_5d_smoke_runtime_dimension_mismatch:"
            f"{len(documents[0])}!={expected_dimension}"
        )
    return documents, queries, {
        "device": effective_device,
        "model_load_seconds": load_seconds,
        "inference_seconds": inference_seconds,
    }


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if platform.system() == "Darwin" else value * 1024


def execute_smoke(
    *,
    db: Path,
    config_path: Path,
    out: Path,
    sample_count: int,
    device: str,
    inference: InferenceFunction,
) -> dict[str, Any]:
    db_before = contract.sha256_file(db)
    source_summary, _documents, jobs, _excluded = contract.build_documents(
        db, config_path
    )
    selected = select_smoke_jobs(jobs, sample_count)
    config = contract.load_config(config_path)
    texts = [str(row["embedding_text"]) for row in selected]
    started = time.monotonic()
    document_vectors, query_vectors, runtime = inference(
        config["model_path_resolved"],
        texts,
        int(config["model_dimension"]),
        device,
    )
    elapsed = time.monotonic() - started
    checks, matrix = validate_vectors(
        document_vectors, query_vectors, int(config["model_dimension"])
    )
    db_after = contract.sha256_file(db)
    checks["central_db_unchanged"] = db_before == db_after
    checks["all_checks_pass"] = bool(checks["all_checks_pass"]) and bool(
        checks["central_db_unchanged"]
    )

    vector_rows: list[dict[str, Any]] = []
    vector_payload = bytearray()
    for index, (job, vector) in enumerate(zip(selected, document_vectors)):
        payload = struct.pack(f"<{len(vector)}f", *vector)
        byte_offset = len(vector_payload)
        vector_payload.extend(payload)
        vector_rows.append(
            {
                "sample_index": index,
                "text_vector_id": job["text_vector_id"],
                "embedding_text_sha256": job["embedding_text_sha256"],
                "document_count": job["document_count"],
                "dimension": len(vector),
                "dtype": "float32",
                "normalized": True,
                "byte_offset": byte_offset,
                "byte_length": len(payload),
                "vector_sha256": sha256_bytes(payload),
            }
        )

    summary = {
        "status": "PASS" if checks["all_checks_pass"] else "FAIL",
        "technical_status": "PASS" if checks["all_checks_pass"] else "FAIL",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "smoke_version": SMOKE_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "planned_embedding_run_id": source_summary["planned_embedding_run_id"],
        "source_staging_run_id": source_summary["source_staging_run_id"],
        "source_propagation_run_id": source_summary["source_propagation_run_id"],
        "source_document_count": source_summary["document_count"],
        "source_unique_text_count": source_summary["unique_text_inference_count"],
        "smoke_input_count": len(selected),
        "model_name": config["model_name"],
        "model_path": str(config["model_path_resolved"]),
        "model_dimension": int(config["model_dimension"]),
        "vector_dtype": config["vector_dtype"],
        "normalize_embeddings": bool(config["normalize_embeddings"]),
        "query_prompt_name": "query",
        "runtime": runtime,
        "total_elapsed_seconds": elapsed,
        "peak_rss_bytes": peak_rss_bytes(),
        "checks": checks,
        "central_db_sha256_before": db_before,
        "central_db_sha256_after": db_after,
        "central_db_write": False,
        "model_run": True,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
        "search_index_created": False,
    }

    reports = out / "reports"
    manifests = out / "manifests"
    vectors = out / "vectors"
    write_json(reports / "stop03_5d_smoke_summary.json", summary)
    write_jsonl(manifests / "selected_text_jobs.jsonl", selected)
    write_jsonl(manifests / "vector_manifest.jsonl", vector_rows)
    vectors.mkdir(parents=True, exist_ok=True)
    (vectors / "document_vectors_float32.bin").write_bytes(vector_payload)
    with (reports / "same_text_query_similarity.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["query_index", *range(len(selected))])
        for index, row in enumerate(matrix):
            writer.writerow([index, *[f"{value:.9f}" for value in row]])
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "real-smoke"), required=True)
    parser.add_argument("--db", type=Path, default=contract.DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=contract.DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--sample-count", type=int, default=5)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--confirm-real-local-model-smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_summary, _documents, jobs, _excluded = contract.build_documents(
        args.db, args.config
    )
    selected = select_smoke_jobs(jobs, args.sample_count)
    preflight = {
        "status": "PASS",
        "technical_status": "PASS",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "smoke_version": SMOKE_VERSION,
        "planned_embedding_run_id": source_summary["planned_embedding_run_id"],
        "source_document_count": source_summary["document_count"],
        "source_unique_text_count": source_summary["unique_text_inference_count"],
        "smoke_input_count": len(selected),
        "selected_text_vector_ids": [row["text_vector_id"] for row in selected],
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }
    if args.mode == "preflight":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_real_local_model_smoke:
        raise RuntimeError("stop03_5d_real_smoke_confirmation_required")
    summary = execute_smoke(
        db=args.db,
        config_path=args.config,
        out=args.out,
        sample_count=args.sample_count,
        device=args.device,
        inference=real_sentence_transformer_inference,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
