# Model Sources

本仓库和官方 DMG 都不分发模型权重，也不会自动下载模型。程序的 GPL-3.0 授权**不覆盖**下列第三方模型；使用者必须自行阅读并遵守每个上游页面的许可证。

默认模型根目录：

```text
~/Library/Application Support/素材大整理/Models
```

| 根目录下的相对路径 | 用途 | 上游来源 |
|---|---|---|
| `Qwen3-VL-4B-Instruct-4bit/` | 图片/关键帧描述 | [mlx-community/Qwen3-VL-4B-Instruct-4bit](https://huggingface.co/mlx-community/Qwen3-VL-4B-Instruct-4bit/tree/main) |
| `Qwen3-Embedding-0.6B/` | 文本向量 | [Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/tree/main) |
| `yoloe26-l-seg/weights/yoloe-26l-seg.pt` | 开放词汇检测/分割 | [Ultralytics 官方权重](https://github.com/ultralytics/assets/releases/download/v8.4.0/yoloe-26l-seg.pt) |
| `yoloe26-l-seg/mobileclip2_b.ts` | YOLOE 文本提示编码 | [Ultralytics 官方资源](https://github.com/ultralytics/assets/releases/download/v8.4.0/mobileclip2_b.ts) |
| `openclip-vit-b-32-laion2b-s34b-b79k/model.safetensors` | 全视觉向量 | [LAION OpenCLIP 权重](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K/blob/main/model.safetensors) |
| `ocr/PP-OCRv6_medium_det/` | OCR 检测 | [PaddlePaddle/PP-OCRv6_medium_det](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det) |
| `ocr/PP-OCRv6_medium_rec/` | OCR 识别 | [PaddlePaddle/PP-OCRv6_medium_rec](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec) |
| `insightface/buffalo_l/` | 可选人物归并 | [InsightFace v0.7 模型包](https://github.com/deepinsight/insightface/releases/tag/v0.7) |

`buffalo_l` 是可选项。InsightFace 对代码与预训练模型有不同许可说明，尤其是商业使用；使用前必须向上游确认单独授权。

机器可读清单、已知 SHA-256 与许可复核链接位于 [`configs/model_sources_v1.json`](configs/model_sources_v1.json)。目录准备命令只创建空目录并打印上游链接：

```bash
python scripts/prepare_model_directories.py
```

来源链接仅用于帮助用户找到上游项目，不代表 Horizon-94 为第三方模型提供担保、再授权或镜像服务。
