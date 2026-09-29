# Pipeline V2 Resource Calibration

`tools/pipeline_benchmark/run_full_pipeline_calibration.sh` is the single
host-side entry point. It creates a fresh fixture/output per row, writes
`calibration-state.json`, and resumes completed rows without rerunning them.

The campaign deliberately records `PASS`, `FAIL`, `UNSAFE`,
`SKIPPED_DUE_TO_UNSAFE_PARENT`, and `NOT_CONFIGURED`. A missing formal runtime
or fixture is never represented as a successful measurement.

The coordinator currently has a verified Qwen host entry. Whisper, YOLOE,
OpenCLIP, OCR, Embedding, FFmpeg, Person ReID, pair, triple, HDD, and soak
rows are now registered against the 1.2.8 runtime contract and appear in the
readiness matrix. Readiness only verifies that the production entrypoint,
runtime/model location, and fixture-builder contract exist; it does not claim
that the Metal benchmark has already passed.

Use `--check` before a campaign. It prints `READY` for active workloads and
`OPTIONAL_NOT_ACTIVE` for Silero VAD/DeepFilterNet3, which are not in the
current default visual 19-stage path.
