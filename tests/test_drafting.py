"""生成层测试：有模型才生成、来源必须可校验、没有模型就拒答。"""

from __future__ import annotations

from typing import Any

from mla.domain.models import CheckItem, ItemKind, Severity, StatuteEntry
from mla.graph.nodes import Deps, checklist, qa_answer
from mla.services.drafting import draft_answer, draft_suggestions

RULES: tuple = ()


def _item(source_id: str = "LCL-19") -> CheckItem:
    return CheckItem(
        item_id="R-PROBATION-6M",
        kind=ItemKind.RISK,
        clause_ref="",
        finding="试用期超限",
        source_id=source_id,
        severity=Severity.HARD,
        rule_id="R-PROBATION-6M",
    )


def _statute() -> StatuteEntry:
    return StatuteEntry(
        statute_id="LCL-19",
        law_name="中华人民共和国劳动合同法",
        article_no="第十九条",
        text="三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月。",
        issuing_body="全国人民代表大会常务委员会",
    )


class FakeLLM:
    available = True

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, system: str, user: str, schema: dict | None = None) -> dict[str, Any]:
        self.calls += 1
        return self.payload

    def classify(self, text: str, labels: list[str]) -> str:
        return "不确定"


def test_suggestions_drop_unknown_or_empty_sources() -> None:
    llm = FakeLLM(
        {
            "suggestions": [
                {"text": "把试用期调到 6 个月内", "source_ids": ["LCL-19", "BAD-ID"]},
                {"text": "把试用期调到 6 个月内", "source_ids": ["LCL-19"]},  # 重复
                {"text": "", "source_ids": ["LCL-19"]},  # 空文本
            ]
        }
    )
    suggestions, errors = draft_suggestions(llm, [_item()], {}, [_statute()])
    assert len(suggestions) == 1
    assert suggestions[0].source_ids == ("LCL-19",)
    assert any("丢弃" in e for e in errors)


def test_no_model_does_not_generate_suggestions() -> None:
    suggestions, errors = draft_suggestions(None, [_item()], {}, [_statute()])
    assert suggestions == []
    assert any("未配置大模型" in e for e in errors)


def test_answer_requires_model_and_sources() -> None:
    answers, errors = draft_answer(None, "试用期多久", [_statute()])
    assert answers == [] and any("未配置大模型" in e for e in errors)

    llm = FakeLLM({"answers": [{"text": "最长 6 个月", "source_ids": ["LCL-19"]}]})
    answers, errors = draft_answer(llm, "试用期多久", [_statute()])
    assert len(answers) == 1 and answers[0].source_ids == ("LCL-19",)


def test_checklist_without_model_keeps_items_but_no_suggestions() -> None:
    out = checklist({"items": [_item()], "retrieved": [_statute()], "facts": {}}, Deps(rules=RULES))
    assert len(out["items"]) == 1
    assert out["suggestions"] == []
    assert any("未配置大模型" in e for e in out["errors"])


def test_qa_without_model_refuses_instead_of_answering() -> None:
    out = qa_answer(
        {"question": "试用期最长多久", "retrieved": [_statute()]},
        Deps(rules=RULES),
    )
    assert out["refused"] is True
    assert out["boundary_layer"] == "llm"
    assert out["suggestions"] == []
