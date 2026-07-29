# Stop03-5D 通用文本向量中心数据库合同 V1

状态：`GENERIC_RULE_FROZEN / PROJECT_ACCEPTANCE_SEPARATE`

## 1. 这一步做什么

把中心数据库中已经通过质量门的 Qwen-VL、OCR 和语义传播结果，按派生画面合并成
可搜索文本，再把不同文本转换为向量。

本合同冻结的是处理方法和数据库关系，不冻结某个文件夹的记录数量、run ID 或路径。

## 2. 通用输入

每次运行只读取命令行 `--db` 指定数据库中的正式最新视图：

```text
v_stop03_5_latest_unified_evidence
v_stop03_5c_latest_propagation
derived_assets
visual_units
source_assets
```

规则：

- 自动选择该数据库中最新成功的 5B 和 5C run；
- 不接受命令中写死历史 run ID；
- 5B 只接收 `quality_status = PASS`；
- REVIEW 默认不进入向量；
- OCR 文字可以直接进入本帧文本，但不参与相邻帧传播；
- 不读取原始视频或原始图片；
- 不依赖当前测试素材文件夹或当前记录数量。

## 3. 一帧一份文档

对每个 `derived_id`：

```text
Qwen-VL 画面描述
+ 本帧通过质量门的 OCR 文字
+ 传播到本帧的去重对象标签
= 一份 embedding_text
```

每个 `derived_id` 最多一份 `text_document`。没有任何有效文字的派生画面不创建空文档。

## 4. 相同文字只计算一次

设：

```text
D = 本次帧级文档数量
U = embedding_text_sha256 去重后的唯一文本数量
R = 可复用数量
```

必须满足：

```text
U <= D
R = D - U
```

模型只处理 `U` 条唯一文本。多个文档文字完全相同时，通过 link 指向同一个向量，
不重复计算、不重复保存 BLOB。

`D`、`U`、`R` 都从当前数据库动态计算，不设置固定值。

## 5. 模型合同

模型路径和 Python 环境来自配置文件，可按部署机器更换；它们是运行来源记录，不是素材
文件夹规则。

冻结参数：

```text
输出维度 = 1024
向量类型 = float32
归一化 = true
文档 prompt = 空
相似度 = cosine
```

模型必须从本地目录加载，并启用 `local_files_only`。禁止联网下载或修改模型目录。

## 6. 通用中心数据库结构

### `stop03_5d_text_embedding_runs`

保存本次来源 run、模型指纹、规则指纹、动态数量、worker 参数和状态。

### `stop03_5d_text_documents`

每个可搜索派生画面一行，保存来源、合并前文字、合并后文字和来源证据 ID。

### `stop03_5d_text_vectors`

每个唯一文本一行，保存 execution key、模型指纹、维度、BLOB、BLOB SHA256 和状态。

### `stop03_5d_document_vector_links`

连接文档与唯一文本向量，允许多个文档复用同一个向量。

### `v_stop03_5d_latest_text_documents`

只暴露当前数据库中最新成功 Stop03-5D run 的成功文档与向量关系。

## 7. 数据库存储

文本向量以 BLOB 存在中心 SQLite。每个 run 的实际体积由 `U × dimension × dtype` 动态
决定，不用某个测试项目的数量推导生产门槛。

现有 OpenCLIP 视觉向量仍按原合同保存，本阶段不迁移。

## 8. 通用完成标准

设数据库本次 run 声明：

```text
D = document_count
U = unique_text_count
```

只有同时满足以下条件才能 PASS：

```text
documents = D
document links = D
unique vectors = U
success vectors = U
pending = 0
running = 0
failed = 0
每个成功 BLOB 字节数 = dimension × dtype 字节数
每个 BLOB SHA256 正确
所有向量数值有限且已归一化
execution_key duplicates = 0
SQLite integrity_check = ok
foreign key errors = 0
```

不得用某个固定总数代替上述关系。

## 9. 路径通用性

- 项目根目录从脚本自身位置推导，不包含用户名；
- 数据库由 `--db` 指定；
- 输出目录由 `--out` 指定；
- test-output 默认根可由 `MEDIA_ARCHIVE_TEST_OUTPUT_ROOT` 覆盖；
- 模型和 Python 路径由部署配置指定；
- `source_relative_path` 只用于来源追踪，不作为固定素材根；
- 完成判断不依赖输出文件夹名称。

## 10. 与搜索阶段的边界

Stop03-5D 只负责可靠地生成和保存文本向量，不建立最终搜索索引。

文本搜索合同必须在本通用化审计通过后单独设计。不得自动安装 FAISS、sqlite-vec 或
其他依赖。

## 11. 当前项目验收记录

当前测试素材库的数量、run ID、worker 分配和实际耗时不属于本合同，单独保存在：

```text
docs/pipeline_rules/STOP03_5D_PROJECT_ACCEPTANCE_20260717.md
```
