# Stop03 视觉与文本主线最终只读反查及冻结交接

状态：`PASS / FROZEN_HANDOFF / NON_NORMATIVE_PROJECT_EVIDENCE`

本文件记录当前测试项目的最终只读反查。数量是本项目证据，不是未来素材库的固定门槛。
正式入口必须继续按指定数据库、实际文件夹和最新成功run动态计算。

## 1. 当前中心数据库总账

```text
source assets = 2388
  image = 1997
  video = 102
  audio = 15
  text = 274

visual units = 1628
  image = 433
  video frame = 1195

OpenCLIP = 1628 input / 1628 output / 1628 distinct visual units
YOLOE = 1628 input / 5188 labels / 1374 units with detections
V25 candidates = 336 Qwen-VL + 54 OCR
Qwen-VL = 336 success / 0 failed
OCR = 54 success / 0 failed / 6 reused
5B staging = 390 rows / 389 PASS / 1 REVIEW / 0 FAIL
5C propagation = 623 rows / 427 derived targets / 424 canonical targets
5D text documents = 758 / 755 distinct visual units
5D unique vectors = 407 success / 0 failed / 758 links
```

YOLOE没有检出物体的画面不会产生标签行，因此1374不是处理遗漏。全视觉检索覆盖以
OpenCLIP 1628/1628为准；详细文本覆盖以5D的755个不同视觉单元为准，两者不能混称。

## 2. V2全视觉查询交接

正式入口：

```text
scripts/03_stop03_visual_analysis/stop03_5e_hybrid_visual_text_search_v2.py
configs/stop03_5e_hybrid_visual_text_search_v2.json
```

入口动态选择最新完整成功OpenCLIP run及最新成功5D run。每次查询先扫描过滤范围内全部
OpenCLIP视觉向量，再用已有文本和YOLOE标签融合排序。真实验收扫描1628条视觉向量和
407条文本向量；前20项中5项没有5D文本，证明非高价值画面能够通过视觉通道被召回。

查询原文和查询向量不落盘。结果只保存query SHA256、lineage、排名分数、派生预览和播放
区间。横竖屏或旋转可能影响姿态描述时保留原始Qwen证据，由播放器按显示变换处理。

## 3. 数据库及安全检查

```text
SQLite integrity_check = ok
foreign key errors = 0
OpenCLIP visual_unit duplicates = 0
5D execution_key duplicates = 0
database SHA before = database SHA after
database write during V2 query = false
network used = false
download used = false
original media read = false
original media write = false
search index created = false
```

数据库SHA256：

```text
331b76d6b877fd178dce65f60a0ffba3e262715ce697759099ea20172c06f754
```

## 4. 通用性与交接结论

- V2脚本、配置和规范不包含当前项目的固定素材数、run ID或用户绝对路径；
- 当前数量只存在于非规范验收/交接记录；
- 5C仍严格保持高价值帧前后各3帧的Qwen语义传播规则，OCR不传播；
- 全视觉覆盖由已有OpenCLIP检索补齐，不伪装成Qwen传播；
- 旧V1保留为文本证据基线，正式全视觉查询入口为V2；
- 搜索索引暂缓，未来仅在实际规模和延迟需要时另立合同；
- 正式视频跳转、旋转和5/10秒播放属于后续UI/播放器实现。

Stop03视觉与文本主线至全视觉查询已经完成冻结交接。
