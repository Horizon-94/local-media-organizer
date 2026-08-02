# Local Media Organizer

一个面向 macOS 的本地素材整理与搜索工具。它可以扫描图片和视频、生成预览与抽帧、建立本地 SQLite 索引，并通过 OCR、视觉标签、图文描述和向量检索提供离线搜索。

当前稳定源码版本：`1.1.4-search-progress-warm-cache`

当前本地验证候选：`1.1.5`（尚未发布；2.2 TB 从零完整验收待完成）

版权所有：**Copyright (C) 2026 [Horizon-94](https://github.com/Horizon-94)**
官方源码：<https://github.com/Horizon-94/local-media-organizer>

## 1.1.4 的重点

- 搜索启动后立即显示查询阶段、已检查项目数和候选结果。
- 搜索结果分批返回，避免长时间只显示“正在搜索”。
- 对重复查询与常用只读数据建立进程内暖缓存。
- SQLite 搜索使用只读连接，不修改素材库。
- 原始素材和模型目录按只读原则使用。

## 1.1.5 验证进展

1.1.5 以 1.1.4 的界面和现有模型链路为基础，集中修复真实运行中发现的
断点、进度、重复素材、人物管理和搜索等待问题。当前只作为本地验证候选，
不是 GitHub Release，也没有可供普通用户下载的 1.1.5 通用 DMG。

- 视频按单个来源验证、落账和提交，减少中断后整阶段重做的风险；
- 前台显示真实完成数、活动 worker、FFmpeg 进程、输出文件和存储增长；
- ETA 在样本不足时显示“正在估算”，不再固定显示明显错误的短倒计时；
- AppleDouble `._` 文件不再作为真实视频或重复素材候选；
- 重复素材按真实文件并排展示，保留人工判断，不自动删除；
- 人物组支持本地命名、标签、合并和撤销，同时明确当前仍是人脸聚类；
- 搜索增加只读暖缓存和向量缓存，降低重复扫描造成的等待；
- 应用版权与来源固定为 Horizon-94。

完整差异、代码位置、测试证据和未完成边界见
[1.1.4 → 1.1.5 变更记录](docs/history/1.1.4_TO_1.1.5.md)。

## 下载和使用

普通用户优先从 GitHub Releases 下载由 Horizon-94 发布的 macOS DMG。
DMG 内含应用和 Python 运行环境，但**不含任何第三方模型**。
官方 1.1.4 通用 DMG 面向 Apple Silicon（M 系列芯片）；Intel Mac 用户
目前应按下方说明从源码构建，不能把该 DMG 视为兼容版本。

1. 安装 DMG 中的 `本地数据库.app`。
2. 按 [模型安装说明](docs/MODEL_SETUP.md) 下载模型。
3. 默认把模型放到：

   ```text
   ~/Library/Application Support/素材大整理/Models
   ```

4. 也可以在应用的“处理设置 → 本地模型位置”中选择其他总目录。
5. 第一次运行时选择素材目录和索引输出目录；原始素材保持只读。

官方 Release 如未使用 Apple Developer ID 签名和公证，会被 macOS
Gatekeeper 提示。请只下载 Horizon-94 仓库中的发布附件，并核对发布页
提供的 SHA-256。当前维护者尚未持有 Developer ID 证书。

## 发布范围

本仓库是“仅源码”公开仓库，包含：

- Python 后端、Swift 原生前台源码和构建脚本；
- 数据库迁移与运行配置；
- 单元测试、接口冻结记录和开发阶段文档；
- 模型来源与本地部署说明。

本仓库不包含：

- 原始图片、视频或音频；
- 真实 SQLite 数据库、索引或搜索记录；
- 模型权重、Tokenizer、缓存或运行环境；
- Git 历史中的 `.app`、DMG、ZIP 或其他构建产物；
- 本地日志、测试输出、绝对用户路径或访问令牌。

经过隐私审计的 DMG 可以单独作为 GitHub Release 附件发布；它不会进入
Git 历史，也不会包含模型、真实数据库、素材或本机配置。

## 设计原则

1. 原始素材只读，不移动、不重命名、不覆盖。
2. 数据库集中保存派生信息和来源关系。
3. 模型必须从用户明确配置的本地路径加载。
4. 默认关闭模型和数据的自动下载。
5. 搜索入口只读，流水线写入必须显式确认输出目录。

## 硬件与并发

1.1.4 **不把所有电脑固定成开发机的 32 GB 配置**。首次打开和新建任务时，
应用会读取芯片名称、CPU 核数、GPU 核数（系统可提供时）和统一内存：

- CPU 核数和统一内存共同决定保守默认并发；
- 设置页同时显示“默认推荐”和“本机估算上限”；
- 模型并发可在 1–8 路之间调整，抽帧并发可在 1–16 路之间调整；
- 更高配置的 Mac 可以在估算上限内提高并发，低配机器应采用较低值；
- 应用不会静默提高并发；当前版本也不会在运行中自动降档，出现交换内存
  快速增长、界面卡顿或模型退出时，应停止任务并降低并发。

在 CPU 核数足够时，当前保守算法的大致模型并发为：16 GB 默认 1 路；
24 GB 默认 2 路、估算上限 3 路；32 GB 默认 3 路、估算上限 4 路；
64 GB 默认 4 路、估算上限 8 路。这是安全起点，不是性能保证；不同芯片、
模型版本、图片分辨率和同时运行的软件都会改变实际承载能力。

基于 M1 Max 32GB 大型真实任务，并结合 Apple 官方硬件规格整理的逐阶段说明见
[Apple Silicon 硬件、并发与耗时估算](docs/HARDWARE_CONCURRENCY_ESTIMATES.md)。
其中 M5 Max 128GB 与 M3 Ultra 256GB 的并发和耗时均明确标注为**估算**，
尚未经过本项目实机验证。

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

完整的空白 Mac 构建步骤见 [从源码构建](docs/BUILD_FROM_SOURCE.md)。

## 本地模型

模型不会随本仓库或 DMG 分发，也不会由应用自动下载。请先阅读
[模型安装说明](docs/MODEL_SETUP.md) 和 [模型来源与许可](MODEL_SOURCES.md)。

应用默认读取：

```bash
export MEDIA_ARCHIVE_MODEL_ROOT="$HOME/Library/Application Support/素材大整理/Models"
```

运行以下命令只会创建目录并打印上游链接，不会下载模型：

```bash
python scripts/prepare_model_directories.py
```

`configs/model_sources_v1.json` 固定所需相对路径、上游页面和许可提醒。
应用只验证本地文件是否就绪。

## 构建 macOS 应用

源码构建：

```bash
python scripts/04_media_archive_app/build_native_image_video_app_v1.py
```

构建包含五套 Python 运行环境、但不包含模型的通用 DMG：

```bash
MEDIA_ARCHIVE_ENV_ROOT="/path/to/python-environments" \
python scripts/04_media_archive_app/build_native_image_video_app_v1.py \
  --portable-runtimes --dmg
```

发布前必须运行：

```bash
python scripts/release_artifact_audit.py dist/本地数据库.app
```

构建脚本只做 ad-hoc 本地签名。公开分发的无警告安装体验仍需要 Apple
Developer ID Application 证书和 Apple 公证。

## 隐私

详见 [PRIVACY.md](PRIVACY.md)。问题报告中请勿上传真实数据库、素材路径、模型路径或日志全文。

## 开源许可

当前源码采用 [GNU GPL v3.0 only](LICENSE)。分发修改版本时必须遵守
GPL-3.0 的对应源码、许可证、版权和修改声明要求。GPL 允许收费，但不允许
把本程序的衍生版本作为闭源程序分发。

早期 Apache-2.0 发布的授权历史见 [LICENSE_HISTORY.md](LICENSE_HISTORY.md)；
旧授权不能撤回。项目身份和社区期望见
[COMMUNITY_GUIDELINES.md](COMMUNITY_GUIDELINES.md)。

## 维护

- 版本变化见 [CHANGELOG.md](CHANGELOG.md)。
- 历史版本路线见 [VERSION_HISTORY.md](docs/history/VERSION_HISTORY.md)。
- 已知问题与修复经验见 [KNOWN_ISSUES_AND_FIXES.md](docs/history/KNOWN_ISSUES_AND_FIXES.md)。
- 安全问题见 [SECURITY.md](SECURITY.md)。
- 贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。
