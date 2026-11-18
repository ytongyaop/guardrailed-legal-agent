"""认证与会话（规格 006 §4）：初始化 → 登录 → `/me` → 登出、错误口令、过期、改密、引导。

会话过期用可注入时钟推进，不做真实等待。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from .api_helpers import (
    ADMIN_PASSWORD,
    csrf_of,
    error_of,
    login,
    logout,
    register,
    setup_admin,
    write_headers,
)

pytestmark = pytest.mark.no_llm


def test_setup_admin_login_me_logout(api) -> None:
    """初始化管理员 → 登录 → `/me` → 登出 的完整往返，顺带核对 cookie 属性。"""
    first = setup_admin(api.client)
    assert first["user"]["role"] == "管理员"
    assert first["model_configured"] is False

    login_response = api.client.post(
        "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert login_response.status_code == 200
    lowered = login_response.headers["set-cookie"].lower()
    assert "mla_session=" in lowered
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "secure" not in lowered  # 本地 HTTP 默认不加 Secure

    second = login_response.json()
    assert second["csrf_token"] != first["csrf_token"]  # 登录轮换 CSRF token

    me = api.client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"
    assert me.json()["csrf_token"] == csrf_of(second)

    logout(api.client, csrf_of(second))
    assert error_of(api.client.get("/api/auth/me"))["code"] == "UNAUTHENTICATED"


def test_login_uses_new_session_and_csrf_each_time(api) -> None:
    """每次登录轮换会话 ID 与 CSRF token（不复用旧值）。"""
    first = setup_admin(api.client)
    second = login(api.client, "admin", ADMIN_PASSWORD)
    assert first["csrf_token"] != second["csrf_token"]


def test_wrong_password_is_401(api) -> None:
    setup_admin(api.client)
    response = api.client.post("/api/auth/login", json={"username": "admin", "password": "wrong-one"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "UNAUTHENTICATED"


def test_unknown_username_is_same_401(api) -> None:
    setup_admin(api.client)
    response = api.client.post("/api/auth/login", json={"username": "ghost", "password": "whatever1"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "UNAUTHENTICATED"


def test_setup_admin_twice_is_409(api) -> None:
    setup_admin(api.client)
    response = api.client.post(
        "/api/setup/admin",
        json={
            "username": "admin2",
            "password": ADMIN_PASSWORD,
            "confirm_password": ADMIN_PASSWORD,
            "enterprise_name": "",
        },
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "STATE_CONFLICT"


def test_setup_admin_password_mismatch_is_400(api) -> None:
    response = api.client.post(
        "/api/setup/admin",
        json={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "confirm_password": "different1",
            "enterprise_name": "",
        },
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_register_creates_plain_user(api) -> None:
    setup_admin(api.client)
    state = register(api.client, username="staff")
    assert state["user"]["role"] == "用户"
    assert state["user"]["enterprise_id"] == ""
    assert state["model_configured"] is False


def test_idle_expiry(api, clock) -> None:
    """空闲超过 30 分钟即失效。"""
    state = setup_admin(api.client)
    assert api.client.get("/api/auth/me").status_code == 200
    clock.advance(timedelta(minutes=30, seconds=1))
    assert error_of(api.client.get("/api/auth/me"))["code"] == "UNAUTHENTICATED"
    assert state["csrf_token"]


def test_absolute_expiry_even_with_activity(api, clock) -> None:
    """绝对过期 12 小时：中间一直有请求也挡不住。"""
    setup_admin(api.client)
    for _ in range(23):  # 11.5 小时，每半小时触达一次，空闲从未超时
        clock.advance(timedelta(minutes=30))
        assert api.client.get("/api/auth/me").status_code == 200
    clock.advance(timedelta(hours=1))  # 累计 12.5 小时
    assert error_of(api.client.get("/api/auth/me"))["code"] == "UNAUTHENTICATED"


def test_short_timeouts_can_be_injected(make_api) -> None:
    """过期参数可注入：1 秒空闲即失效，用来证明不是靠真实等待。"""
    api = make_api(idle_timeout=timedelta(seconds=1))
    setup_admin(api.client)
    api.clock.advance(timedelta(seconds=2))
    assert api.client.get("/api/auth/me").status_code == 401


def test_logout_revokes_server_side_session(api) -> None:
    """登出后同一个 cookie 不能再用（服务端删会话，而不是只清浏览器 cookie）。"""
    state = setup_admin(api.client)
    cookie = api.client.cookies.get("mla_session")
    assert cookie
    logout(api.client, csrf_of(state))
    replay = api.client.get("/api/auth/me", cookies={"mla_session": cookie})
    assert replay.status_code == 401


def test_password_change_invalidates_other_sessions(api) -> None:
    """改密后：当前会话保留，其他会话立即失效，旧口令不能登录。"""
    setup_admin(api.client)
    other = TestClient(api.app)
    other_state = login(other, "admin", ADMIN_PASSWORD)
    try:
        current = api.client.get("/api/auth/me").json()
        response = api.client.put(
            "/api/auth/password",
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "brandnew123",
                "confirm_password": "brandnew123",
            },
            headers=write_headers(csrf_of(current)),
        )
        assert response.status_code == 204, response.text

        assert other.get("/api/auth/me").status_code == 401, other_state
        assert api.client.get("/api/auth/me").status_code == 200

        assert (
            api.client.post(
                "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
            ).status_code
            == 401
        )
        assert (
            api.client.post(
                "/api/auth/login", json={"username": "admin", "password": "brandnew123"}
            ).status_code
            == 200
        )
    finally:
        other.close()


def test_password_change_validation(api) -> None:
    state = setup_admin(api.client)
    response = api.client.put(
        "/api/auth/password",
        json={
            "current_password": ADMIN_PASSWORD,
            "new_password": "brandnew123",
            "confirm_password": "mismatch123",
        },
        headers=write_headers(csrf_of(state)),
    )
    assert response.status_code == 400
    assert error_of(response)["details"] == {"fields": ["confirm_password"]}

    wrong_old = api.client.put(
        "/api/auth/password",
        json={
            "current_password": "not-the-password",
            "new_password": "brandnew123",
            "confirm_password": "brandnew123",
        },
        headers=write_headers(csrf_of(state)),
    )
    assert wrong_old.status_code == 400
    assert error_of(wrong_old)["code"] == "VALIDATION_ERROR"


def test_onboarding_marks_user(api) -> None:
    state = setup_admin(api.client)
    assert state["user"]["onboarded"] is False
    response = api.client.post("/api/auth/onboarding", headers=write_headers(csrf_of(state)))
    assert response.status_code == 200
    assert response.json()["onboarded"] is True
    assert api.client.get("/api/auth/me").json()["user"]["onboarded"] is True


def test_responses_never_contain_credentials(api) -> None:
    """响应体里不得出现口令、哈希、盐的字段名或值。"""
    state = setup_admin(api.client)
    bodies = [
        api.client.get("/api/auth/me").text,
        api.client.get("/api/admin/users").text,
        repr(state),
    ]
    for text in bodies:
        assert ADMIN_PASSWORD not in text
        assert "password_hash" not in text
        assert '"salt"' not in text
