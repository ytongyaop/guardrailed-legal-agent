"""请求级依赖：每请求独立仓储、会话解析、能力矩阵鉴权、CSRF。

三条硬约束（规格 006 §1 / §4）：

1. **每个请求独立 `SqliteRepository`，请求结束关闭连接**：`get_repo` 是生成器依赖，
   FastAPI 在响应完成后执行 `finally`；不跨请求、不跨线程共享连接对象；
2. **每次请求按 `user_id` 重读用户**：会话里只放 ID 与口令指纹，角色调整 / 改密 / 删号
   立即生效，不需要额外的失效表；
3. **登录态写操作必须带 `X-CSRF-Token`**，常量时间比较；公开写操作不校验 token
   （同源 Origin 由 `app._request_context` 中间件对所有请求统一校验）。

依赖一律写成 `Annotated[T, Depends(...)]` 别名：既是 FastAPI 现行推荐写法，也让
`ruff` 的 B008（"不要在默认参数里调函数"）保持干净，不必为框架放宽规则。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Response

from ..adapters.persist import SqliteRepository
from ..config import Paths, env_flag, load_config
from ..domain.models import User
from ..services.auth import is_admin
from ..services.bootstrap import BootstrapReport, ensure_runtime_ready
from ..services.credentials import CredentialError, resolve_llm_config
from .errors import (
    CSRF_FAILED,
    FORBIDDEN,
    INTERNAL_ERROR,
    UNAUTHENTICATED,
    ApiError,
    make_action,
)
from .permissions import capability
from .schemas import AuthState, UserView
from .sessions import SessionConfig, SessionRecord, SessionStore, credential_fingerprint

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"


@dataclass(frozen=True)
class ApiState:
    """进程级状态：启动一次、全请求共享（但会话内容仍按请求校验）。"""

    bootstrap: BootstrapReport
    db_path: str
    paths: Paths
    config: SessionConfig
    sessions: SessionStore
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    session: SessionRecord


def build_api_state(
    *,
    db_path: str | None = None,
    clock: Callable[[], datetime] | None = None,
    config: SessionConfig | None = None,
) -> ApiState:
    """应用启动时调用一次：引导运行目录（幂等），并准备会话表。"""
    report = ensure_runtime_ready()
    cfg = load_config()
    session_config = config or SessionConfig(secure=env_flag("MLA_COOKIE_SECURE"))
    now = clock or datetime.now
    return ApiState(
        bootstrap=report,
        db_path=db_path or report.db_path,
        paths=cfg.paths,
        config=session_config,
        sessions=SessionStore(now, session_config),
        clock=now,
    )


def get_state(request: Request) -> ApiState:
    state = getattr(request.app.state, "api", None)
    if not isinstance(state, ApiState):
        raise ApiError(500, INTERNAL_ERROR, "服务尚未完成初始化，请重启后重试")
    return state


def get_repo(request: Request) -> Iterator[SqliteRepository]:
    """每请求一个仓储实例，响应结束后关闭连接（异常路径同样关闭）。"""
    state = get_state(request)
    repo = SqliteRepository(paths=state.paths, db_path=state.db_path)
    try:
        yield repo
    finally:
        repo.close()


StateDep = Annotated[ApiState, Depends(get_state)]
RepoDep = Annotated[SqliteRepository, Depends(get_repo)]


def resolve_session(request: Request, repo: SqliteRepository) -> AuthenticatedUser | None:
    """按 cookie 还原会话：过期即弃、用户不存在即弃、口令指纹变化即弃。"""
    state = get_state(request)
    session_id = request.cookies.get(state.config.cookie_name) or ""
    record = state.sessions.lookup(session_id)
    if record is None:
        return None
    user = next((u for u in repo.list_users() if u.user_id == record.user_id), None)
    if user is None or credential_fingerprint(user) != record.fingerprint:
        state.sessions.delete(record.session_id)
        return None
    return AuthenticatedUser(user=user, session=record)


def current_user(request: Request, repo: RepoDep) -> AuthenticatedUser:
    authed = resolve_session(request, repo)
    if authed is None:
        raise ApiError(401, UNAUTHENTICATED, "登录状态已失效，请重新登录")
    return authed


AuthDep = Annotated[AuthenticatedUser, Depends(current_user)]


def admin_user(auth: AuthDep) -> AuthenticatedUser:
    """管理端门禁：非管理员 403（不是 404）——管理入口存在性不是秘密，隐藏的是数据。"""
    if not is_admin(auth.user):
        raise ApiError(
            403,
            FORBIDDEN,
            "需要管理员权限",
            action=make_action("contact_admin", "联系管理员"),
        )
    return auth


AdminDep = Annotated[AuthenticatedUser, Depends(admin_user)]


def enforce_csrf(request: Request, session: SessionRecord) -> None:
    """登录态写操作：缺 token 与 token 不符都返回 403 `CSRF_FAILED`，常量时间比较。"""
    provided = request.headers.get(CSRF_HEADER) or ""
    if not provided:
        raise ApiError(403, CSRF_FAILED, "缺少 CSRF token，写操作已被拒绝")
    if not secrets.compare_digest(provided.encode(), session.csrf_token.encode()):
        raise ApiError(403, CSRF_FAILED, "CSRF token 不匹配，请刷新页面后重试")


def require_capability(name: str) -> Callable[..., None]:
    """按 `permissions.py` 的能力矩阵生成路由级依赖。

    - 公开能力：只要同源（中间件已校验），不要求登录、不要求 token；
    - 登录态能力：401 或 403 `FORBIDDEN`；写方法再校验 CSRF；
    - 管理能力：在登录态基础上要求管理员角色。
    """
    cap = capability(name)

    if cap.anonymous:

        def _anonymous_guard() -> None:
            return None

        return _anonymous_guard

    if cap.admin:

        def _admin_guard(request: Request, auth: AdminDep) -> None:
            _guard_write(request, auth)

        return _admin_guard

    def _user_guard(request: Request, auth: AuthDep) -> None:
        _guard_write(request, auth)

    return _user_guard


def _guard_write(request: Request, auth: AuthenticatedUser) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    enforce_csrf(request, auth.session)


def model_configured() -> bool:
    """当前是否有可用模型 Key（只看「有没有」，不读、不回显 Key 本身）。"""
    try:
        llm, _resolved = resolve_llm_config()
    except CredentialError:
        # 凭据文件损坏按「未配置」呈现：设置页会显式报错，不在这里抛 500
        return False
    return llm is not None


def issue_session(state: ApiState, response: Response, user: User) -> AuthState:
    """登录 / 注册 / 首次初始化共用：新建会话、下发 cookie、返回 AuthState。"""
    record = state.sessions.create(user)
    response.set_cookie(
        key=state.config.cookie_name,
        value=record.session_id,
        max_age=int(state.config.absolute.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
        secure=state.config.secure,
    )
    return AuthState(
        user=UserView.of(user),
        csrf_token=record.csrf_token,
        model_configured=model_configured(),
    )
