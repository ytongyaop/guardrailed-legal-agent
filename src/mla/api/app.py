"""FastAPI 应用工厂（规格 006 §1 / §4）。

- `uvicorn mla.api.app:create_app --factory` 启动；默认只绑回环地址，**不挂 CORS**；
- 启动时执行一次 `ensure_runtime_ready()`（幂等），每个请求的仓储由 `deps.get_repo` 负责关闭；
- 所有请求先经过 `_request_context`：生成 `request_id`，并在带 `Origin` 时做同源校验
  （公开写操作没有 CSRF token，同源就是它们唯一的跨站防线）；
- 核对链路是异步的：进程内一个 `JobStore`（`ThreadPoolExecutor(max_workers=1)`），
  job 与会话只放内存，重启即失效（006 §2 / §4）；
- 未知 `/api/*` 由框架 404 收敛成 006 的 JSON 错误体；SPA 静态托管留到批次 5。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from . import deps
from .errors import CSRF_FAILED, error_response, install_error_handlers, new_request_id
from .jobs import JobStore
from .routes import admin, auth, checks, contracts, enterprise, history, report, settings, setup
from .sessions import DEFAULT_ABSOLUTE, DEFAULT_IDLE, SessionConfig

#: 只绑定回环地址：本机以外的访问一律不开放（006 §4）。
DEFAULT_HOST = "127.0.0.1"
API_PREFIX = "/api"

__all__ = ["API_PREFIX", "DEFAULT_HOST", "create_app"]


def create_app(
    *,
    db_path: str | None = None,
    clock: Callable[[], datetime] | None = None,
    idle_timeout: timedelta | None = None,
    absolute_timeout: timedelta | None = None,
) -> FastAPI:
    """应用工厂：测试可注入时钟与更短的会话过期时间（不测真实等待）。"""
    config = SessionConfig(
        idle=DEFAULT_IDLE if idle_timeout is None else idle_timeout,
        absolute=DEFAULT_ABSOLUTE if absolute_timeout is None else absolute_timeout,
    )
    app = FastAPI(
        title="小微企业用工风险辅助系统 · API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # 启动一次：运行目录引导 + 会话表。放在工厂里而不是 lifespan 里，是为了让
    # `TestClient(app)` 不进入上下文管理器时行为也一致（批次 3 之前不会用到 lifespan）。
    app.state.api = deps.build_api_state(db_path=db_path, clock=clock, config=config)
    state = app.state.api
    # 核对链路的内存任务登记：单 worker、进程重启即失（006 §2「核对采用异步」）。
    app.state.jobs = JobStore(db_path=state.db_path, paths=state.paths)

    @app.middleware("http")
    async def _request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = new_request_id()
        if not _same_origin(request):
            return error_response(
                request,
                403,
                CSRF_FAILED,
                "请求来源与本站不一致，已拒绝",
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    install_error_handlers(app)
    for module in (setup, auth, enterprise, contracts, settings, admin, checks, history, report):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


def _same_origin(request: Request) -> bool:
    """带 `Origin` 时必须与本站同源；不带（本机脚本、测试客户端）则放行。

    只比较 `scheme://host:port`：开发态 Vite 代理与生产都由同一个源提供页面，
    所以不存在"合法的跨源"场景，跨源一律拒绝。
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    host = request.headers.get("host") or request.url.netloc
    return parts.netloc.lower() == host.lower()
