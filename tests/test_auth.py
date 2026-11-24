"""账号与权限测试：密码不落明文、登录不泄露账号存在性、管理端门禁。"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role, User
from mla.services.auth import (
    _ITER,
    _LEGACY_ITER,
    authenticate,
    change_password,
    create_first_admin,
    has_admin,
    hash_password,
    is_admin,
    register,
    require_admin,
    verify_password,
)

pytestmark = pytest.mark.no_llm


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "auth.sqlite3"),
    )


def test_hash_is_salted_and_verifiable() -> None:
    a, salt_a = hash_password("secret123")
    b, salt_b = hash_password("secret123")
    assert salt_a != salt_b  # 每次盐不同
    assert a != b  # 因此哈希不同
    assert verify_password("secret123", a, salt_a, _ITER)
    assert not verify_password("wrong", a, salt_a, _ITER)


def test_register_and_authenticate(tmp_path) -> None:
    repo = _repo(tmp_path)
    user = register(repo, "alice", "secret123")
    assert user.role is Role.USER
    assert authenticate(repo, "alice", "secret123") is not None
    assert authenticate(repo, "alice", "bad") is None
    assert authenticate(repo, "nobody", "secret123") is None


def test_duplicate_username_rejected(tmp_path) -> None:
    repo = _repo(tmp_path)
    register(repo, "alice", "secret123")
    with pytest.raises(ValueError):
        register(repo, "alice", "secret123")


def test_short_password_rejected(tmp_path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        register(repo, "bob", "123")


def test_admin_gate(tmp_path) -> None:
    repo = _repo(tmp_path)
    admin = register(repo, "root", "secret123", role=Role.ADMIN)
    user = register(repo, "carol", "secret123")
    assert is_admin(admin) is True
    assert is_admin(user) is False
    require_admin(admin)  # 不抛异常
    with pytest.raises(PermissionError):
        require_admin(user)
    with pytest.raises(PermissionError):
        require_admin(None)


def test_has_admin_until_first_admin_created(tmp_path) -> None:
    """空库没有管理员；注册普通用户不算；建了管理员才算。"""
    repo = _repo(tmp_path)
    assert has_admin(repo) is False
    register(repo, "alice", "secret123")
    assert has_admin(repo) is False
    register(repo, "root", "secret123", role=Role.ADMIN)
    assert has_admin(repo) is True


def test_change_password_four_branches(tmp_path) -> None:
    """改口令：旧口令错 / 新口令过短 / 新旧相同 / 成功四种分支。"""
    repo = _repo(tmp_path)
    user = register(repo, "alice", "secret123")

    ok, msg = change_password(repo, user, "wrong-pass", "newsecret123")
    assert ok is False and msg == "当前口令不正确"

    ok, msg = change_password(repo, user, "secret123", "short")
    assert ok is False and msg == "新口令至少 8 位"

    ok, msg = change_password(repo, user, "secret123", "secret123")
    assert ok is False and msg == "新口令不能与当前口令相同"

    ok, msg = change_password(repo, user, "secret123", "newsecret123")
    assert ok is True and msg == ""
    assert authenticate(repo, "alice", "newsecret123") is not None
    assert authenticate(repo, "alice", "secret123") is None
    # 换了盐，哈希也跟着变
    updated = repo.get_user_by_name("alice")
    assert updated is not None
    assert updated.salt != user.salt
    assert updated.password_hash != user.password_hash


def test_create_first_admin_binds_enterprise(tmp_path) -> None:
    """首次初始化：建管理员 + 企业并绑定，建完能登录，has_admin 为 True。"""
    repo = _repo(tmp_path)
    admin = create_first_admin(repo, "root", "admin12345", "示范小微企业")
    assert is_admin(admin) is True
    assert admin.enterprise_id
    ent = repo.get_enterprise(admin.enterprise_id)
    assert ent is not None and ent.name == "示范小微企业"

    saved = repo.get_user_by_name("root")
    assert saved is not None and saved.enterprise_id == admin.enterprise_id
    assert authenticate(repo, "root", "admin12345") is not None
    assert has_admin(repo) is True


def test_create_first_admin_without_enterprise(tmp_path) -> None:
    """只给账号不给企业名：不建企业，enterprise_id 为空。"""
    repo = _repo(tmp_path)
    admin = create_first_admin(repo, "root", "admin12345")
    assert admin.enterprise_id == ""
    assert repo.list_enterprises() == []


def test_create_first_admin_rejects_short_password_and_duplicate(tmp_path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        create_first_admin(repo, "root", "short")
    assert has_admin(repo) is False  # 口令不足时不留半成品

    create_first_admin(repo, "root", "admin12345")
    with pytest.raises(ValueError):
        create_first_admin(repo, "root", "admin12345")


# ---------- 口令哈希迭代数与渐进升级 ----------
#
# 迭代数代际：迁移前 12 万，现在 60 万。库里每条记录自带迭代数（users.iterations），
# 验证按记录自己的值算，登录成功时顺手把旧代际的记录升到当前标准。


def _legacy_user(repo: SqliteRepository, username: str = "oldie", password: str = "secret123") -> User:
    """造一条“迁移前写入”的记录：12 万次哈希 + iterations 标成旧代际。"""
    digest, salt = hash_password(password, iterations=_LEGACY_ITER)
    user = User(
        user_id="U-legacy",
        username=username,
        password_hash=digest,
        salt=salt,
        iterations=_LEGACY_ITER,
    )
    repo.create_user(user)
    return user


def test_iteration_constants_are_pinned() -> None:
    """常量本身钉住：被谁改回旧值都要当场红，而不是等用户登不上才发现。"""
    assert _ITER == 600_000
    assert _LEGACY_ITER == 120_000


def test_legacy_user_login_is_upgraded(tmp_path) -> None:
    """存量账号：照常登录，库里迭代数与哈希被就地升级，再用新哈希登录一次仍成功。"""
    repo = _repo(tmp_path)
    legacy = _legacy_user(repo)

    user = authenticate(repo, "oldie", "secret123")
    assert user is not None
    assert user.iterations == _ITER  # 返回的是升级后的对象（会话指纹要用新 hash）

    saved = repo.get_user_by_name("oldie")
    assert saved is not None
    assert saved.iterations == _ITER
    assert saved.password_hash != legacy.password_hash  # 确实是重算的新哈希
    assert saved.salt != legacy.salt

    # 第二次登录走的是升级后的记录，不能因为换了哈希而登不上
    assert authenticate(repo, "oldie", "secret123") is not None


def test_register_writes_current_iterations(tmp_path) -> None:
    """新用户直接落当前迭代数。"""
    repo = _repo(tmp_path)
    created = register(repo, "alice", "secret123")
    assert created.iterations == _ITER
    saved = repo.get_user_by_name("alice")
    assert saved is not None and saved.iterations == _ITER


def test_change_password_writes_current_iterations(tmp_path) -> None:
    """旧代际账号改口令：按旧迭代数验旧口令，新哈希直接落当前迭代数。"""
    repo = _repo(tmp_path)
    legacy = _legacy_user(repo)

    ok, reason = change_password(repo, legacy, "secret123", "newsecret123")
    assert ok is True and reason == ""
    saved = repo.get_user_by_name("oldie")
    assert saved is not None and saved.iterations == _ITER
    assert authenticate(repo, "oldie", "newsecret123") is not None
    assert authenticate(repo, "oldie", "secret123") is None


def test_wrong_password_rejected_at_both_iterations(tmp_path) -> None:
    """两个代际的记录都用各自迭代数验证；错口令一律拒绝，且不会触发升级回写。"""
    repo = _repo(tmp_path)
    register(repo, "newbie", "secret123")
    legacy = _legacy_user(repo)

    assert authenticate(repo, "newbie", "wrong-pass") is None
    assert authenticate(repo, "oldie", "wrong-pass") is None

    untouched = repo.get_user_by_name("oldie")
    assert untouched is not None
    assert untouched.iterations == _LEGACY_ITER  # 验证失败不能顺手升级
    assert untouched.password_hash == legacy.password_hash


def test_upgrade_writeback_failure_does_not_block_login(tmp_path, monkeypatch, caplog) -> None:
    """回写失败（只读库 / 磁盘满）也不能把人锁在门外：本次照常登录，只留一条不含口令的日志。"""
    repo = _repo(tmp_path)
    legacy = _legacy_user(repo)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo, "set_user_password", boom)
    with caplog.at_level(logging.WARNING):
        user = authenticate(repo, "oldie", "secret123")

    assert user is not None
    assert user.iterations == _LEGACY_ITER  # 没写成功，返回的仍是旧记录
    saved = repo.get_user_by_name("oldie")
    assert saved is not None and saved.iterations == _LEGACY_ITER
    assert saved.password_hash == legacy.password_hash
    # 日志只记 user_id：既不能有明文口令，也不能有 hash
    assert "U-legacy" in caplog.text
    assert "secret123" not in caplog.text
    assert legacy.password_hash not in caplog.text


def test_migration_is_idempotent_and_adds_iterations_once(tmp_path) -> None:
    """同一个库初始化两次不报错，iterations 列只加了一次。"""
    db_path = str(tmp_path / "migrate.sqlite3")
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))

    SqliteRepository(paths=paths, db_path=db_path).close()
    SqliteRepository(paths=paths, db_path=db_path).close()  # 第二次初始化：迁移必须幂等

    with sqlite3.connect(db_path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
    assert cols.count("iterations") == 1


def test_legacy_db_without_iterations_column_still_logs_in(tmp_path) -> None:
    """真正的存量库：老 schema 里没有 iterations 列，迁移后默认 12 万，账号照常登录并被升级。"""
    db_path = str(tmp_path / "legacy.sqlite3")
    digest, salt = hash_password("secret123", iterations=_LEGACY_ITER)
    with sqlite3.connect(db_path) as conn:
        # 迁移前（0.4.0 之前）的 users 表：没有 onboarded，也没有 iterations
        conn.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
            " password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT '用户',"
            " enterprise_id TEXT DEFAULT '', created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO users(user_id,username,password_hash,salt,role,enterprise_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("U-old", "oldie", digest, salt, "用户", "", "2024-01-01T00:00:00"),
        )

    repo = SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=db_path,
    )
    migrated = repo.get_user_by_name("oldie")
    assert migrated is not None and migrated.iterations == _LEGACY_ITER  # 列默认值 = 旧代际

    user = authenticate(repo, "oldie", "secret123")
    assert user is not None and user.iterations == _ITER
    saved = repo.get_user_by_name("oldie")
    assert saved is not None and saved.iterations == _ITER
