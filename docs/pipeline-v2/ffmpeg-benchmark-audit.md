# FFmpeg / Stage 03 benchmark audit

## Parameter comparison

The installed 1.2.8 Stage 03 implementation is the frozen extractor
`step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py`, wrapped by the
generic interval adapter.  The exact verified contract is:

| Parameter | Installed Stage 03 |
|---|---:|
| sampling offset | 2,000 ms |
| sampling interval | 3,000 ms |
| maximum long edge | 1,280 px |
| primary decoder | VideoToolbox |
| fallback | software FFmpeg when primary fails or produces no valid JPEG |
| JPEG quality | `-q:v 3` |
| streams | first video stream; audio/subtitle/data excluded for frame extraction |
| short-video fallback | one frame for clips up to 5 s when normal sampling yields none |

## Previous direct FFmpeg run

The earlier `ffmpeg-hevc-4k-videotoolbox-x1` run is retained as an **experimental
baseline**, not a formal Stage 03 measurement.  It used the same visible filter
and VideoToolbox flag, but bypassed the installed DB adapter, per-video
checkpoint/state, JPEG validation, and software fallback.  It produced 168
frames and exited successfully in 150.75 s.  Its old report also used a
pre-fix disk field name, so its disk delta is not comparable.

## Formal installed-parameter run

The formal run used the installed adapter and frozen extractor with the same
source video represented by an isolated benchmark DB.  It used a single video
job, concurrency 1, and wrote only to the benchmark output directory.  It
produced 168 JPEGs, completed the DB-backed stage with exit code 0, and passed
the installed checkpoint/state path.  Wall time was 546.85 s.

This is a correctness/contract result, not a claim that the formal wrapper is
faster than direct FFmpeg: the wrapper performs individual frame extraction,
validation, manifest/state writes, and DB commit work.  The raw report is kept
under `test-output` and is intentionally not committed.

The sandbox could not expose the child process tree to the independent
benchmark monitor for this run (`ps` returned no descendants), so the formal
run's peak RSS and swap fields are marked unavailable rather than fabricated.
The installed stage telemetry and exit/output counts remain valid for the
parameter and correctness audit.

## Fixture policy

The old 30-derived-frame fixture remains limited to model-load and short-run
resource checks.  A separate readonly fixture builder now records the complete
small sample selected from the `001DZLtest` inventory: 10 videos and 20 images,
about 9.53 GB of source media, without copying or changing those files.  This
fixture is the required input for later Legacy-vs-Pipeline-V2 A/B work.
