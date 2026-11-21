# 004 下一阶段路线图（交接）

面向接续本项目的人：读这一份就知道仓库停在哪、下一步按什么顺序做、哪些东西不要碰。
日期 2026-11-18，对应版本 v0.4.0。本文件只描述路线，不改代码也不改数据；规格细节以同目录下 002 / 003 / 005 / 006 原文为准。

## 1. 当前状态（2026-11-18）

```
版本 v0.4.0（src/mla/__init__.py:8）
架构 FastAPI（src/mla/api/）+ Vue 3（web/，Vite + Element Plus），Streamlit 已移除
Python 测试 926 passed / 1 skipped（collect-only 927 条）
       分套 no_llm 489 / mock_llm 229，两套零重叠，并集 718，209 条未打标
       覆盖率 92%（pytest --cov=mla --cov-report=term 的 TOTAL 行；CI 门槛 75%）
前端 87 单测（vitest，11 个文件）+ 6 E2E（Playwright，1 个 spec）
规则 16 / 触发器 35 / 法源 143 / 要素 13 / clause_gold 95
门禁 pytest + ruff check src tests scripts + pyright src + check_ui_text（含 web/src）
     + npm --prefix web run typecheck / test / build + Playwright + 6 个数据校验脚本
```

数字口径：`pytest` 全量 927 条收集，926 passed / 1 skipped；分套用 `pytest -m no_llm --collect-only` 与 `-m mock_llm --collect-only` 得到 489 / 229，两套无重叠，未打标 209 条（最大来源 `tests/test_credentials.py` 40 条）。注意 `pyproject.toml:74` 的 `addopts = "-q"`，命令行再加 `-q` 会变成 `-qq`、吞掉 "N tests collected" 那行。规则、触发器、要素来自 `src/mla/data/rules/rules.yaml` 与 `clauses/*.yaml` 的条目数（触发器 35 条共 126 个 pattern），法源与 gold 来自两个 `.jsonl` 的非空行数。数据校验脚本清单见 `.github/workflows/ci.yml` 的 `Validate data` 步骤（statutes / rules / triggers / missing_terms / clause_gold / elements，六个齐全）。

真实基线（4 份合同 + `clause_gold` 95 条）：规则通道覆盖 0/38，模型通道覆盖 38/38（Wilson 95% 区间 0.9082–1.0000）。完整口径、分母与"为什么不能读作满分"见 `docs/evaluation.md`。

## 2. 已完成（0.1.1 → 0.4.0）

