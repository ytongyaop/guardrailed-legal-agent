# 设计文档

## 这个目录是什么

设计决策记录，不是用户文档：写“为什么这样改”，面向想理解架构取舍的开发者与维护者。三份文档对应两次架构变更与一份路线图；规格本身不是功能说明，写成待实现的章节不等于已经上线。动对应代码之前先读规格。

## 三份文档的定位

- `002-credentials.md`：凭据与运行期数据路径重构。模型 Key 从业务数据库移到用户目录下的 `auth.json`，界面、命令行、服务层与评测脚本收敛到同一个 resolver，旧库里的 Key 由一次性迁移接管。
- `003-model-first.md`：模型主导架构。判定权从规则移交给模型，规则降为数值校验器，靠四层约束（L1 规则级 / L2 辅助层 / L3 校验层 / L4 返工）控制错误半径，并配套分级重构（影响度 + 确定性两层）与规则瘦身。
- `004-roadmap.md`：当前状态、已知问题与后续路线（0.3.0 凭据重构 → 前端信息架构 → 打包）

## 项目约定

- 质量门禁（提交前全部通过）：`pytest`、`ruff check src tests scripts`、`pyright src`、`python scripts/check_ui_text.py`。
- 数据校验：`validate_statutes`、`validate_rules`、`validate_triggers`、`validate_missing_terms`、`validate_clause_gold`、`validate_elements`（检查合同要素骨架 13 项）六个脚本，改了哪类数据就跑对应那一个。
- 评测：`scripts/run_eval.py` 跑回归集与留出集，`scripts/eval_clauses.py` 跑条款级评测；回归集只要求不退化，留出集不参与调优。
- 测试默认离线：`tests/conftest.py` 强制 `MLA_EXTRACT_MODE=regex` 并屏蔽真实 Key，跑测试既不需要网络，也不需要额度。
- 文案规范：简体中文，用中文引号（不用直角引号），不用 emoji 装饰；由 `scripts/check_ui_text.py` 检查。
- 免责口径：只说“与法条存在冲突”并给出依据定位，不判“违法”或“无效”。
- 数据真源：法源、规则、触发器、评测集都在 `src/mla/data/`，随包分发，按只读数据处理。

## 修改原则

- 先改规格再改代码：行为变化先落到规格与验收标准，再动实现。
- 每份规格里的“验收标准”章节是收尾依据，逐条通过才算完成。

相关文档：[README](../../README.md) · [架构说明](../architecture.md) · [贡献指南](../../CONTRIBUTING.md)
