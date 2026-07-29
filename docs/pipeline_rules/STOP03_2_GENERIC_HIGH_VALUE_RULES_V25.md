# Stop03-2 通用高价值帧与 OCR 候选规则 V25

状态：`FROZEN`
说明：本文件定义正式通用规则。完成 V25 代码实现、当前数据回归、数据库写入与幂等验证后，状态改为 `FROZEN`。
适用范围：所有后续本地图片、视频、录屏、延时摄影素材批次。不得针对当前文件夹、项目主题或当前统计数量写专用规则。

V25 是 Stop03-2 最终冻结候选。除录屏路径识别新增大小写不敏感的 `rpreplay` marker 外，视频 coverage、tail-only remap、局部择优、refill、视频内部去重、高信号 supplement、普通视频 OCR 和时间映射必须完整保持 V24 行为。图片规则冻结为 Finder、XMP、timelapse 三来源 canonical 并集。

---

## 1. 规则目标

V25 不是“人工挑看起来好的帧”，而是一个确定性、可复跑、可解释、可写库的本地规则。

目标：

1. 保证普通视频全时段有覆盖入口。
2. 在覆盖位置附近优先选择已有视觉证据更强的帧。
3. 去除同一视频中低运动、空镜、动作变化小造成的时序冗余。
4. 不删除任何原始素材、派生帧或原始时间码。
5. 录屏与普通视频分流。
6. OCR 与 Qwen-VL 候选分流。
7. 每个结果都能追溯到原视频、原始秒数、代表帧和被折叠成员。
8. 后续所有批次使用同一规则，不依赖人工偏好。

---

## 2. 不冻结当前测试结果数量

以下数量只属于当前测试数据，不属于 V25 规则，不得写成固定验收目标：

- 当前 visual 总数
- 当前 canonical visual 数
- 当前 Qwen 候选数
- 当前 OCR 候选数
- 当前视频数量
- 当前 coverage window 数
- 当前重复帧数量

V25 冻结的是：

- 输入来源
- 分组顺序
- coverage 生成方式
- 窗口内择优方式
- 去重证据组合
- refill 方式
- 角色标记
- OCR 分流
- 数据库存储和时间映射
- 自动验收闸门

---

## 3. 输入规则

### 3.1 正式输入

重型视觉候选默认只读取：

```sql
canonical_visual_units_for_heavy
```

源文件默认只读取：

```sql
canonical_source_assets_for_heavy
```

### 3.2 原始记录不得丢失

中心数据库仍必须完整保留：

```text
visual_units
source_assets
duplicate visual -> canonical visual 映射
每个 visual 的 time_position_ms
每个 visual 的 frame_index
duplicate group 成员
```

canonical 视图只是后续重型处理入口，不是删除清单。

### 3.3 禁止旧版本运行依赖

V25 不得依赖某个旧输出目录中的 V14、V17、V20 或 V22 manifest 才能运行。

V14 的高信号思想、V17 的时间覆盖思想和 V22 的 vector/grid 去重逻辑必须吸收进 V25 代码，由 V25 直接根据中心数据库现有 YOLOE、OpenCLIP、派生帧和时间码重新计算。

V24 candidate manifest 只允许在本次 finalizer 中用于排除 RPReplay 后的一次性普通视频语义回归。V25 冻结后的生产运行不得要求或读取 `--video-regression-baseline`。

---

## 4. 普通视频 coverage 规则

### 4.1 帧排序

同一 `source_content_id` 内按以下顺序稳定排序：

```text
time_position_ms
frame_index
visual_unit_id
```

### 4.2 基础 coverage

默认使用 Step02 已抽取帧序列，按“每隔 6 个 Step02 帧建立一个 coverage anchor”的规则覆盖整条视频。

固定配置：

```text
coverage_stride_frames = 6
coverage_local_radius_frames = 3
```

规则：

1. 先取视频中间位置作为第一个 anchor。
2. 再从中间向左、向右每隔 6 个 Step02 帧建立 anchor。
3. 短视频至少有一个 anchor。
4. coverage 数量由视频实际 Step02 帧数决定，不使用当前测试数据的固定数量。
5. 若未来 Step02 采样间隔变化，V25 仍按 sampled-frame index 工作，同时记录实际时间间隔供审计。

