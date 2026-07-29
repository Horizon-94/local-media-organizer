#!/bin/zsh

set -u

ROOT="/Users/yourname/Documents/AI-Local/media-archive-clean"
OUT_DIR="$ROOT/dist/v1.1.2-full-runtime-final-20260728-r4"
LOG_FILE="$ROOT/logs/v1.1.2_full_runtime_final_r4.log"
EXIT_FILE="$ROOT/logs/v1.1.2_full_runtime_final_r4.exit_code"

cd "$ROOT" || exit 70
mkdir -p "$ROOT/logs" "$OUT_DIR"

python3 "$ROOT/scripts/04_media_archive_app/build_native_image_video_app_v1.py" \
  --output-dir "$OUT_DIR" \
  --python /Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python \
  --portable-runtimes \
  --dmg \
  > "$LOG_FILE" 2>&1

CODE=$?
printf '%s\n' "$CODE" > "$EXIT_FILE"
exit "$CODE"
