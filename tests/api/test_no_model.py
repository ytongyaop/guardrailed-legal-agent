"""无模型门禁（规格 006 AC-04 / 错误约定）：409 `NO_MODEL`，且一个残缺任务都不许落库。

三条路径各有断言：

1. **入队前**：`POST /checks` 直接 409，文案用 `services.tasks.NO_MODEL_MESSAGE`，
   管理员动作指向设置页、普通用户指向联系管理员；tasks / contracts 表都是空的。
2. **worker 内**：预检放行但服务层门禁仍然拦（模拟"入队后 Key 被清掉"），job 以
   `status=failed` + `error.code=NO_MODEL` 结束，同样没有任务落库。
3. **问答**：范围内问题要模型 → 409 `NO_MODEL_QA_MESSAGE`；越界问题由纯规则拒答 →
   202 + 成功结果里的 `refusal`（拒答不是 HTTP 异常）。
"""

from __future__ import annotations

import pytest

from mla.api.jobs import TaskSession
from mla.api.schemas import QaIn
from mla.domain.models import CheckTask, TaskMode
from mla.services.tasks import NO_MODEL_MESSAGE, NO_MODEL_QA_MESSAGE

from .api_helpers import csrf_of, register, setup_admin
from .flow_helpers import count_rows, wait_job

pytestmark = pytest.mark.no_llm

CONTRACT = "第一条 试用期六个月。第二条 月工资 6000 元，次月月底发放。"


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """无模型环境：根 conftest 的放行开关必须关掉，Key 一并不留。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


def _memory_task() -> CheckTask:
    return CheckTask(
        task_id="T-MEM",
        mode=TaskMode.CHECK,
        enterprise_id="E-TEST",
        contract_id="C-TEST",
        contract_name="演示合同.txt",
        contract_chars=60,
    )


def _remember_session(api, user_id: str) -> None:
    """直接把“当前任务”放进内存登记：问答/追问路由只认内存会话（重启即失）。"""
    api.app.state.jobs.remember(
        TaskSession(
            task=_memory_task(),
            owner_id=user_id,
            contract_text=CONTRACT,
            contract_name="演示合同.txt",
            contract_id="C-TEST",
            clause_review=False,
        )
    )


def test_check_without_model_is_409_and_persists_nothing(api, monkeypatch) -> None:
    """管理员：409 `NO_MODEL` + 去设置页；tasks / contracts 都不许有行。"""
    _no_model(monkeypatch)
    state = setup_admin(api.client)
    response = api.client.post(
        "/api/checks",
        json={"text": CONTRACT, "contract_name": "演示合同"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_MODEL"
    assert error["message"] == NO_MODEL_MESSAGE
    assert error["action"] == {"type": "open_settings", "label": "去设置页配置 API Key"}
    assert count_rows(api.db_path, "tasks") == 0, "无模型时不允许留下任何任务"
    assert count_rows(api.db_path, "contracts") == 0, "无模型时连合同都不该存档"


def test_check_without_model_points_user_to_admin(api, monkeypatch) -> None:
    """普通用户：同一份文案，动作是“联系管理员”而不是设置页（用户看不到模型设置）。"""
    _no_model(monkeypatch)
    setup_admin(api.client)
    registered = register(api.client, username="staff", enterprise_name="另一家")
    response = api.client.post(
        "/api/checks",
        json={"text": CONTRACT},
        headers={"X-CSRF-Token": csrf_of(registered)},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_MODEL"
    assert error["message"] == NO_MODEL_MESSAGE
    assert error["action"] == {"type": "contact_admin", "label": "联系管理员"}
    assert count_rows(api.db_path, "tasks") == 0


def test_worker_failure_maps_to_no_model_without_partial_task(api, monkeypatch) -> None:
    """worker 内门禁：预检被绕过也只会得到 failed job，不会留下半成品任务。"""
    _no_model(monkeypatch)
    state = setup_admin(api.client)
    # 预检放行（等价"入队后 Key 失效"）；服务层 `run_check` 仍然拒绝
    monkeypatch.setattr("mla.api.routes.checks.has_model", lambda: True)
    response = api.client.post(
        "/api/checks",
        json={"text": CONTRACT, "contract_name": "演示合同"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    job = wait_job(api.client, response.json()["job_id"])

    assert job["status"] == "failed"
    assert job["result"] is None
    assert job["task_id"] is None
    assert job["error"]["code"] == "NO_MODEL"
    assert job["error"]["message"] == NO_MODEL_MESSAGE
    assert job["error"]["action"] == {"type": "open_settings", "label": "去设置页配置 API Key"}
    assert count_rows(api.db_path, "tasks") == 0
    assert count_rows(api.db_path, "contracts") == 0


def test_qa_in_scope_without_model_is_409(api, monkeypatch) -> None:
    """范围内问题需要模型作答：入队前就 409，不生成"未配置模型"的残缺问答任务。"""
    _no_model(monkeypatch)
    state = setup_admin(api.client)
    _remember_session(api, state["user"]["user_id"])
    response = api.client.post(
        "/api/tasks/T-MEM/qa",
        json={"question": "试用期最长可以约定多久"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_MODEL"
    assert error["message"] == NO_MODEL_QA_MESSAGE
    assert count_rows(api.db_path, "tasks") == 0


def test_qa_out_of_scope_refuses_without_model(api, monkeypatch) -> None:
    """越界问题由纯规则拒答：202 + 成功结果里的 refusal，不是 409。"""
    _no_model(monkeypatch)
    state = setup_admin(api.client)
    _remember_session(api, state["user"]["user_id"])
    response = api.client.post(
        "/api/tasks/T-MEM/qa",
        json={"question": "离婚时房产怎么分割"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 202, response.text
    job = wait_job(api.client, response.json()["job_id"])

    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["parent_task_id"] == "T-MEM"
    assert result["refusal"] is not None
    assert result["refusal"]["layer"] == "terms"
    assert result["refusal"]["message"].startswith("越界拒答（判定层：terms）")
    assert result["empty_text"] is None
    assert result["process"] is None, "过程开关默认关闭，问答运行详情也不返回"


def test_qa_request_cannot_inject_context(api, monkeypatch) -> None:
    """问答上下文只能由服务端 `_qa_context` 生成：请求体只认 `question`，多余字段 400。"""
    assert set(QaIn.model_fields) == {"question"}
    assert QaIn.model_config.get("extra") == "forbid"

    _no_model(monkeypatch)
    state = setup_admin(api.client)
    _remember_session(api, state["user"]["user_id"])
    response = api.client.post(
        "/api/tasks/T-MEM/qa",
        json={
            "question": "离婚时房产怎么分割",
            "context": {"facts": {"a": "b"}},
            "contract_text": "伪造正文",
        },
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert set(error["details"]["fields"]) == {"context", "contract_text"}
    assert count_rows(api.db_path, "tasks") == 0, "被拒绝的请求不得产生任务"

    # 合法请求体照常入队（与「多余字段被拒绝」成对验证，防止把正常请求也挡掉）
    ok = api.client.post(
        "/api/tasks/T-MEM/qa",
        json={"question": "离婚时房产怎么分割"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert ok.status_code == 202, ok.text
    assert wait_job(api.client, ok.json()["job_id"])["status"] == "succeeded"
