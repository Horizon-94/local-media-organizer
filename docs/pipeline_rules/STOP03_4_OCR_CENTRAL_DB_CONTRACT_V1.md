# Stop03-4 OCR Central DB Contract V1

状态：`IMPLEMENTED_AND_FROZEN`

已验证：

- fake targeted tests：10/10 PASS，最大动态并发 3；
- 单 worker 真实数据库闭环 smoke：3/3 success；
- 三 worker 真实数据库闭环 smoke：3/3 success，观察到 3 个 worker PID；
- 当前正式 OCR evidence：6 条、6 个候选、6 个唯一 execution key；
- 中心数据库 integrity=`ok`，foreign key errors=0；
- 未观察到自动下载、official model cache 写入或原始视频读取。

完整 full run 已由 `stop03_4_ocr_20260716_202238_033459` 完成并通过独立
readback。当前正式入口与验收记录见 `STOP03_4_OCR_DB_NODE_V1.md`。

## 唯一输入

正式入口只读取中心数据库视图
`v_stop03_2_v25_ocr_execution_queue`。不得读取旧 CSV queue，不得重新选择候选，
不得读取原始视频。OCR 输入只能是冻结快照中的 `runtime_visual_file` 派生帧，
执行前必须重算并匹配 `runtime_visual_file_sha256`。

## 本地模型

- Python：`$BUNDLED_PIPELINE_ENVS/media-archive-v06-ocr/bin/python`
- detection：`$MODEL_ROOT/ocr/PP-OCRv6_medium_det`
- recognition：`$MODEL_ROOT/ocr/PP-OCRv6_medium_rec`

必须显式传入 detection/recognition 目录，并关闭文档方向、文档矫正和文字方向模型。
禁止使用无参数 `PaddleOCR()`，禁止使用 `~/.paddlex/official_models` 作为正式模型源。
worker 内必须阻断 socket 网络连接。

## 数据库闭环

`stop03_4_ocr_runs` 保存 run；`stop03_4_ocr_run_items` 保存动态任务状态；
`stop03_4_ocr_attempts` 保存每次尝试；`stop03_4_ocr_results` 保存可复用正式 evidence。

每项执行顺序：

1. 短事务领取一条 pending/可重试任务并标记 running。
2. 关闭事务后执行 OCR。
3. 新短事务写 attempt、result，并更新 run_item。
4. 每完成一条立即更新 run 汇总和追加 progress JSONL。

同一 execution key 的既有 success/no_text evidence 在后续 full run 中直接复用，
不得重复 OCR。resume 必须重置中断遗留 running，跳过 success/no_text。

## 结果语义

- `success`：OCR 正常完成且至少提取一条非空文字。
- `no_text`：OCR 技术执行成功，但未识别出非空文字；不是模型失败。
- `input_fingerprint_mismatch`：派生图 SHA 与冻结快照不一致。
- `failed`：初始化或推理失败；达到 max attempts 后成为终态。

full run 只有在所有 run_item 都为 success/no_text、failed=0、
execution key 无重复、SQLite integrity/FK 均通过时才能报告技术 PASS。

## 模式

- `--mode preflight`：中心数据库只读，不创建表，不运行 OCR。
- `--mode dry-run`：只写 test-output summary，不修改中心数据库，不运行 OCR。
- `--mode run`：需显式确认中心数据库写入；创建 smoke 或 full run。
- `--mode resume`：继续指定 run_id，不创建新 run，不重跑终态。
- `--mode readback`：只读核验指定 run_id。

完整 54 条正式任务必须由用户在本地终端运行。Codex 只允许执行短测试和获批的小批量 smoke。
