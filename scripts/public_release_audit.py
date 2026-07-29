#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


FORBIDDEN_SUFFIXES = {
    ".db", ".dmg", ".gguf", ".onnx", ".pkg", ".pt", ".pth",
    ".safetensors", ".sqlite", ".sqlite3", ".zip",
}
FORBIDDEN_NAMES = {
    ".env", "pipeline.pid", "pipeline_state.json", "mobileclip2_b.ts",
}
FORBIDDEN_PARTS = {
    ".git", ".venv", "__pycache__", "build", "dist", "logs",
    "model", "models", "test-output",
}
TEXT_PATTERNS = {
    "private_key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "github_token": re.compile(r"\b(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]{16,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "local_username": re.compile(
        r"/Users/(?!yourname\b)[A-Za-z0-9._-]+(?:/|$)"
    ),
}
MAX_FILE_BYTES = 10 * 1024 * 1024


def audit(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            if path.is_file():
                failures.append(f"forbidden_path:{relative}")
            continue
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden_file:{relative}")
        if path.stat().st_size > MAX_FILE_BYTES:
            failures.append(f"oversize_file:{relative}:{path.stat().st_size}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in TEXT_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label}:{relative}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a source-only public release")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    failures = audit(root)
    if failures:
        print("FAIL")
        for failure in failures:
            print(failure)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
