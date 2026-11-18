"""核对历史（规格 006 §2：`/history`、`/history/trend`、`/history/{task_id}`）。

口径固定为「最近 200 次核对」：`list_tasks(limit=200)` 客户端筛选只在这一窗口内进行，
`total_all` 是**窗口内**条数而不是全库总量（006 未决问题 11），所以 `window_size` 必须一起返回。
筛选不另起判定：`risk` 用 `summarize_verdict().status`，`degraded` 用 `_show_degraded_badge`
的同一判断（都在 `presenters` 里，与顶部结论/徽标共用一份）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...adapters.persist import SqliteRepository
from ...domain.models import CheckTask, TaskMode
from .. import presenters
from ..deps import AuthDep, RepoDep, require_capability
from ..errors import NOT_FOUND, VALIDATION_ERROR, ApiError
from ..schemas import HistoryPage, TrendView
from .checks import find_history_task, job_store

router = APIRouter()

#: 历史窗口与分页上限（006 §2：`GET /history` 的 `window_size=200`、默认每页 20）。
WINDOW_SIZE = 200
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
FILTERS = ("all", "risk", "degraded")


def _check_tasks(repo: SqliteRepository, enterprise_id: str) -> list[CheckTask]:
    """窗口内的核对记录（问答记录不进历史列表，与旧界面一致）。"""
    return [task for task in repo.list_tasks(enterprise_id, limit=WINDOW_SIZE) if task.mode is TaskMode.CHECK]


@router.get(
    "/history",
    dependencies=[Depends(require_capability("history"))],
    response_model=HistoryPage,
)
def get_history(
    auth: AuthDep,
    repo: RepoDep,
    filter: str = "all",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> HistoryPage:
    """历史列表：`filter=all|risk|degraded`，分页不改变“最近 200 条”的口径。"""
    choice = str(filter or "all").strip().lower()
    if choice not in FILTERS:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "筛选只能取 all / risk / degraded",
            details={"fields": ["filter"]},
        )
    enterprise_id = auth.user.enterprise_id
    checks = _check_tasks(repo, enterprise_id)
    shown = presenters.filter_history(checks, choice)
    size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    current = max(int(page), 1)
    start = (current - 1) * size
    _valid, deleted = repo.count_tasks(enterprise_id)
    show_process = presenters.include_process(repo)
    return HistoryPage(
        items=[
            presenters.history_summary(task, show_process=show_process)
            for task in shown[start : start + size]
        ],
        total=len(shown),
        page=current,
        page_size=size,
        total_all=len(checks),
        deleted_count=deleted,
        window_size=WINDOW_SIZE,
    )


@router.get(
    "/history/trend",
    dependencies=[Depends(require_capability("history_trend"))],
    response_model=TrendView,
)
def get_history_trend(auth: AuthDep, repo: RepoDep) -> TrendView:
    """统计与趋势：最近 12 次核对的风险条目数（只数规则条目，与旧界面同口径）。"""
    checks = _check_tasks(repo, auth.user.enterprise_id)
    return presenters.history_trend(checks, window_size=WINDOW_SIZE)


@router.delete(
    "/history/{task_id}",
    status_code=204,
    dependencies=[Depends(require_capability("history_delete"))],
)
def delete_history(task_id: str, auth: AuthDep, repo: RepoDep, request: Request) -> None:
    """软删（006 §2）：历史页不再显示，操作日志与记录本身保留；已删/不存在都 404。"""
    enterprise_id = auth.user.enterprise_id
    if find_history_task(repo, task_id, enterprise_id) is None:
        raise ApiError(404, NOT_FOUND, "核对记录不存在")
    repo.delete_task(task_id, enterprise_id)
    job_store(request).forget(task_id)
    presenters.forget_optional_picks(task_id)
