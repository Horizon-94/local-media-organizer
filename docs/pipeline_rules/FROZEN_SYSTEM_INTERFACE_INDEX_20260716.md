# Media Archive 冻结接口与阶段索引

状态：`CURRENT_FROZEN_INTERFACE_INDEX`

日期：`2026-07-17`

## 当前主线完成度

本索引以当前中心数据库及已冻结规则为准，不把历史实验脚本、旧版本 dry-run
或仅有代码但未完成真实验收的阶段计算为完成。

| 阶段 | 状态 | 当前证据 |
|---|---|---|
| Step01 来源扫描、lineage、去重建档 | PASS | 2681 个文件记录，2388 个 source assets；正式 DB run done |
| Step02 图片预览 | PASS | 433 个图片 visual units/preview；A9T-v3 规则保留 |
| Step02 视频抽帧 | PASS / ACCEPTED RAW EXCLUSION | 102 个视频输入；97 个产生画面派生帧；5 个 CRM/BRAW 保留为源资产但不进入当前画面分析；1195 个 visual units、1097 个 unique derived frames |
| Stop03-1 YOLOE | PASS | 1628 个 visual units，5188 条 visual labels |
| Stop03-1 OpenCLIP | PASS_WITH_EXTERNAL_PAYLOAD | 1628/1628 embedding metadata 在中心库；512 维 payload 位于已校验的外部 JSONL |
| Stop03-2 通用候选选择 | PASS/FROZEN | V25 快照 390 条：Qwen-VL 336，OCR 54 |
| Stop03-3 Qwen-VL | PASS/FROZEN | 正式 run 336/336 success，中心 DB readback PASS |
| Stop03-4 OCR | PASS/FROZEN | 正式 full run 54/54 success；6 条复用、48 条新推理，中心 DB readback PASS |
| Stop03-5 统一证据 staging / 分发 | 5A GENERIC/FROZEN；5B PASS/COMMITTED | 5A 动态 run/动态计数；5B 中心表 390 条 evidence，389 PASS + 1 REVIEW，幂等反查通过 |
| Stop03-5C Qwen/YOLOE 语义传播 | PASS/FROZEN/COMMITTED | 最新 5B Qwen 视频锚点，严格前后各 3 帧，三方对象交集；OCR 不传播；623 条当前项目记录已写入中心表并通过幂等反查 |
| Stop03-5D 文本向量 | GENERIC/FROZEN/COMMITTED | 文档数和唯一文本数从指定数据库动态生成；success=unique_text_count、links=document_count；当前项目实测另见非规范验收报告 |
| Stop03-5E V1 文本证据搜索 | PASS/FROZEN BASELINE；非全视觉覆盖 | 动态选择最新成功5D run；只读分块 cosine、分页、缩略图、时间码、5/10秒区间和场景歧义标签已通过；当前项目758文档对应755个不同视觉单元，不代表全部画面 |
| Stop03-5E V2 混合全视觉搜索 | PASS/FROZEN | 以OpenCLIP覆盖全部视觉单元，5D文本和YOLOE作为附加排序证据；正式数据库全量payload、dry-run、真实query和可视资源均已通过；未写库、未建索引 |

2026-07-17 中心数据库反查后，不再用单一百分比混合表达阶段完成度和素材覆盖率。
当前已列视觉阶段门均已完成；5 个 RAW 视频属于已接受的画面处理例外，不影响未来
独立音频路线。

```text
视觉主线至 Stop03-5C：完成
Stop03-5D 文本向量合同、真实 smoke、完整向量和中心数据库写回：完成
Stop03-5E V1 文本证据搜索：完成并冻结为基线，但不代表全视觉覆盖
Stop03-5E V2 混合全视觉搜索：完成真实单query验证并冻结
Stop03视觉/文本主线最终只读反查与冻结交接：完成
搜索索引：暂缓；当前规模全量向量扫描已通过，未来达到规模或延迟门槛后再单独设计
```

详细反查见
`docs/pipeline_rules/STOP01_STOP03_5C_CENTRAL_DB_RECONCILIATION_AUDIT_20260717.md` 和
`docs/pipeline_rules/STOP03_VISUAL_TEXT_MAINLINE_FINAL_HANDOFF_20260717.md`。

