"""规格 005（结果页信息架构收敛）：快照不变量、默认态/开关态、条号常驻、旧记录容错。

两层保护：
1. **不变量**（AC-05）：`summarize_verdict` 与 `sort_items` 的输出来自领域层，本轮只动呈现层；
   这里把改造前的输出存成 JSON 字面快照（硬编码，不在运行时生成），任何字段变化都会直接红。
2. **呈现层**（AC-01/02/04/06/07/08）：默认态第三层的元素一个都不产生（不是渲染后 CSS 隐藏）；
   开关态只增加第三层，第一二层规范化后逐行相同；条号在折叠器外，原文与类案默认折叠。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import Paths, data_path  # noqa: E402
from mla.domain.grading import display_of, sort_items  # noqa: E402
from mla.domain.models import (  # noqa: E402
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    Severity,
    Suggestion,
    TaskMode,
    UndeterminedRule,
    UnmetTerm,
)
from mla.domain.verdict import summarize_verdict  # noqa: E402
from mla.ui.app import (  # noqa: E402
    RESULT_DISCLAIMER,
    SHOW_QUERY_PATH_HELP,
    SHOW_QUERY_PATH_KEY,
    SHOW_QUERY_PATH_LABEL,
    show_query_path,
    split_verdict_details,
)

# ---------------- 夹具（快照与界面断言共用同一批任务） ----------------


def _rule_item(item_id: str, finding: str, statute_id: str, rule_id: str) -> CheckItem:
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK,
        clause_ref="",
        finding=finding,
        source_id=statute_id,
        severity=Severity.HARD,
        rule_id=rule_id,
    )


def _legacy_task() -> CheckTask:
    """旧路径（`model_first=False`）：规则条目 + 条款审查 A/C + 必备条款 + 判不了的规则。"""
    return CheckTask(
        task_id="T-LEGACY-SNAP",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        contract_chars=1200,
        clause_review=True,
        items=[
            _rule_item("I-1", "试用期约定超过法定上限", "LCL-19", "R-PROBATION-6M"),
            _rule_item("I-2", "竞业限制期限超过两年", "LCL-23", "R-NONCOMPETE-TERM-2Y"),
        ],
        clause_findings=[
            ClauseFinding(
                paragraph=3,
                span="试用期工资为转正工资的 60%。",
                grade="A",
                reason="该约定与第二十条关于试用期工资的规定不符",
                statute_ids=("LCL-20",),
                trigger_id="TR-PROBATION-WAGE-BASE",
            ),
            ClauseFinding(
                paragraph=5,
                span="竞业限制期限为五年。",
                grade="C",
                reason="竞业限制期限是否超过两年需人工确认",
                statute_ids=("LCL-23",),
                trigger_id="TR-NONCOMPETE-TERM",
            ),
        ],
        missing_terms=[
            MissingTerm(
                term_id="MT-1",
                name="劳动合同期限",
                clause_no="（一）",
                statute_id="LCL-17",
                status=MissingStatus.WRITTEN,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-2",
                name="工作内容和工作地点",
                clause_no="（二）",
                statute_id="LCL-17",
                status=MissingStatus.ABSENT,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-3",
                name="劳动报酬",
                clause_no="（六）",
                statute_id="LCL-17",
                status=MissingStatus.UNSEEN,
                certainty="低",
            ),
            MissingTerm(
                term_id="MT-9",
                name="法律、法规规定应当纳入劳动合同的其他事项",
                clause_no="（九）",
                statute_id="LCL-17",
                status=MissingStatus.MANUAL,
                certainty="低",
            ),
        ],
        undetermined=[
            UndeterminedRule(
                rule_id="R-PROBATION-ONCE",
                name="同一用人单位与同一劳动者只能约定一次试用期",
                statute_id="LCL-19",
                missing_fields=("probation_times",),
                reason="field_missing",
            ),
            UndeterminedRule(
                rule_id="R-OT-MONTH-36H",
                name="每月加班不得超过三十六小时",
                statute_id="LL-41",
                missing_fields=("working_hours", "monthly_overtime_hours"),
                reason="premise_missing",
            ),
        ],
        unmet=[
            UnmetTerm(
                rule_id="R-WPR-MONTHLY-PAY",
                name="工资至少每月支付一次",
                statute_id="WPR-7",
                fields=("pay_times_per_month",),
            )
        ],
        statutory_na=3,
        rule_coverage=RuleCoverage(total=20, violated=2, compliant=15, not_applicable=1, unknown=2),
        suggestions=[Suggestion(text="把试用期调整到法定上限内。", source_ids=("LCL-19",))],
        latency_s=1.25,
        token_in=120,
        token_out=60,
        ask_rounds=1,
        questions=["试用期工资是多少？"],
        verify_ok=True,
    )


def _consensus_item(
    item_id: str,
    finding: str,
    statute_id: str,
    grade: str,
    source: str,
    element_id: str,
    *,
    paragraph: str = "",
    note: str = "",
    rule_id: str = "",
) -> CheckItem:
    return CheckItem(
        item_id=item_id,
        kind=ItemKind.RISK if grade in ("A", "B") else ItemKind.UNKNOWN,
        clause_ref=paragraph,
        finding=finding,
        source_id=statute_id,
        evidence_text="试用期为十二个月。" if paragraph else "",
        grade=grade,
        source=source,
        note=note,
        statute_ids=(statute_id,),
        element_id=element_id,
        rule_id=rule_id,
    )


def _model_first_task() -> CheckTask:
    """新路径（`model_first=True`）：模型结论 + 程序追加的数值复核条目，混合等级与影响度。"""
    return CheckTask(
        task_id="T-MODEL-FIRST-SNAP",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        contract_chars=1500,
        model_first=True,
        clause_review=True,
        items=[
            _consensus_item(
                "CF-4-TR-PROBATION-TERM-EXCEED",
                "该约定与第十九条关于试用期期限的规定不符",
                "LCL-19",
                "A",
                "trigger",
                "EL-11",
                paragraph="4",
                note="数值校验一致",
            ),
            _consensus_item(
                "CF-6-TR-NONCOMPETE-TERM",
                "竞业限制期限是否超过两年需补充事实",
                "LCL-23",
                "B",
                "trigger",
                "EL-3",
                paragraph="6",
                note="需补充事实：竞业限制期限",
            ),
            _consensus_item(
                "RC-R-NONCOMPETE-TERM-2Y",
                "数值与第二十三条不一致，请核对合同约定",
                "LCL-23",
                "C",
                "rule_check",
                "EL-3",
                note="数值与第二十三条不一致，请核对合同约定",
                rule_id="R-NONCOMPETE-TERM-2Y",
            ),
            _consensus_item(
                "CF-2-TR-PARTY-NAME",
                "称谓前后不一致",
                "LCL-17",
                "C",
                "trigger",
                "EL-1",
                paragraph="2",
            ),
        ],
        missing_terms=[
            MissingTerm(
                term_id="MT-1",
                name="劳动合同期限",
                clause_no="（一）",
                statute_id="LCL-17",
                status=MissingStatus.WRITTEN,
                certainty="高",
            ),
            MissingTerm(
                term_id="MT-4",
                name="劳动保护、劳动条件和职业危害防护",
                clause_no="（四）",
                statute_id="LCL-17",
                status=MissingStatus.ABSENT,
                certainty="高",
            ),
        ],
        rule_coverage=RuleCoverage(total=20, violated=0, compliant=17, not_applicable=2, unknown=1),
        suggestions=[Suggestion(text="补充竞业限制期限的约定。", source_ids=("LCL-23",))],
        latency_s=2.5,
        token_in=200,
        token_out=90,
    )


def _empty_task() -> CheckTask:
    """空条目：没有 items、没有条款结论、没有必备条款——顶部只能写“覆盖不完整”。"""
    return CheckTask(task_id="T-EMPTY-SNAP", mode=TaskMode.CHECK, enterprise_id="E1")


def _mixed_grades_task() -> CheckTask:
    """混合等级：A / B / C 与 rule_check 都在，用于固定排序与确定性口径。"""
    base = _model_first_task()
    base.task_id = "T-MIXED"
    return base


def _same_layer_task() -> CheckTask:
    """同层排序：同一影响度（EL-3 中）内四键需要决出先后。"""
    items = [
        _consensus_item("S-P9", "段落九的冲突", "LCL-23", "A", "trigger", "EL-3", paragraph="9"),
        _consensus_item("S-RC", "程序复核条目", "LCL-23", "C", "rule_check", "EL-3", note="复核"),
        _consensus_item("S-P2", "段落二的冲突", "LCL-23", "A", "trigger", "EL-3", paragraph="2"),
        _consensus_item("S-C5", "段落五的待确认", "LCL-23", "C", "trigger", "EL-3", paragraph="5"),
    ]
    return CheckTask(
        task_id="T-SAME-LAYER",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        model_first=True,
        clause_review=True,
        items=items,
    )


FIXTURES = {
    "legacy": _legacy_task,
    "model_first": _model_first_task,
    "empty": _empty_task,
    "mixed_grades": _mixed_grades_task,
    "same_layer": _same_layer_task,
}


def _verdict_snapshot(task: CheckTask) -> dict:
    verdict = summarize_verdict(task)
    return {
        "status": verdict.status,
        "risk_count": verdict.risk_count,
        "review_count": verdict.review_count,
        "headline": verdict.headline,
        "details": list(verdict.details),
    }


def _items_snapshot(task: CheckTask) -> list[dict]:
    """条目按 `sort_items` 的顺序逐条落快照：ID / 正文 / 影响度 / 确定性 / 四键。"""
    rows: list[dict] = []
    for item in sort_items(task.items):
        level = display_of(item)
        rows.append(
            {
                "item_id": item.item_id,
                "finding": item.finding,
                "impact": level.impact,
                "certainty": level.certainty,
                "sort_key": list(level.sort_key),
            }
        )
    return rows


# 改造前（v0.3.0）`summarize_verdict` / `sort_items` 对这些夹具的输出：JSON 字面量，不在运行时生成。
SNAPSHOT: dict[str, dict] = {
    "legacy": {
        "verdict": {
            "status": "risk",
            "risk_count": 3,
            "review_count": 0,
            "headline": "发现 3 条与法条存在冲突的事项",
            "details": [
                "规则 18/20 可判定",
                "条款审查已运行",
                "条款审查发现 1 条明确冲突或显著失衡",
                "1 条条款待人工确认",
                "2 条规则因缺少事实无法判定",
                "必备条款 1/8 已写",
            ],
        },
        "sorted_items": [
            {
                "item_id": "I-1",
                "finding": "试用期约定超过法定上限",
                "impact": "中",
                "certainty": "待确认",
                "sort_key": [1, 1, 0, 0],
            },
            {
                "item_id": "I-2",
                "finding": "竞业限制期限超过两年",
                "impact": "中",
                "certainty": "待确认",
                "sort_key": [1, 1, 0, 0],
            },
        ],
    },
    "model_first": {
        "verdict": {
            "status": "risk",
            "risk_count": 2,
            "review_count": 2,
            "headline": "发现 2 条与法条存在冲突的事项",
            "details": ["规则 19/20 可判定", "条款审查已运行", "2 条待确认", "必备条款 1/8 已写"],
        },
        "sorted_items": [
            {
                "item_id": "CF-4-TR-PROBATION-TERM-EXCEED",
                "finding": "该约定与第十九条关于试用期期限的规定不符",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 0, 4],
            },
            {
                "item_id": "RC-R-NONCOMPETE-TERM-2Y",
                "finding": "数值与第二十三条不一致，请核对合同约定",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 1, 0],
            },
            {
                "item_id": "CF-6-TR-NONCOMPETE-TERM",
                "finding": "竞业限制期限是否超过两年需补充事实",
                "impact": "高",
                "certainty": "待确认",
                "sort_key": [0, 1, 0, 6],
            },
            {
                "item_id": "CF-2-TR-PARTY-NAME",
                "finding": "称谓前后不一致",
                "impact": "低",
                "certainty": "待确认",
                "sort_key": [2, 1, 0, 2],
            },
        ],
    },
    "empty": {
        "verdict": {
            "status": "partial",
            "risk_count": 0,
            "review_count": 0,
            "headline": "未发现硬性风险，但覆盖不完整",
            "details": ["条款审查未运行", "合同正文较短，结论可能不完整"],
        },
        "sorted_items": [],
    },
    "mixed_grades": {
        "verdict": {
            "status": "risk",
            "risk_count": 2,
            "review_count": 2,
            "headline": "发现 2 条与法条存在冲突的事项",
            "details": ["规则 19/20 可判定", "条款审查已运行", "2 条待确认", "必备条款 1/8 已写"],
        },
        "sorted_items": [
            {
                "item_id": "CF-4-TR-PROBATION-TERM-EXCEED",
                "finding": "该约定与第十九条关于试用期期限的规定不符",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 0, 4],
            },
            {
                "item_id": "RC-R-NONCOMPETE-TERM-2Y",
                "finding": "数值与第二十三条不一致，请核对合同约定",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 1, 0],
            },
            {
                "item_id": "CF-6-TR-NONCOMPETE-TERM",
                "finding": "竞业限制期限是否超过两年需补充事实",
                "impact": "高",
                "certainty": "待确认",
                "sort_key": [0, 1, 0, 6],
            },
            {
                "item_id": "CF-2-TR-PARTY-NAME",
                "finding": "称谓前后不一致",
                "impact": "低",
                "certainty": "待确认",
                "sort_key": [2, 1, 0, 2],
            },
        ],
    },
    "same_layer": {
        "verdict": {
            "status": "risk",
            "risk_count": 3,
            "review_count": 1,
            "headline": "发现 3 条与法条存在冲突的事项",
            "details": ["条款审查已运行", "1 条待确认", "合同正文较短，结论可能不完整"],
        },
        "sorted_items": [
            {
                "item_id": "S-P2",
                "finding": "段落二的冲突",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 0, 2],
            },
            {
                "item_id": "S-P9",
                "finding": "段落九的冲突",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 0, 9],
            },
            {
                "item_id": "S-RC",
                "finding": "程序复核条目",
                "impact": "高",
                "certainty": "确定冲突",
                "sort_key": [0, 0, 1, 0],
            },
            {
                "item_id": "S-C5",
                "finding": "段落五的待确认",
                "impact": "高",
                "certainty": "待确认",
                "sort_key": [0, 1, 0, 5],
            },
        ],
    },
}


# ---------------- AC-05：不变量快照 ----------------
@pytest.mark.parametrize("name", list(FIXTURES))
def test_snapshot_matches_v030(name: str) -> None:
    """结论句、风险/待确认数、条目 ID/正文、影响度、确定性与四键排序逐字段等于 v0.3.0。"""
    task = FIXTURES[name]()
    actual = {"verdict": _verdict_snapshot(task), "sorted_items": _items_snapshot(task)}
    assert actual == SNAPSHOT[name]


def test_snapshot_is_json_literal_not_generated() -> None:
    """快照必须是字面量：文件里能 `json.loads` 出来，且覆盖五种夹具（新旧路径/空/混合/同层）。"""
    assert set(SNAPSHOT) == {"legacy", "model_first", "empty", "mixed_grades", "same_layer"}
    dumped = json.dumps(SNAPSHOT, ensure_ascii=False)
    assert json.loads(dumped) == SNAPSHOT
    assert "CF-4-TR-PROBATION-TERM-EXCEED" in dumped  # 确实落了具体条目，不是空壳


# ---------------- 结果页渲染的公共脚手架 ----------------
def _repo(tmp_path: Path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "ia.sqlite3"))


PRELUDE = """
QUERY_PATH_ON = False
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import (
    CheckItem,
    CheckTask,
    ClauseFinding,
    ItemKind,
    MissingStatus,
    MissingTerm,
    RuleCoverage,
    Severity,
    Suggestion,
    TaskMode,
    UndeterminedRule,
    UnmetTerm,
)
from mla.ui.app import SHOW_QUERY_PATH_KEY, _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
if QUERY_PATH_ON:
    repo.set_setting(SHOW_QUERY_PATH_KEY, "1")
