"""模型主导路径的端到端回归（规格 003 第 9.2 节 B 套 / 第 13 章 P4-2）。

一条 `run_check` 走完整链路：正则抽取 → 规则校验 → 条款审查（假模型）→ 共识层 → 分级 → 落库。
断言只针对**对外契约**，不复制中间实现细节：

- 条目只来自由模型结论 + 程序追加的复核条目（没有裸规则条目）；
- 每条结论都带 `element_id` / `impact` / `certainty`，且与 `domain/grading.py` 的纯函数一致；
- 展示顺序是"影响度 → 确定性 → 段落号"（`domain/grading.sort_items`，界面与 Word 报告共用）；
- `verdict` 的冲突条数 / 复核条数只由 `items` 算出，不叠加条款 A/B；
- L4 返工：可修正失败回喂一次即修好，成本记在任务字段上；
- 落库往返后展示字段不丢。

模型应答按 payload 里的 `trigger_id` 生成（不是按调用序号硬编码），所以候选顺序变了也不会假绿；
同时断言"送进模型的 payload"（只发候选 ±2 行、要素骨架只发本批命中的那几个），隐私口径因此被覆盖。

全部离线：conftest 已把抽取锁在正则档并屏蔽真实 Key，本文件再把假模型注入服务装配层。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from helpers import FakeGenerator

from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path
from mla.domain.elements import IMPACTS
from mla.domain.grading import (
    CERTAINTY_BODY,
    CERTAINTY_FACTS,
    certainty_for,
    display_of,
    impact_for,
    sort_items,
)
from mla.domain.models import Role, User
from mla.domain.verdict import summarize_verdict
from mla.services.tasks import run_check

pytestmark = pytest.mark.mock_llm

# 条款审查的 system 里固定有这句话（`services/clause_review.REVIEW_SYSTEM`）：用它区分"审查调用"与
# "处理建议调用"，避免按调用序号猜。
REVIEW_MARK = "条款风险审查"

# 一份 9 行的对抗合同：候选只落在第 3–5 段，第 1–2 段与第 8–9 段离候选 >2 行，
# 因此"只发候选 ±2 行"这条能被测到（第 9 段的手机号是哨兵）。
LINES = [
    "甲方（用人单位）：某某科技有限公司，法定代表人：张某",
    "乙方（劳动者）：李某",
    "第一条 试用期为十二个月。",
    "第二条 乙方不得就任何劳动争议向劳动争议仲裁委员会申请仲裁。",
    "第三条 劳动合同仅由用人单位持有，劳动者所持文本无效。",
    "第四条 本合同未尽事宜，由甲方另行书面决定。",
    "第五条 乙方应遵守甲方依法制定的规章制度。",
    "第六条 本合同期满即行终止。",
    "第七条 乙方联系方式：13800000000，住址：某某市某某路 1 号。",
]
CONTRACT = "\n".join(LINES) + "\n"
SENTINEL = "13800000000"

# 固定事实：与 `tests/test_rule_check_dualwrite.py` 同一口径，结论只随合同正文变化。
FACTS = {"employer_type": "有限责任公司", "written_contract": "是", "term_years": "3"}

# 三个候选各自的预置结论：A / B / A（B 带 need_facts，验证"需补事实只挂在条目内"）。
# `quote_statute` 抄的是真实库内条文的连续子串，A 级三件套才能过第 3 道回声核验。
REPLY: dict[str, dict[str, Any]] = {
    "TR-PROBATION-TERM-EXCEED": {
        "grade": "A",
        "reason": "该约定与第十九条关于试用期期限的规定不符",
        "statute_ids": ["LCL-19"],
        "quote_statute": "三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月",
        "need_facts": [],
    },
    "TR-ARBITRATION-WAIVER": {
        "grade": "B",
        "reason": "该约定排除了劳动者申请劳动仲裁的权利",
        "statute_ids": ["LDA-5"],
        "quote_statute": "可以向劳动争议仲裁委员会申请仲裁",
        "need_facts": ["是否已就争议解决方式与管理方另行协商"],
    },
    "TR-CONTRACT-COPY-INVALID": {
        "grade": "A",
        "reason": "该约定使劳动者所持合同文本无效，与文本各执一份的要求不符",
        "statute_ids": ["LCL-16"],
        "quote_statute": "劳动合同文本由用人单位和劳动者各执一份",
        "need_facts": [],
    },
}

# 候选之外的自提结论（自提一律封顶 C）：验证"需补事实"那一层也来自模型通道。
EXTRA = {
    "grade": "C",
    "reason": "未尽事宜的解释权全部留给甲方，劳动者没有协商余地",
    "statute_ids": [],
    "evidence": "第四条 本合同未尽事宜，由甲方另行书面决定。",
    "need_facts": ["未尽事宜是否会涉及报酬、工时等核心条款"],
}


def _responder(*, fail_first_review: bool = False) -> Any:
    """按 payload 里的候选组应答；处理建议调用一律空结果。

    `fail_first_review=True` 时，第一轮把"文本无效"那条的条号写成库外条号（可修正的机械失败），
    用于验证 L4 返工一次即修好。
    """

    state = {"review_calls": 0}

    def respond(system: str, user: str) -> dict[str, Any]:
        if REVIEW_MARK not in system:
            return {"suggestions": []}
        state["review_calls"] += 1
        # 返工请求是"整批 payload + 上一轮输出 + 失败清单"，第一个 JSON 值仍是 payload。
        payload = json.JSONDecoder().raw_decode(user)[0]
        rows: list[dict[str, Any]] = []
        for idx, candidate in enumerate(payload.get("candidates") or []):
            preset = REPLY.get(candidate.get("trigger_id"))
            if preset is None:
                continue
            row = dict(preset, idx=idx, evidence=candidate["span"])
            if (
                fail_first_review
                and state["review_calls"] == 1
                and candidate.get("trigger_id") == "TR-CONTRACT-COPY-INVALID"
            ):
                row["statute_ids"] = ["LCL-999"]
            rows.append(row)
        return {"findings": rows, "extra": [dict(EXTRA)]}

    return respond


def _user() -> User:
    return User(
        user_id="U1",
        username="e2e",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_first_review: bool = False,
    repo: SqliteRepository | None = None,
    save: bool = False,
) -> tuple[Any, FakeGenerator]:
    """走模型主导路径：放开门禁 + 开关 on + 假模型注入装配层（`_deps` 里构造生成器处）。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.setenv("MLA_MODEL_FIRST", "on")
    monkeypatch.delenv("MLA_CLAUSE_RETRY", raising=False)
    fake = FakeGenerator(_responder(fail_first_review=fail_first_review))
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: fake)
    task = run_check(
        user=_user(),
        contract_text=CONTRACT,
        facts=dict(FACTS),
        enterprise_id="E1",
        repo=repo,
        save=save,
        llm=LLMConfig(api_key="fake-key"),
    )
    return task, fake


