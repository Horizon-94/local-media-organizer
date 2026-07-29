#!/usr/bin/env python3
"""Run an existing frozen stage inside one explicitly selected library root.

The historical stage files intentionally only accepted the project and
``test-output`` roots.  The desktop application lets the user select a
different index folder.  This adapter changes only the in-process safety root;
it does not edit the frozen stage implementation or weaken source protection.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--allowed-source-root", type=Path)
    parser.add_argument("stage_args", nargs=argparse.REMAINDER)
    return parser


def _patch_module(value: Any, output_root: Path, source_root: Path | None) -> None:
    if not isinstance(value, ModuleType):
        return
    if hasattr(value, "TEST_OUTPUT_ROOT"):
        setattr(value, "TEST_OUTPUT_ROOT", output_root)
    if source_root is not None and hasattr(value, "ALLOWED_SOURCE_ROOTS"):
        setattr(value, "ALLOWED_SOURCE_ROOTS", {source_root})


def execute(
    script: Path,
    output_root: Path,
    source_root: Path | None,
    stage_args: Sequence[str],
) -> int:
    script = script.expanduser().resolve(strict=True)
    output_root = output_root.expanduser().resolve()
    source_root = source_root.expanduser().resolve(strict=True) if source_root else None
    if not output_root.is_dir():
        raise RuntimeError(f"allowed_output_root_missing:{output_root}")
    sys.path.insert(0, str(script.parent))
    # Use the real file stem as the module name.  macOS multiprocessing uses
    # ``spawn`` and must be able to import worker functions by module name in
    # each child process.  A synthetic runpy name works for serial stages but
    # makes ProcessPoolExecutor functions unpicklable.
    spec = importlib.util.spec_from_file_location(script.stem, script)
    if not spec or not spec.loader:
        raise RuntimeError(f"stage_import_spec_failed:{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[script.stem] = module
    spec.loader.exec_module(module)
    namespace = vars(module)
    namespace["TEST_OUTPUT_ROOT"] = output_root
    if source_root is not None:
        namespace["ALLOWED_SOURCE_ROOTS"] = {source_root}
    for value in namespace.values():
        _patch_module(value, output_root, source_root)
    main = namespace.get("main")
    if not callable(main):
        raise RuntimeError(f"stage_main_missing:{script}")
    args = list(stage_args)
    if args[:1] == ["--"]:
        args = args[1:]
    signature = inspect.signature(main)
    if len(signature.parameters) == 0:
        previous = sys.argv
        try:
            sys.argv = [str(script), *args]
            result = main()
        finally:
            sys.argv = previous
    else:
        result = main(args)
    return int(result or 0)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return execute(
        args.script,
        args.allowed_output_root,
        args.allowed_source_root,
        args.stage_args,
    )


if __name__ == "__main__":
    raise SystemExit(main())
