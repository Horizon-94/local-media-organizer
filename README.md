# Local Media Organizer

一个面向 macOS 的本地素材整理与搜索工具。它可以扫描图片和视频、生成预览与抽帧、建立本地 SQLite 索引，并通过 OCR、视觉标签、图文描述和向量检索提供离线搜索。

当前公开版本：`1.1.4-search-progress-warm-cache`

## 1.1.4 的重点

- 搜索启动后立即显示查询阶段、已检查项目数和候选结果。
- 搜索结果分批返回，避免长时间只显示“正在搜索”。
- 对重复查询与常用只读数据建立进程内暖缓存。
- SQLite 搜索使用只读连接，不修改素材库。
- 原始素材和模型目录按只读原则使用。

## 仓库范围

本仓库是“仅源码”公开仓库，包含：

- Python 后端、Swift 原生前台源码和构建脚本；
- 数据库迁移与运行配置；
- 单元测试、接口冻结记录和开发阶段文档；
- 模型来源与本地部署说明。

本仓库不包含：

- 原始图片、视频或音频；
- 真实 SQLite 数据库、索引或搜索记录；
- 模型权重、Tokenizer、缓存或运行环境；
- `.app`、DMG、ZIP 或其他构建产物；
- 本地日志、测试输出、绝对用户路径或访问令牌。

## 设计原则

1. 原始素材只读，不移动、不重命名、不覆盖。
2. 数据库集中保存派生信息和来源关系。
3. 模型必须从用户明确配置的本地路径加载。
4. 默认关闭模型和数据的自动下载。
5. 搜索入口只读，流水线写入必须显式确认输出目录。

## 开发环境

最低要求：

- macOS 13 或更新版本；
- Python 3.10 或更新版本；
- Xcode Command Line Tools；
- `ffmpeg`、`ffprobe` 和系统 `sips`。

安装基础开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

运行不加载模型的核心测试：

```bash
python -m pytest -q \
  tests/test_media_archive_search_readiness_v1.py \
  tests/test_media_archive_repository_schema_compat_v1.py \
  tests/test_media_archive_native_bridge_state_v1.py
```

执行源码公开审计：

```bash
python scripts/public_release_audit.py .
```

## 本地模型配置

模型不会随本仓库分发。请先阅读 [MODEL_SOURCES.md](MODEL_SOURCES.md)，确认每个模型自己的许可证，再设置：

```bash
export MEDIA_ARCHIVE_MODEL_ROOT="$HOME/Library/Application Support/素材大整理/Models"
export MEDIA_ARCHIVE_ENV_ROOT="/path/to/python-environments"
```

`configs/models.local.example.json` 只是一份示例。不要提交真实机器路径或私有模型清单。

## 构建 macOS 应用

构建脚本不会下载模型：

```bash
python scripts/04_media_archive_app/build_native_image_video_app_v1.py \
  --help
```

构建前需要分别准备视觉、YOLO、Qwen-VL、OCR 和文本向量运行环境，并通过 `MEDIA_ARCHIVE_ENV_ROOT` 指向它们。公开仓库不提供已签名应用或 DMG。

## 隐私

详见 [PRIVACY.md](PRIVACY.md)。问题报告中请勿上传真实数据库、素材路径、模型路径或日志全文。

## 开源许可

源码采用 [Apache License 2.0](LICENSE)。Apache-2.0 允许商业使用和再分发，同时要求保留许可证与版权声明。项目另有一份不具法律约束力的 [社区倡议](COMMUNITY_GUIDELINES.md)，鼓励衍生版本继续开放并避免误导性收费。

## 维护

- 版本变化见 [CHANGELOG.md](CHANGELOG.md)。
- 安全问题见 [SECURITY.md](SECURITY.md)。
- 贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。
