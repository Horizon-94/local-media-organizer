# Stop03-2 通用视频候选规则重审与 V22 设计（DR V17）

状态：审计与设计完成，V22 尚未实现、尚未运行。
审计日期：2026-07-10
适用范围：通用本地素材库，不针对任何单一项目、作物、机器或拍摄主题。

## 1. 本轮边界与状态口径

本轮只做审计和规则设计，不写 V22 脚本，不写中心 SQLite，不生成 V22 测试输出。

本轮实际读取：

- 中心 SQLite：`media_archive.sqlite`
- V14、V17、V19、V20、V21 脚本
- V14、V17、V19、V20、V21 已有 summary、manifest、video budget report
- 已有 Step02 派生帧，用于低成本 16x5 灰度网格可行性审计
- 已有 OpenCLIP JSONL 向量 payload，用于确认向量可读性

本轮未读取原始视频内容，未运行 YOLOE、OpenCLIP、Qwen-VL、OCR，未联网、下载或安装依赖，未修改原始素材。

状态必须分开解释：

- `technical_status`：程序、安全边界、输出一致性和泄漏检查是否通过。
- `policy_status`：候选规则是否真正执行、是否改善覆盖/内容/去重/OCR，取值只能是 `PASS`、`REVIEW`、`FAIL`。
- 旧 summary 的 `validation_status=PASS` 只能证明当时脚本定义的技术门槛通过，不能自动等价于策略 PASS。

当前 V22 状态：

```text
technical_status = NOT_RUN
policy_status = REVIEW
reason = design_complete_pending_user_confirmation_and_implementation
```

## 2. 数据快照与可用证据

中心库当前审计结果：

```text
visual_units = 1628
input_video_visual_units = 1195
input_image_visual_units = 433
video_source_group_count = 97
normal_video_group_count = 91
screen_capture_video_group_count = 6
visual_labels = 5188
labeled_visual_units = 1374
embeddings = 1628
manual_high_value_visual_seeds = 125
```

当前质量字段限制：

```text
video visual_units.near_black = 0 for all 1195 rows
video visual_units.luma_mean is NULL for all 1195 rows
video visual_units.luma_std is NULL for all 1195 rows
near_dup_group_id populated rows = 0
```

因此 V17 以后只依赖 DB 的 `near_black/luma_*` 不能证明真实执行了黑帧检查；V22 必须读取已有派生帧做实际质量统计。V14 已读取派生图做过黑帧检查，当时 1628 个派生视觉单元没有黑帧或无效文件泄漏。

## 3. V14 / V17 / V19 / V20 / V21 复盘

### 3.1 量化对照

| 版本 | 技术状态 | 策略状态 | 视频候选 | 普通视频覆盖 | 最大覆盖空白 | 无 YOLOE 标签帧 | 普通视频 OCR | 核心结论 |
|---|---|---|---:|---:|---:|---:|---:|---|
| V14 | PASS | REVIEW | 132 | 91/91 | 90s；5 个视频大于 30s | 6/132，4.55% | 0 | 高信号倾向较好，但时间覆盖不均 |
| V17 | PASS | REVIEW | 168 | 91/91 | 27s；无视频大于 30s | 23/168，13.69% | 0 | 覆盖明显更好，但混入更多平庸/无标签帧 |
| V19 | PASS | REVIEW | 168 | 91/91 | 与 V17 相同 | 与 V17 相同 | 2 | 角色拆分有效；补充和最终去重未产生效果 |
| V20 | PASS | FAIL | 168 | 91/91 | 与 V17 相同 | 与 V17 相同 | 2 | 名义上加入内容感知，实际代码路径未接入 |
| V21 | PASS | FAIL | 168 | 91/91 | 与 V17 相同 | 与 V17 相同 | 2 | 主要是版本/字段改名，策略仍未执行 |

统一覆盖空白定义：在每个普通视频的已有 Step02 时间范围内，将首段空白、相邻候选间隔和末段空白取最大值。该指标只基于已有派生帧时间，不读取原始视频。

