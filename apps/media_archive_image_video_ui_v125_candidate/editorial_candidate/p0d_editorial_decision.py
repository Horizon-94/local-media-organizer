from __future__ import annotations

import re
from typing import Any


CONTRACT_VERSION = "p0d_editorial_decision_v1"
PROFILES = {"documentary", "short_video"}

CLAIM_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("production_or_audio_cue", ("这里落", "原声", "同期声", "画外音", "音乐起", "音乐停", "黑场", "字幕出现")),
    ("quotation_or_hearsay", ("听过", "听说", "有人说", "这句话", "意思大概", "意思是")),
    ("memory_or_time", ("以前", "那时候", "曾经", "过去", "小时候", "后来", "现在", "这次", "记得")),
    ("psychology_or_attitude", ("觉得", "感觉", "心里", "说不清", "说不好", "不明白", "知道", "相信", "怀疑", "愣", "想", "回头看一眼", "回头看看", "回头看自己")),
    ("identity_or_ownership", ("我家", "自己家", "家里的", "属于", "是谁", "谁种", "谁收", "还是我家")),
    ("causal_or_normative", ("因为", "所以", "当然", "应该", "就该", "才会", "导致", "意味着")),
    ("question", ("为什么", "怎么", "究竟", "？", "?")),
)

OBSERVABLE_TERMS = (
    "站", "走", "跑", "回来", "回去", "进门", "离开", "拿", "放", "开",
    "种", "收", "割", "劳作", "工作", "拍", "吹", "晃", "变", "黄", "成熟", "熟了", "下雨", "天亮",
    "有地", "有活", "出现", "消失", "倒下", "抬头", "回头", "停下",
)

NON_VISUAL_TYPES = {
    "production_or_audio_cue",
    "quotation_or_hearsay",
    "memory_or_time",
    "psychology_or_attitude",
    "identity_or_ownership",
    "causal_or_normative",
    "question",
}

PROFILE_CONFIG = {
    "documentary": {
        "label": "纪录片",
        "chars_per_second": 4.0,
        "roles": ["证据", "动作覆盖", "建立", "反应", "插入/细节", "环境/呼吸", "主叙述", "收束"],
        "basis": "事实真实性、过程完整、人物关系和同期声优先",
    },
    "short_video": {
        "label": "网感视频",
        "chars_per_second": 5.2,
        "roles": ["钩子", "动作覆盖", "插入/细节", "证据", "反应", "转场", "主叙述", "收束"],
        "basis": "在不制造错误事实的前提下，信息变化、动作能量、钩子和节拍优先",
    },
}


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[，,；;。！？!?]+", text) if part.strip()]


def _is_question_clause(clause: str) -> bool:
    compact = clause.strip()
    if "？" in compact or "?" in compact:
        return True
    return re.match(r"^(为什么|为何|究竟|到底|怎么(?:会|能|办|回事|可能))", compact) is not None


def _claim_types(clause: str) -> list[str]:
    types = [
        name for name, terms in CLAIM_RULES
        if name != "question" and any(term in clause for term in terms)
    ]
    if _is_question_clause(clause):
        types.append("question")
    if any(term in clause for term in OBSERVABLE_TERMS):
        types.append("observable_action_or_state")
    if not types:
        types.append("observable_or_context_unresolved")
    return list(dict.fromkeys(types))


