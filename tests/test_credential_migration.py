"""首次凭据迁移（规格 002 第 7 章）：成功流程、失败分支、幂等、并发与旧数据去留。

全部用例只碰 `tmp_path` 里的哑库与哑 Key（固定 `sk-dummy-` 前缀）：
真实库、真实 Key 不进入任何断言、文件或输出。Windows 的 ACL 收紧一律打桩，
测试不调用真实 `whoami` / `icacls`。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mla.adapters import auth_file
from mla.adapters.auth_file import CredentialError
from mla.services import bootstrap
from mla.services.bootstrap import (
    LEGACY_SETTING_KEY,
    MIGRATION_FILE,
    MIGRATION_FLAG,
    ensure_runtime_ready,
    migrate_legacy_key,
)
from mla.services.credentials import clear_key, resolve_key

DUMMY_KEY = "sk-dummy-" + "0" * 26
OTHER_KEY = "sk-dummy-" + "1" * 26
KEY_PREFIX = "sk-dummy-"


@pytest.fixture(autouse=True)
def fake_acl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 whoami / icacls 换成可预测的假输出：迁移里的写入不碰真实 ACL。"""
    state: dict[str, Any] = {"commands": []}

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        state["commands"].append(args)
        if args[0] == "whoami":
            return subprocess.CompletedProcess(args, 0, "TESTPC\\tester\n", "")
        return subprocess.CompletedProcess(args, 0, f"{args[1]} TESTPC\\tester:(F)\n", "")

    monkeypatch.setattr(auth_file, "_run_cmd", fake_run)
    return state


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """本用例专属 MLA_HOME（不预先创建）。"""
    return tmp_path / "mla-home"


def _legacy_db(path: Path, *, key: str | None = DUMMY_KEY, extra: dict[str, str] | None = None) -> Path:
    """造一份哑旧库：settings 表（可选 Key 与额外设置）+ 一张业务表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES ('U-1', 'alice')")
        if key is not None:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (LEGACY_SETTING_KEY, key))
        for name, value in (extra or {}).items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (name, value))
    return path


def _settings(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT key, value FROM settings").fetchall())


def _users(path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute("SELECT user_id, username FROM users").fetchall())


def _marker(home: Path) -> dict[str, Any]:
    return json.loads((home / MIGRATION_FILE).read_text(encoding="utf-8"))


def _auth(home: Path) -> dict[str, Any]:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))


def _assert_no_key_leak(report: bootstrap.MigrationReport) -> None:
    """面向用户的消息既不带 Key，也不带 Key 的任何片段。"""
    for message in report.messages:
        assert KEY_PREFIX not in message
        assert DUMMY_KEY not in message and OTHER_KEY not in message


# ---------------------------------------------------------------- 成功流程


def test_first_run_imports_key_and_cleans_legacy_row(home: Path, tmp_path: Path) -> None:
    """AC-03：值相等、来源 auth_file、旧行已删、其他 settings 与业务表不变、标记完成。"""
    db = _legacy_db(tmp_path / "mla.sqlite3", extra={"foo": "bar"})

    report = migrate_legacy_key(home, [db])

    assert report.status == "imported"
    assert any("已从旧配置迁移到用户凭据文件" in m for m in report.messages)
    assert any(str(home / "auth.json") in m for m in report.messages)
    _assert_no_key_leak(report)

    entry = _auth(home)["deepseek"]
    assert entry == {"type": "api_key", "key": DUMMY_KEY}
    resolved = resolve_key("deepseek", env={}, home=home)
    assert (resolved.key, resolved.source) == (DUMMY_KEY, "auth_file")

    assert LEGACY_SETTING_KEY not in _settings(db)  # 旧行删掉
    assert _settings(db) == {"foo": "bar"}  # 其他 settings 保留
    assert _users(db) == [("U-1", "alice")]  # 业务数据不变
    assert db.is_file()  # 旧库文件本身保留
    assert _marker(home) == {MIGRATION_FLAG: "complete"}


def test_second_run_is_idempotent(home: Path, tmp_path: Path) -> None:
    """连续调用结果一致：第二次是 skipped，文件与库都不再变动。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")
    assert migrate_legacy_key(home, [db]).status == "imported"
    auth_before = (home / "auth.json").read_bytes()
    marker_before = (home / MIGRATION_FILE).read_bytes()
    settings_before = _settings(db)

    again = migrate_legacy_key(home, [db])

    assert again.status == "skipped"
    assert again.messages == []
    assert (home / "auth.json").read_bytes() == auth_before
    assert (home / MIGRATION_FILE).read_bytes() == marker_before
    assert _settings(db) == settings_before


