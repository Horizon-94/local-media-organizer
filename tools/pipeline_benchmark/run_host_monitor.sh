#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/host-monitor-$(date +%Y%m%d-%H%M%S)}"
INTERVAL="${INTERVAL:-1}"
exec "$ROOT/tools/pipeline_benchmark/host_resource_monitor.py" \
  --interval "$INTERVAL" --output-dir "$OUT" "${@:2}"
