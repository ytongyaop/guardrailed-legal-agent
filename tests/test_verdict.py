"""结论形态（Verdict）测试：把“0 条条目”翻成有语义的结论，边界逐条锁住。

覆盖三件事：有条目就报 risk；没条目但通道没跑全 / 判不动只能算 partial；
三个通道都跑过、都判得动才算 clean。正文过短要单独留一行提示。
"""

from __future__ import annotations

from mla.domain.models import (
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    TaskMode,
    UndeterminedRule,
)
from mla.domain.verdict import Verdict, summarize_verdict

COVERAGE = RuleCoverage(total=20, violated=1, compliant=8, not_applicable=8, unknown=3)


def _item() -> CheckItem:
    return CheckItem(
        item_id="R-PROBATION-6M",
        kind=ItemKind.RISK,
        clause_ref="第十九条",
        finding="试用期约定超过法定上限",
        source_id="LCL-19",
        rule_id="R-PROBATION-6M",
    )


def _term(term_id: str, status: MissingStatus) -> MissingTerm:
    return MissingTerm(
        term_id=term_id,
        name=f"第{term_id}类",
        clause_no="（一）",
        statute_id="LCL-17",
        status=status,
        certainty="高",
    )


def _undetermined() -> UndeterminedRule:
    return UndeterminedRule(
        rule_id="R-SI-REGISTER-30D",
        name="应自用工之日起 30 日内办理社会保险登记",
        statute_id="SIL-58",
        missing_fields=("days_to_si_register",),
        reason="field_missing",
    )


def _finding(grade: str) -> ClauseFinding:
    """一条条款审查结论；等级决定它算不算“冲突”、要不要挡住 clean。"""
    return ClauseFinding(
        paragraph=3,
        span="试用期工资为转正工资的 60%。",
        grade=grade,
        reason="该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符",
        statute_ids=("LCL-20",),
        trigger_id="TR-PROBATION-WAGE-BASE",
    )


def _task(**kwargs) -> CheckTask:
    """默认是一份“正文够长、规则层跑过”的核对任务，各用例只改自己关心的字段。"""
    base = {"task_id": "T-VERDICT", "mode": TaskMode.CHECK, "contract_chars": 1200}
    base.update(kwargs)
    return CheckTask(**base)


def _all_written() -> list[MissingTerm]:
    """八类都写了 + 第（九）类兜底（兜底本来就不判，状态恒为需人工确认）。"""
    rows = [_term(f"MT-{i}", MissingStatus.WRITTEN) for i in range(1, 9)]
    rows.append(_term("MT-9", MissingStatus.MANUAL))
    return rows


def test_items_make_risk_verdict() -> None:
    """有 items 就是 risk：条数照实报，不许被别的东西稀释。"""
    task = _task(items=[_item()], rule_coverage=COVERAGE)
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 1
    assert verdict.headline == "发现 1 条与法条存在冲突的事项"


