# 006：0.4.0 FastAPI + Vue 前端迁移规格

基线：v0.3.2；行号引用均以 v0.3.2 为准，按函数定位，不按行号机械修改。现状依据为 `005-result-ia.md`、`ui/app.py`、`services/`、`adapters/persist.py` 与 `domain/`。本版全量迁移后直接替换并删除 Streamlit UI，不发布中间版本。

## 目标

用 FastAPI 原样调用现有 `services/`，以 Vue 3 + Vite + Element Plus 复现全部现有页面和 005 三层结果行为；同一录制模型输入下，新接口的条目、分级、排序、话术和 Word 内容与旧界面一致，生产环境由单个 FastAPI 进程托管前端构建产物。

非目标：不换判定、不新增判定逻辑，不改“确定冲突 / 待确认”、拒答和免责声明；不为前端重写计数、影响度、确定性或排序。

## 范围

### 1. 架构与运行方式

```text
Vue Router / Element Plus（只渲染 DTO）
        │ 同源 /api，开发态走 Vite proxy
FastAPI routes ── 会话/CSRF/企业与管理端授权 ── Pydantic schemas
        ├─ presenters：CheckTask → 三层 DTO；只调用 domain 的 summarize_verdict/display_of/sort_items
        ├─ jobs：进程内 ThreadPoolExecutor(max_workers=1)，调用 run_check/run_qa/persist
        └─ services（原样）→ graph/domain（不改）/adapters（仓储语义不改）
生产：uvicorn 单进程单 worker ── /api + /assets + 非 /api 的 SPA fallback
```

```text
src/mla/api/{app,deps,schemas,presenters,jobs}.py
src/mla/api/routes/{setup,auth,enterprise,contracts,checks,history,settings,admin}.py
src/mla/api/static/                 # Vite 输出，生成物
web/{index.html,package.json,package-lock.json,vite.config.ts,tsconfig.json}
web/src/{main.ts,App.vue,api/,router/,types/,components/,views/,composables/}
tests/api/                          # 契约、权限、旧记录、导出与新旧快照
web/tests/                          # Vitest；Playwright 仅开发依赖
```

- Python 新增 `web` extra：FastAPI、uvicorn、python-multipart；Node 只用于构建/测试，不进运行包。ECharts 仅用于现有趋势图，按需引入。
- 开发态：`uvicorn mla.api.app:create_app --factory --reload --port 8000` 与 `npm --prefix web run dev`；Vite 将 `/api` 代理到 `127.0.0.1:8000`，不启用 CORS。
- 生产态：`npm --prefix web ci && npm --prefix web run build`，再以 `127.0.0.1:8000` 启动上述应用；未知 `/api/*` 返回 JSON 404，其他 GET 回退 `index.html`。
- 启动只执行一次 `ensure_runtime_ready()`；请求和任务各自使用独立 `SqliteRepository`，只补充连接关闭生命周期，不跨线程共享连接。不得多进程/多 worker 运行（内存会话与任务登记要求如此）。

### 2. API 契约

约定：前缀 `/api`；JSON 用 UTF-8，时间用 ISO-8601 字符串，可空字段显式为 `null`；`str/int/float/bool` 简写为 `s/i/f/b`。`Page<T>={items:T[],total:i,page:i,page_size:i}`。除标注“公开”的端点外均需登录；所有登录态写操作还需 CSRF；标“管理员”的端点同时校验角色。路径中的对象必须按当前用户企业或任务属主鉴权，越权统一 404。

基础 DTO：`Enterprise={enterprise_id:s,name:s,employer_type:s,staff_size:i,working_hours:s,social_insurance:s,created_at:s}`；`StatuteEntry={statute_id:s,law_name:s,article_no:s,text:s,issuing_body:s,doc_no:s,effective_date:s\|null,expiry_date:s\|null,status:s,superseded_by:s\|null,level:s,region:s,source_url:s,note:s}`；`RuleEntry={rule_id:s,name:s,field:s,op:s,threshold:f\|s,statute_id:s,unit:s,severity:s,effective_date:s\|null,note:s,when_field:s,when_op:s,when_value:f\|s,when_field2:s,when_op2:s,when_value2:f\|s,when_mode:s,evidence_span:s}`；`OperationLog={log_id:s,task_id:s,user_id:s,action:s,detail:s,created_at:s}`。

