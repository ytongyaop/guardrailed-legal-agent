"""受限问答智能体测试：工具调用、来源校验、拒绝编造（不联网）。"""

from __future__ import annotations

import json
from typing import Any

from mla.domain.models import StatuteEntry
from mla.services.qa_agent import run_qa_agent


def _statute() -> StatuteEntry:
    return StatuteEntry(
        statute_id="LCL-19",
        law_name="中华人民共和国劳动合同法",
        article_no="第十九条",
        text="三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月。",
        issuing_body="全国人民代表大会常务委员会",
    )


class FakeRetriever:
    def search(self, query: str, top_k: int = 8, dense_query: str | None = None):
        return [_statute()]

    def health(self) -> bool:
        return True


class ScriptedLLM:
    available = True

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)
        self.tool_rounds = 0

    def chat(self, messages, tools=None, response_format=None):
        if tools:
            self.tool_rounds += 1
        return self.messages.pop(0)

    def generate(self, system: str, user: str, schema: dict | None = None):
        raise AssertionError("问答智能体不应调用 generate")


def test_agent_calls_tool_then_answers() -> None:
    llm = ScriptedLLM(
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "search_statutes", "arguments": '{"query":"试用期"}'},
                    }
                ]
            },
            {"content": json.dumps({"answers": [{"text": "最长 6 个月", "source_ids": ["LCL-19"]}]})},
        ]
    )
    answers, trace, errors = run_qa_agent(llm, FakeRetriever(), (), "试用期最长多久")
    assert answers and answers[0].source_ids == ("LCL-19",)
    assert trace and trace[0].startswith("search_statutes")
    assert not errors


def test_agent_rejects_unreturned_source() -> None:
    llm = ScriptedLLM(
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "search_statutes", "arguments": '{"query":"试用期"}'},
                    }
                ]
            },
            {"content": json.dumps({"answers": [{"text": "编的", "source_ids": ["BAD-1"]}]})},
        ]
    )
    answers, _, errors = run_qa_agent(llm, FakeRetriever(), (), "试用期最长多久")
    assert answers == []
    assert any("丢弃" in e for e in errors)


def test_agent_without_tool_calls_gets_no_grounding() -> None:
    llm = ScriptedLLM(
        [{"content": json.dumps({"answers": [{"text": "凭记忆回答", "source_ids": ["LCL-19"]}]})}]
    )
    answers, trace, _ = run_qa_agent(llm, FakeRetriever(), (), "试用期最长多久")
    assert answers == []  # 没有工具返回，source 不可校验
    assert trace == []
