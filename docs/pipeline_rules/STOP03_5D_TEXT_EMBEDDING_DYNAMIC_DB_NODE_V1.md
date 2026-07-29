# Stop03-5D 通用文本向量动态数据库节点 V1

状态：`GENERIC_NODE_FROZEN / PROJECT_ACCEPTANCE_SEPARATE`

## 1. 调度规则

worker 数由 `--workers` 指定。每个 worker：

1. 使用独立进程和独立 SQLite 连接；
2. 从当前 run 原子领取一条唯一文本；
3. 立即结束领取事务；
4. 在没有数据库写事务的情况下运行本地模型；
5. 用新的短事务写回一个向量；
6. 完成后继续领取下一条，直到数据库没有可领取任务。

任务不会静态平均分片。谁先完成，谁继续领取。

## 2. 通用任务身份

run ID 由以下内容共同决定：

```text
最新成功 5B 来源
最新成功 5C 来源
帧级文档 payload
模型文件指纹
模型配置指纹
维度、dtype、归一化和 prompt 规则
```

同一输入和同一合同得到同一身份；换数据库内容、模型或规则后得到新身份。

## 3. 动态队列

```text
pending -> running -> success
                  -> failed -> 自动重试（attempt_count < max_attempts）
```

- success 永不重跑；
- failed 不阻止其他 worker；
- 中断留下的 running 在 resume 时恢复为 pending；
- 每次领取只标记实际交给 worker 的一条任务；
- 推理期间不持有 SQLite 事务或全局数据库锁；
- 每完成一条立即更新数据库并追加一条 `progress.jsonl`。

## 4. 通用完成判断

完成总数来自当前 run 的 `unique_text_count`，不来自代码常量。

```text
remaining = pending + running + 可重试 failed
```

正常完成必须满足：

```text
success = unique_text_count
pending = 0
running = 0
failed = 0
```

文档和 link 数量还必须满足通用合同中的 `D`、`U` 关系。

## 5. 并发与资源

每个 worker 只加载一次模型。worker 数不是数据库合同的一部分，可以根据部署机器内存
调整。当前命令使用多少 worker 只记录在对应 run 中，不成为下一批素材的固定规则。

监控页动态显示：

```text
数据库实际总数
pending / running / success / failed
每个 worker 当前任务和累计完成数
最近平均耗时和动态 ETA
进程树 CPU 与 RSS
最近逐条写回结果
```

## 6. 数据来源与输出位置

- `--db` 指向要处理的中心数据库；
- `--out` 指向本次日志、状态和备份目录；
- 数据源由数据库正式最新视图决定；
- 不扫描某个固定素材文件夹；
- 不读取原始图片或视频；
- 不依据当前数据库的记录数决定成功。

脚本的默认项目根由脚本位置推导，不包含用户名。所有正式参数均可通过命令行覆盖。

## 7. 安全边界

- 本地模型使用 `local_files_only`；
- Hugging Face、Transformers、Datasets 强制离线；
- worker 内阻止 socket 网络连接；
- 模型目录只读；
- run 前在 `--out/backups` 生成数据库备份；
- `preflight`、`dry-run` 不写中心数据库；
- `run`、`resume` 必须带显式数据库写入确认；
- 不建立搜索索引。

## 8. 正式入口

```text
scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py
scripts/stop03_monitor/stop03_5d_text_embedding_db_monitor.py
configs/stop03_5d_text_embedding_db_orchestrator_v1.json
migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql
tests/test_stop03_5d_text_embedding_db_orchestrator_v1.py
```

## 9. 验收记录分离

本节点只冻结通用执行逻辑。任何具体 run ID、记录数量、worker 分配、耗时和当前数据库
哈希均放在项目验收报告中：

```text
docs/pipeline_rules/STOP03_5D_PROJECT_ACCEPTANCE_20260717.md
```
