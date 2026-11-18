"""首轮核对的落库时机（`POST /checks` → `run_check(save=False)` → 唯一出口落库）。

三条口径：

1. **无追问**：核对完成即落库（`shared` 跟着 `archive`），「看完就走」的记录不再丢；
2. **有追问**：首轮只在内存会话里，等用户作答重跑或点「跳过」时再落库（不重复写）；
3. **落库失败**：不静默、也不把已算好的结果整份丢掉——原因写进结果里的 `errors`。

这份文件与 `test_checks_flow.py` 的分工：那里测异步链路与属主边界（用的合同会触发追问），
这里只测「什么时候入库」，所以两份合同分别是「阻断项齐全」与「缺阻断项」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mla.adapters.persist import SqliteRepository
from mla.api.app import create_app

from .api_helpers import ADMIN_PASSWORD, csrf_of, login, setup_admin
from .flow_helpers import count_rows, enable_fake_model, wait_job

pytestmark = pytest.mark.mock_llm

#: 三项阻断事实（主体类型 / 书面订立 / 期限）都能从正文抽到 → 不需要追问，核对完即最终结果。
SETTLED_CONTRACT = (
    "甲方为有限责任公司，双方签订书面劳动合同。\n"
    "第一条 合同期限为二年，月工资为人民币 6000 元，甲方每月 15 日以货币形式支付。\n"
)

#: 正文既没写书面订立、也没写合同期限 → 阻断项缺失，必然追问，首轮不落库。
ASKING_CONTRACT = (
    "第一条 乙方岗位为营业员，工作地点为门店。\n"
    "第二条 月工资为人民币 6000 元，甲方每月 15 日以货币形式支付。\n"
)


def _post_check(client: TestClient, state: dict, **payload: object) -> dict:
    response = client.post(
        "/api/checks",
        json={**payload},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


def _check(client: TestClient, state: dict, text: str, **payload: object) -> dict:
    """发起核对并等到成功，返回轮询结果（断言成功由调用方各自负责）。"""
    return dict(wait_job(client, _post_check(client, state, text=text, **payload)["job_id"]))


def _saved_task(db_path: str, task_id: str) -> object:
    repo = SqliteRepository(db_path=db_path)
    try:
        return repo.get_task(task_id)
    finally:
        repo.close()


def test_no_questions_persists_on_creation(api, monkeypatch) -> None:
    """无追问：核对完成即落库，`get_task` 立刻可读、历史可见、默认存档。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)

    done = _check(api.client, state, SETTLED_CONTRACT, contract_name="无追问合同")
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["workflow"]["questions"] == [], "这份合同不该触发追问，否则用例没测到无追问分支"
    task_id = result["task"]["task_id"]

    assert count_rows(api.db_path, "tasks") == 1, "无追问的核对完成即落库"
    saved = _saved_task(api.db_path, task_id)
    assert saved is not None, "落库后按任务号应能单条查到"
    assert saved.task_id == task_id
    assert saved.questions == []
    assert saved.shared is True, "省略 archive → 存入企业档案"
    assert saved.owner_id, "落库时写入显式属主"

    detail = api.client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["task"]["task_id"] == task_id
    assert detail.json()["task"]["shared"] is True

    history = api.client.get("/api/history").json()
    assert history["total_all"] == 1
    assert history["items"][0]["task_id"] == task_id

    # 重启（内存会话消失）后仍能读回来：这正是「看完就走、重启后找不回」的修复点
    restarted = create_app(db_path=api.db_path)
    client = TestClient(restarted)
    try:
        login(client, "admin", ADMIN_PASSWORD)
        assert client.get(f"/api/tasks/{task_id}").status_code == 200
    finally:
        client.close()


def test_no_questions_persists_even_when_archive_false(api, monkeypatch) -> None:
    """`archive=false` 同样落库，只是 `shared=0`：历史不可见，属主仍凭任务链接可读。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)

    done = _check(api.client, state, SETTLED_CONTRACT, contract_name="仅自己可见", archive=False)
    assert done["status"] == "succeeded", done
    task_id = done["result"]["task"]["task_id"]
    assert done["result"]["task"]["shared"] is False

    assert count_rows(api.db_path, "tasks") == 1, "不存档也是落库，只是不进历史"
    saved = _saved_task(api.db_path, task_id)
    assert saved is not None and saved.shared is False

    assert api.client.get("/api/history").json()["total_all"] == 0
    assert api.client.get(f"/api/tasks/{task_id}").status_code == 200


def test_questions_defer_persist_until_answered(api, monkeypatch) -> None:
    """有追问：首轮不落库；点「跳过」补存当前这套结果后表里 +1。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)

    done = _check(api.client, state, ASKING_CONTRACT, contract_name="要追问的合同")
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["workflow"]["questions"], "缺阻断项时必须追问"
    task_id = result["task"]["task_id"]
    assert count_rows(api.db_path, "tasks") == 0, "有追问时首轮不落库"

    response = api.client.post(
        f"/api/tasks/{task_id}/answers",
        json={"answers": {}, "skip": True},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    skipped = wait_job(api.client, response.json()["job_id"])
    assert skipped["status"] == "succeeded", skipped
    assert skipped["task_id"] == task_id, "跳过落的是当前这套结果，不重跑"

    assert count_rows(api.db_path, "tasks") == 1, "跳过追问后落库一次"
    assert _saved_task(api.db_path, task_id) is not None
    assert api.client.get("/api/history").json()["total_all"] == 1


def test_save_failure_is_reported_in_result(api, monkeypatch) -> None:
    """落库抛错：结果照样返回（不升级成 job 失败），但错误必须写进结果里。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)

    def boom(self: SqliteRepository, task: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(SqliteRepository, "save_task", boom)

    done = _check(api.client, state, SETTLED_CONTRACT, contract_name="落库失败")
    assert done["status"] == "succeeded", "落库失败不该把已经算好的结果整份丢掉"
    result = done["result"]
    assert any("未能存入历史" in line for line in result["errors"]), result["errors"]
    assert count_rows(api.db_path, "tasks") == 0

    # 内存会话还在，结果页仍可读（只是历史里没有这条）
    task_id = result["task"]["task_id"]
    assert api.client.get(f"/api/tasks/{task_id}").status_code == 200
