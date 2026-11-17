"""P2-7 / 规格 003 第 4.5 节：L4 返工（把机械失败回喂模型）。

全部离线：假 Generator + 假法条库，不联网、不花钱；每个用例都断言"调了几次、修好了几条"。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path, load_config
from mla.domain.clauses import ClauseCandidate
from mla.domain.models import CheckTask, Role, StatuteEntry, TaskMode, User
from mla.services.clause_review import REVIEW_SYSTEM, RetryStats, review_clauses
from mla.services.tasks import run_check

# 三行合同：每条候选的段落与证据都落在自己那一行里（第 2 道核验要求同段）。
LINES = [
    "甲方有权单方调整乙方工作岗位，乙方不得拒绝。",
    "乙方月综合薪资 8000 元，其中基本工资 2200 元。",
    "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴。",
]
CONTRACT = "\n".join(LINES) + "\n"
SPANS = [
    "甲方有权单方调整乙方工作岗位，乙方不得拒绝",
    "乙方月综合薪资 8000 元，其中基本工资 2200 元",
    "乙方自愿放弃缴纳社会保险，甲方折算为现金补贴",
]
TRIGGERS = ("TR-UNILATERAL-TRANSFER", "TR-WAGE-UNDERPAID", "TR-SI-WAIVER")
# 假库条文的真实子串：A 级三件套要过第 3 道回声核验才靠得住。
QUOTE_35 = "用人单位与劳动者协商一致，可以变更劳动合同约定的内容"


def _statute(sid: str, text: str = QUOTE_35 + "。") -> StatuteEntry:
    return StatuteEntry(
        statute_id=sid,
        law_name="中华人民共和国劳动合同法",
        article_no="第三十五条",
        text=text,
        issuing_body="全国人民代表大会常务委员会",
    )


LIBRARY = {sid: _statute(sid) for sid in ("LCL-35", "LCL-31")}


def _candidate(paragraph: int = 1, trigger_id: str = TRIGGERS[0]) -> ClauseCandidate:
    return ClauseCandidate(
        paragraph=paragraph,
        span=SPANS[paragraph - 1],
        trigger_id=trigger_id,
        name="测试候选",
        candidate_statute_ids=("LCL-35",),
        severity_hint="A",
    )


def _finding(idx: int = 0, **over: Any) -> dict[str, Any]:
    """一条形态齐全的模型输出（A 级三件套齐备）。"""
    row: dict[str, Any] = {
        "idx": idx,
        "grade": "A",
        "reason": "该约定与第三十五条关于协商一致变更的规定不符",
        "statute_ids": ["LCL-35"],
        "quote_statute": QUOTE_35,
        "evidence": SPANS[idx] if 0 <= idx < len(SPANS) else SPANS[0],
        "need_facts": [],
    }
    row.update(over)
    return row


class ScriptedLLM:
    """按调用顺序返回预置结果的假模型（Generator 协议）；记录每次送进去的 user 串。"""

    available = True
    usage_in = 0
    usage_out = 0

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.users: list[str] = []
        self.systems: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.users)

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.systems.append(system)
        self.users.append(user)
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def _run(
    llm: ScriptedLLM,
    candidates: list[ClauseCandidate],
    *,
    batch_size: int = 5,
    retry: int | None = None,
) -> tuple[tuple[Any, ...], list[str], RetryStats]:
    errors: list[str] = []
    stats = RetryStats()
    findings = review_clauses(
        llm,
        candidates,
        CONTRACT,
        batch_size=batch_size,
        errors=errors,
        statutes=LIBRARY,
        retry=retry,
        retry_stats=stats,
    )
    return findings, errors, stats


# ---------- V13：可返工失败 → 返工后采纳 ----------


def test_missing_quote_fixed_on_first_retry_is_accepted() -> None:
    """第 1 次缺 quote、第 2 次补上 → 采纳，`retry_calls=1`、`retry_passed=1`。"""
    broken = _finding(quote_statute="")
    fixed = _finding()
    llm = ScriptedLLM({"findings": [broken]}, {"findings": [fixed]})
    findings, errors, stats = _run(llm, [_candidate()])

    assert [f.grade for f in findings] == ["A"]
    assert findings[0].quote_statute == QUOTE_35
    assert (stats.calls, stats.items, stats.passed) == (1, 1, 1)
    assert any("第 1 批返工第 1 次：自 1 条失败中修正 1 条" in e for e in errors)
    assert any("check=quote_statute_mismatch" in e for e in errors), "成功诊断要带修好的 check 列表"
    print(
        f"[retry] 第 1 次缺 quote：模型调用 {llm.calls} 次，retry_calls={stats.calls}，"
        f"retry_passed={stats.passed}"
    )
    # 返工请求重发整批 payload（不是只发失败条），再追加上一轮输出与失败清单
    assert llm.users[1].startswith(llm.users[0])
    envelope = json.loads(llm.users[1].split("user（返工清单）：", 1)[1])
    assert [entry["check"] for entry in envelope["failed"]] == ["quote_statute_mismatch"]
    assert envelope["round"] == 1
    # system 不变（只多一句返工口径）
    assert llm.systems[0] == llm.systems[1]
    assert "若收到返工清单，只修正清单里列出的问题，不要改动其他结论" in REVIEW_SYSTEM


# ---------- V14：返工 2 次仍失败 → 丢弃 + 诊断 ----------


def test_still_missing_quote_after_two_retries_is_dropped() -> None:
    """三次都缺 quote → 丢弃；`retry_calls=2`、`retry_passed=0`，诊断含"返工 2 次仍失败"。"""
    broken = {"findings": [_finding(quote_statute="")]}
    llm = ScriptedLLM(broken, broken, broken)
    findings, errors, stats = _run(llm, [_candidate()])

    assert findings == ()
    assert (stats.calls, stats.items, stats.passed) == (2, 1, 0)
    assert any("返工 2 次仍失败" in e and "quote_statute_mismatch" in e for e in errors)
    assert any("第 1 批返工第 2 次：自 1 条失败中修正 0 条" in e for e in errors)
    print(
        f"[retry] 两次都缺 quote：模型调用 {llm.calls} 次，retry_calls={stats.calls}，"
        f"retry_passed={stats.passed}，结论 {len(findings)} 条"
    )


# ---------- V15：不可返工失败 → 不触发返工 ----------


def test_unlocatable_evidence_does_not_trigger_retry() -> None:
    """证据在合同原文定位不到（编造原文）→ 不返工，1 次调用后直接丢弃。"""
    llm = ScriptedLLM({"findings": [_finding(evidence="乙方同意无限期加班且不主张任何报酬")]})
    findings, errors, stats = _run(llm, [_candidate()])

    assert findings == ()
    assert llm.calls == 1, "不可返工失败不该多发调用"
    assert (stats.calls, stats.items, stats.passed) == (0, 0, 0)
    assert any("无法在合同原文定位" in e for e in errors)
    assert not any("返工" in e for e in errors)
    print(f"[retry] 证据定位不到：模型调用 {llm.calls} 次，retry_calls={stats.calls}")


# ---------- 开关：MLA_CLAUSE_RETRY=0 等价 P2-6 ----------


class _OutOfLibraryLLM(ScriptedLLM):
    """按 payload 里的候选回一条"条号不在库"的结论（证据取候选 span），用于多批计数。"""

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.systems.append(system)
        self.users.append(user)
        payload = json.loads(user)
        rows = [
            {
                "idx": idx,
                "grade": "A",
                "reason": "该约定与第三十五条关于协商一致变更的规定不符",
                "statute_ids": ["LCL-99"],
                "quote_statute": QUOTE_35,
                "evidence": candidate["span"],
                "need_facts": [],
            }
            for idx, candidate in enumerate(payload.get("candidates") or [])
        ]
        return {"findings": rows, "extra": []}


def test_retry_switch_off_sends_no_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MLA_CLAUSE_RETRY=0`：4 条候选 / 每批 2 条 → 2 批共 2 次调用，无额外调用、无返工诊断。"""
    monkeypatch.setenv("MLA_CLAUSE_RETRY", "0")
    assert load_config().clause_review.retry == 0
    llm = _OutOfLibraryLLM()
    candidates = [
        _candidate(paragraph=1, trigger_id=TRIGGERS[0]),
        _candidate(paragraph=1, trigger_id=TRIGGERS[1]),
        _candidate(paragraph=2, trigger_id=TRIGGERS[0]),
        _candidate(paragraph=2, trigger_id=TRIGGERS[1]),
    ]
    findings, errors, stats = _run(llm, candidates, batch_size=2)

    assert findings == ()
    assert llm.calls == 2, "关闭返工后调用次数必须等于批数"
    assert (stats.calls, stats.items, stats.passed) == (0, 0, 0)
    assert not any("返工" in e for e in errors)
    assert any("不存在" in e for e in errors)
    print(f"[retry] MLA_CLAUSE_RETRY=0：批数 2，模型调用 {llm.calls} 次")


