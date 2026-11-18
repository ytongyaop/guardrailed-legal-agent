"""请求 / 响应 DTO（规格 006 §2）。

规则（与 006 一致）：
- 可空字段显式 `null`，不省略键；时间一律 ISO-8601 字符串；
- 响应里**不出现**口令、哈希、盐、模型 Key 以及 Key 的长度；
- `*Row` 是 `/admin` 列表用的展示行（对应 006 基础 DTO 的 `StatuteEntry` / `RuleEntry` /
  `OperationLog`）；`*View` 留给批次 3 的结果 DTO，避免同名不同义。
"""

from __future__ import annotations

from datetime import date
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

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
