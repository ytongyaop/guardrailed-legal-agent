"""确定性规则层：命中即给结论并标注法条 ID；规则未覆盖才交给检索 + 模型。

约定：
- `RuleEntry.op` 表示**合法区间**的比较符（"试用期不得超过 6 个月" → `op="<=", threshold=6`），
  因此 `violated = not op(value, threshold)`。
- `when_*`（最多两组）是适用前提，用于区间条件（如"一年以上不满三年"）；前提不满足则跳过。
  `when_mode` 为 `and`（默认，两组都满足）/ `or`（任一满足，用于"三年以上**或**无固定期限"）。
- `evidence_span` 是法条原文片段，由 `scripts/validate_rules.py` 校验其必须是条文子串。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

import yaml

from .models import RuleEntry, Severity

_OPS = {
    "<=": operator.le,
    ">=": operator.ge,
    "<": operator.lt,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}

_REQUIRED = ("rule_id", "name", "field", "op", "threshold", "statute_id", "evidence_span")

_WHEN_MODES = ("and", "or")


@dataclass(frozen=True)
class RuleHit:
    """一条规则的判定结果（value=事实值，violated=是否越界）。"""

    rule: RuleEntry
    value: float
    violated: bool


def load_rules(path: str | Path) -> tuple[RuleEntry, ...]:
    """读取规则表（YAML 列表）并做字段校验。"""
    raw = Path(path).read_text(encoding="utf-8")
    rows = yaml.safe_load(raw) or []
    if not isinstance(rows, list):
        raise ValueError("规则表顶层必须是列表")

    rules: list[RuleEntry] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 条规则不是对象")
        missing = [k for k in _REQUIRED if k not in row]
        if missing:
            raise ValueError(f"第 {i} 条规则缺字段: {missing}")
        if not str(row["rule_id"]).startswith("R-"):
            raise ValueError(f"规则 ID 必须以 R- 开头: {row['rule_id']}")
        if row["op"] not in _OPS:
            raise ValueError(f"不支持的比较符 {row['op']}（可用 {sorted(_OPS)}）")
        for key in ("when_op", "when_op2"):
            if key in row and row[key] not in _OPS:
                raise ValueError(f"第 {i} 条规则前提比较符非法：{row[key]}")
        if row.get("when_mode", "and") not in _WHEN_MODES:
            raise ValueError(f"第 {i} 条规则前提关系非法：{row['when_mode']}（可用 {list(_WHEN_MODES)}）")
        eff = row.get("effective_date")
        rules.append(
            RuleEntry(
                rule_id=str(row["rule_id"]),
                name=str(row["name"]),
                field=str(row["field"]),
                op=str(row["op"]),
                threshold=row["threshold"],
                statute_id=str(row["statute_id"]),
                unit=str(row.get("unit", "")),
                severity=Severity(str(row.get("severity", "硬性"))),
                effective_date=date.fromisoformat(str(eff)) if eff else None,
                note=str(row.get("note", "")),
                when_field=str(row.get("when_field", "")),
                when_op=str(row.get("when_op", ">=")),
                when_value=row.get("when_value", ""),
                when_field2=str(row.get("when_field2", "")),
                when_op2=str(row.get("when_op2", "<")),
                when_value2=row.get("when_value2", ""),
                when_mode=str(row.get("when_mode", "and")),
                evidence_span=str(row["evidence_span"]),
            )
        )
    return tuple(rules)


def _to_number(value: object) -> float | None:
    """把事实值转成数字；转不了返回 None（视为未提供）。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _guard_ok(field: str, op: str, bound: object, facts: dict[str, str]) -> bool:
    """前提判定：无前提返回 True；事实缺失返回 False（不猜）。

    两侧都能转数字 → 数值比较（原口径）；否则只在 `==` / `!=` 下做字符串精确比较
    （如 `term_type == "无固定期限"`），其余比较符一律不成立。
    """
    if not field:
        return True
    raw = facts.get(field)
    cond = _to_number(raw)
    limit = _to_number(bound)
    if cond is not None and limit is not None:
        return bool(_OPS[op](cond, limit))
    if op in ("==", "!="):
        if raw is None or not str(raw).strip():
            return False
        expected = str(bound).strip()
        if not expected:
            return False
        return bool(_OPS[op](str(raw).strip(), expected))
    return False


def rule_applies(rule: RuleEntry, facts: dict[str, str]) -> bool:
    """前提判定：`and`（默认）两组都满足才适用；`or` 任一组满足即适用。

    `or` 下空条件组视为**不满足**——否则没写前提的规则会被判成"处处适用"。
    """
    first = _guard_ok(rule.when_field, rule.when_op, rule.when_value, facts)
    second = _guard_ok(rule.when_field2, rule.when_op2, rule.when_value2, facts)
    if rule.when_mode == "or":
        return (first and bool(rule.when_field)) or (second and bool(rule.when_field2))
    return first and second


def evaluate(rules: tuple[RuleEntry, ...], facts: dict[str, str]) -> list[RuleHit]:
    """逐条判定。纯函数：不读写文件、不联网，便于单测。"""
    hits: list[RuleHit] = []
    for rule in rules:
        if not rule_applies(rule, facts):
            continue
        value = _to_number(facts.get(rule.field))
        if value is None:
            continue
        threshold = _to_number(rule.threshold)
        if threshold is None:
            continue
        violated = not _OPS[rule.op](value, threshold)
        hits.append(RuleHit(rule=rule, value=value, violated=violated))
    return hits


