# Existing benchmark data audit

This report reads completed artifacts only; no workload was rerun.

## Complete metric table

| workload | wall | baseline system used | average system used | peak system used | RSS before | RSS avg | RSS peak | compressed before | compressed peak | compressed after | swap before | swap peak | swap after | swap Δ | CPU avg | CPU P95 | CPU peak | disk delta | throughput | exit | error |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| YOLOE ×1 | 8.512s | 29400.2 MiB | 30185.7 MiB | 32489.2 MiB | 0.9 MiB | 481.0 MiB | 1194.2 MiB | 2007.4 MiB | 2007.4 MiB | 2007.4 MiB | 1993.2 MiB | 1993.2 MiB | 1993.2 MiB | 0.0 MiB | 86.93 | 112.42 | 120.70 | 3.0 mib | 1.175 units/second | 0 | none (exit 0) |
| OpenCLIP ×1 | 16.993s | 29610.5 MiB | 30242.3 MiB | 32628.1 MiB | 0.6 MiB | 531.6 MiB | 1336.5 MiB | 2005.0 MiB | 2005.0 MiB | 2005.0 MiB | 1993.2 MiB | 1993.2 MiB | 1993.2 MiB | 0.0 MiB | 70.83 | 100.32 | 101.60 | 628.0 mib | 0.588 units/second | 0 | none (exit 0) |
| FFmpeg direct HEVC experiment | 150.750s | 32643.5 MiB | 32640.8 MiB | 32696.8 MiB | 0.4 MiB | 1141.2 MiB | 1170.6 MiB | 2010.9 MiB | 2010.9 MiB | 2009.5 MiB | 1993.2 MiB | 1993.2 MiB | 1993.2 MiB | 0.0 MiB | 468.51 | 537.81 | 544.20 | 14791.0 kib | N/A | 0 | none (exit 0) |
| FFmpeg formal Stage 03 | 546.850s | N/A | N/A | N/A | N/A | N/A | N/A | 1989.2 MiB | 2192.1 MiB | 2147.0 MiB | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | 0.307 units/second | 0 | none (exit 0) |

## Sampling validity

- **YOLOE ×1**: 9 samples; median interval 1.055s; range [1.054, 1.059]; process start/active/exit sampled; model-load boundary was not emitted as a separate event. Process RSS: legacy summary: ps tree sampled.
- **OpenCLIP ×1**: 17 samples; median interval 1.056s; range [1.048, 1.088]; process start/active/exit sampled; model-load boundary was not emitted as a separate event. Process RSS: legacy summary: ps tree sampled.
- **FFmpeg direct HEVC experiment**: 142 samples; median interval 1.068s; range [1.056, 1.141]; process start/active/exit sampled; model-load boundary was not emitted as a separate event. Process RSS: legacy summary: ps tree sampled.
- **FFmpeg formal Stage 03**: 527 samples; median interval 1.04s; range [1.015, 1.089]; process start/active/exit sampled; model-load boundary was not emitted as a separate event. Process RSS: N/A - sandbox process visibility unavailable.

## Interpretation limits

- System-used memory is calculated as `physical_memory - available_approx`; it is not a direct per-process allocation and the available value is derived from vm_stat free + speculative pages.
- Existing CSVs record an aggregate cumulative disk counter, not separate read/write bytes. Separate read/write deltas are therefore **N/A**.
- The old benchmark did not emit explicit T0–T6 model lifecycle markers; model-load boundary is **N/A**.
- Apple Silicon has unified memory; no separate GPU-memory number is claimed.
- The direct FFmpeg row is an experimental baseline; the formal Stage 03 row is correctness-valid at 546.85s / 168 frames but its sandbox resource fields are intentionally N/A.
