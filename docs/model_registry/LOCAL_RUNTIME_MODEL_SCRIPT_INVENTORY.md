# LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY

更新时间：2026-07-11
用途：本地素材大整理项目的模型、Python 环境、脚本、配置固定清单。
原则：本地优先；不联网；不下载；不自动补依赖；不修改原始素材。

---

## 0. 使用规则

以后凡是涉及以下内容，必须先读本文件：

- Qwen-VL
- YOLOE / YOLOv8
- OpenCLIP
- OCR
- Whisper
- VAD
- Embedding
- Python 运行环境
- 本地模型路径
- 提示词注册表
- 数据库化 / 检索链路

同时必须优先读取：

```text
$APP_RESOURCES/Pipeline/docs/model_registry/LOCAL_MODEL_REGISTRY.md
$APP_RESOURCES/Pipeline/docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md
```

禁止：

```text
把远程模型名当成本地模型路径
自动从 HuggingFace 下载
自动从 ModelScope 下载
自动从 GitHub 下载
触发 Ultralytics AutoUpdate
触发 pip 自动安装
触发 transformers 自动下载
把整个不兼容 site-packages 塞进 PYTHONPATH
修改原始素材
删除原始素材
移动原始素材
```

发现缺模型、缺依赖、缺配置时，必须停止并报告：

```text
缺少什么
当前脚本在哪里调用
本地已有候选路径在哪里
是否需要用户确认处理
```

---

## 1. 固定目录

| 类型 | 路径 | 说明 |
|---|---|---|
| 项目根目录 | `$APP_RESOURCES/Pipeline` | 当前正式项目 |
| 模型根目录 | `$MODEL_ROOT` | 正式模型统一根目录 |
| 正式 Python 环境目录 | `$BUNDLED_PIPELINE_ENVS` | 当前主要运行环境 |
| 旧/备份 App 环境 | `$USER_HOME/Documents/AI-Standalone-Apps/envs` | 复查，不默认使用 |
| Qwen 旧 runtime | `$USER_HOME/Documents/AI-Local/runtimes/qwenvl/.venv` | 复查，不默认使用 |
| 测试输出根目录 | `$USER_HOME/Documents/001DZLtestbaogao` | 当前测试输出 |
| 提示词注册表 | `$USER_HOME/Documents/本地素材大整理配置/提示词注册表` | OCR_TRIGGER / A_CORE 等 |
| 建议本清单位置 | `$APP_RESOURCES/Pipeline/docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md` | 固定清单 |

---

## 2. 正式模型路径

| 用途 | 模型 | 正式路径 | 状态 |
|---|---|---|---|
| Qwen-VL 图像理解 | Qwen3-VL-4B-Instruct-4bit | `$MODEL_ROOT/Qwen3-VL-4B-Instruct-4bit/model.safetensors` | 正式 |
| Whisper 转写 | whisper-large-v3-turbo | `$MODEL_ROOT/whisper-large-v3-turbo/weights.safetensors` | 正式 |
| 文本向量 | Qwen3-Embedding-0.6B | `$MODEL_ROOT/Qwen3-Embedding-0.6B/model.safetensors` | 正式 |
| 视觉向量 | OpenCLIP ViT-B-32 LAION2B | `$MODEL_ROOT/openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors` | 正式 |
| YOLOE 检测 | yoloe26-l-seg | `$MODEL_ROOT/yoloe26-l-seg/weights/yoloe26-l-seg.pt` | 正式，但需修 CLIP shim |
| VAD | silero-vad | `$MODEL_ROOT/silero-vad/data/silero_vad.onnx` | 正式候选 |
| VAD | silero-vad op18 | `$MODEL_ROOT/silero-vad/data/silero_vad_op18_ifless.onnx` | 正式候选 |
| VAD | silero-vad 16k | `$MODEL_ROOT/silero-vad/data/silero_vad_16k.safetensors` | 正式候选 |
| VAD | silero-vad 16k op15 | `$MODEL_ROOT/silero-vad/data/silero_vad_16k_op15.onnx` | 正式候选 |
| VAD | silero-vad half | `$MODEL_ROOT/silero-vad/data/silero_vad_half.onnx` | 正式候选 |
| YOLOv8 备用 | yolov8n | `$USER_HOME/Documents/AI-Local/models/yolo/yolov8n.pt` | 备用，不默认替代 YOLOE |

