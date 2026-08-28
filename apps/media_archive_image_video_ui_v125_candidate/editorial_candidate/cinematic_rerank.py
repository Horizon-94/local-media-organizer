from __future__ import annotations

from collections import Counter
import math
from typing import Any

try:
    from .cinematic_knowledge import load_knowledge
except ImportError:  # Direct-file contract tests.
    from cinematic_knowledge import load_knowledge


ROLE_TO_FUNCTION = {
    "建立": "ESTABLISH",
    "主叙述": "ILLUSTRATION",
    "证据": "EVIDENCE",
    "动作覆盖": "PROCESS",
    "反应": "REACTION",
    "插入/细节": "DETAIL",
    "环境/呼吸": "BREATHING",
    "转场": "TRANSITION",
    "钩子": "REVEAL",
    "收束": "PAYOFF",
    "KEEP_A_ROLL": "KEEP_A_ROLL",
}


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def analyze_narrative_intent(beat: dict[str, Any]) -> str:
    purpose = str(beat.get("purpose") or "")
    order = int(beat.get("order") or 0)
    p0d = beat.get("p0d_intent") or {}
    claims = {kind for row in p0d.get("claims") or [] for kind in row.get("types") or []}
    text = str(beat.get("text") or "")
    if "production_or_audio_cue" in claims:
        return "audio_cue"
    if purpose == "开场引入" or order == 1:
        return "hook"
    if purpose == "段落收束":
        return "ending"
    if "question" in claims or "？" in text or "?" in text:
        return "conflict"
    if "psychology_or_attitude" in claims:
        return "personal_reflection"
    if "identity_or_ownership" in claims:
        return "claim"
    if "quotation_or_hearsay" in claims:
        return "context"
    if "memory_or_time" in claims:
        return "callback"
    if "causal_or_normative" in claims:
        return "explanation"
    if p0d.get("visible_claims"):
        return "evidence"
    return "context"


def build_visual_strategy(beat: dict[str, Any], track: str) -> dict[str, Any]:
    knowledge = load_knowledge()
    intent = analyze_narrative_intent(beat)
    configured = (knowledge["narrative_intents"].get("intent_strategies") or {}).get(intent) or {}
    prefer = list(configured.get("prefer") or ["CONTEXT", "ILLUSTRATION"])
    avoid = list(configured.get("avoid") or [])
    p0d = beat.get("p0d_intent") or {}
    nonvisual = bool(p0d.get("nonvisual_claims"))
    visible = bool(p0d.get("visible_claims")) and intent != "audio_cue"
    keep_eligible = nonvisual or intent in {"personal_reflection", "emotional_statement", "turning_point", "claim", "hook", "ending", "audio_cue"}
    if keep_eligible and "KEEP_A_ROLL" not in prefer:
        prefer.insert(0, "KEEP_A_ROLL")
    evidence_required = intent != "audio_cue" and (intent in {"claim", "evidence", "comparison"} or visible)
    return {
        "contract_version": "cinematic_visual_strategy_v1",
        "narrative_intent": intent,
        "profile": track,
        "preferred_editorial_functions": prefer,
        "avoid": avoid,
        "evidence_required": evidence_required,
        "keep_a_roll": {
            "eligible": keep_eligible,
            "available": True,
            "recommended": keep_eligible,
            "reason": "建议保留：句子的心理、判断、转折或人物表达不能由泛化 B-roll 直接证明" if keep_eligible else "人工可选：本句优先找可见事实，但如果没有合适画面，你仍可保留人物口播；口播本身不等于事实已经被证明。",
        },
        "shot_brief": _shot_brief(intent, prefer, evidence_required, nonvisual),
    }


def _shot_brief(intent: str, prefer: list[str], evidence_required: bool, nonvisual: bool) -> str:
    if intent == "audio_cue":
        return "这是声音或制作指令：优先保留指定原声、同期画面或黑场，不用指令里的词强行搜索 B-roll。"
    if "KEEP_A_ROLL" in prefer and nonvisual:
        return "先保护人物表达或旁白逻辑；若使用 B-roll，只覆盖能够被画面证明的部分。"
    if evidence_required:
        return "优先找本次真实事件中的人物、动作、物件、文字或结果；环境空镜不能替代证据。"
    if intent in {"setup", "context"}:
        return "先让观众看清地点、人物关系和行动条件，再进入细节。"
    return "镜头必须增加新信息、反应或节奏作用，不能只因题材相似入选。"


def editorial_function(candidate: dict[str, Any]) -> str:
    return ROLE_TO_FUNCTION.get(str(candidate.get("role") or ""), "ILLUSTRATION")


