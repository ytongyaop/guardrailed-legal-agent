"""历史页筛选测试：筛选口径必须与顶部结论一致，不能再另起一套判定。

四条最小构造任务覆盖三件事：规则条目算风险、只有条款审查 A/B 也算风险、
干净任务不算风险、“已降级”只看 `_show_degraded_badge`。
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from mla.domain.models import (  # noqa: E402 - 先跳 streamlit 再导界面模块
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    TaskMode,
)
from mla.ui.app import _filter_checks  # noqa: E402


def _items_task() -> CheckTask:
    return CheckTask(
        task_id="T-ITEMS",
        mode=TaskMode.CHECK,
        contract_chars=1200,
        items=[
            CheckItem(
                item_id="R-PROBATION-6M",
                kind=ItemKind.RISK,
                clause_ref="第十九条",
                finding="试用期约定超过法定上限",
                source_id="LCL-19",
                rule_id="R-PROBATION-6M",
            )
        ],
    )


def _clause_risk_task() -> CheckTask:
    """规则层 0 条，只有条款审查的 A 级结论。"""
    return CheckTask(
        task_id="T-CLAUSE-A",
        mode=TaskMode.CHECK,
        contract_chars=1200,
        clause_review=True,
        clause_findings=[
            ClauseFinding(
                paragraph=3,
                span="试用期工资为转正工资的 60%。",
                grade="A",
                reason="该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符",
                statute_ids=("LCL-20",),
                trigger_id="TR-PROBATION-WAGE-BASE",
            )
        ],
    )


def _clean_task() -> CheckTask:
    """三个通道全跑过、都判得动：结论是 clean，不该出现在“有风险”里。"""
    terms = [
        MissingTerm(
            term_id=f"MT-{i}",
            name=f"第{i}类",
            clause_no="（一）",
            statute_id="LCL-17",
            status=MissingStatus.WRITTEN,
            certainty="高",
        )
        for i in range(1, 9)
    ]
    return CheckTask(
        task_id="T-CLEAN",
        mode=TaskMode.CHECK,
        contract_chars=1200,
        clause_review=True,
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=12, not_applicable=8, unknown=0),
        missing_terms=terms,
    )


def _degraded_task() -> CheckTask:
    """降级但没“未配置 Key”诊断：界面会挂“已降级”徽标，筛选要跟徽标一致。"""
    return CheckTask(task_id="T-DEGRADED", mode=TaskMode.CHECK, contract_chars=1200, degraded=True)


def test_filter_risk_includes_items_and_clause_grades() -> None:
    checks = [_items_task(), _clause_risk_task(), _clean_task(), _degraded_task()]
    shown = _filter_checks(checks, "有风险")

    ids = [t.task_id for t in shown]
    assert ids == ["T-ITEMS", "T-CLAUSE-A"], "规则条目与条款审查 A/B 级都要算风险"
    assert "T-CLEAN" not in ids
    assert "T-DEGRADED" not in ids


def test_filter_degraded_only_degraded() -> None:
    checks = [_items_task(), _clause_risk_task(), _clean_task(), _degraded_task()]
    assert [t.task_id for t in _filter_checks(checks, "已降级")] == ["T-DEGRADED"]


def test_filter_all_returns_every_task() -> None:
    checks = [_items_task(), _clause_risk_task(), _clean_task(), _degraded_task()]
    shown = _filter_checks(checks, "全部")
    assert [t.task_id for t in shown] == [t.task_id for t in checks]
    assert shown is not checks, "返回副本，避免调用方改动原列表"
