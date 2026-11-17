"""首次初始化、首次登录引导、设置页与模型 Key 的设置流程。

界面行为用 Streamlit AppTest 真跑；判定用的纯函数（表单校验、是否弹窗、状态行渲染）
单独测，这样文案与交互规则不依赖界面渲染细节。

Key 一律用固定哑串（`sk-dummy-` 前缀），真实 Key 不进入断言、文件或输出；
凭据文件的权限收紧（whoami / icacls）在本模块统一打桩，测试不调用真实 ACL。
"""

from __future__ import annotations

import inspect
import json
import subprocess
import urllib.error
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

import streamlit as st
from streamlit.testing.v1 import AppTest  # noqa: E402

from mla.adapters import auth_file  # noqa: E402
from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import Paths, data_path, load_config  # noqa: E402
from mla.domain.models import Role, User  # noqa: E402
from mla.services.auth import authenticate, has_admin, hash_password  # noqa: E402
from mla.services.credentials import (  # noqa: E402
    CheckStatus,
    KeyStatus,
    list_key_status,
    resolve_key,
    set_key,
)
from mla.ui import app as ui_app  # noqa: E402
from mla.ui.app import (  # noqa: E402
    INIT_PASSWORD_REMINDER,
    _key_status_line,
    _onboarding_content,
    _resolve_llm,
    _should_onboard,
    init_form_error,
)

APP = str(Path(__file__).resolve().parents[1] / "src" / "mla" / "ui" / "app.py")
ONBOARDING_FIRST_POINT = "用途：核对劳动合同中的用工风险，每条结论都给出法条依据。"

# 哑 Key：固定前缀 + 固定长度，断言里只用前缀与整串，绝不使用真实 Key
DUMMY_KEY = "sk-dummy-" + "0" * 26
OTHER_KEY = "sk-dummy-" + "9" * 26
LEGACY_KEY = "sk-dummy-" + "5" * 26
KEY_PREFIX = "sk-dummy-"

# 旧库里 Key 的键名（阶段三只读兜底仍读它）
LEGACY_SETTING_KEY = "llm_api_key"


@pytest.fixture(autouse=True)
def fake_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据文件的权限收紧打桩：AppTest 里保存 / 清除时不调用真实 whoami / icacls。"""

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "whoami":
            return subprocess.CompletedProcess(args, 0, "TESTPC\\tester\n", "")
        if len(args) == 2 and args[0] == "icacls":
            return subprocess.CompletedProcess(args, 0, f"{args[1]} TESTPC\\tester:(F)\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(auth_file, "_run_cmd", fake_run)


class FakeResponse:
    """`urlopen` 替身：只实现校验路径用到的 read / status / 上下文管理。"""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def read(self) -> bytes:
        return b"{}"

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    error: BaseException | None = None,
    calls: list[tuple[str, str, bool, float | None, str]] | None = None,
) -> None:
    """把 `GET /models` 打桩：记录 (method, url, 是否带 body, 超时, Authorization)。"""

    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - 与 urllib 接口一致
        if calls is not None:
            calls.append(
                (
                    req.get_method(),
                    req.full_url,
                    req.data is not None,
                    timeout,
                    str(req.get_header("Authorization") or ""),
                )
            )
        if error is not None:
            raise error
        return FakeResponse(status)

    monkeypatch.setattr("mla.adapters.llm.urllib.request.urlopen", fake_urlopen)


def _home(tmp_path: Path) -> Path:
    """与 conftest 的 autouse 隔离一致：本用例专属 MLA_HOME。"""
    return tmp_path / "mla-home"


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


def _click(at: AppTest, label: str) -> AppTest:
    return next(b for b in at.button if b.label == label).click().run()


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


# ---------------- 模型 Key 的解析（阶段三：env > auth_file > 旧库兜底） ----------------
def test_resolve_llm_prefers_env_over_auth_file(tmp_path, monkeypatch) -> None:
    """两级优先级：环境变量盖过凭据文件；撤掉环境变量就回落到文件。"""
    set_key("deepseek", DUMMY_KEY, home=_home(tmp_path))
    monkeypatch.setenv("MLA_LLM_API_KEY", OTHER_KEY)
    assert _resolve_llm(load_config()).api_key == OTHER_KEY

    monkeypatch.delenv("MLA_LLM_API_KEY")
    assert _resolve_llm(load_config()).api_key == DUMMY_KEY


def test_resolve_llm_falls_back_to_legacy_database(tmp_path, monkeypatch) -> None:
    """阶段三兜底：文件没有、环境变量也没有时，只读旧库 `settings.llm_api_key`。"""
    repo = _repo(tmp_path)
    repo.set_setting(LEGACY_SETTING_KEY, LEGACY_KEY)
    monkeypatch.setenv("MLA_DB", str(tmp_path / "setup.sqlite3"))
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)

    llm = _resolve_llm(load_config())
    assert llm is not None
    assert llm.api_key == LEGACY_KEY


def test_resolve_llm_unconfigured_returns_none(tmp_path, monkeypatch) -> None:
    """三处都没有 → 未配置（界面按缺 Key 处理，不降级成模板回答）。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    assert _resolve_llm(load_config()) is None
    assert resolve_key("deepseek", legacy_db=load_config().paths.db).source == "none"


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
    st.cache_resource.clear()
    return AppTest.from_string(script, default_timeout=60).run()


