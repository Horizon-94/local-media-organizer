#!/usr/bin/env python3
import argparse
import csv
import json
import re
import shutil
import subprocess
from pathlib import Path
from collections import Counter, defaultdict

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".hif", ".tif", ".tiff",
    ".arw", ".dng", ".raf", ".cr2", ".cr3", ".nef", ".rw2", ".orf"
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".braw", ".crm"}
SIDECAR_EXTS = {".xmp", ".xml"}

MARKER_KEYS = [
    "rating", "label", "marked", "pick", "urgency",
    "xmp:rating", "xmp:label", "xmp:marked",
    "photoshop:urgency", "microsoftphoto:rating"
]

SOFTWARE_KEYS = [
    "photoshop", "camera raw", "bridge", "lightroom", "adobe",
    "creator tool", "crs:", "photoshop:"
]

def read_text_safe(p: Path):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def detect_markers_in_text(txt: str):
    low = txt.lower()
    hits = []

    patterns = {
        "xmp_rating_attr": r'xmp:Rating\s*=\s*"([^"]+)"',
        "xmp_rating_tag": r"<xmp:Rating>(.*?)</xmp:Rating>",
        "xmp_label_attr": r'xmp:Label\s*=\s*"([^"]+)"',
        "xmp_label_tag": r"<xmp:Label>(.*?)</xmp:Label>",
        "xmp_marked_attr": r'xmp:Marked\s*=\s*"([^"]+)"',
        "photoshop_urgency_attr": r'photoshop:Urgency\s*=\s*"([^"]+)"',
    }

    for name, pat in patterns.items():
        for m in re.finditer(pat, txt, flags=re.I | re.S):
            val = (m.group(1) or "").strip()
            if val:
                hits.append(f"{name}={val}")

    # 判断是否是真正选择标记，不把“被 Adobe 软件处理过”直接算高价值
    marked = False
    for h in hits:
        hv = h.lower()
        if "rating" in hv:
            val = hv.split("=", 1)[-1].strip()
            if val not in ("", "0", "-1", "none"):
                marked = True
        elif "label" in hv:
            val = hv.split("=", 1)[-1].strip()
            if val not in ("", "none"):
                marked = True
        elif "marked" in hv:
            val = hv.split("=", 1)[-1].strip()
            if val in ("true", "1", "pick", "yes"):
                marked = True
        elif "urgency" in hv:
            val = hv.split("=", 1)[-1].strip()
            if val not in ("", "0", "none"):
                marked = True

    software_evidence = []
    for k in SOFTWARE_KEYS:
        if k in low:
            software_evidence.append(k)

    return marked, hits, sorted(set(software_evidence))

def sidecar_match_keys(sidecar: Path):
    # 支持 IMG_0001.xmp 和 IMG_0001.ARW.xmp 两种
    s = sidecar.name
    stem = sidecar.stem
    keys = {stem}

    for ext in IMAGE_EXTS | VIDEO_EXTS:
        suffix = ext.lower()
        if stem.lower().endswith(suffix):
            keys.add(stem[: -len(suffix)])

    return keys

def collect_media(source_root: Path):
    media_by_dir_stem = defaultdict(list)
    sidecars = []
    media_files = []

    for p in source_root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
            media_files.append(p)
            media_by_dir_stem[(str(p.parent), p.stem)].append(p)
        elif ext in SIDECAR_EXTS:
            sidecars.append(p)

    return media_files, sidecars, media_by_dir_stem

def audit_sidecars(source_root: Path, out_dir: Path):
    media_files, sidecars, media_by_dir_stem = collect_media(source_root)

    rows = []
    matched = 0
    marked = 0
    software_only = 0

    for sc in sidecars:
        txt = read_text_safe(sc)
        is_marked, marker_hits, software_hits = detect_markers_in_text(txt)

        matches = []
        for key in sidecar_match_keys(sc):
            matches.extend(media_by_dir_stem.get((str(sc.parent), key), []))

        matches = sorted(set(matches))
        if matches:
            matched += 1
        if is_marked:
            marked += 1
        elif software_hits:
            software_only += 1

        rows.append({
            "sidecar_path": str(sc),
            "sidecar_relative_path": str(sc.relative_to(source_root)),
            "matched_media_count": len(matches),
            "matched_media_paths": " | ".join(str(m) for m in matches[:10]),
            "matched_media_exts": "|".join(sorted(set(m.suffix.lower() for m in matches))),
            "has_selection_marker": int(is_marked),
            "marker_hits": "|".join(marker_hits),
            "software_evidence": "|".join(software_hits),
        })

    out_csv = out_dir / "sidecar_marker_audit.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        fields = [
            "sidecar_path", "sidecar_relative_path",
            "matched_media_count", "matched_media_paths", "matched_media_exts",
            "has_selection_marker", "marker_hits", "software_evidence"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    return {
        "media_file_count": len(media_files),
        "sidecar_count": len(sidecars),
        "matched_sidecar_count": matched,
        "selection_marked_sidecar_count": marked,
        "software_evidence_only_sidecar_count": software_only,
        "sidecar_audit_csv": str(out_csv),
    }

