#!/usr/bin/env python3
"""Run the frozen V25 evidence loader with a generic density policy.

The frozen selector intentionally remains unchanged.  This desktop adapter only
adds task-scoped 15/20/30 percent video density selection and gate
applicability.  Existing V25 mode remains available for compatible resumes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ADAPTER_VERSION = "stop03_2_candidate_queues_generic_library_v3_hotfix_current_task_recovery"
GENERIC_DENSITY_POLICY_VERSION = "stop03_2_generic_density_policy_v1"
HIGH_VALUE_TARGETS = {
    "frozen_v25_compatible": None,
    "target_15": 0.15,
    "target_20": 0.20,
    "target_30": 0.30,
}
NO_VIDEO_REASON_CODE = "VIDEO_GATES_NOT_APPLICABLE_NO_VIDEO_INPUT"
NO_PAIR_REASON_CODE = "MULTI_EVIDENCE_GATE_NOT_APPLICABLE_NO_PAIR_OPPORTUNITY"
MULTI_EVIDENCE_GATE = "multi_evidence_pair_evaluation_executed"
VIDEO_ONLY_GATES = frozenset({
    "coverage_local_radius_applied",
    "coverage_executed",
    "local_candidate_evaluation_executed",
    "multi_evidence_pair_evaluation_executed",
})


def multi_evidence_pair_opportunity_count(
    video_budget: Sequence[Mapping[str, Any]] | None,
) -> int:
    """Count selected combinations that must execute a pair comparison."""
    return sum(
        max(0, int(row.get("coverage_selected_count") or 0) - 1)
        + max(0, int(row.get("ocr_count") or 0) - 1)
        + max(0, int(row.get("supplement_count") or 0))
        for row in (video_budget or [])
    )


def apply_gate_applicability(
    summary: dict[str, Any],
    density_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-evaluate technical status without claiming an N/A gate executed."""
    raw_gates = dict(summary.get("automatic_acceptance_gates") or {})
    if density_summary and density_summary.get("target_ratio") is not None:
        raw_gates["generic_density_count_matches"] = bool(
            density_summary.get("target_count_matches")
        )
    summary["automatic_acceptance_gates_raw"] = raw_gates
    applicability = {name: "APPLICABLE" for name in raw_gates}

    if int(summary.get("input_video_visual_units") or 0) == 0:
        for name in VIDEO_ONLY_GATES:
            if name in applicability:
                applicability[name] = "NOT_APPLICABLE_NO_VIDEO_INPUT"
        applicable_gates = {
            name: value for name, value in raw_gates.items()
            if name not in VIDEO_ONLY_GATES
        }
        technical = "PASS" if applicable_gates and all(applicable_gates.values()) else "FAIL"
        reason_codes = list(summary.get("policy_reason_codes") or [])
        if NO_VIDEO_REASON_CODE not in reason_codes:
            reason_codes.append(NO_VIDEO_REASON_CODE)
        summary["policy_reason_codes"] = reason_codes
        summary["video_gate_applicability"] = "NOT_APPLICABLE_NO_VIDEO_INPUT"
    else:
        applicable_gates = dict(raw_gates)
        if (
            MULTI_EVIDENCE_GATE in applicability
            and int(summary.get("multi_evidence_pair_opportunity_count") or 0) == 0
        ):
            applicability[MULTI_EVIDENCE_GATE] = (
                "NOT_APPLICABLE_NO_PAIR_OPPORTUNITY"
            )
            applicable_gates.pop(MULTI_EVIDENCE_GATE, None)
            reason_codes = list(summary.get("policy_reason_codes") or [])
            if NO_PAIR_REASON_CODE not in reason_codes:
                reason_codes.append(NO_PAIR_REASON_CODE)
            summary["policy_reason_codes"] = reason_codes
        technical = (
            "PASS"
            if applicable_gates and all(applicable_gates.values())
            else "FAIL"
        )
        summary["video_gate_applicability"] = "APPLICABLE"

    summary["automatic_acceptance_gate_applicability"] = applicability
    summary["automatic_acceptance_applicable_gates"] = applicable_gates
    summary["generic_library_adapter_version"] = ADAPTER_VERSION
    if density_summary:
        summary["generic_density_policy"] = dict(density_summary)
        summary["policy_version"] = str(
            density_summary.get("policy_version") or summary.get("policy_version")
        )
    summary["validation_status"] = technical
    summary["technical_status"] = technical
    if summary.get("execution_mode") == "dry-run":
        summary["dry_run_status"] = technical
    return summary