本轮还对 3 个 V14 独有高标签帧和 3 个 V17 独有零标签帧做了小样本派生图目视审计。V14 样本均有明确人物、设备、文字或社会场景上下文；V17 零标签样本中既有较平淡的水面/石墙画面，也有结构清楚的道路建筑和夜间城市画面。结论是：无 YOLOE 标签表示“现有主体证据弱”，不等于画面一定平庸。V22 应保留 coverage，并用派生图结构和已有向量做窗口内择优，不能把零标签作为硬拒绝。该 3+3 抽样只用于发现机制问题，不作为总体质量比例。

### 3.2 V14

优点：

- 132 个视频候选中位 YOLOE box 数为 3；V17 为 2。
- 无标签帧比例仅 4.55%，明显低于 V17 的 13.69%。
- 包含对象集合变化、信息跃迁、OCR 区域出现和人物构图等已有信号。
- 黑帧检查实际读取了派生图；录屏不进 Qwen-VL；长视频尾部信号会被抑制。

问题：

- 不是纯粹的“高信号帧”：132 帧中有 21 帧来自 `video_min_one_best_available_frame`，本质是最低覆盖 fallback。
- 91 个普通视频虽然都有至少一帧，但有 5 个视频最大覆盖空白超过 30 秒，最大 90 秒。
- 先按信号打分再做时间间隔控制，无法保证长视频的每个主要时段有入口。
- V14 的绝对分数体系与 V17 以后不同，不应直接混用绝对分值。

结论：V14 适合作为高信号证据源和窗口内择优输入，不适合作为唯一覆盖主策略。

### 3.3 V17

优点：

- 91/91 普通视频都有覆盖。
- 最大覆盖空白从 V14 的 90 秒降至 27 秒；没有视频超过 30 秒。
- P90 内部候选间隔为 18 秒，符合 Step02 约 3 秒采样、stride=6 的实际结果。
- 代码层已有尾部替换、黑帧替换和标签 Jaccard 去重入口。

问题：

- 直接使用中心向外的固定索引锚点，没有在时间窗口内真实比较内容。
- 168 帧中 23 帧没有 YOLOE 标签，说明 coverage 不能等同 high value。
- `jacc()` 把两个空标签集合判为相似度 1.0，可能在没有视觉证据时误判重复。
- 黑帧判断依赖当前为空的 DB luma 字段，不能作为未来数据的充分质量门。

结论：V17 适合作为覆盖骨架，不应把全部结果命名为高价值帧。

### 3.4 V19

V19 的角色拆分方向正确：

```text
video_coverage_keyframe = 130
video_coverage_high_signal_overlap = 38
video_high_signal_supplement = 0
```

但 V19 的补充机制在当前数据上结构性失效：

```text
V14 视频帧 = 132
V17 视频帧 = 168
V14 与 V17 重合 = 38
V14 独有 = 94
```

94 个 V14 独有帧中：

- 61 帧属于 Step02 帧数小于 18 的 58 个视频；旧 `supplement_cap_for_group()` 对这些视频返回 0，循环直接退出，没有 reject 统计。
- 剩余 33 帧全部距最近 coverage 帧小于 15 秒：17 帧相距 3 秒、9 帧相距 6 秒、7 帧相距 9 秒，全部被 `v14_supplement_min_gap_ms=15000` 拒绝。

所以 `high_signal_supplement_added_count=0` 不是“V14 都已与 coverage 重合”，而是“coverage 每约 18 秒一帧 + supplement 至少离 coverage 15 秒”在约 3 秒采样上几乎不可能同时满足。

最终去重为 0 也可解释：V17 已选相邻对中 77 对有 66 对恰好相距 18000ms，而最终规则使用严格的 `gap < 18000`。本次数据按现有标签规则确实没有命中最终去重条件。

### 3.5 V20：16x5 与内容感知审计

V20 定义了以下函数：

- `content_grid_signature()`
- `content_score_for_frame()`
- `choose_best_near_anchor()`
- `v20_coverage_dedup()`

但 `run()` 仍然直接调用 V17 的 `choose(fs, labels, args.video_stride)`；上述函数均未进入正式选择路径。另有两个直接证据：

