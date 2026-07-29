# Stop03-5E 通用文本搜索合同 V1

状态：`FROZEN / REAL_QUERY_PASS / VISUAL_PREVIEW_PASS / AMBIGUITY_SAFE / UI_PLAYBACK_DEFERRED / NO_INDEX`

## 1. 白话说明

用户输入一句话，系统用与文档相同的 embedding 模型生成临时 query 向量，再到中心
数据库里寻找意思最接近的文本向量，最后返回对应图片或视频帧的来源信息。

本合同不针对当前测试文件夹，不冻结文档数、向量数或 run ID。

## 2. 正式来源

只读取指定 `--db` 中：

```text
最新成功 stop03_5d_text_embedding_runs
v_stop03_5d_latest_text_documents
stop03_5d_text_vectors
stop03_5d_document_vector_links
```

搜索启动时动态选择最新成功5D run。查询模型的模型指纹、维度、dtype和归一化规则
必须与该 run 一致；不允许拿另一模型的 query 向量混搜。

## 3. 查询处理

```text
用户文字
-> Unicode/空白规范化
-> 长度和空查询检查
-> 本地模型 query prompt
-> float32 归一化 query vector
```

query 向量只在内存中存在，默认不写中心数据库，也不保存用户原始查询。

## 4. 正确性基线

V1 使用：

```text
sqlite_blob_streaming_cosine_v1
```

流程：

1. 从最新成功5D run分块读取唯一向量 BLOB；
2. 校验维度、字节长度和有限值；
3. 因文档向量和 query 都已归一化，点积就是 cosine；
4. 先按 `text_vector_id` 形成唯一文本结果组；
5. 再通过 link 展开为一个或多个文档。

分块大小是内存控制参数，不是数据量门槛。总向量数来自数据库。

## 5. 为什么先不建索引

SQLite 当前有 FTS5，但默认中文分词不等于可靠的中文语义搜索。FTS5 也不能替代向量
cosine。因此 V1 不创建 FTS 或第三方向量索引。

直接扫描的价值：

- 不安装依赖；
- 结果容易核对；
- 可以作为以后任何索引实现的正确性基线；
- 素材库较小时无需维护额外索引。

只有实际延迟测试证明扫描不够快，才进入独立索引阶段。索引必须与基线查询做 recall
对照，不能只因为数据量看起来大就自动切换。

## 6. 结果分组和复用

相同文本可能对应多个文档。如果直接展开，会让同一种文字占满结果页。

因此先返回唯一文本向量组：

```text
text_vector_id
semantic_score
exact_text_match
document_count
```

再按分页展开组内文档。组内稳定顺序使用来源 ID、时间位置和 document ID，不能依赖
SQLite 未声明的自然行顺序。

## 7. 排序

V1 不使用难以解释的加权总分。对外分别返回：

```text
exact_text_match
semantic_score
```

稳定顺序：

1. 明确的规范化文字匹配优先；
2. semantic score 从高到低；
3. text_vector_id；
4. 展开文档时按 source、time、document ID。

每条结果必须带 run ID、document ID、vector ID 和分数，保证可追踪。

## 8. 通用过滤

合同允许：

```text
media_type
document_kind
source_content_id
source_relative_path_prefix
time_position_ms_range
```

过滤器只根据中心数据库字段工作，不扫描素材文件夹，也不读取原始媒体。

## 9. 可视结果和视频区间

每一个展开后的搜索结果都必须显示 `derived_assets.derived_path` 指向的现成派生 JPEG。
HTML 只引用输出目录内的 `assets/<safe_name>.jpg`：优先创建相对软链接，失败时只读复制。
禁止在 HTML 中使用原始素材路径、绝对路径或 `file://`。

视频结果还必须返回以命中帧为锚点的名义5秒区间：

```text
preview_segment_start_ms
preview_segment_end_ms
preview_segment_nominal_duration_ms
```

区间默认从命中帧前2秒开始；靠近开头时从0开始。正式播放器必须按真实视频结尾截断。
本阶段不读取、裁切或复制原视频，也不生成视频片段文件。

对用户显示的时间不得只写累计秒数。小于一小时使用 `MM:SS.mmm`，达到一小时使用
`HH:MM:SS.mmm`。默认保留毫秒，也允许界面选择只显示到秒；数据库中的原始毫秒值保持
不变，便于程序精确定位。

场景环境采用非破坏性通用标签：

```text
indoor               -> 室内
outdoor_day          -> 白天户外
outdoor_night        -> 夜间户外
outdoor              -> 户外（时段未确定）
night_or_indoor      -> 夜间/室内（待确认）
indoor_or_outdoor    -> 室内/户外（待确认）
unknown              -> 未确定
```

图片只使用本帧已有 Qwen 描述。视频同时参考当前点附近最多前2条和后2条已有 Qwen 描述；
如果证据同时支持夜间和室内，不得强行二选一，必须显示“夜间/室内（待确认）”并返回
`environment_user_confirmation_required=true`。该标签不覆盖或修改原始 Qwen 输出。

5秒和10秒只是播放窗口选项。正式 UI 根据 `source_content_id` 找到素材，跳到
`preview_segment_start_ms`，播放到 `preview_segment_end_ms` 后停止。播放器按素材真实时长
截断，不要求提前生成或保存新视频片段。

## 10. 通用完成标准

设最新成功5D run中：

```text
D = document_count
U = unique_text_count
```

preflight 必须确认：

```text
正式视图 documents = D
成功 unique vectors = U
links = D
所有向量维度、dtype、BLOB长度和归一化合同一致
模型指纹存在且 query 端可匹配
execution_key duplicates = 0
SQLite integrity_check = ok
foreign key errors = 0
每个展示文档都有可访问的派生 JPEG
视频展示文档都有预览区间元数据
HTML 图片引用全部为 assets/ 相对路径
```

不得比较某个测试项目的固定 D 或 U。

## 11. 查询 smoke 入口

正式入口：

```text
scripts/03_stop03_visual_analysis/stop03_5e_text_search_smoke_v1.py
```

入口动态读取指定数据库，不包含固定 run ID、文档数或向量数。向量 BLOB 使用
`fetchmany(vector_scan_chunk_size)` 分块读取；过滤先在数据库中生效，再做 cosine。

真实 smoke 仅接收3至5条人工查询。报告只保存查询哈希和长度，不保存原始查询或 query
向量。JSON和HTML只写指定 test-output，中心数据库保持只读。

## 12. 阶段边界

已完成：

```text
合同文档
配置
只读 preflight / dry-run execution plan
targeted tests
通用只读 query smoke 入口
假向量端到端测试
```

少量真实 query smoke 已在本地完成，查询、cosine扫描、结果追踪、缩略图资源和视频区间的
技术检查均通过。用户确认基础搜索整体可用。对于无法可靠区分的室内/夜间户外结果，
冻结接口明确暴露歧义并允许后续 UI 人工确认。本阶段始终不写数据库、不创建索引。

基础查询入口按本合同冻结。完整通用查询入口是下一阶段；播放器 UI 和可选索引是更后面
的独立阶段，不得静默改动本合同的模型身份、分块 cosine 基线、追踪字段或数据库只读边界。
