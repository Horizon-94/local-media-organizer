# STOP03-2 Generic High Value Rules DR V9

## Verdict

V13.0 should be created because V12.0 exposed two policy problems during visual review:

1. OCR was too broad. Normal camera video frames were entering OCR from weak YOLOE labels such as book/sign/billboard even when the real frame had no meaningful text.
2. Screen-recording videos were entering Qwen-VL high-value visual frame selection, but screen recordings should be OCR-first and should not create visual high-value frames by default.
3. Some high-value frames were selected at the final/landing frame of videos. Tail frames are usually weak representatives and should be suppressed by default.

## V13.0 Scope

V13.0 only changes Stop03-2 candidate distribution policy. It does not rerun or load any model.

## Must Not Do

- No network.
- No downloads.
- No dependency install.
- No YOLOE rerun.
- No OpenCLIP rerun.
- No Qwen-VL run.
- No OCR run.
- No original media read/decode/write.
- No Step02 manifest read.
- No timelapse filename rediscovery.

## Inputs

V13.0 reads only the central SQLite database and derived preview/frame JPGs for black-frame validation.

Required DB tables include:

- visual_units
- derived_assets
- source_assets
- visual_labels
- visual_label_terms
- embeddings
- manual_high_value_visual_seeds
- step02_image_timelapse_keyframes
- stop03_2_candidate_queue_items
- model_runs

## OCR Rule

### V12.0 Problem

V12.0 restricted OCR to video frames, but still allowed normal camera video OCR when weak text-like YOLOE labels appeared.

This caused false positives.

### V13.0 Rule

Default OCR is stricter:

- Still images do not enter OCR.
- Normal camera videos do not enter OCR by default.
- Only screen-recording / screenshot-like video sources enter OCR by default.

Strict path/source indicators include:

- RPReplay
- screenrecording
- screen_recording
- screen-recording
- screen recording
- record screen
- recorded screen
- 录屏
- 屏幕录制
- 截屏
- 截图
- screenshot

Normal camera video OCR exceptions are not implemented in V13.0. If needed later, add a separate explicit allowlist or big-text evidence rule.

## Qwen-VL High Value Video Rule

### Screen Recording Suppression

Screen-recording videos should not produce Qwen-VL high-value visual candidates by default.

They can still enter the OCR queue.

Reason: screen recordings are generally text/UI/material for OCR, not visual documentary high-value frame selection.

### Tail Frame Suppression

Final/landing frames should not be selected as high-value by default.

V13.0 suppresses tail video frame selection for videos longer than 30 seconds. Tail window is defined as:

- max(3000 ms, min(15000 ms, duration * 0.03))

If a short video has only minimal coverage, the short-video fallback is not blocked.

## Timelapse Rule

Unchanged from V12.0:

- DB-only.
- Source table: step02_image_timelapse_keyframes.
- No Step02 manifest read.
- No filename/path rediscovery.
- One representative frame per sequence, preferring middle.

## Expected V13.0 Result

Compared with V12.0:

- OCR total should drop below 320 because normal camera video OCR is disabled.
- OCR media_type should remain video only.
- OCR source should be strict_screen_capture_path.
- Qwen video count may drop from 160 because screen-recording videos are removed from Qwen-VL and tail frames are suppressed.
- If Qwen video count drops below the desired 12.5%–15% band, do not re-enable screen-recording Qwen. Instead, add better normal-video time-coverage rules in a later version.

## PASS Conditions

- validation_status is PASS.
- black_leak_into_qwenvl_count = 0.
- black_leak_into_ocr_count = 0.
- video_overselect_review_group_count = 0.
- ocr_media_type_counts has only video.
- image_ocr_default_excluded_count > 0.
- normal_video_ocr_default_excluded_count > 0.
- video_ocr_candidate_source_policy = strict_screen_capture_video_only.
- screen recording groups have no Qwen-VL high-value video candidates.
- timelapse remains DB-only.
- model_rerun all false.

## REVIEW Conditions

- Qwen video ratio falls too low after removing screen recordings.
- OCR becomes empty unexpectedly.
- Any normal camera video still enters OCR without strict screen-capture path evidence.
- Tail frames still appear frequently as high-value candidates.

## Next Human Review

After V13.0, generate the video contact sheet again and check:

- yellow OCR marks only appear on screen-recording-like sources.
- red high-value marks no longer appear on screen recordings.
- red high-value marks avoid the final/landing frame.
- normal camera videos still have enough red high-value coverage.
