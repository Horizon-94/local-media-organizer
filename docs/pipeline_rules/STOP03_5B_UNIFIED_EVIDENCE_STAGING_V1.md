# Stop03-5B Unified Evidence Staging V1

状态：`PASS_COMMITTED`

## 目标

从中心 SQLite 自动选择与当前冻结执行队列完全对应的最新完整 Qwen-VL run 和
OCR full run，复用 Stop03-5A 的逐条质量审计，把两种文本证据整理为统一 staging
合同。候选数、证据数、PASS/REVIEW 数全部按当前数据库计算，不使用
336、54、390 等固定测试数量。

本阶段不运行模型、不读取原始视频、不联网、不下载。`preflight` 不写文件；
`dry-run` 只写 test-output；只有显式 `commit` 加
`--confirm-central-db-write` 才允许写：

- `stop03_5_unified_evidence_runs`
- `stop03_5_unified_evidence_items`

## 证据边界

5B staging 包含 Qwen-VL 和 OCR 文本证据、完整 lineage、来源 run/result、
输入派生图 SHA、文本 SHA、质量状态及模态属性。质量 REVIEW 可以进入 staging，
但必须保留 `quality_status=REVIEW` 和原因。

YOLOE labels 与 OpenCLIP embeddings 已在中心数据库中保持结构化存储，本阶段
不复制标签或向量。Stop03-5C 按 `source_content_id`、`visual_unit_id` 和
`canonical_visual_unit_id` 将这些既有结构化证据与 5B 文本证据联合分发。

## 通用完成门

技术 PASS 必须满足：

- 最新 Qwen 完整 run 的候选集合等于当前 Qwen 执行视图；
- 最新 OCR full run 的候选集合等于当前 OCR 执行视图；
- 5A 技术 PASS，且 staging readiness 为 READY 或 READY_WITH_QUALITY_FLAGS；
- staging 行数等于两个实际输入集合之和；
- candidate ID、evidence ID、canonical evidence key 均唯一；
- 没有质量 FAIL；
- SQLite integrity 与 foreign key check 通过。

历史验收中的 336/54/390 只描述当次数据，不属于本合同门槛。

## 模式

```text
--mode preflight
--mode dry-run
--mode commit --confirm-central-db-write
```

## 正式提交结果

```text
staging_run_id = stop03_5b_8ae4389ecd58010eeb9315c1
qwen_count = 336
ocr_count = 54
evidence_count = 390
pass_count = 389
review_count = 1
fail_count = 0
duplicate evidence groups = 0
duplicate canonical key groups = 0
latest view count = 390
database integrity = ok
foreign key errors = 0
```

重复提交返回 `IDEMPOTENT_PASS`，没有新增 evidence 行。

本节数量仅记录当前项目的正式验收结果，不是未来素材库的固定完成门槛。正式
脚本仍按当前数据库的实际 Qwen/OCR 集合动态计算数量和摘要。

Stop03-5B 已停止在 Stop03-5C 之前。未经新的阶段授权，不执行语义传播、embedding
更新或搜索索引写入。