## 下一步固定顺序

Stop03-5C 通用规则、正式中心数据库接口、完整 dry-run、显式 commit、readback
和幂等复跑均已完成。Stop03-5D 已完成文本合并、相同文本复用、向量 BLOB 存储、
来源追踪和 run 身份合同，并通过 preflight、dry-run、targeted tests、5条真实 smoke
和三路完整动态执行。Stop03-5E V1 文本证据入口已冻结为基线。反查发现它没有覆盖全部
视觉单元，因此新增 V2 混合全视觉入口。V2代码、测试、preflight、dry-run、真实query
和最终只读反查均已通过并冻结。当前不建立搜索索引。

推荐实施顺序：

1. Stop03-4A：OCR 中心数据库合同、migration、preflight、dry-run。`已完成`
2. Stop03-4B：local-only/no-download 小样本 OCR smoke。`已完成，6/6`
3. Stop03-4C：冻结 OCR 队列动态执行、逐条写回中心数据库。`已完成，54/54`
4. Stop03-4D：readback、幂等、恢复、数据库完整性及冻结。`已完成`
5. Stop03-5A：Qwen-VL + OCR 联合质量审计。`已完成并冻结`
6. Stop03-5B：从中心数据库构建统一 evidence staging / 分发。`已提交并幂等反查通过`
7. Stop03-5C：Qwen/YOLOE 三方交集传播。`已提交中心数据库并通过 readback、完整性、外键和幂等复跑`
8. Stop03-5D 文本向量数据库合同、preflight、dry-run。`已完成并冻结；未运行模型、未写中心库`
9. Stop03-5D 本地 3–5 条真实向量 smoke。`已完成，5/5 PASS`
10. 完整向量生成和逐条写回中心库。`通用节点已冻结；当前项目 readback PASS`
11. 文本搜索合同、preflight、dry-run。`已完成并冻结`
12. 少量本地真实 query smoke。`已完成并通过人工验收`
13. V1完整文本证据查询入口。`已通过真实单 query验证并冻结为基线；不代表全视觉覆盖`
14. V2混合全视觉查询入口。`真实单query、全量扫描、预览资源和只读边界均通过；已冻结`
15. Stop03视觉/文本主线最终只读反查与冻结交接。`已完成`
16. 可选搜索索引合同。`暂缓；不是当前验收必需项`

5 个 CRM/BRAW 不再要求画面重跑。它们继续保留 source asset 身份，未来 Whisper
或音频抽取可独立判断是否支持。

## 冻结核心

### Step01 / Step02 DB 主线

- `scripts/02_step01_step02_pipeline/step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py`
- `scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py`
- `scripts/02_step01_step02_pipeline/step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py`

### Stop03-1 低成本视觉证据

- `scripts/03_stop03_visual_analysis/stop03_yoloe_full_from_db_safe_v6_20260709_170200.py`
- `scripts/03_stop03_visual_analysis/stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py`

### Stop03-2 V25 候选与数据库合同

- `scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py`
- `scripts/03_stop03_visual_analysis/finalize_stop03_2_v25.py`
- `scripts/03_stop03_visual_analysis/stop03_2_v25_candidate_contract_lock.py`
- `migrations/20260711_stop03_2_candidate_queue_v25.sql`
- `migrations/20260711_stop03_2_v25_candidate_snapshot_qwenvl_v1.sql`
- `configs/stop03_2_high_value_policy_v25.json`
- `docs/pipeline_rules/STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md`
- `docs/pipeline_rules/STOP03_2_V25_CANDIDATE_DB_CONTRACT_LOCK.md`

候选合同文档中的阶段状态保留其冻结时原文；当前实际状态以本索引的
`PASS/FROZEN` 和中心数据库 readback 为准。冻结原文不为整理压缩包而改写。

### Stop03-3F Qwen-VL 正式冻结节点

