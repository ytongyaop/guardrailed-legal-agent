"""批次 2 冒烟：`create_app()` 可启动、公开端点可访问、同源与未知路径的边界。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mla.api.app import DEFAULT_HOST, create_app

from .api_helpers import error_of, setup_admin

pytestmark = pytest.mark.no_llm


def test_setup_is_public_and_reports_runtime(api) -> None:
    """`GET /api/setup` 公开可访问，返回运行目录引导结果与「是否需要初始化」。"""
    response = api.client.get("/api/setup")
    assert response.status_code == 200
    body = response.json()
    assert body["needs_admin"] is True
    runtime = body["runtime"]
    assert set(runtime) == {
        "copied_from",
        "db_path",
        "warnings",
        "migration_status",
        "messages",
    }
    assert runtime["db_path"] == api.db_path
    assert runtime["copied_from"] is None
    assert isinstance(runtime["warnings"], list)
    assert response.headers["X-Request-ID"].startswith("req_")


def test_setup_flips_after_first_admin(api) -> None:
    setup_admin(api.client)
    assert api.client.get("/api/setup").json()["needs_admin"] is False


def test_unknown_api_path_is_json_404(api) -> None:
    error = error_of(api.client.get("/api/nope"))
    assert error["code"] == "NOT_FOUND"


def test_docs_and_openapi_are_disabled(api) -> None:
    """本机 API 不暴露交互文档：减少可探测面，路由清单只由代码与测试维护。"""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.client.get(path).status_code == 404


def test_no_cors_headers(api) -> None:
    """不开放跨域：同源请求也不回 `Access-Control-Allow-*`。"""
    response = api.client.get("/api/setup", headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert not [header for header in response.headers if header.lower().startswith("access-control-")]


def test_cross_origin_request_is_rejected(api) -> None:
    """带 Origin 的请求必须同源：公开端点同样拒绝跨站（这是公开写操作唯一防线）。"""
    response = api.client.get("/api/setup", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert error_of(response)["code"] == "CSRF_FAILED"


def test_default_host_is_loopback() -> None:
    assert DEFAULT_HOST == "127.0.0.1"


def test_repository_is_per_request_and_closed(api, monkeypatch) -> None:
    """约束 1：每个请求一个仓储实例，响应结束后关闭；两个请求不复用同一连接。"""
    from mla.adapters import persist

    opened: list[object] = []
    closed: list[object] = []
    original_init = persist.SqliteRepository.__init__
    original_close = persist.SqliteRepository.close

    def counting_init(self, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    def counting_close(self) -> None:
        original_close(self)
        closed.append(self)

    monkeypatch.setattr(persist.SqliteRepository, "__init__", counting_init)
    monkeypatch.setattr(persist.SqliteRepository, "close", counting_close)

    assert api.client.get("/api/setup").status_code == 200
    assert api.client.get("/api/auth/me").status_code == 401

    assert len(opened) == 2, "每个请求必须新建一个仓储实例"
    assert closed == opened, "每个请求结束后都必须关闭自己的连接"


def test_bootstrap_runs_once_per_app(monkeypatch) -> None:
    """约束 2：`ensure_runtime_ready()` 在应用启动时执行一次，之后请求不再引导。"""
    from mla.api import deps

    calls: list[int] = []
    original = deps.ensure_runtime_ready

    def counting() -> object:
        calls.append(1)
        return original()

    monkeypatch.setattr(deps, "ensure_runtime_ready", counting)
    app = create_app()
    assert len(calls) == 1

    client = TestClient(app)
    try:
        client.get("/api/setup")
        client.get("/api/auth/me")
    finally:
        client.close()
    assert len(calls) == 1
