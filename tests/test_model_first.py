"""模型主导路径（规格 003 第 13 章 P1-3）：关闭双写，`items` 只由共识层产出。

两条路径必须各自自洽，且可一键回滚：

- 旧路径（`MLA_ALLOW_NO_LLM=1` 的离线场景，或显式 `MLA_MODEL_FIRST=off`）：
  `items` = 规则条目，`verdict.risk_count` = 规则条目 + 条款审查 A/B（现状不动）；
- 新路径（有模型 + `MLA_MODEL_FIRST=on`，默认）：`items` = 共识层结果
  （模型结论 + `source=rule_check` 的数值复核条目），`verdict.risk_count` 只取 `items`。

假模型只回条款判定（1 条与某条数值校验同条号、1 条无关），其余调用不给结论；
数据库一律落 `tmp_path`，不碰仓库里的 `mla.sqlite3`。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path
from mla.domain.models import CheckTask, Role, TaskMode, User
from mla.domain.verdict import summarize_verdict
from mla.services.consensus import NOTE_CONSISTENT, NOTE_NO_CHECK, SOURCE_RULE_CHECK
from mla.services.tasks import model_first_enabled, run_check

pytestmark = pytest.mark.mock_llm

NO_LLM = LLMConfig(api_key="")
# 固定事实与 `tests/test_rule_check_dualwrite.py` 同一口径：结果只随合同正文变化。
BASE_FACTS = {"employer_type": "有限责任公司", "written_contract": "是", "term_years": "3"}

# 旧路径（无模型、无条款审查）实测的规则条目：顺序与规则表一致。
# 规格 003 P1-4 瘦身后少一条（`R-WRITTEN-1M` 下线：签约时限是行为义务，合同文本里抽不到）。
LEGACY_ITEMS = [
    "R-PROBATION-6M",
    "R-PROBATION-WAGE-80",
    "R-NONCOMPETE-TERM-2Y",
    "R-OT-MONTH-36H",
]

# 旧路径 + 有模型：试用期工资规则让位给同域的 TR-PROBATION-WAGE-BASE 结论，条目少一条。
LEGACY_ITEMS_WITH_MODEL = [
    "R-PROBATION-6M",
    "R-NONCOMPETE-TERM-2Y",
    "R-OT-MONTH-36H",
]

# 新路径实测（同一条款结论下）：2 条模型结论 + 2 条程序追加的数值复核条目。
NEW_PATH_ITEM_IDS = [
    "CF-4-TR-PROBATION-TERM-EXCEED",
    "CF-4-TR-PROBATION-WAGE-BASE",
    "RC-R-NONCOMPETE-TERM-2Y",
    "RC-R-OT-MONTH-36H",
]


class ConsensusLLM:
    """假模型：条款判定固定给两条 A 级结论（一条与 R-PROBATION-6M 同条号，一条无规则对应）。

    P2-1 起 A 级须带 quote_statute 三件套（否则第 1 道降 B），这里抄的是真实库内条文的连续子串。
    """

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if "条款风险审查" in system:
            payload = json.loads(user)
            findings: list[dict[str, Any]] = []
            for idx, candidate in enumerate(payload.get("candidates") or []):
                trigger = candidate.get("trigger_id")
                if trigger == "TR-PROBATION-TERM-EXCEED":
                    findings.append(
                        {
                            "idx": idx,
                            "grade": "A",
                            "reason": "该约定与第十九条关于试用期期限的规定不符",
                            "statute_ids": ["LCL-19"],
                            "quote_statute": "劳动合同期限三个月以上不满一年的，试用期不得超过一个月",
                            "evidence": candidate["span"],
                            "need_facts": [],
                        }
                    )
                elif trigger == "TR-PROBATION-WAGE-BASE":
                    findings.append(
                        {
                            "idx": idx,
                            "grade": "A",
                            "reason": "该约定与第二十条关于试用期工资的规定不符",
                            "statute_ids": ["LCL-20"],
                            "quote_statute": (
                                "劳动者在试用期的工资不得低于本单位相同岗位最低档工资"
                                "或者劳动合同约定工资的百分之八十"
                            ),
                            "evidence": candidate["span"],
                            "need_facts": [],
                        }
                    )
            return {"findings": findings, "extra": []}
        if "合规顾问" in system:
            return {"suggestions": []}
        return {}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def _user() -> User:
    return User(
        user_id="U1",
        username="alice",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )


def _contract() -> str:
    return Path(data_path("evalsets/sample_contract.txt")).read_text(encoding="utf-8")


def _run(*, llm: LLMConfig, repo: SqliteRepository | None = None, save: bool = False):
    return run_check(
        user=_user(),
        contract_text=_contract(),
        facts=dict(BASE_FACTS),
        repo=repo,
        save=save,
        llm=llm,
    )


def _enable_model_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """新路径：放开门禁（有模型）+ 开关 on + 假模型。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.setenv("MLA_MODEL_FIRST", "on")
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: ConsensusLLM())


