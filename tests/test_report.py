"""报告导出测试：能生成可打开的 .docx，且关键内容都在（不联网）。"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import (
    CaseRef,
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    Role,
    RuleCoverage,
    TaskMode,
    UndeterminedRule,
    UnmetTerm,
    User,
)
from mla.services.report import build_report_docx, merge_findings
from mla.services.tasks import run_check

CONTRACT = Path(data_path("evalsets/sample_contract.txt")).read_text(encoding="utf-8")


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "report.sqlite3"),
    )


def _rule_item(rule_id: str, finding: str = "示例风险", source_id: str = "LCL-19") -> CheckItem:
    return CheckItem(
        item_id="I-" + rule_id,
        kind=ItemKind.RISK,
        clause_ref="第十九条",
        finding=finding,
        source_id=source_id,
        rule_id=rule_id,
    )


def test_report_docx_contains_sections(tmp_path) -> None:
    repo = _repo(tmp_path)
    user = User(
        user_id="U1", username="alice", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
    )
    task = run_check(user=user, contract_text=CONTRACT, repo=repo, enterprise_id="E1")

    blob = build_report_docx(task, repo)
    assert blob[:2] == b"PK"  # docx 是 zip 容器

    doc = Document(io.BytesIO(blob))
    text = "\n".join(p.text for p in doc.paragraphs)
    table_text = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    merged = text + table_text

    assert "小微企业用工风险核对报告" in merged
    assert "一、核对结论" in merged
    assert "二、处理建议" in merged
    assert "三、引用条文原文" in merged
    assert "三、条款审查（模型辅助）" not in merged  # 条款结论已并入第一节
    assert "四、引用条文原文" not in merged
    assert "本次未启用条款审查" in merged  # 没有 findings 时给一句说明，不留空表
    assert "不构成法律意见" in merged
    assert task.task_id in merged
    assert "劳动合同法" in merged  # 依据渲染成法条名，而非 LCL-19
    assert "规则判定" in merged  # 每条标注来源
    assert task.items[0].finding[:8] in merged


def test_report_merges_clause_findings_into_conclusion(tmp_path) -> None:
    """条款结论并入第一节：规则条目先，再按 A/B/C；片段/理由/依据/待补事实都要落进 Word。"""
    repo = _repo(tmp_path)
    long_span = "乙方在试用期内的工资按转正后约定工资的百分之六十计发，且不享受绩效奖金与其他补贴" * 3
    task = CheckTask(
        task_id="T-CLAUSE",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        items=[_rule_item("R-PROBATION-6M", finding="试用期约定超过法定上限")],
        clause_findings=[
            ClauseFinding(
                paragraph=4,
                span=long_span,
                grade="B",
                reason="该约定与第三十五条关于协商一致变更的规定存在冲突",
                statute_ids=("LCL-35",),
                trigger_id="TR-UNILATERAL-TRANSFER",
                source="trigger",
            ),
            ClauseFinding(
                paragraph=3,
                span="试用期工资为转正工资的 60%。",
                grade="A",
                reason="该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符",
                statute_ids=("LCL-20",),
                trigger_id="TR-PROBATION-WAGE-BASE",
                source="trigger",
                need_facts=("试用期工资的比较基准",),
            ),
        ],
    )

    doc = Document(io.BytesIO(build_report_docx(task, repo)))
    text = "\n".join(p.text for p in doc.paragraphs)
    table_text = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    merged = text + table_text

    assert "一、核对结论" in merged
    assert "三、引用条文原文" in merged
    # 规则条目在最前，A 级次之，B 级最后
    assert merged.index("1. 规则判定：") < merged.index("2. 模型审查 A 级：")
    assert merged.index("2. 模型审查 A 级：") < merged.index("3. 模型审查 B 级：")
    assert "试用期约定超过法定上限" in merged
    assert "依据：《劳动合同法》第十九条 · 规则 R-PROBATION-6M" in merged
    assert "该约定与第二十条关于试用期工资不得低于约定工资 80% 的规定不符" in merged
    assert "《劳动合同法》第二十条（LCL-20）" in merged
    assert "待补事实：试用期工资的比较基准" in merged
    assert "……" in merged  # 超 80 字的片段被截断
    assert long_span[:60] in merged and long_span[:90] not in merged
    assert "不构成法律意见，条款效力认定以仲裁机构或法院裁判为准" in merged
    # 条款结论引用的法条原文也要附在引用条文里
    assert repo.get_statute("LCL-20").text[:20] in merged


def test_report_shows_source_urls(tmp_path) -> None:
    """来源链接要打通：结论条目的法条来源、类案条目都带一行来源（数据里有就必须显示）。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T-SRC",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        items=[_rule_item("R-PROBATION-6M", finding="试用期约定超过法定上限")],
        case_refs=[CaseRef(case_id="C-001", holding="示例裁判要点")],
    )
    merged = "\n".join(p.text for p in Document(io.BytesIO(build_report_docx(task, repo))).paragraphs)

    assert "来源：https://www.mohrss.gov.cn/" in merged  # 法条来源（LCL-19 的官方发布页）
    assert "四、类案参照" in merged  # 有 case_refs 才出这一节
    assert "来源：https://www.court.gov.cn/" in merged  # 案例来源
    assert "不构成本次结论" in merged