### 4.3 anchor 不是最终帧

每个 anchor 只是覆盖位置。V25 必须在 anchor 附近的候选帧中进行确定性择优，不允许直接把 anchor 当作最终高价值帧。

候选范围由完整 Step02 sampled-frame index 计算：

```text
anchor_index - coverage_local_radius_frames
到
anchor_index + coverage_local_radius_frames
```

首尾位置必须 clamp 到实际 sampled-frame 范围。局部范围可包含已被中心去重折叠的 sampled frame 位置，但最终候选只能来自 `canonical_visual_units_for_heavy`。

---

## 5. 窗口内确定性择优

候选帧按以下顺序判断：

### 5.1 硬拒绝

以下帧不得进入 Qwen-VL 视频候选：

```text
missing_or_invalid_derived_frame
near_black
明显尾部落点
中心数据库已标记为 duplicate 且不属于 canonical
录屏视频帧
```

短视频固定定义为 `valid_sampled_frame_count < coverage_stride_frames`。只有短视频经过硬拒绝后没有正常 coverage 候选时，才允许保留最后一个有效 canonical visual，并记录 `short_video_tail_fallback`。普通或长视频不得使用 tail fallback；同一局部范围内 refill 仍失败时必须报告 coverage missing 并阻塞。

### 5.1.1 Tail-only coverage anchor remap

当非短视频某个 anchor 的原始固定局部窗口中，所有具有有效 `time_position_ms` 的 sampled frame 均满足 `time_position_ms >= tail_start_ms`，该 anchor 定义为 tail-only anchor。

tail-only anchor 不允许扩大 `coverage_local_radius_frames`、跳过 anchor、选择 tail protected frame 或使用长视频 tail fallback。必须在同一 `source_content_id` 的完整 Step02 sampled-frame 顺序中查找最后一个满足 `0 <= time_position_ms < tail_start_ms` 的 sampled-frame index，将其作为 `effective_anchor_index`，并以同一固定半径重新建立局部范围。最终候选仍只能来自 `canonical_visual_units_for_heavy`。

成功 remap 必须记录 `tail_only_anchor_remapped_to_last_non_tail`。若 remap 后复用相邻 anchor 已选 canonical visual，coverage 仍算完成，但候选队列不得重复写入。只有整条视频不存在任何非 tail 有效 sampled-frame index，或 remap 后仍无合法 canonical coverage candidate，才记录 remap failure 和 coverage missing。

coverage anchor report 必须同时保存 original/effective anchor 与 interval、`anchor_remap_reason`、`tail_start_ms` 和最终选择信息。summary 必须满足：

```text
tail_anchor_remap_count + tail_anchor_remap_failed_count = tail_only_anchor_count
non_short_video_tail_fallback_count = 0
```

### 5.2 高信号证据

高信号不能使用项目专用词组合。只允许通用类别证据：

```text
人物/社会场景
车辆/机器/设备
动物/生命主体
文字/屏幕/文档
场所/结构/道路
自然/土地/植物
食物/物件上下文
```

证据可来自：

- YOLOE 标签种类、置信度和 bbox
- 主体面积
- 主体中心性
- 主体是否严重贴边或裁切
- 相邻帧标签集合变化
- 视觉结构变化
- OpenCLIP 向量新颖度
- 16×5 grid 结构
- 明显文字承载区域

禁止：

```text
person + wheat
tractor + wheat
某个项目词组合加分
人物身份识别
睁眼闭眼判断
主观美丑判断
```

### 5.3 择优顺序

在同一 coverage 区间内，优先级固定为：

1. 通过硬门。
2. 具有通用高信号证据。
3. 主体完整、面积合理、中心性较好。
4. grid 结构非平坦。
5. 与相邻已选 coverage 帧内容不重复。
6. 距离 anchor 更近，作为最后 tie-breaker。
7. 若仍相同，按 `time_position_ms + visual_unit_id` 稳定排序。

---

## 6. 高价值候选角色

V25 固定使用以下角色，不再把所有视频候选统称为“高价值帧”：

```text
video_coverage_keyframe
video_high_signal_keyframe
video_coverage_high_signal_overlap
video_high_signal_supplement
```

含义：

