from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
import re
from collections import Counter, defaultdict
import sys
from typing import Any

try:
    from .cinematic_rerank import build_visual_strategy, make_keep_a_roll_candidate, score_candidate
    from .selection_gate import (
        analyze_visual_requirement,
        derive_candidate_subject,
        expected_subject_terms,
        evaluate_gate,
        is_state_transition,
        subject_anchor_matches,
        subject_visual_attributes,
        visual_attributes,
    )
    from .editorial_guide import apply_editorial_guide
    from .guide_sources import prepare_source_reference_index, resolve_guide_sources
    from .candidate_channels import observed_events, make_candidate_channels
except ImportError:  # Direct-file contract tests.
    _EDITORIAL_ROOT = Path(__file__).resolve().parent
    if str(_EDITORIAL_ROOT) not in sys.path:
        sys.path.insert(0, str(_EDITORIAL_ROOT))
    from cinematic_rerank import build_visual_strategy, make_keep_a_roll_candidate, score_candidate
    from selection_gate import (
        analyze_visual_requirement,
        derive_candidate_subject,
        expected_subject_terms,
        evaluate_gate,
        is_state_transition,
        subject_anchor_matches,
        subject_visual_attributes,
        visual_attributes,
    )
    from editorial_guide import apply_editorial_guide
    from guide_sources import prepare_source_reference_index, resolve_guide_sources
    from candidate_channels import observed_events, make_candidate_channels

try:
    from .profile_interpretation import interpret_candidate
except ModuleNotFoundError:  # Allows the contract tests to load this file directly.
    _profile_path = Path(__file__).with_name("profile_interpretation.py")
    _profile_spec = importlib.util.spec_from_file_location("profile_interpretation", _profile_path)
    if _profile_spec is None or _profile_spec.loader is None:
        raise
    _profile_module = importlib.util.module_from_spec(_profile_spec)
    sys.modules[_profile_spec.name] = _profile_module
    _profile_spec.loader.exec_module(_profile_module)
    interpret_candidate = _profile_module.interpret_candidate

try:
    from .keyframe_evidence import analyze_keyframe, build_neighbor_index, find_neighbor_context
except ModuleNotFoundError:  # Allows the contract tests to load this file directly.
    _keyframe_path = Path(__file__).with_name("keyframe_evidence.py")
    _keyframe_spec = importlib.util.spec_from_file_location("keyframe_evidence", _keyframe_path)
    if _keyframe_spec is None or _keyframe_spec.loader is None:
        raise
    _keyframe_module = importlib.util.module_from_spec(_keyframe_spec)
    sys.modules[_keyframe_spec.name] = _keyframe_module
    _keyframe_spec.loader.exec_module(_keyframe_module)
    analyze_keyframe = _keyframe_module.analyze_keyframe
    build_neighbor_index = _keyframe_module.build_neighbor_index
    find_neighbor_context = _keyframe_module.find_neighbor_context

try:
    from .p0d_editorial_decision import analyze_script_intent, evaluate_candidate
except ModuleNotFoundError:  # Allows the contract tests to load this file directly.
    _decision_path = Path(__file__).with_name("p0d_editorial_decision.py")
    _decision_spec = importlib.util.spec_from_file_location("p0d_editorial_decision", _decision_path)
    if _decision_spec is None or _decision_spec.loader is None:
        raise
    _decision_module = importlib.util.module_from_spec(_decision_spec)
    sys.modules[_decision_spec.name] = _decision_module
    _decision_spec.loader.exec_module(_decision_module)
    analyze_script_intent = _decision_module.analyze_script_intent
    evaluate_candidate = _decision_module.evaluate_candidate

CONTRACT_VERSION = "p0_script_candidate_board_v1"
TRACKS = {"documentary", "short_video"}
POOLS = ("direct", "supplement", "alternative")
DYNAMIC_POOL_RECALL_LIMIT = 30
DYNAMIC_SHORTLIST_LIMIT = 12
DECISIONS = {"unreviewed", "selected", "rejected", "review"}
ROLES = {
    "建立",
    "主叙述",
    "证据",
    "动作覆盖",
    "反应",
    "插入/细节",
    "环境/呼吸",
    "转场",
    "钩子",
    "收束",
}

STOP_TERMS = {
    "一个", "一些", "一样", "不是", "什么", "以前", "以后", "但是", "只是", "可以", "可能",
    "因为", "所以", "如果", "就是", "已经", "还是", "这个", "那个", "这些", "那些", "这里",
    "那里", "然后", "现在", "时候", "觉得", "知道", "来说", "有点", "没有", "自己", "怎么",
    "进行", "开始", "继续", "画面", "视频", "镜头", "素材", "我们", "他们", "你们", "的话",
    "站在", "一片", "不清", "天天", "一下", "结论", "概括", "元素", "检索", "价值",
    "适合", "使用", "具有", "用于", "表现", "记录", "可见", "背景", "其中", "整体",
}
STOP_EDGE_CHARS = set("的一是在了和与或而也都就又还把被给对于着过很更最嘛吗呢啊呀哦")
STOP_LOCAL_CHARS = STOP_EDGE_CHARS | set("人我你他她它家有不说好想会先再这那地中上下前后里边片")
LOW_SIGNAL_TERMS = {
    "金黄", "绿色", "黄色", "白色", "黑色", "红色", "蓝色", "人物", "有人", "一人",
    "活动", "现场", "站立",
    "行走", "移动", "东西", "地方", "农业", "自然", "风光", "自然风光",
}
EXPANSION_EDGE_STOP = STOP_EDGE_CHARS | set("人有片头色中")
BOILERPLATE_FRAGMENTS = ("具有", "适合", "使用", "素材", "价值", "画面", "用于", "可见", "背景", "整体")


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokens(text: str) -> set[str]:
    normalized = _clean_text(text).lower()
    ascii_terms = set(re.findall(r"[a-z0-9_]+", normalized))
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    chinese_terms: set[str] = set()
    for run in chinese_runs:
        chinese_terms.add(run)
        chinese_terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
        chinese_terms.update(run[index : index + 3] for index in range(max(0, len(run) - 2)))
    return ascii_terms | chinese_terms


def _significant_tokens(text: str) -> set[str]:
    return {
        token
        for token in _tokens(text)
        if 2 <= len(token) <= 8
        and token not in STOP_TERMS
        and not token.isdigit()
        and not (re.fullmatch(r"[\u3400-\u9fff]+", token) and (token[0] in STOP_EDGE_CHARS or token[-1] in STOP_EDGE_CHARS))
    }


def _editorial_guide_tokens(beat: dict[str, Any]) -> set[str]:
    guide = beat.get("editorial_guide") or {}
    return _significant_tokens(str(guide.get("retrieval_text") or ""))


