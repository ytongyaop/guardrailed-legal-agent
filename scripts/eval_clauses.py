"""条款级评测：三条臂 × 五个指标，结果落盘便于直接引用。

    python scripts/eval_clauses.py                  # 三臂 × 3 次（默认合同 contract_traps_01.txt）
    python scripts/eval_clauses.py --runs 1         # 只跑 1 次（省时）
    python scripts/eval_clauses.py --contracts src/mla/data/evalsets/contract_traps_01.txt
    # 多合同：逐合同各出一张表，末尾给汇总表
    python scripts/eval_clauses.py --runs 1 --contracts \
        src/mla/data/evalsets/contract_traps_01.txt src/mla/data/evalsets/contract_traps_02.txt \
        src/mla/data/evalsets/contract_traps_03.txt src/mla/data/evalsets/contract_compliant_01.txt

三条臂：
    rule_only  只有现有 20 条数值规则出结论（不跑触发器、不调模型）；规则命中的 rule_id
               按本脚本里的人工映射对到 gold 正样本，对不上的记为未覆盖。
    model_raw  触发器 + 模型，**关闭**程序核验（`review_clauses(verify=False)`）：
               等级与法条引用原样保留，库外条号单列 `out_of_library` 计数。
    full       触发器 + 模型 + 程序核验（现有实现）。

五个指标（覆盖率与分级一致率按 gold 条计，引用准确率按引用条计）：
    1. 覆盖率          findings 的 span 与 gold span 重叠 ≥50% 的正样本条数 / 该合同正样本数
    2. A 级误报        该合同负样本里被判 A 的条数
    3. 引用准确率      结论引用的法条落在对应 gold `statute_ids` 内的比例（库外条号单列）
    4. span 定位率     结论的 span 能在合同原文（`_norm` 后）定位的比例
    5. 分级一致率      与 gold `grade` 完全一致的条数 / 覆盖条数；另给相邻容忍口径
                      （完全一致 1.0 + A↔B 相邻 0.5）
附带：model_extra 条数、耗时、token、多次重跑的等级稳定率。

多合同口径：gold 按 `gold.contract` 分组，逐合同独立算五指标；末尾「汇总」表把
各合同**最后一轮**的原始计数相加后重算比率（不是把比率取平均）。gold 里没有标注的合同
打 WARN，只报候选数 / 结论数 / 等级分布，准确率列显示 `-`。

另做跨合同噪音检查（不算准确率、不编 gold）：对 `eval_generalization.jsonl` 的 12 条合同
各跑一次 `full` 臂，只统计候选数、findings 数、A/B/C 分布、耗时、token，并给出前 10 条
结论的等级与片段前 40 字，供人工判断有无明显离谱结论。

输出：`src/mla/data/evalsets/last_clause_eval.json`（覆盖写）+ `clause_eval_history.jsonl`（同目录追加）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mla.adapters.extract import RegexExtractor, _norm  # noqa: E402
from mla.adapters.llm import DeepSeekLLM  # noqa: E402
from mla.adapters.rag import load_statutes  # noqa: E402
from mla.config import data_path, load_config  # noqa: E402
from mla.domain.clauses import Trigger, detect_clauses, load_triggers  # noqa: E402
from mla.domain.models import RuleEntry, Severity  # noqa: E402
from mla.domain.rules import evaluate, load_rules  # noqa: E402
from mla.ports.generation import Generator  # noqa: E402
from mla.services.clause_review import review_clauses  # noqa: E402

GRADE_ORDER = ("A", "B", "C")
NEGATIVE_GRADE = "合规"
COVER_MIN = 0.5  # 覆盖判定门槛：gold 片段被结论片段覆盖 ≥50%
DEFAULT_CONTRACT = data_path("evalsets/contract_traps_01.txt")
DEFAULT_GOLD = data_path("evalsets/clause_gold.jsonl")
DEFAULT_OUT = data_path("evalsets/last_clause_eval.json")
DEFAULT_HISTORY = data_path("evalsets/clause_eval_history.jsonl")
GENERALIZATION = data_path("evalsets/eval_generalization.jsonl")
PREVIEW_CHARS = 40
PREVIEW_ROWS = 10

# 规则 → gold 正样本的人工映射（`rule_only` 臂专用；**不改 gold 文件**）。
# 判定口径：规则的 statute_id 与 gold.statute_ids 对得上，且规则字段就是该陷阱的数值形态。
# 逐条核过全部 20 条规则，只有下面两处属于"同一数值形态"：
#   - R-PROBATION-WAGE-80 ↔ T05：规则字段 probation_wage_ratio 正是"试用期工资按基本工资 80%"的比例
#     （gold T05 引 LCL-20，规则挂 LCR-15，同一条规范的两处出处）；
#   - R-NONCOMPETE-TERM-2Y ↔ T12：规则字段 noncompete_years 正是"竞业限制 2 年"的期限
#     （gold T12 引 LCL-24）。
# 其余不映射，理由举三例：R-PROBATION-6M 只对应合法对照组 N03；R-OT-* 看加班时数/倍数，
# 而 T03 是"放弃加班费"的存在性问题；R-WPR-MONTHLY-PAY 看每月支付次数，T19 是"延迟 90 日"。
# 未映射的规则命中一律记为未覆盖——规则层对"条款存在性违法"的结构性缺口正是本对照的意义。
RULE_TO_GOLD: dict[str, tuple[str, ...]] = {
    "R-PROBATION-WAGE-80": ("T05",),
    "R-NONCOMPETE-TERM-2Y": ("T12",),
}


@dataclass(frozen=True)
class GoldRow:
    """gold 集的一行（只取指标要用的字段）。"""

    gold_id: str
    contract: str
    span: str
    grade: str
    statute_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArmFinding:
    """一条结论的指标视图（与 `domain.ClauseFinding` 解耦，便于纯函数单测）。"""

    span: str
    grade: str
    statute_ids: tuple[str, ...] = ()
    gold_ids: tuple[str, ...] = ()  # 仅 rule_only 臂使用：人工映射到的 gold 正样本
    source: str = "trigger"  # trigger / model_extra / rule
    paragraph: int = 0
    trigger_id: str = ""


# ---------- 纯函数：指标计算 ----------


def load_gold(path: str | Path) -> tuple[GoldRow, ...]:
    """读 gold JSONL（只读，不校验；校验交给 `scripts/validate_clause_gold.py`）。"""
    rows: list[GoldRow] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        raw_ids = raw.get("statute_ids") or []
        rows.append(
            GoldRow(
                gold_id=str(raw["gold_id"]),
                contract=str(raw["contract"]),
                span=str(raw["span"]),
                grade=str(raw["grade"]),
                statute_ids=tuple(str(x) for x in raw_ids if isinstance(x, str)),
            )
        )
    return tuple(rows)


def locate(span: str, contract_text: str) -> tuple[int, int] | None:
    """span 在**归一化**合同里的起止下标（口径同 `validate_clause_gold`：`_norm` 后子串）。"""
    target = _norm(span)
    if not target:
        return None
    pos = _norm(contract_text).find(target)
    if pos < 0:
        return None
    return pos, pos + len(target)


def span_coverage(gold_span: str, finding_span: str, contract_text: str) -> float:
    """gold 片段被结论片段覆盖的比例（分母取 gold 长度，位置重叠优先）。

    两段都能在原文定位才有重叠可言；任一定位不到（例如模型证据是改写文本）一律 0.0。
    """
    gold_interval = locate(gold_span, contract_text)
    finding_interval = locate(finding_span, contract_text)
    if gold_interval is None or finding_interval is None:
        return 0.0
    start = max(gold_interval[0], finding_interval[0])
    end = min(gold_interval[1], finding_interval[1])
    width = gold_interval[1] - gold_interval[0]
    if width <= 0:
        return 0.0
    return max(0, end - start) / width


def match_by_span(
    golds: Sequence[GoldRow],
    findings: Sequence[ArmFinding],
    contract_text: str,
    min_ratio: float = COVER_MIN,
) -> dict[str, int | None]:
    """每条 gold 是否被某条结论覆盖；返回 gold_id → 覆盖它的结论下标（未覆盖为 None）。

    取重叠比例最高的那条结论，达到 `min_ratio` 才算覆盖（一个结论可覆盖多条 gold）。
    """
    out: dict[str, int | None] = {}
    for gold in golds:
        best_index: int | None = None
        best_ratio = 0.0
        for index, finding in enumerate(findings):
            ratio = span_coverage(gold.span, finding.span, contract_text)
            if ratio > best_ratio:
                best_index, best_ratio = index, ratio
        out[gold.gold_id] = best_index if best_ratio >= min_ratio else None
    return out


def match_by_mapping(golds: Sequence[GoldRow], findings: Sequence[ArmFinding]) -> dict[str, int | None]:
    """rule_only 臂：按人工映射判定覆盖（规则层不产出合同片段，无法按 span 对）。"""
    out: dict[str, int | None] = {gold.gold_id: None for gold in golds}
    for index, finding in enumerate(findings):
        for gold_id in finding.gold_ids:
            if out.get(gold_id) is None and gold_id in out:
                out[gold_id] = index
    return out


def allowed_statutes(covered: Sequence[GoldRow], matches: Mapping[str, int | None]) -> dict[int, set[str]]:
    """结论下标 → 它覆盖的 gold 的法条集（并集）；只含覆盖到 gold 的结论。"""
    out: dict[int, set[str]] = {}
    for gold in covered:
        index = matches.get(gold.gold_id)
        if index is None:
            continue
        out.setdefault(index, set()).update(gold.statute_ids)
    return out


def adjacent(grade_a: str, grade_b: str) -> bool:
    """相邻等级（A↔B、B↔C）算部分一致；未知等级一律不算。"""
    if grade_a not in GRADE_ORDER or grade_b not in GRADE_ORDER:
        return False
    return abs(GRADE_ORDER.index(grade_a) - GRADE_ORDER.index(grade_b)) == 1


def arm_metrics(
    golds: Sequence[GoldRow],
    findings: Sequence[ArmFinding],
    contract_text: str,
    library_ids: Container[str],
    *,
    use_mapping: bool = False,
) -> dict[str, Any]:
    """一条臂在**一份合同**上的五指标（纯函数：输入 gold + 结论，输出数字）。"""
    positives = [gold for gold in golds if gold.grade != NEGATIVE_GRADE]
    negatives = [gold for gold in golds if gold.grade == NEGATIVE_GRADE]
    matches = (
        match_by_mapping(golds, findings) if use_mapping else match_by_span(golds, findings, contract_text)
    )

    covered = [gold for gold in positives if matches.get(gold.gold_id) is not None]
    uncovered = [gold.gold_id for gold in positives if matches.get(gold.gold_id) is None]
    coverage = round(len(covered) / len(positives), 4) if positives else None

    # 2. A 级误报：负样本片段被某条 A 级结论覆盖 ≥50%，按负样本条计
    false_a_ids = [
        gold.gold_id
        for gold in negatives
        if any(
            finding.grade == "A" and span_coverage(gold.span, finding.span, contract_text) >= COVER_MIN
            for finding in findings
        )
    ]

    # 3. 引用准确率：只看"覆盖到 gold 的结论"的引用；库外条号单列，无 gold 对应的引用单列
    allowed = allowed_statutes(covered, matches)
    cited_total = cited_ok = cited_no_gold = out_of_library = 0
    for index, finding in enumerate(findings):
        gold_statutes = allowed.get(index, set())
        for statute_id in finding.statute_ids:
            if statute_id not in library_ids:
                out_of_library += 1
            if not gold_statutes:
                cited_no_gold += 1
                continue
            cited_total += 1
            if statute_id in gold_statutes:
                cited_ok += 1
    citation_accuracy = round(cited_ok / cited_total, 4) if cited_total else None

    # 4. span 定位率
    located = sum(1 for finding in findings if locate(finding.span, contract_text) is not None)
    span_rate = round(located / len(findings), 4) if findings else None

    # 5. 分级一致率：完全一致 / 相邻容忍（A↔B 记 0.5）
    exact = 0
    tolerant = 0.0
    grade_mismatch: list[str] = []
    for gold in covered:
        index = matches[gold.gold_id]
        assert index is not None  # covered 已保证
        predicted = findings[index].grade
        if predicted == gold.grade:
            exact += 1
            tolerant += 1.0
        elif adjacent(predicted, gold.grade):
            tolerant += 0.5
            grade_mismatch.append(f"{gold.gold_id}：gold={gold.grade} 实测={predicted}（相邻）")
        else:
            grade_mismatch.append(f"{gold.gold_id}：gold={gold.grade} 实测={predicted}")

    return {
        "coverage": coverage,
        "coverage_hits": len(covered),
        "coverage_total": len(positives),
        "false_a": len(false_a_ids),
        "false_a_total": len(negatives),
        "false_a_ids": false_a_ids,
        "citation_accuracy": citation_accuracy,
        "citation_ok": cited_ok,
        "citation_total": cited_total,
        "citation_no_gold": cited_no_gold,
        "out_of_library": out_of_library,
        "span_located": located,
        "span_total": len(findings),
        "span_rate": span_rate,
        "grade_exact": round(exact / len(covered), 4) if covered else None,
        "grade_tolerant": round(tolerant / len(covered), 4) if covered else None,
        # 原始计数（跨合同汇总时相加再重算比率，避免「比率的平均」）
        "grade_exact_hits": exact,
        "grade_tolerant_score": tolerant,
        "grade_covered": len(covered),
        "grade_mismatch": grade_mismatch,
        "covered": [gold.gold_id for gold in covered],
        "uncovered": uncovered,
        "n_findings": len(findings),
        "n_extra": sum(1 for finding in findings if finding.source == "model_extra"),
        "grades": _grade_counts(findings),
    }


def _grade_counts(findings: Sequence[ArmFinding]) -> dict[str, int]:
    """结论的等级分布；非法等级（关闭核验时可能出现）单独计入 `其他`。"""
    counts = {grade: sum(1 for finding in findings if finding.grade == grade) for grade in GRADE_ORDER}
    counts["其他"] = sum(1 for finding in findings if finding.grade not in GRADE_ORDER)
    return counts


def run_consistency(per_run_findings: Sequence[Sequence[ArmFinding]]) -> dict[str, Any]:
    """多次重跑的等级稳定率：取各轮都出现的（段落, 触发器）键，看等级是否完全相同。"""
    if len(per_run_findings) < 2:
        return {"runs": len(per_run_findings), "common": 0, "stable": 0, "rate": None}
    keyed = [
        {(finding.paragraph, finding.trigger_id): finding.grade for finding in run}
        for run in per_run_findings
    ]
    common = set(keyed[0])
    for mapping in keyed[1:]:
        common &= set(mapping)
    stable = sum(1 for key in common if len({mapping[key] for mapping in keyed}) == 1)
    return {
        "runs": len(per_run_findings),
        "common": len(common),
        "stable": stable,
        "rate": round(stable / len(common), 4) if common else None,
    }


# ---------- 三条臂 ----------


def rule_findings(contract_text: str, rules: Sequence[RuleEntry]) -> tuple[list[ArmFinding], dict[str, Any]]:
    """rule_only 臂：正则抽事实 → 数值规则判定；只保留越界命中。"""
    facts = RegexExtractor().extract(contract_text).facts
    hits = evaluate(tuple(rules), facts)
    violated = [hit for hit in hits if hit.violated]
    findings = [
        ArmFinding(
            span="",  # 规则层不产出合同片段：span 类指标天然为 0，这正是对照的一部分
            grade="A" if hit.rule.severity == Severity.HARD else "B",
            statute_ids=(hit.rule.statute_id,),
            gold_ids=RULE_TO_GOLD.get(hit.rule.rule_id, ()),
            source="rule",
        )
        for hit in violated
    ]
    diagnostics = {
        "facts": facts,
        "rules": len(rules),
        "premise_hits": len(hits),
        "violated": [hit.rule.rule_id for hit in violated],
        "mapped": {hit.rule.rule_id: list(RULE_TO_GOLD.get(hit.rule.rule_id, ())) for hit in violated},
    }
    return findings, diagnostics


def model_findings(
    llm: Generator | None,
    contract_text: str,
    triggers: Sequence[Trigger],
    *,
    batch_size: int,
    max_extra: int,
    verify: bool,
) -> tuple[list[ArmFinding], dict[str, Any]]:
    """触发器 + 模型（± 程序核验）的一次运行。"""
    candidates = detect_clauses(contract_text, tuple(triggers))
    errors: list[str] = []
    started = time.perf_counter()
    findings = review_clauses(
        llm,
        candidates,
        contract_text,
        batch_size=batch_size,
        max_extra=max_extra,
        errors=errors,
        verify=verify,
    )
    elapsed = time.perf_counter() - started
    arm = [
        ArmFinding(
            span=finding.span,
            grade=finding.grade,
            statute_ids=finding.statute_ids,
            source=finding.source,
            paragraph=finding.paragraph,
            trigger_id=finding.trigger_id,
        )
        for finding in findings
    ]
    diag = {
        "candidates": len(candidates),
        "errors": errors,
        # 程序核验丢弃条数：只看硬性校验丢的，不含"自提结论与已命中条款重叠"这类去重提示
        "dropped_by_verify": sum(1 for line in errors if "已丢弃" in line and "自提结论" not in line),
        "elapsed_s": round(elapsed, 3),
    }
    return arm, diag


# ---------- 落盘与打印 ----------


def _mean(values: Sequence[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return round(sum(nums) / len(nums), 4) if nums else None


MEAN_KEYS = (
    "coverage",
    "false_a",
    "citation_accuracy",
    "span_rate",
    "grade_exact",
    "grade_tolerant",
    "n_findings",
    "n_extra",
    "out_of_library",
    "citation_ok",
    "citation_total",
    "citation_no_gold",
    "coverage_hits",
    "span_located",
    "span_total",
)

# 汇总时可相加的原始计数（比率由这些计数在 `pooled_arms` 里重算）
SUM_KEYS = (
    "coverage_hits",
    "coverage_total",
    "false_a",
    "false_a_total",
    "citation_ok",
    "citation_total",
    "citation_no_gold",
    "out_of_library",
    "span_located",
    "span_total",
    "grade_exact_hits",
    "grade_tolerant_score",
    "grade_covered",
    "n_findings",
    "n_extra",
)
ARM_NAMES = ("rule_only", "model_raw", "full")


def _sums(metrics: Mapping[str, Any]) -> dict[str, int]:
    """从一轮指标里抽原始计数（缺项按 0）。"""
    return {key: int(metrics.get(key) or 0) for key in SUM_KEYS}


def pooled_arms(contracts: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """跨合同汇总：各合同**最后一轮**的原始计数相加后重算比率（不是比率的平均）。"""
    pooled: dict[str, dict[str, Any]] = {}
    for arm_name in ARM_NAMES:
        totals: dict[str, int] = {key: 0 for key in SUM_KEYS}
        elapsed = 0.0
        token_in = token_out = 0.0
        grades = {grade: 0 for grade in (*GRADE_ORDER, "其他")}
        for entry in contracts:
            arm = entry["arms"].get(arm_name)
            if not arm:
                continue
            for key, value in arm.get("sums", _sums(arm["per_run"][-1]["metrics"])).items():
                totals[key] += int(value)
            elapsed += float(arm.get("elapsed_s") or 0.0)
            token_in += float(arm.get("token_in") or 0.0)
            token_out += float(arm.get("token_out") or 0.0)
            for grade, count in (arm.get("grades") or {}).items():
                grades[grade] = grades.get(grade, 0) + int(count)

        def _ratio(num: str, den: str, counts: dict[str, int] = totals) -> float | None:
            base = counts[den]
            return round(counts[num] / base, 4) if base else None

        pooled[arm_name] = {
            "metrics": {
                "coverage": _ratio("coverage_hits", "coverage_total"),
                "citation_accuracy": _ratio("citation_ok", "citation_total"),
                "span_rate": _ratio("span_located", "span_total"),
                "grade_exact": _ratio("grade_exact_hits", "grade_covered"),
                "grade_tolerant": _ratio("grade_tolerant_score", "grade_covered"),
                "n_findings": totals["n_findings"],
                "n_extra": totals["n_extra"],
                "out_of_library": totals["out_of_library"],
                "citation_ok": totals["citation_ok"],
                "citation_total": totals["citation_total"],
            },
            "counts": {
                "coverage_hits": totals["coverage_hits"],
                "coverage_total": totals["coverage_total"],
                "false_a": totals["false_a"],
                "false_a_total": totals["false_a_total"],
                "citation_ok": totals["citation_ok"],
                "citation_total": totals["citation_total"],
                "out_of_library": totals["out_of_library"],
                "n_findings": totals["n_findings"],
                "n_extra": totals["n_extra"],
            },
            "grades": grades,
            "elapsed_s": round(elapsed, 3),
            "token_in": token_in,
            "token_out": token_out,
            "sums": totals,
        }
    return pooled


def summarize_arm(
    per_run: Sequence[Mapping[str, Any]],
    positives: int,
    negatives: int,
    consistency: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """多轮汇总：指标取均值，逐轮明细留在 `per_run`。"""
    metrics = {key: _mean([row["metrics"].get(key) for row in per_run]) for key in MEAN_KEYS}
    last = per_run[-1]["metrics"]
    return {
        "metrics": metrics,
        # 最后一轮的原始计数：跨合同汇总时相加用（见 `pooled_arms`）
        "sums": _sums(last),
        "counts": {
            "coverage_hits": metrics["coverage_hits"],
            "coverage_total": positives,
            "false_a": metrics["false_a"],
            "false_a_total": negatives,
            "citation_ok": metrics["citation_ok"],
            "citation_total": metrics["citation_total"],
            "out_of_library": metrics["out_of_library"],
            "n_findings": metrics["n_findings"],
            "n_extra": metrics["n_extra"],
        },
        "grades": last["grades"],
        "covered": last["covered"],
        "uncovered": last["uncovered"],
        "grade_mismatch": last["grade_mismatch"],
        "elapsed_s": _mean([row["elapsed_s"] for row in per_run]),
        "token_in": _mean([row["token_in"] for row in per_run]),
        "token_out": _mean([row["token_out"] for row in per_run]),
        "run_consistency": dict(consistency) if consistency is not None else None,
        "diagnostics": per_run[-1]["diagnostics"],
        "per_run": list(per_run),
    }


def eval_contract(
    contract_path: Path,
    golds: Sequence[GoldRow],
    *,
    runs: int,
    llm: Generator | None,
    triggers: Sequence[Trigger],
    rules: Sequence[RuleEntry],
    library_ids: Container[str],
    batch_size: int,
    max_extra: int,
) -> dict[str, Any]:
    """一份合同上跑三条臂：rule_only 跑 1 次，两条模型臂各跑 `runs` 次。"""
    contract_text = contract_path.read_text(encoding="utf-8")
    positives = sum(1 for gold in golds if gold.grade != NEGATIVE_GRADE)
    negatives = len(golds) - positives

    rule_arm, rule_diag = rule_findings(contract_text, rules)
    arms: dict[str, Any] = {
        "rule_only": summarize_arm(
            [
                {
                    "metrics": arm_metrics(golds, rule_arm, contract_text, library_ids, use_mapping=True),
                    "elapsed_s": 0.0,
                    "token_in": 0,
                    "token_out": 0,
                    "diagnostics": rule_diag,
                }
            ],
            positives,
            negatives,
        )
    }

    for arm_name, verify in (("model_raw", False), ("full", True)):
        per_run: list[dict[str, Any]] = []
        arm_findings_all: list[list[ArmFinding]] = []
        for _ in range(max(1, runs)):
            before_in = int(getattr(llm, "usage_in", 0) or 0)
            before_out = int(getattr(llm, "usage_out", 0) or 0)
            findings, diag = model_findings(
                llm,
                contract_text,
                triggers,
                batch_size=batch_size,
                max_extra=max_extra,
                verify=verify,
            )
            per_run.append(
                {
                    "metrics": arm_metrics(golds, findings, contract_text, library_ids),
                    "elapsed_s": diag.pop("elapsed_s"),
                    "token_in": int(getattr(llm, "usage_in", 0) or 0) - before_in,
                    "token_out": int(getattr(llm, "usage_out", 0) or 0) - before_out,
                    "diagnostics": diag,
                }
            )
            arm_findings_all.append(findings)
        arms[arm_name] = summarize_arm(per_run, positives, negatives, run_consistency(arm_findings_all))

    return {
        "contract": str(contract_path),
        "contract_chars": len(contract_text),
        "gold": {"positives": positives, "negatives": negatives},
        "arms": arms,
    }


def generalization_noise(
    llm: Generator | None,
    triggers: Sequence[Trigger],
    *,
    path: str | Path = GENERALIZATION,
    batch_size: int,
    max_extra: int,
) -> dict[str, Any]:
    """跨合同噪音检查：只报数量与片段预览，**不编 gold、不算准确率**。"""
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    cases: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("contract") or "")
        before_in = int(getattr(llm, "usage_in", 0) or 0)
        before_out = int(getattr(llm, "usage_out", 0) or 0)
        findings, diag = model_findings(
            llm, text, triggers, batch_size=batch_size, max_extra=max_extra, verify=True
        )
        cases.append(
            {
                "case_id": str(row.get("case_id", "")),
                "kind": str(row.get("kind", "")),
                "chars": len(text),
                "candidates": diag["candidates"],
                "findings": len(findings),
                "grades": _grade_counts(findings),
                "elapsed_s": diag["elapsed_s"],
                "token_in": int(getattr(llm, "usage_in", 0) or 0) - before_in,
                "token_out": int(getattr(llm, "usage_out", 0) or 0) - before_out,
                "preview": [
                    f"{finding.grade} | {finding.span[:PREVIEW_CHARS]}" for finding in findings[:PREVIEW_ROWS]
                ],
                "errors": diag["errors"][:5],
            }
        )

    def _span(key: str) -> dict[str, Any]:
        values = [case[key] for case in cases]
        return {"min": min(values), "max": max(values), "sum": sum(values)} if values else {}

    return {
        "source": str(path),
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "candidates": _span("candidates"),
            "findings": _span("findings"),
            "elapsed_s": _span("elapsed_s"),
            "token_in": _span("token_in"),
            "token_out": _span("token_out"),
            "cases_with_findings": sum(1 for case in cases if case["findings"]),
        },
    }


def _fmt(value: float | None, pattern: str = "{:.4f}") -> str:
    return "-" if value is None else pattern.format(value)


def _width(text: str) -> int:
    """控制台显示宽度（中文占两列），用于对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _cell(text: str, width: int, align: str = ">") -> str:
    padding = max(0, width - _width(text))
    return (" " * padding + text) if align == ">" else (text + " " * padding)


