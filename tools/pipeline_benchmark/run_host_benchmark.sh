#!/usr/bin/env bash
set -euo pipefail

# Run this from an ordinary macOS Terminal, not inside Codex's sandbox.
# It never writes the source media or model directory. Heavy runs use only the
# existing synthetic fixture and a fresh output directory.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/host-metal-benchmark-$(date +%Y%m%d-%H%M%S)}"
RUN_HEAVY="${RUN_HEAVY:-0}"
PREFLIGHT_ONLY="${PREFLIGHT_ONLY:-0}"
APP="/Applications/本地数据库 1.2.8.app"
PY_QWEN="$APP/Contents/Helpers/PipelinePython/qwen"
PY_WHISPER="$APP/Contents/Helpers/PipelinePython/whisper"
FIXTURE="${QWEN_DB:-/Users/horizon/Documents/AI-Local/test-output/qwen-host-fixture-v2/qwen_fixture.sqlite}"
QWEN_CONFIG="${QWEN_CONFIG:-/Users/horizon/Documents/AI-Local/test-output/qwen-host-fixture-v2/qwen-config.json}"
QWEN_ALLOWED_ROOT="${QWEN_ALLOWED_OUTPUT_ROOT:-/Users/horizon/Documents/AI-Local/test-output}"

mkdir -p "$OUT" "$OUT/logs"
exec > >(tee "$OUT/logs/host-benchmark.log") 2>&1

echo "HOST TERMINAL REQUIRED: $(date -Iseconds)"
echo "output=$OUT"
echo "root=$ROOT"
echo "allowed_output_root=$QWEN_ALLOWED_ROOT"
echo "fixture=$FIXTURE"
echo "resolved_fixture=$(cd "$(dirname "$FIXTURE")" && pwd)/$(basename "$FIXTURE")"
echo "resolved_output=$(cd "$OUT" && printf '%s/qwen-stage' "$(pwd)")"
echo "resolved_derived_root=/Users/horizon/Documents/AI-Local/test-output/qwen-host-fixture-v2/derived"
uname -a || true
sysctl -n hw.memsize hw.ncpu || true
echo "-- memory pressure --"
/usr/bin/memory_pressure -Q || true
echo "-- swap --"
/usr/sbin/sysctl -n vm.swapusage || true
echo "-- Metal / MLX runtime probes --"
"$PY_QWEN" -c 'import mlx.core as mx; print("qwen_default_device", mx.default_device())'
"$PY_WHISPER" -c 'import mlx.core as mx; print("whisper_default_device", mx.default_device())'

if [[ "$RUN_HEAVY" != "1" && "$PREFLIGHT_ONLY" != "1" ]]; then
  cat <<'EOF'
Preflight complete. No heavy inference was started.
To run the isolated Qwen x1 fixture benchmark after Metal probes pass:
  RUN_HEAVY=1 tools/pipeline_benchmark/run_host_benchmark.sh <fresh-output-dir>
The Whisper formal stage command is project-specific; this script intentionally
does not invent a different workload or write a formal database.
EOF
  exit 0
fi

if [[ ! -f "$FIXTURE" || ! -f "$QWEN_CONFIG" ]]; then
  echo "fixture_missing: $FIXTURE or $QWEN_CONFIG" >&2
  exit 2
fi

BENCH_OUT="$OUT/qwen-x1"
mkdir -p "$BENCH_OUT"
MODE="run"
if [[ "$PREFLIGHT_ONLY" == "1" ]]; then
  MODE="preflight"
fi
QWEN_COMMAND=(
  "$PY_QWEN"
  "$APP/Contents/Resources/Pipeline/scripts/04_media_archive_app/run_generic_pipeline_stage.py"
  --script "$APP/Contents/Resources/Pipeline/scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py"
  --allowed-output-root "$QWEN_ALLOWED_ROOT"
  --
  --mode "$MODE"
  --db "$FIXTURE"
  --config "$QWEN_CONFIG"
  --prompt "$APP/Contents/Resources/Pipeline/configs/qwenvl_prompt_v2_384.txt"
  --model "/Users/horizon/Documents/model/Qwen3-VL-4B-Instruct-4bit"
  --qwen-python "$PY_QWEN"
  --out "$OUT/qwen-stage"
  --workers 1
  --max-tokens 384
  --max-attempts 1
  --confirm-central-db-write
)
printf 'QWEN_COMMAND:'
printf ' %q' "${QWEN_COMMAND[@]}"
printf '\n'
"$PY_QWEN" "$ROOT/tools/pipeline_benchmark/run_benchmark.py" \
  --name "qwen-vl-x1-host-fixture-$MODE" \
  --output "$BENCH_OUT" \
  --interval 1 \
  --timeout 3600 \
  -- "${QWEN_COMMAND[@]}"

# Normalize the raw time series and formal-stage report into the shared
# benchmark contract.  This is post-processing only; it never reruns a model.
python3 "$ROOT/tools/pipeline_benchmark/summarize_host_benchmark.py" "$OUT"

if [[ "$MODE" == "preflight" ]]; then
  echo "Qwen host exact-run preflight completed; model was not loaded."
else
  echo "Qwen host x1 command completed; inspect the benchmark summary for model-run status."
fi
echo "Whisper remains a separate workload and must use its formal audio fixture/entry."
