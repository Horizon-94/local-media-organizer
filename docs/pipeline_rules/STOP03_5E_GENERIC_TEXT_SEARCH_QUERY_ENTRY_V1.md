# Stop03-5E 完整通用查询入口 V1

状态：`PASS / FROZEN_TEXT_EVIDENCE_BASELINE / REAL_ENTRY_VALIDATION_PASS / FULL_VISUAL_COVERAGE_SUPERSEDED_BY_V2`

## 1. 用途

本入口把已经冻结的文本搜索合同变成正式的单次查询接口。它不针对当前测试文件夹，不固定
文档数、向量数、run ID或素材路径。

本入口只承诺搜索已有 Stop03-5D 文本证据，不承诺覆盖全部 `visual_units`。全视觉召回由
后续 V2 混合入口承担；V1 保留为可复核的详细文本搜索基线。

正式入口：

```text
scripts/03_stop03_visual_analysis/stop03_5e_text_search_query_v1.py
```

## 2. 动态来源

每次请求都从指定 `--db` 自动选择最新成功 Stop03-5D embedding run，并核对模型身份、
维度、dtype、归一化、文档数、向量数、link、完整性和外键。

查询模型必须与所选 run 的模型身份一致。模型只从数据库记录的本地只读路径加载；禁止
联网、下载或写模型目录。

## 3. 请求合同

一次请求只接受一条自然语言 query。请求支持：

```text
group_offset
group_limit
document_offset
documents_per_group
media_type
document_kind
source_content_id
source_relative_path_prefix
time_position_ms_min / max
preview_window_ms = 5000 or 10000
timecode_precision = second or millisecond
```

相同规范化 query、相同筛选、分页、播放窗口和 embedding run 必须生成相同 request ID。
query 原文和 query 向量不写数据库，也不写 JSON/HTML；报告只保存 query SHA256 和长度。

## 4. 响应合同

响应先分页返回唯一文本向量组，再分页展开组内文档。每个组必须包含：

```text
text_vector_id
semantic_score
exact_text_match
matching_document_count
returned_document_count
next_document_offset
```

每个文档必须包含：

```text
document_id
source_content_id
derived_id
canonical_visual_unit_id
media_type
source_relative_path
time_position_ms
timecode
preview_asset_src
environment_code
environment_label
environment_user_confirmation_required
preview_segment_start_ms / end_ms（视频）
```

UI 可根据 `source_content_id` 解析实际素材，在起点 seek，并在5秒或10秒区间终点停止。
查询入口不裁切、不复制、不读取原视频。

## 5. 分页与稳定性

向量组稳定顺序：明确文字匹配、semantic score、text vector ID。组内文档稳定顺序：来源
ID、时间、document ID。响应显式返回总组数、当前 offset 和 next offset，不使用当前测试
项目的固定完成数量。

## 6. 可视资源

每个展示文档必须使用已有派生 JPEG。输出目录中优先创建相对软链接，失败才只读复制。
HTML 仅允许 `assets/...`，禁止绝对路径、`file://` 和父目录跳转。

## 7. 执行模式

```text
preflight  -> 只读合同和请求检查，不写输出，不运行模型
dry-run    -> 只写 test-output 请求计划，不运行模型
query      -> 明确确认后运行本地 query embedding，返回 JSON + HTML
```

三种模式都不写中心数据库，不建立搜索索引。

## 8. 通用 PASS 标准

```text
动态选择最新成功 embedding run
一个请求只处理一条 query
全部合格唯一向量被扫描
分页顺序稳定且 next offset 正确
全部分数有限
全部结果可追踪到 document/source/derived/vector
全部展示图片可访问
场景不确定性被显式暴露
query 原文和 query 向量不落盘
中心数据库哈希不变
SQLite integrity_check = ok
foreign key errors = 0
network/download/original media read = false
```

本入口已完成一次真实本地单 query 验证并标记为 `PASS/FROZEN`。真实验证确认：查询模型、
完整向量扫描、分页响应、派生图片、视频播放区间、场景歧义标签和只读边界均按本合同工作。
具体 run ID、数量和耗时只记录在非规范项目验收报告中，不属于通用完成门槛。

可选搜索索引仍是后续独立阶段，不能与本入口验收混为一谈。只读基线扫描能满足实际规模
和延迟需求时，不应仅为“完成步骤”而建立索引。