def _repo(tmp_path: Any) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "model-first-e2e.sqlite3"))


# ---------- 1. 全链路：结论来源 + 展示三件套 + 送进模型的 payload ----------


def test_full_chain_produces_graded_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """抽取 → 规则 → 条款审查 → 共识 → 分级：条目只来自模型通道，且每条都带展示三件套。"""
    task, fake = _run(monkeypatch)

    assert task.model_first is True
    # 模型结论（trigger）+ 自提结论（model_extra）：没有"只有 rule_id"的裸规则条目
    assert {item.source for item in task.items} == {"trigger", "model_extra"}
    assert [item for item in task.items if item.source == "rule_check"] == []
    # 条款结论原样留在任务上（审计与历史展示用）：3 条触发器结论 + 1 条自提（封顶 C）
    assert [f.trigger_id for f in task.clause_findings if f.source == "trigger"] == [
        "TR-PROBATION-TERM-EXCEED",
        "TR-ARBITRATION-WAIVER",
        "TR-CONTRACT-COPY-INVALID",
    ]
    assert [f.grade for f in task.clause_findings if f.source == "model_extra"] == ["C"]

    for item in task.items:
        assert item.element_id, f"{item.item_id} 缺 element_id，展示层无从取影响度"
        assert item.impact in IMPACTS
        # 影响度只认要素、确定性只认等级：两者都必须能由纯函数复算出来
        assert item.impact == impact_for(item.element_id)
        assert item.certainty == certainty_for(item.grade)

    # 段落 → 要素 → 影响度这条链的完整落点（分类与分级同源，改数据会在这条断言上现形）
    assert {(item.clause_ref, item.element_id, item.impact, item.certainty) for item in task.items} == {
        ("3", "EL-3", "高", CERTAINTY_BODY),  # 试用期
        ("4", "EL-12", "中", CERTAINTY_BODY),  # 争议解决（程序义务 → 中）
        ("6", "EL-13", "中", CERTAINTY_FACTS),  # 自提的兜底要素 + C 级
        ("5", "EL-1", "低", CERTAINTY_BODY),  # 文本持有（形式瑕疵 → 低）
    }

    # 送进模型的 payload：只发候选 ±2 行，要素骨架只发本批命中的那几个，数值校验信号随包下发
    review_prompts = [prompt for prompt in fake.prompts if REVIEW_MARK in prompt]
    assert review_prompts, "条款审查应当真的发起一次模型调用"
    assert all(SENTINEL not in prompt for prompt in review_prompts), "远离候选的段落不得外发"

    payloads = [payload for payload in fake.payloads if "candidates" in payload]
    assert payloads
    for payload in payloads:
        sent = {int(line.split(".", 1)[0]) for line in payload["contract_paragraphs"]}
        window = {p for c in payload["candidates"] for p in range(c["paragraph"] - 2, c["paragraph"] + 3)}
        assert sent <= window, "外发范围必须是候选 ±2 行"
        assert {row["element"] for row in payload["candidates"]} == {
            element["element_id"] for element in payload["elements"]
        }
        # 数值校验信号（P2-3）与结构化事实同批下发，模型据此判断，不再自己算
        assert payload["rule_checks"], "试用期超限的规则校验应当随候选下发"
        assert payload["fact_sheet"]["probation_months"] == "12"


