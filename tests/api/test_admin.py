"""管理端（规格 006 §2）：用户列表 / 改角色 / 删号 / 法源 / 规则 / 日志 / 评测。

全部要求管理员；普通用户的 403 与越权 404 见 `test_permissions.py`。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mla.adapters.persist import SqliteRepository
from mla.domain.models import OperationLog

from .api_helpers import csrf_of, error_of, register, setup_admin

pytestmark = pytest.mark.no_llm


@pytest.fixture
def admin(api) -> str:
    return csrf_of(setup_admin(api.client))


@pytest.fixture
def staff(api):
    """独立的普通用户客户端：不覆盖管理员的 cookie。"""
    client = TestClient(api.app)
    state = register(client, username="staff")
    try:
        yield client, state
    finally:
        client.close()


def test_user_list_has_no_secrets(api, admin, staff) -> None:
    response = api.client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()
    assert [user["username"] for user in users] == ["admin", "staff"]
    for user in users:
        assert set(user) == {
            "user_id",
            "username",
            "role",
            "enterprise_id",
            "created_at",
            "onboarded",
        }
    assert "password_hash" not in response.text
    assert '"salt"' not in response.text


def test_role_change_takes_effect_immediately(api, admin, staff) -> None:
    """提升后同一会话立刻能进管理端；降级后又立刻 403（每次请求重读角色）。"""
    client, _state = staff
    user_id = next(
        user["user_id"] for user in api.client.get("/api/admin/users").json() if user["username"] == "staff"
    )
    assert client.get("/api/admin/users").status_code == 403

    promoted = api.client.patch(
        f"/api/admin/users/{user_id}/role", json={"role": "管理员"}, headers={"X-CSRF-Token": admin}
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "管理员"
    assert client.get("/api/admin/users").status_code == 200

    demoted = api.client.patch(
        f"/api/admin/users/{user_id}/role", json={"role": "用户"}, headers={"X-CSRF-Token": admin}
    )
    assert demoted.json()["role"] == "用户"
    assert client.get("/api/admin/users").status_code == 403


def test_invalid_role_is_400(api, admin, staff) -> None:
    user_id = next(
        user["user_id"] for user in api.client.get("/api/admin/users").json() if user["username"] == "staff"
    )
    response = api.client.patch(
        f"/api/admin/users/{user_id}/role",
        json={"role": "超级用户"},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"fields": ["role"]}


def test_unknown_user_role_patch_is_404(api, admin) -> None:
    response = api.client.patch(
        "/api/admin/users/U-missing/role", json={"role": "用户"}, headers={"X-CSRF-Token": admin}
    )
    assert response.status_code == 404


def test_delete_user_requires_matching_confirm(api, admin, staff) -> None:
    client, _state = staff
    user_id = next(
        user["user_id"] for user in api.client.get("/api/admin/users").json() if user["username"] == "staff"
    )
    wrong = api.client.request(
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"confirm_username": "someone-else"},
        headers={"X-CSRF-Token": admin},
    )
    assert wrong.status_code == 400
    assert error_of(wrong)["details"] == {"fields": ["confirm_username"]}

    deleted = api.client.request(
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"confirm_username": "staff"},
        headers={"X-CSRF-Token": admin},
    )
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["deleted"] is True
    assert body["username"] == "staff"
    assert (body["contracts"], body["tasks"], body["logs"]) == (0, 0, 0)

    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post("/api/auth/login", json={"username": "staff", "password": "secret456"}).status_code == 401
    )
    assert [user["username"] for user in api.client.get("/api/admin/users").json()] == ["admin"]


def test_delete_self_is_state_conflict(api, admin) -> None:
    admin_id = api.client.get("/api/auth/me").json()["user"]["user_id"]
    response = api.client.request(
        "DELETE",
        f"/api/admin/users/{admin_id}",
        json={"confirm_username": "admin"},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "STATE_CONFLICT"


def test_delete_writes_audit_log(api, admin, staff) -> None:
    user_id = next(
        user["user_id"] for user in api.client.get("/api/admin/users").json() if user["username"] == "staff"
    )
    api.client.request(
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"confirm_username": "staff"},
        headers={"X-CSRF-Token": admin},
    )
    logs = api.client.get("/api/admin/logs").json()
    assert any(row["action"] == "删除账号" for row in logs["items"])
    assert logs["total_all"] >= 1


def test_logs_filters_and_csv_export(api, admin) -> None:
    repo = SqliteRepository(db_path=api.db_path)
    try:
        repo.add_log(
            OperationLog(
                log_id="L-TEST1",
                task_id="T-1",
                user_id="U-1",
                action="核对",
                detail="示例详情",
                created_at="2026-01-05T09:00:00",
            )
        )
    finally:
        repo.close()

    filtered = api.client.get("/api/admin/logs", params={"action": "核对", "q": "示例"})
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["total"] == 1
    assert body["items"][0]["log_id"] == "L-TEST1"

    empty = api.client.get("/api/admin/logs", params={"action": "问答"})
    assert empty.json()["total"] == 0

    export = api.client.get("/api/admin/logs/export", params={"action": "核对"})
    assert export.status_code == 200
    assert export.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = export.content.decode("utf-8-sig")
    assert text.splitlines()[0] == "时间,用户,动作,任务,详情"
    assert "示例详情" in text


def test_statutes_and_rules_are_readable(api, admin) -> None:
    statutes = api.client.get("/api/admin/statutes", params={"page_size": 5})
    assert statutes.status_code == 200
    body = statutes.json()
    assert body["total_all"] > 0
    assert len(body["items"]) == 5
    assert set(body["items"][0]) >= {"statute_id", "law_name", "article_no", "text"}

    hit = api.client.get("/api/admin/statutes", params={"q": "试用期"})
    assert hit.json()["total"] >= 1

    rules = api.client.get("/api/admin/rules").json()
    assert rules["rules"], "规则表不应为空"
    assert rules["slots"], "必问事实清单不应为空"
    assert set(rules["rules"][0]) >= {"rule_id", "field", "op", "threshold", "statute_id"}
    assert rules["slots"][0]["type"] in {"阻断项", "参考项"}


def test_evaluation_is_read_only_view_of_files(api, admin) -> None:
    response = api.client.get("/api/admin/evaluation")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["variants"], "评测文件里应至少有一个消融档位"
    assert set(body["variants"][0]) == {"variant", "metrics", "mismatches"}