def test_cleared_key_does_not_come_back(home: Path, tmp_path: Path) -> None:
    """清除文件凭据后再迁移仍是 skipped：旧值不复活。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")
    assert migrate_legacy_key(home, [db]).status == "imported"

    clear_key("deepseek", home=home)
    assert "deepseek" not in _auth(home)

    report = migrate_legacy_key(home, [db])

    assert report.status == "skipped"
    assert "deepseek" not in _auth(home)
    assert resolve_key("deepseek", env={}, home=home).source == "none"


def test_existing_auth_file_is_not_overwritten(home: Path, tmp_path: Path) -> None:
    """已有合法文件凭据：只清理旧行并标记，`auth.json` 字节不变。"""
    home.mkdir(parents=True)
    (home / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": OTHER_KEY}}), encoding="utf-8"
    )
    before = (home / "auth.json").read_bytes()
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "already_configured"
    assert (home / "auth.json").read_bytes() == before
    assert _auth(home)["deepseek"]["key"] == OTHER_KEY
    assert LEGACY_SETTING_KEY not in _settings(db)
    assert _marker(home) == {MIGRATION_FLAG: "complete"}
    _assert_no_key_leak(report)


def test_both_databases_are_cleaned(home: Path, tmp_path: Path) -> None:
    """home 与 cwd 各有一份旧库：两处的 `llm_api_key` 行都要清理。"""
    home_db = _legacy_db(tmp_path / "home-copy.sqlite3", key=DUMMY_KEY)
    cwd_db = _legacy_db(tmp_path / "cwd.sqlite3", key=DUMMY_KEY)

    report = migrate_legacy_key(home, [home_db, cwd_db])

    assert report.status == "imported"
    assert LEGACY_SETTING_KEY not in _settings(home_db)
    assert LEGACY_SETTING_KEY not in _settings(cwd_db)
    assert _auth(home)["deepseek"]["key"] == DUMMY_KEY


def test_ensure_runtime_ready_runs_migration(home: Path, tmp_path: Path) -> None:
    """入口接线：引导复制旧库后立刻迁移，并同时清理新旧两处旧 Key。"""
    cwd = tmp_path / "old-cwd"
    legacy = _legacy_db(cwd / "mla.sqlite3", extra={"foo": "bar"})

    report = ensure_runtime_ready(env={"MLA_HOME": str(home)}, cwd=cwd)

    assert report.db_path == str(home / "mla.sqlite3")
    assert report.copied_from == str(legacy)
    assert report.migration.status == "imported"
    assert LEGACY_SETTING_KEY not in _settings(Path(report.db_path))
    assert LEGACY_SETTING_KEY not in _settings(legacy)
    assert _settings(Path(report.db_path)) == {"foo": "bar"}
    assert _auth(home)["deepseek"]["key"] == DUMMY_KEY
    assert _marker(home) == {MIGRATION_FLAG: "complete"}


# ---------------------------------------------------------------- 没有候选


def test_no_legacy_database_leaves_no_trace(home: Path, tmp_path: Path) -> None:
    """没有旧库：不建空凭据、不写标记、不创建目录（之后从真正的旧目录启动仍能迁移）。"""
    report = migrate_legacy_key(home, [tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"])

    assert report.status == "no_candidate"
    assert report.messages == []
    assert not home.exists()
    assert not (home / "auth.json").exists()
    assert not (home / MIGRATION_FILE).exists()


def test_container_without_key_is_truly_empty(home: Path, tmp_path: Path) -> None:
    """旧库存在但 settings 里没有该键：no_candidate，不写标记。"""
    db = _legacy_db(tmp_path / "mla.sqlite3", key=None, extra={"foo": "bar"})

    report = migrate_legacy_key(home, [db])

    assert report.status == "no_candidate"
    assert not (home / MIGRATION_FILE).exists()
    assert not (home / "auth.json").exists()
    assert _settings(db) == {"foo": "bar"}


def test_corrupt_marker_is_treated_as_not_migrated(home: Path, tmp_path: Path) -> None:
    """标记文件损坏或形状不对：当作未迁移，继续尝试并重新写入。"""
    home.mkdir(parents=True)
    (home / MIGRATION_FILE).write_text("{oops", encoding="utf-8")
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "imported"
    assert _marker(home) == {MIGRATION_FLAG: "complete"}


def test_complete_marker_stops_reading_legacy_db(home: Path, tmp_path: Path) -> None:
    """已有完成标记就不再读旧库：即使旧库还有 Key，也不清理、不导入。"""
    home.mkdir(parents=True)
    (home / MIGRATION_FILE).write_text(json.dumps({MIGRATION_FLAG: "complete"}), encoding="utf-8")
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "skipped"
    assert _settings(db)[LEGACY_SETTING_KEY] == DUMMY_KEY  # 未被读取也未清理
    assert not (home / "auth.json").exists()


def test_cli_reports_migration_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 入口把迁移结果打到 stderr（含结论与路径，不含 Key），并接上后续命令。"""
    from mla.cli import main

    legacy = _legacy_db(tmp_path / "mla.sqlite3")
    monkeypatch.delenv("MLA_DB", raising=False)  # 让默认路径回到 MLA_HOME + cwd
    monkeypatch.chdir(tmp_path)

    code = main(["check", "--question", "离婚时房产怎么分割"])

    captured = capsys.readouterr()
    assert "已从旧配置迁移到用户凭据文件" in captured.err
    assert DUMMY_KEY not in captured.out + captured.err
    assert KEY_PREFIX not in captured.out + captured.err
    assert LEGACY_SETTING_KEY not in _settings(legacy)
    assert code == 0


