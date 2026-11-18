# 004 下一阶段路线图（交接）

面向接续本项目的人：读这一份就知道仓库停在哪、下一步按什么顺序做、哪些东西不要碰。
日期 2026-11-18，对应版本 v0.4.0。本文件只描述路线，不改代码也不改数据；规格细节以同目录下 002 / 003 / 006 原文为准。

## 1. 当前状态（2026-11-18）

```
版本 v0.4.0（src/mla/__init__.py:8）
测试 925 passed / 1 skipped（collect-only 926 条；no_llm 488 / mock_llm 229，并集 717，209 条未打标）
      前端另有 vitest 单测与 Playwright E2E（真实后端进程 + 打桩模型）
规则 16 / 触发器 35 / 法源 143 / 要素 13 / clause_gold 95
门禁 pytest + ruff check src tests scripts + pyright src + check_ui_text（含 web/src）
     + npm typecheck / vitest / build（CI 另跑 E2E）+ 6 个数据校验脚本
```

数字口径：`pytest` 全量跑完 926 条，其中 925 passed / 1 skipped；分套数用 `pytest --collect-only -m no_llm` / `-m mock_llm` 得到（488 / 229，两套无重叠，并集 717；未打标 209 条）。0.4.0 删除 Streamlit 界面后，删掉的 123 条界面/AppTest 用例已由 `tests/api/` 与 `web/tests/` 覆盖（两处未覆盖的迁到 `tests/api/test_trend.py` 与 `tests/api/test_enterprise.py`）。规则、触发器、要素来自 `src/mla/data/rules/rules.yaml` 与 `clauses/*.yaml` 的条目数，法源与 gold 来自两个 `.jsonl` 的非空行数。数据校验脚本清单见 `.github/workflows/ci.yml` 的 `Validate data` 步骤（statutes / rules / triggers / missing_terms / clause_gold / elements，六个齐全）。

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
- 新增 `mla auth list/set/remove`（`set` 用 `getpass` 隐藏输入，不接受命令行传 Key）；`.streamlit/secrets.toml.example` 删除 `[llm]` 段，只留 `[embedding]`。
- 0.3.1：结果页按结论 / 依据 / 过程三层收敛，过程信息由设置项「显示查询路径与判定来源」统一控制（默认关闭）
- 0.3.2：补齐 12 条触发器 pattern，演示合同 12 类缺陷命中从 8 类提升到 10 类
- 0.4.0：界面从 Streamlit 换成 FastAPI 同源 API + Vue 3 单页应用（规格 006）；核对改异步（202 + job 轮询）、核对结果存档开关（默认进企业档案）、首次核对自动落库；结果页三层与过程开关在 API 与前端同时落地；删除 `src/mla/ui/` 与 streamlit / pandas / altair 依赖

## 3. 已知问题（按优先级）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | ~~凭据与数据路径绑当前工作目录：`config.py:27-29` 的 `_cwd_path`、`:62` 的 `index_dir`、`:106` / `:194` 的 `db` 全部取 `Path.cwd()`；换目录启动（桌面快捷方式、从别的盘运行、CI 里的 wheel 冒烟）就在新目录建空库：账号、历史、已存 Key 一起"消失"~~ **已修（0.3.0）**：凭据、数据库、索引统一落到用户目录（`%LOCALAPPDATA%\mla\`，`MLA_HOME` 可覆盖），旧库首次启动一致性复制 | — |
| 2 | ~~结果页仍展示过程信息：耗时、覆盖率注记、判定来源徽标~~ **已修（0.3.1 / 0.4.0）**：三层信息架构落地（0.3.1），过程信息由设置项「显示查询路径与判定来源」控制且默认关闭；0.4.0 前端只在 `detail.process !== null` 时挂载第三层 | — |
| 3 | `pyright` 对 `tests/` 有既有错误（门禁只跑 `src`，见 `CONTRIBUTING.md:47` 与 `ci.yml` 注释） | 改测试时没有类型保护，改动是否破坏用例只能靠跑测试发现；`pyproject.toml` 的 `include` 含 `tests`，本地全量检查会红，容易养成忽略红的习惯 |
| 4 | 测试标记只覆盖 717/926，209 条未打标；0.3.0 新增的凭据 / 迁移 / CLI auth 用例多数尚未打标（`test_credentials.py`(40) 为首）。0.4.0 删除的 `test_ui_smoke.py` / `test_ui_setup.py` 原本也是未打标大户 | `-m no_llm` 与 `-m mock_llm` 都不代表全量；分套跑全绿会漏掉未打标用例的回归 |
| 5 | ~~`docs/architecture.md:22` 写"SQLite 5 张表（users / enterprises / contracts / tasks / logs）"~~ **已修（2026-11-17）**：改为 6 张表并补 `settings` 用途说明 | — |
| 6 | ~~`README.md:247` 的「版本」一节只指向 Releases 与 CHANGELOG，不写当前版本号~~ **已修（2026-11-17）**：开头补当前版本号，保留 Releases / CHANGELOG 指引 | — |
| 7 | 条款审查存在漏检与模型幻觉：演示合同中「无偿加班/豁免节假日加班费」「免除工伤责任」「侵占职务成果」「概括放弃全部权益」四类未给出结论；另有一次把「终身竞业」误述为「10 年」 **部分已修（0.3.2）**：无偿加班、免除工伤、概括放弃全部权益三类漏检已通过补齐触发器召回解决；模型幻觉（把「终身」误述为「10 年」）与职务成果归属仍未覆盖 | 漏检会让用户以为合同没问题；幻觉会给出错误的修改建议。职务成果归属需先扩充法源库（著作权法、专利法）再评估，其余靠触发器补充与提示词约束

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

