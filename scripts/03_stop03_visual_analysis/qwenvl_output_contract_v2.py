#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen-VL output contract helper v2

Purpose:
- For future Qwen-VL runners, do NOT write full stdout into qwen_text.
- Save clean assistant text, raw stdout, and runtime metrics separately.
- Provide a shorter prompt and larger recommended generation budget to reduce truncation.

This file does not download models, does not call network, does not read original media.
It is a helper module plus selftest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

CONTRACT_VERSION = "qwenvl_output_contract_v2.0"
RECOMMENDED_MAX_TOKENS = 384
RECOMMENDED_TEMPERATURE = 0.0
RECOMMENDED_TOP_P = 1.0


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def build_qwenvl_prompt_v2() -> str:
    """A shorter prompt designed to finish within ~300-384 output tokens."""
    return (
        "只输出中文画面分析正文，不要输出文件路径、Prompt、assistant、token、速度、显存、代码块。\n"
        "按固定结构输出，内容要短而完整：\n"
        "1）概括：一句话说明画面。\n"
        "2）元素：人物、物体、场景、动作、环境、文字区域。没有就写“无”。\n"
        "3）检索价值：说明适合用哪些关键词检索，以及为什么有素材价值。\n"
        "只基于画面可见信息，不要编造。"
    )


_METRIC_PATTERNS = {
    "prompt_tokens": re.compile(r"Prompt:\s*([0-9]+)\s*tokens", re.I),
    "prompt_tokens_per_sec": re.compile(r"Prompt:\s*[0-9]+\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", re.I),
    "generation_tokens": re.compile(r"Generation:\s*([0-9]+)\s*tokens", re.I),
    "generation_tokens_per_sec": re.compile(r"Generation:\s*[0-9]+\s*tokens,\s*([0-9.]+)\s*tokens-per-sec", re.I),
    "peak_memory_gb": re.compile(r"Peak memory:\s*([0-9.]+)\s*GB", re.I),
}


def extract_runtime_metrics(raw_stdout: str) -> Dict[str, Any]:
    raw = raw_stdout or ""
    metrics: Dict[str, Any] = {}
    for key, pat in _METRIC_PATTERNS.items():
        m = pat.search(raw)
        if not m:
            metrics[key] = None
            continue
        val = m.group(1)
        if key.endswith("tokens"):
            try:
                metrics[key] = int(val)
            except ValueError:
                metrics[key] = None
        else:
            try:
                metrics[key] = float(val)
            except ValueError:
                metrics[key] = None
    return metrics


def _strip_before_assistant(text: str) -> str:
    markers = [
        "<|im_start|>assistant",
        "assistant\n",
        "Assistant:\n",
        "ASSISTANT:\n",
    ]
    for marker in markers:
        if marker in text:
            return text.split(marker, 1)[1]
    # If no assistant marker, remove known leading blocks cautiously.
    text = re.sub(r"^=+\s*\nFiles:\s*\[.*?\]\s*\n+", "", text, flags=re.S)
    text = re.sub(r"^Files:\s*\[.*?\]\s*\n+", "", text, flags=re.S)
    return text