def _save_key(at: AppTest, key: str) -> AppTest:
    _field(at, "DeepSeek API Key").set_value(key)
    return _click(at, "测试并保存")


def test_settings_page_saves_key_and_clears_it(tmp_path, monkeypatch) -> None:
    """测试并保存写 auth.json；状态行立刻变“已配置 · 有效”；清除后回到未配置。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    assert "未配置 · 服务商：DeepSeek" in _all_text(at)
    assert "模型设置" in _all_text(at)
    # 服务商下拉的取值是 provider 键名，显示名只做映射（扩容时加一行就多一项）
    assert at.selectbox[0].label == "服务商"
    assert at.selectbox[0].value == "deepseek"
    assert list(at.selectbox[0].options) == ["DeepSeek"]

    at = _click(at, "测试并保存")
    assert not at.exception
    assert "请先粘贴 API Key" in _all_text(at)
    assert not (_home(tmp_path) / "auth.json").exists()

    at = _save_key(at, DUMMY_KEY)
    assert not at.exception
    saved = json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8"))
    assert saved == {"deepseek": {"type": "api_key", "key": DUMMY_KEY}}
    texts = _all_text(at)
    assert "已配置 · 有效（" in texts
    assert "来源：用户凭据文件（auth.json）" in texts
    assert "已保存到用户凭据文件（连接校验通过），下次启动自动读取" in texts
    assert "models 数据库" not in texts

    at = _click(at, "更改 Key")
    at = _click(at, "清除此 Key")
    assert "确认清除" in _all_text(at)
    at = _click(at, "确认清除")
    assert not at.exception
    assert json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8")) == {}
    assert "未配置 · 服务商：DeepSeek" in _all_text(at)


def test_settings_page_hides_model_settings_for_normal_user(tmp_path) -> None:
    """普通用户只看到账号设置，看不到模型 Key 区块。"""
    repo = _repo(tmp_path)
    _make_user(repo, "alice", "secret123", Role.USER, onboarded=True)

    at = _settings_app(tmp_path, "alice")
    assert not at.exception
    texts = _all_text(at)
    assert "账号设置" in texts
    assert "模型设置" not in texts
    assert "未配置 · 服务商" not in texts
    assert not any(b.label == "测试并保存" for b in at.button)


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


def test_settings_page_test_connection_only_when_configured(tmp_path, monkeypatch) -> None:
    """未配置不显示“测试连接”；已配置才显示，点了只复核当前生效 Key 并写校验历史。"""
    calls: list[tuple[str, str, bool, float | None, str]] = []
    _patch_probe(monkeypatch, calls=calls)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    assert not any(b.label == "测试连接" for b in at.button)

    set_key("deepseek", DUMMY_KEY, home=_home(tmp_path))
    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    assert any(b.label == "测试连接" for b in at.button)

    at = _click(at, "测试连接")
    assert not at.exception
    assert "连接正常" in _all_text(at)
    assert "已配置 · 有效（" in _all_text(at)
    assert [c[0] for c in calls] == ["GET"]
    assert calls[0][1].endswith("/models")


# ---------------- 设置页：第 10 章交互验收 ----------------
def test_save_visible_changes(tmp_path, monkeypatch) -> None:
    """AC-13：未配置起始保存哑 Key，状态行 / 勾选框 / 有效性三处同时变化。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True, enterprise_id="E1")

    before = _settings_app(tmp_path, "admin")
    assert "未配置 · 服务商：DeepSeek" in _all_text(before)
    # 核对页：未配置时被门禁挡住（勾选框不渲染）
    gated = _check_page_app(tmp_path, "admin")
    assert not any(c.label == "启用条款审查（模型辅助）" for c in gated.checkbox)

    at = _save_key(before, DUMMY_KEY)
    assert not at.exception
    texts = _all_text(at)
    assert "已配置" in texts
    assert "来源：用户凭据文件（auth.json）" in texts
    assert "有效（" in texts
    assert (_home(tmp_path) / "auth-status.json").exists()

    # 保存后：条款审查勾选框可点
    checked = _check_page_app(tmp_path, "admin")
    box = next(c for c in checked.checkbox if c.label == "启用条款审查（模型辅助）")
    assert box.proto.disabled is False


