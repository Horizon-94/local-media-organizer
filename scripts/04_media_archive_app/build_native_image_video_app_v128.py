#!/usr/bin/env python3
"""Build the 1.2.8 generic spoken-content and interview release."""
from __future__ import annotations

from pathlib import Path


SOURCE_REPLACEMENTS = {
    '"""Build the tested 1.2.7 cross-library-search release."""':
        '"""Build the 1.2.8 generic spoken-content and interview release."""',
    'APP_RELEASE_NAME = "本地数据库 1.2.7"':
        'APP_RELEASE_NAME = "本地数据库 1.2.8"',
    'APP_BUNDLE_VERSION = "1.2.7"': 'APP_BUNDLE_VERSION = "1.2.8"',
    'APP_SEMVER = "1.2.7"': 'APP_SEMVER = "1.2.8"',
    'APP_BUILD_NUMBER = "127"': 'APP_BUILD_NUMBER = "128"',
    'APP_VERSION = "1.2.7"': 'APP_VERSION = "1.2.8"',
    '"release_version": "1.2.7"': '"release_version": "1.2.8"',
}


def builder_namespace() -> dict:
    source_path = Path(__file__).with_name("build_native_image_video_app_v127.py")
    source = source_path.read_text(encoding="utf-8")
    for old, new in SOURCE_REPLACEMENTS.items():
        if source.count(old) != 1:
            raise RuntimeError(f"v128_builder_anchor_mismatch:{old}:{source.count(old)}")
        source = source.replace(old, new)
    namespace = {
        "__name__": "v128_packager_impl",
        "__file__": str(Path(__file__).resolve()),
    }
    exec(compile(source, str(Path(__file__).resolve()), "exec"), namespace)
    return namespace["builder_namespace"]()


if __name__ == "__main__":
    raise SystemExit(builder_namespace()["main"]())