def score_candidate(
    beat: dict[str, Any],
    candidate: dict[str, Any],
    track: str,
    *,
    prior_source_usage: Counter[str] | None = None,
) -> dict[str, Any]:
    knowledge = load_knowledge()
    strategy = beat.get("visual_strategy") or build_visual_strategy(beat, track)
    function = editorial_function(candidate)
    requirement = beat.get("selection_requirement") or {}
    profile = candidate.get("candidate_subject_profile") or {}
    if requirement.get("temporal_state") == "STATE_TRANSITION" and profile.get("actual_primary_subject") in {
        "ENVIRONMENT", "ENVIRONMENT_STATE", "DETAIL"
    }:
        required_states = set(str(value) for value in requirement.get("required_visual_attributes") or [])
        gate = candidate.get("selection_gate") or {}
        state_values = (
            gate.get("subject_visual_attributes")
            if "subject_visual_attributes" in gate
            else profile.get("visual_attributes") or []
        )
        candidate_states = set(str(value) for value in state_values or [])
        function = "EVIDENCE" if required_states and required_states.issubset(candidate_states) else "ILLUSTRATION"
    preferred = set(strategy["preferred_editorial_functions"])
    decision = candidate.get("editorial_decision") or {}
    evidence_mode = str(decision.get("evidence_mode") or "insufficient_evidence")
    retrieval = max(0.0, float((candidate.get("score_components") or {}).get("retrieval_score") or 0.0))
    semantic = _clamp(100.0 * (1.0 - math.exp(-retrieval / 14.0)))
    narrative = 90.0 if function in preferred else (62.0 if function in {"CONTEXT", "ILLUSTRATION", "DETAIL", "BREATHING"} else 38.0)
    recommendation = str(decision.get("recommendation") or "")
    editorial = {"recommended_for_preview": 86.0, "borderline_for_preview": 62.0, "exclude_from_editor_view": 20.0}.get(recommendation, 45.0)
    evidence = {
        "direct_visible_evidence": 94.0,
        "partial_visible_evidence": 76.0,
        "contextual_visible_support": 57.0,
        "contextual_carrier_only": 42.0,
        "a_roll_primary_carrier": 82.0,
        "insufficient_evidence": 18.0,
    }.get(evidence_mode, 35.0)
    keyframe = candidate.get("keyframe_analysis") or {}
    known_language = sum(bool((keyframe.get(name) or {}).get("values")) for name in ("shot_scale", "composition", "camera_angle"))
    cinematic = 48.0 + known_language * 9.0
    source_id = str(candidate.get("source_content_id") or "")
    used = int((prior_source_usage or Counter()).get(source_id, 0)) if source_id else 0
    sequence = 78.0 - min(36.0, used * 14.0)
    preview = bool(candidate.get("preview_available"))
    technical = 76.0 if preview else 35.0
    if candidate.get("technical_score") is not None:
        normalized_sharpness = 100.0 * (1.0 - math.exp(-max(0.0, float(candidate["technical_score"])) / 5.0))
        technical = max(technical, normalized_sharpness)
    aesthetic = 50.0
    authenticity = 82.0 if evidence_mode == "direct_visible_evidence" else 64.0
    gate = candidate.get("selection_gate") or {}
    subject_match = float(gate.get("subject_match_score") or 50.0)
    shot_role_match = float(gate.get("shot_role_match_score") or 50.0)
    truthfulness = float(gate.get("truthfulness_score") or 70.0)
    if function == "KEEP_A_ROLL":
        # A-roll is evaluated as a carrier decision, not as a media-search hit.
        # It must not lose merely because it has no B-roll keywords or preview.
        semantic = 78.0
        narrative = 96.0 if "KEEP_A_ROLL" in preferred else 72.0
        editorial = 92.0
        evidence = 88.0
        cinematic = 70.0
        sequence = 82.0
        technical = 70.0
        authenticity = 95.0
        subject_match = 90.0
        shot_role_match = 96.0
        truthfulness = 98.0
    if not (candidate.get("evidence_sources") or {}).get("qwenvl"):
        authenticity -= 12.0
    sources = candidate.get("evidence_sources") or {}
    if sources.get("ocr") and strategy["evidence_required"]:
        evidence += 6.0
    if sources.get("nearby_asr"):
        authenticity += 5.0
    if sources.get("person_cluster"):
        authenticity += 3.0
    if candidate.get("high_value_score") is not None:
        cinematic += min(8.0, max(0.0, float(candidate["high_value_score"])) * 0.6)
    if candidate.get("favorite"):
        aesthetic += 8.0
    user_preference = 72.0 if candidate.get("favorite") else (62.0 if candidate.get("user_rating") else 50.0)
    scores = {
        "semantic_relevance": semantic,
        "narrative_fit": _clamp(narrative),
        "editorial_utility": _clamp(editorial),
        "evidence_strength": _clamp(evidence),
        "subject_match": _clamp(subject_match),
        "shot_role_match": _clamp(shot_role_match),
        "truthfulness": _clamp(truthfulness),
        "cinematic_fit": _clamp(cinematic),
        "sequence_compatibility": _clamp(sequence),
        "technical_usability": _clamp(technical),
        "aesthetic_quality": _clamp(aesthetic),
        "authenticity": _clamp(authenticity),
        "user_preference_placeholder": user_preference,
    }
    penalties = {name: 0.0 for name in knowledge["weights"]["penalties"]}
    if used:
        penalties["repetition_penalty"] = min(float(knowledge["weights"]["penalties"]["repetition_penalty"]), used * 4.0)
    if strategy["evidence_required"] and evidence_mode in {"contextual_carrier_only", "insufficient_evidence"}:
        penalties["generic_broll_penalty"] = float(knowledge["weights"]["penalties"]["generic_broll_penalty"])
    if gate:
        penalties["gate_penalty"] = float(gate.get("gate_penalty") or 0.0)
    weights = knowledge["weights"]["weights"]
    weighted = sum(scores[name] * float(weight) for name, weight in weights.items())
    final = _clamp(weighted - sum(penalties.values()))
    reason = _recommendation_reason(strategy, function, evidence_mode, candidate, scores, penalties)
    guide = beat.get("editorial_guide") or {}
    guide_terms = [
        str(value) for value in (candidate.get("score_components") or {}).get("editorial_guide_terms") or []
    ]
    if guide_terms:
        reason += " 同时命中当前项目逐句表中的画面方向：" + "、".join(guide_terms[:5]) + "。"
    if str(guide.get("guidance_status") or "") == "SOURCE_REVIEW":
        reason += " 逐句表将相关素材标为待核，正式采用前仍要打开原片确认。"
    return {
        "contract_version": "cinematic_rerank_v1",
        "narrative_intent": strategy["narrative_intent"],
        "visual_strategy": strategy,
        "editorial_function": function,
        "scores": scores,
        "penalties": penalties,
        "final_score": final,
        "recommendation_reason": reason,
        "gate_status": str(gate.get("gate_status") or "PASS"),
        "gate_reason_codes": list(gate.get("reason_codes") or []),
        "weights_contract": knowledge["weights"]["contract_version"],
        "aesthetic_is_not_editorial_utility": True,
    }


