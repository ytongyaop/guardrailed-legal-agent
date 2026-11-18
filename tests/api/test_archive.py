"""核对结果的存档开关（`POST /checks` 的 `archive` → `CheckTask.shared`）。

两种可见性：

- **存入企业档案**（`archive` 省略 / `true`，默认）：历史页可见，同企业同事可读；
- **不存档**（`archive=false`）：任务照常落库，属主凭任务链接可读、报告可下载，
  但历史页不出现、同企业其他人（含管理员）一律 404。

另有两条向后兼容守卫：旧库补列后老记录 `shared=True`（历史照常可见）、重复初始化幂等。

属主（`CheckTask.owner_id`）走显式字段：落库时写入，管理员替属主操作也改不掉；
迁移前的旧记录该列为空，读取属主时回退查操作日志。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mla.adapters.persist import SqliteRepository
from mla.api.app import create_app
from mla.config import Paths, data_path
from mla.domain.models import CheckTask, OperationLog, TaskMode
from mla.services.tasks import run_check

from .api_helpers import (
    ADMIN_PASSWORD,
    USER_PASSWORD,
    csrf_of,
    login,
    setup_admin,
)
from .flow_helpers import add_user_to_enterprise, count_rows, enable_fake_model, seed_task, wait_job

pytestmark = pytest.mark.mock_llm

CONTRACT = (
    "第一条 合同期限为二年。\n"
    "第二条 试用期六个月，试用期工资按基本工资的百分之六十发放。\n"
    "第三条 月工资为人民币 6000 元。\n"
)


def _post_check(client: TestClient, state: dict, **payload: Any) -> str:
    """发起核对并等到成功，返回任务号。"""
    response = client.post(
        "/api/checks",
        json={**payload},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    done = wait_job(client, response.json()["job_id"])
    assert done["status"] == "succeeded", done
    return str(done["result"]["task"]["task_id"])


def _skip(client: TestClient, state: dict, task_id: str) -> dict[str, Any]:
    """跳过追问 = 把首轮结果补存（有追问时首轮未落库，这里是补存入口；异步）。"""
    response = client.post(
        f"/api/tasks/{task_id}/answers",
        json={"answers": {}, "skip": True},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    done = wait_job(client, response.json()["job_id"])
    assert done["status"] == "succeeded", done
    return done["result"]


def _enterprise_id(client: TestClient) -> str:
    enterprise = client.get("/api/enterprise").json()
    assert enterprise is not None
    return str(enterprise["enterprise_id"])


def _colleague(app: Any, db_path: str, enterprise_id: str, username: str) -> tuple[TestClient, dict]:
    """同企业的普通同事（独立 cookie，同一进程会话表）。"""
    client = TestClient(app)
    state = add_user_to_enterprise(
        client,
        db_path,
        username=username,
        enterprise_id=enterprise_id,
        password=USER_PASSWORD,
    )
    return client, state


# ---------------------------------------------------------------- 默认存档
def test_archive_default_is_shared(api, monkeypatch) -> None:
    """省略 `archive` → `shared=True`：历史可见、同企业同事可读、报告可下载。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    task_id = _post_check(api.client, state, text=CONTRACT, contract_name="默认存档")

    detail = api.client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["shared"] is True

    _skip(api.client, state, task_id)
    assert count_rows(api.db_path, "tasks") == 1
    history = api.client.get("/api/history").json()
    assert history["total_all"] == 1
    assert history["items"][0]["task_id"] == task_id

    colleague, _colleague_state = _colleague(api.app, api.db_path, enterprise_id, "same-ent")
    try:
        assert colleague.get(f"/api/tasks/{task_id}").status_code == 200
        assert colleague.get(f"/api/tasks/{task_id}/report").status_code == 200
    finally:
        colleague.close()


