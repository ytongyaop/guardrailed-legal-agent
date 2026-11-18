"""请求 / 响应 DTO（规格 006 §2）。

规则（与 006 一致）：
- 可空字段显式 `null`，不省略键；时间一律 ISO-8601 字符串；
- 响应里**不出现**口令、哈希、盐、模型 Key 以及 Key 的长度；
- `*Row` 是 `/admin` 列表用的展示行（对应 006 基础 DTO 的 `StatuteEntry` / `RuleEntry` /
  `OperationLog`）；`*View` 留给批次 3 的结果 DTO，避免同名不同义。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..adapters.files import normalize_contract_name
from ..domain.models import (
    ContractRecord,
    Enterprise,
    OperationLog,
    RuleEntry,
    StatuteEntry,
    User,
)

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """`Page<T>={items,total,page,page_size}`；带 `total_all` 的子类见下方各页。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


# ---------------------------------------------------------------- setup


class RuntimeInfo(BaseModel):
    copied_from: str | None = None
    db_path: str
    warnings: list[str] = Field(default_factory=list)
    migration_status: str
    messages: list[str] = Field(default_factory=list)


class SetupStatus(BaseModel):
    needs_admin: bool
    runtime: RuntimeInfo


class SetupAdminIn(BaseModel):
    username: str
    password: str
    confirm_password: str
    enterprise_name: str = ""


# ---------------------------------------------------------------- auth


class UserView(BaseModel):
    user_id: str
    username: str
    role: str
    enterprise_id: str
    onboarded: bool

    @classmethod
    def of(cls, user: User) -> UserView:
        return cls(
            user_id=user.user_id,
            username=user.username,
            role=str(user.role),
            enterprise_id=user.enterprise_id,
            onboarded=bool(user.onboarded),
        )


class AuthState(BaseModel):
    user: UserView
    csrf_token: str
    model_configured: bool


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    enterprise_name: str = ""


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


# ---------------------------------------------------------------- enterprise


class EnterpriseIn(BaseModel):
    name: str = ""
    employer_type: str = ""
    staff_size: int = Field(default=0, ge=0)
    working_hours: str = ""
    social_insurance: str = ""


class EnterpriseView(BaseModel):
    enterprise_id: str
    name: str
    employer_type: str
    staff_size: int
    working_hours: str
    social_insurance: str
    created_at: str

    @classmethod
    def of(cls, ent: Enterprise) -> EnterpriseView:
        return cls(
            enterprise_id=ent.enterprise_id,
            name=ent.name,
            employer_type=ent.employer_type,
            staff_size=int(ent.staff_size),
            working_hours=ent.working_hours,
            social_insurance=ent.social_insurance,
            created_at=ent.created_at,
        )


# ---------------------------------------------------------------- contracts


class ContractSummary(BaseModel):
    contract_id: str
    filename: str
    char_count: int
    uploaded_at: str

    @classmethod
    def of(cls, rec: ContractRecord) -> ContractSummary:
        return cls(
            contract_id=rec.contract_id,
            filename=normalize_contract_name(rec.filename),
            char_count=len(rec.text),
            uploaded_at=rec.uploaded_at,
        )


class ContractDetail(BaseModel):
    contract_id: str
    enterprise_id: str
    filename: str
    text: str
    uploaded_at: str

    @classmethod
    def of(cls, rec: ContractRecord) -> ContractDetail:
        return cls(
            contract_id=rec.contract_id,
            enterprise_id=rec.enterprise_id,
            filename=normalize_contract_name(rec.filename),
            text=rec.text,
            uploaded_at=rec.uploaded_at,
        )


class ContractPage(Page[ContractSummary]):
    total_all: int = 0


class ParseTextIn(BaseModel):
    filename: str
    text: str


class ParseResult(BaseModel):
    filename: str
    text: str
    char_count: int


# ---------------------------------------------------------------- settings


class SettingsUiIn(BaseModel):
    show_query_path: bool


class SettingsUiView(BaseModel):
    show_query_path: bool


class ProviderView(BaseModel):
    provider: str
    display_name: str
    configured: bool
    source: str
    label: str