def test_key_persists_after_restart(tmp_path, monkeypatch) -> None:
    """AC-14：销毁会话、用同一 MLA_HOME 新建会话后仍是“已配置”（证明落在文件里）。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception

    again = _settings_app(tmp_path, "admin")
    assert not again.exception
    texts = _all_text(again)
    assert "已配置" in texts
    assert "来源：用户凭据文件（auth.json）" in texts
    assert json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8"))["deepseek"]["key"] == (
        DUMMY_KEY
    )


def test_unconfigured_hint(tmp_path, monkeypatch) -> None:
    """AC-16：无 Key 时条款审查置灰并给“去设置”指引，且说明三项能力受影响。"""
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True, enterprise_id="E1")

    at = _check_page_app(tmp_path, "admin")
    assert not at.exception
    texts = _all_text(at)
    assert "去设置" in "\n".join(p.value for p in at.page_link)
    for ability in ("处理建议", "生成式问答", "条款级审查"):
        assert ability in texts
    assert "系统故障" not in texts

    # 勾选框本体的 disabled 状态单独渲染一遍：置灰不能只留一个灰框
    toggle = AppTest.from_string(TOGGLE_SCRIPT, default_timeout=30).run()
    assert not toggle.exception
    box = next(c for c in toggle.checkbox if c.label == "启用条款审查（模型辅助）")
    assert box.proto.disabled is True
    assert "去设置" in _all_text(toggle)


def test_env_source_label(tmp_path, monkeypatch) -> None:
    """AC-17：只设环境变量时来源如实显示，整页文本不含哑 Key 也不含其长度。"""
    _patch_probe(monkeypatch)
    monkeypatch.setenv("MLA_LLM_API_KEY", DUMMY_KEY)
    st.cache_resource.clear()
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _settings_app(tmp_path, "admin")
    assert not at.exception
    texts = _all_text(at)
    assert "已配置 · 未校验（来源：环境变量（MLA_LLM_API_KEY））" in texts
    assert "服务商：DeepSeek" in texts
    assert "模型：deepseek-chat" in texts
    assert KEY_PREFIX not in texts
    assert str(len(DUMMY_KEY)) not in texts

    # 列表接口只给元数据：没有 Key 字段，也没有长度字段
    statuses = list_key_status()
    names = {f.name for f in fields(KeyStatus)}
    assert names == {"provider", "type", "source", "label", "path"}
    assert all("key" not in name and "length" not in name for name in names)
    assert all(isinstance(s.path, str) for s in statuses)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (urllib.error.URLError("dns failure"), "网络不通，请稍后重试（本次未保存）"),
        (TimeoutError("timed out"), "网络不通，请稍后重试（本次未保存）"),
    ],
)
def test_save_offline_no_write(tmp_path, monkeypatch, failure, message: str) -> None:
    """AC-18 / AC-24：断网或超时“测试并保存”不落盘、不写校验历史、不调 set_key。"""
    _patch_probe(monkeypatch, error=failure)
    spy: list[str] = []
    monkeypatch.setattr(ui_app, "set_key", lambda provider, key: spy.append(provider))
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception
    assert not (_home(tmp_path) / "auth.json").exists()
    assert not (_home(tmp_path) / "auth-status.json").exists()
    assert [e.value for e in at.error] == [message]
    texts = _all_text(at)
    assert "网络不通" in texts
    assert "未保存" in texts
    assert "已保存" not in texts
    assert "已更新" not in texts
    assert "未配置 · 服务商：DeepSeek" in texts
    assert spy == []


@pytest.mark.parametrize("code", [401, 403])
def test_save_invalid_key_no_write(tmp_path, monkeypatch, code: int) -> None:
    """AC-19：401 / 403 提示恰为固定文案，文件不生成，set_key 一次都不调。"""
    url = "https://api.deepseek.com/models"
    _patch_probe(monkeypatch, error=urllib.error.HTTPError(url, code, "denied", None, None))
    spy: list[str] = []
    monkeypatch.setattr(ui_app, "set_key", lambda provider, key: spy.append(provider))
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception
    assert [e.value for e in at.error] == ["Key 不正确，请检查后重新输入"]
    assert not (_home(tmp_path) / "auth.json").exists()
    assert not (_home(tmp_path) / "auth-status.json").exists()
    assert "已配置" not in _all_text(at)
    assert spy == []


def test_probe_models_only(tmp_path, monkeypatch) -> None:
    """AC-20：校验只有一次 GET /models，带 Authorization，没有对话请求与生成负载。"""
    calls: list[tuple[str, str, bool, float | None, str]] = []
    _patch_probe(monkeypatch, calls=calls)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception

    assert len(calls) == 1
    method, url, has_body, timeout, auth = calls[0]
    assert method == "GET"
    assert url.endswith("/models")
    assert has_body is False
    assert auth == f"Bearer {DUMMY_KEY}"
    assert timeout == load_config().llm.auth_check_timeout_s
    assert not any("/chat/completions" in c[1] for c in calls)


def test_no_key_echo_after_save(tmp_path, monkeypatch) -> None:
    """AC-21：保存后页面、控件与会话状态里都找不到哑 Key；输入框已清空。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception
    assert KEY_PREFIX not in _all_text(at)
    for element in at.text_input:
        assert KEY_PREFIX not in str(element.value)
    for name in ("settings_key_input", "settings_flash", "settings_error", "settings_editing"):
        assert KEY_PREFIX not in str(at.session_state.get(name))

    # 展开“更改 Key”后输入框仍是空的；切到“显示”也还是空（是清空，不是遮罩）
    at = _click(at, "更改 Key")
    assert _field(at, "DeepSeek API Key").value == ""
    at = at.toggle[0].set_value(True).run()
    assert not at.exception
    assert _field(at, "DeepSeek API Key").value == ""
    assert KEY_PREFIX not in _all_text(at)


