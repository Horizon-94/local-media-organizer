# Stop03-3 Qwen-VL Central DB Contract V1

状态：`READY_FOR_MIGRATION_REVIEW`
输出合同：`qwenvl_output_contract_v2.0`

## 唯一输入

生产模式只读取中心数据库视图 `v_stop03_2_v25_qwenvl_execution_queue`，不读取旧 manifest、不扫描 Finder 标签、不重新选候选、不读取原始视频。OCR 视图独立存在，但本编排器不执行 OCR。

每条执行项原样保留 candidate/source/visual/canonical/derived ID、candidate role、reason codes、policy version、media/time 和 runtime 派生图 SHA。Qwen 只生成可见画面语义，不重复生成或替代 YOLOE 标签。

## 生成参数

```text
max_tokens = 384
temperature = 0.0
top_p = 1.0
contract = qwenvl_output_contract_v2.0
```

正式 `run`/`resume` 的 max_tokens 小于 384 必须拒绝。只有 `smoke --allow-low-token-debug` 可降低 token，并且该结果只能用于 debug，不得升级为生产 success。

## 强指纹和幂等

模型强指纹必须同时保存 `model.safetensors`、`config.json`、配置声明的 tokenizer 正式文件摘要，以及模型目录全文件相对路径/大小/内容 SHA 的稳定清单摘要。缺失文件记录为 `MISSING` 并阻止执行，绝不下载。`execution_key` 为以下字段的稳定 SHA-256：

```text
candidate_id + runtime input SHA + aggregate model fingerprint SHA + prompt SHA
+ output contract + max_tokens
```

中心 DB 是正式状态来源；CSV/JSONL 只是审计镜像。每项使用短事务更新。resume 跳过 success，只选择 pending/running/failed/review/parse_failed/missing_required_fields/input_fingerprint_mismatch；同一 execution key 不得生成两个成功结果。人工批准后的全量新 run 会跳过已成功的同指纹 smoke 项；若同指纹项不是 success，必须使用其原 run_id resume，禁止覆盖或重复注册。

## 结果门

必须使用 `qwenvl_output_contract_v2.py` 分离 clean text、raw stdout 和 metrics。正式 clean text 写中心 DB；raw stdout、stderr 和 metrics 写 test-output，DB 保存路径、SHA、metrics JSON、preview、token/显存指标及全部输入/模型/prompt/config/script 强指纹。成功和非成功尝试都保留结果审计行，但只有通过全部门禁的记录可标记 `success`。

以下任一状态不得写 success：

- 子进程非 0、空输出；
- runtime file SHA 与冻结快照不一致；
- candidate/canonical 等强制 ID 缺失；
- wrapper、内部绝对路径或运行指标残留；
- generation tokens 达到上限或句尾疑似截断；
- 固定“概括/元素/检索价值”结构缺失。

状态只能是：`success`、`truncated`、`parse_failed`、`missing_required_fields`、`input_fingerprint_mismatch`、`failed`，另有队列内部 `pending/running/review`。

## 正式 smoke 边界

获批 finalizer 只运行中心视图中的 3 条、384-token、单 worker smoke。生产 smoke/run/resume 禁止使用内存模拟路径；正式结果只写中心数据库，JSON/CSV/局部 SQLite 只能作为审计镜像。