def _recommendation_reason(
    strategy: dict[str, Any],
    function: str,
    evidence_mode: str,
    candidate: dict[str, Any],
    scores: dict[str, float],
    penalties: dict[str, float],
) -> str:
    observation = str((candidate.get("observations") or [candidate.get("display_title") or "该画面"])[0])
    intent = strategy["narrative_intent"]
    requirement = strategy.get("selection_requirement") or {}
    if function == "KEEP_A_ROLL":
        if intent == "audio_cue":
            return "这是声音或制作指令，应该保留指定原声、同期画面或有意黑场；不能把“原声/有空”等指令文字当作普通画面关键词。"
        return f"这句属于 {intent}，核心由人物表达或声音承担；保留 A-roll 能避免泛化 B-roll 把心理、判断或转折说成画面事实。"
    if evidence_mode == "direct_visible_evidence":
        base = f"这句属于 {intent}；画面“{observation}”能直接承担 {function}，其优势是可见事实和当前句相互对应。"
    elif evidence_mode == "partial_visible_evidence":
        base = f"这句属于 {intent}；画面“{observation}”只能证明其中可见部分，适合短覆盖，身份、心理或因果仍由旁白/A-roll承担。"
    else:
        base = f"这句属于 {intent}；画面“{observation}”主要承担 {function}，不是整句话的事实证明。"
    if penalties.get("generic_broll_penalty"):
        base += " 因本句需要证据而该画面偏泛化，已降低排序。"
    if penalties.get("repetition_penalty"):
        base += " 该原片在前文已使用，已加入重复惩罚。"
    gate = candidate.get("selection_gate") or {}
    profile = candidate.get("candidate_subject_profile") or {}
    requirement = strategy.get("selection_requirement") or {}
    expected = str(gate.get("expected_primary_subject") or requirement.get("expected_primary_subject") or "UNKNOWN")
    actual = str(gate.get("actual_primary_subject") or profile.get("actual_primary_subject") or "UNKNOWN")
    role = str(gate.get("candidate_shot_role") or function or "UNKNOWN")
    base += f" 本句需要的主要主体是 {expected}，该画面主要主体判断为 {actual}，镜头承担 {role}。"
    gate_reasons = [str(value) for value in gate.get("reasons") or []]
    if gate_reasons:
        base += " " + " ".join(gate_reasons[:2])
    if gate.get("requires_source_review"):
        base += " 当前证据不足以确认动态、声音或完整动作，需要回看原片。"
    if gate.get("fact_conflicts"):
        base += " 它不能用来证明与画面事实冲突的部分。"
    elif strategy.get("evidence_required") and evidence_mode != "direct_visible_evidence":
        base += " 它只能支撑画面中可见的部分，不能代替旁白证明身份、心理或因果。"
    if requirement.get("temporal_state") == "STATE_TRANSITION":
        state_names = {
            "GREEN": "绿色", "YELLOW": "黄色/金黄", "RED": "红色", "BLUE": "蓝色",
            "BLACK": "黑色", "WHITE": "白色", "BRIGHT": "明亮", "DARK": "昏暗",
            "EMPTY": "空", "FULL": "满", "WET": "湿", "DRY": "干",
        }
        required_states = set(str(value) for value in requirement.get("required_visual_attributes") or [])
        state_values = (
            gate.get("subject_visual_attributes")
            if "subject_visual_attributes" in gate
            else (candidate.get("candidate_subject_profile") or {}).get("visual_attributes") or []
        )
        candidate_states = set(str(value) for value in state_values or [])
        covered = required_states & candidate_states
        named_states = "、".join(state_names.get(value, value) for value in sorted(covered))
        if required_states and required_states.issubset(candidate_states):
            base += f" 该画面在同一核心对象上同时呈现“{named_states}”，能直接承担状态对照，而不是用相关动作代替变化。"
        elif covered:
            base += f" 该画面只呈现“{named_states}”这一端，适合作为变化前或变化后的单侧镜头，需要与另一状态镜头前后组合。"
    return base + f" 主体匹配 {scores['subject_match']:.0f}，职责匹配 {scores['shot_role_match']:.0f}，证据力 {scores['evidence_strength']:.0f}；美学分不替代这些判断。"


