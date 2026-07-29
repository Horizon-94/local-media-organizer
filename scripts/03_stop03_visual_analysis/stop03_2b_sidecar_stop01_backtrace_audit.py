#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-2B local sidecar + Stop01 backtrace audit.

Purpose:
- Read only.
- Search original source folder for XMP/XML/sidecar-like files.
- Loosely match sidecars to media files by same name / Sony-style M01 suffix / embedded media stem.
- Search Stop01/Stop02/Stop03 manifests for sidecar rows and marker-like fields.
- Report whether image user markers existed in source sidecars or were dropped before Stop03-2.

This script does NOT modify, move, delete, rename, or write to original media folders.
It writes reports only under --out.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Any


MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".hif", ".tif", ".tiff",
    ".arw", ".dng", ".raf", ".cr2", ".cr3", ".nef", ".rw2", ".orf",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".braw", ".crm", ".wav", ".mp3", ".m4a"
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".hif", ".tif", ".tiff",
    ".arw", ".dng", ".raf", ".cr2", ".cr3", ".nef", ".rw2", ".orf"
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".braw", ".crm"}

SIDECAR_EXTS = {".xmp", ".xml"}

TEXT_LIKE_EXTS = {".xmp", ".xml", ".json", ".txt", ".csv", ".plist"}

MARKER_FIELD_RE = re.compile(
    r"(xmp|xml|sidecar|rating|label|marked|pick|reject|stars?|color|colour|"
    r"urgency|favorite|favourite|tag|bridge|lightroom|camera\s*raw|photoshop|"
    r"crs:|lr:|microsoftphoto)",
    re.I,
)

# These indicate a user selection/label if non-empty / non-zero.
MARKER_PATTERNS = {
    "xmp_rating_attr": re.compile(r'xmp:Rating\s*=\s*"([^"]+)"', re.I),
    "xmp_rating_tag": re.compile(r"<xmp:Rating>(.*?)</xmp:Rating>", re.I | re.S),
    "xmp_label_attr": re.compile(r'xmp:Label\s*=\s*"([^"]+)"', re.I),
    "xmp_label_tag": re.compile(r"<xmp:Label>(.*?)</xmp:Label>", re.I | re.S),
    "xmp_marked_attr": re.compile(r'xmp:Marked\s*=\s*"([^"]+)"', re.I),
    "xmp_marked_tag": re.compile(r"<xmp:Marked>(.*?)</xmp:Marked>", re.I | re.S),
    "photoshop_urgency_attr": re.compile(r'photoshop:Urgency\s*=\s*"([^"]+)"', re.I),
    "photoshop_urgency_tag": re.compile(r"<photoshop:Urgency>(.*?)</photoshop:Urgency>", re.I | re.S),
    "microsoft_rating_attr": re.compile(r'MicrosoftPhoto:Rating\s*=\s*"([^"]+)"', re.I),
}

SOFTWARE_PATTERNS = {
    "adobe": re.compile(r"adobe", re.I),
    "photoshop": re.compile(r"photoshop", re.I),
    "camera_raw": re.compile(r"camera\s*raw|crs:", re.I),
    "bridge": re.compile(r"bridge", re.I),
    "lightroom": re.compile(r"lightroom|lr:", re.I),
    "xmpmm": re.compile(r"xmpMM:", re.I),
    "sony_nonreal_time_meta": re.compile(r"nonreal|cameraunitmetadata|sony|clipcontent|materialid|mediaprofile", re.I),
}

# Common non-user-pick values.
EMPTY_MARKER_VALUES = {"", "0", "-1", "none", "false", "no", "null", "{}", "[]", "unmarked", "unset"}


def read_text_prefix(path: Path, max_bytes: int = 1024 * 256) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(path)


def normalize_stem(stem: str) -> str:
    return stem.lower().strip()


