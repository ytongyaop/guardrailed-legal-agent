"""模型辅助追问：问句润色、字段挑选、程序核验与回退（全部离线，用假模型）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mla.adapters.persist import SqliteRepository
from mla.config import Paths
from mla.domain.asking import candidate_fields
from mla.domain.field_catalog import field_question
from mla.domain.models import CheckTask, TaskMode
from mla.domain.rules import load_rules
from mla.domain.slots import BLOCKING_SLOTS, derive_slots
from mla.graph.nodes import Deps, ask
from mla.graph.state import CheckState
from mla.services.asking import _verify_asks, polish_questions, suggest_optional

RULES = load_rules(Path("data/rules/rules.yaml"))
DEPS = Deps(rules=RULES)

BLOCKING = list(BLOCKING_SLOTS)  # employer_type / written_contract / term_years


class FakeLLM:
    """最小生成端口替身：只回放预设结构，记录每次调用的 prompt。"""

    def __init__(self, payload: Any = None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.available = True
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.payload

    def classify(self, text: str, labels: list[str]) -> str:
        return "不确定"


def _by_slot(asks: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {item["slot_id"]: item for item in asks}


# ---------- polish_questions ----------
def test_no_model_returns_fixed_questions() -> None:
    asks, errors = polish_questions(None, {"extra": "1"}, BLOCKING)
    assert [item["slot_id"] for item in asks] == BLOCKING
    assert all(item["source"] == "fixed" for item in asks)
    assert all(item["question"] == field_question(item["slot_id"]) for item in asks)
    assert errors == []


def test_model_invented_field_is_dropped_and_blocking_backfilled() -> None:
    llm = FakeLLM(
        {
            "asks": [
                {"slot_id": "salary", "question": "员工每个月的工资大概是多少钱呢？"},
                {"slot_id": "term_years", "question": "这份合同约定的期限是几年？"},
            ]
        }
    )
    asks, errors = polish_questions(llm, {}, BLOCKING)
    assert [item["slot_id"] for item in asks] == BLOCKING
    assert any("不在待补事实范围内" in error for error in errors)
    by_slot = _by_slot(asks)
    assert by_slot["term_years"]["source"] == "llm"
    assert by_slot["employer_type"]["source"] == "fixed"
    assert by_slot["written_contract"]["source"] == "fixed"
    assert any("补" in error for error in errors)


def test_model_omitting_blocking_item_still_covers_all() -> None:
    llm = FakeLLM(
        {
            "asks": [
                {"slot_id": "employer_type", "question": "您目前登记的用工主体是哪一种类型？"},
                {"slot_id": "term_years", "question": "这份劳动合同约定了多长的期限？"},
            ]
        }
    )
    asks, errors = polish_questions(llm, {}, BLOCKING)
    assert len(asks) == len(BLOCKING)
    assert [item["slot_id"] for item in asks] == BLOCKING
    assert _by_slot(asks)["written_contract"]["question"] == field_question("written_contract")
    assert any("漏问" in error for error in errors)


def test_unusable_model_output_falls_back_without_raising() -> None:
    broken = FakeLLM({"asks": "不是列表"})
    asks, errors = polish_questions(broken, {}, BLOCKING)
    assert len(asks) == len(BLOCKING)
    assert all(item["source"] == "fixed" for item in asks)
    assert errors and "回退固定问句" in errors[0]

    timeout = FakeLLM(error=TimeoutError("超时"))
    asks2, errors2 = polish_questions(timeout, {}, BLOCKING)
    assert [item["question"] for item in asks2] == [field_question(s) for s in BLOCKING]
    assert errors2 and "超时" in errors2[0]

    empty = FakeLLM(None)
    asks3, errors3 = polish_questions(empty, {}, BLOCKING)
    assert all(item["source"] == "fixed" for item in asks3)
    assert errors3


def test_question_with_article_number_is_reverted() -> None:
    llm = FakeLLM(
        {
            "asks": [
                {"slot_id": "employer_type", "question": "按第八十二条，您的经营主体属于哪一种类型？"},
            ]
        }
    )
    asks, errors = polish_questions(llm, {}, BLOCKING)
    item = _by_slot(asks)["employer_type"]
    assert item["source"] == "fixed"
    assert item["question"] == field_question("employer_type")
    assert any("法条编号" in error for error in errors)


def test_question_number_conflicting_with_facts_is_reverted() -> None:
    llm = FakeLLM({"asks": [{"slot_id": "term_years", "question": "这份合同约定的期限是 5 年吗？"}]})
    asks, errors = polish_questions(llm, {"term_years": "3"}, BLOCKING)
    item = _by_slot(asks)["term_years"]
    assert item["source"] == "fixed"
    assert item["question"] == field_question("term_years")
    assert any("数值" in error for error in errors)

    ok = FakeLLM({"asks": [{"slot_id": "term_years", "question": "这份合同约定的期限是 3 年吗？"}]})
    asks_ok, _ = polish_questions(ok, {"term_years": "3"}, BLOCKING)
    assert _by_slot(asks_ok)["term_years"]["source"] == "llm"


def test_question_reusing_zero_from_fixed_question_is_kept() -> None:
    """模型沿用固定问句里的“填 0”不算编造：固定问句出现过的数字一律放行。"""
    llm = FakeLLM({"asks": [{"slot_id": "term_years", "question": "合同期限是几年，无固定期限是否填 0？"}]})
    asks, errors = polish_questions(llm, {"term_years": "3"}, BLOCKING)
    item = _by_slot(asks)["term_years"]
    assert item["source"] == "llm"
    assert item["question"] == "合同期限是几年，无固定期限是否填 0？"
    assert not any("数值" in error for error in errors)  # 不是数值回退


def test_question_with_random_number_not_in_facts_or_fixed_is_reverted() -> None:
    """既不在已填事实、也不在固定问句里的随机数字仍然回退（防编造口径不放宽）。"""
    llm = FakeLLM({"asks": [{"slot_id": "term_years", "question": "合同期限是 7 年还是 9 年？"}]})
    asks, errors = polish_questions(llm, {"term_years": "3"}, BLOCKING)
    item = _by_slot(asks)["term_years"]
    assert item["source"] == "fixed"
    assert item["question"] == field_question("term_years")
    assert any("数值" in error for error in errors)


def test_duplicate_slot_keeps_first_only() -> None:
    llm = FakeLLM(
        {
            "asks": [
                {"slot_id": "term_years", "question": "这份合同约定的期限是几年呢？"},
                {"slot_id": "term_years", "question": "合同期限一共定了几年时长？"},
            ]
        }
    )
    asks, errors = polish_questions(llm, {}, BLOCKING)
    assert len(asks) == len(BLOCKING)
    assert len({item["slot_id"] for item in asks}) == len(asks)
    assert any("重复" in error for error in errors)


def test_prompt_carries_facts_and_fields_but_no_contract() -> None:
    llm = FakeLLM({"asks": []})
    polish_questions(llm, {"term_years": "3"}, ["term_years"], ask_round=2)
    payload = json.loads(llm.calls[0][1])
    assert set(payload) == {"round", "facts", "fields"}
    assert payload["round"] == 2
    assert payload["facts"][0]["value"] == "3"
    assert payload["fields"][0]["slot_id"] == "term_years"


# ---------- suggest_optional ----------
def test_suggest_optional_without_model_takes_first_three() -> None:
    candidates = ["annual_leave_days", "ot_hours_month", "work_years", "probation_months"]
    selected, errors = suggest_optional(None, {}, candidates)
    assert selected == candidates[:3]
    assert errors == []


def test_suggest_optional_drops_fields_outside_candidates() -> None:
    candidates = ["annual_leave_days", "ot_hours_month", "work_years"]
    llm = FakeLLM({"fields": ["annual_leave_days", "salary", "annual_leave_days"]})
    selected, errors = suggest_optional(llm, {}, candidates)
    assert selected == ["annual_leave_days"]
    assert any("候选范围外" in error for error in errors)


def test_suggest_optional_falls_back_when_nothing_valid() -> None:
    candidates = ["annual_leave_days", "ot_hours_month", "work_years", "probation_months"]
    llm = FakeLLM({"fields": ["没这个字段"]})
    selected, errors = suggest_optional(llm, {}, candidates)
    assert selected == candidates[:3]
    assert errors

    broken = FakeLLM(error=RuntimeError("网络中断"))
    selected2, errors2 = suggest_optional(broken, {}, candidates)
    assert selected2 == candidates[:3]
    assert errors2 and "网络中断" in errors2[0]


def test_suggest_optional_caps_at_three() -> None:
    candidates = ["a1", "a2", "a3", "a4", "a5"]
    llm = FakeLLM({"fields": list(candidates)})
    selected, errors = suggest_optional(llm, {}, candidates)
    assert selected == candidates[:3]
    assert any("超出上限" in error for error in errors)


# ---------- candidate_fields ----------
def test_candidate_fields_excludes_blocking_and_sorts_by_rule_count() -> None:
    slots = derive_slots(RULES, {})
    picked = candidate_fields(RULES, {})
    assert picked
    assert not set(picked) & set(BLOCKING_SLOTS)
    assert len(picked) <= 8
    counts = {slot.slot_id: len(slot.needed_by) for slot in slots}
    values = [counts[field] for field in picked]
    assert values == sorted(values, reverse=True)  # 受影响的规则条数降序
    assert candidate_fields(RULES, {}) == picked  # 确定性：同一输入同一顺序


def test_candidate_fields_honours_limit_and_completed_facts() -> None:
    assert candidate_fields(RULES, {}, limit=2) == candidate_fields(RULES, {})[:2]
    assert candidate_fields(RULES, {}, limit=0) == ()
    first = candidate_fields(RULES, {})[0]
    rest = candidate_fields(RULES, {first: "1"})
    assert first not in rest


# ---------- ask 节点 ----------
def _ask_state(**overrides: Any) -> CheckState:
    state: dict[str, Any] = {"missing_slots": list(BLOCKING), "facts": {}, "ask_rounds": 0}
    state.update(overrides)
    return cast(CheckState, state)


def _run_ask(state: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """调 ask 节点并按普通字典取键：节点只返回状态差额，测试只关心其中几项。"""
    return dict(ask(_ask_state(**state), deps))


def test_ask_node_keeps_question_count_and_round_semantics() -> None:
    out = _run_ask({}, DEPS)
    assert out["ask_rounds"] == 1
    assert len(out["questions"]) == len(BLOCKING)
    assert [item["slot_id"] for item in out["ask_items"]] == BLOCKING
    assert all(item["source"] == "fixed" for item in out["ask_items"])  # 无模型
    assert out["ask_history"][-1] == {"round": 1, "questions": out["questions"]}

    second = _run_ask({"ask_rounds": 1, "ask_items": out["ask_items"]}, DEPS)
    assert second["ask_rounds"] == 2
    assert len(second["questions"]) == len(BLOCKING)


def test_ask_node_calls_model_only_on_first_round() -> None:
    llm = FakeLLM({"asks": [{"slot_id": "term_years", "question": "这份合同约定的期限是几年？"}]})
    deps = Deps(rules=RULES, generator=llm)
    first = _run_ask({}, deps)
    assert len(llm.calls) == 1
    assert _by_slot(first["ask_items"])["term_years"]["source"] == "llm"

    second = _run_ask({"ask_rounds": 1, "ask_items": first["ask_items"]}, deps)
    assert len(llm.calls) == 1  # 第二轮复用，不再调用
    assert second["questions"] == first["questions"]


def test_ask_node_trims_to_blocking_set() -> None:
    llm = FakeLLM(
        {
            "asks": [
                {"slot_id": "term_years", "question": "这份合同约定的期限是几年？"},
                {"slot_id": "written_contract", "question": "是否已经和员工签了书面劳动合同？"},
                {"slot_id": "employer_type", "question": "您目前的经营主体是哪一种类型？"},
                {"slot_id": "salary", "question": "员工每个月的工资大概是多少钱呢？"},
            ]
        }
    )
    deps = Deps(rules=RULES, generator=llm)
    out = _run_ask({}, deps)
    assert len(out["questions"]) == len(BLOCKING)
    assert [item["slot_id"] for item in out["ask_items"]] == BLOCKING
    assert any("不在待补事实范围内" in error for error in out["errors"])


def test_verify_asks_caps_item_count() -> None:
    allowed = {"a", "b", "c", "d", "e"}
    raw = {"asks": [{"slot_id": key, "question": "这份合同约定的期限是几年？"} for key in sorted(allowed)]}
    asks, errors = _verify_asks(raw, allowed, ["a", "b", "c"], {})
    assert [item["slot_id"] for item in asks] == ["a", "b", "c"]
    assert any("超出上限" in error for error in errors)


def test_ask_items_survive_roundtrip(tmp_path) -> None:
    """追问清单要能落库再读回：界面靠字段 id 回填，丢了这个键就会按问句原文硬猜。"""
    paths = Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml")
    repo = SqliteRepository(paths=paths, db_path=str(tmp_path / "t.sqlite3"))
    ask_items = [
        {"slot_id": "employer_type", "question": field_question("employer_type"), "source": "llm"},
        {"slot_id": "term_years", "question": field_question("term_years"), "source": "fixed"},
    ]
    repo.save_task(
        CheckTask(
            task_id="T-ask",
            mode=TaskMode.CHECK,
            enterprise_id="E1",
            questions=[item["question"] for item in ask_items],
            ask_items=ask_items,
            ask_rounds=1,
            created_at=datetime(2026, 11, 6, 9, 0),
        )
    )
    got = repo.list_tasks("E1")[0]
    assert got.ask_items == ask_items
    assert got.questions == [item["question"] for item in ask_items]
