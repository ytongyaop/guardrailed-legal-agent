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
from mla.domain.rules import RuleCheck, RuleCheckStatus
from mla.services.clause_review import (
    MAX_EVIDENCE_CHARS,
    MAX_FACT_CHARS,
    MAX_FACTS,
    MAX_RULE_CHECKS,
    MIN_EVIDENCE_CHARS,
    REVIEW_SYSTEM,
    _locate,
    _overlap_ratio,
    _payload,
    _verify,
    review_clauses,
)
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

# P2-1：A 级要求 statute_ids + evidence + quote_statute 三件套，缺 quote 会降 B。
# 这里用假库条文的真实子串（也是真实劳动合同法第三十五条的子串），断言里的 A 级才成立。
QUOTE_35 = "用人单位与劳动者协商一致，可以变更劳动合同约定的内容"


def _finding(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "idx": 0,
        "grade": "A",
        "reason": "该约定与第三十五条关于协商一致变更的规定不符",
        "statute_ids": ["LCL-35"],
        "evidence": TRANSFER_SPAN,
        "need_facts": [],
        "quote_statute": QUOTE_35,
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


# ---------- 第 2 道加严：证据长度 / 段落 / 跨段（P2-2） ----------

# 9 字：这是合同原文的子串，单看定位没问题，只有长度让它不过。
NINE_CHARS = "甲方有权单方调整乙"
# 101 字：合法但超上限的整句（合同原文连续抄写，只是太长了）。
LONG_EVIDENCE = (
    "乙方确认甲方已在签订本合同前就全部条款逐条说明并充分协商，乙方对合同内容完全理解并自愿接受，"
    "且承诺不再就合同条款提出任何异议或另行主张任何补充权利，包括但不限于工资构成、考勤管理、奖惩办法与合同解除条件"
)
LONG_CONTRACT = CONTRACT + LONG_EVIDENCE + "。\n"
# 84 字：真实对抗合同（`contract_traps_01` 段 20）被触发器命中的整行，verbatim 照抄的长度。
# 这是上限从 80 放宽到 100 的直接理由：80 会把这类整行照抄判成"长度越界"。
EVIDENCE_84 = (
    "甲方有权根据经营需要、项目调整，单方无条件调整乙方工作岗位、工作地点、工作内容，乙方不得拒绝；"
    "拒绝调岗视为严重违反规章制度，甲方可无偿解除劳动合同，无需支付经济补偿金。"
)
# 100 字：正好等于上限的边界证据（合法、连续、能在原文定位）。
EVIDENCE_100 = (
    "乙方确认甲方已在签订本合同前就全部条款逐条说明并充分协商，"
    "乙方对合同内容完全理解并自愿接受本合同，"
    "且承诺不再就合同条款提出任何异议或另行主张任何补充权利，"
    "包括工资构成、考勤管理、奖惩办法与合同解除条件"
)


def test_evidence_shorter_than_min_dropped_with_length_diagnostic() -> None:
    """证据归一化后 9 字 < 下限 → 丢弃，诊断里能看出是"长度"原因。"""
    assert len(NINE_CHARS) < MIN_EVIDENCE_CHARS
    llm = StubLLM({"findings": [_finding(evidence=NINE_CHARS)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("长度" in e and "9 字" in e for e in errors)


def test_evidence_longer_than_max_dropped_with_length_diagnostic() -> None:
    """证据归一化后 101 字 > 上限 → 丢弃，诊断里能看出是"长度"原因。"""
    assert len(LONG_EVIDENCE) > MAX_EVIDENCE_CHARS
    llm = StubLLM({"findings": [_finding(evidence=LONG_EVIDENCE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], LONG_CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == (), "超长证据即使能在原文定位，也必须按长度丢弃"
    assert any("长度" in e and "101 字" in e for e in errors)


def test_evidence_84_chars_passes_after_upper_bound_widened() -> None:
    """84 字整行照抄（真实对抗合同段 20）→ 通过；改前上限 80 会把它判成超限。"""
    assert len(EVIDENCE_84) == 84
    span = "甲方有权根据经营需要、项目调整，单方无条件调整乙方工作岗位"
    llm = StubLLM({"findings": [_finding(evidence=EVIDENCE_84)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate(span=span)], EVIDENCE_84 + "\n", errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "A", "84 字在 10–100 区间内，不该降级"
    assert not any("长度" in e for e in errors)


def test_evidence_exactly_100_chars_passes_at_boundary() -> None:
    """正好 100 字（上限本身）→ 通过：区间是闭区间。"""
    assert len(EVIDENCE_100) == 100
    span = "乙方确认甲方已在签订本合同前就全部条款逐条说明并充分协商"
    llm = StubLLM({"findings": [_finding(evidence=EVIDENCE_100)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate(span=span)], EVIDENCE_100 + "\n", errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert not any("长度" in e for e in errors)


def test_evidence_inside_candidate_paragraph_passes() -> None:
    """证据落在候选所在段落内 → 通过（加严后唯一被接受的定位形态）。"""
    llm = StubLLM({"findings": [_finding()]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].paragraph == 1
    assert got[0].span == TRANSFER_SPAN
    assert not any("第 2 道核验" in e for e in errors)


def test_evidence_only_in_another_paragraph_dropped() -> None:
    """证据只在第 2 段出现、候选在第 1 段 → 丢弃，不再退回首次出现。"""
    llm = StubLLM({"findings": [_finding(evidence="乙方月综合薪资 8000 元")]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("段落" in e and "第 1 段" in e for e in errors)


def test_evidence_with_newline_dropped_as_cross_paragraph() -> None:
    """证据把两段拼起来（含换行）→ 丢弃，诊断含"跨段"。"""
    evidence = TRANSFER_SPAN + "\n乙方月综合薪资 8000 元"
    llm = StubLLM({"findings": [_finding(evidence=evidence)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("跨段" in e for e in errors)


def test_evidence_spanning_paragraph_boundary_dropped_as_cross_paragraph() -> None:
    """没有换行，但归一化后把段尾与段首拼成了一个匹配 → 同样按跨段丢弃。"""
    boundary_contract = "甲方有权单方调整乙方工作岗位，乙方不得拒绝\n乙方月综合薪资 8000 元\n"
    evidence = "乙方不得拒绝乙方月综合薪资"
    llm = StubLLM({"findings": [_finding(evidence=evidence)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], boundary_contract, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("跨段" in e for e in errors)


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
    """`_locate` 本身仍是「同段优先、否则退首次出现」；退回首次出现只留给消融臂。

    P2-2 起生产核验路径（`_verify`）另加"必须落在候选所在段落"的硬校验，
    因此这里的回退行为不再出现在用户可见结论里（见 `test_evidence_only_in_another_paragraph_dropped`）。
    """
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

# 第 6 道的两个反例证据：都落在候选段内，只差与候选 span 的重叠比例。
LOW_OVERLAP_EVIDENCE = "甲方有权单方解除劳动合同，无需支付任何经济补偿"  # 重叠 ≈0.3
HIGH_OVERLAP_EVIDENCE = "甲方有权单方调整乙方工作岗位，乙方可以拒绝"  # 重叠 ≈0.8


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


def test_low_overlap_evidence_demotes_one_grade() -> None:
    """证据落在候选段内但与候选片段重叠不足 → 降一级（A→B），span 仍取候选并记诊断。"""
    contract = (
        "甲方有权单方调整乙方工作岗位，乙方不得拒绝。甲方有权单方解除劳动合同，无需支付任何经济补偿。\n"
    )
    llm = StubLLM({"findings": [_finding(evidence=LOW_OVERLAP_EVIDENCE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], contract, errors=errors, statutes=LIBRARY)

    ratio = _overlap_ratio(LOW_OVERLAP_EVIDENCE, TRANSFER_SPAN)
    assert 0.25 <= ratio < 0.35, "本用例的重叠比例应在 0.3 上下，否则断言失去意义"
    assert len(got) == 1
    assert got[0].grade == "B", "重叠不足要降一级，不是只记诊断"
    assert got[0].span == TRANSFER_SPAN, "span 仍取候选片段"
    assert any("重叠不足" in e and "降为 B" in e for e in errors)


def test_low_overlap_from_b_demotes_to_c() -> None:
    """降级逐级走：B 级重叠不足 → C，仍保留结论与候选 span。"""
    contract = (
        "甲方有权单方调整乙方工作岗位，乙方不得拒绝。甲方有权单方解除劳动合同，无需支付任何经济补偿。\n"
    )
    row = _finding(
        grade="B", statute_ids=["LCL-35"], evidence=LOW_OVERLAP_EVIDENCE, need_facts=["是否经协商一致"]
    )
    llm = StubLLM({"findings": [row]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], contract, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "C"
    assert got[0].span == TRANSFER_SPAN
    assert any("重叠不足" in e and "降为 C" in e for e in errors)


def test_low_overlap_at_c_stays_c_with_diagnostic() -> None:
    """已是 C 级 → 保持 C（降级只降不升），诊断照记。"""
    contract = (
        "甲方有权单方调整乙方工作岗位，乙方不得拒绝。甲方有权单方解除劳动合同，无需支付任何经济补偿。\n"
    )
    row = _finding(grade="C", statute_ids=[], quote_statute="", evidence=LOW_OVERLAP_EVIDENCE)
    llm = StubLLM({"findings": [row]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], contract, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "C", "C 级不再往下降，也不能因此升"
    assert got[0].span == TRANSFER_SPAN
    assert any("重叠不足" in e and "保持 C" in e for e in errors)


def test_high_overlap_does_not_demote() -> None:
    """重叠 ≈0.8（≥ OVERLAP_MIN）→ 不降级，等级原样保留。"""
    contract = "甲方有权单方调整乙方工作岗位，乙方不得拒绝。甲方有权单方调整乙方工作岗位，乙方可以拒绝。\n"
    llm = StubLLM({"findings": [_finding(evidence=HIGH_OVERLAP_EVIDENCE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], contract, errors=errors, statutes=LIBRARY)

    ratio = _overlap_ratio(HIGH_OVERLAP_EVIDENCE, TRANSFER_SPAN)
    assert 0.75 <= ratio < 0.85, "本用例的重叠比例应在 0.8 上下，否则断言失去意义"
    assert len(got) == 1
    assert got[0].grade == "A"
    assert not any("重叠不足" in e for e in errors)


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


# ---------- P2-3：payload 增补 rule_checks / fact_sheet ----------


def _check(
    rule_id: str = "R-PROBATION-TERM",
    status: RuleCheckStatus = RuleCheckStatus.MISMATCH,
    actual: str = "12 个月",
    statute_id: str = "LCL-35",
    field: str = "probation_months",
) -> RuleCheck:
    """造一条校验信号；`statute_id` 默认取候选闭集内的 LCL-35，否则会被相关性过滤掉。"""
    return RuleCheck(
        rule_id=rule_id,
        status=status,
        field=field,
        expected="<= 6 个月",
        actual=actual,
        statute_id=statute_id,
        evidence_span="同一用人单位与同一劳动者只能约定一次试用期。",
        severity="hard",
    )


def test_payload_rule_checks_carry_only_decision_relevant_statuses() -> None:
    """只发 mismatch / needs_fact；ok 不发，且字段只留 5 个（不带 evidence_span / statute_id）。"""
    checks = [
        _check("R-MISMATCH", RuleCheckStatus.MISMATCH),
        _check("R-NEEDS-FACT", RuleCheckStatus.NEEDS_FACT, actual=""),
        _check("R-OK", RuleCheckStatus.OK),
    ]
    llm = StubLLM({"findings": []})
    review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY, rule_checks=checks)

    got = llm.payloads[0]["rule_checks"]
    assert [row["rule_id"] for row in got] == ["R-MISMATCH", "R-NEEDS-FACT"]
    assert {row["status"] for row in got} == {"mismatch", "needs_fact"}
    assert set(got[0]) == {"rule_id", "status", "field", "expected", "actual"}
    # 法条原文只在候选的 statutes 段里出现一次：payload 不得出现 evidence_span 的原文
    assert "evidence_span" not in json.dumps(llm.payloads[0], ensure_ascii=False)
    assert "R-OK" not in json.dumps(llm.payloads[0], ensure_ascii=False)


def test_payload_rule_checks_filtered_by_batch_candidate_statutes() -> None:
    """只发与本批候选 `candidate_statute_ids` 并集相关的规则；无关规则不下发（规格 6.1 口径）。

    本批两条候选法条不同（LCL-35/39 与 LCL-26）→ 并集是三者，相关规则各留一条，
    引 SIL-58 的规则不在任何候选的闭集里，必须被过滤掉。
    """
    checks = [
        _check("R-REL-TRANSFER", statute_id="LCL-35"),
        _check("R-REL-SI", statute_id="LCL-26", field="social_insurance"),
        _check("R-UNRELATED", statute_id="SIL-58"),
    ]
    llm = StubLLM({"findings": []})
    review_clauses(
        llm,
        [
            _candidate(),
            _candidate(paragraph=3, span=SI_SPAN, trigger_id="TR-SI-WAIVER", statutes=("LCL-26",)),
        ],
        CONTRACT,
        statutes=LIBRARY,
        rule_checks=checks,
    )

    got = llm.payloads[0]["rule_checks"]
    assert [row["rule_id"] for row in got] == ["R-REL-TRANSFER", "R-REL-SI"]
    assert "R-UNRELATED" not in json.dumps(llm.payloads[0], ensure_ascii=False)
    # 每条候选自己的 statutes 段仍是自己的闭集（候选级闭集不变）
    assert [c["candidate_statute_ids"] for c in llm.payloads[0]["candidates"]] == [
        ["LCL-35", "LCL-39"],
        ["LCL-26"],
    ]


def test_payload_rule_checks_empty_when_no_related_rule() -> None:
    """有校验信号但都与本批候选无关 → 字段是 `[]`（不是缺字段）。"""
    llm = StubLLM({"findings": []})
    review_clauses(
        llm,
        [_candidate()],
        CONTRACT,
        statutes=LIBRARY,
        rule_checks=[_check("R-UNRELATED", statute_id="SIL-58")],
    )
    assert llm.payloads[0]["rule_checks"] == []


def test_payload_rule_checks_are_per_batch_not_per_candidate() -> None:
    """每批都带同一份校验信号（不是挂到单条候选上）。"""
    checks = [_check("R-MISMATCH", RuleCheckStatus.MISMATCH)]
    llm = StubLLM({"findings": []}, {"findings": []})
    review_clauses(
        llm,
        [_candidate(), _candidate(paragraph=3, span=SI_SPAN, trigger_id="TR-SI-WAIVER")],
        CONTRACT,
        batch_size=1,
        statutes=LIBRARY,
        rule_checks=checks,
        facts={"term_years": "3"},
    )

    assert len(llm.payloads) == 2
    for payload in llm.payloads:
        assert payload["rule_checks"] == [
            {
                "rule_id": "R-MISMATCH",
                "status": "mismatch",
                "field": "probation_months",
                "expected": "<= 6 个月",
                "actual": "12 个月",
            }
        ]
        assert payload["fact_sheet"] == {"term_years": "3"}
        assert "rule_checks" not in payload["candidates"][0]
        assert "fact_sheet" not in payload["candidates"][0]


def test_payload_rule_checks_truncated_at_limit_with_diagnostic() -> None:
    """超过 `MAX_RULE_CHECKS` 截断并记诊断；恰好等于上限时不记。"""
    over = [_check(f"R-{i:02d}", RuleCheckStatus.MISMATCH) for i in range(MAX_RULE_CHECKS + 5)]
    errors: list[str] = []
    llm = StubLLM({"findings": []})
    review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY, rule_checks=over)

    assert len(llm.payloads[0]["rule_checks"]) == MAX_RULE_CHECKS
    assert any("已截断" in e for e in errors), errors

    at_limit = over[:MAX_RULE_CHECKS]
    errors_at_limit: list[str] = []
    llm2 = StubLLM({"findings": []})
    review_clauses(
        llm2, [_candidate()], CONTRACT, errors=errors_at_limit, statutes=LIBRARY, rule_checks=at_limit
    )
    assert len(llm2.payloads[0]["rule_checks"]) == MAX_RULE_CHECKS
    assert not any("已截断" in e for e in errors_at_limit), errors_at_limit


def test_payload_rule_checks_empty_when_none_passed() -> None:
    """没有校验信号时字段是 `[]`（不是缺字段，模型不必猜）。"""
    llm = StubLLM({"findings": []})
    review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY)
    assert llm.payloads[0]["rule_checks"] == []
    assert llm.payloads[0]["fact_sheet"] == {}


def test_payload_fact_sheet_skips_empty_values() -> None:
    """只发已填事实：空串 / 纯空白不下发，键名保持字段 id。"""
    llm = StubLLM({"findings": []})
    review_clauses(
        llm,
        [_candidate()],
        CONTRACT,
        statutes=LIBRARY,
        facts={"term_years": "3", "probation_months": "   ", "term_type": "固定期限"},
    )
    assert llm.payloads[0]["fact_sheet"] == {"term_years": "3", "term_type": "固定期限"}


def test_payload_fact_sheet_caps_items_and_value_length() -> None:
    """超过 `MAX_FACTS` 项只取前几项；单值截断到 `MAX_FACT_CHARS`。"""
    long_value = "长" * (MAX_FACT_CHARS + 20)
    facts = {"term_years": long_value, **{f"f{i}": str(i) for i in range(MAX_FACTS + 2)}}
    llm = StubLLM({"findings": []})
    review_clauses(llm, [_candidate()], CONTRACT, statutes=LIBRARY, facts=facts)

    sheet = llm.payloads[0]["fact_sheet"]
    assert len(sheet) == MAX_FACTS
    assert sheet["term_years"] == long_value[:MAX_FACT_CHARS]
    assert len(sheet["term_years"]) == MAX_FACT_CHARS


def test_payload_fact_sheet_prioritises_fields_used_by_rule_checks() -> None:
    """超过 5 项时优先保住本批 rule_checks 涉及的字段（排在第 6 位的 probation_months 不被截掉）。"""
    facts = {
        "f1": "1",
        "f2": "2",
        "f3": "3",
        "f4": "4",
        "f5": "5",
        "probation_months": "12",
    }
    llm = StubLLM({"findings": []})
    review_clauses(
        llm,
        [_candidate()],
        CONTRACT,
        statutes=LIBRARY,
        rule_checks=[_check("R-PROBATION-TERM", field="probation_months", statute_id="LCL-35")],
        facts=facts,
    )

    sheet = llm.payloads[0]["fact_sheet"]
    assert list(sheet) == ["probation_months", "f1", "f2", "f3", "f4"]
    assert len(sheet) == MAX_FACTS

    # 对照：没有相关 rule_checks 时按原顺序截断，排在第 6 位的 probation_months 被丢掉
    llm2 = StubLLM({"findings": []})
    review_clauses(llm2, [_candidate()], CONTRACT, statutes=LIBRARY, facts=facts)
    assert "probation_months" not in llm2.payloads[0]["fact_sheet"]


PRIVACY_LINES = [
    "甲方：某某科技有限公司，统一社会信用代码 914301MA00XXXXXX。",
    "乙方：张三，身份证号码 4301221998XXXX2517。",
    "第一条 本合同为固定期限劳动合同。",
    "第二条 乙方岗位为算法助理。",
    "第三条 甲方有权单方调整乙方工作岗位，乙方不得拒绝。",
    "第四条 乙方月工资 8000 元。",
    "第五条 乙方自愿放弃缴纳社会保险。",
    "第六条 本合同一式两份。",
]
PRIVACY_CONTRACT = "\n".join(PRIVACY_LINES) + "\n"


def test_payload_privacy_keeps_candidate_plus_minus_two_only() -> None:
    """外发范围不变：只发候选 ±2 段；rule_checks / fact_sheet 不夹带任何合同正文。"""
    llm = StubLLM({"findings": []})
    review_clauses(
        llm,
        [_candidate(paragraph=5)],
        PRIVACY_CONTRACT,
        statutes=LIBRARY,
        rule_checks=[_check("R-MISMATCH", RuleCheckStatus.MISMATCH)],
        facts={"term_years": "3"},
    )

    payload = llm.payloads[0]
    paragraphs = [int(line.split(".")[0]) for line in payload["contract_paragraphs"]]
    assert paragraphs == [3, 4, 5, 6, 7]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert payload["contract_paragraphs"][0].endswith(PRIVACY_LINES[2])
    assert "身份证号码" not in dumped
    assert "某某科技" not in dumped
    assert "4301221998XXXX2517" not in dumped
    assert PRIVACY_CONTRACT not in dumped


def test_review_system_explains_new_fields_without_making_rules_judge() -> None:
    """prompt 说明两个新字段的用途，且明确它们只作参考（不写"必须服从"）。"""
    assert "rule_checks" in REVIEW_SYSTEM
    assert "mismatch" in REVIEW_SYSTEM
    assert "fact_sheet" in REVIEW_SYSTEM
    assert "只作判断参考" in REVIEW_SYSTEM
    assert "不得直接复述为结论" in REVIEW_SYSTEM
    assert "必须服从" not in REVIEW_SYSTEM


def test_payload_direct_call_defaults_to_empty_sections() -> None:
    """`_payload` 直调（评测脚本等）不传新参数时两段仍存在且为空。"""
    payload = _payload([_candidate()], CONTRACT, LIBRARY, 3)
    assert payload["rule_checks"] == []
    assert payload["fact_sheet"] == {}


def test_verify_directly_rejects_without_candidate_set() -> None:
    """`_verify` 可独立调用：自提结论不检查候选集，但等级强制 C。"""
    errors: list[str] = []
    finding = _verify(
        _finding(grade="B", statute_ids=["LCL-26"], evidence=SI_SPAN),
        None,
        CONTRACT,
        LIBRARY,
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


class _CapturingWiringLLM(_WiringLLM):
    """记录每次 payload 的接线用假模型：不产出结论，只看服务层传下了什么。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.payloads.append(json.loads(user))
        return {"findings": [], "extra": []}


def _run_check_with(
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
    enabled: bool,
    llm: Any = None,
    facts: dict[str, str] | None = None,
):
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
        facts=facts,
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


def test_run_check_feeds_rule_checks_and_facts_into_payload(monkeypatch) -> None:
    """接线：图跑完后的 `rule_checks` 与 `facts` 必须进条款判定 payload（P2-3）。

    用"试用期期限"条款而不是 `CONTRACT`：003 第 5.3 节删掉 4 条规则后，`CONTRACT` 里的
    社保放弃条款已无对应规则（SIL-58 / SIL-86 两条随之下线），而 `TR-PROBATION-TERM-EXCEED`
    的候选法条含 LCL-19，与 `R-PROBATION-*` 的 `statute_id` 对齐，接线仍有信号可验。
    """
    llm = _CapturingWiringLLM()
    _run_check_with(
        monkeypatch,
        "乙方试用期为六个月。\n",
        enabled=True,
        llm=llm,
        facts={"term_years": "1"},
    )

    assert llm.payloads, "应当发起至少一次条款判定"
    # 同一次 run_check 里还有处理建议等其它模型调用，只挑条款判定那一批
    payload = next(p for p in llm.payloads if "candidates" in p)
    assert payload["rule_checks"], "规则校验信号应随 payload 下发"
    assert all(row["status"] in {"mismatch", "needs_fact"} for row in payload["rule_checks"])
    assert payload["fact_sheet"]["term_years"] == "1"


# ---------- 落库 ----------


def test_clause_findings_persist_roundtrip(tmp_path) -> None:
    from mla.adapters.persist import SqliteRepository
    from mla.config import Paths, data_path
    from mla.domain.models import CheckTask, TaskMode

    repo = SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
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
