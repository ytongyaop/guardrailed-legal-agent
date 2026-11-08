"""内核数据结构（唯一真源）。

`source_id` 约定前缀（校验器据此区分依据类型）：
- 法条：法律简称编号，如 `LCL-19`（劳动合同法第 19 条）
- 规则：`R-` 开头，如 `R-PROBATION-001`
- 案例：`C-` 开头，如 `C-2024-01`
法条/规则可作结论依据；案例只能作"类案参照"，不得当结论依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class ItemKind(StrEnum):
    RISK = "风险"
    MISSING = "缺项"
    UNKNOWN = "待确认"


class Severity(StrEnum):
    HARD = "硬性"
    SOFT = "参考"


class StatuteStatus(StrEnum):
    EFFECTIVE = "有效"
    EXPIRED = "失效"
    SUPERSEDED = "被替代"


class StatuteLevel(StrEnum):
    LAW = "法律"
    ADMIN = "行政法规"
    JUDICIAL = "司法解释"
    DEPT = "部门规章"
    LOCAL = "地方规定"


class TaskMode(StrEnum):
    CHECK = "核对"
    QA = "问答"


class SourceKind(StrEnum):
    STATUTE = "法条"
    RULE = "规则"
    CASE = "案例"


class Role(StrEnum):
    USER = "用户"
    ADMIN = "管理员"


class MissingStatus(StrEnum):
    """必备条款的"有没有写"四态（不代表内容合法）。"""

    WRITTEN = "已写"
    ABSENT = "缺失"
    UNSEEN = "未见"
    MANUAL = "需人工确认"


def source_kind(source_id: str) -> SourceKind:
    """按前缀判定依据类型（契约见模块 docstring）。"""
    if source_id.startswith("R-"):
        return SourceKind.RULE
    if source_id.startswith("C-"):
        return SourceKind.CASE
    return SourceKind.STATUTE


@dataclass(frozen=True)
class StatuteEntry:
    """法源条目：条文级，带时效与效力层级。"""

    statute_id: str
    law_name: str
    article_no: str
    text: str
    issuing_body: str
    doc_no: str = ""
    effective_date: date | None = None
    expiry_date: date | None = None
    status: StatuteStatus = StatuteStatus.EFFECTIVE
    superseded_by: str | None = None
    level: StatuteLevel = StatuteLevel.LAW
    region: str = "全国"
    source_url: str = ""
    note: str = ""  # 补充说明：如「解释（二）第 21 条废止本条第一款」，不影响检索


@dataclass(frozen=True)
class RuleEntry:
    """确定性规则：把法条里的数值/枚举变成可判定条件。

    `op/threshold` 描述"合法区间"；`when_*` 是可选前提（最多两条，用于区间，如"一年以上不满三年"）。
    `when_mode` 决定两组前提的关系：`and`（默认，都满足才适用）/ `or`（任一满足即适用，
    用于"三年以上固定期限**或**无固定期限"这类并列情形）。
    `evidence_span` 必须是 `statute_id` 原文的子串——规则的每个数字都要能在法条里找到出处。
    """

    rule_id: str
    name: str
    field: str
    op: str
    threshold: float | str
    statute_id: str
    unit: str = ""
    severity: Severity = Severity.HARD
    effective_date: date | None = None
    note: str = ""
    when_field: str = ""
    when_op: str = ">="
    when_value: float | str = ""
    when_field2: str = ""
    when_op2: str = "<"
    when_value2: float | str = ""
    when_mode: str = "and"  # 两组前提的关系：and（且，默认）/ or（或）
    evidence_span: str = ""


@dataclass(frozen=True)
class CaseEntry:
    """官方典型案例：只作类案参照，不作结论依据。"""

    case_id: str
    title: str
    source: str
    holding: str
    statute_ids: tuple[str, ...] = ()
    batch: str = ""
    publish_date: date | None = None
    dispute_type: str = ""
    region: str = "全国"
    level: str = ""
    source_url: str = ""  # 官方发布页链接；空串表示未记录，界面与报告都不显示


@dataclass(frozen=True)
class SlotSpec:
    """必问事实槽位；blocking=True 表示缺了不能出结论。"""

    slot_id: str
    name: str
    question: str
    blocking: bool = True
    needed_by: tuple[str, ...] = ()


@dataclass
class CheckItem:
    """核对清单条目：风险 / 缺项 / 待确认。"""

    item_id: str
    kind: ItemKind
    clause_ref: str
    finding: str
    source_id: str
    evidence_text: str = ""
    severity: Severity = Severity.SOFT
    rule_id: str = ""


@dataclass
class Suggestion:
    """处理建议：生成式表述，必须带依据，必须带免责声明。"""

    text: str
    source_ids: tuple[str, ...]
    disclaimer: str = "非法律意见，仅供参考；重大争议请咨询专业人士。"


@dataclass
class CaseRef:
    """类案参照（界面第三区）。"""

    case_id: str
    holding: str
    note: str = "同类情形通常如何认定，不构成本案结论。"


@dataclass(frozen=True)
class ClauseFinding:
    """条款级审查结论（存在性域）：模型判定 + 程序核验后才允许出现。

    - `grade`：A 明确冲突（法条明文禁止、无需补事实）/ B 显著失衡（需补事实或含裁量）/ C 待确认；
    - `span` 一律取自触发器候选片段（不采信模型改写的定位），保证是合同原文子串；
    - `trigger_id` 为空串表示模型自提（`source="model_extra"`），自提一律封顶 C 级；
    - `statute_ids` 已通过闭集核验：全部存在于法源库，触发器条目还需落在该候选的候选集内。
    """

    paragraph: int
    span: str
    grade: str  # A / B / C
    reason: str  # 与哪条法条冲突、冲突点是什么（不写"无效/违法"终局结论）
    statute_ids: tuple[str, ...] = ()
    trigger_id: str = ""  # 空字符串表示模型自提
    source: str = "trigger"  # trigger / model_extra
    need_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class UndeterminedRule:
    """一条因缺字段而判不了的规则（旁路留痕，不进 `items`）。

    `reason` 只有两种取值：`field_missing` 表示前提已满足、判定字段缺失（真正该判却判不了），
    `premise_missing` 表示前提字段缺失（连是否适用都不确定）。两者不能合并：
    前者该补事实，后者要连前提一起补，提示语不同。
    """

    rule_id: str
    name: str
    statute_id: str
    missing_fields: tuple[str, ...]
    reason: str  # field_missing / premise_missing


@dataclass(frozen=True)
class UnmetTerm:
    """一条"合同未约定"的必备约定（旁路留痕，不进 `items`）。

    与 `UndeterminedRule` 的区别：这里缺的字段是法律要求写进合同的（`required`），
    合同没写就是缺项，不该说成"信息不足判不了"。
    """

    rule_id: str
    name: str
    statute_id: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RuleCoverage:
    """规则层的结局分布：四态计数之和恒等于 `total`（规则表条数）。"""

    total: int
    violated: int
    compliant: int
    not_applicable: int
    unknown: int


@dataclass(frozen=True)
class MissingTerm:
    """一类必备条款的"有没有写"判定（不判内容合法性）。

    `status`：已写 / 缺失（certainty=高且未检出）/ 未见（certainty=低，只请人确认）/
    需人工确认（正文太短，或该类本身不判）；`note` 说明为什么是当前状态。
    """

    term_id: str
    name: str
    clause_no: str
    statute_id: str
    status: MissingStatus
    certainty: str
    note: str = ""


MISSING_TERM_BASE = 8  # 第十七条前八类参与覆盖率；第九类是兜底条款，不判
MISSING_TERM_UNJUDGED_ID = "MT-9"


@dataclass
class CheckTask:
    """一次核对/问答任务：含指标留痕字段（延迟/成本/消融档/拒答层）。"""

    task_id: str
    mode: TaskMode
    enterprise_id: str = ""
    contract_id: str = ""
    contract_name: str = ""  # 合同名快照（历史自包含，合同删了也能看懂）
    contract_chars: int = 0  # 正文长度，用于判断“0 风险”是否因条款太少
    variant: str = "full"  # llm_only / llm_rules / full（三档消融分组）
    model: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    items: list[CheckItem] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    case_refs: list[CaseRef] = field(default_factory=list)
    ask_history: list[dict[str, str]] = field(default_factory=list)  # 补问留痕（指标 4/5）
    ask_rounds: int = 0  # 本次实际补问轮数
    questions: list[str] = field(default_factory=list)  # 最近一轮追问（界面承接 / 指标 4）
    ask_items: list[dict] = field(default_factory=list)  # 追问的结构化形态（字段 id + 问句 + 来源）
    refused: bool = False  # 是否越界拒答（界面据此显示判定层与理由）
    boundary_layer: str = (
        ""  # 拒答由哪一层触发：terms / default（规则判定）、facts（无正文）、llm（问答拒答）
    )
    boundary_reason: str = ""
    verify_ok: bool = True  # 依据校验是否通过（不是"有没有条目"）
    degraded: bool = False  # 是否降级（补问超限或依据不足）
    errors: list[str] = field(default_factory=list)
    agent_trace: list[str] = field(default_factory=list)  # 问答时的工具调用轨迹（可解释证据）
    clause_review: bool = False  # 本次是否真的跑了条款审查（历史详情据此区分「未做」与「未发现」）
    clause_findings: list[ClauseFinding] = field(default_factory=list)  # 条款审查结论（存在性域）
    undetermined: list[UndeterminedRule] = field(default_factory=list)  # 判不了的规则（缺字段，不混进 items）
    unmet: list[UnmetTerm] = field(default_factory=list)  # 合同未约定的必备约定（旁路，不进 items）
    statutory_na: int = 0  # 因合同未约定、按法定标准适用而不判的规则条数
    rule_coverage: RuleCoverage | None = None  # 规则层四态分布；未跑规则层时为空
    missing_terms: list[MissingTerm] = field(default_factory=list)  # 必备条款缺项（旁路，不进 items）
    latency_s: float | None = None
    token_in: int = 0
    token_out: int = 0
    deleted: bool = False  # 软删：历史页不显示，操作日志仍留痕
    created_at: datetime | None = None
    finished_at: date | None = None

    @property
    def missing_coverage(self) -> float | None:
        """必备条款覆盖率 = 已写类数 / 8（第九类不判、不进分母）；无已判类时为 None。"""
        judged = [
            t
            for t in self.missing_terms
            if t.term_id != MISSING_TERM_UNJUDGED_ID and t.status != MissingStatus.MANUAL
        ]
        if not judged:
            return None
        return sum(1 for t in judged if t.status == MissingStatus.WRITTEN) / MISSING_TERM_BASE


@dataclass(frozen=True)
class VerifyReport:
    """校验器输出：ok=True 才算"依据完整"。"""

    ok: bool
    errors: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class User:
    """账号：密码只存 pbkdf2 哈希与盐，不存明文。"""

    user_id: str
    username: str
    password_hash: str
    salt: str
    role: Role = Role.USER
    enterprise_id: str = ""
    created_at: str = ""
    onboarded: bool = False


@dataclass(frozen=True)
class UserDeletion:
    """账号硬删结果：deleted=False 时 reason 说明拒绝原因，各项计数保持 0。"""

    deleted: bool
    username: str = ""
    enterprise_id: str = ""
    contracts: int = 0
    tasks: int = 0
    logs: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Enterprise:
    """企业档案：只存企业级用工事实，不采集员工个人信息。"""

    enterprise_id: str
    name: str
    # 三项不给默认值：空串表示还没填，核对时按缺失处理并追问。
    # 旧库里已有的默认值原样读取，不做迁移（无法区分默认值与真实填写）。
    employer_type: str = ""
    staff_size: int = 0
    working_hours: str = ""
    social_insurance: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ContractRecord:
    """合同档案：原始文本 + 文件名（不存员工个人信息）。"""

    contract_id: str
    enterprise_id: str
    filename: str
    text: str
    uploaded_at: str = ""


@dataclass(frozen=True)
class OperationLog:
    """操作日志：谁、对哪个任务、做了什么（对应模块 10）。"""

    log_id: str
    task_id: str
    user_id: str
    action: str
    detail: str = ""
    created_at: str = ""
