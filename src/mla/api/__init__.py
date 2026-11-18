"""Web 层（规格 006）：FastAPI 同源 API，只调用现有 `services/`，不改判定与仓储语义。

分层与职责：

- `app.py`：应用工厂、请求上下文中间件（request_id / 同源校验）、错误处理装配；
- `deps.py`：每请求独立 `SqliteRepository`（请求结束关闭）、会话解析、能力矩阵鉴权；
- `sessions.py`：服务端内存会话 + 独立 CSRF token（不落库、不落盘）；
- `permissions.py`：`tests/api/fixtures/permissions.json` 的能力矩阵，鉴权由它驱动；
- `errors.py`：006 §2 的统一错误体 `{error:{code,message,details,action},request_id}`；
- `schemas.py`：请求 / 响应 DTO；
- `routes/`：setup / auth / enterprise / contracts / settings / admin，另有 `stubs.py` 占位
  批次 3 的核对与历史端点（先注册、统一 501，鉴权口径与真实实现相同）。

不做什么：不开放 CORS、不托管前端静态产物（批次 5）、不实现核对全链路（批次 3）。
"""
