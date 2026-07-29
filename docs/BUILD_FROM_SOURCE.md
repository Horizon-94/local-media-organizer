# 从源码构建 1.1.4

官方源码只来自：

<https://github.com/Horizon-94/local-media-organizer>

## 能复现到什么程度

- 不加载模型即可安装源码、运行核心测试并构建原生 macOS 应用。
- `--portable-runtimes` 会把五套 Python 环境装入 DMG，模型仍保持外置。
- 当前依赖采用版本范围而非完整哈希锁，因此可以构建功能等价版本，但不能保证与官方 DMG 逐字节一致。
- 官方 DMG 使用 ad-hoc 签名。没有 Apple Developer ID 和公证时，其他 Mac 可能显示 Gatekeeper 提示。

## 基础要求

- macOS 13 或更新版本，Apple Silicon；
- Xcode Command Line Tools；
- Python 3.12 Framework 发行版；
- Homebrew 的 `ffmpeg` 与 `ffprobe`；
- 约 15 GB 临时磁盘空间。

32 GB 是官方 1.1.4 的开发与验收基准，不是硬性要求。应用会按本机 CPU
核数和统一内存选择保守默认并发；16 GB 机器应从 1 路模型并发开始，
64 GB 或更高配置可在设置页显示的估算上限内逐步提高。

```bash
xcode-select --install
brew install ffmpeg
git clone https://github.com/Horizon-94/local-media-organizer.git
cd local-media-organizer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python scripts/public_release_audit.py .
python -m pytest -q
```

这些测试不应下载或加载模型。

## 创建五套可携带环境

下面的目录名是 1.1.4 构建合同的一部分。`/path/to/envs` 可以换成任意本地目录。

```bash
ENV_ROOT=/path/to/envs
python3 -m venv "$ENV_ROOT/media-archive-v06-visual"
python3 -m venv "$ENV_ROOT/media-archive-v06-yolo"
python3 -m venv "$ENV_ROOT/qwen-vl"
python3 -m venv "$ENV_ROOT/media-archive-v06-ocr"
python3 -m venv "$ENV_ROOT/media-archive-embedding"

"$ENV_ROOT/media-archive-v06-visual/bin/pip" install -e '.[visual]'
"$ENV_ROOT/media-archive-v06-yolo/bin/pip" install -e '.[yolo]'
"$ENV_ROOT/qwen-vl/bin/pip" install -e '.[qwen]'
"$ENV_ROOT/media-archive-v06-ocr/bin/pip" install -e '.[ocr]'
"$ENV_ROOT/media-archive-embedding/bin/pip" install -e '.[embedding]'
```

## 构建

开发用应用：

```bash
python scripts/04_media_archive_app/build_native_image_video_app_v1.py
```

不含模型、数据库和素材的通用 DMG：

```bash
MEDIA_ARCHIVE_ENV_ROOT=/path/to/envs \
python scripts/04_media_archive_app/build_native_image_video_app_v1.py \
  --python /path/to/envs/media-archive-v06-visual/bin/python \
  --portable-runtimes \
  --dmg
```

构建器拒绝覆盖现有 `.app` 或 DMG。请使用新的空输出目录，不要删除未知输出。

## 模型

模型不参与构建。完成安装后按 [MODEL_SETUP.md](MODEL_SETUP.md) 放置模型；应用只读取用户选择的模型根目录，不会自动下载。

## 发布前检查

```bash
python scripts/release_artifact_audit.py dist/本地数据库.app
codesign --verify --deep --strict --verbose=2 dist/本地数据库.app
shasum -a 256 dist/本地数据库-1.1.4.dmg
```

发布附件应同时提供 SHA-256、当前提交号、GPL-3.0 许可证和“未公证”提示。
