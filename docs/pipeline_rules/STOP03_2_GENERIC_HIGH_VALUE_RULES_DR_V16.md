# STOP03-2 V21：全视频覆盖优先版

## 结论

V21 是对 V20 的结构修正。

V20 看起来比 V17/V18/V19 好，但 summary 里有几个信号说明它还不够干净：

- `v20_anchor_local_best_shift_count = 0`
- `v20_coverage_dedup_drop_count = 0`
- `v20_final_video_dedup_drop_count = 0`

也就是说，V20 虽然写了内容感知和去重逻辑，但实际生效不明显。

V21 的目标是明确执行：

1. 所有非录屏视频都必须进入同一套 V17 覆盖流程。
2. 每个视频先完整覆盖。
3. 覆盖层内部先去重。
4. 确保每个非录屏视频至少有一张覆盖帧。
5. 再用 V14 做补充。
6. 最后再整体去重。
7. OCR 保持窄口：录屏全进，普通视频只允许明显大字。

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
- 只读 SQLite。
- 只读 derived visual/frame 文件用于 16×5 grid 和黑帧检查。

## V21 和 V20 的区别

### V20

- 设计上有内容择优和覆盖去重；
- 但运行结果中相关计数为 0，说明效果不明显；
- 仍然不够像“全视频统一策略”。

### V21

- 明确统计每个非录屏视频是否有覆盖：
  - `v21_non_screen_video_group_count`
  - `v21_non_screen_video_group_with_coverage_count`
  - `v21_non_screen_video_group_missing_coverage_count`
- 如果某个非录屏视频覆盖层去重后为空，会回填一张最佳可用帧：
  - `v21_min_one_refill_count`
- 锚点附近搜索扩大到 ±3/±4。
- 覆盖层去重在 V14 补充之前完成。
- 如果非录屏视频仍然有 0 覆盖，validation_status 直接 FAIL。

## V21 流程

1. 按 `source_content_id` 分组所有视频帧。
2. 跳过录屏视频进入 Qwen-VL，只保留 OCR。
3. 对每个非录屏视频：
   - 按时间排序；
   - 从中间向两边每隔 6 帧建立锚点；
   - 每个锚点附近 ±3/±4 查找最佳帧；
   - 使用 16×5 luma grid、YOLOE 标签、黑帧、尾部判断进行择优；
   - 覆盖层内部去重；
   - 如果去重后为空，回填一张最佳可用帧。
4. 加入 V14 高信号补充。
5. 最终单视频去重。
6. 输出 V21 队列和 HTML 页面。

## OCR 规则

- 录屏 / 屏幕录制 / 截图类视频 OCR 保留。
- 普通视频 OCR 只允许明显大字。
- 普通视频默认最多 1 帧 OCR。
- 弱文字标签只记录为 excluded，不进入 OCR。

## 验收

PASS：
- 黑帧泄漏 0。
- 录屏进入 Qwen-VL 0。
- `v21_non_screen_video_group_missing_coverage_count = 0`。
- `qwen_video_frame_count` 比 V14 高，覆盖更完整。
- OCR 不泛滥。
- 页面上覆盖比 V20 更稳定。

REVIEW：
- 覆盖帧太平庸。
- V14 补充基本为 0。
- 覆盖层去重仍为 0，但网页效果可接受。

FAIL：
- 非录屏视频有 0 覆盖。
- 黑帧泄漏。
- 录屏进入 Qwen-VL。
- 普通视频 OCR 泛滥。
- 触发联网/下载/模型重跑。
