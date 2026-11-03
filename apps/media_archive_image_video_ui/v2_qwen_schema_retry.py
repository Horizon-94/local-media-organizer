"""Declared, bounded RAW_MEDIA schema correction, preserving original output."""
from dataclasses import asdict

POLICY='raw_media_v3_schema_reask_once_v1'
UNCERTAINTY_POLICY='raw_media_v3_schema_reask_uncertainty_once_v2'
ORDERED_UNCERTAINTY_POLICY='raw_media_v3_ordered_uncertainty_reask_once_v3'
PERSISTENT_GROUP_POLICY='raw_media_v3_persistent_group_reask_v4_keywords_5_12'
GROUP_CORRECTION='''
格式纠正（不改变图片描述任务）：严格按以下五组、十三字段顺序输出，每个字段单独一行。
【内容】
画面描述：
主要主体：
动作/事件：
场景/环境：
【镜头】
景别：
构图/主体位置：
机位/视角：
【可用性】
主体可见性：
技术状态：
【检索】
检索关键词：
可承接主题/对白语义：
【剪辑】
画面功能：
可能用途：
“主体可见性”和“技术状态”只能位于【可用性】，禁止放在【镜头】。不得遗漏【可用性】标题。
检索关键词5–12个独立且有画面依据的词；禁止重复或近义词填充。可承接主题/对白语义3–8项；无法可靠判断时完整写“无法可靠判断”，不得为数量编造。保持短句。
'''
CORRECTION='\n格式提醒：仍使用以上5组13字段。检索关键词通常6–12项、可承接主题/对白语义通常3–8项；画面确实无法支持这些内容时，对应字段完整写“无法可靠判断”，不要写“无”，不要为了数量编造。各字段保持短句。'

def generate_wave_attempt(runner, *, candidate_id, image_path, prompt, attempt_number,
                          previous_outcome=None, policy=UNCERTAINTY_POLICY, operator_retry=None):
    """Exactly ONE physical inference per durable source-wave attempt.

    Same approved schema correction as generate(); no inline first+retry call,
    no token inflation, no parser relaxation. Previous output stays in audit.
    """
    from qwenvl_raw_media_v3 import is_raw_media_prompt
    if not is_raw_media_prompt(prompt):
        raise ValueError('raw_media_schema_retry_requires_thirteen_fields')
    if policy not in (POLICY,UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY,PERSISTENT_GROUP_POLICY):
        raise ValueError('unknown_raw_media_retry_policy')
    authorized = (isinstance(operator_retry,dict) and operator_retry.get('attempt_number')==attempt_number
                  and bool(operator_retry.get('request_id')) and len(str(operator_retry.get('job_id','')))==64
                  and len(str(operator_retry.get('token','')))==32)
    if operator_retry is not None and not authorized:raise ValueError('invalid_operator_retry_receipt')
    if attempt_number not in (1,2,3,4) and not (authorized and attempt_number>4):
        raise ValueError('existing_job_attempt_bound')
    if attempt_number>1 and not isinstance(previous_outcome,dict):
        raise ValueError('durable_previous_outcome_required')
    errors=str((previous_outcome or {}).get('cleanup_warnings') or '').split('|')
    if attempt_number>1:
        # Bound repair context to known schema codes; never echo the full old output.
        reasons=[e[:160] for e in errors if e.startswith('raw_media_')][:8]
        if reasons:prompt+='\n上次验证未通过：'+' | '.join(reasons)+'。仅修正这些问题，保持原有五组十三字段结构与画面依据。'

    persistent=policy==PERSISTENT_GROUP_POLICY and attempt_number>1 and (
        'raw_media_schema_order_or_group_invalid' in errors or
        bool((previous_outcome or {}).get('_schema_group_correction_applied')))
    if persistent:
        outcome=runner.generate_one(candidate_id=candidate_id,image_path=image_path,prompt=prompt+GROUP_CORRECTION)
        return outcome,{'policy':policy,'wave_attempt':attempt_number,'attempts':[asdict(outcome)],
            'previous_outcome':previous_outcome,'correction':GROUP_CORRECTION,'group_correction_applied':True,
            **({'operator_retry':operator_retry} if operator_retry is not None else {})}
    # Other existing failures retain bounded uncertainty handling. New policy
    # never changes the historical v1/v2/v3 semantics or first-pass prompt.
    effective_policy=UNCERTAINTY_POLICY if policy==PERSISTENT_GROUP_POLICY else policy
    # The approved schema correction is used at most ONCE (wave attempt 2).
    # Candidate budget is initial +3 retries; attempts 3/4 use the original prompt.
    correctable=attempt_number==2 and previous_outcome.get('truncation_status')=='not_truncated' and any(
        error.startswith(('raw_media_duplicate_field:','raw_media_schema_order_or_group_invalid',
            'raw_media_unexpected_line','raw_media_function_not_short_label','raw_media_term_count_invalid:')) for error in errors)
    correction=correctable and (effective_policy==ORDERED_UNCERTAINTY_POLICY or (
        effective_policy==UNCERTAINTY_POLICY and any(error.startswith('raw_media_term_count_invalid:') for error in errors)))
    constrained=correctable and not (effective_policy==UNCERTAINTY_POLICY and correction)
    prior=getattr(runner.backend,'raw_media_schema_constraint',False)
    if constrained:runner.backend.raw_media_schema_constraint=True
    try:
        outcome=runner.generate_one(candidate_id=candidate_id,image_path=image_path,
            prompt=prompt+(GROUP_CORRECTION if policy==PERSISTENT_GROUP_POLICY else CORRECTION) if correction else prompt)
    finally:
        if constrained:runner.backend.raw_media_schema_constraint=prior
    audit={'policy':policy,'wave_attempt':attempt_number,'attempts':[asdict(outcome)]}
    if operator_retry is not None:audit['operator_retry']=operator_retry
    if previous_outcome is not None:audit['previous_outcome']=previous_outcome
    if correction:audit['correction']=GROUP_CORRECTION if policy==PERSISTENT_GROUP_POLICY else CORRECTION
    return outcome,audit

