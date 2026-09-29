# 1.2.8 workload manifest

This is the workload list used to plan measurements.  Labels are hypotheses
for test grouping, not resource conclusions; the final class and concurrency
must come from measurements.

| Workload | 1.2.8 entry | Initial class to measure | Primary output |
|---|---|---|---|
| FFmpeg / VideoToolbox | `step02_video_frame_generic_interval_v1.py` → frozen Stage 03 | IO + VideoToolbox | JPEG frames |
| image preview | `step02_2_image_preview_from_db_safe_v6_20260709_182200.py` | IO + CPU | image visual units |
| YOLOE | `stop03_yoloe_full_from_db_safe_v6_20260709_170200.py` | Metal + memory | object/keyword labels |
| OpenCLIP | `stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py` | Metal + memory | visual embeddings |
| Person/ReID | `stop03_1c_person_reid_db_orchestrator_v1.py` | Metal + memory + DB | face/person evidence |
| Qwen3-VL | `stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py` | Metal + memory | visual descriptions |
| OCR | `stop03_4_ocr_db_orchestrator_v1.py` | CPU/Metal + DB | OCR evidence |
| Whisper | audio enrichment runtime | CPU/Metal + IO | transcript/timestamps |
| Silero VAD | audio enrichment runtime | CPU | speech regions |
| DeepFilterNet | audio enrichment runtime | CPU/IO | cleaned audio |
| text embedding | `stop03_5d_text_embedding_db_orchestrator_v1.py` | Metal/CPU + memory | text vectors |
| dedup/grouping | `source_frame_dedup_central_db.py` | CPU + DB | duplicate groups |
| SQLite writer | central DB writer path | DB-bound | durable commits |

The first benchmark pass measures single-worker loading and completion.  Same-
class concurrency and cross-model launch-order tests are only promoted after
the corresponding single-worker result is valid.