WIDTHS = (10, 8, 8, 10, 10, 10, 10, 6, 9, 14)
LABELS = {"rule_only": "规则-only", "model_raw": "触发+模型", "full": "完整方案"}


def _print_table_header() -> None:
    header = (
        "臂",
        "覆盖",
        "A误报",
        "引用准确",
        "span定位",
        "分级一致",
        "容忍一致",
        "额外",
        "耗时(s)",
        "token入/出",
    )
    print(
        "".join(
            _cell(text, width, "<" if index == 0 else ">")
            for index, (text, width) in enumerate(zip(header, WIDTHS, strict=True))
        )
    )
    print("-" * sum(WIDTHS))


def _print_arm_rows(arms: Mapping[str, Any]) -> None:
    """行 = 三条臂，列 = 五指标 + 额外/耗时/token。"""
    for arm_name in ARM_NAMES:
        if arm_name not in arms:
            continue
        arm = arms[arm_name]
        counts = arm["counts"]
        metrics = arm["metrics"]
        coverage = "-"
        if counts["coverage_total"]:
            coverage = f"{counts['coverage_hits']:g}/{counts['coverage_total']}"
        cells = (
            LABELS[arm_name],
            coverage,
            f"{counts['false_a']:g}/{counts['false_a_total']:g}",
            _fmt(metrics["citation_accuracy"]),
            _fmt(metrics["span_rate"]),
            _fmt(metrics["grade_exact"]),
            _fmt(metrics["grade_tolerant"]),
            f"{counts['n_extra']:g}",
            _fmt(arm["elapsed_s"], "{:.1f}"),
            f"{arm['token_in']:g}/{arm['token_out']:g}",
        )
        print(
            "".join(
                _cell(text, width, "<" if index == 0 else ">")
                for index, (text, width) in enumerate(zip(cells, WIDTHS, strict=True))
            )
        )


