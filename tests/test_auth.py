"""账号与权限测试：密码不落明文、登录不泄露账号存在性、管理端门禁。"""

from __future__ import annotations

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role
from mla.services.auth import (
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
    assert verify_password("secret123", a, salt_a)
    assert not verify_password("wrong", a, salt_a)


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
