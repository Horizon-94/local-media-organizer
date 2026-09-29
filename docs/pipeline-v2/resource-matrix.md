# Resource matrix status

This is an interim, evidence-only matrix.  `N/A` means the workload was not
reliably measurable in the current execution environment; it is never treated
as a guessed safe value.

## Single workload

| Workload | Concurrency | Peak process RSS | Compressed memory | Swap | Throughput | Verdict |
|---|---:|---:|---:|---:|---:|---|
| Formal Stage 03 | 1 | N/A (sandbox process visibility) | sampled | N/A (sysctl restricted) | 168 frames / 546.85 s | SAFE for correctness; resource-limited |
| FFmpeg direct experimental | 1 | 1.23 GB | sampled | 0 | 168 frames / 150.75 s | SAFE experimental baseline |
| YOLOE | 1 | 1.25 GB | sampled | 0 | 10 frames / 8.512 s | SAFE short-run only |
| OpenCLIP | 1 | 1.40 GB | sampled | 0 | 10 frames / 16.993 s | SAFE short-run only |
| OCR | 1 | N/A | sampled | N/A | 5 attempts / 21.49 s | BORDERLINE/fixture failed |
| Qwen3-VL | 1 | N/A | sampled | N/A | 0 completed | N/A: no Metal device |
| Whisper | 1+ | N/A | N/A | N/A | N/A | N/A: not measurable yet |

## Concurrency and combinations

No ×2/×3 or cross-model result is promoted yet.  The Qwen worker failure occurs
before model load because MLX cannot access Metal in the current headless
execution context.  It would be unsafe to turn historical concurrency values
into a formal 32GB budget.

## Current scheduling decision

Pipeline V2 must remain on conservative fixed budgets until a Metal-capable host
run supplies Qwen/Whisper and combination measurements.  The only defensible
current rule is: one heavyweight model at a time, no cross-model launch, and no
automatic concurrency escalation.  This is a temporary safety hold, not the
final recommended budget.
