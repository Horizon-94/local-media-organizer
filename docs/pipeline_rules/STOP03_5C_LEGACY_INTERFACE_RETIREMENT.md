# Stop03-5C Legacy Interface Retirement

以下旧入口已退出正式运行：

- `stop03_5c_semantic_propagation_v1.py`
- `stop03_5c_semantic_propagation_v2_yolo_gate.py`

它们读取旧的局部 staging SQLite，并含旧时间窗口、旧字段探测或硬编码标签别名，
不能代表当前中心数据库合同。文件仅为历史审计保留；命令行入口会拒绝运行。

新的唯一正式入口：

`scripts/03_stop03_visual_analysis/stop03_5c_qwenvl_yolo_propagation_v1.py`

它自动选择最新成功 Stop03-5B run，使用中心数据库 `derived_assets` 帧序、
`visual_identity` canonical lineage、`visual_labels` 和 `visual_label_terms`，
并执行严格前后各 3 帧的 Qwen/源 YOLOE/目标 YOLOE 三方交集传播。
