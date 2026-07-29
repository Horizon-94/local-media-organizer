#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Finalize the frozen V25 DB contract and a three-image Qwen-VL smoke.

The workflow is checkpointed by stage. It never reads original video, never
downloads, and only executes Qwen from the committed central database view.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import stop03_2_v25_candidate_contract_lock as contract_lock
import stop03_3c_qwenvl_db_orchestrator_v1 as qwen


SCRIPT_VERSION = "finalize_stop03_3_v25_contract_and_qwenvl_smoke_v1_20260711"
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-3-v25-contract-qwenvl-smoke-finalizer"
QWEN_CONFIG = PROJECT_ROOT / "configs/stop03_3_qwenvl_db_v1.json"
REGISTRY = PROJECT_ROOT / "docs/model_registry/LOCAL_MODEL_REGISTRY.md"
RUNTIME_INVENTORY = PROJECT_ROOT / "docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md"
FINALIZER_TEST = PROJECT_ROOT / "tests/test_finalize_stop03_3_v25_contract_and_qwenvl_smoke.py"

STAGES = (
    "A_registry_and_path_check",
    "B_py_compile",
    "C_targeted_tests",
    "D_candidate_contract_preflight",
    "E_candidate_contract_dry_run",
    "F_database_backup",
    "G_candidate_contract_commit",
    "H_candidate_contract_readback",
    "I_candidate_contract_idempotency",
    "J_qwenvl_preflight",
    "K_qwenvl_smoke_3_at_384",
    "L_qwenvl_database_readback",
    "M_final_integrity_and_summary",
)

