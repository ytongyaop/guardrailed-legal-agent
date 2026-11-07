"""条款判定层测试：批量切分、三道程序核验、自提封顶、降级。

全部用 stub 模型，不联网、不花钱、结果确定。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mla.adapters.llm import LLMError
from mla.config import LLMConfig, load_config
from mla.domain.clauses import ClauseCandidate
from mla.domain.models import ClauseFinding, Role, StatuteEntry, User
from mla.services.clause_review import _locate, _verify, review_clauses
from mla.services.tasks import run_check

CONTRACT = (
    "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n"
    "乙方月综合薪资 8000 元，其中基本工资 2200 元。\n"
    "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴。\n"
)

TRANSFER_SPAN = "甲方有权单方调整乙方工作岗位，乙方不得拒绝"
SI_SPAN = "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴"


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


def _candidate(
    paragraph: int = 1,
    span: str = TRANSFER_SPAN,
    trigger_id: str = "TR-UNILATERAL-TRANSFER",
    statutes: tuple[str, ...] = ("LCL-35", "LCL-39"),
) -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=paragraph,
        span=span,
        trigger_id=trigger_id,
        name="单方强制调岗",
        candidate_statute_ids=statutes,
        severity_hint="A",
    )


def _statute(sid: str, text: str = "用人单位与劳动者协商一致，可以变更劳动合同约定的内容。") -> StatuteEntry:
    return StatuteEntry(
        statute_id=sid,
        law_name="中华人民共和国劳动合同法",
        article_no="第三十五条",
        text=text,
        issuing_body="全国人民代表大会常务委员会",
    )


LIBRARY = {sid: _statute(sid) for sid in ("LCL-35", "LCL-39", "LCL-26", "LCL-31", "J2-19", "SIL-58")}


def _finding(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "idx": 0,
        "grade": "A",
        "reason": "该约定与第三十五条关于协商一致变更的规定不符",
        "statute_ids": ["LCL-35"],
        "evidence": TRANSFER_SPAN,
        "need_facts": [],
    }
    row.update(over)
    return row


# ---------- 闭集与法源库核验 ----------


def test_out_of_candidate_set_dropped() -> None:
    """引用了库内存在、但不在该候选候选集里的条号（LCL-31）→ 丢弃并留痕。"""
    llm = StubLLM({"findings": [_finding(statute_ids=["LCL-31"])]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("候选闭集" in e for e in errors)


def test_unknown_statute_id_dropped() -> None:
    llm = StubLLM({"findings": [_finding(statute_ids=["LCL-35", "LCL-999"])]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("不存在" in e and "LCL-999" in e for e in errors)


# ---------- 证据可定位 ----------


def test_evidence_not_in_contract_dropped() -> None:
    llm = StubLLM({"findings": [_finding(evidence="乙方同意无限期加班且不主张任何报酬")]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("无法在合同原文定位" in e for e in errors)


def test_evidence_with_space_difference_still_located() -> None:
    """证据与原文只差空格（归一化后一致）→ 允许保留（与 _norm 口径一致）。"""
    llm = StubLLM({"findings": [_finding(evidence="甲方有权 单方调整乙方工作岗位，乙方不得拒绝")]})
    got = review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].span == TRANSFER_SPAN


# ---------- 等级与依据 ----------


def test_grade_a_without_statute_downgraded_to_c() -> None:
    llm = StubLLM({"findings": [_finding(statute_ids=[])]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "C"
    assert any("降为 C" in e for e in errors)


def test_invalid_grade_dropped() -> None:
    llm = StubLLM({"findings": [_finding(grade="S")]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("等级不合法" in e for e in errors)


def test_idx_out_of_range_dropped() -> None:
    llm = StubLLM({"findings": [_finding(idx=7)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("idx 越界" in e for e in errors)


def test_idx_bool_dropped_not_used_as_index() -> None:
    """JSON 的 true 是 bool（int 子类），不能被当成 idx=1 命中别的候选。"""
    second = _candidate(paragraph=2, span="乙方月综合薪资 8000 元")
    llm = StubLLM({"findings": [_finding(idx=True, evidence="乙方月综合薪资 8000 元")]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate(), second], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == (), "idx=True 不该等价于 idx=1"
    assert any("idx 越界" in e for e in errors)


# ---------- 模型自提（extra） ----------


def test_verify_off_keeps_extra_grade_without_c_cap() -> None:
    """消融对照臂（verify=False）：自提条目保留模型原始等级，不做「一律 C」封顶。"""
    extra = [
        {
            "grade": "A",
            "reason": "该约定与第二十六条存在冲突",
            "statute_ids": ["LCL-26"],
            "evidence": SI_SPAN,
            "need_facts": [],
        }
    ]
    llm = StubLLM({"findings": [], "extra": extra})
    got = review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY, verify=False)

    assert len(got) == 1
    assert got[0].source == "model_extra"
    assert got[0].grade == "A", "消融臂不封顶 C；生产臂（verify=True）才封顶"
    # 同一份输出走生产臂：自提一律降 C
    kept = review_clauses(
        StubLLM({"findings": [], "extra": extra}), [_candidate()], CONTRACT, statutes=LIBRARY
    )
    assert [f.grade for f in kept] == ["C"]


def test_extra_always_c_and_capped() -> None:
    """自提不论模型给什么等级一律降 C；条数封顶 max_extra。"""
    extra = [
        {
            "grade": "A",
            "reason": "该约定与第二十六条关于排除劳动者权利的规定存在冲突",
            "statute_ids": ["LCL-26"],
            "evidence": SI_SPAN,
            "need_facts": [],
        },
        {
            "grade": "B",
            "reason": "该约定与第三十五条存在冲突",
            "statute_ids": [],
            "evidence": "乙方月综合薪资 8000 元",
            "need_facts": ["实际工资基数"],
        },
        {
            "grade": "C",
            "reason": "重复片段",
            "statute_ids": [],
            "evidence": SI_SPAN,
            "need_facts": [],
        },
        {
            "grade": "C",
            "reason": "第四条",
            "statute_ids": [],
            "evidence": "其中基本工资 2200 元",
            "need_facts": [],
        },
    ]
    llm = StubLLM({"findings": [], "extra": extra})
    got = review_clauses(llm, [_candidate()], CONTRACT, max_extra=2, statutes=LIBRARY)

    assert len(got) == 2
    assert all(f.grade == "C" for f in got)
    assert all(f.source == "model_extra" and f.trigger_id == "" for f in got)
    # 自提也要求证据可定位：span 是原文切片，必是子串
    assert all(f.span in CONTRACT for f in got)


def test_extra_unknown_statute_still_dropped() -> None:
    """自提的法条不要求 ∈ 候选集，但仍必须在法源库内。"""
    extra = [
        {
            "grade": "C",
            "reason": "该约定与某条规定存在冲突",
            "statute_ids": ["LCL-888"],
            "evidence": SI_SPAN,
            "need_facts": [],
        }
    ]
    llm = StubLLM({"findings": [], "extra": extra})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("LCL-888" in e for e in errors)


def test_extra_overlapping_trigger_finding_dropped() -> None:
    """自提与已命中条款重叠 → 不算"触发器之外"的新发现，丢弃。"""
    llm = StubLLM(
        {
            "findings": [_finding()],
            "extra": [
                {
                    "grade": "C",
                    "reason": "重复片段",
                    "statute_ids": [],
                    "evidence": TRANSFER_SPAN,
                    "need_facts": [],
                }
            ],
        }
    )
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert [f.source for f in got] == ["trigger"]
    assert any("与已命中条款重叠" in e for e in errors)


# ---------- 重复文本定位与自提严格去重 ----------

# 同一行里埋两个不同问题（押金 + 放弃社保），span 高度重叠。
SHARED_LINE = "乙方同意向甲方缴纳押金 1000 元，并自愿放弃缴纳社会保险"
SHARED_CONTRACT = SHARED_LINE + "。\n"


def _shared_candidate() -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=1,
        span=SHARED_LINE,
        trigger_id="TR-DEPOSIT-OR-FEES",
        name="向劳动者收取押金",
        candidate_statute_ids=("LCL-26",),
        severity_hint="A",
    )


def test_extra_deduped_even_if_different_reason() -> None:
    """同段不同事由（押金已命中、自提讲社保）→ 自提仍丢弃（2026-11-06 回调，严格口径）。

    同一句里的多个问题由触发器按事由分别召回，不靠自提拆分：
    按事由保留会让同一句被重复报 2–3 次（实测 8 条自提全是同句重复噪音）。
    """
    extra = [
        {
            "grade": "C",
            "reason": "该约定与第五十八条关于社会保险的规定存在冲突",
            "statute_ids": [],
            "evidence": "并自愿放弃缴纳社会保险",
            "need_facts": [],
        }
    ]
    llm = StubLLM(
        {
            "findings": [
                _finding(
                    statute_ids=["LCL-26"],
                    evidence=SHARED_LINE,
                    reason="该约定与第二十六条关于收取押金的规定存在冲突",
                )
            ],
            "extra": extra,
        }
    )
    errors: list[str] = []
    got = review_clauses(llm, [_shared_candidate()], SHARED_CONTRACT, errors=errors, statutes=LIBRARY)

    assert [f.source for f in got] == ["trigger"], "事由不同也按重复丢弃"
    assert any("与已命中条款重叠" in e for e in errors)


def test_extra_dropped_when_same_span_and_same_subject() -> None:
    """同一处 + 同一事由（都讲押金）→ 仍按重复丢弃。"""
    extra = [
        {
            "grade": "C",
            "reason": "该约定与第九条关于收取押金的规定不符",
            "statute_ids": [],
            "evidence": "乙方同意向甲方缴纳押金 1000 元",
            "need_facts": [],
        }
    ]
    llm = StubLLM(
        {
            "findings": [
                _finding(
                    statute_ids=["LCL-26"],
                    evidence=SHARED_LINE,
                    reason="该约定与第九条关于收取押金的规定不符",
                )
            ],
            "extra": extra,
        }
    )
    errors: list[str] = []
    got = review_clauses(llm, [_shared_candidate()], SHARED_CONTRACT, errors=errors, statutes=LIBRARY)

    assert [f.source for f in got] == ["trigger"]
    assert any("与已命中条款重叠" in e for e in errors)


def test_locate_prefers_matching_paragraph() -> None:
    """证据文本在合同里重复出现时，给了段落就取该段的那次，不给仍取首次出现。"""
    repeated = "乙方自愿放弃缴纳社会保险"
    contract = f"{repeated}。\n乙方月综合薪资 8000 元。\n{repeated}。\n"
    third = contract.rindex(repeated)

    assert _locate(repeated, contract, 3) == (third, third + len(repeated))
    assert _locate(repeated, contract) == (0, len(repeated)), "无段落约束时保持原行为（首次出现）"
    assert _locate(repeated, contract, 3) != (0, len(repeated)), "重复文本不能错绑到第一处"
    assert _locate(repeated, contract, 2) == (0, len(repeated)), "该段没有命中时退回首次出现"


# ---------- 封顶只停止新增，不静默丢诊断 ----------


def test_extra_cap_counts_unprocessed_instead_of_silent_break() -> None:
    """max_extra=1 + 4 条自提（第 1 条与已命中重叠）→ 自提保留 1 条，其余计数诊断。"""
    extra = [
        {"grade": "C", "reason": "重复片段", "statute_ids": [], "evidence": TRANSFER_SPAN, "need_facts": []},
        {
            "grade": "C",
            "reason": "该约定与第三十五条存在冲突",
            "statute_ids": [],
            "evidence": "乙方月综合薪资 8000 元",
            "need_facts": [],
        },
        {
            "grade": "C",
            "reason": "该约定与第三十五条存在冲突",
            "statute_ids": [],
            "evidence": "其中基本工资 2200 元",
            "need_facts": [],
        },
        {
            "grade": "C",
            "reason": "该约定与第二十六条存在冲突",
            "statute_ids": [],
            "evidence": SI_SPAN,
            "need_facts": [],
        },
    ]
    llm = StubLLM({"findings": [_finding()], "extra": extra})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, max_extra=1, errors=errors, statutes=LIBRARY)

    extras = [f for f in got if f.source == "model_extra"]
    assert len(extras) == 1, "配额是 1 条，不能因为第 1 条重叠就把配额一起丢掉"
    assert any("因封顶" in e and "2 条" in e for e in errors), "封顶后未处理的条目必须留诊断"


def test_extra_cap_is_global_across_batches() -> None:
    """两批各报 2 条自提，max_extra=1 → 整份合同只留 1 条。"""
    extra = [
        {
            "grade": "C",
            "reason": "该约定与第三十五条存在冲突",
            "statute_ids": [],
            "evidence": SI_SPAN,
            "need_facts": [],
        },
        {
            "grade": "C",
            "reason": "该约定与第三十五条存在冲突",
            "statute_ids": [],
            "evidence": "乙方月综合薪资 8000 元",
            "need_facts": [],
        },
    ]
    candidates = [
        _candidate(paragraph=1, trigger_id="TR-UNILATERAL-TRANSFER"),
        _candidate(paragraph=3, trigger_id="TR-SI-WAIVER", statutes=("J2-19",)),
    ]
    llm = StubLLM({"findings": [], "extra": extra})
    got = review_clauses(llm, candidates, CONTRACT, batch_size=1, max_extra=1, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].source == "model_extra" and got[0].grade == "C"


# ---------- 批量与降级 ----------


def test_batching_splits_candidates() -> None:
    """20 条候选、batch_size=5 → 4 次调用，每批 ≤5 条且 idx 从 0 起。"""
    candidates = [_candidate(paragraph=i + 1, trigger_id=f"TR-TEST-{i:02d}") for i in range(20)]
    llm = StubLLM({"findings": []})
    errors: list[str] = []
    got = review_clauses(llm, candidates, CONTRACT, batch_size=5, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert llm.calls == 4
    assert [len(p["candidates"]) for p in llm.payloads] == [5, 5, 5, 5]
    assert [c["idx"] for c in llm.payloads[0]["candidates"]] == [0, 1, 2, 3, 4]
    assert not errors


def test_invalid_json_returns_empty_with_errors() -> None:
    llm = StubLLM(ValueError("Expecting value: line 1 column 1"))
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("调用失败" in e for e in errors)


def test_non_object_output_returns_empty_with_errors() -> None:
    llm = StubLLM(["不是 JSON 对象"])  # type: ignore[list-item]
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("不是 JSON 对象" in e for e in errors)


def test_no_generator_returns_empty_with_errors() -> None:
    errors: list[str] = []
    got = review_clauses(None, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("未配置大模型" in e for e in errors)


# ---------- span 与排序 ----------


def test_span_comes_from_candidate_not_model_rewrite() -> None:
    """模型自己给出 span 字段也不采信：span 一律取候选片段。"""
    row = _finding(span="模型改写的定位")
    llm = StubLLM({"findings": [row]})
    got = review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].span == TRANSFER_SPAN
    assert got[0].span in CONTRACT
    assert got[0].trigger_id == "TR-UNILATERAL-TRANSFER"
    assert got[0].source == "trigger"


def test_low_overlap_evidence_kept_but_recorded() -> None:
    """证据与候选片段不重叠：仍以证据为准（已核验在原文），span 仍取候选并记诊断。"""
    row = _finding(evidence="乙方月综合薪资 8000 元，其中基本工资 2200 元")
    llm = StubLLM({"findings": [row]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].span == TRANSFER_SPAN
    assert any("重叠不足" in e for e in errors)


def test_findings_sorted_by_paragraph_and_grade() -> None:
    rows = [
        _finding(idx=1, grade="C", statute_ids=[], evidence=SI_SPAN, reason="待确认"),
        _finding(idx=0, grade="A"),
    ]
    candidates = [
        _candidate(paragraph=1, span=TRANSFER_SPAN, trigger_id="TR-UNILATERAL-TRANSFER"),
        _candidate(paragraph=3, span=SI_SPAN, trigger_id="TR-SI-WAIVER", statutes=("J2-19", "SIL-58")),
    ]
    llm = StubLLM({"findings": rows})
    got = review_clauses(llm, candidates, CONTRACT, statutes=LIBRARY)

    assert [(f.paragraph, f.grade, f.trigger_id) for f in got] == [
        (1, "A", "TR-UNILATERAL-TRANSFER"),
        (3, "C", "TR-SI-WAIVER"),
    ]


# ---------- prompt 口径 ----------


def test_payload_carries_statute_text_and_closed_set_rule() -> None:
    llm = StubLLM({"findings": []})
    review_clauses(llm, [_candidate()], CONTRACT, max_extra=2, statutes=LIBRARY)

    payload = llm.payloads[0]
    candidate = payload["candidates"][0]
    assert candidate["candidate_statute_ids"] == ["LCL-35", "LCL-39"]
    assert all(s["statute_id"] in {"LCL-35", "LCL-39"} for s in candidate["statutes"])
    assert all(s["text"] for s in candidate["statutes"])
    assert payload["max_extra"] == 2
    assert any(line.startswith("1. ") for line in payload["contract_paragraphs"])
    system = llm.systems[0]
    assert "只能从它自己的 candidate_statute_ids 里选法条" in system
    assert "最多 2 条" in system


def test_verify_directly_rejects_without_candidate_set() -> None:
    """`_verify` 可独立调用：自提结论不检查候选集，但等级强制 C。"""
    errors: list[str] = []
    finding = _verify(
        _finding(grade="B", statute_ids=["LCL-26"], evidence=SI_SPAN),
        None,
        CONTRACT,
        {"LCL-26"},
        errors=errors,
    )
    assert isinstance(finding, ClauseFinding)
    assert finding.grade == "C"
    assert finding.source == "model_extra"
    assert finding.statute_ids == ("LCL-26",)
    assert finding.span in CONTRACT


@pytest.mark.parametrize("reason", ["", "该约定与第三十五条不符"])
def test_reason_filled_when_missing(reason: str) -> None:
    llm = StubLLM({"findings": [_finding(reason=reason)]})
    got = review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY)
    assert len(got) == 1
    assert got[0].reason


# ---------- 接线：开关打开才跑，且试用期工资规则让位 ----------


class _WiringLLM:
    """接线测试用假模型：条款判定固定返回一条 A 级结论。"""

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"findings": [_finding()], "extra": []}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


class _FailingLLM:
    """条款判定必失败（模拟网络 / 鉴权错误）：用于验证规则回补、风险不静默消失。"""

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        raise LLMError("HTTP 401：鉴权失败")

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


class _ProbationWageLLM:
    """只对试用期工资基数这一条候选出结论，其余候选一律不报。"""

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.loads(user)
        for idx, candidate in enumerate(payload.get("candidates") or []):
            if candidate.get("trigger_id") != "TR-PROBATION-WAGE-BASE":
                continue
            return {
                "findings": [
                    {
                        "idx": idx,
                        "grade": "A",
                        "reason": "该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符",
                        "statute_ids": [candidate["candidate_statute_ids"][0]],
                        "evidence": candidate["span"],
                        "need_facts": [],
                    }
                ],
                "extra": [],
            }
        return {"findings": [], "extra": []}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def _run_check_with(monkeypatch: pytest.MonkeyPatch, contract: str, enabled: bool, llm: Any = None):
    monkeypatch.setenv("MLA_CLAUSE_REVIEW", "on" if enabled else "off")
    fake = llm if llm is not None else _WiringLLM()
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: fake)
    user = User(
        user_id="U1",
        username="alice",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )
    return run_check(
        user=user,
        contract_text=contract,
        llm=LLMConfig(api_key="test-key"),
        save=False,
        enterprise_id="E1",
    )


PROBATION_CONTRACT = CONTRACT + "试用期工资按基本工资的 70% 发放。\n"
# 试用期工资比例低于 80%：规则 R-PROBATION-WAGE-80 与触发器 TR-PROBATION-WAGE-BASE 同域命中
PROBATION_WAGE_CONTRACT = "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n试用期工资为转正工资的 60%。\n"


def test_config_clause_review_on_by_default() -> None:
    """默认开启：不设环境变量时按 on 解析；显式 off 时关闭，行为与关闭前一致。"""
    assert load_config({}).clause_review.enabled is True
    assert load_config({"MLA_CLAUSE_REVIEW": "on"}).clause_review.enabled is True
    assert load_config({"MLA_CLAUSE_REVIEW": "off"}).clause_review.enabled is False


def test_wiring_off_keeps_rule_and_no_findings(monkeypatch) -> None:
    task = _run_check_with(monkeypatch, PROBATION_CONTRACT, enabled=False)
    assert task.clause_findings == []
    assert any(i.rule_id == "R-PROBATION-WAGE-80" for i in task.items)


def test_wiring_on_keeps_rule_without_matching_finding(monkeypatch) -> None:
    """有条款结论但不同域（这里是单方调岗）→ 试用期工资规则照常报，不被误剔。"""
    task = _run_check_with(monkeypatch, PROBATION_CONTRACT, enabled=True)
    assert [f.grade for f in task.clause_findings] == ["A"]
    assert task.clause_findings[0].source == "trigger"
    assert task.clause_findings[0].trigger_id == "TR-UNILATERAL-TRANSFER"
    assert any(i.rule_id == "R-PROBATION-WAGE-80" for i in task.items)


def test_demote_rule_kept_when_model_fails(monkeypatch) -> None:
    """模型失败 → 条款通道为空，试用期工资风险必须由规则报出来（不静默消失）。"""
    task = _run_check_with(monkeypatch, PROBATION_WAGE_CONTRACT, enabled=True, llm=_FailingLLM())

    assert task.clause_findings == []
    assert any(i.rule_id == "R-PROBATION-WAGE-80" for i in task.items)
    assert any("条款判定调用失败" in e for e in task.errors)


def test_demote_rule_deduped_when_finding_exists(monkeypatch) -> None:
    """同域触发器已出结论 → 规则条目不重复报（同一风险只留一条）。"""
    task = _run_check_with(monkeypatch, PROBATION_WAGE_CONTRACT, enabled=True, llm=_ProbationWageLLM())

    assert [f.trigger_id for f in task.clause_findings] == ["TR-PROBATION-WAGE-BASE"]
    assert all(i.rule_id != "R-PROBATION-WAGE-80" for i in task.items)


# ---------- 落库 ----------


def test_clause_findings_persist_roundtrip(tmp_path) -> None:
    from mla.adapters.persist import SqliteRepository
    from mla.config import Paths
    from mla.domain.models import CheckTask, TaskMode

    repo = SqliteRepository(
        paths=Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml"),
        db_path=str(tmp_path / "clause.sqlite3"),
    )
    finding = ClauseFinding(
        paragraph=1,
        span=TRANSFER_SPAN,
        grade="A",
        reason="该约定与第三十五条不符",
        statute_ids=("LCL-35",),
        trigger_id="TR-UNILATERAL-TRANSFER",
        source="trigger",
        need_facts=("是否已协商一致",),
    )
    task = CheckTask(
        task_id="T-1",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        clause_findings=[finding],
    )
    repo.save_task(task)
    saved = repo.list_tasks("E1")
    assert saved and saved[0].clause_findings == [finding]