备份模型副本存在于：

```text
$USER_HOME/Documents/AI-Local/models
$USER_HOME/Documents/AI-Standalone-Apps/models
```

这些副本暂不删除，先标记为 `backup_model_copy_review`。

---

## 3. 正式 Python 环境

| 用途 | 环境名 | Python 路径 | 状态 |
|---|---|---|---|
| Qwen-VL | qwen-vl | `$BUNDLED_PIPELINE_ENVS/qwen-vl/bin/python` | 正式 |
| Whisper | whisper | `$BUNDLED_PIPELINE_ENVS/whisper/bin/python` | 正式 |
| YOLOE / YOLO | media-archive-v06-yolo | `$BUNDLED_PIPELINE_ENVS/media-archive-v06-yolo/bin/python` | 正式，但默认缺 `clip` |
| OpenCLIP 视觉向量 | media-archive-v06-visual | `$BUNDLED_PIPELINE_ENVS/media-archive-v06-visual/bin/python` | 正式 |
| OCR | media-archive-v06-ocr | `$BUNDLED_PIPELINE_ENVS/media-archive-v06-ocr/bin/python` | 正式 |
| VAD | media-archive-v06-vad | `$BUNDLED_PIPELINE_ENVS/media-archive-v06-vad/bin/python` | 正式 |
| Embedding | media-archive-embedding | `$BUNDLED_PIPELINE_ENVS/media-archive-embedding/bin/python` | 正式 |
| ModelScope | modelscope | `$BUNDLED_PIPELINE_ENVS/modelscope/bin/python` | 复查，不默认使用 |
| 旧 YOLO | media-archive-yolo | `$BUNDLED_PIPELINE_ENVS/media-archive-yolo/bin/python` | 复查，不默认使用 |
| 旧 Qwen runtime | runtimes/qwenvl/.venv | `$USER_HOME/Documents/AI-Local/runtimes/qwenvl/.venv/bin/python` | 复查，不默认使用 |
| Standalone Qwen | AI-Standalone-Apps/envs/qwen-vl | `$USER_HOME/Documents/AI-Standalone-Apps/envs/qwen-vl/bin/python` | 复查，不默认使用 |
| Standalone Whisper | AI-Standalone-Apps/envs/whisper | `$USER_HOME/Documents/AI-Standalone-Apps/envs/whisper/bin/python` | 复查，不默认使用 |
| YOLOE 模型侧下载环境 | yoloe26-l-seg/.venv_download | `$MODEL_ROOT/yoloe26-l-seg/.venv_download/bin/python` | 只作 CLIP 来源，不作正式运行环境 |

---

## 4. YOLOE 特殊说明

当前确认：

```text
$BUNDLED_PIPELINE_ENVS/media-archive-v06-yolo
```

有：

```text
ultralytics
torch
torchvision
```

但默认没有：

```text
clip
```

同时：

```text
$MODEL_ROOT/yoloe26-l-seg/.venv_download
```

有：

```text
clip
ultralytics
torch
torchvision
```

禁止做法：

```text
不能把 $MODEL_ROOT/yoloe26-l-seg/.venv_download/lib/python3.9/site-packages
整个加入 PYTHONPATH。
```

原因：它会覆盖正式 YOLO 环境里的 `ultralytics / torch / torchvision`。

正确做法：

```text
只把 clip 做 shim。
只让正式 YOLO 环境 import 到 clip。
不能覆盖正式 YOLO 环境的 torch / ultralytics。
```

建议 shim 路径：

```text
$USER_HOME/Documents/AI-Local/local_python_shims/yoloe_clip_only
```