def extract_clean_assistant_text(raw_stdout: str) -> str:
    """Extract only the semantic assistant answer from common mlx-vlm stdout wrappers."""
    text = (raw_stdout or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_before_assistant(text)

    # Remove trailing chat/template end markers.
    for marker in ["<|im_end|>", "<|endoftext|>"]:
        if marker in text:
            text = text.split(marker, 1)[0]

    # Remove trailing runtime section. In mlx-vlm output it is often after a separator.
    sep_positions = []
    for sep in ["\n==========\n", "\n==========", "==========\n"]:
        pos = text.find(sep)
        if pos >= 0:
            sep_positions.append(pos)
    if sep_positions:
        text = text[: min(sep_positions)]

    # Hard stop before metric lines if separator was absent.
    metric_line = re.search(r"\n(?:Prompt|Generation|Peak memory):\s*", text, flags=re.I)
    if metric_line:
        text = text[: metric_line.start()]

    # Drop any residual prompt/image tokens and internal paths.
    text = re.sub(r"<\|[^>]+\|>", "", text)
    text = re.sub(r"/Users/[^\s\]\)\}\u3002，,;；]+", "", text)
    text = re.sub(r"^\s*(Prompt|Files):.*$", "", text, flags=re.I | re.M)

    # Normalize whitespace but keep paragraph/list breaks.
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


_INCOMPLETE_ENDINGS = (
    "应为", "因为", "包括", "例如", "主要为", "可见", "背景为", "前景为", "疑似", "可能", "有", "和", "与", "及", "为",
    "：", "，", "、", "-", "（", "(", ";", "；",
)


def detect_text_issues(clean_text: str, metrics: Optional[Dict[str, Any]] = None, max_tokens: Optional[int] = None) -> Dict[str, Any]:
    text = clean_text or ""
    compact = re.sub(r"\s+", "", text)
    warnings = []
    status = "ok"

    if not compact:
        status = "failed"
        warnings.append("empty_clean_text")
    if "/Users/" in text or "Files:" in text or "Prompt:" in text or "Peak memory:" in text:
        status = "review"
        warnings.append("wrapper_or_internal_text_remains")
    if any(compact.endswith(x) for x in _INCOMPLETE_ENDINGS):
        status = "review" if status == "ok" else status
        warnings.append("likely_truncated_by_sentence_tail")
    if len(compact) < 80:
        status = "review" if status == "ok" else status
        warnings.append("too_short")

    metrics = metrics or {}
    gen_tokens = metrics.get("generation_tokens")
    if max_tokens is not None and isinstance(gen_tokens, int) and gen_tokens >= max_tokens:
        status = "review" if status == "ok" else status
        warnings.append("generation_reached_max_tokens")

    return {
        "cleanup_status": status,
        "cleanup_warnings": "|".join(warnings),
        "clean_text_len": len(text),
        "clean_text_compact_len": len(compact),
    }


@dataclass
class QwenVLOutputContractRow:
    contract_version: str
    evidence_id: str
    qwen_text: str
    qwen_text_sha256: str
    qwen_raw_stdout_path: str
    qwen_raw_stdout_sha256: str
    qwen_runtime_metrics_path: str
    qwen_runtime_metrics_json: str
    qwen_cleanup_status: str
    qwen_cleanup_warnings: str
    recommended_max_tokens: int


def write_qwenvl_contract_outputs(
    *,
    evidence_id: str,
    raw_stdout: str,
    out_dir: Path | str,
    max_tokens: int = RECOMMENDED_MAX_TOKENS,
) -> Dict[str, Any]:
    """Write clean text, raw stdout, metrics JSON. Return row fields for manifest."""
    out_dir = Path(out_dir)
    clean_dir = out_dir / "qwenvl_clean_text"
    raw_dir = out_dir / "qwenvl_raw_stdout"
    metrics_dir = out_dir / "qwenvl_runtime_metrics"
    clean_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", evidence_id)[:160] or "unknown"
    raw_path = raw_dir / f"{safe_id}.stdout.txt"
    clean_path = clean_dir / f"{safe_id}.txt"
    metrics_path = metrics_dir / f"{safe_id}.json"

    raw_stdout = raw_stdout or ""
    clean_text = extract_clean_assistant_text(raw_stdout)
    metrics = extract_runtime_metrics(raw_stdout)
    issues = detect_text_issues(clean_text, metrics, max_tokens=max_tokens)

    raw_path.write_text(raw_stdout, encoding="utf-8")
    clean_path.write_text(clean_text, encoding="utf-8")
    metrics_payload = {
        "contract_version": CONTRACT_VERSION,
        "evidence_id": evidence_id,
        "metrics": metrics,
        "text_issues": issues,
        "recommended_generation": {
            "max_tokens": RECOMMENDED_MAX_TOKENS,
            "temperature": RECOMMENDED_TEMPERATURE,
            "top_p": RECOMMENDED_TOP_P,
        },
    }
    metrics_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    row = QwenVLOutputContractRow(
        contract_version=CONTRACT_VERSION,
        evidence_id=evidence_id,
        qwen_text=clean_text,
        qwen_text_sha256=sha256_text(clean_text),
        qwen_raw_stdout_path=str(raw_path),
        qwen_raw_stdout_sha256=sha256_text(raw_stdout),
        qwen_runtime_metrics_path=str(metrics_path),
        qwen_runtime_metrics_json=json.dumps(metrics, ensure_ascii=False, sort_keys=True),
        qwen_cleanup_status=issues["cleanup_status"],
        qwen_cleanup_warnings=issues["cleanup_warnings"],
        recommended_max_tokens=RECOMMENDED_MAX_TOKENS,
    )
    # Also expose clean text path for convenience.
    d = asdict(row)
    d["qwen_clean_text_path"] = str(clean_path)
    return d


def selftest() -> int:
    sample = """==========
Files: ['/Users/yourname/test/frame.jpg']

Prompt: <|im_start|>user
<|vision_start|><|image_pad|><|vision_end|>xxx<|im_end|>
<|im_start|>assistant

1）概括：一辆红色列车经过城市高架桥。

2）元素：人物：无；物体：红色列车、高架桥、汽车；场景：城市道路；动作：列车行驶；环境：晴朗；文字区域：无。

3）检索价值：适合检索“轻轨、城市道路、高架桥、车辆”。
==========
Prompt: 948 tokens, 99.218 tokens-per-sec
Generation: 96 tokens, 29.674 tokens-per-sec
Peak memory: 4.241 GB
"""
    clean = extract_clean_assistant_text(sample)
    metrics = extract_runtime_metrics(sample)
    issues = detect_text_issues(clean, metrics, max_tokens=384)
    assert "Files" not in clean
    assert "Prompt" not in clean
    assert "/Users" not in clean
    assert "红色列车" in clean
    assert metrics["generation_tokens"] == 96
    assert issues["cleanup_status"] == "ok", issues
    print("SELFTEST_PASS")
    print(json.dumps({"clean_text": clean, "metrics": metrics, "issues": issues}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="selftest", choices=["selftest", "print_prompt"])
    args = ap.parse_args()
    if args.command == "selftest":
        return selftest()
    if args.command == "print_prompt":
        print(build_qwenvl_prompt_v2())
        print(f"\nRECOMMENDED_MAX_TOKENS={RECOMMENDED_MAX_TOKENS}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
