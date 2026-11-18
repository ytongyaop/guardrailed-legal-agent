# web：Vue 3 前端（规格 006 批次 5）

页面与后端的分工：前端只渲染 API 给的 DTO，不做判定、计数、分级或排序。

- 批次 4：初始化、登录、企业档案、合同管理、设置与 5 个管理页签；
- 批次 5：合同核对（异步任务 + 三层结果 + 追问/可选补充/问答）、历史列表与只读详情、
  趋势图（ECharts 按需引入）、生产静态托管与 E2E。

## 命令

```powershell
npm --prefix web install      # 安装依赖（CI 用 npm ci）
npm --prefix web run dev      # 开发态（Vite :5173，/api 代理到 127.0.0.1:8000）
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build    # 产物在 web/dist
npm --prefix web run test:e2e # Playwright（真实后端进程 + 本机 Edge）
```

## 开发态约定

- 页面与接口同源：Vite 把 `/api` 代理到 FastAPI，`changeOrigin` 必须为 `false`，
  否则后端 `_same_origin` 会因 `Origin` 与 `Host` 不一致返回 403 `CSRF_FAILED`。
- 启动后端：`uvicorn mla.api.app:create_app --factory --reload --port 8000`。
- 会话与 CSRF token 只存内存；刷新页面会重新请求 `/auth/me`，不写 localStorage。
- 核对结果通过 URL 的 `task_id` 恢复：刷新或分享链接不会重复调用模型；「已跳过追问」只用 sessionStorage 记任务号，不存结果内容。
- 所有中文文案按 `scripts/check_ui_text.py` 的口径写：简体、全角标点、不用直角引号。
  门禁脚本直接扫 `web/src/**/*.{vue,ts,css}`（跳注释与 `components.d.ts`），与本目录的
  `npm --prefix web run test` 互补——文案规则只有这一份实现。

## 三层结果的实现位置

| 层 | 组件 | 规则 |
| --- | --- | --- |
| 第一层 结论 | `components/result/ResultView.vue`、`FindingCard.vue`、`MissingTermsBlock.vue` | 结论句/计数、条目（条号常驻）、限制提示、必备条款独立计数、处理建议正文，全部常驻 |
| 第二层 依据 | `components/result/FoldBox.vue`、`StatuteFold.vue` | 法条原文/事实/类案用原生 `<details>`，默认不展开 |
| 第三层 过程 | `components/result/ProcessLayer.vue` | **只在 `detail.process !== null` 时挂载**（`v-if`），不是渲染后隐藏 |

过程开关是全局设置（设置页「显示查询路径与判定来源」），由服务端读取并在 DTO 里体现；
前端不缓存、不补算。去设置页打开开关再回到核对页，页面会重新 `GET /tasks/{id}` 拿同一次核对。

## E2E 说明

`playwright.config.ts` 会拉起两个后端进程（`scripts/e2e_server.py`）：

- `8021`：注入占位 Key + 打桩模型，核对链路可以离线跑通；
- `8022`：不给 Key，用来验证 409 `NO_MODEL` 与页面门禁。

浏览器用本机 **Edge**（`channel: 'msedge'`）：本机下载 Chromium 受网络限制，
`npx playwright install chromium` 无法完成；Edge 与 Chromium 同内核，E2E 仍跑在真实浏览器里。
需要换浏览器时设置 `MLA_PLAYWRIGHT_CHANNEL`（见配置文件注释）。
