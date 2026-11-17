"""`mla_home()` 纯路径计算：平台默认、显式覆盖、错误口径与 cwd 无关性。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mla import config
from mla.config import ConfigError, mla_home


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
