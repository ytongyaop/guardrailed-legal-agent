"""历史列表 / 筛选 / 趋势 / 软删（规格 006 AC-07 的 API 部分，AC-05 的筛选口径）。

口径守卫：

- `total_all` 是**最近 200 条窗口内**的条数，`window_size=200` 必须一起给出（006 未决问题 11）；
- `risk` 筛选与顶部结论同一函数（`summarize_verdict`）：只有条款审查 A 级、规则条目为 0 的任务
  也要算风险；`degraded` 与列表徽标同一判断；
- 分页只切窗口内的结果，不改变"最近 200 条"的口径；
- 软删后列表不再出现、`deleted_count` +1、详情 404。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from mla.domain.models import (
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    Severity,
    TaskMode,
)

from .api_helpers import csrf_of, register, setup_admin
from .flow_helpers import count_rows, seed_task

pytestmark = pytest.mark.no_llm


def _rule_risk(task_id: str, created_at: datetime) -> CheckTask:
    """规则条目 1 条：`summarize_verdict` → risk。"""
    return CheckTask(
        task_id=task_id,
        mode=TaskMode.CHECK,
        contract_name="规则风险.txt",
        contract_chars=1200,
        created_at=created_at,
        items=[
            CheckItem(
                item_id="I-1",
                kind=ItemKind.RISK,
                clause_ref="第十九条",
                finding="试用期约定超过法定上限",
                source_id="LCL-19",
                severity=Severity.HARD,
                rule_id="R-PROBATION-6M",
            )
        ],
    )


def _clause_risk(task_id: str, created_at: datetime) -> CheckTask:
    """规则层 0 条、只有条款审查 A 级：筛选不能把它漏掉。"""
    return CheckTask(
        task_id=task_id,
        mode=TaskMode.CHECK,
        contract_name="条款风险.txt",
        contract_chars=1200,
        clause_review=True,
        clause_findings=[
            ClauseFinding(
                paragraph=3,
                span="试用期工资为转正工资的 60%。",
                grade="A",
                reason="与第二十条关于试用期工资的规定不符",
                statute_ids=("LCL-20",),
                trigger_id="TR-PROBATION-WAGE-BASE",
            )
        ],
        created_at=created_at,
    )


def _clean(task_id: str, created_at: datetime) -> CheckTask:
    """三个通道都跑过、都判得动：结论 clean，不该出现在“有风险”里。"""
    terms = [
        MissingTerm(
            term_id=f"MT-{index}",
            name=f"第{index}类",
            clause_no="（一）",
            statute_id="LCL-17",
            status=MissingStatus.WRITTEN,
            certainty="高",
        )
        for index in range(1, 9)
    ]
    return CheckTask(
        task_id=task_id,
        mode=TaskMode.CHECK,
        contract_name="干净合同.txt",
        contract_chars=1200,
        clause_review=True,
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=12, not_applicable=8, unknown=0),
        missing_terms=terms,
        created_at=created_at,
    )


def _degraded(task_id: str, created_at: datetime) -> CheckTask:
    return CheckTask(
        task_id=task_id,
        mode=TaskMode.CHECK,
        contract_name="降级.txt",
        contract_chars=1200,
        degraded=True,
        created_at=created_at,
    )


@pytest.fixture
def seeded(api, tmp_path: Path):
    state = setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    enterprise_id = enterprise["enterprise_id"]
    created = {
        "T-CLEAN": datetime(2026, 1, 1, 9, 0, 0),
        "T-RULE": datetime(2026, 1, 2, 9, 0, 0),
        "T-CLAUSE": datetime(2026, 1, 3, 9, 0, 0),
        "T-DEGRADED": datetime(2026, 1, 4, 9, 0, 0),
        "T-QA": datetime(2026, 1, 5, 9, 0, 0),
    }
    tasks = [
        _clean("T-CLEAN", created["T-CLEAN"]),
        _rule_risk("T-RULE", created["T-RULE"]),
        _clause_risk("T-CLAUSE", created["T-CLAUSE"]),
        _degraded("T-DEGRADED", created["T-DEGRADED"]),
        CheckTask(task_id="T-QA", mode=TaskMode.QA, contract_chars=10, created_at=created["T-QA"]),
    ]
    for task in tasks:
        task.enterprise_id = enterprise_id
        seed_task(api.db_path, task)
    return api, state, enterprise_id


def _ids(payload: dict) -> list[str]:
    return [row["task_id"] for row in payload["items"]]


def test_history_window_and_order(seeded) -> None:
    """默认窗口：只列核对记录（问答不进），新→旧排序，`window_size` 与 `total_all` 一起给。"""
    api, _state, _enterprise_id = seeded
    response = api.client.get("/api/history")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _ids(payload) == ["T-DEGRADED", "T-CLAUSE", "T-RULE", "T-CLEAN"]
    assert payload["total"] == 4
    assert payload["total_all"] == 4, "窗口内总数（不含 QA 记录）"
    assert payload["window_size"] == 200
    assert payload["deleted_count"] == 0
    assert payload["page"] == 1 and payload["page_size"] == 20
    assert count_rows(api.db_path, "tasks") == 5

    first = payload["items"][0]
    assert first["contract_name"] == "降级"
    assert first["verdict"]["process_details"] == [], "开关默认关闭：历史摘要不带过程明细"
    clause_row = next(row for row in payload["items"] if row["task_id"] == "T-CLAUSE")
    assert clause_row["verdict"]["conclusion_details"], "结论计数留在第一层"
    assert clause_row["verdict"]["status"] == "risk"


def test_history_filters_follow_verdict_and_badge(seeded) -> None:
    """`risk` 用 `summarize_verdict`，`degraded` 用徽标判断：两条口径都不另起。"""
    api, _state, _enterprise_id = seeded
    risk = api.client.get("/api/history", params={"filter": "risk"}).json()
    assert _ids(risk) == ["T-CLAUSE", "T-RULE"], "条款 A 级与规则条目都要算风险"
    assert risk["total"] == 2 and risk["total_all"] == 4

    degraded = api.client.get("/api/history", params={"filter": "degraded"}).json()
    assert _ids(degraded) == ["T-DEGRADED"]
    assert degraded["total"] == 1

    everything = api.client.get("/api/history", params={"filter": "all"}).json()
    assert len(everything["items"]) == 4

    bad = api.client.get("/api/history", params={"filter": "whatever"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_history_pagination_does_not_change_window(seeded) -> None:
    api, _state, _enterprise_id = seeded
    second = api.client.get("/api/history", params={"page": 2, "page_size": 2}).json()
    assert _ids(second) == ["T-RULE", "T-CLEAN"]
    assert second["total"] == 4 and second["total_all"] == 4
    assert second["page"] == 2 and second["page_size"] == 2

    first_only = api.client.get("/api/history", params={"filter": "risk", "page_size": 1}).json()
    assert first_only["total"] == 2 and len(first_only["items"]) == 1


def test_history_trend_counts_rule_items(seeded) -> None:
    """趋势只数 `len(task.items)`（规则条目），最近 12 次按时间正序。"""
    api, _state, _enterprise_id = seeded
    trend = api.client.get("/api/history/trend").json()
    assert trend["window_size"] == 200
    assert [point["order"] for point in trend["points"]] == [1, 2, 3, 4]
    assert [point["risk_item_count"] for point in trend["points"]] == [0, 1, 0, 0]
    assert trend["recent"] == 0
    assert trend["max"] == 1
    assert trend["average"] == pytest.approx(0.25)
    assert trend["points"][0]["label"] == "01-01 09:00"
    assert "统计窗口" in trend["note"]


def test_history_soft_delete(seeded) -> None:
    """软删：列表消失、`deleted_count` +1、详情 404；重复删除 404，记录本身还在库里。"""
    api, state, _enterprise_id = seeded
    headers = {"X-CSRF-Token": csrf_of(state)}
    assert api.client.request("DELETE", "/api/history/T-RULE", headers=headers).status_code == 204

    payload = api.client.get("/api/history").json()
    assert "T-RULE" not in _ids(payload)
    assert payload["total"] == 3
    assert payload["total_all"] == 3, "软删后不再计入窗口条数"
    assert payload["deleted_count"] == 1
    assert count_rows(api.db_path, "tasks") == 5, "软删不物理删除"

    assert api.client.get("/api/tasks/T-RULE").status_code == 404
    again = api.client.request("DELETE", "/api/history/T-RULE", headers=headers)
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "NOT_FOUND"


def test_history_task_detail_is_history_state(seeded) -> None:
    """历史详情：可读、`workflow.state=history`（无可选补充/问答能力）；问答记录不可当详情读。"""
    api, _state, _enterprise_id = seeded
    detail = api.client.get("/api/tasks/T-RULE")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["workflow"]["state"] == "history"
    assert body["workflow"]["optional_fields"] == []
    assert body["workflow"]["questions"] == []
    assert body["verdict"]["status"] == "risk"
    assert body["findings"][0]["item_id"] == "I-1"

    assert api.client.get("/api/tasks/T-QA").status_code == 404


def test_history_is_enterprise_scoped(seeded) -> None:
    """跨企业：列表空、删除 404、详情 404——一律 404，不暴露他人记录是否存在。"""
    api, _admin_state, _enterprise_id = seeded
    other = register(api.client, username="outsider", enterprise_name="另一家企业")
    headers = {"X-CSRF-Token": csrf_of(other)}

    payload = api.client.get("/api/history").json()
    assert payload["items"] == [] and payload["total_all"] == 0

    response = api.client.request("DELETE", "/api/history/T-RULE", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert api.client.get("/api/tasks/T-RULE").status_code == 404
    assert api.client.get("/api/tasks/T-RULE/report").status_code == 404