**结果 DTO（响应的唯一展示真源）**

| 类型 | 字段与类型；数组顺序均为服务端最终顺序 |
| --- | --- |
| `StatuteView` | `{id:s,found:b,label:s,text:s\|null,effective_date:s\|null,source_url:s\|null}`；`label` 为常驻法名条号，缺法源时为“法源库未找到该条文”，前端不得显示内部 ID |
| `FindingView` | `{item_id:s,path:consensus\|rule\|clause,impact:s\|null,certainty:s\|null,display_marker:s\|null,finding:s,clause_ref:s,evidence_text:s,span_preview:s,span_expansible:b,need_facts:s[],statutes:StatuteView[],process:{source_label:s,grade:s,note:s}\|null}`；模型路径由 `sort_items/display_of` 生成，旧路径由 `merge_findings` 生成；条款结论无 `item_id` 时为空串，`display_marker` 常驻“模型自提”提示 |
| `VerdictView` | `{status:risk\|partial\|clean,headline:s,risk_count:i,review_count:i,conclusion_details:s[],process_details:s[],caveat:s}`；直接来自 `summarize_verdict` 与 005 的 `split_verdict_details` |
| `LimitsView` | `{unmet_lines:s[],field_missing_lines:s[],premise_missing_lines:s[],premise_count:i,statutory_na_line:s\|null}`；每行在服务端按原模板生成，含法名条号 |
| `MissingTermsView` | `{summary:s,short_note:s\|null,written_count:i\|null,absent_count:i,unseen_count:i,manual_count:i,items:{term_id:s,name:s,status:s,text:s,statute:StatuteView}[],manual_names:s[]}`；`written_count` 仅过程开关开启时非空，缺失/未见/人工计数始终独立 |
| `SuggestionView` | `{text:s,citation_text:s,display_disclaimer:s\|null,sources:StatuteView[]}`；正文与依据常驻，回退免责声明不重复 |
| `ProcessView` | `{cited_statute_count:i,latency_s:f\|null,latency_text:s,details:s[],finding_help:s,clause_review_note:s\|null,verify_ok:b,traceability_text:s,ask_rounds:i,ask_text:s,degraded_visible:b,extract_note:s\|null,questions:s[],task_id:s,token_in:i,token_out:i,run_detail:s}`；缺耗时保持 `null`，不得伪造 0；`finding_help` 按新旧路径复用原帮助文案 |
| `TaskDetail` | `{task:{task_id:s,enterprise_id:s,contract_id:s,contract_name:s,contract_chars:i,created_at:s\|null,model:s,model_first:b,clause_review:b},verdict:VerdictView,findings:FindingView[],limits:LimitsView,missing_terms:MissingTermsView,suggestions:SuggestionView[],suggestion_empty_text:s\|null,facts:{label:s,value:s}[],cases:{note:s,summary:s,items:{title:s,holding:s,source:s,dispute_type:s,source_url:s\|null}[]},process:ProcessView\|null,refusal:{layer:s,reason:s,message:s,guidance:s}\|null,workflow:Workflow,disclaimer:s}` |
| `Workflow` | `{state:awaiting_facts\|ready\|history,effective_contract_id:s,ask_rounds:i,questions:s[],ask_items:{slot_id:s,question:s,source:s,hint:s\|null}[],optional_fields:{field_id:s,label:s,hint:s\|null,question:s\|null}[]}`；可选字段由服务端 `candidate_fields+suggest_optional` 生成并按任务缓存，历史态不返回可选补充或问答能力 |
| `QaResult` | `{task_id:s,parent_task_id:s,refusal:TaskDetail.refusal\|null,empty_text:s\|null,errors:s[],sources:StatuteView[],suggestions:SuggestionView[],process:{agent_trace:s[],token_in:i,token_out:i,detail_text:s}\|null}` |
| `HistorySummary` | `{task_id:s,contract_name:s,created_at:s\|null,verdict:VerdictView,suggestion_count:i,latency_s:f\|null,latency_text:s,refused:b,verify_ok:b,degraded_visible:b,ask_rounds:i}` |

