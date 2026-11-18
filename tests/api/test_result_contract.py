"""结果 DTO 契约（规格 006 §2「结果 DTO」+ 005 三层归属）。

这里不重跑模型：用一份手工构造的“丰富任务”直接驱动 `presenters.task_detail`，逐个字段断言
契约本身。重点三件事：

1. **过程开关是条件返回**，不是“返回了再藏”：关闭时 `process=None`、`process_details=[]`、
   每条 `FindingView.process=None`；第一层（条号/建议/限制/必备条款计数）与第二层
   （法条原文/事实/类案）一个都不少。
2. **DTO 里没有一处前端可补算的空白**：影响度/确定性/排序/合并都来自领域函数，页面要的分组
   信息在 `impact`/`certainty` 里一次给全。
3. **不泄露内部 ID 作为展示文案**：`StatuteView.label` 永远是可读法名条号（查不到给固定文案），
   内部 ID 只留在 `id` 字段里做 key；限制提示行里也不出现 `LCL-19` 这类编号。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from mla.adapters.persist import SqliteRepository
from mla.api import presenters
from mla.api.routes import settings as settings_routes
from mla.domain.grading import display_of, sort_items
from mla.domain.models import (
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    Severity,
    Suggestion,
    TaskMode,
    UndeterminedRule,
    UnmetTerm,
)
from mla.domain.verdict import summarize_verdict

pytestmark = pytest.mark.no_llm


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SqliteRepository]:
    instance = SqliteRepository(db_path=str(tmp_path / "contract.sqlite3"))
    yield instance
    instance.close()


def _rule_item(item_id: str, finding: str, statute_id: str, rule_id: str) -> CheckItem:
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK,
        clause_ref="",
        finding=finding,
        source_id=statute_id,
        severity=Severity.HARD,
        rule_id=rule_id,
    )


def _rich_task() -> CheckTask:
    """覆盖全部 DTO 分支：规则条目 + 条款 A/C + 必备条款四态 + 缺事实 + 未约定 + 建议。"""
    return CheckTask(
        task_id="T-RICH",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        contract_id="C-1",
        contract_name="演示合同.txt",
        contract_chars=1200,
        clause_review=True,
        items=[
            _rule_item("I-1", "试用期约定超过法定上限", "LCL-19", "R-PROBATION-6M"),
            _rule_item("I-2", "竞业限制期限超过两年", "LCL-23", "R-NONCOMPETE-TERM-2Y"),
        ],
        clause_findings=[
            ClauseFinding(
                paragraph=3,
                span="试用期工资为转正工资的 60%。" * 6,
                grade="A",
                reason="该约定与第二十条关于试用期工资的规定不符",
                statute_ids=("LCL-20",),
                trigger_id="TR-PROBATION-WAGE-BASE",
                need_facts=("probation_wage",),
            ),
            ClauseFinding(
                paragraph=5,
                span="竞业限制期限为五年。",
                grade="C",
                reason="竞业限制期限是否超过两年需人工确认",
                statute_ids=("LCL-23",),
                trigger_id="TR-NONCOMPETE-TERM",
            ),
        ],
        missing_terms=[
            MissingTerm(
                term_id="MT-1",
                name="劳动合同期限",
                clause_no="（一）",
                statute_id="LCL-17",
                status=MissingStatus.WRITTEN,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-2",
                name="工作内容和工作地点",
                clause_no="（二）",
                statute_id="LCL-17",
                status=MissingStatus.ABSENT,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-3",
                name="劳动报酬",
                clause_no="（六）",
                statute_id="LCL-17",
                status=MissingStatus.UNSEEN,
                certainty="低",
            ),
            MissingTerm(
                term_id="MT-9",
                name="法律、法规规定应当纳入劳动合同的其他事项",
                clause_no="（九）",
                statute_id="LCL-17",
                status=MissingStatus.MANUAL,
                certainty="低",
            ),
        ],
        undetermined=[
            UndeterminedRule(
                rule_id="R-PROBATION-ONCE",
                name="同一用人单位与同一劳动者只能约定一次试用期",
                statute_id="LCL-19",
                missing_fields=("probation_times",),
                reason="field_missing",
            ),
            UndeterminedRule(
                rule_id="R-OT-MONTH-36H",
                name="每月加班不得超过三十六小时",
                statute_id="LL-41",
                missing_fields=("working_hours", "monthly_overtime_hours"),
                reason="premise_missing",
            ),
        ],
        unmet=[
            UnmetTerm(
                rule_id="R-WPR-MONTHLY-PAY",
                name="工资至少每月支付一次",
                statute_id="WPR-7",
                fields=("pay_times_per_month",),
            )
        ],
        statutory_na=3,
        rule_coverage=RuleCoverage(total=20, violated=2, compliant=15, not_applicable=1, unknown=2),
        suggestions=[Suggestion(text="把试用期调整到法定上限内。", source_ids=("LCL-19",))],
        facts={"employer_type": "有限责任公司", "term_years": "2.00"},
        latency_s=None,  # 旧记录缺耗时：必须保持 null，不伪造 0
        token_in=120,
        token_out=60,
        ask_rounds=1,
        questions=["试用期工资是多少？"],
        ask_items=[{"slot_id": "probation_wage", "question": "试用期工资是多少？", "source": "fixed"}],
        verify_ok=True,
        created_at=datetime(2026, 1, 5, 9, 0, 0),
    )


def _model_first_task() -> CheckTask:
    """模型主导路径：条目由共识层产出，DTO 必须按 `sort_items` 的顺序原样给出。"""
    items = [
        CheckItem(
            item_id="CF-9",
            kind=ItemKind.UNKNOWN,
            clause_ref="9",
            finding="竞业限制未约定补偿",
            source_id="LCL-23",
            evidence_text="竞业限制期限为三年。",
            grade="C",
            source="trigger",
            statute_ids=("LCL-23",),
            element_id="EL-9",
        ),
        CheckItem(
            item_id="CF-5",
            kind=ItemKind.RISK,
            clause_ref="5",
            finding="试用期超过法定上限",
            source_id="LCL-19",
            evidence_text="试用期六个月。",
            grade="A",
            source="trigger",
            note="数值校验一致",
            statute_ids=("LCL-19",),
            element_id="EL-3",
        ),
        CheckItem(
            item_id="RC-1",
            kind=ItemKind.UNKNOWN,
            clause_ref="",
            finding="数值与第 24 条不一致，请核对合同约定",
            source_id="LCL-24",
            grade="C",
            source="rule_check",
            note="数值与第 24 条不一致，请核对合同约定",
            statute_ids=("LCL-24",),
            rule_id="R-NONCOMPETE-TERM-2Y",
        ),
    ]
    return CheckTask(
        task_id="T-MODEL",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        contract_chars=1500,
        model_first=True,
        clause_review=True,
        items=items,
        latency_s=1.25,
    )


def test_process_switch_off_keeps_first_two_layers(repo: SqliteRepository) -> None:
    """开关关闭：第三层为空，第一二层齐全（条号常驻、原文/事实/类案返回）。"""
    task = _rich_task()
    detail = presenters.task_detail(task, repo, show_process=False)

    assert detail.process is None
    assert detail.verdict.process_details == []
    assert all(finding.process is None for finding in detail.findings)

    # 第一层：结论 + 条目 + 条号 + 限制 + 必备条款 + 建议正文
    verdict = summarize_verdict(task)
    assert detail.verdict.headline == verdict.headline
    assert detail.verdict.risk_count == verdict.risk_count
    assert detail.verdict.review_count == verdict.review_count
    assert detail.findings, "第一层条目不能为空"
    assert all(finding.finding and finding.statutes for finding in detail.findings)
    assert detail.limits.unmet_lines and detail.limits.field_missing_lines
    assert detail.limits.premise_count == 1
    assert detail.limits.statutory_na_line is not None
    assert detail.missing_terms.absent_count == 1
    assert detail.missing_terms.unseen_count == 1
    assert detail.missing_terms.manual_count == 1
    assert detail.missing_terms.written_count is None
    assert detail.suggestions[0].text == "把试用期调整到法定上限内。"
    assert detail.suggestion_empty_text is None

    # 第二层：法条原文、事实、类案
    assert all(statute.text for finding in detail.findings for statute in finding.statutes)
    assert [row.label for row in detail.facts] == ["用工主体类型", "合同期限"]
    assert detail.cases.note == presenters.CASE_NOTE
    assert detail.cases.items


def test_process_switch_on_adds_third_layer_only(repo: SqliteRepository) -> None:
    """开关打开：过程块、覆盖明细、逐条来源齐全；第一二层逐字段不变。"""
    task = _rich_task()
    off = presenters.task_detail(task, repo, show_process=False)
    on = presenters.task_detail(task, repo, show_process=True)

    assert off.findings == [finding.model_copy(update={"process": None}) for finding in on.findings], (
        "第三层以外，条目字段必须与关闭态一致"
    )
    assert on.verdict.conclusion_details == off.verdict.conclusion_details
    assert off.verdict.process_details == []
    assert on.verdict.process_details == presenters.split_verdict_details(summarize_verdict(task).details)[1]
    assert on.verdict.process_details, "开关打开时覆盖明细里应有过程行"
    assert on.process is not None
    assert on.process.details == on.verdict.process_details
    assert on.process.finding_help == presenters.CLAUSE_HELP
    assert on.process.degraded_visible is False
    assert on.missing_terms.written_count == 1
    assert all(finding.process is not None for finding in on.findings)
    assert on.limits == off.limits
    assert on.suggestions == off.suggestions
    assert on.facts == off.facts
    assert on.cases == off.cases


def test_missing_latency_stays_null(repo: SqliteRepository) -> None:
    """缺耗时保持 `null`，文案写“未记录”——绝不伪造 0.00 秒。"""
    detail = presenters.task_detail(_rich_task(), repo, show_process=True)
    assert detail.process is not None
    assert detail.process.latency_s is None
    assert detail.process.latency_text == "未记录"
    summary = presenters.history_summary(_rich_task())
    assert summary.latency_s is None
    assert summary.latency_text == "未记录"


def test_statute_views_never_expose_internal_ids(repo: SqliteRepository) -> None:
    """条号常驻且可读；查不到时是固定文案，内部 ID 只留在 `id` 字段。"""
    task = _rich_task()
    detail = presenters.task_detail(task, repo, show_process=False)
    for finding in detail.findings:
        for statute in finding.statutes:
            assert statute.label != statute.id
            assert statute.found is True
            assert statute.label.startswith("《") and "》" in statute.label

    missing = presenters.statute_view(repo, "ZZ-999")
    assert missing.found is False
    assert missing.label == "法源库未找到该条文"
    assert missing.text is None
    assert missing.effective_date is None
    assert missing.source_url is None

    for line in detail.limits.unmet_lines + detail.limits.field_missing_lines:
        assert "LCL-" not in line and "WPR-" not in line and "LL-" not in line


def test_missing_terms_counts_are_independent(repo: SqliteRepository) -> None:
    """必备条款三态计数独立于冲突/待确认条数，汇总句含条号与兜底说明。"""
    task = _rich_task()
    detail = presenters.task_detail(task, repo, show_process=False)
    terms = detail.missing_terms
    assert len(terms.items) == 4
    assert terms.absent_count + terms.unseen_count + terms.manual_count == 3  # 第四类是"已写"
    assert terms.summary.startswith("本次检查《劳动合同法》第十七条九类必备条款：")
    assert "缺失 1 类" in terms.summary and "未见 1 类" in terms.summary
    assert "已写" not in terms.summary, "覆盖率口径只在开关打开时出现"
    assert terms.manual_names == ["法律、法规规定应当纳入劳动合同的其他事项"]
    statuses = {item.term_id: item.status for item in terms.items}
    assert statuses["MT-2"] == "缺失" and statuses["MT-3"] == "未见"
    assert "缺失：未见工作内容和工作地点的约定" in next(
        item.text for item in terms.items if item.term_id == "MT-2"
    )

    # 计数不进结论条数：冲突/待确认仍由 summarize_verdict 决定
    verdict = summarize_verdict(task)
    assert detail.verdict.risk_count == verdict.risk_count
    assert detail.verdict.review_count == verdict.review_count


def test_missing_terms_short_text_merges_manual_into_one_sentence(repo: SqliteRepository) -> None:
    """正文过短：不并列「未做判定」与「需人工确认 N 类」，合成一句话（类名在同一句里）。"""
    names = ["用人单位名称、住所与负责人", "劳动合同期限"]
    terms = [
        MissingTerm(
            term_id=f"MT-{index}",
            name=name,
            clause_no="（一）",
            statute_id="LCL-17",
            status=MissingStatus.MANUAL,
            certainty="低",
            note="正文过短，需人工确认",
        )
        for index, name in enumerate(names, start=1)
    ]
    task = CheckTask(task_id="T-short", mode=TaskMode.CHECK, missing_terms=terms)
    parsed = presenters.missing_terms_of(task, repo, show_written=False)
    assert parsed.summary == ""
    assert parsed.short_note == (
        "正文过短，未做必备条款判定；以下 2 类需人工确认：用人单位名称、住所与负责人、劳动合同期限。"
    )
    # 短文本态只是不另起一行，不是把「需人工确认」的类数抹成 0
    assert parsed.manual_count == 2
    assert parsed.manual_names == names


def test_model_first_findings_follow_domain_sort(repo: SqliteRepository) -> None:
    """模型主导路径的条目顺序 = `sort_items`（前端不得重排），影响度/确定性来自 `display_of`。"""
    task = _model_first_task()
    detail = presenters.task_detail(task, repo, show_process=False)
    expected = sort_items(task.items)
    assert [finding.item_id for finding in detail.findings] == [item.item_id for item in expected]
    for finding, item in zip(detail.findings, expected, strict=True):
        level = display_of(item)
        assert finding.impact == level.impact
        assert finding.certainty == level.certainty
        assert finding.path == "consensus"
    # 程序数值复核条目的来源只在第三层出现
    rule_check = next(finding for finding in detail.findings if finding.item_id == "RC-1")
    assert rule_check.process is None
    on = presenters.task_detail(task, repo, show_process=True)
    rule_check_on = next(finding for finding in on.findings if finding.item_id == "RC-1")
    assert rule_check_on.process is not None
    assert rule_check_on.process.source_label == presenters.CONSENSUS_RULE_BADGE


def test_legacy_findings_follow_merge_order(repo: SqliteRepository) -> None:
    """旧路径的条目顺序 = `merge_findings`（规则 → A → B → C），DTO 不再二次排序。"""
    from mla.services.report import merge_findings

    task = _rich_task()
    detail = presenters.task_detail(task, repo, show_process=False)
    merged = merge_findings(task.items, task.clause_findings)
    assert len(detail.findings) == len(merged)
    paths = [finding.path for finding in detail.findings]
    assert paths == ["rule" if rank == 0 else "clause" for rank, _entry in merged]
    # 条款结论没有 item_id：按规格给空串，不用段落号冒充
    assert all(finding.item_id == "" for finding in detail.findings if finding.path == "clause")
    clause_rows = [finding for finding in detail.findings if finding.path == "clause"]
    assert [row.span_expansible for row in clause_rows] == [True, False], "超长片段才可展开"


def test_workflow_and_optional_fields(repo: SqliteRepository) -> None:
    """追问态给 ask_items；历史态不给追问/补充能力；可选字段只在有 field_missing 时出现。"""
    task = _rich_task()
    ready = presenters.task_detail(task, repo, show_process=False)
    assert ready.workflow.state == "awaiting_facts"
    assert ready.workflow.questions == ["试用期工资是多少？"]
    assert ready.workflow.ask_items[0].slot_id == "probation_wage"
    assert ready.workflow.effective_contract_id == "C-1"
    assert ready.workflow.optional_fields, "有 field_missing 时应有可选补充"
    assert all(field.label and field.field_id for field in ready.workflow.optional_fields)

    history = presenters.task_detail(task, repo, show_process=False, history=True)
    assert history.workflow.state == "history"
    assert history.workflow.questions == []
    assert history.workflow.ask_items == []
    assert history.workflow.optional_fields == []

    plain = CheckTask(task_id="T-PLAIN", mode=TaskMode.CHECK, contract_chars=1200)
    assert presenters.optional_picks(plain, repo) == []
    assert presenters.workflow_state(plain, history=False) == "ready"


def test_history_summary_uses_same_verdict(repo: SqliteRepository) -> None:
    """历史摘要的结论与详情同源；过程明细默认空，开关打开才返回。"""
    task = _rich_task()
    verdict = summarize_verdict(task)
    summary = presenters.history_summary(task)
    assert summary.verdict.status == verdict.status
    assert summary.verdict.risk_count == verdict.risk_count
    assert summary.verdict.process_details == []
    assert summary.suggestion_count == 1
    assert summary.contract_name == "演示合同"
    assert summary.created_at == "2026-01-05T09:00:00"
    on = presenters.history_summary(task, show_process=True)
    assert on.verdict.process_details == presenters.split_verdict_details(verdict.details)[1]


def test_qa_result_contract(repo: SqliteRepository) -> None:
    """问答：拒答/无依据都是成功结果；运行详情同样受过程开关控制。"""
    parent = _rich_task()
    empty = CheckTask(task_id="T-QA", mode=TaskMode.QA, enterprise_id="E1", token_in=10, token_out=5)
    result = presenters.qa_result(empty, repo, parent_task_id=parent.task_id, show_process=False)
    assert result.parent_task_id == "T-RICH"
    assert result.refusal is None
    assert result.empty_text == presenters.EMPTY_QA_TEXT
    assert result.sources == []
    assert result.process is None

    refused = CheckTask(
        task_id="T-QA2",
        mode=TaskMode.QA,
        enterprise_id="E1",
        refused=True,
        boundary_layer="terms",
        boundary_reason="问题超出劳动用工范围",
    )
    detail = presenters.qa_result(refused, repo, parent_task_id=parent.task_id, show_process=True)
    assert detail.refusal is not None
    assert detail.refusal.message.startswith("越界拒答（判定层：terms）")
    assert "12348" in detail.refusal.guidance
    assert detail.empty_text is None
    assert detail.process is not None

    answered = CheckTask(
        task_id="T-QA3",
        mode=TaskMode.QA,
        enterprise_id="E1",
        suggestions=[Suggestion(text="建议一。", source_ids=("LCL-19", "LCL-19"))],
        agent_trace=("search_statutes", "answer"),
        token_in=7,
        token_out=3,
    )
    with_sources = presenters.qa_result(answered, repo, parent_task_id=parent.task_id, show_process=True)
    assert [source.id for source in with_sources.sources] == ["LCL-19"], "来源按首次出现去重"
    assert with_sources.process is not None
    assert "工具调用" in with_sources.process.detail_text


def test_split_verdict_details_moves_only_process_rows() -> None:
    """第一/第三层的分界只取决于题型，不改写原文（规格 005 第 2 节的五类）。"""
    details = [
        "规则 17/20 可判定",
        "条款审查未运行",
        "条款审查发现 2 条明确冲突或显著失衡",
        "1 条条款待人工确认",
        "3 条待确认",
        "1 条规则因缺少事实无法判定",
        "必备条款 6/8 已写",
        "2 类需人工确认",
        "合同正文较短，结论可能不完整",
    ]
    first, process = presenters.split_verdict_details(details)
    assert process == [
        "规则 17/20 可判定",
        "条款审查未运行",
        "1 条规则因缺少事实无法判定",
        "必备条款 6/8 已写",
    ]
    assert first == [
        "条款审查发现 2 条明确冲突或显著失衡",
        "1 条条款待人工确认",
        "3 条待确认",
        "2 类需人工确认",
        "合同正文较短，结论可能不完整",
    ]
    assert set(first) | set(process) == set(details)


def test_show_query_path_key_matches_settings_route() -> None:
    """过程开关的键名只有一处口径：`presenters` 与设置路由必须一致。"""
    assert presenters.SHOW_QUERY_PATH_KEY == settings_routes.SHOW_QUERY_PATH_KEY == "ui.show_query_path"


@pytest.fixture
def clean_optional_cache() -> Iterator[None]:
    """可选字段缓存是进程级全局：用例自己清场，避免互相看到对方缓存的任务。

    直接操作私有容器是有意的：这条用例守的就是"这个容器必须有界"，绕开它测就没有意义。
    """
    with presenters._OPTIONAL_LOCK:
        presenters._OPTIONAL_CACHE.clear()
    yield
    with presenters._OPTIONAL_LOCK:
        presenters._OPTIONAL_CACHE.clear()


def _cache_task(index: int) -> CheckTask:
    return CheckTask(
        task_id=f"T-CACHE-{index}",
        mode=TaskMode.CHECK,
        contract_chars=1200,
        undetermined=[
            UndeterminedRule(
                rule_id="R-PROBATION-ONCE",
                name="同一用人单位与同一劳动者只能约定一次试用期",
                statute_id="LCL-19",
                missing_fields=("probation_times",),
                reason="field_missing",
            )
        ],
    )


def test_optional_pick_cache_is_bounded(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    clean_optional_cache: None,
) -> None:
    """按任务缓存有界：超出上限淘汰最早写入的；删除任务仍能定点清理。"""
    calls: list[int] = []
    monkeypatch.setattr(presenters, "_default_generator", lambda: calls.append(1))
    limit = presenters.OPTIONAL_CACHE_LIMIT
    tasks = [_cache_task(index) for index in range(limit + 5)]

    for task in tasks:
        assert presenters.optional_picks(task, repo), "有 field_missing 时必须给出候选"
    assert presenters.optional_cache_size() == limit, "缓存不得超过上限"
    assert len(calls) == limit + 5, "每个新任务只调一次模型"

    presenters.optional_picks(tasks[0], repo)
    assert len(calls) == limit + 6, "最早写入的条目应已被淘汰，需重算"
    presenters.optional_picks(tasks[-1], repo)
    assert len(calls) == limit + 6, "最新写入的条目仍在缓存，不得重算"

    presenters.forget_optional_picks(tasks[-1].task_id)
    assert presenters.optional_cache_size() == limit - 1
    presenters.optional_picks(tasks[-1], repo)
    assert len(calls) == limit + 7, "删除后再次请求应重算"


def test_draft_task_created_at_is_null(repo: SqliteRepository) -> None:
    """旧记录缺任务的创建时间：显式 `null`，不补当前时间。"""
    draft = CheckTask(task_id="T-DRAFT", mode=TaskMode.CHECK)
    detail = presenters.task_detail(draft, repo, show_process=False)
    assert detail.task.created_at is None
    assert detail.task.model == ""
    assert detail.task.contract_name == "未命名合同"  # 空名走同一份规范化，不显示空串
    assert presenters.history_summary(draft).created_at is None
