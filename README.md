# guardrailed-legal-agent

[![CI](https://github.com/ytongyaop/guardrailed-legal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ytongyaop/guardrailed-legal-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A guardrailed agent for labor-contract risk review: deterministic rules, model-assisted clause analysis, and source-traceable output for small businesses without in-house legal counsel.

面向小微企业（有雇工、缺专职法务）的劳动合同风险辅助核对系统。受限智能体（guardrailed agent）架构：流程由确定性代码固定，模型只负责自然语言环节，所有输出必须通过规则与证据核验才会呈现给用户。

技术栈：Python 3.11+（开发环境 3.14）· LangGraph 编排（13 个固定节点）· Streamlit 1.66 界面 · SQLite 3.50 存储 · DeepSeek `deepseek-chat`（OpenAI 兼容接口）· 阿里云 `qwen3.7-text-embedding-flash` 语义向量（1024 维，无 Key 自动回退 BM25）· python-docx 导出 Word 报告。

## 它解决什么问题

小微企业主自己签劳动合同，没有专职法务帮着把关条款。现有工具在这件事上有三个常见的坑：

- 通用法律问答大模型会编造法条引用：条号看起来像真的，用户无从核实，错引比不引更危险。
- 通用语义检索在法条上容易失效：法条语言精确、适用条件严格，向量相似度召回的常是"看起来像、适用条件不同"的条文。
- 缺事实前提的判定会硬猜：合同期限没填，也照样给出试用期结论，"判不了"被悄悄当成"没问题"。

本项目的对策是把能定死的定死、把模型说的话核验过再放出去：

- 每条结论绑定 `statute_id` 并指向法条原文，校验器强制 100% 可追溯；建议类条目没有依据就拒绝生成。
- 事实不足时补问：阻断项缺了不能出结论，其余可补充事实允许用户跳过；问题越界时拒答并建议转专业咨询。
- 规则判定输出四态：违规、合规、不适用、无法判定。判不了必须说出来，不静默折叠进"合规"。

## 设计要点：三层判定

不同类型的问题交给不同的判定通道，各自独立出结果：

| 层 | 判什么 | 怎么判 | 模型参与 |
| --- | --- | --- | --- |
| 数值域 | 试用期上限、试用期工资比例、加班倍率、竞业限制期限与补偿、社保登记、年假天数、工资支付次数等数值是否合法 | 20 条确定性规则，纯函数，相同输入必然相同输出 | 不参与 |
| 存在性域 | 合同里有没有值得警惕的条款，例如约定放弃缴纳社保、加班费按基本工资计基数、试用期内可随时解除 | 33 条触发器先召回可疑条款，模型逐条判定 A/B/C 三级，再过六道程序核验 | 参与，但每条输出都被核验 |
| 缺项域 | 《劳动合同法》第十七条规定的九类必备条款，合同正文有没有写 | 逐行匹配、不跨行拼接，只判"写没写"、不判合法性；第（九）类为兜底，不判、不进覆盖率分母 | 不参与 |

存在性域的三级含义：A 级＝法条明文禁止（必须给出法条与合同原文证据），B 级＝需补事实或含裁量（必须给出法条与待补事实），C 级＝待确认。界面与报告按"规则判定 → A → B → C"的顺序混排展示。

追问机制同属这套设计：阻断项只有 3 个（用工主体类型、是否签订书面合同、合同期限），缺了不能出结论；其余是可补充事实，用户可以跳过。问什么由程序决定，模型只负责润色问句、从字段闭集里挑可补充项，输出经八道核验，不合格逐条退回固定问句。

### 为什么规则和模型要分工

数值问题有标准答案：合同期限三年以上，试用期最长六个月——这类判定写成纯函数即可复现、可审计、零调用成本，交给模型只会引入不确定性。语义问题写不成规则：合同里一句"乙方自愿放弃缴纳社会保险，甲方以现金补贴代替"是否构成风险，需要理解自然语言，规则枚举不完。

所以分工是：数值判定归规则，条款理解归模型，但模型每次发言后面都跟着程序核验——引用不得越出候选法条闭集，证据必须能在合同原文定位，缺依据的结论一律降级。规则不会幻觉；模型的幻觉被核验拦住。两层失效模式互不传染：模型不可用时，数值域与缺项域照常出结果，只有存在性域整体不跑。

## 快速开始

环境要求：Python 3.11+。

```bash
# 仅命令行（规则核对、评测脚本）
pip install -e .

# 含界面与报告导出（完整功能）
pip install -e ".[ui,files]"
```

装好后启动界面：

```bash
streamlit run src/mla/ui/app.py
```

浏览器打开命令行输出的地址（默认 8501 端口）。库里还没有管理员账号时，页面会先显示初始化表单，填用户名、密码、企业名称，提交后直接进入主界面，不用再登录一次。

- 密码至少 8 位：注册、首次初始化、改密码用同一条下限。
- 首次登录会展示一次使用说明与免责声明，看过一次后不再弹出。
- 账号相关操作都在设置页：管理员可在那里填写模型 Key，所有登录用户都可以修改密码。

### 无界面部署（可选）

没有浏览器，或者要在服务器上脚本化部署时，改用命令行完成同样的初始化：

```bash
python -m mla init --enterprise "示例企业"
python -m mla check --contract src/mla/data/evalsets/sample_contract.txt
```

`init` 默认创建用户名为 admin 的管理员；不传 `--admin-password` 时自动生成十六位随机口令，建号后只打印一次，请立即记录（之后不再显示）；自己指定口令时至少 8 位，不足直接报错退出。`check` 对仓库自带示例合同跑一次命令行核对，不依赖界面。

没有安装界面依赖（`[ui,files]`）时，`streamlit run` 与 Word 报告导出不可用；命令行核对与评测脚本不受影响。

密钥按需配置：完整能力需要 DeepSeek 的模型 Key，语义向量检索另需 `DASHSCOPE_API_KEY`（阿里云）。模型 Key 有三处来源，优先级为 `.streamlit/secrets.toml` > 设置页保存到本机数据库 > 环境变量 `MLA_LLM_API_KEY`；在设置页配置一次后，之后每次登录自动沿用，不必反复填。不配 Key 时系统不装作能用，而是显式降级：

- 关键字段抽取回退纯正则；
- 条款级审查不执行，界面开关置灰；
- 处理建议不生成，问答直接拒答，不降级成模板回答；
- 法条检索自动回退 BM25，仍可离线使用。

## 配置

密钥来源与优先级（三处都读原始值，前一处非空即生效）：

| 优先级 | 位置 | 说明 |
| --- | --- | --- |
| 1 | `.streamlit/secrets.toml` 的 `[llm]` 段 `api_key` | 服务端统一配置，该文件不入库 |
| 2 | 设置页保存到本机数据库 | 管理员填一次即落库，之后各次登录自动沿用 |
| 3 | 环境变量 `MLA_LLM_API_KEY` | 容器或 CI 里常用 |

任何一处都不写进仓库、不写进日志；要换 Key 时优先改前一处，改完重启界面即可。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MLA_LLM_API_KEY` | DeepSeek API 密钥；也可放 `secrets.toml` 的 `[llm]` 段 `api_key`，或在设置页保存到本机数据库 | 空 |
| `MLA_LLM_MODEL` | 对话模型名（OpenAI 兼容接口） | `deepseek-chat` |
| `MLA_LLM_BASE_URL` | 接口地址 | `https://api.deepseek.com` |
| `DASHSCOPE_API_KEY` | 阿里云密钥，用于 `qwen3.7-text-embedding-flash` 语义向量；也可放 `secrets.toml` 的 `[embedding]` 段 `api_key` | 空，未配置时检索回退 BM25 |
| `MLA_EXTRACT_MODE` | 抽取档位：`auto`＝正则优先、模型补缺失字段；`llm`＝强制模型；`regex`＝纯正则 | `auto` |
| `MLA_CLAUSE_REVIEW` | 条款级审查（存在性域）开关，`on` / `true` / `1` / `yes` 为开 | `on` |
| `MLA_EXTRACT_MAX_CHARS` | 发送给模型的合同正文字数上限 | `20000` |
| `MLA_DB` | SQLite 数据库路径（账号/企业/合同/任务/日志） | 工作目录下的 `mla.sqlite3` |
| `MLA_INDEX_DIR` | 向量缓存目录 | 工作目录下的 `data/index/` |

数据库与向量缓存是运行期可写文件，默认落在**当前工作目录**，不写进包目录（`site-packages` 可能只读）。数据文件本身（法源、规则、条款、案例、评测集）随包只读分发；需要指向别处时用上表中的 `MLA_*` 变量覆盖。

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
| 确定性规则 | 20 条 | 数值域判定，数据驱动 |
| 条款触发器 | 33 条 | 存在性域召回 |
| 必备条款规格 | 9 类 | 依据《劳动合同法》第十七条 |
| 官方案例 | 37 条 | 附在结论后供参考 |
| 评测数据 | 回归集：主流程 52 条、条款级 95 条（正样本 38、负样本 57）；留出集：非模板合同 12 条、问答 20 条 | 两类纪律不同：回归集每次改动必跑、只要求不退化；留出集不参与调优、用于报告能力。构造方式见 [docs/evaluation.md](docs/evaluation.md) |
| 测试 | 442 条 | 全部离线可复现：测试强制纯正则抽取并屏蔽真实 Key |

评测结果（口径与数字同样重要，务必连着读）：

| 集合 | 结果 | 95% CI | 口径 |
| --- | --- | --- | --- |
| 回归集（主流程 52 条） | 纯正则档与模型档的准确率、召回率均为 1.0000 | 准确率 [0.931, 1.000] | 该集合开发中反复修改过，满分属构造性覆盖，只能说明没有退化；纯正则只能为 20 条规则中的 7 条提供全部依赖字段，在 52 条合同上实际触发违规判定的规则为 4 条；接入模型抽取后 20 条全部可判 |
| 留出集（非模板合同 12 条，纯正则） | 准确率 0.4167、召回率 0.3636 | [0.193, 0.680] | 一次成型后未再改动，是留出数据；非模板写法才是对抽取稳健性的真实考验，且 12 条的 95% 区间很宽 |
| 留出集（非模板合同 12 条，接入模型） | 准确率、召回率均为 1.0000 | [0.757, 1.000] | 同上，模型抽取档在留出集上的表现 |
| 回归集（条款级 95 条） | 3 份对抗合同＋1 份全合规对照：38 条正样本覆盖 38/38，57 条负样本 A 级误报 0 | 覆盖率 [0.908, 1.000]；误报率上界 5.1% | 触发器与条款 gold 同步扩充，覆盖属构造性覆盖，不代表对未见合同的表现；陷阱为人工植入 |
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
  ui/          app.py trend.py
               assets/logo.svg
  eval/        metrics.py ablation.py
  cli.py
  config.py
  __main__.py
  data/                          随包分发的数据（只读）
    statutes/statutes.jsonl      143 条法源
    rules/rules.yaml             20 条规则
    clauses/triggers.yaml        33 条触发器
    clauses/missing_terms.yaml   9 类必备条款
    cases/cases.jsonl            37 条官方案例
    evalsets/                    回归集 52 ＋ 95，留出集 12 ＋ 20
scripts/        校验与评测脚本
tests/          442 条测试
docs/           architecture.md evaluation.md
.github/        workflows/ci.yml · dependabot.yml · pull_request_template.md · ISSUE_TEMPLATE/
.streamlit/     config.toml secrets.toml.example
mla.sqlite3     运行后在工作目录生成（不入库）
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
python scripts/validate_statutes.py src/mla/data/statutes/statutes.jsonl
python scripts/validate_rules.py
python scripts/validate_triggers.py
python scripts/validate_missing_terms.py
python scripts/validate_clause_gold.py
python scripts/run_eval.py --extract regex
python scripts/eval_clauses.py --runs 1
```

`validate_*` 脚本分别校验法源、规则、触发器、必备条款规格与条款 gold 的结构一致性——数据文件是判定依据的一部分，数据坏了等于代码坏了。测试离线可复现：`tests/conftest.py` 强制 `MLA_EXTRACT_MODE=regex` 并屏蔽真实 Key；覆盖率下限 75%，与 CI 一致。`pyright` 只检 `src`，`tests` 里有既有的类型报错，不属于本次门禁范围。

## 隐私与边界

按外发内容分四条路径，口径写死，不含糊：

| 路径 | 发送给模型的内容 | 不发送的内容 |
|---|---|---|
| 抽取（LLM 档） | 合同正文，上限由 `MLA_EXTRACT_MAX_CHARS` 控制（默认 20000 字）；正则已抽到的字段优先，模型只补缺失字段，调用失败自动回退纯正则 | — |
| 条款审查 | 只发送被触发器召回的**候选条款原文**及其**前后各 2 行邻近段落** | 合同全文，以及与审查无关的段落（姓名、住址、身份证号、工资数等） |
| 检索（含向量 `dense_query`） | 只发送问题本身 | 合同正文、条款片段、结构化事实 |
| 追问 / 问答 | 只发送问题 + 本次核对的结构化事实（已抽字段、规则命中、条款段落号与等级、命中法条） | 合同正文、条款片段原文 |

- 抽取环节发送正文这件事界面有明确提示；条款审查只发候选邻近段落，因此模型看不到全文，它自己"额外发现"的问题会相应变少——这是隐私优先的取舍。
- 账号、企业、合同、任务、日志存本地 SQLite（默认工作目录下的 `mla.sqlite3`，可用 `MLA_DB` 覆盖；6 张表：users / enterprises / contracts / tasks / logs / settings）；一个账号绑定一个企业，任务删除为软删、日志留痕。设置页保存的模型 Key 也在这张 `settings` 表里，明文落库、不写日志，随数据库文件一起备份或迁移。

## 免责声明

本系统只提示"与某条法条存在冲突"并给出可定位的依据（法条编号、合同原文片段），不判定任何条款违法或无效，不构成法律意见，也不替代法律意见。正式决策请咨询执业律师或当地人力资源社会保障部门。

## 参与贡献

- 开发环境、门禁命令、改数据（规则 / 触发器 / 评测集）的硬规矩与 PR 要求：[CONTRIBUTING.md](CONTRIBUTING.md)
- 报告安全漏洞请勿开公开 Issue，按 [SECURITY.md](SECURITY.md) 的渠道联系

## 版本

当前版本见 [Releases](https://github.com/ytongyaop/guardrailed-legal-agent/releases) 与 [CHANGELOG.md](CHANGELOG.md)，版本号遵循语义化版本。

## English

A labor-contract risk review tool for small and micro businesses that have employees but no in-house legal staff. It checks a contract along three independent channels: 20 deterministic rules for numeric limits (probation length, overtime rates, non-compete terms), 33 clause triggers that recall suspicious wording for model review with six programmatic checks, and a coverage check against the nine mandatory clauses required by Article 17 of the Labor Contract Law.

Every finding is bound to a statute identifier and must resolve to a real article in the bundled 143-entry statute set; suggestions without a citation are not generated. Rule verdicts use four states (violated, compliant, not applicable, undetermined) so that a missing fact is never silently reported as compliant.

Evaluation data is split into a regression set (used to detect regressions, built alongside the rules) and a held-out set (never used for tuning, used to report capability), with Wilson 95% confidence intervals. Results should be read as development-set numbers, not as generalization claims.

Requirements: Python 3.11+. Install with `pip install -e ".[ui,files]"`, then run `streamlit run src/mla/ui/app.py`; the first launch asks you to create an administrator account. Tests run fully offline (`pytest`).

Not legal advice. The tool reports conflicts with specific statutory provisions and points to the text; it does not determine whether a clause is invalid.

## License

MIT，见 [LICENSE](LICENSE)。
