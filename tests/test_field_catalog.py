"""字段中文名目录：规则用到的每个字段都得有中文名，查不到时原样返回 id。"""

from __future__ import annotations

from mla.domain.field_catalog import FIELD_NAMES, field_name
from mla.domain.rules import load_rules

RULES = "data/rules/rules.yaml"


def test_every_rule_field_has_chinese_name() -> None:
    """新增规则字段时先在这里报错，避免界面上漏出原始 id。"""
    used: set[str] = set()
    for rule in load_rules(RULES):
        for value in (rule.field, rule.when_field, rule.when_field2):
            if value:
                used.add(value)
    assert used
    missing = sorted(used - set(FIELD_NAMES))
    assert not missing, f"以下字段缺中文名：{missing}"


def test_unknown_field_returns_id() -> None:
    assert field_name("no_such_field") == "no_such_field"
    assert field_name("") == ""
    assert field_name("annual_leave_days") == "年休假天数"
