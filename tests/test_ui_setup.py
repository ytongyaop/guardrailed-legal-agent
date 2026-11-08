"""首次初始化、首次登录引导、设置页与模型 Key 优先级。

界面行为用 Streamlit AppTest 真跑；判定用的纯函数（表单校验、优先级、是否弹窗）单独测，
这样弹窗文案与优先级规则不依赖界面渲染细节。
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest  # noqa: E402

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import Paths, data_path, load_config  # noqa: E402
from mla.domain.models import Role, User  # noqa: E402
from mla.services.auth import authenticate, has_admin, hash_password  # noqa: E402
from mla.ui.app import (  # noqa: E402
    INIT_PASSWORD_REMINDER,
    LLM_KEY_SETTING,
    _onboarding_content,
    _resolve_llm,
    _should_onboard,
    init_form_error,
    key_source_label,
    llm_key_sources,
    resolve_llm_key,
)

APP = str(Path(__file__).resolve().parents[1] / "src" / "mla" / "ui" / "app.py")
ONBOARDING_FIRST_POINT = "用途：核对劳动合同中的用工风险，每条结论都给出法条依据。"


def _paths() -> Paths:
    return Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(paths=_paths(), db_path=str(tmp_path / "setup.sqlite3"))


def _app(tmp_path, monkeypatch) -> AppTest:
    """AppTest 会把 get_config / get_repo 的结果缓存住，换库前必须清缓存。"""
    monkeypatch.setenv("MLA_DB", str(tmp_path / "setup.sqlite3"))
    st.cache_resource.clear()
    return AppTest.from_file(APP, default_timeout=30)


def _make_user(repo: SqliteRepository, username: str, password: str, role: Role = Role.USER, **kw) -> User:
    digest, salt = hash_password(password)
    user = User(user_id="U-" + username, username=username, password_hash=digest, salt=salt, role=role, **kw)
    repo.create_user(user)
    return user


def _all_text(at: AppTest) -> str:
    return "\n".join(
        e.value
        for e in list(at.markdown)
        + list(at.caption)
        + list(at.code)
        + list(at.error)
        + list(at.warning)
        + list(at.info)
        + list(at.success)
    )


def _field(at: AppTest, label: str):
    return next(x for x in at.text_input if x.label == label)


# ---------------- 首次初始化 ----------------
def test_init_form_validation_order(tmp_path) -> None:
    """四条校验顺序固定：账号非空 → 两次密码一致 → 至少 8 位 → 重名。"""
    repo = _repo(tmp_path)
    assert init_form_error(repo, "  ", "secret123", "secret123") == "请填写管理员账号"
    assert init_form_error(repo, "admin", "secret123", "secret124") == "两次输入的密码不一致"
    assert init_form_error(repo, "admin", "short", "short") == "密码至少 8 位"

    _make_user(repo, "admin", "secret123", Role.ADMIN)
    assert init_form_error(repo, "admin", "secret123", "secret123") == "用户名已存在：admin"
    assert init_form_error(repo, "owner", "secret123", "secret123") == ""


def test_init_page_shows_form_and_no_login(tmp_path, monkeypatch) -> None:
    """空库渲染初始化表单与“创建并进入”，不渲染登录/注册。"""
    at = _app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert [t.value for t in at.title] == ["首次使用"]
    assert [b.label for b in at.button] == ["创建并进入"]
    labels = [i.label for i in at.text_input]
    assert labels == ["管理员账号", "登录密码", "确认密码", "企业名称"]


def test_init_flow_creates_admin_and_enters_main(tmp_path, monkeypatch) -> None:
    """填完表单点创建：库里有管理员、会话里有 user、直接进主界面。"""
    at = _app(tmp_path, monkeypatch).run()
    _field(at, "管理员账号").set_value("owner")
    _field(at, "登录密码").set_value("secret123")
    _field(at, "确认密码").set_value("secret123")
    _field(at, "企业名称").set_value("示范小微企业")
    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    assert at.session_state["user"].username == "owner"
    assert at.session_state["user"].role is Role.ADMIN
    repo = SqliteRepository(paths=_paths(), db_path=str(tmp_path / "setup.sqlite3"))
    assert has_admin(repo) is True
    assert any("合同核对" in h.value for h in at.header)


def test_init_flow_reports_mismatch_without_writing(tmp_path, monkeypatch) -> None:
    """两次密码不一致只报错，不建号。"""
    at = _app(tmp_path, monkeypatch).run()
    _field(at, "管理员账号").set_value("owner")
    _field(at, "登录密码").set_value("secret123")
    _field(at, "确认密码").set_value("secret124")
    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    assert any("两次输入的密码不一致" in e.value for e in at.error)
    assert "user" not in at.session_state
    repo = SqliteRepository(paths=_paths(), db_path=str(tmp_path / "setup.sqlite3"))
    assert has_admin(repo) is False


# ---------------- 忘记密码（本地找回） ----------------
def test_login_page_forgot_password_shows_local_commands(tmp_path, monkeypatch) -> None:
    """登录页有「忘记密码」入口；点开给出可复制的本地重置命令与兜底步骤。"""
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert "本地应用无法通过邮箱找回" not in _all_text(at)

    at = next(b for b in at.button if b.label == "忘记密码").click().run()
    assert not at.exception
    texts = _all_text(at)
    assert "本地应用无法通过邮箱找回" in texts
    assert "python -m mla admin reset --username 你的账号" in texts
    assert "python -m mla admin list" in texts
    assert "python -m mla admin promote --username 你的账号" in texts


def test_init_page_reminds_local_password_reset(tmp_path, monkeypatch) -> None:
    """初始化页提示记住口令，并给出本机终端重置命令。"""
    at = _app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert INIT_PASSWORD_REMINDER in _all_text(at)


# ---------------- 首次登录引导 ----------------
def test_should_onboard_reads_persisted_flag() -> None:
    """只看落库的 onboarded：没登录不弹，未引导弹，标记过不弹。"""
    assert _should_onboard(None) is False
    assert _should_onboard(User(user_id="U1", username="a", password_hash="x", salt="y")) is True
    assert (
        _should_onboard(User(user_id="U1", username="a", password_hash="x", salt="y", onboarded=True))
        is False
    )


def test_onboarding_content_covers_four_points() -> None:
    """登录页删掉的四件事（用途 / 边界 / 数据 / 范围）都在引导文案里。"""
    title, points = _onboarding_content()
    assert title == "开始之前"
    assert len(points) == 4
    joined = "\n".join(points)
    for required in ("用途：", "边界：", "数据：", "范围：", "不判定违法或无效", "会拒答"):
        assert required in joined


def test_onboarding_dialog_shows_once_then_marks(tmp_path, monkeypatch) -> None:
    """未引导账号登录后弹一次；点确认后写库，再进来不再弹。"""
    repo = SqliteRepository(paths=_paths(), db_path=str(tmp_path / "setup.sqlite3"))
    user = _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=False)

    at = _app(tmp_path, monkeypatch)
    at.session_state["user"] = user
    at = at.run()
    assert not at.exception
    assert ONBOARDING_FIRST_POINT in _all_text(at)
    assert any(b.label == "我知道了" for b in at.button)

    next(b for b in at.button if b.label == "我知道了").click().run()
    assert not at.exception
    assert at.session_state["user"].onboarded is True
    assert ONBOARDING_FIRST_POINT not in _all_text(at)
    marked = SqliteRepository(paths=_paths(), db_path=str(tmp_path / "setup.sqlite3")).get_user_by_name(
        "admin"
    )
    assert marked is not None and marked.onboarded is True

    # 换一次会话（同一账号已标记）：不再弹
    again = _app(tmp_path, monkeypatch)
    again.session_state["user"] = marked
    again = again.run()
    assert not again.exception
    assert ONBOARDING_FIRST_POINT not in _all_text(again)


# ---------------- 模型 Key 优先级 ----------------
def test_llm_key_priority_prefers_secrets() -> None:
    """三处都有值时取 secrets.toml。"""
    assert resolve_llm_key(("sk-secrets", "sk-db", "sk-env")) == "sk-secrets"
    assert key_source_label(("sk-secrets", "sk-db", "sk-env")) == "secrets.toml"


def test_llm_key_priority_falls_back_to_db(tmp_path, monkeypatch) -> None:
    """secrets 为空时取数据库设置，并如实报告来源。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    repo = _repo(tmp_path)
    repo.set_setting(LLM_KEY_SETTING, "sk-db")

    sources = llm_key_sources(repo)
    assert sources == ("", "sk-db", "")
    assert resolve_llm_key(sources) == "sk-db"
    assert key_source_label(sources) == "数据库设置"


