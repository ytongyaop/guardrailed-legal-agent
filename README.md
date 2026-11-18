# guardrailed-legal-agent

[![CI](https://github.com/ytongyaop/guardrailed-legal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ytongyaop/guardrailed-legal-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A guardrailed agent for labor-contract risk review: model-led clause judgment checked by deterministic rules and programmatic verification, and source-traceable output for small businesses without in-house legal counsel.

面向小微企业（有雇工、缺专职法务）的劳动合同风险辅助核对系统。受限智能体（guardrailed agent）架构：流程由确定性代码固定，模型负责读懂合同并逐条判定，规则与脚本负责给法条原文、限制输出形态、核算数值前提；模型每一条结论都要通过程序核验才会呈现给用户。

技术栈：Python 3.11+（开发环境 3.14）· LangGraph 编排（13 个固定节点）· FastAPI 同源 API + Vue 3 / Element Plus 单页界面（Vite 构建）· SQLite 3.50 存储 · DeepSeek `deepseek-chat`（OpenAI 兼容接口）· 阿里云 `qwen3.7-text-embedding-flash` 语义向量（1024 维，无 Key 自动回退 BM25）· python-docx 导出 Word 报告。

## 它解决什么问题

小微企业主自己签劳动合同，没有专职法务帮着把关条款。现有工具在这件事上有三个常见的坑：

- 通用法律问答大模型会编造法条引用：条号看起来像真的，用户无从核实，错引比不引更危险。
- 通用语义检索在法条上容易失效：法条语言精确、适用条件严格，向量相似度召回的常是"看起来像、适用条件不同"的条文。
- 缺事实前提的判定会硬猜：合同期限没填，也照样给出试用期结论，"判不了"被悄悄当成"没问题"。

本项目的对策是把能定死的定死、把模型说的话核验过再放出去：

- 每条结论绑定 `statute_id` 并指向法条原文，校验器强制 100% 可追溯；建议类条目没有依据就拒绝生成。
- 事实不足时补问：阻断项缺了不能出结论，其余可补充事实允许用户跳过；问题越界时拒答并建议转专业咨询。
- 模型负责判，规则负责卡数值：条款风险由模型逐条判定，数值前提（试用期上限、工资比例等）由规则核算，两边在汇总时对账；对外只呈现两层——确定冲突、待确认。判不了必须说出来，不静默折叠进"合规"。

## 设计要点：模型主判定，规则做约束与校验

怎么判、谁来判，写死在各环节的分工里：

| 环节 | 判什么 | 谁来做 | 程序管什么 |
| --- | --- | --- | --- |
| 条款判定 | 合同里有没有值得警惕的条款，例如约定放弃缴纳社保、加班费按基本工资计基数、试用期内可随时解除 | 33 条触发器先召回可疑段落，模型逐条判定并给出等级、理由、证据原文与法条原文摘抄 | 每条结论过六道核验（等级、证据定位、法条在库、引用闭集、缺依据降级、重叠度） |
| 数值校验 | 试用期上限、试用期工资比例、加班倍率、竞业限制期限与补偿、社保登记、年假天数、工资支付次数等数值是否超限 | 16 条确定性规则，纯函数，相同输入必然相同输出 | 只输出校验信号（一致 / 不一致 / 待补事实），不单独成为给用户看的结论 |
| 缺项检查 | 《劳动合同法》第十七条规定的九类必备条款，合同正文有没有写 | 逐行匹配、不跨行拼接 | 只判"写没写"、不判合法性；第（九）类为兜底，不判、不进覆盖率分母 |

汇总时模型结论与数值校验按法条条号对账：模型说"没问题"而数值超限的降一级，数值落在法定区间而模型判有问题的也降一级，等级只降不升。对外只展示两层——"确定冲突"（内部 A 级，或数值校验与法条不一致的条目）与"待确认"（内部 B、C 级），列表按影响度（高 / 中 / 低）分组排序，同组内先列确定冲突。

内部等级 A/B/C 仍然保留，只用于核验与降级链，不再直接展示：A 级＝法条明文禁止，必须同时给出法条编号、合同原文证据与法条原文摘抄；B 级＝需补充事实或含裁量；C 级＝现有事实不足以下定性。对外 A 级与规则数值校验条目合并成"确定冲突"，B 级与 C 级合并成"待确认"。

追问机制同属这套设计：阻断项只有 3 个（用工主体类型、是否签订书面合同、合同期限），缺了不能出结论；其余是可补充事实，用户可以跳过。问什么由程序决定，模型只负责润色问句、从字段闭集里挑可补充项，输出经八道核验，不合格逐条退回固定问句。

### 为什么是模型判、规则校验

数值问题有标准答案：合同期限三年以上，试用期最长六个月——这类判定写成纯函数即可复现、可审计、零调用成本，让模型去算只会引入不确定性。语义问题写不成规则：合同里一句"乙方自愿放弃缴纳社会保险，甲方以现金补贴代替"是否构成风险，需要理解自然语言，规则枚举不完。

所以位置是：模型是主判定通道，负责读懂条款、给等级、写理由，并摘抄所引法条原文；规则不判语义，负责三件事——把可引用的法条原文与闭集递给模型、限制模型输出的形态、核算数值前提。模型每一条结论都要过六道程序核验：引用不得越出候选法条闭集，证据必须是合同原文中能定位的 10–100 字连续片段，A 级还要摘抄法条原文（必须是该条原文的连续子串），证据与候选片段重叠不足按降级处理。规则不会幻觉，模型会；核验拦住模型，对账拦住两边不一致。无模型时系统不发起核对，不给缺了主判定通道的残缺结论。

## 快速开始

环境要求：Python 3.11+、Node 20+（只有构建界面才需要）。

```bash
# 仅命令行（规则核对、评测脚本）
pip install -e .

# 含 Web 服务与报告导出（完整功能）：FastAPI + uvicorn + python-docx
pip install -e ".[web,files]"

# 构建界面（产物在 web/dist，后端同源托管）
npm --prefix web ci
npm --prefix web run build
```

装好后启动服务：

```bash
uvicorn mla.api.app:create_app --factory --port 8000
```

浏览器打开 <http://127.0.0.1:8000>（默认只绑回环地址，不开跨域）。库里还没有管理员账号时，页面会先显示初始化表单，填用户名、密码、企业名称，提交后直接进入主界面，不用再登录一次。

开发态另开一个终端跑 `npm --prefix web run dev`（Vite :5173，把 `/api` 代理到 8000），页面改动热更新；不启动 Vite 时后端直接托管 `web/dist` 的构建产物。

- 密码至少 8 位：注册、首次初始化、改密码用同一条下限。
- 首次登录会展示一次使用说明与免责声明，看过一次后不再弹出。
- 账号相关操作都在设置页：管理员可在那里测试并保存模型 Key，所有登录用户都可以修改密码。

### 无界面部署（可选）

没有浏览器，或者要在服务器上脚本化部署时，改用命令行完成同样的初始化：

```bash
python -m mla init --enterprise "示例企业"
python -m mla check --contract src/mla/data/evalsets/sample_contract.txt
```

`init` 默认创建用户名为 admin 的管理员；不传 `--admin-password` 时自动生成十六位随机口令，建号后只打印一次，请立即记录（之后不再显示）；自己指定口令时至少 8 位，不足直接报错退出。`check` 对仓库自带示例合同跑一次命令行核对，不依赖界面。

没有安装 `[web]` 依赖又没有构建产物时，`uvicorn` 起不来或页面返回 503 与构建提示；Word 报告导出需要 `[files]`（`python-docx`），命令行核对与评测脚本不受影响。

密钥按需配置：完整能力需要 DeepSeek 的模型 Key，语义向量检索另需 `DASHSCOPE_API_KEY`（阿里云）。模型 Key 在**设置页 → 「测试并保存」**里配置（没有界面时用 `mla auth set deepseek`）：保存前会发一次 `GET /models` 校验，不通过不落盘；保存成功后写入当前操作系统用户的凭据文件，之后每次启动自动沿用。环境变量 `MLA_LLM_API_KEY` 可临时覆盖凭据文件（优先级更高，且不写任何文件）。不配 Key 时系统不装作能用，而是显式拒绝或降级：

- 核对整体被拒绝：没有模型就不发起核对，不给缺了主判定通道的结论（服务层同一道门禁，CLI 退出码 2，界面提示去设置页配置）；
- 不再是"条款审查不执行但规则照跑"：核对既然被拒绝，也就不存在半套结果；条款审查开关同时置灰；
- 处理建议不生成，范围内问答同样拒绝，不降级成模板回答；
- 登录、设置、企业信息、历史记录与既有报告导出不受影响；法条检索仍可回退 BM25 离线使用；
- 离线评测与诊断用 `MLA_ALLOW_NO_LLM=1` 显式放行旧路径：抽取回退纯正则、规则照跑，仅供测试，不作为产品行为。

## 配置

密钥来源与优先级（只有两级，前一处非空即生效）：

| 优先级 | 位置 | 说明 |
| --- | --- | --- |
| 1 | 环境变量 `MLA_LLM_API_KEY` | 临时覆盖、容器或 CI 里常用；只读，不会写进任何文件 |
| 2 | 用户凭据文件 `<MLA_HOME>/auth.json` 的 `deepseek` 条目 | 设置页「测试并保存」或 `mla auth set deepseek` 写入；Windows 下即 `%LOCALAPPDATA%\mla\auth.json` |
| — | 都为空 | 未配置：核对被拒绝，不降级成模板回答 |

`.streamlit/secrets.toml` 的 `[llm]` 段**不再被读取**：本版没有这条来源，填了也不生效；该文件现在只用于向量检索（`[embedding].api_key`，与 `DASHSCOPE_API_KEY` 等价，二选一）。要换 Key 就在设置页重填并保存，或用 `mla auth set`。

凭据文件以明文保存（已收紧权限，只当前用户可读），**不要分享或提交该文件**；`mla auth list` 只显示服务商、类型、来源与文件路径，不显示 Key。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MLA_LLM_API_KEY` | DeepSeek API 密钥；优先级最高，临时覆盖用户凭据文件，不写入任何文件 | 空 |
| `MLA_LLM_MODEL` | 对话模型名（OpenAI 兼容接口） | `deepseek-chat` |
| `MLA_LLM_BASE_URL` | 接口地址 | `https://api.deepseek.com` |
| `DASHSCOPE_API_KEY` | 阿里云密钥，用于 `qwen3.7-text-embedding-flash` 语义向量；也可放 `secrets.toml` 的 `[embedding]` 段 `api_key` | 空，未配置时检索回退 BM25 |
| `MLA_EXTRACT_MODE` | 抽取档位：`auto`＝正则优先、模型补缺失字段；`llm`＝强制模型；`regex`＝纯正则 | `auto` |
| `MLA_CLAUSE_REVIEW` | 条款判定（触发器召回 + 模型判定）开关，`on` / `true` / `1` / `yes` 为开 | `on` |
| `MLA_EXTRACT_MAX_CHARS` | 发送给模型的合同正文字数上限 | `20000` |
| `MLA_HOME` | 用户级运行目录（凭据、数据库、索引都放这里）；必须是绝对路径 | Windows 下 `%LOCALAPPDATA%\mla\` |
| `MLA_DB` | SQLite 数据库路径（账号/企业/合同/任务/日志） | `<MLA_HOME>\mla.sqlite3` |
| `MLA_INDEX_DIR` | 向量缓存目录 | `<MLA_HOME>\index` |

数据库、向量缓存与凭据都是运行期可写文件，默认落在**用户级目录** `%LOCALAPPDATA%\mla\`（`MLA_HOME`；非 Windows 为 `$XDG_DATA_HOME/mla` 或 `~/.local/share/mla`），不写进包目录（`site-packages` 可能只读），也不再随启动目录变化。数据文件本身（法源、规则、条款、案例、评测集）随包只读分发；需要指向别处时用上表中的 `MLA_*` 变量覆盖。

从 0.2.x 升级时：未显式设置 `MLA_DB`，首次启动会把旧的工作目录 `mla.sqlite3` 一致性复制到新位置（旧文件保留），并把旧库里的 Key 迁进 `auth.json`、清掉旧库那一行。降级回 0.2.x 需要重新填写 Key，业务数据不会删。

### 忘记密码

本地应用没有邮箱与短信，口令只能在本机终端重置：

```bash
python -m mla admin reset --username 你的账号
```

不传 `--password` 时生成 16 位随机口令并只显示一次；自己指定时至少 8 位。若所有管理员账号都忘记了，先列账号再把某个账号提为管理员：

```bash
python -m mla admin list
python -m mla admin promote --username 你的账号
```

登录页的「忘记密码」只展示这三条命令，不做网页找回。

## 数据与评测

数据资产全部以文件形式存放在 `src/mla/data/` 目录，随包分发（`pip install` 后位于 `site-packages/mla/data/`），代码通过 `mla.config.data_path()` 解析，不依赖运行时工作目录：

| 数据 | 规模 | 说明 |
| --- | --- | --- |
| 法源 | 143 条 | 劳动合同法、实施条例、劳动法、社保法、工资支付条例、年休假条例、工伤保险条例、司法解释等 |
| 确定性规则 | 16 条 | 数值校验，数据驱动 |
| 条款触发器 | 33 条 | 条款召回（只召回，不判定） |
| 必备条款规格 | 9 类 | 依据《劳动合同法》第十七条 |
| 官方案例 | 37 条 | 附在结论后供参考 |
| 评测数据 | 回归集：主流程 52 条、条款级 95 条（正样本 38、负样本 57）；留出集：非模板合同 12 条、问答 20 条 | 两类纪律不同：回归集每次改动必跑、只要求不退化；留出集不参与调优、用于报告能力。构造方式见 [docs/evaluation.md](docs/evaluation.md) |
| 测试 | Python 926 条（本版实测 925 passed / 1 skipped），前端另有 vitest 单测与 Playwright E2E | 全部离线可复现：测试强制纯正则抽取并屏蔽真实 Key；E2E 用打桩模型 |

评测结果（口径与数字同样重要，务必连着读）：

| 集合 | 结果 | 95% CI | 口径 |
| --- | --- | --- | --- |
| 回归集（主流程 52 条） | 纯正则档与模型档的准确率、召回率均为 1.0000 | 准确率 [0.931, 1.000] | 该集合开发中反复修改过，满分属构造性覆盖，只能说明没有退化；纯正则抽取不能为全部 16 条规则提供依赖字段，缺字段的规则按"判不了"输出，不会被算成合规；接入模型抽取后 16 条全部可判 |
| 留出集（非模板合同 12 条，纯正则） | 准确率 0.4167、召回率 0.3636 | [0.193, 0.680] | 一次成型后未再改动，是留出数据；非模板写法才是对抽取稳健性的真实考验，且 12 条的 95% 区间很宽 |
| 留出集（非模板合同 12 条，接入模型） | 准确率、召回率均为 1.0000 | [0.757, 1.000] | 同上，模型抽取档在留出集上的表现 |
| 回归集（条款级 95 条） | 3 份对抗合同＋1 份全合规对照：38 条正样本覆盖 38/38，57 条负样本 A 级误报 0 | 覆盖率 [0.9082, 1.0000]；误报率上界 6.31% | 触发器与条款 gold 同步扩充，覆盖属构造性覆盖，不代表对未见合同的表现；陷阱为人工植入 |
| 必备条款覆盖率（4 份合同） | 1.000 / 1.000 / 0.875 / 0.750 | 未给区间 | 无独立评测集，这组数字是逐类人工对照基线；覆盖率分母不含第（九）类兜底 |

必须说明：回归集的满分是构造性覆盖，留出集只有 12 条与 20 条、置信区间很宽；两类数据都是单人标注，指标描述的是这批样本上的表现，不能当作泛化结论。指标定义、消融维度、置信区间与全部局限见 [docs/evaluation.md](docs/evaluation.md)。

## 目录结构

```
src/mla/
  domain/      rules.py missing.py clauses.py slots.py asking.py verify.py
               boundary.py checklist.py models.py policy.py field_catalog.py
  ports/       repository.py generation.py retrieval.py extraction.py
  adapters/    persist.py extract.py extract_llm.py rag.py llm.py files.py embedding.py
  graph/       build.py nodes.py state.py
  services/    tasks.py report.py clause_review.py asking.py qa_agent.py
               retrieval.py drafting.py auth.py
  api/         app.py deps.py errors.py jobs.py sessions.py permissions.py
               presenters.py schemas.py
    routes/    setup.py auth.py enterprise.py contracts.py settings.py admin.py
               checks.py history.py report.py
  eval/        metrics.py ablation.py
  cli.py
  config.py
  __main__.py
  data/                          随包分发的数据（只读）
    statutes/statutes.jsonl      143 条法源
    rules/rules.yaml             16 条规则
    clauses/triggers.yaml        33 条触发器
    clauses/missing_terms.yaml   9 类必备条款
    cases/cases.jsonl            37 条官方案例
    evalsets/                    回归集 52 ＋ 95，留出集 12 ＋ 20
web/            Vue 3 + Element Plus 前端（规格 006）
  src/          views/ components/ api/ stores/ composables/ router/ utils/
  tests/        vitest 单测（DTO 渲染、路由守卫、文案口径走 Python 脚本）
  e2e/          Playwright（真实后端进程 + 打桩模型）
scripts/        校验与评测脚本
tests/          926 条测试（Python；前端单测另计）
docs/           architecture.md evaluation.md design/
.github/        workflows/ci.yml · dependabot.yml · pull_request_template.md · ISSUE_TEMPLATE/
.streamlit/     secrets.toml.example（只剩 embedding 的 Key 指引，程序不读 [llm]）
LICENSE
CONTRIBUTING.md
SECURITY.md
pyproject.toml
.gitignore
.gitattributes
.env.example
```

分层设计与 13 个节点的路由说明见 [docs/architecture.md](docs/architecture.md)。

## 质量门禁

提交代码前全部执行，任何一条失败都不合入：

```bash
pytest --cov=mla --cov-fail-under=75
ruff check src tests scripts
pyright src
python scripts/check_ui_text.py
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build
python scripts/validate_statutes.py src/mla/data/statutes/statutes.jsonl
python scripts/validate_rules.py
python scripts/validate_triggers.py
python scripts/validate_missing_terms.py
python scripts/validate_clause_gold.py
python scripts/run_eval.py --extract regex
python scripts/eval_clauses.py --runs 1
```

`validate_*` 脚本分别校验法源、规则、触发器、必备条款规格与条款 gold 的结构一致性——数据文件是判定依据的一部分，数据坏了等于代码坏了。测试离线可复现：`tests/conftest.py` 强制 `MLA_EXTRACT_MODE=regex` 并屏蔽真实 Key；覆盖率下限 75%，与 CI 一致。`pyright` 只检 `src`，`tests` 里有既有的类型报错，不属于本次门禁范围。前端门禁是类型检查 + vitest + 构建；`check_ui_text.py` 同时扫 Python 文案与 `web/src/**/*.{vue,ts,css}`（文案规则只有这一份实现）。改到前端交互的还要跑 `npm --prefix web run test:e2e`（本机默认 Edge，CI 用 Chromium）。

## 隐私与边界

按外发内容分四条路径，口径写死，不含糊：

| 路径 | 发送给模型的内容 | 不发送的内容 |
|---|---|---|
| 抽取（LLM 档） | 合同正文，上限由 `MLA_EXTRACT_MAX_CHARS` 控制（默认 20000 字）；正则已抽到的字段优先，模型只补缺失字段，调用失败自动回退纯正则 | — |
| 条款审查 | 只发送被触发器召回的**候选条款原文**及其**前后各 2 行邻近段落** | 合同全文，以及与审查无关的段落（姓名、住址、身份证号、工资数等） |
| 检索（含向量 `dense_query`） | 只发送问题本身 | 合同正文、条款片段、结构化事实 |
| 追问 / 问答 | 只发送问题 + 本次核对的结构化事实（已抽字段、规则命中、条款段落号与等级、命中法条） | 合同正文、条款片段原文 |

- 抽取环节发送正文这件事界面有明确提示；条款审查只发候选邻近段落，因此模型看不到全文，它自己"额外发现"的问题会相应变少——这是隐私优先的取舍。
- 账号、企业、合同、任务、日志存本地 SQLite（默认 `<MLA_HOME>\mla.sqlite3`，Windows 下即 `%LOCALAPPDATA%\mla\mla.sqlite3`，可用 `MLA_DB` 覆盖；6 张表：users / enterprises / contracts / tasks / logs / settings）；一个账号绑定一个企业，任务删除为软删、日志留痕。模型 Key **不在数据库里**：它明文保存在当前操作系统用户的凭据文件 `auth.json`（同一目录），已收紧权限、不进日志；不要复制或分享该文件。数据库本身仍含账号口令散列，也应限制权限。

## 免责声明

本系统只提示"与某条法条存在冲突"并给出可定位的依据（法条编号、合同原文片段），不判定任何条款违法或无效，不构成法律意见，也不替代法律意见。正式决策请咨询执业律师或当地人力资源社会保障部门。

## 参与贡献

- 开发环境、门禁命令、改数据（规则 / 触发器 / 评测集）的硬规矩与 PR 要求：[CONTRIBUTING.md](CONTRIBUTING.md)
- 报告安全漏洞请勿开公开 Issue，按 [SECURITY.md](SECURITY.md) 的渠道联系

## 版本

当前版本 0.4.0（以 `src/mla/__init__.py` 与 [Releases](https://github.com/ytongyaop/guardrailed-legal-agent/releases) 为准）。0.4.0 把界面从 Streamlit 换成 Vue 3 + FastAPI，Streamlit 相关代码与依赖已删除。

详细变更见 [CHANGELOG.md](CHANGELOG.md)，版本号遵循语义化版本。

## English

A labor-contract risk review tool for small and micro businesses that have employees but no in-house legal staff. Clause risk is judged by a model: 33 triggers recall suspicious wording, the model grades each candidate and quotes the cited statute text, and six programmatic checks gate every conclusion (closed citation set, evidence located in the contract, verbatim statute echo, evidence overlap). 16 deterministic rules compute numeric limits (probation length, overtime rates, non-compete terms) and only emit verification signals, reconciled against the model's findings; a coverage check covers the nine mandatory clauses required by Article 17 of the Labor Contract Law. Without a model, core review is refused rather than degraded.

Every finding is bound to a statute identifier and must resolve to a real article in the bundled 143-entry statute set; suggestions without a citation are not generated. Missing facts are never silently reported as compliant: numeric rules keep four internal states, and items without enough facts are shown as "needs facts" instead of a conclusion.

Evaluation data is split into a regression set (used to detect regressions, built alongside the rules) and a held-out set (never used for tuning, used to report capability), with Wilson 95% confidence intervals. Results should be read as development-set numbers, not as generalization claims.

Requirements: Python 3.11+ (Node 20+ only to build the UI). Install with `pip install -e ".[web,files]"`, build the frontend with `npm --prefix web ci && npm --prefix web run build`, then start `uvicorn mla.api.app:create_app --factory --port 8000` and open http://127.0.0.1:8000; the first launch asks you to create an administrator account. There is no Streamlit UI since 0.4.0. Tests run fully offline (`pytest`, `npm --prefix web run test -- --run`).

Not legal advice. The tool reports conflicts with specific statutory provisions and points to the text; it does not determine whether a clause is invalid.

## License

MIT，见 [LICENSE](LICENSE)。