# ---------------------------------------------------------------- 失败分支


def test_corrupt_legacy_db_read_failure(home: Path, tmp_path: Path) -> None:
    """旧库不是合法 SQLite：failed + 读取失败文案，旧数据保留、无标记、无凭据文件。"""
    db = tmp_path / "mla.sqlite3"
    db.write_bytes(b"not a sqlite database")
    before = db.read_bytes()

    report = migrate_legacy_key(home, [db])

    assert report.status == "failed"
    assert any("旧凭据读取失败，请重新保存 Key" in m for m in report.messages)
    _assert_no_key_leak(report)
    assert db.read_bytes() == before
    assert not (home / MIGRATION_FILE).exists()
    assert not (home / "auth.json").exists()


def test_locked_legacy_db_read_failure(home: Path, tmp_path: Path) -> None:
    """旧库被独占锁住（读不出来）：同样按失败处理，绝不当成"旧库无 Key"。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")
    holder = sqlite3.connect(str(db))
    try:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO settings (key, value) VALUES ('locked', '1')")
        report = migrate_legacy_key(home, [db])
    finally:
        holder.rollback()
        holder.close()

    assert report.status == "failed"
    assert any("旧凭据读取失败" in m for m in report.messages)
    assert LEGACY_SETTING_KEY in _settings(db)
    assert not (home / MIGRATION_FILE).exists()


def test_write_failure_keeps_legacy_row(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`set_key` 抛 `CredentialError`：failed，旧行仍在、无标记、不生成凭据文件。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")

    def boom(provider: str, key: str, *, home: Path) -> None:
        raise CredentialError(f"写入凭据文件失败：{home / 'auth.json'}")

    monkeypatch.setattr(bootstrap, "_set_key_unlocked", boom)

    report = migrate_legacy_key(home, [db])

    assert report.status == "failed"
    assert any("凭据迁移失败，请重新保存 Key" in m for m in report.messages)
    _assert_no_key_leak(report)
    assert _settings(db)[LEGACY_SETTING_KEY] == DUMMY_KEY
    assert not (home / MIGRATION_FILE).exists()
    assert not (home / "auth.json").exists()


def test_cleanup_failure_keeps_new_file_but_no_marker(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧行删除失败：failed + `旧凭据尚未清理`，已验证的新文件保留、不写标记。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")

    def boom(candidates: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bootstrap, "_delete_legacy_keys", boom)

    report = migrate_legacy_key(home, [db])

    assert report.status == "failed"
    assert any("旧凭据尚未清理" in m for m in report.messages)
    assert _auth(home)["deepseek"]["key"] == DUMMY_KEY
    assert _settings(db)[LEGACY_SETTING_KEY] == DUMMY_KEY
    assert not (home / MIGRATION_FILE).exists()


def test_marker_failure_keeps_file_and_is_completed_next_run(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """标记写入失败：新文件保留；下次启动只补清理与标记，不覆盖已有 Key。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")
    real_write = bootstrap._write_marker

    def boom(home_path: Path) -> None:
        raise OSError("磁盘写满")

    monkeypatch.setattr(bootstrap, "_write_marker", boom)
    first = migrate_legacy_key(home, [db])

    assert first.status == "failed"
    assert any("迁移完成但状态标记写入失败" in m for m in first.messages)
    assert _auth(home)["deepseek"]["key"] == DUMMY_KEY
    assert not (home / MIGRATION_FILE).exists()
    auth_before = (home / "auth.json").read_bytes()

    monkeypatch.setattr(bootstrap, "_write_marker", real_write)
    second = migrate_legacy_key(home, [db])

    assert second.status == "already_configured"
    assert (home / "auth.json").read_bytes() == auth_before  # Key 没被重复覆盖
    assert _marker(home) == {MIGRATION_FLAG: "complete"}
    assert LEGACY_SETTING_KEY not in _settings(db)