def generate(runner,*,candidate_id,image_path,prompt,policy=POLICY):
    from qwenvl_raw_media_v3 import is_raw_media_prompt
    if not is_raw_media_prompt(prompt):
        raise ValueError('raw_media_schema_retry_requires_thirteen_fields')
    first=runner.generate_one(candidate_id=candidate_id,image_path=image_path,prompt=prompt)
    if policy not in (POLICY,UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY):raise ValueError('unknown_raw_media_retry_policy')
    audit={'policy':policy,'attempts':[asdict(first)]}
    # Content/length/native failures are not disguised as schema mistakes.
    errors=str(first.cleanup_warnings or '').split('|')
    correctable=first.truncation_status=='not_truncated' and any(
        error.startswith(('raw_media_duplicate_field:','raw_media_schema_order_or_group_invalid',
                          'raw_media_unexpected_line','raw_media_function_not_short_label','raw_media_term_count_invalid:')) for error in errors)
    if first.result_status!='success' and correctable:
        if policy == UNCERTAINTY_POLICY and any(error.startswith('raw_media_term_count_invalid:') for error in errors):
            # Reask once, never pad values or convert the old answer to success.
            # In uncertainty cases forced minimum-count decoding can prevent
            # newline and make the model echo instructions until truncation.
            second=runner.generate_one(candidate_id=candidate_id,image_path=image_path,prompt=prompt+CORRECTION)
            audit['correction']=CORRECTION
            audit['attempts'].append(asdict(second))
            return second,audit
        previous = getattr(runner.backend, 'raw_media_schema_constraint', False)
        runner.backend.raw_media_schema_constraint = True
        try:
            retry_prompt=prompt+CORRECTION if policy==ORDERED_UNCERTAINTY_POLICY else prompt
            second=runner.generate_one(candidate_id=candidate_id,image_path=image_path,prompt=retry_prompt)
        finally:
            runner.backend.raw_media_schema_constraint = previous
        audit['attempts'].append(asdict(second))
        if policy==ORDERED_UNCERTAINTY_POLICY:audit['correction']=CORRECTION
        return second,audit
    return first,audit
