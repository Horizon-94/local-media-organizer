# STOP03-2 Generic High Value Rules DR V11

## Verdict

V15 is a corrective revision on top of V14. V14 improved strict OCR, screen-recording exclusion, tail suppression, and human composition preference, but manual review still found two problems:

1. Some high-value frames are still near the video tail / last effective frame.
2. In the same source video, multiple high-value frames can be visually too similar: same person/scene, only slightly wider or tighter focal framing.
3. Some selected person frames are poorer representatives than nearby frames: side face, cropped face, hand/finger/edge subject, while a better centered human frame exists nearby.

V15 adds a post-selection comparison pass inside each source video group.

## Scope

V15 changes only Stop03-2 candidate selection and the local contact-sheet viewer.

It does not:

- rerun YOLOE
- rerun OpenCLIP
- run Qwen-VL
- run OCR
- read original video files
- modify original media
- use network
- download models or dependencies
- rediscover timelapse from filenames

## Input policy

All selection data must come from the central SQLite database and existing derived frame files:

- `visual_units`
- `derived_assets`
- `source_assets`
- `visual_labels`
- `visual_label_terms`
- `manual_high_value_visual_seeds`
- `step02_image_timelapse_keyframes`

## V15 rule changes

### 1. Stronger tail suppression

V14 used a relatively narrow tail window. V15 increases it:

- tail ratio: 10% of Step02 timecode coverage
- minimum tail window: 6 seconds
- maximum tail window: 30 seconds

For short clips under 30 seconds, one-frame coverage remains allowed.

### 2. Same-shot high-value de-duplication

After the initial high-value frames are selected inside each video group, V15 compares selected frames to each other.

Frames are treated as likely same-shot duplicates when:

- their time gap is within about 45 seconds, and
- their YOLOE label sets are highly similar, or
- both are human/person/face frames with similar label sets.

When same-shot duplication is detected, V15 keeps the stronger representative frame instead of keeping multiple near-identical HIGH frames.

### 3. Local representative replacement

For each selected video frame, V15 searches nearby candidate frames within about 24 seconds.

If a nearby frame is a better representative, V15 may replace the selected frame.

Better representative means:

- more centered human/face/person box
- less edge-cropped subject
- not hand/finger-only
- not tail frame
- comparable or better existing Qwen score

This is a DB-only heuristic. It does not identify eye state or facial expression.

### 4. Refill after de-duplication

If same-shot de-dup removes selected frames, V15 may refill from remaining non-tail candidates, but only when:

- the frame is time-separated from existing selected frames
- the frame is not redundant with existing selected frames
- the group guard is not exceeded

## Expected effect

Compared with V14, V15 may slightly reduce or redistribute video high-value frame count.

The target is not a fixed number. The target is better frame choice quality:

- fewer tail HIGH frames
- fewer repeated near-identical HIGH frames
- better centered human representatives
- no screen-recording Qwen-VL leakage
- OCR remains strict screen-capture video only

## Acceptance checks

Required PASS gates:

- `validation_status = PASS`
- `model_rerun = false` for YOLOE/OpenCLIP/Qwen-VL/OCR
- `black_leak_into_qwenvl_count = 0`
- `black_leak_into_ocr_count = 0`
- `video_overselect_review_group_count = 0`
- `screen_recording_qwenvl_leak_count = 0`
- OCR source remains `strict_screen_capture_path`
- no original media write
- no network / no downloads

Review metrics:

- `v15_local_replacement_count`
- `v15_same_shot_dedup_drop_count`
- `v15_refill_count`
- `tail_suppressed_signal_count`
- manual contact-sheet inspection

## Freeze note

V15 should not be frozen based on counts alone. It must be judged from the HTML contact sheet, especially videos where V14 previously selected:

- end/tail frames
- hand/finger frames
- side/ugly/cropped person frames
- repeated nearly identical person frames
