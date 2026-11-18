"""CSRF（规格 006 §4）：登录态写操作必须带 `X-CSRF-Token`，公开写操作只需同源。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .api_helpers import csrf_of, error_of, login, send, setup_admin, write_headers

pytestmark = pytest.mark.no_llm


def test_write_without_token_is_403(api) -> None:
    setup_admin(api.client)
    response = api.client.put("/api/settings/ui", json={"show_query_path": True})
    assert response.status_code == 403
    assert error_of(response)["code"] == "CSRF_FAILED"


def test_write_with_wrong_token_is_403(api) -> None:
    setup_admin(api.client)
    response = api.client.put(
        "/api/settings/ui",
        json={"show_query_path": True},
        headers=write_headers("not-the-real-token"),
    )
    assert response.status_code == 403
    assert error_of(response)["code"] == "CSRF_FAILED"


def test_write_with_empty_token_is_403(api) -> None:
    setup_admin(api.client)
    response = api.client.put("/api/settings/ui", json={"show_query_path": True}, headers=write_headers(""))
    assert response.status_code == 403
    assert error_of(response)["code"] == "CSRF_FAILED"


def test_write_with_correct_token_succeeds(api) -> None:
    state = setup_admin(api.client)
    response = api.client.put(
        "/api/settings/ui",
        json={"show_query_path": True},
        headers=write_headers(csrf_of(state)),
    )
    assert response.status_code == 200
    assert response.json() == {"show_query_path": True}


def test_read_does_not_need_token(api) -> None:
    setup_admin(api.client)
    assert api.client.get("/api/settings/ui").status_code == 200


def test_token_is_bound_to_session(api) -> None:
    """一个会话的 token 不能给另一个会话用（token 存在服务端会话里）。"""
    setup_admin(api.client)
    other = TestClient(api.app)
    try:
        other_state = login(other, "admin", "secret123")
        response = api.client.put(
            "/api/settings/ui",
            json={"show_query_path": True},
            headers=write_headers(csrf_of(other_state)),
        )
        assert response.status_code == 403
        assert error_of(response)["code"] == "CSRF_FAILED"
    finally:
        other.close()


def test_public_write_needs_no_token_but_same_origin(api) -> None:
    """公开写操作（登录）不校验 token，但跨站 Origin 一律拒绝。"""
    setup_admin(api.client)
    ok = api.client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    assert ok.status_code == 200

    cross_site = api.client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret123"},
        headers={"Origin": "http://evil.example"},
    )
    assert cross_site.status_code == 403
    assert error_of(cross_site)["code"] == "CSRF_FAILED"


def test_public_write_same_origin_passes(api) -> None:
    setup_admin(api.client)
    response = api.client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret123"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200


def test_stub_write_also_requires_csrf(api) -> None:
    """批次 3 的占位端点同样先过 CSRF 与鉴权，不会因为"还没实现"而放行。"""
    setup_admin(api.client)
    response = send(api.client, "POST", "/tasks/T-1/qa", json={"question": "?"})
    assert response.status_code == 403
    assert error_of(response)["code"] == "CSRF_FAILED"
