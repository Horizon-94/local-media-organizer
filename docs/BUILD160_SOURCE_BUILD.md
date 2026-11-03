# BUILD160 源码构建与验证

当前维护版本是 1.3.2 build160。入口为 `scripts/build160/check.py` 和 `scripts/build160/build.py`，Xcode 工程为 `macos/Build160.xcodeproj`。

## 受支持的验证路径

```bash
python3 scripts/build160/check.py
python3 scripts/build160/build.py --check
```

测试仅使用合成数据、内存 SQLite 和临时文件；不需要用户素材或模型。中文脚本规范化使用 macOS CoreFoundation，中文测试及原生构建需要 macOS。当前原生目标为 arm64、macOS 12+、Swift 5，优化设置与验收前端一致（`-Onone`）。Xcode 的 Build 和 Analyze 都必须通过。

Python 源码及入口导入检查与 95 项回归属于源码验证。前端构建产物不能脱离 App 资源和 helper 单独启动。完整 App 包的 Python 环境组装、模型供给、签名、公证及 DMG 发布不在此次重新验收范围内，不以此源码构建冒充完整安装包验收。

## 运行源码布局

- `apps/media_archive_image_video_ui/`：正式 Swift 前端、桥接、当前发布索引及搜索运行代码。
- `production_engine/`：正式处理引擎及质量、持久化、恢复合同。
- `macos/runtime/`：实际 App 的 Python helper 源码与可移植运行配置模板。
- `scripts/`、`configs/`、`migrations/`：运行合同所需的依赖文件；历史脚本仍保留历史身份。
- `docs/build160-source-manifest.json`：安装实现与公开文件的逐项 SHA-256 对应。

三个运行默认路径改为基于用户目录或环境变量：`MEDIA_ARCHIVE_TEST_ROOT`、`MEDIA_ARCHIVE_SOURCE_ROOT`、`MEDIA_ARCHIVE_WHISPER_DIAGNOSTICS_ROOT`。Qwen 历史独立验证脚本中的示例路径也改为相对目录和环境变量。算法与业务逻辑没有因公开同步改变。配置模板去掉开发机诊断目录，并使用独立源码构建状态命名空间；集成时也应显式设置 `MEDIA_ARCHIVE_APP_STATE_ROOT`，避免连到既有正式库。

模型环境依赖由 `pyproject.toml` 的对应 extras 描述，需分别配置，不要将所有框架混装为一个环境。模型和工具位置通过运行合同解析；配置模板中的 `$APP_CONTENTS`、`$APP_RESOURCES`、`$MODEL_ROOT` 由 App 资源路径及用户模型目录提供。

## 历史恢复保护

`approved_resume_migration.json`、工作区迁移批准和已安装包身份／签名回执属于机器与项目专属资料，不公开。新建处理任务及合同完全一致的恢复不需要这些历史迁移批准；发生合同变化的旧项目恢复必须失败关闭，不能创建宽泛白名单绕过校验。

此前安装版的本地验收与本次公开源码构建分别记录；没有复制 App、数据库或模型来制造公开测试成功。
