#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-3A Qwen-VL + OCR model/environment probe.

Read-only probe. It does not run Qwen-VL inference, OCR inference, or modify original media.
It checks:
- Python environment candidates.
- Import availability for likely Qwen-VL backends.
- Import availability for likely OCR backends.
- Model-root candidates under /Users/yourname/Documents/model.
- Existing Stop03-2 queue counts if provided.

Default behavior is import/path probe only. Heavy model loading is intentionally not done here.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional


QWENVL_IMPORTS = [
    "mlx_vlm",
    "mlx",
    "transformers",
    "torch",
    "PIL",
    "accelerate",
    "sentencepiece",
    "qwen_vl_utils",
]

OCR_IMPORTS = [
    "pytesseract",
    "paddleocr",
    "paddle",
    "cv2",
    "easyocr",
    "rapidocr_onnxruntime",
    "rapidocr_openvino",
    "onnxruntime",
    "cnocr",
    "PIL",
    "numpy",
]

BINARY_TOOLS = [
    "tesseract",
    "ocrmypdf",
    "magick",
    "convert",
]


def run_cmd(cmd: List[str], timeout: int = 30) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as e:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": repr(e),
        }


def probe_python(py: str, imports: List[str]) -> Dict[str, Any]:
    py_path = shutil.which(py) if not Path(py).exists() else py
    if not py_path:
        return {
            "python": py,
            "exists": False,
            "import_results": {},
        }

    code = r"""
import importlib, json, platform, sys
mods = __IMPORTS__
out = {
  "executable": sys.executable,
  "version": sys.version,
  "platform": platform.platform(),
  "import_results": {}
}
for m in mods:
    try:
        mod = importlib.import_module(m)
        ver = getattr(mod, "__version__", None)
        out["import_results"][m] = {"ok": True, "version": str(ver) if ver is not None else ""}
    except Exception as e:
        out["import_results"][m] = {"ok": False, "error": repr(e)}
print(json.dumps(out, ensure_ascii=False))
""".replace("__IMPORTS__", json.dumps(imports, ensure_ascii=False))

    res = run_cmd([py_path, "-c", code], timeout=45)
    parsed: Dict[str, Any] = {}
    if res["stdout"]:
        try:
            parsed = json.loads(res["stdout"].splitlines()[-1])
        except Exception:
            parsed = {"parse_error": True, "raw_stdout": res["stdout"]}

    return {
        "python": py,
        "resolved_python": py_path,
        "exists": True,
        "probe_ok": res["ok"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "parsed": parsed,
    }


def env_candidates(project_root: Path) -> List[str]:
    cands = [
        sys.executable,
        "python3",
        "/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python",
        "/Users/yourname/Documents/AI-Local/envs/media-archive-v06-yolo/bin/python",
        "/Users/yourname/Documents/AI-Local/envs/media-archive-v06-ocr/bin/python",
        "/Users/yourname/Documents/AI-Local/envs/media-archive-v06-qwenvl/bin/python",
        "/Users/yourname/Documents/AI-Local/envs/media-archive-v05/bin/python",
    ]

    # Also scan local envs if present.
    env_root = Path("/Users/yourname/Documents/AI-Local/envs")
    if env_root.exists():
        for p in sorted(env_root.glob("*/bin/python")):
            cands.append(str(p))

    # Deduplicate preserving order.
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def dir_size_limited(path: Path, max_files: int = 2000) -> Dict[str, Any]:
    total = 0
    count = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
                count += 1
                if count >= max_files:
                    return {"bytes_prefix": total, "file_count_prefix": count, "truncated": True}
    except Exception as e:
        return {"error": repr(e)}
    return {"bytes_prefix": total, "file_count_prefix": count, "truncated": False}


def find_model_candidates(model_root: Path) -> Dict[str, Any]:
    result = {
        "model_root": str(model_root),
        "exists": model_root.exists(),
        "qwenvl_candidates": [],
        "ocr_candidates": [],
        "other_relevant_candidates": [],
    }
    if not model_root.exists():
        return result

    for p in sorted(model_root.iterdir()):
        if not p.exists():
            continue
        name = p.name.lower()
        is_dir = p.is_dir()
        size_info = dir_size_limited(p) if is_dir else {"bytes": p.stat().st_size}
        entry = {
            "path": str(p),
            "name": p.name,
            "is_dir": is_dir,
            "size_info": size_info,
            "has_config_json": (p / "config.json").exists() if is_dir else False,
            "has_tokenizer": any((p / x).exists() for x in ["tokenizer.json", "tokenizer.model", "tokenizer_config.json"]) if is_dir else False,
            "safetensors_count": len(list(p.glob("*.safetensors"))) if is_dir else (1 if p.suffix == ".safetensors" else 0),
            "gguf_count": len(list(p.glob("*.gguf"))) if is_dir else (1 if p.suffix == ".gguf" else 0),
        }

        if "qwen" in name and ("vl" in name or "vision" in name):
            result["qwenvl_candidates"].append(entry)
        elif any(k in name for k in ["ocr", "paddle", "tesseract", "easyocr", "rapidocr", "cnocr"]):
            result["ocr_candidates"].append(entry)
        elif any(k in name for k in ["qwen", "clip", "openclip", "whisper", "embedding"]):
            result["other_relevant_candidates"].append(entry)

    return result


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return max(0, sum(1 for _ in csv.DictReader(f)))
    except Exception:
        return -1


def probe_stop03_2(stop03_2_base: Optional[Path]) -> Dict[str, Any]:
    if not stop03_2_base:
        return {"provided": False}
    result: Dict[str, Any] = {
        "provided": True,
        "path": str(stop03_2_base),
        "exists": stop03_2_base.exists(),
    }
    if not stop03_2_base.exists():
        return result

    qwen = sorted(stop03_2_base.glob("**/qwenvl_high_value_candidate_queue.csv"))
    ocr = sorted(stop03_2_base.glob("**/ocr_trigger_candidate_queue.csv"))
    decision = sorted(stop03_2_base.glob("**/visual_unit_candidate_decision_manifest.csv"))
    summary = sorted(stop03_2_base.glob("**/stop03_2_candidate_summary.json"))

    result.update({
        "qwen_queue_csv": str(qwen[0]) if qwen else "",
        "ocr_queue_csv": str(ocr[0]) if ocr else "",
        "decision_csv": str(decision[0]) if decision else "",
        "summary_json": str(summary[0]) if summary else "",
        "qwen_queue_rows": count_csv_rows(qwen[0]) if qwen else -1,
        "ocr_queue_rows": count_csv_rows(ocr[0]) if ocr else -1,
        "decision_rows": count_csv_rows(decision[0]) if decision else -1,
    })

    if summary:
        try:
            result["summary"] = json.loads(summary[0].read_text(encoding="utf-8"))
        except Exception as e:
            result["summary_error"] = repr(e)

    return result


def write_markdown(report: Dict[str, Any], path: Path) -> None:
    qwen_envs = report["python_probe"]["qwenvl"]
    ocr_envs = report["python_probe"]["ocr"]

    def summarize_envs(envs: List[Dict[str, Any]], wanted: List[str]) -> List[str]:
        lines = []
        for e in envs:
            py = e.get("resolved_python") or e.get("python")
            if not e.get("exists"):
                continue
            parsed = e.get("parsed") or {}
            imports = parsed.get("import_results") or {}
            ok = [m for m in wanted if imports.get(m, {}).get("ok")]
            lines.append(f"- `{py}`: ok_imports={ok}")
        return lines or ["- no usable python environment found"]

    model = report["model_candidates"]
    queues = report["stop03_2_queue_probe"]

    lines = [
        "# Stop03-3A Qwen-VL + OCR Environment Probe",
        "",
        f"- timestamp: {report['timestamp']}",
        f"- source_safety: {report['source_safety']}",
        f"- project_root: {report['project_root']}",
        f"- model_root: {report['model_root']}",
        "",
        "## Stop03-2 queue probe",
        f"- provided: {queues.get('provided')}",
        f"- exists: {queues.get('exists')}",
        f"- qwen_queue_rows: {queues.get('qwen_queue_rows')}",
        f"- ocr_queue_rows: {queues.get('ocr_queue_rows')}",
        f"- decision_rows: {queues.get('decision_rows')}",
        f"- qwen_queue_csv: {queues.get('qwen_queue_csv')}",
        f"- ocr_queue_csv: {queues.get('ocr_queue_csv')}",
        "",
        "## Qwen-VL model candidates",
        f"- count: {len(model.get('qwenvl_candidates', []))}",
    ]
    for c in model.get("qwenvl_candidates", [])[:20]:
        lines.append(f"- `{c['path']}` size={c.get('size_info')} config={c.get('has_config_json')} tokenizer={c.get('has_tokenizer')} safetensors={c.get('safetensors_count')} gguf={c.get('gguf_count')}")

    lines += [
        "",
        "## OCR model candidates",
        f"- count: {len(model.get('ocr_candidates', []))}",
    ]
    for c in model.get("ocr_candidates", [])[:20]:
        lines.append(f"- `{c['path']}` size={c.get('size_info')} config={c.get('has_config_json')} tokenizer={c.get('has_tokenizer')} safetensors={c.get('safetensors_count')} gguf={c.get('gguf_count')}")

    lines += [
        "",
        "## Qwen-VL import probe",
        *summarize_envs(qwen_envs, QWENVL_IMPORTS),
        "",
        "## OCR import probe",
        *summarize_envs(ocr_envs, OCR_IMPORTS),
        "",
        "## Binary tools",
    ]
    for name, info in report["binary_tools"].items():
        lines.append(f"- {name}: {info}")

    lines += [
        "",
        "## Interpretation",
        "- PASS condition for Qwen-VL env probe: at least one Python has the selected Qwen-VL backend importable, and a Qwen-VL model directory exists under model_root.",
        "- PASS condition for OCR env probe: at least one OCR backend or binary exists. If none exists, Stop03 OCR execution is BLOCKED until OCR runtime is installed or selected.",
        "- This probe intentionally does not run expensive model inference.",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/Users/yourname/Documents/AI-Local/media-archive-clean")
    ap.add_argument("--model-root", default="/Users/yourname/Documents/model")
    ap.add_argument("--run-root", default="")
    ap.add_argument("--stop03-2-base", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    project_root = Path(args.project_root)
    model_root = Path(args.model_root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    py_candidates = env_candidates(project_root)
    qwen_probes = [probe_python(py, QWENVL_IMPORTS) for py in py_candidates]
    ocr_probes = [probe_python(py, OCR_IMPORTS) for py in py_candidates]

    binary_tools = {}
    for b in BINARY_TOOLS:
        path = shutil.which(b)
        binary_tools[b] = {
            "found": bool(path),
            "path": path or "",
        }
        if path:
            res = run_cmd([path, "--version"], timeout=10)
            binary_tools[b]["version_stdout_first_line"] = (res.get("stdout") or res.get("stderr") or "").splitlines()[:1]

    report: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_safety": "read_only_no_model_inference_no_original_media_modification",
        "project_root": str(project_root),
        "model_root": str(model_root),
        "run_root": args.run_root,
        "python_candidates": py_candidates,
        "python_probe": {
            "qwenvl": qwen_probes,
            "ocr": ocr_probes,
        },
        "binary_tools": binary_tools,
        "model_candidates": find_model_candidates(model_root),
        "stop03_2_queue_probe": probe_stop03_2(Path(args.stop03_2_base) if args.stop03_2_base else None),
    }

    json_path = out_dir / "stop03_3a_qwenvl_ocr_env_probe.json"
    md_path = out_dir / "stop03_3a_qwenvl_ocr_env_probe.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print(json.dumps({
        "status": "DONE",
        "source_safety": report["source_safety"],
        "json": str(json_path),
        "md": str(md_path),
        "qwen_model_candidate_count": len(report["model_candidates"].get("qwenvl_candidates", [])),
        "ocr_model_candidate_count": len(report["model_candidates"].get("ocr_candidates", [])),
        "qwen_queue_rows": report["stop03_2_queue_probe"].get("qwen_queue_rows"),
        "ocr_queue_rows": report["stop03_2_queue_probe"].get("ocr_queue_rows"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
