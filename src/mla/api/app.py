"""FastAPI 应用工厂（规格 006 §1 / §4）。

- `uvicorn mla.api.app:create_app --factory` 启动；默认只绑回环地址，**不挂 CORS**；
- 启动时执行一次 `ensure_runtime_ready()`（幂等），每个请求的仓储由 `deps.get_repo` 负责关闭；
- 所有请求先经过 `_request_context`：生成 `request_id`，并在带 `Origin` 时做同源校验
  （公开写操作没有 CSRF token，同源就是它们唯一的跨站防线）；
- 核对链路是异步的：进程内一个 `JobStore`（`ThreadPoolExecutor(max_workers=1)`），
  job 与会话只放内存，重启即失效（006 §2 / §4）；
- 未知 `/api/*` 一律 006 的 JSON 404；其余 GET 交给前端构建产物，深层路由回退 `index.html`
  （006 §1「生产：/api + /assets + 非 /api 的 SPA fallback」，AC-09）。

开发态不在这里做静态托管或代理：后端只提供 `/api`，页面由 `npm --prefix web run dev` 提供，
Vite 把 `/api` 代理到 `http://127.0.0.1:8000`（`changeOrigin: false`，保证 `Origin` 与 `Host`
一致，不被 `_same_origin` 拒绝）。生产态同源托管 `web/dist`：`/assets` 是 Vite 的资源目录，
`/logo.svg` 这类 `public/` 文件按原路径直出，其余路径回退 `index.html`。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from . import deps
from .errors import CSRF_FAILED, NOT_FOUND, error_response, install_error_handlers, new_request_id
from .jobs import JobStore
from .routes import admin, auth, checks, contracts, enterprise, history, report, settings, setup
from .sessions import DEFAULT_ABSOLUTE, DEFAULT_IDLE, SessionConfig

#: 只绑定回环地址：本机以外的访问一律不开放（006 §4）。
DEFAULT_HOST = "127.0.0.1"
API_PREFIX = "/api"

#: 显式指定前端构建目录（部署到别的路径、或测试注入临时目录）。
STATIC_DIR_ENV = "MLA_STATIC_DIR"

#: 这些前缀永远属于 API / 文档，不做 SPA 回退。`docs` / `redoc` / `openapi.json` 是 FastAPI
#: 默认关闭的文档端点：保留 JSON 404，避免被前端回退掩盖成 200（006 未决问题 9）。
RESERVED_PREFIXES: tuple[str, ...] = ("api", "docs", "redoc", "openapi.json")

#: 构建产物缺失时的提示：说清「缺什么、怎么补」，不抛栈、不白屏。
MISSING_BUILD_HINT = (
    "未找到前端构建产物（index.html）。请先执行："
    "npm --prefix web ci && npm --prefix web run build，"
    "或用 MLA_STATIC_DIR 指向已有的构建目录；后端 /api 不受影响。"
)

__all__ = [
    "API_PREFIX",
    "DEFAULT_HOST",
    "RESERVED_PREFIXES",
    "STATIC_DIR_ENV",
    "create_app",
    "resolve_static_dir",
]


def create_app(
    *,
    db_path: str | None = None,
    clock: Callable[[], datetime] | None = None,
    idle_timeout: timedelta | None = None,
    absolute_timeout: timedelta | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """应用工厂：测试可注入时钟、更短的会话过期时间与静态目录（不测真实等待）。"""
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
    # 静态托管放在所有 API 路由之后：`/api/*` 先匹配路由，未命中才落到下面的回退处理。
    _install_frontend(app, resolve_static_dir(static_dir))
    return app


def resolve_static_dir(explicit: str | Path | None = None) -> Path | None:
    """定位前端构建产物，找不到返回 `None`（明确失败，不装成空目录）。

    顺序：显式参数 → `MLA_STATIC_DIR` → 包内 `static/`（wheel 安装）→ 仓库 `web/dist`
    （源码态 `npm --prefix web run build` 的默认输出）。只有含 `index.html` 的目录才算数，
    避免把一个空目录当成「已构建」。

    显式参数或环境变量**一旦给出就不再自动探测**：部署方明确指了目录却指错时，
    应当得到「未构建」的提示，而不是悄悄退回一份可能过期的 `web/dist`。
    """
    configured: list[Path] = []
    if explicit is not None:
        configured.append(Path(explicit))
    env_value = os.environ.get(STATIC_DIR_ENV, "").strip()
    if env_value:
        configured.append(Path(env_value))
    if configured:
        return next((item.resolve() for item in configured if (item / "index.html").is_file()), None)
    candidates = (
        Path(__file__).resolve().parent / "static",
        Path(__file__).resolve().parents[3] / "web" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def _is_reserved(path: str) -> bool:
    """`api` / `docs` 这类前缀不做 SPA 回退，交回 JSON 404。"""
    head = path.split("/", 1)[0]
    return head in RESERVED_PREFIXES


def _safe_static_file(root: Path, full_path: str) -> Path | None:
    """把请求路径映射到构建目录内的真实文件；越界（`..`、绝对路径）一律返回 `None`。"""
    try:
        candidate = (root / full_path).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _install_frontend(app: FastAPI, root: Path | None) -> None:
    """挂载前端：`/assets` 静态目录 + 其余 GET 的 SPA 回退。

    没有构建产物时不崩溃：`/api` 照常工作，页面请求得到 503 与一句可执行的补构建提示。
    """
    if root is None:

        @app.get("/{full_path:path}", include_in_schema=False)
        def _frontend_missing(full_path: str, request: Request) -> Response:
            if _is_reserved(full_path):
                return error_response(request, 404, NOT_FOUND, "接口不存在")
            return HTMLResponse(
                f"<h1>前端尚未构建</h1><p>{MISSING_BUILD_HINT}</p>",
                status_code=503,
            )

        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str, request: Request) -> Response:
        if _is_reserved(full_path):
            return error_response(request, 404, NOT_FOUND, "接口不存在")
        if full_path:
            found = _safe_static_file(root, full_path)
            if found is not None:
                return FileResponse(found)
        # 深层路由（`/history/T-123`）刷新时也必须拿到 SPA 的入口，而不是 404。
        return FileResponse(index, media_type="text/html")


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