- `scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_node_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py`
- `scripts/stop03_monitor/stop03_3f_qwenvl_dynamic_db_monitor.py`
- `scripts/03_stop03_visual_analysis/qwenvl_output_contract_v2.py`
- `scripts/03_stop03_visual_analysis/stop03_3c_qwenvl_db_orchestrator_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_batch75_diagnostic_v1.py`
- `configs/stop03_3_qwenvl_db_v1.json`
- `configs/qwenvl_prompt_v2_384.txt`
- `docs/pipeline_rules/STOP03_3F_DYNAMIC_QWENVL_DB_NODE_V1.md`

旧 Stop03-3 合同文档保留其设计阶段原文；当前生产执行入口以 Stop03-3F 冻结节点为准。

### Stop03-5A 通用联合质量审计

- `scripts/03_stop03_visual_analysis/stop03_5a_joint_db_quality_audit_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_5a_joint_db_quality_audit_node_v1.py`
- `configs/stop03_5a_joint_quality_audit_v1.json`
- `docs/pipeline_rules/STOP03_5A_JOINT_EVIDENCE_QUALITY_AUDIT_V1.md`

正式入口自动选择与当前执行视图一致的最新完整 Qwen/OCR run；历史
336/54/390 不参与 PASS 判断。

### Stop03-5B 通用统一证据 staging

- `scripts/03_stop03_visual_analysis/stop03_5b_unified_evidence_staging_v1.py`
- `configs/stop03_5b_unified_evidence_staging_v1.json`
- `migrations/20260716_stop03_5b_unified_evidence_staging_v1.sql`
- `docs/pipeline_rules/STOP03_5B_UNIFIED_EVIDENCE_STAGING_V1.md`

5B 已通过显式 commit 创建中心表；重复提交为 `IDEMPOTENT_PASS`。当前中心表包含
一个 success staging run 和 390 条 evidence。该数字是当前项目验收记录，不是
生产固定门槛。

### Stop03-5C 通用 Qwen/YOLOE 语义传播

- `scripts/03_stop03_visual_analysis/stop03_5c_qwenvl_yolo_propagation_v1.py`
- `configs/stop03_5c_qwenvl_yolo_propagation_v1.json`
- `migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql`
- `docs/pipeline_rules/STOP03_5C_GENERIC_QWENVL_YOLO_PROPAGATION_V1.md`
- `docs/pipeline_rules/STOP03_5C_LEGACY_INTERFACE_RETIREMENT.md`

旧 `stop03_5c_semantic_propagation_v1.py` 和
`stop03_5c_semantic_propagation_v2_yolo_gate.py` 已退出正式接口并拒绝命令行运行。
新入口只选择最新成功 5B run，不固定本次 run ID 或数量。当前正式 propagation run
为 `stop03_5c_9f834ea8a6ed014f00ebc8f5`，中心表 623 条是本项目验收记录，不是通用
完成门槛；相同 payload 再次提交返回 `IDEMPOTENT_PASS`。

### Stop03-5D 通用文本向量数据库合同

- `scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_contract_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_smoke_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py`
- `scripts/stop03_monitor/stop03_5d_text_embedding_db_monitor.py`
- `configs/stop03_5d_text_embedding_db_contract_v1.json`
- `configs/stop03_5d_text_embedding_db_orchestrator_v1.json`
- `migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql`
- `docs/pipeline_rules/STOP03_5D_GENERIC_TEXT_EMBEDDING_DB_CONTRACT_DESIGN_V1.md`
- `docs/pipeline_rules/STOP03_5D_TEXT_EMBEDDING_DYNAMIC_DB_NODE_V1.md`
- `docs/pipeline_rules/STOP03_5D_PROJECT_ACCEPTANCE_20260717.md`（仅当前项目验收，不是规则）
- `tests/test_stop03_5d_text_embedding_db_contract_v1.py`
- `tests/test_stop03_5d_text_embedding_smoke_v1.py`
- `tests/test_stop03_5d_text_embedding_db_orchestrator_v1.py`

入口只读取指定数据库中最新成功 5B evidence 和最新成功 5C propagation，不固定 run
ID、文件数或文档数。每个 derived frame 最多形成一份搜索文档；完全相同的合并文本
只安排一次模型推理，再由 document-vector link 复用。