class ModelCheckView(BaseModel):
    ok: bool
    checked_at: str
    kind: str
    stale: bool


class EmbeddingView(BaseModel):
    model: str
    configured: bool
    fallback_text: str


class ModelSettingsView(BaseModel):
    providers: list[ProviderView] = Field(default_factory=list)
    model: str
    status_line: str
    check: ModelCheckView | None = None
    embedding: EmbeddingView


class ModelKeyIn(BaseModel):
    provider: str
    key: str


# ---------------------------------------------------------------- admin


class AdminUser(BaseModel):
    user_id: str
    username: str
    role: str
    enterprise_id: str
    created_at: str
    onboarded: bool

    @classmethod
    def of(cls, user: User) -> AdminUser:
        return cls(
            user_id=user.user_id,
            username=user.username,
            role=str(user.role),
            enterprise_id=user.enterprise_id,
            created_at=user.created_at,
            onboarded=bool(user.onboarded),
        )


class RoleIn(BaseModel):
    role: str


class DeleteAccountIn(BaseModel):
    confirm_username: str


class DeletionOut(BaseModel):
    deleted: bool
    username: str
    contracts: int
    tasks: int
    logs: int
    reason: str


class StatuteRow(BaseModel):
    statute_id: str
    law_name: str
    article_no: str
    text: str
    issuing_body: str
    doc_no: str
    effective_date: date | None = None
    expiry_date: date | None = None
    status: str
    superseded_by: str | None = None
    level: str
    region: str
    source_url: str
    note: str

    @classmethod
    def of(cls, entry: StatuteEntry) -> StatuteRow:
        return cls(
            statute_id=entry.statute_id,
            law_name=entry.law_name,
            article_no=entry.article_no,
            text=entry.text,
            issuing_body=entry.issuing_body,
            doc_no=entry.doc_no,
            effective_date=entry.effective_date,
            expiry_date=entry.expiry_date,
            status=str(entry.status),
            superseded_by=entry.superseded_by,
            level=str(entry.level),
            region=entry.region,
            source_url=entry.source_url,
            note=entry.note,
        )


class StatutePage(Page[StatuteRow]):
    total_all: int = 0


class RuleRow(BaseModel):
    rule_id: str
    name: str
    field: str
    op: str
    threshold: float | str
    statute_id: str
    unit: str
    severity: str
    effective_date: date | None = None
    note: str
    when_field: str
    when_op: str
    when_value: float | str
    when_field2: str
    when_op2: str
    when_value2: float | str
    when_mode: str
    evidence_span: str

    @classmethod
    def of(cls, rule: RuleEntry) -> RuleRow:
        return cls(
            rule_id=rule.rule_id,
            name=rule.name,
            field=rule.field,
            op=rule.op,
            threshold=rule.threshold,
            statute_id=rule.statute_id,
            unit=rule.unit,
            severity=str(rule.severity),
            effective_date=rule.effective_date,
            note=rule.note,
            when_field=rule.when_field,
            when_op=rule.when_op,
            when_value=rule.when_value,
            when_field2=rule.when_field2,
            when_op2=rule.when_op2,
            when_value2=rule.when_value2,
            when_mode=rule.when_mode,
            evidence_span=rule.evidence_span,
        )


class SlotRow(BaseModel):
    slot_id: str
    name: str
    type: str
    question: str
    needed_by: list[str] = Field(default_factory=list)


class RuleCatalogView(BaseModel):
    rules: list[RuleRow] = Field(default_factory=list)
    slots: list[SlotRow] = Field(default_factory=list)


class LogRow(BaseModel):
    log_id: str
    task_id: str
    user_id: str
    action: str
    detail: str
    created_at: str

    @classmethod
    def of(cls, log: OperationLog) -> LogRow:
        return cls(
            log_id=log.log_id,
            task_id=log.task_id,
            user_id=log.user_id,
            action=log.action,
            detail=log.detail,
            created_at=log.created_at,
        )


class LogPage(Page[LogRow]):
    total_all: int = 0