def candidate_stems_for_sidecar(sidecar: Path) -> List[str]:
    """
    Generates loose match keys.

    Examples:
    - IMG_0001.xmp -> IMG_0001
    - IMG_0001.ARW.xmp -> IMG_0001 and IMG_0001.ARW
    - 20250527_A7M4-4896M01.XML -> 20250527_A7M4-4896M01 and 20250527_A7M4-4896
    - C0001M01.XML -> C0001M01 and C0001
    """
    stem = sidecar.stem
    keys = {stem}

    # If sidecar stem contains media suffix, e.g. foo.ARW.xmp
    for ext in MEDIA_EXTS:
        if stem.lower().endswith(ext):
            keys.add(stem[: -len(ext)])

    # Sony sidecar suffix such as M01, M02.
    keys.add(re.sub(r"M\d{2}$", "", stem, flags=re.I))

    # Some camera metadata sidecars add C01/M01-like suffixes; keep conservative variants.
    keys.add(re.sub(r"([_-]?)M\d{2}$", "", stem, flags=re.I))
    keys.add(re.sub(r"([_-]?)C\d{2}$", "", stem, flags=re.I))

    # Strip trailing XML-ish suffix markers.
    keys.add(re.sub(r"[_-]?(meta|metadata|sidecar)$", "", stem, flags=re.I))

    return sorted({k for k in keys if k})


def detect_marker_and_software(text: str) -> Tuple[bool, List[str], List[str], str]:
    marker_hits: List[str] = []
    for name, pat in MARKER_PATTERNS.items():
        for m in pat.finditer(text):
            val = (m.group(1) or "").strip()
            marker_hits.append(f"{name}={val}")

    has_user_selection = False
    for hit in marker_hits:
        val = hit.split("=", 1)[-1].strip().lower()
        if val not in EMPTY_MARKER_VALUES:
            has_user_selection = True
            break

    software_hits = []
    for name, pat in SOFTWARE_PATTERNS.items():
        if pat.search(text):
            software_hits.append(name)

    if has_user_selection:
        guess = "user_selection_marker"
    elif software_hits:
        guess = "software_or_camera_metadata_only"
    elif text.lstrip().startswith("<?xml") or "<" in text[:500]:
        guess = "xml_no_selection_marker"
    else:
        guess = "unknown_sidecar_text"

    return has_user_selection, marker_hits, sorted(set(software_hits)), guess


def collect_source_inventory(source_root: Path) -> Tuple[List[Path], List[Path], Dict[Tuple[str, str], List[Path]]]:
    media_files: List[Path] = []
    sidecars: List[Path] = []
    media_by_parent_stem: Dict[Tuple[str, str], List[Path]] = defaultdict(list)

    for p in source_root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in MEDIA_EXTS:
            media_files.append(p)
            media_by_parent_stem[(str(p.parent), normalize_stem(p.stem))].append(p)
        elif ext in SIDECAR_EXTS:
            sidecars.append(p)

    return media_files, sidecars, media_by_parent_stem


