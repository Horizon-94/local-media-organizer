#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT_VERSION = "stop03_2_v23_video_frame_contact_sheet_20260710_190836"
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")


def assert_under_test_output(path: Path, *, must_exist: bool) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(TEST_OUTPUT_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(f"path_outside_test_output:{resolved}") from exc
    if resolved == TEST_OUTPUT_ROOT.resolve():
        raise RuntimeError("path_must_not_equal_test_output_root")
    if must_exist and not resolved.exists():
        raise RuntimeError(f"required_path_missing:{resolved}")
    return resolved


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"jsonl_parse_failed:{path}:{line_number}:{exc}") from exc
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def timecode(value: Any) -> str:
    try:
        total_ms = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "--:--.---"
    seconds, millis = divmod(total_ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d}.{millis:03d}"


def materialize_assets(
    decisions: Sequence[Mapping[str, Any]], out_dir: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=False)
    mapping: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    symlink_count = 0
    copy_count = 0
    for row in decisions:
        visual_id = str(row.get("visual_unit_id") or "")
        source = Path(str(row.get("visual_file") or ""))
        if not visual_id or not source.is_file():
            failures.append({"visual_unit_id": visual_id, "path": str(source), "reason": "missing_derived_frame"})
            continue
        suffix = source.suffix.lower() if source.suffix else ".jpg"
        target = assets / f"{visual_id}{suffix}"
        try:
            os.symlink(source, target)
            symlink_count += 1
        except OSError:
            try:
                shutil.copy2(source, target)
                copy_count += 1
            except OSError as exc:
                failures.append({"visual_unit_id": visual_id, "path": str(source), "reason": str(exc)})
                continue
        mapping[visual_id] = f"assets/{target.name}"
    return mapping, {
        "asset_symlink_count": symlink_count,
        "asset_copy_fallback_count": copy_count,
        "asset_materialize_failure_count": len(failures),
        "asset_materialize_failures": failures,
    }


def esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def render_card(row: Mapping[str, Any], index: int, asset: str) -> str:
    selected = bool(row.get("qwen_selected") or row.get("ocr_selected"))
    role = str(row.get("qwen_role") or row.get("ocr_role") or "not_selected")
    reasons = row.get("selection_reason_codes") or row.get("decision_reason_codes") or []
    if isinstance(reasons, str):
        reasons = [part for part in reasons.split("|") if part]
    reverse_ids = str(row.get("duplicate_reverse_visual_unit_ids") or "")
    return f"""
    <article class="card {'selected' if selected else 'not-selected'}" data-selected="{int(selected)}" data-source="{esc(row.get('source_content_id'))}">
      <div class="image-wrap"><img loading="lazy" src="{esc(asset)}" alt="V23 derived frame"></div>
      <div class="meta">
        <div class="headline">#{index + 1} · {esc(timecode(row.get('time_position_ms')))} · {esc(role)}</div>
        <div><b>source</b> {esc(row.get('source_relative_path'))}</div>
        <div><b>visual / derived</b> {esc(row.get('visual_unit_id'))} / {esc(row.get('derived_id'))}</div>
        <div><b>frame / anchor</b> {esc(row.get('frame_index'))} / {esc(row.get('coverage_anchor_index'))}</div>
        <div><b>score</b> {esc(row.get('candidate_score'))} · <b>black</b> {esc(row.get('black_frame_status'))}</div>
        <div><b>labels</b> {esc(row.get('labels'))}</div>
        <div><b>duplicate group</b> {esc(row.get('duplicate_group_id'))} · members {esc(row.get('duplicate_reverse_member_count'))}</div>
        <details><summary>reverse mapping</summary><div class="wrap">{esc(reverse_ids)}</div></details>
        <details><summary>reason codes</summary><div class="wrap">{esc(' | '.join(str(item) for item in reasons))}</div></details>
      </div>
    </article>
    """


