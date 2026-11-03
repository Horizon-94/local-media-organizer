"""New video path delegates content validation to the approved RAW_MEDIA code.

Never imports a legacy three-section parser or synthesizes missing fields.
"""
import importlib.util
from pathlib import Path


def approved_schema():
    path=Path(__file__).resolve().parents[2]/'scripts/03_stop03_visual_analysis/qwenvl_raw_media_v3.py'
    spec=importlib.util.spec_from_file_location('video_approved_raw_media_contract',path)
    schema=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    return schema


def contract_revision():
    return approved_schema().CONTRACT_REVISION


def validate(result):
    schema=approved_schema()
    if result.get('qwen_schema_version')!=schema.SCHEMA_VERSION:
        raise ValueError('current_qwen_schema_required')
    text=result.get('text')
    if not isinstance(text,str):raise ValueError('qwen_text_missing')
    values,missing,issues=schema.validate(text)
    if missing or issues:
        raise ValueError('qwen_schema_invalid:'+ '|'.join(missing+issues))
    expected={key+'：':values[key] for key in schema.FIELDS}
    if result.get('sections')!=expected:
        raise ValueError('qwen_sections_text_mismatch')
    raw=result.get('raw')
    if not isinstance(raw,dict) or raw.get('result_status')!='success' or raw.get('truncation_status') not in ('not_truncated','complete'):
        raise ValueError('qwen_generation_not_valid_complete')
    return None
