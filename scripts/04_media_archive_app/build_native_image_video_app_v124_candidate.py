#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional, Sequence


APP_RELEASE_NAME = "本地数据库 1.2.4 候选版"
APP_BUNDLE_NAME = f"{APP_RELEASE_NAME}.app"
APP_BUNDLE_VERSION = "1.2.4-candidate"
APP_SEMVER = "1.2.4"
APP_BUNDLE_IDENTIFIER = "local.horizon.local-database.candidate"
APP_BUILD_NUMBER = "124"
RELEASE_BUNDLE_ALLOWLIST = Path("configs/media_archive_release_bundle_allowlist_v1.json")

PIPELINE_ENV_ROOT = Path(
    os.environ.get("MEDIA_ARCHIVE_ENV_ROOT")
    or Path(__file__).resolve().parents[3] / "envs"
).expanduser().resolve()
BUILD_MODEL_ROOT = Path(
    os.environ.get("MEDIA_ARCHIVE_MODEL_ROOT")
    or Path.home() / "Documents/model"
).expanduser().resolve()
BUILD_PADDLEX_ROOT = Path.home() / ".paddlex/official_models"
PIPELINE_ENVIRONMENTS = {
    "visual": PIPELINE_ENV_ROOT / "media-archive-v06-visual/bin/python",
    "yolo": PIPELINE_ENV_ROOT / "media-archive-v06-yolo/bin/python",
    "qwen": PIPELINE_ENV_ROOT / "qwen-vl/bin/python",
    "ocr": PIPELINE_ENV_ROOT / "media-archive-v06-ocr/bin/python",
    "embedding": PIPELINE_ENV_ROOT / "media-archive-embedding/bin/python",
    "whisper": PIPELINE_ENV_ROOT / "whisper/bin/python",
}
PIPELINE_ROLE_ENVIRONMENT = {
    "system": "visual", "visual": "visual",
    "yolo": "yolo", "person_reid": "yolo",
    "qwen": "qwen", "ocr": "ocr", "embedding": "embedding", "whisper": "whisper",
}
DEVELOPER_PRIVATE_PATH_MARKERS = (str(Path.home()),)
TEXT_BUNDLE_SUFFIXES = {
    ".json", ".md", ".py", ".sh", ".sql", ".txt", ".yaml", ".yml",
}


def build_environment() -> dict[str, str]:
    """Use the standalone Command Line Tools even when a new Xcode install
    has not completed its first-launch licence step.

    The app does not depend on the full Xcode IDE.  Keeping this explicit also
    makes local rebuilds deterministic across machines with or without Xcode.
    """
    environment = os.environ.copy()
    command_line_tools = Path("/Library/Developer/CommandLineTools")
    if command_line_tools.is_dir():
        environment["DEVELOPER_DIR"] = str(command_line_tools)
    return environment


def swift_toolchain() -> tuple[str, str, dict[str, str]]:
    """Resolve compiler and SDK from one developer directory.

    Mixing /usr/bin/swiftc with an SDK selected by another Xcode/CLT release
    caused reproducible module-cache and SDK-version failures in 1.2.0 release
    rehearsals.  xcrun keeps both sides on the same selected toolchain.
    """
    environment = build_environment()
    swiftc = subprocess.run(
        ["/usr/bin/xcrun", "--find", "swiftc"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    ).stdout.strip()
    sdk = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    ).stdout.strip()
    if not Path(swiftc).is_file() or not Path(sdk).is_dir():
        raise RuntimeError(f"incomplete Swift toolchain: swiftc={swiftc!r} sdk={sdk!r}")
    return swiftc, sdk, environment


