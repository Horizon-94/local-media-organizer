#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the V22.1 visual-quality contact sheet from dry-run reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import stop03_2_v22_phase1_readonly_selfcheck_20260710_110619 as phase1
import stop03_2_v22_video_frame_contact_sheet_20260710_112936 as v22_sheet


SCRIPT_VERSION = "stop03_2_v22_1_video_frame_contact_sheet_20260710_132500"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_V22_1_OUT = (
    TEST_OUTPUT_ROOT / "stop03-2-candidate-queues-db-safe-v22_1_dry_run"
)
DEFAULT_OUT_DIR = (
    TEST_OUTPUT_ROOT / "stop03-2-v22-1-video-frame-contact-sheet"
)
ORIGINAL_BUILD_HTML = v22_sheet.build_html


def json_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return "—"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_frame_v22_1(
    row: Mapping[str, Any],
    index: int,
    asset_src_by_visual_file: Mapping[str, str],
) -> tuple[str, str]:
    raw = str(row.get("visual_file") or "")
    image_uri = asset_src_by_visual_file.get(raw, "")
    image_status = "ok" if image_uri else "asset_not_materialized"
    image = (
        f'<img loading="lazy" src="{v22_sheet.escape(image_uri)}" alt="derived frame">'
        if image_uri
        else f'<div class="missing">image unavailable: {v22_sheet.escape(image_status)}</div>'
    )
    qwen_role = str(row.get("qwen_role") or "")
    ocr_role = str(row.get("ocr_role") or "")
    ocr_bar = (
        f'<div class="ocr-bar">OCR: {v22_sheet.escape(ocr_role)}</div>'
        if row.get("ocr_selected") else ""
    )
    badges = []
    if qwen_role:
        badges.append(f'<span class="badge qwen">{v22_sheet.escape(qwen_role)}</span>')
    if row.get("v14_role"):
        badges.append(f'<span class="badge v14">{v22_sheet.escape(row.get("v14_role"))}</span>')
    if row.get("tail_status") == "tail_protected":
        badges.append('<span class="badge danger">tail protected</span>')
    if row.get("black_frame_status") != "ok":
        badges.append(
            f'<span class="badge danger">{v22_sheet.escape(row.get("black_frame_status"))}</span>'
        )
    selected_reasons = row.get("selection_reason_codes") or []
    rejected_reasons = row.get("rejection_reason_codes") or []
    decision_reasons = row.get("decision_reason_codes") or []
    card = f"""
    <article class="{v22_sheet.frame_class(row)}" data-selected="{int(bool(row.get('qwen_selected') or row.get('ocr_selected')))}">
      {ocr_bar}
      <div class="image-wrap">{image}</div>
      <div class="meta">
        <div class="time">#{index + 1} · {v22_sheet.escape(v22_sheet.timecode(row.get('time_position_ms')))}</div>
        <div><b>time / window / rank:</b> {v22_sheet.escape(row.get('time_position_ms'))} / {v22_sheet.escape(row.get('coverage_window_index'))} / {v22_sheet.escape(row.get('window_rank'))}</div>
        <div><b>labels ({v22_sheet.escape(row.get('label_count'))}):</b> {v22_sheet.escape(row.get('labels')) or '—'}</div>
        <div><b>human quality:</b> {v22_sheet.escape(row.get('human_quality_score'))}</div>
        <div><b>sharpness:</b> {v22_sheet.escape(row.get('sharpness_score'))}</div>
        <div><b>tail:</b> {v22_sheet.escape(row.get('tail_status'))} / {v22_sheet.escape(row.get('tail_action'))}</div>
        <div><b>supplement gain:</b> {v22_sheet.escape(row.get('supplement_information_gain'))} · {v22_sheet.escape(json_text(row.get('supplement_gain_reason_codes')))}</div>
        <div><b>nearest vector/grid:</b> {v22_sheet.escape(row.get('vector_cosine_to_nearest_selected'))} / MAD {v22_sheet.escape(row.get('grid_mad_to_nearest_selected'))} / corr {v22_sheet.escape(row.get('grid_corr_to_nearest_selected'))}</div>
        <div class="badges">{''.join(badges)}</div>
        <details><summary>human_quality_components</summary><div class="reasons">{v22_sheet.escape(json_text(row.get('human_quality_components')))}</div></details>
        <details><summary>selection_reason_codes</summary><div class="reasons">{v22_sheet.escape(json_text(selected_reasons))}</div></details>
        <details><summary>rejection_reason_codes</summary><div class="reasons">{v22_sheet.escape(json_text(rejected_reasons))}</div></details>
        <details><summary>all decision reasons</summary><div class="reasons">{v22_sheet.escape(json_text(decision_reasons))}</div></details>
      </div>
    </article>
    """
    return card, image_status


