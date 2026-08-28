from __future__ import annotations

from typing import Any


CONTRACT_VERSION = "p2c_profile_interpretation_v1"
PROFILES = {"documentary", "short_video"}


def _field_values(candidate: dict[str, Any], field: str) -> list[str]:
    language = candidate.get("editorial_language") or {}
    row = language.get(field) or {}
    return [str(value) for value in row.get("values") or []]


def interpret_candidate(beat: dict[str, Any], candidate: dict[str, Any], profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unsupported interpretation profile: {profile}")
    role = str(candidate.get("role") or "")
    purpose = str(beat.get("purpose") or "")
    required_roles = [str(value) for value in beat.get("required_roles") or []]
    strengths: list[str] = []
    risks: list[str] = []
    missing: list[str] = []
    uses: list[dict[str, Any]] = []

    def add_use(value: str, reason: str, confidence: float) -> None:
        if value and not any(row["role"] == value for row in uses):
            uses.append({"role": value, "reason": reason, "confidence": confidence})

    if role in required_roles:
        strengths.append(f"候选用途“{role}”与当前段落“{purpose}”需要的镜头角色一致")
        add_use(role, "内容规则与当前文稿段落的角色需求一致", 0.68)
    else:
        risks.append(f"当前候选用途“{role or '未知'}”不是该段落的优先角色，可能只能作为覆盖或替代")

    shot_scale = _field_values(candidate, "shot_scale")
    composition = _field_values(candidate, "composition")
    orientation = _field_values(candidate, "subject_orientation")
    multiframe = candidate.get("multiframe_analysis") or {}
    camera_motion = multiframe.get("camera_motion") or {}
    audio = multiframe.get("audio") or {}
    clean_status = str(candidate.get("clean_status") or multiframe.get("clean_status") or "")

    if shot_scale:
        strengths.append("已有明确景别证据：" + "、".join(shot_scale))
    else:
        missing.append("景别未被专业标注或明确描述")
    if composition:
        strengths.append("已有构图文字证据：" + "、".join(composition))
    else:
        missing.append("构图和主体位置证据不足")
    if orientation:
        strengths.append("已有主体朝向证据：" + "、".join(orientation))
    else:
        missing.append("人物视线或运动方向未知")
    if camera_motion.get("status") == "candidate":
        strengths.append("多帧运动候选：" + str(camera_motion.get("value") or ""))
        risks.append(str(camera_motion.get("reason") or "运动类别仍需人工复核"))
    else:
        missing.append("摄影机运动无法可靠判断")
    if clean_status == "technical_edges_screened":
        strengths.append("候选首尾已经过黑帧和近全白帧技术筛查")
    else:
        missing.append("没有可用的技术 clean 入出点")

    if profile == "documentary":
        if role in {"证据", "动作覆盖", "反应", "建立", "环境/呼吸"}:
            add_use(role, "纪录片可用它补充空间、过程、反应或可见事实", 0.64)
        if audio.get("status") == "observed":
            strengths.append("存在可读音轨，可供人工检查同期声或声音桥")
            risks.append("当前只测得响度，不能声称是讲话、环境声或关键同期声")
        else:
            missing.append("同期声类型与可用性未知")
        risks.append("画面只能证明可见的这一次事件，不能自动证明旁白中的长期趋势、所有权或人物关系")
        decision_basis = "优先看事实证据、空间和过程是否真实支持旁白"
    else:
        if camera_motion.get("value") in {"水平摇移候选", "垂直摇移候选", "手持/不稳定候选", "复合运动候选"}:
            add_use("钩子", "较高画面变化可能承担开场或模式中断，但仍需检查内容信息量", 0.52)
            strengths.append("画面有变化潜力，可测试快节奏剪法")
        else:
            risks.append("当前没有证据证明画面具有足够的变化密度或开场抓力")
        missing.extend(["竖屏主体安全区未测", "字幕留白未测", "音乐节拍与剪点未测"])
        risks.append("“有运动”不等于“有网感”，钩子仍取决于信息、冲突、结果和声音")
        decision_basis = "优先看前几秒的信息变化、动作能量、字幕空间和节拍适配"

    match_strength = str(candidate.get("match_strength") or "")
    if match_strength == "strong":
        strengths.append("素材内容与当前句存在直接文字命中")
    elif match_strength:
        risks.append("素材主要依赖前后文或全文母题，不应当作当前句的直接证据")

    if uses and strengths:
        status = "possible"
    elif uses:
        status = "weak_possible"
    else:
        status = "insufficient_evidence"
    return {
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        "status": status,
        "decision_basis": decision_basis,
        "possible_uses": uses,
        "strengths": list(dict.fromkeys(strengths)),
        "risks": list(dict.fromkeys(risks)),
        "missing_evidence": list(dict.fromkeys(missing)),
        "human_decision_required": True,
        "not_a_quality_score": True,
    }

