"""命令行 init 的口令策略：不传口令就生成随机口令，仓库里不再有可猜的默认口令。"""

from __future__ import annotations

import pytest

from mla.adapters.persist import SqliteRepository
from mla.cli import main
from mla.services.auth import authenticate


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例一个临时库，不碰工作目录下的 mla.sqlite3。"""
    monkeypatch.setenv("MLA_DB", str(tmp_path / "cli.sqlite3"))


def _printed_password(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("管理员口令："):
            return line.split("：", 1)[1].strip()
    return ""


def test_init_generates_random_password_when_omitted(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "--admin-username", "admin", "--enterprise", "示例企业"]) == 0
    out = capsys.readouterr().out
    pwd = _printed_password(out)
    assert len(pwd) == 16
    assert any(c.isalpha() for c in pwd) and any(c.isdigit() for c in pwd)
    assert out.count(pwd) == 1  # 只打印一次
    assert "请立即记录" in out
    assert authenticate(SqliteRepository(), "admin", pwd) is not None


def test_init_warns_but_accepts_short_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "--admin-username", "admin", "--admin-password", "short1"]) == 0
    captured = capsys.readouterr()
    assert "警告" in captured.err
    assert _printed_password(captured.out) == ""  # 显式给的口令不回显
    assert authenticate(SqliteRepository(), "admin", "short1") is not None
