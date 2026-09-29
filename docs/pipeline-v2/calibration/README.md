# Pipeline V2 Resource Calibration

`tools/pipeline_benchmark/run_full_pipeline_calibration.sh` is the single
host-side entry point. It creates a fresh fixture/output per row, writes
`calibration-state.json`, and resumes completed rows without rerunning them.

The campaign deliberately records `PASS`, `FAIL`, `UNSAFE`,
`SKIPPED_DUE_TO_UNSAFE_PARENT`, and `NOT_CONFIGURED`. A missing formal runtime
or fixture is never represented as a successful measurement.

The coordinator currently has a verified Qwen host entry. Whisper, YOLOE,
OpenCLIP, OCR, Embedding, FFmpeg, Person ReID, pair, triple, HDD, and soak
rows remain explicit until their 1.2.8 formal command and isolated fixture are
registered. This prevents a synthetic substitute from being mistaken for a
Pipeline V2 resource result.
