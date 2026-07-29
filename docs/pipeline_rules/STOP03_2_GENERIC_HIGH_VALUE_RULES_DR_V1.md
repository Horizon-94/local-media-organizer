# Stop03-2 通用高价值帧候选规则 DR V1

## 0. 定位

本规则用于本地素材大整理项目的 Stop03-2：从已经完成的低成本视觉证据中生成高成本模型入口队列。

本规则只定义两个候选队列：

1. Qwen-VL 高价值视觉候选队列；
2. OCR 触发候选队列。

本规则不执行 Qwen-VL，不执行 OCR，不重跑 YOLOE，不重跑 OpenCLIP，不修改原始素材。

## 1. 最高原则

高价值帧不是固定百分比、固定总数，也不是每个视频平均一张。

高价值帧的定义是：这一个视觉单元值得后续更贵的模型再看一遍，因为它可能包含可检索、可理解、可复用、可叙事、可解释的视觉信息。

本规则必须通用。不得为某一个具体项目、某一个片子、某一个主题写死规则。比如不能把“小麦、农机、农村”写成专用优先级。它们只能落入通用类别：自然/土地/植物、车辆/机器/设备、人物/社会场景。

## 2. 输入证据

Stop03-2 只能使用已经存在的低成本证据：

- visual_units
- derived_assets
- source_assets
- visual_labels / visual_label_terms
- embeddings 的存在性和后续可扩展向量索引
- manual_high_value_visual_seeds
- 派生 JPG 的只读质量检测

禁止把 OpenCLIP embedding 是否存在当成高价值主因。embedding 只能作为后续去重、新颖度、语义变化的参考。

## 3. 硬过滤

这些情况直接拒绝，不进入 Qwen-VL，也不进入 OCR：

- 黑帧；
- 近黑帧；
- 派生图打不开；
- 派生图不存在；
- 尺寸异常或解码异常；
- visual_unit 无法追溯来源。

黑帧规则：

```text
luma_mean <= 8 且 luma_std <= 5
或 black_pixel_ratio >= 0.985 且 luma_mean <= 16
```

硬过滤优先级最高。即使人工标记图是黑图，也不能进入高价值队列，只能进入拒绝报告。

## 4. 人工标记

人工标记是强信号。

当前包括：

- macOS Finder 红色/绿色标签；
- 后续可扩展 XMP rating / label / pick；
- 后续可扩展 Bridge / Camera Raw / Photoshop / Lightroom 标记；
- 后续可扩展剪辑软件 marker。

规则：

```text
manual_high_value_visual_seeds 命中 → 进入 Qwen-VL 候选
黑帧/坏图硬过滤优先
```

## 5. 通用 YOLOE 标签分层

标签分层只能使用通用类别，不使用项目专用主题。

### 5.1 强视觉价值类别

这些类别可以触发 Qwen-VL 候选：

- 人物/社会场景：person, face, people, crowd, hand
- 文字/屏幕/文档：text, sign, screen, monitor, display, phone, laptop, computer, document, paper, book, poster, subtitle, whiteboard, license plate, menu
- 车辆/机器/设备：car, truck, bus, train, motorcycle, bicycle, boat, airplane, tractor, harvester, machine, tool, camera, microphone
- 动物/生命主体：dog, cat, cow, sheep, horse, chicken, bird, animal
- 场所/结构/道路：building, house, street, road, bridge, station, store, shop, room, village, city
- 自然/土地/植物：field, farm, crop, plant, tree, river, lake, sea, mountain, beach
- 食物/物件上下文：food, table, chair

注意：上面只是通用类别，不是项目专用词。比如 wheat 如果出现，只能作为 crop/plant/nature 类的一部分，不能独立成为专用规则。

### 5.2 弱背景类别

以下标签不能单独触发高价值：

- sky
- cloud
- wall
- floor
- ceiling
- grass
- window
- door

弱背景标签可以参与描述，但不能成为 Qwen-VL 入选主因。

## 6. 视频高价值帧规则

### 6.1 基本原则

视频必须按原始视频 source_content_id 分组，并按 time_position_ms 排序。

