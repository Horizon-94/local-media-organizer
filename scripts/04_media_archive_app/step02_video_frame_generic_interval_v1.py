#!/usr/bin/env python3
"""Generic 1-5 second adapter for the frozen Step02 video-frame extractor.

The proven extractor remains unchanged.  This adapter selects a task-scoped
sampling contract before delegating to it, so frame ids, manifests, resume
state and database rows all record the interval actually selected by the user.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Sequence


ADAPTER_VERSION = "step02_video_frame_generic_interval_v1"
SUPPORTED_INTERVAL_SECONDS = (1, 2, 3, 4, 5)


def apply_runtime_safety_roots(module: Any) -> None:
    """Propagate roots injected by the generic stage wrapper to the late import."""
    output_root = globals().get("TEST_OUTPUT_ROOT")
    if output_root is not None:
        module.TEST_OUTPUT_ROOT = Path(output_root).expanduser().resolve()
    source_roots = globals().get("ALLOWED_SOURCE_ROOTS")
    if source_roots is not None and hasattr(module, "ALLOWED_SOURCE_ROOTS"):
        module.ALLOWED_SOURCE_ROOTS = {
            Path(root).expanduser().resolve() for root in source_roots
        }


def load_frozen_extractor(project_root: Path) -> Any:
    path = (
        project_root
        / "scripts/02_step01_step02_pipeline/"
        "step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py"
    )
    spec = importlib.util.spec_from_file_location("step02_frozen_v7_generic_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"frozen_step02_import_failed:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def configure_sampling_contract(module: Any, interval_seconds: int) -> dict[str, Any]:
    if interval_seconds not in SUPPORTED_INTERVAL_SECONDS:
        raise ValueError("frame_interval_seconds_must_be_one_of_1_2_3_4_5")
    interval_ms = interval_seconds * 1000
    module.SAMPLING_INTERVAL_MS = interval_ms
    module.SCRIPT_SCHEME = (
        "step02_video_frame_generic_interval_"
        f"{interval_ms}_offset{module.SAMPLING_OFFSET_MS}_jpg1280_v1"
    )
    module.SAMPLING_CONTRACT = module.sampling_contract()
    module.SAMPLING_CONTRACT["generic_interval_adapter_version"] = ADAPTER_VERSION
    module.SAMPLING_CONTRACT_ID = hashlib.sha256(
        json.dumps(
            module.SAMPLING_CONTRACT,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return dict(module.SAMPLING_CONTRACT)


def parse_adapter_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--frame-interval-seconds",
        type=int,
        choices=SUPPORTED_INTERVAL_SECONDS,
        required=True,
    )
    return parser.parse_known_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args, forwarded = parse_adapter_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    frozen = load_frozen_extractor(project_root)
    apply_runtime_safety_roots(frozen)
    contract = configure_sampling_contract(frozen, args.frame_interval_seconds)
    print(
        json.dumps(
            {
                "adapter_version": ADAPTER_VERSION,
                "frame_interval_seconds": args.frame_interval_seconds,
                "sampling_contract_id": frozen.SAMPLING_CONTRACT_ID,
                "sampling_contract": contract,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    previous = sys.argv
    try:
        sys.argv = [str(Path(frozen.__file__).resolve()), *forwarded]
        result = frozen.main()
        return int(result or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