过程开关关闭时 `TaskDetail.process=null`、`VerdictView.process_details=[]` 且所有 `FindingView.process=null`；历史摘要的过程明细也为空。切换开关后只重新取详情，不重跑核对、不改任务，前端不补算。第一层的条号、建议、限制提示和第二层法条原文/事实/类案始终返回，折叠只影响 UI；第三层关闭是条件渲染，不是 CSS 隐藏。

**端点**

| 方法、路径与鉴权 | 请求体/参数 | 成功响应 |
| --- | --- | --- |
| `GET /setup`（公开） | — | `{needs_admin:b,runtime:{copied_from:s\|null,db_path:s,warnings:s[],migration_status:s,messages:s[]}}`；运行/迁移提示由前端每会话只显示一次 |
| `POST /setup/admin`（公开，仅无管理员） | `{username:s,password:s,confirm_password:s,enterprise_name:s}` | `201 AuthState`；已有管理员则 409 |
| `POST /auth/login`、`POST /auth/register`（公开） | 登录 `{username,password}`；注册加 `{enterprise_name}`，密码规则不变 | `200/201 AuthState={user:UserView,csrf_token:s,model_configured:b}`；`UserView={user_id,username,role,enterprise_id,onboarded}` |
| `GET /auth/me`、`POST /auth/logout`、`POST /auth/onboarding` | 后两者无体 | 分别 `AuthState`、`204`、`UserView`；`/me` 每次重读用户与模型状态 |
| `PUT /auth/password` | `{current_password:s,new_password:s,confirm_password:s}` | `204`；旧密码、长度、相同密码校验沿用 `change_password` |
| `GET/PUT/DELETE /enterprise` | PUT 为完整 `{name,employer_type,staff_size:i,working_hours,social_insurance}` | `Enterprise|null` / `Enterprise`（含 `enterprise_id,created_at`），首次保存绑定当前账号；`DELETE /enterprise` 为 204，合同同删、历史保留 |
| `GET /contracts` | `q:s,page=1,page_size=50` | `Page<ContractSummary>` 加 `total_all`；摘要 `{contract_id,filename,char_count:i,uploaded_at}`，文件名已规范化 |
| `POST /contracts/parse` | multipart `file`（txt/docx/文本型 pdf，上限 200 MiB）**或** JSON `{filename:s,text:s}`；只解析不存档 | `{filename:s,text:s,char_count:i}`；核对成功后按现逻辑自动存档 |
| `GET/DELETE /contracts/{contract_id}` | — | `{contract_id,enterprise_id,filename,text,uploaded_at}` / `204` |
| `POST /checks` | `{text:s\|null,contract_name:s\|null,contract_id:s\|null,clause_review:b\|null}`，二选一；企业事实由服务端读取 | `202 Job`；首次调用保持 `save=False,max_ask_rounds=1`，新合同按 `_check_form` 存档 |
| `GET /jobs/{job_id}` | 仅任务属主 | `{job_id:s,kind:check\|recheck\|qa,status:queued\|running\|succeeded\|failed,task_id:s\|null,result:TaskDetail\|QaResult\|null,error:ErrorBody\|null}` |
| `GET /tasks/{task_id}` | 当前内存任务或本企业历史任务 | `TaskDetail`；`include_process` 由服务端读取全局设置，客户端不得覆盖 |
| `POST /tasks/{task_id}/answers` | `{answers:{slot_id:s:value:s},skip:b}` | `202 Job`；提交则合并事实重跑，跳过则 `persist` 原任务，均保持原合同与条款开关 |
| `POST /tasks/{task_id}/optional-facts` | `{facts:{field_id:s:value:s}}` | `202 Job`；仅允许返回过的 `optional_fields`，按原合同、`ask_rounds` 重跑 |
| `POST /tasks/{task_id}/qa` | `{question:s}`；上下文由服务端按 `_qa_context` 生成 | `202 Job<QaResult>`；不得接收合同正文或客户端注入上下文；缺模型门禁沿用 `run_qa/_qa_requires_model`，纯规则越界拒答不转成 409 |
| `GET /history` | `filter=all\|risk\|degraded,page=1,page_size=20` | `Page<HistorySummary>` 加 `{deleted_count:i,window_size:200}`；筛选在最近 200 次核对内由服务端以 `summarize_verdict/_show_degraded_badge` 完成 |
| `GET /history/trend`、`DELETE /history/{task_id}` | — | `{recent:i,average:f,max:i,window_size:i,note:s,points:{order:i,label:s,date:s,risk_item_count:i,suggestion_count:i}[]}` / `204` 软删 |
| `GET /tasks/{task_id}/report` | 当前任务或本企业历史任务 | DOCX 字节流，原 MIME 与 `核对报告-{task_id}.docx`；直接 `build_report_docx` |
| `GET/PUT /settings/ui` | PUT `{show_query_path:b}` | `{show_query_path:b}`；所有角色可用，分别写 "1"/"0"，非法旧值读取为关且不回写 |
| `GET /settings/model`（管理员） | — | `{providers:{provider:s,display_name:s,configured:b,source:s,label:s}[],model:s,status_line:s,check:{ok:b,checked_at:s,kind:s,stale:b}\|null,embedding:{model:s,configured:b,fallback_text:s}}`，绝无 Key/长度/片段 |
| `PUT /settings/model`（管理员） | `{provider:s,key:s}` | 先 `probe_models`，通过才 `set_key` 并记录状态；返回上一端点形状，输入永不回显 |
| `POST /settings/model/{provider}/test`、`DELETE /settings/model/{provider}`（管理员） | 无体 | 测试当前生效 Key 并记录状态，返回模型状态；清除只删文件条目，返回最新状态 |
| `GET /admin/users`（管理员） | — | `AdminUser[]={user_id,username,role,enterprise_id,created_at,onboarded}`，不含哈希/盐 |
| `PATCH /admin/users/{user_id}/role`（管理员） | `{role:用户\|管理员}` | `AdminUser`；保留现有提升/降级语义，每次请求重读角色 |
| `DELETE /admin/users/{user_id}`（管理员） | `{confirm_username:s}` | `{deleted:b,username:s,contracts:i,tasks:i,logs:i,reason:s}`，沿用 `delete_account` 全部保护 |
| `GET /admin/statutes`（管理员） | `q,page,page_size`（默认 200） | `Page<StatuteEntry>` 加 `total_all`，保留法名/条号/正文过滤 |
| `GET /admin/rules`（管理员） | — | `{rules:RuleEntry[],slots:{slot_id,name,type,question,needed_by:s[]}[]}` |
| `GET /admin/logs`（管理员） | `action=全部\|核对\|问答,q,page,page_size`（上限 500） | `Page<OperationLog>` 加 `total_all`；`GET /admin/logs/export` 同参数返回 UTF-8-BOM CSV |
| `GET /admin/evaluation`（管理员） | — | `{available:b,message:s\|null,case_count:i,generated_at:s,variants:{variant:s,metrics:{key:s,label:s,value:f\|i,threshold:f\|null,passed:b\|null}[],mismatches:object[]}[],history:{time:s,case_count:i,variant:s,accuracy:f\|null,recall:f\|null,passed_text:s}[]}`；只读现有 JSON/JSONL，历史最多 10 次 |

