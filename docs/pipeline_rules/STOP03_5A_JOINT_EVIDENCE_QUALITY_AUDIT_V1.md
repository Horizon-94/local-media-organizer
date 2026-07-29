# Stop03-5A Joint Evidence Quality Audit V1

状态：`FROZEN_WITH_QUALITY_REVIEW`

## 范围

本阶段从当前中心数据库动态选择两个正式 run：

- Qwen-VL：与当前 Qwen 执行视图集合一致的最新完整 success run；
- OCR：与当前 OCR 执行视图集合一致的最新完整 full success run。

不得把 Qwen 历史 smoke、retry 或其他 run 的结果混入正式集合。OCR 正式集合必须通过
full run 的 run_items 引用结果，不得直接把任意历史结果当成正式 evidence。

本阶段不运行模型、不读取原始视频、不修改中心数据库、不生成统一 staging、不做语义传播。

## 技术硬门

任一项发生即技术 FAIL：

- 正式 run 不是 success，或 run_item 仍有 pending/running/failed/review；
- 正式候选集合与冻结执行视图不相等；
- candidate/evidence/execution key 重复或跨模态 evidence ID 冲突；
- source/visual/canonical/derived lineage 缺失或与冻结队列不一致；
- 派生输入 SHA、文本 SHA、输出文件 SHA 不一致；
- Qwen clean text 为空、缺少固定三段结构、残留 wrapper/内部绝对路径或被标记截断；
- OCR success 为空、`ocr_lines_json` 无法解析、line count 不一致；
- 同一 visual unit 同时进入 Qwen/OCR 时 lineage 或 runtime SHA 不一致；
- SQLite integrity 或 foreign key check 失败。

## 策略 REVIEW

以下项目不伪造技术失败，但必须形成逐行质量标记：

- Qwen 文本短于 80 字或长于 2000 字；
- OCR 文本短于 4 字；
- OCR mean confidence 小于 0.80；
- 模态内部出现完全相同的规范化文本。

如果只有 REVIEW、没有硬失败，则：

```text
technical_status = PASS
policy_status = REVIEW
staging_readiness = READY_WITH_QUALITY_FLAGS
```

Stop03-5B 必须携带质量标记，不得把 REVIEW evidence 冒充高置信度 evidence。

## 输出

只写 test-output：

- `reports/stop03_5a_joint_quality_summary.json`
- `reports/stop03_5a_joint_quality_summary.md`
- `manifests/qwen_quality_audit.csv`
- `manifests/ocr_quality_audit.csv`
- `manifests/cross_modal_visual_overlap.csv`
- `manifests/quality_review_items.csv`

preflight 不创建输出；audit 前后必须验证中心数据库 SHA 不变。

## 正式验收结果（历史记录，不是通用门槛）

```text
technical_status = PASS
policy_status = REVIEW
staging_readiness = READY_WITH_QUALITY_FLAGS

Qwen selected = 336
Qwen PASS = 336
Qwen historical rows excluded = 337

OCR selected = 54
OCR PASS = 53
OCR REVIEW = 1

combined evidence = 390
unique evidence = 390
hard fail items = 0
cross-modal visual overlaps = 2
cross-modal overlap failures = 0
database integrity = ok
foreign key errors = 0
central database unchanged = true
```

唯一 REVIEW：

```text
candidate_id = cand_v25_d6902b13062acef02ba85d910447
reason = ocr_mean_confidence_low
mean_confidence = 0.7151100635528564
```

该行可进入 Stop03-5B，但必须携带 `quality_status=REVIEW` 和原始 confidence，
不得覆盖或改写 OCR 原文。

后续素材库的数量、run_id 和 REVIEW 数量必须由当前数据库自动发现。不得把本节
336/54/390、具体 run_id 或唯一 REVIEW 行写入正式 PASS 判断。

正式冻结入口：

`scripts/03_stop03_visual_analysis/stop03_5a_joint_db_quality_audit_node_v1.py`

冻结入口 SHA-256：

`8be133e5b976b3a7ec1f778a88bb1d83aea012ea09471b549079931e40f72cb7`