"""

MISSING_TERMS = """
missing_terms = [
    MissingTerm(
        term_id="MT-1", name="劳动合同期限", clause_no="（一）", statute_id="LCL-17",
        status=MissingStatus.WRITTEN, certainty="高",
    ),
    MissingTerm(
        term_id="MT-2", name="工作内容和工作地点", clause_no="（二）", statute_id="LCL-17",
        status=MissingStatus.ABSENT, certainty="高",
    ),
]
"""

RICH_SCRIPT = (
    PRELUDE
    + MISSING_TERMS
    + """
old = CheckTask(
    task_id="T-RICH-OLD",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    contract_chars=1200,
    clause_review=True,
    items=[
        CheckItem(
            item_id="I-1", kind=ItemKind.RISK, clause_ref="第十九条",
            finding="试用期约定超过法定上限", source_id="LCL-19",
            severity=Severity.HARD, rule_id="R-PROBATION-6M",
        )
    ],
    clause_findings=[
        ClauseFinding(
            paragraph=3, span="试用期工资为转正工资的 60%。", grade="A",
            reason="该约定与第二十条关于试用期工资的规定不符",
            statute_ids=("LCL-20",), trigger_id="TR-PROBATION-WAGE-BASE",
        )
    ],
    suggestions=[Suggestion(text="把试用期调整到法定上限内。", source_ids=("LCL-19",))],
    facts={"term_years": "3"},
    undetermined=[
        UndeterminedRule(
            rule_id="R-PROBATION-ONCE",
            name="同一用人单位与同一劳动者只能约定一次试用期",
            statute_id="LCL-19", missing_fields=("probation_times",), reason="field_missing",
        )
    ],
    unmet=[
        UnmetTerm(
            rule_id="R-WPR-MONTHLY-PAY", name="工资至少每月支付一次",
            statute_id="WPR-7", fields=("pay_times_per_month",),
        )
    ],
    statutory_na=3,
    rule_coverage=RuleCoverage(total=20, violated=1, compliant=15, not_applicable=2, unknown=2),
    missing_terms=missing_terms,
    latency_s=1.25,
    token_in=120,
    token_out=60,
    ask_rounds=2,
    questions=["试用期工资是多少？"],
    verify_ok=False,
    degraded=True,
    errors=["抽取回退：模型抽取失败"],
)
_render_task(old, repo)

