from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


CONTRACT_VERSION = "cinematic_selection_gate_v1"
REQUIREMENT_CONTRACT = "cinematic_visual_requirement_v1"
CANDIDATE_CONTRACT = "cinematic_candidate_subject_profile_v1"

GATE_STATUSES = {"PASS", "SOFT_GATE", "HARD_GATE"}
REASON_CODES = {
    "SUBJECT_MISMATCH",
    "DOMINANT_SECONDARY_SUBJECT",
    "FACT_CONFLICT",
    "SEMANTIC_ONLY",
    "UNSUPPORTED_CLAIM",
    "SHOT_ROLE_MISMATCH",
    "WEAK_EVIDENCE",
    "NEED_SOURCE_REVIEW",
    "SOUND_INSTRUCTION",
    "ABSTRACT_NEEDS_PRIMARY_CARRIER",
    "UNKNOWN_SUBJECT",
    "STATE_MISMATCH",
    "PARTIAL_STATE_COVERAGE",
    "SUBJECT_ANCHOR_MISMATCH",
    "EXPLICIT_VISUAL_TARGET_MISSING",
    "PROJECT_GUIDE_RESHOOT_REQUIRED",
    "PROJECT_GUIDE_EVIDENCE_MISSING",
}

_PERSON_TERMS = (
    "人物", "人群", "行人", "男性", "女性", "男子", "女子", "男人", "女人", "老人", "儿童",
    "孩子", "工人", "员工", "顾客", "游客", "主持人", "采访者", "讲述者", "person", "people",
    "农民", "农人", "摄影师", "驾驶员", "一个人", "一名", "一位",
    "man", "woman", "worker", "child", "crowd",
)
_REACTION_TERMS = (
    "反应", "表情", "微笑", "哭泣", "流泪", "沉默", "停顿", "凝视", "注视", "惊讶", "皱眉",
    "reaction", "smile", "cry", "gaze",
)
_ENVIRONMENT_TERMS = (
    "环境", "空间", "地点", "现场", "城市", "村庄", "街道", "道路", "建筑", "房间", "室内", "户外",
    "天空", "山", "河", "海", "湖", "树林", "森林", "广场", "场地", "远处", "整体", "全貌",
    "田野", "田地", "农田", "耕地", "田园", "田间", "田埂", "农作物", "作物", "庄稼", "植被",
    "environment", "location", "street", "room", "building", "landscape", "city",
)
_ACTION_TERMS = (
    "正在", "开始", "进入", "离开", "行走", "奔跑", "移动", "操作", "工作", "劳动", "制作", "驾驶",
    "拿起", "放下", "打开", "关闭", "搬运", "组装", "处理", "使用", "切割", "完成动作", "working",
    "作业", "行驶", "挥动", "收获", "收割",
    "检查", "观察", "查看", "触摸", "轻抚",
    "walking", "running", "moving", "operating", "driving", "making", "opening", "closing",
)
_PROCESS_TERMS = (
    "过程", "逐步", "慢慢", "一天天", "变化", "从", "变成", "制作中", "进行中", "流程", "步骤",
    "process", "progress", "changing",
)
_RESULT_TERMS = (
    "完成", "结束", "之后", "以后", "已经", "最终", "结果", "事后", "只剩", "留下", "空了", "消失",
    "完成后", "结束后", "finished", "completed", "aftermath", "result", "after",
)
_OBJECT_TERMS = (
    "物体", "物件", "产品", "设备", "工具", "机器", "车辆", "食物", "文件", "屏幕", "装置", "object",
    "product", "device", "tool", "machine", "vehicle",
)
_DETAIL_TERMS = (
    "细节", "局部", "特写", "纹理", "手部", "表面", "文字", "标签", "按钮", "detail", "close-up",
)
_ESTABLISH_TERMS = ("来到", "到达", "位于", "这里", "现场", "地点", "空间", "周围", "全貌", "where", "arrive")
_CAUSAL_TERMS = ("因为", "所以", "导致", "意味着", "因此", "原因", "cause", "because")
_IDENTITY_TERMS = ("身份", "属于", "所有权", "是谁", "谁的", "我家", "自己家", "identity", "ownership")
_PSYCHOLOGY_TERMS = ("觉得", "认为", "心里", "感到", "想法", "怀疑", "相信", "心理", "感觉", "think", "feel")
_HISTORY_TERMS = ("以前", "曾经", "过去", "小时候", "历史", "当年", "history", "formerly")

