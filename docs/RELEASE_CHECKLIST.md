# 发行检查清单

1. 当前分支、提交号和版本号一致。
2. `python scripts/public_release_audit.py .` 通过。
3. CI 中的核心测试全部通过。
4. DMG 使用空白首启配置；数据库与输出目录字段为空。
5. DMG 不含模型、素材、数据库、日志、令牌或真实用户路径。
6. `release_artifact_audit.py`、`codesign --verify` 通过。
7. 在干净用户账户中检查安装、首次启动、模型目录选择和只读搜索入口。
8. Release 页面附 SHA-256、提交号、模型说明、GPL-3.0 和未公证提示。
9. DMG 只作为 GitHub Release 附件，不提交到 Git 历史。
10. 只有 Horizon-94 仓库发布页中的附件可标为“官方构建”。
