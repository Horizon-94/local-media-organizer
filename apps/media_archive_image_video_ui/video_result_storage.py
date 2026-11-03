"""Lossless opt-in video persistence projection; legacy values stay readable.

Model output is not changed. Registry dictionaries and duplicate OpenCLIP
vectors are reconstructed exactly, with SHA verification before consumption.
"""
import json
from .v2_producer import fingerprint
from .v2_runtime import ContractError

CONTRACT='video-model-value-storage-v1'


def registry_semantic_sha(value):
    # Timestamps describe worker initialization, not class/search semantics.
    # Old immutable bodies remain untouched and continue to hydrate exactly.
    return fingerprint({'registry':value['registry'],'label_terms':[
        {k:v for k,v in term.items() if k not in ('created_at','updated_at')}
        for term in value['label_terms']]})


def compact(c,stage,value):
    if stage=='yoloe' and 'registry' in value and 'label_terms' in value:
        blob={'registry':value['registry'],'label_terms':value['label_terms']}
        key=fingerprint(blob)
        c.execute('CREATE TABLE IF NOT EXISTS video_shared_registries_v1(revision TEXT PRIMARY KEY,payload TEXT)')
        c.execute('INSERT OR IGNORE INTO video_shared_registries_v1 VALUES(?,?)',(key,json.dumps(blob,sort_keys=True)))
        existing=json.loads(c.execute('SELECT payload FROM video_shared_registries_v1 WHERE revision=?',(key,)).fetchone()[0])
        if fingerprint(existing)!=key:raise ContractError('shared_registry_digest_mismatch')
        result={k:v for k,v in value.items() if k not in ('registry','label_terms')}
        return {**result,'registry_revision_id':key,'storage_contract':CONTRACT,'model_output_sha':fingerprint(value)}
    if stage=='openclip' and isinstance(value.get('raw'),dict) and 'embedding_vector' in value['raw']:
        if value['vector']!=value['raw']['embedding_vector']:raise ContractError('duplicate_vector_not_equivalent')
        raw={k:v for k,v in value['raw'].items() if k!='embedding_vector'}
        return {**value,'raw':raw,'storage_contract':CONTRACT,'model_output_sha':fingerprint(value)}
    return value


def restore(c,value):
    if value.get('storage_contract')!=CONTRACT:return value
    result={k:v for k,v in value.items() if k not in ('storage_contract','model_output_sha','registry_revision_id')}
    if 'registry_revision_id' in value:
        row=c.execute('SELECT payload FROM video_shared_registries_v1 WHERE revision=?',(value['registry_revision_id'],)).fetchone()
        if not row:raise ContractError('shared_registry_missing')
        blob=json.loads(row[0])
        if fingerprint(blob)!=value['registry_revision_id']:raise ContractError('shared_registry_digest_mismatch')
        result.update(blob)
    elif 'vector' in value and isinstance(value.get('raw'),dict):
        result['raw']={**value['raw'],'embedding_vector':value['vector']}
    if fingerprint(result)!=value['model_output_sha']:raise ContractError('restored_model_output_digest_mismatch')
    return result
