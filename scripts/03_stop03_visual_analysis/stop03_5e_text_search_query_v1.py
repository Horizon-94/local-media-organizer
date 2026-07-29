#!/usr/bin/env python3
"""Production-shaped, local-only, read-only Stop03-5E text query entry."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5e_text_search_contract_v1 as contract
import stop03_5e_text_search_smoke_v1 as search


QUERY_ENTRY_VERSION = "stop03_5e_text_search_query_v1"
DEFAULT_OUT = contract.DEFAULT_OUTPUT_ROOT / QUERY_ENTRY_VERSION
QueryEmbedder = Callable[
    [Path, list[str], str, str],
    tuple[list[list[float]], dict[str, Any]],
]


def filters_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "media_type": args.media_type,
        "document_kind": args.document_kind,
        "source_content_id": args.source_content_id,
        "source_relative_path_prefix": args.source_relative_path_prefix,
        "time_position_ms_min": args.time_position_ms_min,
        "time_position_ms_max": args.time_position_ms_max,
    }


def validate_options(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    if args.group_limit is None:
        args.group_limit = int(config["default_vector_group_limit"])
    if args.documents_per_group is None:
        args.documents_per_group = int(config["default_documents_per_group"])
    if not 1 <= args.group_limit <= int(config["max_vector_group_limit"]):
        raise RuntimeError("stop03_5e_query_group_limit_invalid")
    if not 1 <= args.documents_per_group <= int(
        config["max_documents_per_group"]
    ):
        raise RuntimeError("stop03_5e_query_document_limit_invalid")
    if args.group_offset < 0 or args.document_offset < 0:
        raise RuntimeError("stop03_5e_query_pagination_offset_invalid")
    if args.preview_window_ms not in config["video_preview_window_options_ms"]:
        raise RuntimeError("stop03_5e_query_preview_window_invalid")
    if (
        args.time_position_ms_min is not None
        and args.time_position_ms_max is not None
        and args.time_position_ms_min > args.time_position_ms_max
    ):
        raise RuntimeError("stop03_5e_query_time_filter_invalid")


def request_identity(
    *,
    query_sha256: str,
    run_id: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "query_entry_version": QUERY_ENTRY_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "embedding_run_id": run_id,
        "query_sha256": query_sha256,
        "filters": filters_payload(args),
        "pagination": {
            "group_offset": args.group_offset,
            "group_limit": args.group_limit,
            "document_offset": args.document_offset,
            "documents_per_group": args.documents_per_group,
        },
        "preview_window_ms": args.preview_window_ms,
        "timecode_precision": args.timecode_precision,
    }
    digest = search.sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return f"query5e_{digest[:24]}", payload


def build_query_preflight(
    db: Path,
    config_path: Path,
    query: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    config = contract.load_config(config_path)
    validate_options(args, config)
    normalized = search.validate_queries([query], config)[0]
    base = contract.build_preflight(db, config_path)
    run_id = str(base["selected_embedding_run_id"])
    request_id, request_payload = request_identity(
        query_sha256=search.sha256_text(normalized), run_id=run_id, args=args
    )
    filter_sql, filter_values = search.build_document_filter_sql(args)
    documents = search.load_search_documents(
        db, run_id, filter_sql, filter_values
    )
    if not documents:
        raise RuntimeError("stop03_5e_query_no_documents_after_filters")
    summary = {
        "status": "PASS",
        "technical_status": "PASS",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "query_entry_version": QUERY_ENTRY_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "request_id": request_id,
        "request": request_payload,
        "selected_embedding_run_id": run_id,
        "selected_model_name": base["selected_model_name"],
        "query_character_count": len(normalized),
        "query_text_persisted": False,
        "query_vector_persisted": False,
        "eligible_document_count": sum(len(rows) for rows in documents.values()),
        "eligible_unique_vector_count": len(documents),
        "database_write": False,
        "query_model_run": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }
    return summary, normalized


def write_query_response(
    db: Path,
    out: Path,
    response: dict[str, Any],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    request_out = out / str(response["request_id"])
    response["visual_preview"] = search.materialize_preview_assets(
        db, request_out, response, config
    )
    html_text = search.render_html(response)
    html_checks = search.validate_html_asset_refs(
        html_text, request_out / "reports"
    )
    response["visual_preview"].update(html_checks)
    visual_pass = (
        response["visual_preview"]["preview_asset_missing_count"] == 0
        and html_checks["html_img_total_count"]
        == response["visual_preview"]["displayed_document_occurrence_count"]
        and html_checks["html_img_http_accessible_check_status"]
        == "PASS_STATIC_RELATIVE_ASSETS"
    )
    response["technical_checks"][
        "all_displayed_documents_have_preview_assets"
    ] = visual_pass
    response["technical_status"] = (
        "PASS" if all(response["technical_checks"].values()) else "FAIL"
    )
    response["status"] = response["technical_status"]
    report_dir = request_out / "reports"
    search.write_json(report_dir / "query_response.json", response)
    (report_dir / "query_response.html").write_text(
        search.render_html(response), encoding="utf-8"
    )
    return response, request_out


def execute_query(
    *,
    db: Path,
    config_path: Path,
    out: Path,
    query: str,
    args: argparse.Namespace,
    embedder: QueryEmbedder,
) -> tuple[dict[str, Any], Path]:
    preflight, normalized = build_query_preflight(
        db, config_path, query, args
    )
    config = contract.load_config(config_path)
    run_id = str(preflight["selected_embedding_run_id"])
    db_before = contract.sha256_file(db)
    filter_sql, filter_values = search.build_document_filter_sql(args)
    documents = search.load_search_documents(
        db, run_id, filter_sql, filter_values
    )
    with contract.connect_ro(db) as con:
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs "
            "WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()
    started = time.monotonic()
    query_vectors, runtime = embedder(
        Path(str(run["model_path"])),
        [normalized],
        str(config["query_prompt_name"]),
        args.device,
    )
    search.validate_query_vectors(
        query_vectors, 1, int(run["model_dimension"])
    )
    scan_started = time.monotonic()
    results, scan_stats = search.scan_cosine_groups(
        [normalized],
        query_vectors,
        search.iter_vector_chunks(
            db,
            run_id,
            filter_sql,
            filter_values,
            int(config["vector_scan_chunk_size"]),
        ),
        documents,
        top_groups=args.group_limit,
        documents_per_group=args.documents_per_group,
        group_offset=args.group_offset,
        document_offset=args.document_offset,
    )
    scan_seconds = time.monotonic() - scan_started
    db_after = contract.sha256_file(db)
    result = results[0]
    technical_checks = {
        "one_query_processed": result["query_index"] == 1,
        "result_page_nonempty": result["result_group_count"] > 0,
        "all_scores_finite": all(
            math.isfinite(float(group["semantic_score"]))
            for group in result["result_groups"]
        ),
        "all_results_traceable": all(
            group["text_vector_id"]
            and all(
                document["document_id"] and document["source_content_id"]
                for document in group["documents"]
            )
            for group in result["result_groups"]
        ),
        "all_eligible_vectors_scanned": (
            scan_stats["scanned_vector_count"] == len(documents)
        ),
        "central_db_unchanged": db_before == db_after,
    }
    response = {
        "status": "PASS" if all(technical_checks.values()) else "FAIL",
        "technical_status": "PASS" if all(technical_checks.values()) else "FAIL",
        "policy_status": "PASS",
        "commit_status": "DO_NOT_COMMIT",
        "query_entry_version": QUERY_ENTRY_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "request_id": preflight["request_id"],
        "request": preflight["request"],
        "selected_embedding_run_id": run_id,
        "selected_model_name": run["model_name"],
        "timecode_precision": args.timecode_precision,
        "video_preview_window_ms": args.preview_window_ms,
        "query_count": 1,
        "query_text_persisted": False,
        "query_vector_persisted": False,
        "eligible_document_count": sum(len(rows) for rows in documents.values()),
        "eligible_unique_vector_count": len(documents),
        "scanned_unique_vector_count": scan_stats["scanned_vector_count"],
        "vector_scan_chunk_count": scan_stats["scanned_chunk_count"],
        "filters": filters_payload(args),
        "pagination": preflight["request"]["pagination"],
        "runtime": {
            **runtime,
            "cosine_scan_seconds": scan_seconds,
            "total_query_seconds": time.monotonic() - started,
            "peak_rss_bytes": search.peak_rss_bytes(),
        },
        "technical_checks": technical_checks,
        "queries": [result],
        "html_title": "Stop03-5E 文本搜索结果",
        "semantic_interpretation_policy": "AMBIGUITY_EXPOSED",
        "central_db_sha256_before": db_before,
        "central_db_sha256_after": db_after,
        "database_write": False,
        "query_model_run": True,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }
    return write_query_response(db, out, response, config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("preflight", "dry-run", "query"), required=True
    )
    parser.add_argument("--db", type=Path, default=contract.DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=contract.DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--query", required=True)
    parser.add_argument("--group-offset", type=int, default=0)
    parser.add_argument("--group-limit", type=int)
    parser.add_argument("--document-offset", type=int, default=0)
    parser.add_argument("--documents-per-group", type=int)
    parser.add_argument(
        "--preview-window-ms", type=int, choices=(5000, 10000), default=5000
    )
    parser.add_argument(
        "--timecode-precision", choices=("second", "millisecond"),
        default="millisecond",
    )
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--media-type", choices=("image", "video"))
    parser.add_argument(
        "--document-kind",
        choices=("direct_only", "propagation_only", "direct_and_propagation"),
    )
    parser.add_argument("--source-content-id")
    parser.add_argument("--source-relative-path-prefix")
    parser.add_argument("--time-position-ms-min", type=int)
    parser.add_argument("--time-position-ms-max", type=int)
    parser.add_argument("--confirm-real-local-query", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    preflight, _normalized = build_query_preflight(
        args.db, args.config, args.query, args
    )
    if args.mode == "preflight":
        print(json.dumps(preflight, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.mode == "dry-run":
        request_out = args.out / "dry-run" / str(preflight["request_id"])
        plan = {
            **preflight,
            "status": "DRY_RUN_PASS",
            "planned_output_root": str(args.out),
            "query_model_run": False,
        }
        search.write_json(request_out / "query_plan.json", plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
        return 0
    if not args.confirm_real_local_query:
        raise RuntimeError("stop03_5e_real_query_confirmation_required")
    response, request_out = execute_query(
        db=args.db,
        config_path=args.config,
        out=args.out,
        query=args.query,
        args=args,
        embedder=search.real_query_embedder,
    )
    public = {key: value for key, value in response.items() if key != "queries"}
    public["result_group_count"] = response["queries"][0]["result_group_count"]
    public["total_result_group_count"] = response["queries"][0][
        "total_result_group_count"
    ]
    public["response_json"] = str(request_out / "reports/query_response.json")
    public["response_html"] = str(request_out / "reports/query_response.html")
    print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
    return 0 if response["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
