"""Read-only V2 semantic retrieval using the indexed execution's model binding.

No legacy tables, document regeneration or query-vector persistence. Runtime
paths may relocate, but each selected namespace must match the frozen config
and provenance. Existing exact-vector ancestry checks remain authoritative.
"""
import json
import hashlib
import os
import signal
import threading
from pathlib import Path
from .v2_index import SQLiteExactVectors
from .v2_producer import fingerprint
_MODEL_HASHES={}
from . import query_trace as trace

def weights_match(cfg,proof):
    root=Path(cfg['model_path']).resolve()
    expected=proof.get('model_files',{}).get(str(root))
    if not expected:return False
    for relative,digest in expected.items():
        path=root/relative if root.is_dir() else root
        if not path.is_file():return False
        stat=path.stat();key=(str(path),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        if key not in _MODEL_HASHES:
            h=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
            if len(_MODEL_HASHES)>=512:_MODEL_HASHES.clear()
            _MODEL_HASHES[key]=h.hexdigest()
        if _MODEL_HASHES[key]!=digest:return False
    return True

from .build150_search_bindings import bindings

def encode(stage,cfg,query,python_path):
    from .v2_real_handlers import SubprocessModelHandler
    from .v2_resource_budget import CachedSystemMemory
    memory=CachedSystemMemory()()
    required=4096 if stage=='embedding' else 2048
    if (not memory.get('healthy') or memory.get('pressure_level',0)>1 or
            memory.get('available_mb',0)-required<3072):
        raise RuntimeError('semantic_search_memory_headroom_wait:请等待其它模型释放内存后重试')
    # Frozen cfg remains unchanged for provenance matching; query execution never inherits processing telemetry.
    query_cfg={k:v for k,v in cfg.items() if k not in {'_event_log','_inference_event_log','_worker_ready_ack'}}
    options={**query_cfg,'python_path':str(python_path),'response_timeout_seconds':60,'query_owner_pid':os.getpid()}
    if stage=='openclip':options['query_text_only']=True
    worker=SubprocessModelHandler(stage,options)
    previous=None
    if threading.current_thread() is threading.main_thread():
        previous=signal.getsignal(signal.SIGTERM)
        def cancelled(signum,frame):raise RuntimeError('semantic_search_cancelled')
        signal.signal(signal.SIGTERM,cancelled)
    try:return worker({'text':query})['vector']
    finally:
        try:worker.close()
        finally:
            if previous is not None:signal.signal(signal.SIGTERM,previous)

def _query_progress(stage,message,seconds=None):
    import sys
    print('SEARCH_PROGRESS_JSON='+json.dumps({'contract':'media_archive_search_progress_v1',
        'stage':stage,'stage_index':3 if 'encode' in stage else 4,'total_stages':7,
        'message':message,'detail':'正在使用当前已发布索引；未读取原素材',
        'phase_seconds':seconds},ensure_ascii=False),file=sys.stderr,flush=True)


# All modes share one tiny classification projection, never vectors or source text.
# It is valid only under the existing prepared-store identity/data-version token.
# A process-wide cap includes every library; closed or invalidated stores are pruned.
from collections import OrderedDict
import weakref,sys
_CATEGORY_CACHE=OrderedDict()
CATEGORY_CACHE_BYTES=16*1024*1024

def _categories_current(store):
    try:return bool(getattr(store,'reuse_preparation',False)) and store.preparation_current()
    except Exception:return False

def cached_categories(store,namespace,build):
    for key,entry in list(_CATEGORY_CACHE.items()):
        owner=entry['owner']()
        if owner is None or not _categories_current(owner) or entry['token']!=owner.token:_CATEGORY_CACHE.pop(key,None)
    ready=_categories_current(store)
    key=(id(store),namespace);token=getattr(store,'token',None)
    entry=_CATEGORY_CACHE.get(key)
    if ready and entry and entry['owner']() is store and entry['token']==token:
        _CATEGORY_CACHE.move_to_end(key);return entry['rows']
    _CATEGORY_CACHE.pop(key,None)
    rows=build()
    size=sys.getsizeof(rows)+sum(sys.getsizeof(k)+sys.getsizeof(v)+32 for k,v in rows.items())
    if ready and size<=CATEGORY_CACHE_BYTES and _categories_current(store) and store.token==token:
        while _CATEGORY_CACHE and sum(e['bytes'] for e in _CATEGORY_CACHE.values())+size>CATEGORY_CACHE_BYTES:
            _CATEGORY_CACHE.popitem(last=False)
        _CATEGORY_CACHE[key]={'owner':weakref.ref(store),'token':token,'rows':rows,'bytes':size}
    return rows

def candidate_categories(store,namespace):
    def build():
        from .search_evidence import EvidenceReader
        categories={}
        with store.connect() as c:
            reader=EvidenceReader(c)
            for document in c.execute("SELECT d.* FROM v2_searchable_documents d JOIN v2_vectors v ON v.entity_id=d.document_id JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref AND j.status='DONE' WHERE v.namespace=? AND NOT EXISTS(SELECT 1 FROM v2_active_sources a WHERE a.source_id=d.source_id AND a.source_revision<>d.source_revision)",(namespace,)):
                row=reader.classify(dict(document));reader.documents.clear()
                categories[row['document_id']]=row['evidence_category']
        return categories
    return cached_categories(store,namespace,build)

def retrieve(store,query,*,embedding_python,openclip_python,policy,encoder=None,query_cache=None,on_channel=None,search_intent="all"):
    encoder=encoder or encode
    with store.connect() as c:selected=bindings(c)
    scores={}; details={}; cache={} if query_cache is None else query_cache
    import time
    for stage,namespace,cfg,proof in selected:
        if search_intent=="text" and stage=="openclip":continue
        phase_started=time.monotonic()
        _query_progress(stage+'_contract','正在核对查询模型与索引契约')
        with trace.span(stage+'_weights_verify'):
            matched=weights_match(cfg,proof)
        if not matched:
            raise RuntimeError('semantic_search_index_model_contract_mismatch:模型与已有索引不匹配')
        key=(query,stage,fingerprint(cfg))
        if key not in cache:
            _query_progress(stage+'_encode','正在生成文本查询向量' if stage=='embedding' else '正在生成画面查询向量',time.monotonic()-phase_started)
            phase_started=time.monotonic()
            cache[key]=encoder(stage,cfg,query,embedding_python if stage=='embedding' else openclip_python)
        _query_progress(stage+'_retrieve','正在检索文本向量索引' if stage=='embedding' else '正在检索画面向量索引',time.monotonic()-phase_started)
        phase_started=time.monotonic()
        options={}
        if stage=='embedding':
            from .search_evidence import EvidenceReader,allowed
            with trace.span('candidate_category_projection'):
                categories=candidate_categories(store,namespace)
            groups={entity:('visual' if category in {'visual_description','visual_semantic'} else 'dialogue' if category=='dialogue' else 'ocr' if category=='ocr' else 'clue')
                    for entity,category in categories.items() if allowed({'evidence_category':category},search_intent)}
            # One exact-vector pass, at most 500 retained candidates in total.
            # Mode filtering precedes top-K; all-mode reserves independent families.
            options={'candidate_groups':groups}
            if search_intent=='all':options['group_limits']={'visual':225,'dialogue':225,'ocr':25,'clue':25}
        hits=SQLiteExactVectors(store).search(namespace,cache[key],limit=500,**options)
        _query_progress(stage+'_retrieved','文本向量检索完成' if stage=='embedding' else '画面向量检索完成',time.monotonic()-phase_started)
        weight=policy['text_rank_weight'] if stage=='embedding' else policy['visual_rank_weight']
        minimum=policy['minimum_text_semantic_cosine'] if stage=='embedding' else policy['minimum_visual_cosine']
        for rank,(entity,cosine) in enumerate(hits,1):
            if cosine<minimum:continue
            scores[entity]=scores.get(entity,0)+weight/(policy['rrf_k']+rank)
            details.setdefault(entity,[]).append({'type':'TEXT_SEMANTIC' if stage=='embedding' else 'VISUAL_SEMANTIC',
                'authority':'v2_current_vector','cosine':cosine,'namespace':namespace,'rank':rank,'contribution':weight/(policy['rrf_k']+rank)})
        if on_channel is not None:on_channel(stage,dict(scores),{k:list(v) for k,v in details.items()})
    return scores,details,bool(selected)
