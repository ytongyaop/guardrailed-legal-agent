"""20 条确定性规则的阈值边界测试（P0-1 回归）。

背景：`R-NONCOMPETE-UNPAID-3M` 原先写的是 `op: "<=", threshold: 3`，于是
"满 3 个月未支付竞业限制补偿"被判成**合规**。法条原文是"因用人单位的原因导致
**三个月**未支付经济补偿"（J1-38），边界应是 `< 3`——满三个月即触发。
本文件对规则表逐条钉住阈值边界，防止同类"边界偏一格"的错误再犯。

语义说明：`R-NONCOMPETE-UNPAID-3M`（"劳动者可请求解除"）本质是**权利触发**，
不是"用人单位违规"。规则层目前只有合规 / 违规两态，所以它仍以 `VIOLATED`
表达；语义上应读作"风险触发"，后续若引入规则类型字段（kind）再细分。

做法：
- 每条规则先按 `when_field` / `when_field2` / `when_mode` 构造一组**满足前提**的事实
  （前提不满足时规则根本不适用，测不到边界；构造不出来直接失败，不静默跳过）；
- 再取 `threshold - step`、`threshold`、`threshold + step` 三点（整数阈值 step=1，
  浮点阈值 step=0.5），按 `op` 的语义断言三点状态；
- 参数化条数必须等于规则表条数（`EXPECTED_RULE_COUNT`），漏一条即失败。
"""

from __future__ import annotations

import operator
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from mla.config import data_path
from mla.domain.models import RuleEntry
from mla.domain.rules import RuleStatus, judge, load_rules, rule_applies

RULES_PATH = Path(data_path("rules/rules.yaml"))

# 规则表当前有 20 条；改条数必须同步改边界测试，否则下面会报错而不是悄悄漏测。
EXPECTED_RULE_COUNT = 20

# 反向守卫用的白名单：op 必须能和数字阈值组成可判定的边界。
ALLOWED_OPS = ("<", "<=", ">", ">=", "==", "!=")
_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}

# 构造字符串前提（如 term_type == "无固定期限"）时的候选值。
_STRING_CANDIDATES = ("固定期限", "无固定期限", "以完成一定工作任务为期限")

RULES = load_rules(RULES_PATH)


def _raw_rows() -> list[dict[str, object]]:
    """规则表原始行：反向守卫直接看 YAML，不依赖加载器做了多少校验。"""
    rows = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(rows, list), "规则表顶层必须是列表"
    return [row for row in rows if isinstance(row, dict)]


