"""批次 3 占位路由（规格 006 批次 2 的过渡态）。

`/checks`、`/jobs`、`/tasks/*`、`/history*`、`/report` 属于批次 3「核对 API 全链路」。
本批次**先按能力矩阵注册**它们（而不是不注册），理由：

1. 鉴权口径（401 / 403 `FORBIDDEN` / CSRF / 管理端）现在就能被 `test_permissions.py` 逐条验证，
   批次 3 只需替换处理器，不必再回头补授权；
2. 路由清单与 `permissions.json` 严格一致，漏注册或多注册都会在测试里暴露；
3. 前端与文档可以照 `/api` 契约开发，不会误以为端点不存在（501 明确表示"能力尚未提供"）。

处理器一律抛 501 `NOT_IMPLEMENTED`（批次 2 专用的中间态码，不在 006 错误码表里）。
批次 3 落地时删除本模块并加入真实路由即可。
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends

from ..deps import require_capability
from ..errors import NOT_IMPLEMENTED, ApiError

router = APIRouter()

_MESSAGE = "该能力将在 0.4.0 批次 3 提供（核对全链路尚未接入）"


def _not_implemented() -> NoReturn:
    raise ApiError(501, NOT_IMPLEMENTED, _MESSAGE)


@router.post("/checks", dependencies=[Depends(require_capability("check"))])
def post_checks() -> None:
    _not_implemented()


@router.get("/jobs/{job_id}", dependencies=[Depends(require_capability("check"))])
def get_job(job_id: str) -> None:
    _not_implemented()


@router.get("/tasks/{task_id}", dependencies=[Depends(require_capability("check"))])
def get_task(task_id: str) -> None:
    _not_implemented()


@router.post("/tasks/{task_id}/answers", dependencies=[Depends(require_capability("check"))])
def post_answers(task_id: str) -> None:
    _not_implemented()


@router.post(
    "/tasks/{task_id}/optional-facts",
    dependencies=[Depends(require_capability("check"))],
)
def post_optional_facts(task_id: str) -> None:
    _not_implemented()


@router.post("/tasks/{task_id}/qa", dependencies=[Depends(require_capability("qa"))])
def post_qa(task_id: str) -> None:
    _not_implemented()


@router.get("/history", dependencies=[Depends(require_capability("history"))])
def get_history() -> None:
    _not_implemented()


@router.get("/history/trend", dependencies=[Depends(require_capability("history_trend"))])
def get_history_trend() -> None:
    _not_implemented()


@router.delete("/history/{task_id}", dependencies=[Depends(require_capability("history_delete"))])
def delete_history(task_id: str) -> None:
    _not_implemented()


@router.get("/tasks/{task_id}/report", dependencies=[Depends(require_capability("report"))])
def get_report(task_id: str) -> None:
    _not_implemented()
