"""权限矩阵（规格 006 §2 + `tests/api/fixtures/permissions.json`）。

三层断言：
1. `permissions.py` 的能力表必须与批次 1 冻结的夹具逐字段一致（不能各写一套）；
2. 实际注册的路由必须与能力表一致（漏注册 / 多注册都会暴露）；
3. 逐能力探针：普通用户对管理端一律 403 `FORBIDDEN`，管理员不被 401/403 挡；
   跨企业资源一律 404（不是 403，避免探测他人资源是否存在）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mla.api.permissions import CAPABILITIES, capability

from .api_helpers import csrf_of, error_of, register, seed_contract, send, setup_admin

HERE = Path(__file__).resolve().parent
MATRIX: dict[str, Any] = json.loads((HERE / "fixtures" / "permissions.json").read_text(encoding="utf-8"))

# 路径参数统一填一个不存在的值：本批次只验证"到不到得了处理器"，不验证对象存在性
PATH_PARAMS: dict[str, str] = {
    "{provider}": "deepseek",
    "{user_id}": "U-missing",
    "{task_id}": "T-missing",
    "{job_id}": "J-missing",
    "{contract_id}": "C-missing",
}

pytestmark = pytest.mark.no_llm


def _fill(path: str) -> str:
    for placeholder, value in PATH_PARAMS.items():
        path = path.replace(placeholder, value)
    return path


def _matrix_capabilities() -> set[str]:
    return set(MATRIX["endpoints"])


def _flatten_routes(app: Any) -> list[Any]:
    """展开 `app.routes`：本版 FastAPI 的 `include_router` 会包一层 `_IncludedRouter`。

    只做测试内省用，不改生产代码；若将来 FastAPI 把路由拍平，`effective_route_contexts`
    不存在时退化为直接读 `app.routes`。
    """
    routes: list[Any] = []
    for route in app.routes:
        expand: Any = getattr(route, "effective_route_contexts", None)
        if expand is None:
            routes.append(route)
        else:
            routes.extend(list(expand()))
    return routes


def test_capability_table_matches_frozen_fixture() -> None:
    """能力表与批次 1 冻结的权限矩阵必须完全一致。"""
    assert set(CAPABILITIES) == _matrix_capabilities()
    for name, rows in MATRIX["endpoints"].items():
        expected = tuple(tuple(row.split(" ", 1)) for row in rows)  # type: ignore[misc]
        assert capability(name).endpoints == expected, name

    assert {name for name, cap in CAPABILITIES.items() if cap.anonymous} == set(MATRIX["anonymous"])
    assert {name for name, cap in CAPABILITIES.items() if cap.admin} == set(MATRIX["forbidden_for_user"])
    logged_in_users = {name for name, cap in CAPABILITIES.items() if not cap.anonymous and not cap.admin}
    assert logged_in_users == set(MATRIX["user"])


def test_registered_routes_match_matrix(api) -> None:
    """实际注册的路由集合 == 能力表端点集合（含批次 3 的 501 占位）。"""
    registered: set[tuple[str, str]] = set()
    for route in _flatten_routes(api.app):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/api"):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            registered.add((method, path[len("/api") :]))
    expected = {endpoint for cap in CAPABILITIES.values() for endpoint in cap.endpoints}
    assert registered == expected, {
        "缺少": sorted(expected - registered),
        "多出": sorted(registered - expected),
    }


@pytest.fixture
def actors(api):
    """管理员客户端 + 普通用户客户端（各自独立 cookie jar，同一进程内会话表）。"""
    admin_state = setup_admin(api.client)
    user_client = TestClient(api.app)
    user_state = register(user_client, username="staff")
    try:
        yield {
            "admin": (api.client, admin_state),
            "user": (user_client, user_state),
        }
    finally:
        user_client.close()


@pytest.mark.parametrize("cap_name", MATRIX["forbidden_for_user"])
def test_user_is_forbidden_on_admin_capabilities(cap_name: str, actors) -> None:
    """普通用户访问管理端：每一项端点都必须是 403 `FORBIDDEN`。"""
    client, state = actors["user"]
    for method, path in capability(cap_name).endpoints:
        response = send(client, method, _fill(path), csrf_token=csrf_of(state))
        assert response.status_code == 403, (cap_name, method, path, response.text)
        assert error_of(response)["code"] == "FORBIDDEN"


@pytest.mark.parametrize("cap_name", MATRIX["forbidden_for_user"])
def test_admin_is_not_blocked_on_admin_capabilities(cap_name: str, actors) -> None:
    """管理员不会被鉴权挡住（可能是 200/400/404/409/501，但绝不能是 401/403）。"""
    client, state = actors["admin"]
    for method, path in capability(cap_name).endpoints:
        response = send(client, method, _fill(path), csrf_token=csrf_of(state))
        assert response.status_code not in (401, 403), (cap_name, method, path, response.text)


@pytest.mark.parametrize("cap_name", MATRIX["user"])
def test_user_capabilities_are_reachable(cap_name: str, actors) -> None:
    """普通用户对自己的能力不被 401/403 挡住（业务错误照常返回）。"""
    client, state = actors["user"]
    for method, path in capability(cap_name).endpoints:
        response = send(client, method, _fill(path), csrf_token=csrf_of(state))
        assert response.status_code not in (401, 403), (cap_name, method, path, response.text)


@pytest.mark.parametrize("cap_name", MATRIX["anonymous"])
def test_anonymous_capabilities_need_no_session(cap_name: str, api) -> None:
    """公开端点未登录也可达（不是 401/403 挡在最前面）。"""
    for method, path in capability(cap_name).endpoints:
        response = send(api.client, method, _fill(path))
        assert response.status_code not in (401, 403), (cap_name, method, path, response.text)


def test_user_capabilities_need_login(api) -> None:
    """未登录访问登录态能力：401 `UNAUTHENTICATED`。"""
    for method, path in capability("contracts").endpoints:
        response = send(api.client, method, _fill(path))
        assert response.status_code == 401, (method, path, response.text)
        assert error_of(response)["code"] == "UNAUTHENTICATED"


def test_cross_enterprise_contract_is_404(actors, api) -> None:
    """跨企业访问合同：读、删都是 404；管理员自己仍能读到。"""
    admin_client, admin_state = actors["admin"]
    user_client, user_state = actors["user"]

    enterprise = admin_client.get("/api/enterprise").json()
    assert enterprise is not None
    contract_id = seed_contract(api.db_path, enterprise["enterprise_id"])

    for method in ("GET", "DELETE"):
        response = send(user_client, method, f"/contracts/{contract_id}", csrf_token=csrf_of(user_state))
        assert response.status_code == 404, (method, response.text)
        assert error_of(response)["code"] == "NOT_FOUND"

    still_there = send(admin_client, "GET", f"/contracts/{contract_id}", csrf_token=csrf_of(admin_state))
    assert still_there.status_code == 200
    assert contract_id == still_there.json()["contract_id"]


def test_enterprise_is_scoped_to_current_user(actors) -> None:
    """普通用户看不到管理员的企业档案，自己的档案是独立的 `null`。"""
    admin_client, _admin_state = actors["admin"]
    user_client, _user_state = actors["user"]
    admin_ent = admin_client.get("/api/enterprise").json()
    assert admin_ent is not None
    assert user_client.get("/api/enterprise").json() is None
