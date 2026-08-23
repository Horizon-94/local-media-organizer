# STOP03-2 V18 规则：V17 覆盖主策略 + V14 高信号补充

## 结论

V18 不再把 V14 和 V17 二选一。

- V17 负责视频主覆盖：先均匀覆盖整条视频，避免漏段和扎堆。
- V14 负责高信号补充：只在 V17 覆盖之外，补入少量确实有信号价值的帧。

V18 是对照候选版，不是冻结版。

## 最高约束

- 不联网。
- 不下载。
- 不安装依赖。
- 不重跑 YOLOE。
- 不重跑 OpenCLIP。
- 不跑 Qwen-VL。
- 不跑 OCR。
- 不读取原始视频。
- 不写原始素材。
- 只读 SQLite 中心数据库。
- 可读 V14 输出 manifest 作为补充信号输入。
- 写入范围仅限 test-output 和 SQLite candidate 表。

## 输入

中心数据库：

```text
$APP_RESOURCES/Pipeline/media_archive.sqlite
```

V14 高信号队列：

```text
$USER_HOME/Documents/AI-Local/test-output/stop03-2-candidate-queues-db-safe-v14_0_20260709_232500_full/manifests/qwenvl_high_value_candidate_queue.csv
```

V18 主逻辑仍以数据库为主。V14 manifest 只是上一轮策略输出的补充信号来源，不替代数据库。

## 视频 Qwen-VL 候选策略

### 第一层：V17 覆盖主层

1. 普通视频按 `source_content_id` 分组。
2. 组内按 `derived_assets.time_position_ms` 排序。
3. 从中间帧开始，向左右每隔 6 个 Step02 帧取一帧。
4. 黑帧剔除，并在 ±1～±4 范围内寻找非黑替代。
5. 视频尾部默认抑制。
6. 录屏视频不进入 Qwen-VL。

输出类别：

```text
video_coverage_keyframe
```

如果 V17 覆盖帧同时也是 V14 高信号帧，则标记为：

```text
video_coverage_high_signal_overlap
```

### 第二层：V14 高信号补充层

只补充满足以下条件的 V14 视频高信号帧：

1. 不属于录屏视频。
2. 不黑。
3. 不在尾部落点窗口。
4. 不与已有 V17 覆盖帧太近。
5. 默认与已有覆盖帧至少间隔 15 秒。
6. 每个视频补充数量有上限：
   - 小于 18 个 Step02 帧：不补。
   - 18～34 个 Step02 帧：最多补 1 张。
   - 35～79 个 Step02 帧：最多补 2 张。
   - 80 个 Step02 帧及以上：最多补 3 张。

输出类别：

```text
video_high_signal_supplement
```

## OCR 策略

保持 V13/V14/V17 的 strict screen capture only：

- 图片默认不进 OCR。
- 普通相机视频默认不进 OCR。
- 只允许 RPReplay / 录屏 / screen recording / screenshot 等路径进入 OCR。

## Timelapse 策略

保持 DB-only：

- 只读 `step02_image_timelapse_keyframes`。
- 每个 sequence 优先 middle 一张。
- 不从文件名重新识别。

## 验收重点

V18 不只看数量，重点看网页：

1. V17 覆盖帧是否均匀。
2. V14 补充帧是否确实补了更有价值的画面。
3. 是否避免 V15 那种局部扎堆。
4. 录屏是否仍然不进 Qwen-VL。
5. OCR 是否仍然只来自 strict screen capture。
6. 黑帧是否为 0。
7. 尾帧是否明显减少。

建议观察数量：

```text
video_total = video_coverage_keyframe + video_coverage_high_signal_overlap + video_high_signal_supplement
合理观察区间：160～210
```

不是硬阈值，以网页质量为准。

## 当前判断

V18 如果表现稳定，后续可以成为正式方向：

```text
视频主队列 = V17 覆盖基础 + V14 高信号补充
```

但本轮不冻结。
