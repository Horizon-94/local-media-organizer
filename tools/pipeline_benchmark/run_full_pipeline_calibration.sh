#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tools/pipeline_benchmark/run_full_pipeline_calibration.py" "$@"
