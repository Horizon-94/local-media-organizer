# STOP03-2 Generic High Value Rules DR V8

## Verdict Target

Stop03-2 V12.0 is a local DB-only candidate queue selector revision based on V11.0.

## Scope

This revision only changes two areas:

1. Video Qwen-VL high-value candidate selection is opened one more step from V11.0.
2. Still-image OCR remains disabled by default; OCR candidates are video-frame only.

## Non-goals

- No Qwen-VL execution.
- No OCR execution.
- No YOLOE rerun.
- No OpenCLIP rerun.
- No model loading.
- No network.
- No downloads.
- No original media write.
- No Step02 manifest read for timelapse.
- No filename/path rediscovery of timelapse sequences.

## DB-only Inputs

Stop03-2 V12.0 reads from central SQLite tables:

- `visual_units`
- `derived_assets`
- `source_assets`
- `visual_labels`
- `visual_label_terms`
- `embeddings`
- `manual_high_value_visual_seeds`
- `step02_image_timelapse_keyframes`

Timelapse candidates must come from `step02_image_timelapse_keyframes` only.

## Video Gate Change

V11.0 produced:

- video input visual units: 1195
- video high-value candidates: 135
- ratio: 11.3%

V12.0 target observation band:

- 12.5%–15% of existing video visual units when real evidence supports it
- for the current 1195 video visual units, roughly 149–179 candidates

This is not a hard quota. The selector must not invent candidates without evidence.

V12.0 opens the gate by:

- lowering generic video signal threshold from 1.45 to 1.25
- lowering label-change threshold from 0.72 to 0.62
- making OCR scene-change and high-information-jump rules less brittle
- increasing per-source guard for longer video groups
- keeping a minimum selected-frame gap of 15 seconds

## Video Distribution Guard

The selector must avoid:

- single videos exploding to 20/30/50 selected frames on this 30GB test set
- dense repeated selections within the same small interval
- global percentage forcing without visual/OCR/object-change evidence

The selector should prefer:

- adding candidates to longer and information-rich videos
- filling large temporal gaps when there is evidence
- preserving short-video one-frame behavior

## OCR Policy

V12.0 keeps V11.0 OCR policy:

- still images do not enter OCR by default
- OCR queue only accepts `visual_unit_type == video_frame`
- still-image OCR exclusions must be reported as `image_ocr_default_excluded_count`

## Outputs

The selector writes:

- `stop03_2_candidate_queue_items` in central SQLite
- Qwen-VL candidate CSV/JSONL
- OCR candidate CSV/JSONL
- decision manifest CSV/JSONL
- summary JSON/MD
- video budget report CSV

A separate visual audit tool generates:

- local HTML contact sheet showing all 97 video groups
- grey normal frames
- green YOLOE-labeled frames
- red Qwen-VL high-value frames
- yellow OCR frames
- red + yellow overlap for frames that are both high-value and OCR

## PASS Criteria

- `validation_status = PASS`
- no network
- no download
- no model rerun
- no original media write
- `qwen_timelapse_count = 4`
- timelapse source policy remains central DB only
- `ocr_media_type_counts` contains video only
- image OCR excluded count remains reported
- `black_leak_into_qwenvl_count = 0`
- `black_leak_into_ocr_count = 0`
- `video_overselect_review_group_count = 0`
- video high-value count lands around 149–179 or is explainably lower due to lack of evidence
- visual contact sheet is generated for human review before freeze

## REVIEW Criteria

- video count remains near V11.0 level, around 135, meaning V12.0 did not materially open coverage
- video count exceeds 180 on this test set
- a single source group exceeds 18 selected frames without clear reason
- long videos still show large uncovered gaps in the interval audit
- OCR queue contains still images

## FAIL Criteria

- network or downloads used
- any model rerun occurs
- original media is modified
- timelapse is rediscovered from filename/path instead of central DB
- black/invalid visual leaks into Qwen-VL or OCR
- candidate queue cannot be traced to DB visual_unit_id / source_content_id / derived_id
