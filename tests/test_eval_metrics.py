"""评测重建（规格 003 第 11 章）的单测：Wilson 区间、带分母的比率、下线规则 gold。

全部是纯函数 / stub 数据，**不调模型、不读网络**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_clauses.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_clauses_p3", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要能在 sys.modules 里找到本模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_module()

CONTRACT = "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n本合同一式两份，甲乙双方各执一份。\n"
POSITIVE = "甲方有权单方调整乙方工作岗位，乙方不得拒绝"
NEGATIVE = "本合同一式两份，甲乙双方各执一份"
LIBRARY = {"LCL-35"}


def _gold(gold_id: str, span: str, grade: str, statutes: tuple[str, ...] = ()) -> object:
    return ev.GoldRow(gold_id=gold_id, contract="stub.txt", span=span, grade=grade, statute_ids=statutes)


def _finding(span: str, grade: str = "A", statutes: tuple[str, ...] = ()) -> object:
    return ev.ArmFinding(span=span, grade=grade, statute_ids=statutes)


# ---------- Wilson 区间 ----------


def test_wilson_interval_known_samples() -> None:
    """三组已知值（规格 11.2 给的公式手算结果）。"""
    low, high = ev.wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=5e-4)
    assert high == pytest.approx(0.7634, abs=5e-4)

    low, high = ev.wilson_interval(0, 10)
    assert low == 0.0  # 下界截到 0，不出现负数
    assert high == pytest.approx(0.2775, abs=5e-4)

    low, high = ev.wilson_interval(10, 10)
    assert low == pytest.approx(0.7225, abs=5e-4)
    assert high == pytest.approx(1.0, abs=5e-4)  # 上界截到 1


def test_wilson_interval_zero_denominator_returns_none() -> None:
    """n=0 时没有区间可言，返回 None 而不是 (0, 0)。"""
    assert ev.wilson_interval(0, 0) is None


def test_wilson_interval_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        ev.wilson_interval(3, 2)


# ---------- arm_metrics：每个比率都带分母与区间 ----------


def test_arm_metrics_carries_ci_and_denominator() -> None:
    golds = [_gold("T1", POSITIVE, "A", ("LCL-35",)), _gold("N1", NEGATIVE, "合规")]
    findings = [_finding(POSITIVE, grade="A", statutes=("LCL-35",))]
    metrics = ev.arm_metrics(golds, findings, CONTRACT, LIBRARY)

    # 覆盖率（召回）：1/1，区间下界即 Wilson 的 1/1 下界（不是 0.72——那是 10/10）
    assert metrics["coverage"] == 1.0
    assert metrics["coverage_total"] == 1
    assert metrics["coverage_ci"] == pytest.approx((0.2065, 1.0), abs=5e-4)
    # A 级误报率：0/1
    assert metrics["false_a_rate"] == 0.0
    assert metrics["false_a_total"] == 1
    assert metrics["false_a_ci"] == pytest.approx((0.0, 0.7935), abs=5e-4)
    # 结论精确率：1/1
    assert metrics["precision"] == 1.0
    assert metrics["precision_total"] == 1
    assert metrics["precision_ci"] == pytest.approx((0.2065, 1.0), abs=5e-4)
    # 引用准确率 / span 定位率 / 分级一致率同样带区间
    for key in ("citation_accuracy_ci", "span_rate_ci", "grade_exact_ci"):
        assert key in metrics
    assert metrics["citation_accuracy_ci"] == pytest.approx((0.2065, 1.0), abs=5e-4)


def test_arm_metrics_zero_denominator_gives_none_ci() -> None:
    """空 gold + 空结论：比率与区间都为 None，不把「没算」报成 0。"""
    metrics = ev.arm_metrics([], [], CONTRACT, LIBRARY)
    assert metrics["coverage"] is None
    assert metrics["coverage_ci"] is None
    assert metrics["precision"] is None
    assert metrics["precision_ci"] is None


def test_intervals_from_counts_matches_wilson() -> None:
    counts = {
        "coverage_hits": 5,
        "coverage_total": 10,
        "false_a": 0,
        "false_a_total": 10,
        "precision_hits": 10,
        "precision_total": 10,
        "citation_ok": 0,
        "citation_total": 0,
        "span_located": 5,
        "span_total": 10,
        "grade_exact_hits": 5,
        "grade_covered": 10,
    }
    intervals = ev.intervals_from_counts(counts)
    assert intervals["coverage"] == ev.wilson_interval(5, 10)
    assert intervals["false_a_rate"] == ev.wilson_interval(0, 10)
    assert intervals["precision"] == ev.wilson_interval(10, 10)
    assert intervals["citation_accuracy"] is None  # 分母 0
    assert set(intervals) == set(ev.INTERVAL_PAIRS)


# ---------- 已下线规则的 gold：运行时剔除、计数可见 ----------


CASES = [
    {"case_id": "v01", "gold_items": ["R-GONE"]},  # gold 全指向已删规则 → retired
    {"case_id": "v02", "gold_items": ["R-OK"]},  # 规则还在 → 照常进分母
    {"case_id": "v03", "gold_items": ["R-OK", "R-GONE"]},  # 部分失效 → 保留并单列
    {"case_id": "c09", "gold_items": []},  # 本来无 gold → 不影响
]


def test_retired_gold_cases_dropped_and_counted() -> None:
    audit = ev.audit_gold_cases(CASES, {"R-OK"}, source="stub.jsonl")
    assert audit["total"] == 4
    assert [row["case_id"] for row in audit["retired"]] == ["v01"]
    assert audit["retired"][0]["missing_rules"] == ["R-GONE"]
    assert audit["denominator"] == 3  # 4 − 1 条 retired
    assert audit["usable"] == 1  # 只有 v02 的 gold 全部有效；v03 属 partial
    assert [row["case_id"] for row in audit["partial"]] == ["v03"]
    assert audit["no_gold"] == ["c09"]
    assert audit["retired_rules"] == ["R-GONE"]


def test_active_rule_cases_untouched() -> None:
    """规则还在的用例条数与 gold 都不动（只识别下线，不误伤）。"""
    rows = [{"case_id": "v02", "gold_items": ["R-OK", "R-OK2"]}]
    audit = ev.audit_gold_cases(rows, {"R-OK", "R-OK2"})
    assert audit["retired"] == []
    assert audit["partial"] == []
    assert audit["usable"] == 1
    assert audit["denominator"] == 1


def test_retired_audit_aggregates_files(tmp_path: Path) -> None:
    """多文件汇总：剔除总数、规则名、进分母总数都可直接落盘。"""
    first = tmp_path / "eval_cases.jsonl"
    second = tmp_path / "eval_generalization.jsonl"
    first.write_text(
        "\n".join(
            [
                '{"case_id": "v01", "gold_items": ["R-GONE"]}',
                '{"case_id": "v02", "gold_items": ["R-OK"]}',
            ]
        ),
        encoding="utf-8",
    )
    second.write_text('{"case_id": "g01", "gold_items": ["R-GONE"]}\n', encoding="utf-8")

    audit = ev.retired_gold_audit({"R-OK"}, paths=(first, second))
    assert audit["retired_total"] == 2
    assert audit["retired_rules"] == ["R-GONE"]
    assert audit["denominator_total"] == 1  # 2 − 1 ＋ 1 − 1
    assert [item["source"] for item in audit["files"]] == ["eval_cases.jsonl", "eval_generalization.jsonl"]