# ---------- 失败清单只列可返工项 ----------


def test_failed_list_contains_only_retryable_items() -> None:
    """同批一条越界引用（可返工）+ 一条证据定位不到（不可返工）→ 清单只列前者。"""
    rows = [
        _finding(statute_ids=["LCL-31"]),  # 库内但越出候选闭集
        _finding(evidence="合同里根本没有的一句话"),
    ]
    llm = ScriptedLLM({"findings": rows}, {"findings": [_finding()]})
    findings, errors, stats = _run(llm, [_candidate()])

    assert len(llm.users) == 2, "有可返工项就要重发一次"
    envelope = json.loads(llm.users[1].split("user（返工清单）：", 1)[1])
    checks = [entry["check"] for entry in envelope["failed"]]
    assert checks == ["citation_closed_set"]
    assert "evidence_not_found" not in llm.users[1]
    assert len(envelope["original"]) == 1, "清单里的 original 只含待返工的条目"
    assert [f.grade for f in findings] == ["A"]
    assert findings[0].statute_ids == ("LCL-35",), "采纳的是返工后（闭集内）的那次结果"
    assert (stats.calls, stats.items, stats.passed) == (1, 1, 1)
    assert any("check=citation_closed_set" in e for e in errors)
    assert any("引用越出候选闭集" in e for e in errors)
    assert any("无法在合同原文定位" in e for e in errors)