_GROUP_OK = "satisfied"
_GROUP_NO = "unsatisfied"
_GROUP_MISSING = "missing"


class RuleStatus(StrEnum):
    """规则判定四态：命中违规 / 判定合规 / 前提不适用 / 判不了。

    后两态必须分开：`NOT_APPLICABLE` 是"事实齐了、这条规则不管"，
    `UNKNOWN` 是"缺事实、现在还判不了"。混在一起会把"判不了"显示成"合规"。
    """

    VIOLATED = "violated"
    COMPLIANT = "compliant"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuleOutcome:
    """单条规则的一次判定：状态 + 命中值 + 缺哪些字段。

    `value` 只在拿到可比较的数字时非空；`missing_fields` 列出导致 `UNKNOWN` 的字段，
    按首次出现顺序去重；`reason` 是给人看的中文短语。
    """

    rule: RuleEntry
    status: RuleStatus
    value: float | None
    missing_fields: tuple[str, ...] = ()
    reason: str = ""


def _guard_state(field: str, op: str, bound: object, facts: dict[str, str]) -> tuple[str, str]:
    """单个前提组的三态结果：满足 / 明确不满足 / 字段缺失（附字段名）。

    口径与 `_guard_ok` 一致：两侧都能转数字则数值比较；否则只在 `==` / `!=` 下做字符串
    精确比较（去首尾空白）；其余情况算不满足。空 `field` 表示该组不存在，按满足返回，
    由调用方用 `bool(field)` 决定是否计入 `or`。
    """
    if not field:
        return _GROUP_OK, ""
    raw = facts.get(field)
    if raw is None or not str(raw).strip():
        return _GROUP_MISSING, field
    cond = _to_number(raw)
    limit = _to_number(bound)
    if cond is not None and limit is not None:
        ok = bool(_OPS[op](cond, limit))
    elif op in ("==", "!="):
        expected = str(bound).strip()
        ok = bool(expected) and bool(_OPS[op](str(raw).strip(), expected))
    else:
        ok = False
    return (_GROUP_OK if ok else _GROUP_NO), ""


def judge(rule: RuleEntry, facts: dict[str, str]) -> RuleOutcome:
    """判定单条规则，返回四态之一——不适用与判不了分开表达。

    先看前提：`and` 任一组明确不满足即不适用，否则有缺失即判不了，再否则适用；
    `or` 任一组（非空组）满足即适用，否则有缺失即判不了，再否则不适用。
    适用后再看判定字段与阈值，缺值或不可解析都归 `UNKNOWN`，不归 `COMPLIANT`。
    """
    first_state, first_missing = _guard_state(rule.when_field, rule.when_op, rule.when_value, facts)
    second_state, second_missing = _guard_state(rule.when_field2, rule.when_op2, rule.when_value2, facts)
    conditions = (
        (first_state, bool(rule.when_field), first_missing),
        (second_state, bool(rule.when_field2), second_missing),
    )
    if rule.when_mode == "or":
        if not any(state == _GROUP_OK and defined for state, defined, _ in conditions):
            blocked = tuple(dict.fromkeys(name for state, _, name in conditions if state == _GROUP_MISSING))
            if blocked:
                return RuleOutcome(
                    rule, RuleStatus.UNKNOWN, None, blocked, "前提字段缺失: " + "、".join(blocked)
                )
            return RuleOutcome(rule, RuleStatus.NOT_APPLICABLE, None, (), "前提不满足")
    else:
        if any(state == _GROUP_NO for state, _, _ in conditions):
            return RuleOutcome(rule, RuleStatus.NOT_APPLICABLE, None, (), "前提不满足")
        blocked = tuple(dict.fromkeys(name for state, _, name in conditions if state == _GROUP_MISSING))
        if blocked:
            return RuleOutcome(rule, RuleStatus.UNKNOWN, None, blocked, "前提字段缺失: " + "、".join(blocked))

    raw = facts.get(rule.field)
    if not rule.field or raw is None or not str(raw).strip():
        return RuleOutcome(rule, RuleStatus.UNKNOWN, None, (rule.field,), "判定字段缺失: " + rule.field)
    value = _to_number(raw)
    if value is None:
        return RuleOutcome(rule, RuleStatus.UNKNOWN, None, (), "字段值无法解析")
    threshold = _to_number(rule.threshold)
    if threshold is None:
        return RuleOutcome(rule, RuleStatus.UNKNOWN, None, (), "阈值无法解析")
    if _OPS[rule.op](value, threshold):
        return RuleOutcome(rule, RuleStatus.COMPLIANT, value, (), "无违规")
    return RuleOutcome(rule, RuleStatus.VIOLATED, value, (), "命中违规")


def judge_all(rules: tuple[RuleEntry, ...], facts: dict[str, str]) -> tuple[RuleOutcome, ...]:
    """逐条判定并保留全部结果：长度恒等于规则数，顺序与入参一致。"""
    return tuple(judge(rule, facts) for rule in rules)