def _duration_plan(text: str, profile: str, preferred_roles: list[str]) -> dict[str, Any]:
    config = PROFILE_CONFIG[profile]
    readable = re.sub(r"[\s，,。！？!?；;：“”‘’（）()【】]", "", text)
    narration_seconds = max(0.6, len(readable) / float(config["chars_per_second"]))
    if profile == "documentary":
        minimum, maximum = (2.5, 6.0) if any(role in preferred_roles for role in ("建立", "证据", "动作覆盖")) else (1.8, 5.0)
        target = min(maximum, max(minimum, narration_seconds + 0.4))
    else:
        minimum, maximum = (0.8, 2.2) if "钩子" in preferred_roles else (1.0, 3.5)
        target = min(maximum, max(minimum, narration_seconds * 0.85))
    return {
        "basis": "text_length_estimate_only",
        "estimated_narration_seconds": round(narration_seconds, 1),
        "suggested_picture_seconds": round(target, 1),
        "allowed_range_seconds": [minimum, maximum],
        "warning": "这是文稿长度和剪辑角色推算，不是实际配音时长，也不是已经验证的剪点。",
    }


def analyze_script_intent(beat: dict[str, Any], profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unsupported P0-D profile: {profile}")
    text = _clean(beat.get("text"))
    if not text:
        raise ValueError("script beat text is empty")
    claims: list[dict[str, Any]] = []
    for clause in _clauses(text):
        types = _claim_types(clause)
        directly_visible = (
            "production_or_audio_cue" not in types
            and any(value.startswith("observable_") for value in types)
        )
        claims.append({
            "text": clause,
            "types": types,
            "directly_visible": directly_visible,
            "carrier": "image" if directly_visible and not (set(types) & NON_VISUAL_TYPES) else "narration_or_verified_original_sound",
        })
    visible_claims = [row["text"] for row in claims if row["directly_visible"]]
    nonvisual_claims = [row["text"] for row in claims if set(row["types"]) & NON_VISUAL_TYPES]
    forbidden: list[str] = []
    all_types = {value for row in claims for value in row["types"]}
    if "identity_or_ownership" in all_types:
        forbidden.append("没有人物身份、土地归属或档案证据时，不得从相似场景推断“我家/自己家/属于谁”")
    if "psychology_or_attitude" in all_types:
        forbidden.append("不得仅凭景别、色调、站立或回头动作推断人物正在犹豫、怀念或认同")
    if "causal_or_normative" in all_types:
        forbidden.append("单个画面通常不能证明“因为/所以/应该/当然”等因果或规范判断")
    if "memory_or_time" in all_types or "quotation_or_hearsay" in all_types:
        forbidden.append("没有档案、原声或明确时间标识时，当前画面不能直接证明过去经历或话语来源")
    if "production_or_audio_cue" in all_types:
        forbidden.append("方括号或书名号中的原声、同期声、音乐、黑场和字幕说明属于制作指令，不得把指令文字当作画面关键词")

    required_roles = [str(value) for value in beat.get("required_roles") or []]
    profile_roles = PROFILE_CONFIG[profile]["roles"]
    preferred_roles = [role for role in profile_roles if role in required_roles]
    if not preferred_roles:
        preferred_roles = required_roles or profile_roles[:3]
    if nonvisual_claims and not visible_claims:
        primary_carrier = "narration_or_verified_original_sound"
    elif nonvisual_claims:
        primary_carrier = "image_plus_narration"
    else:
        primary_carrier = "image_can_carry_visible_claim"

    if profile == "documentary":
        picture_strategy = (
            "优先寻找能直接证明可见事实的过程、人物和空间关系；抽象或心理部分由旁白、原声或已确认反应承担。"
        )
    else:
        picture_strategy = (
            "优先把最清楚的动作、结果或冲突放在前面，并检查字幕安全区和变化密度；不得用高能量画面替代事实证据。"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        "profile_label": PROFILE_CONFIG[profile]["label"],
        "profile_basis": PROFILE_CONFIG[profile]["basis"],
        "claims": claims,
        "visible_claims": visible_claims,
        "nonvisual_claims": nonvisual_claims,
        "primary_carrier": primary_carrier,
        "preferred_roles": preferred_roles,
        "picture_strategy": picture_strategy,
        "forbidden_inferences": forbidden,
        "duration_plan": _duration_plan(text, profile, preferred_roles),
    }


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        _clean(value)
        for value in [
            candidate.get("display_title"),
            " ".join(candidate.get("observations") or []),
            " ".join(candidate.get("match_reasons") or []),
        ]
        if _clean(value)
    )