def _repo(tmp_path: Path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "model-first.sqlite3"))


# ---------- 1. 开关 ----------


def test_flag_defaults_on_and_forced_off_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 on；`MLA_ALLOW_NO_LLM=1` 强制关（离线路径没有模型输入）；显式 off 可回滚。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.delenv("MLA_MODEL_FIRST", raising=False)
    assert model_first_enabled() is True

    monkeypatch.setenv("MLA_MODEL_FIRST", "off")
    assert model_first_enabled() is False

    monkeypatch.setenv("MLA_MODEL_FIRST", "on")
    monkeypatch.setenv("MLA_ALLOW_NO_LLM", "1")
    assert model_first_enabled() is False


# ---------- 2. 旧路径（默认，无模型）：items 与改动前一致 ----------


def test_old_path_without_model_keeps_rule_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest 的 `MLA_ALLOW_NO_LLM=1` 场景：items / verdict 与双写关闭前逐条一致。"""
    assert model_first_enabled() is False
    task = _run(llm=NO_LLM)
    assert task.model_first is False
    assert [item.item_id for item in task.items] == LEGACY_ITEMS
    assert task.clause_findings == []
    verdict = summarize_verdict(task)
    assert verdict.status == "risk"
    assert verdict.risk_count == len(LEGACY_ITEMS)


# ---------- 3. 旧路径 + 有模型：照旧叠加条款审查 A/B（回滚用） ----------


def test_old_path_with_model_still_merges_clause_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MLA_MODEL_FIRST=off` 完整回旧行为：items 仍只有规则条目，A/B 结论另行叠加。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.setenv("MLA_MODEL_FIRST", "off")
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: ConsensusLLM())
    task = _run(llm=LLMConfig(api_key="test-key"))

    assert task.model_first is False
    assert [item.item_id for item in task.items] == LEGACY_ITEMS_WITH_MODEL
    assert [item.grade for item in task.items] == [""] * len(LEGACY_ITEMS_WITH_MODEL)  # 没有共识层字段
    assert [f.grade for f in task.clause_findings] == ["A", "A"]
    verdict = summarize_verdict(task)
    assert verdict.risk_count == len(LEGACY_ITEMS_WITH_MODEL) + 2 == 5


# ---------- 4. 新路径：items 只由共识层产出 ----------


def test_new_path_items_come_from_consensus(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_model_path(monkeypatch)
    task = _run(llm=LLMConfig(api_key="test-key"))

    assert task.model_first is True
    # clause_findings 原样保留（审计与历史展示用），不删不清
    assert [(f.paragraph, f.grade, f.trigger_id) for f in task.clause_findings] == [
        (4, "A", "TR-PROBATION-TERM-EXCEED"),
        (4, "A", "TR-PROBATION-WAGE-BASE"),
    ]
    # items = 模型结论 + 程序追加的数值复核条目，顺序 A→C 固定
    assert [item.item_id for item in task.items] == NEW_PATH_ITEM_IDS

    model_items = [item for item in task.items if item.source == "trigger"]
    assert [(i.clause_ref, i.grade, i.statute_ids) for i in model_items] == [
        ("4", "A", ("LCL-19",)),
        ("4", "A", ("LCL-20",)),
    ]
    assert [i.note for i in model_items] == [NOTE_CONSISTENT, NOTE_NO_CHECK]

    review_items = [item for item in task.items if item.source == SOURCE_RULE_CHECK]
    assert [i.rule_id for i in review_items] == [
        "R-NONCOMPETE-TERM-2Y",
        "R-OT-MONTH-36H",
    ]
    assert all(i.grade == "C" for i in review_items)
    # 规则不再单独出条目：除 rule_check 复核条目外，没有"只有 rule_id"的条目
    assert [i for i in task.items if i.rule_id and i.source != SOURCE_RULE_CHECK] == []
    assert [i for i in task.items if not i.finding] == []
    # 同域让位继续生效：试用期工资规则不因 mismatch 再补一条复核条目
    assert [i for i in task.items if i.rule_id == "R-PROBATION-WAGE-80"] == []


# ---------- 5. verdict 只数“冲突”，C 级单列“需人工复核” ----------


def test_new_path_verdict_does_not_double_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """新路径：A/B 计冲突、C（含程序复核条目）只计“需人工复核”，两级不混用、不叠加条款 A/B。"""
    _enable_model_path(monkeypatch)
    task = _run(llm=LLMConfig(api_key="test-key"))
    verdict = summarize_verdict(task)

    clause_risk = sum(1 for f in task.clause_findings if f.grade in ("A", "B"))
    assert clause_risk == 2  # 确实存在会被旧算法再叠加一遍的 A 级结论
    assert [i.grade for i in task.items] == ["A", "A", "C", "C"]
    assert verdict.risk_count == 2
    assert verdict.review_count == 2
    assert verdict.status == "risk"
    assert verdict.headline == "发现 2 条与法条存在冲突的事项"
    assert "4 条" not in verdict.headline  # C 级不能说成“冲突”
    assert "2 条需人工复核" in verdict.details
    # 同一 statute 只算一次：LCL-19 在 items 里只出现一次，不再因为条款 A 级重复计入
    item_statutes = [sid for item in task.items for sid in item.statute_ids]
    assert item_statutes.count("LCL-19") == 1


# ---------- 6. 两路径输出确实不同（同一输入、同一假模型） ----------


def test_two_paths_differ_and_are_self_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: ConsensusLLM())
    llm = LLMConfig(api_key="test-key")

    monkeypatch.setenv("MLA_MODEL_FIRST", "off")
    old = _run(llm=llm)
    monkeypatch.setenv("MLA_MODEL_FIRST", "on")
    new = _run(llm=llm)

    assert old.clause_findings == new.clause_findings  # 模型结论相同
    assert [i.item_id for i in old.items] != [i.item_id for i in new.items]
    assert old.model_first is False and new.model_first is True
    old_verdict = summarize_verdict(old)
    new_verdict = summarize_verdict(new)
    assert old_verdict.risk_count == 5  # 旧路径照旧：3 条规则条目 + 2 条条款 A
    assert (old_verdict.review_count, old_verdict.status) == (0, "risk")
    assert (new_verdict.risk_count, new_verdict.review_count, new_verdict.status) == (2, 2, "risk")


# ---------- 7. 落库与回读 ----------


def test_new_path_fields_survive_sqlite_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_model_path(monkeypatch)
    repo = _repo(tmp_path)
    task = _run(llm=LLMConfig(api_key="test-key"), repo=repo, save=True)
    assert task.model_first is True

    saved = repo.list_tasks("E1")[0]
    assert saved.model_first is True
    assert [item.item_id for item in saved.items] == NEW_PATH_ITEM_IDS
    first = saved.items[0]
    assert (first.grade, first.source, first.statute_ids, first.note) == (
        "A",
        "trigger",
        ("LCL-19",),
        NOTE_CONSISTENT,
    )
    review = saved.items[2]
    assert (review.grade, review.source, review.rule_id) == ("C", SOURCE_RULE_CHECK, "R-NONCOMPETE-TERM-2Y")
    # 任务上仍保留条款结论与规则校验信号（审计与历史展示用）
    assert saved.clause_findings == task.clause_findings
    assert len(saved.rule_checks) == len(task.rule_checks)


def test_legacy_record_without_new_columns_reads_as_old_path(tmp_path: Path) -> None:
    """旧库（没有 model_first 列、条目 JSON 没有新键）：迁移补列后按旧路径读，不臆造等级。"""
    db = tmp_path / "legacy-model-first.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY, enterprise_id TEXT, mode TEXT, variant TEXT,
          facts TEXT, items TEXT, suggestions TEXT, verify_ok INTEGER, degraded INTEGER,
          latency_s REAL, token_in INTEGER, token_out INTEGER, created_at TEXT
        );
        """
    )
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
    repo = SqliteRepository(paths=paths, db_path=str(db))
    old = repo.list_tasks("E1")[0]
    assert old.model_first is False
    assert (old.items[0].grade, old.items[0].source, old.items[0].note, old.items[0].statute_ids) == (
        "",
        "",
        "",
        (),
    )

    # 同一库里写新路径任务：新列可写可读，旧记录不受影响
    repo.save_task(
        CheckTask(
            task_id="T-NEW",
            mode=TaskMode.CHECK,
            enterprise_id="E1",
            model_first=True,
            created_at=datetime(2026, 11, 16, 10, 0),
        )
    )
    assert repo.list_tasks("E1")[0].model_first is True