- 文件中没有导入 `PIL.Image`，`content_grid_signature()` 却引用 `Image`。因为函数未被调用，所以运行没有暴露 `NameError`。
- `v20_anchor_local_best_shift_count`、`v20_coverage_dedup_drop_count` 和 `v20_anchor_drop_no_candidate_count` 只出现在 summary 读取处，没有执行路径写入。

因此：

```text
V20 是否读取派生帧计算 16x5 grid = 否
16x5 grid 是否参与择优 = 否
16x5 grid 是否参与去重 = 否
v20_anchor_local_best_shift_count = 0 的原因 = 锚点局部择优未调用
v20_coverage_dedup_drop_count = 0 的原因 = coverage dedup 未调用
```

V20 的候选 `visual_unit_id` 集合与 V19 完全相同，角色映射也完全相同。V20 页面观感即使更好，也不能归因于 V20 候选规则；现有三个输出目录本身只含 manifest/summary/video budget，没有可证明候选变化的 HTML 产物。

### 3.6 V21

V21 与 V20 的有效差异主要是版本名、统计字段名和函数名：

- `v20_coverage_dedup` 改名为 `v21_coverage_dedup`，仍未调用。
- summary 新增 `v21_anchor_total_count`、refill 等字段，但没有执行路径累计。
- `run()` 仍调用 V17 `choose()`。

因此：

```text
v21_anchor_total_count = 0 的原因 = 没有建立或累计 V21 anchor
v21_anchor_local_best_shift_count = 0 的原因 = 局部择优未调用
v21_coverage_dedup_drop_count = 0 的原因 = coverage dedup 未调用
v21_min_one_refill_count = 0 的原因 = refill 未实现到运行路径
```

V17、V19、V20、V21 的 168 个视频候选 ID 完全相同；V19、V20、V21 的 130/38 角色映射也完全相同。V21 是技术 PASS、策略 FAIL。

## 4. 候选角色必须拆分

`qwenvl_high_value` 可暂时保留为兼容 queue_type，但不能再把队列中所有视频帧统称为 high value。V22 必须使用独立角色：

| 角色 | 含义 | 是否直接进入最终 Qwen-VL 队列 |
|---|---|---|
| `video_coverage_keyframe` | 时间窗口代表；保证检索覆盖，不保证是强内容帧 | 是 |
| `video_high_signal_keyframe` | 高信号候选池中的帧；尚未与覆盖层合并 | 否，先参与窗口择优/补充判定 |
| `video_coverage_high_signal_overlap` | 同时满足覆盖代表和高信号证据 | 是，优先保留 |
| `video_high_signal_supplement` | 覆盖层之外、内容显著不同且满足间距的少量补充 | 是 |

V14 的 `video_min_one_best_available_frame` 应重新归为 coverage fallback，不应继续当作高信号证据。

## 5. V22 总原则

```text
V22 = Time Windows for Coverage
    + High Signal Competes Inside Windows
    + Small Novel Supplements
    + Real Derived-Frame Quality
    + Exact/Grid/Vector Dedup with Refill
    + Separate Narrow OCR
```

关键改变不是“在 V17 结果后追加 V14”，而是让高信号帧先参与覆盖窗口的代表选择。这样 V14 独有但距 coverage 仅 3/6/9 秒的帧可以替换平庸锚点，而不必违反 15 秒 supplement 间距。

## 6. V22 详细规则

### 6.1 输入与预检

只读加载：

- `visual_units`、`source_assets`、`derived_assets`
- `visual_labels` 和 bbox
- `manual_high_value_visual_seeds`
- `step02_image_timelapse_keyframes`
- `embeddings` metadata
- V14 Qwen-VL manifest，作为高信号历史证据
- `vector_key` 指向的已有 JSONL payload

预检必须验证：

- DB 和必要表存在。
- 输出目录位于 `$USER_HOME/Documents/AI-Local/test-output`。
- 派生帧路径不在原始素材目录，且只读。
- 不加载任何模型，不存在远程模型名和下载路径。
- V14 manifest 可读，缺失时明确降级而不是伪造 V14 高信号。
- Pillow 已在指定环境中可导入；缺失时 BLOCKED，不安装。

