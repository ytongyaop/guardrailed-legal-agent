"""P2-1：法条全文注入（`truncated` 标记）+ `quote_statute` 回声核验（第 1 / 3 道加严）。

全部离线：假 Generator + 假法条库，不读语料、不发请求、不花钱。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.clauses import ClauseCandidate
from mla.domain.models import CheckTask, ClauseFinding, StatuteEntry, TaskMode
from mla.services.clause_review import STATUTE_FULL_MAX, _payload, _verify, review_clauses

CONTRACT = "甲方有权单方调整乙方工作岗位，乙方不得拒绝。\n乙方月综合薪资 8000 元。\n"
TRANSFER_SPAN = "甲方有权单方调整乙方工作岗位，乙方不得拒绝"

# 劳动合同法第三十五条原文（保留句内换行，用来验证"空白差异不算抄错"）
STATUTE_35 = (
    "用人单位与劳动者协商一致，可以变更劳动合同约定的内容。\n"
    "变更劳动合同，应当采用书面形式。\n"
    "变更后的劳动合同文本由用人单位和劳动者各执一份。"
)
# 真实子串：逐字出现在 STATUTE_35 里
QUOTE_TRUE = "用人单位与劳动者协商一致，可以变更劳动合同约定的内容"
# 形似但不存在：在原文里插入"随时"，归一化后不是连续子串（同义改写必须被拦住）
QUOTE_FAKE = "用人单位与劳动者协商一致，可以随时变更劳动合同约定的内容"

LIBRARY = {
    "LCL-35": StatuteEntry(
        statute_id="LCL-35",
        law_name="中华人民共和国劳动合同法",
        article_no="第三十五条",
        text=STATUTE_35,
        issuing_body="全国人民代表大会常务委员会",
    )
}


class StubLLM:
    """假模型：返回预置结果，并记录每次调用的 payload 与 system。"""

    available = True

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []
        self.systems: list[str] = []

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.systems.append(system)
        self.payloads.append(json.loads(user))
        return self.responses[min(len(self.payloads) - 1, len(self.responses) - 1)]

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def _candidate() -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=1,
        span=TRANSFER_SPAN,
        trigger_id="TR-UNILATERAL-TRANSFER",
        name="单方强制调岗",
        candidate_statute_ids=("LCL-35",),
        severity_hint="A",
    )


def _finding(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "idx": 0,
        "grade": "A",
        "reason": "该约定与第三十五条关于协商一致变更的规定不符",
        "statute_ids": ["LCL-35"],
        "evidence": TRANSFER_SPAN,
        "need_facts": [],
    }
    row.update(over)
    return row


# ---------- 第 3 道：quote_statute 回声核验 ----------


def test_true_statute_substring_kept() -> None:
    """逐字抄的原文 → 通过核验，等级 A 与 quote_statute 都保留。"""
    llm = StubLLM({"findings": [_finding(quote_statute=QUOTE_TRUE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "A"
    assert got[0].quote_statute == QUOTE_TRUE
    assert not any("第 3 道核验" in e for e in errors)


def test_fabricated_statute_text_cleared_and_downgraded() -> None:
    """形似但原文里不存在的 quote → 清空该引用（条号保留），A 级按第 1 道降 B。"""
    llm = StubLLM({"findings": [_finding(quote_statute=QUOTE_FAKE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1, "抄错原文只清引用，不丢整条结论"
    assert got[0].quote_statute == ""
    assert got[0].statute_ids == ("LCL-35",), "第 3 道只清 quote_statute，条号仍保留"
    assert got[0].grade == "B", "A 级失去三件套之一 → 降 B（不是降 C、也不是丢弃）"
    assert any("第 3 道核验" in e and "已清空该引用" in e for e in errors)
    assert any("第 1 道核验" in e and "已降为 B" in e for e in errors)


def test_whitespace_difference_still_passes() -> None:
    """只差空白（空格 / 换行）的合法子串 → 归一化后通过，保留模型抄的原文形态。"""
    quote = "用人单位与劳动者协商一致，\n  可以变更劳动合同约定的内容"
    llm = StubLLM({"findings": [_finding(quote_statute=quote)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "A"
    assert got[0].quote_statute == quote
    assert not any("已清空该引用" in e for e in errors)


def test_quote_without_statute_ids_cleared() -> None:
    """C 级给了 quote 但没给条号 → 无法归因，清空并留痕（结论本身保留）。"""
    llm = StubLLM({"findings": [_finding(grade="C", statute_ids=[], quote_statute=QUOTE_TRUE)]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert len(got) == 1
    assert got[0].grade == "C"
    assert got[0].quote_statute == ""
    assert any("quote_statute 无对应法条" in e for e in errors)


# ---------- 第 1 道：A 级三件套 ----------


def test_grade_a_without_quote_dropped_by_l3_2() -> None:
    """P2-6（003 第 7.2 节第 6 项）起 A 级缺 quote_statute 属 L3.2 形态失败 → 丢弃。

    L3.1 第 1 道的"降为 B"口径未变（`_verify` 未改），但它现在只对已过 L3.2 的条目生效：
    直接调用 `_verify` 仍是降 B，故这里两种口径都断言。"抄错原文"（quote 非空但不是原文）
    不受影响，仍走第 3 道清空 + 降 B（见上一个用例）。
    """
    llm = StubLLM({"findings": [_finding()]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("L3.2 形态校验未过" in e and "quote_statute" in e for e in errors)

    direct: list[str] = []
    finding = _verify(_finding(), _candidate(), CONTRACT, LIBRARY, errors=direct)
    assert isinstance(finding, ClauseFinding)
    assert finding.grade == "B", "L3.1 口径不变：A 级缺 quote 仍降 B（不是丢弃、也不是降 C）"
    assert any("缺 quote_statute" in e and "已降为 B" in e for e in direct)


def test_grade_b_without_quote_dropped_by_l3_2() -> None:
    """P2-6 起 B 级也要求 quote_statute（7.2 第 6 项：A/B 必填）→ 缺失即丢。

    L3.1 第 1 道不要求 B 级三件套，该口径未变：直接调用 `_verify` 时 B 级缺 quote 仍保持 B。
    """
    llm = StubLLM({"findings": [_finding(grade="B", need_facts=["工资基数"])]})
    errors: list[str] = []
    got = review_clauses(llm, [_candidate()], CONTRACT, errors=errors, statutes=LIBRARY)

    assert got == ()
    assert any("L3.2 形态校验未过" in e and "quote_statute" in e for e in errors)

    direct: list[str] = []
    row = _finding(grade="B", need_facts=["工资基数"])
    finding = _verify(row, _candidate(), CONTRACT, LIBRARY, errors=direct)
    assert isinstance(finding, ClauseFinding)
    assert finding.grade == "B", "L3.1 口径不变：B 级不在第 1 道三件套要求内"
    assert not any("第 1 道核验" in e for e in direct)


def test_grade_a_three_pieces_all_present_stays_a() -> None:
    """三件套齐全（statute_ids + evidence + quote_statute）→ A 级原样保留。"""
    errors: list[str] = []
    finding = _verify(
        _finding(quote_statute=QUOTE_TRUE),
        _candidate(),
        CONTRACT,
        LIBRARY,
        errors=errors,
    )

    assert isinstance(finding, ClauseFinding)
    assert finding.grade == "A"
    assert finding.quote_statute == QUOTE_TRUE
    assert not errors


# ---------- 法条全文注入 ----------


def test_long_statute_payload_marked_truncated() -> None:
    """超长条文（> STATUTE_FULL_MAX）→ payload 截到上限并标 truncated=true。"""
    long_text = "本条规定的义务包括：" + "用人单位应当依法履行相应义务。" * 40
    assert len(long_text) > STATUTE_FULL_MAX
    candidate = ClauseCandidate(
        paragraph=1,
        span=TRANSFER_SPAN,
        trigger_id="TR-LONG",
        name="超长条文候选",
        candidate_statute_ids=("LONG-1",),
        severity_hint="C",
    )
    library = {
        "LONG-1": StatuteEntry(
            statute_id="LONG-1",
            law_name="某法律",
            article_no="第一条",
            text=long_text,
            issuing_body="某机关",
        )
    }
    payload = _payload([candidate], CONTRACT, library, 3)
    statute = payload["candidates"][0]["statutes"][0]

    assert len(statute["text"]) == STATUTE_FULL_MAX
    assert statute["truncated"] is True


def test_normal_statute_payload_is_full_text_not_truncated() -> None:
    """未超上限的条文 → 全文注入（不再截 200 字），truncated=false。"""
    payload = _payload([_candidate()], CONTRACT, LIBRARY, 3)
    statute = payload["candidates"][0]["statutes"][0]

    assert statute["text"] == STATUTE_35, "整条注入，保留换行与第二款"
    assert statute["truncated"] is False


# ---------- 持久化兼容 ----------


def _repo(tmp_path: Any) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "quote.sqlite3"),
    )


def test_quote_statute_persist_roundtrip(tmp_path: Any) -> None:
    """新字段随 `clause_findings` JSON 列落库/读回，一字不差。"""
    repo = _repo(tmp_path)
    finding = ClauseFinding(
        paragraph=1,
        span=TRANSFER_SPAN,
        grade="A",
        reason="该约定与第三十五条不符",
        statute_ids=("LCL-35",),
        trigger_id="TR-UNILATERAL-TRANSFER",
        source="trigger",
        need_facts=(),
        quote_statute=QUOTE_TRUE,
    )
    repo.save_task(
        CheckTask(task_id="T-QUOTE", mode=TaskMode.CHECK, enterprise_id="E1", clause_findings=[finding])
    )

    saved = repo.list_tasks("E1")
    assert saved and saved[0].clause_findings == [finding]
    assert saved[0].clause_findings[0].quote_statute == QUOTE_TRUE


def test_legacy_clause_findings_without_quote_deserialized_as_empty(tmp_path: Any) -> None:
    """旧记录（JSON 里没有 quote_statute 键）→ 读回为空串，不抛 KeyError。"""
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T-OLD", mode=TaskMode.CHECK, enterprise_id="E1"))
    legacy_row = {
        "paragraph": 1,
        "span": TRANSFER_SPAN,
        "grade": "A",
        "reason": "该约定与第三十五条不符",
        "statute_ids": ["LCL-35"],
        "trigger_id": "TR-UNILATERAL-TRANSFER",
        "source": "trigger",
        "need_facts": [],
    }
    conn = sqlite3.connect(str(tmp_path / "quote.sqlite3"))
    try:
        conn.execute(
            "UPDATE tasks SET clause_findings=? WHERE task_id=?",
            (json.dumps([legacy_row], ensure_ascii=False), "T-OLD"),
        )
        conn.commit()
    finally:
        conn.close()

    saved = repo.list_tasks("E1")
    assert saved and len(saved[0].clause_findings) == 1
    assert saved[0].clause_findings[0].quote_statute == ""
