"""规则四态判定测试：缺字段不再与"合规"混为一谈，且不改变既有判定口径。"""

from __future__ import annotations

import json
import random
from pathlib import Path

from mla.config import data_path
from mla.domain.rules import (
    RuleEntry,
    RuleOutcome,
    RuleStatus,
    evaluate,
    judge,
    judge_all,
    load_rules,
    rule_applies,
)

RULES_PATH = Path(data_path("rules/rules.yaml"))
EVALSETS = (
    Path(data_path("evalsets/eval_cases.jsonl")),
    Path(data_path("evalsets/eval_generalization.jsonl")),
)

# 随机事实的取值范围：按各字段在合同里的常见区间取整数，允许整字段缺失。
_NUMERIC_RANGES = {
    "probation_months": (0, 12),
    "probation_times": (0, 3),
    "probation_wage_ratio": (50, 120),
    "months_without_contract": (0, 6),
    "noncompete_years": (0, 4),
    "noncompete_unpaid_months": (0, 12),
    "ot_hours_day": (0, 6),
    "ot_hours_month": (0, 60),
    "ot_weekday_ratio": (100, 200),
    "ot_restday_ratio": (100, 250),
    "ot_holiday_ratio": (100, 400),
    "days_to_si_register": (0, 90),
    "si_fine_multiple": (0, 5),
    "annual_leave_days": (0, 20),
    "unused_leave_pay_ratio": (100, 400),
    "pay_times_per_month": (0, 4),
    "term_years": (0, 25),
    "work_years": (0, 30),
}
_TERM_TYPES = ("固定期限", "无固定期限", "以完成一定工作任务为期限")
_SEED = 20251106


def _facts_from_evalsets() -> list[dict[str, str]]:
    """取全部带 facts 的评测用例；问答类用例没有 facts，跳过。"""
    facts: list[dict[str, str]] = []
    for path in EVALSETS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if "facts" in row:
                    facts.append(row["facts"])
    return facts


def _all_fact_sets(rules: tuple[RuleEntry, ...]) -> list[dict[str, str]]:
    cases = _facts_from_evalsets()
    assert len(cases) == 52, "带 facts 的评测用例数变了"
    return cases + _random_facts(rules, 200)


def _dependency_fields(rules: tuple[RuleEntry, ...]) -> tuple[str, ...]:
    names = {rule.field for rule in rules}
    names |= {rule.when_field for rule in rules if rule.when_field}
    names |= {rule.when_field2 for rule in rules if rule.when_field2}
    return tuple(sorted(names))


def _random_facts(rules: tuple[RuleEntry, ...], count: int) -> list[dict[str, str]]:
    rng = random.Random(_SEED)
    fields = _dependency_fields(rules)
    out: list[dict[str, str]] = []
    for _ in range(count):
        facts: dict[str, str] = {}
        for name in fields:
            if rng.random() < 0.35:  # 允许缺失：正是要覆盖"判不了"的路径
                continue
            if name == "term_type":
                facts[name] = rng.choice(_TERM_TYPES)
            else:
                low, high = _NUMERIC_RANGES[name]
                facts[name] = str(rng.randint(low, high))
        out.append(facts)
    return out


def _by_id(outcomes: tuple[RuleOutcome, ...], rule_id: str) -> RuleOutcome:
    matches = [outcome for outcome in outcomes if outcome.rule.rule_id == rule_id]
    assert len(matches) == 1, rule_id
    return matches[0]


def _rule(rule_id: str) -> RuleEntry:
    matches = [rule for rule in load_rules(RULES_PATH) if rule.rule_id == rule_id]
    assert len(matches) == 1, rule_id
    return matches[0]


def test_dependency_fields_are_all_known() -> None:
    """随机生成器覆盖规则表的全部依赖字段；新增字段时这里先报错。"""
    names = set(_dependency_fields(load_rules(RULES_PATH)))
    assert names == set(_NUMERIC_RANGES) | {"term_type"}