### 6.2 录屏与普通视频分组

- 已知 screen capture：不进入任何 Qwen-VL 视频候选角色，只进入 OCR 分支。
- 普通视频：进入 coverage/high-signal 流程。
- 当前 screen capture 判断只能使用已有严格路径/元数据规则。持续高频 `screen` 标签可产生 `screen_capture_review`，但不能单独把普通拍摄屏幕的视频自动改成录屏。
- 每个分组必须输出判断依据和命中关键词。

### 6.3 派生帧质量与尾帧

所有将参与选择的帧必须实际读取已有派生 JPG，计算：

```text
luma_mean
luma_std
black_pixel_ratio
grid_16x5_available
```

硬拒绝：

```text
missing_or_invalid_derived_frame
near_black: mean <= 8 and std <= 5
near_black: black_pixel_ratio >= 0.985 and mean <= 16
```

长于 30 秒的视频使用尾部保护：

```text
tail_window_ms = clamp(round(effective_span_ms * 0.05), 6000, 30000)
```

尾部保护区默认不进入 Qwen-VL coverage 或 supplement。30 秒及以下短视频允许使用最后一帧以保证最低覆盖。尾部候选被排除后必须从同一窗口向前 refill，不能直接造成普通视频无覆盖。

### 6.4 Coverage 时间窗口

默认：

```text
coverage_window_ms = 18000
```

按有效时间范围创建连续窗口，而不是按数组索引中心向外取 stride。目标数量由有效时长和实际存在的 Step02 帧决定，不按固定百分比硬凑。

每个非空窗口建立一个 anchor，并在窗口内比较所有合格帧。每窗最多先选 1 个 coverage 代表。短视频至少 1 个；普通视频最终必须至少 1 个。

窗口内排序证据，按优先级分层而不是混成一个不可解释的大分数：

1. 硬门：非黑、非无效、非受保护尾帧。
2. 高信号角色：V14 真高信号证据或 V22 通用高信号证据。
3. 已有通用主体证据：人物/社会、车辆/机器/设备、动物/生命、文字/屏幕/文档、场所/结构/道路、自然/土地/植物、食物/物件上下文。
4. bbox 质量：主体面积、中心性、是否边缘裁切；仅用已有 YOLOE bbox。
5. 16x5 结构：亮度标准差和相邻格差异，用于排除平坦/无结构帧。
6. 相对 anchor 时间距离，作为最后 tie-breaker。

禁止：

- 写项目专用组合加分。
- 把 `person + 某项目词`、`某机器 + 某项目词` 写成专用规则。
- 把 16x5 中心亮度差解释成“检测到中心主体”；中心主体只能由已有 bbox 支持。

必须统计：

```text
coverage_anchor_total_count
coverage_window_total_count
coverage_window_candidate_evaluated_count
coverage_selected_count
coverage_anchor_exact_selected_count
coverage_anchor_local_best_shift_count
coverage_drop_no_candidate_count
```

对于当前数据，91 个普通视频只要 V22 路径真正执行，`coverage_anchor_total_count` 不可能为 0。

### 6.5 高信号定义与合并

高信号是相对证据，不等同于任意 YOLOE positive。可接受证据包括：

- V14 中非 `video_min_one_best_available_frame` 的视频候选。
- 通用主体类别丰富且置信/面积足够。
- 相邻帧之间已有标签集合明显变化。
- 检测数量或类别数出现显著信息跃迁。
- 大且清晰的文字承载区域出现。
- 人物/主体 bbox 中心性和完整性明显优于同窗口其他帧。
- 与同窗口其他帧在已有向量或 16x5 grid 上有明确内容差异。

V14 `candidate_score` 只在同一视频内做排序参考，不与 V22 coverage 分数直接比较。

合并规则：