视频候选不是按百分比产生，也不是按固定总数产生。

视频候选来自高价值时间段落：

- 有新的主体；
- 有新的场景；
- 有新的文字/屏幕区域；
- 有明显信息密度变化；
- 有主要物体集合变化；
- 有罕见对象；
- 有开头/结尾/关键覆盖点。

### 6.2 默认去重间隔

默认最小间隔 20 秒只用于防重复，不是硬性删除规则。

如果两个候选相距小于 20 秒，但有以下原因，可以打破间隔：

- major_object_set_change
- high_information_jump
- ocr_region_emerges
- rare_object_in_video
- manual_seed
- video_min_one_best_available_frame

### 6.3 每个视频至少一帧

如果一个视频没有任何明显高价值信号，但它有有效非黑帧，则保留该视频中最好的可用代表帧，原因记为：

```text
video_min_one_best_available_frame
```

这不是说所有视频平均一张，而是避免视频完全失联。

### 6.4 不使用固定小预算

废弃以下规则：

```text
每个视频 <=3 选1
<=10 选1
<=30 选2
<=80 选4
>80 选6
```

这会导致长视频和复杂视频被压得太薄。

V6 使用动态段落规则：有几个有效高价值段落就选几个，安全上限只用于防止异常全量扩散。

## 7. 普通图片规则

普通图片分两类：

1. 人工标记图片：直接进入 Qwen-VL；
2. 未人工标记图片：必须有通用强视觉理由，不能只靠 embedding 存在。

未标记图片进入 Qwen-VL 的条件：

- 强视觉类别命中；
- 多类别组合；
- 明显文字/屏幕/文档；
- 信息密度较高；
- 非弱背景；
- 黑帧/坏图过滤通过。

禁止：

```text
has_openclip_embedding → 高价值
任意 YOLOE positive → 高价值
普通图片全部进入
```

## 8. 延时摄影规则

延时摄影必须单独统计，不能混在普通图片里。

规则：

- timelapse_keyframe 可作为候选；
- timelapse_member 默认不进入；
- 人工标记强制进入；
- 若有明显通用强视觉信号或变化信号，可进入；
- 输出 timelapse_candidate_count。

## 9. OCR 触发规则

OCR 队列独立于 Qwen-VL 队列。两者可以重叠。

OCR 触发来源：

- YOLOE label：text, sign, screen, monitor, phone, laptop, document, paper, book, poster, subtitle, whiteboard, license plate, menu 等；
- 路径/文件名：screen, screenshot, screenrecording, RPReplay, 录屏, 屏幕录制, 截屏, 截图, 文档, 发票, 菜单, 路牌, 招牌, 字幕等；
- source group 明显为录屏/截图/文档类。

OCR 本阶段只生成队列，不执行 OCR，不做 OCR 去重，不做 OCR 结果合并。

## 10. 报告验收

V6 必须输出以下统计：

- Qwen-VL 总数；
- Qwen-VL 图片数；
- Qwen-VL 视频数；
- Qwen-VL 延时摄影数；
- 人工标记数；
- 未标记图片视觉信号数；
- OCR 总数；
- OCR 图片数；
- OCR 视频数；
- OCR 延时摄影数；
- Qwen/OCR 重叠数；
- 黑帧泄漏数；
- 每个视频 selected_count；
- 每个视频 candidate_signal_count；
- 每个视频是否使用 dynamic_segment_rule_no_fixed_budget。

## 11. PASS 条件

同时满足：

- 不联网；
- 不下载；
- 不加载模型；
- 不修改原始素材；
- 编译通过；
- preflight PASS；
- DB audit PASS；
- 候选队列生成；
- 黑帧泄漏为 0；
- Qwen-VL/OCR 可重叠；
- 视频不再由固定小预算决定；
- 报告能解释图片、视频、延时摄影、OCR 的来源。

## 12. V6 测试定位

V6 是通用规则测试版。它不是最终冻结版。

如果输出结果仍偏少或偏多，应看每条视频的 reason_codes 和 video_budget_report，再调通用规则，不得引入项目专用主题。
