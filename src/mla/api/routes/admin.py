"""管理端（规格 006 §2，全部要求管理员）：用户 / 法源 / 规则 / 操作日志 / 评测。

- 用户列表与角色调整不含口令哈希、盐；删号沿用 `delete_account` 的全部保护；
- 法源、规则、日志、评测都是**只读**：本批次不新增管理端写能力；
- 角色调整不额外发明规则（沿用旧界面"直接覆盖"语义），每次请求按 user_id 重读角色。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response

from ...config import data_path
from ...domain.models import OperationLog, Role
from ...domain.policy import ACCEPTANCE
from ...domain.slots import derive_slots
from ...services.auth import delete_account
from ..deps import AuthDep, RepoDep, require_capability
from ..errors import FORBIDDEN, NOT_FOUND, STATE_CONFLICT, VALIDATION_ERROR, ApiError
from ..schemas import (
    AdminUser,
    DeleteAccountIn,
    DeletionOut,
    EvalHistoryRow,
    EvaluationView,
    EvalVariant,
    LogPage,
    LogRow,
    MetricRow,
    RoleIn,
    RuleCatalogView,
    RuleRow,
    SlotRow,
    StatutePage,
    StatuteRow,
)

router = APIRouter()

EVAL_FILE = Path(data_path("evalsets/last_eval.json"))
EVAL_HISTORY_FILE = Path(data_path("evalsets/eval_history.jsonl"))
EVAL_LABELS: dict[str, str] = {
    "accuracy": "风险/缺项识别准确率",
    "recall": "风险/缺项召回率",
    "traceable": "依据可追溯率",
    "ask_trigger": "事实补问触发率",
    "no_false_ask": "不误问率",
    "refuse": "越界拒答率",
    "false_refuse": "误拒率",
    "latency_s": "首轮处理时间（秒）",
    "unsourced_suggestion": "建议无依据率",
    "statute_hit": "法条召回率（诊断）",
}
LOG_ACTIONS: tuple[str, ...] = ("全部", "核对", "问答")
LOG_WINDOW = 500
EVAL_HISTORY_LIMIT = 10


# ---------------------------------------------------------------- 用户


@router.get(
    "/admin/users",
    dependencies=[Depends(require_capability("admin_users"))],
    response_model=list[AdminUser],
)
def list_users(repo: RepoDep) -> list[AdminUser]:
    """全部账号：只有 ID / 用户名 / 角色 / 企业 / 建档时间 / 引导标记，无哈希无盐。"""
    return [AdminUser.of(user) for user in repo.list_users()]


@router.patch(
    "/admin/users/{user_id}/role",
    dependencies=[Depends(require_capability("admin_role"))],
    response_model=AdminUser,
)
def patch_user_role(
    user_id: str,
    payload: RoleIn,
    repo: RepoDep,
) -> AdminUser:
    """改角色：只接受「用户 / 管理员」，其余一律 400。下一次请求即按新角色鉴权。"""
    try:
        role = Role(payload.role)
    except ValueError as exc:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "角色只能是「用户」或「管理员」",
            details={"fields": ["role"]},
        ) from exc
    target = next((user for user in repo.list_users() if user.user_id == user_id), None)
    if target is None:
        raise ApiError(404, NOT_FOUND, "账号不存在")
    repo.set_user_role(target.user_id, str(role))
    updated = next((user for user in repo.list_users() if user.user_id == user_id), None)
    if updated is None:  # 理论不可达：刚刚更新过
        raise ApiError(404, NOT_FOUND, "账号不存在")
    return AdminUser.of(updated)


@router.delete(
    "/admin/users/{user_id}",
    dependencies=[Depends(require_capability("admin_users"))],
    response_model=DeletionOut,
)
def delete_user(
    user_id: str,
    payload: DeleteAccountIn,
    auth: AuthDep,
    repo: RepoDep,
) -> DeletionOut:
    """硬删账号：必须回填目标用户名；保护顺序全部沿用 `delete_account`。"""
    target = next((user for user in repo.list_users() if user.user_id == user_id), None)
    if target is None:
        raise ApiError(404, NOT_FOUND, "账号不存在")
    if payload.confirm_username.strip() != target.username:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "确认用户名与目标账号不一致",
            details={"fields": ["confirm_username"]},
        )
    try:
        result = delete_account(repo, auth.user, target.username)
    except PermissionError as exc:  # 已有管理员门禁，这里只做防御
        raise ApiError(403, FORBIDDEN, str(exc)) from exc
    if not result.deleted:
        raise ApiError(409, STATE_CONFLICT, result.reason or "账号未删除")
    return DeletionOut(
        deleted=True,
        username=result.username,
        contracts=result.contracts,
        tasks=result.tasks,
        logs=result.logs,
        reason=result.reason,
    )


# ---------------------------------------------------------------- 法源 / 规则


@router.get(
    "/admin/statutes",
    dependencies=[Depends(require_capability("admin_statutes"))],
    response_model=StatutePage,
)
def list_statutes(
    repo: RepoDep,
    q: str = "",
    page: int = 1,
    page_size: int = 200,
) -> StatutePage:
    """法源库：`q` 过滤法名 / 条号 / 正文，默认每页 200 条。"""
    all_statutes = repo.list_statutes()
    keyword = q.strip()
    rows = [
        entry
        for entry in all_statutes
        if not keyword or keyword in entry.law_name or keyword in entry.article_no or keyword in entry.text
    ]
    size = min(max(int(page_size), 1), 500)
    current = max(int(page), 1)
    start = (current - 1) * size
    return StatutePage(
        items=[StatuteRow.of(entry) for entry in rows[start : start + size]],
        total=len(rows),
        page=current,
        page_size=size,
        total_all=len(all_statutes),
    )


@router.get(
    "/admin/rules",
    dependencies=[Depends(require_capability("admin_rules"))],
    response_model=RuleCatalogView,
)
def list_rules(repo: RepoDep) -> RuleCatalogView:
    """规则表 + 必问事实清单（按规则实际依赖的字段推导，与旧界面同一来源）。"""
    rules = repo.list_rules()
    slots = derive_slots(tuple(rules), {})
    return RuleCatalogView(
        rules=[RuleRow.of(rule) for rule in rules],
        slots=[
            SlotRow(
                slot_id=slot.slot_id,
                name=slot.name,
                type="阻断项" if slot.blocking else "参考项",
                question=slot.question,
                needed_by=list(slot.needed_by),
            )
            for slot in slots
        ],
    )


# ---------------------------------------------------------------- 操作日志


@router.get(
    "/admin/logs",
    dependencies=[Depends(require_capability("admin_logs"))],
    response_model=LogPage,
)
def list_logs(
    repo: RepoDep,
    action: str = "全部",
    q: str = "",
    page: int = 1,
    page_size: int = 100,
) -> LogPage:
    """最近的日志窗口（500 条）内筛选分页；`total_all` 是窗口内总条数。"""
    window = repo.list_logs(limit=LOG_WINDOW)
    rows = _filter_logs(window, action=action, keyword=q)
    size = min(max(int(page_size), 1), LOG_WINDOW)
    current = max(int(page), 1)
    start = (current - 1) * size
    return LogPage(
        items=[LogRow.of(log) for log in rows[start : start + size]],
        total=len(rows),
        page=current,
        page_size=size,
        total_all=len(window),
    )


@router.get(
    "/admin/logs/export",
    dependencies=[Depends(require_capability("admin_logs"))],
)
def export_logs(
    repo: RepoDep,
    action: str = "全部",
    q: str = "",
) -> Response:
    """导出当前筛选结果为 UTF-8-BOM CSV（Excel 直接打开不乱码）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["时间", "用户", "动作", "任务", "详情"])
    for log in _filter_logs(repo.list_logs(limit=LOG_WINDOW), action=action, keyword=q):
        writer.writerow([log.created_at, log.user_id, log.action, log.task_id, log.detail])
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="operation-logs.csv"'},
    )