# ---------------------------------------------------------------- 不存档
def test_archive_false_is_owner_only(api, monkeypatch) -> None:
    """`archive=false`：历史不可见、同事 404、管理员也 404；属主可读且能下载报告。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    task_id = _post_check(api.client, state, text=CONTRACT, contract_name="仅自己可见", archive=False)

    detail = api.client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["shared"] is False

    _skip(api.client, state, task_id)
    # 任务确实落库（不是没存），只是不进历史、不对同事开放
    assert count_rows(api.db_path, "tasks") == 1
    history = api.client.get("/api/history").json()
    assert history["total_all"] == 0 and history["items"] == []
    assert api.client.get("/api/history/trend").json()["points"] == []

    # 属主：详情与报告都在
    assert api.client.get(f"/api/tasks/{task_id}").status_code == 200
    assert api.client.get(f"/api/tasks/{task_id}/report").status_code == 200

    colleague, _colleague_state = _colleague(api.app, api.db_path, enterprise_id, "same-ent2")
    try:
        assert colleague.get(f"/api/tasks/{task_id}").status_code == 404
        assert colleague.get(f"/api/tasks/{task_id}/report").status_code == 404
    finally:
        colleague.close()


def test_archive_false_owner_keeps_access_after_restart(api, monkeypatch) -> None:
    """属主身份落在操作日志里：进程重启（内存会话消失）后属主仍可读，同事仍 404。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    task_id = _post_check(api.client, state, text=CONTRACT, contract_name="重启后仍可见", archive=False)
    _skip(api.client, state, task_id)

    restarted = create_app(db_path=api.db_path)
    client = TestClient(restarted)
    colleague: TestClient | None = None
    try:
        login(client, "admin", ADMIN_PASSWORD)
        assert client.get(f"/api/tasks/{task_id}").status_code == 200
        colleague, _colleague_state = _colleague(restarted, api.db_path, enterprise_id, "same-ent3")
        assert colleague.get(f"/api/tasks/{task_id}").status_code == 404
    finally:
        if colleague is not None:
            colleague.close()
        client.close()