new = CheckTask(
    task_id="T-RICH-NEW",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    contract_chars=1200,
    model_first=True,
    clause_review=True,
    items=[
        CheckItem(
            item_id="CF-4-TR-PROBATION-TERM-EXCEED", kind=ItemKind.RISK, clause_ref="4",
            finding="该约定与第十九条关于试用期期限的规定不符", source_id="LCL-19",
            evidence_text="试用期为十二个月。", severity=Severity.HARD, grade="A",
            source="trigger", note="数值校验一致", statute_ids=("LCL-19",), element_id="EL-11",
        ),
        CheckItem(
            item_id="RC-R-NONCOMPETE-TERM-2Y", kind=ItemKind.UNKNOWN, clause_ref="",
            finding="数值与第二十三条不一致，请核对合同约定", source_id="LCL-23",
            rule_id="R-NONCOMPETE-TERM-2Y", grade="C", source="rule_check",
            note="数值与第二十三条不一致，请核对合同约定", statute_ids=("LCL-23",),
        ),
    ],
    missing_terms=missing_terms,
    rule_coverage=RuleCoverage(total=20, violated=0, compliant=17, not_applicable=2, unknown=1),
    suggestions=[Suggestion(text="补充竞业限制期限的约定。", source_ids=("LCL-23",))],
    latency_s=2.5,
    token_in=200,
    token_out=90,
)
_render_task(new, repo)
"""
)

# 第三层专有内容（默认态一个都不该出现）。逐项对应规格 005 第 2 节第三层的清单。
PROCESS_MARKERS: tuple[str, ...] = (
    "查询路径与判定来源",
    "引用条文",
    "1.25 秒",
    "2.50 秒",
    "规则 18/20 可判定",
    "规则 19/20 可判定",
    "条款审查已运行",
    "条款审查未运行",
    "因缺少事实无法判定",
    "必备条款 1/8 已写",
    "已写 1 类",
    "规则判定·确定性",
    "模型审查·A",
    "程序数值校验",
    "A 与法条明文冲突",
    "数值校验一致",
    "引用不可溯源",
    "补问 2 轮",
    "已降级",
    "抽取已回退",
    "本次提出 1 个追问",
    "试用期工资是多少？",
    "Token 输入 120",
    "任务 T-RICH-OLD",
)

_HELP_MARKERS: tuple[str, ...] = ("等级含义", "影响度由合同要素决定")


def _render_app(tmp_path: Path, script: str, *, query_path: bool = False) -> AppTest:
    text = script.replace("DB_PATH", repr(str(tmp_path / "ia.sqlite3")))
    if query_path:
        text = text.replace("QUERY_PATH_ON = False", "QUERY_PATH_ON = True")
    return AppTest.from_string(text, default_timeout=90).run()


def _visible_text(at: AppTest) -> str:
    parts = [str(m.value) for m in at.markdown]
    parts += [str(c.value) for c in at.caption]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += [str(e.value) for e in at.error]
    parts += [str(e.value) for e in at.warning]
    parts += [str(e.value) for e in at.info]
    parts += [e.label for e in at.expander]
    return "\n".join(parts)


def _help_text(at: AppTest) -> str:
    """所有控件/元素的 help：AC-01 要求帮助文本也不能漏出判定来源。"""
    helps: list[str] = []
    for name in (
        "markdown",
        "caption",
        "button",
        "download_button",
        "checkbox",
        "text_area",
        "text_input",
        "metric",
        "expander",
    ):
        for element in at.get(name):
            proto = getattr(element, "proto", None)
            value = getattr(proto, "help", "") if proto is not None else ""
            if value:
                helps.append(str(value))
    return "\n".join(helps)


_BADGE = re.compile(r":[a-z]+-badge\[[^\]]*\]\s*")


def _layer12(at: AppTest) -> list[str]:
    """第一二层的规范化快照：去徽标标记与“已写 N 类，”前缀，只留结论文字本身。"""
    lines: list[str] = []
    for element in list(at.markdown) + list(at.caption):
        text = _BADGE.sub("", str(element.value or ""))
        text = re.sub(r"已写 \d+ 类，", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(text)
    return lines


# ---------------- AC-01 / AC-02：两态 ----------------
def test_default_hidden(tmp_path: Path) -> None:
    """缺键默认态：第三层的内容、帮助文本一个都不出现（含追问正文与运行详情）。"""
    at = _render_app(tmp_path, RICH_SCRIPT)
    assert not at.exception

    texts = _visible_text(at)
    for marker in PROCESS_MARKERS:
        assert marker not in texts, f"默认态仍渲染了第三层内容：{marker}"
    helps = _help_text(at)
    for marker in _HELP_MARKERS:
        assert marker not in helps, f"默认态帮助文本泄露判定来源：{marker}"

    # 第一层仍然完整：结论句、条目、条号、建议正文、必备条款独立计数、免责
    assert "发现 2 条与法条存在冲突的事项" in texts
    assert "**试用期约定超过法定上限**" in texts
    assert "《劳动合同法》第十九条" in texts
    assert "把试用期调整到法定上限内。" in texts
    assert "补充竞业限制期限的约定。" in texts
    assert "九类必备条款：" in texts
    assert RESULT_DISCLAIMER in texts
    assert "导出 Word 报告" in [b.label for b in at.get("download_button")]


def test_enabled(tmp_path: Path) -> None:
    """开关态：第三层出现，第一二层规范化后与默认态逐行相同（只加不减）。"""
    default = _render_app(tmp_path, RICH_SCRIPT)
    enabled = _render_app(tmp_path, RICH_SCRIPT, query_path=True)
    assert not default.exception and not enabled.exception

    texts = _visible_text(enabled)
    for marker in (
        "查询路径与判定来源",
        "引用条文",
        "规则 18/20 可判定",
        "条款审查已运行",
        "必备条款 1/8 已写",
        "规则判定·确定性",
        "模型审查·A",
        "程序数值校验",
        "数值校验一致",
        "引用不可溯源",
        "补问 2 轮",
        "已降级",
        "抽取已回退",
        "本次提出 1 个追问",
        "Token 输入 120",
        "任务 T-RICH-OLD",
    ):
        assert marker in texts, f"开关态缺少第三层内容：{marker}"
    assert "A 与法条明文冲突" in _help_text(enabled)  # 等级 help 只在开关态出现
    assert "等级含义" in _help_text(enabled)

    # 第一层结论不变：headline 与条目正文逐条相同
    def _headline(app: AppTest) -> str:
        return next(m.value for m in app.markdown if m.value.startswith("### "))

    assert _headline(default) == _headline(enabled)

    # 规范化后默认态是多集子集：开关没有改写任何第一二层文字
    missing = Counter(_layer12(default)) - Counter(_layer12(enabled))
    assert not missing, f"开关改写了第一二层内容：{dict(missing)}"

    # 顺序也不变：默认态逐行按原顺序出现在开关态里（开关只插入，不重排）
    remaining = _layer12(enabled)
    for line in _layer12(default):
        assert line in remaining, f"开关态丢掉了第一二层的一行：{line}"
        remaining = remaining[remaining.index(line) + 1 :]


# ---------------- AC-04：条号常驻 / 原文与类案折叠 ----------------
def test_statute_numbers_stay_outside_folds(tmp_path: Path) -> None:
    """条号在折叠器外仍可见；原文容器与类案容器都是 `expanded=False`，且不露内部 ID。"""
    at = _render_app(tmp_path, RICH_SCRIPT, query_path=True)
    assert not at.exception

    texts = _visible_text(at)
    assert "《劳动合同法》第十九条" in texts  # 旧路径规则条目的常驻条号
    assert "《劳动合同法》第二十条" in texts  # 条款结论的依据条号
    assert "第十七条第二项" in texts  # 必备条款的条文定位

    labels = [e.label for e in at.expander]
    assert any(label.endswith("原文") for label in labels)
    assert "类案参照" in labels
    assert all(e.proto.expanded is False for e in at.expander), "原文与类案的容器必须默认折叠"
    assert "LCL-" not in texts
    assert "C-0" not in texts  # 类案内部编号不外露


def test_cases_collapsed_with_query_path(tmp_path: Path) -> None:
    """开关态同样折叠：第三层只增加过程信息，不展开依据原文。"""
    at = _render_app(tmp_path, RICH_SCRIPT, query_path=True)
    assert not at.exception
    assert all(e.proto.expanded is False for e in at.expander)
    assert any("不构成本案结论" in str(c.value) for c in at.caption)


QA_SCRIPT = (
    PRELUDE
    + """
