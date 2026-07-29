#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the V22 video-frame contact sheet from dry-run reports only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

import stop03_2_v22_phase1_readonly_selfcheck_20260710_110619 as phase1


SCRIPT_VERSION = "stop03_2_v22_video_frame_contact_sheet_20260710_112936"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V22_OUT = (
    TEST_OUTPUT_ROOT
    / "stop03-2-candidate-queues-db-safe-v22_0_20260710_112936_dry_run"
)
DEFAULT_OUT_DIR = (
    TEST_OUTPUT_ROOT
    / "stop03-2-v22-video-frame-contact-sheet-20260710_112936"
)


def assert_under_test_output(path: Path, *, must_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    root = TEST_OUTPUT_ROOT.resolve(strict=False)
    if not phase1.is_relative_to(resolved, root) or resolved == root:
        raise RuntimeError(f"path_outside_test_output:{resolved}")
    if must_exist and not resolved.exists():
        raise RuntimeError(f"missing_path:{resolved}")
    return resolved


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def timecode(time_ms: Any) -> str:
    try:
        value = max(0, int(float(time_ms)))
    except (TypeError, ValueError):
        return "--:--.---"
    seconds, millis = divmod(value, 1000)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minute:02d}:{second:02d}.{millis:03d}"
    return f"{minute:02d}:{second:02d}.{millis:03d}"


def frame_class(row: Mapping[str, Any]) -> str:
    if row.get("black_frame_status") != "ok" or row.get("tail_excluded"):
        return "frame excluded"
    role = str(row.get("qwen_role") or "")
    if role == "video_coverage_high_signal_overlap":
        return "frame overlap"
    if role == "video_high_signal_supplement":
        return "frame supplement"
    if role == "video_coverage_keyframe":
        return "frame coverage"
    if int(row.get("label_count") or 0) > 0:
        return "frame labeled"
    return "frame ordinary"


def resolve_derived_frame(raw: str) -> tuple[Optional[Path], str]:
    path, status = phase1.resolve_allowed_existing_file(raw, TEST_OUTPUT_ROOT)
    if status != "ok" or path is None:
        return None, status
    return path, "ok"


def safe_asset_filename(row: Mapping[str, Any], source: Path, index: int) -> str:
    visual_unit_id = re.sub(
        r"[^A-Za-z0-9_-]+", "_", str(row.get("visual_unit_id") or "frame")
    ).strip("_")
    visual_unit_id = (visual_unit_id or "frame")[:64]
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".jpg"
    return f"{index:04d}_{visual_unit_id}_{digest}{suffix}"


def materialize_assets(
    decisions: Sequence[Mapping[str, Any]], out_dir: Path
) -> tuple[Dict[str, str], Dict[str, Any]]:
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=False, exist_ok=False)
    asset_src_by_visual_file: Dict[str, str] = {}
    method_counts: Counter[str] = Counter()
    failures: List[Dict[str, str]] = []
    for index, row in enumerate(decisions, start=1):
        raw = str(row.get("visual_file") or "")
        if raw in asset_src_by_visual_file:
            continue
        source, status = resolve_derived_frame(raw)
        if status != "ok" or source is None:
            failures.append({"visual_file": raw, "reason": status})
            continue
        filename = safe_asset_filename(row, source, index)
        asset = assets_dir / filename
        relative_target = os.path.relpath(source, start=assets_dir)
        method = "symlink"
        try:
            asset.symlink_to(relative_target)
            if not asset.is_file():
                raise OSError("created_symlink_target_not_readable")
        except OSError as symlink_error:
            if asset.is_symlink():
                asset.unlink()
            method = "copy"
            try:
                shutil.copyfile(source, asset)
                asset.chmod(0o444)
                if not asset.is_file():
                    raise OSError("copied_asset_not_readable")
            except OSError as copy_error:
                failures.append(
                    {
                        "visual_file": raw,
                        "reason": (
                            f"symlink_failed:{type(symlink_error).__name__}:"
                            f"{symlink_error};copy_failed:{type(copy_error).__name__}:{copy_error}"
                        ),
                    }
                )
                continue
        asset_src_by_visual_file[raw] = f"assets/{filename}"
        method_counts[method] += 1
    return asset_src_by_visual_file, {
        "asset_unique_source_count": len(asset_src_by_visual_file),
        "asset_symlink_count": method_counts.get("symlink", 0),
        "asset_copy_fallback_count": method_counts.get("copy", 0),
        "asset_materialize_failure_count": len(failures),
        "asset_materialize_failures": failures,
    }


class ImgSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[tuple[str, Optional[str]]]
    ) -> None:
        if tag.lower() != "img":
            return
        values = {name.lower(): value for name, value in attrs}
        self.sources.append(values.get("src") or "")


def validate_html_assets(page: str, out_dir: Path) -> Dict[str, Any]:
    parser = ImgSrcParser()
    parser.feed(page)
    sources = parser.sources
    relative_assets_count = 0
    absolute_path_count = 0
    file_url_count = 0
    missing_asset_count = 0
    invalid_sources: List[str] = []
    for src in sources:
        lowered = src.lower()
        is_file_url = lowered.startswith("file://")
        is_absolute = src.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", src))
        file_url_count += int(is_file_url)
        absolute_path_count += int(is_absolute)
        pure = PurePosixPath(src)
        is_relative_asset = (
            not is_file_url
            and not is_absolute
            and len(pure.parts) == 2
            and pure.parts[0] == "assets"
            and pure.parts[1] not in {"", ".", ".."}
            and ".." not in pure.parts
            and "?" not in src
            and "#" not in src
        )
        if is_relative_asset:
            relative_assets_count += 1
            if not (out_dir / pure.parts[0] / pure.parts[1]).is_file():
                missing_asset_count += 1
        else:
            invalid_sources.append(src)
            missing_asset_count += 1
    total = len(sources)
    status = (
        "PASS"
        if total > 0
        and relative_assets_count == total
        and absolute_path_count == 0
        and file_url_count == 0
        and missing_asset_count == 0
        else "FAIL"
    )
    return {
        "html_img_total_count": total,
        "html_img_relative_assets_count": relative_assets_count,
        "html_img_absolute_path_count": absolute_path_count,
        "html_img_file_url_count": file_url_count,
        "html_img_missing_asset_count": missing_asset_count,
        "html_img_http_accessible_check_status": status,
        "html_img_invalid_sources": invalid_sources,
    }


def escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def render_frame(
    row: Mapping[str, Any], index: int, asset_src_by_visual_file: Mapping[str, str]
) -> tuple[str, str]:
    raw = str(row.get("visual_file") or "")
    image_uri = asset_src_by_visual_file.get(raw, "")
    image_status = "ok" if image_uri else "asset_not_materialized"
    image = (
        f'<img loading="lazy" src="{escape(image_uri)}" alt="derived frame">'
        if image_uri
        else f'<div class="missing">image unavailable: {escape(image_status)}</div>'
    )
    qwen_role = str(row.get("qwen_role") or "")
    ocr_role = str(row.get("ocr_role") or "")
    ocr_bar = (
        f'<div class="ocr-bar">OCR: {escape(ocr_role)}</div>' if row.get("ocr_selected") else ""
    )
    reason_codes = row.get("decision_reason_codes") or []
    if isinstance(reason_codes, str):
        reason_codes = [reason for reason in reason_codes.split("|") if reason]
    reason_html = "<br>".join(escape(reason) for reason in reason_codes)
    badges = []
    if qwen_role:
        badges.append(f'<span class="badge qwen">{escape(qwen_role)}</span>')
    if row.get("v14_role"):
        badges.append(f'<span class="badge v14">{escape(row.get("v14_role"))}</span>')
    if row.get("tail_excluded"):
        badges.append('<span class="badge danger">tail excluded</span>')
    if row.get("black_frame_status") != "ok":
        badges.append(f'<span class="badge danger">{escape(row.get("black_frame_status"))}</span>')
    card = f"""
    <article class="{frame_class(row)}" data-selected="{int(bool(row.get('qwen_selected') or row.get('ocr_selected')))}">
      {ocr_bar}
      <div class="image-wrap">{image}</div>
      <div class="meta">
        <div class="time">#{index + 1} · {escape(timecode(row.get('time_position_ms')))}</div>
        <div><b>time_position_ms:</b> {escape(row.get('time_position_ms'))}</div>
        <div><b>window:</b> {escape(row.get('coverage_window_index'))}</div>
        <div><b>labels ({escape(row.get('label_count'))}):</b> {escape(row.get('labels')) or '—'}</div>
        <div><b>grid structure/std:</b> {escape(row.get('grid_structure'))} / {escape(row.get('grid_luma_std'))}</div>
        <div class="badges">{''.join(badges)}</div>
        <details><summary>reason_codes</summary><div class="reasons">{reason_html or '—'}</div></details>
      </div>
    </article>
    """
    return card, image_status


