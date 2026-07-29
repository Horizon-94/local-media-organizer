# STOP03-2 通用高价值帧候选规则 DR V2

## 结论

V2 修正 V1/V6 暴露的问题：高价值帧筛选必须是通用规则，但“通用”不等于“YOLOE 有主体就进”。视频帧必须同时满足：

1. 有通用视觉/文字/主体信号；
2. 有段落变化、信息跳变、OCR 区域出现、边界代表、稀有对象或人工标记等强理由；
3. 有有效时间码参与同视频去重与 20 秒间隔判断；
4. 同一派生帧不得因多个 visual_unit 重复入选。

## 不解决

- 不跑 Qwen-VL。
- 不跑 OCR。
- 不重跑 YOLOE。
- 不重跑 OpenCLIP。
- 不重新抽帧。
- 不修改原始素材。
- 不做任何项目专用词规则。

## V2 修正点

### 1. 视频时间码必须使用 effective_time_position_ms

视频帧时间码读取顺序：

```text
visual_units.time_position_ms >= 0
否则 derived_assets.time_position_ms >= 0
否则 frame_index * 3000ms
否则 -1 并记录 missing_time_position
```

如果视频候选中大量 `time_position_ms = -1`，本阶段不得 PASS。

### 2. generic_visual_subject_signal 不能单独入选

`generic_visual_subject_signal` 只是基础信号，不能单独让一帧进入 Qwen-VL 高价值队列。

视频帧必须额外命中至少一个强理由：

```text
major_object_set_change
high_information_jump
ocr_region_emerges
video_coverage_boundary
video_min_one_best_available_frame
manual_seed
```

### 3. OCR 区域出现不是 OCR 持续存在

如果一段录屏每一帧都有 screen recording / subtitle，不能每一帧都因为 OCR 入选 Qwen-VL。

`ocr_region_emerges` 只在这些情况下成立：

```text
当前帧有 OCR 信号，且前后帧无 OCR 信号；
或当前帧标签集合变化明显；
或当前帧是视频边界代表点。
```

### 4. high_information_jump 必须是相对跳变

不能因为一帧里 person 多就每帧入选。

`high_information_jump` 必须相对于前后帧有明显增加：

```text
检测框数量比邻近帧明显增加；
或 distinct label 数量比邻近帧明显增加；
或高检测量同时伴随标签集合变化。
```

### 5. 同一派生帧去重

如果多个 visual_unit 指向同一个 derived_id / derived_path / time_position_ms，只允许一个代表 visual_unit 参与视频候选选择。

报告必须输出：

```text
duplicate_visual_unit_same_derived_count
```

### 6. 百分比只能做审计，不做筛选

不按百分比选高价值帧。

但如果某视频出现：

```text
selected_count > 30
或 selected_count / nonblack_frame_count > 0.5
```

必须标记：

```text
overselect_review_flag = true
```

这是审计告警，不是筛选规则。

## PASS 条件

- 不联网。
- 不下载。
- 不加载模型。
- 不修改原始素材。
- 黑帧泄漏 0。
- 人工标记图片正常进入。
- OCR 队列独立生成，且可与 Qwen-VL 重叠。
- 视频时间码不能大量为 -1。
- `video_selected_group_count` 必须与视频候选真实分组一致，不能为 0。
- 不得出现大量视频组接近全选而无审计告警。
- Qwen-VL 数量不能刚好顶到安全上限；如果顶到，判策略 FAIL。
