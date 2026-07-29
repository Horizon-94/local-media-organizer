# Stop03-5E 全视觉覆盖缺口审计

状态：`NON_NORMATIVE / GAP_CONFIRMED / V2_REAL_QUERY_PASS / GAP_RESOLVED`

本文记录当前测试项目的实际数量，不是通用完成门槛。

## 白话结论

原来的 V1 搜索没有弄丢已经生成的文字，但它只搜索“有文字说明的画面”。高价值帧、OCR
帧和前后传播帧合计形成758条文档，对应755个不同画面；剩下的画面虽然已经抽帧并做过
OpenCLIP/YOLOE，却没有进入这套文字文档。因此“407个唯一文本向量全部扫描”只说明
文字库查完了，不能说明素材画面全部查完了。

## 中心数据库反查

```text
visual units = 1628
images = 433
video frames = 1195

OpenCLIP vectors = 1628
OpenCLIP visual coverage = 1628 / 1628

Stop03-5D text documents = 758
Stop03-5D distinct visual units = 755

YOLOE visual units with detections = 1374
YOLOE detection rows = 5188
visual units with no YOLOE detection row = 254
```

YOLOE在全部输入画面上执行，但没有检出物体的画面不会产生 `visual_labels` 行。因此
YOLOE标签不能单独承担全量召回。OpenCLIP已有每个视觉单元一条512维图像向量，才是
现成的全量视觉检索底座。

## 修复

V2 查询不修改 V1，也不重跑模型：

1. 每次查询必须扫描过滤范围内全部 OpenCLIP 视觉向量；
2. 已有5D文本向量作为详细语义排序证据；
3. 已有YOLOE标签作为明确物体匹配证据；
4. 三种分数用 reciprocal-rank fusion 合并，不直接混加不同模型的 cosine；
5. 返回结果继续使用派生JPEG、可读时间码和5/10秒播放区间。

正式中心数据库 preflight 和 dry-run 已通过：OpenCLIP run 完整、1628条向量 payload
全部存在且维度、有限值、归一化、SHA256均正确；数据库 integrity 为 `ok`、外键错误为0，
中心数据库未改变。

V2 随后完成一次真实本地查询：全部1628条视觉向量和407条唯一文本向量均被扫描；前20
项结果包含19段视频、1张图片，其中5项来自没有5D文本的视觉单元，证明全量视觉补充通道
实际参与了召回。20张派生预览图全部可访问，19段视频均返回10秒播放区间。数据库前后
SHA256一致，网络、下载、原始媒体读取、数据库写入和搜索索引均为false。

用户接受本次视觉结果，并指出个别“侧卧/仰卧”描述可能受横竖屏显示方向影响。该问题已
作为通用方向敏感描述规则记录，不改写原始Qwen文本，不阻塞V2冻结。
