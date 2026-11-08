"""受限问答智能体测试：工具调用、来源校验、拒绝编造（不联网）。"""

from __future__ import annotations

import json
from typing import Any

from mla.domain.models import StatuteEntry
from mla.graph.nodes import Deps, qa_answer
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


class RaisingLLM:
    """chat 直接抛错（模拟超时/网络失败/服务端 5xx），generate 正常。"""

    available = True

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.chat_calls = 0
        self.generate_calls = 0

    def chat(self, messages, tools=None, response_format=None):
        self.chat_calls += 1
        raise RuntimeError("simulated network failure")

    def generate(self, system: str, user: str, schema: dict | None = None):
        self.generate_calls += 1
        return self.payload

    def classify(self, text: str, labels: list[str]) -> str:
        return "不确定"


def test_agent_does_not_raise_when_model_fails() -> None:
    llm = RaisingLLM({"answers": [{"text": "最长 6 个月", "source_ids": ["LCL-19"]}]})
    answers, trace, errors = run_qa_agent(llm, FakeRetriever(), (), "试用期最长多久")
    assert answers == []  # 模型失败不自己造答案
    assert trace == []
    assert any("模型调用失败" in e for e in errors)
    assert any("RuntimeError" in e for e in errors)


class RoundsThenRaiseLLM:
    """每轮都要求调工具，轮数用尽后的收敛调用抛错。"""

    available = True

    def __init__(self) -> None:
        self.loop_calls = 0

    def chat(self, messages, tools=None, response_format=None):
        if tools:
            self.loop_calls += 1
            return {
                "tool_calls": [
                    {
                        "id": f"c{self.loop_calls}",
                        "function": {"name": "search_statutes", "arguments": '{"query":"试用期"}'},
                    }
                ]
            }
        raise RuntimeError("simulated network failure")

    def generate(self, system: str, user: str, schema: dict | None = None):
        raise AssertionError("收敛阶段失败不应再调用 generate")

    def classify(self, text: str, labels: list[str]) -> str:
        return "不确定"


def test_agent_does_not_raise_when_final_call_fails() -> None:
    llm = RoundsThenRaiseLLM()
    answers, trace, errors = run_qa_agent(llm, FakeRetriever(), (), "试用期最长多久")
    assert answers == []
    assert len(trace) == 3  # 三轮工具调用都留痕
    assert any("模型调用失败" in e for e in errors)


class RaisingRetriever:
    """检索层故障（模拟向量库/BM25 抛错）：查询本身不失败，失败在检索内部。"""

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None):
        raise RuntimeError("simulated retrieval failure")

    def health(self) -> bool:
        return True


def test_agent_does_not_raise_when_retrieval_fails() -> None:
    """P0-3 顺带：检索层抛错不穿透，记错误后流程继续（模型可换词重试或收敛）。"""
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
            {"content": json.dumps({"answers": []})},
        ]
    )
    answers, trace, errors = run_qa_agent(llm, RaisingRetriever(), (), "试用期最长多久")

    assert answers == [], "检索失败不能凭空作答"
    assert trace == ["search_statutes(试用期)"], "失败的调用仍应留痕"
    assert any("检索失败" in e and "RuntimeError" in e for e in errors)


def test_qa_node_falls_back_to_draft_answer_when_agent_fails() -> None:
    llm = RaisingLLM({"answers": [{"text": "最长 6 个月", "source_ids": ["LCL-19"]}]})
    out = qa_answer(
        {"question": "试用期最长多久", "retrieved": [_statute()]},
        Deps(rules=(), retriever=FakeRetriever(), generator=llm),
    )
    suggestions = out.get("suggestions") or []
    assert suggestions  # 回退固定检索作答后仍有结果
    assert suggestions[0].source_ids == ("LCL-19",)
    assert out.get("agent_trace") == ["fallback:draft_answer"]
    assert any("模型调用失败" in e for e in out.get("errors") or [])
    assert llm.generate_calls == 1