def test_change_key_paths(tmp_path, monkeypatch) -> None:
    """AC-22 路径一 / 二：换有效 Key 立即替换；换无效 Key 保留旧值并提示。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception

    # 路径一：换一把能通过校验的 Key
    at = _click(at, "更改 Key")
    at = _save_key(at, OTHER_KEY)
    assert not at.exception
    data = json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8"))
    assert data["deepseek"]["key"] == OTHER_KEY
    assert "已保存到用户凭据文件（连接校验通过），下次启动自动读取" in _all_text(at)

    # 路径二：换一把 401 的 Key —— 字节不变、旧 Key 仍生效、无“已切换 / 已更新”
    _patch_probe(
        monkeypatch,
        error=urllib.error.HTTPError("https://api.deepseek.com/models", 401, "denied", None, None),
    )
    before = (_home(tmp_path) / "auth.json").read_bytes()
    at = _click(at, "更改 Key")
    at = _save_key(at, DUMMY_KEY)
    assert not at.exception
    assert (_home(tmp_path) / "auth.json").read_bytes() == before
    texts = _all_text(at)
    assert "新 Key 未通过校验，原 Key 仍在使用" in texts
    assert "Key 不正确，请检查后重新输入" in texts
    assert "已切换" not in texts
    assert "已更新" not in texts
    assert resolve_key("deepseek").key == OTHER_KEY


def test_change_key_cancel_restores_collapsed_view(tmp_path, monkeypatch) -> None:
    """AC-22 前置：更改流程里点“取消”收起输入区、清空本次输入、文件一个字节都不动。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    before = (_home(tmp_path) / "auth.json").read_bytes()

    at = _click(at, "更改 Key")
    _field(at, "DeepSeek API Key").set_value(OTHER_KEY)
    at = _click(at, "取消")
    assert not at.exception
    assert (_home(tmp_path) / "auth.json").read_bytes() == before
    assert not any(b.label == "取消" for b in at.button)
    assert not any(b.label == "清除此 Key" for b in at.button)

    at = _click(at, "更改 Key")
    assert _field(at, "DeepSeek API Key").value == ""


