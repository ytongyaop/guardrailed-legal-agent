"""错误体（规格 006 §2）：状态码 ↔ code 对应、形状、`request_id`。

每个用例只关心"这个错误走的是哪个码"，形状检查统一在 `api_helpers.error_of` 里。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mla.adapters.llm import ProbeResult
from mla.api.routes import contracts as contracts_routes
from mla.api.routes import settings as settings_routes

from .api_helpers import ADMIN_PASSWORD, csrf_of, error_of, register, setup_admin

pytestmark = pytest.mark.no_llm


def test_unauthenticated_is_401(api) -> None:
    error = error_of(api.client.get("/api/auth/me"))
    assert error["code"] == "UNAUTHENTICATED"
    assert error["details"] is None
    assert error["action"] is None


def test_csrf_failed_is_403(api) -> None:
    setup_admin(api.client)
    error = error_of(api.client.put("/api/settings/ui", json={"show_query_path": True}))
    assert error["code"] == "CSRF_FAILED"


def test_forbidden_is_403(api) -> None:
    setup_admin(api.client)
    register(api.client, username="staff")
    error = error_of(api.client.get("/api/admin/users"))
    assert error["code"] == "FORBIDDEN"
    assert error["action"] == {"type": "contact_admin", "label": "联系管理员"}


def test_not_found_is_404(api) -> None:
    assert error_of(api.client.get("/api/nope"))["code"] == "NOT_FOUND"


def test_wrong_method_is_404_not_405(api) -> None:
    """方法不对收敛成 404：不暴露"路径存在但方法不匹配"。"""
    response = api.client.request("PATCH", "/api/setup", json={})
    assert response.status_code == 404
    assert error_of(response)["code"] == "NOT_FOUND"


def test_validation_error_is_400_with_fields(api) -> None:
    setup_admin(api.client)
    response = api.client.post("/api/auth/login", json={"username": "admin"})
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == ["password"]


def test_state_conflict_is_409(api) -> None:
    setup_admin(api.client)
    response = api.client.post(
        "/api/setup/admin",
        json={
            "username": "another",
            "password": ADMIN_PASSWORD,
            "confirm_password": ADMIN_PASSWORD,
            "enterprise_name": "",
        },
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "STATE_CONFLICT"


def test_payload_too_large_is_413(api, monkeypatch) -> None:
    state = setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    assert enterprise is not None
    monkeypatch.setattr(contracts_routes, "MAX_UPLOAD_BYTES", 1024)
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("大.txt", b"x" * 4096, "text/plain")},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 413
    assert error_of(response)["code"] == "PAYLOAD_TOO_LARGE"


def test_unsupported_file_type_is_415(api) -> None:
    state = setup_admin(api.client)
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("x.rtf", b"{\\rtf1}", "application/rtf")},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 415
    assert error_of(response)["code"] == "UNSUPPORTED_FILE_TYPE"


def test_file_parse_error_is_422(api) -> None:
    state = setup_admin(api.client)
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("坏.docx", b"not-a-zip", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "FILE_PARSE_ERROR"


def test_model_key_invalid_is_422(api, monkeypatch) -> None:
    state = setup_admin(api.client)
    monkeypatch.setattr(settings_routes, "probe_key", lambda key: ProbeResult(kind="auth"))
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": "sk-dummy-x"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "MODEL_KEY_INVALID"


def test_model_probe_failed_is_502(api, monkeypatch) -> None:
    state = setup_admin(api.client)
    monkeypatch.setattr(settings_routes, "probe_key", lambda key: ProbeResult(kind="server"))
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": "sk-dummy-x"},
        headers={"X-CSRF-Token": csrf_of(state)},
    )
    assert response.status_code == 502
    assert error_of(response)["code"] == "MODEL_PROBE_FAILED"


def test_internal_error_is_500_without_leaking_detail(api, monkeypatch) -> None:
    """未捕获异常：固定文案 + `INTERNAL_ERROR`，不回显异常文本。"""
    state = setup_admin(api.client)
    assert state["csrf_token"]

    def boom() -> None:
        raise RuntimeError("内部路径 C:\\secret\\db.sqlite3 崩了")

    monkeypatch.setattr(settings_routes, "_model_view", boom)
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.get(
        "/api/settings/model", cookies={"mla_session": api.client.cookies.get("mla_session")}
    )
    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "INTERNAL_ERROR"
    assert "secret" not in response.text
    assert error["action"]["type"] == "retry"


def test_request_ids_are_unique_per_request(api) -> None:
    """成功响应只在头里带 `request_id`；错误体里额外放在响应体。"""
    first = api.client.get("/api/setup")
    second = api.client.get("/api/setup")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    missing = api.client.get("/api/nope")
    assert missing.json()["request_id"] == missing.headers["X-Request-ID"]