def print_table(payload: Mapping[str, Any]) -> None:
    """控制台表格：**逐合同**各一张（行 = 三条臂），末尾一张跨合同汇总表。"""
    print(f"\n条款级评测（合同 {payload['contract_count']} 份，每个模型臂跑 {payload['runs']} 次）")
    contracts = payload.get("contracts") or []
    for entry in contracts:
        name = Path(str(entry["contract"])).name
        gold = entry.get("gold") or {}
        positives = int(gold.get("positives") or 0)
        negatives = int(gold.get("negatives") or 0)
        note = "" if positives or negatives else "（gold 缺失：准确率跳过，只报数量与等级分布）"
        print(f"\n── {name}：gold 正 {positives} / 负 {negatives}{note}")
        _print_table_header()
        _print_arm_rows(entry["arms"])

        for arm_name in ARM_NAMES:
            arm = entry["arms"].get(arm_name)
            if not arm:
                continue
            uncovered = arm["uncovered"]
            detail = "（" + " ".join(uncovered) + "）" if uncovered else ""
            print(
                f"  {LABELS[arm_name]}：未覆盖 {len(uncovered)} 条{detail}；"
                f"库外条号 {arm['counts']['out_of_library']:g} 条"
            )
            grades = arm.get("grades") or {}
            print(
                f"  {LABELS[arm_name]}：结论 {arm['counts']['n_findings']:g} 条"
                f"（A {grades.get('A', 0)} / B {grades.get('B', 0)} / C {grades.get('C', 0)}"
                f" / 其他 {grades.get('其他', 0)}），其中自提 {arm['counts']['n_extra']:g} 条"
            )
        for arm_name in ("model_raw", "full"):
            arm = entry["arms"].get(arm_name)
            if not arm:
                continue
            consistency = arm.get("run_consistency")
            if consistency and consistency.get("rate") is not None:
                print(
                    f"  {LABELS[arm_name]}：{consistency['runs']} 次重跑等级稳定率 {consistency['rate']:.4f}"
                    f"（{consistency['stable']}/{consistency['common']} 条共同结论）"
                )

    if len(contracts) > 1:
        print("\n── 汇总（各合同最后一轮的原始计数相加后重算比率）")
        _print_table_header()
        _print_arm_rows(payload.get("pooled") or {})
    print("说明：覆盖/分级按 gold 条计，引用准确按引用条计；规则-only 不产出合同片段，span 类指标天然为 0。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="条款级评测（三臂消融）")
    parser.add_argument(
        "--contracts", nargs="*", default=None, help="合同路径列表，默认 contract_traps_01.txt"
    )
    parser.add_argument("--gold", default=DEFAULT_GOLD, help="条款级 gold JSONL")
    parser.add_argument("--runs", type=int, default=3, help="每个模型臂重跑次数（默认 3）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="覆盖写的 JSON")
    parser.add_argument("--history", default=DEFAULT_HISTORY, help="追加的历史 JSONL")
    parser.add_argument("--no-generalization", action="store_true", help="跳过 12 条泛化合同的噪音检查")
    args = parser.parse_args(argv)

    cfg = load_config()
    runs = max(1, int(args.runs))
    contract_paths = [Path(p) for p in (args.contracts or [DEFAULT_CONTRACT])]
    for path in contract_paths:
        if not path.is_file():
            print(f"[FAIL] 合同文件不存在：{path}")
            return 1

    golds = load_gold(args.gold)
    triggers = load_triggers(cfg.paths.clauses)
    rules = load_rules(cfg.paths.rules)
    library_ids = {row.statute_id for row in load_statutes(cfg.paths.statutes)}
    llm: Generator | None = DeepSeekLLM(cfg.llm) if cfg.llm.api_key else None
    model_note = f"已启用 {cfg.llm.model}" if llm else "未配置（两条模型臂将为空，只有规则臂有数）"
    print(f"大模型：{model_note}")
    print(
        f"触发器 {len(triggers)} 条；规则 {len(rules)} 条；法源库 {len(library_ids)} 条；gold {len(golds)} 条"
    )

    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contract_count": len(contract_paths),
        "runs": runs,
        "model": cfg.llm.model if llm else "",
        "gold_file": args.gold,
    }

    per_contract: list[dict[str, Any]] = []
    for path in contract_paths:
        scoped = tuple(gold for gold in golds if gold.contract == path.name)
        if not scoped:
            print(f"[WARN] {path.name} 在 gold 里没有标注：跳过准确率，只报候选数与等级分布")
        per_contract.append(
            eval_contract(
                path,
                scoped,
                runs=runs,
                llm=llm,
                triggers=triggers,
                rules=rules,
                library_ids=library_ids,
                batch_size=cfg.clause_review.batch_size,
                max_extra=cfg.clause_review.max_extra,
            )
        )

    payload["contracts"] = per_contract
    payload["gold_summary"] = {
        "positives": sum(1 for gold in golds if gold.grade != NEGATIVE_GRADE),
        "negatives": sum(1 for gold in golds if gold.grade == NEGATIVE_GRADE),
        "grades": {grade: sum(1 for gold in golds if gold.grade == grade) for grade in GRADE_ORDER},
        "by_contract": {
            name: sum(1 for gold in golds if gold.contract == name)
            for name in sorted({gold.contract for gold in golds})
        },
        "pos_neg_by_contract": {
            name: {
                "positives": sum(1 for g in golds if g.contract == name and g.grade != NEGATIVE_GRADE),
                "negatives": sum(1 for g in golds if g.contract == name and g.grade == NEGATIVE_GRADE),
            }
            for name in sorted({gold.contract for gold in golds})
        },
    }
    # 向后兼容：`arms` 仍指第一份合同；逐合同明细在 `contracts`，跨合同汇总在 `pooled`
    payload["arms"] = per_contract[0]["arms"] if per_contract else {}
    payload["pooled"] = pooled_arms(per_contract)

    print_table(payload)
    summary = payload["gold_summary"]
    print(
        f"\ngold 分布：正样本 {summary['positives']}"
        f"（A {summary['grades']['A']} / B {summary['grades']['B']} / C {summary['grades']['C']}），"
        f"负样本 {summary['negatives']}"
    )
    for name, counts in summary["pos_neg_by_contract"].items():
        print(f"  · {name}：正 {counts['positives']} / 负 {counts['negatives']}")

    if not args.no_generalization:
        print(f"\n跨合同噪音检查：{GENERALIZATION}（只报数量，不算准确率）")
        noise = generalization_noise(
            llm,
            triggers,
            batch_size=cfg.clause_review.batch_size,
            max_extra=cfg.clause_review.max_extra,
        )
        payload["generalization_noise"] = noise
        for case in noise["cases"]:
            grades = case["grades"]
            print(
                f"  {case['case_id']:<4} 候选 {case['candidates']:>2}｜结论 {case['findings']:>2}"
                f"（A {grades['A']} B {grades['B']} C {grades['C']}）"
                f"｜{case['elapsed_s']:>5.1f}s｜token {case['token_in']}/{case['token_out']}"
            )
        noise_summary = noise["summary"]
        print(
            f"  汇总：{noise_summary['cases']} 条合同，"
            f"候选 {noise_summary['candidates'].get('min')}–{noise_summary['candidates'].get('max')}，"
            f"结论 {noise_summary['findings'].get('min')}–{noise_summary['findings'].get('max')}，"
            f"耗时 {noise_summary['elapsed_s'].get('min')}–{noise_summary['elapsed_s'].get('max')}s"
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_row = {
        "generated_at": payload["generated_at"],
        "contracts": [str(path) for path in contract_paths],
        "runs": runs,
        "gold_summary": payload["gold_summary"],
        "arms": {
            name: {
                "metrics": arm["metrics"],
                "counts": arm["counts"],
                "elapsed_s": arm["elapsed_s"],
                "token_in": arm["token_in"],
                "token_out": arm["token_out"],
            }
            for name, arm in payload["arms"].items()
        },
        "generalization_noise": payload.get("generalization_noise", {}).get("summary"),
        "by_contract": {
            Path(str(entry["contract"])).name: {
                arm_name: {
                    "metrics": arm["metrics"],
                    "counts": arm["counts"],
                    "elapsed_s": arm["elapsed_s"],
                    "token_in": arm["token_in"],
                    "token_out": arm["token_out"],
                }
                for arm_name, arm in entry["arms"].items()
            }
            for entry in per_contract
        },
        "pooled": payload["pooled"],
    }
    history_path = Path(args.history)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, ensure_ascii=False) + "\n")

    print(f"\n已写入 {out_path}（历史追加 {history_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
