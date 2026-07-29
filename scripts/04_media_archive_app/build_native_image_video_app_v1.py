#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence


APP_RELEASE_NAME = "本地数据库"
APP_BUNDLE_NAME = f"{APP_RELEASE_NAME}.app"
APP_BUNDLE_VERSION = "1.1.4-search-progress-warm-cache"
APP_SEMVER = "1.1.4"
APP_BUNDLE_IDENTIFIER = "local.horizon.local-database.v114"
APP_AUTHOR = "Horizon-94"
APP_SOURCE_URL = "https://github.com/Horizon-94/local-media-organizer"
APP_LICENSE = "GPL-3.0-only"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = Path(
    os.environ.get("MEDIA_ARCHIVE_ENV_ROOT", str(PROJECT_ROOT.parent / "envs"))
).expanduser().absolute()
PIPELINE_ENVIRONMENTS = {
    "visual": ENV_ROOT / "media-archive-v06-visual/bin/python",
    "yolo": ENV_ROOT / "media-archive-v06-yolo/bin/python",
    "qwen": ENV_ROOT / "qwen-vl/bin/python",
    "ocr": ENV_ROOT / "media-archive-v06-ocr/bin/python",
    "embedding": ENV_ROOT / "media-archive-embedding/bin/python",
}
PIPELINE_ROLE_ENVIRONMENT = {
    "system": "visual", "visual": "visual",
    "yolo": "yolo", "person_reid": "yolo",
    "qwen": "qwen", "ocr": "ocr", "embedding": "embedding",
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
        "%s/Frameworks/Python3.framework/Versions/Current", contents_path
    );
    snprintf(launcher_path, sizeof(launcher_path), "%s/Resources/python_bridge.py", contents_path);
    setenv("PYTHONHOME", python_home, 1);
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


def locate_python_framework(python_executable: Path) -> Path:
    metadata = json.loads(subprocess.run(
        [
            str(python_executable), "-c",
            "import json,sys,sysconfig;"
            "print(json.dumps({'base_prefix':sys.base_prefix,"
            "'framework':sysconfig.get_config_var('PYTHONFRAMEWORK')}))",
        ],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=build_environment(),
    ).stdout)
    version_root = Path(metadata["base_prefix"]).resolve()
    framework = version_root.parent.parent
    expected_names = {
        f"{metadata['framework']}.framework",
        "Python.framework",
        "Python3.framework",
    }
    if (
        framework.name not in expected_names
        or not (version_root / "Headers/Python.h").is_file()
    ):
        raise RuntimeError(f"unsupported Python framework runtime: {framework}")
    return framework


def _framework_version_root(framework: Path) -> Path:
    current = framework / "Versions/Current"
    if (current / "Headers/Python.h").is_file():
        return current.resolve()
    candidates = sorted(
        path for path in (framework / "Versions").iterdir()
        if path.is_dir() and (path / "Headers/Python.h").is_file()
    )
    if len(candidates) != 1:
        raise RuntimeError(f"cannot select Python framework version: {framework}")
    return candidates[0]


def normalize_python_framework(framework: Path) -> None:
    version_root = _framework_version_root(framework)
    current = framework / "Versions/Current"
    if not current.exists():
        current.symlink_to(version_root.name)
    binary_name = next(
        (
            name for name in ("Python", "Python3")
            if (version_root / name).is_file()
        ),
        "",
    )
    if not binary_name:
        raise RuntimeError(f"Python framework binary missing: {version_root}")
    for link_name, target in (
        (binary_name, f"Versions/Current/{binary_name}"),
        ("Resources", "Versions/Current/Resources"),
        ("Headers", "Versions/Current/Headers"),
    ):
        link = framework / link_name
        if not link.exists():
            link.symlink_to(target)


def python_framework_architectures(framework: Path) -> tuple[str, ...]:
    version_root = _framework_version_root(framework)
    binary = next(
        version_root / name for name in ("Python", "Python3")
        if (version_root / name).is_file()
    )
    output = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(binary)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.split()
    supported = tuple(name for name in ("arm64", "x86_64") if name in output)
    if not supported:
        raise RuntimeError(f"unsupported Python framework architecture: {output}")
    return supported


