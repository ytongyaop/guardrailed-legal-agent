"""共识层：模型结论 × 数值校验的对账（规格 003 第 5.4 节；实施步骤第 13 章 P1-2）。

纯函数：不读文件、不调模型、不写库、不做分支决策。本阶段**不接线**——P1-3 才由汇总层
调用它来产出最终条目，因此本模块对产品行为零影响。

对齐方式（规格要求，不看自然语言理由）：

    一条 `ClauseFinding` 与一条 `RuleCheck` 关联  ⟺  finding.statute_ids 包含 check.statute_id

`statute_ids` 已经过闭集核验，是机械可判的事实；用"理由文本相似"判断会不稳定、也不可测。

冲突矩阵（7 种情形，逐条实现在 `_pair_rows` / `reconcile` 里）：

| # | 规则状态 | 模型结论 | verdict | 处置 |
| --- | --- | --- | --- | --- |
| 1 | mismatch | A/B/C 且 statute_ids 含该 statute | keep | 保留，附注"数值校验一致" |
| 2 | mismatch | 无任何 finding 关联该 statute | review_required | 追加 C 级复核条目（程序来源，非模型结论） |
| 3 | mismatch | 关联但模型说"合规/未发现问题" | demote | 保留但降一级（A→B→C，C 保持 C） |
| 4 | ok 且 actual 非空 | A/B 且关联 | demote | 降一级 |
| 5 | ok 且 actual 为空 | 任意 | keep | 不降级（前提不满足，校验未介入） |
| 6 | needs_fact | 任意 | keep | 不出结论，只记一句；`check.field` 留给追问层 |
| 7 | severity="prompt" 且 mismatch | 任意 | annotate | 只记附注，不改等级、不生成条目 |

等级只降不升：A→B→C，C 降不动就保持 C；等级缺失或越域一律按 C 处理（宁可低估，不误升级）。

已知边界（留给 P1-3 决定，本阶段不改）：同一条 finding 若同时被多条 check 命中，会按"每对
(finding, check)"各出一行，因此它可能同时出现在"降级"行与"保留"行里；聚合时取最严处置的
口径未定，见本阶段回报的未决问题。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from ..domain.models import ClauseFinding
from ..domain.rules import RuleCheck, RuleCheckStatus

# verdict 取值（规格 5.4；提示级规则下线后仍保留 annotate，供 severity="prompt" 的兼容分支）
VERDICT_KEEP = "keep"
VERDICT_DEMOTE = "demote"
VERDICT_ANNOTATE = "annotate"
VERDICT_REVIEW_REQUIRED = "review_required"

# 内部等级枚举与最低档（与 `services/clause_review.py:37` 的 GRADES 同值；本模块不反向依赖它）
GRADES = ("A", "B", "C")

# 给用户看的一句说明（常量集中在这里，测试直接断言，避免文案改动漏测）
NOTE_CONSISTENT = "数值校验一致"
NOTE_CONFLICT = "模型与数值校验冲突"
NOTE_CONTRADICT = "模型结论与数值区间矛盾"
NOTE_PREMISE = "该条前提不满足，数值校验未介入"
NOTE_NEEDS_FACT = "缺事实，未做数值校验"
NOTE_ANNOTATE = "提示级数值校验不一致，仅附注、不改等级"
NOTE_NO_CHECK = "无相关数值校验"
NOTE_GRADE_FLOOR = "等级已为最低，不再降级"

# 表 3 的"模型说合规 / 未发现问题"判据：等级为空，或理由含下列表述之一。
# 这是一份可枚举的机械判据（字符串包含），不是语义判断——宁可少降一级，也不猜。
_COMPLIANT_MARKS = (
    "合规",
    "未发现",
    "未见异常",
    "不存在问题",
    "无问题",
    "没有发现问题",
    "不违反",
    "符合规定",
    "符合要求",
    "无需修改",
    "未违规",
    "不存在风险",
)

# 只有在"数值校验说合规、模型说有事"时才降级（表 4）；C 已是最低，降不动。
_DEMOTABLE = ("A", "B")

# 表 2 追加条目的段落号哨兵：程序追加的复核条目没有对应的合同段落（与
# `clause_review._unverified` 定位失败时取 0 的口径一致），span 同理为空串
# ——`span` 的既有不变量是"合同原文子串"，这里绝不塞法条原文去冒充。
NO_PARAGRAPH = 0


@dataclass(frozen=True)
class ConsensusRow:
    """一行对账结果：模型结论（可空）+ 数值校验（可空）+ 处置 + 一句中文说明。

    `verdict` 只用于展示与统计；真正落到条目上的是 `finding` 本身——降级行里给的是
    **降过级的副本**（`ClauseFinding` 是 frozen，用 `dataclasses.replace` 产生），
    原对象一个字段都不改。
    """

    finding: ClauseFinding | None
    check: RuleCheck | None
    verdict: str
    note: str


def _grade_rank(grade: str) -> int:
    """排序用等级序号；未知等级排在 A/B/C 之后（不假设模型一定守枚举）。"""
    return GRADES.index(grade) if grade in GRADES else len(GRADES)


def _norm_grade(grade: str) -> str:
    return str(grade or "").strip().upper()


def _demote(finding: ClauseFinding) -> ClauseFinding:
    """降一级（A→B、B→C、C 保持 C）；未知等级直接落到 C——任何路径都不升等级。"""
    current = _norm_grade(finding.grade)
    if current not in GRADES:
        return replace(finding, grade=GRADES[-1])
    index = GRADES.index(current)
    return replace(finding, grade=GRADES[min(index + 1, len(GRADES) - 1)])


def _claims_compliant(finding: ClauseFinding) -> bool:
    """模型是不是在说"这条没问题/未发现问题"（表 3 的判据，参看 `_COMPLIANT_MARKS`）。"""
    if not _norm_grade(finding.grade):
        return True
    reason = str(finding.reason or "")
    return any(mark in reason for mark in _COMPLIANT_MARKS)


def _related(finding: ClauseFinding, check: RuleCheck) -> bool:
    """对齐判据（唯一口径）：`statute_id` 交集。空 `statute_id` 不参与对齐，避免误配。"""
    statute_id = str(check.statute_id or "").strip()
    return bool(statute_id) and statute_id in finding.statute_ids


def _is_prompt(check: RuleCheck) -> bool:
    return str(check.severity or "").strip().lower() == "prompt"


def _article_no(statute_id: str) -> str:
    """从 `statute_id` 取条号（`LCL-19` → `19`）：库内 143 条全部是 `短名-数字` 形态。"""
    return str(statute_id or "").rsplit("-", 1)[-1].strip()


def _review_text(statute_id: str) -> str:
    """表 2 的追加文案：只写"与第 X 条不一致，需人工复核"，不写"这是风险"。"""
    article = _article_no(statute_id)
    if article:
        return f"数值与第 {article} 条不一致，需人工复核"
    return "数值与法条要求不一致，需人工复核"


def _review_entry(check: RuleCheck) -> ClauseFinding:
    """表 2 追加的内部 C 级条目：程序产出，不是模型结论。"""
    statute_id = str(check.statute_id or "").strip()
    return ClauseFinding(
        paragraph=NO_PARAGRAPH,
        span="",
        grade=GRADES[-1],
        reason=_review_text(statute_id),
        statute_ids=(statute_id,) if statute_id else (),
        trigger_id="",
        source="rule_check",
    )


def _pair_rows(finding: ClauseFinding, check: RuleCheck) -> list[ConsensusRow]:
    """一条关联成立时按状态给出处置（表 1 / 3 / 4 / 5 / 6 / 7）。"""
    if check.status is RuleCheckStatus.MISMATCH:
        if _is_prompt(check):
            # 表 7：提示级只记附注——不改等级、不生成条目
            return [ConsensusRow(finding, check, VERDICT_ANNOTATE, NOTE_ANNOTATE)]
        if _claims_compliant(finding):
            # 表 3：方向相反，降一级（C 保持 C）
            return [ConsensusRow(_demote(finding), check, VERDICT_DEMOTE, NOTE_CONFLICT)]
        # 表 1：模型已指向同一数值点
        return [ConsensusRow(finding, check, VERDICT_KEEP, NOTE_CONSISTENT)]

    if check.status is RuleCheckStatus.OK:
        if not str(check.actual or "").strip():
            # 表 5：`actual` 为空实为"前提不满足"，这是防误伤的关键守卫——不得降级
            return [ConsensusRow(finding, check, VERDICT_KEEP, NOTE_PREMISE)]
        if _norm_grade(finding.grade) in _DEMOTABLE:
            # 表 4：数值落在法条区间内，模型却说有事（A/B 才降；C 降不动）
            return [ConsensusRow(_demote(finding), check, VERDICT_DEMOTE, NOTE_CONTRADICT)]
        return [ConsensusRow(finding, check, VERDICT_KEEP, NOTE_GRADE_FLOOR)]

    # 表 6：needs_fact 不进结论；`check.field` 随行返回，留给追问层复用（本阶段不接线）
    return [ConsensusRow(finding, check, VERDICT_KEEP, NOTE_NEEDS_FACT)]


def _row_key(row: ConsensusRow) -> tuple[str, int, int, str, str, str, str]:
    """排序键：statute_id 升序 → 段落号升序 → 等级 → 其余（保证同输入幂等、结果全序）。"""
    if row.check is not None:
        statute_id = str(row.check.statute_id or "").strip()
        rule_id = row.check.rule_id
    else:
        statute_id = min(row.finding.statute_ids) if row.finding and row.finding.statute_ids else ""
        rule_id = ""
    paragraph = row.finding.paragraph if row.finding is not None else NO_PARAGRAPH
    grade = _grade_rank(_norm_grade(row.finding.grade) if row.finding is not None else "")
    span = row.finding.span if row.finding is not None else ""
    return (statute_id, paragraph, grade, row.verdict, span, row.note, rule_id)


def reconcile(
    findings: Sequence[ClauseFinding],
    checks: Sequence[RuleCheck],
) -> tuple[ConsensusRow, ...]:
    """对账入口：模型结论集合 × 数值校验集合 → 排序稳定、可复算的行序列。

    纯函数：同一输入两次调用输出逐字段相同（排序键已全序化，且不读环境、不读时间）。
    未被任何 check 关联的 finding 仍会返回（`keep` + `check=None`），否则模型结论会被
    这个函数静默丢掉——那正是本规格要避免的事。
    """
    finding_list = list(findings)
    check_list = list(checks)

    rows: list[ConsensusRow] = []

    # 1) 以 finding 为主线：关联成立就按矩阵处置；没有任何关联就原样保留并注明无校验
    for finding in finding_list:
        related = [check for check in check_list if _related(finding, check)]
        if not related:
            rows.append(ConsensusRow(finding, None, VERDICT_KEEP, NOTE_NO_CHECK))
            continue
        for check in related:
            rows.extend(_pair_rows(finding, check))

    # 2) 没有任何 finding 关联的 check：只有 mismatch 会在结论里留下东西（表 2 / 表 7），
    #    ok / needs_fact 既不改结论也不该凭校验凭空造条目，因此不出行
    for check in check_list:
        if any(_related(finding, check) for finding in finding_list):
            continue
        if check.status is not RuleCheckStatus.MISMATCH:
            continue
        if _is_prompt(check):
            rows.append(ConsensusRow(None, check, VERDICT_ANNOTATE, NOTE_ANNOTATE))
            continue
        entry = _review_entry(check)
        rows.append(ConsensusRow(entry, check, VERDICT_REVIEW_REQUIRED, _review_text(check.statute_id)))

    # 3) 去重（同一对 (finding, check) 只留一行）后按全序键排序
    unique: list[ConsensusRow] = []
    seen: set[ConsensusRow] = set()
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        unique.append(row)
    unique.sort(key=_row_key)
    return tuple(unique)
