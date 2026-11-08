"""导出报告：把一次核对任务生成 Word（.docx）字节流，供界面下载。

只做"把已有结论排版"，不新增任何判断；免责声明与法条原文附在文末。
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any, cast

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.styles.style import ParagraphStyle

from ..domain.field_catalog import field_name
from ..domain.models import MISSING_TERM_UNJUDGED_ID, CheckTask, MissingStatus
from ..ports.repository import Repository

FONT = "Noto Sans SC"  # OFL-1.1，可商用；微软雅黑商业发布需授权
DISCLAIMER = "本报告由系统自动生成，仅列出可回链法条的风险与缺项，不构成法律意见；重大争议请咨询执业律师。"
# 条款审查区块：等级文案与免责句与界面共用同一口径（不判“违法/无效”，效力以裁判为准）。
CLAUSE_DISCLAIMER = "以上为风险提示与依据定位，不构成法律意见，条款效力认定以仲裁机构或法院裁判为准。"
CLAUSE_EMPTY_NOTE = "本次未启用条款审查，或无条款级结论；未列出的条款不代表合规。"
MISSING_SHORT_NOTE = "必备条款完整性：正文过短，未做必备条款判定。"
# 汇总句合计 9 类，而覆盖率分母是 8：不点明兜底条款容易被读成与 KPI 矛盾。
MISSING_BASE_NOTE = "（第（九）类为兜底条款，不参与覆盖率）"
SPAN_LIMIT = 80
CASE_HOLDING_LIMIT = 120
CASE_NOTE = "同类情形通常如何认定，不构成本次结论；以裁判文书为准。"
# 合并列表的来源标注：档位 0 = 规则确定性判定，1/2/3 = 模型审查的 A/B/C 级。
FINDING_SOURCES = ("规则判定", "模型审查 A 级", "模型审查 B 级", "模型审查 C 级")
CLAUSE_GRADE_RANK = {"A": 1, "B": 2, "C": 3}


def merge_findings(items, findings) -> list[tuple[int, Any]]:
    """把规则条目与条款审查结论按严重度合并排序（界面与 Word 报告共用同一口径）。

    档位 0 是确定性规则命中，1/2/3 依次是模型审查的 A/B/C 级；同档内规则按 rule_id 升序，
    条款结论按（段落号, trigger_id）升序。只决定展示顺序，不改动传入的列表。
    """
    rows: list[tuple[int, tuple, Any]] = []
    for item in items or []:
        rows.append((0, (str(item.rule_id),), item))
    for finding in findings or []:
        rank = CLAUSE_GRADE_RANK.get(str(finding.grade), 3)
        rows.append((rank, (int(finding.paragraph or 0), str(finding.trigger_id or "")), finding))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [(rank, entry) for rank, _, entry in rows]


def _apply_ea(run) -> None:
    """给 run 设置中文字体（python-docx 不直接支持 eastAsia，要动 XML）。"""
    rpr = run._element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), FONT)


def _setup(doc: DocxDocument) -> None:
    style = cast(ParagraphStyle, doc.styles["Normal"])
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FONT)
    for section in doc.sections:
        section.top_margin = Cm(2.2)
        section.bottom_margin = Cm(2.2)
        section.left_margin = Cm(2.4)
        section.right_margin = Cm(2.4)


def _title(doc: DocxDocument, text: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(16)
    run.font.name = FONT
    _apply_ea(run)


def _heading(doc: DocxDocument, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(12)
    run.font.name = FONT
    _apply_ea(run)


def _clip(text: str, limit: int = SPAN_LIMIT) -> str:
    """超长条款片段截断加省略号（中文省略号，与界面口径一致）。"""
    body = str(text or "")
    return body if len(body) <= limit else body[:limit] + "……"


def _kv_table(doc: DocxDocument, rows: list[tuple[str, str]]) -> None:
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    for k, v in rows:
        cells = table.add_row().cells
        cells[0].text = k
        cells[1].text = v
        cells[0].width = Cm(3.2)


def build_report_docx(task: CheckTask, repo: Repository) -> bytes:
    """生成报告字节流；调用方直接丢给 st.download_button。"""
    doc = Document()
    _setup(doc)
    _title(doc, "小微企业用工风险核对报告")

    _kv_table(
        doc,
        [
            ("任务编号", task.task_id),
            ("核对模式", str(task.mode)),
            ("生成日期", date.today().isoformat()),
            ("风险条目", str(len(task.items))),
            ("处理建议", str(len(task.suggestions))),
        ],
    )

    doc.add_paragraph()
    _heading(doc, "一、核对结论")
    findings = list(task.clause_findings or [])
    merged = merge_findings(task.items, findings)
    if merged:
        doc.add_paragraph("以下按严重度排序：先列确定性规则命中的风险，再列模型审查的 A / B / C 级条款结论。")
        for idx, (rank, entry) in enumerate(merged, start=1):
            if rank == 0:
                _finding_paragraph(doc, f"{idx}. {FINDING_SOURCES[0]}：{entry.finding}")
                _meta_paragraph(doc, f"依据：{_statute_label(repo, entry.source_id)} · 规则 {entry.rule_id}")
                _statute_source_paragraph(doc, repo, entry.source_id)
                continue
            _finding_paragraph(
                doc, f"{idx}. {FINDING_SOURCES[rank]}：第 {entry.paragraph} 段：{_clip(entry.span)}"
            )
            _meta_paragraph(doc, f"理由：{entry.reason}")
            cites = "、".join(f"{_statute_label(repo, sid)}（{sid}）" for sid in entry.statute_ids)
            _meta_paragraph(doc, f"依据：{cites or '未给出法条依据'}")
            for sid in entry.statute_ids:
                _statute_source_paragraph(doc, repo, sid)
            if entry.need_facts:
                _meta_paragraph(doc, "待补事实：" + "、".join(entry.need_facts))
            if str(getattr(entry, "source", "trigger")) == "model_extra":
                _meta_paragraph(doc, "来源：模型自提，未经触发器命中，等级封顶 C，建议人工抽检确认。")
    if not task.items:
        doc.add_paragraph("未发现可判定的硬性风险项（不代表合规，请结合事实复核）。")
    if findings:
        note = doc.add_paragraph(CLAUSE_DISCLAIMER)
        for run in note.runs:
            run.italic = True
            run.font.size = Pt(9)
    else:
        doc.add_paragraph(CLAUSE_EMPTY_NOTE)

    coverage = task.rule_coverage
    if coverage is not None and coverage.total > 0:
        judged = coverage.violated + coverage.compliant + coverage.not_applicable
        doc.add_paragraph(
            f"本次 {coverage.total} 条规则中，可判定 {judged} 条，"
            f"不适用 {coverage.not_applicable} 条，无法判定 {coverage.unknown} 条。"
        )
    for fields, rows in _group_undetermined(task.undetermined, "field_missing"):
        doc.add_paragraph(
            f"因缺少{_field_label(fields)}，以下 {len(rows)} 条规则无法判定：{_rule_names(rows)}"
        )
    for fields, rows in _group_undetermined(task.undetermined, "premise_missing"):
        doc.add_paragraph(
            f"另因缺少“{_field_label(fields)}”，以下 {len(rows)} 条规则未判定：{_rule_names(rows)}"
        )

    _missing_terms_paragraphs(doc, task, repo)

    doc.add_paragraph()
    _heading(doc, "二、处理建议")
    if task.suggestions:
        for i, sug in enumerate(task.suggestions, start=1):
            p = doc.add_paragraph(f"{i}. {sug.text}")
            p.paragraph_format.left_indent = Cm(0.4)
            cites = "、".join(_statute_label(repo, sid) for sid in sug.source_ids)
            note = doc.add_paragraph(f"依据：{cites}")
            note.paragraph_format.left_indent = Cm(0.8)
            for run in note.runs:
                run.italic = True
                run.font.size = Pt(9)
    else:
        doc.add_paragraph("本次没有可给出的建议（无依据不生成）。")

    doc.add_paragraph()
    _heading(doc, "三、引用条文原文")
    _statute_bodies(
        doc,
        repo,
        [item.source_id for item in task.items]
        + [sid for finding in findings for sid in finding.statute_ids],
    )

    _case_refs_paragraphs(doc, task, repo)

    doc.add_paragraph()
    note = doc.add_paragraph(DISCLAIMER)
    for run in note.runs:
        run.font.size = Pt(9)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _finding_paragraph(doc: DocxDocument, text: str) -> None:
    """合并列表里的一条结论：加粗一行，紧随其后的依据/理由用小字斜体。"""
    p = doc.add_paragraph(text)
    for run in p.runs:
        run.bold = True
        run.font.size = Pt(10)


def _meta_paragraph(doc: DocxDocument, text: str) -> None:
    p = doc.add_paragraph(text)
    p.paragraph_format.left_indent = Cm(0.4)
    for run in p.runs:
        run.italic = True
        run.font.size = Pt(9)


def _statute_bodies(doc: DocxDocument, repo: Repository, statute_ids: list[str]) -> None:
    """按首次出现顺序附条文原文；重复引用只印一次，取不到的条目不编造。"""
    seen: set[str] = set()
    printed = False
    for statute_id in statute_ids:
        if statute_id in seen:
            continue
        seen.add(statute_id)
        statute = repo.get_statute(statute_id)
        if statute is None:
            continue
        printed = True
        p = doc.add_paragraph(f"{_statute_label(repo, statute_id)}（{statute.status}）")
        for run in p.runs:
            run.bold = True
            run.font.size = Pt(9.5)
        body = doc.add_paragraph(statute.text)
        for run in body.runs:
            run.font.size = Pt(9)
    if not printed:
        doc.add_paragraph("本次没有可附列的条文原文。")


def _missing_terms_paragraphs(doc: DocxDocument, task: CheckTask, repo: Repository) -> None:
    """必备条款完整性：一行汇总 + 缺失/未见逐条（只陈述有没有写，不判效力）。"""
    terms = list(task.missing_terms or [])
    if not terms:
        return
    written = sum(
        1 for t in terms if t.status is MissingStatus.WRITTEN and t.term_id != MISSING_TERM_UNJUDGED_ID
    )
    absent = [t for t in terms if t.status is MissingStatus.ABSENT]
    unseen = [t for t in terms if t.status is MissingStatus.UNSEEN]
    manual = [t for t in terms if t.status is MissingStatus.MANUAL]
    if not absent and not unseen and not written:
        doc.add_paragraph(MISSING_SHORT_NOTE)
        return
    doc.add_paragraph(
        f"必备条款完整性：已写 {written} 类，缺失 {len(absent)} 类，需人工确认 {len(manual)} 类。"
        + MISSING_BASE_NOTE
    )
    if unseen:
        doc.add_paragraph(f"另有 {len(unseen)} 类未检出，需人工确认。")
    for term in absent:
        doc.add_paragraph(f"缺少{term.name}的约定（依据 {_missing_cite(repo, term)}）。")
    for term in unseen:
        doc.add_paragraph(f"未见{term.name}的约定，请人工确认（依据 {_missing_cite(repo, term)}）。")


def _missing_cite(repo: Repository, term) -> str:
    """依据写法：法条 ID + 条文号 + 第几项；条文取不到时只留 ID，不编造条号。"""
    statute = repo.get_statute(term.statute_id)
    article = statute.article_no if statute is not None else ""
    body = str(term.clause_no or "").strip("（）() ")
    if article and body:
        return f"{term.statute_id} {article}第{body}项"
    return term.statute_id


def _statute_label(repo: Repository, statute_id: str) -> str:
    statute = repo.get_statute(statute_id)
    if statute is None:
        return statute_id
    short = statute.law_name.replace("中华人民共和国", "")
    return f"《{short}》{statute.article_no}"


def _statute_source_paragraph(doc: DocxDocument, repo: Repository, statute_id: str) -> None:
    """法条来源链接：法源库里没有 URL 就整行不写（不编造）。"""
    statute = repo.get_statute(statute_id)
    if statute is not None and statute.source_url:
        _meta_paragraph(doc, f"来源：{statute.source_url}")


def _case_refs_paragraphs(doc: DocxDocument, task: CheckTask, repo: Repository) -> None:
    """类案参照：只作参照、不进结论；案例库查不到就跳过，不编造标题与链接。"""
    ids = [ref.case_id for ref in (task.case_refs or [])]
    if not ids:
        return
    by_id = {case.case_id: case for case in repo.list_cases()}
    cases = [by_id[case_id] for case_id in ids if case_id in by_id]
    if not cases:
        return
    doc.add_paragraph()
    _heading(doc, "四、类案参照")
    doc.add_paragraph(CASE_NOTE)
    for case in cases:
        title = doc.add_paragraph()
        run = title.add_run(case.title)
        run.bold = True
        run.font.size = Pt(10)
        _apply_ea(run)
        doc.add_paragraph(_clip(case.holding, CASE_HOLDING_LIMIT))
        _meta_paragraph(doc, f"{case.source} · {case.dispute_type}")
        if case.source_url:
            _meta_paragraph(doc, f"来源：{case.source_url}")


def _group_undetermined(rows, reason: str) -> list[tuple[tuple[str, ...], list]]:
    """把同一类“判不了”的规则按缺失字段归组，排序固定，保证每次导出顺序一致。"""
    grouped: dict[tuple[str, ...], list] = {}
    for row in rows or []:
        if str(getattr(row, "reason", "")) != reason:
            continue
        grouped.setdefault(tuple(row.missing_fields), []).append(row)
    return [(fields, sorted(grouped[fields], key=lambda r: r.rule_id)) for fields in sorted(grouped)]


def _field_label(fields: tuple[str, ...]) -> str:
    """缺失字段的中文名；字段为空（值或阈值解析不了）时给个不露 id 的说法。"""
    return "、".join(field_name(f) for f in fields) or "相关事实"


def _rule_names(rows: list) -> str:
    return "、".join(str(r.name) for r in rows)
