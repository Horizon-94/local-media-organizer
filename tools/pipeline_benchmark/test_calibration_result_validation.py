#!/usr/bin/env python3
"""Regression checks for rejecting zero-work formal-stage exits."""
from pathlib import Path
import importlib.util


MODULE = Path(__file__).with_name("run_full_pipeline_calibration.py")
spec = importlib.util.spec_from_file_location("calibration", MODULE)
calibration = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(calibration)


def main() -> int:
    base = Path("/Users/horizon/Documents/AI-Local/test-output/pipeline-v2-calibration-20260930-073432/runs")
    # These are historical campaign artifacts: visual stages processed input,
    # while OCR and Stage03 exited cleanly without eligible work.
    assert calibration.formal_stage_result("yoloe", base / "yoloe-x1/adapter/yoloe/latest") == (True, None)
    assert calibration.formal_stage_result("openclip", base / "openclip-x1/adapter/openclip/latest") == (True, None)
    assert calibration.formal_stage_result("ocr", base / "ocr-x1/adapter/ocr/latest") == (False, "formal_stage_empty_candidates")
    assert calibration.formal_stage_result("ffmpeg", base / "ffmpeg-stage03-x1/adapter/ffmpeg/latest") == (False, "formal_stage_empty_video_queue")
    print("calibration_result_validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
