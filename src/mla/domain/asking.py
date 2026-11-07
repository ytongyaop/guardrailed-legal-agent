"""可补充事实的候选字段排序（纯函数，零模型、零 I/O）。

阻断项不在此列：它们缺了就不能出结论，由固定追问清单负责。
这一层只回答一个问题：补哪个字段能让更多规则从"判不了"变成"可判定"。
排序键是（受影响的规则条数降序，字段目录顺序），同一份事实每次得到同一个结果。
"""

from __future__ import annotations

from .field_catalog import FIELD_NAMES
from .models import RuleEntry
from .slots import derive_slots

# 字段目录里的原有顺序，用作同分时的稳定顺序（目录外字段排在最后）
_FIELD_ORDER = {field_id: index for index, field_id in enumerate(FIELD_NAMES)}


def _rank(field_id: str) -> tuple[int, str]:
    """同分时的次序：先目录顺序，目录里没有的按 id 排，保证确定性。"""
    return (_FIELD_ORDER.get(field_id, len(FIELD_NAMES)), field_id)


def candidate_fields(
    rules: tuple[RuleEntry, ...], facts: dict[str, str], *, limit: int = 8
) -> tuple[str, ...]:
    """按缺失该字段会导致多少条规则判不了，降序返回最多 limit 个可补充字段。"""
    if limit <= 0:
        return ()
    pool = [slot for slot in derive_slots(rules, facts) if not slot.blocking]
    ordered = sorted(pool, key=lambda slot: (-len(slot.needed_by), _rank(slot.slot_id)))
    return tuple(slot.slot_id for slot in ordered[:limit])
