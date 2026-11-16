"""P2-6 / 规格 003 第 7.2 节：L3.2 行为校验（模型守不守 L1）。

只判**形态**（字段在不在、类型对不对、在不在长度上限内），不判内容对错：
引错法条、抄错原文、证据定位不到都归 L3.1 六道核验，本文件的用例断言这两层分得开。

全部用 stub 模型，不联网、不花钱。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mla.domain.clauses import ClauseCandidate
from mla.domain.models import StatuteEntry
from mla.services.clause_review import (
    L3_2_PREFIX,
    MAX_REASON,
    _behavior_check,
    review_clauses,
)

CONTRACT = "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n乙方月综合薪资 8000 元，其中基本工资 2200 元。\n"
TRANSFER_SPAN = "甲方有权单方调整乙方工作岗位，乙方不得拒绝"
# 假库条文的真实子串（真实劳动合同法第三十五条），A 级三件套要过第 3 道回声核验才靠得住。
QUOTE_35 = "用人单位与劳动者协商一致，可以变更劳动合同约定的内容"


class StubLLM:
    """按顺序返回预置结果的假模型；记录每次调用的 payload 与 system。"""

    available = True

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []
        self.systems: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.payloads)

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.systems.append(system)
        self.payloads.append(json.loads(user))
        if not self.responses:
            return {"findings": [], "extra": []}
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def _candidate(statutes: tuple[str, ...] = ("LCL-35", "LCL-39")) -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=1,
        span=TRANSFER_SPAN,
        trigger_id="TR-UNILATERAL-TRANSFER",
        name="单方强制调岗",
        candidate_statute_ids=statutes,
        severity_hint="A",
    )


def _statute(sid: str) -> StatuteEntry:
    return StatuteEntry(
        statute_id=sid,
        law_name="中华人民共和国劳动合同法",
        article_no="第三十五条",
        text=QUOTE_35 + "。",
        issuing_body="全国人民代表大会常务委员会",
    )


LIBRARY = {sid: _statute(sid) for sid in ("LCL-35", "LCL-39", "LCL-31")}


def _row(**over: Any) -> dict[str, Any]:
    """一条形态齐全的模型输出（A 级三件套齐备）。"""
    row: dict[str, Any] = {
        "idx": 0,
        "grade": "A",
        "reason": "该约定与第三十五条关于协商一致变更的规定不符",
        "statute_ids": ["LCL-35"],
        "evidence": TRANSFER_SPAN,
        "quote_statute": QUOTE_35,
        "need_facts": [],
    }
    row.update(over)
    return row


# ---------- 检查项 1：idx ----------


def test_missing_idx_fails() -> None:
    row = _row()
    del row["idx"]
    problems = _behavior_check(row, [_candidate()])
    assert problems == ["缺 idx（每条结论必须指向本批候选下标）"]


def test_idx_out_of_range_fails() -> None:
    problems = _behavior_check(_row(idx=7), [_candidate()])
    assert len(problems) == 1
    assert "idx 越界" in problems[0] and "7" in problems[0]


def test_idx_bool_fails_not_used_as_index() -> None:
    """JSON 的 true 是 bool（int 子类），不能被当成 idx=1。"""
    problems = _behavior_check(_row(idx=True), [_candidate(), _candidate()])
    assert len(problems) == 1
    assert "idx 越界" in problems[0] and "不是整数" in problems[0]


# ---------- 检查项 2：grade ----------


@pytest.mark.parametrize("grade", ["d", "a", "A ", "", None])
def test_grade_out_of_enum_fails(grade: object) -> None:
    """大小写敏感（`a` / `A ` 都是脏值），空值与缺失同样不过。"""
    problems = _behavior_check(_row(grade=grade), [_candidate()])
    assert len(problems) == 1
    assert "等级不合法" in problems[0]


# ---------- 检查项 3：reason ----------


def test_empty_reason_fails() -> None:
    problems = _behavior_check(_row(reason=""), [_candidate()])
    assert problems == ["缺 reason（空值）"]


def test_reason_too_long_fails() -> None:
    problems = _behavior_check(_row(reason="该" * (MAX_REASON + 1)), [_candidate()])
    assert len(problems) == 1
    assert "reason 超长" in problems[0] and str(MAX_REASON) in problems[0]


def test_reason_at_upper_bound_passes() -> None:
    """正好 200 字是上限本身（闭区间），不该判超长。"""
    assert _behavior_check(_row(reason="该" * MAX_REASON), [_candidate()]) == []


# ---------- 检查项 4：statute_ids ----------


def test_empty_statute_ids_fails_for_a_grade() -> None:
    problems = _behavior_check(_row(statute_ids=[]), [_candidate()])
    assert problems == ["缺 statute_ids（A/B 级必须有法条依据）"]


def test_statute_ids_not_list_fails() -> None:
    problems = _behavior_check(_row(statute_ids="LCL-35"), [_candidate()])
    assert problems == ["statute_ids 不是字符串列表"]


def test_statute_ids_blank_element_fails() -> None:
    problems = _behavior_check(_row(statute_ids=["LCL-35", " "]), [_candidate()])
    assert len(problems) == 1
    assert "空元素" in problems[0]


def test_statute_ids_duplicates_deduped_still_pass() -> None:
    """重复条号去重后仍非空 → 通过（去重是 L3.1 与落库口径，不是 L3.2 的失败项）。"""
    assert _behavior_check(_row(statute_ids=["LCL-35", "LCL-35", "LCL-35"]), [_candidate()]) == []


def test_c_grade_may_omit_statute_and_quote() -> None:
    """C 级的定义就是"事实不足以下定性"，允许没有法条依据（4.2 L1 第 1 条：A/B 强制、C 可空）。"""
    row = _row(grade="C", statute_ids=[], quote_statute="")
    assert _behavior_check(row, [_candidate()]) == []
    row_without_quote = _row(grade="C", statute_ids=[])
    del row_without_quote["quote_statute"]
    assert _behavior_check(row_without_quote, [_candidate()]) == []


# ---------- 检查项 5：evidence ----------


def test_empty_evidence_fails() -> None:
    problems = _behavior_check(_row(evidence="   "), [_candidate()])
    assert problems == ["缺 evidence（空值）"]


def test_evidence_not_string_fails() -> None:
    problems = _behavior_check(_row(evidence=[TRANSFER_SPAN]), [_candidate()])
    assert problems == ["evidence 不是字符串"]


# ---------- 检查项 6：quote_statute ----------


def test_grade_a_without_quote_fails() -> None:
    row = _row()
    del row["quote_statute"]
    problems = _behavior_check(row, [_candidate()])
    assert problems == ["缺 quote_statute（A/B 级必填）"]


def test_quote_not_string_fails() -> None:
    problems = _behavior_check(_row(quote_statute=[QUOTE_35]), [_candidate()])
    assert problems == ["quote_statute 不是字符串"]


# ---------- 全部通过 ----------


def test_all_fields_present_passes() -> None:
    assert _behavior_check(_row(), [_candidate()]) == []
    # 可选字段齐备也算通过（numeric / self_check / impact_suggest 都是 6.2 允许的字段）
    full = _row(
        numeric={"field": "probation_months", "actual": "3", "limit": "1"},
        self_check={"paragraphs_read": [1]},
        impact_suggest="高",
    )
    assert _behavior_check(full, [_candidate()]) == []


# ---------- 检查项 7：未知字段（不算失败） ----------


def test_unknown_field_passes_with_note() -> None:
    errors: list[str] = []
    row = _row(confidence=0.92)
    assert _behavior_check(row, [_candidate()], diagnostics=errors) == []
    assert len(errors) == 1
    assert errors[0].startswith("L3.2 形态校验提示：")
    assert "confidence" in errors[0]


def test_unknown_field_does_not_drop_finding() -> None:
    llm = StubLLM({"findings": [_row(confidence=0.92)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1, "未知字段只记提示，不能因此丢结论"
    assert any("confidence" in e for e in errors)
    assert not any(L3_2_PREFIX in e for e in errors)


# ---------- 检查项 8：失败条目不进入 L3.1 ----------


def test_l3_2_failed_row_never_reaches_l3_1() -> None:
    """缺 quote（L3.2）× 证据抄错（L3.1 第 2 道）→ 只留 L3.2 诊断，拿不出第 2 道的诊断。"""
    llm = StubLLM({"findings": [_row(quote_statute="", evidence="合同里根本没有的一句话")]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any(L3_2_PREFIX in e and "quote_statute" in e for e in errors)
    assert not any("第 2 道核验" in e for e in errors), "L3.2 不过的条目不进入 L3.1 六道"
    assert not any("无法在合同原文定位" in e for e in errors)


def test_l3_1_diagnostics_do_not_carry_l3_2_prefix() -> None:
    """同一批里两类失败并存：L3.2 有固定前缀，L3.1 沿用 `{trigger_id}：...` 句式，互不污染。"""
    rows = [
        _row(reason=""),  # L3.2：reason 空
        _row(statute_ids=["LCL-31"]),  # L3.1 第 4 道：越出候选闭集
    ]
    llm = StubLLM({"findings": rows})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any(e.startswith(L3_2_PREFIX) for e in errors)
    assert any("引用越出候选闭集" in e and "L3.2" not in e for e in errors)


def test_every_l3_2_diagnostic_starts_with_fixed_prefix() -> None:
    rows = [
        _row(idx=7),
        _row(grade="d"),
        _row(evidence=""),
        _row(statute_ids=[]),
    ]
    llm = StubLLM({"findings": rows})
    errors: list[str] = []
    review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    failures = [e for e in errors if "形态校验" in e]
    assert len(failures) == 4, "每条失败各一条诊断"
    assert all(e.startswith(L3_2_PREFIX) for e in failures)


# ---------- 与消融臂的边界 ----------


def test_verify_false_skips_behavior_check() -> None:
    """`verify=False`（model_raw 臂）原样保留模型输出：非法等级也不能被 L3.2 提前丢掉。"""
    llm = StubLLM({"findings": [_row(grade="S", reason="", statute_ids=[])]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY, verify=False)

    assert len(got) == 1
    assert got[0].grade == "S"
    assert not any(L3_2_PREFIX in e for e in errors)
