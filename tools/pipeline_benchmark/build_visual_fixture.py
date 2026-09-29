#!/usr/bin/env python3
"""Create an isolated benchmark SQLite fixture from derived JPEG frames."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE source_assets (
 source_content_id TEXT PRIMARY KEY, absolute_path TEXT NOT NULL,
 relative_path TEXT NOT NULL, file_name TEXT NOT NULL, extension TEXT NOT NULL,
 media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime INTEGER NOT NULL,
 ctime INTEGER NOT NULL, volume_id TEXT NOT NULL DEFAULT 'LOCAL',
 online_status INTEGER DEFAULT 1, first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
 last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP, is_deleted_or_missing INTEGER DEFAULT 0
);
CREATE TABLE derived_assets (
 derived_id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL,
 derived_type TEXT NOT NULL, derived_path TEXT NOT NULL,
 frame_index INTEGER DEFAULT -1, time_position_ms INTEGER DEFAULT -1,
 width INTEGER, height INTEGER, sha256 TEXT,
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE visual_units (
 visual_unit_id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL,
 derived_id TEXT NOT NULL, visual_file TEXT NOT NULL,
 time_position_ms INTEGER NOT NULL DEFAULT -1, near_black INTEGER DEFAULT 0,
 luma_mean REAL, luma_std REAL, near_dup_group_id TEXT,
 is_near_dup_representative INTEGER DEFAULT 0
);
CREATE TABLE model_runs (
 run_id TEXT PRIMARY KEY, stage TEXT NOT NULL, model_name TEXT NOT NULL,
 model_path TEXT NOT NULL, script_version TEXT NOT NULL DEFAULT '',
 script_path TEXT NOT NULL, input_count INTEGER NOT NULL DEFAULT 0,
 output_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 started_at DATETIME DEFAULT CURRENT_TIMESTAMP, finished_at DATETIME,
 error_message TEXT
);
CREATE TABLE processing_errors (
 error_id TEXT PRIMARY KEY, stage TEXT, source_content_id TEXT,
 visual_unit_id TEXT, error_code TEXT, error_message TEXT,
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE source_file_records (
 source_file_id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL,
 absolute_path TEXT NOT NULL, relative_path TEXT NOT NULL,
 source_root TEXT NOT NULL, file_name TEXT NOT NULL, extension TEXT NOT NULL,
 media_kind TEXT NOT NULL, support_status TEXT NOT NULL, support_reason TEXT,
 size_bytes INTEGER, mtime_ns INTEGER, ctime_ns INTEGER, content_sha256 TEXT,
 dedup_role TEXT, next_action TEXT, canonical_source_file_id TEXT,
 folder_path TEXT, file_stem TEXT, stem_key TEXT, finder_tag_status TEXT,
 finder_tags_json TEXT, scan_run_id TEXT NOT NULL,
 updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE source_finder_tags (
 tag_id TEXT PRIMARY KEY, source_file_id TEXT NOT NULL,
 source_content_id TEXT NOT NULL, source_path TEXT NOT NULL,
 tag_raw TEXT NOT NULL, tag_name TEXT, tag_color TEXT,
 scan_run_id TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE embeddings (
 embedding_id TEXT PRIMARY KEY, visual_unit_id TEXT NOT NULL,
 source_content_id TEXT NOT NULL, model_name TEXT NOT NULL,
 model_path TEXT NOT NULL, dimension INTEGER NOT NULL DEFAULT 512,
 vector_key TEXT NOT NULL, run_id TEXT NOT NULL,
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--source-video",
        type=Path,
        default=None,
        help="Optional readonly original video represented by the derived frames.",
    )
    args = parser.parse_args()
    frames = sorted(args.frames.expanduser().resolve().glob("*.jpg"))[: args.limit]
    if not frames:
        raise SystemExit("no_benchmark_frames")
    db = args.db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        raise SystemExit(f"refuse_overwrite_existing_fixture:{db}")
    source_video = args.source_video.expanduser().resolve() if args.source_video else None
    if source_video is not None and not source_video.is_file():
        raise SystemExit(f"source_video_not_found:{source_video}")
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA)
        for index, frame in enumerate(frames):
            source_path = source_video or frame
            source_id = "bench_src_" + digest(str(source_path))
            derived_id = "bench_derived_" + digest(str(frame))
            visual_id = "bench_visual_" + digest(str(frame))
            stat = frame.stat()
            con.execute(
                "INSERT OR IGNORE INTO source_assets VALUES(?,?,?,?,?,?,?,?,?,'BENCHMARK',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0)",
                (source_id, str(source_path), "anonymous/video", "anonymous.mov", ".mov", "video", source_path.stat().st_size, int(source_path.stat().st_mtime), int(source_path.stat().st_ctime)),
            )
            source_file_id = "bench_file_" + digest(str(source_path))
            con.execute(
                "INSERT OR IGNORE INTO source_file_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (source_file_id, source_id, str(source_path), "anonymous/video", str(source_path.parent), "anonymous.mov", ".mov", "video", "supported", "", source_path.stat().st_size, source_path.stat().st_mtime_ns, source_path.stat().st_ctime_ns, digest(str(source_path)), "canonical", "process", source_file_id, str(source_path.parent), "anonymous", "anonymous", "unread", "[]", "benchmark_scan"),
            )
            con.execute(
                "INSERT INTO derived_assets(derived_id,source_content_id,derived_type,derived_path,frame_index,time_position_ms,width,height,sha256) VALUES(?,?,?,?,?,?,?,?,?)",
                (derived_id, source_id, "video_frame", str(frame), index, index * 3000, 720, 1280, digest(f"{stat.st_size}:{stat.st_mtime_ns}")),
            )
            con.execute(
                "INSERT INTO visual_units(visual_unit_id,source_content_id,derived_id,visual_file,time_position_ms) VALUES(?,?,?,?,?)",
                (visual_id, source_id, derived_id, str(frame), index * 3000),
            )
        con.commit()
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"db={db} visual_units={len(frames)} integrity={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