- `video_coverage_keyframe`：覆盖代表帧，保证视频时间入口。
- `video_high_signal_keyframe`：高信号候选池成员，未必直接进入最终队列。
- `video_coverage_high_signal_overlap`：同时满足覆盖与高信号，优先保留。
- `video_high_signal_supplement`：覆盖之外、内容确实新颖的少量补充。

高信号候选优先在 coverage 区间内替换普通 anchor，不应先生成 coverage 后再机械追加。

---

## 7. 去重规则

### 7.1 中心去重

进入 V25 前，先使用中心 source/frame dedup 结果，只处理 canonical visual。

被折叠 visual 不删除，只从默认重型入口隐藏。

### 7.2 V25 候选内部去重

在同一视频内，对最终候选执行二次去重。证据包括：

```text
时间接近
OpenCLIP cosine
16×5 grid 相似度
非空 YOLOE 标签集合相似度
派生 SHA / derived identity
```

去重不能只依赖一个信号。推荐使用共识条件：

```text
时间接近
AND
(vector 高相似 OR grid 高相似)
AND
(非空 label 相似 OR vector 与 grid 同时确认)
```

两个空标签集合不得自动判为重复。

### 7.3 coverage 不得被去重破坏

若某个 coverage 代表被去重删除：

1. 在同一 coverage 区间内寻找下一个 canonical 候选。
2. 找到则 refill。
3. 找不到则保留原代表并标记 `dedup_kept_for_coverage`。
4. 不允许为了增加去重数量而制造 coverage 空洞。

---

## 8. OCR 分流规则

### 8.1 录屏

录屏视频：

```text
不进入 Qwen-VL 视频候选
允许进入 OCR
```

录屏 OCR 仍需做黑帧、无效帧和近重复过滤。

录屏路径 marker 固定包含 `screen recording`、`screenrecording`、`screen_record`、`录屏`、`屏幕录制` 和 `rpreplay`，匹配大小写不敏感。RPReplay 不进入普通视频 coverage 或 Qwen 视频候选，只允许进入现有录屏 OCR 路由。

### 8.2 普通视频

普通视频 OCR 只允许明显大字候选：

```text
明显文字承载类别
bbox 归一化面积达到规则门槛
置信度达到规则门槛
每个普通视频默认只保留极少量最强候选
```

弱标签如单独的 `phone/laptop/book/paper/logo/ticket` 不得自动触发 OCR。

### 8.3 图片

普通图片 OCR 不在本规则中自动扩大。

### 8.4 V25 图片 Qwen 候选

V25 禁止 `image_generic_visual_signal_candidate`。普通图片不得因 YOLOE、generic score、OpenCLIP 新颖度、grid、主体数量、构图或文字区域评分自动进入 Qwen。

最终图片候选仅为以下并集，并按 canonical visual 全局去重：

```text
Finder tag seed
UNION XMP sidecar seed
UNION timelapse representative
```

主角色优先级：

```text
image_finder_tag_seed
> image_xmp_sidecar_seed
> image_timelapse_representative
```

一张 canonical 图片只允许一条 Qwen 行。多来源命中必须保留在 `reason_codes` 和 `image_selection_audit.jsonl`。

#### Finder tag

Finder 标签只读取中心数据库 `source_finder_tags`。任意非空标签有效，只处理在线且未删除的 image source。标签 source 先经 `visual_units` 与 `visual_identity` 映射到 canonical visual；多标签或多个原 visual 映射同一 canonical 时只写一条。必须审计 tag id/raw/name/color、tagged source 和 tagged original visual。人工标签不受 generic score 或 near-black 单独否决，但派生预览缺失或不可读时不得进入候选。

#### XMP sidecar

XMP 只读取 `source_assets`，不扫描文件系统、不解析 sidecar。扩展名大小写不敏感且必须严格为 `.xmp`/`xmp`；普通 `.xml` 不匹配。图片与 XMP 必须同时在线未删除，并满足同一规范化相对父目录和大小写不敏感的完全相同 stem。匹配图片映射到 canonical visual。XMP 信号不受 generic score 或 near-black 单独否决，但无有效派生预览不得进入候选。

#### Timelapse adaptive 1/2/3

延时摄影读取 `step02_image_timelapse_keyframes`，按 sequence 的 first/middle/last 映射到 canonical visual，并用已有 derived identity、OpenCLIP vector、16x5 grid 与非空标签共识比较 first-middle、middle-last。不得运行模型。空标签集合不能单独构成相似证据。