import streamlit as st
from mla.domain.models import Role, User

user = User(
    user_id="U1", username="ui", password_hash="x", salt="y", role=Role.USER, enterprise_id="E1"
)
check = CheckTask(
    task_id="T-QA1",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    items=[
        CheckItem(
            item_id="I-1", kind=ItemKind.RISK, clause_ref="", finding="试用期约定超过法定上限",
            source_id="LCL-19", severity=Severity.HARD, rule_id="R-PROBATION-6M",
        )
    ],
)
st.session_state["last_qa_context"] = {
    "task_id": "T-QA1",
    "qa": CheckTask(
        task_id="T-QA2",
        mode=TaskMode.QA,
        enterprise_id="E1",
        suggestions=[Suggestion(text="试用期最长不得超过六个月。", source_ids=("LCL-19",))],
        agent_trace=["检索法条", "生成回答"],
        token_in=40,
        token_out=20,
    ),
}
_render_task(check, repo, user)
"""
)


def test_qa_statute_folded_and_detail_gated(tmp_path: Path) -> None:
    """问答：原文默认折叠、条号常驻；运行详情（工具调用 / Token）受同一开关控制。"""
    default = _render_app(tmp_path, QA_SCRIPT)
    assert not default.exception
    default_texts = _visible_text(default)
    assert "结论与依据（法条原文）" in default_texts
    assert "试用期最长不得超过六个月。" in default_texts
    assert "《劳动合同法》第十九条" in [e.label for e in default.expander]
    statute_folds = [e for e in default.expander if e.label == "《劳动合同法》第十九条"]
    assert statute_folds and statute_folds[0].proto.expanded is False
    assert "工具调用" not in default_texts
    assert "Token" not in default_texts

    enabled = _render_app(tmp_path, QA_SCRIPT, query_path=True)
    assert not enabled.exception
    enabled_texts = _visible_text(enabled)
    assert "工具调用 检索法条 → 生成回答" in enabled_texts
    assert "Token 输入 40 · 输出 20" in enabled_texts


# ---------------- AC-06：必备条款独立计数 / 建议正文 ----------------
def test_missing_terms_counts_stay_independent(tmp_path: Path) -> None:
    """必备条款缺失/未见/人工计数独立且不变，未并入冲突或待确认条数。"""
    at = _render_app(tmp_path, RICH_SCRIPT)
    assert not at.exception
    texts = _visible_text(at)

    assert "本次检查《劳动合同法》第十七条九类必备条款：缺失 1 类，未见 0 类，需人工确认 0 类。" in texts
    assert "缺失：未见工作内容和工作地点的约定（第十七条第二项）" in texts
    # 独立计数：顶部结论只数冲突/待确认，不把必备条款类数加进去
    assert "发现 2 条与法条存在冲突的事项" in texts
    assert "必备条款" not in next(m.value for m in at.markdown if m.value.startswith("### "))


def test_suggestions_body_kept_without_count_metric(tmp_path: Path) -> None:
    """处理建议正文默认可见（不折叠），顶部不再有“处理建议”数量指标。"""
    at = _render_app(tmp_path, RICH_SCRIPT)
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "处理建议" not in labels
    assert not at.metric  # 默认态没有任何指标
    assert any("把试用期调整到法定上限内。" in str(m.value) for m in at.markdown)
    assert any("补充竞业限制期限的约定。" in str(m.value) for m in at.markdown)


# ---------------- AC-07：旧任务（缺列 / 缺 JSON 键） ----------------
LEGACY_SCHEMA = """
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, enterprise_id TEXT, mode TEXT, variant TEXT,
  facts TEXT, items TEXT, suggestions TEXT, verify_ok INTEGER, degraded INTEGER,
  latency_s REAL, token_in INTEGER, token_out INTEGER, created_at TEXT
);
"""


def test_legacy_record_renders_without_backfill(tmp_path: Path) -> None:
    """缺新列/新 JSON 键的旧任务能打开：无异常、不补算覆盖率、不伪造“已运行”。"""
    db = tmp_path / "legacy-ia.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "T-OLD",
            "E1",
            "核对",
            "full",
            "{}",
            '[{"item_id":"R-PROBATION-6M","kind":"风险","clause_ref":"","finding":"试用期超限",'
            '"source_id":"LCL-19","severity":"硬性","rule_id":"R-PROBATION-6M"}]',
            "[]",
            1,
            0,
            0.5,
            0,
            0,
            "2026-11-01",
        ),
    )
    conn.commit()
    conn.close()

    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    legacy_repo = SqliteRepository(paths=paths, db_path=str(db))
    old = legacy_repo.list_tasks("E1")[0]
    assert old.model_first is False

    app = AppTest.from_string(
        """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
