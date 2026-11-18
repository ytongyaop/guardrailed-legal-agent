"""异步核对链路（规格 006 §2：`POST /checks` → `GET /jobs/{id}` → `/tasks/*`）。

一条主链路走通即覆盖四件事：

1. `202 + job_id`，状态机只有 queued/running/succeeded/failed；
2. 首轮核对沿用旧 `_check_form` 的 `save=False`：**任务不落库**，但新合同按原逻辑存档；
3. `POST /tasks/{id}/answers` 跳过追问时把首轮结果补存，历史立刻可见；
4. 任务与 job 的属主边界：同企业非属主读不到内存任务，同企业管理员可以。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mla.api.app import create_app

from .api_helpers import ADMIN_PASSWORD, USER_PASSWORD, csrf_of, seed_contract, setup_admin
from .flow_helpers import add_user_to_enterprise, count_rows, enable_fake_model, wait_job

pytestmark = pytest.mark.mock_llm

CONTRACT = (
    "第一条 合同期限为二年。\n"
    "第二条 试用期六个月，试用期工资按基本工资的百分之六十发放。\n"
    "第三条 月工资为人民币 6000 元，甲方可延迟至次月月底发放上月工资。\n"
    "第四条 乙方离职后三年内不得从事同行业工作，甲方无需支付竞业限制补偿。\n"
)


def _post_check(client: TestClient, state: dict, **payload) -> dict:
    response = client.post(
        "/api/checks",
        json={**payload},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) >= {"job_id", "kind", "status", "task_id", "result", "error"}
    assert body["job_id"].startswith("J-")
    assert body["status"] in {"queued", "running"}
    assert body["task_id"] is None and body["result"] is None and body["error"] is None
    return body


def test_check_job_flow_does_not_persist_first_call(api, monkeypatch) -> None:
    """首轮核对：任务只在内存，合同入库；轮询拿到的结果就是结果页 DTO。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)

    job = _post_check(api.client, state, text=CONTRACT, contract_name="演示合同")
    done = wait_job(api.client, job["job_id"])

    assert done["status"] == "succeeded", done
    assert done["kind"] == "check"
    result = done["result"]
    assert result is not None
    task_id = result["task"]["task_id"]
    assert done["task_id"] == task_id
    assert result["task"]["contract_name"] == "演示合同"
    assert result["verdict"]["status"] in {"risk", "partial", "clean"}
    assert result["verdict"]["process_details"] == []
    assert result["process"] is None
    assert result["disclaimer"]
    assert result["workflow"]["state"] in {"ready", "awaiting_facts"}
    assert result["findings"], "这份合同至少有规则命中的条目"

    # 首轮 save=False：任务不落库，合同按 `_check_form` 存档
    assert count_rows(api.db_path, "tasks") == 0
    assert count_rows(api.db_path, "contracts") == 1

    detail = api.client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["task"]["task_id"] == task_id
    assert api.client.get("/api/history").json()["total_all"] == 0


