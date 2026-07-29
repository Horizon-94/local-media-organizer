from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"

import sys

sys.path.insert(0, str(APPS))

from media_archive_image_video_ui.native_app import APP_VERSION, default_paths, format_timecode  # noqa: E402
from media_archive_image_video_ui.processing_profile import (  # noqa: E402
    build_processing_profile,
    detect_hardware,
    recommend_workers,
    save_processing_profile,
)
from media_archive_image_video_ui.repository import ReadonlyMediaRepository, VISIBLE_MEDIA_TYPES  # noqa: E402
from media_archive_image_video_ui.search_jobs import (  # noqa: E402
    SearchJob, SearchJobManager, offline_search_environment,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NativeImageVideoAppTests(unittest.TestCase):
    def test_visible_scope_excludes_audio_and_text(self) -> None:
        self.assertEqual(VISIBLE_MEDIA_TYPES, ("image", "video"))

    def test_timecode_uses_minutes_seconds_and_milliseconds(self) -> None:
        self.assertEqual(format_timecode(62_345), "01:02.345")
        self.assertEqual(format_timecode(3_662_345), "01:01:02.345")

    def test_search_job_public_state_does_not_contain_raw_query(self) -> None:
        job = SearchJob(
            job_id="ui5e_test", query_sha256="a" * 64, status="queued",
            created_at=1.0, output_dir=Path("/tmp/out"), log_path=Path("/tmp/log"),
        )
        text = json.dumps(job.public(), ensure_ascii=False)
        self.assertNotIn("query_text", text)
        self.assertNotIn("raw_query", text)

    def test_search_subprocess_does_not_inherit_embedded_python_home(self) -> None:
        previous = {key: __import__("os").environ.get(key) for key in (
            "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__",
        )}
        try:
            for key in previous:
                __import__("os").environ[key] = "/embedded/python"
            __import__("os").environ["PYTHONUSERBASE"] = "/embedded/userbase"
            __import__("os").environ["VIRTUAL_ENV"] = "/embedded/venv"
            environment = offline_search_environment(Path("/tmp/search"))
        finally:
            for key, value in previous.items():
                if value is None:
                    __import__("os").environ.pop(key, None)
                else:
                    __import__("os").environ[key] = value
            __import__("os").environ.pop("PYTHONUSERBASE", None)
            __import__("os").environ.pop("VIRTUAL_ENV", None)
        for key in previous:
            self.assertNotIn(key, environment)
        self.assertNotIn("PYTHONUSERBASE", environment)
        self.assertNotIn("VIRTUAL_ENV", environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("PYTHONPYCACHEPREFIX", environment)

    def test_search_command_uses_app_adapter_and_dynamic_filters(self) -> None:
        defaults = default_paths(ROOT)
        manager = SearchJobManager(
            db_path=defaults["db"], output_root=Path("/tmp/native-app-search-test"),
            search_script=defaults["search_script"], search_config=defaults["search_config"],
            embedding_python=defaults["embedding_python"], openclip_python=defaults["openclip_python"],
        )
        command = manager.build_command(
            "夜间人物", {"media_type": "video", "preview_window_ms": 5000, "limit": 17},
            Path("/tmp/native-app-search-test/output"),
        )
        joined = " ".join(command)
        self.assertIn("stop03_5e_hybrid_search_app_adapter_v1.py", joined)
        self.assertIn("--media-type video", joined)
        self.assertIn("--preview-window-ms 5000", joined)
        self.assertIn("--result-limit 17", joined)
        self.assertIn("--confirm-real-local-query", command)
        self.assertIn("--native-app-result-contract", command)
        self.assertIn(str(defaults["openclip_python"]), command)

    def test_search_manager_preserves_virtualenv_python_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "base-python"
            target.write_text("python")
            venv = root / "venv/bin"
            venv.mkdir(parents=True)
            link = venv / "python"
            link.symlink_to(target)
            marker = root / "marker"
            marker.write_text("x")
            manager = SearchJobManager(
                db_path=marker, output_root=root / "out", search_script=marker,
                search_config=marker, embedding_python=link, openclip_python=link,
            )
            self.assertEqual(manager.embedding_python, link.absolute())
            self.assertEqual(manager.openclip_python, link.absolute())
            self.assertNotEqual(manager.openclip_python, link.resolve())

    def test_search_app_adapter_scrubs_nested_python_environment(self) -> None:
        script = ROOT / "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py"
        namespace: dict[str, object] = {"__file__": str(script), "__name__": "test_adapter"}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        keys = namespace["python_runtime_environment_keys"]({
            "PATH": "/bin", "PYTHONHOME": "/embedded", "PYTHONUSERBASE": "/embedded/user",
            "__PYVENV_LAUNCHER__": "/embedded/python", "VIRTUAL_ENV": "/embedded/venv",
        })
        self.assertEqual(keys, [
            "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__",
        ])
        minimal = namespace["minimal_openclip_environment"]({
            "HOME": "/home", "PATH": "/bin", "EXECUTABLEPATH": "/App/helper",
            "RESOURCEPATH": "/App/Resources", "DYLD_FRAMEWORK_PATH": "/App/Frameworks",
            "PYTHONHOME": "/App/Python",
        })
        self.assertEqual(minimal["HOME"], "/home")
        self.assertNotIn("EXECUTABLEPATH", minimal)
        self.assertNotIn("RESOURCEPATH", minimal)
        self.assertNotIn("DYLD_FRAMEWORK_PATH", minimal)
        self.assertNotIn("PYTHONHOME", minimal)
        with tempfile.TemporaryDirectory() as temp:
            venv = Path(temp) / "visual"
            python = venv / "bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable))
            expected_site_packages = venv / "lib/python3.12/site-packages"
            expected_site_packages.mkdir(parents=True)
            executable, site_packages = namespace["explicit_venv_runtime"](python)
            self.assertTrue(executable.is_file())
            self.assertEqual(site_packages, expected_site_packages.resolve())
        source = script.read_text(encoding="utf-8")
        self.assertIn("sys.path.insert(0,site_packages)", source)

    def test_search_app_adapter_supports_portable_runtime_layout(self) -> None:
        script = ROOT / "scripts/04_media_archive_app/stop03_5e_hybrid_search_app_adapter_v1.py"
        namespace: dict[str, object] = {"__file__": str(script), "__name__": "test_adapter"}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temp:
            contents = Path(temp) / "本地数据库.app/Contents"
            launcher = contents / "Helpers/PipelinePython/visual"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("portable runtime")
            site_packages = contents / "Resources/PipelineEnvs/visual/site-packages"
            site_packages.mkdir(parents=True)

            executable, resolved_site_packages = namespace["explicit_venv_runtime"](launcher)

            self.assertEqual(executable, launcher.resolve())
            self.assertEqual(resolved_site_packages, site_packages.resolve())

    def test_visual_only_library_is_search_ready_without_text_embedding_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            con = __import__("sqlite3").connect(db)
            con.executescript(
                "CREATE TABLE source_assets(source_content_id TEXT,media_type TEXT);"
                "CREATE TABLE derived_assets(derived_id TEXT);"
                "CREATE TABLE visual_units(visual_unit_id TEXT,source_content_id TEXT);"
                "CREATE TABLE embeddings(embedding_id TEXT);"
                "INSERT INTO source_assets VALUES('image-1','image');"
                "INSERT INTO visual_units VALUES('visual-1','image-1');"
                "INSERT INTO embeddings VALUES('embedding');"
                "CREATE TABLE model_runs(run_id TEXT);"
                "CREATE TABLE visual_labels(visual_unit_id TEXT);"
                "CREATE TABLE visual_label_terms(label TEXT);"
            )
            con.close()
            marker = root / "file"
            marker.write_text("x")
            manager = SearchJobManager(
                db_path=db, output_root=root / "out", search_script=marker,
                search_config=marker, embedding_python=marker, openclip_python=marker,
            )
            readiness = manager.readiness()
            self.assertTrue(readiness["ready"])
            self.assertEqual(readiness["search_mode"], "VISUAL_ONLY")
            self.assertFalse(readiness["checks"]["text_enrichment_ready"])

    def test_actual_database_is_read_only_and_latest_run_counts_are_not_accumulated(self) -> None:
        db = ROOT / "media_archive.sqlite"
        if not db.is_file():
            self.skipTest("central database not present")
        before = sha256(db)
        repo = ReadonlyMediaRepository(db)
        overview = repo.overview()
        pipeline = repo.pipeline()
        active = repo.active_runs()
        after = sha256(db)
        self.assertEqual(before, after)
        self.assertEqual(overview["visible_media_types"], ["image", "video"])
        self.assertGreater(overview["source"]["image"]["count"], 0)
        self.assertGreater(overview["source"]["video"]["count"], 0)
        self.assertLessEqual(overview["recognition"]["qwen_success"], 390)
        self.assertTrue(pipeline["search_ready"])
        self.assertEqual(active, [], "terminal model_runs statuses must not appear as active work")
        text_stage = next(row for row in pipeline["stages"] if row["key"] == "text")
        self.assertEqual(text_stage["status"], "success")

    def test_bundle_builder_creates_pure_python_macos_app(self) -> None:
        builder_path = ROOT / "scripts" / "04_media_archive_app" / "build_native_image_video_app_v1.py"
        spec = importlib.util.spec_from_file_location("native_app_builder", builder_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            bundle = module.build_bundle(ROOT, Path(temp), Path("/usr/bin/python3"))
            executable = bundle / "Contents" / "MacOS" / "本地数据库"
            helper = bundle / "Contents" / "Helpers" / "素材大整理Python"
            bridge = bundle / "Contents" / "Resources" / "python_bridge.py"
            config = json.loads((bundle / "Contents" / "Resources" / "app_config.json").read_text(encoding="utf-8"))
            with (bundle / "Contents" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
            self.assertTrue(executable.is_file())
            self.assertTrue(executable.stat().st_mode & 0o100)
            self.assertEqual(bundle.name, "本地数据库.app")
            self.assertEqual(info["CFBundleDisplayName"], "本地数据库")
            self.assertEqual(info["CFBundleExecutable"], "本地数据库")
            self.assertFalse(config["web_server_used"])
            self.assertEqual(config["configuration_state"], "first_run_clean")
            self.assertEqual(config["database"], "")
            self.assertEqual(config["output_root"], "")
            self.assertEqual(config["visible_media_types"], ["image", "video"])
            self.assertEqual(config["hidden_media_interfaces"], ["audio", "text"])
            self.assertTrue(Path(config["runtime_contract_path"]).is_file())
            self.assertNotIn("qwen_python", config)
            self.assertNotIn("qwen_model", config)
            self.assertEqual(config["appearance_policy"], "native_swiftui_system_appearance_v1")
            self.assertEqual(config["runtime_policy"], "native_swiftui_embedded_python_bridge_v1")
            self.assertEqual(config["author"], "Horizon-94")
            self.assertEqual(config["official_source"], "https://github.com/Horizon-94/local-media-organizer")
            self.assertEqual(config["license"], "GPL-3.0-only")
            self.assertTrue(info["NSRequiresAquaSystemAppearance"])
            self.assertEqual(info["NSHumanReadableCopyright"], "Copyright © 2026 Horizon-94")
            self.assertTrue((bundle / "Contents/Frameworks/Python3.framework").is_dir())
            self.assertTrue((bundle / "Contents/Resources/AppIcon.icns").is_file())
            self.assertTrue((bundle / "Contents/Resources/app_icon_1024.png").is_file())
            self.assertEqual(info["CFBundleIconFile"], "AppIcon.icns")
            self.assertTrue(helper.is_file())
            self.assertIn("native_bridge", bridge.read_text(encoding="utf-8"))
            bundled_bridge = (
                bundle / "Contents/Resources/media_archive_image_video_ui/native_bridge.py"
            ).read_text(encoding="utf-8")
            bundled_frontend = (
                bundle / "Contents/Resources/media_archive_image_video_ui/native_frontend.swift"
            ).read_text(encoding="utf-8")
            self.assertIn("media_archive_search_result_v1", bundled_bridge)
            self.assertIn("result_items", bundled_bridge)
            self.assertIn("struct SearchResultCard", bundled_frontend)
            self.assertIn("resultItems", bundled_frontend)
            self.assertIn('Button("进入搜索素材")', bundled_frontend)
            self.assertIn("综合匹配仅用于结果排序，不是识别概率", bundled_frontend)
            self.assertIn("物体标签证据：", bundled_frontend)
            self.assertIn("本次已扫描：画面向量", bundled_frontend)
            self.assertIn('Button("查看同一人物")', bundled_frontend)
            self.assertIn("总用时", bundled_frontend)
            self.assertIn("Copyright © 2026 Horizon-94", bundled_frontend)
            self.assertIn("github.com/Horizon-94/local-media-organizer", bundled_frontend)
            self.assertIn("AVPlayerView", bundled_frontend)
            self.assertNotIn("QuickTime Player", bundled_frontend)
            self.assertNotIn("/usr/bin/osascript", bundled_frontend)
            self.assertIn(executable.read_bytes()[:4], {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"})

    def test_evidence_bundle_excludes_models_database_media_and_test_output(self) -> None:
        builder_path = ROOT / "scripts" / "04_media_archive_app" / "build_frozen_v1_evidence_bundle.py"
        spec = importlib.util.spec_from_file_location("evidence_builder", builder_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "evidence.zip"
            result = module.build_bundle(
                ROOT, ROOT / "docs/pipeline_rules/FROZEN_INTERFACE_BUNDLE_FILES_V3.txt", output,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["models_included"])
            self.assertFalse(result["database_included"])
            self.assertFalse(result["original_media_included"])
            self.assertFalse(result["test_output_included"])
            self.assertTrue(output.is_file())

    def test_native_app_has_finder_without_quicktime_automation_or_web_server(self) -> None:
        source = (APPS / "media_archive_image_video_ui/native_app.py").read_text(encoding="utf-8")
        self.assertIn('"/usr/bin/open", "-R"', source)
        self.assertNotIn("QuickTime Player", source)
        self.assertNotIn("/usr/bin/osascript", source)
        self.assertNotIn("http.server", source)
        self.assertNotIn("ThreadingHTTPServer", source)

    def test_hardware_report_excludes_device_identifiers(self) -> None:
        hardware = detect_hardware()
        self.assertGreaterEqual(hardware["cpu_cores_total"], 1)
        self.assertNotIn("serial_number", hardware)
        self.assertNotIn("platform_UUID", hardware)
        self.assertNotIn("provisioning_UDID", hardware)
        self.assertGreaterEqual(hardware["recommendation"]["model_workers"], 1)
        self.assertGreaterEqual(
            hardware["recommendation"]["estimated_max_model_workers"],
            hardware["recommendation"]["model_workers"],
        )

    def test_worker_recommendation_adapts_to_unified_memory(self) -> None:
        def recommended(memory: int) -> dict[str, int]:
            return recommend_workers({
                "cpu_cores_total": 16,
                "unified_memory_gb": memory,
            })

        self.assertEqual(recommended(16)["model_workers"], 1)
        self.assertEqual(recommended(24)["model_workers"], 2)
        self.assertEqual(recommended(32)["model_workers"], 3)
        self.assertEqual(recommended(32)["estimated_max_model_workers"], 4)
        self.assertEqual(recommended(64)["model_workers"], 4)
        self.assertEqual(recommended(64)["estimated_max_model_workers"], 8)

    def test_processing_profile_activates_generic_interval_and_density(self) -> None:
        hardware = {
            "chip": "test", "cpu_cores_total": 10, "gpu_cores": 32,
            "unified_memory_gb": 32, "recommendation": {"model_workers": 3},
        }
        profile = build_processing_profile(
            hardware,
            scheduler_mode="pipeline_async",
            model_workers=3,
            frame_extract_workers=4,
            video_frame_interval_seconds=2,
            high_value_mode="target_20",
            image_scope="all_images",
        )
        self.assertTrue(profile["scheduler"]["event_driven_database_handoff"])
        self.assertFalse(profile["video_sampling"]["requires_new_generic_step02_contract_when_changed"])
        self.assertTrue(profile["video_sampling"]["effective_in_current_pipeline"])
        self.assertEqual(
            profile["video_sampling"]["supported_intervals_seconds"],
            [1.0, 2.0, 3.0, 4.0, 5.0],
        )
        self.assertTrue(profile["high_value_policy"]["current_frozen_v25_unchanged"])
        self.assertFalse(profile["high_value_policy"]["requires_new_candidate_policy_version"])
        self.assertTrue(profile["high_value_policy"]["density_effective_in_current_pipeline"])
        self.assertTrue(profile["high_value_policy"]["image_scope_effective_in_current_pipeline"])
        self.assertEqual(profile["high_value_policy"]["target_ratio"], 0.20)
        self.assertEqual(profile["activation_status"], "READY_FOR_GENERIC_PIPELINE")
        with tempfile.TemporaryDirectory() as temp:
            output = save_processing_profile(Path(temp), profile)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["profile_id"], profile["profile_id"])

    def test_native_settings_offer_executable_generic_contracts(self) -> None:
        source = (ROOT / "apps/media_archive_image_video_ui/native_frontend.swift").read_text(
            encoding="utf-8"
        )
        settings = source[source.index('SettingPicker(title: "视频抽帧间隔"'):]
        settings = settings[:settings.index("保存为今后任务的默认方案")]
        self.assertIn('values: ["1 秒", "2 秒", "3 秒", "4 秒", "5 秒"]', settings)
        self.assertNotIn('"10 秒"', settings)
        self.assertIn('"目标 15%"', settings)
        self.assertIn('"目标 20%"', settings)
        self.assertIn('"目标 30%"', settings)
        self.assertIn("所有普通图片都进入画面描述", settings)
        self.assertIn("state.savedProfile", source)
        self.assertIn('"all_images"', source)
        self.assertIn("已保存方案会显示在上方", source)

    def test_version_is_image_video_native_release(self) -> None:
        self.assertEqual(APP_VERSION, "1.1.4-search-progress-warm-cache")

    def test_release_builder_supports_portable_runtimes_without_models(self) -> None:
        source = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn("--portable-runtimes", source)
        self.assertIn("portable_runtime_manifest.json", source)
        self.assertIn('"models_included": False', source)
        self.assertIn("PipelinePython", source)
        self.assertIn("PipelineEnvs", source)
        self.assertIn('"$APP_RESOURCES/Pipeline"', source)
        self.assertIn("bundle_release_documents", source)
        self.assertIn("LICENSE-GPL-3.0.txt", source)
        self.assertIn('project_root / "docs" / "pipeline_rules"', source)
        self.assertIn('pipeline_root / "docs" / "pipeline_rules"', source)

    def test_portable_metadata_scrubs_local_editable_install_paths(self) -> None:
        builder_path = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        )
        specification = importlib.util.spec_from_file_location(
            "native_release_metadata_builder", builder_path
        )
        builder = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            site_packages = Path(temporary)
            (site_packages / "editable.pth").write_text(
                "/Users/" + "alice/private-checkout\nimport site\n",
                encoding="utf-8",
            )
            metadata = site_packages / "package.dist-info"
            metadata.mkdir()
            (metadata / "direct_url.json").write_text(
                '{"url":"file:///Users/' + 'alice/private-checkout"}',
                encoding="utf-8",
            )
            builder.sanitize_portable_python_metadata(site_packages)
            self.assertEqual(
                (site_packages / "editable.pth").read_text(encoding="utf-8"),
                "import site\n",
            )
            self.assertFalse((metadata / "direct_url.json").exists())

    def test_release_signing_does_not_sign_shell_wrappers_with_deep_mode(self) -> None:
        source = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        ).read_text(encoding="utf-8")
        sign_bundle = source[source.index("def sign_bundle"):source.index("def build_pkg")]
        self.assertNotIn('"--deep"', sign_bundle)
        self.assertIn("_is_macho", sign_bundle)
        self.assertIn("path != main_executable", sign_bundle)
        self.assertIn("_codesign(bundle)", sign_bundle)

    def test_portable_python_role_launcher_is_native_macho(self) -> None:
        builder_path = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        )
        specification = importlib.util.spec_from_file_location(
            "native_release_builder", builder_path
        )
        builder = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "person_reid"
            builder._write_pipeline_wrapper(
                launcher,
                "PipelinePython312/Python.framework",
                "3.12",
                "visual",
            )
            self.assertIn(launcher.read_bytes()[:4], builder.MACHO_MAGICS)

    def test_pkg_builder_uses_absolute_applications_payload_without_relocation(self) -> None:
        source = (
            ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"
        ).read_text(encoding="utf-8")
        build_pkg = source[source.index("def build_pkg"):source.index("def build_dmg")]
        self.assertIn('"Applications"', build_pkg)
        self.assertIn('"--root"', build_pkg)
        self.assertIn('"--install-location", "/"', build_pkg)
        self.assertNotIn('"--component"', build_pkg)


if __name__ == "__main__":
    unittest.main()