```text
两段都相似 -> middle
first-middle 相似、middle-last 变化 -> middle + last
first-middle 变化、middle-last 相似 -> first + middle
两段都变化 -> first + middle + last
```

位置缺失时只对剩余有效 canonical 位置确定性选择，同一 canonical visual 只写一次。

---

## 9. 时间码与搜索区间

### 9.1 必须保留的字段

每个视频 visual，不论 canonical 还是 duplicate，都必须保留：

```text
source_content_id
visual_unit_id
canonical_visual_unit_id
duplicate_group_id
derived_id
frame_index
time_position_ms
canonical_time_ms
group_start_ms
group_end_ms
dedup_reason
```

### 9.2 搜索返回

视频搜索结果不能只返回孤立图片，必须返回可播放时间区间。

规则：

```text
hit_time_ms = 命中 visual 的原始时间
segment_start_ms = 命中点前的配置缓冲，并覆盖 duplicate group 起点
segment_end_ms = 命中点后的配置缓冲，并覆盖 duplicate group 终点
```

区间必须限制在视频实际时长内。

图片和延时摄影返回单张。
ASR 和音频使用原始片段开始/结束时间。

---

## 10. 数据库写入

V25 必须先完成所有内存选择和一致性校验，再使用单事务写入。

写入至少包括：

```text
candidate queue
候选角色
candidate score / ranking evidence
reason_codes
source_content_id
visual_unit_id
canonical_visual_unit_id
time_position_ms
duplicate_group_id
policy_version
script_version
script_sha256
config_sha256
central_dedup_run_id
YOLOE run_id
OpenCLIP run_id
created_at
```

失败必须回滚。

V25 candidate 普通运行不得自动执行 `ALTER TABLE`。缺少 commit 必需字段时必须阻塞并提示显式 migration。任何 migration 只能由 finalizer 在完整数据库备份后、明确事务中执行。

开发验证状态为 `FROZEN_CANDIDATE`，不得 commit。只有 candidate dry-run 与当前数据库回归全部通过后，`finalize_stop03_2_v25.py` 才能同时将规则文档和配置切换为 `FROZEN`。V25 commit 只接受 `policy_status = FROZEN`。

---

## 11. 自动验收闸门

生产运行不要求人工逐步确认。总调度器自动执行：

```text
preflight
→ canonical input check
→ V25 dry-run in memory
→ technical/policy self-check
→ DB backup
→ transaction commit
→ DB readback
→ integrity/foreign-key check
→ idempotency check
→ 下一阶段
```

自动阻塞条件：

```text
普通视频 coverage 丢失
black leak
录屏进入 Qwen-VL
duplicate visual 进入正式队列
manifest 与 DB 不一致
向量 payload 声称启用但实际不可读
grid 声称启用但执行计数为 0
写库失败或反查不一致
```

---

## 12. 版本冻结

正式标识：

```text
policy_version = stop03_2_generic_high_value_policy_v25
```

完成实现和回归后记录：

```text
script_sha256
config_sha256
rule_document_sha256
```

V25 冻结后不得原地修改。任何算法、阈值语义、输入范围、角色或路由变化必须创建 V26。

---

## 13. V25 实现验收

V25 进入 `FROZEN` 前必须完成：

1. 当前中心数据库回归 dry-run。
2. coverage 实际执行计数大于 0。
3. `coverage_local_radius_frames` 在每个 anchor 的实际候选路径中生效。
4. local candidate evaluation 实际执行。
5. vector/grid pair evaluation 实际执行。
6. central duplicate queue leak 为 0。
7. 普通视频 coverage 无缺失。
8. `non_short_video_tail_fallback_count = 0`。
9. commit 与 DB readback 一致。
10. 相同输入二次运行幂等。
11. 原始素材和派生帧无修改。
12. 人工只做一次冻结验收，不进入正式生产流程。
13. V24 基线排除 RPReplay source 后，剩余普通视频语义集合一致。
14. Finder、XMP、timelapse 图片回归通过，且普通图片自动评分候选为 0。
15. 第二次相同输入 commit 不增加候选表行数或重复 candidate_id。

完成后：

```text
status = FROZEN
```