def build_html(
    summary: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    budgets: Sequence[Mapping[str, Any]],
    assets: Mapping[str, str],
) -> tuple[str, dict[str, Any]]:
    cards: list[str] = []
    missing = 0
    for index, row in enumerate(decisions):
        visual_id = str(row.get("visual_unit_id") or "")
        asset = assets.get(visual_id, "")
        if not asset:
            missing += 1
            continue
        cards.append(render_card(row, index, asset))
    group_count = len({str(row.get("source_content_id") or "") for row in decisions})
    role_counts = Counter(
        str(row.get("qwen_role") or row.get("ocr_role") or "not_selected")
        for row in decisions
    )
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stop03-2 V23 Video Frame Contact Sheet</title>
<style>
body{{margin:0;background:#f4f5f7;color:#1f2328;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}header{{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid #d0d7de;padding:14px 18px}}h1{{font-size:21px;margin:0 0 8px}}.summary{{font-size:13px;line-height:1.7}}.controls{{padding:10px 18px;background:#fff;border-bottom:1px solid #d0d7de}}button{{padding:6px 10px;margin-right:6px}}main{{padding:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}.card{{background:#fff;border:1px solid #d0d7de;border-radius:6px;overflow:hidden}}.card.selected{{border-color:#1a7f37}}.image-wrap{{height:220px;background:#111;display:grid;place-items:center}}img{{width:100%;height:100%;object-fit:contain}}.meta{{padding:10px;font-size:12px;line-height:1.55}}.headline{{font-weight:700;font-size:13px;margin-bottom:5px}}.wrap{{overflow-wrap:anywhere;color:#57606a}}body.only-selected .not-selected{{display:none}}
</style></head><body>
<header><h1>Stop03-2 V23 通用高价值帧与 OCR 候选审查</h1>
<div class="summary">technical {esc(summary.get('technical_status'))} · policy {esc(summary.get('policy_status'))} · canonical visual {esc(summary.get('canonical_visual_input_count'))} · Qwen {esc(summary.get('qwenvl_total_count'))} · video {esc(summary.get('qwen_video_frame_count'))} · OCR {esc(summary.get('ocr_total_count'))} · anchors {esc(summary.get('coverage_anchor_total_count'))} · groups {group_count}</div>
<div class="summary">coverage {esc(summary.get('normal_video_group_with_coverage_count'))}/{esc(summary.get('normal_video_group_count'))} · refill {esc(summary.get('coverage_refill_count'))} · refill failed {esc(summary.get('coverage_refill_failed_count'))} · central duplicate leak {esc(summary.get('central_duplicate_queue_leak_count'))}</div></header>
<div class="controls"><button onclick="document.body.classList.remove('only-selected')">全部 canonical 视频帧</button><button onclick="document.body.classList.add('only-selected')">仅入选</button></div>
<main>{''.join(cards)}</main></body></html>"""
    return page, {
        "frame_count": len(decisions),
        "rendered_frame_count": len(cards),
        "missing_image_count": missing,
        "video_group_count": group_count,
        "role_counts": dict(role_counts),
    }


def validate_html_assets(page: str, out_dir: Path) -> dict[str, Any]:
    sources: list[str] = []
    marker = 'src="'
    cursor = 0
    while True:
        start = page.find(marker, cursor)
        if start < 0:
            break
        start += len(marker)
        end = page.find('"', start)
        if end < 0:
            break
        sources.append(page[start:end])
        cursor = end + 1
    invalid = [src for src in sources if src.startswith(("file://", "/")) or ".." in Path(src).parts]
    missing = [src for src in sources if not (out_dir / src).exists()]
    return {
        "html_img_total_count": len(sources),
        "html_img_relative_asset_count": len(sources) - len(invalid),
        "html_img_invalid_source_count": len(invalid),
        "html_img_missing_asset_count": len(missing),
        "html_asset_validation_status": "PASS" if not invalid and not missing else "FAIL",
        "html_img_invalid_sources": invalid,
        "html_img_missing_sources": missing,
    }


def generate_contact_sheet(v23_out: Path, out_dir: Path) -> dict[str, Any]:
    v23_out = assert_under_test_output(v23_out, must_exist=True)
    out_dir = assert_under_test_output(out_dir, must_exist=False)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"contact_sheet_output_not_empty:{out_dir}")
    summary_path = v23_out / "reports" / "stop03_2_candidate_summary.json"
    decisions_path = v23_out / "reports" / "video_frame_decisions.jsonl"
    budget_path = v23_out / "reports" / "video_budget_report.csv"
    for path in (summary_path, decisions_path, budget_path):
        if not path.is_file():
            raise RuntimeError(f"contact_sheet_input_missing:{path}")
    summary = read_json(summary_path)
    decisions = read_jsonl(decisions_path)
    budgets = read_csv(budget_path)
    out_dir.mkdir(parents=True, exist_ok=False)
    asset_map, asset_audit = materialize_assets(decisions, out_dir)
    if asset_audit["asset_materialize_failure_count"]:
        raise RuntimeError("contact_sheet_asset_materialize_failed")
    page, render_audit = build_html(summary, decisions, budgets, asset_map)
    html_path = out_dir / "v23_video_frame_contact_sheet.html"
    html_path.write_text(page, encoding="utf-8")
    html_audit = validate_html_assets(page, out_dir)
    status = "PASS" if html_audit["html_asset_validation_status"] == "PASS" and render_audit["frame_count"] == render_audit["rendered_frame_count"] else "FAIL"
    result = {
        "technical_status": status,
        "policy_status": summary.get("policy_status", "REVIEW"),
        "commit_status": "DO_NOT_COMMIT" if summary.get("execution_mode") == "dry-run" else summary.get("commit_status"),
        "script_version": SCRIPT_VERSION,
        "html_path": str(html_path),
        "audit_path": str(out_dir / "contact_sheet_summary.json"),
        "derived_frame_access": "read_only_symlink_or_copy_fallback",
        "original_video_read": False,
        "sqlite_write": False,
        "audit": {**render_audit, **asset_audit, **html_audit},
    }
    Path(result["audit_path"]).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate standalone V23 video frame contact sheet")
    parser.add_argument("--v23-out", required=True)
    parser.add_argument("--out-dir")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    v23_out = Path(args.v23_out)
    out_dir = Path(args.out_dir) if args.out_dir else v23_out / "html"
    try:
        result = generate_contact_sheet(v23_out, out_dir)
    except Exception as exc:
        result = {
            "technical_status": "FAIL",
            "policy_status": "REVIEW",
            "commit_status": "DO_NOT_COMMIT",
            "script_version": SCRIPT_VERSION,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "sqlite_write": False,
            "original_video_read": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["technical_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
