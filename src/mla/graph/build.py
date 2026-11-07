"""状态图装配。

    START → intake → boundary ─否→ refuse → END
                         └是→ slots_check ─缺(且未超轮数)→ ask → slots_check
                                       └齐→ extract → retrieve → rule_check
                                            → checklist → verify
                                                ├通过→ output
                                                └失败→ degrade → output → END

节点用闭包绑定 `Deps`（实例不进 state），因此可以注入假实现做单测。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import PolicyConfig
from .nodes import (
    Deps,
    ask,
    boundary,
    checklist,
    degrade,
    extract,
    intake,
    output,
    qa_answer,
    refuse,
    retrieve,
    rule_check,
    slots_check,
    verify_node,
)
from .state import CheckState


def _bind(fn, deps: Deps):
    """把依赖闭包进节点，保持 LangGraph 要求的 state -> state 签名。"""

    def node(state: CheckState) -> CheckState:
        return fn(state, deps)

    node.__name__ = fn.__name__
    return node


def _route_boundary(state: CheckState) -> str:
    """越界 → 拒答；否则按模式分流（核对 / 问答）。"""
    if not state.get("in_scope"):
        return "refuse"
    return "qa" if state.get("mode") == "问答" else "check"


def _route_after_retrieve(state: CheckState) -> str:
    return "qa" if state.get("mode") == "问答" else "check"


def _route_slots(state: CheckState, max_ask_rounds: int) -> str:
    missing = state.get("missing_slots") or []
    rounds = int(state.get("ask_rounds") or 0)
    if missing and rounds < max_ask_rounds:
        return "ask"
    return "ready"


def _route_verify(state: CheckState) -> str:
    return "ok" if state.get("verify_ok") else "degrade"


def build_graph(deps: Deps, policy: PolicyConfig | None = None) -> Any:
    """构建并编译状态图。返回可直接 `.invoke()` / `.stream()` 的对象。"""
    max_ask = (policy or deps.policy).max_ask_rounds
    g = StateGraph(CheckState)

    g.add_node("intake", _bind(intake, deps))
    g.add_node("boundary", _bind(boundary, deps))
    g.add_node("slots_check", _bind(slots_check, deps))
    g.add_node("ask", _bind(ask, deps))
    g.add_node("extract", _bind(extract, deps))
    g.add_node("retrieve", _bind(retrieve, deps))
    g.add_node("rule_check", _bind(rule_check, deps))
    g.add_node("checklist", _bind(checklist, deps))
    g.add_node("qa_answer", _bind(qa_answer, deps))
    g.add_node("verify", _bind(verify_node, deps))
    g.add_node("refuse", _bind(refuse, deps))
    g.add_node("degrade", _bind(degrade, deps))
    g.add_node("output", _bind(output, deps))

    g.add_edge(START, "intake")
    g.add_edge("intake", "extract")
    g.add_edge("extract", "boundary")
    g.add_conditional_edges(
        "boundary", _route_boundary, {"refuse": "refuse", "qa": "retrieve", "check": "slots_check"}
    )
    g.add_conditional_edges(
        "slots_check", lambda s: _route_slots(s, max_ask), {"ask": "ask", "ready": "retrieve"}
    )
    g.add_edge("ask", "slots_check")
    g.add_conditional_edges("retrieve", _route_after_retrieve, {"qa": "qa_answer", "check": "rule_check"})
    g.add_edge("qa_answer", "verify")
    g.add_edge("rule_check", "checklist")
    g.add_edge("checklist", "verify")
    g.add_conditional_edges("verify", _route_verify, {"ok": "output", "degrade": "degrade"})
    g.add_edge("refuse", END)
    g.add_edge("degrade", "output")
    g.add_edge("output", END)

    return g.compile()
