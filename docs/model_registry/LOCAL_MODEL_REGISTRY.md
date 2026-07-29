# LOCAL_MODEL_REGISTRY.md

> 适用项目：本地素材大整理 / media-archive-clean
> 项目根目录：`/Users/yourname/Documents/AI-Local/media-archive-clean`
> 模型根目录：`/Users/yourname/Documents/model`
> 生成目的：让后续所有对话、脚本、任务、Codex/Opus 审查都优先使用本机已有模型；找不到模型时必须停止并报告，禁止自动下载。

---

## 0. 最高规则

本项目是本地离线素材整理项目。

后续任何任务，只要涉及模型、权重、推理、OCR、YOLO、Whisper、Embedding、Qwen-VL、VAD，都必须先检查本文件中的本地模型路径和运行环境。

未经用户明确允许，禁止：

- 下载模型
- 下载权重
- 下载测试素材
- 从 HuggingFace 自动拉取模型
- 从 ModelScope 自动拉取模型
- 从 Ultralytics 自动拉取 YOLO 权重
- 从 PaddleOCR / PaddleX 自动拉取官方模型
- 用远程模型名替代本地路径
- 把 `~/.cache` 或 `~/.paddlex` 当作正式模型源

如果本地模型不存在、路径不完整、运行环境缺失，任务必须判定为：

```text
BLOCKED_MISSING_LOCAL_MODEL
```

并报告：

```text
1. 缺少哪个模型
2. 当前代码在哪里尝试调用它
3. 应该放到哪个本地路径
4. 是否需要用户确认后再处理
```

---

## 1. 已确认本地模型总览

当前本地正式模型目录：

```text
/Users/yourname/Documents/model
```

已确认本地模型总大小：

```text
7.38 GB
```

已确认本地模型数量：

```text
7 个模型目录
```

| 模型目录 | 用途 | 大小 | 路径 |
|---|---|---:|---|
| `Qwen3-VL-4B-Instruct-4bit` | 图像理解 / 视觉语言 | 2.90 GB | `/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit` |
| `whisper-large-v3-turbo` | 语音转写 | 1.51 GB | `/Users/yourname/Documents/model/whisper-large-v3-turbo` |
| `yoloe26-l-seg` | YOLOE 检测 / 分割 | 1.14 GB | `/Users/yourname/Documents/model/yoloe26-l-seg` |
| `Qwen3-Embedding-0.6B` | 文本向量 | 1.12 GB | `/Users/yourname/Documents/model/Qwen3-Embedding-0.6B` |
| `openclip-vit-b-32-laion2b-s34b-b79k` | 图片视觉向量 | 577.11 MB | `/Users/yourname/Documents/model/openclip-vit-b-32-laion2b-s34b-b79k` |
| `ocr` | OCR 检测 / 识别 | 132.72 MB | `/Users/yourname/Documents/model/ocr` |
| `silero-vad` | VAD 语音活动检测 | 10.80 MB | `/Users/yourname/Documents/model/silero-vad` |

---

## 2. Qwen3-VL 本地模型

用途：

```text
图片理解
视频关键帧理解
高价值帧描述
画面语义提取
```

模型路径：

```text
/Users/yourname/Documents/model/Qwen3-VL-4B-Instruct-4bit
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/qwen-vl/bin/python
```

已确认环境包：

```text
mlx
mlx-vlm
transformers
sentencepiece
Pillow
```

规则：

```text
必须从本地路径加载。
禁止使用 Qwen/xxx 这类远程模型名。
禁止触发 HuggingFace 下载。
```

---

## 3. Whisper 本地模型

用途：

```text
音频转文字
视频语音转写
后续语音搜索
```

模型路径：

```text
/Users/yourname/Documents/model/whisper-large-v3-turbo
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/whisper/bin/python
```

已确认环境包：

```text
mlx-whisper
torch
numpy
```

规则：

```text
优先使用 mlx-whisper 本地路径。
禁止从 ~/.cache/whisper 或网络自动下载模型。
缺少模型时必须 BLOCKED。
```

---

## 4. YOLOE-26L 本地模型

用途：

```text
图片 / 视频帧目标检测
视觉标签
OCR 触发辅助
画面对象粗筛
```

模型目录：

```text
/Users/yourname/Documents/model/yoloe26-l-seg
```

核心权重：

```text
/Users/yourname/Documents/model/yoloe26-l-seg/weights/yoloe26-l-seg.pt
```

核心权重大小：

```text
75.25 MB
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/media-archive-v06-yolo/bin/python
```

已确认环境包：

```text
torch
ultralytics
Pillow
numpy
```

绝对禁止：

```python
YOLO("yolo11x.pt")
YOLO("yolov8x.pt")
YOLO("yoloe.pt")
YOLO("yoloe-11l-seg.pt")
```

正确原则：

```text
必须显式传入本地权重：
/Users/yourname/Documents/model/yoloe26-l-seg/weights/yoloe26-l-seg.pt
```

如果该文件不存在，必须停止，不允许 Ultralytics 自动下载。

---

## 5. Qwen3-Embedding 本地模型

用途：

```text
OCR 文本向量
Whisper 文本向量
Qwen-VL 描述文本向量
文件名 / 路径文本向量
后续语义搜索
```

模型路径：

```text
/Users/yourname/Documents/model/Qwen3-Embedding-0.6B
```

