"""骨架契约测试：保证数据结构与阈值口径不漂移（纯逻辑，无外部依赖）。"""

from __future__ import annotations

from datetime import date

from mla.domain.models import (
    CaseEntry,
    CheckItem,
    ItemKind,
    StatuteEntry,
    StatuteStatus,
)
from mla.domain.policy import ACCEPTANCE
from mla.domain.slots import DEFAULT_SLOTS, blocking_slots


def test_acceptance_has_nine_metrics() -> None:
    assert len(ACCEPTANCE) == 9
    assert ACCEPTANCE["traceable"] == 1.0
    assert ACCEPTANCE["unsourced_suggestion"] == 0.0


def test_slots_have_blocking_subset() -> None:
    assert len(DEFAULT_SLOTS) >= 5
    assert len(blocking_slots()) > 0


def test_models_are_constructible() -> None:
    s = StatuteEntry(
        statute_id="LCL-19",
        law_name="劳动合同法",
        article_no="第十九条",
        text="试用期不得超过…",
        issuing_body="全国人民代表大会常务委员会",
        effective_date=date(2008, 1, 1),
        status=StatuteStatus.EFFECTIVE,
    )
    item = CheckItem(
        item_id="1", kind=ItemKind.RISK, clause_ref="第三条", finding="…", source_id=s.statute_id
    )
    c = CaseEntry(case_id="C1", title="…", source="最高人民法院典型案例", holding="…")
    assert item.source_id == "LCL-19"
    assert c.case_id == "C1"