# ---------- 一批 3 条失败、修好 2 条 ----------


def test_three_failures_two_fixed_counts_are_correct() -> None:
    """首轮 3 条越界、返工第 1 轮修好 2 条、第 2 轮仍失败 → items=3 / passed=2 / calls=2。"""
    candidates = [_candidate(paragraph=i + 1, trigger_id=TRIGGERS[i]) for i in range(3)]
    broken = [
        _finding(idx=0, statute_ids=["LCL-31"]),
        _finding(idx=1, statute_ids=["LCL-31"]),
        _finding(idx=2, statute_ids=["LCL-31"]),
    ]
    partly = [
        _finding(idx=0),
        _finding(idx=1),
        _finding(idx=2, statute_ids=["LCL-31"]),
    ]
    still = [_finding(idx=2, statute_ids=["LCL-31"])]
    llm = ScriptedLLM({"findings": broken}, {"findings": partly}, {"findings": still})
    findings, errors, stats = _run(llm, candidates)

    assert sorted(f.paragraph for f in findings) == [1, 2]
    assert (stats.calls, stats.items, stats.passed) == (2, 3, 2)
    assert any("第 1 批返工第 1 次：自 3 条失败中修正 2 条" in e for e in errors)
    assert any("第 1 批返工第 2 次：自 1 条失败中修正 0 条" in e for e in errors)
    assert any("返工 2 次仍失败" in e for e in errors)
    print(
        f"[retry] 3 条失败返工：调用 {llm.calls} 次，items={stats.items}，"
        f"passed={stats.passed}，保留结论 {len(findings)} 条"
    )


# ---------- 成本字段落库 ----------


class _ClauseScriptedLLM(ScriptedLLM):
    """只对条款判定（payload 里有 candidates）按脚本应答；其他调用回空对象。"""

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if '"candidates"' not in user:
            return {}
        return super().generate(system, user, schema)


def test_retry_cost_fields_persist_roundtrip(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run` 汇总写入三个成本字段，落库后读回一致。"""
    repo = SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "retry.sqlite3"),
    )
    fake = _ClauseScriptedLLM(
        {"findings": [_finding(quote_statute="")]},
        {"findings": [_finding()]},
    )
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: fake)
    user = User(
        user_id="U1",
        username="alice",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )
    task = run_check(
        user=user,
        contract_text=LINES[0] + "\n",
        llm=LLMConfig(api_key="fake-key"),
        enterprise_id="E1",
        repo=repo,
        save=True,
    )

    assert (task.clause_retry_calls, task.clause_retry_items, task.clause_retry_passed) == (1, 1, 1)
    saved = repo.list_tasks("E1")
    assert saved, "任务应已落库"
    assert (saved[0].clause_retry_calls, saved[0].clause_retry_items, saved[0].clause_retry_passed) == (
        1,
        1,
        1,
    )
    assert any("返工第 1 次" in e for e in saved[0].errors)


def test_retry_cost_fields_default_to_zero(tmp_path: Any) -> None:
    """旧库 / 未返工的任务读回 0（新增列有默认值，不是 NULL）。"""
    repo = SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "retry-default.sqlite3"),
    )
    repo.save_task(CheckTask(task_id="T-ZERO", mode=TaskMode.CHECK, enterprise_id="E1"))
    saved = repo.list_tasks("E1")
    assert saved
    assert (saved[0].clause_retry_calls, saved[0].clause_retry_items, saved[0].clause_retry_passed) == (
        0,
        0,
        0,
    )
