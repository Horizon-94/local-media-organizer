# Stop03-2 V25 Candidate DB Contract Lock

状态：`READY_FOR_MIGRATION_REVIEW`
合同版本：`stop03_2_v25_candidate_snapshot_v1`

## 冻结对象

本合同不修改 V25 候选算法。它把中心表 `stop03_2_candidate_queue_items` 当前已提交的 V25 选择账本固化为不可变执行快照：

```text
total = 390
qwenvl_high_value = 336
ocr_trigger = 54
contract_name = stop03_2_v25_candidate_snapshot
candidate_id_set_sha256 = d14c7570230b6c2e3a605c0a3f35d04f3cf4aec62a680838907138570ef84e15
candidate_semantic_digest_sha256 = de34d067fec2d132d6b67bfe7baee251d8dd63c7174fbc556cbae84d243b1b22
```

390/336/54 是本次冻结合同的固定行数，不是未来候选算法的通用阈值。冻结输入必须同时满足：candidate ID 唯一、candidate/source/visual/canonical/derived ID 非空、V25 rule/config/script SHA 与冻结文件一致、运行派生图存在且强 SHA 可重算、所有中心 JOIN 成功。

## 快照语义

`stop03_2_candidate_queue_frozen_v25` 每个 `candidate_id` 一行，保留：

- V25 选择语义：queue、role、score、reason、policy、时间与 segment；
- 强制 lineage：source、visual、canonical visual、derived、dedup/YOLOE/OpenCLIP run；
- 运行输入：派生图路径、SHA-256、size、mtime_ns；
- YOLOE 审计：确定性 JSON、行数、SHA、`labeled`/`no_label`；
- 冻结文件 SHA 和逐候选 `candidate_semantic_sha256`。

派生图 SHA 只读取已有派生图片。不得读取原始视频或用原始素材路径作为 Qwen runtime input。无 YOLOE 标签必须保存 `[]`、count 0、`no_label`，不得丢弃候选。

## 摘要与不可变性

`pipeline_frozen_contracts` 保存候选 ID 集合摘要和全候选语义摘要。首次 commit 必须在备份后单事务执行 migration、写入 390 行、写锁记录并反查 390/336/54。锁定后触发器禁止快照 INSERT/UPDATE/DELETE 以及锁记录 UPDATE/DELETE。

重复 commit 必须先比较 row count、ID digest、semantic digest、rule/config/script SHA；完全一致时直接返回 `IDEMPOTENT_PASS`，不得执行 migration 或 INSERT。任何差异必须 FAIL。首次提交前必须备份，migration、快照、锁和反查在单事务内完成；失败从备份恢复。原 `stop03_2_candidate_queue_items` 的 390 行、ID 集合和逐字段稳定语义摘要在提交前后必须完全一致。

## 模式

- `preflight`：中心 DB `mode=ro` + `query_only=ON`，不创建输出。
- `dry-run`：只写 test-output 审计镜像，中心 DB SHA/mtime/行数必须不变。
- `commit`：备份、单事务 migration+snapshot+lock+readback；仅由获批 finalizer 执行。
- `readback`：只读验证已提交合同、视图和摘要。

preflight/dry-run 固定 `DO_NOT_COMMIT`；正式提交必须由 finalizer 独立记录备份、readback 和第二次幂等结果。