class MetricRow(BaseModel):
    key: str
    label: str
    value: float | int
    threshold: float | None = None
    passed: bool | None = None


class EvalVariant(BaseModel):
    variant: str
    metrics: list[MetricRow] = Field(default_factory=list)
    mismatches: list[dict[str, object]] = Field(default_factory=list)


class EvalHistoryRow(BaseModel):
    time: str
    case_count: int
    variant: str
    accuracy: float | None = None
    recall: float | None = None
    passed_text: str


class EvaluationView(BaseModel):
    available: bool
    message: str | None = None
    case_count: int = 0
    generated_at: str = ""
    variants: list[EvalVariant] = Field(default_factory=list)
    history: list[EvalHistoryRow] = Field(default_factory=list)


# ---------------------------------------------------------------- 核对结果（006「结果 DTO」）
#
# 这些模型**只承载**结果页要渲染的数据，不在这里算任何东西：计数、分级、排序、合并全部由
# `presenters.py` 调用 `domain/verdict.py`、`domain/grading.py`、`services/report.py` 的现有函数
# 得到。字段与 006 表一一对应，可空字段显式 `null`。


class StatuteView(BaseModel):
    """一条依据：`label` 永远是可读法名条号，查不到时是固定文案，绝不回内部 ID。"""

    id: str
    found: bool
    label: str
    text: str | None = None
    effective_date: str | None = None
    source_url: str | None = None


class FindingProcess(BaseModel):
    """单条结论的判定来源（第三层；过程开关关闭时整个对象为 `null`）。"""

    source_label: str
    grade: str
    note: str


class FindingView(BaseModel):
    item_id: str
    path: str  # consensus / rule / clause
    impact: str | None = None
    certainty: str | None = None
    display_marker: str | None = None
    finding: str
    clause_ref: str
    evidence_text: str
    span_preview: str
    span_expansible: bool
    need_facts: list[str] = Field(default_factory=list)
    statutes: list[StatuteView] = Field(default_factory=list)
    process: FindingProcess | None = None


class VerdictView(BaseModel):
    status: str  # risk / partial / clean
    headline: str
    risk_count: int
    review_count: int
    conclusion_details: list[str] = Field(default_factory=list)
    process_details: list[str] = Field(default_factory=list)


class LimitsView(BaseModel):
    unmet_lines: list[str] = Field(default_factory=list)
    field_missing_lines: list[str] = Field(default_factory=list)
    premise_missing_lines: list[str] = Field(default_factory=list)
    premise_count: int = 0
    statutory_na_line: str | None = None


class MissingTermItem(BaseModel):
    term_id: str
    name: str
    status: str
    text: str
    statute: StatuteView


class MissingTermsView(BaseModel):
    summary: str
    short_note: str | None = None
    written_count: int | None = None
    absent_count: int = 0
    unseen_count: int = 0
    manual_count: int = 0
    items: list[MissingTermItem] = Field(default_factory=list)
    manual_names: list[str] = Field(default_factory=list)


class SuggestionView(BaseModel):
    text: str
    citation_text: str
    display_disclaimer: str | None = None
    sources: list[StatuteView] = Field(default_factory=list)


class ProcessView(BaseModel):
    cited_statute_count: int
    latency_s: float | None = None
    latency_text: str
    details: list[str] = Field(default_factory=list)
    finding_help: str
    clause_review_note: str | None = None
    verify_ok: bool
    traceability_text: str
    ask_rounds: int = 0
    ask_text: str
    degraded_visible: bool
    extract_note: str | None = None
    questions: list[str] = Field(default_factory=list)
    task_id: str
    token_in: int = 0
    token_out: int = 0
    run_detail: str


class TaskHead(BaseModel):
    task_id: str
    enterprise_id: str
    contract_id: str
    contract_name: str
    contract_chars: int
    created_at: str | None = None
    model: str
    model_first: bool
    clause_review: bool


class AskItemView(BaseModel):
    slot_id: str
    question: str
    source: str
    hint: str | None = None


class OptionalFieldView(BaseModel):
    field_id: str
    label: str
    hint: str | None = None
    question: str | None = None


