from __future__ import annotations

from difflib import SequenceMatcher
import copy
from pathlib import Path
import posixpath
import re
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


CONTRACT_VERSION = "cinematic_project_editorial_guide_v1"
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - 64
    return result - 1


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _worksheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        str(row.attrib.get("Id") or ""): str(row.attrib.get("Target") or "")
        for row in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        name = str(sheet.attrib.get("name") or "")
        relation = str(sheet.attrib.get(f"{{{_DOC_REL_NS}}}id") or "")
        target = targets.get(relation, "")
        if not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        result.append((name, path))
    return result


def _cell_value(cell: ET.Element, shared: list[str]) -> object:
    cell_type = str(cell.attrib.get("t") or "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t"))
    node = cell.find(f"{{{_MAIN_NS}}}v")
    raw = "" if node is None else str(node.text or "")
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    if not raw:
        return ""
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def _sheet_rows(archive: ZipFile, path: str, shared: list[str]) -> list[list[object]]:
    root = ET.fromstring(archive.read(path))
    result: list[list[object]] = []
    for row in root.findall(f".//{{{_MAIN_NS}}}row"):
        values: dict[int, object] = {}
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            index = _column_number(str(cell.attrib.get("r") or "A1"))
            values[index] = _cell_value(cell, shared)
        row_number = int(row.attrib.get("r") or len(result) + 1)
        while len(result) < row_number - 1:
            result.append([])
        result.append([values.get(index, "") for index in range(max(values) + 1)] if values else [])
    return result


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _header_index(headers: list[str], aliases: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        if any(alias in header for alias in aliases):
            return index
    return None


def _value(row: list[object], index: int | None) -> str:
    return _clean(row[index]) if index is not None and index < len(row) else ""


def _split_primary(value: str) -> tuple[str, str]:
    marker = re.search(r"镜头方向\s*[:：]", value)
    if not marker:
        return value, ""
    return value[:marker.start()].strip(), value[marker.end():].strip()


def load_editorial_guide(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.suffix.casefold() != ".xlsx":
        raise ValueError("剪辑指导当前只支持 .xlsx 文件")
    try:
        with ZipFile(resolved) as archive:
            shared = _shared_strings(archive)
            sheets = [
                (name, _sheet_rows(archive, sheet_path, shared))
                for name, sheet_path in _worksheet_paths(archive)
            ]
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"剪辑指导 Excel 无法读取：{exc}") from exc

    selected_name = ""
    selected_rows: list[list[object]] = []
    header_row = -1
    for name, rows in sheets:
        for index, row in enumerate(rows[:30]):
            headers = [_clean(value) for value in row]
            if _header_index(headers, ("旁白", "文稿", "句子", "台词")) is not None and _header_index(
                headers, ("主用镜头", "主镜头", "画面方向")
            ) is not None:
                selected_name, selected_rows, header_row = name, rows, index
                break
        if header_row >= 0:
            break
    if header_row < 0:
        raise ValueError("剪辑指导 Excel 中没有找到“旁白/文稿”和“主用镜头”列")

    headers = [_clean(value) for value in selected_rows[header_row]]
    columns = {
        "order": _header_index(headers, ("序号", "编号")),
        "section": _header_index(headers, ("段落", "章节", "部分")),
        "narration": _header_index(headers, ("旁白", "文稿", "句子", "台词")),
        "primary": _header_index(headers, ("主用镜头", "主镜头", "画面方向")),
        "alternative": _header_index(headers, ("补切", "替换", "备用镜头")),
        "editing": _header_index(headers, ("剪辑手法", "切法", "剪辑建议")),
        "notes": _header_index(headers, ("备注", "提醒", "限制")),
    }
    rows: list[dict[str, Any]] = []
    for excel_row, source in enumerate(selected_rows[header_row + 1:], header_row + 2):
        narration = _value(source, columns["narration"])
        if not narration:
            continue
        primary, direction = _split_primary(_value(source, columns["primary"]))
        notes = _value(source, columns["notes"])
        combined = " ".join((direction, notes))
        status = "RESHOOT_PRIORITY" if any(
            marker in combined for marker in ("补拍优先", "必须补", "素材缺口", "正式前补")
        ) else ("SOURCE_REVIEW" if any(marker in combined for marker in ("待核", "未确认", "只做过渡")) else "READY")
        rows.append({
            "guide_row_id": f"row-{len(rows) + 1:03d}",
            "excel_row": excel_row,
            "order": _value(source, columns["order"]),
            "section": _value(source, columns["section"]),
            "narration": narration,
            "primary_shot": primary,
            "visual_direction": direction,
            "alternative_shot": _value(source, columns["alternative"]),
            "editing_method": _value(source, columns["editing"]),
            "notes": notes,
            "guidance_status": status,
        })
    if not rows:
        raise ValueError("剪辑指导 Excel 没有可用的逐句记录")
    return {
        "contract_version": CONTRACT_VERSION,
        "source_file": str(resolved),
        "sheet_name": selected_name,
        "row_count": len(rows),
        "rows": rows,
    }


def _normalized(text: str) -> str:
    return re.sub(r"[^\u3400-\u9fffa-z0-9]", "", str(text or "").casefold())


def _match_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if min(len(left), len(right)) >= 7 and (left in right or right in left):
        return 0.96 * min(len(left), len(right)) / max(len(left), len(right)) + 0.04
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _alignment_reward(similarity: float) -> float:
    if similarity >= 0.9:
        return 3.2 + similarity
    if similarity >= 0.7:
        return 2.0 + similarity
    if similarity >= 0.5:
        return 0.9 + similarity
    if similarity >= 0.3:
        return similarity - 0.15
    return -0.55


def _sequence_alignment(targets: list[str], sources: list[str]) -> list[tuple[list[int], float] | None]:
    """Align two revisions as ordered documents, not as independent searches.

    One narration revision may split or join a sentence, so the dynamic program
    supports 1:1, 1:2 and 2:1 links.  Low-similarity links are retained only as
    alignment diagnostics; `apply_editorial_guide` decides whether surrounding
    anchors make them safe enough to expose to the user.
    """
    n, m = len(targets), len(sources)
    negative = float("-inf")
    scores = [[negative] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, str, float] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    scores[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            current = scores[i][j]
            if current == negative:
                continue

            def offer(ni: int, nj: int, action: str, value: float, similarity: float = 0.0) -> None:
                candidate = current + value
                if candidate > scores[ni][nj]:
                    scores[ni][nj] = candidate
                    back[ni][nj] = (i, j, action, similarity)

            if i < n:
                offer(i + 1, j, "skip_target", -0.46)
            if j < m:
                offer(i, j + 1, "skip_source", -0.42)
            if i < n and j < m:
                similarity = _match_score(targets[i], sources[j])
                position_penalty = abs((i + 0.5) / max(1, n) - (j + 0.5) / max(1, m)) * 0.35
                offer(i + 1, j + 1, "one_to_one", _alignment_reward(similarity) - position_penalty, similarity)
            if i < n and j + 1 < m:
                similarity = _match_score(targets[i], sources[j] + sources[j + 1])
                offer(i + 1, j + 2, "one_to_two", _alignment_reward(similarity) - 0.18, similarity)
            if i + 1 < n and j < m:
                similarity = _match_score(targets[i] + targets[i + 1], sources[j])
                offer(i + 2, j + 1, "two_to_one", _alignment_reward(similarity) - 0.18, similarity)
            if i < n and j + 2 < m:
                similarity = _match_score(targets[i], sources[j] + sources[j + 1] + sources[j + 2])
                offer(i + 1, j + 3, "one_to_three", _alignment_reward(similarity) - 0.26, similarity)
            if i + 2 < n and j < m:
                similarity = _match_score(targets[i] + targets[i + 1] + targets[i + 2], sources[j])
                offer(i + 3, j + 1, "three_to_one", _alignment_reward(similarity) - 0.26, similarity)

    result: list[tuple[list[int], float] | None] = [None] * n
    i, j = n, m
    while i or j:
        step = back[i][j]
        if step is None:
            break
        previous_i, previous_j, action, similarity = step
        if action == "one_to_one":
            result[previous_i] = ([previous_j], similarity)
        elif action == "one_to_two":
            result[previous_i] = ([previous_j, previous_j + 1], similarity)
        elif action == "two_to_one":
            result[previous_i] = ([previous_j], similarity)
            result[previous_i + 1] = ([previous_j], similarity)
        elif action == "one_to_three":
            result[previous_i] = ([previous_j, previous_j + 1, previous_j + 2], similarity)
        elif action == "three_to_one":
            result[previous_i] = ([previous_j], similarity)
            result[previous_i + 1] = ([previous_j], similarity)
            result[previous_i + 2] = ([previous_j], similarity)
        i, j = previous_i, previous_j
    return result


def load_editorial_guides(paths: Path | list[Path]) -> dict[str, Any]:
    paths = [paths] if isinstance(paths, Path) else paths
    guides = [load_editorial_guide(path) for path in dict.fromkeys(paths)]
    return guides[0] if len(guides) == 1 else {"documents": guides}


def apply_editorial_guide(beats: list[dict[str, Any]], guide: dict[str, Any]) -> dict[str, Any]:
    if "documents" in guide:
        # Align each revision independently. Concatenating two revisions would
        # corrupt ordered matching and could turn guide notes into narration.
        aligned = []
        summaries = []
        for document in guide["documents"]:
            draft = copy.deepcopy(beats)
            summaries.append(apply_editorial_guide(draft, document))
            aligned.append(draft)
        for index, beat in enumerate(beats):
            matches = [draft[index]["editorial_guide"] for draft in aligned if draft[index].get("editorial_guide")]
            matches.sort(key=lambda row: -float(row["match_confidence"]))
            if not matches:
                beat["editorial_guide"] = None
                continue
            merged = copy.deepcopy(matches[0])
            merged["source_guides"] = matches
            # The strongest match defines primary guidance; others remain
            # explicit alternatives instead of silently overwriting it.
            for extra in matches[1:]:
                merged["alternative_shot"] += "\n" + extra["primary_shot"] + "\n" + extra["alternative_shot"]
                merged["notes"] += "\n其他指导（" + Path(extra["source_file"]).name + "）：" + extra["visual_direction"] + "；" + extra["editing_method"]
            merged["retrieval_text"] += " " + " ".join(row["retrieval_text"] for row in matches[1:])
            if len(matches) > 1:
                merged["notes"] += "\n多份指导按匹配强度排序；冲突建议保留为替补，需人工判断，不代表同一事实。"
            beat["editorial_guide"] = merged
        matched = sum(bool(beat.get("editorial_guide")) for beat in beats)
        return {"source_file": " / ".join(row["source_file"] for row in summaries),
                "sheet_name": "多份逐句指导", "guide_row_count": sum(row["guide_row_count"] for row in summaries),
                "matched_beat_count": matched, "unmatched_beat_count": len(beats) - matched, "documents": summaries}
    rows = [dict(row) for row in guide.get("rows") or []]
    normalized_rows = [_normalized(str(row.get("narration") or "")) for row in rows]
    normalized_beats = [_normalized(str(beat.get("text") or "")) for beat in beats]
    alignment = _sequence_alignment(normalized_beats, normalized_rows)
    anchors = [
        index for index, match in enumerate(alignment)
        if match is not None and match[1] >= 0.58
    ]
    matched = 0
    for beat_index, beat in enumerate(beats):
        match = alignment[beat_index]
        if match is None:
            beat["editorial_guide"] = None
            continue
        indices, lexical_score = match
        previous_anchor = max((index for index in anchors if index < beat_index), default=-1)
        next_anchor = min((index for index in anchors if index > beat_index), default=len(beats))
        bounded_by_context = (
            previous_anchor >= 0
            and next_anchor < len(beats)
            and beat_index - previous_anchor <= 8
            and next_anchor - beat_index <= 8
        )
        position_delta = abs(
            (beat_index + 0.5) / max(1, len(beats))
            - (sum(indices) / len(indices) + 0.5) / max(1, len(rows))
        )
        contextual_match = (
            lexical_score >= 0.58
            or (lexical_score >= 0.16 and bounded_by_context)
            or (lexical_score >= 0.34 and position_delta <= 0.045)
        )
        if not contextual_match:
            beat["editorial_guide"] = None
            continue
        source_rows = [rows[index] for index in indices]
        statuses = {str(row.get("guidance_status") or "READY") for row in source_rows}
        status = "RESHOOT_PRIORITY" if "RESHOOT_PRIORITY" in statuses else (
            "SOURCE_REVIEW" if "SOURCE_REVIEW" in statuses else "READY"
        )
        guidance = {
            "contract_version": CONTRACT_VERSION,
            "source_file": str(guide.get("source_file") or ""),
            "match_confidence": round(
                lexical_score if lexical_score >= 0.58 else min(0.78, 0.6 + lexical_score * 0.35),
                4,
            ),
            "match_type": "EXACT_OR_CONTAINED" if lexical_score >= 0.94 else (
                "FUZZY_SEQUENCE" if lexical_score >= 0.58 else "DOCUMENT_CONTEXT_ALIGNMENT"
            ),
            "guide_row_ids": [str(row.get("guide_row_id") or "") for row in source_rows],
            "excel_rows": [int(row.get("excel_row") or 0) for row in source_rows],
            "section": " / ".join(dict.fromkeys(str(row.get("section") or "") for row in source_rows if row.get("section"))),
            "guide_narration": " ".join(str(row.get("narration") or "") for row in source_rows),
            "primary_shot": "\n".join(str(row.get("primary_shot") or "") for row in source_rows if row.get("primary_shot")),
            "visual_direction": "\n".join(str(row.get("visual_direction") or "") for row in source_rows if row.get("visual_direction")),
            "alternative_shot": "\n".join(str(row.get("alternative_shot") or "") for row in source_rows if row.get("alternative_shot")),
            "editing_method": "\n".join(str(row.get("editing_method") or "") for row in source_rows if row.get("editing_method")),
            "notes": "\n".join(str(row.get("notes") or "") for row in source_rows if row.get("notes")),
            "guidance_status": status,
        }
        guidance["retrieval_text"] = " ".join(filter(None, (
            guidance["visual_direction"],
            "" if status == "RESHOOT_PRIORITY" else guidance["primary_shot"],
            "" if status == "RESHOOT_PRIORITY" else guidance["alternative_shot"],
        )))
        beat["editorial_guide"] = guidance
        matched += 1
    return {
        "source_file": str(guide.get("source_file") or ""),
        "sheet_name": str(guide.get("sheet_name") or ""),
        "guide_row_count": len(rows),
        "matched_beat_count": matched,
        "unmatched_beat_count": len(beats) - matched,
    }
