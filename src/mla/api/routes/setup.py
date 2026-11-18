"""首次初始化（规格 006 §2：`GET /setup` 公开、`POST /setup/admin` 公开且仅无管理员时可用）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...services.auth import create_first_admin, has_admin
from ..deps import RepoDep, StateDep, issue_session, require_capability
from ..errors import STATE_CONFLICT, VALIDATION_ERROR, ApiError
from ..schemas import AuthState, RuntimeInfo, SetupAdminIn, SetupStatus

router = APIRouter()


@router.get(
    "/setup",
    dependencies=[Depends(require_capability("setup"))],
    response_model=SetupStatus,
)
def get_setup(state: StateDep, repo: RepoDep) -> SetupStatus:
    """启动引导结果 + 是否需要首次初始化。公开：登录页要用它决定显示哪块。"""
    report = state.bootstrap
    return SetupStatus(
        needs_admin=not has_admin(repo),
        runtime=RuntimeInfo(
            copied_from=report.copied_from,
            db_path=report.db_path,
            warnings=list(report.warnings),
            migration_status=report.migration.status,
            messages=list(report.migration.messages),
        ),
    )


@router.post(
    "/setup/admin",
    status_code=201,
    dependencies=[Depends(require_capability("setup_admin"))],
    response_model=AuthState,
)
def post_setup_admin(
    payload: SetupAdminIn,
    response: Response,
    repo: RepoDep,
    state: StateDep,
) -> AuthState:
    """建首个管理员并直接登录；已有管理员返回 409（不静默忽略，也不覆盖）。"""
    if has_admin(repo):
        raise ApiError(409, STATE_CONFLICT, "管理员账号已存在，请直接登录")
    if payload.password != payload.confirm_password:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "两次输入的密码不一致",
            details={"fields": ["confirm_password"]},
        )
    try:
        user = create_first_admin(repo, payload.username, payload.password, payload.enterprise_name)
    except ValueError as exc:
        # 用户名过短 / 重名 / 口令过短：沿用 services/auth 的文案与口径
        raise ApiError(400, VALIDATION_ERROR, str(exc)) from exc
    return issue_session(state, response, user)
