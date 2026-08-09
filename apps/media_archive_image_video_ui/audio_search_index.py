from __future__ import annotations

import json
import math
import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_VERSION = "media_archive_audio_search_index_v1"


def pack_float32(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_float32(value: bytes, dimension: int) -> tuple[float, ...]:
    if len(value) != dimension * 4:
        raise RuntimeError("audio_vector_blob_dimension_mismatch")
    return struct.unpack(f"<{dimension}f", value)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("audio_vector_dimension_mismatch")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS audio_search_metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_speech_evidence(
                evidence_id TEXT PRIMARY KEY,
                source_content_id TEXT NOT NULL,
                source_path TEXT,
                start_time_ms INTEGER NOT NULL CHECK(start_time_ms >= 0),
                end_time_ms INTEGER NOT NULL CHECK(end_time_ms > start_time_ms),
                hit_time_ms INTEGER NOT NULL,
                transcript_text TEXT NOT NULL,
                language TEXT,
                preview_windows_json TEXT NOT NULL,
                evidence_type TEXT NOT NULL DEFAULT 'speech_text',
                is_fixture INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audio_text_embeddings(
                evidence_id TEXT PRIMARY KEY REFERENCES audio_speech_evidence(evidence_id),
                model_name TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector_dtype TEXT NOT NULL CHECK(vector_dtype='float32'),
                vector_blob BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audio_speech_source_time
                ON audio_speech_evidence(source_content_id, start_time_ms, end_time_ms);
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO audio_search_metadata(key,value) VALUES('schema_version',?)",
            (SCHEMA_VERSION,),
        )
        con.commit()


def upsert_evidence_and_vector(
    con: sqlite3.Connection,
    *,
    evidence_id: str,
    source_content_id: str,
    source_path: str | None,
    start_time_ms: int,
    end_time_ms: int,
    hit_time_ms: int,
    transcript_text: str,
    language: str | None,
    preview_windows: dict[str, object],
    model_name: str,
    vector: Sequence[float],
    is_fixture: bool = False,
) -> None:
    con.execute(
        """INSERT OR REPLACE INTO audio_speech_evidence(
               evidence_id,source_content_id,source_path,start_time_ms,end_time_ms,
               hit_time_ms,transcript_text,language,preview_windows_json,is_fixture
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence_id,
            source_content_id,
            source_path,
            start_time_ms,
            end_time_ms,
            hit_time_ms,
            transcript_text,
            language,
            json.dumps(preview_windows, ensure_ascii=False, sort_keys=True),
            int(is_fixture),
        ),
    )
    con.execute(
        """INSERT OR REPLACE INTO audio_text_embeddings(
               evidence_id,model_name,dimension,vector_dtype,vector_blob
           ) VALUES(?,?,?,?,?)""",
        (evidence_id, model_name, len(vector), "float32", pack_float32(vector)),
    )


def search_database(
    path: Path,
    *,
    query: str,
    query_vector: Sequence[float],
    limit: int = 10,
) -> list[dict[str, object]]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            """SELECT e.*,v.dimension,v.vector_blob
               FROM audio_speech_evidence e
               JOIN audio_text_embeddings v USING(evidence_id)"""
        ).fetchall()
    normalized_query = query.strip().casefold()
    results: list[dict[str, object]] = []
    for row in rows:
        document_vector = unpack_float32(row["vector_blob"], int(row["dimension"]))
        semantic_score = cosine(query_vector, document_vector)
        text = str(row["transcript_text"])
        lexical_score = 1.0 if normalized_query and normalized_query in text.casefold() else 0.0
        results.append(
            {
                "evidence_id": row["evidence_id"],
                "source_content_id": row["source_content_id"],
                "source_path": row["source_path"],
                "start_time_ms": row["start_time_ms"],
                "end_time_ms": row["end_time_ms"],
                "hit_time_ms": row["hit_time_ms"],
                "transcript_text": text,
                "language": row["language"],
                "preview_windows": json.loads(row["preview_windows_json"]),
                "semantic_score": semantic_score,
                "lexical_score": lexical_score,
                "final_score": semantic_score * 0.9 + lexical_score * 0.1,
                "is_fixture": bool(row["is_fixture"]),
            }
        )
    results.sort(key=lambda item: (-float(item["final_score"]), str(item["evidence_id"])))
    return results[:limit]


def database_checks(path: Path) -> dict[str, object]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
        evidence_count = con.execute("SELECT COUNT(*) FROM audio_speech_evidence").fetchone()[0]
        vector_count = con.execute("SELECT COUNT(*) FROM audio_text_embeddings").fetchone()[0]
    return {
        "integrity_check": integrity,
        "foreign_key_error_count": len(foreign_keys),
        "evidence_count": evidence_count,
        "vector_count": vector_count,
    }