def test_corrupt_auth_file_is_reported_without_touching_it(home: Path, tmp_path: Path) -> None:
    """`auth.json` 已损坏：failed + 可操作提示，不自动改名、不覆盖、不生成备份。"""
    home.mkdir(parents=True)
    path = home / "auth.json"
    path.write_text("{oops", encoding="utf-8")
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "failed"
    assert any("重新保存 Key" in m for m in report.messages)
    assert any(str(path) in m for m in report.messages)
    assert path.read_text(encoding="utf-8") == "{oops"
    assert _settings(db)[LEGACY_SETTING_KEY] == DUMMY_KEY
    assert not (home / MIGRATION_FILE).exists()


def test_lock_is_not_stolen(home: Path, tmp_path: Path) -> None:
    """锁已被占用：failed + 另一项配置操作正在进行，不抢锁、不改旧库。"""
    home.mkdir(parents=True)
    lock = home / "auth.lock"
    lock.mkdir()
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "failed"
    assert any("另一项配置操作正在进行" in m for m in report.messages)
    _assert_no_key_leak(report)
    assert lock.is_dir()  # 遗留锁原样保留，由用户确认后手工移除
    assert LEGACY_SETTING_KEY in _settings(db)
    assert not (home / "auth.json").exists()
    assert not (home / MIGRATION_FILE).exists()


def test_migration_messages_never_contain_key(home: Path, tmp_path: Path) -> None:
    """成功消息同样只给路径与结论：整段输出里不出现哑 Key，也不出现它的长度。"""
    db = _legacy_db(tmp_path / "mla.sqlite3")

    report = migrate_legacy_key(home, [db])

    assert report.status == "imported"
    blob = "\n".join(report.messages)
    assert KEY_PREFIX not in blob and DUMMY_KEY not in blob
    assert str(len(DUMMY_KEY)) not in blob
