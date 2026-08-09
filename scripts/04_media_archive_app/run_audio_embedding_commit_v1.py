#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import struct
from pathlib import Path


CONTRACT = "media_archive_audio_embedding_commit_v1"
RUNTIME_CONTRACT = "media_archive_stage_runtime_contract_v1"


def progress(completed: int, total: int, current: str = "") -> None:
    print(json.dumps({
        "contract": RUNTIME_CONTRACT,
        "event": "stage_progress",
        "completed": completed,
        "total": total,
        "success": completed,
        "failed": 0,
        "remaining": max(0, total - completed),
        "current_item": current,
        "model_workers": 1,
        "actual_workers": 1,
    }, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_central_db_write:
        raise RuntimeError("audio_embedding_central_db_write_not_confirmed")
    if not 1 <= args.batch_size <= 256:
        raise ValueError("audio_embedding_batch_size_invalid")
    db = args.db.resolve(strict=True)
    model_path = args.model.resolve(strict=True)
    os.environ.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
    })
    with sqlite3.connect(db, timeout=30.0) as con:
        rows = con.execute(
            """SELECT e.evidence_id,e.transcript_text
               FROM audio_speech_evidence e
               LEFT JOIN audio_text_embeddings v USING(evidence_id)
               WHERE v.evidence_id IS NULL OR v.status<>'success'
               ORDER BY e.evidence_id"""
        ).fetchall()
    total = len(rows)
    if not rows:
        progress(0, 0)
        print(json.dumps({"contract": CONTRACT, "status": "PASS", "embedded": 0}))
        return 0

    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(
        str(model_path), device=device, local_files_only=True, trust_remote_code=False
    )
    completed = 0
    for offset in range(0, total, args.batch_size):
        batch = rows[offset:offset + args.batch_size]
        vectors = model.encode(
            [str(row[1]) for row in batch],
            batch_size=len(batch), show_progress_bar=False, precision="float32",
            convert_to_numpy=True, normalize_embeddings=True,
        ).astype("float32", copy=False)
        with sqlite3.connect(db, timeout=30.0) as con:
            con.execute("PRAGMA foreign_keys=ON")
            for (evidence_id, _text), vector in zip(batch, vectors):
                blob = struct.pack(f"<{len(vector)}f", *map(float, vector))
                con.execute(
                    """INSERT OR REPLACE INTO audio_text_embeddings(
                           evidence_id,model_name,model_path,dimension,vector_dtype,
                           normalized,vector_blob,vector_sha256,status
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id, model_path.name, str(model_path), len(vector),
                        "float32", 1, blob, hashlib.sha256(blob).hexdigest(), "success",
                    ),
                )
            con.commit()
        completed += len(batch)
        progress(completed, total, str(batch[-1][0]))
    print(json.dumps({
        "contract": CONTRACT, "status": "PASS", "embedded": completed,
        "dimension": int(vectors.shape[1]), "device": device,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
