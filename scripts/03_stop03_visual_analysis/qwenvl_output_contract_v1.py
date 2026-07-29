#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen-VL Output Contract V1

Purpose:
  Make Qwen-VL runner write clean semantic text directly, instead of putting
  full stdout / prompt / file path / runtime metrics into qwen_text.

Safety:
  - No network
  - No model download
  - No original media modification
  - This file only provides prompt + parsing + output writing helpers.

Recommended contract:
  qwen_text                  = clean assistant text only
  qwen_raw_stdout_path        = saved raw stdout path for audit
  qwen_runtime_metrics_json   = saved runtime metrics json path
  qwen_text_sha256            = sha256(clean assistant text)
  qwen_raw_stdout_sha256      = sha256(raw stdout)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


QWENVL_CLEAN_PROMPT_V1 = """请只输出中文画面分析正文，不要输出文件路径、Prompt、系统信息、token统计、显存信息、代码块或解释说明。

按以下结构输出：

1）画面概括：
用一句话概括这张图片或视频关键帧。

2）可见元素：
- 人物：
- 物体：
- 场景：
- 动作：
- 环境：
- 文字区域：

3）检索价值：
说明这张图为什么可能适合素材检索。只基于画面中可见信息，不要编造看不见的信息。

要求：
- 不要输出“Files”
- 不要输出“Prompt”
- 不要输出“assistant”
- 不要输出 token / speed / memory
- 不要输出内部路径
- 不要编造不可见信息
- 如果某项没有，就写“无”
""".strip()


@dataclass
class QwenVlRuntimeMetrics:
    prompt_tokens: Optional[float] = None
    prompt_tokens_per_sec: Optional[float] = None
    generation_tokens: Optional[float] = None
    generation_tokens_per_sec: Optional[float] = None
    peak_memory_gb: Optional[float] = None


@dataclass
class QwenVlCleanContract:
    qwen_text: str
    qwen_text_sha256: str
    qwen_raw_stdout_sha256: str
    qwen_runtime_metrics: QwenVlRuntimeMetrics
    cleanup_status: str
    cleanup_warnings: List[str]


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def build_qwenvl_prompt() -> str:
    """Return the prompt that should be passed to Qwen-VL."""
    return QWENVL_CLEAN_PROMPT_V1


def extract_runtime_metrics(raw_stdout: str) -> QwenVlRuntimeMetrics:
    """Extract MLX/Qwen-VL runtime metrics from stdout when present."""
    raw = raw_stdout or ""
    metrics = QwenVlRuntimeMetrics()

    m = re.search(r"Prompt:\s*([0-9.]+)\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", raw, re.I)
    if m:
        metrics.prompt_tokens = float(m.group(1))
        metrics.prompt_tokens_per_sec = float(m.group(2))

    m = re.search(r"Generation:\s*([0-9.]+)\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", raw, re.I)
    if m:
        metrics.generation_tokens = float(m.group(1))
        metrics.generation_tokens_per_sec = float(m.group(2))

    m = re.search(r"Peak\s+memory:\s*([0-9.]+)\s*GB", raw, re.I)
    if m:
        metrics.peak_memory_gb = float(m.group(1))

    return metrics


def _strip_after_runtime_markers(text: str) -> str:
    cut_markers = [
        "\n==========\nPrompt:",
        "\nPrompt:",
        "\nGeneration:",
        "\nPeak memory:",
        "tokens-per-sec",
    ]
    out = text
    for marker in cut_markers:
        idx = out.find(marker)
        if idx >= 0:
            out = out[:idx]
    return out


