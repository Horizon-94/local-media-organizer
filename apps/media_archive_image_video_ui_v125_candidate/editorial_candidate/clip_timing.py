"""One provisional cut range shared by the native UI, JSON and XML."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from .timeline_export import _frame_aligned_time, normalize_frame_rate


def cut_range(candidate: dict[str, Any], fallback: dict[str, Any] | None = None) -> tuple[int, int]:
    fallback = fallback or {}
    def value(keys: tuple[str, ...], default: int) -> int:
        for row in (candidate, fallback):
            for key in keys:
                if row.get(key) is not None:
                    return int(row[key])
        return default
    start = value(("provisional_in_ms", "clean_in_ms", "start_ms"), 0)
    end = value(("provisional_out_ms", "clean_out_ms", "end_ms"), start)
    if start < 0 or end <= start:
        raise ValueError("剪辑入出点无效：出点必须晚于入点，且入点不能小于零")
    return start, end


def timeline_duration_ms(start: int, end: int, rate: object) -> float:
    return float(_frame_aligned_time(end - start, normalize_frame_rate(rate), minimum_one_frame=True) * 1000)


def validate_source_range(start: int, end: int, timing: dict[str, Any], rate: object) -> None:
    source_rate = Fraction(str(timing["source_frame_rate"]))
    aligned_start = _frame_aligned_time(start, source_rate)
    aligned_duration = _frame_aligned_time(end - start, normalize_frame_rate(rate), minimum_one_frame=True)
    if aligned_start + aligned_duration > Fraction(str(timing["source_duration_seconds"])):
        raise ValueError("剪辑出点超过真实原片长度（含时间线帧对齐）；请缩短出点后重试")
