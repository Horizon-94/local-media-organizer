# STOP03-2 V17 对照版规则：Time Coverage Keyframe

结论：V17 不是 V14 的小补丁，而是一条视频主策略候选：先按时间覆盖，再过滤黑帧、录屏、尾帧和重复帧。

## 最高约束

- 不联网，不下载，不安装依赖。
- 不重跑 YOLOE / OpenCLIP / Qwen-VL / OCR。
- 不读取原始视频，不写原始素材。
- 只读 SQLite 中央数据库和 derived visual files。
- 输出只写 test-output 和 SQLite candidate 队列表。

## 规则

1. 视频按 `source_content_id` 分组，按 `derived_assets.time_position_ms` 排序。
2. 录屏 / 截屏 / 屏幕录制类视频不进入 Qwen-VL，只进入 OCR。
3. 普通视频从中间帧开始，左右每隔 6 个 Step02 帧选 1 帧。
4. 黑帧跳过；若选中黑帧，则查找邻近 ±1～±4 的非黑帧替代。
5. 视频尾部默认不选：duration > 30s 时，最后 5% 或 6～30 秒窗口内的帧被抑制。
6. 做轻量相似去重：相邻候选时间太近且 YOLOE 标签集合高度相似，只保留更好的一张。
7. Timelapse 继续 DB-only：读取 `step02_image_timelapse_keyframes`，每组优先 middle。
8. 图片 OCR 默认关闭；普通相机视频 OCR 默认关闭；只保留 strict screen capture path OCR。

## V14 / V17 定位

- V14：高信号帧，适合补强，不适合单独承担覆盖。
- V17：覆盖关键帧，适合作为视频主策略候选。

## 验收

- screen recording 进入 Qwen-VL = 0。
- black leak = 0。
- OCR 全部来自 strict_screen_capture_path。
- video_coverage_keyframe_count 观察范围：150～190。
- 对比网页能看出 V14 红色高信号、V17 蓝色覆盖、重合紫色、OCR 黄色。
