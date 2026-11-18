"""企业档案（规格 006 §2）：读写当前账号绑定的企业；首次保存即绑定。

三项必选（用工主体类型 / 工时制度 / 是否缴纳社保）与旧界面同一口径：没选就不保存，
用 400 + `details.fields` 指明缺哪一项，不让默认值蒙过去。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from ...domain.models import Enterprise
from ..deps import AuthDep, RepoDep, require_capability
from ..errors import INTERNAL_ERROR, VALIDATION_ERROR, ApiError
from ..schemas import EnterpriseIn, EnterpriseView

router = APIRouter()

# 三项必填的字段名与中文名：与旧界面 `ENTERPRISE_REQUIRED_FIELDS` 一致
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("employer_type", "用工主体类型"),
    ("working_hours", "工时制度"),
    ("social_insurance", "是否缴纳社保"),
)


@router.get(
    "/enterprise",
    dependencies=[Depends(require_capability("enterprise"))],
    response_model=EnterpriseView | None,
)
def get_enterprise(auth: AuthDep, repo: RepoDep) -> EnterpriseView | None:
    """未绑定企业或档案已删都返回 `null`，不返回 404：这是"自己的档案还没填"。"""
    if not auth.user.enterprise_id:
        return None
    ent = repo.get_enterprise(auth.user.enterprise_id)
    return None if ent is None else EnterpriseView.of(ent)


@router.put(
    "/enterprise",
    dependencies=[Depends(require_capability("enterprise"))],
    response_model=EnterpriseView,
)
def put_enterprise(payload: EnterpriseIn, auth: AuthDep, repo: RepoDep) -> EnterpriseView:
    """完整覆盖保存；首次保存生成企业 ID 并绑定当前账号。

    `created_at` 沿用已有值：覆盖保存不该把"建档时间"改成"最后修改时间"。
    """
    missing = [
        (field, label) for field, label in REQUIRED_FIELDS if not str(getattr(payload, field) or "").strip()
    ]
    if missing:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "保存前请先选择：" + "、".join(label for _field, label in missing),
            details={"fields": [field for field, _label in missing]},
        )
    enterprise_id = auth.user.enterprise_id or "E-" + uuid.uuid4().hex[:8]
    existing = repo.get_enterprise(enterprise_id)
    repo.save_enterprise(
        Enterprise(
            enterprise_id=enterprise_id,
            name=payload.name.strip(),
            employer_type=payload.employer_type.strip(),
            staff_size=int(payload.staff_size),
            working_hours=payload.working_hours.strip(),
            social_insurance=payload.social_insurance.strip(),
            created_at=existing.created_at if existing is not None else "",
        )
    )
    if not auth.user.enterprise_id:
        repo.set_user_enterprise(auth.user.user_id, enterprise_id)
    saved = repo.get_enterprise(enterprise_id)
    if saved is None:  # 理论不可达：保存后立刻读回
        raise ApiError(500, INTERNAL_ERROR, "企业档案保存后读取失败")
    return EnterpriseView.of(saved)


@router.delete(
    "/enterprise",
    status_code=204,
    dependencies=[Depends(require_capability("enterprise"))],
)
def delete_enterprise(auth: AuthDep, repo: RepoDep) -> None:
    """删档案：合同随档案删除，核对历史保留（与仓储 `delete_enterprise` 语义一致）。"""
    if auth.user.enterprise_id:
        repo.delete_enterprise(auth.user.enterprise_id)