建议运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python
```

该环境已确认：

```text
torch
open-clip-torch
safetensors
Pillow
numpy
```

规则：

```text
正式接入前需要单独验证 Qwen3-Embedding 的本地加载方式。
禁止 from_pretrained("Qwen/xxx")。
必须从 /Users/yourname/Documents/model/Qwen3-Embedding-0.6B 加载。
```

---

## 6. OpenCLIP 本地视觉向量模型

用途：

```text
图片视觉向量
视频帧视觉向量
相似图召回
低成本视觉语义检索
```

模型目录：

```text
/Users/yourname/Documents/model/openclip-vit-b-32-laion2b-s34b-b79k
```

核心权重：

```text
/Users/yourname/Documents/model/openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python
```

已确认环境包：

```text
torch
open-clip-torch
safetensors
Pillow
numpy
```

规则：

```text
必须从本地 safetensors 加载。
禁止让 open_clip 自动下载 pretrained 权重。
```

---

## 7. OCR 本地模型

用途：

```text
图片 OCR
视频帧 OCR
录屏文字识别
截图文字识别
后续 OCR 搜索
```

模型根目录：

```text
/Users/yourname/Documents/model/ocr
```

检测模型：

```text
/Users/yourname/Documents/model/ocr/PP-OCRv6_medium_det
```

识别模型：

```text
/Users/yourname/Documents/model/ocr/PP-OCRv6_medium_rec
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/media-archive-v06-ocr/bin/python
```

已确认环境包：

```text
paddleocr
paddlepaddle
paddlex
Pillow
numpy
```

正确 OCR 初始化原则：

```python
PaddleOCR(
    text_detection_model_dir="/Users/yourname/Documents/model/ocr/PP-OCRv6_medium_det",
    text_recognition_model_dir="/Users/yourname/Documents/model/ocr/PP-OCRv6_medium_rec",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False
)
```

禁止：

```python
PaddleOCR()
```

原因：

```text
PaddleOCR() 默认可能触发 PaddleX 官方模型下载或使用 ~/.paddlex/official_models 缓存。
```

注意：

```text
当前正式 OCR 主线只允许用 /Users/yourname/Documents/model/ocr。
不得默认使用 ~/.paddlex/official_models。
```

---

## 8. Silero VAD 本地模型

用途：

```text
语音活动检测
音频切段
Whisper 前置处理
```

模型路径：

```text
/Users/yourname/Documents/model/silero-vad
```

运行环境：

```text
/Users/yourname/Documents/AI-Local/envs/media-archive-v06-vad/bin/python
```

已确认：

```text
numpy
```

规则：

```text
正式接入前需要单独做本地-only smoke。
不得自动下载 VAD 模型。
```

---

## 9. 隐式下载缓存：不得作为正式模型源

以下目录只作为缓存审计对象，不是正式模型源。

### HuggingFace 缓存

```text
/Users/yourname/.cache/huggingface
```

大小：

```text
1.13 GB
```

规则：

```text
不能默认使用。
如需迁移其中模型，必须先清点，再由用户确认。
```

### PaddleX 官方模型缓存

```text
/Users/yourname/.paddlex/official_models
```

大小：

```text
176.57 MB
```

说明：

```text
这是前面 OCR 脚本错误触发 PaddleX 默认模型逻辑后产生的缓存。
```

规则：

```text
不能默认作为 OCR 正式模型源。
不能在正式流程里读取该目录。
如需迁移，必须由用户明确确认。
建议迁移目标：
/Users/yourname/Documents/model/ocr-paddlex-official-cache-20260708
```

### ModelScope 缓存

```text
/Users/yourname/.cache/modelscope
```

大小：

```text
0B / 2 files
```

规则：

```text
不能默认作为正式模型源。
```

---

## 10. 后续模型调用固定检查流程

任何脚本、任务、Codex 指令，只要涉及模型，必须先执行以下判断：

```text
1. 任务需要哪个模型？
2. 本文件是否列出该模型？
3. 本地模型路径是否存在？
4. 运行环境是否存在？
5. 代码是否显式使用本地路径？
6. 是否存在远程模型名？
7. 是否存在默认下载逻辑？
8. 是否可能写入 ~/.cache、~/.paddlex、~/.ultralytics？
```

如果发现模型不存在：

```text
停止。
不要下载。
不要替换。
不要使用默认模型。
报告 BLOCKED_MISSING_LOCAL_MODEL。
```

如果发现代码中有远程模型名或默认模型名：

```text
停止。
不要运行。
报告 BLOCKED_REMOTE_MODEL_REFERENCE。
```

---

## 11. 后续对 ChatGPT / Codex / Opus 的固定要求

后续任何模型相关任务，必须先遵守本文件。

回答或生成代码前，必须先确认：

```text
本地模型路径
本地运行环境
是否禁止下载
是否禁止远程模型名
是否禁止默认模型名
```

除非用户明确说“允许下载”，否则不允许联网下载任何模型。

如果无法确认本地模型路径，必须先问用户或停止，不能自行下载。

---

## 12. 当前正式结论

```text
本项目已有本地模型，不应再自动下载模型。

正式模型源：
/Users/yourname/Documents/model

正式运行环境：
/Users/yourname/Documents/AI-Local/envs

隐式缓存不是正式模型源：
/Users/yourname/.cache/*
/Users/yourname/.paddlex/*
```
