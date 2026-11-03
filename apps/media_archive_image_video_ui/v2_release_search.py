"""Read-only release publication fences and explainable speech intent retrieval.

No model invocation, second database or write transaction.
"""
import json,re

def enabled(c):return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='release_search_heads' AND type='table'").fetchone())

def predicate(c,source,revision):
    if not enabled(c):return ''
    return f''' AND EXISTS(SELECT 1 FROM release_search_heads ph JOIN release_registry pr
        ON pr.source_id=ph.source_id AND pr.revision=ph.revision AND pr.present=1 AND pr.committed=1
        WHERE ph.visible=1 AND ph.source_id={source} AND ph.revision={revision})'''

def filter_rows(c,rows):
    if not enabled(c):return rows
    ids=sorted({r['source_id'] for r in rows});visible=set()
    for n in range(0,len(ids),500):
        group=ids[n:n+500];marks=','.join('?' for _ in group)
        visible.update(tuple(r) for r in c.execute(f'''SELECT h.source_id,h.revision FROM release_search_heads h
            JOIN release_registry p ON p.source_id=h.source_id AND p.revision=h.revision AND p.present=1 AND p.committed=1
            WHERE h.visible=1 AND h.source_id IN ({marks})''',group))
    return [r for r in rows if (r['source_id'],r.get('source_revision',r.get('revision'))) in visible]

def intent(query):
    pattern=r'有人声|说话|讲话|\bspeaking\b|\bspeech\b'
    return bool(re.search(pattern,query,re.I)),re.sub(pattern,' ',query,flags=re.I).strip()

def search(c,query,limit=50):
    if not enabled(c):return []
    speech,visual=intent(query);hits=None
    if visual:
        from .v2_index import fts_query
        hits={r[0] for r in c.execute('SELECT document_id FROM v2_text_fts WHERE v2_text_fts MATCH ?',(fts_query(visual),))}
    rows=c.execute('''SELECT s.*,r.relative_path,r.row_json FROM release_search_segments s
      JOIN release_search_heads h ON h.source_id=s.source_id AND h.revision=s.revision AND h.generation=s.generation AND h.visible=1
      JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision AND r.present=1 AND r.committed=1
      JOIN v2_active_sources a ON a.source_id=h.source_id AND a.source_revision=h.revision
      WHERE (?=0 OR s.speech_present=1) ORDER BY s.source_id,s.start_ms,s.segment_id''',(speech,))
    result=[]
    for row in rows:
        value=json.loads(row['evidence'])
        if hits is not None and not hits.intersection(json.loads(row['documents'])):
            # Covered IMAGE uses a frozen representative fact, not the current
            # revision of that other file. Changing/deleting a representative
            # must not revoke an unchanged member's own committed evidence.
            snapshot=value.get('coverage_visual_text') or ''
            terms=re.findall(r'[\w]+',visual.lower())
            if not value.get('visual_coverage_proxy') or not terms or not all(t in snapshot.lower() for t in terms):continue
        value.update(segment_id=row['segment_id'],source_revision=row['revision'],
             artifact_id=row['artifact_id'],
             matched_intent='VAD_SPEECH_FILTER' if speech else 'VISUAL_OR_CONFIDENT_TEXT',
             score=1.0 if speech else 0.0)
        result.append(value)
        if len(result)>=limit:break
    return result