def test_four_states_on_real_rules() -> None:
    rules = load_rules(RULES_PATH)

    violated = _by_id(
        judge_all(rules, {"term_years": "3", "term_type": "固定期限", "probation_months": "12"}),
        "R-PROBATION-6M",
    )
    assert violated.status is RuleStatus.VIOLATED
    assert violated.value == 12.0

    compliant = _by_id(
        judge_all(rules, {"term_years": "3", "term_type": "固定期限", "probation_months": "3"}),
        "R-PROBATION-6M",
    )
    assert compliant.status is RuleStatus.COMPLIANT
    assert compliant.value == 3.0

    absent = _by_id(
        judge_all(rules, {"term_years": "1", "term_type": "固定期限", "probation_months": "3"}),
        "R-PROBATION-6M",
    )
    assert absent.status is RuleStatus.NOT_APPLICABLE
    assert absent.value is None

    unknown = _by_id(judge_all(rules, {"annual_leave_days": "5"}), "R-AL-5D")
    assert unknown.status is RuleStatus.UNKNOWN
    assert unknown.value is None


def test_not_applicable_differs_from_unknown() -> None:
    """前提字段缺失 → 判不了；前提有值但不满足 → 不适用。两者不能合并。"""
    rule = _rule("R-AL-5D")
    unknown = judge(rule, {"annual_leave_days": "5"})
    assert unknown.status is RuleStatus.UNKNOWN
    assert unknown.missing_fields == ("work_years",)
    assert unknown.reason.startswith("前提字段缺失")

    absent = judge(rule, {"annual_leave_days": "5", "work_years": "12"})
    assert absent.status is RuleStatus.NOT_APPLICABLE
    assert absent.missing_fields == ()
    assert absent.reason == "前提不满足"


def test_missing_judged_field_is_unknown() -> None:
    """前提满足但判定字段没给：判不了，不是合规。"""
    outcome = judge(_rule("R-AL-5D"), {"work_years": "6"})
    assert outcome.status is RuleStatus.UNKNOWN
    assert outcome.value is None
    assert outcome.missing_fields == ("annual_leave_days",)
    assert outcome.reason == "判定字段缺失: annual_leave_days"


def test_unparsable_value_is_unknown_not_compliant() -> None:
    outcome = judge(_rule("R-PROBATION-ONCE"), {"probation_times": "若干次"})
    assert outcome.status is RuleStatus.UNKNOWN
    assert outcome.value is None
    assert outcome.reason == "字段值无法解析"


def test_violated_set_matches_evaluate() -> None:
    """改造前的行为不能被悄悄改掉：两种口径的违规集合必须逐例一致。"""
    rules = load_rules(RULES_PATH)
    fact_sets = _all_fact_sets(rules)
    for facts in fact_sets:
        judged = {o.rule.rule_id for o in judge_all(rules, facts) if o.status is RuleStatus.VIOLATED}
        legacy = {hit.rule.rule_id for hit in evaluate(rules, facts) if hit.violated}
        assert judged == legacy, facts


def test_every_rule_gets_exactly_one_outcome() -> None:
    rules = load_rules(RULES_PATH)
    outcomes = judge_all(rules, {"term_years": "1"})
    assert len(outcomes) == len(rules)
    assert [o.rule.rule_id for o in outcomes] == [r.rule_id for r in rules]
    assert all(isinstance(o.status, RuleStatus) for o in outcomes)


def test_premise_alignment_with_rule_applies() -> None:
    """前提层与 rule_applies 同口径：不适用永远不会被算成适用。"""
    rules = load_rules(RULES_PATH)
    fact_sets = _all_fact_sets(rules)
    for facts in fact_sets:
        for rule in rules:
            status = judge(rule, facts).status
            applies = rule_applies(rule, facts)
            if status in (RuleStatus.VIOLATED, RuleStatus.COMPLIANT):
                assert applies is True, (rule.rule_id, facts)
            if status is RuleStatus.NOT_APPLICABLE:
                assert applies is False, (rule.rule_id, facts)


def test_judge_all_is_deterministic() -> None:
    rules = load_rules(RULES_PATH)
    facts = {"term_years": "1", "term_type": "固定期限", "probation_months": "6", "work_years": "6"}
    first = judge_all(rules, facts)
    second = judge_all(rules, facts)
    assert [(o.rule.rule_id, o.status, o.value, o.missing_fields, o.reason) for o in first] == [
        (o.rule.rule_id, o.status, o.value, o.missing_fields, o.reason) for o in second
    ]
