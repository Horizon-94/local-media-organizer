#!/usr/bin/env python3
"""Checkpointed host-side Pipeline V2 calibration campaign.

This coordinator never guesses a workload command.  Qwen uses the verified
1.2.8 host entry; stages without a formal fixture/entry are recorded as
NOT_CONFIGURED rather than silently replaced by a different test.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TEST_ROOT = Path("/Users/horizon/Documents/AI-Local/test-output")
FIXTURE_SOURCE = TEST_ROOT / "qwen-host-fixture-v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def campaign_plan() -> list[dict[str, object]]:
    # Qwen is the only heavy formal host command currently proven in this
    # repository.  Other entries remain explicit campaign rows so failures or
    # missing formal contracts are visible and resumable.
    rows: list[dict[str, object]] = []
    for mode, workers in (("cold", 1), ("resident", 1), ("homogeneous", 2), ("homogeneous", 3)):
        rows.append({"id": f"qwen-{mode}-x{workers}", "kind": "qwen", "mode": mode, "workers": workers})
    for name in ("whisper-cold-x1", "whisper-resident-x1", "yoloe-x1", "openclip-x1", "ocr-x1", "embedding-x1", "ffmpeg-stage03-x1", "person-reid-x1"):
        rows.append({"id": name, "kind": name.split("-")[0], "mode": "baseline", "workers": 1})
    for name in ("whisper-x2", "yoloe-x2", "yoloe-x3", "openclip-x2", "openclip-x3", "ocr-x2", "embedding-x2", "ffmpeg-stage03-x2", "ffmpeg-stage03-x3", "ffmpeg-stage03-x4", "ffmpeg-stage03-x5"):
        rows.append({"id": name, "kind": name.split("-")[0], "mode": "homogeneous", "workers": int(name.rsplit("x", 1)[1])})
    for name in ("ffmpeg+yoloe", "ffmpeg+openclip", "ffmpeg+ocr", "ffmpeg+qwen", "yoloe+openclip", "yoloe+ocr", "yoloe+qwen", "openclip+qwen", "ocr+qwen", "embedding+qwen", "qwen+whisper", "whisper+yoloe", "whisper+openclip", "qwen+ffmpeg+yoloe", "qwen+ffmpeg+ocr", "qwen+openclip+ocr", "whisper+ffmpeg+yoloe", "qwen+whisper+ffmpeg", "event-driven-mixed-soak", "hdd-validation"):
        rows.append({"id": name, "kind": "combination", "mode": "representative", "workers": 1})
    return rows


def fresh_qwen_fixture(run_root: Path) -> tuple[Path, Path]:
    fixture = run_root / "fixture" / "qwen_fixture.sqlite"
    config = run_root / "fixture" / "qwen-config.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE_SOURCE / "qwen_fixture.sqlite", fixture)
    shutil.copy2(FIXTURE_SOURCE / "qwen-config.json", config)
    return fixture, config


def run_qwen(row: dict[str, object], run_root: Path) -> dict[str, object]:
    fixture, config = fresh_qwen_fixture(run_root)
    output = run_root / "output"
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"RUN_HEAVY": "1", "QWEN_DB": str(fixture), "QWEN_CONFIG": str(config), "QWEN_WORKERS": str(row["workers"])})
    command = [str(REPO / "tools/pipeline_benchmark/run_host_benchmark.sh"), str(output)]
    started = time.time()
    completed = subprocess.run(command, cwd=REPO, env=env, check=False)
    return {"status": "PASS" if completed.returncode == 0 else "FAIL", "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "output": str(output), "fixture": str(fixture), "workers": row["workers"], "note": "resident mode uses one formal worker process for the candidate batch; explicit multi-batch idle harness is not yet part of 1.2.8."}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=TEST_ROOT / f"pipeline-v2-calibration-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "calibration-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"campaign": "pipeline-v2-resource-calibration-v1", "started_at": utc_now(), "results": {}}
    results: dict[str, object] = state.setdefault("results", {})
    for row in campaign_plan():
        item_id = str(row["id"])
        previous = results.get(item_id)
        if isinstance(previous, dict) and previous.get("status") in {"PASS", "FAIL", "UNSAFE", "BORDERLINE", "NOT_CONFIGURED", "SKIPPED_DUE_TO_UNSAFE_PARENT"}:
            continue
        item_root = root / "runs" / item_id
        print(f"[{len(results)+1}] {item_id}", flush=True)
        if row["kind"] == "qwen":
            result = run_qwen(row, item_root)
        else:
            result = {"status": "NOT_CONFIGURED", "reason_code": "formal_host_entry_or_fixture_not_configured", "kind": row["kind"], "workers": row["workers"]}
        result.update({"id": item_id, "kind": row["kind"], "mode": row["mode"], "finished_at": utc_now()})
        results[item_id] = result
        write_json(state_path, state)
    summary = {"campaign": state["campaign"], "finished_at": utc_now(), "tested": len(results), "passed": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "PASS"), "failed": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "FAIL"), "not_configured": sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "NOT_CONFIGURED"), "results": results}
    write_json(root / "calibration-results.json", summary)
    lines = ["# Pipeline V2 calibration campaign", "", f"- campaign: `{summary['campaign']}`", f"- tested rows: `{summary['tested']}`", f"- pass: `{summary['passed']}`", f"- fail: `{summary['failed']}`", f"- not configured: `{summary['not_configured']}`", "", "Rows marked NOT_CONFIGURED are not successful measurements; they identify formal entry/fixture work still required."]
    (root / "calibration-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "calibration-summary.md")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
