#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs/model_sources_v1.json"
DEFAULT_MODEL_ROOT = Path.home() / "Library/Application Support/素材大整理/Models"


def prepare(model_root: Path, *, create: bool = True) -> list[dict[str, object]]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    root = model_root.expanduser().absolute()
    rows: list[dict[str, object]] = []
    for item in payload["models"]:
        relative = Path(item["relative_path"])
        destination = root / relative
        directory = destination if destination.suffix == "" else destination.parent
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        rows.append({
            "key": item["key"],
            "required": bool(item["required"]),
            "destination": str(destination),
            "upstream": item["upstream"],
            "exists": destination.exists(),
            "automatic_download": False,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the canonical model folders and print upstream links; never download models."
    )
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    rows = prepare(args.model_root, create=not args.check_only)
    print(json.dumps({
        "status": "PASS",
        "model_root": str(args.model_root.expanduser().absolute()),
        "automatic_download": False,
        "models": rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
