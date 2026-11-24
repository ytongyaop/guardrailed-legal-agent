"""首次初始化（规格 006 §2：`GET /setup` 公开、`POST /setup/admin` 公开且仅无管理员时可用）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Response

from ...config import data_path
from ...services.auth import create_first_admin, has_admin
from ..deps import RepoDep, StateDep, issue_session, require_capability
from ..errors import NOT_FOUND, STATE_CONFLICT, VALIDATION_ERROR, ApiError
from ..schemas import AuthState, RuntimeInfo, SampleContractView, SetupAdminIn, SetupStatus

router = APIRouter()

#: 示例合同随包分发：用 `data_path` 定位（打包成 exe 后 `__file__` 相对路径不成立）。
SAMPLE_CONTRACT_FILE = Path(data_path("evalsets/sample_contract.txt"))
SAMPLE_CONTRACT_NAME = "示例劳动合同（含典型问题）"


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


@router.get(
    "/setup/sample-contract",
    dependencies=[Depends(require_capability("setup"))],
    response_model=SampleContractView,
)
def get_sample_contract() -> SampleContractView:
    """首次向导用：一份含典型缺陷的示例合同，让人空手也能跑通一次核对。

    读不到文件就是 404：向导会给出「跳过」出口，静默返回空串会让人以为核对没问题。
    """
    try:
        text = SAMPLE_CONTRACT_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiError(404, NOT_FOUND, "示例合同不可用，可跳过本步直接进入系统") from exc
    if not text.strip():
        raise ApiError(404, NOT_FOUND, "示例合同不可用，可跳过本步直接进入系统")
    return SampleContractView(name=SAMPLE_CONTRACT_NAME, text=text)


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