def _plain_language_explanation(
    intent: dict[str, Any],
    candidate: dict[str, Any],
    evidence_mode: str,
    technical_missing: list[str],
) -> dict[str, str]:
    visible = list(intent.get("visible_claims") or [])
    nonvisual = list(intent.get("nonvisual_claims") or [])
    observation = _clean((candidate.get("observations") or [candidate.get("display_title") or "未提供画面描述"])[0])
    role = _clean(candidate.get("role") or "待人工指定")
    retrieval = "；".join(_clean(value) for value in candidate.get("match_reasons") or [] if _clean(value))
    if visible and nonvisual:
        sentence_need = (
            f"这句话同时包含能拍到的部分“{'、'.join(visible)}”和不能只靠画面证明的部分“{'、'.join(nonvisual)}”。"
            "镜头只能负责前者，后者必须由旁白、原声或身份资料承担。"
        )
    elif visible:
        sentence_need = f"这句话需要观众实际看到“{'、'.join(visible)}”；合适镜头应把这个动作或状态交代清楚。"
    else:
        sentence_need = (
            f"这句话的核心“{'、'.join(nonvisual) or '抽象判断'}”不能由单个画面直接证明。"
            "这里需要的是能托住旁白、建立环境或给观众停顿的画面。"
        )

    candidate_contribution = f"现有画面描述是：“{observation}”。它目前最可能承担“{role}”，而不是自动证明整句话。"
    if evidence_mode == "direct_visible_evidence":
        fit_reason = "合理点在于：画面中的可见人物、对象或动作与本句可见内容直接对应，可以把它当作事实/过程画面使用。"
    elif evidence_mode == "partial_visible_evidence":
        fit_reason = "合理点在于：它能覆盖句子里确实看得见的那一部分；但人物身份、归属、心理或因果仍不能由这个镜头证明。"
    elif evidence_mode == "contextual_visible_support":
        fit_reason = "合理点不在于它能证明整句话，而在于它与前后句属于同一人物、地点或行动语境，可作为补充说明。"
    elif evidence_mode == "contextual_carrier_only":
        fit_reason = "合理点只在于它能给旁白一个具体环境和节奏停顿；它是承载画面，不是事实证据。"
    else:
        fit_reason = "目前找不到足够明确的合理点，不应仅凭题材相似或画面好看就选入。"
    if retrieval:
        fit_reason += f" 检索命中依据：{retrieval}。"

    keyframe = candidate.get("keyframe_analysis") or {}
    language_parts: list[str] = []
    for label, field_name in (("景别", "shot_scale"), ("构图", "composition"), ("角度", "camera_angle")):
        values = ((keyframe.get(field_name) or {}).get("values") or [])
        if values:
            language_parts.append(f"{label}为{'、'.join(str(value) for value in values)}")
    roles = [str(value) for value in keyframe.get("possible_editorial_roles") or []]
    if language_parts:
        visual_language = "目前由代表帧可确认或保守推测：" + "；".join(language_parts) + "。"
        if roles:
            visual_language += f"因此它可能发挥“{'、'.join(roles)}”作用；是否真的成立仍要看原片前后帧。"
    else:
        visual_language = "现有代表帧描述不足以可靠判断景别、构图或角度，因此本次入围不能把“视听语言好”当作选择理由。"

    boundaries = list(intent.get("forbidden_inferences") or [])
    fit_boundary = "；".join(boundaries[:2]) if boundaries else "它只能证明画面中实际可见的内容，不能替人物身份、心理、因果和时间关系作证。"
    checks = [
        "打开原片确认代表帧描述与连续画面一致",
        "确认动作起点和落点完整，镜头没有失焦、剧烈晃动或提前切断",
        "把它放到前后镜头之间，检查景别、方向、动作和声音是否能接上",
    ]
    if technical_missing:
        checks.append("当前尚未完成：" + "、".join(technical_missing[:2]))
    acceptance_check = "；".join(checks) + "。只有这些检查通过，才从“文字相关”升级为“剪辑上可用”。"
    return {
        "sentence_need": sentence_need,
        "candidate_contribution": candidate_contribution,
        "fit_reason": fit_reason,
        "visual_language": visual_language,
        "fit_boundary": fit_boundary,
        "acceptance_check": acceptance_check,
    }