def test_unreviewed_clauses_with_undetermined_is_partial() -> None:
    """条款审查没跑 + 有规则缺事实判不了：只能算 partial，不能说没问题。"""
    task = _task(
        rule_coverage=COVERAGE,
        undetermined=[_undetermined()],
        missing_terms=[_term("MT-3", MissingStatus.WRITTEN)],
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert verdict.risk_count == 0
    assert verdict.headline == "未发现硬性风险，但覆盖不完整"
    assert verdict.details == [
        "规则 17/20 可判定",
        "条款审查未运行",
        "1 条规则因缺少事实无法判定",
        "必备条款 1/8 已写",
    ]


def test_fully_covered_is_clean() -> None:
    """三个通道都跑过、都判得动，才允许说“未发现风险”。"""
    task = _task(
        clause_review=True,
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=12, not_applicable=8, unknown=0),
        missing_terms=_all_written(),
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "clean"
    assert verdict.risk_count == 0
    assert verdict.headline == "未发现风险"
    assert verdict.details == [
        "规则 20/20 可判定",
        "条款审查已运行",
        "必备条款 8/8 已写",
    ]


def test_short_contract_notes_incomplete_coverage() -> None:
    """正文过短：必备条款整体落回“需人工确认”，只能 partial + 一行提醒。"""
    task = _task(
        clause_review=True,
        contract_chars=120,
        missing_terms=[_term(f"MT-{i}", MissingStatus.MANUAL) for i in range(1, 10)],
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert "合同正文较短，结论可能不完整" in verdict.details
    # 正文太短时不做“0/8 已写”这种断言：一类都没判过，不能读成“八类全没写”。
    assert "必备条款 0/8 已写" not in verdict.details


def test_missing_review_failure_is_not_clean() -> None:
    """有“已忽略 / 失败”诊断：审查算没完成，不能落到 clean。"""
    task = _task(
        clause_review=True,
        contract_chars=1200,
        errors=["第 1 批条款判定调用失败：超时"],
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert "条款审查未完成" in verdict.details
    assert "条款审查已运行" not in verdict.details


def test_unconfigured_llm_says_not_run() -> None:
    """没配模型时审查根本没启动：诊断里写了“未启用”，也不能说成“未完成”。"""
    task = _task(errors=["条款审查未启用：未配置大模型"])
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert "条款审查未运行" in verdict.details
    assert "条款审查未完成" not in verdict.details


def test_manual_missing_term_blocks_clean() -> None:
    """必备条款里有“需人工确认”的类（第（九）类除外）也不能算 clean。"""
    task = _task(
        clause_review=True,
        missing_terms=[_term("MT-3", MissingStatus.WRITTEN), _term("MT-8", MissingStatus.MANUAL)],
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert "1 类需人工确认" in verdict.details


def test_grade_a_finding_alone_is_risk() -> None:
    """规则层 0 条、条款审查有 A 级：仍是 risk，不能被判成 clean。"""
    task = _task(clause_review=True, clause_findings=[_finding("A")])
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 1
    assert verdict.headline == "发现 1 条与法条存在冲突的事项"
    assert "条款审查发现 1 条明确冲突或显著失衡" in verdict.details


def test_grade_b_finding_alone_is_risk() -> None:
    """B 级（显著失衡）同样算“与法条存在冲突的事项”。"""
    task = _task(clause_review=True, clause_findings=[_finding("B")])
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 1
    assert "条款审查发现 1 条明确冲突或显著失衡" in verdict.details


def test_grade_c_finding_is_partial_never_clean() -> None:
    """C 级是“待确认”：不算冲突，但绝不能落到 clean。"""
    task = _task(clause_review=True, clause_findings=[_finding("C")])
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert verdict.risk_count == 0
    assert verdict.headline == "未发现硬性风险，但覆盖不完整"
    assert "1 条条款待人工确认" in verdict.details


def test_rules_and_clause_findings_add_up() -> None:
    """两个通道都算“冲突事项”：条数相加，不互相吞掉。"""
    task = _task(clause_review=True, items=[_item()], clause_findings=[_finding("A")])
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 2
    assert verdict.headline == "发现 2 条与法条存在冲突的事项"


def test_no_findings_with_full_coverage_still_clean() -> None:
    """条款审查跑了但一条结论都没有（clause_findings 为空）：覆盖齐了照样 clean。"""
    task = _task(
        clause_review=True,
        clause_findings=[],
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=12, not_applicable=8, unknown=0),
        missing_terms=_all_written(),
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "clean"
    assert verdict.headline == "未发现风险"


# ---------- 模型主导路径（P1-3）：冲突与待复核分开计数 ----------


def _consensus_item(item_id: str, grade: str, *, source: str = "trigger") -> CheckItem:
    """共识层产出的条目：`grade` 是模型结论等级，`source=rule_check` 为程序追加的复核条目。"""
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK if grade in ("A", "B") else ItemKind.UNKNOWN,
        clause_ref="4",
        finding=f"{item_id} 的结论",
        source_id="LCL-19",
        grade=grade,
        source=source,
        statute_ids=("LCL-19",),
    )


def test_verdict_review_count_defaults_to_zero() -> None:
    """新增字段默认 0：旧调用方只读 risk_count / status / headline / details，行为不变。"""
    verdict = Verdict(status="partial", risk_count=0, headline="未发现硬性风险，但覆盖不完整", details=[])
    assert verdict.review_count == 0


def test_new_path_a_b_count_as_conflict_c_stays_review() -> None:
    """新路径 2 条 A（模型结论）+ 3 条 C（程序复核）：headline 只说 2 条冲突，另报 3 条需人工复核。"""
    task = _task(
        model_first=True,
        clause_review=True,
        items=[
            _consensus_item("CF-1", "A"),
            _consensus_item("CF-2", "A"),
            _consensus_item("RC-1", "C", source="rule_check"),
            _consensus_item("RC-2", "C", source="rule_check"),
            _consensus_item("RC-3", "C", source="rule_check"),
        ],
        rule_coverage=COVERAGE,
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 2
    assert verdict.review_count == 3
    assert verdict.headline == "发现 2 条与法条存在冲突的事项"
    assert "5 条" not in verdict.headline
    assert "3 条需人工复核" in verdict.details


def test_new_path_only_c_entries_is_partial_never_clean() -> None:
    """只有 C 级条目（哪怕三个通道都跑齐）：不能说“未发现风险”，也不能说成冲突。"""
    task = _task(
        model_first=True,
        clause_review=True,
        items=[
            _consensus_item("RC-1", "C", source="rule_check"),
            _consensus_item("RC-2", "C", source="rule_check"),
            _consensus_item("RC-3", "C", source="rule_check"),
        ],
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=12, not_applicable=8, unknown=0),
        missing_terms=_all_written(),
    )
    verdict = summarize_verdict(task)

    assert verdict.status == "partial"
    assert verdict.risk_count == 0
    assert verdict.review_count == 3
    assert verdict.headline == "未发现明确冲突，但有 3 条需人工复核"
    assert "3 条需人工复核" in verdict.details


def test_new_path_c_clause_finding_not_reported_twice() -> None:
    """新路径的 C 级条款结论已随 items 计一次：不再额外输出“条条款待人工确认”那一行。"""
    task = _task(
        model_first=True,
        clause_review=True,
        items=[_consensus_item("CF-1", "C")],
        clause_findings=[_finding("C")],
    )
    verdict = summarize_verdict(task)

    assert verdict.review_count == 1
    assert verdict.details == ["条款审查已运行", "1 条需人工复核"]
    assert [row for row in verdict.details if "条款待人工确认" in row] == []


def test_old_path_rule_items_stay_conflicts_without_review() -> None:
    """旧路径守卫：规则条目仍全部计冲突，review_count 恒为 0（行为一个字不变）。"""
    task = _task(items=[_item() for _ in range(5)], rule_coverage=COVERAGE)
    verdict = summarize_verdict(task)

    assert verdict.status == "risk"
    assert verdict.risk_count == 5
    assert verdict.review_count == 0
    assert verdict.headline == "发现 5 条与法条存在冲突的事项"