def _beat_purpose(text: str, order: int, total: int) -> tuple[str, list[str]]:
    if order == 1:
        return "开场引入", ["钩子", "建立", "主叙述"]
    if (
        "？" in text or "?" in text
        or re.match(r"^(为什么|为何|究竟|到底|怎么(?:会|能|办|回事|可能))", text) is not None
    ):
        return "提出问题", ["钩子", "建立"]
    if any(word in text for word in ("但是", "但", "然而", "反而", "却", "不一样", "越来越少")):
        return "转折与矛盾", ["证据", "反应", "环境/呼吸"]
    if any(word in text for word in ("以前", "曾经", "历史", "三代", "多年", "过去", "那时候", "小时候", "记得")):
        return "回忆与背景", ["证据", "插入/细节", "环境/呼吸"]
    if any(word in text for word in ("说不好", "说不清", "不清楚", "不知道", "愣一下", "心里", "觉得")):
        return "迟疑与内心", ["反应", "环境/呼吸", "插入/细节"]
    if any(word in text for word in ("决定", "开始", "尝试", "换一种", "行动", "想先", "就从", "回来")):
        return "行动与选择", ["主叙述", "动作覆盖", "收束"]
    if any(word in text for word in ("看", "听", "声音", "风", "光", "颜色", "黄起来", "变化", "慢慢", "一天天")):
        return "观察与感受", ["证据", "插入/细节", "环境/呼吸"]
    if any(word in text for word in ("听过一句话", "有人说", "这句话", "意思是", "意思大概")):
        return "引用与命题", ["主叙述", "证据", "钩子"]
    if order == total:
        return "段落收束", ["收束", "证据", "环境/呼吸"]
    return "事实说明", ["主叙述", "证据", "插入/细节"]


def _split_long_chunk(text: str, *, max_chars: int = 42) -> list[str]:
    """Split a long narration sentence only at existing clause punctuation."""

    if len(text) <= max_chars:
        return [text]
    clauses = [part for part in re.split(r"(?<=[，,：:])", text) if part]
    if len(clauses) <= 1:
        return [text]
    rows: list[str] = []
    current = ""
    for clause in clauses:
        if current and len(current) + len(clause) > max_chars:
            rows.append(current)
            current = clause
        else:
            current += clause
    if current:
        rows.append(current)
    return rows


_CHAPTER_CARD_RE = re.compile(
    r"^第\s*[一二三四五六七八九十百千万零〇两0-9]+\s*章"
    r"(?:\s*[:：·—-]\s*.*)?$"
)


def _is_non_narration_chapter_card(text: str) -> bool:
    """Identify a standalone chapter card that should not trigger shot search.

    Chapter cards may still be added later as titles or subtitles in the NLE,
    but they are not narration beats.  Keeping this rule in the common splitter
    makes PDF, TXT and pasted text behave the same way.
    """

    compact = _clean_text(text)
    return len(compact) <= 80 and _CHAPTER_CARD_RE.fullmatch(compact) is not None


def split_script(script: str, track: str = "documentary") -> list[dict[str, Any]]:
    if track not in TRACKS:
        raise ValueError(f"unsupported track: {track}")
    cleaned = script.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise ValueError("script is empty")
    if len(cleaned) > 20_000:
        raise ValueError("script exceeds 20000 characters")

    source_chunks = [
        _clean_text(part)
        for part in re.split(r"(?<=[。！？!?；;])(?![】》」』”’）)])\s*|\n+", cleaned)
        if _clean_text(part) and not _is_non_narration_chapter_card(part)
    ]
    if not source_chunks:
        source_chunks = [cleaned]
    chunks = [part for source in source_chunks for part in _split_long_chunk(source)]

    beats: list[dict[str, Any]] = []
    for index, text in enumerate(chunks, 1):
        purpose, required_roles = _beat_purpose(text, index, len(chunks))
        beats.append(
            {
                "beat_id": f"beat-{index:02d}",
                "order": index,
                "text": text,
                "purpose": purpose,
                "required_roles": required_roles,
                "track": track,
            }
        )
    motif_counts = Counter(token for beat in beats for token in _significant_tokens(beat["text"]))
    motifs = [token for token, count in motif_counts.most_common() if count >= 2 and token not in STOP_TERMS][:12]
    for index, beat in enumerate(beats):
        before = [beats[row]["text"] for row in range(max(0, index - 2), index)]
        after = [beats[row]["text"] for row in range(index + 1, min(len(beats), index + 3))]
        context_rows = [*before, beat["text"], *after]
        beat["context_before"] = before
        beat["context_after"] = after
        beat["sequence_position"] = {
            "order": index + 1,
            "total": len(beats),
            "progress": round((index + 1) / len(beats), 4),
        }
        beat["retrieval_context"] = " ".join(context_rows)
        beat["motif_terms"] = motifs
    return beats


def _validate_candidate(candidate: dict[str, Any]) -> None:
    required = {
        "candidate_id",
        "pool",
        "source_content_id",
        "source_file",
        "start_ms",
        "end_ms",
        "role",
        "observations",
        "evidence",
        "risks",
        "tags",
        "visual",
    }
    missing = sorted(required - set(candidate))
    if missing:
        raise ValueError(f"candidate missing fields: {missing}")
    if candidate["pool"] not in POOLS:
        raise ValueError(f"invalid candidate pool: {candidate['pool']}")
    if candidate["role"] not in ROLES:
        raise ValueError(f"invalid candidate role: {candidate['role']}")
    if not isinstance(candidate["start_ms"], int) or not isinstance(candidate["end_ms"], int):
        raise ValueError("candidate time range must use integer milliseconds")
    if candidate["start_ms"] < 0 or candidate["end_ms"] <= candidate["start_ms"]:
        raise ValueError("candidate time range is invalid")
    for field in ("observations", "evidence", "risks", "tags"):
        if not isinstance(candidate[field], list):
            raise ValueError(f"candidate {field} must be a list")


def validate_project(project: dict[str, Any]) -> None:
    if project.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("unexpected project contract version")
    if project.get("track") not in TRACKS:
        raise ValueError("unexpected project track")
    candidates = project.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("project candidates must be a non-empty list")
    ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("candidate must be an object")
        _validate_candidate(candidate)
        candidate_id = str(candidate["candidate_id"])
        if candidate_id in ids:
            raise ValueError(f"duplicate candidate id: {candidate_id}")
        ids.add(candidate_id)


def _inverse_frequency(term: str, frequencies: Counter[str], document_count: int) -> float:
    return math.log((max(1, document_count) + 1) / (frequencies.get(term, 0) + 1)) + 1


