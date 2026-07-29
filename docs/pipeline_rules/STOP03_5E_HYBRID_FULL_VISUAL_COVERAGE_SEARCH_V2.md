# Stop03-5E 混合全视觉覆盖搜索 V2

状态：`PASS / FROZEN / TARGETED_TESTS_PASS / PREFLIGHT_PASS / DRY_RUN_PASS / REAL_QUERY_PASS`

## 1. 修复目标

V1 文本搜索只覆盖已有 Qwen-VL、OCR 和有限传播产生的文本文件，不能代表全部派生画面。
V2 不扩大 Qwen 传播范围，也不重跑模型，而是把已经存在的三类证据合并查询：

- OpenCLIP：所有 `visual_units` 的全量画面召回；
- Stop03-5D：Qwen-VL、OCR、有限传播形成的详细文本语义；
- YOLOE：已有物体标签的明确匹配和加权。

YOLOE 是物体检测证据，不是当前文本搜索使用的语义向量。全视觉向量覆盖来自 OpenCLIP。

## 2. 通用完成门

完成数量一律从指定数据库动态计算，不固定素材数、图片数、视频帧数或 run ID。

```text
最新 OpenCLIP run = success
OpenCLIP input_count = output_count
OpenCLIP distinct visual_unit_id = visual_units count
JSONL payload 中每个数据库 embedding 均存在
全部向量维度、有限值、归一化和 SHA256 正确
每次查询扫描全部过滤后视觉向量
文本向量和 YOLOE 只作为附加排序证据，缺少它们不能排除视觉单元
全部结果可追踪到 visual_unit/source/derived/OpenCLIP run
全部展示图片来自已有派生图片
中心数据库不写入
原始媒体不读取
网络、下载和搜索索引均为 false
```

## 3. 排序

视觉、文本和 YOLOE 使用加权 reciprocal-rank fusion。不同模型的原始 cosine 不直接相加，
避免把不在同一向量空间的分数当成同一种尺度。OpenCLIP 是每个结果必有的基础通道；文本
与物体标签只提升有相应证据的结果。

视频相邻抽帧在返回页面中按来源和时间窗口去重，但所有合格向量仍必须先参加扫描。搜索
结果给出5秒或10秒播放区间；正式播放器跳转仍由后续 UI 实现，不裁切原视频。

## 4. 安全边界

正式入口只读中心 SQLite、OpenCLIP JSONL、已有向量 BLOB 和派生 JPEG。查询原文通过
stdin 交给本地 OpenCLIP 子进程，不放进命令行，也不写 JSON、HTML 或数据库。只保存
query SHA256 和长度。

V1 保留为“详细文本证据基线”，不再代表全视觉覆盖。V2 只有通过真实本地单 query 验证
后才能冻结；随后才能进行 Stop03 最终只读反查和冻结交接。

## 5. 方向敏感描述

人物“站立、侧卧、仰卧”等描述可能受到派生帧显示旋转影响。V2必须保留上游原始文本，
不得仅凭搜索层猜测旋转方向后改写 Qwen 证据。这类姿态词属于可检索但可复核的描述，
不作为素材物理方向的绝对结论。

正式播放器应按素材的显示变换/旋转元数据决定横屏或竖屏播放；该动作属于 UI/播放层，
不能通过修改搜索文本代替。当前数据库没有可靠旋转合同的素材应保持原派生预览方向，并
允许用户按画面复核。

## 6. 冻结结论

V2 已通过正式中心数据库的全量 payload preflight、dry-run 和一次真实本地混合 query。
真实查询确认全部过滤后视觉向量先参与扫描，文本与YOLOE只作为附加排序证据；结果图片、
时间区间、trace、数据库只读和离线边界均通过。具体项目数量只记录在项目验收与最终交接
审计中，不属于本通用规则。
