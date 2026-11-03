"""Lossless OCR consumer projection; never rewrite authoritative DONE evidence.

The result ledger retains input identity/model/provenance. This module changes
only the OCR value, not inference, recognition geometry or confidence.
"""
from __future__ import annotations
import hashlib
import json

CONTRACT = 'video-ocr-text-lines-v1'


def compact_result(result):
    if not isinstance(result, dict) or not isinstance(result.get('text'), str):
        raise ValueError('ocr_text_missing')
    lines = result.get('lines')
    if not isinstance(lines, list):
        raise ValueError('ocr_lines_missing')
    # Preserve ALL extracted-line fields, including legacy box/score spellings.
    # It is the prediction envelope (image tensors/fonts), not evidence lines,
    # which must be excluded. Do not silently drop unknown consumer metadata.
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get('text'), str):
            raise ValueError('ocr_line_invalid')
    projected = {k: v for k, v in result.items() if k not in ('raw', 'raw_result')}
    projected.update(contract=CONTRACT)
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return json.loads(encoded)


def initialize_projection(connection):
    connection.executescript('''
      CREATE TABLE IF NOT EXISTS video_ocr_compact_projection_v1(
        execution_key TEXT NOT NULL, original_result_sha TEXT NOT NULL,
        compact_json TEXT NOT NULL, compact_sha TEXT NOT NULL,
        PRIMARY KEY(execution_key, original_result_sha));
      CREATE TABLE IF NOT EXISTS video_ocr_compact_checkpoint_v1(
        migration TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL);
    ''')


def project_done_batch(connection, limit=16):
    """Caller-owned short transaction, bounded by rows; restart-safe cursor.

    New projection is keyed by immutable evidence SHA. Original result_json,
    result_sha, attempts and status are never updated; no VACUUM or inference.
    A consumer must still prove current ownership before using a projection.
    """
    if not connection.in_transaction:
        raise ValueError('caller_transaction_required')
    if not 1 <= limit <= 64:
        raise ValueError('bounded_batch_required')
    cursor = connection.execute('SELECT last_rowid FROM video_ocr_compact_checkpoint_v1 WHERE migration=?', (CONTRACT,)).fetchone()
    rows = connection.execute('''SELECT rowid,execution_key,result_sha,result_json
        FROM v2_jobs WHERE rowid>? AND stage='ocr' AND status='DONE'
        ORDER BY rowid LIMIT ?''', (cursor[0] if cursor else 0, limit)).fetchall()
    for rowid, key, original_sha, payload in rows:
        compact = compact_result(json.loads(payload))
        encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = connection.execute('SELECT compact_sha FROM video_ocr_compact_projection_v1 WHERE execution_key=? AND original_result_sha=?', (key, original_sha)).fetchone()
        if existing and existing[0] != digest:
            raise ValueError('compact_projection_identity_conflict')
        connection.execute('INSERT OR IGNORE INTO video_ocr_compact_projection_v1 VALUES(?,?,?,?)', (key, original_sha, encoded, digest))
        connection.execute('INSERT INTO video_ocr_compact_checkpoint_v1 VALUES(?,?) ON CONFLICT(migration) DO UPDATE SET last_rowid=excluded.last_rowid', (CONTRACT, rowid))
    return len(rows)
