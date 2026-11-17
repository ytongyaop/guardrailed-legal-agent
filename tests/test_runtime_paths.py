"""运行期路径：`mla_home()` 纯路径计算 + 阶段二默认库/缓存切换与旧库一次性复制。

全部用例只用临时目录里的哑库；不读取也不写入仓库根目录的 `mla.sqlite3`。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mla import config
from mla.config import ConfigError, Paths, RetrievalConfig, load_config, mla_home
from mla.services import bootstrap
from mla.services.bootstrap import ensure_runtime_ready


def test_windows_default_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    local_appdata = tmp_path / "LocalAppData"
    assert mla_home({"LOCALAPPDATA": str(local_appdata)}) == local_appdata / "mla"


def test_windows_without_localappdata_uses_user_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setattr(config, "_user_home", lambda: tmp_path / "profile")
    assert mla_home({}) == tmp_path / "profile" / "AppData" / "Local" / "mla"


def test_posix_default_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    xdg = tmp_path / "xdg-data"
    assert mla_home({"XDG_DATA_HOME": str(xdg)}) == xdg / "mla"


def test_posix_without_xdg_uses_local_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    monkeypatch.setattr(config, "_user_home", lambda: tmp_path / "home")
    assert mla_home({}) == tmp_path / "home" / ".local" / "share" / "mla"


def test_relative_mla_home_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        mla_home({"MLA_HOME": "relative/mla-home"})
    assert "绝对路径" in str(excinfo.value)


def test_absolute_mla_home_wins_over_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    explicit = tmp_path / "explicit-home"
    env = {"MLA_HOME": str(explicit), "LOCALAPPDATA": str(tmp_path / "other")}
    assert mla_home(env) == explicit


def test_home_does_not_depend_on_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setattr(config, "_user_home", lambda: tmp_path / "profile")
    first = tmp_path / "first-cwd"
    second = tmp_path / "second-cwd"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    from_first = mla_home({})
    monkeypatch.chdir(second)
    from_second = mla_home({})
    assert from_first == from_second == tmp_path / "profile" / "AppData" / "Local" / "mla"


def test_unknown_user_home_raises_instead_of_falling_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: False)

    def boom() -> Path:
        raise ConfigError("无法确定当前用户目录，请把 MLA_HOME 设为绝对路径")

    monkeypatch.setattr(config, "_user_home", boom)
    with pytest.raises(ConfigError):
        mla_home({})


def test_home_calculation_does_not_create_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    local_appdata = tmp_path / "LocalAppData"
    resolved = mla_home({"LOCALAPPDATA": str(local_appdata)})
    assert resolved == local_appdata / "mla"
    assert not local_appdata.exists()


# ---------- 阶段二：默认库 / 缓存切到 MLA_HOME（AC-01、AC-10） ----------


def _write_dummy_db(path: Path, *, users: int, contracts: int) -> None:
    """造一个只含账号与合同的哑库：不依赖业务 schema，只看行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL);"
            "CREATE TABLE contracts (contract_id TEXT PRIMARY KEY, filename TEXT);"
        )
        conn.executemany("INSERT INTO users VALUES (?, ?)", [(f"U-{i}", f"user{i}") for i in range(users)])
        conn.executemany(
            "INSERT INTO contracts VALUES (?, ?)", [(f"C-{i}", f"c{i}.txt") for i in range(contracts)]
        )


def _count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0])


def test_default_db_and_index_follow_mla_home_not_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两个不同 cwd 下解析出的默认库与缓存目录相同，都在 MLA_HOME 下。"""
    home = tmp_path / "home"
    first = tmp_path / "first-cwd"
    second = tmp_path / "second-cwd"
    first.mkdir()
    second.mkdir()
    env = {"MLA_HOME": str(home)}

    monkeypatch.chdir(first)
    from_first = load_config(env)
    monkeypatch.chdir(second)
    from_second = load_config(env)

    assert from_first.paths.db == from_second.paths.db == str(home / "mla.sqlite3")
    assert from_first.retrieval.index_dir == from_second.retrieval.index_dir == str(home / "index")


def test_dataclass_defaults_use_mla_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`Paths()` / `RetrievalConfig()` 的字段默认值同样走 MLA_HOME，不再取 cwd。"""
    home = tmp_path / "home"
    monkeypatch.setenv("MLA_HOME", str(home))
    assert Paths().db == str(home / "mla.sqlite3")
    assert RetrievalConfig().index_dir == str(home / "index")


