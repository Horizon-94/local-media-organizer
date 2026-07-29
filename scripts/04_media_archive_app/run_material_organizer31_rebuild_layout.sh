#!/bin/zsh
set -eu

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "用法: $0 <已有素材库的 task.json> [素材大整理31.app]" >&2
  exit 64
fi

TASK_PATH="$1"
APP_PATH="${2:-/Users/yourname/Documents/AI-Local/media-archive-clean/dist/v1.0.31-rebuild-timelapse-fix2/素材大整理31.app}"
HELPER="$APP_PATH/Contents/Helpers/素材大整理Python"
CONFIG="$APP_PATH/Contents/Resources/app_config.json"
PROJECT_ROOT="/Users/yourname/Documents/AI-Local/media-archive-clean"
LOG_DIR="$PROJECT_ROOT/logs"
STAMP="$(date +%Y%m%d_%H%M%S)"
START_LOG="$LOG_DIR/material_organizer31_rebuild_layout_${STAMP}.log"

[[ -f "$TASK_PATH" ]] || { echo "找不到素材库记录: $TASK_PATH" >&2; exit 66; }
[[ -x "$HELPER" ]] || { echo "找不到完整应用后端: $HELPER" >&2; exit 69; }
[[ -f "$CONFIG" ]] || { echo "找不到应用配置: $CONFIG" >&2; exit 69; }
mkdir -p "$LOG_DIR"

export PYTHONUNBUFFERED=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1

"$HELPER" --config "$CONFIG" start-existing-task \
  --task "$TASK_PATH" \
  --task-mode rebuild_search | tee "$START_LOG"

echo "启动记录: $START_LOG"
echo "此模式只重扫位置、复用/生成必要图片预览并替换延时摄影分组；不运行识别模型。"
echo "监测命令: python3 $PROJECT_ROOT/scripts/04_media_archive_app/monitor_material_organizer31_task.py"