**错误约定**：`ErrorBody={code:s,message:s,details:object\|null,action:object\|null}`；非 2xx 一律 `{error:{code:s,message:s,details:object\|null,action:{type:open_settings\|contact_admin\|retry\|none,label:s}\|null},request_id:s}`。400 `VALIDATION_ERROR`（含字段错误）/业务校验，401 `UNAUTHENTICATED`，403 `FORBIDDEN|CSRF_FAILED`，404 `NOT_FOUND`，409 `STATE_CONFLICT`，413 `PAYLOAD_TOO_LARGE`，415 `UNSUPPORTED_FILE_TYPE`，422 `FILE_PARSE_ERROR|MODEL_KEY_INVALID`，502 `MODEL_PROBE_FAILED`，500 `INTERNAL_ERROR`。`NoModelError` 在入队前和 worker 中均映射为 **409 `NO_MODEL`**，`message` 原样采用 `NO_MODEL_MESSAGE/NO_MODEL_QA_MESSAGE`，管理员动作指向设置页，普通用户动作指向联系管理员；不得生成残缺任务。越界/无依据拒答是成功结果中的 `refusal/empty_text`，不是 HTTP 异常。

核对采用异步：条款审查常需几十秒，202 + 轮询可避免 HTTP 长连接中断，并提供 `queued/running/failed` 可见状态。单 worker 是有意的单机串行取舍，不做并发优化；会话/任务登记仅内存保存，进程重启后未知 job 返回 404，界面提示重新提交，已持久化历史不受影响。

