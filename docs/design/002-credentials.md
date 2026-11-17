# 002：密钥存储与运行期数据路径重构规格

- 状态：设计已确认，实施中；本文件只描述规格，不代表功能已经实现。**本版对应 0.3.0**：按第 12 章五个阶段实施，每个阶段独立验收；阶段四为破坏性步骤，执行前需单独确认。提交与发布按项目发布流程执行。
- 代码基线：本仓库（公开发布），当前发布版本 `0.2.1`（`src/mla/__init__.py`、`git describe` 一致）。本文件中的源码行号均指该仓库，且已按 v0.2.1（`7dab9ca`）核对；此后 P0-P5 改动使行号整体偏移，实施时以内容定位为准，不要按行号机械套补丁。
- 设计文档：`docs/design/002-credentials.md`。评审时核对发现相关代码并非完全同版，实施时不得按行号机械套补丁。
- 现状基线：任务提供的测试数为 442 条（该数字为密钥重构规格评审时的基线；后续 UI 修复后为 485 条，见 003）。当前实测 676 条（no_llm 319 / mock_llm 183），以 `pytest --collect-only` 为准。本仓库根目录旧 `mla.sqlite3` 的 `settings` 表只有 `llm_api_key` 一个键、1 行、值长度 35，`users` 表有 3 个账号（本机只读核对结果）。本文不记录、不复述任何密钥值。
- 现状基线（来源实测）：该仓库 **没有任何 `.streamlit/secrets.toml`**（全仓递归查找 0 命中；`.streamlit/` 下只有受 git 管理的 `config.toml` 与 `secrets.toml.example`）。也就是说旧版界面文案里的“secrets 优先”在当前部署中从未有过实际数据。据此本版**彻底移除 LLM Key 的 secrets 来源**，最终只保留两级（见第 4 章）。
- 技术约束：Windows 为主、Python 3.11+、保留跨平台兜底；不新增第三方依赖，不引入 keyring、keytar、cryptography。

## 1. 问题陈述

### 问题一：同一个 Key 有多套解析实现，UI、CLI、配置层优先级不一致

- `src/mla/config.py:148` 只合成 `MLA_LLM_API_KEY` 与 `.streamlit/secrets.toml`（后者由 `src/mla/config.py:136-138` 的 `_secrets_api_key()` 读取）。
- `src/mla/ui/app.py:107-153` 独立维护 `secrets.toml > 数据库 > 环境变量`，并自行生成来源标签（`_secrets_llm_key()` 在 `src/mla/ui/app.py:113-119`）。
- `src/mla/cli.py:33` 重复定义 `LLM_KEY_SETTING`；`src/mla/cli.py:51-67` 独立读 SQLite，`src/mla/cli.py:128-132` 再把配置层结果与数据库结果拼接。
- 因此同一台机器上，界面显示“已配置”不代表 CLI 或服务层能取得同一个值；出现多处来源时，UI 与 CLI 还可能选择不同值。
- 补充事实：旧版里唯一真正**能写**的地方是数据库（`src/mla/ui/app.py:1574,1578`）；`secrets.toml` 只有读取、没有任何写入代码，且当前部署根本没有该文件。一条谁都写不进去、又排在最前面的来源，是纯粹的“配了不生效”后门。本版据此直接删除该来源，不再作为兼容层保留（理由见第 4 章）。

### 问题二：凭据与业务数据混存，写入路径只掌握在界面手里

- `src/mla/adapters/persist.py:73-75` 定义通用 `settings` 表，`src/mla/adapters/persist.py:652-666` 提供明文读写。
- `src/mla/ui/app.py:1553-1579` 将 `llm_api_key` 写入、清空业务数据库；CLI 只能读，不能完成同样的设置与清除。
- 数据库同时装账号、企业、合同、任务和日志。复制数据库用于备份或排障时会顺带复制 Key，凭据生命周期无法独立于业务数据管理。

### 问题三：数据库默认绑定当前工作目录，造成“换种启动方式就丢 Key、丢账号”

- `src/mla/config.py:27-29` 的 `_cwd_path()` 直接使用 `Path.cwd()`；`src/mla/config.py:104` 和 `src/mla/config.py:183` 把默认数据库定位到启动目录下的 `mla.sqlite3`。
- 实测旧库中已有长度 35 的 Key 和 3 个账号；换工作目录启动会打开或新建另一个数据库，原记录自然不可见。这是“Key 没了”的根因，而不是 Key 被意外清空。

### 问题四：向量缓存同样绑定当前工作目录

- `src/mla/config.py:61-62` 与 `src/mla/config.py:162` 将 `index_dir` 默认设为当前目录下的 `data/index`。
- 换目录会分散缓存、触发重建；将来到安装包或只读安装目录运行时，也不能把可写缓存的位置寄托于启动目录。

### 问题五：服务层与模型适配器存在绕过 UI/CLI 兜底逻辑的隐藏入口

- `src/mla/services/tasks.py:42-45` 在未显式传入模型配置时直接使用 `load_config().llm`，读不到只存在于数据库的 Key。
- `src/mla/adapters/llm.py:28-29` 允许 `DeepSeekLLM()` 无参构造，并再次回退到 `load_config().llm`。
- 评测脚本也直接读取 `cfg.llm.api_key`：`scripts/run_eval.py:228-231`、`scripts/eval_qa.py:64-68`、`scripts/eval_clauses.py:810-823`。
- 所以仅修 UI 的 `llm_key_sources()` 不能形成全局一致行为；必须有一个所有调用方共用的解析入口，并封住无参模型的旁路。

## 2. 目标与非目标

### 目标

完成一次可验证的存储与解析收敛：

1. 管理员保存一个全员共用的 DeepSeek Key，持久化到用户级 `auth.json`；UI、CLI、服务层和评测脚本经同一 resolver 取得相同值与来源标签。
2. 默认数据库与向量缓存移到同一用户级运行目录；未设置路径覆盖变量时，从不同工作目录启动仍读到同一份数据。
3. 首次运行能从旧数据库迁移既有 Key，并兼容旧数据库中的账号与业务数据；迁移失败必须可见，不能静默显示“已配置”。
4. 凭据文件采用原子写入、收紧权限、损坏显式报错、清除后不反复回迁；任何日志、异常、列表输出均不包含 Key。
5. 全程只使用 Python 标准库、操作系统自带能力与项目现有依赖。
6. 设置页是全项目唯一的 Key 写入入口，形状为“服务商下拉 + 掩码输入 + “测试并保存”+ 状态行”；**保存动作内置连接校验（`GET /models`），校验不通过不落盘**；保存后 Key 不可查看，界面只答“是否存在 / 是否有效”，且状态必须立刻可见，不允许“静默输入”（见第 10 章）。

### 非目标

- 不做 per-user Key，不做应用内多用户各自凭据，也不做 OAuth、命令动态取 Key、系统钥匙串或云密钥管理。
- **不做按模型存 Key**：Key 按服务商（provider）存，同一服务商下的多个模型共用一把 Key；本版只有 DeepSeek 一家但数据模型按 provider 键名预留扩容（见第 10 章）。
- 不实现 PyInstaller 安装包、FastAPI 或 Vue；本次只保证路径与 resolver 设计可被这些后续形态复用。**已确认方向：项目后续会迁到 FastAPI 形态**，届时不存在 `st.secrets`，因此本版不再把 `secrets.toml` 当作任何一层来源。
- 不改变 DeepSeek 作为唯一对话模型提供方的现状。
- 本版不迁移 embedding 的实际读取行为，只为其在 JSON 数据模型中保留 provider 扩位。
- 不合并多个历史数据库，不全盘扫描旧目录，不自动删除旧的 `.streamlit/secrets.toml`（文件可以留着，程序不再读取它，见第 9 章）。
- 不为假想需求增加加密、远程同步、凭据轮换、复杂锁服务或新包。

### “不加密”是明确选择，不是遗漏

本版采用与所参考开源实现一致的**用户级目录 + 明文 JSON + 权限收紧**。威胁模型是：

- 防止把仓库、业务数据库或排障副本误拷、误分享、误提交时一并带走当前 Key。
- 防止其他普通操作系统用户通过默认宽松继承权限直接读到凭据文件。
- 不防同一操作系统用户下的任意进程、本机管理员、SYSTEM、已控制用户会话的恶意软件读取明文。
- 不防用户本人主动复制或分享 `auth.json`；界面与文档必须把它标为敏感文件。

在同一用户威胁模型下，把加密材料和密文放在同一运行环境并不能达到所宣称的防护，却会增加依赖与恢复成本，所以本版明确不加密、不使用 keyring。

### 参考实现边界

- opencode Auth 模块：统一 `get/all/set/remove`，集中写用户级 `auth.json`，写入时请求 `0600`，并提供 `auth list/login/logout`：  
  `https://github.com/anomalyco/opencode/blob/51e310c9/packages/opencode/src/auth/index.ts`  
  `https://github.com/sst/opencode/blob/4695e685/packages/opencode/src/cli/cmd/auth.ts`
- pi 的 `AuthStorage`：provider 到凭据对象的映射、`api_key` 形状、用户级 `auth.json`、权限模式与统一存取：  
  `https://github.com/badlogic/pi-mono/blob/351efc82/packages/coding-agent/src/core/auth-storage.ts`
- 本项目只借上述存储形状与单一入口思想；环境变量优先级按本项目既有 CI/测试契约调整，不声称逐行复制任何项目。
- 用户提到的 “dsh” 项目名称尚未确认：待用户提供链接；若其也是同类明文 JSON + 单一入口实现，再补为参考，不阻塞本规格评审。

## 3. 数据模型

### 用户目录选择

默认根目录命名为 `MLA_HOME` 解析结果：

| 平台或覆盖方式 | 规则 |
| --- | --- |
| 显式覆盖 | `MLA_HOME` 指向根目录；测试与便携部署优先使用。新变量要求使用绝对路径，避免重新引入 cwd 依赖 |
| Windows 默认 | `%LOCALAPPDATA%\mla\`；若系统未提供该变量，回退到当前用户主目录下的 `AppData\Local\mla` |
| 非 Windows 默认 | 已设置 `XDG_DATA_HOME` 时用 `$XDG_DATA_HOME/mla`，否则用 `~/.local/share/mla` |
| 无法确定用户目录 | 抛出安全配置错误；不得回退到仓库或当前工作目录 |

**开放问题建议：选 `%LOCALAPPDATA%`，不选 `%APPDATA%`。** 这是单机、单企业的本机凭据、数据库与向量缓存，不要求跟随域账号漫游；避免 Key 和可重建缓存被复制到其他机器，也避免漫游增大登录同步负担。代价是同一域用户换电脑需要重新配置，符合本项目场景。

默认布局：

```text
<MLA_HOME>/
  auth.json                 # 当前唯一持久凭据文件
  auth-migration.json       # 只记录迁移状态，不含 Key
  auth-status.json          # 本机校验历史（是否有效 + 时间戳），不含 Key
  mla.sqlite3               # 默认业务数据库
  index/                    # 默认向量缓存目录