def build_html_v22_1(
    summary: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    budgets: Sequence[Mapping[str, Any]],
    asset_src_by_visual_file: Mapping[str, str],
) -> tuple[str, Dict[str, Any]]:
    v22_sheet.render_frame = render_frame_v22_1
    page, audit = ORIGINAL_BUILD_HTML(
        summary, decisions, budgets, asset_src_by_visual_file
    )
    quality_summary = (
        '<div class="summary v22-1-quality">'
        f"human replacements {v22_sheet.escape(summary.get('human_quality_replacement_count'))} · "
        f"last frames {v22_sheet.escape(summary.get('selected_last_frame_count'))} · "
        f"tail selected {v22_sheet.escape(summary.get('selected_tail_protected_frame_count'))} · "
        f"supplement gain pass {v22_sheet.escape(summary.get('supplement_information_gain_pass_count'))} · "
        f"supplement gain reject {v22_sheet.escape(summary.get('supplement_information_gain_reject_count'))}"
        "</div>"
    )
    page = page.replace('<div class="controls">', quality_summary + '\n<div class="controls">', 1)
    page = page.replace(
        "Stop03-2 V22 Video Frame Contact Sheet",
        "Stop03-2 V22.1 Visual Quality Contact Sheet",
    )
    audit["human_quality_field_count"] = sum(
        1 for row in decisions if row.get("human_quality_score") is not None
    )
    audit["sharpness_field_count"] = sum(
        1 for row in decisions if row.get("sharpness_score") is not None
    )
    audit["tail_status_field_count"] = sum(
        1 for row in decisions if str(row.get("tail_status") or "")
    )
    audit["supplement_audit_field_count"] = sum(
        1 for row in decisions if "supplement_information_gain" in row
    )
    return page, audit


def preflight(db: Path, v22_1_out: Path, out_dir: Path) -> Dict[str, Any]:
    con = phase1.connect_readonly(db)
    try:
        db_video_count = int(
            con.execute(
                "SELECT COUNT(*) FROM visual_units vu JOIN source_assets sa "
                "ON sa.source_content_id=vu.source_content_id WHERE sa.media_type='video'"
            ).fetchone()[0]
        )
    finally:
        con.close()
    required = {
        "summary": v22_1_out / "reports" / "stop03_2_candidate_summary.json",
        "decisions": v22_1_out / "reports" / "video_frame_decisions.jsonl",
        "budget": v22_1_out / "reports" / "video_budget_report.csv",
        "human_replacements": v22_1_out / "reports" / "human_quality_replacements.jsonl",
        "supplement_audit": v22_1_out / "reports" / "supplement_information_gain_audit.jsonl",
    }
    exists = {name: path.is_file() for name, path in required.items()}
    status = "PASS" if all(exists.values()) else "FAIL"
    return {
        "validation_status": status,
        "technical_status": status,
        "policy_status": "REVIEW",
        "commit_status": "DO_NOT_COMMIT",
        "visual_review_status": "NOT_RUN",
        "script_version": SCRIPT_VERSION,
        "db_video_visual_unit_count": db_video_count,
        "required_inputs": {name: str(path) for name, path in required.items()},
        "required_input_exists": exists,
        "out_dir_checked_not_created": str(out_dir),
        "sqlite_write": False,
        "original_video_read": False,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V22.1 visual-quality contact sheet")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--v22-1-out", default=str(DEFAULT_V22_1_OUT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    phase1.set_offline_environment()
    try:
        db = Path(args.db).expanduser().resolve(strict=True)
        v22_1_out = v22_sheet.assert_under_test_output(
            Path(args.v22_1_out), must_exist=True
        )
        out_dir = v22_sheet.assert_under_test_output(Path(args.out_dir), must_exist=False)
        if out_dir.exists() and any(out_dir.iterdir()):
            raise RuntimeError(f"out_dir_not_empty:{out_dir}")
        check = preflight(db, v22_1_out, out_dir)
        if args.preflight_only:
            print(json.dumps(check, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if check["technical_status"] == "PASS" else 2
        if check["technical_status"] != "PASS":
            raise RuntimeError("v22_1_contact_sheet_preflight_failed")
        summary = v22_sheet.read_json(
            v22_1_out / "reports" / "stop03_2_candidate_summary.json"
        )
        decisions = v22_sheet.read_jsonl(
            v22_1_out / "reports" / "video_frame_decisions.jsonl"
        )
        budgets = v22_sheet.read_csv(
            v22_1_out / "reports" / "video_budget_report.csv"
        )
        out_dir.mkdir(parents=True, exist_ok=False)
        asset_map, asset_audit = v22_sheet.materialize_assets(decisions, out_dir)
        if asset_audit["asset_materialize_failure_count"]:
            raise RuntimeError("v22_1_contact_sheet_asset_materialize_failed")
        page, audit = build_html_v22_1(summary, decisions, budgets, asset_map)
        if audit["frame_count"] != check["db_video_visual_unit_count"]:
            raise RuntimeError("v22_1_contact_sheet_frame_count_mismatch")
        html_path = out_dir / "v22_1_video_frame_contact_sheet.html"
        audit_path = out_dir / "contact_sheet_summary.json"
        html_path.write_text(page, encoding="utf-8")
        html_audit = v22_sheet.validate_html_assets(page, out_dir)
        if html_audit["html_img_total_count"] != audit["frame_count"]:
            raise RuntimeError("v22_1_contact_sheet_img_count_mismatch")
        if html_audit["html_img_http_accessible_check_status"] != "PASS":
            raise RuntimeError("v22_1_contact_sheet_html_asset_validation_failed")
        result = {
            "validation_status": "PASS",
            "technical_status": "PASS",
            "policy_status": summary.get("policy_status", "REVIEW"),
            "commit_status": "DO_NOT_COMMIT",
            "visual_review_status": "READY_FOR_USER_REVIEW",
            "script_version": SCRIPT_VERSION,
            "html_path": str(html_path),
            "audit_path": str(audit_path),
            "audit": {**audit, **asset_audit, **html_audit},
            **html_audit,
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
            "policy_status": "REVIEW",
            "commit_status": "DO_NOT_COMMIT",
            "visual_review_status": "NOT_RUN",
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
