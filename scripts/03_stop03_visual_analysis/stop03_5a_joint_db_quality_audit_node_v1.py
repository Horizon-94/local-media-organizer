#!/usr/bin/env python3
"""Frozen Stop03-5A joint DB quality-audit node."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import stop03_5a_joint_db_quality_audit_v1 as auditor


NODE_VERSION = "stop03_5a_joint_db_quality_audit_node_v1_frozen_20260716"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_5a_joint_quality_audit_v1.json"
FROZEN_FILES = {
    PROJECT_ROOT
    / "scripts/03_stop03_visual_analysis/stop03_5a_joint_db_quality_audit_v1.py":
        "bc2630ffcc4e50ba1783de83f578c199e879e474e9600a69937f8030789d3f88",
    DEFAULT_CONFIG:
        "216a87ed698f5ec0e4bd8bd166c815e23b92ab4c6f2cec85dd24c682cfd0c9b7",
}
def find_arg(argv: Sequence[str], name: str, default: str) -> str:
    try:
        index = list(argv).index(name)
    except ValueError:
        return default
    if index + 1 >= len(argv):
        raise RuntimeError(f"frozen_node_argument_value_missing:{name}")
    return str(argv[index + 1])


def verify_node(
    db: Path,
    *,
    frozen_files: Optional[Mapping[Path, str]] = None,
) -> dict[str, Any]:
    files = dict(frozen_files or FROZEN_FILES)
    actual_hashes = {}
    for path, expected in files.items():
        if not path.is_file():
            raise RuntimeError(f"frozen_node_file_missing:{path}")
        actual = auditor.sha256_file(path)
        actual_hashes[str(path)] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen_node_file_hash_mismatch:{path}:{actual}:{expected}"
            )
    summary, details = auditor.run_audit(db, DEFAULT_CONFIG)
    expected_combined = (
        summary["qwen_queue_count"] + summary["ocr_queue_count"]
    )
    checks = {
        "technical_status_pass": summary["technical_status"] == "PASS",
        "staging_ready": summary["staging_readiness"]
        in {"READY", "READY_WITH_QUALITY_FLAGS"},
        "qwen_result_count_matches_queue":
            summary["qwen_selected_result_count"] == summary["qwen_queue_count"],
        "ocr_result_count_matches_queue":
            summary["ocr_selected_result_count"] == summary["ocr_queue_count"],
        "combined_count_matches_inputs":
            summary["combined_evidence_count"] == expected_combined,
        "combined_evidence_unique":
            summary["combined_unique_evidence_count"] == expected_combined,
        "combined_candidate_unique":
            summary["combined_unique_candidate_count"] == expected_combined,
        "combined_execution_key_unique":
            summary["combined_execution_key_count"] == expected_combined,
        "no_hard_fail_items": summary["hard_fail_item_count"] == 0,
        "no_cross_modal_lineage_failures":
            summary["cross_modal_visual_overlap_fail_count"] == 0,
        "database_integrity_ok":
            summary["database_integrity_check"] == "ok",
        "foreign_keys_ok": summary["foreign_key_error_count"] == 0,
        "review_rows_consistent":
            len(details["review"]) == summary["quality_review_item_count"],
    }
    mismatches = {name: False for name, passed in checks.items() if not passed}
    if mismatches:
        raise RuntimeError(
            "frozen_node_result_mismatch:"
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    return {
        "status": "PASS",
        "node_version": NODE_VERSION,
        "frozen_files": actual_hashes,
        "generic_invariant_checks": checks,
        "audit_summary": summary,
        "review_candidate_ids": [
            row["candidate_id"] for row in details["review"]
        ],
        "central_db_modified": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    verify_only = "--verify-node-only" in arguments
    if verify_only:
        arguments.remove("--verify-node-only")
    db = Path(find_arg(arguments, "--db", str(DEFAULT_DB))).expanduser().resolve(
        strict=True
    )
    config = Path(
        find_arg(arguments, "--config", str(DEFAULT_CONFIG))
    ).expanduser().resolve(strict=True)
    if config != DEFAULT_CONFIG:
        raise RuntimeError(f"frozen_node_config_mismatch:{config}:{DEFAULT_CONFIG}")
    report = verify_node(db)
    if verify_only:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    normalized = list(arguments)
    if "--db" not in normalized:
        normalized.extend(("--db", str(db)))
    if "--config" not in normalized:
        normalized.extend(("--config", str(DEFAULT_CONFIG)))
    if "--mode" not in normalized:
        normalized.extend(("--mode", "audit"))
    return auditor.main(normalized)


if __name__ == "__main__":
    raise SystemExit(main())
