#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$ROOT/tools/pipeline_benchmark/run_remaining_adapter_smokes.py" "$@"
