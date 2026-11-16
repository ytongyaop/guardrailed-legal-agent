"""P0-2：`RuleCheck` 三态转换测试（规格 003 第 5 章）。

守卫范围：四态 → 三态的映射（含 `not_applicable → ok` 这一处刻意的映射决定）、
真实规则表逐条可转换、三态取值封闭、既有 `RuleStatus` 四态枚举未被改名或改值。
"""

from __future__ import annotations

from pathlib import Path

from mla.config import data_path
from mla.domain.models import RuleEntry
from mla.domain.rules import (
    RuleCheck,
    RuleCheckStatus,
    RuleOutcome,
    RuleStatus,
    judge,
    judge_all,
    load_rules,
    to_rule_check,
)

RULES_PATH = Path(data_path("rules/rules.yaml"))

# 现有四态的枚举值：P0-2 不许改名、不许改值（三态校验是新增，不是替换）。
LEGAL_STATUS_VALUES = ("violated", "compliant", "not_applicable", "unknown")

# 规则表当前 16 条（规格 003 第 5.3 节瘦身后；条数变化必须别处显式同步）。
EXPECTED_RULE_COUNT = 16

_FULL_COMPLIANT: dict[str, str] = {
    "term_years": "3",
    "term_type": "固定期限",
    "probation_months": "6",
    "probation_times": "1",
    "probation_wage_ratio": "80",
    "months_without_contract": "0",
    "noncompete_years": "2",
    "noncompete_unpaid_months": "0",
    "ot_hours_day": "3",
    "ot_hours_month": "36",
    "ot_weekday_ratio": "150",
    "ot_restday_ratio": "200",
    "ot_holiday_ratio": "300",
    "days_to_si_register": "30",
    "si_fine_multiple": "3",
    "work_years": "12",
    "annual_leave_days": "10",
    "unused_leave_pay_ratio": "300",
    "pay_times_per_month": "1",
}

_FULL_VIOLATED: dict[str, str] = {
    "term_years": "3",
    "term_type": "固定期限",
    "probation_months": "12",
    "probation_times": "2",
    "probation_wage_ratio": "60",
    "months_without_contract": "3",
    "noncompete_years": "3",
    "noncompete_unpaid_months": "4",
    "ot_hours_day": "5",
    "ot_hours_month": "50",
    "ot_weekday_ratio": "120",
    "ot_restday_ratio": "150",
    "ot_holiday_ratio": "200",
    "days_to_si_register": "60",
    "si_fine_multiple": "5",
    "work_years": "12",
    "annual_leave_days": "5",
    "unused_leave_pay_ratio": "100",
    "pay_times_per_month": "0",
}

# 一年期合同：走到"前提不满足"的路径（`not_applicable` 的来源）。
_PARTIAL_APPLICABLE: dict[str, str] = {
    "term_years": "1",
    "term_type": "固定期限",
    "probation_months": "3",
    "work_years": "12",
}

# 20 年以上工龄：让 R-AL-15D 也能拿到可比较的判定值。
_SENIOR: dict[str, str] = {"work_years": "25", "annual_leave_days": "15"}

_FACT_SETS: tuple[dict[str, str], ...] = ({}, _FULL_COMPLIANT, _FULL_VIOLATED, _PARTIAL_APPLICABLE, _SENIOR)


def _rule(rule_id: str) -> RuleEntry:
    matches = [rule for rule in load_rules(RULES_PATH) if rule.rule_id == rule_id]
    assert len(matches) == 1, rule_id
    return matches[0]


def test_four_states_map_to_three() -> None:
    """四种 RuleStatus 各造一个最小 outcome，断言三态映射与文本字段。"""
    rule = _rule("R-PROBATION-6M")
    cases = (
        (RuleOutcome(rule, RuleStatus.VIOLATED, 12.0, (), "命中违规"), RuleCheckStatus.MISMATCH, "12 个月"),
        (RuleOutcome(rule, RuleStatus.COMPLIANT, 3.0, (), "无违规"), RuleCheckStatus.OK, "3 个月"),
        (RuleOutcome(rule, RuleStatus.NOT_APPLICABLE, None, (), "前提不满足"), RuleCheckStatus.OK, ""),
        (
            RuleOutcome(rule, RuleStatus.UNKNOWN, None, ("probation_months",), "判定字段缺失"),
            RuleCheckStatus.NEEDS_FACT,
            "",
        ),
    )
    for outcome, status, actual in cases:
        check = to_rule_check(outcome)
        assert isinstance(check, RuleCheck)
        assert check.status is status, outcome.status
        assert check.actual == actual
        assert check.expected == "<= 6 个月"
        assert check.rule_id == "R-PROBATION-6M"
        assert check.field == "probation_months"
        assert check.statute_id == "LCL-19"
        assert check.evidence_span == rule.evidence_span
        assert check.severity == "hard"


