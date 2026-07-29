# Contributing

欢迎提交 Issue 和 Pull Request。

## 基本要求

1. 不提交模型、数据库、真实素材、日志或构建产物。
2. 测试必须使用合成数据和临时目录。
3. 保持原始素材只读与搜索只读接口。
4. 不改变 A9T-v3、R2J-FIX-C4 等已经冻结的名称。
5. 新增模型能力时同时更新 `MODEL_SOURCES.md` 和许可证说明。

提交前运行：

```bash
python scripts/public_release_audit.py .
python -m compileall -q apps scripts
python -m pytest -q \
  tests/test_media_archive_search_readiness_v1.py \
  tests/test_media_archive_repository_schema_compat_v1.py \
  tests/test_media_archive_native_bridge_state_v1.py
```
