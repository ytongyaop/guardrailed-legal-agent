"""设置（规格 006 §2）：界面开关持久化、模型设置不泄露 Key、`PUT` 走打桩的 `probe_models`。

`PUT /settings/model` 的探针在 `routes/settings.py` 里留了一个 `probe_key` 注入点，
用例打桩它即可完全不联网；真实请求路径仍是 `DeepSeekLLM.probe_models`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mla.adapters.llm import ProbeResult
from mla.api.routes import settings as settings_routes
from mla.config import mla_home

from .api_helpers import csrf_of, error_of, login, register, setup_admin

pytestmark = pytest.mark.no_llm

DUMMY_KEY = "sk-dummy-" + "0" * 26


@pytest.fixture
def admin(api) -> str:
    return csrf_of(setup_admin(api.client))


def _all_keys(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        found = {str(key) for key in payload}
        for value in payload.values():
            found |= _all_keys(value)
        return found
    if isinstance(payload, list):
        found: set[str] = set()
        for item in payload:
            found |= _all_keys(item)
        return found
    return set()


def test_ui_setting_roundtrip_and_persist(api, admin, make_api) -> None:
    """`GET/PUT /settings/ui` 落库；换一个应用实例（重启）仍保持。"""
    assert api.client.get("/api/settings/ui").json() == {"show_query_path": False}

    put = api.client.put("/api/settings/ui", json={"show_query_path": True}, headers={"X-CSRF-Token": admin})
    assert put.status_code == 200
    assert put.json() == {"show_query_path": True}
    assert api.client.get("/api/settings/ui").json() == {"show_query_path": True}

    reopened = make_api(db_path=api.db_path)
    reopened_state = login(reopened.client, "admin", "secret123")
    assert reopened.client.get("/api/settings/ui").json() == {"show_query_path": True}

    off = reopened.client.put(
        "/api/settings/ui",
        json={"show_query_path": False},
        headers={"X-CSRF-Token": csrf_of(reopened_state)},
    )
    assert off.json() == {"show_query_path": False}


def test_illegal_legacy_value_reads_as_off(api) -> None:
    """非法旧值（不是 "1"）一律读成关，且读取不回写。"""
    from mla.adapters.persist import SqliteRepository

    setup_admin(api.client)
    repo = SqliteRepository(db_path=api.db_path)
    try:
        repo.set_setting(settings_routes.SHOW_QUERY_PATH_KEY, "yes")
    finally:
        repo.close()
    assert api.client.get("/api/settings/ui").json() == {"show_query_path": False}
    repo = SqliteRepository(db_path=api.db_path)
    try:
        assert repo.get_setting(settings_routes.SHOW_QUERY_PATH_KEY) == "yes"
    finally:
        repo.close()


def test_model_settings_never_expose_the_key(api, admin) -> None:
    """配置了 Key 之后，响应里既没有 Key 本身，也没有任何 key/length 字段。"""
    from mla.services.credentials import set_key

    set_key("deepseek", DUMMY_KEY)
    response = api.client.get("/api/settings/model")
    assert response.status_code == 200
    body = response.json()
    assert body["providers"][0]["configured"] is True
    assert body["providers"][0]["source"] == "auth_file"
    assert DUMMY_KEY not in response.text
    assert not {key for key in _all_keys(body) if "key" in key.lower()}
    assert body["embedding"]["configured"] is False
    assert "回退 BM25" in body["embedding"]["fallback_text"]


def test_put_model_key_probes_then_saves(api, admin, monkeypatch) -> None:
    calls: list[str] = []

    def fake_probe(key: str) -> ProbeResult:
        calls.append(key)
        return ProbeResult(kind="ok", status_code=200, message="HTTP 200")

    monkeypatch.setattr(settings_routes, "probe_key", fake_probe)
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 200
    body = response.json()
    assert calls == [DUMMY_KEY]
    assert body["providers"][0]["configured"] is True
    assert body["check"] is not None and body["check"]["ok"] is True
    assert DUMMY_KEY not in response.text

    stored = json.loads((mla_home() / "auth.json").read_text(encoding="utf-8"))
    assert stored == {"deepseek": {"type": "api_key", "key": DUMMY_KEY}}


def test_put_model_key_rejected_when_probe_says_auth(api, admin, monkeypatch) -> None:
    monkeypatch.setattr(settings_routes, "probe_key", lambda key: ProbeResult(kind="auth", status_code=401))
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "MODEL_KEY_INVALID"
    assert error["action"]["type"] == "open_settings"
    assert DUMMY_KEY not in response.text
    assert not (mla_home() / "auth.json").exists()


def test_put_model_key_reports_probe_failure_502(api, admin, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_routes, "probe_key", lambda key: ProbeResult(kind="network", message="URLError")
    )
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 502
    assert error_of(response)["code"] == "MODEL_PROBE_FAILED"
    assert not (mla_home() / "auth.json").exists()


def test_put_model_key_requires_non_empty_key(api, admin) -> None:
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "key": "   "},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 400
    assert error_of(response)["details"] == {"fields": ["key"]}


def test_unknown_provider_is_404(api, admin) -> None:
    response = api.client.put(
        "/api/settings/model",
        json={"provider": "openai", "key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 404
    assert error_of(response)["code"] == "NOT_FOUND"


def test_test_connection_without_key_is_409(api, admin) -> None:
    response = api.client.post("/api/settings/model/deepseek/test", headers={"X-CSRF-Token": admin})
    assert response.status_code == 409
    assert error_of(response)["code"] == "STATE_CONFLICT"


def test_test_connection_records_status(api, admin, monkeypatch) -> None:
    from mla.services.credentials import set_key

    set_key("deepseek", DUMMY_KEY)
    monkeypatch.setattr(settings_routes, "probe_key", lambda key: ProbeResult(kind="ok", status_code=200))
    response = api.client.post("/api/settings/model/deepseek/test", headers={"X-CSRF-Token": admin})
    assert response.status_code == 200
    body = response.json()
    assert body["check"] is not None
    assert body["check"]["ok"] is True
    assert DUMMY_KEY not in response.text


def test_delete_model_key_keeps_file_entry_only(api, admin) -> None:
    from mla.services.credentials import set_key

    set_key("deepseek", DUMMY_KEY)
    response = api.client.delete("/api/settings/model/deepseek", headers={"X-CSRF-Token": admin})
    assert response.status_code == 200
    assert response.json()["providers"][0]["configured"] is False
    assert json.loads((mla_home() / "auth.json").read_text(encoding="utf-8")) == {}


def test_normal_user_cannot_touch_model_settings(api) -> None:
    """普通用户连读都不行：403 `FORBIDDEN`，动作指向"联系管理员"。"""
    setup_admin(api.client)
    register(api.client, username="staff")
    response = api.client.get("/api/settings/model")
    assert response.status_code == 403
    error = error_of(response)
    assert error["code"] == "FORBIDDEN"
    assert error["action"]["type"] == "contact_admin"
