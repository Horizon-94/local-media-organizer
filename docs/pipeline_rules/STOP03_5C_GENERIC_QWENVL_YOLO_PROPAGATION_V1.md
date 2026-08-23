# Stop03-5C Generic Qwen-VL / YOLOE Semantic Propagation V1

状态：`PASS / RULE_FROZEN / COMMITTED`

## 1. 范围

本规则只传播最新成功 Stop03-5B staging run 中的 Qwen-VL 视频高价值帧证据。
OCR 不传播；图片和延时摄影代表图不做相邻视频帧传播；历史 staging run 不参与。

本规则冻结算法标准，不冻结当前项目的锚点数、视频数、帧数或传播行数。

## 2. 帧序与传播半径

每个源视频以中心数据库 `derived_assets` 中的 `frame_index`、`time_position_ms`、
`derived_id` 稳定排序。相同 `derived_id` 的重复 visual unit 必须先折叠为一个唯一
派生帧，并使用 `visual_identity.canonical_visual_unit_id` 作为目标 canonical ID。

每个直接 Qwen-VL 视频锚点最多传播到：

```text
previous 1, previous 2, previous 3
next 1, next 2, next 3
```

视频首尾不足三帧时只处理实际存在的帧。禁止跨 `source_content_id`，禁止把传播
结果再次作为传播源，禁止按固定毫秒数替代前后 3 帧。

目标无论是普通帧还是另一个高价值帧都可生成传播记录；若目标已有直接 Qwen-VL
证据，直接证据优先，传播结果不得覆盖或改写直接证据。

## 3. 三方语义门

每个传播对象标签必须同时满足：

```text
标签被源 Qwen-VL 文本明确提及
AND 源帧 YOLOE 检测到该标签
AND 目标帧 YOLOE 检测到该标签
```

源帧和目标帧 YOLOE confidence 均不得低于 policy 中的
`yoloe_min_confidence`。标签的中文名称和检索同义词必须来自中心数据库
`visual_label_terms`；正式脚本不得维护项目专用物体词表。

例如源 Qwen 文本含 100 个语义点，但目标帧只与源帧共同确认其中 10 个对象，
则只输出这 10 个对象标签。其余 90 个语义点不得进入传播文本或传播属性。

## 4. 第一版传播内容

第一版只传播 object label 原子。每个 source/target/label 生成一个独立、可追溯的
传播记录和受控文本：

```text
相邻高价值帧传播对象：<label_zh>（<canonical_label>）
```

禁止复制完整 Qwen caption，禁止复制包含未通过三方交集的原句。

以下内容第一版一律不传播：

- OCR 或屏幕文字；
- scene / environment；
- action / state / relation；
- count；
- fine detail；
- Qwen 文本中存在但 YOLOE 未共同确认的对象。

## 5. 最新结果选择

正式入口必须自动选择：

```text
stop03_5_unified_evidence_runs
WHERE status = success
ORDER BY created_at DESC, staging_run_id DESC
LIMIT 1
```

只读取该 run 的 `modality=qwenvl` 且质量状态在 policy 允许集合内的 evidence。
不得固定当前 staging_run_id、Qwen run_id、候选数量或传播数量。

## 6. 输出与安全

`preflight` 不创建输出。`dry-run` 只写 test-output：

- `reports/stop03_5c_summary.json`
- `manifests/semantic_propagation.jsonl`
- `manifests/semantic_propagation_index.csv`
- `manifests/propagation_targets.csv`

本阶段不运行模型、不读取原始视频、不联网、不下载、不修改派生帧。
dry-run 不写中心数据库。

`commit` 只能在显式 `--confirm-central-db-write` 下执行，并且只允许：

- 应用 `migrations/20260717_stop03_5c_qwenvl_yolo_propagation_v1.sql`；
- 写入 `stop03_5c_propagation_runs`；
- 写入 `stop03_5c_propagation_items`；
- 创建或更新只读视图 `v_stop03_5c_latest_propagation`。

commit 前必须备份中心数据库；推送结果后必须执行 readback、SQLite integrity 和
foreign key check。相同 payload 再次提交必须返回 `IDEMPOTENT_PASS`，不得新增 run
或重复 propagation item。

## 7. 技术 PASS

必须同时满足：

- 最新 5B run 存在且为 success；
- 所有源锚点来自该 run 的 Qwen-VL 视频 evidence；
- OCR source count 为 0；
- 每条传播的 frame step 在 1–3；
- source/target 属于同一 source；
- source/target derived ID 不相同；
- propagated label 属于 Qwen mention、source YOLOE 和 target YOLOE 三方交集；
- 所有 YOLOE confidence 达到阈值；
- propagation ID 唯一；
- 没有完整 Qwen 文本泄漏；
- dry-run 前后中心数据库 SHA 不变；
- SQLite integrity 与 foreign key check 通过。

当前项目的实际计数只能写入验收结果章节，不得进入通用 PASS 逻辑。

## 8. 当前项目 dry-run 与正式提交验收记录

以下仅是当前素材库的执行记录：

```text
source staging run = stop03_5b_8ae4389ecd58010eeb9315c1
Qwen video anchors = 205
OCR anchors = 0
unique video frames = 1097
visual-unit aliases collapsed = 98
candidate neighbor pairs = 699
propagation rows = 623
propagation targets = 427
targets already containing direct Qwen = 56
source anchors without YOLOE = 18
source anchors without Qwen/source-YOLOE label intersection = 43
database integrity = ok
foreign key errors = 0
dry-run central database unchanged = true
propagation run = stop03_5c_9f834ea8a6ed014f00ebc8f5
payload digest = 9f834ea8a6ed014f00ebc8f5b55cc89832b327d1f321a7f6101ac449ffb1783f
formal commit status = COMMITTED
idempotent repeat status = IDEMPOTENT_PASS
database propagation runs = 1
database propagation rows = 623
duplicate propagation IDs = 0
duplicate source/target/label semantics = 0
latest propagation view rows = 623
database integrity = ok
foreign key errors = 0
```

传播行的 step 分布：

```text
step 1 = 230
step 2 = 208
step 3 = 185
```

该计数不属于规则阈值。新的素材库必须按其最新成功 5B run 和实际派生帧动态计算。

正式提交前备份：

```text
$USER_HOME/Documents/AI-Local/test-output/stop03_5c_qwenvl_yolo_propagation_v1/backups/media_archive.sqlite.2026-07-16T161633+0000.bak
```

Stop03-5C 已停止在传播结果中心数据库落库与幂等反查完成的位置。未经新的阶段授权，
不得更新 embedding，不得创建或更新搜索索引。