def _technical_status(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    multiframe = candidate.get("multiframe_analysis") or {}
    clean = str(candidate.get("clean_status") or multiframe.get("clean_status") or "")
    missing: list[str] = []
    if clean != "technical_edges_screened":
        missing.append("未验证真实镜头边界和干净入出点")
    if not multiframe:
        missing.extend(["未播放多帧确认运镜、动作起落和稳定性", "未验证同期声、噪声和声音桥价值"])
    return ("screened" if not missing else "pending_source_playback"), missing


def _role_fits(role: str, intent: dict[str, Any]) -> bool:
    preferred = set(intent["preferred_roles"])
    if role in preferred:
        return True
    if intent["visible_claims"] and role in {"证据", "建立", "动作覆盖", "插入/细节", "主叙述"}:
        return bool(preferred & {"证据", "建立", "动作覆盖", "插入/细节", "主叙述", "钩子"})
    if not intent["visible_claims"] and role in {"环境/呼吸", "反应", "插入/细节", "转场"}:
        return True
    return False


def _provisional_window(candidate: dict[str, Any], target_seconds: float) -> dict[str, Any]:
    start = max(0, int(candidate.get("start_ms") or 0))
    end = max(start, int(candidate.get("end_ms") or start))
    available = max(0, end - start)
    requested = max(500, int(target_seconds * 1000))
    use = min(available, requested) if available else requested
    anchor = int(candidate.get("anchor_time_ms") or (start + end) // 2)
    provisional_start = max(start, min(anchor - use // 2, max(start, end - use)))
    provisional_end = provisional_start + use
    return {
        "provisional_in_ms": provisional_start,
        "provisional_out_ms": provisional_end,
        "provisional_duration_ms": use,
        "source_candidate_window_ms": [start, end],
        "requires_playback": True,
        "warning": "只是在现有候选窗口内给出试放范围；必须按动作、声音和真实镜头边界重新定点。",
    }


def evaluate_candidate(beat: dict[str, Any], candidate: dict[str, Any], profile: str) -> dict[str, Any]:
    intent = beat.get("p0d_intent") or analyze_script_intent(beat, profile)
    if intent.get("profile") != profile:
        intent = analyze_script_intent(beat, profile)
    visible = list(intent["visible_claims"])
    nonvisual = list(intent["nonvisual_claims"])
    match_strength = str(candidate.get("match_strength") or "")
    role = str(candidate.get("role") or "")
    keyframe = candidate.get("keyframe_analysis") or {}
    inferred_roles = [str(value) for value in keyframe.get("possible_editorial_roles") or []]
    role_fit = any(_role_fits(value, intent) for value in [role, *inferred_roles])
    reasons: list[str] = []
    limits: list[str] = list(intent["forbidden_inferences"])

    if visible and match_strength == "strong":
        evidence_mode = "direct_visible_evidence" if not nonvisual else "partial_visible_evidence"
        reasons.append("素材与本句的可见动作或状态存在直接召回关系")
    elif visible and match_strength == "contextual":
        evidence_mode = "contextual_visible_support"
        reasons.append("素材主要依靠前后文，只能作为可见内容的语境补充")
    elif not visible and role_fit:
        evidence_mode = "contextual_carrier_only"
        reasons.append("本句核心不可由画面直接证明；该镜头最多承载旁白或提供环境呼吸")
    else:
        evidence_mode = "insufficient_evidence"
        reasons.append("当前证据不足以证明句子核心，也没有明确的语境承载角色")

    if role_fit:
        role_text = "、".join(_unique_roles := list(dict.fromkeys([role, *inferred_roles])))
        reasons.append(f"候选已有或由代表帧推测的作用“{role_text}”包含本句需要的剪辑职责")
    elif role:
        limits.append(f"候选作用“{role}”不属于本句当前优先角色：{'、'.join(intent['preferred_roles'])}")

    if evidence_mode == "direct_visible_evidence" and role_fit:
        recommendation = "recommended_for_preview"
    elif evidence_mode in {"partial_visible_evidence", "contextual_visible_support"}:
        recommendation = "borderline_for_preview"
    elif evidence_mode == "contextual_carrier_only" and role_fit and match_strength in {"strong", "contextual", "fallback"}:
        recommendation = "borderline_for_preview"
    else:
        recommendation = "exclude_from_editor_view"

    technical_status, technical_missing = _technical_status(candidate)
    final_status = "pending_source_playback"
    if recommendation == "exclude_from_editor_view":
        final_status = "not_recommended"
    elif technical_status == "screened" and candidate.get("sequence_fit_status") == "passed":
        final_status = "editorially_ready"
    limits.extend(technical_missing)
    duration = intent["duration_plan"]
    if evidence_mode == "direct_visible_evidence":
        editing_method = "把它作为B-roll事实/过程覆盖，只覆盖与画面直接对应的句段；优先直切，动作剪点待原片确认。"
    elif evidence_mode in {"partial_visible_evidence", "contextual_visible_support"}:
        editing_method = "只覆盖画面能够证明的半句，不能整句铺满；不可见的身份、心理或因果应回到A-roll、原声或另一条证据镜头。"
    elif evidence_mode == "contextual_carrier_only":
        editing_method = "只能作为旁白下的环境或情绪承载，不标成事实证据；宜短用，并检查是否会诱导错误联想。"
    else:
        editing_method = "不进入剪辑候选页面。"
    return {
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        "recommendation": recommendation,
        "evidence_mode": evidence_mode,
        "final_editorial_status": final_status,
        "role_fit": role_fit,
        "reasons": list(dict.fromkeys(reasons)),
        "limits": list(dict.fromkeys(limits)),
        "keyframe_language": {
            "shot_scale": keyframe.get("shot_scale") or {"status": "unknown", "values": []},
            "composition": keyframe.get("composition") or {"status": "unknown", "values": []},
            "camera_angle": keyframe.get("camera_angle") or {"status": "unknown", "values": []},
            "camera_motion": keyframe.get("camera_motion") or {"status": "unknown_from_single_frame", "values": []},
            "color_tone": keyframe.get("color_tone") or {"status": "not_used_for_fit", "values": []},
        },
        "roll_decision": {
            "role": keyframe.get("roll_role") or "b_roll_candidate",
            "reason": keyframe.get("roll_role_reason") or "没有已验证的人物同期讲话和主叙述证据，暂按B-roll候选处理",
        },
        "editing_method": editing_method,
        "neighbor_requirements": keyframe.get("neighbor_requirements") or [],
        "observable_candidate_text": _candidate_text(candidate),
        "duration": _provisional_window(candidate, float(duration["suggested_picture_seconds"])),
        "hard_gate": {
            "identity_ownership_verified": bool(candidate.get("verified_identity_or_ownership")),
            "psychological_state_verified": bool(candidate.get("verified_reaction_event")),
            "source_playback_verified": technical_status == "screened",
            "sequence_fit_verified": candidate.get("sequence_fit_status") == "passed",
        },
        "human_decision_required": True,
        "plain_language_explanation": _plain_language_explanation(
            intent,
            candidate,
            evidence_mode,
            technical_missing,
        ),
    }
