"""规则前提的 OR 模式测试：无固定期限合同同样受"试用期不超过 6 个月"约束。

背景：劳动合同法第十九条第一款是"三年以上固定期限**和**无固定期限的劳动合同"，
两组前提是并列关系；只写数值区间会让无固定期限合同漏判（泛化集 g08）。
"""

from __future__ import annotations

from pathlib import Path

from mla.config import data_path
from mla.domain.rules import RuleEntry, _guard_ok, evaluate, load_rules, rule_applies

RULES_PATH = Path(data_path("rules/rules.yaml"))


def _or_rule() -> RuleEntry:
    """与 R-PROBATION-6M 同构的最小规则：三年以上 或 无固定期限。"""
    return RuleEntry(
        rule_id="R-TEST-OR",
        name="三年以上固定期限或无固定期限合同，试用期不得超过 6 个月",
        field="probation_months",
        op="<=",
        threshold=6,
        statute_id="LCL-19",
        when_field="term_years",
        when_op=">=",
        when_value=3,
        when_field2="term_type",
        when_op2="==",
        when_value2="无固定期限",
        when_mode="or",
        evidence_span="三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月",
    )


def test_or_mode_applies_on_second_condition() -> None:
    """无固定期限合同没有 term_years（或为 0），仍应适用并判违规。"""
    rule = _or_rule()
    for facts in (
        {"probation_months": "7", "term_type": "无固定期限"},  # 缺 term_years
        {"probation_months": "7", "term_type": "无固定期限", "term_years": "0"},
    ):
        assert rule_applies(rule, facts) is True
        hits = evaluate((rule,), facts)
        assert [(h.rule.rule_id, h.violated) for h in hits] == [("R-TEST-OR", True)]


def test_or_mode_not_applied_when_both_fail() -> None:
    """一年期固定期限合同：两组前提都不满足 → 不适用（不串到 6 个月那条）。"""
    rule = _or_rule()
    facts = {"probation_months": "7", "term_years": "1", "term_type": "固定期限"}
    assert rule_applies(rule, facts) is False
    assert evaluate((rule,), facts) == []


def test_or_mode_ignores_empty_condition_group() -> None:
    """OR 模式下没写前提的那组视为不满足，不能因为"空条件"就处处适用。"""
    rule = RuleEntry(
        rule_id="R-TEST-OR-EMPTY",
        name="只有单组前提",
        field="probation_months",
        op="<=",
        threshold=6,
        statute_id="LCL-19",
        when_field2="term_type",
        when_op2="==",
        when_value2="无固定期限",
        when_mode="or",
    )
    assert rule_applies(rule, {"probation_months": "7"}) is False
    assert rule_applies(rule, {"probation_months": "7", "term_type": "无固定期限"}) is True


def test_string_equality_guard() -> None:
    """字符串前提按精确值比较：固定期限 ≠ 无固定期限。"""
    facts = {"term_type": "固定期限"}
    assert _guard_ok("term_type", "==", "无固定期限", facts) is False
    assert _guard_ok("term_type", "!=", "无固定期限", facts) is True
    assert _guard_ok("term_type", "==", "固定期限", facts) is True
    # 事实缺失不猜
    assert _guard_ok("term_type", "==", "无固定期限", {}) is False
    assert _guard_ok("term_type", "!=", "无固定期限", {}) is False
    # 非 == / != 的比较符不成立（字符串没有大小可比）
    assert _guard_ok("term_type", ">=", "固定期限", facts) is False


def test_rules_yaml_probation_6m_uses_or_mode() -> None:
    """落库的那条规则确实是 OR：无固定期限 + 试用期 7 个月要命中。"""
    rules = tuple(r for r in load_rules(RULES_PATH) if r.rule_id == "R-PROBATION-6M")
    assert len(rules) == 1
    rule = rules[0]
    assert rule.when_mode == "or"
    assert rule.when_field2 == "term_type"
    hits = [h for h in evaluate((rule,), {"probation_months": "7", "term_type": "无固定期限"}) if h.violated]
    assert [h.rule.rule_id for h in hits] == ["R-PROBATION-6M"]
