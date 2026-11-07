"""条款级评测指标单测：纯函数、stub 数据、不联网。

被测对象是 `scripts/eval_clauses.py` 里的指标函数（覆盖率 / A 级误报 / 引用准确率 /
span 定位率 / 分级一致率），不触发任何模型调用。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_clauses.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_clauses", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要能在 sys.modules 里找到本模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_module()

CONTRACT = (
    "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n"
    "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴。\n"
    "本合同一式两份，甲乙双方各执一份。\n"
)
TRANSFER = "甲方有权单方调整乙方工作岗位，乙方不得拒绝"
TRANSFER_HEAD = "甲方有权单方调整乙方工作岗位"
TRANSFER_TAIL = "乙方不得拒绝"
SI = "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴"
OK_CLAUSE = "本合同一式两份，甲乙双方各执一份"
LIBRARY = {"LCL-35", "LCL-39", "LCL-26", "J2-19"}


def _gold(gold_id: str, span: str, grade: str, statutes: tuple[str, ...] = ()) -> object:
    return ev.GoldRow(gold_id=gold_id, contract="stub.txt", span=span, grade=grade, statute_ids=statutes)


def _finding(
    span: str,
    grade: str = "A",
    statutes: tuple[str, ...] = (),
    gold_ids: tuple[str, ...] = (),
    source: str = "trigger",
) -> object:
    return ev.ArmFinding(span=span, grade=grade, statute_ids=statutes, gold_ids=gold_ids, source=source)


# ---------- 定位与片段覆盖 ----------


def test_locate_returns_normalized_positions() -> None:
    located = ev.locate(TRANSFER, CONTRACT)
    assert located is not None
    start, end = located
    assert end - start == len(TRANSFER)
    assert ev.locate("合同里没有这句话", CONTRACT) is None
    assert ev.locate("", CONTRACT) is None


def test_span_coverage_full_partial_and_unlocatable() -> None:
    assert ev.span_coverage(TRANSFER, TRANSFER, CONTRACT) == 1.0
    # 头半句覆盖整条 gold 的一段：分母取 gold 长度，约 0.5 上下
    head_ratio = ev.span_coverage(TRANSFER, TRANSFER_HEAD, CONTRACT)
    assert 0.4 <= head_ratio < 1.0
    # 模型改写的片段在原文定位不到 → 0.0
    assert ev.span_coverage(TRANSFER, "甲方可以随便调岗", CONTRACT) == 0.0


def test_match_by_span_one_finding_covers_two_golds() -> None:
    golds = [_gold("G1", TRANSFER_HEAD, "A"), _gold("G2", TRANSFER_TAIL, "A")]
    findings = [_finding(TRANSFER)]
    matches = ev.match_by_span(golds, findings, CONTRACT)
    assert matches == {"G1": 0, "G2": 0}


def test_match_by_span_below_threshold_uncovered() -> None:
    golds = [_gold("G1", TRANSFER_TAIL, "A")]
    findings = [_finding(TRANSFER_HEAD)]
    assert ev.match_by_span(golds, findings, CONTRACT) == {"G1": None}


# ---------- A 级误报 ----------


def test_a_false_positive_counts_negative_rows() -> None:
    golds = [_gold("N1", OK_CLAUSE, "合规")]
    assert ev.arm_metrics(golds, [_finding(OK_CLAUSE, grade="A")], CONTRACT, LIBRARY)["false_a"] == 1
    # B/C 级不算 A 级误报
    assert ev.arm_metrics(golds, [_finding(OK_CLAUSE, grade="B")], CONTRACT, LIBRARY)["false_a"] == 0
    # 片段对不上也不算
    assert ev.arm_metrics(golds, [_finding(TRANSFER, grade="A")], CONTRACT, LIBRARY)["false_a"] == 0


# ---------- 覆盖率与未覆盖清单 ----------


def test_coverage_and_uncovered_list() -> None:
    golds = [_gold("T1", TRANSFER, "A"), _gold("T2", SI, "A"), _gold("T3", OK_CLAUSE, "合规")]
    findings = [_finding(TRANSFER, grade="A", statutes=("LCL-35",))]
    metrics = ev.arm_metrics(golds, findings, CONTRACT, LIBRARY)
    assert metrics["coverage"] == 0.5
    assert metrics["coverage_hits"] == 1
    assert metrics["coverage_total"] == 2
    assert metrics["uncovered"] == ["T2"]
    assert metrics["covered"] == ["T1"]


# ---------- 引用准确率 ----------


def test_citation_accuracy_counts_library_outside_and_no_gold() -> None:
    golds = [_gold("T1", TRANSFER, "A", ("LCL-35", "LCL-39"))]
    findings = [_finding(TRANSFER, grade="A", statutes=("LCL-35", "LCL-999"))]
    metrics = ev.arm_metrics(golds, findings, CONTRACT, LIBRARY)
    assert metrics["citation_ok"] == 1
    assert metrics["citation_total"] == 2
    assert metrics["citation_accuracy"] == 0.5
    assert metrics["out_of_library"] == 1


def test_citation_without_gold_mapping_counted_separately() -> None:
    golds = [_gold("T1", SI, "A", ("J2-19",))]
    findings = [_finding(TRANSFER, grade="A", statutes=("LCL-26",))]
    metrics = ev.arm_metrics(golds, findings, CONTRACT, LIBRARY)
    assert metrics["citation_total"] == 0
    assert metrics["citation_accuracy"] is None
    assert metrics["citation_no_gold"] == 1
    assert metrics["out_of_library"] == 0


# ---------- span 定位率 ----------


def test_span_rate_counts_unlocatable_findings() -> None:
    findings = [_finding(TRANSFER), _finding("模型自己改写的一句话")]
    metrics = ev.arm_metrics([], findings, CONTRACT, LIBRARY)
    assert metrics["span_located"] == 1
    assert metrics["span_total"] == 2
    assert metrics["span_rate"] == 0.5
    # 没有结论时定位率无定义，不能报 0
    assert ev.arm_metrics([], [], CONTRACT, LIBRARY)["span_rate"] is None


# ---------- 分级一致率 ----------


def test_grade_exact_and_tolerant() -> None:
    contract = (
        "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n"
        "乙方自愿放弃缴纳社会保险。\n"
        "本合同一式两份，甲乙双方各执一份。\n"
    )
    golds = [
        _gold("T1", "甲方有权单方调整乙方工作岗位", "A"),
        _gold("T2", "乙方自愿放弃缴纳社会保险", "A"),
        _gold("T3", "本合同一式两份", "A"),
    ]
    findings = [
        _finding("甲方有权单方调整乙方工作岗位", grade="A"),  # 完全一致
        _finding("乙方自愿放弃缴纳社会保险", grade="B"),  # 相邻（A↔B），记 0.5
        _finding("本合同一式两份", grade="C"),  # 不一致
    ]
    metrics = ev.arm_metrics(golds, findings, contract, LIBRARY)
    assert metrics["grade_exact"] == round(1 / 3, 4)
    assert metrics["grade_tolerant"] == round(1.5 / 3, 4)
    assert len(metrics["grade_mismatch"]) == 2


def test_adjacent_only_between_neighbour_grades() -> None:
    assert ev.adjacent("A", "B") is True
    assert ev.adjacent("B", "C") is True
    assert ev.adjacent("A", "C") is False
    assert ev.adjacent("S", "A") is False


# ---------- rule_only 臂：按映射而非 span ----------


def test_rule_arm_uses_mapping_not_span() -> None:
    golds = [_gold("T05", TRANSFER, "A", ("LCL-20",)), _gold("T12", SI, "A", ("LCL-24",))]
    findings = [
        _finding("", grade="A", statutes=("LCR-15",), gold_ids=("T05",), source="rule"),
    ]
    metrics = ev.arm_metrics(golds, findings, CONTRACT, LIBRARY, use_mapping=True)
    assert metrics["coverage_hits"] == 1
    assert metrics["uncovered"] == ["T12"]
    # 规则层不产出合同片段：定位率 0，且不因映射而虚高
    assert metrics["span_rate"] == 0.0
    # 引用的法条不在对应 gold 的 statute_ids 内（LCR-15 vs LCL-20）→ 准确率 0
    assert metrics["citation_accuracy"] == 0.0


def test_rule_mapping_points_at_existing_gold_shape() -> None:
    """映射表只指向 T 开头的正样本 ID，且规则 ID 走 R- 前缀（防止手误写成负样本）。"""
    assert ev.RULE_TO_GOLD  # 非空
    for rule_id, gold_ids in ev.RULE_TO_GOLD.items():
        assert rule_id.startswith("R-")
        assert all(gold_id.startswith("T") for gold_id in gold_ids)


# ---------- 重跑等级稳定率 ----------


def test_run_consistency_counts_common_keys() -> None:
    first = [
        ev.ArmFinding(span=TRANSFER, grade="A", paragraph=1, trigger_id="TR-A"),
        ev.ArmFinding(span=SI, grade="B", paragraph=2, trigger_id="TR-B"),
    ]
    second = [
        ev.ArmFinding(span=TRANSFER, grade="A", paragraph=1, trigger_id="TR-A"),
        ev.ArmFinding(span=SI, grade="C", paragraph=2, trigger_id="TR-B"),
    ]
    consistency = ev.run_consistency([first, second])
    assert consistency["common"] == 2
    assert consistency["stable"] == 1
    assert consistency["rate"] == 0.5
    # 只跑 1 次时不给率（避免把"没重跑"说成"完全一致"）
    assert ev.run_consistency([first])["rate"] is None


# ---------- 消融开关：review_clauses(verify=False) ----------


class _StubLLM:
    """固定返回一条越界结论的假模型：等级非法、引库外条号、证据不在原文。"""

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict | None = None) -> dict:
        return {
            "findings": [
                {
                    "idx": 0,
                    "grade": "S",
                    "reason": "该约定与某条不存在的规定存在冲突",
                    "statute_ids": ["LCL-999"],
                    "evidence": "合同里根本没有的一句话",
                    "need_facts": [],
                }
            ],
            "extra": [],
        }


def _candidate() -> object:
    from mla.domain.clauses import ClauseCandidate

    return ClauseCandidate(
        paragraph=1,
        span=TRANSFER,
        trigger_id="TR-UNILATERAL-TRANSFER",
        name="单方强制调岗",
        candidate_statute_ids=("LCL-35",),
        severity_hint="A",
    )


def test_verify_false_keeps_raw_output() -> None:
    findings = ev.review_clauses(_StubLLM(), [_candidate()], CONTRACT, statutes={}, verify=False)
    assert len(findings) == 1
    # 等级与法条原样保留（不校验、不降级），span 是模型原文（定位不到）
    assert findings[0].grade == "S"
    assert findings[0].statute_ids == ("LCL-999",)
    assert findings[0].span == "合同里根本没有的一句话"


def test_verify_true_drops_the_same_output() -> None:
    errors: list[str] = []
    findings = ev.review_clauses(
        _StubLLM(), [_candidate()], CONTRACT, statutes={}, errors=errors, verify=True
    )
    assert findings == ()
    assert any("等级不合法" in line for line in errors)
