# Stop03-2 通用高价值帧规则 V22.1（DR V18）

状态：V22.1 dry-run 实现规则。适用于通用本地素材库，不针对单一项目或主题。

## 1. 边界

V22.1 只修订普通视频的窗口内排序、尾帧保护和 supplement 信息增益门。继续复用 V22 的 18 秒 coverage window、V14 高信号窗口竞争、已有 OpenCLIP 512 维向量、16×5 grid、bbox 归一化、OCR 窄口、录屏分流、图片/manual seed/timelapse 分支、dedup+refill、manifest 和事务型 commit 框架。

本阶段只允许运行 `--preflight-only` 和 `--dry-run`。禁止联网、下载、安装依赖、加载或重跑模型、读取原始视频、修改原始素材、写模型目录或写中心 SQLite。

## 2. 人物窗口质量

仅使用已有派生帧和已有 YOLOE bbox。计算：

- `human_present`、`face_present`
- person/face bbox 面积、中心距离、四边贴边数
- person/face crop penalty
- partial-body-only penalty
- person/face overlap bonus
- 派生帧 Sobel Tenengrad 清晰度及窗口内相对清晰度

不做人脸身份、情绪或睁闭眼判断。人物窗口按分层证据排序：硬质量门；完整人物/脸、person+face overlap、V14 真高信号及窗口相对清晰度；主体面积/中心性/裁切；最后才使用通用标签、grid、vector novelty 和 anchor 时间距离。每次替换必须保留 V22 旧帧、新帧、质量分及原因。

## 3. 尾帧保护

有效跨度大于 30 秒：

```text
tail_window_ms = clamp(round(effective_span_ms * 0.05), 6000, 30000)
tail_start_ms = effective_end_ms - tail_window_ms
```

保护区默认不得进入 coverage、overlap 或 supplement。尾部窗口无非尾帧时，先从前一窗口未选且不近重复的候选借位；没有安全候选则折叠该尾部窗口。只有全视频无其他合格帧时允许 `video_coverage_fallback`，且不得标为 high signal 或 supplement。

## 4. Supplement 信息增益

V14 未选高信号帧必须同时通过：非黑、非尾、最小时间间隔、dedup 共识、质量门、cap 和真实信息增益。真实增益包括新增通用标签/人物/车辆/动物/机器/文字承载区、主体数量明显增加、主体中心性明显改善、新增 V14 高信号证据，或有主体证据支持的明显场景结构变化。

只有 vector、grid、亮度、颜色、轻微视角或相机平移差异时拒绝，并标记 `supplement_grid_or_vector_only`。所有通过与拒绝候选均写逐条审计，包含最近 coverage、时间差、vector cosine、grid MAD/correlation、标签 Jaccard、gain 和 reject reason codes。

## 5. 清晰度

使用已安装 Pillow + NumPy 在已有派生帧上计算 `sobel_tenengrad_energy_pillow_numpy`。不使用全局语义阈值；人物窗口内按相对清晰度排序。读取或计算失败必须计数，不能静默降级为可用。

## 6. 角色与状态

允许视频角色：

```text
video_coverage_keyframe
video_coverage_high_signal_overlap
video_high_signal_supplement
video_coverage_fallback
```

技术状态只证明安全边界、输入输出一致性、无泄漏、91/91 覆盖和 SQLite 零写入。策略状态还必须证明人物替换、尾帧拒绝和 supplement 信息增益 gate 实际执行。dry-run 与 HTML 人工确认前保持 `policy_status=REVIEW`、`commit_status=DO_NOT_COMMIT`。

## 7. 审计输出

除 V22 manifest、decisions、coverage window 和 video budget 外，V22.1 新增：

```text
human_quality_replacements.jsonl
supplement_information_gain_audit.jsonl
v22_v22_1_replacement_mapping.jsonl
v22_1_added_video_rows.jsonl
v22_1_removed_video_rows.jsonl
tail_change_samples.jsonl
```

Contact sheet 继续只使用输出目录下 `assets/<safe_filename>`；优先相对软链接，失败时只读复制。HTML 不得包含绝对路径、`file://` 或 `../` 图片引用，并逐帧展示人物质量、清晰度、尾部状态、supplement gain、vector/grid 最近邻证据、窗口排名及选择/拒绝原因。

## 8. 安全验收

dry-run 前后必须验证中心 SQLite SHA-256、mtime、`stop03_2_candidate_queue_items` 行数、`model_runs` 行数均不变，V22.1 `model_runs` 新增为 0。任何写库、模型运行、原始视频读取、录屏进入 Qwen、黑帧泄漏或普通视频缺覆盖均为技术 FAIL，并禁止 commit。
