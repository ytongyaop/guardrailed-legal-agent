"""设置（规格 006 §2）：界面开关持久化、模型设置不泄露 Key、`PUT` 走打桩的 `probe_models`。

`PUT /settings/model` 的探针在 `routes/settings.py` 里留了一个 `probe_key` 注入点，
用例打桩它即可完全不联网；真实请求路径仍是 `DeepSeekLLM.probe_models`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mla.adapters.llm import ProbeResult
from mla.api.routes import settings as settings_routes
from mla.config import mla_home
from mla.services.credentials import DASHSCOPE_PROVIDER

from .api_helpers import csrf_of, error_of, login, register, setup_admin

pytestmark = pytest.mark.no_llm

DUMMY_KEY = "sk-dummy-" + "0" * 26
OTHER_KEY = "sk-other-" + "1" * 26


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


# ---------------------------------------------------------------- 向量检索 Key


def _auth_file() -> Path:
    return mla_home() / "auth.json"


def test_put_embedding_key_probes_then_saves(api, admin, monkeypatch) -> None:
    """探测通过才落盘：`auth.json` 多一条 `dashscope`，状态行变「已启用」。"""
    calls: list[str] = []

    def fake_probe(key: str) -> ProbeResult:
        calls.append(key)
        return ProbeResult(kind="ok", status_code=200, message="HTTP 200")

    monkeypatch.setattr(settings_routes, "probe_embedding", fake_probe)
    response = api.client.put(
        "/api/settings/embedding",
        json={"key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [DUMMY_KEY]
    assert body["embedding"]["configured"] is True
    assert "已启用" in body["embedding"]["fallback_text"]
    assert DUMMY_KEY not in response.text

    stored = json.loads(_auth_file().read_text(encoding="utf-8"))
    assert stored == {DASHSCOPE_PROVIDER: {"type": "api_key", "key": DUMMY_KEY}}


def test_put_embedding_key_rejected_when_probe_says_auth(api, admin, monkeypatch) -> None:
    """Key 不对：422 `MODEL_KEY_INVALID`，且绝不落盘（连文件都不创建）。"""
    monkeypatch.setattr(
        settings_routes, "probe_embedding", lambda key: ProbeResult(kind="auth", status_code=401)
    )
    response = api.client.put(
        "/api/settings/embedding",
        json={"key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "MODEL_KEY_INVALID"
    assert DUMMY_KEY not in response.text
    assert not _auth_file().exists()


def test_put_embedding_key_reports_network_failure(api, admin, monkeypatch) -> None:
    """网络类失败：502 `MODEL_PROBE_FAILED`，同样不落盘。"""
    monkeypatch.setattr(
        settings_routes, "probe_embedding", lambda key: ProbeResult(kind="network", message="URLError")
    )
    response = api.client.put(
        "/api/settings/embedding",
        json={"key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 502
    assert error_of(response)["code"] == "MODEL_PROBE_FAILED"
    assert not _auth_file().exists()


def test_put_embedding_key_requires_non_empty_key(api, admin) -> None:
    response = api.client.put(
        "/api/settings/embedding",
        json={"key": "   "},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 400
    assert error_of(response)["details"] == {"fields": ["key"]}


def test_delete_embedding_key_restores_bm25_fallback(api, admin) -> None:
    """清除后回到关键词检索；只删 `dashscope`，大模型条目不受影响。"""
    from mla.services.credentials import set_key

    set_key("deepseek", DUMMY_KEY)
    set_key(DASHSCOPE_PROVIDER, OTHER_KEY)
    response = api.client.delete("/api/settings/embedding", headers={"X-CSRF-Token": admin})
    assert response.status_code == 200
    body = response.json()
    assert body["embedding"]["configured"] is False
    assert "BM25" in body["embedding"]["fallback_text"]
    stored = json.loads(_auth_file().read_text(encoding="utf-8"))
    assert stored == {"deepseek": {"type": "api_key", "key": DUMMY_KEY}}


def test_embedding_key_is_read_from_auth_file(api, admin) -> None:
    """设置页保存的 Key 会被 `load_config()` 读回来（检索装配也从这里取值）。"""
    from mla.config import load_config
    from mla.services.credentials import set_key

    assert load_config().embedding.api_key == ""
    set_key(DASHSCOPE_PROVIDER, OTHER_KEY)
    assert load_config().embedding.api_key == OTHER_KEY
    assert api.client.get("/api/settings/model").json()["embedding"]["configured"] is True


def test_embedding_env_var_beats_auth_file(api, monkeypatch) -> None:
    """优先级：`DASHSCOPE_API_KEY` > auth.json 的 `dashscope` 条目。"""
    from mla.config import load_config
    from mla.services.credentials import set_key

    set_key(DASHSCOPE_PROVIDER, OTHER_KEY)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env-" + "2" * 26)
    assert load_config().embedding.api_key == "sk-env-" + "2" * 26


def test_embedding_env_var_skips_auth_file_read(api, monkeypatch) -> None:
    """环境变量已给出 Key 时连文件都不读（探针与设置页都不会被坏文件拖住）。"""
    from mla.adapters import auth_file
    from mla.config import load_config

    def boom(home: Path, provider: str):
        raise AssertionError("环境变量已提供 Key，不应再读 auth.json")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env-" + "3" * 26)
    monkeypatch.setattr(auth_file, "get_entry", boom)
    assert load_config().embedding.api_key == "sk-env-" + "3" * 26


def test_dashscope_is_not_a_model_provider(api, admin) -> None:
    """两个 Key 不许串门：`dashscope` 不出现在模型服务商列表里，也存不进模型槽位。"""
    response = api.client.put(
        "/api/settings/model",
        json={"provider": DASHSCOPE_PROVIDER, "key": DUMMY_KEY},
        headers={"X-CSRF-Token": admin},
    )
    assert response.status_code == 404
    body = api.client.get("/api/settings/model").json()
    assert [item["provider"] for item in body["providers"]] == ["deepseek"]