合同阶段已生成 dry-run manifest；独立真实 smoke 已用本地模型完成 5/5 验证，1024
维、float32、归一化、有限值和相同文本 query Top-1 均通过。smoke 只写 test-output。
migration 已在当前测试项目正式应用，项目 readback 已通过。具体 run ID 和数量只保存在
非规范验收报告中，不属于冻结接口。下一步在通用性复核通过后进入文本搜索合同。

### Stop03-5E 通用文本搜索合同

- `scripts/03_stop03_visual_analysis/stop03_5e_text_search_contract_v1.py`
- `configs/stop03_5e_text_search_contract_v1.json`
- `docs/pipeline_rules/STOP03_5E_GENERIC_TEXT_SEARCH_CONTRACT_DESIGN_V1.md`
- `tests/test_stop03_5e_text_search_contract_v1.py`
- `scripts/03_stop03_visual_analysis/stop03_5e_text_search_smoke_v1.py`
- `tests/test_stop03_5e_text_search_smoke_v1.py`
- `docs/pipeline_rules/STOP03_5E_PROJECT_ACCEPTANCE_20260717.md`（仅当前项目验收，不是规则）
- `scripts/03_stop03_visual_analysis/stop03_5e_text_search_query_v1.py`
- `tests/test_stop03_5e_text_search_query_v1.py`
- `docs/pipeline_rules/STOP03_5E_GENERIC_TEXT_SEARCH_QUERY_ENTRY_V1.md`
- `scripts/03_stop03_visual_analysis/stop03_5e_hybrid_visual_text_search_v2.py`
- `configs/stop03_5e_hybrid_visual_text_search_v2.json`
- `tests/test_stop03_5e_hybrid_visual_text_search_v2.py`
- `docs/pipeline_rules/STOP03_5E_HYBRID_FULL_VISUAL_COVERAGE_SEARCH_V2.md`
- `docs/pipeline_rules/STOP03_VISUAL_TEXT_MAINLINE_FINAL_HANDOFF_20260717.md`（仅当前项目交接证据）

搜索合同按指定数据库动态选择最新成功5D run，以唯一文本向量组为排名单位，用只读分块
cosine 扫描作为正确性基线，再分页展开文档。合同和真实 smoke 入口均不包含当前项目数量
或 run ID。真实 query 技术检查和带派生JPEG的可视结果已通过，用户确认基础搜索可用；
无法可靠区分的环境显示“夜间/室内（待确认）”，不改写原始 Qwen。查询原文和 query
向量不落盘，不写数据库，也不创建 FTS/向量索引。正式播放器跳转播放属于后续 UI。

V1文本证据查询入口已实现单 query、稳定 request ID、向量组分页、组内文档分页、通用过滤、
5/10秒区间和 JSON/HTML 响应。代码、假向量测试、正式数据库 preflight、无模型 dry-run
及真实本地单 query 验证均已通过，但它只冻结为文本证据基线。V2 使用全部 OpenCLIP
视觉向量作为必有召回通道，再融合 V1 文本和 YOLOE 标签；正式数据库 preflight 已确认
全部视觉单元都有有效向量。V2真实query确认全量扫描、融合、派生预览和只读边界均通过，
全视觉入口现已冻结。搜索索引不是当前阻塞项；未来只有在更大素材库达到延迟门槛后，
才单独设计。

## 历史文件处理原则

- 不删除旧 V3–V24、旧诊断、旧 contact sheet 和失败实验。
- 这些文件不属于当前正式入口，不应继续作为生产命令。
- 清理前必须另做只读 superseded/version audit，并输出保留、归档、可删除候选三类清单。
- 本冻结接口包不包含原始素材、派生图片、模型权重、中心数据库或历史 test-output。

## 冻结接口包定位

冻结接口包是当前正式项目文件的只读快照，不是脱离项目根目录即可独立运行的安装包。
它保留项目内相对路径，适合审计、校验和按原路径恢复。模型、素材、数据库及已有运行结果
必须继续由正式环境提供，不得放入接口包。