def _filter_logs(logs: list[OperationLog], *, action: str, keyword: str) -> list[OperationLog]:
    wanted = action if action in LOG_ACTIONS else "全部"
    text = keyword.strip()
    return [
        log
        for log in logs
        if (wanted == "全部" or log.action == wanted)
        and (not text or text in log.user_id or text in log.task_id or text in log.detail)
    ]


# ---------------------------------------------------------------- 评测


@router.get(
    "/admin/evaluation",
    dependencies=[Depends(require_capability("admin_evaluation"))],
    response_model=EvaluationView,
)
def get_evaluation() -> EvaluationView:
    """只读最近一次评测（由 `scripts/run_eval.py` 生成）；接口里不跑评测、不联网。"""
    if not EVAL_FILE.exists():
        return EvaluationView(
            available=False,
            message="还没有评测结果。先在项目根目录运行：python scripts/run_eval.py",
        )
    try:
        data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return EvaluationView(available=False, message="评测文件无法读取，请重新运行评测脚本")
    variants = data.get("variants") or {}
    if not isinstance(variants, dict) or not variants:
        return EvaluationView(available=False, message="评测文件为空")
    return EvaluationView(
        available=True,
        message=None,
        case_count=int(data.get("case_count") or 0),
        generated_at=str(data.get("generated_at") or ""),
        variants=[_variant(name, result) for name, result in variants.items()],
        history=_history(),
    )


def _variant(name: str, result: dict[str, Any]) -> EvalVariant:
    metrics: dict[str, Any] = result.get("metrics") or {}
    passed: dict[str, Any] = result.get("pass") or {}
    rows: list[MetricRow] = []
    for key, value in metrics.items():
        number = _as_number(value)
        if number is None:
            continue
        rows.append(
            MetricRow(
                key=key,
                label=EVAL_LABELS.get(key, key),
                value=number,
                threshold=ACCEPTANCE.get(key),
                passed=passed.get(key),
            )
        )
    return EvalVariant(
        variant=name,
        metrics=rows,
        mismatches=list(result.get("mismatches") or []),
    )


def _history() -> list[EvalHistoryRow]:
    """历史最多 10 次：保持文件顺序，取最后 10 次展开成「每次每档位一行」。"""
    if not EVAL_HISTORY_FILE.exists():
        return []
    try:
        lines = [line for line in EVAL_HISTORY_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[EvalHistoryRow] = []
    for line in lines[-EVAL_HISTORY_LIMIT:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        for name, result in (record.get("variants") or {}).items():
            metrics = result.get("metrics") or {}
            passed = result.get("pass") or {}
            total = len(passed)
            rows.append(
                EvalHistoryRow(
                    time=str(record.get("generated_at") or ""),
                    case_count=int(record.get("case_count") or 0),
                    variant=str(name),
                    accuracy=_as_float(metrics.get("accuracy")),
                    recall=_as_float(metrics.get("recall")),
                    passed_text=f"{sum(1 for value in passed.values() if value)}/{total}",
                )
            )
    return rows


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_number(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int | float) else None
