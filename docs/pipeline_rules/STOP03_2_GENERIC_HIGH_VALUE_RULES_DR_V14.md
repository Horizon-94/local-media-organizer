# STOP03-2 V19：V18 修正版

## 结论

V19 继续使用“V17 覆盖为主 + V14 高信号补充”的方向，但修正 V18 的两个缺口：

1. V17 覆盖帧和 V14 补充帧合并之后，必须再做一次单视频内部去重。
2. OCR 允许回到普通视频，但只能是明显大字 / 可读文字，且默认每个普通视频最多 1 帧。

## 不解决什么

- 不做人脸识别。
- 不判断睁眼闭眼。
- 不跑 Qwen-VL。
- 不跑 OCR。
- 不重跑 YOLOE。
- 不重跑 OpenCLIP。
- 不读取原始视频。
- 不写原始素材。
- 不联网、不下载、不安装依赖。

## V19 视频 Qwen-VL 候选规则

### 第一层：V17 覆盖

- 普通视频按 `source_content_id` 分组。
- 按 `derived_assets.time_position_ms` 排序。
- 从中间帧开始，左右每隔 6 个 Step02 帧选 1 帧。
- 黑帧跳过，向附近 ±1～±4 找替补。
- 视频尾部落点抑制。
- 录屏 / 屏幕录制不进 Qwen-VL。

### 第二层：V14 高信号补充

- 读取 V14 `video_high_value_segment_candidate`。
- 只补充：
  - 非黑帧；
  - 非尾部；
  - 非录屏；
  - 距离已有 V17 覆盖帧至少 15 秒；
  - 每个视频按帧数限制补充数量。

### 第三层：最终单视频去重

V18 的缺口就是这里。V19 增加最终去重：

- 在每个视频内部，对所有最终 Qwen 视频候选按时间排序。
- 如果两个候选相距小于默认 18 秒，且 YOLOE 标签相似度高于 0.72，则视为重复。
- 如果两个候选非常接近，小于 9 秒，且标签相似度高于 0.50，也视为重复。
- 重复时保留优先级：
  1. `video_coverage_high_signal_overlap`
  2. `video_high_signal_supplement`
  3. `video_coverage_keyframe`
  4. 分数更高者

## V19 OCR 规则

OCR 分两类：

### 1. 录屏 OCR

继续允许：
- RPReplay
- screenrecording
- screen_recording
- screen-recording
- screen recording
- record screen
- recorded screen
- 录屏
- 屏幕录制
- 截屏
- 截图
- screenshot

### 2. 普通视频 OCR

V19 允许普通视频 OCR，但只允许“明显大字”：

- 默认每个普通视频最多 1 帧。
- 默认相邻 OCR 候选至少 20 秒。
- 需要满足：
  - 文本类标签置信度足够高；
  - bbox 面积足够大；
  - 或 bbox 不可用但标签强且置信度很高。
- 弱 `book/sign/subtitle/logo` 不再自动进入 OCR。

## 验收

### PASS

- 不联网、不下载、不跑模型。
- 录屏不进 Qwen-VL。
- 黑帧不进 Qwen-VL / OCR。
- V19 summary 中有：
  - `v19_final_video_dedup_drop_count`
  - `normal_video_ocr_added_count`
  - `normal_video_ocr_weak_excluded_count`
- 普通视频 OCR 数量少，不泛滥。
- 网页上高价值帧比 V18 更不重复。

### REVIEW

- `normal_video_ocr_added_count` 过高。
- `v19_final_video_dedup_drop_count = 0`，说明最终去重没起作用。
- V17 覆盖被去重削得太少。
- V14 补充仍然扎堆。

### FAIL

- 黑帧泄漏。
- 录屏进入 Qwen-VL。
- 普通视频 OCR 泛滥。
- 触发联网/下载/模型重跑。