def loose_match_sidecar(sidecar: Path, media_by_parent_stem: Dict[Tuple[str, str], List[Path]]) -> Tuple[List[Path], List[str]]:
    parent = str(sidecar.parent)
    matched: List[Path] = []
    matched_keys: List[str] = []

    for stem in candidate_stems_for_sidecar(sidecar):
        key = (parent, normalize_stem(stem))
        hits = media_by_parent_stem.get(key, [])
        if hits:
            matched.extend(hits)
            matched_keys.append(stem)

    # Extra loose: if one media stem is a prefix of sidecar stem or vice versa in same directory.
    sc_stem_norm = normalize_stem(sidecar.stem)
    for (pdir, media_stem), hits in media_by_parent_stem.items():
        if pdir != parent:
            continue
        if media_stem and (
            sc_stem_norm.startswith(media_stem)
            or media_stem.startswith(sc_stem_norm)
        ):
            # Avoid very short false positives.
            if min(len(media_stem), len(sc_stem_norm)) >= 6:
                matched.extend(hits)
                matched_keys.append(media_stem)

    # Deduplicate preserving string order.
    seen = set()
    uniq = []
    for p in matched:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)

    return uniq, sorted(set(matched_keys))


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def audit_source_sidecars(source_root: Path, out_dir: Path) -> Dict[str, Any]:
    media_files, sidecars, media_by_parent_stem = collect_source_inventory(source_root)

    rows: List[Dict[str, Any]] = []
    media_sidecar_match_rows: List[Dict[str, Any]] = []

    for sc in sorted(sidecars):
        text = read_text_prefix(sc)
        has_marker, marker_hits, software_hits, content_guess = detect_marker_and_software(text)
        matches, match_keys = loose_match_sidecar(sc, media_by_parent_stem)

        row = {
            "sidecar_path": str(sc),
            "sidecar_relative_path": safe_rel(sc, source_root),
            "sidecar_ext": sc.suffix.lower(),
            "sidecar_size_bytes": sc.stat().st_size if sc.exists() else "",
            "candidate_match_keys": "|".join(match_keys),
            "matched_media_count": len(matches),
            "matched_media_paths": " | ".join(str(m) for m in matches[:20]),
            "matched_media_relative_paths": " | ".join(safe_rel(m, source_root) for m in matches[:20]),
            "matched_media_exts": "|".join(sorted({m.suffix.lower() for m in matches})),
            "has_selection_marker": int(has_marker),
            "marker_hits": "|".join(marker_hits),
            "software_evidence": "|".join(software_hits),
            "content_guess": content_guess,
            "text_prefix": text[:500].replace("\n", "\\n"),
        }
        rows.append(row)

        for m in matches:
            media_sidecar_match_rows.append({
                "media_path": str(m),
                "media_relative_path": safe_rel(m, source_root),
                "media_ext": m.suffix.lower(),
                "sidecar_path": str(sc),
                "sidecar_relative_path": safe_rel(sc, source_root),
                "sidecar_ext": sc.suffix.lower(),
                "has_selection_marker": int(has_marker),
                "marker_hits": "|".join(marker_hits),
                "software_evidence": "|".join(software_hits),
                "content_guess": content_guess,
            })

    sidecar_csv = out_dir / "source_sidecar_inventory.csv"
    match_csv = out_dir / "source_media_sidecar_matches.csv"
    orphan_csv = out_dir / "source_orphan_sidecars.csv"

    fields = [
        "sidecar_path", "sidecar_relative_path", "sidecar_ext", "sidecar_size_bytes",
        "candidate_match_keys", "matched_media_count", "matched_media_paths",
        "matched_media_relative_paths", "matched_media_exts",
        "has_selection_marker", "marker_hits", "software_evidence", "content_guess", "text_prefix",
    ]
    write_csv(sidecar_csv, rows, fields)

    write_csv(match_csv, media_sidecar_match_rows, [
        "media_path", "media_relative_path", "media_ext",
        "sidecar_path", "sidecar_relative_path", "sidecar_ext",
        "has_selection_marker", "marker_hits", "software_evidence", "content_guess",
    ])

    write_csv(orphan_csv, [r for r in rows if int(r["matched_media_count"]) == 0], fields)

    content_counter = Counter(r["content_guess"] for r in rows)
    ext_counter = Counter(r["sidecar_ext"] for r in rows)
    media_ext_counter = Counter()
    for p in media_files:
        media_ext_counter[p.suffix.lower()] += 1

    return {
        "source_media_file_count": len(media_files),
        "source_media_ext_counts": dict(media_ext_counter),
        "source_sidecar_count": len(sidecars),
        "source_sidecar_ext_counts": dict(ext_counter),
        "matched_sidecar_count": sum(1 for r in rows if int(r["matched_media_count"]) > 0),
        "orphan_sidecar_count": sum(1 for r in rows if int(r["matched_media_count"]) == 0),
        "sidecar_with_selection_marker_count": sum(1 for r in rows if int(r["has_selection_marker"]) == 1),
        "sidecar_content_guess_counts": dict(content_counter),
        "source_sidecar_inventory_csv": str(sidecar_csv),
        "source_media_sidecar_matches_csv": str(match_csv),
        "source_orphan_sidecars_csv": str(orphan_csv),
    }


def iter_manifest_rows(path: Path) -> Iterable[Tuple[str, Dict[str, Any]]]:
    ext = path.suffix.lower()
    try:
        if ext == ".jsonl":
            with path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        yield str(line_no), json.loads(line)
                    except Exception:
                        continue
        elif ext == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, list):
                for i, row in enumerate(data):
                    if isinstance(row, dict):
                        yield str(i), row
            elif isinstance(data, dict):
                # If dict contains list values, walk shallowly.
                yielded = False
                for k, v in data.items():
                    if isinstance(v, list):
                        for i, row in enumerate(v):
                            if isinstance(row, dict):
                                yielded = True
                                yield f"{k}[{i}]", row
                if not yielded:
                    yield "0", data
        elif ext == ".csv":
            with path.open("r", encoding="utf-8", newline="") as f:
                for i, row in enumerate(csv.DictReader(f), 1):
                    yield str(i), dict(row)
    except Exception:
        return


