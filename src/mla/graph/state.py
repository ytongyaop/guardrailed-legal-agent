"""状态图共享状态。节点返回"增量"，由 LangGraph 合并（不要原地改）。"""

from __future__ import annotations

from typing import TypedDict

from ..domain.models import CaseRef, CheckItem, MissingTerm, RuleCoverage, Suggestion, UndeterminedRule


class CheckState(TypedDict, total=False):
    # 输入
    task_id: str
    mode: str  # "核对" / "问答"
    contract_text: str
    question: str
    facts: dict[str, str]
    variant: str  # llm_only / llm_rules / full
    qa_context: dict  # 问答附带的核对摘要（只放结构化结论，不放合同原文）
    # 过程
    in_scope: bool
    boundary_layer: str  # classify / retrieval / whitelist（拒答归因，指标 6/7）
    boundary_reason: str  # 判定理由（写进拒答说明）
    missing_slots: list[str]
    ask_rounds: int
    questions: list[str]  # 本轮追问句
    ask_items: list[dict]  # 本轮追问的结构化形态（slot_id + 问句 + 来源）
    ask_history: list[dict]  # 每轮追问留痕（指标 4/5 证据）
    extracted: dict
    retrieved: list  # list[StatuteEntry]
    rule_hits: list  # list[RuleHit]
    undetermined: list[UndeterminedRule]  # 判不了的规则（缺字段）
    rule_coverage: RuleCoverage | None  # 规则层四态分布
    missing_terms: list[MissingTerm]  # 必备条款缺项（旁路，不进 items）
    # 产出
    items: list[CheckItem]
    suggestions: list[Suggestion]
    case_refs: list[CaseRef]
    agent_trace: list[str]
    verify_ok: bool
    degraded: bool
    refused: bool
    errors: list[str]