### 3. 页面清单与路由（与 Streamlit 逐条对应）

| 新路由/组件 | 依赖 API | 现有对应 |
| --- | --- | --- |
| `/setup` | setup、setup/admin | `_init_screen` 首次管理员初始化 |
| `/login`（登录/注册页签、忘记密码说明） | setup、login、register | `_login_screen`、`_forgot_password_help` |
| 全局首次引导对话框 | me、onboarding | `_onboarding_dialog`；未确认每次登录仍出现 |
| `/check` | me、contracts、parse、checks、jobs、tasks、answers、optional-facts、qa、report、settings/ui | `_page_check`、`_collect_input`、`_check_form`、`_ask_panel`、`_render_optional_facts`、`_render_task`、`_render_qa_panel`；无模型时不渲染输入/核对/追问 |
| `/enterprise` | enterprise 三端点 | `_page_enterprise`，含必选校验与删除确认 |
| `/contracts` | contracts 列表/读取/删除 | `_page_contracts`，含文件名/正文搜索、原文与删除确认 |
| `/history` | history、trend、删除 | `_page_history` 列表、筛选、软删、统计趋势；分页不改变最近 200 条口径 |
| `/history/{task_id}` | tasks、report、settings/ui | `_page_history` 中 `_render_task(task,repo)`；无可选补充和问答 |
| `/settings` | settings/ui、model、password | `_page_settings`；管理员顺序模型/界面/账号，普通用户界面/账号 |
| `/admin/users` | users、role、删除 | `_page_admin` 用户页签 |
| `/admin/statutes`、`/admin/rules` | statutes、rules | `_page_admin` 法源库、规则表与必问事实清单页签 |
| `/admin/logs` | logs、logs/export | `_page_admin` 操作日志页签与 CSV |
| `/admin/evaluation` | evaluation | `_render_eval` 只读评测、消融、不一致样本与历史记录 |

侧栏保留 `NAV` 分组：工作台=合同核对；管理=企业档案/合同管理/历史记录/设置；管理端仅管理员。`page_header` 只显示标题，用户名/角色与退出仍在侧栏底部。

路由守卫只负责体验，FastAPI 授权才是边界；普通用户不显示模型设置和管理端，直接访问同样 403/404。

### 4. 认证与安全边界

- 采用**不透明随机会话 ID + 服务端内存会话**，不用 JWT/Starlette 客户端 cookie。`secrets.token_urlsafe(32)` 生成 256 位随机会话 ID，登录成功轮换会话；无需持久签名密钥。cookie `mla_session` 设 `HttpOnly; SameSite=Lax; Path=/`，本地 HTTP 默认不加 Secure，仅显式 `MLA_COOKIE_SECURE=1` 且 HTTPS 时开启。
- 会话 30 分钟空闲过期、12 小时绝对过期；进程重启全部失效。每次请求按 `user_id` 重读用户，角色调整、改密和删号立即使权限变化/会话失效。
- 登录响应与服务端会话保存独立随机 CSRF token；登录态写操作要求 `X-CSRF-Token` 常量时间匹配；公开写操作不检查 token，所有请求携带 `Origin` 时必须同源。token 只存 SPA 内存，不写 localStorage；开发代理与生产均同源，不开放跨域。
- 继续调用 `services/auth.py` 的 PBKDF2-HMAC-SHA256（120,000 次、随机盐、常量时间比较），数据库口令格式不变。日志、响应和异常不得含密码、哈希、盐、模型 Key。
- 合同、结论与问答文本一律按纯文本渲染，禁用 `v-html`；来源链接仅允许 http(s) 并带 `rel=noopener`。
- 默认仅绑定回环地址。威胁模型与 002 一致：防浏览器跨站请求及其他操作系统用户的默认越权，不防同操作系统用户进程、本机管理员、SYSTEM 或已控制会话的恶意软件；不宣称网页会话能保护本机 `auth.json`/SQLite。