def test_legacy_db_copy_keeps_accounts_and_contracts(tmp_path: Path) -> None:
    """旧 cwd 有库、新库不存在：一致性复制到 MLA_HOME，数据行数不变，旧库保留。"""
    home = tmp_path / "home"
    old_cwd = tmp_path / "old-cwd"
    _write_dummy_db(old_cwd / "mla.sqlite3", users=2, contracts=1)

    report = ensure_runtime_ready(env={"MLA_HOME": str(home)}, cwd=old_cwd)

    assert report.db_path == str(home / "mla.sqlite3")
    assert report.copied_from == str(old_cwd / "mla.sqlite3")
    assert report.created_dir is True
    assert report.warnings == []
    assert _count(Path(report.db_path), "users") == 2
    assert _count(Path(report.db_path), "contracts") == 1
    assert (old_cwd / "mla.sqlite3").is_file()


def test_existing_home_db_wins_without_merging(tmp_path: Path) -> None:
    """新库与旧库都在：以新库为准，不合并、不覆盖。"""
    home = tmp_path / "home"
    old_cwd = tmp_path / "old-cwd"
    _write_dummy_db(home / "mla.sqlite3", users=3, contracts=0)
    _write_dummy_db(old_cwd / "mla.sqlite3", users=2, contracts=1)

    report = ensure_runtime_ready(env={"MLA_HOME": str(home)}, cwd=old_cwd)

    assert report.db_path == str(home / "mla.sqlite3")
    assert report.copied_from is None
    assert report.created_dir is False
    assert _count(Path(report.db_path), "users") == 3
    assert _count(Path(report.db_path), "contracts") == 0


def test_explicit_mla_db_is_used_without_copy(tmp_path: Path) -> None:
    """显式 MLA_DB 优先且绝不自动搬迁：不复制旧库、不建目录、不建文件。"""
    home = tmp_path / "home"
    old_cwd = tmp_path / "old-cwd"
    _write_dummy_db(old_cwd / "mla.sqlite3", users=2, contracts=1)
    explicit = tmp_path / "elsewhere" / "custom.sqlite3"

    report = ensure_runtime_ready(env={"MLA_HOME": str(home), "MLA_DB": str(explicit)}, cwd=old_cwd)

    assert report.db_path == str(explicit)
    assert report.copied_from is None
    assert report.created_dir is False
    assert report.warnings == []
    assert not explicit.exists()
    assert not (home / "mla.sqlite3").exists()


def test_relative_mla_db_keeps_legacy_semantics_with_warning(tmp_path: Path) -> None:
    """显式相对 MLA_DB 按原语义（相对当前工作目录）支持，但必须留下警告。"""
    home = tmp_path / "home"
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    report = ensure_runtime_ready(env={"MLA_HOME": str(home), "MLA_DB": "local.sqlite3"}, cwd=work_dir)

    assert report.db_path == str(work_dir / "local.sqlite3")
    assert report.copied_from is None
    assert len(report.warnings) == 1
    assert "相对路径" in report.warnings[0]
    assert not (work_dir / "local.sqlite3").exists()


def test_missing_legacy_db_creates_nothing(tmp_path: Path) -> None:
    """两者都不存在：返回新路径，但不创建目录、不创建空库。"""
    home = tmp_path / "home"
    work_dir = tmp_path / "fresh-cwd"
    work_dir.mkdir()

    report = ensure_runtime_ready(env={"MLA_HOME": str(home)}, cwd=work_dir)

    assert report.db_path == str(home / "mla.sqlite3")
    assert report.copied_from is None
    assert report.created_dir is False
    assert not home.exists()
    assert not (home / "mla.sqlite3").exists()


def test_repeated_calls_are_idempotent(tmp_path: Path) -> None:
    """连续调用结果一致：第二次不再复制，数据不变。"""
    home = tmp_path / "home"
    old_cwd = tmp_path / "old-cwd"
    _write_dummy_db(old_cwd / "mla.sqlite3", users=2, contracts=1)
    env = {"MLA_HOME": str(home)}

    first = ensure_runtime_ready(env=env, cwd=old_cwd)
    second = ensure_runtime_ready(env=env, cwd=old_cwd)

    assert second.db_path == first.db_path
    assert second.copied_from is None
    assert second.created_dir is False
    assert _count(Path(second.db_path), "users") == 2


def test_home_equal_to_cwd_does_not_overwrite_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MLA_HOME 就是当前目录时源与目标同一文件：必须跳过复制，不能自己覆盖自己。"""
    home = tmp_path / "same-dir"
    _write_dummy_db(home / "mla.sqlite3", users=2, contracts=1)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("源与目标同一路径时不得复制")

    monkeypatch.setattr(bootstrap, "_copy_database", _boom)

    report = ensure_runtime_ready(env={"MLA_HOME": str(home)}, cwd=home)

    assert report.db_path == str(home / "mla.sqlite3")
    assert report.copied_from is None
    assert _count(Path(report.db_path), "users") == 2
