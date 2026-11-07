"""生成式表述：把结构化事实交给模型写“处理建议/问答回答”，并做来源校验。

红线：
- 没有模型 / 模型失败 → 不生成，返回空 + 错误（由上层按“无依据不答”处理）；
- 每条生成文本必须带 `source_ids`，且只能取给定的 id；
- 不生成免责声明（由系统统一加），不编造条号。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.checklist import DISCLAIMER
from ..domain.models import CheckItem, StatuteEntry, Suggestion
from ..ports.generation import Generator

SUGGESTION_SYSTEM = """你是劳动合同合规顾问。只输出 JSON，不要解释。
格式：{"suggestions":[{"text":"给用人单位的具体整改动作","source_ids":["法条ID"]}]}
要求：
1. 每条建议必须引用给定 source_ids 中的 1-2 个；不得编造条号或法条。
2. 只针对给定风险条目给整改动作，写“改什么、怎么改”，不超过 120 字。
3. 不写免责声明，不写“建议咨询律师”。
4. 没有可引用依据时返回 {"suggestions":[]}。"""

ANSWER_SYSTEM = """你是劳动用工问答助手，只能依据给定条文回答。只输出 JSON，不要解释。
格式：{"answers":[{"text":"结论（可操作）","source_ids":["法条ID"]}]}
要求：
1. 每条结论必须引用给定 source_ids 中的 1-2 个；不得编造条文、条号或数字。
2. 条文不足以回答时返回 {"answers":[]}；不要猜测。
3. 不写免责声明。"""

MAX_ITEMS = 8
MAX_TEXT = 320


def _available(llm: Generator | None) -> bool:
    return llm is not None and bool(getattr(llm, "available", True))


def _sources_payload(statutes: list[StatuteEntry]) -> list[dict[str, str]]:
    return [
        {
            "source_id": s.statute_id,
            "law": s.law_name,
            "article": s.article_no,
            "text": (s.text or "")[:MAX_TEXT],
        }
        for s in statutes
    ]


def _to_suggestions(payload: dict[str, Any], allowed: set[str]) -> tuple[list[Suggestion], int]:
    raw = payload.get("suggestions")
    if not isinstance(raw, list):
        return [], 0
    out: list[Suggestion] = []
    dropped = 0
    seen: set[str] = set()
    for row in raw[:MAX_ITEMS]:
        if not isinstance(row, dict):
            dropped += 1
            continue
        text = str(row.get("text", "")).strip()
        ids = [str(x) for x in row.get("source_ids", []) if str(x) in allowed]
        key = text
        if not text or not ids or key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(Suggestion(text=text[:200], source_ids=tuple(dict.fromkeys(ids)), disclaimer=DISCLAIMER))
    return out, dropped


def draft_suggestions(
    llm: Generator | None,
    items: list[CheckItem],
    facts: dict[str, str],
    statutes: list[StatuteEntry],
) -> tuple[list[Suggestion], list[str]]:
    if not items:
        return [], []
    if not _available(llm):
        return [], ["未配置大模型：本次不生成处理建议（只保留规则核对结论）"]
    assert llm is not None
    allowed = {s.statute_id for s in statutes}
    payload = {
        "facts": facts,
        "risks": [
            {"rule_id": i.rule_id, "finding": i.finding, "source_id": i.source_id} for i in items[:MAX_ITEMS]
        ],
        "sources": _sources_payload(statutes),
    }
    try:
        result = llm.generate(SUGGESTION_SYSTEM, json.dumps(payload, ensure_ascii=False), {"type": "object"})
    except Exception as exc:  # noqa: BLE001 - 统一按“模型失败”降级
        return [], [f"大模型调用失败：{exc}"]
    suggestions, dropped = _to_suggestions(result, allowed)
    errors: list[str] = []
    if dropped:
        errors.append(f"模型输出有 {dropped} 条建议因缺依据/重复被丢弃")
    if not suggestions:
        errors.append("模型未给出可用建议（可能依据不足）")
    return suggestions, errors


def draft_answer(
    llm: Generator | None,
    question: str,
    statutes: list[StatuteEntry],
) -> tuple[list[Suggestion], list[str]]:
    if not _available(llm):
        return [], ["未配置大模型：按“无依据不答”处理"]
    assert llm is not None
    if not statutes:
        return [], ["未检索到可引用条文：按“无依据不答”处理"]
    allowed = {s.statute_id for s in statutes}
    payload = {"question": question, "sources": _sources_payload(statutes)}
    try:
        result = llm.generate(ANSWER_SYSTEM, json.dumps(payload, ensure_ascii=False), {"type": "object"})
    except Exception as exc:  # noqa: BLE001
        return [], [f"大模型调用失败：{exc}"]
    raw = result.get("answers")
    out: list[Suggestion] = []
    dropped = 0
    if isinstance(raw, list):
        for row in raw[:MAX_ITEMS]:
            if not isinstance(row, dict):
                dropped += 1
                continue
            text = str(row.get("text", "")).strip()
            ids = [str(x) for x in row.get("source_ids", []) if str(x) in allowed]
            if not text or not ids:
                dropped += 1
                continue
            out.append(
                Suggestion(text=text[:300], source_ids=tuple(dict.fromkeys(ids)), disclaimer=DISCLAIMER)
            )
    errors = [f"模型输出有 {dropped} 条回答因缺依据被丢弃"] if dropped else []
    if not out:
        errors.append("模型未能依据条文作答")
    return out, errors