def write_failure_diagnostics(
    frozen: Any,
    out: Path,
    result: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> None:
    """Persist read-only gate evidence without changing the frozen selector."""
    reports = out / "reports"
    failed_coverage = [
        dict(row) for row in result.get("coverage_reports") or []
        if str(row.get("refill_status") or "").startswith("failed_")
    ]
    frozen.write_json(
        reports / "coverage_summary.json",
        {
            "technical_status": summary.get("technical_status"),
            "normal_video_group_count": summary.get("normal_video_group_count"),
            "normal_video_group_with_coverage_count": summary.get(
                "normal_video_group_with_coverage_count"
            ),
            "normal_video_group_missing_coverage_count": summary.get(
                "normal_video_group_missing_coverage_count"
            ),
            "coverage_anchor_total_count": summary.get("coverage_anchor_total_count"),
            "coverage_missing_count": summary.get("coverage_missing_count"),
            "coverage_refill_failed_count": summary.get("coverage_refill_failed_count"),
        },
    )
    frozen.write_csv(reports / "coverage_missing.csv", failed_coverage)
    frozen.write_csv(reports / "coverage_refill_failed.csv", failed_coverage)
    frozen.write_csv(
        reports / "coverage_skipped_no_visual.csv",
        list(result.get("coverage_skipped_no_visual") or []),
        fields=("source_content_id", "reason"),
    )
    frozen.write_json(
        reports / "technical_gates.json",
        {
            "gates": summary.get("automatic_acceptance_gates") or {},
            "applicability": summary.get(
                "automatic_acceptance_gate_applicability"
            ) or {},
        },
    )
    frozen.write_json(reports / "candidate_dry_run_summary.json", dict(summary))


def apply_non_black_coverage_fallbacks(
    frozen: Any,
    result: dict[str, Any],
    *,
    config: Mapping[str, Any],
    run_id: str,
    mode: str,
    hashes: Mapping[str, str],
    lineage: Mapping[str, str],
) -> dict[str, Any]:
    """Repair failed local anchors with an auditable same-source fallback.

    The frozen V25 selector remains byte-for-byte unchanged.  This adapter is
    used only after its normal local refill has exhausted all candidates.
    """
    failed = [
        row for row in result.get("coverage_reports") or []
        if str(row.get("refill_status") or "").startswith("failed_")
    ]
    if not failed:
        return result
    frames_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for frame in result.get("rows") or []:
        if frame.get("media_type") == "video":
            frames_by_source[str(frame.get("source_content_id") or "")].append(frame)
    candidates_by_visual = {
        str(row.get("visual_unit_id") or ""): row
        for row in result.get("q_rows") or []
        if row.get("queue_type") == "qwenvl_high_value"
    }
    decisions = {
        str(row.get("visual_unit_id") or ""): row
        for row in result.get("decisions") or []
    }
    repaired_by_source: dict[str, int] = defaultdict(int)
    for report in failed:
        source_id = str(report.get("source_content_id") or "")
        anchor_time = int(report.get("anchor_time_ms") or -1)
        tail_start = report.get("tail_start_ms")
        pool = [
            frame for frame in frames_by_source.get(source_id, [])
            if frame.get("signature_status") == "PASS"
            and not frame.get("black_rejected")
            and frame.get("identity_status") in {"unique", "canonical", "blocked_decoder"}
        ]
        if not pool:
            continue
        selected = min(
            pool,
            key=lambda frame: (
                int(tail_start is not None and int(frame.get("time_position_ms") or -1) >= int(tail_start)),
                -int(bool(frame.get("generic_high_signal"))),
                -float(frame.get("generic_high_signal_score") or 0.0),
                -float(frame.get("grid_structure") or 0.0),
                abs(int(frame.get("time_position_ms") or -1) - anchor_time),
                int(frame.get("time_position_ms") or -1),
                str(frame.get("visual_unit_id") or ""),
            ),
        )
        visual_id = str(selected["visual_unit_id"])
        reasons = [
            "coverage_fallback_after_local_refill_exhausted",
            "same_source_valid_non_black_frame",
            "1.1.4_hotfix_current_task_recovery",
        ]
        candidate = candidates_by_visual.get(visual_id)
        if candidate is None:
            candidate = frozen.make_candidate(
                selected,
                queue_type="qwenvl_high_value",
                role="COVERAGE_FALLBACK",
                score=float(selected.get("generic_high_signal_score") or 0.0),
                reasons=reasons,
                run_id=run_id,
                mode=mode,
                hashes=hashes,
                lineage=lineage,
                config=config,
                anchor={
                    "anchor_index": report.get("anchor_index"),
                    "anchor_visual_unit_id": report.get("anchor_visual_unit_id"),
                },
            )
            candidate["script_version"] = ADAPTER_VERSION
            result["q_rows"].append(candidate)
            candidates_by_visual[visual_id] = candidate
        else:
            candidate["reason_codes"] = "|".join(dict.fromkeys([
                *str(candidate.get("reason_codes") or "").split("|"),
                *reasons,
                "coverage_fallback_reused_existing_candidate",
            ]))
        decision = decisions.get(visual_id)
        if decision is not None:
            anchors = list(decision.get("coverage_anchor_indices") or [])
            anchor_index = int(report.get("anchor_index") or 0)
            if anchor_index not in anchors:
                anchors.append(anchor_index)
            decision["coverage_anchor_indices"] = anchors
            decision["coverage_anchor_index"] = anchors[0]
            decision["qwen_selected"] = True
            if not decision.get("qwen_role"):
                decision["qwen_role"] = "COVERAGE_FALLBACK"
            decision["selection_reason_codes"] = list(dict.fromkeys([
                *(decision.get("selection_reason_codes") or []), *reasons,
            ]))
        report.update({
            "selected_visual_unit_id": visual_id,
            "selected_time_ms": int(selected.get("time_position_ms") or -1),
            "selected_role": "COVERAGE_FALLBACK",
            "refill_status": "source_non_black_coverage_fallback",
            "fallback_scope": "same_source_outside_local_interval_allowed",
        })
        repaired_by_source[source_id] += 1
        stats = result["stats"]
        stats["coverage_refill_failed_count"] -= 1
        stats["coverage_missing_count"] -= 1
        stats["coverage_refill_count"] += 1
        stats["source_non_black_coverage_fallback_count"] += 1

    for source_id, repaired in repaired_by_source.items():
        still_failed = any(
            str(row.get("source_content_id") or "") == source_id
            and str(row.get("refill_status") or "").startswith("failed_")
            for row in result.get("coverage_reports") or []
        )
        if not still_failed:
            result["stats"]["normal_video_group_missing_coverage_count"] -= 1
            result["stats"]["normal_video_group_with_coverage_count"] += 1
        for budget in result.get("video_budget") or []:
            if str(budget.get("source_content_id") or "") != source_id:
                continue
            budget["coverage_selected_count"] = min(
                int(budget.get("coverage_anchor_count") or 0),
                int(budget.get("coverage_selected_count") or 0) + repaired,
            )
            source_qwen = [
                row for row in result["q_rows"]
                if row.get("queue_type") == "qwenvl_high_value"
                and row.get("media_type") == "video"
                and str(row.get("source_content_id") or "") == source_id
            ]
            budget["qwen_video_count"] = len(source_qwen)
            budget["coverage_unique_representative_count"] = len({
                str(row.get("visual_unit_id") or "") for row in source_qwen
                if "coverage" in str(row.get("candidate_role") or "").lower()
            })
    result["q_rows"].sort(key=lambda row: (
        str(row.get("media_type") or ""), str(row.get("source_relative_path") or ""),
        int(row.get("time_position_ms") or -1), str(row.get("visual_unit_id") or ""),
        str(row.get("candidate_role") or ""),
    ))
    return result


def density_target_count(input_count: int, ratio: float) -> int:
    """Use a per-video ceiling so every non-empty video keeps representation."""
    if input_count <= 0:
        return 0
    return min(input_count, max(1, int(math.ceil(input_count * ratio))))


def select_temporal_density_frames(
    frames: Sequence[Mapping[str, Any]], ratio: float
) -> list[Mapping[str, Any]]:
    """Select one deterministic semantic best frame from each temporal bin."""
    ordered = sorted(
        frames,
        key=lambda row: (
            int(row.get("time_position_ms") or -1),
            int(row.get("frame_index") or -1),
            str(row.get("visual_unit_id") or ""),
        ),
    )
    target = density_target_count(len(ordered), ratio)
    selected: list[Mapping[str, Any]] = []
    for index in range(target):
        start = index * len(ordered) // target
        end = (index + 1) * len(ordered) // target
        bucket = ordered[start:end]
        if not bucket:
            continue
        selected.append(
            min(
                bucket,
                key=lambda row: (
                    -int(bool(row.get("generic_high_signal"))),
                    -float(row.get("generic_high_signal_score") or 0.0),
                    -float(row.get("grid_structure") or 0.0),
                    int(row.get("time_position_ms") or -1),
                    str(row.get("visual_unit_id") or ""),
                ),
            )
        )
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("source_content_id") or ""),
            int(row.get("time_position_ms") or -1),
            str(row.get("visual_unit_id") or ""),
        ),
    )


