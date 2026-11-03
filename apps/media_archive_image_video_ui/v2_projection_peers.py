"""Exact frozen duplicate-text equivalence, not project-wide wakeup fanout."""
import json
import collections
import threading
from functools import lru_cache


@lru_cache(maxsize=1)
def quality():
    from .v2_projection import legacy
    return legacy('quality')


def text_key(stage,result):
    text=result['text'] if 'text' in result else json.dumps(result,ensure_ascii=False,sort_keys=True)
    frozen=quality()
    return stage,frozen.sha256_text(frozen.normalize_text(str(text)))


_text_cache = collections.OrderedDict()
_text_cache_lock = threading.RLock()
_text_cache_bytes = 0
_TEXT_CACHE_LIMIT = 8 * 1024 * 1024


def compact_peers(c, stage=None):
    """Exact duplicate text only; own raw quality evidence remains untouched.

    DONE result digests are immutable. The bounded digest cache only avoids
    reparsing OCR pixel arrays; current lineage is always queried afresh.
    No schema writes, generation triggers, or full-DAG cache are needed.
    """
    global _text_cache_bytes
    condition = 'stage=?' if stage else "stage IN ('qwen','ocr')"
    peers = [dict(r) for r in c.execute('SELECT execution_key,stage,item_id,source_id,source_revision,result_sha '
        "FROM v2_current_jobs WHERE status='DONE' AND " + condition + ' ORDER BY execution_key', (stage,) if stage else ())]
    for peer in peers:
        key = (peer['stage'], peer['result_sha'])
        with _text_cache_lock:
            hit = _text_cache.get(key)
            if hit is not None:
                _text_cache.move_to_end(key)
        if hit is None:
            value = c.execute("SELECT json_extract(result_json,'$.text'),json_type(result_json,'$.text') FROM v2_jobs WHERE execution_key=? AND result_sha=?",
                              (peer['execution_key'], peer['result_sha'])).fetchone()
            if not value:
                from .v2_runtime import ContractError
                raise ContractError('peer_result_digest_changed')
            if value[1] is None:
                raw = c.execute('SELECT result_json FROM v2_jobs WHERE execution_key=?', (peer['execution_key'],)).fetchone()[0]
                decoded=json.loads(raw)
                # VIDEO canonical references are hydrated by its scoped
                # reader. SQL json_extract sees the physical ref, not text.
                text=decoded['text'] if 'text' in decoded else json.dumps(decoded,ensure_ascii=False,sort_keys=True)
            else:
                text = value[0]
            hit = json.dumps({'text': text}, ensure_ascii=False, sort_keys=True)
            size = len(hit.encode('utf-8'))
            if size <= _TEXT_CACHE_LIMIT and peer['result_sha']:
                with _text_cache_lock:
                    if key not in _text_cache:
                        while _text_cache and (_text_cache_bytes + size > _TEXT_CACHE_LIMIT or len(_text_cache) >= 4096):
                            _, previous = _text_cache.popitem(last=False)
                            _text_cache_bytes -= len(previous.encode('utf-8'))
                        _text_cache[key] = hit
                        _text_cache_bytes += size
        peer['result_json'] = hit
        peer['peer_context_contract'] = 'exact_duplicate_text_only_v1'
    return peers


def relevant_peers(peers,own_jobs):
    keys={text_key(j['stage'],json.loads(j['result_json'])) for j in own_jobs if j['stage'] in ('qwen','ocr')}
    return [j for j in peers if text_key(j['stage'],json.loads(j['result_json'])) in keys]


def invalidate_affected_heads(c,job,result):
    from .v2_lineage import invalidate
    peers=compact_peers(c, job['stage'])
    key=text_key(job['stage'],result)
    affected={(job['source_id'],job['source_revision'])}
    affected.update((j['source_id'],j['source_revision']) for j in peers
                    if text_key(j['stage'],json.loads(j['result_json']))==key)
    heads=[r['execution_key'] for r in c.execute('SELECT * FROM v2_final_projection_heads')
           if (r['source_id'],r['source_revision']) in affected]
    return invalidate(c,heads)


def complete_with_targeted_projection(store,job,result,validator,event_type='EVIDENCE_READY',transaction_hook=None):
    """Same result/lease/epoch transaction; narrower quality invalidation only.

The frozen completion implementation remains unchanged. This product adapter
retains every validation and idempotency gate, and substitutes only the set of
projection roots affected by the frozen exact-normalized-text duplicate rule.
"""
    from .v2_runtime import ContractError,LostLease,SupersededCompletion
    from .v2_producer import fingerprint
    store.assert_job_epoch(job);validator(result)
    raw=json.dumps(result,sort_keys=True,allow_nan=False);sha=fingerprint(result)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone()
        if not c.execute('SELECT 1 FROM v2_current_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone():
            raise SupersededCompletion('completion_lineage_inactive')
        if row['provenance']!=job['provenance']:raise ContractError('claimed_contract_changed')
        if row['status']=='DONE' and row['token']==job['token']:
            if row['result_sha']!=sha:raise ContractError('completion_digest_conflict')
            return
        if row['status']!='RUNNING' or row['token']!=job['token'] or row['lease_until']<=store.clock():
            raise LostLease('completion_without_current_ownership')
        c.execute("UPDATE v2_jobs SET status='DONE',result_json=?,result_sha=?,lease_until=NULL WHERE execution_key=?",(raw,sha,job['execution_key']))
        store.candidate_status(c,job['stage'],job['item_id'],'DONE')
        if job['stage'] in ('qwen','ocr') and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_final_projection_heads'").fetchone():
            invalidate_affected_heads(c,job,result)
        payload={'execution_key':job['execution_key'],'stage':job['stage'],'item_id':job['item_id'],
                 'evidence_revision':job['revision'],'input_sha':job['input_sha'],
                 'provenance':json.loads(job['provenance']),'payload_sha':sha,'result':result}
        if transaction_hook:transaction_hook(c,job,result)
        store.emit(c,event_type,job['source_id'],job['source_revision'],job['revision'],payload,
                   producer_id=job['stage'],contract_hash=fingerprint(json.loads(job['provenance'])))
