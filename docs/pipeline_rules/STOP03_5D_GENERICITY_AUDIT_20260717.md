# Stop03-5D 通用性审计

状态：`PASS / GENERIC_CONTRACT_FROZEN`

## 审计结论

Stop03-5D 冻结入口已经与当前测试项目的数据量、run ID、用户名和素材文件夹解绑。

## 证据

- 正式脚本不包含当前用户名绝对项目路径；
- 项目根从脚本位置自动推导；
- `--db`、`--out`、模型配置和 worker 数均可替换；
- 5B/5C 输入视图动态选择指定数据库中最新成功 run；
- 文档数 `D`、唯一文本数 `U`、复用数 `R` 从数据库计算；
- 完成条件为 `success=U`、`links=D`，不比较固定数量；
- 测试向数据库加入不同数量的新记录后，同一入口自动生成新的 D、U 和 run；
- 通用合同与动态节点文档不包含当前项目 run ID 或当前数量；
- 当前项目的具体结果只保存在 `NON_NORMATIVE` 验收报告；
- 23项 Stop03-5D targeted tests 全部通过；
- 中心数据库未因本次通用化审计而改写。

## 通用冻结文件

```text
docs/pipeline_rules/STOP03_5D_GENERIC_TEXT_EMBEDDING_DB_CONTRACT_DESIGN_V1.md
docs/pipeline_rules/STOP03_5D_TEXT_EMBEDDING_DYNAMIC_DB_NODE_V1.md
scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_contract_v1.py
scripts/03_stop03_visual_analysis/stop03_5d_text_embedding_db_orchestrator_v1.py
scripts/stop03_monitor/stop03_5d_text_embedding_db_monitor.py
configs/stop03_5d_text_embedding_db_contract_v1.json
configs/stop03_5d_text_embedding_db_orchestrator_v1.json
migrations/20260717_stop03_5d_text_embedding_db_contract_v1.sql
```

## 非规范项目验收

```text
docs/pipeline_rules/STOP03_5D_PROJECT_ACCEPTANCE_20260717.md
```

该文件可以记录当前项目数字，但不能被正式入口读取，也不能作为其他素材库的 PASS 门槛。
