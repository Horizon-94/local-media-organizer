# 模型安装说明

本程序和 DMG **不包含模型，也不会自动联网下载模型**。模型由用户直接
从上游项目取得，并遵守各模型自己的许可证。Horizon-94 只提供目录合同
和上游链接，不重新托管权重。

## 1. 模型总目录

默认目录：

```text
~/Library/Application Support/素材大整理/Models
```

也可以在应用的“处理设置 → 本地模型位置”中选择外置硬盘或其他目录。
以下所有路径都相对于这个总目录。

## 2. 固定目录结构

```text
Models/
├── yoloe26-l-seg/
│   ├── weights/
│   │   └── yoloe-26l-seg.pt
│   └── mobileclip2_b.ts
├── openclip-vit-b-32-laion2b-s34b-b79k/
│   └── model.safetensors
├── Qwen3-VL-4B-Instruct-4bit/
│   ├── config.json
│   ├── model.safetensors
│   └── 其余 tokenizer/processor 文件
├── Qwen3-Embedding-0.6B/
│   ├── config.json
│   └── 其余模型和 tokenizer 文件
├── ocr/
│   ├── PP-OCRv6_medium_det/
│   └── PP-OCRv6_medium_rec/
└── insightface/
    └── buffalo_l/                 # 可选；单独核对许可
```

## 3. 上游下载页面

| 用途 | 上游 | 本地相对路径 |
|---|---|---|
| YOLOE | [yoloe-26l-seg.pt](https://github.com/ultralytics/assets/releases/download/v8.4.0/yoloe-26l-seg.pt) | `yoloe26-l-seg/weights/yoloe-26l-seg.pt` |
| YOLOE 文本编码器 | [mobileclip2_b.ts](https://github.com/ultralytics/assets/releases/download/v8.4.0/mobileclip2_b.ts) | `yoloe26-l-seg/mobileclip2_b.ts` |
| OpenCLIP | [LAION CLIP ViT-B/32](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K/blob/main/model.safetensors) | `openclip-vit-b-32-laion2b-s34b-b79k/model.safetensors` |
| Qwen-VL | [MLX Qwen3-VL-4B-Instruct-4bit](https://huggingface.co/mlx-community/Qwen3-VL-4B-Instruct-4bit/tree/main) | `Qwen3-VL-4B-Instruct-4bit/` |
| 文本向量 | [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/tree/main) | `Qwen3-Embedding-0.6B/` |
| OCR 检测 | [PP-OCRv6_medium_det](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det) | `ocr/PP-OCRv6_medium_det/` |
| OCR 识别 | [PP-OCRv6_medium_rec](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec) | `ocr/PP-OCRv6_medium_rec/` |
| 人物归并（可选） | [InsightFace v0.7 model packs](https://github.com/deepinsight/insightface/releases/tag/v0.7) | `insightface/buffalo_l/` |

机器可读的同一清单位于 `configs/model_sources_v1.json`。

## 4. 重要许可提醒

- 模型不是 GPL 程序的一部分，各自许可证独立生效。
- Ultralytics 软件和模型需要在下载页面重新核对许可。
- `buffalo_l` 上游明确要求商业使用者联系 InsightFace 确认模型许可；
  因此人物归并默认视为可选能力。
- 不确定许可证时不要下载或启用对应模型。

## 5. 本地检查

以下命令只创建空目录并显示链接，不下载任何内容：

```bash
python scripts/prepare_model_directories.py
```

模型放好后，在应用设置页选择总目录并点击“检查并保存”。应用会逐项显示
“已找到”或“缺失”。
