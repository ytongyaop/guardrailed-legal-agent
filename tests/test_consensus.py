"""共识层单测（规格 003 第 5.4 节 / 第 13 章 P1-2）：7 种情形逐条覆盖。

用例全部构造数据、零模型、零 IO：`reconcile` 是纯函数，本阶段不接线，
所以这里不碰 graph / tasks / 库，只断言矩阵的处置与文案。

对齐口径固定为 `statute_ids ∩ statute_id`；等级只降不升（A→B→C，C 保持 C）。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from mla.domain.models import ClauseFinding
from mla.domain.rules import RuleCheck, RuleCheckStatus
from mla.services.consensus import (
    GRADES,
    NOTE_ANNOTATE,
    NOTE_CONFLICT,
    NOTE_CONSISTENT,
    NOTE_CONTRADICT,
    NOTE_GRADE_FLOOR,
    NOTE_NEEDS_FACT,
    NOTE_NO_CHECK,
    NOTE_PREMISE,
    VERDICT_ANNOTATE,
    VERDICT_DEMOTE,
    VERDICT_KEEP,
    VERDICT_REVIEW_REQUIRED,
    ConsensusRow,
    reconcile,
)

# 「模型指出该数值点有问题」的理由：不含任何合规表述，用于表 1 / 表 3 之外的对照
CONFLICT_REASON = "试用期长度与第十九条的档位不一致"
# 「模型说合规」的理由：命中 _COMPLIANT_MARKS
COMPLIANT_REASON = "该条约定符合规定，未发现问题"


def make_finding(
    grade: str = "A",
    statute_ids: tuple[str, ...] = ("LCL-19",),
    paragraph: int = 3,
    reason: str = CONFLICT_REASON,
    span: str = "试用期三个月",
    trigger_id: str = "TR-PROBATION-TERM-EXCEED",
) -> ClauseFinding:
    return ClauseFinding(
        paragraph=paragraph,
        span=span,
        grade=grade,
        reason=reason,
        statute_ids=statute_ids,
        trigger_id=trigger_id,
        source="trigger" if trigger_id else "model_extra",
    )


def make_check(
    status: RuleCheckStatus = RuleCheckStatus.MISMATCH,
    statute_id: str = "LCL-19",
    actual: str = "3 个月",
    severity: str = "hard",
    rule_id: str = "R-PROBATION-6M",
    field: str = "probation_months",
    expected: str = "<= 6 个月",
) -> RuleCheck:
    return RuleCheck(
        rule_id=rule_id,
        status=status,
        field=field,
        expected=expected,
        actual=actual,
        statute_id=statute_id,
        evidence_span="试用期不得超过六个月",
        severity=severity,
    )


def _rank(grade: str) -> int:
    return GRADES.index(grade)


def _only_row(rows: Sequence[ConsensusRow]) -> ConsensusRow:
    assert len(rows) == 1, f"期望一行，实际 {len(rows)} 行：{[r.verdict for r in rows]}"
    return rows[0]


# ---------- 表 1：mismatch + 模型已指向同一数值点 → keep ----------


def test_mismatch_with_aligned_finding_keeps_grade() -> None:
    """表 1：findings.statute_ids 含该 statute_id，说明模型已指向同一数值点。"""
    finding = make_finding(grade="A")
    rows = reconcile([finding], [make_check()])

    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.finding is not None and row.check is not None
    assert row.finding.grade == "A"  # 不升不降
    assert row.finding.reason == CONFLICT_REASON
    assert row.note == NOTE_CONSISTENT


@pytest.mark.parametrize("grade", GRADES)
def test_mismatch_with_aligned_finding_keeps_every_grade(grade: str) -> None:
    """表 1 对 A/B/C 三档都成立：一致就保留，不因校验状态改动等级。"""
    rows = reconcile([make_finding(grade=grade)], [make_check()])
    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.finding is not None and row.finding.grade == grade


# ---------- 表 2：mismatch + 没有任何 finding 关联 → 追加 C 级复核条目 ----------


def test_mismatch_without_any_finding_appends_review_entry() -> None:
    """表 2：追加条目必须是内部 C 级、程序来源、带该条 statute_id，且不冒充合同原文。"""
    rows = reconcile([], [make_check(actual="3 个月")])

    row = _only_row(rows)
    assert row.verdict == VERDICT_REVIEW_REQUIRED
    assert row.check is not None
    entry = row.finding
    assert entry is not None
    assert entry.grade == "C"
    assert entry.trigger_id == ""  # 空串 = 不是触发器条目，也不是模型自提
    assert entry.source == "rule_check"
    assert entry.statute_ids == ("LCL-19",)
    assert "第 19 条" in entry.reason
    assert "需人工复核" in entry.reason
    assert "风险" not in entry.reason  # 不写"这是风险"的定性
    assert entry.paragraph == 0  # 程序追加条目没有合同段落（哨兵值）
    assert entry.span == ""  # 不拿法条原文冒充合同原文子串
    assert "第 19 条" in row.note and "需人工复核" in row.note


def test_mismatch_unrelated_to_other_findings_still_appends_review_entry() -> None:
    """表 2 的另一半：有 finding、但都没关联该 statute，仍要追加复核条目。"""
    other = make_finding(statute_ids=("LCL-39",))
    rows = reconcile([other], [make_check(statute_id="LCL-19")])

    assert [r.verdict for r in rows] == [VERDICT_REVIEW_REQUIRED, VERDICT_KEEP]
    # statute_id 升序：LCL-19 的复核条目排在 LCL-39 的保留行之前
    assert rows[0].check is not None and rows[0].check.statute_id == "LCL-19"
    assert rows[1].finding is not None and rows[1].finding.statute_ids == ("LCL-39",)
    assert rows[1].note == NOTE_NO_CHECK


# ---------- 表 3：mismatch + 模型说合规 → 降一级 ----------


@pytest.mark.parametrize(("before", "after"), [("A", "B"), ("B", "C"), ("C", "C")])
def test_mismatch_with_compliant_claim_demotes_one_grade(before: str, after: str) -> None:
    """表 3：方向相反时降一级；C 已是最低档，降不动就保持 C（但仍记 demote）。"""
    rows = reconcile([make_finding(grade=before, reason=COMPLIANT_REASON)], [make_check()])

    row = _only_row(rows)
    assert row.verdict == VERDICT_DEMOTE
    assert row.finding is not None and row.finding.grade == after
    assert row.note == NOTE_CONFLICT


def test_mismatch_with_blank_grade_is_treated_as_compliant_claim() -> None:
    """表 3 的另一条判据：`grade` 为空（模型没给等级）也算"说合规"，统一落到 C 档。"""
    rows = reconcile([make_finding(grade="", reason="未发现问题")], [make_check()])

    row = _only_row(rows)
    assert row.verdict == VERDICT_DEMOTE
    assert row.finding is not None and row.finding.grade == "C"


def test_demotion_never_upgrades_grade() -> None:
    """任何降级路径都不得升等级：降完的序号必须不小于入参。"""
    for grade in GRADES:
        demoted = _only_row(reconcile([make_finding(grade=grade, reason=COMPLIANT_REASON)], [make_check()]))
        assert demoted.finding is not None
        assert _rank(demoted.finding.grade) >= _rank(grade)


# ---------- 表 4：ok 且 actual 非空 + 模型 A/B → 降一级 ----------


def test_ok_with_actual_demotes_model_finding() -> None:
    """表 4：数值落在法条区间内，模型却说 A —— 降一级，并记"区间矛盾"。"""
    rows = reconcile(
        [make_finding(grade="A")],
        [make_check(status=RuleCheckStatus.OK, actual="2 个月")],
    )

    row = _only_row(rows)
    assert row.verdict == VERDICT_DEMOTE
    assert row.finding is not None and row.finding.grade == "B"
    assert row.note == NOTE_CONTRADICT


def test_ok_with_actual_keeps_c_grade() -> None:
    """表 4 只对 A/B 生效：C 已是最低档，降不动，保持 C 并记"等级已为最低"。"""
    rows = reconcile(
        [make_finding(grade="C")],
        [make_check(status=RuleCheckStatus.OK, actual="2 个月")],
    )

    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.finding is not None and row.finding.grade == "C"
    assert row.note == NOTE_GRADE_FLOOR


# ---------- 表 5：ok 且 actual 为空（前提不满足）→ 绝不降级 ----------


@pytest.mark.parametrize("grade", GRADES)
def test_ok_with_empty_actual_never_demotes(grade: str) -> None:
    """表 5（防误伤守卫）：`not_applicable` 映射成 ok 且 actual 为空，此时不得降级。

    这是最容易写错的一格：把"前提不满足"当成"数值合规"，会把本该保留的结论降错级。
    """
    rows = reconcile(
        [make_finding(grade=grade, reason=COMPLIANT_REASON)],
        [make_check(status=RuleCheckStatus.OK, actual="")],
    )

    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.finding is not None and row.finding.grade == grade  # 原样
    assert row.note == NOTE_PREMISE


def test_ok_with_empty_actual_and_no_finding_emits_nothing() -> None:
    """表 5 的另一面：没有模型结论时不为"前提不满足"凭空造条目。"""
    assert reconcile([], [make_check(status=RuleCheckStatus.OK, actual="")]) == ()


# ---------- 表 6：needs_fact → keep，field 留给追问层 ----------


def test_needs_fact_keeps_finding_and_exposes_field() -> None:
    """表 6：缺事实不出结论，但 `check.field` 必须能拿到（追问层复用，本阶段不接线）。"""
    rows = reconcile(
        [make_finding(grade="A")],
        [make_check(status=RuleCheckStatus.NEEDS_FACT, actual="", field="probation_times")],
    )

    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.finding is not None and row.finding.grade == "A"
    assert row.note == NOTE_NEEDS_FACT
    assert row.check is not None and row.check.field == "probation_times"


# ---------- 表 7：severity="prompt" 且 mismatch → 只记附注 ----------


def test_prompt_mismatch_only_annotates_without_touching_grade() -> None:
    """表 7：提示级校验不一致只记附注，不改等级、不生成条目。"""
    rows = reconcile(
        [make_finding(grade="A")],
        [make_check(severity="prompt")],
    )

    row = _only_row(rows)
    assert row.verdict == VERDICT_ANNOTATE
    assert row.finding is not None and row.finding.grade == "A"
    assert row.note == NOTE_ANNOTATE


def test_prompt_mismatch_without_finding_creates_no_entry() -> None:
    """表 7 的另一面：没有模型结论时也不追加复核条目（只留一行附注）。"""
    rows = reconcile([], [make_check(severity="prompt")])

    row = _only_row(rows)
    assert row.verdict == VERDICT_ANNOTATE
    assert row.finding is None
    assert row.note == NOTE_ANNOTATE


# ---------- 未关联任何校验的 finding 不能丢 ----------


def test_finding_without_any_check_is_kept() -> None:
    """没有匹配的 check 时仍要返回该 finding，否则模型结论会被对账层静默丢掉。"""
    finding = make_finding(grade="A", statute_ids=("LCL-39",))
    rows = reconcile([finding], [])

    row = _only_row(rows)
    assert row.verdict == VERDICT_KEEP
    assert row.check is None
    assert row.finding is not None and row.finding is finding
    assert row.note == NOTE_NO_CHECK


# ---------- 交叉配对：按 statute_id 对齐、无重复 ----------


def test_cross_pairing_aligns_by_statute_id_without_duplicates() -> None:
    """多条 finding × 多条 check：只配对 statute_id 相同的组合，且不产生重复行。"""
    f19 = make_finding(grade="A", statute_ids=("LCL-19",), paragraph=3, span="试用期三个月")
    f39 = make_finding(grade="B", statute_ids=("LCL-39",), paragraph=5, span="每周工作六天")
    f_both = make_finding(grade="B", statute_ids=("LCL-19", "LCL-39"), paragraph=4, span="综合约定")

    check_19 = make_check(statute_id="LCL-19")
    check_39 = make_check(
        status=RuleCheckStatus.OK,
        statute_id="LCL-39",
        actual="2 个月",
        rule_id="R-NONCOMPETE-TERM-2Y",
        field="noncompete_months",
        expected="<= 2 年",
    )

    rows = reconcile([f19, f39, f_both], [check_19, check_39])

    # 每一行都必须满足对齐前提（复核判据本身，不是复核实现）
    for row in rows:
        assert row.check is not None
        assert row.finding is not None
        assert row.check.statute_id in row.finding.statute_ids

    # 无重复：同一 (finding, check) 只出现一次
    assert len(set(rows)) == len(rows)

    # statute_id 升序：全部 LCL-19 的行排在 LCL-39 的行之前
    statutes = [row.check.statute_id for row in rows if row.check is not None]
    assert statutes == sorted(statutes)

    # 配对总数：f19 与 check_19、f_both 与两条 check、f39 与 check_39 → 共 4 行
    assert len(rows) == 4

    f_both_rows = [
        row for row in rows if row.finding is not None and row.finding.statute_ids == ("LCL-19", "LCL-39")
    ]
    assert {row.check.statute_id for row in f_both_rows if row.check} == {"LCL-19", "LCL-39"}
    assert {row.verdict for row in f_both_rows} == {VERDICT_KEEP, VERDICT_DEMOTE}


def test_duplicate_check_input_is_deduped() -> None:
    """输入里同一条 check 出现两次，也只出一行（对账结果不因重复输入翻倍）。"""
    check = make_check()
    rows = reconcile([make_finding()], [check, check])
    assert len(rows) == 1


# ---------- 排序与幂等 ----------


def test_sorting_is_stable_for_same_input() -> None:
    """同一输入两次调用逐字段相同（幂等），且顺序符合 statute_id → 段落号 → 等级。"""
    findings = [
        make_finding(grade="C", statute_ids=("LCL-39",), paragraph=9, span="乙"),
        make_finding(grade="A", statute_ids=("LCL-19",), paragraph=9, span="甲"),
        make_finding(grade="B", statute_ids=("LCL-19",), paragraph=2, span="丙"),
    ]
    checks = [
        make_check(statute_id="LCL-39", status=RuleCheckStatus.NEEDS_FACT, actual=""),
        make_check(statute_id="LCL-19"),
    ]

    first = reconcile(findings, checks)
    second = reconcile(findings, checks)
    assert first == second

    keys = [
        (row.check.statute_id, row.finding.paragraph)
        for row in first
        if row.check is not None and row.finding is not None
    ]
    assert keys == sorted(keys)
    # 同 statute、同段落内按等级：A 在 C 之前
    same_paragraph = [
        row.finding.grade for row in first if row.finding is not None and row.finding.paragraph == 9
    ]
    assert same_paragraph == ["A", "C"]


def test_empty_input_returns_empty_tuple() -> None:
    """空输入返回空元组（不是空列表、不是 None）。"""
    assert reconcile([], []) == ()


def test_output_is_tuple_of_consensus_rows() -> None:
    """返回类型固定为 tuple[ConsensusRow, ...]，下游可直接解构。"""
    rows = reconcile([make_finding()], [make_check()])
    assert isinstance(rows, tuple)
    assert all(isinstance(row, ConsensusRow) for row in rows)
