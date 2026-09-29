# Resource benchmark status (checkpoint continuation)

Hardware is Apple Silicon M1 Max with 32 GB unified memory. macOS does not
provide a reliable separate GPU-memory counter; reports therefore use process
RSS, compressed memory, swap, Metal errors, and output correctness.

## Current observations

| Workload | Fixture | Workers | Result | Peak RSS | Swap delta | Note |
|---|---|---:|---|---:|---:|---|
| FFmpeg direct VideoToolbox | one 4K HEVC source | 1 | PASS, experimental | 1.23 GB | 0 | not formal Stage 03 |
| Formal installed Stage 03 | same source, DB-backed | 1 | PASS, 168 frames | unavailable in sandbox | unavailable | exact 1.2.8 contract; 546.85 s |
| YOLOE | 10 derived frames | 1 | PASS | 1.25 GB | 0 | short model-load test only |
| OpenCLIP | 10 derived frames | 1 | PASS | 1.40 GB | 0 | short model-load test only |
| OCR | 5 derived frames | 1 | model completed, fixture acceptance failed | N/A | N/A | 5 no-text results; DB FK audit exposed fixture mismatch |

The direct FFmpeg and model measurements are not long-duration concurrency
evidence.  No Qwen/Whisper cross-model safety conclusion is made from the
30-frame fixture.  The complete readonly fixture now contains 10 videos and 20
images and is the required source for sustained runs and Legacy-vs-V2 A/B.

OCR was intentionally not promoted to a resource baseline: the installed OCR
model loaded and processed five items, but the isolated synthetic candidate
fixture failed its foreign-key acceptance audit.  The result is retained as a
fixture-contract failure, not as a claim about OCR stability or throughput.

Qwen3-VL formal entry was preflight-valid, but the actual worker could not
initialize in this Codex execution environment: MLX reported
`metal::load_device: No Metal device available` and the worker retired after
bounded retries.  This is an environment/GPU-access limitation, not a safe
Qwen concurrency result.  Qwen, Whisper, and cross-model Metal combinations
therefore remain N/A until the same commands can run in a host session with
Metal access.

## Safety interpretation

The formal Stage 03 correctness run produced all 168 expected outputs and exit
code 0.  Its independent monitor could not see child processes in the sandbox,
so resource peak values are intentionally blank.  The model rows are useful for
load/short-run sizing only; concurrency promotion requires sustained runs with
swap and failure monitoring.

## Remaining benchmark gates

1. Sustained same-model ×2/×3 runs, stopping on pressure, swap growth, OOM or
   Metal errors.
2. Cross-model combinations, especially Qwen + Whisper, only after the single
   model baselines are available.
3. Legacy vs Pipeline V2 on the complete 10-video/20-image fixture with result
   parity checks.

## Audit continuation

The completed YOLOE, OpenCLIP, and direct-FFmpeg CSVs have now been audited
without rerunning workloads. The full derived table (system-used memory,
process-tree RSS, compressed memory, swap, CPU average/P95/peak, sampling
interval, throughput, and explicit N/A fields) is in
`docs/pipeline-v2/audits/existing-benchmark-data-audit.md`.

The monitor self-check is in
`docs/pipeline-v2/audits/monitor-validation.md`. `vm_stat` and
`memory_pressure -Q` are observable in this shell, but sandbox policy denies
reliable `ps` process-tree visibility, swap usage, and iostat. Consequently
the monitor is **PARTIAL** here; host-terminal sampling is required before new
resource numbers can enter the formal matrix.

Qwen and Whisper are intentionally not retried in this environment. The host
entry point is `tools/pipeline_benchmark/run_host_benchmark.sh`; it probes both
installed MLX runtimes for Metal and only starts the isolated Qwen fixture when
`RUN_HEAVY=1` is explicitly set from an ordinary macOS Terminal. It never
writes the source media or model directory.
