from __future__ import annotations

from typing import Any, Iterable


CONTRACT_VERSION = "editorial_language_evidence_v1"

SHOT_SCALE_TERMS = (
    "大远景",
    "中全景",
    "大特写",
    "中近景",
    "远景",
    "全景",
    "中景",
    "近景",
    "特写",
)
CAMERA_ANGLE_TERMS = (
    "鸟瞰",
    "航拍",
    "高机位",
    "低机位",
    "俯拍",
    "仰拍",
    "平视",
    "主观镜头",
    "过肩镜头",
    "倾斜构图",
)
COMPOSITION_TERMS = {
    "对称构图": ("对称构图", "对称"),
    "居中构图": ("居中构图", "居中"),
    "三分构图": ("三分构图", "三分法", "三分"),
    "框架构图": ("框架构图", "框中框"),
    "引导线": ("引导线",),
    "负空间/留白": ("负空间", "留白"),
    "浅景深": ("浅景深",),
    "深景深": ("深景深",),
    "前景遮挡": ("前景遮挡",),
}
CAMERA_MOTION_TERMS = {
    "固定机位": ("固定机位", "固定镜头"),
    "手持": ("手持拍摄", "手持镜头", "手持轻微晃动"),
    "推镜": ("推镜", "推近", "镜头推进"),
    "拉镜": ("拉镜", "拉远", "镜头后拉"),
    "摇镜": ("摇镜", "摇摄"),
    "移镜": ("移镜", "横移", "侧移"),
    "跟拍": ("跟拍", "跟随镜头"),
    "升降": ("升镜", "降镜", "升降镜头"),
    "环绕": ("环绕镜头", "环绕拍摄"),
    "甩镜": ("甩镜", "甩摄"),
    "变焦": ("变焦", "焦距变化"),
}
SUBJECT_ORIENTATION_TERMS = {
    "背对镜头": ("背对镜头", "人物背影"),
    "面向镜头": ("面向镜头", "正对镜头"),
    "侧面": ("人物侧面", "侧脸"),
}


def _explicit_values(text: str, terms: Iterable[str]) -> list[str]:
    values: list[str] = []
    covered: list[str] = []
    for term in terms:
        if term in text and not any(term in longer for longer in covered):
            values.append(term)
            covered.append(term)
    return values


def _mapped_values(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    return [name for name, clues in mapping.items() if any(clue in text for clue in clues)]


def _field(values: list[str], *, evidence: str, unavailable_reason: str) -> dict[str, Any]:
    return {
        "status": "observed" if values else "unknown",
        "values": values,
        "confidence": 0.9 if values else 0.0,
        "evidence": evidence if values else "",
        "unavailable_reason": "" if values else unavailable_reason,
    }


def analyze_editorial_language(text_value: object, *, current_role: str = "") -> dict[str, Any]:
    text = str(text_value or "")
    shot_scale = _explicit_values(text, SHOT_SCALE_TERMS)
    camera_angle = _explicit_values(text, CAMERA_ANGLE_TERMS)
    composition = _mapped_values(text, COMPOSITION_TERMS)
    if "前景" in text and "背景" in text and "前景—背景层次" not in composition:
        composition.append("前景—背景层次")
    camera_motion = _mapped_values(text, CAMERA_MOTION_TERMS)
    subject_orientation = _mapped_values(text, SUBJECT_ORIENTATION_TERMS)

    possible_roles: list[dict[str, Any]] = []

    def add_role(role: str, reason: str, confidence: float) -> None:
        if role and not any(row["role"] == role for row in possible_roles):
            possible_roles.append({"role": role, "reason": reason, "confidence": confidence})

    if current_role:
        add_role(current_role, "来自当前候选内容规则，仍需结合文稿与前后镜头复核", 0.55)
    if any(value in shot_scale for value in ("大远景", "远景", "全景", "中全景")):
        add_role("建立", "明确的宽景别可能交代地点、人物和空间关系", 0.75)
        add_role("环境/呼吸", "宽景别可能为段落提供环境停顿", 0.65)
    if any(value in shot_scale for value in ("近景", "中近景", "特写", "大特写")):
        add_role("插入/细节", "近景或特写可能补充人物、动作或物件细节", 0.75)
    if camera_motion:
        add_role("动作覆盖", "明确的摄影机运动可能覆盖行动过程", 0.65)
        add_role("转场", "摄影机运动可能提供方向或动势转场，但需检查前后镜头", 0.55)

    return {
        "contract_version": CONTRACT_VERSION,
        "observation_source": "existing_text_explicit_terms_only",
        "shot_scale": _field(
            shot_scale,
            evidence=text,
            unavailable_reason="现有描述未明确写出景别",
        ),
        "camera_angle": _field(
            camera_angle,
            evidence=text,
            unavailable_reason="现有描述未明确写出机位角度",
        ),
        "composition": _field(
            composition,
            evidence=text,
            unavailable_reason="现有描述缺少可靠的主体位置或构图术语",
        ),
        "camera_motion": _field(
            camera_motion,
            evidence=text,
            unavailable_reason="单帧描述不能可靠判断推、拉、摇、移、跟拍或稳定性",
        ),
        "subject_orientation": _field(
            subject_orientation,
            evidence=text,
            unavailable_reason="现有描述未明确写出人物朝向",
        ),
        "possible_roles": possible_roles,
        "sequence_context_required": [
            "前后镜头景别变化",
            "视线与运动方向连续性",
            "剪辑节奏和镜头长度",
            "声音桥接与同期声价值",
        ],
    }