## 5. 0.4.0：前端迁移（已完成）

规格：`docs/design/006-frontend-migration.md`（AC-01…AC-10），分 6 批推进，每批独立可验收；六批均已落地，0.4.0 一次替换、删除 Streamlit UI。

- 结果页三层收敛：**结论**（默认可见，只讲确定冲突 / 待确认）→ **依据**（点开看条文原文，条号常驻可见）→ **过程**（耗时、覆盖率、判定来源默认隐藏）。**已完成（0.3.1）**，规格见 `docs/design/005-result-ia.md`。
- 设置项补一个开关：是否显示"查询路径 / 判定来源"，默认关闭；把第 3 节第 2 条的矛盾交给用户选择。**已完成（0.3.1）**，键名 `ui.show_query_path`，落 `settings` 表。
- FastAPI + Vue 迁移：先定接口契约（请求 / 响应结构、错误码），UI 后换，避免一次改两层。**已完成（0.4.0）**：`src/mla/api/` 九组端点 + `web/` 单页应用，界面与内层解耦；试运行以 `uvicorn mla.api.app:create_app --factory` 启动。
- 范围外改动（006 §7）：核对结果存档开关 `archive=true|false` 与任务属主 `owner_id`，旧记录补默认值不回填。

## 6. 1.0.0：打包（下一步）

- exe 打包（PyInstaller 内嵌 uvicorn + 前端构建产物），前端迁移已完成，不再有"打包目标随 UI 重写返工"的风险。
- 动手前必须先解决三件事：模型不可用时的体验（现在直接拒绝核对，需给出可操作的下一步）、首次启动向导（账号 / Key / 示例合同）、杀软误报（签名或白名单说明）。
- 口令哈希迭代数从 12 万提到 60 万（OWASP 2023 建议）：现有库只存裸哈希与盐、未存迭代数，直接改会让已有账号无法登录。需配套"登录时用旧参数验证 → 用新参数重算 → 回写"的渐进升级，属可测的行为变更，打包前一并做。

## 7. 交接要点

- 规格位置：`docs/design/002-credentials.md`（凭据与路径，0.3.0 已实现）、`docs/design/003-model-first.md`（模型主导架构）、`docs/design/006-frontend-migration.md`（0.4.0 前端迁移）。改行为先改规格，再改代码。
- 0.3.0 的破坏性代价：凭据与数据改在用户目录，降级到 0.2.x 需重新填写 Key，业务数据不删除。
- 下一步：**1.0.0 打包**。0.4.0 之前的界面是 Streamlit（`src/mla/ui/`），已删除；降级到 0.3.x 需重新装 `[ui]` extra，数据无需迁移。
- 数据真源：`src/mla/data/`（法源 / 规则 / 触发器 / 要素 / 评测集），随包分发、按只读数据处理；改规则等于改行为，必须跑对应校验脚本并写明法条依据。
- 不要改的：`CHANGELOG.md` 里 0.1.x / 0.2.0 的历史条目（含其中"no_llm 313"这类当时的数字）、`clause_gold.jsonl` 的既有标注（降级或剔除要单独计数并在 PR 说明里写原因与日期）。
- 复现评测：`python scripts/eval_clauses.py --contracts <四份合同> --runs 1 --no-generalization`。注意 `--contracts` 缺省只跑一份 `contract_traps_01.txt`，直接跑会低估工作量与覆盖面。
- 门禁顺序建议：`pytest` → `ruff check src tests scripts` → `pyright src` → `python scripts/check_ui_text.py` → `npm --prefix web run typecheck` → `npm --prefix web run test -- --run` → `npm --prefix web run build`（改前端再加 `test:e2e`）→ 改了哪类数据就跑对应 validate 脚本；CI 另要求覆盖率 ≥ 75%。
