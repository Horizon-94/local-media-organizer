#!/usr/bin/env python3
"""Build an isolated DB fixture that references a small video+image sample.

The fixture never copies or modifies media.  It records readonly source paths
from an inventory JSON produced by ``inventory.py`` so later Legacy/V2 runs
exercise the same source contract as a project without touching its database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from build_visual_fixture import SCHEMA, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.expanduser().resolve().read_text(encoding="utf-8"))
    sample = inventory.get("sample") or {}
    media = list(sample.get("videos") or []) + list(sample.get("images") or [])
    if not media:
        raise SystemExit("inventory_sample_empty")
    db = args.db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        raise SystemExit(f"refuse_overwrite_existing_fixture:{db}")
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA)
        for row in media:
            path = Path(str(row["path"])).expanduser().resolve()
            if not path.is_file():
                raise SystemExit(f"sample_source_missing:{path}")
            stat = path.stat()
            source_id = "fixture_src_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
            is_video = path.suffix.lower() in {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".braw"}
            media_type = "video" if is_video else "image"
            con.execute(
                "INSERT INTO source_assets(source_content_id,absolute_path,relative_path,file_name,extension,media_type,size_bytes,mtime,ctime,volume_id,online_status,is_deleted_or_missing) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (source_id, str(path), str(row.get("relative_path") or path.name), path.name, path.suffix.lower(), media_type, stat.st_size, int(stat.st_mtime), int(stat.st_ctime), "001DZLTEST_FIXTURE", 1),
            )
            file_id = "fixture_file_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
            con.execute(
                "INSERT INTO source_file_records(source_file_id,source_content_id,absolute_path,relative_path,source_root,file_name,extension,media_kind,support_status,size_bytes,mtime_ns,ctime_ns,dedup_role,next_action,canonical_source_file_id,folder_path,file_stem,stem_key,finder_tag_status,finder_tags_json,scan_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, source_id, str(path), str(row.get("relative_path") or path.name), str(path.parents[-1]), path.name, path.suffix.lower(), media_type, "supported", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, "canonical", "process", file_id, str(path.parent), path.stem, path.stem, "unread", "[]", "benchmark_fixture"),
            )
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {kind: con.execute("SELECT COUNT(*) FROM source_assets WHERE media_type=?", (kind,)).fetchone()[0] for kind in ("video", "image")}
    print(json.dumps({"db": str(db), "video_count": counts["video"], "image_count": counts["image"], "integrity": integrity}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