def test_not_applicable_maps_to_ok_not_needs_fact() -> None:
    """映射决定：前提不满足不构成问题，归 ok（不是 needs_fact，也不是 mismatch）。"""
    outcome = judge(_rule("R-AL-5D"), {"annual_leave_days": "5", "work_years": "12"})
    assert outcome.status is RuleStatus.NOT_APPLICABLE
    check = to_rule_check(outcome)
    assert check.status is RuleCheckStatus.OK
    assert check.actual == ""
    assert "5" in check.expected and "天" in check.expected


def test_needs_fact_keeps_actual_empty() -> None:
    """事实缺失 / 不可解析 → needs_fact，且 actual 必须是空串（不留半截值）。"""
    for facts in ({}, {"probation_times": "若干次"}, {"annual_leave_days": "5"}):
        checks = [to_rule_check(outcome) for outcome in judge_all(load_rules(RULES_PATH), facts)]
        needs = [check for check in checks if check.status is RuleCheckStatus.NEEDS_FACT]
        assert needs, facts
        assert all(check.actual == "" for check in needs)


def test_every_rule_converts_with_required_fields() -> None:
    """真实规则表逐条转换：字段非空、状态封闭、顺序与规则表一致。"""
    rules = load_rules(RULES_PATH)
    assert len(rules) == EXPECTED_RULE_COUNT
    seen: set[RuleCheckStatus] = set()
    for facts in _FACT_SETS:
        checks = [to_rule_check(outcome) for outcome in judge_all(rules, facts)]
        assert len(checks) == len(rules)
        assert [check.rule_id for check in checks] == [rule.rule_id for rule in rules]
        for check in checks:
            assert check.rule_id, facts
            assert check.field, facts
            assert check.statute_id, facts
            assert check.status in set(RuleCheckStatus)
            assert check.severity in ("hard", "prompt")
            if check.status is RuleCheckStatus.NEEDS_FACT:
                assert check.actual == ""
            seen.add(check.status)
    # 16 条规则 × 5 组事实必须覆盖全部三态，否则映射有分支从未被跑到。
    assert seen == set(RuleCheckStatus)


def test_text_fields_are_human_readable() -> None:
    """expected / actual 是人可读文本，不出现 6.0 这类原始浮点写法。"""
    outcome = judge(_rule("R-AL-5D"), {"work_years": "6", "annual_leave_days": "5"})
    check = to_rule_check(outcome)
    assert check.expected == ">= 5 天"
    assert check.actual == "5 天"
    assert ".0" not in check.expected
    assert ".0" not in check.actual

    # 单位换算：`月` → "个月"（避免 "12 月" 被读成十二月）；百分号紧贴数字。
    month = to_rule_check(judge(_rule("R-PROBATION-6M"), {"term_years": "3", "probation_months": "3"}))
    assert month.expected == "<= 6 个月"
    assert month.actual == "3 个月"
    percent = to_rule_check(judge(_rule("R-OT-WEEKDAY-150"), {"ot_weekday_ratio": "120"}))
    assert percent.expected == ">= 150%"
    assert percent.actual == "120%"


def test_four_state_status_enum_unchanged() -> None:
    """守卫：现有 RuleStatus 四态的名字与取值一个都不能变。"""
    assert {status.name: status.value for status in RuleStatus} == {
        "VIOLATED": "violated",
        "COMPLIANT": "compliant",
        "NOT_APPLICABLE": "not_applicable",
        "UNKNOWN": "unknown",
    }
    assert {status.value for status in RuleStatus} == set(LEGAL_STATUS_VALUES)
