"""设置表（键值对）与首次登录引导标记：读写往返、覆盖写、旧库补列后仍可用。"""

from __future__ import annotations

import sqlite3

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import User
from mla.services.auth import hash_password


def _repo(tmp_path) -> SqliteRepository:
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=str(tmp_path / "settings.sqlite3"))


def _make_user(repo: SqliteRepository) -> User:
    digest, salt = hash_password("secret123")
    user = User(user_id="U1", username="alice", password_hash=digest, salt=salt)
    repo.create_user(user)
    return user


def test_setting_roundtrip_and_overwrite(tmp_path) -> None:
    """缺键返回空串；写入后可读；再写覆盖旧值。"""
    repo = _repo(tmp_path)
    assert repo.get_setting("llm.api_key") == ""

    repo.set_setting("llm.api_key", "sk-first")
    assert repo.get_setting("llm.api_key") == "sk-first"

    repo.set_setting("llm.api_key", "sk-second")
    assert repo.get_setting("llm.api_key") == "sk-second"
    # 覆盖写不是新增一行，且刷新 updated_at
    conn = sqlite3.connect(str(tmp_path / "settings.sqlite3"))
    try:
        counted = conn.execute("SELECT COUNT(*) FROM settings WHERE key='llm.api_key'").fetchone()
        stamped = conn.execute("SELECT updated_at FROM settings WHERE key='llm.api_key'").fetchone()
    finally:
        conn.close()
    assert counted is not None and counted[0] == 1
    assert stamped is not None and stamped[0]


def test_setting_survives_reopen(tmp_path) -> None:
    """设置落库：换一个仓库实例（重启）仍读得到。"""
    repo = _repo(tmp_path)
    repo.set_setting("llm.model", "deepseek-chat")
    reopened = SqliteRepository(paths=repo.paths, db_path=str(tmp_path / "settings.sqlite3"))
    assert reopened.get_setting("llm.model") == "deepseek-chat"


def test_onboarded_defaults_false_and_persists(tmp_path) -> None:
    """新账号默认未引导；标记后为 True，重启仍保持。"""
    repo = _repo(tmp_path)
    user = _make_user(repo)
    assert user.onboarded is False
    fetched = repo.get_user_by_name("alice")
    assert fetched is not None and fetched.onboarded is False

    repo.mark_onboarded(user.user_id)
    after = repo.get_user_by_name("alice")
    assert after is not None and after.onboarded is True
    assert [u.onboarded for u in repo.list_users()] == [True]

    reopened = SqliteRepository(paths=repo.paths, db_path=str(tmp_path / "settings.sqlite3"))
    persisted = reopened.get_user_by_name("alice")
    assert persisted is not None and persisted.onboarded is True


def test_onboarded_flag_can_be_set_on_insert(tmp_path) -> None:
    """create_user 也写 onboarded（历史导入的账号可直接标为已引导）。"""
    repo = _repo(tmp_path)
    digest, salt = hash_password("secret123")
    repo.create_user(
        User(
            user_id="U2",
            username="bob",
            password_hash=digest,
            salt=salt,
            onboarded=True,
        )
    )
    got = repo.get_user_by_name("bob")
    assert got is not None and got.onboarded is True


def test_legacy_users_table_without_onboarded_is_migrated(tmp_path) -> None:
    """旧库的 users 表没有 onboarded 列：迁移后老账号读成 False，且能正常标记。"""
    db = tmp_path / "legacy_users.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (
          user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, salt TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT '用户', enterprise_id TEXT DEFAULT '', created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
        ("U-OLD", "olduser", "hash", "00", "管理员", "", "2026-11-01"),
    )
    conn.commit()
    conn.close()

    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    repo = SqliteRepository(paths=paths, db_path=str(db))
    old = repo.get_user_by_name("olduser")
    assert old is not None
    assert old.onboarded is False  # 补列后老记录默认 0

    repo.mark_onboarded("U-OLD")
    marked = repo.get_user_by_name("olduser")
    assert marked is not None and marked.onboarded is True

    # 迁移后新账号照常可写
    digest, salt = hash_password("secret123")
    repo.create_user(User(user_id="U-NEW", username="newuser", password_hash=digest, salt=salt))
    fresh = repo.get_user_by_name("newuser")
    assert fresh is not None and fresh.onboarded is False
