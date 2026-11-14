"""`rule_check` 节点双写（规格 003 第 13 章 P1-1）：旧 `items` + 新 `rule_checks`。

本阶段的契约：
1. `items` 的产出逻辑一行不改（条目数 / 规则 ID 与改动前逐字节一致）；
2. 同时把**全部** `RuleOutcome`（ok / mismatch / needs_fact 三态）经 `to_rule_check`
   写进 `task.rule_checks`，供后续共识层看到全貌；
3. 落库兼容：新列 `rule_checks`（TEXT / JSON），旧库打开时自动补列，旧记录读成空列表，
   不回填（规则表会随 P1-4 瘦身，回填等于篡改历史）。

用例一律 `MLA_ALLOW_NO_LLM=1` + 正则抽取（conftest autouse），只跑规则通道。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path
from mla.domain.models import CheckTask, Role, TaskMode, User
from mla.domain.rules import RuleCheckStatus, judge_all, load_rules, to_rule_check
from mla.services.tasks import run_check

NO_LLM = LLMConfig(api_key="")
# 固定事实：与 tests/test_missing_terms.py 同一口径，保证结果只随合同正文变化。
BASE_FACTS = {"employer_type": "有限责任公司", "written_contract": "是", "term_years": "3"}

# 改动前（双写前）实测条目：`sample_contract.txt` + `BASE_FACTS` 下规则通道的全部违规条目，
# 顺序与规则表一致（`items` 由 `evaluate` 的结果顺序生成，不重排）。
# 注：P1-4 删除 20 条中的 4 条后，`R-WRITTEN-1M` 不再在表内，此处应同步改为 4 条。
SAMPLE_ITEMS = [
    "R-PROBATION-6M",
    "R-PROBATION-WAGE-80",
    "R-WRITTEN-1M",
    "R-NONCOMPETE-TERM-2Y",
    "R-OT-MONTH-36H",
]


def _read(name: str) -> str:
    return Path(data_path("evalsets/" + name)).read_text(encoding="utf-8")


def _user() -> User:
    return User(
        user_id="U1",
        username="alice",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )


def _repo(tmp_path: Path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "dual.sqlite3"))


def _rule_count() -> int:
    return len(load_rules(data_path("rules/rules.yaml")))


def _check_for(rule_id: str):
    """按固定事实取指定规则的校验信号（构造持久化 fixture 用）。"""
    outcomes = judge_all(load_rules(data_path("rules/rules.yaml")), dict(BASE_FACTS))
    return to_rule_check(next(o for o in outcomes if o.rule.rule_id == rule_id))


def _run(contract: str, repo: SqliteRepository | None = None, **kwargs) -> CheckTask:
    return run_check(
        user=_user(),
        contract_text=_read(contract),
        facts=dict(BASE_FACTS),
        repo=repo,
        save=repo is not None,
        llm=NO_LLM,
        **kwargs,
    )


# ---------- 1. items 语义不变 ----------


def test_items_semantics_unchanged() -> None:
    """双写不改 `items`：条目数与规则 ID 必须等于改动前实测值。"""
    task = _run("sample_contract.txt")
    assert [i.item_id for i in task.items] == SAMPLE_ITEMS
    assert all(i.source_id for i in task.items)  # 每条仍带法条 ID
    assert task.rule_coverage is not None
    assert task.rule_coverage.total == _rule_count()


# ---------- 2. rule_checks 覆盖全部规则、三态齐全 ----------


def test_rule_checks_cover_every_rule_with_all_three_states() -> None:
    """每条规则一条校验信号，三态都在，且条数恒等于规则表条数。"""
    rules = load_rules(data_path("rules/rules.yaml"))
    task = _run("sample_contract.txt")
    assert len(task.rule_checks) == len(rules)
    # 顺序与规则表一致（与 judge_all 口径相同，共识层按 rule_id 对齐即可）
    assert [c.rule_id for c in task.rule_checks] == [r.rule_id for r in rules]
    statuses = {c.status for c in task.rule_checks}
    assert statuses == {RuleCheckStatus.MISMATCH, RuleCheckStatus.OK, RuleCheckStatus.NEEDS_FACT}
    assert all(c.expected for c in task.rule_checks)
    assert {c.severity for c in task.rule_checks} == {"hard"}


def test_mismatch_checks_line_up_with_items() -> None:
    """mismatch ↔ items：没有条款审查让位时，`items` 就是全部 mismatch 的那几条。"""
    task = _run("sample_contract.txt")
    mismatched = {c.rule_id for c in task.rule_checks if c.status is RuleCheckStatus.MISMATCH}
    assert mismatched == {i.item_id for i in task.items}


def test_needs_fact_checks_cover_bypass_lists() -> None:
    """判不了的规则既在 `rule_checks`（needs_fact）里，也仍在 `undetermined` / `unmet` 旁路留痕。"""
    task = _run("sample_contract.txt")
    needs_fact = {c.rule_id for c in task.rule_checks if c.status is RuleCheckStatus.NEEDS_FACT}
    bypass = {u.rule_id for u in task.undetermined} | {u.rule_id for u in task.unmet}
    assert bypass <= needs_fact
    assert all(c.actual == "" for c in task.rule_checks if c.status is RuleCheckStatus.NEEDS_FACT)


def test_ok_checks_never_enter_items() -> None:
    """ok 只是校验信号，不进 `items`（规则本阶段仍只由违规产出条目）。"""
    task = _run("sample_contract.txt")
    ok_ids = {c.rule_id for c in task.rule_checks if c.status is RuleCheckStatus.OK}
    assert ok_ids
    assert not ok_ids & {i.item_id for i in task.items}


def test_check_task_defaults_to_empty_list() -> None:
    """新字段默认空列表，不改变任何既有字段语义（也不共享同一个 list）。"""
    a = CheckTask(task_id="T1", mode=TaskMode.CHECK)
    b = CheckTask(task_id="T2", mode=TaskMode.CHECK)
    assert a.rule_checks == []
    assert a.rule_checks is not b.rule_checks


def test_llm_only_variant_writes_no_checks() -> None:
    """llm_only 档整层不跑：返回空列表而非 None（与 `rule_coverage` 的全零口径一致）。"""
    task = _run("sample_contract.txt", variant="llm_only")
    assert task.items == []
    assert task.rule_checks == []


# ---------- 3. 持久化往返 ----------


def test_rule_checks_roundtrip_through_sqlite(tmp_path: Path) -> None:
    task = _run("sample_contract.txt", repo=_repo(tmp_path))
    assert task.rule_checks, "落库前就该有校验信号"
    got = _repo(tmp_path).list_tasks("E1")[0]
    assert got.rule_checks == task.rule_checks
    assert len(got.rule_checks) == _rule_count()
    assert got.items[0].item_id == task.items[0].item_id


def test_legacy_record_without_rule_checks_reads_empty(tmp_path: Path) -> None:
    """旧库没有 rule_checks 列：迁移补列后旧记录读成空列表，新记录可写可读。"""
    db = tmp_path / "legacy-dual.sqlite3"
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
        ("T-OLD", "E1", "核对", "full", "{}", "[]", "[]", 1, 0, 0.5, 0, 0, "2026-11-01"),
    )
    conn.commit()
    conn.close()

    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    repo = SqliteRepository(paths=paths, db_path=str(db))
    old = repo.list_tasks("E1")[0]
    assert old.rule_checks == []  # 不回填：旧记录保持"没有校验信号"

    check = _check_for("R-PROBATION-WAGE-80")
    repo.save_task(
        CheckTask(
            task_id="T-NEW",
            mode=TaskMode.CHECK,
            enterprise_id="E1",
            rule_checks=[check],
            created_at=datetime(2026, 11, 14, 10, 0),
        )
    )
    assert repo.list_tasks("E1")[0].rule_checks == [check]


def test_bad_rule_check_rows_are_skipped(tmp_path: Path) -> None:
    """坏行 / 不认识的状态只跳过这一条，绝不让旧库打不开。"""
    db = tmp_path / "bad-dual.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY, enterprise_id TEXT, mode TEXT, variant TEXT,
          facts TEXT, items TEXT, suggestions TEXT, verify_ok INTEGER, degraded INTEGER,
          latency_s REAL, token_in INTEGER, token_out INTEGER, created_at TEXT,
          rule_checks TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "T-BAD",
            "E1",
            "核对",
            "full",
            "{}",
            "[]",
            "[]",
            1,
            0,
            0.5,
            0,
            0,
            "2026-11-01",
            '[{"rule_id":"R-OK","status":"ok","field":"f","expected":"<= 1","actual":"1",'
            '"statute_id":"LCL-19","evidence_span":"x","severity":"hard"},'
            '{"rule_id":"R-NO-STATUS","expected":"<= 1"},'
            '{"status":"mismatch"},'
            '{"rule_id":"R-BAD","status":"violated"}]',
        ),
    )
    conn.commit()
    conn.close()

    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    repo = SqliteRepository(paths=paths, db_path=str(db))
    got = repo.list_tasks("E1")[0]
    assert [c.rule_id for c in got.rule_checks] == ["R-OK"]
    assert got.rule_checks[0].status is RuleCheckStatus.OK
