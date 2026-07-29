# Model Sources

本仓库不分发任何模型权重。下表只记录源码支持的模型家族与上游来源；使用者必须自行阅读并遵守每个模型的许可证。

| 本地目录约定 | 用途 | 上游来源 | 备注 |
|---|---|---|---|
| `Qwen3-VL-4B-Instruct-4bit` | 图片/关键帧描述 | [Qwen3-VL-4B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct) | 当前本地目录是 4-bit 变体；在确认具体量化发布者与许可证前，不应再分发。 |
| `Qwen3-Embedding-0.6B` | 文本向量 | [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 上游模型卡提供模型与引用信息。 |
| `yoloe26-l-seg` | 开放词汇检测/分割 | [Ultralytics YOLOE 文档](https://github.com/ultralytics/ultralytics/blob/main/docs/en/models/yoloe.md) | 权重与 Ultralytics 软件许可需要分别核对。 |
| `openclip-vit-b-32-laion2b-s34b-b79k` | 视觉向量 | [mlfoundations/open_clip](https://github.com/mlfoundations/open_clip) | 具体预训练权重还受其模型卡和训练数据说明约束。 |
| `ocr/PP-OCRv6_medium_*` | OCR 检测与识别 | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 使用前核对 PP-OCRv6 对应模型说明。 |
| `whisper-large-v3-turbo` | 语音转写 | [openai/whisper](https://github.com/openai/whisper) | 当前 1.1.4 搜索主线未默认运行 Whisper。 |
| `insightface/buffalo_l` | 可选人物归并 | [deepinsight/insightface](https://github.com/deepinsight/insightface) | 上游明确提示 `buffalo_l` 需要单独核对模型许可；因此只保留可选接口，不分发权重。 |

## 来源真实性

- “本地目录约定”描述开发机上的目录命名，不证明文件来自某个特定镜像或量化者。
- 如果无法从模型文件、下载记录或校验值确认具体来源，应标记为未知，不要推测。
- 公开 Release 不得附带这些权重。
