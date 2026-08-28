"""Project-supplied source references. Never treat a date/ID as visual evidence."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
import re
from typing import Any


def _dates(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    patterns = (
        (r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)", True),
        (r"(?<!\d)((?:19|20)\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?", True),
        (r"(?<!\d)(\d{1,2})月(\d{1,2})日?", False),
    )
    for pattern, has_year in patterns:
        for match in re.finditer(pattern, text):
            parts = [int(value) for value in match.groups()]
            year, month, day = parts if has_year else [2000, *parts]
            try:
                date(year, month, day)
            except ValueError:
                continue
            found.append((match.start(), f"{year:04d}-{month:02d}-{day:02d}" if has_year else f"{month:02d}-{day:02d}"))
    return list(dict.fromkeys(value for _, value in sorted(found)))


def _identifiers(text: str) -> set[str]:
    tokens = re.findall(r"(?<![a-zA-Z0-9])[a-zA-Z][a-zA-Z0-9_-]*\d[a-zA-Z0-9_.-]*(?![a-zA-Z0-9])", text)
    return {Path(token).stem.casefold() for token in tokens}


def source_references(text: str) -> dict[str, list[str]]:
    # Only the first dated reference on each shot line is the source date.
    # Later dates often describe comparisons/memories rather than source scope.
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return {
        "identifiers": sorted(set().union(*(_identifiers(line.split("｜", 1)[0]) for line in lines))) if lines else [],
        "dates": list(dict.fromkeys(values[0] for line in lines if (values := _dates(line)))),
    }


def prepare_source_reference_index(candidates: list[dict[str, Any]]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for position, candidate in enumerate(candidates):
        path = str(candidate.get("source_file") or "")
        for identifier in _identifiers(path) | {str(candidate.get("source_content_id") or "").casefold()}:
            if identifier:
                index["id:" + identifier].add(position)
        for value in _dates(path):
            index["date:" + value].add(position)
            index["date:" + value[-5:]].add(position)
    return dict(index)


def resolve_guide_sources(
    guide: dict[str, Any] | None,
    index: dict[str, set[int]],
    candidates: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    if not guide:
        return {}, {"status": "NO_MATCHED_GUIDE", "message": "本句未可靠匹配到逐句表；按文稿和上下文检索，不套用其他句的指导。"}
    matches: dict[int, dict[str, Any]] = {}
    unresolved: list[str] = []
    summary: dict[str, Any] = {"status": "REFERENCES_RESOLVED"}
    for group, rank in (("primary", 0), ("alternative", 2)):
        refs = source_references(str(guide.get(group + "_shot") or ""))
        ids = set().union(*(index.get("id:" + value, set()) for value in refs["identifiers"])) if refs["identifiers"] else set()
        dates = set().union(*(index.get("date:" + value, set()) for value in refs["dates"])) if refs["dates"] else set()
        unresolved += [value.upper() for value in refs["identifiers"] if not index.get("id:" + value)]
        resolved = ids or dates
        kind = "编号" if ids else "日期"
        tier = rank if ids else rank + 1
        label = ("主用" if group == "primary" else "替补") + kind + "范围"
        for position in sorted(resolved):
            matches.setdefault(position, {"tier": tier, "scope": group, "label": label})
        summary[group + "_references"] = refs
        summary[group + "_source_count"] = len({str(candidates[i].get("source_content_id") or candidates[i].get("source_file")) for i in resolved})
    summary["unresolved_identifiers"] = list(dict.fromkeys(unresolved))
    has_refs = any(summary[group + "_references"][key] for group in ("primary", "alternative") for key in ("identifiers", "dates"))
    if not has_refs:
        summary["status"] = "DESCRIPTION_ONLY"
        summary["message"] = "表中没有可定位的编号/日期；按画面描述检索，不猜测拍摄日期。"
    else:
        summary["message"] = (
            f"先查主用范围 {summary['primary_source_count']} 个原文件，再查替补范围 {summary['alternative_source_count']} 个原文件；"
            "按表组仅使用这些范围内的候选，系统补充组另列；日期按原文件/目录名匹配，不等于已核验真实拍摄日期，也不替代内容判断。"
        )
        if unresolved:
            summary["message"] += " 编号未在原文件名中解析：" + "、".join(summary["unresolved_identifiers"]) + "；仅凭日期不能确认就是表中那一条。"
    return matches, summary