对应 clip 来源：

```text
$MODEL_ROOT/yoloe26-l-seg/.venv_download/lib/python3.9/site-packages/clip
```

YOLOE 运行前必须打印并验收：

```text
clip_import_path
ultralytics_import_path
torch_import_path
torchvision_import_path
auto_update_disabled: true
will_download: false
```

---

## 5. 提示词 / 标签注册表

当前已确认存在：

```text
$USER_HOME/Documents/本地素材大整理配置/提示词注册表/当前提示词_OCR_TRIGGER_v1.0.json
$USER_HOME/Documents/本地素材大整理配置/提示词注册表/提示词_20260704_OCR_TRIGGER_v1.0.json
```

YOLOE 关键词 / OCR_TRIGGER 相关任务必须优先使用上述本地 JSON，不允许从聊天上下文猜测。

---

## 6. 当前核心项目脚本

### Stop01 / Stop02

| 脚本 | 路径 | 状态 |
|---|---|---|
| 源素材扫描 | `scripts/02_step01_step02_pipeline/step01_source_scan_lineage_dedup.py` | 保留 |
| 视频抽帧 | `scripts/02_step01_step02_pipeline/step02_video_frame_c4s_id_from_step01_queue.py` | 保留 |
| 图片预览 | `scripts/02_step01_step02_pipeline/step02_2_image_preview_from_step01_queue.py` | 保留 |
| 高价值分布审计 | `scripts/02_step01_step02_pipeline/step02_3_high_value_distribution_audit.py` | 保留 |
| 低成本视觉探测 | `scripts/02_step01_step02_pipeline/step02_3_lowcost_visual_descriptor_probe.py` | 复查 |
| 视觉路由选择 | `scripts/02_step01_step02_pipeline/step02_3_visual_unit_route_selector.py` | 复查 |
| 视觉路由策略 | `scripts/02_step01_step02_pipeline/step02_3_visual_unit_route_policies.py` | 复查 |

### Stop03 视觉链路

| 脚本 | 路径 | 状态 |
|---|---|---|
| OpenCLIP + YOLOE 编排 | `scripts/03_stop03_visual_analysis/stop03_1_visual_then_yoloe4_from_stop02.py` | 复查，YOLOE 验收有漏洞 |
| YOLOE only probe | `scripts/03_stop03_visual_analysis/stop03_1a_yoloe_only_probe.py` | 复查 |
| OpenCLIP probe | `scripts/03_stop03_visual_analysis/stop03_1b_openclip_visual_embedding_probe.py` | 保留 |
| Qwen/OCR 环境探测 | `scripts/03_stop03_visual_analysis/stop03_3a_qwenvl_ocr_env_probe.py` | 保留 |
| Qwen smoke | `scripts/03_stop03_visual_analysis/stop03_3b_qwenvl_smoke_runner.py` | 保留 |
| Qwen full orchestrator | `scripts/03_stop03_visual_analysis/stop03_3c_qwenvl_full_orchestrator.py` | 保留 |
| OCR full orchestrator | `scripts/03_stop03_visual_analysis/stop03_4a_ocr_full_orchestrator.py` | 保留 |
| OCR local smoke | `scripts/03_stop03_visual_analysis/stop03_4d_ocr_local_only_smoke.py` | 保留 |
| Qwen 文本清洗 | `scripts/03_stop03_visual_analysis/stop03_5a2_qwenvl_text_cleanup.py` | 保留 |
| Qwen clean rerun | `scripts/03_stop03_visual_analysis/stop03_5a3_qwenvl_clean_rerun_v2.py` | 保留 |
| Qwen retry failed | `scripts/03_stop03_visual_analysis/stop03_5a3_retry_failed_rows_v1.py` | 保留 |
| 质量审计 | `scripts/03_stop03_visual_analysis/stop03_5a_quality_audit.py` | 保留 |
| 统一 staging | `scripts/03_stop03_visual_analysis/stop03_5b_unified_evidence_staging.py` | 保留 |
| YOLO label staging patch | `scripts/03_stop03_visual_analysis/stop03_5b2_yolo_label_staging_patch.py` | 保留，但依赖 YOLOE 有效结果 |
| 5C semantic propagation v1 | `scripts/03_stop03_visual_analysis/stop03_5c_semantic_propagation_v1.py` | 失败版本，待归档 |
| 5C YOLO gate v2 | `scripts/03_stop03_visual_analysis/stop03_5c_semantic_propagation_v2_yolo_gate.py` | 被 YOLOE 标签阻塞，待修 |
| YOLOE rerun highvalue | `scripts/03_stop03_visual_analysis/stop03_yoloe_rerun_and_highvalue_v1.py` | 待修：必须禁止 AutoUpdate |

