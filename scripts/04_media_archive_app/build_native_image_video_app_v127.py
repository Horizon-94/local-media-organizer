#!/usr/bin/env python3
"""Build the tested 1.2.7 cross-library-search release."""
from __future__ import annotations

import hashlib
from pathlib import Path


REPLACEMENTS = {
    'APP_RELEASE_NAME = "本地数据库 1.2.4 候选版"': 'APP_RELEASE_NAME = "本地数据库 1.2.7"',
    'APP_BUNDLE_VERSION = "1.2.4-candidate"': 'APP_BUNDLE_VERSION = "1.2.7"',
    'APP_SEMVER = "1.2.4"': 'APP_SEMVER = "1.2.7"',
    'APP_BUNDLE_IDENTIFIER = "local.horizon.local-database.candidate"':
        'APP_BUNDLE_IDENTIFIER = "local.horizon.local-database.release"',
    'APP_BUILD_NUMBER = "124"': 'APP_BUILD_NUMBER = "127"',
    'media_archive_image_video_ui_v124_candidate': 'media_archive_image_video_ui_v125_candidate',
}


def builder_namespace() -> dict:
    path = Path(__file__).with_name("build_native_image_video_app_v124_candidate.py")
    source = path.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS.items():
        if old not in source:
            raise RuntimeError(f"v127_builder_anchor_missing:{old}")
        source = source.replace(old, new)
    anchor = '    shutil.copytree(source_package, resources / "media_archive_image_video_ui")'
    if source.count(anchor) != 1:
        raise RuntimeError("v127_bridge_version_anchor_missing")
    source = source.replace(anchor, anchor + '''
    bridge = resources / "media_archive_image_video_ui" / "native_bridge.py"
    bridge_source = bridge.read_text(encoding="utf-8")
    version_anchor = 'APP_VERSION = "1.2.5-development"'
    if bridge_source.count(version_anchor) != 1:
        raise RuntimeError("v127_bridge_version_missing")
    bridge.write_text(bridge_source.replace(version_anchor, 'APP_VERSION = "1.2.7"'), encoding="utf-8")
    search_runtime = resources / "SearchRuntime"
    bundled_search_files = {
        "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py":
            project_root / "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py",
        "scripts/03_stop03_visual_analysis/stop03_5e_hybrid_visual_text_search_v2.py":
            project_root / "scripts/03_stop03_visual_analysis/stop03_5e_hybrid_visual_text_search_v2.py",
        "scripts/03_stop03_visual_analysis/stop03_5e_text_search_contract_v1.py":
            project_root / "scripts/03_stop03_visual_analysis/stop03_5e_text_search_contract_v1.py",
        "scripts/03_stop03_visual_analysis/stop03_5e_text_search_smoke_v1.py":
            project_root / "scripts/03_stop03_visual_analysis/stop03_5e_text_search_smoke_v1.py",
        "configs/stop03_5e_hybrid_visual_text_search_v2.json":
            project_root / "configs/stop03_5e_hybrid_visual_text_search_v2.json",
        "configs/stop03_5e_text_search_contract_v1.json":
            project_root / "configs/stop03_5e_text_search_contract_v1.json",
    }
    for relative, source_path in bundled_search_files.items():
        destination = search_runtime / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
''')
    config_anchor = '''        "portable_pipeline_runtimes": bool(bundle_pipeline_runtimes),
        "models_included": False,'''
    if source.count(config_anchor) != 1:
        raise RuntimeError("v127_search_runtime_config_anchor_missing")
    source = source.replace(config_anchor, config_anchor + '''
        "search_runtime_path": "$APP_RESOURCES/SearchRuntime",
        "embedding_python": (
            "$APP_CONTENTS/Helpers/PipelinePython/embedding"
            if bundle_pipeline_runtimes else
            str(project_root.parent / "envs/media-archive-embedding/bin/python")
        ),
        "openclip_python": (
            "$APP_CONTENTS/Helpers/PipelinePython/visual"
            if bundle_pipeline_runtimes else
            str(project_root.parent / "envs/media-archive-v06-visual/bin/python")
        ),
''')
    ns = {"__name__": "v127_packager", "__file__": str(Path(__file__).resolve())}
    exec(compile(source, str(Path(__file__).resolve()), "exec"), ns)
    original_identity = ns["_source_identity"]

    def source_identity(root: Path) -> dict:
        identity = original_identity(root)
        package = root / "apps/media_archive_image_video_ui_v125_candidate"
        files = [p for p in package.rglob("*") if p.is_file()
                 and "__pycache__" not in p.parts and p.suffix != ".pyc"]
        files += [Path(__file__).resolve(), path.resolve()]
        digest = hashlib.sha256(identity["content_sha256"].encode())
        for item in sorted(files):
            digest.update(str(item.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(item.read_bytes()).digest())
        return {**identity, "content_sha256": digest.hexdigest(),
                "selection_source_file_count": len(files), "release_version": "1.2.7",
                "identity_scope": "base_allowlist_plus_selection_package_and_packagers"}

    ns["_source_identity"] = source_identity
    return ns


if __name__ == "__main__":
    raise SystemExit(builder_namespace()["main"]())