def test_report_omits_case_section_without_refs(tmp_path) -> None:
    """没有类案引用就不出这一节，避免空标题。"""
    repo = _repo(tmp_path)
    task = CheckTask(task_id="T-NOCASE", mode=TaskMode.CHECK, enterprise_id="E1")
    merged = "\n".join(p.text for p in Document(io.BytesIO(build_report_docx(task, repo))).paragraphs)
    assert "四、类案参照" not in merged


def test_merge_findings_orders_rules_then_grades() -> None:
    """纯排序函数：规则 → A → B → C；同档规则按 rule_id、条款按（段落, trigger_id）。"""
    items = [
        _rule_item("R-ZZZ"),
        _rule_item("R-AAA"),
    ]
    findings = [
        ClauseFinding(paragraph=5, span="片段", grade="C", reason="待确认", trigger_id="TR-Z"),
        ClauseFinding(paragraph=2, span="片段", grade="A", reason="冲突", trigger_id="TR-B"),
        ClauseFinding(paragraph=1, span="片段", grade="B", reason="失衡", trigger_id="TR-A"),
        ClauseFinding(paragraph=2, span="片段", grade="A", reason="冲突", trigger_id="TR-A"),
    ]
    snapshot = list(items)

    merged = merge_findings(items, findings)

    assert [rank for rank, _ in merged] == [0, 0, 1, 1, 2, 3]
    assert [entry.rule_id for rank, entry in merged if rank == 0] == ["R-AAA", "R-ZZZ"]
    assert [(entry.paragraph, entry.trigger_id) for rank, entry in merged if rank == 1] == [
        (2, "TR-A"),
        (2, "TR-B"),
    ]
    grades = [(rank, entry.grade) for rank, entry in merged if rank > 0]
    assert grades == [(1, "A"), (1, "A"), (2, "B"), (3, "C")]
    # 只有 C 级时也要排在 B 之后，不能混进 A/B
    assert [rank for rank, entry in merged if rank > 0 and entry.grade == "C"] == [3]
    assert items == snapshot, "排序不能改动传入的规则列表"


def test_report_shows_missing_terms(tmp_path) -> None:
    """必备条款完整性：一行汇总 + 缺失/未见逐条，且不动原有四个章节号。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T-MISS",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        missing_terms=[
            MissingTerm(
                term_id="MT-3",
                name="劳动合同期限",
                clause_no="（三）",
                statute_id="LCL-17",
                status=MissingStatus.ABSENT,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-8",
                name="劳动保护与劳动条件",
                clause_no="（八）",
                statute_id="LCL-17",
                status=MissingStatus.UNSEEN,
                certainty="低",
            ),
            MissingTerm(
                term_id="MT-1",
                name="用人单位名称、住所与负责人",
                clause_no="（一）",
                statute_id="LCL-17",
                status=MissingStatus.WRITTEN,
                certainty="低",
            ),
            MissingTerm(
                term_id="MT-9",
                name="法律法规规定应当纳入的其他事项",
                clause_no="（九）",
                statute_id="LCL-17",
                status=MissingStatus.MANUAL,
                certainty="低",
            ),
        ],
    )

    doc = Document(io.BytesIO(build_report_docx(task, repo)))
    merged = "\n".join(p.text for p in doc.paragraphs)

    assert "必备条款完整性：已写 1 类，缺失 1 类，需人工确认 1 类。" in merged
    assert "（第（九）类为兜底条款，不参与覆盖率）" in merged
    assert "缺少劳动合同期限的约定（依据 LCL-17 第十七条第三项）。" in merged
    assert "未见劳动保护与劳动条件的约定，请人工确认（依据 LCL-17 第十七条第八项）。" in merged
    assert "劳动合同期限" in merged
    assert "条款无效" not in merged
    assert "MT-" not in merged  # 内部编号不印给用户
    for heading in ("一、核对结论", "二、处理建议", "三、引用条文原文"):
        assert heading in merged


def test_report_missing_terms_short_contract_note(tmp_path) -> None:
    """正文过短时只给一行说明，不断言缺失。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T-SHORT",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        missing_terms=[
            MissingTerm(
                term_id="MT-3",
                name="劳动合同期限",
                clause_no="（三）",
                statute_id="LCL-17",
                status=MissingStatus.MANUAL,
                certainty="低",
                note="正文不足，未判定",
            )
        ],
    )
    merged = "\n".join(p.text for p in Document(io.BytesIO(build_report_docx(task, repo))).paragraphs)
    assert "必备条款完整性：正文过短，未做必备条款判定。" in merged
    assert "缺少" not in merged


