"""结果 DTO 组装（规格 006 §2「结果 DTO」）。

**只做数据搬运，不重写任何判定**：

- 结论三态、冲突/待确认条数 → `domain.verdict.summarize_verdict`；
- 影响度、确定性、四键排序 → `domain.grading.display_of` / `sort_items`；
- 旧路径的规则条目 + 条款结论合并 → `services.report.merge_findings`；
- 覆盖明细的第一/第三层归类 → 本模块 `split_verdict_details`；
- Word 报告 → `services.report.build_report_docx`（报告路由直接调用，不在这里重排）。

三条展示口径，任何一个违反都算破坏 005：

1. **过程开关**（`ui.show_query_path`）关闭时：`TaskDetail.process=null`、
   `VerdictView.process_details=[]`、每条 `FindingView.process=null`；开启时三者齐全。
   第一层的条号、建议、限制提示与第二层的法条原文、事实、类案**始终返回**，折叠只是 UI 行为。
2. **缺耗时保持 `null`**（`latency_s` / `HistorySummary.latency_s`），不伪造 `0`。
3. 法条一律以 `StatuteView` 出参：`label` 是可读法名条号，查不到时是
   「法源库未找到该条文」，内部 ID 只出现在 `id` 字段里供前端做 key。

**依赖方向**：本模块是展示层 helper 的唯一实现（0.4.0 前由 `src/mla/ui/app.py` 反过来 import 它，
Streamlit 界面已删除，现在由 `api/routes/*` 直接调用）。反向不行：`api` 不得 import 任何界面框架——它只
import `domain` / `services` / `adapters` 与 `schemas`（pydantic），基线快照因此不带界面依赖
（见 `tests/api/test_legacy_snapshot.py` 的 import 守卫）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..adapters.files import normalize_contract_name
from ..adapters.llm import DeepSeekLLM
from ..domain.asking import candidate_fields
from ..domain.field_catalog import field_hint, field_name, field_question
from ..domain.grading import display_of, sort_items
from ..domain.models import (
    MISSING_TERM_UNJUDGED_ID,
    CheckItem,
    CheckTask,
    ClauseFinding,
    MissingStatus,
    TaskMode,
    User,
)
from ..domain.policy import MAX_ASK_CANDIDATES
from ..domain.verdict import NOT_REVIEWED_MARKERS, Verdict, summarize_verdict
from ..ports.repository import Repository
from ..services.asking import suggest_optional
from ..services.consensus import SOURCE_RULE_CHECK
from ..services.credentials import CredentialError, resolve_llm_config
from ..services.report import merge_findings
from .schemas import (
    AskItemView,
    CaseBlockView,
    CaseItemView,
    FactRow,
    FindingProcess,
    FindingView,
    HistorySummary,
    LimitsView,
    MissingTermItem,
    MissingTermsView,
    OptionalFieldView,
    ProcessView,
    QaProcessView,
    QaResult,
    RefusalView,
    StatuteView,
    SuggestionView,
    TaskDetail,
    TaskHead,
    TrendPoint,
    TrendView,
    VerdictView,
    WorkflowView,
)

# ---------------------------------------------------------------- 口径常量（与旧界面逐字相同）
# 过程开关的键名：`routes/settings.py` 也定义了一份，两边必须一致（测试里有断言）。
SHOW_QUERY_PATH_KEY = "ui.show_query_path"
DISCLAIMER_FALLBACK = "非法律意见，仅供参考；重大争议请咨询专业人士。"
CASE_NOTE = "同类情形通常如何认定，不构成本案结论。"
RESULT_DISCLAIMER = "以上为风险提示与依据定位，不构成法律意见；条款效力以仲裁机构或法院裁判为准。"
#: 顶部结论的常驻提醒（005 第一层「原结论提醒」）：不随过程开关变化。
VERDICT_CAVEAT = "结论不代表合规，请结合事实复核。"
CLAUSE_SPAN_LIMIT = 80
#: 旧路径合并列表的来源徽标（档位 0 = 规则判定，1/2/3 = 条款审查 A/B/C）。
FINDING_SOURCE_BADGES: tuple[str, ...] = ("规则判定·确定性", "模型审查·A", "模型审查·B", "模型审查·C")
#: 模型主导路径里程序追加的数值复核条目（不是模型结论）。
CONSENSUS_RULE_BADGE = "程序数值校验"
MODEL_EXTRA_BADGE = "模型自提"
CONSENSUS_HELP = (
    "按影响度分组：高 / 中 / 低（影响度由合同要素决定）。每组内先列确定冲突，再列待确认。"
    "确定冲突：与法条明文冲突，或数值校验与法条不一致；待确认：需补充事实或含裁量，需人工判断。"
)
CLAUSE_HELP = "等级含义：A 与法条明文冲突，B 显著失衡，C 待确认；只表示与法条存在冲突的风险。"
CLAUSE_NOT_REVIEWED_NOTE = "本次未做条款级审查，条款级风险未覆盖。"
CLAUSE_CLEAR_NOTE = "本次条款级审查未发现可疑条款。"
MISSING_TERMS_BASE_NOTE = "（第（九）类为兜底条款，不参与覆盖率）"
MISSING_SHORT_NOTE = "正文过短，未做必备条款判定。"
#: 正文过短时的合体句：不并列「未做判定」与「需人工确认 N 类」两段（2026-11-18 界面修复）。
MISSING_SHORT_MANUAL = "正文过短，未做必备条款判定；以下 {n} 类需人工确认：{names}。"
SUGGESTION_EMPTY = "本次没有可给出的建议（无依据不生成）。"
SUGGESTION_NO_MODEL = "未配置大模型：本次不生成处理建议（规则核对结论不受影响）。"
EMPTY_QA_TEXT = (
    "没有检索到可引用的现行条文，已按“无依据不答”处理；可补充事实（如合同期限、"
    "是否书面签订）后重问，或转专业咨询。"
)
REFUSAL_LLM_GUIDANCE = "按“无依据不答”原则不给出生成式回答；请在设置页配置 DeepSeek Key 后重试。"
REFUSAL_SCOPE_GUIDANCE = (
    "本系统只处理劳动用工与劳动合同问题；其他问题建议拨打 12348 公共法律服务热线或咨询专业人士。"
)
EXTRACT_FALLBACK_NOTE = "抽取已回退规则式：非常规写法的条款可能未被识别，请人工复核。"
EXTRACT_NO_KEY_NOTE = "本次未使用模型抽取：未配置 Key"
MISSING_TERMS_STATUTE = "《劳动合同法》第十七条九类必备条款"
TREND_NOTE = (
    "统计窗口：最近 {n} 次有效核对；平均值含真实 0 风险记录，分母为 {n}。"
    "图内“风险条目”只统计规则条目，不含条款审查结论与覆盖状态。"
)
#: 历史趋势与旧界面 `ui/trend.py` 同一口径：最近 12 次核对，时间正序。
TREND_POINTS = 12
#: 企业档案里录入、但不参与规则判定的两项：显示中文名而不是原始 id。
ARCHIVE_FIELD_NAMES: dict[str, str] = {"working_hours": "工时制度", "social_insurance": "社保缴纳"}


def fact_display_name(field_id: str) -> str:
    """事实字段显示名：先查档案专用名，再走字段目录，都没命中就原样显示。"""
    return ARCHIVE_FIELD_NAMES.get(field_id) or field_name(field_id)


def include_process(repo: Repository) -> bool:
    """第三层是否返回：只有落库值恰为 "1" 为开；读取不回写（与旧界面同一口径）。"""
    return repo.get_setting(SHOW_QUERY_PATH_KEY) == "1"


# ---------------------------------------------------------------- 分层与状态（规格 005）
def is_process_detail(row: str) -> bool:
    """覆盖明细里哪些行属第三层：`规则 x/y 可判定`、条款审查运行状态、缺事实无法判定数、
    `必备条款 x/8 已写`。其余行是结论计数与限制提示，留在第一层。"""
    text = str(row or "")
    if text.startswith("规则 ") or text.startswith("必备条款 "):
        return True
    if text.startswith("条款审查") and any(k in text for k in ("已运行", "未运行", "未完成")):
        return True
    return "因缺少事实无法判定" in text


def split_verdict_details(details: list[str]) -> tuple[list[str], list[str]]:
    """把 `Verdict.details` 分成（第一层, 第三层）：只归类，不改写任何原文。"""
    first: list[str] = []
    process: list[str] = []
    for row in details or []:
        (process if is_process_detail(row) else first).append(row)
    return first, process


def degraded_visible(task: CheckTask) -> bool:
    """是否显示“已降级”告警（与旧界面 `_show_degraded_badge` 同一判断）。

    “未配置 Key”属预期配置状态，只给中性提示；调用失败 / 超时 / 回退才告警。
    """
    if not task.degraded:
        return False
    errors = [str(e) for e in (task.errors or [])]
    if any(("回退" in e or "失败" in e or "超时" in e) for e in errors):
        return True
    if any("未配置 Key" in e for e in errors):
        return False
    return True


def verdict_status(task: CheckTask) -> str:
    """历史筛选与顶部结论共用的三态（只看 `summarize_verdict`，不另立口径）。"""
    return summarize_verdict(task).status


def filter_history(tasks: list[CheckTask], choice: str) -> list[CheckTask]:
    """历史筛选：`risk` 用 `summarize_verdict().status`，`degraded` 用 `degraded_visible`。"""
    if choice == "risk":
        return [task for task in tasks if verdict_status(task) == "risk"]
    if choice == "degraded":
        return [task for task in tasks if degraded_visible(task)]
    return list(tasks)


def latency_text(task: CheckTask) -> str:
    """耗时：只有真跑过才有值；旧任务缺字段写“未记录”，不伪造 `0.00 秒`。"""
    return f"{task.latency_s:.2f} 秒" if task.latency_s is not None else "未记录"


def run_detail(task: CheckTask) -> str:
    """运行详情一行：任务编号与 Token 用量，只在折叠区出现。"""
    parts = [f"任务 {task.task_id}"]
    if task.token_in or task.token_out:
        parts.append(f"Token 输入 {task.token_in} · 输出 {task.token_out}")
    return " · ".join(parts)


def clause_review_note(task: CheckTask, clause_review: bool | None = None) -> str:
    """合并列表末尾的一句覆盖说明：条款审查做没做、有没有命中（与旧界面一字不差）。"""
    if list(getattr(task, "clause_findings", None) or []):
        return ""
    notes = [
        str(e) for e in (getattr(task, "errors", None) or []) if "条款审查" in str(e) or "条款判定" in str(e)
    ]
    if any(marker in note for note in notes for marker in NOT_REVIEWED_MARKERS):
        return CLAUSE_NOT_REVIEWED_NOTE
    enabled = bool(getattr(task, "clause_review", False)) if clause_review is None else bool(clause_review)
    return CLAUSE_CLEAR_NOTE if enabled else CLAUSE_NOT_REVIEWED_NOTE


# ---------------------------------------------------------------- 依据
def source_label(repo: Repository, statute_id: str) -> str:
    """LCL-19 → 《劳动合同法》第十九条（查不到就原样显示，与旧界面口径一致）。"""
    statute = repo.get_statute(statute_id)
    if statute is None:
        return statute_id
    short = statute.law_name.replace("中华人民共和国", "")
    return f"《{short}》{statute.article_no}"


def statute_view(repo: Repository, statute_id: str) -> StatuteView:
    """一条依据的展示形态：条号常驻，原文/施行日期/链接按需折叠，内部 ID 不对外。"""
    statute = repo.get_statute(statute_id)
    if statute is None:
        return StatuteView(id=statute_id, found=False, label="法源库未找到该条文")
    short = statute.law_name.replace("中华人民共和国", "")
    return StatuteView(
        id=statute_id,
        found=True,
        label=f"《{short}》{statute.article_no}",
        text=statute.text,
        effective_date=_iso(statute.effective_date),
        source_url=statute.source_url or None,
    )


# ---------------------------------------------------------------- 条目
def _clip(text: str) -> str:
    body = str(text or "")
    return body if len(body) <= CLAUSE_SPAN_LIMIT else body[:CLAUSE_SPAN_LIMIT] + "……"


def _marker(source: str) -> str | None:
    return f"{MODEL_EXTRA_BADGE}，待人工确认" if source == "model_extra" else None


def _consensus_finding(item: CheckItem, repo: Repository, *, show_process: bool) -> FindingView:
    level = display_of(item)
    span = str(item.evidence_text or "")
    ids = list(item.statute_ids or ())
    if not ids and item.source_id:
        ids = [item.source_id]
    source = str(item.source or "")
    return FindingView(
        item_id=item.item_id,
        path="consensus",
        impact=level.impact,
        certainty=level.certainty,
        display_marker=_marker(source),
        finding=item.finding,
        clause_ref=str(item.clause_ref or ""),
        evidence_text=span,
        span_preview=_clip(span),
        span_expansible=len(span) > CLAUSE_SPAN_LIMIT,
        need_facts=[],
        statutes=[statute_view(repo, sid) for sid in ids],
        process=FindingProcess(
            source_label=(
                CONSENSUS_RULE_BADGE
                if source == SOURCE_RULE_CHECK
                else MODEL_EXTRA_BADGE
                if source == "model_extra"
                else "模型审查"
            ),
            grade=str(item.grade or ""),
            note=str(item.note or ""),
        )
        if show_process
        else None,
    )


def _rule_finding(item: CheckItem, repo: Repository, *, show_process: bool) -> FindingView:
    """旧路径的规则条目（`merge_findings` 的 rank 0）。"""
    level = display_of(item)
    span = str(item.evidence_text or "")
    return FindingView(
        item_id=item.item_id,
        path="rule",
        impact=level.impact,
        certainty=level.certainty,
        display_marker=_marker(str(item.source or "")),
        finding=item.finding,
        clause_ref=str(item.clause_ref or ""),
        evidence_text=span,
        span_preview=_clip(span),
        span_expansible=len(span) > CLAUSE_SPAN_LIMIT,
        need_facts=[],
        statutes=[statute_view(repo, item.source_id)] if item.source_id else [],
        process=FindingProcess(
            source_label=FINDING_SOURCE_BADGES[0],
            grade=str(item.grade or ""),
            note=str(item.note or ""),
        )
        if show_process
        else None,
    )


def _clause_finding(
    finding: ClauseFinding, repo: Repository, *, rank: int, show_process: bool
) -> FindingView:
    """旧路径的条款结论（`merge_findings` 的 rank 1/2/3）：片段 → 理由 → 依据原文。"""
    level = display_of(finding)
    span = str(finding.span or "")
    return FindingView(
        item_id="",
        path="clause",
        impact=level.impact,
        certainty=level.certainty,
        display_marker=_marker(str(finding.source or "")),
        finding=str(finding.reason or ""),
        clause_ref=str(finding.paragraph),
        evidence_text=span,
        span_preview=_clip(span),
        span_expansible=len(span) > CLAUSE_SPAN_LIMIT,
        need_facts=[str(item) for item in (finding.need_facts or ())],
        statutes=[statute_view(repo, sid) for sid in finding.statute_ids],
        process=FindingProcess(
            source_label=FINDING_SOURCE_BADGES[rank] if rank < len(FINDING_SOURCE_BADGES) else "模型审查",
            grade=str(finding.grade or ""),
            note="",
        )
        if show_process
        else None,
    )


def findings_of(task: CheckTask, repo: Repository, *, show_process: bool) -> list[FindingView]:
    """条目列表：模型主导路径走 `sort_items`，旧路径走 `merge_findings`（不在此处另排一次）。"""
    if bool(task.model_first):
        return [_consensus_finding(item, repo, show_process=show_process) for item in sort_items(task.items)]
    rows: list[FindingView] = []
    for rank, entry in merge_findings(task.items, task.clause_findings):
        if isinstance(entry, CheckItem):
            rows.append(_rule_finding(entry, repo, show_process=show_process))
        else:
            rows.append(_clause_finding(entry, repo, rank=rank, show_process=show_process))
    return rows


# ---------------------------------------------------------------- 限制提示
def undetermined_groups(rows: Any, reason: str) -> list[tuple[tuple[str, ...], list[Any]]]:
    """把同一类“判不了”的规则按缺失字段归组，顺序固定（旧界面也调本函数渲染）。"""
    grouped: dict[tuple[str, ...], list[Any]] = {}
    for row in rows or []:
        if str(getattr(row, "reason", "")) != reason:
            continue
        grouped.setdefault(tuple(row.missing_fields), []).append(row)
    return [(fields, sorted(grouped[fields], key=lambda r: r.rule_id)) for fields in sorted(grouped)]


def field_label(fields: tuple[str, ...]) -> str:
    """缺失字段的中文名；字段为空（值或阈值解析不了）时给个不露 id 的说法。"""
    return "、".join(fact_display_name(f) for f in fields) or "相关事实"


def rule_names(rows: list[Any]) -> str:
    return "、".join(str(r.name) for r in rows)


def unmet_line(row: Any, repo: Repository) -> str:
    """一条“合同未约定”：缺了哪项约定、因此判不了哪条要求、依据在哪。"""
    fields = "、".join(fact_display_name(f) for f in row.fields) or "相关事项"
    return f"未约定{fields}，无法判断是否满足“{row.name}”（依据 {source_label(repo, row.statute_id)}）"


def limits_of(task: CheckTask, repo: Repository) -> LimitsView:
    """第一层的限制提示：合同未约定 / 需补充事实 / 法定兜底，每行按原模板生成。"""
    unmet_lines = [unmet_line(row, repo) for row in sorted(list(task.unmet or []), key=lambda r: r.rule_id)]
    field_groups = undetermined_groups(task.undetermined, "field_missing")
    premise_groups = undetermined_groups(task.undetermined, "premise_missing")
    field_lines = [
        f"因缺少“{field_label(fields)}”，以下 {len(rows)} 条规则无法判定：{rule_names(rows)}"
        for fields, rows in field_groups
    ]
    premise_lines = [
        f"因缺少“{field_label(fields)}”，以下 {len(rows)} 条规则未判定：{rule_names(rows)}"
        for fields, rows in premise_groups
    ]
    return LimitsView(
        unmet_lines=unmet_lines,
        field_missing_lines=field_lines,
        premise_missing_lines=premise_lines,
        premise_count=sum(len(rows) for _, rows in premise_groups),
        statutory_na_line=(
            f"另有 {task.statutory_na} 条规则因合同未约定而适用法定标准。" if task.statutory_na else None
        ),
    )


# ---------------------------------------------------------------- 必备条款
def missing_clause_text(repo: Repository, term: Any) -> str:
    """条文定位写成“第十七条第三项”；取不到条文号时退回条款序号原文。"""
    statute = repo.get_statute(term.statute_id)
    article = statute.article_no if statute is not None else ""
    body = str(term.clause_no or "").strip("（）() ")
    if article and body:
        return f"{article}第{body}项"
    return str(term.clause_no or "")


def missing_term_text(repo: Repository, term: Any) -> str:
    """单类必备条款的状态文案（旧界面的 error/warning 正文由它生成）。"""
    where = missing_clause_text(repo, term)
    status = term.status
    if status is MissingStatus.ABSENT:
        return f"缺失：未见{term.name}的约定（{where}）"
    if status is MissingStatus.UNSEEN:
        return f"未见{term.name}的约定（{where}），请人工确认"
    return f"{term.name}（{where}）"


@dataclass(frozen=True)
class MissingTermGroups:
    """必备条款四态的分组（只分组，不判效力）：已写计数 + 缺失/未见/需人工确认三组。"""

    written_count: int
    absent: list[Any]
    unseen: list[Any]
    manual: list[Any]


def missing_term_groups(task: CheckTask) -> MissingTermGroups:
    """按状态分组；`MT-9`（兜底类）不计入“已写”，与覆盖率分母口径一致。"""
    terms = list(task.missing_terms or [])
    written = sum(
        1
        for term in terms
        if term.status is MissingStatus.WRITTEN and term.term_id != MISSING_TERM_UNJUDGED_ID
    )
    return MissingTermGroups(
        written_count=written,
        absent=[term for term in terms if term.status is MissingStatus.ABSENT],
        unseen=[term for term in terms if term.status is MissingStatus.UNSEEN],
        manual=[term for term in terms if term.status is MissingStatus.MANUAL],
    )


def missing_terms_summary(task: CheckTask, *, show_written: bool) -> str:
    """汇总句：`已写 x 类` 是覆盖率口径（仅开关打开时），其余三态常驻。

    正文过短、三态皆空且没有任何“已写”时返回空串（调用方改用 `MISSING_SHORT_NOTE`）。
    """
    groups = missing_term_groups(task)
    if not groups.absent and not groups.unseen and not groups.written_count:
        return ""
    counts: list[str] = []
    if show_written:
        counts.append(f"已写 {groups.written_count} 类")
    counts.extend(
        [
            f"缺失 {len(groups.absent)} 类",
            f"未见 {len(groups.unseen)} 类",
            f"需人工确认 {len(groups.manual)} 类",
        ]
    )
    return f"本次检查{MISSING_TERMS_STATUTE}：{'，'.join(counts)}。{MISSING_TERMS_BASE_NOTE}"


def missing_short_note(task: CheckTask) -> str:
    """正文过短（必备条款整体落回“需人工确认”）时的一句话说明。

    把待人工确认的类名并进同一句：界面上不再出现「未做判定」与「需人工确认 N 类」
    两段并列（两段并列会被读成自相矛盾）。
    """
    groups = missing_term_groups(task)
    names = "、".join(term.name for term in groups.manual)
    if not names:
        return MISSING_SHORT_NOTE
    return MISSING_SHORT_MANUAL.format(n=len(groups.manual), names=names)


def missing_terms_of(task: CheckTask, repo: Repository, *, show_written: bool) -> MissingTermsView:
    """必备条款完整性：缺失/未见/需人工确认各自计数，独立于冲突与待确认条数。"""
    terms = list(task.missing_terms or [])
    if not terms:
        return MissingTermsView(summary="")
    groups = missing_term_groups(task)
    summary = missing_terms_summary(task, show_written=show_written)
    return MissingTermsView(
        summary=summary,
        short_note=None if summary else missing_short_note(task),
        written_count=groups.written_count if show_written else None,
        absent_count=len(groups.absent),
        unseen_count=len(groups.unseen),
        manual_count=len(groups.manual),
        items=[_term_item(repo, term) for term in terms],
        manual_names=[term.name for term in groups.manual],
    )


def _term_item(repo: Repository, term: Any) -> MissingTermItem:
    return MissingTermItem(
        term_id=term.term_id,
        name=term.name,
        status=str(term.status),
        text=missing_term_text(repo, term),
        statute=statute_view(repo, term.statute_id),
    )


# ---------------------------------------------------------------- 建议 / 事实 / 类案
def suggestions_of(task: CheckTask, repo: Repository) -> list[SuggestionView]:
    """处理建议：正文与依据常驻；回退免责声明不重复展示。"""
    rows: list[SuggestionView] = []
    for suggestion in task.suggestions or []:
        cites = "、".join(source_label(repo, sid) for sid in suggestion.source_ids)
        disclaimer = str(getattr(suggestion, "disclaimer", "") or "")
        rows.append(
            SuggestionView(
                text=suggestion.text,
                citation_text=f"依据 {cites}",
                display_disclaimer=None if disclaimer in ("", DISCLAIMER_FALLBACK) else disclaimer,
                sources=[statute_view(repo, sid) for sid in suggestion.source_ids],
            )
        )
    return rows


def suggestion_empty_text(task: CheckTask) -> str | None:
    """没有建议时的一句说明（两种情况话术不同，与旧界面一致）。"""
    if task.suggestions:
        return None
    if any("未配置大模型" in str(e) for e in (task.errors or [])):
        return SUGGESTION_NO_MODEL
    return SUGGESTION_EMPTY


def facts_of(task: CheckTask) -> list[FactRow]:
    return [
        FactRow(label=fact_display_name(key), value=str(value)) for key, value in (task.facts or {}).items()
    ]


def cases_of(task: CheckTask, repo: Repository) -> CaseBlockView:
    """类案参照：只作参照、不构成本案结论；案例库为空时说明来源。"""
    cases = list(repo.list_cases())
    if not cases:
        return CaseBlockView(note="暂无官方案例，当前结论全部来自法条。", summary="")
    by_id = {case.case_id: case for case in cases}
    shown = [by_id[ref.case_id] for ref in (task.case_refs or []) if ref.case_id in by_id]
    if shown:
        summary = f"共 {len(cases)} 条官方案例，本次匹配 {len(shown)} 条"
    else:
        cited_ids = {item.source_id for item in task.items}
        matched = [case for case in cases if set(case.statute_ids) & cited_ids]
        shown = (matched or cases)[:5]
        summary = f"共 {len(cases)} 条官方案例，按法条匹配 {len(matched)} 条"
    return CaseBlockView(
        note=CASE_NOTE,
        summary=summary,
        items=[
            CaseItemView(
                title=case.title,
                holding=case.holding,
                source=case.source,
                dispute_type=case.dispute_type,
                source_url=case.source_url or None,
            )
            for case in shown
        ],
    )


# ---------------------------------------------------------------- 拒答
def refusal_of(task: CheckTask) -> RefusalView | None:
    if not task.refused:
        return None
    layer = str(task.boundary_layer or "")
    reason = str(task.boundary_reason or "")
    if layer == "llm":
        return RefusalView(
            layer=layer,
            reason=reason,
            message=f"无法生成回答：{reason or '未配置大模型或调用失败'}",
            guidance=REFUSAL_LLM_GUIDANCE,
        )
    return RefusalView(
        layer=layer,
        reason=reason,
        message=f"越界拒答（判定层：{layer or '未记录'}）：{reason or '问题超出劳动用工范围'}",
        guidance=REFUSAL_SCOPE_GUIDANCE,
    )


# ---------------------------------------------------------------- 第三层过程
def process_of(
    task: CheckTask,
    *,
    process_details: list[str],
    clause_review: bool | None,
) -> ProcessView:
    """查询路径与判定来源（只在开关打开时被调用）。"""
    cited = {item.source_id for item in task.items if item.source_id}
    cited.update(sid for item in task.items for sid in (getattr(item, "statute_ids", ()) or ()))
    errors = [str(e) for e in (task.errors or [])]
    extract_note: str | None = None
    if any(("回退" in e or "失败" in e or "超时" in e) for e in errors):
        extract_note = EXTRACT_FALLBACK_NOTE
    elif any("未配置 Key" in e for e in errors):
        extract_note = EXTRACT_NO_KEY_NOTE
    ask_rounds = int(task.ask_rounds or 0)
    return ProcessView(
        cited_statute_count=len(cited),
        latency_s=task.latency_s,
        latency_text=latency_text(task),
        details=list(process_details),
        finding_help=CONSENSUS_HELP if bool(task.model_first) else CLAUSE_HELP,
        clause_review_note=clause_review_note(task, clause_review) or None,
        verify_ok=bool(task.verify_ok),
        traceability_text="引用可溯源" if task.verify_ok else "引用不可溯源",
        ask_rounds=ask_rounds,
        ask_text=f"补问 {ask_rounds} 轮" if ask_rounds else "无补问",
        degraded_visible=degraded_visible(task),
        extract_note=extract_note,
        questions=[str(q) for q in (task.questions or [])],
        task_id=task.task_id,
        token_in=int(task.token_in or 0),
        token_out=int(task.token_out or 0),
        run_detail=run_detail(task),
    )


# ---------------------------------------------------------------- 可补充字段（按任务缓存，有界）
#: 进程内缓存上限：任务多且都不删除时不能无限增长；超出淘汰最早写入的一条（FIFO）。
OPTIONAL_CACHE_LIMIT = 200
_OPTIONAL_CACHE: OrderedDict[str, list[str]] = OrderedDict()
_OPTIONAL_LOCK = threading.Lock()


def optional_cache_size() -> int:
    """当前缓存条目数（给测试与运维观察用；不暴露缓存内容）。"""
    with _OPTIONAL_LOCK:
        return len(_OPTIONAL_CACHE)


def _optional_cached(key: str) -> list[str] | None:
    with _OPTIONAL_LOCK:
        cached = _OPTIONAL_CACHE.get(key)
        return None if cached is None else list(cached)


def _optional_remember(key: str, picked: list[str]) -> None:
    with _OPTIONAL_LOCK:
        _OPTIONAL_CACHE[key] = list(picked)
        _OPTIONAL_CACHE.move_to_end(key)
        while len(_OPTIONAL_CACHE) > OPTIONAL_CACHE_LIMIT:
            _OPTIONAL_CACHE.popitem(last=False)


def _default_generator() -> Any | None:
    """按统一 resolver 构造生成器（没有 Key 时不构造）。测试可直接替换本函数。"""
    try:
        llm_cfg, _resolved = resolve_llm_config()
    except CredentialError:
        return None
    if llm_cfg is None or not llm_cfg.api_key:
        return None
    return DeepSeekLLM(llm_cfg)


def optional_picks(task: CheckTask, repo: Repository) -> list[str]:
    """本次任务的可补充字段：`candidate_fields` 排序 + 模型在候选内挑最多 3 项，按任务缓存。

    与旧界面 `_optional_picks` 同一顺序：只有存在 `field_missing` 时才提供补充入口。
    缓存有界（`OPTIONAL_CACHE_LIMIT`），命中不改变顺序、不重复调用模型。
    """
    if not any(str(getattr(row, "reason", "")) == "field_missing" for row in (task.undetermined or [])):
        return []
    key = str(task.task_id)
    cached = _optional_cached(key)
    if cached is not None:
        return cached
    facts = {str(k): str(v) for k, v in (task.facts or {}).items()}
    candidates = candidate_fields(tuple(repo.list_rules()), facts, limit=MAX_ASK_CANDIDATES)
    picked: list[str] = []
    if candidates:
        picked, _errors = suggest_optional(_default_generator(), facts, candidates)
    _optional_remember(key, picked)
    return list(picked)


def forget_optional_picks(task_id: str) -> None:
    """任务被删除时清掉缓存，避免内存里留下无主条目。"""
    with _OPTIONAL_LOCK:
        _OPTIONAL_CACHE.pop(task_id, None)


# ---------------------------------------------------------------- 工作流状态
def workflow_state(task: CheckTask, *, history: bool) -> str:
    if history:
        return "history"
    return "awaiting_facts" if list(task.questions or []) else "ready"


def workflow_of(task: CheckTask, repo: Repository, *, history: bool) -> WorkflowView:
    """追问 / 可选补充：历史态不返回这两项能力（与旧界面的历史详情一致）。"""
    if history:
        return WorkflowView(
            state="history",
            effective_contract_id=str(task.contract_id or ""),
            ask_rounds=0,
            questions=[],
            ask_items=[],
            optional_fields=[],
        )
    ask_items = [
        AskItemView(
            slot_id=str(item.get("slot_id") or ""),
            question=str(item.get("question") or ""),
            source=str(item.get("source") or ""),
            hint=field_hint(str(item.get("slot_id") or "")) or None,
        )
        for item in (task.ask_items or [])
        if isinstance(item, dict)
    ]
    optional_fields = [
        OptionalFieldView(
            field_id=field_id,
            label=field_name(field_id),
            hint=field_hint(field_id) or None,
            question=field_question(field_id) or None,
        )
        for field_id in optional_picks(task, repo)
    ]
    return WorkflowView(
        state=workflow_state(task, history=False),
        effective_contract_id=str(task.contract_id or ""),
        ask_rounds=int(task.ask_rounds or 0),
        questions=[str(q) for q in (task.questions or [])],
        ask_items=ask_items,
        optional_fields=optional_fields,
    )


# ---------------------------------------------------------------- 入口：TaskDetail
def task_detail(
    task: CheckTask,
    repo: Repository,
    *,
    show_process: bool,
    history: bool = False,
    clause_review: bool | None = None,
) -> TaskDetail:
    """把一次任务搬成结果 DTO；`show_process` 由服务端按全局设置决定，客户端不得覆盖。"""
    verdict: Verdict = summarize_verdict(task)
    first_details, process_details = split_verdict_details(verdict.details)
    return TaskDetail(
        task=TaskHead(
            task_id=task.task_id,
            enterprise_id=str(task.enterprise_id or ""),
            contract_id=str(task.contract_id or ""),
            contract_name=normalize_contract_name(task.contract_name),
            contract_chars=int(task.contract_chars or 0),
            created_at=_iso(task.created_at),
            model=str(task.model or ""),
            model_first=bool(task.model_first),
            clause_review=bool(task.clause_review),
            shared=bool(task.shared),
        ),
        verdict=VerdictView(
            status=str(verdict.status),
            headline=verdict.headline,
            risk_count=int(verdict.risk_count),
            review_count=int(verdict.review_count),
            conclusion_details=first_details,
            process_details=list(process_details) if show_process else [],
            caveat=VERDICT_CAVEAT,
        ),
        findings=findings_of(task, repo, show_process=show_process),
        limits=limits_of(task, repo),
        missing_terms=missing_terms_of(task, repo, show_written=show_process),
        suggestions=suggestions_of(task, repo),
        suggestion_empty_text=suggestion_empty_text(task),
        facts=facts_of(task),
        cases=cases_of(task, repo),
        process=(
            process_of(task, process_details=process_details, clause_review=clause_review)
            if show_process
            else None
        ),
        refusal=refusal_of(task),
        workflow=workflow_of(task, repo, history=history),
        errors=[str(e) for e in (task.errors or [])],
        disclaimer=RESULT_DISCLAIMER,
    )


# ---------------------------------------------------------------- 入口：QaResult
def qa_context(task: CheckTask) -> dict[str, Any]:
    """问答上下文：只取结构化结论（事实、规则命中、条款结论），**不含合同正文与片段**。"""
    return {
        "facts": dict(task.facts or {}),
        "items": [{"rule_id": item.rule_id, "statute_id": item.source_id} for item in (task.items or [])],
        "clauses": [
            {
                "paragraph": finding.paragraph,
                "grade": str(finding.grade),
                "trigger_id": finding.trigger_id,
                "statute_ids": list(finding.statute_ids),
            }
            for finding in (task.clause_findings or [])
        ],
    }


def qa_result(
    task: CheckTask,
    repo: Repository,
    *,
    parent_task_id: str,
    show_process: bool,
) -> QaResult:
    """问答任务 → `QaResult`：拒答/空结果都是成功结果，不是 HTTP 异常。"""
    suggestions = suggestions_of(task, repo)
    refusal = refusal_of(task)
    sources: list[StatuteView] = []
    seen: set[str] = set()
    for suggestion in task.suggestions or []:
        for sid in suggestion.source_ids:
            if sid not in seen:
                seen.add(sid)
                sources.append(statute_view(repo, sid))
    empty_text = None
    if refusal is None and not suggestions:
        empty_text = EMPTY_QA_TEXT
    detail_parts: list[str] = []
    if task.agent_trace:
        detail_parts.append("工具调用 " + " → ".join(task.agent_trace))
    if task.token_in or task.token_out:
        detail_parts.append(f"Token 输入 {task.token_in} · 输出 {task.token_out}")
    return QaResult(
        task_id=task.task_id,
        parent_task_id=parent_task_id,
        refusal=refusal,
        empty_text=empty_text,
        errors=[str(e) for e in (task.errors or [])],
        sources=sources,
        suggestions=suggestions,
        process=(
            QaProcessView(
                agent_trace=[str(item) for item in (task.agent_trace or [])],
                token_in=int(task.token_in or 0),
                token_out=int(task.token_out or 0),
                detail_text=" · ".join(detail_parts),
            )
            if show_process
            else None
        ),
    )


# ---------------------------------------------------------------- 入口：历史
def history_summary(task: CheckTask, *, show_process: bool = False) -> HistorySummary:
    """历史列表摘要：结论口径与详情页同一份 `summarize_verdict`，过程明细按开关置空。"""
    verdict = summarize_verdict(task)
    first_details, process_details = split_verdict_details(verdict.details)
    return HistorySummary(
        task_id=task.task_id,
        contract_name=normalize_contract_name(task.contract_name),
        created_at=_iso(task.created_at),
        verdict=VerdictView(
            status=str(verdict.status),
            headline=verdict.headline,
            risk_count=int(verdict.risk_count),
            review_count=int(verdict.review_count),
            conclusion_details=first_details,
            process_details=list(process_details) if show_process else [],
            caveat=VERDICT_CAVEAT,
        ),
        suggestion_count=len(task.suggestions or []),
        latency_s=task.latency_s,
        latency_text=latency_text(task),
        refused=bool(task.refused),
        verify_ok=bool(task.verify_ok),
        degraded_visible=degraded_visible(task),
        ask_rounds=int(task.ask_rounds or 0),
    )


def _stamp(value: Any) -> str:
    """趋势图 x 轴标签：有则到分钟，旧记录只有日期（与 `ui/trend.py` 同口径）。"""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0:
            return value.strftime("%m-%d")
        return value.strftime("%m-%d %H:%M")
    return str(value or "未知")


def history_trend(tasks: list[CheckTask], *, window_size: int = 200) -> TrendView:
    """最近 12 次核对的风险条目趋势；`tasks` 是历史窗口（新→旧），点数按时间正序。"""
    picks = list(tasks)[:TREND_POINTS]
    stamps = [_stamp(task.created_at) for task in reversed(picks)]
    duplicated = {stamp for stamp in stamps if stamps.count(stamp) > 1}
    seen: dict[str, int] = {}
    points: list[TrendPoint] = []
    for order, (stamp, task) in enumerate(zip(stamps, reversed(picks), strict=True), start=1):
        label = stamp
        if label in duplicated:
            seen[label] = seen.get(label, 0) + 1
            label = f"{label} · {seen[label]}"
        points.append(
            TrendPoint(
                order=order,
                label=label,
                date=stamp,
                risk_item_count=len(task.items or []),
                suggestion_count=len(task.suggestions or []),
            )
        )
    counts = [point.risk_item_count for point in points]
    return TrendView(
        recent=counts[-1] if counts else 0,
        average=(sum(counts) / len(counts)) if counts else 0.0,
        max=max(counts) if counts else 0,
        window_size=window_size,
        note=TREND_NOTE.format(n=len(counts)),
        points=points,
    )


# ---------------------------------------------------------------- 企业事实
def enterprise_facts(repo: Repository, user: User) -> dict[str, str]:
    """企业档案里已录的 3 个事实直接预填，避免重复追问（与旧界面同一顺序）。"""
    if not user.enterprise_id:
        return {}
    ent = repo.get_enterprise(user.enterprise_id)
    if ent is None:
        return {}
    facts: dict[str, str] = {}
    if ent.employer_type:
        facts["employer_type"] = ent.employer_type
    if ent.working_hours:
        facts["working_hours"] = ent.working_hours
    if ent.social_insurance in ("是", "否"):
        facts["social_insurance"] = ent.social_insurance
    return facts


def check_state(task: CheckTask) -> str:
    """内存任务的展示状态（`awaiting_facts` / `ready`）。"""
    return workflow_state(task, history=False)


def is_check_task(task: CheckTask) -> bool:
    return task.mode is TaskMode.CHECK


def _iso(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value) or None


__all__ = [
    "CASE_NOTE",
    "CLAUSE_HELP",
    "CLAUSE_NOT_REVIEWED_NOTE",
    "CLAUSE_CLEAR_NOTE",
    "CLAUSE_SPAN_LIMIT",
    "CONSENSUS_RULE_BADGE",
    "DISCLAIMER_FALLBACK",
    "EMPTY_QA_TEXT",
    "FINDING_SOURCE_BADGES",
    "MISSING_TERMS_BASE_NOTE",
    "OPTIONAL_CACHE_LIMIT",
    "RESULT_DISCLAIMER",
    "SHOW_QUERY_PATH_KEY",
    "VERDICT_CAVEAT",
    "check_state",
    "clause_review_note",
    "degraded_visible",
    "enterprise_facts",
    "fact_display_name",
    "facts_of",
    "field_label",
    "filter_history",
    "findings_of",
    "forget_optional_picks",
    "history_summary",
    "history_trend",
    "include_process",
    "is_check_task",
    "is_process_detail",
    "latency_text",
    "limits_of",
    "missing_clause_text",
    "missing_short_note",
    "missing_term_groups",
    "missing_term_text",
    "missing_terms_of",
    "missing_terms_summary",
    "optional_cache_size",
    "optional_picks",
    "process_of",
    "qa_context",
    "qa_result",
    "refusal_of",
    "rule_names",
    "run_detail",
    "source_label",
    "split_verdict_details",
    "statute_view",
    "suggestion_empty_text",
    "suggestions_of",
    "task_detail",
    "undetermined_groups",
    "unmet_line",
    "verdict_status",
    "workflow_of",
    "workflow_state",
]