def extract_assistant_clean_text(raw_stdout: str) -> Tuple[str, List[str]]:
    """
    Extract only the assistant's semantic description from Qwen-VL stdout.

    Handles old bad stdout like:
      ==========\nFiles: [...]\n\nPrompt: <|im_start|>user ... <|im_start|>assistant\n\nTEXT\n==========\nPrompt: ...

    Returns:
      (clean_text, warnings)
    """
    warnings: List[str] = []
    text = (raw_stdout or "").replace("\r\n", "\n").replace("\r", "\n")

    if not text.strip():
        return "", ["empty_raw_stdout"]

    # 1. Prefer content after assistant template marker.
    assistant_markers = [
        "<|im_start|>assistant",
        "<|assistant|>",
        "assistant\n",
        "Assistant:",
        "ASSISTANT:",
    ]
    found_marker = None
    for marker in assistant_markers:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx + len(marker):]
            found_marker = marker
            break
    if found_marker is None:
        warnings.append("assistant_marker_not_found_used_raw_stdout")

    # 2. Remove possible leading chat special tokens/newlines.
    text = text.replace("<|im_end|>", "\n")
    text = text.replace("<|endoftext|>", "\n")
    text = text.lstrip(" \n\t:：")

    # 3. Cut runtime metrics and trailing wrapper.
    text = _strip_after_runtime_markers(text)

    # 4. If the old wrapper still exists, remove Files / Prompt block before first meaningful numbered item.
    numbered_starts = ["1）", "1)", "一、", "画面概括", "### 画面概括"]
    positions = [text.find(s) for s in numbered_starts if text.find(s) >= 0]
    if positions:
        first = min(positions)
        if first > 0:
            prefix = text[:first]
            if "Files:" in prefix or "Prompt:" in prefix or "/Users/" in prefix or "<|vision_start|>" in prefix:
                text = text[first:]
                warnings.append("removed_leading_wrapper_before_content")

    # 5. Remove remaining obvious prompt/template noise line by line.
    clean_lines: List[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            clean_lines.append("")
            continue
        noise = False
        noise_patterns = [
            r"^=+$",
            r"^Files:\s*\[",
            r"^Prompt:\s*<\|",
            r"^<\|.*\|>$",
            r"^/Users/",
            r"^Prompt:\s*[0-9.]+\s*tokens",
            r"^Generation:\s*[0-9.]+\s*tokens",
            r"^Peak memory:\s*[0-9.]+\s*GB",
        ]
        for pat in noise_patterns:
            if re.search(pat, s):
                noise = True
                break
        if not noise:
            clean_lines.append(line.rstrip())

    text = "\n".join(clean_lines).strip()

    # 6. Compact excessive blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # 7. Validate remaining noise.
    if "/Users/" in text:
        warnings.append("internal_path_remains")
    if "Files:" in text:
        warnings.append("files_marker_remains")
    if "Prompt:" in text or "Generation:" in text or "Peak memory:" in text:
        warnings.append("runtime_or_prompt_marker_remains")
    if "<|im_start|>" in text or "<|vision_start|>" in text or "<|image_pad|>" in text:
        warnings.append("chat_template_token_remains")
    if len(text) < 20:
        warnings.append("clean_text_too_short")

    return text, warnings


def make_qwenvl_clean_contract(raw_stdout: str) -> QwenVlCleanContract:
    clean_text, warnings = extract_assistant_clean_text(raw_stdout)
    status = "ok" if clean_text and not any(w.endswith("_remains") or w == "clean_text_too_short" for w in warnings) else "review"
    if not clean_text:
        status = "failed"
    return QwenVlCleanContract(
        qwen_text=clean_text,
        qwen_text_sha256=sha256_text(clean_text),
        qwen_raw_stdout_sha256=sha256_text(raw_stdout or ""),
        qwen_runtime_metrics=extract_runtime_metrics(raw_stdout or ""),
        cleanup_status=status,
        cleanup_warnings=warnings,
    )


def write_qwenvl_contract_outputs(
    *,
    evidence_id: str,
    raw_stdout: str,
    out_dir: Path,
) -> Dict[str, str]:
    """
    Write separated Qwen-VL outputs for one item.

    Creates:
      out_dir/qwenvl_text/{evidence_id}.txt
      out_dir/qwenvl_raw_stdout/{evidence_id}.txt
      out_dir/qwenvl_runtime_metrics/{evidence_id}.json

    Returns a flat dict suitable for CSV/JSONL manifest row.
    """
    out_dir = Path(out_dir)
    text_dir = out_dir / "qwenvl_text"
    raw_dir = out_dir / "qwenvl_raw_stdout"
    metrics_dir = out_dir / "qwenvl_runtime_metrics"
    for d in (text_dir, raw_dir, metrics_dir):
        d.mkdir(parents=True, exist_ok=True)

    contract = make_qwenvl_clean_contract(raw_stdout)

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", evidence_id or "unknown")[:180]
    text_path = text_dir / f"{safe_id}.txt"
    raw_path = raw_dir / f"{safe_id}.txt"
    metrics_path = metrics_dir / f"{safe_id}.json"

    text_path.write_text(contract.qwen_text, encoding="utf-8")
    raw_path.write_text(raw_stdout or "", encoding="utf-8")
    metrics_path.write_text(
        json.dumps(asdict(contract.qwen_runtime_metrics), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "evidence_id": evidence_id,
        "qwen_text": contract.qwen_text,
        "qwen_text_path": str(text_path),
        "qwen_text_sha256": contract.qwen_text_sha256,
        "qwen_raw_stdout_path": str(raw_path),
        "qwen_raw_stdout_sha256": contract.qwen_raw_stdout_sha256,
        "qwen_runtime_metrics_json": str(metrics_path),
        "qwen_text_cleanup_status": contract.cleanup_status,
        "qwen_text_cleanup_warnings": "|".join(contract.cleanup_warnings),
        "prompt_tokens": contract.qwen_runtime_metrics.prompt_tokens,
        "prompt_tokens_per_sec": contract.qwen_runtime_metrics.prompt_tokens_per_sec,
        "generation_tokens": contract.qwen_runtime_metrics.generation_tokens,
        "generation_tokens_per_sec": contract.qwen_runtime_metrics.generation_tokens_per_sec,
        "peak_memory_gb": contract.qwen_runtime_metrics.peak_memory_gb,
    }


def _cmd_prompt(args: argparse.Namespace) -> int:
    print(build_qwenvl_prompt())
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    raw = Path(args.raw_stdout).read_text(encoding="utf-8", errors="replace")
    contract = make_qwenvl_clean_contract(raw)
    print(json.dumps({
        "status": contract.cleanup_status,
        "warnings": contract.cleanup_warnings,
        "qwen_text_len": len(contract.qwen_text),
        "qwen_text_sha256": contract.qwen_text_sha256,
        "qwen_raw_stdout_sha256": contract.qwen_raw_stdout_sha256,
        "runtime_metrics": asdict(contract.qwen_runtime_metrics),
        "qwen_text": contract.qwen_text,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_write_one(args: argparse.Namespace) -> int:
    raw = Path(args.raw_stdout).read_text(encoding="utf-8", errors="replace")
    row = write_qwenvl_contract_outputs(
        evidence_id=args.evidence_id,
        raw_stdout=raw,
        out_dir=Path(args.out),
    )
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    sample = """==========
Files: ['/Users/yourname/test.jpg']

Prompt: <|im_start|>user
<|vision_start|><|image_pad|><|vision_end|>请分析图片<|im_end|>
<|im_start|>assistant

1）画面概括：一辆车停在路边。

2）可见元素：
- 人物：无
- 物体：汽车、道路
- 场景：城市道路
- 动作：无
- 环境：白天
- 文字区域：无

3）检索价值：适合检索车辆和城市道路素材。
==========
Prompt: 948 tokens, 99.218 tokens-per-sec
Generation: 180 tokens, 29.674 tokens-per-sec
Peak memory: 4.241 GB
"""
    c = make_qwenvl_clean_contract(sample)
    print(json.dumps({
        "status": c.cleanup_status,
        "warnings": c.cleanup_warnings,
        "runtime_metrics": asdict(c.qwen_runtime_metrics),
        "clean_text": c.qwen_text,
    }, ensure_ascii=False, indent=2))
    if "Files:" in c.qwen_text or "/Users/" in c.qwen_text or "Prompt:" in c.qwen_text:
        print("SELFTEST_FAIL: wrapper remains", file=sys.stderr)
        return 2
    if "1）画面概括" not in c.qwen_text:
        print("SELFTEST_FAIL: content missing", file=sys.stderr)
        return 3
    print("SELFTEST_PASS")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Qwen-VL clean output contract helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prompt", help="print clean Qwen-VL prompt")
    sp.set_defaults(func=_cmd_prompt)

    sp = sub.add_parser("extract", help="extract clean assistant text from one raw stdout file")
    sp.add_argument("--raw-stdout", required=True)
    sp.set_defaults(func=_cmd_extract)

    sp = sub.add_parser("write-one", help="write separated contract outputs for one raw stdout file")
    sp.add_argument("--raw-stdout", required=True)
    sp.add_argument("--evidence-id", required=True)
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=_cmd_write_one)

    sp = sub.add_parser("selftest", help="run built-in contract selftest")
    sp.set_defaults(func=_cmd_selftest)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
