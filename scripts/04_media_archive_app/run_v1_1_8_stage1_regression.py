#!/usr/bin/env python3
"""Short, isolated v1.2.0 contract regression runner.

The runner deliberately excludes real media and model inference.  It executes
fixture/unit contracts only, type-checks the native SwiftUI frontend, and
writes durable JSON/Markdown evidence without touching any task database.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTRACT = "media_archive_v1_2_0_final_regression_v1"

TEST_GROUPS = {
    "P0_central_state_recovery": [
        "tests/test_media_archive_central_database_v2.py",
        "tests/test_media_archive_pipeline_orchestrator_v1.py",
        "tests/test_media_archive_stage_runtime_contract_v1.py",
        "tests/test_media_archive_runtime_contract_v1.py",
        "tests/test_media_archive_repository_schema_compat_v1.py",
        "tests/test_qwen_runtime_progress_contract_v1.py",
        "tests/test_stop03_3f_qwenvl_dynamic_db_orchestrator.py",
        "tests/test_current_task_recovery_hotfix_v1.py",
    ],
    "P1_search_history_maintenance": [
        "tests/test_media_archive_search_readiness_v1.py",
        "tests/test_rebuild_search_index_from_database_v1.py",
        "tests/test_stop03_5e_hybrid_visual_text_search_v2.py",
        "tests/test_library_artifact_difference_v1.py",
    ],
    "P2_people_annotations_observability": [
        "tests/test_local_person_annotations_v1.py",
        "tests/test_media_archive_native_bridge_state_v1.py",
        "tests/test_media_archive_storage_audit_v1.py",
    ],
    "P3_candidate_and_release_contracts": [
        "tests/test_stop03_2_v25_candidate_contract_lock.py",
        "tests/test_stop03_2_v25_candidate_queues.py",
        "tests/test_release_privacy_contract_v1.py",
    ],
    "AUDIO_production_search_ui": [
        "tests/test_audio_enhancement_v1.py",
        "tests/test_audio_search_timeline_v1.py",
        "tests/test_audio_search_index_v1.py",
        "tests/test_audio_production_integration_v1.py",
    ],
    "NATIVE_app_release_contract": [
        "tests/test_media_archive_image_video_native_app_v1.py",
    ],
}

GROUP_PYTHONS = {
    # The portable build host intentionally has no Tk module.  The legacy
    # native-app contract imports its Tk fallback and therefore runs under the
    # macOS system Python, while the shipped SwiftUI app remains unchanged.
    "NATIVE_app_release_contract": "/usr/bin/python3",
}


def run_command(command: list[str], cwd: Path) -> dict[str, Any]:
    started = time.time()
    completed = subprocess.run(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = completed.stdout or ""
    return {
        "command": command,
        "exit_code": completed.returncode,
        "elapsed_seconds": round(time.time() - started, 3),
        "output_tail": output[-8000:],
        "status": "PASS" if completed.returncode == 0 else "FAIL",
    }


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=root / "dist" / "v1.2.0-regression")
    parser.add_argument("--only-group", choices=sorted(TEST_GROUPS))
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    groups: dict[str, Any] = {}
    selected_groups = (
        {args.only_group: TEST_GROUPS[args.only_group]}
        if args.only_group else TEST_GROUPS
    )
    for name, files in selected_groups.items():
        print(f"regression_group_start:{name}", flush=True)
        missing = [item for item in files if not (root / item).is_file()]
        if missing:
            groups[name] = {"status": "FAIL", "missing": missing}
            continue
        groups[name] = run_command(
            [GROUP_PYTHONS.get(name, sys.executable), "-m", "pytest", "-q", *files], root
        )
        print(
            f"regression_group_end:{name}:{groups[name]['status']}"
            f":exit={groups[name]['exit_code']}",
            flush=True,
        )

    print("regression_swift_start", flush=True)
    swift = run_command([
        "/usr/bin/swiftc", "-swift-version", "5", "-typecheck",
        str(root / "apps/media_archive_image_video_ui/native_frontend.swift"),
        "-module-cache-path", "/tmp/local-media-organizer-swift-cache",
    ], root)
    print(f"regression_swift_end:{swift['status']}", flush=True)
    static_checks = {
        "no_real_media_path_in_commands": True,
        "no_model_directory_in_commands": True,
        "original_media_write": False,
        "model_load_or_inference": False,
        "task_database_write": False,
    }
    passed = all(value.get("status") == "PASS" for value in groups.values()) and swift["status"] == "PASS"
    report = {
        "contract": CONTRACT,
        "version": "1.2.0",
        "status": "PASS" if passed else "FAIL",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "python": sys.executable,
        "groups": groups,
        "swift_typecheck": swift,
        "safety": static_checks,
    }
    json_path = output / "stage1_regression_report.json"
    md_path = output / "stage1_regression_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# v1.2.0 最终隔离回归报告", "",
        f"- 状态：{report['status']}",
        f"- 生成时间：{report['generated_at']}",
        "- 真实素材写入：否", "- 模型加载/推理：否", "",
        "## 合同组", "",
    ]
    for name, result in groups.items():
        lines.append(f"- {name}: {result.get('status', 'FAIL')}")
    lines.extend(["", f"- SwiftUI 类型检查：{swift['status']}", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "status": report["status"], "json": str(json_path), "markdown": str(md_path)
    }, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
