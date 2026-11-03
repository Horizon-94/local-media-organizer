"""Independent visual proof; audio changes never revoke this proof."""
import json


def invalidate_visual(c, source, revision, stage=None, payload=None):
    if stage in ('audio_extract', 'vad', 'whisper'):
        return
    if stage == 'embedding':
        payload = json.loads(payload) if isinstance(payload, str) else payload or {}
        if payload.get('document_type') == 'whisper':
            return
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_visual_source_closure'").fetchone():
        c.execute("UPDATE v2_visual_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?", (source, revision))
