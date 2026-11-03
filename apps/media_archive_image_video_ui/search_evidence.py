"""Read-only, request-scoped interpretation of persisted search evidence.

No inferred frame IDs, original-media probes, model calls or durable writes.
"""
from __future__ import annotations
import json

MODES = ('visual', 'text', 'all')
TEXT_TYPES = {'audio', 'whisper', 'transcript', 'speech_evidence', 'ocr'}


def mode(value):
    value = value or 'visual'
    if value not in MODES:
        raise ValueError('search_intent_invalid')
    return value


def visual_description(text):
    """Exclude explicitly editorial/topic sections, retain literal scene description."""
    text = str(text or '')
    for marker in ('【检索】', '【剪辑】', '可承接主题/对白语义：', '可能用途：'):
        text = text.split(marker, 1)[0]
    return text.strip()


class EvidenceReader:
    def __init__(self, connection):
        self.connection = connection
        self.documents = {}

    def legacy_document(self, row):
        key = row['document_id']
        if key not in self.documents:
            provenance = json.loads(row.get('provenance') or '{}')
            value = provenance.get('legacy_document', {})
            if isinstance(value, dict) and set(value) == {'$canonical_result'}:
                from .video_canonical_store import load_result
                value = load_result(self.connection, value['$canonical_result'])
            if not isinstance(value, dict):
                raise ValueError('search_visual_provenance_invalid')
            # A different source/frame must never acquire this row's preview.
            for field, expected in (('source_content_id', row['source_id']),
                                    ('derived_id', row.get('artifact_id'))):
                if value.get(field) is not None and value[field] != expected:
                    raise ValueError('search_visual_provenance_identity_mismatch')
            self.documents[key] = value
        return self.documents[key]

    def classify(self, row):
        kind = row.get('document_type')
        aid = row.get('artifact_id')
        result = dict(row)
        result['evidence_document_id'] = row['document_id']
        result['evidence_frame_id'] = aid
        if kind == 'source_filename':
            category, text = 'filename', row.get('text', '')
        elif kind in TEXT_TYPES:
            category = 'ocr' if kind == 'ocr' else 'dialogue'
            text = row.get('text', '')
        elif kind == 'visual' and aid:
            category, text = 'visual_semantic', ''
        elif kind == 'qwen' and aid:
            category, text = 'visual_description', visual_description(row.get('text'))
        elif kind == 'final_visual' and aid:
            proof = self.legacy_document(row)
            direct = visual_description(proof.get('qwen_text'))
            result['evidence_ocr_text'] = str(proof.get('ocr_text') or '') if proof.get('direct_ocr_count',0) else ''
            if proof.get('direct_qwen_count', 0) and direct:
                category, text = 'visual_description', direct
            elif proof.get('direct_ocr_count', 0) and proof.get('ocr_text'):
                category, text = 'ocr', proof['ocr_text']
            else:
                # The stored target is not the supporting frame. Preserve the
                # clue, but do not silently make it a direct visual hit.
                category, text = 'neighbor_clue', row.get('text', '')
        else:
            category, text = 'other_clue', row.get('text', '')
        result.update(evidence_category=category, evidence_text=text,
                      evidence_is_direct=category in {'visual_description', 'visual_semantic', 'dialogue', 'ocr'})
        return result


def allowed(row, intent):
    intent = mode(intent)
    category = row['evidence_category']
    if category == 'filename':
        return True  # Explicitly labelled metadata shortcut, never visual proof.
    if intent == 'all':
        return True
    if intent == 'visual':
        return category in {'visual_description', 'visual_semantic', 'expanded_visual_clue'}
    return category in {'dialogue', 'ocr'}


def direct_lexical_rows(rows, query, intent):
    """Recheck rewritten evidence with the original tokenizer/MATCH semantics.

    A lexical match in removed editorial or propagated text is not a match in
    this frame's direct description. Semantic retrieval remains independent.
    Uses a request-local in-memory FTS table, never changes the project index.
    """
    mode(intent)
    candidates = [(i, row) for i, row in enumerate(rows)
                  if row['evidence_category'] == 'visual_description']
    if not candidates:
        return rows
    import sqlite3
    from .v2_index import fts_tokens, fts_query
    con = sqlite3.connect(':memory:')
    try:
        con.execute('CREATE VIRTUAL TABLE direct_evidence USING fts5(text)')
        con.executemany('INSERT INTO direct_evidence(rowid,text) VALUES (?,?)',
                        ((2*i+1, fts_tokens(row['evidence_text'])) for i,row in candidates))
        con.executemany('INSERT INTO direct_evidence(rowid,text) VALUES (?,?)',
                        ((2*i+2, fts_tokens(row['evidence_ocr_text'])) for i,row in candidates if row.get('evidence_ocr_text')))
        matched = {r[0] for r in con.execute(
            'SELECT rowid FROM direct_evidence WHERE direct_evidence MATCH ?', (fts_query(query),))}
        result=[]
        for i,row in enumerate(rows):
            item=dict(row)
            if row['evidence_category']=='visual_description' and 2*i+1 not in matched:
                if 2*i+2 in matched:
                    item.update(evidence_category='ocr', evidence_is_direct=True, evidence_text=row['evidence_ocr_text'])
                else:
                    item.update(evidence_category='expanded_visual_clue', evidence_is_direct=False,
                        evidence_text=row.get('text',''), evidence_direct_description=row.get('evidence_text',''))
            result.append(item)
        return result
    finally:
        con.close()