# ---------- 8. 展示层不二次合并（界面 + Word 报告） ----------

# 直接渲染一个已组装好的新路径任务：同一条模型结论只能出现一次（否则就是二次合并）。
DISPLAY_SCRIPT = """
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import CheckItem, CheckTask, ClauseFinding, ItemKind, Severity, TaskMode
from mla.ui.app import _render_task

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
task = CheckTask(
    task_id="T-DISPLAY",
    mode=TaskMode.CHECK,
    enterprise_id="E1",
    model_first=MODEL_FIRST,
    clause_review=True,
    clause_findings=[
        ClauseFinding(
            paragraph=4,
            span="试用期为十二个月",
            grade="A",
            reason="该约定与第十九条关于试用期期限的规定不符",
            statute_ids=("LCL-19",),
            trigger_id="TR-PROBATION-TERM-EXCEED",
        )
    ],
    items=[
        CheckItem(
            item_id="CF-4-TR-PROBATION-TERM-EXCEED",
            kind=ItemKind.RISK,
            clause_ref="4",
            finding="该约定与第十九条关于试用期期限的规定不符",
            source_id="LCL-19",
            evidence_text="试用期为十二个月",
            severity=Severity.HARD,
            grade="A",
            source="trigger",
            note="数值校验一致",
            statute_ids=("LCL-19",),
        ),
        CheckItem(
            item_id="RC-R-OT-MONTH-36H",
            kind=ItemKind.UNKNOWN,
            clause_ref="",
            finding="数值与第 41 条不一致，需人工复核",
            source_id="LL-41",
            severity=Severity.SOFT,
            rule_id="R-OT-MONTH-36H",
            grade="C",
            source="rule_check",
            note="数值与第 41 条不一致，需人工复核",
            statute_ids=("LL-41",),
        ),
    ],
)
_render_task(task, repo)
"""


