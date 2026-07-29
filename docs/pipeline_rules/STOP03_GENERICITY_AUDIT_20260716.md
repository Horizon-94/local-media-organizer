# Stop03 Production Genericity Audit 2026-07-16

## 已修正的正式入口

- Stop03-3F Qwen-VL frozen node：不再比较某次 V25 的固定 digest；改为重算并
  校验当前冻结快照记录的 row/count/ID digest/semantic digest。
- Stop03-4 OCR frozen node：不再固定 acceptance run_id；改为选择与当前 OCR
  队列一致的最新完整 full run。
- Stop03-5A joint audit：不再固定 Qwen/OCR run_id，不再用 336/54/390 或固定
  REVIEW 数作为 PASS 门槛；改为动态 run 选择和集合不变量。
- Stop03-5B unified staging：所有数量、摘要和 staging run ID 均由当前数据库
  evidence payload 生成。

## 保留但不作为生产入口的历史快照

以下文件包含固定数量，是特定测试项目或冻结快照的验收证据，不得作为新素材库
的通用运行入口：

- `stop03_2_v25_candidate_contract_lock.py`：V25 已提交不可变快照合同。
- `finalize_stop03_3_v25_contract_and_qwenvl_smoke.py`：V25 合同提交期 finalizer。
- `stop03_3g_full336_standalone.py` 与 `run_stop03_3g_full336_dbflow.sh`：
  336 条专项诊断。
- `stop03_3f_qwenvl_batch_db_orchestrator_v1.py`：固定批次诊断。
- `stop03_5b2_yolo_label_staging_patch.py`：旧 manifest/local-SQLite staging。

这些历史文件不删除、不改写冻结结果，也不进入新的正式接口索引。未来生产运行
必须从当前中心数据库视图发现实际集合大小，并以集合完整性而非固定数字判定完成。
