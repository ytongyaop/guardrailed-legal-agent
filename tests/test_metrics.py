"""指标纯函数测试：P/R、可追溯率、无依据率、达标判定。"""

from __future__ import annotations

from mla.domain.models import CheckItem, ItemKind, Severity, Suggestion
from mla.eval.metrics import (
    LOWER_IS_BETTER,
    micro_accuracy,
    precision_recall,
    rate,
    report,
    traceable_rate,
    unsourced_suggestion_rate,
)


def _item(source_id: str) -> CheckItem:
    return CheckItem(
        item_id="R-1",
        kind=ItemKind.RISK,
        clause_ref="",
        finding="示例",
        source_id=source_id,
        severity=Severity.HARD,
        rule_id="R-1",
    )


def test_precision_recall_basic() -> None:
    assert precision_recall({"a", "b"}, {"b", "c"}) == (0.5, 0.5)
    assert precision_recall(set(), set()) == (1.0, 1.0)
    assert precision_recall({"a"}, set()) == (0.0, 1.0)  # 多报：精确率 0，召回不缺
    assert precision_recall(set(), {"a"}) == (0.0, 0.0)


def test_micro_accuracy_is_jaccard() -> None:
    assert micro_accuracy({"a"}, {"a", "b"}) == 0.5
    assert micro_accuracy(set(), set()) == 1.0


def test_traceable_rate_flags_unknown_source() -> None:
    items = [_item("LCL-19"), _item("UNKNOWN")]
    assert traceable_rate(items, {"LCL-19"}) == 0.5
    assert traceable_rate([], set()) == 1.0


def test_unsourced_suggestion_rate() -> None:
    good = Suggestion(text="改", source_ids=("LCL-19",))
    bad = Suggestion(text="改", source_ids=())
    assert unsourced_suggestion_rate([good, bad]) == 0.5
    assert unsourced_suggestion_rate([]) == 0.0


def test_rate_handles_empty() -> None:
    assert rate([]) == 0.0
    assert rate([True, False]) == 0.5


def test_report_uses_threshold_direction() -> None:
    result = report({"recall": 0.9, "false_refuse": 0.2, "latency_s": 10.0})
    assert result["recall"] is True  # 越大越好
    assert result["false_refuse"] is False  # 越小越好
    assert result["latency_s"] is True
    assert "false_refuse" in LOWER_IS_BETTER
