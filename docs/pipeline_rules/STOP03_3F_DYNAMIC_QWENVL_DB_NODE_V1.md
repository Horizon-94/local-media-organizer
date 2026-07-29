# Stop03-3F Dynamic Qwen-VL DB Node V1

状态：`FROZEN`

冻结日期：`2026-07-16`

## 节点职责

从中心 SQLite 的冻结 Qwen-VL 执行队列读取派生帧，使用三个常驻
Qwen-VL worker 动态领取任务，并在每条推理结束后立即通过短事务写回：

- `stop03_3_qwenvl_run_items`
- `stop03_3_qwenvl_results`
- `stop03_3_qwenvl_runs`

本节点不使用固定分片，也不使用固定候选数量作为完成条件。实际总数来自
当前冻结执行队列；只有本次 run 的全部项目最终为 `success` 才通过。

## 冻结入口

正式入口：

`scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_node_v1.py`

节点入口 SHA-256：

`4fcf977329dbdb1c07b827177683dc04fd17ba6b5ec5718b4af874bf593be871`

正式编排器：

`scripts/03_stop03_visual_analysis/stop03_3f_qwenvl_dynamic_db_orchestrator_v1.py`

编排器 SHA-256：

`920ff3e0573fe4cffde31bf4257e490a88e7dda90cc29f188003d2d70c3b58f3`

只读监控器：

`scripts/stop03_monitor/stop03_3f_qwenvl_dynamic_db_monitor.py`

监控器 SHA-256：

`b70f90e1de219a30bd79046eb386a3699d9ad304c2fc0caeab1206cdee2c5393`

## 冻结输入

- Qwen 配置 SHA-256：
  `a50ff9d366793c3e3509faccf0933669c6c08f8802b8e799547f9d64c25c2e6b`
- Prompt 文件 SHA-256：
  `84c95c574720d1fe2b8991b67a2b55a9d5efb975445bc999746aa17d7ce35779`
- Prompt 正文 SHA-256：
  `c233b36e4c17000fb78a258bd9c2a71569f34e87a98e03b647a5eff7afa2dab0`
- 当前历史验收 V25 candidate ID set SHA-256：
  `d14c7570230b6c2e3a605c0a3f35d04f3cf4aec62a680838907138570ef84e15`
- 当前历史验收 V25 semantic digest SHA-256：
  `de34d067fec2d132d6b67bfe7baee251d8dd63c7174fbc556cbae84d243b1b22`

## 冻结运行参数

- scheduling mode：`dynamic_database_claim`
- workers：`3`
- max tokens：`384`
- max attempts：`3`
- backend：
  `mlx_vlm_batch_generate_dynamic_claim_greedy_v1`
- retry strategy：`compact_retry_prompt_v1`

节点入口在运行模型前验证以上实现文件和参数，并重算当前冻结快照的实际
row/count/ID digest/semantic digest 与数据库锁记录是否一致。上面的两个 digest
仅是本次历史验收值，不是未来素材库的固定值。

## 调度与事务约束

1. 每个 worker 独立加载一次模型。
2. worker 完成当前项目后才动态领取下一项目。
3. 领取任务使用短 `BEGIN IMMEDIATE` 事务并原子切换为 `running`。
4. 推理期间不持有 SQLite 写事务。
5. 每条完成后立即使用新短事务写结果和最终状态。
6. 已有 `success` 结果不得重跑。
7. 中断留下的 `running` 项在 resume 时恢复为可领取状态。
8. 非成功项可进入紧凑格式恢复重试，成功项不受影响。
9. 完成判断使用运行时实际队列总数，不使用固定 336 门槛。

## 正式验收证据

正式 run：

`stop03_3f_dynamic_db_20260716_153210_453034`

最终状态：

- orchestrator status：`DYNAMIC_DB_FULL_PASS`
- database run status：`success`
- actual queue count：`336`
- success：`336`
- pending：`0`
- failed：`0`
- review：`0`
- terminal non-success：`0`
- result rows：`336`
- execution key duplicates：`0`
- readback：`PASS`
- SQLite integrity check：`ok`
- foreign key errors：`0`

其中一条确定性超长重复输出经过 `compact_retry_prompt_v1` 第三次尝试后：

- generation tokens：`82`
- inferred finish reason：`stop`
- result status：`success`
- effective prompt SHA-256：
  `e2e13f7e22c83b8f5f0d17c464ec81f576269d64ff2372d1c1b67f50104bf3a3`

## 验收标准

对于任意非空冻结队列，必须同时满足：

```text
success = actual_queue_count
pending = 0
running = 0
terminal_non_success = 0
result_rows = actual_queue_count
execution_key_duplicates = 0
readback_status = PASS
integrity_check = ok
foreign_key_errors = 0
```

不允许将当前验收数据的 `336` 写成通用完成门槛。