### 5. 数据与构建迁移

- `users/enterprises/contracts/tasks/logs/settings` 六表结构、任务 JSON、软删和企业隔离均不变；无表/列迁移，旧记录缺字段按无处理、不回填不重算。
- `settings` **无新增键**：沿用 `ui.show_query_path`，仅 "1" 为开；会话、CSRF、job 不落 settings，模型 Key 仍只在环境变量或 `auth.json`。
- `web/` 源码、lockfile 入 git；`web/node_modules/` 与 `src/mla/api/static/` 不入 git。静态产物随 wheel 打包，构建 wheel/exe 前必须先跑前端构建；不得把 `node_modules` 打入包。
- 删除 Streamlit UI 后移除其专用依赖与 AppTest；领域/服务测试保留，UI 断言先迁移到 API/Vue。`.streamlit/secrets.toml.example` 的 embedding 指引仍保留，不能随 UI 一并删除。

### 6. 不做什么

不改 `domain/`、图、规则、法源、提示词、模型调用、`services/tasks.py` 的判定编排与数据模型；不做多用户并发优化、云部署、开放 CORS、移动端适配、OCR、用户级界面偏好或中间版本发布；不在前端实现 `summarize_verdict/sort_items/merge_findings`，不借迁移修模型漏检/幻觉，不删除 embedding 的 secrets 兼容。

## 验收标准

| # | 可测断言 | 命令或观察 |
| --- | --- | --- |
| AC-01 | 富任务默认无过程字段/DOM，条号常驻而原文、事实、类案折叠，建议不折叠，必备条款独立，免责原文最后且一次 | `python -m pytest tests/api/test_result_contract.py`；`npm --prefix web run test -- --run`；Playwright `result-ia` 用例 |
| AC-02 | 开关仅增第三层，第一二层规范化快照不变；非法值为关，写后新仓库/进程仍保持，普通用户可用 | `python -m pytest tests/api -k query_path`；Playwright 重启持久化用例 |
| AC-03 | 同一合同与录制模型响应下，旧渲染快照与 API 的条目 ID、正文、影响度、确定性、四键排序逐字段一致；模型主导和旧路径、旧记录均覆盖 | `python -m pytest tests/api/test_parity.py tests/test_verdict.py tests/test_grading.py` |
| AC-04 | 无模型时服务未入队、无任务落库，409/NO_MODEL 文案与角色动作正确；页面无输入/核对/追问 | `python -m pytest tests/api/test_no_model.py tests/test_no_model_gate.py`；Playwright 两种角色用例 |
| AC-05 | 同一任务 API 与旧 `build_report_docx` 的全部段落、表格、法条、建议、免责一致，MIME/文件名正确 | `python -m pytest tests/api/test_report.py tests/test_report.py` |
| AC-06 | 普通用户看不到/调不到模型设置和管理端；跨企业合同、任务、报告均 404；问答上下文不含正文；响应无秘密 | `python -m pytest tests/api/test_auth.py tests/api/test_permissions.py tests/test_qa_privacy.py`；Playwright 菜单用例 |
| AC-07 | 初始化、注册/登录/登出、引导、企业、合同、追问/跳过、可选补充、问答、历史筛选分页软删、全部管理页签行为可走通 | `python -m pytest tests/api`；`npm --prefix web run test:e2e` |
| AC-08 | 生产首屏和核对不劣于旧界面：同机同数据预热后 20 次，中位数与 P95 均不超过旧版 1.05 倍或旧值+250ms（取较宽者） | `python scripts/benchmark_frontend.py --runs 20 --baseline-file _tmp/frontend-perf-v0.3.2.json --candidate fastapi`，输出两次原始样本与比值 |
| AC-09 | 单进程可服务 SPA/资源/API，刷新深层路由不 404，未知 API 为 JSON 404；静态产物和 lockfile 可复现 | `npm --prefix web ci`；`npm --prefix web run build`；`python -m pytest tests/api/test_static.py`；生产模式 Playwright 冒烟；CI 安装 wheel 后观察 `/`、深层路由与静态资源 |
| AC-10 | 全部现有非 Streamlit 测试与新增测试通过，静态门禁和 CI 覆盖率不降低；文案检查扩展到 Python DTO 与 Vue/TS | `python -m pytest --cov=mla --cov-report=term-missing --cov-fail-under=75`；`ruff check src tests scripts`；`python -m pyright src`；`python scripts/check_ui_text.py`；`npm --prefix web run typecheck`；依次运行 CI 的 `validate_statutes/validate_rules/validate_triggers/validate_missing_terms/validate_clause_gold/validate_elements` 六脚本 |

