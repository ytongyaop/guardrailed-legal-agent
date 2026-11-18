"""`tests/api` 的核对链路辅助（批次 3）：等 job、装配假模型、直接落库 / 计数。

为什么单独一个模块而不是塞进 `api_helpers.py`：那个文件是批次 2 的公共夹具，批次 3 只新增
用例不回头改它；这里的东西全部服务于异步链路（202 + 轮询），跟批次 2 的同步端点不是一类。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parents[1]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import FakeGenerator  # noqa: E402  # pyright: ignore[reportMissingImports]

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.domain.models import CheckTask, Role  # noqa: E402
from mla.services.auth import register as register_user  # noqa: E402

FAILED_STATUS = "failed"
SUCCEEDED_STATUS = "succeeded"
TERMINAL = {FAILED_STATUS, SUCCEEDED_STATUS}
POLL_INTERVAL = 0.02
DEFAULT_TIMEOUT = 60.0


def wait_job(client: TestClient, job_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """轮询到终态；状态机只有 queued → running → succeeded/failed 四种。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"job {job_id} 在 {timeout} 秒内未完成")


def count_rows(db_path: str, table: str = "tasks") -> int:
    """直接数表（无模型时"没有任何任务落库"必须查库，不能只看接口）。"""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0] or 0)


def seed_task(db_path: str, task: CheckTask) -> CheckTask:
    """把领域任务直接落库：历史/报告用例不重跑模型，只测接口口径。"""
    repo = SqliteRepository(db_path=db_path)
    try:
        repo.save_task(task)
    finally:
        repo.close()
    return task


def enable_fake_model(monkeypatch, *, response: Any = None) -> FakeGenerator:
    """打开"有模型"环境但绝不联网：模型换成 `FakeGenerator`，可选字段挑选也短路。

    `MLA_ALLOW_NO_LLM` 必须删掉（根 conftest 默认置 1 用于无模型用例），否则 `model_first`
    会被强制关掉，跑出来的是旧路径。
    """
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    monkeypatch.setenv("MLA_LLM_API_KEY", "fake-key-for-api-tests")
    monkeypatch.setenv("MLA_EXTRACT_MODE", "regex")
    monkeypatch.delenv("MLA_MODEL_FIRST", raising=False)
    generator = FakeGenerator() if response is None else FakeGenerator(response)
    monkeypatch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: generator)
    monkeypatch.setattr("mla.api.presenters._default_generator", lambda: None)
    return generator


def add_user_to_enterprise(
    client: TestClient,
    db_path: str,
    *,
    username: str,
    enterprise_id: str,
    password: str,
) -> dict[str, Any]:
    """在指定企业下建一个普通用户并登录（注册接口每次新建企业，同企业第二人只能直接写库）。"""
    repo = SqliteRepository(db_path=db_path)
    try:
        register_user(repo, username, password, Role.USER, enterprise_id)
    finally:
        repo.close()
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload
