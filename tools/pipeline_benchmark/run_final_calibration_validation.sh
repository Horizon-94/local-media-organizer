#!/usr/bin/env bash
set -uo pipefail
TASK_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_OUT="${FINAL_CALIBRATION_ROOT:-/Users/horizon/Documents/AI-Local/test-output/final-calibration-validation-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$TASK_OUT"
echo "Validation output: $TASK_OUT"
echo $$ > "$TASK_OUT/validation.pid"
TASK_FAILED=0
for TASK_CASE in ffmpeg-stage03-x1 ffmpeg-stage03-x2 ffmpeg-stage03-x3 ffmpeg-stage03-x4 ffmpeg-stage03-x5 whisper-cold-x1 whisper-resident-x1 yoloe-x2 yoloe+openclip qwen+openclip+ocr; do
  python3 "$TASK_REPO/tools/pipeline_benchmark/run_full_pipeline_calibration.py" --root "$TASK_OUT" --case "$TASK_CASE" 2>&1 | tee -a "$TASK_OUT/validation.log"
  TASK_EXIT=${PIPESTATUS[0]}
  if [[ "$TASK_EXIT" != 0 ]]; then TASK_FAILED=1; fi
done
python3 "$TASK_REPO/tools/pipeline_benchmark/audit_calibration_validity.py" --root "$TASK_OUT" --out "$TASK_OUT/audit" 2>&1 | tee -a "$TASK_OUT/validation.log"
echo "$TASK_FAILED" > "$TASK_OUT/validation.exit"
echo "Targeted validation finished; audit=$TASK_OUT/audit/validity-audit.md"
echo "Full Campaign and production budgets require review of actual overlap evidence."
exit "$TASK_FAILED"
