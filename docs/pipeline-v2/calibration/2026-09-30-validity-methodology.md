# Calibration validity review — 2026-09-30

## Scope

Installed 1.2.8 (128) production runtimes, stages, models and preprocessing remain unchanged. Changes apply only to calibration fixtures, observers, coordinator, parsers and reports. Original media, formal databases and model directories are not written. This is not a production scheduler rollout.

## Invalid historical interpretations

- FFmpeg x2–x5 with `--limit-new 1` were not multi-video scaling measurements. x1 remains a single-video resource observation, not the ten-video scaling baseline.
- Monitor total elapsed includes the post-exit observation window. Throughput must use actual process exit minus workload start, not total monitor time.
- Whisper's resident label was another fresh production process, not a load-once, multi-batch, idle-resident lifecycle. Both heavy models' unsupported resident cases are excluded from the executed denominator.
- Overlapping audio preparation or VAD tasks do not establish overlapping Whisper inference. A valid x2 fixture must contain at least two actual speech inputs and overlapping production `mlx_whisper.transcribe` calls.
- Independent constituent peak RSS values cannot be summed or maximized to obtain a composite peak. Historical composites without synchronized aggregate sampling cannot supply combined resource costs.
- Historical short-sample P95 calculations are superseded by empirical nearest-rank P95 recomputed from preserved raw samples.

## Current evidence requirements

Each PASS needs positive formal semantic output, expected processed counts and no hidden zero-work success. Completed fixtures are read-only audited for stable content fingerprints, rather than comparing mutated database file hashes or counts alone.

FFmpeg uses the same ten videos and extraction contract at every worker level: offset 2 seconds, interval 3 seconds, long edge 1280, VideoToolbox preferred with software fallback, JPEG quality 3. Native telemetry must show simultaneous FFmpeg processes. Per-video task durations are retained to distinguish dataset critical-path limits from machine-wide worker ceilings.

Model scaling uses production task timing or committed per-item execution intervals, not just worker-count arguments. YOLOE/OpenCLIP timing hooks observe existing production functions; Whisper hooks observe the existing MLX backend. They do not implement alternative inference.

Composite sampling tracks the shared coordinator and descendants on one clock. Statistics are trimmed to the actual constituent start/exit window, preserve individual metrics and report real process overlap. Process overlap is not proof that all models execute GPU kernels concurrently throughout the run.

## Measurement boundaries

- RSS is process memory, not independent GPU VRAM. MLX active/cache/peak allocator snapshots are different measurements; do not add them to RSS.
- Apple Silicon uses unified memory. Whole-system used-memory estimates include reclaimable cache and are not Activity Monitor application-memory figures.
- `ps` CPU samples use approximately 100% per logical core. This machine has ten cores; normalized machine utilization divides workload CPU by ten. Samples are not exact CPU-time integration.
- One-second sampling can miss brief peaks. This especially affects very short Person/ReID runs.
- Disk counters currently provide combined whole-system I/O, not reliable per-workload read/write separation. Per-process GPU memory and optional GPU power/utilization remain N/A when not measured.
- Post-exit 10/30-second samples distinguish workload PID disappearance from system cache, compression and swap behavior. A zero process-tree RSS does not mean all system memory must return immediately to its original baseline.
- Fresh isolated databases and outputs prevent execution-history contamination. Successful prior fixtures are not reused as mutable run templates.

## What this permits

Validated cold x1 and homogeneous/composite samples support an observed resource matrix and benchmark-scoped experimental admission candidates. They do not establish sustained production-safe maximum concurrency, a globally optimal total-media schedule, a precise model-loading reserve, or an HDD ceiling.

Event-driven mixed soak remains a separate follow-up. Without a bound mechanical-disk fixture, HDD validation stays SKIPPED; SSD measurements must not be represented as HDD evidence. No production scheduler defaults change on the basis of these short tests.
