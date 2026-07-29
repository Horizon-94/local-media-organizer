# Stop03-4 OCR DB Node V1

状态：`FROZEN`

冻结日期：`2026-07-16`

## 节点职责

从中心 SQLite 的冻结 OCR 执行视图读取派生帧，使用三个常驻 PaddleOCR worker
动态领取任务，并在每条完成后立即通过短事务写回：

- `stop03_4_ocr_run_items`
- `stop03_4_ocr_attempts`
- `stop03_4_ocr_results`
- `stop03_4_ocr_runs`

本节点不使用固定分片，也不把当前 54 条写成通用完成门槛。实际总数来自当前冻结
执行视图；技术完成条件是该 run 的全部实际项目进入 `success` 或 `no_text`，
且没有失败、pending、running 或数据库合同错误。

## 冻结入口

- 正式节点：`scripts/03_stop03_visual_analysis/stop03_4_ocr_db_node_v1.py`
- 正式编排器：`scripts/03_stop03_visual_analysis/stop03_4_ocr_db_orchestrator_v1.py`
- 只读监控：`scripts/stop03_monitor/stop03_4_ocr_db_monitor.py`
- 配置：`configs/stop03_4_ocr_db_v1.json`
- migration：`migrations/20260716_stop03_4_ocr_db_v1.sql`

冻结 SHA-256：

```text
node         = 0739f3ad88853f1edf92c92081aae2a513fcb441780be129d8729c17bc1dbdcb
orchestrator = 94bc2a96dac92a6aca7058e7cec24646d975be90a9c58b32c8cb31d85c0a4080
monitor      = b04666992ac1ba4369f9bafe6a8a7c4b99dd92eb1b85b1720b594292f2593bd8
config       = 2e5142611b41b9bb4d7d5d0db4f829ae44dd1191b312a06d3737229e87a0e1c5
migration    = 1bafd4096da75fa8aeeecaac2e0bb4fe821e3df775af6375a2692c5277bc7844
```

## 冻结本地模型

```text
detection model  = /Users/yourname/Documents/model/ocr/PP-OCRv6_medium_det
recognition model = /Users/yourname/Documents/model/ocr/PP-OCRv6_medium_rec
detection SHA     = efbea5fae8c00c180dd2ce21d3e27d2139c75f84bcd7cf70bfc4778dd91a63f4
recognition SHA   = 96690ab688e0c480d84f21e9b01f7a47c830ea3c31da93b8f40404e84aea05d5
aggregate SHA     = cb05388209680f5bb4e953cf33768d4015e1899f3b617d43312f91055370c217
```

必须显式绑定 detection/recognition 模型，关闭 doc orientation、doc unwarping
和 textline orientation。worker 内阻断 socket 网络。禁止无参数 `PaddleOCR()`，
禁止自动下载或使用 PaddleX official cache 作为正式模型源。

## 冻结运行参数

```text
scheduling_mode = dynamic_database_claim
workers = 3
max_attempts = 3
run_kind = full
limit = 0
source_policy = derived_visual_only
network_policy = blocked_in_worker
```

## 正式验收证据（历史记录，不是通用门槛）

正式 run：

`stop03_4_ocr_20260716_202238_033459`

结果：

```text
actual queue count = 54
success = 54
no_text = 0
failed = 0
pending = 0
running = 0
result rows = 54
reused prior smoke evidence = 6
new OCR inference attempts = 48
worker PIDs observed = 3
average inference seconds = 7.33
execution key duplicates = 0
candidate set equals frozen OCR view = true
missing output = 0
output SHA mismatch = 0
derived input SHA mismatch = 0
OCR text SHA mismatch = 0
empty success = 0
SQLite integrity = ok
foreign key errors = 0
```

所有 54 条输入均为已有派生图片，没有原始视频路径。正式运行前已生成中心数据库备份。

正式节点不固定该 run_id。只读验收会自动选择与当前 OCR 队列集合一致的最新完整
full run，并按该 run 的实际 candidate_count 判断完成。

## 后续边界

Stop03-4 已冻结。后续必须先通过 Stop03-5A 联合质量审计，再进入 Stop03-5B。