class WorkflowView(BaseModel):
    state: str  # awaiting_facts / ready / history
    effective_contract_id: str
    ask_rounds: int = 0
    questions: list[str] = Field(default_factory=list)
    ask_items: list[AskItemView] = Field(default_factory=list)
    optional_fields: list[OptionalFieldView] = Field(default_factory=list)


class RefusalView(BaseModel):
    layer: str
    reason: str
    message: str
    guidance: str


class FactRow(BaseModel):
    label: str
    value: str


class CaseItemView(BaseModel):
    title: str
    holding: str
    source: str
    dispute_type: str
    source_url: str | None = None


class CaseBlockView(BaseModel):
    note: str
    summary: str
    items: list[CaseItemView] = Field(default_factory=list)


class TaskDetail(BaseModel):
    """结果页的唯一展示真源（006「结果 DTO」表）。"""

    task: TaskHead
    verdict: VerdictView
    findings: list[FindingView] = Field(default_factory=list)
    limits: LimitsView
    missing_terms: MissingTermsView
    suggestions: list[SuggestionView] = Field(default_factory=list)
    suggestion_empty_text: str | None = None
    facts: list[FactRow] = Field(default_factory=list)
    cases: CaseBlockView
    process: ProcessView | None = None
    refusal: RefusalView | None = None
    workflow: WorkflowView
    disclaimer: str


class QaProcessView(BaseModel):
    agent_trace: list[str] = Field(default_factory=list)
    token_in: int = 0
    token_out: int = 0
    detail_text: str


class QaResult(BaseModel):
    task_id: str
    parent_task_id: str
    refusal: RefusalView | None = None
    empty_text: str | None = None
    errors: list[str] = Field(default_factory=list)
    sources: list[StatuteView] = Field(default_factory=list)
    suggestions: list[SuggestionView] = Field(default_factory=list)
    process: QaProcessView | None = None


class HistorySummary(BaseModel):
    task_id: str
    contract_name: str
    created_at: str | None = None
    verdict: VerdictView
    suggestion_count: int
    latency_s: float | None = None
    latency_text: str
    refused: bool
    verify_ok: bool
    degraded_visible: bool
    ask_rounds: int = 0


class HistoryPage(Page[HistorySummary]):
    """`total` 是筛选后的条数，`total_all` 是 200 条窗口内的条数（不是全库总量）。"""

    total_all: int = 0
    deleted_count: int = 0
    window_size: int = 200


class TrendPoint(BaseModel):
    order: int
    label: str
    date: str
    risk_item_count: int
    suggestion_count: int


class TrendView(BaseModel):
    recent: int
    average: float
    max: int
    window_size: int
    note: str
    points: list[TrendPoint] = Field(default_factory=list)


# ---------------------------------------------------------------- 异步任务（006 /checks 与 /jobs）
class CheckIn(BaseModel):
    text: str | None = None
    contract_name: str | None = None
    contract_id: str | None = None
    clause_review: bool | None = None


class AnswersIn(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)
    skip: bool = False


class OptionalFactsIn(BaseModel):
    facts: dict[str, str] = Field(default_factory=dict)


class QaIn(BaseModel):
    """问答请求体：**只有 `question`**。

    上下文由服务端按 `_qa_context` 生成（规格 006 §2：不得接收合同正文或客户端注入上下文）。
    因此多余字段一律 400（`extra="forbid"`），而不是静默丢弃——静默丢弃会让"前端塞了上下文
    却被忽略"变成看不见的契约漂移。
    """

    model_config = ConfigDict(extra="forbid")

    question: str


class ActionView(BaseModel):
    type: str
    label: str


class ErrorBodyView(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    action: ActionView | None = None


class JobView(BaseModel):
    """`result` 是 `TaskDetail` 或 `QaResult` 的 JSON 形态（两者形状不同，按 `kind` 区分）。"""

    job_id: str
    kind: str  # check / recheck / qa
    status: str  # queued / running / succeeded / failed
    task_id: str | None = None
    result: dict[str, Any] | None = None
    error: ErrorBodyView | None = None