# ---------- 2. 排序：影响度 → 确定性 → 段落号 ----------


def test_display_order_is_impact_then_certainty(monkeypatch: pytest.MonkeyPatch) -> None:
    """展示顺序是纯函数的输出：高 → 中 → 低；同影响度内正文输出先于需补事实，再按段落号升序。"""
    task, _ = _run(monkeypatch)
    ordered = sort_items(task.items)

    assert [(item.impact, item.certainty) for item in ordered] == [
        ("高", CERTAINTY_BODY),
        ("中", CERTAINTY_BODY),
        ("中", CERTAINTY_FACTS),
        ("低", CERTAINTY_BODY),
    ]
    # 同影响度、同确定性时按段落号升序；排序键整体单调不减（高 > 中 > 低 的口径）
    assert [int(item.clause_ref) for item in ordered if item.impact == "中"] == [4, 6]
    keys = [display_of(item).sort_key for item in ordered]
    assert keys == sorted(keys)


# ---------- 3. verdict 与 items 一致 ----------


def test_verdict_counts_match_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """冲突条数只数 A/B，复核条数只数 C；两级不混用、不叠加条款 A/B、合起来等于条目数。"""
    task, _ = _run(monkeypatch)
    verdict = summarize_verdict(task)

    conflict = sum(1 for item in task.items if item.grade in ("A", "B"))
    pending = sum(1 for item in task.items if item.grade == "C")
    assert (conflict, pending) == (3, 1)
    assert (verdict.risk_count, verdict.review_count) == (conflict, pending)
    assert verdict.risk_count + verdict.review_count == len(task.items)
    assert verdict.status == "risk"
    assert verdict.headline == "发现 3 条与法条存在冲突的事项"
    assert "1 条需人工复核" in verdict.details


# ---------- 4. L4 返工：一次可修正失败 → 回喂一次即修好 ----------


def test_retry_once_then_success_keeps_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """库外条号（可修正）→ 返工 1 次修好：条目保住、成本记在任务字段上、诊断留痕。"""
    task, fake = _run(monkeypatch, fail_first_review=True)

    assert (task.clause_retry_calls, task.clause_retry_items, task.clause_retry_passed) == (1, 1, 1)
    assert fake.calls == 3, "1 次处理建议 + 2 次条款判定（原始 + 返工）"
    kept = [item for item in task.items if item.item_id == "CF-5-TR-CONTRACT-COPY-INVALID"]
    assert len(kept) == 1 and kept[0].statute_ids == ("LCL-16",)
    assert any("返工第 1 次" in line and "citation_in_library" in line for line in task.errors)
    # 返工不改变展示口径：四条结论的要素与影响度与未返工那次一致
    assert [item.impact for item in sort_items(task.items)] == ["高", "中", "中", "低"]


# ---------- 5. 落库往返：展示字段不丢 ----------


def test_display_fields_survive_sqlite_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """落库 → 读回后 `element_id` / `impact` / `certainty` 与展示顺序都不变（历史详情靠它渲染）。"""
    repo = _repo(tmp_path)
    task, _ = _run(monkeypatch, repo=repo, save=True)

    saved = repo.list_tasks("E1")[0]
    assert saved.model_first is True
    assert [
        (item.item_id, item.element_id, item.impact, item.certainty)
        for item in sorted(saved.items, key=lambda item: item.item_id)
    ] == [
        (item.item_id, item.element_id, item.impact, item.certainty)
        for item in sorted(task.items, key=lambda item: item.item_id)
    ]
    assert [item.item_id for item in sort_items(saved.items)] == [
        item.item_id for item in sort_items(task.items)
    ]
    # 展示层要用的那几项也在：模型结论的 note 与全部条号
    first = next(item for item in saved.items if item.item_id == "CF-3-TR-PROBATION-TERM-EXCEED")
    assert (first.grade, first.note, first.statute_ids, first.source) == (
        "A",
        "数值校验一致",
        ("LCL-19",),
        "trigger",
    )