NATIVE_LAUNCHER_SOURCE = r"""
#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    char executable_path[PATH_MAX];
    uint32_t executable_size = sizeof(executable_path);
    if (_NSGetExecutablePath(executable_path, &executable_size) != 0) return 70;

    char resolved_path[PATH_MAX];
    if (!realpath(executable_path, resolved_path)) return 71;
    char macos_path[PATH_MAX];
    strncpy(macos_path, resolved_path, sizeof(macos_path) - 1);
    macos_path[sizeof(macos_path) - 1] = '\0';

    char contents_candidate[PATH_MAX];
    snprintf(contents_candidate, sizeof(contents_candidate), "%s/..", dirname(macos_path));
    char contents_path[PATH_MAX];
    if (!realpath(contents_candidate, contents_path)) return 72;

    char python_home[PATH_MAX];
    char launcher_path[PATH_MAX];
    snprintf(
        python_home, sizeof(python_home),
        "%s/Frameworks/__PYTHON_FRAMEWORK__/Versions/Current", contents_path
    );
    snprintf(launcher_path, sizeof(launcher_path), "%s/Resources/python_bridge.py", contents_path);
    setenv("PYTHONHOME", python_home, 1);
    /* A signed app bundle must remain immutable after launch.  Python's default
       import cache would otherwise add __pycache__ files inside Contents and
       invalidate the seal after the first snapshot/search command. */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("TK_SILENCE_DEPRECATION", "1", 1);

    int python_argc = argc + 1;
    wchar_t **python_argv = calloc((size_t)python_argc, sizeof(wchar_t *));
    if (!python_argv) return 73;
    python_argv[0] = Py_DecodeLocale(argv[0], NULL);
    python_argv[1] = Py_DecodeLocale(launcher_path, NULL);
    for (int index = 1; index < argc; ++index) {
        python_argv[index + 1] = Py_DecodeLocale(argv[index], NULL);
    }
    int result = Py_Main(python_argc, python_argv);
    for (int index = 0; index < python_argc; ++index) PyMem_RawFree(python_argv[index]);
    free(python_argv);
    return result;
}
"""


class PythonFrameworkRuntime(NamedTuple):
    source: Path
    version: str
    binary_name: str
    headers: Path


def locate_python_framework(python_executable: Path) -> PythonFrameworkRuntime:
    """Locate the base framework behind a venv, not the venv directory.

    Command Line Tools Python uses ``Python3.framework`` while Homebrew 3.12
    uses ``Python.framework`` and does not ship a ``Versions/Current`` link in
    its opt view.  Both are valid framework layouts.
    """
    code = (
        "import json,sys,sysconfig;"
        "print(json.dumps({'base_prefix':sys.base_prefix,"
        "'framework':sysconfig.get_config_var('PYTHONFRAMEWORK') or '',"
        "'framework_prefix':sysconfig.get_config_var('PYTHONFRAMEWORKPREFIX') or ''}))"
    )
    metadata = json.loads(subprocess.run(
        [str(python_executable), "-c", code],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=build_environment(),
    ).stdout)
    version_root = Path(metadata["base_prefix"]).resolve()
    framework = next(
        (
            candidate
            for candidate in (version_root, *version_root.parents)
            if candidate.name in {"Python.framework", "Python3.framework"}
            and (candidate / "Versions").is_dir()
        ),
        None,
    )
    if framework is None:
        prefix = Path(str(metadata.get("framework_prefix") or "")).expanduser()
        candidate = prefix / f"{str(metadata.get('framework') or 'Python')}.framework"
        if candidate.name in {"Python.framework", "Python3.framework"} and (candidate / "Versions").is_dir():
            framework = candidate.resolve()
    if framework is None:
        executable_framework = next(
            (
                candidate
                for candidate in (python_executable.resolve(), *python_executable.resolve().parents)
                if candidate.name in {"Python.framework", "Python3.framework"}
                and (candidate / "Versions").is_dir()
            ),
            None,
        )
        framework = executable_framework
    if framework is None:
        raise RuntimeError(
            "unsupported Python framework runtime:"
            f"base_prefix={metadata['base_prefix']};framework_prefix={metadata.get('framework_prefix', '')}"
        )
    binary_name = str(metadata.get("framework") or framework.stem)
    binary = version_root / binary_name
    if not binary.is_file():
        binary_name = next(
            (name for name in ("Python", "Python3") if (version_root / name).is_file()),
            "",
        )
    header_candidates = [
        version_root / "Headers",
        *(sorted((version_root / "include").glob("python*")) if (version_root / "include").is_dir() else []),
    ]
    headers = next((path for path in header_candidates if (path / "Python.h").is_file()), None)
    if not binary_name or headers is None:
        raise RuntimeError(f"incomplete Python framework runtime: {framework}")
    return PythonFrameworkRuntime(framework, version_root.name, binary_name, headers)


def copy_app_python_framework(runtime: PythonFrameworkRuntime, frameworks: Path) -> Path:
    destination = frameworks / runtime.source.name
    shutil.copytree(
        runtime.source, destination, symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"),
    )
    current = destination / "Versions/Current"
    if not current.exists():
        current.symlink_to(runtime.version)
    for link_name, target in (
        (runtime.binary_name, f"Versions/Current/{runtime.binary_name}"),
        ("Resources", "Versions/Current/Resources"),
        ("Headers", "Versions/Current/Headers"),
    ):
        link = destination / link_name
        if not link.exists() and (destination / target).exists():
            link.symlink_to(target)
    return destination


