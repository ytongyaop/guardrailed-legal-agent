"""分级重构（规格 003 第 3.4 节 / P2-5）：两层映射、影响度来源与排序的纯函数测试。

不跑模型、不读环境：要素骨架全部注入固定表；另有一条读包内文件，守的是
"EL-13 的数据本身就是中"（兜底不暗示"不重要"，避免真实条款被压到列表底部）。
"""

from __future__ import annotations

import pytest

from mla.domain.elements import Element, default_elements
from mla.domain.grading import (
    CERTAINTY_CONFLICT,
    CERTAINTY_PENDING,
    RULE_CHECK_SOURCE,
    DisplayLevel,
    certainty_for,
    display_of,
    impact_for,
    sort_items,
    to_display,
)
from mla.domain.models import CheckItem, ItemKind

pytestmark = pytest.mark.no_llm

ELEMENTS = (
    Element("EL-1", "当事人与基本信息", ("用人单位", "劳动者"), "低", ("确认主体", "确认送达", "确认身份")),
    Element("EL-3", "试用期", ("试用", "考察期"), "高", ("确认试用期", "确认期限关系", "确认次数")),
    Element("EL-5", "工作时间与休息休假", ("工作时间", "加班"), "高", ("确认工时制", "确认加班", "确认休假")),
    Element("EL-11", "变更、解除与终止", ("解除", "终止"), "高", ("确认解除情形", "确认通知期", "确认补偿")),
    Element(
        "EL-12", "违约责任与争议解决", ("争议", "仲裁"), "中", ("确认争议方式", "确认管辖", "确认违约责任")
    ),
    # 与 elements.yaml 一致：兜底要素的数据就是"中"（兜底不暗示"不重要"）
    Element("EL-13", "其他约定", ("其他约定", "附则"), "中", ("确认兜底段", "确认效力顺序", "确认修改生效")),
)


def _item(
    item_id: str,
    *,
    grade: str,
    paragraph: int,
    element_id: str = "",
    impact: str = "",
    certainty: str = "",
    finding: str = "",
    source: str = "",
) -> CheckItem:
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK if grade in ("A", "B") else ItemKind.UNKNOWN,
        clause_ref=str(paragraph) if paragraph else "",
        finding=finding or item_id,
        source_id="LCL-19",
        grade=grade,
        source=source,
        element_id=element_id,
        impact=impact,
        certainty=certainty,
    )


# ---------- 1. 确定性：A → 确定冲突，B / C / 未知 → 待确认，rule_check → 确定冲突 ----------


def test_grade_maps_to_certainty() -> None:
    assert certainty_for("A") == CERTAINTY_CONFLICT
    assert certainty_for("B") == CERTAINTY_PENDING
    assert certainty_for("C") == CERTAINTY_PENDING
    # 未知等级宁可低估，也不能当成"确定冲突"
    assert certainty_for("") == CERTAINTY_PENDING
    assert certainty_for("X") == CERTAINTY_PENDING


def test_rule_check_c_is_conflict() -> None:
    """守卫：规则数值校验条目（`source=rule_check`）即便内部等级是 C，也必须显示"确定冲突"。

    这是"程序按法条数值算出的确定不一致"与"模型判不了"的区别：前者不能落进待确认层。
    """
    assert certainty_for("C", RULE_CHECK_SOURCE) == CERTAINTY_CONFLICT
    assert certainty_for("c", "rule_check") == CERTAINTY_CONFLICT
    # 其他来源不受影响：B / C 仍是待确认
    assert certainty_for("C", "trigger") == CERTAINTY_PENDING
    assert certainty_for("B", "model_extra") == CERTAINTY_PENDING


def test_to_display_all_fields() -> None:
    level = to_display("A", "EL-3", 20, ELEMENTS)
    assert isinstance(level, DisplayLevel)
    assert (level.impact, level.certainty) == ("高", CERTAINTY_CONFLICT)
    assert level.sort_key == (0, 0, 0, 20)