1. 高信号候选先进入所属 coverage 窗口竞争。
2. 若高信号候选成为窗口代表，角色为 `video_coverage_high_signal_overlap`，并累计 local-best shift。
3. 未成为窗口代表的高信号候选只有同时满足内容新颖、质量门和间距门时才可成为 supplement。
4. 若高信号候选离 coverage 很近但明显更好，替换 coverage 代表；不要以 supplement 形式扎堆。

supplement 默认上限按时长而非 Step02 帧数：

```text
effective_span <= 60s: cap 1
60s < effective_span <= 300s: cap 2
effective_span > 300s: cap 3
```

supplement 默认间距：

```text
high_signal_supplement_min_gap_ms = max(9000, coverage_window_ms / 2)
```

此外必须满足至少一个内容新颖条件：

- vector cosine 低于近重复阈值；或
- 16x5 grid 明显不同；或
- 非空标签集合 Jaccard 明显下降。

### 6.6 16x5 grid

本次审计已实际从 1195/1195 个已有视频派生帧成功读取 16x5 灰度网格，因此 V22 可以实现，不需要模型。

每帧保存内存态的 80 维灰度值，并计算：

- `grid_luma_mean`
- `grid_luma_std`
- `grid_structure`：水平/垂直相邻格绝对差均值
- `grid_raw_mad(a,b)`：两帧 80 格绝对差均值
- `grid_centered_corr(a,b)`：去均值后的相关性，降低整体曝光变化影响

grid 只作为低成本结构/相似代理，不作为语义理解。缺图或解码失败必须计数并触发技术 FAIL 或明确 BLOCKED，不允许静默返回空 signature 后继续宣称 grid enabled。

### 6.7 已有视觉向量

本次审计确认：

```text
vector_payload_found = true
DB embedding rows = 1628
JSONL payload rows = 1628
DB embedding_id set == JSONL embedding_id set
dimension = 512
payload path = $USER_HOME/Documents/AI-Local/test-output/stop03-1b-openclip-db-safe-v4_20260709_161500_full/vectors/openclip_vectors.jsonl
```

`vector_key` 格式为：

```text
jsonl:<absolute_jsonl_path>#<embedding_id>
```

因此 V22 可以直接读取已有向量，不重跑 OpenCLIP。当前数据的预期状态应为：

```text
vector_dedup_status = enabled_existing_jsonl_payload
vector_payload_found = true
```

阈值校准审计：1098 对相邻 Step02 帧的向量 cosine 中位数为 0.9148，P90 为 1.0；16x5 raw MAD 中位数为 6.525。V17 已选帧只有 77 对相邻候选，其中 66 对恰好相距 18 秒，向量 cosine P99 为 0.9722。使用 0.985 的向量近重复门槛并要求 grid/label 支持，能优先处理原始候选池中的强重复，同时避免仅凭语义相似删除覆盖帧。该阈值仍需在 V22 HTML 中 REVIEW，不是无需验证的永久常量。

若未来 DB 指向的 payload 不存在、行数/ID/维度不一致：

```text
vector_dedup_status = skipped_no_vector_payload
vector_payload_found = false
policy_status = REVIEW
```

不得假装使用向量，也不得为修复 payload 重跑 OpenCLIP。

### 6.8 去重与 refill

去重只在同一视频内执行。顺序：

1. exact identity：相同 `derived_id`、派生 SHA 或相同物理派生路径。
2. coverage 窗口内择优：同一窗口只保留最佳代表。
3. coverage 跨窗口近重复检查。
4. high-signal supplement 合并检查。
5. 最终视频候选检查。

默认近重复共识规则：

```text
time_gap_ms <= 12000
AND at least one visual condition:
    vector_cosine >= 0.985
    OR (grid_raw_mad <= 6 AND grid_centered_corr >= 0.98)
AND supporting condition:
    nonempty_label_jaccard >= 0.80
    OR both visual conditions agree
```

两个空标签集合不能再自动得到“重复”结论。标签相似只能是支持证据，不能单独删除 coverage。

coverage 去重后若某窗口失去代表，必须从该窗口未选候选中 refill。若 refill 失败，保留原 coverage 帧并标记 `dedup_kept_for_coverage`，不能为了增加 drop 计数制造时间空白。

