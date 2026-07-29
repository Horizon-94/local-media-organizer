#!/usr/bin/env python3
"""Desktop adapter for the frozen Stop03-5E hybrid search implementation."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


ADAPTER_VERSION = "stop03_5e_hybrid_search_app_adapter_v1"


def python_runtime_environment_keys(environment: dict[str, str]) -> list[str]:
    return sorted(
        key for key in environment
        if key.startswith("PYTHON") or key in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}
    )


def minimal_openclip_environment(environment: dict[str, str]) -> dict[str, str]:
    allowed = {
        key: environment[key] for key in (
            "HOME", "TMPDIR", "LANG", "LC_ALL", "PATH",
            "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME",
            "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
            "TOKENIZERS_PARALLELISM", "DEVELOPER_DIR",
        ) if environment.get(key)
    }
    allowed.setdefault("PATH", "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    allowed.setdefault("LANG", "en_US.UTF-8")
    allowed.update({
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
    })
    return allowed


def explicit_venv_runtime(python_path: Path) -> tuple[Path, Path]:
    requested_path = python_path.expanduser().absolute()
    requested = requested_path.resolve(strict=True)
    venv_root = requested_path.parent.parent
    site_packages = sorted(venv_root.glob("lib/python*/site-packages"))
    if len(site_packages) == 1 and site_packages[0].is_dir():
        return requested, site_packages[0].resolve(strict=True)

    if requested_path.parent.name == "PipelinePython":
        contents = requested_path.parent.parent.parent
        portable_site_packages = (
            contents
            / "Resources"
            / "PipelineEnvs"
            / requested_path.name
            / "site-packages"
        )
        if portable_site_packages.is_dir():
            return requested, portable_site_packages.resolve(strict=True)

    raise RuntimeError(f"openclip_runtime_site_packages_not_found:{requested_path}")


def load_frozen_search(project_root: Path) -> Any:
    script = (
        project_root
        / "scripts/03_stop03_visual_analysis/stop03_5e_hybrid_visual_text_search_v2.py"
    )
    module_name = "stop03_5e_hybrid_visual_text_search_v2"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen_search_import_spec_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    frozen = load_frozen_search(Path(__file__).resolve().parents[2])
    original_execute_query = frozen.execute_query

    def clean_nested_openclip(
        python_path: Path, model_name: str, model_path: str, query: str, device: str,
    ) -> tuple[list[float], dict[str, Any]]:
        # The frozen search first tries its short-lived in-memory worker.  The
        # explicit bootstrap below remains as a compatibility fallback for an
        # unusual embedded-Python environment that cannot launch that worker.
        worker_path_error = ""
        try:
            values, runtime = frozen.real_openclip_embedder(
                python_path, model_name, model_path, query, device,
            )
            return values, {
                **runtime,
                "openclip_runtime_adapter": ADAPTER_VERSION,
            }
        except Exception as exc:
            worker_path_error = str(exc)[-1000:]
        payload = {
            "model_name": model_name, "model_path": str(model_path),
            "query": str(query), "device": device,
        }
        started = time.monotonic()
        executable, site_packages = explicit_venv_runtime(Path(python_path))
        child_environment = minimal_openclip_environment(dict(os.environ))
        frozen_script = str(Path(frozen.__file__).resolve())
        bootstrap = (
            "import importlib.util,runpy,sys;"
            "site_packages,script=sys.argv[1],sys.argv[2];"
            "sys.path.insert(0,site_packages);"
            "sys.stderr.write('APP_OPENCLIP_RUNTIME executable='+sys.executable+"
            "' prefix='+sys.prefix+' site='+site_packages+"
            "' torch_spec='+str(importlib.util.find_spec('torch'))+'\\n');"
            "sys.argv=[script,'--internal-openclip-text-embed'];"
            "runpy.run_path(script,run_name='__main__')"
        )
        completed = subprocess.run(
            [str(executable), "-c", bootstrap, str(site_packages), frozen_script],
            input=json.dumps(payload, ensure_ascii=False), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            env=child_environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "app_openclip_query_failed:" + completed.stderr[-2000:]
            )
        lines = [
            line for line in completed.stdout.splitlines()
            if line.startswith("OPENCLIP_QUERY_JSON=")
        ]
        if len(lines) != 1:
            raise RuntimeError("app_openclip_query_output_invalid")
        result = json.loads(lines[0].split("=", 1)[1])
        return [float(value) for value in result["vector"]], {
            "device": result["device"],
            "openclip_model_load_seconds": result["model_load_seconds"],
            "openclip_query_embedding_seconds": result["query_embedding_seconds"],
            "openclip_subprocess_seconds": time.monotonic() - started,
            "openclip_runtime_adapter": ADAPTER_VERSION,
            "openclip_runtime_site_packages": str(site_packages),
            "openclip_worker_path_error": worker_path_error,
        }

    def execute_query_with_clean_nested_runtime(
        db: Any, config_path: Any, out: Any, args: Any,
        visual_embedder: Any = None, text_embedder: Any = None,
    ) -> Any:
        return original_execute_query(
            db, config_path, out, args,
            visual_embedder=clean_nested_openclip,
            text_embedder=text_embedder or frozen.real_text_embedder,
        )

    frozen.execute_query = execute_query_with_clean_nested_runtime
    return int(frozen.main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
