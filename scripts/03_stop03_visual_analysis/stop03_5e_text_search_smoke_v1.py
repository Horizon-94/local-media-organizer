#!/usr/bin/env python3
"""Local-only read-only Stop03-5E semantic text-search smoke."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import resource
import shutil
import socket
import sqlite3
import struct
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stop03_5e_text_search_contract_v1 as search_contract


SMOKE_VERSION = "stop03_5e_text_search_smoke_v1"
DEFAULT_OUT = search_contract.DEFAULT_OUTPUT_ROOT / SMOKE_VERSION
QueryEmbedder = Callable[
    [Path, list[str], str, str],
    tuple[list[list[float]], dict[str, Any]],
]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip()


def validate_queries(
    values: Sequence[str], config: Mapping[str, Any]
) -> list[str]:
    normalized = [normalize_query(value) for value in values]
    minimum = int(config["query_min_characters"])
    maximum = int(config["query_max_characters"])
    for index, value in enumerate(normalized, 1):
        if len(value) < minimum or len(value) > maximum:
            raise RuntimeError(
                f"stop03_5e_query_length_invalid:{index}:{len(value)}"
            )
    if len({sha256_text(value) for value in normalized}) != len(normalized):
        raise RuntimeError("stop03_5e_duplicate_normalized_queries")
    return normalized


def build_document_filter_sql(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if args.media_type:
        clauses.append("d.media_type=?")
        values.append(args.media_type)
    if args.document_kind:
        clauses.append("d.document_kind=?")
        values.append(args.document_kind)
    if args.source_content_id:
        clauses.append("d.source_content_id=?")
        values.append(args.source_content_id)
    if args.source_relative_path_prefix:
        clauses.append("d.source_relative_path LIKE ? ESCAPE '\\'")
        escaped = (
            args.source_relative_path_prefix
            .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        values.append(escaped + "%")
    if args.time_position_ms_min is not None:
        clauses.append("d.time_position_ms>=?")
        values.append(args.time_position_ms_min)
    if args.time_position_ms_max is not None:
        clauses.append("d.time_position_ms<=?")
        values.append(args.time_position_ms_max)
    return (" AND " + " AND ".join(clauses) if clauses else "", values)


def load_search_documents(
    db: Path,
    run_id: str,
    filter_sql: str,
    filter_values: Sequence[Any],
) -> dict[str, list[dict[str, Any]]]:
    with search_contract.connect_ro(db) as con:
        documents = [dict(row) for row in con.execute(
            """SELECT l.text_vector_id,d.document_id,d.source_content_id,
               d.derived_id,d.canonical_visual_unit_id,d.media_type,d.derived_type,
               d.frame_index,d.time_position_ms,d.source_relative_path,d.document_kind,
               d.qwen_text,d.ocr_text,d.propagated_labels_json,d.embedding_text,
               d.embedding_text_sha256
               FROM stop03_5d_document_vector_links l
               JOIN stop03_5d_text_documents d
                 ON d.embedding_run_id=l.embedding_run_id
                AND d.document_id=l.document_id
               WHERE d.embedding_run_id=?""" + filter_sql +
            " ORDER BY l.text_vector_id,d.source_content_id,d.time_position_ms,d.document_id",
            (run_id, *filter_values),
        )]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in documents:
            grouped[str(row["text_vector_id"])].append(row)
    return dict(grouped)


def iter_vector_chunks(
    db: Path,
    run_id: str,
    filter_sql: str,
    filter_values: Sequence[Any],
    chunk_size: int,
) -> Iterator[list[dict[str, Any]]]:
    """Yield eligible vector rows without materializing every BLOB in memory."""
    if chunk_size < 1:
        raise RuntimeError("stop03_5e_vector_chunk_size_invalid")
    with search_contract.connect_ro(db) as con:
        cursor = con.execute(
            """SELECT v.text_vector_id,v.vector_blob,v.model_dimension,
               v.vector_dtype,v.normalized,v.vector_sha256
               FROM stop03_5d_text_vectors v
               WHERE v.embedding_run_id=? AND v.status='success'
                 AND EXISTS (
                   SELECT 1
                   FROM stop03_5d_document_vector_links l
                   JOIN stop03_5d_text_documents d
                     ON d.embedding_run_id=l.embedding_run_id
                    AND d.document_id=l.document_id
                   WHERE l.embedding_run_id=?
                     AND l.text_vector_id=v.text_vector_id"""
            + filter_sql
            + ") ORDER BY v.text_vector_id",
            (run_id, run_id, *filter_values),
        )
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            yield [dict(row) for row in rows]


def validate_query_vectors(
    vectors: Sequence[Sequence[float]], expected_count: int, dimension: int
) -> None:
    if len(vectors) != expected_count:
        raise RuntimeError("stop03_5e_query_vector_count_mismatch")
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise RuntimeError(
                f"stop03_5e_query_dimension_mismatch:{index}:{len(vector)}"
            )
        if not all(math.isfinite(float(value)) for value in vector):
            raise RuntimeError(f"stop03_5e_query_non_finite:{index}")
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if abs(norm - 1.0) > 0.001:
            raise RuntimeError(f"stop03_5e_query_not_normalized:{index}:{norm}")


def scan_cosine_groups(
    queries: Sequence[str],
    query_vectors: Sequence[Sequence[float]],
    vector_chunks: Iterable[Sequence[Mapping[str, Any]]],
    documents_by_vector: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    top_groups: int,
    documents_per_group: int,
    group_offset: int = 0,
    document_offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if group_offset < 0 or document_offset < 0:
        raise RuntimeError("stop03_5e_pagination_offset_invalid")
    results: list[list[dict[str, Any]]] = [[] for _ in queries]
    scanned_vector_count = 0
    scanned_chunk_count = 0
    for vector_rows in vector_chunks:
        scanned_chunk_count += 1
        for row in vector_rows:
            scanned_vector_count += 1
            vector_id = str(row["text_vector_id"])
            documents = list(documents_by_vector.get(vector_id, ()))
            if not documents:
                raise RuntimeError(
                    f"stop03_5e_vector_without_eligible_document:{vector_id}"
                )
            representative = str(documents[0]["embedding_text"])
            dimension = int(row["model_dimension"])
            blob = row["vector_blob"] or b""
            if len(blob) != dimension * 4:
                raise RuntimeError(f"stop03_5e_vector_blob_size_invalid:{vector_id}")
            vector = struct.unpack("<" + str(dimension) + "f", blob)
            folded_text = normalize_query(representative).casefold()
            for query_index, (query, query_vector) in enumerate(
                zip(queries, query_vectors)
            ):
                score = sum(
                    float(left) * float(right)
                    for left, right in zip(query_vector, vector)
                )
                if not math.isfinite(score) or score < -1.001 or score > 1.001:
                    raise RuntimeError(
                        f"stop03_5e_cosine_out_of_range:{vector_id}:{score}"
                    )
                exact = normalize_query(query).casefold() in folded_text
                results[query_index].append({
                    "text_vector_id": vector_id,
                    "semantic_score": float(score),
                    "exact_text_match": bool(exact),
                    "matching_document_count": len(documents),
                    "representative_text_preview": representative[:500],
                    "documents": [
                        {
                            "document_id": document["document_id"],
                            "source_content_id": document["source_content_id"],
                            "derived_id": document["derived_id"],
                            "canonical_visual_unit_id": document["canonical_visual_unit_id"],
                            "media_type": document["media_type"],
                            "derived_type": document["derived_type"],
                            "frame_index": document["frame_index"],
                            "time_position_ms": document["time_position_ms"],
                            "source_relative_path": document["source_relative_path"],
                            "document_kind": document["document_kind"],
                        }
                        for document in documents[
                            document_offset:document_offset + documents_per_group
                        ]
                    ],
                    "document_offset": document_offset,
                    "returned_document_count": min(
                        max(0, len(documents) - document_offset),
                        documents_per_group,
                    ),
                    "next_document_offset": (
                        document_offset + documents_per_group
                        if len(documents) > document_offset + documents_per_group
                        else None
                    ),
                    "documents_truncated": (
                        document_offset > 0
                        or len(documents) > document_offset + documents_per_group
                    ),
                })
    output: list[dict[str, Any]] = []
    for index, rows in enumerate(results):
        ordered_all = sorted(
            rows,
            key=lambda row: (
                -int(row["exact_text_match"]),
                -float(row["semantic_score"]),
                str(row["text_vector_id"]),
            ),
        )
        ordered = ordered_all[group_offset:group_offset + top_groups]
        output.append({
            "query_index": index + 1,
            "query_sha256": sha256_text(queries[index]),
            "query_character_count": len(queries[index]),
            "result_group_count": len(ordered),
            "total_result_group_count": len(ordered_all),
            "group_offset": group_offset,
            "next_group_offset": (
                group_offset + top_groups
                if len(ordered_all) > group_offset + top_groups else None
            ),
            "result_groups": ordered,
        })
    return output, {
        "scanned_vector_count": scanned_vector_count,
        "scanned_chunk_count": scanned_chunk_count,
    }


def block_network() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    def blocked_connect(self: socket.socket, address: Any) -> Any:
        raise RuntimeError(f"stop03_5e_network_blocked:{address}")

    socket.socket.connect = blocked_connect  # type: ignore[assignment]


def real_query_embedder(
    model_path: Path,
    queries: list[str],
    prompt_name: str,
    device: str,
) -> tuple[list[list[float]], dict[str, Any]]:
    block_network()
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
    embed_started = time.monotonic()
    array = model.encode(
        queries,
        prompt_name=prompt_name,
        batch_size=len(queries),
        show_progress_bar=False,
        precision="float32",
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    embed_seconds = time.monotonic() - embed_started
    return array.astype("float32", copy=False).tolist(), {
        "device": effective_device,
        "model_load_seconds": load_seconds,
        "query_embedding_seconds": embed_seconds,
    }


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def format_timecode(milliseconds: int, precision: str) -> str:
    if milliseconds < 0:
        return "--:--"
    if precision not in {"second", "millisecond"}:
        raise RuntimeError("stop03_5e_timecode_precision_invalid")
    total_seconds, remainder_ms = divmod(int(milliseconds), 1000)
    hours, within_hour = divmod(total_seconds, 3600)
    minutes, seconds = divmod(within_hour, 60)
    base = (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours else f"{minutes:02d}:{seconds:02d}"
    )
    return f"{base}.{remainder_ms:03d}" if precision == "millisecond" else base


def classify_environment_texts(texts: Sequence[str]) -> dict[str, Any]:
    """Return a conservative display label without rewriting source evidence."""
    indoor_terms = (
        "室内", "房间", "客厅", "卧室", "办公室", "会议室",
        "店内", "车内", "棚内", "indoor",
    )
    night_terms = (
        "夜间", "夜晚", "夜景", "夜空", "深夜", "黑夜", "night",
    )
    outdoor_terms = (
        "户外", "室外", "街道", "道路", "广场", "海边", "湖边",
        "山野", "建筑外墙", "城市建筑", "outdoor",
    )
    day_terms = (
        "白天", "日间", "晴天", "阳光", "日光", "蓝天",
        "daytime", "daylight",
    )
    folded = [normalize_query(value).casefold() for value in texts if value]

    def evidence_count(terms: Sequence[str]) -> int:
        return sum(any(term in value for term in terms) for value in folded)

    counts = {
        "indoor": evidence_count(indoor_terms),
        "night": evidence_count(night_terms),
        "outdoor": evidence_count(outdoor_terms),
        "day": evidence_count(day_terms),
    }
    indoor = counts["indoor"] > 0
    night = counts["night"] > 0
    outdoor = counts["outdoor"] > 0
    day = counts["day"] > 0
    if indoor and night:
        code, label, confirmation = (
            "night_or_indoor", "夜间/室内（待确认）", True
        )
    elif indoor and outdoor:
        code, label, confirmation = (
            "indoor_or_outdoor", "室内/户外（待确认）", True
        )
    elif night and outdoor:
        code, label, confirmation = "outdoor_night", "夜间户外", False
    elif indoor:
        code, label, confirmation = "indoor", "室内", False
    elif outdoor and day:
        code, label, confirmation = "outdoor_day", "白天户外", False
    elif outdoor:
        code, label, confirmation = "outdoor", "户外（时段未确定）", False
    elif night:
        code, label, confirmation = (
            "night_or_indoor", "夜间/室内（待确认）", True
        )
    else:
        code, label, confirmation = "unknown", "未确定", True
    return {
        "environment_code": code,
        "environment_label": label,
        "environment_user_confirmation_required": confirmation,
        "environment_evidence_text_count": len(folded),
        "environment_evidence_counts": counts,
        "environment_source_policy": (
            "temporal_qwen_consensus_non_destructive_v1"
        ),
    }


def iter_report_documents(report: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for query in report.get("queries", []):
        for group in query.get("result_groups", []):
            for document in group.get("documents", []):
                yield document


def materialize_preview_assets(
    db: Path,
    out: Path,
    report: dict[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    documents = list(iter_report_documents(report))
    document_ids = sorted({str(row["document_id"]) for row in documents})
    if not document_ids:
        raise RuntimeError("stop03_5e_display_documents_missing")
    placeholders = ",".join("?" for _ in document_ids)
    with search_contract.connect_ro(db) as con:
        rows = con.execute(
            f"""SELECT d.document_id,d.derived_id,d.source_content_id,
                d.media_type,d.time_position_ms,d.qwen_text,a.derived_path
                FROM stop03_5d_text_documents d
                JOIN derived_assets a ON a.derived_id=d.derived_id
                WHERE d.embedding_run_id=?
                  AND d.document_id IN ({placeholders})""",
            (report["selected_embedding_run_id"], *document_ids),
        ).fetchall()
    source_by_document = {str(row["document_id"]): dict(row) for row in rows}
    video_source_ids = sorted({
        str(row["source_content_id"])
        for row in source_by_document.values()
        if row["media_type"] == "video"
    })
    context_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if video_source_ids:
        source_placeholders = ",".join("?" for _ in video_source_ids)
        with search_contract.connect_ro(db) as con:
            context_rows = con.execute(
                f"""SELECT source_content_id,document_id,time_position_ms,qwen_text
                    FROM stop03_5d_text_documents
                    WHERE embedding_run_id=?
                      AND source_content_id IN ({source_placeholders})
                      AND qwen_text<>''
                    ORDER BY source_content_id,time_position_ms,document_id""",
                (report["selected_embedding_run_id"], *video_source_ids),
            ).fetchall()
        for row in context_rows:
            context_by_source[str(row["source_content_id"])].append(dict(row))
    assets_dir = out / "reports/assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_status_by_name: dict[str, str] = {}
    source_missing_count = 0
    nominal_window_ms = int(
        report.get("video_preview_window_ms", config["video_preview_window_ms"])
    )
    if nominal_window_ms not in config["video_preview_window_options_ms"]:
        raise RuntimeError("stop03_5e_video_preview_window_not_allowed")
    anchor_offset_ms = int(config["video_preview_anchor_offset_ms"])
    precision = str(
        report.get("timecode_precision", config["timecode_default_precision"])
    )
    neighbor_each_side = int(config["environment_neighbor_count_each_side"])
    segment_count = 0
    for document in documents:
        source_row = source_by_document.get(str(document["document_id"]))
        if source_row is None:
            document["preview_asset_status"] = "missing_database_mapping"
            source_missing_count += 1
            continue
        source = Path(str(source_row["derived_path"]))
        suffix = source.suffix.lower() or ".jpg"
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(source_row["derived_id"]))
        fingerprint = sha256_text(str(source))[:16]
        asset_name = f"{safe_id}_{fingerprint}{suffix}"
        asset_path = assets_dir / asset_name
        status = asset_status_by_name.get(asset_name)
        if status is None:
            if not source.is_file():
                status = "missing_source_derived_frame"
                source_missing_count += 1
            elif asset_path.exists():
                status = "existing_asset"
            elif os.path.lexists(asset_path):
                status = "broken_existing_asset"
                source_missing_count += 1
            else:
                try:
                    relative_target = os.path.relpath(source, assets_dir)
                    asset_path.symlink_to(relative_target)
                    status = "relative_symlink"
                except OSError:
                    try:
                        shutil.copy2(source, asset_path)
                        status = "readonly_copy"
                    except OSError:
                        status = "asset_materialization_failed"
                        source_missing_count += 1
            asset_status_by_name[asset_name] = status
        if status in {"relative_symlink", "readonly_copy", "existing_asset"}:
            document["preview_asset_src"] = f"assets/{asset_name}"
        document["preview_asset_status"] = status
        if source_row["media_type"] == "video":
            point_ms = int(source_row["time_position_ms"])
            context = sorted(
                context_by_source.get(str(source_row["source_content_id"]), []),
                key=lambda row: (
                    abs(int(row["time_position_ms"]) - point_ms),
                    int(row["time_position_ms"]),
                    str(row["document_id"]),
                ),
            )[: neighbor_each_side * 2 + 1]
            environment_texts = [str(row["qwen_text"]) for row in context]
        else:
            environment_texts = [str(source_row["qwen_text"] or "")]
        document.update(classify_environment_texts(environment_texts))
        if source_row["media_type"] == "video":
            point_ms = int(source_row["time_position_ms"])
            if point_ms >= 0:
                start_ms = max(0, point_ms - anchor_offset_ms)
                end_ms = start_ms + nominal_window_ms
                document["preview_segment_start_ms"] = start_ms
                document["preview_segment_end_ms"] = end_ms
                document["preview_segment_nominal_duration_ms"] = nominal_window_ms
                document["preview_segment_requires_source_duration_clamp"] = True
                document["timecode"] = format_timecode(point_ms, precision)
                document["preview_segment_start_timecode"] = format_timecode(
                    start_ms, precision
                )
                document["preview_segment_end_timecode"] = format_timecode(
                    end_ms, precision
                )
                segment_count += 1
        else:
            document["timecode"] = None
    statuses = list(asset_status_by_name.values())
    return {
        "displayed_document_occurrence_count": len(documents),
        "displayed_unique_document_count": len(document_ids),
        "unique_preview_asset_count": len(asset_status_by_name),
        "preview_asset_relative_symlink_count": statuses.count("relative_symlink"),
        "preview_asset_readonly_copy_count": statuses.count("readonly_copy"),
        "preview_asset_existing_count": statuses.count("existing_asset"),
        "preview_asset_missing_count": source_missing_count,
        "video_preview_segment_count": segment_count,
        "video_preview_window_ms": nominal_window_ms,
        "video_preview_window_options_ms": list(
            config["video_preview_window_options_ms"]
        ),
        "timecode_precision": precision,
        "environment_label_policy": config["environment_label_policy"],
        "original_video_clip_generated": False,
    }


def validate_html_asset_refs(html_text: str, report_dir: Path) -> dict[str, Any]:
    sources = re.findall(r'<img\s+[^>]*src="([^"]+)"', html_text)
    relative = [value for value in sources if value.startswith("assets/")]
    absolute = [value for value in sources if value.startswith("/")]
    file_urls = [value for value in sources if value.startswith("file://")]
    escaping = [value for value in sources if ".." in Path(value).parts]
    missing = [value for value in relative if not (report_dir / value).is_file()]
    passed = (
        len(relative) == len(sources)
        and not absolute
        and not file_urls
        and not escaping
        and not missing
    )
    return {
        "html_img_total_count": len(sources),
        "html_img_relative_assets_count": len(relative),
        "html_img_absolute_path_count": len(absolute),
        "html_img_file_url_count": len(file_urls),
        "html_img_parent_escape_count": len(escaping),
        "html_img_missing_asset_count": len(missing),
        "html_img_http_accessible_check_status": (
            "PASS_STATIC_RELATIVE_ASSETS" if passed else "FAIL"
        ),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def render_html(report: Mapping[str, Any]) -> str:
    title = str(report.get("html_title", "Stop03-5E 文本搜索 smoke"))
    sections: list[str] = []
    for query in report["queries"]:
        groups: list[str] = []
        for rank, group in enumerate(query["result_groups"], 1):
            docs_parts: list[str] = []
            for document in group["documents"]:
                image_src = document.get("preview_asset_src")
                image = (
                    '<img loading="lazy" src="'
                    + html.escape(str(image_src), quote=True)
                    + '" alt="derived preview">'
                    if image_src else '<div class="missing">派生预览图缺失</div>'
                )
                segment = ""
                if "preview_segment_start_ms" in document:
                    segment = (
                        "<p class=\"segment\">视频预览区间："
                        + html.escape(str(document["preview_segment_start_timecode"]))
                        + "–"
                        + html.escape(str(document["preview_segment_end_timecode"]))
                        + "（正式播放器按视频结尾截断）</p>"
                    )
                time_display = (
                    "命中时间：" + html.escape(str(document["timecode"]))
                    if document.get("timecode") else "静态图片"
                )
                environment_display = (
                    "场景：" + html.escape(str(document["environment_label"]))
                )
                docs_parts.append(
                    "<li class=\"document\">" + image + "<div><p>"
                    + html.escape(str(document["media_type"]))
                    + " · " + html.escape(str(document["source_relative_path"]))
                    + " · " + time_display
                    + " · " + html.escape(str(document["document_id"]))
                    + "</p><p class=\"environment\">" + environment_display
                    + "</p>" + segment + "</div></li>"
                )
            docs = "".join(docs_parts)
            groups.append(
                f"<article><h3>#{rank} score={group['semantic_score']:.6f} "
                f"exact={str(group['exact_text_match']).lower()}</h3>"
                f"<p><code>{html.escape(group['text_vector_id'])}</code> · "
                f"documents={group['matching_document_count']}</p>"
                f"<pre>{html.escape(group['representative_text_preview'])}</pre>"
                f"<ul>{docs}</ul></article>"
            )
        sections.append(
            f"<section><h2>查询 {query['query_index']} · "
            f"sha256={html.escape(query['query_sha256'][:16])}…</h2>"
            + "".join(groups) + "</section>"
        )
    return "<!doctype html><meta charset=\"utf-8\"><title>" + html.escape(title) + "</title>" + """
<style>body{font-family:system-ui;margin:24px;background:#f5f5f5;color:#222}
section{background:white;padding:18px;margin:18px 0;border-radius:10px}
article{border-top:1px solid #ddd;padding:10px 0}pre{white-space:pre-wrap;background:#fafafa;padding:10px}
code{font-size:12px}.document{display:grid;grid-template-columns:minmax(220px,360px) 1fr;gap:16px;
align-items:start;margin:14px 0;list-style:none}.document img{width:100%;max-height:240px;object-fit:contain;
background:#111;border-radius:8px}.segment{font-weight:650;color:#175a8a}.missing{padding:40px;background:#222;color:#fff}
.environment{font-weight:650;color:#7a3f00}
@media(max-width:700px){.document{grid-template-columns:1fr}}</style><h1>""" + html.escape(title) + "</h1>" + "".join(sections)


def write_visual_report(
    db: Path,
    out: Path,
    report: dict[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    visual = materialize_preview_assets(db, out, report, config)
    report["visual_preview"] = visual
    html_text = render_html(report)
    html_checks = validate_html_asset_refs(html_text, out / "reports")
    report["visual_preview"].update(html_checks)
    visuals_pass = (
        visual["preview_asset_missing_count"] == 0
        and html_checks["html_img_total_count"]
        == visual["displayed_document_occurrence_count"]
        and html_checks["html_img_http_accessible_check_status"]
        == "PASS_STATIC_RELATIVE_ASSETS"
    )
    report["technical_checks"]["all_displayed_documents_have_preview_assets"] = visuals_pass
    report["technical_status"] = (
        "PASS" if all(report["technical_checks"].values()) else "FAIL"
    )
    report["status"] = (
        "TECHNICAL_PASS_POLICY_REVIEW"
        if report["technical_status"] == "PASS" else "FAIL"
    )
    write_json(out / "reports/stop03_5e_query_smoke_results.json", report)
    (out / "reports/stop03_5e_query_smoke_results.html").write_text(
        render_html(report), encoding="utf-8"
    )
    return report


def execute_smoke(
    *,
    db: Path,
    config_path: Path,
    out: Path,
    queries: Sequence[str],
    args: argparse.Namespace,
    embedder: QueryEmbedder,
) -> dict[str, Any]:
    contract_summary = search_contract.build_preflight(db, config_path)
    config = search_contract.load_config(config_path)
    normalized_queries = validate_queries(queries, config)
    db_before = search_contract.sha256_file(db)
    run_id = str(contract_summary["selected_embedding_run_id"])
    filter_sql, filter_values = build_document_filter_sql(args)
    documents_by_vector = load_search_documents(
        db, run_id, filter_sql, filter_values
    )
    if not documents_by_vector:
        raise RuntimeError("stop03_5e_no_documents_after_filters")
    with search_contract.connect_ro(db) as con:
        run = con.execute(
            "SELECT * FROM stop03_5d_text_embedding_runs WHERE embedding_run_id=?",
            (run_id,),
        ).fetchone()
    started = time.monotonic()
    query_vectors, runtime = embedder(
        Path(str(run["model_path"])),
        list(normalized_queries),
        str(config["query_prompt_name"]),
        args.device,
    )
    validate_query_vectors(
        query_vectors, len(normalized_queries), int(run["model_dimension"])
    )
    scan_started = time.monotonic()
    results, scan_stats = scan_cosine_groups(
        normalized_queries,
        query_vectors,
        iter_vector_chunks(
            db,
            run_id,
            filter_sql,
            filter_values,
            int(config["vector_scan_chunk_size"]),
        ),
        documents_by_vector,
        top_groups=args.top_groups,
        documents_per_group=args.documents_per_group,
    )
    scan_seconds = time.monotonic() - scan_started
    total_seconds = time.monotonic() - started
    db_after = search_contract.sha256_file(db)
    technical_checks = {
        "query_count_matches": len(results) == len(normalized_queries),
        "all_queries_have_results": all(row["result_group_count"] > 0 for row in results),
        "all_scores_finite": all(
            math.isfinite(float(group["semantic_score"]))
            for row in results for group in row["result_groups"]
        ),
        "all_results_traceable": all(
            group["text_vector_id"]
            and all(document["document_id"] for document in group["documents"])
            for row in results for group in row["result_groups"]
        ),
        "all_eligible_vectors_scanned": (
            scan_stats["scanned_vector_count"] == len(documents_by_vector)
        ),
        "central_db_unchanged": db_before == db_after,
    }
    technical_status = "PASS" if all(technical_checks.values()) else "FAIL"
    report = {
        "status": "TECHNICAL_PASS_POLICY_REVIEW"
        if technical_status == "PASS" else "FAIL",
        "technical_status": technical_status,
        "policy_status": "REVIEW",
        "semantic_relevance_status": "HUMAN_REVIEW_REQUIRED",
        "commit_status": "DO_NOT_COMMIT",
        "smoke_version": SMOKE_VERSION,
        "contract_version": search_contract.CONTRACT_VERSION,
        "selected_embedding_run_id": run_id,
        "selected_model_name": run["model_name"],
        "timecode_precision": args.timecode_precision,
        "query_count": len(normalized_queries),
        "query_text_persisted": False,
        "query_vectors_persisted": False,
        "eligible_document_count": sum(len(value) for value in documents_by_vector.values()),
        "eligible_unique_vector_count": len(documents_by_vector),
        "scanned_unique_vector_count": scan_stats["scanned_vector_count"],
        "vector_scan_chunk_count": scan_stats["scanned_chunk_count"],
        "vector_scan_chunk_size": int(config["vector_scan_chunk_size"]),
        "top_groups": args.top_groups,
        "documents_per_group": args.documents_per_group,
        "filters": {
            "media_type": args.media_type,
            "document_kind": args.document_kind,
            "source_content_id": args.source_content_id,
            "source_relative_path_prefix": args.source_relative_path_prefix,
            "time_position_ms_min": args.time_position_ms_min,
            "time_position_ms_max": args.time_position_ms_max,
        },
        "runtime": {**runtime, "cosine_scan_seconds": scan_seconds,
                    "total_search_seconds": total_seconds,
                    "peak_rss_bytes": peak_rss_bytes()},
        "technical_checks": technical_checks,
        "queries": results,
        "central_db_sha256_before": db_before,
        "central_db_sha256_after": db_after,
        "database_write": False,
        "query_model_run": True,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "search_index_created": False,
    }
    return write_visual_report(db, out, report, config)


def render_existing_report(
    db: Path,
    config_path: Path,
    out: Path,
    timecode_precision: Optional[str] = None,
) -> dict[str, Any]:
    report_path = out / "reports/stop03_5e_query_smoke_results.json"
    if not report_path.is_file():
        raise RuntimeError("stop03_5e_existing_query_report_missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("smoke_version") != SMOKE_VERSION:
        raise RuntimeError("stop03_5e_existing_query_report_version_mismatch")
    if report.get("query_text_persisted") is not False:
        raise RuntimeError("stop03_5e_existing_query_privacy_contract_invalid")
    db_before = search_contract.sha256_file(db)
    config = search_contract.load_config(config_path)
    if timecode_precision is not None:
        report["timecode_precision"] = timecode_precision
    report = write_visual_report(db, out, report, config)
    db_after = search_contract.sha256_file(db)
    report["central_db_sha256_before"] = db_before
    report["central_db_sha256_after"] = db_after
    report["technical_checks"]["central_db_unchanged"] = db_before == db_after
    report["technical_status"] = (
        "PASS" if all(report["technical_checks"].values()) else "FAIL"
    )
    report["status"] = (
        "TECHNICAL_PASS_POLICY_REVIEW"
        if report["technical_status"] == "PASS" else "FAIL"
    )
    write_json(report_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("preflight", "real-smoke", "render-existing"),
        required=True,
    )
    parser.add_argument("--db", type=Path, default=search_contract.DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=search_contract.DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--top-groups", type=int, default=8)
    parser.add_argument("--documents-per-group", type=int, default=3)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument(
        "--timecode-precision", choices=("second", "millisecond"),
        default="millisecond",
    )
    parser.add_argument("--media-type", choices=("image", "video"))
    parser.add_argument(
        "--document-kind",
        choices=("direct_only", "propagation_only", "direct_and_propagation"),
    )
    parser.add_argument("--source-content-id")
    parser.add_argument("--source-relative-path-prefix")
    parser.add_argument("--time-position-ms-min", type=int)
    parser.add_argument("--time-position-ms-max", type=int)
    parser.add_argument("--confirm-real-local-query-smoke", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = search_contract.load_config(args.config)
    if args.mode == "render-existing":
        report = render_existing_report(
            args.db, args.config, args.out, args.timecode_precision
        )
        public = {key: value for key, value in report.items() if key != "queries"}
        print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
        return 0 if report["technical_status"] == "PASS" else 2
    queries = validate_queries(args.query, config)
    if len(queries) < 3 or len(queries) > 5:
        raise RuntimeError("stop03_5e_smoke_requires_3_to_5_queries")
    if args.top_groups < 1 or args.top_groups > int(config["max_vector_group_limit"]):
        raise RuntimeError("stop03_5e_top_groups_out_of_range")
    if args.documents_per_group < 1:
        raise RuntimeError("stop03_5e_documents_per_group_invalid")
    preflight = search_contract.build_preflight(args.db, args.config)
    if args.mode == "preflight":
        result = {
            **preflight,
            "smoke_version": SMOKE_VERSION,
            "smoke_query_count": len(queries),
            "smoke_query_sha256": [sha256_text(value) for value in queries],
            "query_text_persisted": False,
            "query_model_run": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_real_local_query_smoke:
        raise RuntimeError("stop03_5e_real_query_smoke_confirmation_required")
    report = execute_smoke(
        db=args.db,
        config_path=args.config,
        out=args.out,
        queries=queries,
        args=args,
        embedder=real_query_embedder,
    )
    public = {key: value for key, value in report.items() if key != "queries"}
    public["query_result_group_counts"] = [
        row["result_group_count"] for row in report["queries"]
    ]
    print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