task = repo.list_tasks("E1")[0]
_render_task(task, repo)
""".replace("DB_PATH", repr(str(db))),
        default_timeout=60,
    ).run()
    assert not app.exception
    texts = _visible_text(app)
    assert "试用期超限" in texts
    assert "《劳动合同法》第十九条" in texts  # 条号照旧常驻
    # 旧记录没有覆盖率与条款审查字段：不补算、不写“已运行”
    assert "规则 " not in texts
    assert "条款审查已运行" not in texts
    assert "必备条款" not in texts

    enabled = AppTest.from_string(
        """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.ui.app import SHOW_QUERY_PATH_KEY, _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
repo.set_setting(SHOW_QUERY_PATH_KEY, "1")
task = repo.list_tasks("E1")[0]
_render_task(task, repo)
""".replace("DB_PATH", repr(str(db))),
        default_timeout=60,
    ).run()
    assert not enabled.exception
    enabled_texts = _visible_text(enabled)
    assert "耗时 0.50 秒" in enabled_texts
    assert "条款审查未运行" in enabled_texts  # 事实是“没跑过”，不是伪造的“已运行”


# ---------------- AC-03：设置项读取口径 ----------------
def test_query_path_setting_off_unless_exactly_one(tmp_path: Path) -> None:
    """缺键 / 空串 / 0 / 未知值都是关；只有 "1" 为开，且读取不回写数据库。"""
    repo = _repo(tmp_path)
    assert show_query_path(repo) is False  # 缺键
    assert repo.get_setting(SHOW_QUERY_PATH_KEY) == ""  # 读一次不落库

    for value in ("", "0", "true", "yes", " 1", "01"):
        repo.set_setting(SHOW_QUERY_PATH_KEY, value)
        assert show_query_path(repo) is False, value
    repo.set_setting(SHOW_QUERY_PATH_KEY, "1")
    assert show_query_path(repo) is True


def test_query_path_setting_survives_new_repo(tmp_path: Path) -> None:
    """写一次后换一个仓库实例仍读到同一个值（全局开关，不按用户或任务保存）。"""
    first = _repo(tmp_path)
    first.set_setting(SHOW_QUERY_PATH_KEY, "1")
    assert show_query_path(_repo(tmp_path)) is True

    first.set_setting(SHOW_QUERY_PATH_KEY, "0")
    assert show_query_path(_repo(tmp_path)) is False


# ---------------- AC-08：免责与拒答 ----------------
def test_disclaimer_last_and_once(tmp_path: Path) -> None:
    """免责仍在最后且只出现一次；对外词与免责文本未改。"""
    at = _render_app(tmp_path, RICH_SCRIPT)
    assert not at.exception
    captions = [str(c.value) for c in at.caption]
    assert captions.count(RESULT_DISCLAIMER) == 2  # 两个任务各一行，每个结果页只出现一次
    assert captions[-1] == RESULT_DISCLAIMER
    assert "违法" not in RESULT_DISCLAIMER and "无效" not in RESULT_DISCLAIMER

    single = _render_app(
        tmp_path,
        PRELUDE + "\ntask = CheckTask(task_id='T-DISC', mode=TaskMode.CHECK, enterprise_id='E1')\n"
        "_render_task(task, repo)\n",
    )
    assert not single.exception
    assert [str(c.value) for c in single.caption].count(RESULT_DISCLAIMER) == 1


def test_refused_reason_visible_by_default(tmp_path: Path) -> None:
    """拒答原因默认可见（不属第三层），且默认态没有过程信息。"""
    at = _render_app(
        tmp_path,
        PRELUDE + "\ntask = CheckTask(task_id='T-REFUSE', mode=TaskMode.CHECK, enterprise_id='E1',"
        " refused=True, boundary_layer='terms', boundary_reason='问题超出劳动用工范围')\n"
        "_render_task(task, repo)\n",
    )
    assert not at.exception
    texts = _visible_text(at)
    assert "越界拒答（判定层：terms）" in texts
    assert "问题超出劳动用工范围" in texts
    assert "12348" in texts
    for marker in ("查询路径与判定来源", "引用条文", "Token", "任务 T-REFUSE"):
        assert marker not in texts


# ---------------- 覆盖明细分层（口径守卫） ----------------
def test_split_verdict_details_moves_only_process_rows() -> None:
    """只把五类过程行分到第三层，结论计数与限制提示留在第一层（不改原文）。"""
    details = [
        "规则 17/20 可判定",
        "条款审查未运行",
        "条款审查发现 2 条明确冲突或显著失衡",
        "1 条条款待人工确认",
        "3 条待确认",
        "1 条规则因缺少事实无法判定",
        "必备条款 6/8 已写",
        "2 类需人工确认",
        "合同正文较短，结论可能不完整",
    ]
    first, process = split_verdict_details(details)
    assert process == [
        "规则 17/20 可判定",
        "条款审查未运行",
        "1 条规则因缺少事实无法判定",
        "必备条款 6/8 已写",
    ]
    assert first == [
        "条款审查发现 2 条明确冲突或显著失衡",
        "1 条条款待人工确认",
        "3 条待确认",
        "2 类需人工确认",
        "合同正文较短，结论可能不完整",
    ]
    assert set(first) | set(process) == set(details)  # 不丢行、不改写


def test_settings_copy_is_exact() -> None:
    """设置文案照规格：复选框文字与帮助句一字不差。"""
    assert SHOW_QUERY_PATH_KEY == "ui.show_query_path"
    assert SHOW_QUERY_PATH_LABEL == "显示查询路径与判定来源"
    assert SHOW_QUERY_PATH_HELP == "显示耗时、覆盖率、判定来源、补问与运行详情；不影响核对结论。"