```

`auth-migration.json` 只存形如 `{"legacy_llm_api_key": "complete"}` 的状态。它不是第二份凭据，不写旧库路径、用户名或 Key。`auth-status.json` 属可丢的展示数据，与凭据的职责边界见下节。

### `auth.json` 形状

本版只写 `deepseek` 一个 provider，采用 pi 风格。**顶层键名就是 provider 标识，也是设置页服务商下拉的取值**：加第二家服务商只需在 `auth.json` 里多一个同形状的顶层键，存储结构不变（见第 10 章“扩容路径”）。

```json
{
  "deepseek": {
    "type": "api_key",
    "key": "<由管理员填写>"
  }
}
```

约束：

1. 文件为 UTF-8 JSON 对象；provider 是顶层键，凭据对象包含 `type` 与 `key`。本版不接受把 Key 直接写成字符串。
2. 保存时去除输入首尾空白；空输入不写入。清除操作删除 `deepseek` 条目，而不是保存空字符串。
3. 读改写时保留其他 provider 的原始条目，便于以后加入 `aliyun` embedding 凭据；本版 resolver 不读取、不写入 `aliyun`，现有 `DASHSCOPE_API_KEY` 与 `[embedding].api_key` 行为不变。
4. 顶层不是对象、JSON 无法解析、或 `deepseek` 条目形状错误时抛出不含 Key 的 `CredentialError`；不得当成空文件覆盖，也不得悄悄退回“未配置”。
5. 写入采用同目录临时文件、刷盘、原子替换和写后重读校验。任何临时文件都执行与正式文件相同的权限收紧，失败时清理临时文件并保留旧的可用版本。
6. `ResolvedKey` 等含 Key 的对象必须禁止在 `repr`、异常和日志中展开 Key；`list` 类接口只返回 provider、类型、来源标签和路径。

### `auth-status.json` 形状（本机校验历史，不含 Key）

与 `auth.json` 同目录：`<MLA_HOME>/auth-status.json`。**只存元数据，不含 Key、不含 Key 长度、不含任何 Key 片段**：

```json
{
  "deepseek": {
    "ok": true,
    "checked_at": "2026-11-13T15:20:11+08:00",
    "kind": "ok"
  }
}
```

约束：

1. 顶层键是 provider 名（与 `auth.json` 同一套键名，本版只有 `deepseek`）；条目只含三个字段：`ok`（布尔）、`checked_at`（ISO-8601 带时区偏移）、`kind`（`ok` / `auth` / `network` / `server`，与第 10.9 节三行固定文案一一对应）。
2. **写入时机**：一次针对**当前生效 Key** 的 `GET /models` 校验结束之后写——成功写 `{"ok": true, "kind": "ok"}`，失败写 `{"ok": false, "kind": <分类>}`，写的是“本次结果”，不保留“历史最好结果”。“测试并保存”里被校验的是**尚未落盘的新 Key**：只有校验通过且 `set_key()` 成功后才写 `ok: true`；校验失败不写该文件（旧 Key 仍生效，旧时间戳照旧，见 10.8 第 3 条与 AC-18）。从未发起校验时该文件不存在，**不预置空文件**。
3. **不是凭据来源**：resolver（第 4 章）从不读取它；删除它不改变任何解析结果，也不影响保存与清除；它也不能反向证明某把 Key 可用——校验通过与否由本次请求决定，不由这个文件决定。
4. **损坏按“未校验”降级，不报错**：文件缺失、JSON 无法解析、条目形状错误或时间戳格式非法时，一律当作“没有校验记录”（状态行回落为 `已配置 · 未校验`），不得抛 `CredentialError`、不得阻断启动、保存或核对页。这与 `auth.json` 损坏时“显式报错”的口径相反，因为它是可丢、可重建的展示数据。
5. 写入复用 `adapters/auth_file.py` 的同目录临时文件 + 原子替换 + 权限收紧；但**写入失败只降级显示**（本次状态行不显示时间戳），不得因为写它失败而让一次已经成功的 `set_key()` 报错或回滚。

`auth.json` 与 `auth-status.json` 的职责边界：

| 文件 | 内容 | 可移植 | 丢失后果 | 形状由谁对齐 |
| --- | --- | --- | --- | --- |
| `auth.json` | 唯一持久凭据（provider → `{type, key}`） | 可；换机只需要带走这一个文件 | 必须重新填写 Key | 对齐 pi（第 2 章参考实现） |
| `auth-status.json` | 本机校验历史（provider → `{ok, checked_at, kind}`） | 不必；换机后理应重新校验 | 状态行显示“未校验”，不影响能否使用 | 本规格自定义，不对齐 pi |

### 共享与扩位

当前语义是“一个 Windows 操作系统用户运行的一份 MLA_HOME，对应一个企业共用 Key”。应用内普通用户不能查看或修改，只有管理员可保存、清除。

数据模型不把用户 ID 硬编码进 provider 条目。若将来确需 per-user Key，可在不同应用用户命名空间下放置同形状的独立凭据文件，并给 resolver 增加主体参数；本版不创建这些目录、不实现该分支，只避免把当前 JSON 改成无法扩展的单字符串。

## 4. 解析优先级表

最终版只保留一个 resolver，按下列顺序返回第一个非空值。**只有两级来源**：

| 顺序 | 来源 | 来源标识 | 界面/CLI 来源标签 | 语义 |
| --- | --- | --- | --- | --- |
| 优先 | `MLA_LLM_API_KEY` | `environment` | `环境变量（MLA_LLM_API_KEY）` | 临时覆盖、CI、离线测试；绝不自动写入文件 |
| 其次 | `<MLA_HOME>/auth.json` 的 `deepseek` | `auth_file` | `用户凭据文件（auth.json）` | 管理员保存的全员共用持久 Key |
| 都无 | 空值 | `none` | `未配置` | 不生成处理建议，生成式问答按现有边界拒答 |

**`.streamlit/secrets.toml` 的 `[llm].api_key` 不是来源。** 本版删除这条来源，代码不再读取它；即使文件存在、里面有值，也按“没配”处理（理由见下）。界面上不得再出现“secrets.toml”这个来源标签。

迁移期兜底与最终状态的区别必须写清：

- 阶段三为“尚未迁移的老用户不突然无 Key”，可临时保留最低优先级的旧数据库只读兜底（`legacy_database` / `兼容来源（旧数据库）`），排在 `auth_file` 之后。
- **该兜底在阶段四删除**：完成首次迁移并清掉旧库 `llm_api_key` 行后，来源标识与相关代码一并移除，最终版只剩上表两级。任何阶段都不得把旧数据库当作长期第三级来源。
- 旧 `secrets.toml` 在任何阶段都不是候选：本版没有“从 secrets 迁移”这一步（当前部署也没有该文件，见文件头“现状基线（来源实测）”）。

### 为什么选择环境变量优先

pi 可以让已存凭据优先于环境变量，但本项目已有 `MLA_LLM_API_KEY` 覆盖、测试屏蔽与 CI 使用方式。环境变量最高能让一次启动的显式覆盖始终生效，也避免开发者本机 `auth.json` 意外影响测试。与旧 UI 的“环境变量最后”相比这是**有意行为变更**，必须在发布说明和来源标签中显式呈现。

### 为什么删掉 secrets.toml 来源

三条理由，缺一不可：

1. **写入来源唯一**：旧版只有数据库那条路线真的会写（`src/mla/ui/app.py:1574,1578`），`secrets.toml` 只有读取。保留读不写的来源，等于让“优先级”表里有一格永远由用户手工维护、程序无法保证其与保存动作一致。
2. **`st.secrets` 随 FastAPI 迁移消失**：已确认项目后续迁到 FastAPI 形态。届时没有 Streamlit 运行期，`st.secrets` 与 `secrets.toml` 的读取代码都得删；现在保留只是把同一次删除推迟，并把迁移候选顺序多留一套分支。
3. **避免“配了不生效”的后门**：三级来源里 secrets 排最高，会导致用户明明在设置页保存成功、环境变量也设了，却因一份残留 secrets 被静默盖过；而实测该仓库**没有** `.streamlit/secrets.toml`，删掉它不会造成任何现存用户的迁移缺口。

代价与边界：`.streamlit/secrets.toml.example` 这个受 git 管理的模板文件仍在，**程序行为上本版不动它**，但它的 `[llm]` 段属于阶段五必做清理项（删除 `[llm]` 段、新增 `[embedding]` 示例段、注释改为指向设置页、文件本身保留，6 条具体步骤见第 9 章与第 12 章）；embedding 的 `[embedding].api_key` 仍由 `src/mla/config.py:156-157` 读取，**本版只移除 LLM Key 一侧的 secrets 读取**，向量检索行为不变。

### 错误与空值规则

- 文件不存在不是错误，继续解析低优先级来源；只读解析不得顺手创建 `auth.json` 或数据库。
- 文件存在但损坏、目标 provider 形状错误或权限校验失败，是显式配置错误；不得伪装成文件不存在，也不得用低优先级来源掩盖错误。
- 即使环境变量足以运行，启动时的迁移或凭据文件错误也要在 UI/CLI 留可见诊断；可以允许本次环境变量继续工作，但不能显示“迁移成功”或“凭据文件正常”。
- 所有来源只返回 Key 与元数据，**resolver 自身不做网络探测**。联网校验只发生在两处：设置页/CLI 的**保存动作内置校验**，以及已配置后手动“测试连接”复验（第 10.3、10.8、10.9 节）。校验失败不改变文件、不改变解析结果。
- UI 不缓存凭据解析结果；保存、清除或外部修改后，下一次解析应重新读文件。CLI 在一次进程内复用同一结果即可。

## 5. 运行期数据路径

### 新默认与覆盖关系

`src/mla/config.py` 负责纯路径计算，不读凭据：

```text
MLA_HOME       -> 用户级根目录
MLA_DB         -> 完整数据库路径；未设置时为 <MLA_HOME>/mla.sqlite3
MLA_INDEX_DIR  -> 完整缓存目录；未设置时为 <MLA_HOME>/index
```

- `MLA_DB`、`MLA_INDEX_DIR` 各自覆盖最终路径，优先级高于 `MLA_HOME` 派生值；保留它们是兼容与测试隔离所需。
- 显式 `MLA_DB` 不改变 `auth.json` 的位置。凭据始终由 `MLA_HOME` 决定，避免“换数据库就换一套管理员 Key”。
- 既有显式相对 `MLA_DB` / `MLA_INDEX_DIR` 暂按原语义支持并记录警告；新 `MLA_HOME` 必须为绝对路径。新文档与安装配置一律推荐绝对路径。
- 法源、规则、条款、评测集等只读包数据继续走 `data_path()`，不移动到 `MLA_HOME`。
- 数据库父目录与缓存目录只在实际使用时创建；单纯查询 Key 不应创建业务数据库。

### 对已有用户的兼容策略

未显式设置 `MLA_DB` 时，在创建新库前执行一次性旧库引导：

1. 若 `<MLA_HOME>/mla.sqlite3` 已存在，直接使用它，绝不与旧库合并。
2. 若新库不存在，且当前工作目录存在旧默认 `mla.sqlite3`，用 Python 标准库 `sqlite3.Connection.backup()` 将旧库一致性复制到新路径，再打开新库。旧库文件保留，账号、企业、合同、任务、日志不做表级合并。
3. 若显式 `MLA_DB` 指向旧库，尊重该路径，不自动搬迁数据库；仍允许凭据迁移从该库读取旧 Key。
4. 若新库已经存在，同时当前目录又有旧库，以新库为准；旧库只在凭据迁移尚未完成时作为旧 Key 候选，不做业务数据覆盖。
5. 不扫描其他目录。用户曾在另一个工作目录使用旧版，需要在该目录启动一次新版完成复制，或继续通过 `MLA_DB` 显式指向那份库；界面和发布说明要给出这一恢复办法。
6. 旧 `data/index` 不自动搬运。缓存可重建，旧目录保留；新缓存只在 `<MLA_HOME>/index` 或 `MLA_INDEX_DIR` 生成。

本仓库根目录现有旧库含 3 个账号，兼容验收必须证明复制后账号数不变，而不是只验证 Key。真实库演练只能在副本上进行。

### 测试隔离

**开放问题建议：测试必须同时隔离 HOME、DB、INDEX、环境变量与 cwd，不能只屏蔽环境变量。**

`tests/conftest.py:18-27` 的 autouse fixture 在任何配置解析或 AppTest 启动前：

- 将 `MLA_HOME`、`MLA_DB`、`MLA_INDEX_DIR` 全部指向该用例的 `tmp_path`；
- 继续强制 `MLA_EXTRACT_MODE=regex`，删除 `MLA_LLM_API_KEY` 与 `DASHSCOPE_API_KEY`；
- 保留现有的 `mla.config._secrets_section` 打桩（`tests/conftest.py:23`），但**只服务于 embedding**：LLM 解析路径本版已不读 `secrets.toml`，这条桩防的是 `[embedding].api_key` 把真实向量 Key 带进来；不得再为 LLM 保留“屏蔽 secrets”的单独逻辑；
- 把旧版 cwd 发现所读的目录切到该用例的空临时目录；
- 对 CLI 子进程显式传递同一组隔离环境，不能依赖开发者进程环境；
- 保留现有 `db_path` 注入用例，显式路径优先于 autouse 默认值。

这样可保证 676 条现有离线测试及新增用例都不会读取、迁移或改写开发者真实 `auth.json` 和 `mla.sqlite3`，也不会因本机保存过 Key 而联网。

## 6. 单一入口设计

### 模块分层

新增一个公开 resolver 模块 `src/mla/services/credentials.py`，负责来源优先级、来源标签、面向调用方的读写 API；新增私有文件适配器 `src/mla/adapters/auth_file.py`，负责 JSON 校验、原子写入与平台权限。UI、CLI、服务、脚本只能 import service，不各自解析文件。

数据库复制与首次迁移由 `src/mla/services/bootstrap.py` 负责。它可以调用凭据 service 的存储 API，但不得再发明一套 Key 优先级。`domain` 不感知文件、环境变量或 SQLite。

建议公共契约：

```python
@dataclass(frozen=True)
class ResolvedKey:
    key: str = field(repr=False)
    source: Literal["environment", "auth_file", "none"]
    label: str
    origin: str = ""


