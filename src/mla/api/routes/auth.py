"""认证（规格 006 §2 / §4）：登录、注册、`/me`、登出、首次引导、改口令。

会话与 CSRF 都只存服务端内存：登录/注册成功轮换一次，登出即删；`/me` 每次重读用户，
因此角色调整、改密、删号在下一次请求就生效。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from ...domain.models import Enterprise, Role
from ...services.auth import authenticate, change_password, register
from ..deps import (
    AuthDep,
    RepoDep,
    StateDep,
    issue_session,
    model_configured,
    require_capability,
)
from ..errors import UNAUTHENTICATED, VALIDATION_ERROR, ApiError
from ..schemas import AuthState, LoginIn, PasswordChangeIn, RegisterIn, UserView

router = APIRouter()


@router.post(
    "/auth/login",
    dependencies=[Depends(require_capability("login"))],
    response_model=AuthState,
)
def login(payload: LoginIn, response: Response, repo: RepoDep, state: StateDep) -> AuthState:
    """口令错误与账号不存在返回同一个 401（不泄露账号是否存在）。"""
    user = authenticate(repo, payload.username, payload.password)
    if user is None:
        raise ApiError(401, UNAUTHENTICATED, "用户名或密码不正确")
    return issue_session(state, response, user)


@router.post(
    "/auth/register",
    status_code=201,
    dependencies=[Depends(require_capability("register"))],
    response_model=AuthState,
)
def register_user(
    payload: RegisterIn,
    response: Response,
    repo: RepoDep,
    state: StateDep,
) -> AuthState:
    """自助注册：给了企业名就顺手建档案并绑定（与旧界面同一顺序：先建企业再建账号）。"""
    enterprise_id = ""
    if payload.enterprise_name.strip():
        enterprise_id = "E-" + uuid.uuid4().hex[:8]
        repo.save_enterprise(Enterprise(enterprise_id=enterprise_id, name=payload.enterprise_name.strip()))
    try:
        user = register(repo, payload.username, payload.password, Role.USER, enterprise_id)
    except ValueError as exc:
        raise ApiError(400, VALIDATION_ERROR, str(exc)) from exc
    return issue_session(state, response, user)


@router.get(
    "/auth/me",
    dependencies=[Depends(require_capability("auth_me"))],
    response_model=AuthState,
)
def me(auth: AuthDep) -> AuthState:
    """当前会话自述：用户、CSRF token（前端只存内存）、模型是否已配置。"""
    return AuthState(
        user=UserView.of(auth.user),
        csrf_token=auth.session.csrf_token,
        model_configured=model_configured(),
    )


@router.post(
    "/auth/logout",
    status_code=204,
    dependencies=[Depends(require_capability("logout"))],
)
def logout(response: Response, auth: AuthDep, state: StateDep) -> None:
    state.sessions.delete(auth.session.session_id)
    response.delete_cookie(
        key=state.config.cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
    )


@router.post(
    "/auth/onboarding",
    dependencies=[Depends(require_capability("onboarding"))],
    response_model=UserView,
)
def onboarding(auth: AuthDep, repo: RepoDep) -> UserView:
    """确认首次引导：只改落库的 `onboarded`，重复调用幂等。"""
    repo.mark_onboarded(auth.user.user_id)
    updated = repo.get_user_by_name(auth.user.username) or auth.user
    return UserView.of(updated)


@router.put(
    "/auth/password",
    status_code=204,
    dependencies=[Depends(require_capability("settings_password"))],
)
def change_own_password(
    payload: PasswordChangeIn,
    auth: AuthDep,
    repo: RepoDep,
    state: StateDep,
) -> None:
    """改口令：旧口令、长度、与原口令相同的校验全部沿用 `change_password`。

    成功后只保留当前会话（并刷新口令指纹），该账号的其他会话立即失效。
    """
    if payload.new_password != payload.confirm_password:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "两次输入的新密码不一致",
            details={"fields": ["confirm_password"]},
        )
    ok, reason = change_password(repo, auth.user, payload.current_password, payload.new_password)
    if not ok:
        raise ApiError(400, VALIDATION_ERROR, reason, details={"fields": ["current_password"]})
    updated = repo.get_user_by_name(auth.user.username) or auth.user
    state.sessions.rebind(auth.session, updated)
    state.sessions.delete_other_sessions(updated.user_id, keep=auth.session.session_id)
