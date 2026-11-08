"""账号硬删：仓储级联 + 服务层保护规则 + 审计日志（临时库，跑完即弃）。"""

from __future__ import annotations

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import (
    CheckTask,
    ContractRecord,
    Enterprise,
    OperationLog,
    Role,
    TaskMode,
    User,
)
from mla.services.auth import delete_account, hash_password


def _repo(tmp_path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "t.sqlite3"))


def _user(user_id: str, username: str, role: Role = Role.USER, enterprise_id: str = "") -> User:
    digest, salt = hash_password("secret123")
    return User(
        user_id=user_id,
        username=username,
        password_hash=digest,
        salt=salt,
        role=role,
        enterprise_id=enterprise_id,
    )


def test_delete_user_cascades_enterprise_scope(tmp_path) -> None:
    """删账号要连带该企业范围的企业/合同/任务/日志（含软删任务）。"""
    repo = _repo(tmp_path)
    repo.create_user(_user("U1", "u1", enterprise_id="E1"))
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技"))
    repo.save_contract(ContractRecord(contract_id="C1", enterprise_id="E1", filename="a.txt", text="正文A"))
    repo.save_contract(ContractRecord(contract_id="C2", enterprise_id="E1", filename="b.txt", text="正文B"))
    repo.save_task(CheckTask(task_id="T1", mode=TaskMode.CHECK, enterprise_id="E1"))
    repo.save_task(CheckTask(task_id="T2", mode=TaskMode.CHECK, enterprise_id="E1"))
    repo.delete_task("T2", enterprise_id="E1")  # 软删过也要硬删掉
    repo.add_log(OperationLog(log_id="L1", task_id="T1", user_id="U1", action="核对"))
    repo.add_log(OperationLog(log_id="L2", task_id="T2", user_id="U1", action="核对"))
    repo.add_log(OperationLog(log_id="L3", task_id="T1", user_id="U-other", action="问答"))

    result = repo.delete_user("U1", actor_id="U-admin", log_id="L-del")

    assert result.deleted is True
    assert (result.contracts, result.tasks, result.logs) == (2, 2, 3)
    assert repo.get_user_by_name("u1") is None
    assert repo.get_enterprise("E1") is None
    assert repo.list_contracts("E1") == []
    assert repo.list_tasks("E1", include_deleted=True) == []
    logs = repo.list_logs()
    assert len(logs) == 1
    assert logs[0].action == "删除账号"


def test_delete_user_with_empty_enterprise_keeps_other_data(tmp_path) -> None:
    """空企业不得按空串删数据，别人的企业/合同/任务必须原样保留。"""
    repo = _repo(tmp_path)
    repo.create_user(_user("U1", "u1"))  # enterprise_id 为空
    repo.create_user(_user("U2", "u2", enterprise_id="E2"))
    repo.save_enterprise(Enterprise(enterprise_id="E2", name="另一家"))
    repo.save_contract(ContractRecord(contract_id="C2", enterprise_id="E2", filename="b.txt", text="正文B"))
    repo.save_task(CheckTask(task_id="T2", mode=TaskMode.CHECK, enterprise_id="E2"))

    result = repo.delete_user("U1", actor_id="U-admin", log_id="L-del")

    assert result.deleted is True
    assert result.enterprise_id == ""
    assert (result.contracts, result.tasks) == (0, 0)
    assert repo.get_user_by_name("u1") is None
    assert repo.get_user_by_name("u2") is not None
    assert repo.get_enterprise("E2") is not None
    assert len(repo.list_contracts("E2")) == 1
    assert len(repo.list_tasks("E2", include_deleted=True)) == 1


def test_delete_self_rejected(tmp_path) -> None:
    repo = _repo(tmp_path)
    admin = _user("U-admin", "admin", role=Role.ADMIN)
    repo.create_user(admin)

    result = delete_account(repo, actor=admin, target_username="admin")

    assert result.deleted is False
    assert "当前登录" in result.reason
    assert repo.get_user_by_name("admin") is not None


def test_delete_last_admin_rejected(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.create_user(_user("U-admin", "admin", role=Role.ADMIN))
    # 保护顺序里“删自己”排在“最后一个管理员”之前，用不在库内的管理员身份才能命中第 4 条
    ghost = _user("U-ghost", "ghost", role=Role.ADMIN)

    rejected = delete_account(repo, actor=ghost, target_username="admin")

    assert rejected.deleted is False
    assert rejected.reason == "系统至少保留一个管理员"
    assert repo.get_user_by_name("admin") is not None

    second = _user("U-admin2", "admin2", role=Role.ADMIN)
    repo.create_user(second)
    ok = delete_account(repo, actor=second, target_username="admin")
    assert ok.deleted is True
    assert repo.get_user_by_name("admin") is None


def test_delete_missing_user_returns_false(tmp_path) -> None:
    repo = _repo(tmp_path)
    admin = _user("U-admin", "admin", role=Role.ADMIN)
    repo.create_user(admin)
    repo.add_log(OperationLog(log_id="L1", task_id="T1", user_id=admin.user_id, action="核对"))

    result = delete_account(repo, actor=admin, target_username="nobody")

    assert result.deleted is False
    assert result.reason == "账号不存在"
    assert len(repo.list_logs()) == 1  # 不新增日志


def test_audit_log_records_actor_and_counts(tmp_path) -> None:
    repo = _repo(tmp_path)
    admin = _user("U-admin", "admin", role=Role.ADMIN)
    repo.create_user(admin)
    repo.create_user(_user("U1", "u1", enterprise_id="E1"))
    repo.save_enterprise(Enterprise(enterprise_id="E1", name="某某科技"))
    repo.save_contract(ContractRecord(contract_id="C1", enterprise_id="E1", filename="a.txt", text="正文A"))
    repo.save_contract(ContractRecord(contract_id="C2", enterprise_id="E1", filename="b.txt", text="正文B"))
    repo.save_task(CheckTask(task_id="T1", mode=TaskMode.CHECK, enterprise_id="E1"))
    repo.add_log(OperationLog(log_id="L1", task_id="T1", user_id="U1", action="核对"))

    result = delete_account(repo, actor=admin, target_username="u1")

    assert result.deleted is True
    assert (result.contracts, result.tasks, result.logs) == (2, 1, 1)
    audit = repo.list_logs()[0]
    assert audit.action == "删除账号"
    assert audit.user_id == admin.user_id
    assert audit.task_id == ""
    assert "u1（U1）" in audit.detail
    assert "企业 E1" in audit.detail
    assert "合同 2" in audit.detail
    assert "任务 1" in audit.detail
    assert "日志 1" in audit.detail