def test_llm_key_priority_env_last_and_unset_label(tmp_path, monkeypatch) -> None:
    """只剩环境变量时取环境变量；三处都没有就是未配置。"""
    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    monkeypatch.setenv("MLA_LLM_API_KEY", "sk-env")
    sources = llm_key_sources(_repo(tmp_path))
    assert resolve_llm_key(sources) == "sk-env"
    assert key_source_label(sources) == "环境变量"

    monkeypatch.delenv("MLA_LLM_API_KEY")
    empty = llm_key_sources(_repo(tmp_path))
    assert resolve_llm_key(empty) == ""
    assert key_source_label(empty) == "未配置"


# ---------------- 设置页 ----------------
SETTINGS_SCRIPT = """
import streamlit as st
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
import mla.ui.app as ui_app

repo = SqliteRepository(
    paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
    db_path=DB_PATH,
)
user = repo.get_user_by_name(USERNAME)
st.session_state["user"] = user
ui_app._page_settings(repo, user)
"""


def _settings_app(tmp_path, username: str) -> AppTest:
    script = SETTINGS_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "setup.sqlite3"))).replace(
        "USERNAME", repr(username)
    )
    return AppTest.from_string(script, default_timeout=60).run()


def test_settings_page_saves_and_clears_key(tmp_path) -> None:
    """管理员保存 Key 落库、来源跟着变；清除写空串，回落到未配置。"""
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    assert "模型设置" in _all_text(at)
    assert "当前 Key 来源：未配置" in _all_text(at)

    _field(at, "DeepSeek API Key").set_value("sk-local-123")
    next(b for b in at.button if b.label == "保存").click().run()
    assert not at.exception
    assert repo.get_setting(LLM_KEY_SETTING) == "sk-local-123"
    assert "已保存到本机数据库，下次启动自动读取" in _all_text(at)
    assert "当前 Key 来源：数据库设置" in _all_text(at)

    next(b for b in at.button if b.label == "清除数据库中的 Key").click().run()
    assert not at.exception
    assert repo.get_setting(LLM_KEY_SETTING) == ""
    assert "当前 Key 来源：未配置" in _all_text(at)