@pytest.mark.parametrize(
    ("kind", "error", "message"),
    [
        (
            "auth",
            urllib.error.HTTPError("https://api.deepseek.com/models", 401, "denied", None, None),
            "Key 不正确，请检查后重新输入",
        ),
        ("network", urllib.error.URLError("dns failure"), "网络不通，请稍后重试（本次未保存）"),
    ],
)
def test_test_connection_records_failure_kind(tmp_path, monkeypatch, kind: str, error, message: str) -> None:
    """AC-23 分类落盘：测试连接失败写 ok:false + 英文 kind；auth.json 字节不变。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception
    before = (_home(tmp_path) / "auth.json").read_bytes()

    _patch_probe(monkeypatch, error=error)
    at = _click(at, "测试连接")
    assert not at.exception
    assert [e.value for e in at.error] == [message]
    assert "未通过校验" in _all_text(at)

    entry = json.loads((_home(tmp_path) / "auth-status.json").read_text(encoding="utf-8"))["deepseek"]
    assert entry["ok"] is False
    assert entry["kind"] == kind
    assert (_home(tmp_path) / "auth.json").read_bytes() == before
    assert resolve_key("deepseek").key == DUMMY_KEY


def test_clear_key_path(tmp_path, monkeypatch) -> None:
    """AC-22 路径三：更改流程内二次确认后清除，回到未配置，核对页重新置灰。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True, enterprise_id="E1")

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception
    # 反例守卫：收起态首屏不出现清除按钮
    assert not any(b.label == "清除此 Key" for b in at.button)

    at = _click(at, "更改 Key")
    at = _click(at, "清除此 Key")
    warning = "\n".join(w.value for w in at.warning)
    for ability in ("处理建议", "生成式问答", "条款级审查"):
        assert ability in warning
    at = _click(at, "取消清除")
    assert (_home(tmp_path) / "auth.json").exists()

    at = _click(at, "清除此 Key")
    at = _click(at, "确认清除")
    assert not at.exception
    assert json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8")) == {}
    assert "未配置 · 服务商：DeepSeek" in _all_text(at)

    cleared = _check_page_app(tmp_path, "admin")
    assert not any(c.label == "启用条款审查（模型辅助）" for c in cleared.checkbox)
    assert "去设置" in "\n".join(p.value for p in cleared.page_link)


def test_auth_status_persist(tmp_path, monkeypatch) -> None:
    """AC-23：校验历史落盘、形状固定、重启仍显示同一时间；文件删掉就降级为未校验。"""
    _patch_probe(monkeypatch)
    repo = _repo(tmp_path)
    _make_user(repo, "admin", "secret123", Role.ADMIN, onboarded=True)

    at = _save_key(_settings_app(tmp_path, "admin"), DUMMY_KEY)
    assert not at.exception

    status_file = _home(tmp_path) / "auth-status.json"
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    entry = payload["deepseek"]
    assert set(entry) == {"ok", "checked_at", "kind"}
    assert entry["ok"] is True and entry["kind"] == "ok"
    checked = datetime.fromisoformat(entry["checked_at"])
    assert checked.utcoffset() is not None
    raw = status_file.read_text(encoding="utf-8")
    assert KEY_PREFIX not in raw
    assert DUMMY_KEY not in raw

    # 跨会话：仍显示“有效（同一分钟）”
    again = _settings_app(tmp_path, "admin")
    assert not again.exception
    assert f"有效（{checked.astimezone():%Y-%m-%d %H:%M}）" in _all_text(again)

    # 反例守卫：删掉校验历史 → 降级为“未校验”，不影响 auth.json
    status_file.unlink()
    degraded = _settings_app(tmp_path, "admin")
    assert not degraded.exception
    texts = _all_text(degraded)
    assert "已配置 · 未校验" in texts
    assert "有效（" not in texts
    assert json.loads((_home(tmp_path) / "auth.json").read_text(encoding="utf-8"))["deepseek"]["key"] == (
        DUMMY_KEY
    )