def _discover_corpus_terms(
    beat: dict[str, Any],
    candidate_token_sets: list[set[str]],
    candidate_concept_sets: list[set[str]],
    document_frequency: Counter[str],
    term_postings: dict[str, list[int]] | None = None,
) -> tuple[list[str], str]:
    """Expand a beat from expressions already present in this material library.

    This is corpus feedback, not a project dictionary: exact sentence, adjacent
    context and repeated script motifs find seed documents; terms recurring in
    those documents become bounded second-pass candidates.
    """

    document_count = len(candidate_token_sets)
    local = _significant_tokens(str(beat["text"]))
    local_chars = set(re.findall(r"[\u3400-\u9fff]", str(beat["text"]))) - STOP_LOCAL_CHARS
    context = _significant_tokens(str(beat.get("retrieval_context") or "")) - local
    motifs = set(beat.get("motif_terms") or []) - local - context
    explicit_targets = set(expected_subject_terms(beat, str(beat.get("text") or "")))
    guide_terms = _editorial_guide_tokens(beat)
    query = local | context | motifs | explicit_targets | guide_terms
    local_anchors = {
        term
        for term in local
        if term not in LOW_SIGNAL_TERMS
        and document_frequency.get(term, 0) > 0
        and document_frequency.get(term, 0) <= max(3, int(document_count * 0.35))
    }
    anchors = {
        term
        for term in query
        if term not in LOW_SIGNAL_TERMS
        and document_frequency.get(term, 0) > 0
        and document_frequency.get(term, 0) <= max(3, int(document_count * 0.35))
    }
    if not anchors:
        return [], "none"

    character_frequency: Counter[str] = Counter()
    for tokens in candidate_token_sets:
        character_frequency.update(set("".join(tokens)))

    def character_weight(characters: set[str]) -> float:
        return sum(
            math.log((document_count + 1) / (character_frequency.get(char, 0) + 1)) + 1
            for char in characters
        )

    seed_rows: list[tuple[float, float, set[str], set[str]]] = []
    if term_postings is None:
        candidate_indices = range(len(candidate_token_sets))
    else:
        candidate_indices = sorted({index for term in anchors for index in term_postings.get(term, [])})
    for index in candidate_indices:
        tokens = candidate_token_sets[index]
        overlap = anchors & tokens
        if not overlap:
            continue
        concepts = candidate_concept_sets[index] or tokens
        meaningful_concepts = {
            term for term in concepts
            if not any(low_signal in term for low_signal in LOW_SIGNAL_TERMS)
            and not any(fragment in term for fragment in BOILERPLATE_FRAGMENTS)
        }
        candidate_chars = set("".join(meaningful_concepts))
        character_affinity = character_weight(local_chars & candidate_chars)
        score = sum(_inverse_frequency(term, document_frequency, document_count) for term in overlap)
        score += character_affinity * 1.1
        seed_rows.append((score, character_affinity, tokens, concepts))
    by_score = sorted(seed_rows, key=lambda row: -row[0])
    by_character = sorted(seed_rows, key=lambda row: (-row[1], -row[0]))
    seed_sets: list[set[str]] = []
    seen_seed_ids: set[int] = set()
    for _, _, tokens, concepts in by_character[:6] + by_score[:24]:
        token_set_id = id(tokens)
        if token_set_id in seen_seed_ids:
            continue
        seen_seed_ids.add(token_set_id)
        seed_sets.append(concepts)
        if len(seed_sets) == 24:
            break
    if not seed_sets:
        return [], "none"

    support = Counter(term for tokens in seed_sets for term in tokens)
    priority_support = Counter(term for tokens in seed_sets[:6] for term in tokens)
    minimum_support = 2 if len(seed_sets) >= 4 else 1
    ranked: list[tuple[float, str]] = []
    for term, count in support.items():
        corpus_count = document_frequency.get(term, 0)
        if term in query or term in STOP_TERMS or term in LOW_SIGNAL_TERMS:
            continue
        if any(fragment in term for fragment in BOILERPLATE_FRAGMENTS):
            continue
        if "的" in term:
            continue
        if term[0] in EXPANSION_EDGE_STOP or term[-1] in EXPANSION_EDGE_STOP:
            continue
        if any(low_signal in term for low_signal in LOW_SIGNAL_TERMS):
            continue
        if any(anchor in term or term in anchor for anchor in query if len(anchor) >= 2):
            continue
        character_overlap = local_chars & set(term)
        if count < minimum_support and not (priority_support.get(term, 0) > 0 and character_overlap):
            continue
        if corpus_count <= 0:
            continue
        if corpus_count > max(4, int(document_count * 0.20)):
            continue
        conditional_specificity = count / corpus_count
        character_affinity = character_weight(character_overlap)
        score = count * _inverse_frequency(term, document_frequency, document_count) * conditional_specificity
        score *= 1.0 + character_affinity * 1.6
        ranked.append((score, term))
    ranked.sort(key=lambda row: (-row[0], -len(row[1]), row[1]))
    supported = {term: support[term] for _, term in ranked}
    reduced: list[str] = []
    for _, term in ranked:
        if any(
            term != other
            and term in other
            and supported.get(other, 0) >= supported[term]
            for other in supported
        ):
            continue
        reduced.append(term)
        if len(reduced) == 12:
            break
    bridged_characters = set().union(*(local_chars & set(term) for term in reduced)) if reduced else set()
    has_local_bridge = len(bridged_characters) >= 2
    return reduced, ("local" if local_anchors or has_local_bridge else "context")


def _candidate_searchable(candidate: dict[str, Any]) -> str:
    return " ".join(
        [
            str(candidate.get("role") or ""),
            " ".join(candidate.get("tags") or []),
            " ".join(candidate.get("observations") or []),
            " ".join(candidate.get("evidence") or []),
            str(candidate.get("searchable_text") or ""),
        ]
    )


def _candidate_character_set(candidate: dict[str, Any]) -> set[str]:
    character_text = " ".join(
        [
            " ".join(candidate.get("concept_terms") or []),
            str((candidate.get("observations") or [""])[0]),
        ]
    )
    for low_signal in LOW_SIGNAL_TERMS:
        character_text = character_text.replace(low_signal, "")
    return set(re.findall(r"[\u3400-\u9fff]", character_text))


def prepare_corpus(project: dict[str, Any]) -> dict[str, Any]:
    validate_project(project)
    candidates = copy.deepcopy(project["candidates"])
    document_frequency: Counter[str] = Counter()
    candidate_token_sets: list[set[str]] = []
    candidate_concept_sets: list[set[str]] = []
    candidate_character_sets: list[set[str]] = []
    term_postings: defaultdict[str, list[int]] = defaultdict(list)
    for candidate_index, candidate in enumerate(candidates):
        searchable = _candidate_searchable(candidate)
        tokens = _significant_tokens(searchable)
        tokens |= observed_events(str((candidate.get("observations") or [""])[0]))
        candidate_token_sets.append(tokens)
        for token in tokens:
            term_postings[token].append(candidate_index)
        for attribute in visual_attributes(searchable):
            term_postings[f"__visual_attribute__:{attribute}"].append(candidate_index)
        concept_text = " ".join(
            [
                " ".join(candidate.get("concept_terms") or []),
                str((candidate.get("observations") or [""])[0]),
                " ".join(candidate.get("tags") or []),
            ]
        )
        candidate_concept_sets.append(_significant_tokens(concept_text))
        candidate_character_sets.append(_candidate_character_set(candidate))
        document_frequency.update(tokens)
    return {
        "contract_version": CONTRACT_VERSION,
        "source_mode": str(project.get("source_mode") or "fixture_read_only"),
        "candidates": candidates,
        "document_frequency": document_frequency,
        "candidate_token_sets": candidate_token_sets,
        "candidate_concept_sets": candidate_concept_sets,
        "candidate_character_sets": candidate_character_sets,
        "term_postings": dict(term_postings),
        "source_reference_index": prepare_source_reference_index(candidates),
    }