### 本地盘点脚本

| 脚本 | 路径 | 状态 |
|---|---|---|
| 原始大盘点 | `scripts/local_project_inventory_audit_v1.py` | 保留 |
| 精简 digest | `scripts/local_project_inventory_digest_v1.py` | 保留 |
| 旧模型运行时盘点 | `scripts/00_project_audit/local_model_runtime_inventory.py` | 复查 |

---

## 7. 当前模型链路状态

| 链路 | 状态 | 说明 |
|---|---|---|
| Stop02 visual_unit | PASS | 1628 个 visual_unit |
| OpenCLIP | PASS | 1628 成功 |
| Qwen-VL | PASS | 336 个候选全局成功，剩余 0；332 条正式全量成功、3 条 smoke 成功、1 条 512-token 定向补跑成功；旧 384-token truncated 记录保留用于审计 |
| OCR | PASS | 226 条 |
| 5B staging | PASS | 可入库，但 source/derived 后续需拆分 |
| YOLOE | FAIL_NEEDS_FIX | 原始 YOLOE manifest 全部 detection_count=0 |
| 5B2 YOLO label patch | FAIL | 因 YOLOE 标签全空 |
| 5C semantic propagation | BLOCKED | 因 YOLOE 标签不可用 |

---

## 8. 当前禁止删除清单

以下内容暂时不删除：

```text
$MODEL_ROOT
$BUNDLED_PIPELINE_ENVS
$USER_HOME/Documents/AI-Standalone-Apps/envs
$USER_HOME/Documents/AI-Local/runtimes
$USER_HOME/Documents/001DZLtestbaogao
```

原因：还没有经过最终删除候选清单确认。

可以后续进入“复查/候选删除”的包括：

```text
AI-Standalone-Apps/envs/qwen-vl
AI-Standalone-Apps/envs/whisper
AI-Local/runtimes/qwenvl/.venv
AI-Local/envs/media-archive-yolo
AI-Local/envs/modelscope
失败版本 5C v1 输出目录
失败版本 5C v2 输出目录
YOLOE 全 0 的旧输出目录
```

但必须先生成删除清单，不允许直接删。

---

## 9. 后续固定执行要求

以后任何涉及模型和环境的脚本，必须在运行前输出：

```text
python_executable
model_path
registry_path
env_path
offline_env_flags
will_download: false
will_modify_original_media: false
```

涉及 YOLOE 时，必须额外输出：

```text
clip_import_path
ultralytics_import_path
torch_import_path
torchvision_import_path
auto_update_disabled: true
```

YOLOE 验收不得再只看成功行数，必须检查：

```text
processed_rows == expected_rows
failed_rows == 0
detection_count_positive_rows > 0
detected_labels_nonempty_rows > 0
detections_json_nonempty_rows > 0
yolotest 样本必须有检测结果
```

---

## 10. 当前下一步

在继续 YOLOE 之前，先完成：

```text
1. 固定本清单
2. 后续所有模型任务先读本清单
3. 修 YOLOE clip shim
4. 重跑 YOLOE 1628 张
5. 重新做 YOLO 高价值帧审计
6. 再决定 5B2 → 5C
```
