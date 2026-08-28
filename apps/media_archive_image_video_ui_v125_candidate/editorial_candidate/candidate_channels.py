"""Project-neutral, separately inspectable guide and editorial candidate queues."""
from __future__ import annotations
import re
from typing import Any
try:
    from .selection_gate import subject_anchor_matches
except ImportError:
    from selection_gate import subject_anchor_matches

# Normalize observed actions, not a project's nouns/filenames. A description
# mentioning motion is a retrieval cue, not proof from a single still frame.
EVENTS = {
    "摆动": ("摇曳", "晃动", "摆动", "地晃", "飘动", "飘扬", "轻扬", "风吹动"),
    "行走": ("走进", "走入", "行走", "步行", "走过"),
    "触摸": ("触摸", "抚摸", "摸着", "伸手摸"),
    "旋转": ("旋转", "转动", "回转"),
    "流动": ("流动", "流淌", "涌动"),
    "打开": ("打开", "开启", "掀开"),
    "关闭": ("关闭", "关上", "合上"),
    "拿起": ("拿起", "拾起", "捡起"),
    "放下": ("放下", "放回", "放置"),
}


def observed_events(text: str) -> set[str]:
    text = re.sub(r"(?:镜头|相机|摄影机)[^，。；]{0,5}(?:晃动|摆动|旋转)", "", str(text))
    return {label for label, aliases in EVENTS.items() if any(
        any(not re.search(r"(?:不|未|没有|并非|停止).{0,2}$", text[max(0, m.start()-5):m.start()])
            for m in re.finditer(re.escape(alias), text)) for alias in aliases
    )}


def event_subject_terms(text: str) -> list[str]:
    """Conservative leading-subject hint for simple observable-action clauses.

    This is a lexical heuristic, not entity recognition. Complex/pronominal
    clauses stay unknown instead of inventing an actor from the guide.
    """
    result = []
    for clause in re.split(r"[，。；！？,;!?]", text):
        for aliases in EVENTS.values():
            for alias in aliases:
                at = clause.find(alias)
                if at <= 0:
                    continue
                prefix = clause[:at].strip()
                prefix = re.split(r"就|正在|正|在|随|一片|慢慢|缓慢|轻轻|不断|开始", prefix)[0]
                prefix = prefix.rsplit("的", 1)[-1].strip()
                if 2 <= len(prefix) <= 6 and re.fullmatch(r"[\u4e00-\u9fff]+", prefix) and prefix not in {"这个", "那个", "它们", "我们", "你们", "他们", "她们"}:
                    result.append(prefix)
    return list(dict.fromkeys(result))


def make_candidate_channels(beat: dict[str, Any], rows: list[dict[str, Any]], limit: int = 15) -> dict[str, list[dict[str, Any]]]:
    """A guide queue never silently fills with outside-date/file candidates."""
    required_events = observed_events(str(beat.get("text") or ""))
    if required_events:
        # An explicit visible action cannot be replaced by a same-date static
        # view or an unrelated action. Keep those old rows diagnostic-only;
        # absence of a described action is uncertainty, not fabricated proof.
        subjects = event_subject_terms(str(beat.get("text") or ""))
        qualified = []
        for row in rows:
            observation = str((row.get("observations") or [""])[0])
            if not required_events & observed_events(observation) or (subjects and not subject_anchor_matches(subjects, observation)):
                beat.setdefault("channel_diagnostics", []).append({"candidate_id": row["candidate_id"], "reason_code": "OBSERVED_ACTION_OR_SUBJECT_NOT_CONFIRMED"})
                continue
            qualified.append(row)
        rows = qualified
        scope = beat.setdefault("guide_source_search", {})
        scope["message"] = str(scope.get("message") or "") + " 本句要看到：" + "、".join(subjects + sorted(required_events)) + "；推荐组核对已有描述中的主体与动作，仍需回看片段确认。未描述不代表原片一定没有。"
    def local_strength(row):
        score = row.get("score_components") or {}
        return float(score.get("local_score") or 0)

    def system_key(row):
        score = row.get("score_components") or {}
        return (0 if (row.get("selection_gate") or {}).get("gate_status") == "PASS" else 1,
                -local_strength(row), -float((row.get("cinematic_rerank") or {}).get("final_score") or 0),
                -(float(score.get("retrieval_score") or 0) - float(score.get("editorial_guide_score") or 0)), row["candidate_id"])

    scope = beat.get("guide_source_search") or {}
    scoped = scope.get("status") != "DESCRIPTION_ONLY"
    guide_rows = [row for row in rows if beat.get("editorial_guide") and (
        int((row.get("guide_source_match") or {}).get("tier", 4)) < 4 if scoped
        else bool((row.get("score_components") or {}).get("editorial_guide_terms"))
    )]
    guide_rows.sort(key=lambda row: (int((row.get("guide_source_match") or {}).get("tier", 4)), *system_key(row)))
    system_rows = sorted(rows, key=system_key)

    def diverse(ordered):
        selected, sources = [], set()
        for row in ordered:
            source = row.get("source_content_id") or row["candidate_id"]
            if source in sources:
                continue
            sources.add(source); selected.append(row)
            if len(selected) >= limit:
                break
        return selected
    return {"guide": diverse(guide_rows), "system": diverse(system_rows)}