## 计划

| 批次（各自提交、可回滚） | 范围与产出（只动列出的文件/目录） | 验证 |
| --- | --- | --- |
| 1. 冻结基线 | 只增 `docs/design/006-frontend-migration.md`、`tests/api/fixtures/`、`tests/api/test_legacy_snapshot.py` 与 `scripts/benchmark_frontend.py`；采集旧结果/Word/权限快照，不改运行代码 | 旧快照测试通过；保存旧版 20 次性能样本到 `_tmp/frontend-perf-v0.3.2.json`。回滚：删除新增文档/测试/脚本 |
| 2. API 骨架与基础页面能力 | 增 `src/mla/api/` 的 app/deps/schemas/routes（不含 checks 的完整实现）及 `pyproject.toml`；接通 setup/auth/enterprise/contracts/settings/admin 和仓储关闭；Streamlit 保留 | `python -m pytest tests/api -k "not checks and not parity"` + ruff/pyright。回滚到批 1，数据库无迁移 |
| 3. 核对 API 全链路 | 完成 presenters/jobs/checks/history/report、异步追问/补充/QA、错误映射与 `tests/api/test_parity.py`；展示 helper 原样移入 presenters，旧 `ui/app.py` 暂时仅改为导入/re-export，不改 services/domain | AC-01…AC-07 的 API 部分及既有 `test_result_ia/test_services/test_no_model_gate/test_report/test_history_filter`。回滚：撤本批 API 与导入改动，旧 UI 仍可运行 |
| 4. Vue 基础与全部非结果页 | 增 `web/`、lockfile、路由/会话/API client，并将 logo 移至 `web/public/`；完成初始化、登录、企业、合同、设置和 5 个管理页签 | `npm --prefix web run typecheck`；Vitest + Playwright 非结果页用例；`npm --prefix web run build`。回滚：删 `web/`，API 保留 |
| 5. 结果页、历史与生产整合 | 完成 check/结果/QA/历史/趋势组件、静态托管、三层与权限 E2E、性能对比；补齐构建打包配置 | AC-01…AC-09 全部；生产模式逐路由观察。回滚：撤本批，保留已验 API/基础页 |
| 6. 切换与清理 | 更新 README/CONTRIBUTING/docs/architecture/CHANGELOG/版本；迁完 UI 断言后删除 `src/mla/ui/`、Streamlit/pandas/altair 专用依赖及 AppTest，更新 `check_ui_text.py` 和 CI | AC-10 + AC-01…AC-09 最终全跑；观察普通用户与管理员全页面。回滚：还原本批提交即恢复 Streamlit，无需数据降级 |

依赖顺序固定为“快照 → API → 前端 → 生产静态验证 → 删除旧 UI”；任何一批失败不得靠删断言通过，后续批次不得提前删除旧 UI。

### 风险与回滚

