#!/usr/bin/env python3
"""Prepare a derived-only Qwen preflight fixture without touching formal data.

The input DB is copied through SQLite's backup API into a fresh benchmark
directory. Only the five benchmark queue rows are updated in the copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_db(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close(); src.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db", type=Path, required=True)
    ap.add_argument("--source-derived-dir", type=Path, required=True)
    ap.add_argument("--target-root", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    args = ap.parse_args()
    if not args.source_db.is_file():
        raise SystemExit(f"source_db_missing:{args.source_db}")
    frames = sorted(args.source_derived_dir.rglob("*.jpg"))[:5]
    if len(frames) != 5:
        raise SystemExit(f"derived_frame_count:{len(frames)}")
    args.target_root.mkdir(parents=True, exist_ok=True)
    derived = args.target_root / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for index, source in enumerate(frames, start=1):
        target = derived / f"qwen_candidate_{index:02d}.jpg"
        shutil.copy2(source, target)
        copied.append(target)
    db = args.target_root / "qwen_fixture.sqlite"
    backup_db(args.source_db, db)
    config_target = args.target_root / "qwen-config.json"
    shutil.copy2(args.config, config_target)
    hashes = [sha256(path) for path in copied]
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        # The production snapshot is intentionally immutable. This is a fresh
        # benchmark copy, so temporarily remove only the copied guard triggers,
        # update the five runtime paths, then restore the exact trigger SQL.
        trigger_rows = con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ("
            "'trg_stop03_2_frozen_v25_no_update','trg_stop03_2_frozen_v25_no_delete',"
            "'trg_stop03_2_frozen_v25_no_insert_after_lock')"
        ).fetchall()
        for trigger_name, _ in trigger_rows:
            con.execute(f'DROP TRIGGER "{trigger_name}"')
        rows = con.execute("SELECT candidate_id FROM stop03_2_candidate_queue_frozen_v25 ORDER BY candidate_id").fetchall()
        if len(rows) != 5:
            raise SystemExit(f"candidate_count:{len(rows)}")
        for row, path, digest in zip(rows, copied, hashes):
            con.execute(
                "UPDATE stop03_2_candidate_queue_frozen_v25 SET runtime_visual_file=?, runtime_visual_file_sha256=?, size_bytes=?, mtime_ns=? WHERE candidate_id=?",
                (str(path.resolve()), digest, path.stat().st_size, path.stat().st_mtime_ns, str(row["candidate_id"])),
            )
        for _, trigger_sql in trigger_rows:
            con.execute(trigger_sql)
        con.commit()
        con.execute("PRAGMA foreign_keys=ON")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        queue = [dict(r) for r in con.execute("SELECT candidate_id,runtime_visual_file,runtime_visual_file_sha256 FROM v_stop03_2_v25_qwenvl_execution_queue ORDER BY candidate_id")]
    finally:
        con.close()
    report = {
        "status": "PASS" if integrity == "ok" and not foreign and len(queue) == 5 else "FAIL",
        "database": str(db), "config": str(config_target),
        "derived_files": [{"path": str(p), "sha256": h, "exists": p.is_file()} for p, h in zip(copied, hashes)],
        "integrity_check": integrity, "foreign_key_check": foreign,
        "queue": queue, "candidate_count": len(queue), "model_run": False,
    }
    (args.target_root / "fixture-preparation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