def looks_like_stop01_file(path: Path) -> bool:
    s = str(path).lower()
    name = path.name.lower()
    if not path.is_file():
        return False
    if path.suffix.lower() not in {".csv", ".json", ".jsonl"}:
        return False
    return any(key in s for key in [
        "stop01", "01_stop01", "source", "scan", "manifest", "inventory", "file"
    ]) or "stop01" in name


def row_mentions_sidecar(row: Dict[str, Any]) -> bool:
    text = json.dumps(row, ensure_ascii=False).lower()
    return any(ext in text for ext in [".xmp", ".xml"]) or bool(MARKER_FIELD_RE.search(text))


def audit_stop_manifests(run_root: Path, out_dir: Path) -> Dict[str, Any]:
    manifest_files = sorted([p for p in run_root.rglob("*") if looks_like_stop01_file(p)])

    stop01_sidecar_rows: List[Dict[str, Any]] = []
    marker_field_counts: Counter = Counter()
    marker_field_nonempty_counts: Counter = Counter()
    file_count = 0
    row_count = 0

    for mf in manifest_files:
        file_count += 1
        for row_id, row in iter_manifest_rows(mf):
            row_count += 1

            for k, v in row.items():
                if MARKER_FIELD_RE.search(str(k)):
                    marker_field_counts[str(k)] += 1
                    if v not in ("", None, [], {}, "0", "False", "false"):
                        marker_field_nonempty_counts[str(k)] += 1

            if row_mentions_sidecar(row):
                stop01_sidecar_rows.append({
                    "manifest_file": str(mf),
                    "manifest_relative_path": safe_rel(mf, run_root),
                    "row_id": row_id,
                    "row_json_prefix": json.dumps(row, ensure_ascii=False)[:4000],
                })

    manifest_list_csv = out_dir / "run_root_candidate_manifest_files.csv"
    write_csv(manifest_list_csv, [
        {
            "manifest_file": str(p),
            "manifest_relative_path": safe_rel(p, run_root),
            "size_bytes": p.stat().st_size if p.exists() else "",
        }
        for p in manifest_files
    ], ["manifest_file", "manifest_relative_path", "size_bytes"])

    stop01_sidecar_rows_csv = out_dir / "run_root_sidecar_or_marker_rows.csv"
    write_csv(stop01_sidecar_rows_csv, stop01_sidecar_rows, [
        "manifest_file", "manifest_relative_path", "row_id", "row_json_prefix"
    ])

    field_inventory_json = out_dir / "run_root_marker_field_inventory.json"
    field_inventory = {
        "candidate_manifest_file_count": file_count,
        "scanned_row_count": row_count,
        "marker_like_field_counts": dict(marker_field_counts),
        "marker_like_nonempty_counts": dict(marker_field_nonempty_counts),
        "sidecar_or_marker_row_count": len(stop01_sidecar_rows),
    }
    field_inventory_json.write_text(json.dumps(field_inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "candidate_manifest_file_count": file_count,
        "scanned_manifest_row_count": row_count,
        "sidecar_or_marker_row_count": len(stop01_sidecar_rows),
        "marker_like_field_counts": dict(marker_field_counts),
        "marker_like_nonempty_counts": dict(marker_field_nonempty_counts),
        "run_root_candidate_manifest_files_csv": str(manifest_list_csv),
        "run_root_sidecar_or_marker_rows_csv": str(stop01_sidecar_rows_csv),
        "run_root_marker_field_inventory_json": str(field_inventory_json),
    }


def audit_specific_stop01_dir(run_root: Path, out_dir: Path) -> Dict[str, Any]:
    """
    Simple directory listing for files under Stop01-ish folders.
    """
    rows = []
    for p in sorted(run_root.rglob("*")):
        if not p.is_file():
            continue
        rel = safe_rel(p, run_root)
        if "stop01" in rel.lower() or rel.startswith("01") or "/01" in rel.lower():
            rows.append({
                "path": str(p),
                "relative_path": rel,
                "suffix": p.suffix.lower(),
                "size_bytes": p.stat().st_size if p.exists() else "",
            })

    out_csv = out_dir / "stop01_area_file_listing.csv"
    write_csv(out_csv, rows, ["path", "relative_path", "suffix", "size_bytes"])

    return {
        "stop01_area_file_count": len(rows),
        "stop01_area_file_listing_csv": str(out_csv),
    }


def write_summary_md(summary: Dict[str, Any], path: Path) -> None:
    src = summary["source_sidecar"]
    run = summary["run_root_manifest"]
    stop01 = summary["stop01_area"]

    lines = [
        "# Stop03-2B Sidecar + Stop01 Backtrace Audit",
        "",
        f"- source_root: {summary['source_root']}",
        f"- run_root: {summary['run_root']}",
        f"- source_safety: {summary['source_safety']}",
        "",
        "## Source folder sidecars",
        f"- source_media_file_count: {src['source_media_file_count']}",
        f"- source_sidecar_count: {src['source_sidecar_count']}",
        f"- source_sidecar_ext_counts: {src['source_sidecar_ext_counts']}",
        f"- matched_sidecar_count: {src['matched_sidecar_count']}",
        f"- orphan_sidecar_count: {src['orphan_sidecar_count']}",
        f"- sidecar_with_selection_marker_count: {src['sidecar_with_selection_marker_count']}",
        f"- sidecar_content_guess_counts: {src['sidecar_content_guess_counts']}",
        f"- source_sidecar_inventory_csv: {src['source_sidecar_inventory_csv']}",
        f"- source_media_sidecar_matches_csv: {src['source_media_sidecar_matches_csv']}",
        f"- source_orphan_sidecars_csv: {src['source_orphan_sidecars_csv']}",
        "",
        "## Run-root / Stop01 manifest backtrace",
        f"- candidate_manifest_file_count: {run['candidate_manifest_file_count']}",
        f"- scanned_manifest_row_count: {run['scanned_manifest_row_count']}",
        f"- sidecar_or_marker_row_count: {run['sidecar_or_marker_row_count']}",
        f"- marker_like_field_counts: {run['marker_like_field_counts']}",
        f"- marker_like_nonempty_counts: {run['marker_like_nonempty_counts']}",
        f"- run_root_candidate_manifest_files_csv: {run['run_root_candidate_manifest_files_csv']}",
        f"- run_root_sidecar_or_marker_rows_csv: {run['run_root_sidecar_or_marker_rows_csv']}",
        f"- run_root_marker_field_inventory_json: {run['run_root_marker_field_inventory_json']}",
        "",
        "## Stop01-area file listing",
        f"- stop01_area_file_count: {stop01['stop01_area_file_count']}",
        f"- stop01_area_file_listing_csv: {stop01['stop01_area_file_listing_csv']}",
        "",
        "## Interpretation guide",
        "- If sidecar_with_selection_marker_count > 0 but Stop03-2 image_marked_count is 0, Stop03-2 image marker handling is wrong.",
        "- If source_sidecar_count > 0 but sidecar_with_selection_marker_count is 0, sidecars exist but no user-pick marker was found.",
        "- If run_root marker_like fields are empty, Stop01/Stop02 did not preserve marker metadata in their manifest contracts.",
        "- Embedded RAW/JPEG/XMP metadata still requires exiftool or another metadata reader; this script focuses on sidecar files and existing manifests.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    source_root = Path(args.source_root)
    run_root = Path(args.run_root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "source_root": str(source_root),
        "run_root": str(run_root),
        "out": str(out_dir),
        "source_safety": "read_only_no_move_no_delete_no_modify_original_media",
    }

    summary["source_sidecar"] = audit_source_sidecars(source_root, out_dir)
    summary["run_root_manifest"] = audit_stop_manifests(run_root, out_dir)
    summary["stop01_area"] = audit_specific_stop01_dir(run_root, out_dir)

    summary_json = out_dir / "stop03_2b_sidecar_stop01_backtrace_summary.json"
    summary_md = out_dir / "stop03_2b_sidecar_stop01_backtrace_summary.md"

    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(summary, summary_md)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print("SUMMARY_MD=", summary_md)


if __name__ == "__main__":
    main()
