"""凭据服务与 `auth.json` 适配：优先级、损坏口径、原子写入、权限与锁。

全部用哑 Key（固定 `sk-dummy-` 前缀），真实 Key 不进入任何断言或文件。
Windows 的 ACL 路径一律打桩 `_run_cmd`，测试不调用真实 `icacls`；
POSIX 权限位断言只在非 Windows 运行。
"""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mla.adapters import auth_file
from mla.adapters.auth_file import CredentialError
from mla.services.credentials import (
    clear_key,
    list_key_status,
    read_check_status,
    record_check_status,
    resolve_key,
    resolve_llm_config,
    set_key,
)

DUMMY_KEY = "sk-dummy-" + "0" * 26
OTHER_KEY = "sk-dummy-" + "1" * 26
KEY_PREFIX = "sk-dummy-"

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """本用例专属 MLA_HOME（不预先创建）。"""
    return tmp_path / "mla-home"


@pytest.fixture(autouse=True)
def fake_acl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 whoami / icacls 全部换成可预测的假输出，测试不触碰真实 ACL。"""
    state: dict[str, Any] = {
        "user": "TESTPC\\tester",
        "icacls_returncode": 0,
        "command_error": None,
        "acl_text": "",
        "commands": [],
    }

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        error = state["command_error"]
        if error is not None:
            raise error
        state["commands"].append(list(args))
        if args[0] == "whoami":
            return subprocess.CompletedProcess(args, 0, f"{state['user']}\n", "")
        if len(args) == 2 and args[0] == "icacls":  # 读回校验
            text = state["acl_text"] or (
                f"{args[1]} {state['user']}:(F)\n"
                "                     NT AUTHORITY\\SYSTEM:(F)\n"
                "\n已成功处理 1 个文件；处理 0 个文件时失败\n"
            )
            return subprocess.CompletedProcess(args, 0, text, "")
        code = int(state["icacls_returncode"])
        err = "" if code == 0 else "拒绝访问。"
        return subprocess.CompletedProcess(args, code, "", err)

    monkeypatch.setattr(auth_file, "_run_cmd", fake_run)
    return state


# ---------------------------------------------------------------- 解析


def test_missing_file_resolves_to_none_and_creates_nothing(home: Path) -> None:
    resolved = resolve_key("deepseek", env={}, home=home)
    assert resolved.key == ""
    assert resolved.source == "none"
    assert resolved.label == "未配置"
    assert resolved.origin == ""
    assert not home.exists()


def test_set_key_roundtrip(home: Path) -> None:
    set_key("deepseek", f"  {DUMMY_KEY}  ", home=home)
    data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert data == {"deepseek": {"type": "api_key", "key": DUMMY_KEY}}
    resolved = resolve_key("deepseek", env={}, home=home)
    assert resolved.key == DUMMY_KEY
    assert resolved.source == "auth_file"
    assert resolved.label == "用户凭据文件（auth.json）"
    assert resolved.origin == str(home / "auth.json")


def test_env_wins_over_file_and_is_not_persisted(home: Path) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    before = (home / "auth.json").read_bytes()
    resolved = resolve_key("deepseek", env={"MLA_LLM_API_KEY": f" {OTHER_KEY} "}, home=home)
    assert resolved.key == OTHER_KEY
    assert resolved.source == "environment"
    assert resolved.label == "环境变量（MLA_LLM_API_KEY）"
    assert resolved.origin == "MLA_LLM_API_KEY"
    assert (home / "auth.json").read_bytes() == before


BAD_DOCUMENTS = {
    "invalid_json": "{not json",
    "top_level_array": json.dumps([{"type": "api_key", "key": DUMMY_KEY}]),
    "missing_key": json.dumps({"deepseek": {"type": "api_key"}}),
    "key_not_string": json.dumps({"deepseek": {"type": "api_key", "key": 35}}),
    "wrong_type": json.dumps({"deepseek": {"type": "oauth", "key": DUMMY_KEY}}),
    "blank_key": json.dumps({"deepseek": {"type": "api_key", "key": "   "}}),
}


@pytest.mark.parametrize("payload", list(BAD_DOCUMENTS.values()), ids=list(BAD_DOCUMENTS))
def test_corrupt_auth_file_raises_and_keeps_bytes(home: Path, payload: str) -> None:
    home.mkdir(parents=True)
    path = home / "auth.json"
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(CredentialError) as excinfo:
        resolve_key("deepseek", env={}, home=home)
    assert path.read_bytes() == before
    assert KEY_PREFIX not in str(excinfo.value)
    # 损坏文件不得被当成空文件覆盖
    with pytest.raises(CredentialError):
        set_key("deepseek", DUMMY_KEY, home=home)
    assert path.read_bytes() == before


def test_set_key_rejects_blank_input_without_writing(home: Path) -> None:
    with pytest.raises(CredentialError):
        set_key("deepseek", "", home=home)
    with pytest.raises(CredentialError):
        set_key("deepseek", "   ", home=home)
    assert not (home / "auth.json").exists()


def test_clear_key_keeps_other_providers(home: Path) -> None:
    home.mkdir(parents=True)
    path = home / "auth.json"
    aliyun = {"type": "api_key", "key": OTHER_KEY}
    path.write_text(json.dumps({"aliyun": aliyun}), encoding="utf-8")
    set_key("deepseek", DUMMY_KEY, home=home)
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"aliyun", "deepseek"}

    clear_key("deepseek", home=home)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"aliyun": aliyun}
    assert resolve_key("deepseek", env={}, home=home).source == "none"
    # 幂等：再清除一次仍然静默成功
    clear_key("deepseek", home=home)
    assert json.loads(path.read_text(encoding="utf-8")) == {"aliyun": aliyun}


def test_clear_key_on_missing_file_is_idempotent(home: Path) -> None:
    clear_key("deepseek", home=home)
    assert not (home / "auth.json").exists()


def test_repr_and_messages_never_contain_key(home: Path) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    resolved = resolve_key("deepseek", env={}, home=home)
    statuses = list_key_status(env={}, home=home)
    blobs = [repr(resolved), str(resolved), repr(statuses), str(statuses)]
    with pytest.raises(CredentialError) as blank_error:
        set_key("deepseek", "   ", home=home)
    blobs.append(str(blank_error.value))
    (home / "auth.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(CredentialError) as corrupt_error:
        resolve_key("deepseek", env={}, home=home)
    blobs.append(str(corrupt_error.value))
    for blob in blobs:
        assert KEY_PREFIX not in blob
        assert DUMMY_KEY not in blob


def test_list_key_status_excludes_key(home: Path) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    statuses = list_key_status(env={}, home=home)
    assert len(statuses) == 1
    status = statuses[0]
    assert status.provider == "deepseek"
    assert status.type == "api_key"
    assert status.source == "auth_file"
    assert status.label == "用户凭据文件（auth.json）"
    assert status.path == str(home / "auth.json")
    assert set(vars(status)) == {"provider", "type", "source", "label", "path"}
    assert KEY_PREFIX not in repr(status)


def test_list_key_status_when_unconfigured(home: Path) -> None:
    status = list_key_status(env={}, home=home)[0]
    assert status.source == "none"
    assert status.type == ""
    assert status.label == "未配置"


def test_resolve_llm_config_without_key(home: Path) -> None:
    cfg, resolved = resolve_llm_config(env={}, home=home)
    assert cfg is None
    assert resolved.source == "none"


def test_resolve_llm_config_injects_key(home: Path) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    cfg, resolved = resolve_llm_config(env={}, home=home)
    assert cfg is not None
    assert cfg.api_key == DUMMY_KEY
    assert cfg.model == "deepseek-chat"
    assert resolved.source == "auth_file"


# ---------------------------------------------------------------- 阶段三旧库兜底


def _legacy_db(tmp_path: Path, key: str = DUMMY_KEY) -> Path:
    """造一份带 `settings.llm_api_key` 的旧库（阶段三兜底的输入）。"""
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        conn.execute("INSERT INTO settings (key, value) VALUES ('llm_api_key', ?)", (key,))
    return db


def test_legacy_database_is_third_source(home: Path, tmp_path: Path) -> None:
    """旧库是第三级：标签固定为「兼容来源（旧数据库）」，且解析不创建 MLA_HOME。"""
    db = _legacy_db(tmp_path)

    resolved = resolve_key("deepseek", env={}, home=home, legacy_db=db)

    assert resolved.key == DUMMY_KEY
    assert resolved.source == "legacy_database"
    assert resolved.label == "兼容来源（旧数据库）"
    assert resolved.origin == str(db)
    assert not home.exists()  # 只读：不顺带建目录


def test_env_and_file_override_legacy_database(home: Path, tmp_path: Path) -> None:
    """env 最高，其次是 auth.json；旧库只在两处都没有时兜底。"""
    db = _legacy_db(tmp_path)

    env_key = resolve_key("deepseek", env={"MLA_LLM_API_KEY": OTHER_KEY}, home=home, legacy_db=db)
    assert (env_key.key, env_key.source) == (OTHER_KEY, "environment")

    set_key("deepseek", OTHER_KEY, home=home)
    file_key = resolve_key("deepseek", env={}, home=home, legacy_db=db)
    assert (file_key.key, file_key.source) == (OTHER_KEY, "auth_file")


def test_legacy_database_missing_is_not_created(home: Path, tmp_path: Path) -> None:
    """旧库不存在 → 当作空，返回 none，且绝不创建数据库文件。"""
    missing = tmp_path / "not-there.sqlite3"

    resolved = resolve_key("deepseek", env={}, home=home, legacy_db=missing)

    assert resolved.source == "none"
    assert not missing.exists()


def test_legacy_database_without_settings_table_degrades_to_none(home: Path, tmp_path: Path) -> None:
    """旧库存在但没有 settings 表（读失败）：按空处理，不抛错。"""
    db = tmp_path / "empty.sqlite3"
    sqlite3.connect(db).close()

    assert resolve_key("deepseek", env={}, home=home, legacy_db=db).source == "none"


def test_list_key_status_reports_legacy_label(home: Path, tmp_path: Path) -> None:
    """`list_key_status` 也能反映旧库来源（接口备好，UI 本批不改）。"""
    db = _legacy_db(tmp_path)

    status = list_key_status(env={}, home=home, legacy_db=db)[0]

    assert status.source == "legacy_database"
    assert status.label == "兼容来源（旧数据库）"
    assert status.type == "api_key"
    assert KEY_PREFIX not in repr(status) and KEY_PREFIX not in str(status)


def test_resolve_llm_config_passes_legacy_db(home: Path, tmp_path: Path) -> None:
    """`resolve_llm_config` 透传 legacy_db，且不覆盖校验超时等非秘密参数。"""
    db = _legacy_db(tmp_path)

    cfg, resolved = resolve_llm_config(env={}, home=home, legacy_db=db)

    assert cfg is not None and cfg.api_key == DUMMY_KEY
    assert cfg.auth_check_timeout_s == 10
    assert resolved.source == "legacy_database"


# ---------------------------------------------------------------- 权限


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX 权限位断言（Windows 上 chmod 只影响只读属性）")
def test_posix_permissions(home: Path) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600


@pytest.mark.skipif(not IS_WINDOWS, reason="仅 Windows 走 icacls 分支")
def test_windows_acl_commands(home: Path, fake_acl: dict[str, Any]) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    commands = fake_acl["commands"]
    assert any(cmd[:1] == ["icacls"] and cmd[-1] == "/inheritance:r" for cmd in commands)
    assert any(cmd[-2:] == ["/grant:r", "TESTPC\\tester:F"] for cmd in commands)
    assert any(cmd[-2:] == ["/grant:r", "SYSTEM:F"] for cmd in commands)
    # 替换后再读回校验一次正式文件
    assert ["icacls", str(home / "auth.json")] in commands


@pytest.mark.skipif(not IS_WINDOWS, reason="仅 Windows 走 icacls 分支")
def test_windows_acl_nonzero_keeps_old_file(home: Path, fake_acl: dict[str, Any]) -> None:
    set_key("deepseek", DUMMY_KEY, home=home)
    before = (home / "auth.json").read_bytes()
    fake_acl["icacls_returncode"] = 5
    with pytest.raises(CredentialError):
        set_key("deepseek", OTHER_KEY, home=home)
    assert (home / "auth.json").read_bytes() == before
    assert resolve_key("deepseek", env={}, home=home).key == DUMMY_KEY
    assert not [p.name for p in home.iterdir() if p.name.endswith(".tmp")]
    assert not auth_file.lock_dir(home).exists()


@pytest.mark.skipif(not IS_WINDOWS, reason="仅 Windows 走 icacls 分支")
def test_windows_acl_command_missing_raises(home: Path, fake_acl: dict[str, Any]) -> None:
    fake_acl["command_error"] = FileNotFoundError("icacls")
    with pytest.raises(CredentialError) as excinfo:
        set_key("deepseek", DUMMY_KEY, home=home)
    assert not (home / "auth.json").exists()
    assert KEY_PREFIX not in str(excinfo.value)
    assert not [p.name for p in home.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.skipif(not IS_WINDOWS, reason="仅 Windows 走 icacls 分支")
def test_windows_output_decoding_handles_utf8_and_ansi() -> None:
    """whoami 是 UTF-8、icacls 是 ANSI 代码页：两种字节流都要解成同一个用户名。"""
    import locale

    name = "TESTPC\\刘靖宇"
    assert auth_file._decode_output(name.encode("utf-8")) == name
    ansi = name.encode(locale.getpreferredencoding(False), errors="replace")
    assert auth_file._decode_output(ansi) == name
    assert auth_file._decode_output(b"") == ""


@pytest.mark.skipif(not IS_WINDOWS, reason="仅 Windows 走 icacls 分支")
def test_windows_acl_rejects_broad_subject(home: Path, fake_acl: dict[str, Any]) -> None:
    fake_acl["acl_text"] = f"{home / 'auth.json'} Everyone:(R)\nTESTPC\\tester:(F)\n"
    with pytest.raises(CredentialError):
        set_key("deepseek", DUMMY_KEY, home=home)
    assert not (home / "auth.json").exists()


# ---------------------------------------------------------------- 校验历史


def test_status_roundtrip(home: Path) -> None:
    record_check_status("deepseek", True, "ok", home=home)
    status = read_check_status("deepseek", home=home)
    assert status is not None
    assert status.ok is True
    assert status.kind == "ok"
    # ISO-8601 且带时区偏移（状态行渲染只到分钟）
    assert datetime.fromisoformat(status.checked_at).utcoffset() is not None
    raw = json.loads((home / "auth-status.json").read_text(encoding="utf-8"))
    assert set(raw["deepseek"]) == {"ok", "checked_at", "kind"}
    assert raw["deepseek"]["checked_at"] == status.checked_at
    # 状态文件里不含 Key
    assert KEY_PREFIX not in (home / "auth-status.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", sorted(auth_file.STATUS_KINDS))
def test_status_kind_enum_roundtrip(home: Path, kind: str) -> None:
    record_check_status("deepseek", kind == "ok", kind, "2026-11-13T15:20:11+08:00", home=home)
    status = read_check_status("deepseek", home=home)
    assert status is not None
    assert status.kind == kind
    assert status.checked_at == "2026-11-13T15:20:11+08:00"


def test_unknown_kind_is_rejected(home: Path) -> None:
    with pytest.raises(ValueError):
        record_check_status("deepseek", False, "timeout", home=home)


BAD_STATUS = {
    "invalid_json": "{oops",
    "top_level_array": "[]",
    "missing_timestamp": json.dumps({"deepseek": {"ok": True, "kind": "ok"}}),
    "naive_timestamp": json.dumps(
        {"deepseek": {"ok": True, "checked_at": "2026-11-13T15:20:11", "kind": "ok"}}
    ),
    "unknown_kind": json.dumps(
        {"deepseek": {"ok": True, "checked_at": "2026-11-13T15:20:11+08:00", "kind": "timeout"}}
    ),
    "extra_field": json.dumps(
        {
            "deepseek": {
                "ok": True,
                "checked_at": "2026-11-13T15:20:11+08:00",
                "kind": "ok",
                "api_key": DUMMY_KEY,
            }
        }
    ),
}


@pytest.mark.parametrize("payload", list(BAD_STATUS.values()), ids=list(BAD_STATUS))
def test_corrupt_status_degrades_to_none(home: Path, payload: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth-status.json").write_text(payload, encoding="utf-8")
    assert read_check_status("deepseek", home=home) is None
    # 校验历史不是凭据来源：损坏不影响解析，也不抛错
    set_key("deepseek", DUMMY_KEY, home=home)
    assert resolve_key("deepseek", env={}, home=home).key == DUMMY_KEY


def test_status_write_failure_raises_oserror(home: Path) -> None:
    """写校验历史失败只抛 OSError 由调用方降级：不阻断、不谎称已保存。"""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth-status.json").mkdir()  # 占位成目录，让原子替换失败
    with pytest.raises(OSError):
        record_check_status("deepseek", True, "ok", home=home)
    assert not [p.name for p in home.iterdir() if p.name.endswith(".tmp")]


# ---------------------------------------------------------------- 锁


def test_lock_blocks_concurrent_write(home: Path) -> None:
    auth_file.ensure_home_dir(home)
    lock = auth_file.lock_dir(home)
    lock.mkdir()
    try:
        with pytest.raises(CredentialError):
            set_key("deepseek", DUMMY_KEY, home=home)
        assert not (home / "auth.json").exists()
        with pytest.raises(CredentialError):
            clear_key("deepseek", home=home)
        # 不自动抢锁：锁目录仍在
        assert lock.exists()
    finally:
        lock.rmdir()
    set_key("deepseek", DUMMY_KEY, home=home)
    assert resolve_key("deepseek", env={}, home=home).key == DUMMY_KEY
    assert not lock.exists()
