# STOP03-2 Generic High Value Rules DR V7 / V11.0

## Verdict

V11.0 is a controlled policy update over V10.0.

## Goal

Keep the DB-only Stop03-2 candidate selector, while making two targeted changes:

1. Slightly open the video Qwen-VL high-value gate.
2. Disable still-image OCR by default.

## Non-goals

- Do not rerun YOLOE.
- Do not rerun OpenCLIP.
- Do not run Qwen-VL.
- Do not run OCR.
- Do not read or write original media.
- Do not rediscover timelapse from filenames or folders.
- Do not read Step02 manifests in Stop03-2.
- Do not use default max-qwen / max-ocr target caps.

## DB-only inputs

Stop03-2 reads only the center SQLite DB for upstream state:

- visual_units
- derived_assets
- source_assets
- visual_labels
- visual_label_terms
- embeddings
- manual_high_value_visual_seeds
- step02_image_timelapse_keyframes

Timelapse source policy remains:

- central DB only
- table: step02_image_timelapse_keyframes
- no Step02 manifest read
- no filename rediscovery
- one middle representative per sequence by default

## V11 video policy

V10 video result was 129 / 1195 = 10.8%.

V11 target is not a hard count, but the preferred expected band for this test set is around 12% to 13% if real evidence supports it.

Changes:

- VIDEO_CANDIDATE_MIN_GAP_MS: 20000 -> 15000
- GENERIC_VIDEO_SIGNAL_MIN_SCORE: 1.60 -> 1.45
- video group guard slightly opened:
  - <= 30s: 1
  - <= 90s: 2
  - <= 180s: 4
  - <= 300s: 6
  - <= 600s: 9
  - <= 1200s: 14
  - > 1200s: 18

This is not a quota. If the evidence is weak, the selector should not fabricate frames.

## V11 OCR policy

Still images do not enter OCR by default.

OCR queue is restricted to:

- visual_unit_type == video_frame
- black/invalid rejected = false
- ocr_score > 0

Still-image OCR can only be reintroduced later through an explicit allowlist / explicit text evidence rule. V11 does not include that.

## Expected current 30GB behavior

Expected direction:

- qwen_video_frame_count: above V10's 129, likely toward 140-155 if evidence supports it
- qwen_timelapse_count: 4
- qwen_manual_seed_count: 125
- qwen_image_yoloe_count: about 69 unless unrelated rules change
- ocr_total_count: should drop from 403 to about the previous video OCR count, around 320
- image OCR count: 0 by policy

## PASS gates

- validation_status = PASS
- no network
- no download
- no model rerun
- no original media write
- black leaks = 0
- video_overselect_review_group_count = 0
- timelapse source policy = central_db_only
- qwen_timelapse_count = 4 on current test DB
- OCR media type counts must not include image

## REVIEW gates

- qwen_video_frame_count remains exactly 129: V11 did not materially open video selection
- qwen_video_frame_count exceeds about 180 on this 30GB test set
- any video group selects >16
- any group is overselect_review_flag = true
- OCR still contains image rows

## FAIL gates

- any model reruns
- any network/download behavior
- original media write
- timelapse read from manifest or filename rediscovery
- black/invalid visual leaks into Qwen-VL or OCR