def test_report_handles_empty_task(tmp_path) -> None:
    repo = _repo(tmp_path)
    user = User(user_id="U1", username="bob", password_hash="x", salt="y", role=Role.USER)
    task = run_check(
        user=user, facts={"term_years": "3", "probation_months": "1"}, contract_text="", repo=repo
    )
    assert task.mode is TaskMode.CHECK
    blob = build_report_docx(task, repo)
    assert len(blob) > 1000


def test_report_shows_unmet_and_statutory_summary(tmp_path) -> None:
    """报告分区：合同未约定逐条列，法定兜底只给一句汇总。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T-UNMET-R",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        unmet=[
            UnmetTerm(
                rule_id="R-WPR-MONTHLY-PAY",
                name="工资至少每月支付一次",
                statute_id="WPR-7",
                fields=("pay_times_per_month",),
            )
        ],
        statutory_na=8,
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=5, not_applicable=13, unknown=1),
    )
    text = "\n".join(p.text for p in Document(io.BytesIO(build_report_docx(task, repo))).paragraphs)
    assert "合同未约定：" in text
    assert "未约定每月发薪次数，无法判断是否满足“工资至少每月支付一次”" in text
    assert "《工资支付暂行规定》第七条" in text
    assert "另有 8 条规则因合同未约定而适用法定标准。" in text
    assert "pay_times_per_month" not in text


def test_report_shows_undetermined_rules(tmp_path) -> None:
    """判不了的规则要落进报告：覆盖计数 + 按缺失字段归组，字段名用中文。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T-UNDET",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        undetermined=[
            UndeterminedRule(
                rule_id="R-ANNUAL-LEAVE-5",
                name="累计工作满 1 年不满 10 年，年休假 5 天",
                statute_id="AL-3",
                missing_fields=("annual_leave_days",),
                reason="field_missing",
            ),
            UndeterminedRule(
                rule_id="R-UNUSED-LEAVE-PAY",
                name="未休年假按日工资 300% 支付",
                statute_id="ALI-10",
                missing_fields=("unused_leave_pay_ratio",),
                reason="field_missing",
            ),
            UndeterminedRule(
                rule_id="R-PROBATION-6M",
                name="三年以上固定期限或无固定期限，试用期不得超过 6 个月",
                statute_id="LCL-19",
                missing_fields=("term_years", "probation_months"),
                reason="premise_missing",
            ),
        ],
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=5, not_applicable=12, unknown=3),
    )

    doc = Document(io.BytesIO(build_report_docx(task, repo)))
    text = "\n".join(p.text for p in doc.paragraphs)
    table_text = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    merged = text + table_text

    assert "本次 20 条规则中，可判定 17 条，不适用 12 条，无法判定 3 条。" in merged
    assert "无法判定" in merged
    assert "因缺少年休假天数，以下 1 条规则无法判定" in merged
    assert "未休年假工资倍率" in merged
    assert "另因缺少“合同期限、试用期月数”，以下 1 条规则未判定" in merged
    assert "三年以上固定期限或无固定期限" in merged
    for raw_id in ("annual_leave_days", "unused_leave_pay_ratio", "term_years", "probation_months"):
        assert raw_id not in merged  # 不把原始字段 id 印给用户

    assert "一、核对结论" in merged
    assert "二、处理建议" in merged
    assert "三、引用条文原文" in merged
