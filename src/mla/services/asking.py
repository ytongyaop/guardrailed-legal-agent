"""模型辅助追问：润色问句、挑可补充字段。

模型只决定"怎么说"和"先问哪个"，不决定"问什么"：字段集合由确定性槽位表给出，
模型给出的字段一律先过闭集核验。两条能力：

- `polish_questions`：把阻断项的固定问句改写成更好回答的说法，阻断项必须全覆盖；
- `suggest_optional`：从可补充字段池里挑最多 3 项，挑不出就取候选前 3 项。

隐私口径：只发送已抽取的结构化事实与字段说明，**不发送合同正文**。
失败口径：超时、异常、非 JSON、结构不对一律回退固定问句并记一句诊断，永不向上抛异常。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..domain.field_catalog import field_hint, field_name, field_question
from ..domain.policy import MAX_ASK_ITEMS
from ..ports.generation import Generator

ASK_SYSTEM = """你是劳动合同合规助手的提问编辑。只输出 JSON，不要解释，不要 Markdown。

任务：把给定的每个待补事实字段改写成用户能直接回答的问句。只决定怎么问，不决定问什么。

硬性要求：
1. 只能使用给定的 slot_id，不得新增字段，不得遗漏，不得改变字段含义。
2. 每句 12 到 60 字，单行，以问号结尾。
3. 不写任何法条编号，不出现条号表述，不引用法规名称。
4. 不出现"违法""无效""应当赔偿""构成违约""必须支付"这类结论性说法。
5. 不出现新数字：需要举例时沿用在给定事实里出现过的数值，不得自行编造年限、月数、倍数。
6. 语气是向用人单位了解情况，不用敬称，不用命令句。

输出格式：
{"asks":[{"slot_id":"字段 id","question":"问句","why":"为什么需要这项事实（不超过 20 字）"}]}
"""

FIELDS_SYSTEM = """你是劳动合同合规助手的提问编辑。只输出 JSON，不要解释，不要 Markdown。

任务：从给定的候选字段里挑出最值得先问的几项，最多 __MAX_ITEMS__ 项。

选择口径：优先能影响更多规则判定的字段；同等影响时优先只需要一个数字或一次选择的字段。

硬性要求：
1. 只能从 candidates 的 slot_id 里挑，不得新增字段。
2. 不重复，最多 __MAX_ITEMS__ 项；没有把握就不挑。
3. 不写理由，只输出字段 id。

