#!/usr/bin/env python3
"""Read macOS Finder tags for existing image rows and append them to SQLite.

Original files are metadata-read only. Existing tag evidence is never deleted;
newly observed tags are inserted idempotently so an old completed library can
be supplemented without rebuilding its frozen V25 snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


CONTRACT_VERSION = "refresh_existing_library_finder_tags_v1"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_tag(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip().strip('"')
    parts = [part.strip() for part in re.split(r"\\n|\n", value) if part.strip()]
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), parts[-1]
    return value, ""


def read_tags(path: Path) -> tuple[list[dict[str, str]], str]:
    completed = subprocess.run(
        ["/usr/bin/mdls", "-raw", "-name", "kMDItemUserTags", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    raw = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return [], "mdls_error:" + (completed.stderr or "")[:160]
    if raw in {"", "(null)", "null"}:
        return [], "none"
    tags: list[dict[str, str]] = []
    for part in raw.replace("(", "").replace(")", "").replace(",", "\n").splitlines():
        raw_tag = part.strip().strip('"')
        if not raw_tag:
            continue
        name, color = parse_tag(raw_tag)
        tags.append({"tag_raw": raw_tag, "tag_name": name, "tag_color": color})
    return tags, "ok"


def refresh(db: Path) -> dict[str, Any]:
    con = sqlite3.connect(str(db), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    rows = [dict(row) for row in con.execute(
        """SELECT s.source_content_id,s.absolute_path,
                  COALESCE((SELECT f.source_file_id FROM source_file_records f
                            WHERE f.source_content_id=s.source_content_id
                            ORDER BY CASE f.dedup_role WHEN 'canonical' THEN 0 ELSE 1 END,
                                     f.source_file_id LIMIT 1),s.source_content_id) AS source_file_id
           FROM source_assets s
           WHERE s.media_type='image' AND COALESCE(s.is_deleted_or_missing,0)=0
           ORDER BY s.relative_path,s.source_content_id"""
    )]
    run_id = "finder_tag_refresh_" + sha256_text(
        "\n".join(str(row["source_content_id"]) for row in rows)
    )[:20]
    tagged_sources = 0
    observed_tags = 0
    inserted_tags = 0
    missing_sources = 0
    errors = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """CREATE TABLE IF NOT EXISTS source_finder_tags(
               tag_id TEXT PRIMARY KEY,source_file_id TEXT NOT NULL,
               source_content_id TEXT NOT NULL,source_path TEXT NOT NULL,
               tag_raw TEXT NOT NULL,tag_name TEXT,tag_color TEXT,
               scan_run_id TEXT NOT NULL,created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        con.commit()
        for row in rows:
            path = Path(str(row["absolute_path"])).expanduser()
            if not path.is_file():
                missing_sources += 1
                continue
            tags, status = read_tags(path)
            if status.startswith("mdls_error"):
                errors += 1
            if tags:
                tagged_sources += 1
            observed_tags += len(tags)
            con.execute("BEGIN IMMEDIATE")
            if any(str(info[1]) == "finder_tag_status" for info in con.execute("PRAGMA table_info(source_file_records)")):
                con.execute(
                    """UPDATE source_file_records
                       SET finder_tag_status=?,finder_tags_json=?,updated_at=CURRENT_TIMESTAMP
                       WHERE source_content_id=?""",
                    (status, json.dumps(tags, ensure_ascii=False), row["source_content_id"]),
                )
            for tag in tags:
                tag_id = "tag_" + sha256_text(
                    f"finder_tag_v1|{row['source_file_id']}|{tag['tag_raw']}"
                )[:24]
                cursor = con.execute(
                    """INSERT OR IGNORE INTO source_finder_tags(
                       tag_id,source_file_id,source_content_id,source_path,tag_raw,
                       tag_name,tag_color,scan_run_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        tag_id, row["source_file_id"], row["source_content_id"], str(path),
                        tag["tag_raw"], tag["tag_name"], tag["tag_color"], run_id,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                inserted_tags += int(cursor.rowcount == 1)
            con.commit()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    status = "PASS" if integrity == "ok" and foreign_keys == 0 else "FAIL"
    return {
        "status": status, "technical_status": status,
        "contract_version": CONTRACT_VERSION, "run_id": run_id,
        "image_source_count": len(rows), "tagged_source_count": tagged_sources,
        "observed_tag_count": observed_tags, "inserted_tag_count": inserted_tags,
        "missing_source_count": missing_sources, "metadata_read_error_count": errors,
        "database_integrity_check": integrity, "foreign_key_error_count": foreign_keys,
        "database_write": True, "model_run": False, "network_used": False,
        "original_media_read": "finder_metadata_only", "original_media_write": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_central_db_write:
        raise RuntimeError("finder_tag_refresh_requires_confirmation")
    if not Path("/usr/bin/mdls").is_file():
        raise RuntimeError("finder_tag_refresh_requires_macos_mdls")
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = refresh(args.db.expanduser().resolve(strict=True))
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
