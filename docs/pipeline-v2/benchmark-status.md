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

The direct FFmpeg and model measurements are not long-duration concurrency
evidence.  No Qwen/Whisper cross-model safety conclusion is made from the
30-frame fixture.  The complete readonly fixture now contains 10 videos and 20
images and is the required source for sustained runs and Legacy-vs-V2 A/B.

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
