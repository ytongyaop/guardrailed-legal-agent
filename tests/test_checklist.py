"""清单建议测试：同一句建议命中多条规则时合并文案、并集来源。"""

from __future__ import annotations

from mla.domain.checklist import build_suggestions
from mla.domain.models import CheckItem, ItemKind, Severity


def _item(rule_id: str, source_id: str) -> CheckItem:
    return CheckItem(
        item_id=rule_id,
        kind=ItemKind.RISK,
        clause_ref="",
        finding="示例",
        source_id=source_id,
        severity=Severity.HARD,
        rule_id=rule_id,
    )


def test_same_template_is_merged_with_union_sources() -> None:
    suggestions = build_suggestions(
        [_item("R-PROBATION-6M", "LCL-19"), _item("R-PROBATION-WAGE-80", "LCR-15")]
    )
    assert len(suggestions) == 1
    assert suggestions[0].source_ids == ("LCL-19", "LCR-15")


def test_different_templates_stay_separate() -> None:
    suggestions = build_suggestions([_item("R-PROBATION-6M", "LCL-19"), _item("R-WRITTEN-1M", "LCL-10")])
    assert len(suggestions) == 2
    assert all(s.source_ids for s in suggestions)
