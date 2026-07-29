#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCRIPT_VERSION = "source_frame_dedup_central_db_v1"
TASK_LABEL = "central_db_source_frame_dedup_full_run"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
ALLOWED_SOURCE_ROOTS = {
    Path("/Users/yourname/Documents/001DZLtest"),
    Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE"),
}
DEFAULT_QUICK_BYTES = 65536
DEFAULT_VISUAL_HASH_THRESHOLD = 6
DEFAULT_OPENCLIP_COSINE_THRESHOLD = 0.98
DEFAULT_VIDEO_TIME_WINDOW_SEC = 30


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS dedup_runs (
        run_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL,
        script_version TEXT NOT NULL,
        status TEXT NOT NULL,
        technical_status TEXT NOT NULL,
        policy_status TEXT NOT NULL,
        input_count INTEGER NOT NULL,
        output_count INTEGER NOT NULL,
        canonical_count INTEGER NOT NULL,
        duplicate_count INTEGER NOT NULL,
        blocked_count INTEGER NOT NULL,
        failed_count INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_message TEXT,
        settings_json TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_asset_identity (
        identity_id TEXT PRIMARY KEY,
        source_content_id TEXT NOT NULL,
        source_file_record_id TEXT NOT NULL UNIQUE,
        file_size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        quick_hash TEXT,
        full_content_hash TEXT,
        hash_algorithm TEXT NOT NULL,
        quick_hash_bytes INTEGER NOT NULL,
        identity_status TEXT NOT NULL CHECK(identity_status IN ('unique','canonical','exact_duplicate','blocked','failed')),
        canonical_source_content_id TEXT,
        canonical_source_file_record_id TEXT,
        duplicate_group_id TEXT,
        eligible_for_heavy_models INTEGER NOT NULL CHECK(eligible_for_heavy_models IN (0,1)),
        reason_codes TEXT NOT NULL,
        run_id TEXT NOT NULL,
        script_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),
        FOREIGN KEY(source_file_record_id) REFERENCES source_file_records(source_file_id),
        FOREIGN KEY(canonical_source_content_id) REFERENCES source_assets(source_content_id),
        FOREIGN KEY(canonical_source_file_record_id) REFERENCES source_file_records(source_file_id),
        FOREIGN KEY(run_id) REFERENCES dedup_runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_duplicate_groups (
        duplicate_group_id TEXT PRIMARY KEY,
        full_content_hash TEXT NOT NULL,
        canonical_source_content_id TEXT NOT NULL,
        canonical_source_file_record_id TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        total_bytes INTEGER NOT NULL,
        canonical_reason TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(canonical_source_content_id) REFERENCES source_assets(source_content_id),
        FOREIGN KEY(canonical_source_file_record_id) REFERENCES source_file_records(source_file_id),
        FOREIGN KEY(run_id) REFERENCES dedup_runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS visual_identity (
        visual_identity_id TEXT PRIMARY KEY,
        visual_unit_id TEXT NOT NULL UNIQUE,
        source_content_id TEXT NOT NULL,
        derived_id TEXT NOT NULL,
        dhash TEXT,
        perceptual_hash_algorithm TEXT NOT NULL,
        sharpness_score REAL,
        identity_status TEXT NOT NULL CHECK(identity_status IN ('unique','canonical','near_duplicate','exact_duplicate','blocked_decoder','failed')),
        canonical_visual_unit_id TEXT,
        visual_duplicate_group_id TEXT,
        eligible_for_heavy_models INTEGER NOT NULL CHECK(eligible_for_heavy_models IN (0,1)),
        blocked_reason TEXT,
        openclip_cosine_to_canonical REAL,
        run_id TEXT NOT NULL,
        script_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(visual_unit_id) REFERENCES visual_units(visual_unit_id),
        FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),
        FOREIGN KEY(derived_id) REFERENCES derived_assets(derived_id),
        FOREIGN KEY(canonical_visual_unit_id) REFERENCES visual_units(visual_unit_id),
        FOREIGN KEY(run_id) REFERENCES dedup_runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS visual_duplicate_groups (
        visual_duplicate_group_id TEXT PRIMARY KEY,
        canonical_visual_unit_id TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        representative_reason TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(canonical_visual_unit_id) REFERENCES visual_units(visual_unit_id),
        FOREIGN KEY(run_id) REFERENCES dedup_runs(run_id)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS canonical_source_assets_for_heavy AS
    SELECT DISTINCT sa.*
    FROM source_assets sa
    JOIN source_asset_identity si ON si.source_content_id = sa.source_content_id
    WHERE si.eligible_for_heavy_models = 1
      AND si.identity_status IN ('unique','canonical')
    """,
    """
    CREATE VIEW IF NOT EXISTS canonical_visual_units_for_heavy AS
    SELECT vu.*
    FROM visual_units vu
    JOIN visual_identity vi ON vi.visual_unit_id = vu.visual_unit_id
    WHERE vi.eligible_for_heavy_models = 1
      AND vi.identity_status IN ('unique','canonical','blocked_decoder')
    """,
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json_text(row) + "\n")
            count += 1
    return count


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    con = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def connect_write(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path.expanduser().resolve()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone() is not None


def table_info(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(f"PRAGMA table_info({table})")]


def scalar(con: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def schema_audit(db_path: Path) -> dict[str, Any]:
    required = ["source_assets", "source_file_records", "derived_assets", "visual_units", "model_runs"]
    with connect_readonly(db_path) as con:
        integrity = [str(row[0]) for row in con.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
        objects = [dict(row) for row in con.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
        schemas = {table: table_info(con, table) for table in required if table_exists(con, table)}
        counts = {table: int(scalar(con, f"SELECT COUNT(*) FROM {table}") or 0) for table in required if table_exists(con, table)}
        related = []
        for obj in objects:
            name = str(obj["name"])
            cols = [str(c["name"]) for c in table_info(con, name)] if obj["type"] == "table" else []
            if any(token in name.lower() or any(token in col.lower() for col in cols) for token in ("identity", "duplicate", "canonical", "dedup")):
                related.append({"name": name, "type": obj["type"], "columns": cols})
        source_roots = [str(row[0]) for row in con.execute("SELECT DISTINCT source_root FROM source_file_records ORDER BY source_root")]
        media_counts = {
            str(row[0]): int(row[1])
            for row in con.execute(
                "SELECT media_kind,COUNT(*) FROM source_file_records WHERE support_status='supported' GROUP BY media_kind"
            )
        }
        visual_counts = {
            str(row[0]): int(row[1])
            for row in con.execute(
                "SELECT sa.media_type,COUNT(*) FROM visual_units vu JOIN source_assets sa ON sa.source_content_id=vu.source_content_id GROUP BY sa.media_type"
            )
        }
    return {
        "database_path": str(db_path.resolve()),
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
        "objects": objects,
        "required_table_info": schemas,
        "required_table_counts": counts,
        "dedup_related_objects": related,
        "source_roots": source_roots,
        "supported_media_counts": media_counts,
        "visual_unit_media_counts": visual_counts,
        "dedup_tables_present": {name: any(obj["name"] == name for obj in objects) for name in (
            "source_asset_identity", "source_duplicate_groups", "visual_identity", "visual_duplicate_groups", "dedup_runs"
        )},
    }


def validate_output_root(output_root: Path) -> None:
    root = output_root.expanduser().resolve()
    allowed = TEST_OUTPUT_ROOT.resolve()
    try:
        root.relative_to(allowed)
    except ValueError as exc:
        raise RuntimeError(f"OUTPUT_ROOT_OUTSIDE_TEST_OUTPUT:{root}") from exc


def preflight(db_path: Path, output_root: Path) -> dict[str, Any]:
    audit = schema_audit(db_path)
    required = {"source_assets", "source_file_records", "derived_assets", "visual_units", "model_runs"}
    present = {str(obj["name"]) for obj in audit["objects"]}
    missing_tables = sorted(required - present)
    invalid_roots = []
    allowed = [p.resolve() for p in ALLOWED_SOURCE_ROOTS]
    for raw in audit["source_roots"]:
        path = Path(raw).expanduser().resolve()
        if not any(path == root or path.is_relative_to(root) for root in allowed):
            invalid_roots.append(str(path))
    validate_output_root(output_root)
    with connect_readonly(db_path) as con:
        source_rows = int(
            scalar(
                con,
                """SELECT COUNT(*) FROM source_file_records sfr
                   JOIN source_assets sa ON sa.source_content_id=sfr.source_content_id
                   WHERE sfr.media_kind IN ('image','video') AND sfr.support_status='supported'
                     AND sa.online_status=1 AND sa.is_deleted_or_missing=0""",
            )
            or 0
        )
        visual_rows = int(scalar(con, "SELECT COUNT(*) FROM visual_units") or 0)
        missing_source_paths = int(
            scalar(
                con,
                """SELECT COUNT(*) FROM source_file_records sfr
                   JOIN source_assets sa ON sa.source_content_id=sfr.source_content_id
                   WHERE sfr.media_kind IN ('image','video') AND sfr.support_status='supported'
                     AND sa.online_status=1 AND sa.is_deleted_or_missing=0
                     AND (sfr.absolute_path IS NULL OR sfr.absolute_path='')""",
            )
            or 0
        )
        missing_visual_paths = int(
            scalar(con, "SELECT COUNT(*) FROM visual_units WHERE visual_file IS NULL OR visual_file=''") or 0
        )
        vector_payloads = resolve_vector_payloads(con)
    status = "PASS" if not missing_tables and not invalid_roots and audit["integrity_check"] == ["ok"] and not audit["foreign_key_check"] else "BLOCKED"
    return {
        "status": status,
        "reason_codes": [
            *( ["BLOCKED_MISSING_REQUIRED_TABLES"] if missing_tables else [] ),
            *( ["BLOCKED_UNAUTHORIZED_SOURCE_ROOT"] if invalid_roots else [] ),
            *( ["BLOCKED_SQLITE_INTEGRITY_CHECK_FAILED"] if audit["integrity_check"] != ["ok"] else [] ),
            *( ["BLOCKED_SQLITE_FOREIGN_KEY_CHECK_FAILED"] if audit["foreign_key_check"] else [] ),
        ],
        "database_path": str(db_path.resolve()),
        "output_root": str(output_root.resolve()),
        "source_input_count": source_rows,
        "visual_input_count": visual_rows,
        "missing_required_tables": missing_tables,
        "invalid_source_roots": invalid_roots,
        "missing_source_path_rows": missing_source_paths,
        "missing_visual_path_rows": missing_visual_paths,
        "openclip_vector_payloads": [str(p) for p in vector_payloads],
        "integrity_check": audit["integrity_check"],
        "foreign_key_check": audit["foreign_key_check"],
        "network_download_install_occurred": False,
        "model_rerun_occurred": False,
        "source_media_modified": False,
        "model_directory_modified": False,
    }


def load_source_rows(db_path: Path) -> list[dict[str, Any]]:
    with connect_readonly(db_path) as con:
        rows = con.execute(
            """
            SELECT sfr.source_file_id, sfr.source_content_id, sfr.absolute_path, sfr.relative_path,
                   sfr.source_root, sfr.file_name, sfr.extension, sfr.media_kind, sfr.support_status,
                   sfr.size_bytes, sfr.mtime_ns, sfr.content_sha256,
                   sa.online_status, sa.is_deleted_or_missing
            FROM source_file_records sfr
            JOIN source_assets sa ON sa.source_content_id=sfr.source_content_id
            WHERE sfr.media_kind IN ('image','video') AND sfr.support_status='supported'
              AND sa.online_status=1 AND sa.is_deleted_or_missing=0
            ORDER BY sfr.source_file_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def quick_hash_file(row: dict[str, Any], quick_bytes: int) -> dict[str, Any]:
    path = Path(str(row["absolute_path"]))
    try:
        stat = path.stat()
        size = int(stat.st_size)
        with path.open("rb") as handle:
            first = handle.read(quick_bytes)
            if size > quick_bytes:
                handle.seek(max(0, size - quick_bytes))
                last = handle.read(quick_bytes)
            else:
                last = first
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(first)
        digest.update(b"\0")
        digest.update(last)
        return {**row, "actual_size": size, "actual_mtime_ns": int(stat.st_mtime_ns), "quick_hash": digest.hexdigest(), "quick_hash_error": ""}
    except OSError as exc:
        return {**row, "actual_size": int(row.get("size_bytes") or 0), "actual_mtime_ns": int(row.get("mtime_ns") or 0), "quick_hash": "", "quick_hash_error": str(exc)}


def full_hash_file(row: dict[str, Any]) -> tuple[str, str]:
    try:
        digest = hashlib.sha256()
        with Path(str(row["absolute_path"])).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), ""
    except OSError as exc:
        return "", str(exc)


def source_canonical_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    relative = str(row.get("relative_path") or "")
    return (relative.count("/"), relative, str(row.get("source_content_id") or ""), str(row.get("source_file_id") or ""))


def compute_source_identity(rows: list[dict[str, Any]], *, run_id: str, max_workers: int, quick_bytes: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        prepared = list(pool.map(lambda row: quick_hash_file(row, quick_bytes), rows))
    prepared.sort(key=lambda row: str(row["source_file_id"]))
    quick_groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in prepared:
        if row["quick_hash"]:
            quick_groups[(int(row["actual_size"]), str(row["quick_hash"]))].append(row)
    needs_full = [row for group in quick_groups.values() if len(group) > 1 for row in group]
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        full_results = list(pool.map(full_hash_file, needs_full))
    full_by_id = {str(row["source_file_id"]): result for row, result in zip(needs_full, full_results)}
    content_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prepared:
        full_hash, error = full_by_id.get(str(row["source_file_id"]), ("", ""))
        row["full_content_hash"] = full_hash
        row["full_hash_error"] = error
        if full_hash:
            content_groups[full_hash].append(row)
    ambiguous_quick_keys = {
        key
        for key, members in quick_groups.items()
        if len(members) > 1 and any(str(member.get("full_hash_error") or "") for member in members)
    }
    duplicate_groups: list[dict[str, Any]] = []
    canonical_by_hash: dict[str, dict[str, Any]] = {}
    for full_hash, members in sorted(content_groups.items()):
        if len(members) < 2:
            continue
        canonical = min(members, key=source_canonical_key)
        canonical_by_hash[full_hash] = canonical
        duplicate_groups.append(
            {
                "duplicate_group_id": stable_id("sdg", full_hash),
                "full_content_hash": full_hash,
                "canonical_source_content_id": canonical["source_content_id"],
                "canonical_source_file_record_id": canonical["source_file_id"],
                "member_count": len(members),
                "total_bytes": sum(int(row["actual_size"]) for row in members),
                "canonical_reason": "shallowest_relative_path_then_lexical_path_then_stable_ids",
                "run_id": run_id,
                "members": [
                    {
                        "source_content_id": row["source_content_id"],
                        "source_file_record_id": row["source_file_id"],
                        "absolute_path": row["absolute_path"],
                        "relative_path": row["relative_path"],
                    }
                    for row in sorted(members, key=source_canonical_key)
                ],
            }
        )
    identities: list[dict[str, Any]] = []
    for row in prepared:
        full_hash = str(row["full_content_hash"])
        canonical = canonical_by_hash.get(full_hash)
        quick_error = str(row["quick_hash_error"])
        full_error = str(row["full_hash_error"])
        quick_key = (int(row["actual_size"]), str(row["quick_hash"]))
        if quick_error:
            status = "blocked"
            canonical_content = None
            canonical_file = None
            group_id = None
            eligible = False
            reasons = ["SOURCE_HASH_READ_BLOCKED"]
        elif full_error:
            status = "failed"
            canonical_content = None
            canonical_file = None
            group_id = None
            eligible = False
            reasons = ["SOURCE_FULL_HASH_FAILED"]
        elif quick_key in ambiguous_quick_keys:
            status = "blocked"
            canonical_content = None
            canonical_file = None
            group_id = stable_id("sdg_probable", *quick_key)
            eligible = False
            reasons = ["PROBABLE_DUPLICATE_HELD_AFTER_PEER_FULL_HASH_FAILURE"]
        elif canonical:
            is_canonical = row["source_file_id"] == canonical["source_file_id"]
            status = "canonical" if is_canonical else "exact_duplicate"
            canonical_content = canonical["source_content_id"]
            canonical_file = canonical["source_file_id"]
            group_id = stable_id("sdg", full_hash)
            eligible = is_canonical
            reasons = ["FULL_CONTENT_HASH_MATCH", "CANONICAL_STABLE_PATH_ORDER"] if is_canonical else ["FULL_CONTENT_HASH_MATCH", "EXACT_DUPLICATE_OF_CANONICAL"]
        else:
            status = "unique"
            canonical_content = row["source_content_id"]
            canonical_file = row["source_file_id"]
            group_id = None
            eligible = True
            reasons = ["UNIQUE_QUICK_HASH"]
        identities.append(
            {
                "identity_id": stable_id("sid", row["source_file_id"]),
                "source_content_id": row["source_content_id"],
                "source_file_record_id": row["source_file_id"],
                "absolute_path": row["absolute_path"],
                "relative_path": row["relative_path"],
                "media_kind": row["media_kind"],
                "file_size": int(row["actual_size"]),
                "mtime_ns": int(row["actual_mtime_ns"]),
                "quick_hash": row["quick_hash"],
                "full_content_hash": full_hash or None,
                "hash_algorithm": "sha256",
                "quick_hash_bytes": int(quick_bytes),
                "identity_status": status,
                "canonical_source_content_id": canonical_content,
                "canonical_source_file_record_id": canonical_file,
                "duplicate_group_id": group_id,
                "eligible_for_heavy_models": bool(eligible),
                "reason_codes": reasons,
                "run_id": run_id,
                "script_version": SCRIPT_VERSION,
            }
        )
    counts = Counter(row["identity_status"] for row in identities)
    summary = {
        "status": "PASS" if not counts["failed"] and not counts["blocked"] else "PASS_WITH_BACKLOG",
        "source_input_count": len(rows),
        "source_identity_row_count": len(identities),
        "canonical_source_count": counts["unique"] + counts["canonical"],
        "duplicate_source_count": counts["exact_duplicate"],
        "source_duplicate_group_count": len(duplicate_groups),
        "blocked_count": counts["blocked"],
        "failed_count": counts["failed"],
        "quick_hash_computed_count": sum(1 for row in identities if row["quick_hash"]),
        "full_hash_computed_count": sum(1 for row in identities if row["full_content_hash"]),
        "exact_duplicate_requires_full_hash": True,
        "max_workers": max_workers,
        "quick_hash_bytes": quick_bytes,
    }
    return identities, duplicate_groups, summary


def resolve_vector_payloads(con: sqlite3.Connection) -> list[Path]:
    if not table_exists(con, "embeddings"):
        return []
    paths: set[Path] = set()
    for row in con.execute("SELECT DISTINCT vector_key FROM embeddings WHERE vector_key LIKE 'jsonl:%'"):
        raw = str(row[0]).removeprefix("jsonl:").split("#", 1)[0]
        path = Path(raw)
        if path.is_file():
            paths.add(path)
    return sorted(paths)


def load_openclip_vectors(db_path: Path) -> tuple[dict[str, list[float]], list[str]]:
    with connect_readonly(db_path) as con:
        payloads = resolve_vector_payloads(con)
    vectors: dict[str, list[float]] = {}
    errors: list[str] = []
    for path in payloads:
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        visual_id = str(row.get("visual_unit_id") or "")
                        vector = row.get("vector")
                        if visual_id and isinstance(vector, list) and vector:
                            vectors[visual_id] = [float(value) for value in vector]
                    except Exception as exc:
                        errors.append(f"{path}:{line_number}:{exc}")
        except OSError as exc:
            errors.append(f"{path}:{exc}")
    return vectors, errors


def load_visual_rows(db_path: Path) -> list[dict[str, Any]]:
    with connect_readonly(db_path) as con:
        rows = con.execute(
            """
            SELECT vu.visual_unit_id, vu.source_content_id, vu.derived_id, vu.visual_file,
                   vu.time_position_ms, vu.near_black, vu.luma_mean, vu.luma_std,
                   da.derived_path, da.derived_type, da.frame_index, da.sha256 AS derived_sha256,
                   sa.relative_path AS source_relative_path, sa.absolute_path AS source_absolute_path,
                   sa.media_type
            FROM visual_units vu
            JOIN derived_assets da ON da.derived_id=vu.derived_id
            JOIN source_assets sa ON sa.source_content_id=vu.source_content_id
            ORDER BY vu.visual_unit_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def dhash_and_sharpness(row: dict[str, Any], decode_backend: str) -> dict[str, Any]:
    if decode_backend == "blocked":
        return {**row, "dhash": "", "sharpness_score": None, "decode_error": "BLOCKED_IMAGE_DECODER_UNAVAILABLE"}
    path = Path(str(row["visual_file"]))
    try:
        from PIL import Image

        with Image.open(path) as image:
            gray = image.convert("L")
            sample = gray.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(sample.getdata())
            bits = 0
            for y in range(8):
                for x in range(8):
                    bits = (bits << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
            sharp = gray.resize((min(256, gray.width), min(256, gray.height)), Image.Resampling.BILINEAR)
            values = list(sharp.getdata())
            width, height = sharp.size
            total = 0
            count = 0
            for y in range(height):
                offset = y * width
                for x in range(width - 1):
                    total += abs(values[offset + x + 1] - values[offset + x])
                    count += 1
            for y in range(height - 1):
                offset = y * width
                next_offset = (y + 1) * width
                for x in range(width):
                    total += abs(values[next_offset + x] - values[offset + x])
                    count += 1
        return {**row, "dhash": f"{bits:016x}", "sharpness_score": round(total / max(1, count), 6), "decode_error": ""}
    except Exception as exc:
        return {**row, "dhash": "", "sharpness_score": None, "decode_error": f"BLOCKED_IMAGE_DECODER_UNAVAILABLE:{exc}"}


def hamming_hex(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return None
    return dot / (left_norm * right_norm)


def visual_representative_key(row: dict[str, Any]) -> tuple[float, int, str, str]:
    sharpness = row.get("sharpness_score")
    return (
        -(float(sharpness) if sharpness is not None else -1.0),
        int(row.get("time_position_ms") or -1),
        str(row.get("derived_path") or row.get("visual_file") or ""),
        str(row.get("visual_unit_id") or ""),
    )


def within_visual_scope(left: dict[str, Any], right: dict[str, Any], video_time_window_sec: int) -> bool:
    if left["source_content_id"] != right["source_content_id"]:
        return False
    if left["media_type"] != "video":
        return True
    left_ms = int(left.get("time_position_ms") or -1)
    right_ms = int(right.get("time_position_ms") or -1)
    return left_ms < 0 or right_ms < 0 or abs(left_ms - right_ms) <= video_time_window_sec * 1000


def compute_visual_identity(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    max_workers: int,
    decode_backend: str,
    dhash_threshold: int,
    video_time_window_sec: int,
    openclip_vectors: dict[str, list[float]],
    openclip_cosine_threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        fingerprinted = list(pool.map(lambda row: dhash_and_sharpness(row, decode_backend), rows))
    fingerprinted.sort(key=lambda row: str(row["visual_unit_id"]))
    clusters: list[list[dict[str, Any]]] = []
    assigned: dict[str, tuple[str, int | None, float | None]] = {}
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fingerprinted:
        by_source[str(row["source_content_id"])].append(row)
    for source_id in sorted(by_source):
        source_clusters: list[list[dict[str, Any]]] = []
        for row in sorted(by_source[source_id], key=visual_representative_key):
            visual_id = str(row["visual_unit_id"])
            if row["decode_error"]:
                source_clusters.append([row])
                assigned[visual_id] = (visual_id, None, None)
                continue
            matched: tuple[list[dict[str, Any]], dict[str, Any], int, float | None] | None = None
            for cluster in source_clusters:
                representative = cluster[0]
                if representative["decode_error"] or not within_visual_scope(row, representative, video_time_window_sec):
                    continue
                exact_bytes = bool(row.get("derived_sha256")) and row.get("derived_sha256") == representative.get("derived_sha256")
                distance = hamming_hex(str(row["dhash"]), str(representative["dhash"]))
                if not exact_bytes and distance > dhash_threshold:
                    continue
                cosine = cosine_similarity(openclip_vectors.get(visual_id), openclip_vectors.get(str(representative["visual_unit_id"])))
                if not exact_bytes and cosine is not None and cosine < openclip_cosine_threshold:
                    continue
                matched = (cluster, representative, distance, cosine)
                break
            if matched is None:
                source_clusters.append([row])
                assigned[visual_id] = (visual_id, 0, 1.0 if visual_id in openclip_vectors else None)
            else:
                cluster, representative, distance, cosine = matched
                cluster.append(row)
                assigned[visual_id] = (str(representative["visual_unit_id"]), distance, cosine)
        clusters.extend(source_clusters)
    groups: list[dict[str, Any]] = []
    group_by_visual_id: dict[str, str | None] = {}
    for cluster in clusters:
        representative = cluster[0]
        if len(cluster) == 1:
            group_by_visual_id[str(representative["visual_unit_id"])] = None
            continue
        group_id = stable_id("vdg", representative["source_content_id"], representative["visual_unit_id"])
        exact = all(
            bool(member.get("derived_sha256")) and member.get("derived_sha256") == representative.get("derived_sha256")
            for member in cluster[1:]
        )
        for member in cluster:
            group_by_visual_id[str(member["visual_unit_id"])] = group_id
        groups.append(
            {
                "visual_duplicate_group_id": group_id,
                "canonical_visual_unit_id": representative["visual_unit_id"],
                "member_count": len(cluster),
                "representative_reason": "highest_sharpness_then_time_position_then_derived_path_then_visual_unit_id",
                "duplicate_type": "exact_duplicate" if exact else "near_duplicate",
                "run_id": run_id,
                "members": [
                    {
                        "visual_unit_id": member["visual_unit_id"],
                        "visual_file": member["visual_file"],
                        "time_position_ms": member["time_position_ms"],
                        "dhash": member["dhash"],
                        "sharpness_score": member["sharpness_score"],
                    }
                    for member in cluster
                ],
            }
        )
    identities: list[dict[str, Any]] = []
    cluster_size = {str(member["visual_unit_id"]): len(cluster) for cluster in clusters for member in cluster}
    row_by_id = {str(row["visual_unit_id"]): row for row in fingerprinted}
    for row in fingerprinted:
        visual_id = str(row["visual_unit_id"])
        canonical_id, distance, cosine = assigned[visual_id]
        is_representative = visual_id == canonical_id
        if row["decode_error"]:
            status = "blocked_decoder"
            eligible = True
            blocked_reason = "BLOCKED_IMAGE_DECODER_UNAVAILABLE"
            reasons = ["BLOCKED_IMAGE_DECODER_UNAVAILABLE", "CONSERVATIVE_PASSTHROUGH"]
        elif cluster_size[visual_id] == 1:
            status = "unique"
            eligible = True
            blocked_reason = None
            reasons = ["UNIQUE_VISUAL_FINGERPRINT_IN_SOURCE_SCOPE"]
        elif is_representative:
            status = "canonical"
            eligible = True
            blocked_reason = None
            reasons = ["VISUAL_DUPLICATE_GROUP_REPRESENTATIVE", "REPRESENTATIVE_STABLE_ORDER"]
        else:
            representative = row_by_id[canonical_id]
            exact = bool(row.get("derived_sha256")) and row.get("derived_sha256") == representative.get("derived_sha256")
            status = "exact_duplicate" if exact else "near_duplicate"
            eligible = False
            blocked_reason = None
            reasons = ["EXACT_DERIVED_SHA256_MATCH"] if exact else ["DHash_NEAR_DUPLICATE", "OPENCLIP_READONLY_AUDIT_PASS" if cosine is not None else "OPENCLIP_VECTOR_UNAVAILABLE"]
        identities.append(
            {
                "visual_identity_id": stable_id("vid", visual_id),
                "visual_unit_id": visual_id,
                "source_content_id": row["source_content_id"],
                "derived_id": row["derived_id"],
                "visual_file": row["visual_file"],
                "source_relative_path": row["source_relative_path"],
                "media_type": row["media_type"],
                "time_position_ms": row["time_position_ms"],
                "frame_index": row["frame_index"],
                "dhash": row["dhash"] or None,
                "perceptual_hash_algorithm": "dhash64",
                "sharpness_score": row["sharpness_score"],
                "identity_status": status,
                "canonical_visual_unit_id": canonical_id,
                "visual_duplicate_group_id": group_by_visual_id.get(visual_id),
                "eligible_for_heavy_models": bool(eligible),
                "blocked_reason": blocked_reason,
                "openclip_cosine_to_canonical": round(cosine, 8) if cosine is not None else None,
                "dhash_distance_to_canonical": distance,
                "reason_codes": reasons,
                "run_id": run_id,
                "script_version": SCRIPT_VERSION,
            }
        )
    identities.sort(key=lambda row: str(row["visual_unit_id"]))
    groups.sort(key=lambda row: str(row["visual_duplicate_group_id"]))
    counts = Counter(row["identity_status"] for row in identities)
    vector_coverage = sum(1 for row in identities if row["visual_unit_id"] in openclip_vectors)
    summary = {
        "status": "PASS" if not counts["failed"] else "FAIL",
        "visual_input_count": len(rows),
        "visual_identity_row_count": len(identities),
        "canonical_visual_count": counts["unique"] + counts["canonical"] + counts["blocked_decoder"],
        "duplicate_visual_count": counts["near_duplicate"] + counts["exact_duplicate"],
        "visual_duplicate_group_count": len(groups),
        "blocked_decoder_count": counts["blocked_decoder"],
        "failed_count": counts["failed"],
        "openclip_vector_coverage_count": vector_coverage,
        "openclip_vector_coverage_ratio": round(vector_coverage / max(1, len(rows)), 6),
        "openclip_similarity_audit_used": bool(openclip_vectors),
        "dhash_threshold": dhash_threshold,
        "openclip_cosine_threshold": openclip_cosine_threshold,
        "video_time_window_sec": video_time_window_sec,
        "max_workers": max_workers,
        "blocked_decoder_conservative_passthrough": True,
    }
    return identities, groups, summary


def semantic_digest(*row_sets: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for rows in row_sets:
        for row in rows:
            filtered = {key: value for key, value in row.items() if key not in {"run_id", "created_at", "updated_at"}}
            digest.update(json_text(filtered).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def policy_assessment(source_summary: dict[str, Any], visual_summary: dict[str, Any], *, deterministic: bool | None) -> tuple[str, list[str], bool]:
    reasons: list[str] = []
    if source_summary["failed_count"] or visual_summary["failed_count"]:
        reasons.append("FAILED_IDENTITY_ROWS_PRESENT")
        return "FAIL", reasons, False
    if not source_summary["exact_duplicate_requires_full_hash"]:
        reasons.append("QUICK_HASH_USED_AS_EXACT_DUPLICATE")
        return "FAIL", reasons, False
    if deterministic is False:
        reasons.append("WORKER_RESULTS_NOT_DETERMINISTIC")
        return "FAIL", reasons, False
    if visual_summary["blocked_decoder_count"]:
        reasons.append("BLOCKED_DECODER_CONSERVATIVE_PASSTHROUGH_REQUIRES_REVIEW")
    if not visual_summary["openclip_similarity_audit_used"]:
        reasons.append("DHASH_ONLY_SEMANTIC_DUPLICATE_LIMITATION")
    elif visual_summary["openclip_vector_coverage_ratio"] < 0.95:
        reasons.append("OPENCLIP_READONLY_VECTOR_COVERAGE_BELOW_95_PERCENT")
    if visual_summary["duplicate_visual_count"] > max(10, int(visual_summary["visual_input_count"] * 0.8)):
        reasons.append("VISUAL_DUPLICATE_RATIO_ANOMALOUS")
        return "FAIL", reasons, False
    if reasons:
        return "REVIEW", reasons, False
    return "PASS", ["FULL_HASH_EXACT_SOURCE_CONTRACT_PASS", "CANONICAL_SELECTION_STABLE", "OPENCLIP_READONLY_AUDIT_AVAILABLE"], True


def manifest_rows(source_ids: list[dict[str, Any]], source_groups: list[dict[str, Any]], visual_ids: list[dict[str, Any]], visual_groups: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "source_asset_identity_manifest.jsonl": source_ids,
        "canonical_source_manifest.jsonl": [row for row in source_ids if row["eligible_for_heavy_models"]],
        "source_duplicate_groups_manifest.jsonl": source_groups,
        "frame_visual_identity_manifest.jsonl": visual_ids,
        "canonical_visual_candidate_manifest.jsonl": [row for row in visual_ids if row["eligible_for_heavy_models"]],
        "frame_visual_groups_manifest.jsonl": visual_groups,
    }


def write_manifests(output_root: Path, rows_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {name: write_jsonl(output_root / "manifests" / name, rows) for name, rows in rows_by_name.items()}


def create_schema(con: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        con.execute(statement)


SOURCE_DB_FIELDS = [
    "identity_id", "source_content_id", "source_file_record_id", "file_size", "mtime_ns", "quick_hash",
    "full_content_hash", "hash_algorithm", "quick_hash_bytes", "identity_status", "canonical_source_content_id",
    "canonical_source_file_record_id", "duplicate_group_id", "eligible_for_heavy_models", "reason_codes", "run_id",
    "script_version",
]
VISUAL_DB_FIELDS = [
    "visual_identity_id", "visual_unit_id", "source_content_id", "derived_id", "dhash", "perceptual_hash_algorithm",
    "sharpness_score", "identity_status", "canonical_visual_unit_id", "visual_duplicate_group_id",
    "eligible_for_heavy_models", "blocked_reason", "openclip_cosine_to_canonical", "run_id", "script_version",
]


def comparable_db_value(field: str, value: Any) -> Any:
    if field == "reason_codes":
        return json_text(value)
    if field == "eligible_for_heavy_models":
        return int(bool(value))
    return value


def classify_changes(con: sqlite3.Connection, table: str, pk: str, fields: list[str], rows: list[dict[str, Any]]) -> Counter:
    result: Counter = Counter()
    for row in rows:
        existing = con.execute(f"SELECT {','.join(fields)} FROM {table} WHERE {pk}=?", (row[pk],)).fetchone() if table_exists(con, table) else None
        if existing is None:
            result["new"] += 1
            continue
        incoming = [comparable_db_value(field, row.get(field)) for field in fields]
        current = [existing[field] for field in fields]
        if incoming == current:
            result["unchanged"] += 1
        else:
            result["updated"] += 1
    return result


def upsert_rows(con: sqlite3.Connection, table: str, pk: str, fields: list[str], rows: list[dict[str, Any]], timestamp: str) -> None:
    columns = fields + ["created_at", "updated_at"]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{field}=excluded.{field}" for field in fields if field != pk) + ",updated_at=excluded.updated_at"
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT({pk}) DO UPDATE SET {updates}"
    for row in rows:
        values = [comparable_db_value(field, row.get(field)) for field in fields] + [timestamp, timestamp]
        con.execute(sql, values)


def backup_database(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        backup_path.unlink()
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def restore_database(backup_path: Path, db_path: Path) -> None:
    source = sqlite3.connect(str(backup_path))
    destination = sqlite3.connect(str(db_path))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def write_database(
    db_path: Path,
    output_root: Path,
    run_id: str,
    source_ids: list[dict[str, Any]],
    source_groups: list[dict[str, Any]],
    visual_ids: list[dict[str, Any]],
    visual_groups: list[dict[str, Any]],
    final_report: dict[str, Any],
    *,
    inject_failure_before_commit: bool = False,
) -> dict[str, Any]:
    backup_path = output_root / "database_backup" / db_path.name
    backup_database(db_path, backup_path)
    timestamp = utc_now()
    con = connect_write(db_path)
    committed = False
    try:
        con.execute("BEGIN IMMEDIATE")
        create_schema(con)
        source_changes = classify_changes(con, "source_asset_identity", "identity_id", SOURCE_DB_FIELDS, source_ids)
        visual_changes = classify_changes(con, "visual_identity", "visual_identity_id", VISUAL_DB_FIELDS, visual_ids)
        run_payload = (
            run_id,
            "source_frame_dedup",
            SCRIPT_VERSION,
            "running",
            final_report["technical_status"],
            final_report["policy_status"],
            len(source_ids) + len(visual_ids),
            len(source_ids) + len(visual_ids),
            final_report["canonical_source_count"] + final_report["canonical_visual_count"],
            final_report["duplicate_source_count"] + final_report["duplicate_visual_count"],
            final_report["blocked_decoder_count"],
            final_report["failed_count"],
            timestamp,
            None,
            "",
            json_text(final_report["settings"]),
            json_text(final_report),
        )
        con.execute(
            """INSERT INTO dedup_runs
            (run_id,stage,script_version,status,technical_status,policy_status,input_count,output_count,canonical_count,
             duplicate_count,blocked_count,failed_count,started_at,finished_at,error_message,settings_json,summary_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
             stage=excluded.stage,script_version=excluded.script_version,status=excluded.status,
             technical_status=excluded.technical_status,policy_status=excluded.policy_status,input_count=excluded.input_count,
             output_count=excluded.output_count,canonical_count=excluded.canonical_count,duplicate_count=excluded.duplicate_count,
             blocked_count=excluded.blocked_count,failed_count=excluded.failed_count,error_message=excluded.error_message,
             settings_json=excluded.settings_json,summary_json=excluded.summary_json""",
            run_payload,
        )
        upsert_rows(con, "source_asset_identity", "identity_id", SOURCE_DB_FIELDS, source_ids, timestamp)
        for group in source_groups:
            con.execute(
                """INSERT INTO source_duplicate_groups
                (duplicate_group_id,full_content_hash,canonical_source_content_id,canonical_source_file_record_id,
                 member_count,total_bytes,canonical_reason,run_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(duplicate_group_id) DO UPDATE SET
                 full_content_hash=excluded.full_content_hash,canonical_source_content_id=excluded.canonical_source_content_id,
                 canonical_source_file_record_id=excluded.canonical_source_file_record_id,member_count=excluded.member_count,
                 total_bytes=excluded.total_bytes,canonical_reason=excluded.canonical_reason,run_id=excluded.run_id""",
                (group["duplicate_group_id"], group["full_content_hash"], group["canonical_source_content_id"],
                 group["canonical_source_file_record_id"], group["member_count"], group["total_bytes"],
                 group["canonical_reason"], run_id, timestamp),
            )
        upsert_rows(con, "visual_identity", "visual_identity_id", VISUAL_DB_FIELDS, visual_ids, timestamp)
        for group in visual_groups:
            con.execute(
                """INSERT INTO visual_duplicate_groups
                (visual_duplicate_group_id,canonical_visual_unit_id,member_count,representative_reason,run_id,created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(visual_duplicate_group_id) DO UPDATE SET
                 canonical_visual_unit_id=excluded.canonical_visual_unit_id,member_count=excluded.member_count,
                 representative_reason=excluded.representative_reason,run_id=excluded.run_id""",
                (group["visual_duplicate_group_id"], group["canonical_visual_unit_id"], group["member_count"],
                 group["representative_reason"], run_id, timestamp),
            )
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
        if foreign_keys:
            raise RuntimeError(f"FOREIGN_KEY_CHECK_FAILED:{foreign_keys[:5]}")
        if inject_failure_before_commit:
            raise RuntimeError("INJECTED_FAILURE_BEFORE_COMMIT")
        finished = utc_now()
        con.execute(
            "UPDATE dedup_runs SET status='done',finished_at=?,summary_json=? WHERE run_id=?",
            (finished, json_text(final_report), run_id),
        )
        con.commit()
        committed = True
        return {
            "status": "PASS",
            "commit_status": "COMMITTED",
            "database_backup_path": str(backup_path),
            "source_identity_changes": dict(source_changes),
            "visual_identity_changes": dict(visual_changes),
            "source_identity_rows_upserted": len(source_ids),
            "source_group_rows_upserted": len(source_groups),
            "visual_identity_rows_upserted": len(visual_ids),
            "visual_group_rows_upserted": len(visual_groups),
            "db_rows_written": len(source_ids) + len(source_groups) + len(visual_ids) + len(visual_groups) + 1,
            "foreign_key_check": [],
            "rollback_status": "NOT_REQUIRED",
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
        if not committed:
            # A transaction failure leaves the central DB unchanged. The backup remains for audit.
            pass


def readback_database(db_path: Path, run_id: str, expected: dict[str, int]) -> dict[str, Any]:
    with connect_readonly(db_path) as con:
        integrity = [str(row[0]) for row in con.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
        counts = {
            "source_asset_identity": int(scalar(con, "SELECT COUNT(*) FROM source_asset_identity WHERE run_id=?", (run_id,)) or 0),
            "source_duplicate_groups": int(scalar(con, "SELECT COUNT(*) FROM source_duplicate_groups WHERE run_id=?", (run_id,)) or 0),
            "visual_identity": int(scalar(con, "SELECT COUNT(*) FROM visual_identity WHERE run_id=?", (run_id,)) or 0),
            "visual_duplicate_groups": int(scalar(con, "SELECT COUNT(*) FROM visual_duplicate_groups WHERE run_id=?", (run_id,)) or 0),
            "dedup_runs": int(scalar(con, "SELECT COUNT(*) FROM dedup_runs WHERE run_id=?", (run_id,)) or 0),
        }
        canonical_source_view = int(scalar(con, "SELECT COUNT(*) FROM canonical_source_assets_for_heavy") or 0)
        canonical_visual_view = int(scalar(con, "SELECT COUNT(*) FROM canonical_visual_units_for_heavy") or 0)
    consistency = all(counts.get(table) == count for table, count in expected.items())
    return {
        "status": "PASS" if integrity == ["ok"] and not foreign_keys and consistency else "FAIL",
        "run_id": run_id,
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
        "db_rows_readback": sum(counts.values()),
        "table_counts_for_run": counts,
        "expected_table_counts": expected,
        "manifest_db_consistency": consistency,
        "canonical_source_heavy_view_count": canonical_source_view,
        "canonical_visual_heavy_view_count": canonical_visual_view,
    }


def assert_canonical_source_for_heavy(con: sqlite3.Connection, source_content_ids: Sequence[str]) -> None:
    allowed = {str(row[0]) for row in con.execute("SELECT source_content_id FROM canonical_source_assets_for_heavy")}
    if any(str(value) not in allowed for value in source_content_ids):
        raise RuntimeError("NON_CANONICAL_SOURCE_ENTERED_HEAVY_STAGE")


def assert_canonical_visual_for_heavy(con: sqlite3.Connection, visual_unit_ids: Sequence[str]) -> None:
    allowed = {str(row[0]) for row in con.execute("SELECT visual_unit_id FROM canonical_visual_units_for_heavy")}
    if any(str(value) not in allowed for value in visual_unit_ids):
        raise RuntimeError("DUPLICATE_VISUAL_CANDIDATE_ENTERED_HEAVY_STAGE")


def expand_canonical_source_to_originals(con: sqlite3.Connection, canonical_source_content_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            """SELECT si.source_content_id,si.source_file_record_id,sfr.absolute_path,sfr.relative_path,si.identity_status
               FROM source_asset_identity si JOIN source_file_records sfr ON sfr.source_file_id=si.source_file_record_id
               WHERE si.canonical_source_content_id=? ORDER BY sfr.relative_path,si.source_file_record_id""",
            (canonical_source_content_id,),
        )
    ]


def expand_visual_candidate_to_originals(con: sqlite3.Connection, canonical_visual_unit_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            """SELECT vi.visual_unit_id,vi.source_content_id,vi.derived_id,vu.visual_file,vi.identity_status
               FROM visual_identity vi JOIN visual_units vu ON vu.visual_unit_id=vi.visual_unit_id
               WHERE vi.canonical_visual_unit_id=? ORDER BY vi.visual_unit_id""",
            (canonical_visual_unit_id,),
        )
    ]


def copy_audit_assets(output_root: Path, visual_groups: list[dict[str, Any]]) -> tuple[dict[str, str], int]:
    assets = output_root / "html" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    missing = 0
    try:
        from PIL import Image
    except Exception:
        return mapping, sum(len(group["members"]) for group in visual_groups)
    for group in visual_groups:
        for member in group["members"]:
            visual_id = str(member["visual_unit_id"])
            source = Path(str(member["visual_file"]))
            target = assets / f"{visual_id}.jpg"
            try:
                with Image.open(source) as image:
                    image.convert("RGB").thumbnail((360, 240), Image.Resampling.LANCZOS)
                    rgb = image.convert("RGB")
                    rgb.thumbnail((360, 240), Image.Resampling.LANCZOS)
                    rgb.save(target, "JPEG", quality=80)
                mapping[visual_id] = f"assets/{target.name}"
            except Exception:
                missing += 1
    return mapping, missing


def render_html(output_root: Path, report: dict[str, Any], source_groups: list[dict[str, Any]], visual_groups: list[dict[str, Any]], blocked_visuals: list[dict[str, Any]]) -> dict[str, Any]:
    asset_map, missing_assets = copy_audit_assets(output_root, visual_groups)
    def esc(value: Any) -> str:
        return html.escape(str(value))

    source_sections = []
    for group in source_groups:
        members = "".join(f"<li>{esc(row['relative_path'])} <small>{esc(row['source_file_record_id'])}</small></li>" for row in group["members"])
        source_sections.append(
            f"<section><h3>{esc(group['duplicate_group_id'])}</h3><p>成员 {group['member_count']}，canonical: {esc(group['canonical_source_file_record_id'])}</p><code>{esc(group['full_content_hash'])}</code><ul>{members}</ul></section>"
        )
    visual_sections = []
    for group in visual_groups:
        cards = []
        for member in group["members"]:
            visual_id = str(member["visual_unit_id"])
            image = f"<img src=\"{esc(asset_map[visual_id])}\" alt=\"{esc(visual_id)}\">" if visual_id in asset_map else "<div class=\"missing\">预览不可用</div>"
            cards.append(
                f"<article>{image}<p>{esc(visual_id)}</p><p>时间 {esc(member['time_position_ms'])} ms · dHash {esc(member['dhash'])} · sharpness {esc(member['sharpness_score'])}</p></article>"
            )
        visual_sections.append(
            f"<section><h3>{esc(group['visual_duplicate_group_id'])}</h3><p>代表帧 {esc(group['canonical_visual_unit_id'])}，成员 {group['member_count']}</p><div class=\"grid\">{''.join(cards)}</div></section>"
        )
    blocked = "".join(f"<li>{esc(row['visual_unit_id'])}: {esc(row['blocked_reason'])}</li>" for row in blocked_visuals) or "<li>无</li>"
    page = f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>中心数据库 Source / Frame 去重审计</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;color:#202124;background:#f7f8fa}}header,main{{max-width:1180px;margin:auto;padding:24px}}header{{background:#fff;border-bottom:1px solid #ddd;max-width:none}}h1{{font-size:28px}}section{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px;margin:16px 0}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}article{{border:1px solid #ddd;padding:10px}}img{{display:block;width:100%;height:200px;object-fit:contain;background:#111}}small,code{{word-break:break-all}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}}.metric{{background:#eef2f7;padding:12px}}.missing{{height:200px;display:grid;place-items:center;background:#eee}}</style></head><body>
<header><h1>中心数据库 Source / Frame 去重审计</h1><p>SQLite 是事实源；JSONL 仅用于导出与审计。</p></header><main>
<section><h2>总览</h2><div class=\"metrics\">
<div class=\"metric\">run_id<br><b>{esc(report['run_id'])}</b></div><div class=\"metric\">technical_status<br><b>{esc(report['technical_status'])}</b></div>
<div class=\"metric\">policy_status<br><b>{esc(report['policy_status'])}</b></div><div class=\"metric\">commit_status<br><b>{esc(report['commit_status'])}</b></div>
<div class=\"metric\">source<br><b>{report['source_input_count']}</b></div><div class=\"metric\">canonical source<br><b>{report['canonical_source_count']}</b></div>
<div class=\"metric\">duplicate source<br><b>{report['duplicate_source_count']}</b></div><div class=\"metric\">visual<br><b>{report['visual_input_count']}</b></div>
<div class=\"metric\">canonical visual<br><b>{report['canonical_visual_count']}</b></div><div class=\"metric\">duplicate visual<br><b>{report['duplicate_visual_count']}</b></div>
<div class=\"metric\">blocked decoder<br><b>{report['blocked_decoder_count']}</b></div><div class=\"metric\">missing audit assets<br><b>{missing_assets}</b></div>
</div><p>数据库：{esc(report['database_path'])}</p></section>
<h2>Source duplicate groups</h2>{''.join(source_sections) or '<section>无 source exact duplicate group。</section>'}
<h2>Visual duplicate groups</h2>{''.join(visual_sections) or '<section>无 visual duplicate group。</section>'}
<section><h2>阻塞项</h2><ul>{blocked}</ul></section>
<section><h2>数据库验证</h2><pre>{esc(json.dumps(report.get('db_validation', {}), ensure_ascii=False, indent=2))}</pre></section>
</main></body></html>"""
    html_path = output_root / "html" / "source_and_frame_dedup_central_db_audit.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(page, encoding="utf-8")
    return {"html_path": str(html_path), "html_asset_count": len(asset_map), "missing_html_asset_count": missing_assets}


def write_final_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Central DB Source / Frame Dedup Report",
        "",
        f"- run_id: `{report['run_id']}`",
        f"- technical_status: `{report['technical_status']}`",
        f"- policy_status: `{report['policy_status']}`",
        f"- commit_status: `{report['commit_status']}`",
        f"- final_verdict: `{report['final_verdict']}`",
        f"- source: {report['source_input_count']} input / {report['canonical_source_count']} canonical / {report['duplicate_source_count']} duplicate",
        f"- visual: {report['visual_input_count']} input / {report['canonical_visual_count']} canonical / {report['duplicate_visual_count']} duplicate",
        f"- blocked decoder: {report['blocked_decoder_count']}",
        f"- workers 1 vs 20 deterministic: {report['workers_1_vs_20_deterministic']}",
        "",
        "## Remaining Risks",
        "",
        *[f"- {item}" for item in report.get("remaining_risks", [])],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).expanduser().resolve()
    run_id = args.run_id or default_run_id()
    output_root = Path(args.output_root).expanduser().resolve() if args.output_root else TEST_OUTPUT_ROOT / f"source-frame-dedup-central-db-{run_id}"
    output_root.mkdir(parents=True, exist_ok=True)
    pre = preflight(db_path, output_root)
    audit = schema_audit(db_path)
    write_json(output_root / "reports" / "preflight.json", pre)
    write_json(output_root / "reports" / "schema_audit.json", audit)
    if pre["status"] != "PASS":
        raise RuntimeError("BLOCKED_PREFLIGHT_FAILED:" + ",".join(pre["reason_codes"]))
    if args.mode == "audit":
        return {"status": "PASS", "run_id": run_id, "output_root": str(output_root), "preflight": pre, "schema_audit": audit}
    if args.mode == "readback":
        expected = {
            "source_asset_identity": int(args.expected_source_rows),
            "source_duplicate_groups": int(args.expected_source_groups),
            "visual_identity": int(args.expected_visual_rows),
            "visual_duplicate_groups": int(args.expected_visual_groups),
            "dedup_runs": 1,
        }
        result = readback_database(db_path, run_id, expected)
        write_json(output_root / "reports" / "db_readback_audit.json", result)
        return result
    source_rows = load_source_rows(db_path)
    visual_rows = load_visual_rows(db_path)
    vectors, vector_errors = load_openclip_vectors(db_path)
    source_ids, source_groups, source_summary = compute_source_identity(
        source_rows, run_id=run_id, max_workers=args.max_workers, quick_bytes=args.quick_bytes
    )
    visual_ids, visual_groups, visual_summary = compute_visual_identity(
        visual_rows,
        run_id=run_id,
        max_workers=args.max_workers,
        decode_backend=args.decode_backend,
        dhash_threshold=args.visual_hash_threshold,
        video_time_window_sec=args.video_time_window_sec,
        openclip_vectors=vectors,
        openclip_cosine_threshold=args.openclip_cosine_threshold,
    )
    deterministic: bool | None = None
    if args.determinism_check:
        source_ids_1, source_groups_1, _ = compute_source_identity(source_rows, run_id=run_id, max_workers=1, quick_bytes=args.quick_bytes)
        visual_ids_1, visual_groups_1, _ = compute_visual_identity(
            visual_rows,
            run_id=run_id,
            max_workers=1,
            decode_backend=args.decode_backend,
            dhash_threshold=args.visual_hash_threshold,
            video_time_window_sec=args.video_time_window_sec,
            openclip_vectors=vectors,
            openclip_cosine_threshold=args.openclip_cosine_threshold,
        )
        digest_1 = semantic_digest(source_ids_1, source_groups_1, visual_ids_1, visual_groups_1)
        digest_configured = semantic_digest(source_ids, source_groups, visual_ids, visual_groups)
        deterministic = digest_1 == digest_configured
    policy_status, policy_reasons, commit_recommendation = policy_assessment(source_summary, visual_summary, deterministic=deterministic)
    technical_status = "PASS"
    if source_summary["failed_count"] or visual_summary["failed_count"] or deterministic is False:
        technical_status = "FAIL"
    overall = "PASS" if technical_status == "PASS" and policy_status == "PASS" else ("PASS_WITH_BACKLOG" if technical_status == "PASS" and policy_status == "REVIEW" else "FAIL")
    rows_by_name = manifest_rows(source_ids, source_groups, visual_ids, visual_groups)
    manifest_counts = write_manifests(output_root, rows_by_name)
    write_json(output_root / "reports" / "source_dedup_summary.json", source_summary)
    write_json(output_root / "reports" / "frame_visual_dedup_summary.json", {**visual_summary, "openclip_vector_payload_errors": vector_errors[:20]})
    report: dict[str, Any] = {
        "status": overall,
        "technical_status": technical_status,
        "policy_status": policy_status,
        "commit_status": "NOT_COMMITTED",
        "commit_recommendation": commit_recommendation,
        "task_label": TASK_LABEL,
        "checkpoint_label": None,
        "script_version": SCRIPT_VERSION,
        "project_root": str(PROJECT_ROOT),
        "database_path": str(db_path),
        "run_id": run_id,
        "output_root": str(output_root),
        **{key: source_summary[key] for key in (
            "source_input_count", "source_identity_row_count", "canonical_source_count", "duplicate_source_count", "source_duplicate_group_count"
        )},
        **{key: visual_summary[key] for key in (
            "visual_input_count", "visual_identity_row_count", "canonical_visual_count", "duplicate_visual_count", "visual_duplicate_group_count", "blocked_decoder_count"
        )},
        "failed_count": source_summary["failed_count"] + visual_summary["failed_count"],
        "db_rows_written": 0,
        "db_rows_readback": 0,
        "manifest_db_consistency": None,
        "integrity_check": pre["integrity_check"],
        "foreign_key_check": pre["foreign_key_check"],
        "workers_1_vs_20_deterministic": deterministic,
        "rerun_idempotent": None,
        "manifest_counts": manifest_counts,
        "policy_reason_codes": policy_reasons,
        "settings": {
            "max_workers": args.max_workers,
            "quick_hash_bytes": args.quick_bytes,
            "visual_hash_method": "dhash64",
            "visual_hash_threshold": args.visual_hash_threshold,
            "openclip_cosine_threshold": args.openclip_cosine_threshold,
            "video_time_window_sec": args.video_time_window_sec,
            "decode_backend": args.decode_backend,
            "determinism_check": args.determinism_check,
        },
        "actual_run_commands": [],
        "rollback_status": "NOT_REQUIRED",
        "network_download_install_occurred": False,
        "model_rerun_occurred": False,
        "source_media_modified": False,
        "model_directory_modified": False,
        "final_verdict": policy_status if technical_status == "PASS" else "FAIL",
        "remaining_risks": policy_reasons if policy_status != "PASS" else ["Cross-file semantic duplicate detection remains outside this identity contract."],
    }
    write_audit = {
        "status": "SKIPPED_DRY_RUN",
        "commit_status": "NOT_COMMITTED",
        "db_rows_written": 0,
        "rollback_status": "NOT_REQUIRED",
    }
    readback = {
        "status": "SKIPPED_DRY_RUN",
        "manifest_db_consistency": None,
        "db_rows_readback": 0,
    }
    if args.mode == "commit":
        if not commit_recommendation and not args.force_commit_review:
            raise RuntimeError(f"BLOCKED_POLICY_NOT_APPROVED_FOR_COMMIT:{policy_status}:{policy_reasons}")
        try:
            write_audit = write_database(
                db_path,
                output_root,
                run_id,
                source_ids,
                source_groups,
                visual_ids,
                visual_groups,
                report,
                inject_failure_before_commit=args.inject_failure_before_commit,
            )
            report["commit_status"] = write_audit["commit_status"]
            report["db_rows_written"] = write_audit["db_rows_written"]
            report["rollback_status"] = write_audit["rollback_status"]
            expected = {
                "source_asset_identity": len(source_ids),
                "source_duplicate_groups": len(source_groups),
                "visual_identity": len(visual_ids),
                "visual_duplicate_groups": len(visual_groups),
                "dedup_runs": 1,
            }
            readback = readback_database(db_path, run_id, expected)
            if readback["status"] != "PASS":
                backup_path = Path(write_audit["database_backup_path"])
                restore_database(backup_path, db_path)
                report["rollback_status"] = "RESTORED_FROM_PRECOMMIT_BACKUP_AFTER_READBACK_FAILURE"
                write_json(output_root / "reports" / "rollback_report.json", {
                    "status": "PASS", "reason_code": "POST_COMMIT_READBACK_FAILED", "backup_path": str(backup_path), "readback": readback
                })
                raise RuntimeError("POST_COMMIT_READBACK_FAILED_DATABASE_RESTORED")
            report["db_rows_readback"] = readback["db_rows_readback"]
            report["manifest_db_consistency"] = readback["manifest_db_consistency"]
            report["integrity_check"] = readback["integrity_check"]
            report["foreign_key_check"] = readback["foreign_key_check"]
            report["db_validation"] = readback
            report["rerun_idempotent"] = write_audit["source_identity_changes"].get("new", 0) == 0 and write_audit["visual_identity_changes"].get("new", 0) == 0
        except Exception as exc:
            write_json(output_root / "reports" / "db_write_audit.json", {"status": "FAIL", "error": str(exc), "rollback_status": "TRANSACTION_ROLLED_BACK_OR_BACKUP_RESTORED"})
            raise
    else:
        report["db_validation"] = {"status": "NOT_COMMITTED_DRY_RUN"}
    html_result = render_html(
        output_root,
        report,
        source_groups,
        visual_groups,
        [row for row in visual_ids if row["identity_status"] == "blocked_decoder"],
    )
    report.update(html_result)
    write_json(output_root / "reports" / "db_write_audit.json", write_audit)
    write_json(output_root / "reports" / "db_readback_audit.json", readback)
    write_json(output_root / "reports" / "final_run_report.json", report)
    write_final_markdown(output_root / "reports" / "final_run_report.md", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Central SQLite source/frame identity dedup and audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-root")
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=["audit", "dry-run", "commit", "readback"], default="audit")
    parser.add_argument("--max-workers", type=int, default=20)
    parser.add_argument("--quick-bytes", type=int, default=DEFAULT_QUICK_BYTES)
    parser.add_argument("--visual-hash-threshold", type=int, default=DEFAULT_VISUAL_HASH_THRESHOLD)
    parser.add_argument("--openclip-cosine-threshold", type=float, default=DEFAULT_OPENCLIP_COSINE_THRESHOLD)
    parser.add_argument("--video-time-window-sec", type=int, default=DEFAULT_VIDEO_TIME_WINDOW_SEC)
    parser.add_argument("--decode-backend", choices=["pillow", "blocked"], default="pillow")
    parser.add_argument("--determinism-check", action="store_true")
    parser.add_argument("--force-commit-review", action="store_true", help="Explicitly commit a conservative REVIEW result.")
    parser.add_argument("--inject-failure-before-commit", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-source-rows", type=int, default=0)
    parser.add_argument("--expected-source-groups", type=int, default=0)
    parser.add_argument("--expected-visual-rows", type=int, default=0)
    parser.add_argument("--expected-visual-groups", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(args)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED" if str(exc).startswith("BLOCKED") else "FAIL", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "PASS_WITH_BACKLOG"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