def _render_app(tmp_path: Path, *, model_first: bool):
    from streamlit.testing.v1 import AppTest

    script = DISPLAY_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "display.sqlite3"))).replace(
        "MODEL_FIRST", "True" if model_first else "False"
    )
    return AppTest.from_string(script, default_timeout=60).run()


def _markdown_text(at) -> str:
    return "\n".join(str(getattr(block, "value", block)) for block in at.markdown)


def test_new_path_renders_each_conclusion_once(tmp_path: Path) -> None:
    """新路径：模型结论只在 items 里渲染一次；数值复核条目带来源徽标；不再合并 clause_findings。"""
    at = _render_app(tmp_path, model_first=True)
    assert not at.exception
    text = _markdown_text(at)
    reason = "该约定与第十九条关于试用期期限的规定不符"
    assert text.count(reason) == 1  # 二次合并会让它出现两次
    assert "数值复核·需人工" in text
    assert "数值与第 41 条不一致，需人工复核" in text


def test_old_path_still_merges_clause_findings(tmp_path: Path) -> None:
    """旧路径口径不变：items 只有规则条目，条款结论另行合并列出。"""
    at = _render_app(tmp_path, model_first=False)
    assert not at.exception
    text = _markdown_text(at)
    assert "规则判定·确定性" in text  # 旧路径仍按规则条目渲染 items
    assert text.count("数值与第 41 条不一致，需人工复核") == 1


def test_report_new_path_lists_items_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Word 报告同理：新路径逐条排版 items，同一结论在正文只出现一次。"""
    import io

    from docx import Document

    from mla.services.report import build_report_docx

    _enable_model_path(monkeypatch)
    repo = _repo(tmp_path)
    task = _run(llm=LLMConfig(api_key="test-key"), repo=repo)
    data = build_report_docx(task, repo)
    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    assert text.count("该约定与第十九条关于试用期期限的规定不符") == 1
    assert "数值复核（需人工确认）" in text
    assert "规则判定" not in text  # 新路径不应再出现规则通道的条目行
