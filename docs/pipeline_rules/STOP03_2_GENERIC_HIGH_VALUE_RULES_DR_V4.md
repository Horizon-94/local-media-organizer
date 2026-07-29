# STOP03-2 Generic High-Value Candidate Rules DR V4 / V8.0

## Status
V8.0 freeze-candidate rule document for Stop03-2 candidate queue generation.

## Hard constraints
- Local only.
- No network.
- No downloads.
- No model loading.
- No Qwen-VL run.
- No OCR run.
- No YOLOE / OpenCLIP rerun.
- No original media decode or write.
- Only read SQLite metadata and derived preview/frame JPGs for black-frame validation.

## Core definition
A high-value visual candidate is not a percentage sample. It is a visual unit worth sending to a high-cost downstream model or preserving for search because it has manual value, scene/semantic value, OCR context value, or segment-representative value.

## Qwen-VL candidate rules

### Manual images
Manual high-value seeds from Finder tags or later manual markers enter Qwen-VL unless black/invalid.

### Video frames
Video frames are selected by generic reason-based segment rules:
- one useful representative when a video has any candidate signal;
- additional frames only when there is a strong segment reason;
- generic subject presence alone is not enough;
- long videos do not automatically get many frames;
- group guard prevents over-selection.

Strong segment reasons include:
- major object set change;
- information density jump;
- OCR region emergence with context value;
- strong boundary representative;
- rare/meaningful object appearance.

### Ordinary non-manual images
Non-manual still images enter only with strong generic visual evidence:
- multiple meaningful categories;
- OCR plus scene/object context;
- strong information density;
- high-confidence meaningful subjects.

Embedding presence alone is not a high-value reason.

### Timelapse
Timelapse is an independent group-level rule, not an ordinary still-image rule.

For each detected timelapse sequence:
- select at least one representative frame;
- for a typical three-keyframe sequence, select the middle/best representative;
- allow a second representative only if first/last labels differ strongly in larger sequences;
- do not send all timelapse keyframes by default.

Detection uses local DB paths only, including:
- `TIMELAPSE` in visual/derived/source path;
- `keyframe_pool_jpg` from Step02 image outputs.

## OCR trigger queue
OCR trigger queue is independent from Qwen-VL high-value queue. OCR candidates are selected from screen/text/document/sign/path cues. OCR queue is not deduplicated by Qwen-VL selection.

## Rejection rules
Reject from Qwen-VL and OCR queues:
- black/near-black frames;
- missing/unopenable derived image;
- invalid visual unit with no traceable derived asset;
- ordinary weak YOLOE labels without high-value evidence;
- duplicate video frames in same derived/time group.

## Audit rules
The run must report:
- Qwen-VL total;
- video/image/timelapse/manual breakdown;
- OCR total;
- Qwen/OCR overlap;
- black leak counts;
- video group overselect review count;
- emergency cap status.

## Pass criteria
- black leak into Qwen-VL = 0;
- black leak into OCR = 0;
- no emergency cap reached under normal test run;
- video effective time negative count = 0;
- video overselect review group count = 0;
- timelapse sequences produce at least one representative when detected;
- original media not modified.
