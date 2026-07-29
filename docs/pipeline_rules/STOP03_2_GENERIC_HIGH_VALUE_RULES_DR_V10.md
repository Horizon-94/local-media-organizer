# STOP03-2 Generic High Value Rules DR V10

## Verdict

V13 is not freeze-ready after visual review. V14 is required.

## Problem found in V13

1. High-value video frames can still prefer a poor human frame over a better nearby human frame.
   - Examples: closed eyes, tilted face, side/cropped face, awkward body/face frame.
   - User expectation: when a clear centered/front-facing human frame exists nearby, prefer that over awkward expression or edge/cropped subject.

2. Some high-value frames are selected on hand/finger/edge objects while better human-centered frames exist.
   - User expectation: normal documentary/phone/camera footage usually uses center composition; edge/hand-only frames should not outrank centered subject frames.

3. V13 fixed OCR over-reporting and screen-recording Qwen leakage, but high-value visual selection still lacks a human/composition quality preference.

## V14 scope

V14 only changes video high-value frame ranking. It does not change the already-fixed V13 gates.

Kept from V13:

- DB-only timelapse.
- Still image OCR disabled.
- Normal camera video OCR disabled by default.
- OCR only for strict screen-capture / screen-recording sources.
- Screen-recording video does not enter Qwen-VL high-value queue.
- Tail/end-frame suppression remains enabled.
- No model rerun.
- No original media write.
- No network.
- No downloads.

## V14 new rule

Use existing YOLOE bounding boxes from the central SQLite `visual_labels` table to add a local metadata-only composition adjustment:

- Prefer centered face/person boxes.
- Prefer larger, non-edge-cropped human subjects.
- Penalize off-center or edge-cropped face/person frames.
- Penalize hand/finger/arm-only frames when no centered human subject is present.
- Add audit fields for centered-human preference and composition penalties.

This is not face recognition. It does not identify people. It does not run a face/pose/eye-state model. It only uses existing YOLOE labels and boxes already written in the local DB.

## Expected outcome

Compared with V13:

- OCR should remain strict: only screen-capture video frames.
- Screen-recording Qwen leakage should remain 0.
- High-value video count may shift slightly, but should remain in a reasonable range.
- Human-facing/camera-facing centered frames should be more likely to be selected.
- Hand-only / edge / awkward cropped frames should be less likely to be selected.

## Acceptance gates

PASS if:

- validation_status = PASS
- black leak = 0
- model_rerun all false
- OCR still screen-capture only
- screen_recording_qwenvl_leak_count = 0
- video_overselect_review_group_count = 0
- V14 contact sheet shows fewer obviously bad human picks than V13

REVIEW if:

- qwen_video_frame_count drops too far below V13 without clear reason
- centered human preference does not improve visual review
- too many high-value frames still hit hand/finger/edge-only views

FAIL if:

- original media is modified
- models are rerun
- network/download is used
- screen recordings leak into Qwen-VL
- OCR again includes normal camera videos by default
