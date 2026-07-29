# STOP03-2 通用高价值候选规则 DR V3

## 结论

本规则用于 Stop03-2：从已完成的 YOLOE、OpenCLIP 向量、人工标记、派生帧质量检查中，生成 Qwen-VL 高价值候选队列与 OCR 触发队列。

它是通用素材规则，不是小麦项目、驾照项目或任何单一项目专用规则。

## 不做什么

- 不按固定百分比选帧。
- 不按固定总数选帧。
- 不按每个视频平均分配。
- 不因为视频长就多选。
- 不因为 YOLOE 有检测就进 Qwen-VL。
- 不因为 OpenCLIP embedding 存在就进 Qwen-VL。
- 不让 OCR 队列和 Qwen-VL 队列互斥。
- 不重跑 YOLOE / OpenCLIP / Qwen-VL / OCR。
- 不读取或修改原始视频。

## 基础硬闸门

黑帧、近黑帧、派生图打不开、尺寸异常、无法追溯来源，直接拒绝进入 Qwen-VL 和 OCR 队列。

## Qwen-VL 高价值候选

### 人工标记

人工 Finder 标签、XMP 星标、后续人工确认标记，强制进入 Qwen-VL。黑帧硬闸门优先。

### 视频帧

视频帧必须按原始视频分组，使用 effective_time_position_ms 排序。

effective_time_position_ms 来源顺序：

1. visual_units.time_position_ms >= 0
2. derived_assets.time_position_ms >= 0
3. frame_index * 3000
4. 无法取得时标记为 -1 并进入 REVIEW

视频帧必须满足：通用主体信号 + 明确段落证据。

通用主体信号只作为基础信号，不能单独入选。

明确段落证据包括：

- major_object_set_change：主体类别集合明显变化
- high_information_jump：相对前后帧的信息密度明显跳变
- ocr_region_emerges：文字/屏幕区域新出现或与场景变化同时出现
- video_coverage_boundary：带有强内容信号的视频边界代表帧
- video_min_one_best_available_frame：该视频没有强信号时，只保留最优代表帧

同一视频内默认 20 秒最小间隔。持续重复的口播、采访、屏幕、空镜，不应每隔几秒进入 Qwen-VL。

V7.1 增加 group guard：它不是目标数，只是避免单条视频爆炸。超过 guard 的候选不继续放大。

## 普通图片

普通图片分三类：

1. 人工标记图片：直接进 Qwen-VL。
2. 延时摄影图片：按 sequence 选代表帧。
3. 未标记普通图片：从严，只在多类别组合、OCR+场景、明显信息密度、罕见对象等强通用信号下进入。

未标记普通图片不能因为有普通 YOLOE 标签就进入。

## 延时摄影

延时摄影 sequence 默认选 1 张代表帧。只有 sequence 较大且首尾标签集合明显变化时，可选 2 张。

典型 3 帧延时组通常只选中间或最高分代表帧。全量测试中，延时摄影数量不应明显膨胀。

## OCR 触发队列

OCR 队列独立，不做 OCR 识别，只生成触发候选。

触发来源：

- YOLOE 文本/屏幕/文档/招牌/字幕/书本/纸张/手机/电脑等标签
- 路径和文件名中的 screen、screenshot、screenrecording、RPReplay、录屏、截图、文档、发票、菜单、路牌、字幕等关键词

同一 visual_unit 可以同时进入 Qwen-VL 和 OCR。

## 验收状态

- 黑帧泄漏 > 0：FAIL
- 时间码全部或大量失效：FAIL / REVIEW
- emergency cap 被触达：REVIEW
- video_overselect_review_group_count > 0：REVIEW，不得冻结
- 输出数量正常但报告不能解释：REVIEW
- 只有技术运行成功但业务规则偏宽：REVIEW，不得进入 Qwen-VL

## V7.1 目标

V7.1 是规则收敛测试版。目标不是固定选多少张，而是压住 V7 的过宽问题：

- Qwen-VL 总数应明显低于 V7 的 832
- 视频帧应明显低于 V7 的 513
- 延时摄影应从 V7 的 9 收敛到更接近 sequence 代表帧数量
- 未标记普通图片应明显低于 V7 的 185
- OCR 队列保持独立，不因 Qwen-VL 收紧而丢失
- 黑帧泄漏仍为 0
