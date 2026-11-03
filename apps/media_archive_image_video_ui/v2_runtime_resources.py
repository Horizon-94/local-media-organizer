"""Execution-only pool policy; never rewrites frozen result contracts."""
DEFAULT_QWEN_WORKERS = 2
VALIDATED_QWEN_WORKERS = 2
# Conservative incremental envelopes above measured representative process
# footprints; not precise unified-memory predictions or result-contract fields.
AUXILIARY_MEMORY_MB={
    'yoloe':{'cold':3072,'warm':512},
    'openclip':{'cold':2048,'warm':512},
    'ocr':{'cold':3072,'warm':512},
    'embedding':{'cold':4096,'warm':512},
    'person':{'cold':2048,'warm':512},
}


def runtime_workers(task):
    if task.get('fixture_handlers'):return None
    policy=(task.get('runtime') or {}).get('model_execution') or {}
    if not isinstance(policy,dict) or set(policy)-{'qwen_workers'}:
        raise ValueError('model_execution_policy_invalid')
    count=policy.get('qwen_workers',DEFAULT_QWEN_WORKERS)
    if type(count) is not int or not 1<=count<=VALIDATED_QWEN_WORKERS:
        raise ValueError('qwen_execution_workers_not_validated')
    return {'qwen':count}