def compile_native_launcher(
    executable: Path, framework: Path, runtime: PythonFrameworkRuntime,
    *, apple_silicon_only: bool = False,
) -> None:
    headers = runtime.headers
    framework_binary = framework / f"Versions/Current/{runtime.binary_name}"
    architectures = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(framework_binary)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.split()
    requested = ("arm64",) if apple_silicon_only else ("arm64", "x86_64")
    supported = [arch for arch in requested if arch in architectures]
    if not supported:
        raise RuntimeError(f"Python framework has no supported architecture: {architectures}")
    with tempfile.TemporaryDirectory(prefix="media-archive-launcher-") as temporary:
        source = Path(temporary) / "launcher.c"
        source.write_text(
            NATIVE_LAUNCHER_SOURCE.replace("__PYTHON_FRAMEWORK__", framework.name),
            encoding="utf-8",
        )
        subprocess.run(
            [
                "/usr/bin/clang", "-std=c11",
                *(item for arch in supported for item in ("-arch", arch)),
                "-I", str(headers), str(source), "-F", str(framework.parent),
                "-framework", runtime.binary_name,
                "-Wl,-rpath,@executable_path/../Frameworks",
                "-o", str(executable),
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=build_environment(),
        )
    linked = subprocess.run(
        ["/usr/bin/otool", "-L", str(executable)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout
    dependency = next(
        (
            line.strip().split(" (", 1)[0]
            for line in linked.splitlines()[1:]
            if f"/{framework.name}/" in line
        ),
        "",
    )
    desired = f"@rpath/{framework.name}/Versions/Current/{runtime.binary_name}"
    if dependency and dependency != desired:
        subprocess.run(
            ["/usr/bin/install_name_tool", "-change", dependency, desired, str(executable)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    executable.chmod(0o755)


def compile_swift_frontend(
    source: Path, executable: Path, *, apple_silicon_only: bool = False,
) -> None:
    swiftc, sdk, environment = swift_toolchain()
    with tempfile.TemporaryDirectory(prefix="media-archive-swiftui-") as temporary:
        temporary_root = Path(temporary)
        architecture_outputs = []
        for architecture in (("arm64",) if apple_silicon_only else ("arm64", "x86_64")):
            architecture_output = temporary_root / f"frontend-{architecture}"
            completed = subprocess.run(
                [
                    # This is a UI shell, not a numeric workload.  The Swift
                    # 6.3 optimiser bundled with the standalone CLT can exit
                    # nondeterministically on this large SwiftUI view while
                    # the unoptimised build is stable on both architectures.
                    swiftc, "-swift-version", "5", "-Onone",
                    "-target", f"{architecture}-apple-macosx12.0",
                    "-sdk", sdk,
                    "-framework", "SwiftUI", "-framework", "AppKit",
                    "-framework", "AVFoundation", "-framework", "AVKit",
                    "-framework", "PDFKit",
                    "-module-cache-path", str(temporary_root / f"module-cache-{architecture}"),
                    str(source), "-o", str(architecture_output),
                ],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=environment,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no compiler output"
                raise RuntimeError(f"Swift {architecture} compile failed: {detail}")
            architecture_outputs.append(architecture_output)
        if len(architecture_outputs) == 1:
            shutil.copy2(architecture_outputs[0], executable)
        else:
            subprocess.run(
                ["/usr/bin/lipo", "-create", *(str(path) for path in architecture_outputs), "-output", str(executable)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=build_environment(),
            )
    executable.chmod(0o755)


def compile_avfoundation_audio_salvage(source: Path, executable: Path) -> None:
    """Build the Apple-native fallback used only for malformed AAC sources."""
    swiftc, sdk, environment = swift_toolchain()
    completed = subprocess.run(
        [
            swiftc, "-swift-version", "5", "-O",
            "-target", "arm64-apple-macosx12.0",
            "-sdk", sdk,
            "-framework", "Foundation", "-framework", "AVFoundation",
            "-module-cache-path", str(Path(tempfile.gettempdir()) / "media-archive-audio-module-cache"),
            str(source), "-o", str(executable),
        ],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no compiler output"
        raise RuntimeError(f"AVFoundation audio helper compile failed: {detail}")
    executable.chmod(0o755)


def build_app_icon(source: Path, resources: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"application icon source not found: {source}")
    from PIL import Image

    master = resources / "app_icon_1024.png"
    with Image.open(source) as input_image:
        image = input_image.convert("RGBA").resize((1024, 1024), Image.Resampling.LANCZOS)
        image.save(master, format="PNG")
        image.save(
            resources / "AppIcon.icns", format="ICNS",
            sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)],
        )


def _runtime_metadata(python_executable: Path) -> dict[str, str]:
    code = (
        "import json,sys,sysconfig;"
        "print(json.dumps({'major':str(sys.version_info.major),"
        "'minor':str(sys.version_info.minor),'base_prefix':sys.base_prefix,"
        "'purelib':sysconfig.get_paths()['purelib']}))"
    )
    return json.loads(subprocess.run(
        [str(python_executable), "-c", code],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=build_environment(),
    ).stdout)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_bundle_sources(project_root: Path) -> tuple[dict[str, object], list[Path]]:
    manifest_path = project_root / RELEASE_BUNDLE_ALLOWLIST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract") != "media_archive_release_bundle_allowlist_v1":
        raise RuntimeError("release_bundle_allowlist_contract_mismatch")
    paths: set[Path] = set()
    for relative in manifest.get("files") or []:
        candidate = Path(str(relative))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(f"unsafe_release_bundle_path:{candidate}")
        source = project_root / candidate
        if not source.is_file():
            raise FileNotFoundError(f"release bundle file missing: {candidate}")
        paths.add(source)
    for relative in manifest.get("trees") or []:
        candidate = Path(str(relative))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(f"unsafe_release_bundle_tree:{candidate}")
        source = project_root / candidate
        if not source.is_dir():
            raise FileNotFoundError(f"release bundle tree missing: {candidate}")
        paths.update(
            path for path in source.rglob("*")
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
                and path.name != ".DS_Store"
            )
        )
    return manifest, sorted(paths, key=lambda item: str(item.relative_to(project_root)))


def _copy_release_bundle_sources(project_root: Path, pipeline_root: Path) -> dict[str, object]:
    manifest, sources = _release_bundle_sources(project_root)
    for source in sources:
        relative = source.relative_to(project_root)
        destination = pipeline_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    return {
        "contract": manifest["contract"],
        "file_count": len(sources),
        "total_bytes": sum(path.stat().st_size for path in sources),
        "allowlist_sha256": _sha256(project_root / RELEASE_BUNDLE_ALLOWLIST),
    }


def _source_identity(project_root: Path) -> dict[str, object]:
    """Return a reproducible identity for the code copied into the app.

    Git commit alone is insufficient during local release work because some
    release sources may still be untracked.  The content digest therefore
    covers the complete native app package plus the runtime contract and this
    builder, without including models, media, workspaces, or user paths.
    """
    _, files = _release_bundle_sources(project_root)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: str(item.relative_to(project_root))):
        relative = str(path.relative_to(project_root)).replace(os.sep, "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    commit = ""
    dirty = True
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=project_root, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        ).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {
        "contract": "media_archive_release_source_identity_v1",
        "git_commit": commit,
        "git_dirty": dirty,
        "content_sha256": digest.hexdigest(),
        "file_count": len(files),
    }


def _copy_pipeline_python_framework(
    metadata: dict[str, str], frameworks: Path,
) -> tuple[Path, str]:
    major_minor = f"{metadata['major']}.{metadata['minor']}"
    source = Path(metadata["base_prefix"]).resolve().parent.parent
    if source.name not in {"Python.framework", "Python3.framework"}:
        raise RuntimeError(f"unsupported pipeline Python framework: {source}")
    # Keep the framework's original basename and bundle layout.  Renaming a
    # ``Python.framework`` directory to ``PipelinePython312.framework`` makes
    # codesign reject it because CFBundleExecutable no longer matches the
    # framework bundle name.  A versioned parent safely allows 3.9 and 3.12 to
    # coexist while each nested framework remains a valid Apple bundle.
    destination = (
        frameworks
        / f"PipelinePython{metadata['major']}{metadata['minor']}"
        / source.name
    )
    if not destination.exists():
        shutil.copytree(
            source, destination, symlinks=True,
            # Homebrew's base framework contains a site-packages symlink back
            # into the Cellar.  Dependencies are bundled separately per role,
            # so copying this external link would break strict code signing
            # and blank-machine portability.
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"),
        )
        version_root = destination / f"Versions/{major_minor}"
        current = destination / "Versions/Current"
        if not current.exists():
            current.symlink_to(major_minor)
        binary_name = next(
            (
                name for name in ("Python", "Python3")
                if (version_root / name).is_file()
            ),
            "",
        )
        if not binary_name:
            raise RuntimeError(f"pipeline Python framework binary missing: {version_root}")
        for link_name, target in (
            (binary_name, f"Versions/Current/{binary_name}"),
            ("Resources", "Versions/Current/Resources"),
            ("Headers", "Versions/Current/Headers"),
        ):
            link = destination / link_name
            if not link.exists():
                link.symlink_to(target)
        executable = destination / f"Versions/{major_minor}/bin/python{major_minor}"
        if executable.is_file():
            linked = subprocess.run(
                ["/usr/bin/otool", "-L", str(executable)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ).stdout
            absolute_library = next(
                (
                    line.strip().split(" (", 1)[0]
                    for line in linked.splitlines()[1:]
                    if "/Python.framework/" in line or "/Python3.framework/" in line
                ),
                "",
            )
            if absolute_library.startswith("/"):
                subprocess.run(
                    [
                        "/usr/bin/install_name_tool", "-change", absolute_library,
                        "@executable_path/../Python", str(executable),
                    ],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
    return destination, major_minor


def _write_pipeline_wrapper(
    path: Path, framework_path: str, major_minor: str, environment_name: str,
) -> None:
    # Use a tiny native launcher instead of an executable shell script.
    # macOS stores signatures for signed scripts in extended attributes, which
    # can be lost while creating or copying a DMG.  A Mach-O launcher carries
    # its signature in the binary and remains valid after installation.
    source = f"""
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int parent_in_place(char *value) {{
    char *slash = strrchr(value, '/');
    if (slash == NULL) return -1;
    *slash = '\\0';
    return 0;
}}

int main(int argc, char **argv) {{
    char executable[PATH_MAX];
    char resolved[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0 ||
        realpath(executable, resolved) == NULL) {{
        perror("resolve pipeline launcher");
        return 126;
    }}
    if (parent_in_place(resolved) != 0 ||
        parent_in_place(resolved) != 0 ||
        parent_in_place(resolved) != 0) {{
        fprintf(stderr, "invalid pipeline launcher path\\n");
        return 126;
    }}

    char python_home[PATH_MAX];
    char site_packages[PATH_MAX];
    char python_executable[PATH_MAX];
    if (snprintf(
            python_home, sizeof(python_home), "%s/Frameworks/%s/Versions/Current",
            resolved, {json.dumps(framework_path)}
        ) >= (int)sizeof(python_home) ||
        snprintf(
            site_packages, sizeof(site_packages),
            "%s/Resources/PipelineEnvs/%s/site-packages",
            resolved, {json.dumps(environment_name)}
        ) >= (int)sizeof(site_packages) ||
        snprintf(
            python_executable, sizeof(python_executable), "%s/bin/python%s",
            python_home, {json.dumps(major_minor)}
        ) >= (int)sizeof(python_executable)) {{
        fprintf(stderr, "pipeline launcher path too long\\n");
        return 126;
    }}

    unsetenv("PYTHONEXECUTABLE");
    unsetenv("__PYVENV_LAUNCHER__");
    unsetenv("VIRTUAL_ENV");
    unsetenv("PYTHONUSERBASE");
    setenv("PYTHONHOME", python_home, 1);
    setenv("PYTHONPATH", site_packages, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("MEDIA_ARCHIVE_PORTABLE_RUNTIME", "1", 1);

    char **child_argv = calloc((size_t)argc + 1, sizeof(char *));
    if (child_argv == NULL) {{
        perror("allocate pipeline arguments");
        return 126;
    }}
    child_argv[0] = python_executable;
    for (int index = 1; index < argc; index++) child_argv[index] = argv[index];
    execv(python_executable, child_argv);
    perror("launch bundled Python");
    free(child_argv);
    return errno == ENOENT ? 127 : 126;
}}
"""
    with tempfile.TemporaryDirectory(prefix="pipeline-python-launcher-") as temporary:
        source_path = Path(temporary) / "launcher.c"
        source_path.write_text(source, encoding="utf-8")
        subprocess.run(
            ["/usr/bin/clang", "-O2", str(source_path), "-o", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def bundle_pipeline_runtime(
    project_root: Path, contents: Path, resources: Path,
    frameworks: Path, helpers: Path,
) -> dict[str, object]:
    pipeline_root = resources / "Pipeline"
    bundle_source_manifest = _copy_release_bundle_sources(project_root, pipeline_root)
    privacy_audit = sanitize_embedded_project_files(
        pipeline_root, project_root=project_root,
    )

    metadata_by_environment: dict[str, dict[str, str]] = {}
    framework_by_version: dict[str, tuple[str, str]] = {}
    for environment_name, executable in PIPELINE_ENVIRONMENTS.items():
        if not executable.is_file():
            raise FileNotFoundError(f"pipeline Python missing: {executable}")
        metadata = _runtime_metadata(executable)
        metadata_by_environment[environment_name] = metadata
        version = f"{metadata['major']}.{metadata['minor']}"
        if version not in framework_by_version:
            framework, major_minor = _copy_pipeline_python_framework(metadata, frameworks)
            framework_by_version[version] = (
                str(framework.relative_to(frameworks)), major_minor,
            )
        shutil.copytree(
            Path(metadata["purelib"]),
            resources / "PipelineEnvs" / environment_name / "site-packages",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )

    runtime_privacy_audit = sanitize_embedded_project_files(
        resources / "PipelineEnvs", project_root=project_root,
    )

    wrapper_root = helpers / "PipelinePython"
    wrapper_root.mkdir(parents=True)
    role_paths: dict[str, str] = {}
    for role, environment_name in PIPELINE_ROLE_ENVIRONMENT.items():
        metadata = metadata_by_environment[environment_name]
        framework_name, major_minor = framework_by_version[
            f"{metadata['major']}.{metadata['minor']}"
        ]
        wrapper = wrapper_root / role
        _write_pipeline_wrapper(wrapper, framework_name, major_minor, environment_name)
        role_paths[role] = f"$APP_CONTENTS/Helpers/PipelinePython/{role}"

    source_contract = json.loads(
        (project_root / "configs/media_archive_app_runtime_contract_v1.json").read_text(
            encoding="utf-8"
        )
    )

    def portable(value):
        if isinstance(value, str):
            return value.replace(
                str(project_root), "$APP_RESOURCES/Pipeline",
            ).replace(str(BUILD_MODEL_ROOT), "$MODEL_ROOT")
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, dict):
            return {key: portable(item) for key, item in value.items()}
        return value

    contract = portable(source_contract)
    contract["project_root"] = "$APP_RESOURCES/Pipeline"
    contract["python"] = role_paths
    contract["config_path_policy"] = "materialize_effective_runtime_configs_v1"
    contract_path = resources / "runtime_contract.json"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "contract": "media_archive_portable_pipeline_runtime_v1",
        "python_roles": role_paths,
        "unique_environments": sorted(PIPELINE_ENVIRONMENTS),
        "models_included": False,
        "network_download_implemented": False,
        "runtime_contract_sha256": _sha256(contract_path),
        "bundle_sources": bundle_source_manifest,
        "developer_private_paths": privacy_audit,
        "runtime_developer_private_paths": runtime_privacy_audit,
    }
    (resources / "portable_runtime_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def sanitize_embedded_project_files(
    pipeline_root: Path, *, project_root: Path,
) -> dict[str, object]:
    """Remove build-machine paths from the app's own embedded text assets.

    Development configs remain useful in the checkout, but the portable app
    resolves them through ``runtime_contract.json`` and task-local effective
    configs.  This pass therefore changes only the copied bundle tree and then
    fails closed if any developer-home marker remains.
    """
    replacements = {
        str(project_root): "$APP_RESOURCES/Pipeline",
        str(PIPELINE_ENV_ROOT): "$BUNDLED_PIPELINE_ENVS",
        str(BUILD_MODEL_ROOT): "$MODEL_ROOT",
        str(BUILD_PADDLEX_ROOT): "$TASK_RUNTIME/paddlex_cache",
        str(Path.home()): "$USER_HOME",
    }
    changed_files: list[str] = []
    scanned_files = 0
    for path in sorted(pipeline_root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or (
                path.suffix.lower() not in TEXT_BUNDLE_SUFFIXES
                and path.name != "RECORD"
            )
        ):
            continue
        scanned_files += 1
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sanitized = source
        for original, replacement in replacements.items():
            sanitized = sanitized.replace(original, replacement)
        if sanitized != source:
            path.write_text(sanitized, encoding="utf-8")
            changed_files.append(str(path.relative_to(pipeline_root)))

    violations: list[str] = []
    for path in sorted(pipeline_root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or (
                path.suffix.lower() not in TEXT_BUNDLE_SUFFIXES
                and path.name != "RECORD"
            )
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for marker in dict.fromkeys(DEVELOPER_PRIVATE_PATH_MARKERS):
            if marker and marker in source:
                violations.append(f"{path.relative_to(pipeline_root)}:{marker}")
    if violations:
        raise RuntimeError(
            "embedded_developer_private_path_detected:" + " | ".join(violations[:20])
        )
    return {
        "status": "PASS",
        "scanned_text_files": scanned_files,
        "sanitized_file_count": len(changed_files),
        "sanitized_files": changed_files,
        "remaining_violation_count": 0,
    }


def build_bundle(
    project_root: Path,
    output_dir: Path,
    python_executable: Path,
    app_name: str = APP_BUNDLE_NAME,
    development_database: Optional[Path] = None,
    development_output_root: Optional[Path] = None,
    bundle_pipeline_runtimes: bool = False,
    apple_silicon_only: bool = False,
) -> Path:
    project_root = project_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    source_package = project_root / "apps" / "media_archive_image_video_ui_v124_candidate"
    if not source_package.is_dir():
        raise FileNotFoundError(f"native app package not found: {source_package}")
    if not python_executable.is_file():
        raise FileNotFoundError(f"python executable not found: {python_executable}")
    if apple_silicon_only and os.uname().machine != "arm64":
        raise RuntimeError("apple_silicon_only_build_requires_arm64_host")

    bundle = output_dir / app_name
    if bundle.exists():
        raise FileExistsError(f"refusing to replace existing app bundle: {bundle}")
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    helpers = contents / "Helpers"
    resources = contents / "Resources"
    frameworks = contents / "Frameworks"
    macos.mkdir(parents=True)
    helpers.mkdir(parents=True)
    resources.mkdir(parents=True)
    build_app_icon(source_package / "assets" / "app_icon_1024.png", resources)
    shutil.copytree(source_package, resources / "media_archive_image_video_ui")
    python_runtime = locate_python_framework(python_executable)
    python_framework = copy_app_python_framework(python_runtime, frameworks)

    if bundle_pipeline_runtimes:
        bundle_pipeline_runtime(
            project_root, contents, resources, frameworks, helpers,
        )

    config = {
        "app_bundle_contract": "media_archive_native_image_video_app_bundle_v1",
        "configuration_state": "development_attached" if development_database else "first_run_clean",
        "project_root": "$APP_RESOURCES/Pipeline" if bundle_pipeline_runtimes else str(project_root),
        "runtime_contract_path": (
            "$APP_RESOURCES/runtime_contract.json"
            if bundle_pipeline_runtimes else
            str(project_root / "configs" / "media_archive_app_runtime_contract_v1.json")
        ),
        "database": str(development_database.expanduser().resolve()) if development_database else "",
        "output_root": str(development_output_root.expanduser().resolve()) if development_output_root else "",
        "visible_media_types": ["image", "video"],
        "hidden_media_interfaces": ["audio", "text"],
        "web_server_used": False,
        "appearance_policy": "native_swiftui_system_appearance_v1",
        "runtime_policy": "native_swiftui_embedded_python_bridge_v1",
        "portable_pipeline_runtimes": bool(bundle_pipeline_runtimes),
        "models_included": False,
        "supported_architectures": ["arm64"] if apple_silicon_only else ["host_default"],
    }
    (resources / "app_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_identity = _source_identity(project_root)
    (resources / "release_source_identity.json").write_text(
        json.dumps(source_identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    python_bridge = resources / "python_bridge.py"
    python_bridge.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "resources = Path(__file__).resolve().parents[1] / 'Resources'\n"
        "os.environ.setdefault('TK_SILENCE_DEPRECATION', '1')\n"
        "sys.path.insert(0, str(resources))\n"
        "from media_archive_image_video_ui.native_bridge import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    helper_executable = helpers / "素材大整理Python"
    compile_native_launcher(
        helper_executable, python_framework, python_runtime,
        apple_silicon_only=apple_silicon_only,
    )
    executable = macos / APP_RELEASE_NAME
    compile_swift_frontend(
        source_package / "native_frontend.swift", executable,
        apple_silicon_only=apple_silicon_only,
    )
    compile_avfoundation_audio_salvage(
        source_package / "avfoundation_audio_salvage.swift",
        helpers / "AVFoundationAudioSalvage",
    )
    plist = {
        "CFBundleDisplayName": APP_RELEASE_NAME,
        "CFBundleExecutable": APP_RELEASE_NAME,
        "CFBundleIdentifier": APP_BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_RELEASE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_SEMVER,
        "CFBundleVersion": APP_BUILD_NUMBER,
        "CFBundleIconFile": "AppIcon.icns",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": True,
        "NSHumanReadableCopyright": "Copyright © 2026 Horizon-94. GPL-3.0-only.",
        "HorizonBuildDate": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "HorizonOfficialSource": "https://github.com/Horizon-94/local-media-organizer",
        "HorizonSourceContentSHA256": str(source_identity["content_sha256"]),
        "HorizonSourceGitCommit": str(source_identity["git_commit"]),
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    return bundle


MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def _codesign(path: Path) -> None:
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def sign_bundle(bundle: Path) -> None:
    """Sign portable Mach-O code and bundles in deterministic inside-out order."""
    main_executable = bundle / "Contents" / "MacOS" / bundle.stem
    macho_files = sorted(
        (
            path for path in bundle.rglob("*")
            if _is_macho(path) and path != main_executable
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in macho_files:
        _codesign(path)

    nested_bundles = sorted(
        (
            path for path in bundle.rglob("*")
            if path.is_dir()
            and not path.is_symlink()
            and path.suffix.lower() in {".app", ".framework", ".bundle"}
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in nested_bundles:
        _codesign(path)

    _codesign(bundle)


def build_pkg(bundle: Path, output_path: Path) -> Path:
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"refusing to replace existing installer: {output_path}")
    with tempfile.TemporaryDirectory(prefix="material-organizer-pkg-root-") as temporary:
        package_root = Path(temporary)
        applications = package_root / "Applications"
        applications.mkdir()
        shutil.copytree(bundle, applications / bundle.name, symlinks=True)
        subprocess.run(
            [
                "/usr/bin/pkgbuild", "--root", str(package_root),
                "--install-location", "/", "--identifier",
                f"{APP_BUNDLE_IDENTIFIER}.pkg", "--version", APP_SEMVER,
                str(output_path),
            ],
            check=True,
        )
    return output_path


def build_dmg(bundle: Path, output_path: Path) -> Path:
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"refusing to replace existing disk image: {output_path}")
    with tempfile.TemporaryDirectory(prefix="media-archive-dmg-") as temporary:
        staging = Path(temporary) / APP_RELEASE_NAME
        staging.mkdir()
        # Preserve framework and venv symlinks. Dereferencing them duplicates
        # runtime trees and invalidates the signed app copied into the DMG.
        shutil.copytree(bundle, staging / bundle.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        (staging / "安装说明.txt").write_text(
            f"把“{APP_RELEASE_NAME}”拖入 Applications 文件夹。以后可从“应用程序”、启动台或 Dock 打开。\n"
            "安装包已经包含程序所需的 Python 运行环境，但不包含第三方模型。\n"
            "首次打开后进入“设置 → 本地模型位置”，选择包含各模型子目录的总目录，点击“检查并保存”。\n"
            "全部模型显示“已找到”后退出并重新打开应用，即可开始建立或读取素材库。\n"
            "支持图片、视频与人声转写证据搜索；不会修改原始素材。\n",
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [
                    "/usr/bin/hdiutil", "create", "-volname", f"{APP_RELEASE_NAME} {APP_SEMVER}",
                    "-srcfolder", str(staging), "-format", "UDZO", str(output_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"hdiutil failed: {exc.stderr.strip() or exc.stdout.strip()}") from exc
    return output_path


def build_parser(project_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the pure-Python macOS image/video app bundle")
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--output-dir", type=Path, default=project_root / "dist")
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    parser.add_argument("--development-database", type=Path, help="attach a database only for a non-release QA build")
    parser.add_argument("--development-output-root", type=Path, help="QA output root; ignored by a clean first-run build")
    parser.add_argument("--dmg", action="store_true", help="build the DMG installer only")
    parser.add_argument(
        "--portable-runtimes", action="store_true",
        help="bundle pipeline scripts and local Python environments; models remain external",
    )
    parser.add_argument(
        "--apple-silicon-only", action="store_true",
        help="build and label this release for Apple Silicon (arm64) only",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    project_root = Path(__file__).resolve().parents[2]
    args = build_parser(project_root).parse_args(argv)
    source_identity = _source_identity(args.project_root.expanduser().resolve())
    bundle = build_bundle(
        args.project_root, args.output_dir, args.python,
        development_database=args.development_database,
        development_output_root=args.development_output_root,
        bundle_pipeline_runtimes=args.portable_runtimes,
        apple_silicon_only=args.apple_silicon_only,
    )
    sign_bundle(bundle)
    installers = []
    if args.dmg:
        installers.append(str(build_dmg(
            bundle, args.output_dir / f"{APP_RELEASE_NAME}-{APP_SEMVER}.dmg",
        )))
    manifest = {
        "status": "PASS",
        "app_bundle": bundle.name,
        "app_version": APP_BUNDLE_VERSION,
        "semantic_version": APP_SEMVER,
        "build_number": APP_BUILD_NUMBER,
        "build_created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "installers": [Path(path).name for path in installers],
        "code_signature": "ad_hoc_local",
        "ui_kind": "native_swiftui_python_backend",
        "web_server_used": False,
        "central_database_write": False,
        "model_run": False,
        "portable_pipeline_runtimes": bool(args.portable_runtimes),
        "models_included": False,
        "supported_architectures": ["arm64"] if args.apple_silicon_only else ["host_default"],
        "configuration_state": "development_attached" if args.development_database else "first_run_clean",
        "official_source": "https://github.com/Horizon-94/local-media-organizer",
        "license": "GPL-3.0-only",
        "source_identity": source_identity,
        "local_user_paths_in_manifest": False,
        "sha256": {
            Path(path).name: _sha256(Path(path)) for path in installers
        },
    }
    (args.output_dir / "release_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
