"""必问事实清单（slot schema）：纯数据 + 纯函数，零模型参与。

槽位不再手写，改为按规则表实际依赖的字段自动导出：
- 阻断项（blocking=True）：固定为 `BLOCKING_SLOTS` 三项，缺了不能出结论，必须追问；
- 可补充项（blocking=False）：规则依赖的其余字段 + 两项合同外事实，缺了不阻断，
  只在结果页提示补齐还能多判几条，避免把什么都当必答。

追问句、中文名、填写提示统一来自 `field_catalog`，不在此另存一份。
"""

from __future__ import annotations

from .field_catalog import FIELD_NAMES, field_name, field_question
from .models import RuleEntry, SlotSpec

# 缺了就不能出结论的三项：适用法域（主体类型）、是否书面订立、期限决定试用期上限
BLOCKING_SLOTS: tuple[str, ...] = ("employer_type", "written_contract", "term_years")

# 不在规则表里、但属于合同外事实的两项
EXTRA_SLOT_FIELDS: tuple[str, ...] = ("employer_type", "written_contract")


def _field_refs(rules: tuple[RuleEntry, ...]) -> dict[str, list[str]]:
    """字段到规则 id 的映射：判定字段与两组前提字段都算依赖，保持规则表顺序。"""
    refs: dict[str, list[str]] = {}
    for rule in rules:
        for field_id in (rule.field, rule.when_field, rule.when_field2):
            if not field_id:
                continue
            bucket = refs.setdefault(field_id, [])
            if rule.rule_id not in bucket:
                bucket.append(rule.rule_id)
    return refs


def _candidate_fields(rules: tuple[RuleEntry, ...]) -> tuple[str, ...]:
    """候选字段与输出顺序：先阻断项，再两项合同外事实，再目录顺序，最后兜底收目录外字段。"""
    ordered: list[str] = list(BLOCKING_SLOTS)
    for field_id in (*EXTRA_SLOT_FIELDS, *FIELD_NAMES, *_field_refs(rules)):
        if field_id not in ordered:
            ordered.append(field_id)
    return tuple(ordered)


def derive_slots(rules: tuple[RuleEntry, ...], facts: dict[str, str]) -> tuple[SlotSpec, ...]:
    """按规则实际依赖的字段生成槽位，只保留当前 facts 里缺的字段。"""
    refs = _field_refs(rules)
    filled = {key for key, value in facts.items() if str(value).strip()}
    return tuple(
        SlotSpec(
            slot_id=field_id,
            name=field_name(field_id),
            question=field_question(field_id),
            blocking=field_id in BLOCKING_SLOTS,
            needed_by=tuple(refs.get(field_id, ())),
        )
        for field_id in _candidate_fields(rules)
        if field_id not in filled
    )


DEFAULT_SLOTS: tuple[SlotSpec, ...] = derive_slots((), {})


def blocking_slots() -> tuple[SlotSpec, ...]:
    """阻断性槽位（缺了不能出结论）。"""
    return tuple(s for s in DEFAULT_SLOTS if s.blocking)


def missing_slots(facts: dict[str, str]) -> tuple[SlotSpec, ...]:
    """仍未提供的阻断性槽位（空字符串视为未提供）。"""
    return tuple(s for s in blocking_slots() if not str(facts.get(s.slot_id, "")).strip())


def questions_for(slot_ids: list[str] | tuple[str, ...]) -> list[str]:
    """把槽位 ID 换成面向用户的追问句，保持清单顺序。"""
    wanted = set(slot_ids)
    return [s.question for s in DEFAULT_SLOTS if s.slot_id in wanted]
