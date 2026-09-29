# Installed 1.2.8 stage DAG audit

This audit is derived from the installed 1.2.8 source checkpoint
(`v1.2.8-installed-baseline`), especially `build_stage_plan()` in
`pipeline_orchestrator.py`. It does not infer a fixed count from the UI.

## Observed plan sizes

- Default full plan with audio runtime available: 17 stages.
- Full plan without audio runtime: 16 stages.
- `all_images` adds three supplement stages: 20 stages with audio, 19 without.

This explains why historical projects can show 19 stages while the current
planner can produce another count. Pipeline V2 must persist the actual plan
for each task and must not hard-code `19`.

## Classification

| Order | Stage key | Class today | Inputs | Outputs / commit boundary | Earliest safe V2 trigger | Retry / idempotency note |
|---:|---|---|---|---|---|---|
| 1 | `scan` | C barrier today; A-capable discovery | source root | `source_assets`, lineage and scan manifests | each discovered stable file can emit `source.discovered`; scan completion remains a reconciliation barrier | strong file identity makes upsert possible; hashing must not duplicate rows |
| 2 | `image_preview` | A per-image | image `source_assets` | preview file and `visual_units` | one committed image source | claim by source content + preview contract; atomic file then short DB commit |
| 3 | `video_frames` | A per-video, emits B frames | video `source_assets` | frame files, frame manifest, `visual_units`; optional FLAC | one committed video source | execution key includes source fingerprint + sampling contract |
| 3b | `audio_search_enrichment` | A per-video with internal A/B work | source video or co-extracted FLAC | VAD/clean audio/transcript/audio embeddings | one audio artifact ready | temporary audio cleanup only after durable transcript commit |
| 4 | `visual_schema_v3` | C schema barrier | database | required visual tables | before any visual writer | one migration lease; transactional and idempotent |
| 5 | `yoloe` | B per-visual-unit | preview/frame | labels | each committed visual unit | execution key includes visual digest + model/config fingerprint |
| 6 | `openclip` | B per-visual-unit | preview/frame | visual embedding | each committed visual unit | same key pattern; no batch-wide barrier required for inference |
| 7 | `dedup` | C global/barrier | complete or declared source/frame set | duplicate groups and representatives | after a declared scan epoch closes | global grouping cannot be treated as independent per-frame work |
| 7b | `person_reid_optional_v1` | mixed B then C | visual units | face detections/embeddings, then person groups | face extraction per visual unit; grouping after epoch/barrier | split detection from clustering; clustering is global and versioned |
| 8 | `candidate_schema` | C schema barrier | database/project | candidate ledger tables | before candidate selection | one migration/ledger lease |
| 9 | `candidates_generic_v2` | C global policy today | visual units + labels + embeddings + dedup | high-value and OCR/Qwen candidate queues | after required evidence for a scan epoch; incremental epochs may close independently | queue generation must be deterministic for an epoch; never clear another epoch |
| 10 | `candidate_snapshot` | C global snapshot | candidate ledger | frozen execution snapshot | candidate epoch closed | immutable snapshot id is downstream execution key component |
| 11 | `qwen_optional_v2` | B per candidate | frozen candidate + visual file | description | each frozen Qwen candidate | already database-claim oriented; safe bounded worker pool |
| 12 | `ocr_optional_v2` | B per candidate | frozen OCR candidate + visual file | OCR text | each frozen OCR candidate | already database-claim oriented; safe bounded worker pool |
| 13 | `evidence_optional_v2` | B per visual/document with D writer | Qwen/OCR/source evidence | unified evidence rows | relevant upstream candidate terminal | compute in workers, serialize short commits |
| 14 | `propagation_optional_v2` | mixed B/C | evidence + neighbor/dedup relations | propagated evidence | local relation closed; final reconciliation at epoch barrier | must not propagate across incomplete/unstable duplicate groups |
| 15 | `embedding_optional_v2` | B per text document | committed evidence text | text vectors | each stable evidence document | key includes text digest + model/config fingerprint |
| 11a | `all_image_supplement_contract` | C global policy | all image visual units | supplement queue | image epoch closed | deterministic queue snapshot |
| 11b | `all_image_supplement_qwen` | B per candidate | supplement candidate | Qwen description | each frozen supplement candidate | bounded model workers |
| 13a | `all_image_evidence_merge` | B + D writer | supplement descriptions | evidence rows | each terminal candidate | idempotent evidence upsert |

## Required barriers retained in V2

1. Database schema migrations precede writers.
2. A scan epoch must close before authoritative global dedup.
3. Global person clustering waits for its face-embedding epoch, although face
   extraction itself may stream.
4. Candidate selection/snapshot is versioned by a closed evidence epoch.
5. Final acceptance and search-readiness are reconciliation barriers.

## Database serialization rule

Inference and media decoding may run concurrently, but workers do not hold a
SQLite write transaction while decoding or running a model. They submit a
small immutable result to one writer queue. The writer verifies the execution
key, commits with a short transaction, then emits the downstream event.

## Installed scheduler limitation

The installed scheduler runs `execute_pipeline()` stage by stage. The profile
contains a `pipeline_async` choice, but 1.2.8 explicitly rewrites execution to
`stage_serial`. Therefore the installed UI text is not evidence that an
event-driven scheduler already exists.
