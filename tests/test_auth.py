"""账号与权限测试：密码不落明文、登录不泄露账号存在性、管理端门禁。"""

from __future__ import annotations

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import Paths
from mla.domain.models import Role
from mla.services.auth import (
    authenticate,
    hash_password,
    is_admin,
    register,
    require_admin,
    verify_password,
)


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml"),
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
