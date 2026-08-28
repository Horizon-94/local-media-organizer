"""Synthetic-only fixtures extracted from local tests; no user project or database.

The public hotfix suite does not import the private Golden Set runner or the
older 1.2.4 comparison package just to construct a tiny temporary database.
"""
import hashlib
import sqlite3
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path) -> tuple[Path, Path]:
    database = root / "media_archive.sqlite"
    preview = root / "麦田.jpg"
    preview.write_bytes(b"preview")
    source = root / "麦田.MOV"
    source.write_bytes(b"source")
    script = root / "文稿.txt"
    script.write_text("第一章：测试\n走进现场。\n麦田里的人正在收割成熟的麦子。", encoding="utf-8")
    con = sqlite3.connect(database)
    con.executescript("""
        CREATE TABLE source_assets (
            source_content_id TEXT PRIMARY KEY, absolute_path TEXT NOT NULL,
            file_name TEXT NOT NULL, volume_id TEXT NOT NULL DEFAULT 'LOCAL',
            online_status INTEGER NOT NULL DEFAULT 1, is_deleted_or_missing INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE derived_assets (derived_id TEXT PRIMARY KEY, derived_path TEXT NOT NULL);
        CREATE TABLE stop03_5d_text_documents (
            embedding_run_id TEXT NOT NULL, document_id TEXT PRIMARY KEY,
            source_content_id TEXT NOT NULL, media_type TEXT NOT NULL,
            time_position_ms INTEGER, document_kind TEXT NOT NULL,
            qwen_text TEXT NOT NULL, ocr_text TEXT NOT NULL,
            propagated_labels_json TEXT NOT NULL, source_relative_path TEXT NOT NULL,
            derived_id TEXT NOT NULL, quality_status TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    con.execute("INSERT INTO source_assets VALUES ('s1',?,'麦田.MOV','LOCAL',1,0)", (str(source),))
    con.execute("INSERT INTO derived_assets VALUES ('d1',?)", (str(preview),))
    con.execute("""INSERT INTO stop03_5d_text_documents VALUES
        ('run','doc1','s1','video',8000,'direct_only',?, '', '[]','麦田.MOV','d1','PASS','2026-01-01')""",
        ("中全景中几位农民在麦田里收割成熟的麦子，前景有麦穗。",))
    con.commit()
    con.close()
    return database, script


def _clip(identifier: str, order: int, kind: str = "selected_clip") -> dict:
    return {"candidate_id": identifier, "source_content_id": identifier, "source_file": identifier + ".mov",
            "source_absolute_path": "/tmp/" + identifier + ".mov", "source_start_seconds": "3600",
            "source_duration_seconds": "60", "source_frame_rate": "30000/1001", "has_audio": True,
            "start_ms": 2000, "end_ms": 8000, "beat_order": order, "beat_text": "设备运行。", "item_kind": kind}