- 架构反转：模型成为主判定通道，规则与脚本降为约束器与数值校验器（003 规格落地）。
- 四层约束：L1 输出契约（法条原文摘抄 + 等级 + 证据）/ L2 合同要素骨架 / L3 形态与内容校验 / L4 返工回路。
- 无模型时核对被拒绝：界面、CLI（退出码 2）、服务层（`NoModelError`）三处一致，登录 / 设置 / 历史 / 导出仍可用。
- 规则瘦身 20 → 16：移出行为义务类规则（书面合同时限、竞业补偿欠付、社保登记、罚款上限）。
- 评测规范化：所有比率附 Wilson 区间与分母；规则下线导致的 gold 剔除单独计数。
- 对外分层：只讲「确定冲突」与「待确认」，内部 A/B/C 与"需补事实"等机制词不再出现。
- 凭据迁到用户级 `auth.json`（Windows 默认 `%LOCALAPPDATA%\mla\`，`MLA_HOME` 可覆盖），来源两级：环境变量 `MLA_LLM_API_KEY` > 文件；`secrets.toml` 的 `[llm]` 不再被读取。
- 数据库与向量缓存迁到同一用户目录：未显式设 `MLA_DB` 时，旧库首次启动做一次性一致性复制，旧文件原样保留，索引缓存按可重建处理。
- 设置页写动作合并为「测试并保存」：先发 `GET /models` 校验（不消耗 token），不通过不落盘；状态行读 `auth-status.json`，重启后仍显示上次校验时间。
- 首次启动迁移旧库 Key 并删除旧行；六类失败分支（读旧库 / 写 `auth.json` / 清理旧行 / 写标记 / 文件损坏 / 抢锁）各有固定提示，均不假装成功。
- 新增 `mla auth list/set/remove`（`set` 用 `getpass` 隐藏输入，不接受命令行传 Key）；`.streamlit/secrets.toml.example` 删除 `[llm]` 段、只留 `[embedding]`（文件本身保留）。
- 0.4.0：前端迁到 FastAPI 同源 API + Vue 3 + Element Plus，单进程托管（`uvicorn mla.api.app:create_app --factory`）。
- 0.4.0：结果页三层（结论 / 依据 / 过程），过程开关 `ui.show_query_path` 默认关闭。
- 0.4.0：核对结果存档开关——`shared` / `owner_id` 两列，幂等迁移，旧记录补默认值不回填。
- 0.4.0：首次核对即落库（此前走完追问才落库，无追问的任务不入库）。
- 0.4.0：结果按 URL 恢复，整页刷新不丢（此前刷新后需重跑，重复消耗模型额度）。
- 0.4.0：删除 `src/mla/ui/` 与 123 条 AppTest 用例，CI 接入前端步骤与 Playwright E2E。

## 3. 已知问题（按优先级）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | ~~凭据与数据路径取 `Path.cwd()`，换目录启动就在新目录建空库~~ **已修（0.3.0）**：凭据 / 数据库 / 索引统一落用户目录（`MLA_HOME` 可覆盖），旧库首次启动一致性复制 | — |
| 2 | ~~结果页展示过程信息（耗时、覆盖率注记、判定来源徽标）~~ **已修（0.3.1 / 0.4.0）**：三层信息架构 + 过程开关（默认关）；前端只在 `detail.process !== null` 时挂载第三层 | — |
| 3 | `pyright` 对 `tests/` 有 21 个既有错误（实测 `python -m pyright tests` → `21 errors`），门禁只跑 `src`（`CONTRIBUTING.md:38`、`:52`）；但 `pyproject.toml:90` 的 `include` 仍是 `["src", "tests"]`，本地全量检查必红 | 改测试时没有类型保护，改动是否破坏用例只能靠跑测试发现；红惯了就会忽略红 |
| 4 | 测试标记只覆盖 718/927，209 条未打标（`test_credentials.py` 40、`test_llm.py` 24、`test_credential_migration.py` 19 为首） | `-m no_llm` 与 `-m mock_llm` 都不代表全量；分套跑全绿会漏掉未打标用例的回归 |
| 5 | ~~`docs/architecture.md` 写"SQLite 5 张表"~~ **已修（2026-11-17）**：改为 6 张表并补 `settings` 用途说明 | — |
| 6 | ~~`README.md` 的「版本」一节不写当前版本号~~ **已修（2026-11-17）**：开头补当前版本号，保留 Releases / CHANGELOG 指引 | — |
| 7 | 条款审查存在漏检与模型幻觉：演示合同中「无偿加班 / 豁免节假日加班费」「免除工伤责任」「侵占职务成果」「概括放弃全部权益」曾未给结论；另有一次把「终身竞业」误述为「10 年」 **部分已修（0.3.2）**：前两类与「概括放弃」已靠补触发器召回解决；幻觉与职务成果归属仍未覆盖 | 漏检会让用户以为合同没问题；幻觉会给出错误的修改建议。职务成果归属需先扩充法源库（著作权法、专利法）再评估 |
| 8 | 前端首屏性能对比不可复测：Streamlit 已删除，006 AC-08 只剩服务层基准（`scripts/benchmark_frontend.py` + 冻结基线 `tests/api/fixtures/frontend-perf-v0.3.2.json`） | 前端渲染回归只能靠 E2E 与人工验证，没有可比的数字 |
| 9 | 导出报告（docx）覆盖口径重算：`services/report.py:255` 的 `judged` 已含 `not_applicable`，下一句又单列「不适用 N 条」，三个数相加会大于 `total`（既有口径，未修） | 报告读者会对不上账，误判覆盖率 |
| 10 | `rule_check` 条目 `kind` 与 `certainty` 语义冲突：`graph/nodes.py:379` 按 C 级给 `kind=待确认`，而 `grading.py:87-97`（`certainty_for`）因 `source=rule_check` 给 `certainty=确定冲突` | 消费方各取一个就分叉；前端 `FindingCard.vue:22` 以 `certainty` 为准，`kind` 实际无人读 |
| 11 | `scripts/eval_qa.py:125` 产出 `evalsets/last_qa_eval.json`，但仓库内没有任何读取方（管理端评测页只读 `last_eval.json`） | 跑完 QA 评测看不到结果，等于白跑；要么接页面，要么明确标为离线产物 |

## 4. 0.3.0：已完成（凭据与数据路径重构）

规格：`docs/design/002-credentials.md`（含 24 条验收标准 AC-01…AC-24），按 5 个阶段推进，每阶段独立可验收；五个阶段均已落地，实现在 `services/credentials.py` / `adapters/auth_file.py` / `services/bootstrap.py`。

| 阶段 | 做什么 | 破坏性 | 状态 |
| --- | --- | --- | --- |
| 1 | 用户级 `auth.json` + 统一 resolver（`env > auth_file` 两级优先级），纯新增，不改现有调用方 | 否 | 已完成 |
| 2 | 数据库与索引目录迁到用户目录，旧库做一致性复制 | 否（行为变更） | 已完成 |
| 3 | 调用方收敛到 resolver，设置页改「测试并保存」+ 状态行 | 内部接口 | 已完成 |
| 4 | 首次凭据迁移、幂等标记，删除旧库中的 Key 行 | **是** | 已完成 |
| 5 | `mla auth list/set/remove` + UI 交互补项（更改 / 清除 Key、手动测试连接）+ 版本发布 | 否 | 已完成 |

Key 相关的三条硬约束（后续维护同样适用）：

- 校验只发 `GET /models`，不产生对话请求、不消耗 token；超时用 `MLA_AUTH_CHECK_TIMEOUT`，与推理超时互不影响。
- 保存前必校验：`GET /models` 通过才落盘，失败按 401 / 超时 / 断网三类分别提示，不通过不写 `auth.json`。
- 保存后前端不可查看：界面只回答"是否已配置"和"上次校验是否通过"，输入框保存后清空且不回填。

## 5. 0.3.1 / 0.3.2 已完成

- 0.3.1 结果页三层收敛（规格 `docs/design/005-result-ia.md`，AC 全绿）：**结论**（冲突 / 待确认条数、条目、常驻条号、必备条款、处理建议）默认可见 → **依据**（法条原文、类案参照、依据事实）折叠 → **过程**（耗时、引用条文数、覆盖率明细、判定来源与内部等级、补问轮数、降级、运行详情）默认隐藏，由设置项「显示查询路径与判定来源」控制（键名 `ui.show_query_path`，落 `settings` 表，默认关）。纯呈现层改动：判定逻辑、条目数与排序、覆盖口径均未变，另加逐字段比对快照测试。
- 0.3.2 触发器召回修复：补齐 12 条 pattern（单方面调岗、综合工时、薪酬含加班费、延迟发放、放弃五险一金、人身安全风险自负、终身竞业且无补偿、培训费违约金、提前 90 日离职、离职扣工资等），新增 `TR-FEMALE-PREGNANCY-RESTRICT` 与 `TR-EXIT-CERT-WITHHOLD` 两个触发器。演示合同 12 类缺陷命中 8 → 10 类，条目 11 → 24 条；四份样本合同候选基线 22 / 9 / 13 / 0 未变，57 条负样本零新增命中。
- 该次复测证明漏检根因是触发器召回而非模型能力：同一模型、同一提示词，只补 pattern 就从 11 条涨到 24 条。

## 6. 0.4.0：已完成（前端迁移）

规格：`docs/design/006-frontend-migration.md`（AC-01…AC-10），分 6 批推进，每批独立可验收；六批均已落地，一次替换并删除 Streamlit UI。

- 接口先行：`src/mla/api/` 九组端点 + `api/presenters.py` 统一展示层口径（原在 `ui/app.py`），前端只消费 DTO，不做判定、计数、分级、排序。
- 跑起来的方式：`uvicorn mla.api.app:create_app --factory`，同源托管 `web/dist` 与 `src/mla/api/static/` 的 SPA 回退；默认只绑回环、不开跨域。核对改异步：`POST /checks` 返回 202 + `job_id`，进程内单 worker 轮询（job 与内存任务重启即失效）。
- 范围外改动（006 §7）：结果存档开关 `archive=true|false` 与任务属主 `owner_id`；旧记录补默认值、不回填。
- 已知留白：AC-08 的前端首屏对比随 Streamlit 删除不可重跑（见第 3 节第 8 条），前端性能由 E2E 与人工验证覆盖。

## 7. 1.0.0：打包（下一步）

- exe 打包（PyInstaller 内嵌 uvicorn + 前端构建产物）。前端迁移已完成，不再有"打包目标随 UI 重写返工"的风险；注意 wheel / exe 构建前必须先 `npm --prefix web run build` 并把产物复制进 `src/mla/api/static/`，否则包内没有页面。
- 打包前必须先解决四件事：
  - 模型不可用时的体验：现在直接拒绝核对，需要给出可操作的下一步（去设置填 Key？先用确定性规则跑一遍？）。
  - 首次启动向导：账号 / Key / 示例合同三步走通，不要让人对着空界面猜。
  - 杀软误报：PyInstaller 产物的签名或白名单说明，否则用户第一关就过不去。
  - 口令哈希迭代数 12 万 → 60 万（OWASP 2023 建议）：现有库只存裸哈希与盐、未存迭代数，直接改会让已有账号无法登录。需配套"登录时用旧参数验证 → 用新参数重算 → 回写"的渐进升级，属可测的行为变更。

## 8. 交接要点

- 规格位置：`docs/design/002-credentials.md`（凭据与路径，0.3.0 已实现）、`003-model-first.md`（模型主导与共识层）、`005-result-ia.md`（结果页三层）、`006-frontend-migration.md`（0.4.0 前端迁移）。改行为先改规格，再改代码。
- 启动方式变了，三条命令走完：
  `pip install -e ".[web]"` → `npm --prefix web ci` 再 `npm --prefix web run build` → `uvicorn mla.api.app:create_app --factory`。
  开发态另跑 `npm --prefix web run dev`（Vite :5173，`/api` 代理到 8000）。旧界面是 Streamlit（`src/mla/ui/`），已删除；降级到 0.3.x 要重装 `[ui]` extra，业务数据无需迁移。
- 0.3.0 的破坏性代价：凭据与数据改在用户目录，降级到 0.2.x 需重新填写 Key，业务数据不删除。
- 数据真源：`src/mla/data/`（法源 / 规则 / 触发器 / 要素 / 评测集），随包分发、按只读数据处理；改规则等于改行为，必须跑对应校验脚本并写明法条依据。
- 不要改的：`CHANGELOG.md` 里 0.1.x / 0.2.0 的历史条目（含其中"no_llm 313"这类当时的数字）、`clause_gold.jsonl` 的既有标注（降级或剔除要单独计数，并在 PR 说明里写原因与日期）。
- 已知教训一：测试不要依赖被 gitignore 的产物。`tests/api/test_admin.py` 的评测页用例曾断言 `available=true`，读的是被忽略的 `last_eval.json`——本地跑过评测所以绿，CI 干净检出必挂（run #25）。改为 `tmp_path` 自造产物 + monkeypatch `EVAL_FILE` / `EVAL_HISTORY_FILE`。
- 已知教训二："命令输出里有成功字样"不等于"远端真的有"。推送 / 发布后要回远端核一遍（`git log origin/main`、看 Actions 结论、必要时拉一次），别信本地那一行 OK。
- 复现评测：`python scripts/eval_clauses.py --contracts <四份合同> --runs 1 --no-generalization`。注意 `--contracts` 缺省只跑一份 `contract_traps_01.txt`，直接跑会低估工作量与覆盖面。
- 门禁顺序建议：`pytest` → `ruff check src tests scripts` → `pyright src` → `python scripts/check_ui_text.py` → `npm --prefix web run typecheck` → `npm --prefix web run test -- --run` → `npm --prefix web run build`（改前端再加 `test:e2e`）→ 改了哪类数据就跑对应 validate 脚本；CI 另要求覆盖率 ≥ 75%。
