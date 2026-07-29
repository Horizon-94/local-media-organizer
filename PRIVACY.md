# Privacy

Local Media Organizer 的目标是让素材、索引和模型留在用户自己的 Mac 上。

## 默认数据边界

- 原始素材目录只读。
- 搜索使用只读 SQLite 连接。
- 模型从本地路径加载。
- 正式流水线关闭 Hugging Face、Ultralytics、PaddleX 等自动下载路径。
- 本项目不提供遥测、云端上传或远程推理功能。

## 不应提交到 GitHub 的内容

- 真实素材、缩略图、抽帧或音频；
- SQLite、JSONL 索引、向量文件和搜索历史；
- 模型权重、Tokenizer、缓存和虚拟环境；
- 日志、崩溃报告、用户名、绝对路径、设备标识和访问令牌；
- 签名证书、Provisioning Profile 或未经隐私审计的 DMG/ZIP。

经过 `scripts/release_artifact_audit.py` 检查的通用 DMG 可以作为
GitHub Release 附件发布，但不能提交到 Git 历史。DMG 不得包含模型、
真实数据库、素材、日志、令牌、本机用户名或本机绝对路径。

## 问题报告

提交 Issue 前请使用最小合成数据重现问题。路径请改写为 `/Users/yourname/...`，数据库请只提供 schema 或人工构造的小样本。
