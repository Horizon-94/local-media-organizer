# Pipeline benchmark tools

These tools build a bounded, read-only media inventory and collect macOS
unified-memory/process samples. Generated inventories may contain private local
paths and filenames and therefore belong under `test-output/`; they must not be
committed.

The monitor intentionally does not report a fabricated Apple Silicon “VRAM”
number. Apple Silicon uses unified memory, so the first version records process
RSS, system memory/compression, swap, CPU and process failures. Metal errors are
captured by the workload wrapper that owns the benchmark run.

`build_visual_fixture.py` is intentionally a derived-frame fixture for short
model-load checks.  `build_media_fixture.py` instead creates a DB that points
at the complete selected sample (10 videos + 20 images) without copying or
modifying media; use that fixture for sustained runs and Legacy/V2 parity.
