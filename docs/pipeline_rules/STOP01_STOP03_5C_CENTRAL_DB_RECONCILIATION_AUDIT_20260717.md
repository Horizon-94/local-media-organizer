# Stop01–Stop03-5C 中心数据库反查审计

状态：`PASS / RAW_VIDEO_VISUAL_EXCLUSION_ACCEPTED`

日期：`2026-07-17`

正式中心数据库：

```text
/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite
SHA256 = eb60e23fcd1150a763bceb9a28ad2b7ad765f1979c0597ea8c94bb35242b9b74
```

本审计只读中心数据库和已有派生报告，不读取原始视频，不运行模型，不联网，
不修改中心数据库。

## 1. 总结

Stop01 到 Stop03-5C 的正式结构化元数据、最终候选、Qwen-VL 结果、OCR 结果、
统一 evidence 和传播结果均位于同一个中心 SQLite。已声明外键和补充逻辑关联
检查均无孤儿记录，SQLite integrity 为 `ok`，foreign key error 为 `0`。

需要说明的是，中心库保存的是主流程结果和可追溯信息，不是把所有大文件都塞进
SQLite：

1. 102 个视频中有 5 个 CRM/BRAW 不能按当前画面抽帧方式解码。中心库已经登记
   这 5 个 source asset；它们作为“当前没有画面派生帧”的已知例外保留，不阻塞
   当前视觉流程。以后做 Whisper 时仍可单独尝试音频抽取。
2. 1628 个 OpenCLIP embedding 的 metadata 在中心库，512 维向量 payload 位于
   一个外部 JSONL；中心库通过 `vector_key` 引用它。
3. Qwen-VL clean text 和 OCR text 在中心库；原始 stdout、metrics 和 OCR JSON
   是外部审计文件，中心库保存路径和 SHA。

本项目当前不制作最终 ZIP。后续文本向量优先采用中心数据库自包含的向量 BLOB
合同，避免再增加一套必须依赖绝对路径的外部向量文件。

## 2. 原始文件账目

`source_file_records`：

```text
全部文件记录 = 2681
支持的图片 = 1997
支持的视频 = 102
支持的音频 = 15
支持的文本文件记录 = 299
不支持文件 = 268
```

文件级角色：

```text
canonical = 2388
content_duplicate_alias = 25
unsupported_or_blocked = 268
```

`source_assets` 是 canonical 资产层：

```text
图片 = 1997
视频 = 102
音频 = 15
文本 = 274
合计 = 2388
```

校验：

```text
1997 + 102 + 15 + 299 + 268 = 2681 file records
1997 + 102 + 15 + 274 = 2388 canonical source assets
299 text file records - 25 duplicate aliases = 274 canonical text assets
source asset / canonical file record 双向缺失 = 0
missing/offline source assets = 0
```

## 3. 图片与延时摄影

1997 张原始图片按 A9T-v3 动态识别：

```text
普通图片 = 421
延时摄影序列 = 4
延时摄影序列成员 = 1576
延时摄影代表帧 = 12（每组 first/middle/last）
被代表帧覆盖而跳过单独预览的成员 = 1564
```

校验：

```text
421 普通图片 + 12 延时代表帧 + 1564 被覆盖成员 = 1997
421 普通预览 + 12 延时代表预览 = 433 image visual units
```

四组序列成员数是：

```text
1229 + 108 + 138 + 101 = 1576
```

V25 没有把 12 张延时关键帧全部送入 Qwen-VL，而是根据冻结的通用相似性规则
选出当前项目的 6 张代表帧。`6` 是本素材库结果，不是固定门槛。

## 4. 视频抽帧

中心库有 102 个 canonical video source assets。已有 Step02 正式报告显示：

```text
正常成功 = 94
短视频单帧 fallback 成功 = 3
失败 = 5
成功视频合计 = 97
```

五个失败源是：

```text
3 × .CRM
2 × .BRAW
```

成功输出：

```text
video visual units = 1195
unique derived video frames = 1097
同一 derived frame 的 visual-unit aliases = 98
visual_identity canonical video units = 1016
额外 perceptual/exact duplicate collapse = 81
```

当前约定：

```text
processing_errors 中对应记录 = 0
Step02 model_runs status = done
```

中心库能正确说明“有 102 个视频、97 个视频产生了派生帧”。另外 5 个 RAW 视频
不纳入当前画面分析覆盖率，不删除、不重跑，也不妨碍未来独立的音频路线。

## 5. YOLOE 与 OpenCLIP

视觉输入：

```text
image visual units = 433
video visual units = 1195
total visual units = 1628
```

YOLOE 最终 run：

```text
processed visual units = 1628
units with at least one label = 1374
units with zero detected labels = 254
visual label rows = 5188
```