def apply_generic_density(
    frozen: Any,
    result: dict[str, Any],
    *,
    ratio: float,
    run_id: str,
    mode: str,
    hashes: Mapping[str, str],
    lineage: Mapping[str, str],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace video Qwen candidates with a deterministic target-density set."""
    by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for frame in result["rows"]:
        if frame.get("media_type") != "video":
            continue
        if frozen.is_screen_recording(str(frame.get("source_relative_path") or ""), config):
            continue
        if frame.get("signature_status") != "PASS" or frame.get("black_rejected"):
            continue
        if frame.get("identity_status") not in {"unique", "canonical", "blocked_decoder"}:
            continue
        by_source[str(frame["source_content_id"])].append(frame)

    image_rows = [row for row in result["q_rows"] if row.get("media_type") != "video"]
    new_video_rows: list[dict[str, Any]] = []
    source_counts: list[dict[str, Any]] = []
    decisions = {
        str(row["visual_unit_id"]): row
        for row in result.get("decisions") or []
    }
    for decision in decisions.values():
        decision["qwen_selected"] = False
        decision["qwen_role"] = ""
        decision["coverage_anchor_index"] = -1
        decision["coverage_anchor_indices"] = []
        decision["selection_reason_codes"] = [
            reason
            for reason in decision.get("selection_reason_codes") or []
            if not str(reason).startswith(("coverage_", "video_high_signal_", "high_signal_"))
        ]

    adapter_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    percent = int(round(ratio * 100))
    for source_id, frames in sorted(by_source.items()):
        selected = select_temporal_density_frames(frames, ratio)
        source_counts.append(
            {
                "source_content_id": source_id,
                "eligible_frame_count": len(frames),
                "selected_frame_count": len(selected),
                "target_ratio": ratio,
                "rounding": "ceil_per_video",
            }
        )
        for frame in selected:
            high = bool(frame.get("generic_high_signal"))
            role = (
                "video_coverage_high_signal_overlap"
                if high else "video_coverage_keyframe"
            )
            reasons = [
                f"generic_target_density_{percent}_percent",
                "temporal_bin_deterministic_best",
                *frozen.evidence_reason_codes(frame),
            ]
            candidate = frozen.make_candidate(
                frame,
                queue_type="qwenvl_high_value",
                role=role,
                score=float(frame.get("generic_high_signal_score") or 0.0),
                reasons=reasons,
                run_id=run_id,
                mode=mode,
                hashes=hashes,
                lineage=lineage,
                config=config,
            )
            candidate.update(
                {
                    "candidate_id": frozen.stable_id(
                        "cand_generic_",
                        GENERIC_DENSITY_POLICY_VERSION,
                        f"target_{percent}",
                        frame["visual_unit_id"],
                    ),
                    "policy_version": GENERIC_DENSITY_POLICY_VERSION,
                    "script_version": ADAPTER_VERSION,
                    "script_sha256": adapter_sha,
                }
            )
            new_video_rows.append(candidate)
            decision = decisions.get(str(frame["visual_unit_id"]))
            if decision is not None:
                decision.update(
                    {
                        "qwen_selected": True,
                        "qwen_role": role,
                        "selection_reason_codes": reasons,
                    }
                )

    result["q_rows"] = sorted(
        [*image_rows, *new_video_rows],
        key=lambda row: (
            str(row.get("media_type") or ""),
            str(row.get("source_relative_path") or ""),
            int(row.get("time_position_ms") or -1),
            str(row.get("visual_unit_id") or ""),
        ),
    )
    selected_count = len(new_video_rows)
    eligible_count = sum(len(rows) for rows in by_source.values())
    expected_count = sum(
        density_target_count(len(frames), ratio)
        for frames in by_source.values()
    )
    result["generic_density_summary"] = {
        "policy_version": GENERIC_DENSITY_POLICY_VERSION,
        "mode": f"target_{percent}",
        "target_ratio": ratio,
        "eligible_video_frame_count": eligible_count,
        "expected_selected_video_frame_count": expected_count,
        "selected_video_frame_count": selected_count,
        "target_count_matches": selected_count == expected_count,
        "actual_ratio": (selected_count / eligible_count) if eligible_count else 0.0,
        "rounding": "ceil_per_video",
        "source_count": len(by_source),
        "source_counts": source_counts,
        "fixed_output_count": False,
    }
    result["stats"]["generic_density_eligible_video_frame_count"] = eligible_count
    result["stats"]["generic_density_selected_video_frame_count"] = selected_count
    return result


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_frozen_v25(project_root: Path) -> Any:
    script = (
        project_root
        / "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py"
    )
    if not script.is_file():
        raise RuntimeError(f"frozen_v25_script_missing:{script}")
    module_name = "stop03_2_candidate_queues_from_db_safe_v25_0_20260711"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen_v25_import_spec_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def configure_frozen_v25_paths(
    frozen: Any, project_root: Path, output_root: Path,
) -> None:
    """Bind import-time frozen paths to the selected packaged pipeline.

    Release builds replace developer paths with portable placeholders.  The
    frozen module derives several constants from ``PROJECT_ROOT`` while it is
    imported, so changing only ``TEST_OUTPUT_ROOT`` leaves ``RULE_DOCUMENT``
    pointing at the literal placeholder.  Rebind the complete derived set
    before preflight or selection without changing the frozen policy.
    """
    frozen.PROJECT_ROOT = project_root
    frozen.TEST_OUTPUT_ROOT = output_root
    frozen.DEFAULT_DB = project_root / "media_archive.sqlite"
    frozen.DEFAULT_CONFIG = (
        project_root / "configs" / "stop03_2_high_value_policy_v25.json"
    )
    frozen.RULE_DOCUMENT = (
        project_root
        / "docs"
        / "pipeline_rules"
        / "STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md"
    )
    frozen.DEFAULT_OUT = output_root / "stop03_2_v25_dry_run"


def next_candidate_attempt_root(stage_root: Path) -> Path:
    """Return a fresh child output while preserving earlier failure evidence."""
    attempts = stage_root / "candidate_attempts"
    for number in range(1, 10_000):
        candidate = attempts / f"attempt_{number:04d}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"candidate_attempt_space_exhausted:{attempts}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic-library adapter for frozen Stop03-2 V25")
    parser.add_argument("--mode", choices=["dry-run", "commit"], default="dry-run")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--allowed-output-root", required=True)
    parser.add_argument("--video-regression-baseline")
    parser.add_argument(
        "--high-value-mode",
        choices=tuple(HIGH_VALUE_TARGETS),
        default="frozen_v25_compatible",
    )
    parser.add_argument("--clear-existing-candidate-items", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    allowed = Path(args.allowed_output_root).expanduser().resolve(strict=True)
    db = Path(args.db).expanduser().resolve(strict=True)
    stage_out = Path(args.out).expanduser().resolve(strict=False)
    out = next_candidate_attempt_root(stage_out)
    config = Path(args.config).expanduser().resolve(strict=True)
    if not within(db, allowed):
        raise RuntimeError(f"database_outside_selected_workspace:{db}")
    if stage_out == allowed or not within(stage_out, allowed):
        raise RuntimeError(f"output_outside_selected_workspace:{stage_out}")

    frozen = load_frozen_v25(project_root)
    configure_frozen_v25_paths(frozen, project_root, allowed)
    original_build_summary: Callable[..., dict[str, Any]] = frozen.build_summary
    original_select_candidates: Callable[..., dict[str, Any]] = frozen.select_candidates
    original_attach_signatures: Callable[..., dict[str, Any]] = frozen.attach_signatures
    original_fingerprint_one: Callable[..., dict[str, Any]] = frozen.fingerprint_one
    progress_lock = threading.Lock()
    progress = {"completed": 0, "total": 0, "workers": 1}

    def emit_progress(current_item: str = "") -> None:
        print(json.dumps({
            "contract": "media_archive_stage_runtime_contract_v1",
            "event": "stage_progress",
            "completed": progress["completed"],
            "total": progress["total"],
            "success": progress["completed"],
            "skipped": 0,
            "failed": 0,
            "current_item": current_item,
            "actual_workers": progress["workers"],
        }, ensure_ascii=False), flush=True)

    def fingerprint_with_progress(
        row: Mapping[str, Any], loaded_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = original_fingerprint_one(row, loaded_config)
        with progress_lock:
            progress["completed"] += 1
            completed = progress["completed"]
            total = progress["total"]
            if completed == total or completed % 25 == 0:
                emit_progress(str(row.get("source_relative_path") or row.get("visual_unit_id") or ""))
        return result

    def attach_signatures_with_progress(
        rows: list[dict[str, Any]], loaded_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        with progress_lock:
            progress["completed"] = 0
            progress["total"] = len(rows)
            progress["workers"] = max(1, int(loaded_config.get("signature_workers") or 1))
            emit_progress("正在校验候选画面")
        return original_attach_signatures(rows, loaded_config)

    frozen.fingerprint_one = fingerprint_with_progress
    frozen.attach_signatures = attach_signatures_with_progress

    def select_candidates_with_density(
        canonical_rows: list[dict[str, Any]],
        raw_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
        loaded_config: Mapping[str, Any],
        run_id: str,
        mode: str,
        hashes: Mapping[str, str],
        lineage: Mapping[str, str],
        image_inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = original_select_candidates(
            canonical_rows,
            raw_by_source,
            loaded_config,
            run_id,
            mode,
            hashes,
            lineage,
            image_inputs,
        )
        ratio = HIGH_VALUE_TARGETS[args.high_value_mode]
        if ratio is None:
            result = apply_non_black_coverage_fallbacks(
                frozen,
                result,
                config=loaded_config,
                run_id=run_id,
                mode=mode,
                hashes=hashes,
                lineage=lineage,
            )
            result["generic_density_summary"] = {
                "policy_version": frozen.POLICY_VERSION,
                "mode": "frozen_v25_compatible",
                "target_ratio": None,
                "fixed_output_count": False,
            }
            return result
        return apply_generic_density(
            frozen,
            result,
            ratio=ratio,
            run_id=run_id,
            mode=mode,
            hashes=hashes,
            lineage=lineage,
            config=loaded_config,
        )

    def build_summary_with_applicability(*values: Any, **named: Any) -> dict[str, Any]:
        summary = original_build_summary(*values, **named)
        result = values[0] if values else named.get("result")
        density = (
            result.get("generic_density_summary")
            if isinstance(result, Mapping) else None
        )
        video_budget = (
            result.get("video_budget")
            if isinstance(result, Mapping) else None
        )
        summary["multi_evidence_pair_opportunity_count"] = (
            multi_evidence_pair_opportunity_count(video_budget)
        )
        summary = apply_gate_applicability(summary, density)
        if summary.get("technical_status") != "PASS":
            write_failure_diagnostics(frozen, out, result or {}, summary)
        return summary

    frozen.select_candidates = select_candidates_with_density
    frozen.build_summary = build_summary_with_applicability
    frozen_args = [
        "--mode", args.mode,
        "--db", str(db),
        "--out", str(out),
        "--config", str(config),
    ]
    if args.preflight_only:
        frozen_args.append("--preflight-only")
    if args.video_regression_baseline:
        frozen_args.extend(["--video-regression-baseline", args.video_regression_baseline])
    if args.clear_existing_candidate_items:
        frozen_args.append("--clear-existing-candidate-items")
    return int(frozen.main(frozen_args))


if __name__ == "__main__":
    raise SystemExit(main())
