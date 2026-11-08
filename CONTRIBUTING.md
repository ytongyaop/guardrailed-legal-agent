# 参与开发

先说清楚本项目的形状：它是**数据驱动**的——判定能力由 `src/mla/data/` 下的规则表、触发器、法源、评测集决定，代码主要是把它们串起来。所以「改规则」在这个项目里等同于改行为，比改代码更需要证据。

## 开发环境

需要 Python 3.11+（CI 在 3.11 与 3.12 上各跑一遍）。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -e ".[ui,files,dev]"
```

## 跑测试

```bash
pytest
```

测试**强制离线**：`tests/conftest.py` 把抽取固定为纯正则档（`MLA_EXTRACT_MODE=regex`）、屏蔽 secrets 里的真实 Key，并删掉 `MLA_LLM_API_KEY` / `DASHSCOPE_API_KEY`。所以跑测试**不需要任何 API Key，也不会联网、不会花钱**。写了依赖网络或真实模型的测试，请改成注入假实现。

单独的 CLI 冒烟（不污染工作目录的数据库）：

```bash
python -m mla init --admin-username admin --enterprise "示例企业"
python -m mla check --contract src/mla/data/evalsets/sample_contract.txt
```

## 质量门禁

提交前逐条跑过，和 CI 一致：

```bash
ruff check src tests scripts
python -m pyright src                                  # 只检查 src
python scripts/check_ui_text.py                        # 界面文案规则（不判「违法/无效」等口径）
python scripts/validate_statutes.py src/mla/data/statutes/statutes.jsonl
python scripts/validate_rules.py
python scripts/validate_triggers.py
python scripts/validate_missing_terms.py
python scripts/validate_clause_gold.py
python -m pytest --cov=mla --cov-report=term-missing   # CI 要求覆盖率不低于 75%
```

`tests/` 里存在既有的类型报错，因此 `pyright` **只对 `src` 生效**；不要在 PR 里顺手改测试的类型标注。

## 改数据的两条硬规矩

### 1. 改 `src/mla/data/rules/` 或 `clauses/` 必须跑对应校验脚本，并写明法条依据

数据不是配置文件，是判定依据。任何新增或修改的规则、触发器、缺项规格，必须满足：

- **跑对应校验脚本，且通过**：
  - `rules/rules.yaml` → `python scripts/validate_rules.py`
  - `clauses/triggers.yaml` → `python scripts/validate_triggers.py`
  - `clauses/missing_terms.yaml` → `python scripts/validate_missing_terms.py`
- **说明该规则的法条依据**：在 PR 描述里给出引用的法条条号，并确认该条号存在于 `statutes/statutes.jsonl`（校验脚本会查库，但人也要说清楚为什么这条规则对应这条法条）。
- 引用的条号必须能在法源库里定位到；库里没有的条号不能凭印象写进去。

### 2. 评测样本的归档纪律

新增样本按 [`src/mla/data/evalsets/README.md`](src/mla/data/evalsets/README.md) 的规矩归档，核心是分清**回归集**与**留出集**：

- 出题时已经看过判定逻辑、并且为了让它通过调整过规则或提示词的 → 进**回归集**（`eval_cases.jsonl` / `clause_gold.jsonl`），只用来保证「没退化」。
- 按事前口径一次写好、之后不再回改的 → 才能进**留出集**（`eval_generalization.jsonl` / `qa_cases.jsonl`），用来报告能力。

**为了让某条样本通过而改规则，就必须把该样本降级为回归集**，并在文件名或清单里标注降级原因与日期。不要既改规则又留着样本冒充留出集——那样指标是自己给自己发的奖状。

跑完两个集合，数字分开记录：

```bash
python scripts/run_eval.py --extract regex
python scripts/run_eval.py --cases src/mla/data/evalsets/eval_generalization.jsonl
python scripts/eval_qa.py
python scripts/eval_clauses.py --runs 1
```

## Pull Request 要求

1. **跑过门禁**：上面「质量门禁」那一组命令全部通过，并在 PR 里贴结果（覆盖率数字请给 `TOTAL` 那行）。
2. **说清改了什么、为什么**：行为变化要写清楚对哪一层有影响（数值域 / 存在性域 / 缺项域），以及有没有动评测样本、动的是回归集还是留出集。
3. **不要提交 Key 或真实合同**：`.streamlit/secrets.toml`、`.env`、任何真实合同文本都不要进 PR。需要演示时用 `src/mla/data/evalsets/` 里的示例文件。
4. 保持范围单一：一个 PR 只做一件事；顺手重构、顺手改风格请单独提。

## 报告安全问题

不要开公开 Issue，见 [SECURITY.md](SECURITY.md)。
