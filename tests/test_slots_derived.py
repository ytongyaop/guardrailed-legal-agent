"""槽位由规则依赖自动导出：字段集、阻断项、needed_by 对账、已填字段剔除、文案可用。"""

from __future__ import annotations

import re
from pathlib import Path

from mla.config import data_path
from mla.domain.field_catalog import FIELD_HINTS, FIELD_NAMES, FIELD_QUESTIONS
from mla.domain.models import RuleEntry
from mla.domain.rules import load_rules
from mla.domain.slots import (
    DEFAULT_SLOTS,
    EXTRA_SLOT_FIELDS,
    blocking_slots,
    derive_slots,
    missing_slots,
)

RULES = load_rules(Path(data_path("rules/rules.yaml")))

# 规则表实际依赖 19 个字段，加 2 项合同外事实（主体类型、是否书面订立）
EXPECTED_FIELDS = 21


def _fields_of(rule: RuleEntry) -> tuple[str, ...]:
    """一条规则引用的字段：判定字段 + 两组前提字段，去空去重保序。"""
    return tuple(dict.fromkeys(f for f in (rule.field, rule.when_field, rule.when_field2) if f))


def test_field_set_is_complete() -> None:
    """导出结果覆盖全部规则字段与两项合同外事实，一个不多一个不少。"""
    slots = derive_slots(RULES, {})
    assert len(slots) == EXPECTED_FIELDS
    used = {f for rule in RULES for f in _fields_of(rule)}
    assert used | set(EXTRA_SLOT_FIELDS) == {s.slot_id for s in slots}
    assert len(used | set(EXTRA_SLOT_FIELDS)) == EXPECTED_FIELDS


def test_blocking_slots_are_exactly_three() -> None:
    assert [s.slot_id for s in derive_slots(RULES, {}) if s.blocking] == [
        "employer_type",
        "written_contract",
        "term_years",
    ]
    assert [s.slot_id for s in blocking_slots()] == [s.slot_id for s in DEFAULT_SLOTS if s.blocking]


def test_needed_by_matches_rule_dependencies() -> None:
    """双向对账：字段的 needed_by 恰是引用它的规则，反向无虚标、无漏登。"""
    by_id = {s.slot_id: s for s in derive_slots(RULES, {})}
    for rule in RULES:
        for field_id in _fields_of(rule):
            assert rule.rule_id in by_id[field_id].needed_by, (field_id, rule.rule_id)
    for slot in by_id.values():
        expected = [rule.rule_id for rule in RULES if slot.slot_id in _fields_of(rule)]
        assert list(slot.needed_by) == expected, slot.slot_id


def test_written_contract_has_no_rule_reference() -> None:
    """合同外事实没有规则引用时，needed_by 必须为空，不得虚标。"""
    by_id = {s.slot_id: s for s in derive_slots(RULES, {})}
    assert by_id["written_contract"].needed_by == ()
    assert by_id["employer_type"].needed_by == ()
    assert by_id["months_without_contract"].needed_by == ("R-WRITTEN-1M",)


def test_filled_fields_are_dropped() -> None:
    ids = [s.slot_id for s in derive_slots(RULES, {"term_years": "3"})]
    assert "term_years" not in ids
    assert len(ids) == EXPECTED_FIELDS - 1


def test_blank_values_do_not_count_as_filled() -> None:
    ids = [s.slot_id for s in derive_slots(RULES, {"term_years": "   "})]
    assert "term_years" in ids


def test_three_dicts_share_keys() -> None:
    assert set(FIELD_NAMES) == set(FIELD_QUESTIONS) == set(FIELD_HINTS)


def test_questions_are_usable() -> None:
    """问句长度 12 至 40 字、以问号结尾、不引条号、不下结论。"""
    for slot in derive_slots(RULES, {}):
        question = slot.question
        assert 12 <= len(question) <= 40, (slot.slot_id, len(question))
        assert question.endswith("？"), slot.slot_id
        assert not re.search(r"第\s*\d+\s*条", question), slot.slot_id
        assert "违法" not in question and "无效" not in question, slot.slot_id


def test_missing_slots_counts_blocking_only() -> None:
    """补问轮数只受阻断项影响：缺可补充项不算缺。"""
    assert [s.slot_id for s in missing_slots({})] == ["employer_type", "written_contract", "term_years"]
    assert [s.slot_id for s in missing_slots({"employer_type": "有限责任公司"})] == [
        "written_contract",
        "term_years",
    ]
    assert missing_slots({"employer_type": "公司", "written_contract": "是", "term_years": "3"}) == ()
