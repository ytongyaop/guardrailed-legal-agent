"""SQLite 仓储测试：账号/企业/合同/任务/日志 的存取往返（临时库，跑完即弃）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from mla.adapters.persist import SqliteRepository
from mla.config import Paths
from mla.domain.models import (
    CheckItem,
    CheckTask,
    ContractRecord,
    Enterprise,
    ItemKind,
    MissingStatus,
    MissingTerm,
    OperationLog,
    RuleCoverage,
    Severity,
    Suggestion,
    TaskMode,
    UndeterminedRule,
    User,
)
from mla.services.auth import hash_password


def _repo(tmp_path) -> SqliteRepository:
    paths = Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml")
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "t.sqlite3"))


def test_user_roundtrip(tmp_path) -> None:
    repo = _repo(tmp_path)
    digest, salt = hash_password("secret123")
    repo.create_user(User(user_id="U1", username="alice", password_hash=digest, salt=salt))
    got = repo.get_user_by_name("alice")
    assert got is not None
    assert got.user_id == "U1"
    assert got.password_hash == digest


def test_enterprise_and_contract(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技", staff_size=12))
    ent = repo.get_enterprise("E1")
    assert ent is not None and ent.name == "某某科技" and ent.staff_size == 12

    repo.save_contract(
        ContractRecord(contract_id="C1", enterprise_id="E1", filename="a.txt", text="合同正文")
    )
    contracts = repo.list_contracts("E1")
    assert len(contracts) == 1 and contracts[0].text == "合同正文"
    assert repo.get_contract("C1") is not None
    assert repo.get_contract("C1").filename == "a.txt"  # type: ignore[union-attr]


def test_task_roundtrip_preserves_items(tmp_path) -> None:
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T1",
        mode=TaskMode.CHECK,
        enterprise_id="E1",
        contract_name="某科技-劳动合同.docx",
        contract_chars=320,
        facts={"term_years": "3"},
        items=[
            CheckItem(
                item_id="R-PROBATION-6M",
                kind=ItemKind.RISK,
                clause_ref="",
                finding="试用期超限",
                source_id="LCL-19",
                severity=Severity.HARD,
                rule_id="R-PROBATION-6M",
            )
        ],
        suggestions=[Suggestion(text="调整试用期", source_ids=("LCL-19",))],
        latency_s=0.42,
        created_at=datetime(2026, 11, 3, 10, 30, 5),
    )
    repo.save_task(task)
    got = repo.list_tasks("E1")
    assert len(got) == 1
    assert got[0].items[0].source_id == "LCL-19"
    assert got[0].suggestions[0].source_ids == ("LCL-19",)
    assert got[0].facts["term_years"] == "3"
    assert got[0].contract_name == "某科技-劳动合同.docx"
    assert got[0].contract_chars == 320
    assert got[0].created_at == datetime(2026, 11, 3, 10, 30, 5)
    assert got[0].deleted is False


def test_task_roundtrip_preserves_closure_fields(tmp_path) -> None:
    """第 0 步：补问轮数 / 追问 / 拒答层 / 校验结果必须完整往返（界面靠这些字段说话）。"""
    repo = _repo(tmp_path)
    task = CheckTask(
        task_id="T2",
        mode=TaskMode.QA,
        enterprise_id="E1",
        questions=["是否已签订书面劳动合同？", "合同期限是几年？"],
        ask_rounds=2,
        ask_history=[{"round": 1, "questions": ["是否已签订书面劳动合同？"]}],  # type: ignore[list-item]
        refused=True,
        boundary_layer="terms",
        boundary_reason="命中越界词：['离婚']",
        verify_ok=False,
        degraded=True,
        errors=["越界拒答", "已降级"],
        created_at=datetime(2026, 11, 4, 9, 15),
    )
    repo.save_task(task)
    got = repo.list_tasks("E1")[0]
    assert got.ask_rounds == 2
    assert got.questions == ["是否已签订书面劳动合同？", "合同期限是几年？"]
    assert got.refused is True
    assert got.boundary_layer == "terms"
    assert got.boundary_reason == "命中越界词：['离婚']"
    assert got.verify_ok is False
    assert got.degraded is True
    assert got.errors == ["越界拒答", "已降级"]
    assert got.ask_history[0]["round"] == 1


def test_task_roundtrip_preserves_rule_coverage(tmp_path) -> None:
    """规则层旁路留痕（未判定条目 + 四态分布）必须完整往返。"""
    repo = _repo(tmp_path)
    undetermined = [
        UndeterminedRule(
            rule_id="R-AL-10D",
            name="累计工作满 10 年不满 20 年，年休假 10 天",
            statute_id="AL-3",
            missing_fields=("annual_leave_days",),
            reason="field_missing",
        ),
        UndeterminedRule(
            rule_id="R-PROBATION-1M",
            name="合同期限三个月以上不满一年，试用期不得超过 1 个月",
            statute_id="LCL-19",
            missing_fields=("term_years",),
            reason="premise_missing",
        ),
    ]
    coverage = RuleCoverage(total=20, violated=1, compliant=2, not_applicable=3, unknown=14)
    repo.save_task(
        CheckTask(
            task_id="T3",
            mode=TaskMode.CHECK,
            enterprise_id="E1",
            undetermined=undetermined,
            rule_coverage=coverage,
            created_at=datetime(2026, 11, 6, 8, 0),
        )
    )
    got = repo.list_tasks("E1")[0]
    assert got.undetermined == undetermined
    assert got.rule_coverage == coverage


def test_task_roundtrip_preserves_missing_terms(tmp_path) -> None:
    """必备条款判定逐字段往返；覆盖率是属性，不落库。"""
    repo = _repo(tmp_path)
    terms = [
        MissingTerm(
            term_id="MT-3",
            name="劳动合同期限",
            clause_no="（三）",
            statute_id="LCL-17",
            status=MissingStatus.ABSENT,
            certainty="高",
            note="未检出必备条款表述",
        ),
        MissingTerm(
            term_id="MT-6",
            name="劳动报酬",
            clause_no="（六）",
            statute_id="LCL-17",
            status=MissingStatus.WRITTEN,
            certainty="高",
        ),
        MissingTerm(
            term_id="MT-9",
            name="法律法规规定应当纳入的其他事项",
            clause_no="（九）",
            statute_id="LCL-17",
            status=MissingStatus.MANUAL,
            certainty="低",
            note="兜底事项，需人工确认",
        ),
    ]
    repo.save_task(
        CheckTask(
            task_id="T-MISS",
            mode=TaskMode.CHECK,
            enterprise_id="E1",
            missing_terms=terms,
            created_at=datetime(2026, 11, 6, 10, 0),
        )
    )
    got = repo.list_tasks("E1")[0]
    assert got.missing_terms == terms
    assert got.missing_terms[0].status is MissingStatus.ABSENT
    assert got.missing_coverage == 1 / 8


def test_task_roundtrip_preserves_clause_review_flag(tmp_path) -> None:
    """条款审查开关要落库：历史详情靠它区分「未做审查」与「审查过但没发现」。"""
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T-REV", mode=TaskMode.CHECK, enterprise_id="E1", clause_review=True))
    repo.save_task(CheckTask(task_id="T-NOREV", mode=TaskMode.CHECK, enterprise_id="E1"))
    got = {t.task_id: t for t in repo.list_tasks("E1")}
    assert got["T-REV"].clause_review is True
    assert got["T-NOREV"].clause_review is False


def test_missing_terms_absent_roundtrips_as_empty(tmp_path) -> None:
    """没有判过的任务读回来是空列表，不报错。"""
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T-EMPTY", mode=TaskMode.CHECK, enterprise_id="E1"))
    assert repo.list_tasks("E1")[0].missing_terms == []


def test_broken_missing_terms_column_falls_back_to_empty(tmp_path) -> None:
    """坏 JSON / 空串 / 不认识的状态值都不能让读取炸掉。"""
    db = tmp_path / "t.sqlite3"
    repo = _repo(tmp_path)
    for task_id in ("T-BAD1", "T-BAD2", "T-BAD3"):
        repo.save_task(CheckTask(task_id=task_id, mode=TaskMode.CHECK, enterprise_id="E1"))
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE tasks SET missing_terms='{不是 JSON' WHERE task_id='T-BAD1'")
    conn.execute(
        'UPDATE tasks SET missing_terms=\'[{"term_id": "MT-3", "status": "不存在的状态"}]\' '
        "WHERE task_id='T-BAD2'"
    )
    conn.commit()
    conn.close()

    by_id = {t.task_id: t for t in repo.list_tasks("E1")}
    assert by_id["T-BAD1"].missing_terms == []
    assert by_id["T-BAD2"].missing_terms == []
    assert by_id["T-BAD3"].missing_terms == []


def test_rule_coverage_absent_roundtrips_as_empty(tmp_path) -> None:
    """没有留痕的任务（问答类等）读回来是空列表 / None，不是报错。"""
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T9", mode=TaskMode.CHECK, enterprise_id="E1"))
    got = repo.list_tasks("E1")[0]
    assert got.undetermined == []
    assert got.rule_coverage is None


def test_broken_rule_columns_fall_back_to_empty(tmp_path) -> None:
    """库里存了坏 JSON 也不能让读取炸掉。"""
    db = tmp_path / "t.sqlite3"
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T4", mode=TaskMode.CHECK, enterprise_id="E1"))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE tasks SET undetermined='{不是 JSON', rule_coverage='{\"total\": 1}' WHERE task_id='T4'"
    )
    conn.commit()
    conn.close()

    got = repo.list_tasks("E1")[0]
    assert got.undetermined == []
    assert got.rule_coverage is None


def test_soft_delete_hides_task_but_keeps_row(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T1", mode=TaskMode.CHECK, enterprise_id="E1"))
    repo.save_task(CheckTask(task_id="T2", mode=TaskMode.CHECK, enterprise_id="E1"))

    repo.delete_task("T1", enterprise_id="E1")

    assert [t.task_id for t in repo.list_tasks("E1")] == ["T2"]
    assert repo.count_tasks("E1") == (1, 1)
    assert [t.task_id for t in repo.list_tasks("E1", include_deleted=True)] == ["T2", "T1"]
    # 跨企业不可误删
    repo.delete_task("T2", enterprise_id="OTHER")
    assert len(repo.list_tasks("E1")) == 1


def test_user_enterprise_binding(tmp_path) -> None:
    repo = _repo(tmp_path)
    digest, salt = hash_password("secret123")
    repo.create_user(User(user_id="U1", username="bob", password_hash=digest, salt=salt))
    repo.set_user_enterprise("U1", "E1")
    assert repo.get_user_by_name("bob").enterprise_id == "E1"  # type: ignore[union-attr]


def test_tasks_are_isolated_by_enterprise(tmp_path) -> None:
    """企业隔离：空企业不能看到全库记录，也不能跨企业删除。"""
    repo = _repo(tmp_path)
    repo.save_task(CheckTask(task_id="T1", mode=TaskMode.CHECK, enterprise_id="E1"))
    repo.save_task(CheckTask(task_id="T2", mode=TaskMode.CHECK, enterprise_id="E2"))

    assert [t.task_id for t in repo.list_tasks("E1")] == ["T1"]
    assert [t.task_id for t in repo.list_tasks("E2")] == ["T2"]
    assert repo.list_tasks("") == []
    assert repo.count_tasks("") == (0, 0)

    repo.delete_task("T1", enterprise_id="")
    assert len(repo.list_tasks("E1")) == 1


def test_legacy_tasks_table_is_migrated(tmp_path) -> None:
    """旧库（无新列）打开不报错：补列后老数据可读，新数据可写。"""
    db = tmp_path / "legacy.sqlite3"
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

    paths = Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml")
    repo = SqliteRepository(paths=paths, db_path=str(db))
    old = repo.list_tasks("E1")[0]
    assert old.task_id == "T-OLD"
    assert old.ask_rounds == 0 and old.refused is False
    assert old.created_at == datetime(2026, 11, 1, 0, 0)
    assert old.deleted is False

    repo.save_task(CheckTask(task_id="T-NEW", mode=TaskMode.CHECK, enterprise_id="E1", ask_rounds=2))
    assert repo.list_tasks("E1")[0].ask_rounds == 2


def test_legacy_tasks_table_without_rule_columns(tmp_path) -> None:
    """不含 undetermined / rule_coverage 的旧表：迁移后能打开，老记录读成空列表 / None。"""
    db = tmp_path / "legacy2.sqlite3"
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
        ("T-OLD2", "E1", "核对", "full", "{}", "[]", "[]", 1, 0, 0.3, 0, 0, "2026-11-05"),
    )
    conn.commit()
    conn.close()

    paths = Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml")
    repo = SqliteRepository(paths=paths, db_path=str(db))
    old = repo.list_tasks("E1")[0]
    assert old.task_id == "T-OLD2"
    assert old.undetermined == []
    assert old.rule_coverage is None
    assert old.missing_terms == []  # 旧库没有 missing_terms 列，补列后读成空列表

    coverage = RuleCoverage(total=20, violated=0, compliant=0, not_applicable=0, unknown=20)
    repo.save_task(
        CheckTask(task_id="T-NEW2", mode=TaskMode.CHECK, enterprise_id="E1", rule_coverage=coverage)
    )
    assert repo.list_tasks("E1")[0].rule_coverage == coverage


def test_enterprise_delete_removes_contracts(tmp_path) -> None:
    """企业档案可随时删除；合同随档案删，任务历史保留。"""
    repo = _repo(tmp_path)
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技"))
    repo.save_contract(ContractRecord(contract_id="C1", enterprise_id="E1", filename="a.txt", text="正文"))
    repo.save_task(CheckTask(task_id="T1", mode=TaskMode.CHECK, enterprise_id="E1"))

    repo.delete_enterprise("E1")

    assert repo.get_enterprise("E1") is None
    assert repo.list_contracts("E1") == []
    assert len(repo.list_tasks("E1")) == 1


def test_contract_delete(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.save_contract(ContractRecord(contract_id="C1", enterprise_id="E1", filename="a.txt", text="正文"))
    repo.delete_contract("C1")
    assert repo.get_contract("C1") is None
    assert repo.list_contracts("E1") == []


def test_logs_are_newest_first(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.add_log(OperationLog(log_id="L1", task_id="T1", user_id="U1", action="核对", detail="a"))
    repo.add_log(OperationLog(log_id="L2", task_id="T2", user_id="U1", action="问答", detail="b"))
    logs = repo.list_logs()
    assert [x.log_id for x in logs] == ["L2", "L1"]


def test_statutes_and_rules_loaded(tmp_path) -> None:
    repo = _repo(tmp_path)
    assert len(repo.list_statutes()) >= 100
    assert len(repo.list_rules()) == 20
