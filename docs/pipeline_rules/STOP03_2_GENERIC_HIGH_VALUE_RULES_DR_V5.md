# Stop03-2 Generic High Value Rules DR V5

Version: `stop03_2_generic_high_value_rules_v5_v9_0_db_materialized_timelapse_20260709`

## Conclusion

Stop03-2 V9 must be DB-driven. It must not rediscover timelapse by filename, path, YOLOE label, source_content_id, or derived preview pool.

The only valid timelapse candidate source for Stop03-2 is a central SQLite metadata table materialized from the frozen Step02 image preprocessing result:

`step02_image_timelapse_keyframes`

This table records Step02's frozen timelapse keyframes:

- `visual_unit_id`
- `sequence_id`
- `representative_position`
- `preview_role`
- `source_relative_path`
- `visual_file`
- producer metadata

For the current test run, Step02 has frozen:

- `timelapse_sequence_count = 4`
- `timelapse_keyframe_count = 12`
- each sequence has `first / middle / last`

Stop03-2 V9 should select one representative per Step02 sequence by default, preferring `middle`.

Expected current result:

- `qwen_timelapse_count = 4`

## Scope

V9 solves only Stop03-2 high-value candidate queue selection.

It does not:

- rerun Step02 image preprocessing
- rerun YOLOE
- rerun OpenCLIP
- run Qwen-VL
- run OCR
- modify original media
- download models
- use network
- redefine timelapse detection

## Timelapse Rule

Allowed input:

- DB table `step02_image_timelapse_keyframes`

Materialization source only when the table is missing or refresh is explicitly requested:

- frozen Step02 manifest `image_preview_visual_unit_manifest.csv`

Selection:

1. Read timelapse keyframes from DB.
2. Group by `sequence_id`.
3. Prefer `representative_position = middle`.
4. Fallback to `first`, then `last`, then CSV/DB order.
5. Select exactly one representative per sequence by default.
6. Do not add non-Step02 timelapse candidates.
7. Do not use YOLOE score to replace the middle representative.

## Safety

- Original media read: no
- Original media write: no
- Derived JPG read: yes, for black-frame validation only
- DB write: yes, metadata table and candidate queue table
- Model loading: no
- Network: no
- Downloads: no

## Validation

V9 cannot pass if:

- `qwen_timelapse_count = 0`
- `qwen_timelapse_count = 10` for the current 12-keyframe / 4-sequence case
- any timelapse candidate does not come from `step02_image_timelapse_keyframes`
- video overselect returns
- black leak is nonzero
- emergency cap is reached under default settings

Current expected key counts:

- Qwen manual seeds: 125
- Qwen image YOLOE: about 69
- Qwen video frames: about 129
- Qwen timelapse: 4
- OCR: 403
- black leak: 0
- video overselect groups: 0