必须统计：

```text
coverage_dedup_candidate_pair_count
coverage_dedup_drop_count
coverage_refill_count
coverage_refill_failed_count
final_video_dedup_candidate_pair_count
final_video_dedup_drop_count
grid_similarity_pair_evaluated_count
grid_similarity_drop_count
vector_similarity_pair_evaluated_count
vector_similarity_drop_count
```

`*_drop_count=0` 可以是数据结果，但必须同时证明 pair evaluation 实际大于 0，并在 policy report 解释阈值和样本。只输出 0 而没有执行计数，策略 FAIL。

### 6.9 OCR

OCR 与 Qwen-VL 独立。

录屏视频：

- 允许进入 OCR。
- 不进入 Qwen-VL。
- 仍需黑帧/无效帧过滤和 exact/grid/vector 近重复过滤。
- 不使用普通视频每视频 1 帧的上限；通过时间覆盖与近重复过滤控制泛滥。

普通视频：

```text
normal_video_ocr_source = obvious_large_text
normal_video_ocr_cap_per_video = 1
normal_video_ocr_min_gap_ms = 20000
```

V19/V20/V21 的两个普通视频 OCR 候选不是完整的“大字 bbox”验证：现有 bbox 是绝对像素坐标，而旧 `parse_bbox()` 遇到坐标大于 2 时返回 unavailable，最终依赖 `billboard`/`blackboard` 高置信标签 fallback。V22 必须使用 `derived_assets.width/height` 将绝对 bbox 归一化。

默认明显大字 proxy：

- 直接文字承载类别：`text/sign/billboard/blackboard/whiteboard/document/screen/presentation slide/subtitle` 等通用类别。
- 最高置信度至少 0.70。
- 单个文字承载 bbox 面积至少占画面 3%，或同类可信 bbox 合计面积至少 5% 且最高置信度至少 0.80。
- `phone/laptop/book/paper/logo/ticket` 等弱对象标签单独出现时不能触发 OCR。
- 对同一视频按置信度、面积、清晰结构排序，只取 0 或 1 帧。

该规则只是 OCR 触发 proxy，不宣称已经证明文字可读；真正可读性只能由后续 OCR 结果确认。

必须统计：

```text
normal_video_ocr_added_count
normal_video_ocr_weak_excluded_count
normal_video_ocr_cap_excluded_count
normal_video_ocr_bad_bbox_excluded_count
normal_video_ocr_min_gap_excluded_count
screen_video_ocr_dedup_drop_count
```

### 6.10 图片分支

- manual seed：非黑/有效时保留。
- timelapse：只使用 DB 已冻结代表，不按文件名重新发现。
- 普通图片：使用通用主体类别、多类别上下文、bbox 质量和派生图结构；不使用项目专用组合。
- 图片 OCR 不在 V22 扩项。

## 7. V22 计划输出与接口

用户确认后才实现：

```text
scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v22_0_YYYYMMDD_HHMMSS.py
scripts/03_stop03_visual_analysis/stop03_2_v22_video_frame_contact_sheet_YYYYMMDD_HHMMSS.py
```

候选脚本计划支持：

```text
--preflight-only
--db
--out
--v14-out
--clear-existing-candidate-items
--image-yolo-threshold
--coverage-window-ms
--video-stride (兼容参数；V22 主策略优先 coverage-window-ms)
--normal-video-ocr-cap
--normal-video-ocr-min-score
--normal-video-ocr-min-gap-ms
--final-video-dedup-min-gap-ms
--final-video-dedup-vector-threshold
--final-video-dedup-grid-mad-threshold
--final-video-dedup-label-sim-threshold
```

正式写 DB 时使用单事务：先完成所有内存选择和输出一致性校验，再按 `--clear-existing-candidate-items` 清理 Stop03-2 候选表并写入新行，同时写 `model_runs`。任一失败回滚事务。

HTML 对照页必须显示普通 Step02 帧、YOLOE、有角色的 V22 候选、OCR、黑/尾/重复排除状态，并为每帧显示 timecode、labels、grid/vector/dedup reason。HTML 只能引用已有派生帧，不读取原始视频。

