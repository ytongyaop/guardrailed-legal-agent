"""受限问答的“智能体”循环：模型自己决定查什么、查几轮、何时停。

与固定管线（检索→作答）的区别：
- 工具只暴露“查法条 / 查规则”，模型通过 tool_calls 自主选择；
- 最终答案的 source_ids 必须落在工具返回集合内，否则丢弃；
- 工具失败/超轮数 → 回退固定检索作答，保证不比改造前更差。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.checklist import DISCLAIMER
from ..domain.models import RuleEntry, Suggestion
from ..ports.generation import Generator
from ..ports.retrieval import Retriever

MAX_ROUNDS = 3

SYSTEM = """你是劳动用工领域的受限智能体。你可以调用工具查询法条和规则，再作答。
规则：
1. 必须先调用工具；回答只能使用工具返回的条文/规则，不得凭记忆编造条号。
2. 每条回答带 source_ids，且只能取自工具返回的 source_id。
3. 工具返回不足时返回 {"answers":[]}，不要猜测。
4. 最终只输出 JSON：{"answers":[{"text":"结论","source_ids":["ID"]}]}，不要解释。"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_statutes",
            "description": "按关键词检索现行有效的劳动法条，返回条文原文与 source_id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，如“试用期 上限”"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_rules",
            "description": (
                "按关键词检索确定性规则表（试用期/竞业/加班/社保等），返回规则名称、阈值与对应法条 ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_rule",
            "description": "按 rule_id 精确查询一条规则。",
            "parameters": {
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
        },
    },
]


def _exec_tool(
    name: str,
    args: dict[str, Any],
    retriever: Retriever | None,
    rules: tuple[RuleEntry, ...],
) -> tuple[dict[str, Any], set[str]]:
    if name == "search_statutes":
        query = str(args.get("query", "")).strip()
        if not query or retriever is None:
            return {"results": []}, set()
        top_k = min(int(args.get("top_k") or 5), 8)
        hits = retriever.search(query, top_k=top_k, dense_query=query)
        rows = [
            {
                "source_id": s.statute_id,
                "law": s.law_name,
                "article": s.article_no,
                "text": (s.text or "")[:300],
            }
            for s in hits
        ]
        return {"results": rows}, {s.statute_id for s in hits}

    if name == "search_rules":
        query = str(args.get("query", "")).strip()
        matched = [r for r in rules if query and (query in r.name or query in r.field)][:5]
        rows = [
            {
                "rule_id": r.rule_id,
                "name": r.name,
                "limit": f"{r.op} {r.threshold}{r.unit}",
                "source_id": r.statute_id,
            }
            for r in matched
        ]
        return {"results": rows}, {r.statute_id for r in matched}

    if name == "lookup_rule":
        rule_id = str(args.get("rule_id", "")).strip()
        rule = next((r for r in rules if r.rule_id == rule_id), None)
        if rule is None:
            return {"error": "未找到该 rule_id"}, set()
        return (
            {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "limit": f"{rule.op} {rule.threshold}{rule.unit}",
                "source_id": rule.statute_id,
            },
            {rule.statute_id},
        )

    return {"error": f"未知工具 {name}"}, set()


def _parse_answers(content: str, allowed: set[str]) -> tuple[list[Suggestion], int]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return [], 0
    rows = payload.get("answers")
    if not isinstance(rows, list):
        return [], 0
    out: list[Suggestion] = []
    dropped = 0
    for row in rows[:5]:
        if not isinstance(row, dict):
            dropped += 1
            continue
        text = str(row.get("text", "")).strip()
        ids = [str(x) for x in row.get("source_ids", []) if str(x) in allowed]
        if not text or not ids:
            dropped += 1
            continue
        out.append(Suggestion(text=text[:300], source_ids=tuple(dict.fromkeys(ids)), disclaimer=DISCLAIMER))
    return out, dropped


def run_qa_agent(
    llm: Generator,
    retriever: Retriever | None,
    rules: tuple[RuleEntry, ...],
    question: str,
    max_rounds: int = MAX_ROUNDS,
) -> tuple[list[Suggestion], list[str], list[str]]:
    """返回（回答, 工具调用轨迹, 错误）。没有可用工具调用时返回空回答。"""
    if not hasattr(llm, "chat"):
        return [], [], ["当前模型不支持工具调用"]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    allowed: set[str] = set()
    trace: list[str] = []
    errors: list[str] = []

    for _ in range(max_rounds):
        try:
            message = llm.chat(messages, tools=TOOLS)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 模型失败不穿透，由上层回退固定检索作答
            return [], trace, errors + [f"模型调用失败：{type(exc).__name__}: {str(exc)[:120]}"]
        calls = message.get("tool_calls") or []
        if not calls:
            answers, dropped = _parse_answers(str(message.get("content") or ""), allowed)
            if dropped:
                errors.append(f"模型输出有 {dropped} 条回答因缺依据被丢弃")
            return answers, trace, errors
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result, ids = _exec_tool(name, args, retriever, rules)
            except Exception as exc:  # noqa: BLE001 - 检索层失败不穿透，处理方式见下
                # 记错误并回填一条工具错误消息，保持「assistant tool_calls 必有对应 tool 回复」的
                # 消息结构合法（否则下一轮 llm.chat 会被模型端拒绝）。模型下一轮可换关键词或换工具
                # 重试；真查不到证据时答案会因为 source 不在工具返回集合内被丢弃，不会凭空作答。
                errors.append(f"检索失败：{type(exc).__name__}: {str(exc)[:120]}")
                result, ids = {"error": "检索失败，未返回可用来源"}, set()
            allowed |= ids
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            label = args.get("query") or args.get("rule_id") or ""
            trace.append(f"{name}({label})" if label else name)

    # 轮数用完：不带工具要求收敛（仍只接受工具已返回的来源）
    try:
        final = llm.chat(  # type: ignore[attr-defined]
            messages, tools=None, response_format={"type": "json_object"}
        )
    except Exception as exc:  # noqa: BLE001 - 同上，异常不穿透调用方
        return [], trace, errors + [f"模型调用失败：{type(exc).__name__}: {str(exc)[:120]}"]
    answers, dropped = _parse_answers(str(final.get("content") or ""), allowed)
    if dropped:
        errors.append(f"模型输出有 {dropped} 条回答因缺依据被丢弃")
    if not answers:
        errors.append("达到工具调用轮数上限，仍未给出可核验回答")
    return answers, trace, errors
