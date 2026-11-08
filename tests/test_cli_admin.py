"""admin 子命令：列账号、本地重置口令、提升管理员。

本地单机应用没有邮箱，找回只能靠本机终端；这里验命令行行为与输出里不留口令敏感信息。
"""

from __future__ import annotations

import pytest

from mla.adapters.persist import SqliteRepository
from mla.cli import main
from mla.domain.models import Enterprise, Role
from mla.services.auth import authenticate, has_admin, register


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例一个临时库，不碰工作目录下的 mla.sqlite3。"""
    monkeypatch.setenv("MLA_DB", str(tmp_path / "admin.sqlite3"))


def _repo() -> SqliteRepository:
    return SqliteRepository()


def _printed_password(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("新口令："):
            return line.split("：", 1)[1].strip()
    return ""


def test_admin_list_prints_accounts_without_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    """列出账号给出用户名、角色与企业绑定，不出现哈希与盐。"""
    repo = _repo()
    repo.save_enterprise(Enterprise(enterprise_id="E-1", name="示例企业"))
    register(repo, "admin", "secret123", Role.ADMIN, "E-1")
    register(repo, "alice", "secret123", Role.USER)

    assert main(["admin", "list"]) == 0
    out = capsys.readouterr().out
    assert "admin｜管理员｜已绑定企业" in out
    assert "alice｜用户｜未绑定企业" in out
    for name in ("admin", "alice"):
        user = repo.get_user_by_name(name)
        assert user is not None
        assert user.password_hash not in out
        assert user.salt not in out
        assert "secret123" not in out


def test_admin_list_on_empty_db(capsys: pytest.CaptureFixture[str]) -> None:
    """空库不报错，只提示尚无账号。"""
    assert main(["admin", "list"]) == 0
    assert "尚无账号" in capsys.readouterr().out


def test_admin_reset_generates_password_shown_once(capsys: pytest.CaptureFixture[str]) -> None:
    """不传口令：生成 16 位随机口令、只打印一次，新口令能登录，旧口令失效。"""
    repo = _repo()
    register(repo, "admin", "oldpass123", Role.ADMIN)

    assert main(["admin", "reset", "--username", "admin"]) == 0
    out = capsys.readouterr().out
    pwd = _printed_password(out)
    assert len(pwd) == 16
    assert any(c.isalpha() for c in pwd) and any(c.isdigit() for c in pwd)
    assert out.count(pwd) == 1
    assert "请立即记录" in out
    assert authenticate(_repo(), "admin", pwd) is not None
    assert authenticate(_repo(), "admin", "oldpass123") is None


def test_admin_reset_accepts_explicit_password(capsys: pytest.CaptureFixture[str]) -> None:
    """显式给的口令不回显，重置后能用。"""
    repo = _repo()
    register(repo, "admin", "oldpass123", Role.ADMIN)

    assert main(["admin", "reset", "--username", "admin", "--password", "newpass123"]) == 0
    out = capsys.readouterr().out
    assert "newpass123" not in out
    assert _printed_password(out) == ""
    assert authenticate(_repo(), "admin", "newpass123") is not None


def test_admin_reset_rejects_short_password(capsys: pytest.CaptureFixture[str]) -> None:
    """不足 8 位直接退出 1，原口令仍可用。"""
    repo = _repo()
    register(repo, "admin", "oldpass123", Role.ADMIN)

    assert main(["admin", "reset", "--username", "admin", "--password", "short"]) == 1
    captured = capsys.readouterr()
    assert "至少 8 位" in captured.err
    assert authenticate(_repo(), "admin", "oldpass123") is not None


def test_admin_reset_unknown_user_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """账号不存在退出 1，并说明是哪个账号。"""
    assert main(["admin", "reset", "--username", "nobody"]) == 1
    assert "账号不存在：nobody" in capsys.readouterr().err


def test_admin_promote_makes_ordinary_user_admin(capsys: pytest.CaptureFixture[str]) -> None:
    """提升后 has_admin 为真，角色落库为管理员。"""
    repo = _repo()
    register(repo, "alice", "secret123", Role.USER)
    assert has_admin(_repo()) is False

    assert main(["admin", "promote", "--username", "alice"]) == 0
    assert "已提升为管理员：alice" in capsys.readouterr().out
    assert has_admin(_repo()) is True
    promoted = _repo().get_user_by_name("alice")
    assert promoted is not None and promoted.role is Role.ADMIN


def test_admin_promote_existing_admin_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """已是管理员时只提示，退出码仍为 0。"""
    repo = _repo()
    register(repo, "admin", "secret123", Role.ADMIN)

    assert main(["admin", "promote", "--username", "admin"]) == 0
    assert "已是管理员" in capsys.readouterr().out


def test_admin_promote_unknown_user_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """账号不存在时退出 1。"""
    assert main(["admin", "promote", "--username", "nobody"]) == 1
    assert "账号不存在：nobody" in capsys.readouterr().err
