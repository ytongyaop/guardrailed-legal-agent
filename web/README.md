# web：Vue 3 前端（规格 006 批次 4）

只做非结果页：初始化、登录、企业档案、合同管理、设置与 5 个管理页签。
核对、结果页、历史与生产静态托管在批次 5。

## 命令

```powershell
npm --prefix web install     # 安装依赖
npm --prefix web run dev     # 开发态（Vite :5173，/api 代理到 127.0.0.1:8000）
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build   # 产物在 web/dist
```

## 开发态约定

- 页面与接口同源：Vite 把 `/api` 代理到 FastAPI，`changeOrigin` 必须为 `false`，
  否则后端 `_same_origin` 会因 `Origin` 与 `Host` 不一致返回 403 `CSRF_FAILED`。
- 启动后端：`uvicorn mla.api.app:create_app --factory --reload --port 8000`。
- 会话与 CSRF token 只存内存；刷新页面会重新请求 `/auth/me`，不写 localStorage。
- 所有中文文案按 `scripts/check_ui_text.py` 的口径写：简体、全角标点、不用直角引号，
  `npm --prefix web run test` 里的 `ui-text.spec.ts` 会扫一遍源码。