def test_archive_false_blocks_follow_up_for_others(api, monkeypatch) -> None:
    """追问 / 补充 / 问答对未存档任务只允许属主：同事连会话都不该看到。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    task_id = _post_check(api.client, state, text=CONTRACT, contract_name="未存档追问", archive=False)

    colleague, colleague_state = _colleague(api.app, api.db_path, enterprise_id, "same-ent4")
    try:
        headers = {"X-CSRF-Token": csrf_of(colleague_state)}
        for path, body in (
            (f"/api/tasks/{task_id}/answers", {"answers": {}, "skip": True}),
            (f"/api/tasks/{task_id}/optional-facts", {"facts": {"monthly_wage": "6000"}}),
            (f"/api/tasks/{task_id}/qa", {"question": "试用期最长多久"}),
        ):
            response = colleague.post(path, json=body, headers=headers)
            assert response.status_code == 404, (path, response.text)
    finally:
        colleague.close()


# ---------------------------------------------------------------- 重跑继承
def test_recheck_inherits_shared_false(api, monkeypatch) -> None:
    """追问重跑生成的新任务继续 `shared=False`，不会被默认值洗成「已存档」。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    task_id = _post_check(api.client, state, text=CONTRACT, contract_name="重跑继承", archive=False)

    response = api.client.post(
        f"/api/tasks/{task_id}/answers",
        json={"answers": {"monthly_wage": "6000"}, "skip": False},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    done = wait_job(api.client, response.json()["job_id"])
    assert done["status"] == "succeeded", done
    reran_id = str(done["result"]["task"]["task_id"])
    assert reran_id != task_id
    assert done["result"]["task"]["shared"] is False

    repo = SqliteRepository(db_path=api.db_path)
    try:
        reran = repo.get_task(reran_id)
        assert reran is not None and reran.shared is False
        assert repo.list_tasks(_enterprise_id(api.client)) == []
        assert [t.task_id for t in repo.list_tasks(_enterprise_id(api.client), shared_only=False)] == [
            reran_id
        ]
    finally:
        repo.close()

    assert api.client.get("/api/history").json()["total_all"] == 0


# ---------------------------------------------------------------- 旧记录兼容
def _legacy_db(db: Path) -> None:
    """造一个没有 `shared` 列的旧版 tasks 表，并塞一条老记录。"""
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


def _repo_for(db: Path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(db))


def test_legacy_rows_are_shared_by_default(tmp_path: Path) -> None:
    """迁移前的记录补列后 `shared=True`：历史照常可见，`get_task` 也能按号取到。"""
    db = tmp_path / "legacy.sqlite3"
    _legacy_db(db)
    repo = _repo_for(db)
    try:
        assert [task.task_id for task in repo.list_tasks("E1")] == ["T-OLD"]
        old = repo.get_task("T-OLD")
        assert old is not None and old.shared is True
        assert old.mode is TaskMode.CHECK
    finally:
        repo.close()


def test_shared_migration_is_idempotent(tmp_path: Path) -> None:
    """重复初始化不报错、不重复加列；新写的任务能带上 `shared=False`。"""
    db = tmp_path / "legacy-idem.sqlite3"
    _legacy_db(db)
    for _ in range(3):
        repo = _repo_for(db)
        try:
            repo.save_task(CheckTask(task_id="T-NEW", mode=TaskMode.CHECK, enterprise_id="E1", shared=False))
        finally:
            repo.close()

    conn = sqlite3.connect(db)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    finally:
        conn.close()
    assert columns.count("shared") == 1
    assert columns.count("owner_id") == 1

    repo = _repo_for(db)
    try:
        assert [task.task_id for task in repo.list_tasks("E1")] == ["T-OLD"]
        new = repo.get_task("T-NEW")
        assert new is not None and new.shared is False
    finally:
        repo.close()


# ---------------------------------------------------------------- 属主（owner_id）
def test_explicit_owner_id_beats_logs(tmp_path: Path) -> None:
    """显式 `owner_id` 优先：就算首条日志是别人（管理员代操作），属主也不被改写。"""
    repo = _repo_for(tmp_path / "owner.sqlite3")
    try:
        repo.save_task(
            CheckTask(
                task_id="T-OWNED",
                mode=TaskMode.CHECK,
                enterprise_id="E1",
                shared=False,
                owner_id="U-OWNER",
            )
        )
        repo.add_log(OperationLog(log_id="L-ADMIN", task_id="T-OWNED", user_id="U-ADMIN", action="核对"))
        assert repo.get_task("T-OWNED").owner_id == "U-OWNER"  # type: ignore[union-attr]
        assert repo.task_owner("T-OWNED") == "U-OWNER", "显式列优先于日志推断"
        assert repo.list_logs(10)[0].user_id == "U-ADMIN", "日志里确实是别人（证明不是靠它取到的）"
    finally:
        repo.close()


def test_legacy_record_owner_falls_back_to_logs(tmp_path: Path) -> None:
    """迁移前的旧记录 `owner_id` 为空：回退查操作日志，仍能认出属主。"""
    db = tmp_path / "legacy-owner.sqlite3"
    _legacy_db(db)
    repo = _repo_for(db)
    try:
        assert repo.get_task("T-OLD").owner_id == ""  # type: ignore[union-attr]
        assert repo.task_owner("T-OLD") == "", "没有日志时属主未知"
        repo.add_log(OperationLog(log_id="L-OLD", task_id="T-OLD", user_id="U-OLD", action="核对"))
        assert repo.task_owner("T-OLD") == "U-OLD", "旧记录回退到日志里的属主"
    finally:
        repo.close()


def test_admin_action_does_not_steal_ownership(api) -> None:
    """管理员代操作（写下的日志是管理员）之后，属主仍可读，管理员仍 404。"""
    setup_admin(api.client)
    admin_id = api.client.get("/api/auth/me").json()["user"]["user_id"]
    enterprise_id = _enterprise_id(api.client)
    owner_client, _owner_state = _colleague(api.app, api.db_path, enterprise_id, "task-owner")
    owner_id = owner_client.get("/api/auth/me").json()["user"]["user_id"]
    try:
        seed_task(
            api.db_path,
            CheckTask(
                task_id="T-OWNED-API",
                mode=TaskMode.CHECK,
                enterprise_id=enterprise_id,
                contract_name="属主可读",
                shared=False,
                owner_id=owner_id,
            ),
        )
        repo = SqliteRepository(db_path=api.db_path)
        try:
            # 模拟「管理员替属主补存」：日志里的 user_id 是管理员，任务行的 owner_id 是属主
            repo.add_log(
                OperationLog(log_id="L-ADMIN-API", task_id="T-OWNED-API", user_id=admin_id, action="核对")
            )
        finally:
            repo.close()

        assert owner_client.get("/api/tasks/T-OWNED-API").status_code == 200
        assert owner_client.get("/api/tasks/T-OWNED-API/report").status_code == 200
        assert api.client.get("/api/tasks/T-OWNED-API").status_code == 404
        assert api.client.get("/api/tasks/T-OWNED-API/report").status_code == 404
    finally:
        owner_client.close()


# ---------------------------------------------------------------- 重跑继承属主
def _rerun(client: TestClient, state: dict, task_id: str, answers: dict[str, str]) -> dict[str, Any]:
    """提交追问重跑（`skip=False`），返回成功结果。"""
    response = client.post(
        f"/api/tasks/{task_id}/answers",
        json={"answers": answers, "skip": False},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    done = wait_job(client, response.json()["job_id"])
    assert done["status"] == "succeeded", done
    return dict(done["result"])


def test_admin_rerun_keeps_original_owner(api, monkeypatch) -> None:
    """管理员替属主点重跑：新任务（新 task_id）的 `owner_id` 仍是原属主，不是管理员。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    admin_id = api.client.get("/api/auth/me").json()["user"]["user_id"]
    enterprise_id = _enterprise_id(api.client)
    owner_client, owner_state = _colleague(api.app, api.db_path, enterprise_id, "rerun-owner")
    owner_id = owner_client.get("/api/auth/me").json()["user"]["user_id"]
    try:
        task_id = _post_check(owner_client, owner_state, text=CONTRACT, contract_name="属主重跑")
        reran = _rerun(api.client, state, task_id, {"monthly_wage": "6000"})
        reran_id = str(reran["task"]["task_id"])
        assert reran_id != task_id
        assert reran["task"]["shared"] is True, "存档开关同样继承"

        repo = SqliteRepository(db_path=api.db_path)
        try:
            saved = repo.get_task(reran_id)
            assert saved is not None
            assert saved.owner_id == owner_id, "属主跟着原任务走，而不是跟着执行人走"
            assert saved.owner_id != admin_id
            assert repo.task_owner(reran_id) == owner_id
        finally:
            repo.close()

        # 存档任务：属主与同企业管理员都能读（可见性仍按 shared 判，不按属主判）
        assert owner_client.get(f"/api/tasks/{reran_id}").status_code == 200
        assert api.client.get(f"/api/tasks/{reran_id}").status_code == 200
    finally:
        owner_client.close()


def test_owner_rerun_stays_unarchived_and_owner_only(api, monkeypatch) -> None:
    """不存档任务的重跑：`shared=False` 与属主都被继承，属主 200、管理员 404。"""
    enable_fake_model(monkeypatch)
    setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    owner_client, owner_state = _colleague(api.app, api.db_path, enterprise_id, "rerun-owner2")
    owner_id = owner_client.get("/api/auth/me").json()["user"]["user_id"]
    try:
        task_id = _post_check(
            owner_client, owner_state, text=CONTRACT, contract_name="重跑仍不存档", archive=False
        )
        reran = _rerun(owner_client, owner_state, task_id, {"monthly_wage": "6000"})
        reran_id = str(reran["task"]["task_id"])
        assert reran["task"]["shared"] is False

        repo = SqliteRepository(db_path=api.db_path)
        try:
            saved = repo.get_task(reran_id)
            assert saved is not None and saved.owner_id == owner_id
            assert repo.list_tasks(enterprise_id) == [], "不进历史"
        finally:
            repo.close()

        assert owner_client.get(f"/api/tasks/{reran_id}").status_code == 200
        assert owner_client.get(f"/api/tasks/{reran_id}/report").status_code == 200
        assert api.client.get(f"/api/tasks/{reran_id}").status_code == 404
        assert api.client.get(f"/api/tasks/{reran_id}/report").status_code == 404
    finally:
        owner_client.close()


def test_service_rerun_by_admin_keeps_owner(api, monkeypatch) -> None:
    """按路由会传的参数（`shared=False, owner_id=原属主`）由管理员执行：属主 200、管理员 404。

    接口层对「不存档任务」的追问/补充/问答只放行属主（同事与管理员都 404），
    所以管理员**发起不了**这种重跑；这里直接调服务层，验证「万一放行」时属主不会被改写。
    """
    enable_fake_model(monkeypatch)
    setup_admin(api.client)
    enterprise_id = _enterprise_id(api.client)
    owner_client, _owner_state = _colleague(api.app, api.db_path, enterprise_id, "svc-owner")
    owner_id = owner_client.get("/api/auth/me").json()["user"]["user_id"]
    repo = SqliteRepository(db_path=api.db_path)
    try:
        admin_user = repo.get_user_by_name("admin")
        assert admin_user is not None
        saved_task = run_check(
            user=admin_user,
            contract_text=CONTRACT,
            repo=repo,
            enterprise_id=enterprise_id,
            clause_review=False,
            shared=False,
            owner_id=owner_id,
        )
        assert saved_task.owner_id == owner_id
        reloaded = repo.get_task(saved_task.task_id)
        assert reloaded is not None and reloaded.owner_id == owner_id
        assert reloaded.owner_id != admin_user.user_id
    finally:
        repo.close()

    try:
        assert owner_client.get(f"/api/tasks/{saved_task.task_id}").status_code == 200
        assert api.client.get(f"/api/tasks/{saved_task.task_id}").status_code == 404
    finally:
        owner_client.close()