def build_html(
    summary: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    budgets: Sequence[Mapping[str, Any]],
    asset_src_by_visual_file: Mapping[str, str],
) -> tuple[str, Dict[str, Any]]:
    by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in decisions:
        by_source[str(row.get("source_content_id") or "")].append(row)
    budget_by_source = {str(row.get("source_content_id") or ""): row for row in budgets}
    sections: List[str] = []
    image_status_counts: Counter[str] = Counter()
    for source_content_id, frames in sorted(
        by_source.items(), key=lambda item: str(item[1][0].get("source_relative_path") or "")
    ):
        frames = sorted(
            frames,
            key=lambda row: (int(row.get("time_position_ms") or -1), str(row.get("visual_unit_id") or "")),
        )
        budget = budget_by_source.get(source_content_id, {})
        cards: List[str] = []
        for index, row in enumerate(frames):
            card, image_status = render_frame(row, index, asset_src_by_visual_file)
            cards.append(card)
            image_status_counts[image_status] += 1
        source_path = str(frames[0].get("source_relative_path") or "") if frames else ""
        sections.append(
            f"""
            <section class="video-group" data-screen="{escape(budget.get('screen_capture', 0))}">
              <header>
                <h2>{escape(source_path)}</h2>
                <div class="source-id">{escape(source_content_id)}</div>
                <div class="group-stats">
                  frames {escape(budget.get('step02_frame_count'))} · duration {escape(budget.get('duration_ms'))}ms ·
                  coverage {escape(budget.get('coverage_count'))} · overlap {escape(budget.get('overlap_count'))} ·
                  supplement {escape(budget.get('supplement_count'))} · OCR {escape(budget.get('ocr_count'))} ·
                  YOLOE frames {escape(budget.get('yoloe_frame_count'))} · max gap {escape(budget.get('max_coverage_gap_ms'))}ms
                </div>
              </header>
              <div class="frames">{''.join(cards)}</div>
            </section>
            """
        )
    legend = """
      <span class="legend ordinary">普通 Step02</span>
      <span class="legend labeled">YOLOE label</span>
      <span class="legend coverage">coverage</span>
      <span class="legend supplement">supplement</span>
      <span class="legend overlap">coverage + high signal</span>
      <span class="legend ocr">OCR</span>
      <span class="legend excluded">black/tail/excluded</span>
    """
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stop03-2 V22 Video Frame Contact Sheet</title>
<style>
:root {{ color-scheme: dark; --bg:#101216; --panel:#181c22; --text:#edf1f7; --muted:#98a2b3; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.top {{ position:sticky; top:0; z-index:20; padding:16px 22px; background:rgba(16,18,22,.96); border-bottom:1px solid #343a44; backdrop-filter:blur(12px); }}
h1 {{ margin:0 0 8px; font-size:22px; }}
.summary {{ color:var(--muted); margin-bottom:10px; }}
.controls button {{ margin-right:8px; border:1px solid #505968; border-radius:7px; padding:7px 11px; background:#222832; color:var(--text); cursor:pointer; }}
.legend {{ display:inline-block; margin:5px 5px 0 0; padding:4px 8px; border-radius:5px; border:2px solid transparent; }}
.legend.ordinary {{ background:#5b616b; }} .legend.labeled {{ background:#287a4e; }} .legend.coverage {{ background:#2563eb; }}
.legend.supplement {{ background:#b4232f; }} .legend.overlap {{ background:#7c3aed; }} .legend.ocr {{ background:#d8a900; color:#111; }}
.legend.excluded {{ background:#08090b; border-color:#555; }}
main {{ padding:18px; }}
.video-group {{ margin-bottom:30px; border:1px solid #313844; border-radius:12px; background:var(--panel); overflow:hidden; }}
.video-group>header {{ padding:14px 16px; border-bottom:1px solid #313844; }}
.video-group h2 {{ margin:0 0 4px; font-size:17px; overflow-wrap:anywhere; }}
.source-id,.group-stats {{ color:var(--muted); overflow-wrap:anywhere; }}
.frames {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:11px; padding:12px; }}
.frame {{ position:relative; min-width:0; border:3px solid #5b616b; border-radius:9px; overflow:hidden; background:#11151a; }}
.frame.labeled {{ border-color:#2d9b63; }} .frame.coverage {{ border-color:#3478f6; }}
.frame.supplement {{ border-color:#e13d49; }} .frame.overlap {{ border-color:#9b67f6; }}
.frame.excluded {{ border-color:#111; opacity:.58; filter:saturate(.45); }}
.image-wrap {{ aspect-ratio:16/9; background:#090b0e; display:flex; align-items:center; justify-content:center; }}
.image-wrap img {{ width:100%; height:100%; object-fit:contain; }} .missing {{ padding:12px; color:#fda29b; }}
.ocr-bar {{ position:absolute; z-index:2; top:0; left:0; right:0; padding:3px 7px; background:#f4cf37; color:#17140a; font-weight:700; }}
.meta {{ padding:9px; overflow-wrap:anywhere; }} .time {{ font-weight:750; margin-bottom:5px; }}
.badges {{ margin-top:7px; }} .badge {{ display:inline-block; margin:2px 4px 2px 0; padding:2px 5px; border-radius:4px; background:#343b47; font-size:11px; }}
.badge.qwen {{ background:#254f9c; }} .badge.v14 {{ background:#6841c6; }} .badge.danger {{ background:#7a271a; }}
details {{ margin-top:7px; }} summary {{ cursor:pointer; color:#b9c4d4; }} .reasons {{ margin-top:5px; color:#aeb8c7; font-size:12px; }}
body.selected-only .frame[data-selected="0"] {{ display:none; }}
body.hide-screen .video-group[data-screen="1"] {{ display:none; }}
</style>
</head>
<body>
<div class="top">
  <h1>Stop03-2 V22 Video Frame Contact Sheet</h1>
  <div class="summary">technical {escape(summary.get('technical_status'))} · policy {escape(summary.get('policy_status'))} · Qwen {escape(summary.get('qwenvl_total_count'))} · video {escape(summary.get('qwen_video_frame_count'))} · OCR {escape(summary.get('ocr_total_count'))} · windows {escape(summary.get('coverage_window_total_count'))}</div>
  <div>{legend}</div>
  <div class="controls"><button onclick="document.body.classList.toggle('selected-only')">仅候选</button><button onclick="document.body.classList.toggle('hide-screen')">隐藏录屏</button></div>
</div>
<main>{''.join(sections)}</main>
</body>
</html>
"""
    audit = {
        "video_group_count": len(by_source),
        "frame_count": len(decisions),
        "image_status_counts": dict(image_status_counts),
        "missing_image_count": len(decisions) - image_status_counts.get("ok", 0),
        "coverage_selected_frame_count": sum(1 for row in decisions if row.get("qwen_selected")),
        "ocr_selected_frame_count": sum(1 for row in decisions if row.get("ocr_selected")),
    }
    return page, audit


def preflight(db: Path, v22_out: Path, out_dir: Path) -> Dict[str, Any]:
    con = phase1.connect_readonly(db)
    try:
        raw_db_video_count = int(
            con.execute(
                "SELECT COUNT(*) FROM visual_units vu JOIN source_assets sa ON sa.source_content_id=vu.source_content_id WHERE sa.media_type='video'"
            ).fetchone()[0]
        )
        db_video_count = int(
            con.execute(
                "SELECT COUNT(*) FROM canonical_visual_units_for_heavy vu JOIN source_assets sa ON sa.source_content_id=vu.source_content_id WHERE sa.media_type='video'"
            ).fetchone()[0]
        )
    finally:
        con.close()
    required = {
        "summary": v22_out / "reports" / "stop03_2_candidate_summary.json",
        "decisions": v22_out / "reports" / "video_frame_decisions.jsonl",
        "budget": v22_out / "reports" / "video_budget_report.csv",
    }
    exists = {name: path.is_file() for name, path in required.items()}
    status = "PASS" if all(exists.values()) else "FAIL"
    return {
        "validation_status": status,
        "technical_status": status,
        "script_version": SCRIPT_VERSION,
        "raw_db_video_visual_unit_count": raw_db_video_count,
        "db_video_visual_unit_count": db_video_count,
        "visual_input_source": "canonical_visual_units_for_heavy",
        "required_inputs": {name: str(path) for name, path in required.items()},
        "required_input_exists": exists,
        "out_dir_checked_not_created": str(out_dir),
        "sqlite_write": False,
        "original_video_read": False,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V22 video-frame contact sheet")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--v22-out", default=str(DEFAULT_V22_OUT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    phase1.set_offline_environment()
    try:
        db = Path(args.db).expanduser().resolve(strict=True)
        v22_out = assert_under_test_output(Path(args.v22_out), must_exist=True)
        out_dir = assert_under_test_output(Path(args.out_dir), must_exist=False)
        if out_dir.exists() and any(out_dir.iterdir()):
            raise RuntimeError(f"out_dir_not_empty:{out_dir}")
        check = preflight(db, v22_out, out_dir)
        if args.preflight_only:
            print(json.dumps(check, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if check["technical_status"] == "PASS" else 2
        if check["technical_status"] != "PASS":
            raise RuntimeError("contact_sheet_preflight_failed")
        summary = read_json(v22_out / "reports" / "stop03_2_candidate_summary.json")
        decisions = read_jsonl(v22_out / "reports" / "video_frame_decisions.jsonl")
        budgets = read_csv(v22_out / "reports" / "video_budget_report.csv")
        out_dir.mkdir(parents=True, exist_ok=False)
        asset_src_by_visual_file, asset_audit = materialize_assets(decisions, out_dir)
        if asset_audit["asset_materialize_failure_count"]:
            raise RuntimeError(
                f"contact_sheet_asset_materialize_failed:"
                f"{asset_audit['asset_materialize_failure_count']}"
            )
        page, audit = build_html(summary, decisions, budgets, asset_src_by_visual_file)
        if audit["frame_count"] != check["db_video_visual_unit_count"]:
            raise RuntimeError(
                f"decision_frame_count_mismatch:{audit['frame_count']}!={check['db_video_visual_unit_count']}"
            )
        if audit["missing_image_count"]:
            raise RuntimeError(f"contact_sheet_missing_images:{audit['missing_image_count']}")
        html_path = out_dir / "v22_video_frame_contact_sheet.html"
        audit_path = out_dir / "contact_sheet_summary.json"
        html_path.write_text(page, encoding="utf-8")
        html_asset_audit = validate_html_assets(page, out_dir)
        if html_asset_audit["html_img_total_count"] != audit["frame_count"]:
            raise RuntimeError(
                f"contact_sheet_img_count_mismatch:"
                f"{html_asset_audit['html_img_total_count']}!={audit['frame_count']}"
            )
        if html_asset_audit["html_img_http_accessible_check_status"] != "PASS":
            raise RuntimeError("contact_sheet_html_asset_validation_failed")
        result = {
            "validation_status": "PASS",
            "technical_status": "PASS",
            "policy_status": "REVIEW",
            "script_version": SCRIPT_VERSION,
            "html_path": str(html_path),
            "audit_path": str(audit_path),
            "audit": {**audit, **asset_audit, **html_asset_audit},
            **html_asset_audit,
            "sqlite_write": False,
            "original_video_read": False,
            "derived_frame_access": "read_only_symlink_or_copy_fallback",
        }
        audit_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result = {
            "validation_status": "FAIL",
            "technical_status": "FAIL",
            "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "sqlite_write": False,
            "original_video_read": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