- **最高：双路径结果漂移。** 模型主导与旧路径的计数/合并/排序不同；用同一 `CheckTask` 快照同时锁住 API 和旧渲染，Presenter 只调用现有领域函数。
- **异步与重启状态。** 内存 job/会话重启即失，单 worker 又会排队；界面必须显示状态与可重试动作，禁止多 worker 掩饰问题，已落库历史作为恢复来源。
- **认证与秘密泄露。** cookie/CSRF/角色刷新、改密和 Key 保存是高风险写操作；逐端点做跨企业、普通用户、响应体和日志扫描，未登录找回仍仅本机 CLI。
- **前端构建链与体积。** lockfile 固定 Node 依赖，Element Plus/ECharts 按需加载、路由分包；CI 验证干净 `npm ci/build`，记录 wheel/静态体积且排除 `node_modules`。
- **浏览器与性能。** 支持桌面 Edge/Chrome 当前及前一主版本，不宣称移动端；生产构建下测首屏与长任务，失败可回到上一批，最终删除批可整体还原到 v0.3.2。

## 未决问题

1. `_check_form` 首次 `run_check(save=False)` 后，若 `task.questions` 为空，当前代码没有显式 `persist`；本规格为保持现状未擅自补存。需确认这是有意行为还是迁移前应单独修复的缺陷，不能用前端迁移顺手改变。
2. 网页端不提供"管理员重置他人口令"：现有 Streamlit 管理端也没有该功能，口令重置继续只走 `python -m mla admin reset`。迁移不新增此能力。
3. 性能对比需要一台固定 Windows 机器和可重复的旧版启动环境；批 1 若采不到可信旧基线，AC-08 不能宣称通过，需先确定基准机器而不能用主观感受代替。
4. **报告分项计数不自洽（既有缺陷）**：`services/report.py` 的 `judged = violated + compliant + not_applicable`，导致「可判定 + 不适用 + 无法判定」三个数之和大于规则总数。批次 3 原样继承该输出；修它属判定口径变更，需与 005 的 `verdict.details` 快照一并评估，不在 0.4.0 范围。
5. **`rule_check` 条目语义冲突（既有缺陷）**：`kind="待确认"` 而 `certainty="确定冲突"`。新前端一律以 `certainty` 与 `impact` 分组展示（与现有 `display_of` 一致），不读 `kind`；该字段含义待单独厘清。
6. **基线快照的重录纪律**：见 `tests/api/fixtures/README.md`。任何重录必须单独提交并说明触发原因，不得为让断言变绿而重录。
7. **企业档案 `created_at` 语义修正**：旧 Streamlit 每次保存都把 `created_at` 重置为当前时间，新 API 保留首次建档时间。这是有意的行为修正（字段语义应为建档时间），但会导致新旧快照在该字段上不一致，比对时需排除。
8. **最后一个管理员可被降级**：现有 `delete_account` 有"最后一个管理员不可删"保护，但改角色没有同类保护，管理员可能把自己降级后无人能进管理端。旧界面同样没有该保护，本版保持一致；是否补保护需产品单独决定。
9. **OpenAPI 文档端点默认关闭**：`docs_url/redoc_url/openapi_url` 全部置空以减少暴露面。若批次 4/5 需要用 OpenAPI 生成前端类型定义，应改为仅在开发态开启（环境变量控制），生产保持关闭。
10. **超大上传的拒绝时机**：`POST /contracts/parse` 的 200 MiB 上限在读完整请求体后才由框架判定（413），无法在流式解析前拒绝分块传输。要硬上限需自写流式解析或在前置反代限制；单机场景下当前实现可接受，接口口径按"整包上限"记录。
11. **`total_all` 是窗口内条数**：`GET /history` 与 `GET /admin/logs` 返回的 `total_all` 是各自窗口（200 条 / 500 条）内的总数，不是全库总量。前端展示时应与 `window_size` 一起解释，不能当作全量计数。
12. **UI 间接依赖 pydantic**：`ui/app.py` 从 `api.presenters` 导入展示 helper，后者依赖 `api.schemas`（pydantic）。已在 `ui` extra 声明该依赖；批次 6 删除 UI 后此依赖随之消失。若希望彻底解耦，可把共享 helper 下沉到独立模块，但考虑到 UI 即将移除，当前为最小修复。同批发现 `ui` extra 早先也缺 `python-docx`（`services/report.py` 顶层导入），已一并补上。