def test_status_line_renderer_has_no_key_param() -> None:
    """状态行渲染器的签名里没有 Key 参数（规格 002 第 10.3 节，接口上禁止回显）。"""
    assert list(inspect.signature(_key_status_line).parameters) == ["status", "check", "model"]


def test_status_line_flags_stale_check() -> None:
    """超过 30 天的校验记录只在末尾加提示，不把“有效”改写成未校验 / 无效。"""
    old = (datetime.now().astimezone() - timedelta(days=40)).isoformat(timespec="seconds")
    status = KeyStatus(
        provider="deepseek",
        type="api_key",
        source="auth_file",
        label="用户凭据文件（auth.json）",
        path=str(Path("auth.json")),
    )
    line = _key_status_line(status, CheckStatus(ok=True, checked_at=old, kind="ok"), "deepseek-chat")
    assert "已配置 · 有效（" in line
    assert "上次校验超过 30 天，建议重新测试" in line


# ---------------- 核对页入口 ----------------
CHECK_PAGE_SCRIPT = """
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
# 直接调用页面函数时 _build_nav 没跑，NAV 为空；门禁里的设置页链接需要它存在。
ui_app.NAV["设置"] = st.Page(lambda: None, title="设置", url_path="settings")
ui_app._page_check(repo, user)
"""

TOGGLE_SCRIPT = """
from mla.config import load_config
import mla.ui.app as ui_app

ui_app._clause_review_toggle(load_config(), None)
"""


def _check_page_app(tmp_path, username: str) -> AppTest:
    script = CHECK_PAGE_SCRIPT.replace("DB_PATH", repr(str(tmp_path / "setup.sqlite3"))).replace(
        "USERNAME", repr(username)
    )
    st.cache_resource.clear()
    return AppTest.from_string(script, default_timeout=60).run()


# ---------------- 侧栏只剩登录信息 ----------------
def _logged_in_app(tmp_path, monkeypatch, *, key: str = "") -> AppTest:
    """真跑 app.py：库里建好管理员并塞进会话，落在默认页（合同核对）。"""
    monkeypatch.delenv("MLA_LLM_API_KEY", raising=False)
    repo = _repo(tmp_path)
    if key:
        set_key("deepseek", key, home=_home(tmp_path))
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


def test_check_page_enables_clause_review_with_auth_file_key(tmp_path, monkeypatch) -> None:
    """auth.json 里有 Key、环境变量清空时，核对页的条款审查开关可用。"""
    at = _logged_in_app(tmp_path, monkeypatch, key=DUMMY_KEY)
    assert not at.exception
    box = next(c for c in at.checkbox if c.label == "启用条款审查（模型辅助）")
    assert box.proto.disabled is False
    assert box.value is True


def test_check_page_gated_without_key(tmp_path, monkeypatch) -> None:
    """三处来源都没有 Key 时核对页被门禁挡住：连条款审查开关都不渲染。"""
    at = _logged_in_app(tmp_path, monkeypatch)
    assert not at.exception
    assert "未配置模型，合同核对不可用；配置后才能给出结论。" in _all_text(at)
    assert not any(c.label == "启用条款审查（模型辅助）" for c in at.checkbox)
    assert not any(b.label == "开始核对" for b in at.button)


def test_check_page_unconfigured_does_not_degrade_to_template(tmp_path, monkeypatch) -> None:
    """缺 Key 不是“系统故障”，也不给模板回答：页面只说明影响并指路设置页。"""
    at = _logged_in_app(tmp_path, monkeypatch)
    texts = _all_text(at)
    assert "系统故障" not in texts
    assert "处理建议" in texts
    assert "生成式问答" in texts
    assert "条款级审查" in texts
    assert not any(h.value == "核对结果" for h in at.subheader)
