"""状态图测试：路径、补问回流、条件边、产出与校验（注入真实规则表，不联网）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mla.config import data_path
from mla.domain.models import CheckItem, ItemKind, RuleCoverage, UndeterminedRule
from mla.domain.rules import evaluate, load_rules
from mla.graph.build import build_graph
from mla.graph.nodes import Deps, rule_check
from mla.graph.state import CheckState

RULES = load_rules(Path(data_path("rules/rules.yaml")))
DEPS = Deps(rules=RULES)

# 阻断项齐全（主体类型 / 是否书面合同 / 合同期限），用于「不进入补问」的用例
BASE_FACTS = {
    "employer_type": "有限责任公司",
    "written_contract": "是",
    "term_years": "3",
}


def _invoke(facts: dict[str, str]) -> dict[str, Any]:
    graph = build_graph(Deps(rules=RULES))
    init: CheckState = {"task_id": "T-test", "mode": "核对", "facts": facts}
    return dict(graph.invoke(init))


def _invoke_traced(facts: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    graph = build_graph(Deps(rules=RULES))
    init: CheckState = {"task_id": "T-test", "mode": "核对", "facts": facts}
    final: dict[str, Any] = dict(init)
    order: list[str] = []
    for chunk in graph.stream(init, stream_mode="updates"):
        for node, delta in chunk.items():
            order.append(node)
            if isinstance(delta, dict):
                final.update(delta)
    return final, order


def test_violation_produces_traceable_item() -> None:
    final = _invoke({**BASE_FACTS, "probation_months": "12"})
    items = final["items"]
    assert len(items) == 1
    assert items[0].source_id == "LCL-19"
    assert final["verify_ok"] is True


def test_compliant_produces_no_item() -> None:
    final = _invoke({**BASE_FACTS, "probation_months": "3"})
    assert final["items"] == []
    assert final["verify_ok"] is True
    assert not final.get("ask_rounds")


def test_node_order_is_stable() -> None:
    _, order = _invoke_traced({**BASE_FACTS, "probation_months": "12"})
    assert order == [
        "intake",
        "extract",
        "boundary",
        "slots_check",
        "retrieve",
        "rule_check",
        "checklist",
        "verify",
        "output",
    ]


def test_qa_route_unchanged_with_context() -> None:
    """问答带上下文时路由与节点数不变：retrieve → qa_answer → verify → output。"""
    graph = build_graph(Deps(rules=RULES))
    init: CheckState = {
        "task_id": "T-qa",
        "mode": "问答",
        "question": "试用期最长可以约定多久？",
        "qa_context": {
            "facts": {"term_years": "3"},
            "items": [{"rule_id": "R-PROBATION-6M", "statute_id": "LCL-19"}],
            "clauses": [{"paragraph": 3, "grade": "A", "trigger_id": "TR-PROBATION-WAGE-BASE"}],
        },
    }
    order = [node for chunk in graph.stream(init, stream_mode="updates") for node in chunk]
    assert order == [
        "intake",
        "extract",
        "boundary",
        "retrieve",
        "qa_answer",
        "verify",
        "output",
    ]


def test_missing_blocking_facts_triggers_ask_loop() -> None:
    """缺阻断项 → 追问 → 回流，直到达到轮数上限后降级继续。"""
    final, order = _invoke_traced({"term_years": "3"})
    assert order.count("ask") == 3  # max_ask_rounds = 3
    assert order[:4] == ["intake", "extract", "boundary", "slots_check"]
    assert final["degraded"] is True
    assert any("仍缺" in e for e in final["errors"])


def test_ask_history_is_recorded() -> None:
    final = _invoke({})
    history = final["ask_history"]
    assert len(history) == 3
    assert all(item["questions"] for item in history)  # 每轮都有具体追问句


# ---------- 三态判定的接入 ----------
def _facts_cases() -> list[dict[str, str]]:
    """评测集里全部带**非空**结构化事实的用例（问答类没有 facts，跳过）。"""
    out: list[dict[str, str]] = []
    for name in ("eval_cases.jsonl", "eval_generalization.jsonl"):
        path = Path(data_path("evalsets")) / name
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            facts = json.loads(line).get("facts") or {}
            if facts:
                out.append(facts)
    return out


def _item_key(item: CheckItem) -> tuple[str, str, str, str, str, str]:
    """比对用指纹：条目六要素 + 顺序位置（顺序由列表本身保证）。"""
    return (
        item.item_id,
        str(item.kind),
        item.finding,
        item.source_id,
        str(item.severity),
        item.rule_id,
    )


def _expected_items(facts: dict[str, str]) -> list[CheckItem]:
    """按旧口径重建期望条目：evaluate 的违规集合逐条映射。"""
    return [
        CheckItem(
            item_id=h.rule.rule_id,
            kind=ItemKind.RISK,
            clause_ref="",
            finding=f"{h.rule.name}（实际 {h.value:g}{h.rule.unit}，限值 {h.rule.threshold}{h.rule.unit}）",
            source_id=h.rule.statute_id,
            severity=h.rule.severity,
            rule_id=h.rule.rule_id,
        )
        for h in evaluate(RULES, facts)
        if h.violated
    ]


def _rule_layer(
    facts: dict[str, str], variant: str = "full"
) -> tuple[list[CheckItem], list[UndeterminedRule], RuleCoverage | None]:
    """直接调 `rule_check`，用 `.get` 取三个非必选键（不触发非必选键的静态告警）。"""
    out = rule_check({"facts": facts, "variant": variant}, DEPS)
    return (
        list(out.get("items") or []),
        list(out.get("undetermined") or []),
        out.get("rule_coverage"),
    )


def test_items_are_identical_to_legacy_filter() -> None:
    """接入旁路留痕后 items 必须一字不变：逐例逐条比对六要素，顺序也一致。"""
    cases = _facts_cases()
    assert len(cases) >= 50, "带事实的评测用例数变了"
    produced = 0
    for facts in cases:
        items, _, _ = _rule_layer(facts)
        assert [_item_key(i) for i in items] == [_item_key(i) for i in _expected_items(facts)], facts
        produced += len(items)
    assert produced > 0, "没有任何用例命中规则，这条测试等于没跑"


def test_undetermined_separates_field_from_premise() -> None:
    """两类"判不了"必须分开：前提已满足缺判定字段 vs 前提字段就缺。"""
    _, undetermined, _ = _rule_layer({"work_years": "12"})
    by_id = {u.rule_id: u for u in undetermined}

    judged_field = by_id["R-AL-10D"]  # 满 10 年不满 20 年：前提满足，缺年休假天数
    assert judged_field.reason == "field_missing"
    assert judged_field.missing_fields == ("annual_leave_days",)
    assert judged_field.statute_id == "AL-3"

    premise = by_id["R-PROBATION-1M"]  # 连合同期限都没给：是否适用都不确定
    assert premise.reason == "premise_missing"
    assert premise.missing_fields == ("term_years",)


def test_undetermined_never_enters_items() -> None:
    items, undetermined, _ = _rule_layer({"work_years": "12"})
    assert undetermined, "这组事实本应判不了"
    assert items == []
    assert {u.rule_id for u in undetermined} & {i.rule_id for i in items} == set()


def test_llm_only_returns_zero_coverage() -> None:
    """llm_only 档整层不跑：条目与未判定都为空，分布是全零而不是 None。"""
    items, undetermined, coverage = _rule_layer(
        {"term_years": "3", "probation_months": "12"}, variant="llm_only"
    )
    assert items == []
    assert undetermined == []
    assert coverage is not None and coverage.total == 0


def test_rule_coverage_covers_every_rule() -> None:
    items, undetermined, coverage = _rule_layer({**BASE_FACTS, "probation_months": "12"})
    assert coverage is not None
    assert coverage.total == len(RULES)
    assert (
        coverage.violated + coverage.compliant + coverage.not_applicable + coverage.unknown == coverage.total
    )
    assert coverage.violated == len(items)
    assert coverage.unknown == len(undetermined)


def test_graph_carries_coverage_and_undetermined() -> None:
    """状态图必须把两个新键带出来（否则落库拿不到）。"""
    final = _invoke({**BASE_FACTS, "work_years": "12"})
    coverage = final.get("rule_coverage")
    assert coverage is not None and coverage.total == len(RULES)
    assert final["undetermined"]
    assert {u.reason for u in final["undetermined"]} <= {"field_missing", "premise_missing"}