def test_settings_page_hides_model_settings_for_normal_user(tmp_path) -> None:
    """普通用户只看到账号设置，看不到模型 Key 区块。"""
    repo = _repo(tmp_path)
    _make_user(repo, "alice", "secret123", Role.USER, onboarded=True)

    at = _settings_app(tmp_path, "alice")
    assert not at.exception
    texts = _all_text(at)
    assert "账号设置" in texts
    assert "模型设置" not in texts
    assert "当前 Key 来源" not in texts
    assert not any(b.label == "保存" for b in at.button)


def test_settings_page_change_password_flow(tmp_path) -> None:
    """旧密码错报错、新密码短报错、成功后新密码能登录。"""
    repo = _repo(tmp_path)
    _make_user(repo, "alice", "oldpass123", Role.USER, onboarded=True)

    at = _settings_app(tmp_path, "alice")
    assert _field(at, "用户名").value == "alice"

    def _submit(old: str, new: str, confirm: str) -> AppTest:
        _field(at, "当前密码").set_value(old)
        _field(at, "新密码（至少 8 位）").set_value(new)
        _field(at, "确认新密码").set_value(confirm)
        return next(b for b in at.button if b.label == "修改密码").click().run()

    wrong_old = _submit("notmypass", "newpass123", "newpass123")
    assert not wrong_old.exception
    assert any("当前口令不正确" in e.value for e in wrong_old.error)
    assert authenticate(repo, "alice", "oldpass123") is not None

    short_new = _submit("oldpass123", "short", "short")
    assert not short_new.exception
    assert any("新口令至少 8 位" in e.value for e in short_new.error)
    assert authenticate(repo, "alice", "oldpass123") is not None

    ok = _submit("oldpass123", "newpass123", "newpass123")
    assert not ok.exception
    assert any("密码已修改" in s.value for s in ok.success)
    assert authenticate(repo, "alice", "newpass123") is not None
    assert authenticate(repo, "alice", "oldpass123") is None