def audit_embedded_exiftool(source_root: Path, out_dir: Path, limit: int):
    exiftool = shutil.which("exiftool")
    out_csv = out_dir / "embedded_exiftool_marker_audit.csv"

    if not exiftool:
        out_csv.write_text("exiftool_not_found\n", encoding="utf-8")
        return {
            "exiftool_available": False,
            "embedded_checked_count": 0,
            "embedded_selection_marked_count": 0,
            "embedded_audit_csv": str(out_csv),
        }

    media = [
        p for p in source_root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    if limit > 0:
        media = media[:limit]

    argfile = out_dir / "exiftool_argfile.txt"
    argfile.write_text("\n".join(str(p) for p in media), encoding="utf-8")

    cmd = [
        exiftool,
        "-j",
        "-Rating",
        "-Label",
        "-XMP:Rating",
        "-XMP:Label",
        "-XMP:Marked",
        "-Photoshop:Urgency",
        "-MicrosoftPhoto:Rating",
        "-Subject",
        "-HierarchicalSubject",
        "-CreatorTool",
        "-Software",
        "-RawFileName",
        "-@",
        str(argfile),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
    except Exception as e:
        out_csv.write_text(f"exiftool_failed,{e}\n", encoding="utf-8")
        return {
            "exiftool_available": True,
            "embedded_checked_count": 0,
            "embedded_selection_marked_count": 0,
            "embedded_error": str(e),
            "embedded_audit_csv": str(out_csv),
        }

    rows = []
    marked_count = 0

    for r in data:
        source = r.get("SourceFile", "")
        rating = str(r.get("Rating", "") or r.get("XMP:Rating", "") or "")
        label = str(r.get("Label", "") or r.get("XMP:Label", "") or "")
        marked = str(r.get("Marked", "") or r.get("XMP:Marked", "") or "")
        urgency = str(r.get("Urgency", "") or r.get("Photoshop:Urgency", "") or "")
        creator = str(r.get("CreatorTool", "") or "")
        software = str(r.get("Software", "") or "")

        selection = False
        if rating not in ("", "0", "-1", "None"):
            selection = True
        if label not in ("", "None"):
            selection = True
        if marked.lower() in ("true", "1", "pick", "yes"):
            selection = True
        if urgency not in ("", "0", "None"):
            selection = True

        if selection:
            marked_count += 1

        rows.append({
            "source_file": source,
            "has_selection_marker": int(selection),
            "rating": rating,
            "label": label,
            "marked": marked,
            "urgency": urgency,
            "creator_tool": creator,
            "software": software,
        })

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        fields = [
            "source_file", "has_selection_marker",
            "rating", "label", "marked", "urgency",
            "creator_tool", "software"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    return {
        "exiftool_available": True,
        "embedded_checked_count": len(rows),
        "embedded_selection_marked_count": marked_count,
        "embedded_audit_csv": str(out_csv),
    }

def audit_stop02_manifest(run_root: Path, out_dir: Path):
    manifest = run_root / "02_2_stop02_image_preview/manifests/image_preview_visual_unit_manifest.jsonl"
    out_json = out_dir / "stop02_image_manifest_marker_field_inventory.json"

    if not manifest.exists():
        out_json.write_text(json.dumps({"exists": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"stop02_image_manifest_exists": False, "inventory_json": str(out_json)}

    field_counter = Counter()
    marker_field_nonempty = Counter()
    rows = 0

    marker_patterns = re.compile(
        r"(xmp|xml|sidecar|rating|label|marked|marker|camera raw|bridge|photoshop|lightroom|metadata|exif)",
        re.I
    )

    with manifest.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows += 1
            try:
                r = json.loads(line)
            except Exception:
                continue
            for k, v in r.items():
                field_counter[k] += 1
                if marker_patterns.search(k):
                    if v not in ("", None, [], {}):
                        marker_field_nonempty[k] += 1

    marker_fields = sorted([k for k in field_counter if marker_patterns.search(k)])
    result = {
        "exists": True,
        "row_count": rows,
        "marker_like_fields": marker_fields,
        "marker_like_nonempty_counts": dict(marker_field_nonempty),
        "all_field_count": len(field_counter),
        "inventory_json": str(out_json),
    }
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "stop02_image_manifest_exists": True,
        "stop02_image_manifest_row_count": rows,
        "stop02_marker_like_field_count": len(marker_fields),
        "stop02_marker_like_nonempty_counts": dict(marker_field_nonempty),
        "inventory_json": str(out_json),
    }

def audit_stop03_image_marker(stop03_2_base: Path, out_dir: Path):
    hits = sorted(stop03_2_base.glob("**/*image*marker*.csv")) + sorted(stop03_2_base.glob("**/*marker*audit*.csv"))
    result = {
        "stop03_2_image_marker_audit_found": len(hits),
        "paths": [str(p) for p in hits],
    }

    if hits:
        p = hits[0]
        rows = []
        with p.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)

        result["first_audit_path"] = str(p)
        result["row_count"] = len(rows)
        c = Counter()
        for r in rows:
            for k, v in r.items():
                if "mark" in k.lower() or "rating" in k.lower() or "label" in k.lower():
                    if v not in ("", "0", "None", None):
                        c[k] += 1
        result["nonempty_marker_like_fields"] = dict(c)

    out_json = out_dir / "stop03_2_image_marker_audit_inventory.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["inventory_json"] = str(out_json)
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--stop03-2-base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exiftool-limit", type=int, default=0, help="0 means all images")
    args = ap.parse_args()

    source_root = Path(args.source_root)
    run_root = Path(args.run_root)
    stop03_2_base = Path(args.stop03_2_base)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "source_root": str(source_root),
        "run_root": str(run_root),
        "stop03_2_base": str(stop03_2_base),
        "source_safety": "read_only_no_move_no_delete_no_modify_original_media",
    }

    summary["sidecar"] = audit_sidecars(source_root, out_dir)
    summary["embedded_exiftool"] = audit_embedded_exiftool(source_root, out_dir, args.exiftool_limit)
    summary["stop02_manifest"] = audit_stop02_manifest(run_root, out_dir)
    summary["stop03_2_image_marker_audit"] = audit_stop03_image_marker(stop03_2_base, out_dir)

    summary_json = out_dir / "image_marker_backtrace_summary.json"
    summary_md = out_dir / "image_marker_backtrace_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Image Marker Backtrace Audit",
        "",
        f"- source_root: {summary['source_root']}",
        f"- run_root: {summary['run_root']}",
        f"- stop03_2_base: {summary['stop03_2_base']}",
        "",
        "## Sidecar",
        f"- sidecar_count: {summary['sidecar']['sidecar_count']}",
        f"- matched_sidecar_count: {summary['sidecar']['matched_sidecar_count']}",
        f"- selection_marked_sidecar_count: {summary['sidecar']['selection_marked_sidecar_count']}",
        f"- software_evidence_only_sidecar_count: {summary['sidecar']['software_evidence_only_sidecar_count']}",
        f"- sidecar_audit_csv: {summary['sidecar']['sidecar_audit_csv']}",
        "",
        "## Embedded exiftool",
        f"- exiftool_available: {summary['embedded_exiftool']['exiftool_available']}",
        f"- embedded_checked_count: {summary['embedded_exiftool']['embedded_checked_count']}",
        f"- embedded_selection_marked_count: {summary['embedded_exiftool']['embedded_selection_marked_count']}",
        f"- embedded_audit_csv: {summary['embedded_exiftool']['embedded_audit_csv']}",
        "",
        "## Stop02 image manifest",
        f"- exists: {summary['stop02_manifest']['stop02_image_manifest_exists']}",
        f"- row_count: {summary['stop02_manifest'].get('stop02_image_manifest_row_count')}",
        f"- marker_like_field_count: {summary['stop02_manifest'].get('stop02_marker_like_field_count')}",
        f"- marker_like_nonempty_counts: {summary['stop02_manifest'].get('stop02_marker_like_nonempty_counts')}",
        f"- inventory_json: {summary['stop02_manifest']['inventory_json']}",
        "",
        "## Stop03-2 image marker audit",
        f"- audit_found: {summary['stop03_2_image_marker_audit']['stop03_2_image_marker_audit_found']}",
        f"- nonempty_marker_like_fields: {summary['stop03_2_image_marker_audit'].get('nonempty_marker_like_fields')}",
        f"- inventory_json: {summary['stop03_2_image_marker_audit']['inventory_json']}",
    ]
    summary_md.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nSUMMARY_MD=", summary_md)

if __name__ == "__main__":
    main()
