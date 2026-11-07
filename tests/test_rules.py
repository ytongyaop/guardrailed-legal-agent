"""规则层测试：区间前提、合法区间、缺字段不猜、每条规则可回到原文。"""

from __future__ import annotations

import json
from pathlib import Path

from mla.domain.rules import RuleHit, evaluate, load_rules

RULES_PATH = Path("data/rules/rules.yaml")
STATUTES = Path("data/statutes/statutes.jsonl")


def _hits(facts: dict[str, str]) -> list[RuleHit]:
    return [h for h in evaluate(load_rules(RULES_PATH), facts) if h.violated]


def test_rules_loaded() -> None:
    rules = load_rules(RULES_PATH)
    assert len(rules) == 20
    ids = {r.rule_id for r in rules}
    assert "R-PROBATION-6M" in ids
    assert "R-OT-WEEKDAY-150" in ids
    assert all(r.evidence_span for r in rules)


def test_every_rule_traces_to_statute_text() -> None:
    """每条规则的 evidence_span 必须是条文的子串（防凭空造规则）。"""
    statutes = {}
    for line in STATUTES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            statutes[row["statute_id"]] = "".join(str(row["text"]).split())
    for rule in load_rules(RULES_PATH):
        text = statutes.get(rule.statute_id, "")
        assert text, f"法条缺失：{rule.statute_id}"
        assert "".join(rule.evidence_span.split()) in text, f"{rule.rule_id} 依据不在原文中"


def test_probation_violation_three_year_term() -> None:
    hits = _hits({"term_years": "3", "probation_months": "12"})
    assert [h.rule.rule_id for h in hits] == ["R-PROBATION-6M"]


def test_probation_range_exclusive() -> None:
    """一年期合同只走"一至三年"那条，不串到三年那条。"""
    hits = _hits({"term_years": "1", "probation_months": "6"})
    assert [h.rule.rule_id for h in hits] == ["R-PROBATION-2M"]


def test_compliant_probation_not_flagged() -> None:
    assert _hits({"term_years": "3", "probation_months": "3"}) == []


def test_missing_fact_is_not_guessed() -> None:
    """只有合同期限、没有试用期月数时，不应产出试用期相关命中。"""
    hits = _hits({"term_years": "3"})
    assert all("PROBATION" not in h.rule.rule_id for h in hits)


def test_annual_leave_threshold() -> None:
    hits = _hits({"work_years": "12", "annual_leave_days": "5"})
    assert [h.rule.rule_id for h in hits] == ["R-AL-10D"]


def test_overtime_ratio() -> None:
    hits = _hits({"ot_weekday_ratio": "120"})
    assert [h.rule.rule_id for h in hits] == ["R-OT-WEEKDAY-150"]
