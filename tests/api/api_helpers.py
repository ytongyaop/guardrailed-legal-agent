"""`tests/api` 的共用小工具：初始化账号、登录、发请求、断言错误体。

刻意不 import 任何界面模块（0.4.0 批次 6 已删除 `src/mla/ui/`）；只依赖 `httpx` 客户端行为。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from mla.adapters.persist import SqliteRepository
from mla.domain.models import ContractRecord

ADMIN_PASSWORD = "secret123"
USER_PASSWORD = "secret456"
ADMIN_ENTERPRISE = "测试企业"


def setup_admin(
    client: TestClient,
    *,
    username: str = "admin",
    password: str = ADMIN_PASSWORD,
    enterprise_name: str = ADMIN_ENTERPRISE,
) -> dict[str, Any]:
    """首次初始化管理员并直接登录，返回 AuthState。"""
    response = client.post(
        "/api/setup/admin",
        json={
            "username": username,
            "password": password,
            "confirm_password": password,
            "enterprise_name": enterprise_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def register(
    client: TestClient,
    *,
    username: str,
    password: str = USER_PASSWORD,
    enterprise_name: str = "",
) -> dict[str, Any]:
    """自助注册（会被直接登录），返回 AuthState。"""
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "enterprise_name": enterprise_name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def logout(client: TestClient, csrf_token: str) -> None:
    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 204, response.text


def csrf_of(auth_state: dict[str, Any]) -> str:
    token = auth_state.get("csrf_token")
    assert isinstance(token, str) and token
    return token


def write_headers(csrf_token: str | None) -> dict[str, str]:
    return {} if csrf_token is None else {"X-CSRF-Token": csrf_token}


def send(
    client: TestClient,
    method: str,
    path: str,
    *,
    csrf_token: str | None = None,
    json: Any = None,
) -> Any:
    """按权限矩阵批量探针用：路径统一加 `/api` 前缀，必要时带 CSRF 头。"""
    method_upper = method.upper()
    headers = write_headers(csrf_token)
    if json is None and method_upper not in {"GET", "HEAD", "OPTIONS"}:
        json = {}
    return client.request(method_upper, "/api" + path, json=json, headers=headers)


def error_of(response: Any) -> dict[str, Any]:
    """断言错误体形状并返回 `error` 对象（006 §2）。

    形状检查集中在这里：任何一处漏字段都会在第一个用例里暴露，不用逐条重复断言。
    """
    assert response.status_code >= 400, response.text
    body = response.json()
    assert set(body) == {"error", "request_id"}, body
    assert isinstance(body["request_id"], str) and body["request_id"], body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "action"}, error
    assert isinstance(error["code"], str) and error["code"], error
    assert isinstance(error["message"], str) and error["message"], error
    assert error["details"] is None or isinstance(error["details"], dict), error
    assert error["action"] is None or set(error["action"]) == {"type", "label"}, error
    if response.headers.get("x-request-id"):
        assert response.headers["x-request-id"] == body["request_id"]
    return error


def seed_contract(
    db_path: str,
    enterprise_id: str,
    *,
    contract_id: str = "C-TEST1",
    filename: str = "合同A.txt",
    text: str = "第一条 试用期三个月。第二条 工资按月发放。",
) -> str:
    """直接落库一份合同：批次 2 还没有 /checks，合同只能由仓储写入（批次 3 才自动存档）。"""
    repo = SqliteRepository(db_path=db_path)
    try:
        repo.save_contract(
            ContractRecord(
                contract_id=contract_id,
                enterprise_id=enterprise_id,
                filename=filename,
                text=text,
                uploaded_at="2026-01-05T09:00:00",
            )
        )
    finally:
        repo.close()
    return contract_id