def test_to_display_passes_source_through() -> None:
    """`to_display` 必须把来源透传到确定性判定，否则共识层算出的 rule_check 条目会落错层。"""
    level = to_display("C", "EL-12", 46, ELEMENTS, source=RULE_CHECK_SOURCE)
    assert (level.impact, level.certainty) == ("中", CERTAINTY_CONFLICT)
    assert level.sort_key == (1, 0, 1, 46)


# ---------- 2. 影响度取要素值 ----------


def test_impact_comes_from_element() -> None:
    assert impact_for("EL-3", ELEMENTS) == "高"
    assert impact_for("EL-12", ELEMENTS) == "中"
    assert impact_for("EL-1", ELEMENTS) == "低"
    assert to_display("C", "EL-12", 46, ELEMENTS).impact == "中"


def test_model_suggest_never_beats_element() -> None:
    diagnostics: list[str] = []
    level = to_display("A", "EL-3", 20, ELEMENTS, impact_suggest="低", diagnostics=diagnostics)
    assert level.impact == "高", "模型建议与要素不一致时必须以要素为准"
    assert diagnostics == ["第 20 段：模型建议影响度「低」与要素「EL-3」的「高」不一致，按要素值处理"]


def test_matching_suggest_adds_no_diagnostic() -> None:
    diagnostics: list[str] = []
    level = to_display("A", "EL-3", 20, ELEMENTS, impact_suggest="高", diagnostics=diagnostics)
    assert level.impact == "高"
    assert diagnostics == []


def test_out_of_domain_suggest_is_ignored_with_diagnostic() -> None:
    diagnostics: list[str] = []
    level = to_display("B", "EL-12", 46, ELEMENTS, impact_suggest="极高", diagnostics=diagnostics)
    assert level.impact == "中"
    assert len(diagnostics) == 1 and "不在 高/中/低 内" in diagnostics[0]


# ---------- 3. 兜底与未知要素：都按"中"（防降级守卫） ----------


def test_fallback_element_is_mid() -> None:
    # 兜底要素的数据本身是"中"（elements.yaml / 注入表一致），不得被当成"低"
    assert impact_for("EL-13", ELEMENTS) == "中"
    assert to_display("C", "EL-13", 47, ELEMENTS).impact == "中"


def test_unknown_element_is_mid() -> None:
    assert impact_for("", ELEMENTS) == "中"
    assert impact_for("EL-999", ELEMENTS) == "中"
    assert to_display("C", "EL-999", 47, ELEMENTS).impact == "中"


def test_packaged_yaml_fallback_is_mid() -> None:
    """包内骨架的兜底要素必须是"中"：数据若退回"低"，真实条款会被压到列表底部。"""
    yaml_fallback = next(e for e in default_elements() if e.element_id == "EL-13")
    assert yaml_fallback.impact == "中"
    assert impact_for("EL-13") == "中"


# ---------- 4. 排序：影响度 → 确定性 → 来源 → 段落号 ----------


def test_same_impact_conflict_before_pending_then_paragraph() -> None:
    items = [
        _item("C-30", grade="C", paragraph=30, element_id="EL-3"),
        _item("A-24", grade="A", paragraph=24, element_id="EL-5"),
        _item("C-12", grade="C", paragraph=12, element_id="EL-11"),
        _item("B-20", grade="B", paragraph=20, element_id="EL-11"),
    ]
    ordered = sort_items(items, ELEMENTS)
    # 三条都是高影响：A 是确定冲突排最前，B 与 C 同为待确认，按段落号升序
    assert [i.item_id for i in ordered] == ["A-24", "C-12", "B-20", "C-30"]
    assert [display_of(i, ELEMENTS).certainty for i in ordered] == [
        CERTAINTY_CONFLICT,
        CERTAINTY_PENDING,
        CERTAINTY_PENDING,
        CERTAINTY_PENDING,
    ]


