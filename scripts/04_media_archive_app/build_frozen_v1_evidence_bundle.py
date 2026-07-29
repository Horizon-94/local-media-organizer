#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence


CONTRACT = "media_archive_image_video_frozen_evidence_bundle_v1"
FORBIDDEN_PARTS = {"test-output", "models", "model", "MEDIA_ARCHIVE_TEST_SOURCE", "__pycache__"}
FORBIDDEN_NAMES = {"media_archive.sqlite", "mobileclip2_b.ts"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_file_list(project_root: Path, list_path: Path, seen: Optional[set[Path]] = None) -> list[str]:
    seen = seen or set()
    list_path = list_path.resolve()
    if list_path in seen:
        raise RuntimeError(f"recursive bundle list include: {list_path}")
    seen.add(list_path)
    items: list[str] = []
    for raw in list_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@include "):
            include = project_root / line.removeprefix("@include ").strip()
            items.extend(load_file_list(project_root, include, seen))
            continue
        items.append(line)
    return items


def validate_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe evidence path: {value}")
    if FORBIDDEN_PARTS.intersection(path.parts) or path.name in FORBIDDEN_NAMES:
        raise ValueError(f"forbidden evidence content: {value}")
    return path


def build_bundle(project_root: Path, list_path: Path, output: Path) -> dict[str, object]:
    project_root = project_root.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace evidence bundle: {output}")
    raw_items = load_file_list(project_root, list_path)
    items = sorted(dict.fromkeys(raw_items))
    records = []
    for value in items:
        relative = validate_relative_path(value)
        source = (project_root / relative).resolve()
        if project_root not in source.parents or not source.is_file():
            raise FileNotFoundError(f"listed frozen file missing: {relative}")
        records.append({
            "path": relative.as_posix(),
            "size_bytes": source.stat().st_size,
            "sha256": sha256(source),
        })
    manifest = {
        "contract": CONTRACT,
        "release": "1.0.1-image-video",
        "file_count": len(records),
        "files": records,
        "excluded": ["models", "model weights", "central SQLite", "original media", "test-output"],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("FROZEN_EVIDENCE_MANIFEST.json", manifest_bytes)
        archive.writestr(
            "README.txt",
            "AI 本地素材库 V1 冻结证据包。\n"
            "本包用于版本复核和升级，不含模型、数据库、原始素材或测试输出。\n",
        )
        for record in records:
            archive.write(project_root / str(record["path"]), str(record["path"]))
    return {
        "status": "PASS",
        "contract": CONTRACT,
        "output": str(output),
        "file_count": len(records),
        "zip_size_bytes": output.stat().st_size,
        "zip_sha256": sha256(output),
        "models_included": False,
        "database_included": False,
        "original_media_included": False,
        "test_output_included": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Build the V1 frozen evidence ZIP")
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--file-list", type=Path, default=project_root / "docs/pipeline_rules/FROZEN_INTERFACE_BUNDLE_FILES_V3.txt")
    parser.add_argument("--output", type=Path, default=project_root / "dist/AI本地素材库-V1-冻结证据.zip")
    args = parser.parse_args(argv)
    print(json.dumps(build_bundle(args.project_root, args.file_list, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