def test_answers_skip_persists_and_shows_in_history(api, monkeypatch) -> None:
    """跳过追问 = 把首轮结果补存：任务落库、历史可见、还能继续读详情。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    task_id = wait_job(
        api.client, _post_check(api.client, state, text=CONTRACT, contract_name="演示合同")["job_id"]
    )["result"]["task"]["task_id"]

    response = api.client.post(
        f"/api/tasks/{task_id}/answers",
        json={"answers": {}, "skip": True},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    skipped = wait_job(api.client, response.json()["job_id"])
    assert skipped["status"] == "succeeded", skipped
    assert skipped["kind"] == "recheck"
    assert skipped["task_id"] == task_id

    assert count_rows(api.db_path, "tasks") == 1
    history = api.client.get("/api/history").json()
    assert history["total_all"] == 1
    assert history["items"][0]["task_id"] == task_id
    assert api.client.get(f"/api/tasks/{task_id}").status_code == 200


def test_optional_facts_rejects_unknown_field(api, monkeypatch) -> None:
    """可选补充只接受本任务返回过的字段；越界字段 400，不重跑。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    task_id = wait_job(
        api.client, _post_check(api.client, state, text=CONTRACT, contract_name="演示合同")["job_id"]
    )["result"]["task"]["task_id"]

    detail = api.client.get(f"/api/tasks/{task_id}").json()
    allowed = [field["field_id"] for field in detail["workflow"]["optional_fields"]]

    response = api.client.post(
        f"/api/tasks/{task_id}/optional-facts",
        json={"facts": {"definitely_not_a_field": "x"}},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "definitely_not_a_field" in error["details"]["fields"]
    assert count_rows(api.db_path, "tasks") == 0

    if allowed:
        accepted = api.client.post(
            f"/api/tasks/{task_id}/optional-facts",
            json={"facts": {allowed[0]: "是"}},
            headers={"X-CSRF-Token": csrf_of(state)},
        )
        assert accepted.status_code == 202, accepted.text
        assert wait_job(api.client, accepted.json()["job_id"])["status"] == "succeeded"


def test_qa_job_refusal_is_a_result(api, monkeypatch) -> None:
    """问答：越界是成功结果里的 `refusal`，不是 HTTP 异常；轮询结果带 parent_task_id。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    task_id = wait_job(
        api.client, _post_check(api.client, state, text=CONTRACT, contract_name="演示合同")["job_id"]
    )["result"]["task"]["task_id"]

    response = api.client.post(
        f"/api/tasks/{task_id}/qa",
        json={"question": "离婚时房产怎么分割"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    job = wait_job(api.client, response.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["kind"] == "qa"
    result = job["result"]
    assert result["parent_task_id"] == task_id
    assert result["refusal"]["layer"] == "terms"
    assert result["process"] is None


def test_job_and_memory_task_ownership(api, monkeypatch) -> None:
    """属主边界：job 只给属主；内存任务给属主或同企业管理员，同企业普通同事 404。"""
    enable_fake_model(monkeypatch)
    setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    enterprise_id = enterprise["enterprise_id"]

    staff = TestClient(api.app)
    colleague = TestClient(api.app)
    try:
        staff_state = add_user_to_enterprise(
            staff,
            api.db_path,
            username="staff",
            enterprise_id=enterprise_id,
            password=USER_PASSWORD,
        )
        add_user_to_enterprise(
            colleague,
            api.db_path,
            username="colleague",
            enterprise_id=enterprise_id,
            password=USER_PASSWORD,
        )
        job = _post_check(staff, staff_state, text=CONTRACT, contract_name="演示合同")
        task_id = wait_job(staff, job["job_id"])["result"]["task"]["task_id"]

        # job 只给属主：同企业管理员也不是属主，看不到
        assert api.client.get(f"/api/jobs/{job['job_id']}").status_code == 404
        assert staff.get(f"/api/jobs/{job['job_id']}").status_code == 200
        # 同企业普通同事读不到尚未落库的内存任务（越权一律 404）
        assert colleague.get(f"/api/tasks/{task_id}").status_code == 404
        # 属主与同企业管理员可读
        assert staff.get(f"/api/tasks/{task_id}").status_code == 200
        assert api.client.get("/api/auth/me").json()["user"]["role"] == "管理员"
        assert api.client.get(f"/api/tasks/{task_id}").status_code == 200
    finally:
        staff.close()
        colleague.close()


def test_check_input_validation(api, monkeypatch) -> None:
    """正文与已存合同二选一；缺企业档案 409。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    headers = {"X-CSRF-Token": csrf_of(state)}

    empty = api.client.post("/api/checks", json={}, headers=headers)
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "VALIDATION_ERROR"
    both = api.client.post(
        "/api/checks",
        json={"text": CONTRACT, "contract_id": "C-1"},
        headers=headers,
    )
    assert both.status_code == 400
    assert set(both.json()["error"]["details"]["fields"]) == {"text", "contract_id"}
    assert count_rows(api.db_path, "tasks") == 0


def test_check_from_saved_contract(api, monkeypatch) -> None:
    """从历史合同核对：正文与合同名取自库里的档案，合同不会重复存档。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    contract_id = seed_contract(
        api.db_path,
        enterprise["enterprise_id"],
        contract_id="C-SAVED",
        filename="已存合同.txt",
        text=CONTRACT,
    )

    job = _post_check(api.client, state, contract_id=contract_id)
    done = wait_job(api.client, job["job_id"])
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["task"]["contract_id"] == contract_id
    assert result["task"]["contract_name"] == "已存合同"
    assert count_rows(api.db_path, "contracts") == 1, "已有合同不重复存档"

    unknown = api.client.post(
        "/api/checks",
        json={"contract_id": "C-MISSING"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert unknown.status_code == 404


def test_job_is_lost_after_restart(api, monkeypatch) -> None:
    """重启语义：job 只存在内存里，新进程（新 `JobStore`）不认识旧 job，已落库历史不受影响。"""
    enable_fake_model(monkeypatch)
    state = setup_admin(api.client)
    job = _post_check(api.client, state, text=CONTRACT, contract_name="演示合同")
    wait_job(api.client, job["job_id"])

    restarted = create_app(db_path=api.db_path)
    client = TestClient(restarted)
    try:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200, login.text
        gone = client.get(f"/api/jobs/{job['job_id']}")
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "NOT_FOUND"
        # 同一份库里的任务仍然可读（历史是恢复来源）
        assert client.get("/api/history").status_code == 200
    finally:
        client.close()
