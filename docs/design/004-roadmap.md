# 004 下一阶段路线图（交接）

面向接续本项目的人：读这一份就知道仓库停在哪、下一步按什么顺序做、哪些东西不要碰。
日期 2026-11-17，对应版本 v0.2.1。本文件只描述路线，不改代码也不改数据；规格细节以同目录下 002 / 003 原文为准。

## 1. 当前状态（2026-11-17）

```
版本 v0.2.1（src/mla/__init__.py:7）
测试 676 passed（no_llm 319 / mock_llm 183，并集 502，174 条未打标）
规则 16 / 触发器 33 / 法源 143 / 要素 13 / clause_gold 95
门禁 pytest + ruff check src tests scripts + pyright src + check_ui_text + 5 个数据校验脚本
```

数字口径：`pytest` 全量跑通 676 条；分套数用 `pytest --collect-only -m no_llm` / `-m mock_llm` 得到。规则、触发器、要素来自 `src/mla/data/rules/rules.yaml` 与 `clauses/*.yaml` 的条目数，法源与 gold 来自两个 `.jsonl` 的非空行数。数据校验脚本清单见 `.github/workflows/ci.yml` 的 `Validate data` 步骤（statutes / rules / triggers / missing_terms / clause_gold；`scripts/validate_elements.py` 存在但未接入 CI）。

真实基线（4 份合同 + `clause_gold` 95 条）：规则通道覆盖 0/38，模型通道覆盖 38/38（Wilson 95% 区间 0.9082–1.0000）。完整口径、分母与"为什么不能读作满分"见 `docs/evaluation.md`。

## 2. 已完成（0.1.1 → 0.2.1）

- 架构反转：模型成为主判定通道，规则与脚本降为约束器与数值校验器（003 规格落地）。
- 四层约束：L1 输出契约（法条原文摘抄 + 等级 + 证据）/ L2 合同要素骨架 / L3 形态与内容校验 / L4 返工回路。
- 无模型时核对被拒绝：界面、CLI（退出码 2）、服务层（`NoModelError`）三处一致，登录 / 设置 / 历史 / 导出仍可用。
- 规则瘦身 20 → 16：移出行为义务类规则（书面合同时限、竞业补偿欠付、社保登记、罚款上限）。
- 评测规范化：所有比率附 Wilson 区间与分母；规则下线导致的 gold 剔除单独计数。
- 对外分层：只讲「确定冲突」与「待确认」，内部 A/B/C 与"需补事实"等机制词不再出现。

## 3. 已知问题（按优先级）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | 凭据与数据路径绑当前工作目录：`config.py:27-29` 的 `_cwd_path`、`:62` 的 `index_dir`、`:106` / `:194` 的 `db` 全部取 `Path.cwd()` | 换目录启动（桌面快捷方式、从别的盘运行、CI 里的 wheel 冒烟）就在新目录建空库：账号、历史、已存 Key 一起"消失"，用户会当成数据丢失 |
| 2 | 结果页仍展示过程信息：耗时（`ui/app.py:1269` 指标、`:1517` 摘要行）、覆盖率注记（`:1514`）、判定来源徽标（`:83-92`、`:1020`、`:1334` / `:1342`） | 读者在"哪几条有冲突"和"程序怎么算出来的"之间来回跳，结论被稀释；非技术用户容易把徽标当成风险等级 |
| 3 | `pyright` 对 `tests/` 有 14 个既有错误（门禁只跑 `src`，见 `CONTRIBUTING.md:47` 与 `ci.yml` 注释） | 改测试时没有类型保护，改动是否破坏用例只能靠跑测试发现；`pyproject.toml:70` 的 `include` 含 `tests`，本地全量检查会红，容易养成忽略红的习惯 |
| 4 | 测试标记只覆盖 502/676，174 条未打标，集中在 `test_ui_smoke.py`(38)、`test_ui_setup.py`(21)、`test_contract_name.py`(15) 及 CLI / 检索类 | `-m no_llm` 与 `-m mock_llm` 都不代表全量；分套跑全绿会漏掉未打标用例的回归 |
| 5 | ~~`docs/architecture.md:22` 写"SQLite 5 张表（users / enterprises / contracts / tasks / logs）"~~ **已修（2026-11-17）**：改为 6 张表并补 `settings` 用途说明 | — |
| 6 | ~~`README.md:247` 的「版本」一节只指向 Releases 与 CHANGELOG，不写当前版本号~~ **已修（2026-11-17）**：开头补"当前版本 0.2.1"，保留 Releases / CHANGELOG 指引 | — |

