"""触发器层测试：召回、不误伤、确定性、span 取自原文。

覆盖面：每类触发器至少一条正例 / 一条反例；21 条 gold 正样本全覆盖；
候选总数上限；同一段落同一触发器只出一条。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mla.adapters.extract import _norm
from mla.config import data_path
from mla.domain.clauses import MAX_SPAN, Trigger, detect_clauses, load_triggers

TRIGGERS_PATH = Path(data_path("clauses/triggers.yaml"))
GOLD_PATH = Path(data_path("evalsets/clause_gold.jsonl"))
STATUTES_PATH = Path(data_path("statutes/statutes.jsonl"))
TRAPS_CONTRACT = Path(data_path("evalsets/contract_traps_01.txt"))
MAX_CANDIDATES = 40  # 单份合同候选上限（pattern 太松会突破这个数）

TRIGGERS = load_triggers(TRIGGERS_PATH)


def _statute_ids() -> set[str]:
    out = set()
    for line in STATUTES_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.add(str(json.loads(line)["statute_id"]))
    return out


def _gold() -> list[dict]:
    return [json.loads(x) for x in GOLD_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]


def _hit_ids(text: str) -> set[str]:
    return {c.trigger_id for c in detect_clauses(text, TRIGGERS)}


def _lcs(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _overlap(gold_span: str, cand_span: str) -> float:
    a, b = _norm(gold_span), _norm(cand_span)
    shortest = min(len(a), len(b))
    return 0.0 if not shortest else _lcs(a, b) / shortest


# ---------- 表结构 ----------


def test_triggers_loaded() -> None:
    assert len(TRIGGERS) >= 20
    ids = [t.trigger_id for t in TRIGGERS]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("TR-") for i in ids)
    assert all(t.patterns and t.name for t in TRIGGERS)


def test_statute_ids_in_library_and_unique() -> None:
    known = _statute_ids()
    for t in TRIGGERS:
        assert t.candidate_statute_ids, f"{t.trigger_id} 没有候选法条（闭集引用不能为空）"
        assert len(set(t.candidate_statute_ids)) == len(t.candidate_statute_ids)
        for sid in t.candidate_statute_ids:
            assert sid in known, f"{t.trigger_id} 引用了不存在的法条 {sid}"
        assert t.severity_hint in {"A", "B", "C"}


# ---------- 正例：每类触发器至少一条 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("甲方可单方无条件调整乙方工作岗位，乙方不得拒绝。", "TR-UNILATERAL-TRANSFER"),
        ("甲方有权单方降低乙方绩效工资，无需与乙方协商。", "TR-UNILATERAL-PAY-CUT"),
        ("乙方岗位实行综合计算工时工作制。", "TR-OUTSOURCED-HOURS-NO-APPROVAL"),
        ("乙方自愿放弃加班工资。", "TR-OVERTIME-WAIVER"),
        ("乙方须服从所有驻场安排，驻场期间不计加班。", "TR-DISPATCH-NO-OVERTIME-PAY"),
        ("年休假一律由甲方在春节假期统一抵扣。", "TR-ANNUAL-LEAVE-OFFSET"),
        ("试用期工资按底薪的百分之八十计发。", "TR-PROBATION-WAGE-BASE"),
        ("甲方可延期发放工资，最长延迟六十日。", "TR-WAGE-DELAY"),
        ("乙方离职时甲方可暂扣未结算奖金。", "TR-WAGE-WITHHOLD-ON-EXIT"),
        ("双方约定乙方放弃缴纳社会保险。", "TR-SI-WAIVER"),
        ("乙方发生工伤，全部责任由乙方自行承担。", "TR-WORK-INJURY-WAIVER"),
        ("乙方患病均属于个人原因，不属于工伤。", "TR-OCCUPATIONAL-DISEASE-DISCLAIM"),
        ("乙方无论任何原因离职，需要向甲方支付违约金五万元。", "TR-DISMISSAL-PENALTY"),
        ("乙方离职须提前六个月书面通知甲方。", "TR-RESIGN-NOTICE-EXTEND"),
        ("甲方可随时解除本合同，无需支付经济补偿。", "TR-NO-COMPENSATION-TERMINATION"),
        ("所有岗位均适用竞业限制，且不支付任何竞业限制补偿。", "TR-NONCOMPETE-ALL-STAFF"),
        ("乙方离职后永久承担保密义务。", "TR-CONFIDENTIAL-UNLIMITED"),
        ("乙方签署即代表放弃既往一切权利主张。", "TR-GENERAL-WAIVER"),
        ("争议不得向劳动仲裁委员会申请仲裁。", "TR-ARBITRATION-WAIVER"),
        ("员工手册无需公示即自动对乙方生效。", "TR-RULES-NO-PUBLICITY"),
        ("本合同优先于国家法律适用。", "TR-CONTRACT-OVER-LAW"),
    ],
)
def test_each_trigger_has_positive_case(text: str, expected: str) -> None:
    assert expected in _hit_ids(text)


# ---------- 反例：合规表述不误触发 ----------


@pytest.mark.parametrize(
    "text",
    [
        "本合同为固定期限劳动合同，期限自 2026 年 03 月 01 日起至 2029 年 02 月 28 日止。",
        "试用期：6 个月，自 2026 年 03 月 01 日至 2026 年 08 月 31 日。",
        "乙方岗位：算法助理，工作地点：长沙市望城区产业园办公区。",
        "乙方月综合薪资合计 8000 元，其中：基本工资 2200 元，绩效工资 5800 元。",
        "乙方确认：签署本合同前，已经完整阅读全部条款，自愿签署。",
        "本合同一式两份，甲乙双方各执一份，自签字盖章之日生效。",
        "甲方依法为乙方缴纳社会保险。",
        "双方协商一致，可以变更劳动合同约定的内容，并采用书面形式。",
    ],
)
def test_compliant_clause_not_triggered(text: str) -> None:
    assert _hit_ids(text) == set()


# ---------- gold 覆盖与负样本 ----------


def test_gold_positive_coverage() -> None:
    contract = TRAPS_CONTRACT.read_text(encoding="utf-8")
    candidates = detect_clauses(contract, TRIGGERS)
    # 本用例只针对 contract_traps_01.txt 这份样本合同（多合同 gold 的逐合同覆盖率由
    # scripts/validate_triggers.py 与 scripts/eval_clauses.py 报，不在此处混淆口径）
    positives = [r for r in _gold() if r["grade"] != "合规" and r["contract"] == TRAPS_CONTRACT.name]
    missing = [
        r["gold_id"] for r in positives if not any(_overlap(r["span"], c.span) >= 0.5 for c in candidates)
    ]
    assert positives, "gold 集里应有正样本"
    assert not missing, f"未被触发器覆盖的正样本：{missing}"


def test_gold_negative_not_triggered() -> None:
    contract = TRAPS_CONTRACT.read_text(encoding="utf-8")
    candidates = detect_clauses(contract, TRIGGERS)
    negatives = [r for r in _gold() if r["grade"] == "合规" and r["contract"] == TRAPS_CONTRACT.name]
    hit = [r["gold_id"] for r in negatives if any(_overlap(r["span"], c.span) >= 0.5 for c in candidates)]
    assert not hit, f"合规条款被误触发：{hit}"


def test_gold_coverage_multi_contract() -> None:
    """逐合同复核覆盖率：gold 里每份带正样本的合同，触发器都必须覆盖到它的全部正样本。

    与 `test_gold_positive_coverage` 的区别：那条只管 contract_traps_01.txt（单合同口径），
    本条把 `scripts/validate_triggers.py` 的核心判据（正样本 span 与某条候选 span 归一化后
    重叠 ≥ 50%）搬到单测里，按 `gold.contract` 分组逐份跑 `detect_clauses`。
    """
    by_contract: dict[str, list[dict]] = {}
    for row in _gold():
        if row["grade"] != "合规":
            by_contract.setdefault(str(row["contract"]), []).append(row)

    assert by_contract, "gold 集里应有正样本"

    missing: dict[str, list[str]] = {}
    for name, rows in sorted(by_contract.items()):
        candidates = detect_clauses((GOLD_PATH.parent / name).read_text(encoding="utf-8"), TRIGGERS)
        lost = [
            str(row["gold_id"])
            for row in rows
            if not any(_overlap(str(row["span"]), c.span) >= 0.5 for c in candidates)
        ]
        if lost:
            missing[name] = lost

    assert not missing, f"以下合同的 gold 正样本未被触发器覆盖：{missing}"


def test_candidate_count_within_ceiling() -> None:
    candidates = detect_clauses(TRAPS_CONTRACT.read_text(encoding="utf-8"), TRIGGERS)
    assert len(candidates) <= MAX_CANDIDATES


# ---------- 输出性质 ----------


def test_deterministic() -> None:
    text = TRAPS_CONTRACT.read_text(encoding="utf-8")
    assert detect_clauses(text, TRIGGERS) == detect_clauses(text, TRIGGERS)
    assert detect_clauses(text, load_triggers(TRIGGERS_PATH)) == detect_clauses(text, TRIGGERS)


def test_span_is_original_substring_and_bounded() -> None:
    text = TRAPS_CONTRACT.read_text(encoding="utf-8")
    for c in detect_clauses(text, TRIGGERS):
        assert c.span, "span 不能为空"
        assert c.span in text, f"span 不是原文子串：{c.span}"
        assert len(c.span) <= MAX_SPAN
        assert c.candidate_statute_ids
        assert c.name


def test_one_candidate_per_paragraph_and_trigger() -> None:
    text = TRAPS_CONTRACT.read_text(encoding="utf-8")
    keys = [(c.paragraph, c.trigger_id) for c in detect_clauses(text, TRIGGERS)]
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys), "输出必须按 (paragraph, trigger_id) 稳定排序"


def test_paragraph_number_counts_non_empty_lines() -> None:
    """paragraph 按非空行计：社保条款在原文第 32 个非空行。"""
    text = TRAPS_CONTRACT.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = detect_clauses(text, TRIGGERS)
    si = [c for c in candidates if c.trigger_id == "TR-SI-WAIVER"]
    assert si and si[0].paragraph == 32
    # 整行 ≤80 字时 span 就是整行；过长时 span 是该行的 80 字窗口（仍是原文子串）
    assert si[0].span in lines[si[0].paragraph - 1]
    assert si[0].span in text


def test_fullwidth_digits_supported() -> None:
    """全角数字 / 全角百分号不影响命中（pattern 用 \\d 与 [%％]）。"""
    hit = _hit_ids("试用期工资按基本工资的８０％发放，即１７６０元。")
    assert "TR-PROBATION-WAGE-BASE" in hit


def test_probation_trigger_capital_numeral() -> None:
    """「试用期为柒个月」要能召回（字符类含 柒捌玖）；裸写数字仍不召回。"""
    assert "TR-PROBATION-TERM-EXCEED" in _hit_ids("双方约定试用期为柒个月。")
    bare = "试用期：6 个月，自 2026 年 03 月 01 日至 2026 年 08 月 31 日。"
    assert "TR-PROBATION-TERM-EXCEED" not in _hit_ids(bare)


def test_probation_trigger_no_candidates_on_compliant_contract() -> None:
    """扩字符类不能误捞合规合同：compliant_01 该触发器仍 0 条候选。"""
    text = (GOLD_PATH.parent / "contract_compliant_01.txt").read_text(encoding="utf-8")
    hits = [c for c in detect_clauses(text, TRIGGERS) if c.trigger_id == "TR-PROBATION-TERM-EXCEED"]
    assert not hits, f"合规合同被误捞：{[c.span for c in hits]}"


def test_trigger_dataclass_is_usable_directly() -> None:
    """load_triggers 之外也能手工构造 Trigger（便于以后新增临时触发器）。"""
    t = Trigger(trigger_id="TR-TEST", name="测试", patterns=("赔钱",), candidate_statute_ids=("LCL-26",))
    got = detect_clauses("第一段。\n\n乙方需赔钱给甲方。", (t,))
    assert len(got) == 1
    assert got[0].paragraph == 2
    assert got[0].severity_hint == "C"


# ---------- 写作变体召回与否定声明排除 ----------


def test_unilateral_transfer_paraphrase_recalled() -> None:
    """不写「单方/无条件」、只写「调整岗位 + 应服从」的写法也要召回。"""
    assert "TR-UNILATERAL-TRANSFER" in _hit_ids("甲方可根据经营需要调整乙方岗位，乙方应服从。")


def test_unilateral_transfer_compliant_negotiated_not_hit() -> None:
    """合规对照组：协商一致 + 书面变更协议，没有「服从」，不得召回（对应 gold W06）。"""
    text = "甲方因生产经营需要调整乙方工作岗位的，应当与乙方协商一致并签订书面变更协议。"
    assert "TR-UNILATERAL-TRANSFER" not in _hit_ids(text)


def test_overtime_waiver_paraphrase_recalled() -> None:
    """口语写法「加班不另付报酬」也要召回（原 pattern 只认「不再另行支付」）。"""
    assert "TR-OVERTIME-WAIVER" in _hit_ids("乙方加班不另付报酬。")


def test_overtime_waiver_compliant_paid_not_hit() -> None:
    """合规对照组：按法定标准支付加班费，不得召回（对应 gold W10/W11 的写法）。"""
    assert "TR-OVERTIME-WAIVER" not in _hit_ids("乙方加班后，甲方按小时工资的 150% 支付加班费。")


@pytest.mark.parametrize(
    "text",
    [
        "公司不收取任何形式的押金、保证金。",
        "甲方无需乙方缴纳任何押金，也不扣押身份证件。",
    ],
)
def test_deposit_compliance_statement_not_hit(text: str) -> None:
    """否定式合规声明（不收取 / 无需缴纳 / 不扣押）不得召回，否则是反向误捞。"""
    assert "TR-DEPOSIT-OR-FEES" not in _hit_ids(text)


def test_deposit_collection_still_recalled() -> None:
    """同一触发器的正例仍要召回（对应 gold V06）。"""
    assert "TR-DEPOSIT-OR-FEES" in _hit_ids("入职交培训押金 500 元，离职时退还。")


# ---------- 触发器补强（加班费基数 / 服务期 / 试用期随意解除）----------

SUPPLEMENTARY_TRIGGER_IDS = (
    "TR-OT-BASE-LOW",
    "TR-SERVICE-PERIOD-PENALTY",
    "TR-PROBATION-ARBITRARY-DISMISSAL",
)


def test_supplementary_triggers_loaded_and_severity_b() -> None:
    """三条新触发器都能被 load_triggers 读入，且 severity_hint 全是 B。

    只能是 B：三者的定级都依赖合同外事实（加班费基数金额 / 是否提供专项培训 /
    试用期解除理由与录用条件），不满足 A 级「法条明文禁止、无需补事实」的口径。
    """
    by_id = {t.trigger_id: t for t in TRIGGERS}
    for tid in SUPPLEMENTARY_TRIGGER_IDS:
        assert tid in by_id, f"缺触发器 {tid}"
        assert by_id[tid].severity_hint == "B", f"{tid} 必须为 B 级，不得升为 A"
        assert by_id[tid].ask_fields, f"{tid} 是 B 级触发器，必须给出待补事实（ask_fields）"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("加班费的计算基数为基本工资。", "TR-OT-BASE-LOW"),
        ("乙方服务期内离职的，应当向甲方赔偿培训费用。", "TR-SERVICE-PERIOD-PENALTY"),
        # pattern 2：试用期 + 用人单位 + 可 + 随时 + 解除
        ("试用期内，公司有权随时解除劳动合同。", "TR-PROBATION-ARBITRARY-DISMISSAL"),
        # pattern 3：试用期 + 不合格 + 即/就/一律/直接 + 解除
        ("乙方试用期考核不合格的，甲方即可解除劳动合同。", "TR-PROBATION-ARBITRARY-DISMISSAL"),
        # TR-WAGE-WITHHOLD-ON-EXIT 追加的 pattern：以「交接保证金」为名扣款
        ("离职时扣发当月绩效，作为交接保证金。", "TR-WAGE-WITHHOLD-ON-EXIT"),
        ("未办理交接的，甲方不予结算工资。", "TR-WAGE-WITHHOLD-ON-EXIT"),
    ],
)
def test_supplementary_trigger_positive_cases(text: str, expected: str) -> None:
    assert expected in _hit_ids(text)


def test_probation_arbitrary_dismissal_long_subject_phrase_recalled() -> None:
    """「甲方有权在试用期内随时解除劳动合同」：主语与「随时」隔 5 个字，正则窗口必须够宽。"""
    assert "TR-PROBATION-ARBITRARY-DISMISSAL" in _hit_ids("甲方有权在试用期内随时解除劳动合同。")


def test_probation_arbitrary_dismissal_laborer_resignation_not_hit() -> None:
    """合规对照：劳动者一方的「试用期内提前 3 日通知即可解除」不得召回。

    pattern 若只要求「试用期 + 解除」，会命中 compliant_01 第 35 行的合法条款，
    因此收紧为「用人单位主语 + 随时/任意」或「不合格 + 即/就/一律/直接」两类（见 yaml 注释）。
    """
    text = "乙方提前 30 日以书面形式通知甲方，可以解除本合同；试用期内提前 3 日通知即可解除。"
    assert "TR-PROBATION-ARBITRARY-DISMISSAL" not in _hit_ids(text)
    compliant = (GOLD_PATH.parent / "contract_compliant_01.txt").read_text(encoding="utf-8")
    hits = [
        c for c in detect_clauses(compliant, TRIGGERS) if c.trigger_id == "TR-PROBATION-ARBITRARY-DISMISSAL"
    ]
    assert not hits, f"合规合同被误捞：{[c.span for c in hits]}"


# 四份样本合同的候选数基线（与 scripts/validate_triggers.py 的 EXPECTED_CANDIDATES 同源）。
# 新增/放宽触发器后这里若变红，说明正则过宽：修正则，不要改这个数字。
CONTRACT_CANDIDATE_BASELINE = {
    "contract_traps_01.txt": 22,
    "contract_traps_02.txt": 9,
    "contract_traps_03.txt": 12,
    "contract_compliant_01.txt": 0,
}


@pytest.mark.parametrize(("contract_name", "expected"), sorted(CONTRACT_CANDIDATE_BASELINE.items()))
def test_contract_candidate_counts_baseline(contract_name: str, expected: int) -> None:
    text = (GOLD_PATH.parent / contract_name).read_text(encoding="utf-8")
    actual = len(detect_clauses(text, TRIGGERS))
    assert actual == expected, f"{contract_name} 候选数期望 {expected}，实测 {actual}"