颜色和字段固定为：

```text
灰色 = 普通 Step02 帧
绿色 = YOLOE 有标签帧
蓝色 = video_coverage_keyframe
红色 = video_high_signal_supplement
紫色 = video_coverage_high_signal_overlap
黄色条 = OCR
黑色/暗色标记 = 黑帧、尾帧或被排除帧
```

每个视频必须显示：

```text
source_relative_path
source_content_id
Step02 frame count
effective duration
coverage count
supplement count
overlap count
OCR count
YOLOE count
每帧 time_position_ms / timecode / label count
可展开 reason_codes
grid/vector similarity 和 dedup/refill 结果
```

## 8. Summary 与状态字段

除任务书要求字段外，V22 必须新增：

```text
technical_status
policy_status
policy_reason_codes
same_as_v20_video_visual_unit_set
same_as_v21_video_visual_unit_set
same_result_explanation
coverage_window_candidate_evaluated_count
coverage_anchor_exact_selected_count
coverage_anchor_local_best_shift_count
grid_signature_success_count
grid_signature_failed_count
grid_similarity_pair_evaluated_count
vector_payload_row_count
vector_payload_integrity_status
vector_similarity_pair_evaluated_count
normal_video_ocr_bad_bbox_excluded_count
```

任务书列出的字段仍全部保留，包括：

```text
validation_status
policy_version
script_version
run_id
input_visual_units
input_video_visual_units
input_image_visual_units
qwenvl_total_count
qwen_video_frame_count
qwen_manual_seed_count
qwen_timelapse_count
qwen_image_yoloe_count
ocr_total_count
qwen_category_counts
ocr_media_type_counts
video_source_group_count
normal_video_group_count
screen_capture_video_group_count
normal_video_group_with_coverage_count
normal_video_group_missing_coverage_count
coverage_anchor_total_count
coverage_window_total_count
coverage_selected_count
coverage_dedup_drop_count
coverage_refill_count
high_signal_candidate_count
high_signal_supplement_added_count
coverage_high_signal_overlap_count
high_signal_reject_near_coverage_count
high_signal_reject_bad_or_tail_count
final_video_dedup_drop_count
black_leak_into_qwenvl_count
black_leak_into_ocr_count
screen_recording_qwenvl_leak_count
normal_video_ocr_added_count
normal_video_ocr_weak_excluded_count
normal_video_ocr_cap_excluded_count
vector_dedup_status
vector_payload_found
vector_similarity_drop_count
grid_similarity_enabled
grid_similarity_drop_count
model_rerun
safety
outputs
```

## 9. 技术验收

技术 PASS 必须同时满足：

```text
不联网、不下载、不安装依赖、不加载或重跑模型
不读取原始视频内容
不修改原始素材和模型目录
派生帧只读
black_leak_into_qwenvl_count = 0
black_leak_into_ocr_count = 0
screen_recording_qwenvl_leak_count = 0
normal_video_group_missing_coverage_count = 0
summary 字段完整
DB rows == CSV rows == JSONL rows
candidate_id 唯一
model_runs 状态与输出一致
HTML 可生成并引用存在的派生帧
```

任一安全越界、manifest/DB 不一致、已知录屏进入 Qwen、黑帧泄漏或普通视频无覆盖，`technical_status=FAIL`。

## 10. 策略验收

### 策略 PASS

必须同时满足：

- coverage anchor/window/evaluation 计数证明新路径实际执行。
- 91 个普通视频都有覆盖；长视频最大覆盖空白不劣于 V17 基线，默认不超过 30 秒。
- V14/V22 高信号真实参与窗口择优，`coverage_anchor_local_best_shift_count` 可追溯。
- grid signature 成功数与参与帧一致。
- 当前数据向量 payload 完整加载并实际参与 pair evaluation。
- 普通视频 OCR 为 0 或 1/视频，且每条都有归一化 bbox 面积证据。
- HTML 人工抽查没有系统性平庸帧、尾帧或重复帧问题。
- 结果与 V20/V21 不完全相同，或相同结果有完整执行计数、逐机制解释并经人工 REVIEW 后确认。