5188 是检测框/标签记录数，不是图片或帧数。

OpenCLIP 最终 run：

```text
embedding metadata rows = 1628
embedded visual units = 1628
missing embeddings = 0
dimension = 512
```

向量 payload 不在 SQLite BLOB 中，位于：

```text
/Users/yourname/Documents/AI-Local/test-output/stop03-1b-openclip-db-safe-v4_20260709_161500_full/vectors/openclip_vectors.jsonl
```

该文件存在，约 18 MB，1628 行；中心库的 1628 个 `vector_key` 全部指向该文件，
缺失引用为 0。

## 6. V25、Qwen-VL、OCR 与统一 evidence

V25 冻结候选：

```text
Qwen-VL = 336
  image = 131
  video = 205，来自 91 个视频
OCR = 54，来自 10 个视频
candidate rows = 390
unique derived frames = 388
```

390 与 388 的差异来自 2 个派生帧同时具有独立的 Qwen-VL candidate 和 OCR
candidate；不是重复 candidate ID。

正式 Qwen-VL run：

```text
run_id = stop03_3f_dynamic_db_20260716_153210_453034
run items = 336
results = 336
success = 336
duplicate execution keys = 0
```

`stop03_3_qwenvl_results` 全表有 673 行，因为中心库保留了早期 smoke、partial
和 retry run 的历史结果。正式读取必须限定最终 success run，不能把全表 673
误认为当前候选数量。

正式 OCR run：

```text
run_id = stop03_4_ocr_20260716_202238_033459
run items = 54
results = 54
success = 54
reused = 6
duplicate execution keys = 0
```

Stop03-5B：

```text
Qwen evidence = 336 PASS
OCR evidence = 53 PASS + 1 REVIEW
total evidence = 390
unique derived frames = 388
```

## 7. 623 条传播记录的计算

623 不是“205 个视频帧各复制 6 次”。传播表的一行表示：

```text
一个 source Qwen anchor
+ 一个 target 派生帧
+ 一个通过三方语义门的对象标签
```

计算过程：

```text
Qwen 视频锚点 = 205
- 源帧没有 YOLOE 标签 = 18
- Qwen 文本与源 YOLOE 没有共同对象 = 43
= 可进入邻帧枚举的锚点 = 144

144 × 前后最多 3 帧 = 864 个理论邻位
- 视频首尾越界邻位 = 165
= 实际 source-target 候选对 = 699

699
- target 没有 YOLOE 标签 = 74
- Qwen/source YOLOE/target YOLOE 无三方交集 = 79
= 通过语义门的 source-target 对 = 546
```

546 个通过的 source-target 对中：

```text
474 对传播 1 个标签
67 对传播 2 个标签
5 对传播 3 个标签
```

所以：

```text
474 × 1 + 67 × 2 + 5 × 3 = 623 propagation label rows
```

这些记录实际涉及：

```text
有传播输出的 source anchors = 135
有传播输出的 source videos = 49
unique target frames = 427
其中已有直接 Qwen-VL 的 target frames = 56
OCR source rows = 0
```

同一 target 可以由多个相邻高价值锚点覆盖，同一 source-target 对也可以传播多个
共同对象标签，因此 623 大于 546，且 546 大于 427。

## 8. 关联完整性

以下逻辑孤儿计数全部为 0：

```text
derived_assets → source_assets
visual_units → derived_assets
visual_identity → visual_units
visual_labels → visual_units
embeddings → visual_units
V25 candidates → visual_units / derived_assets
Qwen final items/results → V25 candidates
OCR final items/results → V25 candidates
5B evidence → V25 candidates
5C source evidence → 5B evidence
5C target frames → derived_assets
```

数据库检查：

```text
PRAGMA integrity_check = ok
PRAGMA foreign_key_check errors = 0
```

## 9. 通用性结论

正式 Stop01、Step02、Stop03-2、Stop03-3F、Stop03-4、Stop03-5B 和 Stop03-5C
入口均接受 `--db`，默认指向本项目中心 SQLite。正式规则没有以 336、54、390、
623 等当前项目计数作为完成条件；这些数字只属于本素材库运行记录。

下一步不是清理历史 run，也不是重新处理 RAW 视频，而是建立文本向量中心数据库
合同。当前项目数字不能写成通用完成门槛。

## 10. 下一步判定

```text
next stage = Stop03-5D generic text embedding DB contract
final ZIP = not requested
RAW video visual rerun = not required
historical run deletion = not required
```

历史 run 用于审计；正式入口只读取冻结 V25、最终成功 Qwen/OCR run、最新成功
5B view 和最新成功 5C view，因此历史测试不会混入当前正式结果。