FROZEN_HASHES = {
    str(contract_lock.RULE_DOCUMENT): "8f5d0ba75bf148eb44e2dd507c7ebd5025a5a23cc4e2dc2bbdf7e846640d2f7a",
    str(contract_lock.POLICY_CONFIG): "19c9008ae64c18713f6c815118249e9f3b72b9ada5c301d259b7c98c344e70a5",
    str(contract_lock.CANDIDATE_SCRIPT): "b16f147fef7610223afcfe5518e16b25aa4d1697804df2a2dd542748331ca4a9",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(stable_json(value), encoding="utf-8")
    os.replace(temp, path)


def assert_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(TEST_OUTPUT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise RuntimeError(f"output_outside_test_output:{resolved}") from exc
    if resolved == TEST_OUTPUT_ROOT.resolve(strict=False):
        raise RuntimeError("output_must_not_equal_test_output_root")
    return resolved


def parse_json_stdout(stdout: str) -> Dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise RuntimeError("command_json_output_missing")
    return dict(json.loads(stdout[start:]))


class Finalizer:
    def __init__(
        self, *, mode: str, db: Path, out: Path, smoke_limit: int,
        max_repair_cycles: int, repair_cycles_used: int,
    ) -> None:
        self.mode = mode
        self.db = db.expanduser().resolve(strict=True)
        self.out = assert_output_path(out)
        self.smoke_limit = smoke_limit
        self.max_repair_cycles = max_repair_cycles
        self.state_path = self.out / "checkpoints/finalizer_state.json"
        if smoke_limit != 3:
            raise RuntimeError("smoke_limit_must_equal_3")
        if not 0 <= repair_cycles_used <= max_repair_cycles <= 3:
            raise RuntimeError("repair_cycle_bounds_invalid")
        if mode == "run":
            if self.out.exists() and any(self.out.iterdir()):
                raise RuntimeError(f"run_output_not_empty:{self.out}")
            for name in ("checkpoints", "logs", "reports", "work", "backups", "tmp", "pycache"):
                (self.out / name).mkdir(parents=True, exist_ok=True)
            self.state: Dict[str, Any] = {
                "script_version": SCRIPT_VERSION,
                "status": "RUNNING",
                "technical_status": "REVIEW",
                "policy_status": "REVIEW",
                "commit_status": "NOT_STARTED",
                "failure_stage": "",
                "failure_reason": "",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "db": str(self.db),
                "out": str(self.out),
                "smoke_limit": smoke_limit,
                "max_repair_cycles": max_repair_cycles,
                "repair_cycles_used": repair_cycles_used,
                "stages": {},
                "network_used": False,
                "download_used": False,
                "original_media_modified": False,
                "model_directory_written": False,
            }
            self.save_state()
        else:
            if not self.state_path.is_file():
                raise RuntimeError(f"resume_state_missing:{self.state_path}")
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state.get("db") != str(self.db) or self.state.get("out") != str(self.out):
                raise RuntimeError("resume_db_or_out_mismatch")
            if int(self.state.get("smoke_limit", 0)) != smoke_limit:
                raise RuntimeError("resume_smoke_limit_mismatch")
            self.state["status"] = "RUNNING"
            self.state["failure_stage"] = ""
            self.state["failure_reason"] = ""
            self.save_state()
        atomic_write_json(self.out / "checkpoints/finalizer_process.json", {
            "pid": os.getpid(), "mode": mode, "started_at": now_iso(), "status": "running"
        })

    def save_state(self) -> None:
        self.state["updated_at"] = now_iso()
        atomic_write_json(self.state_path, self.state)

    def append_log(self, stage: str, text: str) -> None:
        with (self.out / f"logs/{stage}.log").open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")

    def run_command(self, stage: str, command: Sequence[str]) -> Dict[str, Any]:
        env = dict(os.environ)
        env.update({
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
            "TMPDIR": str(self.out / "tmp"),
            "PYTHONPYCACHEPREFIX": str(self.out / "pycache"),
        })
        self.append_log(stage, "command=" + json.dumps(list(command), ensure_ascii=False))
        proc = subprocess.run(
            list(command), cwd=str(PROJECT_ROOT), env=env,
            capture_output=True, text=True, check=False,
        )
        self.append_log(stage, "stdout:\n" + (proc.stdout or ""))
        self.append_log(stage, "stderr:\n" + (proc.stderr or ""))
        if proc.returncode != 0:
            raise RuntimeError(f"command_failed_exit_{proc.returncode}:{command[0]}")
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def run_stage(self, name: str, function: Callable[[], Mapping[str, Any]]) -> None:
        previous = dict(self.state.get("stages", {}).get(name) or {})
        if self.mode == "resume" and previous.get("status") == "PASS":
            self.append_log(name, f"{now_iso()} resume_skip_previous_pass")
            return
        record = {"status": "RUNNING", "started_at": now_iso()}
        self.state["current_stage"] = name
        self.state.setdefault("stages", {})[name] = record
        self.save_state()
        self.append_log(name, f"{now_iso()} stage_start")
        try:
            report = dict(function())
            report.setdefault("status", "PASS")
            record.update({
                "status": "PASS", "finished_at": now_iso(),
                "report_path": str(self.out / f"reports/{name}.json"),
                "result_status": report.get("status"),
            })
            atomic_write_json(self.out / f"reports/{name}.json", report)
            self.append_log(name, f"{now_iso()} stage_pass result_status={report.get('status')}")
            self.save_state()
        except Exception as exc:
            failure = {
                "status": "FAIL", "stage": name, "error_type": type(exc).__name__,
                "error_message": str(exc), "traceback": traceback.format_exc(),
            }
            atomic_write_json(self.out / f"reports/{name}.json", failure)
            record.update({
                "status": "FAIL", "finished_at": now_iso(),
                "report_path": str(self.out / f"reports/{name}.json"),
                "error_message": str(exc),
            })
            self.state.update({
                "status": "FAIL", "technical_status": "FAIL", "policy_status": "FAIL",
                "failure_stage": name, "failure_reason": str(exc),
            })
            self.append_log(name, f"{now_iso()} stage_fail {type(exc).__name__}:{exc}")
            self.save_state()
            raise

    def stage_a(self) -> Mapping[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        required = [
            REGISTRY, RUNTIME_INVENTORY, contract_lock.MIGRATION,
            contract_lock.RULE_DOCUMENT, contract_lock.POLICY_CONFIG,
            contract_lock.CANDIDATE_SCRIPT, QWEN_CONFIG,
            Path(config["prompt_path"]), Path(config["model_path"]),
            Path(config["qwen_python"]), FINALIZER_TEST,
        ]
        missing = [str(path) for path in required if not path.exists()]
        frozen_actual = {
            path: contract_lock.sha256_file(Path(path)) for path in FROZEN_HASHES
        }
        checks = {
            "required_paths_exist": not missing,
            "frozen_hashes_match": frozen_actual == FROZEN_HASHES,
            "database_writable": os.access(self.db, os.R_OK | os.W_OK),
            "smoke_limit_3": self.smoke_limit == 3,
            "max_repair_cycles_at_most_3": self.max_repair_cycles <= 3,
            "offline_environment_forced": True,
        }
        if not all(checks.values()):
            raise RuntimeError("registry_or_path_check_failed:" + json.dumps({
                "missing": missing, "checks": checks, "frozen_actual": frozen_actual,
            }, sort_keys=True))
        return {
            "status": "PASS", "checks": checks, "missing_paths": missing,
            "frozen_hashes": frozen_actual, "network_used": False,
            "download_used": False, "original_video_read": False,
        }

    def stage_b(self) -> Mapping[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        files = [
            Path(contract_lock.__file__).resolve(),
            Path(qwen.__file__).resolve(), Path(__file__).resolve(),
            PROJECT_ROOT / "tests/test_stop03_2_v25_candidate_contract_lock.py",
            PROJECT_ROOT / "tests/test_stop03_3c_qwenvl_db_orchestrator_v1.py",
            FINALIZER_TEST,
        ]
        cmd = [str(config["qwen_python"]), "-m", "py_compile", *map(str, files)]
        result = self.run_command(STAGES[1], cmd)
        return {"status": "PASS", "compiled_file_count": len(files), "returncode": result["returncode"]}

    def stage_c(self) -> Mapping[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        tests = [
            "tests/test_stop03_2_v25_candidate_contract_lock.py",
            "tests/test_stop03_3c_qwenvl_db_orchestrator_v1.py",
            "tests/test_finalize_stop03_3_v25_contract_and_qwenvl_smoke.py",
        ]
        result = self.run_command(STAGES[2], [str(config["qwen_python"]), "-m", "unittest", *tests])
        combined = (result["stdout"] or "") + "\n" + (result["stderr"] or "")
        import re
        match = re.search(r"Ran\s+(\d+)\s+tests?", combined)
        return {
            "status": "PASS", "tests_passed": int(match.group(1)) if match else 0,
            "tests_failed": 0, "returncode": result["returncode"],
        }

    def stage_d(self) -> Mapping[str, Any]:
        report = contract_lock.preflight(self.db, self.out / "work/candidate_contract_dry_run")
        if report["technical_status"] != "PASS":
            raise RuntimeError("candidate_contract_preflight_failed")
        return report

    def stage_e(self) -> Mapping[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        target = self.out / "work/candidate_contract_dry_run"
        result = self.run_command(STAGES[4], [
            str(config["qwen_python"]), str(Path(contract_lock.__file__).resolve()),
            "--mode", "dry-run", "--db", str(self.db), "--out", str(target),
        ])
        report = parse_json_stdout(result["stdout"])
        if report.get("technical_status") != "PASS" or report.get("central_db_modified"):
            raise RuntimeError("candidate_contract_dry_run_failed")
        return report

    def stage_f(self) -> Mapping[str, Any]:
        backup = self.out / f"backups/media_archive_before_v25_contract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
        con = contract_lock.connect_readonly(self.db)
        try:
            ledger = contract_lock.candidate_ledger_audit(con)
        finally:
            con.close()
        contract_lock.backup_database(self.db, backup)
        backup_con = contract_lock.connect_readonly(backup)
        try:
            backup_integrity = str(backup_con.execute("PRAGMA integrity_check").fetchone()[0])
            backup_ledger = contract_lock.candidate_ledger_audit(backup_con)
        finally:
            backup_con.close()
        if backup_integrity != "ok" or backup_ledger != ledger:
            raise RuntimeError("database_backup_validation_failed")
        self.state["database_backup_path"] = str(backup)
        self.state["candidate_ledger_before"] = ledger
        self.save_state()
        return {
            "status": "PASS", "database_backup_path": str(backup),
            "backup_integrity_check": backup_integrity,
            "candidate_ledger_before": ledger,
        }

    def stage_g(self) -> Mapping[str, Any]:
        backup = Path(str(self.state.get("database_backup_path") or ""))
        if not backup.is_file():
            raise RuntimeError("validated_database_backup_missing")
        snapshot = contract_lock.build_snapshot(self.db)
        if snapshot["summary"]["technical_status"] != "PASS":
            raise RuntimeError("candidate_snapshot_build_failed")
        try:
            result = contract_lock.apply_snapshot_transaction(self.db, snapshot)
        except Exception:
            contract_lock.restore_database(backup, self.db)
            raise
        if result["status"] not in {"PASS", "IDEMPOTENT_PASS"}:
            raise RuntimeError("candidate_contract_commit_failed")
        if result["candidate_ledger_before"] != result["candidate_ledger_after"]:
            raise RuntimeError("candidate_ledger_changed_during_commit")
        self.state["commit_status"] = "COMMITTED"
        self.save_state()
        return {"status": "PASS", "commit_result": result, "snapshot_summary": snapshot["summary"]}

    def stage_h(self) -> Mapping[str, Any]:
        con = contract_lock.connect_readonly(self.db)
        try:
            report = contract_lock.readback(con)
        finally:
            con.close()
        if report["status"] != "PASS":
            raise RuntimeError("candidate_contract_readback_failed")
        return report

    def stage_i(self) -> Mapping[str, Any]:
        snapshot = contract_lock.build_snapshot(self.db)
        report = contract_lock.apply_snapshot_transaction(self.db, snapshot)
        if report["status"] != "IDEMPOTENT_PASS" or not report["idempotent"]:
            raise RuntimeError("candidate_contract_idempotency_failed")
        return report

    def qwen_preflight(self) -> Dict[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        model = Path(str(config["model_path"])).resolve(strict=True)
        python = Path(str(config["qwen_python"])).absolute()
        prompt = Path(str(config["prompt_path"])).resolve(strict=True)
        return qwen.preflight(
            db=self.db, out=self.out / "work/qwenvl_smoke", config_path=QWEN_CONFIG,
            model_path=model, qwen_python=python, prompt_path=prompt,
            max_tokens=384, mode="smoke", allow_low_token_debug=False,
            allow_simulation=False,
        )

    def stage_j(self) -> Mapping[str, Any]:
        report = self.qwen_preflight()
        if report["technical_status"] != "PASS" or report["queue_source"] != "central_db_view":
            raise RuntimeError("qwenvl_preflight_failed")
        return report

    def stage_k(self) -> Mapping[str, Any]:
        config = qwen.load_config(QWEN_CONFIG)
        pre = self.qwen_preflight()
        run_id = str(self.state.get("qwen_run_id") or "")
        if run_id:
            con = qwen.readonly_connection(self.db)
            try:
                rows = qwen.resume_filter([dict(row) for row in con.execute(
                    "SELECT * FROM stop03_3_qwenvl_run_items WHERE run_id=? ORDER BY candidate_id",
                    (run_id,),
                )])
            finally:
                con.close()
        else:
            con, source = qwen.queue_connection(self.db, allow_simulation=False)
            try:
                rows = qwen.load_queue(con)[:self.smoke_limit]
            finally:
                con.close()
            if source != "central_db_view" or len(rows) != self.smoke_limit:
                raise RuntimeError("smoke_requires_three_central_view_rows")
            run_id, rows = qwen.create_run_and_items(
                db=self.db, rows=rows, pre=pre,
                prompt_path=Path(str(config["prompt_path"])),
                max_tokens=384, workers=1,
            )
            self.state["qwen_run_id"] = run_id
            self.save_state()

        def progress(value: Mapping[str, Any]) -> None:
            self.state["qwen_progress"] = dict(value)
            self.save_state()
            self.append_log(STAGES[10], "progress=" + json.dumps(dict(value), ensure_ascii=False, sort_keys=True))

        work = self.out / "work/qwenvl_smoke"
        work.mkdir(parents=True, exist_ok=True)
        execution = qwen.execute_items(
            db=self.db, out=work, run_id=run_id, rows=rows, pre=pre,
            qwen_python=Path(str(config["qwen_python"])),
            model_path=Path(str(config["model_path"])),
            prompt=Path(str(config["prompt_path"])).read_text(encoding="utf-8").strip(),
            required_sections=config["required_output_sections"],
            max_tokens=384, timeout=int(config["default_timeout_seconds"]),
            workers=1, progress_callback=progress,
        )
        return {
            "status": "PASS", "run_id": run_id, "input_count": self.smoke_limit,
            "max_tokens": 384, "workers": 1,
            "output_contract_version": qwen.output_contract.CONTRACT_VERSION,
            "execution": execution, "network_used": False, "download_used": False,
        }

    def stage_l(self) -> Mapping[str, Any]:
        run_id = str(self.state.get("qwen_run_id") or "")
        report = qwen.readback_run(self.db, run_id, expected_count=self.smoke_limit)
        if report["status"] != "PASS":
            raise RuntimeError("qwenvl_database_readback_failed")
        return report

    def stage_m(self) -> Mapping[str, Any]:
        con = contract_lock.connect_readonly(self.db)
        try:
            ledger = contract_lock.candidate_ledger_audit(con)
            candidate_count = int(con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_items").fetchone()[0])
            snapshot_count = int(con.execute("SELECT COUNT(*) FROM stop03_2_candidate_queue_frozen_v25").fetchone()[0])
            qwen_count = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue").fetchone()[0])
            ocr_count = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue").fetchone()[0])
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = [list(row) for row in con.execute("PRAGMA foreign_key_check")]
            contract = contract_lock.readback(con)
        finally:
            con.close()
        qwen_rb = qwen.readback_run(self.db, str(self.state["qwen_run_id"]), expected_count=3)
        before = self.state.get("candidate_ledger_before")
        success = int(qwen_rb["status_counts"].get("success", 0))
        checks = {
            "candidate_ledger_390": candidate_count == 390,
            "candidate_snapshot_390": snapshot_count == 390,
            "qwen_view_336": qwen_count == 336,
            "ocr_view_54": ocr_count == 54,
            "candidate_ledger_unchanged": ledger == before,
            "candidate_id_digest_match": contract["candidate_id_set_sha256"] == contract_lock.EXPECTED_CANDIDATE_ID_SET_SHA256,
            "candidate_semantic_digest_match": contract["candidate_semantic_digest_sha256"] == contract_lock.EXPECTED_CANDIDATE_SEMANTIC_DIGEST_SHA256,
            "candidate_contract_readback_pass": contract["status"] == "PASS",
            "qwen_readback_pass": qwen_rb["status"] == "PASS",
            "qwen_smoke_all_three_success": success == 3,
            "integrity_check_ok": integrity == "ok",
            "foreign_key_check_empty": not foreign_keys,
            "network_false": not self.state.get("network_used"),
            "download_false": not self.state.get("download_used"),
            "original_media_safe": not self.state.get("original_media_modified"),
            "model_directory_safe": not self.state.get("model_directory_written"),
        }
        status = "PASS" if all(checks.values()) else "FAIL"
        report = {
            "status": status, "technical_status": status, "policy_status": status,
            "commit_status": "COMMITTED_AND_QWENVL_SMOKE_WRITTEN",
            "checks": checks, "candidate_ledger_count": candidate_count,
            "candidate_snapshot_count": snapshot_count, "qwen_view_count": qwen_count,
            "ocr_view_count": ocr_count, "candidate_ledger_after": ledger,
            "candidate_contract": contract, "qwen_readback": qwen_rb,
            "integrity_check": integrity, "foreign_key_check": foreign_keys,
            "database_backup_path": self.state.get("database_backup_path", ""),
            "network_used": False, "download_used": False,
            "original_media_modified": False, "model_directory_written": False,
        }
        atomic_write_json(self.out / "reports/final_summary.json", report)
        if status != "PASS":
            raise RuntimeError("final_acceptance_failed:" + json.dumps(
                [key for key, value in checks.items() if not value], ensure_ascii=False
            ))
        self.state.update({
            "status": "PASS", "technical_status": "PASS", "policy_status": "PASS",
            "commit_status": "COMMITTED_AND_QWENVL_SMOKE_WRITTEN",
            "failure_stage": "", "failure_reason": "", "final_summary": report,
        })
        self.save_state()
        return report

    def run(self) -> Dict[str, Any]:
        functions = (
            self.stage_a, self.stage_b, self.stage_c, self.stage_d, self.stage_e,
            self.stage_f, self.stage_g, self.stage_h, self.stage_i, self.stage_j,
            self.stage_k, self.stage_l, self.stage_m,
        )
        try:
            for name, function in zip(STAGES, functions):
                self.run_stage(name, function)
            process_status = "pass"
        except Exception:
            process_status = "fail"
            raise
        finally:
            atomic_write_json(self.out / "checkpoints/finalizer_process.json", {
                "pid": os.getpid(), "mode": self.mode, "finished_at": now_iso(),
                "status": process_status,
            })
        return self.state


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize V25 contract and three-image Qwen-VL smoke")
    parser.add_argument("--mode", required=True, choices=("run", "resume"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--smoke-limit", type=int, default=3)
    parser.add_argument("--max-repair-cycles", type=int, default=3)
    parser.add_argument("--repair-cycles-used", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    os.environ.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
    })
    try:
        finalizer = Finalizer(
            mode=args.mode, db=Path(args.db), out=Path(args.out),
            smoke_limit=args.smoke_limit, max_repair_cycles=args.max_repair_cycles,
            repair_cycles_used=args.repair_cycles_used,
        )
        state = finalizer.run()
        print(stable_json(state))
        return 0
    except Exception as exc:
        print(stable_json({
            "status": "FAIL", "technical_status": "FAIL", "policy_status": "FAIL",
            "error_type": type(exc).__name__, "error_message": str(exc),
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