### 策略 REVIEW

以下任一情况进入 REVIEW：

- 候选总量明显偏高/偏低，但技术安全通过。
- dedup drop 为 0，但 pair evaluation 已真实执行且阈值有数据解释。
- supplement 为 0，但高信号已真实参与窗口替换，且 reject 原因完整。
- 未来数据缺失 vector payload，明确降级为 grid+label。
- 普通视频 OCR 只有 0-2 条。
- V22 视频候选集合与 V20/V21 完全相同，即使执行计数证明数据确实选择相同，也必须先 REVIEW，不能直接 PASS。

### 策略 FAIL

- anchor/grid/vector 函数只定义未调用，或执行计数与实际路径不一致。
- 通过改字段名制造“新策略”但候选机制没有变化。
- V22 与 V20/V21 完全相同且没有解释。
- 高信号补充仍因互相矛盾的 cap/gap 规则结构性归零。
- coverage 被错误命名为 high value，角色未拆分。
- 普通视频 OCR 仅凭弱标签大量进入。
- 为追求 dedup drop 破坏时间覆盖。
- 使用项目专用词或组合规则。

## 11. V22 与 V20/V21 相同时的强制解释

若最终 `visual_unit_id` 集合完全相同，summary 必须依次回答：

```text
1. coverage_anchor_total_count 是否大于 0？
2. coverage_window_candidate_evaluated_count 是否大于 anchor 数？
3. grid_signature_success_count 是否与预期一致？
4. vector_payload_integrity_status 是否 PASS？
5. grid/vector pair evaluation 是否实际发生？
6. 每个 anchor 为什么仍选择原帧？
7. V14 独有帧分别被替换、判重、判坏、判尾还是信号不足？
8. 是数据导致一致，还是代码路径未走到？
```

若 1-5 不能证明机制执行：策略 FAIL。若机制执行但数据确实选择相同：策略 REVIEW，待 HTML 人工确认后才可能提升为 PASS。

## 12. 不解决什么

V22 不解决：

- 不重新抽帧，不直接读取原始视频，不做镜头切分模型。
- 不重新运行 YOLOE、OpenCLIP、Qwen-VL、OCR。
- 不判断身份、眼睛是否睁开、动作语义或真实事件重要性。
- 16x5 grid 不能理解语义，也不能可靠识别主体。
- 在没有明确路径/元数据时，不能保证识别所有被改名的录屏文件。
- 普通视频 OCR gate 只能判断“大字候选 proxy”，不能在 OCR 前证明文字可读。
- 不建立 V0.5 搜索索引，不进入未批准阶段。

## 13. 安全边界

```text
network = disabled
download = disabled
dependency_install = disabled
model_loading = disabled
model_rerun = false
original_video_decode = false
original_media_write = false
derived_frame_read = true, read_only
model_directory_write = false
```

允许写入仅限用户指定目录和表；实现阶段也不得写入原始素材目录或 `$MODEL_ROOT`。

## 14. 回滚

V22 实现后如技术或策略不通过：

1. 保留 V22 输出用于审计，不删除旧输出。
2. 不修改原始素材，不清理任何未授权 workspace。
3. 参考基线恢复为：V14 用作高信号参考，V20 用作覆盖结果参考但不视为策略冻结版本。
4. 若需要恢复中心候选表，只能由用户确认后运行任务书指定的 V20 命令并使用 `--clear-existing-candidate-items`。

本轮未执行任何回滚命令，也未写中心 SQLite。

## 15. 下一步确认点

用户确认本设计后，下一阶段才编写 V22 候选脚本和 HTML 脚本。实现顺序应为：

1. 先写并短测 preflight、vector payload loader、16x5 signature 和 bbox 归一化。
2. 再写 coverage window + high-signal 窗口内择优。
3. 再写 dedup/refill 和 OCR 独立分支。
4. 最后写事务型 DB/manifest 输出和 contact sheet。
5. 先跑短 preflight；正式 V22 运行需再次按用户确认边界执行。
