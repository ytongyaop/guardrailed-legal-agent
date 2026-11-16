"""核对结论的一句话判定：把“0 条条目”翻译成用户读得懂的覆盖状态。

结果页顶部原先把 `len(task.items)` 直接当指标显示：离线环境（未配模型、缺事实）同样
显示“风险条目 0”，第一眼会读成“这份合同没问题”。本模块只做**表述**——输入是已经算
完的任务，输出 status / headline / details，不改规则判定、条款审查与必备条款的任何结论。

三态口径：
- `risk`：存在“与法条存在冲突的事项”（规则命中、或条款/模型结论的 A/B 级），条数照实报；
  模型主导路径（规格 003 P1-3）下 `items` 已含模型结论，只取其中 A/B 条目，不再叠加条款 A/B；
- `partial`：没有明确冲突，但还有“需人工复核”的 C 级条目，或覆盖不完整（条款审查没跑完、
  有缺事实判不了的规则、有“需人工确认”的必备条款类）；C 级条目只进 `review_count`，
  **绝不说成“冲突”**（把“待确认”当“确定冲突”是 A1 阶段修掉的那类误导）；
- `clean`：没有冲突、没有待复核，**且**三个通道都真跑过、都判得动，这时才敢说“未发现风险”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import MISSING_TERM_BASE, MISSING_TERM_UNJUDGED_ID, CheckTask, MissingStatus

SHORT_CONTRACT_CHARS = 400
# 条款审查的诊断里出现这些词，说明没跑起来或整批输出不可用；只能说“未完成”，
# 不能说“未发现”。关键字与界面 `_NOT_REVIEWED_MARKERS` 保持同一口径（同一张表）。
NOT_REVIEWED_MARKERS: tuple[str, ...] = ("未启用", "失败", "已忽略")
_REVIEW_ERROR_HINTS: tuple[str, ...] = ("条款审查", "条款判定")


@dataclass(frozen=True)
class Verdict:
    """一次核对的结论形态；`details` 是逐条覆盖明细，界面与 Word 报告共用。

    `risk_count` 只数“与法条存在冲突的事项”（A/B 级与规则命中）；`review_count` 只数
    “需人工复核 / 待确认”（C 级）。两者不混用：C 级不能进 headline 里的“冲突”条数。
    新增字段带默认值，旧调用方（只读 risk_count / status / headline / details）不受影响。
    """

    status: Literal["risk", "partial", "clean"]
    risk_count: int
    headline: str
    details: list[str]
    review_count: int = 0


def _review_errors(task: CheckTask) -> list[str]:
    """只挑条款审查相关的诊断，避免把抽取回退之类的无关报错算进来。"""
    return [str(e) for e in (task.errors or []) if any(hint in str(e) for hint in _REVIEW_ERROR_HINTS)]


def _review_failed(task: CheckTask) -> bool:
    """审查没跑起来 / 整批输出不可用（与界面 `_clause_review_note` 同一判断）。"""
    return any(marker in note for note in _review_errors(task) for marker in NOT_REVIEWED_MARKERS)


def _manual_count(task: CheckTask) -> int:
    """“需人工确认”的必备条款类数：第（九）类是兜底规格，本来就不判，不计入。"""
    return sum(
        1
        for term in (task.missing_terms or [])
        if term.status is MissingStatus.MANUAL and term.term_id != MISSING_TERM_UNJUDGED_ID
    )


def _clause_risk_count(task: CheckTask) -> int:
    """条款审查里算得上“与法条存在冲突”的条数：只算 A（明文冲突）/ B（显著失衡）。

    C 级是“待确认”，说成冲突过重——它不增 risk_count，但会让任务落到 partial。
    """
    return sum(1 for f in (task.clause_findings or []) if str(f.grade) in ("A", "B"))


def _clause_pending_count(task: CheckTask) -> int:
    """待人工确认的条款结论条数（C 级）：有它就不能宣称“未发现风险”。"""
    return sum(1 for f in (task.clause_findings or []) if str(f.grade) == "C")


# 算“冲突”的等级：A 明文冲突 / B 显著失衡；C 只是待确认。
_CONFLICT_GRADES = ("A", "B")


def _item_grade(item) -> str:
    return str(getattr(item, "grade", "") or "").strip().upper()


def _conflict_review_counts(task: CheckTask) -> tuple[int, int]:
    """(冲突条数, 需人工复核条数)：两条路径算法不同，靠任务标志区分（不读环境变量）。

    - 模型主导路径（`task.model_first=True`）：`items` 已由共识层合并模型结论与程序复核条目，
      A/B 计冲突、其余（C 级与无法定级）计“需人工复核”，两级不混用、也不叠加条款 A/B；
    - 旧路径：`items` 只有规则条目（一律算冲突），条款审查的 A/B 照旧叠加；
      条款 C 级仍由 `_details` 的“N 条条款待人工确认”那一行体现，`review_count` 保持 0，
      旧路径的 headline / status / details 一个字都不变。
    """
    items = list(task.items or [])
    if bool(task.model_first):
        conflict = sum(1 for item in items if _item_grade(item) in _CONFLICT_GRADES)
        return conflict, len(items) - conflict
    return len(items) + _clause_risk_count(task), 0


def _details(task: CheckTask) -> list[str]:
    """覆盖明细：有数据才输出该行，顺序固定（规则 → 条款审查 → 需人工复核 → 缺事实 → 必备条款 → 长度）。"""
    rows: list[str] = []
    coverage = task.rule_coverage
    if coverage is not None and coverage.total:
        judged = coverage.violated + coverage.compliant + coverage.not_applicable
        rows.append(f"规则 {judged}/{coverage.total} 可判定")
    if not task.clause_review:
        rows.append("条款审查未运行")
    elif _review_failed(task):
        rows.append("条款审查未完成")
    else:
        rows.append("条款审查已运行")
    clause_risk = _clause_risk_count(task)
    if clause_risk:
        rows.append(f"条款审查发现 {clause_risk} 条明确冲突或显著失衡")
    clause_pending = _clause_pending_count(task)
    # 新路径的 C 级已随 items 计入下面“N 条需人工复核”，这里不再重复报一次同一个数。
    if clause_pending and not bool(task.model_first):
        rows.append(f"{clause_pending} 条条款待人工确认")
    review_count = _conflict_review_counts(task)[1]
    if review_count:
        rows.append(f"{review_count} 条需人工复核")
    undetermined = list(task.undetermined or [])
    if undetermined:
        rows.append(f"{len(undetermined)} 条规则因缺少事实无法判定")
    terms = list(task.missing_terms or [])
    if terms and task.missing_coverage is not None:
        written = sum(
            1
            for term in terms
            if term.status is MissingStatus.WRITTEN and term.term_id != MISSING_TERM_UNJUDGED_ID
        )
        rows.append(f"必备条款 {written}/{MISSING_TERM_BASE} 已写")
        manual = _manual_count(task)
        if manual:
            rows.append(f"{manual} 类需人工确认")
    if task.contract_chars < SHORT_CONTRACT_CHARS:
        rows.append("合同正文较短，结论可能不完整")
    return rows


def _fully_covered(task: CheckTask) -> bool:
    """三个通道都真跑过、都判得动，才允许说“未发现风险”。

    “真跑过”除了 `clause_review` 为 True，还要没有“已忽略 / 失败”这类诊断——
    整批输出不可用的审查不算跑完，否则顶部说 clean、明细说未完成，自相矛盾。
    有 C 级“待确认”也说明覆盖不完整：不能一边列着待确认条款一边说“未发现风险”。
    """
    return (
        bool(task.clause_review)
        and not _review_failed(task)
        and _clause_pending_count(task) == 0
        and not list(task.undetermined or [])
        and _manual_count(task) == 0
    )


def summarize_verdict(task: CheckTask) -> Verdict:
    """把任务变成三态结论；同一个任务必然得到同一份输出（纯函数，不改入参）。

    headline 只承诺它数得清的东西：冲突条数只含 A/B 与规则命中；C 级（含程序追加的
    数值复核条目）单独报“需人工复核”，宁可说“要人看”，也不把待确认说成确定冲突。
    """
    conflict, review = _conflict_review_counts(task)
    details = _details(task)
    if conflict:
        return Verdict(
            status="risk",
            risk_count=conflict,
            headline=f"发现 {conflict} 条与法条存在冲突的事项",
            details=details,
            review_count=review,
        )
    if review:
        return Verdict(
            status="partial",
            risk_count=0,
            headline=f"未发现明确冲突，但有 {review} 条需人工复核",
            details=details,
            review_count=review,
        )
    if _fully_covered(task):
        return Verdict(status="clean", risk_count=0, headline="未发现风险", details=details)
    return Verdict(
        status="partial",
        risk_count=0,
        headline="未发现硬性风险，但覆盖不完整",
        details=details,
    )