def relocate_python_framework(framework: Path) -> None:
    version_root = _framework_version_root(framework)
    binary_name = next(
        name for name in ("Python", "Python3")
        if (version_root / name).is_file()
    )
    subprocess.run(
        [
            "/usr/bin/install_name_tool", "-id",
            f"@rpath/{framework.name}/Versions/Current/{binary_name}",
            str(version_root / binary_name),
        ],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def compile_native_launcher(executable: Path, framework: Path) -> None:
    headers = _framework_version_root(framework) / "Headers"
    architecture_arguments = [
        argument
        for architecture in python_framework_architectures(framework)
        for argument in ("-arch", architecture)
    ]
    with tempfile.TemporaryDirectory(prefix="media-archive-launcher-") as temporary:
        source = Path(temporary) / "launcher.c"
        source.write_text(
            NATIVE_LAUNCHER_SOURCE.replace("Python3.framework", framework.name),
            encoding="utf-8",
        )
        subprocess.run(
            [
                "/usr/bin/clang", "-std=c11", *architecture_arguments,
                "-I", str(headers), str(source), "-F", str(framework.parent),
                "-framework", framework.stem,
                "-Wl,-rpath,@executable_path/../Frameworks",
                "-o", str(executable),
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=build_environment(),
        )
    executable.chmod(0o755)


def compile_swift_frontend(
    source: Path, executable: Path,
    architectures: Sequence[str] = ("arm64", "x86_64"),
) -> None:
    with tempfile.TemporaryDirectory(prefix="media-archive-swiftui-") as temporary:
        temporary_root = Path(temporary)
        architecture_outputs = []
        for architecture in architectures:
            architecture_output = temporary_root / f"frontend-{architecture}"
            completed = subprocess.run(
                [
                    # This is a UI shell, not a numeric workload.  The Swift
                    # 6.3 optimiser bundled with the standalone CLT can exit
                    # nondeterministically on this large SwiftUI view while
                    # the unoptimised build is stable on both architectures.
                    "/usr/bin/swiftc", "-swift-version", "5", "-Onone",
                    "-target", f"{architecture}-apple-macosx12.0",
                    "-framework", "SwiftUI", "-framework", "AppKit",
                    "-framework", "AVFoundation", "-framework", "AVKit",
                    "-module-cache-path", str(temporary_root / f"module-cache-{architecture}"),
                    str(source), "-o", str(architecture_output),
                ],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=build_environment(),
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no compiler output"
                raise RuntimeError(f"Swift {architecture} compile failed: {detail}")
            architecture_outputs.append(architecture_output)
        subprocess.run(
            ["/usr/bin/lipo", "-create", *(str(path) for path in architecture_outputs), "-output", str(executable)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=build_environment(),
        )
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
    for name in ("scripts", "configs", "migrations"):
        shutil.copytree(
            project_root / name, pipeline_root / name, symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    shutil.copytree(
        project_root / "docs" / "pipeline_rules",
        pipeline_root / "docs" / "pipeline_rules",
        symlinks=True,
        ignore=shutil.ignore_patterns(".DS_Store"),
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
        environment_target = resources / "PipelineEnvs" / environment_name / "site-packages"
        shutil.copytree(
            Path(metadata["purelib"]),
            environment_target,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        sanitize_portable_python_metadata(environment_target)

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
            return (
                value
                .replace(str(project_root), "$APP_RESOURCES/Pipeline")
                .replace("$PROJECT_ROOT", "$APP_RESOURCES/Pipeline")
                .replace(
                    "/Users/yourname/Documents/AI-Local/media-archive-clean",
                    "$APP_RESOURCES/Pipeline",
                )
                .replace("/Users/yourname/Documents/model", "$MODEL_ROOT")
            )
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
    }
    (resources / "portable_runtime_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def sanitize_portable_python_metadata(site_packages: Path) -> None:
    """Remove local checkout references that Python installers record as metadata.

    The application package itself is bundled separately, so editable-install
    path hints are neither required nor safe to distribute.
    """
    for path in sorted(site_packages.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == "direct_url.json":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "file://" in text or "/Users/" in text:
                path.unlink()
        elif path.suffix == ".pth":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            kept = [line for line in lines if "/Users/" not in line and "file://" not in line]
            if kept:
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                path.unlink()
        elif path.name == "RECORD":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            kept = [
                line for line in lines
                if "/Users/" not in line and "file://" not in line
            ]
            path.write_text(
                ("\n".join(kept) + "\n") if kept else "",
                encoding="utf-8",
            )


def bundle_release_documents(project_root: Path, resources: Path) -> None:
    documentation = resources / "Documentation"
    documentation.mkdir()
    files = {
        project_root / "LICENSE": documentation / "LICENSE-GPL-3.0.txt",
        project_root / "LICENSE_HISTORY.md": documentation / "LICENSE_HISTORY.md",
        project_root / "NOTICE": documentation / "NOTICE.txt",
        project_root / "MODEL_SOURCES.md": documentation / "MODEL_SOURCES.md",
        project_root / "docs" / "MODEL_SETUP.md": documentation / "MODEL_SETUP.md",
        project_root / "docs" / "BUILD_FROM_SOURCE.md": documentation / "BUILD_FROM_SOURCE.md",
    }
    for source, destination in files.items():
        if not source.is_file():
            raise FileNotFoundError(f"release document missing: {source}")
        shutil.copy2(source, destination)


def build_bundle(
    project_root: Path,
    output_dir: Path,
    python_executable: Path,
    app_name: str = APP_BUNDLE_NAME,
    development_database: Optional[Path] = None,
    development_output_root: Optional[Path] = None,
    bundle_pipeline_runtimes: bool = False,
) -> Path:
    project_root = project_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    source_package = project_root / "apps" / "media_archive_image_video_ui"
    if not source_package.is_dir():
        raise FileNotFoundError(f"native app package not found: {source_package}")
    if not python_executable.is_file():
        raise FileNotFoundError(f"python executable not found: {python_executable}")

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
    bundle_release_documents(project_root, resources)
    python_framework = locate_python_framework(python_executable)
    bundled_python_framework = frameworks / python_framework.name
    shutil.copytree(
        python_framework, bundled_python_framework, symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    normalize_python_framework(bundled_python_framework)
    relocate_python_framework(bundled_python_framework)

    if bundle_pipeline_runtimes:
        bundle_pipeline_runtime(
            project_root, contents, resources, frameworks, helpers,
        )

    config = {
        "app_bundle_contract": "media_archive_native_image_video_app_bundle_v1",
        "configuration_state": "development_attached" if development_database else "first_run_clean",
        "project_root": (
            "$APP_RESOURCES/Pipeline"
            if bundle_pipeline_runtimes else str(project_root)
        ),
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
        "author": APP_AUTHOR,
        "official_source": APP_SOURCE_URL,
        "license": APP_LICENSE,
    }
    (resources / "app_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
    supported_architectures = python_framework_architectures(bundled_python_framework)
    compile_native_launcher(helper_executable, bundled_python_framework)
    executable = macos / APP_RELEASE_NAME
    compile_swift_frontend(
        source_package / "native_frontend.swift", executable,
        architectures=supported_architectures,
    )

    plist = {
        "CFBundleDisplayName": APP_RELEASE_NAME,
        "CFBundleExecutable": APP_RELEASE_NAME,
        "CFBundleIdentifier": APP_BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_RELEASE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_SEMVER,
        "CFBundleVersion": "114",
        "CFBundleIconFile": "AppIcon.icns",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": True,
        "LSArchitecturePriority": list(supported_architectures),
        "NSHumanReadableCopyright": f"Copyright © 2026 {APP_AUTHOR}",
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
        documentation = bundle / "Contents" / "Resources" / "Documentation"
        shutil.copy2(documentation / "LICENSE-GPL-3.0.txt", staging / "GNU GPL v3.0.txt")
        shutil.copy2(documentation / "MODEL_SETUP.md", staging / "模型安装说明.md")
        shutil.copy2(documentation / "NOTICE.txt", staging / "项目与版权说明.txt")
        (staging / "安装说明.txt").write_text(
            f"把“{APP_RELEASE_NAME}”拖入 Applications 文件夹。以后可从“应用程序”、启动台或 Dock 打开。\n"
            "当前版本只显示图片与视频；不会修改原始素材。\n"
            "模型不在安装包内，也不会自动下载。请阅读“模型安装说明.md”。\n"
            f"官方源码：{APP_SOURCE_URL}\n"
            f"Copyright © 2026 {APP_AUTHOR} · {APP_LICENSE}\n",
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    project_root = Path(__file__).resolve().parents[2]
    args = build_parser(project_root).parse_args(argv)
    bundle = build_bundle(
        args.project_root, args.output_dir, args.python,
        development_database=args.development_database,
        development_output_root=args.development_output_root,
        bundle_pipeline_runtimes=args.portable_runtimes,
    )
    sign_bundle(bundle)
    if args.portable_runtimes:
        subprocess.run(
            [
                sys.executable,
                str(args.project_root / "scripts" / "release_artifact_audit.py"),
                str(bundle),
            ],
            check=True,
        )
    installers = []
    if args.dmg:
        installers.append(str(build_dmg(
            bundle, args.output_dir / f"{APP_RELEASE_NAME}-{APP_SEMVER}.dmg",
        )))
    result = {
        "status": "PASS",
        "app_bundle": str(bundle),
        "app_version": APP_BUNDLE_VERSION,
        "installers": installers,
        "code_signature": "ad_hoc_local",
        "ui_kind": "native_swiftui_python_backend",
        "web_server_used": False,
        "central_database_write": False,
        "model_run": False,
        "portable_pipeline_runtimes": bool(args.portable_runtimes),
        "models_included": False,
        "configuration_state": "development_attached" if args.development_database else "first_run_clean",
        "official_source": APP_SOURCE_URL,
        "license": APP_LICENSE,
        "sha256": {
            str(Path(path).name): _sha256(Path(path))
            for path in installers
        },
    }
    if args.portable_runtimes:
        (args.output_dir / "release_manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
