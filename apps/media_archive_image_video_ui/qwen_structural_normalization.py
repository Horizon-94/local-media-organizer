"""Lossless structural repair; the existing full classifier remains authoritative."""
import re
from dataclasses import asdict, replace

def normalize(text, schema):
    values={}
    headings={heading for heading,_ in schema.GROUPS}
    for line in text.splitlines():
        line=line.strip()
        if not line or line in headings:continue
        match=re.fullmatch(r'('+ '|'.join(re.escape(f) for f in schema.FIELDS)+r')\s*[:：]\s*(.*)',line)
        if not match:return None
        field,value=match.groups();value=value.strip()
        if not value or (field in values and values[field]!=value):return None
        values[field]=value
    if set(values)!=set(schema.FIELDS):return None
    canonical='\n'.join(line for heading,fields in schema.GROUPS for line in (heading,*(f+'：'+values[f] for f in fields)))
    _,missing,issues=schema.validate(canonical)
    return canonical if not missing and not issues else None

def repair(outcome, runner_module, max_tokens):
    audit={'original_outcome':asdict(outcome),'deterministic_repaired':False}
    if outcome.result_status=='success' or outcome.truncation_status!='not_truncated':return outcome,audit
    canonical=normalize(outcome.clean_text,runner_module.raw_media_contract)
    if canonical is None or canonical==outcome.clean_text:return outcome,audit
    parsed=runner_module.ParsedResponse(text=canonical,**{k:getattr(outcome,k) for k in
        ('prompt_tokens','generation_tokens','peak_memory_gb','generation_tps','raw_finish_reason','inferred_finish_reason','response_shape')})
    checked=runner_module.classify_response(outcome.candidate_id,parsed,max_tokens=max_tokens)
    if checked.result_status!='success':return outcome,audit
    audit['deterministic_repaired']=True
    return replace(checked,raw_text=outcome.raw_text),audit