def test_same_impact_and_certainty_a_before_rule_check() -> None:
    """同影响度、同确定冲突层内：有段落的 A 级排在无段落的规则数值校验条目前面。

    rule_check 的段落键恒为 0，若不加"来源"这一键会插到 A 级前面（旧 bug）。
    """
    items = [
        _item("RC-1", grade="C", paragraph=0, element_id="EL-3", source="rule_check"),
        _item("A-20", grade="A", paragraph=20, element_id="EL-3"),
    ]
    ordered = sort_items(items, ELEMENTS)
    assert [i.item_id for i in ordered] == ["A-20", "RC-1"]
    assert [display_of(i, ELEMENTS).certainty for i in ordered] == [
        CERTAINTY_CONFLICT,
        CERTAINTY_CONFLICT,
    ]
    # 待确认层不受影响：来源键都是 0，仍按段落号排序
    pending = [
        _item("P-9", grade="C", paragraph=9, element_id="EL-3"),
        _item("B-5", grade="B", paragraph=5, element_id="EL-3"),
    ]
    assert [i.item_id for i in sort_items(pending, ELEMENTS)] == ["B-5", "P-9"]


def test_impact_group_order_high_mid_low() -> None:
    items = [
        _item("low", grade="C", paragraph=1, element_id="EL-1"),
        _item("mid", grade="B", paragraph=2, element_id="EL-12"),
        _item("high", grade="C", paragraph=3, element_id="EL-11"),
    ]
    assert [i.item_id for i in sort_items(items, ELEMENTS)] == ["high", "mid", "low"]
    assert [display_of(i, ELEMENTS).impact for i in sort_items(items, ELEMENTS)] == ["高", "中", "低"]


def test_sort_items_is_stable_and_does_not_mutate() -> None:
    first = _item("first", grade="B", paragraph=0, element_id="EL-13")
    second = _item("second", grade="C", paragraph=0, element_id="EL-13")
    items = [first, second]
    ordered = sort_items(items, ELEMENTS)
    assert items == [first, second]
    # 四键完全相同（同影响度、同确定性、同来源、同段落）→ 保持输入顺序
    assert [i.item_id for i in ordered] == ["first", "second"]


def test_display_of_prefers_filled_fields() -> None:
    # 共识层已填好展示字段时不再重复求值（即使 element_id 为空也不回落）
    # 这里刻意让存值与等级不一致，只为验证"已填就用填的"这一优先级
    filled = _item("filled", grade="B", paragraph=30, impact="高", certainty=CERTAINTY_CONFLICT)
    level = display_of(filled, ELEMENTS)
    assert (level.impact, level.certainty) == ("高", CERTAINTY_CONFLICT)
    # 旧记录没填：按 grade 现算；无 element_id → 中
    legacy = _item("legacy", grade="A", paragraph=30)
    assert (display_of(legacy, ELEMENTS).impact, display_of(legacy, ELEMENTS).certainty) == (
        "中",
        CERTAINTY_CONFLICT,
    )


def test_display_of_ignores_stale_rule_check_certainty() -> None:
    """rule_check 条目落库值若是旧口径（待确认），展示层必须按来源重算成确定冲突。"""
    stale = _item(
        "RC-1", grade="C", paragraph=4, element_id="EL-3", certainty=CERTAINTY_PENDING, source="rule_check"
    )
    assert display_of(stale, ELEMENTS).certainty == CERTAINTY_CONFLICT


def test_full_demo_order() -> None:
    """规格验收样例：高影响两条确定冲突 + 一条待确认，中影响一条确定冲突 + 兜底条目（EL-13 落中影响）。"""
    items = [
        _item("p47", grade="C", paragraph=47, element_id="EL-13"),  # 兜底 → 中影响
        _item("p30", grade="C", paragraph=30, element_id="EL-3"),
        _item("p24", grade="A", paragraph=24, element_id="EL-5"),
        _item("p20", grade="A", paragraph=20, element_id="EL-11"),
        _item("p46", grade="A", paragraph=46, element_id="EL-12"),
    ]
    ordered = sort_items(items, ELEMENTS)
    assert [
        (display_of(i, ELEMENTS).impact, display_of(i, ELEMENTS).certainty, i.item_id) for i in ordered
    ] == [
        ("高", CERTAINTY_CONFLICT, "p20"),
        ("高", CERTAINTY_CONFLICT, "p24"),
        ("高", CERTAINTY_PENDING, "p30"),
        ("中", CERTAINTY_CONFLICT, "p46"),
        ("中", CERTAINTY_PENDING, "p47"),
    ]