_VISUAL_ATTRIBUTE_PATTERNS = {
    "GREEN": ("绿色", "青绿", "翠绿", "碧绿", "嫩绿"),
    "YELLOW": ("黄色", "金黄", "枯黄", "泛黄", "黄起来", "变黄"),
    "RED": ("红色", "通红", "变红"),
    "BLUE": ("蓝色", "蔚蓝", "变蓝"),
    "BLACK": ("黑色", "漆黑", "变黑"),
    "WHITE": ("白色", "雪白", "变白"),
    "BRIGHT": ("明亮", "变亮", "亮起来"),
    "DARK": ("昏暗", "变暗", "暗下来"),
    "EMPTY": ("空旷", "空了", "清空"),
    "FULL": ("装满", "满了", "充满"),
    "WET": ("湿润", "湿了", "浸湿"),
    "DRY": ("干燥", "干了", "晒干"),
}
_TRANSITION_SINGLE_CHAR_ATTRIBUTES = {
    "绿": "GREEN", "黄": "YELLOW", "红": "RED", "蓝": "BLUE",
    "黑": "BLACK", "白": "WHITE", "亮": "BRIGHT", "暗": "DARK",
    "空": "EMPTY", "满": "FULL", "湿": "WET", "干": "DRY",
}
_SUBJECT_ANCHOR_STOP_TERMS = {
    "家里", "句话", "时候", "那时候", "起来", "回来", "突然", "自己", "这里", "那里",
    "什么", "怎么", "以前", "以后", "现在", "这样", "一样", "可以", "没有", "一个",
}


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def visual_attributes(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", str(text or ""))
    result = [
        name for name, patterns in _VISUAL_ATTRIBUTE_PATTERNS.items()
        if any(pattern in normalized for pattern in patterns)
    ]
    for match in re.finditer(r"(?:从|由)([^，。；]{1,8}?)(?:变成|变为|转为|变|到)([^，。；]{1,8})", normalized):
        for fragment in match.groups():
            for char, name in _TRANSITION_SINGLE_CHAR_ATTRIBUTES.items():
                if char in fragment and name not in result:
                    result.append(name)
    return result


def is_state_transition(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    if re.search(r"(?:从|由).{1,12}(?:变成|变为|转为|变|到).{1,12}", normalized):
        return True
    return bool(visual_attributes(normalized)) and any(
        marker in normalized for marker in ("逐渐", "慢慢", "一天天", "越来越", "变得", "起来")
    )


def _expected_subject_terms(beat: dict[str, Any], text: str) -> list[str]:
    explicit_targets = _matched_visible_target_concepts(text)
    if explicit_targets:
        return list(dict.fromkeys(
            str(term)
            for target in explicit_targets
            for term in target.get("candidate_terms") or []
            if str(term).strip()
        ))
    if not re.search(r"(?:它|其|这个|那个|这些|那些)", text):
        return []
    for value in beat.get("motif_terms") or []:
        term = str(value).strip()
        if 2 <= len(term) <= 8 and term not in _SUBJECT_ANCHOR_STOP_TERMS:
            return [term]
    return []


def expected_subject_terms(beat: dict[str, Any], text: str) -> list[str]:
    """Return the explicit subject anchor used by both recall and gating."""
    return _expected_subject_terms(beat, text)


def _subject_anchor_matches(expected_terms: list[str], candidate_text: str) -> list[str]:
    stop_chars = set("的一是在了和与或而也都就又还把被给对于着过很更最人我你他她它家有不说")
    ambiguous_prefixes = set("人物作事地家生活车路时中大上下前后")
    candidate_chars = set(re.findall(r"[\u3400-\u9fff]", candidate_text)) - stop_chars
    matches: list[str] = []
    for term in expected_terms:
        if term in candidate_text:
            matches.append(term)
            continue
        term_chars = set(re.findall(r"[\u3400-\u9fff]", term)) - stop_chars
        strong_character_overlap = (
            len(term_chars) >= 3
            and len(term_chars & candidate_chars) / len(term_chars) >= 0.75
        )
        shared_specific_prefix = (
            len(term) >= 2
            and term[0] not in ambiguous_prefixes
            and re.search(re.escape(term[0]) + r"[\u3400-\u9fff]", candidate_text) is not None
        )
        if strong_character_overlap or shared_specific_prefix:
            matches.append(term)
    return matches


def subject_anchor_matches(expected_terms: list[str], candidate_text: str) -> list[str]:
    """Public, generic subject-anchor matcher shared by recall and gating."""
    return _subject_anchor_matches(expected_terms, candidate_text)


def _strict_visible_target_matches(expected_terms: list[str], candidate_text: str) -> list[str]:
    """Match a named, verifiable target without fuzzy same-prefix guesses.

    The target registry already contains observable synonyms.  Exact presence
    is therefore safer than the broader pronoun/motif resolver used elsewhere.
    """

    normalized = re.sub(r"\s+", "", str(candidate_text or "")).casefold()
    return [
        term for term in expected_terms
        if re.sub(r"\s+", "", str(term)).casefold() in normalized
    ]


def _subject_visual_attributes(expected_terms: list[str], observation: str) -> list[str]:
    if not expected_terms:
        return visual_attributes(observation)
    result: list[str] = []
    reset_markers = (
        "背景", "远处", "旁边", "路边", "身后", "画面外", "另一边",
        "左侧", "右侧", "前景", "后景",
    )
    for sentence in [part.strip() for part in re.split(r"[。！？!?]", observation) if part.strip()]:
        active_subject = False
        for clause in [part.strip() for part in re.split(r"[，,；;、]", sentence) if part.strip()]:
            if _subject_anchor_matches(expected_terms, clause):
                active_subject = True
            elif any(clause.startswith(marker) for marker in reset_markers) or _contains(clause, _PERSON_TERMS):
                active_subject = False
            if active_subject:
                for value in visual_attributes(clause):
                    if value not in result:
                        result.append(value)
    return result


def subject_visual_attributes(expected_terms: list[str], observation: str) -> list[str]:
    """Return only visual states attached to the requested subject."""
    return _subject_visual_attributes(expected_terms, observation)


def _visible_target_concepts() -> list[dict[str, Any]]:
    path = Path(__file__).with_name("knowledge") / "rules" / "visible_target_concepts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in payload.get("concepts") or [] if isinstance(row, dict)]


def _matched_visible_target_concepts(text: str) -> list[dict[str, Any]]:
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    return [
        row for row in _visible_target_concepts()
        if any(str(term).casefold() in normalized for term in row.get("trigger_terms") or [])
    ]


def _policy() -> dict[str, Any]:
    path = Path(__file__).with_name("knowledge") / "rules" / "selection_gate.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _claim_codes(p0d: dict[str, Any]) -> list[str]:
    types = {value for row in p0d.get("claims") or [] for value in row.get("types") or []}
    result: list[str] = []
    mapping = {
        "identity_or_ownership": "IDENTITY_OR_OWNERSHIP",
        "psychology_or_attitude": "PSYCHOLOGY",
        "causal_or_normative": "CAUSALITY",
        "memory_or_time": "HISTORY_OR_MEMORY",
        "quotation_or_hearsay": "QUOTATION_OR_HEARSAY",
        "production_or_audio_cue": "PRODUCTION_OR_AUDIO_CUE",
    }
    for source, target in mapping.items():
        if source in types:
            result.append(target)
    return result


def _fallback_plan(
    beat: dict[str, Any],
    visual_task: str,
    expected: str,
    visualizability: str,
    preferred_roles: list[str],
    a_roll_preference: str,
    sound_instruction: bool,
    target_concepts: list[dict[str, Any]],
) -> dict[str, Any]:
    content_by_task = {
        "PERSON": "让人物本身成为信息中心，观众要能看清是谁、正在做什么或处在什么关系里。",
        "PERSON_REACTION": "抓到真实反应、停顿、视线或表情变化，不用无关人物代替当事人的反应。",
        "ACTION": "画面里必须真正发生这项动作，并能看清动作的方向和关键节点。",
        "PROCESS": "至少交代过程的开始、关键步骤或变化，不能只拍一个静止的相关物体。",
        "OBJECT": "让具体物件成为主体，同时保留足够环境说明它在哪里、被怎样使用。",
        "DETAIL": "用可辨认的局部细节把文稿落到具体证据上，避免只有漂亮但含义不清的特写。",
        "ENVIRONMENT_STATE": "让环境及其当前状态成为主体；人物可以出现，但不能抢走观众对空间和状态的判断。",
        "LOCATION_ESTABLISHING": "先交代地点、空间关系和观看方向，让观众知道事情发生在哪里。",
        "RESULT": "优先展示完成后的结果或事后状态，不能用仍在进行的过程冒充结果。",
        "EVIDENCE": "寻找能够直接证明句子可见部分的真实证据；身份、心理和因果仍由旁白或人物表达承担。",
        "ABSTRACT_REFLECTION": "这句话的核心是判断或感受，画面只负责承接语气和情绪，不能假装证明人物心理。",
        "SOUND_LED": "这句话首先由同期声、环境声或指定原声承担，画面只需维持现场和声音来源。",
        "UNKNOWN": "先保留人工判断，不要因为几个相似词就强行配一个 B-roll。",
    }
    aesthetic_by_task = {
        "PERSON": "优先中景、近景或真实关系镜头；脸、动作和视线清楚，背景不要抢主体。",
        "PERSON_REACTION": "优先近景或中近景，保留反应发生前后的停顿，不要只截一张似笑非笑的单帧。",
        "ACTION": "优先动作清楚的中景，再用细节补重音；原片要有干净的动作起点和落点。",
        "PROCESS": "景别最好有整体—动作—细节变化，方向连续，镜头长度足以看清步骤。",
        "OBJECT": "主体边缘、形状和使用关系清楚；避免杂乱背景或高显著度人物抢走注意力。",
        "DETAIL": "焦点准确、局部足够大，并尽量有前一镜交代它属于哪个空间或对象。",
        "ENVIRONMENT_STATE": "优先全景或中远景、层次清楚、状态可辨；避免人物大面积占画面中心。",
        "LOCATION_ESTABLISHING": "优先全景或中远景，地理关系、入口、方向或规模一眼可读。",
        "RESULT": "构图应把结果放在视觉中心，并与过程镜头形成明显变化；需要回看原片确认状态。",
        "EVIDENCE": "清楚、真实、可辨认优先于漂亮；镜头应让观众不靠旁白也能看见关键事实。",
        "ABSTRACT_REFLECTION": "优先保留 A-roll；若覆盖 B-roll，可用克制的环境、留白或真实反应，不追求字面配图。",
        "SOUND_LED": "画面稳定、声源关系可信，留出声音进入和离开的时间，不用快速切换抢走听觉重点。",
        "UNKNOWN": "保持构图和信息简洁，先确认这句话是否真的需要离开主叙述画面。",
    }
    role_by_task = {
        "PERSON": "承担人物介绍、关系说明或行动主体。",
        "PERSON_REACTION": "承担反应和情绪后果，让事件落到人身上。",
        "ACTION": "承担动作覆盖和节奏推进。",
        "PROCESS": "承担过程说明、时间压缩和动作连续。",
        "OBJECT": "承担具体说明、物证或操作对象。",
        "DETAIL": "承担插入细节、关键词重音或剪点遮盖。",
        "ENVIRONMENT_STATE": "承担环境说明、状态证据或段落呼吸。",
        "LOCATION_ESTABLISHING": "承担空间建立，让后续中景和特写有位置依据。",
        "RESULT": "承担结果、转折或动作完成后的回报。",
        "EVIDENCE": "承担事实证明，而不是只做题材相似的装饰画面。",
        "ABSTRACT_REFLECTION": "主要由 A-roll、旁白和停顿承担；B-roll 只负责有限的情绪或环境支撑。",
        "SOUND_LED": "承担声音引导、声桥或现场延续，画面不是主要证明者。",
        "UNKNOWN": "暂不指定固定职责，留给剪辑人员根据前后句决定。",
    }
    text = re.sub(r"\s+", " ", str(beat.get("text") or "")).strip()
    context_before = [str(value) for value in beat.get("context_before") or [] if str(value).strip()]
    context_after = [str(value) for value in beat.get("context_after") or [] if str(value).strip()]
    content = content_by_task.get(visual_task, content_by_task["UNKNOWN"])
    aesthetic = aesthetic_by_task.get(visual_task, aesthetic_by_task["UNKNOWN"])
    editorial_role = role_by_task.get(visual_task, role_by_task["UNKNOWN"])
    if target_concepts:
        content = f"针对本句“{text}”，" + "；".join(
            str(row.get("content_requirement") or "").rstrip("。")
            for row in target_concepts
            if str(row.get("content_requirement") or "").strip()
        ) + "。"
        aesthetic = "；".join(
            str(row.get("aesthetic_requirement") or "").rstrip("。")
            for row in target_concepts
            if str(row.get("aesthetic_requirement") or "").strip()
        ) + "。"
        editorial_role = "；".join(
            str(row.get("editing_responsibility") or "").rstrip("。")
            for row in target_concepts
            if str(row.get("editing_responsibility") or "").strip()
        ) + "。"
        if context_before or context_after:
            before = f"承接前文“{context_before[-1]}”" if context_before else "作为当前段落的起点"
            after = f"再交给后文“{context_after[0]}”继续推进" if context_after else "并为段落收束提供依据"
            editorial_role += f" 在这段叙事里，它要{before}，{after}；不能用全文母题画面反复代替这个具体事实。"

    if sound_instruction:
        capture = "先检查现有同期声和声源画面；缺少时优先补录声音，不必为了填满画面而补拍无关 B-roll。"
    elif a_roll_preference == "RECOMMENDED":
        capture = "可以直接保留人物主画面或旁白主载体；只有找到真实反应、环境或证据时才局部覆盖 B-roll。"
    elif visualizability == "DIRECT":
        capture = "现有库没有合格镜头时，按上述主体和职责补拍一组：先可读的主体镜头，再补动作或细节，不必拍很多相似空镜。"
    else:
        capture = "先保留 A-roll 或时间线缺口；补拍前确认这句话哪一部分真的能由画面表达。"
    if target_concepts:
        capture = "；".join(
            str(row.get("capture_suggestion") or "").rstrip("。")
            for row in target_concepts
            if str(row.get("capture_suggestion") or "").strip()
        ) + "。现有库没有这些可见证据时，应保留 A-roll 或素材缺口，不要用题材相近但主体错误的画面凑数。"
    return {
        "contract_version": "cinematic_fallback_plan_v1",
        "content_requirement": content,
        "aesthetic_requirement": aesthetic,
        "editing_responsibility": editorial_role,
        "capture_suggestion": capture,
        "expected_primary_subject": expected,
        "preferred_shot_roles": preferred_roles,
        "a_roll_is_valid": a_roll_preference in {"RECOMMENDED", "ALLOWED"},
        "do_not_force_broll": visualizability in {"KEEP_A_ROLL_PREFERRED", "SOUND_INSTRUCTION", "NEEDS_HUMAN_REVIEW"},
        "sequence_context": {
            "before": context_before,
            "current": text,
            "after": context_after,
        },
        "visual_target_ids": [str(row.get("id") or "") for row in target_concepts],
        "visual_target_labels": [str(row.get("label") or "") for row in target_concepts],
    }


def analyze_visual_requirement(beat: dict[str, Any], track: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(beat.get("text") or "")).strip()
    p0d = beat.get("p0d_intent") or {}
    strategy = beat.get("visual_strategy") or {}
    intent = str(strategy.get("narrative_intent") or "context")
    claim_codes = _claim_codes(p0d)
    has_visible = bool(p0d.get("visible_claims"))
    has_nonvisual = bool(p0d.get("nonvisual_claims"))
    sound_instruction = "PRODUCTION_OR_AUDIO_CUE" in claim_codes or intent == "audio_cue"

    visual_task = "EVIDENCE"
    expected = "UNKNOWN"
    allowed: list[str] = []
    undesired: list[str] = []
    temporal_state = "UNKNOWN"
    confidence = 0.58
    required_attributes = visual_attributes(text)
    expected_subject_terms: list[str] = []
    target_concepts = _matched_visible_target_concepts(text)
    project_guide = beat.get("editorial_guide") or {}

    if sound_instruction:
        visualizability = "SOUND_INSTRUCTION"
        visual_task = "SOUND_LED"
        expected = "UNKNOWN"
        confidence = 0.98
    elif intent in {"personal_reflection", "emotional_statement", "turning_point"} and has_nonvisual:
        visualizability = "KEEP_A_ROLL_PREFERRED"
        visual_task = "ABSTRACT_REFLECTION"
        expected = "PERSON" if _contains(text, _PERSON_TERMS) else "UNKNOWN"
        allowed = ["PERSON_REACTION", "ATMOSPHERE", "ENVIRONMENT"]
        confidence = 0.86
    elif target_concepts:
        visualizability = "DIRECT"
        visual_task = "EVIDENCE"
        expected = "EVIDENCE"
        allowed = ["OBJECT", "DETAIL", "ACTION", "PROCESS", "ENVIRONMENT"]
        undesired = ["UNRELATED_DOMINANT_SUBJECT"]
        confidence = 0.92
        expected_subject_terms = _expected_subject_terms(beat, text)
    elif is_state_transition(text):
        visualizability = "DIRECT"
        visual_task = "ENVIRONMENT_STATE"
        expected = "ENVIRONMENT_STATE"
        allowed = ["ENVIRONMENT", "DETAIL", "PROCESS"]
        undesired = ["PERSON_DOMINANT", "ACTION_DOMINANT"]
        temporal_state = "STATE_TRANSITION"
        confidence = 0.9
        expected_subject_terms = _expected_subject_terms(beat, text)
    elif _contains(text, _RESULT_TERMS):
        visualizability = "DIRECT" if has_visible else "PARTIAL"
        visual_task = "RESULT"
        temporal_state = "RESULT_STATE"
        if _contains(text, _ENVIRONMENT_TERMS):
            expected = "ENVIRONMENT_STATE"
        elif _contains(text, _PERSON_TERMS):
            expected = "PERSON"
        elif _contains(text, _OBJECT_TERMS):
            expected = "OBJECT"
        else:
            expected = "RESULT"
        allowed = ["DETAIL", "ENVIRONMENT", "OBJECT"]
        confidence = 0.82
    elif _contains(text, _REACTION_TERMS):
        visualizability = "DIRECT"
        visual_task = "PERSON_REACTION"
        expected = "PERSON_REACTION"
        allowed = ["PERSON", "DETAIL"]
        undesired = ["ENVIRONMENT_DOMINANT"]
        confidence = 0.86
    elif _contains(text, _ACTION_TERMS) or _contains(text, _PROCESS_TERMS):
        visualizability = "DIRECT" if has_visible else "PARTIAL"
        visual_task = "PROCESS" if _contains(text, _PROCESS_TERMS) else "ACTION"
        temporal_state = "PROCESS_ONGOING"
        expected = "PERSON" if _contains(text, _PERSON_TERMS) else visual_task
        allowed = ["ACTION", "PROCESS", "DETAIL", "OBJECT", "PERSON"]
        confidence = 0.78
    elif _contains(text, _DETAIL_TERMS):
        visualizability = "DIRECT"
        visual_task = "DETAIL"
        expected = "DETAIL"
        allowed = ["OBJECT"]
        undesired = ["ENVIRONMENT_DOMINANT", "PERSON_DOMINANT"]
        confidence = 0.84
    elif _contains(text, _ESTABLISH_TERMS) or str(beat.get("purpose") or "") == "建立情境":
        visualizability = "DIRECT"
        visual_task = "LOCATION_ESTABLISHING"
        expected = "ENVIRONMENT"
        allowed = ["PERSON", "RELATIONSHIP"]
        undesired = ["DETAIL_DOMINANT"]
        confidence = 0.76
    elif _contains(text, _ENVIRONMENT_TERMS):
        visualizability = "DIRECT" if has_visible else "PARTIAL"
        visual_task = "ENVIRONMENT_STATE"
        expected = "ENVIRONMENT"
        allowed = ["ATMOSPHERE", "LOCATION_ESTABLISHING"]
        undesired = ["PERSON_DOMINANT", "OBJECT_DOMINANT"]
        confidence = 0.78
    elif _contains(text, _PERSON_TERMS):
        visualizability = "DIRECT" if has_visible else "PARTIAL"
        visual_task = "PERSON"
        expected = "PERSON"
        allowed = ["PERSON_REACTION", "ACTION", "RELATIONSHIP"]
        undesired = ["ENVIRONMENT_DOMINANT"]
        confidence = 0.74
    elif _contains(text, _OBJECT_TERMS):
        visualizability = "DIRECT" if has_visible else "PARTIAL"
        visual_task = "OBJECT"
        expected = "OBJECT"
        allowed = ["DETAIL", "PROCESS"]
        undesired = ["PERSON_DOMINANT", "ENVIRONMENT_DOMINANT"]
        confidence = 0.72
    elif has_nonvisual and not has_visible:
        visualizability = "KEEP_A_ROLL_PREFERRED"
        visual_task = "ABSTRACT_REFLECTION"
        expected = "UNKNOWN"
        allowed = ["ATMOSPHERE", "PERSON_REACTION"]
        confidence = 0.78
    elif has_visible:
        visualizability = "DIRECT"
        visual_task = "EVIDENCE"
        expected = "EVIDENCE"
        allowed = ["ACTION", "PROCESS", "OBJECT", "PERSON", "ENVIRONMENT"]
    else:
        visualizability = "NEEDS_HUMAN_REVIEW"
        visual_task = "UNKNOWN"
        expected = "UNKNOWN"
        confidence = 0.35

    preferred_roles = list(strategy.get("preferred_editorial_functions") or [])
    acceptable_roles = list(dict.fromkeys([
        *preferred_roles,
        "CONTEXT", "ILLUSTRATION", "DETAIL", "BREATHING",
    ]))
    a_roll = "RECOMMENDED" if visualizability in {"KEEP_A_ROLL_PREFERRED", "SOUND_INSTRUCTION"} else (
        "ALLOWED" if has_nonvisual else "LOW"
    )
    fallback_plan = _fallback_plan(
        beat,
        visual_task,
        expected,
        visualizability,
        preferred_roles,
        a_roll,
        sound_instruction,
        target_concepts,
    )
    if temporal_state == "STATE_TRANSITION":
        fallback_plan.update({
            "content_requirement": "让同一主体的变化前状态、变化后状态，或两种状态同时清楚可见；普通相关动作不能代替状态变化。",
            "aesthetic_requirement": "优先主体明确的全景或中远景；单个镜头只呈现一端时，应与另一状态镜头组成前后对照。",
            "editing_responsibility": "承担状态对照和时间推进。可以用两条镜头分别表现变化前与变化后，不要求一个镜头包办全过程。",
        })
    if project_guide:
        guide_status = str(project_guide.get("guidance_status") or "READY")
        guide_direction = str(project_guide.get("visual_direction") or "").strip()
        guide_editing = str(project_guide.get("editing_method") or "").strip()
        guide_notes = str(project_guide.get("notes") or "").strip()
        fallback_plan["project_guidance"] = {
            "source": "用户提供的当前项目剪辑指导表",
            "status": guide_status,
            "section": str(project_guide.get("section") or ""),
            "guide_narration": str(project_guide.get("guide_narration") or ""),
            "visual_direction": guide_direction,
            "editing_method": guide_editing,
            "notes": guide_notes,
            "match_confidence": float(project_guide.get("match_confidence") or 0.0),
            "excel_rows": list(project_guide.get("excel_rows") or []),
        }
        if guide_status == "RESHOOT_PRIORITY":
            fallback_plan["content_requirement"] = (
                f"本项目逐句表把这句标记为“补拍优先”。需要的画面方向是：{guide_direction}"
                if guide_direction else fallback_plan["content_requirement"]
            )
            if guide_editing:
                fallback_plan["aesthetic_requirement"] = guide_editing
                fallback_plan["editing_responsibility"] = (
                    fallback_plan["editing_responsibility"] + " 项目剪辑表的具体执行法：" + guide_editing
                )
            fallback_plan["capture_suggestion"] = " ".join(filter(None, (
                guide_notes,
                "如果原素材盘中仍找不到表内要求的直接证据，保留 A-roll 或素材缺口，不得用表内占位镜头冒充正式证据。",
            )))
        else:
            if guide_direction:
                fallback_plan["content_requirement"] += f" 当前项目还要求优先检查：{guide_direction}"
            if guide_editing:
                fallback_plan["editing_responsibility"] += f" 当前项目剪辑手法：{guide_editing}"
            if guide_notes:
                fallback_plan["capture_suggestion"] += f" 项目备注：{guide_notes}"
    return {
        "contract_version": REQUIREMENT_CONTRACT,
        "narrative_intent": intent,
        "visualizability": visualizability,
        "visual_task": visual_task,
        "expected_primary_subject": expected,
        "allowed_secondary_subjects": allowed,
        "undesired_dominant_subjects": undesired,
        "preferred_shot_roles": preferred_roles,
        "acceptable_shot_roles": acceptable_roles,
        "forbidden_claims": claim_codes,
        "a_roll_preference": a_roll,
        "sound_instruction": sound_instruction,
        "temporal_state": temporal_state,
        "required_visual_attributes": required_attributes,
        "expected_subject_terms": expected_subject_terms,
        "visual_target_ids": [str(row.get("id") or "") for row in target_concepts],
        "visual_target_labels": [str(row.get("label") or "") for row in target_concepts],
        "strict_subject_anchor": bool(target_concepts),
        "project_editorial_guidance": project_guide or None,
        "confidence": confidence,
        "profile": track,
        "fallback_plan": fallback_plan,
    }


def derive_candidate_subject(candidate: dict[str, Any]) -> dict[str, Any]:
    observations = [str(value) for value in candidate.get("observations") or []]
    text = " ".join([
        str(candidate.get("display_title") or ""),
        *observations,
        " ".join(str(value) for value in candidate.get("tags") or []),
    ])
    first_clause = re.split(r"[，,。；;：:]", text, maxsplit=1)[0]
    keyframe = candidate.get("keyframe_analysis") or {}
    scales = [str(value) for value in (keyframe.get("shot_scale") or {}).get("values") or []]
    has_person = bool(candidate.get("person_cluster_ids")) or _contains(text, _PERSON_TERMS)
    person_leads = has_person and _contains(first_clause, _PERSON_TERMS)
    close_person = has_person and any("近景" in value or "特写" in value for value in scales)
    human_salience = "HIGH" if person_leads or close_person else ("MEDIUM" if has_person else "NONE")
    has_environment = _contains(text, _ENVIRONMENT_TERMS) or any("远景" in value or "全景" in value for value in scales)
    has_action = _contains(text, _ACTION_TERMS)
    has_process = _contains(text, _PROCESS_TERMS) or has_action
    has_result = _contains(text, _RESULT_TERMS) or re.search(
        r"(?:完成|结束|处理|制作|加工|施工|使用|收获|收割|清理|拆除)后", text,
    ) is not None
    has_reaction = has_person and _contains(text, _REACTION_TERMS)
    has_detail = _contains(text, _DETAIL_TERMS) or any("特写" in value for value in scales)
    nonhuman_labels = [
        str(value) for value in candidate.get("tags") or []
        if str(value) and not _contains(str(value), _PERSON_TERMS)
    ]
    has_object = _contains(text, _OBJECT_TERMS) or bool(nonhuman_labels)

    if has_reaction and human_salience == "HIGH":
        primary = "PERSON_REACTION"
    elif human_salience == "HIGH":
        primary = "PERSON"
    elif has_result and has_environment:
        primary = "ENVIRONMENT_STATE"
    elif has_result:
        primary = "RESULT"
    elif has_action:
        primary = "ACTION"
    elif has_process:
        primary = "PROCESS"
    elif has_detail:
        primary = "DETAIL"
    elif has_environment:
        primary = "ENVIRONMENT"
    elif has_object:
        primary = "OBJECT"
    elif has_person:
        primary = "PERSON"
    else:
        primary = "UNKNOWN"

    secondary: list[str] = []
    for value, present in (
        ("PERSON", has_person), ("ENVIRONMENT", has_environment), ("ACTION", has_action),
        ("PROCESS", has_process), ("RESULT", has_result), ("DETAIL", has_detail), ("OBJECT", has_object),
    ):
        if present and value != primary and value not in secondary:
            secondary.append(value)
    evidence_sources = candidate.get("evidence_sources") or {}
    evidence_strength = 86.0 if evidence_sources.get("qwenvl") else (62.0 if evidence_sources.get("yoloe_propagation") else 35.0)
    confidence = 0.84 if primary != "UNKNOWN" and evidence_sources.get("qwenvl") else (0.58 if primary != "UNKNOWN" else 0.0)
    temporal_state = "RESULT_STATE" if has_result else ("PROCESS_ONGOING" if has_action or has_process else "STATIC_OR_UNKNOWN")
    return {
        "contract_version": CANDIDATE_CONTRACT,
        "actual_primary_subject": primary,
        "secondary_subjects": secondary,
        "human_presence": has_person,
        "human_salience": human_salience,
        "subject_scale": scales or ["UNKNOWN"],
        "subject_role": "DOMINANT" if primary != "UNKNOWN" else "UNKNOWN",
        "candidate_shot_role": str(candidate.get("role") or "UNKNOWN"),
        "evidence_strength": evidence_strength,
        "temporal_state": temporal_state,
        "visual_attributes": visual_attributes(text),
        "state_transition_visible": is_state_transition(text),
        "fact_conflicts": [],
        "uncertain_claims": [] if evidence_sources.get("qwenvl") else ["PRIMARY_SUBJECT_REQUIRES_REVIEW"],
        "confidence": confidence,
    }


def _compatible(expected: str, actual: str, allowed: list[str]) -> float:
    if expected == "UNKNOWN" or actual == "UNKNOWN":
        return 50.0
    if expected == actual:
        return 94.0
    groups = {
        "PERSON": {"PERSON", "PERSON_REACTION"},
        "PERSON_REACTION": {"PERSON_REACTION", "PERSON"},
        "ENVIRONMENT": {"ENVIRONMENT", "ENVIRONMENT_STATE", "LOCATION_ESTABLISHING", "ATMOSPHERE"},
        "ENVIRONMENT_STATE": {"ENVIRONMENT_STATE", "ENVIRONMENT", "RESULT"},
        "ACTION": {"ACTION", "PROCESS", "PERSON"},
        "PROCESS": {"PROCESS", "ACTION", "DETAIL"},
        "RESULT": {"RESULT", "ENVIRONMENT_STATE", "DETAIL", "OBJECT"},
        "OBJECT": {"OBJECT", "DETAIL"},
        "DETAIL": {"DETAIL", "OBJECT"},
        "EVIDENCE": {"PERSON", "ENVIRONMENT", "ACTION", "PROCESS", "OBJECT", "DETAIL", "RESULT"},
    }
    if actual in groups.get(expected, set()):
        return 78.0
    if actual in allowed:
        return 68.0
    return 24.0


def evaluate_gate(
    requirement: dict[str, Any],
    candidate: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    policy = _policy()
    thresholds = policy["thresholds"]
    expected = str(requirement.get("expected_primary_subject") or "UNKNOWN")
    actual = str(profile.get("actual_primary_subject") or "UNKNOWN")
    allowed = [str(value) for value in requirement.get("allowed_secondary_subjects") or []]
    subject_score = _compatible(expected, actual, allowed)
    function = str((candidate.get("cinematic_rerank") or {}).get("editorial_function") or candidate.get("role") or "")
    role_map = {
        "建立": "ESTABLISH", "主叙述": "ILLUSTRATION", "证据": "EVIDENCE", "动作覆盖": "PROCESS",
        "反应": "REACTION", "插入/细节": "DETAIL", "环境/呼吸": "BREATHING", "转场": "TRANSITION",
        "钩子": "REVEAL", "收束": "PAYOFF",
    }
    function = role_map.get(function, function)
    required_states = set(str(value) for value in requirement.get("required_visual_attributes") or [])
    expected_terms = [str(value) for value in requirement.get("expected_subject_terms") or []]
    # Qwen's first observation is usually a short summary while the second
    # contains the factual element list.  Use both factual observations for
    # subject/state checks; retrieval-oriented text is deliberately excluded
    # because it may mention concepts that are not visibly present.
    factual_observations = [
        str(value).strip()
        for value in (candidate.get("observations") or [])[:2]
        if str(value).strip()
    ]
    primary_observation = " ".join(dict.fromkeys([
        str(candidate.get("display_title") or "").strip(),
        *factual_observations,
    ])).strip()
    candidate_states = set(
        _subject_visual_attributes(expected_terms, primary_observation)
        if expected_terms else profile.get("visual_attributes") or []
    )
    visible_target_matches = (
        _strict_visible_target_matches(expected_terms, primary_observation)
        if requirement.get("strict_subject_anchor") else
        _subject_anchor_matches(expected_terms, primary_observation)
    )
    if requirement.get("temporal_state") == "STATE_TRANSITION" and actual in {
        "ENVIRONMENT", "ENVIRONMENT_STATE", "DETAIL"
    }:
        function = "EVIDENCE" if required_states and required_states.issubset(candidate_states) else "ILLUSTRATION"
    preferred = set(str(value) for value in requirement.get("preferred_shot_roles") or [])
    acceptable = set(str(value) for value in requirement.get("acceptable_shot_roles") or [])
    role_score = 92.0 if function in preferred else (70.0 if function in acceptable else 34.0)
    evidence_score = float(profile.get("evidence_strength") or 0.0)
    truthfulness = 92.0
    reasons: list[str] = []
    reason_codes: list[str] = []
    fact_conflicts: list[str] = []

    if expected == "ENVIRONMENT" and profile.get("human_salience") == "HIGH":
        subject_score = min(subject_score, 44.0)
        reason_codes.append("DOMINANT_SECONDARY_SUBJECT")
        reasons.append("本句需要环境成为主体，但候选中的人物显著度较高，可能抢走空间信息。")
    if expected == "PERSON" and actual in {"ENVIRONMENT", "ENVIRONMENT_STATE"}:
        reason_codes.append("SUBJECT_MISMATCH")
        reasons.append("本句需要人物承担主要信息，候选却以环境为主要主体。")
    if requirement.get("temporal_state") == "RESULT_STATE" and profile.get("temporal_state") == "PROCESS_ONGOING":
        truthfulness = 12.0
        fact_conflicts.append("RESULT_VS_ONGOING_PROCESS")
        reason_codes.append("FACT_CONFLICT")
        reasons.append("文稿要求结果或事后状态，候选仍显示过程正在进行，画面事实方向相反。")
    if requirement.get("temporal_state") == "PROCESS_ONGOING" and profile.get("temporal_state") == "RESULT_STATE":
        truthfulness = 42.0
        fact_conflicts.append("ONGOING_PROCESS_VS_RESULT")
        reason_codes.append("FACT_CONFLICT")
        reasons.append("文稿要求过程发生，候选主要显示完成后的结果，只能作为后备。")
    if requirement.get("temporal_state") == "STATE_TRANSITION":
        state_overlap = required_states & candidate_states
        if required_states and not state_overlap:
            truthfulness = min(truthfulness, 28.0)
            reason_codes.append("STATE_MISMATCH")
            reasons.append("本句要求可见的状态变化，但候选没有呈现变化前或变化后的目标状态。")
        elif len(required_states) >= 2 and not required_states.issubset(candidate_states):
            reason_codes.append("PARTIAL_STATE_COVERAGE")
            reasons.append("候选只呈现状态变化的一端，需要与另一状态镜头组成前后对照。")
        if actual in {"ACTION", "PROCESS", "PERSON", "PERSON_REACTION"}:
            subject_score = min(subject_score, 20.0)
            reason_codes.append("SUBJECT_MISMATCH")
            reasons.append("本句要看的是主体状态变化，候选却让动作或人物成为主要信息。")
        if (
            profile.get("temporal_state") in {"PROCESS_ONGOING", "RESULT_STATE"}
            and not profile.get("state_transition_visible")
        ):
            truthfulness = min(truthfulness, 18.0)
            fact_conflicts.append("STATE_TRANSITION_VS_UNRELATED_EVENT")
            reason_codes.append("FACT_CONFLICT")
            reasons.append("本句要求自然状态或外观变化，候选主要呈现另一项动作过程或动作完成后的结果。")
        if expected_terms and not _subject_anchor_matches(expected_terms, primary_observation):
            subject_score = min(subject_score, 20.0)
            reason_codes.append("SUBJECT_ANCHOR_MISMATCH")
            reasons.append("画面虽出现了相似颜色或状态，但主要画面描述没有让这些状态落在文稿正在谈论的核心对象上。")

    if requirement.get("strict_subject_anchor") and expected_terms and not visible_target_matches:
        subject_score = min(subject_score, 16.0)
        reason_codes.extend(["SUBJECT_ANCHOR_MISMATCH", "EXPLICIT_VISUAL_TARGET_MISSING"])
        labels = [str(value) for value in requirement.get("visual_target_labels") or [] if str(value)]
        target_label = "、".join(labels) or "本句点名的可见对象"
        reasons.append(
            f"本句需要看到“{target_label}”的直接可见证据；候选画面描述没有出现这些对象或行为，"
            "只有全文题材或前后文相似，不能进入正常推荐。"
        )

    project_guide = requirement.get("project_editorial_guidance") or {}
    guide_status = str(project_guide.get("guidance_status") or "")
    guide_confidence = float(project_guide.get("match_confidence") or 0.0)
    guide_overlap = [
        str(value) for value in (candidate.get("score_components") or {}).get("editorial_guide_terms") or []
    ]
    if guide_status == "RESHOOT_PRIORITY" and guide_confidence >= 0.72:
        reason_codes.append("PROJECT_GUIDE_RESHOOT_REQUIRED")
        reasons.append("当前项目逐句表明确标记这句应优先补拍；已有画面即使相关，也只能先按过渡或后备素材判断。")
        if not guide_overlap:
            reason_codes.append("PROJECT_GUIDE_EVIDENCE_MISSING")
            reasons.append("候选没有命中逐句表要求的真实画面方向，不能用题材相近的画面冒充本句证据。")

    forbidden = set(str(value) for value in requirement.get("forbidden_claims") or [])
    evidence_mode = str((candidate.get("editorial_decision") or {}).get("evidence_mode") or "")
    nonvisual_only = requirement.get("visualizability") in {"KEEP_A_ROLL_PREFERRED", "SOUND_INSTRUCTION"}
    if forbidden & {"IDENTITY_OR_OWNERSHIP", "PSYCHOLOGY", "CAUSALITY", "HISTORY_OR_MEMORY"} and nonvisual_only:
        if evidence_mode == "direct_visible_evidence":
            truthfulness = min(truthfulness, 18.0)
            reason_codes.append("UNSUPPORTED_CLAIM")
            reasons.append("单个画面不能确定性证明本句的身份、心理、因果或历史含义。")
        else:
            reason_codes.append("ABSTRACT_NEEDS_PRIMARY_CARRIER")
            reasons.append("这句的核心应由人物表达、原声或旁白承担，画面只能提供有限支撑。")
    if requirement.get("sound_instruction"):
        reason_codes.append("SOUND_INSTRUCTION")
        reasons.append("这是声音/制作指令，普通 B-roll 只能作为人工备用，不能替代指定声音。")
    if actual == "UNKNOWN":
        reason_codes.append("UNKNOWN_SUBJECT")
        reasons.append("现有描述不足以确定主要主体，必须查看原片。")
    if expected != "UNKNOWN" and subject_score < float(thresholds["subject_pass"]):
        reason_codes.append("SUBJECT_MISMATCH")
        if not any("主体" in value for value in reasons):
            reasons.append(f"本句需要的主要主体是 {expected}，候选主要主体为 {actual}，主体匹配不足。")
    if role_score < float(thresholds["shot_role_pass"]):
        reason_codes.append("SHOT_ROLE_MISMATCH")
        reasons.append("候选镜头职责不是本句优先或可接受职责，只能作为后备。")
    if evidence_score < float(thresholds["evidence_weak"]):
        reason_codes.append("WEAK_EVIDENCE")
        reasons.append("候选缺少完整视觉描述，当前主体判断证据较弱。")
    if (candidate.get("editorial_decision") or {}).get("final_editorial_status") == "pending_source_playback":
        reason_codes.append("NEED_SOURCE_REVIEW")
    match_strength = str(candidate.get("match_strength") or "")
    if match_strength == "fallback" and subject_score <= 30.0 and role_score <= 40.0:
        reason_codes.append("SEMANTIC_ONLY")
        reasons.append("候选只保留了弱语义联系，主体和镜头职责都不支持本句。")

    reason_codes = list(dict.fromkeys(code for code in reason_codes if code in REASON_CODES))
    hard = bool(fact_conflicts and truthfulness <= 20.0) or "SEMANTIC_ONLY" in reason_codes
    if (
        "EXPLICIT_VISUAL_TARGET_MISSING" in reason_codes
        and float(profile.get("confidence") or 0.0) >= float(thresholds["high_confidence"])
        and float(requirement.get("confidence") or 0.0) >= float(thresholds["high_confidence"])
    ):
        hard = True
    if (
        subject_score <= float(thresholds["subject_hard_mismatch"])
        and float(profile.get("confidence") or 0.0) >= float(thresholds["high_confidence"])
        and float(requirement.get("confidence") or 0.0) >= float(thresholds["high_confidence"])
    ):
        hard = True
    if "UNSUPPORTED_CLAIM" in reason_codes and truthfulness <= 20.0:
        hard = True
    if "PROJECT_GUIDE_EVIDENCE_MISSING" in reason_codes:
        hard = True
    soft_gate_codes = {
        "SUBJECT_MISMATCH",
        "DOMINANT_SECONDARY_SUBJECT",
        "FACT_CONFLICT",
        "UNSUPPORTED_CLAIM",
        "SHOT_ROLE_MISMATCH",
        "WEAK_EVIDENCE",
        "SOUND_INSTRUCTION",
        "ABSTRACT_NEEDS_PRIMARY_CARRIER",
        "UNKNOWN_SUBJECT",
        "STATE_MISMATCH",
        "PARTIAL_STATE_COVERAGE",
        "SUBJECT_ANCHOR_MISMATCH",
        "EXPLICIT_VISUAL_TARGET_MISSING",
        "PROJECT_GUIDE_RESHOOT_REQUIRED",
        "PROJECT_GUIDE_EVIDENCE_MISSING",
    }
    if hard:
        status = "HARD_GATE"
    elif any(code in soft_gate_codes for code in reason_codes):
        status = "SOFT_GATE"
    else:
        status = "PASS"
    penalty = float(policy["gate_penalties"][status])
    return {
        "contract_version": CONTRACT_VERSION,
        "gate_status": status,
        "gate_penalty": penalty,
        "reason_codes": reason_codes,
        "reasons": reasons or ["主体、画面事实和镜头职责基本符合本句需求。"],
        "subject_match_score": subject_score,
        "shot_role_match_score": role_score,
        "evidence_score": evidence_score,
        "truthfulness_score": truthfulness,
        "fact_conflicts": fact_conflicts,
        "requires_source_review": "NEED_SOURCE_REVIEW" in reason_codes or actual == "UNKNOWN",
        "expected_primary_subject": expected,
        "actual_primary_subject": actual,
        "candidate_shot_role": function or "UNKNOWN",
        "subject_visual_attributes": sorted(candidate_states),
        "visible_target_matches": visible_target_matches,
        "diagnostic_only": status == "HARD_GATE",
    }