## 4. 0.3.0 目标：凭据与数据路径重构

规格：`docs/design/002-credentials.md`（含 24 条验收标准 AC-01…AC-24），按 5 个阶段推进，每阶段独立可验收。

| 阶段 | 做什么 | 破坏性 |
| --- | --- | --- |
| 1 | 用户级 `auth.json` + 统一 resolver（`env > auth_file` 两级优先级），纯新增，不改现有调用方 | 否 |
| 2 | 数据库与索引目录迁到用户目录，旧库做一致性复制 | 否（行为变更） |
| 3 | 调用方收敛到 resolver，设置页改「测试并保存」+ 状态行 | 内部接口 |
| 4 | 首次凭据迁移、幂等标记，删除旧库中的 Key 行 | **是** |
| 5 | `mla auth list/set/remove` + UI 交互补项（更改 / 清除 Key、手动测试连接）+ 版本发布 | 否 |

Key 相关的三条硬约束（不在阶段间协商）：

- 校验只发 `GET /models`，不产生对话请求、不消耗 token；超时用 `MLA_AUTH_CHECK_TIMEOUT`，与推理超时互不影响。
- 保存前必校验：`GET /models` 通过才落盘，失败按 401 / 超时 / 断网三类分别提示，不通过不写 `auth.json`。
- 保存后前端不可查看：界面只回答"是否已配置"和"上次校验是否通过"，输入框保存后清空且不回填。

## 5. 0.3.x / 0.4.0：结果页信息架构与前端

- 结果页三层收敛：**结论**（默认可见，只讲确定冲突 / 待确认）→ **依据**（点开看条文原文，条号常驻可见）→ **过程**（耗时、覆盖率、判定来源默认隐藏）。
- 设置项补一个开关：是否显示"查询路径 / 判定来源"，默认关闭；把第 3 节第 2 条的矛盾交给用户选择。
- 以上在 Streamlit 内做完并稳定后，再评估 FastAPI + Vue 迁移：先定接口契约（请求 / 响应结构、错误码），UI 后换，避免一次改两层。

## 6. 1.0.0：打包

- exe 打包（PyInstaller 内嵌 uvicorn），排在前端迁移之后，避免打包目标随 UI 重写而返工。
- 动手前必须先解决三件事：模型不可用时的体验（现在直接拒绝核对，需给出可操作的下一步）、首次启动向导（账号 / Key / 示例合同）、杀软误报（签名或白名单说明）。

## 7. 交接要点

- 规格位置：`docs/design/002-credentials.md`（凭据与路径）、`docs/design/003-model-first.md`（模型主导架构）。改行为先改规格，再改代码。
- 数据真源：`src/mla/data/`（法源 / 规则 / 触发器 / 要素 / 评测集），随包分发、按只读数据处理；改规则等于改行为，必须跑对应校验脚本并写明法条依据。
- 不要改的：`CHANGELOG.md` 里 0.1.x / 0.2.0 的历史条目（含其中"no_llm 313"这类当时的数字）、`clause_gold.jsonl` 的既有标注（降级或剔除要单独计数并在 PR 说明里写原因与日期）。
- 复现评测：`python scripts/eval_clauses.py --contracts <四份合同> --runs 1 --no-generalization`。注意 `--contracts` 缺省只跑一份 `contract_traps_01.txt`，直接跑会低估工作量与覆盖面。
- 门禁顺序建议：`pytest` → `ruff check src tests scripts` → `pyright src` → `python scripts/check_ui_text.py` → 改了哪类数据就跑对应 validate 脚本；CI 另要求覆盖率 ≥ 75%。
