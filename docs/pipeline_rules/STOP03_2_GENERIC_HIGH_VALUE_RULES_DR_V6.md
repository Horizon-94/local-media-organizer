# STOP03-2 通用高价值候选规则 DR V6 / V10.0

## 结论

Stop03-2 V10.0 必须是中心数据库优先版本。

Stop03-2 不再从 Step02 manifest 读取或物化 timelapse；不再通过文件名、路径、keyframe_pool、YOLOE 标签重新识别延时摄影。

唯一允许的 timelapse 输入源是中心 SQLite 表：

```text
step02_image_timelapse_keyframes
```

## 依据

V9.0 已完成一次性回填：

```text
step02_image_timelapse_keyframes = 12 rows
sequence_count = 4
每组 first / middle / last
```

V10.0 的职责是消费这张 DB 表，而不是再次读取外部文件。

## 执行规则

1. Stop03-2 启动时检查 `step02_image_timelapse_keyframes` 是否存在。
2. 如果表不存在或为空，直接 BLOCK，不回退到 manifest，不猜路径。
3. 读取 `preview_role='timelapse_keyframe'` 的记录。
4. 按 `sequence_id` 分组。
5. 每组默认选择 `representative_position='middle'`。
6. 没有 middle 时，按 first、last、CSV/DB 顺序兜底。
7. 输出 `high_value_category='timelapse_candidate'`。
8. reason_codes 必须包含：
   - `step02_timelapse_keyframe_db_source`
   - `timelapse_sequence_min_representative`
   - `sequence_id:<id>`
   - `representative_position:<position>`

## 禁止

- 禁止从普通图片路径重新发现 timelapse。
- 禁止从 `TIMELAPSE` 文件名重新分组。
- 禁止从 `keyframe_pool_jpg` 推断 timelapse。
- 禁止依赖 YOLOE 标签判断 timelapse。
- 禁止 Stop03-2 读取 Step02 manifest 作为正式输入。
- 禁止默认 max-qwen / max-ocr 作为目标数。

## 当前 30GB 验收预期

```text
qwen_timelapse_count = 4
qwenvl_total_count ≈ 327
qwen_manual_seed_count = 125
qwen_image_yoloe_count ≈ 69
qwen_video_frame_count ≈ 129
ocr_total_count = 403
black leak = 0
video_overselect_review_group_count = 0
```

## 阶段边界

如果 `step02_image_timelapse_keyframes` 缺失，说明 Step02 数据库化不完整。Stop03-2 V10 只能停止并报告，不允许绕过中心 DB。