def test_settings_page_has_test_connection_button(tmp_path, monkeypatch) -> None:
    """模型设置区块有测试连接：未配置 Key 时禁用并提示先保存，配好后可用。"""
    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    button = next(b for b in at.button if b.label == "测试连接")
    assert button.proto.disabled is True
    assert "请先保存 Key，再测试连接。" in _all_text(at)

    repo.set_setting(LLM_KEY_SETTING, "sk-local-123")
    st.cache_resource.clear()
    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    assert next(b for b in at.button if b.label == "测试连接").proto.disabled is False
    assert "请先保存 Key，再测试连接。" not in _all_text(at)


# ---------------- 模型 Key 的解析（核对页真实入口） ----------------
def test_resolve_llm_reads_db_key_without_repo_arg_it_is_none(tmp_path, monkeypatch) -> None:
    """库里存了 Key 时 _resolve_llm 返回配置；漏传 repo 就是读不到（本次修的 bug）。"""
    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    repo = _repo(tmp_path)
    repo.set_setting(LLM_KEY_SETTING, "sk-db")

    llm = _resolve_llm(load_config(), repo)
    assert llm is not None
    assert llm.api_key == "sk-db"
    assert _resolve_llm(load_config()) is None


def test_resolve_llm_key_priority_secrets_db_env(tmp_path, monkeypatch) -> None:
    """三处来源的优先级在 _resolve_llm 上仍然是 secrets > 数据库 > 环境变量。"""

    def _key(repo) -> str:
        llm = _resolve_llm(load_config(), repo)
        return llm.api_key if llm is not None else ""

    repo = _repo(tmp_path)
    repo.set_setting(LLM_KEY_SETTING, "sk-db")
    monkeypatch.setenv("MLA_LLM_API_KEY", "sk-env")

    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "sk-secrets")
    assert _key(repo) == "sk-secrets"

    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    assert _key(repo) == "sk-db"

    repo.set_setting(LLM_KEY_SETTING, "")
    assert _key(repo) == "sk-env"

    monkeypatch.delenv("MLA_LLM_API_KEY")
    assert _key(repo) == ""


# ---------------- 侧栏只剩登录信息 ----------------
def _logged_in_app(tmp_path, monkeypatch, *, db_key: str = "") -> AppTest:
    """真跑 app.py：库里建好管理员并塞进会话，落在默认页（合同核对）。"""
    monkeypatch.setattr("mla.ui.app._secrets_llm_key", lambda: "")
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    repo = _repo(tmp_path)
    if db_key:
        repo.set_setting(LLM_KEY_SETTING, db_key)
    user = _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True, enterprise_id="E1")
    at = _app(tmp_path, monkeypatch)
    at.session_state["user"] = user
    return at.run()


def _sidebar_text(at: AppTest) -> str:
    return "\n".join(
        [e.value for e in list(at.sidebar.markdown) + list(at.sidebar.caption)]
        + [e.label for e in at.sidebar.button]
    )


def test_sidebar_drops_llm_settings_entry(tmp_path, monkeypatch) -> None:
    """模型设置只留设置页：侧栏没有“大模型设置”，只剩账号与退出登录。"""
    at = _logged_in_app(tmp_path, monkeypatch)
    assert not at.exception
    sidebar = _sidebar_text(at)
    assert "大模型设置" not in sidebar
    assert "大模型设置" not in _all_text(at)
    assert "admin · 管理员" in sidebar
    assert "退出登录" in sidebar


def test_check_page_enables_clause_review_with_db_key(tmp_path, monkeypatch) -> None:
    """库里有 Key、环境变量清空时，核对页的条款审查开关可用（bug 修复的实测）。"""
    at = _logged_in_app(tmp_path, monkeypatch, db_key="sk-db-only")
    assert not at.exception
    box = next(c for c in at.checkbox if c.label == "启用条款审查（模型辅助）")
    assert box.proto.disabled is False
    assert box.value is True


def test_check_page_disables_clause_review_without_key(tmp_path, monkeypatch) -> None:
    """三处来源都没有 Key 时仍然置灰，说明上一例不是恒为可用。"""
    at = _logged_in_app(tmp_path, monkeypatch)
    assert not at.exception
    box = next(c for c in at.checkbox if c.label == "启用条款审查（模型辅助）")
    assert box.proto.disabled is True
    assert box.value is False
