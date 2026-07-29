#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FORBIDDEN_SUFFIXES = {
    ".db", ".gguf", ".log", ".onnx", ".pt", ".pth", ".safetensors",
    ".sqlite", ".sqlite3",
}
MEDIA_SUFFIXES = {".m4a", ".mov", ".mp3", ".mp4", ".wav"}
FORBIDDEN_NAMES = {
    ".env", "mobileclip2_b.ts", "pipeline.pid", "pipeline_state.json",
}
SENSITIVE_PATTERNS = {
    "private_user_path": re.compile(
        rb"/Users/(?!yourname(?:/|\b))[A-Za-z0-9._-]+/"
    ),
    "private_key": re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY"),
    "github_token": re.compile(rb"(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]{16,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}
BUILDER_HOME_PATTERN = re.compile(
    re.escape(str(Path.home()).encode("utf-8")) + rb"(?:/|\b)"
)
CHUNK_SIZE = 1024 * 1024
OVERLAP = 256


def _scan_file(path: Path, *, third_party_runtime: bool = False) -> list[str]:
    findings: list[str] = []
    previous = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                sample = previous + chunk
                patterns = (
                    {"private_builder_path": BUILDER_HOME_PATTERN}
                    if third_party_runtime
                    else SENSITIVE_PATTERNS
                )
                for label, pattern in patterns.items():
                    if label not in findings and pattern.search(sample):
                        findings.append(label)
                previous = sample[-OVERLAP:]
    except OSError as exc:
        findings.append(f"unreadable:{exc}")
    return findings


def _is_third_party_runtime(relative: Path) -> bool:
    parts = relative.parts
    return (
        parts[:2] == ("Contents", "Frameworks")
        or parts[:3] == ("Contents", "Resources", "PipelineEnvs")
    )


def _is_runtime_configuration_pth(path: Path, relative: Path) -> bool:
    if path.suffix.lower() != ".pth" or "site-packages" not in relative.parts:
        return False
    try:
        return path.stat().st_size <= 64 * 1024 and "\x00" not in path.read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return False


def audit_app_bundle(bundle: Path) -> list[str]:
    bundle = bundle.expanduser().resolve()
    failures: list[str] = []
    if bundle.suffix != ".app" or not bundle.is_dir():
        return [f"not_app_bundle:{bundle}"]

    resources = bundle / "Contents" / "Resources"
    required = [
        bundle / "Contents" / "Info.plist",
        resources / "app_config.json",
        resources / "Documentation" / "LICENSE-GPL-3.0.txt",
        resources / "Documentation" / "LICENSE_HISTORY.md",
        resources / "Documentation" / "NOTICE.txt",
        resources / "Documentation" / "MODEL_SOURCES.md",
        resources / "Documentation" / "MODEL_SETUP.md",
    ]
    for path in required:
        if not path.is_file():
            failures.append(f"missing_required:{path.relative_to(bundle)}")

    config_path = resources / "app_config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"invalid_app_config:{exc}")
        else:
            expected = {
                "configuration_state": "first_run_clean",
                "database": "",
                "output_root": "",
                "models_included": False,
                "author": "Horizon-94",
                "official_source": "https://github.com/Horizon-94/local-media-organizer",
                "license": "GPL-3.0-only",
            }
            for key, value in expected.items():
                if config.get(key) != value:
                    failures.append(f"app_config:{key}")
            project_root = str(config.get("project_root") or "")
            if config.get("portable_pipeline_runtimes") and project_root != "$APP_RESOURCES/Pipeline":
                failures.append("app_config:portable_project_root")

    portable_manifest = resources / "portable_runtime_manifest.json"
    if portable_manifest.is_file():
        try:
            manifest = json.loads(portable_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"invalid_portable_manifest:{exc}")
        else:
            if manifest.get("models_included") is not False:
                failures.append("portable_manifest:models_included")
            if manifest.get("network_download_implemented") is not False:
                failures.append("portable_manifest:network_download_implemented")

    for path in sorted(bundle.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(bundle)
        third_party_runtime = _is_third_party_runtime(relative)
        forbidden_suffix = path.suffix.lower() in FORBIDDEN_SUFFIXES
        if _is_runtime_configuration_pth(path, relative):
            forbidden_suffix = False
        if (
            path.name in FORBIDDEN_NAMES
            or forbidden_suffix
            or (path.suffix.lower() in MEDIA_SUFFIXES and not third_party_runtime)
        ):
            failures.append(f"forbidden_file:{relative}")
        for finding in _scan_file(path, third_party_runtime=third_party_runtime):
            failures.append(f"{finding}:{relative}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit a built macOS app for private data and release invariants"
    )
    parser.add_argument("app", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    failures = audit_app_bundle(args.app)
    payload = {
        "status": "PASS" if not failures else "FAIL",
        "app": str(args.app.expanduser().resolve()),
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload["status"])
        for failure in failures:
            print(failure)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