def make_keep_a_roll_candidate(beat: dict[str, Any], track: str) -> dict[str, Any]:
    strategy = beat.get("visual_strategy") or build_visual_strategy(beat, track)
    is_audio_cue = strategy["narrative_intent"] == "audio_cue"
    duration = ((beat.get("p0d_intent") or {}).get("duration_plan") or {}).get("estimated_narration_seconds") or 3.0
    candidate = {
        "candidate_id": f"keep-a-roll::{beat['beat_id']}",
        "source_content_id": "",
        "source_file": "",
        "media_type": "a_roll_placeholder",
        "preview_available": False,
        "preview_absolute_path": "",
        "display_title": "保留原声 / 同期画面" if is_audio_cue else "保留 A-roll / 人物表达",
        "observations": [
            "这是声音或制作指令：保留指定原声、同期画面或有意黑场，不强制插入 B-roll。"
            if is_audio_cue else
            "不强制插入 B-roll，保留人物同期画面、已录旁白主画面或后期指定主叙述镜头。"
        ],
        "pool": "alternative",
        "role": "KEEP_A_ROLL",
        "start_ms": 0,
        "end_ms": int(float(duration) * 1000),
        "anchor_time_ms": None,
        "time_basis": "a_roll_placeholder",
        "match_reasons": [strategy["keep_a_roll"]["reason"]],
        "risks": ["当前尚未指定真实 A-roll 原片，导出时只生成带文稿标记的占位段"],
        "keyframe_analysis": {},
        "evidence_sources": {"qwenvl": False, "ocr": False, "yoloe_propagation": False},
        "score_components": {"retrieval_score": 0.0},
        "editorial_decision": {
            "recommendation": "recommended_for_preview",
            "evidence_mode": "a_roll_primary_carrier",
            "editing_method": (
                "按文稿指令保留原声、同期画面或黑场；不要用指令文字检索泛化 B-roll。"
                if is_audio_cue else
                "保留人物表达；只在确有证据或动作需要时局部插入 B-roll。"
            ),
            "duration": {"provisional_in_ms": 0, "provisional_out_ms": int(float(duration) * 1000)},
            "plain_language_explanation": {
                "sentence_need": strategy["shot_brief"],
                "candidate_contribution": "它保护人物判断、语气和停顿，不让题材相似的画面替人物作证。",
                "fit_reason": strategy["keep_a_roll"]["reason"],
                "visual_language": "这里的选择不是景别更漂亮，而是决定暂时不离开主叙述载体。",
                "fit_boundary": "需要在精剪时指定真实 A-roll、旁白主画面或有意黑场。",
                "acceptance_check": "确认人物表演、同期声或旁白停顿值得保留，并检查前后镜能否自然接回。",
            },
        },
    }
    candidate["cinematic_rerank"] = score_candidate(beat, candidate, track)
    return candidate
