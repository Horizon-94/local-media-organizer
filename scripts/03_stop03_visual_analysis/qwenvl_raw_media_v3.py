"""RAW_MEDIA_V3 text contract; no database or model dependencies."""
import re

SCHEMA_VERSION = 'raw_media_v3'
CONTRACT_REVISION = 'raw_media_v3_r2_keywords_5_12'
KEYWORD_MIN = 5
KEYWORD_MAX = 12
GROUPS = (
    ('【内容】', ('画面描述', '主要主体', '动作/事件', '场景/环境')),
    ('【镜头】', ('景别', '构图/主体位置', '机位/视角')),
    ('【可用性】', ('主体可见性', '技术状态')),
    ('【检索】', ('检索关键词', '可承接主题/对白语义')),
    ('【剪辑】', ('画面功能', '可能用途')),
)
FIELDS = tuple(field for _, fields in GROUPS for field in fields)
REQUIRED_SECTIONS = tuple(field + '：' for field in FIELDS)
FUNCTIONS = {'环境建立', '人物状态', '人物动作', '动作细节', '物体细节', '过程记录',
             '反应镜头', '过渡素材', '氛围空镜', 'B-roll', '采访主体', '信息说明', '无法判断'}

def is_raw_media_prompt(prompt):
    return 'RAW_MEDIA_V3' in prompt and all(field + '：' in prompt for field in FIELDS)

def parse_fields(text):
    return {m.group(1): m.group(2).strip() for m in re.finditer(
        r'(?m)^\s*(' + '|'.join(re.escape(f) for f in FIELDS) + r')\s*[:：]\s*([^\n]*)$', text)}

def validate(text):
    """Exact ordered schema; do not silently deduplicate or repair output."""
    expected = [item for heading, fields in GROUPS for item in (heading, *fields)]
    actual = []; values = {}; issues = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in {heading for heading, _ in GROUPS}:
            actual.append(line)
            continue
        match = re.fullmatch(r'(' + '|'.join(re.escape(f) for f in FIELDS) + r')\s*[:：]\s*(.*)', line)
        if not match:
            issues.append('raw_media_unexpected_line')
            continue
        field, value = match.groups(); actual.append(field)
        if field in values:
            issues.append('raw_media_duplicate_field:' + field)
        values[field] = value.strip()
        if not value.strip():
            issues.append('raw_media_empty_field:' + field)
    missing = [field + '：' for field in FIELDS if field not in values]
    if actual != expected:
        issues.append('raw_media_schema_order_or_group_invalid')
    for field, low, high in [('检索关键词', KEYWORD_MIN, KEYWORD_MAX), ('可承接主题/对白语义', 3, 8), ('画面功能', 1, 2)]:
        if field in values:
            # The prompt explicitly permits uncertainty. Do not require invented
            # themes merely to fill the ordinary three-to-eight topic slots.
            if values[field] == '无法可靠判断':
                continue
            terms = [term.strip(' 。.!！?？') for term in re.split('[、,，;；]', values[field]) if term.strip(' 。.!！?？')]
            if not low <= len(terms) <= high:
                issues.append('raw_media_term_count_invalid:' + field)
            if field == '检索关键词':
                # Reject padding; never silently deduplicate, invent, or repair.
                import unicodedata
                normalized=[re.sub(r'\s+','',unicodedata.normalize('NFKC',term)).casefold() for term in terms]
                if len(set(normalized)) != len(normalized):
                    issues.append('raw_media_duplicate_term:' + field)
            if field == '画面功能':
                custom = [term for term in terms if term not in FUNCTIONS]
                if len(custom) > 1 or any(len(term) > 8 or re.search('[。句，,；;]', term) for term in custom):
                    issues.append('raw_media_function_not_short_label')
    if '可能用途' in values:
        purpose = values['可能用途']
        if purpose != '无法可靠判断' and not any(word in purpose for word in ('可用于', '可能', '适合作为')):
            issues.append('raw_media_absolute_purpose')
        if len([part for part in re.split('[。！!？?]', purpose) if part.strip()]) > 1:
            issues.append('raw_media_multiple_purpose_sentences')
    # Long exact adjacent loops only; repeated object names are legitimate.
    if re.search(r'(.{16,160}?)(?:\1){2,}', text, flags=re.S):
        issues.append('raw_media_repetition_loop')
    return values, missing, list(dict.fromkeys(issues))
