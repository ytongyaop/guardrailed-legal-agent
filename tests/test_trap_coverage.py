"""用户实测陷阱合同的召回回归（不属于评测集 gold，不进任何评测指标）。

来源：用户实测的一份含 10 个人工预埋陷阱的劳动合同。当时 28 条触发器只召回 2 条，
漏掉无偿加班、社保迟缴与转嫁、竞业无补偿、放弃仲裁、自动续期等写法。
这份合同**不进** clause_gold.jsonl，也不改四份基线合同：它只用于回归召回能力，
因此这里断言的是召回条数下限与关键触发器必中，不是准确率，也不能当泛化结论。
"""

from __future__ import annotations

from pathlib import Path

from mla.config import data_path
from mla.domain.clauses import ClauseCandidate, detect_clauses, load_triggers

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trap10_contract.txt"
COMPLIANT = Path(data_path("evalsets/contract_compliant_01.txt"))
TRIGGERS = load_triggers(data_path("clauses/triggers.yaml"))

# 召回下限：改之前是 2 条，10 类陷阱全部覆盖后实际 13 条，下限取 8 留出调整空间，
# 但任何一次改动只要跌回 8 以下，说明有新回归 (把下限写死在 13 会让正常收敛变得不可做)。
MIN_CANDIDATES = 8
MIN_TRIGGERS = 8

# 10 类陷阱对应的触发器：缺任何一条都说明该写法族漏召回。
EXPECTED_TRIGGER_IDS = {
    "TR-OT-UNPAID-VOLUNTARY": "无偿加班 / 不申请加班费与调休",
    "TR-UNILATERAL-PAY-CUT": "单方任意下调工资",
    "TR-WAGE-WITHHOLD-ON-EXIT": "离职当月不结算 / 未结薪资自动作废",
    "TR-SI-DELAYED": "入职满半年后才缴社保",
    "TR-SI-COST-SHIFT": "社保与公积金全部由劳动者承担",
    "TR-NONCOMPETE-ALL-STAFF": "竞业无补偿（原文不写竞业限制字样）",
    "TR-CONFIDENTIAL-UNLIMITED": "终身承担保密义务",
    "TR-ARBITRATION-WAIVER": "放弃劳动仲裁权利（劳动仲裁只出现一次）",
    "TR-CONTRACT-COPY-INVALID": "员工所持合同无法律效力",
    "TR-CONTRACT-AUTO-RENEW": "合同到期无书面解除即自动续期",
}


def _candidates() -> tuple[ClauseCandidate, ...]:
    return detect_clauses(FIXTURE.read_text(encoding="utf-8"), TRIGGERS)


def test_fixture_exists_and_keeps_original_text() -> None:
    """夹具是用户实测原文的副本；改动它等于改测试输入，必须单独确认。"""
    text = FIXTURE.read_text(encoding="utf-8")
    assert "员工所持合同无法律效力" in text
    assert "放弃劳动仲裁权利" in text


def test_recall_floor_not_regressed() -> None:
    candidates = _candidates()
    assert len(candidates) >= MIN_CANDIDATES, f"候选数跌回 {len(candidates)} 条，低于下限 {MIN_CANDIDATES}"
    assert len({c.trigger_id for c in candidates}) >= MIN_TRIGGERS


def test_each_seeded_trap_is_recalled() -> None:
    """10 类预埋陷阱逐一命名断言，漏一类就能直接看出是哪一类。"""
    hit = {c.trigger_id for c in _candidates()}
    missing = [f"{tid}（{desc}）" for tid, desc in EXPECTED_TRIGGER_IDS.items() if tid not in hit]
    assert not missing, "以下陷阱未被召回：" + "；".join(missing)


def test_candidates_stay_inside_original_text() -> None:
    """召回层只能给原文切片：span 必须是原文子串，且每条候选都带闭集法条。"""
    text = FIXTURE.read_text(encoding="utf-8")
    for c in _candidates():
        assert c.span and c.span in text
        assert c.candidate_statute_ids
        assert c.severity_hint in {"A", "B", "C"}


def test_compliant_contract_has_no_candidate() -> None:
    """防误报：放宽触发器后合规对照样本必须仍是 0 条候选（与 validate_triggers 同口径）。"""
    candidates = detect_clauses(COMPLIANT.read_text(encoding="utf-8"), TRIGGERS)
    assert not candidates, f"合规合同被误捞：{[c.trigger_id for c in candidates]}"