def _as_number(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _format(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _step(bound: float) -> float:
    """整数阈值走 1，浮点阈值走 0.5——保证三点等距且落在阈值两侧。"""
    return 1.0 if float(bound).is_integer() else 0.5


def _numeric_candidate(conditions: list[tuple[str, float]]) -> float | None:
    """挑一个同时满足全部 (op, bound) 的数；挑不出来返回 None。"""
    candidates: list[float] = []
    for _, bound in conditions:
        delta = _step(bound)
        candidates.extend((bound, bound + delta, bound - delta, bound * 2))
    usable = sorted({c for c in candidates if all(_OPS[op](c, bound) for op, bound in conditions)})
    return usable[0] if usable else None


def _string_candidate(op: str, bound: str) -> str | None:
    if op == "==":
        return bound
    if op != "!=":
        raise AssertionError(f"字符串前提不支持比较符 {op}（只有 == / !=）")
    for candidate in _STRING_CANDIDATES:
        if candidate != bound:
            return candidate
    return None


def _facts_for_groups(groups: Sequence[tuple[str, str, object]]) -> dict[str, str] | None:
    """把若干 (field, op, bound) 前提翻译成一组事实；做不到返回 None。"""
    by_field: dict[str, list[tuple[str, float]]] = {}
    text: dict[str, str] = {}
    for field, op, bound in groups:
        number = _as_number(bound)
        if number is not None:
            by_field.setdefault(field, []).append((op, number))
            continue
        value = _string_candidate(op, str(bound))
        if value is None:
            return None
        text[field] = value

    facts: dict[str, str] = {}
    for field, conditions in by_field.items():
        number_value = _numeric_candidate(conditions)
        if number_value is None:
            return None
        facts[field] = _format(number_value)
    facts.update(text)
    return facts


def _premise_facts(rule: RuleEntry) -> dict[str, str]:
    """构造满足该规则前提的事实；`or` 下两组都满足优先，否则任选一组能满足的。"""
    groups = [
        (rule.when_field, rule.when_op, rule.when_value),
        (rule.when_field2, rule.when_op2, rule.when_value2),
    ]
    defined = [group for group in groups if group[0]]
    facts = _facts_for_groups(defined)
    if facts is None and rule.when_mode == "or":
        for group in defined:
            facts = _facts_for_groups([group])
            if facts is not None:
                break
    assert facts is not None, f"{rule.rule_id} 的前提构造不出满足条件的事实: {defined}"
    assert rule_applies(rule, facts) is True, f"{rule.rule_id} 构造的前提实际不满足: {facts}"
    return facts


def _boundary_points(threshold: float) -> tuple[float, float, float]:
    delta = _step(threshold)
    return threshold - delta, threshold, threshold + delta


def _expected_statuses(op: str) -> tuple[RuleStatus, RuleStatus, RuleStatus]:
    """按 op 的"合法区间"语义给出低 / 等 / 高三点的期望状态。"""
    compliant, violated = RuleStatus.COMPLIANT, RuleStatus.VIOLATED
    if op == "<":
        return compliant, violated, violated
    if op == "<=":
        return compliant, compliant, violated
    if op == ">":
        return violated, violated, compliant
    if op == ">=":
        return violated, compliant, compliant
    if op == "==":
        return violated, compliant, violated
    if op == "!=":
        return compliant, violated, compliant
    raise AssertionError(f"未知比较符 {op}")


def _rule(rule_id: str) -> RuleEntry:
    matches = [rule for rule in RULES if rule.rule_id == rule_id]
    assert len(matches) == 1, rule_id
    return matches[0]


@pytest.mark.parametrize("rule", RULES, ids=[rule.rule_id for rule in RULES])
def test_rule_boundary(rule: RuleEntry) -> None:
    """每条规则三组边界事实：低于阈值 / 等于阈值 / 高于阈值。"""
    threshold = _as_number(rule.threshold)
    assert threshold is not None, f"{rule.rule_id} 的阈值无法解析: {rule.threshold!r}"
    premise = _premise_facts(rule)
    points = _boundary_points(threshold)
    expected = _expected_statuses(rule.op)

    for value, want in zip(points, expected, strict=True):
        facts = dict(premise)
        facts[rule.field] = _format(value)
        outcome = judge(rule, facts)
        assert outcome.status is want, (
            f"{rule.rule_id}: op={rule.op} threshold={rule.threshold} value={_format(value)} "
            f"期望 {want.value}，实际 {outcome.status.value}"
        )


def test_boundary_cases_cover_every_rule() -> None:
    """参数化条数必须等于规则表条数：新增规则却忘了补边界会在这里暴露。"""
    assert len(RULES) == EXPECTED_RULE_COUNT, f"规则表条数变了: {len(RULES)}"
    rows = _raw_rows()
    assert len(rows) == EXPECTED_RULE_COUNT, f"YAML 条数变了: {len(rows)}"
    assert [rule.rule_id for rule in RULES] == [str(row["rule_id"]) for row in rows], (
        "加载后的规则顺序与 YAML 不一致，参数化可能漏掉或多出规则"
    )


def test_every_rule_declares_valid_op_and_numeric_threshold() -> None:
    """反向守卫：op 必须在白名单内、threshold 必须可解析为数字，否则边界无从验证。"""
    for row in _raw_rows():
        rule_id = row.get("rule_id")
        assert row.get("op") in ALLOWED_OPS, f"{rule_id} 的 op 非法: {row.get('op')!r}"
        assert _as_number(row.get("threshold")) is not None, (
            f"{rule_id} 的 threshold 无法解析为数字: {row.get('threshold')!r}"
        )


def test_noncompete_unpaid_boundary_regression() -> None:
    """P0-1 回归：法条"三个月未支付"= 满三个月即触发，op 必须是 "<"。"""
    rule = _rule("R-NONCOMPETE-UNPAID-3M")
    assert rule.op == "<"
    assert rule.threshold == 3
    statuses = [judge(rule, {"noncompete_unpaid_months": value}).status for value in ("2", "3", "4")]
    assert statuses == [RuleStatus.COMPLIANT, RuleStatus.VIOLATED, RuleStatus.VIOLATED]
