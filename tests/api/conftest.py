"""`tests/api` 公共夹具：临时运行目录（由根 conftest 隔离）、可注入时钟、带会话的客户端。

不联网、不读真实 `auth.json`：根 `conftest.py` 已把 `MLA_HOME` / `MLA_DB` / `MLA_INDEX_DIR`
指到本用例专属的临时目录，`create_app()` 只会在这份空目录里引导。
Windows 的凭据 ACL 调用（`whoami` / `icacls`）一律打桩，测试不碰真实系统 ACL。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mla.adapters import auth_file
from mla.api.app import create_app


class FakeClock:
    """可注入时钟：会话过期用例靠它「过时间」，不做真实等待。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 5, 9, 0, 0)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now


@dataclass
class ApiClient:
    """测试用客户端包装：保留 app（取 db_path）与时钟（推进过期）。"""

    app: FastAPI
    client: TestClient
    clock: FakeClock

    @property
    def db_path(self) -> str:
        state = self.app.state.api
        return str(state.db_path)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(clock: FakeClock) -> Iterator[ApiClient]:
    """默认客户端：真实时钟语义（假时钟，但不缩短过期时间）。"""
    app = create_app(clock=clock.now)
    with TestClient(app) as client:
        yield ApiClient(app=app, client=client, clock=clock)


@pytest.fixture
def make_api() -> Iterator[Callable[..., ApiClient]]:
    """自定义过期时间 / 时钟的客户端工厂；用例结束时统一关闭。"""
    opened: list[TestClient] = []

    def _make(**kwargs: Any) -> ApiClient:
        injected = kwargs.pop("clock", None)
        fake = injected if isinstance(injected, FakeClock) else FakeClock()
        app = create_app(clock=fake.now, **kwargs)
        client = TestClient(app)
        opened.append(client)
        return ApiClient(app=app, client=client, clock=fake)

    yield _make
    for client in opened:
        client.close()


@pytest.fixture(autouse=True)
def fake_acl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 `whoami` / `icacls` 换成可预测的假输出（与 tests/test_credentials.py 同一套）。"""
    state: dict[str, Any] = {"user": "TESTPC\\tester", "commands": []}

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        state["commands"].append(list(args))
        if args[0] == "whoami":
            return subprocess.CompletedProcess(args, 0, f"{state['user']}\n", "")
        text = (
            f"{args[1]} {state['user']}:(F)\n"
            "                     NT AUTHORITY\\SYSTEM:(F)\n"
            "\n已成功处理 1 个文件；处理 0 个文件时失败\n"
        )
        return subprocess.CompletedProcess(args, 0, text, "")

    monkeypatch.setattr(auth_file, "_run_cmd", fake_run)
    return state