def resolve_key(
    provider: str = "deepseek",
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ResolvedKey: ...


def resolve_llm_config(
    cfg: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[LLMConfig | None, ResolvedKey]: ...


def set_key(provider: str, key: str, *, home: Path | None = None) -> None: ...
def clear_key(provider: str, *, home: Path | None = None) -> None: ...
def list_key_status(*, env: Mapping[str, str] | None = None, home: Path | None = None) -> list[KeyStatus]: ...
```

- `resolve_llm_config()` 返回 `(None, ResolvedKey.none)` 表示未配置；有 Key 时返回已注入 Key 的 `LLMConfig` 与来源信息。
- `env`、`home` 是可测试性注入点；生产默认取进程环境与标准用户目录。
- `set_key()` 成功返回前必须完成权限设置和重读校验，否则抛 `CredentialError`，调用方不得显示“已保存”。
- `list_key_status()` 的结果不含 Key，也不含 Key 长度；UI 与 `mla auth list` 只能展示来源、provider、类型和敏感文件路径。
- **UI 约束（与第 10 章配套）**：`set_key()` / `clear_key()` 是唯一的写入函数，设置页与 `mla auth set/remove` 都调它，任何调用方不得自行写 `auth.json`；设置页状态行只通过 `list_key_status()` 渲染，渲染器不得接受 Key 参数，从接口上就做不到把 Key 或长度显示出来。测试断言方式：对状态行渲染函数检查其入参类型不含 `str` 形式的 Key，并断言 AppTest 输出的整段文本里不含哑 Key 的任何前缀。
- **校验历史（第 3 章 `auth-status.json`）也由 service 负责**：读写各走一个函数（例如 `record_check_status(provider, ok=..., kind=..., checked_at=...)` / `read_check_status(provider)`），调用方不得直接读写该 JSON 文件。`set_key()` / `clear_key()` 不擦写它——清除 Key 后旧记录可以留着，状态行因为“未配置”自然不显示时间；`list_key_status()` 仍只返回存在性、来源、类型与路径，**不返回 `checked_at`**，避免把“校验历史”和“是否存在凭据”混成一个返回值。

### 必须改动的运行时代码调用点

| 文件与行号 | 改动要求 |
| --- | --- |
| `src/mla/config.py:124-133` | 保留 `_secrets_section()`，但**只服务 embedding**；不得再供 LLM 使用 |
| `src/mla/config.py:136-138,148` | 删除 `_secrets_api_key()`（LLM 一侧的 secrets 读取）与 `api_key=e.get("MLA_LLM_API_KEY", ...)` 里的 secrets 合成；`load_config()` 只装非秘密模型参数，`LLMConfig.api_key` 保留为显式注入字段但默认必须为空。改完全仓不得再有 LLM Key 的 secrets 读取 |
| `src/mla/config.py:156-157` | embedding 旧读取暂留，避免把本版范围扩大到向量服务 |
| `src/mla/config.py:61-62,162,183` | 数据库与缓存默认改为 `MLA_HOME` 派生，保留显式覆盖 |
| `src/mla/ui/app.py:107-153` | 删除本地 `LLM_KEY_SETTING`、来源元组、优先级和标签实现（含 `src/mla/ui/app.py:113-119` 的 `_secrets_llm_key()`），改为 resolver；保留“各来源不预先混合”的设计意图 |
| `src/mla/ui/app.py:739,838,1047,1099,1470,1582` | 全部 `_resolve_llm()` 调用改走统一 `resolve_llm_config()`，不得漏传 repo 才能读 Key 的隐性条件 |
| `src/mla/ui/app.py:1553-1600` | 管理员保存/清除改写 `auth.json`；显示统一来源标签、迁移/权限错误；文案不再说“保存到本机数据库” |
| `src/mla/ui/app.py:1877-1879` | 在创建 repository 前完成运行目录与旧库引导 |
| `src/mla/cli.py:33,51-67,113-133` | 删除数据库专用兜底和重复常量，`cmd_check` 使用统一 resolver |
| `src/mla/cli.py:265-312` | 注册 `auth` 子命令；所有 CLI 命令在取默认路径前经过统一引导 |
| `src/mla/services/tasks.py:36-45` | 未显式传入 `llm` 时调用 resolver；显式传入仍用于测试与已解析配置 |
| `src/mla/adapters/llm.py:28-29` | 取消无参时自行读 `load_config().llm` 的旁路，要求调用方传入已解析 `LLMConfig` |
| `src/mla/adapters/llm.py:56-60,67-77` | **错误结构化（口径已写死）**：①`health()` **保持返回 `bool`、签名不变**（`src/mla/adapters/llm.py:67-77`；`src/mla/ui/app.py:1586` 与 `tests/test_llm.py:141,151` 的 `is False` 断言不动），内部可以复用新探测，但返回值语义不变；②**新增** `probe_models(key: str) -> ProbeResult`，返回结构化结果 `kind ∈ {"ok","auth","network","server"}` + `status_code`（HTTP 失败时为响应的状态码，网络类为 `None`）+ 可选 `message`（只放不含 Key 的诊断），供第 10.9 节三条固定文案映射；③保存校验与手动“测试连接”**只调 `probe_models()`，不调 `health()`**；④分类只取自捕获到的 `urllib.error.HTTPError.code` 与 `(urllib.error.URLError, TimeoutError, socket.gaierror)`，**禁止字符串匹配或中文关键字判类**；⑤推理、抽词、问答三条既有路径的 `LLMError` 语义与 `except LLMError` 处理一律不变 |
| `scripts/run_eval.py:228-231` | 用统一 resolver 决定是否构造模型 |
| `scripts/eval_qa.py:64-68` | 用统一 resolver 判断未配置并构造模型 |
| `scripts/eval_clauses.py:810-823` | 用统一 resolver 决定模型臂与展示状态 |
| `src/mla/adapters/persist.py:73-75,652-666` | 通用 settings 表和方法暂留；删除凭据专用业务语义，不由正常解析路径调用 |

`DeepSeekLLM` 构造契约收紧属于内部调用方的破坏性改动，必须随全仓搜索一起完成，不能只修 UI 和 CLI。

错误结构化必须**只增不改**既有异常语义，三条既有用法不得被这次改结构破坏：

- `except LLMError` / `str(exc)` 仍要可读可用——推理（`generate()`）、抽词与问答路径继续按原样捕获，**一律不改**：不新增必填构造参数、不改变已抛出的异常类型，也不因为多了新探测方法而改变它们的处理分支；
- `health()` **保持返回 `bool`、签名不变**：`src/mla/ui/app.py:1586` 的 `probe.health()` 直接当布尔值用，`tests/test_llm.py:141,151` 断言 `health() is False`（严格 `is`）。`health()` 可以内部复用 `probe_models()` 的请求路径，但返回值与签名都不动；
- **`probe_models(key: str) -> ProbeResult` 是唯一的结构化出口**：`kind` 取 `ok` / `auth` / `network` / `server`，另带 `status_code` 与可选 `message`。它以**传参**方式接收要校验的 Key，因此能校验**尚未落盘的新 Key**，无需读 resolver；保存校验与“测试连接”只调它，`health()` 不再是校验入口；
- 分类判据只用异常类型与 `HTTPError.code`，写进 `auth-status.json` 的 `kind` 也只存枚举值，不存中文文案。

### CLI 开放问题建议

建议新增：

```text
mla auth list
mla auth set deepseek
mla auth remove deepseek
```

理由是与 opencode 的凭据子命令一致，让无界面启动、安装包排障和 CLI 用户也能完成管理闭环。`set` 使用标准库 `getpass` 隐藏输入，不接受把 Key 直接放在命令参数中，避免进入 shell 历史；`list` 不显示 Key；`remove` 只删除文件内对应 provider，**环境变量仍需用户自行管理**（本版没有其他可回落的来源）。

### 测试调用点

- `tests/conftest.py:18-27` 扩展为完整运行目录隔离（`_secrets_section` 打桩只服务 embedding）。
- `tests/test_cli.py:65-82` 的 SQLite Key 专用测试改为统一 resolver 与迁移测试。
- `tests/test_ui_setup.py:209-239,266-286,334-389` 重写为 `env > auth_file` 两级优先级、文件保存/清除、错误提示与第 10 章的状态行可见性。
- `tests/test_ui_setup.py:413,424,433` 现有的“侧栏无 Key 入口”“有 Key 才启用条款审查”“无 Key 时核对页不可用”用例保留并改为读 `auth.json` 场景，作为第 10 章唯一入口与可见变化的回归护栏（勾选框的组件级置灰由 `_clause_review_toggle(cfg, None)` 单测覆盖）。
- `tests/test_settings.py` 继续验证通用 settings，不把所有键值存储能力误删。
- 新增 `test_credentials.py`、`test_credential_migration.py`、`test_runtime_paths.py`、`test_cli_auth.py`，并更新服务与模型构造相关用例。

## 7. 迁移方案

### 触发时机与候选顺序

UI、CLI 和直接使用服务的入口在首次解析持久配置前执行 `ensure_runtime_ready()`。数据库复制与凭据迁移是两项独立步骤：前者保证账号和业务数据可见，后者只处理 `llm_api_key`。

凭据迁移只执行一次，且只从当前旧环境发现候选。**本版只有一个候选来源：旧数据库。**

1. 显式 `MLA_DB` 指向的旧库，或当前工作目录旧 `mla.sqlite3` 的 `settings.llm_api_key`；若本阶段已复制到新库，也要把新旧两处的旧 Key 清理纳入同一迁移结果。

`.streamlit/secrets.toml` 已不是候选：无论文件是否存在，迁移都不读取、不导入、不改写它（第 4 章给出了删除该来源的三条理由，并已实测当前部署没有该文件）。因此不存在“secrets 与数据库取值不同、升级后无故换 Key”的问题，也不需要保留旧的候选顺序。环境变量永不落盘，且仍然覆盖导入结果。

### 成功流程

1. 若 `auth-migration.json` 已标记 `complete`，不再读取旧库 Key；用户之后清除 `auth.json` 也不会触发旧值复活。
2. 若 `auth.json` 已有合法 `deepseek`，不覆盖它，直接转入旧库清理与标记完成。
3. 若没有文件凭据且发现非空旧候选，通过 `set_key()` 写入；随后重新解析 `auth.json`，在内存中确认值一致、来源为 `auth_file`，全程不打印 Key。
4. 写后校验成功才删除旧库 `settings` 中键为 `llm_api_key` 的行；若新旧库是两份文件，两处都检查并清理。其他 settings、表和旧库文件保留。
5. 旧库清理成功后原子写入 `complete` 标记。若标记写入失败，下次启动看到已有合法文件凭据后只做清理与补标记，不重复覆盖 Key。
6. UI 显示一次“已从旧配置迁移到用户凭据文件”及文件路径；CLI 在stderr显示不含 Key 的迁移结果。之后只显示正常来源标签。

### 没有发现旧配置

- 没有任何旧库时，按“未配置”处理，不创建空凭据，也不把这次启动永久标成已迁移；这样用户之后从真正的旧目录启动仍能完成迁移。存在 `.streamlit/secrets.toml` 但旧库无 Key，同样按“未配置”处理，不得把 secrets 当作旧候选。
- 找到了旧配置容器但其中没有 Key，可以记录已检查完成，避免每次启动重复读旧库。
- 新装用户直接在 UI 或 `mla auth set deepseek` 保存后，迁移逻辑不得再覆盖新值。

### 失败处理

| 失败点 | 必须行为 |
| --- | --- |
| 旧库无法读取 | 保留旧数据，显示“旧凭据读取失败，请重新保存 Key”及安全诊断；不得把空读取当成旧库无 Key 并标记完成 |
| `auth.json` 写入、权限收紧或重读校验失败 | 不写完成标记、不删除旧库 Key；UI/CLI 明确显示“凭据迁移失败，请重新保存 Key”，不得显示“已保存到凭据文件” |
| 旧库 Key 删除失败 | 已验证的新文件可继续使用，但显示“旧凭据尚未清理”警告，不写完成标记，下次重试清理；不得宣称迁移全部完成 |
| 标记写入失败 | 保留已验证的新文件，显示警告；下次幂等补标记，不重新导入或改变 Key |
| 文件已损坏 | 不自动重命名、不自动覆盖、不生成额外明文备份；管理员显式重新保存后才可替换 |

迁移失败时，环境变量可以按优先级维持本次运行（本版没有其他可回落的来源），但 UI/CLI 必须持续显示“迁移未完成”。如果唯一来源是旧数据库且导入失败，最终版不得静默把数据库当正常来源继续用，应提示管理员重新保存。

### 幂等与并发

- 同一进程或连续多次调用 `ensure_runtime_ready()`，结果必须一致；导入、删除旧行、写标记都可重复执行而不改变最终状态。
- 保存与迁移需要按 `MLA_HOME` 串行化。用标准库原子创建 `<MLA_HOME>/auth.lock/` 目录作为互斥；创建失败即明确提示另一配置操作可能正在进行，不得两个进程同时 read-modify-write 造成丢条目。正常结束必须在 `finally` 中释放。
- 锁目录内的 owner 信息只写进程标识与创建时间，不含 Key。启动时发现遗留锁要显示诊断，由用户确认没有运行中的配置操作后手工移除；不得仅凭等待时长自动判死并强行抢锁。

### 旧数据去留的开放问题建议

**建议迁移验证成功后删除旧库中的 `llm_api_key` 行，但保留旧库文件与全部其他数据。** 留着会让数据库副本继续泄露 Key，也给将来旧代码误读留下通道；删除的代价是不能靠直接降级恢复旧 Key，因此本步骤单独标为破坏性，发布说明必须写明“降级需重新填写 Key，业务数据不删”。旧 `.streamlit/secrets.toml` 本版已完全不读，程序不自动删改它；但若用户历史上曾把 Key 写进该文件，建议在迁移后用文本编辑器手工删除该条目，避免仓库副本或备份继续泄露（这是安全提示，不是迁移动作，不产生候选来源）。

## 8. Windows 权限

### 不把 `chmod 0600` 当成 Windows ACL

Python 官方文档说明，Windows 上 `os.chmod()` 只能影响只读属性，其他权限位被忽略。因此：

- 非 Windows 平台：创建用户目录时请求 `0700`，凭据文件与临时文件收紧到 `0600`，并检查实际模式。
- Windows：`0600` 可以保留为跨平台语义提示，但安全验收必须以 NTFS ACL 为准，不能检查 `st_mode` 后宣称完成。
- `%LOCALAPPDATA%` 的用户配置文件默认 ACL 通常能隔离其他普通用户，这是良好基础；但继承 ACL 可被系统策略或自定义 `MLA_HOME` 改变，所以不能仅凭路径判定“足够”。

### 本版的可行做法

在不增加 Python 依赖的前提下，使用标准库 `subprocess` 调用 Windows 自带的 `whoami` 与 `icacls`：

1. 解析当前操作系统用户，给凭据临时文件显式授予当前用户与 SYSTEM 所需权限，关闭继承，避免 `Everyone`、普通 Users 组或等价宽泛主体获得读取权限。
2. 先收紧并校验临时文件，再原子替换正式 `auth.json`；替换后再次校验。失败时恢复旧文件或清理新建文件，且保存操作必须报错。
3. 只修改凭据文件及其临时文件的 ACL，不递归修改用户目录，不修改整个 `MLA_HOME`，防止用户把覆盖变量指向已有目录后造成越权改动。
4. `icacls` 不存在、返回失败、文件系统不支持 ACL、或校验发现宽泛主体时，不声称保存成功。用户可改用环境变量，或选择支持 ACL 的本机目录。
5. 已存在的凭据文件若权限校验失败，文件来源标记为配置错误；环境变量可继续用于恢复运行，但界面必须提示修复，不能静默接受宽松权限。

SYSTEM 与本机管理员本就在威胁模型之外；显式 ACL 的目标是普通用户隔离和防止误继承，不是宣称能防御提权后的读取。

### 验证方法

- Windows 实测观察：对隔离测试目录中的 `auth.json` 执行 `icacls <路径>`，输出应只有预期主体，不应出现 Everyone、普通 Users 组或等价宽泛读取项；再用另一普通用户账号尝试读取应被拒绝。测试只使用哑 Key。
- 自动化：模拟 `icacls` 成功、失败和异常输出，断言失败不会显示保存成功且旧值仍可恢复；Windows 集成用例只在隔离临时目录运行。
- 非 Windows：在测试中读取实际模式并断言目录为 `0700`、文件为 `0600`；不能用 Windows 的只读属性替代这项断言。

## 9. closes 与废弃项

### `secrets.toml`（LLM 一侧彻底作废）

- `.streamlit/secrets.toml` 的 `[llm].api_key` **不再被任何代码读取**：`src/mla/config.py:136-138` 的 `_secrets_api_key()` 删除，`src/mla/config.py:148` 的 LLM `api_key` 合成删除，`src/mla/ui/app.py:113-119` 的 `_secrets_llm_key()` 删除。即使文件存在且有值，解析结果仍是 `none`。
- 该文件**可以保留，程序忽略**：不读、不写、不自动改名、不自动删除。`git` 中不存在 `.streamlit/secrets.toml`（只有 `.streamlit/secrets.toml.example`），所以本版不存在“删来源就丢 Key”的风险。
- `src/mla/config.py:124-133` 的 `_secrets_section()` 保留，但只被 `src/mla/config.py:156-157` 的 `[embedding].api_key` 使用；embedding 行为不变，不在本版迁移范围内。
- `.streamlit/secrets.toml.example` 仍受 git 管理、仍在引导用户“复制为 secrets.toml 并填入 `[llm].api_key`”，与本版口径直接冲突：本版已彻底移除 LLM 侧的 secrets 读取，按模板填进去的 Key 永远不生效。因此它被列为**本版阶段五必做项**（不是“可选文档清理”）。理由与第 4 章“为什么删掉 secrets.toml 来源”三条一致，核心是**不留“配了不生效”的入口**。具体步骤固定为下面 6 步（只改这一个模板文件）：
  1. 打开 `.streamlit/secrets.toml.example`。**此前两个代码副本实测均为 8 行**：6 行注释/空行 + `[llm]` 段 2 行（段头 + 一行 `api_key` 占位示例），**没有 `[embedding]` 段**；以实施时的实际内容为准，先看再改。
  2. **删除 `[llm]` 段整段**：段头 `[llm]` 与它的 `api_key` 行一起删，不留空段头、不留注释掉的示例行。
  3. **新增一个 `[embedding]` 示例段**：向量检索仍从 secrets 读 `DASHSCOPE_API_KEY` / `[embedding].api_key`（`src/mla/config.py:156-157`），删完 `[llm]` 若不再补这一段，模板会变成没有指引价值的空文件。示例段固定为：

      ```toml
      [embedding]
      api_key = "<在此填入向量检索 Key（可选）>"
      ```

      环境变量 `DASHSCOPE_API_KEY` 与这段等价，二选一，写在段外的注释里说明。
  4. 文件顶部注释改写为固定一句：`# 模型（LLM）的 API Key 请在应用“设置”页配置，不要写在本文件；本文件只保留向量检索（可选）的 Key。`；同时删掉三句已失效的旧文案（“复制为 secrets.toml 后填入自己的 Key”“界面上可把 Key 保存到本机数据库”“优先级低于本文件”）。
  5. **文件本身保留、不改名**：不删除、不改名、不新建第二个模板，`.gitignore` 对 `.streamlit/secrets.toml` 的排除项也不变——本版只是不再读取它，不是要求用户删文件。
  6. 验收：文件里**搜不到 `[llm]`**（`rg "\[llm\]" .streamlit/secrets.toml.example` 命中 0）、**能搜到 `[embedding]`**、注释含“设置页”；`git diff -- .streamlit/secrets.toml.example` 显示预期改动；AC-11 的“保存/清除 LLM Key 前后该文件与模板字节不变”继续通过（程序不改写任何 secrets 文件）；`rg "_secrets_api_key|_secrets_llm_key" src` 命中 0。

### 数据库

- `settings` 表及通用 `get_setting/set_setting` 保留，其他非秘密设置不受影响。
- `llm_api_key` 不再是正常解析来源；**阶段四**一次性迁移并验证后删除该行，UI 不再写空字符串代替清除。阶段三保留的旧库只读兜底也在阶段四一并删除。
- 旧库文件和其他业务数据保留；数据库复制不等同于删除旧库。

### 要删除或停止使用的代码

- `src/mla/ui/app.py` 的 `LLM_KEY_SETTING`、`llm_key_sources()`、`resolve_llm_key()`、`key_source_label()`、`_secrets_llm_key()` 及数据库保存/清空分支。
- `src/mla/cli.py` 的 `LLM_KEY_SETTING`、`_sqlite_api_key()` 和手工优先级拼接。
- `src/mla/config.py` 的 `_secrets_api_key()` 及 LLM `api_key` 的 secrets 合成；`_secrets_section()` 仅在 embedding 兼容需要时保留。
- `DeepSeekLLM` 无参构造时自行解析配置的旁路。
- 迁移完成后的 `legacy_database` 正常回退分支。
- 所有“Key 不写落盘”“Key 保存在本机数据库”“优先级 secrets.toml > 数据库 > 环境变量”的过时文案，统一改为“DeepSeek Key 以明文保存在当前操作系统用户的凭据文件中，已收紧权限；不要分享该文件”，来源标签只允许 `环境变量（MLA_LLM_API_KEY）` / `用户凭据文件（auth.json）` / `未配置` 三种。

### 不变项

- `MLA_LLM_API_KEY` 继续支持且优先级最高。
- 无 Key 时处理建议不生成、生成式问答拒答，不降级成模板回答。
- 应用内只有管理员能保存、测试和清除共用 Key；普通用户看不到模型设置区块。
- `[embedding].api_key` 与 `DASHSCOPE_API_KEY` 的读取、回退 BM25 行为完全不变。
- 不新增第三方依赖，不把任何真实 Key 写入规格、测试快照、日志、异常或版本库。

## 10. UI 交互：服务商选择与 Key 输入

本章是设计中新增的交互要求，目标是**不允许静默输入**：管理员填完 Key 并保存后，界面上必须能立刻看到变化，而不是“看起来没反应、不知道存没存上”。本章只描述界面契约；存储与解析仍以第 3、4、6 章为准。

### 10.1 唯一入口

- 全项目只有**设置页**一处可以写 Key（`src/mla/ui/app.py:1553-1600` 的模型设置区块，仅管理员可见）。
- 核对页、侧栏、登录页、结果页**不得**提供第二个输入框，也不得提供“临时输入 Key”之类的会话级入口（旧版已用 `tests/test_ui_setup.py:413` 断言侧栏不再有该入口，本版继续保持）。
- CLI 侧的 `mla auth set deepseek` 是同一套逻辑的第二个**调用方**，不是第二处 UI 入口；代码上它和设置页都调 `set_key()`（第 6 章）。

### 10.2 两层概念：provider 与 model（用户最容易混淆的地方）

| 概念 | 是什么 | 存不存 Key | 本版取值 |
| --- | --- | --- | --- |
| provider（服务商） | 提供模型 API 的厂商，如 DeepSeek | **存**，一个服务商一把 Key，写在 `auth.json` 的顶层键 | 只有 `deepseek`，下拉里只有一项 |
| model（模型） | 服务商下的具体模型，如 `deepseek-chat` | **不存**，共用所属服务商的 Key | 由 `MLA_LLM_MODEL` 决定，默认 `deepseek-chat` |

**明确不做“按模型存 Key”**，理由：同一服务商下的多个模型（`deepseek-chat` / `deepseek-reasoner` 等）共用同一把 Key；按模型存会让用户在两个模型名下重复填同一串字符，既无意义又制造“哪个才是生效的”歧义。所以模型只做**展示**，不做选择器、不单独要 Key。

### 10.3 控件形状（设置页“模型设置”区块内）

本版控件清单（未配置态与已配置态共用同一套元件，区别只在展开状态与状态行，见 10.8）：

1. **服务商下拉**：标签“服务商”，当前只有 `DeepSeek` 一项。选项值直接来自 `auth.json` 允许的 provider 键名，而不是硬编码显示名；本版下拉只有一项时也保留下拉控件，让“以后会加第二家”这件事在界面上是可预期的。
2. **Key 输入**：掩码输入（`type="password"`），**输入期**允许“显示/隐藏”切换，便于确认粘贴是否完整（输入框里只会有用户本次粘贴的字符，DeepSeek Key 约 35 字符）。规则：空输入不提交，提交前 `strip()`；**仅输入期可查看**——保存成功后输入框立即清空，**不回填任何已保存的 Key**，不显示字符与长度；重新打开页面时输入框为空，已保存的 Key 永不回显。
3. **主按钮“测试并保存”**：唯一的写动作，内部顺序固定为 ①按当前输入的 Key 发起 `GET /models` 校验 → ②校验通过才调 `set_key("deepseek", ...)` → ③落盘并重读校验成功后才显示成功提示。校验不通过时不写 `auth.json`、不显示“已保存”，按 10.9 分类报错。**不再存在独立的“保存”按钮**，保存与校验是同一个动作的两步。
4. **已配置状态行**：`已配置 · 有效（<上次校验时间>）· 来源：<标签> · 服务商：DeepSeek · 模型：<模型名>`。
5. **未配置状态行**：`未配置 · 服务商：DeepSeek`。

补充约束：

- 状态行只回答两件事：**是否已配置**（存在性，来自 `list_key_status()`）与**是否有效**（最近一次 `GET /models` 校验结果，读 `auth-status.json`）+ 上次校验时间。它不回答“Key 是什么”。来源标签只能是 `环境变量（MLA_LLM_API_KEY）` / `用户凭据文件（auth.json）` / `未配置`。
- 状态行渲染器**不接受 Key 参数**，不显示 Key、不显示 Key 长度，也不显示任何 Key 片段（“前 4 位 / 后 4 位”这类掩码提示同样禁止）。
- `<上次校验时间>` 的**唯一来源**是 `<MLA_HOME>/auth-status.json` 里的 `checked_at`（第 3 章）：文件内是带时区的 ISO-8601（精确到秒），状态行只渲染到分钟。从未校验过（文件缺失或读不出）时状态行写 `已配置 · 未校验（来源：<标签>）`，不得凭空显示“有效”；最近一次校验失败（`ok: false`）时状态行也不得停留在“有效”，改写 `已配置 · 未通过校验（<10.9 的固定文案>）`。页面渲染只读这个文件，**不得每次都发校验请求**（否则一次刷新就是一次网络调用）。
- 校验结果的存放位置：`<MLA_HOME>/auth-status.json`，只含 provider、结果、分类与时间戳，**不含 Key、不含长度**，且不改动 `auth.json` 形状（第 3 章 `auth.json` 形状约束 4）。进程内不再用 `st.session_state` 作为“上次校验时间”的存储：重启后状态行仍能显示上次校验时间（AC-23），`st.session_state` 里依旧不留任何 Key（AC-14）。
- **校验时间陈旧（`checked_at` 超过 30 天）**：状态行在现有文字后追加一句提示 `上次校验超过 30 天，建议重新测试`；状态主体仍是 `已配置 · 有效（<上次校验时间>）`——这个“有效”只代表“上次校验时通过”（见下条），**不得因为时间陈旧就改写为“未校验”或“无效”**（没测过就不该断言无效）。本版**不做后台自动复验**（会耗流量、断网时还会失败）；用户点 10.8 的“测试连接”即刷新 `checked_at`，提示随之消失。
- “有效”只代表“上次校验时 `GET /models` 通过”，不代表此刻仍然可用；界面不得写成“Key 正常”“Key 健康”这类更强断言。

### 10.4 保存成功后必须同时出现的 6 个可见变化

这六条就是用户要的“不能静默输入、且保存后不可查看”，验收时逐条对照（AC-13 / AC-14 / AC-21）：

1. 状态行从 `未配置 · 服务商：DeepSeek` 变为 `已配置 · 有效（<上次校验时间>）· 来源：用户凭据文件（auth.json） · 服务商：DeepSeek · 模型：<模型名>`。
2. 核对页由整页门禁提示变为可进入，条款审查勾选框可用（旧实现里 `disabled=llm is None` 的置灰已退为组件级行为，界面以“页面能否进入”为准）。
3. **校验通过并保存后，状态行出现“有效 + 上次校验时间”**，且核对页可开启条款审查——这三件事是同一次“测试并保存”的结果，不再把“保存”与“测试”拆成两个按钮（旧版 `src/mla/ui/app.py:1583` 那个“保存前不可点的测试连接”口径作废）。
4. 结果页不再出现“本次未做条款级审查，条款级风险未覆盖。”（旧常量 `CLAUSE_NOT_REVIEWED_NOTE`，`src/mla/ui/app.py:86`）。前提是本次核对确实勾选了条款审查；没勾选时这句还会如实出现，那是用户自己关掉的，不算静默。
5. 重启应用（新进程）后仍显示“已配置”，证明 Key 落在 `auth.json` 而不是会话内存（`st.session_state` 里不留 Key）。
6. **保存成功后 Key 输入框被清空**：页面文本与控件值里都找不到刚输入的那串字符（哑 Key 前缀扫描 0 命中），也不能靠“显示/隐藏”切回查看，因为输入框已经是空的。

补充：保存后要给一次性的 `settings_flash` 成功提示（旧版是“已保存到本机数据库，下次启动自动读取”，本版必须改为“已保存到用户凭据文件（连接校验通过），下次启动自动读取”），且提示中不出现 Key。

### 10.5 未配置时的界面行为

- 设置页说明缺 Key 影响哪些功能：**处理建议不生成、生成式问答拒答、条款级审查不可用**；确定性规则核对与必备条款缺项检测不依赖模型；缺项检测的结论并入核对流程，无模型时不单独向用户交付（与 003 第 8 章一致）。注意 0.3.0 起核对入口受 003 门禁约束（无模型时不发起核对），因此界面文案只描述“能力不依赖模型”，不得写成“系统不可用”或降级输出模板回答。
- 核对页整页显示门禁提示与“去设置”指引（可点链接跳到设置页），不能只给一句冷冰冰的拒绝。0.3.0 起不渲染合同输入与勾选框；勾选框在组件层面的置灰由单测覆盖。
- 任一位置都**不降级成模板回答**：没有 Key 就是不给生成式结论，不是给个假答案。

### 10.6 扩容路径（加第二家服务商）

- 只需两处改动：①`auth.json` 增加一个 provider 顶层键（形状同 `{"type": "api_key", "key": "..."}`）；②设置页下拉加一个选项。
- 存储结构、resolver 优先级、`set_key()/clear_key()/list_key_status()` 签名都不动；`resolve_key(provider=...)` 已经是按 provider 取值的。
- 模型名沿用 `MLA_LLM_MODEL`（每服务商的默认模型名在配置侧映射，不在 `auth.json` 里存）；设置页只展示当前生效的服务商与模型名。
- 本版不做多服务商并行：同一时刻只有一个生效 provider，切换 provider 不删除另一家的 Key（`auth.json` 读改写保留其他条目，见第 3 章约束 3）。

### 10.7 不做的事

- 不按模型存 Key（10.2）。
- 不显示 Key 内容、不显示 Key 长度、不在状态行透出任何 Key 片段（含“前 4 位 / 后 4 位”掩码提示）。
- 不提供第二输入入口（侧栏/核对页/登录页/CLI 参数均不接受 Key 明文）。
- **不把“保存”做成可以跳过校验的动作**：保存内置 `GET /models` 校验（不通过不落盘）是**硬约束，不是选项**，没有开关可以关掉它。
- **不做“跳过校验强制保存”的旁路**：不提供 `--force`、不提供“先存后测”、不提供“已知风险仍然保存”复选框。离线或内网受限的机器因此无法通过界面保存 Key——这是有意取舍，代价写在第 13 章风险表。

### 10.8 更改 Key 与清除 Key

配置完成之后输入区不再常驻，只留状态行与动作按钮；改 Key 走显式入口，避免“输入框常开”带来误覆盖或误清空。

**已配置态（收起）** 只显示三样：

1. 状态行（见 10.3 第 4 条）。
2. **“更改 Key”**：展开输入区，本身不写文件。
3. **“测试连接”**：手动复验**当前已生效**的 Key，只调 `GET /models`（超时用 `MLA_AUTH_CHECK_TIMEOUT`），不改 `auth.json`、不产生对话请求；结果写 `auth-status.json`（`ok` / `kind` / `checked_at`，第 3 章）并据此刷新状态行的“有效 / 上次校验时间”。

**点“更改 Key”后（展开）**：

1. 展开区包含：服务商下拉（当前仅 DeepSeek）+ 新 Key 掩码输入 + **“测试并保存”** + **“取消”**。
2. “测试并保存”与 10.3 第 3 条是同一条路径：先校验，通过才 `set_key()`，落盘并重读成功后才提示成功。
3. **替换失败保留旧值**：新 Key 校验不通过时**不写文件**，`auth.json` 保持原值且旧 Key 继续生效，状态行仍为原来的“有效（原校验时间）”，提示固定为 `新 Key 未通过校验，原 Key 仍在使用`（后面再附 10.9 的具体原因）。不得出现“已切换”“已更新”这类措辞。
4. **“取消”**：收起输入区并清空本次输入，不触碰文件、不改状态行。
5. **“清除此 Key”放在更改流程内**（收起态首屏不出现），点击后**二次确认**；确认文案必须写明清除后会失去的能力：**处理建议生成、生成式问答、条款级审查**（确定性规则核对与必备条款缺项检测不依赖模型；缺项检测结论随核对流程交付，口径见 10.5）。清除调 `clear_key("deepseek")`，成功后回到未配置态：状态行变 `未配置 · 服务商：DeepSeek`，核对页回到整页门禁提示并显示“去设置”指引（口径同 AC-16）。
6. 二次确认的默认焦点是“取消”，“清除”不得做成 primary 按钮，也不得用红色以外的强调把用户往上推。
7. 清除只删 provider 条目，不写空字符串、不留“空 Key”占位（第 3 章约束 2）；清除后**不得**从旧数据库回迁旧的 Key（迁移标记已 complete 时尤其要注意，见第 7 章成功流程第 1 条）。

**未配置态**：直接显示输入区（服务商下拉 + Key 输入 + “测试并保存”），不需要先点“更改 Key”；此时没有可清的条目，因此不显示“清除此 Key”；也没有已生效的 Key，因此不显示“测试连接”（校验已经内置在保存里）。

权限边界：以上动作只对管理员可见，普通用户看不到模型设置区块（10.1）。

### 10.9 校验失败的三类提示（必须分开）

保存动作与手动“测试连接”共用同一套校验结果分类。三类必须分开提示，因为用户下一步该做什么完全不同：

| 情况 | 判定 | 提示文案（固定） |
| --- | --- | --- |
| HTTP 401 / 403 | Key 无效 | `Key 不正确，请检查后重新输入` |
| 超时 / 连接被拒 / DNS 解析失败 / 代理不通 | 网络问题 | `网络不通，请稍后重试（本次未保存）` |
| 其他 4xx / 5xx（含 429、503） | 服务端异常 | `服务暂时不可用，本次未保存` |

硬约束：

1. **任何失败都不写入 `auth.json`**：三类失败一律不调 `set_key()`，文件字节保持不变；已配置态下旧 Key 继续生效（10.8 第 3 条）。
2. **任何失败都不得显示“已保存”**，也不得显示“已更新”“已切换”“保存成功（但校验失败）”这类混合措辞。成功提示只在**落盘 + 重读校验**都通过之后出现（第 6 章 `set_key()` 契约）。
3. 提示里不带 Key 的任何片段，也不回显用户输入的内容；打印异常时不得把 Key 拼进 URL 或请求头（现实现把 Key 放在 `Authorization` 头，不在 URL，继续保持）。
4. 失败时输入框保留用户本次输入（便于改一个字符重试），但**仍不显示长度**；离开页面或点“取消”后清空。
5. **实现要点（不要靠中文文案判类）**：现有 `src/mla/adapters/llm.py:56-60` 把 HTTP 错误与网络错误都塞进同一个 `LLMError` 字符串（`HTTP <code>：...` 与 `调用失败：...`）。保存校验与“测试连接”**只调第 6 章新增的 `probe_models(key)`**，拿到结构化 `ProbeResult`（`kind` 取 `ok` / `auth` / `network` / `server`，另带 `status_code`），再由界面映射到上表三条固定文案；分类在捕获处按 `urllib.error.HTTPError.code` 与 `(urllib.error.URLError, TimeoutError, socket.gaierror)` 判定，**不得用字符串匹配或中文关键字判断类别**；`health()` 保持布尔返回，不承担分类职责。
6. **校验只发 `GET /models`**：`probe_models()` 与既有的 `src/mla/adapters/llm.py:67-77` `health()` 共用同一条请求路径（payload 为空时 `src/mla/adapters/llm.py:42-52` 走 `method="GET"`）。**不得发 `/chat/completions`**，不得为校验构造 messages 或任何生成负载，不产生 token 费用（AC-20 用打桩断言）。
7. **超时单独设短的超时参数**：校验用 `MLA_AUTH_CHECK_TIMEOUT`（默认 **10 秒**），**只**作用于保存内置校验与手动“测试连接”两处；正常推理、抽词、问答仍只读 `MLA_LLM_TIMEOUT`（默认 60 秒，`src/mla/config.py:133`），本次改动不得缩短推理超时。理由：一次点击的反馈不能让人等 60 秒，而跑一次长回答等 60 秒是正常的。超时仍归入“网络问题”分类（上表第 2 行固定文案），验收只断言“超时 → 网络类提示 + 未保存”（AC-24 断言两个超时变量互不串用）。

## 11. 验收标准

以下验收均使用隔离临时目录和哑 Key；真实旧库只用于副本演练，任何命令输出不得包含 Key。新增测试名称是实现时的契约，可按等价命名调整，但覆盖项不得减少。

### AC-01：换工作目录后 Key、账号与数据库不丢

- 自动：`pytest tests/test_runtime_paths.py -k "cwd or legacy_db_copy"`。在两个不同 cwd 下解析默认路径，断言 `MLA_HOME`、`MLA_DB`、`MLA_INDEX_DIR` 不变；从旧 cwd 引导后，新库账号与业务记录数保持一致。
- 手工观察：在目录甲保存管理员 Key，改到目录乙启动 UI 与 `mla auth list`，两者均显示 `用户凭据文件（auth.json）`，历史账号仍可登录。
- 真实副本演练：旧库 3 个账号在复制后仍为 3 个；不得用真实 Key 做输出断言。

### AC-02：UI、CLI、服务与脚本读取同一个值和来源

- 自动：`pytest tests/test_credentials.py tests/test_cli.py tests/test_services.py tests/test_ui_setup.py -k "resolver or source or llm"`。
- 在同一隔离 HOME 中分别经 UI 设置、CLI check、service 默认装配和评测脚本装配取得配置，断言 Key 相等、来源均为 `auth_file`；无参 `DeepSeekLLM()` 不得再提供隐藏回退。
- 手工观察：UI 的来源标签与 `mla auth list` 一致，且任何输出都看不到 Key。

### AC-03：旧库迁移成功且只执行一次

- 自动：`pytest tests/test_credential_migration.py -k "success or idempotent or clear"`。
- 准备含旧 `llm_api_key` 的哑数据库，首次引导后断言：文件值与旧值相等、来源正确、旧行已删、其他 settings 和业务数据不变、标记完成。
- 再运行一次结果不变；清除文件凭据后再次运行不得从旧库复活 Key。

### AC-04：解析优先级严格为 env > auth_file，且 secrets 不参与

- 自动：`pytest tests/test_credentials.py -k "priority"`。
- 用互不相同的哑值覆盖两级来源，逐项撤掉高优先级来源，断言选中值与标签按表切换；环境变量不得被写入 `auth.json`。
- 反证项：在隔离 HOME 下预置一份**含非空 `[llm].api_key`** 的 `.streamlit/secrets.toml`，且不预置 `auth.json`，断言 resolver 返回 `none`；若同目录另有合法 `auth.json`，来源只能是 `auth_file`，**绝不能是 secrets**。来源标签中不出现任何与 secrets 有关的字样。
- 迁移完成后代码搜索不得再有正常 `legacy_database` 回退：`rg "legacy_database|_sqlite_api_key|LLM_KEY_SETTING|_secrets_api_key|_secrets_llm_key" src` 只能有迁移说明需要保留的命中，最终运行时代码不得调用数据库读 Key，也不得读 LLM 的 secrets。

### AC-05：缺少文件与未配置行为正确

- 自动：`pytest tests/test_credentials.py tests/test_ui_setup.py -k "missing or unconfigured"`。
- 不存在的 `auth.json` 不报错、不被只读解析创建；其他来源也为空时返回 `none`。
- UI 显示“未配置”和管理员保存指引，普通用户看不到模型设置；无模型时生成式问答按现有规则拒答，确定性核对仍运行。

### AC-06：损坏文件显式失败且不覆盖、不泄露

- 自动：`pytest tests/test_credentials.py -k "corrupt or invalid or error"`。
- 分别放入非法 JSON、非对象顶层、错误 `deepseek` 形状，断言抛出安全错误、原文件字节不变、不会静默回退成“未配置”。
- UI/AppTest 与 `mla auth list` 均显示可操作的修复提示；stdout、stderr、异常文本和日志不包含测试 Key。

### AC-07：迁移失败不会假装成功，旧值可恢复

- 自动：`pytest tests/test_credential_migration.py -k "read_failure or write_failure or permission_failure or cleanup_failure"`。
- 分别模拟旧库读失败、文件写失败、权限失败、重读不一致、旧行清理失败和标记失败，逐项核对第 7 章表格。
- 特别是导入失败时旧库 Key 必须仍在、无完成标记、UI 显示“请重新保存 Key”；修复原因后重试能够幂等完成。

### AC-08：权限收紧可观察

- 自动：`pytest tests/test_credentials.py -k "posix_mode or windows_acl or acl_failure"`。
- Windows 手工：在隔离目录执行 `icacls <临时MLA_HOME>/auth.json`，按第 8 章检查主体；用另一普通用户读取应被拒绝。
- 非 Windows 自动断言目录 `0700`、文件 `0600`。不得只凭一次 `chmod` 调用就判定 Windows 验收通过。

### AC-09：保存、清除与 CLI 管理闭环

- 自动：`pytest tests/test_ui_setup.py tests/test_cli_auth.py -k "save or clear or set or list or remove"`。
- 管理员保存后文件出现合法 `deepseek`，清除后条目消失而其他 provider 保留；普通用户仍无权限。
- `mla auth set deepseek` 使用隐藏输入，`list` 只显示元数据，`remove` 后按优先级回落；捕获全部输出并断言输入的哑 Key 从未出现。

### AC-10：显式路径覆盖与测试隔离有效

- 自动：`pytest tests/test_runtime_paths.py tests/test_credentials.py -k "override or isolation"`，并运行完整测试确认没有真实 HOME 读写。
- 显式 `MLA_DB` 时不得自动搬迁数据库；显式 `MLA_INDEX_DIR` 时不得写默认缓存；`MLA_HOME` 相对路径必须报安全配置错误。
- 在开发者真实 HOME 预置可识别的隔离陷阱文件，测试运行后其内容与修改时间不得变化；测试进程不得使用其中的值联网。

### AC-11：secrets 读取已失效、embedding 边界未破坏

- 自动：`pytest tests/test_credentials.py tests/test_dense.py tests/test_rag.py`；在 `test_credentials.py` 新增断言：`.streamlit/secrets.toml` 存在且 `[llm].api_key` 非空时，LLM resolver 结果不受影响（来源不是 secrets、值不等于该哑值）。
- 另加断言：`[embedding].api_key` 不进 LLM resolver；保存/清除 LLM Key 前后 `.streamlit/secrets.toml` 与 `secrets.toml.example` 的字节不变（程序不改写用户文件）。
- embedding 行为保持原样：`DASHSCOPE_API_KEY` 与 `[embedding].api_key` 的读取、无 Key 回退 BM25 全部不变。

### AC-12：项目门禁与发布检查全部通过

在本仓库运行并贴出最后一次输出：

```powershell
.venv\Scripts\python -m pytest
ruff check src tests scripts
.venv\Scripts\python -m pyright src
.venv\Scripts\python scripts\check_ui_text.py
.venv\Scripts\python scripts\run_eval.py --check-determinism
.venv\Scripts\python -m mla --version
```

- 现有 676 条测试是基线，不是允许失败数；新增用例后必须全绿，不能用删除旧测试代替迁移覆盖。
- 版本命令最终应输出 `0.3.0`（由阶段五改号）。AC-12 在本地跑完并贴输出；提交与发布按项目发布流程执行。
- 发布前再以真实旧库副本完成 AC-01、AC-03、AC-07 的人工升级演练；不输出 Key，只核对长度 35 的值在迁移前后相等以及 3 个账号仍可读取。

### AC-13：保存后状态行、勾选框、有效性三处同时变化

- 自动：`pytest tests/test_ui_setup.py -k "save_visible_changes"`。用 Streamlit AppTest 在“未配置”起始状态下保存一个哑 Key，然后断言三处同时改变：①设置页状态行由 `未配置` 变为含 `已配置` 与 `用户凭据文件（auth.json）`；②核对页由整页门禁提示变为可进入，条款审查勾选框可用（未配置时不渲染勾选框，故以“页面是否可进入”为准）；③状态行同时出现 `有效（<上次校验时间>）`，且该时间晚于本次点击保存的时刻（证明保存内部真的跑了校验，而不是“存完再说”）。
- 手工观察：按第 10.4 节逐条对照 6 个变化（含第 4 条“结果页不再出现‘本次未做条款级审查’”与第 6 条“输入框被清空”），中间不刷新页面以外的任何额外操作。
- 反例守卫：保存成功后不得出现“看起来没反应”的状态——即状态行、有效性任一仍停留在未配置形态，或核对页仍停留在门禁提示形态，即判失败；状态行出现“已配置”但校验未通过也判失败（那属于 AC-18/AC-19 的“不得显示已保存”）。

### AC-14：重启进程后仍为已配置（证明落盘）

- 自动：`pytest tests/test_ui_setup.py tests/test_credentials.py -k "persist_after_restart"`。在隔离 HOME 保存哑 Key 后，销毁 AppTest 会话、用同一个 `MLA_HOME` 新建一次会话，断言状态行仍为 `已配置`，来源 `auth_file`。
- 同时断言 `auth.json` 文件存在且 `deepseek.key` 等于所存哑值（只在测试内读取，不打印）。
- 手工观察：真实启动 `streamlit run` 保存后杀掉进程再启动，设置页仍显示已配置；`st.session_state` 中不存在任何 Key 字段。

### AC-15：全仓搜索无第二处 Key 输入控件

- 自动：`rg "type=\"password\"|text_input\(.*Key|API Key" src` + 人工核对每处命中。除设置页与账号密码类输入外，不得出现 Key 输入框；`src/mla/cli.py` 的 `auth set` 用 `getpass` 隐藏输入，不接受 Key 作为命令行参数。
- 断言清单：核对页、侧栏、登录页、初始化页、结果页均无 Key 输入控件；`tests/test_ui_setup.py:413` 的“侧栏无模型设置入口”用例继续通过。
- 手工观察：在界面上把每个页面走一遍，确认只有“设置 → 模型设置”一处能输入 Key。

### AC-16：未配置时整页门禁与“去设置”指引（勾选框置灰为组件级行为）

- 自动：`pytest tests/test_ui_setup.py -k "unconfigured_hint"`。在无 Key 的隔离 HOME 打开核对页，断言页面给出“未配置模型”门禁提示、“去设置”指引，以及缺 Key 影响说明（处理建议 / 生成式问答 / 条款级审查不可用）。
- **与 003 门禁的关系**：0.3.0 起无模型时核对页整页拒绝（规格 003 第 8 章），不渲染合同输入与勾选框，因此本条的界面断言以门禁提示与指引为准；“启用条款审查（模型辅助）”在 `llm is None` 时为 `disabled` 属组件级行为，由 `_clause_review_toggle(cfg, None)` 的单测覆盖，不要求在整页门禁下渲染出来。
- 反例守卫：只说“系统故障”、不提“去设置”、或降级输出模板回答，判失败。
- 手工观察：确定性规则核对与必备条款缺项检测在无 Key 时照常出结果，口径与项目 AGENTS.md 一致。

### AC-17：环境变量来源如实显示且不泄露 Key

- 自动：`pytest tests/test_ui_setup.py tests/test_credentials.py -k "env_source_label"`。只设置 `MLA_LLM_API_KEY`（无 `auth.json`）时，状态行为 `已配置 · 来源：环境变量（MLA_LLM_API_KEY） · 服务商：DeepSeek · 模型：<模型名>`；断言该行不含哑 Key 的任何片段，也不含其长度数字。
- 断言 `list_key_status()` 输出结构里无 Key 字段、无长度字段；AppTest 捕获的全部文本中哑 Key 出现次数为 0。
- 手工观察：环境变量存在时设置页仍允许保存覆盖到 `auth.json`（保存后来源标签切换为 `用户凭据文件（auth.json）`），但下一次启动仍由环境变量取胜——这与第 4 章优先级一致，不算 bug。

### AC-18：断网时“测试并保存”不落盘

- 自动：`pytest tests/test_ui_setup.py -k "save_offline_no_write"`。在隔离 `MLA_HOME` 下把校验用的 `urllib.request.urlopen` 打桩为抛 `urllib.error.URLError`（模拟断网 / DNS 失败），点“测试并保存”后断言：①`auth.json` 在此前不存在时**不生成**，此前存在时**字节与点击前完全相同**；②状态行仍为 `未配置 · 服务商：DeepSeek`（已配置场景仍保持原“有效”状态与旧时间戳）；③提示文本同时含“网络不通”与“未保存”；④页面文本与日志中不出现“已保存”“已更新”。
- 反例守卫：`set_key()` 被调用过即判失败（spy 断言调用次数为 0）；把网络错误提示成“Key 不正确”也判失败（那会把用户引向换 Key 的错误方向，见 10.9 分类）。

### AC-19：无效 Key 提示“Key 不正确”且文件不变

- 自动：`pytest tests/test_ui_setup.py -k "save_invalid_key_no_write"`。把校验打桩为返回 HTTP 401，再跑一遍 HTTP 403，断言提示恰为 `Key 不正确，请检查后重新输入`，`auth.json` 不生成 / 字节不变，`set_key()` 调用次数为 0，状态行未变成“已配置”。
- 已配置态补充：换无效 Key 的旧值保留语义由 AC-22 路径二覆盖，提示不得出现“已切换”。

### AC-20：校验只调用 `GET /models`，不产生对话请求

- 自动：`pytest tests/test_ui_setup.py tests/test_credentials.py -k "probe_models_only"`。对 `urllib.request.urlopen` 打桩并在调用处记录 `(method, 完整 URL, 是否带 body)`，断言：①校验只出现一次 `GET`，路径以 `/models` 结尾；②没有任何 `/chat/completions` 命中；③请求带 `Authorization` 头，且不含 messages / temperature / stream 等生成负载。
- 静态断言：校验实现所在模块不得调用 `generate()` / `chat()`（对校验模块跑 `rg "chat/completions|generate\(|messages="`，命中数 0）。
- 手工观察：真实保存一次 Key 后服务商后台的 token 用量不因这次保存而增加（只核对“无新增”，不看具体数值）。
- 反例守卫：一次保存触发多次校验请求（例如先 GET 再 POST 兜底）判失败。

### AC-21：保存后页面与接口响应里查不到 Key

- 自动：`pytest tests/test_ui_setup.py -k "no_key_echo_after_save"`。保存一把哑 Key（测试内构造的固定假串，约 35 字符，含一个可识别的固定前缀，绝不使用真实 Key）后断言：①AppTest 捕获的全部页面文本与控件值中，该假串前缀出现次数为 0；②`st.session_state` 中不存在保存 Key 的字段；③`list_key_status()` 返回结构里无 Key 字段、无长度字段。
- 输入框断言：保存后读 Key 输入控件当前值等于 `""`；切到“显示”后仍为 `""`（证明是清空而不是遮罩）。
- 手工观察：保存后把设置页每个控件点一遍并抓页面文本，该固定前缀命中 0 次；已配置状态行只有“已配置 / 有效 / 时间 / 来源 / 服务商 / 模型”，没有任何片段。
- 接口侧：对设置页向浏览器暴露的序列化结果做同一次扫描（本版 Streamlit 无自定义接口，此条对将来 FastAPI 形态继续有效）。

### AC-22：更改 Key 与清除 Key 三条路径

- 自动：`pytest tests/test_ui_setup.py tests/test_credentials.py -k "change_key or clear_key"`。
- 路径一（换有效）：已配置态点“更改 Key”，填入另一把能通过校验的哑 Key → `auth.json` 的 `deepseek.key` 变成新值，状态行时间戳刷新，提示为成功且不含 Key。
- 路径二（换无效）：已配置态点“更改 Key”，填入会返回 401 的哑 Key → `auth.json` 字节不变、旧 Key 仍能解析并可正常使用，提示含“原 Key 仍在使用”，页面无“已切换 / 已更新”字样。
- 路径三（清除）：在更改流程内点“清除此 Key”并二次确认 → `deepseek` 条目消失，状态行回到 `未配置 · 服务商：DeepSeek`，核对页回到整页门禁提示并显示“去设置”指引；其他 provider 条目（若有）保留。
- 反例守卫：清除按钮出现在收起态首屏（未点“更改 Key”时）判失败；清除没有二次确认判失败；清除后核对页仍可按原路径进入并送出核对判失败；清除后旧库 Key 被回迁判失败（第 7 章成功流程第 1 条）。

### AC-23：重启进程后状态行仍显示上次校验时间（校验历史落盘）

- 自动：`pytest tests/test_ui_setup.py tests/test_credentials.py -k "auth_status_persist"`。在隔离 `MLA_HOME` 下保存一把能通过校验的哑 Key（校验打桩为成功），断言：①`<MLA_HOME>/auth-status.json` 生成，`deepseek` 条目形状为 `{"ok": true, "checked_at": <ISO-8601 带时区偏移>, "kind": "ok"}`；②该文件全文与结构里既不含哑 Key，也不含其长度或任何片段。
- 跨进程：销毁 AppTest 会话、用同一个 `MLA_HOME` 新建一次会话（`auth.json` 保留、`st.session_state` 清空），断言状态行仍显示 `有效（<上次校验时间>）`，且该时间等于文件中 `checked_at` 的渲染值（精确到分钟），来源仍为 `用户凭据文件（auth.json）`。
- 分类落盘：把“测试连接”打桩为 HTTP 401 → 文件写 `ok: false, kind: "auth"`；打桩为超时 / `URLError` → `ok: false, kind: "network"`；两种情况 `auth.json` 字节均不变，且 `kind` 里不出现中文文案。
- 反例守卫：删除 `auth-status.json`，或写入非法 JSON / 缺 `checked_at` → 状态行降级为 `已配置 · 未校验`，不抛异常、不显示“有效”、不影响 `auth.json` 的解析与保存；把时间戳写进 `auth.json`、或让 resolver 读 `auth-status.json` 取值判失败（它不是凭据来源，第 3 章 `auth-status.json` 形状约束 3）；页面每次渲染都发一次 `GET /models` 判失败（渲染只读文件）。

### AC-24：校验超时与推理超时互不影响

- 自动：`pytest tests/test_llm.py tests/test_ui_setup.py tests/test_credentials.py -k "auth_check_timeout"`。断言：①`MLA_AUTH_CHECK_TIMEOUT` 未设置时，保存校验与手动“测试连接”的超时取 **10 秒**；设为 3 秒时这两处取 3 秒；②同一次推理调用（`generate()`）的超时只取自 `MLA_LLM_TIMEOUT`（未设置时 60 秒），在 `MLA_AUTH_CHECK_TIMEOUT=3` 下推理超时**仍为 60**，证明两个变量不串用；③把校验打桩为抛超时（`TimeoutError` / `socket.timeout`）时，“测试并保存”提示恰为网络类固定文案 `网络不通，请稍后重试（本次未保存）`，`auth.json` 不生成或字节不变，`set_key()` 调用次数为 0。
- 反例守卫：把 `MLA_AUTH_CHECK_TIMEOUT` 同时套到推理超时判失败；把超时归成“Key 不正确”判失败；推理侧误读 `MLA_AUTH_CHECK_TIMEOUT`（两处读同一变量）判失败。
- 手工观察：`MLA_AUTH_CHECK_TIMEOUT=1` 时点“测试连接”，在断网环境下提示约 1 秒内出现且为网络类文案；随后发起一次正常问答，等待行为不变（仍按 `MLA_LLM_TIMEOUT` 计）。

## 12. 分阶段计划

每个阶段都是独立可验收增量；实施后运行该阶段验证并贴输出。本版对应 0.3.0；阶段四（删旧库 Key）为破坏性步骤，执行前单独确认。

UI 改动按可独立验收来切分（第 10 章的控件形状）：

| UI 改动 | 落在哪个阶段 | 对应验收 |
| --- | --- | --- |
| 保存逻辑（掩码输入 + “测试并保存”：先 `GET /models` 校验，通过才调 `set_key()`，失败不落盘） | 阶段三 | AC-09、AC-13 第①项、AC-18、AC-19、AC-20、AC-24 |
| 状态行（由 `list_key_status()` 渲染，含“有效 + 上次校验时间”，时间读 `auth-status.json`） | 阶段三 | AC-13 第①③项、AC-17、AC-23 |
| 保存后清空输入框、不回填（第 10.3 第 2 条） | 阶段三 | AC-21 |
| 未配置时的“去设置”指引与影响说明 | 阶段三 | AC-16 |
| 核对页从门禁提示到可进入、结果页注解消失 | 阶段三（随解析收敛自然生效） | AC-13 第②④项 |
| 服务商下拉（按 provider 键名取值，当前仅 DeepSeek 一项） | 阶段五 | 第 10.6 节扩容路径 |
| 更改 Key / 清除此 Key（第 10.8 节）与手动“测试连接”复验 | 阶段五 | AC-22 |
| 重启后仍已配置（落盘证明） | 阶段三 | AC-14 |

### 阶段一：建立用户目录、文件存储与统一 resolver（非破坏）

步骤：

1. 新增 `config.py` 的纯 `MLA_HOME` 路径计算、`adapters/auth_file.py` 和 `services/credentials.py`，暂不改现有调用方。
2. 完成 JSON 校验、`env > auth_file` 两级优先级、来源标签、原子写入、POSIX/Windows 权限与错误类型；**本阶段就不实现 secrets 来源**，从第一版契约起只有两级。
3. 新增隔离测试与 `tests/conftest.py` 全量 HOME/DB/INDEX/cwd 防泄漏保护（`_secrets_section` 打桩只服务 embedding）。

涉及文件：上述三个源码文件、`tests/conftest.py`、新增 `test_runtime_paths.py`、`test_credentials.py`。

验证：`pytest tests/test_runtime_paths.py tests/test_credentials.py`，覆盖缺文件、损坏文件、优先级、权限失败和无 Key；旧功能测试继续全绿。

回滚：删除新增模块与测试即可，旧运行路径尚未切换。

### 阶段二：切换数据库与缓存默认目录，复制旧业务库（行为变更，非破坏）

步骤：

1. `config.py` 默认 DB/INDEX 改到 `MLA_HOME`，保留 `MLA_DB` 与 `MLA_INDEX_DIR`。
2. 新增 `services/bootstrap.py`，在 repository 创建前执行旧库发现与 SQLite 一致性复制；UI/CLI 入口接入。
3. 补齐新库已存在、显式覆盖、无旧库、其他 cwd 旧库和缓存重建测试。

涉及文件：`config.py`、`services/bootstrap.py`、`ui/app.py`、`cli.py`、`adapters/persist.py` 的必要装配点、`test_runtime_paths.py`。

验证：AC-01、AC-10；使用含账号与业务数据的哑库验证复制前后一致，真实旧库只用副本。

回滚：恢复旧默认路径即可，旧库仍在；若新版已经产生新业务记录，回滚时用旧版已支持的 `MLA_DB` 指向新库，不自动合并两份数据。

### 阶段三：所有运行调用方收敛到 resolver + 设置页保存与状态行（内部接口破坏，旧 Key 不删）

步骤：

1. 按第 6 章清单改 UI、CLI check、services、DeepSeekLLM 构造和三个评测脚本，UI 保存目标改为 `auth.json`；删除 LLM 一侧的 secrets 读取（`_secrets_api_key()`、`_secrets_llm_key()`）。
2. 本阶段暂时保留最低优先级旧数据库只读兜底，保证尚未迁移的用户不会突然无 Key（**该兜底在阶段四删除**）。
3. **UI 保存逻辑与状态行在本阶段完成**：掩码输入（仅输入期可显示/隐藏）、**“测试并保存”**（内置 `GET /models` 校验，**校验不通过不落盘**，按 10.9 三类提示报错）、保存成功后**清空输入框且不回填**、`list_key_status()` 驱动的状态行（`已配置 · 有效（<上次校验时间>）· 来源：<标签> · 服务商：DeepSeek · 模型：<模型名>`，其中校验时间读 `<MLA_HOME>/auth-status.json`，本阶段即按落盘实现、不先用 `st.session_state` 过渡）、未配置时的“去设置”指引与影响说明（第 10.1–10.5、10.9 节）。保存后的第 1、3、6 个可见变化在本阶段即可验收；第 5 个变化（重启仍已配置）也靠本阶段的落盘与状态行就能验。**服务商下拉、手动“测试连接”、更改 Key / 清除 Key（第 10.8 节）放阶段五**（见下表分工）。
4. 重写 UI/CLI/service 测试，确认同一来源和值，完成过时界面文案调整。

涉及文件：第 6 章全部运行调用点、`ui/app.py`（设置页与核对页）、`test_cli.py`、`test_ui_setup.py`、`test_services.py`、`test_llm.py`、评测相关测试。

验证：AC-02、AC-04、AC-05、AC-09 的 UI 部分、AC-13、AC-14、AC-16、AC-18、AC-19、AC-20、AC-21、AC-23、AC-24（校验超时与推理超时隔离），以及 `pytest` / `ruff` / `pyright` / `check_ui_text`。

回滚：恢复调用方即可；旧数据库 Key 尚未删除，旧版仍可运行。新写入的 `auth.json` 留作无害用户文件。

### 阶段四：首次凭据迁移、幂等标记与旧库 Key 清理（破坏性）

步骤：

1. 在 bootstrap 中实现**旧数据库单一候选**的导入（secrets 不参与）、写后重读、迁移标记与失败状态展示。
2. 导入验证成功后删除新旧库中的 `llm_api_key` 行，删除正常 `legacy_database` 回退（阶段三的临时兜底到此为止）。
3. 完成迁移成功、全部失败分支、并发/重复执行、清除后不回迁和真实库副本演练。

涉及文件：`services/bootstrap.py`、`services/credentials.py`、必要的持久化迁移辅助、UI/CLI 诊断、新增 `test_credential_migration.py`。

验证：AC-03、AC-06、AC-07、AC-11；真实副本只核对长度 35 的值相等与 3 个账号保留，不打印值。

回滚：回滚到阶段三的代码会直接读取已验证的 `auth.json`，不依赖被删旧行；不得直接跳到阶段二或更早并声称 Key 可自动恢复。若必须降到不支持 `auth.json` 的旧版，管理员需重新填写 Key，其他业务数据不受影响。

### 阶段五：补齐 auth CLI、UI 交互补项与版本号变更（收尾与改号）

**本阶段的性质变了：只把改动做完、验收跑完并贴输出。** 本阶段完成后跑 AC-12 全部门禁，提交与发布按项目发布流程执行。

步骤：

1. 新增 `mla auth list/set/remove` 与 `test_cli_auth.py`，验证隐藏输入和无 Key 输出，确认 CLI 与设置页都调同一个 `set_key()`。
2. **UI 交互补项**：按第 10.8 节做已配置态收起、“更改 Key”展开、**替换失败保留旧值**、“清除此 Key”二次确认与能力损失说明、以及手动“测试连接”复验（仍只发 `GET /models`，超时用 `MLA_AUTH_CHECK_TIMEOUT`，结果写 `auth-status.json`）；把服务商下拉做成按 provider 键名取值的结构（当前仍只有 DeepSeek 一项），落实第 10.6 节扩容路径。补齐 AC-15（全仓无第二输入控件）、AC-17（环境变量来源标签）、AC-22（更改 / 清除三路径），并复核 AC-21（保存后不可查看）与 AC-23 的“测试连接”分类落盘部分（401 → `kind: "auth"`、超时 → `kind: "network"`）。
3. 将 `src/mla/__init__.py` 从 `0.2.1` 升为 `0.3.0`；更新发布说明、用户配置/迁移/降级文档，明示明文威胁模型、旧 Key 清理与“secrets.toml 不再被读取”。**同时按第 9 章的 6 个步骤清理 `.streamlit/secrets.toml.example`：删除 `[llm]` 段整段、新增 `[embedding]` 示例段、文件顶部注释改为指向设置页、文件本身保留不改名**（理由见第 9 章：该模板仍在教用户填 `[llm].api_key`，而本版已彻底移除 LLM 侧的 secrets 读取，留着等于制造“配了不生效”的死路；`[embedding].api_key` 的读取行为不变，模板仍需为它留指引）。
4. 跑 AC-12 全部门禁和人工升级演练（AC-01、AC-03、AC-07）。跑完贴输出后按项目发布流程提交；跨仓同步不涉及（私有仓已冻结）。

涉及文件：`cli.py`、`__init__.py`、CLI 测试、`ui/app.py`（更改/清除 Key、手动测试连接、服务商下拉）、项目既有 README/CHANGELOG/使用文档、`.streamlit/secrets.toml.example`（删除 `[llm]` 段、新增 `[embedding]` 示例段）。

验证：`mla --version` 输出 `0.3.0`；AC-01 至 AC-24 全部通过；`git status --porcelain` 只显示预期改动文件。

回滚：未发布前本地回退文件即可。已公开发布后的降级会触发第 13 章所述 Key 与数据库分叉风险，必须按降级说明操作，发布按项目发布流程执行。

### 发布与跨仓同步（确认后执行，不属本版范围）

0.3.0 的提交与发布按项目发布流程执行；跨仓同步仍不涉及（私有仓已冻结）。

以下动作全部挂起，只有在明确确认“可以提交 / 可以推 / 可以发布”之后才执行：

1. 以本仓库为实现基线提交，再按**文件内容**逐项同步相关代码，禁止只按旧行号套用。
2. 提交前以本仓库当前 tag 为发布基线，核对版本号与 CHANGELOG 一致。
3. 设计文档位置：`docs/design/002-credentials.md`。

## 13. 风险与回滚

### 风险清单

| 风险 | 影响 | 预防与恢复 |
| --- | --- | --- |
| 用户升级后第一次看到“未配置” | 误以为 Key 被删，直接重复建库或分享旧库 | 启动时先迁移再渲染设置页；显示迁移状态、来源和路径；找不到旧目录时提示在原启动目录运行一次或用 `mla auth set` |
| 默认数据库切换后只看到新空库 | 账号、合同、历史任务看似丢失 | 新库不存在时先复制 cwd 旧库；真实副本验证 3 个账号；绝不自动合并或覆盖已有新库 |
| 迁移中途失败 | 新旧状态不一致或旧 Key 被提前删除 | 写文件、权限、重读全部成功后才删旧行；各失败点按第 7 章保留可重试状态 |
| 环境变量优先级变化 | 旧 UI 可能从持久 Key 切到进程 env | 作为有意破坏性变化写发布说明；来源标签常显；测试固定优先级 |
| 明文 `auth.json` 被主动复制 | Key 泄露 | 界面和文档标敏感；权限只防误继承与普通用户，不夸大安全边界；泄露后在提供方后台吊销并重配 |
| 历史副本仍含 Key（旧 `secrets.toml`、数据库备份、排障拷贝） | 迁移后旧副本仍可能泄露 | 本版不读 `.streamlit/secrets.toml`，旧值不会“复活”成生效来源；但仍提示用户手工清理该条目与检查备份；本版承诺的是“新数据库不再保存 Key、程序不再读 secrets” |
| 输入了 Key 但界面看不出变化（静默输入） | 用户重复保存、以为没生效，或误以为已生效而继续用无模型功能 | 第 10.4 节 6 个可见变化 + AC-13 三处同时变化断言 + AC-14 重启仍已配置 + AC-21 保存后不可查看 |
| 联网校验成为保存的前置条件 | 离线或内网受限的机器（无外网、必须走代理、防火墙拦 `api.deepseek.com`）无法通过界面保存 Key；用户可能误判为“软件坏了”或反复重试 | **这是有意设计，不是缺陷**：不做跳过校验的旁路（10.7）。界面按 10.9 把“Key 错误”与“网络错误”分开提示，网络类提示必须写“本次未保存”，避免用户去改 Key 白折腾；发布说明给出替代路径——临时用 `MLA_LLM_API_KEY` 环境变量启动（不落盘、不需校验），网络恢复后再用设置页保存 |
| 校验探针被换成消耗型实现 | 若将来把 `GET /models` 改成 `/chat/completions` 之类探针，每次保存都会消耗 token 并被计费，额度还会被无意义消耗 | 本版只用 `GET /models`（不产生 token 费用），AC-20 以打桩断言“只有一次 GET /models、无 /chat/completions、无生成负载”；任何换成消耗型探针的改动都必须重新评估成本并同步改本规格与 AC-20 |
| `auth-status.json` 陈旧、被篡改或 Key 已在服务商后台被吊销，但本机仍显示“有效” | 用户以为 Key 可用，实际调用会失败 | 状态行时间戳必须常显；`checked_at` 超过 **30 天** 时状态行追加提示“上次校验超过 30 天，建议重新测试”；不做自动后台复验（会耗流量、断网失败）；用户点“测试连接”即刷新 |
| Windows 权限假收紧 | `chmod` 成功但 ACL 仍宽松 | 以 `icacls` 和跨普通用户读取测试验收；失败不显示保存成功 |
| 新增第二家服务商时误改成按模型存 Key | 用户重复填同一把 Key，来源判断混乱 | 数据模型按 provider 顶层键（第 3 章）+ 第 10.2/10.6 节明确“一个服务商一把 Key” |
| 新旧数据库在回滚后分叉 | 业务记录各有一份 | 回滚时通过 `MLA_DB` 明确选同一份库；不做自动双向同步或合并 |
| 此前两个代码副本不同版 | 漏改调用点、行号错位、发布与私有开发行为不同 | 本次实施以 v0.2.1（`7dab9ca`）为基线；先做内容基线核对，再提交；同步后跑相同门禁并逐文件比较变更集 |
| 安装包或服务账号使用不同 HOME | 升级后看不到桌面用户的配置 | 本版不承诺跨操作系统用户共享；打包/服务化时必须显式设置同一 `MLA_HOME` 并处理 ACL，列入后续部署验收 |

### 总体回滚原则

- 阶段一、二不删除旧数据；阶段三不删除旧 Key，均可单独回滚。
- 阶段四是唯一删除旧凭据的破坏性阶段，必须单独验收、单独发布说明，并在提交前单独确认。
- 从不支持 `auth.json` 的旧版本直接恢复时，只能保证业务数据库仍在，不能保证旧 Key 自动恢复；管理员必须重新填写，不能从日志或规格中找回。
- 任何回滚都不得把另一份数据库自动覆盖到当前库，也不得为了“恢复成功”重新把 Key 写进仓库或打印到终端。

### 开放问题建议汇总

| 开放问题 | 本规格建议 | 一句话理由 |
| --- | --- | --- |
| `%LOCALAPPDATA%` 还是 `%APPDATA%` | `%LOCALAPPDATA%\mla\` | 单机凭据、数据库和缓存无需漫游，避免跨机复制与同步 |
| 测试如何避免真实凭据 | autouse 同时隔离 `MLA_HOME`、DB、INDEX、env 与 cwd | 只删环境变量仍会被真实文件或旧库迁移旁路命中；`_secrets_section` 打桩只为 embedding |
| `.streamlit/secrets.toml` 还读不读 | **不读，彻底移除 LLM 一侧来源** | 写入来源唯一、`st.secrets` 随 FastAPI 迁移消失、避免“配了不生效”的后门；实测该仓库无此文件 |
| 是否按模型存 Key | 不按模型存，按服务商存 | 同一服务商下多模型共用一把 Key，按模型存会让用户重复填 |
| 是否增加 auth CLI | 增加 `list/set/remove`，隐藏输入且不输出 Key | 无界面与安装包场景也需要完整管理闭环 |
| “上次校验时间”要不要跨重启保留 | **已决定：持久化到 `<MLA_HOME>/auth-status.json`**（只含 provider / `ok` / `checked_at` / `kind`，不含 Key） | 重启后状态行仍能显示上次校验时间（AC-23）；该文件可丢、可重建、损坏按“未校验”降级，与 `auth.json` 的凭据职责分开（第 3 章） |
| 旧库 Key 删不删 | 验证新文件后删除旧行，保留其他旧数据 | 防止数据库副本继续泄露和旧代码误读；代价在降级说明中明示 |
| 版本号与何时发布 | 升为 `0.3.0`，跑完 AC-12 后按发布流程执行 | 默认路径、来源优先级、内部模型构造和旧凭据清理均有行为变化，不宜作补丁版 |

仍需外部补充但不阻塞当前设计的事项：用户确认的 “dsh” 项目链接；两个代码副本实施前的具体提交基线对照结果。除此之外，本规格没有以未验证数字或假想需求作为验收依据。
