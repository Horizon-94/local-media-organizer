# Stop03-5E 当前测试项目验收记录

状态：`NON_NORMATIVE / PASS / ENVIRONMENT_AMBIGUITY_EXPOSED / UI_PLAYBACK_DEFERRED`

本文只记录当前测试文件夹的实际结果，不是通用规则，不得用其中数量作为以后项目的完成门槛。

## 本次实际结果

```text
embedding run = stop03_5d_db064187a0a53b7f8cd7c598
queries = 5
eligible documents = 758
eligible unique vectors = 407
scanned unique vectors = 407
displayed result documents = 40
preview assets = 40
missing preview assets = 0
video preview segments = 22
preview window = 5000 ms
```

查询模型使用本地 `Qwen3-Embedding-0.6B`，设备为 MPS。模型加载约1.27秒，5条 query
向量约0.22秒，407个唯一向量 cosine 扫描约0.26秒。

## 安全与完整性

```text
technical status = PASS
policy status = REVIEW
database SHA before = database SHA after
database write = false
network used = false
download used = false
original media read = false
search index created = false
original video clip generated = false
```

HTML中的40个图片引用全部为 `assets/` 相对路径。40个现成派生JPEG均通过相对软链接
进入 smoke 输出目录；没有绝对路径、`file://`、父目录跳转或缺失资源。

时间码经人工反馈后改为人类可读格式。默认显示到毫秒：小于一小时为 `MM:SS.mmm`，
超过一小时为 `HH:MM:SS.mmm`；界面也可选择只显示到秒。数据库原始毫秒值没有改变。

## 人工反馈与语义质量待办

用户确认图片和整体搜索表现基本可用，但发现“室内人物肖像”结果中混入实际为夜间户外的
自拍视频。核对派生帧和数据库文字后确认：向量搜索没有改写场景，上游 Qwen 对同一夜间
视频的描述不一致；部分帧正确写“城市夜景”，部分帧把建筑外墙误写为“室内墙面”。

因此基础搜索接口冻结时不把环境语义标记为完全准确，而是增加：

```text
indoor
outdoor_day
outdoor_night
outdoor
night_or_indoor
indoor_or_outdoor
unknown
```

视频场景环境结合当前点附近已有 Qwen 描述做非破坏性一致性判断。证据矛盾时不得仅凭
“画面暗”强行判断；本次“室内人物肖像”的第2、5、8项均显示“夜间/室内（待确认）”，
用户可在后续 UI 中自行确认。该处理没有改写冻结向量或原始模型输出。

正式播放器属于 UI 阶段。搜索结果已经提供 `source_content_id`、命中时间和播放区间；UI
可直接跳到区间起点并播放5秒或10秒，不需要预先裁出新视频。

## 完整通用查询入口真实验收

以下数字只描述当前测试项目，不属于通用规则：

```text
request id = query5e_82d5e54ee76ddcec3a2de50f
query = 不落盘；仅以 SHA256 和长度记录
embedding run = stop03_5d_db064187a0a53b7f8cd7c598
eligible documents = 758
eligible unique vectors = 407
scanned unique vectors = 407
returned vector groups = 8
total vector groups = 407
next group offset = 8
displayed documents = 8
preview assets = 8
missing preview assets = 0
video preview segments = 8
preview window = 10000 ms
```

查询使用本地 `Qwen3-Embedding-0.6B` 和 MPS。模型加载约1.28秒，单条 query 向量约
0.17秒，完整 cosine 扫描约0.09秒，总请求约8.59秒。8张图片全部使用输出目录内
`assets/` 相对软链接，静态资源检查通过；8个视频结果都返回10秒播放区间及毫秒时间码。

查询“夜间户外戴眼镜的人物”的前列结果主要命中对应的夜间人物视频；上游环境描述有
冲突的结果显示“夜间/室内（待确认）”，没有强行改写成单一结论。真实入口状态为
`PASS`，中心数据库前后 SHA256 一致，且：

```text
database write = false
network used = false
download used = false
original media read = false
search index created = false
all results traceable = true
all displayed documents have preview assets = true
```

## V2 混合全视觉查询验收

V1验收后反查确认：758条文本文件只对应755个不同视觉单元，不能代表全部画面。V2使用
已有OpenCLIP全量视觉向量补齐召回，5D文本和YOLOE只作为附加排序证据。

```text
request id = query5ev2_02d8b9374bc4c9b7fcab3101
visual units = 1628
images = 433
video frames = 1195
validated OpenCLIP vectors = 1628
scanned visual vectors = 1628
scanned unique text vectors = 407
text-scored visual units = 755
YOLOE query-matched visual units = 904
results before temporal dedup = 1628
results after temporal dedup = 989
returned results = 20
returned images = 1
returned video frames = 19
returned results without 5D text = 5
preview assets = 20
missing preview assets = 0
video preview segments = 19
preview window = 10000 ms
```

本次总请求约20.75秒，其中OpenCLIP子进程约10.87秒，Qwen3 query embedding约0.25秒。
全部20张图片使用相对 `assets/` 软链接，静态HTTP资源检查通过。数据库前后SHA256均为
`331b76d6b877fd178dce65f60a0ffba3e262715ce697759099ea20172c06f754`。

结果中19段视频和1张图片是本次查询的相关度排序，不是图片覆盖缺失；433张图片都已参加
向量扫描。个别Qwen文本写“侧卧/仰卧”，用户指出可能受横竖屏显示方向影响。本次不改写
上游文本；冻结规则要求播放器处理显示旋转，方向敏感姿态词只作为可复核证据。