def _candidate_indices_for_beat(
    beat: dict[str, Any],
    term_postings: dict[str, list[int]],
) -> list[int]:
    local = _significant_tokens(str(beat.get("text") or ""))
    local |= observed_events(str(beat.get("text") or ""))
    inferred = set(beat.get("corpus_expansion_terms") or [])
    context = _significant_tokens(str(beat.get("retrieval_context") or ""))
    motifs = set(beat.get("motif_terms") or [])
    guide_terms = _editorial_guide_tokens(beat)
    attributes = {f"__visual_attribute__:{value}" for value in visual_attributes(str(beat.get("text") or ""))}
    explicit_targets = set(expected_subject_terms(beat, str(beat.get("text") or "")))
    query_terms = local | inferred | context | motifs | attributes | explicit_targets | guide_terms
    return sorted({index for term in query_terms for index in term_postings.get(term, [])})


def _candidate_score(
    beat: dict[str, Any],
    candidate: dict[str, Any],
    track: str,
    document_frequency: Counter[str] | None = None,
    document_count: int = 1,
    candidate_tokens: set[str] | None = None,
    candidate_characters: set[str] | None = None,
) -> tuple[float, list[str], dict[str, Any]]:
    searchable = _candidate_searchable(candidate)
    original_local_tokens = _significant_tokens(beat["text"])
    original_local_tokens |= observed_events(beat["text"])
    expansion_scope = str(beat.get("corpus_expansion_scope") or "none")
    inferred_tokens = set(beat.get("corpus_expansion_terms") or []) - original_local_tokens
    local_inferred_tokens = inferred_tokens if expansion_scope == "local" else set()
    context_inferred_tokens = inferred_tokens if expansion_scope == "context" else set()
    context_tokens = _significant_tokens(
        str(beat.get("retrieval_context") or beat["text"])
    ) - original_local_tokens - inferred_tokens
    context_tokens |= context_inferred_tokens
    motif_tokens = set(beat.get("motif_terms") or []) - original_local_tokens - inferred_tokens - context_tokens
    guide_tokens = _editorial_guide_tokens(beat) - original_local_tokens - inferred_tokens
    candidate_tokens = candidate_tokens if candidate_tokens is not None else _significant_tokens(searchable)
    beat_text = str(beat.get("text") or "")
    required_attributes = set(visual_attributes(beat_text))
    candidate_attributes = set(visual_attributes(searchable))
    expected_terms = expected_subject_terms(beat, beat_text)
    factual_observation = " ".join(dict.fromkeys([
        str(candidate.get("display_title") or "").strip(),
        *[
            str(value).strip()
            for value in (candidate.get("observations") or [])[:2]
            if str(value).strip()
        ],
    ])).strip()
    candidate_subject_attributes = set(
        subject_visual_attributes(expected_terms, factual_observation)
        if expected_terms else candidate_attributes
    )
    attribute_overlap = sorted(required_attributes & candidate_subject_attributes)
    subject_anchor_terms = subject_anchor_matches(
        expected_terms,
        searchable,
    )
    local_characters = set(re.findall(r"[\u3400-\u9fff]", str(beat["text"]))) - STOP_LOCAL_CHARS
    candidate_characters = candidate_characters if candidate_characters is not None else _candidate_character_set(candidate)
    local_character_overlap = sorted(local_characters & candidate_characters)
    original_local_overlap = sorted(original_local_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    inferred_overlap = sorted(inferred_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    local_inferred_overlap = sorted(local_inferred_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    local_overlap = sorted(
        set(original_local_overlap) | set(local_inferred_overlap),
        key=lambda value: (-len(value), value),
    )
    context_overlap = sorted(context_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    motif_overlap = sorted(motif_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    guide_overlap = sorted(guide_tokens & candidate_tokens, key=lambda value: (-len(value), value))
    frequencies = document_frequency or Counter()

    def weighted(terms: list[str], multiplier: float) -> float:
        total = 0.0
        for term in terms:
            inverse_frequency = _inverse_frequency(term, frequencies, document_count)
            total += min(6.0, inverse_frequency) * (1.25 if len(term) >= 3 else 1.0) * multiplier
        return total

    attribute_score = len(attribute_overlap) * 9.0
    if len(required_attributes) >= 2 and required_attributes.issubset(candidate_attributes):
        attribute_score += 8.0
    subject_anchor_score = min(12.0, len(subject_anchor_terms) * 7.0)
    local_score = (
        weighted(original_local_overlap, 2.2)
        + weighted(local_inferred_overlap, 1.35)
        + attribute_score
        + subject_anchor_score
    )
    context_score = weighted(context_overlap, 0.85)
    motif_score = weighted(motif_overlap, 0.55)
    guide_score = weighted(guide_overlap, 1.45)
    has_semantic_match = bool(local_overlap or context_overlap or motif_overlap or guide_overlap)
    role_bonus = 1.25 if has_semantic_match and candidate["role"] in beat["required_roles"] else 0.0
    preferred_tracks = set(candidate.get("preferred_tracks") or [])
    track_bonus = 0.4 if has_semantic_match and track in preferred_tracks else 0.0
    risk_penalty = min(1.2, len(candidate.get("risks") or []) * 0.2)
    score = float(local_score + context_score + motif_score + guide_score + role_bonus + track_bonus - risk_penalty)

    if local_score >= 5.0:
        strength = "strong"
    elif local_score > 0 or context_score >= 2.5 or guide_score > 0:
        strength = "contextual"
    elif motif_score > 0:
        strength = "fallback"
    else:
        strength = "none"

    reasons: list[str] = []
    if original_local_overlap:
        reasons.append("当前句直接命中：" + "、".join(original_local_overlap[:5]))
    if attribute_overlap:
        reasons.append("画面状态命中：" + "、".join(attribute_overlap))
    if subject_anchor_terms:
        reasons.append("画面主体命中：" + "、".join(subject_anchor_terms[:3]))
    if inferred_overlap:
        prefix = "由当前句在素材库中扩展" if expansion_scope == "local" else "由前后文在素材库中扩展"
        reasons.append(prefix + "：" + "、".join(inferred_overlap[:5]))
    if context_overlap:
        reasons.append("借用前后句语境：" + "、".join(context_overlap[:4]))
    if motif_overlap:
        reasons.append("延续全文母题：" + "、".join(motif_overlap[:4]))
    if guide_overlap:
        reasons.append("命中本项目剪辑指导：" + "、".join(guide_overlap[:5]))
    if role_bonus:
        reasons.append("可承担剪辑角色：" + candidate["role"])
    if strength == "none":
        reasons.append("没有可靠的文字或母题命中，不应作为直述画面")
    return score, reasons, {
        "match_strength": strength,
        "local_terms": local_overlap,
        "original_local_terms": original_local_overlap,
        "corpus_expansion_terms": inferred_overlap,
        "corpus_expansion_scope": expansion_scope,
        "local_character_overlap": local_character_overlap,
        "visual_attribute_terms": attribute_overlap,
        "visual_attribute_score": round(attribute_score, 3),
        "subject_visual_attribute_terms": sorted(candidate_subject_attributes),
        "subject_anchor_terms": subject_anchor_terms,
        "subject_anchor_score": round(subject_anchor_score, 3),
        "context_terms": context_overlap,
        "motif_terms": motif_overlap,
        "editorial_guide_terms": guide_overlap,
        "local_score": round(local_score, 3),
        "context_score": round(context_score, 3),
        "motif_score": round(motif_score, 3),
        "editorial_guide_score": round(guide_score, 3),
    }


def _take_diverse(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    excluded_ids: set[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    sources: defaultdict[str, int] = defaultdict(int)
    for max_per_source in (1, 2):
        for row in rows:
            candidate_id = str(row["candidate_id"])
            source_id = str(row["source_content_id"])
            if candidate_id in excluded_ids or any(item["candidate_id"] == candidate_id for item in selected):
                continue
            if sources[source_id] >= max_per_source:
                continue
            selected.append(row)
            sources[source_id] += 1
            if len(selected) == limit:
                excluded_ids.update(str(item["candidate_id"]) for item in selected)
                return selected
    excluded_ids.update(str(item["candidate_id"]) for item in selected)
    return selected


def _prioritize_state_endpoints(
    rows: list[dict[str, Any]],
    required_attributes: set[str],
) -> list[dict[str, Any]]:
    """Move useful single-state endpoints ahead of generic colour matches.

    A transition can be edited from two complementary shots.  Retrieval must
    therefore keep at least one candidate for each visible endpoint instead of
    letting a large number of visually similar end-state shots fill the pool.
    """
    if len(required_attributes) < 2:
        return rows
    buckets: dict[str, list[dict[str, Any]]] = {}
    for attribute in sorted(required_attributes):
        buckets[attribute] = [
            row for row in rows
            if attribute in set(row.get("score_components", {}).get("subject_visual_attribute_terms") or [])
            and not required_attributes.issubset(
                set(row.get("score_components", {}).get("subject_visual_attribute_terms") or [])
            )
            and bool(row.get("score_components", {}).get("subject_anchor_terms"))
        ][:8]
    seeds: list[dict[str, Any]] = []
    for bucket_index in range(8):
        for attribute in sorted(required_attributes):
            bucket = buckets[attribute]
            if bucket_index < len(bucket) and bucket[bucket_index] not in seeds:
                seeds.append(bucket[bucket_index])
    return [*seeds, *(row for row in rows if row not in seeds)]


def _editorial_shortlist(
    beat: dict[str, Any],
    pools: dict[str, list[dict[str, Any]]],
    track: str,
    neighbor_index: dict[str, list[dict[str, Any]]],
    *,
    limit: int = 3,
    prior_source_usage: Counter[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Keep at most three usable choices across all semantic pools.

    Pool names explain how a candidate relates to the sentence. They are not
    three independent quotas and do not replace the editor's final decision.
    """

    recommendation_order = {
        "recommended_for_preview": 0,
        "borderline_for_preview": 1,
        "exclude_from_editor_view": 2,
    }
    evidence_order = {
        "a_roll_primary_carrier": 0,
        "direct_visible_evidence": 0,
        "partial_visible_evidence": 1,
        "contextual_visible_support": 2,
        "contextual_carrier_only": 3,
        "insufficient_evidence": 4,
    }
    pool_order = {"direct": 0, "supplement": 1, "alternative": 2}
    rows: list[dict[str, Any]] = []
    beat["p0d_intent"] = analyze_script_intent(beat, track)
    beat["visual_strategy"] = build_visual_strategy(beat, track)
    beat["selection_requirement"] = analyze_visual_requirement(beat, track)
    beat["visual_strategy"]["selection_requirement"] = beat["selection_requirement"]
    beat["gate_diagnostics"] = []
    beat["a_roll_option"] = None
    source_usage = prior_source_usage or Counter()
    for pool in POOLS:
        for candidate in pools[pool]:
            keyframe = analyze_keyframe(candidate)
            keyframe["neighbor_context"] = find_neighbor_context(candidate, neighbor_index)
            candidate["keyframe_analysis"] = keyframe
            candidate["editorial_decision"] = evaluate_candidate(beat, candidate, track)
            candidate["candidate_subject_profile"] = derive_candidate_subject(candidate)
            candidate["selection_gate"] = evaluate_gate(
                beat["selection_requirement"],
                candidate,
                candidate["candidate_subject_profile"],
            )
            if candidate["editorial_decision"]["recommendation"] == "exclude_from_editor_view":
                diagnostic = copy.deepcopy(candidate["selection_gate"])
                diagnostic["gate_status"] = "HARD_GATE"
                diagnostic["diagnostic_only"] = True
                diagnostic["gate_penalty"] = max(100.0, float(diagnostic.get("gate_penalty") or 0.0))
                diagnostic["reason_codes"] = list(dict.fromkeys([
                    *(diagnostic.get("reason_codes") or []),
                    "SEMANTIC_ONLY",
                ]))
                diagnostic["reasons"] = list(diagnostic.get("reasons") or []) + [
                    "现有剪辑证据层已判定该候选不应进入编辑候选区。",
                ]
                candidate["selection_gate"] = diagnostic
                beat["gate_diagnostics"].append(candidate)
                continue
            if candidate["selection_gate"]["gate_status"] == "HARD_GATE":
                beat["gate_diagnostics"].append(candidate)
                continue
            candidate["cinematic_rerank"] = score_candidate(
                beat,
                candidate,
                track,
                prior_source_usage=source_usage,
            )
            rows.append(candidate)

    if beat.get("comparison_mode"):
        beat["candidate_groups"] = make_candidate_channels(beat, rows)
    if beat.get("editorial_guide"):
        scope = beat.get("guide_source_search") or {}
        for name, tiers in (("primary", {0, 1}), ("alternative", {2, 3})):
            scope[name + "_eligible_source_count"] = len({
                row["source_content_id"] for row in (beat["candidate_groups"]["guide"] if beat.get("comparison_mode") else rows)
                if int((row.get("guide_source_match") or {}).get("tier", 4)) in tiers
            })
        scope["message"] = str(scope.get("message") or "") + (
            f" 当前推荐队列：主用范围 {scope['primary_eligible_source_count']} 个、"
            f"替补范围 {scope['alternative_eligible_source_count']} 个原文件（仍需人工复核）。"
        )
        beat["guide_source_search"] = scope

    # Availability is an editor's right, not an intent-classifier verdict.
    # The strategy still distinguishes recommended from merely optional.
    beat["a_roll_option"] = make_keep_a_roll_candidate(beat, track)

    rows.sort(
        key=lambda row: (
            0 if (row.get("selection_gate") or {}).get("gate_status") == "PASS" else 1,
            int((row.get("guide_source_match") or {}).get("tier", 4)),
            -float((row.get("cinematic_rerank") or {}).get("final_score") or 0.0),
            recommendation_order[row["editorial_decision"]["recommendation"]],
            evidence_order[row["editorial_decision"]["evidence_mode"]],
            int(source_usage.get(str(row.get("source_content_id") or ""), 0)),
            -float(row.get("score_components", {}).get("retrieval_score") or 0.0),
            pool_order.get(str(row.get("pool")), 9),
            str(row.get("candidate_id") or ""),
        )
    )
    selected: list[dict[str, Any]] = []
    source_counts: defaultdict[str, int] = defaultdict(int)

    requirement = beat.get("selection_requirement") or {}
    required_states = set(str(value) for value in requirement.get("required_visual_attributes") or [])
    if requirement.get("temporal_state") == "STATE_TRANSITION" and len(required_states) >= 2:
        # Rank one best overall candidate first, followed by complementary
        # single-state endpoints when the library contains them.  This gives
        # an editor both a one-shot option and a before/after construction.
        if rows:
            selected.append(rows[0])
            source_counts[str(rows[0].get("source_content_id") or rows[0].get("candidate_id") or "")] += 1
        for state in sorted(required_states):
            endpoint = next((
                row for row in rows
                if row not in selected
                and state in set((row.get("selection_gate") or {}).get("subject_visual_attributes") or [])
                and not required_states.issubset(
                    set((row.get("selection_gate") or {}).get("subject_visual_attributes") or [])
                )
                and "SUBJECT_ANCHOR_MISMATCH" not in set((row.get("selection_gate") or {}).get("reason_codes") or [])
                and source_counts[str(row.get("source_content_id") or row.get("candidate_id") or "")] == 0
            ), None)
            if endpoint is not None:
                selected.append(endpoint)
                source_counts[str(endpoint.get("source_content_id") or endpoint.get("candidate_id") or "")] += 1
            if len(selected) == limit:
                break
    for max_per_source in (1, 2, 3):
        for row in rows:
            if row in selected:
                continue
            source_id = str(row.get("source_content_id") or row.get("candidate_id") or "")
            if source_counts[source_id] >= max_per_source:
                continue
            selected.append(row)
            source_counts[source_id] += 1
            if len(selected) == limit:
                break
        if len(selected) == limit:
            break
    result: dict[str, list[dict[str, Any]]] = {pool: [] for pool in POOLS}
    for shortlist_rank, row in enumerate(selected, 1):
        row["shortlist_rank"] = shortlist_rank
        gate = row.get("selection_gate") or {}
        gate["shortlist_rank"] = shortlist_rank
        gate["rank_reason"] = (
            "主体、事实边界和镜头职责通过前置门控后，按视听适配、证据、技术与序列分排序。"
            if gate.get("gate_status") == "PASS"
            else "该候选存在可解释的主体、职责或证据限制，因此仅在合格候选之后参与排序。"
        )
        result[str(row["pool"])].append(row)
    beat["gap_status"] = {
        "available": True,
        "recommended": not bool(selected),
        "candidate_slots_consumed": 0,
        "reason": (
            "当前没有通过门控的真实素材，时间线可保留文稿缺口，等待补拍或精剪处理。"
            if not selected else
            "缺口始终可作为人工选择，但不会占用三个真实素材候选名额。"
        ),
    }
    return result


def build_board(
    script: str,
    project: dict[str, Any],
    track: str | None = None,
    *,
    prepared_corpus: dict[str, Any] | None = None,
    editorial_guide: dict[str, Any] | None = None,
    target_beat_id: str | None = None,
    reserved_visuals: list[dict[str, Any]] | None = None,
    comparison_mode: bool = False,
    dismissed_candidate_ids: list[str] | None = None,
    bound_guidance: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_project(project)
    selected_track = track or str(project["track"])
    ignored_chapter_cards = [
        _clean_text(line)
        for line in script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if _is_non_narration_chapter_card(line)
    ]
    beats = split_script(script, selected_track)
    guide_summary = apply_editorial_guide(beats, editorial_guide) if editorial_guide else None
    if bound_guidance and not editorial_guide:
        if [(b["beat_id"], b["text"]) for b in beats] != [(b.get("beat_id"), b.get("text")) for b in bound_guidance]:
            raise ValueError("工程句序与文稿不一致，不能套用已保存指导；请保留原工程")
        for beat, saved in zip(beats, bound_guidance):
            guidance = copy.deepcopy(saved.get("project_editorial_guidance") or {})
            if guidance:
                guidance["retrieval_text"] = " ".join(str(guidance.get(k) or "") for k in ("visual_direction", "primary_shot", "alternative_shot"))
                beat["editorial_guide"] = guidance
        count = sum(bool(b.get("editorial_guide")) for b in beats)
        guide_summary = {"source_file":"工程内嵌指导", "sheet_name":"保存时逐句对应", "guide_row_count":count, "matched_beat_count":count, "unmatched_beat_count":len(beats)-count}
    if target_beat_id is not None:
        beats = [beat for beat in beats if beat["beat_id"] == target_beat_id]
        if not beats:
            raise ValueError("文稿已变化，无法定位待更新句；请重新生成候选")
    corpus = prepared_corpus or prepare_corpus(project)
    if corpus.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("unexpected prepared corpus contract version")
    source_candidates = corpus["candidates"]
    dynamic_pools = project.get("source_mode") == "real_database_read_only"
    document_frequency: Counter[str] = corpus["document_frequency"]
    candidate_token_sets: list[set[str]] = corpus["candidate_token_sets"]
    candidate_concept_sets: list[set[str]] = corpus["candidate_concept_sets"]
    candidate_character_sets: list[set[str]] = corpus["candidate_character_sets"]
    term_postings: dict[str, list[int]] = corpus.get("term_postings") or {}
    neighbor_index = build_neighbor_index([{"candidates": source_candidates}]) if dynamic_pools else {}

    if dynamic_pools:
        for beat in beats:
            if is_state_transition(str(beat.get("text") or "")):
                # Attribute transitions already have explicit visual anchors.
                # Free corpus expansion can turn a state word (for example a
                # colour) into an unrelated object/action association.
                expansion_terms, expansion_scope = [], "none"
            else:
                expansion_terms, expansion_scope = _discover_corpus_terms(
                    beat,
                    candidate_token_sets,
                    candidate_concept_sets,
                    document_frequency,
                    term_postings,
                )
            beat["corpus_expansion_terms"] = expansion_terms
            beat["corpus_expansion_scope"] = expansion_scope

    board_beats: list[dict[str, Any]] = []
    displayed_source_usage: Counter[str] = Counter(str(row.get("source_content_id") or "") for row in reserved_visuals or [])
    def occupied(candidate: dict[str, Any]) -> bool:
        for used in reserved_visuals or []:
            if not candidate.get("source_content_id") or candidate.get("source_content_id") != used.get("source_content_id"):
                continue
            if candidate.get("media_type") == "image" or used.get("media_type") == "image":
                return True
            if max(int(candidate.get("start_ms") or 0), int(used.get("start_ms") or 0)) < min(int(candidate.get("end_ms") or 0), int(used.get("end_ms") or 0)):
                return True
            if candidate.get("anchor_time_ms") is not None and used.get("anchor_time_ms") is not None and abs(int(candidate["anchor_time_ms"]) - int(used["anchor_time_ms"])) <= 5000:
                return True
        return False
    for beat in beats:
        beat["comparison_mode"] = comparison_mode
        ranked_by_pool: dict[str, list[dict[str, Any]]] = {pool: [] for pool in POOLS}
        all_ranked: list[dict[str, Any]] = []
        source_matches, source_summary = resolve_guide_sources(
            beat.get("editorial_guide"),
            corpus.get("source_reference_index") or prepare_source_reference_index(source_candidates),
            source_candidates,
        )
        beat["guide_source_search"] = source_summary
        candidate_indices = (
            _candidate_indices_for_beat(beat, term_postings)
            if dynamic_pools and term_postings
            else list(range(len(source_candidates)))
        )
        candidate_indices = sorted(set(candidate_indices) | set(source_matches))
        before_exclusion = len(candidate_indices)
        dismissed = set(dismissed_candidate_ids or [])
        candidate_indices = [index for index in candidate_indices if not occupied(source_candidates[index]) and source_candidates[index]["candidate_id"] not in dismissed]
        beat["excluded_visual_count"] = before_exclusion - len(candidate_indices)
        beat["retrieval_candidate_count"] = len(candidate_indices)
        beat["retrieval_source_count"] = len({
            str(source_candidates[index].get("source_content_id") or "")
            for index in candidate_indices
        })
        for candidate_index in candidate_indices:
            candidate = source_candidates[candidate_index]
            score, reasons, match = _candidate_score(
                beat,
                candidate,
                selected_track,
                document_frequency,
                len(source_candidates),
                candidate_token_sets[candidate_index],
                candidate_character_sets[candidate_index],
            )
            # Keep the per-beat ranking row small. A full candidate can contain
            # long descriptions and sidecars; copying every candidate for every
            # sentence makes full-project scripts needlessly consume gigabytes.
            enriched = {
                "_candidate_index": candidate_index,
                "candidate_id": candidate["candidate_id"],
                "source_content_id": candidate["source_content_id"],
                "pool": candidate["pool"],
                "role": candidate["role"],
                "evidence_sources": candidate.get("evidence_sources", {}),
            }
            enriched["score_components"] = {
                "retrieval_score": score,
                "risk_count": len(candidate.get("risks") or []),
                **match,
            }
            enriched["match_reasons"] = reasons
            enriched["match_strength"] = match["match_strength"]
            enriched["decision"] = "unreviewed"
            enriched["guide_source_match"] = source_matches.get(candidate_index, {"tier": 4, "scope": "outside", "label": "库内其他范围"})
            if candidate_index in source_matches:
                enriched["match_reasons"].append("按项目逐句表定位：" + source_matches[candidate_index]["label"] + "；日期/编号不是画面内容证明")
            enriched["profile_interpretation"] = interpret_candidate(beat, enriched, selected_track)
            all_ranked.append(enriched)

        all_ranked.sort(
            key=lambda row: (int(row["guide_source_match"]["tier"]), -float(row["score_components"]["retrieval_score"]), row["candidate_id"])
        )
        if dynamic_pools:
            used: set[str] = set()

            def has_visual_anchor(row: dict[str, Any]) -> bool:
                terms = (
                    row["score_components"]["local_terms"]
                    + row["score_components"]["context_terms"]
                    + row["score_components"]["motif_terms"]
                    + row["score_components"].get("editorial_guide_terms", [])
                )
                return bool(
                    row["score_components"].get("visual_attribute_terms")
                    or row["score_components"].get("subject_anchor_terms")
                    or row["score_components"].get("editorial_guide_terms")
                ) or any(
                    term not in LOW_SIGNAL_TERMS for term in terms
                )

            def has_visual_evidence(row: dict[str, Any]) -> bool:
                sources = row.get("evidence_sources", {})
                return bool(sources.get("qwenvl", True) or sources.get("yoloe_propagation", False))

            direct_rows = [
                row for row in all_ranked
                if row["match_strength"] == "strong"
                and (
                    row["score_components"]["local_terms"]
                    or row["score_components"].get("visual_attribute_terms")
                    or row["score_components"].get("subject_anchor_terms")
                    or row["score_components"].get("editorial_guide_terms")
                )
                and (
                    row["score_components"]["original_local_terms"]
                    or row["score_components"].get("visual_attribute_terms")
                    or row["score_components"].get("subject_anchor_terms")
                    or (
                        row["score_components"]["corpus_expansion_scope"] == "local"
                        and len(row["score_components"]["corpus_expansion_terms"]) >= 2
                        and len(row["score_components"]["local_character_overlap"]) >= 2
                    )
                )
                and bool(row.get("evidence_sources", {}).get("qwenvl", True))
                and has_visual_anchor(row)
            ]
            direct_rows.sort(
                key=lambda row: (
                    -float(row["score_components"]["local_score"]),
                    -float(row["score_components"]["retrieval_score"]),
                    row["candidate_id"],
                )
            )
            if direct_rows:
                best_local_score = float(direct_rows[0]["score_components"]["local_score"])
                direct_rows = [
                    row for row in direct_rows
                    if float(row["score_components"]["local_score"]) >= max(5.0, best_local_score * 0.55)
                ]
                direct_rows.sort(
                    key=lambda row: (
                        int(row["guide_source_match"]["tier"]),
                        int(displayed_source_usage.get(str(row.get("source_content_id") or ""), 0)),
                        -float(row["score_components"]["local_score"]),
                        -float(row["score_components"]["retrieval_score"]),
                        row["candidate_id"],
                    )
                )
                if is_state_transition(str(beat.get("text") or "")):
                    direct_rows = _prioritize_state_endpoints(
                        direct_rows,
                        set(visual_attributes(str(beat.get("text") or ""))),
                    )
            ranked_by_pool["direct"] = _take_diverse(
                direct_rows, limit=DYNAMIC_POOL_RECALL_LIMIT, excluded_ids=used,
            )
            supplement_rows = [
                row for row in all_ranked
                if row["match_strength"] in {"strong", "contextual"}
                and (
                    row["score_components"]["local_terms"]
                    or row["score_components"]["context_terms"]
                    or row["score_components"].get("visual_attribute_terms")
                    or row["score_components"].get("subject_anchor_terms")
                    or row["score_components"].get("editorial_guide_terms")
                )
                and has_visual_evidence(row)
                and has_visual_anchor(row)
            ]
            supplement_rows.sort(
                key=lambda row: (
                    int(row["guide_source_match"]["tier"]),
                    int(displayed_source_usage.get(str(row.get("source_content_id") or ""), 0)),
                    -float(row["score_components"]["retrieval_score"]),
                    row["candidate_id"],
                )
            )
            if is_state_transition(str(beat.get("text") or "")):
                supplement_rows = _prioritize_state_endpoints(
                    supplement_rows,
                    set(visual_attributes(str(beat.get("text") or ""))),
                )
            ranked_by_pool["supplement"] = _take_diverse(
                supplement_rows, limit=DYNAMIC_POOL_RECALL_LIMIT, excluded_ids=used,
            )
            alternative_rows = [
                row for row in all_ranked
                if row["match_strength"] in {"contextual", "fallback"}
                and (
                    row["score_components"]["local_terms"]
                    or row["score_components"]["context_terms"]
                    or row["score_components"]["motif_terms"]
                    or row["score_components"].get("visual_attribute_terms")
                    or row["score_components"].get("subject_anchor_terms")
                    or row["score_components"].get("editorial_guide_terms")
                )
                and has_visual_evidence(row)
                and has_visual_anchor(row)
            ]
            alternative_rows.sort(
                key=lambda row: (
                    int(row["guide_source_match"]["tier"]),
                    int(displayed_source_usage.get(str(row.get("source_content_id") or ""), 0)),
                    -float(row["score_components"]["retrieval_score"]),
                    row["candidate_id"],
                )
            )
            if is_state_transition(str(beat.get("text") or "")):
                alternative_rows = _prioritize_state_endpoints(
                    alternative_rows,
                    set(visual_attributes(str(beat.get("text") or ""))),
                )
            ranked_by_pool["alternative"] = _take_diverse(
                alternative_rows, limit=DYNAMIC_POOL_RECALL_LIMIT, excluded_ids=used,
            )
            for pool, rows in ranked_by_pool.items():
                for row in rows:
                    row["evidence_tier"] = row["pool"]
                    row["pool"] = pool
                    row["match_strength"] = {
                        "direct": "strong",
                        "supplement": "contextual",
                        "alternative": "fallback",
                    }[pool]
        else:
            for row in all_ranked:
                ranked_by_pool[row["pool"]].append(row)
            for pool in POOLS:
                ranked_by_pool[pool] = ranked_by_pool[pool][:3]
        if comparison_mode and dynamic_pools:
            # Ensure both retrieval routes reach the same factual/subject gate;
            # guide-date priority must not starve the ordinary editorial route.
            present = {row["candidate_id"] for rows in ranked_by_pool.values() for row in rows}
            system_recall = sorted(all_ranked, key=lambda row: (
                -float(row["score_components"]["local_score"]),
                -(float(row["score_components"]["retrieval_score"]) - float(row["score_components"].get("editorial_guide_score") or 0)), row["candidate_id"]))
            scoped_recall = [row for row in all_ranked if int(row["guide_source_match"]["tier"]) < 4]
            for route in (scoped_recall, system_recall):
                extra = _take_diverse([row for row in route if has_visual_anchor(row) and has_visual_evidence(row)],
                                      limit=30, excluded_ids=set())
                for row in extra:
                    if row["candidate_id"] not in present:
                        present.add(row["candidate_id"])
                        row["pool"] = "supplement"
                        ranked_by_pool["supplement"].append(row)
        for pool, rows in ranked_by_pool.items():
            materialized: list[dict[str, Any]] = []
            for ranked in rows:
                payload = copy.deepcopy(source_candidates[int(ranked["_candidate_index"])])
                payload.update(
                    {
                        key: copy.deepcopy(value)
                        for key, value in ranked.items()
                        if key != "_candidate_index"
                    }
                )
                materialized.append(payload)
            ranked_by_pool[pool] = materialized
        reserve_by_pool: dict[str, list[dict[str, Any]]] = {pool: [] for pool in POOLS}
        if dynamic_pools:
            shortlisted = _editorial_shortlist(
                beat,
                ranked_by_pool,
                selected_track,
                neighbor_index,
                limit=DYNAMIC_SHORTLIST_LIMIT,
                prior_source_usage=displayed_source_usage,
            )
            ranked_by_pool = {
                pool: [row for row in rows if int(row.get("shortlist_rank") or 0) <= 3]
                for pool, rows in shortlisted.items()
            }
            reserve_by_pool = {
                pool: [row for row in rows if int(row.get("shortlist_rank") or 0) > 3]
                for pool, rows in shortlisted.items()
            }
            displayed = [
                row
                for rows in ranked_by_pool.values()
                for row in rows
            ]
            displayed_source_usage.update(
                str(row.get("source_content_id") or row.get("candidate_id") or "")
                for row in displayed
            )
            beat["shortlist_source_count"] = len({
                str(row.get("source_content_id") or "")
                for row in displayed
            })
        board_beats.append({
            **beat,
            "candidate_pools": ranked_by_pool,
            "candidate_reserve_pools": reserve_by_pool,
        })

    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": project["project_id"],
        "project_title": project["project_title"],
        "track": selected_track,
        "source_mode": str(project.get("source_mode") or "fixture_read_only"),
        "editorial_guide_summary": guide_summary,
        "shot_boundary_mode": str(project.get("shot_boundary_mode") or "disabled"),
        "shot_boundary_source_count": int(project.get("shot_boundary_source_count") or 0),
        "shot_boundary_enriched_candidates": int(project.get("shot_boundary_enriched_candidates") or 0),
        "shot_boundary_unmatched_candidates": int(project.get("shot_boundary_unmatched_candidates") or 0),
        "shot_boundary_adjusted_candidates": int(project.get("shot_boundary_adjusted_candidates") or 0),
        "shot_boundary_out_of_range_candidates": int(project.get("shot_boundary_out_of_range_candidates") or 0),
        "multiframe_mode": str(project.get("multiframe_mode") or "disabled"),
        "multiframe_enriched_candidates": int(project.get("multiframe_enriched_candidates") or 0),
        "multiframe_unmatched_candidates": int(project.get("multiframe_unmatched_candidates") or 0),
        "multiframe_no_clean_candidates": int(project.get("multiframe_no_clean_candidates") or 0),
        "candidate_count": len(source_candidates),
        "ignored_chapter_cards": ignored_chapter_cards,
        "shortlist_limit_per_beat": 3 if dynamic_pools else 9,
        "reserve_limit_per_beat": DYNAMIC_SHORTLIST_LIMIT if dynamic_pools else 9,
        "beats": board_beats,
        "warnings": [
            (
                "候选来自当前中心数据库的只读分析结果。"
                if project.get("source_mode") == "real_database_read_only"
                else "候选来自只读样例，不代表已经连接 1.2.3 正式数据库。"
            ),
            "分数仅用于候选排序，不是镜头质量、电影感或爆款概率。",
            "抽象句可能没有直述候选；空栏比伪造高相关结果更可靠。",
            (
                "已接入只读 P1B 镜头边界 manifest；候选窗口受物理镜头边界约束，但 clean 入出点仍待验证。"
                if project.get("shot_boundary_mode") == "manifest_read_only"
                else "尚未接入镜头边界 manifest；视频仍使用抽样点临时窗口。"
            ),
            (
                "已接入只读 P1C/P2B 多帧 sidecar；clean 入出点只筛技术异常，运镜仍是待复核候选。"
                if project.get("multiframe_mode") == "manifest_read_only"
                else "尚未接入多帧 sidecar；摄影机运动、技术 clean 入出点和音轨响度仍未知。"
            ),
        ],
    }


def decision_summary(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("decision") or "unreviewed") for row in decisions)
    unexpected = set(counts) - DECISIONS
    if unexpected:
        raise ValueError(f"unexpected decision states: {sorted(unexpected)}")
    return {state: counts.get(state, 0) for state in sorted(DECISIONS)}