输出格式：
{"fields":["字段 id"]}
"""

# 问句形态：12 至 60 字、单行、以问号结尾
MIN_QUESTION = 12
MAX_QUESTION = 60
# 问句里不允许出现的法条编号形态与结论性说法
_ARTICLE_RE = re.compile(r"第\s*[0-9０-９一二三四五六七八九十百千零〇两]+\s*条")
_STATUTE_ID_RE = re.compile(r"[A-Za-z]{2,4}-\d+")
_BANNED = ("违法", "无效", "应当赔偿", "构成违约", "必须支付")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

_UNUSABLE = "追问模型输出不可用，已回退固定问句"
_FIELDS_UNUSABLE = "字段选择模型输出不可用，已回退候选前 3 项"


def _available(llm: Generator | None) -> bool:
    """与生成层同口径：没有实例或实例自报不可用都算没有模型。"""
    return llm is not None and bool(getattr(llm, "available", True))


def _fixed_asks(slot_ids: Sequence[str]) -> list[dict[str, str]]:
    """固定问句版追问清单（模型缺席或输出不可用时的唯一出口）。"""
    return [
        {"slot_id": slot_id, "question": field_question(slot_id), "source": "fixed"} for slot_id in slot_ids
    ]


def _numbers(text: str) -> set[str]:
    """抽出文本里的数值并按数值归一（3 与 3.0 视为同一个），供防编造比对。"""
    out: set[str] = set()
    for token in _NUMBER_RE.findall(text or ""):
        out.add(str(float(token)).rstrip("0").rstrip(".") if "." in token else str(int(token)))
    return out


def _facts_numbers(facts: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for value in facts.values():
        out |= _numbers(str(value))
    return out


def _question_ok(question: str, facts_numbers: set[str], fixed_numbers: set[str]) -> tuple[bool, str]:
    """单句核验：形态、法条编号、结论性说法、数值一致性。返回（是否可用, 不可用原因）。

    数值口径：问句里的数字必须来自已填事实，或来自该字段的固定问句（模型会沿用“填 0”
    这类固定说法）——两者都没有才算编造，避免把沿用固定问句的润色问句误判成新数字。
    """
    if not question:
        return False, "问句为空"
    if "\n" in question or "\r" in question:
        return False, "问句含换行"
    if not MIN_QUESTION <= len(question) <= MAX_QUESTION:
        return False, f"问句长度不在 {MIN_QUESTION} 至 {MAX_QUESTION} 字之间"
    if question[-1] not in "？?":
        return False, "问句未以问号结尾"
    if _ARTICLE_RE.search(question) or _STATUTE_ID_RE.search(question):
        return False, "问句引用了法条编号"
    hit = next((word for word in _BANNED if word in question), "")
    if hit:
        return False, f"问句含结论性说法 {hit}"
    invented = sorted(_numbers(question) - facts_numbers - fixed_numbers)
    if invented:
        return False, "问句数值与已填事实不一致 " + "/".join(invented)
    return True, ""


def _ask_payload(facts: dict[str, str], slot_ids: Sequence[str], ask_round: int) -> dict[str, Any]:
    """追问 prompt 的输入：已抽事实 + 待补字段说明 + 轮次。不含合同正文。"""
    return {
        "round": max(0, int(ask_round)),
        "facts": [
            {"field": field_name(key), "value": str(value)}
            for key, value in facts.items()
            if str(value).strip()
        ],
        "fields": [
            {
                "slot_id": slot_id,
                "name": field_name(slot_id),
                "hint": field_hint(slot_id),
                "fixed_question": field_question(slot_id),
            }
            for slot_id in slot_ids
        ],
    }


def _verify_asks(
    raw: dict[str, Any],
    allowed_slot_ids: set[str],
    missing_blocking: Sequence[str],
    facts: dict[str, str],
) -> tuple[list[dict[str, str]], list[str]]:
    """追问的程序核验：字段闭集、条数上限、阻断项全覆盖、问句形态与数值一致性。"""
    errors: list[str] = []
    rows = raw.get("asks")
    if not isinstance(rows, list):
        return _fixed_asks(missing_blocking), [_UNUSABLE]
    facts_numbers = _facts_numbers(facts)
    seen: set[str] = set()
    kept: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append("模型输出有 1 条追问不是对象，已丢弃")
            continue
        slot_id = str(row.get("slot_id", "") or "").strip()
        if slot_id not in allowed_slot_ids:
            errors.append(f"模型给出的字段 {slot_id or '空'} 不在待补事实范围内，已丢弃")
            continue
        if slot_id in seen:
            errors.append(f"{field_name(slot_id)} 的追问重复出现，只保留第一条")
            continue
        seen.add(slot_id)
        kept.append(
            {
                "slot_id": slot_id,
                "question": str(row.get("question", "") or "").strip(),
                "source": "llm",
            }
        )
    if len(kept) > MAX_ASK_ITEMS:
        errors.append(f"模型给出 {len(kept)} 条追问，超出上限 {MAX_ASK_ITEMS} 条，已按序截断")
        kept = kept[:MAX_ASK_ITEMS]
    by_slot = {item["slot_id"]: item for item in kept}
    out: list[dict[str, str]] = []
    for slot_id in missing_blocking:
        item = by_slot.pop(slot_id, None)
        if item is None:
            errors.append(f"模型漏问 {field_name(slot_id)}，已用固定问句补齐")
            out.append({"slot_id": slot_id, "question": field_question(slot_id), "source": "fixed"})
            continue
        ok, why = _question_ok(item["question"], facts_numbers, _numbers(field_question(slot_id)))
        if not ok:
            errors.append(f"{field_name(slot_id)}：{why}，已改回固定问句")
            item = {"slot_id": slot_id, "question": field_question(slot_id), "source": "fixed"}
        out.append(item)
    return out, errors


def polish_questions(
    llm: Generator | None,
    facts: dict[str, str],
    slot_ids: Sequence[str],
    *,
    ask_round: int = 0,
) -> tuple[list[dict[str, str]], list[str]]:
    """把阻断项的固定问句交给模型润色，返回（追问清单, 诊断）。

    没有模型时整份用固定问句、`source="fixed"`、不产生诊断；模型输出有任何问题
    （超时、异常、非 JSON、字段越界、问句不合规）都按条回退或整份回退，不向上抛异常。
    """
    ordered = [str(slot_id) for slot_id in slot_ids]
    if not ordered:
        return [], []
    fixed = _fixed_asks(ordered)
    try:
        if not _available(llm):
            return fixed, []
        assert llm is not None
        payload = json.dumps(_ask_payload(facts, ordered, ask_round), ensure_ascii=False)
        raw = llm.generate(ASK_SYSTEM, payload, {"type": "object"})
        if not isinstance(raw, dict):
            return fixed, [_UNUSABLE]
        return _verify_asks(raw, set(ordered), ordered, facts)
    except Exception as exc:  # 模型超时 / 结构异常统一降级，追问不能因此中断核对
        return fixed, [f"{_UNUSABLE}（{exc}）"]


def _fields_payload(facts: dict[str, str], candidates: Sequence[str]) -> dict[str, Any]:
    """字段选择 prompt 的输入：已抽事实 + 候选字段说明。不含合同正文。"""
    return {
        "facts": [
            {"field": field_name(key), "value": str(value)}
            for key, value in facts.items()
            if str(value).strip()
        ],
        "candidates": [
            {
                "slot_id": field_id,
                "name": field_name(field_id),
                "hint": field_hint(field_id),
                "question": field_question(field_id),
            }
            for field_id in candidates
        ],
    }


def _verify_fields(raw: dict[str, Any], allowed: Sequence[str]) -> tuple[list[str], list[str]]:
    """字段核验：闭集、去重、条数上限；挑不出有效字段就退回候选前 3 项。"""
    fallback = list(allowed[:MAX_ASK_ITEMS])
    rows = raw.get("fields")
    if not isinstance(rows, list):
        return fallback, [_FIELDS_UNUSABLE]
    allowed_set = set(allowed)
    errors: list[str] = []
    picked: list[str] = []
    outside = 0
    for row in rows:
        field_id = str(row).strip()
        if field_id not in allowed_set:
            outside += 1
            continue
        if field_id not in picked:
            picked.append(field_id)
    if outside:
        errors.append(f"模型给出 {outside} 个候选范围外的字段，已丢弃")
    if len(picked) > MAX_ASK_ITEMS:
        errors.append(f"模型给出 {len(picked)} 个字段，超出上限 {MAX_ASK_ITEMS} 个，已按序截断")
        picked = picked[:MAX_ASK_ITEMS]
    if not picked:
        errors.append(_FIELDS_UNUSABLE)
        return fallback, errors
    return picked, errors


def suggest_optional(
    llm: Generator | None,
    facts: dict[str, str],
    candidates: Sequence[str],
) -> tuple[list[str], list[str]]:
    """从可补充字段里挑最多 3 项，返回（字段 id, 诊断）。

    没有模型时按候选顺序取前 3 项且不产生诊断；模型输出不可用同样退回前 3 项，
    并记一句诊断。候选为空时返回空清单。
    """
    ordered = [str(field_id) for field_id in candidates]
    if not ordered:
        return [], []
    fallback = ordered[:MAX_ASK_ITEMS]
    try:
        if not _available(llm):
            return fallback, []
        assert llm is not None
        payload = json.dumps(_fields_payload(facts, ordered), ensure_ascii=False)
        system = FIELDS_SYSTEM.replace("__MAX_ITEMS__", str(MAX_ASK_ITEMS))
        raw = llm.generate(system, payload, {"type": "object"})
        if not isinstance(raw, dict):
            return fallback, [_FIELDS_UNUSABLE]
        selected, errors = _verify_fields(raw, ordered)
        return selected, errors
    except Exception as exc:  # 同一口径：字段选择失败不阻塞结果页
        return fallback, [f"{_FIELDS_UNUSABLE}（{exc}）"]
